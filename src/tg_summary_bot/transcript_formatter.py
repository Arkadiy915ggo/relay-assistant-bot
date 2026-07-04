from __future__ import annotations

from tg_summary_bot.llm import LLMClient


TRANSCRIPT_FORMAT_SYSTEM_PROMPT = """
Ты аккуратно форматируешь русскую расшифровку голосового сообщения.
Сохраняй смысл, порядок мыслей, имена, факты и формулировки автора.
Не добавляй новые факты, выводы, заголовки от себя и краткое содержание.
Разрешено только:
- разбить текст на абзацы;
- добавить простой список, если автор явно перечисляет пункты;
- убрать явные паразитные звуки и заполнители вроде "э-э", "эм", "мм", "как бы" только когда они не несут смысла;
- слегка поправить пунктуацию и очевидные повторы распознавания.
Верни только отредактированную расшифровку без комментариев.
""".strip()


class TranscriptFormatter:
    def __init__(self, llm: LLMClient, *, model_name: str, max_chars: int) -> None:
        self.llm = llm
        self.model_name = model_name
        self.max_chars = max_chars

    async def format(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return ""
        if len(text) > self.max_chars:
            raise RuntimeError(
                f"Transcript is too long to format: {len(text)} chars. "
                f"Limit: {self.max_chars} chars."
            )

        user = f"""
Отформатируй расшифровку. Не сокращай и не пересказывай текст.

Расшифровка:
{text}
""".strip()
        return await self.llm.complete(system=TRANSCRIPT_FORMAT_SYSTEM_PROMPT, user=user)

    async def unload(self) -> None:
        await self.llm.unload()
