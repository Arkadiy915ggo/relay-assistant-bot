from __future__ import annotations

import re

from tg_summary_bot.llm import LLMClient
from tg_summary_bot.observability import opik_track, update_opik_span_metadata


TRANSCRIPT_FORMAT_PROMPT_VERSION = "v4-no-russian-translation"


TRANSCRIPT_FORMAT_SYSTEM_PROMPT = """
Ты аккуратно форматируешь расшифровку голосового сообщения или аудиодорожки.
Сохраняй смысл, порядок мыслей, имена, факты и формулировки автора.
Не добавляй новые факты, выводы, заголовки от себя и краткое содержание.
Разрешено только:
- разбить текст на абзацы;
- добавить простой список, если автор явно перечисляет пункты;
- убрать явные паразитные звуки и заполнители вроде "э-э", "эм", "мм", "yyy", "um", "как бы" только когда они не несут смысла;
- слегка поправить пунктуацию и очевидные повторы распознавания.

Если весь текст на русском, верни только отредактированную расшифровку без заголовков.
Не добавляй секцию «Перевод на русский» для русского текста. Запрещено переводить русский на русский.
Если сомневаешься, русский ли текст, считай его русским и не добавляй перевод.
Если есть польский, английский или другой нерусский язык, верни:
**Оригинал**
<аккуратно отформатированный исходный текст на оригинальном языке>

**Перевод на русский**
<точный перевод нерусских фрагментов на русский>

Если текст смешанный, в оригинале сохрани все языки, а в переводе переведи только нерусские фрагменты; русские фрагменты не дублируй.
Верни только готовую расшифровку без комментариев.
""".strip()


TRANSCRIPT_TRANSLATE_SYSTEM_PROMPT = """
Ты переводишь расшифровку аудио на русский язык.
Не сокращай, не пересказывай и не добавляй новые факты.
Сохрани оригинальный текст и дай точный русский перевод.
Верни строго такой формат:

**Оригинал**
<исходная расшифровка>

**Перевод на русский**
<точный перевод на русский>
""".strip()


class TranscriptFormatter:
    def __init__(self, llm: LLMClient, *, model_name: str, max_chars: int) -> None:
        self.llm = llm
        self.model_name = model_name
        self.max_chars = max_chars

    @property
    def cache_key(self) -> str:
        return f"transcript_format={TRANSCRIPT_FORMAT_PROMPT_VERSION}:{self.model_name}"

    @opik_track(name="transcript.format")
    async def format(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return ""
        if len(text) > self.max_chars:
            raise RuntimeError(
                f"Transcript is too long to format: {len(text)} chars. "
                f"Limit: {self.max_chars} chars."
            )
        update_opik_span_metadata(
            {
                "model": self.model_name,
                "transcript_chars": len(text),
                "max_chars": self.max_chars,
            }
        )

        user = f"""
Отформатируй расшифровку. Не сокращай и не пересказывай текст.
Если расшифровка уже на русском, не добавляй заголовки и не добавляй перевод.
Если расшифровка содержит польский или другой нерусский язык, добавь перевод на русский по правилам системной инструкции.

Расшифровка:
{text}
""".strip()
        formatted = await self.llm.complete(system=TRANSCRIPT_FORMAT_SYSTEM_PROMPT, user=user)
        formatted = remove_unneeded_russian_translation(text, formatted)
        if looks_non_russian(text) and not has_russian_translation(formatted):
            return await self.translate_to_russian(text)
        return formatted

    @opik_track(name="transcript.translate", type="llm")
    async def translate_to_russian(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return ""
        if len(text) > self.max_chars:
            raise RuntimeError(
                f"Transcript is too long to translate: {len(text)} chars. "
                f"Limit: {self.max_chars} chars."
            )
        update_opik_span_metadata(
            {
                "model": self.model_name,
                "transcript_chars": len(text),
                "max_chars": self.max_chars,
            }
        )

        user = f"""
Переведи расшифровку на русский. Не сокращай и не пересказывай текст.

Расшифровка:
{text}
""".strip()
        return await self.llm.complete(system=TRANSCRIPT_TRANSLATE_SYSTEM_PROMPT, user=user)

    async def unload(self) -> None:
        await self.llm.unload()


def has_russian_translation(text: str) -> bool:
    return "перевод на русский" in text.lower()


def remove_unneeded_russian_translation(source: str, formatted: str) -> str:
    if not looks_mostly_russian(source) or not has_russian_translation(formatted):
        return formatted

    translation_header = re.search(r"(?im)^\s*\**\s*перевод\s+на\s+русский\s*\**\s*:?\s*$", formatted)
    if not translation_header:
        return formatted

    cleaned = formatted[: translation_header.start()].strip()
    cleaned = re.sub(r"(?im)^\s*\**\s*оригинал\s*\**\s*:?\s*\n?", "", cleaned, count=1).strip()
    return cleaned or source.strip()


def looks_non_russian(text: str) -> bool:
    latin, cyrillic = count_script_letters(text)
    return latin >= 12 and latin > cyrillic * 2


def looks_mostly_russian(text: str) -> bool:
    latin, cyrillic = count_script_letters(text)
    return cyrillic >= 12 and cyrillic >= latin * 2


def count_script_letters(text: str) -> tuple[int, int]:
    latin = 0
    cyrillic = 0
    polish_extra = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
    for char in text:
        lower = char.lower()
        if "a" <= lower <= "z" or char in polish_extra:
            latin += 1
        elif "а" <= lower <= "я" or lower == "ё":
            cyrillic += 1
    return latin, cyrillic
