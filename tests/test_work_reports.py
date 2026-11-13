import asyncio
import os
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
from tg_summary_bot.storage import MessageStore
from tg_summary_bot.work_reports import WorkReporter, WorkReportWorker, split_report_text


NOW = datetime(2026, 11, 13, 18, tzinfo=timezone.utc)


def settings(**changes):
    values = dict(work_reports_enabled=True, work_report_chat_ids={1}, allowed_chat_ids=set(),
                  work_report_min_messages=2, work_report_chat_min_messages={},
                  work_report_hour=18, work_report_timezone="UTC", work_report_max_messages=5000,
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


class ReporterTests(unittest.IsolatedAsyncioTestCase):
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
        client = build_llm_client(config, provider="opencode_go", max_output_tokens=4096)
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
        self.assertEqual(payload["max_tokens"], 4096)
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
            self.assertLessEqual(len(call.kwargs["user"]), 1200)
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

    def test_invalid_limits_and_provider_are_rejected(self):
        for env in ({"WORK_REPORT_HOUR": "24"}, {"WORK_REPORT_MIN_MESSAGES": "0"},
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
