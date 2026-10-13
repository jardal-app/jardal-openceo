from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from pymongo.errors import DuplicateKeyError

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
from objectives import engine as engine_mod
from objectives.engine import (
    ObjectiveRejected,
    TicketCloseContext,
    on_ticket_closed,
)
from objectives.pedido import is_pedido
from objectives.ticket_cost import COST_RUNS_KEY, record_runs
from objectives.plan_spend import (
    SPEND_WAITING_REASON,
    PlanSpendError,
    check_plan_spend,
    loose_spend_source_id,
    parse_costs,
    spend_hold_objective,
    spend_total_text,
)
from orchestration.agent_sandbox import ticket_dir
from orchestration.crystallization import (
    DRAFTS_DIRNAME,
    SaveStepConfig,
    has_drafts,
    mirror_ticket,
    run_save_step,
)
from owner_channel import (
    OWNER_QUESTION_PHASE,
    build_owner_question,
    build_tool_ticket,
    denied_origin_verdict,
    is_owner_question,
    owner_answer_record,
    owner_question_block,
    question_answered,
)
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
from tool_locks import (
    LOCK_WAITING_PREFIX,
    ToolLockError,
    action_fingerprint,
    check_dns_delete_lock,
    check_email_lock,
    check_payment_lock,
    lock_hold_action,
    lock_question_spec,
    lock_source_id,
    lock_waiting_reason,
    pending_lock_result,
)
from tools.erpnext_client import (
    ERPNextDateBoundError,
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
import visibility

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


class _UnknownWorkflowTarget(Exception):
    """Explicit workflow target no template resolves (loud failure)."""


def _target_of(task: dict[str, Any]) -> str:
    params = task.get("params")
    params = params if isinstance(params, dict) else {}
    return str(params.get("target_agent") or "").strip()


def _workflow_target_of(task: dict[str, Any]) -> str:
    params = task.get("params")
    params = params if isinstance(params, dict) else {}
    for key in ("workflow", "workflow_id", "workflow_ref"):
        name = str(params.get(key) or "").strip()
        if name:
            return name
    return "?"

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

#: `apply_business_status(parent_task=...)`: distingue "el webhook dice sin
#: padre" (None) de "el llamador no lo pasa" (llamadas viejas/tests).
_WEBHOOK_PARENT_UNSET: Any = object()


def _transition_audit_text(value: Any, limit: int = _TRANSITION_AUDIT_MAX) -> str:
    return " ".join(str(value or "").split())[:limit]


def wait_comment_text(task: dict[str, Any], reason: str) -> str:
    """ERP one-shot text for a ``routing_missing`` wait (pure).

    Agent waits (unknown/disabled/human target, TASK-2026-00195) name the
    agent and its reason; tier waits keep the historical wording.
    """
    if "agent '" in reason:
        detail = reason.split("routing_missing:", 1)[1].strip() \
            if "routing_missing:" in reason else reason.strip()
        return ("OpenCEO no puede ejecutar este ticket: "
                f"el agente pedido no puede correr ({detail}).")
    # Undecided loose work is planned before dispatch (TASK-2026-00204),
    # so this wait means a planner/validation gap, not an owner action.
    if task.get("objective_id"):
        return (
            "OpenCEO no puede ejecutar este ticket: pertenece a un "
            "objetivo pero llegó sin tier (coding_tool). Hay que "
            "regenerar el hijo con tier o fijar coding_tool "
            "(tier_s, tier_a, tier_b o tier_c) por API."
        )
    return (
        "OpenCEO no puede ejecutar este ticket: llegó sin decisión "
        "(ni agente ni tier) y el planner no lo decidió. Fija un "
        "agente explícito o coding_tool por API para continuar."
    )


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
        # Visibilidad derivada (00146 + 00147): cola y worker propios para que
        # un rollup lento nunca retrase los pushes de estado.
        self._visibility_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._visibility_worker: asyncio.Task[None] | None = None
        self._horizon_terminal: Any | None = None
        # Dispatcher wake callback (F3 TASK-2026-00113): main.py injects
        # Dispatcher.wake. Read via getattr: tests build TaskService with
        # __new__ and without __init__, so the attribute may not exist.
        self._dispatcher_wake: Any | None = None
        self._ceo_wake: Any | None = None
        self._token_issuer: Any | None = None

    def set_token_issuer(self, issuer: Any | None) -> None:
        """Inject the MCP ticket-token issuer (TASK-2026-00196).

        Lets the terminal-transition funnel revoke the ticket's badges.
        Read via getattr like the other wakes: tests may build
        TaskService without __init__.
        """
        self._token_issuer = issuer

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

    def wake_dispatcher(self) -> None:
        """Public queue-event signal for external producers (00174).

        Same guarantee as the internal wake: coalesced by the
        dispatcher, silent without wiring, never raises.
        """
        self._wake_dispatcher()

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

    async def _revoke_ticket_badges(self, task: dict[str, Any] | None) -> None:
        """Revoke the ticket's MCP badges on terminal execution (TASK-2026-00196).

        Never breaks the close: runs as a background task and swallows
        every failure. Silent without an issuer (tests, standalone use).
        """
        issuer = getattr(self, "_token_issuer", None)
        if issuer is None or not task:
            return
        name = self._webhook_name(task)
        if not name:
            return
        try:
            await issuer.revoke_ticket(name)
        except Exception:
            logger.warning("MCP badge revocation failed for %s", name, exc_info=True)

    async def revoke_badges_for_patch(
        self, task_id: str, status: str | None,
    ) -> None:
        """Prompt badge revocation for direct repo writers (objectives envs).

        Best-effort and silent: never breaks the writer. The service
        funnel covers its own writes; env.patch_task closures call here.
        """
        if not status or status not in _TERMINAL_TASK_STATUSES:
            return
        try:
            task = await self._repo.get_task(task_id)
        except Exception:
            logger.warning("MCP badge lookup failed for %s", task_id,
                           exc_info=True)
            return
        await self._revoke_ticket_badges(task)

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

    def start_visibility_worker(self) -> None:
        if self._visibility_worker is not None and not self._visibility_worker.done():
            return
        self._visibility_worker = asyncio.create_task(self._visibility_queue_loop())

    async def stop_visibility_worker(self) -> None:
        if self._visibility_worker is not None:
            self._visibility_worker.cancel()
            try:
                await self._visibility_worker
            except asyncio.CancelledError:
                pass
            self._visibility_worker = None

    async def _visibility_queue_loop(self) -> None:
        while True:
            item = await self._visibility_queue.get()
            try:
                await self._refresh_visibility(item)
            except Exception:
                logger.warning("Fallo recalculando visibilidad", exc_info=True)
            finally:
                self._visibility_queue.task_done()

    def _enqueue_visibility(self, task_id: str | None, name: str, reason: str) -> None:
        """Encola un refresco de visibilidad (00146 + 00147). Nunca lanza.

        El % del grupo, las fechas reales/estimadas y el span del padre son
        valores DERIVADOS del estado de negocio: se recalculan por evento en
        su propio worker, fuera del camino crítico, y un fallo aquí no rompe
        la transición que los originó.
        """
        queue = getattr(self, "_visibility_queue", None)
        if queue is None or not str(name or "").strip():
            return
        try:
            queue.put_nowait(
                {
                    "task_id": str(task_id or "") or None,
                    "name": str(name).strip(),
                    "reason": str(reason or ""),
                }
            )
        except Exception:
            logger.warning("No se pudo encolar la visibilidad de %s", name, exc_info=True)

    async def _push_status(self, item: dict[str, Any]) -> None:
        """Empuja una transicion con reintento real, no un sleep fijo."""
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not adapter.is_enabled:
            return

        # QA condicional (TASK-2026-00040) y esperas con accion humana
        # (TASK-2026-00118): el destino de `completed`/`waiting` se decide con
        # el ticket fresco, no con el estado encolado. Si QA aprobo o la espera
        # se resolvio entre el encolado y el push, el ticket no va a revision.
        # `in_progress` también necesita el ticket: sus fechas reales
        # (started_at) viajan en el mismo PUT (TASK-2026-00147).
        task = None
        if str(item.get("status") or "").strip().lower() in {
            TaskStatus.COMPLETED.value,
            TaskStatus.WAITING.value,
            TaskStatus.IN_PROGRESS.value,
        }:
            try:
                repo = getattr(self, "_repo", None)
                if repo is not None:
                    task = await repo.get_task(str(item.get("task_id") or ""))
            except Exception:
                logger.warning("No se pudo leer el ticket para QA condicional", exc_info=True)
                task = None

        work_dates = self._work_dates_for_push(item.get("status"), task)
        span_extended = False
        delay = 1.0
        for attempt in range(3):
            try:
                try:
                    await adapter.update_status(
                        item["name"], item["status"], task=task,
                        work_dates=work_dates,
                    )
                except ERPNextDateBoundError:
                    # El hijo pasa el fin del padre: se extienden los ancestros
                    # y se reintenta UNA vez (el padre corto sí entra, así que
                    # extender no rebota). Si vuelve a fallar, cae al `except
                    # ERPNextError` exterior como error duro.
                    if span_extended or not work_dates:
                        raise
                    await self._ensure_span_for_child(
                        adapter,
                        str(item["name"] or ""),
                        start=work_dates.get("exp_start_date"),
                        end=work_dates.get("exp_end_date"),
                    )
                    span_extended = True
                    await adapter.update_status(
                        item["name"], item["status"], task=task,
                        work_dates=work_dates,
                    )
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
                self._enqueue_visibility(
                    str(item.get("task_id") or "") or None,
                    str(item.get("name") or ""),
                    f"push:{item.get('status')}",
                )
                return

    def _work_dates_for_push(
        self, openceo_status: Any, task: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Fechas del PUT de estado, o None si no aportan nada (00147)."""
        if not isinstance(task, dict):
            return None
        zone = self._site_zone_name()
        try:
            dates = visibility.work_dates_for_push(
                openceo_status=str(openceo_status or ""),
                started_at=visibility.as_utc_datetime(task.get("started_at")),
                completed_at=visibility.as_utc_datetime(task.get("completed_at")),
                zone=zone,
                now=datetime.now(timezone.utc),
            )
        except Exception:
            logger.warning("No se pudieron calcular las fechas del push", exc_info=True)
            return None
        return dates or None

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
                reheld = await self._rehold_loose_spend(existing)
                return reheld or existing

        declared_spend = self._loose_spend_entries(
            payload.params, objective_id=payload.objective_id,
            origin=payload.origin)
        if declared_spend is not None:
            # Malformed costs reject before anything persists: no
            # business record, no execution (TASK-2026-00207).
            parse_costs(declared_spend)

        doc = make_task_document(payload)
        await self._apply_default_phase_workflow_to_document(doc)
        if doc.ticket_number is None:
            doc.ticket_number = await self._repo.get_next_ticket_number()

        # Spend pre-park (review H3): the decision is sync and happens
        # BEFORE persist, so the ticket is never visible as
        # queued-with-spend to a concurrent dispatch cycle. The hold
        # below reuses this same decision (no mandate re-read).
        spend_hold = self._prepark_spend_hold(doc, declared_spend, payload)

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
            if spend_hold is None:
                return created
            # Review H1: a declared spend holds even with the link
            # pending (the dispatcher skips linkless tickets, and the
            # link heals by mirror, not by waiting_reason).

        # Spend lock (TASK-2026-00207): a loose ticket declaring new
        # spend parks for Owner approval before the dispatcher can see
        # it. Like erpnext_pending above, this is a creation-time hold,
        # not admission: the F3 single path below is untouched.
        held = await self._hold_loose_spend(
            created, declared_spend, spend=spend_hold)
        if held is not None:
            return held
        if erpnext_pending:
            return created

        # The dispatcher (F3) owns admission: creation persists queued and
        # wakes it. No direct launch and no pre-gate here, so priority order,
        # per-project/per-objective limits and Tier S reserves apply to every
        # ticket through a single path.
        if self._settings.TASK_AUTORUN and payload.autorun:
            self._wake_dispatcher()

        # Visibilidad (00146 + 00147): estima la hoja nueva y refresca al
        # padre si lo tiene. Job en cola, fuera del camino crítico.
        business_name = ""
        mirrors = self._task_params(created).get("_mirrors")
        if isinstance(mirrors, dict) and isinstance(mirrors.get("erpnext"), dict):
            business_name = str(mirrors["erpnext"].get("name") or "").strip()
        if business_name:
            self._enqueue_visibility(
                str(created.get("task_id") or "") or None, business_name, "create"
            )

        return created

    @staticmethod
    def _loose_spend_entries(
        params: dict[str, Any] | None,
        *,
        objective_id: str | None,
        origin: Any,
    ) -> list[Any] | None:
        """Declared costs when the creation spend gate applies, else None.

        Objective children are plan-gated, never creation-gated; tickets
        targeting a YAML workflow skip the creation gate and keep their
        in-workflow handling. Shape errors stay for ``parse_costs`` at
        the call sites.
        """
        if objective_id or str(origin or "") == "objective_planner":
            return None
        if not isinstance(params, dict):
            return None
        if any(params.get(key) for key in (
                "workflow", "workflow_id", "workflow_ref", "use_workflow",
                "discover_workflow", "workflow_discovery")):
            return None
        costs = params.get("costs")
        if costs is None:
            return None
        return costs  # type: ignore[return-value]

    def _prepark_spend_hold(
        self,
        doc: Any,
        declared: list[Any] | None,
        payload: TaskCreateRequest,
    ) -> dict[str, Any] | None:
        """Spend decision before persist; pre-parks the doc when held.

        Sync (the mandate read is local): with no declared spend, no
        launch scheduled, or an automatic decision, the doc is
        untouched and creation proceeds. A held doc persists already
        waiting, so no dispatch cycle can admit it mid-creation.
        """
        if not declared:
            return None
        if not (self._settings.TASK_AUTORUN and payload.autorun):
            return None
        spend = check_plan_spend(
            costs=declared,
            summary=str(payload.title or ""),
            mandate=self._company_mandate(),
        )
        if not spend["escalate"]:
            return None
        doc.status = TaskStatus.WAITING
        doc.waiting_reason = SPEND_WAITING_REASON
        return spend

    async def _hold_loose_spend(
        self,
        created: dict[str, Any],
        declared: list[Any] | None,
        *,
        spend: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Park a loose ticket with declared spend for Owner approval.

        The ticket keeps autorun (the approval requeues it) and waits
        operationally; the question carries the human action through
        the single human channel. None when the ticket launches. The
        creation path passes its pre-persist decision; the re-hold
        path re-checks (a stored declaration may be unparseable).
        """
        if not declared:
            return None
        if not self._task_allows_autorun(created):
            return None
        status = str(created.get("status") or "")
        if status == TaskStatus.WAITING.value and (
                str(created.get("waiting_reason") or "")
                != SPEND_WAITING_REASON
                or (created.get("params") or {}).get("owner_question_id")):
            return None
        if status not in (TaskStatus.QUEUED.value, TaskStatus.WAITING.value):
            return None
        if spend is None:
            try:
                spend = check_plan_spend(
                    costs=declared,
                    summary=str(created.get("title") or ""),
                    mandate=self._company_mandate(),
                )
            except PlanSpendError as exc:
                # Unparseable declaration on a stored ticket (new
                # creations reject earlier): escalate, never launch blind.
                spend = {
                    "escalate": True,
                    "category": "gasto_nuevo",
                    "reason": f"malformed costs declaration: {exc}",
                    "human_action": (
                        "Corregir params.costs o denegar: "
                        f"{str(created.get('title') or '')}."
                    ),
                    "amount": "?",
                    "currency": "?",
                    "unknown": [],
                }
        if not spend["escalate"]:
            return None
        task_id = str(created.get("task_id") or "")
        total = spend_total_text(spend)
        question = await self.create_owner_question(
            kind="question",
            question=f"Autorizar el gasto declarado del ticket ({total})?",
            human_action=str(spend.get("human_action")
                             or "Autorizar o denegar"),
            project=created.get("project"),
            origin_task_id=task_id,
            origin_title=str(created.get("title") or ""),
            context=f"Motivo: {spend.get('reason')}",
            source_id=loose_spend_source_id(task_id),
        )
        parked = await self.patch_task(task_id, {
            "status": TaskStatus.WAITING.value,
            "waiting_reason": SPEND_WAITING_REASON,
            "params": {"owner_question_id": question.get("task_id")},
        })
        return parked or created

    async def _rehold_loose_spend(
        self, existing: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Re-hold a spend ticket whose park was lost (fail-closed).

        A duplicate creation of a queued spend ticket with no linked
        question re-runs the hold. An answered question means the flow
        already ran its course and is left alone.
        """
        if str(existing.get("status") or "") != TaskStatus.QUEUED.value:
            return None
        params = existing.get("params")
        if not isinstance(params, dict):
            return None
        if params.get("owner_question_id"):
            return None
        declared = self._loose_spend_entries(
            params, objective_id=existing.get("objective_id"),
            origin=existing.get("origin"))
        if not declared:
            return None
        try:
            prior = await self._repo.find_task_by_source(
                "api", loose_spend_source_id(
                    str(existing.get("task_id") or "")))
        except Exception:
            prior = None
        if prior is not None and question_answered(prior):
            return None
        return await self._hold_loose_spend(existing, declared)

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
    # webhook de Frappe, no un sondeo: las reglas permanentes de AGENTS.md prohiben
    # los barridos periodicos.

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
        parent_task: Any = _WEBHOOK_PARENT_UNSET,
        business_name: str | None = None,
    ) -> dict[str, Any]:
        """Aplica al runtime una decision tomada por un humano en ERPNext.

        Solo se propagan las decisiones de NEGOCIO (`Completed`, `Cancelled` y el borrado).
        Los demas estados de `Task` son eco de lo que OpenCEO ya empujo, y propagarlos
        seria un bucle.

        El guardia anti-eco es estructural, no casual: el parche escribe `status_pushed`
        con el mismo valor que el estado nuevo, asi que aunque el observador vea el
        cambio, no lo reenvia a ERPNext.

        Visibilidad (00146 + 00147): el webhook también trae trabajo manual
        (inicio/cierre/reapertura/relink en desk). Lo derivado se recalcula
        con un job `kind=visibility`; el eco de nuestros propios PUTs no
        encola nada (se decide con ejecución + espejo, sin lecturas extra).
        """
        adapter = getattr(self, "_erpnext", None)
        if adapter is None:
            return {"ok": False, "status": "no_adapter"}

        task = await self.find_task_for_business_ref(task_id=task_id, ref=ref)
        if task is None:
            if str(business_name or "").strip():
                # Ticket de desk sin ejecución: igualmente deriva (el worker
                # aplica las reglas sin espejo: solo rellena huecos).
                self._enqueue_visibility(None, str(business_name).strip(), "webhook:untracked")
            return {"ok": False, "status": "not_found"}

        target = _ERPNEXT_BUSINESS_TO_TASK_STATUS.get(str(business_status or "").strip())
        if deleted:
            target = TaskStatus.CANCELLED.value
        exec_id = str(task.get("task_id") or "")
        if target is None:
            # El relink se decide con el retorno del sync: la fusión del
            # espejo muta el dict anidado, así que tras sincronizar ya no se
            # puede comparar contra el valor previo.
            if await self._sync_webhook_parent(task, parent_task):
                self._enqueue_visibility(
                    exec_id or None, self._webhook_name(task), "webhook:relink"
                )
            else:
                self._enqueue_webhook_ignored(task, business_status)
            return {"ok": True, "status": "ignored", "task_id": task.get("task_id")}
        if str(task.get("status") or "") == target:
            if await self._sync_webhook_parent(task, parent_task):
                self._enqueue_visibility(exec_id or None, self._webhook_name(task), "webhook:relink")
            return {"ok": True, "status": "already_applied", "task_id": task.get("task_id")}

        params = self._task_params(task)
        mirror = {
            "business_status": "Deleted" if deleted else str(business_status or ""),
            "business_synced_at": datetime.now(timezone.utc).isoformat(),
            # Guardia anti-eco: el observador compara contra este campo.
            "status_pushed": target,
        }
        if parent_task is not _WEBHOOK_PARENT_UNSET:
            live_parent = str(parent_task or "").strip() or None
            if live_parent != self._visibility_mirror(task).get("parent_task"):
                mirror["parent_task"] = live_parent
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
        self._enqueue_visibility(exec_id or None, self._webhook_name(task), "webhook:applied")
        result: dict[str, Any] = {
            "ok": True,
            "status": "applied",
            "task_id": task.get("task_id"),
            "task_status": target,
        }
        # Desk answer to an owner question (TASK-2026-00206): closing the
        # question approves, cancelling/deleting it denies. The status
        # above already landed with its anti-echo guard; here the decision
        # is recorded and the origin continues.
        if is_owner_question(task) and target in {
            TaskStatus.COMPLETED.value,
            TaskStatus.CANCELLED.value,
        }:
            try:
                result["owner_answer"] = await self._continue_answered_question(
                    str(task.get("task_id") or ""),
                    approve=target == TaskStatus.COMPLETED.value,
                    actor_id="erpnext-desk",
                )
            except Exception as exc:
                logger.warning(
                    "Owner-answer continuation failed for %s",
                    task.get("task_id"), exc_info=True)
                result["owner_answer"] = {"continued": False,
                                          "reason": str(exc)[:200]}
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

    @staticmethod
    def _webhook_name(task: dict[str, Any]) -> str:
        mirror: dict[str, Any] = {}
        params = task.get("params")
        if isinstance(params, dict):
            mirrors = params.get("_mirrors")
            if isinstance(mirrors, dict) and isinstance(mirrors.get("erpnext"), dict):
                mirror = mirrors["erpnext"]
        return str(mirror.get("name") or "").strip()

    async def _sync_webhook_parent(self, task: dict[str, Any], parent_task: Any) -> bool:
        """Sincroniza el padre del espejo con el doc del webhook. True si cambió."""
        if parent_task is _WEBHOOK_PARENT_UNSET:
            return False
        live_parent = str(parent_task or "").strip() or None
        if live_parent == self._visibility_mirror(task).get("parent_task"):
            return False
        merged = self._merge_erpnext_mirror_params(
            self._task_params(task), {"parent_task": live_parent}
        )
        await self.patch_task(str(task.get("task_id") or ""), {"params": merged})
        return True

    def _enqueue_webhook_ignored(
        self, task: dict[str, Any], business_status: str | None
    ) -> None:
        """Decide si un webhook `ignored` merece job de visibilidad (00147).

        Sin lecturas extra: Working manual (la ejecución no lo empezó) o
        reapertura manual a Open (la ejecución sigue cerrada). El eco de
        nuestros PUTs no cumple ninguna. El relink lo decide quien llama con
        el retorno de `_sync_webhook_parent` (ver comentario allí).
        """
        exec_id = str(task.get("task_id") or "") or None
        name = self._webhook_name(task)
        if not name:
            return
        status = str(business_status or "").strip()
        exec_status = str(task.get("status") or "")
        if status == "Working" and (
            exec_status != TaskStatus.IN_PROGRESS.value or task.get("started_at") is None
        ):
            self._enqueue_visibility(exec_id, name, "webhook:manual")
        elif status == "Open" and exec_status in (
            TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value,
        ):
            self._enqueue_visibility(exec_id, name, "webhook:manual")

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
        # Huella temporal real de la sesión (00147): los tickets que cierran
        # las sesiones por transición no pasaban por el runtime y Mongo se
        # quedaba sin inicio ni fin. El inicio se fija una vez; la reapertura
        # limpia el fin.
        moment = datetime.now(timezone.utc)
        if target == "Working" and task.get("started_at") is None:
            updates["started_at"] = moment
        elif target == "Completed":
            updates["completed_at"] = moment
        elif target == "Open":
            updates["completed_at"] = None
        elif target == "Cancelled" and task.get("completed_at") is None:
            updates["completed_at"] = moment
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

        try:
            transition_dates = visibility.work_dates_for_transition(
                target=target,
                zone=self._site_zone_name(),
                now=datetime.now(timezone.utc),
                explicit_due=self._visibility_mirror(task).get("due_date_explicit"),
            ) or None
        except Exception:
            logger.warning(
                "No se pudieron calcular las fechas de la transición %s", name,
                exc_info=True,
            )
            transition_dates = None
        span_extended = False
        delay = 1.0
        for attempt in range(3):
            try:
                try:
                    await adapter.transition_status(
                        name, target, work_dates=transition_dates
                    )
                except ERPNextDateBoundError:
                    # Como en `_push_status`: extender ancestros y reintentar
                    # una vez; si vuelve a fallar es error duro.
                    if span_extended or not transition_dates:
                        raise
                    await self._ensure_span_for_child(
                        adapter,
                        name,
                        start=transition_dates.get("exp_start_date"),
                        end=transition_dates.get("exp_end_date"),
                    )
                    span_extended = True
                    await adapter.transition_status(
                        name, target, work_dates=transition_dates
                    )
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
        self._enqueue_visibility(
            str((task or {}).get("task_id") or "") or None, name, f"transition:{target}"
        )
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
        (reglas permanentes de AGENTS.md).
        """
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not adapter.is_enabled:
            return 0

        pending = await self._repo.list_tasks_needing_erpnext_sync(limit=200)
        queue = getattr(self, "_erpnext_queue", None)
        repaired = 0
        if pending:
            logger.info("Startup reconcile ERPNext: %d tickets con enlace roto", len(pending))
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

        # Visibilidad pendiente (00146 + 00147): valores derivados que quedaron
        # sin recalcular con ERPNext caído. Consulta separada para no tocar el
        # pin de `list_tasks_needing_erpnext_sync`.
        try:
            list_pending = getattr(self._repo, "list_tasks_pending_visibility", None)
            vis_pending = await list_pending(limit=200) if list_pending else []
        except Exception:
            logger.warning("No se pudo listar la visibilidad pendiente", exc_info=True)
            vis_pending = []
        vis_repaired = 0
        for task in vis_pending or []:
            mirror = self._visibility_mirror(task if isinstance(task, dict) else None)
            flagged = str(mirror.get("pending_visibility") or "").strip()
            name = str(mirror.get("name") or "").strip() or flagged
            if not name:
                continue
            self._enqueue_visibility(
                str((task or {}).get("task_id") or "") or None, name, "reconcile"
            )
            vis_repaired += 1
        if vis_repaired:
            logger.info("Startup reconcile visibilidad: %d reencolados", vis_repaired)
        return repaired + vis_repaired

    # --- Visibilidad: % de grupos y fechas Gantt (00146 + 00147) -----------
    #
    # Valores DERIVADOS del estado de negocio (nunca backlog): % del grupo
    # desde sus hijos, fechas reales al empezar/cerrar, estimaciones
    # deterministas para lo abierto y span del grupo. Se recalculan por
    # evento vía jobs `kind=visibility` de la cola ERPNext; todo es
    # compare-before-write (sin cambios no hay PUT: el eco no escribe).
    # ERPNext caído → `pending_visibility` en el espejo (reconciliación o
    # siguiente evento). Cero LLM, cero polling, cero barridos.

    def _site_zone_name(self) -> str | None:
        settings = getattr(self, "_settings", None)
        return getattr(settings, "ERPNEXT_SITE_TIMEZONE", None)

    @staticmethod
    def _visibility_mirror(execution: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(execution, dict):
            return {}
        params = execution.get("params")
        mirrors = params.get("_mirrors") if isinstance(params, dict) else None
        mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
        return dict(mirror) if isinstance(mirror, dict) else {}

    async def _patch_visibility_state(
        self,
        task_id: str | None,
        execution: dict[str, Any] | None,
        *,
        mirror: dict[str, Any] | None = None,
        top: dict[str, Any] | None = None,
    ) -> None:
        """Parche de ejecución para visibilidad. Nunca lanza."""
        repo = getattr(self, "_repo", None)
        if repo is None or not task_id:
            return
        task = execution
        if not isinstance(task, dict):
            try:
                task = await repo.get_task(str(task_id))
            except Exception:
                logger.warning(
                    "No se pudo leer %s para visibilidad", task_id, exc_info=True
                )
                return
        if not isinstance(task, dict):
            return
        updates: dict[str, Any] = {}
        if top:
            updates.update(top)
        if mirror:
            updates["params"] = self._merge_erpnext_mirror_params(
                self._task_params(task), dict(mirror)
            )
        if not updates:
            return
        try:
            # Sin `status`: no dispara al observador (solo espejo/fechas).
            await repo.patch_task(str(task_id), updates)
        except Exception:
            logger.warning(
                "No se pudo guardar la visibilidad de %s", task_id, exc_info=True
            )

    async def _refresh_visibility(self, item: dict[str, Any]) -> dict[str, Any]:
        """Worker `kind=visibility`: recalcula lo derivado de un ticket."""
        adapter = getattr(self, "_erpnext", None)
        if adapter is None or not getattr(adapter, "is_enabled", False):
            return {"ok": False, "status": "no_adapter"}
        name = str(item.get("name") or "").strip()
        if not name:
            return {"ok": False, "status": "no_name"}
        task_id = str(item.get("task_id") or "").strip() or None
        reason = str(item.get("reason") or "")
        try:
            try:
                business = await adapter.resolve_task(name)
            except ERPNextValidationError as exc:
                if exc.status_code == 404 and task_id:
                    return await self._rollup_orphan_parent(task_id)
                raise
            execution: dict[str, Any] | None = None
            repo = getattr(self, "_repo", None)
            if repo is not None and task_id:
                try:
                    execution = await repo.get_task(task_id)
                except Exception:
                    logger.warning(
                        "No se pudo leer %s para visibilidad", task_id, exc_info=True
                    )
                    execution = None
            zone = self._site_zone_name()
            now = datetime.now(timezone.utc)
            wrote_own = await self._ensure_own_dates(
                adapter, business, execution, zone, now, reason
            )
            rolled = await self._rollup_chain_for(adapter, business)
            dependents = 0
            if str(business.get("status") or "").strip() == "Completed":
                dependents = await self._reestimate_dependents(
                    adapter, business, execution, zone, now
                )
            if task_id:
                await self._patch_visibility_state(
                    task_id, execution, mirror={"pending_visibility": None}
                )
            return {
                "ok": True,
                "own_dates": wrote_own,
                "rolled": rolled,
                "dependents": dependents,
            }
        except ERPNextUnavailable as exc:
            logger.warning(
                "Visibilidad de %s pendiente: ERPNext caído (%s)", name, exc
            )
            if task_id:
                await self._patch_visibility_state(
                    task_id, None, mirror={"pending_visibility": name}
                )
            return {"ok": False, "status": "unavailable"}
        except ERPNextError as exc:
            logger.warning("Visibilidad de %s sin recalcular: %s", name, exc)
            return {"ok": False, "status": "business_error"}

    async def _rollup_orphan_parent(self, task_id: str) -> dict[str, Any]:
        """El hijo ya no existe (borrado en desk): rollup del padre que
        recuerda el espejo. Sin padre conocido no hay nada que derivar."""
        adapter = getattr(self, "_erpnext", None)
        repo = getattr(self, "_repo", None)
        if adapter is None or repo is None:
            return {"ok": False, "status": "no_adapter"}
        try:
            execution = await repo.get_task(task_id)
        except Exception:
            return {"ok": False, "status": "no_execution"}
        parent = str(self._visibility_mirror(execution).get("parent_task") or "")
        parent = parent.strip()
        if not parent:
            return {"ok": False, "status": "no_parent"}
        rolled = await self._rollup_chain(adapter, parent)
        return {"ok": True, "orphan_parent": parent, "rolled": rolled}

    async def _ensure_own_dates(
        self,
        adapter: Any,
        business: dict[str, Any],
        execution: dict[str, Any] | None,
        zone: str | None,
        now: datetime,
        reason: str,
    ) -> bool:
        """Fechas propias de una hoja. True si escribió. Grupos: no (derivan).

        Completed sin fin/done → sello tardío real. Working sin inicio →
        sello tardío real. Open y Pending Review (trabajo abierto) →
        estimación (ver `_estimate_open_ticket`). Cancelled/otros → solo
        adopción de explícita en el espejo. Las transiciones manuales del
        desk también dejan su huella temporal en la ejecución
        (started_at/completed_at) cuando falta.
        """
        if business.get("is_group"):
            return False
        status = str(business.get("status") or "").strip()
        if status not in ("Open", "Working", "Completed", "Pending Review"):
            await self._adopt_due_only(adapter, business, execution)
            return False
        name = str(business.get("name") or "").strip()
        mirror = self._visibility_mirror(execution)
        live_start = business.get("exp_start_date")
        live_end = business.get("exp_end_date")
        task_id = str((execution or {}).get("task_id") or "") or None

        if status in ("Open", "Pending Review"):
            return await self._estimate_open_ticket(
                adapter, business, execution, zone, now, reason
            )

        dates: dict[str, Any] = {}
        top: dict[str, Any] = {}
        # Mejor inicio conocido: started_at, creation del negocio (Frappe
        # siempre la pone) y, en última instancia, now.
        created_site = visibility.parse_site_datetime(business.get("creation"))
        created_str = (
            created_site.strftime(visibility.SITE_DATETIME_FORMAT)
            if created_site is not None else None
        )
        started = visibility.as_utc_datetime((execution or {}).get("started_at"))
        start_value = (
            visibility.to_site_datetime(started, zone) if started is not None
            else created_str if created_str is not None
            else visibility.to_site_datetime(now, zone)
        )
        if status == "Working" and visibility.parse_site_datetime(live_start) is None:
            dates["exp_start_date"] = start_value
            if task_id and started is None:
                top["started_at"] = (
                    visibility.site_datetime_to_utc(created_str, zone) or now
                    if created_str is not None else now
                )
            if business.get("progress") not in (None, 0, 0.0):
                # Normaliza el 100 pegado de una reapertura; entre
                # transiciones el % manual se respeta (aquí no se toca).
                dates["progress"] = 0
        if status == "Completed":
            if visibility.parse_site_datetime(live_start) is None:
                dates["exp_start_date"] = start_value
                if task_id and started is None and "started_at" not in top:
                    top["started_at"] = (
                        visibility.site_datetime_to_utc(created_str, zone) or now
                        if created_str is not None else now
                    )
            if visibility.parse_site_datetime(live_end) is None:
                done = visibility.as_utc_datetime((execution or {}).get("completed_at"))
                dates["exp_end_date"] = visibility.to_site_datetime(done or now, zone)
                if task_id and done is None:
                    top["completed_at"] = now
            if visibility.parse_site_day(business.get("completed_on")) is None:
                done_day = visibility.as_utc_datetime((execution or {}).get("completed_at"))
                dates["completed_on"] = visibility.to_site_date(done_day or now, zone)

        # La ejecución adopta las fechas manuales del desk cuando le faltan.
        if task_id and isinstance(execution, dict):
            if status == "Working" and execution.get("started_at") is None:
                adopted = visibility.site_datetime_to_utc(live_start, zone)
                if adopted is not None:
                    top["started_at"] = adopted
            if status == "Completed" and execution.get("completed_at") is None:
                adopted = visibility.site_datetime_to_utc(live_end, zone)
                if adopted is not None:
                    top["completed_at"] = adopted

        mirror_updates: dict[str, Any] = {}
        if status == "Completed":
            if mirror.get("exp_end_estimated") is not None:
                # Consumida: el fin ya es real, no estimado.
                mirror_updates["exp_end_estimated"] = None
            # El fin real queda registrado: si el ticket se reabre, el vivo
            # coincide con él y se trata como obsoleto. Aquí NO se adopta
            # explícita (el vivo es real por definición; la explícita se
            # adoptó mientras estaba abierto).
            real_end = visibility.parse_site_datetime(
                dates.get("exp_end_date", live_end)
            )
            if real_end is not None:
                real_str = real_end.strftime(visibility.SITE_DATETIME_FORMAT)
                if real_str != mirror.get("exp_end_real"):
                    mirror_updates["exp_end_real"] = real_str
        else:
            explicit = visibility.adopt_explicit_due(
                live_end,
                mirror.get("exp_end_estimated"),
                mirror.get("due_date_explicit"),
            )
            if explicit != mirror.get("due_date_explicit"):
                mirror_updates["due_date_explicit"] = explicit

        if dates:
            await self._put_work_dates(adapter, name, dates)
        if task_id and (mirror_updates or top):
            await self._patch_visibility_state(
                task_id, execution,
                mirror=mirror_updates or None, top=top or None,
            )
        return bool(dates)

    async def _adopt_due_only(
        self,
        adapter: Any,
        business: dict[str, Any],
        execution: dict[str, Any] | None,
    ) -> None:
        """Solo registra una explícita adoptada (estados sin semántica de fecha)."""
        del adapter
        if not isinstance(execution, dict):
            return
        task_id = str(execution.get("task_id") or "") or None
        if not task_id:
            return
        mirror = self._visibility_mirror(execution)
        explicit = visibility.adopt_explicit_due(
            business.get("exp_end_date"),
            mirror.get("exp_end_estimated"),
            mirror.get("due_date_explicit"),
        )
        if explicit != mirror.get("due_date_explicit"):
            await self._patch_visibility_state(
                task_id, execution, mirror={"due_date_explicit": explicit}
            )

    async def _put_work_dates(
        self, adapter: Any, name: str, dates: dict[str, Any]
    ) -> None:
        """PUT de fechas con extender-primero ante DateBound (un reintento)."""
        try:
            await adapter.update_work_dates(name, dates)
        except ERPNextDateBoundError:
            await self._ensure_span_for_child(
                adapter,
                name,
                start=dates.get("exp_start_date"),
                end=dates.get("exp_end_date"),
            )
            await adapter.update_work_dates(name, dates)

    async def _estimate_open_ticket(
        self,
        adapter: Any,
        business: dict[str, Any],
        execution: dict[str, Any] | None,
        zone: str | None,
        now: datetime,
        reason: str,
    ) -> bool:
        """Estima una hoja Open. True si escribió. Nunca pisa reales,
        manuales ni explícitas; sin ejecución solo rellena huecos."""
        name = str(business.get("name") or "").strip()
        mirror = self._visibility_mirror(execution)
        live_start = business.get("exp_start_date")
        live_end = business.get("exp_end_date")
        rec_start = mirror.get("exp_start_estimated")
        rec_end = mirror.get("exp_end_estimated")

        # Fin real obsoleto (reapertura): el vivo coincide con el fin que se
        # registró al cerrar. Se trata como hueco, no como explícita.
        if visibility.parse_site_datetime(
            live_end
        ) is not None and self._same_site_stamp(live_end, mirror.get("exp_end_real")):
            live_end = None
        explicit = visibility.adopt_explicit_due(
            live_end, rec_end, mirror.get("due_date_explicit")
        )

        start_set = visibility.parse_site_datetime(live_start) is not None
        end_set = visibility.parse_site_datetime(live_end) is not None
        start_is_ours = start_set and self._same_site_stamp(live_start, rec_start)
        end_is_ours = end_set and self._same_site_stamp(live_end, rec_end)
        end_is_explicit = end_set and self._same_site_stamp(live_end, explicit)
        stale_done = visibility.parse_site_day(business.get("completed_on")) is not None

        recompute = reason in (
            "create", "transition:Open", "webhook:manual", "reconcile", "dep_closed",
        )
        if start_set and not start_is_ours and rec_start is not None:
            # Manual: los marcadores propios quedaron viejos.
            rec_start = None
        if end_set and not end_is_ours and not end_is_explicit and rec_end is not None:
            rec_end = None
        needs_start = not start_set or (start_is_ours and recompute)
        needs_end = (not end_set or (end_is_ours and recompute)) and not end_is_explicit
        if not needs_start and not needs_end and not stale_done:
            if explicit != mirror.get("due_date_explicit") and execution is not None:
                task_id = str(execution.get("task_id") or "") or None
                if task_id:
                    await self._patch_visibility_state(
                        task_id, execution, mirror={"due_date_explicit": explicit}
                    )
            return False

        dates: dict[str, Any] = {}
        if stale_done:
            dates["completed_on"] = None
        end_is_restore = False
        if needs_start or needs_end:
            tier = (execution or {}).get("coding_tool") or business.get(
                "openceo_coding_tool"
            )
            dep_ends = await self._dependency_ends(adapter, business)
            medians: dict[str | None, int] = {}
            try:
                rows = await adapter.list_closed_with_dates(
                    str(business.get("project") or ""),
                    limit=visibility.MAX_HISTORY_ROWS,
                )
                medians = visibility.median_durations(rows or [])
            except ERPNextUnavailable:
                raise
            except ERPNextError as exc:
                logger.warning("Sin historial para estimar %s: %s", name, exc)
                medians = {}
            today = now.astimezone(visibility.site_zone(zone)).date()
            want_start, want_end = visibility.estimate_window(
                today=today, tier=tier, dep_ends=dep_ends, medians=medians
            )
            if needs_start:
                dates["exp_start_date"] = want_start
            if needs_end:
                # Con compromiso registrado y fin vacío se restaura la
                # explícita (no es estimación: no se registra como tal);
                # si no, la estimación fresca.
                if explicit is not None:
                    dates["exp_end_date"] = explicit
                    end_is_restore = True
                else:
                    dates["exp_end_date"] = want_end
            if explicit is not None and "exp_start_date" in dates:
                # El inicio estimado nunca pasa la explícita (el formato
                # canónico ordena lexicográficamente igual que cronológico).
                if dates["exp_start_date"] > explicit:
                    dates["exp_start_date"] = explicit
            if business.get("progress") not in (None, 0, 0.0):
                dates["progress"] = 0

        await self._put_work_dates(adapter, name, dates)
        if isinstance(execution, dict):
            task_id = str(execution.get("task_id") or "") or None
            if task_id:
                updates: dict[str, Any] = {"due_date_explicit": explicit}
                if "exp_start_date" in dates:
                    updates["exp_start_estimated"] = dates["exp_start_date"]
                elif rec_start is None and mirror.get("exp_start_estimated") is not None:
                    updates["exp_start_estimated"] = None
                if "exp_end_date" in dates:
                    updates["exp_end_estimated"] = (
                        None if end_is_restore else dates["exp_end_date"]
                    )
                elif rec_end is None and mirror.get("exp_end_estimated") is not None:
                    updates["exp_end_estimated"] = None
                await self._patch_visibility_state(task_id, execution, mirror=updates)
        return True

    @staticmethod
    def _same_site_stamp(live: Any, recorded: Any) -> bool:
        live_dt = visibility.parse_site_datetime(live)
        rec_dt = visibility.parse_site_datetime(recorded)
        return live_dt is not None and rec_dt is not None and live_dt == rec_dt

    async def _dependency_ends(
        self, adapter: Any, business: dict[str, Any]
    ) -> list[Any]:
        """Fines de los depends_on (GET acotado por dependencia)."""
        names = depends_on_names(business) or []
        ends: list[Any] = []
        for dep in names[: visibility.MAX_DEPENDENT_RESOLVE]:
            try:
                doc = await adapter.resolve_task(dep)
            except ERPNextUnavailable:
                raise
            except ERPNextError:
                continue
            if isinstance(doc, dict) and doc.get("exp_end_date"):
                ends.append(doc.get("exp_end_date"))
        if len(names) > visibility.MAX_DEPENDENT_RESOLVE:
            logger.warning(
                "Estimación de %s con depends truncados (%d)",
                business.get("name"), len(names),
            )
        return ends

    async def _rollup_chain_for(
        self, adapter: Any, business: dict[str, Any]
    ) -> list[str]:
        """Rollup desde el sujeto (si es grupo) o desde su padre."""
        if business.get("is_group"):
            start = str(business.get("name") or "").strip()
        else:
            start = str(business.get("parent_task") or "").strip()
        if not start:
            return []
        return await self._rollup_chain(adapter, start)

    async def _rollup_chain(self, adapter: Any, start_name: str) -> list[str]:
        """Propaga % + span hacia arriba, un PUT por padre. Tope de fondo."""
        touched: list[str] = []
        current = str(start_name or "").strip()
        depth = 0
        while current and depth < visibility.MAX_ROLLUP_DEPTH:
            try:
                changed, parent = await self._rollup_single(adapter, current)
            except ERPNextValidationError as exc:
                if exc.status_code == 404:
                    break
                raise
            if changed:
                touched.append(current)
            current = str(parent or "").strip()
            depth += 1
        if current:
            logger.warning("Rollup truncado por fondo en %s", current)
        return touched

    async def _rollup_single(self, adapter: Any, name: str) -> tuple[bool, str | None]:
        """Recalcula un grupo. Devuelve (escribió, padre)."""
        business = await adapter.resolve_task(name)
        parent = str(business.get("parent_task") or "").strip() or None
        children = await adapter.list_children(
            name, limit=visibility.MAX_CHILDREN_ROLLUP
        )
        explicit: str | None = None
        try:
            execution = await self.find_task_for_business_ref(
                task_id=str(business.get("openceo_task_id") or "").strip() or None,
                ref=str(business.get("openceo_ref") or "").strip() or None,
            )
        except Exception:
            execution = None
        if isinstance(execution, dict):
            raw = self._visibility_mirror(execution).get("due_date_explicit")
            parsed = visibility.parse_site_datetime(raw)
            explicit = parsed.strftime(visibility.SITE_DATETIME_FORMAT) if parsed else None
        progress = visibility.group_progress(children or [])
        start, end = visibility.group_span(children or [], explicit)
        desired: dict[str, Any] = {}
        try:
            live_progress = round(float(business.get("progress") or 0.0), 2)
        except (TypeError, ValueError):
            live_progress = 0.0
        if live_progress != progress:
            desired["progress"] = progress
        live_start = visibility.parse_site_datetime(business.get("exp_start_date"))
        live_start_str = (
            live_start.strftime(visibility.SITE_DATETIME_FORMAT) if live_start else None
        )
        if live_start_str != start:
            desired["exp_start_date"] = start
        live_end = visibility.parse_site_datetime(business.get("exp_end_date"))
        live_end_str = (
            live_end.strftime(visibility.SITE_DATETIME_FORMAT) if live_end else None
        )
        if live_end_str != end:
            desired["exp_end_date"] = end
        if not desired:
            return False, parent
        try:
            await adapter.update_group_rollup(name, desired)
        except ERPNextDateBoundError:
            # El grupo pasa a SU padre: extender arriba y reintentar una vez.
            await self._ensure_span_for_child(
                adapter, name,
                start=desired.get("exp_start_date"),
                end=desired.get("exp_end_date"),
            )
            await adapter.update_group_rollup(name, desired)
        return True, parent

    async def _ensure_span_for_child(
        self, adapter: Any, name: str, *, start: Any, end: Any
    ) -> None:
        """Extiende los ancestros para abarcar [start, end]. Nunca estrecha.

        Dos pasadas: primero sube leyendo la cadena, luego aplica de arriba
        abajo para que cada PUT respete a su (ya extendido) padre. El padre
        corto entra siempre, así que extender no rebota.
        """
        want_start = visibility.parse_site_datetime(start)
        want_end = visibility.parse_site_datetime(end)
        if want_start is None and want_end is None:
            return
        try:
            child = await adapter.resolve_task(name)
        except ERPNextError:
            return
        chain: list[tuple[str, Any, Any]] = []
        current = str(child.get("parent_task") or "").strip()
        while current and len(chain) < visibility.MAX_ROLLUP_DEPTH:
            try:
                doc = await adapter.resolve_task(current)
            except ERPNextError:
                break
            chain.append((
                current,
                visibility.parse_site_datetime(doc.get("exp_start_date")),
                visibility.parse_site_datetime(doc.get("exp_end_date")),
            ))
            current = str(doc.get("parent_task") or "").strip()
        # Wants de abajo arriba, aplicación de arriba abajo.
        levels: list[tuple[str, Any, Any, Any, Any]] = []
        for level_name, live_start, live_end in chain:
            want_start = min(
                [w for w in (want_start, live_start) if w is not None],
                default=None,
            )
            want_end = max(
                [w for w in (want_end, live_end) if w is not None],
                default=None,
            )
            levels.append((level_name, live_start, live_end, want_start, want_end))
        for level_name, live_start, live_end, want_s, want_e in reversed(levels):
            need: dict[str, Any] = {}
            # Solo se extienden límites PRESENTES: contra un None Frappe no
            # acota (los hijos con fechas bajo padres sin fechas lo prueban
            # en vivo) y escribirlo podría invertir un span parcial. El
            # rollup posterior fija los spans exactos.
            if want_s is not None and live_start is not None and want_s < live_start:
                need["exp_start_date"] = want_s.strftime(visibility.SITE_DATETIME_FORMAT)
            if want_e is not None and live_end is not None and want_e > live_end:
                need["exp_end_date"] = want_e.strftime(visibility.SITE_DATETIME_FORMAT)
            if need:
                await adapter.update_group_rollup(level_name, need)

    async def _reestimate_dependents(
        self,
        adapter: Any,
        business: dict[str, Any],
        execution: dict[str, Any] | None,
        zone: str | None,
        now: datetime,
    ) -> int:
        """Al cerrar, re-estima los abiertos que dependen del sujeto.

        El `list` no devuelve child tables (`depends_on` solo viaja en el
        GET completo), así que es una lista de abiertos + GET acotado por
        candidato. Sin ejecución (sin project_key) no hay por dónde
        listar: se omite.
        """
        project_key = str((execution or {}).get("project") or "").strip()
        if not project_key:
            return 0
        subject = str(business.get("name") or "").strip()
        try:
            rows = await adapter.search_tickets(
                status="Open", project_key=project_key,
                limit=visibility.MAX_DEPENDENT_RESOLVE,
            )
        except ERPNextUnavailable:
            raise
        except ERPNextError as exc:
            logger.warning("Sin re-estimación tras %s: %s", subject, exc)
            return 0
        count = 0
        for row in (rows or [])[: visibility.MAX_DEPENDENT_RESOLVE]:
            candidate = str(row.get("name") or "").strip()
            if not candidate or candidate == subject:
                continue
            try:
                doc = await adapter.resolve_task(candidate)
            except ERPNextUnavailable:
                raise
            except ERPNextError:
                continue
            if not isinstance(doc, dict):
                continue
            if str(doc.get("status") or "").strip() != "Open":
                continue
            if subject not in (depends_on_names(doc) or []):
                continue
            try:
                dep_execution = await self.find_task_for_business_ref(
                    task_id=str(doc.get("openceo_task_id") or "").strip() or None,
                    ref=str(doc.get("openceo_ref") or "").strip() or None,
                )
            except Exception:
                dep_execution = None
            try:
                if await self._estimate_open_ticket(
                    adapter, doc, dep_execution, zone, now, "dep_closed"
                ):
                    count += 1
            except ERPNextUnavailable:
                raise
            except ERPNextError as exc:
                logger.warning("Sin re-estimar %s: %s", candidate, exc)
                continue
        return count

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

    async def _plan_pedido_pre_stage(
        self, task: dict[str, Any], *, force: bool,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Plan a pedido before execution (TASK-2026-00204).

        Returns ``(task_for_run, None)`` to proceed, or ``(task, stop)``
        with the ticket to return instead of running. Explicit tickets
        proceed untouched; a decided pedido refreshes from the patched
        decision, holds on declared spend, and re-queues for decided
        A/B/S tiers so the dispatcher re-admits with the exact tier.
        Unplannable pedidos (unwired service, broken env) fail fast:
        proceeding undecided would strand in a wake-less wait.
        """
        task_id = str(task.get("task_id") or "")
        if not is_pedido(task):
            return task, None
        service = getattr(self, "_objective_service", None)
        if service is None:
            return task, await self._stop_pedido_failed(
                task_id, "planner unavailable: no objective service wired")
        if force:
            params = dict(task.get("params") or {})
            if params.pop("planner_attempts", None) is not None:
                task = await self._repo.patch_task(
                    task_id, {"params": params}) or task
        try:
            env = service.env_for(str(task.get("project") or ""))
        except Exception as exc:
            logger.warning("pedido env failed for %s", task_id, exc_info=True)
            return task, await self._stop_pedido_failed(
                task_id, f"planner unavailable: {exc}")
        try:
            out = await engine_mod.plan_pedido(env, task)
        except Exception:
            logger.warning("pedido planner crashed for %s", task_id,
                           exc_info=True)
            out = {"ok": False, "reason": "planner_error",
                   "detail": "planner run crashed"}
        if out.get("ok") and out.get("action") == "decided":
            return await self._plan_pedido_decided(task, out)
        if out.get("ok") and out.get("action") == "convert":
            return task, await self._plan_pedido_convert(task, env, out)
        if out.get("reason") == "config":
            return task, await self._stop_pedido_failed(
                task_id, f"planner unavailable: {out.get('detail')}")
        return task, await self._plan_pedido_backoff(task, out)

    async def _hold_decided_spend(
        self, task: dict[str, Any], costs: Any, spend: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Hold a decided pedido with escalating spend; None on decline.

        The base helper only holds queued/waiting tickets, but at this
        point the dispatcher already claimed the ticket (in_progress)
        or it failed (return replan), so revert it to queued first.
        A decline (autorun-off) leaves it queued for its caller's
        normal path; the position is never lost.
        """
        if not spend.get("escalate"):
            return None
        task_id = str(task.get("task_id") or "")
        if str(task.get("status") or "") != TaskStatus.QUEUED.value:
            reverted = await self._repo.patch_task(
                task_id, {"status": TaskStatus.QUEUED.value})
            if reverted is not None:
                task = reverted
        return await self._hold_loose_spend(task, costs, spend=spend)

    async def _plan_pedido_decided(
        self, task: dict[str, Any], out: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Refresh a decided pedido; hold on spend, re-queue for A/B/S."""
        task_id = str(task.get("task_id") or "")
        decision = out.get("decision") or {}
        refreshed = await self._repo.get_task(task_id) or task
        spend = out.get("spend") or {}
        held = await self._hold_decided_spend(
            refreshed, decision.get("costs"), spend)
        if held is not None:
            return refreshed, held
        tier = str(decision.get("coding_tool") or "")
        if tier in {"tier_s", "tier_a", "tier_b"}:
            # The dispatch admitted this pedido tier-unaware; stop here
            # (decision kept) so the next cycle re-admits with the exact
            # tier: full concurrency, quota and reserve semantics, no
            # partial re-check. The ticket arrives claimed
            # (in_progress): explicitly requeue, or it would strand.
            stopped = await self._repo.patch_task(
                task_id, {"status": TaskStatus.QUEUED.value})
            return refreshed, stopped or refreshed
        return refreshed, None

    async def _plan_pedido_convert(
        self, task: dict[str, Any], env: Any,
        out: dict[str, Any],
    ) -> dict[str, Any]:
        """Convert a pedido that outgrows one ticket; stop the run."""
        task_id = str(task.get("task_id") or "")
        try:
            # convert_ticket plans the first horizon internally; no
            # outer plan_objective (a second cycle would double-spend
            # and could duplicate children).
            objective = await engine_mod.convert_ticket(
                env, task, "planner pedido conversion",
                sizing_text=str(out.get("state_summary") or ""))
            objective_id = str(objective.get("objective_id") or "")
        except Exception as exc:
            logger.warning("pedido convert failed for %s", task_id,
                           exc_info=True)
            return await self._stop_pedido_failed(
                task_id, f"pedido conversion failed: {exc}")
        try:
            note = (f"Planner converts to objective {objective_id}: "
                    f"{str(out.get('state_summary') or '')[:300]}")
            costs = out.get("costs") or []
            if costs:
                # Objective-level spend on a convert is speculative (no
                # tickets exist yet); the objective plans re-declare and
                # gate it, but the declaration must not vanish silently.
                bits = ", ".join(
                    f"{entry.get('label') or '?'} "
                    f"({entry.get('amount') or '?'})"
                    for entry in costs if isinstance(entry, dict))
                note += (" Declared spend (re-gated by objective plans): "
                         f"{bits[:300]}.")
            await self.comment_on_ticket(task_id, note)
        except Exception:
            logger.warning("pedido convert comment failed for %s", task_id,
                           exc_info=True)
        refreshed = await self._repo.get_task(task_id)
        return refreshed or task

    async def _plan_pedido_backoff(
        self, task: dict[str, Any], out: dict[str, Any],
    ) -> dict[str, Any]:
        """Back off a failed pedido plan; fail terminally past 3 tries."""
        task_id = str(task.get("task_id") or "")
        refreshed = await self._repo.get_task(task_id) or task
        params = refreshed.get("params") \
            if isinstance(refreshed.get("params"), dict) else {}
        attempts = int(params.get("planner_attempts") or 0)
        if out.get("reason") == "planner_error" and out.get("detail") == \
                "planner run crashed":
            attempts += 1
            await self._repo.patch_task(
                task_id, {"params": {**params, "planner_attempts": attempts}})
        detail = str(out.get("detail") or "planner_error")
        if attempts >= 3:
            return await self._stop_pedido_failed(
                task_id, f"planner failed {attempts}x: {detail[:300]}")
        backoff = (5, 15, 30)[min(max(attempts, 1), 3) - 1]
        parked = await self._repo.patch_task(task_id, {
            "status": TaskStatus.WAITING.value,
            "waiting_reason": f"planner_error: {detail[:200]}",
            "resume_at": datetime.now(timezone.utc)
            + timedelta(minutes=backoff),
        })
        try:
            await self.comment_on_ticket(
                task_id,
                f"Planner failed (attempt {max(attempts, 1)}/3, retry in "
                f"{backoff}m): {detail[:300]}")
        except Exception:
            logger.warning("pedido backoff comment failed for %s", task_id,
                           exc_info=True)
        return parked or refreshed

    async def _stop_pedido_failed(
        self, task_id: str, detail: str,
    ) -> dict[str, Any]:
        """Fail a pedido whose planning cannot proceed. Mirrors a run crash."""
        failed = await self._repo.patch_task(task_id, {
            "status": TaskStatus.FAILED.value,
            "error": detail[:500],
            "waiting_reason": None,
            "resume_at": None,
            "completed_at": datetime.now(timezone.utc),
        })
        if failed is not None:
            await self._hub.broadcast("task_updated", failed)
            asyncio.create_task(self._fire_notion_sync_back(failed, "failed"))
            asyncio.create_task(self._revoke_ticket_badges(failed))
        try:
            await self.comment_on_ticket(task_id, detail[:500])
        except Exception:
            logger.warning("pedido failure comment failed for %s", task_id,
                           exc_info=True)
        current = await self._repo.get_task(task_id)
        return current or failed or {"task_id": task_id}

    async def run_task(self, task_id: str, force: bool = False) -> dict[str, Any] | None:
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        if lock.locked() and not force:
            return await self._repo.get_task(task_id)

        async with lock:
            task = await self._repo.get_task(task_id)
            if task is None:
                return None

            # A business cancellation is terminal: force relaunches a
            # completed/converted ticket, never an owner-cancelled one
            # (TASK-2026-00177).
            if str(task.get("status") or "") == TaskStatus.CANCELLED.value:
                return task
            if task.get("status") in (
                TaskStatus.COMPLETED.value, TaskStatus.CONVERTED.value,
            ) and not force:
                return task
            entry_status = str(task.get("status") or "")

            # Serialize execution per project so dev agents never race on the same repo
            project = task.get("project") or "default"
            project_lock = self._project_locks.setdefault(project, asyncio.Lock())
            async with project_lock:
                # The ticket may have been cancelled (or closed/converted)
                # while waiting for the project lock: re-read before
                # writing in_progress, or the run would resurrect it. Only
                # a CHANGE to terminal fences: a force relaunch of an
                # already-completed ticket must still proceed.
                task = await self._repo.get_task(task_id)
                if task is None:
                    return None
                current_status = str(task.get("status") or "")
                if current_status != entry_status and current_status in (
                    TaskStatus.COMPLETED.value,
                    TaskStatus.CONVERTED.value,
                    TaskStatus.CANCELLED.value,
                ):
                    logger.info(
                        "run_task: ticket %s is %s after acquiring the "
                        "project lock; not starting",
                        task_id, current_status,
                    )
                    self._wake_dispatcher()
                    return task
                try:
                    task, stop = await self._plan_pedido_pre_stage(
                        task, force=force)
                except Exception as exc:
                    # Outside the run try: fail terminally (commented, CEO
                    # sees it) instead of crashing the launch or stranding
                    # the ticket in an undecided wait.
                    logger.warning("pedido pre-stage failed for %s",
                                   task_id, exc_info=True)
                    self._wake_dispatcher()
                    return await self._stop_pedido_failed(
                        task_id, f"pedido planning failed: {exc}")
                if stop is not None:
                    # Planned holds and parks patch waiting/queued
                    # explicitly (the dispatch claimed in_progress
                    # before launch), so the dispatcher sees them as
                    # ordinary waits.
                    self._wake_dispatcher()
                    return stop
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
                    # Explicit routing first: the persisted hint, else the
                    # persisted agent (legacy mirrors carry the name
                    # there) — except the "ceo" default, which is
                    # undecided and must not shadow params.target_agent.
                    # Nothing undecided reaches the graph: the pre-stage
                    # planned every pedido above.
                    agent = task.get("agent")
                    agent = agent if str(agent or "").strip().lower() not in {"", "ceo"} else None
                    hint = task.get("agent_hint") or agent
                    if has_approval_phases:
                        graph_out = await self._graph.execute(task, agent_hint=hint)
                    elif self._workflow_service is not None:
                        workflow_out = await self._workflow_service.run_for_task(task)
                        if workflow_out is not None:
                            graph_out = self._workflow_outcome_to_graph_out(workflow_out)
                        elif explicit_yaml_target and not hint and not _target_of(task):
                            # Unknown workflow target with no agent to
                            # fall back to: fail loudly instead of
                            # stranding in a wake-less routing wait.
                            target = _workflow_target_of(task)
                            await self.comment_on_ticket(
                                task_id,
                                f"Unknown workflow target {target!r} and "
                                "no agent to fall back to: failing. Fix "
                                "the target or set an explicit agent.")
                            raise _UnknownWorkflowTarget(target)
                        else:
                            graph_out = await self._graph.execute(task, agent_hint=hint)
                    else:
                        graph_out = await self._graph.execute(task, agent_hint=hint)
                except Exception as exc:
                    current = await self._repo.get_task(task_id)
                    current_status = str((current or {}).get("status") or "")
                    if (current is not None
                            and current_status == TaskStatus.CANCELLED.value):
                        logger.info(
                            "run_task: ticket %s was cancelled during "
                            "execution; discarding failure %s",
                            task_id, type(exc).__name__,
                        )
                        self._wake_dispatcher()
                        return current
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
                        asyncio.create_task(self._revoke_ticket_badges(failed))
                    # A freed slot is a dispatch event even on crash.
                    self._wake_dispatcher()
                    return failed

            # Fencing before incorporating the result: a business
            # cancellation that landed mid-run wins over whatever the
            # graph produced. Only cancelled is fenced here: an F6
            # conversion legitimately flips the running ticket to
            # converted, and its outcome must still land.
            current = await self._repo.get_task(task_id)
            current_status = str((current or {}).get("status") or "")
            if (current is not None
                    and current_status == TaskStatus.CANCELLED.value):
                logger.info(
                    "run_task: ticket %s was cancelled during execution; "
                    "discarding %s result",
                    task_id, graph_out.get("status"),
                )
                self._wake_dispatcher()
                return current
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
            cost_runs = graph_out.get(COST_RUNS_KEY)
            if cost_runs:
                # Measured cost (TASK-2026-00212): this execution's runs
                # fold onto the stored ticket cost. The mid-run re-read
                # is the freshest base; the run-start snapshot covers a
                # ticket deleted mid-flight.
                base = (current or task or {}).get("cost")
                updates["cost"] = record_runs(base, list(cost_runs))
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
            # Persist modified files into params to keep task document stable.
            # The dev session's own additions (planner decision, default
            # tier, frozen oracle, executor link, material failure...)
            # travel in
            # ``graph_out["params"]``: merge them over the ticket's params.
            # Rebuilding from ``task`` alone discarded them on every run that
            # touched files (TASK-2026-00178; live ticket #196 kept only the
            # planner keys).
            modified_files = graph_out.get("modified_files")
            if modified_files:
                params = dict(task.get("params") or {})
                session_params = graph_out.get("params")
                if isinstance(session_params, dict):
                    params.update(session_params)
                params["modified_files"] = modified_files
                updates["params"] = params

            current_params = current.get("params") \
                if isinstance(current, dict) else {}
            if not isinstance(current_params, dict):
                current_params = {}
            lock_hold = (
                current is not None
                and str(current.get("status") or "")
                == TaskStatus.WAITING.value
                and str(current.get("waiting_reason") or "").startswith(
                    LOCK_WAITING_PREFIX)
                and bool(current_params.get("owner_question_id"))
            )
            if lock_hold and current is not None:
                # A tool lock parked mid-run wins over the run outcome
                # (TASK-2026-00208): the run that asked the Owner cannot
                # complete above the hold. Its verdict still merges in.
                final_status = TaskStatus.WAITING.value
                updates["status"] = final_status
                updates["waiting_reason"] = current.get("waiting_reason")
                updates["resume_at"] = None
                updates.pop("completed_at", None)
                merged = dict(current_params)
                graph_params = graph_out.get("params")
                if isinstance(graph_params, dict):
                    merged.update(graph_params)
                merged["owner_question_id"] = current_params.get(
                    "owner_question_id")
                updates["params"] = merged
            start_answer = (task.get("params") or {}).get("owner_answer")
            fresh_answer = current_params.get("owner_answer")
            resumed_mid_run = (
                current is not None
                and isinstance(fresh_answer, dict)
                and (not isinstance(start_answer, dict)
                     or fresh_answer.get("at") != start_answer.get("at"))
                and str(current.get("status") or "")
                in {TaskStatus.QUEUED.value,
                    TaskStatus.IN_PROGRESS.value,
                    TaskStatus.WAITING.value}
                and not bool(current_params.get("owner_question_id"))
            )
            if not lock_hold and resumed_mid_run:
                # The lock was answered in the run tail and the ticket
                # moved on (resumed or relaunched): this outcome is
                # stale. Compared against the run-start snapshot, so a
                # re-run carrying an old answer still lands.
                logger.info(
                    "run_task: ticket %s was resumed mid-run; discarding "
                    "%s result",
                    task_id, graph_out.get("status"),
                )
                self._wake_dispatcher()
                return current

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
                if final_status in _TERMINAL_TASK_STATUSES:
                    asyncio.create_task(self._revoke_ticket_badges(updated))
                if final_status == TaskStatus.WAITING.value:
                    reason = str(updated.get("waiting_reason") or "")
                    if reason.startswith(("routing_missing", "config",
                                          "workflow_blocked_tool")):
                        asyncio.create_task(self._comment_wait_on_erp(updated, reason))
                result_comment = graph_out.get("result_comment")
                if result_comment and str(result_comment).strip():
                    # The orchestrator reports the agent verdict on the
                    # business mirror; agents never change states
                    # (TASK-2026-00205). Never raises, never blocks long.
                    await self.comment_on_ticket(
                        task_id, str(result_comment).strip())
                if final_status in {TaskStatus.COMPLETED.value,
                                    TaskStatus.FAILED.value} \
                        and has_drafts(
                            task_id,
                            base_dir=self._crystallization_drafts_base()):
                    # Crystallization save step (TASK-2026-00210):
                    # drafts left in the ticket folder become versioned
                    # role skills. Escalated closes stay out: their close
                    # belongs to whoever resolves the escalation.
                    asyncio.create_task(self._crystallize_after_close(
                        task_id,
                        str(graph_out.get("selected_agent") or ""),
                        updated))
                if (final_status == TaskStatus.COMPLETED.value
                        and updated.get("objective_id")):
                    # Single post-persist objective close (TASK-2026-00169):
                    # before the dispatcher wake so planner children are
                    # covered by it.
                    await self._trigger_objective_close(
                        updated, graph_out.get("params"))
                if final_status == TaskStatus.FAILED.value:
                    # Single post-persist agent return (TASK-2026-00202;
                    # loose tickets return once as well, TASK-2026-00204):
                    # the hook no-ops without a verdict.
                    await self._trigger_objective_return(
                        updated, graph_out.get("params"))
                # Any finished run frees its slot: dispatch event.
                self._wake_dispatcher()
            return updated

    async def _comment_wait_on_erp(self, task: dict[str, Any], reason: str) -> None:
        """Post a one-shot ERP comment for waits that need the owner.

        Only routing, config and blocked-tool waits qualify: all mean a
        human (or the tier authority) must fix something before the ticket
        can run. Transient waits (quota, repo_busy, rate limits) heal
        alone and stay silent. Never breaks execution.
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
                text = wait_comment_text(task, reason)
            elif key == "workflow_blocked_tool":
                detail = str(task.get("error") or reason).strip()
                text = ("OpenCEO no puede ejecutar este ticket: su "
                        "workflow necesita una tool inexistente "
                        f"({detail}). Revisa la skill.")
            else:
                text = f"OpenCEO no puede ejecutar este ticket: configuración inválida ({reason})."
            await adapter.add_comment(name, text)
            await self._repo.patch_task(task["task_id"], {"wait_commented_for": key})
        except Exception:
            logger.warning("Could not comment wait on ERP for %s", task.get("task_id"), exc_info=True)

    def _crystallization_drafts_base(self) -> Path | None:
        """Ticket-folder base the generic runs wrote drafts under.

        Generic runs stage their folders under the wiring's
        ``work_base_dir`` (late-bound on the graph); reading the same
        source keeps writer and gate aligned (review 00012 H1). Any
        gap falls back to the sandbox default.
        """
        wiring = getattr(getattr(self, "_graph", None),
                         "_generic_wiring", None)
        base = getattr(wiring, "work_base_dir", None)
        return Path(base) if base else None

    async def _crystallize_after_close(
            self, task_id: str, agent: str,
            ticket: dict[str, Any]) -> None:
        """Fire-and-forget crystallization save (TASK-2026-00210).

        Drafts the agent left in its ticket folder become versioned
        role skills through the deterministic save gate. Never raises,
        never touches the ticket outcome: failures land in the outcome
        comment at most.
        """
        try:
            mirror = mirror_ticket(ticket)
            folder = ticket_dir(
                task_id,
                base_dir=self._crystallization_drafts_base()) \
                / DRAFTS_DIRNAME
            if not agent or mirror is None:
                logger.warning(
                    "crystallization skipped for %s (agent=%r, mirror=%s): "
                    "drafts kept in the ticket folder",
                    task_id, agent, mirror)
                return
            outcome = await asyncio.to_thread(
                run_save_step,
                SaveStepConfig(
                    repo=Path(__file__).resolve().parent.parent,
                    agent=agent, ticket=mirror, drafts_dir=folder))
            if outcome.comment:
                await self.comment_on_ticket(task_id, outcome.comment)
        except Exception:
            logger.warning("Crystallization failed for %s", task_id,
                           exc_info=True)

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

    async def plan_objective(self, objective_id: str) -> dict[str, Any]:
        """Run one objective planning cycle now (TASK-2026-00174).

        External producers (Horizon promotion) call this after leaving
        work the planner must consider. Never raises: without wiring
        it reports ok False.
        """
        service = getattr(self, "_objective_service", None)
        if service is None:
            return {"ok": False, "reason": "no-objective-service"}
        return await service.plan_objective(objective_id)

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

    async def create_owner_question(
        self,
        *,
        kind: str,
        question: str,
        human_action: str,
        project: str,
        origin_task_id: str | None = None,
        origin_title: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        context: str | None = None,
        tool: dict[str, Any] | None = None,
        source_id: str | None = None,
        priority: TaskPriority = TaskPriority.HIGH,
    ) -> dict[str, Any]:
        """Owner-question entry (single human channel, TASK-2026-00206).

        Persists the question ERPNext-first through :meth:`create_task`
        (autorun off, caller-provided ``source_id`` for idempotency), then
        parks it in ``waiting``/``owner_question`` with any project phase
        workflow cleared: the status observer pushes Pending Review, the
        "Requiere humano" tray lists it, and the cockpit answers it. The
        answer resumes the origin ticket from its checkpoint. Raises
        OwnerChannelError when the kind is not for the Owner.
        """
        spec = build_owner_question(
            kind=kind,
            question=question,
            human_action=human_action,
            origin_task_id=origin_task_id,
            origin_title=origin_title,
            checkpoint=checkpoint,
            context=context,
            tool=tool,
            project=project,
            source_id=source_id,
        )
        payload = TaskCreateRequest(
            project=project,
            source=TaskSource.API,
            source_id=spec["source_id"],
            title=spec["title"],
            description=spec["description"],
            priority=priority,
            params=spec["params"],
            tags=spec["tags"],
            created_by="owner-channel",
            autorun=False,
        )
        created = await self.create_task(payload)
        if question_answered(created):
            # Idempotent hit on a decided question: never resurrect it.
            return created
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

    async def check_lock_state(self, source_id: str) -> dict[str, Any]:
        """State of a lock question by its idempotency key. No side effects.

        ``none`` (no question yet), ``pending``, ``approved`` (decided,
        execution still owed), ``executed`` (with the stored outcome) or
        ``denied``. Re-runs of a locked tool read this instead of
        re-escalating; only the decision path executes. Store errors
        propagate (fail-closed): a forged ``none`` would re-escalate
        what is already decided.
        """
        key = str(source_id or "").strip()
        if not key:
            return {"state": "none", "question": None}
        prior = await self._repo.find_task_by_source("api", key)
        if prior is None or lock_hold_action(prior) is None:
            return {"state": "none", "question": None}
        block = owner_question_block(prior)
        decision = block.get("decision") or {}
        verdict = str(decision.get("decision") or "").strip()
        if verdict == "denied":
            return {"state": "denied", "question": prior}
        execution = (dict(block.get("lock") or {}).get("execution")
                     if isinstance(block.get("lock"), dict) else None)
        if isinstance(execution, dict) and execution.get("status") == "executed":
            return {
                "state": "executed",
                "question": prior,
                "outcome": execution.get("outcome"),
            }
        if verdict == "approved":
            return {"state": "approved", "question": prior}
        return {"state": "pending", "question": prior}

    async def request_tool_lock(
        self,
        *,
        tool: str,
        name: str,
        question: str,
        action: dict[str, Any],
        fingerprint: str,
        decision: dict[str, Any],
        describe: dict[str, Any] | None = None,
        context: str | None = None,
        project: str,
        origin_task_id: str | None = None,
        origin_title: str | None = None,
        source_id: str | None = None,
        _duplicate_retry: bool = False,
    ) -> dict[str, Any]:
        """Raise a ``lock`` owner-question and park the origin. Idempotent.

        A repeat call with the same ``source_id`` never duplicates the
        question: pending/approved replays the hold, executed replays the
        stored outcome, denied reports the denial. A concurrent duplicate
        loses the unique index and replays the winner instead of failing.
        The origin parks in ``waiting(tool_lock:*)`` with its params
        merged (never wiped); without a live origin the question stands
        alone and the approval still executes the stored action once.
        """
        clean_tool = str(tool or "").strip() or "tool"
        origin: dict[str, Any] | None = None
        if origin_task_id:
            try:
                origin = await self._repo.get_task(str(origin_task_id))
            except Exception:
                origin = None
        origin_key = str(origin_task_id or "").strip() or "no-origin"
        key = (str(source_id or "").strip()
               or lock_source_id(origin_key, clean_tool, fingerprint))
        seen = await self.check_lock_state(key)
        reason = lock_waiting_reason(clean_tool)
        if seen["state"] == "executed":
            outcome = seen.get("outcome")
            if isinstance(outcome, dict):
                return dict(outcome)
            return pending_lock_result(
                tool=clean_tool,
                question_task_id=str((seen.get("question") or {}).get("task_id") or ""),
                waiting_reason=reason,
                detail="Ya ejecutado; outcome ilegible, no se repite.",
            )
        if seen["state"] in {"pending", "approved"}:
            prior = seen.get("question") or {}
            return pending_lock_result(
                tool=clean_tool,
                question_task_id=str(prior.get("task_id") or ""),
                waiting_reason=reason,
                detail="Ya esta pendiente de aprobacion del dueno.",
            )
        if seen["state"] == "denied":
            return {
                "ok": False,
                "status": "denied",
                "tool": clean_tool,
                "question_task_id": str((seen.get("question") or {}).get("task_id") or ""),
                "reason": "El dueno denego esta accion.",
            }
        if not str(project or "").strip():
            return {
                "ok": False,
                "status": "invalid_input",
                "tool": clean_tool,
                "reason": "lock needs a project: pass project with the call.",
            }
        live_origin = (
            origin is not None
            and str(origin.get("status") or "") in {
                TaskStatus.QUEUED.value,
                TaskStatus.IN_PROGRESS.value,
                TaskStatus.WAITING.value,
            }
        )
        spec = lock_question_spec(
            tool=clean_tool,
            name=name,
            question=question,
            action=action,
            fingerprint=fingerprint,
            decision=decision,
            describe=describe,
            context=context,
            origin_task_id=str(origin_task_id) if live_origin else None,
            origin_title=origin_title,
            project=project,
            source_id=key,
        )
        slot = spec["params"]["owner_question"]
        try:
            created = await self.create_owner_question(
                kind=str(slot.get("kind") or "lock"),
                question=str(slot.get("question") or ""),
                human_action=str(slot.get("human_action") or ""),
                project=project,
                origin_task_id=slot.get("origin_task_id"),
                origin_title=slot.get("origin_title"),
                checkpoint=dict(slot.get("checkpoint") or {}),
                context=context,
                tool=dict(slot.get("tool") or {}),
                source_id=key,
            )
        except DuplicateKeyError:
            # A concurrent twin won the unique (source, source_id)
            # index: replay the winner instead of failing. The check
            # first makes this terminate after one retry.
            if _duplicate_retry:
                raise
            logger.info("lock %s lost a creation race; replaying", key)
            return await self.request_tool_lock(
                tool=clean_tool, name=name, question=question,
                action=action, fingerprint=fingerprint,
                decision=decision, describe=describe, context=context,
                project=project, origin_task_id=origin_task_id,
                origin_title=origin_title, source_id=key,
                _duplicate_retry=True,
            )
        question_id = str(created.get("task_id") or "")
        if live_origin and origin is not None:
            await self.patch_task(str(origin.get("task_id") or ""), {
                "status": TaskStatus.WAITING.value,
                "waiting_reason": reason,
                "resume_at": None,
                "params": {"owner_question_id": question_id},
                "last_response_summary": (
                    f"Candado ({clean_tool}): pendiente de aprobacion "
                    f"del dueno ({question_id})."
                ),
            })
            try:
                await self.comment_on_ticket(
                    str(origin.get("task_id") or ""),
                    f"Candado ({clean_tool}): pedi aprobacion al dueno "
                    f"({question_id}); el trabajo se reanuda solo.",
                )
            except Exception:
                logger.warning("lock park comment failed for %s",
                               origin.get("task_id"), exc_info=True)
        return pending_lock_result(
            tool=clean_tool,
            question_task_id=question_id,
            waiting_reason=reason,
            detail=question,
        )

    def _lock_project_config(self, project: str | None) -> dict[str, Any]:
        loader = getattr(self, "_config_loader", None)
        if loader is None or not str(project or "").strip():
            return {}
        try:
            config = loader.load_project(str(project).strip())
        except Exception:
            return {}
        return dict(config) if isinstance(config, dict) else {}

    async def _resolve_lock_origin(
        self,
        *,
        origin_task_id: str | None = None,
        ticket: str | None = None,
    ) -> dict[str, Any] | None:
        if origin_task_id:
            try:
                found = await self._repo.get_task(str(origin_task_id))
            except Exception:
                found = None
            if found is not None:
                return found
        name = str(ticket or "").strip()
        if name:
            try:
                return await self._repo.find_task_by_business_name(name)
            except Exception:
                logger.warning("lock origin lookup failed for %s", name,
                               exc_info=True)
        return None

    async def _snapshot_attachments(
        self, attachments: Any
    ) -> dict[str, str]:
        """sha256 of each attachment at request time (TOCTOU guard).

        Keyed by resolved path, with the sender's own normalization, so
        a swap or deletion between the request and the approval fails
        the execution instead of sending something else. With
        attachments, snapshot failures fail fast at the request
        (review 00008): a silent empty snapshot would hold every
        approval forever.
        """
        from tools.email_sender import SMTPEmailSender

        if not attachments:
            return {}
        sender = SMTPEmailSender(self._settings)
        items = attachments if isinstance(attachments, list) else [attachments]

        def _hash() -> dict[str, str]:
            out: dict[str, str] = {}
            for item in sender._normalize_attachments(items):
                out[str(item.path)] = hashlib.sha256(
                    item.path.read_bytes()).hexdigest()
            return out

        return await asyncio.to_thread(_hash)

    def _verify_attachment_snapshot(self, action: dict[str, Any]) -> None:
        """Refuse the send when attachments changed since the request."""
        from tools.email_sender import SMTPEmailSender

        attachments = action.get("attachments") or []
        expected = action.get("_attachment_hashes") or {}
        sender = SMTPEmailSender(self._settings)
        items = attachments if isinstance(attachments, list) else [attachments]
        current: dict[str, str] = {}
        for item in sender._normalize_attachments(items):
            current[str(item.path)] = hashlib.sha256(
                item.path.read_bytes()).hexdigest()
        if current != expected:
            raise ToolLockError(
                "attachments changed since the request; refusing to send")

    async def tool_send_email(
        self,
        *,
        from_email: str | None,
        to: Any,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        cc: Any = None,
        bcc: Any = None,
        reply_to: str | None = None,
        attachments: Any = None,
        language: str | None = None,
        project: str | None = None,
        origin_task_id: str | None = None,
        ticket: str | None = None,
    ) -> dict[str, Any]:
        """Gated email send: the single email lock (TASK-2026-00208).

        MCP, registry and session paths all enter here. On-allowlist
        mail sends at once; anything else raises a ``lock`` question
        and answers pending. Approvals execute the stored send exactly
        once; re-runs replay its outcome.
        """
        from tools.email_sender import SMTPEmailSender

        origin = await self._resolve_lock_origin(
            origin_task_id=origin_task_id, ticket=ticket)
        named = str(origin_task_id or "").strip() \
            or str(ticket or "").strip()
        if named and origin is None:
            # A named origin that does not resolve is a caller error,
            # never a standalone question: silently orphaning it would
            # let the run complete above the hold.
            return {
                "ok": False,
                "status": "invalid_input",
                "tool": "email",
                "reason": f"origin not found: {named}.",
            }
        project_name = (str(project or "").strip()
                        or str((origin or {}).get("project") or "").strip()
                        or None)
        decision = check_email_lock(
            to=to, cc=cc, bcc=bcc,
            project_config=self._lock_project_config(project_name),
            mandate=self._company_mandate(),
        )
        if not decision.get("escalate"):
            sender = SMTPEmailSender(self._settings)
            return await asyncio.to_thread(
                sender.send_email,
                from_email=from_email,
                to=to,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                cc=cc,
                bcc=bcc,
                reply_to=reply_to,
                attachments=attachments,
                language=language,
            )
        blocked = decision.get("blocked") or []
        action = {
            "from_email": from_email,
            "to": to if isinstance(to, list) else [to],
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "cc": cc if isinstance(cc, list) else ([cc] if cc else []),
            "bcc": bcc if isinstance(bcc, list) else ([bcc] if bcc else []),
            "reply_to": reply_to,
            "attachments": attachments or [],
            "language": language,
        }
        action["_attachment_hashes"] = await self._snapshot_attachments(
            attachments)
        def _norm(items: Any) -> list[str]:
            return sorted(str(item).strip().lower() for item in items or [])

        fingerprint = action_fingerprint({
            "from": str(from_email or "").strip().lower(),
            "to": _norm(action["to"]),
            "cc": _norm(action["cc"]),
            "bcc": _norm(action["bcc"]),
            "subject": str(subject or "").strip(),
            "body": str(body_text or ""),
            "body_html": str(body_html or ""),
            "reply_to": str(reply_to or "").strip().lower(),
            "attachments": action["_attachment_hashes"],
            "language": str(language or ""),
        })
        attachment_names = ", ".join(
            sorted(action["_attachment_hashes"].keys()))
        return await self.request_tool_lock(
            tool="email",
            name="send_email",
            question=(f"Enviar correo a {', '.join(blocked[:4])}?"[:200]),
            action=action,
            fingerprint=fingerprint,
            decision=decision,
            describe={
                "from": from_email or "(defecto)",
                "to": ", ".join(decision.get("recipients") or []),
                "subject": subject,
                "body": str(body_text or ""),
                "reply_to": reply_to or "-",
                "attachments": attachment_names or "-",
            },
            context=f"Motivo: {decision.get('reason')}",
            project=project_name or "",
            origin_task_id=str(origin.get("task_id") or "")
            if origin is not None else None,
            origin_title=str(origin.get("title") or "") if origin else None,
        )

    async def tool_dns_delete(
        self,
        *,
        hostname: str,
        project: str | None = None,
        origin_task_id: str | None = None,
        ticket: str | None = None,
    ) -> dict[str, Any]:
        """Gated DNS deletion: always through the Owner (TASK-2026-00208)."""
        origin = await self._resolve_lock_origin(
            origin_task_id=origin_task_id, ticket=ticket)
        named = str(origin_task_id or "").strip() \
            or str(ticket or "").strip()
        if named and origin is None:
            return {
                "ok": False,
                "status": "invalid_input",
                "tool": "dns",
                "reason": f"origin not found: {named}.",
            }
        project_name = (str(project or "").strip()
                        or str((origin or {}).get("project") or "").strip()
                        or None)
        clean_host = str(hostname or "").strip()
        decision = check_dns_delete_lock(
            hostname=clean_host, mandate=self._company_mandate())
        if not decision.get("escalate"):
            return {
                "ok": False,
                "status": "refused",
                "tool": "dns",
                "reason": "DNS deletion refused without escalation.",
            }
        return await self.request_tool_lock(
            tool="dns",
            name="dns_delete",
            question=f"Borrar el registro DNS de {clean_host}?"[:200],
            action={"hostname": clean_host},
            fingerprint=action_fingerprint(
                {"hostname": clean_host.lower()}),
            decision=decision,
            describe={"hostname": clean_host},
            context=f"Motivo: {decision.get('reason')}",
            project=project_name or "",
            origin_task_id=str(origin.get("task_id") or "")
            if origin is not None else None,
            origin_title=str(origin.get("title") or "") if origin else None,
        )

    async def tool_elfec_pay(
        self,
        *,
        workflow: dict[str, Any] | None = None,
        expected_row_label: str = "",
        project: str | None = None,
        origin_task_id: str | None = None,
        ticket: str | None = None,
    ) -> dict[str, Any]:
        """Gated Elfec payment run: always through the Owner (00208)."""
        origin = await self._resolve_lock_origin(
            origin_task_id=origin_task_id, ticket=ticket)
        named = str(origin_task_id or "").strip() \
            or str(ticket or "").strip()
        if named and origin is None:
            return {
                "ok": False,
                "status": "invalid_input",
                "tool": "elfec",
                "reason": f"origin not found: {named}.",
            }
        project_name = (str(project or "").strip()
                        or str((origin or {}).get("project") or "").strip()
                        or None)
        cfg = dict(workflow or {})
        if expected_row_label and "expected_row_label" not in cfg:
            cfg["expected_row_label"] = expected_row_label
        decision = check_payment_lock(
            summary="Ejecutar el pago Elfec House (practica)",
            mandate=self._company_mandate(),
            details={"workflow_id": str(cfg.get("id") or "pago_elfec_house")},
        )
        if not decision.get("escalate"):
            return {
                "ok": False,
                "status": "refused",
                "tool": "elfec",
                "reason": "Payment refused without escalation.",
            }
        return await self.request_tool_lock(
            tool="elfec",
            name="elfec_pay",
            question="Ejecutar el pago Elfec House (practica)?"[:200],
            action={"workflow": cfg},
            fingerprint=action_fingerprint({"workflow": cfg}),
            decision=decision,
            describe={"workflow": str(cfg.get("id") or "pago_elfec_house")},
            context=f"Motivo: {decision.get('reason')}",
            project=project_name or "",
            origin_task_id=str(origin.get("task_id") or "")
            if origin is not None else None,
            origin_title=str(origin.get("title") or "") if origin else None,
        )

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
        :meth:`create_phase_action`; an owner-question ticket (TASK-2026-00206)
        records the decision and resumes (approve) or fails + returns to the
        planner (deny) its origin; a standalone owner-escalation ticket
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
            is_owner_question(task)
            and (
                str(task.get("status") or "") == TaskStatus.WAITING.value
                or await self._owner_question_retry_due(task)
            )
        ):
            # H3 (revisión 00206): una decisión ya grabada manda sobre el
            # botón que pulse el reintento; así un fallo parcial (desk con
            # ERPNext caído) no puede invertirse desde cockpit.
            recorded = (
                owner_question_block(task).get("decision") or {}
            ).get("decision")
            if recorded in {"approved", "denied"}:
                approve = recorded == "approved"
            return await self._apply_decision_to_owner_question(
                task, approve=approve, actor_id=actor_id, comment=note
            )

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

    async def _apply_decision_to_owner_question(
        self,
        task: dict[str, Any],
        *,
        approve: bool,
        actor_id: str,
        comment: str,
    ) -> dict[str, Any]:
        task_id = str(task.get("task_id") or "")
        block = owner_question_block(task)
        kind = str(block.get("kind") or "")
        origin_id = str(block.get("origin_task_id") or "")
        dev_task_id: str | None = None
        if kind == "new_tool" and approve:
            # The dev ticket first: creation may raise (ERPNext hard fail),
            # and then the question must stay open for a retry.
            dev_task_id = await self._create_tool_ticket_from_question(
                task, block
            )
        record = owner_answer_record(
            approve=approve,
            actor_id=actor_id,
            comment=comment,
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        summary = self._owner_answer_summary(approve, comment)
        if kind == "new_tool" and approve and dev_task_id:
            tool = block.get("tool") if isinstance(block.get("tool"), dict) else {}
            summary = (
                f"Tool de frontera {tool.get('name')} aprobada; "
                f"ticket de dev {dev_task_id} creado. "
                "Replanificar cuando exista."
                + (f" {comment}" if comment else "")
            )
        patched = await self._record_owner_answer(
            task, block, record, approve=approve,
            summary=summary, actor_id=actor_id,
            # H3 retry (already landed by the desk) only re-records: a
            # desk-cancelled question must never flip to completed.
            land_status=str(task.get("status") or "")
            == TaskStatus.WAITING.value,
        )
        await self._review_owner_answer(
            task, approve=approve, actor_id=actor_id, comment=comment
        )
        route = await self._handle_answered_origin(
            kind=kind,
            origin_id=origin_id,
            approve=approve,
            record=record,
            summary=summary,
            question=task,
        )
        result: dict[str, Any] = {
            "applied": True, "route": route, "task": patched or task,
        }
        if dev_task_id:
            result["dev_task_id"] = dev_task_id
        return result

    @staticmethod
    def _owner_answer_summary(approve: bool, comment: str) -> str:
        verdict = "aprobada" if approve else "denegada"
        summary = f"Decisión del dueño: {verdict}."
        if comment:
            summary += f" {comment}"
        return summary

    async def _record_owner_answer(
        self,
        task: dict[str, Any],
        block: dict[str, Any],
        record: dict[str, Any],
        *,
        approve: bool,
        summary: str,
        actor_id: str,
        land_status: bool,
    ) -> dict[str, Any] | None:
        """Store the decision on the question. With ``land_status`` the
        question also completes (cockpit path); the desk path already
        landed the status through the webhook and only records."""
        task_id = str(task.get("task_id") or "")
        params = dict(task.get("params") or {})
        stored_block = dict(block)
        stored_block["decision"] = record
        params["owner_question"] = stored_block
        updates: dict[str, Any] = {
            "approval_state": (
                ApprovalState.APPROVED.value if approve
                else ApprovalState.REJECTED.value
            ),
            "waiting_reason": None,
            "last_response_summary": summary,
            "params": params,
            "updated_by": actor_id,
        }
        if land_status:
            updates["status"] = TaskStatus.COMPLETED.value
            updates["completed_at"] = datetime.now(timezone.utc)
        return await self.patch_task(task_id, updates)

    async def _review_owner_answer(
        self,
        task: dict[str, Any],
        *,
        approve: bool,
        actor_id: str,
        comment: str,
    ) -> None:
        payload = TaskReviewCreateRequest(
            actor_type=TaskReviewActorType.INTERNAL,
            actor_id=actor_id,
            action=TaskReviewAction.APPROVE if approve else TaskReviewAction.REJECT,
            comment=comment,
            metadata={"phase": OWNER_QUESTION_PHASE, "event": "owner_decision"},
        )
        review_doc = make_task_review_document(
            task_id=str(task.get("task_id") or ""),
            project=str(task.get("project") or ""),
            payload=payload,
            approval_state_after=(
                ApprovalState.APPROVED if approve else ApprovalState.REJECTED
            ),
        )
        review = await self._repo.create_task_review(review_doc.model_dump(mode="python"))
        await self._hub.broadcast("task_review_created", review)

    async def _owner_question_retry_due(self, task: dict[str, Any]) -> bool:
        """True when a recorded answer still owes its origin work (H3).

        A partial failure (desk landing before a failed dev-ticket
        creation) leaves decision recorded + origin waiting: retryable
        from the cockpit, honoring the recorded verdict. Anything else
        (no decision, no origin, origin moved on) is not a retry.
        """
        if not question_answered(task):
            return False
        origin_id = str(
            owner_question_block(task).get("origin_task_id") or ""
        )
        if not origin_id:
            return False
        origin = await self._repo.get_task(origin_id)
        return (
            origin is not None
            and str(origin.get("status") or "") == TaskStatus.WAITING.value
        )

    async def _handle_answered_origin(
        self,
        *,
        kind: str,
        origin_id: str,
        approve: bool,
        record: dict[str, Any],
        summary: str,
        question: dict[str, Any] | None = None,
    ) -> str:
        """Resume (approve) or fail + return to planner (deny) the origin.

        A ``new_tool`` approval also fails the origin: it cannot proceed
        until the dev ticket builds the tool, so the planner replans.
        A spend-hold question has no origin ticket: the approval
        launches the stored plan, the denial returns the motive to the
        planner (TASK-2026-00207). Only a waiting origin moves;
        anything else (already relaunched, terminal) is left alone and
        the question simply closes.
        """
        spend_objective = spend_hold_objective(question)
        if spend_objective is not None:
            return await self._apply_spend_decision(
                question or {}, spend_objective, approve=approve,
                summary=summary)
        lock = lock_hold_action(question)
        if lock is not None:
            return await self._apply_lock_decision(
                question or {}, lock, origin_id=origin_id,
                approve=approve, record=record, summary=summary)
        if kind == "new_tool" and approve:
            await self._deny_origin_from_question(origin_id, kind, summary)
            return "owner_question_tool"
        if not approve:
            await self._deny_origin_from_question(origin_id, kind, summary)
            return "owner_question_deny"
        if await self._resume_origin_from_question(origin_id, record, summary):
            return "owner_question_resume"
        return "owner_question_close"

    async def _resume_origin_from_question(
        self,
        origin_id: str,
        record: dict[str, Any],
        summary: str,
    ) -> bool:
        if not origin_id:
            return False
        origin = await self._repo.get_task(origin_id)
        if origin is None:
            return False
        if str(origin.get("status") or "") != TaskStatus.WAITING.value:
            return False
        await self.patch_task(origin_id, {
            "status": TaskStatus.QUEUED.value,
            "waiting_reason": None,
            "resume_at": None,
            "params": {"owner_answer": record, "owner_question_id": None},
            "last_response_summary": summary,
        })
        self._wake_dispatcher()
        try:
            await self.comment_on_ticket(origin_id, summary)
        except Exception:
            logger.warning("owner-answer comment failed for %s", origin_id,
                           exc_info=True)
        return True

    async def _deny_origin_from_question(
        self,
        origin_id: str,
        kind: str,
        summary: str,
    ) -> None:
        if not origin_id:
            return
        origin = await self._repo.get_task(origin_id)
        if origin is None:
            return
        if str(origin.get("status") or "") != TaskStatus.WAITING.value:
            return
        verdict = denied_origin_verdict(kind=kind, owner_text=summary)
        updated = await self.patch_task(origin_id, {
            "status": TaskStatus.FAILED.value,
            "waiting_reason": None,
            "resume_at": None,
            "error": summary,
            "last_response_summary": summary,
            "params": {"agent_result": verdict, "owner_question_id": None},
        })
        if updated is None:
            return
        fresh: dict[str, Any] = {"agent_result": verdict}
        returns_from = (origin.get("params") or {}).get("returns_from")
        if returns_from:
            # Second unresolved return parks capability_gap instead of
            # replanning again (same one-return rule as the run funnel).
            fresh["returns_from"] = returns_from
        await self._trigger_objective_return(updated, fresh)

    async def _apply_spend_decision(
        self,
        question: dict[str, Any],
        objective_id: str,
        *,
        approve: bool,
        summary: str,
    ) -> str:
        """Continue a spend-held plan: launch it or return the motive.

        The approval launches exactly the stored plan; the denial
        sends a bloqueado verdict with the Owner's motive back to the
        planner through the single-return path (TASK-2026-00207).
        """
        service = getattr(self, "_objective_service", None)
        question_id = str(question.get("task_id") or "")
        if service is None:
            logger.warning("spend decision on %s without objective wiring",
                           question_id)
            return "owner_question_spend_unwired"
        if approve:
            outcome = await service.apply_spend_approval(
                objective_id, question_id)
            if outcome.get("ok"):
                return "owner_question_spend_approve"
            if str(outcome.get("reason") or "") in {
                    "no-pending-plan", "approval-plan-mismatch"}:
                await self._signal_orphan_spend_approval(question)
            return "owner_question_spend_held"
        await service.plan_return(objective_id, {
            "task_id": question_id,
            "title": str(question.get("title") or ""),
            "agent": "owner",
            "resultado": "bloqueado",
            "motivo": summary[:500],
            "necesidad": "",
        })
        return "owner_question_spend_deny"

    async def _apply_lock_decision(
        self,
        question: dict[str, Any],
        lock: dict[str, Any],
        *,
        origin_id: str,
        approve: bool,
        record: dict[str, Any],
        summary: str,
    ) -> str:
        """Continue a tool lock: execute once or fail the origin.

        The approval executes the stored action exactly once (the
        execution marker on the question guards retries), then resumes
        the origin when it is still waiting; the denial fails the
        origin with a bloqueado verdict for the planner. Without an
        origin the question simply closes after executing or denying.
        """
        question_id = str(question.get("task_id") or "")
        if not approve:
            await self._deny_origin_from_question(
                origin_id, "lock", summary)
            return "owner_question_lock_deny"
        # Fresh read: the lock above comes from a pre-record snapshot,
        # so a sequential retry must see the marker it is owed. (An
        # exactly-simultaneous double approval still races; approvals
        # are human-speed and every decision path shares that class.)
        fresh = await self._repo.get_task(question_id)
        fresh_lock = lock_hold_action(fresh)
        fresh_execution = (fresh_lock or {}).get("execution")
        if not (isinstance(fresh_execution, dict)
                and fresh_execution.get("status") == "executed"):
            try:
                outcome = await self._execute_locked_action(lock)
            except Exception as exc:
                logger.warning("locked action failed on %s: %s",
                               question_id, exc, exc_info=True)
                try:
                    await self.comment_on_ticket(
                        question_id,
                        "La aprobacion no pudo ejecutarse "
                        f"({type(exc).__name__}); se puede reintentar "
                        "desde la bandeja.",
                    )
                except Exception:
                    logger.warning("lock failure comment failed for %s",
                                   question_id, exc_info=True)
                return "owner_question_lock_held"
            await self._record_lock_execution(question_id, outcome)
        if origin_id and await self._resume_lock_origin(
                origin_id, question_id, record, summary):
            return "owner_question_lock_resume"
        return "owner_question_lock_close"

    async def _resume_lock_origin(
        self,
        origin_id: str,
        question_id: str,
        record: dict[str, Any],
        summary: str,
    ) -> bool:
        """Resume the origin only while it waits on this question.

        A second lock re-parks the origin under its own question; the
        first approval executes but must not steal that hold — the
        second approval resumes.
        """
        origin = await self._repo.get_task(origin_id)
        if origin is None:
            return False
        current = (origin.get("params") or {}).get("owner_question_id")
        if current != question_id:
            return False
        return await self._resume_origin_from_question(
            origin_id, record, summary)

    async def _record_lock_execution(
        self, question_id: str, outcome: Any
    ) -> None:
        """Stamp the executed outcome on the lock question (once)."""
        fresh = await self._repo.get_task(question_id)
        if fresh is None:
            return
        block = owner_question_block(fresh)
        stored = dict(block.get("lock") or {})
        stored["execution"] = {
            "status": "executed",
            "at": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
        }
        block["lock"] = stored
        await self.patch_task(question_id, {"params": {"owner_question": block}})

    async def _execute_locked_action(self, lock: dict[str, Any]) -> Any:
        """Run the stored action. Only the decision path calls this."""
        tool = str(lock.get("tool") or "")
        action = lock.get("action") or {}
        if not isinstance(action, dict):
            raise ToolLockError("locked action is not a mapping")
        if tool == "email":
            from tools.email_sender import SMTPEmailSender

            await asyncio.to_thread(
                self._verify_attachment_snapshot, dict(action))
            sender = SMTPEmailSender(self._settings)
            kwargs = {
                key: action.get(key)
                for key in ("from_email", "to", "subject", "body_text",
                            "body_html", "cc", "bcc", "reply_to",
                            "attachments", "language")
            }
            return await asyncio.to_thread(sender.send_email, **kwargs)
        if tool == "dns":
            from tools.cloudflare import CloudflareDNSManager

            manager = CloudflareDNSManager(self._settings)
            return await asyncio.to_thread(
                manager.delete, str(action.get("hostname") or ""))
        if tool == "elfec":
            from agents.finance.elfec import run_elfec_house_payment_practice

            workflow = action.get("workflow")
            return await asyncio.to_thread(
                run_elfec_house_payment_practice,
                self._settings,
                dict(workflow) if isinstance(workflow, dict) else None,
            )
        raise ToolLockError(f"unknown locked tool {tool!r}")

    async def _signal_orphan_spend_approval(
        self, question: dict[str, Any],
    ) -> None:
        """Tell the Owner their approval launched nothing (orphan question).

        A crash between question creation and plan persistence (or a
        superseded hold) leaves an answerable question with no plan
        behind it: approving it must say so instead of going silent.
        Never breaks the answer path.
        """
        question_id = str(question.get("task_id") or "")
        if not question_id:
            return
        summary = ("Aprobaste un gasto sin plan retenido detras: no se "
                   "lanzo nada. Si el trabajo sigue pendiente, llegara "
                   "una nueva pregunta.")
        try:
            await self.patch_task(
                question_id, {"last_response_summary": summary})
        except Exception:
            logger.warning("orphan spend signal failed for %s", question_id,
                           exc_info=True)
            return
        await self.comment_on_ticket(question_id, summary)

    async def _create_tool_ticket_from_question(
        self,
        task: dict[str, Any],
        block: dict[str, Any],
    ) -> str | None:
        tool = block.get("tool") if isinstance(block.get("tool"), dict) else {}
        name = str(tool.get("name") or "").strip()
        if not name:
            return None
        spec = build_tool_ticket(
            name=name,
            need=str(tool.get("need") or ""),
            spec_text=str(tool.get("spec") or ""),
            question_task_id=str(task.get("task_id") or ""),
            origin_task_id=str(block.get("origin_task_id") or "") or None,
            project=task.get("project"),
        )
        created = await self.create_task(TaskCreateRequest(
            project=spec["project"],
            source=TaskSource.API,
            source_id=spec["source_id"],
            title=spec["title"],
            description=spec["description"],
            priority=TaskPriority.HIGH,
            params=spec["params"],
            agent_hint=AgentName.DEV,
            created_by="owner-channel",
            autorun=True,
        ))
        return str(created.get("task_id") or "") or None

    async def _continue_answered_question(
        self,
        task_id: str,
        *,
        approve: bool,
        actor_id: str,
        comment: str = "",
    ) -> dict[str, Any]:
        """Desk path: the webhook already landed the status; record the
        decision and continue with the origin. Idempotent: an answered
        question is left alone."""
        question = await self._repo.get_task(task_id)
        if question is None:
            return {"continued": False, "reason": "task_not_found"}
        if not is_owner_question(question):
            return {"continued": False, "reason": "not_a_question"}
        if question_answered(question):
            return {"continued": False, "reason": "already_answered"}
        block = owner_question_block(question)
        kind = str(block.get("kind") or "")
        origin_id = str(block.get("origin_task_id") or "")
        # H3: the desk verdict is recorded BEFORE the fallible work, so a
        # failed dev-ticket creation still leaves a retryable, uninverted
        # decision behind.
        record = owner_answer_record(
            approve=approve,
            actor_id=actor_id,
            comment=comment,
            decided_at=datetime.now(timezone.utc).isoformat(),
        )
        summary = self._owner_answer_summary(approve, comment)
        await self._record_owner_answer(
            question, block, record, approve=approve,
            summary=summary, actor_id=actor_id, land_status=False,
        )
        await self._review_owner_answer(
            question, approve=approve, actor_id=actor_id, comment=comment
        )
        dev_task_id: str | None = None
        if kind == "new_tool" and approve:
            dev_task_id = await self._create_tool_ticket_from_question(
                question, block
            )
        route = await self._handle_answered_origin(
            kind=kind,
            origin_id=origin_id,
            approve=approve,
            record=record,
            summary=summary,
            question=question,
        )
        result: dict[str, Any] = {"continued": True, "route": route}
        if dev_task_id:
            result["dev_task_id"] = dev_task_id
        return result

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
                # Review H4: an unpaused spend ticket re-holds BEFORE the
                # wake; when the hold itself fails the ticket stays
                # paused instead of launching without approval.
                try:
                    await self._rehold_loose_spend(updated)
                except Exception:
                    logger.warning(
                        "spend re-hold failed on unpause for %s", task_id,
                        exc_info=True)
                    try:
                        await self._repo.patch_task(task_id, {
                            "autorun": False,
                            "last_response_summary": (
                                "Unpause reverted: the spend approval "
                                "could not be requested. Fix and retry."),
                        })
                        updated = await self._repo.get_task(task_id) \
                            or updated
                    except Exception:
                        logger.warning(
                            "spend re-pause failed for %s", task_id,
                            exc_info=True)
                    return updated
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
                asyncio.create_task(self._revoke_ticket_badges(updated))
                self._wake_ceo_incident(updated)
            if (
                str(updated.get("status")) == TaskStatus.CANCELLED.value
                and str(task.get("status")) != TaskStatus.CANCELLED.value
            ):
                # Dependency wakeup (§9.5, TASK-2026-00180): a cancel
                # landing through the funnel (desk webhook, explicit
                # transition) releases direct dependents, like a
                # run_task finish does. run_task never lands cancelled
                # itself (00177 fencing), so without this the
                # event-held blocked_by parks strand. Never breaks
                # the patch.
                try:
                    await self._requeue_dependents(task_id)
                except Exception:
                    logger.warning(
                        "Dependent requeue error for %s",
                        task_id, exc_info=True,
                    )
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

    async def _trigger_objective_close(
        self,
        ticket: dict[str, Any],
        session_params: Any,
    ) -> None:
        """Notify the objectives engine of a persisted COMPLETED (§9.4).

        Single post-persist close (TASK-2026-00169): the session no
        longer triggers on a stale pre-persist read. ``session_params``
        is the run's fresh output (never the merged ticket params):
        Tier S marks its inline notification there so this skips it;
        simple tiers stash their close context there; anything else
        notifies with what the persisted ticket carries. Never fails
        the close.
        """
        try:
            fresh = session_params \
                if isinstance(session_params, dict) else {}
            if fresh.get("_objective_close_notified"):
                return  # Tier S already notified inline, post-persist.
            service = getattr(self, "_objective_service", None)
            if service is None:
                return
            stashed = fresh.get("_objective_close")
            if isinstance(stashed, dict):
                close = TicketCloseContext(
                    task_id=str(ticket.get("task_id") or ""),
                    title=str(ticket.get("title") or ""),
                    tier=str(ticket.get("coding_tool") or ""),
                    sha=str(stashed.get("sha") or ""),
                    files=list(stashed.get("files") or []),
                    handoff_excerpt=str(
                        stashed.get("handoff_excerpt") or ""))
            else:
                persisted = ticket.get("params") or {}
                close = TicketCloseContext(
                    task_id=str(ticket.get("task_id") or ""),
                    title=str(ticket.get("title") or ""),
                    tier=str(ticket.get("coding_tool") or ""),
                    sha="",
                    files=list(persisted.get("modified_files") or []))
            env = service.env_for(str(ticket.get("project") or ""))
            await on_ticket_closed(env, ticket, close)
        except Exception:
            logger.warning("objective close trigger failed for %s",
                           ticket.get("task_id"), exc_info=True)

    async def _trigger_objective_return(
        self,
        ticket: dict[str, Any],
        session_params: Any,
    ) -> None:
        """Return a persisted FAILED verdict to the planner (00202/00204).

        Single post-persist return, mirroring the §9.4 close: the run's
        fresh ``agent_result`` (parsed or legacy-mapped) decides. A
        first bloqueado/no_aplica replans once with ``return_from``; a
        verdict on a relaunched ticket (``returns_from`` tag) parks
        capability_gap instead — or, for a loose ticket with no
        objective to park on, stays failed with its return history for
        the CEO reactive review. Skipped without a verdict, after a
        dev material failure (§29 already replanned), on cancellation
        and without objective wiring. Never fails the run.
        """
        try:
            fresh = session_params \
                if isinstance(session_params, dict) else {}
            verdict = fresh.get("agent_result")
            if not isinstance(verdict, dict):
                return
            resultado = str(verdict.get("resultado") or "")
            if resultado not in {"bloqueado", "no_aplica"}:
                return
            if fresh.get("material_failure") is not None:
                return  # dev §29 already replanned this failure
            task_id = str(ticket.get("task_id") or "")
            if self._repo is not None and task_id:
                try:
                    current = await self._repo.get_task(task_id)
                except Exception:
                    current = None
                if current is not None and str(
                        current.get("status") or "") == \
                        TaskStatus.CANCELLED.value:
                    return
            objective_id = str(ticket.get("objective_id") or "")
            if not objective_id:
                await self._trigger_loose_return(
                    ticket, fresh, verdict, task_id, resultado)
                return
            service = getattr(self, "_objective_service", None)
            if service is None:
                return
            entry = {
                "task_id": task_id,
                "title": str(ticket.get("title") or ""),
                "agent": str(ticket.get("agent") or ""),
                "resultado": resultado,
                "motivo": str(verdict.get("motivo") or "")[:500],
                "necesidad": str(verdict.get("necesidad") or "")[:500],
            }
            if fresh.get("returns_from"):
                outcome = await service.park_return_gap(
                    objective_id, entry)
                note = (f"Agent {resultado} after one replan: parked "
                        f"capability_gap ({outcome.get('reason')}). "
                        f"Motivo: {entry['motivo']}")
            else:
                outcome = await service.plan_return(objective_id, entry)
                if outcome.get("ok"):
                    note = (f"Agent {resultado}: returned once to the "
                            f"planner "
                            f"({outcome.get('tickets_created', 0)} "
                            f"ticket(s)). Motivo: {entry['motivo']}")
                else:
                    detail = str(outcome.get("detail") or "").strip()
                    note = (f"Agent {resultado}: return replan parked "
                            f"({outcome.get('reason')}"
                            f"{': ' + detail[:200] if detail else ''}). "
                            f"Motivo: {entry['motivo']}")
            try:
                await self.comment_on_ticket(task_id, note)
            except Exception:
                logger.warning("return comment failed for %s", task_id,
                               exc_info=True)
        except Exception:
            logger.warning("objective return trigger failed for %s",
                           ticket.get("task_id"), exc_info=True)

    async def _trigger_loose_return(
        self,
        ticket: dict[str, Any],
        fresh: dict[str, Any],
        verdict: dict[str, Any],
        task_id: str,
        resultado: str,
    ) -> None:
        """Return a loose FAILED verdict to the planner, once (00204).

        A first bloqueado/no_aplica replans the pedido with the return
        as context: the planner re-decides (or converts), the stale
        verdict is dropped, and the ticket requeues for its second and
        last attempt. A verdict on the relaunched ticket
        (``returns_from`` tag) stays failed with the return history in
        a comment — the CEO reactive review already wakes on failed
        company executions. Never fails the run.
        """
        motivo = str(verdict.get("motivo") or "")[:500]
        if fresh.get("returns_from"):
            note = (f"Agent {resultado} after one replan: no further "
                    f"return, the ticket stays failed. Motivo: {motivo}")
            try:
                await self.comment_on_ticket(task_id, note)
            except Exception:
                logger.warning("loose return comment failed for %s",
                               task_id, exc_info=True)
            return
        service = getattr(self, "_objective_service", None)
        if service is None:
            return
        entry = {
            "task_id": task_id,
            "title": str(ticket.get("title") or ""),
            "agent": str(ticket.get("agent") or ""),
            "resultado": resultado,
            "motivo": motivo,
            "necesidad": str(verdict.get("necesidad") or "")[:500],
        }
        try:
            env = service.env_for(str(ticket.get("project") or ""))
            current = None
            if self._repo is not None and task_id:
                try:
                    current = await self._repo.get_task(task_id)
                except Exception:
                    current = None
            out = await engine_mod.plan_pedido(
                env, current or ticket, return_from=entry)
        except Exception:
            logger.warning("loose return replan crashed for %s", task_id,
                           exc_info=True)
            return
        if out.get("ok") and out.get("action") == "convert":
            try:
                # convert_ticket plans the first horizon internally.
                objective = await engine_mod.convert_ticket(
                    env, current or ticket, "planner loose-return conversion",
                    sizing_text=str(out.get("state_summary") or ""))
                objective_id = str(objective.get("objective_id") or "")
                note = (f"Agent {resultado}: the return outgrows one "
                        f"ticket, converted to objective {objective_id}. "
                        f"Motivo: {motivo}")
                costs = out.get("costs") or []
                if costs:
                    bits = ", ".join(
                        f"{entry.get('label') or '?'} "
                        f"({entry.get('amount') or '?'})"
                        for entry in costs if isinstance(entry, dict))
                    note += (" Declared spend (re-gated by objective "
                             f"plans): {bits[:300]}.")
            except Exception:
                logger.warning("loose return convert failed for %s",
                               task_id, exc_info=True)
                return
        elif out.get("ok") and out.get("action") == "decided":
            decision = out.get("decision") or {}
            latest = None
            try:
                latest = await self._repo.get_task(task_id)
            except Exception:
                latest = None
            try:
                held = await self._hold_decided_spend(
                    latest or current or ticket, decision.get("costs"),
                    out.get("spend") or {})
            except Exception:
                logger.warning("loose return spend hold failed for %s",
                               task_id, exc_info=True)
                held = None
            if held is not None:
                note = (f"Agent {resultado}: returned once to the planner, "
                        f"re-decided with declared spend, held for Owner "
                        f"approval. Motivo: {motivo}")
                self._wake_dispatcher()
                try:
                    await self.comment_on_ticket(task_id, note)
                except Exception:
                    logger.warning("loose return comment failed for %s",
                                   task_id, exc_info=True)
                return
            try:
                # Fresh read: the planner patched its decision above and
                # this requeue must keep it, not clobber it.
                latest = await self._repo.get_task(task_id) or {}
                params = dict(latest.get("params") or {})
                params["returns_from"] = task_id
                params.pop("agent_result", None)
                await self._repo.patch_task(task_id, {
                    "status": TaskStatus.QUEUED.value,
                    "waiting_reason": None,
                    "resume_at": None,
                    "error": None,
                    "params": params,
                })
            except Exception:
                logger.warning("loose return requeue failed for %s",
                               task_id, exc_info=True)
                return
            note = (f"Agent {resultado}: returned once to the planner, "
                    f"re-decided ({decision.get('target_agent')}/"
                    f"{decision.get('coding_tool') or '-'}), requeued. "
                    f"Motivo: {motivo}")
            self._wake_dispatcher()
        else:
            note = (f"Agent {resultado}: return replan failed "
                    f"({out.get('reason')}: "
                    f"{str(out.get('detail') or '')[:200]}); the ticket "
                    f"stays failed. Motivo: {motivo}")
        try:
            await self.comment_on_ticket(task_id, note)
        except Exception:
            logger.warning("loose return comment failed for %s", task_id,
                           exc_info=True)

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
