# Feature 04: Casino Slots

## Статус

Реализована. Domain/Storage, Slash Lifecycle/Recovery и Addressed Intent находятся в текущем коде и покрыты casino tests.

Перед началом casino migration Feature 03 должна быть проверена полным test suite и зафиксирована отдельным коммитом. В casino-коммиты нельзя смешивать незавершённые изменения joke selector, provenance, award transaction или summary integration.

## Цель

Добавить полностью виртуальный Telegram-слот за текущие chat-scoped points. Пользователь вызывает `/casino` либо явно адресованную фразу вроде `Реле, прокрути слот`. Бот атомарно резервирует ставку, отвечает нативным Telegram Dice `🎰`, рассчитывает выплату только из подтверждённого `Dice.value`, атомарно завершает или отменяет spin и показывает terminal balance.

Очки не являются деньгами, не покупаются, не переводятся, не выводятся и не обмениваются на реальные ценности.

## Зависимости и preflight

Обязательны завершённые:

- [[02 Intent Router and User Actions]] и [[02.5 Pre-Feature 03 Stabilization]];
- [[03 Joke Points and Leaderboard]] с append-only ledger, nonnegative balance projection и atomic award;
- исправленная классификация expected duplicate conflict: неизвестный `IntegrityError` не считается idempotent success;
- transaction-validated source rendering Feature 03;
- handler-level tests summary award/repeat/failure, `/compare`, `/balance`, `/top`, `/stats` и shared `gpu_lock` sequencing;
- синхронизированная документация Feature 03.

Preflight-команды:

```bash
git status --short
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/opencode/relay-assistant-pycache \
  .venv/bin/python -m compileall -q src
git diff --check
```

Если Feature 03 всё ещё находится в незакоммиченном worktree, сначала завершить и отдельно зафиксировать её. Feature 04 не должна маскировать или одновременно исправлять Feature 03.

## Границы

Входит:

- один слот `🎰`;
- ставка ровно `10` points;
- versioned mapping всех `Dice.value` от 1 до 64;
- RTP `90.625%`;
- атомарные reserve, settle и refund;
- request idempotency;
- startup recovery незавершённых spins;
- `/casino` и явный routed intent `casino`;
- `/top`, `/stats` и pinned leaderboard как рейтинг текущих balances;
- terminal operation outcomes и безопасные технические логи.

Не входит:

- реальные деньги, платежи, покупки и вывод;
- перевод points между участниками или чатами;
- произвольная ставка;
- другие игры, внешние RNG, бонусы и кредиты;
- ручное редактирование balances;
- multi-instance coordination/leases;
- изменение правила `+10` за лучшую шутку;
- отдельный lifetime joke leaderboard в v1.

## Зафиксированные продуктовые решения

- Stake: `10`.
- Gross payouts: `0`, `5`, `50`, `250`.
- `/top` и pinned leaderboard показывают текущие spendable balances после joke awards, bets, payouts и refunds.
- Заголовок pinned сообщения: `Топ балансов`, не `Топ шуток`.
- Неизвестный или незафиксированный Telegram outcome считается `void`; stake возвращается.
- Видимая Dice-анимация без committed settlement не имеет экономической силы.
- Mapping принимается как versioned community contract `telegram_slots_base4_v1`.
- Jackpot v1: только `seven/seven/seven`, `Dice.value == 64`.
- Completed spin всегда имеет `casino_bet` и `casino_payout`, включая payout `0`.
- Refunded spin всегда имеет `casino_bet` и `casino_refund`; payout и refund взаимно исключаются.
- Поддерживается один активный процесс бота. Multi-instance deployment в v1 не поддерживается.
- Играть может только реальный non-bot `from_user` в allowed chat, без anonymous `sender_chat`; request `message_id` обязан быть положительным.

## Экономика v1

| Категория | Gross payout | Net после stake 10 |
| --- | ---: | ---: |
| Нет совпадений | 0 | -10 |
| Ровно одна пара | 5 | -5 |
| Три одинаковых, кроме jackpot | 50 | +40 |
| `seven/seven/seven` | 250 | +240 |

При принятой карте из 64 результатов:

- no match: `24`;
- pair: `36`;
- ordinary triple: `3`;
- jackpot: `1`.

```text
expected gross = (24×0 + 36×5 + 3×50 + 1×250) / 64 = 9.0625
RTP = 9.0625 / 10 = 90.625%
expected net = -0.9375 point per spin
```

Gross payout сначала credit-ится после уже committed stake debit. Handler и LLM никогда не задают payout или delta.

## Versioned Slot Mapping

Новый `src/tg_summary_bot/casino.py` содержит чистый domain contract:

```python
CASINO_RULES_VERSION = "telegram_slots_base4_v1"
CASINO_STAKE = 10
CASINO_SYMBOLS = ("bar", "grape", "lemon", "seven")

n = value - 1
left = n % 4
middle = (n // 4) % 4
right = (n // 16) % 4
```

Sentinel values:

| Value | Комбинация | Категория | Payout |
| ---: | --- | --- | ---: |
| `1` | bar/bar/bar | triple | 50 |
| `22` | grape/grape/grape | triple | 50 |
| `43` | lemon/lemon/lemon | triple | 50 |
| `64` | seven/seven/seven | jackpot | 250 |

Validation:

- принимать только `type(value) is int`;
- диапазон только `1..64`;
- `bool`, float, string, `0`, `65` и другие значения отклоняются;
- категория определяется по числу равных symbol indexes;
- jackpot проверяется до общей triple category;
- payout выводится только из category и rules version.

Community mapping является осознанно принятой недокументированной зависимостью Telegram. Каждый spin сохраняет `rules_version`, чтобы будущая смена mapping не меняла исторические settlements. Tests обязаны независимо перечислить expected category/payout для всех 64 values, а не повторять production decoder тем же алгоритмом.

## Модель данных

### `chat_casino_spins`

```sql
CREATE TABLE IF NOT EXISTS chat_casino_spins (
    spin_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    request_message_id INTEGER NOT NULL CHECK(request_message_id > 0),
    participant_key TEXT NOT NULL,
    participant_name TEXT NOT NULL,

    rules_version TEXT NOT NULL,
    stake INTEGER NOT NULL CHECK(stake = 10),

    dice_message_id INTEGER CHECK(dice_message_id > 0),
    dice_value INTEGER CHECK(dice_value BETWEEN 1 AND 64),
    category TEXT CHECK(category IN ('none', 'pair', 'triple', 'jackpot')),
    payout INTEGER CHECK(payout IN (0, 5, 50, 250)),

    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'refunded')),
    terminal_balance INTEGER CHECK(terminal_balance >= 0),
    terminal_reason TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    refunded_at TEXT,

    UNIQUE(chat_id, request_message_id)
);
```

Application transaction validation дополняет DDL:

- `pending`: terminal поля, category и payout отсутствуют;
- `completed`: dice id/value, category, payout, terminal balance и completed_at обязательны;
- `refunded`: payout/category отсутствуют, terminal balance/refunded_at/reason обязательны;
- terminal state не меняется;
- participant identity и request reference после insert не меняются.

Индексы:

```sql
CREATE INDEX IF NOT EXISTS idx_chat_casino_spins_pending_created
ON chat_casino_spins(status, created_at);

CREATE INDEX IF NOT EXISTS idx_chat_casino_spins_chat_participant
ON chat_casino_spins(chat_id, participant_key, created_at);
```

### Ledger migration

Добавить nullable колонку безопасной `_ensure_column` migration:

```sql
ALTER TABLE chat_point_ledger ADD COLUMN casino_spin_id INTEGER;
```

Добавить partial unique index:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_point_ledger_casino_spin_reason
ON chat_point_ledger(chat_id, reason, casino_spin_id)
WHERE casino_spin_id IS NOT NULL;
```

Для casino ledger row:

- `source_message_id = request_message_id` для Telegram-аудита;
- `casino_spin_id = spin_id` для доменной связи;
- `reason` только `casino_bet`, `casino_payout`, `casino_refund`;
- delta задаётся storage method, а не caller-ом.

Существующие `best_joke` rows остаются без `casino_spin_id` и не меняются. Текущий unique `(chat_id, reason, source_message_id)` сохраняется.

Не добавлять универсальный публичный `adjust_balance(delta, reason)`. Нужны только узкие typed casino methods.

## Typed Storage Results

Рекомендуемые immutable dataclasses/status literals:

- reserve: `created`, `insufficient_balance`, `existing_pending`, `existing_completed`, `existing_refunded`, `identity_conflict`;
- settle: `completed`, `already_completed`, `refunded_conflict`, `not_found`, `invalid_dice`;
- refund: `refunded`, `already_refunded`, `completed_conflict`, `not_found`;
- recovery: refunded spin ids, affected chat ids и count.

Typed result возвращает только canonical DB state: spin id/status, participant, rules version, stake, dice, category, payout и terminal balance. UI не строится из caller input после transaction.

## Atomic Reserve

`reserve_casino_spin(...)` выполняется на одной connection под `MessageStore._write_lock` и `BEGIN IMMEDIATE`:

1. Проверить existing spin по `(chat_id, request_message_id)`.
2. Для existing spin проверить совпадение participant key; mismatch вернуть как invariant violation без изменений.
3. Для terminal duplicate вернуть сохранённое состояние; новый Dice не отправлять.
4. Создать `pending` spin и получить `spin_id`.
5. Выполнить conditional debit:

```sql
UPDATE chat_point_balances
SET balance = balance - 10,
    participant_name = ?,
    updated_at = ?
WHERE chat_id = ?
  AND participant_key = ?
  AND balance >= 10;
```

6. Если affected rows = 0, rollback: spin и ledger не существуют, результат `insufficient_balance`.
7. Вставить ровно один ledger `casino_bet=-10` со `spin_id`.
8. Прочитать balance after debit.
9. Commit.

Нельзя выполнять `get balance -> проверить -> позже списать`. Отсутствующая balance row эквивалентна нулю.

## Telegram Send и Trust Boundary

После committed reserve, без DB lock и без `gpu_lock`:

```python
sent = await message.reply_dice(emoji="🎰")
```

Перед settlement проверить:

- returned chat совпадает с request chat;
- `sent.message_id > 0`;
- `sent.dice is not None`;
- `sent.dice.emoji == "🎰"`;
- `type(sent.dice.value) is int`;
- value входит в `1..64`.

Пользовательский входящий Dice, model output, command argument и query никогда не используются как источник выплаты.

Reply должен сохранять исходный forum topic/thread. Mock integration test проверяет `chat_id`, `reply_parameters`, thread semantics и `emoji`, а не только вызов функции.

## Atomic Settlement

`settle_casino_spin(spin_id, dice_message_id, dice_value)` выполняется под `_write_lock` и `BEGIN IMMEDIATE`:

1. Перечитать spin.
2. `completed` вернуть идемпотентно из canonical row без новой ledger записи.
3. `refunded` вернуть terminal conflict без payout.
4. Разрешить transition только из `pending`.
5. Проверить Dice и вычислить category/payout внутри domain/storage boundary.
6. Вставить ровно один `casino_payout`, включая `delta=0`.
7. Увеличить balance на payout и обновить participant name/time.
8. Записать dice id/value, category, payout, terminal balance, completed_at.
9. Выполнить compare-and-set `pending -> completed`.
10. Commit.

Ни один result message не заявляет payout или balance до успешного commit.

## Atomic Refund

`refund_casino_spin(spin_id, reason)` выполняется под `_write_lock` и `BEGIN IMMEDIATE`:

1. Перечитать spin.
2. `refunded` вернуть идемпотентно.
3. `completed` вернуть terminal conflict; completed payout не отменяется.
4. Разрешить transition только из `pending`.
5. Вставить ровно один `casino_refund=+10`.
6. Вернуть stake в balance.
7. Записать terminal balance, safe reason и refunded_at.
8. Выполнить compare-and-set `pending -> refunded`.
9. Commit.

Settlement/refund race завершается ровно одним terminal state. Один spin не может иметь одновременно payout и refund.

## Честная семантика Telegram/SQLite окна

Telegram API и SQLite не образуют распределённую транзакцию. Bot API не принимает client idempotency key для `sendDice` и не позволяет восстановить потерянный response по request message.

Зафиксированный v1 contract:

> Если Telegram мог показать Dice, но бот не имеет committed settlement, spin считается void и stake возвращается. Видимая анимация такого spin не имеет экономической силы.

Сценарии:

- send явно failed: немедленный idempotent refund;
- send timeout/connection loss: uncertain outcome, refund как `telegram_outcome_unknown`;
- process cancellation: попытаться `asyncio.shield(refund)`, затем пробросить cancellation;
- hard kill после reserve: startup recovery;
- Dice response получен, settlement временно failed: bounded retry; если commit не подтверждён, оставить pending для startup void/refund;
- final text send failed после completed commit: payout остаётся completed, refund запрещён.

Документация и UI не должны обещать exactly-once Dice delivery. Экономическая exactly-once обеспечивается только для committed storage transitions.

## Startup Recovery

V1 поддерживает один активный процесс.

Порядок startup:

1. `await store.init()`.
2. Зафиксировать UTC `startup_cutoff`.
3. До dispatcher/polling вызвать `refund_pending_casino_spins(created_before=startup_cutoff)`.
4. В одной или нескольких idempotent transactions перевести pre-start pending spins в `refunded` с reason `startup_recovery`.
5. Получить affected chat ids.
6. Best-effort обновить pinned `Топ балансов` для affected chats.
7. Только после успешного storage recovery начать polling.

Если recovery storage transaction не выполняется, startup должен fail closed и не принимать новые bets. Ошибка Telegram leaderboard refresh не блокирует polling и не откатывает refunds.

## Общая Bot Operation

Одна обычная async-операция `spin_casino(...)` используется slash и routed вызовами. Нельзя вызывать aiogram-handler из handler-а или синтезировать `/casino`.

Admission:

- allowed chat;
- не channel post;
- `message.from_user` существует и `is_bot == false`;
- `message.sender_chat is None`;
- положительный `message_id`;
- participant key вычисляется существующим общим helper.

Flow:

1. Reserve.
2. Existing pending/completed/refunded вернуть как replay без нового Dice.
3. Insufficient balance показать без spin/ledger side effects.
4. Для нового pending отправить reply Dice.
5. На send/validation failure выполнить refund.
6. На valid response выполнить settlement.
7. После commit опционально дождаться короткой UX-задержки окончания анимации; delay не влияет на transaction.
8. Отправить reply с stake, category, gross payout, net change и terminal balance.
9. Best-effort refresh pinned leaderboard.
10. Вернуть один terminal `OperationOutcome`.

Duplicate UX:

- existing pending: `Этот спин уже обрабатывается.`;
- completed: повторно показать сохранённый category/payout/terminal balance;
- refunded: сообщить, что spin отменён и stake возвращён;
- identity conflict: безопасный отказ и invariant log.

## Leaderboard Semantics

`chat_point_balances` является spendable wallet projection.

- `/balance` показывает текущий wallet balance.
- `/top` показывает top-10 текущих положительных balances.
- `/stats` leader берётся из того же ordering.
- Pinned title меняется на `Топ балансов`.
- Joke award, completed casino, refund и startup recovery используют одну best-effort refresh operation.
- Проигрыш может понизить место, jackpot повысить.
- Refresh failure никогда не откатывает committed ledger/balance/spin state.
- Удалённое pinned сообщение пересоздаётся только для подтверждённого Telegram not-found; transient/no-change error сохраняет stored message id.

Отдельный lifetime joke leaderboard не входит в v1.

## Routed Intent

Router integration выполняется только после стабильного `/casino` lifecycle.

Добавить `casino` в `Action`, strict allowlist и prompt. `period` и `query` для casino должны быть `null`; caller input не задаёт stake.

Post-guard требует одновременно:

- action verb: `крути`, `крутан`, `прокрути`, `запусти`, `сыграй`;
- casino object: `казино`, `слот`, `слоты`.

Примеры, которые запускают casino:

- `Реле, прокрути казино`;
- `Реле, крутань слот`;
- `Реле, сыграй в слоты`.

Не запускают casino:

- `что ты думаешь о казино?`;
- `объясни правила слотов`;
- `у нас вчера было казино`;
- invalid router JSON или model error.

Routed casino вызывается после освобождения router `gpu_lock`; сама casino operation `gpu_lock` не использует.

## Observability

Terminal `operation_outcome` не содержит пользовательский текст. Безопасные поля:

- operation=`casino`;
- invocation slash/routed/recovery;
- status/reason;
- spin id/status;
- rules version;
- stake/category/payout/net;
- terminal balance;
- duplicate/recovered flags;
- Telegram send state без exception message.

Не логировать raw request, Telegram exception text, profile facts или произвольный model output. Полный exception остаётся в обычном server log; operation event содержит только `exception_type` и stable reason.

## PR 4A: Domain и Storage

Содержание:

- новый `casino.py`;
- versioned mapping и payout contract;
- exhaustive 64-value tests;
- `chat_casino_spins` schema/indexes;
- `casino_spin_id` ledger migration;
- typed reserve/settle/refund/recovery results;
- atomic methods и injected-failure tests;
- никаких handlers, startup recovery calls или router changes.

Gate PR 4A: все migration, concurrency и projection invariants проходят на временной SQLite DB.

## PR 4B: Slash Lifecycle и Recovery

Содержание:

- общая `spin_casino(...)`;
- `/casino`;
- admission/identity checks;
- mocked reply Dice и response validation;
- void/refund semantics;
- startup recovery до polling;
- terminal outcomes;
- `Топ балансов`, `/help`, README и AGENTS;
- fake Telegram integration tests.

PR 4B нельзя выпускать без startup recovery и send-uncertainty tests.

## PR 4C: Addressed Intent

Содержание:

- `casino` в router schema/allowlist/prompt;
- explicit lexical post-guard;
- static dispatch в ту же `spin_casino(...)`;
- positive/negative/fallback tests;
- никакого handler-to-handler call.

## Обязательные тесты PR 4A

- mapping всех 64 values из независимой expected table;
- category counts `24/36/3/1`;
- RTP `90.625%`;
- invalid values, включая bool;
- новая DB и migration существующей Feature 03 DB;
- повторный `init()`;
- existing best-joke ledger/balances не меняются;
- reserve success/insufficient/duplicate/identity conflict;
- два concurrent distinct spins при balance 10;
- concurrency через два `MessageStore` instances;
- settle payout `0/5/50/250`;
- повторный settle без duplicate payout;
- refund exactly once;
- completed/refunded conflicts;
- settle/refund race;
- injected failure rollback spin/ledger/balance вместе;
- после каждого terminal transition balance projection равна сумме ledger deltas;
- recovery cutoff и повторный recovery.

## Обязательные тесты PR 4B

- denied chat, channel, bot sender, anonymous sender и invalid message id;
- insufficient balance не вызывает Telegram;
- valid `/casino` отправляет ровно один `🎰` reply;
- returned chat/id/emoji/value validation;
- incoming user Dice не settle-ит spin;
- send error/timeout/cancellation выполняют refund;
- valid Dice выполняет settlement и рендерит canonical DB result;
- final reply failure не refund-ит completed spin;
- duplicate pending/completed/refunded не отправляет второй Dice;
- forum topic/reply parameters сохраняются;
- startup recovery выполняется после init и до polling;
- storage recovery failure запрещает polling;
- leaderboard refresh best effort;
- один terminal outcome на invocation.

## Обязательные тесты PR 4C

- explicit positive Russian phrases;
- случайное упоминание casino остаётся question;
- invalid/model-error fallback не создаёт spin;
- routed operation вызывается один раз;
- slash и routed используют одну operation;
- router unload завершён до casino reserve;
- casino не захватывает `gpu_lock`;
- period/query/stake не управляются LLM output.

## Критерии готовности

- Feature 03 отдельно зафиксирована и её full suite проходит.
- Один `(chat_id, request_message_id)` создаёт максимум один spin, bet и Dice send.
- Два конкурентных spins не делают balance отрицательным.
- Reserve, settle и refund атомарно меняют spin, ledger и balance.
- Один spin имеет либо payout, либо refund, но не оба.
- Payout рассчитывается только по versioned mapping из validated Telegram `Dice.value`.
- Все 64 values покрыты independent expected tests.
- Неизвестный Telegram outcome приводит к void/refund ровно один раз.
- Startup recovery возвращает все pre-start pending stakes до polling.
- Completed payout не отменяется из-за ошибки итогового Telegram reply.
- `/top`, `/stats` и pinned `Топ балансов` отражают текущие balances.
- Slash и routed paths используют одну operation.
- Casino не использует `gpu_lock` и не исполняет model-provided delta/stake.
- В коде, UI и документации нет денег, платежей, переводов или вывода.
- Full unittest suite, compileall, pip check и diff check проходят.

## Файлы

- новый `src/tg_summary_bot/casino.py`;
- `src/tg_summary_bot/storage.py`;
- `src/tg_summary_bot/bot.py`;
- `src/tg_summary_bot/intent_router.py`;
- `src/tg_summary_bot/observability.py` при необходимости безопасных metadata;
- `README.md`, `AGENTS.md`, `/help`;
- `tests/test_casino.py`;
- `tests/test_storage_casino.py`;
- `tests/test_casino_bot.py`;
- `tests/test_intent_router.py`;
- migration/startup sequencing tests.
