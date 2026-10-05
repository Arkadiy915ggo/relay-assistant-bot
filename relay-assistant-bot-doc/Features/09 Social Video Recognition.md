# Feature 09: Social Video Recognition

## Статус

Базовый дизайн и известная проблема, 2026-10-03. YouTube download flow уже есть, но по сообщению владельца текущая среда получает отказ доступа/требование cookies. Блокировка IP и необходимость cookies — рабочие гипотезы, а не установленная причина.

TikTok/Instagram ingestion пока не реализован. Этот документ не поручает подключать их сейчас.

## Цель

Восстановить понятный и надёжный YouTube-сценарий, затем дать один UX распознавания/расшифровки видео по supported social URL. Сохранять источник и использовать уже существующий frame/audio pipeline.

## Текущее поведение

- `/video <YouTube URL>` и reply на ссылку используют `youtube.py` / `yt-dlp`.
- В downloader нет настройки cookies, browser-session import или иных authentication credentials.
- Есть limits размера/длительности, отказ от playlists/live и cleanup при timeout/cancellation.
- Реальное upstream access failure не исправляется unit tests или изменением prompt распознавания.

## Этап A: диагностика YouTube

В отдельной будущей сессии:

1. Получить точный тип ошибки и версии `yt-dlp`, FFmpeg, extractor/runtime components; не выводить cookies/tokens в логи.
2. Проверить один public video и известный failing URL в той же hosting environment.
3. Проверить актуальные требования extractor: обновление `yt-dlp`, supported JS runtime/challenge handling и особенности сети.
4. Если отказ действительно связан с session/authentication, сравнить explicit local cookie file и пользовательский browser export.
5. Отдельно проверить, что выбранное решение работает в headless deploy, а не только в личном браузере.
6. Зафиксировать воспроизводимый результат; не объявлять «IP заблокирован» только по сообщению «sign in».

Проектирование cookie setting потребует явного локального пути, проверки доступности и инструкции обновления/истечения сессии. Cookies не должны попадать в Git, message responses, response logs или shareable configuration export. Реализацию выбрать после диагностики.

## Этап B: общий social-video downloader

```text
URL → source validation → provider adapter → DownloadedVideo
    → existing frame/audio recognition → source-backed context → cleanup
```

- Узкий общий контракт: local path, provider, canonical URL, title, duration, size и cleanup ownership.
- YouTube adapter первым переносится/подключается к контракту без изменения Telegram video behavior.
- Далее отдельно TikTok single-video adapter, затем Instagram Reel/single-video adapter.
- Не отдавать все URL произвольному extractor: supported domains/resource types валидируются до download.
- Общие limits bytes/duration/runtime и агрегатного temp size; stream/playlist/profile feed/carousel semantics уточнять отдельно.
- Normalized failures: unsupported URL, inaccessible/auth required, too large/long, download timeout, no media.
- GPU занимается только после успешного download и проверки metadata.
- Расшифровка и visual recognition переиспользуют existing implementations; без отдельного GPU lock для каждой платформы.

## Предлагаемый UX

- Сохранить `/video <URL>` и reply на ссылку как единый entry point.
- Короткий progress status: скачивание → анализ → результат.
- Ошибка доступа говорит о невозможности скачать, а не о сбое распознавания речи.
- Если download недоступен, пользователь может загрузить файл видео напрямую в Telegram и использовать existing flow.
- В результате показать платформу, название и исходную ссылку; long output split остаётся общим.

## Критерии приёмки будущей реализации

- Public single video проходит подготовленные smoke cases для каждой платформы.
- Недоступный/private/deleted resource имеет предсказуемую ошибку и не создаёт пустой контекст.
- Authentication failure не запускает бесконечный retry или тяжёлую recognition.
- Timeout/cancellation дожидаются последних записей и удаляют abandoned files.
- Telegram video/cache tests продолжают проходить.

## Метрики и вопросы

Измерять download success отдельно от recognition success, распределение access failures по source и end-to-end latency. Не объединять network/auth failure с качеством Whisper.

- Достаточны ли public videos или нужны личные logged-in sessions?
- Какая платформа следующая после YouTube, исходя из реальных URL в целевых чатах?
- Как поддерживать extractor updates без непредсказуемого dependency drift?
- Нужен ли cache normalized URL/content hash и как истекает доступ к private sources?
- Кто обновляет session configuration при её истечении?
