"""Bounded concurrent polling with ordered handling within each chat."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from weakref import WeakValueDictionary

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.types import Update


POLLING_CONCURRENCY_LIMIT = 64


class ChatOrderedDispatcher(Dispatcher):
    """Keep chat handlers ordered while other chats can progress during delivery waits."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._chat_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.shutdown.register(self._cancel_pending_updates)

    async def _process_update(self, bot: Bot, update: Update, **kwargs: Any) -> bool:
        message = update.message or update.channel_post
        if message is None:
            return await super()._process_update(bot, update, **kwargs)
        lock = self._chat_locks.setdefault(message.chat.id, asyncio.Lock())
        async with lock:
            return await super()._process_update(bot, update, **kwargs)

    async def _polling(
        self, bot, polling_timeout=30, handle_as_tasks=True, backoff_config=DEFAULT_BACKOFF_CONFIG,
        allowed_updates=None, tasks_concurrency_limit=POLLING_CONCURRENCY_LIMIT, **kwargs: Any,
    ) -> None:
        if not handle_as_tasks:
            return await super()._polling(
                bot, polling_timeout=polling_timeout, handle_as_tasks=False,
                backoff_config=backoff_config, allowed_updates=allowed_updates, **kwargs,
            )
        user = await bot.me()
        semaphore = asyncio.Semaphore(tasks_concurrency_limit or POLLING_CONCURRENCY_LIMIT)

        def finished(task: asyncio.Task[Any]) -> None:
            semaphore.release()
            self._handle_update_tasks.discard(task)

        logging.info("Concurrent polling started bot_id=%s username=%s", bot.id, user.username)
        try:
            async for update in self._listen_updates(
                bot, polling_timeout=polling_timeout, backoff_config=backoff_config, allowed_updates=allowed_updates,
            ):
                # Acquire before constructing the coroutine. aiogram's stock loop creates
                # it first, leaking an unawaited coroutine if a saturated poll is cancelled.
                await semaphore.acquire()
                task = asyncio.create_task(self._process_update(bot=bot, update=update, **kwargs))
                self._handle_update_tasks.add(task)
                task.add_done_callback(finished)
        finally:
            logging.info("Concurrent polling stopped bot_id=%s", bot.id)
    async def _cancel_pending_updates(self, **kwargs: Any) -> None:
        # aiogram closes the HTTP session after shutdown callbacks. Finish cancellation
        # (including casino refunds and media cleanup) before that session is closed.
        tasks = list(self._handle_update_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
