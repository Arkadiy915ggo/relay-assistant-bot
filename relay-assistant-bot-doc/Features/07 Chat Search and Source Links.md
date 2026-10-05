# Feature 07: Chat Search and Source Links

## Статус

Идея и базовый дизайн, 2026-10-03. Команды поиска по сообщениям/файлам пока нет. `/question` отвечает по контексту, но не является поиском с воспроизводимым списком источников.

Связанные фичи: [[08 Files and Document Understanding]], [[12 Product UX and Portfolio]].

## Проблема и гипотеза

В чате трудно найти нужное сообщение, ссылку, файл, обещание или число. Бот должен помогать возвращаться к оригиналу, а не только пересказывать память.

Гипотеза: короткий список результатов с цитатой, датой и ссылкой сокращает время поиска и повышает доверие к ответам.

## Предлагаемый MVP

- Явный запрос вида `/search <текст>` или «Реле, найди сообщение про …»; команда предварительная.
- Поиск только в текущем разрешённом чате и по тому, что бот действительно сохранил.
- Первые 5 результатов: автор, дата, краткий фрагмент, тип источника и ссылка на исходное сообщение.
- Literal/phrase search по messages; captions и имеющиеся OCR/transcripts учитывать с явной меткой происхождения.
- Файлы сначала искать по имени/caption/metadata после добавления общего file index. Поиск внутри документа зависит от Feature 08.
- Если ничего не найдено, сообщить это и предложить уточнить запрос.

Второй этап: фильтры автора/периода/типа, pagination, семантический поиск, объединение дубликатов и ответы по найденным источникам.

## Source-link contract

- Для public supergroup/channel с username: `https://t.me/<username>/<message_id>`.
- Для подходящего private supergroup/channel: `https://t.me/c/<internal_chat_id>/<message_id>`; ID преобразовывать только для подтверждённого Telegram chat type.
- Для private dialogs/basic groups универсальная ссылка недоступна: возвращать понятную reference и согласованный fallback вместо выдуманного URL.
- Thread/topic metadata учитывать в последующем UX; базовая ссылка должна вести к реальному source message.
- Ссылка не даёт доступ к закрытому чату: получатель должен быть его участником.
- Bot API не предоставляет произвольное чтение истории или универсальный `getMessage`. Удалённое сообщение/сменившийся username могут сделать результат устаревшим; не обещать live-validation каждого hit.

## Минимальная архитектура

```text
search request → validated query/filters → chat-scoped SQLite search
               → SearchHit(source_chat_id, source_message_id, excerpt, origin, metadata)
               → deterministic source-link rendering
```

- Вынести search в focused service, не смешивать с memory rollups и answer generation.
- Для text MVP рассмотреть SQLite FTS5 с простым fallback при отсутствии FTS. Existing memory/profile FTS не заменяет индекс raw messages.
- Сначала находить source IDs, затем при необходимости использовать LLM для переформулировки/ответа. Модель не сочиняет ссылки и не выбирает чужой chat ID.
- При indexed generated result хранить связь с оригинальным media/file source, чтобы ссылка вела к нему, а не только к служебному ответу.
- Feature flag, limit, update/upsert semantics и retention определить до schema migration.

## Критерии приёмки

- Между чатами нет результатов-пересечений; текущий allowlist действует до query.
- Одинаковый literal query возвращает deterministic ordering и реальные source IDs.
- Есть тесты links для supported chat types и честного fallback для unsupported types.
- Empty query, no results, длинный запрос и спецсимволы не ломают поиск.
- История до добавления бота и не полученные Telegram updates явно недоступны.

## Метрики и вопросы

Измерять время до найденного источника, долю успешных задач поиска и оценку relevance первых результатов. Бот не видит обычный click по прямой `t.me` ссылке; для click-through понадобится отдельный UX/instrumentation decision.

- Какие фильтры действительно нужны в MVP?
- В каком порядке ранжировать: keyword relevance, recency, source type?
- Нужен ли reply/copy fallback для basic groups и private chat?
- Какие данные можно включать в snippets и сколько хранить file metadata?
- Как пользователь исправляет stale result?
