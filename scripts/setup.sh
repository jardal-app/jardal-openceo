#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f openceo-core/.env ]]; then
  cp openceo-core/.env.example openceo-core/.env
fi

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r openceo-core/requirements.txt
./scripts/install-agent-requirements.sh

if python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('playwright') else 1)"; then
  python -m playwright install chromium
fi

echo "Setup complete."
echo "1) source .venv/bin/activate"
echo "2) configura openceo-core/.env con tus servicios externos (Mongo/n8n/OpenWebUI)"
echo "3) si usaras finanzas, configura openceo-core/.env.finance.local"
echo "4) cd openceo-core && python main.py"
