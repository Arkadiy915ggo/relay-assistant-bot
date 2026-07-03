from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from tg_summary_bot.assistant import ChatAssistant
from tg_summary_bot.config import Settings, load_settings
from tg_summary_bot.llm import build_llm_client
from tg_summary_bot.periods import format_period, parse_period
from tg_summary_bot.storage import MessageStore
from tg_summary_bot.summarizer import Summarizer
from tg_summary_bot.transcriber import FasterWhisperTranscriber


RESPONSE_LOGGER_NAME = "tg_summary_bot.responses"


def telegram_html(text: str) -> str:
    rendered = html.escape(text, quote=False)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", rendered)
    return rendered


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


def audio_file_id(message: Message) -> str | None:
    if message.voice:
        return message.voice.file_id
    if message.audio:
        return message.audio.file_id
    return None


def audio_duration(message: Message) -> int | None:
    if message.voice:
        return message.voice.duration
    if message.audio:
        return message.audio.duration
    return None


def parse_question_command(text: str, default_period: str) -> tuple[str, str] | None:
    args = text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        return None

    raw = args[1].strip()
    parts = raw.split(maxsplit=1)
    if len(parts) == 2:
        try:
            parse_period(parts[0])
        except ValueError:
            pass
        else:
            return parts[0], parts[1].strip()

    return default_period, raw


def setup_logging(settings: Settings) -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    settings.response_log_file.parent.mkdir(parents=True, exist_ok=True)
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

    response_handler = RotatingFileHandler(
        settings.response_log_file,
        maxBytes=10_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    response_handler.setFormatter(logging.Formatter("%(message)s"))

    response_logger = logging.getLogger(RESPONSE_LOGGER_NAME)
    response_logger.handlers.clear()
    response_logger.setLevel(logging.INFO)
    response_logger.propagate = False
    response_logger.addHandler(response_handler)


def command_name(message: Message | None) -> str | None:
    if not message or not message.text or not message.text.startswith("/"):
        return None
    return message.text.split(maxsplit=1)[0]


def log_bot_response(
    *,
    action: str,
    text: str,
    response_message: Message,
    source_message: Message | None = None,
) -> None:
    event = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "chat_id": response_message.chat.id,
        "chat_type": str(response_message.chat.type),
        "command": command_name(source_message),
        "source_message_id": source_message.message_id if source_message else None,
        "response_message_id": response_message.message_id,
        "text": text,
    }
    logging.getLogger(RESPONSE_LOGGER_NAME).info(json.dumps(event, ensure_ascii=False))


async def answer_logged(message: Message, text: str) -> Message:
    response = await message.answer(telegram_html(text), parse_mode="HTML")
    log_bot_response(
        action="answer",
        text=text,
        response_message=response,
        source_message=message,
    )
    return response


async def reply_logged(message: Message, text: str) -> Message:
    response = await message.reply(telegram_html(text), parse_mode="HTML")
    log_bot_response(
        action="reply",
        text=text,
        response_message=response,
        source_message=message,
    )
    return response


async def edit_text_logged(
    message: Message,
    text: str,
    *,
    source_message: Message | None = None,
) -> None:
    await message.edit_text(telegram_html(text), parse_mode="HTML")
    log_bot_response(
        action="edit_text",
        text=text,
        response_message=message,
        source_message=source_message,
    )


async def create_dispatcher(
    settings: Settings,
    store: MessageStore,
    summarizer: Summarizer,
    chat_assistant: ChatAssistant,
    transcriber: FasterWhisperTranscriber | None,
    gpu_lock: asyncio.Lock,
) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(Command("start", "help"))
    async def help_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        await answer_logged(
            message,
            "I store new text messages from this chat and produce short summaries.\n\n"
            "Commands:\n"
            "`/summary` - summary for the default period\n"
            "`/summary 6h` - last 6 hours\n"
            "`/summary 7d` - last 7 days\n"
            "`/summary today` - today in UTC\n"
            "`/question 24h <text>` - chat with the assistant using recent context\n"
            "`/compare 10m` - compare summaries across Ollama models\n"
            "`/stats` - chat_id and stored message count\n\n"
            f"Current chat_id: `{message.chat.id}`"
        )

    @dp.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        count = await store.count_messages(message.chat.id)
        await answer_logged(
            message,
            f"chat_id: `{message.chat.id}`\n"
            f"chat_type: `{message.chat.type}`\n"
            f"saved_messages: `{count}`\n"
            f"llm_provider: `{settings.resolved_llm_provider}`\n"
            f"ollama_model: `{settings.ollama_model}`\n"
            f"ollama_timeout_seconds: `{settings.ollama_timeout_seconds}`\n"
            f"ollama_num_ctx: `{settings.ollama_num_ctx}`\n"
            f"ollama_num_predict: `{settings.ollama_num_predict}`\n"
            f"transcribe_voice: `{settings.transcribe_voice}`\n"
            f"whisper_model: `{settings.whisper_model}`\n"
            f"whisper_device: `{settings.whisper_device}`"
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
            await answer_logged(message, f"Could not parse period: {exc}")
            return

        wait_message = await answer_logged(message, "Collecting messages and building summary...")
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
            settings.ollama_model
            if settings.resolved_llm_provider == "ollama"
            else settings.openai_model,
            len(messages),
        )
        started = time.perf_counter()
        try:
            async with gpu_lock:
                try:
                    summary = await summarizer.summarize(messages, format_period(period_raw))
                finally:
                    await summarizer.unload()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Summary failed")
            await edit_text_logged(
                wait_message,
                f"Failed to build summary: `{type(exc).__name__}: {exc}`",
                source_message=message,
            )
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
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)

    @dp.message(Command("question"))
    async def question_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return

        parsed = parse_question_command(message.text or "", settings.default_summary_period)
        if not parsed:
            await answer_logged(
                message,
                "Usage: `/question [period] your question`\n"
                "Example: `/question 24h who promised to fix the issue?`",
            )
            return

        period_raw, question = parsed
        try:
            period = parse_period(period_raw)
        except ValueError as exc:
            await answer_logged(message, f"Could not parse period: {exc}")
            return

        wait_message = await answer_logged(message, "Thinking...")
        since = datetime.now(timezone.utc) - period
        messages = await store.get_messages_since(
            chat_id=message.chat.id,
            since=since,
            limit_chars=settings.max_summary_input_chars,
        )
        logging.info(
            "Question started chat_id=%s period=%s messages=%s",
            message.chat.id,
            period_raw,
            len(messages),
        )
        started = time.perf_counter()
        try:
            async with gpu_lock:
                try:
                    answer = await chat_assistant.ask(
                        messages,
                        format_period(period_raw),
                        question,
                    )
                finally:
                    await chat_assistant.unload()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Question failed")
            await edit_text_logged(
                wait_message,
                f"Failed to answer question: `{type(exc).__name__}: {exc}`",
                source_message=message,
            )
            return

        logging.info(
            "Question finished chat_id=%s period=%s elapsed_s=%.1f",
            message.chat.id,
            period_raw,
            time.perf_counter() - started,
        )
        text = f"**Answer for {format_period(period_raw)}**\n{answer}"
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)

    @dp.message(Command("compare"))
    async def compare_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if settings.resolved_llm_provider != "ollama":
            await answer_logged(message, "/compare currently requires LLM_PROVIDER=ollama.")
            return
        if not settings.compare_models:
            await answer_logged(
                message,
                "COMPARE_MODELS is empty. Add comma-separated models to .env.",
            )
            return

        args = (message.text or "").split(maxsplit=1)
        period_raw = args[1].strip() if len(args) > 1 else settings.default_summary_period
        try:
            period = parse_period(period_raw)
        except ValueError as exc:
            await answer_logged(message, f"Could not parse period: {exc}")
            return

        wait_message = await answer_logged(
            message,
            "Collecting messages for model comparison...\n"
            f"Models: {', '.join(settings.compare_models)}"
        )
        since = datetime.now(timezone.utc) - period
        messages = await store.get_messages_since(
            chat_id=message.chat.id,
            since=since,
            limit_chars=settings.max_summary_input_chars,
        )
        if not messages:
            await edit_text_logged(
                wait_message,
                f"No stored messages for period {format_period(period_raw)}.",
                source_message=message,
            )
            return

        for model in settings.compare_models:
            await edit_text_logged(
                wait_message,
                f"Model comparison: running {model}...",
                source_message=message,
            )
            started = time.perf_counter()
            logging.info(
                "Compare started chat_id=%s period=%s model=%s messages=%s",
                message.chat.id,
                period_raw,
                model,
                len(messages),
            )
            model_summarizer = Summarizer(
                build_llm_client(settings, model=model),
                settings.chunk_chars,
            )
            try:
                async with gpu_lock:
                    try:
                        summary = await model_summarizer.summarize(
                            messages,
                            format_period(period_raw),
                        )
                    finally:
                        await model_summarizer.unload()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Compare failed for model %s", model)
                await answer_logged(
                    message,
                    f"Model {model} failed to build summary: {type(exc).__name__}: {exc}",
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
            text = (
                f"Comparison: {model}\n"
                f"Period: {format_period(period_raw)}\n"
                f"Elapsed: {elapsed:.1f} sec\n\n"
                f"{summary}"
            )
            for part in split_telegram_text(text):
                await answer_logged(message, part)

        await edit_text_logged(
            wait_message,
            "Model comparison finished.",
            source_message=message,
        )

    @dp.message(F.voice | F.audio)
    async def transcribe_audio_message(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not settings.transcribe_voice:
            return
        if not transcriber:
            await answer_logged(message, "Voice transcription is not configured.")
            return

        duration = audio_duration(message)
        if duration and duration > settings.max_voice_seconds:
            await answer_logged(
                message,
                "Voice message is too long: "
                f"{duration} sec. Limit: {settings.max_voice_seconds} sec.",
            )
            return

        file_id = audio_file_id(message)
        if not file_id:
            return

        voice_sender_name = sender_name(message)[1].replace("*", "").strip() or "Unknown"
        status_message = await reply_logged(
            message,
            f"🎙 Transcribing voice message from **{voice_sender_name}**...",
        )
        audio_path: Path | None = None
        started = time.perf_counter()
        try:
            audio_path = await download_audio_message(settings, bot, message, file_id)
            async with gpu_lock:
                transcript = await transcriber.transcribe(audio_path)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Voice transcription failed")
            await edit_text_logged(
                status_message,
                f"Failed to transcribe voice message: `{type(exc).__name__}: {exc}`",
                source_message=message,
            )
            return
        finally:
            if audio_path:
                audio_path.unlink(missing_ok=True)

        if not transcript:
            await edit_text_logged(
                status_message,
                "No speech was recognized in the voice message.",
                source_message=message,
            )
            return

        saved_text = f"🎙 Voice message from {voice_sender_name}: {transcript}"
        await save_message_text(settings, store, message, saved_text)
        elapsed = time.perf_counter() - started
        preview = transcript[:900] + ("..." if len(transcript) > 900 else "")
        await edit_text_logged(
            status_message,
            f"**Voice transcription from {voice_sender_name}**\n"
            f"Saved for summaries in {elapsed:.1f} sec.\n\n"
            f"{preview}",
            source_message=message,
        )

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
    await save_message_text(settings, store, message, text)


async def save_message_text(
    settings: Settings,
    store: MessageStore,
    message: Message,
    text: str,
) -> None:
    text = " ".join(text.split())[: settings.max_message_chars]
    sender_id, name = sender_name(message)
    await store.save_message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        chat_type=str(message.chat.type),
        sender_id=sender_id,
        sender_name=name,
        text=text,
        created_at=message.date,
        reply_to_message_id=(
            message.reply_to_message.message_id
            if message.reply_to_message
            else None
        ),
    )


async def download_audio_message(
    settings: Settings,
    bot: Bot,
    message: Message,
    file_id: str,
) -> Path:
    settings.voice_download_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".ogg"
    if message.audio:
        suffix = Path(message.audio.file_name or "audio.ogg").suffix
    if not suffix:
        suffix = ".ogg"
    audio_path = settings.voice_download_dir / f"{message.chat.id}_{message.message_id}{suffix}"
    await bot.download(file_id, destination=audio_path)
    return audio_path


async def main() -> None:
    settings = load_settings()
    setup_logging(settings)
    store = MessageStore(settings.database_path)
    await store.init()
    llm = build_llm_client(settings)
    summarizer = Summarizer(llm, settings.chunk_chars)
    chat_assistant = ChatAssistant(llm, settings.chunk_chars)
    transcriber = FasterWhisperTranscriber(settings) if settings.transcribe_voice else None
    gpu_lock = asyncio.Lock()

    bot = Bot(token=settings.telegram_bot_token)
    dp = await create_dispatcher(
        settings,
        store,
        summarizer,
        chat_assistant,
        transcriber,
        gpu_lock,
    )

    logging.info("Bot started with LLM provider: %s", settings.resolved_llm_provider)
    await dp.start_polling(
        bot,
        allowed_updates=["message", "channel_post"],
        handle_as_tasks=False,
    )


if __name__ == "__main__":
    asyncio.run(main())
