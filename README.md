# Telegram Summary Bot

A safe Telegram bot that stores new chat messages and produces short AI summaries for a selected time period. The bot uses the official Telegram Bot API, so it does not log in as a user and cannot read chat history from before it was added.

## Features

- stores new text messages and media captions in SQLite;
- creates summaries with `/summary 24h`, `/summary 7d`, `/summary today`;
- compares summaries from multiple Ollama models with `/compare 10m`;
- optionally awards `+10` chat-scoped virtual points from durable autonomous joke blocks;
- shows personal joke points with `/balance` and a deterministic top-10 with `/top`;
- spins one virtual Telegram slot with `/casino` for a fixed 10-point stake;
- works in direct chats, groups, supergroups, and channels;
- supports OpenAI API or a local Ollama model;
- splits long discussions into chunks and merges the final summary;
- restricts access with `ALLOWED_CHAT_IDS`;
- optionally transcribes Telegram voice/audio messages locally with `faster-whisper`;
- optionally generates Telegram voice replies with CosyVoice 3: `/voice`, `/ask voice`, `/voice_type`;
- manually recognizes text from images with an Ollama vision model;
- makes simple image memes with `/meme` from a replied/latest image;
- manually recognizes videos through sampled key frames and auto-recognizes Telegram video notes;
- routes addressed messages to contextual answers, summaries, Wikipedia, media recognition, transcription, memes, or profiles;
- lets chat participants add aliases for addressing the bot in regular messages and media captions;
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

## Voice generation (Feature 06)

TTS is disabled by default and uses a separate Python 3.10 environment. The bot stays
on Python 3.11+; its mandatory dependencies do not include torch or CosyVoice.
`install-voice` and `install-voice-cuda` still install **Whisper transcription**.

### Installation and enabling

The supported TTS setup is **Linux with an NVIDIA GPU** and a driver compatible with
CUDA 12.1. The helper installs CUDA-enabled PyTorch; it does not install the NVIDIA
driver or provide a CPU/macOS TTS fallback. Text commands remain usable on those systems
with `TTS_ENABLED=false`.

1. Complete [Quick Start](#quick-start) for the text bot (`./run.sh install`, Telegram
   token and LLM settings in `.env`). Keep your existing `.env` when adding TTS.
2. Install `uv`, Git, ffmpeg/ffprobe and C/C++ build tools (needed by Python audio
   dependencies). On Debian/Ubuntu, system packages include `git ffmpeg build-essential
   python3-dev`. Install `uv` using its [official instructions](https://docs.astral.sh/uv/getting-started/installation/).
3. Check the driver and audio tools:

```bash
nvidia-smi
uv --version
ffmpeg -hide_banner -h filter=rubberband
ffmpeg -hide_banner -h encoder=libopus
ffprobe -version
```

The ffmpeg commands must describe `Filter rubberband` and `Encoder libopus`, rather
than report an unknown filter/encoder. Both bundled presets use the same availability
check, so rubberband is required even when the selected voice is Cartman.

4. From the repository root, install the separate TTS environment and model:

```bash
./run.sh install-tts-cosyvoice3
```

This explicit helper installs the pinned runtime baseline and downloads about
5 GB of model files. It requires `uv`, Git, an NVIDIA/CUDA installation, and ffmpeg
with `libopus` and the `rubberband` filter. It is never run at startup or by tests.
Upstream revision: `074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc`.
Model: `FunAudioLLM/Fun-CosyVoice3-0.5B-2512`, revision
`29e01c4e8d000f4bcd70751be16fa94bf3d85a18` (pinned from the model catalogue;
real synthesis on this installation remains a manual check).
PyTorch/torchaudio: 2.3.1/cu121; ONNX Runtime GPU: 1.18.0.
See `scripts/install_tts_cosyvoice3.sh` for the remaining pinned baseline dependencies.

5. Add or update these values in `.env`:

```dotenv
TTS_ENABLED=true
TTS_PROVIDER=cosyvoice3
TTS_DEFAULT_VOICE=anubarak
TTS_PYTHON_PATH=.venv-cosyvoice3/bin/python
TTS_COSYVOICE_UPSTREAM_DIR=tts/cosyvoice3/upstream
TTS_MODEL_DIR=tts/cosyvoice3/models/Fun-CosyVoice3-0.5B-2512
TTS_LOCAL_VOICES_DIR=voices/local
TTS_OUTPUT_DIR=data/tts
TTS_MAX_TEXT_CHARS=2000
TTS_MAX_AUDIO_SECONDS=180
TTS_TIMEOUT_SECONDS=180
TTS_ENCODING_TIMEOUT_SECONDS=60
TTS_UPLOAD_TIMEOUT_SECONDS=60
```

If the bot uses local Ollama, also set `OLLAMA_UNLOAD_AFTER_TASK=true` so completed
LLM requests release their model before TTS needs the GPU. This is independent of
`TRANSCRIBE_VOICE`: you do not need Whisper to generate speech.

Paths resolve against the installation working directory; bundled presets resolve
against the installed package. The helper accepts exported `TTS_PYTHON_PATH`,
`TTS_COSYVOICE_UPSTREAM_DIR`, and `TTS_MODEL_DIR`; it does not read `.env`.
For custom paths, export the same values before running the installer and put them
in `.env` for the bot. No files under `experiments/` are needed for setup or runtime;
the two ready references and their transcripts are included in the installed package.

6. Validate the setup, then start (or restart) the bot:

```bash
./run.sh doctor
./run.sh start
```

Doctor should report `CosyVoice runner/CUDA and ffmpeg libopus/rubberband available`.
Disabled or broken TTS leaves ordinary
text commands usable. Startup checks runner/imports/CUDA and ffmpeg capabilities
without loading the synthesis model. No model is downloaded automatically.

### Disabling

Set this in `.env` and restart the bot:

```dotenv
TTS_ENABLED=false
```

No CosyVoice environment, model, CUDA or ffmpeg capability checks are required in
this mode. `/voice` and `/ask voice` report that speech is unavailable before any
model work. Ordinary `/ask` and `/question` still work; Whisper remains controlled
by `TRANSCRIBE_VOICE`. `/voice_type` can still show or save a selection for later
use, and disabling TTS preserves the saved chat selections.

### Commands and bundled voices

| Command | Result |
| --- | --- |
| `/voice <text>` | Speak supplied text as an OGG/Opus voice reply to the command. |
| `/ask [period] <question>` | Alias of `/question` with its contextual answer behavior. |
| `/ask voice [period] <question>` | Generate one answer, send all text parts as replies, then speak only its body. |
| `/voice_type` | Show effective voice, selection source, available IDs and usage. |
| `/voice_type <id>` | Save this chat's selection; available to any participant in an allowed chat. |
| `/voice_type reset` | Remove the override and use the bot default. |

Only the first `/ask` argument `voice` is a flag. `/question voice ...` remains a
literal question. Standard `@bot_username` suffixes work. Voice settings and synthesis
are explicit slash commands, not intent-router actions.

The bundled `anubarak` voice uses the original owner-confirmed
`nerubiancryptlordpissed3.ogg`, converted to 24 kHz mono PCM WAV (8.93 seconds), with
the exact transcript «Мы, нежить, народ работящий. Такая уж наша тяжкая доля.».
Zero-shot generation always uses model speed **1.0**, followed by `atempo=0.85`,
pitch **−1 semitone with preserved formants**, then Opus encoding. No substitute
effect is used if rubberband is unavailable.

The second bundled voice is **`cartman` / Картман**. Its ready reference and
transcript are `src/tg_summary_bot/voice_presets/cartman/reference.wav` and
`preset.json`. The user-selected 11.4-second reference
is converted to mono PCM16 24 kHz for package compatibility; no tempo/pitch effects
or normalization are applied. The supplied transcript remains the experiment's ASR
draft; independent listening verification was not performed during bundling.
Generated speech uses model speed **1.0**, with **no atempo or rubberband filters**;
only OGG/Opus encoding and Telegram-compatible channel/sample-rate conversion remain.

Restart the bot to load the preset, then select it for the current chat:

```text
/voice_type cartman
/voice Привет! У меня есть отличный план.
```

Use `/voice_type reset` to return to the configured default. To make Cartman the
bot-wide default instead, set `TTS_DEFAULT_VOICE=cartman` and restart.

Additional voices live in the fully ignored `voices/local/<directory>/` (or configured
local root). Copy the bundled `preset.json` schema and provide an original PCM WAV
reference with an exact transcript, sample rate **at least 16 kHz**, and duration
**greater than zero and at most 30 seconds** (30 seconds is accepted). Incompatible
references are excluded before synthesis, with `reference_sample_rate_below_16000_hz`
or `reference_longer_than_30_seconds` in the registry diagnostics/startup or doctor output.
Prepare a compatible WAV and restart to reload the library.
IDs use lowercase ASCII letters/digits/underscore/
hyphen; `reset` is reserved. Version 1 supports `ru/en/zh/ja/ko/de/fr/es/it`, speed
0.5–2, pitch −12–12 semitones and boolean `preserve_formants`. References
must resolve within the preset directory, including symlinks. Invalid and duplicate
local entries are diagnosed and excluded; bundled IDs cannot be silently replaced.
Restart after library edits. A removed chat voice falls back to the valid configured
default with a notice. An invalid default makes TTS unavailable.

`MessageStore.init()` idempotently creates `chat_voice_settings(chat_id, voice_id,
updated_at)`; no manual SQL migration is required. Direct speech is persisted only
after upload as `generated/tts_text` with reply linkage and is not a joke source.
Voice answers keep one canonical `assistant/assistant_answer`; the audio is not a
second eligible source. No outgoing voice invokes Whisper or transcript formatting,
including automatic updates and slash/routed `/transcribe` replies to this bot.
Human voice transcription keeps its existing behavior.

Oversized direct text is rejected before work. Oversized generated answers are
delivered fully as text with a voice-skipped notice, without truncation or rewriting.
If synthesis, encoding or upload fails, the text remains available; persistence and
voice errors both appear in the partial outcome. Each operation has its own temporary
directory kept until upload finishes. Timed-out/cancelled runner and ffmpeg processes
are terminated, escalated to kill if needed, and awaited before unlock/cleanup.
The synthesis execution timeout starts after acquiring the existing shared GPU lock;
encoding and upload have separate limits.

### First Telegram check and troubleshooting

In an allowed chat, send these commands one at a time:

```text
/voice_type anubarak
/voice Привет! Проверяем голос Ануб’арака.
/voice_type cartman
/voice Привет! Проверяем голос Картмана.
/ask voice 24h Что обсуждали в чате?
/voice_type reset
```

Check that `/voice` returns a playable voice reply and `/ask voice` sends its text
first, then audio replying to the same command. A very long answer can be text-only
with a voice-skipped notice; `TTS_MAX_TEXT_CHARS` limits spoken text, not the full
text response.

| Doctor diagnosis | What to check |
| --- | --- |
| `disabled` | Set `TTS_ENABLED=true` and restart when speech is wanted. |
| `missing_python` / `wrong_python_version` | Run the TTS installer; `TTS_PYTHON_PATH` must point to the separate Python 3.10 environment. |
| `missing_upstream` / `missing_model` | Check the configured paths or rerun the installer with the same exported paths. |
| `missing_dependencies` / `runner_incompatible` | Reinstall the pinned TTS environment and upstream submodules. |
| `cuda_unavailable` | Check `nvidia-smi` and CUDA visibility in the TTS environment. |
| `missing_ffmpeg` / `missing_rubberband_or_libopus` | Install ffmpeg/ffprobe with both the required filter and encoder. |
| `invalid_default_voice` | Use the bundled `anubarak` or `cartman` ID, or a valid installed local preset. |
| `invalid_limits` | All `TTS_MAX_*` and `TTS_*_SECONDS` values must be positive integers. |

For the **manual acceptance smoke**, listen to the original reference and short/long
Russian outputs; check tempo, pitch, Telegram playback and direct reply IDs. Measure
cold/warm time and VRAM for LLM → TTS with `OLLAMA_UNLOAD_AFTER_TASK=true`.
Ollama unload failures and other resident models may still retain VRAM; serialization
alone cannot prevent OOM. Whisper creates/deletes its model per transcription call.
No external processes are killed. GPU synthesis, listening and Telegram playback
have not been verified by the isolated automated tests.

### Runtime stabilization and voice tracing

Background participant-profile refresh uses **one batch per attempt**, bounded by
`PROFILE_BATCH_CHARS=2000` (including rendered headers) and `PROFILE_BATCH_MESSAGES=8`.
It asks for at most one useful source-backed fact, using a separate profile client
with `PROFILE_NUM_PREDICT=256` for Ollama and a `PROFILE_TIMEOUT_SECONDS=60` request
limit. `PROFILE_MODEL` optionally chooses an already-installed model; empty uses the
main LLM model. These settings do not change ordinary answer generation.
If the model returns several candidates, rejected entries are skipped before applying
the limit: at most the first valid fact is saved, so an invalid first candidate cannot
hide a later valid one. General memory compression keeps its existing multi-fact behavior.

Profile work has a two-second foreground grace period, skips an already-busy GPU,
coalesces overlapping requests per chat, and attempts at most once per
`PROFILE_REFRESH_INTERVAL_SECONDS=120`. It releases the shared lock after the batch
and model-unload attempt. The request timeout does not include lock wait or unload;
Ollama unloading keeps its existing bounded, best-effort behavior.
Invalid/truncated JSON and timeouts do **not** advance the checkpoint. The next
admitted refresh can retry. A timestamp/message-ID cursor prevents loss when smaller
batches split messages sharing the same timestamp. Startup idempotently adds nullable
`chat_profile_state.processed_message_id`; legacy timestamp checkpoints are preserved.
Oversized messages are clipped only in the model input; raw stored messages remain intact.

With Opik enabled, slash commands have end-to-end `telegram.voice` / `telegram.question`
traces. Speech spans show GPU wait, synthesis, encoding, validation, upload, and direct
voice persistence. Voice-answer traces also include model unload, text delivery and
canonical persistence, with existing LLM spans nested under the command. Stage metadata
does not include spoken text, reference paths, audio files or raw provider exceptions.
Handled failures/partial outcomes are searchable as trace errors without re-failing the
Telegram handler. Profile refresh is an independent trace; invalid JSON is a failure.

An optional **synthetic** ingestion check is available:

```bash
.venv/bin/python tests/voice_trace_smoke.py
```

It uses the existing Opik SDK configuration and publishes tagged verification traces.
It uses fake model/ffmpeg/Telegram calls and a temporary SQLite database: no production
data, `.env`, GPU work or Telegram requests. It is not part of the offline unit suite.

Leaderboard `message is not modified` responses are quiet successes. A confirmed lack
of pin permission no longer retries an otherwise-current balance display forever:
pinning becomes best effort, with a ten-minute permission retry cooldown per bot/chat/
message. Editing remains active. Transient failures stay retryable, and only confirmed
not-found errors recreate the message.

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

`/stats` intentionally bypasses `ALLOWED_CHAT_IDS` so it can be used to discover a target `chat_id`. Its response includes `access_allowed: true|false`; this exception does not allow message storage, routed actions, or other commands in a denied chat.

You may keep the normal allowlist configured while discovering another chat. An empty value still allows all chats and can be used for an unrestricted first run:

```env
ALLOWED_CHAT_IDS=
```

To keep the bot available but disable economics in selected chats, use separate comma-separated lists:

```env
JOKE_AWARDS_DISABLED_CHAT_IDS=-1001111111111,-1002222222222
CASINO_DISABLED_CHAT_IDS=-1001111111111,-1002222222222
```

Joke-disabled chats do not run manual or autonomous joke selection, awards, announcements, or bot casino outbox work. Casino-disabled chats reject `/casino`, `/casino bot`, and addressed casino requests without changing balances.

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
access_allowed: false
```

After that, restrict access:

```env
ALLOWED_CHAT_IDS=-1001234567890,123456789
```

Restart the bot.

The current allowlist also applies when the autonomous worker claims existing jobs and outbox
actions. Removing a chat pauses its queued work; it does not delete it. Re-allowing the chat can
resume that work. Startup casino refunds still repair reserved balances in SQLite, but Telegram
leaderboard refreshes are sent only to currently allowed chats.

## Commands

```text
/start
/help
/stats
/balance
/top
/casino
/casino bot
/summary
/summary 30m
/summary 6h
/summary 24h
/summary 7d
/summary 2w
/summary today
/question your question
/question 24h your question
/alias add Реле, Релейка
/alias list
/alias remove Реле
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
/video https://youtu.be/example
/vocr
/compare 10m
```

## Joke Points

With `AUTONOMOUS_JOKES_ENABLED=false` (the default), a successful `/summary` keeps the legacy manual
selector and can award one verified joke. When enabled, a durable worker creates non-overlapping blocks
of 50 eligible messages, or aged 5-49-message blocks after 3 days. Each block has zero or one winner
worth `+10`; shadow mode records selections without changing balances or Telegram messages. Enabled
`/summary` is read-only and renders an already committed autonomous result when available. `/compare`
never selects jokes or changes balances.

Before a selected winner can earn points, a separate qualification call checks only its
visible text. It must identify wordplay, irony, absurdity, a punchline, or a comic comparison
and quote supporting text exactly. Candidates depending on an unseen image/chat context
are declined, including contradictory rationales claiming missing context. A plain reaction
or insult is not enough; rough language and short self-contained jokes remain allowed.
The primary selector's JSON contract is unchanged. Qualification uses its own strict schema;
invalid JSON/IDs lead to retry without points, while a valid rejection means no joke in that
block. This adds one model call for a selected finalist, with the same shared GPU lock,
per-call timeout and lease renewal. The judge output budget is 256 tokens. Humour remains a
model judgment; the qualification guard does not guarantee agreement with every human rating.

Autonomous model work is scheduled in batches, independently of the 30-second outbox
poll. `AUTONOMOUS_JOKES_RUN_INTERVAL=6h` admits a run at most once per six hours, with
up to `AUTONOMOUS_JOKES_MAX_JOBS_PER_RUN=4` ready jobs. The first nonempty run can start
after startup grace; empty queues and a busy GPU do not consume the interval. Admission
is persisted atomically with the first job claim, so restarting does not reset it.
Retries also wait for a scheduled run, even when their individual backoff has elapsed.
Busy chats may accumulate a backlog at these conservative defaults; increase the job
limit or shorten the run interval if faster processing is needed.

The autonomous judge accepts up to 50 candidates per prompt within the existing character
budget: a short 50-message block needs one selection plus one qualification, rather than
three selection pages, a tournament and qualification. Longer blocks still use bounded
pages. The model stays loaded across pages, tournament, qualification and adjacent jobs.
`AUTONOMOUS_JOKES_SESSION_MAX_CALLS=8` and `AUTONOMOUS_JOKES_SESSION_MAX_SECONDS=120`
bound each GPU session. These are checked between calls, not a cancellation deadline:
one in-flight call and model unloading can extend the time limit. At a boundary the worker
unloads under the shared lock, releases it and lets queued foreground work run before
continuing the same tournament. Normal configured Ollama unload/keep-alive behavior applies.
Award writes occur only after releasing the GPU lock.

`AUTONOMOUS_JOKES_MAX_VALIDATION_FAILURES=3` quarantines a job after repeated malformed
model results, without points or notifications. The separate durable `validation_failures`
counter excludes GPU contention, lost leases, timeouts, model/network errors and storage
errors. Existing jobs start this new counter at zero on upgrade. Quarantined rows retain
their immutable source block and error code; `quarantined_at` excludes them from automatic
claims (their underlying status remains `retry` for legacy SQLite CHECK compatibility).
They require explicit maintenance to resume, not a restart. Logs show job/chat/attempt,
safe error code, next attempt, model-call count, GPU-session count and elapsed time; no
source text or model-generated rationale is added to those events.

Award delivery is separate from accounting. `AUTONOMOUS_JOKE_ANNOUNCEMENT_MAX_AGE=24h`
limits how long a committed award can wait for a public notification. Older notices are
retired as `skipped` with `announcement_expired`; **earned points and ledger rows are never
removed or awarded again**. Fresh notices take priority and are delivered before unrelated
model selection work. Notices include the original award time in UTC, so delivery time is
not presented as a new credit. Up to ten expired notices are retired per delivery pass,
with at most one actual Telegram action per pass. A newly awarded aged-message block can
still be announced: freshness is measured from award commitment, not source-message age.
Allowlisted/disabled-chat, shadow, activation and action-pause policies continue to apply;
paused notices are assessed for expiry only when their delivery is enabled again. Existing
pending notices use this policy after the bot restarts; no schema migration is needed.

`AUTONOMOUS_JOKE_ANNOUNCE_BACKLOG=false` additionally prevents startup catch-up bursts:
notices for awards committed **before the current worker started** are skipped as
`announcement_before_worker_start`, even when they are less than 24 hours old. Awards
committed after startup can be announced normally, including newly processed aged blocks.
The cutoff is per worker instance, independent of the durable first-activation boundary.
Points, past awards, leaderboard updates and casino decisions are unchanged. Set backlog
to true only when catch-up delivery is wanted; the age limit still applies. Chat/access,
shadow and action-pause policies still gate any retirement or delivery.

`AUTONOMOUS_JOKE_ANNOUNCEMENT_MIN_INTERVAL=12h` is the **per-chat posting limit**:
automatic best-joke notices are sent no more often than once per half day in each chat.
This is separate from block size, notice age, and startup-backlog handling. Points and
leaderboard updates still proceed; excess notices are deferred with
`announcement_rate_limited` and are subject to the usual freshness/startup policy later.
Other chats are independent and are not blocked by one chat's cooldown.

SQLite `chat_joke_announcement_state` stores the latest attempt and confirmed delivery
timestamps. Startup creates it idempotently and seeds confirmed deliveries from existing
outbox history, so upgrading or restarting cannot reset the hour. Send admission uses
the current outbox lease and an atomic transaction across workers. An uncertain/failed
send also consumes the cooldown, preventing rapid retries that could duplicate a delivered
message. Confirmed completion records its delivery timestamp atomically with outbox completion.
No manual SQL migration or balance adjustment is needed. `/stats` shows the configured interval.

Configure rollout with `AUTONOMOUS_JOKES_SHADOW_MODE`, block/partial/age settings,
startup grace, poll and lease intervals, optional `AUTONOMOUS_JOKE_JUDGE_MODEL`, and
`AUTONOMOUS_JOKE_ANNOUNCE`. Bot casino uses `BOT_AUTO_CASINO_ENABLED` and
`BOT_AUTO_CASINO_CHANCE`; `.env.example` documents all defaults and validation.

Both `.env.example` and `.env.ollama.example` use 50-message blocks, a 5-message partial minimum,
and a 3-day maximum age. Activation-point initialization and initial backfill are retried after
transient storage errors; an unexpected worker task failure is reported in the bot log.

Only new rows with explicit provenance can be candidates: incoming text, incoming captions, incoming
voice transcripts, and final contextual assistant answers. Existing rows are migrated as
`legacy/legacy_unclassified` and intentionally never become eligible. Recognition results, summaries,
profiles, status messages, memory blocks, memes, errors, and generated context remain ineligible.

Points are chat-scoped and have no monetary value. They cannot be bought, transferred, withdrawn, or
exchanged for money or any real-world value.

After the first new award, the bot creates a `Топ балансов` message and tries to pin it. Each later
award, slot settlement, or slot refund edits that same message and re-pins it. If it was deleted, the bot creates a replacement. The bot
needs Telegram permission to pin messages; without it, it still keeps and updates the leaderboard message.

## Virtual Casino Slot

`/casino` and an explicitly addressed request such as `Реле, прокрути слот` use one Telegram `🎰`
Dice animation. The fixed stake is 10 virtual points. Gross payouts are 0 for no match, 5 for one pair,
50 for three equal non-seven symbols, and 250 for `seven/seven/seven`; the corresponding net outcomes
after stake are -10, -5, +40, and +240. The v1 return-to-player is 90.625%.

The mapping is the versioned community contract `telegram_slots_base4_v1`. Telegram does not formally
document it, so every spin stores that rules version; jackpot is only `Dice.value=64`. If Telegram may
have shown Dice but the bot has no committed SQLite settlement, the spin is void and the stake is
refunded. This intentionally does not promise exactly-once Telegram Dice delivery. The casino has no
real money, payments, purchases, transfers, arbitrary stakes, or withdrawals.

`/casino bot` and an explicitly addressed request with a self-marker such as `Реле, крути себе слот`
use the bot's own chat balance. The bot identity is always `id:<Telegram bot id>`; its current display
name in leaderboards is the first configured chat alias, then its Telegram name. After a non-shadow
autonomous award, the bot records one durable automatic casino decision with the configured 25% chance;
retries never resample it. A missing balance skips that decision.

After a trusted Dice response, settlement is attempted up to three times with 0.25/0.5-second
delays; retries reuse that response and never send another Dice. An already committed payout is
replayed without a second award. If manual settlement remains unconfirmed, the bot sends a warning;
any stake still reserved in a pending spin is refunded at the next bot startup. Cancellation
during settlement also attempts an idempotent refund.

## Bot Aliases

Any chat participant can configure names that address the bot without using its Telegram username:

```text
/alias add Реле, Релейка
/alias list
/alias remove Реле
```

Aliases are local to a chat. The bot recognizes them in ordinary text and image/video captions,
including a single insertion, deletion, or replacement for names whose normalized token length is at
least five characters. A multi-token alias allows one typo in total, not one typo per word. Short
aliases require an exact match.

Allowing every participant in an allowed chat to manage aliases is the accepted current policy. An
optional admin-only toggle may be added in the future, but no such setting or restriction exists now.

## Addressed Actions

After a confirmed `@username`, Telegram text mention, or configured alias, the bot routes the
cleaned request to one safe built-in action. For example:

```text
Реле, сделай саммари за 6 часов
Релейка, найди в Википедии Ada Lovelace
@relay_bot распознай эту картинку
```

The router accepts only a fixed action set: contextual question, summary, Wikipedia search, image
recognition, meme, video recognition, replied voice/audio transcription, profile display, and the
explicit virtual casino slot request.
Slash commands and channel posts are not routed. For image/video/meme actions, a matching reply has
priority, then media in the addressed message, and only then the latest indexed media.

If a routed Wikipedia intent has no search query, the bot asks what to search instead of silently
falling back to a contextual question. Requests to joke remain normal questions, not meme commands.
A surprise meme has an explicit 2% chance only after a valid `question` route, an explicit joke
request, and an image in the current or replied message; router failures and the latest unrelated
chat image cannot trigger it.

## Operation Results And Persistence

Summary, question, Wikipedia, image, video, and profile operations produce structured terminal
outcomes for logs: `succeeded`, `rejected`, `partial`, or `failed`, with a stable reason and no raw
query or transcript in the operation event. Slash and naturally routed calls use the same operation
logic.

Wikipedia, image, Telegram video, and YouTube results use best-effort context persistence. The bot
shows `Saved for summaries.` only after the SQLite write succeeds. If generation succeeded but the
write fails, the useful result is still sent with a safe warning and the operation is recorded as
partial; private database error details are not shown. A video cache write failure is handled the
same way and does not discard an already generated recognition result.

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

`/wiki` returns short article extracts with source links and attempts to save successful results as normal stored messages, so future `/summary` and `/question` calls can use them as chat context when the write succeeds. This is not a full web search engine; it only uses the selected Wikipedia language edition.

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

For outgoing `/voice` and `/ask voice` replies, use the separate
[voice-generation setup](#voice-generation-feature-06) above. It includes the ready
Anub’арак and Cartman presets and is controlled by `TTS_ENABLED`.
The instructions below concern incoming audio transcription with Whisper.

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
- `/image` and `/ocr` results are saved as normal stored messages when the SQLite write succeeds, so future `/summary` and `/question` calls can use them. `/meme` does not save generated images in SQLite.

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
- `/video <YouTube URL>` downloads one on-demand YouTube video with `yt-dlp` and runs the same frame and optional audio analysis. Playlists, channels, live/upcoming streams, and videos with unknown duration are rejected. YouTube videos use `MAX_VIDEO_SIZE_MB` and `MAX_VIDEO_SECONDS` and are deleted after processing.
- You can also reply to a YouTube link with `/video`.
- `/vocr` is an alias for `/video`.
- Videos over `MAX_VIDEO_SIZE_MB` are rejected before recognition. If `TELEGRAM_DOWNLOAD_LIMIT_MB` is positive, it is also used as an early cutoff; otherwise the bot attempts the download and reports Telegram's real `file is too big` response if it happens.
- The bot downloads the video, extracts key frames with `ffmpeg`, sends those frames to `VIDEO_RECOGNITION_MODEL`, optionally extracts/transcribes the audio track when `VIDEO_TRANSCRIBE_AUDIO=true`, unloads the model after the task, and deletes temporary files.
- Repeated `/video` calls for the same Telegram message and settings use a SQLite cache instead of rerunning `ffmpeg` and Ollama. YouTube downloads are not cached in v1.
- The result is saved as a normal stored message when the SQLite write succeeds, so future `/summary` and `/question` calls can use it.

YouTube availability depends on the hosting network and upstream access requirements. The current
downloader does not configure cookies or authenticated browser sessions. A sign-in/anti-bot error
can prevent downloads even when the URL is valid; IP blocking is not established just from that
error. Cookie/session support and TikTok/Instagram adapters are future work in the
[social-video design](relay-assistant-bot-doc/Features/09%20Social%20Video%20Recognition.md).
On timeout or cancellation, the downloader requests a stop and removes abandoned files after the
download thread finishes, including a late successful download; it does not remove files while
that thread is still writing.

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
# Keep Gemma for questions, but use a small model for addressed-action routing.
QUESTION_MODEL=gemma3:12b
INTENT_ROUTER_MODEL=qwen2.5:1.5b
COMPARE_MODELS=
```

The recommended `.env.ollama.example` documents the maintainer's stable local model choices:

- `llama3.1:8b` for the safest text summaries/questions baseline.

`INTENT_ROUTER_MODEL` is only used to classify explicitly addressed natural-language requests. It defaults to
`QUESTION_MODEL`, then `OLLAMA_MODEL`; set it to a smaller installed text model to avoid loading Gemma before
every routed action. A vision model such as the configured OCR model can also route text if it supports ordinary
text chat, but a small text-only model usually starts faster and uses less VRAM.
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

## Development Roadmap

The [feature roadmap](relay-assistant-bot-doc/00%20Feature%20Roadmap.md) separates implemented
behavior from future designs: voice generation, chat search/source links, document understanding,
YouTube/TikTok/Instagram ingestion, reactions, proactive messages, and product onboarding/GUI with
optional local compute. Draft commands and settings in those notes are not current bot capabilities.
The [product vision](relay-assistant-bot-doc/01%20Product%20Vision.md) is community-first: an enjoyable
AI participant with useful skills, growing into a configurable builder of chat personalities,
voices, capabilities, and introductory prompts; a formal work profile is one possible preset.
The [pre-merge stabilization notes](relay-assistant-bot-doc/Features/05.5%20Pre-Merge%20Stabilization.md)
describe the current reliability fixes and regression checks.
