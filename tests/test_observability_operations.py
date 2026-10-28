import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from tg_summary_bot.observability import (
    OperationOutcome, configure_opik_tracing, opik_operation, opik_stage,
)


class OperationTracingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.errors = []
        self.options = []
        self.spans = []
        def track(**options):
            self.options.append(options)
            def decorate(func):
                async def wrapped(*args, **kwargs):
                    try:
                        return await func(*args, **kwargs)
                    except Exception as exc:
                        self.errors.append(str(exc))
                        raise
                return wrapped
            return decorate
        @contextmanager
        def span(**kwargs):
            value = SimpleNamespace(metadata=None, error_info=None)
            self.spans.append((kwargs, value))
            yield value
        self.sdk = SimpleNamespace(track=track, start_as_current_span=span)
        configure_opik_tracing(enabled=True, project_name="test-project")
        self.addCleanup(configure_opik_tracing, enabled=False, project_name="")

    async def test_handled_failure_and_partial_are_traced_but_not_raised_to_handler(self):
        with patch.dict("sys.modules", {"opik": self.sdk}):
            for status in ("failed", "partial", "succeeded", "rejected"):
                outcome = OperationOutcome("voice", status, "safe_failure_code")
                @opik_operation(name="telegram.voice")
                async def operation():
                    return outcome
                self.assertIs(await operation(), outcome)
        self.assertEqual(self.errors, ["safe_failure_code", "safe_failure_code"])
        self.assertTrue(all(not options["capture_input"] and not options["capture_output"] for options in self.options))
        self.assertTrue(all(options["create_duplicate_root_span"] for options in self.options))

    async def test_stage_keeps_metadata_and_masks_raw_failure_content(self):
        with patch.dict("sys.modules", {"opik": self.sdk}):
            with self.assertRaisesRegex(RuntimeError, "private speech text"):
                with opik_stage("speech.upload", {"voice_id": "cartman", "audio_duration_s": 1.2}):
                    raise RuntimeError("private speech text")
        options, span = self.spans[0]
        self.assertNotIn("input", options)
        self.assertNotIn("output", options)
        self.assertEqual(span.metadata["voice_id"], "cartman")
        self.assertEqual(span.metadata["stage_status"], "failed")
        self.assertNotIn("private speech text", json.dumps(span.error_info))
        self.assertEqual(span.error_info["message"], "speech.upload_failed")

    async def test_disabled_tracing_preserves_partial_without_sdk(self):
        configure_opik_tracing(enabled=False, project_name="")
        @opik_operation(name="telegram.voice")
        async def operation():
            return OperationOutcome("voice", "partial", "safe_code")
        with patch.dict("sys.modules", {"opik": None}):
            self.assertEqual((await operation()).status, "partial")
