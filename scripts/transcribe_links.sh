#!/usr/bin/env bash
# Batch-transcribe media links with Groq Whisper.
#
# Source-agnostic: TikTok, YouTube, Instagram, X/Twitter, or any site yt-dlp
# supports. Reads one link per line from LINKS_FILE and writes every transcript
# into a single OUT_FILE. Already-transcribed links are skipped on re-runs.
# Downloaded audio is deleted after each successful transcript, so the temp
# AUDIO_DIR stays clean; pass --keep-audio to preserve it.
#
# Usage:
#   scripts/transcribe_links.sh                        # uses the default tmp paths
#   scripts/transcribe_links.sh <links> <out>          # custom paths
#   scripts/transcribe_links.sh <links> <out> --keep-audio
#
# The GROQ_API_KEY is read from openceo-core/.env (dedicated key, no voice fallback).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$ROOT_DIR/.venv/bin/python"
TOOL="$ROOT_DIR/openceo-core/tools/media_transcribe.py"

LINKS_FILE="${1:-$ROOT_DIR/tmp/links_tiktok.txt}"
OUT_FILE="${2:-$ROOT_DIR/tmp/transcripcion_tiktok.txt}"
AUDIO_DIR="${AUDIO_DIR:-$ROOT_DIR/tmp/audios_tiktok}"

if [[ ! -x "$VENV_PY" ]]; then
  echo "No se encontró el virtualenv en $VENV_PY" >&2
  exit 1
fi

if [[ ! -f "$LINKS_FILE" ]]; then
  echo "No existe el archivo de enlaces: $LINKS_FILE" >&2
  echo "Crea el archivo con un enlace de TikTok por línea." >&2
  exit 1
fi

if ! grep -qE '^[[:space:]]*[^#[:space:]]' "$LINKS_FILE"; then
  echo "El archivo $LINKS_FILE está vacío. Agrega un enlace por línea." >&2
  exit 1
fi

echo "Enlaces : $LINKS_FILE"
echo "Salida  : $OUT_FILE"
echo "Audios  : $AUDIO_DIR"
echo

exec "$VENV_PY" "$TOOL" --links "$LINKS_FILE" --out "$OUT_FILE" --audio-dir "$AUDIO_DIR" "${@:3}"
