"""Pinned SDK contract smoke: real serialization, fake HTTP, no home config or live service."""
import asyncio
import json
import os
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import relay_bot_opik as exporter
from tg_summary_bot.observability import SpanRecord, TraceOptions


class SDKContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_serializes_batches_with_telemetry_off_and_explicit_auth(self):
        requests = []
        async def receive(request):
            requests.append(request)
            return httpx.Response(204)
        with patch.dict(os.environ, {"OPIK_CONFIG_PATH": "/must-not-read-home-config",
                                     "OPIK_SENTRY_ENABLE": "true"}), \
             patch("socket.getaddrinfo", side_effect=AssertionError("real DNS")), \
             patch("socket.socket.connect", side_effect=AssertionError("real network")):
            sdk = await asyncio.to_thread(exporter._load_sdk)
            self.assertEqual(os.environ["OPIK_SENTRY_ENABLE"], "false")
            self.assertEqual(os.environ["OPIK_CONFIG_PATH"], os.devnull)
            import sentry_sdk
            self.assertFalse(sentry_sdk.is_initialized())
            transport = httpx.AsyncClient(transport=httpx.MockTransport(receive))
            client = sdk[0](base_url="https://synthetic.invalid/api/", api_key="explicit-key",
                            workspace_name="synthetic-workspace", httpx_client=transport)
            backend = exporter.OpikBackend(TraceOptions("opik", "synthetic", "explicit-key"),
                                            self.fail, client, transport, sdk[1:])
            try:
                span_id, trace_id = backend.new_id(), backend.new_id()
                self.assertEqual(uuid.UUID(span_id).version, 7)
                backend.record_span(SpanRecord(
                    id=span_id, trace_id=trace_id, parent_span_id=None, name="synthetic",
                    type="llm", start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc),
                    usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                    input={"text": "private prompt"}, error_info={"message": "safe_failure_code",
                                                               "exception_type": "RuntimeError", "traceback": ""},
                ))
                await backend.aclose()
            finally:
                await backend.aclose()
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.headers["authorization"] == "explicit-key" for request in requests))
        self.assertTrue(all(request.headers["comet-workspace"] == "synthetic-workspace" for request in requests))
        self.assertNotIn("private prompt", "".join(request.content.decode() for request in requests))
        bodies = [json.loads(request.content) for request in requests]
        self.assertEqual(bodies[0]["traces"][0]["id"], trace_id)
        self.assertEqual(bodies[1]["spans"][0]["trace_id"], trace_id)
        self.assertEqual(bodies[1]["spans"][0]["usage"]["total_tokens"], 5)

    async def test_real_sdk_auth_and_server_errors_are_not_retried(self):
        sdk = await asyncio.to_thread(exporter._load_sdk)
        for status in (401, 503):
            requests = []
            errors = []
            async def receive(request):
                requests.append(request)
                return httpx.Response(status, json={"message": "synthetic error"})
            transport = httpx.AsyncClient(transport=httpx.MockTransport(receive))
            client = sdk[0](base_url="https://synthetic.invalid/api/", api_key="explicit-key",
                            workspace_name="synthetic", httpx_client=transport)
            backend = exporter.OpikBackend(TraceOptions("opik", "synthetic", "explicit-key"),
                                            errors.append, client, transport, sdk[1:])
            try:
                backend.record_span(SpanRecord(
                    id=backend.new_id(), trace_id=backend.new_id(), parent_span_id=None,
                    name="synthetic", type="general", start_time=datetime.now(timezone.utc),
                ))
                await asyncio.wait_for(backend.queue.join(), 2)
                self.assertEqual(len(requests), 1)
                self.assertEqual(errors, ["export_failed"])
            finally:
                await backend.aclose()
