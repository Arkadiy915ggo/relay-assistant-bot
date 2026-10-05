# Feature 12: Product UX, Onboarding and PM Portfolio

## Статус и цель

Базовый продуктовый дизайн, 2026-10-03. Проект планируется как Product Manager portfolio: важны понятная пользовательская ценность, качественный first-run experience, проверенные решения и доказуемые результаты, а не только список технических фичей.

Продуктовая рамка: [[01 Product Vision]]. Первичная аудитория — владельцы/участники неформальных комьюнити. Долгосрочный продукт — конструктор AI-участника: навыки, характер, голоса, степень инициативности и редактируемые вводные промпты. Формальный рабочий профиль — один из presets, а не primary positioning.

GUI, desktop build, universal installer и отдельный local-compute package пока не реализованы. Existing CLI `run.sh`, doctor, API/Ollama clients и configuration остаются текущей базой.

## Аудитория и задачи для проверки

Первичные сегменты-гипотезы:

- Владелец/участник дружеского или тематического комьюнити: хочет мемы, шутки, голоса, игровые механики и узнаваемого помощника, который умеет быть полезным.
- Организатор комьюнити: хочет вовлечённость, summaries после отсутствия, поиск источников и будущую помощь со встречами.
- Организатор рабочей группы: хочет более формальный профиль, поиск/summary/documents и возможность выключить игровые/social навыки.
- Local-AI enthusiast: хочет использовать свои мощности и выбирать модели.

Исследовать, кому продукт полезнее, кто устанавливает/поддерживает бота, какие системы/языки нужны и где onboarding сейчас ломается. Не пытаться одинаково обслужить все сегменты первым MVP.

## Целевой first-run journey

```text
landing / README → choose API-only or local compute → install/start app
                 → BotFather token → provider setup → connection check
                 → add bot to chat + explain Group Privacy → discover/allow chat
                 → choose community/work/custom profile + skills → preview persona
                 → first enjoyable/useful interaction → adjust capabilities
```

- Каждая ступень показывает состояние, следующий шаг и понятную recoverable error.
- Token validation и provider test не должны требовать понимания Python tracebacks.
- Различать «бот работает», «нет разрешённых чатов», «нет сохранённой истории» и «модель недоступна».
- Показывать невозможность читать историю до добавления бота до первой попытки summary.
- Optional OCR/video/Whisper/TTS включать после успешного baseline text flow.
- Дать checklist действий в Telegram с примерами сообщений для первого полезного результата.

## Конструктор: persona, навыки и chat profiles

Настройки профиля применяются к конкретному чату; backend credentials и доступность моделей относятся к deployment. Не заставлять пользователя выбирать API model для каждого изменения характера.

- Presets: community hangout, community organizer, formal work и custom.
- Persona: имя/описание роли, формальность, юмор, длина ответов, языки и доступные voice presets.
- Skills: независимые toggles для каждой реализованной capability, включая memes, casino, summaries, voice и search.
- Social policy: addressed-only/разрешённая инициатива, reaction policy, frequency и quiet hours.
- Configured profile применяется одинаково к slash, addressed и background actions; выключенный skill не вызывается через обходной entry point.
- Templates являются стартовой точкой. Пользователь может оставить шутки в формальном профиле или отключить казино в неформальном.

Поэтапный MVP: сначала небольшая typed chat configuration и выбор preset; затем persona editor/preview и versioned export/import без secrets; сложный visual builder или сторонние plugins — только после проверки этого UX.

## Редактирование вводных промптов

- Simple editor с полями «кто бот / как общается / особенности комьюнити», без обязательного знания prompt engineering.
- Advanced editor для approved style/feature prompt overrides: область применения, default, версия, reset и rollback видны пользователю.
- Preview запускается на supplied или демонстрационном input с теми же capabilities; сравнение до/после не отправляет сообщение в реальный чат автоматически.
- Precedence определить явно: base contracts → saved chat persona → feature-specific style → текущий запрос/context. Chat messages не сохраняют новые overrides сами по себе.
- Поведенческие настройки вроде включения навыка, частоты сообщений и доступа задаются structured config, а не только текстом prompt.
- Parser/tool/access/storage contracts и casino economics остаются кодовыми инвариантами; редактирование промпта не меняет их.
- Edit permissions определить отдельно для chat profile и deployment config. Не менять текущую alias policy в рамках будущего дизайна.
- Экспорт profile содержит schema/preset/prompt version и настройки, но не API keys, cookies, private history или raw voice references.

## Deployment и упаковка

Предлагаемые этапы:

1. Улучшить existing CLI/doctor и documented API-only quick start.
2. Подготовить один воспроизводимый supported installation profile для внешнего пользователя; оценить Docker/local launcher по исследованию.
3. Local GUI/wizard для настройки и статуса.
4. При подтверждённом спросе — OS-specific desktop distribution с понятными обновлениями и recovery.

Не обещать universal one-click binary до проверки Windows/macOS/Linux, ffmpeg, SQLite paths, runtime updates и Telegram polling lifecycle. Выбрать первую supported platform и явно показать support matrix.

## Provider architecture и optional local compute

- Сохранить простое bot core: Telegram operations, storage и feature orchestration.
- Определить narrow capability interfaces: text LLM, speech-to-text, text-to-speech, vision, downloads/extraction.
- Уже существует `LLMClient` с OpenAI/Ollama; развивать эту границу постепенно, а не переписывать весь бот ради абстракций.
- OpenAI-compatible endpoint остаётся базовым hosted adapter. Другие APIs подключаются capability-by-capability после проверки конкретной потребности.
- Execution policy зависит от backend: remote API не требует локальной GPU, local CPU тоже не равен local GPU.
- Local GPU jobs используют shared coordinator; API-only installation должна работать без Torch/CUDA/Ollama/Whisper.
- Вынести ML integrations в optional extras/package или отдельный process/service только при реальной dependency isolation необходимости.
- Git submodule — возможный способ подключения local-compute кода, но не зафиксированное решение. Сравнить его с optional package и worker service по установке/версированию/поддержке.
- Каждая feature проверяет capability availability и даёт понятное объяснение при отключённом backend.

## GUI MVP

Предварительно local web UI/wizard или desktop wrapper; выбрать один после first-run исследования.

Минимальные экраны:

1. Setup: Telegram token, provider/endpoint/model, connectivity check.
2. Chats: обнаруженные chat IDs, allowlist, profile preset и capability toggles.
3. Character: persona/prompts, voice presets, formality/initiative и preview.
4. Capabilities: text, image/video, transcription, TTS; установлено/настроено/недоступно.
5. Status: polling, active jobs, queue, concise errors и restart control.
6. Configuration: save/apply, проверка изменений и masked export без secrets.

Local UI и runtime должны использовать один settings schema/source of truth. Предварительная настройка не должна разойтись с `.env` или незаметно запустить второй polling process. Live changes/restart semantics выбрать явно; local UI access по умолчанию ограничить локальной машиной.

## Документация и onboarding deliverables

- Короткий README: ценность, demo, current capabilities, первый supported setup.
- Guides по use cases: summary, question, chat search, documents, media и voice — только по мере реализации.
- Troubleshooting matrix: сообщение → причина/проверка → действие, включая Telegram privacy, access policy, provider errors и social download limitations.
- Capability/provider support matrix и требования CPU/GPU/FFmpeg.
- Update/backup/restore/uninstall guide с понятными data paths.
- Demo с вымышленным чатом, без приватной истории, токенов и личных voice references.
- Отдельный блок Known limitations вместо обещаний будущих команд как уже доступных.

## PM portfolio case study

Структура будущего кейса:

1. Problem discovery: исходные задачи, интервью/наблюдения, аудитория и ограничения Telegram.
2. Opportunity и hypotheses: какую проблему решаем и как поймём полезность.
3. Prioritization: ценность/сложность/зависимости, причины выбранного MVP и отложенных решений.
4. UX: user journey, onboarding prototype, errors/recovery и usability findings.
5. Delivery: итерации, quality gates, migration/reliability work и engineering trade-offs.
6. Outcomes: измеренные результаты, что не сработало, изменения решения и следующий эксперимент.

Не выдумывать engagement, время установки, отзывы или бизнес-эффект. Скриншоты, наблюдения и метрики снабжать условиями измерения; local TTS benchmark не выдавать за UX proof всего продукта.

## Критерии приёмки и метрики

- Внешний пользователь проходит выбранный supported setup без личной помощи автора и получает первый useful response.
- API-only путь не устанавливает локальный GPU stack.
- GUI даёт actionable error и не раскрывает secrets в export/logs.
- Restart/update не теряет сохранённые messages/settings и не запускает duplicate polling.
- Документация совпадает с реально доступными commands/settings.
- Пользователь меняет character/skill set для своего чата без редактирования кода; disabled capabilities не обходятся роутером или worker.
- Persona preview, rollback и различие chat profile/deployment settings понятны внешнему пользователю.

Метрики будущего исследования: setup completion, time-to-first-value, доля запросов поддержки по шагам, успешность ключевых use cases, optional capability adoption и повторное использование. Baseline и численные цели установить после первых usability sessions; не добавлять автоматическую telemetry только ради портфолио.

Для community-first профиля добавить оценки характера/уместности, распределение обращения к боту между участниками, настройку/сохранение profiles и opt-out инициатив. Полезный эффект включает удовольствие от общения и участие, а не только экономию рабочего времени; рост числа сообщений сам по себе не доказывает качество.

## Открытые решения

- Какой сегмент и OS первыми становятся supported?
- Self-hosted/local app или также managed hosting?
- GUI web или desktop и какую часть установки он автоматизирует?
- Как хранить secrets и обновлять optional dependencies без поломки рабочего профиля?
- Какие дополнительные API providers оправданы исследованием?
- Какую часть portfolio/demo делать публичной и на каком языке?
- Кто может менять persona/prompts конкретного чата и как восстановить default?
- Какие 2-3 presets достаточны для первого user test и где profile наследует instance defaults?
- Какая часть advanced prompt editor действительно нужна пользователям, а какие настройки лучше представить обычными полями?
