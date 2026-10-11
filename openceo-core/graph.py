from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from collections.abc import Collection, Mapping

from langgraph.graph import END, START, StateGraph

from agents.dev.session import DevSessionManager
from agents.finance.session import FinanceSessionManager
from agents.pm.session import PMSessionManager
from agents.secretary.session import SecretarySessionManager
from catalog import Catalog, load_catalog
from config_loader import ConfigLoader
from objectives.agent_result import legacy_blocked_result
from router import CEORouter
from state import AgentName, GraphState, TaskStatus

#: Agents with a wired executor node below. Disabled-but-wired names stay
#: alive on their legacy session until their removal ticket lands.
WIRED_AGENTS = ("dev", "pm", "finance", "secretary")


@dataclass(frozen=True)
class AgentGateDecision:
    """Result of :func:`resolve_agent_gate`.

    ``proceed`` with ``hint`` routes by that name (never by text);
    otherwise the ticket waits with ``reason`` (``routing_missing``).
    """

    proceed: bool
    hint: str | None
    reason: str


def _normalize_agent_name(value: Any) -> str:
    return str(value or "").strip().lower()


def resolve_agent_gate(
    agent_hint: Any,
    params: Mapping[str, Any] | None,
    coding_tool: Any,
    catalog: Catalog,
    wired: Collection[str] = WIRED_AGENTS,
) -> AgentGateDecision:
    """Gate agent dispatch against the single catalog (TASK-2026-00195).

    A ticket-level ``coding_tool`` still forces dev (persisted routing
    decision, F0 pilot 00097). Otherwise the explicit hint wins and the
    planner target (``params.target_agent``) backs it up: a wired name
    routes by name, never by text; unknown, disabled, human or ``ceo``
    names wait with ``routing_missing`` and their reason. No hint and no
    target means a loose ticket, classified as today.
    """
    if str(coding_tool or "").strip():
        return AgentGateDecision(True, AgentName.DEV.value, "")
    hint = _normalize_agent_name(agent_hint)
    target = _normalize_agent_name((params or {}).get("target_agent"))
    for name in (hint, target):
        if not name:
            continue
        if name in wired:
            return AgentGateDecision(True, name, "")
        if name == AgentName.CEO.value:
            return AgentGateDecision(
                False, None,
                "routing_missing: agent 'ceo' never executes tickets "
                "(it decides work)")
        entry = catalog.agents.get(name)
        if entry is None:
            return AgentGateDecision(
                False, None,
                f"routing_missing: agent {name!r} is unknown "
                "(not in the catalog)")
        if not entry.enabled:
            ticket = f"; see {entry.decided_by_ticket}" \
                if entry.decided_by_ticket else ""
            return AgentGateDecision(
                False, None,
                f"routing_missing: agent {name!r} is disabled "
                f"({entry.disabled_reason}{ticket})")
        return AgentGateDecision(
            False, None,
            f"routing_missing: agent {name!r} is enabled but has no runner "
            "yet")
    return AgentGateDecision(True, None, "")


class OpenCEOGraph:
    """LangGraph pipeline for CEO routing and worker execution."""

    def __init__(
        self,
        router: CEORouter,
        config_loader: ConfigLoader,
        dev_session: DevSessionManager,
        finance_session: FinanceSessionManager,
        secretary_session: SecretarySessionManager,
        pm_session: PMSessionManager | None = None,
        notion: Any | None = None,
    ) -> None:
        self._router = router
        self._config_loader = config_loader
        self._dev_session = dev_session
        self._finance_session = finance_session
        self._secretary_session = secretary_session
        self._pm_session = pm_session
        self._notion = notion

        builder = StateGraph(GraphState)
        builder.add_node("ceo_router", self._ceo_router_node)
        builder.add_node("dev_agent", self._dev_node)
        builder.add_node("pm_agent", self._pm_node)
        builder.add_node("finance_agent", self._finance_node)
        builder.add_node("secretary_agent", self._secretary_node)
        builder.add_node("unsupported_agent", self._unsupported_agent_node)

        builder.add_edge(START, "ceo_router")
        builder.add_conditional_edges(
            "ceo_router",
            self._after_ceo_route,
            {
                "dev": "dev_agent",
                "pm": "pm_agent",
                "finance": "finance_agent",
                "secretary": "secretary_agent",
                "unsupported": "unsupported_agent",
            },
        )
        builder.add_edge("dev_agent", END)
        builder.add_edge("pm_agent", END)
        builder.add_edge("finance_agent", END)
        builder.add_edge("secretary_agent", END)
        builder.add_edge("unsupported_agent", END)

        self._graph = builder.compile()

    async def execute(self, task: dict[str, Any], agent_hint: str | None = None) -> dict[str, Any]:
        gate = resolve_agent_gate(
            agent_hint, task.get("params"), task.get("coding_tool"),
            load_catalog())
        if not gate.proceed:
            return {
                "status": TaskStatus.WAITING.value,
                "steps": [{
                    "step": "agent_gate",
                    "status": "waiting",
                    "detail": gate.reason,
                    "timestamp": datetime.now(timezone.utc),
                }],
                "waiting_reason": gate.reason,
                "resume_at": None,
            }
        agent_hint = gate.hint
        initial_state: GraphState = {
            "task_id": task["task_id"],
            "ticket_number": task.get("ticket_number"),
            "project": task.get("project"),
            "title": task["title"],
            "description": task["description"],
            "source": task["source"],
            "source_id": task.get("source_id"),
            "attachments": task.get("attachments", []),
            "params": task.get("params"),
            "priority": task.get("priority", "medium"),
            "agent_hint": agent_hint,
            "phase": task.get("phase"),
            "phase_index": int(task.get("phase_index") or 0),
            "workflow_phases": task.get("workflow_phases") or [],
            "steps": [],
            "status": task.get("status", TaskStatus.QUEUED.value),
            "coding_tool": task.get("coding_tool"),
            "objective_id": task.get("objective_id"),
        }
        out = await self._graph.ainvoke(initial_state)
        return self._attach_legacy_agent_result(task, out)

    @staticmethod
    def _attach_legacy_agent_result(
        task: dict[str, Any], out: dict[str, Any],
    ) -> dict[str, Any]:
        """Map a legacy non-dev FAILED to its agent verdict (00202).

        Deterministic sessions (pm/finance/secretary) return a bare
        status: their FAILED means the agent could not do it, recorded
        as ``bloqueado`` with the session detail. Dev runs parse their
        own verdict from the CLI output; completed and waiting runs
        carry none. Total: never raises, never drops params.
        """
        if not isinstance(out, dict):
            return out
        if str(out.get("status") or "") != TaskStatus.FAILED.value:
            return out
        if str(out.get("selected_agent") or "") \
                not in {"pm", "finance", "secretary"}:
            return out
        raw_params = out.get("params")
        params = dict(raw_params) if isinstance(raw_params, dict) else {}
        if not params and isinstance(task, dict):
            task_params = task.get("params")
            if isinstance(task_params, dict):
                params = dict(task_params)
        if isinstance(params.get("agent_result"), dict):
            return out
        motivo = str(out.get("error")
                     or out.get("last_response_summary") or "").strip()
        params["agent_result"] = legacy_blocked_result(motivo)
        out["params"] = params
        return out

    async def _ceo_router_node(self, state: GraphState) -> GraphState:
        project_cfg = self._config_loader.load_project(state.get("project"))
        selected_agent = self._phase_agent_override(state)
        if selected_agent is None:
            selected_agent = await self._router.select_agent(
                title=state["title"],
                description=state["description"],
                agent_hint=state.get("agent_hint"),
                project_config=project_cfg,
                coding_tool=state.get("coding_tool"),
            )

        step = {
            "step": "ceo_route",
            "status": "ok",
            "detail": self._route_detail(selected_agent, state),
            "timestamp": datetime.now(timezone.utc),
        }

        return {
            "selected_agent": selected_agent.value,
            "project_config": project_cfg,
            "steps": [*(state.get("steps") or []), step],
            "status": TaskStatus.IN_PROGRESS.value,
        }

    def _phase_agent_override(self, state: GraphState) -> AgentName | None:
        phases = state.get("workflow_phases") or []
        if not isinstance(phases, list) or not phases:
            return None

        phase_key = str(state.get("phase") or "").strip()
        phase_index = int(state.get("phase_index") or 0)
        current: dict[str, Any] | None = None

        if phase_key:
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                if str(phase.get("key") or "").strip() == phase_key:
                    current = phase
                    break
        elif 0 <= phase_index < len(phases):
            candidate = phases[phase_index]
            current = candidate if isinstance(candidate, dict) else None

        if not current:
            return None
        if str(current.get("type") or "").strip().lower() != "agent":
            return None

        raw_agent = str(current.get("agent") or "").strip().lower()
        return AgentName(raw_agent) if raw_agent in AgentName._value2member_map_ else None

    @staticmethod
    def _route_detail(selected_agent: AgentName, state: GraphState) -> str:
        phase_key = str(state.get("phase") or "").strip()
        phases = state.get("workflow_phases") or []
        if phase_key and isinstance(phases, list):
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                if str(phase.get("key") or "").strip() != phase_key:
                    continue
                if str(phase.get("type") or "").strip().lower() == "agent":
                    return f"Task routed to {selected_agent.value} by phase {phase_key}"
                break
        tool = str(state.get("coding_tool") or "").strip()
        if tool and selected_agent == AgentName.DEV:
            return f"Task routed to dev by coding_tool={tool}"
        return f"Task routed to {selected_agent.value}"

    def _after_ceo_route(self, state: GraphState) -> str:
        if state.get("selected_agent") == AgentName.DEV.value:
            return "dev"
        if state.get("selected_agent") == AgentName.PM.value:
            return "pm"
        if state.get("selected_agent") == AgentName.FINANCE.value:
            return "finance"
        if state.get("selected_agent") == AgentName.SECRETARY.value:
            return "secretary"
        return "unsupported"

    async def _dev_node(self, state: GraphState) -> GraphState:
        task = {
            "task_id": state["task_id"],
            "ticket_number": state.get("ticket_number"),
            "project": state.get("project"),
            "title": state["title"],
            "description": state["description"],
            "attachments": state.get("attachments", []),
            "params": state.get("params") or {},
            "priority": state.get("priority", "medium"),
            "coding_tool": state.get("coding_tool"),
            "objective_id": state.get("objective_id"),
        }

        result = await self._dev_session.run(task, state.get("project_config", {}))

        return {
            "selected_agent": AgentName.DEV.value,
            "status": result.status.value,
            "steps": [*(state.get("steps") or []), *result.steps],
            "coding_tool": result.coding_tool.value if result.coding_tool else None,
            "coding_session_id": result.coding_session_id,
            "coding_session_title": result.coding_session_title,
            "last_prompt_sent": result.last_prompt_sent,
            "last_response_summary": result.last_response_summary,
            "waiting_reason": result.waiting_reason,
            "resume_at": result.resume_at,
            "error": result.error,
            "modified_files": result.modified_files,
            "params": result.params,
        }

    async def _pm_node(self, state: GraphState) -> GraphState:
        if self._pm_session is None:
            step = {
                "step": "pm_dispatch",
                "status": "failed",
                "detail": "PM agent not configured",
                "timestamp": datetime.now(timezone.utc),
            }
            return {
                "selected_agent": AgentName.PM.value,
                "status": TaskStatus.FAILED.value,
                "steps": [*(state.get("steps") or []), step],
                "error": "PM agent not configured",
            }

        task_params = state.get("params") or {}
        autorun = bool(task_params.get("autorun", False))

        result = await self._pm_session.extract_tickets(
            interview_text=state["description"],
            project=state.get("project"),
            autorun=autorun,
        )

        step = {
            "step": "pm_extraction",
            "status": "ok" if result.status == TaskStatus.COMPLETED else "failed",
            "detail": result.summary,
            "timestamp": datetime.now(timezone.utc),
        }

        return {
            "selected_agent": AgentName.PM.value,
            "status": result.status.value,
            "steps": [*(state.get("steps") or []), step],
            "last_response_summary": result.summary,
            "error": result.error,
        }

    async def _finance_node(self, state: GraphState) -> GraphState:
        task = {
            "task_id": state["task_id"],
            "project": state.get("project"),
            "title": state["title"],
            "description": state["description"],
            "attachments": state.get("attachments", []),
            "priority": state.get("priority", "medium"),
        }

        result = await self._finance_session.run(task, state.get("project_config", {}))

        return {
            "selected_agent": AgentName.FINANCE.value,
            "status": result.status.value,
            "steps": [*(state.get("steps") or []), *result.steps],
            "last_response_summary": result.last_response_summary,
            "error": result.error,
        }

    async def _secretary_node(self, state: GraphState) -> GraphState:
        task = {
            "task_id": state["task_id"],
            "project": state.get("project"),
            "title": state["title"],
            "description": state["description"],
            "attachments": state.get("attachments", []),
            "priority": state.get("priority", "medium"),
        }

        result = await self._secretary_session.run(task)

        return {
            "selected_agent": AgentName.SECRETARY.value,
            "status": result.status.value,
            "steps": [*(state.get("steps") or []), *result.steps],
            "last_response_summary": result.last_response_summary,
            "error": result.error,
        }

    async def _unsupported_agent_node(self, state: GraphState) -> GraphState:
        selected = str(state.get("selected_agent", "unknown"))
        entry = load_catalog().agents.get(selected)
        if entry is not None and not entry.enabled:
            ticket = f"; see {entry.decided_by_ticket}" \
                if entry.decided_by_ticket else ""
            reason = (f"routing_missing: agent '{selected}' is disabled "
                      f"({entry.disabled_reason}{ticket})")
        else:
            reason = (f"routing_missing: agent '{selected}' has no runner "
                      "(not an executor)")
        step = {
            "step": "worker_dispatch",
            "status": "waiting",
            "detail": reason,
            "timestamp": datetime.now(timezone.utc),
        }
        return {
            "selected_agent": selected,
            "status": TaskStatus.WAITING.value,
            "steps": [*(state.get("steps") or []), step],
            "waiting_reason": reason,
            "resume_at": None,
            "error": None,
        }
