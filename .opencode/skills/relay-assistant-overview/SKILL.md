---
name: relay-assistant-overview
description: Use when working in relay-assistant-bot, telegram-summary-bot, tg_summary_bot, bot.py, summarizer.py, storage.py, Ollama, OpenAI, Whisper, OCR, video recognition, or project onboarding/debug tasks. Provides the project architecture, safe inspection rules, commands, and module responsibilities.
---

# Relay Assistant Overview

Use this skill before doing non-trivial work in this repository. It exists so agents do not re-discover the same architecture every session.

## Project Summary

`telegram-summary-bot` is an async Python Telegram bot. It stores new Telegram messages in SQLite and uses AI models to create Russian summaries, answer questions with recent chat context, recognize text/images/videos, and transcribe voice/audio.

The project is deliberately compact. Most orchestration lives in `src/tg_summary_bot/bot.py`; each expensive or external capability has one focused module.

## Safe Context Rules

- Read `AGENTS.md` first for the current project guide.
- Use `.env.example` for config shape.
- Do not read `.env` unless the user explicitly asks; it contains secrets.
- Do not inspect `data/` unless the user explicitly asks; it contains private chat data, logs, SQLite DBs, and downloaded media.
- Do not treat generated `__pycache__/`, `.venv/`, `*.egg-info`, or runtime files as source.

## First Files To Inspect

- `AGENTS.md`: concise project guide for agents.
- `README.md`: user-facing setup and behavior details.
- `pyproject.toml`: package metadata, dependencies, ruff settings.
- `run.sh`: supported install/start helpers.
- `.env.example`: complete configuration surface without secrets.
- `src/tg_summary_bot/bot.py`: handlers and runtime flow.
- `src/tg_summary_bot/config.py`: settings and environment parsing.
- `src/tg_summary_bot/storage.py`: SQLite schema and persistence.
- `src/tg_summary_bot/transcript_formatter.py`: optional post-processor for already-sent Whisper transcripts.
- `src/tg_summary_bot/memory.py`: long-term memory helper used by `/summary`, `/question`, and `/memory`.

## Architecture Map

- Entry point: `python -m tg_summary_bot` -> `__main__.py` -> `bot.main()`.
- Settings: `load_settings()` reads `.env`, validates provider/token, and returns frozen `Settings`.
- Storage: `MessageStore.init()` creates SQLite tables for messages, images, videos, and video recognition cache.
- Memory schema: storage also creates `chat_memory_blocks` and `chat_memory_state`; `memory.py` compresses/searches old chat history for long `/summary` and `/question` periods.
- LLM: `build_llm_client()` returns either `OpenAIClient` or `OllamaClient` behind the `LLMClient` interface.
- Dispatcher: `create_dispatcher()` registers Telegram commands and passive indexing handlers.
- GPU serialization: one `asyncio.Lock` prevents Ollama/Whisper jobs from running at the same time.
- Polling: `start_polling()` listens for `message` and `channel_post` updates with `handle_as_tasks=False`.

## Feature Flows

### Text Storage

Incoming non-command text/captions pass through `save_incoming_message()` and `save_message_text()`. Text is normalized, truncated by `MAX_MESSAGE_CHARS`, and saved to SQLite with UTC timestamps.

### Summaries

`/summary [period]` parses the period, loads stored messages since that UTC cutoff, and calls `Summarizer.summarize()`. Long input is split by `CHUNK_CHARS`; partial summaries are merged into the final report.

### Questions

`/question [period] <text>` loads recent messages and calls `ChatAssistant.ask()`. For long histories it first extracts relevant notes from chunks, then answers using only relevant chat context when helpful.

### Image Recognition

Photos and image documents are passively indexed by `file_id`. `/image` and `/ocr` choose the replied image or latest indexed image, download it, send it to an Ollama vision model, save the result as a message, and delete the temporary file.

### Video Recognition

Videos, video documents, and video notes are passively indexed by `file_id`. `/video` and `/vocr` choose the replied/latest video, check size/duration limits, reuse a SQLite cache when possible, otherwise download the video, extract key frames with `ffmpeg`, optionally extract/transcribe audio, combine visual and audio results, cache them, save them as a message, and delete temporary files.

### Voice Transcription

When `TRANSCRIBE_VOICE=true`, voice/audio messages are downloaded and transcribed with `faster-whisper`. The transcript is saved as a normal stored message for later summaries/questions.

If `TRANSCRIPTION_FORMAT_ENABLED=true`, the raw Whisper transcript is sent first, then `TranscriptFormatter` runs through the configured LLM in the background and edits the already-sent Telegram messages with lightly formatted text.

### Model Comparison

`/compare [period]` requires Ollama and runs the same message set through each model in `COMPARE_MODELS`.

### Long-Term Memory

`memory.py` contains `ChatMemory` and `should_use_memory()` for compressing older messages into reusable memory blocks. `config.py` exposes `MEMORY_ENABLED`, `MEMORY_RECENT_PERIOD`, `MEMORY_CHUNK_CHARS`, `MEMORY_MAX_BLOCKS`, and `MEMORY_SEARCH_LIMIT`; `storage.py` has the backing tables and queries.

## Verification Commands

- Syntax check: `.venv/bin/python -m compileall src`.
- Fallback syntax check: `python -m compileall src`.
- Start bot after config exists: `./run.sh start`.
- Install editable package: `./run.sh install`.
- Install voice extras: `./run.sh install-voice` or `./run.sh install-voice-cuda`.

There is no dedicated test suite at the time this guide was written. Prefer targeted compile/import checks and focused manual reasoning.

## Change Guidance

- Add new environment variables to `Settings`, `load_settings()`, `.env.example`, and README if user-facing.
- Add new Telegram commands inside `create_dispatcher()` and update `/help` plus README command docs.
- Add new persisted fields carefully; SQLite schema currently uses `CREATE TABLE IF NOT EXISTS` without migrations.
- Keep external model calls behind their focused modules when possible.
- Always consider Telegram message length and split long replies.
- Preserve model unload behavior when adding Ollama-heavy tasks.
