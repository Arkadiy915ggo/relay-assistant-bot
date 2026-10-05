import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import aiosqlite

from tg_summary_bot.bot import (
    render_best_joke_section,
    save_incoming_message,
    save_message_text,
    try_save_final_assistant_answer,
    try_save_generated_context,
)
from tg_summary_bot.storage import MessageStore


class JokeProvenanceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = MessageStore(Path(self.directory.name) / "messages.sqlite3")
        await self.store.init()
        self.settings = SimpleNamespace(max_message_chars=10_000, allowed_chat_ids=set())
        self.now = datetime.now(timezone.utc)

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    def _message(self, message_id: int, *, text: str | None = None, caption: str | None = None):
        return SimpleNamespace(
            message_id=message_id,
            chat=SimpleNamespace(id=1, type="group"),
            from_user=SimpleNamespace(id=10, full_name="User", username="user"),
            sender_chat=None,
            text=text,
            caption=caption,
            date=self.now,
            reply_to_message=None,
        )

    async def _provenance(self, message_id: int) -> tuple[str, str]:
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (
                await db.execute("SELECT origin, kind FROM messages WHERE chat_id = 1 AND message_id = ?", (message_id,))
            ).fetchone()
        return str(row[0]), str(row[1])

    async def test_incoming_and_final_assistant_provenance_is_explicit(self) -> None:
        text = self._message(1, text="ordinary text")
        caption = self._message(2, caption="media caption")
        voice = self._message(3)
        answer = self._message(4, text="assistant output")
        await save_incoming_message(self.settings, self.store, text)
        await save_incoming_message(self.settings, self.store, caption)
        await save_message_text(
            self.settings,
            self.store,
            voice,
            "transcript",
            origin="incoming",
            kind="voice_transcript",
        )
        self.assertTrue(
            await try_save_final_assistant_answer(self.settings, self.store, answer, "final logical answer")
        )
        self.assertEqual(await self._provenance(1), ("incoming", "text"))
        self.assertEqual(await self._provenance(2), ("incoming", "caption"))
        self.assertEqual(await self._provenance(3), ("incoming", "voice_transcript"))
        self.assertEqual(await self._provenance(4), ("assistant", "assistant_answer"))

    async def test_generated_and_status_rows_are_not_eligible(self) -> None:
        generated = self._message(10, text="generated")
        status = self._message(11, text="Thinking...")
        self.assertTrue(await try_save_generated_context(self.settings, self.store, generated, "wiki result"))
        await save_message_text(
            self.settings,
            self.store,
            status,
            "Thinking...",
            origin="generated",
            kind="status",
        )
        self.assertEqual(await self._provenance(10), ("generated", "generated_context"))
        self.assertEqual(await self._provenance(11), ("generated", "status"))
        self.assertIsNone(
            await self.store.get_eligible_snapshot_boundary(chat_id=1, since=self.now.replace(year=2025))
        )

    def test_validated_joke_rendering_never_claims_uncommitted_points(self) -> None:
        self.assertEqual(render_best_joke_section(), "Лучшая шутка: Не нашлось.")
        self.assertIn(
            "+10 очков",
            render_best_joke_section(winner_name="Alice", winner_text="точная шутка", award_status="awarded"),
        )
        self.assertIn(
            "+0.",
            render_best_joke_section(
                winner_name="Alice", winner_text="точная шутка", award_status="already_awarded"
            ),
        )
        self.assertNotIn(
            "+10",
            render_best_joke_section(winner_name="Alice", winner_text="точная шутка", award_status="failed"),
        )
