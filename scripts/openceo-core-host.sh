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

resolve_runtime_pid() {
  ps -C python -o pid=,args= \
    | awk -v py="$VENV_PY" '$2 == py && $3 == "main.py" {print $1}' \
    | tail -n 1
}

load_env() {
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
  if [[ ! -x "$VENV_PY" ]]; then
    echo "Missing virtualenv python at $VENV_PY"
    echo "Run: scripts/setup.sh"
    return 1
  fi
  (
    cd "$APP_DIR"
    nohup "$VENV_PY" main.py >>"$LOG_FILE" 2>&1 &
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
  local pid runtime_pid
  pid=""
  if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
  fi
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    runtime_pid="$(resolve_runtime_pid || true)"
    pid="${runtime_pid:-$pid}"
  fi
  if [[ -z "$pid" ]]; then
    echo "openceo-core host not running."
    rm -f "$PID_FILE"
    return 0
  fi
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" || true
    fi
    echo "openceo-core host stopped."
  else
    echo "Stale pid file found."
  fi
  rm -f "$PID_FILE"
}

status() {
  load_env
  if is_running; then
    local pid
    pid="$(cat "$PID_FILE")"
    echo "openceo-core host is running (pid $pid) on port ${OPENCEO_PORT}."
  else
    echo "openceo-core host is stopped."
    return 1
  fi
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
