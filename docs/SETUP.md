# Setup (Dev Agent Production)

Este proyecto **no** levanta MongoDB, n8n ni OpenWebUI localmente.
Usa servicios externos ya desplegados.

## 1) Entorno Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r openceo-core/requirements.txt
pip install -r requirements-dev.txt  # ruff: gate ruff_swallowed_exceptions (TASK-2026-00369)
./scripts/install-agent-requirements.sh
```

## 2) Configuración

```bash
cp openceo-core/.env.example openceo-core/.env
cp openceo-core/.env.finance.local.example openceo-core/.env.finance.local  # opcional, solo si usaras finance
# editar openceo-core/.env
```

Variables mínimas:
- `MONGODB_URL`
- `MONGODB_DATABASE`
- `DEV_EXECUTION_MODE=cli`
- `CODEX_CLI_PATH`
- `CLAUDE_CODE_PATH`
- `GEMINI_CLI_PATH`
- `OPENCODE_CLI_PATH`

Si vas a usar OpenCode Go:
- agrega la credencial en `~/.local/share/opencode/auth.json` vía `/connect` o manualmente
- opcionalmente expón `OPENCODE_GO_API_KEY` en `openceo-core/.env` para scripts propios
- usa `OPENCODE_MODEL_OVERRIDE` si quieres forzar un modelo Go para el provider `opencode`

Si vas a usar el agente de finanzas localmente:

```bash
python -m playwright install chromium
```

## 3) Ejecutar API (host mode, instancia única)

```bash
scripts/openceo-core-host.sh start    # o restart; nunca `python main.py` directo (CLAUDE.md §3)
scripts/openceo-core-host.sh status
```

## 4) Smoke manual

```bash
curl http://localhost:18765/health
curl http://localhost:18765/api/projects
```
