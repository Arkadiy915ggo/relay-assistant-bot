# Промпт: реализация Feature 03 Joke Points and Leaderboard

```text
Работай в репозитории /home/arkadiy915/Repos/Personal/relay-assistant-bot и реализуй только Feature 03 Joke Points and Leaderboard.

Обязательный контекст перед изменениями:
1. Прочитай AGENTS.md.
2. Прочитай relay-assistant-bot-doc/Features/02.5 Pre-Feature 03 Stabilization.md.
3. Прочитай relay-assistant-bot-doc/Features/03 Joke Points and Leaderboard.md и следуй ему как основной спецификации.
4. Изучи текущие storage.py, bot.py, summarizer.py, memory.py, observability.py, intent_router.py и существующие tests.
5. Проверь git status --short. Не откатывай и не изменяй чужие изменения, если они не конфликтуют с задачей.
6. Не читай .env и data/. Для конфигурации используй .env.example и config.py.

Граница задачи:
- Реализуй Feature 03 полностью, но раздели работу на два последовательных PR-ready набора: Foundation и Integration.
- Не реализуй Feature 04, casino, ставки, payout/refund, transfers, intent casino или реальные деньги.
- Не меняй принятую Stage 02.5 политику aliases, /stats, profile sharing, surprise meme и operation outcomes, кроме минимальной совместимой интеграции Feature 03.
- Не объявляй Feature 03 завершённой до прохождения acceptance criteria и тестов.
- Не создавай commits/PR, если это отдельно не попросили. В финале явно разложи изменённые файлы по будущим Foundation PR и Integration PR.

PR 1, Foundation:
- Добавь provenance migration messages, closed eligibility predicate, raw snapshot/keyset API, append-only point ledger, balance projection, atomic award API, strict JokeSelector и page/tournament algorithm.
- Покрой foundation изолированными unit/SQLite tests.
- Не подключай пользовательские handlers до готовности foundation.

PR 2, Integration:
- Проставь provenance во всех persistence paths.
- Сохраняй только окончательный contextual assistant answer как eligible bot message.
- Подключи selector и award только к успешному /summary.
- Добавь /balance, /top top-10 и leader line в /stats.
- Обнови /help, README.md, AGENTS.md и .env.example только если действительно добавишь настройки.
- Добавь integration tests и сохрани весь существующий UX Feature 02.5 и YouTube.

Зафиксированный provenance contract:
- Добавь messages.origin TEXT NOT NULL DEFAULT 'legacy'.
- Добавь messages.kind TEXT NOT NULL DEFAULT 'legacy_unclassified'.
- Все существующие строки получают legacy/legacy_unclassified и всегда INELIGIBLE. Не делай эвристический backfill по sender, text prefix, id или timestamp.
- Единственные eligible пары:
  incoming/text
  incoming/caption
  incoming/voice_transcript
  assistant/assistant_answer
- Allowlist закрытый: все неизвестные и будущие пары ineligible.
- Generated YouTube/wiki/image/video/profile/status/summary/compare/meme/help/error text ineligible, даже если он сохранён для будущих summary/question.
- Memory blocks и synthetic StoredMessage ineligible.
- Используй один общий eligibility predicate и в snapshot query, и в transaction-time award validation.

Final assistant answer:
- Сохраняй только окончательный содержательный ответ ChatAssistant как assistant/assistant_answer.
- Thinking/status не должен становиться кандидатом.
- Используй реальный Telegram message_id первого final response/status message после edit.
- При Telegram splitting сохрани единый логический answer под первым response id; дополнительные chunks не делай отдельными eligible кандидатами.
- Slash /question и addressed question должны использовать одну operation.
- Не маркируй summary, profile или generated media/wiki output как assistant_answer.
- Ошибка сохранения answer не должна скрывать уже отправленный ответ и не должна давать award.

Raw snapshot и pagination:
- Не используй get_messages_since с character truncation для JokeSelector.
- В начале summary зафиксируй max eligible (created_at, message_id) текущего chat как snapshot boundary.
- Читай весь eligible period keyset-страницами в ORDER BY created_at ASC, message_id ASC.
- Cursor и верхняя boundary должны быть составными (created_at, message_id); offset запрещён.
- Новые сообщения после snapshot не включай.
- Ограничивай page и количеством строк, и character budget. Даже одна слишком длинная строка должна продвинуть cursor.
- Сам summary может использовать текущий memory/limit flow; joke selection обязан пройти сырой snapshot независимо от MAX_SUMMARY_INPUT_CHARS.

Strict JokeSelector JSON:
- Создай отдельный joke_awards.py с protocol/dataclasses/parser/client.
- Принимай только ровно один JSON object без fences/prose и без extra keys.
- Точная схема:
  {"has_joke":true,"source_message_id":123,"reason":"короткое объяснение"}
  или
  {"has_joke":false,"source_message_id":null,"reason":"смешной реплики нет"}
- Набор ключей ровно has_joke, source_message_id, reason.
- has_joke только JSON boolean.
- reason непустая строка максимум 300 символов.
- Для true source_message_id только integer, не bool, и только из exact candidate id set текущего prompt.
- Для false source_message_id только null.
- Не извлекай и не чини JSON regex-ом.
- Автор, participant key, quote и award amount никогда не бери из model JSON.

Page/tournament algorithm:
- Выбирай максимум одного winner на raw page.
- has_joke=false не добавляет кандидата.
- Invalid response на любой page/round завершает весь selector failure без award.
- После raw pages проведи tournament среди page winners.
- Если winners не помещаются, проводи рекурсивные rounds с теми же лимитами до одного winner или none.
- Каждый round валидирует id только против своего exact input set.
- Winner всегда ссылается на исходный StoredMessage, synthetic ids запрещены.
- Добавь progress guard: каждый round обязан уменьшать число кандидатов.

GPU contract:
- Используй существующий единый shared gpu_lock для summary generation и JokeSelector, но два последовательных захвата и отдельные LLM client objects.
- Сначала полностью заверши summary, unload его model и освободи shared gpu_lock.
- Затем заново захвати тот же shared gpu_lock для selector; unload selector client в finally до освобождения lock.
- Не вызывай selector внутри первого summary lock и не создавай второй независимый GPU lock: Ollama, vision и Whisper должны оставаться взаимно исключающимися.
- Не делай широкий рефакторинг media/Whisper lock paths.
- Timeout/model/parser/unload selector failure не отменяет готовое summary: отправь summary без award.

Ledger и balance:
- Создай append-only chat_point_ledger с entry_id, chat_id, participant_key, participant_name, delta, reason, source_message_id, created_at.
- Для Feature 03 reason только best_joke, delta только +10.
- Обязателен UNIQUE(chat_id, reason, source_message_id).
- Создай chat_point_balances с PK(chat_id, participant_key), participant_name, balance CHECK(balance >= 0), updated_at.
- award_unique_joke выполняй на одной SQLite connection под MessageStore._write_lock.
- Явно начни BEGIN IMMEDIATE.
- Внутри transaction перечитай source по chat_id/message_id и повторно проверь closed provenance allowlist.
- Participant key вычисляй существующим memory.participant_key только из source sender id/name.
- В одной transaction вставь ledger и upsert balance +10; один commit.
- Unique conflict означает already_awarded и никогда не меняет balance.
- Любая ошибка означает rollback; ledger и balance не могут разойтись.
- Не делай pre-check существования award вне transaction.
- Верни typed award result: awarded, already_awarded, ineligible/not_found с нужными read-only данными.

Summary integration:
- /compare никогда не вызывает JokeSelector и не начисляет points.
- Если summary generation failed, selector не запускается.
- После успешного summary пройди selector по snapshot.
- При valid winner вызови atomic award.
- Рендери author и exact quote только из source row; можно детерминированно обрезать quote.
- Убери/не используй свободную model-generated Best joke section как источник истины.
- Новый award: покажи validated joke и +10.
- Already awarded: покажи validated joke с пометкой, что она уже награждена, без +10.
- No joke: Лучшая шутка: Не нашлось.
- Selector/award failure: отправь summary без неподтверждённой награды, с нейтральной пометкой или без joke section.
- Не заявляй +10 до успешного commit.

Команды:
- /balance показывает balance from_user в текущем chat; отсутствующая row = 0 без создания записей.
- /top показывает максимум 10 положительных balances.
- Ordering top и leader: balance DESC, normalized participant_name ASC, participant_key ASC.
- Tie-break обязан быть детерминированным.
- /stats добавляет leader или none, но сохраняет Stage 02.5 исключение: /stats намеренно доступен вне ALLOWED_CHAT_IDS для chat-id discovery и ничего не пишет.
- /balance и /top проверяют ALLOWED_CHAT_IDS.
- Обнови /help и README, не добавляя casino.

Observability:
- Сохрани terminal operation outcome summary.
- Добавь безопасные metadata selector: snapshot boundary, page count, rounds, selector status/reason, winner id, award state.
- Не логируй raw candidate text, query, transcript или profile facts в operation event при выключенном content capture.
- Отличай selector failure от award persistence failure.

Обязательные tests Foundation:
- migration новой/legacy DB и defaults legacy/legacy_unclassified;
- allowlist принимает ровно 4 пары и отклоняет generated/unknown;
- snapshot boundary и keyset при одинаковых timestamps, без skip/duplicate;
- cursor progress на длинной строке;
- strict JSON cases, включая bool-as-int и id вне set;
- page selection, no-joke pages, multi-round tournament, invalid round abort, progress guard;
- first/duplicate/concurrent award, cross-chat/missing/ineligible source;
- rollback consistency ledger/balance;
- chat isolation и deterministic top-10 tie ordering.

Обязательные tests Integration:
- правильная provenance для incoming text/caption/voice transcript;
- ineligible provenance для YouTube/wiki/image/video/profile/status/summary/compare;
- final assistant_answer eligible, Thinking/status отсутствует;
- slash/addressed question общий path;
- summary award ровно один раз, repeat не меняет balance;
- selector invalid/timeout/model failure всё равно отправляет summary без award;
- transaction failure не показывает +10;
- /compare никогда не вызывает selector/award;
- /balance, /top, /stats leader/access;
- первый shared gpu_lock release before selector, повторный acquire, selector unload before второго unlock, отсутствие nested lock.

Проверки:
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall src

Если .venv отсутствует, используй доступный python только после проверки окружения и явно укажи это в итоге. Не запускай бот против реального Telegram и не используй реальные данные.

Acceptance criteria:
- Legacy rows не могут выиграть.
- Только четыре provenance pair eligible.
- Весь raw snapshot обрабатывается keyset pagination, независимо от summary limits.
- Model не может выбрать id вне exact prompt/snapshot/chat.
- Author/key/quote получены из source row.
- Один source получает максимум один +10 при конкурентных summaries.
- Ledger и balance атомарны под BEGIN IMMEDIATE и _write_lock.
- Selector failure не ломает summary и не начисляет points.
- /compare не начисляет points.
- /balance и top-10 chat-scoped, tie ordering deterministic.
- Feature 04 отсутствует.

В финальном ответе:
- перечисли точные изменённые файлы;
- отдельно перечисли Foundation PR и Integration PR boundaries;
- опиши migration/default для существующих баз;
- перечисли выполненные команды и их результат;
- укажи остаточные риски/непроверенные реальные интеграции;
- явно подтверди, что Feature 04 не реализована.
```
