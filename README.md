# Telegram Summary Bot

Безопасный Telegram-бот для краткой AI-выжимки обсуждений за период. Бот использует официальный Telegram Bot API, поэтому не логинится как пользователь и не читает старую историю чата.

## Что умеет

- сохраняет новые текстовые сообщения и подписи к медиа в SQLite;
- делает саммари по команде `/summary 24h`, `/summary 7d`, `/summary today`;
- умеет сравнивать саммари разных Ollama-моделей через `/compare 10m`;
- добавляет в отчет категорию `Лучшая шутка` и выбирает смешную реплику из обсуждения;
- работает в личном чате с ботом, группах, супергруппах и каналах;
- использует OpenAI API или локальную Ollama-модель;
- режет длинные обсуждения на части и собирает финальное саммари;
- ограничивает доступ через `ALLOWED_CHAT_IDS`.

## Важные ограничения Telegram

- Бот не видит сообщения, которые были написаны до его добавления.
- В группах бот увидит все сообщения только если выключить `Group Privacy` в `@BotFather`.
- В канале бот должен быть администратором. Он сможет получать новые посты канала, но не комментарии в привязанном discussion-чате, если его не добавить и туда.
- В личных чатах бот видит только сообщения, отправленные ему напрямую.

## OpenAI или подписка ChatGPT

Подписка ChatGPT Plus/Pro не является API-ключом. Для автоматического бота нужен `OPENAI_API_KEY` из OpenAI Platform, он оплачивается отдельно.

Если API-ключ использовать не хочется, можно запустить локальную модель через Ollama. Качество саммари обычно ниже, но для приватного использования это безопаснее.

## Быстрый старт

Требования:

- Python 3.11+;
- Telegram bot token от `@BotFather`;
- один из вариантов LLM:
- `OPENAI_API_KEY`;
- или локальная Ollama.

Установка:

```bash
cd telegram-summary-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Заполни `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:your_real_token
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-your-key
OPENAI_MODEL=gpt-4o-mini
```

Запуск:

```bash
python -m tg_summary_bot
```

Или через shell-скрипт из корня проекта:

```bash
./run.sh install
./run.sh start
```

Если нужна локальная расшифровка голосовых через Whisper:

```bash
./run.sh install-voice
```

## Настройка через BotFather

1. Открой `@BotFather`.
2. Выполни `/newbot`.
3. Задай имя и username бота.
4. Скопируй token в `.env` как `TELEGRAM_BOT_TOKEN`.
5. Для групп выключи privacy mode:
   `@BotFather` -> `/mybots` -> твой бот -> `Bot Settings` -> `Group Privacy` -> `Turn off`.

После этого добавь бота в нужный чат.

## Безопасная настройка доступа

Первый запуск можно сделать с пустым `ALLOWED_CHAT_IDS`, чтобы узнать `chat_id`:

```env
ALLOWED_CHAT_IDS=
```

В нужном чате напиши:

```text
/stats
```

Бот ответит примерно так:

```text
chat_id: -1001234567890
chat_type: supergroup
saved_messages: 42
llm_provider: openai
```

После этого лучше ограничить доступ:

```env
ALLOWED_CHAT_IDS=-1001234567890,123456789
```

Перезапусти бота.

## Команды

```text
/start
/help
/stats
/summary
/summary 30m
/summary 6h
/summary 24h
/summary 7d
/summary 2w
/summary today
/compare 10m
```

## Логи ответов

Ответы бота пишутся отдельными JSON-строками в `data/responses.log`. Путь можно изменить через:

```env
RESPONSE_LOG_FILE=data/responses.log
```

Этот лог удобно использовать, чтобы анализировать качество саммари и дорабатывать промпты.

## Голосовые сообщения

Бот может локально расшифровывать Telegram voice/audio через `faster-whisper` и сохранять результат как обычное сообщение для будущих `/summary`.

Установка voice-зависимостей для CPU:

```bash
./run.sh install-voice
```

Установка voice-зависимостей для NVIDIA GPU:

```bash
./run.sh install-voice-cuda
```

Настройки для сильной модели на NVIDIA GPU:

```env
TRANSCRIBE_VOICE=true
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda
WHISPER_COMPUTE_TYPE=float16
WHISPER_LANGUAGE=ru
MAX_VOICE_SECONDS=600
OLLAMA_UNLOAD_AFTER_TASK=true
```

LLM-задачи и расшифровка используют общую GPU-очередь: бот не запускает Ollama и Whisper одновременно. После `/summary` или `/compare` Ollama-модель явно выгружается, затем Whisper может занять GPU для расшифровки.

`run.sh` автоматически добавляет CUDA-библиотеки из `.venv` в `LD_LIBRARY_PATH`, если они установлены через `install-voice-cuda`.

Если при `WHISPER_DEVICE=cuda` появляется ошибка:

```text
RuntimeError: Library libcublas.so.12 is not found or cannot be loaded
```

Поставь CUDA voice-зависимости и перезапусти бота:

```bash
./run.sh install-voice-cuda
./run.sh start
```

Быстрая проверка, что библиотеки доступны:

```bash
SITE_PACKAGES="$(.venv/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")"
LD_LIBRARY_PATH="$SITE_PACKAGES/nvidia/cublas/lib:$SITE_PACKAGES/nvidia/cudnn/lib:$SITE_PACKAGES/nvidia/cuda_nvrtc/lib" \
  .venv/bin/python -c "import ctypes; ctypes.CDLL('libcublas.so.12'); ctypes.CDLL('libcudnn.so.9'); print('CUDA libs OK')"
```

Если `large-v3` окажется медленной или будет не хватать VRAM, понижай модель в таком порядке:

```env
WHISPER_MODEL=large-v3-turbo
WHISPER_MODEL=medium
WHISPER_MODEL=small
```

По умолчанию `/summary` использует период из `DEFAULT_SUMMARY_PERIOD`, сейчас это `24h`.

`/compare` прогоняет один и тот же набор сообщений через модели из `COMPARE_MODELS`. Это удобно для сравнения качества, но может быть очень долго на 30B/70B моделях.

## Использование в личном чате

1. Открой бота в Telegram.
2. Нажми `Start`.
3. Пиши сообщения боту.
4. Через время отправь `/summary 24h`.

Важно: бот не может читать твои личные переписки с другими людьми. Он видит только диалог с самим ботом.

## Использование в группе или супергруппе

1. Выключи `Group Privacy` у бота в `@BotFather`.
2. Добавь бота в группу.
3. Желательно дать ему право читать сообщения. Админка обычно не обязательна для обычной группы, но помогает избежать ограничений.
4. Напиши `/stats`, скопируй `chat_id` в `ALLOWED_CHAT_IDS`.
5. Через несколько часов или дней используй `/summary 24h`.

Важно: сообщения начнут сохраняться только после добавления бота и включения нужных прав.

## Использование в канале

1. Добавь бота в канал как администратора.
2. Дай минимум право видеть/получать посты. Для ответа командами удобнее разрешить публикацию сообщений, но это зависит от сценария.
3. Новые посты канала будут сохраняться как `channel_post`.
4. Команду `/summary` удобнее вызывать в личном чате или группе, если канал не позволяет обычный диалог с ботом.

Если у канала есть discussion-группа с комментариями, добавь бота еще и в эту группу. Комментарии живут именно там.

## Локальная модель через Ollama

Установи Ollama и скачай модель:

```bash
ollama pull llama3.1:8b
```

В `.env`:

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_TIMEOUT_SECONDS=1800
OLLAMA_KEEP_ALIVE=30m
OLLAMA_NUM_CTX=4096
OLLAMA_NUM_PREDICT=800
COMPARE_MODELS=qwen3:14b,gemma3:27b,qwen3-coder:30b
```

Запусти бота:

```bash
python -m tg_summary_bot
```

Если на компьютере есть другая модель, посмотри список:

```bash
ollama list
```

И впиши ее имя в `OLLAMA_MODEL`.

Если видишь ошибку `ReadTimeout`, чаще всего модель слишком долго грузится или отвечает. Для 30B/70B моделей это нормально на первом запросе. Что делать:

- увеличь `OLLAMA_TIMEOUT_SECONDS`, например до `1800` или `3600`;
- оставь `OLLAMA_KEEP_ALIVE=30m`, чтобы модель не выгружалась сразу;
- для проверки скорости временно поставь `OLLAMA_MODEL=qwen3:14b`;
- уменьши период саммари, например `/summary 10m` вместо `/summary 24h`.

## Оптимизация и приватность

- `MAX_MESSAGE_CHARS` обрезает слишком длинные сообщения перед сохранением.
- `MAX_SUMMARY_INPUT_CHARS` ограничивает объем текста, отправляемого в модель за один запрос саммари.
- `CHUNK_CHARS` задает размер частей для длинных обсуждений.
- `OLLAMA_NUM_CTX` задает контекст Ollama. Если он маленький, слишком длинные чанки могут давать `400 Bad Request`.
- `OLLAMA_NUM_PREDICT` ограничивает длину ответа модели. Для медленных 70B моделей это помогает не упираться в timeout.
- `LOG_FILE` задает файл логов бота, по умолчанию `data/bot.log`.
- SQLite включен в WAL-режиме, этого достаточно для небольшого и среднего чата.
- Секреты лежат в `.env`, он добавлен в `.gitignore`.

## Продакшен-запуск

Для постоянной работы лучше запускать через `systemd`, `supervisor`, Docker или любой process manager. Минимально можно оставить в `tmux`:

```bash
cd telegram-summary-bot
source .venv/bin/activate
python -m tg_summary_bot
```

## Где лежат данные

По умолчанию база:

```text
telegram-summary-bot/data/messages.sqlite3
```

Эту папку не стоит коммитить в git.
