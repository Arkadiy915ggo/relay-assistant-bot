import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageText, GetMe, SendDice, SendMessage

from tg_summary_bot.telegram_delivery import TelegramDeliveryMiddleware


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_rate_limit_retries_the_same_request_after_cooldown(self) -> None:
        middleware = TelegramDeliveryMiddleware(interval=0)
        method = SendMessage(chat_id=-1, text="result")
        response = object()
        request = AsyncMock(side_effect=[
            TelegramRetryAfter(method=method, message="flood control", retry_after=33), response,
        ])
        clock = SimpleNamespace(now=100.0)

        async def sleep(delay):
            clock.now += delay

        loop = SimpleNamespace(time=lambda: clock.now)
        with patch("tg_summary_bot.telegram_delivery.asyncio.get_running_loop", return_value=loop), patch(
            "tg_summary_bot.telegram_delivery.asyncio.sleep", new=AsyncMock(side_effect=sleep)
        ) as wait, self.assertLogs(level="WARNING"):
            self.assertIs(await middleware(request, None, method), response)
        wait.assert_awaited_once()
        self.assertAlmostEqual(wait.await_args.args[0], 33.1)
        self.assertEqual(request.await_count, 2)
        self.assertTrue(all(call.args == (None, method) for call in request.await_args_list))

    async def test_dice_text_and_edits_share_per_chat_pacing(self) -> None:
        middleware = TelegramDeliveryMiddleware(interval=3.1)
        clock = SimpleNamespace(now=100.0)
        calls = []

        async def request(bot, method):
            calls.append(clock.now)

        async def sleep(delay):
            clock.now += delay

        with patch("tg_summary_bot.telegram_delivery.asyncio.get_running_loop", return_value=SimpleNamespace(
            time=lambda: clock.now
        )), patch("tg_summary_bot.telegram_delivery.asyncio.sleep", side_effect=sleep):
            await middleware(request, None, SendDice(chat_id=-1, emoji="🎰"))
            await middleware(request, None, SendMessage(chat_id=-1, text="result"))
            await middleware(request, None, EditMessageText(chat_id=-1, message_id=1, text="top"))
            await middleware(request, None, SendMessage(chat_id=-2, text="other chat"))
        self.assertEqual(len(calls), 4)
        self.assertAlmostEqual(calls[1] - calls[0], 3.1)
        self.assertAlmostEqual(calls[2] - calls[1], 3.1)
        self.assertEqual(calls[3], calls[2])

    async def test_retries_are_bounded_and_network_errors_are_not_retried(self) -> None:
        for method, error_type in ((SendDice(chat_id=-1), TelegramRetryAfter), (GetMe(), TelegramRetryAfter),
                                   (SendDice(chat_id=-1), TimeoutError)):
            with self.subTest(method=type(method).__name__, error=error_type.__name__):
                middleware = TelegramDeliveryMiddleware(interval=0, max_retries=2)
                error = (TelegramRetryAfter(method=method, message="flood", retry_after=0)
                         if error_type is TelegramRetryAfter else TimeoutError())
                request = AsyncMock(side_effect=error)
                with patch("tg_summary_bot.telegram_delivery.asyncio.sleep", new_callable=AsyncMock), self.assertRaises(
                    error_type
                ):
                    await middleware(request, None, method)
                self.assertEqual(request.await_count, 3 if error_type is TelegramRetryAfter else 1)

    async def test_cancellation_releases_chat_lock(self) -> None:
        middleware = TelegramDeliveryMiddleware(interval=0)
        method = SendMessage(chat_id=-1, text="result")
        request = AsyncMock(side_effect=TelegramRetryAfter(method=method, message="flood", retry_after=33))
        with patch("tg_summary_bot.telegram_delivery.asyncio.sleep", side_effect=asyncio.CancelledError), self.assertRaises(
            asyncio.CancelledError
        ), self.assertLogs(level="WARNING"):
            await middleware(request, None, method)
        self.assertFalse(middleware._chats[-1].lock.locked())
        self.assertGreater(middleware._chats[-1].next_request_at, asyncio.get_running_loop().time())

    async def test_rate_limited_chat_does_not_block_another_chat_or_allow_overtaking(self) -> None:
        middleware = TelegramDeliveryMiddleware(interval=0)
        waiting = asyncio.Event()
        release = asyncio.Event()
        calls = []
        rejected = False

        async def request(bot, method):
            nonlocal rejected
            calls.append((method.chat_id, method.text))
            if method.chat_id == -1 and not rejected:
                rejected = True
                raise TelegramRetryAfter(method=method, message="flood", retry_after=33)
            return method.text

        async def sleep(delay):
            waiting.set()
            await release.wait()

        with patch("tg_summary_bot.telegram_delivery.asyncio.sleep", side_effect=sleep), self.assertLogs(level="WARNING"):
            first = asyncio.create_task(middleware(request, None, SendMessage(chat_id=-1, text="first")))
            try:
                await asyncio.wait_for(waiting.wait(), timeout=1)
                second = asyncio.create_task(middleware(request, None, SendMessage(chat_id=-1, text="second")))
                other = await asyncio.wait_for(
                    middleware(request, None, SendMessage(chat_id=-2, text="other")), timeout=1,
                )
                self.assertEqual(other, "other")
                self.assertFalse(second.done())
                release.set()
                self.assertEqual(await asyncio.wait_for(asyncio.gather(first, second), timeout=1), ["first", "second"])
            finally:
                release.set()
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(calls, [(-1, "first"), (-2, "other"), (-1, "first"), (-1, "second")])
