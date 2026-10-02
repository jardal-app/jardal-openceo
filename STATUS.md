# OpenCEO Core

Resumen operativo corto del estado real del repo.

## Continuar aquí (traspaso a la próxima sesión)

**RESEARCH — cierre de implementación y pruebas autónomas (2026-10-02, cierre 16).** Leídos `CLAUDE.md`, los tickets del proyecto y el vault exacto de jardal-openceo. El usuario fijó revisión humana **al final de TODO el proyecto**, no al final de cada fase ni sesión; el agente ejecutó las pruebas que estaban a su alcance. La implementación disponible de A/B/C/D y proveedores queda integrada en `main`: commit `5db271e`, merge `0b1dc82`; fix del cargador systemd `3dcf312`, merge `580846c`, ambos merges subidos a origin. **El épico no está terminado:** faltan las demostraciones reales B/C indicadas abajo; no son revisiones rutinarias de código.

**C — social:** broker en `research/social.py`, helper `social_x.py` con `twscrape==0.20.1`, HTTP autenticado y CLI local `scripts/research_social_account.py`. Sesiones externas 0700/0600, metadata sin cookies, grants por cuenta/proyecto/owner/consumer/fuente/operación, lease Mongo compartida con fencing incluso entre procesos, revalidación al adquirir y leer artifacts, cancelación/retire/retención. Una cuenta, una petición/página y hasta cinco posts; búsqueda/post/cuenta solo lectura, sin proxy/rotación/retries, DM/bookmarks/feed o rutas LLM privadas. Artifacts `restricted` y `declaration`. Código/pruebas terminados; **no hubo sesión autorizada de X ni piloto real**.

**D — deep y eventos:** fases HTTP controladas con `deepseek-flash`, reserva monetaria compartida antes del I/O, límites/deadline/cancelación y sin llamadas auxiliares; researcher/verifier con contexto nuevo, una corrección y revisión final, extractos exactos de evidencia y contradicciones conservadas. Eventos versionados/scoped, outbox atómico payload+cursor, replay y recovery bounded sin repetir modelo, frescura/retención reevaluadas al leer; API/MCP con permisos explícitos. Exportador local separado, solo conclusión pública/durable/revisada del proyecto exacto autorizado; probado contra vault sintético, sin exportación automática de informes ni integración declarada con productos externos.

**Muestra D REAL revisada:** 10 informes públicos (5 briefs empresa/rol sintéticos sin CV, 3 sucesos, 2 preguntas), **30 claims** inspeccionados contra blobs/extractos: **23 supported / 7 insufficient_evidence**, **5 completed / 5 partial**, **0 citas inventadas/ajenas aceptadas**, 30 llamadas HTTP al modelo / 0 auxiliares. Dos casos se repitieron tras reforzar el verifier ante un footer no pertinente y una suposición sobre herramientas del usuario; la muestra usa esas versiones revisadas. Estimación por tarifa **USD 0,053575**, bound reservado **USD 0,263259**; factura reconciliada y build exacto del modelo **null**. Alias solicitado/reportado `deepseek-flash`; no se promete certeza ni porcentaje general. Se usó el límite USD 2 ya configurado por el usuario, sin elevarlo. Evidencia **privada adjunta y releída** en `TASK-2026-00076`: `research-acceptance-d-20261002.json`, id `832cc90c73`, 31671 bytes. Datos crudos en `/tmp/jardal-research-deep-pilot-20261002-final` y `-reviewed`; copia local ignorada en `tmp/research-acceptance-d-20261002.json`. Recomendar eliminar las copias locales tras la revisión final y comprobar que el adjunto conserva lo necesario; no se borraron.

**Proveedores opcionales (`00083`):** Serper y Firecrawl montados bajo política efectiva del job, sin fallback automático. Saldos consultados por API: Serper 2500 consultas; Firecrawl 1275 créditos con ventana hasta `2026-11-02T02:57:53.873Z`. Ledger nativo compartido separado de USD, débito previo al I/O, fallos ambiguos conservados, ventana caducada bloquea; sin compra/topup/renovación automática. Probes reales: Serper devolvió una URL; Firecrawl extrajo 854 caracteres de example.org, ambos sin LLM. Se corrigió el manejo de respuesta streaming/gzip tras un fallo real de Firecrawl; el débito incierto del intento fallido permanece. Firecrawl conserva `remote_egress_unattested`/partial porque no acredita toda su cadena remota DNS/redirect. El lector HTTP conserva la causa de degradación y no interpreta `Last-Modified` como fecha de publicación.

**Verificación:** toda la batería Research + Horizon **848 passed / 2 skipped / 0 failed** (65 subtests). La suite completa se ejecutó una vez: **1176 passed / 8 failed / 2 skipped** (183 subtests); los mismos ocho fallos preexistentes se reprodujeron en `main` anterior `4805b8f` con el mismo `.env` en worktree aislado. Tras cambios posteriores se repitieron los checks afectados: proveedores 62 passed, Deep 11 passed (incluye cancelación con HTTP en vuelo), cargadores host/systemd 1 passed + 2 subtests; compilación y `bash -n` correctos. No se afirma toda la suite verde. Smoke **API y MCP por transporte real**, con JWT/Mongo y OpenCode CLI free en container de egress: ambos recorridos quick completed/ok, lectura y citas de example.com.

**Configuración y despliegue verificados:** nuevos valores escritos directamente en **`openceo-core/.env`**, no dejados para copiar de ejemplos. `CEO_LLM_MODEL=deepseek-flash`; defaults/referencias operativas del alias antiguo reemplazados, menciones históricas conservadas. `RESEARCH_DEEP_API_KEY=${DEEPSEEK_API_KEY}` resuelto; modelo/precios/techo deep, directorio social y cuotas nativas configurados. Se descubrió que systemd relanzaba el servicio usando otro loader que no expandía referencias: corregidos ambos launchers y probado que texto tipo `$(...)` no se ejecuta. Unidad `openceo-core-host.service` reiniciada; **PID 1510331**, configuración efectiva CEO y Deep `deepseek-flash`, clave deep configurada y coincidente con la referencia. OpenAPI **200**, rutas de eventos/social montadas, anónimo **401**, autenticado **503** por `RESEARCH_ENABLED=false` y `RESEARCH_SCHEDULER_ENABLED=false`. No se activó recurrencia de producción. Operación y comandos: [`docs/research-operations.md`](docs/research-operations.md).

**ERPNext, estados releyendo cada cambio:**

| Ticket | Estado de negocio | Resultado o motivo restante |
| --- | --- | --- |
| `TASK-2026-00071` | Completed | Broker social implementado y verificado por el agente |
| `TASK-2026-00074` | Completed | Deep implementado y probado con muestra real |
| `TASK-2026-00075` | Completed | Eventos, replay, permisos y exportador comprobados |
| `TASK-2026-00083` | Completed | Cuotas medidas, wiring y probes reales Serper/Firecrawl |
| `TASK-2026-00072` | Pending Review | Solo falta sesión/cookies **locales autorizadas** de X y cuenta/consulta pública para piloto de hasta cinco posts; login/2FA por persona |
| `TASK-2026-00073` | Working | Depende del piloto real `00072`; pruebas de aislamiento y privacidad hechas |
| `TASK-2026-00070` | Pending Review | Solo falta autorización final de recurrencia y URL pública RSS/Atom de prueba controlada para demostración desplegada create/restart/tick/pause/cancel |
| `TASK-2026-00076` | Working | Muestra D real hecha; cierre total depende de aceptación B `00070` y C `00073` |
| `TASK-2026-00056` | Working | Épico pendiente de esas aceptaciones, sin revisión humana por fase |

**Cierre humano único del proyecto:** aportar la sesión X por el canal local oculto y autorizar/fijar la fuente de la prueba recurrente. Después el agente ejecuta esas dos demostraciones, cancela/limpia las pruebas y completa dependencias en orden; no pedir a una persona que revise cada diff o repita tests. No crear credenciales ni declarar aceptación con fixtures. Resultados y motivos exactos registrados en comentarios/descripciones de ERPNext; los dependientes quedan Working, no en cola humana.

**Conocimiento durable:** `components/research-social-broker.md`, `components/research-deep.md`, `components/research-events.md`, `components/research-providers.md` y hub **solo del vault de jardal-openceo**, comprobados con `--project` y cinco rutas explícitas (0 errores / 0 avisos), commit `103dbc3` en jardal-knowledge. No nuevos backlogs ni temporales en `docs/work`; los temporales Research de la sesión anterior ya se borraron con permiso. El temporal de Tier S y el original de Firecrawl con credencial siguen fuera de esta limpieza.

---

**CIERRE de research (2026-10-02, noche, cierre 15): `TASK-2026-00069` — suscripciones, ticks incrementales, API/MCP y retención.** Rama `feat/research-suscripciones-00069`. Una suscripción es un schedule (`research_schedules`) con workflow `research.watch_tick` o `research.discovery_tick`; cada tick lee la fuente, deduplica en código y admite **un** job quick solo si hay items nuevos o cambiados que pasen el filtro (**0 llamadas al modelo sin cambios pertinentes**; no crea Tasks ni toca ERPNext). Módulos: `research/watch.py` (motor, adaptadores RSS/Greenhouse/Lever, eventos), `research/subscriptions.py` (servicio), `research/execution.py` (contexto compartido), rutas `/api/research/subscriptions*` en `router.py`, 6 tools MCP en `mcp_facade.py` (grants `subscribe`/`subscriptions`/`manage_subscription`, **fuera** del conjunto por defecto), YAMLs `workflows/research/{watch,discovery}_tick.yaml`.

**Orden de un tick (a prueba de crash):** leer → dedup contra `research_watch_events` (único `(subscription_id, event_key)`) → insertar eventos → **después** avanzar el cursor con fencing → admitir un job por lote `pending` con clave idempotente del *lote* (no del tick). Primera pasada = línea base (sin job) salvo `backfill`. Cobertura incompleta declarada: `item_cap`/`page_limit` retienen el cursor (releer sirve, hay progreso) y `source_truncated` avanza (releer da lo mismo). Descubrimiento: cupo diario propio consumido **antes** de buscar, solo hash de las URLs del SERP (términos de Brave), solo `exclude_terms`. Cada tick vuelve a resolver la autoridad: una denegación **pausa** la suscripción con `status_reason` (`access_revoked`/`policy_denied`) en vez de reintentar; revocar la última credencial de un consumidor la pausa (`grant_revoked`).

**Defectos reales encontrados y corregidos (antes silenciosos):** `store.update` calculaba el nuevo vencimiento y lo descartaba (cambiar la cadencia no surtía efecto); `schedule_fingerprint` usaba `repr` de dicts anidados (mismos params en otro orden = 409 falso); `set_research_schedule_status` pisaba estados terminales (una pausa tardía resucitaba un cancelado); re-suscribirse tras cancelar devolvía el cancelado como "idempotente" (ahora cancelar/expirar libera `dedupe_key`); `daily_at` inválido salía como excepción cruda; el 5xx del lector llega como `protocol` (se reconoce por su detalle para reintentar). La revisión `/code-review high` dejó 10 hallazgos: 9 reales, corregidos con test de regresión (cursor retenido para siempre en fuentes >200 items, filtro `include_terms` sobre URLs, `revoke` de un token pausaba todo el consumidor, listener de revocación sin log, filtrado no reevaluable al ampliar el filtro, `insert_one` en bucle → `insert_many`, evento `pending` sin URL, helper muerto).

**Verificado:** 51 tests nuevos de vigilancia (Mongo real, servicio real, mutaciones comprobadas: clave del lote, línea base, tope de items, cupo previo a la búsqueda), 53 de suscripciones (servicio, HTTP sobre ASGI con JWT, MCP sobre transporte real; aislamiento entre owners **del mismo proyecto** comprobado con mutación), tests de scheduler/workflows/contratos ampliados. Suite completa del repo (antes de las correcciones de la revisión): **1093 pasan / 8 fallan / 2 skipped**; los 8 son los preexistentes y se comprobó que **también fallan en `main` limpio** (worktree con el mismo `.env`). Tras las correcciones se repitió toda la batería de research + Horizon: **779 pasan / 2 skipped / 0 fallan** (las demás suites no tocan estos módulos). **TTL real de Mongo** verificado una vez con script fuera de la suite: tras 57 s borró el job terminal vencido y el evento vencido y respetó el job vivo, el no vencido y el evento vigente. Contratos de Greenhouse (`{jobs,meta}`, sin auth ni paginación) y Lever (lista JSON, `skip`/`limit`, sin límite por defecto documentado → se pide siempre una página de 100) comprobados en su documentación oficial; los fixtures **no** prueban conectividad ni calidad real.

**Retención:** `research_jobs.expires_at` solo lo escribe la transición a terminal (`RESEARCH_JOB_RETENTION_DAYS`=180; un job vivo no tiene el campo); borrar un job libera su clave idempotente (límite aceptado). Eventos con TTL **deslizante** de 90 días. Los jobs terminales anteriores a este cambio no llevan `expires_at` y no caducan (no se hizo backfill).

**Límites declarados:** el tope de suscripciones por owner (`RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER`=10) no es transaccional (dos altas simultáneas al límite podrían pasar); el registro de consumidores sigue en memoria (el vencimiento por TTL no pausa, lo durable es la cuenta del owner); el tick no renueva su lease (un tick más largo que el lease pierde el commit por fencing y se repite sin duplicar). **Nada se activa:** `RESEARCH_ENABLED=false` y `RESEARCH_SCHEDULER_ENABLED=false` se conservan; el servicio vivo responde 503 a las suscripciones hasta que el dueño las encienda.

**Aceptación B offline hecha (`TASK-2026-00070`, sigue abierto):** `tests/test_research_acceptance_b.py` (19 tests, Mongo real, **tiempo virtual**, reinicios simulados con otro despliegue sobre la misma base, fallos inyectados en cada punto): cadencias 30 min/1 h/4 h exactas, DST (hueco de primavera y repetición de otoño), reinicio que conserva identidad/vencimiento/cursor sin duplicar, un solo catch-up tras 48 intervalos perdidos, crash en adquisición/almacén/cursor/admisión/tras el claim (un evento por item y scope, un job por lote, `pending_reconcile` declarado), 0 llamadas al modelo sin cambios, fuente caída que conserva el cursor, owners independientes sobre la misma fuente, timer sin sondeo y despertado por la superficie real, overlap skip/merge, owner revocado pausa, ticks sin Tasks, TTL que no toca schedules y dedup que sobrevive al job. Cuatro mutaciones (cursor antes de eventos, denegación reintentada, timer con tope, overlap sin contar) hacen fallar el test que les corresponde. **Falta lo que exige evidencia real:** reiniciar el servicio **desplegado** con `RESEARCH_ENABLED` y `RESEARCH_SCHEDULER_ENABLED` encendidos, observar un tick contra una fuente pública controlada (el guard de egress bloquea localhost) y pausar/cancelar; el propio ticket exige autorización del dueño antes de activar la capacidad recurrente.

**Estado en ERPNext (verificado releyendo):** `TASK-2026-00069` **Completed** (ejecución `completed`, por el webhook); `TASK-2026-00070` **Pending Review**, con en la descripción la acción concreta del dueño: (a) autorizar encender `RESEARCH_ENABLED` y `RESEARCH_SCHEDULER_ENABLED` y habilitar research en el `project.yaml` del proyecto de prueba, y (b) fijar una **URL pública** de feed RSS/Atom de prueba. Al recibirlo, el agente reinicia el servicio, crea una suscripción acotada, verifica que identidad/vencimiento/cursor sobreviven a un reinicio, observa un tick y la cancela. Servicio reiniciado con el código mergeado (`4acca1c`): openapi 200, anónimo 401, autenticado 503 por research deshabilitado.

**Siguiente sesión:** si el dueño autoriza, la demostración desplegada de `00070`. Sin esa decisión no hay más trabajo de código de la Entrega B. La Entrega C (`00071`–`00073`) exige login HITL y credenciales de X; la D (`00074`–`00076`) exige elegir modelo/presupuesto y medir calidad y coste reales; `00083` (cuotas Serper/Firecrawl) no bloquea. `TASK-2026-00083` (cuotas/Serper/Firecrawl) sigue Open. Recomendado borrar `docs/work/TASK-2026-00069-suscripciones.md` y `docs/work/TASK-2026-00083-proveedores-research.md` (git-ignorados) con autorización del dueño; lo durable quedó en el vault (`components/research-subscriptions.md`, `components/research-scheduler.md`) y en los tickets.

**Cierre de la sesión (2026-10-02):** el dueño autorizó borrar los temporales. Borrados `docs/work/TASK-2026-00069-suscripciones.md` (el diseño ya está en el vault y en el ticket), `docs/work/TASK-2026-00083-proveedores-research.md` (su contenido útil — estado de claves, criterio de la guía de Firecrawl y las 4 referencias sin verificar — quedó en un comentario de `TASK-2026-00083`) y `docs/work/TASK-2026-00056-investigator-elite.md` (copia privada adjunta al épico). **Se conserva `docs/work/TASK-2026-00030-tier-s.md`** a propósito: es la referencia operativa vigente de `TASK-2026-00030` (Open, su descripción la cita) y no pertenece a esta línea de trabajo; borrarlo requiere su propia autorización. Sigue ignorado y **no** versionado `tmp/Info FIRECRAWL.txt` (contiene una credencial; no adjuntar). Vault: `components/research-subscriptions.md` (nueva), `components/research-scheduler.md` y el hub, con checker 0/0, **commiteados** en `jardal-knowledge` (`e75d735`, solo esas tres rutas); la copia nocturna del vault los incluye. Memoria del agente: `feedback_trabajo_nocturno_desatendido` y `proyecto_research_estado`.

**CIERRE de research (2026-10-02, cierre 14): `TASK-2026-00082` COMPLETED — runner de schedules integrado al servicio.** Entrega acotada del trabajo de `00069`, registrada como hijo del épico `00056`. `research.investigate` y `workflows/research/investigate.yaml` admiten un job quick con contexto research autenticado por un runner separado de Tasks; no crean ni cierran tickets de negocio. Coordinator montado en lifespan, con inicio condicionado a **ambas** flags `RESEARCH_ENABLED` y `RESEARCH_SCHEDULER_ENABLED`, y parada antes del servicio/repositorio. Owner, permiso, proyecto, política, estado, fencing y lease se comprueban antes de admitir.

**Corrección verificada:** la toma preserva `scheduled_for` durante backoff y recovery; antes lo reemplazaba por el nuevo `next_run_at` y podía admitir un segundo job tras una respuesta perdida. La clave estable reconcilia el mismo job, repara un wake perdido si sigue queued y evita redespachar terminales. **14 tests nuevos**; checks dirigidos: **119 de research + 9 de Horizon = 128 pasan**, incluyendo Mongo real, caída tras persistir, reinicio, revocación, pausa durante auth y wake del store. Commit `c41e009`, merge `ce0db13` a `main` con push; servicio reiniciado; smoke vivo: openapi 200, workflow/tool presentes, anónimo 401 y autenticado 503 por configuración deshabilitada. Vault `components/research-scheduler.md` actualizado, checker 0/0.

**Siguiente sesión:** `TASK-2026-00069` sigue **Working**: faltan `watch_tick`/`discovery_tick`, API/MCP de suscripciones con identidad y grants, cursores RSS/ATS incrementales y retención; después aceptación B en `00070`. El épico `00056` continúa Open. No falta aprobación humana para estas tareas de código.

**APIs nuevas:** claves Brave/Serper/Firecrawl presentes en `.env`; no se validaron conectividad ni saldo. `RESEARCH_ENABLED=false` se conserva. Los adaptadores existen, pero wiring monta Brave y HTTP público; verificación de cuotas y conexión opcional de Serper/Firecrawl quedan en **`TASK-2026-00083` (Open, autorun=false)**. Guía aportada sintetizada sin credenciales en `docs/work/TASK-2026-00083-proveedores-research.md` (local, ignorado); original `tmp/Info FIRECRAWL.txt` conservado e ignorado. No se instalaron MCP/CLI globales ni se consumieron las APIs. Al cerrar `00083`, recomendar eliminar su documento temporal con autorización del dueño.

**CIERRE de la sesión del MCP (2026-10-01, cierre 13): `TASK-2026-00079` COMPLETED — el MCP ya tiene el hilo del ticket completo.** Ocho herramientas nuevas en `openceo-core/mcp_server.py`: `openceo_update_ticket`, `openceo_add_ticket_comment`, `openceo_update_ticket_comment`, `openceo_delete_ticket_comment`, `openceo_attach_ticket_file`, `openceo_get_ticket_attachment`, `openceo_update_ticket_attachment`, `openceo_delete_ticket_attachment`, más `openceo_get_ticket` ampliado. **13 tools de tickets + DNS = 14 en ambas monturas.** Descripciones de una línea, con test que las mantiene bajo 220 caracteres y 32 palabras (cada palabra viaja en cada petición de la sesión de código).

**Dos decisiones del dueño aplicadas en la misma sesión:**

1. **Una sola lectura para entender un ticket.** `openceo_get_ticket` devuelve campos + `execution_status` + comentarios + adjuntos con su `download_url`. Se creó `openceo_get_ticket_thread` y se **eliminó el mismo día** por pedido del dueño: dos tools para leer un ticket obligaban al agente a un paso extra en cada turno. El adapter separa `thread_of(task_name)` (sin resolver de nuevo) de `list_thread(identifier)`.
2. **Control completo del hilo.** Frappe reserva `Comment` a `System Manager`/`Website Manager`, así que ni editar un comentario ajeno ni borrar ninguno era posible. El dueño lo autorizó y se hizo **desde el ERP por API** (sin navegador): rol dedicado **`OpenCEO API`** (`desk_access=0`) con `read`/`write`/`create`/`delete` sobre `Comment` (permlevel 0, `export=0`) asignado a `openceo@jardal.app` junto a `Projects User` + `Accounts User`. **Reversible:** `permission_manager.reset("Comment")` + quitar el rol al usuario. Como ese rol abre `Comment` en todo el sitio, el adapter repone la frontera que ponía el método de Frappe: antes de editar o borrar lee el comentario y exige `comment_type == "Comment"`, `reference_doctype == "Task"`, un ticket legible y —al borrar— que cuelga del ticket que el llamador nombró (verificado en vivo: con el ticket equivocado devuelve `invalid_input` y no borra).

**Hechos medidos contra el sitio vivo** (Frappe 16.35.0 / ERPNext 16.36.0) que fijan el resto del diseño: se escribe por los métodos del desk (`add_comment`, `update_comment`, `remove_attach`, `get_docinfo`, `upload_file`), que comprueban permiso sobre el **ticket**; `File` entra en la allowlist de DocTypes solo para renombrar o cambiar privacidad, y `Comment` solo para la edición ajena y el borrado. Idempotencia: adjunto por `md5(contenido)` + ticket (`upload_file` crea un `File` nuevo en cada llamada), comentario por autor + texto normalizado; los POST que crean van con un solo intento. Adjuntos privados por defecto, tope 10 MiB, raíces autorizadas `ERPNEXT_ATTACHMENT_ALLOWED_ROOTS`, nombre saneado sin traversal, extensiones de script rechazadas y sin ingesta por URL (cero SSRF). `openceo_update_ticket` no toca el estado (máquina de negocio: `status_pushed` + webhook `on_update`) y acepta texto plano o el HTML que el campo ya tiene (Frappe sanea al guardar). Suite: **969 pasan / 8 fallan / 2 skipped** (los mismos 8 preexistentes de entorno; +82 nuevos, todos en `tests/test_erpnext_ticket_thread.py`). Commits `18edbb8`, `1518ba7` y `384a8a4`; merges `1b872ac`, `cb2e2a9` y `ab9cbaa` a `main` con **push**; servicio reiniciado (pid 4058411, openapi 200). Prueba real en el ticket de ensayo `TASK-2026-00081` (queda `Cancelled` con su hilo de evidencia: comentario creado, editado, deduplicado y borrado; adjunto subido, renombrado, leído y borrado; comentario de `admin@jardal.app` editado y borrado por el MCP); resultado, adjunto `TASK-2026-00079-resultado.txt` y comentario de cierre en el ticket real. Contrato y motivos en `docs/ARCHITECTURE_REFERENCE_2026.md`, sección «Ticket thread».

**Ojo con los 8 fallos de la suite: no son Mongo caído.** Mongo responde (8.0.13, bd `openceo`, 125 tasks, 26 colecciones) y el servicio lee la ejecución sin problema. De los 8, solo 2 mencionan Mongo y son `RuntimeError: MongoRepository not connected` de `db/mongo.py:44`: tests unitarios que construyen el servicio sin conectar un repositorio. Los otros 6 son deriva de tests viejos (etiqueta en inglés que el prompt ya no emite, `notion_manager` en firmas que ya no lo aceptan, un `autorun=False` que un mock no espera, y la frase de aporte que dice 0.00 porque el scope de Finance sigue vacío). Son el baseline documentado desde el cierre 12 y no los tocó este trabajo; arreglarlos es un ticket aparte.

**CIERRE de la sesión de research (2026-10-01, cierre 12): ENTREGA B INICIADA, `TASK-2026-00068` COMPLETED.** Sin claves ni permisos nuevos: la Entrega B es trabajo de código. `openceo-core/research/scheduler.py` implementa el scheduler persistido (colección `research_schedules`, sin TTL) con semántica propia: espera **indefinida** sin vencimiento (corrige el barrido de `HorizonScheduler._loop`, tope 3600 s), `interval_seconds` XOR `daily_at` con DST explícito, toma atómica `active/idle→running` con lease y `fencing_token`, backoff acotado, catch-up único tras reinicio y `pending_reconcile` (no se afirma exactly-once de la red). `39` tests nuevos (`tests/test_research_scheduler.py`, integración de lease/fencing contra Mongo real aislado). Suite: **887 pasan / 8 fallan / 2 skipped** (los 8 preexistentes de entorno; +39 sobre el baseline 848). Commit `ca93590`, merge `8f570b6` a `main` y **push**; servicio reiniciado (pid 3344675, openapi 200). Vault: `components/research-scheduler.md` (checker 0/0), **sin commit** (git cercado al repo de la sesión). **Límite declarado en el ticket:** el coordinator aún no se monta en el lifespan; su handler es el tick de workflows (`00069`) y el wake desde superficie real se demuestra en `00070`.

**Siguiente sesión (research):** `TASK-2026-00069` — registry `research.*`, workflows `workflows/research/*.yaml` deterministas, suscripciones (identidad por proyecto+owner+propósito+fuente+versión) y cursores incrementales, montando el coordinator en el lifespan; después `TASK-2026-00070` (aceptación B). Entrega C (`00071`–`00073`, broker social + piloto X) exige login HITL y credenciales de X: ahí sí hará falta decisión del dueño. **Ninguna API key (Brave/Serper/Firecrawl/Meta) es necesaria para B ni C.**

**CIERRE de la sesión de research (2026-10-01, cierre 11): ENTREGA A CUMPLIDA.** `TASK-2026-00062`, `00063`, `00065`, `00066` y `00067` quedan **Completed** en ERPNext, con el resultado verificado escrito en cada ticket. El épico `TASK-2026-00056` sigue **Open** a propósito: faltan las Entregas B, C y D.

**Lo que se demostró (ejecutado, no deducido):**
- **Circuito real por la API y por la fachada MCP**, con consumidores de prueba: `scripts/research_facade_probe.py --path both` da `ok=true`; en ambos caminos el job termina `completed` con **2 afirmaciones que citan su propio `evidence_id`** y las citas resuelven contra la evidencia persistida del job. Modelo: el candidato libre del épico (`opencode/muse-spark-1.3-contributor-free`) con **perfil cerrado** y versión del CLI **verificada** (2.0.20).
- **Aislamiento real**: canario fuera del workspace **inaccesible** y **destino prohibido bloqueado** (egress filtrado por destino). Evidencia: `scripts/research_circuit_probe.py` y `tests/test_research_isolation.py` (9 tests, con smoke real opt-in `RESEARCH_EGRESS_SMOKE=1`).
- **Broker montado** en la aplicación real: 401 sin Bearer, `tools/list` con las tres tools exactas, apagado (404) por defecto; del worker aislado sale `mcp connected server=research-broker tools=3`.
- **Suite del repo: 848 pasan / 8 fallan / 2 skipped.** Los 8 son los preexistentes de entorno; cero regresiones.

**Hallazgo que desbloqueó todo (no obvio y medido):** la frontera de *deny por defecto* del worker va en **`experimental.policies`**, no en `agents.*.permissions`. La capa libre de OpenCode responde `403 "free tier can only be used from within OpenCode"` ante **cualquier** regla `deny` de agente — reproducido con 5 modelos libres y 5 variantes de perfil. `policies` es binaria, nunca pregunta y solo endurece, y el CLI sí la acepta. **Si alguien mueve el deny a `permissions`, el worker se queda sin modelo.** `assert_worker_config_is_closed()` rechaza explícitamente esa forma.

**Defectos reales corregidos** (todos visibles solo al ejecutar): credencial de consumidor anulada por `fastmcp` 3.x (`authorization` excluido por defecto); motor no montado en bwrap; config del worker invisible; DNS roto (`/etc/resolv.conf` es un enlace a `/run`, no montado); plan que afirmaba una frontera de red inexistente + backend `unshare` sin aislamiento de ficheros; `PATH` saneado sin `/usr/sbin`, que impedía aplicar `iptables`; hash del lector HTTP que rompía toda lectura HTML; `_build_discovery` no invocable; esquema MCP (`oauth`, claves de `timeout`); informe exigido en JSON puro; y **colisión de ids de evidencia entre jobs del mismo proyecto** (`EvidenceConflict`), con test de regresión.

**Siguiente sesión (research):** Entrega B (`00068`–`00070`, scheduler persistido y suscripciones) y después C y D. Opcional: endurecer el arranque en despliegue — el filtrado de egress es **opt-in** (`RESEARCH_ISOLATION_BACKEND=container` + gateway + `RESEARCH_BROKER_ALLOWED_IPS` con la subred); `scripts/research_worker_image.sh` prepara imagen y red e imprime ajustes y regla de UFW. Sin ese ajuste el plan corre con bwrap y **declara** `egress_restricted=false`.

**Cambios de infraestructura en el host (no son código; revertibles):** red docker `openceo-research-egress` (172.31.240.0/24), imagen `openceo-research-worker:latest` y **dos reglas UFW** (`172.31.240.0/24` → 18765 broker y → 18899 probe). No publican nada hacia fuera.

**Resuelto (2026-10-02):** el dueño autorizó borrar los documentos temporales; `docs/work/TASK-2026-00056-investigator-elite.md` se borró tras adjuntar una copia **privada** idéntica (mismo hash md5) al épico `TASK-2026-00056` como `TASK-2026-00056-borrador-y-auditoria.md` (el borrador arquitectónico y su auditoría, útil para las Entregas C y D; los tickets revisados prevalecen sobre él).

**Plan de cierre de Tier S registrado el 2026-10-01:** se amplió **`TASK-2026-00030`** con la aceptación del flujo base: activación de `claudej` tras la renovación comunicada por el dueño, modelos efectivos, protección de lectura de revisores, piloto completo, preguntas/reanudación y proyección a ERPNext real. Sigue **`Open`** porque esa aceptación no se ha cumplido; su ejecución `failed` del 26-09 fue un ruteo a Finance, no una ejecución del consenso. El motor está integrado en `main` (`3128146`), pero `projects/jardal-openceo/project.yaml` todavía conserva `planner.active=false`.

Se crearon **`TASK-2026-00078`** (adaptador OpenCode V2 de revisión de solo lectura y piloto con modelo gratuito) y **`TASK-2026-00079`** (comentarios y adjuntos de tickets por MCP, funcionalidad futura independiente de Tier S), ambos **`Open`**, ejecución **`queued`**, **`autorun=false`**. El piloto base se conserva en `00030` y puede cerrarse con su roster actual; `00078` amplía después el consenso con OpenCode y `00079` no es una dependencia del cierre. `TASK-2026-00042` sigue siendo el ruteo de revisión humana, sin dependencia del piloto técnico. Los detalles y criterios viven en ERPNext; esta actualización solo registra la planificación, sin implementación, activación, pruebas ni modelos.

**Actualización de la misma sesión (cierre 11, segunda tanda): el bloqueo de modelo está resuelto y el circuito real CIERRA con el perfil cerrado.** La aceptación exigía *deny por defecto* en el perfil del worker, y la capa libre del proveedor rechazaba cualquier perfil con deny (`403 "OpenCode's free tier can only be used from within OpenCode"`). Se reprodujo con **cinco modelos libres y cinco variantes de perfil**: dispara **cualquier** regla `deny` en `agents.*.permissions`, no una acción concreta. OpenCode V2 separa dos capas ([policies](https://opencode.ai/v2/docs/policies)): `permissions` (por agente, allow/deny/ask) y **`experimental.policies`**, que es **binaria, nunca pregunta y solo endurece** lo que permisos y proveedores ya permiten — no la levanta una regla de agente ni una aprobación guardada. La frontera del worker vive ahora ahí (deny global primero + `allow` de las tres tools del broker) y `agents.*.permissions` queda en `allow` **a propósito**: el deny de agente no era defensa en profundidad, era el fallo que dejaba al worker sin modelo. `assert_worker_config_is_closed()` valida la capa de policies y **rechaza explícitamente** un `deny` en la capa de agente con el motivo real.

Con eso, el modelo candidato del epic (`opencode/muse-spark-1.3-contributor-free`, **libre**) ejecuta el circuito completo: `ok=true`, `tool_called=true`, `cites_broker_evidence=true`, lectura real de `https://example.com/` vía HTTP, evidencia persistida en SQLite/FTS5 (`br-d80791d09dfd0edfeec0e213`, `content_hash=db1730df…`, `observed_at`, `access_method=http`), **extracto citado verificado contra el texto almacenado**, y el worker **no puede ejecutar shell ni leer el canario** fuera del workspace (`DENEGADO`). El CLI registra `mcp connected server=research-broker tools=3`. Reproducible: `./.venv/bin/python scripts/research_circuit_probe.py --model opencode/muse-spark-1.3-contributor-free`.

También se añadió la **comprobación de versión**: el runtime verifica el pin (`2.0.20`) contra el binario (`probe_cli_version` cableado en `wiring.py`); antes el pin era una declaración que no se comprobaba y una versión distinta ejecutaba igual. El probe reporta `version_verified=true`. Suite del repo: **844 pasan / 8 fallan / 1 skipped** (los 8 preexistentes de entorno; cero regresiones).

**Lo que queda (todo trabajo de agente, ninguna credencial):** (1) el **egress del worker no está restringido por destino** — el plan lo declara (`egress_restricted=false`, `network="unrestricted"`); las herramientas que podrían usarlo están denegadas por policy, así que la superficie efectiva del modelo es solo el broker, pero la frontera de red del **proceso** sigue abierta. Exige control de red de host (netns filtrado: podman con política, o iptables con `CAP_NET_ADMIN`); con Docker disponible y el usuario en el grupo `docker` es viable, pero hay que construir imagen y **medir** el límite. Mientras `00062` siga abierto, ERPNext **no permite completar** a sus dependientes (`00063`/`00065`/`00066`/`00067`): es el gate real de la cadena, no una elección. (2) El punto 1 de la aceptación de `00067` pide el circuito **vía API y fachada MCP con consumidores de prueba**: está demostrado a nivel runtime+broker con transporte MCP real, y la fachada está verificada por separado sobre transporte real, pero falta la corrida **extremo a extremo por la fachada** (submit → despacho → worker → broker → evidencia → informe) con un consumidor de prueba.

Estado al cerrar la sesión del **2026-10-01 (cierre 11)**: **el entorno cambió por completo y con él se desbloqueó la Entrega A**. Este contenedor **sí** concede ahora una frontera real (`unshare --user` funciona; existen `bwrap`, `podman`, `docker`, `sudo` y `/var/run/docker.sock`), el **CLI de OpenCode v2.0.20 ya existe**, el `.venv` del root **funciona** (Python 3.12.3, pytest 9.0.3) y **el servicio se puede reiniciar**. Con eso se ejecutó por fin lo que las sesiones anteriores no pudieron: el **piloto de aislamiento real**, el **montaje del broker** y el **circuito completo**. Nuevo baseline de suite en este entorno: **842 pasan / 8 fallan / 1 skipped** (los 8 son preexistentes de entorno; al empezar la sesión era 826/12 — los 4 fallos de la fachada MCP eran un **defecto real**, no entorno).

**Nuevo helper de esta sesión:** `./.venv/bin/python scripts/research_circuit_probe.py --model <modelo>` monta el broker igual que la app, sirve por HTTP en el mismo bucle, emite una capability y ejecuta el worker real bajo bwrap; imprime un informe JSON (preflight de alcance desde dentro del sandbox, `tool_called`, evidencia persistida, cita). `--diagnose` comprueba la conexión MCP del CLI.

**Seis defectos reales encontrados al ejecutar de verdad (ninguno visible en tests offline):**

1. **`fastmcp` 3.x anula la credencial de consumidor de la fachada MCP.** `get_http_headers()` **excluye `authorization` por defecto**; la fachada lee ahí la credencial por contrato, así que rechazaba con `unauthenticated` **todas** las llamadas legítimas (4 tests fallaban por el motivo correcto). Corregido pidiendo el encabezado explícitamente, con reserva a la petición HTTP cruda. `requirements.txt` fija `fastmcp==2.14.5` y el venv tiene **3.3.1**: hay drift, y ahora el código tolera ambos.
2. **El plan de bwrap no montaba el motor.** El CLI vive en `/home/develop/.opencode/bin/opencode` y solo se montaban `/usr /bin /lib /lib64 /etc`: el worker **no podía ni ejecutarse**. Ahora se monta el binario en solo lectura (`extra_read_only`), no el home.
3. **La config del worker era invisible dentro de la frontera.** `HOME`/`XDG_CONFIG_HOME` apuntaban a directorios no montados → `opencode.json` no existía dentro y `--agent investigator` volvía a morir con *Agent not found*. Ahora home y config del job se montan, y hay `/tmp` privado.
4. **Sin DNS no hay modelo.** `/etc/resolv.conf` es un **enlace** a `/run/systemd/resolve/...` y `/run` no se monta: el worker moría con `provider.transport getaddrinfo ETIMEOUT`. El plan monta el **destino real** del enlace sobre un `/run` propio.
5. **El plan afirmaba una frontera de red que no existe.** Declaraba `network="broker"` pasando `--share-net` (el worker alcanza **cualquier** destino). `IsolationPlan` ahora declara `filesystem_isolated` y `egress_restricted` por separado, con la verdad medida, y el runtime **se niega a ejecutar** si el backend no acredita aislamiento de ficheros (`UnshareBackend` no monta nada: era un degradado silencioso).
6. **El hash del lector HTTP rompía toda lectura HTML.** `_build_reader` publicaba el hash del **cuerpo crudo** como `content_hash`, pero el almacén valida el hash del **texto** almacenado: cada página real fallaba con `EvidenceHashMismatch`. Además `_build_discovery` devolvía un `BraveClient` **no invocable** cuando el contrato del servicio es `discover(question=..., research_id=..., project_key=...)`: con una clave de Brave, **cada job con búsqueda** habría fallado con un `failed` genérico. Ambos corregidos.

**Lo que quedó montado y verificado:** `research/broker_mount.py` (broker con las operaciones reales: mismo lector, mismas evidencias, mismo ledger; montura **loopback + Bearer** reutilizando `build_mounted_mcp_server`), settings `RESEARCH_BROKER_ENABLED`/`RESEARCH_BROKER_PATH`, capability **por job** emitida por el servicio y revocada al terminar/cancelar (`revoke_all` al apagar), `BrokerReadLog` para que lo que el modelo abre por el broker **sea** evidencia del job, y dos defectos de esquema MCP corregidos (`oauth: false` — un remoto usa OAuth por defecto y dejaba las tools fuera; y `timeout` con las claves **V2** `startup`/`catalog`/`execution`, no las V1). **Smoke real tras reinicio**: con `RESEARCH_BROKER_ENABLED=true` la ruta responde **401 sin Bearer** y `tools/list` devuelve **exactamente** las tres tools; con el valor por defecto (`false`) la ruta es **404** — apagada es la decisión de despliegue, y así se dejó el servicio.

**El circuito completo se demostró de punta a punta:** un modelo real llamó a `research_read`, el broker **leyó `https://example.com/` de verdad**, la evidencia quedó **persistida** en SQLite/FTS5 (`br-f67550e3c9ece54cee19d660`, recuperable por proyecto/owner) y el informe **citó ese `evidence_id`** con un extracto literal. **Pero solo con el perfil de permisos abierto.** Con el perfil **cerrado** que exige la aceptación, **todos** los modelos libres probados (`muse-spark-1.3-contributor-free`, `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`, `longcat-2.5-preview-free`, `mimo-v2.6-flash-free`, `ling-3.0-flash-fin-free`) responden `403 "OpenCode's free tier can only be used from within OpenCode"`, y el único que tolera el perfil cerrado (`space-bunny-free`) **no emite llamadas de herramienta válidas** (las escribe como texto). **No es un fallo de la integración**: el CLI registra `mcp connected server=research-broker tools=3` y el preflight desde dentro del sandbox devuelve 200.

**Tickets (ERPNext, reescritos y comprobados uno a uno):** `00062`/`00063`/`00065`/`00066` quedan **Working con el motivo escrito** — el trabajo está hecho y verificado, pero **00062 no puede completar** (el egress del worker no está restringido por destino y restringirlo exige control de red de host: `CapEff=0`), y la regla de dependencias impide cerrar a sus dependientes. `00067` sigue **Open** con el bloqueo concreto: falta **presupuesto/aprobación** para un modelo registrado (el epic ya fija `RESEARCH_OPENCODE_PROD_MODEL=meta/muse-spark-1.3-contributor`) o habilitar otro motor que tolere el perfil cerrado. **No se relajó el perfil ni se cambió a un modelo pagado para “pasar” la aceptación.**

**Próximo trabajo:** (1) decidir el modelo/motor (con la ruta del modelo registrado, el circuito **ya está listo**: solo hay que cambiarlo y reejecutar el probe); (2) si se quiere cerrar 00062, dar al worker una frontera de red con política (podman con red filtrada, o reglas iptables con `CAP_NET_ADMIN`) — con Docker disponible y el usuario en el grupo `docker` **es viable**, pero exige construir una imagen con el CLI y probar el límite; (3) solo después, iniciar la Entrega B (`00068` en adelante).

Estado al cerrar la sesión del **2026-10-01 (cierre 10)**: **el bloqueo del binario de OpenCode está resuelto y el CLI se usó de verdad** (`/home/develop/.opencode/bin/opencode`, **v2.0.20**; necesita `HOME=/home/develop`, con `HOME=/` falla con `EACCES: mkdir '/.local'`). Se capturaron las **fixtures JSONL reales** (sustituyen a la sintética: éxito mínimo, éxito con herramienta, error de proveedor y `Agent not found`) y eso descubrió **tres defectos reales que la fixture falsa ocultaba**, ya corregidos: (1) `research/worker/profiles.py` emitía los permisos en el formato **V1** (dict `{"bash": "deny"}`) cuando OpenCode V2 exige una **lista ordenada** de reglas `{action, resource, effect}` (gana la última que coincide; <https://opencode.ai/v2/docs/agents>) — el CLI **no avisa del esquema y descarta el agente entero**, así que `--agent investigator` moría con `Agent not found: "investigator"`; (2) el runtime **nunca escribía la configuración del worker**: `build_worker_config` solo se usaba en tests, de modo que el CLI arrancaba con un `XDG_CONFIG_HOME` vacío — ahora `OpenCodeRuntime.run()` materializa `config/opencode/opencode.json` y lo **valida cerrado antes de lanzar** (`RuntimeConfigurationError` si falta el broker o el perfil no está cerrado); (3) el CLI reporta los fallos por **stdout** (evento `type: error`) y deja **stderr vacío**, así que el `RuntimeExecutionError` quedaba sin motivo — ahora `extract_jsonl_error()` recupera `error.type`/`message`/`status`. También se añadieron `RESEARCH_BROKER_URL`/`RESEARCH_BROKER_BEARER` (`settings.py` + `wiring.py`) para que el runtime reciba el broker. **Hallazgo de planificación:** el modelo libre candidato `opencode/muse-spark-1.3-contributor-free` está en el catálogo (79 modelos) y **autentica sin credenciales heredadas**, pero con el perfil **cerrado** que exige la Entrega A responde de forma determinista `provider.auth 403 "OpenCode's free tier can only be used from within OpenCode"` (se dispara al negar `*`, `read`... (line truncated to 2000 chars)
 (`/home/develop/.opencode/bin/opencode`, **v2.0.20**; necesita `HOME=/home/develop`, con `HOME=/` falla con `EACCES: mkdir '/.local'`). Se capturaron las **fixtures JSONL reales** (sustituyen a la sintética: éxito mínimo, éxito con herramienta, error de proveedor y `Agent not found`) y eso descubrió **tres defectos reales que la fixture falsa ocultaba**, ya corregidos: (1) `research/worker/profiles.py` emitía los permisos en el formato **V1** (dict `{"bash": "deny"}`) cuando OpenCode V2 exige una **lista ordenada** de reglas `{action, resource, effect}` (gana la última que coincide; <https://opencode.ai/v2/docs/agents>) — el CLI **no avisa del esquema y descarta el agente entero**, así que `--agent investigator` moría con `Agent not found: "investigator"`; (2) el runtime **nunca escribía la configuración del worker**: `build_worker_config` solo se usaba en tests, de modo que el CLI arrancaba con un `XDG_CONFIG_HOME` vacío — ahora `OpenCodeRuntime.run()` materializa `config/opencode/opencode.json` y lo **valida cerrado antes de lanzar** (`RuntimeConfigurationError` si falta el broker o el perfil no está cerrado); (3) el CLI reporta los fallos por **stdout** (evento `type: error`) y deja **stderr vacío**, así que el `RuntimeExecutionError` quedaba sin motivo — ahora `extract_jsonl_error()` recupera `error.type`/`message`/`status`. También se añadieron `RESEARCH_BROKER_URL`/`RESEARCH_BROKER_BEARER` (`settings.py` + `wiring.py`) para que el runtime reciba el broker. **Hallazgo de planificación:** el modelo libre candidato `opencode/muse-spark-1.3-contributor-free` está en el catálogo (79 modelos) y **autentica sin credenciales heredadas**, pero con el perfil **cerrado** que exige la Entrega A responde de forma determinista `provider.auth 403 "OpenCode's free tier can only be used from within OpenCode"` (se dispara al negar `*`, `read` o `shell`; con `opencode/space-bunny-free` el mismo perfil cerrado **sí** completa). **Bloqueos que quedan y necesitan al dueño/host:** (a) **aislamiento**: este contenedor corre con `CapEff=0` y el perfil seccomp de Docker, así que `unshare` falla en **todas** sus formas (`--user`, `--mount`, `--net`, `--pid` → *Operation not permitted*) y no hay `bwrap`/`podman`/`docker`/socket/`sudo`; el kernel sí lo permitiría (`unprivileged_userns_clone=1`, `max_user_namespaces=125194`), así que el piloto del worker aislado (canario + destino prohibido) y el reinicio del servicio son **acción de host**; (b) el **broker MCP del worker no está montado** en ninguna ruta de `main.py`/`wiring.py`/`router.py` (`build_research_broker` existe y se prueba, pero nadie lo sirve), así que el circuito modelo → tool del broker → lectura → síntesis sigue sin existir; (c) el **modelo**: sin aprobar presupuesto no se cambia a un modelo pagado, y el candidato libre no tolera el perfil cerrado. **Entorno de tests:** el contenedor cambió y **ya no hay Python 3.12**, así que `.venv/` está roto (`.venv/bin/python3` apunta al 3.11 del sistema → *No module named pytest*); la suite se corrió con un entorno reconstruido con `uv` + Python 3.13.1 en `/home/develop/.venvs/jopenceo`. **Baseline sin cambios (`22241a1`) en ese entorno: 813 pasan / 10 fallan / 1 skipped**; con los cambios de este cierre: **828 pasan / 10 fallan / 1 skipped**, los **mismos 10 nombres** (fallos de entorno/dependencias, no regresiones) y **15 tests nuevos**. Estados en ERPNext: `TASK-2026-00062` y `TASK-2026-00063` **`Working`** (implementación avanzada y verificada contra el binario real, pero falta el piloto de aislamiento, el montaje del broker y la decisión de modelo); `TASK-2026-00067` (compuerta A) y el épico `TASK-2026-00056` siguen **`Open`**; `00065`/`00066` siguen `Working` por la dependencia nativa de ERPNext. **Nada se mergeó ni se reinició** (no hay credenciales de GitHub en el contenedor y el servicio vive en otro espacio de PID); los cambios están **sin commit** en el árbol de trabajo. Detalle de la captura real en `docs/work/TASK-2026-00056-investigator-elite.md` (sección «Captura real del CLI de OpenCode — 2026-10-01 (cierre 10)», temporal, git-ignored). **Próximo trabajo:** decidir el modelo y el host con aislamiento, montar el broker MCP del worker, completar el piloto de aislamiento y solo entonces cerrar `00062`/`00063`/`00065`/`00066`/`00067` e iniciar la Entrega B (`00068` en adelante).

Estado al cerrar la sesión del **2026-10-01 (cierre 9)**: **Entrega A del épico research parcialmente implementada: `TASK-2026-00057..00062` implementados** (el estado final de cada ticket se corrigió al cerrar la sesión; ver «Corrección de estados» más abajo) (rama `feat/research-entrega-a`, merge a `main`), con el resultado registrado en cada ticket y el progreso en el épico `TASK-2026-00056`. Suite del repo: **644 pasan / 6 fallan** (los 6 preexistentes, nombres idénticos al baseline `2032943` = 386/6) — **258 tests nuevos** para research. Qué hay en `openceo-core/research/`: `money.py` (dinero como entero escalado en micro-USD; `$0.0025` no se redondea a 0; desconocido=`null`, nunca 0); `budget.py` (reservas **atómicas de un solo documento** con pipeline `$cond`: la decisión y la mutación de contadores ocurren sobre la misma versión, así que dos reservas concurrentes por el último saldo no pueden gastarlo dos veces sin transacción multi-documento — Mongo standalone no la soporta; libro `research_usage` append-only idempotente por `operation_id`; **autoridad de reserva y libro de uso separados**, primero se registra el uso y después se retira la reserva, así un crash deja el lado conservador; timeout ambiguo queda `uncertain` y **no** se cuenta como gasto 0; sin TTL sobre la reserva viva); `contracts.py` (**un solo** enum de estado de 6 valores, **sin** `interrupted`; un `running` huérfano pasa a `partial`/`failed` con causa `runtime_interrupted` y no relanza gasto; fingerprint sin `owner_id` ni clave; `mechanical_issues` que separa integridad mecánica de soporte semántico) + colección `research_jobs` con índice **parcial** de idempotencia scoped por proyecto/owner/propósito (`$type: string`; sin clave se admiten consultas nuevas), claim con lease y heartbeat con fencing, y **ninguna** operación escribe `tasks` ni despierta el observador ERPNext; `policies.py` (owner derivado del principal autenticado — el payload no autoriza; techo de clasificación: pedir por encima se **rechaza**, no se degrada; límites y presupuesto pagado solo a la baja, `min(global, proyecto)` con global `0.00` por defecto; y **guard de egress con pinning**: `PinnedEgressTransport` conecta a la IP ya validada conservando `Host`/SNI y **vuelve a validar cada redirect**, cerrando el TOCTOU de "validar DNS y dejar que el cliente resuelva otra vez"); `acquisition/` (Brave/Serper/Firecrawl + HTTP/RSS; taxonomía de errores con `402` sin retry, `429` con `Retry-After` acotado por deadline y timeout ambiguo con `mark_uncertain`; **el SERP de Brave es transitorio**, solo url y posición; RSS rechaza DOCTYPE/ENTITY; no existe excepción `Duplicate`); `evidence.py` (SQLite/FTS5 por proyecto con `user_version`, WAL, FTS5 verificado con `MATCH` real, **scope owner/proyecto/clasificación aplicado ANTES del MATCH**, traversal/symlinks rechazados, retención 7/90/180 disparada al operar **sin barridos** y tombstone que dice que una evidencia ya no es recuperable en vez de devolver vacío); `runtimes/` (argv V2 sin `--dir`/`--command`/`--continue`, JSONL tolerante, **exit 0 sin texto útil es error**, deadline que mata el **grupo** de procesos, entorno del worker por allowlist sin HOME host ni claves ni MCP de infraestructura, y **aislamiento obligatorio**: sin backend real el runtime se niega a ejecutar en lugar de fingir sandbox). El test del deadline encontró un bug real (la espera del proceso estaba fuera del deadline) y quedó corregido. `tests/conftest.py` carga `motor` antes de que un módulo de test lo sustituya por un stub (contaminación global preexistente que hacía fallar los tests de Mongo solo en la suite completa). **Nada de esto está montado aún en `main.py`**: la fachada API/MCP y el servicio son `00065`/`00066`, así que no hay capacidad viva que activar. **Bloqueo concreto para `TASK-2026-00067`:** este contenedor no tiene el binario OpenCode (`/home/develop/.opencode/bin/opencode`), ni backend de aislamiento (los user namespaces están denegados: `unshare --user` → *Operation not permitted*), ni claves de Brave/Serper/Firecrawl/Meta; además la fixture JSONL es **sintética** (documentado en `tests/fixtures/README.md`). El circuito real Muse → herramienta → fuente → síntesis citada y el piloto de aislamiento **no pueden ejecutarse aquí** y no se fuerza su cierre. **Servicio no reiniciado:** el proceso (pid 648787, del cierre 8) vive en otro namespace (`/proc/<pid>` no visible y `ps` ausente), así que el restart queda como acción del host; smoke sobre el servicio en ejecución: `/openapi.json` 200 y `/health` `{"status":"ok"}`.

Tercera tanda de la misma sesión (**2026-10-01, cierre 9**): **`TASK-2026-00066` implementada y `TASK-2026-00067` intentada sin cerrarla**. `00066` — `research/router.py` (POST /api/research/jobs → 202 + `Location`, GET y cancel, sobre `get_current_user`/`require_permissions`; owner del principal, job ajeno = **404 y no 403**, `Idempotency-Key` como encabezado con **409** si el cuerpo cambia, 422 de validación, respuesta por lista blanca), `research/consumers.py` (credencial de consumidor propia: token opaco hasheado, owner explícito, proyectos y operaciones concedidos, revocación y vencimiento), `research/mcp_facade.py` (**la credencial se verifica en el transporte**, cabecera `Authorization` del MCP; sin cabecera se rechaza; ni `owner_id` ni `_meta.sessionID` participan) y `research/wiring.py` + `main.py` (router montado, servicio construido en el `lifespan` **aunque falten claves o CLI** para poder explicar el motivo, cerrado antes que el repositorio, y montura MCP tras `RESEARCH_MCP_ENABLED`, apagada por defecto). **`TASK-2026-00067` queda Open**: la aceptación exige el circuito real modelo → tool MCP del broker → lectura → síntesis citada y un smoke tras reinicio, y este entorno no tiene binario de OpenCode, ni backend de aislamiento (user namespaces denegados), ni claves, ni puede reiniciar el servicio. Lo que sí se verificó, en `tests/test_research_acceptance_a.py` (12 pasan + 1 smoke de red opt-in ejecutado una vez): ciclo compuesto con repositorio **Mongo**, ledger y almacén de evidencia **SQLite/FTS5 reales**; aislamiento de evidencia entre owners; cuota agotada, cita ajena → `partial` (nunca `completed`), Firecrawl deshabilitado, presupuesto 0 con **cero llamadas pagadas**, reserva concurrente por el último saldo con $0,0025 exactos, cancelación tras finalizar, recuperación de huérfano sin estado `interrupted`; y **lectura real de `https://example.com/`** por el guard de egress (200, hash, `observed_at`, conexión fijada a la IP validada conservando `Host` y SNI). Esa prueba real encontró un **bug que los fakes ocultaban**: `PinnedEgressTransport` fallaba con el transporte real de httpx al pasar el `stream` original; quedó corregido y con regresión offline. Suite: **789 pasan / 6 fallan** (los 6 preexistentes) **/ 1 skipped** y **404 tests de research**. **Corrección de estados (regla del dueño, 2026-10-01):** había puesto `00057`–`00066` en **`Pending Review`**, y era incorrecto. `Pending Review` es **solo** para lo que necesita atención humana (generar un token o una credencial, o aprobar algo que el agente no puede producir); revisar flujos, leer el diff o correr los tests **no** lo justifica, porque eso lo hace el agente. Causa mecánica: `ERPNextTicketAdapter.update_status(name, "completed")` **sin contexto** aplica el defecto seguro y empuja `Pending Review`; el destino correcto solo se resuelve aportando `qa_review_required`/`qa_review_state` del ticket. Hallazgo que cambia la planificación: **ERPNext sí hace cumplir `depends_on`** (error 417 *"Cannot complete task … as its dependant task …"*), así que los estados se ordenan topológicamente y un bloqueo intermedio arrastra a sus dependientes. Estados corregidos y ya aplicados: **`Completed`** `00057`, `00058`, `00059`, `00060`, `00061`, `00064`; **`Working`** `00062` y `00063` (exigen CLI de OpenCode + aislamiento) y, por dependencia, `00065` y `00066` (implementación terminada y verificada, pero ERPNext no permite completarlas antes que sus predecesoras); **`Open`** `00067` (compuerta A) y el épico `00056`. Cada ticket lleva escrito su «Estado y criterio» en la descripción. Criterio durable en el vault: `decisions/2026-10-01-criterio-estados-tickets.md`.

**Próximo trabajo:** conseguir un host con CLI de OpenCode, aislamiento disponible y —solo si se quiere el smoke pagado— credencial de Meta; capturar la fixture JSONL real, ejecutar el probe del worker con canario, reiniciar el servicio, cerrar `00062`/`00063`/`00065`/`00066`/`00067` y solo entonces iniciar la Entrega B (`00068` en adelante).

Continuación de la misma sesión del **2026-10-01 (cierre 9, segunda tanda)**: **`TASK-2026-00063/64/65` implementados** (estado final corregido al cerrar; ver «Corrección de estados») (mismos bloqueos de entorno: sin CLI de OpenCode, sin backend de aislamiento, sin claves de proveedor). `00064` — `research/acquisition/grounded.py`: grounding opcional contra el endpoint fijo de Meta `/responses` con `tools=[{type: web_search}]` e `include=[web_search_call.results]`; distingue **habilitar la herramienta de haber buscado** (`tool_enabled` vs `searched`, con hueco de cobertura y aviso si el cliente exige actualidad); las `url_citation` son **evidencia derivada** (`derived_evidence()` con `content_hash=None` y `access_method=grounding`, para no fingir lectura ni hash del artículo); pago por defecto 0 = 0 llamadas; clasificación distinta de `public` se degrada como `model_policy_unavailable` **sin** fallback a Contributor; liquida desde el uso reportado (búsquedas a $0,0025 + tokens, redondeo al alza al micro) y deja `reconciled=null` **incierto** si falta el contador, nunca inferido del número de resultados; no promete tope exacto (`hard_cap=false`). Deliberadamente **no** usa `run_provider_call`: ese motor liquida el coste **estimado** y aquí el coste real solo se conoce después, así que hace su propia reserva y liquidación. Se añadió `ModelPolicyUnavailableError` a `acquisition/base.py` para que la negativa de privacidad tenga su propia causa de cobertura en vez de confundirse con un fallo de configuración. `00063` — `research/worker/`: **capabilities** cortas por job (token opaco guardado **hasheado**, TTL, revocación explícita y `revoke_for_job()`, autorización **por operación**), **perfiles V2 cerrados** (deny por defecto, allow solo para las tres tools del broker, `websearch=false`, skills y plugins deshabilitados, `mcp.servers` con un único servidor, `codemode=false` y `timeout` como **objeto** connect/request que cabe en el deadline, más `assert_worker_config_is_closed()`) y **broker MCP** FastMCP con exactamente `research_discover`, `research_read` y `research_grounded_lookup`: el alcance (proyecto/owner/research_id/clasificación) sale **siempre** de la capability — no es argumento de ninguna tool y `_meta.sessionID` no se lee en ningún punto —, la lectura vuelve `trust=untrusted` y un adaptador ausente responde `missing_config` sin romper el catálogo. `00065` — `research/service.py`: admisión que **persiste antes de responder**, despacho con **claim atómico** (lease) y un único despachador documentado, **slots separados** de jobs y de llamadas al modelo (2 y 2), **límites reales** contados en el servicio (consultas, páginas, pasos y deadline), **partial honesto** (cita inventada, sin soporte o **de otro job** nunca acaba en `completed`) y cancelación por CAS coherente con una carrera frente al final; recuperación de `running` huérfano a `partial`/`failed` con causa `runtime_interrupted`, sin estado `interrupted` ni relanzamiento facturable. Dos tests encontraron bugs reales en esta tanda: el adaptador de grounding devolvía `source_unavailable` en vez de `model_policy_unavailable` para datos no públicos, y `tests/conftest.py` tuvo que cargar **también** `fastmcp` temprano (otro módulo de test lo sustituye por un stub, igual que hacía con `motor`), porque el catálogo del broker solo fallaba en la suite completa. Suite: **739 pasan / 6 fallan** (los 6 preexistentes, idénticos al baseline `2032943`) y **353 tests de research**.

Estado tras la **auditoría del 2026-10-01**: **TASK-2026-00056..00076 revisados y corregidos en ERPNext y en sus descripciones de ejecución OpenCEO**, conservando los 21 números, `Open/queued` y `autorun=false`. La arquitectura A–D se conserva, pero el plan original no era ejecutable literalmente: tenía contradicciones de estado, garantías incompletas de presupuesto/concurrencia, autorización MCP sin identidad de owner, aislamiento/SSRF insuficientemente definidos y aceptaciones que permitían cerrar con fakes. El épico ahora es **grupo real con 20 hijos, 70 dependencias de orden entre hijos y 20 de cierre del épico (90 relaciones nativas sin ciclos)**; `00063` depende de grounding `00064`. **El runtime actual no hace cumplir las dependencias:** comprobar predecesores antes de ejecutar. Corregidos precisión subcentavo y reservas atómicas, idempotencia/frescura, scope owner de evidencias/subs, contexto research de workflows sin tickets por tick, timer sin consultas periódicas en reposo, control/cancelación de subs y piloto X con sesión/espera acotadas. A/C/D exigen evidencia real; claves opcionales no bloquean desarrollo. Hechos contrastados: OpenCode **v2.0.20**, sin `--dir` ni `--command` en help; catálogo incluye `opencode/muse-spark-1.3-contributor-free` (sin ejecutar LLM en esta auditoría); SQLite ya existe en el runner de QA; Horizon reconsulta tras su max_sleep de 3600 s. Tarifas/Muse/Responses confirmadas en fuentes oficiales, como referencias fechadas, no saldo medido. Auditoría y precedencia de tickets sobre borrador en `docs/work/TASK-2026-00056-investigator-elite.md` (temporal, git-ignored). **Próximo trabajo: TASK-2026-00057**, configuración y presupuesto; no requiere todas las claves para empezar. Revisión de planificación, sin código de aplicación, merge/restart ni activación de gasto.

Estado al cerrar la sesión del **2026-09-28 (cierre 7)**: **TASK-2026-00053/54 mergeados a `main`** (merge `61940b5`, pusheado) y **servicio reiniciado** (pid 3811539, openapi 200). Finance ya escribe ingresos y lee la regla del 10%: `finance.register_income` (el mismo `Journal Entry` idempotente por `openceo_ref`, prefijo `voice-income:`, patas invertidas: débito `Efectivo - J` / crédito a la cuenta de ingreso **real y verificada**: `Servicios - J` por defecto, `Ventas - J`, `Interest Income - J`, `Interest on Fixed Deposits - J`; ninguna cuenta nueva) y `finance.get_contribution_10_status` (solo lectura, `Decimal`, ingreso elegible = créditos a cuentas de ingreso del período). **Hallazgo que define el diseño:** el plan de cuentas de Jardal **no tiene cuenta de aportes/donaciones**, así que lo aportado se devuelve `None` con `state="needs_input"` y su razón, **nunca 0**; definir esa cuenta cambia qué significa la medición y es decisión del dueño (cuando exista, se lista en `CONTRIBUTION_ACCOUNTS` y pasa a `ok` sin más cambios). **Despachador único** `agents/finance/intent.py` fija el orden ingreso→aporte→saldo→gasto (los verbos se solapan: "registra ingreso 500 bs" cumplía el detector de gasto) y **Horizon ahora lo consulta** en vez de duplicar vocabulario: sin esa guarda el daño no era solo una nota mal archivada — Horizon dejaba pasar la frase y Finance la contabilizaba con el **signo invertido** ("registra ingreso sueldo 6200 bolivianos" → débito a gasto / crédito a efectivo); la captura personal era la parte visible, no la peor. (Corregido el 2026-09-29 tras la revisión: el rationale anterior describía mal el comportamiento de base). Reglas nuevas: una orden de pago anula un sustantivo de ingreso suelto ("pago de sueldo 3000 bs" es gasto → `Salario. - J`) y **una pregunta no registra nada**. Parseo de dinero e idempotencia del asiento extraídos a `money_text.py`/`journal.py`, compartidos por gasto e ingreso; de paso corrige el orden `usd`/`usdt` que dejaba una "t" suelta en el concepto. Suite: **365 pasan / 7 fallan** (los 7 preexistentes, nombres idénticos al baseline; +43 tests nuevos). En vivo: `ACC-JV-2026-00013` (ingreso Bs 1, débito Efectivo / crédito Servicios) y `ACC-JV-2026-00014` (gasto Bs 1, regresión del refactor compartido), saldo 13.00 BOB en 8 asientos, diezmo 1.00→0.10, y "¿cuándo me pagaron los 500 dólares?" **no** escribió nada (5 `Journal Entry` antes y después). Reinicio sin ejecución fantasma de los tickets `autorun=false`. Ambos tickets en ERPNext `Completed` y ejecución `completed`: **el webhook de Frappe propagó solo** al poner `Completed` por REST, sin parchear Mongo a mano (a diferencia del cierre 5). Vault: `components/finance-erpnext-adapter.md` (§Ingreso y regla del 10%, §Despachador único) + `components/voice-first-layer.md` (check 0 errores, 2 notas); **sin commit del vault** — el guard del daemon bloquea git fuera del repo de la sesión. Siguiente corte: presupuestos por Scope (Fase D) **bloqueado en dos decisiones del dueño** (`OpenCEO Scope` sigue vacío —el eje operativo es Cost Center— y no hay cuenta de aportes); sin bloqueo quedan `finance.register_internal_transfer` y `finance.list_pending_obligations`, y `TASK-2026-00042` (ruteo de revisor) sigue en `queued`.

Estado al cerrar la sesión del **2026-09-29 (cierre 8)**: **Revisión medium sobre el diff `7a5e176..03f6a7b` → Request changes (12 Critical, 6 Suggestion, 4 baja confianza; artefacto `.qwen/reviews/2026-09-29-013158-local.{md,json}`)** y **los 22 hallazgos corregidos en código+tests+docs** (ticket ERPNext `TASK-2026-00055`). Lo que estaba mal y quedó arreglado: (1) **dinero**: "6.200" se leía 6.20 (1000× menos) y ahora son 6200; `"10 %"` ya no es monto; `$500` es USD (el `\$` estaba dentro de `\b…\b` y era inalcanzable); el importe es el número con moneda pegada, no el primero ("recibí 3 pagos de 500 bolivianos" ya no contabiliza 3.00); y una moneda distinta a la de la Company **exige la tasa real o no se escribe nada** (fail-closed; antes "me pagaron 500 dólares" entraba como Bs 500, contrato §11); (2) **signo**: la frontera ingreso/gasto exige señal de cobro real — fuera "pagaron" desnudo y los sustantivos sueltos, que invertían egresos ("pagaron la luz 200 bs") y contabilizaban declarativas ("el sueldo sube a 3000 el próximo mes"); `is_question` solo si el interrogativo abre la frase (la voz no trae tildes: "registra como gasto 50 bs" ya se registra); `parse_income`/`parse_expense` aplican las guardas del módulo, así que el tool ya no escribe preguntas, tráfico dev ni egresos invertidos; (3) **lecturas honestas**: el saldo deriva el período del dictado ("este mes") y lo dice, y `limit_reached`/`excluded_entries` llegan a la voz en las dos ramas de la regla del 10%, que además ya no dice "cobramos" porque la medición no verifica la contrapartida de caja (son créditos de ingreso, un devengado cuenta igual); el detector del 10% ya no intercepta órdenes de gasto ("aporte patronal", "10 % de recargo"); (4) **idempotencia real**: `finance.register_income`/`register_expense` derivan `openceo_ref` de la tarea (`voice-income:<task_id>`), así que un re-despacho (POST /run con force) o un parche perdido a Mongo ya no duplican el asiento; (5) **Horizon**: la guarda solo reclama escritura y regla del 10% (no la lectura de saldo, que es vocabulario ordinario) y loguea el fail-open. **Suite: 413 pasan / 7 fallan** (los 7 preexistentes, nombres idénticos al baseline `03f6a7b`: 365/7). Mutantes verificados: borrar la rama INCOME del despacho, borrar el `finally: aclose()` y quitar el ref derivado ponen tests en rojo. Contrato §6.1/§11 reforzados (mismo re-despacho = mismo ref; sin tasa real no hay asiento). Vault: `components/finance-erpnext-adapter.md` (§Ronda de corrección R1) + `components/voice-first-layer.md` (guard 0 errores). **Servicio reiniciado** (pid 648787, openapi 200, smokes de voz: balance con período, aporte honesto, USD rechazado). Ambos tickets en ERPNext `Completed` y ejecución `completed`.

Estado al cerrar la sesión del **2026-09-28 (cierre 6)**: **TASK-2026-00046/47/48 mergeados a `main`** (merge `2fadb5c`, pusheado) y **servicio reiniciado** (pid 3479283, openapi 200, smoke de voz OK: 11.00 BOB en 4 asientos). Pulido de la revisión: `excluded_entries` visible en `to_dict` y voz, flag `limit_reached`, fake estricto en tests, nits (docstring, `_norm` fuera). Suite: 322 pasan / 7 fallan (preexistentes). Los 3 tickets en ERPNext `Completed`, ejecución `completed`. Vault actualizado (misma nota, check pendiente). Siguiente: ingresos (`register_income`) o presupuestos; `TASK-2026-00045` paraguas cancelado (`Cancelled`/`cancelled`, sin reanudación).

Estado al cerrar la sesión del **2026-09-28 (cierre 5)**: **TASK-2026-00044 mergeado a `main`** (merge `d905dfd`, pusheado) y **servicio reiniciado** (pid 3329776, openapi 200). Finance lectura: `finance.get_scope_summary` (solo lectura sobre `GL Entry`, Decimal, `as_of`/company/moneda/filtros §15, sin ledger paralelo) + voz "cuánto gastamos/saldo" sin tickets; `GL Entry` se lee por `list_gl_entries` sin entrar a la allowlist de escritura. Suite: 321 pasan / 7 fallan (preexistentes, verificados idénticos). En vivo: resumen 11.00 BOB debe/haber en 4 asientos (incluye `ACC-JV-2026-00012` pan Bs 10 del smoke); gasto por voz intacto. Ticket en ERPNext `Completed`, ejecución `completed`. Lección: parchear Mongo fuera del servicio no dispara al observador ERPNext (vive en el proceso del servicio); la transición perdida se reparó empujando `Completed` por REST + espejo `status_pushed` params-only. Vault `finance-erpnext-adapter.md` §Primera lectura (check 0 errores). Siguiente corte: ingresos (`register_income`) o presupuestos por Scope. Recomiendo borrar `docs/work/TASK-2026-00044-finance-lectura.md` con aprobación del dueño (está git-ignored, solo local).

Estado al cerrar la sesión del **2026-09-28 (cierre 3)**: **TASK-2026-00041 mergeado a `main`** (merge `c2b5170`, pusheado). Bandeja central `/dashboard/inbox` (`HumanInbox.jsx`, chunk 6.2 kB, `npm run build` OK): waiting de cualquier fase + QA pendiente/solicitada, filtros Todos/QA/Aprobaciones/Cliente, acciones aprobar/cambios por `phase-action` (el servidor ya exigía permiso por fase). Sin cambio de backend → sin restart del core. Hallazgo: `QADashboard.jsx` estaba huérfana (sin ruta, `/dashboard/review` redirigía a inicio); el menú `Revisión Cliente` ahora apunta a la bandeja. Rama local borrada. Vault `ticket-erpnext-adapter.md` §Bandeja humana (check 0 errores). Pendiente del dueño: ver la página en navegador (contenedor react apagado) y `User Permissions` por proyecto en el desk para clientes. Pendiente futura sesión: `TASK-2026-00042` (asignar revisor automático al pedir revisión — `involved_user_ids` + `_assign`; la bandeja filtra pero nadie rutea).

Estado al cerrar la sesión del **2026-09-28 (cierre 4)**: **TASK-2026-00043 mergeado a `main`** (merge `1ba05cd`, pusheado) y **servicio reiniciado** (pid 2928333, openapi 200). Causa del Working fantasma: `TaskDocument` no tenía campo `autorun`, el `False` del registro se perdía (`None`) y `resume_queued_tickets` lo lanzaba igual (default `True`) — el router lo mandó a secretary y falló. Fix: `autorun` persistido (`False` = no ejecuta, `None` legacy = `True`); regla 6 en `CLAUDE.md` §8: **Working solo con ejecución real**. `TASK-2026-00042` reparado a `queued`/`Open` + `autorun` False; `TASK-2026-00043` frenado en `queued` antes de que lo cazara. Rama local borrada. Suite: 314 pasan / 7 fallan (preexistentes). Nota: `test_task_service_queue.py` no colecta solo (mock `pymongo` preexistente); en suite completa sí.

### Qué se hizo (2026-09-28, TASK-2026-00032)

"Registra gasto personal carne 50 bolivianos" / "pago de luz de casa 300 bs" por voz/chat (modelo CEO) se registra directo en ERPNext como `Journal Entry` idempotente (`openceo_ref`, retry = 417 = releer). Sin tickets por gasto. Detalle temporal en `docs/work/TASK-2026-00032-finance-voz-gastos.md` (git-ignored, solo en este working tree); durable en vault `components/finance-erpnext-adapter.md`. Suite: 308 pasan / 7 fallan (preexistentes). En ERPNext quedan `ACC-JV-2026-00011` (Bs 1.00 enviado, registro inaugural) y `ACC-JV-2026-00009` (probe cancelado, neto cero). Siguiente corte recomendado: lectura (`get_cash_position`/resumen por Scope) antes de ingresos o presupuestos. Palabra clave para retomar: **"Continúa con TASK-2026-00032"**.

Estado al cerrar la sesión del **2026-09-24**: **los tickets de OpenCEO se crean en ERPNext** y
el backlog de `PENDINGS.md` se migró. `PENDINGS.md` ya no es el backlog: quedó como puntero.

### Qué se hizo (2026-09-24)

Se ejecutó el plan de redirección de tickets a ERPNext, en 6 fases. **No hay flag de
encendido/apagado**: ERPNext es el registro de negocio de los tickets, así que el flujo está
cristalizado. El servicio corre con el código nuevo.

| Fase | Qué | Verificación |
| --- | --- | --- |
| 0 | **Credenciales movidas a su sitio.** El bootstrap del 2026-09-22 dejó `.env.finance.local` en la **raíz** del repo, pero `settings.py:14` lee `openceo-core/.env.finance.local`; pydantic ignora un `env_file` inexistente, así que `ERPNEXT_URL` llegaba **vacío** y el fallo habría parecido "ERPNext caído". También hubo que **declarar** los campos `ERPNEXT_*` en `settings.py` (con `extra="ignore"` una variable no declarada se descarta en silencio) | `Settings().ERPNEXT_URL` ya no está vacío |
| 0 | **Rol `Projects User`** al usuario API de ERPNext. Antes `GET /api/resource/Task` daba **403** | Ahora **200** en `Task` y `Project`; `if_owner=0` y `create=1` comprobados en `tabDocPerm` |
| 0 | Campos custom `Task.openceo_ref` (unique), `Task.openceo_ticket_number`, `Task.openceo_task_id`, `Project.project_key` (unique) y 4 `Task Type` | Índices `Non_unique=0` y `Null=YES` en MariaDB |
| 1 | `openceo-core/tools/erpnext_client.py` (HTTP determinista, async, timeouts 2/5 s) y `tools/erpnext_tickets.py` (operaciones semánticas) | 45 tests |
| 2 | **`TaskService.create_task` crea el ticket de negocio ANTES de persistir en Mongo**, con las tres ramas de resultado | En vivo: éxito, idempotencia por 417 y ERPNext caído |
| 3 | Transiciones por **observador en `MongoRepository.patch_task`** (el único camino por el que pasan las 7 vías que escriben estado) + cola con reintento + reconciliación de arranque | En vivo: `in_progress→Working`, `waiting→Pending Review`; `completed` **no** se empuja |
| 4 | **27 items de `PENDINGS.md` migrados** a ERPNext, auditados contra el código | 27 creados, segunda corrida: 0 creados / 27 existentes |
| 5 | **Camino inverso**: webhook de Frappe → `POST /api/integrations/erpnext/task` → el runtime reacciona | En vivo: cerrar→`completed`, cancelar→`cancelled`, borrar→`cancelled` |

**Tests:** baseline 7 fallan / 236 pasan → ahora **7 fallan / 289 pasan**. Los 7 son los
preexistentes diagnosticados abajo; **cero regresiones**. `npm run build` del dashboard OK.

### El camino inverso (Fase 5)

Si alguien **cierra, cancela o borra** un `Task` en el desk de ERPNext, OpenCEO se entera. Lo
hace un **webhook de Frappe** (`on_update` + `on_trash`), no un sondeo: AGENTS.md §5.1 prohíbe
los barridos periódicos y el contrato exige que la actividad humana llegue por eventos
explícitos.

| Estado en ERPNext | Estado en OpenCEO | Por qué |
| --- | --- | --- |
| `Completed` | `completed` | El humano aceptó el trabajo |
| `Cancelled` | `cancelled` (nuevo) | El negocio dijo que no; es terminal y **no reanuda** (se limpia `resume_at`) |
| borrado (`on_trash`) | `cancelled` | El ticket ya no existe en el negocio |
| `Open`/`Working`/`Pending Review`/... | sin cambio | Son **eco** de lo que OpenCEO ya empujó: propagarlos sería un bucle |

**Guardia anti-eco estructural:** el parche escribe `status_pushed` con el mismo valor que el
estado nuevo, así que aunque el observador vea el cambio, no lo reenvía a ERPNext.

**`cancelled` es un estado nuevo de `TaskStatus`** (no se reutilizó `waiting`, que implica que
algo lo va a desbloquear, ni `failed`, que implicaría que falló la ejecución). Se añadió a
`_TERMINAL_TASK_STATUSES` y a los mapas de etiquetas de `TicketList`, `TicketDetail` y
`QADashboard`.

**Infraestructura que hubo que tocar** (fuera de OpenCEO, reportado por transparencia):

1. **Regla `ufw`**: `allow from 172.23.0.0/16 to any port 18765 proto tcp`. UFW tiene política
   `DROP` y ya existían dos reglas idénticas para `172.20.0.0/16` y `172.19.0.0/16`; se extendió
   el mismo patrón a la red `jardal-erpnext-net`. Sin esto el contenedor de ERPNext no alcanza el
   servicio.
2. **Dos `Webhook` en ERPNext**: `OpenCEO Ticket Sync` (`on_update`) y `OpenCEO Ticket Trash`
   (`on_trash`), ambos a `http://172.23.0.1:18765/api/integrations/erpnext/task` con la cabecera
   `X-OpenCEO-Webhook-Secret`.

**Secreto del webhook:** `ERPNEXT_WEBHOOK_SECRET` en `openceo-core/.env`. Si falta, el endpoint
responde **503**; con secreto incorrecto, **401**. Se compara con `hmac.compare_digest`.

### Capacidades MCP para los agentes (Fase 6)

Un agente que solo puede **crear** tickets duplica trabajo: no tiene forma de saber si ya existe
uno que lo cubra. Se añadieron dos tools de lectura al **MCP interno** (el global sigue solo con
DNS):

| Tool | Qué hace |
| --- | --- |
| `openceo_search_tickets` | Busca por texto en el asunto, filtra por `project` (project_key) y `status`, y **enriquece con el estado de ejecución de Mongo** cuando el ticket llegó a ejecutarse. Es de solo lectura: un `project_key` desconocido devuelve lista vacía, **no crea el proyecto** |
| `openceo_get_ticket` | Lee uno por `TASK-2026-000NN` o por su `openceo_ref` |

**`autorun` ahora es explícito y por defecto `false`** en los tres tools de creación. Antes los
tools MCP no pasaban `autorun` y heredaban el `True` del modelo, así que **registrar backlog
lanzaba ejecución** — con `TASK_AUTORUN=true` en el `.env`. Ahora registrar no ejecuta: hace falta
`autorun: true` explícito, y la descripción del tool lo dice.

**Los dos mounts MCP exponen ahora lo mismo.** El global tenía solo DNS, y era la única
superficie que veían las **sesiones manuales de Codex CLI** (`mcp_servers.openceo-global` en
`~/.codex/config.toml`): un agente fuera de OpenCEO no podía ni buscar ni registrar trabajo.
Claude Code manual ya los tenía porque lee el interno (`~/.claude.json`). La diferencia entre
mounts es **quién los recibe**, no qué contienen. `MCP_SERVER_ALLOWED_IPS` es loopback-only y
ninguno está publicado en Traefik.

**No hay documento de tickets en este repo a propósito.** El canónico es
`jardal-knowledge/README.md` y llega a todos los proyectos por los cargadores de nivel usuario
(abajo). Un archivo por proyecto sería una tercera fuente que deriva.

### Visibilidad del provider en el tablero (2026-09-25)

Dos campos custom nuevos en `Task`, para que el tablero de ERPNext no oculte lo que decide
cuánto cuesta un ticket:

| Campo | Tipo | Por qué |
| --- | --- | --- |
| `openceo_coding_tool` | Select (`codex`/`claude`/`glm`/`gemini`/`opencode`/`kimi`/`tier_s`) | Sin él no se ve que un ticket es **Tier S** — lento, caro y con revisores múltiples |
| `openceo_external_url` | Data (URL) | El enlace a donde nació la petición |

`map_coding_tool` **omite** un provider que no esté en el enum: el campo es un Select y un
valor inválido haría fallar el POST, perdiendo el ticket entero por un dato de ejecución.

**Lo que sigue sin viajar a ERPNext, y está bien que no viaje:** fases de workflow,
aprobaciones, QA, visibilidad de cliente, sesiones de coding y trazabilidad. Son *cómo se
ejecuta* y su casa es MongoDB; el vínculo es `openceo_ref` + `openceo_task_id`. Comparado con
lo que el espejo de Notion sí mostraba (`agent`, `approval_state`, `runtime_summary`,
`external_url`), ERPNext sigue mostrando menos — por eso `external_url` se agregó, y por eso
`runtime_summary` y `last_sync_at` se dejaron fuera a propósito: cambian en cada parche.

### Memoria de ingeniería centralizada

`jardal-knowledge/README.md` es la **autoridad** (reglas e instalación centralizadas):
los cuatro dominios con sus casas, las reglas de tickets y cuándo usar el vault. El problema
era que un `CLAUDE.md` solo lo lee quien trabaja **dentro** de esa carpeta, y no existía
ningún cargador de nivel usuario — así que no llegaba a ningún proyecto. Ahora sí:

| Agente | Cargador |
| --- | --- |
| `clauded` | `~/.claude/CLAUDE.md` — el archivo |
| `claudep`, `claudeg` | `~/.claude-deepseek/CLAUDE.md` — symlink al anterior |
| `claudej` | `~/.claude-j/CLAUDE.md` — symlink al anterior |
| Codex CLI | `~/.codex/AGENTS.md` |

Un solo archivo físico con symlinks, así los perfiles no pueden divergir. Cada wrapper
`claude*` fija su propio `CLAUDE_CONFIG_DIR`, que es la razón de un symlink por perfil.

### El índice de la migración (`TICKET-NNN → TASK-2026-NNNNN`)

Los 27 items viven ahora en ERPNext (`https://erp.jardal.app/app/task`) con
`openceo_ref = "pendings:TICKET-0NN"`, todos en el proyecto `jardal-openceo`.

| PENDINGS | ERPNext | Estado auditado | PENDINGS | ERPNext | Estado auditado |
| --- | --- | --- | --- | --- | --- |
| TICKET-001 | TASK-2026-00001 | Completed | TICKET-015 | TASK-2026-00015 | **Cancelled** |
| TICKET-002 | TASK-2026-00002 | Open | TICKET-016 | TASK-2026-00016 | Open |
| TICKET-003 | TASK-2026-00003 | Completed | TICKET-017 | TASK-2026-00017 | Open |
| TICKET-004 | TASK-2026-00004 | Open | TICKET-018 | TASK-2026-00018 | Completed |
| TICKET-005 | TASK-2026-00005 | Completed | TICKET-019 | TASK-2026-00019 | Open |
| TICKET-006 | TASK-2026-00006 | Completed | TICKET-020 | TASK-2026-00020 | Working |
| TICKET-007 | TASK-2026-00007 | Completed | TICKET-021 | TASK-2026-00021 | Working |
| TICKET-008 | TASK-2026-00008 | Completed | TICKET-022 | TASK-2026-00022 | Working |
| TICKET-009 | TASK-2026-00009 | Working | TICKET-023 | TASK-2026-00023 | Open |
| TICKET-010 | TASK-2026-00010 | Working | TICKET-024 | TASK-2026-00024 | Open |
| TICKET-011 | TASK-2026-00011 | Open | TICKET-025 | TASK-2026-00025 | Working |
| TICKET-012 | TASK-2026-00012 | Open | TICKET-026 | TASK-2026-00026 | Working |
| TICKET-013 | TASK-2026-00013 | Open | TICKET-027 | TASK-2026-00027 | Open |
| TICKET-014 | TASK-2026-00014 | **Cancelled** | | | |

**7 `Completed`** (001, 003, 005, 006, 007, 008, 018): figuraban como pendientes y **ya estaban
implementados** — verificado en código, no en el markdown. **2 `Cancelled`** (014, 015): absorbidos
por TICKET-026; migrarlos como abiertos habría resucitado decisiones cerradas (la colección
`objectives` paralela viola D-006 y el cron de barrido viola AGENTS.md §5.1).

### Decisiones del dueño en esta sesión

- **DocType `Task`, no `Issue`, no un DocType custom.** `Task` es árbol (`is_tree=1`) y admite
  campos custom, `type` y `_user_tags`: **no es estático**. Un DocType custom obligaría a
  reconstruir a mano el árbol con roll-up, `depends_on`, los campos de Timesheet y el enlace a
  `Project`.
- **`Issue` queda reservado, no descartado**, para trabajo **entrante** con reloj de SLA (un
  cliente o ayudante reporta algo). Se enlaza al mismo `Project`. No recibe `openceo_ref` todavía.
- **`Project` es la identidad de trabajo; `OpenCEO Scope` sigue siendo el eje financiero.** No
  compiten: son ejes distintos. Esto **actualiza** `§3.2` y `§13` de la decisión financiera final
  de `jardal-erpnext` y `§14` del contrato de Finance, que quedaron marcados como superados.
- **Notion sigue encendido** hasta que ERPNext esté probado; el análisis de eliminación quedó como
  ticket (TASK-2026-00028, ver abajo).

### Hallazgos que no son de esta funcionalidad pero afectan cómo se verifica

- **`params.initial_status` es código muerto.** `mcp_server.py:205` lo escribe y **nadie lo lee**:
  el `status` que acepta `openceo_create_ticket(status=...)` no hace nada hoy; el ticket nace
  siempre `queued`. El adapter no heredó esa mentira.
- **`_user_tags` no se puede escribir por REST.** Frappe descarta esa columna en un POST (la
  administra el motor de tags) y `frappe.client.add_tag` no existe en esta versión. Las etiquetas
  van al cuerpo del ticket; hacerlo bien exigiría documentos `Tag Link` (una llamada HTTP por
  etiqueta) en el camino del turno de voz. Decidido no pagarlo en v1.
- **`Task.is_calendar_and_gantt=0` y `track_changes=0`** en este build: las vistas Gantt/Calendario
  y el historial por campo no vienen encendidos. Son casillas, no funcionalidad perdida.

### Pendientes de esta iniciativa

| Pendiente | Tipo | Nota |
| --- | --- | --- |
| **`parent_task` no se mapea** | Límite declarado de v1 | El árbol de ERPNext queda plano aunque OpenCEO tenga jerarquía real (spillover, batches de PM) |
| **`_assign`/usuarios y aprobaciones de negocio** | Diferido | `qa_review` y `client_signoff` siguen en el dashboard como interfaces de compatibilidad |
| **Apagar Notion** | Espera al análisis | Creado como ticket en ERPNext |
| Los 52 tickets históricos de Mongo | **No se migran a propósito** | Son registro de ejecución, no negocio |

**Cosas que harán perder tiempo si no se saben:**

- **Los tests necesitan el venv del root**: `./.venv/bin/python -m pytest tests/ -q`. Línea base al
  cerrar: **289 pasan / 7 fallan**.
- **No hay flag de encendido/apagado, y es deliberado.** Existió `ERPNEXT_TICKETS_ENABLED` y se
  eliminó: apagado, OpenCEO creaba tickets que vivían solo en Mongo y que el sistema de negocio
  nunca vería, y la reconciliación **no los recupera** (no hace backfill a propósito). Eso no es
  degradación, es corrupción silenciosa. **La resiliencia la da la marca `erpnext_pending`**, no un
  interruptor: con ERPNext caído el ticket se crea igual, sin `autorun`, y se repara al arrancar.
- **Sin configurar no es "apagado": falla fuerte.** Si falta `ERPNEXT_URL`/`API_KEY`/`API_SECRET`,
  la creación de tickets lanza y el arranque lo avisa con un `ERROR`. Es un error de despliegue.
- **`status_pushed` guarda el vocabulario de OpenCEO, NO el de ERPNext.** Comparar `in_progress`
  contra `Working` haría que cada parche pareciera una transición nueva. Y ojo con
  `str(TaskStatus.QUEUED)`, que da `'TaskStatus.QUEUED'` y no `'queued'`: hay que usar `.value`.
- **`completed` NO cierra el ticket de ERPNext, a propósito.** Deja `Pending Review` con la
  evidencia. Cerrar es una decisión de negocio (`ARCHITECTURE_REFERENCE_2026.md` §119) y va a
  generar la pregunta "¿por qué sigue ahí si ya terminó?".
- **El observador de estado se lee con `getattr`, nunca se crea en `__init__`**: los tests de
  Horizon construyen el repositorio sin llamar a `super().__init__()`.
- **El webhook necesita que UFW deje pasar la red de ERPNext.** Regla añadida:
  `allow from 172.23.0.0/16 to any port 18765 proto tcp`. Si el día de mañana se recrea la red
  `jardal-erpnext-net` con otra subred, el webhook deja de llegar **en silencio** (Frappe registra
  el fallo en su Error Log, no en el de OpenCEO).
- **Los webhooks se disparan con cada actualización de `Task`,** incluidas las que provoca el
  propio OpenCEO. Es correcto: el handler ignora los estados que son eco.
- **La consola de `bench` decodifica stdin como latin-1.** Un archivo UTF-8 se doble-codifica
  (`técnica` → `tÃ©cnica`). Usar escapes `é` en scripts ASCII; `Task Type` no permite renombrar
  y `Webhook` tiene `autoname=prompt`, así que hay que pasarle `name`.

**Cómo comprobar que sigue vivo:**

```bash
./scripts/openceo-core-host.sh status
./.venv/bin/python -m pytest tests/ -q                      # 289 pasan / 7 fallan
./.venv/bin/python scripts/migrate_pendings_to_erpnext.py   # 0 creados / 27 existentes
set -a; . openceo-core/.env.finance.local; set +a
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: token $ERPNEXT_API_KEY:$ERPNEXT_API_SECRET" \
  "$ERPNEXT_URL/api/resource/Task?limit_page_length=1"      # 200
# el camino inverso, de punta a punta (debe dar 401 sin secreto)
docker exec jardal-erpnext-backend-1 curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST -H "Content-Type: application/json" \
  http://172.23.0.1:18765/api/integrations/erpnext/task
```

## Traspaso anterior (2026-09-20, sesión 3)

Estado al cerrar la **tercera sesión del 2026-09-20**: la **vista del espacio personal** (la mitad decidible de HZ-004) está implementada en `openceo-react`, verificada contra los datos reales y **commiteada en `8e3b0f9`** (7 archivos, +1072/−10). **HZ-003 sigue commiteado** en `e47ff09` y el servicio corre con el código nuevo.

### Qué se hizo en esta sesión (2026-09-20, sesión 3)

0. **Commit `8e3b0f9`** — `feat(horizon): vista del espacio personal en el dashboard (HZ-004)`, con **solo** los 7 archivos de esta iniciativa (`PersonalSpace.jsx` nuevo, `pages.css`, `router/index.jsx`, `router/Menu.jsx`, `utils/permissions.js`, `auth/service.py`, `STATUS.md`). Los streams ajenos siguen sin commitear a propósito. `CLAUDE.md` sigue **sin trackear** (nunca lo estuvo): su línea de roadmap de HZ-004 se actualizó en disco, no en git.

1. **Vista del espacio personal** ([`openceo-react/src/pages/PersonalSpace.jsx`](file:///home/develop/development/Jardal/jardal-openceo/openceo-react/src/pages/PersonalSpace.jsx), ruta `/dashboard/personal`): misión y rumbo, objetivos con hitos base→meta, plan de hoy (capacidad, bloques, *qué quedó fuera y por qué*, decisiones pendientes), siguiente acción con delegación, delegación medida con entrega única, captura rápida y registro de métricas humanas. **No se añadió ni una ruta API nueva**: consume el contrato existente.
2. **Permiso `personal_space`** en `admin_permissions` ([`auth/service.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/auth/service.py)) y en `PERMISSIONS` del frontend, para que el espacio personal no se muestre a los empleados (spec §9.3/§9.10). La vista se gobierna con `PermissionCheck`; el aislamiento de los datos ya lo hacía la API por usuario autenticado.
3. **Regla de presentación respetada — desconocido nunca se dibuja como cero**: voz sin minutos útiles → "—" y "no proyectamos fecha"; capacidad no declarada → "desconocido"; `needs_input` y `empty` se etiquetan distinto ("Falta un dato" vs "Día saturado"); los minutos humanos en `0` sí se muestran como 0.
4. **Hallazgo — el grupo `admin` no existe en esta Mongo.** El admin (`usr_8c9941ae0bc9`) tiene `group_ids: []` y `all_permissions: []`; en `groups` solo hay `client` y `qa`. `create_default_user_if_needed` retorna temprano cuando el usuario admin ya existe, así que su `$addToSet` sobre el grupo `admin` no crea nada. Consecuencia: el permiso nuevo llega al propietario **por `is_admin`** (el frontend cortocircuita en `checkPermissions`), no por la lista de permisos; en una instalación limpia sí lo llevará el grupo. **No se tocó el auth**: recrear grupos es decisión del dueño.
5. **Hallazgo — `admin/admin1234` da 401**: la contraseña fue cambiada en Mongo (ya estaba anotado desde HZ-001). Por eso **no hubo smoke HTTP autenticado**: la verificación se hizo contra los payloads reales del servicio, en solo lectura.
6. **Validación**: `py_compile` OK · suite Horizon **163 pasan** · suite completa **236 pasan / 7 fallan** (idéntico a la línea base: los 7 preexistentes, sin regresión) · `npm run build` del dashboard OK (chunk `PersonalSpace` ≈21 kB) · **contrato UI↔API campo por campo contra los datos reales: 0 faltantes**. Ese chequeo fue un **script efímero en `/tmp` que no quedó en el repo** (solo lectura sobre la Mongo real); el método para repetirlo está en la trampa de verificación de abajo, y si se quiere repetible se puede persistir como `scripts/horizon_contract_check.py`. **La página no se abrió en un navegador**: falta sesión autenticada, así que la verificación visual queda pendiente del dueño.

### Decisiones pendientes del dueño — clasificadas

**No hay NINGÚN bloqueante.** Todo lo pendiente admite que la próxima sesión siga avanzando en paralelo:

| Pendiente | Tipo | ¿Bloquea el avance? | Qué puede hacer la próxima sesión sin esperar al dueño |
| --- | --- | --- | --- |
| **Ver la página en un navegador** | Sesión del dueño | **No** — el código y el contrato ya están verificados | El contenedor `openceo-react` está **apagado** (no aparece en `docker ps`): se levanta con `docker compose -f openceo-react/docker-compose.yml up -d` y se entra a `:3000`. El ítem "Espacio Personal" aparece para `is_admin`. |
| **Fuente conectada (la otra mitad de HZ-004)** | Necesidad observada en el uso | **No** | La spec §9.8/§11 la condiciona a *"conectar fuente solo si aporta ahorro"*; sin una fuente observada que reduzca captura manual, elegirla sería adivinar. |
| **Piloto de delegación con 3 tickets reales** (spec §9.6/§9.8) | Participación personal del dueño | **No** — solo frena la *medición* de HZ-003, no el desarrollo | Todo lo demás. El piloto se dispara cuando el dueño quiera: *"delega la siguiente acción"* por voz/chat o `POST /api/horizon/delegate`; métricas con *"anota 15 minutos de supervisión"*; resumen en `GET /api/horizon/delegation/summary`. **No lanzar delegaciones reales por iniciativa propia**: cada encargo consume cuota de proveedores y hace trabajo real en el repo. |
| **Streams sin commitear del working tree** | Revisión/commit eventual | **No** — no tocan los archivos de Horizon | Seguir trabajando; solo respetar la regla: **no mezclarlos con commits de Horizon**. Involucra: media/transcribe (`agents/media/`, `tools/media_transcribe.py`, `scripts/transcribe_links.sh`, `.qwen/`), voz Android (`openceo-voice/`, `openceo-voice-android/`), fix de lock por proyecto en `dev_session_service.py` (documentado el 2026-09-13), `params.coding_tool` en `agents/dev/session.py`, `config_loader.py`, `project_service.py`, `projects/*/project.yaml`, `README.md`, `.env.example`×2, y los sin trackear `CLAUDE.md`/`VOICE_GUIDE.md`/`docs/decision-2026-09-19-sistema-financiero-erpnext.md`/`scripts/chat*`. |
| **Elección del canal de entrega push** (hueco HZ-002, TICKET-026) | Decisión de canal del dueño | **No** — solo bloquea implementar *ese* canal | Preparar/investigar sin decidir (p.ej. dejar `secretary.send_email` cableado como primer canal). El dueño ya decidió explícitamente que hoy no hay push; el único canal durable que funciona es `secretary.send_email` (`openceo-core/tools/email_sender.py`); Telegram es stub. |
| **Entregar el mapa de funciones ERPNext → agente Finance** (`PENDINGS.md` TICKET-027) | Entregable del dueño | **No** — solo bloquea la Fase 3 de TICKET-025; el adapter no debe escribirse antes | Seguir con Horizon. **No abrir flujos financieros dentro de OpenCEO** ni inventar la superficie de tools: el contrato lo entrega el dueño cuando el sistema financiero esté definido. |

### Ruta por defecto de la próxima sesión (sin esperar al dueño)

Lo más barato y con efecto inmediato: **cerrar el lazo de la propia vista**. Hoy "Delegación medida" exige pulsar *"Ver resultado pendiente"* y las métricas van en pasos fijos de 15 minutos (`+15 min supervisión` / `+15 min retrabajo`); se puede cargar la entrega pendiente al abrir la página y permitir minutos libres. La alternativa de más valor estratégico es **HZ-005 — adapter de solo lectura hacia ERPNext** según [`docs/decision-2026-09-19-sistema-financiero-erpnext.md`](file:///home/develop/development/Jardal/jardal-openceo/docs/decision-2026-09-19-sistema-financiero-erpnext.md) (resúmenes con referencia y fecha de corte, sin contabilidad propia, spec §4.7) — **con la salvedad del 2026-09-20: el adapter no se escribe antes de recibir el mapa de funciones (`PENDINGS.md` TICKET-027)**. Y si el dueño ya eligió canal de push, cablearlo.

**Cosas que harán perder tiempo si no se saben:**

- **Los tests necesitan el venv del root**: `./.venv/bin/python -m pytest tests/ -q`. `python3` pelado **no** sirve (le faltan fastapi y motor; da 8 errores de colección). Línea base al cerrar: **224 pasan / 7 fallan**; los 7 son preexistentes y están diagnosticados abajo. Los tests de Horizon solos: **163 pasan**.
- **Trampa de `lifespan` en `main.py`**: `settings` se asigna dentro de la función (bloque de auth), así que es local a toda ella. Leer `settings.X` **antes** de esa asignación lanza `UnboundLocalError` y tumba el arranque — ya pasó una vez en esta sesión y `py_compile` no lo detecta. En ese bloque se usa `get_settings()` directamente.
- **Escribir en `horizon_triggers` a mano no despierta el timer**, por diseño: no hay sondeo, así que solo lo despierta `enqueue()`. Un encolado externo espera al siguiente vencimiento o hasta `max_sleep` (1 h). Los caminos reales del código pasan todos por `enqueue()`.
- **El motor YAML rechaza `llm_eval`** (`workflow_service.py`): cualquier `when` o `loop.condition` debe ser `python_eval`. Y `_resolve_value` convierte un parámetro ausente en `""`, no en `None` — leerlo como cero convertiría "no lo sé" en "día saturado".
- **El dueño del espacio personal es único**: `HORIZON_OWNER_USER_ID` (`settings.py`), hoy `usr_8c9941ae0bc9` (admin). `HorizonService.owner_for()` es el único punto de resolución: la API usa el usuario autenticado, voz y chat el dueño configurado. No añadir otro camino.
- **El grupo `admin` no existe en esta Mongo**: el admin tiene `group_ids: []` y `all_permissions: []`, así que **cualquier permiso nuevo no le llega por lista** — le llega por `is_admin` (el frontend cortocircuita y el backend usa `if not current_user.is_admin`). No concluir "el permiso no se aplicó" mirando `all_permissions`: verificar el camino de `is_admin`.
- **La contraseña de `admin` ya no es la del default** (`admin1234` da 401), así que los smokes HTTP con sesión no salen sin credenciales del dueño. Para verificar contra datos reales existe el camino de solo lectura: construir `MongoRepository(get_settings())` + `HorizonService` en un script con el venv del root (sin auth, sin escrituras).
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

## Estado actual (2026-09-20) — HZ-004 (vista del espacio personal)

- `Horizon HZ-004 — Fuentes conectadas y espacio personal (Fase 4 de 5)`: implementada la mitad **decidible hoy** de `jardal-horizon/README.md` §9.8 — *"un mismo canal de chat/voz y una vista del plan actual bastan para validar el primer uso"* — y la fila de aceptación §9.10 *"Se abre el espacio personal de OpenCEO: la misión mundial, sus objetivos y progreso son visibles al propietario, sin incorporarlos al contexto de todos los empleados"*. La otra mitad (conectar una fuente) queda **explícitamente fuera**: §9.8 y §11 la condicionan a *"conectar fuente solo si aporta ahorro… necesidad observada en el uso"*.
  - **Qué se implementó**:
    1. [`openceo-react/src/pages/PersonalSpace.jsx`](file:///home/develop/development/Jardal/jardal-openceo/openceo-react/src/pages/PersonalSpace.jsx) (≈790 líneas): la vista consume **el contrato que ya existía**, sin una sola ruta API nueva. Bloques: misión/rumbo con objetivo de crecimiento y estado de voz; plan de hoy con capacidad, bloques, *qué quedó fuera y por qué* (`excluded[].reason`), decisiones pendientes y motivo de la decisión; siguiente acción con `ready/in_progress` contra sus límites y delegación; delegación medida con entrega única (`already_delivered` visible, enlaces de evidencia) y registro de minutos humanos; objetivos con hitos `base → meta`; aparcados con su condición de revisión; captura rápida con deduplicación.
    2. **Acciones operables desde la vista**: declarar disponibilidad y replanificar (`POST /plan/day`), capturar (`POST /capture`), anotar avance (`POST /progress`), delegar (`POST /actions/{id}/delegate`), obtener la entrega pendiente (`GET /delivery`), registrar métricas (`POST /actions/{id}/metrics`).
    3. **Permiso `personal_space`** en [`auth/service.py`](file:///home/develop/development/Jardal/jardal-openceo/openceo-core/auth/service.py) y en `PERMISSIONS` (`utils/permissions.js`), con la ruta `/dashboard/personal` y el ítem de menú gobernados por `PermissionCheck`. Es un permiso de **vista**: el aislamiento real de los datos sigue siendo el filtro por usuario autenticado de la API, que el código ya documenta como correcto y que no se sustituyó.
    4. Estilos en `pages.css` (sección propia, `.horizon-*`) y rebuild del dashboard.
  - **Decisiones de diseño relevantes**:
    - **Desconocido no se dibuja como cero, también en la UI.** Es la regla del modelo (`DeclaredValue`) llevada a la pantalla: voz sin minutos útiles → "—" y *"no proyectamos fecha"*; capacidad no declarada → "desconocido"; `needs_input` ("Falta un dato") y `empty` ("Día saturado") se etiquetan distinto. En cambio los minutos humanos en `0` se muestran como `0`, porque ahí cero es una medida.
    - **No se añadió un segundo camino de identidad.** La tentación era poner un guard de propietario en las 16 rutas `/api/horizon/*`; se descartó porque `owner_for()` documenta que la API conserva el usuario autenticado y *"no hay que sustituirlo"*, y porque cada usuario ya ve solo lo suyo. Lo que faltaba no era aislamiento, era **visibilidad**: por eso el aporte es el permiso y la vista.
    - **Delegar pide confirmación explícita** (diálogo con el costo: *"consumirá cuota del proveedor"*), coherente con la nota del traspaso anterior de no lanzar encargos reales por iniciativa propia.
  - **Hallazgos (no son de esta funcionalidad, pero afectan cómo se verifica)**:
    - El **grupo `admin` no existe** en esta Mongo y el admin tiene `all_permissions: []`: el permiso nuevo le llega por `is_admin`, no por lista. En instalación limpia sí lo llevará el grupo. No se tocó el auth.
    - `admin/admin1234` da **401** (contraseña cambiada en Mongo), así que no hubo smoke HTTP autenticado.
  - **Tests**: **no se añadieron tests** — la unidad de trabajo es una vista de React y este repo no tiene infraestructura de tests de frontend (solo `vite build`). Lo que sí se verificó: `py_compile` OK · suite Horizon **163 pasan** · build OK · **contrato UI↔API campo por campo contra los payloads reales: 0 faltantes** (script de solo lectura con el venv del root; `n/a` donde la lista real está vacía). **Pendiente: ver la página en el navegador** (requiere sesión del dueño; el contenedor `openceo-react` está apagado).
  - **Pendiente de esta iniciativa**: fuente conectada (necesidad observada), y el pulido de la propia vista (cargar la entrega pendiente al abrir y permitir minutos libres en las métricas, hoy fijos en 15).

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

## Estado actual (2026-09-20) — Qué instalar en ERPNext (TICKET-025 / TICKET-027)

- **Documento nuevo [`ERPNEXT.md`](file:///home/develop/development/Jardal/jardal-openceo/ERPNEXT.md)** en la raíz (estado: *propuesta de instalación, no desplegado*). Resuelve la pregunta del dueño —"¿qué módulos instalar y hace falta levantar el stack completo?"— con evidencia de código fuente de ERPNext/Frappe y de la documentación de `frappe_docker`. Respuesta en dos partes:
  1. **Apps: solo `erpnext`.** Los módulos internos **no son elegibles**: una *app* se instala, un *módulo* llega con ella (`erpnext/modules.txt` es una lista fija de 21; `Show/Hide Modules` ya no se soporta desde v15). Nada de `crm`, `helpdesk`, `lms`, `drive`, `wiki`, `gameplan`, `raven` — duplican OpenCEO. **Consecuencia práctica: no hace falta construir imagen propia**, porque la imagen prebuilt de `frappe/erpnext` contiene exactamente frappe + erpnext. `payments`, `hrms`, `insights` y `print_designer` quedan como *después, con necesidad demostrada*: añadir una app exige **reconstruir la imagen** (instalar en runtime no está soportado y se pierde al recrear el contenedor).
  2. **Contenedores: stack estándar, sin recortes.** No existe perfil "slim" oficial. El único recorte documentado —consolidar colas de workers— no compensa: **deben quedar 2 Redis** (el `configurator` apunta `redis_socketio` a `REDIS_QUEUE` y no hay override de uno solo), `scheduler` es obligatorio para trabajos periódicos y quitar `websocket` es cirugía no soportada (pierde notificaciones y actualización en vivo; `frontend` lo declara en `depends_on`). Queda **corregida** la estimación previa de TICKET-025 ("1 Redis, 1 worker").
- **Hallazgos que condicionan el despliegue**:
  - **Bolivia no tiene plantilla de plan de cuentas en ERPNext.** Verificado directamente en `chart_of_accounts/verified` y `unverified`: **ninguna entrada `bo_`** (sí `ar`, `br`, `co`, `gt`, `mx`, `ni`, `el`, `cl`, `cr`, `ec`, `hn`, `pa`, `pe`, `uy`, `ve`), y no hay app de localización boliviana. El plan de cuentas se **construye a mano** y su contenido tributario se define con criterio contable local: **fuera de OpenCEO y fuera de `ERPNEXT.md`** (ahí no se fija ninguna alícuota).
  - **Choques con el Traefik existente**: `compose.proxy.yaml` / `compose.https.yaml` levantan **su propio** Traefik en 80/443; `compose.multi-bench.yaml` espera una red externa llamada literalmente `traefik-public` (la nuestra es `traefik-net`); y `SITES` está deprecado a favor de **`SITES_RULE`**, que es una regla de **Traefik v3** — nuestra versión.
  - **Una sola imagen para todos los roles** (el split `-worker` / `-nginx` / `-socketio` está retirado) y versión recomendada **`v16` fijada por tag de parche (`frappe/erpnext:v16.35.0`)** para que un `pull` no cambie versión en silencio.
  - **No existen cifras oficiales de RAM por servicio ni de disco por sitio.** El "4 GB" de `frappe_docker` es guía **de desarrollo**; Frappe Cloud estima ~400 MB por bench (2 workers gunicorn + 1 set de workers); el "2 GB mínimo / 4 GB recomendado" que circula es folclore de foro. La medición obligatoria de TICKET-025 fase 1 sigue vigente.
- **Medición propia del host (2026-09-20)**: **20 GB de RAM disponibles** (10 usados, 14 en caché), swap 3.4 GB en uso, **130 GB libres en `/` (55% usado)** y 57 contenedores corriendo. El disco **deja de ser** la restricción registrada el 2026-09-19 (55 GB libres, 82%): la fila de restricciones de TICKET-025 quedó actualizada con la medición nueva.
- **Frontera registrada (decisión del dueño, 2026-09-20)**: **todo el módulo financiero se trabaja fuera de OpenCEO** — despliegue, plan de cuentas, workflows y automatizaciones viven en el ciclo de vida de ERPNext. No se mezclan flujos financieros con el runtime de agentes. OpenCEO solo guardará referencias y resúmenes con fecha de corte, y el **único** punto de contacto será el **mapa de funciones** que el dueño entregue cuando el sistema esté definido: nuevo **`PENDINGS.md` TICKET-027**, que **bloquea la Fase 3 de TICKET-025**. Los seis nombres de tools de esa fase quedaron marcados en `PENDINGS.md` como **candidatos provisionales, no contrato**.
- **Nada implementado en el repo**: solo documentación. No se tocó código, no se desplegó nada y no hay flujo de runtime que validar en esta entrada.

## Estado actual (2026-09-20) — Ops de Traefik, baja de kimi y limpieza de logs

Trabajo de infraestructura hecho **fuera de OpenCEO** (en `/home/develop/deployments/jardal-traefik/`), derivado del runbook. **No se tocó código de este repo.**

- **Traefik actualizado a `v3.7.13`** por el dueño el 2026-09-20 19:38 UTC siguiendo `docs/traefik-upgrade-runbook.md` (respaldo previo en `/home/develop/backups/traefik-20260920T193100Z/`; el `docker-compose.yml.bak-v3.0` difiere solo en la línea `image:`). Parque verificado por sondeos: `n8n`, `analitica`, `drive`, `mail`, `portainer`, `dev.melanyryr` → 200; `opencode` 401, `agentboard` 302, `objects` 403 (auth de cada app, normal). El runbook quedó marcado como **ejecutado**.
- **`kimi.jardal.app`: dado de baja** (21:45 UTC). No era un contenedor sino un **proceso host** (`systemd --user kimi-web.service`, PID 1404, 51 días, `127.0.0.1:5494`). Se eliminaron de `dynamic.yml` el middleware `kimi-auth`, los routers `kimi-web`/`kimi-websecure` y el servicio `kimi` (YAML validado, Traefik recargó con 0 errores), y se hizo `systemctl --user disable --now kimi-web` → **puerto 5494 cerrado**. El archivo del unit se conserva para revertir. **El DNS lo debe borrar el dueño** (el MCP solo crea/verifica); hoy `kimi.jardal.app` responde `401` **de Cloudflare**, no del origen.
- **Consecuencia para OpenCEO:** `openceo-core/agents/dev/kimi_session_manager.py` usa `KIMI_WEB_BASE_URL`, por defecto `https://kimi.jardal.app` (2 ocurrencias) → **el provider `kimi` de las sesiones dev queda sin endpoint**. El dueño lo autorizó explícitamente ("baja completa"). Si se retoma ese provider, hay que apuntarlo a otro endpoint o retirarlo del código.
- **Logs limpiados:** `/var/lib/docker/containers` pasó de **1137 MB a 151 MB** (986 MB liberados) truncando los logs ≥5 MB. **No hay rotación configurada** en ningún contenedor: volverán a crecer (pendiente `max-size`/`max-file`).
- **Volúmenes huérfanos de `jardal-vpn` / fugovpn:** eliminados los dos de caché `node_modules` (341 MB). **Se conserva a propósito `jardal-vpn_mongo-dev-data` (307 MB, datos)** por decisión del dueño. No existe ningún contenedor de ese proyecto, ni corriendo ni parado.
- **Hallazgo no pedido, pendiente del dueño:** `cebitec-rest` **reinicia cada ~12 s (55.333 reinicios)** por `MongoServerError: Authentication failed (code 18)`; su HTTP responde de forma intermitente (el `404 Cannot GET /` de `cebitec-api.jardal.app` es de la app). Es además el motor del ruido en el log de Traefik (cada `die`/`start` fuerza un rebuild). El dueño se encarga de matar ese servicio.
- **Evidencia completa:** `/home/develop/deployments/jardal-traefik/DIGEST-logs-2026-09-20.md` (§1–§8), incluidas las renovaciones ACME que fallan por diseño en `chat`, `code`, `agentboard` (detrás de Cloudflare) y `cui`, `cloudcli` (sin DNS).

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

- Motor base integrado en `main` (`3128146`, `TASK-2026-00030`). El informe de implementación registra 87 pruebas correctas; el piloto real de principio a fin y la proyección a ERPNext vivo siguen pendientes. No confundir esa evidencia lógica con aceptación completa.
- Entrada por `task.coding_tool=tier_s` o por el modo Dev del proyecto/sesión. El proveedor general de `jardal-openceo` permanece en `codex`; su piloto debe seleccionar Tier S explícitamente.
- Roster actual: `claudej` planner, `codexf` revisor obligatorio de plan/código, `claudep` builder y `qwen` + `claudeg` revisores adicionales. `agyd` y OpenCode/MiMo están declarados inactivos. El dueño comunicó la compra de Claude Pro, pero la configuración aún conserva `planner.active=false`; la continuación de `00030` debe activarlo y verificar modelos/comandos efectivos.
- Se exige revisión válida del plan antes del build; cada ronda intenta todos los revisores activos y necesita Codex más otro modelo independiente. Fallos, cuota, reportes inválidos o hallazgos bloqueantes conservan una espera recuperable y no equivalen a aprobación.
- El builder escribe código; los revisores inspeccionan snapshots Git en worktrees temporales. Un cambio de snapshot invalida el reporte, pero el worktree no es una barrera del sistema operativo contra escrituras por rutas absolutas: esa protección debe validarse/reforzarse en `00030`.
- MongoDB `tier_s_runs` conserva fase, roster, pregunta y checkpoints. Los artefactos locales están en `.tier-s/` (`CURRENT_PLAN.md`, `BUILD_SUMMARY_N.md`, `REVIEW-*.md`, señales y archivo de intentos); no se crea `status.json`. La atención y el progreso se proyectan al mismo Task de ERPNext.
- API: `POST /api/tasks/{task_id}/tier-s/answer`, `POST /api/tasks/{task_id}/tier-s/cancel` y `GET /api/tier-s/waits?project=...`. El piloto debe verificar pregunta real, respuesta por opción/texto libre y reanudación del mismo intento.
- OpenCode todavía no tiene adaptador de revisión de lectura en este consenso: `TASK-2026-00078` cubre su integración y piloto gratuito. Una configuración heredada no vacía `opencode_reviewers` produce espera explícita y debe migrarse a `reviewers` con adaptador verificado. El circuito Research con policies restrictivas es un antecedente útil, no evidencia de una revisión Tier S ya funcional.
- Seguimiento: `TASK-2026-00030` y `TASK-2026-00078` permanecen `Open`, sin lanzamiento automático. El contrato temporal y el informe están en `docs/work/TASK-2026-00030-tier-s.md` y `docs/reviews/TASK-2026-00030-tier-s-review.md`; los criterios de cierre vigentes están en ERPNext.

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
