import unittest
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from tg_summary_bot.bot import refresh_pinned_leaderboard, render_leaderboard
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


class PinnedLeaderboardTests(unittest.IsolatedAsyncioTestCase):
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
