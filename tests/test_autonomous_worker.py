import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

from tg_summary_bot.autonomous_jokes import AutonomousJokeWorker, log_worker_failure
from tg_summary_bot.joke_awards import JokeSelector
from tg_summary_bot.storage import MessageStore


class AutonomousJokeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = MessageStore(Path(self.directory.name) / "jobs.sqlite3")
        await self.store.init()
        self.now = datetime.now(timezone.utc)
        self.start = self.now - timedelta(hours=1)
        await self.store.get_or_create_autonomous_joke_start_at(now=self.start)
        self.settings = SimpleNamespace(
            allowed_chat_ids={2}, joke_awards_disabled_chat_ids=set(),
            casino_disabled_chat_ids=set(), autonomous_jokes_block_messages=1,
            autonomous_jokes_partial_min_messages=1,
            autonomous_jokes_max_block_age=timedelta(days=3),
            autonomous_jokes_initial_lookback=timedelta(days=7),
            autonomous_jokes_lease_seconds=900, autonomous_jokes_shadow_mode=False,
            autonomous_joke_announce=True, bot_auto_casino_enabled=False,
            bot_auto_casino_chance=0.25, autonomous_jokes_startup_grace_seconds=0.001,
            autonomous_jokes_poll_seconds=0.001,
        )

        async def complete(*, system: str, user: str, **_: object) -> str:
            candidate = json.loads(user)["candidates"][0]
            return json.dumps({
                "has_joke": True, "source_message_id": candidate["target_message_id"],
                "reason": "test joke",
            })

        self.client = SimpleNamespace(complete=AsyncMock(side_effect=complete), unload=AsyncMock())
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=99)))
        self.worker = AutonomousJokeWorker(
            settings=self.settings, store=self.store, selector=JokeSelector(self.client),
            bot=self.bot, gpu_lock=asyncio.Lock(),
        )

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def plan_chat(self, chat_id: int):
        await self.store.save_message(
            chat_id=chat_id, message_id=1, chat_type="group", sender_id=10,
            sender_name="Alice", text="test joke", created_at=self.now,
            reply_to_message_id=None, origin="incoming", kind="text",
        )
        jobs = await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(), start_after=self.start, block_messages=1,
            partial_min_messages=1, max_block_age=timedelta(days=3), now=self.now,
        )
        return next(job for job in jobs if job.chat_id == chat_id)

    async def test_old_denied_job_does_not_block_allowed_job_or_send(self) -> None:
        denied = await self.plan_chat(1)
        allowed = await self.plan_chat(2)
        await self.worker.tick()
        self.assertEqual((await self.store.get_joke_job(denied.job_id)).status, "pending")
        self.assertEqual((await self.store.get_joke_job(allowed.job_id)).status, "awarded")
        self.assertIsNone(await self.store.get_point_balance(chat_id=1, participant_key="id:10"))
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 10)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args[0], 2)
        self.client.complete.assert_awaited_once()

    async def test_old_denied_outbox_stays_paused_until_chat_is_allowed(self) -> None:
        job = await self.plan_chat(1)
        claimed = await self.store.claim_due_joke_job(worker_id="old", now=self.now, lease_seconds=900)
        await self.store.finalize_autonomous_joke_job(
            job_id=job.job_id, lease_token=claimed.lease_token, source_message_id=1,
            bot_spin_decision="skip",
        )
        await self.worker._run_outbox()
        self.bot.send_message.assert_not_awaited()
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute(
                "SELECT status, attempt_count FROM chat_joke_outbox WHERE job_id=?", (job.job_id,)
            )).fetchall()
        self.assertTrue(all(tuple(row) == ("pending", 0) for row in rows))

        self.settings.allowed_chat_ids = set()
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args[0], 1)

    async def test_disabled_chat_overrides_allowlist_for_jobs_and_outbox(self) -> None:
        job = await self.plan_chat(2)
        self.settings.joke_awards_disabled_chat_ids = {2}
        await self.worker.tick()
        self.assertEqual((await self.store.get_joke_job(job.job_id)).status, "pending")
        self.client.complete.assert_not_awaited()
        claimed = await self.store.claim_due_joke_job(worker_id="old", now=self.now, lease_seconds=900)
        await self.store.finalize_autonomous_joke_job(
            job_id=job.job_id, lease_token=claimed.lease_token, source_message_id=1,
            bot_spin_decision="skip",
        )
        await self.worker._run_outbox()
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(await self.store.claim_due_joke_outbox(
            worker_id="test", now=self.now, lease_seconds=900,
            allowed_chat_ids={2}, disabled_chat_ids={2}, start_after=self.start,
        ))

    async def test_transient_activation_or_backfill_error_is_retried(self) -> None:
        for failing_step in ("activation", "backfill"):
            with self.subTest(failing_step=failing_step):
                self.worker.start_after = None
                stop = asyncio.Event()
                activation = AsyncMock(side_effect=(
                    [aiosqlite.OperationalError("locked"), self.start]
                    if failing_step == "activation" else [self.start]
                ))
                backfill = AsyncMock(side_effect=(
                    [aiosqlite.OperationalError("locked"), 0]
                    if failing_step == "backfill" else [0]
                ))
                self.worker.store = SimpleNamespace(
                    get_or_create_autonomous_joke_start_at=activation,
                    backfill_autonomous_joke_inbox=backfill,
                )
                self.worker.tick = AsyncMock(side_effect=stop.set)
                with self.assertLogs(level="ERROR"):
                    await asyncio.wait_for(self.worker.run(stop), timeout=1)
                self.assertEqual(activation.await_count, 2 if failing_step == "activation" else 1)
                self.assertEqual(backfill.await_count, 2 if failing_step == "backfill" else 1)
                self.assertEqual(backfill.await_args.kwargs["start_after"], self.start)
                self.worker.tick.assert_awaited_once()

    async def commit_live_job(self):
        job = await self.plan_chat(2)
        claimed = await self.store.claim_due_joke_job(worker_id="old", now=self.now, lease_seconds=900)
        await self.store.finalize_autonomous_joke_job(
            job_id=job.job_id, lease_token=claimed.lease_token, source_message_id=1,
            bot_spin_decision="spin", announce=True,
        )
        # Fund the bot with one real ledger award, separately from the human winner.
        await self.store.save_message(
            chat_id=2, message_id=2, chat_type="group", sender_id=99,
            sender_name="Bot", text="bot joke", created_at=self.now,
            reply_to_message_id=None, origin="assistant", kind="assistant_answer",
        )
        await self.store.award_unique_joke(chat_id=2, source_message_id=2)
        self.bot.send_dice = AsyncMock(return_value=SimpleNamespace(
            chat=SimpleNamespace(id=2), message_id=55, dice=SimpleNamespace(emoji="🎰", value=64),
        ))
        return job

    async def test_shadow_pauses_all_old_outbox_actions_without_claiming(self) -> None:
        await self.commit_live_job()
        self.settings.autonomous_jokes_shadow_mode = True
        self.settings.bot_auto_casino_enabled = True
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", new_callable=AsyncMock) as refresh:
            for _ in range(4):
                await self.worker._run_outbox()
            refresh.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()
        self.bot.send_dice.assert_not_awaited()
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:99")).balance, 10)
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute("SELECT status, attempt_count FROM chat_joke_outbox")).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("pending", 0)] * 3)
        self.settings.autonomous_jokes_shadow_mode = False
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()

    async def test_disabled_actions_pause_without_blocking_refresh_and_resume_once(self) -> None:
        await self.commit_live_job()
        self.settings.autonomous_joke_announce = False
        self.settings.bot_auto_casino_enabled = False
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", new_callable=AsyncMock) as refresh:
            await self.worker._run_outbox()
            await self.worker._run_outbox()
            refresh.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()
        self.bot.send_dice.assert_not_awaited()
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute(
                "SELECT action, status, attempt_count FROM chat_joke_outbox ORDER BY outbox_id"
            )).fetchall()
        self.assertEqual([tuple(row) for row in rows], [
            ("award_notification", "pending", 0),
            ("award_leaderboard_refresh", "completed", 1),
            ("bot_casino", "pending", 0),
        ])
        self.settings.bot_auto_casino_enabled = True
        with patch("tg_summary_bot.bot.cached_bot_identity", AsyncMock(return_value=(99, "bot", "Bot"))):
            await self.worker._run_outbox()
        self.bot.send_dice.assert_awaited_once()
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:99")).balance, 250)
        self.settings.autonomous_joke_announce = True
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        self.bot.send_dice.assert_awaited_once()

    async def test_task_failure_is_reported_and_cancellation_is_expected(self) -> None:
        async def fail() -> None:
            raise RuntimeError("worker failure")

        task = asyncio.create_task(fail())
        await asyncio.gather(task, return_exceptions=True)
        with self.assertLogs(level="ERROR") as logs:
            log_worker_failure(task)
        self.assertIn("stopped unexpectedly", logs.output[0])

        cancelled = asyncio.create_task(asyncio.sleep(60))
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        with self.assertNoLogs(level="ERROR"):
            log_worker_failure(cancelled)
