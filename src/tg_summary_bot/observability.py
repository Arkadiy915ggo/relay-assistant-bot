from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, TypeVar, cast


_opik_enabled = False
_opik_project_name = ""
_trace_depth: ContextVar[int] = ContextVar("opik_trace_depth", default=0)
_llm_usage_totals: ContextVar[dict[str, int] | None] = ContextVar(
    "opik_llm_usage_totals",
    default=None,
)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


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
                        create_duplicate_root_span=False,
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


def update_opik_span_metadata(metadata: dict[str, Any]) -> None:
    if not _opik_enabled:
        return
    try:
        import opik
    except ImportError:
        return
    span_updated = False
    try:
        opik.opik_context.update_current_span(metadata=metadata)
        span_updated = True
    except Exception:
        pass
    if span_updated:
        return
    try:
        opik.opik_context.update_current_trace(metadata=metadata)
    except Exception:
        return


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
