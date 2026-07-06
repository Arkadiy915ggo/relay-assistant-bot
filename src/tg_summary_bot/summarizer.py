from __future__ import annotations

from tg_summary_bot.llm import LLMClient
from tg_summary_bot.observability import opik_track, update_opik_span_metadata
from tg_summary_bot.storage import StoredMessage


SYSTEM_PROMPT = """
Ты аккуратный ассистент для саммари Telegram-обсуждений.
Пиши по-русски, кратко, тезисно, без воды.
Не выдумывай факты, решения, дедлайны и ответственных.
Если данных мало или обсуждение было флудом, прямо скажи об этом.
Отделяй факты от предположений.
В категорию «Лучшая шутка» выбирай реально смешную реплику из обсуждения.
Если уместно, можешь слегка доработать формулировку шутки, но не меняй смысл и не приписывай ее другому человеку.
Если шуток не было, напиши: «Не нашлось».
""".strip()


def _render_messages(messages: list[StoredMessage]) -> str:
    lines: list[str] = []
    for msg in messages:
        reply = f" reply_to={msg.reply_to_message_id}" if msg.reply_to_message_id else ""
        lines.append(f"[{msg.created_at} #{msg.message_id}{reply}] {msg.sender_name}: {msg.text}")
    return "\n".join(lines)


def _split_text(text: str, chunk_chars: int) -> list[str]:
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > chunk_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


class Summarizer:
    def __init__(self, llm: LLMClient, chunk_chars: int) -> None:
        self.llm = llm
        self.chunk_chars = chunk_chars

    @opik_track(name="summary.summarize")
    async def summarize(self, messages: list[StoredMessage], period_label: str) -> str:
        if not messages:
            return f"За период `{period_label}` сохраненных сообщений нет."

        rendered = _render_messages(messages)
        chunks = _split_text(rendered, self.chunk_chars)
        update_opik_span_metadata(
            {
                "period_label": period_label,
                "message_count": len(messages),
                "chunk_count": len(chunks),
                "input_chars": len(rendered),
            }
        )

        if len(chunks) == 1:
            return await self._summarize_chunk(chunks[0], period_label, final=True)

        partials: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            partial = await self._summarize_chunk(
                chunk,
                period_label,
                final=False,
                chunk_index=index,
                chunk_count=len(chunks),
            )
            partials.append(partial)

        return await self._merge_summaries(partials, period_label)

    async def unload(self) -> None:
        await self.llm.unload()

    @opik_track(name="summary.chunk")
    async def _summarize_chunk(
        self,
        chunk: str,
        period_label: str,
        *,
        final: bool,
        chunk_index: int = 1,
        chunk_count: int = 1,
    ) -> str:
        update_opik_span_metadata(
            {
                "period_label": period_label,
                "final": final,
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
                "input_chars": len(chunk),
            }
        )
        if final:
            user = f"""
Сделай итоговое саммари Telegram-обсуждения за период: {period_label}.

Формат:
**Коротко**
- 3-7 главных тезисов

**Решения**
- только явно принятые решения

**Задачи**
- задача — ответственный — срок, если они явно есть

**Открытые вопросы**
- вопросы, по которым нет финального решения

**Лучшая шутка**
- автор — цитата или слегка доработанная формулировка; если шуток не было, напиши «Не нашлось»

**Шум / неважное**
- 1 строка, если был заметный флуд

Сообщения:
{chunk}
""".strip()
        else:
            user = f"""
Сожми часть {chunk_index}/{chunk_count} Telegram-обсуждения за период {period_label}.
Вытащи только важное: темы, решения, задачи, открытые вопросы, конфликты/разногласия, кандидаты на лучшую шутку.
Не делай финальный отчет, это промежуточная выжимка.

Сообщения:
{chunk}
""".strip()
        return await self.llm.complete(system=SYSTEM_PROMPT, user=user)

    @opik_track(name="summary.merge")
    async def _merge_summaries(self, partials: list[str], period_label: str) -> str:
        joined = "\n\n---\n\n".join(partials)
        update_opik_span_metadata(
            {
                "period_label": period_label,
                "partial_count": len(partials),
                "input_chars": len(joined),
            }
        )
        user = f"""
Собери финальное краткое саммари Telegram-обсуждения за период: {period_label}.
Ниже промежуточные выжимки частей длинного обсуждения.

Формат:
**Коротко**
- 3-9 главных тезисов

**Решения**
- только явно принятые решения

**Задачи**
- задача — ответственный — срок, если они явно есть

**Открытые вопросы**
- вопросы, по которым нет финального решения

**Лучшая шутка**
- автор — цитата или слегка доработанная формулировка; если шуток не было, напиши «Не нашлось»

**Что можно пропустить**
- кратко про флуд/малозначимые темы

Промежуточные выжимки:
{joined}
""".strip()
        return await self.llm.complete(system=SYSTEM_PROMPT, user=user)
