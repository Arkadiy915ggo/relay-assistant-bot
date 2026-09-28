import asyncio
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tg_summary_bot.bot as bot_module
from tg_summary_bot.joke_awards import JokeSelectionResult
from tg_summary_bot.storage import JokeAwardResult, SnapshotBoundary, StoredMessage


NOW = datetime.now(timezone.utc)


class RecordingLock:
    def __init__(self, events: list[str]) -> None:
        self._lock = asyncio.Lock()
        self.events = events

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        self.events.append("lock:acquire")

    async def __aexit__(self, *_: object) -> None:
        self.events.append("lock:release")
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()


class FakeResponse:
    def __init__(self, message_id: int, chat: SimpleNamespace) -> None:
        self.message_id = message_id
        self.chat = chat
        self.text = ""
        self.edits: list[str] = []

    async def edit_text(self, text: str, **_: object) -> None:
        self.text = text
        self.edits.append(text)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.message_id = 100
        self.chat = SimpleNamespace(id=1, type="group")
        self.text = text
        self.caption = None
        self.answers: list[FakeResponse] = []

    async def answer(self, text: str, **_: object) -> FakeResponse:
        response = FakeResponse(200 + len(self.answers), self.chat)
        response.text = text
        self.answers.append(response)
        return response


class FakeSummarizer:
    def __init__(self, events: list[str], lock: RecordingLock) -> None:
        self.events = events
        self.lock = lock

    async def summarize(self, *_: object) -> str:
        self.events.append(f"summary:run:{self.lock.locked()}")
        return "summary text"

    async def unload(self) -> None:
        self.events.append(f"summary:unload:{self.lock.locked()}")


class FakeSelector:
    def __init__(
        self,
        events: list[str],
        lock: RecordingLock,
        result: JokeSelectionResult,
    ) -> None:
        self.events = events
        self.lock = lock
        self.result = result
        self.calls = 0

    async def choose(self, *_: object, **__: object) -> JokeSelectionResult:
        self.calls += 1
        self.events.append(f"selector:run:{self.lock.locked()}")
        return self.result

    async def unload(self) -> None:
        self.events.append(f"selector:unload:{self.lock.locked()}")


class FakeStore:
    def __init__(self, awards: list[JokeAwardResult | Exception]) -> None:
        self.awards = awards
        self.award_calls = 0
        self.message = StoredMessage(
            message_id=10,
            chat_id=1,
            chat_type="group",
            sender_id=7,
            sender_name="Selector Name",
            text="selector text",
            created_at=NOW.isoformat(),
            reply_to_message_id=None,
            origin="incoming",
            kind="text",
        )

    async def get_eligible_snapshot_boundary(self, **_: object) -> SnapshotBoundary:
        return SnapshotBoundary(self.message.created_at, self.message.message_id)

    async def get_messages_since(self, **_: object) -> list[StoredMessage]:
        return [self.message]

    async def award_unique_joke(self, **_: object) -> JokeAwardResult:
        self.award_calls += 1
        result = self.awards.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "allowed_chat_ids": set(),
        "default_summary_period": "24h",
        "max_summary_input_chars": 10000,
        "resolved_llm_provider": "ollama",
        "ollama_model": "summary-model",
        "openai_model": "openai-model",
        "compare_models": ["compare-model"],
        "chunk_chars": 1000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def make_dispatcher(
    *,
    store: FakeStore,
    summarizer: object,
    lock: RecordingLock,
    selector: FakeSelector,
) -> object:
    return await bot_module.create_dispatcher(
        settings(),  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        summarizer,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        None,
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        None,
        None,
        lock,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        selector,  # type: ignore[arg-type]
    )


def handler(dispatcher: object, name: str):
    return next(
        item.callback
        for item in dispatcher.message.handlers  # type: ignore[attr-defined]
        if item.callback.__name__ == name
    )


class JokeSummaryHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_award_uses_canonical_source_and_separate_lock_phases(self) -> None:
        events: list[str] = []
        lock = RecordingLock(events)
        award = JokeAwardResult(
            status="awarded",
            source_message_id=10,
            participant_key="id:7",
            participant_name="Canonical Name",
            source_text="canonical text",
            balance=10,
        )
        store = FakeStore([award])
        selector = FakeSelector(
            events,
            lock,
            JokeSelectionResult(status="selected", winner=store.message),
        )
        dispatcher = await make_dispatcher(
            store=store,
            summarizer=FakeSummarizer(events, lock),
            lock=lock,
            selector=selector,
        )
        message = FakeMessage("/summary")
        refresh = AsyncMock()

        with patch.object(bot_module, "refresh_pinned_leaderboard", refresh):
            await handler(dispatcher, "summary_command")(message, SimpleNamespace())

        rendered = message.answers[0].edits[-1]
        self.assertIn("Canonical Name", rendered)
        self.assertIn("canonical text", rendered)
        self.assertNotIn("Selector Name", rendered)
        self.assertIn("+10 очков", rendered)
        self.assertEqual(store.award_calls, 1)
        refresh.assert_awaited_once()
        self.assertEqual(
            events,
            [
                "lock:acquire",
                "summary:run:True",
                "summary:unload:True",
                "lock:release",
                "lock:acquire",
                "selector:run:True",
                "selector:unload:True",
                "lock:release",
            ],
        )

    async def test_repeat_summary_does_not_claim_a_second_award(self) -> None:
        events: list[str] = []
        lock = RecordingLock(events)
        repeated = JokeAwardResult(
            status="already_awarded",
            source_message_id=10,
            participant_key="id:7",
            participant_name="Canonical Name",
            source_text="canonical text",
            balance=10,
        )
        store = FakeStore([repeated])
        selector = FakeSelector(
            events,
            lock,
            JokeSelectionResult(status="selected", winner=store.message),
        )
        dispatcher = await make_dispatcher(
            store=store,
            summarizer=FakeSummarizer(events, lock),
            lock=lock,
            selector=selector,
        )
        message = FakeMessage("/summary")
        refresh = AsyncMock()

        with patch.object(bot_module, "refresh_pinned_leaderboard", refresh):
            await handler(dispatcher, "summary_command")(message, SimpleNamespace())

        rendered = message.answers[0].edits[-1]
        self.assertIn("Уже была награждена", rendered)
        self.assertNotIn("+10 очков", rendered)
        refresh.assert_not_awaited()

    async def test_award_failure_keeps_summary_without_claiming_points(self) -> None:
        events: list[str] = []
        lock = RecordingLock(events)
        store = FakeStore([RuntimeError("database failed")])
        selector = FakeSelector(
            events,
            lock,
            JokeSelectionResult(status="selected", winner=store.message),
        )
        dispatcher = await make_dispatcher(
            store=store,
            summarizer=FakeSummarizer(events, lock),
            lock=lock,
            selector=selector,
        )
        message = FakeMessage("/summary")

        with self.assertLogs(level="ERROR"):
            await handler(dispatcher, "summary_command")(message, SimpleNamespace())

        rendered = message.answers[0].edits[-1]
        self.assertIn("summary text", rendered)
        self.assertIn("очки не начислены", rendered)
        self.assertNotIn("+10 очков", rendered)

    async def test_compare_never_calls_selector_or_award_storage(self) -> None:
        events: list[str] = []
        lock = RecordingLock(events)
        store = FakeStore([])
        selector = FakeSelector(
            events,
            lock,
            JokeSelectionResult(status="none"),
        )
        dispatcher = await make_dispatcher(
            store=store,
            summarizer=FakeSummarizer(events, lock),
            lock=lock,
            selector=selector,
        )
        message = FakeMessage("/compare 24h")

        class CompareSummarizer:
            def __init__(self, *_: object) -> None:
                pass

            async def summarize(self, *_: object) -> str:
                return "comparison"

            async def unload(self) -> None:
                return None

        with (
            patch.object(bot_module, "build_llm_client", return_value=SimpleNamespace()),
            patch.object(bot_module, "Summarizer", CompareSummarizer),
        ):
            await handler(dispatcher, "compare_command")(message)

        self.assertEqual(selector.calls, 0)
        self.assertEqual(store.award_calls, 0)
        self.assertTrue(any("Comparison: compare-model" in item.text for item in message.answers))
