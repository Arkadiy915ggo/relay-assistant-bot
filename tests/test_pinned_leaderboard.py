import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from tg_summary_bot.bot import (
    refresh_pinned_leaderboard, refresh_pinned_leaderboard_for_chat,
    refresh_recovered_casino_leaderboards, render_leaderboard,
)
from tg_summary_bot.storage import PinnedLeaderboard, PointBalance


class FakeBot:
    def __init__(self, *, edit_error: Exception | None = None, pin_error: Exception | None = None) -> None:
        self.edit_error = edit_error
        self.pin_error = pin_error
        self.edits: list[dict[str, object]] = []
        self.pins: list[dict[str, object]] = []

    async def edit_message_text(self, **kwargs: object) -> None:
        self.edits.append(kwargs)
        if self.edit_error:
            raise self.edit_error

    async def pin_chat_message(self, **kwargs: object) -> None:
        self.pins.append(kwargs)
        if self.pin_error:
            raise self.pin_error


class FakeMessage:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(id=1, type="group")
        self.message_id = 10
        self.text = "/summary"
        self.answers: list[str] = []

    async def answer(self, text: str, **_: object) -> SimpleNamespace:
        self.answers.append(text)
        return SimpleNamespace(message_id=77, chat=self.chat)


class FakeStore:
    def __init__(self, pinned: PinnedLeaderboard | None = None) -> None:
        self.pinned = pinned
        self.deleted = False
        self.saved: list[tuple[int, int]] = []
        self.balances = [
            PointBalance(1, "id:1", "Alice", 20, "now"),
            PointBalance(1, "id:2", "Bob", 10, "now"),
        ]

    async def get_top_point_balances(self, **_: object) -> list[PointBalance]:
        return self.balances

    async def get_pinned_leaderboard(self, **_: object) -> PinnedLeaderboard | None:
        return self.pinned

    async def delete_pinned_leaderboard(self, **_: object) -> None:
        self.pinned = None
        self.deleted = True

    async def save_pinned_leaderboard(self, *, chat_id: int, message_id: int) -> None:
        self.saved.append((chat_id, message_id))
        self.pinned = PinnedLeaderboard(chat_id, message_id, "now")


class PinnedLeaderboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_refreshes_read_latest_balance_in_order(self) -> None:
        store = FakeStore(PinnedLeaderboard(1, 44, "now"))
        store.balances = [PointBalance(1, "id:1", "Alice", 10, "now")]
        first_edit = asyncio.Event()
        release = asyncio.Event()
        second_started = asyncio.Event()
        displayed = []

        async def edit(**kwargs):
            if "Alice - 10" in kwargs["text"]:
                first_edit.set()
                await release.wait()
            displayed.append(kwargs["text"])

        async def second_refresh():
            second_started.set()
            await refresh_pinned_leaderboard_for_chat(bot=bot, store=store, chat_id=1)

        bot = FakeBot()
        bot.edit_message_text = edit
        store.get_top_point_balances = AsyncMock(wraps=store.get_top_point_balances)
        first = asyncio.create_task(refresh_pinned_leaderboard_for_chat(bot=bot, store=store, chat_id=1))
        second = None
        try:
            await asyncio.wait_for(first_edit.wait(), 1)
            store.balances = [PointBalance(1, "id:1", "Alice", 20, "now")]
            second = asyncio.create_task(second_refresh())
            await asyncio.wait_for(second_started.wait(), 1)
            self.assertEqual(store.get_top_point_balances.await_count, 1)
            # A slow chat must not serialize all other chats on the same bot.
            await asyncio.wait_for(refresh_pinned_leaderboard_for_chat(
                bot=FakeBot(), store=FakeStore(PinnedLeaderboard(2, 55, "now")), chat_id=2,
            ), 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertEqual([text.splitlines()[-1] for text in displayed], ["1. Alice - 10", "1. Alice - 20"])

    async def test_overlapping_first_refresh_creates_only_one_message(self) -> None:
        store = FakeStore()
        sent = asyncio.Event()
        release = asyncio.Event()
        second_started = asyncio.Event()
        bot = FakeBot()

        async def send(*args, **kwargs):
            sent.set()
            await release.wait()
            return SimpleNamespace(message_id=88)

        bot.send_message = AsyncMock(side_effect=send)

        async def refresh_again():
            second_started.set()
            return await refresh_pinned_leaderboard_for_chat(bot=bot, store=store, chat_id=1)

        first = asyncio.create_task(refresh_pinned_leaderboard_for_chat(bot=bot, store=store, chat_id=1))
        second = None
        try:
            await asyncio.wait_for(sent.wait(), 1)
            second = asyncio.create_task(refresh_again())
            await asyncio.wait_for(second_started.wait(), 1)
            bot.send_message.assert_awaited_once()
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 1)
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertEqual(store.saved, [(1, 88)])
        self.assertEqual([edit["message_id"] for edit in bot.edits], [88])
        self.assertEqual([pin["message_id"] for pin in bot.pins], [88, 88])

    async def test_cancelled_refresh_releases_chat_lock(self) -> None:
        started = asyncio.Event()

        async def edit(**kwargs):
            started.set()
            await asyncio.Event().wait()

        store = FakeStore(PinnedLeaderboard(1, 44, "now"))
        bot = FakeBot()
        bot.edit_message_text = edit
        task = asyncio.create_task(refresh_pinned_leaderboard_for_chat(bot=bot, store=store, chat_id=1))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await asyncio.wait_for(refresh_pinned_leaderboard_for_chat(
            bot=FakeBot(), store=store, chat_id=1,
        ), 1))

    async def test_startup_refresh_uses_current_allowlist(self) -> None:
        for allowed, expected in (({2}, [2]), (set(), [1, 2])):
            with self.subTest(allowed=allowed), patch(
                "tg_summary_bot.bot.refresh_pinned_leaderboard_for_chat", new_callable=AsyncMock
            ) as refresh:
                await refresh_recovered_casino_leaderboards(
                    settings=SimpleNamespace(allowed_chat_ids=allowed),
                    bot=FakeBot(), store=FakeStore(), chat_ids={1, 2},
                )
                self.assertEqual([call.kwargs["chat_id"] for call in refresh.await_args_list], expected)

    def test_rendering_matches_top_order(self) -> None:
        rendered = render_leaderboard(
            [PointBalance(1, "id:1", "Alice", 10, "now")]
        )
        self.assertEqual(rendered, "Топ балансов:\n1. Alice - 10")

    async def test_edits_and_repins_existing_leaderboard(self) -> None:
        store = FakeStore(PinnedLeaderboard(1, 44, "now"))
        bot = FakeBot()
        message = FakeMessage()
        await refresh_pinned_leaderboard(bot=bot, store=store, source_message=message)  # type: ignore[arg-type]
        self.assertEqual(bot.edits[0]["message_id"], 44)
        self.assertEqual(bot.pins[0]["message_id"], 44)
        self.assertFalse(message.answers)
        self.assertFalse(store.saved)

    async def test_deleted_leaderboard_is_recreated_and_pinned(self) -> None:
        store = FakeStore(PinnedLeaderboard(1, 44, "now"))
        bot = FakeBot(
            edit_error=TelegramBadRequest(
                method=SendMessage(chat_id=1, text="x"),
                message="Bad Request: message to edit not found",
            ),
            pin_error=RuntimeError("not admin"),
        )
        message = FakeMessage()
        with self.assertLogs(level="WARNING"):
            await refresh_pinned_leaderboard(bot=bot, store=store, source_message=message)  # type: ignore[arg-type]
        self.assertTrue(store.deleted)
        self.assertEqual(store.saved, [(1, 77)])
        self.assertEqual(bot.pins[-1]["message_id"], 77)
