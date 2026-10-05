# Feature 10: Message Reactions

## Статус

Идея и базовый дизайн, 2026-10-03. Сейчас бот не ставит реакции на сообщения.

Связи: addressed intent router, [[05 Autonomous Joke Awards and Bot Casino]], [[11 Proactive Chat Messages]].

## Ценность и гипотеза

Бот может выразить короткое подтверждение или эмоцию — 👍, ❤️, 😂 — без отдельного сообщения. Это делает взаимодействие живее и может уменьшить количество служебных ответов.

Гипотеза: релевантная реакция воспринимается как полезное присутствие бота; слишком частые реакции создают шум. Проверить частоту и контекст на небольшом rollout.

## Предлагаемый MVP

- Явная просьба в reply: «Реле, поставь лайк» или будущая команда `/react 👍`.
- Только известный emoji allowlist; default 👍 при явной просьбе без emoji.
- Target всегда реальное replied message текущего разрешённого чата.
- Проверить доступность нужной реакции в чате и ограничения Telegram Bot API перед реализацией.
- Если Telegram не разрешает действие, дать короткое понятное объяснение.

Автоматические реакции — отдельный следующий этап, выключенный по умолчанию. Первый узкий кейс: optional реакция на committed joke winner. Это не дополнительная награда и не изменение баланса.

## Execution и policy

```text
explicit reaction request → validated target + emoji
                          → Telegram setMessageReaction → terminal result
```

- Focused reaction service, без GPU и без LLM для обычного `/react`.
- Если действие выбирает intent router, emoji и target всё равно валидируются детерминированно; модель не получает arbitrary Telegram API execution.
- Chat policy задаёт allowed emojis и разрешение automatic mode; settings UX согласовать с Feature 12.
- Для автоматических реакций предусмотреть cooldown/лимит, idempotent intent identity и opt-out чата.
- Telegram retries учитывать как set desired state, а не как бесконечное добавление реакций.
- Premium/custom/paid reactions не считать доступными боту по умолчанию; проверять текущий официальный API contract.
- Reading users' reactions — другая capability. Не обещать её, не добавив permissions/allowed updates и отдельную задачу.

## Критерии приёмки

- Valid reply request ставит одну поддерживаемую реакцию на выбранное сообщение.
- Нет reply/unsupported emoji/denied chat → явный отказ до API call.
- Telegram restriction не ломает основной message handler.
- Automatic mode имеет измеримый budget и выключение; ошибка реакции не откатывает committed joke points.
- Отдельные тесты на repeated request, unavailable reaction и Telegram failure.

## Метрики и вопросы

Измерять успешность reaction action, частоту на чат и пользовательскую оценку уместности. Не приравнивать reaction count к satisfaction без исследования.

- Какие emoji нужны в первом rollout?
- Должна ли успешная реакция обходиться без текстового ответа?
- Нужна ли команда очистки/замены реакции?
- Кто управляет automatic policy в чате?
- Автоматически реагировать только на jokes или на обычные сообщения тоже?
