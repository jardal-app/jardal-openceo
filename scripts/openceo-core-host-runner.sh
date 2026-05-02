#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/openceo-core"
ENV_FILE="$APP_DIR/.env"
VENV_PY="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Missing virtualenv python at $VENV_PY"
  echo "Run: scripts/setup.sh"
  exit 1
fi

if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" == *"="* ]]; then
      key="${line%%=*}"
      val="${line#*=}"
      key="$(printf '%s' "$key" | tr -d '[:space:]')"
      export "$key=$val"
    fi
  done < "$ENV_FILE"
fi

export OPENCEO_HOST="0.0.0.0"
export OPENCEO_PORT="${OPENCEO_HOST_PORT:-18765}"
if [[ -n "${MONGODB_URL:-}" ]] && [[ "${MONGODB_URL}" == *"jardal-mongo"* ]]; then
  export MONGODB_URL="${MONGODB_URL//jardal-mongo/127.0.0.1}"
fi
export PROJECTS_DIR="${PROJECTS_DIR:-$ROOT_DIR/projects}"
export PYTHONUNBUFFERED=1

cd "$APP_DIR"
exec "$VENV_PY" main.py
