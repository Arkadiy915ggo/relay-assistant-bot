from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from tg_summary_bot.config import Settings, load_settings
from tg_summary_bot.llm import build_llm_client
from tg_summary_bot.periods import format_period, parse_period
from tg_summary_bot.storage import MessageStore
from tg_summary_bot.summarizer import Summarizer


def is_allowed(settings: Settings, chat_id: int) -> bool:
    return not settings.allowed_chat_ids or chat_id in settings.allowed_chat_ids


def message_text(message: Message) -> str:
    text = message.text or message.caption or ""
    return " ".join(text.split())


def sender_name(message: Message) -> tuple[int | None, str]:
    if message.from_user:
        user = message.from_user
        name = user.full_name or user.username or str(user.id)
        return user.id, name
    if message.sender_chat:
        return message.sender_chat.id, message.sender_chat.title or str(message.sender_chat.id)
    return None, "Unknown"


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        addition = line + "\n"
        if current and len(current) + len(addition) > limit:
            chunks.append(current.rstrip())
            current = ""
        current += addition
    if current:
        chunks.append(current.rstrip())
    return chunks or [text[:limit]]


def setup_logging(settings: Settings) -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler], force=True)


async def create_dispatcher(settings: Settings, store: MessageStore, summarizer: Summarizer) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start", "help"))
    async def help_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        await message.answer(
            "Я сохраняю новые текстовые сообщения этого чата и делаю краткие саммари.\n\n"
            "Команды:\n"
            "`/summary` — саммари за период по умолчанию\n"
            "`/summary 6h` — за 6 часов\n"
            "`/summary 7d` — за 7 дней\n"
            "`/summary today` — за сегодня UTC\n"
            "`/compare 10m` — сравнить саммари разных Ollama-моделей\n"
            "`/stats` — chat_id и число сохраненных сообщений\n\n"
            f"Текущий chat_id: `{message.chat.id}`"
        )

    @dp.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        count = await store.count_messages(message.chat.id)
        await message.answer(
            f"chat_id: `{message.chat.id}`\n"
            f"chat_type: `{message.chat.type}`\n"
            f"saved_messages: `{count}`\n"
            f"llm_provider: `{settings.resolved_llm_provider}`\n"
            f"ollama_model: `{settings.ollama_model}`\n"
            f"ollama_timeout_seconds: `{settings.ollama_timeout_seconds}`\n"
            f"ollama_num_ctx: `{settings.ollama_num_ctx}`\n"
            f"ollama_num_predict: `{settings.ollama_num_predict}`"
        )

    @dp.message(Command("summary"))
    async def summary_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return

        args = (message.text or "").split(maxsplit=1)
        period_raw = args[1].strip() if len(args) > 1 else settings.default_summary_period
        try:
            period = parse_period(period_raw)
        except ValueError as exc:
            await message.answer(f"Не понял период: {exc}")
            return

        wait_message = await message.answer("Собираю сообщения и делаю саммари...")
        since = datetime.now(timezone.utc) - period
        messages = await store.get_messages_since(
            chat_id=message.chat.id,
            since=since,
            limit_chars=settings.max_summary_input_chars,
        )
        logging.info(
            "Summary started chat_id=%s period=%s model=%s messages=%s",
            message.chat.id,
            period_raw,
            settings.ollama_model if settings.resolved_llm_provider == "ollama" else settings.openai_model,
            len(messages),
        )
        started = time.perf_counter()
        try:
            summary = await summarizer.summarize(messages, format_period(period_raw))
        except Exception as exc:  # noqa: BLE001
            logging.exception("Summary failed")
            await wait_message.edit_text(f"Не удалось сделать саммари: `{type(exc).__name__}: {exc}`")
            return
        logging.info(
            "Summary finished chat_id=%s period=%s elapsed_s=%.1f",
            message.chat.id,
            period_raw,
            time.perf_counter() - started,
        )

        header = f"**Саммари за {format_period(period_raw)}**\n"
        text = header + summary
        parts = split_telegram_text(text)
        await wait_message.edit_text(parts[0])
        for part in parts[1:]:
            await message.answer(part)

    @dp.message(Command("compare"))
    async def compare_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if settings.resolved_llm_provider != "ollama":
            await message.answer("/compare сейчас работает только с LLM_PROVIDER=ollama.")
            return
        if not settings.compare_models:
            await message.answer("COMPARE_MODELS пустой. Добавь модели в .env через запятую.")
            return

        args = (message.text or "").split(maxsplit=1)
        period_raw = args[1].strip() if len(args) > 1 else settings.default_summary_period
        try:
            period = parse_period(period_raw)
        except ValueError as exc:
            await message.answer(f"Не понял период: {exc}")
            return

        wait_message = await message.answer(
            "Собираю сообщения для сравнения моделей...\n"
            f"Модели: {', '.join(settings.compare_models)}"
        )
        since = datetime.now(timezone.utc) - period
        messages = await store.get_messages_since(
            chat_id=message.chat.id,
            since=since,
            limit_chars=settings.max_summary_input_chars,
        )
        if not messages:
            await wait_message.edit_text(f"За период {format_period(period_raw)} сохраненных сообщений нет.")
            return

        for model in settings.compare_models:
            await wait_message.edit_text(f"Сравнение моделей: сейчас работает {model}...")
            started = time.perf_counter()
            logging.info(
                "Compare started chat_id=%s period=%s model=%s messages=%s",
                message.chat.id,
                period_raw,
                model,
                len(messages),
            )
            model_summarizer = Summarizer(build_llm_client(settings, model=model), settings.chunk_chars)
            try:
                summary = await model_summarizer.summarize(messages, format_period(period_raw))
            except Exception as exc:  # noqa: BLE001
                logging.exception("Compare failed for model %s", model)
                await message.answer(
                    f"Модель {model} не смогла сделать саммари: {type(exc).__name__}: {exc}"
                )
                continue

            elapsed = time.perf_counter() - started
            logging.info(
                "Compare finished chat_id=%s period=%s model=%s elapsed_s=%.1f",
                message.chat.id,
                period_raw,
                model,
                elapsed,
            )
            text = f"Сравнение: {model}\nПериод: {format_period(period_raw)}\nВремя: {elapsed:.1f} сек\n\n{summary}"
            for part in split_telegram_text(text):
                await message.answer(part)

        await wait_message.edit_text("Сравнение моделей завершено.")

    @dp.channel_post(F.text | F.caption)
    async def save_channel_post(message: Message) -> None:
        await save_incoming_message(settings, store, message)

    @dp.message(F.text | F.caption)
    async def save_regular_message(message: Message) -> None:
        if message.text and message.text.startswith("/"):
            return
        await save_incoming_message(settings, store, message)

    return dp


async def save_incoming_message(settings: Settings, store: MessageStore, message: Message) -> None:
    if not is_allowed(settings, message.chat.id):
        return

    text = message_text(message)
    if not text:
        return
    text = text[: settings.max_message_chars]
    sender_id, name = sender_name(message)
    await store.save_message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        chat_type=str(message.chat.type),
        sender_id=sender_id,
        sender_name=name,
        text=text,
        created_at=message.date,
        reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
    )


async def main() -> None:
    settings = load_settings()
    setup_logging(settings)
    store = MessageStore(settings.database_path)
    await store.init()
    llm = build_llm_client(settings)
    summarizer = Summarizer(llm, settings.chunk_chars)

    bot = Bot(token=settings.telegram_bot_token)
    dp = await create_dispatcher(settings, store, summarizer)

    logging.info("Bot started with LLM provider: %s", settings.resolved_llm_provider)
    await dp.start_polling(bot, allowed_updates=["message", "channel_post"])


if __name__ == "__main__":
    asyncio.run(main())
