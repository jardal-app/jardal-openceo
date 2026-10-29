#!/usr/bin/env bash
# Escribe .runtime/RELEASE con el SHA exacto que se ejecuta (TASK-2026-00367).
# Uso: write-release.sh <repo> <runtime-dir>
# Aborta (exit 1) si hay cambios rastreados, salvo OPENCEO_ALLOW_DIRTY=1,
# que arranca dejando dirty=true registrado.
set -euo pipefail

REPO="${1:?repo}"
OUT_DIR="${2:?runtime-dir}"
FILE="$OUT_DIR/RELEASE"

sha="$(git -C "$REPO" rev-parse HEAD)" || {
  echo "write-release: no se pudo leer el HEAD de $REPO" >&2
  exit 1
}

if [[ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]]; then
  if [[ "${OPENCEO_ALLOW_DIRTY:-0}" != "1" ]]; then
    echo "write-release: hay cambios rastreados sin commitear; revisalos o" \
      "repite con OPENCEO_ALLOW_DIRTY=1 (queda dirty=true registrado)." >&2
    exit 1
  fi
  dirty=true
else
  dirty=false
fi

mkdir -p "$OUT_DIR"
at="$(date -u +%FT%TZ)"
tmp="$FILE.tmp.$$"
printf '{"sha":"%s","dirty":%s,"at":"%s"}\n' "$sha" "$dirty" "$at" >"$tmp"
mv "$tmp" "$FILE"
echo "release $sha dirty=$dirty"
