# CosyVoice 3: сравнение с Qwen3-TTS

Отдельный CLI для [Fun-CosyVoice3-0.5B-2512](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512). Использует **тот же текст и те же локальные референсы** Ануб’арака, что и `experiments/qwen3-tts/README.md` (раздел «Сравнение референсов на длинной фразе»). Бот не запускается. Аудио, модель и сторонний код не коммитятся.

## Установка из корня репозитория

Нужны `uv`, `ffmpeg`, NVIDIA GPU и достаточно свободного места для весов (~5,1 ГБ) и окружения. CosyVoice требует Python 3.10, поэтому используется отдельная `.venv-cosyvoice3`, не окружение бота или Qwen:

```bash
uv python install 3.10
uv venv --python 3.10 .venv-cosyvoice3
git clone --depth 1 --recurse-submodules https://github.com/FunAudioLLM/CosyVoice.git experiments/cosyvoice3/upstream
# Для повторения именно этого теста: upstream commit 074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
git -C experiments/cosyvoice3/upstream fetch --depth 1 origin 074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
git -C experiments/cosyvoice3/upstream checkout 074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
git -C experiments/cosyvoice3/upstream submodule update --init --recursive

uv pip install --python .venv-cosyvoice3/bin/python torch==2.3.1 torchaudio==2.3.1 \
  --index-url https://download.pytorch.org/whl/cu121
uv pip install --python .venv-cosyvoice3/bin/python onnxruntime-gpu==1.18.0 \
  --index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
uv pip install --python .venv-cosyvoice3/bin/python \
  conformer==0.3.2 diffusers==0.29.0 hyperpyyaml==1.2.3 librosa==0.10.2 \
  numpy==1.26.4 transformers==4.51.3 inflect==7.3.1 modelscope==1.20.0 \
  x-transformers==2.11.24 soundfile==0.12.1 omegaconf==2.3.0 \
  protobuf==4.25.3 setuptools==80.9.0 wheel hydra-core==1.3.2 \
  lightning==2.2.4 matplotlib==3.7.5 rich==13.7.1 pyworld==0.3.4 \
  gdown==5.1.0 wget==3.2 pyarrow==18.1.0
uv pip install --python .venv-cosyvoice3/bin/python openai-whisper==20231117 --no-build-isolation
```

`wetext` здесь не нужен: CLI передаёт `text_frontend=False`. Выкачиваем только файлы для обычного запуска; RL-веса, батчевый токенизатор и TensorRT ONNX не используются:

```bash
.venv-cosyvoice3/bin/hf download FunAudioLLM/Fun-CosyVoice3-0.5B-2512 \
  --exclude 'flow.decoder.estimator.fp32.onnx' 'llm.rl.pt' 'speech_tokenizer_v3.batch.onnx' 'asset/*' \
  --local-dir experiments/cosyvoice3/models/Fun-CosyVoice3-0.5B-2512
```

## Сравнение

Сначала соберите `reference-anubarak-worker.wav` и `reference-anubarak-mix.wav` по рецепту в `experiments/qwen3-tts/README.md`. Все три вызова используют **одинаковый текст**:

```bash
TEXT='Я чат-бот Алёша. Готов служить Плети. Ну, я могу подготовить презентацию. Будет готово к среде в два часа.'

.venv-cosyvoice3/bin/python experiments/cosyvoice3/clone.py \
  --ref-audio experiments/qwen3-tts/outputs/reference-anubarak-worker.wav \
  --ref-text 'Мы, нежить, народ работящий. Такая уж наша тяжкая доля.' \
  --text "$TEXT" --output experiments/cosyvoice3/outputs/anubarak-worker-zero-shot.wav

.venv-cosyvoice3/bin/python experiments/cosyvoice3/clone.py \
  --ref-audio experiments/qwen3-tts/outputs/reference-anubarak-mix.wav \
  --ref-text 'Всё не так плохо, как вы думаете. Мы, нежить, народ работящий.' \
  --text "$TEXT" --output experiments/cosyvoice3/outputs/anubarak-mix-zero-shot.wav

.venv-cosyvoice3/bin/python experiments/cosyvoice3/clone.py \
  --ref-audio experiments/qwen3-tts/outputs/reference-anubarak-worker.wav \
  --text "$TEXT" \
  --instruct 'Speak Russian slowly and evenly, pause briefly after each sentence, and finish each sentence with a calm falling intonation.' \
  --speed 0.85 \
  --output experiments/cosyvoice3/outputs/anubarak-worker-slow-instruct.wav
```

Первые два вызова — обычный zero-shot с текстом референса. Третий использует `inference_instruct2`: этот режим принимает **аудио**, но по API не принимает текст референса. `speed` задаётся самой CosyVoice; это не постобработка `ffmpeg`, как у Qwen-скрипта. Инструкция — просьба к модели, а не гарантия ударения, пауз или интонации.

На RTX 4070 SUPER (12 ГБ) после прогрева загрузка модели заняла ~3,6–3,7 с, синтез — 4,4–4,7 с, резерв PyTorch в пике ~5,2–5,3 ГиБ. Получились WAV 24 кГц длительностью 14,96 / 9,96 / 9,96 с соответственно. Первая загрузка дольше; измерение PyTorch не включает память ONNX Runtime и других процессов. Автоматическое распознавание по-разному искажало «чат-бот Алёша» и зачастую заменяло «готово» на «готова»; сходство с референсом и ударение в «Плети» оценивайте прослушиванием.

## Сдвиг высоты и замедление готового голоса

Для сравнения возьмите **одну и ту же генерацию** `anubarak-worker-zero-shot.wav`. Сперва замедлите её до 85% исходного темпа, не меняя высоту. Затем дважды понизьте высоту **на один полутон** (по-русски «на полтона», −1 semitone, частотный множитель `2^(-1/12) ≈ 0.943874`) без изменения темпа:

```bash
ffmpeg -y -i experiments/cosyvoice3/outputs/anubarak-worker-zero-shot.wav \
  -af atempo=0.85 -c:a pcm_s16le \
  experiments/cosyvoice3/outputs/anubarak-worker-zero-shot-slow.wav

.venv-cosyvoice3/bin/python experiments/cosyvoice3/pitch_shift.py \
  --input experiments/cosyvoice3/outputs/anubarak-worker-zero-shot-slow.wav \
  --output experiments/cosyvoice3/outputs/anubarak-worker-zero-shot-slow-minus1-preserved.wav \
  --semitones -1 --formant preserved
.venv-cosyvoice3/bin/python experiments/cosyvoice3/pitch_shift.py \
  --input experiments/cosyvoice3/outputs/anubarak-worker-zero-shot-slow.wav \
  --output experiments/cosyvoice3/outputs/anubarak-worker-zero-shot-slow-minus1-shifted.wav \
  --semitones -1 --formant shifted
```

`preserved` сохраняет резонансы (тембр обычно естественнее), `shifted` сдвигает и резонансы (эффект может быть заметнее). Все три получившихся WAV имеют одинаковую длительность ~17,6 с; исходная запись — ~15 с. Для более тонкой правки подойдёт `--semitones -0.5`; положительное значение повысит голос. Скрипт применим и к WAV из тестов Qwen. Нужен `ffmpeg` с фильтром `rubberband` (`ffmpeg -h filter=rubberband`). Это обработка готовой записи, а не повторный синтез, поэтому повторно загружать модель не нужно.

### Ещё две реплики тем же голосом

Тот же zero-shot референс «Мы, нежить, народ работящий…», что и у `anubarak-worker-zero-shot.wav`. Синтезируем каждую фразу отдельно, затем получаем замедленный до 85% и пониженный на полтона вариант (форманты сохраняются):

```bash
REF=experiments/qwen3-tts/outputs/reference-anubarak-worker.wav
REF_TEXT='Мы, нежить, народ работящий. Такая уж наша тяжкая доля.'
OUT=experiments/cosyvoice3/outputs

.venv-cosyvoice3/bin/python experiments/cosyvoice3/clone.py \
  --ref-audio "$REF" --ref-text "$REF_TEXT" \
  --text 'Не бойся когда ты один. Бойся когда ты два.' \
  --output "$OUT/anubarak-dont-fear-raw.wav"
.venv-cosyvoice3/bin/python experiments/cosyvoice3/clone.py \
  --ref-audio "$REF" --ref-text "$REF_TEXT" \
  --text 'Соседка снизу, может быть сверху.' \
  --output "$OUT/anubarak-neighbor-raw.wav"

ffmpeg -y -i "$OUT/anubarak-dont-fear-raw.wav" -af atempo=0.85 \
  -c:a pcm_s16le "$OUT/anubarak-dont-fear-slow.wav"
.venv-cosyvoice3/bin/python experiments/cosyvoice3/pitch_shift.py \
  --input "$OUT/anubarak-dont-fear-slow.wav" \
  --output "$OUT/anubarak-dont-fear-slow-minus1.wav" --semitones -1 --formant preserved
ffmpeg -y -i "$OUT/anubarak-neighbor-raw.wav" -af atempo=0.85 \
  -c:a pcm_s16le "$OUT/anubarak-neighbor-slow.wav"
.venv-cosyvoice3/bin/python experiments/cosyvoice3/pitch_shift.py \
  --input "$OUT/anubarak-neighbor-slow.wav" \
  --output "$OUT/anubarak-neighbor-slow-minus1.wav" --semitones -1 --formant preserved
```

Итоговые файлы для прослушивания: `anubarak-dont-fear-slow-minus1.wav` (7,37 с) и `anubarak-neighbor-slow-minus1.wav` (6,01 с). Автоматическое распознавание подтверждает слова обеих фраз после обработки. CosyVoice предупреждает, что короткий текст относительно референса может снижать качество: сходство голоса и интонацию оценивайте на слух.
