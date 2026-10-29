#!/usr/bin/env bash
# Migra el core entre arranque por script y unidad systemd de usuario
# (TASK-2026-00370, §16.4). Nunca hay dos instancias: install para el
# arranque por script antes de habilitar la unidad; uninstall la retira
# sin arrancar nada (el operador decide cuándo volver al script).
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

cmd_install() {
  need_systemd
  check_linger
  # Sin dos instancias: primero se para el arranque por script.
  bash "$ROOT_DIR/scripts/openceo-core-host.sh" stop
  link_units
  systemctl --user enable --now openceo-core-host.service
  sleep 2
  bash "$ROOT_DIR/scripts/openceo-core-host.sh" status
}

cmd_uninstall() {
  need_systemd
  systemctl --user disable --now openceo-core-host.service || true
  unlink_units
  echo "Unidad retirada. Para volver al script:"
  echo "  scripts/openceo-core-host.sh restart && scripts/openceo-core-host.sh status"
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
