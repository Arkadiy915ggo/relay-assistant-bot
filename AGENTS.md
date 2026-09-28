# Agent Guide

## Purpose

This repository contains `telegram-summary-bot`, an async Python Telegram bot that stores new chat messages in SQLite and produces Russian AI summaries, contextual answers, image OCR, video OCR, and voice/video transcription.

The bot uses the official Telegram Bot API through `aiogram`. It cannot read historical Telegram messages from before it was added to a chat.

## Safety Rules For Agents

- Do not read `.env`; use `.env.example` for configuration shape.
- Do not read or dump `data/` by default; it may contain private Telegram logs, media cache, SQLite databases, and model responses.
- Keep changes small and local. This project is intentionally simple and mostly single-module-per-feature.
- Prefer async-compatible code. Do not add blocking network, file, or model work to handlers unless it is already wrapped safely.
- Preserve Russian bot prompts and response behavior unless the task explicitly asks to change language or tone.
- When touching local model flows, remember that summaries, image recognition, video recognition, and transcription share one `gpu_lock`.

## Stack

- Python `>=3.11` with package source under `src/tg_summary_bot`.
- Telegram framework: `aiogram` 3.x.
- Storage: `aiosqlite` with SQLite WAL mode.
- LLM providers: OpenAI Chat Completions or local Ollama.
- Optional speech transcription: `faster-whisper`.
- Video frame/audio extraction: external `ffmpeg` command.

## Main Commands

- Install editable package: `./run.sh install`.
- Install voice support on CPU: `./run.sh install-voice`.
- Install voice support with CUDA libs: `./run.sh install-voice-cuda`.
- Start bot: `./run.sh start` or `python -m tg_summary_bot`.
- Lightweight syntax check: `.venv/bin/python -m compileall src`.
- Test suite: `.venv/bin/python -m unittest discover -s tests -v`.

The repository uses the standard-library `unittest` framework with fakes and temporary SQLite databases.

Current focused tests:

- `tests/test_addressing.py`: aliases, mentions, token boundaries, and one-total-typo behavior.
- `tests/test_storage_aliases.py`: chat-scoped alias persistence.
- `tests/test_intent_router.py`: strict routing, explicit meme/profile guards, GPU sequencing, surprise meme policy, and command admission.
- `tests/test_operation_stabilization.py`: generated-context persistence and content-free terminal outcomes.
- `tests/test_assistant_identity.py`: chat alias identity context.
- `tests/test_youtube.py`: URL/download validation, limits, cleanup, and timeout cancellation.
- `tests/test_video_audio_chunks.py`: bounded video-audio transcription chunks.

## Runtime Flow

1. `python -m tg_summary_bot` runs `src/tg_summary_bot/__main__.py`.
2. `bot.main()` loads `.env` via `config.load_settings()`.
3. Logging is configured to console, `LOG_FILE`, and JSONL `RESPONSE_LOG_FILE`.
4. `MessageStore.init()` creates SQLite tables and indexes in `DATABASE_PATH`.
5. LLM clients, recognizers, optional Whisper transcriber, and one `asyncio.Lock` for GPU-heavy work are created.
6. `create_dispatcher()` registers command handlers and passive message/media indexing handlers.
7. `dp.start_polling(..., allowed_updates=["message", "channel_post"], handle_as_tasks=False)` starts long polling.

## Module Map

- `src/tg_summary_bot/bot.py`: Telegram handlers, access checks, response logging, media resolution/download, command orchestration, GPU lock usage.
- `src/tg_summary_bot/config.py`: `.env` parsing into frozen `Settings`; provider selection with `resolved_llm_provider`.
- `src/tg_summary_bot/storage.py`: SQLite schema and async persistence for messages, indexed images/videos, and cached video recognitions.
- `src/tg_summary_bot/llm.py`: `LLMClient` abstraction plus OpenAI and Ollama clients.
- `src/tg_summary_bot/summarizer.py`: Russian summary prompts, chunking, partial summary merge.
- `src/tg_summary_bot/assistant.py`: `/question` flow with relevant-context extraction for long histories.
- `src/tg_summary_bot/image_recognizer.py`: manual `/image` and `/ocr` recognition through Ollama vision.
- `src/tg_summary_bot/video_recognizer.py`: manual `/video` and `/vocr` key-frame recognition through Ollama vision with adaptive compression.
- `src/tg_summary_bot/youtube.py`: validated single-video YouTube downloads through `yt-dlp` for the existing video recognition flow.
- `src/tg_summary_bot/transcriber.py`: optional local Whisper transcription for voice/audio and video audio tracks.
- `src/tg_summary_bot/transcript_formatter.py`: optional LLM post-processor that formats already-sent Whisper transcripts with minimal edits.
- `src/tg_summary_bot/memory.py`: structured long-term chat memory, hybrid memory search, rollups, and participant profile facts used by `/summary`, `/question`, and mention answers.
- `src/tg_summary_bot/periods.py`: period parsing for `30m`, `6h`, `24h`, `7d`, `2w`, `today`, and `сегодня`.

## Bot Commands

- `/start`, `/help`: describe bot commands and current chat id.
- `/stats`: show chat id, stored counts, provider/model settings, and media/transcription settings.
- `/balance`: show the command author's joke-point balance in the current chat.
- `/top`: show up to ten positive joke-point balances in deterministic order.
- `/casino`: spin the Telegram `🎰` virtual slot for a fixed 10-point stake; no money, transfers, purchases, or withdrawals.
- `/summary [period]`: summarize stored messages for the period; default comes from `DEFAULT_SUMMARY_PERIOD`.
- `/question [period] <text>`: answer using stored chat context when relevant.
- `/memory`: show compressed chat memory status; `/memory rebuild` resets memory blocks/state so they can be rebuilt from stored raw messages.
- `/profile`: show, forget, or correct source-backed participant profile facts.
- `/transcribe`: transcribe a replied voice/audio message manually.
- `/image`, `/ocr`: recognize replied image or latest indexed image; save result as a stored message.
- `/video`, `/vocr`: recognize replied/latest indexed Telegram video or one supplied YouTube URL; cache Telegram results and save recognition text as a stored message.
- `/compare [period]`: run summaries through `COMPARE_MODELS`; only supported with `LLM_PROVIDER=ollama`.

## Data Model

SQLite tables are initialized in `MessageStore.init()`:

- `messages`: primary key `(chat_id, message_id)`, stores normalized text/captions/transcripts/OCR results with UTC `created_at` and optional `reply_to_message_id`.
- `messages.origin`/`messages.kind`: explicit provenance. Only `incoming/text`, `incoming/caption`, `incoming/voice_transcript`, and `assistant/assistant_answer` are eligible Joke Points sources; migrated legacy rows remain ineligible.
- `images`: primary key `(chat_id, message_id)`, stores Telegram `file_id` and metadata for later manual recognition.
- `videos`: primary key `(chat_id, message_id)`, stores Telegram `file_id`, duration, size, and metadata for later manual recognition.
- `video_recognitions`: primary key `(chat_id, message_id, cache_key)`, caches expensive video recognition results by prompt/model/frame/audio settings.
- `chat_memory_blocks`: optional long-term structured memory blocks with period bounds, summary, topics, keywords, message count, structured JSON, and hierarchy level.
- `chat_memory_state`: tracks the latest processed message timestamp per chat for memory compression.
- `chat_participant_facts`: source-backed participant profile facts with type, confidence, status, source message ids, and optional expiration.
- `chat_point_ledger`: append-only `best_joke` award entries, unique per chat/source message.
- `chat_point_balances`: chat-scoped balance projection used by `/balance`, `/top`, and `/stats` leader.
- `chat_pinned_leaderboards`: the chat-scoped Telegram `message_id` used for best-effort leaderboard edits and pinning.
- `chat_casino_spins`: idempotent virtual slot reserve/settle/refund records linked to ledger rows by `casino_spin_id`.
- `chat_joke_inbox`, `chat_joke_jobs`, `chat_joke_job_items`, `chat_joke_outbox`: durable autonomous joke queue, immutable blocks, fenced leases, and post-award delivery.

## Important Behavior

- Access control is enforced by `ALLOWED_CHAT_IDS`; an empty set allows all chats.
- `JOKE_AWARDS_DISABLED_CHAT_IDS` disables manual/autonomous joke awards for listed chats; `CASINO_DISABLED_CHAT_IDS` independently disables all casino flows there.
- `/stats` intentionally bypasses `ALLOWED_CHAT_IDS` for chat-id discovery and reports `access_allowed`; this exception does not apply to storage, routing, or other commands.
- Any participant in an allowed chat may manage chat aliases. An optional admin-only toggle is a future possibility and is not implemented.
- Multi-token aliases allow one typo in total when their normalized length is at least five characters; short aliases require exact matching.
- Text and captions are stored passively unless the message is a slash command.
- Images and videos are indexed passively by Telegram `file_id`; Telegram video notes are recognized automatically, while normal video files require explicit commands.
- Voice/audio messages are transcribed only when `TRANSCRIBE_VOICE=true` and voice dependencies are installed. Optional transcript formatting runs after the raw Whisper response is already sent.
- Video recognition can include audio transcription when `VIDEO_TRANSCRIBE_AUDIO=true` and a transcriber is configured.
- Long summaries/questions are chunked before model calls; final summaries merge partials.
- `/summary` takes an eligible raw snapshot, then runs a strict joke selector in a second acquisition of the existing shared `gpu_lock`; awards are committed atomically only after that lock is released. `/compare` never selects or awards jokes.
- With `AUTONOMOUS_JOKES_ENABLED=true`, a background worker creates 50-message or aged 5-49-message blocks after 3 days; `/summary` becomes read-only for awards. Shadow mode persists results without points or Telegram notifications.
- The autonomous worker shares `gpu_lock`, acquires it for one selector batch at a time, and uses SQLite leases for restart recovery. It cannot recover Telegram history unavailable to the Bot API.
- Bot balances use stable `id:<bot_id>` keys; aliases are display-only. `/casino bot` and explicit self-addressed casino requests debit that bot balance.
- `/top` and the pinned `Топ балансов` reflect current spendable balances after awards, bets, payouts, and refunds. Only a confirmed Telegram not-found error recreates the pinned message.
- Mentioning the bot in a message triggers the same contextual answer flow as `/question` using the default period.
- Wiki/image/video/YouTube context persistence is best effort: a completed result is still sent with a safe partial-success warning if SQLite persistence fails.
- A surprise meme has a fixed 2% chance only for a valid routed joke question with an image in the current or replied message.
- Telegram responses are split below the Telegram message limit by `split_telegram_text()`.
- Casino uses `telegram_slots_base4_v1`, a fixed stake of 10, gross payouts 0/5/50/250, and RTP 90.625%. An unknown or uncommitted Telegram Dice outcome is void and refunded; there are no real-money, payment, transfer, purchase, or withdrawal features.

## Configuration

Use `.env.example` as the source of truth for documented environment variables. Also check `config.py` when changing settings, because it may contain newer in-progress knobs not yet documented in `.env.example`. The most important groups are:

- Telegram/access: `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`.
- LLM provider: `LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`.
- Ollama generation: `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_KEEP_ALIVE`, `OLLAMA_UNLOAD_AFTER_TASK`, `OLLAMA_NUM_CTX`, `OLLAMA_NUM_PREDICT`.
- Storage/logs: `DATABASE_PATH`, `LOG_FILE`, `RESPONSE_LOG_FILE`.
- Voice: `TRANSCRIBE_VOICE`, `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, `WHISPER_LANGUAGE`, `MAX_VOICE_SECONDS`, `TRANSCRIPTION_FORMAT_*`.
- Image/video: `IMAGE_RECOGNITION_MODEL`, `VIDEO_RECOGNITION_MODEL`, frame limits, size/duration limits, and download directories.
- Summary limits: `MAX_MESSAGE_CHARS`, `MAX_SUMMARY_INPUT_CHARS`, `CHUNK_CHARS`, `DEFAULT_SUMMARY_PERIOD`.
- Memory settings in code: `MEMORY_ENABLED`, `MEMORY_RECENT_PERIOD`, `MEMORY_CHUNK_CHARS`, `MEMORY_MAX_BLOCKS`, `MEMORY_SEARCH_LIMIT`.

## Common Debug Entry Points

- Bad startup config: inspect `config.py`, `.env.example`, and the exact missing/invalid env var from the exception.
- Telegram handler behavior: inspect `create_dispatcher()` in `bot.py`.
- Missing messages in summaries: check `is_allowed()`, passive save handlers, `save_message_text()`, and `MessageStore.get_messages_since()`.
- OCR/video result not reused: check `video_recognition_cache_key()` and `video_recognitions` cache logic.
- Ollama failures/timeouts: inspect `llm.py`, recognizer classes, context/predict env vars, and `OLLAMA_TIMEOUT_SECONDS`.
- GPU/VRAM contention: inspect `gpu_lock` usage and model unload calls.
