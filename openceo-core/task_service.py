from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from company_mandate import (
    ESCALATION_PHASE,
    ESCALATION_TAG,
    SECRET_BLOCK_BUSINESS_STATUS,
    SECRET_BLOCK_PARAM,
    EscalationDecision,
    MandateError,
    OwnerAction,
    build_escalation_ticket,
    build_secret_block,
    project_requires_human_review,
    requires_owner,
)
from db.mongo import MongoRepository
from events import RealtimeHub
from graph import OpenCEOGraph
from horizon.privacy import is_personal_scope
from horizon.promotion import is_horizon_promoted
from objectives.engine import ObjectiveRejected
from phase_workflows import (
    find_phase_index,
    first_executable_phase_index,
    merge_workflow_templates,
    normalize_phases,
    phase_key,
    phase_label,
    resolve_template_key,
)
from qa_review_service import QAReviewService
from rate_monitor import RateMonitor
from settings import Settings
from state import (
    AgentName,
    ApprovalState,
    TaskCreateRequest,
    TaskPriority,
    TaskReviewAction,
    TaskReviewActorType,
    TaskReviewCreateRequest,
    TaskSource,
    TaskStatus,
    WorkflowPhaseType,
    WorkflowRunStatus,
    make_task_document,
    make_task_review_document,
)
from tools.erpnext_client import (
    ERPNextDependencyBlockedError,
    ERPNextDuplicateError,
    ERPNextError,
    ERPNextPermissionError,
    ERPNextUnavailable,
    ERPNextValidationError,
)
from tools.erpnext_tickets import (
    depends_on_names,
    normalize_transition_status,
    resolve_erpnext_status,
    task_needs_human_review,
)

if TYPE_CHECKING:
    from tools.erpnext_tickets import ERPNextTicketAdapter
    from tools.notion import NotionTicketManager
    from workflow_service import WorkflowExecutionOutcome, WorkflowService

logger = logging.getLogger(__name__)

_NOTION_MIRROR_EVENT_KEY = "notion_mirror_created"

#: Marca en `params._mirrors.erpnext` de un ticket cuyo enlace de negocio quedo pendiente.
ERPNEXT_PENDING_REASON = "erpnext_pending"


# Estados que cierran un ticket. Horizon reacciona a la transicion hacia uno de
# ellos para replanificar, nunca a los parches posteriores.
_TERMINAL_TASK_STATUSES = frozenset(
    {
        TaskStatus.COMPLETED.value,
        TaskStatus.CONVERTED.value,
        TaskStatus.FAILED.value,
        TaskStatus.ESCALATED.value,
        TaskStatus.CANCELLED.value,
    }
)

#: Estados de ERPNext que son una decision de NEGOCIO y por tanto se propagan de vuelta
#: al runtime. El resto (Open, Working, Pending Review, Overdue, Template) son eco de lo
#: que OpenCEO ya empujo: propagarlos seria un bucle.
_ERPNEXT_BUSINESS_TO_TASK_STATUS: dict[str, str] = {
    "Completed": TaskStatus.COMPLETED.value,
    "Cancelled": TaskStatus.CANCELLED.value,
}

#: Destino de transicion explicita (vocabulario ERPNext) -> estado de ejecucion
#: (vocabulario OpenCEO). Es el espejo el que se alinea: `status_pushed` guarda
#: SIEMPRE este lado, nunca el de ERPNext (TASK-2026-00132).
_ERPNEXT_TO_TASK_STATUS: dict[str, str] = {
    "Open": TaskStatus.QUEUED.value,
    "Working": TaskStatus.IN_PROGRESS.value,
    "Pending Review": TaskStatus.WAITING.value,
    "Completed": TaskStatus.COMPLETED.value,
    "Cancelled": TaskStatus.CANCELLED.value,
}

#: Nombres de negocio dentro de un mensaje de Frappe (el 417 por dependientes
#: nombra al bloqueado y a un dependiente entre etiquetas HTML).
_TASK_NAME_PATTERN = re.compile(r"TASK-\d+-\d+")

#: Tope de la auditoria de transicion en el espejo: una cita, no un volcado.
_TRANSITION_AUDIT_MAX = 500


def _transition_audit_text(value: Any, limit: int = _TRANSITION_AUDIT_MAX) -> str:
    return " ".join(str(value or "").split())[:limit]


def _classify_transition_error(exc: Exception) -> dict[str, Any]:
    """Fallo del adapter -> resultado clasificado (mismo vocabulario que el MCP)."""
    if isinstance(exc, ERPNextPermissionError):
        status = "forbidden"
    elif isinstance(exc, ERPNextUnavailable):
        status = "unavailable"
    elif isinstance(exc, ERPNextDuplicateError):
        status = "duplicate"
    elif isinstance(exc, ERPNextValidationError):
        status = {404: "not_found", 413: "too_large", 400: "invalid_input"}.get(
            exc.status_code, "invalid_input"
        )
    else:
        status = "error"
    return {"ok": False, "status": status, "message": str(exc)}


def _transition_cancel_needs_owner(
    business: dict[str, Any] | None, task: dict[str, Any] | None
) -> tuple[bool, str]:
    """True cuando cancelar exige citar la orden del dueno. Fail-closed.

    Automatico solo lo probadamente creado por agentes (hijo de objetivo,
    spillover, `created_by` de agente) que no sea un grupo ni un escalado:
    abandonar un objetivo es replanificacion estrategica, y retirar una
    pregunta al dueno sin su orden rompe el bucle de decision. Todo lo demas
    (suelto, voz/desk, sin registro de ejecucion, origen desconocido) pregunta.
    """
    if bool((business or {}).get("is_group")):
        return True, "es un grupo (objetivo): abandonarlo es replanificacion estrategica"
    record = task or {}
    tags = [str(tag) for tag in (record.get("tags") or [])]
    if (
        str(record.get("phase") or "") == ESCALATION_PHASE
        or str(record.get("waiting_reason") or "").startswith(f"{ESCALATION_PHASE}:")
        or ESCALATION_TAG in tags
    ):
        return True, "es un escalado al dueno: retirarlo exige su orden"
    source = record.get("source")
    source_value = getattr(source, "value", source)
    agent_marks = (
        bool(str(record.get("objective_id") or "").strip())
        or str(source_value or "") == TaskSource.SPILLOVER.value
        or any(tag.startswith("spillover_from:") for tag in tags)
        or str(record.get("created_by") or "") == "ceo"
    )
    if agent_marks:
        return False, "trabajo creado por agentes"
    return True, "trabajo fijado por el dueno o de origen desconocido"


class TaskService:
    """Task lifecycle orchestration around DB + LangGraph + realtime updates."""

    def __init__(
        self,
        settings: Settings,
        repo: MongoRepository,
        graph: OpenCEOGraph,
        realtime_hub: RealtimeHub,
        notion: NotionTicketManager | None = None,
        rate_monitor: RateMonitor | None = None,
        qa_service: QAReviewService | None = None,
        workflow_service: WorkflowService | None = None,
        erpnext: "ERPNextTicketAdapter | None" = None,
        config_loader: Any | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._graph = graph
        self._hub = realtime_hub
        self._notion = notion
        self._rate_monitor = rate_monitor
        self._qa_service = qa_service
        self._workflow_service = workflow_service
        # Colaborador opcional: sin el (o con ERPNEXT_TICKETS_ENABLED=false) el servicio
        # se comporta exactamente como antes de la integracion. Los tests construyen
        # TaskService con __new__ y sin __init__, asi que todo uso tolera None.
        self._erpnext = erpnext
        # Optional project.yaml reader (autonomy F4 TASK-2026-00118): the
        # default phase template skips human QA unless the project policy
        # demands it. Tests build TaskService without it; every use
        # tolerates None (policy unreadable = keep the QA phase).
        self._config_loader = config_loader
        self._locks: dict[str, asyncio.Lock] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._notion_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._notion_worker: asyncio.Task[None] | None = None
        self._erpnext_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._erpnext_worker: asyncio.Task[None] | None = None
        self._horizon_terminal: Any | None = None
        # Dispatcher wake callback (F3 TASK-2026-00113): main.py injects
        # Dispatcher.wake. Read via getattr: tests build TaskService with
        # __new__ and without __init__, so the attribute may not exist.
        self._dispatcher_wake: Any | None = None
        self._ceo_wake: Any | None = None

    def set_dispatcher_wake(self, wake: Any | None) -> None:
        """Inject the dispatcher wake-up (called on every queue event).

        The dependency points outward on purpose: TaskService never imports
        the dispatcher, it just signals. Without a wake the queue events
        are silent (tests, standalone use).
        """
        self._dispatcher_wake = wake

    def _wake_dispatcher(self) -> None:
        """Signal the dispatcher, never breaking the caller."""
        wake = getattr(self, "_dispatcher_wake", None)
        if wake is None:
            return
        try:
            wake()
        except Exception:
            logger.warning("Dispatcher wake failed", exc_info=True)

    def set_ceo_wake(self, wake: Any | None) -> None:
        """Inject the CEO loop wake-up (incident events only).

        Same outward dependency as the dispatcher wake: TaskService
        never imports the CEO loop, it just signals. Without a wake
        the incident events are silent (tests, standalone use).
        """
        self._ceo_wake = wake

    def _wake_ceo_incident(self, task: dict[str, Any] | None) -> None:
        """Signal the CEO loop on a company incident. Never breaks.

        Fires only for failed/escalated executions of company scope:
        personal work stays out of the company review (privacy), and
        routine closes are not incidents. The loop debounces bursts.
        """
        if task is None:
            return
        if str(task.get("status") or "") not in {
            TaskStatus.FAILED.value,
            TaskStatus.ESCALATED.value,
        }:
            return
        if is_personal_scope(task):
            return
        wake = getattr(self, "_ceo_wake", None)
        if wake is None:
            return
        try:
            wake(f"Incident: ticket {task.get('task_id')} "
                 f"{task.get('status')}: "
                 f"{str(task.get('title') or '')[:120]}")
        except Exception:
            logger.warning("CEO wake failed", exc_info=True)

    def set_horizon_hooks(self, *, on_task_terminal: Any | None = None) -> None:
        """Inyecta la reaccion de Horizon a un resultado de tarea.

        La dependencia apunta hacia afuera a proposito: Horizon no importa
        TaskService, es TaskService quien avisa. Sin gancho configurado el flujo
        de tickets funciona igual.
        """
        self._horizon_terminal = on_task_terminal

    async def _notify_horizon_terminal(self, task: dict[str, Any]) -> None:
        """Avisa a Horizon de una tarea personal o promovida terminada.

        Sin poder romperla: un fallo del gancho nunca impide que un ticket se
        cierre. Solo el alcance personal y el trabajo promovido desde Horizon:
        una tarea de empresa ordinaria no pertenece al espacio personal
        (seccion 9.3) y no debe replanificar la vida del usuario.
        """
        if self._horizon_terminal is None or task is None:
            return
        if not is_personal_scope(task) and not is_horizon_promoted(task):
            return
        try:
            await self._horizon_terminal(task)
        except Exception:
            # Un fallo de Horizon nunca puede impedir que un ticket se cierre.
            logger.warning("Horizon no pudo registrar el resultado de la tarea", exc_info=True)

    def start_notion_worker(self) -> None:
        """Start the background worker that drains the Notion mirror queue."""
        if self._notion_worker is not None and not self._notion_worker.done():
            return
        self._notion_worker = asyncio.create_task(self._notion_queue_loop())

    async def stop_notion_worker(self) -> None:
        if self._notion_worker is not None:
            self._notion_worker.cancel()
            try:
                await self._notion_worker
            except asyncio.CancelledError:
                pass
            self._notion_worker = None

    async def _notion_queue_loop(self) -> None:
        """Process Notion mirrors one at a time, ~1 req/s to avoid 429."""
        while True:
            task = await self._notion_queue.get()
            try:
                await self._ensure_notion_mirror(task)
            except Exception:
                logger.warning(
                    "Notion mirror queue error for task %s",
                    task.get("task_id"),
                    exc_info=True,
                )
            finally:
                self._notion_queue.task_done()
            await asyncio.sleep(1)

    def _enqueue_notion_mirror(self, task: dict[str, Any]) -> None:
        """Put a task into the Notion mirror queue (non-blocking)."""
        self._notion_queue.put_nowait(task)

    # --- ERPNext: registro de negocio -------------------------------------
    #
    # ERPNext es dueno del ticket de NEGOCIO; Mongo conserva la EJECUCION. La direccion
    # esta en docs/OPENCEO_DIRECTION.md v1.1 y el contrato en
    # docs/ARCHITECTURE_REFERENCE_2026.md §Read and Write Boundaries.
    #
    # Solo la creacion es sincrona: "escribir una tarea en Mongo no acredita un registro
    # exitoso en ERPNext". Las transiciones de estado (Fase 3) van por cola y nunca
    # bloquean el camino del request.

    def _erpnext_ref(self, doc: Any) -> str:
        """Clave de idempotencia determinista del ticket de negocio.

        Con `source_id` se deriva de el, asi que un reintento y una llamada concurrente
        producen el MISMO valor, y ERPNext deduplica aunque Mongo todavia no tenga el
        documento. Sin `source_id` queda el `task_id`, que es un identificador generado y
        por tanto NO protege ante un reintento del llamador: para ese caso `POST /api/tasks`
        acepta la cabecera `Idempotency-Key`, que se convierte en `source_id` sintetico.
        """
        source = getattr(doc, "source", None)
        source_value = getattr(source, "value", None) or str(source or "")
        source_id = str(getattr(doc, "source_id", "") or "").strip()
        if source_id:
            return f"{source_value}:{source_id}"
        return str(getattr(doc, "task_id", "") or "")

    def _merge_erpnext_mirror_params(
        self, params: dict[str, Any] | None, mirror: dict[str, Any]
    ) -> dict[str, Any]:
        merged = dict(params or {})
        mirrors = merged.get("_mirrors")
        if not isinstance(mirrors, dict):
            mirrors = {}
        current = mirrors.get("erpnext")
        if isinstance(current, dict):
            current.update(mirror)
            mirror = current
        mirrors["erpnext"] = mirror
        merged["_mirrors"] = mirrors
        return merged

    async def _blocked_by_business_names(self, blocked_by: Any) -> list[str]:
        """Resuelve `blocked_by` (task_ids de ejecucion) a nombres `TASK-...`.

        Es la mitad OpenCEO -> ERPNext del mapeo `blocked_by` <-> `depends_on`
        (TASK-2026-00013): cada bloqueador aporta el `name` de su espejo de negocio.
        Una entrada que ya es un nombre de negocio pasa tal cual; un bloqueador sin
        espejo (sin contraparte en ERPNext) no puede referenciarse y se omite.
        """
        names: list[str] = []
        repo = getattr(self, "_repo", None)
        for entry in blocked_by or []:
            raw = str(entry or "").strip()
            if not raw or raw in names:
                continue
            if raw.upper().startswith("TASK-"):
                names.append(raw)
                continue
            if repo is None:
                continue
            try:
                blocker = await repo.get_task(raw)
            except Exception:
                logger.warning("No se pudo resolver el bloqueador %s", raw, exc_info=True)
                blocker = None
            mirror = self._task_params(blocker or {}).get("_mirrors", {})
            business = mirror.get("erpnext", {}) if isinstance(mirror, dict) else {}
            name = str(business.get("name") or "").strip() if isinstance(business, dict) else ""
            if name and name not in names:
                names.append(name)
            elif not name:
                logger.info("Bloqueador %s sin espejo de negocio: no entra en depends_on.", raw)
        return names

    async def _register_business_ticket(
        self,
        doc: Any,
        *,
        parent_task: str | None = None,
        depends_on: list[str] | None = None,
        is_group: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Crea el ticket de negocio en ERPNext ANTES de persistir en Mongo.

        Devuelve `(mirror, pending)`:

        - `(None, False)` — la integracion esta apagada, o el ticket es de alcance personal
          y el espejo externo personal esta deshabilitado.
        - `(mirror, False)` — el ticket quedo registrado en el sistema de negocio.
        - `(mirror, True)` — ERPNext no respondio. El enlace queda pendiente y el llamador
          debe impedir que arranque ejecucion sobre trabajo que no existe en negocio.

        Lanza si ERPNext **rechaza** los datos: ahi no se persiste nada en Mongo, porque un
        ticket de ejecucion sin contraparte de negocio es exactamente el "competing business
        record" que el contrato prohibe.
        """
        adapter = getattr(self, "_erpnext", None)
        if adapter is None:
            # Solo ocurre en tests, que inyectan el adapter explicitamente. En runtime
            # `main.py` siempre lo construye.
            return None, False
        if not adapter.is_enabled:
            # No configurado NO es "apagado": es un error de despliegue. Se falla fuerte
            # en vez de crear un ticket que el sistema de negocio nunca vera.
            raise ERPNextValidationError(
                adapter.client.config_error() or "ERPNext no esta configurado."
            )

        snapshot = doc.model_dump(mode="python")
        if is_personal_scope(snapshot) and not getattr(
            self._settings, "HORIZON_PERSONAL_MIRROR_ENABLED", False
        ):
            return None, False

        ref = self._erpnext_ref(doc)
        params = snapshot.get("params") if isinstance(snapshot.get("params"), dict) else {}
        priority = getattr(doc.priority, "value", None) or str(doc.priority or "")
        now = datetime.now(timezone.utc).isoformat()

        # Jerarquia de negocio (TASK-2026-00013): el padre va tal cual y las
        # dependencias son la union del `depends_on` explicito con el `blocked_by`
        # resuelto a nombres de negocio.
        business_parent = str(parent_task or "").strip() or None
        business_deps = [str(entry or "").strip() for entry in depends_on or []]
        business_deps = [entry for entry in business_deps if entry]
        for name in await self._blocked_by_business_names(snapshot.get("blocked_by")):
            if name not in business_deps:
                business_deps.append(name)

        try:
            result = await adapter.create_ticket(
                ref=ref,
                title=snapshot.get("title") or "",
                description=snapshot.get("description") or "",
                project_key=snapshot.get("project"),
                priority=priority,
                ticket_type=params.get("ticket_type"),
                tags=snapshot.get("tags"),
                due_date=params.get("due_date"),
                ticket_number=snapshot.get("ticket_number"),
                task_id=snapshot.get("task_id"),
                # Provider y origen son visibilidad de negocio: sin el provider, el
                # tablero oculta que un ticket es Tier S (lento, caro, multi-revisor).
                coding_tool=snapshot.get("coding_tool"),
                external_url=params.get("external_url"),
                parent_task=business_parent,
                depends_on=business_deps or None,
                is_group=is_group,
            )
        except ERPNextUnavailable as exc:
            logger.warning("ERPNext no respondio al registrar el ticket %s: %s", ref, exc)
            # Misma forma que la rama de exito, con `name` vacio: quien lea el espejo no
            # tiene que distinguir dos esquemas.
            return {"name": None, "ref": ref, "pending": True, "linked_at": now}, True
        except ERPNextError:
            logger.exception("ERPNext rechazo el registro del ticket %s", ref)
            raise

        task = result.get("task") or {}
        # Ojo: `TaskStatus` es `str, Enum`, pero `str(TaskStatus.QUEUED)` da
        # 'TaskStatus.QUEUED' y no 'queued'. Sin `.value` el valor guardado nunca
        # coincidiria con el que devuelve Mongo y cada parche pareceria una transicion.
        status_value = snapshot.get("status")
        status_value = getattr(status_value, "value", status_value)
        mirror: dict[str, Any] = {
            "name": task.get("name"),
            "ref": ref,
            # `status_pushed` guarda SIEMPRE el vocabulario de OpenCEO, no el de
            # ERPNext: el observador compara contra el, y comparar `in_progress`
            # contra `Working` haria que cada parche pareciera una transicion nueva.
            "status_pushed": str(status_value or ""),
            "pending": False,
            "linked_at": now,
        }
        # Lo que se empujo de jerarquia queda en el espejo (retiene la referencia
        # de negocio, ARCH §Parent Task Tracking); la autoridad sigue en ERPNext.
        if business_parent:
            mirror["parent_task"] = business_parent
        if business_deps:
            mirror["depends_on"] = list(business_deps)
        if is_group:
            mirror["is_group"] = True
        return mirror, False

    async def _enqueue_erpnext_registration(self, task: dict[str, Any]) -> None:
        """Reintenta el enlace de negocio de un ticket que quedo pendiente.

        Se llama en el camino de idempotencia: si el ticket ya existe en Mongo pero su
        espejo de ERPNext esta pendiente o ausente, hay que repararlo en vez de devolverlo
        como si estuviera completo. Fire-and-forget: nunca puede romper la creacion.
        """
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not adapter.is_enabled:
            return

        params = self._task_params(task)
        mirrors = params.get("_mirrors")
        existing = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
        if isinstance(existing, dict) and existing.get("name") and not existing.get("pending"):
            return

        ref = ""
        if isinstance(existing, dict):
            ref = str(existing.get("ref") or "").strip()
        if not ref:
            return

        try:
            task_doc = await adapter.find_by_ref(ref)
            if task_doc is not None:
                mirror = {
                    "name": task_doc.get("name"),
                    "ref": ref,
                    # Vocabulario de OpenCEO, no el de ERPNext (ver _register_business_ticket).
                    "status_pushed": str(getattr(task.get("status"), "value", task.get("status")) or ""),
                    "pending": False,
                    "linked_at": datetime.now(timezone.utc).isoformat(),
                }
                await self._repo.patch_task(
                    task["task_id"],
                    {
                        "params": self._merge_erpnext_mirror_params(params, mirror),
                        "waiting_reason": None,
                    },
                )
                # The business link healed: a held ticket may be dispatchable.
                self._wake_dispatcher()
                logger.info("Enlace ERPNext reparado para el ticket %s -> %s", task["task_id"], mirror["name"])
        except Exception:
            logger.warning("No se pudo reparar el enlace ERPNext del ticket %s", task.get("task_id"), exc_info=True)

    # --- ERPNext: transiciones de estado ----------------------------------
    #
    # `MongoRepository.patch_task` llama a `observe_task_status` en CADA transicion, asi
    # que aqui no hay que conocer los sitios que escriben estado. La comparacion es contra
    # el ultimo estado EMPUJADO (no contra el anterior en la base), lo que hace el envio
    # idempotente y lo vuelve independiente del orden entre parches concurrentes.

    def start_erpnext_worker(self) -> None:
        if self._erpnext_worker is not None and not self._erpnext_worker.done():
            return
        self._erpnext_worker = asyncio.create_task(self._erpnext_queue_loop())

    async def stop_erpnext_worker(self) -> None:
        if self._erpnext_worker is not None:
            self._erpnext_worker.cancel()
            try:
                await self._erpnext_worker
            except asyncio.CancelledError:
                pass
            self._erpnext_worker = None

    async def observe_task_status(self, task: dict[str, Any]) -> None:
        """Recibe cada transicion de estado. Nunca hace I/O: solo encola."""
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not adapter.is_enabled or not task:
            return
        if is_personal_scope(task) and not getattr(
            self._settings, "HORIZON_PERSONAL_MIRROR_ENABLED", False
        ):
            return

        params = self._task_params(task)
        mirrors = params.get("_mirrors")
        mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
        if not isinstance(mirror, dict):
            return
        name = str(mirror.get("name") or "").strip()
        if not name:
            # El enlace de negocio todavia esta pendiente: lo repara la reconciliacion.
            return

        status = str(task.get("status") or "")
        tier_s_progress = task.get("tier_s_progress")
        if (
            status == TaskStatus.WAITING.value
            and isinstance(tier_s_progress, dict)
            and tier_s_progress.get("status") in {"waiting", "cancelled", "failed"}
        ):
            # Operational pause (quota/question) is not ERPNext's business review.
            # The existing Task stays Working; description shows Tier S progress.
            return
        if status == str(mirror.get("status_pushed") or ""):
            return
        if resolve_erpnext_status(task) is None:
            # Estado que no se empuja (failed, escalated, interrupted,
            # converted: el padre se completa al cerrar el objetivo, §9.7;
            # esperas operativas sin accion humana, TASK-2026-00118: el
            # negocio conserva Open/Working).
            return

        queue = getattr(self, "_erpnext_queue", None)
        if queue is not None:
            queue.put_nowait({"task_id": task.get("task_id"), "name": name, "status": status})

    async def _erpnext_queue_loop(self) -> None:
        while True:
            item = await self._erpnext_queue.get()
            try:
                await self._push_status(item)
            except Exception:
                logger.warning("Fallo empujando un estado a ERPNext", exc_info=True)
            finally:
                self._erpnext_queue.task_done()

    async def _push_status(self, item: dict[str, Any]) -> None:
        """Empuja una transicion con reintento real, no un sleep fijo."""
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not adapter.is_enabled:
            return

        # QA condicional (TASK-2026-00040) y esperas con accion humana
        # (TASK-2026-00118): el destino de `completed`/`waiting` se decide con
        # el ticket fresco, no con el estado encolado. Si QA aprobo o la espera
        # se resolvio entre el encolado y el push, el ticket no va a revision.
        task = None
        if str(item.get("status") or "").strip().lower() in {
            TaskStatus.COMPLETED.value,
            TaskStatus.WAITING.value,
        }:
            try:
                repo = getattr(self, "_repo", None)
                if repo is not None:
                    task = await repo.get_task(str(item.get("task_id") or ""))
            except Exception:
                logger.warning("No se pudo leer el ticket para QA condicional", exc_info=True)
                task = None

        delay = 1.0
        for attempt in range(3):
            try:
                await adapter.update_status(item["name"], item["status"], task=task)
            except ERPNextUnavailable as exc:
                if attempt == 2:
                    logger.warning(
                        "ERPNext no respondio al empujar %s -> %s: %s", item["name"], item["status"], exc
                    )
                    await self._record_status_outcome(item, pushed=False)
                    return
                await asyncio.sleep(delay)
                delay *= 2
            except ERPNextDependencyBlockedError as exc:
                # Orden topologico (TASK-2026-00013): ERPNext no deja completar con
                # hijos o dependencias abiertos. No es un error de datos: queda como
                # `pending_status` y la reconciliacion lo reintenta cuando cierren.
                # Sin reintento inmediato: lo que desbloquea es otro cierre, no el tiempo.
                logger.warning(
                    "ERPNext difiere %s -> %s por dependientes abiertos: %s",
                    item["name"],
                    item["status"],
                    exc,
                )
                await self._record_status_outcome(item, pushed=False)
                return
            except ERPNextError:
                logger.exception(
                    "ERPNext rechazo la transicion %s -> %s", item["name"], item["status"]
                )
                return
            else:
                await self._record_status_outcome(item, pushed=True)
                return

    async def _record_status_outcome(self, item: dict[str, Any], *, pushed: bool) -> None:
        """Deja constancia del resultado en `params._mirrors.erpnext`.

        Un fallo no se pierde en silencio: queda como `pending_status` para que la
        reconciliacion de arranque lo reintente. Es el fallo que el espejo de Notion si
        tiene (marca el exito despues del HTTP y en una escritura aparte).
        """
        task_id = str(item.get("task_id") or "")
        if not task_id:
            return
        task = await self._repo.get_task(task_id)
        if not task:
            return
        params = self._task_params(task)
        if pushed:
            mirror = {
                "status_pushed": item["status"],
                "pending_status": None,
                "status_pushed_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            mirror = {"pending_status": item["status"]}
        # Escribir solo `params` no dispara al observador (no hay cambio de `status`).
        await self._repo.patch_task(
            task_id, {"params": self._merge_erpnext_mirror_params(params, mirror)}
        )

    async def create_task(self, payload: TaskCreateRequest) -> dict[str, Any]:
        # Idempotency: if a task with the same source+source_id exists, return it
        if payload.source_id:
            existing = await self._repo.find_task_by_source(payload.source.value, payload.source_id)
            if existing is not None:
                logger.info("Idempotent hit: task %s already exists for source=%s source_id=%s",
                            existing["task_id"], payload.source.value, payload.source_id)
                self._enqueue_notion_mirror(existing)
                # El ticket puede existir en Mongo con el enlace de negocio pendiente:
                # devolverlo sin repararlo lo dejaria invisible en ERPNext para siempre.
                await self._enqueue_erpnext_registration(existing)
                return existing

        doc = make_task_document(payload)
        await self._apply_default_phase_workflow_to_document(doc)
        if doc.ticket_number is None:
            doc.ticket_number = await self._repo.get_next_ticket_number()

        # El ticket de negocio se crea ANTES de persistir la ejecucion. Si ERPNext rechaza
        # los datos, esto lanza y no queda nada en Mongo: un ticket de ejecucion sin
        # contraparte de negocio es el "competing business record" que el contrato prohibe.
        erpnext_mirror, erpnext_pending = await self._register_business_ticket(
            doc,
            parent_task=payload.parent_task,
            depends_on=payload.depends_on,
            is_group=payload.is_group,
        )
        if erpnext_mirror is not None:
            doc.params = self._merge_erpnext_mirror_params(doc.params, erpnext_mirror)

        created = await self._repo.create_task(doc.model_dump(mode="python"))
        await self._hub.broadcast("task_created", created)
        self._enqueue_notion_mirror(created)

        # ERPNext no respondio: el ticket queda registrado pero sin enlace de negocio.
        # No se arranca ejecucion sobre trabajo que todavia no existe en el sistema de
        # negocio; el reencolado repara el enlace cuando ERPNext vuelva.
        if erpnext_pending:
            logger.warning(
                "Ticket %s queda pendiente de enlace ERPNext; no se lanza ejecucion.",
                created["task_id"],
            )
            await self._repo.patch_task(
                created["task_id"],
                {
                    "status": TaskStatus.QUEUED.value,
                    "waiting_reason": ERPNEXT_PENDING_REASON,
                },
            )
            return created

        # The dispatcher (F3) owns admission: creation persists queued and
        # wakes it. No direct launch and no pre-gate here, so priority order,
        # per-project/per-objective limits and Tier S reserves apply to every
        # ticket through a single path.
        if self._settings.TASK_AUTORUN and payload.autorun:
            self._wake_dispatcher()

        return created

    def _task_params(self, task: dict[str, Any] | Any) -> dict[str, Any]:
        if isinstance(task, dict):
            params = task.get("params")
        else:
            params = getattr(task, "params", None)
        return dict(params) if isinstance(params, dict) else {}

    async def _project_workflow_templates(self, project: str | None) -> dict[str, dict[str, Any]]:
        if not project:
            return merge_workflow_templates(None)
        row = await self._repo.projects.find_one({"name": project})
        raw_templates = row.get("workflow_templates") if row else None
        return merge_workflow_templates(raw_templates if isinstance(raw_templates, dict) else None)

    def _task_ticket_type(self, task: dict[str, Any] | Any) -> str:
        params = self._task_params(task) if isinstance(task, dict) else dict(getattr(task, "params", None) or {})
        for key in ("ticket_type", "type", "workflow_type"):
            value = str(params.get(key) or "").strip()
            if value:
                return value
        pm_ticket = params.get("pm_ticket")
        if isinstance(pm_ticket, dict):
            value = str(pm_ticket.get("ticket_type") or pm_ticket.get("type") or "").strip()
            if value:
                return value
        return "default"

    async def _apply_default_phase_workflow_to_document(self, doc: Any) -> None:
        if doc.workflow_phases:
            doc.workflow_phases = normalize_phases(doc.workflow_phases)
            if doc.workflow_phases and not doc.phase:
                index = first_executable_phase_index(doc.workflow_phases)
                doc.phase_index = index
                doc.phase = phase_key(doc.workflow_phases[index])
                doc.phase_started_at = datetime.now(timezone.utc)
            return

        params = self._task_params(doc)
        explicit_yaml_target = bool(
            params.get("workflow")
            or params.get("workflow_id")
            or params.get("workflow_ref")
            or params.get("use_workflow")
            or params.get("discover_workflow")
            or params.get("workflow_discovery")
        )
        if explicit_yaml_target and not doc.workflow_key:
            return

        agent = str((doc.agent_hint or doc.agent or AgentName.CEO).value if hasattr(doc.agent_hint or doc.agent, "value") else (doc.agent_hint or doc.agent or AgentName.CEO.value))
        templates = await self._project_workflow_templates(doc.project)
        requested_key = doc.workflow_key
        key = resolve_template_key(
            templates,
            requested_key=requested_key,
            agent=agent,
            ticket_type=self._task_ticket_type(doc),
        )
        if not key:
            return

        template = templates[key]
        phases = normalize_phases(template.get("phases") or [])
        if not phases:
            return
        if not requested_key:
            phases = self._drop_default_human_qa(phases, doc.project)
            if not phases:
                return

        index = first_executable_phase_index(phases)
        doc.workflow_key = key
        doc.workflow_phases = phases
        doc.phase_index = index
        doc.phase = phase_key(phases[index])
        doc.phase_assignments = dict(doc.phase_assignments or {})
        doc.phase_started_at = datetime.now(timezone.utc)

    def _drop_default_human_qa(
        self, phases: list[dict[str, Any]], project: str | None
    ) -> list[dict[str, Any]]:
        """Default templates skip human QA unless project policy demands it.

        Autonomy F4 (TASK-2026-00118): the `dev.default` template parked every
        dev ticket in a human `qa_review` phase, which pushed `Pending Review`
        with no nameable human action. The escalation policy (TASK-2026-00114)
        keeps human QA only when the project mandates it
        (``dev.qa.require_human_review``); otherwise the technical gate
        (green tests + no QA required, TASK-2026-00040) is the acceptance.
        Only this default path filters, and only HUMAN `qa_review` phases:
        explicit phases (creation payload, requested template key,
        `apply_phase_workflow_to_task`) and other human phases
        (`client_signoff`) are untouched. Without a config loader, or when
        the project is unreadable, the policy is unknown and the phases
        stay (safe default).
        """
        loader = getattr(self, "_config_loader", None)
        if loader is None:
            return phases
        try:
            project_config = loader.load_project(project)
        except Exception:
            logger.warning(
                "Default phase workflow kept human QA: project %r unreadable",
                project,
            )
            return phases
        if project_requires_human_review(project_config):
            return phases
        kept = [
            phase
            for phase in phases
            if not (
                str(phase.get("type") or "") == WorkflowPhaseType.HUMAN.value
                and str(phase.get("key") or "") == "qa_review"
            )
        ]
        if not any(
            str(phase.get("type") or "") != WorkflowPhaseType.TERMINAL.value
            for phase in kept
        ):
            return phases
        return kept

    async def apply_phase_workflow_to_task(
        self,
        task_id: str,
        *,
        workflow_key: str | None = None,
        workflow_phases: list[dict[str, Any]] | None = None,
        phase_assignments: dict[str, list[str]] | None = None,
        reset_phase: bool = True,
        autorun: bool | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any] | None:
        task = await self._repo.get_task(task_id)
        if task is None:
            return None

        phases = normalize_phases(workflow_phases or [])
        resolved_key = workflow_key
        if not phases:
            templates = await self._project_workflow_templates(task.get("project"))
            resolved_key = resolve_template_key(
                templates,
                requested_key=workflow_key,
                agent=str(task.get("agent_hint") or task.get("agent") or AgentName.CEO.value),
                ticket_type=self._task_ticket_type(task),
            )
            if not resolved_key:
                raise ValueError("No workflow template matched this task")
            phases = normalize_phases(templates[resolved_key].get("phases") or [])

        if not phases:
            raise ValueError("Workflow must define at least one phase")

        merged_assignments = dict(task.get("phase_assignments") or {})
        for phase, user_ids in (phase_assignments or {}).items():
            clean_ids = [str(user_id).strip() for user_id in user_ids if str(user_id).strip()]
            if clean_ids:
                merged_assignments[str(phase)] = sorted(set(clean_ids))

        involved = set(str(uid) for uid in (task.get("involved_user_ids") or []) if uid)
        for user_ids in merged_assignments.values():
            involved.update(str(uid) for uid in user_ids if uid)

        updates: dict[str, Any] = {
            "workflow_key": resolved_key,
            "workflow_phases": phases,
            "phase_assignments": merged_assignments,
            "involved_user_ids": sorted(involved),
            "updated_by": actor_id,
        }
        if autorun is not None:
            updates["autorun"] = bool(autorun)

        if reset_phase or not task.get("phase"):
            index = first_executable_phase_index(phases)
            phase = phases[index]
            updates.update(self._phase_entry_updates(task, phases, index, phase, actor_id=actor_id))

        patched = await self.patch_task(task_id, updates)
        if patched is not None and updates.get("status") == TaskStatus.QUEUED.value:
            # A reset landing on an agent phase re-queues: dispatch event.
            self._wake_dispatcher()
        return patched

    async def bulk_apply_phase_workflow(
        self,
        task_ids: list[str],
        *,
        workflow_key: str | None,
        workflow_phases: list[dict[str, Any]] | None,
        phase_assignments: dict[str, list[str]],
        reset_phase: bool,
        autorun: bool | None,
        actor_id: str,
    ) -> dict[str, Any]:
        applied = 0
        failed = 0
        errors: list[dict[str, str]] = []
        for task_id in task_ids:
            try:
                updated = await self.apply_phase_workflow_to_task(
                    task_id,
                    workflow_key=workflow_key,
                    workflow_phases=workflow_phases,
                    phase_assignments=phase_assignments,
                    reset_phase=reset_phase,
                    autorun=autorun,
                    actor_id=actor_id,
                )
                if updated is None:
                    failed += 1
                    errors.append({"task_id": task_id, "error": "Task not found"})
                else:
                    applied += 1
            except Exception as exc:
                failed += 1
                errors.append({"task_id": task_id, "error": str(exc)})
        return {"applied": applied, "failed": failed, "errors": errors}

    def _current_phase(self, task: dict[str, Any]) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
        phases = normalize_phases(task.get("workflow_phases") or [])
        if not phases:
            return [], 0, None
        index = int(task.get("phase_index") or 0)
        if task.get("phase"):
            index = find_phase_index(phases, str(task.get("phase")))
        index = max(0, min(index, len(phases) - 1))
        return phases, index, phases[index]

    def _phase_entry_updates(
        self,
        task: dict[str, Any],
        phases: list[dict[str, Any]],
        index: int,
        phase: dict[str, Any],
        *,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        key = phase_key(phase)
        phase_type = str(phase.get("type") or WorkflowPhaseType.HUMAN.value)
        updates: dict[str, Any] = {
            "phase": key,
            "phase_index": index,
            "phase_started_at": datetime.now(timezone.utc),
            "waiting_reason": None,
            "resume_at": None,
            "updated_by": actor_id,
        }

        if phase.get("client_visible") is True:
            updates["client_visible"] = True

        if phase_type == WorkflowPhaseType.HUMAN.value:
            updates.update(
                {
                    "status": TaskStatus.WAITING.value,
                    "waiting_reason": f"phase:{key}",
                    "approval_required": True,
                    "approval_state": ApprovalState.PENDING.value,
                }
            )
            return updates

        if phase_type == WorkflowPhaseType.TERMINAL.value:
            terminal_status = str(phase.get("status") or TaskStatus.COMPLETED.value)
            updates.update(
                {
                    "status": terminal_status,
                    "approval_required": False,
                    "approval_state": ApprovalState.APPROVED.value,
                    "completed_at": datetime.now(timezone.utc) if terminal_status == TaskStatus.COMPLETED.value else task.get("completed_at"),
                }
            )
            return updates

        agent = str(phase.get("agent") or AgentName.CEO.value)
        updates.update(
            {
                "agent": agent,
                "agent_hint": agent,
                "status": TaskStatus.QUEUED.value,
                "approval_required": False,
                "approval_state": ApprovalState.NOT_REQUIRED.value,
                "completed_at": None,
                "error": None,
            }
        )
        return updates

    def _phase_completion_updates(self, task: dict[str, Any]) -> dict[str, Any] | None:
        phases, index, current = self._current_phase(task)
        if not current:
            return None
        if str(current.get("type") or "") != WorkflowPhaseType.AGENT.value:
            return None
        next_index = index + 1
        if next_index >= len(phases):
            return None
        return self._phase_entry_updates(
            task,
            phases,
            next_index,
            phases[next_index],
            actor_id="system",
        )

    def _task_allows_autorun(self, task: dict[str, Any]) -> bool:
        autorun = task.get("autorun")
        if autorun is None:
            return True
        return bool(autorun)

    def _task_needs_notion_mirror(self, task: dict[str, Any]) -> bool:
        if self._notion is None or not self._settings.NOTION_MIRROR_ENABLED:
            return False
        if task.get("source") == "notion":
            return False
        # El espacio personal de Horizon nunca se proyecta a un espejo externo.
        # El safety net de arranque tiene su propio filtro en
        # ``MongoRepository.list_tasks_missing_notion_mirror``: ambos deben
        # excluir el alcance personal o los datos privados se publicarian.
        if is_personal_scope(task):
            return False
        if self._resolve_notion_page_id(task):
            return False
        already_done = task.get("post_actions_executed") or []
        return _NOTION_MIRROR_EVENT_KEY not in already_done

    def _task_notion_mirror(self, task: dict[str, Any]) -> dict[str, Any]:
        params = self._task_params(task)
        mirrors = params.get("_mirrors")
        if not isinstance(mirrors, dict):
            return {}
        notion = mirrors.get("notion")
        return dict(notion) if isinstance(notion, dict) else {}

    def _resolve_notion_page_id(self, task: dict[str, Any]) -> str | None:
        mirror = self._task_notion_mirror(task)
        page_id = str(mirror.get("page_id") or "").strip()
        if page_id:
            return page_id
        if task.get("source") == "notion":
            source_id = str(task.get("source_id") or "").strip()
            if source_id:
                return source_id
        return None

    def _merge_notion_mirror_params(
        self,
        task: dict[str, Any],
        *,
        page_id: str,
        url: str | None,
    ) -> dict[str, Any]:
        params = self._task_params(task)
        mirrors = params.get("_mirrors")
        if not isinstance(mirrors, dict):
            mirrors = {}
        notion = mirrors.get("notion")
        if not isinstance(notion, dict):
            notion = {}
        notion.update(
            {
                "page_id": page_id,
                "url": url,
                "mirrored_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        mirrors["notion"] = notion
        params["_mirrors"] = mirrors
        return params

    def _build_notion_mirror_payload(self, task: dict[str, Any]) -> dict[str, Any]:
        params = self._task_params(task)
        pm_ticket = params.get("pm_ticket")
        pm_ticket = pm_ticket if isinstance(pm_ticket, dict) else {}
        title = str(task.get("title") or "").strip()
        description = str(
            params.get("notion_description")
            or task.get("description")
            or ""
        ).strip()
        source_id = str(task.get("task_id") or "").strip()
        external_url = str(
            pm_ticket.get("external_url")
            or params.get("external_url")
            or params.get("client_review_url")
            or ""
        ).strip() or None
        due_date = (
            pm_ticket.get("due_date")
            if pm_ticket.get("due_date")
            else params.get("due_date")
        )
        tags = pm_ticket.get("tags") if pm_ticket.get("tags") else params.get("tags")
        ticket_type = (
            pm_ticket.get("ticket_type")
            if pm_ticket.get("ticket_type")
            else params.get("ticket_type")
        )
        return {
            "title": title,
            "description": description,
            "project": task.get("project"),
            "priority": task.get("priority"),
            "ticket_type": ticket_type,
            "tags": tags,
            "due_date": due_date,
            "external_url": external_url,
            "source_id": source_id,
        }

    async def ensure_notion_mirror(self, task_id: str) -> dict[str, Any] | None:
        task = await self._repo.get_task(task_id)
        if task is None:
            return None
        return await self._ensure_notion_mirror(task)

    async def startup_reconcile_notion_mirrors(self) -> int:
        """One-time safety net at startup for orphaned tasks without mirrors."""
        if self._notion is None or not self._settings.NOTION_MIRROR_ENABLED:
            return 0
        pending = await self._repo.list_tasks_missing_notion_mirror(limit=500)
        if not pending:
            return 0
        logger.info("Startup reconcile: %d tasks missing Notion mirror", len(pending))
        mirrored = 0
        for task in pending:
            updated = await self._ensure_notion_mirror(task)
            if self._resolve_notion_page_id(updated):
                mirrored += 1
        logger.info("Startup reconcile: mirrored %d/%d", mirrored, len(pending))
        return mirrored

    # --- ERPNext: camino inverso (decisiones de negocio hacia el runtime) ---
    #
    # El contrato exige que la actividad humana en ERPNext llegue a OpenCEO por eventos
    # explicitos (ARCHITECTURE_REFERENCE_2026.md §Read and Write Boundaries). Lo hace un
    # webhook de Frappe, no un sondeo: AGENTS.md §5.1 prohibe los barridos periodicos.

    async def find_task_for_business_ref(
        self, *, task_id: str | None = None, ref: str | None = None
    ) -> dict[str, Any] | None:
        """Resuelve la tarea de Mongo a partir de una referencia de ERPNext.

        Se prefiere `openceo_task_id` (identidad directa); el `ref` es el respaldo, y se
        descompone con `partition` porque `TaskSource` es un enum cerrado sin `:`.
        """
        if task_id:
            task = await self._repo.get_task(str(task_id).strip())
            if task:
                return task
        key = str(ref or "").strip()
        if not key:
            return None
        source, _, source_id = key.partition(":")
        if source and source_id:
            return await self._repo.find_task_by_source(source, source_id)
        return None

    async def apply_business_status(
        self,
        *,
        task_id: str | None = None,
        ref: str | None = None,
        business_status: str | None = None,
        deleted: bool = False,
    ) -> dict[str, Any]:
        """Aplica al runtime una decision tomada por un humano en ERPNext.

        Solo se propagan las decisiones de NEGOCIO (`Completed`, `Cancelled` y el borrado).
        Los demas estados de `Task` son eco de lo que OpenCEO ya empujo, y propagarlos
        seria un bucle.

        El guardia anti-eco es estructural, no casual: el parche escribe `status_pushed`
        con el mismo valor que el estado nuevo, asi que aunque el observador vea el
        cambio, no lo reenvia a ERPNext.
        """
        adapter = getattr(self, "_erpnext", None)
        if adapter is None:
            return {"ok": False, "status": "no_adapter"}

        task = await self.find_task_for_business_ref(task_id=task_id, ref=ref)
        if task is None:
            return {"ok": False, "status": "not_found"}

        target = _ERPNEXT_BUSINESS_TO_TASK_STATUS.get(str(business_status or "").strip())
        if deleted:
            target = TaskStatus.CANCELLED.value
        if target is None:
            return {"ok": True, "status": "ignored", "task_id": task.get("task_id")}
        if str(task.get("status") or "") == target:
            return {"ok": True, "status": "already_applied", "task_id": task.get("task_id")}

        params = self._task_params(task)
        mirror = {
            "business_status": "Deleted" if deleted else str(business_status or ""),
            "business_synced_at": datetime.now(timezone.utc).isoformat(),
            # Guardia anti-eco: el observador compara contra este campo.
            "status_pushed": target,
        }
        updates: dict[str, Any] = {
            "status": target,
            "params": self._merge_erpnext_mirror_params(params, mirror),
        }
        if target == TaskStatus.CANCELLED.value:
            # Una cancelacion del negocio no se reanuda nunca: se limpia el despertador.
            updates["resume_at"] = None
            updates["waiting_reason"] = None
            updates["error"] = (
                "El ticket fue eliminado en ERPNext."
                if deleted
                else "El ticket fue cancelado en ERPNext."
            )

        updated = await self.patch_task(task["task_id"], updates)
        logger.info(
            "Decision de negocio aplicada al ticket %s: %s",
            task.get("task_id"),
            "eliminado" if deleted else business_status,
        )
        # A business close/cancel frees a slot: dispatch event.
        self._wake_dispatcher()
        result: dict[str, Any] = {
            "ok": True,
            "status": "applied",
            "task_id": task.get("task_id"),
            "task_status": target,
        }
        # Inverse webhook on an objective parent (TASK-2026-00117): the group
        # decision reaches the objective. Children closing in ERP take the
        # plain ticket path above; only a converted parent propagates.
        if str(task.get("status") or "") == TaskStatus.CONVERTED.value \
                and task.get("objective_id"):
            service = getattr(self, "_objective_service", None)
            if service is not None:
                try:
                    result["objective"] = await service.propagate_group_decision(
                        task,
                        target=target,
                        business_status=("Deleted" if deleted
                                         else str(business_status or "")),
                    )
                except Exception as exc:
                    logger.warning(
                        "Objective propagation failed for %s",
                        task.get("task_id"), exc_info=True)
                    result["objective"] = {"ok": False, "reason": str(exc)[:200]}
        return result

    # --- Transicion explicita de estado (TASK-2026-00132) --------------------
    #
    # `openceo_transition_ticket` (MCP) es la via por la que un agente mueve el
    # estado de NEGOCIO con el criterio global: antes solo existia por dentro
    # (`update_status`/`_push_status`) y las cancelaciones y devoluciones se
    # hacian a mano en el desk. El criterio vive aqui, en el servidor, no en el
    # prompt del llamador.

    def _company_mandate(self) -> Any | None:
        """Mandato de empresa validado, o None si falta o es ilegible.

        None no bloquea la transicion: `requires_owner` escala todo sin
        mandato valido (fail-closed hacia preguntar, no hacia actuar).
        """
        loader = getattr(self, "_config_loader", None)
        if loader is None:
            return None
        try:
            return loader.load_company_mandate()
        except Exception:
            logger.warning(
                "Company mandate unreadable; transition escalates fail-closed",
                exc_info=True,
            )
            return None

    async def _record_transition_mirror(
        self,
        task: dict[str, Any],
        *,
        name: str,
        target: str,
        equivalent: str,
        reason: str,
        human_action: str = "",
        evidence: str = "",
        owner_instruction: str = "",
        previous: str = "",
        secret_block: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Parche unico: estado de ejecucion + espejo coherente + auditoria.

        El guardia anti-eco es estructural: el parche escribe `status_pushed`
        con el mismo valor que el estado nuevo, asi que el observador no lo
        reenvia, y el webhook inverso vera `already_applied`/`ignored`.

        El marcador de secreto ausente (TASK-2026-00142) viaja en el mismo
        parche: se escribe cuando la transicion lo trae y se limpia en
        cualquier otra transicion (al salir de Working el bloqueo se levanta
        solo, sin lifecycle manual).
        """
        task_id = str(task.get("task_id") or "")
        if not task_id:
            return None
        now = datetime.now(timezone.utc).isoformat()
        audit: dict[str, Any] = {
            "at": now,
            "from": previous,
            "to": target,
            "reason": _transition_audit_text(reason),
        }
        if human_action:
            audit["human_action"] = _transition_audit_text(human_action)
        if evidence:
            audit["evidence"] = _transition_audit_text(evidence)
        if owner_instruction:
            audit["owner_instruction"] = _transition_audit_text(owner_instruction)
        mirror = {
            "status_pushed": equivalent,
            "pending_status": None,
            "business_status": target,
            "business_synced_at": now,
            "transition": audit,
        }
        merged_params = self._merge_erpnext_mirror_params(
            self._task_params(task), mirror
        )
        if secret_block is not None:
            merged_params[SECRET_BLOCK_PARAM] = secret_block
        else:
            # None borra en la fusion de patch_task: pop aqui no limpiaria
            # nada porque la fusion conserva las claves ausentes.
            merged_params[SECRET_BLOCK_PARAM] = None
        updates: dict[str, Any] = {
            "status": equivalent,
            "params": merged_params,
        }
        if target == "Cancelled":
            # Terminal como la del camino inverso: sin despertador y con motivo.
            updates["resume_at"] = None
            updates["waiting_reason"] = None
            updates["error"] = (
                f"Cancelado por transicion de {name}: "
                f"{_transition_audit_text(reason, 300)}"
            )
        elif target == "Pending Review":
            # La accion humana nombrable viaja como aprobacion pendiente: es la
            # senal que la bandeja Requiere humano y Horizon ya consumen.
            updates["approval_required"] = True
            updates["approval_state"] = ApprovalState.PENDING.value
            updates["waiting_reason"] = _transition_audit_text(human_action)
        return await self.patch_task(task_id, updates)

    async def _escalate_transition_cancel(
        self,
        *,
        name: str,
        business: dict[str, Any],
        task: dict[str, Any] | None,
        reason: str,
        why: str,
    ) -> dict[str, Any]:
        """Crea el escalado en vez de cancelar (falta la orden del dueno).

        Idempotente por `source_id` determinista: un segundo intento devuelve
        el escalado existente SIN re-aparcarlo (un escalado ya respondido no se
        reabre) y SIN cancelar. La aprobacion no cancela sola: el llamador
        reintenta citando la orden en `owner_instruction`.
        """
        current = str(business.get("status") or "").strip()
        subject = str(business.get("subject") or name).strip()
        source_id = f"transition-cancel:{name}"
        repo = getattr(self, "_repo", None)
        if repo is not None:
            try:
                existing = await repo.find_task_by_source(TaskSource.API.value, source_id)
            except Exception:
                logger.warning("No se pudo buscar el escalado %s", source_id, exc_info=True)
                existing = None
            if existing is not None:
                return {
                    "ok": True,
                    "outcome": "escalated",
                    "duplicate": True,
                    "ticket": name,
                    "business_status": current,
                    "reason": f"Cancelar {name} exige la orden del dueno ({why}); "
                    "ya existe un escalado y sigue sin cancelarse.",
                    "escalation": {
                        "task_id": existing.get("task_id"),
                        "ticket": self._escalation_business_name(existing),
                        "status": existing.get("status"),
                    },
                }
        action = OwnerAction(
            kind="ticket_cancel",
            summary=f"Cancelar {name}: {subject}",
            project=str((task or {}).get("project") or "") or None,
            irreversible=True,
            strategic_replan=bool(business.get("is_group")),
            human_action_hint=(
                f"Autoriza la cancelacion de {name} ({subject}): {reason}"
            ),
            details={"ticket": name, "reason": reason, "cancel_rule": why},
        )
        try:
            created = await self.create_owner_escalation(
                action=action,
                decision=requires_owner(action, self._company_mandate()),
                source_id=source_id,
            )
        except ERPNextError as exc:
            return _classify_transition_error(exc)
        return {
            "ok": True,
            "outcome": "escalated",
            "duplicate": False,
            "ticket": name,
            "business_status": current,
            "reason": f"Cancelar {name} exige la orden del dueno ({why}); "
            "se creo el escalado y no se cancelo nada.",
            "escalation": {
                "task_id": created.get("task_id"),
                "ticket": self._escalation_business_name(created),
                "status": created.get("status"),
            },
        }

    @staticmethod
    def _escalation_business_name(task: dict[str, Any] | None) -> str | None:
        params = (task or {}).get("params") or {}
        mirrors = params.get("_mirrors") or {}
        business = mirrors.get("erpnext") or {}
        name = str(business.get("name") or "").strip()
        return name or None

    async def transition_ticket(
        self,
        ticket: str,
        to_status: str,
        reason: str,
        *,
        human_action: str | None = None,
        evidence: str | None = None,
        owner_instruction: str | None = None,
        secret_block: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mueve el estado de NEGOCIO con el criterio global (TASK-2026-00132).

        `to_status` nombra el estado ERPNext exacto (Open, Working,
        Pending Review, Completed, Cancelled); `reason` es obligatoria siempre.
        Pending Review exige `human_action` nombrable y nunca es el defecto;
        Completed exige `evidence` de verificacion y respeta el gate de QA;
        cancelar trabajo del dueno exige citar su orden en `owner_instruction`
        (sin ella se crea el escalado y no se cancela nada).
        `secret_block` (TASK-2026-00142) solo aparca en Working: escribe el
        marcador de secreto ausente que el cockpit lee como bloqueo.

        Devuelve `outcome` (`applied` | `already_applied` | `escalated`) con el
        estado de negocio RELEIDO, o `ok: False` con `status` clasificado. El
        espejo Mongo queda coherente en el mismo parche (anti-eco
        estructural); sin registro de ejecucion (ticket de desk) solo se mueve
        el negocio. No disponible: sin cambios, error `unavailable` (registrar
        `pending_status` sin cambiar la ejecucion falsearia la reconciliacion).
        """
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not getattr(adapter, "is_enabled", False):
            return {
                "ok": False,
                "status": "not_configured",
                "message": "ERPNext no esta configurado.",
            }
        try:
            target = normalize_transition_status(to_status)
        except ERPNextValidationError as exc:
            return {"ok": False, "status": "invalid_input", "message": str(exc)}
        clean_marker: dict[str, Any] | None = None
        if secret_block is not None:
            if target != SECRET_BLOCK_BUSINESS_STATUS:
                return {
                    "ok": False,
                    "status": "invalid_input",
                    "message": "secret_block solo aparca en Working: un secreto "
                    "ausente bloquea el trabajo, no lo cierra ni lo escala.",
                }
            if not isinstance(secret_block, dict) or not str(
                secret_block.get("secret") or ""
            ).strip():
                return {
                    "ok": False,
                    "status": "invalid_input",
                    "message": "secret_block necesita el nombre exacto del secreto.",
                }
            clean_marker = dict(secret_block)
        clean_reason = _transition_audit_text(reason)
        if not clean_reason:
            return {
                "ok": False,
                "status": "invalid_input",
                "message": "reason es obligatoria: di por que cambia el estado.",
            }
        try:
            business = await adapter.resolve_task(ticket)
        except ERPNextError as exc:
            return _classify_transition_error(exc)

        name = str(business.get("name") or "").strip()
        current = str(business.get("status") or "").strip()
        repo = getattr(self, "_repo", None)
        task: dict[str, Any] | None = None
        if repo is not None:
            try:
                task = await self.find_task_for_business_ref(
                    task_id=str(business.get("openceo_task_id") or "").strip() or None,
                    ref=str(business.get("openceo_ref") or "").strip() or None,
                )
            except Exception:
                logger.warning(
                    "No se pudo resolver la ejecucion de %s", name, exc_info=True
                )
                task = None

        equivalent = _ERPNEXT_TO_TASK_STATUS[target]
        if current == target:
            # Idempotente: relee y devuelve. Si la ejecucion (no terminal)
            # discrepa del negocio, la alinea: es la reparacion del webhook
            # perdido, no una resurreccion (un terminal no se toca). Un no-op
            # conserva el marcador de secreto ausente; un re-reporte con otro
            # marcador lo actualiza.
            healed = False
            exec_status = str((task or {}).get("status") or "")
            task_params = self._task_params(task or {})
            mirror = task_params.get("_mirrors", {})
            pushed = mirror.get("erpnext", {}) if isinstance(mirror, dict) else {}
            existing_marker = task_params.get(SECRET_BLOCK_PARAM)
            if (
                task is not None
                and exec_status not in _TERMINAL_TASK_STATUSES
                and (
                    exec_status != equivalent
                    or str(pushed.get("status_pushed") or "") != equivalent
                    or (
                        clean_marker is not None
                        and existing_marker != clean_marker
                    )
                )
            ):
                if clean_marker is not None:
                    healed_marker: dict[str, Any] | None = clean_marker
                elif isinstance(existing_marker, dict):
                    healed_marker = existing_marker
                else:
                    healed_marker = None
                updated = await self._record_transition_mirror(
                    task,
                    name=name,
                    target=target,
                    equivalent=equivalent,
                    reason=clean_reason,
                    human_action=_transition_audit_text(human_action),
                    evidence=_transition_audit_text(evidence),
                    owner_instruction=_transition_audit_text(owner_instruction),
                    previous=current,
                    secret_block=healed_marker,
                )
                healed = updated is not None
                if healed:
                    exec_status = equivalent
            return {
                "ok": True,
                "outcome": "already_applied",
                "ticket": name,
                "business_status": current,
                "task_status": exec_status or None,
                "mirror_healed": healed,
            }

        if current == "Cancelled":
            return {
                "ok": False,
                "status": "terminal",
                "ticket": name,
                "business_status": current,
                "message": f"{name} esta Cancelled: es terminal, crea un ticket nuevo.",
            }
        exec_status = str((task or {}).get("status") or "")
        if exec_status == TaskStatus.CANCELLED.value:
            return {
                "ok": False,
                "status": "terminal",
                "ticket": name,
                "business_status": current,
                "message": f"La ejecucion de {name} esta cancelled: es terminal, "
                "crea un ticket nuevo.",
            }
        if exec_status == TaskStatus.CONVERTED.value:
            return {
                "ok": False,
                "status": "converted",
                "ticket": name,
                "business_status": current,
                "message": f"{name} es el padre convertido de un objetivo: cierra por "
                "el objetivo (§9.7), no por transicion directa.",
            }

        clean_human_action = _transition_audit_text(human_action)
        clean_evidence = _transition_audit_text(evidence)
        clean_instruction = _transition_audit_text(owner_instruction)
        if target == "Pending Review" and not clean_human_action:
            return {
                "ok": False,
                "status": "invalid_input",
                "ticket": name,
                "business_status": current,
                "message": "Pending Review exige human_action: nombra la accion "
                "humana exacta. Nunca es el estado por defecto.",
            }
        if target == "Completed":
            if not clean_evidence:
                return {
                    "ok": False,
                    "status": "invalid_input",
                    "ticket": name,
                    "business_status": current,
                    "message": "Completed exige evidence: que verificacion acredita "
                    "el cierre.",
                }
            if task is not None and task_needs_human_review(task):
                return {
                    "ok": False,
                    "status": "qa_required",
                    "ticket": name,
                    "business_status": current,
                    "message": f"{name} tiene QA requerida sin aprobar: pasa a "
                    "Pending Review con human_action o aprueba la QA; la "
                    "evidencia no salta el gate.",
                }
        if target == "Cancelled":
            needs_owner, why = _transition_cancel_needs_owner(business, task)
            if needs_owner and not clean_instruction:
                return await self._escalate_transition_cancel(
                    name=name,
                    business=business,
                    task=task,
                    reason=clean_reason,
                    why=why,
                )

        delay = 1.0
        for attempt in range(3):
            try:
                await adapter.transition_status(name, target)
                break
            except ERPNextUnavailable as exc:
                if attempt == 2:
                    logger.warning(
                        "ERPNext no respondio a la transicion %s -> %s: %s",
                        name,
                        target,
                        exc,
                    )
                    return {
                        "ok": False,
                        "status": "unavailable",
                        "ticket": name,
                        "business_status": current,
                        "message": f"ERPNext no respondio; {name} sigue en {current}.",
                    }
                await asyncio.sleep(delay)
                delay *= 2
            except ERPNextDependencyBlockedError as exc:
                blocked = sorted(
                    {hit for hit in _TASK_NAME_PATTERN.findall(str(exc)) if hit != name}
                )
                result: dict[str, Any] = {
                    "ok": False,
                    "status": "blocked",
                    "ticket": name,
                    "business_status": current,
                    "to_status": target,
                    "message": f"{name} no pasa a {target}: {exc}",
                    "blocked_by": blocked,
                }
                known = depends_on_names(business)
                if known is not None:
                    result["depends_on"] = known
                return result
            except ERPNextError as exc:
                return _classify_transition_error(exc)

        try:
            reread = await adapter.resolve_task(name)
        except ERPNextError as exc:
            return {
                "ok": False,
                "status": "unverified",
                "ticket": name,
                "to_status": target,
                "message": f"El PUT de {name} -> {target} llego pero no se pudo "
                f"releer: {exc}",
            }
        actual = str(reread.get("status") or "").strip()
        if actual != target:
            return {
                "ok": False,
                "status": "mismatch",
                "ticket": name,
                "business_status": actual,
                "to_status": target,
                "message": f"{name} quedo en {actual}, no en {target}; "
                "la ejecucion no se toco.",
            }

        task_status: str | None = None
        mirror_state = "no_execution_record"
        if task is not None:
            updated = await self._record_transition_mirror(
                task,
                name=name,
                target=target,
                equivalent=equivalent,
                reason=clean_reason,
                human_action=clean_human_action,
                evidence=clean_evidence,
                owner_instruction=clean_instruction,
                previous=current,
                secret_block=clean_marker,
            )
            if updated is not None:
                task_status = equivalent
                mirror_state = "aligned"
            else:
                task_status = str(task.get("status") or "") or None
                mirror_state = "execution_gone"
        return {
            "ok": True,
            "outcome": "applied",
            "ticket": name,
            "business_status": actual,
            "previous_status": current,
            "task_status": task_status,
            "mirror": mirror_state,
        }

    async def report_missing_secret(
        self,
        ticket: str,
        secret: str,
        sources_checked: Sequence[str] | None,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Aparca en Working por secreto ausente, con marcador para el cockpit.

        Regla del dueno 2026-10-07 (TASK-2026-00142): el llamador ya busco el
        secreto en todas las fuentes accesibles y no esta en ninguna. El
        negocio pasa a Working con el secreto exacto nombrado en el motivo
        (nunca Pending Review: no hay decision humana que tomar) y la
        ejecucion recibe el marcador `params.secret_block`, que el cockpit
        de Horizon lee como bloqueo. Sin registro de ejecucion (ticket de
        desk) solo se mueve el negocio. Una entrada invalida no escribe nada.
        """
        try:
            spec = build_secret_block(secret, sources_checked)
        except MandateError as exc:
            return {
                "ok": False,
                "status": "invalid_input",
                "ticket": str(ticket or "").strip() or None,
                "message": str(exc),
            }
        reason = str(spec["reason"])
        clean_note = _transition_audit_text(note)
        if clean_note:
            reason = f"{reason} Nota del agente: {clean_note}"
        result = await self.transition_ticket(
            ticket,
            str(spec["business_status"]),
            reason,
            secret_block=dict(spec["marker"]),
        )
        if not result.get("ok"):
            return result
        result["secret_block"] = {
            "secret": spec["marker"]["secret"],
            "sources_checked": spec["marker"]["sources_checked"],
        }
        return result

    async def startup_reconcile_erpnext(self) -> int:
        """Repara al arrancar los enlaces de negocio que quedaron a medias.

        Cinturon de seguridad del observador de transiciones: si el proceso murio entre el
        cambio de estado y el envio a ERPNext, o si ERPNext estaba caido, la brecha se
        repara aqui. Nunca replica un efecto ya exitoso, porque compara contra el estado
        empujado y no contra el estado anterior.

        Acotada y de una sola pasada, como la de Notion: no es un barrido periodico
        (AGENTS.md §5.1).
        """
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not adapter.is_enabled:
            return 0

        pending = await self._repo.list_tasks_needing_erpnext_sync(limit=200)
        if not pending:
            return 0
        logger.info("Startup reconcile ERPNext: %d tickets con enlace roto", len(pending))

        queue = getattr(self, "_erpnext_queue", None)
        repaired = 0
        for task in pending:
            params = self._task_params(task)
            mirrors = params.get("_mirrors")
            mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
            if not isinstance(mirror, dict):
                continue

            if not str(mirror.get("name") or "").strip():
                # El ticket de negocio nunca llego a crearse (o no se supo): reintentar.
                await self._enqueue_erpnext_registration(task)
                repaired += 1
                continue

            # El ticket existe pero una transicion no se pudo empujar.
            status = str(task.get("status") or "")
            pending_status = str(mirror.get("pending_status") or "").strip()
            if queue is not None and pending_status:
                item = {
                    "task_id": task.get("task_id"),
                    "name": mirror["name"],
                    "status": status,
                }
                if resolve_erpnext_status(task) is None:
                    # Ya no hay nada que empujar (p. ej. espera operativa sin
                    # accion humana): se limpia el pendiente sin HTTP.
                    await self._record_status_outcome(item, pushed=True)
                else:
                    queue.put_nowait(item)
                repaired += 1

        logger.info("Startup reconcile ERPNext: %d reparados de %d", repaired, len(pending))
        return repaired

    async def _ensure_notion_mirror(self, task: dict[str, Any]) -> dict[str, Any]:
        """Create a Notion mirror page for a task. Called reactively from CRUD operations only."""
        if not self._task_needs_notion_mirror(task):
            return task

        already_done = task.get("post_actions_executed") or []

        # Include runtime fields in the initial creation so Agent,
        # Runtime Status, etc. appear immediately in Notion (1 API call).
        status_label = str(task.get("status") or TaskStatus.QUEUED.value)
        mirror_payload = self._build_notion_mirror_payload(task)
        mirror_payload.update(
            runtime_status=status_label,
            agent=task.get("agent"),
            openceo_ref=task.get("task_id"),
            last_sync_at=datetime.now(timezone.utc).isoformat(),
            approval_required=task.get("approval_required") if bool(task.get("approval_required")) else None,
            approval_state=task.get("approval_state") if bool(task.get("approval_required")) else None,
        )

        try:
            result = await asyncio.to_thread(
                self._notion.create_ticket,
                **mirror_payload,
            )
        except Exception:
            logger.warning("Notion mirror creation error for task %s", task.get("task_id"), exc_info=True)
            return task

        if not result.get("ok"):
            logger.warning(
                "Notion mirror creation failed for task %s: %s",
                task.get("task_id"),
                result.get("message"),
            )
            return task

        sync_key = f"notion_sync_{status_label}"
        patched = await self.patch_task(
            str(task.get("task_id") or ""),
            {
                "params": self._merge_notion_mirror_params(
                    task,
                    page_id=str(result.get("page_id") or ""),
                    url=str(result.get("url") or "").strip() or None,
                ),
                "post_actions_executed": already_done + [_NOTION_MIRROR_EVENT_KEY, sync_key],
            },
        )
        if patched is not None:
            task = patched

        return task

    async def run_task(self, task_id: str, force: bool = False) -> dict[str, Any] | None:
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        if lock.locked() and not force:
            return await self._repo.get_task(task_id)

        async with lock:
            task = await self._repo.get_task(task_id)
            if task is None:
                return None

            if task.get("status") in (
                TaskStatus.COMPLETED.value, TaskStatus.CONVERTED.value,
            ) and not force:
                return task

            # Serialize execution per project so dev agents never race on the same repo
            project = task.get("project") or "default"
            project_lock = self._project_locks.setdefault(project, asyncio.Lock())
            async with project_lock:
                started_at = datetime.now(timezone.utc)
                patch_started = {
                    "status": TaskStatus.IN_PROGRESS.value,
                    "started_at": started_at,
                    "completed_at": None,
                    "steps": [],
                    "coding_session_id": None,
                    "coding_session_title": None,
                    "last_prompt_sent": None,
                    "last_response_summary": None,
                    "error": None,
                    "waiting_reason": None,
                    "resume_at": None,
                }
                task = await self._repo.patch_task(task_id, patch_started)
                if task is None:
                    return None
                await self._hub.broadcast("task_updated", task)
                asyncio.create_task(self._fire_notion_sync_back(task, "in_progress"))

                try:
                    # Tickets with manual approval phases (e.g. dev_work -> qa_review -> client_signoff)
                    # should execute directly via the agent graph, not via the YAML workflow service,
                    # unless the task explicitly targeted a YAML workflow or discovery.
                    params = self._task_params(task)
                    explicit_yaml_target = bool(
                        params.get("workflow")
                        or params.get("workflow_id")
                        or params.get("workflow_ref")
                        or params.get("use_workflow")
                        or params.get("discover_workflow")
                        or params.get("workflow_discovery")
                    )
                    has_approval_phases = bool(task.get("workflow_phases")) and not explicit_yaml_target
                    if has_approval_phases:
                        graph_out = await self._graph.execute(task, agent_hint=task.get("agent_hint"))
                    elif self._workflow_service is not None:
                        workflow_out = await self._workflow_service.run_for_task(task)
                        if workflow_out is not None:
                            graph_out = self._workflow_outcome_to_graph_out(workflow_out)
                        else:
                            graph_out = await self._graph.execute(task, agent_hint=task.get("agent_hint"))
                    else:
                        graph_out = await self._graph.execute(task, agent_hint=task.get("agent_hint"))
                except Exception as exc:
                    failed = await self._repo.patch_task(
                        task_id,
                        {
                            "status": TaskStatus.FAILED.value,
                            "error": str(exc),
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )
                    if failed is not None:
                        await self._hub.broadcast("task_updated", failed)
                        asyncio.create_task(self._fire_notion_sync_back(failed, "failed"))
                    # A freed slot is a dispatch event even on crash.
                    self._wake_dispatcher()
                    return failed

            final_status = graph_out.get("status", TaskStatus.FAILED.value)
            updates = {
                "agent": graph_out.get("selected_agent", task.get("agent", "ceo")),
                "status": final_status,
                "steps": graph_out.get("steps", task.get("steps", [])),
                "last_prompt_sent": graph_out.get("last_prompt_sent"),
                "last_response_summary": graph_out.get("last_response_summary"),
                "coding_session_id": graph_out.get("coding_session_id"),
                "coding_session_title": graph_out.get("coding_session_title"),
                "waiting_reason": graph_out.get("waiting_reason"),
                "resume_at": graph_out.get("resume_at"),
                "error": graph_out.get("error"),
            }
            # Preserve the ticket-level tool if the graph didn't return it
            # (e.g. error before _dev_node, or fallback path)
            graph_tool = graph_out.get("coding_tool")
            if graph_tool is not None:
                updates["coding_tool"] = graph_tool
            if graph_out.get("approval_required") is not None:
                updates["approval_required"] = graph_out["approval_required"]
            if graph_out.get("approval_state") is not None:
                updates["approval_state"] = graph_out["approval_state"]
            if graph_out.get("params") is not None:
                updates["params"] = graph_out["params"]
            if graph_out.get("waiting_reason") and str(graph_out["waiting_reason"]).startswith("phase:"):
                phase_name = str(graph_out["waiting_reason"]).split("phase:", 1)[1].strip()
                updates["phase"] = phase_name
                updates["workflow_phases"] = [
                    {
                        "key": phase_name,
                        "phase": phase_name,
                        "type": "human",
                        "name": phase_name.replace("_", " ").title(),
                        "permission": phase_name,
                    }
                ]
                updates["phase_index"] = 0
            # Persist modified files into params to keep task document stable
            modified_files = graph_out.get("modified_files")
            if modified_files:
                params = dict(task.get("params") or {})
                params["modified_files"] = modified_files
                updates["params"] = params

            if final_status == TaskStatus.COMPLETED.value:
                phase_updates = self._phase_completion_updates(task)
                if phase_updates:
                    updates.update(phase_updates)
                    final_status = str(phase_updates.get("status") or final_status)

            if final_status in {
                TaskStatus.COMPLETED.value,
                TaskStatus.CONVERTED.value,
                TaskStatus.FAILED.value,
                TaskStatus.ESCALATED.value,
            }:
                updates["completed_at"] = datetime.now(timezone.utc)

            updated = await self._repo.patch_task(task_id, updates)
            if updated is not None:
                await self._hub.broadcast("task_updated", updated)
                asyncio.create_task(self._fire_notion_sync_back(updated, final_status))
                if final_status in {
                    TaskStatus.COMPLETED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.ESCALATED.value,
                }:
                    asyncio.create_task(self._on_ticket_done(updated))
                    # ``run_task`` escribe con ``self._repo.patch_task``, no con
                    # el embudo del servicio, asi que el aviso a Horizon tiene
                    # que repetirse aqui o los tickets ejecutados de verdad
                    # serian justo los que nunca replanifican.
                    asyncio.create_task(self._notify_horizon_terminal(updated))
                    self._wake_ceo_incident(updated)
                if final_status == TaskStatus.WAITING.value:
                    reason = str(updated.get("waiting_reason") or "")
                    if reason.startswith(("routing_missing", "config")):
                        asyncio.create_task(self._comment_wait_on_erp(updated, reason))
                # Any finished run frees its slot: dispatch event.
                self._wake_dispatcher()
            return updated

    async def _comment_wait_on_erp(self, task: dict[str, Any], reason: str) -> None:
        """Post a one-shot ERP comment for waits that need the owner.

        Only routing and config waits qualify: both mean a human (or the
        tier authority) must fix something before the ticket can run.
        Transient waits (quota, repo_busy, rate limits) heal alone and stay
        silent. Never breaks execution.
        """
        try:
            adapter = getattr(self, "_erpnext", None)
            if adapter is None or not getattr(adapter, "is_enabled", False):
                return
            key = reason.split(":", 1)[0].strip()
            if task.get("wait_commented_for") == key:
                return
            mirrors = (task.get("params") or {}).get("_mirrors") or {}
            mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
            name = str(mirror.get("name") or "") if isinstance(mirror, dict) else ""
            if not name:
                return
            if key == "routing_missing":
                # Loose tickets self-classify at dispatch (TASK-2026-00137), so
                # this wait now means an objective child arrived without the
                # planner tier: a planner/validation gap, not an owner action.
                if task.get("objective_id"):
                    text = (
                        "OpenCEO no puede ejecutar este ticket: pertenece a un "
                        "objetivo pero llegó sin tier (coding_tool). La "
                        "clasificación automática solo cubre tickets sueltos; "
                        "hay que regenerar el hijo con tier o fijar coding_tool "
                        "(tier_s, tier_a, tier_b o tier_c) por API."
                    )
                else:
                    text = (
                        "OpenCEO no puede ejecutar este ticket: falta el campo coding_tool, "
                        "que se fija al crear la tarea (tier_s, tier_a, tier_b o tier_c; "
                        "tier_c es la opción por defecto). Asigna el tier para continuar."
                    )
            else:
                text = f"OpenCEO no puede ejecutar este ticket: configuración inválida ({reason})."
            await adapter.add_comment(name, text)
            await self._repo.patch_task(task["task_id"], {"wait_commented_for": key})
        except Exception:
            logger.warning("Could not comment wait on ERP for %s", task.get("task_id"), exc_info=True)

    async def comment_on_ticket(self, task_id: str, text: str) -> bool:
        """Post an ERP comment on a ticket's business mirror (Tier S, F3–F6).

        Used for close summaries, escalations and objective conversions.
        Returns False (never raises) when the adapter or the mirror is
        missing; the adapter itself dedupes identical content.
        """
        try:
            adapter = getattr(self, "_erpnext", None)
            if adapter is None or not getattr(adapter, "is_enabled", False):
                return False
            task = await self._repo.get_task(task_id)
            if task is None:
                return False
            mirrors = (task.get("params") or {}).get("_mirrors") or {}
            mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
            name = str(mirror.get("name") or "") if isinstance(mirror, dict) else ""
            if not name or not str(text or "").strip():
                return False
            await adapter.add_comment(name, str(text).strip())
            return True
        except Exception:
            logger.warning("Could not comment on ERP for %s", task_id, exc_info=True)
            return False

    def set_objective_service(self, service: Any | None) -> None:
        """Company-level objectives entry (TASK-2026-00117, wired in main)."""
        self._objective_service = service

    async def create_objective(
        self,
        *,
        project: str,
        goal: str,
        acceptance: list[str] | None = None,
        entry: str = "top_down",
        source: str = "api",
        source_id: str | None = None,
        requester: str | None = None,
    ) -> dict[str, Any]:
        """Top-down objective entry: the single surface for API/MCP/voice.

        Raises :class:`ObjectiveRejected` (and its limits/duplicates
        subclasses) when refused deterministically.
        """
        service = getattr(self, "_objective_service", None)
        if service is None:
            raise ObjectiveRejected("objective service is not wired")
        return await service.create_objective(
            project=project,
            goal=goal,
            acceptance=acceptance,
            entry=entry,
            source=source,
            source_id=source_id,
            requester=requester,
        )

    async def get_objective(self, objective_id: str) -> dict[str, Any] | None:
        """Read one objective document (API/MCP status surface)."""
        service = getattr(self, "_objective_service", None)
        if service is None:
            return await self._repo.get_objective(objective_id)
        return await service.get_objective(objective_id)

    async def create_owner_escalation(
        self,
        *,
        action: OwnerAction,
        decision: EscalationDecision,
        source_id: str | None = None,
        priority: TaskPriority = TaskPriority.HIGH,
    ) -> dict[str, Any]:
        """Owner-escalation entry (autonomy F4 TASK-2026-00114).

        Persists the escalation ERPNext-first through :meth:`create_task`
        (autorun off, caller-provided ``source_id`` for idempotency), then
        parks it in ``waiting``/``owner_escalation`` with any project phase
        workflow cleared: the status observer pushes Pending Review, the
        "Requiere humano" tray lists it by phase through the search API, and
        Horizon F5 reads phase/waiting_reason/tag. No ``resume_at``: it waits
        for the human, never for the scheduler.
        """
        if not decision.escalate:
            raise ValueError("nothing to escalate: the decision is automatic")
        spec = build_escalation_ticket(action, decision)
        payload = TaskCreateRequest(
            project=action.project,
            source=TaskSource.API,
            source_id=str(source_id).strip() if source_id else None,
            title=spec["title"],
            description=spec["description"],
            priority=priority,
            params=spec["params"],
            tags=spec["tags"],
            created_by="ceo",
            autorun=False,
        )
        created = await self.create_task(payload)
        parked = await self.patch_task(
            created["task_id"],
            {
                "status": TaskStatus.WAITING.value,
                "phase": spec["phase"],
                "waiting_reason": spec["waiting_reason"],
                "workflow_key": None,
                "workflow_phases": [],
                "phase_index": 0,
                "phase_assignments": {},
            },
        )
        return parked or created

    async def apply_owner_decision(
        self,
        task_id: str,
        *,
        approve: bool,
        actor_id: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """Apply the owner's answer to a pending decision (cockpit F5, TASK-2026-00120).

        Routes, in order: a YAML run blocked on a human resumes (approve) or
        fails (deny); an active human phase workflow goes through
        :meth:`create_phase_action`; a standalone owner-escalation ticket
        records the decision and completes, so the status observer pushes
        Completed to ERPNext. Anything else (personal scope, no human wait,
        QA without phases, discovery) is rejected fail-closed with a reason.
        """
        task = await self._repo.get_task(task_id)
        if task is None:
            return {"applied": False, "reason": "task_not_found"}
        if is_personal_scope(task):
            return {"applied": False, "reason": "personal_scope"}
        note = " ".join(str(comment or "").split())[:2000]

        blocked_run: dict[str, Any] | None = None
        if self._workflow_service is not None:
            run = await self._repo.latest_workflow_run_for_task(task_id)
            if run and str(run.get("status") or "") == WorkflowRunStatus.BLOCKED_HUMAN.value:
                blocked_run = run
        if blocked_run is not None:
            return await self._apply_decision_to_blocked_run(
                task, blocked_run, approve=approve, actor_id=actor_id, comment=note
            )

        _phases, _index, current = self._current_phase(task)
        if current and str(current.get("type") or "") == WorkflowPhaseType.HUMAN.value:
            action = TaskReviewAction.APPROVE if approve else TaskReviewAction.REJECT
            try:
                result = await self.create_phase_action(
                    task_id,
                    actor_id=actor_id,
                    actor_type=TaskReviewActorType.INTERNAL,
                    action=action,
                    comment=note,
                )
            except ValueError as exc:
                return {"applied": False, "reason": "phase_action_rejected", "detail": str(exc)}
            return {"applied": True, "route": "phase_action", "task": result.get("task")}

        if (
            str(task.get("phase") or "") == ESCALATION_PHASE
            and str(task.get("status") or "") == TaskStatus.WAITING.value
        ):
            return await self._apply_decision_to_escalation(
                task, approve=approve, actor_id=actor_id, comment=note
            )

        return {
            "applied": False,
            "reason": "not_answerable",
            "hint": "Se responde desde la bandeja Requiere humano del dashboard.",
        }

    async def _apply_decision_to_blocked_run(
        self,
        task: dict[str, Any],
        run: dict[str, Any],
        *,
        approve: bool,
        actor_id: str,
        comment: str,
    ) -> dict[str, Any]:
        task_id = str(task.get("task_id") or "")
        waiting = run.get("waiting") if isinstance(run.get("waiting"), dict) else {}
        current_key = (
            str(task.get("phase") or "").strip()
            or str(waiting.get("permission") or "").strip()
            or "human_in_the_loop"
        )
        if approve:
            resumed = await self._workflow_service.resume_blocked_run_for_task(
                task_id, action="approve", actor_id=actor_id
            )
            if resumed is None:
                return {"applied": False, "reason": "resume_failed"}
            payload = TaskReviewCreateRequest(
                actor_type=TaskReviewActorType.INTERNAL,
                actor_id=actor_id,
                action=TaskReviewAction.APPROVE,
                comment=comment,
                metadata={"phase": current_key, "event": "owner_decision"},
            )
            review_doc = make_task_review_document(
                task_id=task_id,
                project=str(task.get("project") or ""),
                payload=payload,
                approval_state_after=ApprovalState.APPROVED,
            )
            review = await self._repo.create_task_review(review_doc.model_dump(mode="python"))
            await self._hub.broadcast("task_review_created", review)
            updated_task = await self._repo.get_task(task_id) or task
            return {"applied": True, "route": "workflow_resume", "task": updated_task}

        await self._repo.patch_workflow_run(
            str(run["run_id"]),
            {
                "status": WorkflowRunStatus.FAILED.value,
                "error": f"Rejected by {actor_id}: {comment}",
                "completed_at": datetime.now(timezone.utc),
            },
        )
        summary = f"Rechazado por el dueño: {comment}" if comment else "Rechazado por el dueño."
        patched = await self.patch_task(
            task_id,
            {
                "status": TaskStatus.FAILED.value,
                "approval_state": ApprovalState.REJECTED.value,
                "waiting_reason": None,
                "error": summary,
                "last_response_summary": summary,
                "completed_at": None,
                "updated_by": actor_id,
            },
        )
        payload = TaskReviewCreateRequest(
            actor_type=TaskReviewActorType.INTERNAL,
            actor_id=actor_id,
            action=TaskReviewAction.REJECT,
            comment=comment,
            metadata={"phase": current_key, "event": "owner_decision"},
        )
        review_doc = make_task_review_document(
            task_id=task_id,
            project=str(task.get("project") or ""),
            payload=payload,
            approval_state_after=ApprovalState.REJECTED,
        )
        review = await self._repo.create_task_review(review_doc.model_dump(mode="python"))
        await self._hub.broadcast("task_review_created", review)
        return {"applied": True, "route": "workflow_reject", "task": patched or task}

    async def _apply_decision_to_escalation(
        self,
        task: dict[str, Any],
        *,
        approve: bool,
        actor_id: str,
        comment: str,
    ) -> dict[str, Any]:
        task_id = str(task.get("task_id") or "")
        params = dict(task.get("params") or {})
        escalation = dict(params.get("escalation") or {})
        escalation["decision"] = {
            "decision": "approved" if approve else "denied",
            "by": actor_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "comment": comment,
        }
        params["escalation"] = escalation
        verdict = "aprobada" if approve else "denegada"
        summary = f"Decisión del dueño: {verdict}."
        if comment:
            summary += f" {comment}"
        patched = await self.patch_task(
            task_id,
            {
                "status": TaskStatus.COMPLETED.value,
                "approval_state": (
                    ApprovalState.APPROVED.value if approve else ApprovalState.REJECTED.value
                ),
                "waiting_reason": None,
                "last_response_summary": summary,
                "completed_at": datetime.now(timezone.utc),
                "params": params,
                "updated_by": actor_id,
            },
        )
        payload = TaskReviewCreateRequest(
            actor_type=TaskReviewActorType.INTERNAL,
            actor_id=actor_id,
            action=TaskReviewAction.APPROVE if approve else TaskReviewAction.REJECT,
            comment=comment,
            metadata={"phase": ESCALATION_PHASE, "event": "owner_decision"},
        )
        review_doc = make_task_review_document(
            task_id=task_id,
            project=str(task.get("project") or ""),
            payload=payload,
            approval_state_after=(
                ApprovalState.APPROVED if approve else ApprovalState.REJECTED
            ),
        )
        review = await self._repo.create_task_review(review_doc.model_dump(mode="python"))
        await self._hub.broadcast("task_review_created", review)
        return {"applied": True, "route": "escalation_close", "task": patched or task}

    async def blockers_open(self, task: Mapping[str, Any]) -> bool:
        """True when any ``blocked_by`` dependency is not terminal (§9.5).

        The enqueuer never dispatches a ticket with open blockers; ``failed``
        counts as open (no work was done).
        """
        blockers = task.get("blocked_by") or []
        for blocker in blockers:
            row = await self._repo.get_task(str(blocker))
            status = str((row or {}).get("status") or "")
            if status not in {"completed", "cancelled"}:
                return True
        return False

    async def _fire_notion_sync_back(self, task: dict[str, Any], status_label: str) -> None:
        if self._notion is None:
            return
        if not self._settings.NOTION_SYNC_BACK_ENABLED:
            return
        page_id = self._resolve_notion_page_id(task)
        if not page_id:
            return

        sync_key = f"notion_sync_{status_label}"
        already_done = task.get("post_actions_executed") or []
        if sync_key in already_done:
            return

        try:
            result = await asyncio.to_thread(
                self._notion.update_page_properties,
                page_id=page_id,
                runtime_status=status_label,
                runtime_summary=task.get("last_response_summary"),
                last_sync_at=datetime.now(timezone.utc).isoformat(),
                openceo_ref=task.get("task_id"),
                agent=task.get("agent"),
                approval_required=task.get("approval_required"),
                approval_state=task.get("approval_state"),
            )
            if result.get("ok"):
                await self._repo.patch_task(
                    task["task_id"],
                    {"post_actions_executed": already_done + [sync_key]},
                )
                logger.info("Notion sync-back '%s' succeeded for task %s", status_label, task["task_id"])
            else:
                logger.warning("Notion sync-back '%s' failed for task %s: %s",
                               status_label, task["task_id"], result.get("message"))
        except Exception:
            logger.warning("Notion sync-back '%s' error for task %s", status_label, task["task_id"], exc_info=True)

    async def patch_task(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        task = await self._repo.get_task(task_id)
        if task is None:
            return None

        # Merge params instead of overwriting (preserves pm_ticket, evidence, etc.)
        if "params" in updates and isinstance(updates["params"], dict):
            existing = dict(task.get("params") or {})
            for k, v in updates["params"].items():
                if v is None:
                    existing.pop(k, None)
                else:
                    existing[k] = v
            updates["params"] = existing

        actor = str(updates.get("updated_by") or "").strip()

        # Build history entry from meaningful changes
        changes: list[str] = []
        for key in ("status", "phase", "phase_index", "workflow_key", "approval_state", "qa_review_state", "error", "waiting_reason"):
            if key in updates and updates[key] != task.get(key):
                changes.append(f"{key}={updates[key]}")
        if changes and actor:
            history_entry = {
                "action": "updated",
                "actor_id": actor,
                "detail": "; ".join(changes),
                "timestamp": datetime.now(timezone.utc),
            }
            updates["history"] = (task.get("history") or []) + [history_entry]

        updated = await self._repo.patch_task(task_id, updates)
        if updated is not None:
            await self._hub.broadcast("task_updated", updated)
            if (
                task.get("autorun") is False
                and "autorun" in updates
                and bool(updates["autorun"]) is True
            ):
                # An operator unpaused the ticket: dispatch event. The
                # dispatcher never writes `autorun`, so this cannot
                # self-wake; no other field wakes here for the same reason.
                self._wake_dispatcher()
            if any(key in updates for key in {"approval_required", "approval_state"}):
                asyncio.create_task(self._fire_notion_approval_sync_back(updated))
            # Solo en la transicion a un estado terminal, no en cada parche
            # posterior: si no, un ticket ya cerrado replanificaria sin parar.
            if (
                str(updated.get("status")) in _TERMINAL_TASK_STATUSES
                and str(task.get("status")) != str(updated.get("status"))
            ):
                asyncio.create_task(self._notify_horizon_terminal(updated))
                self._wake_ceo_incident(updated)
        return updated

    async def create_task_review(self, task_id: str, payload: TaskReviewCreateRequest) -> dict[str, Any]:
        task = await self._repo.get_task(task_id)
        if task is None:
            raise ValueError("Task not found")

        approval_required = bool(task.get("approval_required"))
        approval_state = str(task.get("approval_state") or ApprovalState.NOT_REQUIRED.value)
        next_state: ApprovalState | None = None

        if payload.action == TaskReviewAction.APPROVE:
            approval_required = True
            next_state = ApprovalState.APPROVED
        elif payload.action == TaskReviewAction.REQUEST_CHANGES:
            approval_required = True
            next_state = ApprovalState.CHANGES_REQUESTED
        elif payload.action == TaskReviewAction.REJECT:
            approval_required = True
            next_state = ApprovalState.REJECTED
        elif approval_required and approval_state == ApprovalState.NOT_REQUIRED.value:
            next_state = ApprovalState.PENDING

        review_doc = make_task_review_document(
            task_id=task_id,
            project=str(task.get("project") or ""),
            payload=payload,
            approval_state_after=next_state,
        )
        created = await self._repo.create_task_review(review_doc.model_dump(mode="python"))

        updated_task = task
        if next_state is not None:
            patched = await self.patch_task(
                task_id,
                {
                    "approval_required": approval_required,
                    "approval_state": next_state.value,
                },
            )
            if patched is not None:
                updated_task = patched

        await self._hub.broadcast("task_review_created", created)
        return {
            "review": created,
            "task": updated_task,
        }

    async def create_phase_action(
        self,
        task_id: str,
        *,
        actor_id: str,
        actor_type: TaskReviewActorType,
        action: TaskReviewAction,
        comment: str = "",
        attachments: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task = await self._repo.get_task(task_id)
        if task is None:
            raise ValueError("Task not found")

        phases, index, current = self._current_phase(task)
        if not current:
            raise ValueError("Task does not have an active phase workflow")
        if str(current.get("type") or "") != WorkflowPhaseType.HUMAN.value:
            raise ValueError("Current phase is not waiting for a human decision")

        current_key = phase_key(current) or str(task.get("phase") or "")
        payload = TaskReviewCreateRequest(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            comment=comment,
            attachments=attachments or [],
            metadata={
                **(metadata or {}),
                "phase": current_key,
                "workflow_key": task.get("workflow_key"),
                "phase_index": index,
            },
        )

        approval_after: ApprovalState | None = None
        updates: dict[str, Any] = {}
        if action == TaskReviewAction.APPROVE:
            approval_after = ApprovalState.APPROVED
            if self._workflow_service is not None:
                resumed = await self._workflow_service.resume_blocked_run_for_task(
                    task_id, action="approve", actor_id=actor_id
                )
                if resumed is not None:
                    review_doc = make_task_review_document(
                        task_id=task_id,
                        project=str(task.get("project") or ""),
                        payload=payload,
                        approval_state_after=approval_after,
                    )
                    review = await self._repo.create_task_review(review_doc.model_dump(mode="python"))
                    await self._hub.broadcast("task_review_created", review)
                    updated_task = await self._repo.get_task(task_id) or task
                    return {"review": review, "task": updated_task}

            next_index = index + 1
            if next_index >= len(phases):
                updates = {
                    "status": TaskStatus.COMPLETED.value,
                    "approval_required": False,
                    "approval_state": ApprovalState.APPROVED.value,
                    "waiting_reason": None,
                    "completed_at": datetime.now(timezone.utc),
                    "updated_by": actor_id,
                }
            else:
                updates = self._phase_entry_updates(
                    task,
                    phases,
                    next_index,
                    phases[next_index],
                    actor_id=actor_id,
                )
        elif action in {TaskReviewAction.REQUEST_CHANGES, TaskReviewAction.REJECT}:
            approval_after = ApprovalState.CHANGES_REQUESTED if action == TaskReviewAction.REQUEST_CHANGES else ApprovalState.REJECTED
            if self._workflow_service is not None:
                run = await self._repo.latest_workflow_run_for_task(task_id)
                if run and str(run.get("status") or "") == WorkflowRunStatus.BLOCKED_HUMAN.value:
                    await self._repo.patch_workflow_run(
                        str(run["run_id"]),
                        {
                            "status": WorkflowRunStatus.FAILED.value,
                            "error": f"Rejected by {actor_id}: {comment}",
                            "completed_at": datetime.now(timezone.utc),
                        },
                    )
            updates = self._phase_rejection_updates(task, phases, current, actor_id=actor_id, comment=comment)
        elif action == TaskReviewAction.COMMENT:
            approval_after = None

        review_doc = make_task_review_document(
            task_id=task_id,
            project=str(task.get("project") or ""),
            payload=payload,
            approval_state_after=approval_after,
        )
        review = await self._repo.create_task_review(review_doc.model_dump(mode="python"))

        updated_task = task
        if updates:
            patched = await self.patch_task(task_id, updates)
            if patched is not None:
                updated_task = patched
                if updates.get("status") == TaskStatus.QUEUED.value:
                    # Rejection/approve-to-agent re-queues: dispatch event.
                    self._wake_dispatcher()

        await self._hub.broadcast("task_review_created", review)
        return {"review": review, "task": updated_task}

    async def revert_phase_approval(
        self,
        task_id: str,
        *,
        actor_id: str,
        actor_type: TaskReviewActorType,
        comment: str = "",
    ) -> dict[str, Any]:
        task = await self._repo.get_task(task_id)
        if task is None:
            raise ValueError("Task not found")

        phases, index, current = self._current_phase(task)
        if not current:
            raise ValueError("Task does not have an active phase workflow")
        if str(current.get("type") or "") != WorkflowPhaseType.HUMAN.value:
            raise ValueError("Current phase is not waiting for a human decision")
        if index <= 0:
            raise ValueError("Current phase cannot be reverted")

        target_index: int | None = None
        for candidate_index in range(index - 1, -1, -1):
            candidate = phases[candidate_index]
            if str(candidate.get("type") or "") == WorkflowPhaseType.HUMAN.value:
                target_index = candidate_index
                break
        if target_index is None:
            raise ValueError("No previous human phase to revert to")

        target_phase = phases[target_index]
        current_key = phase_key(current) or str(task.get("phase") or "")
        target_key = phase_key(target_phase) or str(target_phase.get("key") or "")
        review_comment = (comment or "").strip() or f"Fase revertida de {current_key} a {target_key}"
        payload = TaskReviewCreateRequest(
            actor_type=actor_type,
            actor_id=actor_id,
            action=TaskReviewAction.COMMENT,
            comment=review_comment,
            metadata={
                "event": "phase_reverted",
                "phase": current_key,
                "workflow_key": task.get("workflow_key"),
                "phase_index": index,
                "reverted_to_phase": target_key,
                "reverted_to_phase_index": target_index,
            },
        )
        review_doc = make_task_review_document(
            task_id=task_id,
            project=str(task.get("project") or ""),
            payload=payload,
            approval_state_after=ApprovalState.PENDING,
        )
        review = await self._repo.create_task_review(review_doc.model_dump(mode="python"))

        updates = self._phase_entry_updates(
            task,
            phases,
            target_index,
            target_phase,
            actor_id=actor_id,
        )
        if not bool(target_phase.get("client_visible")):
            updates["client_visible"] = False

        patched = await self.patch_task(task_id, updates)
        updated_task = patched or task
        if patched is not None and updates.get("status") == TaskStatus.QUEUED.value:
            # A revert lands on a human phase (waiting) today; if that ever
            # re-queues, the dispatcher must hear about it.
            self._wake_dispatcher()

        await self._hub.broadcast("task_review_created", review)
        return {"review": review, "task": updated_task}

    def _phase_rejection_updates(
        self,
        task: dict[str, Any],
        phases: list[dict[str, Any]],
        current: dict[str, Any],
        *,
        actor_id: str,
        comment: str,
    ) -> dict[str, Any]:
        on_reject = str(current.get("on_reject") or "restart").strip().lower()
        target_index = 0
        if on_reject.startswith("phase:"):
            target_index = find_phase_index(phases, on_reject.split(":", 1)[1])
        elif on_reject == "previous":
            target_index = max(0, int(task.get("phase_index") or 0) - 1)
        else:
            target_index = first_executable_phase_index(phases)

        target_phase = phases[target_index]
        updates = self._phase_entry_updates(
            task,
            phases,
            target_index,
            target_phase,
            actor_id=actor_id,
        )
        updates.update(
            {
                "status": TaskStatus.QUEUED.value,
                "agent": AgentName.CEO.value,
                "agent_hint": AgentName.CEO.value,
                "waiting_reason": None,
                "approval_required": False,
                "approval_state": ApprovalState.CHANGES_REQUESTED.value,
                "completed_at": None,
                "error": None,
            }
        )

        params = self._task_params(task)
        rejections = params.get("phase_rejections")
        if not isinstance(rejections, list):
            rejections = []
        rejections.append(
            {
                "phase": phase_key(current),
                "actor_id": actor_id,
                "comment": comment,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        params["phase_rejections"] = rejections
        updates["params"] = params
        return updates

    @staticmethod
    def _workflow_outcome_to_graph_out(outcome: WorkflowExecutionOutcome) -> dict[str, Any]:
        return {
            "selected_agent": outcome.selected_agent,
            "status": outcome.status,
            "steps": outcome.steps,
            "last_response_summary": outcome.last_response_summary,
            "waiting_reason": outcome.waiting_reason,
            "error": outcome.error,
            "params": outcome.params,
            "approval_required": outcome.approval_required,
            "approval_state": outcome.approval_state,
        }

    async def list_task_reviews(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        task = await self._repo.get_task(task_id)
        if task is None:
            raise ValueError("Task not found")
        return await self._repo.list_task_reviews(task_id, limit=limit)

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._repo.list_tasks(limit=limit)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._repo.get_task(task_id)

    # NOTE (F3 TASK-2026-00113): the dispatch cycle (requeue due waiting +
    # admit queued) lives in ``dispatcher.Dispatcher`` now; the retired
    # ``resume_waiting_tasks``/``resume_queued_tickets``/``_maybe_gate_launch``
    # called ``RateMonitor.can_launch_ticket``, retired in favour of
    # ``RateMonitor.admission_for_ticket`` (tier-aware reserves + reason).

    async def _on_ticket_done(self, ticket: dict[str, Any]) -> None:
        """Post-completion: QA gate, then refresh rate limits and wake the dispatcher."""
        # Dependency wakeup (§9.5): tickets blocked by this one requeue when
        # it lands terminal-good. Event-driven, no sweeps.
        if str(ticket.get("status") or "") in {"completed", "cancelled"}:
            try:
                await self._requeue_dependents(str(ticket.get("task_id") or ""))
            except Exception:
                logger.warning("Dependent requeue error for %s",
                               ticket.get("task_id"), exc_info=True)
        # QA review gate: if enabled, intercept before marking truly done
        if self._qa_service is not None and ticket.get("qa_review_required"):
            try:
                ticket = await self._qa_service.on_ticket_completed(ticket)
            except Exception:
                logger.warning("QA gate error for ticket %s", ticket.get("task_id"), exc_info=True)

        if self._rate_monitor is None:
            return
        try:
            await self._rate_monitor.on_ticket_completed(ticket)
            try:
                # Fresh quota invalidates stale quota parks: drop their
                # resume so the wake below re-evaluates instead of
                # resume-holding them until a stale reset instant.
                await self._repo.clear_quota_park_resumes()
            except Exception:
                logger.warning("Quota park clearing failed", exc_info=True)
        except Exception:
            logger.warning("_on_ticket_done error", exc_info=True)
        # Quota refreshed after a finish: the dispatcher re-evaluates.
        self._wake_dispatcher()

    async def _requeue_dependents(self, task_id: str) -> int:
        """Requeue tickets directly blocked by ``task_id`` (§9.5).

        Only direct dependents: each requeued ticket re-checks its own
        remaining blockers at admission, so chains wake level by level.
        """
        if not task_id:
            return 0
        dependents = await self._repo.list_tasks_blocked_by(task_id)
        requeued = 0
        for ticket in dependents:
            if not self._task_allows_autorun(ticket):
                continue
            await self._repo.patch_task(str(ticket.get("task_id") or ""), {
                "status": TaskStatus.QUEUED.value,
                "waiting_reason": None,
                "resume_at": None,
            })
            requeued += 1
        if requeued:
            self._wake_dispatcher()
        return requeued

    async def _fire_notion_approval_sync_back(self, task: dict[str, Any]) -> None:
        if self._notion is None:
            return
        if not self._settings.NOTION_SYNC_BACK_ENABLED:
            return
        page_id = self._resolve_notion_page_id(task)
        if not page_id:
            return

        sync_key = f"notion_approval_sync_{task.get('approval_required')}_{task.get('approval_state')}"
        already_done = task.get("post_actions_executed") or []
        if sync_key in already_done:
            return

        try:
            result = await asyncio.to_thread(
                self._notion.update_page_properties,
                page_id=page_id,
                approval_required=bool(task.get("approval_required")),
                approval_state=task.get("approval_state"),
                last_sync_at=datetime.now(timezone.utc).isoformat(),
                openceo_ref=task.get("task_id"),
                agent=task.get("agent"),
            )
            if result.get("ok"):
                await self._repo.patch_task(
                    task["task_id"],
                    {"post_actions_executed": already_done + [sync_key]},
                )
                logger.info("Notion approval sync-back succeeded for task %s", task["task_id"])
            else:
                logger.warning(
                    "Notion approval sync-back failed for task %s: %s",
                    task["task_id"],
                    result.get("message"),
                )
        except Exception:
            logger.warning("Notion approval sync-back error for task %s", task["task_id"], exc_info=True)
