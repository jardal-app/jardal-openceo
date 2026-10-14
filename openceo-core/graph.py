from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from collections.abc import Collection, Mapping

from langgraph.graph import END, START, StateGraph

from agents.dev.session import DevSessionManager
from catalog import AgentType, Catalog, load_catalog
from config_loader import ConfigLoader
from objectives.ticket_cost import COST_RUNS_KEY
from orchestration.generic_agent import (
    GenericAgentWiring,
    run_generic_agent,
)
from router import CEORouter, resolve_selected_agent
from state import AgentName, GraphState, TaskStatus

#: Agents with a wired executor node below (pm left in 00209,
#: secretary in 00220, finance in 00221; catalog agents run generic).
WIRED_AGENTS = ("dev",)


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
    routes to its legacy node by name, never by text; an enabled AI
    name with a muse-code profile routes to the generic node
    (TASK-2026-00205); unknown, disabled, human, untranslated-engine
    or ``ceo`` names wait with ``routing_missing`` and their reason.
    No hint and no target means an undecided ticket: the node waits
    fail-closed (TASK-2026-00204) — in production the run pre-stage
    plans every pedido, so the node never sees one.
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
        if entry.type is not AgentType.AI:
            return AgentGateDecision(
                False, None,
                f"routing_missing: agent {name!r} is human (humans "
                "answer from the cockpit, never execute tickets)")
        if entry.engine != "muse-code":
            return AgentGateDecision(
                False, None,
                f"routing_missing: agent {name!r} engine "
                f"{entry.engine!r} has no runner yet (translator "
                "TASK-2026-00200)")
        if not (entry.profile or "").strip():
            return AgentGateDecision(
                False, None,
                f"routing_missing: agent {name!r} is enabled but has no "
                "runner yet")
        return AgentGateDecision(True, name, "")
    return AgentGateDecision(True, None, "")


class OpenCEOGraph:
    """LangGraph pipeline for CEO routing and worker execution."""

    def __init__(
        self,
        router: CEORouter,
        config_loader: ConfigLoader,
        dev_session: DevSessionManager,
        notion: Any | None = None,
        generic_wiring: GenericAgentWiring | None = None,
        catalog: Catalog | None = None,
    ) -> None:
        self._router = router
        self._config_loader = config_loader
        self._dev_session = dev_session
        self._notion = notion
        self._generic_wiring = generic_wiring
        self._catalog = catalog

        builder = StateGraph(GraphState)
        builder.add_node("ceo_router", self._ceo_router_node)
        builder.add_node("dev_agent", self._dev_node)
        builder.add_node("generic_agent", self._generic_node)
        builder.add_node("unsupported_agent", self._unsupported_agent_node)

        builder.add_edge(START, "ceo_router")
        builder.add_conditional_edges(
            "ceo_router",
            self._after_ceo_route,
            {
                "dev": "dev_agent",
                "generic_agent": "generic_agent",
                "unsupported": "unsupported_agent",
            },
        )
        builder.add_edge("dev_agent", END)
        builder.add_edge("generic_agent", END)
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
            "params": task.get("params")
            if isinstance(task.get("params"), dict) else None,
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
        return await self._graph.ainvoke(initial_state)

    async def _ceo_router_node(self, state: GraphState) -> GraphState:
        project_cfg = self._config_loader.load_project(state.get("project"))
        # Explicit only (TASK-2026-00204): a validated-plan phase agent,
        # else the gate-validated hint resolved through the catalog
        # (aliases first, then any catalog name to itself,
        # TASK-2026-00244). Nothing here reads ticket text; undecided
        # work never reaches this node (the run pre-stage plans every
        # pedido) and waits fail-closed if it ever does.
        catalog = self._catalog if self._catalog is not None \
            else load_catalog()
        phase_agent = self._phase_agent_override(state)
        if phase_agent is not None:
            selected_agent: str | None = phase_agent.value
        else:
            selected_agent = resolve_selected_agent(
                state.get("agent_hint"), state.get("coding_tool"), catalog)

        step = {
            "step": "ceo_route",
            "status": "ok",
            "detail": self._route_detail(selected_agent, state),
            "timestamp": datetime.now(timezone.utc),
        }

        return {
            "selected_agent": selected_agent
            if selected_agent is not None else "",
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
    def _route_detail(selected_agent: str | None,
                      state: GraphState) -> str:
        name = selected_agent if selected_agent is not None \
            else "undecided"
        phase_key = str(state.get("phase") or "").strip()
        phases = state.get("workflow_phases") or []
        if phase_key and isinstance(phases, list):
            for phase in phases:
                if not isinstance(phase, dict):
                    continue
                if str(phase.get("key") or "").strip() != phase_key:
                    continue
                if str(phase.get("type") or "").strip().lower() == "agent":
                    return f"Task routed to {name} by phase {phase_key}"
                break
        tool = str(state.get("coding_tool") or "").strip()
        if tool and selected_agent == AgentName.DEV:
            return f"Task routed to dev by coding_tool={tool}"
        if selected_agent is None:
            return ("No explicit agent: routing_missing — the single "
                    "planner decides pedidos before dispatch")
        return f"Task routed to {name} (explicit)"

    def _after_ceo_route(self, state: GraphState) -> str:
        """Route by catalog (TASK-2026-00244): wired names to their node,
        enabled AI catalog names to the generic node, anything else to
        unsupported. No manual agent list: a new card routes without
        touching this method, and the generic node re-validates."""
        selected = state.get("selected_agent")
        if selected in WIRED_AGENTS:
            return selected
        if not selected or selected == AgentName.CEO.value:
            return "unsupported"
        catalog = self._catalog if self._catalog is not None \
            else load_catalog()
        entry = catalog.agents.get(selected)
        if entry is not None and entry.enabled \
                and entry.type is AgentType.AI:
            return "generic_agent"
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

        out: dict[str, Any] = {
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
        if getattr(result, COST_RUNS_KEY, None):
            out[COST_RUNS_KEY] = getattr(result, COST_RUNS_KEY)
        return out  # type: ignore[return-value]

    async def _generic_node(self, state: GraphState) -> GraphState:
        agent = str(state.get("selected_agent") or "")
        wiring = self._generic_wiring
        if wiring is None:
            reason = "config: generic agent runner is not wired"
            step = {
                "step": "generic_agent",
                "status": "waiting",
                "detail": reason,
                "timestamp": datetime.now(timezone.utc),
            }
            return {
                "selected_agent": agent,
                "status": TaskStatus.WAITING.value,
                "steps": [*(state.get("steps") or []), step],
                "waiting_reason": reason,
                "resume_at": None,
                "error": None,
            }
        task = {
            "task_id": state["task_id"],
            "ticket_number": state.get("ticket_number"),
            "project": state.get("project"),
            "title": state["title"],
            "description": state["description"],
            "source": state.get("source"),
            "attachments": state.get("attachments", []),
            "params": state.get("params") or {},
            "priority": state.get("priority", "medium"),
            "objective_id": state.get("objective_id"),
        }
        out = await run_generic_agent(agent, task, wiring)
        merged = {
            "selected_agent": out.get("selected_agent", agent),
            "status": out.get("status", TaskStatus.FAILED.value),
            "steps": [*(state.get("steps") or []), *out.get("steps", [])],
            "params": out.get("params", task["params"]),
            "last_response_summary": out.get("last_response_summary"),
            "error": out.get("error"),
        }
        if out.get(COST_RUNS_KEY):
            merged[COST_RUNS_KEY] = out[COST_RUNS_KEY]
        if out.get("waiting_reason") is not None:
            merged["waiting_reason"] = out["waiting_reason"]
            merged["resume_at"] = out.get("resume_at")
        if out.get("result_comment"):
            merged["result_comment"] = out["result_comment"]
        return merged  # type: ignore[return-value]

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
