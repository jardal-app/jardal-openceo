#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/openceo-core"
ENV_FILE="$APP_DIR/.env"
VENV_PY="$ROOT_DIR/.venv/bin/python"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/openceo-core-host.pid"
LOG_FILE="$RUNTIME_DIR/openceo-core-host.log"
RELEASE_FILE="$RUNTIME_DIR/RELEASE"

mkdir -p "$RUNTIME_DIR"

runtime_pids() {
  ps -C python -o pid=,args= \
    | awk -v py="$VENV_PY" '$2 == py && $3 == "main.py" {print $1}'
}

resolve_runtime_pid() {
  runtime_pids | tail -n 1
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

is_running() {
  local pid runtime_pid
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi

  runtime_pid="$(resolve_runtime_pid || true)"
  if [[ -n "$runtime_pid" ]] && kill -0 "$runtime_pid" 2>/dev/null; then
    echo "$runtime_pid" >"$PID_FILE"
    return 0
  fi

  return 1
}

start() {
  load_env
  if is_running; then
    echo "openceo-core host already running (pid $(cat "$PID_FILE"))."
    return 0
  fi
  local owner
  owner="$(port_owner || true)"
  if [[ -n "$owner" ]]; then
    echo "Port ${OPENCEO_PORT} already held by pid $owner (not openceo-core); refusing to start."
    return 1
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    echo "Missing virtualenv python at $VENV_PY"
    echo "Run: scripts/setup.sh"
    return 1
  fi
  # Identidad de release (TASK-2026-00367): solo se despliega desde un
  # commit limpio. Con cambios rastreados aborta, salvo override explícito
  # OPENCEO_ALLOW_DIRTY=1, que deja dirty=true registrado en RELEASE.
  if ! bash "$ROOT_DIR/scripts/write-release.sh" "$ROOT_DIR" "$RUNTIME_DIR"; then
    return 1
  fi
  "$VENV_PY" "$ROOT_DIR/scripts/sync-mcp-config.py" || echo "WARN: MCP config sync failed"
  (
    cd "$APP_DIR"
    # setsid: detach into a new session so agent/tool shells reaping their
    # process tree cannot take the daemon down with them (nohup alone stays
    # in the invoker's session). $! is the short-lived setsid parent; the
    # loop below re-resolves the real daemon pid.
    setsid -f "$VENV_PY" main.py >>"$LOG_FILE" 2>&1 </dev/null &
    echo $! >"$PID_FILE"
  )
  local runtime_pid
  for _ in 1 2 3 4 5; do
    runtime_pid="$(resolve_runtime_pid || true)"
    if [[ -n "$runtime_pid" ]]; then
      echo "$runtime_pid" >"$PID_FILE"
      break
    fi
    sleep 1
  done
  if is_running; then
    echo "openceo-core host started on port ${OPENCEO_PORT} (pid $(cat "$PID_FILE"))."
    return 0
  fi
  echo "Failed to start openceo-core host. Check $LOG_FILE"
  return 1
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

unit_managed() {
  # La unidad manda solo si está habilitada o activa: instalada pero
  # deshabilitada + arranque por script sigue siendo vía script.
  command -v systemctl >/dev/null 2>&1 || return 1
  # Sin sesión de usuario el bus no existe; se apunta al estándar.
  : "${XDG_RUNTIME_DIR:=/run/user/$(id -u)}"
  : "${DBUS_SESSION_BUS_ADDRESS:=unix:path=$XDG_RUNTIME_DIR/bus}"
  export XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
  systemctl --user cat openceo-core-host.service >/dev/null 2>&1 || return 1
  systemctl --user is-enabled --quiet openceo-core-host.service 2>/dev/null || \
    systemctl --user is-active --quiet openceo-core-host.service 2>/dev/null
}

stop() {
  local pid pids all target
  all=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      all="$pid"
    fi
  fi
  pids="$(runtime_pids || true)"
  for target in $pids; do
    if [[ " $all " != *" $target "* ]]; then
      all="$all $target"
    fi
  done
  # grep exits 1 with no pid; under pipefail that aborted stop/restart.
  all="$(echo "$all" | tr ' ' '\n' | { grep -E '^[0-9]+$' || true; } | sort -un | tr '\n' ' ')"
  if [[ -z "${all// }" ]]; then
    if [[ -f "$PID_FILE" ]]; then
      echo "Stale pid file found."
    else
      echo "openceo-core host not running."
    fi
    rm -f "$PID_FILE"
    return 0
  fi
  for target in $all; do
    kill "$target" 2>/dev/null || true
  done
  sleep 1
  for target in $all; do
    if kill -0 "$target" 2>/dev/null; then
      kill -9 "$target" 2>/dev/null || true
    fi
  done
  echo "openceo-core host stopped."
  rm -f "$PID_FILE"
}

status() {
  load_env
  if unit_managed; then
    local state pids count owner pid
    state="$(systemctl --user is-active openceo-core-host.service 2>/dev/null || echo unknown)"
    pids="$(runtime_pids || true)"
    if [[ -f "$RELEASE_FILE" ]]; then
      echo "release: $(cat "$RELEASE_FILE")"
    fi
    if [[ "$state" == "active" ]]; then
      # Bajo la unidad también se verifica instancia única y puerto.
      count="$(echo "$pids" | grep -cE '^[0-9]+$' || true)"
      if [[ "$count" -gt 1 ]]; then
        echo "WARNING: unit active but $count openceo-core instances run (pids: $(echo $pids | tr '\n' ' ')). Only one may run; stop all and start once."
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
      echo "WARNING: unit $state but legacy PIDs run ($(echo $pids | tr '\n' ' ')); use one path (CLAUDE.md §3)."
      return 2
    fi
    echo "openceo-core host is stopped (unit $state)."
    return 1
  fi
  local pids count owner
  pids="$(runtime_pids || true)"
  count="$(echo "$pids" | grep -cE '^[0-9]+$' || true)"
  owner="$(port_owner || true)"
  if [[ -z "${pids// }" ]]; then
    if [[ -n "$owner" ]]; then
      echo "openceo-core host is stopped, but port ${OPENCEO_PORT} is held by pid $owner."
    else
      echo "openceo-core host is stopped."
    fi
    return 1
  fi
  if [[ "$count" -gt 1 ]]; then
    echo "WARNING: $count openceo-core instances running (pids: $(echo $pids | tr '\n' ' ')). Only one may run; stop all and start once."
    return 2
  fi
  local pid
  pid="$(echo "$pids" | head -n 1)"
  if [[ -n "$owner" && "$owner" != "$pid" ]]; then
    echo "WARNING: openceo-core (pid $pid) is not the port ${OPENCEO_PORT} owner (pid $owner)."
    return 2
  fi
  echo "$pid" >"$PID_FILE"
  if [[ -f "$RELEASE_FILE" ]]; then
    echo "release: $(cat "$RELEASE_FILE")"
  fi
  echo "openceo-core host is running (pid $pid) on port ${OPENCEO_PORT}."
}

logs() {
  # Bajo la unidad la salida va al journal, no al fichero histórico.
  if unit_managed && command -v journalctl >/dev/null 2>&1; then
    journalctl --user -u openceo-core-host.service -n "${2:-120}" --no-pager || true
    return 0
  fi
  touch "$LOG_FILE"
  tail -n "${2:-120}" "$LOG_FILE"
}

case "${1:-}" in
  start) start ;;
  stop)
    # Bajo la unidad manda systemd: matar PIDs sin avisar provoca
    # un reinicio inmediato (Restart=on-failure), así que stop no pararía.
    if unit_managed; then
      systemctl --user stop openceo-core-host.service
    else
      stop
    fi ;;
  restart)
    # Con cambios rastreados no se toca la instancia sana: se valida
    # antes de parar (TASK-2026-00367, hallazgo de revisión).
    if ! bash "$ROOT_DIR/scripts/write-release.sh" "$ROOT_DIR" "$RUNTIME_DIR"; then
      exit 1
    fi
    if unit_managed; then
      systemctl --user restart openceo-core-host.service
    else
      stop; start
    fi ;;
  status) status ;;
  foreground) foreground ;;
  logs) logs "$@" ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|foreground|logs [lines]}"
    exit 1
    ;;
esac
