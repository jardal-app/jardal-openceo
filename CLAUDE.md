# CLAUDE.md - OpenCEO Developer & Agent Guide

Guía y contrato de arquitectura para agentes y desarrolladores que interactúan con OpenCEO.

**Vault de este proyecto**: `projects/jardal/jardal-openceo` dentro de
`/home/develop/development/Jardal/jardal-knowledge/vault/` (usar el skill `obsidian-vault-links`).
Trabajo temporal de tickets: `docs/work/TASK-2026-000NN-tema.md`.

Instrucciones operativas del repo (F7a): `AGENTS.md` — los CLI del roster lo cargan directamente; las sesiones del Owner leen este archivo, que lo importa en §10.

Continuar el roadmap vigente (épico **TASK-2026-00245**: departamentos y empleados, CLIs intercambiables y continuidad tras reinicios): el Owner escribe **`sigue`** y la sesión aplica la receta de §11. TASK-2026-00187 está cerrado; sus pilotos reales siguen separados en **TASK-2026-00233**, solo con **`valida`** (§11), y no son una condición de cierre del épico nuevo.

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
- Pendiente del Owner (2026-10-05): launcher canónico — script vs unit systemd `openceo-core-host.service` (parada el 2026-10-05 tras 167 reinicios en churn; NO habilitar ambas a la vez).

### Compilar APK Android
```bash
cd openceo-voice-android
./gradlew assembleDebug
# El APK generado queda en:
# app/build/outputs/apk/debug/app-debug.apk
```

---

## 5. Motor de Workflows, Autodiscovery y Dev Tier S

Contrato vigente: épico TASK-2026-00187, sección «Agentes por rol» de `docs/ARCHITECTURE_REFERENCE_2026.md` y decisión `decisions/2026-10-11-agentes-por-rol-planner-unico` (vocabulario: Owner, el personal, pedido, no_aplica…). Lo que sigue describe la implementación actual; cada ticket del épico actualiza su parte.

### Workflows Deterministas (Tier 0 - 0 Tokens)
- Almacenados en `workflows/<agente>/<nombre>.yaml`.
- Ejecución directa secuencial sin gasto de LLM.
- Se invocan registrando tickets con `params: {"workflow": "<id_workflow>"}` o con `params: {"use_workflow": true}` si el YAML define `auto_match: true` y triggers.

### Autodiscovery de Workflows (retirado en TASK-2026-00215)
- El flujo `discover_workflow` + DeepSeek Flash + `discovery_runs` + aprobación ya no existe en el código; los flags legados no deciden nada (`objectives/pedido.py`).
- El aprendizaje ahora cristaliza en skills (sección «Agentes por rol» de `docs/ARCHITECTURE_REFERENCE_2026.md`).

### Human-in-the-Loop (HITL) en YAMLs
- Los pasos con `type: human_in_the_loop` y `permission: <fase>` pausan la ejecución (`WorkflowRunStatus.BLOCKED_HUMAN`).
- El ticket se marca en `status: waiting` con `phase: <permission>` (ej. `qa_review`).
- Al aprobar la acción desde la UI/API (`create_phase_action`), el motor reanuda el workflow automáticamente ejecutando los pasos restantes sin reiniciar.

### Planner único (global)
- El único que decide todo el trabajo: para un Objetivo, qué tickets deben existir; para un pedido (ticket suelto sin decisión explícita), si es ticket u objetivo, el agente, la skill sugerida y, para dev, el tier. Muse Code headless, un run fresco por ciclo, salida `PlannerResult` validada (Pydantic + validación determinista). Las peticiones explícitas solo se validan contra el catálogo.
- Normativo único: `openceo-core/objectives/routing_policy.md` (núcleo; el detalle por agente vive en `openceo-core/objectives/routing_policy.d/`). El porqué vive en el vault (`decisions/2026-10-06-objetivo-unico-ceo-autonomo`).
- Sustituye a `claudej`/`TICKETS.yaml` (TASK-2026-00112; motor en `openceo-core/objectives/`, TASK-2026-00117).
- El plan Claude/Codex de un ticket Tier S planifica cómo implementar ese ticket, no es un segundo planner; tampoco lo es el orden determinista del día en Horizon.

### Dev Tier S (Consenso Crítico Multi-IA)
- Se activa enviando `"coding_tool": "tier_s"` en la raíz del ticket o dentro de `params` (routing per-task; `dev.coding_tool` en `project.yaml` está obsoleto), o cuando el planner único lo decide para un pedido.
- **Criterio de selección (Owner, 2026-10-09)**: S se elige cuando orquestar varios modelos independientes dará un resultado materialmente más correcto, en **cualquier dominio**. Dinero, pagos y auth son ejemplos, no la lista; un pedido explícito de máxima garantía, de casi cero fallas o de orquestación multi-modelo también basta. Sin cupo por porcentaje (política §19–§20).
- Orquesta: Planner (`claudej`), Plan Reviewer (`codexj` no bloqueante), Builder y Closer (procesos one-shot `musej`), árbitro (`claudej`), y pool de revisores con quorum por familias en 2 rondas (slots vigentes en `config_loader.py`).

---

## 6. Horizon — Contexto Personal y Estratégico del CEO

Horizon **no es un agente separado** ni tiene runtime propio. Es la capacidad y contexto personal/estratégico que consume el CEO: misión, objetivos, hitos, capacidad, ideas capturadas y siguiente acción (`mission → objective → milestone → action → result`).
- **Especificación canónica**: [`jardal-horizon/README.md`](file:///home/develop/development/Jardal/jardal-openceo/jardal-horizon/README.md) v2.1.
- **Implementación en Core**: [`openceo-core/horizon/`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/).
- **Tracking operacional**: los tickets viven en ERPNext (`https://erp.jardal.app/app/task`); `STATUS.md` guarda el avance y los traspasos de sesión. `PENDINGS.md` es solo un puntero al backlog.

### Horizon como cockpit del Owner (autonomía)
- **Cockpit**: «qué sigue» y el brief diario combinan el plan personal, el status de lo que avanzó el CEO y las decisiones pendientes. Es la interfaz principal del Owner (TASK-2026-00120: `horizon/cockpit.py`, bandeja con la fuente de Requiere humano, respuesta por voz/móvil/API, entrega única, privacidad un solo sentido).
- **Promoción automática**: si una acción toca un proyecto o repo de la empresa, es trabajo de empresa y entra sola al Objetivo o ticket por la API común (ERPNext primero); solo viaja la especificación del trabajo. Pregunta solo si implica costo, gasto o replanificación (TASK-2026-00121: `horizon/promotion.py` + `HorizonService.promote_action`, toque explícito por acción, `POST /api/horizon/promote`).
- **Privacidad en un solo sentido**: lo de empresa entra a Horizon en solo lectura; lo personal (misión, finanzas, familia, salud) nunca sale. Sin interruptor general de exportación.
- **Terminología**: los de Horizon son siempre «objetivos personales»; «Objetivo» a secas es objetivo de empresa (contrato: §Proactive CEO de `docs/ARCHITECTURE_REFERENCE_2026.md`; decisión del vault `decisions/2026-10-06-objetivo-unico-ceo-autonomo`).
- Las tareas personales de Horizon no son tickets de empresa (excepción acotada en §10); la capacidad del Owner frena solo los escalados, no el trabajo autónomo; el plan del día sigue sin depender de un LLM.

### Fases de Implementación (Roadmap)
1. **HZ-001 — Contexto personal del CEO** (Completada): Modelos de jerarquía (`models.py`), mandato (`mandate.py`), privacidad estricta (`privacy.py`), clasificador determinista antes de LLM (`classify.py`), colecciones Mongo `horizon_*` e importador idempotente (`importer.py`).
2. **HZ-002 — Ciclo proactivo del CEO** (Completada): Capacidad diaria determinista al 70% (`capacity.py`), planificador lexicográfico (`planner.py`), disparadores intencionales persistidos sin barridos (`triggers.py`), revisión periódica (`review.py`), workflows `ceo/horizon_daily.yaml` y `ceo/horizon_review.yaml`.
3. **HZ-003 — Delegación y dev móvil** (Implementada, pendiente piloto con 3 tickets reales): Encargo de tickets de alcance personal sobre sesiones dev existentes en background, medición de tiempo humano de supervisión y retrabajo, y entrega única del resultado ("delivered once") tolerante a desconexión del móvil.
4. **HZ-004 — Fuentes conectadas y espacio personal** (vista implementada; fuente conectada pendiente de necesidad observada): vista del espacio personal en `openceo-react` — ruta `/dashboard/personal`, permiso `personal_space` — con misión, objetivos e hitos, plan del día (capacidad, bloques, exclusiones y decisiones), siguiente acción, delegación medida con entrega única, captura y métricas humanas. La integración de fuentes pasivas queda condicionada a que el uso demuestre el ahorro (§9.8 y §11: *"conectar fuente solo si aporta ahorro"*).
5. **HZ-005 — Frontera financiera con ERPNext**: Consumo de saldos y asientos vía adapter de Finance hacia ERPNext (TICKET-025, decisión del vault `decisions/2026-09-19-erpnext-financial-record`), sin almacenar dinero en floats ni crear contabilidad propia en OpenCEO.

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

## 7. Reglas Críticas de Eficiencia y Escalabilidad

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

## 9. Cierre de sesión (regla del Owner, 2026-09-28)

Una sesión de trabajo **no termina con el código listo**: termina cuando el ticket queda
cerrado en su ciclo completo. No hay sesiones dedicadas solo a mergear; el merge es parte
del mismo trabajo. Al cerrar, en orden:

1. **Merge a `main` + push** de la rama del ticket (`git checkout main && git merge --no-ff <rama> && git push origin main`).
2. **Servicio reiniciado con el código nuevo** (`scripts/openceo-core-host.sh restart` + `status` + smoke del endpoint).
3. **Ticket en ERPNext en su estado final** con `openceo_transition_ticket` (criterio global en el servidor; relee, verifica y deja el espejo coherente sin eco): `Completed` con `evidence` de la verificación si el agente terminó y verificó; `Pending Review` con `human_action` nombrable solo si queda una aprobación o decisión que únicamente el Owner puede tomar; `Working` con `reason` si sigue abierto el trabajo o una dependencia. Cancelar trabajo del Owner exige citar su orden (`owner_instruction`); sin ella la herramienta escala en vez de cancelar. Borrar la rama local (`-d`) y remota si existe una vez mergeada.
4. **Docs temporales eliminados**: `docs/work/TASK-2026-000NN-tema.md` se borra al pasar el checklist (resultado en el ticket + vault actualizado o innecesario + original adjunto como privado si tiene valor futuro); informar en una línea.
5. **Registro durable**: `STATUS.md` (traspaso), ticket ERPNext (resultado) y vault (solo hechos, decisiones, flujos y conclusiones reutilizables, con el número de ticket como evidencia).

---

## 10. Contrato operativo raíz (Tier S F7a)

@AGENTS.md

Los CLI del roster leen `AGENTS.md` directamente. El resto de este archivo es el manual de las sesiones del Owner.

---

## 11. Continuar el roadmap: palabra clave `sigue` (sesiones del Owner)

Solo sesiones del Owner; los ejecutores Tier S lo ignoran (§10).

Cuando el Owner escriba **`sigue`** (también `SIGUE`), no preguntes: aplica esta receta al épico **TASK-2026-00245**. `sigue TASK-2026-000NN` fija un hijo; `sigue TASK-2026-00245` fija explícitamente el épico. Si el identificador es otro grupo, confirma su proyecto y estado antes de tomar sus hijos; un grupo ya `Completed` se confirma como terminado y no dispara trabajo de otro épico. TASK-2026-00187 y TASK-2026-00110 son la base cerrada, no el trabajo pendiente actual.

La descripción y el comentario **«ROADMAP DE AJUSTES FINALES»** de 00245 en ERPNext son la fuente del alcance y del orden. El Owner agrupó el trabajo en tres entregables amplios: departamentos y especialización de empleados; selección de CLI; continuidad tras reinicios y trabajo nocturno. No crear microtickets por CLI, flujo pequeño o prueba: diagnosticar y resolver lo que pertenezca al hijo existente. Una observación registrada es una hipótesis que se debe contrastar, no una orden de modificar código sin diagnóstico.

**Alcance de `sigue` (orden del Owner, 2026-10-14).** Codificar y verificar los hijos de 00245 con tests simples de lógica, contrato o comunicación y ejecutores simulados. No correr pruebas extensas ni end-to-end con modelos. TASK-2026-00233 sigue fuera de este ciclo; sus pilotos no bloquean el trabajo ni el cierre de 00245.
- Las próximas ejecuciones consumen esta receta; el registro de un ticket con `autorun=false` no lo lanza. No asumir que existe un runner en una ruta determinada: localizar la entrada canónica antes de cambiar su configuración. La recuperación automática del runtime sigue pendiente hasta verificar el hijo de continuidad; editar esta receta no la implementa.
- Si falta una credencial, cuota, autenticación o acción exclusiva del Owner, dejar el recurso, acción y condición de reanudación en el mismo ticket; mantenerlo `Working` y continuar otro hijo independiente. No trasladar esos bloqueos automáticamente a 00233 ni cerrar una aceptación parcial.
- `Pending Review` solo si todo lo verificable está hecho y falta una aprobación o decisión exclusivamente humana exigida. Una falta técnica o dependencia abierta queda `Working` con motivo. Elegir por cuenta propia las decisiones técnicas rutinarias y reversibles.

1. **Situarse, sin barridos.**
   - `openceo_get_ticket TASK-2026-00245`, con sus comentarios actuales y dependencias, y releer los hijos identificados allí. Confirmar `project=jardal-openceo` y `parent_task` antes de tomar uno. Incluir `Open`, `Overdue` y `Working` al valorar pendientes; no confundir `execution_status` con el estado de negocio.
   - Leer otros tickets solo para comprobar una dependencia o un posible duplicado. Si hace falta buscar, usar `openceo_search_tickets` con `project=jardal-openceo` y consulta dirigida; no listar el backlog entero ni añadir polling para elegir trabajo.
   - El primer párrafo de «Continuar aquí» en `STATUS.md`.
   - `git status`, actualizar las referencias remotas y revisar el log de `main` y `origin/main`, además de los diffs dirigidos que afecten al candidato. Si otra sesión tiene trabajo activo en el checkout, usar un worktree aislado; no cambiarle la rama ni incorporar sus cambios al propio ticket.
2. **Elegir un ticket.** El indicado por el Owner. Si no hay, continuar el trabajo propio a medias; después seguir el orden recomendado del roadmap: **TASK-2026-00247** (continuidad/reinicio), **TASK-2026-00244** (departamentos/empleados), **TASK-2026-00246** (CLIs, depende de 00244). Verificar las dependencias como `Completed`; el orden recomendado no crea una dependencia artificial entre 00247 y 00244. Un ticket `Working` de otra sesión no se toma ni se duplica: comprobar qué cubre y elegir otro independiente. No deducir que la épica está terminada de un traspaso antiguo.
   - Si TASK-2026-00245 ya está `Completed` en ERPNext, confirmar su cierre y terminar; no tomar tickets de 00233 ni reabrir 00187.
   - Si todos sus hijos están `Completed` o `Cancelled` y el épico sigue abierto, ir al paso 9.
   - Si ningún pendiente puede avanzar, explicar el bloqueo concreto y parar.
3. **Contexto mínimo.**
   - Descripción y comentarios del ticket.
   - El workfile del épico y el del ticket, si existen.
   - El rumbo y la restricción de pruebas del épico 00245. El porqué histórico, solo si hace falta: el diseño adjunto a 00187 o su decisión del vault (`decisions/2026-10-11-agentes-por-rol-planner-unico`). No cargar el vault para cambios ordinarios; si es necesario, aplicar el skill y el aislamiento del proyecto.
   - El contrato normativo: `docs/ARCHITECTURE_REFERENCE_2026.md` §Proactive CEO y la sección «Agentes por rol» cuando exista (+ `openceo-core/objectives/routing_policy.md` y `openceo-core/objectives/routing_policy.d/`, normativa del planner repartida por TASK-2026-00201).
   - Código: búsqueda dirigida + LSP.
4. **Contrastar la observación antes de corregir.** Aplicar criterio técnico riguroso a cada afirmación del ticket: comprobar el contrato vigente, seguir el flujo completo de productores y consumidores y buscar evidencia que confirme o refute el problema. Las líneas citadas, el diagnóstico y el estado del ticket pueden haberse quedado atrás respecto al código.
   - Buscar duplicados en este proyecto por causa y comportamiento, no solo por título. Revisar comentarios, commits, merges y diffs pertinentes de otras sesiones. No suponer que un fallo sigue presente ni que ya fue corregido: demostrar qué cubre el código actual y qué falta.
   - Cuando sea posible, reproducir con una prueba pequeña determinista o un ejecutor simulado, sin modelos en vivo ni flujos completos. Si la evidencia es estática, dejar la ruta concreta que provoca el fallo y sus condiciones; no afirmar que se reprodujo. No reproducir un efecto externo irreversible. No poder reproducirlo todavía no demuestra que la observación sea falsa.
   - Decidir y registrar en el ticket: fallo real vigente, corrección parcial, ya resuelto y verificado, duplicado, observación incorrecta o diagnóstico inconcluso. Incluir evidencia y valorar si el ajuste propuesto es prudente, respeta el rumbo y resuelve la causa sin introducir regresiones ni complejidad innecesaria.
   - Fallo real vigente o parcial: implementar solo el delta necesario. Ya resuelto: verificar la aceptación y cerrar con evidencia, sin atribuirse una corrección ajena. Observación incorrecta: explicar y demostrar por qué, resolver el ticket con el criterio global y no forzar un cambio para justificar su existencia. Duplicado: enlazar el ticket canónico y evitar una segunda implementación; si el trabajo canónico sigue pendiente, mantener `Working` con la dependencia y razón exactas. Diagnóstico inconcluso: seguir investigando o mantener `Working` con el impedimento concreto; no cerrar ni pedir revisión humana por falta de certeza. Cancelar trabajo fijado por el Owner sigue exigiendo su orden según el criterio global.
5. **Implementar** en una rama `feat/<ticket>-<tema>` solo si el diagnóstico justifica una corrección. El tier marca el rigor: en `tier_s`, plan escrito en el workfile antes de codificar y una revisión independiente de solo lectura antes del merge. Coordinar cualquier solapamiento comprobado con otra sesión sin sobrescribir su trabajo.
6. **Probar solo lo necesario**: `py_compile` de los Python tocados, tests simples y dirigidos del área y los checks obligatorios de §10. No exigir la suite completa ni ensayos integrados extensos como aceptación de este épico. Las regresiones deben comprobar el fallo y la conducta esperada, no convalidar automáticamente el comportamiento existente o el diagnóstico del ticket. Una vez verdes, ampliar o repetir solo si otro cambio o fallo lo justifica.
7. **Cerrar según §9.**
   - Antes del merge o de cerrar sin cambios, actualizar las referencias remotas y volver a contrastar el diagnóstico y la aceptación con `main`, el ticket y los cambios pertinentes de otras sesiones. Si otra corrección ya cubre el fallo, descartar o ajustar el delta redundante y volver a verificar; no mergear por inercia.
   - Merge `--no-ff` a `main` + push.
   - Antes de un restart, registrar en el ticket y en el checkpoint existente qué quedó publicado/verificado y qué acción falta. Restart canónico + status + smoke si cambió el servicio; los cambios solo documentales no requieren reiniciar. No generar una batería de reinicios como prueba.
   - Ticket en su estado final con `openceo_transition_ticket` (`reason` siempre, `evidence` para cerrar, `human_action` para revisión), con un comentario de resultado.
   - Rama borrada.
   - Un párrafo corto al inicio de «Continuar aquí»: resultado y siguiente ticket. Si solo se completó parte del alcance, nombrar lo pendiente y conservar el estado correcto, sin declarar cerrado el hijo entero.
   - Vault solo si hay una decisión o un hecho durable nuevo.
   - Cuando el resultado sea verificado sin cambios de código, registrar ese resultado y su evidencia; no fabricar una rama, merge o restart para aparentar una corrección.
8. **Seguir o retomar.** Si queda margen, releer el épico/hijos y volver al paso 2. Tras un reinicio o cambio de sesión, releer el ticket activo, checkpoints, artefactos y estado de git antes de actuar: continuar la etapa pendiente, sin duplicar ejecuciones, efectos confirmados o cierres. No repetir un `Completed`, revivir un cancelado ni saltar una espera humana/dependencia. Aplicar únicamente la recuperación que el runtime tenga implementada y verificada; si falta, registrar el impedimento y continuar trabajo independiente. Al terminar, una línea con resultado y siguiente acción.
9. **Cerrar el épico** cuando no le quede ningún hijo pendiente:
   - criterios de los tres hijos cumplidos, con evidencia de sus tests simples y los checks obligatorios de §10 en verde; no exigir modelos en vivo, pilotos de 00233 ni la suite completa;
   - `main` igual a `origin/main`; si hubo cambios de servicio, servicio actualizado con status y smoke;
   - `openceo_transition_ticket` de TASK-2026-00245 a `Completed`, con esa evidencia, y relectura que confirme el estado;
   - un párrafo al inicio de «Continuar aquí».

Al Owner solo se le pide lo que únicamente él puede aportar, según el mandato y las reglas de acceso: gasto nuevo, acción irreversible, replanificación estratégica, cambio de mandato, revisión humana exigida o credenciales/datos propios. Registrar en el ticket actual la acción exacta y continuar trabajo independiente; no inventar aprobaciones ni declarar completo lo bloqueado. Lo demás se decide y se hace.

**Consigna del Owner para TASK-2026-00245 (2026-10-14): solo tests simples de lógica, contrato o comunicación; sin tests complejos/extensos ni end-to-end con modelos.** Esta regla reemplaza para el ciclo actual el permiso anterior de gastar tokens en pruebas de componentes de 00187. Limita la validación, no las invocaciones de los CLI necesarias para implementar el trabajo autorizado.

Permitido:
- tests unitarios/de contrato y comunicación pequeños, con mocks, fixtures y ejecutores/CLI simulados;
- `muse --provider echo`, `help`/`version` u otra comprobación local no generativa, sin invocar modelos;
- checks obligatorios de §10, `py_compile` y un smoke mínimo del endpoint cuando se despliegue un cambio de servicio.

Fuera de la aceptación de 00245:
- pruebas de componente con modelos reales o validaciones que consuman tokens;
- objetivos, misiones o ciclos Tier S completos como prueba, ensayos integrados extensos y flujos extremadamente grandes, aunque sean simulados;
- crear microtickets de validación o exigir una corrida real para cerrar un entregable que se puede verificar con lógica/comunicación simulada.

Los pilotos históricos de TASK-2026-00233, **TASK-2026-00219** y **TASK-2026-00224**, conservan su receta aparte: solo con `valida` y sus dependencias `Completed`. Nunca con `sigue` ni por el ciclo nocturno de 00245; tampoco se presentan como verificación ya realizada.

**Palabra clave `valida` (sesiones del Owner, con el Owner disponible).** Cuando el Owner escriba **`valida`**, corre las pruebas reales de **TASK-2026-00233**. No preguntes, salvo lo que la propia prueba le pida al Owner.
1. **Situarse.** `openceo_get_ticket TASK-2026-00233` y sus hijos. Arrancar solo si TASK-2026-00187 está `Completed`, salvo una orden explícita del Owner para una prueba concreta.
2. **Elegir** el siguiente hijo con dependencias `Completed`: primero los `[Validación/Fix]` abiertos, luego 00219 (Piloto 1) y después 00224 (Aceptación). Una pregunta pendiente del Owner se le presenta en ese momento.
3. **Preflight sin tokens** (el comando que deja TASK-2026-00234). Si no está listo, corregir el entorno o registrar en el ticket lo que falta, y parar.
4. **Prueba real** según el runbook del ticket, vigilada en vivo (logs, ERPNext, cockpit). No intervenir, salvo en lo que el flujo le pida al Owner; registrar cada intervención.
5. **Informe** de métricas en el ticket (el comando de 00234) y conclusiones en el vault.
6. **Fallos.** Cada uno, un hijo `[Validación/Fix]` de 00233 con evidencia. Se corrige con el rigor de `sigue` (pasos 4 a 7) y la prueba se repite. Una prueba cierra `Completed` solo si su aceptación se cumplió de verdad; 00233 cierra cuando todos sus hijos están `Completed`.

**Palabra clave `audita` (sesiones del Owner).** Cuando el Owner escriba **`audita`**, no preguntes: audita que `main` cumpla el contrato de arquitectura del épico TASK-2026-00187. Fuentes de verdad, en este orden:
1. El RUMBO de la descripción del épico.
2. Su decisión del vault, `decisions/2026-10-11-agentes-por-rol-planner-unico` (`decisions/2026-10-06-objetivo-unico-ceo-autonomo` queda como base histórica).
3. El contrato `docs/ARCHITECTURE_REFERENCE_2026.md` (§Proactive CEO y «Agentes por rol»), más `openceo-core/objectives/routing_policy.md`.
4. El comentario ROADMAP del épico.

Buscar fugas: código en carpetas que no le corresponden, referencias a archivos inexistentes, docs que afirman lo que el código no hace, reglas que contradicen el contrato o las reglas globales del Owner, y tickets viejos que chocan con el rumbo. No arreglar nada: buscar duplicados y crear tickets hijos del épico (batch con `parent_task`) con evidencia `archivo:línea`, y actualizar el comentario ROADMAP. Sin pruebas que gasten tokens; la suite unitaria sí vale.
