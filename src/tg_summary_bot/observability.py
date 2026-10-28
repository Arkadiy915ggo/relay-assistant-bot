"""Local operation logging and an opt-in, provider-neutral tracing interface.

No exporter is imported until both explicit activation and a key are present.
Exporters receive completed records; they never wrap or re-run application code.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib.metadata import entry_points
from typing import Any, Literal, Protocol, TypeVar, cast

from tg_summary_bot.config import Settings


@dataclass(frozen=True)
class TraceOptions:
    backend: str
    project_name: str
    api_key: str = field(repr=False)
    workspace: str = ""
    endpoint: str = ""
    capture_content: bool = False


@dataclass(frozen=True)
class TracingStatus:
    state: str = "disabled"
    backend: str = ""
    project_name: str = ""
    capture_content: bool = False

    @property
    def active(self) -> bool:
        return self.state == "active"


@dataclass
class SpanRecord:
    id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    type: str
    start_time: datetime
    end_time: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error_info: dict[str, str] | None = None
    usage: dict[str, int] | None = None
    provider: str | None = None
    model: str | None = None
    tags: list[str] = field(default_factory=list)


class TraceBackend(Protocol):
    def new_id(self) -> str: ...
    def record_span(self, record: SpanRecord) -> None: ...
    async def aclose(self) -> None: ...


_backend: TraceBackend | None = None
_status = TracingStatus()
_current_span: ContextVar[SpanRecord | None] = ContextVar("current_span", default=None)
_root_span: ContextVar[SpanRecord | None] = ContextVar("root_span", default=None)
F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def inspect_tracing(settings: Settings) -> TracingStatus:
    """Diagnose configuration without importing an exporter or its SDK."""
    state = "disabled"
    if settings.tracing_enabled:
        if not settings.tracing_api_key.strip():
            state = "missing_api_key"
        else:
            try:
                entries = entry_points(group="tg_summary_bot.observability", name=settings.tracing_backend)
                state = "configured" if entries else "missing_backend"
            except Exception:
                state = "discovery_failed"
    return TracingStatus(state, settings.tracing_backend, settings.tracing_project_name,
                         settings.tracing_capture_content)


def tracing_status() -> TracingStatus:
    return _status


def disable_tracing(reason: str = "backend_error") -> None:
    global _status
    if _status.active:
        _status = TracingStatus(reason, _status.backend, _status.project_name,
                                _status.capture_content)
        # Never log raw provider errors, endpoints or credentials.
        logging.warning("Tracing disabled: %s", reason)


async def configure_tracing(settings: Settings) -> TracingStatus:
    """Called once at startup, independently of the number of model clients."""
    global _backend, _status
    await close_tracing()
    _status = inspect_tracing(settings)
    if _status.state != "configured":
        if settings.tracing_enabled:
            logging.warning("Tracing disabled: %s", _status.state)
        return _status
    try:
        entries = entry_points(group="tg_summary_bot.observability", name=settings.tracing_backend)
        if len(entries) != 1:
            raise RuntimeError("ambiguous_backend")
        factory = next(iter(entries)).load()
        options = TraceOptions(
            backend=settings.tracing_backend, project_name=settings.tracing_project_name,
            api_key=settings.tracing_api_key.strip(), workspace=settings.tracing_workspace,
            endpoint=settings.tracing_endpoint, capture_content=settings.tracing_capture_content,
        )
        _backend = await factory(options, disable_tracing)
    except Exception:
        _status = TracingStatus("initialization_failed", _status.backend,
                                _status.project_name, _status.capture_content)
        logging.warning("Tracing disabled: initialization_failed")
    else:
        _status = TracingStatus("active", _status.backend, _status.project_name,
                                _status.capture_content)
    return _status


async def close_tracing() -> TracingStatus:
    global _backend, _status
    backend, _backend = _backend, None
    if backend is not None:
        try:
            await asyncio.wait_for(backend.aclose(), timeout=5)
        except Exception:
            disable_tracing("shutdown_failed")
    final_status, _status = _status, TracingStatus()
    return final_status


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
    safe_metadata = {key: value for key, value in event.items() if key != "created_at"}
    update_span_metadata(safe_metadata)
    root = _root_span.get()
    if root is not None:
        root.metadata.update(safe_metadata)


def get_current_span() -> SpanRecord | None:
    return _current_span.get()


def update_span_metadata(metadata: dict[str, Any], *, name: str | None = None) -> None:
    span = _current_span.get()
    if span is not None:
        span.metadata.update(metadata)
        if name:
            span.name = name
            root = _root_span.get()
            if root is not None:
                root.name = name
                root.metadata.update(metadata)


@contextmanager
def trace_stage(
    name: str, metadata: dict[str, Any] | None = None, *, type: str = "general",
) -> Iterator[SpanRecord | None]:
    """Measure a stage; export failures never alter the application outcome."""
    started = time.perf_counter()
    backend = _backend if _status.active else None
    parent = _current_span.get()
    span = None
    if backend is not None:
        try:
            span = SpanRecord(
                id=backend.new_id(), trace_id=parent.trace_id if parent else backend.new_id(),
                parent_span_id=parent.id if parent else None, name=name, type=type,
                start_time=datetime.now(timezone.utc), metadata=dict(metadata or {}),
            )
        except Exception:
            disable_tracing()
    # Isolate untraced children too, rather than mutating a parent's record.
    span_token = _current_span.set(span)
    root_token = _root_span.set(span) if parent is None else None
    failed = False
    try:
        yield span
    except BaseException as exc:
        failed = True
        if span is not None:
            span.error_info = {"exception_type": exc.__class__.__name__,
                               "message": f"{name}_failed", "traceback": ""}
        raise
    finally:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        _current_span.reset(span_token)
        if root_token is not None:
            _root_span.reset(root_token)
        if span is not None:
            span.end_time = datetime.now(timezone.utc)
            span.metadata.update(elapsed_ms=elapsed_ms,
                                 stage_status="failed" if span.error_info else "succeeded")
            if _status.active and backend is _backend:
                try:
                    backend.record_span(span)
                except Exception:
                    disable_tracing()
        if name.startswith("speech."):
            logging.info("Speech stage=%s elapsed_ms=%s status=%s", name, elapsed_ms,
                         "failed" if failed else "succeeded")


def trace(*, name: str, type: str = "general") -> Callable[[F], F]:
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_stage(name, type=type):
                return await func(*args, **kwargs)
        return cast(F, wrapper)
    return decorator


def trace_operation(*, name: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            with trace_stage(name) as span:
                outcome = await func(*args, **kwargs)
                if span is not None and outcome.status in {"failed", "partial"}:
                    span.error_info = {"exception_type": "HandledOperationError",
                                       "message": outcome.reason, "traceback": ""}
                return outcome
        return cast(F, wrapper)
    return decorator


@asynccontextmanager
async def traced_gpu_lock(lock, operation: str):
    with trace_stage(f"{operation}.gpu_wait"):
        await lock.acquire()
    try:
        yield
    finally:
        lock.release()


def update_llm_usage(
    *, provider: str, model: str, usage: dict[str, int] | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    span, root = _current_span.get(), _root_span.get()
    if span is None:
        return
    normalized = _normalize_usage(usage or {})
    span.provider, span.model = provider, model
    span.usage = normalized or None
    span.metadata.update({"provider": provider, "model": model, **(metadata or {}), **normalized})
    if root is not None:
        root.metadata.update(llm_last_provider=provider, llm_last_model=model)
        for key in ("num_ctx", "num_predict", "total_duration_s", "eval_duration_s",
                    "eval_tokens_per_second", "output_chars", "ctx_saturated", "output_too_short"):
            if metadata and key in metadata:
                root.metadata[f"llm_last_{key}"] = metadata[key]
        if normalized:
            root.metadata["llm_call_count"] = root.metadata.get("llm_call_count", 0) + 1
            for key, value in normalized.items():
                total_key = f"llm_{key}"
                root.metadata[total_key] = root.metadata.get(total_key, 0) + value


def _normalize_usage(usage: dict[str, int]) -> dict[str, int]:
    prompt = _int_usage_value(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _int_usage_value(usage.get("completion_tokens") or usage.get("output_tokens"))
    total = _int_usage_value(usage.get("total_tokens")) or prompt + completion
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total} if total else {}


def _int_usage_value(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
