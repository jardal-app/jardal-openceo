# jardal-openceo - Project Context

Multi-agent enterprise orchestrator. FastAPI + LangGraph + MongoDB.
Phase 1 (Core) and Phase 2 (CEO + Dev Agent) completed.

## Key files
- openceo-core/main.py — FastAPI server, OpenAI-compat endpoint
- openceo-core/dev_session_service.py — Dev session orchestration
- openceo-core/agents/dev/provider_runtime.py — CLI subprocess execution
- openceo-core/settings.py — Environment config
- docker-compose.yml — Docker deployment (jardal-internal-net)

## Current state
- Docker container `openceo-core` running on jardal-internal-net
- OpenWebUI connected at http://openceo-core:8000/v1
- Models: openceo (CEO routing), openceo-dev (direct dev agent)
- Providers: clauded (claude --dangerously-skip-permissions), codex
- MongoDB: jardal-mongo, database openceo

## 2026-03-03 19:55 UTC | Session 69534ef6-fd77-4c25-92d5-a7fd8f682c35
- Title: que proyectos tienes configurados?
- Provider: claude
- Summary: {"status": "completed", "summary": "Encontré 3 organizaciones en development: Jardal (35 proyectos), DigitalInnovations y Univalle. Jardal incluye openceo, openwebui, trading, devops, atlassian, n8n, minio, mongo, traefik, portainer, vpn, mail, entre otros. El workspace actual es jardal-openceo.", "next_action": "¿Quieres detalles de algún proyecto específico o ejecutar algo en alguno?", "branch": null, "confidence": 1.0}

## 2026-03-03 19:57 UTC | Session c070709e-a456-412c-aaf4-00af3d6ace0b
- Title: hola que proyectos tienes disponibles
- Provider: claude
- Summary: {"status": "completed", "summary": "Encontré 3 organizaciones en development: Jardal (35 proyectos), DigitalInnovations y Univalle. Jardal incluye openceo, openwebui, trading, devops, atlassian, n8n, minio, mongo, traefik, portainer, vpn, mail, voltagent, entre otros. El workspace actual es jardal-openceo.", "next_action": "¿Quieres que trabaje en algún proyecto específico o necesitas detalles de alguno?", "branch": null, "confidence": 1.0}

## 2026-03-03 19:57 UTC | Session b22aab90-61ad-4222-83d5-2db2a9e6cb55
- Title: ### Task:
- Provider: claude
- Summary: {"status": "completed", "summary": "Generé follow-ups relevantes basados en el historial de chat sobre proyectos disponibles.", "next_action": "Entregar los follow-ups al usuario.", "branch": null, "confidence": 1.0, "follow_ups": ["¿Puedes darme más detalles del proyecto openceo?", "¿Cuáles son los proyectos de DigitalInnovations y Univalle?", "¿Puedes listar los 35 proyectos de Jardal?", "¿Cómo está configurado el proyecto de trading?", "¿Puedes ejecutar alguna tarea en el proyecto openwebui?"]}

## 2026-03-04 18:04 UTC | Session 801b339b-b777-413e-b881-bc016ef0ba5a
- Title: Sesión dev (jardal-openceo)
- Provider: claude
- Summary: {"status": "completed", "summary": "Listado del repo jardal-openceo: AGENTS.md, deploy/, docker-compose.yml, docs/, openceo-core/, openceo-dashboard/, openceo-worker/, projects/, README.md, scripts/, STATUS.md. Son 11 elementos entre archivos y directorios.", "next_action": "¿Quieres explorar algún directorio en particular?", "branch": null, "confidence": 1.0}

## 2026-03-04 18:05 UTC | Session e9933938-5b19-4582-8e19-7ce506bcf5bb
- Title: Sesión dev (jardal-openceo)
- Provider: claude
- Summary: {"status": "completed", "summary": "Listado del directorio jardal-openceo: AGENTS.md, deploy/, docker-compose.yml, docs/, .env, .env.example, openceo-core/, openceo-dashboard/, openceo-worker/, projects/, README.md, .runtime/, scripts/, STATUS.md, .venv/. Total 15 elementos entre archivos y directorios.", "next_action": "¿Quieres explorar algún directorio o archivo en particular?", "branch": null, "confidence": 1.0}

## 2026-03-05 10:43 UTC | Session 59238b9d-62bd-467e-870a-50fdd14fcb53
- Title: Sesión dev (jardal-openceo)
- Provider: codex
- Summary: {"status":"completed","summary":"codex peer ok en jardal-openceo","next_action":"Ninguna; listo para la siguiente instrucción.","waiting_reason":"","branch":"59238b9d-62bd-467e-870a-50fdd14fcb53-sesi-n-dev-jardal-openceo","confidence":1.0}
