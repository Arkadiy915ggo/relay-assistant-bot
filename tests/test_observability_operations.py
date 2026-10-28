import asyncio
import builtins
import json
import os
import socket
import unittest
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tg_summary_bot import config, observability
from tg_summary_bot.llm import TracedLLMClient, build_llm_client
from tg_summary_bot.observability import (
    OperationOutcome, close_tracing, configure_tracing, get_current_span,
    inspect_tracing, trace, trace_operation, trace_stage, tracing_status,
    update_span_metadata,
)


def settings(**overrides):
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:synthetic"}, clear=True), \
         patch.object(config, "load_dotenv"):
        return replace(config.load_settings(), **overrides)


class RecordingBackend:
    def __init__(self):
        self.records = []
        self.counter = 0
        self.closed = False

    def new_id(self):
        self.counter += 1
        return str(self.counter)

    def record_span(self, record):
        self.records.append(record)

    async def aclose(self):
        self.closed = True


class TracingActivationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await close_tracing()

    async def test_disabled_and_missing_key_never_discover_or_import_exporter_or_use_network(self):
        original_import = builtins.__import__
        attempted = []

        def guarded_import(name, *args, **kwargs):
            if name.startswith(("opik", "relay_bot_opik", "sentry_sdk")):
                attempted.append(name)
                raise AssertionError("unexpected optional import")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", guarded_import), \
             patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS")), \
             patch.object(observability, "entry_points", side_effect=AssertionError("discovery")):
            for enabled, key, expected in ((False, "key", "disabled"), (True, "", "missing_api_key"),
                                           (True, "  ", "missing_api_key")):
                cfg = settings(tracing_enabled=enabled, tracing_api_key=key)
                self.assertEqual(inspect_tracing(cfg).state, expected)
                await configure_tracing(cfg)
                for _ in range(3):
                    client = build_llm_client(cfg)
                    self.assertNotIsInstance(client, TracedLLMClient)
                @trace_operation(name="telegram.voice")
                async def operation():
                    with trace_stage("speech.upload"):
                        update_span_metadata({"safe": True})
                    return OperationOutcome("voice", "partial", "safe_code")
                self.assertEqual((await operation()).status, "partial")
        self.assertEqual(attempted, [])

    async def test_missing_or_broken_backend_is_nonfatal_and_warns_once(self):
        cfg = settings(tracing_enabled=True, tracing_api_key="private-key")
        with patch.object(observability, "entry_points", return_value=[]), self.assertLogs(level="WARNING") as logs:
            self.assertEqual((await configure_tracing(cfg)).state, "missing_backend")
            build_llm_client(cfg)
        self.assertEqual(len(logs.output), 1)
        entry = SimpleNamespace(load=Mock(side_effect=RuntimeError("private-key")))
        with patch.object(observability, "entry_points", return_value=[entry]), self.assertLogs(level="WARNING") as logs:
            self.assertEqual((await configure_tracing(cfg)).state, "initialization_failed")
        self.assertNotIn("private-key", str(logs.output))

    async def test_legacy_variables_and_new_explicit_overrides(self):
        env = {"TELEGRAM_BOT_TOKEN": "123:synthetic", "OPIK_ENABLED": "true",
               "OPIK_API_KEY": "legacy-key", "OPIK_CAPTURE_CONTENT": "true"}
        with patch.dict(os.environ, env, clear=True), patch.object(config, "load_dotenv"):
            cfg = config.load_settings()
            self.assertTrue(cfg.tracing_enabled)
            self.assertEqual(cfg.tracing_api_key, "legacy-key")
            self.assertTrue(cfg.tracing_capture_content)
            with patch.dict(os.environ, {"TRACING_ENABLED": "false", "TRACING_API_KEY": "",
                                         "TRACING_CAPTURE_CONTENT": "false"}):
                cfg = config.load_settings()
                self.assertFalse(cfg.tracing_enabled)
                self.assertEqual(cfg.tracing_api_key, "")
                self.assertFalse(cfg.tracing_capture_content)
        self.assertNotIn("legacy-key", repr(cfg))
        self.assertFalse(settings().tracing_capture_content)


class OperationTracingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = RecordingBackend()
        self.factory = AsyncMock(return_value=self.backend)
        entry = SimpleNamespace(load=lambda: self.factory)
        with patch.object(observability, "entry_points", return_value=[entry]):
            await configure_tracing(settings(tracing_enabled=True, tracing_api_key="fake-key"))

    async def asyncTearDown(self):
        await close_tracing()

    async def test_handled_failures_are_errors_without_raising_and_stages_keep_one_root(self):
        for status in ("failed", "partial", "succeeded", "rejected"):
            outcome = OperationOutcome("voice", status, "safe_failure_code")
            @trace_operation(name="telegram.voice")
            async def operation():
                with trace_stage("speech.upload", {"voice_id": "cartman"}):
                    pass
                return outcome
            self.assertIs(await operation(), outcome)
        roots = [record for record in self.backend.records if record.parent_span_id is None]
        self.assertEqual(len(roots), 4)
        self.assertEqual([bool(record.error_info) for record in roots], [True, True, False, False])
        for child, root in zip(self.backend.records[::2], roots):
            self.assertEqual(child.parent_span_id, root.id)
            self.assertEqual(child.trace_id, root.trace_id)
            self.assertEqual(child.metadata["voice_id"], "cartman")

    async def test_raw_errors_are_propagated_but_never_exported(self):
        @trace(name="private.operation")
        async def operation():
            with trace_stage("speech.upload"):
                raise RuntimeError("private speech text")
        with self.assertRaisesRegex(RuntimeError, "private speech text"):
            await operation()
        serialized = json.dumps([asdict(record) for record in self.backend.records], default=str)
        self.assertNotIn("private speech text", serialized)
        self.assertTrue(all(record.error_info for record in self.backend.records))
        self.assertIsNone(get_current_span())

    async def test_export_failure_does_not_repeat_model_or_change_result_or_error(self):
        self.backend.record_span = Mock(side_effect=RuntimeError("private provider error"))
        calls = 0
        @trace(name="request")
        async def operation():
            nonlocal calls
            calls += 1
            return "answer"
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(await operation(), "answer")
            self.assertEqual(await operation(), "answer")
        self.assertEqual(calls, 2)
        self.assertEqual(self.backend.record_span.call_count, 1)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private provider error", str(logs.output))
        self.assertEqual(tracing_status().state, "backend_error")

    async def test_llm_metadata_only_and_content_opt_in_and_totals(self):
        model = SimpleNamespace(model="fake", complete=AsyncMock(return_value="private answer"),
                                last_usage={"prompt_tokens": 3, "completion_tokens": 2},
                                last_metadata={"num_ctx": 10})
        client = TracedLLMClient(model, provider="openai")
        with trace_stage("telegram.question"):
            for _ in range(2):
                await client.complete(system="private system", user="private question")
        root = self.backend.records[-1]
        self.assertEqual(root.metadata["llm_total_tokens"], 10)
        self.assertEqual(root.metadata["llm_call_count"], 2)
        self.assertTrue(all(record.input is None and record.output is None for record in self.backend.records))
        self.assertNotIn("private", json.dumps([asdict(record) for record in self.backend.records], default=str))
        with patch.object(observability, "_status", replace(tracing_status(), capture_content=True)):
            await client.complete(system="private system", user="private question")
        self.assertEqual(self.backend.records[-1].input["user"], "private question")
        self.assertEqual(self.backend.records[-1].output["text"], "private answer")

    async def test_cancellation_is_preserved_and_concurrent_requests_have_distinct_roots(self):
        async def request(index):
            with trace_stage("request") as root:
                await asyncio.sleep(0)
                with trace_stage("child") as child:
                    self.assertEqual(child.parent_span_id, root.id)
                return root.trace_id
        ids = await asyncio.gather(*(request(index) for index in range(3)))
        self.assertEqual(len(set(ids)), 3)
        with self.assertRaises(asyncio.CancelledError):
            with trace_stage("cancelled"):
                raise asyncio.CancelledError()
        self.assertEqual(self.backend.records[-1].error_info["exception_type"], "CancelledError")
        self.assertIsNone(get_current_span())

    async def test_building_clients_does_not_reconfigure_exporter(self):
        cfg = settings(tracing_enabled=True, tracing_api_key="fake-key")
        for _ in range(5):
            self.assertIsInstance(build_llm_client(cfg), TracedLLMClient)
        self.factory.assert_awaited_once()
        self.assertNotIn("fake-key", repr(self.factory.await_args.args[0]))

    async def test_real_voice_orchestration_keeps_stages_under_one_root_and_safe_partial(self):
        import wave
        from pathlib import Path
        from tg_summary_bot.assistant import ChatAssistant
        from tg_summary_bot.speech_synthesizer import SpeechSynthesizer
        from tg_summary_bot.voice_presets import VoiceRegistry
        from test_joke_handlers import handler
        from test_voice_handlers import FakeMessage, HandlerTests

        case = HandlerTests("test_direct_speech_provenance_and_reply_link")
        await case.asyncSetUp()
        self.addCleanup(case.doCleanups)
        case.lock = asyncio.Lock()
        model = SimpleNamespace(model="fake", complete=AsyncMock(return_value="private model answer"),
                                unload=AsyncMock(), last_usage={"total_tokens": 5}, last_metadata={})
        case.assistant = ChatAssistant(TracedLLMClient(model, provider="ollama"), chunk_chars=1000)
        case.speech = SpeechSynthesizer(case.settings, VoiceRegistry(case.root / "local"))
        case.speech.unavailable_reason = None
        case.speech.check_available = AsyncMock(return_value=None)

        async def fake_process(argv, *, timeout, payload=None):
            if payload is not None:
                with wave.open(json.loads(payload)["output"], "wb") as audio:
                    audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    audio.writeframes(b"\0\0" * 24000)
                return b"{}"
            if argv[0] == "ffmpeg":
                Path(argv[-1]).write_bytes(b"synthetic")
                return b""
            return b'{"format":{"duration":1},"streams":[{"codec_name":"opus"}]}'

        with patch("tg_summary_bot.speech_synthesizer.run_process", fake_process):
            dp = await case.dispatcher()
            try:
                for voice_answer, upload_error in ((False, False), (True, False), (False, True)):
                    before = len(self.backend.records)
                    message = FakeMessage("/ask voice private question" if voice_answer else "/voice private speech")
                    message.upload_error = upload_error
                    if upload_error:
                        with self.assertLogs(level="WARNING"):
                            await handler(dp, "voice_command")(message)
                    else:
                        await handler(dp, "ask_command" if voice_answer else "voice_command")(message)
                    records = self.backend.records[before:]
                    roots = [record for record in records if record.parent_span_id is None]
                    self.assertEqual(len(roots), 1)
                    root = roots[0]
                    self.assertTrue(all(record.trace_id == root.trace_id for record in records))
                    self.assertEqual(bool(root.error_info), upload_error)
                    names = {record.name for record in records}
                    self.assertTrue({"speech.gpu_wait", "speech.synthesize", "speech.encode",
                                     "speech.validate", "speech.upload"} <= names)
                    if voice_answer:
                        self.assertIn("question.text_delivery", names)
                        self.assertIn("llm.ollama.fake", names)
                    self.assertNotIn("private", json.dumps([asdict(record) for record in records], default=str))
            finally:
                await dp.emit_shutdown()
