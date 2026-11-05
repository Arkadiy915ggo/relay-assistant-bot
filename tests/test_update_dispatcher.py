import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from tg_summary_bot.telegram_delivery import TelegramDeliveryMiddleware
from tg_summary_bot.update_dispatcher import ChatOrderedDispatcher, POLLING_CONCURRENCY_LIMIT


def update(chat_id, message_id):
    return SimpleNamespace(message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=message_id), channel_post=None)


def bot():
    return SimpleNamespace(id=1, me=AsyncMock(return_value=SimpleNamespace(username="test", full_name="Test")))


class UpdateDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_polling_keeps_other_chat_moving_during_retry_and_preserves_chat_order(self):
        dp = ChatOrderedDispatcher()
        delivery = TelegramDeliveryMiddleware(interval=0)
        blocked, release, other_done, all_done, exit_polling = [asyncio.Event() for _ in range(5)]
        started, completed = [], []
        rejected = False

        async def updates(*args, **kwargs):
            for item in (update(-1, 1), update(-1, 2), update(-2, 3)):
                yield item
            await exit_polling.wait()

        async def request(bot, method):
            nonlocal rejected
            if method.chat_id == -1 and not rejected:
                rejected = True
                raise TelegramRetryAfter(method=method, message="flood", retry_after=33)

        async def process(self, bot, update, **kwargs):
            message = update.message
            started.append(message.message_id)
            await delivery(request, bot, SendMessage(chat_id=message.chat.id, text="report"))
            completed.append(message.message_id)
            if message.chat.id == -2:
                other_done.set()
            if len(completed) == 3:
                all_done.set()

        async def sleep(delay):
            blocked.set()
            await release.wait()

        with patch.object(dp, "_listen_updates", new=updates), patch.object(Dispatcher, "_process_update", new=process), patch(
            "tg_summary_bot.telegram_delivery.asyncio.sleep", new=sleep
        ), self.assertLogs(level="WARNING"):
            polling = asyncio.create_task(dp._polling(bot(), handle_as_tasks=True, tasks_concurrency_limit=POLLING_CONCURRENCY_LIMIT))
            try:
                await asyncio.wait_for(blocked.wait(), 1)
                await asyncio.wait_for(other_done.wait(), 1)
                self.assertEqual(started, [1, 3])
                self.assertEqual(completed, [3])
                release.set()
                await asyncio.wait_for(all_done.wait(), 1)
                self.assertEqual(completed, [3, 1, 2])
            finally:
                release.set()
                exit_polling.set()
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                await dp.emit_shutdown()

    async def test_polling_bounds_active_updates_and_shutdown_waits_for_cleanup(self):
        dp = ChatOrderedDispatcher()
        full, cleanup_started, cleanup_release, exit_polling = [asyncio.Event() for _ in range(4)]
        started, cleaned = [], []

        async def updates(*args, **kwargs):
            for number in range(5):
                yield update(number, number)
            await exit_polling.wait()

        async def process(self, bot, update, **kwargs):
            number = update.message.message_id
            started.append(number)
            if len(started) == 2:
                full.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.append(number)

        with patch.object(dp, "_listen_updates", new=updates), patch.object(Dispatcher, "_process_update", new=process):
            polling = asyncio.create_task(dp._polling(bot(), handle_as_tasks=True, tasks_concurrency_limit=2))
            shutdown = None
            try:
                await asyncio.wait_for(full.wait(), 1)
                self.assertEqual(started, [0, 1])
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                shutdown = asyncio.create_task(dp.emit_shutdown())
                await asyncio.wait_for(cleanup_started.wait(), 1)
                self.assertFalse(shutdown.done())
                cleanup_release.set()
                await asyncio.wait_for(shutdown, 1)
                self.assertCountEqual(cleaned, [0, 1])
                self.assertTrue(all(task.done() for task in dp._handle_update_tasks))
            finally:
                cleanup_release.set()
                exit_polling.set()
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                if shutdown:
                    await shutdown
                await dp.emit_shutdown()
