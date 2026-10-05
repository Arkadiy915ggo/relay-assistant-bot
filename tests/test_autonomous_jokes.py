import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from tg_summary_bot.casino import automatic_casino_trigger
from tg_summary_bot.storage import MessageStore


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class AutonomousJokeStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = MessageStore(Path(self.directory.name) / "jobs.sqlite3")
        await self.store.init()

    async def asyncTearDown(self) -> None:
        self.directory.cleanup()

    async def save(self, message_id: int, *, created_at: datetime = NOW) -> None:
        await self.store.save_message(
            chat_id=1, message_id=message_id, chat_type="group", sender_id=10,
            sender_name="Alice", text=f"joke {message_id}", created_at=created_at,
            reply_to_message_id=None, origin="incoming", kind="text",
        )

    async def test_inbox_blocks_and_atomic_finalize(self) -> None:
        for message_id in range(1, 121):
            await self.save(message_id)
        jobs = await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(), block_messages=100, partial_min_messages=20,
            max_block_age=timedelta(hours=24), now=NOW,
        )
        self.assertEqual([job.message_count for job in jobs], [100])
        self.assertEqual(len(await self.store.get_joke_job_messages(jobs[0].job_id)), 100)
        claimed = await self.store.claim_due_joke_job(worker_id="test", now=NOW, lease_seconds=300)
        self.assertIsNotNone(claimed)
        result = await self.store.finalize_autonomous_joke_job(
            job_id=claimed.job_id, lease_token=claimed.lease_token or "", source_message_id=1,
            announce=True, bot_spin_decision="skip",
        )
        self.assertEqual(result.status, "awarded")
        self.assertEqual((await self.store.get_point_balance(chat_id=1, participant_key="id:10")).balance, 10)

    async def test_aged_partial_and_fewer_than_minimum_remain_open(self) -> None:
        for message_id in range(1, 20):
            await self.save(message_id, created_at=NOW - timedelta(days=2))
        self.assertEqual(await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(), block_messages=100, partial_min_messages=20,
            max_block_age=timedelta(hours=24), now=NOW + timedelta(days=2),
        ), [])
        await self.save(20, created_at=NOW - timedelta(days=2))
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute(
                "UPDATE chat_joke_inbox SET enqueued_at = ?",
                ((NOW - timedelta(days=2)).isoformat(),),
            )
            await db.commit()
        jobs = await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(), block_messages=100, partial_min_messages=20,
            max_block_age=timedelta(hours=24), now=NOW,
        )
        self.assertEqual([job.message_count for job in jobs], [20])

    async def test_lease_token_fences_stale_finalizer(self) -> None:
        await self.save(1)
        await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(), block_messages=1, partial_min_messages=1,
            max_block_age=timedelta(hours=24), now=NOW,
        )
        first = await self.store.claim_due_joke_job(worker_id="first", now=NOW, lease_seconds=1)
        second = await self.store.claim_due_joke_job(worker_id="second", now=NOW + timedelta(seconds=2), lease_seconds=300)
        stale = await self.store.finalize_autonomous_joke_job(
            job_id=first.job_id, lease_token=first.lease_token or "", source_message_id=1
        )
        self.assertEqual(stale.status, "lost_lease")
        self.assertEqual((await self.store.finalize_autonomous_joke_job(
            job_id=second.job_id, lease_token=second.lease_token or "", source_message_id=None
        )).status, "no_joke")

    async def test_terminal_finalize_replays_canonical_result_and_uses_saved_period(self) -> None:
        await self.save(1, created_at=NOW)
        await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(), block_messages=1, partial_min_messages=1,
            max_block_age=timedelta(hours=24), now=NOW,
        )
        claimed = await self.store.claim_due_joke_job(worker_id="test", now=NOW, lease_seconds=300)
        first = await self.store.finalize_autonomous_joke_job(
            job_id=claimed.job_id, lease_token=claimed.lease_token or "", source_message_id=1,
            bot_spin_decision="skip",
        )
        replay = await self.store.finalize_autonomous_joke_job(
            job_id=claimed.job_id, lease_token="stale", source_message_id=None,
        )
        self.assertEqual((first.status, replay.status), ("awarded", "awarded"))
        # Replacing the message cannot move the committed winner out of the original period.
        await self.store.save_message(
            chat_id=1, message_id=1, chat_type="group", sender_id=10, sender_name="Alice",
            text="replaced", created_at=NOW + timedelta(days=10), reply_to_message_id=None,
            origin="generated", kind="status", replace=True,
        )
        self.assertEqual(
            (await self.store.get_latest_autonomous_award(chat_id=1, since=NOW - timedelta(hours=1))).job_id,
            claimed.job_id,
        )

    async def test_automatic_insufficient_balance_is_terminal_with_outbox(self) -> None:
        await self.save(1)
        await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(), block_messages=1, partial_min_messages=1,
            max_block_age=timedelta(hours=24), now=NOW,
        )
        job = await self.store.claim_due_joke_job(worker_id="test", now=NOW, lease_seconds=300)
        await self.store.finalize_autonomous_joke_job(
            job_id=job.job_id, lease_token=job.lease_token or "", source_message_id=1,
            bot_spin_decision="spin",
        )
        async with aiosqlite.connect(self.store.database_path) as db:
            row = await (await db.execute(
                "SELECT outbox_id FROM chat_joke_outbox WHERE job_id=? AND action='bot_casino'", (job.job_id,)
            )).fetchone()
            await db.execute(
                """UPDATE chat_joke_outbox SET status='running', lease_token='lease'
                   WHERE outbox_id=?""", (row[0],)
            )
            await db.commit()
        result = await self.store.reserve_automatic_casino_spin(
            job_id=job.job_id, outbox_id=int(row[0]), lease_token="lease", chat_id=1,
            participant_key="id:999", participant_name="Bot", trigger=automatic_casino_trigger(job.job_id),
        )
        self.assertEqual(result.status, "insufficient_balance")
        async with aiosqlite.connect(self.store.database_path) as db:
            job_row = await (await db.execute(
                "SELECT bot_spin_decision FROM chat_joke_jobs WHERE job_id=?", (job.job_id,)
            )).fetchone()
            outbox_row = await (await db.execute(
                "SELECT status FROM chat_joke_outbox WHERE outbox_id=?", (row[0],)
            )).fetchone()
        self.assertEqual((job_row[0], outbox_row[0]), ("insufficient_balance", "skipped"))

    async def test_disabled_chat_is_not_planned(self) -> None:
        for message_id in range(1, 21):
            await self.save(message_id)
        jobs = await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(),
            disabled_chat_ids={1},
            block_messages=20,
            partial_min_messages=5,
            max_block_age=timedelta(hours=24),
            now=NOW,
        )
        self.assertEqual(jobs, [])

    async def test_activation_point_excludes_existing_history(self) -> None:
        for message_id in range(1, 21):
            await self.save(message_id, created_at=NOW - timedelta(days=30))
        start_after = await self.store.get_or_create_autonomous_joke_start_at(now=NOW)
        self.assertEqual(
            await self.store.get_or_create_autonomous_joke_start_at(now=NOW + timedelta(days=1)),
            start_after,
        )
        for message_id in range(21, 41):
            await self.save(message_id, created_at=NOW + timedelta(seconds=1))
        jobs = await self.store.plan_autonomous_joke_jobs(
            allowed_chat_ids=set(),
            start_after=start_after,
            block_messages=20,
            partial_min_messages=5,
            max_block_age=timedelta(hours=24),
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual([job.message_count for job in jobs], [20])
        self.assertEqual(
            [message.message_id for message in await self.store.get_joke_job_messages(jobs[0].job_id)],
            list(range(21, 41)),
        )
