from __future__ import annotations

import asyncio
import html
import json
import logging
import mimetypes
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from weakref import WeakValueDictionary

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from tg_summary_bot.addressing import remove_aliases_from_text, split_aliases
from tg_summary_bot.autonomous_jokes import AutonomousJokeWorker, log_worker_failure
from tg_summary_bot.assistant import ChatAssistant
from tg_summary_bot.casino import CASINO_STAKE, CasinoTrigger, bot_request_casino_trigger, slot_result
from tg_summary_bot.config import Settings, load_settings
from tg_summary_bot.image_recognizer import ImageRecognizer
from tg_summary_bot.intent_router import (
    IntentRoute,
    IntentRouter,
    IntentRouterProtocol,
    is_joke_request,
)
from tg_summary_bot.joke_awards import JokeSelector
from tg_summary_bot.llm import build_llm_client
from tg_summary_bot.memory import ChatMemory, MemoryCompressionError, participant_key, should_use_memory
from tg_summary_bot.meme_generator import MemeGenerator
from tg_summary_bot.observability import (
    OperationOutcome,
    log_operation_outcome,
    opik_track,
    update_opik_span_metadata,
)
from tg_summary_bot.periods import format_period, parse_period
from tg_summary_bot.storage import CasinoSpin, CasinoSpinResult, MessageStore, PointBalance, StoredImage, StoredVideo
from tg_summary_bot.summarizer import Summarizer
from tg_summary_bot.transcriber import FasterWhisperTranscriber
from tg_summary_bot.transcript_formatter import TranscriptFormatter
from tg_summary_bot.video_recognizer import VideoRecognizer
from tg_summary_bot.web_search import WikipediaSearchClient, format_wiki_results
from tg_summary_bot.youtube import (
    YouTubeDownloadError,
    download_youtube_video,
    youtube_url_from_message,
)


RESPONSE_LOGGER_NAME = "tg_summary_bot.responses"
SURPRISE_MEME_CHANCE = 0.02
CASINO_SETTLEMENT_RETRY_DELAYS = (0.25, 0.5)
_BOT_IDENTITIES: dict[int, tuple[int, str | None, str]] = {}
_LEADERBOARD_LOCKS: WeakValueDictionary[
    tuple[asyncio.AbstractEventLoop, int], asyncio.Lock
] = WeakValueDictionary()


class TelegramDownloadTooLargeError(RuntimeError):
    pass


def telegram_html(text: str) -> str:
    rendered = html.escape(text, quote=False)
    rendered = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", rendered)
    return rendered


def is_allowed(settings: Settings, chat_id: int) -> bool:
    return not settings.allowed_chat_ids or chat_id in settings.allowed_chat_ids


def joke_awards_enabled(settings: Settings, chat_id: int) -> bool:
    return chat_id not in getattr(settings, "joke_awards_disabled_chat_ids", set())


def casino_enabled(settings: Settings, chat_id: int) -> bool:
    return chat_id not in getattr(settings, "casino_disabled_chat_ids", set())


async def cached_bot_identity(bot: Bot) -> tuple[int, str | None, str]:
    identity = _BOT_IDENTITIES.get(id(bot))
    if identity is None:
        me = await bot.get_me()
        identity = (me.id, me.username, me.full_name or me.username or str(me.id))
        _BOT_IDENTITIES[id(bot)] = identity
    return identity


def resolved_intent_router_model(settings: Settings) -> str:
    fallback = (
        settings.ollama_model
        if settings.resolved_llm_provider == "ollama"
        else settings.openai_model
    )
    return settings.intent_router_model or settings.question_model or fallback


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


def has_current_or_replied_image(message: Message) -> bool:
    return bool(image_from_message(message) or (message.reply_to_message and image_from_message(message.reply_to_message)))


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
    transcript_formatter: TranscriptFormatter | None,
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
    formatter = transcript_formatter.cache_key if transcript_formatter else "transcript_format=off"
    return f"{video_recognizer.cache_key}|{audio}|{formatter}"


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


def has_bot_command_entity(message: Message) -> bool:
    entities = message.entities if message.text else message.caption_entities
    return any(entity_type_name(entity) == "bot_command" for entity in entities or [])


def extract_addressed_request(
    message: Message,
    *,
    bot_id: int | None,
    bot_username: str | None,
    normalized_aliases: list[str],
) -> str | None:
    text = message.text or message.caption or ""
    if not text:
        return None

    entities = message.entities if message.text else message.caption_entities
    addressed = False
    request = text
    for entity in entities or []:
        entity_type = entity_type_name(entity)
        if entity_type == "mention" and bot_username:
            mention = entity.extract_from(text).lstrip("@").lower()
            if mention == bot_username.lower():
                addressed = True
                request = request.replace(entity.extract_from(text), "", 1)
        elif entity_type == "text_mention" and bot_id:
            user = getattr(entity, "user", None)
            if user and user.id == bot_id:
                addressed = True
                request = request.replace(entity.extract_from(text), "", 1)

    without_aliases = remove_aliases_from_text(request, normalized_aliases)
    if without_aliases is not None:
        addressed = True
        request = without_aliases
    return " ".join(request.split()) if addressed else None


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


async def route_under_gpu_lock(
    router: IntentRouterProtocol,
    gpu_lock: asyncio.Lock,
    request: str,
) -> IntentRoute:
    """Route and unload while holding the shared GPU lock; actions run afterwards."""
    async with gpu_lock:
        try:
            return await router.route(request)
        finally:
            await router.unload()


def should_generate_surprise_meme(
    *,
    route: IntentRoute,
    request: str,
    has_image: bool,
    random_value: float,
) -> bool:
    return (
        route.valid
        and route.action == "question"
        and route.reason == "ok"
        and is_joke_request(request)
        and has_image
        and random_value < SURPRISE_MEME_CHANCE
    )


def render_best_joke_section(
    *,
    winner_name: str | None = None,
    winner_text: str | None = None,
    award_status: str | None = None,
) -> str:
    if not winner_name or winner_text is None:
        return "Лучшая шутка: Не нашлось."
    quote = winner_text[:1000]
    line = f"Лучшая шутка: {winner_name}: «{quote}»"
    if award_status == "awarded":
        return line + " (+10 очков)"
    if award_status == "already_awarded":
        return line + "\nУже была награждена; +0."
    if award_status == "committed":
        return line + " (+10 очков, начислено ранее)"
    return "Лучшая шутка: не удалось определить; очки не начислены."


def render_leaderboard(balances: list[PointBalance]) -> str:
    if not balances:
        return "Топ балансов: пока нет положительных балансов."
    return "Топ балансов:\n" + "\n".join(
        f"{index}. {balance.participant_name} - {balance.balance}"
        for index, balance in enumerate(balances, start=1)
    )


async def leaderboard_display_names(bot: Bot, store: MessageStore, chat_id: int) -> dict[str, str]:
    """Aliases are presentation-only, but must be resolved before tie ordering."""
    try:
        bot_id, _username, fallback_name = await cached_bot_identity(bot)
        aliases = await store.get_chat_bot_aliases(chat_id)
        return {f"id:{bot_id}": aliases[0].alias if aliases else fallback_name}
    except Exception:  # noqa: BLE001
        return {}


async def refresh_pinned_leaderboard(
    *,
    bot: Bot,
    store: MessageStore,
    source_message: Message,
) -> None:
    """Best-effort refresh; leaderboard delivery never affects an awarded transaction."""
    await refresh_pinned_leaderboard_for_chat(
        bot=bot,
        store=store,
        chat_id=source_message.chat.id,
        source_message=source_message,
    )


async def refresh_existing_pinned_leaderboard(*, bot: Bot, store: MessageStore, chat_id: int) -> None:
    if not await store.get_pinned_leaderboard(chat_id=chat_id):
        return
    if not await refresh_pinned_leaderboard_for_chat(bot=bot, store=store, chat_id=chat_id):
        logging.warning("Pinned leaderboard alias refresh failed chat_id=%s", chat_id)


async def refresh_recovered_casino_leaderboards(
    *, settings: Settings, bot: Bot, store: MessageStore, chat_ids: set[int]
) -> None:
    for chat_id in sorted(chat_ids):
        if not is_allowed(settings, chat_id):
            continue
        try:
            await refresh_pinned_leaderboard_for_chat(bot=bot, store=store, chat_id=chat_id)
        except Exception:  # noqa: BLE001
            logging.exception("Startup casino leaderboard refresh failed chat_id=%s", chat_id)


def telegram_message_not_found(error: Exception) -> bool:
    return isinstance(error, TelegramBadRequest) and "not found" in str(error).lower()


def telegram_message_not_modified(error: Exception) -> bool:
    return isinstance(error, TelegramBadRequest) and "message is not modified" in str(error).lower()


async def refresh_pinned_leaderboard_for_chat(
    *,
    bot: Bot,
    store: MessageStore,
    chat_id: int,
    source_message: Message | None = None,
) -> bool:
    # Hold a strong reference across waiters. Idle locks disappear automatically;
    # the loop key keeps separate bot lifecycles from reusing an old bound lock.
    key = (asyncio.get_running_loop(), chat_id)
    lock = _LEADERBOARD_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        return await _refresh_pinned_leaderboard_for_chat(
            bot=bot, store=store, chat_id=chat_id, source_message=source_message,
        )


async def _refresh_pinned_leaderboard_for_chat(
    *, bot: Bot, store: MessageStore, chat_id: int, source_message: Message | None = None,
) -> bool:
    """Refresh current wallets; recreate only a confirmed deleted Telegram message."""
    text = render_leaderboard(
        await store.get_top_point_balances(
            chat_id=chat_id, limit=10, display_names=await leaderboard_display_names(bot, store, chat_id)
        )
    )
    pinned = await store.get_pinned_leaderboard(chat_id=chat_id)
    message_id: int | None = pinned.message_id if pinned else None
    if message_id is not None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=telegram_html(text),
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("Pinned leaderboard edit failed chat_id=%s", chat_id, exc_info=True)
            if telegram_message_not_found(exc):
                try:
                    await store.delete_pinned_leaderboard(chat_id=chat_id)
                except Exception:  # noqa: BLE001
                    logging.warning("Pinned leaderboard state cleanup failed chat_id=%s", chat_id, exc_info=True)
                    return False
                message_id = None
            elif telegram_message_not_modified(exc):
                # The previous attempt may have edited successfully before crashing; still retry pinning.
                pass
            else:
                return False
    if message_id is None:
        response = (
            await answer_logged(source_message, text)
            if source_message
            else await bot.send_message(chat_id, telegram_html(text), parse_mode="HTML")
        )
        message_id = response.message_id
        try:
            await store.save_pinned_leaderboard(chat_id=chat_id, message_id=message_id)
        except Exception:  # noqa: BLE001
            logging.warning("Pinned leaderboard state save failed chat_id=%s", chat_id, exc_info=True)
            return False
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception:  # noqa: BLE001
        logging.warning("Pinned leaderboard pin failed chat_id=%s", chat_id, exc_info=True)
        return False
    return True


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


def valid_slot_dice(sent: object, *, chat_id: int) -> tuple[int, int] | None:
    chat = getattr(sent, "chat", None)
    dice = getattr(sent, "dice", None)
    message_id = getattr(sent, "message_id", None)
    value = getattr(dice, "value", None)
    if (
        not chat
        or getattr(chat, "id", None) != chat_id
        or type(message_id) is not int
        or message_id <= 0
        or not dice
        or getattr(dice, "emoji", None) != "🎰"
        or type(value) is not int
        or not 1 <= value <= 64
    ):
        return None
    return message_id, value


def render_casino_spin(spin: CasinoSpin) -> str:
    if spin.status == "pending":
        return "Этот спин уже обрабатывается."
    if spin.status == "refunded":
        return (
            f"Спин отменён; ставка {spin.stake} очков возвращена. "
            f"Баланс: {spin.terminal_balance}."
        )
    net = (spin.payout or 0) - spin.stake
    category = {"none": "нет совпадений", "pair": "пара", "triple": "три одинаковых", "jackpot": "джекпот"}.get(
        spin.category or "", "неизвестно"
    )
    return (
        f"Слот: {category}. Ставка: {spin.stake}; выплата: {spin.payout}; "
        f"итог: {net:+d}; баланс: {spin.terminal_balance}."
    )


def casino_outcome_metadata(spin: CasinoSpin | None) -> dict[str, object]:
    if not spin:
        return {}
    return {
        "spin_id": spin.spin_id,
        "spin_status": spin.status,
        "rules_version": spin.rules_version,
        "stake": spin.stake,
        "category": spin.category,
        "payout": spin.payout,
        "net": (spin.payout - spin.stake) if spin.payout is not None else None,
        "terminal_balance": spin.terminal_balance,
    }


async def run_casino_dice_lifecycle(
    *,
    chat_id: int,
    spin_id: int,
    send_dice: Callable[[], Awaitable[object]],
    settle: Callable[[int, int], Awaitable[CasinoSpinResult]],
    refund: Callable[[str], Awaitable[CasinoSpinResult]],
) -> tuple[str, CasinoSpinResult | None]:
    """Shared trusted Telegram Dice lifecycle for human and automatic casino flows."""
    try:
        sent = await send_dice()
    except asyncio.CancelledError:
        await asyncio.shield(refund("telegram_cancelled"))
        raise
    except Exception:  # noqa: BLE001
        logging.exception("Casino Dice send failed chat_id=%s spin_id=%s", chat_id, spin_id)
        return "telegram_outcome_unknown", await refund("telegram_outcome_unknown")
    dice = valid_slot_dice(sent, chat_id=chat_id)
    if not dice:
        return "telegram_response_invalid", await refund("telegram_response_invalid")
    for attempt in range(len(CASINO_SETTLEMENT_RETRY_DELAYS) + 1):
        try:
            return "completed", await settle(*dice)
        except asyncio.CancelledError:
            await asyncio.shield(refund("settlement_cancelled"))
            raise
        except Exception:  # noqa: BLE001
            logging.exception(
                "Casino settlement failed chat_id=%s spin_id=%s attempt=%s",
                chat_id, spin_id, attempt + 1,
            )
        if attempt < len(CASINO_SETTLEMENT_RETRY_DELAYS):
            try:
                await asyncio.sleep(CASINO_SETTLEMENT_RETRY_DELAYS[attempt])
            except asyncio.CancelledError:
                await asyncio.shield(refund("settlement_cancelled"))
                raise
    return "settlement_unconfirmed", None


async def spin_casino(
    settings: Settings,
    store: MessageStore,
    bot: Bot,
    message: Message,
    *,
    participant: tuple[str, str] | None = None,
    trigger: CasinoTrigger | None = None,
) -> OperationOutcome:
    """Run one idempotent virtual slot operation without the shared GPU lock."""
    if not is_allowed(settings, message.chat.id):
        return OperationOutcome("casino", "rejected", "access_denied")
    if not casino_enabled(settings, message.chat.id):
        response = await reply_logged(message, "Казино отключено для этого чата.")
        return OperationOutcome("casino", "rejected", "feature_disabled", response_message_id=response.message_id)
    if (
        message.chat.type == "channel"
        or not message.from_user
        or message.from_user.is_bot
        or message.sender_chat is not None
        or type(message.message_id) is not int
        or message.message_id <= 0
    ):
        return OperationOutcome("casino", "rejected", "invalid_sender")
    key, name = participant or participant_ref(message)
    trigger = trigger or None
    reserve = await store.reserve_casino_spin(
        chat_id=message.chat.id,
        request_message_id=message.message_id,
        participant_key=key,
        participant_name=name,
        trigger=trigger,
    )
    if reserve.status == "insufficient_balance":
        response = await reply_logged(message, f"Для слота нужно {CASINO_STAKE} очков.")
        return OperationOutcome("casino", "rejected", "insufficient_balance", response_message_id=response.message_id)
    if reserve.status == "identity_conflict":
        logging.error("Casino invariant identity_conflict chat_id=%s message_id=%s", message.chat.id, message.message_id)
        response = await reply_logged(message, "Не удалось безопасно обработать повторный спин.")
        return OperationOutcome("casino", "rejected", "identity_conflict", response_message_id=response.message_id)
    if reserve.status.startswith("existing_") and reserve.spin:
        response = await reply_logged(message, render_casino_spin(reserve.spin))
        return OperationOutcome(
            "casino", "succeeded", reserve.status, response_message_id=response.message_id,
            metadata=casino_outcome_metadata(reserve.spin),
        )
    if reserve.status != "created" or not reserve.spin:
        return OperationOutcome("casino", "failed", "reserve_failed")
    spin = reserve.spin
    outcome_reason, terminal = await run_casino_dice_lifecycle(
        chat_id=message.chat.id,
        spin_id=spin.spin_id,
        send_dice=lambda: message.reply_dice(emoji="🎰"),
        settle=lambda dice_message_id, dice_value: store.settle_casino_spin(
            spin_id=spin.spin_id, dice_message_id=dice_message_id, dice_value=dice_value
        ),
        refund=lambda reason: store.refund_casino_spin(spin_id=spin.spin_id, reason=reason),
    )
    if (
        outcome_reason == "settlement_unconfirmed" or not terminal or not terminal.spin
        or (outcome_reason == "completed" and terminal.status not in {"completed", "already_completed"})
    ):
        response = await reply_logged(
            message,
            "Не удалось подтвердить результат спина. Повторно слот не запускаю. "
            "Если ставка осталась зарезервирована, она будет возвращена при перезапуске бота.",
        )
        return OperationOutcome(
            "casino", "failed", terminal.status if terminal else outcome_reason,
            response_message_id=response.message_id,
        )
    response = await reply_logged(message, render_casino_spin(terminal.spin))
    try:
        await refresh_pinned_leaderboard(bot=bot, store=store, source_message=message)
    except Exception:  # noqa: BLE001
        logging.exception("Casino leaderboard refresh failed chat_id=%s", message.chat.id)
    return OperationOutcome(
        "casino", "succeeded" if outcome_reason == "completed" else "partial", outcome_reason,
        response_message_id=response.message_id, metadata=casino_outcome_metadata(terminal.spin),
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
    intent_router: IntentRouterProtocol,
    joke_selector: JokeSelector | None = None,
) -> Dispatcher:
    dp = Dispatcher()
    profile_refresh_tasks: set[asyncio.Task[None]] = set()

    async def execute_routed_operation(
        message: Message,
        route: IntentRoute,
        operation: str,
        action: Awaitable[OperationOutcome | None],
        *,
        started: float,
    ) -> None:
        try:
            result = await action
        except Exception as exc:
            outcome = OperationOutcome(
                operation=operation,
                status="failed",
                reason="unhandled_exception",
            )
            log_operation_outcome(
                outcome,
                chat_id=message.chat.id,
                message_id=message.message_id,
                invocation="routed",
                route_action=route.action,
                route_reason=route.reason,
                elapsed_seconds=time.perf_counter() - started,
                provider=settings.resolved_llm_provider,
                model=resolved_intent_router_model(settings),
            )
            logging.exception(
                "Routed operation failed operation=%s exception_type=%s",
                operation,
                type(exc).__name__,
            )
            raise
        outcome = result or OperationOutcome(
            operation=operation,
            status="succeeded",
            reason="completed",
        )
        log_operation_outcome(
            outcome,
            chat_id=message.chat.id,
            message_id=message.message_id,
            invocation="routed",
            route_action=route.action,
            route_reason=route.reason,
            elapsed_seconds=time.perf_counter() - started,
            provider=settings.resolved_llm_provider,
            model=resolved_intent_router_model(settings),
        )

    async def execute_slash_operation(
        message: Message,
        operation: str,
        action: Awaitable[OperationOutcome | None],
    ) -> None:
        started = time.perf_counter()
        try:
            result = await action
        except Exception as exc:
            outcome = OperationOutcome(operation, "failed", "unhandled_exception")
            logging.exception(
                "Slash operation failed operation=%s exception_type=%s",
                operation,
                type(exc).__name__,
            )
            log_operation_outcome(
                outcome,
                chat_id=message.chat.id,
                message_id=message.message_id,
                invocation="slash",
                elapsed_seconds=time.perf_counter() - started,
            )
            raise
        log_operation_outcome(
            result or OperationOutcome(operation, "succeeded", "completed"),
            chat_id=message.chat.id,
            message_id=message.message_id,
            invocation="slash",
            elapsed_seconds=time.perf_counter() - started,
        )

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
        bot_id, username, _fallback_name = await cached_bot_identity(bot)
        return bot_id, username

    async def bot_participant(message: Message, bot: Bot) -> tuple[str, str] | None:
        bot_id, username = await get_bot_identity(bot)
        if bot_id is None:
            return None
        aliases = await store.get_chat_bot_aliases(message.chat.id)
        _bot_id, _username, fallback_name = await cached_bot_identity(bot)
        return f"id:{bot_id}", aliases[0].alias if aliases else fallback_name

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
            "`/balance` - your joke points in this chat\n"
            "`/top` - top joke points in this chat\n"
            "`/casino` - spin the virtual slot for 10 chat points\n"
            "`/casino bot` - let the bot spin with its own points\n"
            "`/question 24h <text>` - chat with the assistant using recent context\n"
            "`/alias add <name[, name]>` - add chat names for the bot\n"
            "`/alias list` - show configured bot names\n"
            "`/alias remove <name>` - remove a bot name\n"
            "`/wiki <text>` - search Wikipedia and save the result for chat context\n"
            "`/memory` - compressed chat memory status; `/memory rebuild` resets blocks\n"
            "`/profile [name]` - show your, replied, or named participant profile\n"
            "`/profile correct [@name] <fact>` - save a profile fact for yourself, "
            "named, or replied participant\n"
            "`/transcribe` - transcribe replied voice/audio\n"
            "`/image` - recognize the latest image or replied image\n"
            "`/meme` - make a meme from replied/latest image\n"
            "`/video [YouTube URL]` - recognize a replied/latest Telegram video or one YouTube video\n"
            "`/compare 10m` - compare summaries across Ollama models\n"
            "`/stats` - chat_id and stored message count\n\n"
            "Voice messages are transcribed automatically when enabled. "
            "Video notes are recognized automatically. "
            "Mention the bot or use an alias in a message to ask, summarize, search Wikipedia, "
            "recognize media, make a meme, transcribe a replied voice/audio, or show a profile.\n\n"
            f"Current chat_id: `{message.chat.id}`"
        )

    @dp.message(Command("alias"))
    async def alias_command(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        parts = (message.text or "").split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else ""
        argument = parts[2].strip() if len(parts) > 2 else ""

        if action == "list" and not argument:
            aliases = await store.get_chat_bot_aliases(message.chat.id)
            if not aliases:
                await answer_logged(message, "Алиасы для этого чата пока не настроены.")
                return
            await answer_logged(
                message,
                "Алиасы бота в этом чате:\n" + "\n".join(f"- `{item.alias}`" for item in aliases),
            )
            return

        if action not in {"add", "remove"} or not argument:
            await answer_logged(
                message,
                "Использование: `/alias add Реле, Релейка`\n"
                "`/alias list`\n"
                "`/alias remove Реле`",
            )
            return
        if not message.from_user:
            await answer_logged(message, "Не удалось определить автора сообщения.")
            return

        aliases = split_aliases(argument)
        if not aliases:
            await answer_logged(message, "Укажите имя бота из букв или цифр.")
            return
        if action == "remove" and len(aliases) != 1:
            await answer_logged(message, "За один раз можно удалить только один алиас.")
            return

        if action == "add":
            added: list[str] = []
            existing: list[str] = []
            for alias, normalized in aliases:
                inserted = await store.add_chat_bot_alias(
                    chat_id=message.chat.id,
                    alias=alias,
                    normalized_alias=normalized,
                    created_by_user_id=message.from_user.id if message.from_user else None,
                    created_at=message.date,
                )
                (added if inserted else existing).append(alias)
            text = ""
            if added:
                text += "Добавлены алиасы: " + ", ".join(f"`{item}`" for item in added) + "."
            if existing:
                text += ("\n" if text else "") + "Уже есть: " + ", ".join(
                    f"`{item}`" for item in existing
                ) + "."
            await answer_logged(message, text)
            if added:
                await refresh_existing_pinned_leaderboard(bot=bot, store=store, chat_id=message.chat.id)
            return

        alias, normalized = aliases[0]
        removed = await store.remove_chat_bot_alias(
            chat_id=message.chat.id,
            normalized_alias=normalized,
        )
        if removed:
            await answer_logged(message, f"Алиас `{alias}` удалён.")
            await refresh_existing_pinned_leaderboard(bot=bot, store=store, chat_id=message.chat.id)
        else:
            await answer_logged(message, f"Алиас `{alias}` не найден в этом чате.")

    @dp.message(Command("stats"))
    async def stats_command(message: Message, bot: Bot) -> None:
        count = await store.count_messages(message.chat.id)
        image_count = await store.count_images(message.chat.id)
        video_count = await store.count_videos(message.chat.id)
        leaders = await store.get_top_point_balances(
            chat_id=message.chat.id, limit=1,
            display_names=await leaderboard_display_names(bot, store, message.chat.id),
        )
        leader = f"{leaders[0].participant_name}: {leaders[0].balance}" if leaders else "none"
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
            f"intent_router_model: `{settings.intent_router_model or settings.question_model or settings.ollama_model}`\n"
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
            f"autonomous_jokes_enabled: `{settings.autonomous_jokes_enabled}`\n"
            f"autonomous_jokes_shadow_mode: `{settings.autonomous_jokes_shadow_mode}`\n"
            f"autonomous_joke_judge_model: `{settings.autonomous_joke_judge_model or (settings.ollama_model if settings.resolved_llm_provider == 'ollama' else settings.openai_model)}`\n"
            f"autonomous_joke_announce: `{settings.autonomous_joke_announce}`\n"
            f"bot_auto_casino_enabled: `{settings.bot_auto_casino_enabled}`\n"
            f"bot_auto_casino_chance: `{settings.bot_auto_casino_chance}`\n"
            f"joke_awards_enabled: `{joke_awards_enabled(settings, message.chat.id)}`\n"
            f"casino_enabled: `{casino_enabled(settings, message.chat.id)}`\n"
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
            f"leader: `{leader}`\n"
            f"access_allowed: `{is_allowed(settings, message.chat.id)}`"
        )

    @dp.message(Command("balance"))
    async def balance_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        if not message.from_user:
            await answer_logged(message, "Не удалось определить участника.")
            return
        key, name = participant_ref(message)
        balance = await store.get_point_balance(chat_id=message.chat.id, participant_key=key)
        await answer_logged(message, f"Баланс {name}: `{balance.balance if balance else 0}` очков.")

    @dp.message(Command("casino"))
    async def casino_command(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        args = (message.text or "").split()
        if len(args) > 2 or (len(args) == 2 and args[1].lower() != "bot"):
            await answer_logged(message, "Использование: `/casino` или `/casino bot`.")
            return
        requested_bot = len(args) == 2
        if not requested_bot:
            await execute_slash_operation(message, "casino", spin_casino(settings, store, bot, message))
            return
        identity = await bot_participant(message, bot)
        if not identity:
            await answer_logged(message, "Не удалось определить identity бота.")
            return
        await execute_slash_operation(
            message,
            "casino_bot",
            spin_casino(
                settings, store, bot, message, participant=identity,
                trigger=bot_request_casino_trigger(message.message_id),
            ),
        )

    @dp.message(Command("top"))
    async def top_command(message: Message, bot: Bot) -> None:
        if not is_allowed(settings, message.chat.id):
            return
        balances = await store.get_top_point_balances(
            chat_id=message.chat.id, limit=10,
            display_names=await leaderboard_display_names(bot, store, message.chat.id),
        )
        if not balances:
            await answer_logged(message, "Положительных балансов пока нет.")
            return
        await answer_logged(message, render_leaderboard(balances))

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
            log_operation_outcome(
                OperationOutcome("profile_show", "rejected", "access_denied"),
                chat_id=message.chat.id,
                message_id=message.message_id,
                invocation="slash",
            )
            return

        parts = (message.text or "").split(maxsplit=2)
        action = parts[1].lower() if len(parts) > 1 else "show"

        if action in {"forget", "correct"} and not chat_memory:
            await answer_logged(message, "Chat memory is disabled: `MEMORY_ENABLED=false`.")
            return

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

        query = (
            parts[2].strip()
            if action == "show" and len(parts) > 2
            else " ".join(parts[1:]).strip() if action not in {"show", "forget", "correct"} else None
        )
        await execute_slash_operation(
            message,
            "profile_show",
            run_profile_show(message, query, reply_to_source=False),
        )

    async def run_summary(
        message: Message,
        bot: Bot,
        period_raw: str,
        *,
        exclude_message_id: int | None = None,
    ) -> OperationOutcome:
        if not is_allowed(settings, message.chat.id):
            return OperationOutcome("summary", "rejected", "access_denied")
        try:
            period = parse_period(period_raw)
        except ValueError as exc:
            response = await answer_logged(message, f"Could not parse period: {exc}")
            return OperationOutcome(
                "summary",
                "rejected",
                "invalid_period",
                response_message_id=response.message_id,
            )

        now = datetime.now(timezone.utc)
        since = now - period
        snapshot_boundary = await store.get_eligible_snapshot_boundary(
            chat_id=message.chat.id,
            since=since,
        )
        wait_message = await answer_logged(message, "Collecting messages and building summary...")
        use_memory = should_use_memory(period, chat_memory)
        raw_since = chat_memory.recent_since(now) if use_memory and chat_memory else since
        if raw_since < since:
            raw_since = since
        messages = await store.get_messages_since(
            chat_id=message.chat.id,
            since=raw_since,
            limit_chars=settings.max_summary_input_chars,
        )
        if exclude_message_id is not None:
            messages = [item for item in messages if item.message_id != exclude_message_id]
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
                    if exclude_message_id is not None:
                        context_messages = [
                            item for item in context_messages if item.message_id != exclude_message_id
                        ]
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
            return OperationOutcome(
                "summary",
                "failed",
                "generation_error",
                response_message_id=wait_message.message_id,
            )
        logging.info(
            "Summary finished chat_id=%s period=%s elapsed_s=%.1f",
            message.chat.id,
            period_raw,
            time.perf_counter() - started,
        )

        joke_section = "Лучшая шутка: не удалось определить; очки не начислены."
        selector_status = "not_started"
        award_status: str | None = None
        leaderboard_needs_refresh = False
        autonomous_enabled = bool(getattr(settings, "autonomous_jokes_enabled", False))
        if not joke_awards_enabled(settings, message.chat.id):
            selector_status = "disabled_for_chat"
            joke_section = "Лучшая шутка: отключено для этого чата."
        elif autonomous_enabled:
            selector_status = "autonomous_read_only"
            job = await store.get_latest_autonomous_award(chat_id=message.chat.id, since=since)
            if job and job.winner_participant_name and job.winner_source_text is not None:
                joke_section = render_best_joke_section(
                    winner_name=job.winner_participant_name,
                    winner_text=job.winner_source_text,
                    award_status="committed",
                )
            else:
                joke_section = render_best_joke_section()
        elif snapshot_boundary is None:
            joke_section = render_best_joke_section()
            selector_status = "no_eligible_messages"
        elif joke_selector:
            try:
                # This is deliberately a second lock phase: summary unload completed above.
                async with gpu_lock:
                    try:
                        selection = await joke_selector.choose(
                            store,
                            chat_id=message.chat.id,
                            since=since,
                            boundary=snapshot_boundary,
                            exclude_message_id=exclude_message_id,
                        )
                    finally:
                        await joke_selector.unload()
                selector_status = selection.status
                if selection.status == "none":
                    joke_section = render_best_joke_section()
                elif selection.status == "selected" and selection.winner:
                    try:
                        award = await store.award_unique_joke(
                            chat_id=message.chat.id,
                            source_message_id=selection.winner.message_id,
                        )
                    except Exception:  # noqa: BLE001
                        logging.exception("Joke award persistence failed chat_id=%s", message.chat.id)
                    else:
                        award_status = award.status
                        leaderboard_needs_refresh = award.status == "awarded"
                        if award.status in {"awarded", "already_awarded"}:
                            joke_section = render_best_joke_section(
                                winner_name=award.participant_name,
                                winner_text=award.source_text,
                                award_status=award.status,
                            )
            except Exception:  # noqa: BLE001
                selector_status = "error"
                logging.exception("Joke selection failed chat_id=%s", message.chat.id)
        update_opik_span_metadata(
            {
                "joke_snapshot_boundary": (
                    f"{snapshot_boundary.created_at}:{snapshot_boundary.message_id}"
                    if snapshot_boundary
                    else None
                ),
                "joke_selector_status": selector_status,
                "joke_award_status": award_status,
            }
        )

        header = f"**Саммари за {format_period(period_raw)}**\n"
        text = header + summary + "\n\n" + joke_section
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)
        if leaderboard_needs_refresh:
            try:
                await refresh_pinned_leaderboard(
                    bot=bot,
                    store=store,
                    source_message=message,
                )
            except Exception:  # noqa: BLE001
                logging.exception("Pinned leaderboard refresh failed chat_id=%s", message.chat.id)
        schedule_profile_refresh(message.chat.id, now=now)
        return OperationOutcome(
            "summary",
            "succeeded",
            "ok",
            response_message_id=wait_message.message_id,
        )

    @dp.message(Command("summary"))
    async def summary_command(message: Message, bot: Bot) -> None:
        args = (message.text or "").split(maxsplit=1)
        await execute_slash_operation(
            message,
            "summary",
            run_summary(
                message,
                bot,
                args[1].strip() if len(args) > 1 else settings.default_summary_period,
            ),
        )

    async def answer_chat_question(
        message: Message,
        *,
        period_raw: str,
        question: str,
        reply_to_source: bool = False,
        exclude_message_id: int | None = None,
    ) -> OperationOutcome:
        if not is_allowed(settings, message.chat.id):
            return OperationOutcome("question", "rejected", "access_denied")

        try:
            period = parse_period(period_raw)
        except ValueError as exc:
            response = await answer_logged(message, f"Could not parse period: {exc}")
            return OperationOutcome(
                "question",
                "rejected",
                "invalid_period",
                response_message_id=response.message_id,
            )

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
        if exclude_message_id is not None:
            messages = [item for item in messages if item.message_id != exclude_message_id]
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
                    if exclude_message_id is not None:
                        context_messages = [
                            item for item in context_messages if item.message_id != exclude_message_id
                        ]
                    aliases = await store.get_chat_bot_aliases(message.chat.id)
                    answer = await chat_assistant.ask(
                        context_messages,
                        format_period(period_raw),
                        question,
                        bot_names=[item.alias for item in aliases],
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
            return OperationOutcome(
                "question",
                "failed",
                "generation_error",
                response_message_id=wait_message.message_id,
            )

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
        persisted = await try_save_final_assistant_answer(settings, store, wait_message, text)
        schedule_profile_refresh(message.chat.id, now=now)
        return OperationOutcome(
            "question",
            "succeeded" if persisted else "partial",
            "ok" if persisted else "context_persistence_failed",
            persisted=persisted,
            response_message_id=wait_message.message_id,
        )

    async def handle_addressed_message(message: Message, bot: Bot) -> bool:
        if (
            not is_allowed(settings, message.chat.id)
            or message.chat.type == "channel"
            or (message.from_user and message.from_user.is_bot)
        ):
            return False
        if has_bot_command_entity(message):
            return False
        bot_id, bot_username = await get_bot_identity(bot)
        aliases = await store.get_chat_bot_aliases(message.chat.id)
        request = extract_addressed_request(
            message,
            bot_id=bot_id,
            bot_username=bot_username,
            normalized_aliases=[item.normalized_alias for item in aliases],
        )
        if request is None:
            return False
        if not request:
            await reply_logged(message, "Да? Напишите, что нужно сделать.")
            log_operation_outcome(
                OperationOutcome("router", "rejected", "empty_request"),
                chat_id=message.chat.id,
                message_id=message.message_id,
                invocation="routed",
            )
            return True
        started = time.perf_counter()
        route = await route_under_gpu_lock(intent_router, gpu_lock, request)
        logging.info(
            "Intent route chat_id=%s message_id=%s action=%s valid=%s reason=%s elapsed_s=%.3f provider=%s model=%s",
            message.chat.id,
            message.message_id,
            route.action,
            route.valid,
            route.reason,
            time.perf_counter() - started,
            settings.resolved_llm_provider,
            resolved_intent_router_model(settings),
        )
        if route.reason == "missing_wiki_query":
            response = await reply_logged(message, "Что именно найти в Википедии?")
            log_operation_outcome(
                OperationOutcome(
                    "wiki",
                    "rejected",
                    "missing_query",
                    response_message_id=response.message_id,
                ),
                chat_id=message.chat.id,
                message_id=message.message_id,
                invocation="routed",
                route_action=route.action,
                route_reason=route.reason,
                elapsed_seconds=time.perf_counter() - started,
                provider=settings.resolved_llm_provider,
                model=resolved_intent_router_model(settings),
            )
            return True
        if should_generate_surprise_meme(
            route=route,
            request=request,
            has_image=has_current_or_replied_image(message),
            random_value=random.random(),
        ):
            await execute_routed_operation(
                message,
                route,
                "meme",
                run_meme(message, bot),
                started=started,
            )
            return True
        if not route.valid or route.action == "none":
            await execute_routed_operation(
                message,
                route,
                "question",
                answer_chat_question(
                    message,
                    period_raw=settings.default_summary_period,
                    question=request,
                    reply_to_source=True,
                    exclude_message_id=message.message_id,
                ),
                started=started,
            )
            return True
        if route.action == "question":
            await execute_routed_operation(
                message,
                route,
                "question",
                answer_chat_question(
                    message,
                    period_raw=route.period or settings.default_summary_period,
                    question=request,
                    reply_to_source=True,
                    exclude_message_id=message.message_id,
                ),
                started=started,
            )
            return True
        if route.action == "summary":
            await execute_routed_operation(
                message,
                route,
                "summary",
                run_summary(
                    message,
                    bot,
                    route.period or settings.default_summary_period,
                    exclude_message_id=message.message_id,
                ),
                started=started,
            )
            return True
        if route.action == "casino":
            await execute_routed_operation(
                message,
                route,
                "casino",
                spin_casino(settings, store, bot, message),
                started=started,
            )
            return True
        if route.action == "casino_bot":
            identity = await bot_participant(message, bot)
            if not identity:
                return True
            await execute_routed_operation(
                message,
                route,
                "casino_bot",
                spin_casino(
                    settings, store, bot, message, participant=identity,
                    trigger=bot_request_casino_trigger(message.message_id),
                ),
                started=started,
            )
            return True
        if route.action == "wiki":
            await execute_routed_operation(
                message,
                route,
                "wiki",
                run_wiki(message, route.query or "", routed=True),
                started=started,
            )
            return True
        if route.action == "image":
            await execute_routed_operation(
                message,
                route,
                "image",
                run_image(message, bot, routed=True),
                started=started,
            )
            return True
        if route.action == "meme":
            await execute_routed_operation(
                message,
                route,
                "meme",
                run_meme(message, bot),
                started=started,
            )
            return True
        if route.action == "video":
            await execute_routed_operation(
                message,
                route,
                "video",
                run_video(message, bot, routed=True, youtube=youtube_url_from_message(message)),
                started=started,
            )
            return True
        if route.action == "transcribe":
            await execute_routed_operation(
                message,
                route,
                "transcribe",
                run_transcribe(message, bot),
                started=started,
            )
            return True
        if route.action == "profile_show":
            await execute_routed_operation(
                message,
                route,
                "profile_show",
                run_profile_show(message, route.query, reply_to_source=True),
                started=started,
            )
        return True

    @dp.message(Command("question"))
    async def question_command(message: Message) -> None:
        if not is_allowed(settings, message.chat.id):
            log_operation_outcome(
                OperationOutcome("question", "rejected", "access_denied"),
                chat_id=message.chat.id,
                message_id=message.message_id,
                invocation="slash",
            )
            return

        parsed = parse_question_command(message.text or "", settings.default_summary_period)
        if not parsed:
            response = await answer_logged(
                message,
                "Usage: `/question [period] your question`\n"
                "Example: `/question 24h who promised to fix the issue?`",
            )
            log_operation_outcome(
                OperationOutcome(
                    "question",
                    "rejected",
                    "missing_question",
                    response_message_id=response.message_id,
                ),
                chat_id=message.chat.id,
                message_id=message.message_id,
                invocation="slash",
            )
            return

        period_raw, question = parsed
        await execute_slash_operation(
            message,
            "question",
            answer_chat_question(message, period_raw=period_raw, question=question),
        )

    async def run_wiki(
        message: Message,
        search_query: str,
        *,
        routed: bool = False,
    ) -> OperationOutcome:
        if not is_allowed(settings, message.chat.id):
            return OperationOutcome("wiki", "rejected", "access_denied")
        if not settings.wiki_search_enabled:
            response = await answer_logged(
                message,
                "Wikipedia search is disabled: `WIKI_SEARCH_ENABLED=false`.",
            )
            return OperationOutcome(
                "wiki",
                "rejected",
                "feature_disabled",
                response_message_id=response.message_id,
            )

        if not search_query.strip():
            response = await answer_logged(message, "Usage: `/wiki what to search`")
            return OperationOutcome(
                "wiki",
                "rejected",
                "missing_query",
                response_message_id=response.message_id,
            )
        search_query = search_query.strip()
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
            return OperationOutcome(
                "wiki",
                "failed",
                "provider_error",
                response_message_id=wait_message.message_id,
            )

        text = format_wiki_results(search_query, results)
        persisted: bool | None = None
        if results:
            persisted = await try_save_generated_context(
                settings,
                store,
                wait_message if routed else message,
                f"Wikipedia search for {search_query}: {text}",
                limit_chars=settings.max_transcription_chars,
                origin="generated",
                kind="wiki_result",
            )
            text = text + f"\n\n{generated_context_note(persisted)}"
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)
        return OperationOutcome(
            "wiki",
            "succeeded" if persisted is not False else "partial",
            "ok" if results and persisted else "no_results" if not results else "context_persistence_failed",
            persisted=persisted,
            response_message_id=wait_message.message_id,
        )

    @dp.message(Command("wiki"))
    async def wiki_command(message: Message) -> None:
        query = (message.text or "").split(maxsplit=1)
        await execute_slash_operation(
            message,
            "wiki",
            run_wiki(message, query[1] if len(query) > 1 else ""),
        )

    async def run_image(
        message: Message,
        bot: Bot,
        *,
        routed: bool = False,
    ) -> OperationOutcome:
        if not is_allowed(settings, message.chat.id):
            return OperationOutcome("image", "rejected", "access_denied")
        if not settings.image_recognition_model:
            response = await answer_logged(
                message,
                "Image recognition is disabled: IMAGE_RECOGNITION_MODEL is empty.",
            )
            return OperationOutcome(
                "image",
                "rejected",
                "feature_disabled",
                response_message_id=response.message_id,
            )

        image = await resolve_image_for_command(store, message)
        if not image:
            response = await answer_logged(
                message,
                "No image found. Reply to an image with `/image`, or send `/image` after an image.",
            )
            return OperationOutcome(
                "image",
                "rejected",
                "missing_media",
                response_message_id=response.message_id,
            )
        if image_too_large(settings, image):
            response = await answer_logged(
                message,
                "Image is too large: "
                f"{image.file_size} bytes. Limit: {settings.max_image_size_mb} MB.",
            )
            return OperationOutcome(
                "image",
                "rejected",
                "media_too_large",
                response_message_id=response.message_id,
            )

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
            return OperationOutcome(
                "image",
                "failed",
                "recognition_error",
                response_message_id=wait_message.message_id,
            )
        finally:
            if image_path:
                image_path.unlink(missing_ok=True)

        elapsed = time.perf_counter() - started
        saved_text = (
            f"🖼 Image recognition for message #{image.message_id} "
            f"from {image.sender_name}: {result}"
        )
        persisted = await try_save_generated_context(
            settings,
            store,
            wait_message if routed else message,
            saved_text,
            origin="generated",
            kind="image_recognition",
        )
        text = (
            f"**Image recognition for message #{image.message_id}**\n"
            f"Source: {image.sender_name}\n"
            f"Model: `{settings.image_recognition_model}`\n"
            f"Elapsed: {elapsed:.1f} sec\n"
            f"{generated_context_note(persisted)}\n\n"
            f"{result}"
        )
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)
        return OperationOutcome(
            "image",
            "succeeded" if persisted else "partial",
            "ok" if persisted else "context_persistence_failed",
            persisted=persisted,
            response_message_id=wait_message.message_id,
        )

    @dp.message(Command("image", "ocr"))
    async def image_command(message: Message, bot: Bot) -> None:
        await execute_slash_operation(message, "image", run_image(message, bot))

    async def run_meme(message: Message, bot: Bot) -> None:
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

    @dp.message(Command("meme"))
    async def meme_command(message: Message, bot: Bot) -> None:
        await execute_slash_operation(message, "meme", run_meme(message, bot))

    @opik_track(name="video.process")
    async def recognize_video_source(
        *,
        video: StoredVideo,
        request_message: Message,
        save_target_message: Message,
        bot: Bot,
        notify_disabled: bool = False,
        status_as_reply: bool = False,
        routed: bool = False,
    ) -> OperationOutcome:
        if not is_allowed(settings, request_message.chat.id):
            return OperationOutcome("video", "rejected", "access_denied")
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
            response_message_id = None
            if notify_disabled:
                response = await answer_logged(
                    request_message,
                    "Video recognition is disabled: VIDEO_RECOGNITION_MODEL is empty.",
                )
                response_message_id = response.message_id
            return OperationOutcome(
                "video",
                "rejected",
                "feature_disabled",
                response_message_id=response_message_id,
            )

        if video_too_large(settings, video):
            response = await answer_logged(
                request_message,
                "Видео слишком большое для распознавания: "
                f"{video.file_size} bytes. Лимит: {effective_video_size_limit_mb(settings)} MB.",
            )
            return OperationOutcome(
                "video",
                "rejected",
                "media_too_large",
                response_message_id=response.message_id,
            )
        if video_too_long(settings, video):
            response = await answer_logged(
                request_message,
                "Video is too long: "
                f"{video.duration} sec. Limit: {settings.max_video_seconds} sec.",
            )
            return OperationOutcome(
                "video",
                "rejected",
                "media_too_long",
                response_message_id=response.message_id,
            )

        cache_key = video_recognition_cache_key(
            settings,
            video_recognizer,
            transcriber,
            transcript_formatter,
        )
        cached = await store.get_video_recognition(
            chat_id=video.chat_id,
            message_id=video.message_id,
            cache_key=cache_key,
        )
        if cached and cached.result.strip():
            update_opik_span_metadata({"cache_hit": True})
            wait_message = (
                await reply_logged(request_message, "Recognizing video...") if routed else None
            )
            saved_text = (
                f"🎞 Video recognition for message #{video.message_id} "
                f"from {video.sender_name}: {cached.result}"
            )
            persisted = await try_save_generated_context(
                settings,
                store,
                wait_message if wait_message else save_target_message,
                saved_text,
                limit_chars=settings.max_transcription_chars,
                origin="generated",
                kind="video_recognition",
            )
            text = (
                f"**Video recognition for message #{video.message_id}**\n"
                f"Source: {video.sender_name}\n"
                f"Type: `{video.media_type}`\n"
                f"Model: `{settings.video_recognition_model}`\n"
                "Cache: `hit`\n"
                f"{generated_context_note(persisted)}\n\n"
                f"{cached.result}"
            )
            response_message_id = wait_message.message_id if wait_message else None
            for index, part in enumerate(split_telegram_text(text)):
                if index == 0 and wait_message:
                    await edit_text_logged(wait_message, part, source_message=request_message)
                elif index == 0 and status_as_reply:
                    response = await reply_logged(request_message, part)
                    response_message_id = response.message_id
                else:
                    response = await answer_logged(request_message, part)
                    if index == 0:
                        response_message_id = response.message_id
            return OperationOutcome(
                "video",
                "succeeded" if persisted else "partial",
                "ok" if persisted else "context_persistence_failed",
                persisted=persisted,
                cache_hit=True,
                response_message_id=response_message_id,
            )

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
                        audio_transcript = await transcribe_video_audio(
                            settings,
                            audio_path,
                            video.duration,
                            transcriber,
                        )
                        if audio_transcript.strip() and transcript_formatter:
                            try:
                                audio_transcript = await transcript_formatter.format(audio_transcript)
                            except Exception:  # noqa: BLE001
                                logging.exception("Video audio transcript formatting failed")
                            finally:
                                await transcript_formatter.unload()
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
            return OperationOutcome(
                "video",
                "failed",
                "recognition_error",
                cache_hit=False,
                response_message_id=wait_message.message_id,
            )
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
        cache_persisted = True
        try:
            await store.save_video_recognition(
                chat_id=video.chat_id,
                message_id=video.message_id,
                cache_key=cache_key,
                result=result,
            )
        except Exception:  # noqa: BLE001
            cache_persisted = False
            logging.exception(
                "Video recognition cache persistence failed chat_id=%s message_id=%s",
                video.chat_id,
                video.message_id,
            )
        persisted = await try_save_generated_context(
            settings,
            store,
            wait_message if routed else save_target_message,
            saved_text,
            limit_chars=settings.max_transcription_chars,
            origin="generated",
            kind="video_recognition",
        )
        text = (
            f"**Video recognition for message #{video.message_id}**\n"
            f"Source: {video.sender_name}\n"
            f"Type: `{video.media_type}`\n"
            f"Model: `{settings.video_recognition_model}`\n"
            f"Elapsed: {elapsed:.1f} sec\n"
            f"{generated_context_note(persisted)}\n\n"
            f"{result}"
        )
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=request_message)
        for part in parts[1:]:
            await answer_logged(request_message, part)
        failures = []
        if not cache_persisted:
            failures.append("cache")
        if not persisted:
            failures.append("context")
        return OperationOutcome(
            "video",
            "succeeded" if not failures else "partial",
            "ok" if not failures else "_and_".join(failures) + "_persistence_failed",
            persisted=persisted,
            cache_hit=False,
            response_message_id=wait_message.message_id,
        )

    async def run_youtube_video(
        message: Message,
        bot: Bot,
        url: str,
        *,
        routed: bool = False,
    ) -> OperationOutcome:
        if not settings.video_recognition_model:
            response = await answer_logged(
                message,
                "Video recognition is disabled: VIDEO_RECOGNITION_MODEL is empty.",
            )
            return OperationOutcome(
                "video",
                "rejected",
                "feature_disabled",
                response_message_id=response.message_id,
            )
        wait_message = (
            await reply_logged(message, "Downloading and recognizing YouTube video...")
            if routed
            else await answer_logged(message, "Downloading and recognizing YouTube video...")
        )
        downloaded = None
        audio_path: Path | None = None
        try:
            downloaded = await download_youtube_video(
                url=url,
                directory=settings.video_download_dir,
                max_size_mb=settings.max_video_size_mb,
                max_seconds=settings.max_video_seconds,
            )
            source = StoredVideo(
                message_id=message.message_id,
                chat_id=message.chat.id,
                chat_type=str(message.chat.type),
                file_id="youtube",
                media_type="youtube",
                sender_name="YouTube",
                created_at=message.date.isoformat(),
                duration=downloaded.duration,
                file_size=downloaded.path.stat().st_size,
                file_name=downloaded.path.name,
                mime_type="video/mp4",
            )
            if settings.video_transcribe_audio:
                audio_path = await extract_video_audio(settings, downloaded.path, source)

            visual_result = ""
            visual_note = "Визуальный анализ кадров не дал результата."
            audio_transcript = ""
            audio_note = "Аудиодорожка не найдена или речь не распознана."
            async with gpu_lock:
                try:
                    visual_result = await video_recognizer.recognize(
                        downloaded.path,
                        message_id=message.message_id,
                        duration=downloaded.duration,
                    )
                except Exception:  # noqa: BLE001
                    logging.exception("YouTube visual recognition failed")
                    visual_note = "Визуальный анализ кадров не удался."
                finally:
                    await video_recognizer.unload()

                if settings.video_transcribe_audio:
                    if audio_path and transcriber:
                        try:
                            audio_transcript = await transcribe_video_audio(
                                settings,
                                audio_path,
                                downloaded.duration,
                                transcriber,
                            )
                            if audio_transcript.strip() and transcript_formatter:
                                try:
                                    audio_transcript = await transcript_formatter.format(audio_transcript)
                                except Exception:  # noqa: BLE001
                                    logging.exception("YouTube transcript formatting failed")
                                finally:
                                    await transcript_formatter.unload()
                        except Exception:  # noqa: BLE001
                            logging.exception("YouTube audio transcription failed")
                            audio_note = "Расшифровка аудио не удалась."
                    elif audio_path:
                        audio_note = "Аудио найдено, но Whisper transcription is not configured."
                else:
                    audio_note = "Расшифровка аудио для видео отключена."
            if not visual_result.strip() and not audio_transcript.strip():
                raise RuntimeError(f"{visual_note}; {audio_note}")
            result = combine_video_result(
                visual_result,
                visual_note,
                audio_transcript,
                audio_note,
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("YouTube video recognition failed")
            detail = str(exc) if isinstance(exc, YouTubeDownloadError) else type(exc).__name__
            await edit_text_logged(
                wait_message,
                f"Failed to recognize YouTube video: `{detail}`",
                source_message=message,
            )
            return OperationOutcome(
                "video",
                "failed",
                "youtube_processing_error",
                response_message_id=wait_message.message_id,
            )
        finally:
            if audio_path:
                audio_path.unlink(missing_ok=True)
            if downloaded:
                downloaded.path.unlink(missing_ok=True)

        saved_text = f"🎞 YouTube recognition for {downloaded.title}: {result}"
        persisted = await try_save_generated_context(
            settings,
            store,
            wait_message,
            saved_text,
            limit_chars=settings.max_transcription_chars,
            origin="generated",
            kind="youtube_recognition",
        )
        text = (
            f"**YouTube video recognition: {downloaded.title}**\n"
            f"{generated_context_note(persisted)}\n\n{result}"
        )
        parts = split_telegram_text(text)
        await edit_text_logged(wait_message, parts[0], source_message=message)
        for part in parts[1:]:
            await answer_logged(message, part)
        return OperationOutcome(
            "video",
            "succeeded" if persisted else "partial",
            "ok" if persisted else "context_persistence_failed",
            persisted=persisted,
            cache_hit=False,
            response_message_id=wait_message.message_id,
        )

    async def run_video(
        message: Message,
        bot: Bot,
        *,
        routed: bool = False,
        youtube: str | None = None,
    ) -> OperationOutcome:
        if not is_allowed(settings, message.chat.id):
            return OperationOutcome("video", "rejected", "access_denied")

        if message.reply_to_message:
            video = await resolve_video_for_command(store, message)
        else:
            video = video_from_message(message)
            if not video and youtube:
                return await run_youtube_video(message, bot, youtube, routed=routed)
            if not video:
                video = await store.get_latest_video(message.chat.id)
        if not video:
            if youtube:
                return await run_youtube_video(message, bot, youtube, routed=routed)
            response = await answer_logged(
                message,
                "No video found. Reply to a video with `/video`, or send `/video` after a video.",
            )
            return OperationOutcome(
                "video",
                "rejected",
                "missing_media",
                response_message_id=response.message_id,
            )

        return await recognize_video_source(
            video=video,
            request_message=message,
            save_target_message=message,
            bot=bot,
            notify_disabled=True,
            routed=routed,
        )

    @dp.message(Command("video", "vocr"))
    async def video_command(message: Message, bot: Bot) -> None:
        await execute_slash_operation(
            message,
            "video",
            run_video(message, bot, youtube=youtube_url_from_message(message)),
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
            origin="incoming",
            kind="voice_transcript",
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
        persisted = await try_save_generated_context(
            settings,
            store,
            source_message,
            saved_text,
            limit_chars=settings.max_transcription_chars,
            replace_existing=replace_existing,
            origin="incoming",
            kind="voice_transcript",
        )
        elapsed = time.perf_counter() - started
        persistence_text = (
            f"Saved for summaries in {elapsed:.1f} sec."
            if persisted
            else generated_context_note(False)
        )
        text = (
            f"**Voice transcription from {voice_sender_name}**\n"
            f"{persistence_text}\n\n"
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

    async def run_transcribe(message: Message, bot: Bot) -> None:
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

    @dp.message(Command("transcribe"))
    async def transcribe_command(message: Message, bot: Bot) -> None:
        await execute_slash_operation(message, "transcribe", run_transcribe(message, bot))

    async def run_profile_show(
        message: Message,
        query: str | None,
        *,
        reply_to_source: bool,
    ) -> OperationOutcome:
        if not is_allowed(settings, message.chat.id):
            return OperationOutcome("profile_show", "rejected", "access_denied")
        if not chat_memory:
            response = await answer_logged(message, "Chat memory is disabled: `MEMORY_ENABLED=false`.")
            return OperationOutcome(
                "profile_show",
                "rejected",
                "memory_disabled",
                response_message_id=response.message_id,
            )

        self_profile = False
        if message.reply_to_message:
            key, _ = participant_ref(message.reply_to_message)
            text = await chat_memory.profile_text(chat_id=message.chat.id, participant_keys=[key])
        elif query and query.strip():
            matches = await resolve_profile_target(store, chat_id=message.chat.id, query=query)
            normalized = normalize_profile_target(query)
            exact = [
                match for match in matches
                if normalized in {
                    normalize_profile_target(match[0].removeprefix("name:")),
                    normalize_profile_target(match[1]),
                }
            ]
            if not matches or (len(matches) > 1 and len(exact) != 1):
                send = reply_logged if reply_to_source else answer_logged
                response = await send(message, "Уточните имя участника или ответьте на его сообщение.")
                return OperationOutcome(
                    "profile_show",
                    "rejected",
                    "participant_ambiguous" if matches else "participant_not_found",
                    response_message_id=response.message_id,
                )
            key, _ = (exact or matches)[0]
            text = await chat_memory.profile_text(chat_id=message.chat.id, participant_keys=[key])
        elif message.from_user:
            key, _ = participant_ref(message)
            text = await chat_memory.profile_text(chat_id=message.chat.id, participant_keys=[key])
            self_profile = True
        else:
            send = reply_logged if reply_to_source else answer_logged
            response = await send(message, "Не удалось определить участника. Ответьте на его сообщение.")
            return OperationOutcome(
                "profile_show",
                "rejected",
                "participant_unknown",
                response_message_id=response.message_id,
            )
        if self_profile and text == "Паспорт участника пока пуст.":
            text += (
                "\n\n`/profile` без reply показывает ваш профиль. "
                "Чтобы посмотреть другого участника, ответьте на его сообщение `/profile` "
                "или используйте `/profile <name>`."
            )
        send = reply_logged if reply_to_source else answer_logged
        response = await send(message, text)
        return OperationOutcome(
            "profile_show",
            "succeeded",
            "ok",
            response_message_id=response.message_id,
        )

    @dp.message(F.voice | F.audio)
    async def transcribe_audio_message(message: Message, bot: Bot) -> None:
        await transcribe_audio_source(message, bot)

    @dp.channel_post(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def save_channel_image(message: Message) -> None:
        await save_incoming_image(settings, store, message)

    @dp.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
    async def save_regular_image(message: Message, bot: Bot) -> None:
        await save_incoming_image(settings, store, message)
        if not has_bot_command_entity(message):
            await save_incoming_message(settings, store, message)
        await handle_addressed_message(message, bot)

    @dp.channel_post(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
    async def save_channel_video(message: Message) -> None:
        await save_incoming_video(settings, store, message)

    @dp.message(F.video | F.video_note | (F.document & F.document.mime_type.startswith("video/")))
    async def save_regular_video(message: Message, bot: Bot) -> None:
        await save_incoming_video(settings, store, message)
        if not has_bot_command_entity(message):
            await save_incoming_message(settings, store, message)
        if await handle_addressed_message(message, bot):
            return
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
        if has_bot_command_entity(message) or (message.text and message.text.startswith("/")):
            return
        await save_incoming_message(settings, store, message)
        await handle_addressed_message(message, bot)

    return dp


async def resolve_image_for_command(store: MessageStore, message: Message) -> StoredImage | None:
    if message.reply_to_message:
        replied_image = image_from_message(message.reply_to_message)
        if replied_image:
            return replied_image
        indexed_reply = await store.get_image_by_message_id(
            message.chat.id,
            message.reply_to_message.message_id,
        )
        if indexed_reply:
            return indexed_reply
        return image_from_message(message)
    current_image = image_from_message(message)
    if current_image:
        return current_image
    return await store.get_latest_image(message.chat.id)


async def resolve_video_for_command(store: MessageStore, message: Message) -> StoredVideo | None:
    if message.reply_to_message:
        replied_video = video_from_message(message.reply_to_message)
        if replied_video:
            return replied_video
        indexed_reply = await store.get_video_by_message_id(
            message.chat.id,
            message.reply_to_message.message_id,
        )
        if indexed_reply:
            return indexed_reply
        return video_from_message(message)
    current_video = video_from_message(message)
    if current_video:
        return current_video
    return await store.get_latest_video(message.chat.id)


async def save_incoming_message(settings: Settings, store: MessageStore, message: Message) -> None:
    if not is_allowed(settings, message.chat.id):
        return

    text = message_text(message)
    if not text:
        return
    await save_message_text(
        settings,
        store,
        message,
        text,
        origin="incoming",
        kind="caption" if message.caption else "text",
    )


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
    origin: str = "legacy",
    kind: str = "legacy_unclassified",
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
        origin=origin,
        kind=kind,
        replace=replace_existing,
    )


async def try_save_generated_context(
    settings: Settings,
    store: MessageStore,
    message: Message,
    text: str,
    *,
    limit_chars: int | None = None,
    replace_existing: bool = False,
    origin: str = "generated",
    kind: str = "generated_context",
) -> bool:
    try:
        await save_message_text(
            settings,
            store,
            message,
            text,
            limit_chars=limit_chars,
            replace_existing=replace_existing,
            origin=origin,
            kind=kind,
        )
    except Exception:  # noqa: BLE001
        logging.exception(
            "Generated context persistence failed chat_id=%s message_id=%s",
            message.chat.id,
            message.message_id,
        )
        return False
    return True


async def try_save_final_assistant_answer(
    settings: Settings,
    store: MessageStore,
    response_message: Message,
    text: str,
) -> bool:
    try:
        await save_message_text(
            settings,
            store,
            response_message,
            text,
            origin="assistant",
            kind="assistant_answer",
        )
    except Exception:  # noqa: BLE001
        logging.exception(
            "Assistant answer persistence failed chat_id=%s message_id=%s",
            response_message.chat.id,
            response_message.message_id,
        )
        return False
    return True


def generated_context_note(persisted: bool) -> str:
    if persisted:
        return "Saved for summaries."
    return "Результат получен, но сохранить его для саммари не удалось."


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


def audio_chunk_ranges(duration: int | None, max_seconds: int) -> list[tuple[int, int]]:
    max_seconds = max(max_seconds, 1)
    if not duration:
        return [(0, max_seconds)]
    if duration <= max_seconds:
        return [(0, duration)]
    return [
        (start, min(max_seconds, duration - start))
        for start in range(0, duration, max_seconds)
    ]


async def transcribe_video_audio(
    settings: Settings,
    audio_path: Path,
    duration: int | None,
    transcriber: FasterWhisperTranscriber,
) -> str:
    ranges = audio_chunk_ranges(duration, settings.max_voice_seconds)
    if len(ranges) == 1:
        return await transcriber.transcribe(audio_path)

    chunks: list[Path] = []
    transcripts: list[str] = []
    try:
        for index, (start, length) in enumerate(ranges, start=1):
            chunk_path = audio_path.with_name(f"{audio_path.stem}_part_{index:03d}.wav")
            chunks.append(chunk_path)
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(start),
                "-t",
                str(length),
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(chunk_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"ffmpeg audio split failed: {detail[:1000]}")
            transcript = (await transcriber.transcribe(chunk_path)).strip()
            if transcript:
                transcripts.append(transcript)
    finally:
        for chunk in chunks:
            chunk.unlink(missing_ok=True)
    return "\n\n".join(transcripts)


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
    startup_cutoff = datetime.now(timezone.utc)
    bot = Bot(token=settings.telegram_bot_token)
    recovery = await store.refund_pending_casino_spins(created_before=startup_cutoff)
    automatic_recovery = await store.recover_pending_automatic_casino_spins(created_before=startup_cutoff)
    await cached_bot_identity(bot)
    await refresh_recovered_casino_leaderboards(
        settings=settings, bot=bot, store=store,
        chat_ids=set(recovery.chat_ids) | set(automatic_recovery.chat_ids),
    )
    llm = build_llm_client(settings)
    question_llm = build_llm_client(settings, model=settings.question_model or None)
    router_llm = build_llm_client(
        settings,
        model=settings.intent_router_model or settings.question_model or None,
        num_ctx=2048,
        num_predict=96,
    )
    summarizer = Summarizer(llm, settings.chunk_chars)
    chat_assistant = ChatAssistant(question_llm, settings.chunk_chars)
    joke_selector = JokeSelector(
        build_llm_client(
            settings,
            model=settings.question_model or None,
            num_ctx=4096,
            num_predict=160,
        )
    )
    intent_router = IntentRouter(router_llm)
    chat_memory = ChatMemory(store, llm, settings) if settings.memory_enabled else None
    transcript_formatter_llm = (
        build_llm_client(
            settings,
            provider=settings.transcription_format_provider,
            model=settings.transcription_format_model,
            num_ctx=settings.transcription_format_num_ctx,
            num_predict=settings.transcription_format_num_predict,
        )
        if (settings.transcribe_voice or settings.video_transcribe_audio)
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
        intent_router,
        joke_selector,
    )

    logging.info("Bot started with LLM provider: %s", settings.resolved_llm_provider)
    worker_task: asyncio.Task[None] | None = None
    if settings.autonomous_jokes_enabled:
        autonomous_selector = JokeSelector(
            build_llm_client(settings, model=settings.autonomous_joke_judge_model or None,
                             num_ctx=4096, num_predict=160)
        )
        worker = AutonomousJokeWorker(
            settings=settings, store=store, selector=autonomous_selector, bot=bot, gpu_lock=gpu_lock
        )
        stop_event = asyncio.Event()
        worker_task = asyncio.create_task(worker.run(stop_event))
        worker_task.add_done_callback(log_worker_failure)
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "channel_post"],
            handle_as_tasks=False,
        )
    finally:
        if worker_task:
            stop_event.set()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
