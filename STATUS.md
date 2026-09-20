# OpenCEO Core

Resumen operativo corto del estado real del repo.

## Continuar aquí (traspaso a la próxima sesión)

Estado al cerrar la sesión del **2026-09-20 (segunda sesión del día)**: Horizon **HZ-003 (delegación y dev móvil) implementada, probada y desplegada**. El servicio corre con el código nuevo (reiniciado y verificado: 16 rutas `/api/horizon/*`). Detalle de la implementación en la entrada "Estado actual (2026-09-20) — HZ-003" de abajo.

**Ojo con el working tree:** conviven **sin commitear a propósito** los streams ajenos a Horizon — media/transcribe (`agents/media/`, `tools/media_transcribe.py`, `scripts/transcribe_links.sh`, `.qwen/`), voz Android (`openceo-voice/`, `openceo-voice-android/`), fix de lock por proyecto en `dev_session_service.py` (documentado en la entrada del 2026-09-13) y `params.coding_tool` en `agents/dev/session.py`. No mezclarlos con commits de Horizon.

**Lo siguiente, por orden de valor:**

1. **Piloto real de delegación (3 tickets)** — spec §9.6 y §9.8. HZ-003 está implementado pero falta el piloto con tickets reales y acotados que mida tiempo humano de encargo, supervisión y retrabajo. Es decisión del dueño dispararlo: *"delega la siguiente acción"* por voz/chat o `POST /api/horizon/delegate`. Las métricas se anotan diciendo *"anota 15 minutos de supervisión"* y el resumen sale solo de `GET /api/horizon/delegation/summary`.
2. **El canal de entrega del plan** (hueco conocido de HZ-002). El plan se persiste y se expone, pero no hay push: la fila de la sección 9.10 *"llega la hora diaria y no has abierto el chat"* se cumple solo a medias. El único canal saliente durable que funciona es `secretary.send_email` (`openceo-core/tools/email_sender.py`). Detalle y tabla de canales en `PENDINGS.md` TICKET-026.
3. **HZ-004 — Fuentes conectadas y vista del espacio personal** en `openceo-react`. El contrato ya existe: `GET /api/horizon/plan/today`, `GET /api/horizon/triggers` y, desde HZ-003, `GET /api/horizon/delivery` y `GET /api/horizon/delegation/summary`.

**Cosas que harán perder tiempo si no se saben:**

- **Los tests necesitan el venv del root**: `./.venv/bin/python -m pytest tests/ -q`. `python3` pelado **no** sirve (le faltan fastapi y motor; da 8 errores de colección). Línea base al cerrar: **224 pasan / 7 fallan**; los 7 son preexistentes y están diagnosticados abajo. Los tests de Horizon solos: **151 pasan**.
- **Trampa de `lifespan` en `main.py`**: `settings` se asigna dentro de la función (bloque de auth), así que es local a toda ella. Leer `settings.X` **antes** de esa asignación lanza `UnboundLocalError` y tumba el arranque — ya pasó una vez en esta sesión y `py_compile` no lo detecta. En ese bloque se usa `get_settings()` directamente.
- **Escribir en `horizon_triggers` a mano no despierta el timer**, por diseño: no hay sondeo, así que solo lo despierta `enqueue()`. Un encolado externo espera al siguiente vencimiento o hasta `max_sleep` (1 h). Los caminos reales del código pasan todos por `enqueue()`.
- **El motor YAML rechaza `llm_eval`** (`workflow_service.py`): cualquier `when` o `loop.condition` debe ser `python_eval`. Y `_resolve_value` convierte un parámetro ausente en `""`, no en `None` — leerlo como cero convertiría "no lo sé" en "día saturado".
- **El dueño del espacio personal es único**: `HORIZON_OWNER_USER_ID` (`settings.py`), hoy `usr_8c9941ae0bc9` (admin). `HorizonService.owner_for()` es el único punto de resolución: la API usa el usuario autenticado, voz y chat el dueño configurado. No añadir otro camino.
- **No hay que tocar los 7 tests que fallan**: no son de Horizon y su causa está diagnosticada abajo.

**Cómo comprobar que sigue vivo:**

```bash
./scripts/openceo-core-host.sh status          # servicio arriba
./.venv/bin/python -m pytest tests/test_horizon_*.py -q   # 163 tests (151 + 12 de delegación)
./scripts/chat.sh "hoy solo tengo 15 minutos"  # replanifica por capacidad
./scripts/chat.sh "qué sigue"                  # acción principal + objetivo
./scripts/chat.sh "cómo va lo delegado"        # entrega única (o "no hay pendientes")
curl -s localhost:18765/openapi.json | grep -c '/horizon/'   # 16 rutas
```

## Estado actual (2026-09-20) — HZ-003

- `Horizon HZ-003 — Delegación y dev móvil (Fase 3 de 5)`: implementado el tercer entregable de [`jardal-horizon/README.md`](file:///home/develop/development/Jardal/jardal-openceo/jardal-horizon/README.md) §9.8 — *"Un ticket autorizado se ejecuta desde voz/chat hasta evidencia de resultado; el teléfono puede desconectarse, el trabajo sigue y el resultado se entrega una sola vez"*.
  - **Qué se implementó** (el circuito completo):
    1. [`openceo-core/horizon/service.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/service.py): `delegate_action()` encarga la acción (la siguiente por prioridad o una específica) como ticket canónico con `agent_hint=dev`, `autorun=true` y alcance personal en `params` — reutiliza la cola de tareas y las sesiones dev existentes, no crea un runtime paralelo. `deliver_action_result()` implementa **entrega única** (`delivered_at` persistido: una reconexión móvil que re-consulta recibe el mismo resultado marcado `already_delivered`, nunca un segundo efecto). `record_delegation_metrics()` acumula minutos humanos de supervisión y retrabajo (§9.6 *"medir la promesa"*), y `get_delegation_summary()` agrega delegación efectiva.
    2. **Cierre reactivo del ciclo**: el puente `TaskService._notify_horizon_terminal` → `HorizonService.acknowledge_task_result` (commiteado en HZ-002) ahora captura evidencia — `last_response_summary` → `result_summary`, y `review_links`/`test_url`/`client_review_url` → `evidence_links` — y deja la acción en espera de **entrega humana** (`delivered_at: null`): el agente terminó no significa que el dueño ya lo vio.
    3. **Idempotencia y desconexión móvil**: re-delegar una acción `in_progress` devuelve la misma tarea sin crear otra (`already_in_progress`), y el ticket lleva `dedupe_key` en `params`. La escritura del resultado se resuelve por `task_id` + estado, no por cercanía en el chat.
    4. **5 endpoints nuevos**: `POST /api/horizon/delegate`, `POST /api/horizon/actions/{id}/delegate`, `GET /api/horizon/delivery`, `POST /api/horizon/actions/{id}/metrics`, `GET /api/horizon/delegation/summary`. Total: 16 rutas `/api/horizon/*`.
    5. **3 intents nuevos** en `first_layer.py` y `classify.py`: `horizon_delegate`, `horizon_delivery`, `horizon_metrics`, con el clasificador determinista corriendo **antes** del modelo (captura con `parse_metrics_input` extrae minutos de supervisión/retrabajo sin LLM). Un test exige que frases dev (*"crea una sesión dev"*, *"corre el preset review"*) **no** sean robadas por los intents nuevos.
  - **Decisiones de diseño relevantes**:
    - **La delegación no ejecuta dinero ni mensajes externos**: el ticket nace con alcance personal (`privacy.personal_scope_params`), por lo que queda fuera del espejo de Notion por las dos rutas ya filtradas (guard reactivo + safety net de arranque).
    - **"Hecho" del agente ≠ "entregado" al dueño**: `acknowledge_task_result` marca la acción `done` con evidencia, pero la entrega es un evento aparte (`deliver_action_result`) — la fila de §9.10 *"responde tarde 'hecho'"* se resuelve contra el aviso, no contra la acción vigente.
    - **Fix de HEAD roto incluido**: `d628a2d` referencia `WorkflowRunStatus.BLOCKED_HUMAN` desde `workflow_service.py` (3 sitios) pero el enum en `state.py` quedó sin commitear — un checkout limpio de HEAD rompía con `AttributeError`. La línea entra en este commit.
  - **Tests**: `tests/test_horizon_delegation.py` — 12 tests + 19 subtests (delegación, idempotencia por desconexión, entrega única, métricas humanas, agregación del resumen, clasificador determinista). Suite de Horizon: **163 pasan**. Suite completa: **236 pasan / 7 fallan** (los mismos 7 preexistentes diagnosticados).
  - **Pendiente de esta iniciativa**: el **piloto con 3 tickets reales** (§9.6) que mida tiempo humano — es decisión del dueño disparar cada encargo. Después: canal de entrega push (TICKET-026) y HZ-004 (vista del espacio personal en `openceo-react`).

## Estado actual (2026-09-20, primera sesión)

- `Horizon HZ-002 — Ciclo proactivo del CEO (Fase 2 de 5)`: implementada la fase que la especificación reclamaba en [`jardal-horizon/README.md`](file:///home/develop/development/Jardal/jardal-openceo/jardal-horizon/README.md) §9.2 punto 1 — *"Un prompt por sí solo no mantiene objetivos ni despierta por la mañana"*. Hasta ahora Horizon era **reactivo**: sólo respondía cuando el usuario hablaba.
  - **Qué se implementó**:
    1. [`openceo-core/horizon/capacity.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/capacity.py): capacidad diaria determinista. Regla del 70% (sección 9.5) con la agenda de ejercicio de la sección 10.1 (115 min/semana) reservada **antes** del crecimiento, y `floor_to_step(…, 5)` que reproduce literalmente la tabla de la sección 10.2 (60 min → 40, 120 → 80, 15 → 10).
    2. [`openceo-core/horizon/planner.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/planner.py): los ocho pasos de la sección 9.5 con **comparación lexicográfica**, no una suma ponderada — la sección prohíbe fabricar un retorno por hora para actividades cuyo efecto no se conoce. Una acción principal y hasta dos secundarias.
    3. [`openceo-core/horizon/triggers.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/triggers.py): trabajo intencional persistido y **un solo timer** que duerme hasta el próximo vencimiento. No hay barrido (AGENTS.md §5.1).
    4. [`openceo-core/horizon/review.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon/review.py): revisión periódica que separa lo que el sistema ajusta solo de lo que exige decisión del usuario, con la revisión mensual como la misma función con otra ventana.
    5. Dos colecciones nuevas (`horizon_plans`, `horizon_triggers`) y cinco endpoints nuevos (`GET /plan/today`, `POST /plan/day`, `GET /triggers`, `POST /review`, y `POST /progress` acepta `aviso_id`).
    6. Tools `ceo.plan_day`, `ceo.review_objectives`, `ceo.get_next_action`, `ceo.capture_context` y `ceo.record_progress` en `WorkflowToolRegistry`, más [`workflows/ceo/horizon_daily.yaml`](file:///home/develop/development/Jardal/jardal-openceo/workflows/ceo/horizon_daily.yaml) y `horizon_review.yaml`.
    7. Ocho intents nuevos en `first_layer.py` (`horizon_capacity`, `horizon_plan_day`, `horizon_review`, `horizon_import`), con el clasificador determinista corriendo **antes** del modelo.
  - **Decisiones de diseño relevantes**:
    - **Sin barrido, y comprobado.** El timer pide el próximo vencimiento, duerme exactamente ese tiempo, y lo despierta cualquier encolado. Un test cuenta las consultas con el sistema ocioso y exige que no haya bucle: un test de resultado habría seguido pasando con un sondeo cada segundo.
    - **Desconocido no es cero, también en la capacidad.** Sin horas libres declaradas el presupuesto es `None` y el plan queda en `needs_input` con la acción a demanda y sin reloj, en vez de inventar una hora (sección 9.5). Cero es un valor medido y significa "día saturado".
    - **Una sola versión vigente por usuario, garantizada por índice.** Índice único parcial sobre `is_current` **más** índice único `(user_id, version)`: el primero impide dos planes vigentes, el segundo impide que dos escritores acuñen la misma versión. Ninguno basta solo. El lector cae a la versión más alta si el proceso muere en la ventana entre retirar el vigente y marcar el nuevo.
    - **El escritor perdedor de una carrera no aplica efectos.** Los efectos derivados (corregir estimaciones, mover acciones a espera) viajan aparte y sólo se aplican si la publicación ganó. Sin esto, el perdedor mutaría acciones para un plan que nunca llegó a vigente.
    - **El propietario del espacio personal se unifica, y eso arregla un fallo silencioso de la fase 1.** El chat resolvía `user_id = payload.user or "openwebui"` (`main.py:1355`) mientras el dashboard usa `Depends(get_current_user)`: **identidades distintas**. Una captura por voz se guardaba bajo un nombre que el dashboard nunca consulta, así que el espacio personal parecía vacío sin que nada fallara. Ahora `HorizonService.owner_for()` es el único punto de resolución y todo converge en `HORIZON_OWNER_USER_ID`. No hubo nada que migrar: `horizon_objectives` y `horizon_captures` estaban vacíos.
    - **"Hecho" tardío se resuelve contra el aviso, no contra la acción vigente** (sección 9.3 y fila correspondiente de 9.10). Cada aviso guarda qué acción se mostró. Un aviso sin resolver mayor de 24 h no puede tragarse una confirmación de hoy: si existe una acción en curso, gana esa. Es la única heurística de la escalera y está acotada y documentada.
    - **Tres bloques perdidos por la misma causa cambian la estimación en vez de repetir el intento** (secciones 9.5 y 9.10). Exigió persistir `lost_blocks` y `lost_reason` en la acción: sin ese contador la regla era inaplicable porque la memoria del fracaso no vivía en ningún sitio.
    - **Un vencimiento externo no se mueve nunca** desde el planificador. Es una restricción dura, con test, no un comentario.
    - **Dos flags muertos quedaron conectados.** `HORIZON_ENABLED` gobierna el arranque del scheduler y `HORIZON_PERSONAL_MIRROR_ENABLED` alimenta el mandato (antes estaba forzado a `False` en `mandate.py` pese al comentario que decía "salvo habilitación explícita del propietario").
  - **Modo degradado por construcción**: el ciclo de planificación, cálculo y revisión **no llama a ningún LLM**. Un test lo comprueba inspeccionando los imports reales de los cuatro módulos con `ast` (no el texto: los docstrings hablan de "sin LLM" a propósito y darían un falso positivo).
  - **Tests**: 4 archivos nuevos (`test_horizon_{capacity,plan,triggers,workflow}.py`) — **73 tests nuevos**. La suite de Horizon pasa de 75 a **148**, y la suite completa queda en **221 pasan / 7 fallan**.
  - **Corrección de un diagnóstico anterior de este mismo archivo**: los 7 fallos preexistentes **no** vienen del `autorun=False` sin commitear, como decía la entrada de 2026-09-19. Verificado ejecutando la suite: sólo **1 de los 7** se relaciona con `autorun`, y su punto de llamada (`main.py:4283`) está **sin cambios respecto a HEAD**, así que fallaría igual en un HEAD limpio. Las causas reales son dos `RuntimeError: MongoRepository not connected`, dos `TypeError: ... unexpected keyword argument 'notion_manager'` (`agents/pm/session.py:157,273`), un `TaskStatus.FAILED != COMPLETED`, y un test que espera la etiqueta en inglés `"Module: Ventas / Inventario"` mientras el prompt ya emite `"Módulo: ..."`. **No se tocaron**: no son regresiones de esta fase.
  - **Hueco pendiente y conocido — el canal de entrega**: el plan se **persiste y se expone** (API, voz/chat, dashboard); no hay push. La fila de la sección 9.10 "llega la hora diaria y no has abierto el chat" se cumple a medias: el disparador **prepara y deja listo** el plan, y el usuario lo recibe en su siguiente interacción. Es una decisión explícita del propietario, no un olvido. El único canal saliente durable que funciona hoy es `secretary.send_email` (SMTP, `openceo-core/tools/email_sender.py`); Telegram es un stub sin implementar (`integrations/telegram_bot.py`), `RealtimeHub` es efímero y la voz exige sala abierta. Registrado en `PENDINGS.md` TICKET-026.
  - **Pendiente de esta iniciativa**: HZ-003 (delegación y dev móvil, que es el punto 6 del criterio de aceptación) y HZ-004 (fuentes conectadas y vista del espacio personal en `openceo-react`). TICKET-014 y TICKET-015 quedan **formalmente absorbidos** por este motor: no se crea una colección `objectives` paralela ni un cron de barrido.

## Estado actual (2026-09-19)

- `Horizon HZ-001 — Contexto personal del CEO (Fase 1 de 5)`: implementado el primer entregable de [`jardal-horizon/README.md`](file:///home/develop/development/Jardal/jardal-openceo/jardal-horizon/README.md) v2.1 como **módulo del CEO**, no como agente nuevo (sección 9.2 y decisión D-006).
  - **Qué se implementó**:
    1. Paquete nuevo [`openceo-core/horizon/`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/horizon): `models.py` (jerarquía misión → objetivo → hito → acción → resultado), `mandate.py` (límites de la sección 9.9 con techos de política), `privacy.py` (gate de alcance personal), `classify.py` (clasificador determinista previo al LLM), `importer.py` (importación idempotente desde la especificación) y `service.py` (captura, progreso, siguiente acción, resumen de plan).
    2. Cinco colecciones propias en [`db/mongo.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/db/mongo.py) (`horizon_profile`, `horizon_objectives`, `horizon_captures`, `horizon_actions`, `horizon_events`) con índices por propietario, clave natural de upsert y **clave única de deduplicación** de capturas y eventos.
    3. Capacidades en [`first_layer.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/first_layer.py): intents `horizon_capture | horizon_next_action | horizon_progress | horizon_plan_query`, con **clasificador determinista antes del modelo**.
    4. Endpoints `/api/horizon/{profile,objectives,plan,next-action,capture,progress,import}` en [`main.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/main.py), todos filtrados por el usuario autenticado.
    5. Ajustes `HORIZON_*` en [`settings.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/settings.py) (zona horaria `America/La_Paz`, fracción de capacidad 0.70, espejo personal deshabilitado).
  - **Privacidad (dos rutas, ambas filtradas)**: el alcance personal se excluye tanto en el guard reactivo [`TaskService._task_needs_notion_mirror`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/task_service.py) como en el safety net de arranque [`MongoRepository.list_tasks_missing_notion_mirror`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/db/mongo.py). Filtrar solo el primero habría publicado datos personales en Notion en el primer reinicio.
  - **Decisiones de diseño relevantes**:
    - El clasificador de Horizon corre **antes** de la llamada al LLM: con una sesión dev activa, una nota personal se clasificaba como `dev_instruction` y se escribía en tmux (riesgo explícito de la sección 9.2). Además, permite que captura, cálculos y consulta sigan funcionando con el presupuesto de IA agotado (sección 9.7).
    - "Minutos grabados" (18 declarados) y "minutos de audio útil" (`null` en la especificación) se mantienen como medidas distintas: el hito de voz arranca **desconocido**, nunca en 18, para no inflar el progreso hacia la meta de 180.
    - Un dato desconocido se guarda como `null` con incertidumbre `unknown`, **nunca como cero**.
    - Los límites de seguridad se **recortan** al importar: una reimportación del README solo puede ser más conservadora que la política vigente, nunca más permisiva (capacidad 0.70, 1 objetivo de crecimiento activo, 6 acciones listas, 1 en curso; espejo externo y ejecución de dinero siempre deshabilitados).
    - Los objetivos **no son tickets**: solo la acción enlaza un `task_id` canónico, así un compromiso personal puede no tener repositorio.
  - **Tests**: 4 archivos nuevos (`test_horizon_{classify,privacy,importer,capture}.py`) + soporte en [`tests/horizon_support.py`](file:///home/develop/development/Jardal/jardal-openceo/tests/horizon_support.py) — **75 tests y 35 subtests, todos en verde**. La suite completa queda en **148 pasan / 7 fallan**; los 7 son **preexistentes a esta sesión** y provienen del `+ autorun=False` que está sin commitear en `workflow_service.py` (flujo PM), que cambió la firma que `test_pm_reliability`, `test_routing_config` y `test_dev_runtime_cleanup` esperan. No son regresiones de Horizon y no se tocaron.
  - **Hueco de uso detectado**: importar el contexto una vez exige `POST /api/horizon/import` con sesión autenticada (`get_current_user` lee la cookie `openceo_session` y el `admin/admin1234` por defecto ya no sirve), así que hoy no se puede disparar desde voz o chat. Sin ese intent, el espacio personal arranca vacío. Registrado en `PENDINGS.md` TICKET-026.
  - **Pendiente de esta iniciativa (fases 2 a 5)**: `ceo.plan_day` / `ceo.review_objectives`, planificador de capacidad y disparadores persistidos sin barridos (HZ-002); delegación y dev móvil sobre las sesiones existentes (HZ-003); frontera financiera (sección 4.7), que **ya está decidida como ERPNext en `PENDINGS.md` TICKET-025** y que Horizon debe consumir en vez de construir registro propio; y fuentes conectadas + vista del espacio personal (HZ-004).
  - **Solapamiento a resolver antes de la fase 2**: `README.md` → `## Proactive CEO — Autonomous Objectives` y `PENDINGS.md` TICKET-014/015 describen un motor de objetivos plano. Debe unificarse sobre este motor (**un motor, dos alcances**): crear una colección `objectives` aparte violaría D-006, y el cron "cada minuto" de TICKET-015 viola AGENTS.md 5.1.

## Estado actual (2026-09-13)

- `Manejo de Proyectos y Presets por Número en project.yaml (Sin order.yaml)`:
  - **Requerimiento**: No utilizar un archivo separado `order.yaml` por ser demasiado específico. Unificar la numeración directamente dentro del `project.yaml` de cada proyecto (tanto para el número de proyecto como para los presets/procesos), permitiendo que al invocar un preset por número o nombre se cree automáticamente la sesión dev en tmux, se escriba el preprompt inicial en pantalla sin dar Enter y se quede esperando el dictado del usuario.
  - **Implementación**:
    1. Eliminado `order.yaml`: el orden numérico se define directamente con `number: N` en cada [`projects/<proyecto>/project.yaml`](file:///home/develop/development/Jardal/jardal-openceo/projects/jardal-openceo/project.yaml) (`jardal-openceo: 1`, `jardal-trading: 2`, `jardal-vpn: 3`, `melanyryr: 4`, `cebitec: 5`).
    2. En [`ConfigLoader.available_projects()`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/config_loader.py): ordenamiento dinámico leyendo el campo `number` / `order` de cada `project.yaml`.
    3. En [`ProjectService.get_project_presets()`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/project_service.py): los presets se indexan simultáneamente por su nombre (ej: `review`) y por su número (ej: `1`), leyendo `number: N` del `project.yaml`.
    4. En [`first_layer.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/first_layer.py):
       - Añadido `_resolve_preset_from_query()` para resolver presets por número (*"preset 1"*, *"el primero"*, *"1"*) o por nombre (*"review"*, *"test_fix"*).
       - En `preset_load` / `preset_run`: si la sesión dev en tmux no existe, la crea automáticamente en segundo plano, escribe el preprompt inicial en el prompt line de tmux sin dar Enter (`enter=False`), y queda en espera activa de dictado adicional o de la confirmación *"dale enter"*.
       - Actualizado `preset_list` para listar los presets con su número y nombre (ej: *"1 review, 2 test_fix, 3 refactor..."*).
    5. Validado end-to-end con `./scripts/chat.sh`: *"que presets tenemos?"* lista los presets numerados; *"pon el preset 1"* crea la sesión tmux, escribe el preprompt en pantalla de Codex, agrega texto dictado adicional y se cancela/limpia limpiamente vía `C-u`.

- `OpenCEO Voice & Dev Sessions - Corrección de Doble Sesión Tmux y Endpointing de Voz`:
  - **Causa raíz del bug**:
    1. *Clasificación ambigua en First Layer Gateway*: Al pedir *"cree una sesion en jardal-trading"*, el prompt anterior inducía a DeepSeek a clasificar la intención como `tmux_create` en lugar de `dev_session_start`. Además, `tmux_create` abría una terminal bash vacía sin resolver el directorio del proyecto (`cwd=None`), dejando un tmux huérfano.
    2. *Fallback de proyecto*: Si DeepSeek devolvía `project: null`, el gateway caía al proyecto activo (`selected_project=jardal-openceo`), abriendo la sesión dev en el proyecto equivocado.
    3. *Endpointing prematuro en LiveKit Voice (`min_endpointing_delay: 0.2s`)*: Pausas normales al hablar en español cortaban el turno antes de tiempo, generando dos turnos consecutivos ante una misma orden. A su vez, el timeout por defecto de 10s del cliente LLM en LiveKit disparaba reintentos de turno mientras Codex inicializaba.
    4. *Condición de carrera en `DevSessionService`*: `create_session()` no adquiría el lock por proyecto (`self._project_locks[project]`), permitiendo inserciones concurrentes de documentos antes de finalizar la espera en `wait_until_ready()`.
  - **Solución implementada**:
    1. En `openceo-core/first_layer.py`: añadido `_extract_project_from_text()` con fuzzy match (ej: "trading" -> "jardal-trading"); ajustado el system prompt para que cualquier petición de abrir sesión en un proyecto sea estrictamente `dev_session_start`; añadido interceptor de seguridad que redirige `tmux_create` a `dev_session_start` si se menciona un proyecto; y reutilización limpia de sesión activa existente sincronizando `select_project()`.
    2. En `openceo-core/dev_session_service.py`: protegido `create_session()` bajo `async with self._project_locks[project]:` evitando duplicación concurrente.
    3. En `openceo-voice/agent.py` y `.env`: ajustados los delays de endpointing de voz a valores robustos (`min_endpointing_delay: 0.6s`, `max_endpointing_delay: 1.5s`) y timeout de LLM a 45s para soportar inicializaciones de providers.
    4. Limpieza completa de sesiones huérfanas en tmux y MongoDB, recompilación y reinicio exitoso de `openceo-core` (puerto 18765) y contenedor `openceo-voice-agent`.

- `OpenCEO Voice Bridge - STT, LLM y EdgeTTS Operativos al 100%`:
  - **Causa raíz del cuelgue inicial (5 puntos parpadeando / THINKING)**:
    1. La `GROQ_API_KEY` en `openceo-voice/.env` arrojaba error 401 (clave revocada/inválida), impidiendo que Whisper transcribiera el audio.
    2. Al intentar procesar la respuesta, LiveKit requería un sintetizador de voz (TTS) para emitir el audio de vuelta. Al estar `VOICE_TTS_API_KEY` vacía, `tts=None` disparaba una excepción interna en el pipeline de agentes de LiveKit.
  - **Soluciones implementadas**:
    1. Se actualizó la clave de Groq válida provista por el usuario, verificando respuesta HTTP 200 en `models` de Groq.
    2. Se integró `EdgeTTS` directamente en `openceo-voice/agent.py` utilizando la voz neural en español de Microsoft (`es-ES-AlvaroNeural`), decodificada y streameada como MP3 nativo sin costo ni necesidad de API key externa.
    3. Se precargó `edge_tts` dentro de `prewarm()` para eliminar el lag de importación de 900ms durante la conexión de la llamada.
    4. Se validó end-to-end con tests automatizados: STT Whisper de Groq (transcripción 100% precisa), LLM con `openceo-core` (respuestas en tiempo real), EdgeTTS (generación de frames de audio instantánea) y LiveKit worker dispatch (<2 segundos de conexión).
    5. Contenedor `openceo-voice-agent` activo, con worker registrado en LiveKit y listo para llamadas.

## Estado actual (2026-09-12)

- `App Android (openceo-voice-android) - Corrección de Conexión de Sesión`:
  - Diagnosticada y corregida la causa raíz del error *"Error connecting to the session."* al pulsar "HABLAR AHORA".
  - **Causa raíz**: En `VoiceAssistantViewModel.kt`, la obtención del token con OkHttp síncrono (`fetchTokenFromServer`) se ejecutaba dentro de `session.start()` invocado desde un `LaunchedEffect(canEnableMic)` sobre el hilo principal de Android (`Dispatchers.Main`). Esto disparaba inmediatamente un `android.os.NetworkOnMainThreadException` del sistema operativo antes de alcanzar la red, y la excepción no capturada en `TokenSource.fromCustom` retornaba fallo inmediato con un Toast genérico sin detalle.
  - **Solución implementada**:
    1. Se delegó la petición de token a `withContext(Dispatchers.IO)` protegido con `runCatching` y timeouts de conexión/lectura explícitos (15s) en `OkHttpClient`.
    2. Se agregó validación de HTTP status code (`response.isSuccessful`) y control de token nulo/vacío con mensajes de error explícitos.
    3. Se mejoró `VoiceAssistantScreen.kt` para registrar en Logcat (`android.util.Log.e`) y mostrar en el Toast el mensaje de error real si la conexión falla (`result.exceptionOrNull()?.localizedMessage`).
    4. Se aseguró la configuración de red (`networkSecurityConfig`) en `app/src/main/res/xml/network_security_config.xml` y en el `AndroidManifest.xml` principal.
    5. Se validaron todos los servicios de voz activos: `openceo-livekit` (7880, 7881 TCP, 7882 UDP), `openceo-voice-token` (HTTP 200 en `health` y `api/voice/token`), `openceo-voice-agent` y `openceo-core` (puerto host 18765).
    6. Se compiló exitosamente el nuevo APK con `./gradlew assembleDebug` y se actualizó el binario en `/home/develop/development/Jardal/jardal-openceo/openceo-voice.apk`.

## Estado actual (2026-09-10)

- `Motor de Workflows y Autodiscovery con DeepSeek v4 Flash`: completada la implementación integral del motor de workflows YAML y autodiscovery:
  1. **Tool Registry Completo (15 tools operativas)**: implementado en `WorkflowToolRegistry` y publicado vía `GET /api/workflows`. Incluye herramientas de Core (`core.echo`, `core.complete`, `core.check_registry`, `core.query_tasks`, `core.query_workflow_runs`), PM (`pm.create_ticket`, `pm.create_tickets_batch`, `pm.extract_tickets` con DeepSeek), Secretaría (`secretary.send_email`, `secretary.list_sender_accounts`, `secretary.save_note`, `secretary.query_notes`), Finanzas (`finance.pago_elfec_house`) e Infraestructura (`cloudflare.upsert_dns_record`, `cloudflare.delete_dns_record`).
  2. **Autodiscovery Guiado por Catálogo Preciso**: `_build_discovery_proposal` inyecta las firmas y descripciones exactas (`self._tools.describe()`) en el prompt del LLM (`deepseek-v4-flash`), eliminando alucinaciones de parámetros y generando YAMLs válidos con Jinja expressions y triggers acordes al requerimiento.
  3. **Unificación Human-in-the-Loop (YAML + Dashboard Phase Actions)**: soportado el paso `type: human_in_the_loop` en YAMLs. Al llegar a este paso, el runner pausa con `status: blocked_human`, registra en MongoDB el ticket con `status: waiting`, `phase: <permission>` y `waiting_reason: phase:<permission>`. Al aprobarse en la UI (`create_phase_action` con `APPROVE`), el workflow se reanuda de forma reactiva ejecutando los pasos restantes sin reiniciar ni perder outputs previos.
  4. **Validación End-to-End**: verificado con tests automatizados contra MongoDB local y la API de DeepSeek (`deepseek-v4-flash`), y comprobado en el servicio host activo en el puerto `18765`.
- `Desacople de YAML Workflows / Autodiscovery frente a Phase Workflows`: corregida la colisión arquitectónica en `openceo-core/task_service.py` donde tickets con intención explícita de workflow determinista o autodiscovery (`params.workflow`, `params.workflow_id`, `params.workflow_ref`, `params.use_workflow`, `params.discover_workflow`) eran secuestrados por las fases por defecto de tickets dev (`dev.default` con `workflow_phases`). Ahora se preserva la prioridad de `WorkflowService` para ejecutar deterministamente en Tier 0/1 o iniciar `discovery_runs` sin requerir bypass manual.
- `Flujo de Presets de Prompts y Dictado Técnico por Voz`: refinado el flujo para que los presets se configuren exclusivamente de forma manual en `projects/<project>/project.yaml` (`review`, `test_fix`, `refactor`, `sync_context`, `security_audit`). Al pedir un preset por voz (*"pon el preset review"*), OpenCEO escribe el prompt inicial en la terminal interactiva `tmux` sin presionar Enter (`enter=False`), dejando el cursor listo para dictar el resto. Permite concatenar texto técnico adicional, enviar la instrucción final (*"dale enter"* / *"envíalo"*), o cancelar y limpiar la línea mediante `C-u` (*"borra la línea"* / *"cancela el preset"*).
- `Optimización de Respuestas de Voz Ultra-Cortas (TTS)`: eliminada completamente la repetición en voz alta de fragmentos de prompts, git diffs o nombres técnicos de sesiones. Las respuestas conversacionales del gateway quedaron reducidas a 1 o 2 oraciones directas, naturales y humanas (ej: *"Listo, preset review en pantalla. Dime qué le agregamos."*, *"Anotado. ¿Algo más o le damos enter?"*, *"Listo, enviado."*, *"Listo, borrado."*).
- `Alineación de Modelos y Providers Codex`: removida la bandera obsoleta `-m gpt-5.4` en `projects/jardal-openceo/project.yaml`, `projects/cebitec/project.yaml` y `projects/jardal-vpn/project.yaml`. Ahora Codex utiliza de forma limpia y transparente la configuración nativa de `~/.codex/config.toml` (`model = "gpt-6-astra"`, `reasoning_effort = "xhigh"`).
- `App Android (openceo-voice-android)`: validado el APK debug compilado (`openceo-voice.apk`, 68MB) con opción persistente "Conectar directo al abrir" y selector de Proyecto Foco.
- `Documentación viva`: actualizados `VOICE_GUIDE.md` y `CLAUDE.md` reflejando el flujo exacto de dictado técnico, comandos de voz y setup manual de presets.

## Estado actual (2026-09-09)

- `OpenCEO Voice Assistant & Android APK`: implementada experiencia de voz lista para hablar al abrir la app Android (`openceo-voice-android`). Se añadió soporte de selección de Proyecto Foco (`AVAILABLE_PROJECTS`), switch de auto-conexión persistente en `SharedPreferences` ("Conectar directo al abrir"), modelo predeterminado CEO, y compilación exitosa del APK debug en `/home/develop/development/Jardal/jardal-openceo/openceo-voice.apk` (68MB).
- `First Layer Gateway Conversacional (DeepSeek v4 Flash)`: actualizado `openceo-core/first_layer.py` para hablar en primera persona plural impersonal (*"Tenemos abiertos X tickets..."*), proporcionar guía activa contextual cuando el usuario está perdido o no entiende un comando (`guidance`), consultar y contar tickets en lenguaje natural fluido para TTS (sin UUIDs ni URLs), y orientar con sugerencias inmediatas.
- `Sistema de Presets de Prompts por Proyecto (Dictado Técnico por Voz)`: permite preconfigurar manualmente en `projects/<project>/project.yaml` prompts frecuentes reutilizables (`review`, `test_fix`, `refactor`, `sync_context`, `security_audit`). Al invocar un preset por voz (*"pon el preset review"*), OpenCEO escribe el prompt inicial en la terminal interactiva `tmux` de la sesión dev activa sin presionar Enter (`enter=False`), dejando el cursor listo para que el usuario continúe dictando el resto de la instrucción técnica por voz. La orden se envía diciendo *"dale enter"* o *"envíalo"*, y se cancela/limpia diciendo *"cancela el preset"* / *"borra la línea"* vía `C-u`. No requiere gestión de guardado/borrado por voz.
- `Documentación Integral de Voz`: creados `CLAUDE.md` y `VOICE_GUIDE.md` en la raíz del repositorio, documentando la arquitectura, contratos, comandos de desarrollo, y todos los flujos de voz paso a paso.
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
- `openceo-voice-android`: compilado en `openceo-voice.apk` (68MB) con selector de proyecto, auto-connect persistido ("Conectar directo al abrir") y modelo `ceo` por defecto para hablar al instante
- `FirstLayerGateway`: Primera capa cognitiva con DeepSeek v4 Flash, respuestas naturales en primera persona plural ("Tenemos..."), guía activa ante ambigüedad o usuario perdido, conteo y detalle de tickets abiertos
- `Prompt Presets` por proyecto: presets configurados en `project.yaml` y persistidos en MongoDB (`project_presets`); son prompts/instrucciones predefinidas despachadas en background a la sesión dev interactiva en tmux (`dev_session_service.send_message_async`) con autocreación de sesión si no existe
- Inspección de estado y salidas de presets/sesiones dev con captura de pantalla tmux pane en tiempo real
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
