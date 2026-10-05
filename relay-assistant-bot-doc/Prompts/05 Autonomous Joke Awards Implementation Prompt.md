# Промпт: реализация Feature 05 Autonomous Joke Awards and Bot Casino

```text
Работай в репозитории:

/home/arkadiy915/Repos/Personal/relay-assistant-bot

Реализуй Feature 05 по спецификации:

relay-assistant-bot-doc/Features/05 Autonomous Joke Awards and Bot Casino.md

Перед изменениями:

1. Прочитай AGENTS.md.
2. Прочитай Feature 03, Feature 04 и Feature 05 docs.
3. Изучи storage.py, bot.py, joke_awards.py, casino.py, config.py, intent_router.py и существующие joke/casino/handler tests.
4. Проверь git status --short, git diff и git log --oneline -10.
5. Не откатывай и не перезаписывай чужие изменения.
6. Не читай .env и data/.
7. Не запускай бот против реального Telegram.
8. Используй временные SQLite DB, fake LLM и mocked Telegram objects.
9. До edits запусти full unittest suite, compileall и git diff --check. Если baseline сломан, сначала сообщи точный блокер и не маскируй его Feature 05.

Зафиксированные продуктовые правила:

- autonomous jokes disabled by default;
- current default block = 50 eligible messages;
- current default partial block = 5-49 messages старше 3d;
- 0/1 winner per block;
- fixed +10 per winner;
- no daily cap and no award-inflation protection: более активный чат намеренно получает больше блоков и динамики;
- no_joke is terminal success;
- assistant/assistant_answer remains eligible and bot can win;
- toxicity/safety moderation is explicitly out of scope; do not reject insulting, black or targeted humor solely for content;
- strict provenance, source-id validation, prompt-injection hardening and idempotency remain mandatory;
- when autonomous mode is enabled, /summary does not select or award a joke; it only reads an already committed autonomous result;
- when disabled, preserve current /summary award fallback;
- bot participant key is id:<Telegram bot id>;
- bot leaderboard display uses first chat alias, fallback Telegram name/username/id;
- bot can spin casino through /casino bot, explicit addressed self-intent and automatic durable 25% decision after a new autonomous award;
- ordinary /casino and addressed casino without self-marker remain user spins;
- Feature 04 stake=10, mapping, payouts 0/5/50/250, RTP and void/refund semantics do not change;
- single active process remains the deployment contract.

Implement in PR-ready stages. Complete and verify each stage before the next one. Do not commit unless explicitly requested.

PR 5A: Durable Foundation

1. Add chat_joke_inbox and atomic eligible-message enqueue in the same MessageStore.save_message transaction.
2. Add bounded initial backfill for eligible allowed-chat messages, default 7d.
3. Add chat_joke_jobs plus immutable chat_joke_job_items. Range fields are audit summaries only; UNIQUE(queue_id) item rows created in the same planning transaction are the exact membership source.
4. Add chat_joke_outbox with independently claimed/fenced/retried award_notification, award_leaderboard_refresh, bot_casino and casino_leaderboard_refresh actions so terminal jobs cannot lose post-finalize work after a crash or miss the second balance refresh.
5. Add nullable chat_point_ledger.joke_job_id and partial unique (joke_job_id).
6. Preserve existing unique (chat_id, reason, source_message_id).
7. Preserve configurable planner defaults: full blocks of 50; partial 5-49 only after 3d; fewer than 5 remain open. Historical 20/24h jobs keep their original policy.
8. Implement claim/reclaim with BEGIN IMMEDIATE, opaque lease-token fencing and CAS lease renewal. Abort immediately on lost ownership. Validate lease > one selector timeout + 60 seconds.
9. Implement exact job-message reads through immutable job items and canonical messages rows.
10. Refactor joke award transaction into a private connection-level helper used by both current manual award and autonomous finalize.
11. Finalize job + best_joke ledger + balance + canonical winner snapshot + required outbox rows in one transaction. LLM must never run inside it.
12. Handle terminal no_joke, awarded, already_awarded and source_invalid idempotently.
13. Add config fields and validation, but do not start worker in 5A.
14. Add exhaustive storage/migration/race/rollback tests from Feature 05.

PR 5B: Shadow Worker and Summary Decoupling

1. Add src/tg_summary_bot/autonomous_jokes.py.
2. Add worker startup grace, planning, one-job claim, selector execution, retry/backoff, independently claimed outbox processing and graceful shutdown. Apply the current allowlist/disabled-chat policy to both job and outbox claims, including old queued work. Retry activation and initial backfill failures inside the worker loop.
3. Keep strong task references, report unexpected task failures, and await/cancel worker in main try/finally.
4. Use the same shared GPU coordination as all Ollama/Whisper flows. Never create an independent lock.
5. A background acquisition performs at most one model batch call, releases between pages and does not hold GPU while doing SQLite, Telegram send or sleep.
6. If strict foreground priority cannot be guaranteed with asyncio.Lock, add a small foreground/background GpuScheduler and migrate call sites deliberately; do not rely on lock.locked() while claiming strict priority.
7. Refactor JokeSelector to select from a fixed canonical list while preserving strict JSON, prompt-local IDs, row/char budgets and tournament progress.
8. Serialize candidates as JSON. Add replied message and up to two predecessor context rows without changing target award identity.
9. Use a dedicated autonomous judge client with fallback to the main summary model.
10. Implement shadow mode as default: jobs reach shadow terminal statuses, but no ledger/balance, award announcement or automatic casino changes occur.
11. When enabled and not shadow, commit +10 and outbox rows atomically, then process announcement and leaderboard refresh through durable post-finalize claims.
12. When autonomous mode is enabled, remove selector/award side effects from /summary and show a read-only committed autonomous winner in the requested period.
13. Preserve old /summary behavior when feature is disabled and preserve /compare no-award behavior always.
14. Add lifecycle, cancellation, GPU sequencing, retry, announcement and handler tests.

PR 5C: Bot Identity and Casino

1. Cache Telegram bot identity from getMe.
2. Use stable id:<bot_id> participant key.
3. Resolve current leaderboard display from the first chat alias before tie sorting and limit; /top, /stats and pinned board share this ordering. Fallback to Telegram full name/username/id.
4. Do not use alias as identity key and do not split balance when aliases change.
5. Migrate chat_casino_spins to a trigger identity contract: nullable positive request_message_id, required trigger_kind/trigger_key, a cross-field CHECK requiring request IDs for requested kinds and NULL for automatic kind, unique (chat_id, trigger_key), and partial unique real request message IDs. Preserve spin_id and all existing rows through a transactional table-copy migration; backfill user-message:<id> keys.
6. Build trigger kind/key only from a typed trigger descriptor inside domain helpers. Existing replay must validate trigger kind, optional request id and participant identity before returning canonical state; callers never pass raw trigger_key.
7. Extract a normal async casino domain operation from the human aiogram handler. Do not create synthetic Message and do not call handlers from worker.
8. Preserve human admission including from_user.is_bot rejection in the human handler.
9. Add /casino bot and strict addressed casino_bot intent. Require a lexical self-marker such as себе/сам/сама/свой after model routing; without it run ordinary user casino.
10. Requested bot spin debits only bot balance and uses trigger bot-request:<message_id>.
11. If BOT_AUTO_CASINO_ENABLED is false, do not call RNG or create bot-casino outbox. Otherwise sample inside new-award finalize and persist the decision; production default chance is 0.25. A rolled-back finalize may sample again because no decision exists, but retries after a committed decision never resample. Create a bot_casino outbox action only for spin decisions.
12. During PR 5C migration mark all older terminal jobs with NULL bot decision as skip; never spin retroactively.
13. If bot balance is below stake, store terminal insufficient_balance and skipped outbox result. Do not retry it later.
14. Automatic spin uses trigger joke-job:<job_id>, request_message_id=NULL, Bot.send_dice and the existing trusted Dice validation, reserve/settle/refund and void contract. Its casino ledger rows use deterministic negative source_message_id=-spin_id while casino_spin_id remains the domain link. Terminal settlement/refund, bot_casino outbox completion and creation of casino_leaderboard_refresh commit in one transaction.
15. Automatic casino never takes gpu_lock.
16. Add bot alias/identity, requested/automatic, outbox recovery, idempotency, insufficient balance, Telegram failure/refund and leaderboard tests.

Do not implement PR 5D small-model prefilter in this session unless the user explicitly asks after reviewing shadow metrics. The existing page/tournament selector is the first rollout. Keep the future extension points from the spec, but do not add speculative candidate tables or model configuration now.

Critical invariants:

- message + inbox commit together;
- exact non-overlapping job membership;
- one terminal result and at most one award per job;
- one best_joke award per source across manual/autonomous flows;
- job + ledger + balance commit together;
- model output never supplies author, identity, quote, delta or payout;
- bot alias is display only;
- bot casino uses the same trusted Telegram Dice and transaction contracts as human casino;
- Telegram/history/notification limitations are documented honestly;
- no raw chat content in operational logs when content capture is disabled.

Verification after each stage and at the end:

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/opencode/relay-assistant-pycache \
  .venv/bin/python -m compileall -q src
git diff --check
git status --short

Update .env.example, README.md, AGENTS.md, Feature Roadmap and Agent Handoffs for the implemented behavior. In the final response report:

- stage-by-stage changes;
- schema migrations and compatibility with existing DB;
- exact worker/restart semantics;
- GPU scheduling behavior and remaining priority limitations;
- /summary behavior enabled vs disabled;
- bot participant/alias/casino behavior;
- tests and commands run;
- any deferred PR 5D work or residual Telegram limitations.
```
