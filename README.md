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
- optionally transcribes Telegram voice/audio messages locally with `faster-whisper`;
- manually recognizes text from images with an Ollama vision model;
- makes simple image memes with `/meme` from a replied/latest image;
- manually recognizes videos through sampled key frames and auto-recognizes Telegram video notes;
- answers contextual questions when the bot is mentioned in a message;
- searches Wikipedia with `/wiki` and saves found excerpts for future context;
- compresses old chat history into structured SQLite memory blocks for long `/question` and `/summary` periods;
- keeps source-backed participant profile facts that can be used in answers;
- optionally logs LLM traces to Opik for answer quality analysis.

## Telegram Limitations

- The bot cannot see messages sent before it was added.
- In groups, the bot sees all messages only if `Group Privacy` is disabled in `@BotFather`.
- In channels, the bot must be an administrator. It can receive new channel posts, but it will not receive comments from a linked discussion group unless it is added there too.
- In direct chats, the bot only sees messages sent directly to it.
- The official cloud Telegram Bot API can reject large media downloads with `file is too big`. By default `TELEGRAM_DOWNLOAD_LIMIT_MB=0`, so the bot tries the download and handles Telegram's actual answer. Set a positive value only when you want an early local cutoff.

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

Recommended Mac + local Ollama start:

```bash
brew install ollama ffmpeg
ollama pull llama3.1:8b
./run.sh install
cp .env.ollama.example .env
```

Fill `TELEGRAM_BOT_TOKEN` in `.env`, then check and start:

```bash
./run.sh doctor
./run.sh start
```

This profile starts with text summaries/questions only. After it works, enable image/video/voice options in `.env` and run `./run.sh doctor` again.

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
OPENAI_BASE_URL=
OPENAI_MODEL=gpt-4o-mini
```

For OpenAI-compatible local or hosted servers such as LM Studio, LiteLLM, vLLM, or OpenRouter:

```env
LLM_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:1234/v1
OPENAI_API_KEY=not-needed
OPENAI_MODEL=local-model-name
```

Run:

```bash
python -m tg_summary_bot
```

Or use the shell helper from the project root:

```bash
./run.sh install
./run.sh doctor
./run.sh start
```

For local voice transcription with Whisper:

```bash
./run.sh install-voice
```

For Opik tracing:

```bash
./run.sh install-opik
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
/wiki what to search
/memory
/memory rebuild
/profile
/profile forget
/profile correct true fact
/transcribe
/image
/ocr
/meme
/video
/vocr
/compare 10m
```

## Wikipedia Search

The bot can search Wikipedia without any API key:

```text
/wiki Python programming language
```

Configure:

```env
WIKI_SEARCH_ENABLED=true
WIKI_LANGUAGE=ru
WIKI_TIMEOUT_SECONDS=20
WIKI_MAX_RESULTS=3
WIKI_USER_AGENT=telegram-summary-bot/0.1 (https://github.com/Arkadiy915ggo/relay-assistant-bot)
```

`/wiki` returns short article extracts with source links and saves successful results as normal stored messages, so future `/summary` and `/question` calls can use them as chat context. This is not a full web search engine; it only uses the selected Wikipedia language edition.

## Chat Memory

For periods longer than `MEMORY_RECENT_PERIOD`, the bot can compress older raw messages into structured SQLite memory blocks. `/question` uses fresh raw messages plus relevant memory blocks; `/summary` uses fresh raw messages plus memory blocks for the requested period.

Memory blocks now keep structured fields for summaries, decisions, tasks, open questions, important events, keywords, and source-backed participant facts. Old blocks are rolled up into higher-level blocks instead of being discarded immediately when the block count grows.

Participant profiles are built from explicit facts with source message ids, confidence, status, and optional expiration for temporary facts. Question answers and mention-triggered answers can include only relevant participant profile facts, including the author of the question and the author of a replied message.

Profile facts are updated in two ways: older messages are processed during long-memory compression, and recent raw messages are scanned after `/summary` and `/question` even for short periods. Recent profile scanning uses a separate, smaller chunk size derived from `OLLAMA_NUM_CTX` and capped by `CHUNK_CHARS`/`MEMORY_CHUNK_CHARS`, so a small context window automatically reduces profile extraction chunk size.

Configure:

```env
MEMORY_ENABLED=true
MEMORY_RECENT_PERIOD=24h
MEMORY_CHUNK_CHARS=18000
MEMORY_MAX_BLOCKS=200
MEMORY_SEARCH_LIMIT=8
```

With `OLLAMA_NUM_CTX=32768`, the default profile extraction chunk is already capped at a safe size. If you run Ollama with a much smaller context, keep `CHUNK_CHARS` and `MEMORY_CHUNK_CHARS` conservative or increase `OLLAMA_NUM_CTX`.

Check status:

```text
/memory
```

If you already have old unstructured memory blocks, reset them and let the bot rebuild them from stored raw messages on the next long `/summary` or `/question`:

```text
/memory rebuild
```

Inspect or edit participant profiles:

```text
/profile                 # show your profile, or reply to show another participant
/profile <name>          # search profile facts by participant name
/profile forget          # reply to a participant to forget active facts
/profile forget <name>   # forget active facts found by name
/profile correct <fact>  # reply to a participant to save a high-confidence correction
```

## Response Logs

Bot replies are written as JSON Lines to `data/responses.log`. The path can be changed with:

```env
RESPONSE_LOG_FILE=data/responses.log
```

Use this log to inspect real bot answers and improve prompt quality.

## Opik Tracing

Opik can collect LLM traces for `/summary`, `/question`, `/compare`, and transcript formatting. It is disabled by default because prompts can contain private chat history.

Install and configure:

```bash
./run.sh install-opik
opik configure
```

Enable in `.env`:

```env
OPIK_ENABLED=true
OPIK_PROJECT_NAME=telegram-summary-bot
OPIK_CAPTURE_CONTENT=true
```

Set `OPIK_CAPTURE_CONTENT=false` to log only metadata without prompts and outputs. For private chats, prefer a local or self-hosted Opik backend.

## Voice Messages

The bot can locally transcribe Telegram voice/audio messages through `faster-whisper` and store the transcript as a normal message for future `/summary` calls. It can also send the fast raw Whisper result first, then format the already-sent message with an LLM in the background.

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
WHISPER_LANGUAGE=
MAX_VOICE_SECONDS=600
TRANSCRIPTION_FORMAT_ENABLED=true
TRANSCRIPTION_FORMAT_PROVIDER=ollama
TRANSCRIPTION_FORMAT_MODEL=qwen2.5vl:7b
TRANSCRIPTION_FORMAT_NUM_CTX=16384
TRANSCRIPTION_FORMAT_NUM_PREDICT=4096
MAX_TRANSCRIPTION_FORMAT_CHARS=12000
OLLAMA_UNLOAD_AFTER_TASK=true
```

Recommended first-run macOS/CPU settings:

```env
TRANSCRIBE_VOICE=true
WHISPER_MODEL=small
WHISPER_DEVICE=cpu
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=
TRANSCRIPTION_FORMAT_ENABLED=true
TRANSCRIPTION_FORMAT_PROVIDER=ollama
TRANSCRIPTION_FORMAT_MODEL=qwen2.5vl:7b
```

LLM tasks, transcription, and transcript formatting share a single GPU queue: the bot does not run Ollama and Whisper at the same time. The raw Whisper transcript is sent immediately; formatting is scheduled after that response and edits the sent messages when it finishes. After local LLM work, the Ollama model is explicitly unloaded when `OLLAMA_UNLOAD_AFTER_TASK=true`.

Keep `WHISPER_LANGUAGE=` empty for automatic language detection, including Russian, Polish, and English speech. Use `WHISPER_LANGUAGE=ru`, `WHISPER_LANGUAGE=pl`, or another Whisper language code only when the chat is fixed to one language and auto-detection is worse on short messages. When transcript formatting is enabled, non-Russian speech is preserved in the original language and translated to Russian.

Set `TRANSCRIPTION_FORMAT_ENABLED=false` to keep raw Whisper output only. Set `TRANSCRIPTION_FORMAT_MODEL=` to disable formatting by leaving no model configured. Very long transcripts above `MAX_TRANSCRIPTION_FORMAT_CHARS` are left unformatted rather than partially rewritten.

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

## Image Recognition

The bot can recognize images manually through an Ollama vision model. It does not run OCR automatically for every incoming image. Incoming photos and image documents are only indexed by Telegram `file_id`, so the bot can later download the selected image when you explicitly ask. It can also make a simple meme from the replied/latest image with `/meme`.

Recommended local model for a good GPU such as RTX 4070 Ti Super:

```bash
ollama pull qwen2.5vl:7b
```

Configure `.env`:

```env
IMAGE_RECOGNITION_MODEL=qwen2.5vl:7b
IMAGE_RECOGNITION_NUM_CTX=8192
MAX_IMAGE_SIZE_MB=20
IMAGE_DOWNLOAD_DIR=data/images
MEME_ENABLED=true
MEME_MODEL=
MEME_OUTPUT_DIR=data/memes
MEME_FONT_PATH=
MEME_MAX_IMAGE_SIZE_MB=20
MEME_NUM_PREDICT=160
OLLAMA_UNLOAD_AFTER_TASK=true
```

Usage:

```text
/image
/ocr
/meme
```

Behavior:

- If `/image` is sent as a reply to an image, the bot recognizes only the replied image.
- If `/image` is sent without a reply, the bot recognizes the latest indexed image in the chat.
- `/ocr` is an alias for `/image`.
- `/meme` uses the replied image or the latest indexed image, asks the vision model for a short safe Russian joke, renders classic top/bottom meme text with Pillow, sends the resulting image, and deletes temporary files.
- `/image` and `/ocr` results are saved as normal stored messages, so future `/summary` and `/question` calls can use them. `/meme` does not save generated images in SQLite.

The image response format is:

```text
Text from image in the original language
Russian translation if the text is not Russian
Short Russian summary of the image
```

The vision model uses the same Ollama server settings as text models: `OLLAMA_BASE_URL`, `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_KEEP_ALIVE`, and `OLLAMA_NUM_PREDICT`. Image recognition uses its own context size through `IMAGE_RECOGNITION_NUM_CTX`, so changing it does not affect `/summary`. `/meme` uses `MEME_MODEL`, or `IMAGE_RECOGNITION_MODEL` when `MEME_MODEL` is empty. Meme rendering uses `MEME_FONT_PATH` when set, then tries DejaVu Sans Bold from the system, then falls back to Pillow's default font. The bot runs image recognition and meme caption generation inside the same GPU queue as summaries and voice transcription. After recognition, it unloads the vision model when `OLLAMA_UNLOAD_AFTER_TASK=true`.

## Video Recognition

The bot can manually recognize ordinary Telegram videos, video documents, and Telegram video notes/circles. It does not analyze every incoming video automatically. Incoming videos are only indexed by Telegram `file_id`, and recognition runs only when requested.

Install the recommended non-thinking vision model for video frames:

```bash
ollama pull qwen2.5vl:7b
```

Make sure `ffmpeg` is available:

```bash
ffmpeg -version
```

Configure `.env`:

```env
VIDEO_RECOGNITION_MODEL=qwen2.5vl:7b
VIDEO_RECOGNITION_NUM_CTX=16384
VIDEO_RECOGNITION_NUM_PREDICT=800
MAX_VIDEO_SIZE_MB=50
TELEGRAM_DOWNLOAD_LIMIT_MB=0
MAX_VIDEO_SECONDS=120
VIDEO_DOWNLOAD_DIR=data/video
VIDEO_FRAME_DIR=data/video_frames
VIDEO_FRAME_COUNT=8
VIDEO_FRAME_MAX_WIDTH=960
VIDEO_TRANSCRIBE_AUDIO=false
OLLAMA_UNLOAD_AFTER_TASK=true
```

Usage:

```text
/video
/vocr
```

Behavior:

- If `/video` is sent as a reply to a video or Telegram video note, the bot recognizes only the replied video.
- If `/video` is sent without a reply, the bot recognizes the latest indexed video in the chat.
- `/vocr` is an alias for `/video`.
- Videos over `MAX_VIDEO_SIZE_MB` are rejected before recognition. If `TELEGRAM_DOWNLOAD_LIMIT_MB` is positive, it is also used as an early cutoff; otherwise the bot attempts the download and reports Telegram's real `file is too big` response if it happens.
- The bot downloads the video, extracts key frames with `ffmpeg`, sends those frames to `VIDEO_RECOGNITION_MODEL`, optionally extracts/transcribes the audio track when `VIDEO_TRANSCRIBE_AUDIO=true`, unloads the model after the task, and deletes temporary files.
- Repeated `/video` calls for the same message and same video settings use a SQLite cache instead of rerunning `ffmpeg` and Ollama.
- The result is saved as a normal stored message, so future `/summary` and `/question` calls can use it.

The video response format is:

```text
Text from video frames in the original language
Russian translation if the text is not Russian
Short Russian summary of the video
Short description of what happens in the video
Audio transcript if the video contains speech
```

Video recognition shares the same GPU queue as summaries, image recognition, and voice transcription, so heavy local tasks do not run at the same time.

Audio transcription for videos uses the same local Whisper settings as voice messages: `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, and `WHISPER_LANGUAGE`. If `VIDEO_TRANSCRIBE_AUDIO=true`, install voice dependencies with `./run.sh install-voice` or `./run.sh install-voice-cuda`. With `WHISPER_LANGUAGE=` the bot can auto-detect Polish speech in ordinary videos and Telegram video notes/circles.

If video recognition hits a context error such as `request (...) exceeds the available context size`, the bot automatically retries with fewer and smaller frames. If even the compressed fallback is not enough, reduce the frame settings first:

```env
VIDEO_FRAME_COUNT=4
VIDEO_FRAME_MAX_WIDTH=640
```

Only increase the video context setting if you have enough memory for it:

```env
VIDEO_RECOGNITION_NUM_CTX=32768
```

Speed and OCR quality knobs:

```env
VIDEO_FRAME_COUNT=8
VIDEO_FRAME_MAX_WIDTH=960
VIDEO_RECOGNITION_NUM_PREDICT=800
```

Lower `VIDEO_FRAME_COUNT` to speed up processing. Lower `VIDEO_FRAME_MAX_WIDTH` to reduce image tokens and speed up vision processing, but this may make small text harder to read. Increase it back to `1280` for better OCR if your context size allows it.

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
brew install ollama ffmpeg  # macOS
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
COMPARE_MODELS=
```

The recommended `.env.ollama.example` documents the maintainer's stable local model choices:

- `llama3.1:8b` for the safest text summaries/questions baseline.
- `qwen2.5vl:7b` for stable `/image`, `/ocr`, `/meme`, and `/video` frame recognition.
- `qwen3:14b`, `gemma3:27b`, and `qwen3-coder:30b` for `/compare` on stronger local machines.

A Mac with 48 GB unified memory can often run heavier models than a smaller desktop GPU setup. Start with `llama3.1:8b`, confirm the bot works with `./run.sh doctor`, then pull larger models and add them to `COMPARE_MODELS`.

Check local setup before starting:

```bash
./run.sh doctor
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
- `MEMORY_CHUNK_CHARS` controls old-message memory compression chunks, capped by `CHUNK_CHARS`.
- Recent participant profile extraction chooses a smaller dynamic chunk from `OLLAMA_NUM_CTX`, `CHUNK_CHARS`, and `MEMORY_CHUNK_CHARS`.
- `OLLAMA_NUM_CTX` controls the Ollama context size. If it is too small, long chunks can produce `400 Bad Request` or very short model outputs.
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
