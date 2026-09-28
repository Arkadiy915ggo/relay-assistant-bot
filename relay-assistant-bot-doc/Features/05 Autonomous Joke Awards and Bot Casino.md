# Feature 05: Autonomous Joke Awards and Bot Casino

## Статус

Реализована: durable foundation, shadow worker/summary decoupling и bot identity/casino. PR 5D prefilter намеренно отложен до shadow-метрик.

Feature 01-04 считаются завершённой базой. Перед изменениями текущий full test suite должен проходить. Feature 05 не меняет правила Telegram slot mapping, stake, payouts или RTP.

## Цель

Отвязать поиск лучших шуток и начисление очков от ручного `/summary`. Бот постоянно и циклично обрабатывает новые eligible-сообщения в каждом разрешённом чате, формирует непересекающиеся блоки, выбирает лучшую шутку и атомарно начисляет очки. После рестарта обработка продолжается с durable state без повторных наград и без потери уже сохранённых сообщений.

Дополнительно сделать самого бота полноценным участником экономики чата:

- ответы `assistant/assistant_answer` остаются eligible и могут выигрывать;
- бот имеет тот же spendable balance, что и остальные участники;
- в leaderboard бот отображается под первым настроенным chat alias;
- бот может крутить казино автоматически и по явной просьбе участников;
- casino бота использует существующие stake, mapping, payouts, reserve/settle/refund и pinned leaderboard.

## Зависимости и preflight

Обязательны текущие контракты:

- [[03 Joke Points and Leaderboard]]: provenance allowlist, strict selector, append-only ledger, balance projection и atomic award;
- [[04 Casino Slots]]: versioned mapping `telegram_slots_base4_v1`, atomic reserve/settle/refund, startup recovery и void/refund contract;
- один shared `gpu_lock` для Ollama, vision и Whisper;
- один активный процесс бота;
- `handle_as_tasks=False` и последовательная обработка Telegram updates.

Preflight:

```bash
git status --short
git log --oneline -10
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/opencode/relay-assistant-pycache \
  .venv/bin/python -m compileall -q src
git diff --check
```

Не читать `.env` и `data/`. Не запускать polling против реального Telegram во время тестов.

## Зафиксированные продуктовые решения

- Автономные награды включаются отдельным config toggle и выключены по умолчанию.
- Базовый блок содержит `20` eligible-сообщений одного чата.
- Полный блок закрывается сразу после накопления 20 сообщений.
- Неполный блок закрывается через `24h` после первого сообщения, только если накоплено минимум `5` сообщений.
- Если сообщений меньше 5, блок остаётся открытым до достижения 5 или 20 сообщений.
- Один блок даёт `0` или `1` победителя.
- Базовая награда остаётся фиксированной: `+10`.
- Модель не обязана выбирать победителя; `no_joke` является успешным terminal outcome.
- Чем активнее чат, тем больше непересекающихся блоков и потенциальных наград. Это намеренная динамика, а не ограничиваемая инфляция.
- Variable rewards и несколько победителей внутри одного блока не входят в первый rollout.
- Одна source message получает `best_joke` не более одного раза за историю чата.
- При включённой автономной системе `/summary` больше не создаёт награду. Он только строит summary и показывает уже committed joke result для подходящего периода, если такой есть.
- При выключенной автономной системе текущий `/summary` award flow остаётся fallback-поведением.
- `assistant/assistant_answer` остаётся eligible. Бот может выигрывать очки.
- Safety/toxicity moderation не входит в selector policy. Оскорбительный, чёрный или направленный юмор не исключается и не штрафуется только из-за содержания.
- Защита от prompt injection, несуществующих IDs, generated rows и повторного начисления остаётся обязательной.
- Для отображения бота используется первый alias из `chat_bot_aliases` в текущем порядке `created_at ASC`; fallback: Telegram full name, username, затем bot id.
- Идентичность бота стабильна и строится из Telegram bot id: `participant_key = id:<bot_id>`. Alias влияет только на display name.
- Бот может крутить казино по явной просьбе и автоматически.
- Автоматическая политика v1: после каждого нового autonomous award принять и durable-сохранить одно случайное решение с вероятностью `25%`; если выбран spin и balance бота не меньше stake, выполнить максимум один bot spin для этой joke job.
- Пропущенное из-за недостаточного balance автоматическое решение terminal и не переигрывается для той же job.
- Явная просьба поддерживается как `/casino bot` и адресованная фраза с явным self-marker, например `Реле, крути себе слот`.
- Обычный `/casino` и `Реле, крути слот` продолжают играть от имени пользователя.

## Границы

Входит:

- durable inbox eligible-сообщений;
- непересекающиеся message-count blocks;
- persistent jobs, leases, retry/backoff и restart recovery;
- автономный worker с shared `gpu_lock`;
- переиспользование strict JokeSelector и atomic point ledger;
- shadow mode до реального начисления;
- отвязка awards от `/summary` при включённом worker;
- best-effort award announcement и pinned leaderboard refresh;
- bot participant identity через Telegram bot id и первый chat alias;
- requested и automatic bot casino spins;
- конфигурация, документация, observability и тесты.

Не входит в обязательный первый rollout:

- toxicity/safety/moderation filter;
- author cooldown;
- лимит наград в сутки;
- variable award amount;
- несколько winners внутри одного блока;
- reactions-based scoring;
- ручная модерация победителей;
- multi-instance GPU coordination;
- восстановление Telegram history, которую Bot API больше не отдаёт;
- обязательный small-model prefilter до получения shadow-метрик.

## Ключевые инварианты

1. Каждая eligible source message попадает в durable inbox не более одного раза.
2. Каждая inbox row входит не более чем в одну joke job item.
3. Membership закрытой job фиксируется immutable item rows и не зависит от будущих `messages` upserts.
4. На одну job существует не более одного terminal selector result.
5. На одну job начисляется не более одной `best_joke` награды.
6. Одна source message не награждается повторно между manual и autonomous flows.
7. Job completion, ledger insert и balance projection обновляются одной SQLite-транзакцией.
8. LLM и Telegram API никогда не вызываются внутри SQLite-транзакции.
9. Winner, author, participant key и source text перечитываются из canonical `messages` row при finalize.
10. Bot identity определяется Telegram bot id; alias не создаёт нового participant key.
11. Один automatic bot casino trigger создаёт максимум один casino spin.
12. Ошибка announcement или pinned leaderboard не откатывает committed points.

## Почему нужен durable inbox

Текущий selector строит snapshot по `(created_at, message_id)`. Для автономной message-count обработки этого недостаточно:

- delayed voice transcript может быть сохранён позже со старым Telegram timestamp;
- после restart Telegram может доставить старые pending updates;
- timestamp-only cursor неоднозначен;
- повторный scan всей таблицы дорог и усложняет разграничение блоков;
- закрытый временной snapshot не фиксирует точный набор из 20 сообщений.

Новый inbox фиксирует порядок фактического поступления eligible row в локальную систему. Он является очередью обработки, а не копией текста.

## Модель данных

### `chat_joke_inbox`

```sql
CREATE TABLE IF NOT EXISTS chat_joke_inbox (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    enqueued_at TEXT NOT NULL,
    UNIQUE(chat_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_joke_inbox_chat_queue
ON chat_joke_inbox(chat_id, queue_id);
```

Контракт:

- inbox row вставляется в той же connection и transaction, что и eligible `messages` row;
- используется общий closed provenance predicate;
- duplicate Telegram delivery не создаёт duplicate inbox row;
- если replace меняет source на eligible pair, `INSERT OR IGNORE` создаёт inbox row;
- если source позже стал ineligible, inbox остаётся audit row, но planning/finalize повторно отклоняет source;
- полный текст в inbox не копируется.

### `chat_joke_jobs`

```sql
CREATE TABLE IF NOT EXISTS chat_joke_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    first_queue_id INTEGER NOT NULL,
    last_queue_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL CHECK(message_count > 0),
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'retry', 'shadow_selected', 'shadow_no_joke',
        'no_joke', 'awarded', 'already_awarded', 'source_invalid'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_until TEXT,
    selector_status TEXT,
    winner_source_message_id INTEGER,
    winner_participant_key TEXT,
    winner_participant_name TEXT,
    winner_source_text TEXT,
    winner_source_hash TEXT,
    award_ledger_entry_id INTEGER,
    bot_spin_decision TEXT CHECK(bot_spin_decision IN (
        'skip', 'insufficient_balance', 'spin', 'completed', 'refunded'
    )),
    bot_spin_id INTEGER,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(chat_id, first_queue_id, last_queue_id),
    CHECK(first_queue_id <= last_queue_id)
);
```

Индексы:

```sql
CREATE INDEX IF NOT EXISTS idx_chat_joke_jobs_due
ON chat_joke_jobs(status, next_attempt_at, lease_until);

CREATE INDEX IF NOT EXISTS idx_chat_joke_jobs_chat_range
ON chat_joke_jobs(chat_id, last_queue_id);
```

`policy_version` первого rollout: `message_blocks_20_or_24h_v1`.

### Exact membership

Range в job является audit summary, но не источником membership. Точный неизменяемый состав хранится отдельно:

```sql
CREATE TABLE IF NOT EXISTS chat_joke_job_items (
    job_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    queue_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    PRIMARY KEY(job_id, position),
    UNIQUE(queue_id),
    UNIQUE(job_id, source_message_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_joke_job_items_job
ON chat_joke_job_items(job_id, position);
```

Job и все item rows создаются одной planning transaction. `UNIQUE(queue_id)` гарантирует, что одна inbox row не попадёт в два блока даже при конкурентных planners.

Planner выбирает первые currently eligible inbox rows без item в порядке `queue_id`. После создания job provenance может измениться, но item не удаляется и не заменяется. Selector пропускает ставший ineligible item, а finalize повторно отклоняет такого winner.

`message_count` равен количеству item rows. `first_queue_id`/`last_queue_id` равны min/max item queue ids, но rows других чатов и permanently ineligible inbox rows между ними не входят в membership.

### `chat_joke_outbox`

Terminal selector job больше не claim-ится основным flow, поэтому notification, leaderboard refresh и automatic casino требуют отдельного durable lifecycle:

```sql
CREATE TABLE IF NOT EXISTS chat_joke_outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN (
        'award_notification', 'award_leaderboard_refresh',
        'bot_casino', 'casino_leaderboard_refresh'
    )),
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'running', 'retry', 'completed', 'skipped'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_until TEXT,
    telegram_message_id INTEGER,
    casino_spin_id INTEGER,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(job_id, action)
);

CREATE INDEX IF NOT EXISTS idx_chat_joke_outbox_due
ON chat_joke_outbox(status, next_attempt_at, lease_until);
```

Award finalize создаёт нужные outbox actions в той же transaction, что job/ledger/balance. Outbox имеет отдельные claim, fenced lease, retry и startup reclaim. Crash после committed award не теряет post-finalize actions.

Award notification и award leaderboard refresh создаются в PR 5B. Bot casino и casino leaderboard refresh actions создаются только кодом PR 5C. Раздельные action keys гарантируют refresh и после award, и после более позднего casino settlement. При migration PR 5C все ранее terminal jobs с `bot_spin_decision IS NULL` получают `skip`; исторические awards не запускают казино задним числом.

### Ledger migration

Добавить nullable `joke_job_id`:

```sql
ALTER TABLE chat_point_ledger ADD COLUMN joke_job_id INTEGER;

CREATE UNIQUE INDEX IF NOT EXISTS uq_point_ledger_joke_job
ON chat_point_ledger(joke_job_id)
WHERE joke_job_id IS NOT NULL;
```

Существующий unique `(chat_id, reason, source_message_id)` сохраняется. Autonomous award использует тот же `reason='best_joke'`, чтобы manual и background flow не могли дважды наградить одну source message.

### Casino trigger migration

Текущая таблица требует положительный Telegram `request_message_id`. Automatic bot spin не имеет входящего Telegram request, поэтому подставлять joke source id или synthetic Telegram id нельзя: source могла ранее быть routed casino request, а выдуманный id нарушает аудит.

В отдельной проверяемой migration пересоздать `chat_casino_spins` с сохранением `spin_id` и всех существующих rows. Новый identity contract:

```sql
request_message_id INTEGER CHECK(
    request_message_id IS NULL OR request_message_id > 0
),
trigger_kind TEXT NOT NULL CHECK(trigger_kind IN (
    'user_request', 'bot_request', 'bot_automatic'
)),
trigger_key TEXT NOT NULL,
UNIQUE(chat_id, trigger_key),
CHECK(
    (trigger_kind IN ('user_request', 'bot_request') AND request_message_id IS NOT NULL)
    OR (trigger_kind = 'bot_automatic' AND request_message_id IS NULL)
)
```

Добавить partial unique для реальных Telegram requests:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_casino_spins_request_message
ON chat_casino_spins(chat_id, request_message_id)
WHERE request_message_id IS NOT NULL;
```

Migration выполняется одной transaction через новую table, explicit column copy, validation counts/invariants, rename и повторное создание indexes. Existing rows получают `trigger_kind='user_request'` и `trigger_key='user-message:<request_message_id>'`.

Trigger kind/key строятся только typed domain helper-ом. Caller не передаёт произвольную строку. Existing-spin replay повторно проверяет trigger kind, optional request id и participant identity; mismatch является invariant failure без economic mutation.

Новые trigger keys:

- user spin: `user-message:<message_id>`;
- requested bot spin: `bot-request:<message_id>`;
- automatic bot spin: `joke-job:<job_id>`.

Для automatic bot spin `request_message_id=NULL`. Casino ledger продолжает связываться через уникальный `casino_spin_id`; для обязательного legacy-поля `source_message_id` automatic rows используют deterministic negative sentinel `-spin_id`. Positive Telegram message ids используются только requested flows. Existing unique `(chat_id, reason, source_message_id)` и partial unique по `casino_spin_id` при этом не конфликтуют.

## Ingestion

`MessageStore.save_message()` должен записывать `messages` и optional inbox row одним commit. Нельзя сохранить eligible message, завершить commit и затем отдельным вызовом пытаться enqueue.

Алгоритм:

1. Нормализовать timestamp и provenance.
2. Начать текущую write transaction/connection.
3. Выполнить insert/upsert `messages`.
4. Перечитать resulting row; если её фактическая provenance eligible, выполнить `INSERT OR IGNORE chat_joke_inbox`.
5. Commit обоих изменений.
6. При любой ошибке rollback обоих изменений.

Для существующей базы добавить bounded initial backfill:

- только eligible rows;
- только разрешённые chat IDs;
- не старше `AUTONOMOUS_JOKES_INITIAL_LOOKBACK`;
- `INSERT OR IGNORE`;
- chronological order `(created_at, message_id)` для стабильного первого запуска;
- default lookback `7d`;
- backfill не пытается получить Telegram history извне SQLite.

## Planning Blocks

Planner работает отдельно для каждого allowed chat:

1. Прочитать первые currently eligible inbox rows без `chat_joke_job_items` по `queue_id`.
2. Зафиксировать exact rows как будущие immutable job items.
3. Если доступно минимум 20, создать job из первых 20.
4. Если доступно 5-19 и oldest `enqueued_at` старше 24h, создать partial job из всех доступных rows.
5. Если меньше 5, ничего не закрывать.
6. Повторять, пока можно создать следующую full job.

Job creation и все item inserts выполняются под `_write_lock` и `BEGIN IMMEDIATE`. Unique job items предотвращают duplicate planning после restart и при конкурентных `MessageStore` instances.

`ALLOWED_CHAT_IDS` применяется до planning. При пустом allowlist планируются все chat IDs с eligible inbox rows, как и текущая access policy.

## Selector Integration

### Первый rollout

Первый rollout переиспользует текущий strict parser и page/tournament semantics без отдельной маленькой модели.

Рефакторинг `JokeSelector`:

- добавить public operation выбора из фиксированного списка canonical `StoredMessage`;
- сохранить batch limits, strict prompt-local ID validation и tournament progress guard;
- текущий `choose(store, since, boundary)` оставить для manual fallback либо реализовать через общий internal iterator;
- сериализовать candidate input как JSON, а не свободные `id/author/text` blocks;
- model output по-прежнему содержит только `has_joke`, `source_message_id`, `reason`;
- модель не задаёт author, participant key, quote или award amount.

Для context-aware rendering в v1 допускается добавить к каждому target:

- replied message, если оно существует в той же job или доступно в `messages`;
- до двух непосредственно предшествующих сообщений job;
- явную метку `target_message_id`, автор которого получит points.

Context rows не становятся отдельными award candidates, если не входят в candidate target set.

Selector prompt не содержит toxicity/safety criterion. Он оценивает юмор и может выбирать оскорбительный или чёрный юмор.

### Small-model prefilter после shadow-метрик

Отдельный prefilter добавляется только если наблюдения показывают неприемлемую стоимость или GPU latency.

Cascade:

1. Closed provenance filter.
2. Conservative deny-only rules: empty, URL-only, emoji-only, exact duplicate.
3. Маленькая text LLM возвращает strict top-K target IDs для каждой page.
4. Persist candidate IDs/context IDs, model name и policy version.
5. Большая judge model проводит текущий tournament.

Prefilter не назначает points и не делает safety filtering. Его false negative rate сначала измеряется в shadow mode сравнением с полным selector.

Не использовать Whisper или vision model как text prefilter. Добавить отдельный `AUTONOMOUS_JOKE_PREFILTER_MODEL` с fallback на `INTENT_ROUTER_MODEL` только после принятия этого этапа.

## Background GPU Policy

Сейчас отдельной priority queue нет; `gpu_lock` является общей взаимной блокировкой. Worker должен использовать тот же объект.

Контракт v1:

- если `gpu_lock` занят, background worker не становится постоянным waiter, а откладывает попытку;
- одна background acquisition выполняет максимум один model batch call;
- между page calls worker освобождает lock и делает cooperative yield;
- SQLite claim/finalize, Telegram send, sleep и planning выполняются без `gpu_lock`;
- unload выполняется в `finally` до release, если текущий client contract требует unload after task;
- user command, уже ожидающая lock, не должна быть обойдена следующим background batch;
- один long-running job не удерживает GPU на весь page/tournament cycle.

Если это невозможно гарантировать одним `asyncio.Lock` без private API, добавить небольшой `GpuScheduler` с foreground/background admission и перевести существующие lock call sites отдельным PR. Не обещать strict foreground priority на основе только `lock.locked()`.

## Worker Lifecycle

Новый модуль: `src/tg_summary_bot/autonomous_jokes.py`.

Ответственность:

- initial backfill;
- planning;
- claim/reclaim;
- selector execution;
- retry/backoff;
- atomic finalize;
- notification retry;
- automatic bot casino decision/execution;
- безопасные метрики.

Startup:

1. `load_settings()`.
2. `store.init()`.
3. casino recovery.
4. создать LLM clients, selector, shared GPU coordination и dispatcher.
5. создать worker task, но выдержать startup grace по умолчанию `60s`.
6. начать polling.
7. после grace worker начинает backfill/planning, чтобы Telegram backlog сначала сохранился в inbox.

Shutdown:

```python
stop_event = asyncio.Event()
worker_task = asyncio.create_task(worker.run(stop_event))
try:
    await dp.start_polling(...)
finally:
    stop_event.set()
    worker_task.cancel()
    await asyncio.gather(worker_task, return_exceptions=True)
```

Cancellation не переводит running job в terminal state. Lease позволяет reclaim после restart.

## Claim, Lease и Retry

`claim_due_joke_job(worker_id, now, lease_seconds)`:

1. `_write_lock` + `BEGIN IMMEDIATE`.
2. Найти одну `pending/retry` job с due `next_attempt_at` либо `running` с истёкшим lease.
3. Сгенерировать новый opaque `lease_token`.
4. Increment `attempt_count`.
5. Записать owner/token/until и `status='running'`.
6. Commit и вернуть immutable job.

Любой finalize/retry method проверяет `job_id + lease_token`. Старый worker после reclaim не может завершить job.

Worker обновляет `lease_until` compare-and-set запросом по `job_id`, `status='running'` и текущему непросроченному `lease_token`. Typed result: `renewed` или `lost`. При `lost` worker немедленно прекращает новые model calls и не вызывает finalize.

Lease обновляется между model batches. Один job с несколькими pages/tournament rounds не должен потерять ownership только из-за нормальной длительности обработки. Config validation требует `lease_seconds > JOKE_SELECTOR_TIMEOUT_SECONDS + 60`.

Backoff:

```text
60s, 2m, 4m, 8m, 15m, 30m, 60m, затем 60m
```

Outcomes:

- valid `none` -> `no_joke`;
- valid winner + committed award -> `awarded`;
- winner already rewarded -> `already_awarded`;
- missing/ineligible/out-of-job source -> `source_invalid`;
- shadow selection -> `shadow_selected` или `shadow_no_joke`;
- timeout, invalid JSON, model error, transient SQLite error -> `retry`;
- cancellation/process death -> lease expiry и reclaim.

Не вводить малый finite retry limit. После 10 attempts логировать alert-level metadata, но продолжать capped retries.

## Atomic Finalize

`finalize_autonomous_joke_job(job_id, lease_token, selection, awarded_at)` выполняется одной transaction:

1. `BEGIN IMMEDIATE` под `_write_lock`.
2. Перечитать job и проверить token/status.
3. Для terminal replay вернуть canonical saved result.
4. Для `none` записать `no_joke` и commit.
5. Для winner перечитать source row через inbox membership текущей job.
6. Повторно проверить chat, exact immutable job item membership и closed provenance.
7. Определить participant key из source sender id/name.
8. Если source sender id равен Telegram bot id, сохранить тот же `id:<bot_id>` key; display alias разрешается отдельно.
9. Вставить `best_joke/+10` с `joke_job_id`.
10. При source duplicate сохранить canonical winner snapshot и завершить job как `already_awarded`; не выбирать второго winner, не менять balance и не создавать award notification/automatic-casino actions.
11. Только для нового ledger insert выполнить upsert balance `+10`.
12. Сохранить bounded canonical winner participant key/name/text и deterministic source hash для delayed notification и `/summary` rendering.
13. Записать winner, ledger id и terminal timestamps.
14. Только для нового award создать enabled outbox rows в той же transaction.
15. Commit.
16. При неизвестной ошибке rollback всех изменений.

Текущий `award_unique_joke()` нельзя вызывать из этой transaction, потому что он открывает свою connection. Выделить private transaction helper и переиспользовать его из manual и autonomous public methods.

## `/summary` после включения worker

При `AUTONOMOUS_JOKES_ENABLED=false` сохранить текущий flow без UX-регрессии.

При `true`:

- summary не вызывает selector и `award_unique_joke()`;
- summary generation использует текущий memory/raw path;
- после generation выполняется read-only lookup последней autonomous award job, winner которой входит в requested period;
- committed winner рендерится без новой награды;
- delayed rendering использует canonical winner snapshot из job, а не изменяемую `messages` row;
- pending/retry jobs не блокируют summary;
- `/compare` по-прежнему не читает и не создаёт awards.

Так устраняется overlap, при котором manual summary и worker могли бы выбрать разные winners из одного блока.

## Award Notification

После `awarded` отдельный outbox worker best-effort отправляет одно короткое сообщение с canonical author, quote и `+10`, затем независимо обновляет pinned `Топ балансов`.

Правила:

- `no_joke` не создаёт Telegram сообщение;
- storage commit не зависит от Telegram send;
- outbox status/lease сохраняются и повторяются после restart;
- raw model reason не показывается;
- Telegram не даёт exactly-once send idempotency, поэтому delivery остаётся best effort;
- подтверждённый `announcement_message_id` сохраняется, повторная обработка не отправляет второе сообщение;
- stale pinned leaderboard исправляется повторным refresh.

## Bot Participant Identity

На startup получить `getMe()` и сформировать:

```text
participant_key = id:<telegram_bot_id>
fallback_name = full_name or username or str(bot_id)
```

Для каждого чата display name:

1. первый `chat_bot_aliases` по существующему ordering;
2. Telegram fallback name.

Alias никогда не используется как participant key. Добавление, удаление или переименование alias не создаёт новый balance.

Leaderboard service разрешает current bot display name до tie sorting и применения `limit`. `/top`, `/stats` и pinned board используют один mapped ordering: `balance DESC`, resolved display name, participant key. Простая подмена имени после `limit=1/10` запрещена, потому что она меняет результат tie ordering.

Исторический ledger сохраняет participant name на момент операции, но текущий leaderboard показывает актуальный выбранный alias.

Assistant source остаётся canonical source award. Исключение касается только display name бота, а не winner id, text или identity key.

## Bot Casino

### Общая domain operation

Не создавать synthetic aiogram `Message` и не вызывать handler из worker.

Выделить из `spin_casino()` обычную async domain operation, принимающую:

- chat id;
- typed trigger descriptor (`user_request`, `bot_request` или `bot_automatic` + domain id);
- optional positive audit `request_message_id` для requested flow;
- `None` для automatic flow;
- participant key/name;
- callable для `send_dice`;
- callable для terminal response при requested flow;
- store и bot dependencies.

Human handler сохраняет текущий admission и передаёт identity пользователя. Проверку `from_user.is_bot` не ослаблять.

Bot flow передаёт stable bot participant key, first alias и вызывает `Bot.send_dice(chat_id=..., emoji="🎰")`. Requested flow сохраняет reply/thread semantics там, где есть исходное сообщение.

Reserve, Telegram response validation, settle, refund и startup recovery остаются общими. Payout всегда выводится только из trusted Telegram `Dice.value`.

### Requested bot spin

Поддержать:

- `/casino bot`;
- addressed intent с явным self-marker: `себе`, `сам`, `сама`, `свой`;
- пример: `Реле, крути себе слот`.

Без self-marker routed `casino` играет за автора сообщения, как сейчас.

Для `/casino bot` и `casino_bot`:

- trigger key `bot-request:<request_message_id>`;
- balance списывается у `id:<bot_id>`;
- при недостаточном bot balance вернуть понятный ответ;
- requester balance не меняется.

Router должен различать `casino` и `casino_bot` строгим action allowlist. Детерминированный lexical guard self-marker применяется после model output, чтобы обычная просьба не переключилась на bot balance.

### Automatic bot spin

После каждого нового autonomous `awarded`:

1. Если `BOT_AUTO_CASINO_ENABLED=false`, не вызывать RNG, сохранить `skip` и не создавать bot-casino outbox.
2. Если toggle включён, в award-finalize transaction сэмплировать решение по configured chance `0.25`.
3. Сохранить `skip` или `spin`; после committed decision outbox retries никогда не пересэмпливают случайность. Если вся finalize transaction rollback-нулась, решения не существует и следующий finalize attempt может сэмплировать заново.
4. Для `spin` создать `bot_casino` outbox action в той же transaction.
5. Outbox processor читает bot balance.
6. Если balance меньше 10, записать `insufficient_balance` и outbox `skipped` terminal.
7. Иначе reserve с typed automatic trigger для `joke-job:<job_id>`.
8. Отправить `Bot.send_dice` без DB lock/GPU lock.
9. Использовать текущий validate/settle/refund contract.
10. Сохранить `bot_spin_id`, terminal decision state и outbox result.
11. В той же transaction, которая commit-ит terminal casino settlement/refund и завершает `bot_casino` outbox action, создать `casino_leaderboard_refresh`. Crash не должен оставить изменённый balance без durable refresh action.

Автоматический casino не занимает `gpu_lock`.

## Конфигурация

Минимальные новые settings:

```env
AUTONOMOUS_JOKES_ENABLED=false
AUTONOMOUS_JOKES_SHADOW_MODE=true
AUTONOMOUS_JOKES_BLOCK_MESSAGES=20
AUTONOMOUS_JOKES_PARTIAL_MIN_MESSAGES=5
AUTONOMOUS_JOKES_MAX_BLOCK_AGE=24h
AUTONOMOUS_JOKES_INITIAL_LOOKBACK=7d
AUTONOMOUS_JOKES_STARTUP_GRACE_SECONDS=60
AUTONOMOUS_JOKES_POLL_SECONDS=30
AUTONOMOUS_JOKES_LEASE_SECONDS=900
AUTONOMOUS_JOKE_JUDGE_MODEL=
AUTONOMOUS_JOKE_ANNOUNCE=true
BOT_AUTO_CASINO_ENABLED=true
BOT_AUTO_CASINO_CHANCE=0.25
```

Validation:

- block messages > 0;
- partial minimum в `1..block_messages`;
- positive durations/intervals;
- chance в `0..1` и не принимает NaN/Infinity;
- judge model fallback: основная summary model;
- feature disabled не создаёт worker task и не меняет `/summary` behavior;
- shadow mode создаёт jobs/results, но не ledger/balance/Telegram award announcement/automatic casino.
- `AUTONOMOUS_JOKE_ANNOUNCE=false` не создаёт `award_notification`; award leaderboard refresh всё равно создаётся;
- `BOT_AUTO_CASINO_ENABLED=false` не вызывает RNG и не создаёт `bot_casino` action;

Обновить `Settings`, `load_settings()`, `.env.example`, README `/stats` и doctor model checks при отдельной model.

## Observability

Структурированные metadata без raw content по умолчанию:

- job id/chat id/policy version;
- block size/range/age;
- attempt/lease/retry code;
- page count/tournament rounds;
- selector status;
- winner source id;
- award status/ledger id;
- notification status;
- bot spin decision/spin id/status;
- queue wait и model latency.

Не логировать полный prompt, candidate text, source quote или model reason при выключенном content capture. Учитывать, что `OPIK_CAPTURE_CONTENT=true` может отправлять текст чата во внешний tracing backend.

## Telegram History Limitation

Durable inbox гарантирует catch-up только для сообщений, которые уже сохранены в SQLite или ещё доставлены Telegram после restart. Bot API не позволяет запросить произвольную историю чата и не хранит pending updates бесконечно. Документация не должна обещать восстановление сообщений, которые Telegram больше не отдаёт.

Startup grace уменьшает race между worker и ingestion накопленных updates, но не превращает Bot API в history API.

## Этапы реализации

### PR 5A: Durable Foundation

- inbox schema и atomic ingestion;
- initial bounded backfill;
- joke jobs, immutable job items и outbox schema;
- planner с atomic job/item creation;
- ledger `joke_job_id` migration;
- selector-job и outbox claim/lease/retry storage methods;
- exact job message read;
- atomic finalize через общий private award transaction helper;
- storage/config tests;
- worker ещё не запускается.

### PR 5B: Shadow Worker и Summary Decoupling

- `autonomous_jokes.py`;
- lifecycle/startup grace/shutdown;
- fixed-list selector refactor и JSON candidate serialization;
- background GPU admission по одному model call;
- shadow mode по умолчанию;
- notification state и pinned refresh;
- `/summary` read-only autonomous winner при enabled mode;
- restart/cancellation/retry tests;
- после shadow review включить real awards config-ом.

### PR 5C: Bot Identity и Casino

- cached bot identity;
- first-alias leaderboard display;
- common casino domain operation без synthetic Message;
- `trigger_key` migration;
- `/casino bot`;
- strict `casino_bot` addressed intent/self-marker guard;
- durable 25% automatic decision per awarded joke job;
- bot reserve/send/settle/refund/recovery;
- tests requested/automatic/insufficient/retry/idempotency/alias changes.

### PR 5D: Optional Small Prefilter

Начинать только после shadow-метрик PR 5B:

- context episodes;
- conservative rules;
- separate small text model;
- persistent candidate IDs;
- full-selector control sample;
- false-negative/cost/latency comparison;
- rollout только при измеримом выигрыше.

## Тестовый план PR 5A

- New DB, existing Feature 03 DB и existing Feature 04 DB migrations.
- Repeated `init()`.
- Message и inbox commit/rollback вместе.
- Duplicate save не дублирует inbox.
- Replace ineligible -> eligible enqueue; eligible -> ineligible revalidation.
- Eligible assistant answer входит в inbox.
- Generated/legacy rows не входят.
- Initial lookback и allowed chats.
- Full blocks 100+100+remainder.
- Partial block 5-19 после 24h; меньше 5 остаётся open.
- Two planners не присваивают одну inbox row двум job items.
- Claim/reclaim/token fencing.
- Lease renewal CAS и немедленный abort при lost ownership.
- Process death simulation через expired lease.
- Terminal award создаёт outbox rows атомарно.
- Crash/restart после award продолжает pending notification/leaderboard actions.
- No-joke terminal replay.
- Job/source unique awards.
- Manual/autonomous source conflict.
- Job + ledger + balance rollback together.
- Projection equals ledger sum после awards и casino rows.

## Тестовый план PR 5B

- Worker disabled не запускается и `/summary` сохраняет старый award flow.
- Enabled worker отключает award side effect `/summary`.
- Shadow mode не меняет ledger/balance и не отправляет award notification.
- Exact job membership передаётся selector-у.
- JSON candidate serialization и prompt-local IDs.
- Reply/predecessor context не меняет target author.
- One GPU batch per background acquisition.
- Busy GPU откладывает background work.
- Cancellation до/во время/после model call оставляет recoverable job.
- Retry backoff capped на 60m.
- Startup grace позволяет сохранить pending updates до planning.
- Award announcement только после commit.
- Telegram failure не откатывает award; notification retry.
- Delayed notification и `/summary` используют сохранённый canonical winner snapshot после source replace.
- Pinned leaderboard refresh best effort.
- `/compare` не создаёт jobs/awards.

## Тестовый план PR 5C

- Stable bot key `id:<bot_id>`.
- First alias выбирается детерминированно; removal переключает display на следующий/fallback.
- Alias разрешается до tie sorting и top limit для `/top`, `/stats` и pinned board.
- Alias change не создаёт новый balance.
- Assistant answer award credit-ит bot balance.
- Existing human `/casino` admission и behavior не регрессируют.
- `/casino bot` списывает только bot balance.
- Addressed self-marker -> `casino_bot`; без marker -> requester `casino`.
- Router не может переключить participant без lexical self-marker.
- На job существует ровно одно committed automatic decision; после commit RNG не вызывается повторно, rollback без решения допускает новый sample.
- Chance 0/1 deterministic tests; production 0.25 mocked RNG.
- Insufficient bot balance terminal и не retry-ится.
- Automatic reserve idempotent по `joke-job:<job_id>`.
- Requested reserve idempotent по `bot-request:<message_id>`.
- Casino table-copy migration сохраняет existing spin ids/rows и валидирует row count.
- Trigger cross-field CHECK и replay identity mismatch.
- Pre-PR-5C terminal joke jobs получают `bot_spin_decision=skip` без retroactive spins.
- Crash после award до bot casino продолжает durable outbox action.
- Trusted Telegram Dice validation применяется к bot flow.
- Send error/timeout/cancellation даёт existing void/refund semantics.
- Automatic casino не использует GPU lock.
- Leaderboard показывает alias и актуальный spendable balance бота.

## Критерии готовности

- Очки появляются без `/summary`.
- На 300 eligible сообщений создаются три непересекающиеся full jobs по 100, а не одна job с произвольными тремя winners.
- Restart не теряет сохранённые inbox rows и не дублирует jobs/awards.
- Один job и одна source message не могут дать повторные points при retries/races.
- `/summary` не конкурирует с worker за право начислить points при enabled mode.
- `no_joke` завершает job без ledger изменений.
- Assistant answers могут выиграть, а bot balance использует стабильный Telegram bot id.
- `/top`, `/stats` и pinned leaderboard показывают бота под первым alias.
- Бот может крутить казино по `/casino bot`, явному addressed self-intent и durable automatic policy.
- Human casino behavior, mapping, RTP и recovery не изменились.
- Background LLM не создаёт второй GPU lock и не удерживает lock во время DB/Telegram операций.
- Shadow mode позволяет проверить качество до включения реальных начислений.
- Полный unittest suite, compileall и `git diff --check` проходят.

## Рекомендуемый rollout

1. Deploy PR 5A с feature disabled.
2. Deploy PR 5B с `ENABLED=true`, `SHADOW_MODE=true`, без announcements и bot casino.
3. Наблюдать минимум несколько десятков закрытых blocks: no-joke rate, invalid rate, latency, winners, GPU waits.
4. Включить real fixed `+10` awards и announcements.
5. Deploy PR 5C; сначала requested bot casino, затем automatic chance.
6. Рассматривать PR 5D только при подтверждённой проблеме GPU cost/latency.
