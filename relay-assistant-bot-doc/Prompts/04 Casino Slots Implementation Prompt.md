# Промпт: реализация Feature 04 Casino Slots

```text
Работай в репозитории:

/home/arkadiy915/Repos/Personal/relay-assistant-bot

Реализуй только Feature 04 Casino Slots по спецификации:

relay-assistant-bot-doc/Features/04 Casino Slots.md

Перед изменениями:

1. Прочитай AGENTS.md.
2. Прочитай Stage 02.5, Feature 03 и Feature 04 docs.
3. Изучи storage.py, bot.py, intent_router.py, observability.py и Feature 03 tests.
4. Проверь git status --short и git log --oneline -10.
5. Не откатывай и не перезаписывай чужие изменения.
6. Не читай .env и data/.
7. Не запускай бот против реального Telegram.
8. Используй временные SQLite DB, fakes и mocked Telegram objects.

Preflight gate:

- Feature 03 должна быть завершена и отдельно зафиксирована.
- Full test suite должен проходить до casino edits.
- Unknown IntegrityError в joke award не должен считаться duplicate success.
- Joke award должен использовать transaction-validated canonical source.
- Feature 03 handler-level tests должны покрывать summary award/repeat/failure, /compare и shared gpu_lock sequencing.
- Если preflight не выполнен, остановись и перечисли блокеры. Не смешивай Feature 03 fixes с casino commits.

Не входит:

- реальные деньги;
- платежи, покупки и вывод;
- transfer points;
- произвольные ставки;
- другие игры и внешние RNG;
- multi-instance leases;
- ручное изменение balances;
- отдельный lifetime joke leaderboard.

Зафиксированные правила:

- stake = 10;
- rules version = telegram_slots_base4_v1;
- gross payouts: none=0, pair=5, triple=50, jackpot=250;
- RTP = 90.625%;
- jackpot только seven/seven/seven, Dice.value=64;
- /top и pinned leaderboard показывают текущие spendable balances;
- pinned title = Топ балансов;
- unknown/uncommitted Telegram outcome = void + refund;
- completed spin имеет bet + payout, включая payout=0;
- refunded spin имеет bet + refund;
- payout и refund взаимно исключаются;
- single active process only;
- casino не использует gpu_lock.

Реализуй три последовательных PR-ready этапа. Не смешивай их без необходимости.

PR 4A: Domain и Storage

Создай src/tg_summary_bot/casino.py.

Domain mapping:

CASINO_RULES_VERSION = "telegram_slots_base4_v1"
CASINO_STAKE = 10
CASINO_SYMBOLS = ("bar", "grape", "lemon", "seven")

n = value - 1
left = n % 4
middle = (n // 4) % 4
right = (n // 16) % 4

Validation:

- type(value) is int;
- range 1..64;
- bool/float/string/0/65 rejected;
- jackpot checked before triple;
- payout derived only from category/rules version.

Sentinels:

- 1 = bar/bar/bar = triple = 50;
- 22 = grape/grape/grape = triple = 50;
- 43 = lemon/lemon/lemon = triple = 50;
- 64 = seven/seven/seven = jackpot = 250.

Independent tests must enumerate all 64 expected categories/payouts and assert counts 24/36/3/1. Do not generate expected values by calling or duplicating the production decoder.

Add chat_casino_spins with the exact schema/invariants from Feature 04 doc:

- spin_id;
- chat_id/request_message_id unique;
- participant key/name;
- rules version/stake;
- dice message/value;
- category/payout;
- pending/completed/refunded;
- terminal balance/reason;
- created/updated/completed/refunded timestamps.

Add nullable casino_spin_id to chat_point_ledger via safe idempotent migration.

Add partial unique index:

(chat_id, reason, casino_spin_id) WHERE casino_spin_id IS NOT NULL

For casino ledger rows:

- source_message_id = request_message_id;
- casino_spin_id = spin_id;
- reasons only casino_bet/casino_payout/casino_refund;
- caller never supplies arbitrary delta/reason/payout.

Do not add public adjust_balance(delta, reason).

Implement typed methods:

- reserve_casino_spin;
- settle_casino_spin;
- refund_casino_spin;
- refund_pending_casino_spins.

Every mutation:

- uses one connection;
- holds MessageStore._write_lock;
- starts BEGIN IMMEDIATE;
- commits spin + ledger + balance together;
- rolls back everything on any unexpected error.

Reserve contract:

1. Check existing by chat/request id inside transaction.
2. Duplicate never debits again.
3. Identity mismatch is invariant failure.
4. Insert pending and obtain spin id.
5. Conditional UPDATE balance WHERE balance >= 10.
6. Zero affected rows means rollback/insufficient balance.
7. Insert casino_bet=-10.
8. Commit canonical balance after debit.

Never use get_balance -> check -> later debit.

Settle contract:

1. Only pending can become completed.
2. Completed is idempotent replay.
3. Refunded cannot settle.
4. Accept only spin id, dice message id and dice value.
5. Domain computes category and payout.
6. Insert exactly one casino_payout, including delta=0.
7. Credit payout.
8. Save canonical dice/category/payout/terminal balance.
9. Commit pending -> completed.

Refund contract:

1. Only pending can become refunded.
2. Refunded is idempotent replay.
3. Completed cannot refund.
4. Insert exactly one casino_refund=+10.
5. Credit stake and save terminal balance/reason.
6. Commit pending -> refunded.

Settle/refund race must produce exactly one terminal state.

PR 4A tests:

- new DB and Feature 03 DB migration;
- repeated init;
- best_joke rows unchanged;
- all 64 mapping values;
- invalid mapping values;
- reserve success/insufficient/duplicate/identity conflict;
- concurrent spins at balance 10;
- two MessageStore instances;
- all payout classes;
- duplicate settle/refund;
- settle/refund race;
- injected rollback failures;
- projection equals ledger sum;
- recovery cutoff and repeated recovery.

PR 4B: Slash Lifecycle и Recovery

Create one ordinary async operation spin_casino(...).

Admission:

- allowed chat;
- not channel;
- real from_user;
- from_user.is_bot is false;
- sender_chat is None;
- message_id > 0.

Flow:

1. Reserve.
2. Existing pending/completed/refunded returns replay without new Dice.
3. Insufficient balance does not call Telegram.
4. New pending sends message.reply_dice(emoji="🎰").
5. Validate returned chat, message id, dice object, emoji and true int value 1..64.
6. Explicit send failure or uncertain timeout performs idempotent refund.
7. Cancellation attempts asyncio.shield(refund), then re-raises.
8. Valid response calls settlement.
9. Final result is rendered only from canonical DB result.
10. Final Telegram text failure does not refund completed payout.
11. Best-effort refresh Топ балансов.
12. Return one terminal OperationOutcome.

Distributed-window contract:

If Telegram may have shown Dice but committed settlement is absent, spin is void and stake is returned. A visible uncommitted animation has no economic force. Do not claim exactly-once Telegram delivery.

Duplicate UX:

- pending: spin already processing;
- completed: replay saved result, no new Dice;
- refunded: report void/refund, no new Dice;
- identity mismatch: reject and log stable invariant code.

Startup recovery:

1. await store.init();
2. capture startup_cutoff;
3. refund all pending created before cutoff;
4. obtain affected chat ids;
5. best-effort refresh leaderboards;
6. only then start polling.

If storage recovery fails, fail startup closed. Do not accept new bets.

Leaderboard semantics:

- /balance = current wallet;
- /top = top-10 current positive wallets;
- /stats leader uses same ordering;
- pinned title = Топ балансов;
- refresh after joke award, completed spin, refund and recovery;
- transient/message-not-modified edit errors do not recreate pinned message;
- only confirmed not-found recreates it.

PR 4B tests:

- all admission rejections;
- insufficient balance no Telegram call;
- exactly one reply Dice;
- forum topic/reply parameters;
- returned response validation;
- user incoming Dice ignored;
- send error/timeout/cancellation refund;
- valid settlement/result;
- final reply error leaves completed;
- duplicate state replay;
- startup ordering init -> recovery -> polling;
- recovery failure prevents polling;
- leaderboard refresh best effort;
- one terminal outcome, no raw content.

PR 4C: Addressed Intent

Add casino to Action/allowlist/prompt only now.

Casino route requires both:

- action verb: крути/крутань/прокрути/запусти/сыграй;
- object: казино/слот/слоты.

Positive examples:

- Реле, прокрути казино;
- Реле, крутань слот;
- Реле, сыграй в слоты.

Negative examples:

- что ты думаешь о казино?;
- объясни правила слотов;
- вчера было казино;
- invalid/model-error fallback.

period/query for casino must be null. LLM cannot set stake, payout or delta.

Dispatch calls the same spin_casino operation after router unload/unlock. Never call a handler from another handler. Casino never acquires gpu_lock.

PR 4C tests:

- positive/negative phrases;
- invalid fallback has no side effect;
- one classification and one operation;
- same operation for slash/routed;
- router unlock precedes reserve;
- no gpu_lock acquisition;
- model fields cannot alter economy.

Documentation:

- update /help, README, AGENTS, roadmap and handoff;
- explicitly say virtual/no money/no transfer/no withdrawal;
- document stake, gross payout, net payout and RTP;
- document community mapping/rules version risk;
- document void/refund uncertainty semantics;
- rename pinned UI to Топ балансов.

Verification after each PR-ready phase:

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v

PYTHONPYCACHEPREFIX=/tmp/opencode/relay-assistant-pycache \
  .venv/bin/python -m compileall -q src

.venv/bin/python -m pip check
git diff --check
git status --short

Do not create commits unless explicitly requested. If commits are requested, inspect status, diff and log first and stage only the intended phase.

Final response must include:

- exact changed files;
- PR 4A/4B/4C boundaries;
- migration behavior for existing Feature 03 DB;
- mapping version and all payout classes;
- transaction/recovery guarantees;
- commands and test results;
- residual Telegram distributed-window limitation;
- explicit confirmation that there are no real-money/payment/transfer features.
```
