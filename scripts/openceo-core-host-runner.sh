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

load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    # Use the same dotenv expansion as Settings and the manual host launcher.
    while IFS= read -r -d '' key && IFS= read -r -d '' val; do
      export "$key=$val"
    done < <("$VENV_PY" - "$ENV_FILE" <<'PYENV'
import re
import sys
from dotenv import dotenv_values
for key, value in dotenv_values(sys.argv[1]).items():
    if value is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        sys.stdout.buffer.write(key.encode() + b"\0" + value.encode() + b"\0")
PYENV
    )
  fi
}

load_env

export OPENCEO_HOST="0.0.0.0"
export OPENCEO_PORT="${OPENCEO_HOST_PORT:-18765}"
if [[ -n "${MONGODB_URL:-}" ]] && [[ "${MONGODB_URL}" == *"jardal-mongo"* ]]; then
  export MONGODB_URL="${MONGODB_URL//jardal-mongo/127.0.0.1}"
fi
# Same CLI search path as the manual launcher: Tier S resolves bare role
# commands (claudej, codexj, musej, muse, qwen, claudep) via PATH. Without this,
# the systemd-managed service fails preflight with "no se encontró el comando".
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.opencode/bin:$PATH"
export PROJECTS_DIR="${PROJECTS_DIR:-$ROOT_DIR/projects}"
export PYTHONUNBUFFERED=1

cd "$APP_DIR"
"$VENV_PY" "$ROOT_DIR/scripts/sync-mcp-config.py" || echo "WARN: MCP config sync failed"
exec "$VENV_PY" main.py
