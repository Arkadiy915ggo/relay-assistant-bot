# Agent Guide

## Purpose

This repository contains `telegram-summary-bot`, an async Python Telegram bot that stores new chat messages in SQLite and produces Russian AI summaries, contextual answers, image OCR, video OCR, and voice/video transcription.

The bot uses the official Telegram Bot API through `aiogram`. It cannot read historical Telegram messages from before it was added to a chat.

## Safety Rules For Agents

- Do not read `.env`; use `.env.example` for configuration shape.
- Do not read or dump `data/` by default; it may contain private Telegram logs, media cache, SQLite databases, and model responses.
- `experiments/` and local summary-experiment plans are Git-ignored working material. Keep them local; bot runtime/setup must use the packaged voice presets, never experiment scripts or outputs.
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
- `tests/test_autonomous_worker.py`: current access policy for queued jobs/outbox, activation/backfill retries, and worker failure reporting.
- `tests/test_casino.py`: slot mapping, atomic balances, bounded settlement retries, uncertain commit replay, cancellation, and startup recovery.
- `tests/test_pinned_leaderboard.py`: leaderboard reuse and allowlisted startup refresh.
- `tests/test_profile_stabilization.py`, `tests/test_observability_operations.py`: bounded profile batches/keyset migration, timeout/checkpoint safety, no-key exporter isolation and content-free handled-outcome tracing.
- `tests/test_whisper_local.py`: local snapshot/tokenizer/preprocessing completeness and explicit download opt-in, without models or network. An optional installed-faster-whisper contract check verifies actual 80/128 mel preprocessing with fake weight/tokenizer construction.
- `dev/opik/tests`: bounded exporter lifecycle and pinned SDK serialization through fake HTTP; run separately with `PYTHONPATH=dev/opik/src .venv/bin/python -m unittest discover -s dev/opik/tests -v` after developer setup.
- `tests/test_voice_presets.py`, `tests/test_voice_handlers.py`, `tests/test_speech_synthesizer.py`, `tests/test_cosyvoice_runner.py`: registry/storage, command delivery/provenance, own-bot isolation, runner protocol and supervised process cleanup without GPU/network/models.

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
- `src/tg_summary_bot/observability.py`: local operation logs and generic tracing hooks; no vendor SDK imports. `dev/opik` is a separate opt-in development distribution, not a bot dependency.
- `src/tg_summary_bot/summarizer.py`: Russian summary prompts, chunking, partial summary merge.
- `src/tg_summary_bot/assistant.py`: `/question` flow with relevant-context extraction for long histories.
- `src/tg_summary_bot/image_recognizer.py`: manual `/image` and `/ocr` recognition through Ollama vision.
- `src/tg_summary_bot/video_recognizer.py`: manual `/video` and `/vocr` key-frame recognition through Ollama vision with adaptive compression.
- `src/tg_summary_bot/youtube.py`: validated single-video YouTube downloads through `yt-dlp` for the existing video recognition flow.
- `src/tg_summary_bot/transcriber.py`: optional local Whisper transcription for voice/audio and video audio tracks.
- `src/tg_summary_bot/voice_presets.py`: immutable bundled/local voice registry with confined references.
- `src/tg_summary_bot/speech_synthesizer.py`: supervised optional TTS/ffmpeg lifecycle, shared GPU lock and upload-lifetime temporary files.
- `src/tg_summary_bot/cosyvoice_runner.py`: dedicated Python 3.10 zero-shot runner; never imported into the bot environment.
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
- `/ask [period] <text>`: alias of `/question`; `/ask voice [period] <text>` sends one answer as text then voice replies.
- `/voice <text>`: optional CosyVoice 3 speech reply; `/voice_type [id|reset]` manages chat-scoped voice selection.
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
- `chat_voice_settings`: chat-scoped voice preset ID and update timestamp; created idempotently at startup.

## Important Behavior

- Access control is enforced by `ALLOWED_CHAT_IDS`; an empty set allows all chats.
- `JOKE_AWARDS_DISABLED_CHAT_IDS` disables manual/autonomous joke awards for listed chats; `CASINO_DISABLED_CHAT_IDS` independently disables all casino flows there.
- `/stats` intentionally bypasses `ALLOWED_CHAT_IDS` for chat-id discovery and reports `access_allowed`; this exception does not apply to storage, routing, or other commands.
- Any participant in an allowed chat may manage chat aliases. An optional admin-only toggle is a future possibility and is not implemented.
- Multi-token aliases allow one typo in total when their normalized length is at least five characters; short aliases require exact matching.
- Text and captions are stored passively unless the message is a slash command.
- Images and videos are indexed passively by Telegram `file_id`; Telegram video notes are recognized automatically, while normal video files require explicit commands.
- Voice/audio messages are transcribed only when `TRANSCRIBE_VOICE=true` and voice dependencies are installed. Optional transcript formatting runs after the raw Whisper response is already sent.
- TTS defaults disabled and uses separate `.venv-cosyvoice3`; `./run.sh install-tts-cosyvoice3` explicitly installs/downloads its pinned baseline. Broken optional setup leaves text commands working. Own-bot audio is rejected before transcription work, including slash/routed replies.
- `chat_voice_settings` stores per-chat preset IDs; direct voice uses ineligible `generated/tts_text`, and `/ask voice` persists only one eligible assistant answer. Bundled Anub’арак uses model speed 1.0 then atempo=0.85/pitch −1 with formants preserved. Real GPU/listening/Telegram acceptance remains manual.
- Bundled `cartman` uses the user-selected MDX Extra reference and raw transcript from the Cartman experiment. Speed 1.0/pitch 0 bypass all tempo/pitch filters; only Telegram-compatible encoding remains.
- Profile refresh uses one rendered-size/message-count bounded batch, a separate profile client/token budget, timeout and per-chat cooldown; busy GPU work is deferred. Invalid JSON/timeouts preserve the cursor; `chat_profile_state.processed_message_id` handles same-timestamp boundaries.
- Optional tracing requires a separately installed dev exporter, `TRACING_ENABLED=true` and an explicit nonblank `TRACING_API_KEY` (legacy `OPIK_*` fallbacks supported). No key means no SDK import or network; SDK home config is ignored. Content capture defaults false. The Opik adapter disables SDK Sentry before import and stops exports on first error, without retrying bot work. `/stats` reports actual state; doctor diagnoses without importing the SDK. Slash traces retain speech stages and handled partial/failure errors; background profile traces are detached. `dev/opik/voice_trace_smoke.py --send` is an explicit synthetic live-ingestion check, not part of offline tests. Opik MCP is personal developer configuration, not project startup.
- Whisper defaults to `WHISPER_LOCAL_FILES_ONLY=true`: prepare models explicitly with `./run.sh download-whisper <model>`. Missing/incomplete local tokenizer/model/preprocessing files or invalid preprocessing `feature_size` fail before model construction, without online or 80-mel fallback. Set false only to explicitly allow runtime Hugging Face downloads/checks.
- Harmless leaderboard not-modified edits are quiet; confirmed pin-permission failures are best effort with a ten-minute cooldown, while balance edits continue and transient failures remain retryable.
- Video recognition can include audio transcription when `VIDEO_TRANSCRIBE_AUDIO=true` and a transcriber is configured.
- Long summaries/questions are chunked before model calls; final summaries merge partials.
- `/summary` takes an eligible raw snapshot, then runs a strict joke selector in a second acquisition of the existing shared `gpu_lock`; awards are committed atomically only after that lock is released. `/compare` never selects or awards jokes.
- With `AUTONOMOUS_JOKES_ENABLED=true`, a background worker creates 50-message or aged 5-49-message blocks after 3 days; `/summary` becomes read-only for awards. Shadow mode persists results without points or Telegram notifications.
- The autonomous worker shares `gpu_lock` and uses bounded warm judge sessions (8 calls / soft 120s), yielding between sessions without discarding tournament progress. Scheduled runs default to 6h / 4 jobs; SQLite admission survives restart, and outbox polling remains independent. Short 50-message blocks fit one selection within the existing character budget, then qualification. Awards commit outside the GPU lock. It cannot recover Telegram history unavailable to the Bot API.
- A separate durable `validation_failures` counter quarantines a joke job after 3 malformed results; `quarantined_at` excludes automatic claims while preserving the legacy retry status and source block. Transient errors do not consume that limit; new counters start at zero on upgrade.
- Jobs and outbox claims apply the current `ALLOWED_CHAT_IDS` and joke-disabled chat policy; excluded work stays paused without losing its durable state. Activation and backfill failures are retried in the worker loop.
- Joke finalists require a separate source-grounded qualification with an exact visible quote, a supported humour mechanism and no missing external context. Rough/short jokes remain eligible; invalid qualification retries without points and valid rejection yields no joke.
- Award notices expire after `AUTONOMOUS_JOKE_ANNOUNCEMENT_MAX_AGE` (default 24h from award commitment), without changing the ledger or balances. Fresh notices get priority; up to ten expired notices are retired per pass, and notices state the original credit time. Current access/shadow/action-pause policies are preserved.
- `AUTONOMOUS_JOKE_ANNOUNCE_BACKLOG=false` prevents startup catch-up bursts: pre-worker-start award notices are skipped even within the age window. New awards after startup can be announced; the durable activation cutoff and earned points are unaffected.
- `AUTONOMOUS_JOKE_ANNOUNCEMENT_MIN_INTERVAL=12h` caps automatic notices per chat (one per half day). SQLite announcement state atomically reserves attempts and tracks confirmed sends; cooldown survives restart/upgrade and uncertain sends. Rate-limited notices defer without changing points or blocking other chats.
- Bot balances use stable `id:<bot_id>` keys; aliases are display-only. `/casino bot` and explicit self-addressed casino requests debit that bot balance.
- `/top` and the pinned `Топ балансов` reflect current spendable balances after awards, bets, payouts, and refunds. Only a confirmed Telegram not-found error recreates the pinned message.
- Mentioning the bot in a message triggers the same contextual answer flow as `/question` using the default period.
- Wiki/image/video/YouTube context persistence is best effort: a completed result is still sent with a safe partial-success warning if SQLite persistence fails.
- A surprise meme has a fixed 2% chance only for a valid routed joke question with an image in the current or replied message.
- Telegram responses are split below the Telegram message limit by `split_telegram_text()`.
- Casino uses `telegram_slots_base4_v1`, a fixed stake of 10, gross payouts 0/5/50/250, and RTP 90.625%. An unknown or uncommitted Telegram Dice outcome is void and refunded; there are no real-money, payment, transfer, purchase, or withdrawal features.
- Trusted casino settlement has up to three attempts (0.25/0.5-second backoff), never a second Dice. Unconfirmed manual settlement sends a safe warning and preserves pending state for startup refund; startup Telegram refresh respects the current allowlist.
- YouTube cancellation/timeout cleanup waits for final thread writes, including late success. Cookies/authentication are not configured by the current downloader; upstream sign-in failures remain a known limitation.

## Product Roadmap

`relay-assistant-bot-doc/00 Feature Roadmap.md` links implemented Features 01-05, pre-merge stabilization, Feature 06 runtime MVP (manual smoke pending), and draft Features 07-12: chat search, documents/files, social videos, reactions, proactive messages, and product onboarding/GUI/provider modularity. Draft commands, settings, metrics, and architecture are proposals, not current runtime capabilities. Implement them only when requested.

`relay-assistant-bot-doc/01 Product Vision.md` is the product source of truth: community-first entertainment plus useful assistance, evolving into configurable chat personas, skills, voices, initiative, and editable introductory prompts. Formal work use is one preset. Profile/prompt editing and meeting/debate assistance remain future designs; preserve current runtime contracts until explicitly implemented.

## Configuration

Use `.env.example` as the source of truth for documented environment variables. Also check `config.py` when changing settings, because it may contain newer in-progress knobs not yet documented in `.env.example`. The most important groups are:

- Telegram/access: `TELEGRAM_BOT_TOKEN`, `ALLOWED_CHAT_IDS`.
- LLM provider: `LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`.
- Ollama generation: `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_KEEP_ALIVE`, `OLLAMA_UNLOAD_AFTER_TASK`, `OLLAMA_NUM_CTX`, `OLLAMA_NUM_PREDICT`.
- Storage/logs: `DATABASE_PATH`, `LOG_FILE`, `RESPONSE_LOG_FILE`.
- Optional developer tracing: `TRACING_ENABLED`, `TRACING_BACKEND`, `TRACING_API_KEY`, `TRACING_WORKSPACE`, `TRACING_PROJECT_NAME`, `TRACING_CAPTURE_CONTENT`, `TRACING_ENDPOINT`; install with `./run.sh install-dev`.
- Voice: `TRANSCRIBE_VOICE`, `WHISPER_MODEL`, `WHISPER_LOCAL_FILES_ONLY`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, `WHISPER_LANGUAGE`, `MAX_VOICE_SECONDS`, `TRANSCRIPTION_FORMAT_*`.
- TTS: `TTS_ENABLED`, `TTS_PROVIDER`, `TTS_DEFAULT_VOICE`, `TTS_LOCAL_VOICES_DIR`, `TTS_OUTPUT_DIR`, `TTS_PYTHON_PATH`, `TTS_COSYVOICE_UPSTREAM_DIR`, `TTS_MODEL_DIR`, `TTS_MAX_TEXT_CHARS`, `TTS_MAX_AUDIO_SECONDS`, `TTS_TIMEOUT_SECONDS`, `TTS_ENCODING_TIMEOUT_SECONDS`, `TTS_UPLOAD_TIMEOUT_SECONDS`.
- Image/video: `IMAGE_RECOGNITION_MODEL`, `VIDEO_RECOGNITION_MODEL`, frame limits, size/duration limits, and download directories.
- Summary limits: `MAX_MESSAGE_CHARS`, `MAX_SUMMARY_INPUT_CHARS`, `CHUNK_CHARS`, `DEFAULT_SUMMARY_PERIOD`.
- Memory settings in code: `MEMORY_ENABLED`, `MEMORY_RECENT_PERIOD`, `MEMORY_CHUNK_CHARS`, `MEMORY_MAX_BLOCKS`, `MEMORY_SEARCH_LIMIT`.
- Profile refresh: `PROFILE_MODEL`, `PROFILE_BATCH_CHARS`, `PROFILE_BATCH_MESSAGES`, `PROFILE_NUM_PREDICT`, `PROFILE_TIMEOUT_SECONDS`, `PROFILE_REFRESH_INTERVAL_SECONDS`.

## Common Debug Entry Points

- Bad startup config: inspect `config.py`, `.env.example`, and the exact missing/invalid env var from the exception.
- Telegram handler behavior: inspect `create_dispatcher()` in `bot.py`.
- Missing messages in summaries: check `is_allowed()`, passive save handlers, `save_message_text()`, and `MessageStore.get_messages_since()`.
- OCR/video result not reused: check `video_recognition_cache_key()` and `video_recognitions` cache logic.
- Ollama failures/timeouts: inspect `llm.py`, recognizer classes, context/predict env vars, and `OLLAMA_TIMEOUT_SECONDS`.
- GPU/VRAM contention: inspect `gpu_lock` usage and model unload calls.
