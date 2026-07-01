#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

COMMAND="${1:-start}"

usage() {
  cat <<'EOF'
Usage:
  ./run.sh install   Create .venv and install dependencies
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

case "$COMMAND" in
  install)
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .
    ;;
  start|run)
    ensure_venv
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
