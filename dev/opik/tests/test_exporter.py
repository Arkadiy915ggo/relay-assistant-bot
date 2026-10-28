import asyncio
import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import relay_bot_opik as exporter
from tg_summary_bot.observability import SpanRecord, TraceOptions


def record(*, root=True):
    return SpanRecord(
        id="span", trace_id="trace", parent_span_id=None if root else "parent",
        name="synthetic", type="llm", start_time=datetime.now(timezone.utc),
        end_time=datetime.now(timezone.utc), metadata={"safe": True},
        input={"text": "private prompt"}, output={"text": "private answer"},
    )


class ExporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.options = TraceOptions("opik", "synthetic-project", "fake-key")
        self.errors = Mock()
        self.transport = SimpleNamespace(aclose=AsyncMock())
        self.client = SimpleNamespace(
            traces=SimpleNamespace(create_traces=AsyncMock()),
            spans=SimpleNamespace(create_spans=AsyncMock()),
        )
        self.backend = exporter.OpikBackend(
            self.options, self.errors, self.client, self.transport,
            (lambda **kwargs: kwargs, lambda **kwargs: kwargs, lambda: "fake-id"),
        )

    async def asyncTearDown(self):
        await self.backend.aclose()

    async def test_batches_final_records_without_content_and_without_retries(self):
        self.backend.record_span(record(root=False))
        self.backend.record_span(record())
        await self.backend.aclose()
        traces = self.client.traces.create_traces.await_args.kwargs
        spans = self.client.spans.create_spans.await_args.kwargs
        self.assertEqual(len(traces["traces"]), 1)
        self.assertEqual(len(spans["spans"]), 2)
        self.assertEqual(spans["request_options"]["max_retries"], 0)
        self.assertNotIn("private", str(traces) + str(spans))
        self.assertTrue(self.backend.worker.done())
        self.errors.assert_not_called()

    async def test_content_capture_is_explicit(self):
        self.backend.options = replace(self.options, capture_content=True)
        self.backend.record_span(record())
        await self.backend.aclose()
        spans = self.client.spans.create_spans.await_args.kwargs["spans"]
        self.assertEqual(spans[0]["input"], {"text": "private prompt"})

    async def test_first_auth_or_network_failure_stops_all_further_sends(self):
        self.client.traces.create_traces.side_effect = RuntimeError("private response with fake-key")
        self.backend.record_span(record())
        await self.backend.queue.join()
        for _ in range(10):
            self.backend.record_span(record())
        await asyncio.sleep(0)
        self.client.traces.create_traces.assert_awaited_once()
        self.client.spans.create_spans.assert_not_awaited()
        self.errors.assert_called_once_with("export_failed")
        self.assertTrue(self.backend.failed)
        self.assertTrue(self.backend.worker.done())
        self.assertTrue(self.backend.queue.empty())

    async def test_queue_overflow_is_bounded_and_stops_exporter(self):
        for _ in range(exporter.QUEUE_SIZE + 10):
            self.backend.record_span(record())
        await self.backend.aclose()
        self.errors.assert_called_once_with("queue_full")
        self.client.traces.create_traces.assert_not_awaited()
        self.assertTrue(self.backend.queue.empty())

    async def test_shutdown_cancels_stuck_upload_and_closes_transport(self):
        entered = asyncio.Event()
        async def stuck(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.client.traces.create_traces.side_effect = stuck
        self.backend.record_span(record())
        await entered.wait()
        with patch.object(exporter, "SHUTDOWN_TIMEOUT_SECONDS", 0.01):
            await self.backend.aclose()
        self.assertTrue(self.backend.worker.done())
        self.transport.aclose.assert_awaited()
        self.errors.assert_called_once_with("shutdown_timeout")

    async def test_factory_rejects_key_before_loading_sdk(self):
        with patch.object(exporter, "_load_sdk", side_effect=AssertionError("SDK import")):
            with self.assertRaisesRegex(ValueError, "missing_api_key"):
                await exporter.create_backend(replace(self.options, api_key="  "), self.errors)
