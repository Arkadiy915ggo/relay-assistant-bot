import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from tg_summary_bot.joke_awards import JokeSelector, parse_joke_selection
from tg_summary_bot.storage import (
    ELIGIBLE_MESSAGE_PROVENANCE,
    MessageStore,
    SnapshotBoundary,
    StoredMessage,
    is_eligible_message_provenance,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.unloaded = False

    async def complete(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.responses.pop(0)

    async def unload(self) -> None:
        self.unloaded = True


class JokeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "messages.sqlite3"
        self.store = MessageStore(self.path)
        await self.store.init()

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def _save(
        self,
        message_id: int,
        *,
        chat_id: int = 1,
        created_at: datetime = NOW,
        text: str = "joke",
        origin: str = "incoming",
        kind: str = "text",
        sender_id: int | None = 10,
        sender_name: str = "Alice",
    ) -> None:
        await self.store.save_message(
            chat_id=chat_id,
            message_id=message_id,
            chat_type="group",
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            created_at=created_at,
            reply_to_message_id=None,
            origin=origin,
            kind=kind,
        )

    async def test_new_and_legacy_migrations_are_idempotent_and_legacy_is_ineligible(self) -> None:
        await self.store.init()
        await self._save(1, origin="legacy", kind="legacy_unclassified")
        boundary = await self.store.get_eligible_snapshot_boundary(chat_id=1, since=NOW - timedelta(days=1))
        self.assertIsNone(boundary)

        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """CREATE TABLE messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, chat_type TEXT NOT NULL,
                sender_id INTEGER, sender_name TEXT NOT NULL, text TEXT NOT NULL,
                created_at TEXT NOT NULL, reply_to_message_id INTEGER,
                PRIMARY KEY(chat_id, message_id))"""
        )
        connection.execute(
            "INSERT INTO messages VALUES (1, 9, 'group', 1, 'Old', 'old', ?, NULL)",
            (NOW.isoformat(),),
        )
        connection.commit()
        connection.close()
        legacy_store = MessageStore(legacy_path)
        await legacy_store.init()
        await legacy_store.init()
        async with aiosqlite.connect(legacy_path) as db:
            row = await (await db.execute("SELECT origin, kind FROM messages WHERE message_id = 9")).fetchone()
        self.assertEqual(tuple(row), ("legacy", "legacy_unclassified"))
        self.assertIsNone(
            await legacy_store.get_eligible_snapshot_boundary(chat_id=1, since=NOW - timedelta(days=1))
        )

    def test_closed_allowlist_has_exactly_four_pairs(self) -> None:
        self.assertEqual(
            ELIGIBLE_MESSAGE_PROVENANCE,
            {
                ("incoming", "text"),
                ("incoming", "caption"),
                ("incoming", "voice_transcript"),
                ("assistant", "assistant_answer"),
            },
        )
        self.assertFalse(is_eligible_message_provenance("generated", "wiki_result"))
        self.assertFalse(is_eligible_message_provenance("future", "text"))

    async def test_snapshot_keyset_boundary_same_timestamp_and_oversized_progress(self) -> None:
        for message_id in range(1, 5):
            await self._save(message_id, text="x" * (50 if message_id == 1 else 3))
        boundary = await self.store.get_eligible_snapshot_boundary(chat_id=1, since=NOW - timedelta(days=1))
        self.assertEqual(boundary, SnapshotBoundary(NOW.isoformat(), 4))
        await self._save(5, created_at=NOW + timedelta(seconds=1))
        cursor = None
        seen: list[int] = []
        while True:
            page = await self.store.get_eligible_snapshot_page(
                chat_id=1,
                since=NOW - timedelta(days=1),
                boundary=boundary,
                after=cursor,
                row_limit=2,
                char_budget=10,
            )
            if not page.messages:
                break
            seen.extend(message.message_id for message in page.messages)
            cursor = page.next_cursor
        self.assertEqual(seen, [1, 2, 3, 4])

    async def test_atomic_award_duplicate_cross_chat_and_rollback(self) -> None:
        await self._save(1)
        first, duplicate = await asyncio.gather(
            self.store.award_unique_joke(chat_id=1, source_message_id=1),
            self.store.award_unique_joke(chat_id=1, source_message_id=1),
        )
        self.assertEqual({first.status, duplicate.status}, {"awarded", "already_awarded"})
        for result in (first, duplicate):
            self.assertEqual(result.source_message_id, 1)
            self.assertEqual(result.participant_name, "Alice")
            self.assertEqual(result.source_text, "joke")
        balance = await self.store.get_point_balance(chat_id=1, participant_key="id:10")
        self.assertEqual(balance.balance if balance else None, 10)
        self.assertEqual((await self.store.award_unique_joke(chat_id=2, source_message_id=1)).status, "not_found")
        self.assertEqual((await self.store.award_unique_joke(chat_id=1, source_message_id=99)).status, "not_found")
        await self._save(2, origin="generated", kind="image_recognition")
        self.assertEqual((await self.store.award_unique_joke(chat_id=1, source_message_id=2)).status, "ineligible")

        await self._save(3, sender_id=11, sender_name="Bob")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """CREATE TRIGGER fail_balance BEFORE INSERT ON chat_point_balances
                WHEN NEW.participant_key = 'id:11' BEGIN SELECT RAISE(ABORT, 'fail'); END"""
            )
            await db.commit()
        with self.assertRaises(aiosqlite.DatabaseError):
            await self.store.award_unique_joke(chat_id=1, source_message_id=3)
        async with aiosqlite.connect(self.path) as db:
            count = await (await db.execute("SELECT COUNT(*) FROM chat_point_ledger WHERE source_message_id = 3")).fetchone()
        self.assertEqual(count[0], 0)

    async def test_unknown_ledger_integrity_error_is_not_duplicate_success(self) -> None:
        await self._save(4)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """CREATE TRIGGER reject_award BEFORE INSERT ON chat_point_ledger
                WHEN NEW.source_message_id = 4 BEGIN SELECT RAISE(ABORT, 'unexpected'); END"""
            )
            await db.commit()

        with self.assertRaises(aiosqlite.IntegrityError):
            await self.store.award_unique_joke(chat_id=1, source_message_id=4)
        self.assertIsNone(await self.store.get_point_balance(chat_id=1, participant_key="id:10"))
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM chat_point_ledger WHERE source_message_id = 4"
                )
            ).fetchone()
        self.assertEqual(row[0], 0)

    async def test_top_is_chat_scoped_and_deterministic(self) -> None:
        for message_id, name, sender in [(1, "zoe", 1), (2, "Anna", 2), (3, "anna", 3), (4, "Other", 4)]:
            await self._save(message_id, sender_name=name, sender_id=sender, chat_id=1 if message_id < 4 else 2)
            await self.store.award_unique_joke(chat_id=1 if message_id < 4 else 2, source_message_id=message_id)
        top = await self.store.get_top_point_balances(chat_id=1)
        self.assertEqual([item.participant_name for item in top], ["Anna", "anna", "zoe"])
        self.assertEqual(len(await self.store.get_top_point_balances(chat_id=2)), 1)

    async def test_pinned_leaderboard_state_is_chat_scoped_and_replaceable(self) -> None:
        self.assertIsNone(await self.store.get_pinned_leaderboard(chat_id=1))
        await self.store.save_pinned_leaderboard(chat_id=1, message_id=100)
        await self.store.save_pinned_leaderboard(chat_id=1, message_id=101)
        pinned = await self.store.get_pinned_leaderboard(chat_id=1)
        self.assertEqual(pinned.message_id if pinned else None, 101)
        self.assertIsNone(await self.store.get_pinned_leaderboard(chat_id=2))
        await self.store.delete_pinned_leaderboard(chat_id=1)
        self.assertIsNone(await self.store.get_pinned_leaderboard(chat_id=1))


class JokeSelectorTests(unittest.IsolatedAsyncioTestCase):
    def test_strict_json_rejects_bool_and_out_of_prompt_and_repairs_nothing(self) -> None:
        valid = parse_joke_selection('{"has_joke":true,"source_message_id":1,"reason":"yes"}', {1})
        self.assertTrue(valid.valid)
        for response in (
            "```json\n{\"has_joke\":false,\"source_message_id\":null,\"reason\":\"no\"}\n```",
            '{"has_joke":true,"source_message_id":true,"reason":"no"}',
            '{"has_joke":true,"source_message_id":2,"reason":"no"}',
            '{"has_joke":false,"source_message_id":1,"reason":"no"}',
            '{"has_joke":false,"source_message_id":null,"reason":"no","extra":1}',
            '{"has_joke":false,"source_message_id":null,"reason":"a","reason":"b"}',
        ):
            with self.subTest(response=response):
                self.assertFalse(parse_joke_selection(response, {1}).valid)

    async def test_page_selection_tournament_and_invalid_abort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MessageStore(Path(directory) / "messages.sqlite3")
            await store.init()
            for message_id in range(1, 5):
                await store.save_message(
                    chat_id=1,
                    message_id=message_id,
                    chat_type="group",
                    sender_id=message_id,
                    sender_name=f"User {message_id}",
                    text=f"joke {message_id}",
                    created_at=NOW + timedelta(seconds=message_id),
                    reply_to_message_id=None,
                    origin="incoming",
                    kind="text",
                )
            boundary = await store.get_eligible_snapshot_boundary(chat_id=1, since=NOW)
            llm = FakeLLM([
                '{"has_joke":true,"source_message_id":1,"reason":"page"}',
                '{"has_joke":true,"source_message_id":3,"reason":"page"}',
                '{"has_joke":true,"source_message_id":3,"reason":"final"}',
            ])
            selector = JokeSelector(llm, row_limit=2, char_budget=1000)  # type: ignore[arg-type]
            result = await selector.choose(store, chat_id=1, since=NOW, boundary=boundary)
            self.assertEqual(result.status, "selected")
            self.assertEqual(result.winner.message_id if result.winner else None, 3)
            self.assertEqual(result.tournament_rounds, 1)

            invalid = JokeSelector(FakeLLM(['not json']), row_limit=2, char_budget=1000)  # type: ignore[arg-type]
            failed = await invalid.choose(store, chat_id=1, since=NOW, boundary=boundary)
            self.assertEqual(failed.status, "invalid")

    async def test_multi_round_tournament_and_invalid_round_abort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MessageStore(Path(directory) / "messages.sqlite3")
            await store.init()
            for message_id in range(1, 9):
                await store.save_message(
                    chat_id=1,
                    message_id=message_id,
                    chat_type="group",
                    sender_id=message_id,
                    sender_name=f"User {message_id}",
                    text=f"joke {message_id}",
                    created_at=NOW + timedelta(seconds=message_id),
                    reply_to_message_id=None,
                    origin="incoming",
                    kind="text",
                )
            boundary = await store.get_eligible_snapshot_boundary(chat_id=1, since=NOW)
            llm = FakeLLM([
                '{"has_joke":true,"source_message_id":1,"reason":"page"}',
                '{"has_joke":true,"source_message_id":3,"reason":"page"}',
                '{"has_joke":true,"source_message_id":5,"reason":"page"}',
                '{"has_joke":true,"source_message_id":7,"reason":"page"}',
                '{"has_joke":true,"source_message_id":1,"reason":"round"}',
                '{"has_joke":true,"source_message_id":5,"reason":"round"}',
                '{"has_joke":true,"source_message_id":5,"reason":"final"}',
            ])
            result = await JokeSelector(llm, row_limit=2, char_budget=1000).choose(  # type: ignore[arg-type]
                store,
                chat_id=1,
                since=NOW,
                boundary=boundary,
            )
            self.assertEqual(result.winner.message_id if result.winner else None, 5)
            self.assertEqual(result.tournament_rounds, 2)

            invalid_llm = FakeLLM([
                '{"has_joke":true,"source_message_id":1,"reason":"page"}',
                '{"has_joke":true,"source_message_id":3,"reason":"page"}',
                '{"has_joke":true,"source_message_id":5,"reason":"page"}',
                '{"has_joke":true,"source_message_id":7,"reason":"page"}',
                "not json",
            ])
            invalid = await JokeSelector(invalid_llm, row_limit=2, char_budget=1000).choose(  # type: ignore[arg-type]
                store,
                chat_id=1,
                since=NOW,
                boundary=boundary,
            )
            self.assertEqual(invalid.status, "invalid")
