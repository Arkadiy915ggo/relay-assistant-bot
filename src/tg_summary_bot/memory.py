from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from tg_summary_bot.config import Settings
from tg_summary_bot.llm import LLMClient
from tg_summary_bot.observability import opik_track, update_opik_span_metadata
from tg_summary_bot.periods import parse_period
from tg_summary_bot.storage import (
    ChatMemoryBlock,
    ChatParticipantFact,
    MessageStore,
    StoredMessage,
)


MEMORY_SYSTEM_PROMPT = """
Ты аккуратно сжимаешь историю Telegram-чата в долговременную память.
Пиши по-русски, кратко и фактически.
Не выдумывай решения, ответственных, даты, роли, предпочтения и договоренности.
Сохраняй только то, что явно следует из сообщений.
Каждый факт о человеке должен иметь source_message_ids из входных сообщений.
Не сохраняй шутки, оскорбления, догадки и временные эмоции как факты о человеке.
""".strip()


FACT_TYPES = {
    "role",
    "responsibility",
    "project",
    "preference",
    "constraint",
    "task",
    "temporary_state",
    "skill",
    "relationship",
    "other",
}
TEMPORARY_FACT_TYPES = {"task", "temporary_state"}
ROLLUP_GROUP_SIZE = 8
PROFILE_CHARS_PER_CONTEXT_TOKEN = 3
PROFILE_CHUNK_MIN_CHARS = 3500
PROFILE_CHUNK_MAX_CHARS = 9000


class MemoryCompressionError(RuntimeError):
    pass


def participant_key(sender_id: int | None, sender_name: str) -> str:
    if sender_id is not None:
        return f"id:{sender_id}"
    normalized = re.sub(r"[^\wа-яА-ЯёЁ]+", "_", sender_name.strip().lower()).strip("_")
    return f"name:{normalized or 'unknown'}"


def _render_messages(messages: list[StoredMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        reply = f" reply_to={msg.reply_to_message_id}" if msg.reply_to_message_id else ""
        key = participant_key(msg.sender_id, msg.sender_name)
        lines.append(
            f"[{msg.created_at} #{msg.message_id}{reply} sender_key={key}] "
            f"{msg.sender_name}: {msg.text}"
        )
    return "\n".join(lines)


def _json_object(text: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _safe_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _string_list(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _object_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _text_items(value: object, *, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("fact") or item.get("task") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            items.append(text)
    return items[:limit]


def _has_structured_items(data: dict[str, object]) -> bool:
    return any(
        _object_list(data.get(key))
        for key in [
            "decisions",
            "tasks",
            "open_questions",
            "important_events",
            "participant_facts",
        ]
    ) or bool(_text_items(data.get("topics")) or _text_items(data.get("keywords")))


def _validate_memory_data(data: dict[str, object], *, message_count: int) -> None:
    summary = str(data.get("summary") or "").strip()
    min_summary_chars = 80 if message_count >= 10 else 20
    if len(summary) >= min_summary_chars or _has_structured_items(data):
        return
    raise MemoryCompressionError(
        "Memory compression returned an implausibly short/empty JSON result. "
        "Increase OLLAMA_NUM_CTX, lower MEMORY_CHUNK_CHARS, or restart Ollama before rebuilding memory."
    )


def _empty_fact_stats() -> dict[str, int]:
    return {
        "saved": 0,
        "returned": 0,
        "rejected_no_text": 0,
        "rejected_too_short": 0,
        "rejected_no_source": 0,
        "rejected_low_confidence": 0,
        "rejected_invalid_key": 0,
        "invalid_json": 0,
    }


def _profile_chunk_chars(settings: Settings, memory_chunk_chars: int) -> int:
    ctx_based_chars = settings.ollama_num_ctx * PROFILE_CHARS_PER_CONTEXT_TOKEN // 4
    return max(
        PROFILE_CHUNK_MIN_CHARS,
        min(memory_chunk_chars, PROFILE_CHUNK_MAX_CHARS, ctx_based_chars),
    )


def _tokens(text: str) -> list[str]:
    result: list[str] = []
    for token in re.findall(r"[\wа-яА-ЯёЁ]{3,}", text.lower()):
        if token not in result:
            result.append(token)
    return result[:24]


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        result.append(number)
    return result


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expires_at(fact_type: str, last_seen_at: str, raw: dict[str, object]) -> str | None:
    raw_days = raw.get("expires_after_days")
    try:
        days = int(raw_days) if raw_days is not None else None
    except (TypeError, ValueError):
        days = None

    if days is None and fact_type == "temporary_state":
        days = 14
    if days is None and fact_type == "task":
        days = 60
    if days is None or days <= 0:
        return None
    return (_parse_datetime(last_seen_at) + timedelta(days=days)).isoformat()


def _participant_name_for_key(messages: list[StoredMessage], key: str) -> str:
    for message in messages:
        if participant_key(message.sender_id, message.sender_name) == key:
            return message.sender_name
    return key


def _block_structured_data(block: ChatMemoryBlock) -> dict[str, object]:
    data = _json_object(block.structured_json)
    return data or {}


def _block_text(block: ChatMemoryBlock) -> str:
    data = _block_structured_data(block)
    sections = [
        f"Сжатая память чата ({block.level}) за {block.period_start}..{block.period_end}.",
        f"Темы: {block.topics or 'не выделены'}",
        f"Ключевые слова: {block.keywords or 'не выделены'}",
        f"Сообщений в блоке: {block.message_count}",
        "",
        str(data.get("summary") or block.summary),
    ]
    for title, key in [
        ("Решения", "decisions"),
        ("Задачи", "tasks"),
        ("Открытые вопросы", "open_questions"),
        ("Важные события", "important_events"),
    ]:
        items = _text_items(data.get(key))
        if items:
            sections.append(f"\n{title}:")
            sections.extend(f"- {item}" for item in items)
    return "\n".join(sections).strip()


def _profile_lines(facts: list[ChatParticipantFact], *, limit: int = 18) -> list[str]:
    grouped: dict[str, list[ChatParticipantFact]] = {}
    names: dict[str, str] = {}
    for fact in facts:
        if fact.confidence == "low":
            continue
        grouped.setdefault(fact.participant_key, []).append(fact)
        names[fact.participant_key] = fact.participant_name

    lines: list[str] = []
    for key, user_facts in grouped.items():
        lines.append(f"- {names.get(key, key)}:")
        for fact in user_facts[:limit]:
            suffix = f" ({fact.fact_type}, {fact.confidence})"
            lines.append(f"  - {fact.fact_text}{suffix}")
    return lines


class ChatMemory:
    def __init__(self, store: MessageStore, llm: LLMClient, settings: Settings) -> None:
        self.store = store
        self.llm = llm
        self.recent_period = parse_period(settings.memory_recent_period)
        self.chunk_chars = min(settings.memory_chunk_chars, settings.chunk_chars)
        if self.chunk_chars < settings.memory_chunk_chars:
            logging.info(
                "Memory chunk chars capped by CHUNK_CHARS memory_chunk_chars=%s effective=%s",
                settings.memory_chunk_chars,
                self.chunk_chars,
            )
        self.max_blocks = settings.memory_max_blocks
        self.search_limit = settings.memory_search_limit
        self.profile_chunk_chars = _profile_chunk_chars(settings, self.chunk_chars)

    def recent_since(self, now: datetime | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc) - self.recent_period

    async def ensure_current(self, chat_id: int, *, now: datetime | None = None) -> int:
        cutoff = self.recent_since(now)
        processed = await self.store.get_memory_processed_until(chat_id)
        created_blocks = 0

        while True:
            messages = await self.store.get_memory_message_chunk(
                chat_id=chat_id,
                before=cutoff,
                after=processed,
                limit_chars=self.chunk_chars,
            )
            if not messages:
                await self._maintain_hierarchy(chat_id)
                return created_blocks

            data = await self._compress_messages(messages)
            period_start = messages[0].created_at
            period_end = messages[-1].created_at
            await self.store.save_memory_block(
                chat_id=chat_id,
                period_start=period_start,
                period_end=period_end,
                summary=str(data.get("summary") or "")[:2500],
                topics=_string_list(data.get("topics"))[:1000],
                keywords=_string_list(data.get("keywords"))[:1000],
                message_count=len(messages),
                structured_json=_safe_json(data),
                level="chunk",
                processed_until=period_end,
            )
            fact_stats = await self._save_participant_facts(chat_id, data, messages)
            processed = period_end
            created_blocks += 1
            logging.info(
                "Chat memory block created chat_id=%s messages=%s facts=%s period=%s..%s",
                chat_id,
                len(messages),
                fact_stats["saved"],
                period_start,
                period_end,
            )

    async def ensure_recent_profiles_current(
        self,
        chat_id: int,
        *,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)

        recent_start = self.recent_since(now).isoformat()
        processed = await self.store.get_profile_processed_until(chat_id)
        after = max(processed, recent_start) if processed else recent_start
        saved_total = 0

        while True:
            messages = await self.store.get_profile_message_chunk(
                chat_id=chat_id,
                after=after,
                before=now,
                limit_chars=self.profile_chunk_chars,
            )
            if not messages:
                return saved_total

            stats = await self._extract_participant_facts(chat_id, messages)
            if stats["invalid_json"]:
                logging.warning(
                    "Profile fact extraction returned invalid JSON chat_id=%s messages=%s period=%s..%s",
                    chat_id,
                    len(messages),
                    messages[0].created_at,
                    messages[-1].created_at,
                )
                return saved_total
            saved_total += stats["saved"]
            after = messages[-1].created_at
            await self.store.save_profile_processed_until(chat_id, after)
            logging.info(
                "Chat profile facts extracted chat_id=%s messages=%s saved=%s period=%s..%s",
                chat_id,
                len(messages),
                stats["saved"],
                messages[0].created_at,
                messages[-1].created_at,
            )

    async def blocks_for_summary(
        self,
        *,
        chat_id: int,
        since: datetime,
        until: datetime,
    ) -> list[ChatMemoryBlock]:
        return await self.store.get_memory_blocks_for_period(
            chat_id=chat_id,
            since=since,
            until=until,
        )

    async def search(
        self,
        *,
        chat_id: int,
        since: datetime,
        until: datetime,
        query: str,
    ) -> list[ChatMemoryBlock]:
        candidates = await self.store.search_memory_blocks(
            chat_id=chat_id,
            since=since,
            until=until,
            query=query,
            limit=max(self.search_limit * 4, 20),
        )
        if not candidates:
            candidates = await self.store.get_recent_memory_blocks_for_period(
                chat_id=chat_id,
                since=since,
                until=until,
                limit=max(self.search_limit * 3, 12),
            )
        if len(candidates) <= self.search_limit:
            return candidates

        keyword_ranked = self._keyword_rank(candidates, query)
        reranked = await self._rerank_blocks(keyword_ranked[: max(self.search_limit * 3, 12)], query)
        return reranked[: self.search_limit]

    async def participant_context(
        self,
        *,
        chat_id: int,
        query: str,
        participant_keys: list[str] | None = None,
        participant_names: list[str] | None = None,
        limit: int = 18,
    ) -> str:
        facts: list[ChatParticipantFact] = []
        seen: set[int] = set()

        explicit_keys = list(dict.fromkeys(participant_keys or []))
        if explicit_keys:
            for fact in await self.store.get_participant_facts(
                chat_id=chat_id,
                participant_keys=explicit_keys,
                limit=limit,
            ):
                if fact.fact_id not in seen:
                    facts.append(fact)
                    seen.add(fact.fact_id)

        for name in participant_names or []:
            for fact in await self.store.get_participant_facts(
                chat_id=chat_id,
                participant_name=name,
                limit=limit,
            ):
                if fact.fact_id not in seen:
                    facts.append(fact)
                    seen.add(fact.fact_id)

        search_facts = await self.store.search_participant_facts(
            chat_id=chat_id,
            query=query,
            participant_keys=explicit_keys or None,
            limit=limit,
        )
        for fact in search_facts:
            if fact.fact_id not in seen:
                facts.append(fact)
                seen.add(fact.fact_id)

        lines = _profile_lines(facts, limit=limit)
        if not lines:
            return ""
        return "Паспорта релевантных участников:\n" + "\n".join(lines)

    async def profile_text(
        self,
        *,
        chat_id: int,
        participant_keys: list[str] | None = None,
        participant_name: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> str:
        facts = await self.store.get_participant_facts(
            chat_id=chat_id,
            participant_keys=participant_keys,
            participant_name=participant_name,
            include_inactive=include_inactive,
            limit=limit,
        )
        if not facts:
            return "Паспорт участника пока пуст."
        lines = _profile_lines(facts, limit=limit)
        return "Паспорт участника:\n" + "\n".join(lines)

    async def forget_profile(self, *, chat_id: int, participant_keys: list[str]) -> int:
        return await self.store.mark_participant_facts_status(
            chat_id=chat_id,
            participant_keys=participant_keys,
            status="forgotten",
        )

    async def reset_blocks(self, chat_id: int) -> None:
        await self.store.reset_chat_memory(chat_id)

    async def add_profile_correction(
        self,
        *,
        chat_id: int,
        participant_key: str,
        participant_name: str,
        fact_text: str,
        source_message_id: int,
        created_at: datetime,
    ) -> None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_iso = created_at.astimezone(timezone.utc).isoformat()
        await self.store.save_participant_fact(
            chat_id=chat_id,
            participant_key=participant_key,
            participant_name=participant_name,
            fact_type="other",
            fact_text=fact_text,
            source_message_ids=[source_message_id],
            confidence="high",
            first_seen_at=created_at_iso,
            last_seen_at=created_at_iso,
            expires_at=None,
        )

    async def status(self, chat_id: int) -> dict[str, object]:
        cutoff = self.recent_since()
        processed_until = await self.store.get_memory_processed_until(chat_id)
        profile_processed_until = await self.store.get_profile_processed_until(chat_id)
        return {
            "memory_blocks": await self.store.count_memory_blocks(chat_id),
            "chunk_blocks": await self.store.count_memory_blocks(chat_id, level="chunk"),
            "rollup_blocks": await self.store.count_memory_blocks(chat_id, level="rollup"),
            "archive_blocks": await self.store.count_memory_blocks(chat_id, level="archive"),
            "participant_facts": await self.store.count_participant_facts(chat_id),
            "processed_until": processed_until or "none",
            "profile_processed_until": profile_processed_until or "none",
            "latest_raw_messages": await self.store.count_messages_since(
                chat_id=chat_id,
                since=cutoff,
            ),
            "pending_old_messages": await self.store.count_messages_before_after(
                chat_id=chat_id,
                before=cutoff,
                after=processed_until,
            ),
            "recent_period": str(self.recent_period),
            "chunk_chars": self.chunk_chars,
            "profile_chunk_chars": self.profile_chunk_chars,
            "max_blocks_per_level": self.max_blocks,
            "search_limit": self.search_limit,
        }

    async def unload(self) -> None:
        await self.llm.unload()

    def blocks_as_messages(self, blocks: list[ChatMemoryBlock]) -> list[StoredMessage]:
        messages: list[StoredMessage] = []
        for block in blocks:
            messages.append(
                StoredMessage(
                    message_id=-block.block_id,
                    chat_id=block.chat_id,
                    chat_type="memory",
                    sender_id=None,
                    sender_name="ChatMemory",
                    text=_block_text(block),
                    created_at=block.period_end,
                    reply_to_message_id=None,
                )
            )
        return messages

    def participant_context_as_message(self, chat_id: int, text: str) -> StoredMessage:
        return StoredMessage(
            message_id=-900_000_000,
            chat_id=chat_id,
            chat_type="memory",
            sender_id=None,
            sender_name="ParticipantProfiles",
            text=text,
            created_at=datetime.now(timezone.utc).isoformat(),
            reply_to_message_id=None,
        )

    @opik_track(name="memory.compress")
    async def _compress_messages(self, messages: list[StoredMessage]) -> dict[str, object]:
        rendered = _render_messages(messages)
        update_opik_span_metadata(
            {
                "message_count": len(messages),
                "input_chars": len(rendered),
                "level": "chunk",
            }
        )
        user = f"""
Сожми старую часть Telegram-чата в один memory-блок.

Верни строго JSON без markdown в таком формате:
{{
  "summary": "короткая фактическая выжимка до 1500-2500 символов",
  "decisions": [{{"text": "решение", "source_message_ids": [123]}}],
  "tasks": [{{"text": "задача/обещание", "owner": "имя", "source_message_ids": [123]}}],
  "open_questions": [{{"text": "открытый вопрос", "source_message_ids": [123]}}],
  "important_events": [{{"text": "важное событие", "source_message_ids": [123]}}],
  "participant_facts": [
    {{
      "participant_key": "точный sender_key участника",
      "participant_name": "имя участника",
      "fact_type": "role|responsibility|project|preference|constraint|task|temporary_state|skill|relationship|other",
      "fact": "атомарный факт о человеке",
      "source_message_ids": [123],
      "confidence": "high|medium|low",
      "expires_after_days": 30
    }}
  ],
  "topics": ["тема 1", "тема 2"],
  "keywords": ["ключевое слово", "имя участника", "проект"]
}}

Правила для participant_facts:
- сохраняй только атомарные факты, явно подтвержденные сообщениями;
- не сохраняй факт без source_message_ids;
- participant_key должен точно совпадать с sender_key из сообщения;
- если факт про участника не подтвержден явно, не добавляй его;
- low confidence лучше не добавлять, кроме очень полезных, но сомнительных фактов;
- временные состояния и задачи должны иметь expires_after_days;
- шутки, сарказм, оскорбления и догадки не являются фактами о человеке.

Что сохранить в memory-блоке:
- важные темы и контекст;
- решения и договоренности;
- задачи, ответственных и сроки, только если они явно есть;
- открытые вопросы и разногласия;
- важные факты о людях, проектах, планах.

Сообщения:
{rendered}
""".strip()
        compact_user = f"""
Верни строго один JSON-объект без markdown. Если данных для раздела нет, верни пустой список.
Поля: summary, decisions, tasks, open_questions, important_events, participant_facts, topics, keywords.
participant_facts добавляй только с точным participant_key и source_message_ids из сообщений.

Сообщения:
{rendered}
""".strip()
        last_error = ""
        for attempt, prompt in enumerate([user, compact_user], start=1):
            response = await self.llm.complete(
                system=MEMORY_SYSTEM_PROMPT,
                user=prompt,
                response_format="json",
            )
            data = _json_object(response)
            if not data:
                update_opik_span_metadata(
                    {
                        "json_valid": False,
                        "failed_attempt": attempt,
                        "invalid_output_chars": len(response),
                    }
                )
                last_error = "Memory compression did not return JSON."
                logging.warning(
                    "Memory compression returned non-JSON response attempt=%s response_chars=%s",
                    attempt,
                    len(response),
                )
                continue
            data["summary"] = str(data.get("summary") or response[:2500]).strip()[:2500]
            try:
                _validate_memory_data(data, message_count=len(messages))
            except MemoryCompressionError as exc:
                last_error = str(exc)
                update_opik_span_metadata(
                    {
                        "json_valid": True,
                        "rejected_attempt": attempt,
                        "participant_facts_returned": len(
                            _object_list(data.get("participant_facts"))
                        ),
                    }
                )
                logging.warning("Memory compression rejected JSON response attempt=%s: %s", attempt, exc)
                continue
            update_opik_span_metadata(
                {
                    "json_valid": True,
                    "attempts": attempt,
                    "participant_facts_returned": len(_object_list(data.get("participant_facts"))),
                }
            )
            return data
        raise MemoryCompressionError(
            f"{last_error} "
            "Increase OLLAMA_NUM_CTX, lower MEMORY_CHUNK_CHARS, or restart Ollama before rebuilding memory."
        )

    @opik_track(name="memory.rollup")
    async def _compress_blocks(
        self,
        blocks: list[ChatMemoryBlock],
        *,
        target_level: str,
    ) -> dict[str, object]:
        rendered = "\n\n---\n\n".join(_block_text(block) for block in blocks)
        update_opik_span_metadata(
            {
                "block_count": len(blocks),
                "input_chars": len(rendered),
                "level": target_level,
            }
        )
        user = f"""
Сожми несколько уже существующих memory-блоков в один блок уровня {target_level}.
Не добавляй новых фактов, которых нет в блоках. Если сведения противоречат друг другу,
сохрани это как открытое противоречие, а не выбирай произвольно одну версию.

Верни строго JSON без markdown в формате:
{{
  "summary": "краткая выжимка до 2500 символов",
  "decisions": [],
  "tasks": [],
  "open_questions": [],
  "important_events": [],
  "participant_facts": [],
  "topics": [],
  "keywords": []
}}

Блоки:
{rendered}
""".strip()
        response = await self.llm.complete(
            system=MEMORY_SYSTEM_PROMPT,
            user=user,
            response_format="json",
        )
        data = _json_object(response)
        if not data:
            return {"summary": response[:2500], "topics": [], "keywords": []}
        data["summary"] = str(data.get("summary") or response[:2500]).strip()[:2500]
        return data

    @opik_track(name="profile.extract")
    async def _extract_participant_facts(
        self,
        chat_id: int,
        messages: list[StoredMessage],
    ) -> dict[str, int]:
        rendered = _render_messages(messages)
        update_opik_span_metadata(
            {
                "message_count": len(messages),
                "input_chars": len(rendered),
                "profile_chunk_chars": self.profile_chunk_chars,
            }
        )
        user = f"""
Извлеки только полезные факты о конкретных участниках Telegram-чата.

Верни строго JSON без markdown в формате:
{{
  "participant_facts": [
    {{
      "participant_key": "точный sender_key участника",
      "participant_name": "имя участника",
      "fact_type": "role|responsibility|project|preference|constraint|task|temporary_state|skill|relationship|other",
      "fact": "информативный атомарный факт в 1-2 предложениях",
      "source_message_ids": [123],
      "confidence": "high|medium",
      "expires_after_days": 30
    }}
  ]
}}

Правила:
- сохраняй только факты, явно подтвержденные сообщениями;
- fact должен быть понятным без исходного сообщения, не короче короткой фразы;
- participant_key должен точно совпадать с sender_key из сообщения;
- source_message_ids обязательны и должны ссылаться на сообщения ниже;
- не добавляй low confidence;
- шутки, оскорбления, сарказм и временные эмоции не являются фактами о человеке;
- временные состояния и задачи сохраняй только если они полезны позже, с expires_after_days;
- если фактов нет, верни пустой массив.

Сообщения:
{rendered}
""".strip()
        response = await self.llm.complete(
            system=MEMORY_SYSTEM_PROMPT,
            user=user,
            response_format="json",
        )
        data = _json_object(response)
        if not data:
            update_opik_span_metadata(
                {
                    "json_valid": False,
                    "invalid_output_chars": len(response),
                    "participant_facts_returned": 0,
                    "participant_facts_saved": 0,
                }
            )
            stats = _empty_fact_stats()
            stats["invalid_json"] = 1
            return stats

        stats = await self._save_participant_facts(chat_id, data, messages)
        update_opik_span_metadata(
            {
                "json_valid": True,
                "participant_facts_returned": stats["returned"],
                "participant_facts_saved": stats["saved"],
                "participant_facts_rejected_no_text": stats["rejected_no_text"],
                "participant_facts_rejected_too_short": stats["rejected_too_short"],
                "participant_facts_rejected_no_source": stats["rejected_no_source"],
                "participant_facts_rejected_low_confidence": stats[
                    "rejected_low_confidence"
                ],
                "participant_facts_rejected_invalid_key": stats["rejected_invalid_key"],
            }
        )
        return stats

    async def _save_participant_facts(
        self,
        chat_id: int,
        data: dict[str, object],
        messages: list[StoredMessage],
    ) -> dict[str, int]:
        message_by_id = {message.message_id: message for message in messages if message.message_id > 0}
        stats = _empty_fact_stats()
        for raw in _object_list(data.get("participant_facts")):
            stats["returned"] += 1
            fact_text = str(raw.get("fact") or raw.get("fact_text") or raw.get("text") or "").strip()
            if not fact_text:
                stats["rejected_no_text"] += 1
                continue
            if len(fact_text) < 12:
                stats["rejected_too_short"] += 1
                continue
            source_ids = [item for item in _int_list(raw.get("source_message_ids")) if item in message_by_id]
            if not source_ids:
                stats["rejected_no_source"] += 1
                continue

            fact_type = str(raw.get("fact_type") or "other").strip().lower()
            if fact_type not in FACT_TYPES:
                fact_type = "other"

            confidence = str(raw.get("confidence") or "medium").strip().lower()
            if confidence not in {"high", "medium", "low"} or confidence == "low":
                stats["rejected_low_confidence"] += 1
                continue

            key = str(raw.get("participant_key") or "").strip()
            source_senders = {
                participant_key(message_by_id[source_id].sender_id, message_by_id[source_id].sender_name)
                for source_id in source_ids
            }
            if not key and len(source_senders) == 1:
                key = next(iter(source_senders))
            if key not in source_senders and key not in {
                participant_key(message.sender_id, message.sender_name) for message in messages
            }:
                stats["rejected_invalid_key"] += 1
                continue

            name = str(raw.get("participant_name") or "").strip()
            if not name:
                name = _participant_name_for_key(messages, key)

            source_dates = [_parse_datetime(message_by_id[source_id].created_at) for source_id in source_ids]
            first_seen = min(source_dates).isoformat()
            last_seen = max(source_dates).isoformat()
            await self.store.save_participant_fact(
                chat_id=chat_id,
                participant_key=key,
                participant_name=name,
                fact_type=fact_type,
                fact_text=fact_text[:800],
                source_message_ids=source_ids,
                confidence=confidence,
                first_seen_at=first_seen,
                last_seen_at=last_seen,
                expires_at=_expires_at(fact_type, last_seen, raw),
            )
            stats["saved"] += 1
        return stats

    async def _maintain_hierarchy(self, chat_id: int) -> None:
        await self._rollup_level(chat_id, source_level="chunk", target_level="rollup")
        await self._rollup_level(chat_id, source_level="rollup", target_level="archive")
        await self._rollup_level(chat_id, source_level="archive", target_level="archive")

    async def _rollup_level(self, chat_id: int, *, source_level: str, target_level: str) -> None:
        while await self.store.count_memory_blocks(chat_id, level=source_level) > self.max_blocks:
            blocks = await self.store.get_oldest_memory_blocks(
                chat_id=chat_id,
                level=source_level,
                limit=ROLLUP_GROUP_SIZE,
            )
            if len(blocks) < 2:
                return
            data = await self._compress_blocks(blocks, target_level=target_level)
            await self.store.save_memory_block(
                chat_id=chat_id,
                period_start=blocks[0].period_start,
                period_end=blocks[-1].period_end,
                summary=str(data.get("summary") or "")[:2500],
                topics=_string_list(data.get("topics"))[:1000],
                keywords=_string_list(data.get("keywords"))[:1000],
                message_count=sum(block.message_count for block in blocks),
                structured_json=_safe_json(data),
                level=target_level,
                processed_until=None,
            )
            await self.store.delete_memory_blocks([block.block_id for block in blocks])
            logging.info(
                "Chat memory rollup created chat_id=%s source=%s target=%s blocks=%s",
                chat_id,
                source_level,
                target_level,
                len(blocks),
            )

    def _keyword_rank(self, blocks: list[ChatMemoryBlock], query: str) -> list[ChatMemoryBlock]:
        terms = _tokens(query)
        if not terms:
            return blocks
        scored: list[tuple[int, ChatMemoryBlock]] = []
        for block in blocks:
            haystack = " ".join(
                [block.summary, block.topics, block.keywords, block.structured_json]
            ).lower()
            score = 0
            for term in terms:
                score += block.keywords.lower().count(term) * 4
                score += block.topics.lower().count(term) * 2
                score += haystack.count(term)
            scored.append((score, block))
        scored.sort(key=lambda item: (item[0], item[1].period_end), reverse=True)
        return [block for _, block in scored]

    @opik_track(name="memory.rerank")
    async def _rerank_blocks(self, blocks: list[ChatMemoryBlock], query: str) -> list[ChatMemoryBlock]:
        if len(blocks) <= self.search_limit:
            return blocks
        rendered = "\n\n".join(
            f"block_id={block.block_id}\n{_block_text(block)[:1200]}" for block in blocks
        )
        update_opik_span_metadata(
            {
                "candidate_count": len(blocks),
                "query_chars": len(query),
            }
        )
        user = f"""
Выбери memory-блоки, которые реально помогут ответить на вопрос.
Верни строго JSON без markdown: {{"block_ids": [1, 2, 3]}}
Не выбирай блок только из-за совпадения случайного слова.
Максимум блоков: {self.search_limit}.

Вопрос:
{query}

Кандидаты:
{rendered}
""".strip()
        response = await self.llm.complete(
            system=MEMORY_SYSTEM_PROMPT,
            user=user,
            response_format="json",
        )
        data = _json_object(response)
        ids = _int_list(data.get("block_ids") if data else None)
        if not ids:
            return blocks
        by_id = {block.block_id: block for block in blocks}
        selected = [by_id[block_id] for block_id in ids if block_id in by_id]
        return selected + [block for block in blocks if block.block_id not in ids]


def should_use_memory(period: timedelta, memory: ChatMemory | None) -> bool:
    return memory is not None and period > memory.recent_period
