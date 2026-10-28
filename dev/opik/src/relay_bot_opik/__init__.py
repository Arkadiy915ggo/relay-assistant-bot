"""Explicitly loaded dev exporter. Importing this package does not import Opik."""
from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextvars import Context
from typing import Any

from tg_summary_bot.observability import SpanRecord, TraceOptions


QUEUE_SIZE = 128
BATCH_SIZE = 64
BATCH_DELAY_SECONDS = 0.2
REQUEST_TIMEOUT_SECONDS = 5
SHUTDOWN_TIMEOUT_SECONDS = 3


def _load_sdk():
    # Set these BEFORE importing opik: its __init__ can initialize Sentry.
    # Do not inherit ~/.opik.config, including credentials or telemetry settings.
    os.environ["OPIK_SENTRY_ENABLE"] = "false"
    os.environ["OPIK_CONFIG_PATH"] = os.devnull
    from opik.rest_api.client import AsyncOpikApi
    from opik.rest_api.types.span_write import SpanWrite
    from opik.rest_api.types.trace_write import TraceWrite
    from uuid6 import uuid7
    return AsyncOpikApi, SpanWrite, TraceWrite, uuid7


async def create_backend(options: TraceOptions, on_error: Callable[[str], None]):
    if not options.api_key.strip():
        raise ValueError("missing_api_key")
    # SDK import is expensive; never run it on the bot event loop.
    sdk = await asyncio.to_thread(_load_sdk)
    import httpx
    transport = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        client = sdk[0](
            base_url=options.endpoint or "https://www.comet.com/opik/api/",
            api_key=options.api_key, workspace_name=options.workspace or "default",
            httpx_client=transport,
        )
        return OpikBackend(options, on_error, client, transport, sdk[1:])
    except BaseException:
        await transport.aclose()
        raise


class OpikBackend:
    def __init__(self, options, on_error, client, transport, sdk):
        self.options, self.on_error = options, on_error
        self.client, self.transport = client, transport
        self.SpanWrite, self.TraceWrite, self.uuid7 = sdk
        self.queue: asyncio.Queue[SpanRecord] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.failed = False
        self.closed = False
        self.worker = asyncio.create_task(self._run(), name="dev-opik-export", context=Context())

    def new_id(self) -> str:
        return str(self.uuid7())

    def record_span(self, record: SpanRecord) -> None:
        if self.failed or self.closed:
            return
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            self._fail("queue_full")
            self.worker.cancel()

    def _discard_queue(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()

    def _fail(self, reason: str) -> None:
        if not self.failed:
            self.failed = True
            self._discard_queue()
            self.on_error(reason)

    async def _run(self) -> None:
        try:
            while not self.failed:
                batch = [await self.queue.get()]
                try:
                    await asyncio.sleep(BATCH_DELAY_SECONDS)
                    while len(batch) < BATCH_SIZE and not self.queue.empty():
                        batch.append(self.queue.get_nowait())
                    await asyncio.wait_for(self._send(batch), REQUEST_TIMEOUT_SECONDS)
                finally:
                    for _ in batch:
                        self.queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception:
            # One content-free warning via the host. No auth/network retry loops.
            self._fail("export_failed")
        finally:
            self._discard_queue()
            await self.transport.aclose()

    async def _send(self, records: list[SpanRecord]) -> None:
        spans, traces = [], []
        for record in records:
            common: dict[str, Any] = {
                "project_name": self.options.project_name,
                "name": record.name, "start_time": record.start_time, "end_time": record.end_time,
                "metadata": record.metadata, "error_info": record.error_info, "source": "sdk",
                "tags": record.tags or None,
            }
            if self.options.capture_content:
                common.update(input=record.input, output=record.output)
            spans.append(self.SpanWrite(
                **common, id=record.id, trace_id=record.trace_id,
                parent_span_id=record.parent_span_id, type=record.type,
                usage=record.usage, provider=record.provider, model=record.model,
            ))
            if record.parent_span_id is None:
                traces.append(self.TraceWrite(**common, id=record.trace_id))
        request_options = {"max_retries": 0, "timeout_in_seconds": REQUEST_TIMEOUT_SECONDS}
        if traces:
            await self.client.traces.create_traces(traces=traces, request_options=request_options)
        if spans:
            await self.client.spans.create_spans(spans=spans, request_options=request_options)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await asyncio.wait_for(self.queue.join(), SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._fail("shutdown_timeout")
        finally:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            # Also covers cancellation before the worker's first execution.
            self._discard_queue()
            await self.transport.aclose()
