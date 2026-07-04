from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from tg_summary_bot.config import Settings
from tg_summary_bot.llm import LLMClient
from tg_summary_bot.periods import parse_period
from tg_summary_bot.storage import ChatMemoryBlock, MessageStore, StoredMessage


MEMORY_SYSTEM_PROMPT = """
Ты аккуратно сжимаешь историю Telegram-чата в долговременную память.
Пиши по-русски, кратко и фактически.
Не выдумывай решения, ответственных, даты и договоренности.
Сохраняй имена участников, явные обещания, важные темы, решения и открытые вопросы.
""".strip()


def _render_messages(messages: list[StoredMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        reply = f" reply_to={msg.reply_to_message_id}" if msg.reply_to_message_id else ""
        lines.append(f"[{msg.created_at} #{msg.message_id}{reply}] {msg.sender_name}: {msg.text}")
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


def _string_list(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[\wа-яА-ЯёЁ]{3,}", text)]


class ChatMemory:
    def __init__(self, store: MessageStore, llm: LLMClient, settings: Settings) -> None:
        self.store = store
        self.llm = llm
        self.recent_period = parse_period(settings.memory_recent_period)
        self.chunk_chars = settings.memory_chunk_chars
        self.max_blocks = settings.memory_max_blocks
        self.search_limit = settings.memory_search_limit

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
                return created_blocks

            summary, topics, keywords = await self._compress(messages)
            period_start = messages[0].created_at
            period_end = messages[-1].created_at
            await self.store.save_memory_block(
                chat_id=chat_id,
                period_start=period_start,
                period_end=period_end,
                summary=summary,
                topics=topics,
                keywords=keywords,
                message_count=len(messages),
                processed_until=period_end,
                max_blocks=self.max_blocks,
            )
            processed = period_end
            created_blocks += 1
            logging.info(
                "Chat memory block created chat_id=%s messages=%s period=%s..%s",
                chat_id,
                len(messages),
                period_start,
                period_end,
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
        candidates = await self.store.get_recent_memory_blocks_for_period(
            chat_id=chat_id,
            since=since,
            until=until,
            limit=self.max_blocks,
        )
        terms = _tokens(query)
        if not terms:
            return candidates[: self.search_limit]

        scored: list[tuple[int, ChatMemoryBlock]] = []
        for block in candidates:
            keywords = block.keywords.lower()
            topics = block.topics.lower()
            summary = block.summary.lower()
            score = 0
            for term in terms:
                score += keywords.count(term) * 3
                score += topics.count(term) * 2
                score += summary.count(term)
            if score:
                scored.append((score, block))

        if not scored:
            return candidates[: self.search_limit]

        scored.sort(key=lambda item: (item[0], item[1].period_end), reverse=True)
        return [block for _, block in scored[: self.search_limit]]

    async def status(self, chat_id: int) -> dict[str, object]:
        cutoff = self.recent_since()
        processed_until = await self.store.get_memory_processed_until(chat_id)
        return {
            "memory_blocks": await self.store.count_memory_blocks(chat_id),
            "processed_until": processed_until or "none",
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
            "max_blocks": self.max_blocks,
            "search_limit": self.search_limit,
        }

    async def unload(self) -> None:
        await self.llm.unload()

    def blocks_as_messages(self, blocks: list[ChatMemoryBlock]) -> list[StoredMessage]:
        messages: list[StoredMessage] = []
        for block in blocks:
            text = (
                f"Сжатая память чата за {block.period_start}..{block.period_end}.\n"
                f"Темы: {block.topics or 'не выделены'}\n"
                f"Ключевые слова: {block.keywords or 'не выделены'}\n"
                f"Сообщений в блоке: {block.message_count}\n\n"
                f"{block.summary}"
            )
            messages.append(
                StoredMessage(
                    message_id=-block.block_id,
                    chat_id=block.chat_id,
                    chat_type="memory",
                    sender_name="ChatMemory",
                    text=text,
                    created_at=block.period_end,
                    reply_to_message_id=None,
                )
            )
        return messages

    async def _compress(self, messages: list[StoredMessage]) -> tuple[str, str, str]:
        rendered = _render_messages(messages)
        user = f"""
Сожми старую часть Telegram-чата в один memory-блок.

Верни строго JSON без markdown в таком формате:
{{
  "summary": "короткая фактическая выжимка до 1500-2500 символов",
  "topics": ["тема 1", "тема 2"],
  "keywords": ["ключевое слово", "имя участника", "проект"]
}}

Что сохранить:
- важные темы и контекст;
- решения и договоренности;
- задачи, ответственных и сроки, только если они явно есть;
- открытые вопросы и разногласия;
- важные факты о людях, проектах, планах;
- заметные шутки или цитаты, только если они могут быть полезны позже.

Сообщения:
{rendered}
""".strip()
        response = await self.llm.complete(system=MEMORY_SYSTEM_PROMPT, user=user)
        data = _json_object(response)
        if not data:
            return response[:2500], "", ""

        summary = str(data.get("summary") or "").strip()[:2500]
        topics = _string_list(data.get("topics"))[:1000]
        keywords = _string_list(data.get("keywords"))[:1000]
        if not summary:
            summary = response[:2500]
        return summary, topics, keywords


def should_use_memory(period: timedelta, memory: ChatMemory | None) -> bool:
    return memory is not None and period > memory.recent_period
