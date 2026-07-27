# Feature 03: Joke Points and Leaderboard

## Статус

Реализована в текущем worktree. Перед Feature 04 реализация должна пройти полный test suite и быть зафиксирована отдельным коммитом, чтобы casino migration не смешивалась с изменениями provenance, selector и award flow.

## Зависимость

Обязательна завершённая [[02.5 Pre-Feature 03 Stabilization]]. Feature 03 опирается на terminal operation outcomes, best-effort persistence и зафиксированные исключения Stage 02.5. Feature 04 зависит от завершённой Feature 03, но казино в эту работу не входит.

## Цель

После успешного `/summary` выбрать одну реальную лучшую шутку из допустимых сырых сообщений snapshot-а, показать проверенные автора и цитату и один раз начислить автору `+10` виртуальных очков в текущем чате. Добавить `/balance`, top-10 `/top` и лидера в `/stats`.

`/compare` никогда не выбирает победителя и не начисляет очки.

## Неподвижные продуктовые правила

- Награда: ровно `+10` за новую уникальную winning source message.
- Одна исходная реплика получает награду не более одного раза в одном чате за всю историю.
- Балансы изолированы по `chat_id`, не переводятся и не имеют денежной ценности.
- Автор, имя, цитата и participant key берутся только из сохранённой строки `messages`, а не из JSON модели.
- Memory blocks, summary text и любые generated context rows не могут быть источником награды.
- Если смешной допустимой реплики нет, показывается `Не нашлось`, ledger не меняется.
- Если selector сломан, timeout-нулся или вернул невалидный ответ, обычное саммари всё равно отправляется без награды.
- Повторный summary может снова выбрать уже награждённую реплику, но atomic storage method вернёт `already_awarded` и не изменит balance.

## Разделение на два PR

Feature 03 реализуется двумя последовательными PR. Integration PR нельзя начинать до принятия Foundation PR.

### PR 1: Foundation

- provenance schema/migration и закрытый eligibility predicate;
- raw snapshot/keyset storage API;
- point ledger, balance projection и атомарные storage methods;
- `JokeSelector`, strict parser, page/tournament algorithm;
- изолированные unit/SQLite tests без изменения пользовательских handlers.

### PR 2: Integration

- маркировка всех входящих и исходящих persisted rows корректной provenance pair;
- selective persistence финального `assistant_answer`;
- `/summary` -> selector -> atomic award -> validated rendering;
- две последовательные фазы захвата существующего общего `gpu_lock` для summary и selector;
- `/balance`, `/top`, leader line в `/stats`, `/help`, README и config docs при необходимости;
- integration tests, включая selector failure и запрет awards в `/compare`.

Каждый PR должен быть самостоятельно проверяемым. Foundation не должен тихо менять текущий UX; Integration не должен менять schema, не покрытую Foundation tests.

## Provenance Contract

### Новые поля `messages`

Добавить через idempotent `_ensure_column` два `NOT NULL` поля:

| Поле | Default для legacy rows | Назначение |
| --- | --- | --- |
| `origin` | `legacy` | Кто/какой pipeline создал строку. |
| `kind` | `legacy_unclassified` | Семантический тип текста. |

Существующие строки после миграции получают пару `legacy/legacy_unclassified` и всегда **INELIGIBLE**. Нельзя угадывать их происхождение по текстовым prefix, sender id, положительному message id или времени.

### Закрытый allowlist eligible pairs

Кандидатами являются только эти точные пары:

| `origin` | `kind` | Пример |
| --- | --- | --- |
| `incoming` | `text` | Обычное входящее Telegram text message. |
| `incoming` | `caption` | Caption входящего photo/video/document. |
| `incoming` | `voice_transcript` | Финальный сохранённый transcript входящего voice/audio. |
| `assistant` | `assistant_answer` | Финальный содержательный ответ contextual assistant-а. |

Любая пара вне списка ineligible, включая неизвестные будущие значения. Eligibility должна быть одним общим predicate/SQL clause, используемым snapshot query и award validation.

Явно ineligible:

- `legacy/legacy_unclassified`;
- YouTube recognition text;
- Wikipedia results;
- image OCR/recognition;
- Telegram video recognition и video-note auto recognition;
- profile output, corrections/status text и memory-generated profile context;
- status placeholders (`Thinking...`, `Collecting...`, download/recognition status);
- summary и partial summary text;
- `/compare` output;
- meme captions/alt text;
- errors, warnings, help/stats и command responses;
- memory blocks и synthetic messages.

Успешное сохранение строки «для саммари» не означает eligibility. Generated wiki/image/video/YouTube text остаётся полезным контекстом, но не кандидатом на очки.

### Final `assistant_answer`

Только окончательный текст ответа `ChatAssistant` может быть сохранён как `assistant/assistant_answer`. Status `Thinking...` никогда не сохраняется как кандидат. Использовать реальный Telegram `message_id` первого final response/status message после edit. Если ответ разбит на несколько Telegram сообщений, в `messages` под id первого response сохраняется единый финальный логический answer text с обычным лимитом хранения; дополнительные chunks отдельно eligible не становятся.

Не сохранять как `assistant_answer` summary, profile, wiki/image/video result, router output или сообщение об ошибке. Slash `/question` и addressed question используют одну общую selective-persistence operation. Ошибка этой записи не должна скрывать уже отправленный ответ и не должна превращаться в award.

## Raw Snapshot и Keyset Pagination

Selector не использует `get_messages_since(..., limit_chars=...)`, memory blocks или offset pagination.

В начале summary integration фиксируется верхняя граница raw snapshot для текущего чата: максимальная eligible key `(created_at, message_id)` на момент запроса. Нижняя граница задаётся parsed summary period. Все страницы читаются с условиями:

```sql
chat_id = :chat_id
AND created_at >= :since
AND (created_at < :snapshot_created_at
     OR (created_at = :snapshot_created_at AND message_id <= :snapshot_message_id))
AND (created_at > :after_created_at
     OR (created_at = :after_created_at AND message_id > :after_message_id))
AND <closed eligible provenance predicate>
ORDER BY created_at ASC, message_id ASC
LIMIT :page_size
```

Для первой страницы `after` отсутствует. Snapshot boundary и cursor являются составными; нельзя пагинировать только по timestamp. Новые сообщения после snapshot не попадают в текущий турнир. Page API возвращает только реальные `StoredMessage` и следующий key; offset не используется. Размер page ограничивается одновременно количеством строк и безопасным character budget, но первая слишком длинная строка должна продвигать cursor, чтобы исключить бесконечный цикл.

`/summary` может продолжать строить сам текст по текущему memory/limit пути. JokeSelector отдельно проходит весь eligible raw snapshot и не наследует `MAX_SUMMARY_INPUT_CHARS`.

## Strict `JokeSelector` JSON

Новый `joke_awards.py` содержит protocol, immutable result dataclass, parser и selector client. Модель возвращает ровно один JSON object без Markdown, prefix/suffix и дополнительных ключей.

Положительный ответ:

```json
{"has_joke":true,"source_message_id":123,"reason":"короткое объяснение"}
```

Отрицательный ответ:

```json
{"has_joke":false,"source_message_id":null,"reason":"смешной реплики нет"}
```

Валидация deny-by-default:

- набор ключей должен быть ровно `has_joke`, `source_message_id`, `reason`;
- `has_joke` обязан быть JSON boolean;
- `reason` обязан быть непустой строкой не длиннее 300 символов;
- при `has_joke=true` id обязан быть JSON integer, но не boolean, и входить в exact candidate id set текущего вызова;
- при `has_joke=false` id обязан быть `null`;
- array/scalar, fences, prose, unknown key, duplicate/concatenated JSON, неверный type и id вне candidate set дают invalid result;
- selector JSON никогда не содержит автора, цитату, participant key или размер награды;
- parser не пытается чинить/извлекать JSON регулярным выражением.

LLM prompt должен требовать выбирать только явно смешную реплику, не награждать нейтральный текст из необходимости и считать `has_joke=false` нормальным исходом.

## Page/Tournament Selection

1. Прочитать следующую raw snapshot page.
2. Передать selector-у numbered candidates с source ids и текстом.
3. Строго провалидировать page winner. Invalid page response завершает весь selector как failure, без частичной награды.
4. `has_joke=false` не добавляет кандидата; `true` добавляет исходный `StoredMessage`, найденный по validated id.
5. После всех raw pages провести tournament только среди page winners.
6. Если winners не помещаются в один prompt, разбивать их тем же page budget и проводить следующие rounds, пока не останется один winner или ни одного.
7. Каждый round валидирует id только против exact set своего prompt. Winner всегда остаётся ссылкой на исходную строку, а не synthetic candidate.
8. Защититься от отсутствия прогресса: round обязан уменьшать количество кандидатов; неправильная конфигурация page size/budget завершает selector failure.

Reason можно логировать как технический selector metadata только при разрешённом content capture; award и UI от reason не зависят.

## GPU Phases

Summary generation и JokeSelector используют существующий единый shared `gpu_lock`, но два последовательных захвата и отдельные LLM client objects. Это сохраняет общую взаимную исключаемость Ollama, vision и Whisper.

- Summary полностью завершает generation/unload и освобождает shared `gpu_lock` до запуска selector.
- Selector повторно захватывает тот же shared `gpu_lock`, выполняет все page/tournament calls последовательно и выгружает свой client в `finally` до освобождения lock.
- Не вызывать selector из первого summary lock и не создавать отдельный независимый GPU lock.
- Остальные существующие media/Whisper lock paths не рефакторить шире необходимого в Feature 03.
- Selector timeout/model/parser/unload error не отменяет готовое summary: записать failure outcome/metadata, отправить summary без winner и без award.

## Ledger и Balance Projection

### `chat_point_ledger`

Неизменяемый append-only журнал:

| Поле | Контракт |
| --- | --- |
| `entry_id` | `INTEGER PRIMARY KEY AUTOINCREMENT`. |
| `chat_id` | Scope очков. |
| `participant_key` | Результат существующего `memory.participant_key`. |
| `participant_name` | Имя из winning source row на момент награды. |
| `delta` | Для Feature 03 всегда `10`. |
| `reason` | Для Feature 03 только `best_joke`. |
| `source_message_id` | Непустой реальный id winning source. |
| `created_at` | UTC ISO timestamp. |

Обязателен `UNIQUE(chat_id, reason, source_message_id)`. Ledger rows не обновляются и не удаляются обычным API.

### `chat_point_balances`

Проекция:

| Поле | Контракт |
| --- | --- |
| `chat_id`, `participant_key` | Составной primary key. |
| `participant_name` | Последнее имя, подтверждённое award source. |
| `balance` | `INTEGER NOT NULL CHECK(balance >= 0)`. |
| `updated_at` | UTC ISO timestamp. |

### Atomic award

`award_unique_joke(chat_id, source_message_id, awarded_at)` выполняется под существующим `MessageStore._write_lock` на одной connection:

1. `BEGIN IMMEDIATE`;
2. перечитать source row по `(chat_id, message_id)`;
3. повторно проверить closed provenance eligibility внутри транзакции;
4. вычислить `participant_key` только из source sender id/name;
5. вставить ledger `best_joke/+10`;
6. если unique conflict, rollback/commit без balance update и вернуть `already_awarded`;
7. upsert balance `balance = balance + 10`, обновив participant name/time;
8. commit;
9. при любом исключении rollback и проброс безопасной storage error вверх.

Ledger insert и balance update никогда не выполняются через две connection или два commit. Нельзя сначала проверять существование award отдельным SELECT вне transaction. `BEGIN IMMEDIATE` плюс unique constraint являются защитой от конкурентных summaries.

Метод возвращает typed result: `awarded` с новым balance/participant, `already_awarded` с текущим balance или `ineligible/not_found`. Integration рендерит `+10` только для `awarded`.

## Summary Integration и Rendering

Текущая свободная секция `Best joke` в summarizer prompt не является источником истины. Убрать её из model-generated summary либо удалить её перед отправкой и всегда добавить одну детерминированную секцию из validated selector result.

Порядок `/summary`:

1. parse/access checks и фиксация raw eligible snapshot;
2. обычная summary generation;
3. если summary generation failed, selector и award не запускаются;
4. после освобождения summary lock запустить JokeSelector по snapshot;
5. при valid winner вызвать atomic award;
6. собрать секцию из source row: escaped participant name, точная исходная quote (допустимо детерминированно обрезать), и award state;
7. отправить summary даже при selector/storage award failure, но не заявлять `+10`, если transaction не завершилась;
8. schedule profile refresh сохраняет текущее поведение.

Рендеринг:

- новый award: `Лучшая шутка: <author>: «<exact quote>» (+10 очков)`;
- winner уже награждён: та же проверенная цитата с пометкой `уже была награждена`, без `+10`;
- `has_joke=false`: `Лучшая шутка: Не нашлось.`;
- selector/award failure: summary отправляется с нейтральной строкой `Лучшая шутка: не удалось определить; очки не начислены.` либо без секции, но никогда с неподтверждённой наградой.

Цитата берётся из source text, не из reason модели. Telegram escaping/splitting обязателен.

`/compare` сохраняет текущий compare flow: модели могут вывести текстовые сравнения, но JokeSelector, ledger и balance API там не вызываются ни при каких условиях.

## Команды

### `/balance`

- Проверяет `ALLOWED_CHAT_IDS` как обычная команда.
- Использует реального `from_user`; при отсутствии пользователя возвращает понятный отказ.
- Participant key вычисляется существующим helper.
- Отсутствующая balance row означает `0`, но не создаёт ledger/balance запись.
- Показывает баланс только в текущем чате.

### `/top`

- Показывает максимум 10 участников с положительным балансом.
- Порядок детерминирован: `balance DESC`, затем нормализованное display name `ASC`, затем `participant_key ASC`.
- Места нумеруются 1-10. При равных balance tie не меняет порядок между запросами.
- Пустой ledger даёт короткое сообщение об отсутствии наград.

### `/stats`

Добавляет leader line: имя и balance первого участника по тому же ordering либо `none`. Сохраняется исключение Stage 02.5: `/stats` доступен вне `ALLOWED_CHAT_IDS` для discovery и не создаёт/не меняет очки.

Обновить `/help` и README. Не добавлять `/casino`.

## Наблюдаемость

- Summary outcome остаётся terminal outcome пользовательской операции.
- Добавить metadata: snapshot boundary, eligible rows/pages, tournament rounds, selector status/reason, winner id, award state, без raw candidate text при выключенном content capture.
- Не логировать profile facts, полные шутки или prompt content в `tg_summary_bot.operations`.
- Selector failure и award persistence failure различаются; оба дают summary без ложного начисления.

## Тестовый план PR 1

- Migration новой и legacy DB; legacy defaults строго `legacy/legacy_unclassified`.
- Closed allowlist принимает ровно четыре пары и отклоняет generated/unknown pairs.
- Raw snapshot не включает новые строки после boundary.
- Keyset корректен при одинаковом `created_at`, не пропускает/не дублирует ids и продвигается на длинной строке.
- Strict JSON: success/none, fences/prose/array/scalar, extra/missing keys, bool-as-int, wrong types, id вне prompt.
- Page winners и multi-round tournament сохраняют исходный id; no-joke pages; invalid round abort.
- Atomic award: first insert, duplicate, missing/ineligible/cross-chat source, parallel duplicate attempts.
- Ledger и balance rollback вместе при injected failure.
- Балансы изолированы по chat.
- Top-10 ordering детерминирован при tie.

## Тестовый план PR 2

- Incoming text/caption/voice transcript получают правильные eligible pairs.
- Wiki/image/video/YouTube/profile/status/summary/compare получают ineligible pairs.
- Только final contextual assistant answer сохраняется как `assistant/assistant_answer`; `Thinking...` отсутствует.
- Slash и addressed question используют общий persistence path.
- Summary request row исключается из summary context по существующему правилу, но provenance остаётся корректной.
- Summary success + new winner даёт одну ledger row и `+10`.
- Repeat summary не меняет balance.
- Selector invalid JSON/timeout/model error всё равно отправляет summary без award.
- Award transaction failure не показывает `+10`.
- `/compare` не вызывает selector/storage award.
- `/balance`, `/top`, `/stats` leader и access behavior.
- Первый shared GPU lock освобождён до selector; затем тот же lock захвачен повторно, а selector unload выполнен до второго освобождения.
- Full unittest suite и compileall.

## Критерии готовности

- Feature 02.5 завершена и не регрессировала.
- Legacy rows всегда ineligible без эвристического backfill.
- Только четыре provenance pair могут попасть в selector и пройти transaction-time validation.
- Selector обходит весь raw eligible snapshot keyset-страницами, независимо от summary character limit.
- Модель не может наградить id вне текущего prompt/snapshot/chat.
- Автор, participant key и quote совпадают с source row.
- Один `chat_id + best_joke + source_message_id` начисляется максимум один раз при конкурентных вызовах.
- Ledger и balance изменяются одной `BEGIN IMMEDIATE` транзакцией под `_write_lock`.
- Selector failure не мешает отправке готового summary и не начисляет очки.
- `/compare` не начисляет очки.
- `/balance` и top-10 работают chat-scoped; tie ordering стабилен.
- Summary и selector используют один shared `gpu_lock` двумя последовательными фазами и отдельные client objects без вложенного захвата.
- Документация не объявляет Feature 04 реализованной.

## Явно не входит

- Feature 04 casino и intent `casino`;
- ставки, payout/refund, transfers, purchases и реальные деньги;
- награды за старые `legacy` строки;
- награды за generated YouTube/wiki/image/video/profile/status/summary text;
- выбор winners из memory blocks;
- backfill очков из старых summary `Best joke` sections;
- ручная выдача/редактирование баланса;
- admin-only aliases toggle;
- изменение Stage 02.5 surprise meme policy;
- awards из `/compare`.

## Файлы реализации

- `src/tg_summary_bot/storage.py`
- `src/tg_summary_bot/bot.py`
- `src/tg_summary_bot/summarizer.py`
- новый `src/tg_summary_bot/joke_awards.py`
- при необходимости `src/tg_summary_bot/config.py`, `.env.example`, `doctor.py` только для явных selector settings
- `README.md`, `AGENTS.md`
- новые focused tests для provenance, snapshot pagination, selector, ledger и integration.
