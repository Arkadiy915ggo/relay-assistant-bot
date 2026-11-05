import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendDice, SendMessage

from tg_summary_bot.bot import create_dispatcher, run_casino_dice_lifecycle, spin_casino
from tg_summary_bot.casino import (
    CASINO_MAX_STAKE, CASINO_RULES_VERSION, CASINO_STAKE, CASINO_VARIABLE_RULES_VERSION, SQLITE_MAX_INTEGER,
    bot_request_casino_trigger, parse_casino_stake, slot_result,
)
from tg_summary_bot.storage import MessageStore
from tg_summary_bot.telegram_delivery import TelegramDeliveryMiddleware


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
    def test_custom_stake_payouts_and_odd_pair_rounding(self) -> None:
        for stake, expected in ((50, [0, 25, 250, 1250]), (55, [0, 27, 275, 1375]), (1, [0, 0, 5, 25])):
            self.assertEqual([
                slot_result(value, stake=stake, rules_version=CASINO_VARIABLE_RULES_VERSION).payout
                for value in (7, 2, 1, 64)
            ], expected)
        with self.assertRaises(ValueError):
            slot_result(64, stake=50)

    def test_stake_parser_accepts_commands_and_addressed_requests(self) -> None:
        for text, addressed, expected in (
            ("", False, 10), ("50", False, 50), ("all", False, "all"),
            ("на все очки", False, "all"), ("на весь баланс", False, "all"),
            ("Алёша крутани казик на 50 очков", True, 50),
            ("Алеша, крути себе слот на все бабки!", True, "all"),
            ("крутани казик на все очки", True, "all"),
            ("крутани казик на все деньги", True, "all"),
            ("крутани казик", True, 10), ("крути слот себе на 1 очко пожалуйста", True, 1),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_casino_stake(text, addressed=addressed), expected)
        for text in ("0", "-50", "1.5", "1,5", ".50", ",5", ".5", "50 100", "all 50", "1e3", "пятьдесят", str(CASINO_MAX_STAKE + 1)):
            with self.subTest(invalid=text), self.assertRaises(ValueError):
                parse_casino_stake(text)
        for text in ("крути казик на -50", "крути казик на все очки и на 50", "крути на 50 казик",
                     "крути казик50", "крути казик .5", "крути казик ,5", "крути на пятьдесят очков казик"):
            with self.subTest(invalid=text), self.assertRaises(ValueError):
                parse_casino_stake(text, addressed=True)

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
    async def test_pending_jackpots_reserve_capacity_across_store_instances(self) -> None:
        stake = (SQLITE_MAX_INTEGER - 10) // 25
        await self.fund(amount=stake + 10)
        first = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice", stake=stake,
        )
        other = MessageStore(self.path)
        second = await other.reserve_casino_spin(
            chat_id=1, request_message_id=2, participant_key="id:1", participant_name="Alice", stake=10,
        )
        self.assertEqual((first.status, second.status), ("created", "invalid_stake"))
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 10)
        settled = await self.store.settle_casino_spin(spin_id=first.spin.spin_id, dice_message_id=20, dice_value=64)
        self.assertEqual(settled.spin.terminal_balance, 10 + stake * 25)
        self.assertEqual(await self.ledger_sum(), 10 + stake * 25)

    async def test_multiple_admitted_jackpots_fit_and_awards_cannot_take_reserved_capacity(self) -> None:
        stake = (SQLITE_MAX_INTEGER - 250) // 25
        await self.fund(amount=stake + 10)
        spins = []
        for message_id, amount in ((1, stake), (2, 10)):
            result = await self.store.reserve_casino_spin(
                chat_id=1, request_message_id=message_id, participant_key="id:1", participant_name="Alice", stake=amount,
            )
            self.assertEqual(result.status, "created")
            spins.append(result.spin)
        await self.store.save_message(
            chat_id=1, message_id=999, chat_type="group", sender_id=1, sender_name="Alice",
            text="joke", created_at=NOW, reply_to_message_id=None, origin="incoming", kind="text",
        )
        with self.assertRaises(OverflowError):
            await self.store.award_unique_joke(chat_id=1, source_message_id=999)
        for spin in reversed(spins):
            settled = await self.store.settle_casino_spin(spin_id=spin.spin_id, dice_message_id=20 + spin.spin_id, dice_value=64)
            self.assertEqual(settled.status, "completed")
        self.assertEqual(await self.ledger_sum(), stake * 25 + 250)

    async def test_legacy_unsafe_pending_spin_cannot_write_real_or_partial_payout(self) -> None:
        await self.fund(amount=50)
        pending = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice", stake=50,
        )
        # Simulate an unsafe old reservation or an external balance mutation.
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE chat_point_balances SET balance=?", (SQLITE_MAX_INTEGER - 1,))
            await db.commit()
        result = await self.store.settle_casino_spin(spin_id=pending.spin.spin_id, dice_message_id=20, dice_value=64)
        self.assertEqual(result.status, "balance_limit")
        async with aiosqlite.connect(self.path) as db:
            self.assertEqual((await (await db.execute("SELECT typeof(balance) FROM chat_point_balances")).fetchone())[0], "integer")
            self.assertEqual((await (await db.execute("SELECT COUNT(*) FROM chat_point_ledger WHERE reason='casino_payout'")).fetchone())[0], 0)

    async def test_all_in_is_atomic_across_stores_and_duplicate_replay_keeps_original_stake(self) -> None:
        await self.fund(amount=55)
        other = MessageStore(self.path)
        results = await asyncio.gather(*(
            store.reserve_casino_spin(
                chat_id=1, request_message_id=message_id, participant_key="id:1", participant_name="Alice", stake="all",
            ) for store, message_id in ((self.store, 1), (other, 2))
        ))
        self.assertEqual({r.status for r in results}, {"created", "insufficient_balance"})
        spin = next(r.spin for r in results if r.status == "created")
        self.assertEqual(spin.stake, 55)
        self.assertEqual(await self.ledger_sum(), 0)
        result = await self.store.settle_casino_spin(spin_id=spin.spin_id, dice_message_id=20, dice_value=2)
        self.assertEqual(result.spin.payout, 27)
        repeated = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=spin.request_message_id,
            participant_key="id:1", participant_name="Alice", stake="all",
        )
        self.assertEqual((repeated.status, repeated.spin.stake), ("existing_completed", 55))
        self.assertEqual(await self.ledger_sum(), 27)

    async def test_custom_stake_settlement_and_restart_refund_use_saved_stake(self) -> None:
        await self.fund(amount=100)
        first = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice", stake=50,
        )
        self.assertEqual(first.spin.rules_version, CASINO_VARIABLE_RULES_VERSION)
        settled = await self.store.settle_casino_spin(spin_id=first.spin.spin_id, dice_message_id=20, dice_value=64)
        self.assertEqual((settled.spin.payout, settled.spin.terminal_balance), (1250, 1300))
        pending = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=2, participant_key="id:1", participant_name="Alice", stake="all",
        )
        self.assertEqual(pending.spin.stake, 1300)
        restarted = MessageStore(self.path)
        await restarted.init()
        await restarted.refund_pending_casino_spins(created_before=datetime.now(timezone.utc))
        self.assertEqual(await self.ledger_sum(), 1300)
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 1300)

    async def test_invalid_and_unaffordable_stakes_never_debit(self) -> None:
        await self.fund(amount=50)
        for stake in (0, -1, True, 1.5, CASINO_MAX_STAKE + 1):
            with self.subTest(stake=stake), self.assertRaises(ValueError):
                await self.store.reserve_casino_spin(
                    chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice", stake=stake,
                )
        result = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=1, participant_key="id:1", participant_name="Alice", stake=51,
        )
        self.assertEqual(result.status, "insufficient_balance")
        self.assertEqual(await self.ledger_sum(), 50)
        self.assertEqual((await self.store.reserve_casino_spin(
            chat_id=2, request_message_id=1, participant_key="id:1", participant_name="Alice", stake="all",
        )).status, "insufficient_balance")

    async def test_fixed_stake_migration_preserves_rows_indexes_and_sequence(self) -> None:
        path = Path(self.directory.name) / "fixed.sqlite3"
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE chat_casino_spins (
            spin_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
            request_message_id INTEGER CHECK(request_message_id IS NULL OR request_message_id > 0),
            trigger_kind TEXT NOT NULL, trigger_key TEXT NOT NULL,
            participant_key TEXT NOT NULL, participant_name TEXT NOT NULL, rules_version TEXT NOT NULL,
            stake INTEGER NOT NULL CHECK(stake = 10), dice_message_id INTEGER, dice_value INTEGER,
            category TEXT, payout INTEGER CHECK(payout IN (0, 5, 50, 250)), status TEXT NOT NULL,
            terminal_balance INTEGER, terminal_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            completed_at TEXT, refunded_at TEXT, UNIQUE(chat_id, trigger_key))""")
        db.execute("CREATE INDEX historical_casino_index ON chat_casino_spins(chat_id)")
        for spin_id, status, category, payout in ((1, "pending", None, None), (2, "completed", "pair", 5),
                                                   (3, "refunded", None, None), (99, "pending", None, None)):
            db.execute("""INSERT INTO chat_casino_spins
                (spin_id,chat_id,request_message_id,trigger_kind,trigger_key,participant_key,participant_name,
                 rules_version,stake,status,category,payout,created_at,updated_at)
                VALUES (?,1,?,'user_request',?,'id:1','Alice',?,10,?,?,?,?,?)""",
                (spin_id, spin_id, f"user-message:{spin_id}", CASINO_RULES_VERSION, status, category, payout,
                 NOW.isoformat(), NOW.isoformat()))
        db.execute("DELETE FROM chat_casino_spins WHERE spin_id=99")
        before = db.execute("SELECT * FROM chat_casino_spins ORDER BY spin_id").fetchall()
        db.commit()
        db.close()
        store = MessageStore(path)
        await store.init()
        await store.init()
        async with aiosqlite.connect(path) as migrated:
            self.assertEqual(await (await migrated.execute("SELECT * FROM chat_casino_spins ORDER BY spin_id")).fetchall(), before)
            indexes = await (await migrated.execute("PRAGMA index_list(chat_casino_spins)")).fetchall()
            self.assertIn("historical_casino_index", {row[1] for row in indexes})
            self.assertIn("uq_chat_casino_spins_request_message", {row[1] for row in indexes})
            self.assertEqual((await (await migrated.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='chat_casino_spins'"
            )).fetchone())[0], 99)

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
    async def test_addressed_all_in_uses_human_wallet_and_reports_actual_stake(self) -> None:
        message = self.message()
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard", new_callable=AsyncMock):
            outcome = await spin_casino(
                self.settings, self.store, SimpleNamespace(), message, request="Алёша крутани казик на все бабки",
            )
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.metadata["stake"], 10)
        self.assertIn("Ставка: 10; выплата: 250", message.replies[0][0])

    async def test_addressed_invalid_stake_does_not_fall_back_to_default(self) -> None:
        for request in ("Алёша крутани казик на -50 очков", "Алёша крутани казик .50", "Алёша крутани казик50"):
            message = self.message()
            outcome = await spin_casino(self.settings, self.store, SimpleNamespace(), message, request=request)
            self.assertEqual(outcome.reason, "invalid_stake")
            self.assertEqual(message.dice_calls, [])
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 10)

    async def test_custom_bot_stake_debits_only_bot_wallet(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO chat_point_balances VALUES (1, 'id:99', 'Bot', 100, ?)", (NOW.isoformat(),))
            await db.commit()
        message = self.message(dice_value=2)
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard", new_callable=AsyncMock):
            outcome = await spin_casino(
                self.settings, self.store, SimpleNamespace(), message,
                participant=("id:99", "Bot"), trigger=bot_request_casino_trigger(message.message_id),
                request="Алёша крутани себе казик на 50 очков",
            )
        self.assertEqual(outcome.metadata["stake"], 50)
        self.assertEqual(outcome.metadata["payout"], 25)
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:99")).balance, 75)
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 10)

    async def test_custom_stake_slash_command_for_human_and_bot_wallets(self) -> None:
        dispatcher = await create_dispatcher(
            self.settings, self.store, SimpleNamespace(), SimpleNamespace(), None,
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
            None, None, asyncio.Lock(), SimpleNamespace(),
        )
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == "casino_command")
        for command, expected_stake, bot_wallet in (
            ("/casino 50", 50, False), ("/casino all", "all", False),
            ("/casino bot 50", 50, True), ("/casino bot на все очки", "all", True),
        ):
            message = self.message()
            message.text = command
            with patch("tg_summary_bot.bot.spin_casino", new_callable=AsyncMock, return_value=None) as spin, patch(
                "tg_summary_bot.bot.cached_bot_identity", new_callable=AsyncMock, return_value=(99, "bot", "Bot")
            ) as identity:
                await handler(message, SimpleNamespace())
            self.assertEqual(spin.await_args.kwargs["stake"], expected_stake)
            if bot_wallet:
                self.assertEqual(spin.await_args.kwargs["participant"], ("id:99", "Bot"))
                self.assertGreater(identity.await_count, 0)
            else:
                self.assertNotIn("participant", spin.await_args.kwargs)
                identity.assert_not_awaited()
        for command in ("/casino .50", "/casino ,5", "/casino bot .5"):
            message = self.message()
            message.text = command
            with patch("tg_summary_bot.bot.spin_casino", new_callable=AsyncMock) as spin, patch(
                "tg_summary_bot.bot.cached_bot_identity", new_callable=AsyncMock,
            ) as identity:
                await handler(message, SimpleNamespace())
            spin.assert_not_called()
            identity.assert_not_awaited()

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

    async def test_denied_command_rejects_before_usage_reply_or_bot_identity(self) -> None:
        settings = SimpleNamespace(allowed_chat_ids={2})
        dispatcher = await create_dispatcher(
            settings, self.store, SimpleNamespace(), SimpleNamespace(), None,
            SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
            None, None, asyncio.Lock(), SimpleNamespace(),
        )
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == "casino_command")
        bot = SimpleNamespace(get_me=AsyncMock())
        for command in ("/casino", "/casino bad", "/casino bot", "/casino bot extra"):
            message = self.message()
            message.text = command
            with patch.object(self.store, "get_chat_bot_aliases", new_callable=AsyncMock) as aliases:
                await handler(message, bot)
                aliases.assert_not_awaited()
            self.assertEqual(message.replies, [])
            self.assertEqual(message.dice_calls, [])
        bot.get_me.assert_not_awaited()
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 10)

    async def test_rate_limited_report_retries_without_repeating_dice_or_payout(self) -> None:
        message = self.message()
        method = SendMessage(chat_id=1, text="result")
        response = SimpleNamespace(message_id=30, chat=message.chat)
        request = AsyncMock(side_effect=[
            TelegramRetryAfter(method=method, message="flood", retry_after=0), response,
        ])
        delivery = TelegramDeliveryMiddleware(interval=0)

        async def reply(*args, **kwargs):
            return await delivery(request, None, method)

        message.reply = reply
        with patch("tg_summary_bot.telegram_delivery.asyncio.sleep", new_callable=AsyncMock), patch(
            "tg_summary_bot.bot.refresh_pinned_leaderboard", new_callable=AsyncMock
        ), self.assertLogs(level="WARNING"):
            outcome = await spin_casino(self.settings, self.store, SimpleNamespace(), message)
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "completed"))
        self.assertEqual(request.await_count, 2)
        self.assertEqual(len(message.dice_calls), 1)
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 250)
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT reason, COUNT(*) FROM chat_point_ledger WHERE casino_spin_id IS NOT NULL GROUP BY reason"
            )).fetchall()
        self.assertEqual(dict(rows), {"casino_bet": 1, "casino_payout": 1})

    async def test_exhausted_report_delivery_preserves_completed_spin_and_refreshes_top(self) -> None:
        message = self.message()
        message.reply = AsyncMock(side_effect=TelegramRetryAfter(
            method=SendMessage(chat_id=1, text="result"), message="flood", retry_after=33,
        ))
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard", new_callable=AsyncMock) as refresh, self.assertLogs(
            level="ERROR"
        ):
            outcome = await spin_casino(self.settings, self.store, SimpleNamespace(), message)
        self.assertEqual((outcome.status, outcome.reason), ("partial", "report_delivery_failed"))
        self.assertEqual(outcome.metadata["spin_status"], "completed")
        self.assertEqual(outcome.metadata["payout"], 250)
        self.assertIsNone(outcome.response_message_id)
        self.assertEqual(len(message.dice_calls), 1)
        refresh.assert_awaited_once()
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 250)

    async def test_explicit_dice_rate_limit_retries_before_settlement_without_refund(self) -> None:
        message = self.message()
        method = SendDice(chat_id=1, emoji="🎰")
        sent = SimpleNamespace(chat=message.chat, message_id=20, dice=SimpleNamespace(emoji="🎰", value=64))
        request = AsyncMock(side_effect=[
            TelegramRetryAfter(method=method, message="flood", retry_after=0), sent,
        ])
        delivery = TelegramDeliveryMiddleware(interval=0)

        async def reply_dice(**kwargs):
            return await delivery(request, None, method)

        message.reply_dice = reply_dice
        with patch("tg_summary_bot.telegram_delivery.asyncio.sleep", new_callable=AsyncMock), patch(
            "tg_summary_bot.bot.refresh_pinned_leaderboard", new_callable=AsyncMock
        ), self.assertLogs(level="WARNING"):
            outcome = await spin_casino(self.settings, self.store, SimpleNamespace(), message)
        self.assertEqual(outcome.reason, "completed")
        self.assertEqual(request.await_count, 2)
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute(
                "SELECT reason, COUNT(*) FROM chat_point_ledger WHERE casino_spin_id IS NOT NULL GROUP BY reason"
            )).fetchall()
        self.assertEqual(dict(rows), {"casino_bet": 1, "casino_payout": 1})

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

    async def test_transient_settlement_error_retries_without_sending_another_dice(self) -> None:
        original_settle = self.store.settle_casino_spin

        async def settle(**kwargs: object):
            if retried.await_count == 1:
                raise aiosqlite.OperationalError("temporarily locked")
            return await original_settle(**kwargs)

        retried = AsyncMock(side_effect=settle)
        message = self.message()
        bot = SimpleNamespace(edit_message_text=AsyncMock(), pin_chat_message=AsyncMock())
        with patch.object(self.store, "settle_casino_spin", retried), patch(
            "tg_summary_bot.bot.CASINO_SETTLEMENT_RETRY_DELAYS", (0, 0)
        ), self.assertLogs(level="ERROR"):
            outcome = await spin_casino(self.settings, self.store, bot, message)
        self.assertEqual(outcome.reason, "completed")
        self.assertEqual(retried.await_count, 2)
        self.assertEqual(len(message.dice_calls), 1)
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 250)

    async def test_uncertain_commit_replay_preserves_one_payout_and_reports_success(self) -> None:
        original_settle = self.store.settle_casino_spin

        async def settle(**kwargs: object):
            result = await original_settle(**kwargs)
            if retried.await_count == 1:
                raise RuntimeError("response lost after commit")
            return result

        retried = AsyncMock(side_effect=settle)
        message = self.message()
        bot = SimpleNamespace(edit_message_text=AsyncMock(), pin_chat_message=AsyncMock())
        with patch.object(self.store, "settle_casino_spin", retried), patch(
            "tg_summary_bot.bot.CASINO_SETTLEMENT_RETRY_DELAYS", (0, 0)
        ), self.assertLogs(level="ERROR"):
            outcome = await spin_casino(self.settings, self.store, bot, message)
        self.assertEqual((outcome.status, outcome.reason), ("succeeded", "completed"))
        self.assertEqual(len(message.dice_calls), 1)
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 250)
        async with aiosqlite.connect(self.path) as db:
            count = await (await db.execute(
                "SELECT COUNT(*) FROM chat_point_ledger WHERE reason='casino_payout'"
            )).fetchone()
        self.assertEqual(count[0], 1)
        self.assertIn("выплата: 250", message.replies[0][0])

    async def test_exhausted_settlement_retries_warn_and_keep_startup_recovery(self) -> None:
        retried = AsyncMock(side_effect=aiosqlite.OperationalError("private database detail"))
        message = self.message()
        with patch.object(self.store, "settle_casino_spin", retried), patch(
            "tg_summary_bot.bot.CASINO_SETTLEMENT_RETRY_DELAYS", (0, 0)
        ), self.assertLogs(level="ERROR"):
            outcome = await spin_casino(self.settings, self.store, SimpleNamespace(), message)
        self.assertEqual((outcome.status, outcome.reason), ("failed", "settlement_unconfirmed"))
        self.assertEqual(retried.await_count, 3)
        self.assertEqual(len(message.dice_calls), 1)
        self.assertEqual(len(message.replies), 1)
        self.assertIn("перезапуске", message.replies[0][0])
        self.assertNotIn("private database detail", message.replies[0][0])
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 0)
        await self.store.refund_pending_casino_spins(created_before=datetime.now(timezone.utc))
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 10)

    async def test_cancellation_during_settlement_attempts_refund(self) -> None:
        reserved = await self.store.reserve_casino_spin(
            chat_id=1, request_message_id=10, participant_key="id:1", participant_name="Alice",
        )
        with self.assertRaises(asyncio.CancelledError):
            await run_casino_dice_lifecycle(
                chat_id=1, spin_id=reserved.spin.spin_id,
                send_dice=self.message().reply_dice,
                settle=AsyncMock(side_effect=asyncio.CancelledError()),
                refund=lambda reason: self.store.refund_casino_spin(
                    spin_id=reserved.spin.spin_id, reason=reason,
                ),
            )
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:1")).balance, 10)
