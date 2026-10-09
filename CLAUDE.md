# CLAUDE.md - OpenCEO Developer & Agent Guide

Guía y contrato de arquitectura para agentes y desarrolladores que interactúan con OpenCEO.

**Vault de este proyecto**: `projects/jardal/jardal-openceo` dentro de
`/home/develop/development/Jardal/jardal-knowledge/vault/` (usar el skill `obsidian-vault-links`).
Trabajo temporal de tickets: `docs/work/TASK-2026-000NN-tema.md`.

Instrucciones operativas del repo (F7a, los CLI del roster las cargan directamente): §10 de este archivo (también visible como `AGENTS.md`, symlink a este archivo).

Continuar el roadmap de autonomía: el dueño escribe **`sigue`** y la sesión aplica la receta de §11.

---

## 1. Arquitectura y Stack

- **Orquestador**: LangGraph (Python) en `openceo-core/`
- **Gateway Cognitivo (First Layer)**: `openceo-core/first_layer.py` con `DeepSeek Flash` (`CEO_LLM_MODEL`)
- **API Server**: FastAPI con endpoints compatibles OpenAI `/v1/chat/completions` en puerto `18765` (host) u `8000` (docker)
- **Base de Datos**: MongoDB (`tasks`, `dev_sessions`, `user_context`, `project_presets`, `task_reviews`, `workflow_runs`)
- **Voz en Tiempo Real**: LiveKit + Silero VAD + Groq Whisper STT + OpenAI TTS (`openceo-voice/`)
- **App Móvil**: Android Jetpack Compose + LiveKit SDK (`openceo-voice-android/`)
- **Dashboard**: React 19 + Vite 6 + Tailwind CSS v4 (`openceo-react/`)
- **Coding Providers**: `musej`/`muse` (muse-spark-1.3-contributor), `codexj` (gpt-6.1-sol), `claudej` (opus/opusplan), `qwen` (qwen3.8-max, glm-5.3), `dsh` (deepseek-v4.1-flash); reservas `claudep`, `agyd`, `opencode`; QA con Kimi CLI y OpenCode (roster vigente en `openceo-core/config_loader.py`)

---

## 2. Primera Capa Cognitiva: DeepSeek Flash (`first_layer.py`)

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

### Instancia única (obligatorio)

Solo puede correr UNA instancia de openceo-core por host. El lifespan de arranque tiene efectos globales (tareas interrumpidas, reconciliación tmux, schedulers) y uvicorn lo ejecuta ANTES de bindear el puerto: una segunda instancia corrompe el estado compartido y solo después muere con `Errno 98`. Una unit en churn o un arranque manual repiten el incidente F0 cada pocos segundos.

- Arranque canónico: `scripts/openceo-core-host.sh restart|start` — nunca `python main.py` directo (sin env bindea el puerto 8000 por defecto y convive como segundo cerebro en otro puerto).
- El script verifica en cada `start`/`status`: pids coincidentes + dueño del puerto (`status` avisa con exit 2 si hay más de una); `stop` mata TODAS las coincidencias.
- `main.py` toma un flock en `.runtime/openceo-core-host.lock` antes del lifespan; si está tomado, muere con FATAL + exit 3 sin tocar Mongo ni schedulers. El kernel lo libera ante cualquier muerte: no hay estado stale.
- Pendiente del dueño (2026-10-05): launcher canónico — script vs unit systemd `openceo-core-host.service` (parada el 2026-10-05 tras 167 reinicios en churn; NO habilitar ambas a la vez).

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

### Autodiscovery de Workflows (`deepseek-flash`)
- Activación explícita con `params: {"discover_workflow": true}`.
- DeepSeek Flash recibe el catálogo exacto de las herramientas disponibles (`WorkflowToolRegistry.describe()`) y sintetiza una propuesta en YAML libre de alucinaciones.
- Crea un registro en `discovery_runs` (`status: proposed`) y el ticket queda en `waiting`. Al aprobarse, se guarda el YAML en disco como nuevo workflow cristalizado.

### Human-in-the-Loop (HITL) en YAMLs
- Los pasos con `type: human_in_the_loop` y `permission: <fase>` pausan la ejecución (`WorkflowRunStatus.BLOCKED_HUMAN`).
- El ticket se marca en `status: waiting` con `phase: <permission>` (ej. `qa_review`).
- Al aprobar la acción desde la UI/API (`create_phase_action`), el motor reanuda el workflow automáticamente ejecutando los pasos restantes sin reiniciar.

### Planner único de objetivos (global)
- El único que decide qué tickets deben existir para un Objetivo: Muse Code headless, un run fresco por ciclo, salida `PlannerResult` validada (Pydantic + validación determinista).
- Normativos: `openceo-core/objectives/routing_policy.md` (el planner lo lee) y `docs/decision-2026-10-05-planner-objetivos-muse.md` (ADR).
- Sustituye a `claudej`/`TICKETS.yaml` (TASK-2026-00112; motor en `openceo-core/objectives/`, TASK-2026-00117).
- El plan Claude/Codex de un ticket Tier S planifica cómo implementar ese ticket, no es un segundo planner; tampoco lo es el orden determinista del día en Horizon.

### Dev Tier S (Consenso Crítico Multi-IA)
- Se activa enviando `"coding_tool": "tier_s"` en la raíz del ticket o dentro de `params` (routing per-task; `dev.coding_tool` en `project.yaml` está obsoleto).
- **Criterio de selección (dueño, 2026-10-09)**: S se elige cuando orquestar varios modelos independientes dará un resultado materialmente más correcto, en **cualquier dominio**. Dinero, pagos y auth son ejemplos, no la lista; un pedido explícito de máxima garantía, de casi cero fallas o de orquestación multi-modelo también basta. Sin cupo por porcentaje (política §19–§20).
- Orquesta: Planner (`claudej`), Plan Reviewer (`codexj` no bloqueante), Builder y Closer (procesos one-shot `musej`), árbitro (`claudej`), y pool de revisores con quorum por familias en 2 rondas (slots vigentes en `config_loader.py`).

---

## 6. Horizon — Contexto Personal y Estratégico del CEO

Horizon **no es un agente separado** ni tiene runtime propio. Es la capacidad y contexto personal/estratégico que consume el CEO: misión, objetivos, hitos, capacidad, ideas capturadas y siguiente acción (`mission → objective → milestone → action → result`).
- **Especificación canónica**: [`jardal-horizon/README.md`](file:///home/develop/development/Jardal/jardal-openceo/jardal-horizon/README.md) v2.1.
- **Implementación en Core**: [`openceo-core/horizon/`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/).
- **Tracking operacional**: los tickets viven en ERPNext (`https://erp.jardal.app/app/task`); `STATUS.md` guarda el avance y los traspasos de sesión. `PENDINGS.md` es solo un puntero al backlog.

### Horizon como cockpit del dueño (autonomía)
- **Cockpit**: «qué sigue» y el brief diario combinan el plan personal, el status de lo que avanzó el CEO y las decisiones pendientes. Es la interfaz principal del dueño (TASK-2026-00120: `horizon/cockpit.py`, bandeja con la fuente de Requiere humano, respuesta por voz/móvil/API, entrega única, privacidad un solo sentido).
- **Promoción automática**: si una acción toca un proyecto o repo de la empresa, es trabajo de empresa y entra sola al Objetivo o ticket por la API común (ERPNext primero); solo viaja la especificación del trabajo. Pregunta solo si implica costo, gasto o replanificación (TASK-2026-00121: `horizon/promotion.py` + `HorizonService.promote_action`, toque explícito por acción, `POST /api/horizon/promote`).
- **Privacidad en un solo sentido**: lo de empresa entra a Horizon en solo lectura; lo personal (misión, finanzas, familia, salud) nunca sale. Sin interruptor general de exportación.
- **Terminología**: los de Horizon son siempre «objetivos personales»; «Objetivo» a secas es objetivo de empresa (contrato: §Proactive CEO de `docs/ARCHITECTURE_REFERENCE_2026.md`; decisión del vault `decisions/2026-10-06-objetivo-unico-ceo-autonomo`).
- Las tareas personales de Horizon no son tickets de empresa (excepción acotada en §10); la capacidad del dueño frena solo los escalados, no el trabajo autónomo; el plan del día sigue sin depender de un LLM.

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
5. **`musej` es el `muse` de dev por defecto; `muse` pelado es la sesión aislada**:
   - `/home/develop/.local/bin/musej` envuelve a `muse` añadiendo al final `--yolo --disable-sandbox --disable-approval --approval-mode never --trust-workspace` (acceso total, como `codexj`/`claudej`). Úsalo en builder, closer, tier C, sesiones dev sueltas y monitor de cuota.
   - Para una sesión **aislada** (revisor read-only, auditoría sin contexto extra del workspace) invoca `muse` pelado directamente: sandbox + aprobaciones ON, sin `--trust-workspace`. Los flags del wrapper **no se pueden reescribir encima** (van los últimos y no existe `--no-yolo).

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

1. **`completed` es condicional, no automático a un solo estado**: con revisión humana pendiente (`qa_review_required` sin aprobar) empuja `Pending Review`; sin ella, `Completed` directo. `failed`/`escalated`/`interrupted` no se empujan.
2. **`status_pushed` guarda el vocabulario de OpenCEO, no el de ERPNext** — comparar `in_progress`
   contra `Working` rompería la idempotencia. Y `str(TaskStatus.QUEUED)` da `'TaskStatus.QUEUED'`:
   usar `.value`.
3. **Nunca un `create_document(doctype, payload)` genérico** hacia ERPNext: solo operaciones
   semánticas cerradas, y el cliente tiene allowlist de DocTypes.
4. **`openceo_ref` es la clave de idempotencia** (nullable, única). Un reintento usa el mismo valor.
5. **La consola de `bench` decodifica stdin como latin-1**: usar escapes `é` en scripts ASCII.
6. **`Working` solo con ejecución real**: `Open` = registrado/backlog, sin ejecución. El scheduler solo lanza tickets con `autorun` explícito `True` o legacy sin campo (equivale a `True`); registrar con `autorun=false` persiste el campo y nunca ejecuta ni toca `Working` (TASK-2026-00043: `TaskDocument` no tenía el campo, el `False` se perdía y el ticket aparecía `Working` sin nadie trabajando).
7. **Visibilidad derivada por evento, nunca a mano**: el % de un grupo, sus fechas y las estimaciones de los abiertos los recalcula su worker propio (`visibility.py` + `_refresh_visibility`); las fechas reales viajan en el mismo PUT del estado. No editar `progress`/`exp_*` a mano para "arreglar" el Gantt: el siguiente evento lo recalcula (TASK-2026-00146 + 00147).

---

## 9. Cierre de sesión (regla del dueño, 2026-09-28)

Una sesión de trabajo **no termina con el código listo**: termina cuando el ticket queda
cerrado en su ciclo completo. No hay sesiones dedicadas solo a mergear; el merge es parte
del mismo trabajo. Al cerrar, en orden:

1. **Merge a `main` + push** de la rama del ticket (`git checkout main && git merge --no-ff <rama> && git push origin main`).
2. **Servicio reiniciado con el código nuevo** (`scripts/openceo-core-host.sh restart` + `status` + smoke del endpoint).
3. **Ticket en ERPNext en su estado final** con `openceo_transition_ticket` (criterio global en el servidor; relee, verifica y deja el espejo coherente sin eco): `Completed` con `evidence` de la verificación si el agente terminó y verificó; `Pending Review` con `human_action` nombrable solo si queda una aprobación o decisión que únicamente el dueño puede tomar; `Working` con `reason` si sigue abierto el trabajo o una dependencia. Cancelar trabajo del dueño exige citar su orden (`owner_instruction`); sin ella la herramienta escala en vez de cancelar. Borrar la rama local (`-d`) y remota si existe una vez mergeada.
4. **Docs temporales eliminados**: `docs/work/TASK-2026-000NN-tema.md` se borra al pasar el checklist (resultado en el ticket + vault actualizado o innecesario + original adjunto como privado si tiene valor futuro); informar en una línea.
5. **Registro durable**: `STATUS.md` (traspaso), ticket ERPNext (resultado) y vault (solo hechos, decisiones, flujos y conclusiones reutilizables, con el número de ticket como evidencia).

---

## 10. Contrato operativo raíz (Tier S F7a)

Repo-root instructions. Every CLI in the Tier S roster reads this file
directly; prompts never paste it. This section is also served as
`AGENTS.md` (symlink to this file) so every agent loads the same rules.

### Layout

- `openceo-core/` — orchestrator (Python, FastAPI, LangGraph), tests in `tests/`
- `openceo-core/agents/dev/tier_s/` — Tier S engine (F1–F7): single-ticket
  cycle, plan, gates, models, stores, telemetry
- `openceo-core/orchestration/` — shared orchestration both engines import:
  one-shot executors, review pool/rounds, findings, inbox, knowledge
- `openceo-core/objectives/` — company objective engine (execution state, planner models)
- `openceo-core/company_mandate.py` — company mandate + owner escalation policy
- `openceo-core/ops/` — deterministic Tier-0 ops tools (e.g. create_project)
- `openceo-react/` — dashboard (React 19 + Vite + Tailwind v4)
- `openceo-voice/`, `openceo-voice-android/` — realtime voice + Android app
- `workflows/` — deterministic Tier-0 YAML workflows
- `projects/<project>/project.yaml` — per-project config (validated)
- `docs/work/TASK-2026-000NN-tema.md` — temporary ticket research (delete after)

### Python

- Always use the root venv: `./.venv/bin/python -m pytest …`
- Never create venvs in `/tmp`; never force-install into system python
- Compile-check touched files: `python3 -m py_compile <files>`

### Validate before finishing

```bash
./.venv/bin/python -m pytest tests/test_tier_s_final.py tests/test_tier_s_contract.py \
  tests/test_coding_tool_routing.py tests/test_tier_s_config_validation.py -q
```

Full suite (~8 min): `./.venv/bin/python -m pytest tests/ -q`

### Service (host mode)

```bash
scripts/openceo-core-host.sh restart|status
```

### Tier S executor contract

**Scope: this contract applies ONLY when your prompt names you a Tier S executor (builder/reviewer). Owner sessions follow §9 instead; executors never follow §9.**

- Work on the checked-out branch; NEVER commit, push, rebase, stash, or
  change branches. Only the orchestrator commits (squash) and merges.
- Orchestrator files live in `.tier-s/` (PLAN, FINDINGS, FIX, HANDOFF,
  status; objectives engine: `objectives/<id>/`, `PLANNER_RESULT.json`,
  `OBJECTIVE_CLOSURE.md`). `.tier-s/` is the orchestrator-wide scratch
  (TASK-2026-00136). Read them; write only the artifact your prompt names.
- Gates are run by the orchestrator; report failures with file + line.
- Reviewers: read-only worktrees, reports on stdout in §16.4 format.
- One ticket = one session; no transcripts or diffs in prompts.

### Standing rules

- No polling or Mongo/API sweeps: react to CRUD events only.
- ERPNext owns business state (`Task`/`Project`); Mongo owns execution.
  Never create Mongo-only tickets: internal creation goes through the
  ticket API (ERPNext first). Bounded exception: Horizon personal tasks
  are not company tickets and stay Mongo-only (no business mirror);
  any work on a company project/repo goes to ERPNext first.
- No secrets, credentials, or personal data in notes or temp docs.
- Conventions over configuration: mirror sibling code, reuse its helpers.
- Owner rule (2026-10-06): no token-spending live tests (Tier S pilots,
  live planner/CEO/Muse runs, real ticket launches as tests) until epic
  TASK-2026-00110 F0–F6 are complete. Validate with unit/contract tests
  and simulated executors (§11).

---

## 11. Continuar el roadmap: palabra clave `sigue` (sesiones del dueño)

Solo sesiones del dueño; los ejecutores Tier S lo ignoran (§10).

Cuando el dueño escriba **`sigue`** (o `sigue TASK-2026-000NN` para fijar el ticket), no preguntes: aplica esta receta. El roadmap vigente es el épico **TASK-2026-00110** (OpenCEO autónomo), incluidos sus ajustes pendientes. Descubre el trabajo actual en ERPNext; consulta el comentario «ROADMAP POR FASES» y sus ampliaciones, sin mantener aquí una lista fija de tickets. Una observación registrada es una hipótesis que se debe contrastar, no una orden de modificar código sin diagnóstico.

1. **Situarse, sin barridos.**
   - `openceo_get_ticket TASK-2026-00110`, con sus comentarios actuales y dependencias.
   - `openceo_search_tickets` con `project=jardal-openceo`, por separado para `status=Open` y `status=Working`. Identificar los pendientes de esta épica por `parent_task`, sus dependencias y referencias explícitas del roadmap, aunque no tengan el prefijo `[Autonomía]`. Leer otros tickets solo si hacen falta para comprobar una dependencia o un posible duplicado.
   - El primer párrafo de «Continuar aquí» en `STATUS.md`.
   - `git status`, actualizar las referencias remotas y revisar el log de `main` y `origin/main`, además de los diffs dirigidos que afecten al candidato. Si otra sesión tiene trabajo activo en el checkout, usar un worktree aislado; no cambiarle la rama ni incorporar sus cambios al propio ticket.
2. **Elegir un ticket.** El indicado por el dueño. Si no hay, continuar el trabajo propio a medias; después elegir un pendiente de la épica con dependencias verificadas como `Completed`, según impacto, prioridad y desbloqueo del roadmap vigente. Un ticket `Working` de otra sesión no se toma ni se duplica: comprobar qué cubre y elegir otro independiente. No deducir que la épica está terminada de un traspaso antiguo o de que las fases originales estén cerradas. Si ningún pendiente puede avanzar, explicar el bloqueo concreto y parar.
3. **Contexto mínimo.**
   - Descripción y comentarios del ticket.
   - El workfile del épico y el del ticket, si existen.
   - La decisión del vault `decisions/2026-10-06-objetivo-unico-ceo-autonomo`, solo si hace falta el porqué.
   - El contrato normativo: `docs/ARCHITECTURE_REFERENCE_2026.md` §Proactive CEO (+ `openceo-core/objectives/routing_policy.md` y el ADR del planner).
   - Código: búsqueda dirigida + LSP.
4. **Contrastar la observación antes de corregir.** Aplicar criterio técnico riguroso a cada afirmación del ticket: comprobar el contrato vigente, seguir el flujo completo de productores y consumidores y buscar evidencia que confirme o refute el problema. Las líneas citadas, el diagnóstico y el estado del ticket pueden haberse quedado atrás respecto al código.
   - Buscar duplicados en este proyecto por causa y comportamiento, no solo por título. Revisar comentarios, commits, merges y diffs pertinentes de otras sesiones. No suponer que un fallo sigue presente ni que ya fue corregido: demostrar qué cubre el código actual y qué falta.
   - Cuando sea posible, reproducir con una prueba determinista o un ejecutor simulado, sin modelos en vivo. Si la evidencia es estática, dejar la ruta concreta que provoca el fallo y sus condiciones; no afirmar que se reprodujo. No reproducir un efecto externo irreversible. No poder reproducirlo todavía no demuestra que la observación sea falsa.
   - Decidir y registrar en el ticket: fallo real vigente, corrección parcial, ya resuelto y verificado, duplicado, observación incorrecta o diagnóstico inconcluso. Incluir evidencia y valorar si el ajuste propuesto es prudente, respeta el rumbo y resuelve la causa sin introducir regresiones ni complejidad innecesaria.
   - Fallo real vigente o parcial: implementar solo el delta necesario. Ya resuelto: verificar la aceptación y cerrar con evidencia, sin atribuirse una corrección ajena. Observación incorrecta: explicar y demostrar por qué, resolver el ticket con el criterio global y no forzar un cambio para justificar su existencia. Duplicado: enlazar el ticket canónico y evitar una segunda implementación; si el trabajo canónico sigue pendiente, mantener `Working` con la dependencia y razón exactas. Diagnóstico inconcluso: seguir investigando o mantener `Working` con el impedimento concreto; no cerrar ni pedir revisión humana por falta de certeza. Cancelar trabajo fijado por el dueño sigue exigiendo su orden según el criterio global.
5. **Implementar** en una rama `feat/<ticket>-<tema>` solo si el diagnóstico justifica una corrección. El tier marca el rigor: en `tier_s`, plan escrito en el workfile antes de codificar y una revisión independiente de solo lectura antes del merge. Coordinar cualquier solapamiento comprobado con otra sesión sin sobrescribir su trabajo.
6. **Probar solo lo necesario**: `py_compile` de lo tocado, los tests del área y la suite de §10, respetando la consigna de abajo. Las regresiones deben comprobar el fallo y la conducta esperada, no convalidar automáticamente el comportamiento existente o el diagnóstico del ticket.
7. **Cerrar según §9.**
   - Antes del merge o de cerrar sin cambios, actualizar las referencias remotas y volver a contrastar el diagnóstico y la aceptación con `main`, el ticket y los cambios pertinentes de otras sesiones. Si otra corrección ya cubre el fallo, descartar o ajustar el delta redundante y volver a verificar; no mergear por inercia.
   - Merge `--no-ff` a `main` + push.
   - Restart + status + smoke si cambió el servicio.
   - Ticket en su estado final con `openceo_transition_ticket` (`reason` siempre, `evidence` para cerrar, `human_action` para revisión), con un comentario de resultado.
   - Rama borrada.
   - Un párrafo corto al inicio de «Continuar aquí»: qué se cerró y cuál es el siguiente ticket.
   - Vault solo si hay una decisión o un hecho durable nuevo.
   - Cuando el resultado sea verificado sin cambios de código, registrar ese resultado y su evidencia; no fabricar una rama, merge o restart para aparentar una corrección.
8. **Seguir.** Si queda margen, refrescar los pendientes en ERPNext y volver al paso 2. Al terminar, una línea: qué se resolvió, si hubo fix o verificación sin cambios, y qué sigue según el estado actual.

Al dueño solo se le pregunta por la política de escalado del épico: gasto nuevo, acción irreversible, replanificación estratégica, cambio de mandato o revisión humana exigida por política. Lo demás se decide y se hace.

**Consigna del dueño (2026-10-06): ninguna prueba que gaste tokens hasta completar la arquitectura.** Mientras F0–F6 del épico no estén `Completed`, queda prohibido:

- correr pilotos o ciclos Tier S reales;
- invocar en vivo el planner, el CEO o Muse;
- lanzar tickets reales como prueba;
- usar `chat.sh` contra modelos.

Se valida con tests unitarios y de contrato, ejecutores o modelos simulados y `py_compile`. La única prueba real completa es **TASK-2026-00122** (F7), al final.

**Palabra clave `audita` (sesiones del dueño).** Cuando el dueño escriba **`audita`**, no preguntes: audita que `main` cumpla el rumbo del épico TASK-2026-00110. Fuentes de verdad, en este orden: la decisión del vault `decisions/2026-10-06-objetivo-unico-ceo-autonomo`, el contrato `docs/ARCHITECTURE_REFERENCE_2026.md` §Proactive CEO (más `openceo-core/objectives/routing_policy.md` y su ADR `docs/decision-2026-10-05-planner-objetivos-muse.md`) y el comentario ROADMAP del épico. Buscar fugas: código en carpetas que no le corresponden, referencias a archivos inexistentes, docs que afirman lo que el código no hace, reglas que contradicen el contrato o las reglas globales del dueño, y tickets viejos que chocan con el rumbo. No arreglar nada: buscar duplicados y crear tickets hijos del épico (batch con `parent_task`) con evidencia `archivo:línea`, y actualizar el comentario ROADMAP. Sin pruebas que gasten tokens; la suite unitaria sí vale.
