from __future__ import annotations

import inspect
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yaml
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from db.mongo import MongoRepository
from events import RealtimeHub
from horizon import privacy as privacy_module
from settings import Settings, get_settings
from state import DiscoveryRunStatus, TaskStatus, WorkflowRunStatus
from tool_locks import (
    LOCK_WAITING_PREFIX,
    ToolLockError,
    is_lock_pending,
)

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"{{\s*([^}]+?)\s*}}")
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class WorkflowResolution:
    workflow: dict[str, Any]
    tier: str
    reason: str


@dataclass(frozen=True)
class WorkflowExecutionOutcome:
    selected_agent: str
    status: str
    steps: list[dict[str, Any]]
    last_response_summary: str | None = None
    waiting_reason: str | None = None
    error: str | None = None
    params: dict[str, Any] | None = None
    approval_required: bool | None = None
    approval_state: str | None = None


class WorkflowToolRegistry:
    """Runtime registry for deterministic YAML workflow tools."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        repo: MongoRepository | None = None,
        task_service: Any | None = None,
    ) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}
        self._settings = settings or get_settings()
        self._repo = repo
        self._task_service = task_service
        self._horizon: Any | None = None
        self._research: Any | None = None
        self._research_bridge: Any | None = None
        self._research_watch: Any | None = None
        self._register_builtin_tools()

    def set_task_service(self, task_service: Any) -> None:
        self._task_service = task_service

    def set_repo(self, repo: MongoRepository) -> None:
        self._repo = repo

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                "name": name,
                "signature": self._signature_text(fn),
                "doc": inspect.getdoc(fn) or "",
            }
            for name, fn in sorted(self._tools.items())
        }

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    async def call(self, name: str, params: dict[str, Any], context: dict[str, Any]) -> Any:
        fn = self._tools.get(name)
        if fn is None:
            raise MissingWorkflowToolError(f"Tool not registered: {name}")

        kwargs = self._build_kwargs(fn, params, context)
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _register_builtin_tools(self) -> None:
        # Core (check_registry removed in TASK-2026-00199: the agent
        # catalog replaces it; pm.* removed: tickets go through the MCP).
        self.register("core.echo", self._core_echo)
        self.register("core.complete", self._core_complete)
        self.register("core.query_tasks", self._core_query_tasks)
        self.register("core.query_workflow_runs", self._core_query_workflow_runs)

        # Secretary
        self.register("secretary.send_email", self._secretary_send_email)
        self.register("secretary.list_sender_accounts", self._secretary_list_sender_accounts)

        # Finance
        self.register("finance.pago_elfec_house", self._finance_pago_elfec_house)
        self.register("finance.register_expense", self._finance_register_expense)
        self.register("finance.register_income", self._finance_register_income)
        self.register("finance.get_scope_summary", self._finance_get_scope_summary)
        self.register("finance.get_contribution_10_status", self._finance_get_contribution_10_status)

        # CEO / Horizon (espacio personal). Nombres del contrato de la seccion
        # 9.2 punto 3; todos delegan en HorizonService, que es deterministico.
        self.register("ceo.plan_day", self._ceo_plan_day)
        self.register("ceo.review_objectives", self._ceo_review_objectives)
        self.register("ceo.get_next_action", self._ceo_get_next_action)
        self.register("ceo.capture_context", self._ceo_capture_context)
        self.register("ceo.record_progress", self._ceo_record_progress)

        # Operations project creation left the registry in TASK-2026-00223:
        # the operations skill owns it (`skills/operations/crear-proyecto`,
        # CLI over `ops.create_project` plus the ERPNext-project and DNS
        # boundary tools). No `ops.*` tool remains registered.

    def set_horizon_service(self, horizon_service: Any) -> None:
        self._horizon = horizon_service

    def set_research_service(self, research_service: Any) -> None:
        self._research = research_service
        self.register("research.investigate", self._research_investigate)

    def set_research_bridge(self, bridge: Any) -> None:
        """Mount the ticket bridge. Without it these tools do not exist."""
        self._research_bridge = bridge
        self.register("research.submit_job", self._research_submit_job)
        self.register("research.get_job", self._research_get_job)

    async def _research_submit_job(
        self,
        project: str = "",
        ticket: str = "",
        purpose: str = "",
        question: str = "",
        mode: str = "quick",
        seed_urls: list[str] | None = None,
        idempotency_key: str | None = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Admits one investigation under the ticket's consumer (TASK-2026-00222)."""
        if self._research_bridge is None:
            raise RuntimeError("Puente de research no montado")
        return await self._research_bridge.submit(
            ticket=ticket, project=project, purpose=purpose,
            question=question, mode=mode, seed_urls=seed_urls,
            idempotency_key=idempotency_key)

    async def _research_get_job(
        self,
        project: str = "",
        ticket: str = "",
        research_id: str = "",
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Status of one of the ticket's own investigations (TASK-2026-00222)."""
        if self._research_bridge is None:
            raise RuntimeError("Puente de research no montado")
        return await self._research_bridge.get(
            ticket=ticket, project=project, research_id=research_id)

    async def _research_investigate(
        self, request: dict[str, Any], context: dict[str, Any],
    ) -> dict[str, Any]:
        """Admite un job quick por ocurrencia con scope autenticado; no crea Tasks."""
        from research.workflows import admit_investigation
        return await admit_investigation(self._research, request, context)

    def set_research_watch(self, engine: Any) -> None:
        """Monta los ticks de suscripcion. Sin motor no hay tools: nada que fingir."""
        self._research_watch = engine
        self.register("research.watch_tick", self._research_watch_tick)
        self.register("research.discovery_tick", self._research_discovery_tick)

    async def _research_watch_tick(
        self, subscription: dict[str, Any], context: dict[str, Any],
    ) -> dict[str, Any]:
        """Lee una fuente, deduplica y admite un job solo si hay cambios pertinentes.

        Sin cambios pertinentes no llama al modelo ni crea Tasks. El cursor avanza
        despues de persistir los eventos, con fencing del schedule.
        """
        from research.execution import require_research_context
        execution = require_research_context(context)
        if self._research_watch is None:
            raise RuntimeError("Motor de vigilancia research no montado")
        return await self._research_watch.watch_tick(execution, subscription)

    async def _research_discovery_tick(
        self, subscription: dict[str, Any], context: dict[str, Any],
    ) -> dict[str, Any]:
        """Busca fuentes nuevas con cupo diario propio; admite un job solo si hay nuevas."""
        from research.execution import require_research_context
        execution = require_research_context(context)
        if self._research_watch is None:
            raise RuntimeError("Motor de vigilancia research no montado")
        return await self._research_watch.discovery_tick(execution, subscription)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        """Interpreta un entero tolerando el templating del motor YAML.

        ``_resolve_value`` convierte un parametro ausente en ``""`` (o en ``None``
        cuando ocupa todo el valor). Leer ese ``""`` como cero convertiria "no lo
        se" en "dia saturado", que es justo lo contrario.
        """
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _resolve_horizon_owner(
        self, user_id: Any, context: dict[str, Any] | None
    ) -> str | None:
        """Resuelve el propietario del espacio personal. Nunca lo adivina.

        Orden: parametro explicito, propietario de la tarea de contexto, y nada
        mas. Un workflow no puede elegir de quien es el espacio personal.
        """
        explicit = str(user_id or "").strip()
        if explicit:
            return explicit
        task = (context or {}).get("task") or {}
        owner = privacy_module.personal_owner(task)
        return str(owner) if owner else None

    async def _ceo_plan_day(
        self,
        *,
        user_id: Any = None,
        date: Any = None,
        free_minutes: Any = None,
        exercise_included: Any = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Prepara y publica el plan del dia del espacio personal."""
        if self._horizon is None:
            return {"ok": False, "reason": "horizon_unavailable"}
        owner = self._resolve_horizon_owner(user_id, context)
        if not owner:
            return {"ok": False, "reason": "not_personal_scope"}

        date_key = str(date).strip() or None if date else None
        plan = await self._horizon.plan_day(
            owner,
            declared_free_minutes=self._coerce_int(free_minutes),
            exercise_included=(
                None if exercise_included in (None, "") else bool(exercise_included)
            ),
            date_key=date_key,
        )
        return {
            "ok": True,
            "plan_id": plan.get("plan_id"),
            "version": plan.get("version"),
            "status": plan.get("status"),
            "message": plan.get("message"),
            "growth_budget_minutes": (plan.get("capacity") or {}).get(
                "growth_budget_minutes"
            ),
        }

    async def _ceo_review_objectives(
        self,
        *,
        user_id: Any = None,
        kind: Any = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Revisa resultados, desviaciones y carga del espacio personal."""
        if self._horizon is None:
            return {"ok": False, "reason": "horizon_unavailable"}
        owner = self._resolve_horizon_owner(user_id, context)
        if not owner:
            return {"ok": False, "reason": "not_personal_scope"}
        review = await self._horizon.review_objectives(
            owner, kind=str(kind or "weekly_review")
        )
        return {"ok": True, **review}

    async def _ceo_get_next_action(
        self,
        *,
        user_id: Any = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Devuelve la accion principal de hoy, con su motivo."""
        if self._horizon is None:
            return {"ok": False, "reason": "horizon_unavailable"}
        owner = self._resolve_horizon_owner(user_id, context)
        if not owner:
            return {"ok": False, "reason": "not_personal_scope"}
        result = await self._horizon.next_action(owner)
        return {"ok": True, **result}

    async def _ceo_capture_context(
        self,
        *,
        user_id: Any = None,
        text: Any = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Registra una idea, pendiente o avance en el espacio personal."""
        if self._horizon is None:
            return {"ok": False, "reason": "horizon_unavailable"}
        owner = self._resolve_horizon_owner(user_id, context)
        if not owner:
            return {"ok": False, "reason": "not_personal_scope"}
        result = await self._horizon.capture(owner, str(text or ""))
        return {"ok": True, **result}

    async def _ceo_record_progress(
        self,
        *,
        user_id: Any = None,
        text: Any = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Registra un avance medible con evidencia y deduplicacion."""
        if self._horizon is None:
            return {"ok": False, "reason": "horizon_unavailable"}
        owner = self._resolve_horizon_owner(user_id, context)
        if not owner:
            return {"ok": False, "reason": "not_personal_scope"}
        result = await self._horizon.record_progress(owner, str(text or ""))
        return {"ok": True, **result}

    @staticmethod
    def _core_echo(**params: Any) -> dict[str, Any]:
        """Echoes back the parameters passed to it."""
        return dict(params)

    @staticmethod
    def _core_complete(**params: Any) -> dict[str, Any]:
        """Marks a workflow branch as completed with the given parameters."""
        return {"status": "completed", **params}

    async def _core_query_tasks(
        self,
        project: str | None = None,
        status: str | None = None,
        agent: str | None = None,
        limit: int = 10,
        **_: Any,
    ) -> list[dict[str, Any]]:
        """Queries tasks in MongoDB with filters (project, status, agent, limit)."""
        if not self._repo:
            return []
        query: dict[str, Any] = {}
        if project:
            query["project"] = project
        if status:
            query["status"] = status
        if agent:
            query["$or"] = [{"agent": agent}, {"agent_hint": agent}]
        cursor = self._repo.tasks.find(query).sort("created_at", -1).limit(max(1, min(limit, 50)))
        results: list[dict[str, Any]] = []
        async for doc in cursor:
            results.append({
                "task_id": doc.get("task_id"),
                "ticket_number": doc.get("ticket_number"),
                "title": doc.get("title"),
                "status": doc.get("status"),
                "agent": doc.get("agent"),
                "project": doc.get("project"),
                "priority": doc.get("priority"),
            })
        return results

    async def _core_query_workflow_runs(
        self,
        status: str | None = None,
        workflow: str | None = None,
        task_id: str | None = None,
        limit: int = 10,
        **_: Any,
    ) -> list[dict[str, Any]]:
        """Queries workflow run records in MongoDB with filters (status, workflow, task_id, limit)."""
        if not self._repo:
            return []
        runs = await self._repo.list_workflow_runs(task_id=task_id, status=status, limit=max(1, min(limit, 50)))
        if workflow:
            runs = [r for r in runs if str(r.get("workflow") or "") == workflow]
        return [
            {
                "run_id": r.get("run_id"),
                "task_id": r.get("task_id"),
                "workflow": r.get("workflow"),
                "status": r.get("status"),
                "tier": r.get("tier"),
                "created_at": str(r.get("created_at")),
            }
            for r in runs
        ]

    async def _secretary_send_email(
        self,
        context: dict[str, Any] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Boundary (a)+(b): SMTP send with the Owner lock inside (00208)."""
        task = (context or {}).get("task") or {}
        if self._task_service is None:
            return {"ok": False, "status": "unavailable",
                    "message": "Email needs the Owner lock; "
                             "TaskService is not wired."}
        return await self._task_service.tool_send_email(
            from_email=params.get("from_email"),
            to=params.get("to"),
            subject=params.get("subject") or "",
            body_text=params.get("body_text") or "",
            body_html=params.get("body_html"),
            cc=params.get("cc"),
            bcc=params.get("bcc"),
            reply_to=params.get("reply_to"),
            attachments=params.get("attachments"),
            language=params.get("language"),
            project=task.get("project"),
            origin_task_id=task.get("task_id"),
        )

    @staticmethod
    def _secretary_list_sender_accounts(**_: Any) -> dict[str, Any]:
        """Lists the configured sender addresses (no secrets)."""
        from agents.secretary.tools import list_sender_accounts
        return list_sender_accounts()

    async def _finance_pago_elfec_house(
        self,
        context: dict[str, Any] | None = None,
        **params: Any,
    ) -> dict[str, Any]:
        """Boundary (a)+(b): Elfec run with the Owner lock inside (00208)."""
        task = (context or {}).get("task") or {}
        if self._task_service is None:
            return {"ok": False, "status": "unavailable",
                    "message": "Elfec needs the Owner lock; "
                             "TaskService is not wired."}
        workflow_cfg = params.get("workflow")
        if not isinstance(workflow_cfg, dict):
            workflow_cfg = dict(params)
        return await self._task_service.tool_elfec_pay(
            workflow=dict(workflow_cfg),
            expected_row_label=str(params.get("expected_row_label") or ""),
            project=task.get("project"),
            origin_task_id=task.get("task_id"),
        )

    @staticmethod
    def _operation_ref(
        prefix: str,
        openceo_ref: str | None,
        context: dict[str, Any] | None,
    ) -> str | None:
        """Referencia idempotente del intento: mismo task -> mismo `openceo_ref`.

        El tool acunaba una referencia nueva por llamada, asi que el indice
        unico de ERPNext no podia frenar nunca un re-despacho (POST /run con
        `force`) ni un parche a Mongo perdido justo despues de la escritura:
        quedaban dos asientos sometidos, ambos con `duplicate=False` (contrato
        §6.1: "mismo intento/retry -> mismo valor").
        """
        explicit = (openceo_ref or "").strip()
        if explicit:
            return explicit
        task = (context or {}).get("task") or {}
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            return None
        return f"{prefix}:{task_id}"

    async def _finance_register_expense(
        self,
        text: str = "",
        openceo_ref: str | None = None,
        submit: bool = True,
        exchange_rate: str | None = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Registra un gasto ocasional por voz/chat en ERPNext (TASK-2026-00032).

        Crea un `Journal Entry` idempotente por `openceo_ref` y lo envia para
        que llegue al libro mayor. Sin tickets por gasto.
        """
        from agents.finance import voice_expense as _voice_expense
        from agents.finance.money_text import MissingExchangeRateError
        from tools.erpnext_client import ERPNextClient

        rate = _rate_or_none(exchange_rate)
        parsed = _voice_expense.parse_expense(text, exchange_rate=rate)
        if parsed is None:
            return {"ok": False, "status": "invalid_input",
                    "error": "no_amount",
                    "summary": (
                        "No encontré un monto válido para registrar. "
                        "Dime por ejemplo: 'registra gasto personal "
                        "carne 50 bolivianos'.")}
        ref = self._operation_ref("voice-expense", openceo_ref, context)
        client = ERPNextClient(self._settings)
        try:
            try:
                result = await _voice_expense.register_expense(
                    client, parsed, ref=ref, submit=bool(submit)
                )
            except MissingExchangeRateError as exc:
                return {
                    "ok": False,
                    "status": "invalid_input",
                    "error": "missing_exchange_rate",
                    "summary": str(exc),
                }
        finally:
            await client.aclose()
        doc = result["doc"]
        return {
            "ok": True,
            "duplicate": result["duplicate"],
            "ref": result["ref"],
            "journal_entry": doc.get("name"),
            "docstatus": doc.get("docstatus"),
            "summary": _voice_expense.build_spoken_summary(
                parsed, doc, result["duplicate"]
            ),
        }

    async def _finance_get_scope_summary(
        self,
        cost_center: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        text: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """Resumen de saldos/gasto por Scope (TASK-2026-00044). SOLO lectura
        sobre `GL Entry`: no crea ni modifica ningun documento.

        Explicit bounds win; else the dictated text sets them
        (TASK-2026-00221: the exact skill carries no LLM to compute them).
        """
        from agents.finance import scope_summary as _scope
        from tools.erpnext_client import ERPNextClient

        if not (date_from or date_to) and str(text or "").strip():
            date_from, date_to = _scope.period_bounds(text)
        client = ERPNextClient(self._settings)
        try:
            summary = await _scope.get_scope_summary(
                client,
                cost_center=cost_center or _scope.COST_CENTER,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            await client.aclose()
        payload = summary.to_dict()
        payload["ok"] = True
        payload["summary"] = _scope.build_spoken_summary(summary)
        return payload

    async def _finance_register_income(
        self,
        text: str = "",
        openceo_ref: str | None = None,
        submit: bool = True,
        exchange_rate: str | None = None,
        context: dict[str, Any] | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Registra un ingreso ya cobrado por voz/chat en ERPNext (TASK-2026-00053).

        Espejo de `finance.register_expense`: el mismo `Journal Entry`
        idempotente por `openceo_ref`, con las dos patas invertidas (debito a
        efectivo / credito a la cuenta de ingreso). Sin tickets por ingreso.
        """
        from agents.finance import voice_income as _voice_income
        from agents.finance.money_text import MissingExchangeRateError
        from tools.erpnext_client import ERPNextClient

        rate = _rate_or_none(exchange_rate)
        parsed = _voice_income.parse_income(text, exchange_rate=rate)
        if parsed is None:
            return {"ok": False, "status": "invalid_input",
                    "error": "no_amount",
                    "summary": (
                        "No encontré un monto válido para el ingreso. "
                        "Dime por ejemplo: 'registra ingreso sueldo "
                        "6200 bolivianos'.")}
        ref = self._operation_ref("voice-income", openceo_ref, context)
        client = ERPNextClient(self._settings)
        try:
            try:
                result = await _voice_income.register_income(
                    client, parsed, ref=ref, submit=bool(submit)
                )
            except MissingExchangeRateError as exc:
                return {
                    "ok": False,
                    "status": "invalid_input",
                    "error": "missing_exchange_rate",
                    "summary": str(exc),
                }
        finally:
            await client.aclose()
        doc = result["doc"]
        return {
            "ok": True,
            "duplicate": result["duplicate"],
            "ref": result["ref"],
            "journal_entry": doc.get("name"),
            "docstatus": doc.get("docstatus"),
            "summary": _voice_income.build_spoken_summary(
                parsed, doc, result["duplicate"]
            ),
        }

    async def _finance_get_contribution_10_status(
        self,
        date_from: str | None = None,
        date_to: str | None = None,
        cost_center: str | None = None,
        text: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """Estado de la regla del 10% (TASK-2026-00054). SOLO lectura sobre
        `GL Entry`: no crea ni modifica ningun documento.

        Explicit bounds win; else the dictated text sets them
        (mismo criterio que el resumen, TASK-2026-00221/00044).
        """
        from agents.finance import contribution as _contribution
        from agents.finance import scope_summary as _scope
        from tools.erpnext_client import ERPNextClient

        if not (date_from or date_to) and str(text or "").strip():
            date_from, date_to = _scope.period_bounds(text)
        client = ERPNextClient(self._settings)
        try:
            status = await _contribution.get_contribution_10_status(
                client,
                cost_center=cost_center or _contribution.COST_CENTER,
                date_from=date_from,
                date_to=date_to,
            )
        finally:
            await client.aclose()
        payload = status.to_dict()
        payload["ok"] = True
        payload["summary"] = _contribution.build_spoken_summary(status)
        return payload

    @staticmethod
    def _build_kwargs(fn: Callable[..., Any], params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        signature = inspect.signature(fn)
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        kwargs = dict(params)
        if "context" in signature.parameters:
            kwargs["context"] = context
        if accepts_kwargs:
            return kwargs
        return {key: value for key, value in kwargs.items() if key in signature.parameters}

    @staticmethod
    def _signature_text(fn: Callable[..., Any]) -> str:
        try:
            return str(inspect.signature(fn))
        except (TypeError, ValueError):
            return "(...)"


class MissingWorkflowToolError(RuntimeError):
    pass


class WorkflowService:
    """YAML workflow catalog, deterministic executor, and discovery state."""

    def __init__(
        self,
        *,
        settings: Settings,
        repo: MongoRepository,
        realtime_hub: RealtimeHub,
        tool_registry: WorkflowToolRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._hub = realtime_hub
        self._tools = tool_registry or WorkflowToolRegistry(settings=settings, repo=repo)
        self._llm: ChatOpenAI | None = None
        if settings.DEEPSEEK_API_KEY:
            self._llm = ChatOpenAI(
                model=settings.CEO_LLM_MODEL,
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.CEO_LLM_BASE_URL,
                temperature=0,
            )

    def set_task_service(self, task_service: Any) -> None:
        self._tools.set_task_service(task_service)

    def set_horizon_service(self, horizon_service: Any) -> None:
        self._tools.set_horizon_service(horizon_service)

    def set_research_service(self, research_service: Any) -> None:
        self._tools.set_research_service(research_service)

    def set_research_watch(self, engine: Any) -> None:
        self._tools.set_research_watch(engine)

    # Workflows research ejecutables por el runner de schedules: archivo YAML y nombre
    # del unico parametro de su unico paso. El nombre del workflow es el de su tool.
    _RESEARCH_WORKFLOWS = {
        "research.investigate": ("investigate.yaml", "request"),
        "research.watch_tick": ("watch_tick.yaml", "subscription"),
        "research.discovery_tick": ("discovery_tick.yaml", "subscription"),
    }

    async def execute_research_workflow(
        self, workflow_id: str, params: dict[str, Any], *, context: dict[str, Any],
    ) -> dict[str, Any]:
        """Runner de schedules, separado de execute_task_workflow y core.complete.

        Solo los YAML conocidos (admision, vigilancia y descubrimiento), cada uno con un
        unico paso deterministico. No escribe workflow_runs de negocio ni usa un LLM para
        decidir que hacer: el modelo solo corre despues, sobre un job ya admitido.
        """
        from research.workflows import require_research_context
        require_research_context(context)
        entry = self._RESEARCH_WORKFLOWS.get(workflow_id)
        if entry is None:
            raise ValueError("Workflow research no soportado")
        filename, param_name = entry
        path = self.workflows_dir / "research" / filename
        workflow = self._parse_workflow_yaml(path.read_text(encoding="utf-8"), source=str(path))
        steps = workflow["steps"]
        if (workflow["workflow"] != workflow_id or workflow.get("auto_match")
                or workflow.get("triggers") or len(steps) != 1
                or steps[0].get("tool") != workflow_id
                or steps[0].get("type", "tool") != "tool"):
            raise ValueError("El YAML research debe contener solo su tool de admision")
        resolved = _resolve_value(steps[0].get("params") or {}, {**context, "research_request": params})
        if resolved != {param_name: params}:
            raise ValueError("El YAML research no puede reemplazar los parametros de la ocurrencia")
        return await self._tools.call(workflow_id, resolved, context)

    async def resume_blocked_run_for_task(
        self,
        task_id: str,
        *,
        action: str = "approve",
        actor_id: str = "admin",
    ) -> WorkflowExecutionOutcome | None:
        run = await self._repo.latest_workflow_run_for_task(task_id)
        if not run or str(run.get("status") or "") != WorkflowRunStatus.BLOCKED_HUMAN.value:
            return None
        workflow_id = str(run.get("workflow") or "")
        workflows = self._load_workflows()
        workflow = self._find_workflow(workflows, workflow_id)
        if not workflow:
            return None

        curr_index = int(run.get("current_step_index") or 0)
        patched_run = await self._repo.patch_workflow_run(
            str(run["run_id"]),
            {
                "current_step_index": curr_index + 1,
                "current_step_id": None,
                "waiting": None,
            },
        )
        run = patched_run or run
        outcome = await self._continue_workflow_run(run, workflow)
        task = await self._repo.get_task(task_id)
        if task:
            await self._apply_outcome_to_task(task, outcome)
        return outcome

    @property
    def workflows_dir(self) -> Path:
        return Path(self._settings.WORKFLOWS_DIR).expanduser().resolve()

    def list_workflows(self, *, include_steps: bool = False) -> list[dict[str, Any]]:
        workflows: list[dict[str, Any]] = []
        for workflow in self._load_workflows().values():
            item = deepcopy(workflow)
            if not include_steps:
                item.pop("steps", None)
            workflows.append(item)
        return sorted(workflows, key=lambda item: str(item.get("workflow") or ""))

    def tool_registry(self) -> dict[str, dict[str, Any]]:
        return self._tools.describe()

    async def run_for_task(self, task: dict[str, Any]) -> WorkflowExecutionOutcome | None:
        resolution = self.resolve_task_workflow(task)
        if resolution is None:
            if self._should_start_discovery(task):
                return await self.start_discovery_for_task(
                    task,
                    reason="No crystallized workflow matched this task.",
                )
            return None
        return await self.execute_task_workflow(task, resolution.workflow, resolution=resolution)

    def resolve_task_workflow(self, task: dict[str, Any]) -> WorkflowResolution | None:
        workflows = self._load_workflows()
        if not workflows:
            return None

        params = _task_params(task)
        explicit = _first_text(
            params.get("workflow"),
            params.get("workflow_id"),
            params.get("workflow_ref"),
            params.get("_workflow", {}).get("workflow") if isinstance(params.get("_workflow"), dict) else None,
        )
        if explicit:
            workflow = self._find_workflow(workflows, explicit)
            if workflow is not None:
                return WorkflowResolution(workflow=workflow, tier="tier0", reason=f"explicit workflow={explicit}")
            return None

        if not _truthy(params.get("use_workflow")):
            return None

        text = " ".join(
            part.strip().lower()
            for part in [
                str(task.get("title") or ""),
                str(task.get("description") or ""),
                str(task.get("agent_hint") or ""),
            ]
            if part and str(part).strip()
        )
        scored: list[tuple[int, dict[str, Any], str]] = []
        for workflow in workflows.values():
            score, reason = self._score_workflow(workflow, text)
            if score > 0:
                scored.append((score, workflow, reason))
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return None
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return WorkflowResolution(workflow=scored[0][1], tier="tier1", reason=scored[0][2])

    async def latest_workflow_outputs(self, task_id: str) -> dict[str, Any]:
        """Step outputs of the latest run for one task, {} when none."""
        run = await self._repo.latest_workflow_run_for_task(task_id)
        if not isinstance(run, dict):
            return {}
        outputs = run.get("outputs")
        return dict(outputs) if isinstance(outputs, dict) else {}

    async def execute_task_workflow(
        self,
        task: dict[str, Any],
        workflow: dict[str, Any],
        *,
        resolution: WorkflowResolution | None = None,
    ) -> WorkflowExecutionOutcome:
        latest = await self._repo.latest_workflow_run_for_task(str(task["task_id"]))
        workflow_id = str(workflow["workflow"])
        if latest and latest.get("workflow") == workflow_id:
            latest_status = str(latest.get("status") or "")
            if latest_status in {WorkflowRunStatus.RUNNING.value, WorkflowRunStatus.PENDING.value}:
                return self._running_outcome(task, workflow, latest)

        now = _utc_now()
        run_doc = {
            "run_id": str(uuid4()),
            "task_id": task["task_id"],
            "project": task.get("project"),
            "workflow": workflow_id,
            "workflow_name": workflow.get("name") or workflow_id,
            "agent": workflow.get("agent") or _agent_from_workflow_id(workflow_id),
            "status": WorkflowRunStatus.PENDING.value,
            "tier": resolution.tier if resolution else "direct",
            "resolution_reason": resolution.reason if resolution else "direct execution",
            "current_step_index": 0,
            "current_step_id": None,
            "steps": [],
            "outputs": {},
            "context": {
                "task": _task_context(task),
                "params": _task_params(task),
                "outputs": {},
                "steps": {},
            },
            "waiting": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        run = await self._repo.create_workflow_run(run_doc)
        await self._hub.broadcast("workflow_run_created", run)
        return await self._continue_workflow_run(run, workflow)

    async def start_discovery_for_task(
        self,
        task: dict[str, Any],
        *,
        reason: str,
    ) -> WorkflowExecutionOutcome:
        proposal = await self._build_discovery_proposal(task)
        status = DiscoveryRunStatus.PROPOSED if proposal else DiscoveryRunStatus.PENDING_APPROVAL
        discovery_doc = {
            "discovery_id": str(uuid4()),
            "task_id": task["task_id"],
            "project": task.get("project"),
            "status": status.value,
            "reason": reason,
            "available_tools": self._tools.names(),
            "proposed_workflow": proposal,
            "review": None,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "completed_at": None,
        }
        discovery = await self._repo.create_discovery_run(discovery_doc)
        await self._hub.broadcast("discovery_run_created", discovery)

        params = _task_params(task)
        params["_discovery"] = {
            "discovery_id": discovery["discovery_id"],
            "status": discovery["status"],
        }
        return WorkflowExecutionOutcome(
            selected_agent=str(task.get("agent") or task.get("agent_hint") or "ceo"),
            status=TaskStatus.WAITING.value,
            steps=[
                _task_step(
                    "workflow_discovery",
                    "pending_approval",
                    "No hay workflow cristalizado. Revise y apruebe la propuesta de discovery.",
                )
            ],
            waiting_reason="workflow_discovery_pending_approval",
            last_response_summary="Discovery creado y pendiente de aprobacion humana.",
            params=params,
            approval_required=True,
            approval_state="pending",
        )

    async def approve_discovery_run(
        self,
        discovery_id: str,
        *,
        actor_id: str,
        approve: bool,
        comment: str = "",
        execute: bool = False,
        workflow_yaml: str | None = None,
    ) -> dict[str, Any]:
        discovery = await self._repo.get_discovery_run(discovery_id)
        if discovery is None:
            raise ValueError("Discovery run not found")

        if not approve:
            updated = await self._repo.patch_discovery_run(
                discovery_id,
                {
                    "status": DiscoveryRunStatus.REJECTED.value,
                    "review": {"actor_id": actor_id, "comment": comment, "approved": False},
                    "completed_at": _utc_now(),
                },
            )
            return {"discovery": updated, "workflow": None, "task": None}

        raw_yaml = workflow_yaml or str(discovery.get("proposed_workflow") or "").strip()
        workflow = self._parse_workflow_yaml(raw_yaml, source="<discovery>")
        saved_path = self._save_workflow_yaml(raw_yaml, workflow)
        updated = await self._repo.patch_discovery_run(
            discovery_id,
            {
                "status": DiscoveryRunStatus.APPROVED.value,
                "review": {
                    "actor_id": actor_id,
                    "comment": comment,
                    "approved": True,
                    "workflow_path": str(saved_path),
                },
                "completed_at": _utc_now(),
            },
        )

        task_result = None
        if execute:
            task = await self._repo.get_task(str(discovery.get("task_id") or ""))
            if task is not None:
                outcome = await self.execute_task_workflow(task, workflow)
                task_result = await self._apply_outcome_to_task(task, outcome)
        return {"discovery": updated, "workflow": workflow, "task": task_result}

    async def _continue_workflow_run(
        self,
        run: dict[str, Any],
        workflow: dict[str, Any],
    ) -> WorkflowExecutionOutcome:
        run = await self._repo.patch_workflow_run(
            str(run["run_id"]),
            {"status": WorkflowRunStatus.RUNNING.value},
        ) or run
        await self._hub.broadcast("workflow_run_updated", run)

        context = dict(run.get("context") or {})
        context.setdefault("outputs", dict(run.get("outputs") or {}))
        context.setdefault("steps", dict(context.get("outputs") or {}))
        context.setdefault("workflow", {"id": workflow.get("workflow"), "name": workflow.get("name")})

        steps = list(run.get("steps") or [])
        outputs = dict(run.get("outputs") or {})
        start_index = int(run.get("current_step_index") or 0)

        try:
            result = await self._execute_steps(
                workflow=workflow,
                steps_def=list(workflow.get("steps") or []),
                run=run,
                context=context,
                outputs=outputs,
                events=steps,
                start_index=start_index,
            )
        except MissingWorkflowToolError as exc:
            updated_run = await self._repo.patch_workflow_run(
                str(run["run_id"]),
                {
                    "status": WorkflowRunStatus.BLOCKED_TOOL.value,
                    "error": str(exc),
                    "steps": steps + [_run_event("workflow_tool", "blocked_tool", str(exc))],
                },
            )
            return self._run_outcome(
                updated_run or run,
                workflow,
                task_status=TaskStatus.WAITING.value,
                waiting_reason="workflow_blocked_tool",
                error=str(exc),
            )
        except Exception as exc:
            logger.warning("Workflow run %s failed", run.get("run_id"), exc_info=True)
            updated_run = await self._repo.patch_workflow_run(
                str(run["run_id"]),
                {
                    "status": WorkflowRunStatus.FAILED.value,
                    "error": str(exc),
                    "steps": steps + [_run_event("workflow", "failed", str(exc))],
                    "completed_at": _utc_now(),
                },
            )
            return self._run_outcome(
                updated_run or run,
                workflow,
                task_status=TaskStatus.FAILED.value,
                error=str(exc),
            )

        if result.get("blocked"):
            blocked_reason = result.get("blocked_reason")
            if blocked_reason == "tool_lock":
                pending = result.get("pending") \
                    if isinstance(result.get("pending"), dict) else {}
                params = dict((run.get("context") or {}).get("params") or {})
                params["_workflow"] = {
                    "run_id": run.get("run_id"),
                    "workflow": workflow.get("workflow"),
                    "status": WorkflowRunStatus.BLOCKED_TOOL.value,
                }
                if pending.get("question_task_id"):
                    params["owner_question_id"] = pending.get(
                        "question_task_id")
                reason = str(pending.get("waiting_reason") or "")
                if not reason.startswith(LOCK_WAITING_PREFIX):
                    reason = f"{LOCK_WAITING_PREFIX}tool"
                return WorkflowExecutionOutcome(
                    selected_agent=str(workflow.get("agent") or _agent_from_workflow_id(str(workflow.get("workflow") or ""))),
                    status=TaskStatus.WAITING.value,
                    steps=[
                        _task_step(str(item.get("id") or "workflow_step"), str(item.get("status") or ""), item.get("detail"))
                        for item in (result.get("events") or [])
                    ],
                    last_response_summary=(
                        f"Candado ({pending.get('tool') or result.get('tool') or 'tool'}): "
                        "pendiente de aprobacion del dueno."
                    ),
                    waiting_reason=reason,
                    params=params,
                )
            if blocked_reason == "human_in_the_loop":
                perm = str(result.get("permission") or "qa_review")
                prompt = str(result.get("prompt") or "")
                params = dict((run.get("context") or {}).get("params") or {})
                params["_workflow"] = {
                    "run_id": run.get("run_id"),
                    "workflow": workflow.get("workflow"),
                    "status": WorkflowRunStatus.BLOCKED_HUMAN.value,
                }
                return WorkflowExecutionOutcome(
                    selected_agent=str(workflow.get("agent") or _agent_from_workflow_id(str(workflow.get("workflow") or ""))),
                    status=TaskStatus.WAITING.value,
                    steps=[
                        _task_step(str(item.get("id") or "workflow_step"), str(item.get("status") or ""), item.get("detail"))
                        for item in (result.get("events") or [])
                    ],
                    last_response_summary=f"Esperando aprobacion humana ({perm}): {prompt}",
                    waiting_reason=f"phase:{perm}",
                    approval_required=True,
                    approval_state="pending",
                    params=params,
                )

        updated_run = await self._repo.patch_workflow_run(
            str(run["run_id"]),
            {
                "status": WorkflowRunStatus.COMPLETED.value,
                "current_step_index": len(workflow.get("steps") or []),
                "current_step_id": None,
                "steps": result["events"],
                "outputs": result["outputs"],
                "context": context,
                "error": None,
                "completed_at": _utc_now(),
            },
        )
        await self._hub.broadcast("workflow_run_updated", updated_run)
        return self._run_outcome(
            updated_run or run,
            workflow,
            task_status=TaskStatus.COMPLETED.value,
        )

    async def _execute_steps(
        self,
        *,
        workflow: dict[str, Any],
        steps_def: list[dict[str, Any]],
        run: dict[str, Any],
        context: dict[str, Any],
        outputs: dict[str, Any],
        events: list[dict[str, Any]],
        start_index: int = 0,
    ) -> dict[str, Any]:
        index = start_index
        while index < len(steps_def):
            step = steps_def[index]
            step_id = _step_id(step, index)
            if not _evaluate_when(step.get("when"), context):
                events.append(_run_event(step_id, "skipped", "when condition evaluated false"))
                index += 1
                continue

            await self._repo.patch_workflow_run(
                str(run["run_id"]),
                {
                    "current_step_index": index,
                    "current_step_id": step_id,
                    "steps": events,
                    "outputs": outputs,
                    "context": context,
                },
            )

            if step.get("ref"):
                ref_id = str(step["ref"]).strip()
                ref_workflow = self._load_workflows().get(ref_id)
                if ref_workflow is None:
                    raise MissingWorkflowToolError(f"Referenced workflow not found: {ref_id}")
                ref_context = deepcopy(context)
                ref_params = _resolve_value(step.get("params") or {}, context)
                ref_context["params"] = ref_params if isinstance(ref_params, dict) else {}
                ref_result = await self._execute_steps(
                    workflow=ref_workflow,
                    steps_def=list(ref_workflow.get("steps") or []),
                    run=run,
                    context=ref_context,
                    outputs={},
                    events=[],
                    start_index=0,
                )
                if ref_result.get("blocked"):
                    return ref_result
                outputs[step_id] = ref_result["outputs"]
                _sync_context_outputs(context, outputs)
                events.append(_run_event(step_id, "ok", f"Referenced workflow executed: {ref_id}", outputs[step_id]))
                index += 1
                continue

            step_type = str(step.get("type") or "tool").strip().lower()
            if step_type == "human_in_the_loop":
                permission = str(step.get("permission") or "qa_review").strip()
                prompt = str(step.get("prompt") or step.get("description") or f"Aprobacion requerida: {permission}")
                events.append(_run_event(step_id, "waiting_approval", prompt))
                await self._repo.patch_workflow_run(
                    str(run["run_id"]),
                    {
                        "status": WorkflowRunStatus.BLOCKED_HUMAN.value,
                        "current_step_index": index,
                        "current_step_id": step_id,
                        "steps": events,
                        "outputs": outputs,
                        "context": context,
                        "waiting": {"permission": permission, "prompt": prompt},
                    },
                )
                return {
                    "blocked": True,
                    "blocked_reason": "human_in_the_loop",
                    "permission": permission,
                    "prompt": prompt,
                    "events": events,
                    "outputs": outputs,
                }

            if step_type == "loop":
                loop_result = await self._execute_loop(step, workflow, run, context, outputs, events)
                if loop_result.get("blocked"):
                    return loop_result
                outputs[step_id] = loop_result["outputs"]
                _sync_context_outputs(context, outputs)
                index += 1
                continue

            tool_name = str(step.get("tool") or "").strip()
            if not tool_name:
                raise ValueError(f"Workflow step '{step_id}' does not define a tool")
            resolved_params = _resolve_value(step.get("params") or {}, context)
            if not isinstance(resolved_params, dict):
                raise ValueError(f"Workflow step '{step_id}' params must resolve to an object")

            result = await self._tools.call(tool_name, resolved_params, context)
            outputs[step_id] = result
            _sync_context_outputs(context, outputs)
            if isinstance(result, dict) and result.get("status") in {
                    "denied", "invalid_input", "unavailable"}:
                # A terminal tool state fails the run like the sessions
                # do: later steps must never act as if the refused send,
                # delete, payment or booking had happened. The refused
                # output stays on the record: voice reads its summary back.
                reason = str(result.get("reason")
                             or result.get("message")
                             or result.get("error") or "").strip()
                events.append(_run_event(
                    step_id, "failed",
                    f"{tool_name} ended: {result.get('status')}",
                    result))
                raise ToolLockError(
                    f"{tool_name} ended: "
                    f"{result.get('status')}"
                    f"{(': ' + reason) if reason else ''}")
            if is_lock_pending(result):
                detail = (f"Candado ({result.get('tool') or tool_name}): "
                          f"pendiente de aprobacion del dueno.")
                events.append(_run_event(step_id, "waiting_approval", detail))
                await self._repo.patch_workflow_run(
                    str(run["run_id"]),
                    {
                        "status": WorkflowRunStatus.BLOCKED_TOOL.value,
                        "current_step_index": index,
                        "current_step_id": step_id,
                        "steps": events,
                        "outputs": outputs,
                        "context": context,
                    },
                )
                return {
                    "blocked": True,
                    "blocked_reason": "tool_lock",
                    "tool": tool_name,
                    "pending": result,
                    "events": events,
                    "outputs": outputs,
                }
            events.append(_run_event(step_id, "ok", f"Tool executed: {tool_name}", result))
            await self._repo.patch_workflow_run(
                str(run["run_id"]),
                {
                    "current_step_index": index + 1,
                    "current_step_id": step_id,
                    "steps": events,
                    "outputs": outputs,
                    "context": context,
                },
            )
            index += 1

        return {"blocked": False, "events": events, "outputs": outputs}

    async def _execute_loop(
        self,
        step: dict[str, Any],
        workflow: dict[str, Any],
        run: dict[str, Any],
        context: dict[str, Any],
        parent_outputs: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        step_id = str(step.get("id") or "loop")
        max_iterations = max(1, min(int(step.get("max_iterations") or 1), 100))
        loop_steps = list(step.get("steps") or [])
        condition = step.get("condition")
        results: list[dict[str, Any]] = []

        for iteration in range(max_iterations):
            if condition and not _evaluate_condition(condition, context):
                events.append(_run_event(step_id, "ok", f"Loop stopped at iteration {iteration}."))
                break
            iteration_outputs: dict[str, Any] = {}
            iteration_context = deepcopy(context)
            iteration_context["iteration"] = iteration
            result = await self._execute_steps(
                workflow=workflow,
                steps_def=loop_steps,
                run=run,
                context=iteration_context,
                outputs=iteration_outputs,
                events=[],
                start_index=0,
            )
            if result.get("blocked"):
                return result
            results.append(result["outputs"])
            parent_outputs[step_id] = results
            _sync_context_outputs(context, parent_outputs)
        return {"blocked": False, "outputs": results}

    async def _apply_outcome_to_task(
        self,
        task: dict[str, Any],
        outcome: WorkflowExecutionOutcome,
    ) -> dict[str, Any] | None:
        updates: dict[str, Any] = {
            "agent": outcome.selected_agent,
            "status": outcome.status,
            "steps": outcome.steps,
            "last_response_summary": outcome.last_response_summary,
            "waiting_reason": outcome.waiting_reason,
            "error": outcome.error,
        }
        if outcome.approval_required is not None:
            updates["approval_required"] = outcome.approval_required
        if outcome.approval_state is not None:
            updates["approval_state"] = outcome.approval_state
        if outcome.params is not None:
            updates["params"] = outcome.params
        if outcome.waiting_reason and outcome.waiting_reason.startswith("phase:"):
            phase_name = outcome.waiting_reason.split("phase:", 1)[1].strip()
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
        if outcome.status in {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.ESCALATED.value}:
            updates["completed_at"] = _utc_now()
        updated = await self._repo.patch_task(str(task["task_id"]), updates)
        if updated is not None:
            await self._hub.broadcast("task_updated", updated)
        return updated

    def _run_outcome(
        self,
        run: dict[str, Any] | None,
        workflow: dict[str, Any],
        *,
        task_status: str,
        waiting_reason: str | None = None,
        error: str | None = None,
    ) -> WorkflowExecutionOutcome:
        run = run or {}
        params = dict((run.get("context") or {}).get("params") or {})
        params["_workflow"] = {
            "run_id": run.get("run_id"),
            "workflow": workflow.get("workflow"),
            "status": run.get("status"),
        }
        run_steps = [
            _task_step(str(item.get("id") or item.get("step") or "workflow_step"), str(item.get("status") or ""), item.get("detail"))
            for item in (run.get("steps") or [])
        ]
        summary = self._summarize_run(run, workflow)
        return WorkflowExecutionOutcome(
            selected_agent=str(workflow.get("agent") or _agent_from_workflow_id(str(workflow.get("workflow") or ""))),
            status=task_status,
            steps=run_steps,
            last_response_summary=summary,
            waiting_reason=waiting_reason,
            error=error,
            params=params,
        )

    @staticmethod
    def _running_outcome(task: dict[str, Any], workflow: dict[str, Any], run: dict[str, Any]) -> WorkflowExecutionOutcome:
        params = _task_params(task)
        params["_workflow"] = {
            "run_id": run.get("run_id"),
            "workflow": workflow.get("workflow"),
            "status": run.get("status"),
        }
        return WorkflowExecutionOutcome(
            selected_agent=str(workflow.get("agent") or _agent_from_workflow_id(str(workflow.get("workflow") or ""))),
            status=TaskStatus.IN_PROGRESS.value,
            steps=[
                _task_step(str(item.get("id") or "workflow_step"), str(item.get("status") or ""), item.get("detail"))
                for item in (run.get("steps") or [])
            ],
            last_response_summary="Workflow en ejecucion.",
            params=params,
        )

    def _summarize_run(self, run: dict[str, Any], workflow: dict[str, Any]) -> str:
        status = str(run.get("status") or "unknown")
        workflow_name = str(workflow.get("name") or workflow.get("workflow") or "workflow")
        if status == WorkflowRunStatus.COMPLETED.value:
            return f"Workflow completado: {workflow_name}"
        if run.get("error"):
            return f"Workflow {workflow_name} fallo: {run['error']}"
        return f"Workflow {workflow_name}: {status}"

    async def _build_discovery_proposal(self, task: dict[str, Any]) -> str:
        if self._llm is None:
            return self._fallback_workflow_yaml(task)

        instruction = (
            "You are the OpenCEO Enterprise Workflow Architect. "
            "Generate one crystallized YAML workflow for this task using the available tools.\n\n"
            "Rules:\n"
            "1. Top-level keys: workflow, name, agent, description, auto_match, triggers, steps.\n"
            "2. workflow must be in format {agent}.{action_name} (e.g. dev.implement, secretary.monthly_digest, etc.).\n"
            "3. Each step must use either:\n"
            "   - tool: <tool_name> with params matching the tool's signature.\n"
            "   - ref: <workflow_id> to call another crystallized workflow.\n"
            "   - type: loop with max_iterations, condition (type: python_eval, expr: ...), and steps.\n"
            "   - type: human_in_the_loop with permission: qa_review | client_review | admin_approval and prompt: 'message' for sensitive operations or approvals.\n"
            "4. Variables: use {{task.params.field}}, {{task.title}}, or {{steps.<step_id>.<field>}}.\n"
            "5. auto_match: true only if triggers contains 2-5 explicit natural language trigger phrases.\n"
            "6. Use ONLY tools from the catalog. Do not invent non-existent tools.\n"
            "7. Return clean YAML only, without markdown formatting or commentary."
        )
        payload = {
            "task": _task_context(task),
            "available_tools": self._tools.describe(),
        }
        try:
            response = await self._llm.ainvoke(
                [
                    SystemMessage(content=instruction),
                    HumanMessage(content=yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)),
                ]
            )
            text = _strip_fenced_yaml(str(getattr(response, "content", "") or ""))
            self._parse_workflow_yaml(text, source="<llm_discovery>")
            return text
        except Exception:
            logger.warning("Workflow discovery proposal failed; falling back to skeleton", exc_info=True)
            return self._fallback_workflow_yaml(task)

    def _fallback_workflow_yaml(self, task: dict[str, Any]) -> str:
        params = _task_params(task)
        raw_name = _safe_name(str(params.get("workflow") or task.get("title") or "discovered_workflow"))
        workflow_id = raw_name if "." in raw_name else f"ceo.{raw_name}"
        doc = {
            "workflow": workflow_id,
            "name": str(task.get("title") or workflow_id),
            "agent": _agent_from_workflow_id(workflow_id),
            "description": str(task.get("description") or ""),
            "auto_match": False,
            "triggers": [],
            "steps": [
                {
                    "id": "complete",
                    "tool": "core.complete",
                    "params": {
                        "task_id": "{{task.task_id}}",
                        "message": "Replace this step with real tools before using this workflow.",
                    },
                },
            ],
        }
        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=False)

    def _load_workflows(self) -> dict[str, dict[str, Any]]:
        base = self.workflows_dir
        if not base.exists():
            return {}
        workflows: dict[str, dict[str, Any]] = {}
        for path in sorted([*base.rglob("*.yaml"), *base.rglob("*.yml")]):
            try:
                workflow = self._parse_workflow_yaml(path.read_text(encoding="utf-8"), source=str(path))
            except Exception as exc:
                logger.warning("Skipping invalid workflow %s: %s", path, exc)
                continue
            workflow["path"] = str(path)
            workflows[str(workflow["workflow"])] = workflow
        return workflows

    def parse_workflow_text(self, raw: str, *, source: str) -> dict[str, Any]:
        """Parse one workflow document (exact skills, TASK-2026-00205)."""
        return self._parse_workflow_yaml(raw, source=source)

    def _parse_workflow_yaml(self, raw: str, *, source: str) -> dict[str, Any]:
        loaded = yaml.safe_load(raw)
        if not isinstance(loaded, dict):
            raise ValueError(f"Workflow {source} must be a YAML object")
        workflow_id = str(loaded.get("workflow") or "").strip()
        if not workflow_id:
            raise ValueError(f"Workflow {source} is missing 'workflow'")
        steps = loaded.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"Workflow {source} must define a non-empty steps list")
        self._validate_yaml_steps(steps, source=source)
        loaded["workflow"] = workflow_id
        loaded.setdefault("agent", _agent_from_workflow_id(workflow_id))
        loaded.setdefault("name", workflow_id)
        loaded.setdefault("auto_match", False)
        loaded.setdefault("triggers", [])
        loaded.setdefault("keywords", [])
        return loaded

    def _validate_yaml_steps(self, steps: list[Any], *, source: str) -> None:
        for index, raw_step in enumerate(steps):
            if not isinstance(raw_step, dict):
                raise ValueError(f"Workflow {source} step {index + 1} must be an object")
            step_type = str(raw_step.get("type") or "tool").strip().lower()
            if step_type == "human_in_the_loop":
                if not raw_step.get("permission"):
                    raw_step["permission"] = "qa_review"
                continue
            if step_type == "loop":
                nested = raw_step.get("steps") or []
                if not isinstance(nested, list) or not nested:
                    raise ValueError(f"Workflow {source} loop step {index + 1} must define steps")
                self._validate_yaml_steps(nested, source=source)

    def _save_workflow_yaml(self, raw_yaml: str, workflow: dict[str, Any]) -> Path:
        workflow_id = str(workflow["workflow"])
        agent = _safe_name(str(workflow.get("agent") or _agent_from_workflow_id(workflow_id)))
        name = _safe_name(workflow_id.split(".", 1)[-1])
        base = self.workflows_dir / agent
        base.mkdir(parents=True, exist_ok=True)
        path = (base / f"{name}.yaml").resolve()
        if self.workflows_dir not in path.parents:
            raise ValueError("Invalid workflow path")
        path.write_text(raw_yaml.strip() + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _find_workflow(workflows: dict[str, dict[str, Any]], lookup: str) -> dict[str, Any] | None:
        normalized = lookup.strip().lower()
        for workflow_id, workflow in workflows.items():
            candidates = [
                workflow_id,
                str(workflow.get("name") or ""),
                *(str(item) for item in workflow.get("aliases") or []),
            ]
            if any(candidate.strip().lower() == normalized for candidate in candidates):
                return workflow
        return None

    @staticmethod
    def _score_workflow(workflow: dict[str, Any], text: str) -> tuple[int, str]:
        if not _truthy(workflow.get("auto_match")):
            return 0, ""
        workflow_id = str(workflow.get("workflow") or "").lower()
        name = str(workflow.get("name") or "").lower()
        if workflow_id and workflow_id in text:
            return 100, f"matched workflow id {workflow_id}"
        for trigger in workflow.get("triggers") or []:
            token = str(trigger or "").strip().lower()
            if token and token in text:
                return 80, f"matched trigger {token}"
        for keyword in workflow.get("keywords") or []:
            token = str(keyword or "").strip().lower()
            if token and token in text:
                return 50, f"matched keyword {token}"
        if name and name in text:
            return 30, f"matched name {name}"
        return 0, ""

    @staticmethod
    def _should_start_discovery(task: dict[str, Any]) -> bool:
        params = _task_params(task)
        if _truthy(params.get("discover_workflow")) or _truthy(params.get("workflow_discovery")):
            return True
        if _truthy(params.get("use_workflow")) and not _truthy(params.get("no_discovery")):
            return True
        if _first_text(params.get("workflow"), params.get("workflow_id"), params.get("workflow_ref")):
            return not _truthy(params.get("no_discovery"))
        return False


def _task_params(task: dict[str, Any]) -> dict[str, Any]:
    params = task.get("params")
    return dict(params) if isinstance(params, dict) else {}


def _rate_or_none(value: Any) -> Decimal | None:
    """Tasa de cambio explicita como `Decimal`, o None si no hay/esta mal.

    Nunca se inventa una tasa: un valor invalido se trata como ausente y el
    flujo falla cerrado en `money_text.MissingExchangeRateError`.
    """
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _task_context(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task.get("task_id"),
        "project": task.get("project"),
        "title": task.get("title"),
        "description": task.get("description"),
        "source": task.get("source"),
        "source_id": task.get("source_id"),
        "attachments": task.get("attachments") or [],
        "priority": task.get("priority"),
        "params": _task_params(task),
    }


def _step_id(step: dict[str, Any], index: int) -> str:
    return str(step.get("id") or step.get("tool") or step.get("ref") or f"step{index + 1}")


def _sync_context_outputs(context: dict[str, Any], outputs: dict[str, Any]) -> None:
    context["outputs"] = outputs
    context["steps"] = outputs


def _resolve_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_value(item, context) for item in value]
    if not isinstance(value, str):
        return value

    match = _TEMPLATE_RE.fullmatch(value.strip())
    if match:
        return _lookup_context(match.group(1), context)

    def replace(match_obj: re.Match[str]) -> str:
        resolved = _lookup_context(match_obj.group(1), context)
        if resolved is None:
            return ""
        return str(resolved)

    return _TEMPLATE_RE.sub(replace, value)


def _lookup_context(expr: str, context: dict[str, Any]) -> Any:
    path = [part.strip() for part in expr.strip().split(".") if part.strip()]
    if not path:
        return None

    if path[0] in context:
        current: Any = context[path[0]]
        path = path[1:]
    elif path[0] in (context.get("outputs") or {}):
        current = context["outputs"][path[0]]
        path = path[1:]
    else:
        return None

    for part in path:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            current = getattr(current, part, None)
        if current is None:
            return None
    return current


def _evaluate_when(value: Any, context: dict[str, Any]) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        exact = _TEMPLATE_RE.fullmatch(value.strip())
        if exact:
            return _truthy(_lookup_context(exact.group(1), context))
        expr = _TEMPLATE_RE.sub(lambda m: repr(_lookup_context(m.group(1), context)), value)
        try:
            return bool(eval(expr, {"__builtins__": {}}, {}))
        except Exception:
            return _truthy(_resolve_value(value, context))
    return _truthy(value)


def _evaluate_condition(condition: Any, context: dict[str, Any]) -> bool:
    if not isinstance(condition, dict):
        return _evaluate_when(condition, context)
    condition_type = str(condition.get("type") or "python_eval").strip().lower()
    if condition_type == "python_eval":
        return _evaluate_when(condition.get("expr") or condition.get("expression") or condition.get("prompt"), context)
    if condition_type == "llm_eval":
        raise ValueError("llm_eval conditions require a dedicated evaluator and are not enabled in deterministic mode")
    return _evaluate_when(condition.get("value"), context)


def _run_event(step_id: str, status: str, detail: str | None, output: Any = None) -> dict[str, Any]:
    event = {
        "id": step_id,
        "status": status,
        "detail": detail,
        "timestamp": _utc_now(),
    }
    if output is not None:
        event["output"] = output
    return event


def _task_step(step: str, status: str, detail: str | None = None) -> dict[str, Any]:
    return {
        "step": step,
        "status": status,
        "detail": detail,
        "timestamp": _utc_now(),
    }


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
    return bool(value)


def _agent_from_workflow_id(workflow_id: str) -> str:
    if "." in workflow_id:
        return workflow_id.split(".", 1)[0].strip() or "ceo"
    return "ceo"


def _safe_name(value: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("_", value.strip().lower()).strip("._-")
    return cleaned or "workflow"


def _strip_fenced_yaml(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    if lines and lines[0].strip().lower() in {"yaml", "yml"}:
        lines = lines[1:]
    return "\n".join(lines).strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
