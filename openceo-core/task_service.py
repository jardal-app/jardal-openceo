from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from db.mongo import MongoRepository
from events import RealtimeHub
from graph import OpenCEOGraph
from horizon.privacy import is_personal_scope
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
    TaskReviewAction,
    TaskReviewActorType,
    TaskReviewCreateRequest,
    TaskStatus,
    WorkflowPhaseType,
    WorkflowRunStatus,
    make_task_document,
    make_task_review_document,
)
from tools.erpnext_client import ERPNextError, ERPNextUnavailable, ERPNextValidationError
from tools.erpnext_tickets import map_status

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
        self._locks: dict[str, asyncio.Lock] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._notion_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._notion_worker: asyncio.Task[None] | None = None
        self._erpnext_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._erpnext_worker: asyncio.Task[None] | None = None
        self._horizon_terminal: Any | None = None

    def set_horizon_hooks(self, *, on_task_terminal: Any | None = None) -> None:
        """Inyecta la reaccion de Horizon a un resultado de tarea.

        La dependencia apunta hacia afuera a proposito: Horizon no importa
        TaskService, es TaskService quien avisa. Sin gancho configurado el flujo
        de tickets funciona igual.
        """
        self._horizon_terminal = on_task_terminal

    async def _notify_horizon_terminal(self, task: dict[str, Any]) -> None:
        """Avisa a Horizon de una tarea personal terminada, sin poder romperla.

        Solo el alcance personal: una tarea de empresa no pertenece al espacio
        personal (seccion 9.3) y no debe replanificar la vida del usuario.
        """
        if self._horizon_terminal is None or task is None:
            return
        if not is_personal_scope(task):
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

    async def _register_business_ticket(self, doc: Any) -> tuple[dict[str, Any] | None, bool]:
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
        return (
            {
                "name": task.get("name"),
                "ref": ref,
                # `status_pushed` guarda SIEMPRE el vocabulario de OpenCEO, no el de
                # ERPNext: el observador compara contra el, y comparar `in_progress`
                # contra `Working` haria que cada parche pareciera una transicion nueva.
                "status_pushed": str(status_value or ""),
                "pending": False,
                "linked_at": now,
            },
            False,
        )

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
        if status == str(mirror.get("status_pushed") or ""):
            return
        if map_status(status) is None:
            # Estado que no se empuja (completed, failed, escalated, interrupted).
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

        delay = 1.0
        for attempt in range(3):
            try:
                await adapter.update_status(item["name"], item["status"])
            except ERPNextUnavailable as exc:
                if attempt == 2:
                    logger.warning(
                        "ERPNext no respondio al empujar %s -> %s: %s", item["name"], item["status"], exc
                    )
                    await self._record_status_outcome(item, pushed=False)
                    return
                await asyncio.sleep(delay)
                delay *= 2
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
        erpnext_mirror, erpnext_pending = await self._register_business_ticket(doc)
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

        if self._settings.TASK_AUTORUN and payload.autorun:
            try:
                if self._rate_monitor is not None:
                    can, resume_at = await self._maybe_gate_launch(created)
                    if not can:
                        logger.info(
                            "Rate limit gate: ticket %s queued until %s",
                            created["task_id"],
                            resume_at,
                        )
                        await self._repo.patch_task(
                            created["task_id"],
                            {
                                "status": TaskStatus.QUEUED.value,
                                "waiting_reason": "rate_limit_queued",
                                "resume_at": resume_at,
                            },
                        )
                        return created
                asyncio.create_task(self.run_task(created["task_id"]))
            except Exception:
                logger.exception("Rate monitor gate failed for ticket %s — launching anyway", created["task_id"])
                asyncio.create_task(self.run_task(created["task_id"]))

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
        key = resolve_template_key(
            templates,
            requested_key=doc.workflow_key,
            agent=agent,
            ticket_type=self._task_ticket_type(doc),
        )
        if not key:
            return

        template = templates[key]
        phases = normalize_phases(template.get("phases") or [])
        if not phases:
            return

        index = first_executable_phase_index(phases)
        doc.workflow_key = key
        doc.workflow_phases = phases
        doc.phase_index = index
        doc.phase = phase_key(phases[index])
        doc.phase_assignments = dict(doc.phase_assignments or {})
        doc.phase_started_at = datetime.now(timezone.utc)

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

        return await self.patch_task(task_id, updates)

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
        return {
            "ok": True,
            "status": "applied",
            "task_id": task.get("task_id"),
            "task_status": target,
        }

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
            if queue is not None and pending_status and map_status(status) is not None:
                queue.put_nowait(
                    {"task_id": task.get("task_id"), "name": mirror["name"], "status": status}
                )
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

            if task.get("status") == TaskStatus.COMPLETED.value and not force:
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
            # Preserve ticket-level tier/tool if the graph didn't return them
            # (e.g. error before _dev_node, or fallback path)
            graph_tier = graph_out.get("coding_tier")
            if graph_tier is not None:
                updates["coding_tier"] = graph_tier
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
            return updated

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
            if any(key in updates for key in {"approval_required", "approval_state"}):
                asyncio.create_task(self._fire_notion_approval_sync_back(updated))
            # Solo en la transicion a un estado terminal, no en cada parche
            # posterior: si no, un ticket ya cerrado replanificaria sin parar.
            if (
                str(updated.get("status")) in _TERMINAL_TASK_STATUSES
                and str(task.get("status")) != str(updated.get("status"))
            ):
                asyncio.create_task(self._notify_horizon_terminal(updated))
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

    async def resume_waiting_tasks(self) -> int:
        now_utc = datetime.now(timezone.utc)
        due = await self._repo.due_waiting_tasks(now_utc)

        resumed = 0
        for task in due:
            if not self._task_allows_autorun(task):
                continue
            task_id = task["task_id"]
            await self.patch_task(
                task_id,
                {
                    "status": TaskStatus.QUEUED.value,
                    "waiting_reason": None,
                    "resume_at": None,
                },
            )
            asyncio.create_task(self.run_task(task_id))
            resumed += 1

        return resumed

    async def resume_queued_tickets(self) -> int:
        """Resume queued tickets whose resume_at has passed AND rate limits allow."""
        if self._rate_monitor is None:
            return 0

        now_utc = datetime.now(timezone.utc)
        queued = await self._repo.list_tasks_by_status(TaskStatus.QUEUED)
        active = await self._repo.list_tasks_by_status(TaskStatus.IN_PROGRESS)
        active_projects = {str(ticket.get("project") or "default") for ticket in active}

        resumed = 0
        for ticket in queued:
            if not self._task_allows_autorun(ticket):
                continue
            project = str(ticket.get("project") or "default")
            if project in active_projects:
                continue
            resume_at = ticket.get("resume_at")
            if resume_at is not None and resume_at > now_utc:
                continue

            can, new_resume = await self._rate_monitor.can_launch_ticket(
                ticket, active_tickets=active,
            )
            if can:
                await self.patch_task(
                    ticket["task_id"],
                    {
                        "status": TaskStatus.QUEUED.value,
                        "waiting_reason": None,
                        "resume_at": None,
                    },
                )
                asyncio.create_task(self.run_task(ticket["task_id"]))
                resumed += 1
                active.append(
                    {
                        "task_id": ticket["task_id"],
                        "project": project,
                        "status": TaskStatus.IN_PROGRESS.value,
                    }
                )
                active_projects.add(project)
            elif new_resume:
                await self._repo.patch_task(
                    ticket["task_id"],
                    {"resume_at": new_resume},
                )

        return resumed

    async def _maybe_gate_launch(
        self, ticket: dict[str, Any]
    ) -> tuple[bool, datetime | None]:
        """Check rate limits before autorunning a ticket."""
        if self._rate_monitor is None:
            return True, None

        active = await self._repo.list_tasks_by_status(TaskStatus.IN_PROGRESS)
        return await self._rate_monitor.can_launch_ticket(
            ticket, active_tickets=active,
        )

    async def _on_ticket_done(self, ticket: dict[str, Any]) -> None:
        """Post-completion: QA gate, then refresh rate limits and launch queued tickets."""
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
            launched = await self.resume_queued_tickets()
            if launched:
                logger.info("Post-completion: launched %d queued ticket(s)", launched)
        except Exception:
            logger.warning("_on_ticket_done error", exc_info=True)

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
