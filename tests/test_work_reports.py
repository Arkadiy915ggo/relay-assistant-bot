import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from openai import AsyncOpenAI

from tg_summary_bot.config import load_settings
from tg_summary_bot.llm import build_llm_client
from tg_summary_bot.storage import MessageStore, StoredMessage
from tg_summary_bot.work_reports import WorkReportError, WorkReporter, WorkReportWorker, split_report_text


NOW = datetime(2026, 11, 13, 18, tzinfo=timezone.utc)


def settings(**changes):
    values = dict(work_reports_enabled=True, work_report_chat_ids={1}, allowed_chat_ids=set(),
                  work_report_chat_start_message_ids={},
                  work_report_min_messages=2, work_report_chat_min_messages={},
                  work_report_hour=18, work_report_timezone="UTC", work_report_max_messages=5000,
                  work_report_messages_per_document=500,
                  work_report_timeout_seconds=10, work_report_provider="openai")
    values.update(changes)
    return SimpleNamespace(**values)


class WorkReportWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MessageStore(self.root / "db.sqlite3")
        await self.store.init()
        self.bot = SimpleNamespace(send_document=AsyncMock())
        self.reporter = SimpleNamespace(summarize=AsyncMock(return_value="## Проект\n### Задачи\n- Проверить"),
                                        llm=SimpleNamespace(unload=AsyncMock()))
        self.lock = asyncio.Lock()
        self.clock = Mock(return_value=NOW)
        self.worker = self.make_worker()
        await self.worker.store.init()

    def make_worker(self, **changes):
        return WorkReportWorker(settings=settings(work_report_output_dir=self.root / "reports", **changes),
                                store=self.store, reporter=self.reporter, bot=self.bot, gpu_lock=self.lock,
                                clock=self.clock)

    async def tick(self, now):
        self.clock.return_value = now
        await self.worker.tick(now)

    async def save(self, mid, *, chat_id=1, origin="incoming", kind="text", at=None):
        await self.store.save_message(chat_id=chat_id, message_id=mid, chat_type="group",
                                      sender_id=7, sender_name="Участник", text=f"Задача {mid}",
                                      created_at=at or NOW - timedelta(minutes=1),
                                      reply_to_message_id=None, origin=origin, kind=kind)

    async def test_daily_report_file_delivery_restart_and_new_only(self):
        await self.save(1)
        await self.save(2)
        await self.tick(NOW)
        path = self.root / "reports/1/work-report-2026-11-13-2.md"
        self.assertIn("Рабочий отчет", path.read_text())
        self.assertIn("Сообщений: 2", path.read_text())
        self.assertEqual(self.bot.send_document.call_args.kwargs["chat_id"], 1)
        self.assertEqual(await self.worker.store.pending(1), None)
        self.worker = self.make_worker()
        await self.save(3, at=NOW + timedelta(hours=1))
        await self.tick(NOW + timedelta(hours=2))
        self.assertEqual(self.bot.send_document.await_count, 1)
        await self.save(4, at=NOW + timedelta(hours=2))
        await self.tick(NOW + timedelta(days=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [3, 4])

    async def test_topic_storage_names_are_chat_scoped_and_survive_replacement(self):
        for chat_id in (1, 2):
            await self.store.save_message(chat_id=chat_id, message_id=100, chat_type="supergroup",
                sender_id=7, sender_name="Name", text="source", created_at=NOW,
                reply_to_message_id=None, origin="incoming", kind="text", message_thread_id=10)
        await self.store.save_forum_topic(1, 10, "Art")
        await self.store.save_forum_topic(2, 10, "Sound")
        await self.store.save_forum_topic(1, 10, "Art renamed")
        await self.store.save_forum_topic(1, 10, "Art", overwrite=False)  # Old reply root after rename.
        await self.store.save_message(chat_id=1, message_id=100, chat_type="supergroup",
            sender_id=7, sender_name="Name", text="updated", created_at=NOW,
            reply_to_message_id=None, origin="incoming", kind="voice_transcript", replace=True)
        await self.store.init()  # Idempotent startup must retain topic metadata.
        for chat_id, expected in ((1, "Art renamed"), (2, "Sound")):
            state = await self.worker.store.state(chat_id, NOW)
            rows = await self.worker.store.messages(chat_id, state, NOW, 5000)
            self.assertEqual([(m.message_thread_id, m.topic_name) for m in rows], [(10, expected)])

    async def test_legacy_database_migrates_without_guessing_old_topics(self):
        path = self.root / "legacy.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, chat_type TEXT NOT NULL,
                sender_id INTEGER, sender_name TEXT NOT NULL, text TEXT NOT NULL,
                created_at TEXT NOT NULL, reply_to_message_id INTEGER,
                origin TEXT NOT NULL DEFAULT 'legacy', kind TEXT NOT NULL DEFAULT 'legacy_unclassified',
                PRIMARY KEY(chat_id,message_id))""")
            db.execute("INSERT INTO messages VALUES (1,1,'supergroup',7,'Name','old',?,NULL,'incoming','text')",
                       (NOW.isoformat(),))
        migrated = MessageStore(path)
        await migrated.init()
        from tg_summary_bot.work_reports import WorkReportStore
        reports = WorkReportStore(migrated)
        await reports.init()
        rows = await reports.messages(1, await reports.state(1, NOW), NOW, 5000)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].message_thread_id)
        self.assertIsNone(rows[0].topic_name)

    async def test_skipped_messages_accumulate_but_same_day_is_not_rechecked(self):
        await self.save(1)
        await self.tick(NOW)
        await self.save(2)
        await self.tick(NOW + timedelta(minutes=1))
        self.reporter.summarize.assert_not_awaited()
        await self.tick(NOW + timedelta(days=2))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [1, 2])

    async def test_upload_failure_retries_cached_report_after_restart(self):
        await self.save(1)
        await self.save(2)
        self.bot.send_document.side_effect = RuntimeError("fake failure")
        await self.tick(NOW)
        self.assertIsNotNone(await self.worker.store.pending(1))
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 0)
        self.worker = self.make_worker()
        await self.tick(NOW + timedelta(minutes=30))
        self.assertEqual(self.bot.send_document.await_count, 1)
        self.bot.send_document.side_effect = None
        await self.tick(NOW + timedelta(hours=1))
        self.reporter.summarize.assert_awaited_once()
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 2)

    async def test_access_policy_pauses_pending_uploads(self):
        await self.save(1)
        await self.save(2)
        self.bot.send_document.side_effect = RuntimeError()
        await self.tick(NOW)
        self.bot.send_document.reset_mock()
        self.worker = self.make_worker(allowed_chat_ids={2})
        await self.tick(NOW + timedelta(hours=2))
        self.bot.send_document.assert_not_awaited()
        self.assertIsNotNone(await self.worker.store.pending(1))

    async def test_older_delivery_does_not_consume_today_generation_after_restart(self):
        await self.save(1)
        await self.save(2)
        self.bot.send_document.side_effect = RuntimeError("uncertain upload")
        await self.tick(NOW)
        tomorrow_morning = NOW + timedelta(hours=14)  # 08:00 the next day
        self.worker = self.make_worker()
        self.bot.send_document.side_effect = None
        await self.tick(tomorrow_morning)
        state = await self.worker.store.state(1, tomorrow_morning)
        self.assertEqual(state["cursor_id"], 2)
        self.assertEqual(state["checked_day"], "2026-11-13")
        await self.save(3, at=tomorrow_morning)
        await self.save(4, at=tomorrow_morning)
        self.worker = self.make_worker()
        await self.tick(NOW + timedelta(days=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [3, 4])
        self.assertEqual(self.reporter.summarize.await_count, 2)
        await self.tick(NOW + timedelta(days=1, minutes=1))
        self.assertEqual(self.reporter.summarize.await_count, 2)

    async def test_slow_generation_and_file_write_use_actual_attempt_time(self):
        await self.save(1)
        await self.save(2)
        async def slow_generation(messages):
            self.clock.return_value = NOW + timedelta(minutes=25)
            return "## Отчет"
        self.reporter.summarize.side_effect = slow_generation
        self.bot.send_document.side_effect = RuntimeError("uncertain upload")
        from tg_summary_bot.work_reports import save_report
        def slow_write(path, markdown):
            save_report(path, markdown)
            self.clock.return_value = NOW + timedelta(minutes=35)
        with patch("tg_summary_bot.work_reports.save_report", side_effect=slow_write):
            await self.tick(NOW)
        pending = await self.worker.store.pending(1)
        self.assertEqual(datetime.fromisoformat(pending["last_attempt"]), NOW + timedelta(minutes=35))
        self.worker = self.make_worker()
        await self.tick(NOW + timedelta(minutes=94))
        self.assertEqual(self.bot.send_document.await_count, 1)
        await self.tick(NOW + timedelta(minutes=95))
        self.assertEqual(self.bot.send_document.await_count, 2)
        self.reporter.summarize.assert_awaited_once()

    async def test_retry_eligibility_uses_fresh_clock_not_tick_snapshot(self):
        await self.save(1)
        await self.save(2)
        self.bot.send_document.side_effect = RuntimeError("uncertain upload")
        await self.tick(NOW)
        self.clock.return_value = NOW + timedelta(hours=1)
        # The tick snapshot is stale, as it can be after processing slow chats.
        await self.worker.tick(NOW + timedelta(minutes=30))
        self.assertEqual(self.bot.send_document.await_count, 2)
        pending = await self.worker.store.pending(1)
        self.assertEqual(datetime.fromisoformat(pending["last_attempt"]), NOW + timedelta(hours=1))

    async def test_multiple_slow_chats_get_independent_fresh_attempt_timestamps(self):
        for chat_id in (1, 2):
            await self.save(1, chat_id=chat_id)
            await self.save(2, chat_id=chat_id)
        async def slow_generation(messages):
            self.clock.return_value += timedelta(minutes=40)
            return "## Отчет"
        self.reporter.summarize.side_effect = slow_generation
        self.bot.send_document.side_effect = RuntimeError("uncertain upload")
        self.worker = self.make_worker(work_report_chat_ids={1, 2})
        await self.tick(NOW)
        for chat_id, minutes in ((1, 40), (2, 80)):
            pending = await self.worker.store.pending(chat_id)
            self.assertEqual(datetime.fromisoformat(pending["last_attempt"]),
                             NOW + timedelta(minutes=minutes))
        await self.tick(NOW + timedelta(minutes=81))
        self.assertEqual(self.bot.send_document.await_count, 2)

    async def test_chat_selection_threshold_overrides_and_provenance(self):
        for mid in (1, 2):
            await self.save(mid, chat_id=2)
        await self.save(1, origin="generated")
        await self.save(2, origin="assistant", kind="assistant_answer")
        await self.save(3, origin="legacy", kind="legacy_unclassified")
        await self.save(4)
        await self.tick(NOW)
        self.reporter.summarize.assert_not_awaited()
        self.worker = self.make_worker(work_report_chat_ids={2}, work_report_min_messages=3,
                                       work_report_chat_min_messages={2: 2})
        await self.tick(NOW)
        self.assertEqual(self.bot.send_document.call_args.kwargs["chat_id"], 2)

    async def test_schedule_timezone_disabled_and_initial_lookback(self):
        await self.save(1, at=NOW - timedelta(days=2))
        await self.save(2)
        self.worker = self.make_worker(work_report_hour=22, work_report_timezone="Europe/Moscow")
        await self.tick(NOW)  # 21:00 local, too early
        self.reporter.summarize.assert_not_awaited()
        await self.tick(NOW + timedelta(hours=1))
        self.reporter.summarize.assert_not_awaited()  # old row does not count
        await self.save(3)
        self.worker = self.make_worker(work_reports_enabled=False)
        await self.tick(NOW + timedelta(days=1))
        self.reporter.summarize.assert_not_awaited()

    async def test_bounded_batch_carries_over_remaining_same_timestamp_messages(self):
        for mid in range(1, 6):
            await self.save(mid)
        self.worker = self.make_worker(work_report_max_messages=2)
        await self.tick(NOW)
        await self.tick(NOW + timedelta(days=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [3, 4])
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 4)

    async def test_explicit_start_is_inclusive_and_includes_old_saved_history(self):
        for mid in (4543, 4544, 4545):
            await self.save(mid, at=NOW - timedelta(days=10))
        self.worker = self.make_worker(work_report_chat_start_message_ids={1: 4544})
        await self.tick(NOW)
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [4544, 4545])
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 4545)
        await self.save(4546, at=NOW - timedelta(days=9))
        await self.save(4547, at=NOW - timedelta(days=9))
        self.worker = self.make_worker(work_report_chat_start_message_ids={1: 4544})
        await self.tick(NOW + timedelta(days=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [4546, 4547])

    async def test_daily_range_splits_across_restart_including_short_tail(self):
        for mid in range(1, 6):
            await self.save(mid)
        self.worker = self.make_worker(work_report_messages_per_document=2)
        await self.tick(NOW)
        state = await self.worker.store.state(1, NOW)
        self.assertEqual((state["cursor_id"], state["batch_end_id"]), (2, 5))
        await self.save(6)  # Arrived after the daily range was admitted.
        self.worker = self.make_worker(work_report_messages_per_document=2)
        await self.tick(NOW + timedelta(minutes=1))
        await self.tick(NOW + timedelta(minutes=2))
        self.assertEqual([[m.message_id for m in call.args[0]] for call in
                          self.reporter.summarize.call_args_list], [[1, 2], [3, 4], [5]])
        self.assertEqual(self.bot.send_document.await_count, 3)
        state = await self.worker.store.state(1, NOW)
        self.assertEqual((state["cursor_id"], state["batch_end_id"]), (5, None))
        await self.tick(NOW + timedelta(minutes=3))
        self.assertEqual(self.reporter.summarize.await_count, 3)

    async def test_split_delivery_retry_does_not_regenerate_or_skip_next_document(self):
        for mid in range(1, 6):
            await self.save(mid)
        self.worker = self.make_worker(work_report_messages_per_document=2)
        self.bot.send_document.side_effect = RuntimeError("uncertain upload")
        await self.tick(NOW)
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 0)
        self.worker = self.make_worker(work_report_messages_per_document=2)
        self.bot.send_document.side_effect = None
        await self.tick(NOW + timedelta(hours=1))
        self.reporter.summarize.assert_awaited_once()
        await self.tick(NOW + timedelta(hours=1, minutes=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [3, 4])
        await self.tick(NOW + timedelta(hours=1, minutes=2))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [5])

    async def test_failed_split_generation_waits_until_next_day_at_same_cursor(self):
        for mid in range(1, 6):
            await self.save(mid)
        self.worker = self.make_worker(work_report_messages_per_document=2)
        await self.tick(NOW)
        self.reporter.summarize.side_effect = RuntimeError("model failed")
        await self.tick(NOW + timedelta(minutes=1))
        self.worker = self.make_worker(work_report_messages_per_document=2)
        await self.tick(NOW + timedelta(minutes=2))
        self.assertEqual(self.reporter.summarize.await_count, 2)
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 2)
        self.reporter.summarize.side_effect = None
        await self.tick(NOW + timedelta(days=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [3, 4])
        await self.tick(NOW + timedelta(days=1, minutes=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [5])

    async def test_old_report_schema_preserves_pending_upload_and_cursor(self):
        from tg_summary_bot.work_reports import WorkReportStore
        path = self.root / "old-reports.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE work_report_state(chat_id INTEGER PRIMARY KEY, since TEXT NOT NULL,
                    cursor_id INTEGER NOT NULL DEFAULT 0, checked_day TEXT NOT NULL DEFAULT '');
                CREATE TABLE work_report_outbox(chat_id INTEGER PRIMARY KEY, report_day TEXT NOT NULL,
                    end_id INTEGER NOT NULL, markdown TEXT NOT NULL, last_attempt TEXT);
                INSERT INTO work_report_state VALUES(1,'1970-01-01T00:00:00+00:00',100,'2026-11-13');
                INSERT INTO work_report_outbox VALUES(1,'2026-11-13',200,'cached',NULL);
            """)
        reports = WorkReportStore(SimpleNamespace(database_path=path))
        await reports.init()
        await reports.init()
        state = await reports.state(1, NOW)
        self.assertEqual((state["cursor_id"], state["checked_day"], state["batch_end_id"]),
                         (100, "2026-11-13", None))
        self.assertEqual((await reports.pending(1))["markdown"], "cached")

    async def test_explicit_start_accumulates_and_pending_retry_does_not_overlap(self):
        self.worker = self.make_worker(work_report_chat_start_message_ids={1: 100})
        await self.save(99, at=NOW - timedelta(days=10))
        await self.save(100, at=NOW - timedelta(days=10))
        await self.tick(NOW)
        self.reporter.summarize.assert_not_awaited()
        await self.save(101)
        await self.tick(NOW + timedelta(minutes=1))
        self.reporter.summarize.assert_not_awaited()
        self.bot.send_document.side_effect = RuntimeError("upload failed")
        await self.tick(NOW + timedelta(days=1))
        self.assertEqual([m.message_id for m in self.reporter.summarize.call_args.args[0]], [100, 101])
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 99)
        self.worker = self.make_worker(work_report_chat_start_message_ids={1: 100})
        self.bot.send_document.side_effect = None
        await self.tick(NOW + timedelta(days=1, hours=1))
        self.reporter.summarize.assert_awaited_once()
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 101)

    async def test_start_is_chat_scoped_and_does_not_reset_existing_state(self):
        state = await self.worker.store.state(1, NOW, start_message_id=100)
        self.assertEqual(state["cursor_id"], 99)
        await self.worker.store.checked(1, "2026-11-13")
        await self.worker.store.delivered(1, 200)
        updated = await self.worker.store.state(1, NOW, start_message_id=300)
        self.assertEqual(updated["cursor_id"], 200)
        self.assertEqual(updated["checked_day"], "2026-11-13")
        other = await self.worker.store.state(2, NOW)
        self.assertEqual(other["cursor_id"], 0)
        self.assertEqual(other["since"], (NOW - timedelta(days=1)).isoformat())

    async def test_local_provider_uses_shared_gpu_lock_and_unloads_on_failure(self):
        await self.save(1)
        await self.save(2)
        async def fail(messages):
            self.assertTrue(self.lock.locked())
            raise RuntimeError("fake model failure")
        self.reporter.summarize.side_effect = fail
        self.worker = self.make_worker(work_report_provider="ollama")
        await self.tick(NOW)
        self.reporter.llm.unload.assert_awaited_once()
        self.assertFalse(self.lock.locked())
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 0)
        await self.tick(NOW + timedelta(minutes=1))
        self.reporter.summarize.assert_awaited_once()

    async def test_remote_model_does_not_hold_gpu_lock(self):
        await self.save(1)
        await self.save(2)
        async with self.lock:
            await self.tick(NOW)
        self.bot.send_document.assert_awaited_once()

    async def test_timeout_preserves_cursor_and_does_not_block_other_chats(self):
        for chat_id in (1, 2):
            await self.save(1, chat_id=chat_id)
            await self.save(2, chat_id=chat_id)
        async def summarize(messages):
            if messages[0].chat_id == 1:
                await asyncio.Event().wait()
            return "## Отчет"
        self.reporter.summarize.side_effect = summarize
        self.worker = self.make_worker(work_report_chat_ids={1, 2}, work_report_timeout_seconds=0.5)
        await self.tick(NOW)
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 0)
        self.assertEqual(self.bot.send_document.call_args.kwargs["chat_id"], 2)

    async def test_local_write_failure_keeps_outbox_without_upload(self):
        await self.save(1)
        await self.save(2)
        with patch("tg_summary_bot.work_reports.save_report", side_effect=OSError()):
            await self.tick(NOW)
        self.bot.send_document.assert_not_awaited()
        self.assertIsNotNone(await self.worker.store.pending(1))
        await self.tick(NOW + timedelta(minutes=1))
        self.bot.send_document.assert_awaited_once()
        self.reporter.summarize.assert_awaited_once()


    async def test_compression_failure_preserves_checkpoint_and_has_safe_reason(self):
        await self.save(1)
        await self.save(2)
        self.reporter.summarize.side_effect = WorkReportError("notes_budget_exceeded")
        with self.assertLogs(level="WARNING") as logs:
            await self.tick(NOW)
        self.assertIn("reason=notes_budget_exceeded", "\n".join(logs.output))
        self.assertEqual((await self.worker.store.state(1, NOW))["cursor_id"], 0)
        self.assertIsNone(await self.worker.store.pending(1))
        self.bot.send_document.assert_not_awaited()


class ReporterTests(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_notes_retry_original_source_then_render_final_report(self):
        from tg_summary_bot.work_reports import WORK_NOTES_PROMPT, WORK_REPORT_PROMPT
        llm = SimpleNamespace(complete=AsyncMock(side_effect=[
            "x" * 1500, "Задача | проверить #1", "Решение | принято #1", "### Итог\nПроверить #1",
        ]))
        message = SimpleNamespace(created_at="date", message_id=1, reply_to_message_id=None,
                                  sender_name="Name", text="SOURCE " * 200)
        result = await WorkReporter(llm, 1000).summarize([message])
        self.assertIn("### Итог", result)
        calls = llm.complete.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0].kwargs["system"], WORK_NOTES_PROMPT)
        self.assertEqual(calls[1].kwargs["system"], WORK_NOTES_PROMPT)
        original = calls[0].kwargs["user"].split("<source>\n")[1]
        self.assertEqual(original, calls[1].kwargs["user"].split("<source>\n")[1])
        self.assertIn("Предыдущая попытка", calls[1].kwargs["user"])
        self.assertEqual(calls[-1].kwargs["system"], WORK_REPORT_PROMPT)

    async def test_persistent_oversize_is_bounded_and_never_returns_clipped_notes(self):
        llm = SimpleNamespace(complete=AsyncMock(return_value="x" * 18000))
        reporter = WorkReporter(llm, 18000)
        with self.assertRaises(WorkReportError) as error:
            await reporter._notes("source " * 2500)
        self.assertEqual(error.exception.reason, "notes_budget_exceeded")
        self.assertEqual(llm.complete.await_count, 2)

    async def test_many_chunks_converge_with_compact_notes(self):
        from tg_summary_bot.work_reports import WORK_NOTES_PROMPT
        async def complete(*, system, user):
            return "факт #1; " * 20 if system == WORK_NOTES_PROMPT else "### Итог\nРешение #1"
        llm = SimpleNamespace(complete=AsyncMock(side_effect=complete))
        message = SimpleNamespace(created_at="date", message_id=1, reply_to_message_id=None,
                                  sender_name="Name", text="a" * 12500)
        result = await WorkReporter(llm, 1000).summarize([message])
        self.assertIn("### Итог", result)
        # 13 original chunks -> 3 chunks of notes -> one final report.
        self.assertEqual(llm.complete.await_count, 17)
        for call in llm.complete.call_args_list:
            source = call.kwargs["user"].split("<source>\n")[1].rsplit("\n</source>", 1)[0]
            self.assertLessEqual(len(source), 1000)

    async def test_topics_are_summarized_independently_with_deterministic_headings(self):
        llm = SimpleNamespace(complete=AsyncMock(return_value="### Задачи\n- Проверить"))
        messages = [StoredMessage(mid, 1, "supergroup", 7, "Name", text, NOW.isoformat(), None,
                                  "incoming", "text", thread, name)
                    for mid, text, thread, name in (
                        (1, "ART_ONLY", 10, "Art"), (2, "SOUND_ONLY", 20, "Sound"),
                        (3, "ART_AGAIN", 10, "Art"), (4, "UNKNOWN_ONLY", None, None),
                        (5, "GENERAL_ONLY", 1, None), (6, "UNNAMED_ONLY", 30, None))]
        result = await WorkReporter(llm, 18000).summarize(messages)
        self.assertEqual(llm.complete.await_count, 5)
        calls = [call.kwargs["user"] for call in llm.complete.call_args_list]
        self.assertIn("ART_ONLY", calls[0])
        self.assertIn("ART_AGAIN", calls[0])
        self.assertNotIn("SOUND_ONLY", calls[0])
        self.assertIn("SOUND_ONLY", calls[1])
        self.assertNotIn("ART_ONLY", calls[1])
        for heading in ("## Art", "## Sound", "## Общий чат / тема не сохранена", "## General", "## Тема #30"):
            self.assertIn(heading.replace("#30", "\\#30"), result)

    async def test_topic_names_from_service_events_reply_roots_and_general(self):
        from tg_summary_bot.bot import save_message_topic
        store = SimpleNamespace(save_forum_topic=AsyncMock())
        chat = SimpleNamespace(id=1, is_forum=True)
        created = SimpleNamespace(chat=chat, message_id=10, forum_topic_created=SimpleNamespace(name="Art"))
        self.assertEqual(await save_message_topic(store, created), 10)
        store.save_forum_topic.assert_awaited_with(1, 10, "Art", overwrite=True)
        reply = SimpleNamespace(chat=chat, message_thread_id=10, reply_to_message=created)
        self.assertEqual(await save_message_topic(store, reply), 10)
        store.save_forum_topic.assert_awaited_with(1, 10, "Art", overwrite=False)
        edited = SimpleNamespace(chat=chat, message_thread_id=10, forum_topic_edited=SimpleNamespace(name="New Art"))
        await save_message_topic(store, edited)
        store.save_forum_topic.assert_awaited_with(1, 10, "New Art", overwrite=True)
        self.assertEqual(await save_message_topic(store, SimpleNamespace(chat=chat)), 1)
        self.assertIsNone(await save_message_topic(store, SimpleNamespace(chat=SimpleNamespace(id=2))))

    async def test_model_diagnostics_report_safe_reason_and_usage_without_content(self):
        for finish, output, reason in (
            ("length", "PRIVATE_RESPONSE", "output_truncated"),
            ("content_filter", "PRIVATE_RESPONSE", "output_filtered"),
            ("stop", "", "empty_output"),
        ):
            with self.subTest(finish=finish):
                llm = SimpleNamespace(complete=AsyncMock(return_value=output),
                                      last_metadata={"finish_reason": finish},
                                      last_usage={"prompt_tokens": 123, "completion_tokens": 4096},
                                      max_output_tokens=4096)
                reporter = WorkReporter(llm, 1000)
                with self.assertLogs(level="INFO") as logs, self.assertRaises(WorkReportError) as error:
                    await reporter._complete("PRIVATE_SOURCE", partial=True)
                self.assertEqual(error.exception.reason, reason)
                rendered = "\n".join(logs.output)
                self.assertIn(f"finish_reason={finish}", rendered)
                self.assertIn("completion_tokens=4096", rendered)
                self.assertIn("max_output_tokens=4096", rendered)
                self.assertNotIn("PRIVATE_", rendered)

    async def test_request_failure_diagnostics_do_not_log_exception_text(self):
        llm = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError("PRIVATE_KEY")))
        with self.assertLogs(level="INFO") as logs, self.assertRaises(RuntimeError):
            await WorkReporter(llm, 1000)._complete("PRIVATE_SOURCE", partial=False)
        rendered = "\n".join(logs.output)
        self.assertIn("stage=final error_type=RuntimeError", rendered)
        self.assertNotIn("PRIVATE_", rendered)

    async def test_truncated_output_is_rejected(self):
        llm = SimpleNamespace(complete=AsyncMock(return_value="## Incomplete"),
                              last_metadata={"finish_reason": "length"})
        reporter = WorkReporter(llm, 1000)
        message = SimpleNamespace(created_at="date", message_id=1, reply_to_message_id=None,
                                  sender_name="Имя", text="test")
        with self.assertRaises(RuntimeError):
            await reporter.summarize([message])

    async def test_go_chat_request_through_fake_http_only(self):
        import json
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={
                "id": "fake", "object": "chat.completion", "created": 0,
                "model": "glm-5.3-flash", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "## Отчет"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake", "OPENCODE_GO_API_KEY": "fake-go"}, clear=True), \
             patch("tg_summary_bot.config.load_dotenv"):
            config = load_settings()
        client = build_llm_client(config, provider="opencode_go",
                                  max_output_tokens=config.work_report_max_output_tokens)
        headers = dict(client.client.default_headers)
        await client.client.close()
        client.client = AsyncOpenAI(api_key="fake-go", base_url=config.opencode_go_base_url,
                                    default_headers=headers,
                                    http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        try:
            for _ in range(2):
                self.assertEqual(await client.complete(system="system", user="source"), "## Отчет")
        finally:
            await client.client.close()
        self.assertEqual(str(seen[0].url), config.opencode_go_base_url + "/chat/completions")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer fake-go")
        self.assertEqual(seen[0].headers["x-opencode-session"], seen[1].headers["x-opencode-session"])
        payload = json.loads(seen[0].content)
        self.assertEqual(payload["model"], "glm-5.3-flash")
        self.assertEqual(payload["max_tokens"], 32768)
        self.assertEqual(client.last_metadata["finish_reason"], "stop")

    async def test_chunk_and_merge_requests_are_bounded(self):
        llm = SimpleNamespace(complete=AsyncMock(return_value="## Тема\n- Решение #1"))
        reporter = WorkReporter(llm, 1000)
        message = SimpleNamespace(created_at="date", message_id=1, reply_to_message_id=None,
                                  sender_name="Имя", text="x" * 3500)
        result = await reporter.summarize([message])
        self.assertIn("Тема", result)
        self.assertEqual(llm.complete.await_count, 5)
        for call in llm.complete.call_args_list:
            self.assertLessEqual(len(call.kwargs["user"]), 1300)
            self.assertIn("нерабочие", call.kwargs["system"])

    async def test_empty_or_noncompressing_response_is_not_delivered(self):
        llm = SimpleNamespace(complete=AsyncMock(return_value=""))
        reporter = WorkReporter(llm, 1000)
        message = SimpleNamespace(created_at="date", message_id=1, reply_to_message_id=None,
                                  sender_name="Имя", text="test")
        with self.assertRaises(RuntimeError):
            await reporter.summarize([message])
        message.text = "x" * 3000
        llm.complete.return_value = "y" * 2000
        with self.assertRaises(RuntimeError):
            await reporter.summarize([message])
        self.assertTrue(all(len(chunk) <= 1000 for chunk in split_report_text("x" * 5001, 1000)))


class WorkReportConfigTests(unittest.TestCase):
    def load(self, **env):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fake", **env}, clear=True), \
             patch("tg_summary_bot.config.load_dotenv"):
            return load_settings()  # Never read the real .env.

    def test_defaults_and_chat_overrides(self):
        config = self.load(WORK_REPORT_CHAT_IDS="-100,2", WORK_REPORT_CHAT_MIN_MESSAGES='{"-100":100}')
        self.assertFalse(config.work_reports_enabled)
        self.assertEqual(config.work_report_chat_ids, {-100, 2})
        self.assertEqual(config.work_report_chat_min_messages, {-100: 100})
        self.assertEqual(config.work_report_min_messages, 200)
        self.assertEqual(config.work_report_max_output_tokens, 32768)
        self.assertEqual(config.work_report_messages_per_document, 500)
        self.assertEqual(self.load(WORK_REPORT_MESSAGES_PER_DOCUMENT="400").work_report_messages_per_document, 400)

    def test_report_token_budget_is_independent_and_positive(self):
        config = self.load(WORK_REPORT_MAX_OUTPUT_TOKENS="8192", OLLAMA_NUM_PREDICT="512")
        self.assertEqual(config.work_report_max_output_tokens, 8192)
        self.assertEqual(config.ollama_num_predict, 512)
        report = build_llm_client(config, provider="ollama", num_predict=config.work_report_max_output_tokens)
        ordinary = build_llm_client(config, provider="ollama")
        self.assertEqual(getattr(report, "wrapped", report).num_predict, 8192)
        self.assertEqual(getattr(ordinary, "wrapped", ordinary).num_predict, 512)
        for value in ("0", "-1"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.load(WORK_REPORT_MAX_OUTPUT_TOKENS=value)
        self.assertEqual(config.work_report_chat_start_message_ids, {})

    def test_explicit_start_message_ids(self):
        config = self.load(WORK_REPORT_CHAT_START_MESSAGE_IDS='{"-1004361551668":4544,"2":1}')
        self.assertEqual(config.work_report_chat_start_message_ids, {-1004361551668: 4544, 2: 1})
        for value in ('[]', '{"1":true}', '{"1":0}', '{"1":-1}', '{"1":"4544"}',
                      '{"bad":1}', '{"1":9223372036854775808}', 'invalid'):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                self.load(WORK_REPORT_CHAT_START_MESSAGE_IDS=value)

    def test_invalid_limits_and_provider_are_rejected(self):
        for env in ({"WORK_REPORT_HOUR": "24"}, {"WORK_REPORT_MIN_MESSAGES": "0"},
                    {"WORK_REPORT_MESSAGES_PER_DOCUMENT": "0"},
                    {"WORK_REPORT_MAX_MESSAGES": "100"}, {"WORK_REPORT_PROVIDER": "unknown"},
                    {"WORK_REPORT_CHAT_MIN_MESSAGES": "[]"},
                    {"WORK_REPORT_CHAT_MIN_MESSAGES": '{"1":true}'},
                    {"WORK_REPORT_CHAT_MIN_MESSAGES": '{"1":-1}'},
                    {"WORK_REPORT_CHUNK_CHARS": "1"}):
            with self.subTest(env=env), self.assertRaises(RuntimeError):
                self.load(**env)

    def test_go_factory_endpoint_model_key_and_session_headers(self):
        config = self.load(OPENCODE_GO_API_KEY="fake-go-key")
        with patch("tg_summary_bot.llm.AsyncOpenAI") as sdk:
            client = build_llm_client(config, provider="opencode_go")
            self.assertEqual(client.model, "glm-5.3-flash")
            args = sdk.call_args.kwargs
            self.assertEqual(args["base_url"], "https://opencode.ai/inference/go/openai/v1")
            self.assertEqual(args["api_key"], "fake-go-key")
            self.assertIn("relay-assistant-bot", args["default_headers"]["User-Agent"])
            self.assertTrue(args["default_headers"]["x-opencode-session"])
        with self.assertRaises(RuntimeError):
            build_llm_client(self.load(), provider="opencode_go")
