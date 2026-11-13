"""Bounded concurrent polling with ordered handling within each chat."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any
from weakref import WeakValueDictionary

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.types import Update


POLLING_CONCURRENCY_LIMIT = 64
POLLING_PENDING_LIMIT = 1024


class ChatOrderedDispatcher(Dispatcher):
    """Keep chat handlers ordered while other chats can progress during delivery waits."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._chat_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()
        self.shutdown.register(self._drain_pending_updates)

    async def _process_update(
        self, bot: Bot, update: Update, *,
        _concurrency_semaphore: asyncio.Semaphore | None = None, **kwargs: Any,
    ) -> bool:
        async with AsyncExitStack() as stack:
            message = update.message or update.channel_post
            if message is not None:
                # Bot IDs isolate chats when polling multiple bots with one dispatcher.
                key = (bot.id, message.chat.id)
                lock = self._chat_locks.setdefault(key, asyncio.Lock())
                await stack.enter_async_context(lock)
            # Lock waiters belong to the bounded backlog, not runnable capacity.
            # Semaphore FIFO admission lets other ready chats run before this chat's
            # next update, while the lock preserves order within each chat.
            if _concurrency_semaphore is not None:
                await stack.enter_async_context(_concurrency_semaphore)
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
        pending_slots = asyncio.Semaphore(max(
            POLLING_PENDING_LIMIT, tasks_concurrency_limit or POLLING_CONCURRENCY_LIMIT,
        ))

        def finished(task: asyncio.Task[Any]) -> None:
            pending_slots.release()
            self._handle_update_tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                logging.error("Polling handler failed", exc_info=task.exception())

        logging.info("Concurrent polling started bot_id=%s username=%s", bot.id, user.username)
        try:
            async for update in self._listen_updates(
                bot, polling_timeout=polling_timeout, backoff_config=backoff_config, allowed_updates=allowed_updates,
            ):
                # Acquire before constructing the coroutine. aiogram's stock loop creates
                # it first, leaking an unawaited coroutine if a saturated poll is cancelled.
                await pending_slots.acquire()
                task = asyncio.create_task(self._process_update(
                    bot=bot, update=update, _concurrency_semaphore=semaphore, **kwargs,
                ))
                self._handle_update_tasks.add(task)
                task.add_done_callback(finished)
        finally:
            logging.info("Concurrent polling stopped bot_id=%s", bot.id)

    async def _drain_pending_updates(self, **kwargs: Any) -> None:
        # Polling has stopped, but Telegram may already have acknowledged these
        # updates. Let even queued handlers finish before closing the HTTP session.
        # Do not impose a cancellation timeout: it would silently lose accepted work.
        tasks = list(self._handle_update_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
