from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Awaitable, Callable
from typing import Literal

from tg_summary_bot.llm import LLMClient
from tg_summary_bot.storage import MessageStore, SnapshotBoundary, StoredMessage


JOKE_SELECTOR_TIMEOUT_SECONDS = 120
DEFAULT_SELECTOR_ROW_LIMIT = 20
DEFAULT_SELECTOR_CHAR_BUDGET = 12_000

SYSTEM_PROMPT = """
Ты выбираешь одну действительно смешную реплику из предложенных сообщений Telegram-чата.
Никогда не выбирай нейтральный текст только потому, что нужно вернуть кандидата.
Длинные реплики могут быть сокращены с маркером […]; оценивай только видимый текст, не додумывай пропущенное.
Верни ровно один JSON object без Markdown, пояснений и дополнительных ключей.
Схема только такая:
{"has_joke":true,"source_message_id":123,"reason":"короткое объяснение"}
или:
{"has_joke":false,"source_message_id":null,"reason":"смешной реплики нет"}
""".strip()


@dataclass(frozen=True)
class JokeSelection:
    has_joke: bool
    source_message_id: int | None
    reason: str
    valid: bool = True
    error: str | None = None


@dataclass(frozen=True)
class JokeSelectionResult:
    status: Literal["selected", "none", "invalid", "error"]
    winner: StoredMessage | None = None
    reason: str | None = None
    pages: int = 0
    tournament_rounds: int = 0


def _reject(reason: str) -> JokeSelection:
    return JokeSelection(False, None, "", valid=False, error=reason)


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def parse_joke_selection(response: str, candidate_ids: set[int]) -> JokeSelection:
    """Validate one complete model response without extracting or repairing JSON."""
    try:
        value = json.loads(response, object_pairs_hook=_strict_object_pairs)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _reject("invalid_json")
    if not isinstance(value, dict):
        return _reject("not_object")
    if set(value) != {"has_joke", "source_message_id", "reason"}:
        return _reject("invalid_keys")
    has_joke = value["has_joke"]
    source_message_id = value["source_message_id"]
    reason = value["reason"]
    if not isinstance(has_joke, bool):
        return _reject("invalid_has_joke")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
        return _reject("invalid_reason")
    if not has_joke:
        if source_message_id is not None:
            return _reject("non_null_no_joke_id")
        return JokeSelection(False, None, reason)
    if isinstance(source_message_id, bool) or not isinstance(source_message_id, int):
        return _reject("invalid_source_message_id")
    if source_message_id not in candidate_ids:
        return _reject("source_not_in_candidates")
    return JokeSelection(True, source_message_id, reason)


def _candidate_line(message: StoredMessage) -> str:
    return json.dumps(
        {"target_message_id": message.message_id, "author": message.sender_name, "text": message.text},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _candidate_batches(
    candidates: list[StoredMessage],
    *,
    row_limit: int,
    char_budget: int,
) -> list[list[StoredMessage]]:
    batches: list[list[StoredMessage]] = []
    current: list[StoredMessage] = []
    current_chars = 0
    for candidate in candidates:
        size = len(_candidate_line(candidate)) + 2
        if current and (len(current) >= row_limit or current_chars + size > char_budget):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(candidate)
        current_chars += size
    if current:
        batches.append(current)
    return batches


def _candidate_prompt(candidates: list[StoredMessage], char_budget: int) -> str | None:
    """Bound the serialized prompt, retaining canonical objects for award persistence."""
    def render(limit: int) -> str:
        def excerpt(text: str) -> str:
            if len(text) <= limit:
                return text
            if limit < 5:
                return text[:limit]
            head = (limit - 5) // 2
            tail = limit - 5 - head
            return text[:head] + " […] " + (text[-tail:] if tail else "")

        return json.dumps(
            {"candidates": [
                {"target_message_id": item.message_id, "author": item.sender_name,
                 "text": excerpt(item.text)} for item in candidates
            ]}, ensure_ascii=False, separators=(",", ":"),
        )

    low, high = 0, max((len(item.text) for item in candidates), default=0)
    full = render(high)
    if len(full) <= char_budget:
        return full
    if len(render(0)) > char_budget:
        return None
    while low < high:
        middle = (low + high + 1) // 2
        if len(render(middle)) <= char_budget:
            low = middle
        else:
            high = middle - 1
    return render(low)


class JokeSelector:
    """LLM-backed selector with strict, prompt-local source-id validation."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        row_limit: int = DEFAULT_SELECTOR_ROW_LIMIT,
        char_budget: int = DEFAULT_SELECTOR_CHAR_BUDGET,
    ) -> None:
        if row_limit <= 0 or char_budget <= 0:
            raise ValueError("row_limit and char_budget must be positive")
        self.llm = llm
        self.row_limit = row_limit
        self.char_budget = char_budget

    async def unload(self) -> None:
        await self.llm.unload()

    async def choose(
        self,
        store: MessageStore,
        *,
        chat_id: int,
        since: datetime,
        boundary: SnapshotBoundary,
        exclude_message_id: int | None = None,
    ) -> JokeSelectionResult:
        cursor: SnapshotBoundary | None = None
        page_winners: list[StoredMessage] = []
        pages = 0
        while True:
            page = await store.get_eligible_snapshot_page(
                chat_id=chat_id,
                since=since,
                boundary=boundary,
                after=cursor,
                row_limit=self.row_limit,
                char_budget=self.char_budget,
            )
            if not page.messages:
                break
            cursor = page.next_cursor
            candidates = [
                message
                for message in page.messages
                if message.message_id != exclude_message_id
            ]
            if candidates:
                pages += 1
                selection = await self._select_from(candidates)
                if not selection.valid:
                    return JokeSelectionResult("invalid", reason=selection.error, pages=pages)
                if selection.has_joke:
                    page_winners.append(
                        next(
                            message
                            for message in candidates
                            if message.message_id == selection.source_message_id
                        )
                    )
            if page.next_cursor is None:
                break

        if not page_winners:
            return JokeSelectionResult("none", pages=pages)
        winner, reason, rounds, error = await self._run_tournament(page_winners)
        if error:
            return JokeSelectionResult(
                "invalid",
                reason=error,
                pages=pages,
                tournament_rounds=rounds,
            )
        if not winner:
            return JokeSelectionResult("none", pages=pages, tournament_rounds=rounds)
        return JokeSelectionResult(
            "selected",
            winner=winner,
            reason=reason,
            pages=pages,
            tournament_rounds=rounds,
        )

    async def choose_messages(
        self,
        candidates: list[StoredMessage],
        *,
        select_batch: Callable[[list[StoredMessage]], Awaitable[JokeSelection]] | None = None,
    ) -> JokeSelectionResult:
        """Select from immutable canonical job rows without querying a time snapshot."""
        if not candidates:
            return JokeSelectionResult("none")
        pages = 0
        winners: list[StoredMessage] = []
        batch_selector = select_batch or self._select_from
        for batch in _candidate_batches(candidates, row_limit=self.row_limit, char_budget=self.char_budget):
            pages += 1
            selection = await batch_selector(batch)
            if not selection.valid:
                return JokeSelectionResult("invalid", reason=selection.error, pages=pages)
            if selection.has_joke:
                winners.append(next(item for item in batch if item.message_id == selection.source_message_id))
        if not winners:
            return JokeSelectionResult("none", pages=pages)
        # A callback is used by the worker so every model batch can have its own GPU admission.
        if select_batch is None:
            winner, reason, rounds, error = await self._run_tournament(winners)
        else:
            winner, reason, rounds, error = await self._run_tournament_with(winners, batch_selector)
        if error:
            return JokeSelectionResult("invalid", reason=error, pages=pages, tournament_rounds=rounds)
        if not winner:
            return JokeSelectionResult("none", pages=pages, tournament_rounds=rounds)
        return JokeSelectionResult("selected", winner=winner, reason=reason, pages=pages, tournament_rounds=rounds)

    async def _run_tournament(
        self,
        candidates: list[StoredMessage],
    ) -> tuple[StoredMessage | None, str | None, int, str | None]:
        return await self._run_tournament_with(candidates, self._select_from)

    async def _run_tournament_with(
        self,
        candidates: list[StoredMessage],
        select_batch: Callable[[list[StoredMessage]], Awaitable[JokeSelection]],
    ) -> tuple[StoredMessage | None, str | None, int, str | None]:
        current = candidates
        rounds = 0
        reason: str | None = None
        while len(current) > 1:
            rounds += 1
            next_round: list[StoredMessage] = []
            batches = _candidate_batches(current, row_limit=self.row_limit, char_budget=self.char_budget)
            if len(batches) == len(current):
                # Singleton-only comparisons cannot eliminate a candidate. Compare
                # pairs using bounded excerpts; source text and identity stay intact.
                batches = [current[index:index + 2] for index in range(0, len(current), 2)]
            for batch in batches:
                selection = await select_batch(batch)
                if not selection.valid:
                    return None, None, rounds, selection.error
                if selection.has_joke:
                    next_round.append(next(item for item in batch if item.message_id == selection.source_message_id))
                    reason = selection.reason
            if len(next_round) >= len(current):
                return None, None, rounds, "tournament_no_progress"
            current = next_round
            if not current:
                return None, None, rounds, None
        return current[0], reason, rounds, None

    async def _select_from(self, candidates: list[StoredMessage]) -> JokeSelection:
        candidate_ids = {candidate.message_id for candidate in candidates}
        prompt = _candidate_prompt(candidates, self.char_budget)
        if prompt is None:
            return _reject("prompt_budget_too_small")
        try:
            async with asyncio.timeout(JOKE_SELECTOR_TIMEOUT_SECONDS):
                response = await self.llm.complete(
                    system=SYSTEM_PROMPT,
                    user=prompt,
                    response_format="json",
                )
        except TimeoutError:
            return _reject("timeout")
        except Exception:
            return _reject("model_error")
        return parse_joke_selection(response, candidate_ids)
