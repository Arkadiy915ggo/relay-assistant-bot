#!/usr/bin/env bash
set -euo pipefail
# Explicit installation only. Never invoked by startup or tests.
ROOT="$(pwd)"
UPSTREAM="${TTS_COSYVOICE_UPSTREAM_DIR:-$ROOT/tts/cosyvoice3/upstream}"
MODEL="${TTS_MODEL_DIR:-$ROOT/tts/cosyvoice3/models/Fun-CosyVoice3-0.5B-2512}"
PYTHON="${TTS_PYTHON_PATH:-$ROOT/.venv-cosyvoice3/bin/python}"
REVISION=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
MODEL_REVISION=29e01c4e8d000f4bcd70751be16fa94bf3d85a18
command -v uv >/dev/null
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
uv python install 3.10
if [ ! -x "$PYTHON" ]; then
  uv venv --python 3.10 "$(dirname "$(dirname "$PYTHON")")"
fi
mkdir -p "$(dirname "$UPSTREAM")"
if [ ! -d "$UPSTREAM" ]; then
  git clone --recurse-submodules https://github.com/FunAudioLLM/CosyVoice.git "$UPSTREAM"
fi
if [ -n "$(git -C "$UPSTREAM" status --porcelain)" ]; then
  echo "Upstream contains local changes; refusing to overwrite them."
  exit 1
fi
git -C "$UPSTREAM" fetch --depth 1 origin "$REVISION"
git -C "$UPSTREAM" checkout "$REVISION"
git -C "$UPSTREAM" submodule update --init --recursive
uv pip install --python "$PYTHON" torch==2.3.1 torchaudio==2.3.1 \
  --index-url https://download.pytorch.org/whl/cu121
uv pip install --python "$PYTHON" onnxruntime-gpu==1.18.0 \
  --index-url https://aiinfra.pkgs.visualstudio.com/PublicPackages/_packaging/onnxruntime-cuda-12/pypi/simple/
uv pip install --python "$PYTHON" \
  conformer==0.3.2 diffusers==0.29.0 hyperpyyaml==1.2.3 librosa==0.10.2 \
  numpy==1.26.4 transformers==4.51.3 inflect==7.3.1 modelscope==1.20.0 \
  x-transformers==2.11.24 soundfile==0.12.1 omegaconf==2.3.0 \
  protobuf==4.25.3 setuptools==80.9.0 wheel==0.45.1 hydra-core==1.3.2 \
  lightning==2.2.4 matplotlib==3.7.5 rich==13.7.1 pyworld==0.3.4 \
  gdown==5.1.0 wget==3.2 pyarrow==18.1.0 huggingface-hub==0.30.2
uv pip install --python "$PYTHON" openai-whisper==20231117 --no-build-isolation
"$(dirname "$PYTHON")/huggingface-cli" download FunAudioLLM/Fun-CosyVoice3-0.5B-2512 \
  --revision "$MODEL_REVISION" \
  --exclude 'flow.decoder.estimator.fp32.onnx' 'llm.rl.pt' 'speech_tokenizer_v3.batch.onnx' 'asset/*' \
  --local-dir "$MODEL"
echo "TTS installed. Configure TTS_ENABLED and paths, then run ./run.sh doctor."
