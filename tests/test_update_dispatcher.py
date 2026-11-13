import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Update

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

    async def test_polling_bounds_active_updates_and_shutdown_drains_all_handlers(self):
        dp = ChatOrderedDispatcher()
        full, cleanup_release, exit_polling = [asyncio.Event() for _ in range(3)]
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
                await cleanup_release.wait()
            finally:
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
                self.assertFalse(shutdown.done())
                self.assertEqual(cleaned, [])
                cleanup_release.set()
                await asyncio.wait_for(shutdown, 1)
                self.assertCountEqual(cleaned, [0, 1, 2, 3, 4])
                self.assertTrue(all(task.done() for task in dp._handle_update_tasks))
            finally:
                cleanup_release.set()
                exit_polling.set()
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                if shutdown:
                    await shutdown
                await dp.emit_shutdown()

    async def test_saturated_same_chat_backlog_leaves_capacity_for_other_chat(self):
        dp = ChatOrderedDispatcher()
        blocked, release, other_done, exit_polling = [asyncio.Event() for _ in range(4)]
        started, completed = [], []

        async def updates(*args, **kwargs):
            for number in range(POLLING_CONCURRENCY_LIMIT):
                yield update(-1, number)
            yield update(-2, POLLING_CONCURRENCY_LIMIT)
            await exit_polling.wait()

        async def process(self, bot, update, **kwargs):
            message = update.message
            started.append(message.message_id)
            if message.chat.id == -1:
                blocked.set()
                await release.wait()
            else:
                other_done.set()
            completed.append(message.message_id)

        with patch.object(dp, "_listen_updates", new=updates), patch.object(Dispatcher, "_process_update", new=process):
            polling = asyncio.create_task(dp._polling(bot()))
            try:
                await asyncio.wait_for(blocked.wait(), 1)
                await asyncio.wait_for(other_done.wait(), 1)
                self.assertEqual(started, [0, POLLING_CONCURRENCY_LIMIT])
                self.assertEqual(completed, [POLLING_CONCURRENCY_LIMIT])
            finally:
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                release.set()
                await dp.emit_shutdown()
            self.assertEqual(completed[1:], list(range(POLLING_CONCURRENCY_LIMIT)))

    async def test_acknowledged_same_chat_updates_drain_before_session_closes(self):
        acknowledged, blocked, release, draining = [asyncio.Event() for _ in range(4)]

        class ObservedDispatcher(ChatOrderedDispatcher):
            async def _drain_pending_updates(self, **kwargs):
                draining.set()
                await super()._drain_pending_updates(**kwargs)

        dp = ObservedDispatcher()
        started, completed, offsets = [], [], []
        transport = bot()
        transport.session = SimpleNamespace(timeout=0, close=AsyncMock())

        class FakeTransport:
            id = transport.id
            me = transport.me
            session = transport.session

            async def __call__(self, method, **kwargs):
                offsets.append(method.offset)
                if len(offsets) == 1:
                    return [Update.model_validate({
                        "update_id": number,
                        "message": {"message_id": number, "date": 0,
                                    "chat": {"id": -1, "type": "group"}, "text": "test"},
                    }) for number in (1, 2)]
                acknowledged.set()
                await asyncio.Event().wait()

        async def process(self, bot, update, **kwargs):
            number = update.message.message_id
            started.append(number)
            blocked.set()
            await release.wait()
            completed.append(number)

        with patch.object(Dispatcher, "_process_update", new=process):
            polling = asyncio.create_task(dp.start_polling(
                FakeTransport(), handle_signals=False, allowed_updates=["message"],
            ))
            stopping = None
            try:
                await asyncio.wait_for(acknowledged.wait(), 1)
                await asyncio.wait_for(blocked.wait(), 1)
                self.assertEqual(offsets, [None, 3])
                self.assertEqual(started, [1])
                stopping = asyncio.create_task(dp.stop_polling())
                await asyncio.wait_for(draining.wait(), 1)
                transport.session.close.assert_not_awaited()
                self.assertFalse(stopping.done())
                release.set()
                await asyncio.wait_for(stopping, 1)
                await asyncio.wait_for(polling, 1)
                self.assertEqual(completed, [1, 2])
                transport.session.close.assert_awaited_once()
            finally:
                release.set()
                if not polling.done() and stopping is None:
                    stopping = asyncio.create_task(dp.stop_polling())
                await asyncio.gather(polling, *([stopping] if stopping else []), return_exceptions=True)

    async def test_pending_backlog_is_bounded_and_drains_when_polling_is_cancelled(self):
        dp = ChatOrderedDispatcher()
        saturated, blocked, release = [asyncio.Event() for _ in range(3)]
        completed = []

        async def updates(*args, **kwargs):
            for number in range(10):
                if number == 4:
                    saturated.set()
                yield update(-1, number)

        async def process(self, bot, update, **kwargs):
            blocked.set()
            await release.wait()
            completed.append(update.message.message_id)

        with patch("tg_summary_bot.update_dispatcher.POLLING_PENDING_LIMIT", 4), patch.object(
            dp, "_listen_updates", new=updates,
        ), patch.object(Dispatcher, "_process_update", new=process):
            polling = asyncio.create_task(dp._polling(bot(), tasks_concurrency_limit=2))
            try:
                await asyncio.wait_for(saturated.wait(), 1)
                await asyncio.wait_for(blocked.wait(), 1)
                self.assertEqual(len(dp._handle_update_tasks), 4)
                self.assertEqual(completed, [])
            finally:
                polling.cancel()
                await asyncio.gather(polling, return_exceptions=True)
                release.set()
                await dp.emit_shutdown()
            # The fifth update was not accepted/acknowledged and remains for Telegram
            # to redeliver. All four accepted updates finish, in arrival order.
            self.assertEqual(completed, [0, 1, 2, 3])
