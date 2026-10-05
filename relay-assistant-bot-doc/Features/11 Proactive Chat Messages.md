# Feature 11: Proactive Chat Messages

## Статус

Идея и базовый дизайн, 2026-10-03. Сейчас бот отвечает на команды/обращения и выполняет отдельные autonomous joke notifications, но не имеет общего механизма инициативных «чё как, кого?» сообщений.

Связи: [[05 Autonomous Joke Awards and Bot Casino]], [[10 Message Reactions]], [[12 Product UX and Portfolio]].

## Ценность и гипотеза

Бот иногда может сам начать лёгкий разговор, спросить о делах или продолжить недавнюю тему. Это должно ощущаться как уместная инициатива, а не случайный спам.

Гипотеза: редкие контекстные сообщения поддерживают участие в неформальном чате. Результат зависит от аудитории; полезность для рабочего чата может быть иной.

В [[01 Product Vision]] это часть community-first характера бота, а не просто служебное напоминание. Tone, initiative и frequency задаются chat profile независимо от включения полезных навыков. В formal preset можно выбрать addressed-only behavior, в community preset — явно включаемую редкую инициативу.

## Предлагаемый MVP

- Opt-in для каждого чата, по умолчанию выключено.
- Один тип инициативы: короткий check-in с chat tone preset.
- Простой schedule/activity rule: eligible time window + достаточный cooldown + разрешённое состояние активности.
- До отправки повторно проверять текущий allowlist, включение proactive mode, timezone/quiet hours и дневной budget.
- Пользователь может быстро выключить режим; предварительный UX `/proactive off`, точная команда позже.
- Для первого rollout не нужны agentic goals, самостоятельный web search или произвольные инструменты.

## Scheduler design

```text
chat policy + activity state → due candidate
                            → current-policy check → optional short LLM generation
                            → recheck policy/activity → send → record delivery
```

- Отдельный lightweight durable scheduler, а не таймер внутри message handler.
- Состояние chat-scoped: next due time, last delivery, daily budget, timezone, policy version и pause flag.
- После downtime не отправлять пачку пропущенных приветствий. Просроченные идеи coalesce/expire; новая отправка заново проходит policy.
- Если GPU занят foreground work, proactive candidate откладывается или истекает, а не забирает отдельный lock.
- LLM получает небольшой актуальный контекст и тон чата; template-only fallback можно оценить в MVP.
- Перед send перепроверить активность: если за время генерации началось интенсивное обсуждение, candidate можно отменить.
- Зафиксировать Telegram uncertain-send contract: не обещать exactly-once delivery. Для greetings предпочтителен пропуск при неизвестном результате, а не потенциальный duplicate retry.
- Не использовать joke outbox как общую таблицу всех будущих proactive jobs; общий scheduler нужен только после конкретного минимального сценария.

## Ограничения поведения

- Не генерировать инициативы по каждому входящему сообщению.
- Не отвечать собственным сообщениям и не запускать bot-to-bot бесконечные диалоги.
- Не выдавать выдуманные воспоминания или приписывать людям непроверенные события.
- Check-in не меняет балансы, настройки или профили без явного действия пользователя.
- Frequency/quiet hours — часть продукта и настройки чата, а не скрытая prompt instruction.

## Критерии приёмки

- Off mode, denied chat и quiet hours исключают delivery.
- Restart сохраняет cooldown/budget и не создаёт backlog spam.
- За выбранный день/окно не превышается configured budget.
- Смена policy до send отменяет candidate.
- Есть тесты timezone/day boundary, restart, busy GPU, cancellation и uncertain Telegram send.

## Метрики и вопросы

Измерять opt-in/opt-out, replies на proactive message, негативную обратную связь, сообщения на чат/день и queue impact. Рост числа сообщений сам по себе не является успехом.

- Какую частоту пользователи считают комфортной?
- Ориентироваться на затишье или недавнюю активность?
- Кто включает режим и выбирает тон/время?
- Хранить ли proactive text как обычный assistant context и делать ли его eligible для joke awards?
- Нужен ли template-first эксперимент до подключения LLM?
