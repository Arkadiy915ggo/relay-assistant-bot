import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

from tg_summary_bot.bot import spin_casino
from tg_summary_bot.casino import CASINO_RULES_VERSION, CASINO_STAKE, bot_request_casino_trigger, slot_result
from tg_summary_bot.storage import MessageStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Independently maintained Telegram community-map expectations, indexed by Dice.value - 1.
EXPECTED = (
    "triple", "pair", "pair", "pair", "pair", "pair", "none", "none",
    "pair", "none", "pair", "none", "pair", "none", "none", "pair",
    "pair", "pair", "none", "none", "pair", "triple", "pair", "pair",
    "none", "pair", "pair", "none", "none", "pair", "none", "pair",
    "pair", "none", "pair", "none", "none", "pair", "pair", "none",
    "pair", "pair", "triple", "pair", "none", "none", "pair", "pair",
    "pair", "none", "none", "pair", "none", "pair", "none", "pair",
    "none", "none", "pair", "pair", "pair", "pair", "pair", "jackpot",
)
PAYOUTS = {"none": 0, "pair": 5, "triple": 50, "jackpot": 250}


class CasinoDomainTests(unittest.TestCase):
    def test_all_values_match_independent_contract_and_rtp(self) -> None:
        actual = [slot_result(value) for value in range(1, 65)]
        self.assertEqual([result.category for result in actual], list(EXPECTED))
        self.assertEqual([result.payout for result in actual], [PAYOUTS[item] for item in EXPECTED])
        self.assertEqual({item: EXPECTED.count(item) for item in PAYOUTS}, {
            "none": 24, "pair": 36, "triple": 3, "jackpot": 1,
        })
        self.assertEqual(sum(result.payout for result in actual) / 64 / CASINO_STAKE, 0.90625)
        for value in (1, 22, 43, 64):
            self.assertEqual(slot_result(value).payout, 250 if value == 64 else 50)

    def test_invalid_values_are_rejected(self) -> None:
        for value in (True, False, 1.0, "1", 0, 65, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    slot_result(value)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            slot_result(1, rules_version="unknown")


class CasinoStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "casino.sqlite3"
        self.store = MessageStore(self.path)
        self._fund_source = 100
        await self.store.init()

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def fund(self, *, key: str = "id:1", name: str = "Alice", amount: int = 10) -> None:
        self._fund_source += 1
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO chat_point_balances VALUES (1, ?, ?, ?, ?)",
                (key, name, amount, NOW.isoformat()),
            )
            await db.execute(
                """INSERT INTO chat_point_ledger
                (chat_id, participant_key, participant_name, delta, reason, source_message_id, created_at)
                VALUES (1, ?, ?, ?, 'best_joke', ?, ?)""",
                (key, name, amount, self._fund_source, NOW.isoformat()),
            )
            await db.commit()

    async def ledger_sum(self, key: str = "id:1") -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT COALESCE(SUM(delta), 0) FROM chat_point_ledger WHERE chat_id = 1 AND participant_key = ?",
                    (key,),
                )
            ).fetchone()
        return int(row[0])

    async def test_schema_migration_is_idempotent_and_keeps_jokes(self) -> None:
        await self.fund()
        await self.store.init()
        async with aiosqlite.connect(self.path) as db:
            ledger = await (await db.execute("SELECT reason, casino_spin_id FROM chat_point_ledger")).fetchone()
            columns = await (await db.execute("PRAGMA table_info(chat_point_ledger)")).fetchall()
        self.assertEqual(tuple(ledger), ("best_joke", None))
        self.assertIn("casino_spin_id", {row[1] for row in columns})

    async def test_trigger_migration_preserves_feature04_spin(self) -> None:
        legacy_path = Path(self.directory.name) / "feature04.sqlite3"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            """CREATE TABLE chat_casino_spins (
                spin_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                request_message_id INTEGER NOT NULL CHECK(request_message_id > 0),
                participant_key TEXT NOT NULL, participant_name TEXT NOT NULL,
                rules_version TEXT NOT NULL, stake INTEGER NOT NULL CHECK(stake = 10),
                dice_message_id INTEGER, dice_value INTEGER, category TEXT, payout INTEGER,
                status TEXT NOT NULL, terminal_balance INTEGER, terminal_reason TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT, refunded_at TEXT,
                UNIQUE(chat_id, request_message_id))"""
        )
        connection.execute(
            """INSERT INTO chat_casino_spins
               (spin_id, chat_id, request_message_id, participant_key, participant_name, rules_version,
                stake, status, created_at, updated_at) VALUES (7, 1, 42, 'id:1', 'Alice', ?, 10,
                'pending', ?, ?)""",
            (CASINO_RULES_VERSION, NOW.isoformat(), NOW.isoformat()),
        )
        connection.commit()
        connection.close()
        migrated = MessageStore(legacy_path)
        await migrated.init()
        await migrated.init()
        async with aiosqlite.connect(legacy_path) as db:
            row = await (await db.execute(
                "SELECT spin_id, trigger_kind, trigger_key, request_message_id FROM chat_casino_spins"
            )).fetchone()
        self.assertEqual(tuple(row), (7, "user_request", "user-message:42", 42))

    async def test_reserve_duplicate_identity_and_insufficient(self) -> None:
        self.assertEqual((await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice"
        )).status, "insufficient_balance")
        await self.fund()
        created = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice", created_at=NOW
        )
        self.assertEqual(created.status, "created")
        self.assertEqual(created.spin.terminal_balance if created.spin else None, None)
        self.assertEqual((await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Renamed"
        )).status, "existing_pending")
        self.assertEqual((await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:2", participant_name="Bob"
        )).status, "identity_conflict")
        self.assertEqual(await self.ledger_sum(), 0)

    async def test_request_message_trigger_collision_is_an_identity_conflict(self) -> None:
        await self.fund()
        await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=8, participant_key="id:1", participant_name="Alice"
        )
        collision = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=8, participant_key="id:99", participant_name="Bot",
            trigger=bot_request_casino_trigger(8),
        )
        self.assertEqual(collision.status, "identity_conflict")

    async def test_concurrent_reserves_do_not_overdraw_across_store_instances(self) -> None:
        await self.fund()
        other = MessageStore(self.path)
        first, second = await asyncio.gather(
            self.store.reserve_casino_spin(chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice"),
            other.reserve_casino_spin(chat_id=1, request_message_id=2, participant_key="id:1", participant_name="Alice"),
        )
        self.assertEqual({first.status, second.status}, {"created", "insufficient_balance"})
        balance = await self.store.get_point_balance(chat_id=1, participant_key="id:1")
        self.assertEqual(balance.balance if balance else None, 0)

    async def test_settlement_classes_and_refund_race_are_exactly_once(self) -> None:
        for request_id, value, payout in ((1, 2, 5), (2, 1, 50), (3, 64, 250), (4, 7, 0)):
            await self.fund(amount=10, key=f"id:{request_id}", name=f"User {request_id}")
            reserved = await self.store.reserve_casino_spin(
                chat_id=1, request_message_id=request_id, participant_key=f"id:{request_id}", participant_name=f"User {request_id}"
            )
            settled = await self.store.settle_casino_spin(
                spin_id=reserved.spin.spin_id, dice_message_id=100 + request_id, dice_value=value
            )
            self.assertEqual(settled.status, "completed")
            self.assertEqual(settled.spin.payout if settled.spin else None, payout)
            self.assertEqual((await self.store.settle_casino_spin(
                spin_id=reserved.spin.spin_id, dice_message_id=999, dice_value=64
            )).status, "already_completed")

        await self.fund(amount=10, key="id:99", name="Race")
        reserved = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=99, participant_key="id:99", participant_name="Race"
        )
        settled, refunded = await asyncio.gather(
            self.store.settle_casino_spin(spin_id=reserved.spin.spin_id, dice_message_id=199, dice_value=1),
            self.store.refund_casino_spin(spin_id=reserved.spin.spin_id, reason="test"),
        )
        self.assertIn(settled.status, {"completed", "refunded_conflict"})
        self.assertIn(refunded.status, {"refunded", "completed_conflict"})
        self.assertNotEqual(settled.status == "completed", refunded.status == "refunded")

    async def test_rollback_and_recovery_cutoff(self) -> None:
        await self.fund()
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""CREATE TRIGGER fail_bet BEFORE INSERT ON chat_point_ledger
            WHEN NEW.reason = 'casino_bet' BEGIN SELECT RAISE(ABORT, 'fail'); END""")
            await db.commit()
        with self.assertRaises(aiosqlite.IntegrityError):
            await self.store.reserve_casino_spin(
                chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice"
            )
        self.assertEqual(await self.ledger_sum(), 10)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DROP TRIGGER fail_bet")
            await db.commit()
        reserved = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=2, participant_key="id:1", participant_name="Alice", created_at=NOW
        )
        recovered = await self.store.refund_pending_casino_spins(created_before=NOW + timedelta(seconds=1))
        self.assertEqual(recovered.spin_ids, (reserved.spin.spin_id,))
        self.assertEqual((await self.store.refund_pending_casino_spins(created_before=NOW + timedelta(seconds=1))).spin_ids, ())
        self.assertEqual(await self.ledger_sum(), 10)


class CasinoBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "casino.sqlite3"
        self.store = MessageStore(self.path)
        await self.store.init()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO chat_point_balances VALUES (1, 'id:1', 'Alice', 10, ?)", (NOW.isoformat(),)
            )
            await db.execute(
                """INSERT INTO chat_point_ledger
                (chat_id, participant_key, participant_name, delta, reason, source_message_id, created_at)
                VALUES (1, 'id:1', 'Alice', 10, 'best_joke', 1, ?)""", (NOW.isoformat(),)
            )
            await db.commit()
        self.settings = SimpleNamespace(allowed_chat_ids=set())

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    def message(self, *, dice_value: int = 64, dice_error: Exception | None = None) -> SimpleNamespace:
        chat = SimpleNamespace(id=1, type="group")
        message = SimpleNamespace(
            chat=chat,
            message_id=10,
            text="/casino",
            from_user=SimpleNamespace(id=1, full_name="Alice", username="alice", is_bot=False),
            sender_chat=None,
            date=NOW,
            replies=[],
            dice_calls=[],
        )

        async def reply(text: str, **_: object) -> SimpleNamespace:
            response = SimpleNamespace(message_id=30 + len(message.replies), chat=chat)
            message.replies.append((text, response))
            return response

        async def answer(text: str, **_: object) -> SimpleNamespace:
            response = SimpleNamespace(message_id=40 + len(message.replies), chat=chat)
            message.replies.append((text, response))
            return response

        async def reply_dice(**kwargs: object) -> SimpleNamespace:
            message.dice_calls.append(kwargs)
            if dice_error:
                raise dice_error
            return SimpleNamespace(
                chat=chat,
                message_id=20,
                dice=SimpleNamespace(emoji="🎰", value=dice_value),
            )

        message.reply = reply
        message.answer = answer
        message.reply_dice = reply_dice
        return message

    async def test_valid_spin_sends_one_dice_and_settles_from_returned_value(self) -> None:
        await self.store.save_pinned_leaderboard(chat_id=1, message_id=88)
        message = self.message(dice_value=64)
        bot = SimpleNamespace(
            edit_message_text=AsyncMock(),
            pin_chat_message=AsyncMock(),
        )
        outcome = await spin_casino(self.settings, self.store, bot, message)
        self.assertEqual(outcome.reason, "completed")
        self.assertEqual(message.dice_calls, [{"emoji": "🎰"}])
        self.assertIn("выплата: 250", message.replies[0][0])
        balance = await self.store.get_point_balance(chat_id=1, participant_key="id:1")
        self.assertEqual(balance.balance if balance else None, 250)
        bot.edit_message_text.assert_awaited_once()
        self.assertEqual(bot.edit_message_text.await_args.kwargs["message_id"], 88)
        self.assertIn("Alice - 250", bot.edit_message_text.await_args.kwargs["text"])

    async def test_insufficient_balance_never_calls_telegram(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE chat_point_balances SET balance = 0")
            await db.commit()
        message = self.message()
        outcome = await spin_casino(self.settings, self.store, SimpleNamespace(), message)
        self.assertEqual(outcome.reason, "insufficient_balance")
        self.assertEqual(message.dice_calls, [])

    async def test_disabled_chat_never_reserves_or_sends_dice(self) -> None:
        message = self.message()
        settings = SimpleNamespace(allowed_chat_ids=set(), casino_disabled_chat_ids={1})
        outcome = await spin_casino(settings, self.store, SimpleNamespace(), message)
        self.assertEqual(outcome.reason, "feature_disabled")
        self.assertEqual(message.dice_calls, [])
        balance = await self.store.get_point_balance(chat_id=1, participant_key="id:1")
        self.assertEqual(balance.balance if balance else None, 10)

    async def test_unknown_send_outcome_refunds_once(self) -> None:
        message = self.message(dice_error=TimeoutError())
        bot = SimpleNamespace(edit_message_text=AsyncMock(), pin_chat_message=AsyncMock())
        with self.assertLogs(level="ERROR"):
            outcome = await spin_casino(self.settings, self.store, bot, message)
        self.assertEqual(outcome.reason, "telegram_outcome_unknown")
        balance = await self.store.get_point_balance(chat_id=1, participant_key="id:1")
        self.assertEqual(balance.balance if balance else None, 10)
        self.assertTrue(any("возвращена" in text for text, _ in message.replies))
