# Tareas pendientes de implementación

---

## TICKET-001: MCP Internal — eliminar notion_create_ticket
- **Componente**: `openceo-core/mcp-internal`
- **Prioridad**: Alta
- **Descripción**: Eliminar `notion_create_ticket` del MCP Internal. Ahora solo se usa `create_ticket` centralizado con trigger reactivo interno en cola. Verificar que el flujo sigue funcionando después del cambio.
- **Criterio de aceptación**: MCP Internal funciona sin `notion_create_ticket`. El trigger reactivo crea el ticket en Notion automáticamente desde `create_ticket` central.

---

## TICKET-002: MCP External — migrar a openceo-core
- **Componente**: `apps-sdk-mcp-test` → `openceo-core`
- **Prioridad**: Alta
- **Descripción**: Eliminar `apps-sdk-mcp-test` y mudar el MCP External dentro de `openceo-core`. Incluye migrar el contenedor Docker y la URL que expone. Renombrar `execute_workflow` porque no siempre ejecuta workflows — también ejecuta tools primitivos. Usar naming `agent.tool_name` directamente o un nombre genérico como `execute_tool`.
- **Criterio de aceptación**: `apps-sdk-mcp-test` eliminado. MCP External vive dentro de `openceo-core`. Las funciones expuestas usan el naming convention `agent.tool_name`. Consumidores externos (ChatGPT, etc.) siguen funcionando.

---

## TICKET-003: Secretary — tool de notas rápidas
- **Componente**: `openceo-core/agents/secretary`
- **Prioridad**: Media
- **Descripción**: Agregar dos tools primitivos: `secretary.save_note` y `secretary.query_notes` para capturar ideas/notas rápidas por chat o voz. Almacenadas en MongoDB collection `notes`. Campos: `content`, `project` (opcional), `tags`, `user`, `created_at`. El `query_notes` permite al LLM formular filtros dinámicos con el schema en la descripción del tool.
- **Criterio de aceptación**: El usuario puede decir "guárdame esta idea para X proyecto" y la nota se persiste. Puede consultar "¿qué notas tengo del proyecto Y?" y recibir resultados filtrados.

---

## TICKET-004: Finance — administrador de fortuna
- **Componente**: `openceo-core/agents/finance`
- **Prioridad**: Media
- **Nota**: No va al README. Son capacidades avanzadas del agente de finanzas.
- **Descripción**: Finance se convierte en un administrador de fortuna completo. Incluye:
  - **Arqueo de cajas**: por cada SaaS con ingresos, verificar que los invoices/entradas coincidan con el dinero real en BTCPay Server, nodo Lightning y BlueWallet.
  - **Detección de fugas**: si los montos no coinciden, alertar. Si el usuario sacó dinero manualmente, debe registrar el movimiento para no marcarlo como fuga.
  - **Tipos de movimiento Lightning**:
    - Automático: nodo al 90% de capacidad → withdrawal automático → registrar como movimiento
    - Semiautomático: usuario da la orden → se conecta a API de Binance → genera invoice → nodo Lightning de BTCPay lo paga → registrar
    - Manual: usuario mueve desde el nodo directamente → debe registrar manualmente
  - **Centralización en Binance**: la mayoría de movimientos terminan en Binance como punto central.
  - **Diferenciación de ingresos**: separar ingresos brutos (sin comisiones) para calcular el 10% del patrocinador. El usuario cubre comisiones, fees y gastos de servidor por separado.
- **Criterio de aceptación**: Finance puede: arquear cajas por SaaS, detectar discrepancias entre invoices y saldos reales, registrar movimientos manuales/semi/automáticos, calcular ingresos brutos vs netos con separación para patrocinador.

---

## TICKET-005: TOOL_REGISTRY + check_registry
- **Componente**: `openceo-core`
- **Prioridad**: Alta (prerrequisito para tiers 1-2)
- **Descripción**: Implementar el diccionario global `TOOL_REGISTRY` que mapea `agent.tool_name` → función Python. Implementar el tool `check_registry(agent=None)` para que el CEO consulte el catálogo. Sin agente: resumen compacto de todos los agentes y sus workflows. Con agente: detalle completo. Registrar todos los tools existentes de cada agente bajo la naming convention `{agent}.{tool_name}`.
- **Criterio de aceptación**: `TOOL_REGISTRY` contiene todos los tools actuales. `check_registry()` devuelve resumen global. `check_registry("finance")` devuelve detalle de finance. El naming convention es consistente.

---

## TICKET-006: YAML Workflow Executor (determinista)
- **Componente**: `openceo-core`
- **Prioridad**: Alta (prerrequisito para crystallization)
- **Descripción**: Implementar el executor Python que parsea YAML cristalizados y ejecuta los steps secuencialmente. Resolver variables `{{stepN.field}}` con outputs reales. Soportar `type: sequence` (lineal). Sin LLM — puro Python determinista. Los tools se resuelven contra `TOOL_REGISTRY`.
- **Criterio de aceptación**: Un YAML con 3 steps que usan tools del registry se ejecuta correctamente. Las variables entre steps se resuelven. Cero tokens consumidos. El executor reporta éxito/fallo por step.

---

## TICKET-007: YAML Schema — loops y composición
- **Componente**: `openceo-core`
- **Prioridad**: Media (extensión del executor)
- **Dependencia**: TICKET-006
- **Descripción**: Extender el executor YAML para soportar: (1) `type: loop` con `max_iterations`, condiciones `python_eval` y `llm_eval`, y `state` acumulativo entre iteraciones. (2) `when: "{{expr}}"` para steps condicionales evaluados en Python puro. (3) `ref:` para composición de workflows (Tier 1.5) — el executor carga y ejecuta el sub-workflow referenciado. Las aprobaciones humanas no viven en YAML; se modelan como fases de dashboard con `status=waiting`.
- **Criterio de aceptación**: Un workflow con loop + llm_eval itera hasta que la condición es True. Workflows con `ref:` ejecutan los sub-workflows correctamente. Las condiciones `when` se evalúan en Python puro. Un YAML con step de aprobación humana es rechazado.

---

## TICKET-008: Workflow Runs — MongoDB tracking
- **Componente**: `openceo-core`
- **Prioridad**: Alta
- **Dependencia**: TICKET-006
- **Descripción**: Crear colección `workflow_runs` en MongoDB. Cada ejecución de un workflow cristalizado genera un run con: `run_id`, `workflow`, `status` (PENDING/BLOCKED_TOOL/COMPLETED/FAILED/CANCELED), `steps_completed`, `current_step`, `blocked_at`, `created_at`, `updated_at`. Implementar el tool `query_workflow_runs(filters)` para consultas por LLM. Los runs sobreviven reinicios del servicio.
- **Criterio de aceptación**: Cada ejecución de workflow crea un run en MongoDB. El status se actualiza en cada step. `query_workflow_runs` filtra correctamente. Los runs son consultables desde chat.

---

## TICKET-009: Discovery Engine (Tier 2)
- **Componente**: `openceo-core`
- **Prioridad**: Media
- **Dependencia**: TICKET-005
- **Descripción**: Implementar discovery de nivel 1 (CEO) y nivel 2 (agente interno) usando `create_react_agent`. Nivel 1: CEO con tools `[call_pm, call_finance, call_secretary, call_dev]` descubre qué agentes usar y en qué orden. Nivel 2: agente interno con tools primitivos de su dominio descubre cómo combinarlos. Al completar el discovery, generar YAML candidato para cristalización. El flujo de aprobación humana antes de cristalizar.
- **Criterio de aceptación**: El CEO puede descubrir un flujo nuevo usando ReAct. Un agente puede descubrir cómo combinar sus primitivos. El YAML generado se presenta al humano para aprobación. Aprobado → se guarda como YAML cristalizado.

---

## TICKET-010: Blocked Discovery — estado persistente
- **Componente**: `openceo-core`
- **Prioridad**: Media
- **Dependencia**: TICKET-009
- **Descripción**: Cuando el discovery detecta un tool faltante: (1) guardar estado en MongoDB colección `discovery_runs` con `status=BLOCKED_TOOL`, `blocked_at`, contexto completo. (2) Notificar al admin con especificación del tool necesario. (3) Si admin aprueba → crear ticket al Dev agent. (4) Cuando el tool esté disponible → continuar discovery desde `blocked_at`. (5) Si admin rechaza → `CANCELED_ADMIN`, cerrar definitivamente.
- **Criterio de aceptación**: Un discovery que necesita un tool faltante se bloquea y persiste su estado. Admin puede aprobar/rechazar. Aprobación genera ticket. Tool registrado → discovery continúa desde donde se bloqueó.

---

## TICKET-011: Peer-to-Peer Communication (team_message + team_claim)
- **Componente**: `openceo-core`
- **Prioridad**: Media
- **Dependencia**: TICKET-009
- **Descripción**: Implementar comunicación peer-to-peer entre agentes para Tier 2 discovery.

### Contexto arquitectónico

Este ticket implementa el transporte peer-to-peer, NO el discovery engine (eso es TICKET-009). La distinción clave:
- **TICKET-009** resuelve: cómo un agente descubre qué hacer y genera un YAML candidato para cristalización.
- **TICKET-011** resuelve: cómo los agentes se comunican, se despiertan, y reclaman subtasks entre sí durante ese discovery.

**Importante**: en Tier 2, un agente publica una **subtask/capability request** (e.g. "necesito generar un reporte con estos datos financieros"), NO un workflow. El workflow cristalizado es el *resultado* del discovery (TICKET-009), no la entrada al claim.

### Modelo de activación (push, no polling)

Los agentes NO hacen polling de una cola. El mecanismo es push via session injection:

1. **El CEO abre un discovery group** con agentes participantes explícitos (top-down, README línea 638). El CEO ya sabe quiénes participan.
2. **Un agente publica una subtask/capability request** al pool compartido (JSONL append-only, O(1) write).
3. **El sistema hace session injection solo a los candidatos dentro de ese discovery group** — esto es el "wake". No se notifica a agentes fuera del grupo.
4. **Cada candidato evalúa** la subtask contra sus propios tools y responde `claim` o `cannot_claim`.
5. **El claim es atómico**: temp file → fsync → rename → fsync(dir). El primero gana.

### Edge cases

| Escenario | Comportamiento |
|---|---|
| Nadie reclama dentro del timeout | Escala a humano (HITL) o marca BLOCKED_TOOL |
| Dos agentes intentan claim | Atomic rename, primero gana, segundo recibe cannot_claim |
| Agente reclama pero falla al ejecutar | Libera (release) → session injection al resto del grupo → otro puede reclamar |
| Agente reclama pero la subtask no aplica a sus tools | Responde cannot_claim, no hace claim |

### Implementación

1. `team_message`: mensajes directos agente → agente (JSONL append-only + session injection, sin polling).
2. `team_claim`: toma atómica de subtasks del pool compartido.
3. **Candidate set routing**: el discovery group (definido por el CEO al iniciar) determina a quién se inyecta cada subtask. No se hace broadcast global.
4. Mesa compartida: JSONL por discovery group, no global.

- **Criterio de aceptación**:
  1. El CEO puede crear un discovery group con agentes participantes.
  2. Un agente puede publicar una subtask/capability request en la mesa compartida del grupo.
  3. El sistema hace session injection solo a candidatos del grupo (no broadcast global).
  4. Un candidato puede hacer claim atómico. Double-claim es prevenido por rename atómico.
  5. Un agente que falla puede hacer release y otro del grupo puede reclamar.
  6. Timeout sin claim → escala a humano o BLOCKED_TOOL.
  7. `team_message` permite comunicación directa agente → agente sin pasar por CEO.

---

## TICKET-012: CEO Discovery — fragmentación + tiers 0-1.5
- **Componente**: `openceo-core`
- **Prioridad**: Alta
- **Dependencia**: TICKET-005, TICKET-006
- **Descripción**: Implementar el flujo de resolución del CEO con tiers: (1) Tier 0: keywords directos → match a workflow conocido → executor determinista. (2) Tier 1: `check_registry()` → CEO match semántico → ejecutar. (3) Tier 1.5: unión de workflows existentes vía `ref:` → YAML combinado. (4) Análisis de dependencias para fragmentación paralela vs secuencial. (5) Para fragmentos paralelos: `assign_task` a cada agente. Para secuenciales: construir YAML con mapeos.
- **Criterio de aceptación**: Una tarea con keyword conocido ejecuta en Tier 0 (0 tokens). Una tarea sin keyword pero con workflow en registry ejecuta en Tier 1. Una tarea que requiere combinar workflows existentes genera YAML combinado (Tier 1.5). Tareas grandes se fragmentan correctamente en paralelo o secuencial.

---

## TICKET-013: create_ticket global + parent task tracking
- **Componente**: `openceo-core`
- **Prioridad**: Alta
- **Descripción**: (1) Extender `create_ticket` para aceptar `assigned_to` con cualquier agente (no solo dev). Agregar campo `parent_task` opcional. (2) Implementar parent task tracking: toda tarea grande que se fragmenta genera un task padre en MongoDB. Subtasks/workflow_runs/discovery_runs son hijos con `parent_task_id`. (3) Sincronización con Notion usando Relation property para parent_task.
- **Criterio de aceptación**: `create_ticket(assigned_to="finance")` funciona. Tareas fragmentadas tienen un parent task visible. Notion muestra la relación padre-hijo vía Relation. `parent_task_id` permite filtrar hijos.

---

## TICKET-014: Proactive CEO — objectives + autonomous loop
- **Componente**: `openceo-core`
- **Prioridad**: Media
- **⚠️ Solapamiento**: este ticket y TICKET-015 describen un motor de objetivos que Horizon (**TICKET-026**) ya implementa para el alcance personal. Véase la tabla de solapamiento en TICKET-026 antes de implementar: crear una colección `objectives` aparte viola la especificación D-006.
- **Descripción**: (1) Crear colección `objectives` en MongoDB con schema: objective_id, description, priority, deadline, progress, status, max_active_tickets, kpis. (2) Implementar CEO loop autónomo con dos triggers: periódico (cron configurable) y reactivo (post-workflow completion). (3) Anti-bloat rules: máximo N tickets activos por objetivo, acción concreta obligatoria, verificar tickets existentes antes de crear, KPIs medibles, scope check. (4) El CEO usa `create_ticket` global para generar tickets operativos.
- **Criterio de aceptación**: Se pueden crear objetivos con KPIs. El CEO genera tickets automáticamente cuando un objetivo tiene deadline cerca y progreso bajo. No se crean tickets duplicados ni vagos. El progreso se actualiza cuando workflows completan.

---

## TICKET-015: Internal Scheduler — cron runner
- **Componente**: `openceo-core`
- **Prioridad**: Media
- **⚠️ Conflicto de diseño**: el "revisa cada minuto" de este ticket es un barrido periódico, prohibido por **AGENTS.md §5.1** y por la especificación Horizon §9.2 pt.4. La implementación correcta es un trabajo intencional persistido con `next_run_at` y un solo timer que despierta en el próximo vencimiento. Véase TICKET-026.
- **Descripción**: (1) Crear colección `schedules` en MongoDB con schema: schedule_id, cron, workflow, params, project, status. (2) Implementar cron runner como proceso async dentro de FastAPI que revisa cada minuto. (3) CEO parsea lenguaje natural a cron expressions + workflow name. (4) Personal (project: null) vs empresarial (project: "name") en el mismo sistema. (5) Si no existe workflow cristalizado → entra a tiers normales.
- **Criterio de aceptación**: "recuérdame los jueves a las 8am comprar carne" crea un schedule con cron correcto. El cron runner ejecuta el workflow en el horario definido. Schedules con project se ejecutan en contexto del proyecto. CRUD de schedules funciona.

---

## TICKET-016: Refactor estructura de carpetas de agentes
- **Componente**: `openceo-core/agents/`
- **Prioridad**: Baja
- **Descripción**: Migrar la estructura actual de `tools.py` (archivo único por agente) a `tools/` (directorio con archivos organizados por dominio). Cada archivo max 200-300 líneas. Agregar directorio `workflows/` por agente para YAML cristalizados. Mantener backward compatibility durante la migración. Actualizar imports en `graph.py`, `router.py`, y archivos de sesión.
- **Criterio de aceptación**: Finance tiene `tools/trading.py`, `tools/payments.py`, etc. Secretary tiene `tools/email.py`, `tools/documents.py`. Los imports funcionan. El servicio arranca sin errores. Los YAML de workflows están en `agents/{name}/workflows/`.

---

## TICKET-017: Tier 0 estricto — eliminar patterns amplios de routing
- **Componente**: `openceo-core/main.py`, `openceo-core/router.py`
- **Prioridad**: Alta (los falsos positivos rompen el funcionamiento actual)
- **Descripción**: Reducir Tier 0 (keyword routing) a SOLO matches inequívocos. Mantener intactos todos los comandos de dev session/tmux (`_SESSION_START_PATTERNS`, `_SESSION_STOP_PATTERNS`, `_SESSION_LIST_PATTERNS`, `_SESSION_PROGRESS_PATTERNS`, `_SESSION_OUTPUT_PATTERNS`, `_SESSION_FOCUS_PATTERNS`, `_DELETE_*_PATTERNS`, `_SESSION_SWITCH_PATTERNS`, `_ALL_SESSIONS_STATUS_PATTERNS`, `_SIMPLE_ACTIVE_SESSIONS_PATTERNS`, `_ACTIVE_SESSION_*_PATTERNS`, `_DEV_HELP_PATTERNS`, `_MODEL_*_PATTERNS`, `_PROVIDER_*_PATTERNS`, `_PROJECT_LIST_PATTERNS`, `_PROJECT_SELECT_PATTERNS`). Eliminar `_DEV_INSTRUCTION_PATTERNS` (verbos amplios: `haz`, `crea`, `corrige`, `implementa`, etc.) que matchean casi cualquier frase en español. Eliminar `_looks_like_pm_payload()` heuristic en router.py (scoring con feature hints). Todo lo que no matchee un comando exacto/anclado debe caer al LLM fallback (DeepSeek, ya implementado). Cuando hay sesión activa y no matchea ningún comando especial, enviar directamente a la sesión tmux sin necesitar pattern de verbos — si estás en sesión dev, todo lo que no sea un comando va a la sesión.
- **Criterio de aceptación**: Los comandos de tmux/sesión siguen funcionando igual. "haz un login" ya NO matchea como dev instruction cuando no hay sesión activa. Mensajes ambiguos van al LLM fallback. Con sesión activa, todo mensaje no-comando va directo a la sesión. Cero falsos positivos por verbos genéricos.

---

## TICKET-018: Integrar Workflows YAML con Phase Workflows (unificar human_in_the_loop)
- **Componente**: `openceo-core/workflow_service.py`, `openceo-core/phase_workflows.py`, `openceo-core/task_service.py`
- **Prioridad**: Alta (bloquea casos de uso enterprise donde workflows autodescubiertos necesitan aprobaciones)
- **Dependencia**: TICKET-006 (YAML executor), Phase workflows ya implementados

### Contexto

Actualmente existen DOS sistemas paralelos para workflows:

1. **Workflows YAML** (`workflow_service.py`): Ejecutan tools deterministas (`tool`, `ref`, `loop`). NO pueden incluir humanos. El validator explícitamente rechaza `type: human_in_the_loop` (línea 769-773). El discovery LLM se instruye explícitamente para NO incluir human approvals en YAML.

2. **Phase Workflows** (`phase_workflows.py`): Fases de aprobación humana configurables por dashboard (`qa_review`, `client_signoff`, etc.). NO ejecutan tools deterministas.

**Esta separación es un problema para casos de uso enterprise donde un workflow YAML descubierto necesita insertar una aprobación humana en medio de su ejecución.** Ejemplo: un workflow de licitación que hace research automático (tools), luego necesita aprobación del PM antes de continuar con generación de documentos (más tools).

### Lo que se necesita

1. **Permitir `type: human_in_the_loop` en YAML** — eliminar la validación que lo rechaza.
2. **Cuando el executor YAML llega a `human_in_the_loop`, debe pausar** y:
   - Crear o actualizar el `workflow_run` con `status=BLOCKED_HUMAN`
   - Insertar una fase en el ticket con `status=waiting` y `phase=<step.permission>`
   - El dashboard correspondiente (según el permission) muestra el ticket pendiente
3. **Cuando el humano aprueba en el dashboard**, el `PhaseAction` debe:
   - No solo avanzar la fase del ticket, sino también **resumir el workflow_run** desde donde se quedó
   - Pasar el `action` del humano como input al siguiente step del YAML
4. **El CEO en autodiscovery debe poder incluir `human_in_the_loop`** cuando considere necesario — cambiar el prompt del LLM para permitirlo y usar permissions existentes.

### Edge cases

| Escenario | Comportamiento |
|---|---|
| Workflow YAML sin phases preexistentes | Se crea un workflow_run temporal o se vincula con el task existente |
| Human approval en medio de un loop | El loop entero espera, no solo la iteración actual |
| Rechazo humano | `on_reject` del step YAML define si vuelve a step anterior, reinicia, o falla |
| Múltiples human_in_the_loop en secuencia | Cada uno crea una fase distinta con su propio permission |

### Implementación propuesta

1. Modificar `workflow_service.py`:
   - `_validate_yaml_steps`: eliminar rechazo de `human_in_the_loop`
   - `_execute_steps`: el handler de `human_in_the_loop` (ya existe, línea 628-661) debe conectar con `task_service.create_phase_action` o equivalente
   - `_build_discovery_proposal`: permitir al LLM generar `human_in_the_loop` con `permission` existente

2. Modificar `task_service.py`:
   - `create_phase_action`: cuando la acción es `approve`, verificar si hay un `workflow_run` bloqueado para este task y resumirlo
   - `_phase_completion_updates`: si el siguiente paso es un workflow YAML pendiente, resumir ejecución en lugar de solo cambiar fase

3. Unificar modelo mental:
   - Un workflow YAML **es** un phase workflow donde algunas fases son AGENT (ejecutan YAML) y otras son HUMAN (esperan dashboard)
   - No deben ser dos conceptos separados

- **Criterio de aceptación**:
  1. Un workflow YAML descubierto puede incluir `type: human_in_the_loop` con `permission: qa_review`
  2. Al llegar a ese step, el ticket aparece en el QA Dashboard como `phase=qa_review, status=waiting`
  3. El QA aprueba → el workflow YAML continúa desde el siguiente step
  4. El CEO en autodiscovery puede proponer workflows con aprobaciones humanas intermedias
  5. Cero duplicación de estado entre `workflow_runs` y `task` phases

---

## TICKET-019: Auto-discovery automático — quitar el flag explícito obligatorio
- **Componente**: `openceo-core/workflow_service.py`, `openceo-core/task_service.py`
- **Prioridad**: Alta (el comportamiento actual contradice la visión de orquestación autónoma)
- **Relacionado**: TICKET-009 (Discovery Engine), TICKET-012 (CEO Discovery tiers 0-1.5)

### Problema (comportamiento actual incorrecto)

Hoy el auto-discovery **NO es automático**: solo se dispara si el ticket trae un flag explícito. En `workflow_service.py:826`, `_should_start_discovery()` retorna `True` únicamente si están presentes `params.discover_workflow`, `params.workflow_discovery`, o un workflow ref explícito. Por eso `run_for_task()` (línea 184-193), cuando **no** encuentra un workflow cristalizado que matchee y **no** hay flag, retorna `None` y la tarea cae directo al graph (CEO router → agente) sin pasar nunca por discovery. El discovery queda inalcanzable salvo que alguien sepa setear el flag a mano.

### Comportamiento deseado

Cuando entra una tarea, el sistema debe:
1. Intentar resolverla con un **workflow conocido** (cristalizado / registry) — igual que hoy.
2. Si **ningún** workflow conocido matchea, entrar **automáticamente** a auto-discovery (sin flag) para empezar a construir su propio camino: descubrir qué agentes/tools combinar, generar el YAML candidato, e incluso identificar/crear tools nuevos que falten (se cruza con BLOCKED_TOOL de TICKET-010).
3. El flag explícito pasa a ser, como mucho, un **override** para forzar o saltar discovery — no el requisito para activarlo.

### Notas / cuidado al implementar
- Revisar la interacción con el fallback actual al graph para no romper tickets que hoy se resuelven bien con un agente directo (p.ej. un ticket de código que va al Dev). Hace falta un **guard** que distinga "tarea sin ruta conocida → discovery" de "tarea que el CEO ya sabe rutear a un agente → directo".
- Definir el criterio de "no hay workflow conocido" y evitar entrar en discovery en tareas triviales.
- Esto encaja con la lógica de tiers 0-1.5 de TICKET-012 (keywords → registry → unión → fragmentación → discovery como último recurso).

### Criterio de aceptación
- Una tarea sin workflow cristalizado que matchee entra a auto-discovery automáticamente, **sin** flag `discover_workflow`.
- El flag explícito funciona como override (forzar o saltar discovery), no como disparador único.
- Tareas con workflow conocido siguen ejecutándolo directamente (no entran a discovery).
- Existe un guard que evita discovery innecesario cuando la tarea ya tiene ruta clara (workflow conocido o agente directo).

---

## TICKET-020: DeepSeek Direct First Layer Gateway & Adaptive Routing
- **Componente**: `openceo-core/first_layer.py`, `openceo-core/main.py`
- **Prioridad**: Alta (fase 1 implementada; fase 2 pendiente de optimización)
- **Estado**: Base operativa implementada y validada en vivo.
- **Descripción**: La primera capa de OpenCEO opera ahora mediante `FirstLayerGateway` con el modelo oficial `deepseek-v4-flash` (DeepSeek API 2026 oficial: `deepseek-v4-flash`, `deepseek-v4-pro`, `deepseek-v4-flash-vision-exp`), eliminando la dependencia de patrones regex frágiles (`_looks_like_pm_payload`, `_DEV_INSTRUCTION_PATTERNS`) que producían falsos positivos con verbos cotidianos ("haz", "crea", "roles", "login").
  - **Fase 2 pendiente**:
    1. Optimización de latencia: prompt caching en DeepSeek para fragmentos invariantes de configuración de proyectos.
    2. Streaming incremental: cuando el router genera una respuesta conversacional larga, emitir los chunks vía SSE en vez de respuesta completa en bloque.
    3. Descubrimiento dinámico de proyectos sin requerir reinicio del servicio cuando se añada un nuevo `project.yaml`.
    4. Manejo de contexto masivo: si un proyecto tiene más de 50 tickets abiertos, usar un compactador de lista de tareas en el prompt del sistema.
- **Criterio de aceptación**: El 100% de las instrucciones en lenguaje natural son interpretadas en contexto sin falsos positivos de palabras clave. Respuestas de voz inferiores a 2 segundos.

---

## TICKET-021: Mobile Walking & Voice Ticket Focus Workflow ("Caminar por la calle")
- **Componente**: `openceo-core/db/mongo.py`, `openceo-core/main.py`, `openceo-voice`
- **Prioridad**: Alta
- **Estado**: Foco persistente y consultas contextuales operativas en Mongo y API.
- **Descripción**: Permitir que el usuario camine por la calle con su teléfono y converse de forma completamente natural con todos sus tickets, manteniendo el foco en uno para trabajarlo, auditarlo o reintentarlo, y luego cambiar ágilmente a otro.
  - **Capacidades ya activas**:
    1. `set_active_ticket` / `get_active_ticket` en MongoDB `user_context`.
    2. Comandos naturales de cambio de foco: "vamos al ticket 49", "enfoca el 48".
    3. Preguntas con pronombres contextuales: "¿por qué falló?", "¿qué error tuvo?", "¿cómo va?".
    4. Endpoints REST: `GET/POST/DELETE /api/tasks/focus`.
  - **Extensiones pendientes de implementar**:
    1. Consulta por voz de criterios de aceptación: "¿qué requisitos pide este ticket?".
    2. Acciones de revisión por voz: "pásalo a QA con la nota: probado en staging", "aprueba el ticket para entrega a cliente".
    3. Reejecución con selección de modelo o provider: "reintenta este ticket con opencode y modelo kimi" o "pásaselo a claude con opus".
    4. Respuestas de audio cortas (pacing de voz): limitar los resúmenes hablados a 20-30 palabras directas para evitar saturación auditiva con auriculares.
- **Criterio de aceptación**: El usuario puede auditar, avanzar y reintentar 5 tickets consecutivos en 5 minutos caminando en la calle únicamente usando comandos de voz en español.

---

## TICKET-022: Global Host Tmux Management from Mobile
- **Componente**: `openceo-core/agents/dev/tmux_runtime.py`, `openceo-core/main.py`
- **Prioridad**: Media
- **Estado**: Métodos de control global y endpoints REST operativos.
- **Descripción**: Permitir que el usuario controle desde su celular cualquier sesión de tmux que esté corriendo en la VM (no solo las sesiones `openceo-dev-`, sino también sesiones de trading, bots o agentes como `agentboard`, `business`, `investor`, `tr`).
  - **Capacidades ya activas**:
    1. `list_all_host_sessions()` en `TmuxRuntime` retorna todas las sesiones host con estado attached y última línea.
    2. `capture_pane_by_name()` lee el buffer de cualquier sesión.
    3. `send_keys_by_name()` inyecta comandos/teclas.
    4. `create_generic_session()` abre una nueva sesión de tmux en cualquier directorio de la VM.
    5. `kill_by_name()` cierra sesiones de forma remota.
    6. Endpoints REST: `/api/tmux/sessions`, `/api/tmux/sessions/{name}/pane`, `/api/tmux/sessions/{name}/keys`, etc.
  - **Extensiones pendientes**:
    1. Watcher de salud de sesiones críticas: alerta proactiva por Telegram/WebSocket si una sesión tmux (ej. `tr` o `investor`) muere o entra en loop de crash.
    2. Presets de comandos para móvil: "reinicia el bot en tr", "limpia la pantalla de business".
- **Criterio de aceptación**: El usuario puede consultar qué está saliendo en cualquier tmux, mandar teclas o abrir una nueva sesión desde el móvil por voz o chat sin necesidad de abrir una terminal SSH.

---

## TICKET-023: Admin Panel (`openceo-react`) Mission Control Enhancements
- **Componente**: `openceo-react` (Frontend React 19 + Tailwind v4 + Vite)
- **Prioridad**: Media
- **Estado**: URL `https://dev.openceo.jardal.app` restablecida (fix de IP drift en Traefik).
- **Descripción**: El panel de administración tiene gestión completa de tickets, usuarios, grupos y revisiones QA/Cliente, pero carece de visibilidad sobre los procesos en vivo y el foco móvil:
  - **Tareas a implementar**:
    1. **Tab de Tmux Sessions / Terminal Monitor**: Nueva vista en el menú lateral para ver las sesiones tmux activas en la VM, previsualizar la pantalla del terminal en tiempo real (consumiendo `GET /api/tmux/sessions/{name}/pane`), enviar comandos y matar/crear sesiones desde la interfaz web.
    2. **Widget de Foco Activo en Header**: Mostrar un indicador permanente en la barra superior con el ticket que el usuario tiene enfocado actualmente en su teléfono (ej: `Foco Móvil: #49 melanyryr [failed]`), con botón para cambiar de foco o ir al detalle del ticket.
    3. **Optimización Responsive Mobile**: Adaptar las tablas de tickets (`TicketList.jsx`) y la tarjeta de acciones de fase (`TicketDetail.jsx`) para que no tengan desborde horizontal en pantallas de smartphones (< 420px), permitiendo realizar revisiones rápidas con una sola mano desde la calle.
- **Criterio de aceptación**: El administrador puede abrir `dev.openceo.jardal.app` desde su teléfono, ver el terminal de cualquier sesión tmux sin SSH, y revisar qué ticket tiene en foco en ese instante.

---

## TICKET-024: Mobile App (`openceo-voice-android`) Unified Mode & Focus Badge
- **Componente**: `openceo-voice-android` (Kotlin + Jetpack Compose + LiveKit Android SDK)
- **Prioridad**: Media
- **Estado**: Backend de voz `openceo-voice-agent` reparado y conectado a LiveKit.
- **Descripción**: Adaptar la aplicación móvil de Android para una experiencia 100% de manos libres:
  - **Tareas a implementar**:
    1. **Modelo Unificado por Defecto**: Configurar el selector de modelo en `TokenExt.kt` para que use por defecto el modo unificado (`ceo`), el cual utiliza `FirstLayerGateway` y elimina la necesidad de que el usuario cambie manualmente entre `dev`, `ceo`, `pm` o `finance` en la pantalla del celular.
    2. **Badge de Foco en Pantalla de Voz**: Mostrar un chip visual dinámico en `VoiceAssistantScreen.kt` con el ticket enfocado y el proyecto activo (`melanyryr · Ticket #49`).
    3. **Reconexión WebRTC Resiliente**: Configurar LiveKit Android para reintentos transparentes cuando el usuario cambia entre cobertura Wi-Fi y datos móviles 4G/5G caminando por la calle, manteniendo el estado de la conversación.
- **Criterio de aceptación**: La app Android se abre, se conecta con 1 tap al modo unificado, y muestra visualmente en pantalla el ticket y proyecto sobre el que se está conversando.

---

## TICKET-025: Sistema Financiero — ERPNext como registro canónico
- **Componente**: stack nuevo `jardal-erpnext` + `openceo-core/agents/finance`
- **Prioridad**: Alta
- **Nota**: Decisión, alternativas descartadas y mediciones en [`docs/decision-2026-09-19-sistema-financiero-erpnext.md`](file:///home/develop/development/Jardal/jardal-openceo/docs/decision-2026-09-19-sistema-financiero-erpnext.md). Se relaciona con TICKET-004 (Finance — administrador de fortuna).
- **Descripción**: Implementar el sistema de registro financiero decidido (ERPNext, GPL-3.0, self-hosted), conectado al agente Finance de OpenCEO, para cubrir dinero personal, por proyecto y a futuro por empresa.
  - **Fronteras**: ERPNext es dueño de asientos, cuentas y saldos. OpenCEO sigue siendo dueño de objetivos y tareas. El agente Finance es la puerta de entrada (captura y consulta). BTCPay Server sigue siendo la fuente de verdad de crypto y sincroniza **hacia** ERPNext. Notion nunca toca dinero. **Regla: adoptar el complemento, nunca el duplicado** — no se usan los módulos Project, CRM, Helpdesk, Calendar ni Discuss de ERPNext.
  - **Fases a implementar**:
    1. **Fase 0 — Cerrar el espejo.** Asegurar que los datos financieros personales no se espejen a Notion (`NOTION_MIRROR_ENABLED`, `openceo-core/settings.py:87`). Horizon §9.2 lo exige antes de importar información privada.
    2. **Fase 1 — Desplegar ERPNext en topology mínimo.** 1 MariaDB, 1 Redis, 1 web, 1 worker (todas las colas), 1 scheduler, socketio solo si se necesita tiempo real. Reutilizar el Traefik existente. DNS vía el MCP `manage_dns_check_or_create`. **Medir y reportar consumo real de RAM en reposo y bajo carga** — la estimación (~1–1.5 GB reposo, 2.5–4 GB carga) no sustituye la medición.
    3. **Fase 2 — Modelo de datos.** Company "Personal"; moneda base BOB, segunda USD; chart of accounts base boliviana; **Cost Center tree por proyecto** (personal, jardal-openceo, jardal-trading, jardal-vpn, melanyryc, cebitec, docencia/remoto). Decidir aquí la dimensión custom para proyectos que crucen empresas.
    4. **Fase 3 — Adapter del agente Finance.** `openceo-core/agents/finance/erpnext.py` + tools registrados en `WorkflowToolRegistry` (`openceo-core/workflow_service.py:49`): `finance.register_movement`, `finance.get_balance`, `finance.get_project_spend`, `finance.get_goal_progress`, `finance.list_pending`, `finance.register_tithe`. **Idempotencia obligatoria**: campo custom `openceo_ref` con índice único, para que un reintento no duplique el asiento. Captura por voz y Telegram — sin esto la adopción diaria no ocurre.
    5. **Fase 4 — BTCPay → ERPNext.** Arqueo de cajas: traer invoices y settlements de BTCPay Server, nodo Lightning y Binance. Relacionado con TICKET-004. Hoy no existe ninguna integración (`openceo-core` no referencia `btcpay`, `lightning` ni `binance`).
    6. **Fase 5 — Objetivos.** La colección `objectives` de OpenCEO lee los números vía Finance para mostrar "cuánto falta" contra cada meta. El objetivo vive en OpenCEO; el sistema financiero solo aporta la cifra.
    7. **Fase 6 — Empresas (futuro).** Alta de Companies reales + inter-company, cuando exista forma jurídica. **Sin migración** — por eso se elige ERPNext desde el inicio.
  - **Restricciones**: `execute_money_movements` permanece en `false` (Horizon §9.9) hasta que la conciliación esté probada. El disco del host está al 82% (55 GB libres) — condición preexistente a vigilar.
- **Criterio de aceptación**: ERPNext corre en el host con consumo medido y documentado; existe la Company "Personal" con BOB/USD y Cost Centers por proyecto; el agente Finance registra un movimiento por voz con idempotencia verificada (el mismo movimiento dos veces produce un solo asiento) y responde el saldo por proyecto; BTCPay sincroniza sus movimientos; un objetivo de OpenCEO muestra su "cuánto falta" leyendo el número desde ERPNext.

---

## TICKET-026: Horizon — fases 2 a 5 (ciclo proactivo, delegación, fuentes)
- **Componente**: `openceo-core/horizon/`, `openceo-core/workflow_service.py`, `openceo-core/scheduler.py`, `workflows/ceo/`
- **Prioridad**: Alta
- **Estado**: **Fases 1 (HZ-001) y 2 (HZ-002) implementadas y verificadas.** Fase 1 el 2026-09-19; fase 2 el 2026-09-20. Las fases 3 a 5 no han empezado.
- **Especificación**: [`jardal-horizon/README.md`](file:///home/develop/development/Jardal/jardal-openceo/jardal-horizon/README.md) v2.1. Estado en `STATUS.md` (2026-09-19).

### Solapamiento que hay que resolver ANTES de implementar

Este ticket **no** debe construirse en paralelo a TICKET-014 y TICKET-015. Hay que decidir y unificar primero, porque hacerlo por separado crea exactamente el segundo planificador que la especificación prohíbe (D-006, §9.2 punto 1: "no crear una segunda cola").

| Ya documentado | Qué describe | Conflicto / relación con Horizon |
|---|---|---|
| `README.md` → `## Proactive CEO — Autonomous Objectives` + **TICKET-014** | Colección `objectives` **plana** (`objective_id, description, priority, deadline, progress, status, max_active_tickets, kpis`) y loop del CEO con dos disparadores | Es el **mismo motor** en otro alcance. Horizon ya implementa la jerarquía misión → objetivo → hito → acción en `horizon_objectives`, con propietario, privacidad, capacidad y aparcados por condición. Crear una `objectives` aparte duplicaría el motor. **Decisión: un solo motor, dos alcances** — `horizon_objectives` para lo personal, y el alcance de empresa se modela sobre el mismo motor en vez de una colección nueva. |
| **TICKET-015** | Cron runner que "revisa cada minuto" | Contradice **AGENTS.md §5.1** (prohibido barrido periódico) y §9.2 punto 4. La solución de Horizon es trabajos intencionales persistidos con `next_run_at`, un solo timer que despierta en el próximo vencimiento, recuperación al reiniciar y reintentos acotados. **No copiar el patrón de barrido.** |
| **TICKET-025** (ERPNext) + `docs/decision-2026-09-19-sistema-financiero-erpnext.md` | Sistema financiero canónico externo | **Es** la frontera financiera de Horizon §4.7. Horizon no debe construir registro financiero propio: consume el adapter del agente Finance cuando exista. El objetivo financiero de Horizon muestra "cuánto falta" leyendo la cifra de ERPNext. |

### Fases a implementar

1. **HZ-002 — Ciclo proactivo.** Planificador de capacidad con la regla del 70% (ejercicio y descanso reservados antes del crecimiento); una **única versión de plan vigente por usuario**; disparadores persistidos según el párrafo anterior; replanificación reactiva cuando un resultado de tarea llega; revisión automática periódica; y **modo degradado**: con el presupuesto de LLM agotado, los cálculos, el registro y la consulta del último plan válido siguen funcionando.
   - Tools `ceo.plan_day` y `ceo.review_objectives` en `WorkflowToolRegistry` (`workflow_service.py`), hoy inexistentes.
   - `workflows/ceo/horizon_daily.yaml` (el directorio `workflows/ceo/` existe y está vacío).
   - Cuidado: el motor YAML **no** habilita `llm_eval` (`workflow_service.py:1355` lanza `ValueError`). Cualquier condición de `loop` debe ser `python_eval`.
   - Criterio: una interrupción real modifica el plan y la acción nueva llega al usuario; no existe una segunda cola paralela a las tareas.
2. **HZ-003 — Delegación y dev móvil.** Encargo autónomo de un ticket de alcance personal sobre las sesiones dev existentes, midiendo tiempo humano y retrabajo. Entrega única del resultado aunque el teléfono se desconecte.
3. **HZ-004 — Fuentes conectadas y espacio personal.** Fuentes que reduzcan la captura manual, y la vista de misión/objetivos/progreso en `openceo-react` con permisos por propietario. (Se relaciona con TICKET-023.)

### Cerrado en la fase 2

| Pendiente | Cómo se cerró |
|---|---|
| Importar el contexto desde voz o chat sin sesión web | Intent `horizon_import` ("importa mi contexto") en el clasificador determinista y en el prompt del modelo. |
| El espacio personal arrancaba vacío y `qué sigue?` respondía que no había objetivos | Se resuelve con lo anterior. **Además se encontró y corrigió un fallo silencioso**: el chat resolvía `user_id = payload.user or "openwebui"` (`main.py:1355`) mientras el dashboard usa el usuario de la sesión, así que una captura por voz se guardaba bajo un propietario que el dashboard nunca consulta. Ahora `HorizonService.owner_for()` es el único punto de resolución y todo converge en `HORIZON_OWNER_USER_ID` (hoy `usr_8c9941ae0bc9`, admin). No hubo datos que migrar: `horizon_objectives` y `horizon_captures` estaban vacíos. |

### Pendiente de la fase 2: el canal de entrega

El plan del día se **persiste y se expone** (API, voz/chat, dashboard); **no hay push**. La fila de la sección 9.10 *"Llega la hora diaria y no has abierto el chat → Recibes una acción preparada por el canal probado"* se cumple a medias: el disparador de las 06:00 **prepara y deja listo** el plan con `delivered_at` pendiente, y el usuario lo recibe en su siguiente interacción. Es una decisión explícita del propietario, no un olvido, y por eso queda escrito aquí en vez de darse por cumplido.

Estado real de los canales salientes, verificado en código:

| Canal | ¿Funciona con la app cerrada? | Estado |
|---|---|---|
| **Email (SMTP)** — `secretary.send_email`, `openceo-core/tools/email_sender.py` | **Sí** | Implementado, configurado y multi-cuenta. **Es el único canal durable que existe hoy** y el candidato natural para cerrar este hueco. |
| Telegram | — | **Stub sin implementar**: `integrations/telegram_bot.py` dice `# Implement this in phase 7` y nadie lo importa. Sin variables de configuración. |
| WhatsApp | — | Sólo un valor de enum para el origen de tickets entrantes, no un canal saliente. |
| `RealtimeHub` (`/ws`) | **No** | Efímero: sin persistencia, sin replay y sin dirigirse a un usuario concreto. Sólo alcanza un dashboard ya abierto. |
| Voz (LiveKit) | **No** | El agente espera a que alguien entre a la sala (`openceo-voice/agent.py:259-260`). `openceo-core` no tiene ninguna referencia a LiveKit: hoy no puede alcanzar la voz. |
| Recordatorios programados / cron | — | No existe. No hay dependencia de scheduler en `requirements.txt`. |

### Cerrado también: el solapamiento con TICKET-014 y TICKET-015

La fase 2 implementa el motor de objetivos sobre `horizon_objectives`, **sin** crear una colección `objectives` paralela (D-006) y **sin** cron de barrido: disparadores persistidos con `next_run_at` y un solo timer que despierta en el próximo vencimiento. `TICKET-014` y `TICKET-015` quedan absorbidos por este motor y no deben implementarse por separado.

- **Criterio de aceptación** (estado tras la fase 2):
  1. Un solo motor de objetivos sirve a los alcances personal y de empresa, sin colecciones competidoras. — **Cumplido**: todo vive en `horizon_objectives`; TICKET-014/015 quedan absorbidos.
  2. El plan diario se entrega a la hora configurada mediante un trabajo persistido, no un barrido, y sobrevive a un reinicio del servicio. — **Cumplido en el mecanismo**: disparador persistido con `next_run_at`, timer único sin sondeo (con test que lo comprueba), recuperación al reiniciar y reintentos acotados. **Parcial en la entrega**: se prepara y se deja disponible, sin push (véase "Pendiente de la fase 2: el canal de entrega").
  3. Una interrupción real replanifica y notifica la acción nueva. — **Cumplido**: replanificación reactiva al cerrarse un ticket de alcance personal, con deduplicación por `(task_id, status)`. La *notificación* queda en el mismo canal pendiente del punto 2.
  4. Con el presupuesto de IA agotado, captura, cálculo y consulta del último plan siguen respondiendo. — **Cumplido por construcción**: el ciclo no llama a ningún LLM, y un test lo verifica inspeccionando los imports reales de los módulos.
  5. El contexto personal es importable desde voz o chat sin sesión web. — **Cumplido**, con la unificación de propietario descrita arriba.
  6. Un ticket de alcance personal se delega y su resultado se entrega una sola vez. — **Pendiente: es HZ-003.**

