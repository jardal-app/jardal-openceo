#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$ROOT_DIR/openceo-core"
ENV_FILE="$APP_DIR/.env"
VENV_PY="$ROOT_DIR/.venv/bin/python"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/openceo-core-host.pid"
LOG_FILE="$RUNTIME_DIR/openceo-core-host.log"

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
  export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.opencode/bin:$PATH"
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
  all="$(echo "$all" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -un | tr '\n' ' ')"
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
  echo "openceo-core host is running (pid $pid) on port ${OPENCEO_PORT}."
}

logs() {
  touch "$LOG_FILE"
  tail -n "${2:-120}" "$LOG_FILE"
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) stop; start ;;
  status) status ;;
  logs) logs "$@" ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs [lines]}"
    exit 1
    ;;
esac
