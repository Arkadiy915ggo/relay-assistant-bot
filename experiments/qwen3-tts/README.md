# Qwen3-TTS: тест клонирования голоса

Изолированный эксперимент с [Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base). Бот при этом не запускается и не меняется. Base копирует голос по аудиореференсу; для генерации голоса *по описанию* нужна отдельная модель VoiceDesign.

## Установка (из корня репозитория)

Нужны Python 3.11/3.12, `uv` и GPU с CUDA (для CPU используйте `--device cpu`, но это будет медленно). Окружение отдельное от `.venv` бота:

```bash
uv venv --python 3.12 .venv-tts
uv pip install --python .venv-tts/bin/python qwen-tts
```

Загрузить веса (~4.6 ГБ; хранятся локально, игнорируются Git):

```bash
.venv-tts/bin/hf download Qwen/Qwen3-TTS-12Hz-1.7B-Base \
  --local-dir experiments/qwen3-tts/models/Qwen3-TTS-12Hz-1.7B-Base
```

## Пробный голос

Подготовьте короткий фрагмент с одним отчётливым голосом (порядка 3–10 секунд) и **точно** запишите в `--ref-text` произнесённые слова. Например, сохраните свой аудиофайл как `reference.wav`; для другого персонажа нужен его реальный голосовой фрагмент, название персонажа само по себе Base не использует. Референс и результат не добавляйте в Git.

```bash
.venv-tts/bin/python experiments/qwen3-tts/clone.py \
  --ref-audio /путь/к/reference.wav \
  --ref-text "Точная расшифровка фрагмента." \
  --text "Ледяная корона ждёт своего короля." \
  --language Russian \
  --output experiments/qwen3-tts/outputs/first-try.wav
```

Для сравнения попробуйте несколько записей одного голоса (без музыки и чужих реплик) и одинаковый `--text`. Результат — WAV, который можно прослушать локально. Для английского текста используйте `--language English` и англоязычный референс, если он есть. Если у вас уже есть модель в другом каталоге, передайте `--model /путь/к/модели`.

Без своего референса можно сначала проверить пайплайн на [демо-записи Qwen](https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav) (это **не** голос Артаса):

```bash
mkdir -p experiments/qwen3-tts/outputs
curl -fL https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav \
  -o experiments/qwen3-tts/outputs/reference-demo.wav
.venv-tts/bin/python experiments/qwen3-tts/clone.py \
  --ref-audio experiments/qwen3-tts/outputs/reference-demo.wav \
  --ref-text "Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it! And thanks to you." \
  --text "Ледяная корона ждёт своего короля." \
  --output experiments/qwen3-tts/outputs/demo-russian.wav
```

Официальные [примеры voice clone](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base#voice-clone) используют `generate_voice_clone` с `ref_audio` и `ref_text`; FlashAttention для первого запуска не требуется.
