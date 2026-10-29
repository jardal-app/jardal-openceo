"""Autonomous CEO review flow (autonomy epic TASK-2026-00110).

F4 TASK-2026-00119. One review compiles bounded company signals, runs
the semantic decision once (Muse headless, the same mechanism as the
objective planner, own contract in :mod:`ceo.models`), validates it
with the deterministic anti-bloat rules, performs exactly one action,
and publishes the review record Horizon reads.

Business-state effects flow only through sanctioned channels
(Jardal ERP-first creation, ``create_owner_escalation``, the engine
observer). If a future CEO ever needs to close or cancel business
work directly, it MUST go through ``TaskService.transition_ticket``
(TASK-2026-00132) — never through a direct adapter write. No such
call site exists in this ticket: the five-action contract states a
need (a pedido the single planner shapes), escalates, reports, or
closes a mission with evidence, and business acceptance stays an
Jardal ERP decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection, Mapping, Sequence

from company_mandate import (
    MANDATE_PRINCIPLES,
    SECRET_BLOCK_PARAM,
    MandatoEmpresa,
    OwnerAction,
    requires_owner,
)
from objectives import engine as engine_mod
from objectives import ticket_cost as cost_mod
from ceo import models as models_mod
from ceo.models import (
    CEOAction,
    CEO_DECISION_FILE,
    CEODecision,
    CEOKpiReading,
    CEOValidationContext,
    DUPLICATE_ESCALATION_PREFIX,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CEOEnv",
    "CEOReviewKind",
    "CEOSignals",
    "CEO_EXECUTOR_ROLE",
    "CEO_RELIABILITY_MAX",
    "collapse_reliability_wake_reasons",
    "compile_reliability",
    "compile_signals",
    "new_review_id",
    "reliability_wake_reason",
    "run_review",
]

#: One-shot executor role (global ``ceo`` block, same account/model as
#: the planner, separate telemetry).
CEO_EXECUTOR_ROLE = "ceo_reviewer"

#: Review kinds. Scheduled follows the mandate cadence; reactive
#: follows company events drained by the loop.
CEOReviewKind = ("scheduled", "reactive")

# Signal bounds. Every source is capped and degrades to unknown on its
# own: one unreadable source never empties the others.
CEO_BUSINESS_ROWS = 50
CEO_OBJECTIVES = 10
CEO_MISSIONS = 10
CEO_OBJECTIVES_PER_MISSION = 20
CEO_CHILDREN_PER_OBJECTIVE = 100
CEO_ROADMAP_PER_OBJECTIVE = 10
CEO_DEFERRED_PER_OBJECTIVE = 10
CEO_CLOSED_ROWS = 10
CEO_BLOCKED_ROWS = 20
CEO_RESEARCH_ROWS = 5
CEO_OPEN_TITLES = 200
CEO_PROMPT_REASONS = 10
CEO_STATUS_ROWS = 10

# Reliability section (TASK-2026-00380, design v2 §12). At most this
# many non-storm Problems travel, ranked by impact; every
# release_regression storms on top, untruncated.
CEO_RELIABILITY_MAX = 10
CEO_RELIABILITY_AFFECTED_MAX = 20

#: Problem statuses the CEO sees (open diagnosis pipeline states).
_RELIABILITY_VISIBLE_STATUSES = frozenset({
    "open", "diagnosed", "ticketed", "regressed",
})

#: Statuses counting as an active Problem for the operative readout.
_RELIABILITY_ACTIVE_STATUSES = frozenset({
    "open", "diagnosing", "diagnosed", "needs_split", "ticketed",
    "fix_merged", "verifying", "regressed",
})

#: Transitions that wake the CEO, grouped by Problem + transition.
#: 20 equivalent events render the same reason and collapse to one.
_RELIABILITY_WAKE_TRANSITIONS = frozenset({
    "new_critical",
    "new_high",
    "regressed",
    "release_regression",
    "brake_enforce_stop",
})

#: Hunter-health keys. Unknown stays None, never 0.
_RELIABILITY_HEALTH_KEYS = (
    "capture_failures",
    "spool_pending",
    "spool_discards",
    "suppressed_incidents",
    "budget_exhausted",
    "worker_restarts",
    "shadow_would_stops",
)

_OPEN_CHILD = frozenset({"queued", "in_progress", "waiting"})
_BLOCKED_CHILD = frozenset({"failed", "escalated", "interrupted"})
_CLOSED_CHILD = frozenset({"completed", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _short(text: Any, limit: int = 200) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)] + "..."


def _secret_blocked(ticket: Mapping[str, Any]) -> bool:
    params = ticket.get("params")
    return isinstance(params, dict) and isinstance(
        params.get(SECRET_BLOCK_PARAM), dict
    )


def _reliability_severity_rank(value: Any) -> int:
    return {"critical": 2, "high": 1}.get(
        str(value or "").strip().lower(), 0)


def _reliability_counts(problem: Mapping[str, Any]) -> dict[str, Any]:
    raw = problem.get("counts")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _reliability_impact_key(problem: Mapping[str, Any]) -> tuple[int, int, int]:
    counts = _reliability_counts(problem)
    try:
        occurrences = int(counts.get("occurrences", 0))
    except (TypeError, ValueError):
        occurrences = 0
    try:
        tickets = int(counts.get("distinct_tickets", 0))
    except (TypeError, ValueError):
        tickets = 0
    return (
        _reliability_severity_rank(problem.get("severity_max")),
        occurrences,
        tickets,
    )


def _is_storm_problem(problem: Mapping[str, Any]) -> bool:
    if str(problem.get("kind") or "") == "release_regression":
        return True
    return str(problem.get("_id") or "").startswith("release_regression:")


def _summarize_problem(problem: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded CEO view of one Problem (design v2 §12)."""
    counts = _reliability_counts(problem)
    ticket = problem.get("ticket")
    ticket_view = (
        {
            "business_ref": ticket.get("business_ref"),
            "source_id": ticket.get("source_id"),
            "type": ticket.get("type"),
        }
        if isinstance(ticket, Mapping) else None
    )
    affected = problem.get("affected_open_tasks")
    affected_list = (
        [str(item) for item in affected] if isinstance(affected, list)
        else []
    )
    return {
        "problem_key": str(problem.get("_id") or ""),
        "scope": problem.get("scope"),
        "kind": problem.get("kind"),
        "component": problem.get("component"),
        "attribution": problem.get("attribution"),
        "severity": problem.get("severity_max"),
        "counts": {
            "occurrences": counts.get("occurrences"),
            "suppressed": counts.get("suppressed"),
            "distinct_tickets": counts.get("distinct_tickets"),
            "distinct_projects": counts.get("distinct_projects"),
        },
        "status": problem.get("status"),
        "ticket": ticket_view,
        "affected_open_tasks": affected_list[:CEO_RELIABILITY_AFFECTED_MAX],
        "affected_open_tasks_total": len(affected_list),
    }


def compile_reliability(
    problems: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Rank visible Problems; storms always travel (design v2 §12).

    Returns ``(top, truncated, environment_active)``: at most
    ``CEO_RELIABILITY_MAX`` non-storm Problems by impact, every
    release_regression storm, whether the regular list was cut, and
    the active environment Problem count for the operative readout.
    """
    docs = [item for item in (problems or [])
            if isinstance(item, Mapping)]
    storms = sorted(
        (_summarize_problem(item) for item in docs
         if _is_storm_problem(item)),
        key=lambda view: str(view["problem_key"]),
    )
    visible = sorted(
        (item for item in docs
         if not _is_storm_problem(item)
         and str(item.get("status") or "") in _RELIABILITY_VISIBLE_STATUSES),
        key=_reliability_impact_key,
        reverse=True,
    )
    truncated = len(visible) > CEO_RELIABILITY_MAX
    top = [_summarize_problem(item) for item in visible[:CEO_RELIABILITY_MAX]]
    environment_active = sum(
        1 for item in docs
        if str(item.get("attribution") or "") == "environment"
        and str(item.get("status") or "") in _RELIABILITY_ACTIVE_STATUSES
    )
    return top + storms, truncated, environment_active


def reliability_wake_reason(event: Mapping[str, Any] | None) -> str | None:
    """Stable CEO wake reason for one hunter transition, else None.

    The reason names only Problem + transition, so 20 equivalent
    events render one identical reason and the loop debounce keeps a
    single notice (design v2 §12).
    """
    if not isinstance(event, Mapping):
        return None
    transition = str(
        event.get("transition") or event.get("kind") or ""
    ).strip().lower()
    if transition not in _RELIABILITY_WAKE_TRANSITIONS:
        return None
    problem = event.get("problem")
    key = (
        event.get("problem_key") or event.get("problem_id")
        or (problem.get("_id") if isinstance(problem, Mapping) else None)
    )
    text = str(key or "").strip()
    if not text:
        return None
    return f"Reliability {text} {transition}"


def collapse_reliability_wake_reasons(
    events: Sequence[Mapping[str, Any]] | None,
) -> list[str]:
    """Group hunter events by Problem + transition, order kept."""
    reasons: list[str] = []
    for event in events or []:
        reason = reliability_wake_reason(event)
        if reason is not None and reason not in reasons:
            reasons.append(reason)
    return reasons


def new_review_id(now: datetime | None = None) -> str:
    """Review identity: ``CEO-<UTC stamp>-<rand>`` (source_id root)."""
    import secrets

    moment = now or _utcnow()
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    return f"CEO-{stamp}-{secrets.token_hex(2).upper()}"


@dataclass
class CEOEnv:
    """Everything one CEO review needs (wired by dispatch)."""

    mandate: MandatoEmpresa | None
    objectives: engine_mod.ObjectiveStore
    tasks: engine_mod.ObjectiveTaskLister
    open_titles: Callable[[int], Awaitable[list[str]]]
    open_escalations: Callable[[], Awaitable[list[dict[str, Any]]]]
    recently_closed: Callable[[int], Awaitable[list[dict[str, Any]]]]
    blocked_tasks: Callable[[int], Awaitable[list[dict[str, Any]]]]
    recent_research: Callable[[int], Awaitable[list[dict[str, Any]]]]
    business_open: Callable[[], Awaitable[dict[str, Any]]]
    read_kpi: Callable[[str], Awaitable[CEOKpiReading]]
    known_projects: Collection[str]
    ops_project: str
    create_ticket: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
    escalate: Callable[..., Awaitable[dict[str, Any]]]
    executor: engine_mod.Runner
    repo_root: Path
    publish: Callable[[dict[str, Any]], Awaitable[None]]
    ceo_cfg: dict[str, Any] = field(default_factory=dict)
    now: Callable[[], datetime] | None = None
    # Missions (TASK-2026-00218). None in envs without mission wiring:
    # no MISSIONS signals and mission decisions fail as unknown.
    missions: Any | None = None
    patch_task: (
        Callable[[str, dict[str, Any]], Awaitable[None]] | None
    ) = None
    close_mission: (
        Callable[..., Awaitable[dict[str, Any]]] | None
    ) = None
    # Reliability hunter (TASK-2026-00380, design v2 §12). Both readers
    # are optional: envs without hunter wiring compile the section as
    # unknown instead of narrowing the rest of the review.
    reliability_problems: (
        Callable[[], Awaitable[list[dict[str, Any]]]] | None
    ) = None
    reliability_health: (
        Callable[[], Awaitable[dict[str, Any] | None]] | None
    ) = None

    def moment(self) -> datetime:
        return self.now() if self.now is not None else _utcnow()


@dataclass
class CEOSignals:
    """Bounded company state compiled for one review."""

    axes: list[dict[str, Any]] = field(default_factory=list)
    principios: list[str] = field(default_factory=list)
    missions: list[dict[str, Any]] = field(default_factory=list)
    missions_unknown: bool = False
    limits: dict[str, Any] = field(default_factory=dict)
    spend_threshold: str = ""
    spend_currency: str = ""
    business_rows: list[dict[str, Any]] = field(default_factory=list)
    business_unknown: bool = False
    objectives: list[dict[str, Any]] = field(default_factory=list)
    objectives_unknown: bool = False
    closed_rows: list[dict[str, Any]] = field(default_factory=list)
    closed_unknown: bool = False
    blocked_rows: list[dict[str, Any]] = field(default_factory=list)
    blocked_unknown: bool = False
    research_rows: list[dict[str, Any]] = field(default_factory=list)
    research_unknown: bool = False
    open_titles: list[str] = field(default_factory=list)
    open_titles_unknown: bool = False
    open_escalations: list[dict[str, Any]] = field(default_factory=list)
    escalations_unknown: bool = False
    kpi_readings: dict[str, CEOKpiReading] = field(default_factory=dict)
    reliability_problems: list[dict[str, Any]] = field(default_factory=list)
    reliability_truncated: bool = False
    reliability_unknown: bool = False
    reliability_environment_active: int | None = None
    reliability_health: dict[str, Any] = field(default_factory=dict)
    reliability_health_unknown: bool = False


def _objective_goal(objective: Mapping[str, Any]) -> str:
    anchor = objective.get("anchor")
    if isinstance(anchor, dict) and str(anchor.get("goal") or "").strip():
        return str(anchor.get("goal"))
    return str(objective.get("goal") or "")


async def _objective_digest(
    env: CEOEnv, objective: Mapping[str, Any]
) -> dict[str, Any]:
    objective_id = str(objective.get("objective_id") or "")
    try:
        children = await env.tasks.list_by_objective(objective_id)
    except Exception:  # noqa: BLE001
        logger.warning("CEO: children of %s unreadable", objective_id)
        children = None
    if children is None:
        open_count = blocked_count = closed_count = None
    else:
        rows = list(children)[:CEO_CHILDREN_PER_OBJECTIVE]
        open_count = sum(
            1
            for item in rows
            if str(item.get("status") or "") in _OPEN_CHILD
            and not _secret_blocked(item)
        )
        blocked_count = sum(
            1
            for item in rows
            if str(item.get("status") or "") in _BLOCKED_CHILD
            or _secret_blocked(item)
        )
        closed_count = sum(
            1
            for item in rows
            if str(item.get("status") or "") in _CLOSED_CHILD
        )
    gap = objective.get("capability_gap")
    gap_summary = ""
    if isinstance(gap, dict):
        gap_summary = _short(gap.get("summary"), 300)
    telemetry = objective.get("planner_telemetry")
    runs = objective.get("planning_runs") or []
    digest: dict[str, Any] = {
        "objective_id": objective_id,
        "project": objective.get("project"),
        "entry": objective.get("entry"),
        "goal": _short(_objective_goal(objective), 300),
        "goal_full": str(_objective_goal(objective)),
        "status": objective.get("status"),
        "waiting_reason": _short(objective.get("waiting_reason"), 160),
        "open_children": open_count,
        "blocked_children": blocked_count,
        "closed_children": closed_count,
        "capability_gap": gap_summary,
        "deferred": [
            _short(item, 160)
            for item in list(objective.get("deferred") or [])[
                :CEO_DEFERRED_PER_OBJECTIVE
            ]
        ],
        "roadmap_remaining": [
            _short(item, 160)
            for item in list(objective.get("roadmap_remaining") or [])[
                :CEO_ROADMAP_PER_OBJECTIVE
            ]
        ],
        "planning_runs": len(runs) if isinstance(runs, list) else None,
        "tickets_created": (
            telemetry.get("tickets_created")
            if isinstance(telemetry, dict)
            else None
        ),
        "material_failures": len(objective.get("material_failures") or []),
    }
    try:
        digest["tier_c_late_failure_rate"] = (
            engine_mod.tier_c_late_failure_rate(objective.get("tier_c"))
        )
    except Exception:  # noqa: BLE001
        digest["tier_c_late_failure_rate"] = None
    rows = list(children)[:CEO_CHILDREN_PER_OBJECTIVE] \
        if children is not None else None
    digest["cost"] = (
        cost_mod.aggregate_objective_cost(
            [item.get("cost") for item in rows])
        if rows is not None else None
    )
    return digest


async def _mission_digest(
    env: CEOEnv, mission: Mapping[str, Any]
) -> dict[str, Any]:
    mission_id = str(mission.get("mission_id") or "")
    ledger = [item for item in mission.get("objectives") or []
              if isinstance(item, Mapping)][:CEO_OBJECTIVES_PER_MISSION]
    children: list[dict[str, Any]] = []
    costs: list[Any] = []
    for item in ledger:
        objective_id = str(item.get("objective_id") or "")
        try:
            objective = await env.objectives.get_objective(objective_id)
        except Exception:  # noqa: BLE001
            logger.warning("CEO: mission objective %s unreadable",
                           objective_id)
            objective = None
        if objective is None:
            children.append({"objective_id": objective_id,
                             "status": "unknown"})
            continue
        status = str(objective.get("status") or "")
        children.append({"objective_id": objective_id, "status": status})
        try:
            tickets = await env.tasks.list_by_objective(objective_id)
        except Exception:  # noqa: BLE001
            logger.warning("CEO: children of %s unreadable", objective_id)
            tickets = None
        if tickets is not None:
            costs.extend(item.get("cost")
                         for item in list(tickets)[:CEO_CHILDREN_PER_OBJECTIVE])
    shell_status = ""
    shell_id = str(mission.get("conversion_source_task_id") or "")
    if shell_id:
        try:
            shell = await env.tasks.get_task(shell_id)
        except Exception:  # noqa: BLE001
            logger.warning("CEO: mission shell %s unreadable", shell_id)
            shell = None
        if isinstance(shell, Mapping):
            shell_status = str(shell.get("status") or "")
    question_state = ""
    question_id = str(mission.get("question_task_id") or "")
    if question_id:
        try:
            question = await env.tasks.get_task(question_id)
        except Exception:  # noqa: BLE001
            logger.warning("CEO: mission question %s unreadable",
                           question_id)
            question = None
        if isinstance(question, Mapping):
            from owner_channel import question_answered
            if question_answered(question):
                question_state = "answered"
            elif str(question.get("status") or "") in {
                    "completed", "cancelled"}:
                question_state = "closed without recorded answer"
            else:
                question_state = (
                    "open (deadline "
                    f"{mission.get('question_deadline')})"
                )
    return {
        "mission_id": mission_id,
        "project": mission.get("project"),
        "goal": _short(mission.get("goal"), 300),
        "status": mission.get("status"),
        "shell_status": shell_status,
        "acceptance": [str(item) for item in mission.get("acceptance") or []],
        "acceptance_source": mission.get("acceptance_source"),
        "acceptance_status": mission.get("acceptance_status"),
        "acceptance_disputed": bool(mission.get("acceptance_disputed")),
        "question_state": question_state,
        "objectives": children,
        "cost": cost_mod.aggregate_mission_cost(costs) if costs else None,
    }


async def compile_signals(env: CEOEnv) -> CEOSignals:
    """Compile the bounded signals. Unknown stays unknown, never zero."""
    signals = CEOSignals()
    mandate = env.mandate
    if mandate is not None:
        signals.principios = [str(item) for item in mandate.principios]
        for eje in mandate.ejes:
            kpis = []
            for kpi in eje.kpis:
                try:
                    reading = await env.read_kpi(kpi.fuente)
                except Exception:
                    logger.warning(
                        "CEO: KPI read failed for %s:%s",
                        eje.id, kpi.id, exc_info=True,
                    )
                    reading = CEOKpiReading(value=None, unknown=True)
                if not isinstance(reading, CEOKpiReading):
                    reading = CEOKpiReading(value=None, unknown=True)
                signals.kpi_readings[f"{eje.id}:{kpi.id}"] = reading
                kpis.append(
                    {
                        "id": kpi.id,
                        "fuente": kpi.fuente,
                        "reading": None if reading.unknown else reading.value,
                        "unknown": reading.unknown,
                    }
                )
            signals.axes.append(
                {"id": eje.id, "meta": eje.meta, "kpis": kpis}
            )
        signals.limits = {
            "objetivos_activos": mandate.limites.objetivos_activos,
            "objetivos_activos_por_proyecto": (
                mandate.limites.objetivos_activos_por_proyecto
            ),
            "tickets_activos_por_objetivo": (
                mandate.limites.tickets_activos_por_objetivo
            ),
        }
        signals.spend_threshold = str(
            mandate.presupuesto.gasto_nuevo_sin_preguntar
        )
        signals.spend_currency = mandate.presupuesto.moneda

    try:
        business = await env.business_open()
    except Exception:
        logger.warning("CEO: business state unreadable", exc_info=True)
        business = None
    if not isinstance(business, dict):
        signals.business_unknown = True
    else:
        signals.business_unknown = bool(business.get("unknown", False))
        rows = business.get("rows") or []
        signals.business_rows = list(rows)[:CEO_BUSINESS_ROWS]

    try:
        active = await env.objectives.list_active_objectives()
    except Exception:
        logger.warning("CEO: objectives unreadable", exc_info=True)
        active = None
    if active is None:
        signals.objectives_unknown = True
    else:
        for objective in list(active)[:CEO_OBJECTIVES]:
            signals.objectives.append(
                await _objective_digest(env, objective)
            )

    if env.missions is None:
        signals.missions = []
    else:
        try:
            missions = await env.missions.list_active_missions()
        except Exception:
            logger.warning("CEO: missions unreadable", exc_info=True)
            missions = None
        if missions is None:
            signals.missions_unknown = True
        else:
            for mission in list(missions)[:CEO_MISSIONS]:
                signals.missions.append(
                    await _mission_digest(env, mission)
                )

    try:
        signals.closed_rows = list(
            await env.recently_closed(CEO_CLOSED_ROWS)
        )[:CEO_CLOSED_ROWS]
    except Exception:
        logger.warning("CEO: recently closed unreadable", exc_info=True)
        signals.closed_unknown = True
    try:
        signals.blocked_rows = list(
            await env.blocked_tasks(CEO_BLOCKED_ROWS)
        )[:CEO_BLOCKED_ROWS]
    except Exception:
        logger.warning("CEO: blocked tasks unreadable", exc_info=True)
        signals.blocked_unknown = True
    try:
        signals.research_rows = list(
            await env.recent_research(CEO_RESEARCH_ROWS)
        )[:CEO_RESEARCH_ROWS]
    except Exception:
        logger.warning("CEO: research results unreadable", exc_info=True)
        signals.research_unknown = True
    try:
        signals.open_titles = [
            str(item) for item in await env.open_titles(CEO_OPEN_TITLES)
        ][:CEO_OPEN_TITLES]
    except Exception:
        logger.warning("CEO: open titles unreadable", exc_info=True)
        signals.open_titles_unknown = True
    try:
        rows = await env.open_escalations()
        signals.open_escalations = [
            {
                "category": str(item.get("category") or ""),
                "human_action": _short(item.get("human_action"), 300),
            }
            for item in (rows or [])
            if isinstance(item, Mapping)
        ]
    except Exception:
        logger.warning("CEO: open escalations unreadable", exc_info=True)
        signals.escalations_unknown = True
    if env.reliability_problems is None:
        signals.reliability_unknown = True
    else:
        try:
            problems = await env.reliability_problems()
        except Exception:
            logger.warning("CEO: reliability problems unreadable",
                           exc_info=True)
            problems = None
        if problems is None:
            signals.reliability_unknown = True
        else:
            top, truncated, environment_active = compile_reliability(problems)
            signals.reliability_problems = top
            signals.reliability_truncated = truncated
            signals.reliability_environment_active = environment_active
    if env.reliability_health is None:
        signals.reliability_health_unknown = True
    else:
        try:
            health = await env.reliability_health()
        except Exception:
            logger.warning("CEO: hunter health unreadable", exc_info=True)
            health = None
        if not isinstance(health, Mapping):
            signals.reliability_health_unknown = True
        else:
            signals.reliability_health = {
                key: health.get(key) for key in _RELIABILITY_HEALTH_KEYS
            }
    return signals


def _mandate_section(signals: CEOSignals) -> str:
    lines = ["MANDATE"]
    lines.append("PRINCIPLES (optimize every decision for these):")
    for item in signals.principios or list(MANDATE_PRINCIPLES):
        lines.append(f"- {item}")
    if not signals.axes:
        lines.append(
            "(no axes declared: pedidos carry a written justificacion "
            "instead of a KPI link)"
        )
    for eje in signals.axes:
        lines.append(f"- eje {eje['id']}: {_short(eje['meta'], 160)}")
        if not eje["kpis"]:
            lines.append("  kpis: (none declared)")
        for kpi in eje["kpis"]:
            reading = "UNKNOWN" if kpi["unknown"] else repr(kpi["reading"])
            lines.append(
                f"  kpi {kpi['id']} (fuente {kpi['fuente']}): {reading}"
            )
    lines.append(
        "limits: "
        + ", ".join(
            f"{key}={signals.limits.get(key)}" for key in (
                "objetivos_activos",
                "objetivos_activos_por_proyecto",
                "tickets_activos_por_objetivo",
            )
        )
    )
    lines.append(
        f"new spend without asking: {signals.spend_threshold} "
        f"{signals.spend_currency}"
    )
    return "\n".join(lines)


def _objectives_section(signals: CEOSignals) -> str:
    if signals.objectives_unknown:
        return "OBJECTIVES\nUNKNOWN (objectives unreadable this review)"
    if not signals.objectives:
        return "OBJECTIVES\n(none active)"
    lines = ["OBJECTIVES"]
    for item in signals.objectives:
        lines.append(
            f"- {item['objective_id']} [{item['project']}] "
            f"({item['status']}): {item['goal']}"
        )
        lines.append(
            f"  children open={item['open_children']} "
            f"blocked={item['blocked_children']} "
            f"closed={item['closed_children']}; "
            f"planning_runs={item['planning_runs']} "
            f"tickets_created={item['tickets_created']} "
            f"material_failures={item['material_failures']} "
            f"tier_c_late_failure_rate="
            f"{item['tier_c_late_failure_rate']}"
        )
        if item["waiting_reason"]:
            lines.append(f"  waiting: {item['waiting_reason']}")
        if item["capability_gap"]:
            lines.append(f"  capability_gap: {item['capability_gap']}")
        if isinstance(item.get("cost"), dict):
            lines.append(f"  cost: {cost_mod.cost_summary_line(item['cost'])}")
        for deferred in item["deferred"]:
            lines.append(f"  deferred: {deferred}")
        for roadmap in item["roadmap_remaining"]:
            lines.append(f"  roadmap_remaining: {roadmap}")
    return "\n".join(lines)


def _missions_section(signals: CEOSignals) -> str:
    if signals.missions_unknown:
        return "MISSIONS\nUNKNOWN (missions unreadable this review)"
    if not signals.missions:
        return "MISSIONS\n(none active)"
    lines = ["MISSIONS"]
    for item in signals.missions:
        lines.append(
            f"- {item['mission_id']} [{item['project']}] "
            f"({item['status']}): {item['goal']}"
        )
        criteria = item["acceptance"] or ["(no criteria yet: propose them "
                                          "in your next mission pedido)"]
        lines.append(
            f"  acceptance ({item['acceptance_source']}/"
            f"{item['acceptance_status']}"
            f"{', DISPUTED' if item['acceptance_disputed'] else ''}): "
            f"{'; '.join(str(criterion) for criterion in criteria)[:400]}"
        )
        if item["question_state"]:
            lines.append(f"  owner question: {item['question_state']}")
        children = item["objectives"] or []
        states = ", ".join(
            f"{child['objective_id']}={child['status']}"
            for child in children) or "(no objectives yet)"
        lines.append(f"  objectives: {states}")
        if isinstance(item.get("cost"), dict):
            lines.append(f"  cost: {cost_mod.cost_summary_line(item['cost'])}")
    return "\n".join(lines)


def _reliability_value(value: Any) -> str:
    return "UNKNOWN" if value is None else repr(value)


def _reliability_section(signals: CEOSignals) -> str:
    """Deterministic hunter readout: top Problems plus hunter health."""
    if signals.reliability_unknown:
        return "FIABILIDAD\nUNKNOWN (problems unreadable this review)"
    lines = ["FIABILIDAD"]
    if not signals.reliability_problems:
        lines.append("(no visible problems)")
    for item in signals.reliability_problems:
        counts = item.get("counts") or {}
        ticket = item.get("ticket") or {}
        affected_total = item.get("affected_open_tasks_total", 0)
        lines.append(
            f"- {item.get('problem_key')} [{item.get('scope')}] "
            f"({item.get('status')}): {item.get('kind')}/"
            f"{item.get('component')} attr={item.get('attribution')} "
            f"sev={item.get('severity')} "
            f"occ={counts.get('occurrences')} "
            f"supp={counts.get('suppressed')} "
            f"tickets={counts.get('distinct_tickets')} "
            f"projects={counts.get('distinct_projects')} "
            f"ticket={ticket.get('business_ref') or ticket.get('source_id')} "
            f"affected_open={affected_total}"
        )
    lines.append(
        "truncated: "
        + ("yes (showing top "
           f"{CEO_RELIABILITY_MAX} by impact plus every "
           "release_regression storm)"
           if signals.reliability_truncated else "no")
    )
    lines.append(
        "environment_active: "
        + _reliability_value(signals.reliability_environment_active)
    )
    if signals.reliability_health_unknown:
        lines.append("hunter health: UNKNOWN (unreadable this review)")
    else:
        health = signals.reliability_health or {}
        lines.append(
            "hunter health: "
            + ", ".join(
                f"{key}={_reliability_value(health.get(key))}"
                for key in _RELIABILITY_HEALTH_KEYS
            )
        )
    return "\n".join(lines)


def _rows_section(
    title: str, rows: Sequence[Mapping[str, Any]], unknown: bool
) -> str:
    if unknown:
        return f"{title}\nUNKNOWN (unreadable this review)"
    if not rows:
        return f"{title}\n(none)"
    lines = [title]
    for row in rows:
        if title == "BUSINESS OPEN":
            lines.append(
                f"- {row.get('erp_name')} [{row.get('project')}] "
                f"({row.get('status')}): {_short(row.get('subject'), 140)}"
            )
        elif title == "RECENTLY CLOSED":
            lines.append(
                f"- {row.get('erp_name') or row.get('task_id')} "
                f"[{row.get('project')}]: {_short(row.get('title'), 140)}"
            )
        elif title == "BLOCKED":
            lines.append(
                f"- {row.get('erp_name') or row.get('task_id')} "
                f"[{row.get('project')}] ({row.get('status')}): "
                f"{_short(row.get('title'), 120)} — "
                f"{_short(row.get('reason'), 120)}"
            )
        elif title == "RESEARCH RESULTS":
            lines.append(
                f"- {row.get('research_id')}: "
                f"{_short(row.get('question'), 140)} "
                f"({_short(row.get('summary'), 140)})"
            )
        elif title == "OPEN ESCALATIONS":
            lines.append(
                f"- {row.get('category')}: "
                f"{_short(row.get('human_action'), 200)}"
            )
    return "\n".join(lines)


def _ceo_prompt(
    signals: CEOSignals,
    *,
    kind: str,
    reasons: Sequence[str],
    review_id: str,
    repo_root: Path,
    ops_project: str,
) -> str:
    """CEO prompt: bounded state + JSON contract (self-contained)."""
    reason_lines = "\n".join(
        f"- {reason}" for reason in list(reasons)[:CEO_PROMPT_REASONS]
    ) or "(scheduled review: no trigger reasons)"
    return "\n\n".join([
        "You are the autonomous company CEO of OpenCEO. This is review "
        f"{review_id} ({kind}). Decide exactly ONE action for the company. "
        "Doing nothing is a valid outcome: never invent work.",
        f"TRIGGER\n{reason_lines}",
        _mandate_section(signals),
        _rows_section(
            "BUSINESS OPEN", signals.business_rows,
            signals.business_unknown),
        _missions_section(signals),
        _objectives_section(signals),
        _reliability_section(signals),
        _rows_section(
            "RECENTLY CLOSED", signals.closed_rows,
            signals.closed_unknown),
        _rows_section(
            "BLOCKED", signals.blocked_rows, signals.blocked_unknown),
        _rows_section(
            "RESEARCH RESULTS", signals.research_rows,
            signals.research_unknown),
        _rows_section(
            "OPEN ESCALATIONS", signals.open_escalations,
            signals.escalations_unknown),
        "\n".join([
            "RULES",
            "- One action only: keep_going | crear_pedido | escalate | "
            "do_nothing | close_mission.",
            "- keep_going: open work suffices, nothing new needed now. "
            "do_nothing: no action is warranted at all.",
            "- crear_pedido states a need, never its form: the single "
            "planner decides ticket or objective, agent, skill and "
            "(dev) tier. Never name the outcome ticket or objective; "
            "describe the work.",
            "- A mission advances only through your mission pedidos: "
            "when a mission needs its next objective, crear_pedido "
            "with mission_id (filed under the mission group; the "
            "planner gives it objective form). Name the mission need "
            "in title/description. When a mission has no criteria "
            "yet, propose them in the pedido acceptance: adoption "
            "asks the Owner without blocking.",
            "- close_mission declares one mission complete: use it "
            "only when its acceptance is met on delivered evidence "
            "(every mission objective terminal, at least one "
            "closed). The deterministic validation rejects early or "
            "criteria-less closes.",
            "- crear_pedido links the mandate: when a KPI is measured "
            "this review (a reading, not UNKNOWN), link it via eje_id "
            "and kpi_ids; otherwise write why the work matters in "
            "justificacion. One of the two is REQUIRED; kpi_ids "
            "without eje_id is rejected.",
            "- acceptance carries the Owner's criteria when the "
            "trigger states them; otherwise propose them yourself: "
            "every pedido registers how its outcome is judged.",
            "- Unknown is null: never invent readings, tickets, or "
            "owners. An unreadable source narrows what you may decide.",
            "- No agent, skill or tier field anywhere: the single "
            "planner decides them.",
            "- Declare every new cost the work needs in costs (label "
            "plus a decimal-string amount, null when unknown): the "
            "spend gate holds it for the Owner before launch.",
            f"- A new company project is a pedido filed in {ops_project} "
            "with project_key and display_name; the operations skill "
            "creates it. Other pedidos are filed in the project the "
            "work belongs to.",
            "- escalate ONLY with a policy cause: new spend over the "
            "threshold (amount as a decimal string), an irreversible "
            "external action, a strategic replanning, a mandate change, "
            "or a policy-mandated human review. Routine work never "
            "escalates. An already-open identical escalation needs no "
            "duplicate.",
            "- FIABILIDAD informs prioritization and escalation, never "
            "autonomy over the hunter: never approve hunter admission "
            "(§9.3) or protected-path (§9.4) gates and never change "
            "hunter configuration through any pedido. Reliability "
            "wakes group by Problem and transition: one notice covers "
            "equivalent events.",
            "- The deterministic validation rejects duplicates, "
            "unknown-project, unjustified pedidos, and causeless "
            "escalations. Propose work that survives it.",
        ]),
        "\n".join([
            f"OUTPUT: write {CEO_DECISION_FILE} under {repo_root}/.openceo/ "
            "with exactly this JSON (no other file, no other text):",
            '{"action": "<one of the five>", "summary": "<one line>", '
            '"pedido": {"project": "...", "title": "...", '
            '"description": "...", "acceptance": [...], '
            '"eje_id?": "...", "kpi_ids?": ["..."], '
            '"justificacion?": "<why, when no KPI is measured>", '
            '"mission_id?": "<mission to chain as its next objective>", '
            '"costs": [{"label": "...", "amount": '
            '"..."|null}], "project_key": "...", "display_name": "..."} '
            '| "escalation": '
            '{"kind": "...", "summary": "...", "project": null, '
            '"amount": null, "uses_paid_quota": false, '
            '"irreversible": false, "strategic_replan": false, '
            '"mandate_change": false, '
            '"policy_requires_human_review": false} '
            '| "mission": {"mission_id": "<mission to close>"}}',
            "keep_going and do_nothing carry action+summary only; "
            "costs and project_key/display_name are optional (omit the "
            "keys when the pedido needs neither); eje_id/kpi_ids or "
            "justificacion is REQUIRED (omit the keys you do not use).",
        ]),
    ])


def _ceo_retry_prompt(errors: Sequence[str], repo_root: Path) -> str:
    return (
        f"Your {CEO_DECISION_FILE} is invalid: {'; '.join(errors)}\n"
        "Fix exactly that (same five actions, same rules) and rewrite "
        f"the file under {repo_root}/.openceo/. No other text."
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _missing_decision_error(execution: Any) -> str:
    base = f"no {CEO_DECISION_FILE}"
    if getattr(execution, "returncode", 0):
        tail = str(getattr(execution, "output_tail", "") or "")[-500:]
        if tail:
            return f"{base} (ceo process failed ({execution.returncode}): {tail})"
        return f"{base} (ceo process failed ({execution.returncode}))"
    return base


def _validation_context(env: CEOEnv, signals: CEOSignals) -> CEOValidationContext:
    return CEOValidationContext(
        mandate=env.mandate,
        known_projects=list(env.known_projects),
        ops_project=str(env.ops_project or ""),
        open_titles=list(signals.open_titles),
        open_titles_unknown=signals.open_titles_unknown,
        active_objectives=[
            {
                "objective_id": item.get("objective_id"),
                "project": item.get("project"),
                "goal": item.get("goal_full") or item.get("goal"),
            }
            for item in signals.objectives
        ],
        objectives_unknown=signals.objectives_unknown,
        open_escalations=list(signals.open_escalations),
        escalations_unknown=signals.escalations_unknown,
        kpi_readings=dict(signals.kpi_readings),
        missions={
            str(item.get("mission_id") or ""): {
                "status": item.get("status"),
                "shell_status": item.get("shell_status"),
                "acceptance": list(item.get("acceptance") or []),
            }
            for item in signals.missions
        },
        mission_objectives={
            str(item.get("mission_id") or ""): [
                {"objective_id": str(child.get("objective_id") or ""),
                 "status": str(child.get("status") or "")}
                for child in item.get("objectives") or []
            ]
            for item in signals.missions
        },
        missions_unknown=signals.missions_unknown,
    )


def _review_status(signals: CEOSignals) -> dict[str, Any]:
    """Published status: advanced / blocked / awaiting the owner."""
    advanced = [
        {
            "ref": str(row.get("erp_name") or row.get("task_id") or ""),
            "title": _short(row.get("title"), 140),
        }
        for row in signals.closed_rows[:CEO_STATUS_ROWS]
    ]
    blocked = [
        {
            "ref": str(row.get("erp_name") or row.get("task_id") or ""),
            "title": _short(row.get("title"), 140),
            "reason": _short(row.get("reason"), 140),
        }
        for row in signals.blocked_rows[:CEO_STATUS_ROWS]
    ]
    awaiting = [
        {
            "category": str(row.get("category") or ""),
            "human_action": _short(row.get("human_action"), 200),
        }
        for row in signals.open_escalations[:CEO_STATUS_ROWS]
    ]
    return {
        "advanced": advanced,
        "advanced_unknown": signals.closed_unknown,
        "blocked": blocked,
        "blocked_unknown": signals.blocked_unknown,
        "awaiting_owner": awaiting,
        "awaiting_unknown": signals.escalations_unknown,
        "reliability": [
            {
                "ref": str(row.get("problem_key") or ""),
                "title": (
                    f"{row.get('kind')}/{row.get('component')} "
                    f"({row.get('status')})"
                ),
            }
            for row in signals.reliability_problems[:CEO_STATUS_ROWS]
        ],
        "reliability_unknown": signals.reliability_unknown,
        "reliability_truncated": signals.reliability_truncated,
    }


def _signals_digest(signals: CEOSignals) -> dict[str, Any]:
    measured = sum(
        1 for reading in signals.kpi_readings.values()
        if not reading.unknown
    )
    return {
        "axes": [eje["id"] for eje in signals.axes],
        "kpis_measured": measured,
        "kpis_unknown": len(signals.kpi_readings) - measured,
        "business_rows": len(signals.business_rows),
        "business_unknown": signals.business_unknown,
        "missions": len(signals.missions),
        "missions_unknown": signals.missions_unknown,
        "objectives": len(signals.objectives),
        "objectives_unknown": signals.objectives_unknown,
        "closed": len(signals.closed_rows),
        "closed_unknown": signals.closed_unknown,
        "blocked": len(signals.blocked_rows),
        "blocked_unknown": signals.blocked_unknown,
        "research": len(signals.research_rows),
        "research_unknown": signals.research_unknown,
        "open_escalations": len(signals.open_escalations),
        "escalations_unknown": signals.escalations_unknown,
        "reliability_problems": len(signals.reliability_problems),
        "reliability_truncated": signals.reliability_truncated,
        "reliability_unknown": signals.reliability_unknown,
        "reliability_environment_active": (
            signals.reliability_environment_active),
        "reliability_health_unknown": signals.reliability_health_unknown,
    }


async def _publish_record(env: CEOEnv, record: dict[str, Any]) -> None:
    try:
        await env.publish(record)
    except Exception:
        # Telemetry loss, not review failure: the outcome stands and
        # the next review republishes. Never break the loop on it.
        logger.warning(
            "CEO: review record %s not published",
            record.get("review_id"), exc_info=True,
        )


def _action_from_escalation_spec(
    spec: models_mod.EscalationSpec,
) -> OwnerAction:
    return OwnerAction(
        kind=str(spec.kind),
        summary=str(spec.summary),
        project=str(spec.project) if spec.project else None,
        amount=spec.amount,
        uses_paid_quota=bool(spec.uses_paid_quota),
        irreversible=bool(spec.irreversible),
        strategic_replan=bool(spec.strategic_replan),
        mandate_change=bool(spec.mandate_change),
        policy_requires_human_review=bool(
            spec.policy_requires_human_review),
        human_action_hint=spec.human_action_hint,
    )


async def _run_no_mandate_review(
    env: CEOEnv, *, kind: str, reasons: Sequence[str], review_id: str,
    moment: datetime,
) -> dict[str, Any]:
    """Fail-closed review without a valid mandate. Zero tokens, no model."""
    action = OwnerAction(
        kind="ceo_review",
        summary=(
            f"CEO review {review_id}: no valid company mandate, "
            "autonomy is closed"
        ),
    )
    try:
        open_escalations = await env.open_escalations()
        escalations_unknown = False
    except Exception:
        logger.warning("CEO: open escalations unreadable", exc_info=True)
        open_escalations, escalations_unknown = [], True
    decision = requires_owner(action, None)
    assert decision.escalate and decision.category is not None
    wanted = (
        decision.category.value,
        models_mod.normalize_title(decision.human_action),
    )
    duplicate = any(
        isinstance(item, Mapping)
        and (
            str(item.get("category") or "").strip(),
            models_mod.normalize_title(item.get("human_action")),
        ) == wanted
        for item in (open_escalations or [])
    )
    if duplicate:
        outcome: dict[str, Any] = {
            "ok": True, "action": "escalation_exists",
            "category": decision.category.value,
        }
    else:
        created = await env.escalate(
            action=action,
            decision=decision,
            source_id=f"ceo:{review_id}:escalation:{decision.category.value}",
        )
        outcome = {
            "ok": True, "action": "escalated",
            "category": decision.category.value,
            "task_id": created.get("task_id"),
        }
    record = {
        "review_id": review_id,
        "at": moment.isoformat().replace("+00:00", "Z"),
        "kind": kind,
        "reasons": list(reasons),
        "signals": {
            "axes": [], "kpis_measured": 0, "kpis_unknown": 0,
            "open_escalations": len(open_escalations or []),
            "escalations_unknown": escalations_unknown,
            "reliability_problems": 0,
            "reliability_truncated": False,
            "reliability_unknown": True,
            "reliability_environment_active": None,
            "reliability_health_unknown": True,
        },
        "decision": None,
        "errors": [],
        "outcome": outcome,
        "status": _review_status(CEOSignals(
            open_escalations=[
                {
                    "category": str(item.get("category") or ""),
                    "human_action": _short(item.get("human_action"), 300),
                }
                for item in (open_escalations or [])
                if isinstance(item, Mapping)
            ],
            escalations_unknown=escalations_unknown,
        )),
        "model_runs": 0,
    }
    await _publish_record(env, record)
    return {**outcome, "review_id": review_id}


async def _act_on_decision(
    env: CEOEnv, decision: CEODecision, *, review_id: str, kind: str,
) -> dict[str, Any]:
    """Perform exactly one validated action. Never raises for refusals."""
    if decision.action in (CEOAction.KEEP_GOING, CEOAction.DO_NOTHING):
        return {"ok": True, "action": decision.action.value}
    if decision.action == CEOAction.CLOSE_MISSION:
        spec = decision.mission
        assert spec is not None  # Shape-guarded.
        if env.close_mission is None:
            return {
                "ok": False, "action": "error",
                "reason": "mission service is not wired",
            }
        mission_id = str(spec.mission_id or "").strip()
        try:
            result = await env.close_mission(
                mission_id, reason=str(decision.summary))
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "action": "error",
                "reason": f"mission close failed: {exc}",
            }
        if not isinstance(result, Mapping) or not result.get("ok"):
            return {
                "ok": False, "action": "error",
                "reason": str((result or {}).get("reason")
                              or "mission close failed"),
            }
        return {
            "ok": True, "action": "mission_closed",
            "mission_id": mission_id,
        }
    if decision.action == CEOAction.CREAR_PEDIDO:
        spec = decision.pedido
        assert spec is not None  # Shape-guarded.
        eje_id = str(spec.eje_id or "").strip()
        if eje_id:
            citation = (
                f"[CEO review {review_id} ({kind})] Eje {eje_id}, "
                f"KPIs {', '.join(str(item) for item in spec.kpi_ids)}."
            )
        else:
            citation = (
                f"[CEO review {review_id} ({kind})] "
                f"Justificación: {_short(spec.justificacion, 160)}"
            )
        params: dict[str, Any] = {"ceo_review": review_id}
        if eje_id:
            params["eje_id"] = eje_id
            params["kpi_ids"] = [str(item) for item in spec.kpi_ids]
        if str(spec.justificacion or "").strip():
            params["justificacion"] = str(spec.justificacion)
        mission_id = str(spec.mission_id or "").strip()
        parent_task: str | None = None
        if mission_id:
            params["mission_id"] = mission_id
            if env.missions is not None:
                try:
                    mission = await env.missions.get_mission(mission_id)
                except Exception:  # noqa: BLE001
                    mission = None
                group = (str((mission or {}).get("erp_group_ref") or "")
                         .strip())
                parent_task = group or None
        if spec.costs:
            params["costs"] = [
                {"label": str(cost.label), "amount": cost.amount}
                for cost in spec.costs
            ]
        if spec.project_key is not None:
            params["project_key"] = str(spec.project_key)
            params["display_name"] = str(spec.display_name or "")
        payload: dict[str, Any] = {
            "title": str(spec.title),
            "description": f"{citation}\n\n{spec.description}",
            "project": str(spec.project),
            "source": "api",
            "source_id": f"ceo:{review_id}:pedido",
            "acceptance": [str(item) for item in spec.acceptance],
            "params": params,
            "tags": ["ceo-review"],
        }
        if parent_task:
            payload["parent_task"] = parent_task
        created = await env.create_ticket(payload)
        if created is None:
            return {
                "ok": False, "action": "error",
                "reason": "pedido creation failed",
            }
        return {
            "ok": True, "action": "pedido_created",
            "task_id": created.get("task_id"),
        }
    spec = decision.escalation
    assert spec is not None  # Shape-guarded.
    action = _action_from_escalation_spec(spec)
    escalation = requires_owner(action, env.mandate)
    assert escalation.escalate and escalation.category is not None
    created = await env.escalate(
        action=action,
        decision=escalation,
        source_id=(
            f"ceo:{review_id}:escalation:{escalation.category.value}"
        ),
    )
    return {
        "ok": True, "action": "escalated",
        "category": escalation.category.value,
        "task_id": created.get("task_id"),
    }


async def _expire_mission_questions(
    env: CEOEnv, moment: datetime,
) -> list[str]:
    """Confirm proposals whose Owner deadline passed silently.

    Deterministic bookkeeping, not an action: every mission with an
    unanswered acceptance question past its deadline keeps its
    proposal as confirmed, and the question closes with the note. A
    late Owner answer still applies afterwards. Never raises.
    """
    from objectives import missions as missions_mod

    expired: list[str] = []
    if env.missions is None or env.patch_task is None:
        return expired
    try:
        missions = await env.missions.list_active_missions()
    except Exception:
        logger.warning("CEO: missions unreadable for expiry", exc_info=True)
        return expired
    for mission in missions or []:
        question_id = str(mission.get("question_task_id") or "")
        if not question_id:
            continue
        try:
            question = await env.tasks.get_task(question_id)
        except Exception:  # noqa: BLE001
            logger.warning("CEO: mission question %s unreadable",
                           question_id)
            continue
        try:
            overdue = missions_mod.mission_question_expired(
                question, moment)
        except Exception:  # noqa: BLE001, S112
            continue
        if not overdue:
            continue
        mission_id = str(mission.get("mission_id") or "")
        try:
            await env.missions.patch_mission(
                mission_id, missions_mod.deadline_confirmation_patch())
            await env.patch_task(question_id, {
                "status": "completed",
                "waiting_reason": None,
                "resume_at": None,
                "completed_at": moment.isoformat().replace("+00:00", "Z"),
                "last_response_summary": (
                    "Plazo vencido sin respuesta: la propuesta del CEO "
                    "queda confirmada para la misión "
                    f"{mission_id}."),
            })
        except Exception:
            logger.warning("CEO: mission expiry failed for %s",
                           mission_id, exc_info=True)
            continue
        expired.append(mission_id)
    return expired


async def run_review(
    env: CEOEnv, *, kind: str, reasons: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Run one CEO review. Returns the outcome (never raises it).

    Without a valid mandate the review escalates deterministically and
    never invokes the model. Otherwise: expire silent mission
    deadlines, compile signals, one headless run (plus one retry on
    invalid output), validate, act once, publish the record.
    Unexpected failures become an ``error`` outcome with a published
    record — the loop stays alive.
    """
    if kind not in CEOReviewKind:
        raise ValueError(f"unknown CEO review kind {kind!r}")
    clean_reasons = [
        str(reason).strip()[:300]
        for reason in (reasons or []) if str(reason).strip()
    ]
    moment = env.moment()
    review_id = new_review_id(moment)
    if env.mandate is None:
        return await _run_no_mandate_review(
            env, kind=kind, reasons=clean_reasons,
            review_id=review_id, moment=moment,
        )
    expired_questions = await _expire_mission_questions(env, moment)
    signals = await compile_signals(env)
    ctx = _validation_context(env, signals)
    prompt = _ceo_prompt(
        signals, kind=kind, reasons=clean_reasons,
        review_id=review_id, repo_root=env.repo_root,
        ops_project=str(env.ops_project or ""),
    )
    scratch = env.repo_root / ".openceo"
    scratch.mkdir(parents=True, exist_ok=True)
    target = scratch / CEO_DECISION_FILE
    timeout_s = int((env.ceo_cfg or {}).get("timeout_minutes", 15)) * 60
    errors: list[str] = []
    decision: CEODecision | None = None
    model_runs = 0
    current_prompt = prompt
    for _attempt in range(2):
        try:
            target.unlink()
        except OSError:
            pass
        try:
            execution = await env.executor.run(
                CEO_EXECUTOR_ROLE, current_prompt, env.repo_root,
                timeout_s,
            )
            model_runs += 1
        except Exception as exc:  # noqa: BLE001
            errors = [f"ceo run failed: {exc}"]
            current_prompt = prompt
            continue
        text = _read_text(target)
        if not text:
            errors = [_missing_decision_error(execution)]
        else:
            try:
                candidate = models_mod.parse_ceo_decision(text)
            except ValueError as exc:
                errors = [str(exc)]
            else:
                errors = models_mod.validate_ceo_decision(candidate, ctx)
                if not errors:
                    decision = candidate
        if not errors:
            break
        if not any(
            error.startswith(DUPLICATE_ESCALATION_PREFIX)
            for error in errors
        ):
            current_prompt = _ceo_retry_prompt(errors, env.repo_root)
        else:
            break
    try:
        await env.executor.cleanup(CEO_EXECUTOR_ROLE)
    except Exception:  # noqa: BLE001, S110
        pass

    if decision is None and any(
        error.startswith(DUPLICATE_ESCALATION_PREFIX)
        for error in errors
    ):
        outcome: dict[str, Any] = {
            "ok": True, "action": "escalation_exists",
            "reason": errors[0],
        }
    elif decision is None:
        outcome = {
            "ok": False, "action": "rejected",
            "reason": "; ".join(errors) or "invalid decision",
        }
    else:
        try:
            outcome = await _act_on_decision(
                env, decision, review_id=review_id, kind=kind,
            )
        except Exception as exc:
            logger.warning(
                "CEO: review %s action failed", review_id, exc_info=True,
            )
            outcome = {
                "ok": False, "action": "error",
                "reason": f"{decision.action.value} failed: {exc}",
            }
    record = {
        "review_id": review_id,
        "at": moment.isoformat().replace("+00:00", "Z"),
        "kind": kind,
        "reasons": clean_reasons,
        "signals": _signals_digest(signals),
        "decision": (
            decision.model_dump(mode="json") if decision is not None
            else None
        ),
        "errors": list(errors),
        "outcome": outcome,
        "status": _review_status(signals),
        "model_runs": model_runs,
        "expired_questions": expired_questions,
    }
    await _publish_record(env, record)
    return {**outcome, "review_id": review_id}
