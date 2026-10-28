from __future__ import annotations

import functools
import json
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar, cast


_opik_enabled = False
_opik_project_name = ""
_trace_depth: ContextVar[int] = ContextVar("opik_trace_depth", default=0)
_llm_usage_totals: ContextVar[dict[str, int] | None] = ContextVar(
    "opik_llm_usage_totals",
    default=None,
)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@dataclass(frozen=True)
class OperationOutcome:
    operation: str
    status: Literal["succeeded", "rejected", "partial", "failed"]
    reason: str
    persisted: bool | None = None
    cache_hit: bool | None = None
    response_message_id: int | None = None
    metadata: dict[str, object] | None = None


def log_operation_outcome(
    outcome: OperationOutcome,
    *,
    chat_id: int,
    message_id: int,
    invocation: str,
    route_action: str | None = None,
    route_reason: str | None = None,
    elapsed_seconds: float | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    outcome_data = asdict(outcome)
    metadata = outcome_data.pop("metadata") or {}
    event = {
        "event": "operation_outcome",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "message_id": message_id,
        "invocation": invocation,
        "route_action": route_action,
        "route_reason": route_reason,
        "elapsed_ms": round(elapsed_seconds * 1000, 1) if elapsed_seconds is not None else None,
        "provider": provider,
        "model": model,
        **outcome_data,
        **metadata,
    }
    logging.getLogger("tg_summary_bot.operations").info(json.dumps(event, ensure_ascii=False))
    update_opik_span_metadata({key: value for key, value in event.items() if key != "created_at"})
    if _opik_enabled:
        try:
            import opik
            opik.opik_context.update_current_trace(metadata={key: value for key, value in event.items() if key != "created_at"})
        except Exception:
            pass


def configure_opik_tracing(*, enabled: bool, project_name: str) -> None:
    global _opik_enabled, _opik_project_name
    _opik_enabled = enabled
    _opik_project_name = project_name


def opik_track(
    *,
    name: str,
    type: str = "general",
    capture_input: bool = False,
    capture_output: bool = False,
    create_duplicate_root_span: bool = False,
) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        tracked_func: F | None = None
        tracked_project_name: str | None = None

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            nonlocal tracked_func, tracked_project_name
            if not _opik_enabled:
                return await func(*args, **kwargs)

            if tracked_func is None or tracked_project_name != _opik_project_name:
                try:
                    import opik
                except ImportError:
                    return await func(*args, **kwargs)

                tracked_func = cast(
                    F,
                    opik.track(
                        name=name,
                        type=type,
                        project_name=_opik_project_name or None,
                        capture_input=capture_input,
                        capture_output=capture_output,
                        create_duplicate_root_span=create_duplicate_root_span,
                    )(func),
                )
                tracked_project_name = _opik_project_name

            depth = _trace_depth.get()
            depth_token = _trace_depth.set(depth + 1)
            totals_token = None
            if depth == 0:
                totals_token = _llm_usage_totals.set(
                    {
                        "call_count": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    }
                )
            try:
                return await tracked_func(*args, **kwargs)
            finally:
                _trace_depth.reset(depth_token)
                if totals_token is not None:
                    _llm_usage_totals.reset(totals_token)

        return cast(F, wrapper)

    return decorator


class HandledOperationError(RuntimeError):
    def __init__(self, outcome: OperationOutcome) -> None:
        super().__init__(outcome.reason)
        self.outcome = outcome


def opik_operation(*, name: str):
    """Record handled partial/failure outcomes as trace errors, without re-failing the handler.

    Opik's decorator finalization overwrites manually-set root error_info on a normal
    return. A content-free sentinel passes through that decorator, then is consumed here.
    """
    def decorator(func):
        # A real root span anchors nested spans. A trace-only root is popped by
        # this SDK when its first parentless child completes, orphaning later work.
        @opik_track(name=name, create_duplicate_root_span=True)
        async def tracked(*args, **kwargs):
            outcome = await func(*args, **kwargs)
            if _opik_enabled and outcome.status in {"failed", "partial"}:
                raise HandledOperationError(outcome)
            return outcome

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await tracked(*args, **kwargs)
            except HandledOperationError as exc:
                return exc.outcome
        return wrapper
    return decorator


def update_opik_span_metadata(metadata: dict[str, Any], *, name: str | None = None) -> None:
    if not _opik_enabled:
        return
    try:
        import opik
    except ImportError:
        return
    span_updated = False
    try:
        opik.opik_context.update_current_span(metadata=metadata, name=name)
        if name:
            opik.opik_context.update_current_trace(name=name, metadata=metadata)
        span_updated = True
    except Exception:
        pass
    if span_updated:
        return
    try:
        opik.opik_context.update_current_trace(metadata=metadata, name=name)
    except Exception:
        return


@contextmanager
def opik_stage(name: str, metadata: dict[str, Any] | None = None):
    """Content-free orchestration timing; never attach speech, paths or raw exceptions."""
    started = time.perf_counter()
    manager = None
    span = None
    failed = False
    if _opik_enabled:
        try:
            import opik
            manager = opik.start_as_current_span(name=name, project_name=_opik_project_name or None,
                                                 metadata=metadata or {})
            span = manager.__enter__()
        except Exception:
            manager = None
    try:
        yield
    except BaseException as exc:
        failed = True
        if span is not None:
            span.error_info = {"exception_type": type(exc).__name__, "message": f"{name}_failed", "traceback": ""}
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        if span is not None:
            span.metadata = {**(metadata or {}), **(span.metadata or {}), "elapsed_ms": elapsed_ms,
                             "stage_status": "failed" if failed else "succeeded"}
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        if name.startswith("speech."):
            logging.info("Speech stage=%s elapsed_ms=%s status=%s", name, elapsed_ms,
                         "failed" if failed else "succeeded")


@asynccontextmanager
async def traced_gpu_lock(lock, operation: str):
    with opik_stage(f"{operation}.gpu_wait"):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def update_opik_llm_usage(
    *,
    provider: str,
    model: str,
    usage: dict[str, int] | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not _opik_enabled:
        return
    try:
        import opik
    except ImportError:
        return

    normalized_usage = _normalize_usage(usage or {})
    span_metadata = {
        "provider": provider,
        "model": model,
        **(metadata or {}),
    }
    if normalized_usage:
        span_metadata.update(
            {
                "prompt_tokens": normalized_usage["prompt_tokens"],
                "completion_tokens": normalized_usage["completion_tokens"],
                "total_tokens": normalized_usage["total_tokens"],
            }
        )

    try:
        opik.opik_context.update_current_span(
            metadata=span_metadata,
            usage=normalized_usage or None,
            provider=provider,
            model=model or None,
        )
    except Exception:
        pass

    trace_metadata: dict[str, Any] = {
        "llm_last_provider": provider,
        "llm_last_model": model,
    }
    for key in (
        "num_ctx",
        "num_predict",
        "total_duration_s",
        "eval_duration_s",
        "eval_tokens_per_second",
        "output_chars",
        "ctx_saturated",
        "output_too_short",
    ):
        if metadata and key in metadata:
            trace_metadata[f"llm_last_{key}"] = metadata[key]
    totals = _llm_usage_totals.get()
    if normalized_usage and totals is not None:
        totals["call_count"] += 1
        totals["prompt_tokens"] += normalized_usage["prompt_tokens"]
        totals["completion_tokens"] += normalized_usage["completion_tokens"]
        totals["total_tokens"] += normalized_usage["total_tokens"]
        trace_metadata.update(
            {
                "llm_call_count": totals["call_count"],
                "llm_prompt_tokens": totals["prompt_tokens"],
                "llm_completion_tokens": totals["completion_tokens"],
                "llm_total_tokens": totals["total_tokens"],
            }
        )
    elif normalized_usage:
        trace_metadata.update(
            {
                "llm_call_count": 1,
                "llm_prompt_tokens": normalized_usage["prompt_tokens"],
                "llm_completion_tokens": normalized_usage["completion_tokens"],
                "llm_total_tokens": normalized_usage["total_tokens"],
            }
        )

    try:
        opik.opik_context.update_current_trace(metadata=trace_metadata)
    except Exception:
        return


def _normalize_usage(usage: dict[str, int]) -> dict[str, int]:
    prompt_tokens = _int_usage_value(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion_tokens = _int_usage_value(
        usage.get("completion_tokens") or usage.get("output_tokens")
    )
    total_tokens = _int_usage_value(usage.get("total_tokens"))
    if not total_tokens and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    if not (prompt_tokens or completion_tokens or total_tokens):
        return {}
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _int_usage_value(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
