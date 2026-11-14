"""Deterministic, source-backed chat search helpers (no model-generated IDs)."""

from __future__ import annotations

import re


SEARCH_REQUEST_RE = re.compile(
    r"^\s*[,;:!—–-]*\s*(?:пожалуйста[,\s]+)?"
    r"(?:найди|поищи|отыщи|разыщи|ищи|(?:выполни|сделай|проведи|запусти)\s+поиск)\b",
    re.IGNORECASE,
)


def is_chat_search_request(text: str) -> bool:
    """Require a leading explicit search instruction, never infer it from conversation."""
    match = SEARCH_REQUEST_RE.match(text)
    if not match:
        return False
    # Keep explicit external searches on the existing wiki/question routes.
    return not re.match(
        r"\s*(?:пожалуйста[,\s]+)?(?:в\s+(?:википедии|интернете|гугле|сети)|на\s+(?:ютубе|youtube)|wikipedia\b)",
        text[match.end():], re.IGNORECASE,
    )
SELF_SEARCH_RE = re.compile(r"\b(?:где\s+я|я\s+(?:(?:когда-то|раньше|тогда)\s+)?(?:писал\w*|говорил\w*|написал\w*|рассказывал\w*|упоминал\w*)|мо[ийеё]\s+сообщени\w*)\b", re.IGNORECASE)


def source_link(chat_id: int, chat_type: str, message_id: int, username: str | None = None) -> str | None:
    if chat_type not in {"supergroup", "channel"}:
        return None
    if username and re.fullmatch(r"[A-Za-z0-9_]+", username):
        return f"https://t.me/{username}/{message_id}"
    if str(chat_id).startswith("-100"):
        return f"https://t.me/c/{str(chat_id)[4:]}/{message_id}"
    return None
