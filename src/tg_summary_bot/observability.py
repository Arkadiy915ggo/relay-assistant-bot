from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast


_opik_enabled = False
_opik_project_name = ""

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
                    )(func),
                )
                tracked_project_name = _opik_project_name

            return await tracked_func(*args, **kwargs)

        return cast(F, wrapper)

    return decorator


def update_opik_span_metadata(metadata: dict[str, Any]) -> None:
    if not _opik_enabled:
        return
    try:
        import opik
    except ImportError:
        return
    try:
        opik.opik_context.update_current_span(metadata=metadata)
    except Exception:
        return
