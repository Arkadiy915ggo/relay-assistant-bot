#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

COMMAND="${1:-start}"

usage() {
  cat <<'EOF'
Usage:
  ./run.sh install   Create .venv and install dependencies
  ./run.sh install-voice
                     Create .venv and install CPU voice transcription dependencies
  ./run.sh install-voice-cuda
                     Create .venv and install CUDA voice transcription dependencies
  ./run.sh start     Start the Telegram bot
  ./run.sh help      Show this help

Default command: start
EOF
}

ensure_venv() {
  if [ ! -x ".venv/bin/python" ]; then
    echo "Missing .venv. Run: ./run.sh install"
    exit 1
  fi
}

setup_cuda_lib_path() {
  local site_packages
  site_packages="$(.venv/bin/python - <<'PY'
import sysconfig

print(sysconfig.get_paths()["purelib"])
PY
)"

  local cuda_paths=(
    "$site_packages/nvidia/cublas/lib"
    "$site_packages/nvidia/cudnn/lib"
    "$site_packages/nvidia/cuda_nvrtc/lib"
  )

  local path
  for path in "${cuda_paths[@]}"; do
    if [ -d "$path" ]; then
      export LD_LIBRARY_PATH="$path:${LD_LIBRARY_PATH:-}"
    fi
  done
}

case "$COMMAND" in
  install)
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .
    ;;
  install-voice)
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e '.[voice]'
    ;;
  install-voice-cuda)
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e '.[voice-cuda]'
    ;;
  start|run)
    ensure_venv
    setup_cuda_lib_path
    if [ ! -f ".env" ]; then
      echo "Missing .env. Create it from .env.example first."
      exit 1
    fi
    .venv/bin/python -m tg_summary_bot
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown command: $COMMAND"
    usage
    exit 1
    ;;
esac
