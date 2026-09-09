# OpenCEO Core

Resumen operativo corto del estado real del repo.

## Estado actual (2026-09-09)

- `Migración Gemini CLI -> Antigravity CLI (agyd)`: reemplazado en su totalidad el uso de `gemini -y` por `agyd` (`/home/develop/.local/bin/agyd`, wrapper con `--dangerously-skip-permissions`). Esto resuelve el bloqueo crítico P2 en `WriteFile` durante revisiones Tier S y turnos de código, operando de forma 100% autónoma.
- `Configuración agyd sin --model`: `agyd` se configuró sin flag de modelo para que utilice su valor predeterminado del CLI (**Gemini 3.8 Flash · High**), garantizando arranque inmediato en `tmux` (<1s) y la versión más reciente con razonamiento alto.
- `TmuxRuntime & Prompts de Antigravity`: ajustado el regex de prompt de `gemini` para detectar `? for shortcuts` y `>`, detección de banner de Antigravity CLI, y estados de generación (`Generating...`, `esc to cancel`) para evitar falsos positivos de turno completado.
- `Proyectos y Providers`: actualizados `projects/jardal-trading/project.yaml` (`gemini_reviewer` en Tier S y `providers.gemini`), `projects/jardal-openceo/project.yaml`, `projects/melanyryr/project.yaml`, `openceo-core/settings.py` (`GEMINI_CLI_PATH=/home/develop/.local/bin/agyd`, `GEMINI_DEFAULT_ARGS=""`), `.env` y `.env.example`.
- `Routing Natural OpenWebUI`: `openceo-core/main.py` ahora reconoce intenciones como `crea una sesión agy`, `crea una sesión agyd` o `crea una sesión gemini` para enrutar directamente al provider.
- `UI Kanban Mission Control & Vistas Rápidas (openceo-react)`: implementado tablero Kanban completo en `openceo-react/src/components/KanbanBoard/index.jsx` integrado tanto en `Dashboard.jsx` (inicio/panel administrativo) como en `TicketList.jsx` (con switch dinámico Tabla/Kanban). Soporta 3 modos de agrupación de columnas (Por Estado, Por Agente: Dev/Finanzas/Secretaría/PM/CEO, y Por Workflow), buscador reactivo en tiempo real, filtros combinados por Proyecto, Agente, Workflow y Prioridad, badges estilizados con ADN Obsidian/DESIGN.md, y triggers rápidos de ejecución.
- `API Task Filtering por Agente`: endpoints `GET /api/tasks` y `POST /api/tasks/search` en `openceo-core/main.py` ahora soportan filtrado directo por `agent` (evaluando `agent` y `agent_hint`).

## Estado actual (2026-09-08)

- `First Layer Gateway`: implementado en `openceo-core/first_layer.py` con el modelo oficial `deepseek-v4-flash`, resolviendo comprensión de contexto en lenguaje natural, foco de tickets y control de tmux sin falsos positivos de palabras clave
- `Ticket Focus Workflow`: persistencia de ticket activo en `user_context` (`active_ticket_number`, `active_ticket_id`) + endpoints `/api/tasks/focus` + comandos conversacionales de voz ("vamos al ticket 49", "¿por qué falló?")
- `Host Tmux Management`: soporte en `TmuxRuntime` para listar, capturar pane, inyectar teclas y crear sesiones para todos los tmux del host (`agentboard`, `business`, `investor`, `tr`, etc.) + endpoints REST `/api/tmux/sessions`
- `Admin Dashboard (openceo-react)`: corregido 502 por drift de IP en Traefik; puerto 3000 mapeado a localhost en `docker-compose.yml` y Traefik configurado a `127.0.0.1:3000`; dashboard online en `https://dev.openceo.jardal.app` (200 OK)
- `LiveKit Voice Agent`: corregido fallo de conexión a LiveKit (se añadió red `traefik-net` y URL interna `ws://openceo-livekit:7880`); agente de voz operativo y escuchando en puerto 8081
- `Phase 1 (Core)`: completada
- `Phase 2 (CEO + Dev Agent)`: completada
- `Voice backend bridge`: base implementada en `openceo-voice/`
- `MCP dual`: Internal MCP (3 tools: DNS + Notion) para sesiones OpenCEO, Global MCP (1 tool: DNS) para toda la VM
- `Apps SDK MCP test`: conector MCP en `apps-sdk-mcp-test/` expuesto por Traefik en `https://mcp.openceo.jardal.app/mcp`; registra `get_company_context`, `get_user_context`, `execute_workflow` y `get_task_status`, usa `@modelcontextprotocol/ext-apps` para la primera UI estandar MCP Apps colgada correctamente de `execute_workflow` en el caso `pm.create_tickets` (selector dark host-driven para proyecto + confirmacion de batch PM), ya no expone tools PM extras ni la demo ChatGPT-only de `secretary`, ya no inyecta `widget_html` inline en `execute_workflow`, declara `ui.domain=https://mcp.openceo.jardal.app` en el template MCP Apps para evitar el warning de widget domain en ChatGPT, y ahora deja tres reglas generales escalables: discovery al inicio del chat con `get_company_context` y refresco solo si el catalogo esta ausente, viejo o dudoso; `get_user_context` solo on-demand cuando la peticion depende de estado actual y `active_project` queda como contexto advisory; y `mcp-first` para acciones reales, priorizando el workflow conectado por encima del comportamiento local del modelo cuando exista una capacidad compatible. Cuando el workflow ya esta identificado y los faltantes restantes son seleccionables/confirmables, la guia viva del connector empuja a entrar en `execute_workflow` para que el propio contrato devuelva `needs_input` o MCP Apps, en lugar de cerrar ese loop manualmente en puro chat. En side effects con aprobacion, la expectativa correcta es resumen corto + paso de aprobacion, no pegar el artefacto final completo salvo que el usuario pida revisar el texto exacto. `pm.create_tickets` en este path ya no escribe directo a Notion: registra un task canonico en Mongo por ticket con `autorun=false`, lo deja en cola manual de dev y deja que el worker global de mirror sobre Mongo clone la vista minima a Notion aunque el task haya nacido fuera del runtime principal. Para PM y otras escrituras sensibles por proyecto, el connector ya no cae silenciosamente a `active_project`: si el proyecto no viene explicito, fuerza confirmacion o seleccion UI antes de registrar. Soporta jobs async persistidos en Mongo, persiste/recupera `client_id` OAuth dinámicos y la clave de firma del issuer OAuth embebido para no invalidar conectores/tokens tras restart o redeploy, expone TTL configurable de access token y el contenedor público ya monta repo/runtimes/redes internas para ejecutar workflows reales contra Mongo/SMTP/OpenCEO
- `Rate Limit Management`: implementado en `openceo-core/rate_monitor.py`
- Tool compartido de Notion: creación de tickets (individual + batch) + sync-back bidireccional implementados
- Notion sync-back: idempotencia de tasks + escritura automática de Runtime Status, OpenCEO Ref, Runtime Summary, Last Sync At, Agent
- Review/approval data model: `approval_required` + `approval_state` como estado global mínimo del task, con historial separado en `task_reviews`
- Workflow engine genérico inicial: carga YAMLs desde `workflows/`, ejecuta steps deterministas con `TOOL_REGISTRY`, persiste `workflow_runs`, soporta `ref`, `when`, loop `python_eval` y `discovery_runs` aprobables que cristalizan YAML. Las aprobaciones humanas ya no viven en YAML; deben resolverse con `status=waiting` + `phase` desde dashboard
- Phase workflow dashboard: cada proyecto puede declarar `workflow_templates` reutilizables por tipo/agente, cada task copia `workflow_key`, `workflow_phases`, `phase`, `phase_index` y `phase_assignments`, y las fases humanas (`qa_review`, `client_signoff`, etc.) pausan con `status=waiting` para revisión desde React
- Target real de Notion configurado en el core y workspace inicial de OpenCEO preparado
- `PM Agent`: implementado con flujo guiado de entrevistas, 3 motores de extracción, API directa para dashboard
- Modelos OpenWebUI expuestos: `ceo`, `dev`, `pm`, `secretary`, `finance`
- Workers realmente operativos hoy: `dev`, `pm`, `secretary`, `finance`
- `openceo-dev` funciona en dos modos: conversación local + sesión activa explícita
- Los turnos dev se ejecutan en background; OpenWebUI no queda bloqueado esperando al provider
- Runtime dev actual: sesiones tmux persistentes
- El runner legacy de tickets ahora usa `coding_tier: tier3` como one-shot `OpenCode build` (`opencode run --agent build`) solo para la cola de tasks; no cambia el peer programming conversacional de `openceo-dev`
- En host-mode, `scripts/openceo-core-host.sh` ahora exporta `PATH` con `~/.local/bin`, `~/.npm-global/bin` y `~/.opencode/bin`, y fija `KIMI_CLI_PATH=~/.local/bin/kimi` por defecto para que Tier4/Kimi no falle por `Command not found`
- `openceo-core/.env` ahora fija `KIMI_CLI_PATH=/home/develop/.local/bin/kimi` y `projects/melanyryr/project.yaml` declara `providers.kimi.command` explícito; los tickets `tier4` de `melanyryr` ya no dependen del `PATH` del servicio
- `openceo-core/graph.py` ahora pasa `ticket_number` y `params` completos al Dev Agent; el prompt vuelve a incluir `Ticket`, `Module`, `Type` y `Acceptance Criteria` desde `pm_ticket`
- `openceo-core/graph.py` ahora respeta la fase agente actual del workflow (`dev_work -> agent=dev`) como source of truth para routing; un ticket DEV con `workflow_phases` ya no puede rebotar accidentalmente a `pm_extraction` por heurística CEO o por `params.workflow=pm.create_tickets_direct`
- `openceo-core/agents/dev/session.py` ya no agrega los steps fantasma `branch_setup` ni `push_pr`; la UI de ejecución muestra solo trabajo real del runtime actual
- `openceo-core/task_service.py` limpia `steps`, `last_prompt_sent`, `last_response_summary`, `completed_at`, `waiting_reason` y `resume_at` al relanzar un ticket, evitando basura histórica de intentos viejos
- Los tickets `tier3` y `tier4` ahora asignan título humano a la sesión CLI: OpenCode recibe `--title`, Kimi persiste `custom_title` en su `state.json`, y ambos guardan `coding_session_id` + `coding_session_title` en el ticket para verlos desde el dashboard
- El runner Dev ahora exige un bloque final `REVIEW_LINKS` en el prompt, extrae links directos de vistas afectadas desde OpenCode/Kimi/otros CLIs, los guarda en `params.review_links` + `params.test_url` + `params.client_review_url`, y el dashboard muestra múltiples links para QA/cliente cuando existen
- El runner `tier3/opencode` ya no depende de un resumen final del modelo para cerrar el ticket: ahora envuelve `opencode run` en shell con sentinel `OPENCEO_OPENCODE_DONE::<id>:<exit_code>`, falla explícitamente si no aparece la marca de fin, y usa ese exit code real para distinguir cierre exitoso de cierre roto
- El runner `tier4/kimi` ahora detecta fallos reintentables de `--print`: usa el exit code `75` propio de Kimi CLI para 429/5xx/timeouts, distingue `rate_limit:kimi` vs `retryable_error:kimi`, y deja el ticket en `waiting` con `resume_at` en vez de `failed`
- Las sesiones QA de Kimi ya escriben en el mismo `~/.kimi` que consume Kimi Web: `KimiSessionManager` respeta `KIMI_SHARE_DIR` y el contenedor monta `/home/develop/.kimi` en `/home/ceouser/.kimi` con permisos de escritura, evitando sesiones "fantasma" creadas solo dentro del contenedor
- El paso `update_files` del runner Dev ahora es best-effort: si `CONTEXT.md` no puede escribirse por volumen read-only o permisos, el step se marca `skipped` y no tumba tickets cuyo `code` ya terminó bien
- `CONTEXT.md` ya no se resuelve bajo `projects/<project>/`. El runtime usa `PROJECT_CONTEXTS_DIR`; en Docker apunta a `/app/.runtime/project-context` para mantener `projects/` en read-only sin perder el log de contexto
- El runtime OpenCode ya corrige automáticamente paths hardcodeados de host vs Docker: si `project.yaml` trae `/home/develop/.opencode/bin/opencode` pero el binario real existe en `/home/ceouser/.opencode/bin/opencode`, Tier3/TierS lo reescriben antes de lanzar el provider
- `resume_queued_tickets()` ya respeta de verdad el límite de 1 ticket activo por proyecto dentro del mismo ciclo del scheduler: después de lanzar uno, marca ese proyecto como ocupado y no dispara los demás `queued` del mismo repo hasta la siguiente oportunidad
- `melanyryr` mantiene `branch_base: develop` alineado con su `AGENTS.md`, pero OpenCEO no vuelve a ejecutar ni verificar git en runtime; el cumplimiento del flujo de branch/PR/merge queda otra vez delegado solo al CLI vía prompt + `AGENTS.md`
- Sesiones QA (Kimi + OpenCode): ahora construyen un prompt rico que incluye prompt original del agente dev, AGENTS.md completo, búsqueda automática de ramas git + PRs cerrados por número de ticket, y rol instruido para ponerse al tanto y esperar observaciones del QA sin empezar a modificar. Siempre crean sesión nueva (sin reúsar existentes). El endpoint `/api/tasks/{task_id}/phase-opencode` crea sesiones OpenCode con modelo `opencode-go/kimi-k2.6` y deep-linkea directo al chat via `https://opencode.jardal.app/{base64(work_dir)}/session/{sid}`. El dashboard muestra ambos botones: Kimi y OpenCode, y espera la respuesta del backend antes de abrir la pestaña.
- Routing CEO configurable por proyecto: `project.yaml -> routing.agents.{agent}.triggers|patterns` + fallback LLM opcional
- `openceo-dev` reconcilia sesiones abiertas contra `tmux` en cada entrada, para no dejar sesiones "fantasma" en Mongo
- Al arrancar el servicio, las sesiones dev que quedaron en `running` sin task viva pasan a `waiting_user` para evitar falsos "instruccion enviada"
- Con sesión enfocada, solo bypassean localmente `sal de la sesión` y `que respondio|que dijo|que salio`; el resto se manda al tmux salvo que la sesión siga en `running`
- El wrapper final para dev ya no fuerza "frase corta"; pide respuesta natural pensada para voz y evita falso positivo al eco del prompt
- `openceo-voice-android` permite escoger el modelo antes de unirse a LiveKit
- Packaging de skills alineado: cada carpeta en `openceo-core/agents/*` tiene `requirements.txt` y `.env.example`

## Flujos vigentes

### `openceo-dev`

- Default: conversación local en español, sin crear sesión automática
- Activación explícita: `crea una sesión dev` o `crea una sesión codex|claude|glm|gemini|agyd|opencode`
- Con sesión activa + instrucción: responde al instante y ejecuta en segundo plano
- Dentro de sesión enfocada, los únicos atajos locales de salida son `que respondio`, `que dijo` y `que salio`
- Fuera del foco siguen existiendo preguntas naturales administrativas como `que sesiones hay activas`, `que proyectos hay`, `como va`, `ya termino`
- Los comandos con `/` para control de sesiones/modelo ya no forman parte del flujo voz/chat
- `reestablece la ultima conversacion` vuelve a enfocar la última sesión abierta y envía `/resume` internamente al provider
- Estado y salida natural comparten el mismo resolvedor: si ya hay respuesta final, la devuelve; si no, dice que sigue trabajando
- Cuando ya hay respuesta final, se devuelve directa y completa, sin prefijo tipo "Ultima respuesta de..." ni truncado artificial
- El wrapper final de dev volvió a ser mínimo; solo exige una respuesta final y evita URLs, IDs, rutas y nombres de archivos salvo que hagan falta
- El formatter de salida conserva párrafos y listas útiles; ya no aplasta toda la respuesta a una sola línea
- El parser de proyecto ya no toma `usa ... primero ...` como cambio a `cebitec`; los índices/ordinales de proyecto requieren contexto explícito
- Listado natural: `que sesiones hay activas`, `que proyectos hay`
- Si el `tmux` real desapareció, la sesión se cierra antes de enrutar y el chat no responde "instrucción enviada" a una sesión muerta
- Si el bloque final ya apareció en el pane pero Mongo seguía en `running`, `openceo-dev` lo reconcilia y pasa la sesión a lista
- Salida: `sal de la sesión` quita foco; `cerrar sesión dev` / `elimina esa sesión` cancela la sesión
- Gestión natural soportada: `selecciona la 2`, `usa la sesion de <proyecto>`, `selecciona la sesion 2`, `elimina todas las sesiones`
- `reanudar sesion` ya no reabre sesiones cerradas; para lenguaje natural se trata como entrar/enfocar una sesion abierta

### `openceo-pm`

- Flujo guiado por chat: absorbe proyecto + herramienta + entrevista en cualquier orden y pregunta solo lo que falta
- Si el usuario entra por el perfil `ceo` y el intent cae en PM, se deriva al flujo guiado PM en vez de crear una task genérica
- Si el flujo PM ya está activo en el chat, los siguientes turnos continúan en PM aunque el mensaje sea solo `1`, `2`, `si`, etc.
- Frases de intención como `tengo una entrevista` disparan el flujo, pero no se guardan como entrevista real si no traen contenido sustantivo
- Si el usuario pega una entrevista o requerimiento sustantivo como primer mensaje en `ceo`, el router lo deriva a PM aunque no diga la palabra `entrevista`
- Si la entrevista llega antes que proyecto o herramienta, se guarda en `user_context.pm_state` y el chat sigue guiando hasta completar los faltantes
- Cuando ya quedaron definidos proyecto y herramienta, el prompt final pide solo la entrevista y deja explícito que no hace falta repetir lo anterior
- Confirmación obligatoria antes de procesar
- Después de confirmar, PM responde al instante y ejecuta la extracción en segundo plano; el chat no queda bloqueado esperando a DeepSeek, Codex o Claude
- Mientras el job PM corre o después de terminar, el empleado puede preguntar naturalmente `ya termino` o `que tickets creo` y el chat responde usando el estado persistido
- Si el servicio reinicia mientras un PM estaba corriendo en background, ese estado pasa a `background_failed` al arrancar para no dejar falsos `running`
- 3 motores: Básica (DeepSeek/langraph), Media (Codex), Máxima (Claude)
- DeepSeek: 1 llamada LLM → JSON tickets → `create_tickets_batch()` directo
- DeepSeek ya tolera JSON fenced (` ```json ... ``` `) y volvió a crear tickets reales en Notion en smoke mínimo
- Todas las corridas PM ahora dejan `source_id` en Notion; DeepSeek lo escribe directo y Claude/Codex lo fuerzan desde el tool MCP
- Claude headless ya recibe MCP explícito por `--mcp-config`; no depende del cwd ni del `~/.claude.json` interactivo
- La herramienta MCP PM fuerza `project`, `source` y `source_id` en todo el lote; el modelo ya no puede “olvidar” esos campos
- Si Codex/Claude crean tickets pero el CLI termina en timeout, OpenCEO puede recuperarlos desde Notion por `source_id` y reportar éxito real
- Smoke real validado contra Notion: DeepSeek, Codex y Claude volvieron a crear tickets en `melanyryr`
- Estado del flujo guiado persiste en MongoDB (`user_context.pm_state`)
- Excepcion vigente: el path legacy de extraccion PM en OpenWebUI todavia escribe directo en Notion y aun no genera automaticamente un `tasks` canonico por ticket en Mongo; el path MCP `pm.create_tickets` ya quedo Mongo-first
- API directa: `POST /api/pm/extract` con `{project, interview, tool}` — listo para dashboard
- API de lectura para dashboard/chat ops: `GET /api/pm/status?user_id=...`
- Modelo OpenWebUI: `openceo-pm` o `pm`

### Notion Sync Bidireccional

- Ley arquitectonica: MongoDB es la fuente de verdad canonica de runtime y cola de trabajo; Notion debe actuar solo como espejo/proyeccion de los campos necesarios para humanos
- Evento nuevo: `task_created` en `TaskService` programa el mirror minimo a Notion para cualquier task canonico creado en Mongo
- Tasks con `source=notion` + `source_id` son idempotentes: segundo POST devuelve el existente
- Índice parcial único en `(source, source_id)` en MongoDB previene duplicados
- Al cambiar estado de un task (in_progress, completed, failed, etc.), se escribe de vuelta a la página de Notion:
  - `Runtime Status` (select), `OpenCEO Ref` (rich_text), `Runtime Summary` (rich_text), `Last Sync At` (date), `Agent` (select)
- Si el task usa gate de aprobación, OpenCEO también puede sincronizar `Needs Approval` y `Approval State` cuando esas propiedades existan en Notion
- Sync-back es fire-and-forget (`asyncio.create_task`), nunca bloquea ejecución del task
- `post_actions_executed` en TaskDocument trackea qué syncs ya se dispararon
- Kill switch: `NOTION_SYNC_BACK_ENABLED=false`
- Propiedad `Source` eliminada de Notion (era redundante con metadata interna)
- Agent options alineados: `ceo`, `dev`, `finance`, `secretary`, `marketing`, `sales`, `media`
- Propiedades opcionales de workflow humano: `Sync Enabled`, `Needs Approval`, `Approval State`
- El tool de Notion ya autoagrega opciones faltantes de `Project` y `Tags` al schema antes de crear páginas; proyectos nuevos como `melanyryr` ya no requieren edición manual en Notion

### Review / Approval

- `TaskDocument` no crece con historial de revisión; solo guarda el estado global mínimo: `approval_required` y `approval_state`
- El historial append-only vive en MongoDB `task_reviews`, enlazado al task canónico por `task_id`
- API directa disponible:
  - `GET /api/tasks/{task_id}/reviews`
  - `POST /api/tasks/{task_id}/reviews`
- Cada review actualiza el resumen vigente del task cuando cambia el gate (`approved`, `changes_requested`, `rejected`) y mantiene la trazabilidad fuera de `params`

### Phase Workflows / Dashboard Approval

- `ProjectDocument.workflow_templates` define flujos dinámicos por proyecto. Defaults actuales: `dev.default` (`dev_work -> qa_review -> completed`) y `dev.client_visible` (`dev_work -> qa_review -> client_signoff -> completed`)
- `TaskDocument` copia el flujo al ticket con campos genéricos: `workflow_key`, `workflow_phases`, `phase`, `phase_index`, `phase_assignments`, `phase_started_at`
- `phase_assignments` agrega usuarios involucrados al ticket para que QA/cliente puedan verlo sin inventar campos especializados por rol
- Las fases humanas usan `status=waiting` + `waiting_reason=phase:<phase>` y permisos por fase (`qa_review`, `client_review`)
- Rechazar o pedir cambios vuelve el ticket a `status=queued`, `agent=ceo`, conserva historial y deja feedback en `params.phase_rejections`
- APIs vigentes: `POST /api/tasks/{task_id}/workflow`, `POST /api/tasks/bulk-apply-workflow`, `POST /api/tasks/{task_id}/phase-action`, `POST /api/tasks/{task_id}/phase-kimi`, `GET/PUT/DELETE /api/projects/{project}/workflow-templates/{key}`
- React vigente: lista de tickets permite aplicar workflow masivo a los visibles, `QADashboard` filtra `phase=qa_review,status=waiting`, `ClientReview` filtra `phase=client_signoff,status=waiting,client_visible=true`

### Workflow Engine

- `TaskDocument` sigue siendo el contrato canónico; un task puede disparar un `workflow_run` si declara `params.workflow|workflow_id|workflow_ref` o si pide `params.use_workflow=true` y hay match único contra YAML con `auto_match=true`
- Si no hay workflow compatible, el runtime existente (`graph.py` -> agentes concretos) sigue como fallback para no romper `dev`, `pm`, `finance` ni `secretary`
- YAMLs activos iniciales:
  - `workflows/core/echo.yaml` para smoke sin side effects
  - `workflows/finance/pago_elfec_house.yaml` apuntando al flujo ELFEC existente
- APIs nuevas: `GET /api/workflows`, `GET /api/workflow-runs`, `GET /api/discovery-runs`, `POST /api/discovery-runs/{discovery_id}/approve`, `POST /api/tasks/{task_id}/discover-workflow`
- Discovery actual: genera o guarda una propuesta YAML y espera aprobación humana antes de cristalizarla; el ReAct peer-to-peer completo con claim cross-agent queda como siguiente capa

### Tier S

- Activo cuando `project.yaml` define `coding_tool: tier_s` y el usuario no fuerza provider
- Proyecto con default actual: `jardal-trading`
- Levanta 4 sesiones tmux: `dev-{project}-claude|codex|gemini|opencode` (el reviewer `gemini` corre `agyd` con `--dangerously-skip-permissions`, evitando diálogos de confirmación en escritura de archivos)
- Claude lidera; reviewers escriben en `FEEDBACK-*.md`
- El reviewer `opencode` sigue siendo un solo slot de Tier S, pero ya no usa el team runtime experimental
- OpenCode corre 5 reviewers headless one-shot mediante `opencode run --agent build --model ... --format json`, con scheduler híbrido por tandas y compilación final separada
- Scheduler OpenCode actual: `GLM-5 + Kimi` en paralelo, luego `M2.5 + M2.7` en paralelo; `DeepSeek Reasoner` corre aparte desde el inicio y el compilador arranca solo cuando terminan los 5 reviewers
- Reviewers OpenCode actuales: `GLM-5`, `Kimi K2.5`, `MiniMax M2.5`, `MiniMax M2.7`, `DeepSeek Reasoner`
- Los modelos OpenCode Go del consenso ya no están hardcodeados: salen de `openceo-core/.env` vía `OPENCODE_GO_MODEL_*`
- `OPENCODE_MODEL_OVERRIDE` puede forzar el modelo efectivo del provider `opencode` sin editar `project.yaml`; hoy queda desactivado para respetar el modelo configurado por proyecto
- Los prompts de reviewers OpenCode comparten un armazón común, pero cada modelo tiene `Suggested strength areas`, `context_focus` y `specialty` propios; esas fortalezas son sugerencias, no límites duros del review
- El consenso OpenCode ya no lo hace un team lead global: después de los 5 reviewers corre 1 compilador headless separado con `--agent build --model opencode-go/minimax-m2.7`, que deduplica findings y escribe `FEEDBACK-opencode.md`
- Los artifacts intermedios viven en `.opencode-team-review/step-XX-iteration-YY/`; solo `FEEDBACK-opencode.md` es consumido por OpenCEO
- Cada reviewer OpenCode y el compilador rescatan su `sessionID` desde la salida JSON y lo persisten en archivos `.opencode-session-opencode-reviewer-*.id` y `.opencode-session-opencode-compiler.id`; en las siguientes rondas se reutiliza con `--session` para mantener contexto acumulado del Tier S
- OpenCEO ya no reparte timeouts dinámicos por batch; cada reviewer OpenCode puede correr hasta 10 minutos y el compilador hasta 10 minutos antes de corte forzado
- Si la compilación final sale bien, se limpian los artifacts intermedios; al cerrar la sesión Tier S se limpian también los `session_id` persistidos de reviewers y compilador
- La orquestación avanza por señales deterministas: `.consensus-status`, `REVIEW_DONE_*`, `OPENCEO_FINAL_BEGIN/END`
- Pre-launch check de rate limits: Tier S requiere Claude + Codex disponibles antes de iniciar

### Legacy Ticket Tier 3

- En la cola legacy de tasks, `coding_tier: tier3` ya no cae en Claude: ejecuta un one-shot de OpenCode con `run --format json --agent build`
- Ese runner falla correctamente si OpenCode devuelve un evento JSON `type:error` aunque el proceso salga con código 0
- Ese path usa el `cwd` y `env` configurados en `project.yaml -> dev.providers.opencode`
- El cambio aplica solo al runner one-shot de tickets (`openceo-core/agents/dev/session.py`); las sesiones vivas de `openceo-dev` mantienen su resolución actual de provider

### Rate Limit Management

- `rate_monitor.py` implementado con monitor sessions dedicadas (`dev-monitor-codex`, `dev-monitor-claude`)
- Parsing de `/status` (Codex: `XX% left`) y `/usage` (Claude: `XX% used` → invertido)
- Cache en memoria con refresh cada 5 min + persistencia en MongoDB `agent_status`
- Pre-launch gating en `task_service.py`: tickets pasan a `queued` con `resume_at` si no hay cuota
- Post-completion hook: refresh de límites + lanzamiento automático de tickets en cola
- Scheduler retries periódicos de tickets en cola via `resume_queued_tickets()`
- Mid-task detection: polling loop en `tmux_runtime.py` detecta rate limit y ejecuta wait-and-resume
  - Claude: selecciona opción "Stop and wait", duerme hasta reset + 2 min, envía prompt de continuación
  - Codex: duerme hasta reset + 2 min, presiona Enter para reanudar
  - Recursivo: si vuelve a pegar límite post-resume, repite el ciclo
- Crash recovery: detecta bash prompt (proceso murió), usa `codex resume {session_id}` o relanza CLI
- Concurrencia: máx 3 slots (Tier S = 3 slots, normal = 1 slot)
- Reserves: Claude 0%/0% (OpenCEO lo consume todo), Codex 20%/5% (reserva para uso manual)
- Configuración: 8 variables `RATE_*` en `.env`

### Finance

- Workflow operativo: `Pago Elfec House` / `Pago Luz Casa`
- Usa Playwright determinista
- Entra al portal ELFEC, selecciona la factura más antigua, llega a la pantalla de tarjeta y se detiene
- No confirma el pago ni persiste CVV
- Secretos en `openceo-core/.env.finance.local`

### Secretary

- Envío de correo operativo vía SMTP
- Wrapper compartido: `openceo-core/tools/email_sender.py`
- Soporta remitente por cuenta, `language=es|en`, adjuntos y firma institucional

## Componentes críticos

- `openceo-core/main.py`: routing OpenWebUI-compatible + comandos naturales de sesión dev
- `openceo-core/dev_session_service.py`: ciclo de vida tmux, background execution y Tier S
- `openceo-core/db/mongo.py`: proyecto seleccionado por usuario + foco de sesión activa
- `openceo-core/agents/dev/tmux_runtime.py`: runtime tmux persistente
- `openceo-core/agents/dev/consensus.py`: orquestación Tier S
- `openceo-core/agents/dev/opencode_run.py`: helpers para `opencode run --format json`, extracción de `sessionID` y persistencia de contexto OpenCode
- `openceo-core/agents/dev/opencode_session_manager.py`: creación de sesiones OpenCode para revisión QA con deep-link directo a OpenCode Web vía URL
- `openceo-core/agents/dev/kimi_session_manager.py`: creación de sesiones Kimi CLI programáticas para QA review
- `openceo-core/qa_review_service.py`: gate de revisión QA, construcción de contexto rico (AGENTS.md + git branch/PR + prompt original), sesiones Kimi y OpenCode
- `openceo-core/agents/finance/elfec.py`: flujo Playwright de finanzas
- `openceo-core/agents/pm/session.py`: extracción de tickets con DeepSeek + Notion batch
- `openceo-core/tools/email_sender.py`: sender SMTP compartido
- `openceo-core/tools/notion.py`: creación de tickets + sync-back bidireccional a Notion
- `openceo-core/task_service.py`: orquestación de tasks con idempotencia, sync-back y rate limit gating
- `openceo-core/db/mongo.py`: tasks, foco de sesión dev, `pm_state` y colección `task_reviews`
- `openceo-core/rate_monitor.py`: tracking de límites, gating pre-launch, detección mid-task, crash recovery
- `openceo-core/mcp_server.py`: MCP interno owner-curated
- `apps-sdk-mcp-test/`: conector MCP para ChatGPT Apps SDK con OAuth embebido opcional + tools de orquestación (`get_company_context`, `get_user_context`, `execute_workflow`, `get_task_status`)
- `openceo-voice/`: bridge LiveKit

## Operación

- Host-mode: `scripts/openceo-core-host.sh`, puerto default `18765`
- OpenWebUI recomendado: `http://172.20.0.1:18765/v1` + `OPENCEO_API_KEY`
- Salidas de chat/voz pasan por sanitización común: tono natural y sin UUIDs/URLs salvo que hagan falta

## openceo-react — Dashboard React

### Ubicación
`/home/develop/development/Jardal/jardal-openceo/openceo-react/`

### Stack
- React 19 + Vite 6, Tailwind CSS v4, GSAP + @gsap/react, @headlessui/react, @heroicons/react, Zustand, React Router v7

### Funcionalidades implementadas
- **Login**: JWT con cookies httpOnly (`openceo_session`) + token decoy `heaven` en localStorage. Usuario por defecto: `admin` / `admin1234`
- **Permisos en 3 niveles**:
  - Nivel 1 (rutas): `ProtectedRoute` con check de auth + permisos
  - Nivel 2 (menú): `Sidebar` filtra items según `user.all_permissions`
  - Nivel 3 (botones): `PermissionCheck` wrapper con modo AND/OR
- **Dashboard**: Mission Control con fila de 6 métricas operativas en tiempo real (Total, En Progreso, En Espera/Revisión, En Cola, Completadas, Fallidos) + tablero Kanban interactivo con 3 modos de agrupación (Por Estado, Por Agente, Por Flujo), filtros combinados y ejecución directa.
- **Global project selector**: en header del dashboard, almacenado en `StoreContext` + `localStorage`; auto-preselecciona si el usuario solo tiene 1 proyecto
- **TicketList**: vista dual con switch dinámico (Tabla / Kanban) persistido en `localStorage`, filtros por agente, estado, proyecto y búsqueda en tiempo real, tabla paginada con GSAP animations, bulk apply de workflow templates a tickets visibles
- **KanbanBoard**: componente modular (`src/components/KanbanBoard/index.jsx`) con diseño Obsidian dark, columnas dinámicas, scroll horizontal optimizado, badges visuales por agente (`dev`, `finance`, `secretary`, `pm`, `ceo`) y workflow, modal/botón de ejecución de tareas y vista alternativa de tabla compacta
- **TicketDetail**: descripción, evidencia, URL prueba, historial reviews, fases de workflow (`phase`, `workflow_phases`), botones de aprobación/rechazo/cambios por fase
- **ClientReview**: vista simplificada para clientes (`is_client=true`): solo ve tickets `client_visible=true` con status_display "En proceso"/"Entregado"; puede aprobar o solicitar cambios
- **QADashboard**: filtra `phase=qa_review,status=waiting`; permite aprobar, pedir cambios, o abrir sesión Kimi para review
- **Admin Users**: CRUD usuarios con flag `is_client`, asignación de grupos y proyectos permitidos
- **Admin Groups**: CRUD grupos con permisos editables via checkboxes; eliminación protegida con SweetAlert confirm + manejo de errores del backend
- **Admin Projects**: CRUD proyectos con editor JSON de `workflow_templates`
- **SweetAlert2**: tema dark integrado (`main.jsx` config global); helpers reutilizables `swalConfirm` y `swalError` para confirmaciones y errores de API; reemplaza todos los `alert()`/`confirm()` nativos

### Endpoints backend usados
- `POST /api/auth/login`, `/api/auth/logout`, `/api/auth/refresh`, `/GET /api/auth/me`
- `GET/POST/PATCH/DELETE /api/users`, `GET/POST/PATCH/DELETE /api/groups`
- `GET /api/tasks`, `POST /api/tasks/search` (body filters: project, status, phase, workflow_key, client_visible)
- `GET/PATCH /api/tasks/{task_id}`, `GET/POST /api/tasks/{task_id}/reviews`
- `POST /api/tasks/{task_id}/workflow`, `POST /api/tasks/bulk-apply-workflow`, `POST /api/tasks/{task_id}/phase-action`, `POST /api/tasks/{task_id}/phase-kimi`
- `GET/PUT/DELETE /api/projects/{project}/workflow-templates/{key}`
- `GET /api/workflows`, `GET /api/workflow-runs`, `GET /api/discovery-runs`

### Permisos definidos
`read_tickets`, `write_tickets`, `approve_tickets`, `reject_tickets`, `review_tickets`, `qa_review`, `manage_users`, `manage_groups`, `manage_projects`, `admin_dashboard`, `client_review`

### Desarrollo
- **Docker production**: `openceo-react/docker-compose.yml` → nginx:alpine con dist/ estático
- **Backend Docker**: `openceo-core/docker-compose.yml` → python:3.11-slim
- **Rutas en production**:
  - `dev.openceo.jardal.app` → frontend (nginx + SPA)
  - `api.openceo.jardal.app` → backend (openceo-core:8000)
- **CORS**: configurado en nginx para `api.openceo.jardal.app` con credenciales

## Proyectos configurados

- `jardal-openceo`: `/home/develop/development/Jardal/jardal-openceo` -> `codex`
- `jardal-trading`: `/home/develop/development/Jardal/jardal-trading` -> `tier_s`
- `jardal-vpn`: `/home/develop/development/Jardal/jardal-vpn` -> `claude`
- `cebitec`: `/home/develop/development/DigitalInnovations/CEBITEC` -> `codex`
- `melanyryr`: `/home/develop/development/Jardal/melanyryr` -> `codex`

## Validación mínima

1. Compilación Python: `python3 -m py_compile openceo-core/main.py openceo-core/dev_session_service.py openceo-core/db/mongo.py`
2. Levantar servicio: Docker `docker compose up -d --build openceo-core` o host `scripts/openceo-core-host.sh restart`
3. Smoke OpenWebUI-compatible: `crea una sesión dev` -> instrucción -> `que respondio?` -> `sal de la sesión` -> `que sesiones hay activas?` -> `reestablece la ultima conversacion` -> `cerrar sesión dev`

## Pendientes

- streaming real incremental (`stdout` -> SSE por chunks)
- discovery ReAct peer-to-peer completo con claim cross-agent y creación asistida de tools faltantes
- más agentes no-dev realmente ejecutables
- Worker PC dedicado para browser tasks
- TTS definitivo end-to-end en voz
- n8n workflow Notion webhook → `POST /api/tasks` (formato documentado, aún no configurado)
- Notion `Sync Enabled` checkbox: existe en DB, no chequeado por sync-back aún
- UI/dashboard de revisión humana sobre `task_reviews` ✅ implementada en `openceo-react/`
- Integrar Workflows YAML con Phase Workflows (TICKET-018 en `PENDINGS.md`): permitir `human_in_the_loop` dentro de YAMLs descubiertos, unificar estado entre `workflow_runs` y `task.phases`
- Deploy automático a producción como fase post-cliente (`deploy_prod`) en workflow templates

## openceo-react — Dashboard React (Producción)

### URLs producción (2026-04-19)
- **Frontend**: https://dev.openceo.jardal.app
- **Backend**: https://api.openceo.jardal.app
- **Admin default**: `admin` / `admin1234`

### Diseño
- Diseño copiado de FugoVPN (jardal-vpn_react): fondo obsidiana #0a0a0a, acento lima #ccff00, tipografía Orbitron/Inter/JetBrains Mono
- Login: split hero + form card con animaciones GSAP
- SideMenu: colapsable con hover, drawer mobile, permisos 3 niveles
- Layout: Dashboard4 con topbar, backdrop blur, gradiente bordes
- Stack: React 19.2 + Vite 7 + Tailwind v4 + GSAP + Framer Motion + HeadlessUI + Heroicons

### Docker deployment
- `openceo-react/docker-compose.yml` → `node:22-alpine` con Vite dev server
- `openceo-core/docker-compose.yml` → Dockerfile Python build
- Ambas containers en `traefik-net`, SSL/TLS con Let's Encrypt (`jardal-resolver`)
- Static routes en Traefik via `/home/develop/deployments/jardal-traefik/dynamic.yml`
- `ENV=production` en openceo-core para cookies seguros (secure=true)

###HTTPS/SSL
- DNS Cloudflare: `dev.openceo.jardal.app` y `api.openceo.jardal.app` en "DNS only" (grey cloud)
- Let's Encrypt TLS challenge funciona después de deshabilitar proxy Cloudflare
- Certificados generados via `jardal-resolver` en Traefik

## Roadmap

- Phase 3: Sales + Secretary + Support (workflow templates, shared tools)
- Phase 4: Marketing + Finance
- Phase 5: HR + Legal + PM expansions
- Phase 6: Media Agent + Worker PC Agent (remote browser bridge)
- Phase 7: Dashboard + User Management + Keyword Suggestions
- Phase 8: Telegram + WhatsApp routing
- Phase 9: Voice peer programming (LiveKit Agents + Silero VAD + Groq Whisper + Android app)
- Phase 10: MCP server para coding agents dentro de `openceo-core`
- Phase 11: Open source release de OpenCEO Core + Agent Marketplace

## Backlog movido desde README

- Access control en MongoDB con empleados, roles reutilizables y filtros por proyecto
- Vistas de dashboard: overview de agentes, task board, project selector y analítica
- Contratos de comandos Telegram para owner/employee + notificaciones outbound
