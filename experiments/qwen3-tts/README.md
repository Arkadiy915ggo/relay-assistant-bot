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

## Пример с нарезкой Артаса

Если записи лежат в `$HOME/Downloads/Альянс/Артас`, три коротких файла можно собрать в моно-WAV (5,6 секунды). Вместо яркого «За Лордерон!» в этой версии используется «Теперь я действительно зол.» — окончание подтверждено распознаванием полной минутной сборки. Нужен `ffmpeg`; исходные `.m4a` изменять не требуется:

```bash
SOURCE="$HOME/Downloads/Альянс/Артас"
mkdir -p experiments/qwen3-tts/outputs
ffmpeg -y \
  -i "$SOURCE/[Я служу свету] Артас, Альянс. Warcraft 3.m4a" \
  -i "$SOURCE/[Во Имя правосудия] Артас, Альянс. Warcraft 3.m4a" \
  -i "$SOURCE/[Теперь я действител...]Артас, Альянс. Warcraft 3.m4a" \
  -filter_complex '[0:a]aformat=sample_rates=24000:channel_layouts=mono[a0];[1:a]aformat=sample_rates=24000:channel_layouts=mono[a1];[2:a]aformat=sample_rates=24000:channel_layouts=mono[a2];[a0][a1][a2]concat=n=3:v=0:a=1[a]' \
  -map '[a]' -c:a pcm_s16le experiments/qwen3-tts/outputs/reference-arthas-v2.wav

.venv-tts/bin/python experiments/qwen3-tts/clone.py \
  --ref-audio experiments/qwen3-tts/outputs/reference-arthas-v2.wav \
  --ref-text 'Я служу свету. Во имя правосудия. Теперь я действительно зол.' \
  --text 'Привет я чат бот Алёша, я служу свету!' \
  --output experiments/qwen3-tts/outputs/arthas-alyosha-v2.wav
```

Итог: `experiments/qwen3-tts/outputs/arthas-alyosha-v2.wav`. Старый вариант остаётся в `arthas-alyosha.wav` для сравнения. Аудиофайлы в Git не попадают.

## Пример с голосом Работника и замером GPU

В `$HOME/Downloads/Альянс/Работник` возьмите «Я готов» и «Ты что ль король? А я за тебя не голосовал»; полный текст второй реплики уточнён по записи. Соберите референс длительностью около 3,7 секунды:

```bash
SOURCE="$HOME/Downloads/Альянс/Работник"
mkdir -p experiments/qwen3-tts/outputs
ffmpeg -y \
  -i "$SOURCE/[Я готов] Работник, Альянс.Warcraft 3.m4a" \
  -i "$SOURCE/[Ты чтоль король...] Работник, Альянс.Warcraft 3.m4a" \
  -filter_complex '[0:a]aformat=sample_rates=24000:channel_layouts=mono[a0];[1:a]aformat=sample_rates=24000:channel_layouts=mono[a1];[a0][a1]concat=n=2:v=0:a=1[a]' \
  -map '[a]' -c:a pcm_s16le experiments/qwen3-tts/outputs/reference-worker.wav

.venv-tts/bin/python experiments/qwen3-tts/benchmark.py \
  --ref-audio experiments/qwen3-tts/outputs/reference-worker.wav \
  --ref-text 'Я готов. Ты что ли король? А я за тебя не голосовал.' \
  --text 'Привет я чат бот алёша. Я за вас не голосовал.' \
  --output experiments/qwen3-tts/outputs/worker-alyosha.wav
```

`benchmark.py` запускает `clone.py --profile` и опрашивает `nvidia-smi` примерно четыре раза в секунду. Внутренние замеры показывают время импорта, загрузки модели, синтеза, а также память, выделенную и зарезервированную *этим процессом* через PyTorch. `nvidia-smi` показывает **общую** занятую память и загрузку видеокарты (включая другие процессы); изменение относительно начального значения — лишь приближённая оценка вклада TTS.

Пример одного холодного запуска на RTX 4070 SUPER (12 ГБ; фон до запуска: 2506 МиБ): импорт 2,36 с, загрузка модели 0,84 с, синтез 2,37 с, всего внутри CLI 5,58 с; PyTorch после загрузки модели 4007 МиБ, пик выделения 4247 МиБ, резерв 4328 МиБ. Пик занятой памяти видеокарты 7144 МиБ, средняя/пиковая загрузка GPU 39/77%, контроллера памяти 20/55% (в течение всего запуска). Результат — WAV 24 кГц длиной 3,12 с. Повторный запуск и фоновая нагрузка могут дать другие цифры.
