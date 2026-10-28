# Feature 06: Voice Message Generation

## Статус и границы

Runtime MVP реализован 2026-10-12: отдельный production runner CosyVoice 3, `/voice`, `/ask voice`, `/voice_type`, package registry, chat-scoped SQLite selection, supervision/cleanup и own-bot transcription guard. Релизная ветка — `release/bot-voice-presets` (2026-10-28). TTS по умолчанию выключен. Готовые bundled presets — `anubarak` и `cartman`; их WAV и transcript включены в пакет. Runtime и setup не используют локальные эксперименты: `experiments/` исключён из индекса Git и игнорируется. Изолированные tests не заменяют реальные GPU/listening/Telegram smoke checks: эти проверки ещё предстоят.

## Запуск и отключение

Подробная инструкция с диагностикой: [README: Voice generation](../../README.md#voice-generation-feature-06).

1. Настроить обычный текстовый бот по README: Python 3.11+, `./run.sh install`,
   `TELEGRAM_BOT_TOKEN` и LLM в `.env`. Существующий `.env` не заменять целиком.
2. Для TTS нужны Linux, NVIDIA GPU и драйвер с поддержкой CUDA 12.1, `uv`, Git,
   C/C++ build tools и ffmpeg/ffprobe с `libopus` и фильтром `rubberband`.
   CPU/macOS TTS fallback в этом setup не предусмотрен.
3. Из корня репозитория выполнить `./run.sh install-tts-cosyvoice3`.
   Установщик создаёт отдельную `.venv-cosyvoice3` на Python 3.10, скачивает upstream
   и около 5 GB модели в `tts/cosyvoice3/`. При старте бота скачиваний нет.
4. В `.env` установить:

```dotenv
TTS_ENABLED=true
TTS_PROVIDER=cosyvoice3
TTS_DEFAULT_VOICE=anubarak
TTS_PYTHON_PATH=.venv-cosyvoice3/bin/python
TTS_COSYVOICE_UPSTREAM_DIR=tts/cosyvoice3/upstream
TTS_MODEL_DIR=tts/cosyvoice3/models/Fun-CosyVoice3-0.5B-2512
# Для локального Ollama перед TTS освобождать модель:
OLLAMA_UNLOAD_AFTER_TASK=true
```

Остальные лимиты и пути — в `.env.example`. Если пути установки нестандартные,
экспортировать те же `TTS_PYTHON_PATH`, `TTS_COSYVOICE_UPSTREAM_DIR`, `TTS_MODEL_DIR`
перед запуском installer: сам shell-скрипт `.env` не читает.

5. Выполнить `./run.sh doctor`, затем запустить или перезапустить бот через
   `./run.sh start`. В doctor ожидается сообщение о доступности CosyVoice runner/CUDA
   и ffmpeg libopus/rubberband. Эта проверка не синтезирует речь.
6. В разрешённом чате проверить `/voice_type anubarak`, `/voice Привет!`, затем
   `/voice_type cartman` и `/voice Привет!`. `/ask voice 24h Что обсуждали?`
   должен прислать текст, затем voice reply. `/voice_type reset` возвращает default.

**Отключение:** поставить `TTS_ENABLED=false` и перезапустить бот. Проверки TTS
окружения/CUDA/ffmpeg не запускаются, `/voice` и `/ask voice` сообщают о недоступности
озвучки до model work. Обычные `/ask` и `/question` работают. Сохранённый голос чата
остаётся в SQLite, `/voice_type` доступен для настройки. Whisper независимо
управляется `TRANSCRIBE_VOICE`; для генерации речи он не нужен.

## Проверки релизной подготовки — 2026-10-28

- `compileall src`, полный offline suite **206 tests**, `git diff --check` и
  `bash -n run.sh scripts/install_tts_cosyvoice3.sh` прошли.
- Новый handler test проверяет `TTS_ENABLED=false`: TTS availability check, synthesis
  и voice-mode LLM не запускаются; обычный `/ask` и chat voice selection работают.
- Wheel собран и установлен в отдельный временный каталог. Из внешнего CWD
  обнаружены ровно два bundled references — `anubarak` и `cartman`; импорт registry
  и synthesizer не загружает torch/CosyVoice, экспериментов в wheel нет.
- Реальный CPU ffmpeg encode установленных references прошёл: Ануб’арак —
  OGG/Opus mono 48 kHz, 10.497 s, atempo + rubberband; Картман — 11.406 s,
  без effect filters. Это проверка упаковки/кодирования, не генерации или качества голоса.
- Runtime и installer не ссылаются на `experiments/`. Installer/download, реальные
  GPU synthesis и Telegram playback в этой сессии не запускались.

## История реализации

Проверено в implementation-сессии: `compileall src`, полный `unittest discover` (145 tests, включая 29 новых), `git diff --check`, shell syntax setup scripts, wheel build и installed-package discovery из внешнего CWD. Отдельно проверена реальная ffmpeg-цепочка на оригинальном reference: OGG/Opus, 10.497 s после atempo/rubberband. Это проверка encoding/filter availability, не качества синтеза или Telegram playback. Model installer, bot startup, GPU generation и Telegram smoke не запускались; `.env` и `data/` не читались.

Следующая фича после [[05.5 Pre-Merge Stabilization]]. Product source of truth: [[01 Product Vision]]. Готовый handoff: [06 Voice Message Generation Implementation Prompt.md](../Prompts/06%20Voice%20Message%20Generation%20Implementation%20Prompt.md).

Цель: бот озвучивает supplied text и собственные contextual answers, имеет узнаваемый голос и позволяет каждому чату выбрать установленный preset. Реализация ограничена явными slash-командами. Natural-language TTS routing, streaming, автоматическая озвучка всех ответов, загрузка голосов через Telegram и редактор voice library не входят в этот этап.

## Принятые решения

- Provider: **CosyVoice 3**, отдельное Python-окружение; Qwen остаётся экспериментом.
- Default preset: **Ануб’арак**, zero-shot reference, затем постобработка до **85% скорости и −1 semitone с сохранением формант**.
- `/voice <text>` отправляет voice reply с произнесённым supplied text.
- `/ask` — новый alias существующего `/question`; `/question` сохраняет текущий контракт.
- В `/ask voice <question>` слово `voice` — флаг voice output, не имя preset.
- `/ask voice` возвращает generated answer как текст и voice. Обе копии — replies на исходную команду. Это не transcript и не результат Whisper.
- Default voice можно выбрать отдельно для каждого чата через `/voice_type`.
- Bundled samples хранятся в Git; дополнительные samples конкретной установки — в Git-ignored каталоге.
- Второй bundled voice — `cartman` / Картман: готовые `reference.wav` и `preset.json` внутри пакета; model speed 1.0 и никакой tempo/pitch post-processing.
- Generated voice этого бота не запускает Whisper, transcript formatter или автоматическую recognition-проверку качества.

Следующие defaults — инженерные предложения этого плана: любому участнику allowed chat доступна смена voice (как aliases), лимит текста 2 000 символов, аудио 180 секунд, execution timeout 180 секунд. Это не измеренные гарантии модели; значения должны быть конфигурируемыми и проверяться на baseline.

## Команды и UX

| Команда | Поведение |
| --- | --- |
| `/voice <text>` | Озвучить текст выбранным голосом чата; отправить playable Telegram voice reply. |
| `/ask [period] <question>` | Обычный text answer через существующую contextual question operation. |
| `/ask voice [period] <question>` | Получить один contextual answer, отправить текст, затем озвучить тот же answer. |
| `/voice_type` | Показать effective preset, источник выбора (chat/default), список доступных IDs и usage. |
| `/voice_type <preset_id>` | Проверить preset и сохранить выбор для текущего чата. |
| `/voice_type reset` | Удалить chat override и вернуться к configured bot default. |

- Поддержать стандартные `/command@bot_username` через aiogram admission.
- В `/ask` зарезервирован только первый argument `voice`; в `/question` это слово остаётся обычным текстом вопроса. Для буквального вопроса, начинающегося с `voice`, можно использовать `/question`.
- Optional period следует после voice flag; отсутствие period использует текущий `DEFAULT_SUMMARY_PERIOD`. Не терять и не повторять question text при разборе.
- Пустой supplied text/question и unknown preset отклонять до model work; unknown ID не меняет предыдущий выбор.
- `/voice` не интерпретирует текст как settings: для выбора есть отдельная `/voice_type`.
- Текстовый answer отправляется первым. Все его split parts и voice message должны быть replies на command message, включая статус, если он используется как первый edited text reply.
- Для `/ask voice` всегда использовать отдельные text и voice messages, не полагаться на меньший caption limit.
- Озвучивать только body answer, без заголовка периода, статусов и persistence warnings. Удалять presentation markup для speech без LLM-переписывания смысла; показываемый answer сохраняется полным.
- Обычный `/ask` сохраняет поведение `/question`; reply-specific delivery применяется к voice mode.

## Presets и файлы

```text
src/tg_summary_bot/voice_presets/    # tracked package data
  anubarak/
    preset.json
    reference.wav
  cartman/
    preset.json
    reference.wav
voices/local/                     # весь каталог Git-ignored
  <local_voice>/
    preset.json
    reference.wav
```

Каждый preset содержит version, stable ID, display name, reference filename, точный reference transcript, language и post-processing settings: speed, pitch semitones, preserve formants. Model/upstream/executable paths — настройки установки, а не команды из preset.

- Bundled references включить в setuptools package data; загрузка должна работать после установки пакета, а не только из repository CWD.
- Добавить `/voices/local/` в `.gitignore`. Local root configurable; `voices/local/` — default относительно корня установки, разрешаемый в абсолютный путь.
- Допустимые preset IDs ограничить простой схемой (например, lowercase ASCII letters/digits/underscore/hyphen). `reset` зарезервирован.
- Проверять JSON types/version, непустой transcript, supported language, bounded post-processing values и наличие reference.
- Reference должен быть непустым PCM WAV с частотой от **16 kHz** и длительностью
  **до 30 секунд включительно**. Несовместимый preset исключается из registry до
  synthesis; в startup/doctor diagnostics выводится `reference_sample_rate_below_16000_hz`
  или `reference_longer_than_30_seconds`. После подготовки совместимого WAV перезапустить бот.
- Reference разрешать только внутри директории preset, включая проверку resolved symlink path. Не принимать произвольные user paths через команды.
- Duplicate IDs между bundled/local presets не должны молча переопределять голос. Broken/duplicate local presets исключать с диагностикой; валидные bundled voices остаются доступны. Невалидный configured default делает TTS недоступным с понятной диагностикой.
- Изменения library подхватываются после restart; hot reload не нужен для MVP.
- Models, environments, upstream code, generated WAV/OGG и local samples не коммитить.

### Ануб’арак

Готовый reference: `src/tg_summary_bot/voice_presets/anubarak/reference.wav` (PCM16 mono 24 kHz, 8.93 s). Исходная реплика — `nerubiancryptlordpissed3.ogg`.

Reference transcript: «Мы, нежить, народ работящий. Такая уж наша тяжкая доля.»

Оригинальный sample и transcript подтверждены владельцем при bundling. Для новой установки достаточно файлов пакета; искать исходник в локальных экспериментах или Downloads не требуется.

Последовательность: zero-shot CosyVoice с model speed 1.0 → `atempo=0.85` → pitch −1 semitone с formants preserved → OGG/Opus. Не заменять post-processing модельным speed=0.85 без listening comparison. Production encoder использует ffmpeg `rubberband`; проверять наличие фильтра. Не подменять выбранный эффект молча.

### Картман

Готовые файлы: `src/tg_summary_bot/voice_presets/cartman/reference.wav` и `preset.json`. По явному выбору владельца использованы исходник `cartman_002_ai_mdx_extra.wav` и неизменённый transcript `cartman_002_raw.txt`. Исходный float WAV (44.1 kHz stereo, 11.4 s) преобразован в PCM16 mono 24 kHz для registry/package compatibility, без normalization или tempo/pitch эффектов. Transcript остаётся ASR draft; независимое listening-подтверждение в implementation-сессии не выполнялось.

Preset ID `cartman`, display name `Картман`, language `ru`, speed 1.0, pitch 0. Generated speech не проходит даже neutral atempo/rubberband: фильтры полностью пропускаются, остаётся только Telegram OGG/Opus encoding. Выбор: `/voice_type cartman`; default Anub’арак не изменён.

После добавления Картмана: `compileall src`, полный suite (148 tests), `git diff --check`, wheel build и discovery установленного Cartman preset/reference из внешнего CWD прошли. Новые tests проверяют neutral settings, отсутствие любых audio effect filters в encoding command и выбор Картмана для чата. GPU synthesis/listening/Telegram playback остаются manual checks.

### Runtime-стабилизация после review логов и Opik

Исправлена причина больших profile batches: limit учитывает полный rendered input с headers, а не только raw text. Defaults: 2000 chars / 8 messages, один полезный факт, отдельный profile client с Ollama output budget 256 tokens, request timeout 60 s. Refresh делает один batch, coalesces задачи по чату, ждёт foreground grace 2 s, пропускает busy GPU и использует cooldown 120 s. Invalid JSON/timeouts сохраняют checkpoint; новая nullable колонка `chat_profile_state.processed_message_id` позволяет безопасно разбивать одинаковые timestamps, не меняя legacy checkpoint.

Лимит одного profile fact применяется после проверки кандидатов: пустой, слишком
короткий, low-confidence или не подтверждённый источниками первый объект не скрывает
следующий валидный. Сохраняется не более первого валидного факта, после чего batch
получает checkpoint. Обычная memory compression по-прежнему может сохранять несколько фактов.

Slash command traces охватывают весь voice operation, включая GPU wait, LLM/unload, text delivery, speech synthesis/encoding/validation/upload/persistence. Root span нужен для правильного nesting в используемом SDK; иначе первый parentless child закрывал trace и оставлял остальные stages orphaned. Handled partial/failure outcomes отмечаются error в Opik без повторной ошибки Telegram handler. Profile refresh trace отсоединён от foreground trace. Speech stage metadata не содержит исходного текста, файлов или raw provider exceptions.

Leaderboard not-modified edits теперь quiet success. Подтверждённый недостаток pin rights — best effort с cooldown 10 минут, без бесконечного outbox retry; balances продолжают редактироваться. Transient errors остаются retryable.

Live ingestion проверен через `tests/voice_trace_smoke.py` с synthetic inputs/fake models/ffmpeg/Telegram и temporary SQLite. В Opik подтверждены все ожидаемые spans: direct voice — 7, contextual voice answer — 13 (включая один synthetic LLM call), upload failure — 6 с safe root/stage errors. Это не real GPU/Telegram latency smoke. Настройки и migration подробно описаны в README; реальные изменения вступают в силу после restart текущего bot process.

Финальные проверки стабилизации: `compileall src`, 162 offline tests и `git diff --check` прошли. Opik healthcheck и synthetic live ingestion/coverage подтверждены; production bot process не перезапускался автоматически, реальное улучшение latency нужно измерить после restart.

## Chat-scoped selection

Идемпотентно добавить таблицу `chat_voice_settings`: `chat_id` primary key, `voice_id`, `updated_at`. Нужны get/set/reset методы `MessageStore`; изменения используют текущий write lock. В SQLite хранится только preset ID, не raw audio.

Resolution: valid chat override → configured bot default (`anubarak`). Отсутствие override не ошибка. Если ранее выбранный local preset удалён, использовать valid bot default и уведомить пользователя; invalid bot default не вызывает silent substitution.

Preset resolve делать один раз при admission операции: изменение настроек параллельным запросом не должно менять голос уже выполняемого answer. Все команды проверяют текущий `ALLOWED_CHAT_IDS`. Proposed policy: любой участник allowed chat может читать/менять выбор. Admin-only toggle и per-chat private library не добавлять: local presets доступны allowed chats этой установки.

## Runtime architecture

Рекомендуемые focused modules:

- `voice_presets.py`: registry, validation, bundled/local discovery, immutable preset data.
- `speech_synthesizer.py`: async contract, subprocess lifecycle, limits, metadata, temporary work и conversion orchestration.
- Dedicated CosyVoice runner вне обязательных bot imports: конфигурируемые model/upstream paths, вход через JSON file/stdin, WAV output и structured result. Можно разместить в `scripts/tts/`; deployment/setup должны включать runner.
- `bot.py`: command parsing/admission, reuse question operation, reply delivery, logged outcomes.

Production использует только `cosyvoice_runner.py`; локальные experiment CLI не являются зависимостями. Их файлы остаются на диске под `.gitignore`.

CosyVoice работает в отдельной `.venv-cosyvoice3` (текущий experiment — Python 3.10); bot остаётся Python >=3.11. Heavy ML dependencies не входят в mandatory package requirements. Для MVP — один завершённый subprocess на запрос, без постоянно resident TTS worker.

```text
/ask voice
  → access/arguments/preset/dependency validation
  → existing question generation under gpu_lock
  → unload question/memory models → unlock
  → send text reply; best-effort canonical persistence
  → acquire same gpu_lock → CosyVoice subprocess → await actual exit → unlock
  → CPU ffmpeg speed/pitch/Opus → validate duration/file
  → Telegram voice reply → cleanup
```

`/voice` пропускает question generation. Использовать `asyncio.create_subprocess_exec`, без shell interpolation, bounded captured diagnostics и без spoken text в argv/logging. При timeout/cancellation завершить процесс (terminate, затем kill при необходимости), дождаться выхода/закрытия потоков и только потом отпустить GPU lock и удалить файлы. Аналогично дожидаться ffmpeg, чтобы не было late writes. Не добавлять второй GPU lock или менять polling concurrency.

GPU serialization сама по себе не освобождает VRAM resident Ollama/Whisper models. Проверить существующие unload/keep-alive contracts и поведение transcriber; не заявлять, что lock гарантирует отсутствие OOM. Не завершать внешние процессы или другие Ollama models произвольно. Реальный baseline должен включать последовательность LLM → TTS при настройках установки.

Результат synthesizer содержит audio path, duration, provider/model/preset metadata. Temporary directory уникален для операции и сохраняется до завершения Telegram upload. Выход — OGG/Opus, отправка через voice API и `FSInputFile`, не WAV document.

## Limits, failures и persistence

- Proposed defaults: max speech input 2 000 chars, max output 180 sec, synthesis execution timeout 180 sec. Timeout model subprocess начинается после получения GPU lock; это не обещание общего response time. Encoding/upload также должны иметь bounded timeouts.
- `/voice` over-limit отклоняется до synthesis. Oversized generated `/ask voice` answer отправить целиком как текст и сообщить, что voice skipped; не truncation и не скрытое summarization.
- Feature disabled/dependencies unavailable сообщаются до запуска LLM в voice mode. Обычные `/ask` и `/question` продолжают работать.
- Если LLM generation failed, не запускать TTS. Если synthesis/encoding/upload failed после text success, сохранить текст и отправить короткое partial-success уведомление.
- Ошибка SQLite не отменяет доставку готового результата; использовать текущую best-effort persistence policy и безопасное предупреждение. Итог отражает все причины partial success, не теряет persistence failure при TTS failure.
- `/ask voice`: existing `assistant/assistant_answer` сохраняется один раз; audio copy не создаёт второй eligible source и не запускает duplicate joke award.
- `/voice`: после успешной доставки сохранить supplied spoken text отдельно под ID voice message, `origin=generated`, `kind=tts_text`, с reply linkage. Это не `incoming/voice_transcript` и не eligible joke source. Не ослаблять закрытый provenance allowlist.
- Success сообщает только после подтверждённой доставки; cleanup в finally для всех terminal paths.

## Whisper isolation

Generated text известен до synthesis: Whisper и transcript formatter не вызываются ни для `/voice`, ни для `/ask voice`.

Telegram обычно не отдаёт собственный outgoing message как incoming update, но это не единственная защита. В shared audio transcription entry point проверять sender ID против текущего bot ID **до download/status/model work**. Автоматическая обработка own-bot voice пропускается; явный `/transcribe` reply на own-bot voice даёт понятное сообщение без распознавания. Это покрывает slash и routed transcribe через общий entry point.

Не отключать Whisper глобально, не менять transcription участников и не блокировать все `is_bot` messages без отдельной необходимости. Копия аудио, заново загруженная человеком, считается новым incoming audio; fingerprint detection для этого не входит в MVP.

## Configuration и setup

Предлагаемые settings (имена зафиксировать при реализации):

- `TTS_ENABLED=false`, `TTS_PROVIDER=cosyvoice3`, `TTS_DEFAULT_VOICE=anubarak`.
- `TTS_LOCAL_VOICES_DIR`, `TTS_OUTPUT_DIR` (runtime temporary root).
- `TTS_PYTHON_PATH`, `TTS_COSYVOICE_UPSTREAM_DIR`, `TTS_MODEL_DIR`.
- `TTS_MAX_TEXT_CHARS=2000`, `TTS_MAX_AUDIO_SECONDS=180`, `TTS_TIMEOUT_SECONDS=180`.

Обновить `Settings`, `load_settings()`, `.env.example`, README, `/help` и doctor. При disabled TTS startup не импортирует CosyVoice и не требует его окружения. При enabled, но broken setup, отметить TTS unavailable, сохранить text bot functionality и дать точную configuration diagnosis без secrets.

Existing `install-voice`/`install-voice-cuda` относятся к Whisper transcription; не менять их смысл. Добавить отдельный documented `install-tts-cosyvoice3` helper с version-pinned отдельным окружением и model setup. Использовать проверенные versions из experiment README как baseline, документировать model/upstream revision и проверку CUDA, ffmpeg/libopus/rubberband. Не скачивать несколько гигабайт автоматически при startup или unit tests.

## Порядок реализации

1. **Foundation:** preset schema/registry, bundled Anub’арак sample, ignored local root, package data, config, chat selection storage и `/voice_type`.
2. **TTS pipeline:** dedicated runner, async subprocess supervision, same GPU lock, post-processing, encoding, setup/doctor checks.
3. **Direct speech:** `/voice`, voice reply, generated provenance, limits/failure handling и cleanup.
4. **Voice answers:** `/ask` alias/parser, reuse existing contextual generation, text-first replies, TTS from one answer, canonical persistence и partial outcomes.
5. **Isolation/verification:** own-bot transcription guard, automated checks, real listening/Telegram playback smoke test, актуализация runtime docs.

Незавершённые части обозначать явно; не считать missing sample, manual playback или GPU smoke test выполненными только на основании mocks. Commit/PR — только по отдельному запросу.

## Критерии приёмки и tests

- `/voice` возвращает playable voice reply; `/ask voice` — text и voice replies на command; синтезируется один generated answer без повторного LLM call.
- `/ask` сохраняет question behavior, optional period/default и command suffix; `/question` не меняет parser contract.
- Chat selection различается между чатами, survives restart, reset работает; invalid ID не перезаписывает настройку, missing local preset даёт documented fallback.
- Registry проверен на malformed JSON, duplicate IDs, path traversal/symlinks, missing reference и package-data discovery вне repo CWD.
- Fake runner/ffmpeg покрывают success, no/invalid output, too-long audio, timeout, cancellation, kill/wait before unlock и no late writes/temporary leaks.
- Lock sequencing тестирует LLM unload → release → TTS acquire и исключает одновременную local GPU работу.
- Mocked Telegram tests покрывают split replies, upload failure, persistence failure, combined partial failure и oversized generated answer fallback.
- Temporary SQLite tests подтверждают idempotent schema и один canonical eligible assistant source; direct TTS text ineligible.
- Whisper mocks подтверждают zero recognition/formatting calls для generated voice, own-bot automatic update и slash/routed `/transcribe` reply; human voice transcription сохранена.
- Disabled/unavailable TTS не ломает startup/text features и не импортирует ML dependencies.

Проверки: `.venv/bin/python -m compileall src`, `.venv/bin/python -m unittest discover -s tests -v`, `git diff --check`. Unit suite не требует GPU, network, model downloads, `.env` или `data/`.

Manual smoke после настройки: прослушать оба bundled references, короткую и длинную русские генерации; проверить speed/pitch Ануб’арака и отсутствие этих эффектов у Картмана, реальные Telegram voice replies, время cold/warm run и VRAM при LLM → TTS. Не использовать Whisper для этой production-проверки. Результаты и ограничения записать честно.
