#!/usr/bin/env bash
# Registra la última salida del core para el cazador de fallos
# (TASK-2026-00370, diseño v2 §16.4). Lo invoca systemd vía ExecStopPost
# con SERVICE_RESULT, EXIT_CODE y EXIT_STATUS en el entorno.
# Nunca falla: un error aquí no debe enmascarar la salida real.
set -uo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_DIR="${OPENCEO_RUNTIME_DIR:-$ROOT_DIR/.runtime}"
OUT="$RUNTIME_DIR/last-exit.json"

mkdir -p "$RUNTIME_DIR" || exit 0
at="$(date -u +%FT%TZ)" || exit 0
tmp="$OUT.tmp.$$"
PYBIN="$ROOT_DIR/.venv/bin/python"
[[ -x "$PYBIN" ]] || PYBIN="python3"
SERVICE_RESULT="${SERVICE_RESULT:-unknown}" \
EXIT_CODE="${EXIT_CODE:-unknown}" \
EXIT_STATUS="${EXIT_STATUS:-unknown}" \
RECORD_AT="$at" \
"$PYBIN" - "$tmp" <<'PY' || exit 0
import json, os, sys
doc = {
    "at": os.environ["RECORD_AT"],
    "service_result": os.environ["SERVICE_RESULT"],
    "exit_code": os.environ["EXIT_CODE"],
    "exit_status": os.environ["EXIT_STATUS"],
}
with open(sys.argv[1], "w") as fh:
    json.dump(doc, fh)
    fh.write("\n")
PY
mv "$tmp" "$OUT" || exit 0
