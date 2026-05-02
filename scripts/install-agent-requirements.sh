#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CORE_DIR="${1:-$ROOT_DIR/openceo-core}"

if [[ ! -d "$CORE_DIR/agents" ]]; then
  echo "Agents directory not found: $CORE_DIR/agents" >&2
  exit 1
fi

find "$CORE_DIR/agents" -mindepth 2 -maxdepth 2 -type f -name requirements.txt | sort | while IFS= read -r req_file; do
  pip install -r "$req_file"
done
