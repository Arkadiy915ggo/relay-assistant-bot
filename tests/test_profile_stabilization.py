import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_summary_bot.memory import ChatMemory, ProfileExtractionError, _render_messages
from tg_summary_bot.storage import MessageStore


NOW = datetime.now(timezone.utc)


def settings(**changes):
    values = dict(memory_recent_period="24h", memory_chunk_chars=18000, chunk_chars=7000,
                  memory_max_blocks=20, memory_search_limit=8, ollama_num_ctx=32768,
                  profile_batch_chars=512, profile_batch_messages=3, profile_timeout_seconds=1)
    values.update(changes)
    return SimpleNamespace(**values)


class ProfileBatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = MessageStore(Path(self.temporary.name) / "db.sqlite3")
        await self.store.init()
        self.llm = SimpleNamespace(complete=AsyncMock(return_value='{"participant_facts":[]}'), unload=AsyncMock())
        self.memory = ChatMemory(self.store, self.llm, settings())

    async def save(self, message_id, text="Короткое сообщение", created_at=None):
        await self.store.save_message(chat_id=1, message_id=message_id, chat_type="group", sender_id=7,
                                      sender_name="Имя участника", text=text, created_at=created_at or NOW - timedelta(minutes=1),
                                      reply_to_message_id=None, origin="incoming", kind="text")

    async def test_same_timestamp_batches_count_full_rendered_input_and_resume_without_loss(self):
        for message_id in range(1, 13):
            await self.save(message_id)
        seen = []
        for _ in range(12):
            previous = await self.store.get_profile_cursor(1)
            await self.memory.ensure_recent_profiles_current(1, now=NOW)
            current = await self.store.get_profile_cursor(1)
            if previous == current:
                break
            prompt = self.llm.complete.call_args.kwargs["user"]
            body = prompt.split("Сообщения:\n", 1)[1]
            self.assertLessEqual(len(body), 512)
            self.assertLessEqual(len(body.splitlines()), 3)
            seen.extend(int(line.split("#", 1)[1].split()[0]) for line in body.splitlines())
            # Restart between batches to exercise the durable keyset cursor.
            self.memory = ChatMemory(MessageStore(self.store.database_path), self.llm, settings())
        self.assertEqual(seen, list(range(1, 13)))
        self.assertGreater(self.llm.complete.await_count, 1)

    async def test_single_oversized_message_is_clipped_only_for_model_input(self):
        await self.save(1, "Исходный текст " * 2000)
        chunk = await self.store.get_profile_message_chunk(chat_id=1, after=(NOW - timedelta(hours=1)).isoformat(),
                                                         before=NOW, limit_chars=512)
        self.assertLessEqual(len(_render_messages(chunk)), 512)
        self.assertTrue(chunk[0].text.endswith("…"))
        raw = await self.store.get_messages_since(chat_id=1, since=NOW - timedelta(hours=1), limit_chars=100000)
        self.assertGreater(len(raw[0].text), 20000)
        self.assertEqual(chunk[0].message_id, raw[0].message_id)

    async def test_invalid_json_and_schema_do_not_advance_cursor_or_save_facts(self):
        await self.save(1)
        for response in ('{"participant_facts":[{"participant_key":', '{}', '{"participant_facts":true}', '{"participant_facts":[null]}'):
            self.llm.complete.return_value = response
            with self.assertRaisesRegex(ProfileExtractionError, "profile_invalid_json"):
                await self.memory.ensure_recent_profiles_current(1, now=NOW)
            self.assertIsNone(await self.store.get_profile_cursor(1))
            self.assertEqual(await self.store.count_participant_facts(1), 0)

    async def test_timeout_cancels_request_unloads_before_unlock_and_keeps_cursor(self):
        await self.save(1)
        cancelled = asyncio.Event()
        lock = asyncio.Lock()
        async def complete(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def unload():
            self.assertTrue(cancelled.is_set())
            self.assertTrue(lock.locked())
        self.llm.complete.side_effect = complete
        self.llm.unload.side_effect = unload
        self.memory.profile_timeout_seconds = 0.01
        with self.assertRaisesRegex(ProfileExtractionError, "profile_timeout"):
            async with lock:
                try:
                    await self.memory.ensure_recent_profiles_current(1, now=NOW)
                finally:
                    await self.memory.unload_profiles()
        self.assertFalse(lock.locked())
        self.assertIsNone(await self.store.get_profile_cursor(1))
        self.llm.unload.assert_awaited_once()

    async def test_separate_profile_client_and_source_backed_top_fact(self):
        await self.save(1, "Я отвечаю за проект и готовлю документацию.")
        profile = SimpleNamespace(complete=AsyncMock(return_value=json.dumps({"participant_facts": [{
            "participant_key": "id:7", "fact_type": "responsibility", "fact": "Отвечает за проект и документацию.",
            "source_message_ids": [1], "confidence": "high",
        }]})), unload=AsyncMock())
        memory = ChatMemory(self.store, self.llm, settings(), profile_llm=profile)
        self.assertEqual(await memory.ensure_recent_profiles_current(1, now=NOW), 1)
        await memory.unload_profiles()
        self.llm.complete.assert_not_called()
        self.llm.unload.assert_not_called()
        profile.unload.assert_awaited_once()
        facts = await self.store.get_participant_facts(chat_id=1, limit=10)
        self.assertEqual(json.loads(facts[0].source_message_ids), [1])

    async def test_rejected_first_candidate_does_not_hide_valid_profile_fact(self):
        rejected = [
            {},
            {"fact": "Коротко"},
            {"source_message_ids": [999]},
            {"confidence": "low"},
            {"participant_key": "id:999"},
        ]
        for message_id, changes in enumerate(rejected, start=1):
            with self.subTest(changes=changes):
                await self.save(message_id, f"Я отвечаю за проект {message_id} и документацию.")
                valid = {"participant_key": "id:7", "fact_type": "responsibility",
                         "fact": f"Отвечает за проект {message_id} и документацию.",
                         "source_message_ids": [message_id], "confidence": "high"}
                first = {**valid, **changes} if changes else {}
                self.llm.complete.return_value = json.dumps({"participant_facts": [first, valid]})
                self.assertEqual(await self.memory.ensure_recent_profiles_current(1, now=NOW), 1)
                self.assertEqual((await self.store.get_profile_cursor(1)).message_id, message_id)
                self.assertEqual(await self.store.count_participant_facts(1), message_id)
                calls = self.llm.complete.await_count
                self.assertEqual(await self.memory.ensure_recent_profiles_current(1, now=NOW), 0)
                self.assertEqual(self.llm.complete.await_count, calls)

    async def test_profile_refresh_saves_only_one_valid_fact(self):
        await self.save(1, "Я отвечаю за проект и документацию.")
        facts = [{"participant_key": "id:7", "fact_type": "responsibility", "fact": text,
                  "source_message_ids": [1], "confidence": "high"}
                 for text in ("Отвечает за проект.", "Готовит документацию.")]
        self.llm.complete.return_value = json.dumps({"participant_facts": [{}, *facts]})
        self.assertEqual(await self.memory.ensure_recent_profiles_current(1, now=NOW), 1)
        saved = await self.store.get_participant_facts(chat_id=1, limit=10)
        self.assertEqual([fact.fact_text for fact in saved], [facts[0]["fact"]])

    async def test_memory_fact_persistence_keeps_multiple_valid_facts(self):
        await self.save(1, "Я отвечаю за проект и документацию.")
        messages = await self.store.get_messages_since(
            chat_id=1, since=NOW - timedelta(hours=1), limit_chars=2000,
        )
        facts = [{"participant_key": "id:7", "fact_type": "responsibility", "fact": text,
                  "source_message_ids": [1], "confidence": "high"}
                 for text in ("Отвечает за проект.", "Готовит документацию.")]
        stats = await self.memory._save_participant_facts(1, {"participant_facts": facts}, messages)
        self.assertEqual(stats["saved"], 2)
        self.assertEqual(await self.store.count_participant_facts(1), 2)

    async def test_migration_preserves_legacy_timestamp_checkpoint(self):
        path = Path(self.temporary.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE chat_profile_state(chat_id INTEGER PRIMARY KEY, processed_until TEXT NOT NULL)")
            db.execute("INSERT INTO chat_profile_state VALUES(?,?)", (1, NOW.isoformat()))
            db.commit()
        store = MessageStore(path)
        await store.init()
        await store.init()
        cursor = await store.get_profile_cursor(1)
        self.assertEqual(cursor.created_at, NOW.isoformat())
        self.assertEqual(cursor.message_id, 9223372036854775807)
