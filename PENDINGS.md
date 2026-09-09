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
- **Descripción**: (1) Crear colección `objectives` en MongoDB con schema: objective_id, description, priority, deadline, progress, status, max_active_tickets, kpis. (2) Implementar CEO loop autónomo con dos triggers: periódico (cron configurable) y reactivo (post-workflow completion). (3) Anti-bloat rules: máximo N tickets activos por objetivo, acción concreta obligatoria, verificar tickets existentes antes de crear, KPIs medibles, scope check. (4) El CEO usa `create_ticket` global para generar tickets operativos.
- **Criterio de aceptación**: Se pueden crear objetivos con KPIs. El CEO genera tickets automáticamente cuando un objetivo tiene deadline cerca y progreso bajo. No se crean tickets duplicados ni vagos. El progreso se actualiza cuando workflows completan.

---

## TICKET-015: Internal Scheduler — cron runner
- **Componente**: `openceo-core`
- **Prioridad**: Media
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

