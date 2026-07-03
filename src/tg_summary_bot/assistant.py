from __future__ import annotations

from tg_summary_bot.llm import LLMClient
from tg_summary_bot.storage import StoredMessage


CHAT_SYSTEM_PROMPT = """
Ты дружелюбный ассистент в Telegram-чате.
Отвечай по-русски, понятно и по делу.
Если история чата помогает ответить — используй ее как контекст.
Если история чата не помогает, отвечай как обычный ассистент.
Не выдумывай факты о чате, участниках, решениях и договоренностях.
Если ссылаешься на историю чата, отделяй это от общего ответа.
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


class ChatAssistant:
    def __init__(self, llm: LLMClient, chunk_chars: int) -> None:
        self.llm = llm
        self.chunk_chars = chunk_chars

    async def ask(self, messages: list[StoredMessage], period_label: str, question: str) -> str:
        if not messages:
            return await self._answer_from_context("", period_label, question)

        rendered = _render_messages(messages)
        chunks = _split_text(rendered, self.chunk_chars)

        if len(chunks) == 1:
            return await self._answer_from_context(chunks[0], period_label, question)

        notes: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            note = await self._extract_relevant_context(
                chunk,
                period_label,
                question,
                chunk_index=index,
                chunk_count=len(chunks),
            )
            notes.append(note)

        return await self._answer_from_context(
            "\n\n---\n\n".join(notes),
            period_label,
            question,
        )

    async def unload(self) -> None:
        await self.llm.unload()

    async def _extract_relevant_context(
        self,
        chunk: str,
        period_label: str,
        question: str,
        *,
        chunk_index: int,
        chunk_count: int,
    ) -> str:
        user = f"""
Пользователь хочет пообщаться с ассистентом и задал вопрос:
{question}

Ниже часть {chunk_index}/{chunk_count} сообщений за период {period_label}.
Вытащи только факты, цитаты и контекст из истории чата, которые могут помочь ответить.
Если история в этой части не помогает, напиши: «Нет релевантного контекста».
Не отвечай на вопрос полностью, только подготовь релевантные заметки из чата.

Сообщения:
{chunk}
""".strip()
        return await self.llm.complete(system=CHAT_SYSTEM_PROMPT, user=user)

    async def _answer_from_context(self, context: str, period_label: str, question: str) -> str:
        context_block = context or "История чата за выбранный период пуста."
        user = f"""
Пользователь задал вопрос в Telegram-чате.

Вопрос:
{question}

Правила:
- отвечай по-русски;
- отвечай естественно, как в диалоге;
- используй историю чата за период {period_label}, если она релевантна;
- если история чата не релевантна, не притягивай ее насильно
  и отвечай как обычный ассистент;
- не выдумывай факты о чате, участниках, решениях и договоренностях;
- если отвечаешь по истории чата, кратко укажи, на что опираешься.

История/контекст чата:
{context_block}
""".strip()
        return await self.llm.complete(system=CHAT_SYSTEM_PROMPT, user=user)
