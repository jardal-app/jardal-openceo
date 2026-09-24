# CLAUDE.md - OpenCEO Developer & Agent Guide

Guía y contrato de arquitectura para agentes y desarrolladores que interactúan con OpenCEO.

---

## 1. Arquitectura y Stack

- **Orquestador**: LangGraph (Python) en `openceo-core/`
- **Gateway Cognitivo (First Layer)**: `openceo-core/first_layer.py` con `DeepSeek v4 Flash` (`CEO_LLM_MODEL`)
- **API Server**: FastAPI con endpoints compatibles OpenAI `/v1/chat/completions` en puerto `18765` (host) u `8000` (docker)
- **Base de Datos**: MongoDB (`tasks`, `dev_sessions`, `user_context`, `project_presets`, `task_reviews`, `workflow_runs`)
- **Voz en Tiempo Real**: LiveKit + Silero VAD + Groq Whisper STT + OpenAI TTS (`openceo-voice/`)
- **App Móvil**: Android Jetpack Compose + LiveKit SDK (`openceo-voice-android/`)
- **Dashboard**: React 19 + Vite 6 + Tailwind CSS v4 (`openceo-react/`)
- **Coding Providers**: Codex CLI (GPT-5.4), Claude Code (Opus 4.6), Antigravity CLI (`agyd`), OpenCode CLI, Kimi CLI

---

## 2. Primera Capa Cognitiva: DeepSeek v4 Flash (`first_layer.py`)

Cualquier mensaje entrante por voz o chat que no tenga prefijo `@` es procesado primero por `FirstLayerGateway`:
1. **Comprensión contextual**: Conoce los proyectos configurados, el proyecto activo, el ticket en foco, las terminales tmux del servidor y los presets.
2. **Personalidad por voz**:
   - Tono natural y fluido de colega técnico.
   - Uso obligatorio de la primera persona plural impersonal: *"Tenemos..."*, *"Estamos en..."*, *"Veo que..."*.
   - Sin tablas markdown, listas largas con asteriscos, UUIDs ni URLs en `spoken_reply`.
3. **Conteo de tickets**:
   - Responde inmediatamente con número de tickets abiertos y resumen corto (ej. *"Tenemos abiertos 3 tickets en jardal-openceo: el #48 sobre trading en progreso..."*).
4. **Guía activa para usuarios perdidos (`guidance`)**:
   - Si el usuario dice *"ayuda"*, *"estoy perdido"*, *"qué puedo hacer?"*, lo guía con opciones concretas de proyectos, tickets, presets y sesiones dev.
   - Si una orden no se entiende, aclara cordialmente y ofrece opciones.
5. **Presets por proyecto (`preset_run`, `preset_list`, `preset_save`, `preset_output`)**:
   - Ejecuta comandos configurados en `project.yaml` o guardados dinámicamente en Mongo.
   - Si `tmux: true`, corre en una terminal tmux dedicada (`p-{proyecto}-{nombre}`) manteniéndola abierta para inspección por voz (*"qué sale en el preset build"*).

---

## 3. Presets por Proyecto

Definidos en `projects/{proyecto}/project.yaml` o en MongoDB `project_presets`:

```yaml
presets:
  test:
    cmd: "pytest tests/ -q"
    description: "Correr pruebas unitarias"
    tmux: true
  build:
    cmd: "docker compose build openceo-core"
    description: "Compilar imágenes Docker"
    tmux: true
  restart:
    cmd: "scripts/openceo-core-host.sh restart"
    description: "Reiniciar backend host"
    tmux: false
  status:
    cmd: "git status"
    description: "Ver estado de git"
    tmux: false
```

---

## 4. Comandos de Desarrollo y Validación

### Validación Python
```bash
python3 -m py_compile openceo-core/main.py openceo-core/first_layer.py openceo-core/project_service.py openceo-core/db/mongo.py
```

### Ejecutar o reiniciar OpenCEO en Host Mode
```bash
scripts/openceo-core-host.sh restart
# O verificar estado:
scripts/openceo-core-host.sh status
```

### Compilar APK Android
```bash
cd openceo-voice-android
./gradlew assembleDebug
# El APK generado queda en:
# app/build/outputs/apk/debug/app-debug.apk
```

---

## 5. Motor de Workflows, Autodiscovery y Dev Tier S

### Workflows Deterministas (Tier 0 - 0 Tokens)
- Almacenados en `workflows/<agente>/<nombre>.yaml`.
- Ejecución directa secuencial sin gasto de LLM.
- Se invocan registrando tickets con `params: {"workflow": "<id_workflow>"}` o con `params: {"use_workflow": true}` si el YAML define `auto_match: true` y triggers.

### Autodiscovery de Workflows (`deepseek-v4-flash`)
- Activación explícita con `params: {"discover_workflow": true}`.
- DeepSeek v4 Flash recibe el catálogo exacto de las 15 herramientas disponibles (`WorkflowToolRegistry.describe()`) y sintetiza una propuesta en YAML libre de alucinaciones.
- Crea un registro en `discovery_runs` (`status: proposed`) y el ticket queda en `waiting`. Al aprobarse, se guarda el YAML en disco como nuevo workflow cristalizado.

### Human-in-the-Loop (HITL) en YAMLs
- Los pasos con `type: human_in_the_loop` y `permission: <fase>` pausan la ejecución (`WorkflowRunStatus.BLOCKED_HUMAN`).
- El ticket se marca en `status: waiting` con `phase: <permission>` (ej. `qa_review`).
- Al aprobar la acción desde la UI/API (`create_phase_action`), el motor reanuda el workflow automáticamente ejecutando los pasos restantes sin reiniciar.

### Dev Tier S (Consenso Crítico Multi-IA)
- Se activa enviando `"coding_tool": "tier_s"` en la raíz del ticket o dentro de `params`.
- En proyectos con `dev.coding_tool: tier_s` en `project.yaml` (como `jardal-trading`), todos los tickets dev entran a Tier S de forma predeterminada.
- Orquesta: Planner (`clauded`), Plan Reviewer (`codexf` / reasoning xhigh), Builder (`claudep`), y 3 revisores cruzados (Codex, Antigravity CLI `agyd`, OpenCode Kimi/Qwen) en 2 rondas de auditoría.

---

## 6. Horizon — Contexto Personal y Estratégico del CEO

Horizon **no es un agente separado** ni tiene runtime propio. Es la capacidad y contexto personal/estratégico que consume el CEO: misión, objetivos, hitos, capacidad, ideas capturadas y siguiente acción (`mission → objective → milestone → action → result`).
- **Especificación canónica**: [`jardal-horizon/README.md`](file:///home/develop/development/Jardal/jardal-openceo/jardal-horizon/README.md) v2.1.
- **Implementación en Core**: [`openceo-core/horizon/`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/).
- **Tracking operacional**: los tickets viven en ERPNext (`https://erp.jardal.app/app/task`); `STATUS.md` guarda el avance y los traspasos de sesión. `PENDINGS.md` es solo un puntero al backlog.

### Fases de Implementación (Roadmap)
1. **HZ-001 — Contexto personal del CEO** (Completada): Modelos de jerarquía (`models.py`), mandato (`mandate.py`), privacidad estricta (`privacy.py`), clasificador determinista antes de LLM (`classify.py`), colecciones Mongo `horizon_*` e importador idempotente (`importer.py`).
2. **HZ-002 — Ciclo proactivo del CEO** (Completada): Capacidad diaria determinista al 70% (`capacity.py`), planificador lexicográfico (`planner.py`), disparadores intencionales persistidos sin barridos (`triggers.py`), revisión periódica (`review.py`), workflows `ceo/horizon_daily.yaml` y `ceo/horizon_review.yaml`.
3. **HZ-003 — Delegación y dev móvil** (Implementada, pendiente piloto con 3 tickets reales): Encargo de tickets de alcance personal sobre sesiones dev existentes en background, medición de tiempo humano de supervisión y retrabajo, y entrega única del resultado ("delivered once") tolerante a desconexión del móvil.
4. **HZ-004 — Fuentes conectadas y espacio personal** (vista implementada; fuente conectada pendiente de necesidad observada): vista del espacio personal en `openceo-react` — ruta `/dashboard/personal`, permiso `personal_space` — con misión, objetivos e hitos, plan del día (capacidad, bloques, exclusiones y decisiones), siguiente acción, delegación medida con entrega única, captura y métricas humanas. La integración de fuentes pasivas queda condicionada a que el uso demuestre el ahorro (§9.8 y §11: *"conectar fuente solo si aporta ahorro"*).
5. **HZ-005 — Frontera financiera con ERPNext**: Consumo de saldos y asientos vía adapter de Finance hacia ERPNext (TICKET-025, `docs/decision-2026-09-19-sistema-financiero-erpnext.md`), sin almacenar dinero en floats ni crear contabilidad propia en OpenCEO.

### Reglas Críticas de Horizon
- **Clasificador determinista antes del LLM**: Evita que frases personales se clasifiquen erróneamente como instrucciones dev enviadas a tmux, y permite operar con presupuesto de IA agotado.
- **Privacidad estricta (Sin espejo a Notion)**: Ninguna tarea personal se sincroniza a Notion (filtrado en `TaskService._task_needs_notion_mirror` y `MongoRepository.list_tasks_missing_notion_mirror`).
- **Desconocido es `null`, nunca `0`**: Cero es un valor medido; desconocido representa ausencia de medición con incertidumbre `unknown`.
- **Sin polling ni barridos**: Disparadores persistidos en `horizon_triggers` con `next_run_at` y un único timer reactivo despertado por `enqueue()`.
- **Modo degradado por construcción**: El ciclo de capacidad, planificación y revisión no invoca ningún LLM.

### Validación de Horizon
```bash
# Correr suite de Horizon (usar siempre venv del root)
./.venv/bin/python -m pytest tests/test_horizon_*.py -q

# Build del dashboard (vista del espacio personal)
cd openceo-react && npm run build

# Comprobación de servicio y endpoints
./scripts/openceo-core-host.sh status
./scripts/chat.sh "hoy solo tengo 15 minutos"
./scripts/chat.sh "qué sigue"
```

---

## 7. Reglas Críticas de Eficiencia y Escalabilidad (AGENTS.md)

1. **Sin barridos ni polling periódico**: Prohibido hacer polling contra Mongo o APIs externas en background loops. Todo cambio debe ser reactivo por eventos CRUD.
2. **Respuesta inmediata en modo dev**: Los turnos de código se procesan en background (`send_message_async`), la respuesta HTTP/SSE es instantánea.
3. **1 API call por operación**: No dividir llamadas innecesariamente.
4. **Respetar aislamiento de credenciales y paths**:
   - Host mode: usa `~/.codex`, `~/.claude`, `/home/develop/.local/bin/agyd`.
   - `gemini` en OpenCEO corre con `agyd` (`--dangerously-skip-permissions`).

---

## 8. Tickets: ERPNext es el registro de negocio

Desde el 2026-09-24 el trabajo empresarial pertenece a ERPNext y OpenCEO conserva la
**ejecución**. La dirección está en `docs/OPENCEO_DIRECTION.md` v1.1; las fronteras, en
`docs/ARCHITECTURE_REFERENCE_2026.md` §System Boundaries.

| Pregunta | Dónde se responde |
| --- | --- |
| ¿Qué trabajo existe? | ERPNext — `Task` (ticket) y `Project` (identidad de trabajo) |
| ¿Cómo se está ejecutando? | MongoDB — `tasks`, sesiones, runs |

**No hay flag de encendido/apagado, y es deliberado.** Existió `ERPNEXT_TICKETS_ENABLED` y se
eliminó: apagado, OpenCEO creaba tickets que vivían solo en Mongo y que el sistema de negocio
nunca vería, y la reconciliación no los recupera. La resiliencia ante un ERPNext caído la da la
marca `erpnext_pending`, no un interruptor.

### Flujo directo (OpenCEO → ERPNext)

`TaskService.create_task()` crea el ticket de negocio **antes** de persistir en Mongo, y tiene
tres ramas: éxito, `erpnext_pending` (timeout: el ticket se crea igual, **sin `autorun`**, y se
repara al arrancar) y fallo duro (4xx: no se persiste nada). Las transiciones se propagan por un
observador en `MongoRepository.patch_task`.

### Flujo inverso (ERPNext → OpenCEO)

Un webhook de Frappe (`OpenCEO Ticket Sync` para `on_update`, `OpenCEO Ticket Trash` para
`on_trash`) llama a `POST /api/integrations/erpnext/task` con la cabecera
`X-OpenCEO-Webhook-Secret`. Cerrar un ticket lo pasa a `completed`; cancelarlo o borrarlo, a
`cancelled` (terminal, no reanuda). Los estados que son **eco** de lo que OpenCEO ya empujó se
ignoran, para no crear un bucle.

### Reglas al tocar esto

1. **`completed` no cierra el ticket en ERPNext a propósito**: cerrar es una decisión de negocio.
2. **`status_pushed` guarda el vocabulario de OpenCEO, no el de ERPNext** — comparar `in_progress`
   contra `Working` rompería la idempotencia. Y `str(TaskStatus.QUEUED)` da `'TaskStatus.QUEUED'`:
   usar `.value`.
3. **Nunca un `create_document(doctype, payload)` genérico** hacia ERPNext: solo operaciones
   semánticas cerradas, y el cliente tiene allowlist de DocTypes.
4. **`openceo_ref` es la clave de idempotencia** (nullable, única). Un reintento usa el mismo valor.
5. **La consola de `bench` decodifica stdin como latin-1**: usar escapes `é` en scripts ASCII.

