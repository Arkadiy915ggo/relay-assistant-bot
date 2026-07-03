# Telegram Summary Bot

A safe Telegram bot that stores new chat messages and produces short AI summaries for a selected time period. The bot uses the official Telegram Bot API, so it does not log in as a user and cannot read chat history from before it was added.

## Features

- stores new text messages and media captions in SQLite;
- creates summaries with `/summary 24h`, `/summary 7d`, `/summary today`;
- compares summaries from multiple Ollama models with `/compare 10m`;
- includes a `Best joke` section in the generated report;
- works in direct chats, groups, supergroups, and channels;
- supports OpenAI API or a local Ollama model;
- splits long discussions into chunks and merges the final summary;
- restricts access with `ALLOWED_CHAT_IDS`;
- optionally transcribes Telegram voice/audio messages locally with `faster-whisper`.

## Telegram Limitations

- The bot cannot see messages sent before it was added.
- In groups, the bot sees all messages only if `Group Privacy` is disabled in `@BotFather`.
- In channels, the bot must be an administrator. It can receive new channel posts, but it will not receive comments from a linked discussion group unless it is added there too.
- In direct chats, the bot only sees messages sent directly to it.

## OpenAI API vs ChatGPT Subscription

A ChatGPT Plus/Pro subscription is not an API key. Automated bot usage requires `OPENAI_API_KEY` from OpenAI Platform, billed separately.

If you do not want to use an API key, run a local model through Ollama. Local summaries may be lower quality, but they are safer for private use.

## Quick Start

Requirements:

- Python 3.11+;
- Telegram bot token from `@BotFather`;
- one LLM option:
- `OPENAI_API_KEY`;
- or local Ollama.

Install:

```bash
cd telegram-summary-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Fill `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:your_real_token
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-your-key
OPENAI_MODEL=gpt-4o-mini
```

Run:

```bash
python -m tg_summary_bot
```

Or use the shell helper from the project root:

```bash
./run.sh install
./run.sh start
```

For local voice transcription with Whisper:

```bash
./run.sh install-voice
```

## BotFather Setup

1. Open `@BotFather`.
2. Run `/newbot`.
3. Choose the bot name and username.
4. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
5. For groups, disable privacy mode:
   `@BotFather` -> `/mybots` -> your bot -> `Bot Settings` -> `Group Privacy` -> `Turn off`.

Then add the bot to the target chat.

## Access Control

The first run can use an empty `ALLOWED_CHAT_IDS` to discover the target `chat_id`:

```env
ALLOWED_CHAT_IDS=
```

Send this command in the target chat:

```text
/stats
```

The bot will respond with something like:

```text
chat_id: -1001234567890
chat_type: supergroup
saved_messages: 42
llm_provider: openai
```

After that, restrict access:

```env
ALLOWED_CHAT_IDS=-1001234567890,123456789
```

Restart the bot.

## Commands

```text
/start
/help
/stats
/summary
/summary 30m
/summary 6h
/summary 24h
/summary 7d
/summary 2w
/summary today
/question your question
/question 24h your question
/compare 10m
```

## Response Logs

Bot replies are written as JSON Lines to `data/responses.log`. The path can be changed with:

```env
RESPONSE_LOG_FILE=data/responses.log
```

Use this log to inspect real bot answers and improve prompt quality.

## Voice Messages

The bot can locally transcribe Telegram voice/audio messages through `faster-whisper` and store the transcript as a normal message for future `/summary` calls.

Install CPU voice dependencies:

```bash
./run.sh install-voice
```

Install NVIDIA GPU voice dependencies:

```bash
./run.sh install-voice-cuda
```

Recommended high-quality NVIDIA GPU settings:

```env
TRANSCRIBE_VOICE=true
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
WHISPER_LANGUAGE=ru
MAX_VOICE_SECONDS=600
OLLAMA_UNLOAD_AFTER_TASK=true
```

LLM tasks and transcription share a single GPU queue: the bot does not run Ollama and Whisper at the same time. After `/summary` or `/compare`, the Ollama model is explicitly unloaded so Whisper can use the GPU.

`run.sh` automatically adds CUDA libraries from `.venv` to `LD_LIBRARY_PATH` when they are installed through `install-voice-cuda`.

If `WHISPER_DEVICE=cuda` fails with:

```text
RuntimeError: Library libcublas.so.12 is not found or cannot be loaded
```

Install CUDA voice dependencies and restart the bot:

```bash
./run.sh install-voice-cuda
./run.sh start
```

Quick library check:

```bash
SITE_PACKAGES="$(.venv/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
LD_LIBRARY_PATH="$SITE_PACKAGES/nvidia/cublas/lib:$SITE_PACKAGES/nvidia/cudnn/lib:$SITE_PACKAGES/nvidia/cuda_nvrtc/lib" \
  .venv/bin/python -c "import ctypes; ctypes.CDLL('libcublas.so.12'); ctypes.CDLL('libcudnn.so.9'); print('CUDA libs OK')"
```

If `large-v3` is too slow or VRAM is insufficient, downgrade in this order:

```env
WHISPER_MODEL=large-v3-turbo
WHISPER_MODEL=medium
WHISPER_MODEL=small
```

By default, `/summary` uses `DEFAULT_SUMMARY_PERIOD`, currently `24h`.

`/question` lets you chat with the assistant. The bot uses stored messages from the default period as optional context, but it can also answer general questions when the chat history is not relevant. Add a period as the first argument to override the context window, for example `/question 24h what did we discuss about Ollama?`. Answers are generated in Russian.

`/compare` runs the same message set through models from `COMPARE_MODELS`. This is useful for quality comparison, but it can be slow with 30B/70B models.

## Direct Chat Usage

1. Open the bot in Telegram.
2. Press `Start`.
3. Send messages to the bot.
4. Later, run `/summary 24h`.

Important: the bot cannot read your private chats with other people. It only sees the direct chat with the bot itself.

## Group And Supergroup Usage

1. Disable `Group Privacy` for the bot in `@BotFather`.
2. Add the bot to the group.
3. Preferably give it permission to read messages. Administrator rights are usually not required for normal groups, but they help avoid restrictions.
4. Run `/stats` and copy `chat_id` into `ALLOWED_CHAT_IDS`.
5. After a few hours or days, run `/summary 24h`.

Important: messages are only stored after the bot is added and has the required access.

## Channel Usage

1. Add the bot to the channel as an administrator.
2. Give it at least permission to receive channel posts. Allowing message publishing is useful if you want command responses, but this depends on the scenario.
3. New channel posts are stored as `channel_post` updates.
4. It is usually easier to call `/summary` from a direct chat or group if the channel does not allow bot dialogue.

If the channel has a linked discussion group, add the bot to that group too. Comments live in the discussion group, not in the channel itself.

## Local Ollama Model

Install Ollama and pull a model:

```bash
ollama pull llama3.1:8b
```

In `.env`:

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_TIMEOUT_SECONDS=1800
OLLAMA_KEEP_ALIVE=30m
OLLAMA_NUM_CTX=4096
OLLAMA_NUM_PREDICT=800
COMPARE_MODELS=qwen3:14b,gemma3:27b,qwen3-coder:30b
```

Run the bot:

```bash
python -m tg_summary_bot
```

List locally installed models:

```bash
ollama list
```

Then set the chosen model name in `OLLAMA_MODEL`.

If you see `ReadTimeout`, the model is usually loading or generating too slowly. This is normal on the first request for 30B/70B models. Options:

- increase `OLLAMA_TIMEOUT_SECONDS`, for example to `1800` or `3600`;
- keep `OLLAMA_KEEP_ALIVE=30m` so the model is not unloaded immediately;
- temporarily test speed with `OLLAMA_MODEL=qwen3:14b`;
- reduce the summary period, for example `/summary 10m` instead of `/summary 24h`.

## Performance And Privacy

- `MAX_MESSAGE_CHARS` truncates very long messages before storage.
- `MAX_SUMMARY_INPUT_CHARS` limits how much text is sent to the model for one summary request.
- `CHUNK_CHARS` controls chunk size for long discussions.
- `OLLAMA_NUM_CTX` controls the Ollama context size. If it is too small, long chunks can produce `400 Bad Request`.
- `OLLAMA_NUM_PREDICT` limits response length. This helps avoid timeouts with slow 70B models.
- `LOG_FILE` sets the bot log file, defaulting to `data/bot.log`.
- SQLite uses WAL mode, which is enough for small and medium chats.
- Secrets are stored in `.env`, which is ignored by git.

## Production Run

For long-running deployments, use `systemd`, `supervisor`, Docker, or another process manager. A minimal `tmux` session is also fine:

```bash
cd telegram-summary-bot
source .venv/bin/activate
python -m tg_summary_bot
```

## Data Location

Default database path:

```text
telegram-summary-bot/data/messages.sqlite3
```

Do not commit this directory to git.
