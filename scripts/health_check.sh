#!/usr/bin/env bash
set -euo pipefail

API_URL="${1:-http://localhost:8000}"

echo "Checking ${API_URL}/health"
curl -fsS "${API_URL}/health" | sed 's/^/health: /'

echo "Checking ${API_URL}/api/tasks"
curl -fsS "${API_URL}/api/tasks?limit=1" | sed 's/^/tasks: /'

echo "OK"
