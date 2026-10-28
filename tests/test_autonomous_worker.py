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
from tg_summary_bot.config import load_settings
from tg_summary_bot.joke_awards import QUALIFICATION_SYSTEM_PROMPT, JokeSelector
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
            autonomous_jokes_max_jobs_per_run=1,
        )

        async def complete(*, system: str, user: str, **_: object) -> str:
            candidate = json.loads(user)["candidates"][0]
            if system == QUALIFICATION_SYSTEM_PROMPT:
                return json.dumps({"has_joke": True, "source_message_id": candidate["target_message_id"],
                                   "humor_type": "wordplay", "evidence_quote": candidate["text"],
                                   "needs_context": False, "reason": "test joke"})
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
        self.worker.announcement_start_at = self.start

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

    async def commit_award(self, message_id, awarded_at, *, source_age=timedelta(0)):
        await self.store.save_message(
            chat_id=2, message_id=message_id, chat_type="group", sender_id=10, sender_name="Alice",
            text=f"joke {message_id}", created_at=self.now - source_age, reply_to_message_id=None,
            origin="incoming", kind="text",
        )
        jobs = await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids={2}, block_messages=1, partial_min_messages=1,
            max_block_age=timedelta(days=3), now=self.now,
        )
        job = jobs[-1]
        claimed = await self.store.claim_due_joke_job(worker_id="fixture", now=self.now, lease_seconds=900)
        await self.store.finalize_autonomous_joke_job(
            job_id=job.job_id, lease_token=claimed.lease_token, source_message_id=message_id,
            awarded_at=awarded_at, bot_spin_decision="skip",
        )
        return job

    async def test_expired_notice_is_skipped_without_changing_points_or_award(self):
        job = await self.commit_award(1, self.now - timedelta(days=40))
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker._run_outbox()
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 10)
        self.assertEqual((await self.store.get_joke_job(job.job_id)).status, "awarded")
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute("SELECT status,last_error_code FROM chat_joke_outbox WHERE job_id=? AND action='award_notification'", (job.job_id,))).fetchone()
            count = await (await db.execute("SELECT COUNT(*) FROM chat_point_ledger WHERE reason='best_joke'")).fetchone()
        self.assertEqual(tuple(row), ("skipped", "announcement_expired"))
        self.assertEqual(count[0], 1)

    async def test_restart_skips_yesterdays_notices_even_inside_freshness_window(self):
        first = await self.commit_award(1, self.now - timedelta(hours=12))
        second = await self.commit_award(2, self.now - timedelta(hours=3))
        restarted = AutonomousJokeWorker(settings=self.settings, store=self.store, selector=self.worker.selector,
                                         bot=self.bot, gpu_lock=asyncio.Lock())
        restarted.announcement_start_at = self.now
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await restarted._run_outbox()
        self.bot.send_message.assert_not_awaited()
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute("SELECT status,last_error_code FROM chat_joke_outbox WHERE action='award_notification' ORDER BY outbox_id")).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("skipped", "announcement_before_worker_start")] * 2)
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 20)
        for job in (first, second):
            self.assertEqual((await self.store.get_joke_job(job.job_id)).status, "awarded")

    async def test_post_restart_award_is_announced(self):
        self.worker.announcement_start_at = self.now - timedelta(seconds=1)
        await self.commit_award(1, self.now)
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()

    async def test_announcement_limit_defers_second_notice_without_changing_points(self):
        await self.commit_award(1, self.now)
        await self.worker._run_outbox()
        job = await self.commit_award(2, self.now + timedelta(seconds=1))
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute("SELECT status,last_error_code,next_attempt_at FROM chat_joke_outbox WHERE job_id=? AND action='award_notification'", (job.job_id,))).fetchone()
        self.assertEqual(tuple(row)[:2], ("retry", "announcement_rate_limited"))
        self.assertGreater(datetime.fromisoformat(row[2]), self.now + timedelta(minutes=59))
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 20)

    async def test_announcement_limit_survives_store_and_worker_restart(self):
        await self.commit_award(1, self.now)
        await self.worker._run_outbox()
        store = MessageStore(self.store.database_path)
        await store.init()
        restarted = AutonomousJokeWorker(settings=self.settings, store=store, selector=self.worker.selector,
                                         bot=self.bot, gpu_lock=asyncio.Lock())
        restarted.announcement_start_at = self.now
        await self.commit_award(2, self.now + timedelta(seconds=1))
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await restarted._run_outbox()
        self.bot.send_message.assert_awaited_once()

    async def test_rate_limited_chat_does_not_block_another_chat(self):
        await self.commit_award(1, self.now)
        await self.worker._run_outbox()
        self.settings.allowed_chat_ids = set()
        await self.commit_award(2, self.now + timedelta(seconds=1))
        job = await self.plan_chat(3)
        claimed = await self.store.claim_due_joke_job(worker_id="fixture", now=self.now, lease_seconds=900)
        await self.store.finalize_autonomous_joke_job(job_id=job.job_id, lease_token=claimed.lease_token,
                                                     source_message_id=1, awarded_at=self.now + timedelta(seconds=2))
        await self.worker._run_outbox()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.bot.send_message.call_args.args[0], 3)

    async def test_uncertain_send_failure_also_consumes_cooldown(self):
        await self.commit_award(1, self.now)
        self.bot.send_message.side_effect = RuntimeError("uncertain network response")
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        await self.commit_award(2, self.now + timedelta(seconds=1))
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()

    async def test_cooldown_expiry_allows_pending_notice(self):
        await self.commit_award(1, self.now)
        await self.worker._run_outbox()
        await self.commit_award(2, self.now + timedelta(seconds=1))
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker._run_outbox()
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute("SELECT last_sent_at FROM chat_joke_announcement_state WHERE chat_id=2")).fetchone()
        available = datetime.fromisoformat(row[0]) + timedelta(hours=12, seconds=1)
        clock = SimpleNamespace(now=lambda tz: available, fromisoformat=datetime.fromisoformat)
        with patch("tg_summary_bot.autonomous_jokes.datetime", clock):
            await self.worker._run_outbox()
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_atomic_reservation_across_store_instances_and_lease_fencing(self):
        await self.commit_award(1, self.now)
        await self.commit_award(2, self.now)
        first = await self.store.claim_due_joke_outbox(worker_id="one", now=self.now, lease_seconds=900,
                                                     allowed_actions={"award_notification"})
        second = await self.store.claim_due_joke_outbox(worker_id="two", now=self.now, lease_seconds=900,
                                                      allowed_actions={"award_notification"})
        other = MessageStore(self.store.database_path)
        results = await asyncio.gather(*[
            store.reserve_joke_announcement(chat_id=2, outbox_id=outbox.outbox_id, lease_token=outbox.lease_token,
                                            now=self.now, min_interval=timedelta(hours=1))
            for store, outbox in ((self.store, first), (other, second))
        ])
        self.assertEqual(results.count(None), 1)
        self.assertIn(self.now + timedelta(hours=1), results)
        with self.assertRaisesRegex(RuntimeError, "lost_lease"):
            await other.reserve_joke_announcement(chat_id=2, outbox_id=first.outbox_id, lease_token="stale-token",
                                                  now=self.now, min_interval=timedelta(hours=1))

    async def test_upgrade_seeds_recent_delivery_from_existing_outbox_history(self):
        await self.commit_award(1, self.now)
        await self.worker._run_outbox()
        await self.commit_award(2, self.now)
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute("DROP TABLE chat_joke_announcement_state")  # Temporary legacy-schema fixture only.
            await db.commit()
        store = MessageStore(self.store.database_path)
        await store.init()
        await store.init()
        now = datetime.now(timezone.utc)
        outbox = await store.claim_due_joke_outbox(worker_id="upgrade", now=now, lease_seconds=900,
                                                 allowed_actions={"award_notification"})
        available = await store.reserve_joke_announcement(chat_id=2, outbox_id=outbox.outbox_id,
                                                         lease_token=outbox.lease_token, now=now,
                                                         min_interval=timedelta(hours=1))
        self.assertGreater(available, now + timedelta(minutes=59))

    async def test_explicit_backlog_opt_in_keeps_age_limit(self):
        await self.commit_award(1, self.now - timedelta(hours=3))
        self.worker.announcement_start_at = self.now
        self.settings.autonomous_joke_announce_backlog = True
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        await self.commit_award(2, self.now - timedelta(days=2))
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()

    async def test_fresh_notice_overtakes_old_backlog_and_reports_original_award_time(self):
        old = await self.commit_award(1, self.now - timedelta(days=40))
        fresh = await self.commit_award(2, self.now - timedelta(minutes=1))
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        self.assertIn("joke 2", self.bot.send_message.call_args.args[1])
        self.assertIn("начислено", self.bot.send_message.call_args.args[1])
        self.assertIn((self.now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M UTC"), self.bot.send_message.call_args.args[1])
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute("SELECT job_id,status FROM chat_joke_outbox WHERE action='award_notification' ORDER BY job_id")).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(old.job_id, "pending"), (fresh.job_id, "completed")])
        with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 20)

    async def test_old_source_with_fresh_award_can_be_announced(self):
        await self.commit_award(1, self.now, source_age=timedelta(days=4))
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()

    async def test_expiry_preserves_paused_chat_and_shadow_records(self):
        job = await self.commit_award(1, self.now - timedelta(days=40))
        for mode in ("denied", "disabled", "shadow", "announce_off"):
            self.settings.allowed_chat_ids = {3} if mode == "denied" else {2}
            self.settings.joke_awards_disabled_chat_ids = {2} if mode == "disabled" else set()
            self.settings.autonomous_jokes_shadow_mode = mode == "shadow"
            self.settings.autonomous_joke_announce = mode != "announce_off"
            with patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
                await self.worker._run_outbox()
            async with aiosqlite.connect(self.store.database_path) as db:
                row = await (await db.execute("SELECT status,attempt_count FROM chat_joke_outbox WHERE job_id=? AND action='award_notification'", (job.job_id,))).fetchone()
            self.assertEqual(tuple(row), ("pending", 0))
        self.bot.send_message.assert_not_awaited()

    async def test_custom_expiry_and_transient_retry_crossing_expiry(self):
        job = await self.commit_award(1, self.now - timedelta(minutes=4))
        self.settings.autonomous_joke_announcement_max_age = timedelta(minutes=5)
        self.bot.send_message.side_effect = RuntimeError("temporary network failure")
        await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute("UPDATE chat_joke_outbox SET next_attempt_at=NULL WHERE job_id=? AND action='award_notification'", (job.job_id,))
            await db.commit()
        clock = SimpleNamespace(now=lambda tz: self.now + timedelta(minutes=2), fromisoformat=datetime.fromisoformat)
        with patch("tg_summary_bot.autonomous_jokes.datetime", clock), \
             patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker._run_outbox()
        self.bot.send_message.assert_awaited_once()
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 10)

    async def test_delivery_precedes_expensive_selection(self):
        await self.commit_award(1, self.now)
        await self.store.save_message(chat_id=2, message_id=2, chat_type="group", sender_id=10,
                                      sender_name="Alice", text="second joke", created_at=self.now,
                                      reply_to_message_id=None, origin="incoming", kind="text")
        async def select(job):
            self.bot.send_message.assert_awaited_once()
        with patch.object(self.worker, "_run_job", side_effect=select), \
             patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker.tick()

    async def test_expired_cleanup_is_bounded_and_keeps_all_awards(self):
        for message_id in range(1, 12):
            await self.commit_award(message_id, self.now - timedelta(days=40))
        await self.worker._run_outbox()
        async with aiosqlite.connect(self.store.database_path) as db:
            rows = await (await db.execute("SELECT status,COUNT(*) FROM chat_joke_outbox WHERE action='award_notification' GROUP BY status")).fetchall()
        self.assertEqual(dict(rows), {"skipped": 10, "pending": 1})
        self.bot.send_message.assert_not_awaited()
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 110)

    async def test_qualification_uses_shared_lock_and_rejected_winner_never_gets_points(self):
        job = await self.plan_chat(2)
        events = []
        async def complete(*, system, user, **kwargs):
            self.assertTrue(self.worker.gpu_lock.locked())
            if system == QUALIFICATION_SYSTEM_PROMPT:
                events.append("qualify")
                return json.dumps({"has_joke": False, "source_message_id": None, "humor_type": "none",
                                   "evidence_quote": "", "needs_context": False, "reason": "no visible joke"})
            events.append("select")
            return '{"has_joke":true,"source_message_id":1,"reason":"winner"}'
        async def unload():
            self.assertTrue(self.worker.gpu_lock.locked())
            events.append("unload")
        self.worker.selector = JokeSelector(SimpleNamespace(complete=complete, unload=unload))
        await self.worker.tick()
        self.assertEqual(events, ["select", "qualify", "unload"])
        self.assertEqual((await self.store.get_joke_job(job.job_id)).status, "no_joke")
        self.assertIsNone(await self.store.get_point_balance(chat_id=2, participant_key="id:10"))
        self.bot.send_message.assert_not_awaited()

    async def test_broken_qualification_retries_without_award(self):
        job = await self.plan_chat(2)
        self.client.complete.side_effect = [
            '{"has_joke":true,"source_message_id":1,"reason":"winner"}', '{"has_joke":true',
        ]
        await self.worker.tick()
        result = await self.store.get_joke_job(job.job_id)
        self.assertEqual(result.status, "retry")
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute("SELECT last_error_code FROM chat_joke_jobs WHERE job_id=?", (job.job_id,))).fetchone()
        self.assertEqual(row[0], "qualification_invalid_json")
        self.assertIsNone(await self.store.get_point_balance(chat_id=2, participant_key="id:10"))
        self.bot.send_message.assert_not_awaited()

    async def make_due_again(self):
        # Isolated test fixture; never used against production storage.
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute("UPDATE autonomous_joke_state SET next_run_at=NULL")
            await db.execute("UPDATE chat_joke_jobs SET next_attempt_at=NULL")
            await db.commit()

    async def test_adjacent_jobs_reuse_model_and_commit_only_after_unlock(self):
        self.settings.autonomous_jokes_max_jobs_per_run = 4
        self.settings.allowed_chat_ids = set()
        first = await self.plan_chat(2)
        second = await self.plan_chat(3)
        original = self.store.finalize_autonomous_joke_job
        async def commit(**kwargs):
            self.assertFalse(self.worker.gpu_lock.locked())
            return await original(**kwargs)
        async def unload():
            self.assertTrue(self.worker.gpu_lock.locked())
            self.assertEqual(self.client.complete.await_count, 4)
        self.client.unload.side_effect = unload
        with patch.object(self.store, "finalize_autonomous_joke_job", side_effect=commit):
            await self.worker.tick()
        self.client.unload.assert_awaited_once()
        for job in (first, second):
            self.assertEqual((await self.store.get_joke_job(job.job_id)).status, "awarded")

    async def test_short_fifty_message_block_needs_only_two_model_calls(self):
        self.settings.autonomous_jokes_block_messages = 50
        self.worker.selector = JokeSelector(self.client, row_limit=50)
        for message_id in range(1, 51):
            await self.store.save_message(
                chat_id=2, message_id=message_id, chat_type="group", sender_id=10,
                sender_name="Alice", text=f"joke {message_id}", created_at=self.now,
                reply_to_message_id=None, origin="incoming", kind="text",
            )
        await self.worker.tick()
        self.assertEqual(self.client.complete.await_count, 2)
        self.client.unload.assert_awaited_once()
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 10)

    async def test_schedule_survives_restart_without_delaying_outbox(self):
        self.settings.allowed_chat_ids = set()
        first = await self.plan_chat(2)
        second = await self.plan_chat(3)
        await self.worker.tick()
        next_run = await self.store.get_autonomous_joke_next_run_at()
        self.assertGreater(datetime.fromisoformat(next_run), self.now + timedelta(hours=5))
        self.store = MessageStore(self.store.database_path)
        await self.store.init()
        restarted = AutonomousJokeWorker(settings=self.settings, store=self.store,
                                         selector=self.worker.selector, bot=self.bot, gpu_lock=asyncio.Lock())
        with patch.object(restarted, "_run_outbox", new_callable=AsyncMock) as outbox:
            await restarted.tick()
        outbox.assert_awaited_once()
        self.assertEqual(self.client.complete.await_count, 2)
        self.assertEqual((await self.store.get_joke_job(first.job_id)).status, "awarded")
        self.assertEqual((await self.store.get_joke_job(second.job_id)).status, "pending")
        self.assertEqual(await self.store.get_autonomous_joke_next_run_at(), next_run)

    async def test_scheduled_run_opens_again_when_interval_expires(self):
        self.settings.allowed_chat_ids = set()
        await self.plan_chat(2)
        second = await self.plan_chat(3)
        await self.worker.tick()
        due = datetime.fromisoformat(await self.store.get_autonomous_joke_next_run_at())
        clock = SimpleNamespace(now=lambda tz: due + timedelta(seconds=1), fromisoformat=datetime.fromisoformat)
        with patch("tg_summary_bot.autonomous_jokes.datetime", clock), \
             patch("tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", AsyncMock(return_value=True)):
            await self.worker.tick()
        self.assertEqual(self.client.complete.await_count, 4)
        self.assertEqual((await self.store.get_joke_job(second.job_id)).status, "awarded")

    async def test_max_jobs_bounds_each_scheduled_run(self):
        self.settings.autonomous_jokes_max_jobs_per_run = 2
        self.settings.allowed_chat_ids = set()
        jobs = [await self.plan_chat(chat_id) for chat_id in (2, 3, 4)]
        await self.worker.tick()
        self.assertEqual(self.client.complete.await_count, 4)
        self.client.unload.assert_awaited_once()
        self.assertEqual([(await self.store.get_joke_job(job.job_id)).status for job in jobs],
                         ["awarded", "awarded", "pending"])

    async def test_atomic_run_admission_across_stores(self):
        await self.plan_chat(2)
        await self.plan_chat(3)
        other = MessageStore(self.store.database_path)
        claims = await asyncio.gather(*[
            store.claim_due_joke_job(worker_id=name, now=self.now, lease_seconds=900,
                                     run_interval=timedelta(hours=6))
            for store, name in ((self.store, "one"), (other, "two"))
        ])
        self.assertEqual(sum(job is not None for job in claims), 1)

    async def test_empty_or_gpu_busy_tick_does_not_consume_schedule_or_attempts(self):
        await self.worker.tick()
        self.assertIsNone(await self.store.get_autonomous_joke_next_run_at())
        job = await self.plan_chat(2)
        async with self.worker.gpu_lock:
            await self.worker.tick()
        stored = await self.store.get_joke_job(job.job_id)
        self.assertEqual((stored.status, stored.attempt_count), ("pending", 0))
        self.assertIsNone(await self.store.get_autonomous_joke_next_run_at())
        self.client.complete.assert_not_awaited()
        self.client.unload.assert_not_awaited()
        await self.worker.tick()
        self.assertEqual(self.client.complete.await_count, 2)

    async def test_call_limit_yields_to_foreground_without_repeating_selection(self):
        self.settings.autonomous_jokes_session_max_calls = 1
        job = await self.plan_chat(2)
        selected = asyncio.Event()
        queued = asyncio.Event()
        foreground_done = False
        original = self.client.complete.side_effect
        async def complete(**kwargs):
            if kwargs["system"] != QUALIFICATION_SYSTEM_PROMPT:
                selected.set()
                await queued.wait()
            else:
                self.assertTrue(foreground_done)
            return await original(**kwargs)
        async def foreground():
            nonlocal foreground_done
            await selected.wait()
            queued.set()
            async with self.worker.gpu_lock:
                self.assertEqual(self.client.unload.await_count, 1)
                foreground_done = True
        self.client.complete.side_effect = complete
        await asyncio.wait_for(asyncio.gather(self.worker.tick(), foreground()), timeout=2)
        self.assertEqual(self.client.complete.await_count, 2)
        self.assertEqual(self.client.unload.await_count, 2)
        self.assertEqual((await self.store.get_joke_job(job.job_id)).attempt_count, 1)
        self.assertEqual((await self.store.get_joke_job(job.job_id)).status, "awarded")

    async def test_soft_time_limit_yields_between_calls_without_restarting_job(self):
        await self.plan_chat(2)
        elapsed = 0
        original = self.client.complete.side_effect
        async def complete(**kwargs):
            nonlocal elapsed
            elapsed += 121
            return await original(**kwargs)
        self.client.complete.side_effect = complete
        with patch("tg_summary_bot.autonomous_jokes.time", SimpleNamespace(monotonic=lambda: elapsed)):
            await self.worker.tick()
        self.assertEqual(self.client.complete.await_count, 2)
        self.assertEqual(self.client.unload.await_count, 2)

    async def test_tournament_progress_survives_session_boundary(self):
        self.settings.autonomous_jokes_session_max_calls = 3
        self.settings.autonomous_jokes_block_messages = 5
        self.worker.selector = JokeSelector(self.client, row_limit=2)
        for message_id in range(1, 6):
            await self.store.save_message(
                chat_id=2, message_id=message_id, chat_type="group", sender_id=10,
                sender_name="Alice", text=f"joke {message_id}", created_at=self.now,
                reply_to_message_id=None, origin="incoming", kind="text",
            )
        await self.worker.tick()
        # Three pages, two tournament calls, one qualification: no replayed pages.
        self.assertEqual(self.client.complete.await_count, 6)
        self.assertEqual(self.client.unload.await_count, 2)
        self.assertEqual((await self.store.get_point_balance(chat_id=2, participant_key="id:10")).balance, 10)

    async def test_failure_flushed_mid_run_is_not_claimed_twice_in_same_batch(self):
        self.settings.allowed_chat_ids = set()
        self.settings.autonomous_jokes_max_jobs_per_run = 4
        self.settings.autonomous_jokes_session_max_calls = 3
        first = await self.plan_chat(2)
        second = await self.plan_chat(3)
        original = self.client.complete.side_effect
        async def complete(**kwargs):
            if self.client.complete.await_count == 2:
                return "not json"
            return await original(**kwargs)
        self.client.complete.side_effect = complete
        with patch("tg_summary_bot.autonomous_jokes.retry_delay", return_value=timedelta(0)):
            await self.worker.tick()
        self.assertEqual(self.client.complete.await_count, 4)
        self.assertEqual((await self.store.get_joke_job(first.job_id)).attempt_count, 1)
        self.assertEqual((await self.store.get_joke_job(second.job_id)).status, "awarded")

    async def test_cancelled_selection_unloads_before_unlock_without_award(self):
        job = await self.plan_chat(2)
        entered = asyncio.Event()
        async def complete(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        async def unload():
            self.assertTrue(self.worker.gpu_lock.locked())
        self.client.complete.side_effect = complete
        self.client.unload.side_effect = unload
        task = asyncio.create_task(self.worker.tick())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.unload.assert_awaited_once()
        self.assertFalse(self.worker.gpu_lock.locked())
        self.assertIsNone(await self.store.get_point_balance(chat_id=2, participant_key="id:10"))
        self.assertEqual((await self.store.get_joke_job(job.job_id)).status, "running")

    async def test_malformed_results_are_quarantined_durably_after_three_failures(self):
        job = await self.plan_chat(2)
        original = self.client.complete.side_effect
        async def complete(**kwargs):
            if kwargs["system"] == QUALIFICATION_SYSTEM_PROMPT:
                return '{"has_joke":false,"source_message_id":1,"humor_type":"none",' \
                       '"evidence_quote":"","needs_context":false,"reason":"none"}'
            return await original(**kwargs)
        self.client.complete.side_effect = complete
        for index in range(3):
            if index == 2:
                with self.assertLogs(level="WARNING") as captured:
                    await self.worker.tick()
                self.assertIn("quarantined=True", " ".join(captured.output))
            else:
                await self.worker.tick()
            stored = await self.store.get_joke_job(job.job_id)
            self.assertEqual(stored.validation_failures, index + 1)
            self.assertEqual(stored.quarantined_at is not None, index == 2)
            self.store = MessageStore(self.store.database_path)
            await self.store.init()
            self.worker.store = self.store
            await self.make_due_again()
        await self.worker.tick()
        self.assertEqual(self.client.complete.await_count, 6)
        self.assertEqual((await self.store.get_joke_job(job.job_id)).attempt_count, 3)
        self.assertIsNone(await self.store.get_point_balance(chat_id=2, participant_key="id:10"))
        self.bot.send_message.assert_not_awaited()

    async def test_transient_failures_do_not_exhaust_validation_budget(self):
        job = await self.plan_chat(2)
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute("UPDATE chat_joke_jobs SET attempt_count=20 WHERE job_id=?", (job.job_id,))
            await db.commit()
        self.client.complete.side_effect = RuntimeError("temporary network error")
        for _ in range(4):
            await self.worker.tick()
            await self.make_due_again()
        stored = await self.store.get_joke_job(job.job_id)
        self.assertEqual((stored.attempt_count, stored.validation_failures, stored.quarantined_at), (24, 0, None))
        self.client.complete.side_effect = None
        self.client.complete.return_value = "not json"
        await self.worker.tick()
        stored = await self.store.get_joke_job(job.job_id)
        self.assertEqual((stored.validation_failures, stored.quarantined_at), (1, None))

    async def test_stale_failure_cannot_change_validation_counter(self):
        job = await self.plan_chat(2)
        first = await self.store.claim_due_joke_job(worker_id="first", now=self.now, lease_seconds=1)
        second = await self.store.claim_due_joke_job(worker_id="second", now=self.now + timedelta(seconds=2),
                                                    lease_seconds=900)
        committed = await self.store.fail_joke_job(
            job_id=job.job_id, lease_token=first.lease_token, error_code="invalid_json",
            next_attempt_at=self.now, validation_failure=True, max_validation_failures=1,
        )
        self.assertFalse(committed)
        stored = await self.store.get_joke_job(job.job_id)
        self.assertEqual(stored.lease_token, second.lease_token)
        self.assertEqual((stored.validation_failures, stored.quarantined_at), (0, None))

    async def test_legacy_schema_upgrade_starts_new_counter_without_using_attempt_count(self):
        job = await self.plan_chat(2)
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute("ALTER TABLE chat_joke_jobs DROP COLUMN validation_failures")
            await db.execute("ALTER TABLE chat_joke_jobs DROP COLUMN quarantined_at")
            await db.execute("ALTER TABLE autonomous_joke_state DROP COLUMN next_run_at")
            await db.execute("UPDATE chat_joke_jobs SET attempt_count=15,last_error_code='qualification_invalid_json'")
            await db.commit()
        await self.store.init()
        await self.store.init()
        stored = await self.store.get_joke_job(job.job_id)
        self.assertEqual((stored.attempt_count, stored.validation_failures, stored.quarantined_at), (15, 0, None))
        self.assertIsNone(await self.store.get_autonomous_joke_next_run_at())

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
        self.assertEqual(self.client.complete.await_count, 2)

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


class AutonomousJokeConfigTests(unittest.TestCase):
    def test_batch_defaults_and_positive_limits_without_reading_env_file(self):
        with patch("tg_summary_bot.config.load_dotenv"), patch.dict(
            "os.environ", {"TELEGRAM_BOT_TOKEN": "123:test"}, clear=True,
        ):
            settings = load_settings()
        self.assertEqual(settings.autonomous_jokes_run_interval, timedelta(hours=6))
        self.assertEqual(settings.autonomous_jokes_max_jobs_per_run, 4)
        self.assertEqual(settings.autonomous_jokes_session_max_calls, 8)
        self.assertEqual(settings.autonomous_jokes_session_max_seconds, 120)
        self.assertEqual(settings.autonomous_jokes_max_validation_failures, 3)
        for name in ("AUTONOMOUS_JOKES_MAX_JOBS_PER_RUN", "AUTONOMOUS_JOKES_SESSION_MAX_CALLS",
                     "AUTONOMOUS_JOKES_SESSION_MAX_SECONDS", "AUTONOMOUS_JOKES_MAX_VALIDATION_FAILURES",
                     "AUTONOMOUS_JOKES_RUN_INTERVAL"):
            with self.subTest(name=name), patch("tg_summary_bot.config.load_dotenv"), patch.dict(
                "os.environ", {"TELEGRAM_BOT_TOKEN": "123:test", name: "0"}, clear=True,
            ), self.assertRaises((RuntimeError, ValueError)):
                load_settings()
