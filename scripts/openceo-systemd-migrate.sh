#!/usr/bin/env bash
# Instala o retira la unidad systemd de usuario del core (TASK-2026-00370,
# §16.4). Es la única forma de correr el core (TASK-2026-00510): install
# para cualquier proceso suelto antes de habilitar la unidad; uninstall la
# retira y el core queda parado hasta reinstalarla.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${OPENCEO_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
HOST_UNIT="$ROOT_DIR/deploy/systemd/openceo-core-host.service"
ALERT_UNIT="$ROOT_DIR/deploy/systemd/openceo-alert.service"

need_systemd() {
  command -v systemctl >/dev/null 2>&1 || {
    echo "systemctl no disponible; registre el comando exacto en TASK-2026-00370." >&2
    exit 1
  }
}

check_linger() {
  # Sin linger la unidad de usuario no vuelve tras reiniciar el host.
  local user linger
  user="${USER:-$(id -un)}"
  command -v loginctl >/dev/null 2>&1 || {
    echo "WARN: sin loginctl; no se pudo verificar Linger para $user." >&2
    return 0
  }
  linger="$(loginctl show-user "$user" -p Linger --value 2>/dev/null || true)"
  [[ "$linger" == "yes" ]] || {
    echo "Linger no activo para $user: la unidad no volvería tras reiniciar." >&2
    echo "Habilítelo con: sudo loginctl enable-linger $user" >&2
    echo "y registre el resultado en TASK-2026-00370." >&2
    exit 1
  }
}

link_units() {
  mkdir -p "$UNIT_DIR"
  ln -sfn "$HOST_UNIT" "$UNIT_DIR/openceo-core-host.service"
  ln -sfn "$ALERT_UNIT" "$UNIT_DIR/openceo-alert.service"
  systemctl --user daemon-reload
}

unlink_units() {
  for unit in openceo-core-host.service openceo-alert.service; do
    if [[ -L "$UNIT_DIR/$unit" ]]; then
      rm -f "$UNIT_DIR/$unit"
    fi
  done
  systemctl --user daemon-reload
}

cmd_status() {
  need_systemd
  systemctl --user is-active openceo-core-host.service 2>/dev/null || echo inactive
  bash "$ROOT_DIR/scripts/openceo-core-host.sh" status || true
}

stop_strays() {
  # A core started outside the unit would hold the lock (exit 3 for the
  # unit); SIGTERM lets its lifespan shut down cleanly first.
  local py="$ROOT_DIR/.venv/bin/python" pids
  pids="$(ps -C python -o pid=,args= | awk -v py="$py" '$2 == py && $3 == "main.py" {print $1}')"
  [[ -z "${pids// }" ]] && return 0
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 $pids 2>/dev/null || return 0
    sleep 1
  done
  kill -9 $pids 2>/dev/null || true
}

cmd_install() {
  need_systemd
  check_linger
  stop_strays
  link_units
  systemctl --user enable --now openceo-core-host.service
  sleep 2
  bash "$ROOT_DIR/scripts/openceo-core-host.sh" status
}

cmd_uninstall() {
  need_systemd
  systemctl --user disable --now openceo-core-host.service || true
  unlink_units
  echo "Unidad retirada: el core no corre hasta reinstalarla (no hay arranque por script)."
  echo "  scripts/openceo-systemd-migrate.sh install"
}

case "${1:-}" in
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  *)
    echo "Usage: $0 {install|uninstall|status}"
    exit 1
    ;;
esac
