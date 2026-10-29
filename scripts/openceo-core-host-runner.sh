#!/usr/bin/env bash
# Lanzador histórico para systemd; delega en el modo en primer plano del
# script canónico para no duplicar el entorno (TASK-2026-00370).
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/openceo-core-host.sh" foreground
