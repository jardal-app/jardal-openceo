# TASK-2026-00441 — Timers aislados y diseño del kernel de schedule global

> **TEMPORAL (ticket TASK-2026-00441, ejecución n.º 486, padre TASK-2026-00440).** Informe de
> descubrimiento y diseño; no es referencia permanente. Vive en una ruta versionada solo porque
> `docs/work/` está en `.gitignore` y el orquestador integra únicamente lo que aparece en
> `git status`. Al cerrar: adjuntarlo como privado al ticket, trasladar a
> `ARCHITECTURE_REFERENCE_2026.md §Internal Scheduler` lo que cada migración haga realidad y
> borrar este fichero. Sin secretos ni datos personales.

Alcance: solo descubrimiento y diseño. Ningún cambio de comportamiento, ningún timer migrado;
`ARCHITECTURE_REFERENCE` y el cron de `model_routing` intactos. Lo afirmado sale de leer el código
en `HEAD` (`cf405f5`). Lo que además se ejecutó está marcado **[ejecutado]**; el resto es
**[leído]**. Nada se probó contra Mongo, systemd ni el servicio vivo. Rutas relativas a
`openceo-core/` salvo indicación.

---

## 1. Resumen ejecutivo

1. El ticket habla de **4** timers; el código tiene **7 temporizadores en proceso** (6 bucles con
   parada propia + 1 `sleep` escondido en el dispatcher) y **1 cron externo**. Además de
   dispatcher, Horizon, research y CEO loop están la recuperación de objetivos, el latido del
   mecánico y DeadlineWatch. La regla de TASK-2026-00440 («nunca un timer propio») los alcanza a
   todos, así que el kernel debe converger los 7.
2. Los cuatro repiten el patrón (intención persistida + `asyncio.Event` + `wait_for`), pero
   **cada uno resolvió distinto** la reclamación, la recuperación, el tope de espera y el fallo.
   Lo que hay que unificar son esas decisiones, no el bucle.
3. El inventario destapó **cinco defectos de clase** (§4). Dos están **reproducidos [ejecutado]** y
   afectan a Horizon hoy: el encolado no despierta a su timer, y la siguiente ocurrencia
   recurrente solo se crea al arrancar.
4. Diseño recomendado: **un kernel con un único punto de espera y N «agendas» por dominio** que
   conservan sus colecciones. Cero migración de datos; migración por dominio, un commit cada uno,
   revertible. Orden: research → Horizon → CEO → recuperación → dispatcher (+DeadlineWatch +
   latido).
5. **Veredicto `model_routing`: queda fuera del kernel** (cron de usuario, proceso aparte,
   calendario fijo). Detalle en §7.

---

## 2. Inventario de timers

«Despertar» = quién llama a `wake()`. «Tope» = máximo `wait_for` aunque no haya vencimiento.

### 2.1 Los cuatro pedidos

| | **Dispatcher** | **Horizon** | **Research** | **CEO loop** |
|---|---|---|---|---|
| Código | `dispatcher.py:145` (bucle `:235`) | `horizon/triggers.py:315` (bucle `:388`) | `research/scheduler.py:570` (bucle `:644`); coordinador `research/workflows.py:93` | `ceo/loop.py:92` (bucle `:227`) |
| Intención persistida | Doc único `dispatcher_backup` `{_id:"backup", next_run_at, reason}` (`db/mongo.py:801-827`), que `_refresh_backup` (`dispatcher.py:550`) recalcula al final de cada ciclo como `min(resume_at de lo que espera, ahora+300 s)`; vacío ⇒ se borra. Más `tasks.resume_at` (índice `(status,resume_at)`, `mongo.py:222`) | Colección `horizon_triggers`, un doc por trabajo y periodo; índices due / dedupe único / owner_time (`mongo.py:395-406`) | Colección `research_schedules`; índices de identidad, dedupe parcial, due, owner_time y lease (`mongo.py:464-483`) | Doc único `ceo_review_trigger` `{_id:"trigger", next_scheduled_at, pending_reasons, last_reactive_at…}` (`mongo.py:849-867`) |
| Próxima espera | Lee el doc backup; `None` ⇒ espera indefinida; fallo de lectura ⇒ 300 s (`dispatcher.py:278-289`) | Una consulta ordenada por `next_run_at` (`mongo.py:2124`) | Una consulta indexada de `next_run_at` (`mongo.py:2600`) | Lee el doc y compara `next_scheduled_at` (`loop.py:274-288`) |
| Tope de espera | **Ninguno** | **3600 s** (`triggers.py:49,404`) | **Ninguno** (`max_sleep_seconds=None`, `scheduler.py:589,653`) | **3600 s** (`loop.py:53,269`) |
| Despertar | `Dispatcher.wake()` (`:181`), inyectado a TaskService (`main.py:232`), mecánico (`:287`), motor de objetivos y fin de lanzamiento | `HorizonScheduler.wake()` (`:366`): **nadie lo llama** (D1) | `ResearchScheduleStore.notify()` (`scheduler.py:415`) enlazado por `bind_waker` (`:412`, `:609`) tras cada create/update/pause/resume/cancel | `CEOLoop.wake(reason)` (`:128`), inyectado a TaskService, ObjectiveService y misiones (`main.py:258-259`) |
| Reclamación contra doble disparo | Por ticket: `claim_queued_ticket` (`mongo.py:771`), `find_one_and_update` queued→in_progress, **sin lease ni fencing** (lo declara su docstring); el ciclo no se reclama | `mark_horizon_trigger_running` (`mongo.py:2133`) `pending→running`, **sin lease ni fencing** | `claim_next_research_schedule` (`mongo.py:2499`): pipeline atómico con **lease + `fencing_token` + `scheduled_for`** | **Ninguna**: no hay estado `running`; un solo bucle serial |
| Recuperación al arranque | `start()` ejecuta `dispatch_cycle()` una vez (`:201-213`) + `mark_interrupted_tasks` en el lifespan (`main.py:441,473`) | `_recover` (`triggers.py:370`): `running` con `started_at < ahora-900 s` vuelve a la cola; caducados ⇒ `expired` | `_recover` (`scheduler.py:634`): lease vencida **hace más de 900 s**; un solo catch-up; `pending_reconcile` | `_ensure_trigger` + `_run_overdue_once` (`loop.py:139-152`): una revisión vencida |
| Fallo / reintento | `LoopSupervisor` (incidente + backoff) copiado inline (`:245-268`) | Backoff 60·2ⁿ hasta 1 h, 3 intentos, luego `failed` (`:480-509`) | Igual y, agotado, vuelve a su cadencia (`:732-764`) | `LoopSupervisor` copiado inline (`:237-261`) |
| Ejecución del handler | Inline en el bucle; lanza tickets con `create_task` | **Inline, serial**: un plan lento bloquea el timer | Inline, serial | **Inline**: una revisión musej bloquea el bucle |
| Arranque / parada | `main.py:531` / `:638` | `main.py:546` (si `HORIZON_ENABLED`) / `:634` | `main.py:617` (si research y su scheduler están activos) / `:631` | `main.py:532` / `:637` |
| Pruebas | `tests/test_dispatcher.py` | `tests/test_horizon_triggers.py`, `test_horizon_plan.py` | `tests/test_research_scheduler.py` (reloj virtual `now_fn`/`waiter`) | `tests/test_ceo_loop.py` |

### 2.2 Temporizadores que el pedido no nombra

| Timer | Dónde | Intención persistida | Tope / despertar | Nota |
|---|---|---|---|---|
| **Recuperación de objetivos** (TASK-2026-00481) | `objectives/recovery_scheduler.py:23` (bucle `:73`); decisiones en `objectives/service.py:590-726` | `objectives.recovery.due_at` | **Tope 3600 s**, mínimo 5 s (`recovery_policy.py:79,334-350`); despierta `_objective_park` (`main.py:264`) | Cada tick llama a `recovery_candidates` (`service.py:619`): lee hasta 100 objetivos en espera y, por cada uno, sus tickets hijos. Sin nada vencido, es un barrido horario |
| **Latido del mecánico** (TASK-2026-00510) | `mechanic/service.py:265-299` | **Ninguna**: `_next_heartbeat` vive en memoria (`:216`), así que cada arranque escanea de inmediato | `heartbeat_minutes=30`; `wake(task_id, trigger, detail)` con cola en memoria (`:228`) | Única excepción declarada al no-polling (AGENTS.md, `.claude/rules/mechanic.md`) |
| **DeadlineWatch** | `reliability/deadlines.py:484-524` | Derivada de `tasks.attempt_deadline_at` y `resume_at` (`deadline_candidates`, `mongo.py:747`) | `asyncio.create_task(sleep(delay))` al plazo futuro más próximo; se rearma en cada `dispatch_cycle` vía `_refresh_deadlines` (`dispatcher.py:354`) | Segundo timer **dentro** del dispatcher, sin `Event`; nunca mata procesos |

### 2.3 Barrida de primitivos: lo que NO es una agenda

Barrida de `asyncio.sleep`, `time.sleep`, `call_later`, `call_at`, `threading.Timer`, `apscheduler`,
`croniter` y `while True`/`while not self._stop*` sobre `openceo-core/` (solo código). No hay
`call_later`, `Timer` ni `apscheduler`. Los únicos bucles con parada propia son los 6 de §2.1-2.2.
El resto es de otra clase y **no entra al kernel**:

- **Cron de `model_routing`**: §7.
- **Despachador de research** (`main.py:673-703`): drena la cola hasta vaciarla y termina; lo lanza
  el evento de admisión (`_wake_research_dispatcher`). Sin espera propia.
- **Colas de consumo** `asyncio.Queue` de Notion, Jardal ERP y visibilidad (`task_service.py:364,366,370`
  y sus bucles `:578,910,934`); el `sleep(1)` de `:590` limita ritmo, no agenda.
- **Reintentos acotados dentro de una ejecución viva**: empuje a Jardal ERP con backoff
  (`task_service.py:1022,2971`, máx. 3 intentos), reintentos de proveedor
  (`agents/dev/session.py:2268,3038`, `orchestration/live.py:270`), `reliability/supervisor.py:133`,
  reloj de adquisición de research (`research/acquisition/base.py:127`).
- **Pausa de reintento de revisores** (`orchestration/review.py:1116`): la única espera en proceso
  de **horas** (≥300 s entre reintentos, plazo de ronda 43 200 s). No es intención persistida; la
  persistencia de la ronda está en el checkpoint `params.review_round`. Observación en §8, no se
  migra.
- **Sondeo de TUI vivas** (`agents/dev/tmux_runtime.py`, `rate_monitor.py:480,489`): observan un
  terminal en ejecución; AGENTS.md deja tmux «solo para sesiones vivas».
- **Bucles de reintento/fallback de modelo** (`model_routing/research_wiring.py:894,960,1011`),
  paginación (`model_routing/refresh.py:377`), recorte de borradores (`orchestration/crystallization.py:878`),
  redirecciones HTTP (`research/policies.py:756`), bucles de protocolo (`events.py:42`,
  websocket `main.py:3395`).
- **`RateMonitor`**: caché con TTL evaluado al leer (`get_status`, `rate_monitor.py:529-536`); sin timer.

---

## 3. Qué comparten y en qué divergen

Comparten: (a) intención en Mongo con instante de vencimiento, (b) `asyncio.Event` coalescido,
(c) «limpiar el evento **antes** de trabajar» para no perder un despertar, (d) arranque con
recuperación acotada, (e) `LoopSupervisor` o reintento acotado.

Divergen (cada fila es una decisión que el kernel debe tomar **una vez**):

| Decisión | Hoy |
|---|---|
| Reclamación | 4 modelos: sin reclamo de ciclo / `pending→running` plano / lease+fencing / ninguno |
| Recuperación | umbral por antigüedad de 900 s (Horizon, research) / re-ejecución idempotente (dispatcher, CEO) |
| Tope | 3600 s en Horizon, CEO y recuperación de objetivos; ninguno en dispatcher y research |
| Siguiente ocurrencia | en el mismo parche del cierre (research) / documento nuevo por periodo, **solo al arrancar** (Horizon) / recalculada al cerrar (CEO) / recalculada por ciclo (dispatcher) |
| Ejecución | inline y serial en todos: un handler lento bloquea su propio timer |
| Primitivos duplicados | `_aware` ×6 (`dispatcher.py:69`, `ceo/loop.py:63`, `research/scheduler.py:99`, `research/watch.py:606`, `research/evidence.py:377`, `horizon/review.py:44`), `backoff_seconds` ×2 (`triggers.py:122`, `scheduler.py:215`), `STALE_RUNNING_SECONDS=900` ×2, bloque `LoopSupervisor` ×2 |

---

## 4. Defectos de clase encontrados

Se registran, **no se corrigen** (fuera de alcance). Cada uno se cierra por contrato del kernel en
la fase indicada, no con un parche por caso (CLAUDE.md §4.6).

**D1 — El encolado de Horizon no despierta a su timer. [ejecutado]**
`HorizonTriggerStore.enqueue` (`triggers.py:233`) no tiene waker. `main.py:317-323` construye
`horizon_store` y `horizon_scheduler` y nunca los enlaza; `HorizonScheduler.wake` no tiene
llamadores; y `HorizonService.enqueue_replan` (`horizon/service.py:1536`, invocado sin `store` en
`:1616`) crea un store desechable. Sonda con las clases reales y `FakeHorizonRepo`: tras
`store.enqueue(...)`, `scheduler._wake.is_set()` es `False`. Efecto: una replanificación reactiva
espera hasta el menor entre el siguiente vencimiento y el tope de 3600 s. → Fase 3.

**D2 — Las ocurrencias recurrentes de Horizon no se rearman. [ejecutado]**
`ensure_upcoming` solo se invoca desde `HorizonService.ensure_upcoming_triggers`, cuyo **único**
llamador de producción es el arranque (`main.py:543`). `_process` (`triggers.py:462-478`) marca
`done` y no encola nada, y el `handle_trigger` real (`horizon/service.py:1475-1501`) tampoco.
Sonda: al arrancar se crean 4 disparadores (uno por tipo); se ejecuta el `daily_plan` con el reloj
fijado en su vencimiento; el handler corre, la fila queda `done` y **no queda ningún `daily_plan`
pendiente** (una sola fila). Efecto: cada arranque produce **una** pasada de plan diario, cierre
de día, revisión semanal y mensual; después no hay más hasta el siguiente reinicio. Sin verificar:
el Mongo vivo, y si `HORIZON_ENABLED` está activo en el `.env` de producción (por defecto `True`).
→ Fase 3. **Prioridad alta: es un fallo visible hoy y no depende del kernel.**

**D3 — La recuperación por antigüedad no cubre un reinicio rápido. [leído]**
Horizon recupera `running` con `started_at < ahora-900 s` (`mongo.py:2176`); research, leases con
`lease_expires_at <= ahora-900 s` (`mongo.py:2619`) aunque la lease dura 120 s
(`scheduler.py:82`). Con `RestartSec=10` (`deploy/systemd/openceo-core-host.service:18`), un fallo a
mitad de ejecución se recupera casi siempre **antes** de los 900 s; la recuperación solo corre al
arrancar y no vuelve a ejecutarse, así que el doc queda `running`: Horizon no vuelve a leerlo
(`list_due` solo mira `pending`) y su clave de dedupe impide recrearlo; research exige
`run_state=idle` para reclamar. Con instancia única por flock (CLAUDE.md §3), todo `running` de un
arranque anterior está huérfano **al instante**: la propiedad correcta es el identificador de
arranque, no la edad. → Fases 2-3.

**D4 — Los topes de 3600 s sostienen comportamiento sin declararlo. [leído]**
(i) CEO: una ráfaga en debounce deja `pending_reasons` persistidas y se vacía en el primer
despertar posterior a `CEO_REACTIVE_MIN_INTERVAL_SECONDS` (3600 s por defecto, `loop.py:168-172`),
que hoy es el despertar del tope (`loop.py:298-323`). Sin tope y sin vencimiento persistido, se
retrasaría hasta la siguiente revisión programada o el siguiente evento. (ii) Recuperación de
objetivos: relee hasta 100 objetivos y sus hijos cada hora aunque esté ociosa. (iii) Horizon cubre
con el tope el despertar que D1 no entrega. Son sondeos según la definición del propio repo: el
kernel necesita **vencimientos persistidos para cada espera real** y ningún tope. → Fases 3-5.

**D5 — Un escritor fuera de proceso no puede despertar el bucle. [leído]**
`ops/resume_pedido.py:84-96` reescribe `ceo_review_trigger.next_scheduled_at` con
`save_ceo_trigger` desde fuera del bucle; un `asyncio.Event` no cruza procesos (lo declara el
docstring de `ResearchScheduleStore`, `scheduler.py:400-405`). Surtiría efecto al siguiente
despertar (hasta 1 h). Solo hay llamadores en tests (`tests/test_resume_pedido_349.py`); la vía de
uso real sería una skill de operaciones (sin verificar). El repo no configura replica set ni usa
`watch()`, y no pude comprobar el Mongo desplegado: un change stream no se puede dar por
supuesto. → decisión en §8.

**D6 (menor) — Ajuste muerto y duplicación.** `RESEARCH_SCHEDULER_MAX_SLEEP_SECONDS`
(`settings.py:424`) no lo lee ningún código. `ceo_loop` y `dispatcher` copian el bloque de
`LoopSupervisor`. El docstring de `research/scheduler.py:1-12` describe el tope de Horizon como
motivo para no reutilizarlo (correcto hoy, obsoleto tras la migración). Se resuelve al migrar.

---

## 5. Diseño del kernel

### 5.1 Principio

> Un único punto de espera en el proceso; cada dominio conserva su agenda (colección, campos,
> handler, reglas) y despierta al kernel. El kernel no conoce tickets, planes ni investigaciones.

Es lo que ya dice `ARCHITECTURE_REFERENCE §Internal Scheduler` (TASK-2026-00440). Se descarta
mover todo a una colección `schedules` única: obligaría a migrar datos de 5 dominios y
`tasks.resume_at` lo leen además UI, Horizon, mecánico y DeadlineWatch.

### 5.2 Contrato

Sketch de pseudocódigo Python (módulo nuevo, p. ej. `openceo-core/schedule_kernel/`; nombre que no
choque con `research/scheduler.py` ni `model_routing/schedule.py`):

```python
class Agenda(Protocol):
    name: str
    lanes: int                     # concurrencia del handler; 1 conserva la semántica serial de hoy
    async def next_due(self) -> datetime | None
        # UNA consulta indexada al vencimiento más próximo RECLAMABLE. None = nada que esperar.
    async def claim_due(self, now: datetime, limit: int) -> list[Claim]
        # Atómica (find_one_and_update condicional). Un Claim lleva boot_id y fencing_token.
    async def run(self, claim: Claim) -> Outcome
        # Handler del dominio. Puede ser largo.
    async def settle(self, claim: Claim, outcome: Outcome) -> None
        # Persiste, en el MISMO parche fenced: resultado, próxima ocurrencia (si recurrente),
        # o reintento con backoff acotado, o estado terminal.
    async def recover_at_boot(self, boot_id: str) -> int
        # Una sola vez. Libera todo claim de otro boot_id. Sin umbral de edad.

class ScheduleKernel:
    def register(self, agenda: Agenda) -> None   # antes de start()
    def wake(self, agenda: str) -> None          # síncrono, coalescido, confinado al event loop
    async def start(self) -> None                # recover_at_boot de cada agenda, luego el bucle
    async def stop(self) -> None
    def status(self) -> dict                     # next_due, en vuelo y último error por agenda
```

Los dominios mantienen su API pública (`Dispatcher.wake`, `CEOLoop.wake(reason)`,
`ResearchScheduleStore.notify`…) como delgados delegados de `kernel.wake(nombre)`: **los
productores existentes (TaskService, motor de objetivos, mecánico) no cambian**. `wake` está
confinado al event loop, igual que hoy; ningún waker actual se llama desde otro hilo.

### 5.3 Bucle (el único timer)

```
estado: due[agenda], sucias, en_vuelo[agenda], evento wake

mientras no parado:
    wake.clear()                                   # ANTES de trabajar (patrón vigente)
    para cada agenda en sucias (o sin due conocido):
        due[agenda] = await agenda.next_due(); quitar de sucias
    ahora = reloj(); lanzado = falso
    para cada agenda con due <= ahora:
        libres = agenda.lanes - en_vuelo[agenda]
        si libres == 0: continuar                  # carril lleno: no cuenta para la espera
        claims = await agenda.claim_due(ahora, libres)
        si claims:
            lanzar cada claim en el carril de su agenda
            sucias += agenda; lanzado = verdadero
        si no:                                     # carrera perdida: el estado ya cambió
            due[agenda] = await agenda.next_due()
            si due[agenda] <= ahora:               # incumple el contrato de next_due
                registrar incidente; due[agenda] = None   # sin reintento en caliente
    si lanzado: continuar
    futuros = [d para d en due.values() si d no es None y d > ahora]
    espera = min(futuros) - ahora si futuros si no None    # None => espera indefinida
    await wait_for(wake.wait(), timeout=espera)    # único sleep de todo el proceso

al terminar un carril: en_vuelo[agenda] -= 1; sucias += agenda; wake.set()
```

Reglas:
- **Sin tope por defecto.** `safety_net_seconds=None`: es un parámetro del kernel, no de cada
  dominio; si el Owner quiere uno, es único y visible. Riesgo aceptado: `asyncio` mide con reloj
  monotónico; si el host llegara a suspenderse, se añade ese tope único.
- **Sin giro en caliente.** `espera > 0` por construcción: solo cuentan vencimientos estrictamente
  futuros. Una agenda vencida con el carril lleno queda fuera de la espera y se reevalúa por el
  evento de fin de carril (despertar por evento, no por tiempo). Un claim vacío con `due` vencido
  se reconsulta una vez; si persiste es un incumplimiento de `next_due` y se registra.
- **Carriles por agenda.** Un handler largo (revisión del CEO, plan de Horizon) no puede bloquear el
  despacho de tickets. Hoy cada dominio tiene su bucle y por eso no se bloquean entre sí; un
  kernel con ejecución inline lo rompería. Es el riesgo de diseño más fácil de pasar por alto.
- **Evento sin intención persistida** (dispatcher, mecánico, CEO reactivo): `wake` marca la agenda
  «caliente» (`due = ahora`); `claim_due` decide si hay trabajo.
- **`wake` invalida solo su agenda**: una oleada de despertares cuesta una consulta, no cinco.
- **Fallos del handler**: `LoopSupervisor` aplicado una vez por carril (incidente + backoff), en
  lugar de copiarlo en cada dominio.
- **Reloj y espera inyectables** (`now_fn`/`waiter`, la costura que ya usa research) para probar
  con reloj virtual sin dormir.

### 5.4 Cómo se cumple cada restricción del ticket

| Restricción | Mecanismo |
|---|---|
| Intención persistida con `next_run_at` | Cada agenda guarda su `next_run_at` indexado; el kernel no tiene colección ni intención propia |
| Despertar por CRUD/evento | `kernel.wake(agenda)` desde el mismo punto donde hoy se escribe la intención; contrato de test «toda escritura de intención despierta» (cierra D1) |
| Claim atómico contra doble disparo | `claim_due` = `find_one_and_update` condicional; `fencing_token` en `settle` (existe en research, se generaliza). Dispatcher: el ciclo es idempotente y el reclamo atómico por ticket (`claim_queued_ticket`) se conserva |
| Sin barridos ni polling | Sin tope; un `next_due` indexado por agenda y solo al despertar o tras ejecutar; la recuperación de objetivos pasa de «releer 100 objetivos» a un índice sobre `recovery.due_at` |
| Recuperación acotada al arranque | `recover_at_boot(boot_id)` una vez, antes del bucle, por **propiedad de arranque** y no por edad (cierra D3). Un único catch-up por elemento atrasado (regla vigente de research) |

### 5.5 Qué conserva cada dominio (las 5 agendas)

| Agenda | `next_due()` | `claim_due()` | `run()` | Lo que cambia |
|---|---|---|---|---|
| research | `next_research_schedule_due_at` | `claim_next_research_schedule` (ya fenced) | `ResearchScheduleHandler` | Se extrae `_drain_due/_process/_complete/_retry`; el repo no cambia |
| Horizon | `next_horizon_trigger_due_at` | `mark_horizon_trigger_running` + `claimed_boot_id` | `HorizonService.handle_trigger` | `settle` rearma la siguiente ocurrencia (D2); `enqueue` llama a `kernel.wake` (D1); sin tope |
| CEO | `min(next_scheduled_at, last_reactive_at + intervalo si hay pending_reasons)` | pseudo-reclamo sobre el doc único | el `_cycle` actual | El vencimiento del debounce se persiste (D4); vía para escritores externos (D5) |
| recuperación | índice `objectives (status, recovery.due_at)` | CAS actual en `recover_objective` | `recovery_tick` por objetivo vencido | Deja de releer 100 objetivos por tick (D4) |
| dispatcher | `dispatcher_backup.next_run_at` (ya integra `resume_at`, ver §2.1) | pseudo-reclamo de ciclo; `claim_queued_ticket` sigue por ticket | `dispatch_cycle` | Absorbe el timer de DeadlineWatch (segundo `next_due` de la misma agenda, del plazo futuro más próximo). El backup condicional de 5 min **se conserva tal cual** (decisión vigente del Owner, ARCH §Dispatcher) |

`_refresh_backup` lee los estados `queued` y `waiting` completos en cada ciclo
(`dispatcher.py:558-559`). No es un barrido por tiempo, pero es coste por ciclo acotado por la cola;
queda fuera de este diseño y se anota en §8.

El latido del mecánico se registra como agenda de clase `heartbeat`, de modo que la única excepción
al no-polling queda **en un solo sitio y localizable con grep**. Su `next_heartbeat` pasa a
persistirse (hoy cada arranque escanea de inmediato). Fase final; no bloquea el resto.

### 5.6 Fuera de diseño, a propósito

- Lenguaje cron: ninguna agenda lo necesita hoy (research usa `interval_seconds`/`daily_at`, Horizon
  horas locales, CEO intervalos). Si un workflow recurrente futuro lo exige, se declara una
  biblioteca mantenida en `requirements.txt` (la que ya viene transitiva en el venv no está
  declarada) y no se escribe un parser propio.
- Sin flags de encendido/apagado por dominio: dos caminos de código por fase duplican el riesgo. La
  reversión es `git revert` del commit del dominio (no hay migración de datos que deshacer).

---

## 6. Plan de migración

Principio: **estrangulamiento por dominio**. Cada fase es un commit independiente, con los tests del
dominio en verde sin editarlos (las clases públicas se conservan como agendas), datos intactos salvo
campos aditivos opcionales (`claimed_boot_id`) y reversible. No empiezan hasta que el Owner apruebe
este diseño.

| Fase | Contenido | Por qué en este orden | Prueba de aceptación (dobles, sin modelos vivos) |
|---|---|---|---|
| **1** | Kernel + `Agenda` + carriles + `status()`. Sin cablear en producción | Fija el contrato antes de tocar un dominio | Con agenda falsa y reloj virtual: ocioso = 0 consultas y espera `None`; despertar durante el trabajo no se pierde; `wake(a)` reconsulta solo `a`; un carril lento no bloquea a otro; agenda vencida con carril lleno no gira; claim vacío con `due` vencido se registra una vez y no gira; doble reclamo dispara una vez; claim de otro `boot_id` más joven que 900 s se libera; un solo catch-up; `stop()` cancela carriles |
| **2** | **Research** | Ya espera sin tope, tiene lease+fencing, `bind_waker` y reloj virtual; está **apagado por defecto** ⇒ riesgo de producción mínimo; valida el contrato en un dominio real | Los tests de `test_research_scheduler.py` sin cambios + recuperación por `boot_id` |
| **3** | **Horizon**: waker en `enqueue` (D1), rearme en `settle` (D2), `boot_id` (D3), sin tope | Corrige un fallo visible hoy; dominio pequeño | `test_horizon_triggers.py` + `test_horizon_plan.py`; casos nuevos: encolar despierta; tras ejecutar un `daily_plan` queda el siguiente; un `running` joven tras reinicio se recupera |
| **4** | **CEO loop**: vencimiento de debounce persistido, vía para escritores externos (D4, D5) | Depende de que el kernel ya sea fiable en Horizon | `test_ceo_loop.py`; casos nuevos: ráfaga en debounce se vacía sin tope; `resume_pedido` surte efecto sin esperar 1 h |
| **5** | **Recuperación de objetivos** + índice | Sustituye un sondeo horario real | `test_objective_recovery_481.py` y `test_recovery_deploy_483.py` |
| **6** | **Dispatcher** + DeadlineWatch + latido del mecánico | Último: es la ruta caliente de cada ticket; mayor cobertura previa; toca arranque y parada del lifespan | `test_dispatcher.py` (cubre DeadlineWatch) y `test_mechanic_510.py`; `restart` + `status` conservan instancia única |
| **7** | Limpieza: borrar bucles huérfanos, `_aware`/`backoff` duplicados y el ajuste muerto; pasar a `ARCHITECTURE_REFERENCE` lo que ya sea real | Solo cuando nada los referencia | `ruff` (gate `ruff_swallowed_exceptions`), `py_compile`, suite enfocada de AGENTS.md |

Orden de arranque: hoy `main.py` arranca dispatcher → CEO → recuperación → mecánico (`:531-534`) →
Horizon (`:546`) → research (`:617`, tras el bloque de auth). El kernel se arranca **una vez al
final** de la fase de arranque del lifespan, con todas las agendas registradas; el
`recover_at_boot` de cada una va envuelto para que un fallo no impida el arranque (regla actual de
`dispatcher.start` y `ceo_loop.start`). En la parada, el kernel se detiene primero (cancela sus
carriles) y después lo que hoy cierra `main.py:631-639`.

Aceptación general (AGENTS.md, orden del Owner 2026-10-14): solo lógica/contrato con dobles;
ninguna llamada a modelos vivos ni ensayo integrado.

---

## 7. Veredicto `model_routing`

**Queda fuera del kernel y no se toca.** `no_aplica` a la migración.

Hechos [leídos]: dos líneas de crontab de usuario (`15 2 * * *` benchmarks; `20 */6 * * *`
proveedores) generadas por `model_routing/schedule.py:41-57`, que lanzan `free-router update` como
**proceso aparte**; la exclusión mutua es un `flock` en `run_update` (no hay `flock` externo en el
cron, lo dice el docstring de `render_crontab_block`); escribe instantáneas en
`~/.config/free-router/`. La activación en el host pertenece a TASK-2026-00299.

Razones:
1. **Frontera de proceso.** Un `asyncio.Event` no cruza procesos y el refresco no debe depender de
   que el core esté vivo.
2. **No es un «timer propio» en el sentido de TASK-2026-00440**, que apunta a bucles `asyncio`
   aislados dentro del core. Es un trabajo por lotes de calendario fijo, sin intención en Mongo ni
   despertar por CRUD.
3. **Ya está declarado externo:** `ARCHITECTURE_REFERENCE` («Daily benchmark research/update
   remains external, with no new periodic polling»).
4. **El lado consumidor ya es reactivo y por fichero:** los revisores cargan la instantánea una vez
   por ronda, y el CEO lee las alertas del refresco montadas sobre una revisión existente
   (`ceo/loop.py:329-365`).

No verificado: **si el bloque está instalado en el crontab del host** (`crontab -l` dio
`Permission denied` desde esta sesión). No afecta al veredicto.

---

## 8. Decisiones y riesgos para quien apruebe el diseño

Decisiones que tomé como rutinarias (revisables): sin tope por defecto; carriles de concurrencia 1
por agenda; adaptadores sobre colecciones existentes en vez de colección única; sin flags por fase;
el latido del mecánico converge como clase `heartbeat` pero en la última fase.

Decisión que **no** es mía:
- **D5, vía para escritores fuera de proceso.** Opciones: (a) ruta interna autenticada del propio
  core que llama a `kernel.wake` (el core ya sirve FastAPI y MCP interno) — recomendada; (b) aceptar
  la latencia hasta el siguiente despertar y documentarla; (c) replica set de Mongo para change
  streams — descartada por coste operativo desproporcionado.

Riesgos:
- Ejecución inline en el kernel rompería el aislamiento actual entre dominios ⇒ carriles (§5.3).
- Quitar los topes sin cerrar D1/D4/D5 **empeora** la latencia hoy escondida ⇒ las fases 3-5
  retiran el tope de cada dominio solo junto con su vencimiento persistido.
- El dispatcher es la ruta caliente: su fase va última y sin cambios de semántica de admisión.
- La cápsula con los criterios de aceptación del objetivo no está en el ticket (sin adjuntos); el
  diseño responde a los criterios del texto del ticket. Si la cápsula añade criterios, contrastarlos
  con §5.4.

Observaciones fuera de alcance (no se actúa): la pausa de reintento de revisores de horas
(`orchestration/review.py:1116`) y la lectura completa de `queued`/`waiting` por ciclo del
dispatcher (§5.5) son candidatas a revisar después del kernel, no parte de él.

---

## 9. Evidencia de esta sesión

- **[ejecutado]** Línea base: `./.venv/bin/python -m pytest tests/test_horizon_triggers.py tests/test_ceo_loop.py tests/test_research_scheduler.py tests/test_dispatcher.py tests/test_objective_recovery_481.py -q` → `176 passed, 10 subtests passed`.
- **[ejecutado]** Sondas de D1 y D2: script fuera del repo contra `HorizonTriggerStore`,
  `HorizonScheduler` y `FakeHorizonRepo` reales (`tests/horizon_support.py`) →
  `D1 wake set after enqueue: False`; `D2 created_at_startup=4 handler_ran=['daily_plan']`,
  `daily_plan` en `done`, `pending daily_plan after run: 0`. La primera versión de la sonda D2
  daba «0» **de forma vacía** (el reloj la hacía expirar antes de ejecutar); se corrigió y se
  comprobó que el handler corre antes de aceptar el resultado.
- **[ejecutado]** Barrida de primitivos de temporización sobre `openceo-core/` (§2.3).
- **[leído]** Todo lo demás, con `fichero:línea`.
- No se ejecutó nada contra Mongo, systemd ni modelos. No se tocó código de producción, así que no
  hay `py_compile` que ejecutar sobre ficheros modificados.
