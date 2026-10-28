# Feature 06: Voice Message Generation — Implementation Prompt

Скопируй блок ниже в новую агентскую сессию. Specification: [06 Voice Message Generation.md](../Features/06%20Voice%20Message%20Generation.md). Это поручение на реализацию Feature 06, а не на весь roadmap.

```text
Реализуй Feature 06 Voice Message Generation по спецификации:
relay-assistant-bot-doc/Features/06 Voice Message Generation.md.

Сначала прочитай AGENTS.md и спецификацию, проверь git status --short и актуальную базу ветки. Сохрани незнакомые/неотслеживаемые user files: в planning-сессии был untracked experiments/cosyvoice3/testVoise/. Не удаляй и не коммить его автоматически. Не читай .env или data/. Изучи текущие bot.py, assistant.py, config.py, storage.py, observability.py, transcriber.py, doctor.py, pyproject.toml, run.sh, .gitignore, .env.example и experiment README/clone/pitch scripts в объёме этой фичи. Repo guide актуальнее старого overview skill, если они расходятся о tests.

Принятые продуктовые решения:
1. CosyVoice 3, zero-shot, отдельное Python-окружение (.venv-cosyvoice3; текущий experiment Python 3.10). Bot Python >=3.11, без обязательных ML dependencies.
2. Default Anub’арак: original reference «Мы, нежить, народ работящий. Такая уж наша тяжкая доля.», model speed 1.0, затем atempo=0.85 и pitch −1 semitone с formants preserved. Не заменять это model speed=0.85 молча.
3. /voice <text> — Telegram voice reply на команду.
4. /ask [period] <question> — alias existing /question. /ask voice [period] <question> — флаг voice output: один contextual answer, сначала text replies, затем voice reply на тот же command. Не повторять LLM generation. /question сохраняет текущий parser contract.
5. /voice_type показывает effective/current voice и available IDs; /voice_type <id> сохраняет chat-scoped default в SQLite; /voice_type reset удаляет override. Proposed permission policy: любой участник allowed chat, как aliases. Settings не доступны через LLM router.
6. Bundled preset.json/reference.wav находятся в src/tg_summary_bot/voice_presets/ и Git/package data. Local voices — configurable voices/local/, весь default root Git-ignored. Второй bundled sample владелец предоставит позже; не создавать broken placeholder.
7. Generated own-bot voice никогда не запускает Whisper или transcript formatter, включая explicit slash/routed /transcribe reply. Guard в shared transcription entry point до download/model work; human incoming transcription остаётся рабочей.

Перед bundling проверь точный Anub’арак audio reference и transcript. Кандидат из README: experiments/qwen3-tts/outputs/reference-anubarak-worker.wav; источник — $HOME/Downloads/Анубарак/nerubiancryptlordpissed3.ogg. В planning-сессии существование/содержимое не проверены. Если недоступен, спроси точный путь, продолжая независимую реализацию. Не бери generated audio вместо original reference и не ищи sample в private data/.

Выполни последовательными локальными этапами:
A. Preset registry/validation/package data + ignored local root + config + idempotent chat_voice_settings schema/get/set/reset + /voice_type.
B. Dedicated production CosyVoice runner с configurable paths, async supervision, bounded timeouts, WAV → speed/pitch → OGG/Opus. Не hardcode dependency на experiment CLI. Separate install-tts-cosyvoice3 setup; existing install-voice commands по-прежнему Whisper.
C. /voice admission, same shared gpu_lock, voice delivery/logging, cleanup, generated/tts_text persistence после delivery.
D. /ask parser/alias и voice mode через reused question operation. Сохрани memory/profile/identity context, model unload, split text и best-effort persistence. Не вызывай handlers из handlers и не создавай synthetic Message.
E. Own-bot transcription guard, meaningful tests, documentation/doctor updates и доступная verification.

Обязательные engineering contracts:
- asyncio subprocess exec без shell interpolation; текст через JSON input/file/stdin, не argv/logs. Preset reference paths confined to preset root, включая symlinks; no arbitrary executable commands from presets.
- Duplicate/broken local presets диагностируются и исключаются без silent overrides; configured default must be valid. Resolve selected immutable preset once per admitted operation; missing chat preset falls back to valid bot default с уведомлением.
- LLM generation/unload/release → text delivery → reacquire same gpu_lock for TTS. Timeout/cancellation terminate/kill и await real process exit до unlock/cleanup; await ffmpeg exit тоже. One subprocess per TTS request, no resident service/new GPU lock/polling concurrency changes.
- Проверь resident Ollama/Whisper VRAM и existing unload contracts; lock сериализует работы, но не гарантирует освобождение всей VRAM. Не убивай внешние процессы произвольно.
- Proposed configurable defaults: 2 000 speech chars, 180 sec audio, 180 sec synthesis execution timeout after lock acquisition. /voice over-limit reject before work; oversized generated answer delivered fully as text with voice-skipped notice, no silent truncation.
- /ask voice sends only answer body to TTS, not period/status/persistence warnings. Preserve textual answer and wording; no recognition or LLM rewrite for speech.
- assistant/assistant_answer сохраняется один раз; voice copy не второй eligible joke source. /voice stores generated/tts_text, not incoming/voice_transcript; preserve closed joke eligibility allowlist.
- Text success + TTS/encoding/upload failure → partial outcome и safe warning; persistence failure тоже partial, не отменяет готовый результат. Не потерять одну причину failure при другой. Cleanup temporary work after uploads complete.
- TTS defaults disabled. Disabled/broken optional setup не ломает обычный text bot startup; feature availability diagnosed до LLM voice request. Не читать secrets и не показывать unsafe provider exceptions пользователю.
- Preserve Russian prompts/response conventions. Update Settings/load_settings, .env.example, README, /help, doctor, run.sh, package data и feature status только по реально выполненной работе.

Tests должны быть meaningful, изолированными и без GPU/network/models:
- command parser/default period/voice flag/@username; text+audio direct reply IDs и split replies;
- per-chat selection/restart/reset/invalid and missing preset; package registry/duplicates/traversal/symlinks;
- subprocess timeout/cancel/kill/wait before GPU unlock, failure cleanup/no late writes;
- LLM unload/TTS lock sequencing, exactly one answer generation;
- audio upload/persistence/combined partial failures и long-answer text fallback;
- generated provenance и absence of duplicate eligible assistant context;
- zero Whisper/formatter calls for generated voice/own-bot transcribe, human voice unchanged;
- optional TTS startup without ML dependencies.

Выполни .venv/bin/python -m compileall src, .venv/bin/python -m unittest discover -s tests -v и git diff --check. Дополнительные targeted checks runner/package data по необходимости. Не скачивай большие models или не запускай bot автоматически ради tests. Реальный GPU/listening/Telegram smoke выполняй только при доступном явно настроенном окружении; otherwise перечисли точные remaining manual steps и не заявляй playback/качество проверенными по mocks.

Продолжай до завершения доступной реализации; задавай вопросы только по реальным blockers. Не делай commit, push, merge или PR без отдельного запроса. В финале перечисли изменённые файлы, выполненные проверки, настройку запуска, migrations, sample availability и честные ограничения/manual follow-ups.
```
