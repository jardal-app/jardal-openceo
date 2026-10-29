#!/usr/bin/env bash
# Alerta externa cuando el core agota los reinicios (TASK-2026-00370, §16.4).
# La invoca openceo-alert.service vía OnFailure; no depende del core:
# solo lee .runtime/last-exit.json y usa canales externos (spool local,
# syslog y webhook opcional). Nunca falla.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="${OPENCEO_RUNTIME_DIR:-$ROOT_DIR/.runtime}"
SPOOL="$RUNTIME_DIR/alerts.log"
LAST_EXIT="$RUNTIME_DIR/last-exit.json"

mkdir -p "$RUNTIME_DIR" || exit 0
umask 077
at="$(date -u +%FT%TZ)" || exit 0
last="{}"
[[ -f "$LAST_EXIT" ]] && last="$(cat "$LAST_EXIT" 2>/dev/null || echo '{}')"
msg="openceo-core agotó los reinicios de systemd at=$at last-exit=$last"
printf '%s\n' "$msg" >>"$SPOOL" || true
command -v logger >/dev/null 2>&1 \
  && logger -t openceo-alert -- "$msg" || true
if [[ -n "${OPENCEO_ALERT_WEBHOOK:-}" ]] && command -v curl >/dev/null 2>&1; then
  curl -fsS -m 10 -X POST -H 'Content-Type: application/json' \
    --data "{\"text\":\"$at openceo-core agotó los reinicios\"}" \
    "$OPENCEO_ALERT_WEBHOOK" >/dev/null 2>&1 || true
fi
exit 0
