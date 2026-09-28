import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from tg_summary_bot.bot import (
    generated_context_note,
    resolve_image_for_command,
    resolve_video_for_command,
    try_save_generated_context,
)
from tg_summary_bot.observability import OperationOutcome, log_operation_outcome


class FakeStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.saved: list[dict[str, object]] = []

    async def save_message(self, **kwargs: object) -> None:
        if self.error:
            raise self.error
        self.saved.append(kwargs)


def fake_message() -> SimpleNamespace:
    return SimpleNamespace(
        message_id=20,
        chat=SimpleNamespace(id=10, type="group"),
        from_user=SimpleNamespace(id=5, full_name="User", username="user"),
        sender_chat=None,
        date=datetime.now(timezone.utc),
        reply_to_message=None,
    )


class GeneratedContextPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_success_only_after_storage_write(self) -> None:
        store = FakeStore()
        persisted = await try_save_generated_context(
            SimpleNamespace(max_message_chars=1000),  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            fake_message(),  # type: ignore[arg-type]
            "generated result",
        )
        self.assertTrue(persisted)
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(generated_context_note(persisted), "Saved for summaries.")

    async def test_storage_failure_becomes_a_user_safe_warning(self) -> None:
        store = FakeStore(RuntimeError("private database detail"))
        with self.assertLogs(level="ERROR"):
            persisted = await try_save_generated_context(
                SimpleNamespace(max_message_chars=1000),  # type: ignore[arg-type]
                store,  # type: ignore[arg-type]
                fake_message(),  # type: ignore[arg-type]
                "generated result",
            )
        self.assertFalse(persisted)
        self.assertNotIn("private database detail", generated_context_note(persisted))


class OperationOutcomeLoggingTests(unittest.TestCase):
    def test_logs_structured_metadata_without_user_content(self) -> None:
        with self.assertLogs("tg_summary_bot.operations", level="INFO") as captured:
            log_operation_outcome(
                OperationOutcome(
                    "video",
                    "partial",
                    "context_persistence_failed",
                    persisted=False,
                    cache_hit=True,
                    response_message_id=30,
                ),
                chat_id=10,
                message_id=20,
                invocation="routed",
                route_action="video",
                route_reason="ok",
                elapsed_seconds=0.125,
                provider="ollama",
                model="router-model",
            )
        event = json.loads(captured.output[0].split(":", 2)[-1])
        self.assertEqual(event["event"], "operation_outcome")
        self.assertEqual(event["status"], "partial")
        self.assertEqual(event["elapsed_ms"], 125.0)
        serialized = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("query", serialized)
        self.assertNotIn("transcript", serialized)


class MediaResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_image_follows_nonmatching_reply_without_latest_fallback(self) -> None:
        current_image = object()
        reply = SimpleNamespace(message_id=9)
        message = SimpleNamespace(reply_to_message=reply, chat=SimpleNamespace(id=10))
        store = SimpleNamespace(
            get_image_by_message_id=self._none,
            get_latest_image=self._unexpected,
        )
        with patch(
            "tg_summary_bot.bot.image_from_message",
            side_effect=lambda value: current_image if value is message else None,
        ):
            result = await resolve_image_for_command(store, message)  # type: ignore[arg-type]
        self.assertIs(result, current_image)

    async def test_current_video_follows_nonmatching_reply_without_latest_fallback(self) -> None:
        current_video = object()
        reply = SimpleNamespace(message_id=9)
        message = SimpleNamespace(reply_to_message=reply, chat=SimpleNamespace(id=10))
        store = SimpleNamespace(
            get_video_by_message_id=self._none,
            get_latest_video=self._unexpected,
        )
        with patch(
            "tg_summary_bot.bot.video_from_message",
            side_effect=lambda value: current_video if value is message else None,
        ):
            result = await resolve_video_for_command(store, message)  # type: ignore[arg-type]
        self.assertIs(result, current_video)

    async def _none(self, *_: object) -> None:
        return None

    async def _unexpected(self, *_: object) -> None:
        self.fail("latest media must not be used when a reply exists")
