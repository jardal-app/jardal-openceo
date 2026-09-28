#!/usr/bin/env bash
# scripts/chat.sh - Terminal interactiva rápida para probar órdenes de OpenCEO sin voz

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  VENV_PY="python3"
fi

exec "$VENV_PY" "$ROOT_DIR/scripts/chat_repl.py" "$@"
