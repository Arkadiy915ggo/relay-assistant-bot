"""Shared outgoing pacing; retry only explicit Telegram flood-control rejections."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType


@dataclass
class _ChatGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_request_at: float = 0.0


class TelegramDeliveryMiddleware(BaseRequestMiddleware):
    """Serialize chat writes and respect retry_after without replaying bot operations.

    3.1 seconds leaves room below Telegram's usual 20 messages/minute group
    limit. Edits share the same gate with text, Dice and media sends. Network
    failures are deliberately not retried: delivery may already have happened.
    """

    def __init__(self, *, interval: float = 3.1, max_retries: int = 3) -> None:
        self.interval = interval
        self.max_retries = max_retries
        self._chats: dict[int | str, _ChatGate] = {}

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> TelegramType:
        chat_id = getattr(method, "chat_id", None)
        paced = method.__api_method__.startswith(("send", "edit", "copy", "forward"))
        if chat_id is None or not paced:
            return await self._request(make_request, bot, method)
        gate = self._chats.setdefault(chat_id, _ChatGate())
        async with gate.lock:
            return await self._request(make_request, bot, method, gate)

    async def _request(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
        gate: _ChatGate | None = None,
    ) -> TelegramType:
        loop = asyncio.get_running_loop()
        for attempt in range(self.max_retries + 1):
            if gate:
                delay = gate.next_request_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                gate.next_request_at = loop.time() + self.interval
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                # Keep the cooldown even when retries are exhausted or cancelled.
                delay = max(0, exc.retry_after) + 0.1
                if gate:
                    gate.next_request_at = max(gate.next_request_at, loop.time() + delay)
                if attempt == self.max_retries:
                    raise
                logging.warning(
                    "Telegram delivery rate limited method=%s retry_after=%s attempt=%s",
                    method.__api_method__, exc.retry_after, attempt + 1,
                )
                if gate is None:
                    await asyncio.sleep(delay)
        raise AssertionError("unreachable")
