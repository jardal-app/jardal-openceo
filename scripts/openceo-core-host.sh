#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/openceo-core"
ENV_FILE="$APP_DIR/.env"
VENV_PY="$ROOT_DIR/.venv/bin/python"
RUNTIME_DIR="$ROOT_DIR/.runtime"
RELEASE_FILE="$RUNTIME_DIR/RELEASE"
UNIT="openceo-core-host.service"

mkdir -p "$RUNTIME_DIR"

runtime_pids() {
  ps -C python -o pid=,args= \
    | awk -v py="$VENV_PY" '$2 == py && $3 == "main.py" {print $1}'
}

port_owner() {
  local port="${OPENCEO_PORT:-18765}"
  command -v ss >/dev/null 2>&1 || return 0
  ss -tlnp 2>/dev/null | grep -E ":${port}[[:space:]]" | grep -oE 'pid=[0-9]+' \
    | head -n 1 | cut -d= -f2
}

load_env() {
  if [[ -f "$ENV_FILE" ]]; then
    # Match Settings' dotenv parsing, including quotes and ${KEY} references.
    # NUL-delimited values are data, never evaluated as shell commands.
    while IFS= read -r -d '' key && IFS= read -r -d '' val; do
      export "$key=$val"
    done < <("$VENV_PY" - "$ENV_FILE" <<'PY'
import re
import sys
from dotenv import dotenv_values
for key, value in dotenv_values(sys.argv[1]).items():
    if value is not None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        sys.stdout.buffer.write(key.encode() + b"\0" + value.encode() + b"\0")
PY
    )
  fi

  # Host-mode overrides
  export OPENCEO_HOST="0.0.0.0"
  export OPENCEO_PORT="${OPENCEO_HOST_PORT:-18765}"
  if [[ -n "${MONGODB_URL:-}" ]] && [[ "${MONGODB_URL}" == *"jardal-mongo"* ]]; then
    export MONGODB_URL="${MONGODB_URL//jardal-mongo/127.0.0.1}"
  fi
  # Bun is the jardal-erp gate runner (bun run typecheck / bun test) and
  # lives outside the system PATH; without it every baseline gate is red.
  export PATH="$HOME/.bun/bin:$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.opencode/bin:$PATH"
  export KIMI_CLI_PATH="${KIMI_CLI_PATH:-$HOME/.local/bin/kimi}"
  export PROJECTS_DIR="${PROJECTS_DIR:-$ROOT_DIR/projects}"
  export PYTHONUNBUFFERED=1
}

foreground() {
  # Primer plano para systemd ExecStart (TASK-2026-00370): mismo entorno
  # que start pero exec sin demonizar. El flock lo aplica main.py (exit 3
  # si otra instancia tiene el candado; la unidad no lo reintenta).
  local sha_now
  load_env
  if [[ ! -x "$VENV_PY" ]]; then
    echo "Missing virtualenv python at $VENV_PY"
    echo "Run: scripts/setup.sh"
    return 1
  fi
  # Best-effort: los reinicios de systemd no pueden bloquearse por árbol
  # sucio; el gate estricto vive en restart (TASK-2026-00367).
  if ! bash "$ROOT_DIR/scripts/write-release.sh" "$ROOT_DIR" "$RUNTIME_DIR"; then
    # Sin identidad limpia no se conserva la anterior: se marca el HEAD
    # en curso como sucio para no anunciar un sha limpio ajeno.
    sha_now="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
    [[ -n "${sha_now:-}" ]] && \
      printf '{"sha":"%s","dirty":true}\n' "$sha_now" >"$RELEASE_FILE" || true
    echo "WARN: write-release failed; continuing with dirty release identity."
  fi
  "$VENV_PY" "$ROOT_DIR/scripts/sync-mcp-config.py" || echo "WARN: MCP config sync failed"
  cd "$APP_DIR"
  exec "$VENV_PY" main.py
}

require_unit() {
  # The core runs only under its systemd user unit (TASK-2026-00510): a
  # second, unsupervised start path once left the core outside the unit.
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found: the core runs only as the $UNIT user unit."
    exit 1
  fi
  # Sin sesión de usuario el bus no existe; se apunta al estándar.
  : "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
  : "${DBUS_SESSION_BUS_ADDRESS:=unix:path=$XDG_RUNTIME_DIR/bus}"
  export XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
  if ! systemctl --user cat "$UNIT" >/dev/null 2>&1; then
    echo "Unit $UNIT is not installed. Run: scripts/openceo-systemd-migrate.sh install"
    exit 1
  fi
}

status() {
  local state pids count owner pid
  load_env
  require_unit
  state="$(systemctl --user is-active "$UNIT" 2>/dev/null || echo unknown)"
  pids="$(runtime_pids || true)"
  if [[ -f "$RELEASE_FILE" ]]; then
    echo "release: $(cat "$RELEASE_FILE")"
  fi
  if [[ "$state" == "active" ]]; then
    count="$(echo "$pids" | grep -cE '^[0-9]+$' || true)"
    if [[ "$count" -gt 1 ]]; then
      echo "WARNING: unit active but $count openceo-core instances run (pids: $(echo $pids | tr '\n' ' ')). Only one may run; stop the strays."
      return 2
    fi
    pid="$(echo "$pids" | grep -E '^[0-9]+$' | head -n 1 || true)"
    owner="$(port_owner || true)"
    if [[ -n "$pid" && -n "$owner" && "$owner" != "$pid" ]]; then
      echo "WARNING: openceo-core (pid $pid) is not the port ${OPENCEO_PORT} owner (pid $owner)."
      return 2
    fi
    echo "openceo-core host is running under systemd (unit active)."
    return 0
  fi
  if [[ -n "${pids// }" ]]; then
    echo "WARNING: unit $state but stray openceo-core processes run ($(echo $pids | tr '\n' ' ')); stop them, then start the unit."
    return 2
  fi
  echo "openceo-core host is stopped (unit $state)."
  return 1
}

logs() {
  require_unit
  journalctl --user -u "$UNIT" -n "${2:-120}" --no-pager || true
}

case "${1:-}" in
  start|restart)
    require_unit
    # Solo se despliega un commit limpio (TASK-2026-00367); el override
    # OPENCEO_ALLOW_DIRTY=1 deja dirty=true registrado en RELEASE.
    if ! bash "$ROOT_DIR/scripts/write-release.sh" "$ROOT_DIR" "$RUNTIME_DIR"; then
      exit 1
    fi
    systemctl --user "$1" "$UNIT" ;;
  stop)
    require_unit
    systemctl --user stop "$UNIT" ;;
  status) status ;;
  foreground) foreground ;;
  logs) logs "$@" ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|foreground|logs [lines]}"
    exit 1
    ;;
esac
