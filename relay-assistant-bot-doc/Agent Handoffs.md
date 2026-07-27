# Agent Handoffs

Каждый блок ниже можно отдать отдельному агенту. Перед началом агент должен прочитать `AGENTS.md`, нужную спецификацию и проверить `git status --short`. Не читать `.env` и `data/`.

Текущий статус: Feature 01, Feature 02 и Stage 02.5 реализованы. Feature 03 и Feature 04 не реализованы.

## Фича 1: алиасы (реализовано)

Документ: [[Features/01 Aliases and Address Detection]]

Файл: [01 Aliases and Address Detection.md](Features/01%20Aliases%20and%20Address%20Detection.md)

```text
Поддерживай фичу по спецификации relay-assistant-bot-doc/Features/01 Aliases and Address Detection.md.
Работай только в рамках этой фичи. Сначала изучи AGENTS.md, bot.py, storage.py, config.py и README.md. Не читай .env и data/.
Сохрани текущую логику @username, runtime-алиасы для любого участника разрешённого чата и безопасное определение обращения в тексте и caption. Не добавляй admin-only проверку без отдельного продуктового решения и toggle. Не реализуй очки.
Добавь целевые тесты и выполни доступные проверки. В финале перечисли изменённые файлы, команды проверки и ограничения.
```

## Фича 2: роутер намерений (реализовано)

Документ: [[Features/02 Intent Router and User Actions]]

Файл: [02 Intent Router and User Actions.md](Features/02%20Intent%20Router%20and%20User%20Actions.md)

```text
Поддерживай фичу по спецификации relay-assistant-bot-doc/Features/02 Intent Router and User Actions.md.
Фича 01 Aliases and Address Detection уже должна быть в ветке. Сначала изучи AGENTS.md и оба документа.
Добавь строго валидируемый JSON-роутер только для адресованных сообщений. Вынеси переиспользуемую логику пользовательских команд из aiogram-handler'ов; не вызывай handler напрямую и не исполняй произвольные команды из LLM-вывода.
Соблюдай порядок router lock -> route -> unload -> unlock -> action, не маршрутизируй `bot_command` в text/caption и при естественном routed-вызове сохраняй wiki/image/video result отдельно от уже сохранённого пользовательского запроса. Не добавляй `casino` в schema или allowlist до Feature 04.
Добавь parser, admission/dispatch, storage и lock-sequencing тесты, выполни проверки и дай итог с изменёнными файлами.
```

## Этап 2.5: стабилизация перед очками (реализовано)

Документ: [[Features/02.5 Pre-Feature 03 Stabilization]]

Файл: [02.5 Pre-Feature 03 Stabilization.md](Features/02.5%20Pre-Feature%2003%20Stabilization.md)

При сопровождении не нарушать принятые исключения: `/stats` доступен для chat-id discovery вне allowlist; aliases управляются любым участником разрешённого чата; profile facts являются chat-scoped общим ресурсом; surprise meme имеет только явную 2% политику. Ошибка context persistence даёт partial success и безопасное предупреждение, а не потерю готового результата.

## Фича 3: очки за шутки

Документ: [[Features/03 Joke Points and Leaderboard]]

Файл: [03 Joke Points and Leaderboard.md](Features/03%20Joke%20Points%20and%20Leaderboard.md)

Готовый standalone prompt: [03 Joke Points Implementation Prompt.md](Prompts/03%20Joke%20Points%20Implementation%20Prompt.md)

```text
Реализуй только Feature 03 по standalone prompt relay-assistant-bot-doc/Prompts/03 Joke Points Implementation Prompt.md.
Stage 02.5 уже должна быть завершена. Сохрани её контракты и выполни Feature 03 двумя последовательными PR-ready частями: Foundation, затем Integration.
Legacy rows остаются legacy/legacy_unclassified и INELIGIBLE; используй только закрытый provenance allowlist, raw snapshot keyset pagination, strict JokeSelector JSON и atomic BEGIN IMMEDIATE award transaction.
Selector failure не должен ломать summary, /compare не начисляет очки. Добавь /balance, top-10 и deterministic tie-break. Не реализуй казино.
Не читай .env и data/. Добавь изолированные SQLite и integration tests. В финале укажи migration, PR boundaries и поведение существующих баз.
```

## Фича 4: казино

Документ: [[Features/04 Casino Slots]]

Файл: [04 Casino Slots.md](Features/04%20Casino%20Slots.md)

```text
Реализуй фичу по спецификации relay-assistant-bot-doc/Features/04 Casino Slots.md.
Фича 03 Joke Points and Leaderboard должна быть уже реализована. Сначала изучи AGENTS.md, storage.py и bot.py.
Добавь только виртуальное казино со ставкой 10, нативной Telegram-анимацией 🎰, атомарным списанием/расчётом/возвратом и восстановлением pending-спинов. Не меняй правила начисления очков. Если Feature 02 уже есть в ветке, расширь её schema/allowlist intent-ом `casino`; иначе оставь только `/casino` и явно укажи отложенную интеграцию.
Не читай .env и data/. Проверь конкурентные списания, недостаток баланса, сбой отправки и таблицу выплат. В финале опиши точную таблицу соответствия Dice value и выплат.
```
