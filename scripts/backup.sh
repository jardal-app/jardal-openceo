#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-./backups}"
TS="$(date +%Y%m%d-%H%M%S)"
TARGET="${OUT_DIR}/mongo-${TS}"

mkdir -p "$TARGET"
mongodump --uri "${MONGODB_URL:-mongodb://localhost:27017}" --db "${MONGODB_DATABASE:-openceo}" --out "$TARGET"

echo "Backup saved to $TARGET"
