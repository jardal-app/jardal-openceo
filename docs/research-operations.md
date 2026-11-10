# Operación de Research: social, deep y eventos

La configuración efectiva se edita en `openceo-core/.env`. La política de cada
proyecto solo puede reducir los límites del servidor. Crear un job no crea un
ticket de Jardal ERP. La activación recurrente se decide al terminar el proyecto;
las pruebas del agente no requieren revisión humana por fase.

Cuando el host está administrado por `openceo-core-host.service`, reiniciar la
unidad de systemd de usuario. El launcher manual y el modo `foreground` de esa
unidad interpretan `.env` con python-dotenv: comillas, referencias `${KEY}` y
saltos de línea se resuelven como en Settings, sin ejecutar texto del archivo
(el runner solo delega en `foreground`).

```bash
XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus systemctl --user restart openceo-core-host.service
```

## Tramo vivo en jardal-openceo

`projects/jardal-openceo/project.yaml` autoriza `quick` y `deep` (#641,
Owner 2026-11-06), techo `public`, Brave y Serper para búsqueda y Firecrawl
opcional para extracción. `grounding` sigue apagado, `read_provider: http`
y `allowed_account_refs: []`. `paid_budget_usd: "2.00"` iguala el techo
de servidor autorizado; el ceiling deep existente sigue en 2.00. La política
efectiva toma el mínimo servidor/proyecto: el YAML no activa flags ni eleva
ningún techo del entorno. Los límites quick son 3 búsquedas/5 páginas/8 pasos/300 s
(#694: techo 300 para el turno de razonamiento Demo B);
deep, 15/25/30/900, siempre recortados por servidor y petición.

El servicio está encendido en producción (`openceo-core/.env:142`
`RESEARCH_ENABLED=true`; `RESEARCH_SCHEDULER_ENABLED` ausente, defecto `false`
en `settings.py`). Con `RESEARCH_ENABLED` abierto, `/api/research/*` admite jobs
a quien tenga `run_research`; sin él, el servicio responde deshabilitado y no
admite jobs. El scheduler recurrente exige ambas flags (`workflows.py:112-113`)
y solo se activa con autorización explícita del Owner; queda apagado. El
servidor trae gasto pagado `2.00` y techo deep `2.00` (`.env:169`, `.env:217`),
en mínimo con el proyecto. El despacho quick usa el perfil free-research
(#648): exige `RESEARCH_FREE_ROUTER_ENABLED=true` (ausente, defecto `false` en
`settings.py:407`) y un snapshot free-research vigente, o la síntesis queda
`blocked` con `failure_reason` (ver `Perfil gratuito del servicio`).

Smoke #594 verificado sin tocar `.env` (8/8 con el flag en memoria): config
real del yaml vía `ConfigLoader`, servicio disponible, `submit`→`queued`
(gasto 0), `get`→mismo job, `cancel`→`cancelled`, deep rechazado, scheduler
off. Mongo real + lector falso declarado, sin red ni LLM vivo.

Activación en producción (Owner, tras el merge #594): (1)
`RESEARCH_ENABLED=false`→`true` en `openceo-core/.env`, dejando
`RESEARCH_SCHEDULER_ENABLED` ausente; (2) `scripts/openceo-core-host.sh restart`
+ `status`; (3) smoke prod #613 verde (12/12, release `1ce5eed`):
submit/get/cancel quick contra el servicio vivo (gasto 0), deep denegado y
scheduler verificado apagado (gate de ambas flags, agendas de arranque sin
research, 0 suscripciones y 0 schedules activos); (4) el Owner decide si las
flags quedan encendidas.

Cuándo este research merece su propia línea en vez de tickets sueltos, ver
`Línea dedicada de investigación` (criterio del ticket #592; aquí no se
duplica). El perfil free-research se describe en `Perfil gratuito del servicio`. El enrutado del planner se cubre en
`Enrutado del planner` (ticket #618) y el intake externo en
`Intake externo` (ticket #619).

## Perfil gratuito del servicio

Quick consume `free-research` en dos turnos acotados (analisis de evidencia y
sintesis), con afinidad, ranking congelado y checkpoint por turno. Cada intento
consume un paso del mismo presupuesto. Los fallbacks respetan herramientas,
contexto, privacidad, gratuidad y disponibilidad; los proveedores externos ya
soportados reciben solo su credencial `FREE_ROUTER_*` seleccionada. Deep usa el
mismo perfil gratuito por defecto (#662), con grupo nativo A primero y orden
del perfil dentro de cada grupo; quick conserva su orden vigente.

Tras agotar la sesion gratuita (`BLOCKED_NO_ELIGIBLE_MODEL`), #693 permite
un unico intento musej por fase quick o deep, sin adelantar ni modificar los
reintentos, rotaciones o cooldowns. `RESEARCH_MUSEJ_FALLBACK_ENABLED=true`
por defecto; `false` lo desactiva. Usa `MUSE_CLI_PATH` y el modelo explicito
`muse-spark-1.3-contributor`, con prompt por argv. No activa DeepSeek ni
grounding pagado: proveedor `musej`, `cost_tier=own`, cero `micro_usd`, sin
reservas pagadas. Los intentos consumen el presupuesto y slots del job y
quedan en `free_research.model_usage` y en el ledger `research_usage` (operacion
idempotente por job/fase, cargo cero); el limite por fase persiste al reanudar.
Si falla, el job queda `blocked` con motivo y checkpoint; no hay otro intento.

Musej reutiliza el spawn, deadline, cancelacion y frontera bwrap del worker:
home/config propios, solo MCP del broker, sin repo ni vault del host. Monta
solo los ejecutables/metadata del launcher y su archivo de autenticacion en
solo lectura (`MUSE_AUTH_PATH`, o `XDG_CONFIG_HOME/muse/auth.json` del host).
Sin autenticacion o bwrap no ejecuta. No hereda configuracion personal;
las herramientas nativas, workflows y subagentes quedan desactivados.
La validacion del builder usa dobles, nunca el proveedor vivo; el simulacro
real de agotamiento corresponde al Owner en otra sesion tras integrar.

Decision de degradacion: ranking o catalogo ausente, invalido o caducado,
router deshabilitado (`RESEARCH_FREE_ROUTER_ENABLED`, defecto `false` en
`settings.py:407`) o perfil sin rutas compatibles dejan el job `blocked` con
`failure_reason`; nunca se usa la lista estatica del helper legacy de sintesis.
El agotamiento de disponibilidad usa el backstop musej descrito arriba.
Vigente significa ranking con perfil free-research generado dentro de
`RESEARCH_FREE_ROUTER_SNAPSHOT_MAX_AGE_HOURS` (30 h por defecto).
Tras recuperar elegibilidad, `POST /api/research/jobs/{research_id}/resume`
(mismo owner, permiso `run_research`) reencola el mismo job y despierta el
despachador. Conserva fase, evidencia, pasos restantes y checkpoint, sin repetir
descubrimiento ni lectura. No hay reintento periodico. Un conflicto de resultados
de herramientas requiere reconciliacion antes de reanudar (HTTP 409).

## Enrutado del planner

Toda pregunta de internet, librerías o tecnologías se enruta a
`research` con la skill `investigar` (ticket #618): la pregunta y la
decisión que desbloquea viajan en el ticket. Lo que el repo responde
por sí solo (causa raíz, arquitectura, compatibilidad) sigue siendo
un ticket DEV `tier_a`. La regla normativa vive en
`openceo-core/objectives/routing_policy.d/research.md` (RESEARCH
ROUTING §§45–§49), detalle on-demand del core que el planner lee
antes de asignar trabajo de research; el core la cita en la nota de
split y en `Per-agent detail`.

Quick live por defecto: el puente de ticket, la tool de submit y la
receta `investigar` usan `quick` salvo que el ticket nombre `deep`.
Deep gratuito admite presupuesto pagado cero. Solo `deep_paid_opt_in: true`
exige presupuesto efectivo positivo y falla cerrado (`forbidden`) con cero.
La selección deep se define con criterios explícitos (§47:
brecha de cobertura desde un brief quick previo, verificación
independiente, decisión que desbloquea gasto/frontera/migración y
presupuesto pagado positivo cuando se solicita DeepSeek); la admisión sigue la política
efectiva (#641) y el contrato se verifica con servicio simulado.

La corrida admite una investigación bajo el consumidor del ticket
(`openceo_research_submit`), la sondea (`openceo_research_get`),
escribe el brief solo desde la evidencia devuelta, lo adjunta
privado al ticket solicitante (`openceo_attach_ticket_file`) y
termina `hecho` nombrando el adjunto; el resultado vuelve al
planner, nunca continúa solo a DEV. Verificado con
`tests/test_planner_research_routing_618.py` (contrato original de
detalle servido, quick por defecto y ciclo con planner simulado): sin
modelos vivos ni red. La política efectiva vigente gobierna deep. El intake
externo vive en `Intake externo` (ticket hermano #619); el scheduler
sigue apagado.

Tier S aplica el §9 del core (`routing_policy.md`) antes de desarrollar
(#642): solo investiga si el planner
identifica dependencia material de información externa desconocida. Sin esa
necesidad no hay investigación. Emite primero `research/investigar`, quick
por defecto; `research_mode: deep` exige los cuatro `deep_criteria` de §47
nombrados y presupuesto efectivo positivo, sujeto a admisión del servicio.
El desarrollo queda en `roadmap_remaining`, sin precrear su ticket.

Al cerrar research se invoca de nuevo al planner, incluso con otros hijos
listos. Recibe los briefs privados del objetivo como evidencia y decide de
nuevo el agente y alcance. Si crea DEV dependiente, `research.needed: true`,
`research.reason` y `research.brief_ids` registran la decisión; solo admite
adjuntos de research completado del mismo objetivo. El ticket posterior
lleva la decisión en el texto y copias privadas verificadas antes de autorun.
Un fallo de copia retiene la ejecución. Un pedido suelto que necesita
research y desarrollo se convierte en objetivo para conservar esa vuelta al
planner; una pregunta autónoma sigue siendo un ticket research suelto.
Los badges DEV, incluidos los de Tier S, no admiten submit/get de research,
incluso con una caja mal ampliada; el consumidor y los tokens pertenecen a
research. `tests/test_tier_s_research_first_642.py` verifica estos contratos
con planner, servicio y adjuntos simulados, sin red ni modelos vivos.

## Intake externo

La puerta externa de research queda fijada sobre las fachadas y grants
existentes (ticket #619): el ticket de solicitud (Jardal ERP first) es la
vía por defecto y la API/MCP con grants explícitos es la vía programática.
Fuente única de la matriz: `openceo-core/research/intake.py`
(`CONSUMER_INTAKE`); lo que la tabla dice lo fijan los tests.

| Consumidor | Vía | Autenticación | El owner sale de | Operaciones | Fachada |
| --- | --- | --- | --- | --- | --- |
| solicitante externo | ticket (por defecto) | ticket de solicitud en Jardal ERP | `ticket:<TASK-2026-NNNNN>` (lo acuña el puente en servidor) | submit, get | `research/ticket_bridge.py` |
| cliente HTTP programatico | http | cookie de sesión + permisos run_research/read_research | principal autenticado (`get_current_user`) | submit, get, cancel | `research/router.py` |
| consumidor MCP programatico | mcp | token opaco de consumidor en `Authorization: Bearer` | `grant.owner_id` del token | las del grant explícito (submit, get, cancel por defecto) | `research/mcp_facade.py` |

Garantías en la vía programática: sin identidad no hay autorización (sin
fail-open); el owner nunca viaja en argumentos ni `_meta`; un job ajeno se
lee como 404, nunca 403. El token del consumidor se guarda hasheado y el del
puente de ticket nunca sale del servidor. Jardal ERP es el registro de
negocio; Mongo conserva la ejecución.

La corrida por defecto admite una investigación bajo el consumidor del ticket
(`openceo_research_submit`), la sondea (`openceo_research_get`) y devuelve el
brief al solicitante; el enrutado del planner hacia esa corrida vive en
`Enrutado del planner` (#618). Verificado con
`tests/test_research_intake_619.py` (definición, ciclo ticket, grants
explícitos, matriz en docs): servicio simulado, sin modelos vivos ni red. El
scheduler sigue apagado.

## Línea dedicada de investigación

Cuándo el research merece su propio objetivo (línea) en vez de vivir como
tickets sueltos dentro de otro objetivo (ticket #592):

- El tramo prevé más de 6 tickets de research vivos a la vez (el tope de
  tickets activos por objetivo) o más de ~10 en el tramo (una fracción
  significativa del presupuesto lifetime de 30 por objetivo).
- El research tiene aceptación propia, independiente del objetivo principal:
  su cierre y su evidencia se juzgan por separado.
- El research competiría por slots activos con el trabajo principal y lo
  frenaría (`objective-full`).

Un research puntual sigue siendo un ticket hijo del objetivo que lo pide.
La línea dedicada se abre por el canal normal (pedido del CEO u objetivo
Jardal ERP-first); abrirla no habilita research en producción ni activa el
scheduler recurrente — eso sigue siendo decisión del dueño (ver
`Cierre humano único`).

## Lectura de X

Instalar la dependencia opcional con el Python del proyecto:

```bash
./.venv/bin/pip install -r openceo-core/research/requirements-social.txt
./.venv/bin/python scripts/research_social_account.py create --owner USER_ID
./.venv/bin/python scripts/research_social_account.py cookies --owner USER_ID --account ACCOUNT_ID
./.venv/bin/python scripts/research_social_account.py grant --owner USER_ID --account ACCOUNT_ID --source 'from:CUENTA'
```

La entrada de cookies es local y oculta. El operador aporta su propia sesión
autorizada y resuelve login/2FA; no se solicitan contraseñas ni se automatizan
challenges. No pegar cookies en chat, `.env`, argumentos o tickets.

`RESEARCH_SOCIAL_SESSION_DIR` debe estar fuera de repo, workspace, evidencias y
vault: directorio 0700, una SQLite 0600 por cuenta. No incluirlo en backups de
evidencias/vault; el servicio no crea copias de sesiones. Expirar o revocar niega
acceso; la eliminación local explícita usa `retire` con los mismos owner/account.

El proyecto debe conceder `allowed_account_refs` y estar habilitado para Research.
La identidad HTTP necesita `run_research`; el grant corresponde a `http:USER_ID`,
proyecto, fuente exacta y operación. `POST /api/research/social/read` obtiene como
máximo cinco items y **una petición GraphQL**; no expone DM, bookmarks ni el feed del
owner. Antes de esa petición la librería genera su identificador de transacción:
baja una página pública de x.com con las cookies y, con sesión válida, los scripts de
la web de X. Cada lectura es un proceso nuevo, sin caché entre lecturas, así que la
red tocada es mayor que una petición. Los artifacts caducan en siete días o antes si
vence el grant. Leerlos revalida autoridad; quedan `restricted` y `declaration`, sin
llamadas al modelo.

La lease compartida en Mongo serializa lectores y setup incluso entre procesos.
Cancelación mata el helper y libera la lease; el helper tiene además un deadline
propio de 20 s para terminar por sí mismo y limpia al empezar los bloqueos de 15
minutos que la librería deja si lo matan. No hay rotación ni espera indefinida.

Causas que devuelve la lectura (HTTP 409 salvo `access_denied`, que es 403):

| Causa | Qué significa y qué hace el servicio |
| --- | --- |
| `login_required` | X ya no acepta la sesión (caducó, cookies mal copiadas o cuenta cerrada). Pausa la cuenta; quien tiene grant ve esta causa y no hay más intentos hasta repetir `cookies`. |
| `challenge_required` | X pide desbloquear la cuenta en un navegador. Misma pausa; lo resuelve la persona. |
| `rate_limited` | X limitó la cuenta. Cooldown de 15 minutos sin contactar a X. |
| `access_restricted` | 403 sin código de error; no se interpreta como sesión caducada. |
| `source_unavailable` / `source_timeout` | X cambió su web, falló la red o se agotó el deadline. No pausa la cuenta. |
| `account_busy` | Otra lectura o setup tiene la lease. Reintentar después. |

Sin grant válido todas las causas se ven como `access_denied`. Si
`RESEARCH_SOCIAL_SESSION_DIR` no es válido (permisos, symlink, dentro de repo,
workspace, evidencias o vault) el servicio arranca igual y solo la ruta social
responde 503; el log indica `unsafe_session_directory`, `session_dir_not_isolated` o
`social_init_failed`.

## Fuentes recurrentes (RSS/Atom)

Antes de suscribirse a una fuente, comprobarla con la sonda de solo lectura. Usa el
lector con guard de egress y el adaptador de vigilancia, lee dos veces para ver si la
fuente responde a la petición condicional y no necesita Mongo, modelo ni flags:

```bash
./.venv/bin/python scripts/research_watch_probe.py --url https://example.org/feed.xml
./.venv/bin/python scripts/research_watch_probe.py --type greenhouse --board TOKEN
./.venv/bin/python scripts/research_watch_probe.py --type lever --board TOKEN
```

Medido el 2026-10-02 contra fuentes públicas reales: el Atom de releases de GitHub, el
RSS 2.0 de `peps.python.org` y el tablero Greenhouse `gitlab` devolvieron items con
título, URL y fecha, y la segunda lectura fue `not_modified` (ETag, y Last-Modified en
el RSS); el tablero Lever `leverdemo` devolvió sus 11 postings y se relee entero, porque
Lever no usa validadores por diseño. Una fuente que pasa aquí la puede leer una
suscripción; encender `RESEARCH_ENABLED` y el scheduler sigue siendo decisión del dueño
y se hace al final del proyecto. El guard de egress bloquea localhost y redes privadas:
la fuente de la prueba recurrente debe ser una URL pública.

## Demo B

Demo B despliega la recurrencia sobre el feed autorizado único
`https://github.com/jardal-app/jardal-openceo/releases.atom` (Owner 2026-11-06,
corrección 2026-11-09T12:49 que sustituye la URL muerta `Jardal/`, ticket #643)
y deja el scheduler recurrente listo para quedar encendido, con
`RESEARCH_ENABLED=true` y `RESEARCH_SCHEDULER_ENABLED=true` permanentes tras la
demo y suscripciones/schedules activos verificables. La spec acotada vive en
`openceo-core/research/demo_b.py`: fuente RSS exacta, intervalo horario,
propósito y clave idempotente fijos (una sola Demo B por owner), primera pasada
como línea base; los topes de ingestión por tick y de suscripciones activas los
aplica el motor. El scheduler es reactivo: sin vencimiento espera
indefinidamente, sin sondeos.

Sonda del feed autorizado (solo lectura, sin Mongo, modelo ni flags):

```bash
./.venv/bin/python scripts/research_watch_probe.py --url https://github.com/jardal-app/jardal-openceo/releases.atom
```

Registro 2026-11-08: `ok: false`, `Respuesta HTTP 404` (`source_unavailable`,
0 items). El repo no responde en público (ni `Jardal` ni `jardal-app` devuelven
la página ni el Atom; el remoto git apunta a `jardal-app/jardal-openceo`): sin
releases públicamente legibles, el tick productivo degrada con causa y conserva
el cursor en vez de admitir trabajo. Precondición del runbook: repetir la sonda
y ver `ok: true` con items.

Verificación pre-flip (`tests/test_research_demo_b_643.py`, servicio simulado
sobre Mongo real con tiempo virtual, modelo Mock intacto, sin red ni LLM vivo):
spec exacta y topes (`demo_b_bounds_problems`), gate de ambas flags, dos
reinicios que conservan identidad/vencimiento/cursor con 304 tras reiniciar,
tick que admite un único job quick público con presupuesto pagado cero y
límites de la política efectiva, admisión acotada por tick sin duplicados y
cancelación que detiene la adquisición. Oráculo intacto
(`test_research_watch.py`, `test_research_subscriptions.py`).

Runbook productivo (Owner, ya autorizado; sin editar `.env` en el repo):

1. Repetir la sonda hasta `ok: true` (precondición: releases públicamente legibles).
2. Poner `RESEARCH_ENABLED=true` y `RESEARCH_SCHEDULER_ENABLED=true` en
   `openceo-core/.env` del host.
3. `scripts/openceo-core-host.sh restart` + `status`.
4. Crear la suscripción Demo B con la spec de `research/demo_b.py`
   (`POST /api/research/subscriptions`).
5. Verificar activas (`GET /api/research/subscriptions`, schedules activos),
   observar un tick (un job quick, gasto 0) y reiniciar dos veces comprobando
   que identidad, vencimiento y cursor sobreviven.
6. Las flags quedan encendidas tras la demo. La cancelación que detiene la
   adquisición está probada en tests; la suscripción Demo B queda activa para
   que suscripciones y schedules sigan verificables.

## Deep

Deep ejecuta researcher/verifier mediante `free-router` + OpenCode por defecto,
con coste marginal de modelo $0 y sin reservas DeepSeek. Usa los ajustes existentes
`RESEARCH_FREE_ROUTER_ENABLED`, `RESEARCH_FREE_ROUTER_SNAPSHOT_MAX_AGE_HOURS` y
`RESEARCH_OPENCODE_*`, el broker montado y el aislamiento obligatorio del runtime.
Cada fase abre contexto nuevo, conserva afinidad de modelo y guarda resultados,
evidencia, pasos y checkpoints para reanudar sin repetir fases terminadas ni efectos
de herramientas. Ruta gratuita no disponible deja `blocked` con `failure_reason`;
nunca activa un modelo estático ni DeepSeek. El runtime actual no soporta `musej`
con esa frontera: no se añade una ejecución CLI sin aislamiento equivalente.

Solo para casos nombrados por el Owner, una petición HTTP/job con
`deep_paid_opt_in: true` selecciona DeepSeek HTTP. La bandera es estrictamente
booleana, se persiste y distingue la identidad idempotente; su ausencia, incluso
en jobs antiguos, selecciona gratuito. No cambia los límites ni autoriza crédito.
Requiere `RESEARCH_DEEP_API_KEY`, `RESEARCH_DEEP_MODEL`, precios máximos de entrada y
salida por millón de tokens y `RESEARCH_DEEP_ACCOUNT_CEILING_USD`. La clave es una
configuración explícita; puede referenciar `${DEEPSEEK_API_KEY}` en el `.env`.
Sin ruta privada compatible, solo se admiten fases públicas.

Solo esta selección pagada exige presupuesto efectivo positivo: mínimo entre
`RESEARCH_PAID_SPEND_USD` y `research.paid_budget_usd` del proyecto. Se reserva antes
del HTTP contra la cuenta compartida y el job; un fallo ambiguo conserva la
reserva. No hay topups ni reintentos del modelo. Las tarifas estiman un máximo;
la factura reconciliada queda `null` sin medición del proveedor.

El montaje comparte la autoridad existente `deepseek-research:default` entre
deep y Serper, conservando el gasto previo y el menor techo de servidor/deep.
Serper reserva además contra su proveedor. Las reservas máximas quedan inciertas:
sin factura no se repone capacidad entre fases, jobs, procesos o proveedores.
Una interrupción entre reservas solo reduce capacidad; no autoriza gasto extra.
Los créditos nativos siguen siendo otra unidad y no compran presupuesto USD.

`GET /api/research/jobs/{id}` expone `provider_status` por job, sin secretos:
`configured` indica prerrequisitos de entorno completos; `mounted`, adapter
y ledger presentes para proveedores pagados, runtime para `deep-free` y `musej`;
`deep` sigue identificando DeepSeek opt-in. `verified: null`, ninguna llamada correcta
observada en ese job. Pasa a `true` tras búsqueda/extracción correcta o ambas
fases researcher/verifier; no certifica facturación, calidad ni producción.
El tier `musej` pasa a `verified: true` tras un turno correcto de backstop.
La validación #641 usa servicio, HTTP y cuotas simulados sin red ni LLM vivo
(`tests/test_research_deep_live_641.py`). El servidor ya trae gasto 2.00 y
techo deep 2.00 (`.env:169`, `.env:217`); la verificación con proveedores
reales sigue siendo un paso manual del Owner; scheduler/Demo B pertenecen a
otro ticket (#643).

Hasta dos conjuntos researcher, verifier con contexto nuevo, una corrección y
revisión final. Cada fase consume el mismo límite de pasos/deadline y libera su
slot; quick y deep comparten un máximo de dos llamadas activas. Extractos e ids
deben pertenecer a la evidencia adquirida. Un veredicto del modelo es una opinión
con soporte comprobable; no prueba independencia ni certeza. Contradicciones o
soporte insuficiente producen `partial`.

Deep gratuito exige un objeto JSON con el array de la fase (`claims` o
`verdicts`); solo tolera envolturas de texto alrededor de ese JSON. Una respuesta
verifier en prosa recibe como máximo un reintento de formato, con contexto nuevo,
dentro del mismo cupo de tiempo y pasos. Nunca se convierten etiquetas en prosa en
veredictos. Si sigue malformada, los claims quedan sin verificar y el job `partial`.
Los extractos deben ser subcadenas contiguas exactas del texto normalizado que
persiste el almacén para una fuente citada; las fases reciben ese mismo texto.
La ruta gratuita relee los blobs con scope de proyecto/owner antes de las fases;
un blob inaccesible deja el job bloqueado y se comprueba otra vez al reanudar.
Se rechazan fragmentos unidos, incluso si el verifier los aprueba.
Se retiran del resultado también cuando falla una fase posterior; la corrección
puede sustituirlos por citas separadas y se verifica de nuevo.

La ruta gratuita limita researcher-1/2 a 90 s cada uno, verifier/final a 120 s
cada uno (incluido su reintento de formato) y correction a 60 s. Cada intento
runtime de verifier recibe como máximo 90 s. Todo cuenta también la espera de
slot, recuperación del router y persistencia. El job gratuito tiene un máximo de
510 s: suma de cupos (480 s) más 30 s de margen para adquisición; siempre prevalece
un deadline efectivo menor. El probe gratuito solicita esos 510 s y registra los
límites y cupos en su manifest. La ruta pagada conserva sus límites y los 120 s
del probe; no se amplía su autorización ni se añaden reintentos pagados.

Cada intento Deep gratuito tiene además 15 s para la primera actividad del
asistente (texto, razonamiento o herramienta); eventos de arranque no cuentan.
Después conserva el cupo de fase. El runtime lee errores JSONL mientras el worker
sigue vivo y cancela su grupo sin esperar el cupo. `provider.no-route`,
model-unavailable y ausencia de primera respuesta enfrían solo el deployment
(60 s iniciales, progresivos hasta 3600 s) y rotan en la misma fase, sin reintento
en ese deployment. Se mantienen dos rotaciones máximas por turno, el presupuesto
compartido y la insuficiencia de los claims que no completan verificación.

Deep solicita explícitamente `native_first`; Quick usa `best_global`.
Deep ordena primero todas las rutas nativas presentes en el ranking que pasan
los filtros de catálogo, coste $0, herramientas/contexto y configuración del
proveedor, incluso backups de modelos con un externo mejor posicionado.
La disponibilidad puede descartarlas. Rotar excluye deployments intentados,
permitiendo otro deployment del mismo modelo: no aporta independencia adicional.
La reanudación conserva el orden y la afinidad del checkpoint, incluido un externo
ya servido aunque se recuperen nativos. El proveedor servido por sí solo no
demuestra un fallo de política.

La muestra de aceptación pública requiere una sesión Owner de validación separada:

```bash
./.venv/bin/python scripts/research_deep_probe.py --output /tmp/research-pilot-NUEVO
```

Los diez casos gratuitos usan Mongo, workspaces y evidencia separados; necesitan
el catálogo/ranking vigente, CLI aislado y el broker existente accesible para su
catálogo de herramientas (las fases no emiten capabilities de adquisición).
El antiguo `--use-approved-budget` sigue siendo compatible y no selecciona pago.
Solo `--paid-deepseek --use-approved-budget --case ID` selecciona casos pagados;
sus reservas usan la cuenta real compartida bajo la misma política y ledger.
Este re-run vivo corresponde al Owner después del merge, en sesión separada;
los tests del builder usan únicamente dobles. Revisar claims contra blobs guardados,
registrar denominadores y separar estimación de reconciliación. Las pruebas con
fixtures comprueban contratos, no calidad real.

## Proveedores opcionales

Brave, Serper y Firecrawl se eligen por los adapters habilitados en la política;
una denegación/fallo no activa otro proveedor. El broker resuelve esa autoridad
desde el job persistido, no desde argumentos del modelo. No se envían consultas
privadas a estos proveedores públicos.

Serper requiere saldo medido en `RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES`.
Sus consultas debitan créditos compartidos no renovables en Mongo antes del HTTP;
un fallo conserva el débito. Con ambos buscadores autorizados, el servicio alterna
Brave y Serper mientras necesite cobertura y quede cupo; un fallo detiene la
búsqueda, nunca selecciona el siguiente proveedor como failover.
Firecrawl v2 requiere `RESEARCH_FIRECRAWL_ACCOUNT_CREDITS` y una ventana medida en
`RESEARCH_FIRECRAWL_CREDIT_WINDOW`; su autoridad de créditos nativos es compartida
en Mongo y está separada de los dólares. Cada intento debita antes del I/O y un
fallo conserva incertidumbre. No hay renovación automática de ventanas ni compra.

Para extracción se necesita `research.read_provider: firecrawl` además del adapter.
Sin montaje o autorización devuelve un gap; no cambia a HTTP. El grant sale del
job persistido, igual que la selección de buscador, nunca del texto del modelo.
Se validan la URL inicial y la devuelta; el proveedor no acredita su cadena remota
completa de DNS/redirecciones, así que se conserva un coverage gap y el informe
es `partial`. El lector HTTP sigue siendo el predeterminado.

Probes explícitos de una consulta/lectura, sin modelo ni flags de producción:

```bash
./.venv/bin/python scripts/research_provider_probe.py --provider serper --use-credit
./.venv/bin/python scripts/research_provider_probe.py --provider firecrawl --use-credit
```

## Eventos y exportación

`GET /api/research/events?project_key=...&after=...` requiere `read_research`.
`openceo_research_events` necesita un grant MCP explícito `events` (fuera de los
permisos por defecto). El cursor se limita a proyecto/owner; un id no autoriza.

El outbox atómico guarda payload y cursor juntos; recuperación/replay no ejecutan
el modelo. Las correcciones crean otra versión y conservan historial. Al leer se
revalida retención de los blobs y frescura; la evidencia caducada ya no respalda
claims. `Last-Modified` sirve para HTTP condicional, nunca como fecha de publicación.

`research.export.export_conclusion` es una operación local separada y explícita:
requiere mapping exacto del proyecto, owner y conclusiones públicas revisadas como
durables. El runtime no monta el vault. Seguir `llm-vault-links`, enlazar el
hub y comprobar únicamente las notas creadas con `--project` y rutas explícitas.

Un job que no puede publicar su evento (scope o esquema inválidos) queda anotado con
`event_delivery_failed` y no se reintenta; un fallo transitorio conserva
`event_delivery_pending` y se reintenta en el siguiente arranque. La publicación nunca
impide devolver el job terminado ni arrancar el servicio.

## Cierre humano único

La recurrencia y el piloto social se deciden al terminar todo el proyecto y
nada de eso se activa antes. El research quick ya está encendido en producción
y deep autorizado dentro del mismo techo (#641):
`projects/jardal-openceo/project.yaml` trae el bloque `research` (ver
`Tramo vivo en jardal-openceo`), `RESEARCH_ENABLED=true` en el host y smoke prod
#613 verde (12/12, release `1ce5eed`); el scheduler sigue apagado y no se activa
salvo autorización explícita del Owner con `RESEARCH_SCHEDULER_ENABLED=true`.
El despacho quick usa el perfil free-research (#648): el flip enciende
`RESEARCH_FREE_ROUTER_ENABLED=true` con un snapshot vigente, o la síntesis
queda `blocked` con `failure_reason`.
El proyecto autoriza hasta `paid_budget_usd: "2.00"` (#641); la política
efectiva sigue siendo el mínimo entre servidor y proyecto. `RESEARCH_ENABLED` abierto
admite `/api/research/*` a quien tenga `run_research`; no inicia nada recurrente
por sí solo.

**Demostración B (recurrencia desplegada).** Ver `Demo B` (ticket #643): feed
autorizado único fijado por el Owner, sonda registrada, verificación pre-flip
con servicio simulado y runbook productivo con ambas flags permanentes. El
bloque vivo trae adaptadores, presupuesto y límites (ver
`Tramo vivo en jardal-openceo`); encender el scheduler sigue siendo decisión
del dueño.

**Piloto X (hasta cinco posts).** La persona ejecuta `create` y `cookies` con su propia
sesión (login/2FA incluido), acuerda una cuenta o consulta pública y añade
`allowed_account_refs: [ACCOUNT_ID]` al mismo bloque. El agente hace `grant`, una lectura
real, comprueba causas, aislamiento y procedencia, y termina con `retire` y limpieza.
