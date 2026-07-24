from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from tg_summary_bot.llm import LLMClient
from tg_summary_bot.periods import parse_period


Action = Literal[
    "question", "summary", "wiki", "image", "meme", "video", "transcribe", "profile_show", "none"
]
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"question", "summary", "wiki", "image", "meme", "video", "transcribe", "profile_show", "none"}
)
MAX_ROUTER_INPUT_CHARS = 2000
MAX_QUERY_CHARS = 500
MAX_PERIOD_CHARS = 32
MAX_PERIOD = timedelta(days=365)
ROUTER_TIMEOUT_SECONDS = 90
MEME_REQUEST_RE = re.compile(r"\b(?:мем\w*|meme\w*)\b", re.IGNORECASE)
JOKE_REQUEST_RE = re.compile(r"\b(?:пошути\w*|шутк\w*|рассмеши\w*)\b", re.IGNORECASE)

ROUTER_SYSTEM_PROMPT = """Ты маршрутизатор действий Telegram-бота. Верни только один JSON object без Markdown и текста.
Схема: {"action":"question|summary|wiki|image|meme|video|transcribe|profile_show|none","period":string|null,"query":string|null}.
Выбирай summary для просьб о саммари, wiki для поиска в Wikipedia, image для OCR/описания картинки,
meme для мема из картинки, video для анализа видео, transcribe для расшифровки replied voice/audio,
profile_show для просмотра профиля участника. Обычные вопросы и сомнения: question. Не добавляй ключи действий вне схемы.
Выбирай meme только если пользователь явно просит мем или meme. Просьба «пошути», «расскажи шутку» или
«рассмеши» всегда означает question, даже если сообщение является reply на картинку.
Примеры: просьба «саммари за 6 часов» -> {"action":"summary","period":"6h","query":null};
«найди в википедии Ada Lovelace» -> {"action":"wiki","period":null,"query":"Ada Lovelace"};
«просто расскажи шутку» -> {"action":"question","period":null,"query":null}."""


@dataclass(frozen=True)
class IntentRoute:
    action: Action
    period: str | None = None
    query: str | None = None
    valid: bool = True
    reason: str = "ok"


def fallback_route(reason: str) -> IntentRoute:
    return IntentRoute(action="none", valid=False, reason=reason)


def is_joke_request(text: str) -> bool:
    return bool(JOKE_REQUEST_RE.search(text))


def parse_route_response(response: str) -> IntentRoute:
    """Accept exactly one JSON object matching the narrow router contract."""
    try:
        value = json.loads(response)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback_route("invalid_json")
    if not isinstance(value, dict):
        return fallback_route("not_object")

    action = value.get("action")
    period = value.get("period")
    query = value.get("query")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        return fallback_route("invalid_action")
    if period is not None and not isinstance(period, str):
        return fallback_route("invalid_period_type")
    if query is not None and not isinstance(query, str):
        return fallback_route("invalid_query_type")
    if period is not None and len(period) > MAX_PERIOD_CHARS:
        return fallback_route("period_too_long")
    if query is not None and len(query) > MAX_QUERY_CHARS:
        return fallback_route("query_too_long")
    if period is not None:
        try:
            if parse_period(period) > MAX_PERIOD:
                return fallback_route("period_too_long_range")
        except (ValueError, TypeError, OverflowError):
            return fallback_route("invalid_period")
    if action == "wiki" and not (query and query.strip()):
        return fallback_route("missing_wiki_query")
    return IntentRoute(action=action, period=period, query=query)


class IntentRouter:
    def __init__(self, client: LLMClient) -> None:
        self.client = client

    async def route(self, text: str) -> IntentRoute:
        try:
            async with asyncio.timeout(ROUTER_TIMEOUT_SECONDS):
                response = await self.client.complete(
                    system=ROUTER_SYSTEM_PROMPT,
                    user=text[:MAX_ROUTER_INPUT_CHARS],
                    response_format="json",
                )
        except TimeoutError:
            return fallback_route("timeout")
        except Exception:  # noqa: BLE001
            return fallback_route("model_error")
        route = parse_route_response(response)
        if route.action == "meme" and not MEME_REQUEST_RE.search(text):
            return fallback_route("meme_not_explicit")
        return route

    async def unload(self) -> None:
        await self.client.unload()
