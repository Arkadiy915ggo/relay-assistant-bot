from __future__ import annotations

import asyncio
import html
import json
import logging
import mimetypes
import re
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from tg_summary_bot.assistant import ChatAssistant
from tg_summary_bot.config import Settings, load_settings
from tg_summary_bot.image_recognizer import ImageRecognizer
from tg_summary_bot.llm import build_llm_client
from tg_summary_bot.memory import ChatMemory, MemoryCompressionError, participant_key, should_use_memory
from tg_summary_bot.meme_generator import MemeGenerator
from tg_summary_bot.observability import opik_track, update_opik_span_metadata
from tg_summary_bot.periods import format_period, parse_period
from tg_summary_bot.storage import MessageStore, StoredImage, StoredVideo
from tg_summary_bot.summarizer import Summarizer
from tg_summary_bot.transcriber import FasterWhisperTranscriber
from tg_summary_bot.transcript_formatter import TranscriptFormatter
from tg_summary_bot.video_recognizer import VideoRecognizer
from tg_summary_bot.web_search import WikipediaSearchClient, format_wiki_results


RESPONSE_LOGGER_NAME = "tg_summary_bot.responses"


class TelegramDownloadTooLargeError(RuntimeError):
    pass


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


def participant_ref(message: Message) -> tuple[str, str]:
    sender_id, name = sender_name(message)
    return participant_key(sender_id, name), name


def normalize_profile_target(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", value.strip().lstrip("@").lower())


def split_profile_correction_target(text: str) -> tuple[str | None, str]:
    text = text.strip()
    target, separator, fact = text.partition(" ")
    if target.startswith("@"):
        return target.strip("@,:; "), fact.strip() if separator else ""
    return None, text


def ranked_profile_target_matches(
    query: str,
    candidates: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    normalized_query = normalize_profile_target(query)
    if not normalized_query:
        return []

    seen: set[str] = set()
    scored: list[tuple[int, int, str, str]] = []
    for key, name in candidates:
        if key in seen:
            continue
        seen.add(key)
        normalized_name = normalize_profile_target(name)
        normalized_key = normalize_profile_target(key.removeprefix("name:"))
        score = 0
        if normalized_query in {normalized_name, normalized_key}:
            score = 4
        elif normalized_name.startswith(normalized_query) or normalized_key.startswith(
            normalized_query
        ):
            score = 3
        elif normalized_query in normalized_name or normalized_query in normalized_key:
            score = 2
        if score:
            scored.append((score, -len(name), key, name))

    scored.sort(reverse=True)
    return [(key, name) for _, _, key, name in scored]


async def resolve_profile_target(
    store: MessageStore,
    *,
    chat_id: int,
    query: str,
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for fact in await store.get_participant_facts(chat_id=chat_id, limit=200):
        candidates.append((fact.participant_key, fact.participant_name))
    for participant in await store.get_chat_participants(chat_id=chat_id, limit=200):
        candidates.append(
            (
                participant_key(participant.sender_id, participant.sender_name),
                participant.sender_name,
            )
        )
    return ranked_profile_target_matches(query, candidates)


def participant_refs_for_context(message: Message) -> tuple[list[str], list[str]]:
    refs = [participant_ref(message)]
    if message.reply_to_message:
        refs.append(participant_ref(message.reply_to_message))

    keys: list[str] = []
    names: list[str] = []
    for key, name in refs:
        if key not in keys:
            keys.append(key)
        if name and name not in names:
            names.append(name)
    return keys, names


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while line:
            remaining = limit - len(current)
            if remaining <= 0:
                chunks.append(current.rstrip())
                current = ""
                remaining = limit
            current += line[:remaining]
            line = line[remaining:]
        if len(current) >= limit:
            chunks.append(current.rstrip())
            current = ""
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


def image_payload(message: Message) -> tuple[str, str, int | None, str | None, str | None] | None:
    if message.photo:
        photo = max(message.photo, key=lambda item: item.width * item.height)
        return photo.file_id, "photo", photo.file_size, None, "image/jpeg"

    if message.document and message.document.mime_type:
        mime_type = message.document.mime_type
        if mime_type.startswith("image/"):
            return (
                message.document.file_id,
                "document",
                message.document.file_size,
                message.document.file_name,
                mime_type,
            )

    return None


def image_from_message(message: Message) -> StoredImage | None:
    payload = image_payload(message)
    if not payload:
        return None
    file_id, media_type, file_size, file_name, mime_type = payload
    return StoredImage(
        message_id=message.message_id,
        chat_id=message.chat.id,
        chat_type=str(message.chat.type),
        file_id=file_id,
        media_type=media_type,
        sender_name=sender_name(message)[1],
        created_at=message.date.astimezone(timezone.utc).isoformat(),
        file_size=file_size,
        file_name=file_name,
        mime_type=mime_type,
    )


def image_too_large(settings: Settings, image: StoredImage) -> bool:
    if not image.file_size:
        return False
    return image.file_size > settings.max_image_size_mb * 1024 * 1024


def meme_image_too_large(settings: Settings, image: StoredImage) -> bool:
    if not image.file_size:
        return False
    return image.file_size > settings.meme_max_image_size_mb * 1024 * 1024


def video_payload(
    message: Message,
) -> tuple[str, str, int | None, int | None, str | None, str | None] | None:
    if message.video_note:
        return (
            message.video_note.file_id,
            "video_note",
            message.video_note.duration,
            message.video_note.file_size,
            None,
            "video/mp4",
        )

    if message.video:
        return (
            message.video.file_id,
            "video",
            message.video.duration,
            message.video.file_size,
            message.video.file_name,
            message.video.mime_type,
        )

    if message.document and message.document.mime_type:
        mime_type = message.document.mime_type
        if mime_type.startswith("video/"):
            return (
                message.document.file_id,
                "document",
                None,
                message.document.file_size,
                message.document.file_name,
                mime_type,
            )

    return None


def video_from_message(message: Message) -> StoredVideo | None:
    payload = video_payload(message)
    if not payload:
        return None
    file_id, media_type, duration, file_size, file_name, mime_type = payload
    return StoredVideo(
        message_id=message.message_id,
        chat_id=message.chat.id,
        chat_type=str(message.chat.type),
        file_id=file_id,
        media_type=media_type,
        sender_name=sender_name(message)[1],
        created_at=message.date.astimezone(timezone.utc).isoformat(),
        duration=duration,
        file_size=file_size,
        file_name=file_name,
        mime_type=mime_type,
    )


def video_too_large(settings: Settings, video: StoredVideo) -> bool:
    if not video.file_size:
        return False
    return video.file_size > effective_video_size_limit_mb(settings) * 1024 * 1024


def effective_video_size_limit_mb(settings: Settings) -> int:
    if settings.telegram_download_limit_mb <= 0:
        return settings.max_video_size_mb
    return min(settings.max_video_size_mb, settings.telegram_download_limit_mb)


def telegram_download_limit_label(settings: Settings) -> str:
    if settings.telegram_download_limit_mb <= 0:
        return "не задан заранее; Telegram отклонил файл при скачивании"
    return f"{settings.telegram_download_limit_mb} MB"


def video_too_long(settings: Settings, video: StoredVideo) -> bool:
    if not video.duration:
        return False
    return video.duration > settings.max_video_seconds


def video_recognition_cache_key(
    settings: Settings,
    video_recognizer: VideoRecognizer,
    transcriber: FasterWhisperTranscriber | None,
) -> str:
    if settings.video_transcribe_audio and transcriber:
        audio = (
            f"audio=whisper:{transcriber.model_name}:"
            f"{transcriber.device}:{transcriber.compute_type}:{transcriber.language or 'auto'}"
        )
    elif settings.video_transcribe_audio:
        audio = "audio=missing-transcriber"
    else:
        audio = "audio=off"
    return f"{video_recognizer.cache_key}|{audio}"


def combine_video_result(
    visual_result: str,
    visual_note: str,
    audio_transcript: str,
    audio_note: str,
) -> str:
    visual = visual_result.strip() or visual_note
    transcript = audio_transcript.strip() or audio_note
    return (
        f"{visual}\n\n"
        "**Аудио / речь**\n"
        f"{transcript}"
    ).strip()


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


def entity_type_name(entity: object) -> str:
    return str(getattr(entity, "type", "")).lower().split(".")[-1]


def has_mention_entity(message: Message) -> bool:
    entities = message.entities if message.text else message.caption_entities
    return any(
        entity_type_name(entity) in {"mention", "text_mention"}
        for entity in entities or []
    )


def bot_mention_question(
    message: Message,
    *,
    bot_id: int | None,
    bot_username: str | None,
) -> str | None:
    text = message.text or message.caption or ""
    if not text:
        return None

    entities = message.entities if message.text else message.caption_entities
    mentioned = False
    for entity in entities or []:
        entity_type = entity_type_name(entity)
        if entity_type == "mention" and bot_username:
            mention = entity.extract_from(text).lstrip("@").lower()
            if mention == bot_username.lower():
                mentioned = True
        elif entity_type == "text_mention" and bot_id:
            user = getattr(entity, "user", None)
            if user and user.id == bot_id:
                mentioned = True

    if not mentioned:
        return None

    question = text
    if bot_username:
        question = re.sub(
            rf"(?i)(?<!\w)@{re.escape(bot_username)}\b",
            "",
            question,
        )
    return " ".join(question.split())


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
    chat_memory: ChatMemory | None,
    image_recognizer: ImageRecognizer,
    meme_generator: MemeGenerator,
    video_recognizer: VideoRecognizer,
    wiki_search: WikipediaSearchClient,
    transcriber: FasterWhisperTranscriber | None,
    transcript_formatter: TranscriptFormatter | None,
    gpu_lock: asyncio.Lock,
) -> Dispatcher:
    dp = Dispatcher()
    bot_identity: tuple[int | None, str | None] | None = None
    profile_refresh_tasks: set[asyncio.Task[None]] = set()

    async def refresh_recent_profile_facts(chat_id: int, *, now: datetime) -> None:
        if not chat_memory:
            return
        try:
            async with gpu_lock:
                try:
                    saved = await chat_memory.ensure_recent_profiles_current(chat_id, now=now)
                finally:
                    await chat_memory.unload()
            if saved:
                logging.info("Recent profile facts refreshed chat_id=%s saved=%s", chat_id, saved)
        except Exception:  # noqa: BLE001
            logging.exception("Recent profile fact refresh failed chat_id=%s", chat_id)

    def schedule_profile_refresh(chat_id: int, *, now: datetime) -> None:
        if chat_memory:
            task = asyncio.create_task(refresh_recent_profile_facts(chat_id, now=now))
            profile_refresh_tasks.add(task)
            task.add_done_callback(profile_refresh_tasks.discard)

    async def get_bot_identity(bot: Bot) -> tuple[int | None, str | None]:
        nonlocal bot_identity
        if bot_identity is None:
            me = await bot.get_me()
            bot_identity = (me.id, me.username)
        return bot_identity

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
            "`/wiki <text>` - search Wikipedia and save the result for chat context\n"
            "`/memory` - compressed chat memory status; `/memory rebuild` resets blocks\n"
            "`/profile [name]` - show your, replied, or named participant profile\n"
            "`/profile correct [@name] <fact>` - save a profile fact for yourself, "
            "named, or replied participant\n"
            "`/transcribe` - transcribe replied voice/audio\n"
            "`/image` - recognize the latest image or replied image\n"
            "`/meme` - make a meme from replied/latest image\n"
            "`/video` - recognize the latest video/video note or replied video\n"
            "`/compare 10m` - compare summaries across Ollama models\n"
            "`/stats` - chat_id and stored message count\n\n"
            "Voice messages are transcribed automatically when enabled. "
            "Video notes are recognized automatically. "
            "Mention the bot in a message to ask a contextual question.\n\n"
            f"Current chat_id: `{message.chat.id}`"
        )

    @dp.message(Command("stats"))
    async def stats_command(message: Message) -> None:
        count = await store.count_messages(message.chat.id)
        image_count = await store.count_images(message.chat.id)
        video_count = await store.count_videos(message.chat.id)
        await answer_logged(
            message,
            f"chat_id: `{message.chat.id}`\n"
            f"chat_type: `{message.chat.type}`\n"
            f"saved_messages: `{count}`\n"
            f"saved_images: `{image_count}`\n"
            f"saved_videos: `{video_count}`\n"
            f"llm_provider: `{settings.resolved_llm_provider}`\n"
            f"ollama_model: `{settings.ollama_model}`\n"
            f"question_model: `{settings.question_model or settings.ollama_model}`\n"
            f"image_recognition_model: `{settings.image_recognition_model}`\n"
            f"image_recognition_num_ctx: `{settings.image_recognition_num_ctx}`\n"
            f"meme_enabled: `{settings.meme_enabled}`\n"
            f"meme_model: `{settings.meme_model or settings.image_recognition_model}`\n"
            f"meme_output_dir: `{settings.meme_output_dir}`\n"
            f"video_recognition_model: `{settings.video_recognition_model}`\n"
            f"video_recognition_num_ctx: `{settings.video_recognition_num_ctx}`\n"
            f"video_frame_count: `{settings.video_frame_count}`\n"
            f"video_frame_max_width: `{settings.video_frame_max_width}`\n"
            f"video_transcribe_audio: `{settings.video_transcribe_audio}`\n"
            f"max_video_size_mb: `{settings.max_video_size_mb}`\n"
            f"telegram_download_limit_mb: `{settings.telegram_download_limit_mb}`\n"
            f"ollama_timeout_seconds: `{settings.ollama_timeout_seconds}`\n"
            f"ollama_num_ctx: `{settings.ollama_num_ctx}`\n"
            f"ollama_num_predict: `{settings.ollama_num_predict}`\n"
            f"opik_enabled: `{settings.opik_enabled}`\n"
            f"opik_project_name: `{settings.opik_project_name}`\n"
            f"opik_capture_content: `{settings.opik_capture_content}`\n"
            f"memory_enabled: `{settings.memory_enabled}`\n"
            f"wiki_search_enabled: `{settings.wiki_search_enabled}`\n"
            f"wiki_language: `{settings.wiki_language}`\n"
            f"wiki_max_results: `{settings.wiki_max_results}`\n"
            f"transcribe_voice: `{settings.transcribe_voice}`\n"
            f"whisper_model: `{settings.whisper_model}`\n"
            f"whisper_device: `{settings.whisper_device}`\n"
            f"transcription_format_enabled: `{settings.transcription_format_enabled}`\n"
            f"transcription_format_provider: `{settings.transcription_format_provider}`\n"
            f"transcription_format_model: `{settings.transcription_format_model}`\n"
            f"transcription_format_num_ctx: `{settings.transcription_format_num_ctx}`\n"
            f"transcription_format_num_predict: `{settings.transcription_format_num_predict}`\n"
            f"max_transcription_format_chars: `{settings.max_transcription_format_chars}`\n"
            f"max_transcription_chars: `{settings.max_transcription_chars}`\n"
            f"access_allowed: `{is_allowed(settings, message.chat.id)}`"
        )

    @dp.message(Command("memory"))
    async def memory_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not chat_memory:
            await answer_logged(message, "Chat memory is disabled: `MEMORY_ENABLED=false`.")
            return

        args = (message.text or "").split(maxsplit=1)
        if len(args) > 1:
            action = args[1].strip().lower()
            if action == "rebuild":
                await chat_memory.reset_blocks(message.chat.id)
                await answer_logged(
                    message,
                    "Memory blocks were reset. Participant profile facts were kept. "
                    "The next long `/summary` or `/question` will rebuild structured memory.",
                )
                return
            await answer_logged(message, "Usage: `/memory` or `/memory rebuild`")
            return

        status = await chat_memory.status(message.chat.id)
        await answer_logged(
            message,
            f"memory_blocks: `{status['memory_blocks']}`\n"
            f"chunk_blocks: `{status['chunk_blocks']}`\n"
            f"rollup_blocks: `{status['rollup_blocks']}`\n"
            f"archive_blocks: `{status['archive_blocks']}`\n"
            f"participant_facts: `{status['participant_facts']}`\n"
            f"processed_until: `{status['processed_until']}`\n"
            f"profile_processed_until: `{status['profile_processed_until']}`\n"
            f"latest_raw_messages: `{status['latest_raw_messages']}`\n"
            f"pending_old_messages: `{status['pending_old_messages']}`\n"
            f"recent_period: `{status['recent_period']}`\n"
            f"chunk_chars: `{status['chunk_chars']}`\n"
            f"profile_chunk_chars: `{status['profile_chunk_chars']}`\n"
            f"max_blocks_per_level: `{status['max_blocks_per_level']}`\n"
            f"search_limit: `{status['search_limit']}`",
        )

    @dp.message(Command("profile"))
    async def profile_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not chat_memory:
            await answer_logged(message, "Chat memory is disabled: `MEMORY_ENABLED=false`.")
            return

        parts = (message.text or "").split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "show"

        if action == "forget":
            if message.reply_to_message:
                keys = [participant_ref(message.reply_to_message)[0]]
            elif len(parts) > 2:
                facts = await store.get_participant_facts(
                    chat_id=message.chat.id,
                    participant_name=parts[2],
                    limit=100,
                )
                keys = list(dict.fromkeys(fact.participant_key for fact in facts))
            else:
                await answer_logged(
                    message,
                    "Usage: reply with `/profile forget`, or `/profile forget <name>`.",
                )
                return
            count = await chat_memory.forget_profile(
                chat_id=message.chat.id,
                participant_keys=keys,
            )
            await answer_logged(message, f"Forgot active profile facts: `{count}`.")
            return

        if action == "correct":
            if len(parts) < 3 or not parts[2].strip():
                await answer_logged(
                    message,
                    "Usage: `/profile correct <true fact>` for yourself, "
                    "`/profile correct @name <true fact>` for a named participant, "
                    "or reply to a participant with `/profile correct <true fact>`.",
                )
                return
            fact_text = parts[2].strip()
            if message.reply_to_message:
                key, name = participant_ref(message.reply_to_message)
            else:
                target_query, parsed_fact = split_profile_correction_target(fact_text)
                if target_query is None:
                    key, name = participant_ref(message)
                else:
                    if not target_query or not parsed_fact:
                        await answer_logged(
                            message,
                            "Usage: `/profile correct @name <true fact>`.",
                        )
                        return
                    matches = await resolve_profile_target(
                        store,
                        chat_id=message.chat.id,
                        query=target_query,
                    )
                    if not matches:
                        await answer_logged(
                            message,
                            f"Не нашёл участника `{target_query}`. "
                            "Ответьте на его сообщение `/profile correct <true fact>` "
                            "или используйте часть отображаемого имени.",
                        )
                        return
                    normalized_target = normalize_profile_target(target_query)
                    exact_matches = [
                        match
                        for match in matches
                        if normalized_target
                        in {
                            normalize_profile_target(match[0].removeprefix("name:")),
                            normalize_profile_target(match[1]),
                        }
                    ]
                    if len(matches) > 1 and len(exact_matches) != 1:
                        candidates = "\n".join(
                            f"- {candidate_name}" for _, candidate_name in matches[:5]
                        )
                        await answer_logged(
                            message,
                            f"Нашёл несколько участников для `{target_query}`. "
                            "Уточните имя или ответьте на сообщение участника.\n"
                            f"{candidates}",
                        )
                        return
                    key, name = (exact_matches or matches)[0]
                    fact_text = parsed_fact
            await chat_memory.add_profile_correction(
                chat_id=message.chat.id,
                participant_key=key,
                participant_name=name,
                fact_text=fact_text,
                source_message_id=message.message_id,
                created_at=message.date,
            )
            text = await chat_memory.profile_text(
                chat_id=message.chat.id,
                participant_keys=[key],
            )
            await answer_logged(
                message,
                f"Saved profile correction for `{name}`.\n\n{text}",
            )
            return

        if action == "show" and len(parts) > 2 and parts[2].strip():
            text = await chat_memory.profile_text(
                chat_id=message.chat.id,
                participant_name=parts[2].strip(),
            )
            await answer_logged(message, text)
            return

        if action not in {"show", "forget", "correct"}:
            participant_name = " ".join(parts[1:]).strip()
            text = await chat_memory.profile_text(
                chat_id=message.chat.id,
                participant_name=participant_name,
            )
            await answer_logged(message, text)
            return

        if message.reply_to_message:
            key, _ = participant_ref(message.reply_to_message)
            text = await chat_memory.profile_text(
                chat_id=message.chat.id,
                participant_keys=[key],
            )
        else:
            key, _ = participant_ref(message)
            text = await chat_memory.profile_text(
                chat_id=message.chat.id,
                participant_keys=[key],
            )
            if text == "Паспорт участника пока пуст.":
                text += (
                    "\n\n`/profile` без reply показывает ваш профиль. "
                    "Чтобы посмотреть другого участника, ответьте на его сообщение `/profile` "
                    "или используйте `/profile <name>`."
                )
        await answer_logged(message, text)

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
        now = datetime.now(timezone.utc)
        since = now - period
        use_memory = should_use_memory(period, chat_memory)
        raw_since = chat_memory.recent_since(now) if use_memory and chat_memory else since
        if raw_since < since:
            raw_since = since
        messages = await store.get_messages_since(
            chat_id=message.chat.id,
            since=raw_since,
            limit_chars=settings.max_summary_input_chars,
        )
        logging.info(
            "Summary started chat_id=%s period=%s model=%s raw_messages=%s memory=%s",
            message.chat.id,
            period_raw,
            settings.ollama_model
            if settings.resolved_llm_provider == "ollama"
            else settings.openai_model,
            len(messages),
            use_memory,
        )
        started = time.perf_counter()
        try:
            async with gpu_lock:
                try:
                    context_messages = messages
                    if use_memory and chat_memory:
                        try:
                            created_blocks = await chat_memory.ensure_current(message.chat.id, now=now)
                            blocks = await chat_memory.blocks_for_summary(
                                chat_id=message.chat.id,
                                since=since,
                                until=raw_since,
                            )
                            context_messages = chat_memory.blocks_as_messages(blocks) + messages
                            logging.info(
                                "Summary memory context chat_id=%s blocks=%s created_blocks=%s",
                                message.chat.id,
                                len(blocks),
                                created_blocks,
                            )
                        except MemoryCompressionError as exc:
                            logging.warning(
                                "Summary memory rebuild failed; falling back to raw messages chat_id=%s period=%s: %s",
                                message.chat.id,
                                period_raw,
                                exc,
                            )
                            if raw_since != since:
                                messages = await store.get_messages_since(
                                    chat_id=message.chat.id,
                                    since=since,
                                    limit_chars=settings.max_summary_input_chars,
                                )
                            context_messages = messages
                    summary = await summarizer.summarize(context_messages, format_period(period_raw))
                finally:
                    if use_memory and chat_memory:
                        await chat_memory.unload()
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
        schedule_profile_refresh(message.chat.id, now=now)

    async def answer_chat_question(
        message: Message,
        *,
        period_raw: str,
        question: str,
        reply_to_source: bool = False,
    ) -> None:
        if not is_allowed(settings, message.chat.id):
            return

        try:
            period = parse_period(period_raw)
        except ValueError as exc:
            await answer_logged(message, f"Could not parse period: {exc}")
            return

        if reply_to_source:
            wait_message = await reply_logged(message, "Thinking...")
        else:
            wait_message = await answer_logged(message, "Thinking...")
        now = datetime.now(timezone.utc)
        since = now - period
        use_memory = should_use_memory(period, chat_memory)
        raw_since = chat_memory.recent_since(now) if use_memory and chat_memory else since
        if raw_since < since:
            raw_since = since
        messages = await store.get_messages_since(
            chat_id=message.chat.id,
            since=raw_since,
            limit_chars=settings.max_summary_input_chars,
        )
        logging.info(
            "Question started chat_id=%s period=%s raw_messages=%s memory=%s",
            message.chat.id,
            period_raw,
            len(messages),
            use_memory,
        )
        started = time.perf_counter()
        try:
            async with gpu_lock:
                try:
                    profile_context = ""
                    context_messages = messages
                    if use_memory and chat_memory:
                        try:
                            created_blocks = await chat_memory.ensure_current(message.chat.id, now=now)
                            blocks = await chat_memory.search(
                                chat_id=message.chat.id,
                                since=since,
                                until=raw_since,
                                query=question,
                            )
                            await chat_memory.unload()
                            context_messages = chat_memory.blocks_as_messages(blocks) + messages
                            logging.info(
                                "Question memory context chat_id=%s blocks=%s created_blocks=%s",
                                message.chat.id,
                                len(blocks),
                                created_blocks,
                            )
                        except MemoryCompressionError as exc:
                            logging.warning(
                                "Question memory rebuild failed; falling back to raw messages chat_id=%s period=%s: %s",
                                message.chat.id,
                                period_raw,
                                exc,
                            )
                            if raw_since != since:
                                messages = await store.get_messages_since(
                                    chat_id=message.chat.id,
                                    since=since,
                                    limit_chars=settings.max_summary_input_chars,
                                )
                            context_messages = messages
                    if chat_memory:
                        participant_keys, participant_names = participant_refs_for_context(message)
                        profile_context = await chat_memory.participant_context(
                            chat_id=message.chat.id,
                            query=question,
                            participant_keys=participant_keys,
                            participant_names=participant_names,
                        )
                    if profile_context and chat_memory:
                        context_messages = [
                            chat_memory.participant_context_as_message(
                                message.chat.id,
                                profile_context,
                            )
                        ] + context_messages
                    answer = await chat_assistant.ask(
                        context_messages,
                        format_period(period_raw),
                        question,
                    )
                finally:
                    if use_memory and chat_memory:
                        await chat_memory.unload()
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
        schedule_profile_refresh(message.chat.id, now=now)

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
        await answer_chat_question(message, period_raw=period_raw, question=question)

    @dp.message(Command("wiki"))
    async def wiki_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not settings.wiki_search_enabled:
            await answer_logged(message, "Wikipedia search is disabled: `WIKI_SEARCH_ENABLED=false`.")
            return

        query = (message.text or "").split(maxsplit=1)
        if len(query) < 2 or not query[1].strip():
            await answer_logged(message, "Usage: `/wiki what to search`")
            return

        search_query = query[1].strip()
        wait_message = await answer_logged(message, f"Searching Wikipedia for `{search_query}`...")
        try:
            results = await wiki_search.search(search_query)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Wikipedia search failed")
            await edit_text_logged(
                wait_message,
                f"Failed to search Wikipedia: `{type(exc).__name__}: {exc}`",
                source_message=message,
            )
            return

        text = format_wiki_results(search_query, results)
        if results:
            await save_message_text(
                settings,
                store,
                message,
                f"Wikipedia search for {search_query}: {text}",
                limit_chars=settings.max_transcription_chars,
            )
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)

    @dp.message(Command("image", "ocr"))
    async def image_command(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not settings.image_recognition_model:
            await answer_logged(
                message,
                "Image recognition is disabled: IMAGE_RECOGNITION_MODEL is empty.",
            )
            return

        image = await resolve_image_for_command(store, message)
        if not image:
            await answer_logged(
                message,
                "No image found. Reply to an image with `/image`, or send `/image` after an image.",
            )
            return
        if image_too_large(settings, image):
            await answer_logged(
                message,
                "Image is too large: "
                f"{image.file_size} bytes. Limit: {settings.max_image_size_mb} MB.",
            )
            return

        wait_message = await answer_logged(
            message,
            f"Recognizing image #{image.message_id} with `{settings.image_recognition_model}`...",
        )
        image_path: Path | None = None
        started = time.perf_counter()
        try:
            image_path = await download_image(settings, bot, image)
            async with gpu_lock:
                try:
                    result = await image_recognizer.recognize(image_path)
                finally:
                    await image_recognizer.unload()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Image recognition failed")
            await edit_text_logged(
                wait_message,
                f"Failed to recognize image: `{type(exc).__name__}: {exc}`",
                source_message=message,
            )
            return
        finally:
            if image_path:
                image_path.unlink(missing_ok=True)

        elapsed = time.perf_counter() - started
        saved_text = (
            f"🖼 Image recognition for message #{image.message_id} "
            f"from {image.sender_name}: {result}"
        )
        await save_message_text(settings, store, message, saved_text)
        text = (
            f"**Image recognition for message #{image.message_id}**\n"
            f"Source: {image.sender_name}\n"
            f"Model: `{settings.image_recognition_model}`\n"
            f"Elapsed: {elapsed:.1f} sec\n"
            "Saved for summaries.\n\n"
            f"{result}"
        )
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)

    @dp.message(Command("meme"))
    async def meme_command(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not settings.meme_enabled:
            await answer_logged(message, "Meme generation is disabled: `MEME_ENABLED=false`.")
            return
        meme_model = settings.meme_model or settings.image_recognition_model
        if not meme_model:
            await answer_logged(
                message,
                "Meme generation is disabled: MEME_MODEL and IMAGE_RECOGNITION_MODEL are empty.",
            )
            return

        image = await resolve_image_for_command(store, message)
        if not image:
            await answer_logged(
                message,
                "No image found. Reply to an image with `/meme`, or send `/meme` after an image.",
            )
            return
        if meme_image_too_large(settings, image):
            await answer_logged(
                message,
                "Image is too large: "
                f"{image.file_size} bytes. Limit: {settings.meme_max_image_size_mb} MB.",
            )
            return

        wait_message = await answer_logged(
            message,
            f"Делаю мем из картинки #{image.message_id} через `{meme_model}`...",
        )
        image_path: Path | None = None
        output_path: Path | None = None
        started = time.perf_counter()
        try:
            image_path = await download_image(settings, bot, image)
            try:
                async with gpu_lock:
                    try:
                        caption = await meme_generator.generate_caption(image_path)
                    finally:
                        await meme_generator.unload()
            except Exception as exc:  # noqa: BLE001
                logging.exception("Meme caption generation failed")
                await edit_text_logged(
                    wait_message,
                    f"Failed to generate meme text: `{type(exc).__name__}: {exc}`",
                    source_message=message,
                )
                return

            output_path = settings.meme_output_dir / f"{image.chat_id}_{image.message_id}_meme.jpg"
            try:
                meme_generator.render_meme(image_path, caption, output_path)
            except Exception as exc:  # noqa: BLE001
                logging.exception("Meme rendering failed")
                await edit_text_logged(
                    wait_message,
                    f"Failed to render meme: `{type(exc).__name__}: {exc}`",
                    source_message=message,
                )
                return

            elapsed = time.perf_counter() - started
            response = await message.reply_photo(
                photo=FSInputFile(output_path),
                caption=f"Мем готов за {elapsed:.1f} сек.",
            )
            log_bot_response(
                action="reply_photo",
                text=f"Meme for image #{image.message_id}: {caption.alt_text}",
                response_message=response,
                source_message=message,
            )
            await wait_message.delete()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Meme command failed")
            await edit_text_logged(
                wait_message,
                f"Failed to create meme: `{type(exc).__name__}: {exc}`",
                source_message=message,
            )
        finally:
            if image_path:
                image_path.unlink(missing_ok=True)
            if output_path:
                output_path.unlink(missing_ok=True)

    @opik_track(name="video.process")
    async def recognize_video_source(
        *,
        video: StoredVideo,
        request_message: Message,
        save_target_message: Message,
        bot: Bot,
        notify_disabled: bool = False,
        status_as_reply: bool = False,
    ) -> None:
        if not is_allowed(settings, request_message.chat.id):
            return
        update_opik_span_metadata(
            {
                "chat_id": request_message.chat.id,
                "message_id": video.message_id,
                "media_type": video.media_type,
                "duration_s": video.duration,
                "file_size": video.file_size,
                "model": settings.video_recognition_model,
                "video_transcribe_audio": settings.video_transcribe_audio,
            }
        )
        if not settings.video_recognition_model:
            if notify_disabled:
                await answer_logged(
                    request_message,
                    "Video recognition is disabled: VIDEO_RECOGNITION_MODEL is empty.",
                )
            return

        if video_too_large(settings, video):
            await answer_logged(
                request_message,
                "Видео слишком большое для распознавания: "
                f"{video.file_size} bytes. Лимит: {effective_video_size_limit_mb(settings)} MB.",
            )
            return
        if video_too_long(settings, video):
            await answer_logged(
                request_message,
                "Video is too long: "
                f"{video.duration} sec. Limit: {settings.max_video_seconds} sec.",
            )
            return

        cache_key = video_recognition_cache_key(settings, video_recognizer, transcriber)
        cached = await store.get_video_recognition(
            chat_id=video.chat_id,
            message_id=video.message_id,
            cache_key=cache_key,
        )
        if cached and cached.result.strip():
            update_opik_span_metadata({"cache_hit": True})
            saved_text = (
                f"🎞 Video recognition for message #{video.message_id} "
                f"from {video.sender_name}: {cached.result}"
            )
            await save_message_text(
                settings,
                store,
                save_target_message,
                saved_text,
                limit_chars=settings.max_transcription_chars,
            )
            text = (
                f"**Video recognition for message #{video.message_id}**\n"
                f"Source: {video.sender_name}\n"
                f"Type: `{video.media_type}`\n"
                f"Model: `{settings.video_recognition_model}`\n"
                "Cache: `hit`\n"
                "Saved for summaries.\n\n"
                f"{cached.result}"
            )
            for index, part in enumerate(split_telegram_text(text)):
                if index == 0 and status_as_reply:
                    await reply_logged(request_message, part)
                else:
                    await answer_logged(request_message, part)
            return

        update_opik_span_metadata({"cache_hit": False})

        status_text = (
            f"Recognizing video #{video.message_id} "
            f"with `{settings.video_recognition_model}`..."
        )
        if status_as_reply:
            wait_message = await reply_logged(request_message, status_text)
        else:
            wait_message = await answer_logged(request_message, status_text)
        video_path: Path | None = None
        audio_path: Path | None = None
        started = time.perf_counter()
        try:
            video_path = await download_video(settings, bot, video)
            audio_path = await extract_video_audio(settings, video_path, video)
            visual_result = ""
            visual_note = "Визуальный анализ кадров не дал результата."
            audio_transcript = ""
            audio_note = "Аудиодорожка не найдена или речь не распознана."
            async with gpu_lock:
                try:
                    visual_result = await video_recognizer.recognize(
                        video_path,
                        message_id=video.message_id,
                        duration=video.duration,
                    )
                except Exception as exc:  # noqa: BLE001
                    logging.exception("Video visual recognition failed")
                    visual_note = f"Визуальный анализ кадров не удался: {type(exc).__name__}: {exc}"
                finally:
                    await video_recognizer.unload()

                if settings.video_transcribe_audio:
                    if audio_path and transcriber:
                        audio_transcript = await transcriber.transcribe(audio_path)
                    elif audio_path:
                        audio_note = "Аудио найдено, но Whisper transcription is not configured."
                else:
                    audio_note = "Расшифровка аудио для видео отключена."
            if not visual_result.strip() and not audio_transcript.strip():
                raise RuntimeError(f"{visual_note}; {audio_note}")
            result = combine_video_result(visual_result, visual_note, audio_transcript, audio_note)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Video recognition failed")
            if isinstance(exc, TelegramDownloadTooLargeError):
                text = str(exc)
            else:
                text = f"Failed to recognize video: `{type(exc).__name__}: {exc}`"
            await edit_text_logged(
                wait_message,
                text,
                source_message=request_message,
            )
            return
        finally:
            if audio_path:
                audio_path.unlink(missing_ok=True)
            if video_path:
                video_path.unlink(missing_ok=True)

        elapsed = time.perf_counter() - started
        saved_text = (
            f"🎞 Video recognition for message #{video.message_id} "
            f"from {video.sender_name}: {result}"
        )
        await store.save_video_recognition(
            chat_id=video.chat_id,
            message_id=video.message_id,
            cache_key=cache_key,
            result=result,
        )
        await save_message_text(
            settings,
            store,
            save_target_message,
            saved_text,
            limit_chars=settings.max_transcription_chars,
        )
        text = (
            f"**Video recognition for message #{video.message_id}**\n"
            f"Source: {video.sender_name}\n"
            f"Type: `{video.media_type}`\n"
            f"Model: `{settings.video_recognition_model}`\n"
            f"Elapsed: {elapsed:.1f} sec\n"
            "Saved for summaries.\n\n"
            f"{result}"
        )
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=request_message)
        for part in parts[1:]:
            await answer_logged(request_message, part)

    @dp.message(Command("video", "vocr"))
    async def video_command(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return

        video = await resolve_video_for_command(store, message)
        if not video:
            await answer_logged(
                message,
                "No video found. Reply to a video with `/video`, or send `/video` after a video.",
            )
            return

        await recognize_video_source(
            video=video,
            request_message=message,
            save_target_message=message,
            bot=bot,
            notify_disabled=True,
        )

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

    async def format_transcript_response(
        *,
        source_message: Message,
        request_message: Message,
        response_messages: list[Message],
        voice_sender_name: str,
        transcript: str,
        transcription_elapsed: float,
    ) -> None:
        if not transcript_formatter:
            return
        if len(transcript) > settings.max_transcription_format_chars:
            logging.info(
                "Transcript formatting skipped chat_id=%s message_id=%s chars=%s limit=%s",
                source_message.chat.id,
                source_message.message_id,
                len(transcript),
                settings.max_transcription_format_chars,
            )
            return

        started = time.perf_counter()
        try:
            async with gpu_lock:
                try:
                    formatted = await transcript_formatter.format(transcript)
                finally:
                    await transcript_formatter.unload()
        except Exception:  # noqa: BLE001
            logging.exception(
                "Transcript formatting failed chat_id=%s message_id=%s",
                source_message.chat.id,
                source_message.message_id,
            )
            return

        formatted = formatted.strip()
        if not formatted:
            return

        await save_message_text(
            settings,
            store,
            source_message,
            f"🎙 Voice message from {voice_sender_name}: {formatted}",
            limit_chars=settings.max_transcription_chars,
            replace_existing=True,
        )
        format_elapsed = time.perf_counter() - started
        text = (
            f"**Voice transcription from {voice_sender_name}**\n"
            f"Saved for summaries in {transcription_elapsed:.1f} sec.\n"
            f"Formatted by `{transcript_formatter.model_name}` in {format_elapsed:.1f} sec.\n\n"
            f"{formatted}"
        )
        parts = split_telegram_text(text)
        for index, part in enumerate(parts):
            if index < len(response_messages):
                await edit_text_logged(
                    response_messages[index],
                    part,
                    source_message=request_message,
                )
            else:
                response_messages.append(await answer_logged(request_message, part))

        for old_message in response_messages[len(parts) :]:
            await edit_text_logged(
                old_message,
                "Formatted transcript was merged into the previous message.",
                source_message=request_message,
            )

    @opik_track(name="audio.process")
    async def transcribe_audio_source(
        source_message: Message,
        bot: Bot,
        *,
        request_message: Message | None = None,
        replace_existing: bool = False,
        notify_disabled: bool = False,
    ) -> None:
        request_message = request_message or source_message
        if not is_allowed(settings, request_message.chat.id):
            return
        update_opik_span_metadata(
            {
                "chat_id": request_message.chat.id,
                "message_id": source_message.message_id,
                "duration_s": audio_duration(source_message),
                "model": settings.whisper_model,
                "device": settings.whisper_device,
                "compute_type": settings.whisper_compute_type,
                "language": settings.whisper_language or "auto",
            }
        )
        if not settings.transcribe_voice:
            if notify_disabled:
                await answer_logged(
                    request_message,
                    "Voice transcription is disabled: `TRANSCRIBE_VOICE=false`.",
                )
            return
        if not transcriber:
            await answer_logged(request_message, "Voice transcription is not configured.")
            return

        duration = audio_duration(source_message)
        if duration and duration > settings.max_voice_seconds:
            await answer_logged(
                request_message,
                "Voice message is too long: "
                f"{duration} sec. Limit: {settings.max_voice_seconds} sec.",
            )
            return

        file_id = audio_file_id(source_message)
        if not file_id:
            return

        voice_sender_name = sender_name(source_message)[1].replace("*", "").strip() or "Unknown"
        status_message = await reply_logged(
            request_message,
            f"🎙 Transcribing voice message from **{voice_sender_name}**...",
        )
        audio_path: Path | None = None
        started = time.perf_counter()
        try:
            audio_path = await download_audio_message(settings, bot, source_message, file_id)
            async with gpu_lock:
                transcript = await transcriber.transcribe(audio_path)
        except Exception as exc:  # noqa: BLE001
            logging.exception("Voice transcription failed")
            await edit_text_logged(
                status_message,
                f"Failed to transcribe voice message: `{type(exc).__name__}: {exc}`",
                source_message=request_message,
            )
            return
        finally:
            if audio_path:
                audio_path.unlink(missing_ok=True)

        if not transcript:
            await edit_text_logged(
                status_message,
                "No speech was recognized in the voice message.",
                source_message=request_message,
            )
            return

        saved_text = f"🎙 Voice message from {voice_sender_name}: {transcript}"
        await save_message_text(
            settings,
            store,
            source_message,
            saved_text,
            limit_chars=settings.max_transcription_chars,
            replace_existing=replace_existing,
        )
        elapsed = time.perf_counter() - started
        text = (
            f"**Voice transcription from {voice_sender_name}**\n"
            f"Saved for summaries in {elapsed:.1f} sec.\n\n"
            f"{transcript}"
        )
        parts = split_telegram_text(text)
        response_messages = [status_message]
        await edit_text_logged(status_message, parts[0], source_message=request_message)
        for part in parts[1:]:
            response_messages.append(await answer_logged(request_message, part))

        if transcript_formatter:
            asyncio.create_task(
                format_transcript_response(
                    source_message=source_message,
                    request_message=request_message,
                    response_messages=response_messages,
                    voice_sender_name=voice_sender_name,
                    transcript=transcript,
                    transcription_elapsed=elapsed,
                )
            )

    @dp.message(Command("transcribe"))
    async def transcribe_command(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not message.reply_to_message:
            await answer_logged(message, "Reply to a voice/audio message with `/transcribe`.")
            return
        if not audio_file_id(message.reply_to_message):
            await answer_logged(message, "The replied message is not voice/audio.")
            return

        await transcribe_audio_source(
            message.reply_to_message,
            bot,
            request_message=message,
            replace_existing=True,
            notify_disabled=True,
        )

    @dp.message(F.voice | F.audio)
    async def transcribe_audio_message(message: Message, bot: Bot) -> None:
        await transcribe_audio_source(message, bot)

    @dp.channel_post(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def save_channel_image(message: Message) -> None:
        await save_incoming_image(settings, store, message)

    @dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def save_regular_image(message: Message) -> None:
        await save_incoming_image(settings, store, message)

    @dp.channel_post(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
    async def save_channel_video(message: Message) -> None:
        await save_incoming_video(settings, store, message)

    @dp.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
    async def save_regular_video(message: Message, bot: Bot) -> None:
        await save_incoming_video(settings, store, message)
        if not message.video_note:
            return

        video = video_from_message(message)
        if not video:
            return
        await recognize_video_source(
            video=video,
            request_message=message,
            save_target_message=message,
            bot=bot,
            status_as_reply=True,
        )

    @dp.channel_post(F.text | F.caption)
    async def save_channel_post(message: Message) -> None:
        await save_incoming_message(settings, store, message)

    @dp.message(F.text | F.caption)
    async def save_regular_message(message: Message, bot: Bot) -> None:
        if message.text and message.text.startswith("/"):
            return
        await save_incoming_message(settings, store, message)
        if not has_mention_entity(message):
            return

        bot_id, bot_username = await get_bot_identity(bot)
        question = bot_mention_question(
            message,
            bot_id=bot_id,
            bot_username=bot_username,
        )
        if question is None:
            return
        if not question:
            await reply_logged(message, "Напишите вопрос рядом с упоминанием бота.")
            return
        await answer_chat_question(
            message,
            period_raw=settings.default_summary_period,
            question=question,
            reply_to_source=True,
        )

    return dp


async def resolve_image_for_command(store: MessageStore, message: Message) -> StoredImage | None:
    if message.reply_to_message:
        replied_image = image_from_message(message.reply_to_message)
        if replied_image:
            return replied_image
        return await store.get_image_by_message_id(message.chat.id, message.reply_to_message.message_id)
    current_image = image_from_message(message)
    if current_image:
        return current_image
    return await store.get_latest_image(message.chat.id)


async def resolve_video_for_command(store: MessageStore, message: Message) -> StoredVideo | None:
    if message.reply_to_message:
        replied_video = video_from_message(message.reply_to_message)
        if replied_video:
            return replied_video
        return await store.get_video_by_message_id(message.chat.id, message.reply_to_message.message_id)
    return await store.get_latest_video(message.chat.id)


async def save_incoming_message(settings: Settings, store: MessageStore, message: Message) -> None:
    if not is_allowed(settings, message.chat.id):
        return

    text = message_text(message)
    if not text:
        return
    await save_message_text(settings, store, message, text)


async def save_incoming_image(settings: Settings, store: MessageStore, message: Message) -> None:
    if not is_allowed(settings, message.chat.id):
        return

    payload = image_payload(message)
    if not payload:
        return

    file_id, media_type, file_size, file_name, mime_type = payload
    sender_id, name = sender_name(message)
    await store.save_image(
        chat_id=message.chat.id,
        message_id=message.message_id,
        chat_type=str(message.chat.type),
        file_id=file_id,
        media_type=media_type,
        sender_id=sender_id,
        sender_name=name,
        created_at=message.date,
        file_size=file_size,
        file_name=file_name,
        mime_type=mime_type,
    )


async def save_incoming_video(settings: Settings, store: MessageStore, message: Message) -> None:
    if not is_allowed(settings, message.chat.id):
        return

    payload = video_payload(message)
    if not payload:
        return

    file_id, media_type, duration, file_size, file_name, mime_type = payload
    sender_id, name = sender_name(message)
    await store.save_video(
        chat_id=message.chat.id,
        message_id=message.message_id,
        chat_type=str(message.chat.type),
        file_id=file_id,
        media_type=media_type,
        sender_id=sender_id,
        sender_name=name,
        created_at=message.date,
        duration=duration,
        file_size=file_size,
        file_name=file_name,
        mime_type=mime_type,
    )


async def save_message_text(
    settings: Settings,
    store: MessageStore,
    message: Message,
    text: str,
    *,
    limit_chars: int | None = None,
    replace_existing: bool = False,
) -> None:
    limit = settings.max_message_chars if limit_chars is None else limit_chars
    text = " ".join(text.split())[:limit]
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
        replace=replace_existing,
    )


async def download_image(settings: Settings, bot: Bot, image: StoredImage) -> Path:
    settings.image_download_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(image.file_name).suffix if image.file_name else ""
    if not suffix and image.mime_type:
        suffix = mimetypes.guess_extension(image.mime_type) or ""
    if not suffix:
        suffix = ".jpg"
    image_path = settings.image_download_dir / f"{image.chat_id}_{image.message_id}{suffix}"
    await bot.download(image.file_id, destination=image_path)
    return image_path


async def download_video(settings: Settings, bot: Bot, video: StoredVideo) -> Path:
    settings.video_download_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(video.file_name).suffix if video.file_name else ""
    if not suffix and video.mime_type:
        suffix = mimetypes.guess_extension(video.mime_type) or ""
    if not suffix:
        suffix = ".mp4"
    video_path = settings.video_download_dir / f"{video.chat_id}_{video.message_id}{suffix}"
    try:
        await bot.download(video.file_id, destination=video_path)
    except TelegramBadRequest as exc:
        if "file is too big" in str(exc).lower():
            video_path.unlink(missing_ok=True)
            limit = effective_video_size_limit_mb(settings)
            raise TelegramDownloadTooLargeError(
                "Видео слишком большое для скачивания через Telegram Bot API. "
                f"Лимит скачивания: {telegram_download_limit_label(settings)}. "
                f"Лимит распознавания бота: {limit} MB. "
                "Сожмите/обрежьте видео или отправьте кружочек/короткий фрагмент."
            ) from exc
        raise
    return video_path


async def extract_video_audio(settings: Settings, video_path: Path, video: StoredVideo) -> Path | None:
    settings.video_download_dir.mkdir(parents=True, exist_ok=True)
    audio_path = settings.video_download_dir / f"{video.chat_id}_{video.message_id}_audio.wav"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-map",
        "0:a:0?",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        str(audio_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        logging.warning("Video audio extraction failed for %s: %s", video_path, detail[:500])
        audio_path.unlink(missing_ok=True)
        return None
    if not audio_path.exists() or audio_path.stat().st_size <= 44:
        audio_path.unlink(missing_ok=True)
        return None
    return audio_path


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
    question_llm = build_llm_client(settings, model=settings.question_model or None)
    summarizer = Summarizer(llm, settings.chunk_chars)
    chat_assistant = ChatAssistant(question_llm, settings.chunk_chars)
    chat_memory = ChatMemory(store, llm, settings) if settings.memory_enabled else None
    transcript_formatter_llm = (
        build_llm_client(
            settings,
            provider=settings.transcription_format_provider,
            model=settings.transcription_format_model,
            num_ctx=settings.transcription_format_num_ctx,
            num_predict=settings.transcription_format_num_predict,
        )
        if settings.transcribe_voice
        and settings.transcription_format_enabled
        and settings.transcription_format_model
        else None
    )
    transcript_formatter = (
        TranscriptFormatter(
            transcript_formatter_llm,
            model_name=settings.transcription_format_model,
            max_chars=settings.max_transcription_format_chars,
        )
        if transcript_formatter_llm
        else None
    )
    image_recognizer = ImageRecognizer(settings)
    meme_generator = MemeGenerator(settings)
    video_recognizer = VideoRecognizer(settings)
    wiki_search = WikipediaSearchClient(settings)
    transcriber = (
        FasterWhisperTranscriber(settings)
        if settings.transcribe_voice or settings.video_transcribe_audio
        else None
    )
    gpu_lock = asyncio.Lock()

    bot = Bot(token=settings.telegram_bot_token)
    dp = await create_dispatcher(
        settings,
        store,
        summarizer,
        chat_assistant,
        chat_memory,
        image_recognizer,
        meme_generator,
        video_recognizer,
        wiki_search,
        transcriber,
        transcript_formatter,
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
