from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_EDGE_PUNCTUATION = " \t\r\n,;:!?.-—–"


def normalize_alias(value: str) -> str:
    """Normalize human-entered names without joining separate words."""
    value = unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
    return " ".join(_TOKEN_RE.findall(value))


def split_aliases(value: str) -> list[tuple[str, str]]:
    aliases: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_alias in value.split(","):
        alias = raw_alias.strip()
        normalized = normalize_alias(alias)
        if normalized and normalized not in seen:
            aliases.append((alias, normalized))
            seen.add(normalized)
    return aliases


def _tokens_with_spans(text: str) -> list[tuple[str, int, int]]:
    return [
        (normalize_alias(match.group()), match.start(), match.end())
        for match in _TOKEN_RE.finditer(unicodedata.normalize("NFKC", text))
    ]


def _within_one_edit(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right, strict=True)) <= 1

    if len(left) > len(right):
        left, right = right, left
    index_left = index_right = edits = 0
    while index_left < len(left) and index_right < len(right):
        if left[index_left] == right[index_right]:
            index_left += 1
            index_right += 1
            continue
        edits += 1
        index_right += 1
        if edits > 1:
            return False
    return True


def _matches_alias(tokens: list[tuple[str, int, int]], index: int, alias_tokens: list[str]) -> bool:
    if index + len(alias_tokens) > len(tokens):
        return False
    if len(alias_tokens) > 1:
        return all(
            tokens[index + offset][0] == alias_token
            for offset, alias_token in enumerate(alias_tokens)
        )

    token = tokens[index][0]
    alias = alias_tokens[0]
    return token == alias or (len(alias) >= 5 and _within_one_edit(token, alias))


def remove_aliases_from_text(text: str, normalized_aliases: Iterable[str]) -> str | None:
    """Return text without matching aliases, or None when none addressed the bot."""
    aliases = [alias.split() for alias in normalized_aliases if alias]
    if not aliases:
        return None

    tokens = _tokens_with_spans(text)
    spans: list[tuple[int, int]] = []
    for index in range(len(tokens)):
        matches = [alias for alias in aliases if _matches_alias(tokens, index, alias)]
        if matches:
            alias = max(matches, key=len)
            start = tokens[index][1]
            end = tokens[index + len(alias) - 1][2]
            spans.append((start, end))

    if not spans:
        return None

    cleaned: list[str] = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        cleaned.append(text[cursor:start])
        cursor = end
    cleaned.append(text[cursor:])
    return " ".join("".join(cleaned).strip(_EDGE_PUNCTUATION).split())
