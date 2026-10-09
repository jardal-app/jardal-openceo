"""Company objectives: multi-ticket work chains (design §9, F6; F2 TASK-2026-00117).

One Objective type for every domain. Identity lives in ERPNext — a group Task
with real children (``parent_task``) and ``depends_on`` — while the
``objectives`` collection (§15.2) holds execution state only: capsule,
cycles, replans, final review and telemetry. Tickets are still created
gradually by the single global objective planner (Muse Code headless →
``PlannerResult`` JSON, validated with Pydantic + deterministic rules), with
OpenCEO-side dependencies, a deterministically compiled capsule, triggers
and loop guardrails. Not to be confused with Horizon objectives
(``horizon_objectives``): those are the CEO's personal goals.

Every objective has exactly one parent ticket that IS the ERP group: a
conversion reuses the oversized ticket, a top-down creation mints a shell
ticket (``origin=objective_shell``). Notifications, close and the inverse
webhook all flow through that parent ticket.

Entry points: :func:`create_objective` (top-down, for the CEO/owner; F5
Horizon promotion calls it too), :func:`convert_ticket` (a ticket outgrew
one session — inside its own objective when it already has one),
:func:`plan_objective` (next horizon of tickets), :func:`on_ticket_closed`
(triggers + capsule after any objective ticket closes) and
:func:`run_final_review` (planner declared ``complete_candidate``).
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from agents.dev.loose_tier import routing_guide_section
from orchestration import findings as findings_mod
from orchestration import inbox as inbox_mod
from orchestration import knowledge as knowledge_mod
from orchestration import review as review_mod
from orchestration.live import TIMEOUT_RETURNCODE
from objectives import planner_models as planner_mod


class Runner(Protocol):
    """Structural executor (see :class:`orchestration.live.Executor`)."""

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any: ...
    async def cleanup(self, role: str) -> None: ...


class Quota(Protocol):
    async def check(self, accounts: Sequence[str]) -> Mapping[str, Any]: ...


class ObjectiveErp(Protocol):
    """Business-identity operations the engine needs (implemented by wiring).

    The engine never imports the ERPNext adapter: group conversion is the
    only ERP write it performs directly (children link through the common
    ticket creation, close pushes through the parent ticket).
    """

    async def convert_to_group(self, identifier: str) -> dict[str, Any]: ...


class ObjectiveRejected(ValueError):
    """The objective was refused deterministically (bad input, no capacity)."""


class ObjectiveLimitReached(ObjectiveRejected):
    """An active-objective admission limit refused the creation."""


class ObjectiveDuplicate(ObjectiveRejected):
    """An active objective with the same project + goal already exists."""

    def __init__(self, message: str, existing_id: str | None = None) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class ObjectiveErpError(RuntimeError):
    """Business identity is unavailable (fail closed, never orphan)."""


class ObjectiveStore(Protocol):
    async def create_objective(self, doc: dict[str, Any]) -> dict[str, Any]: ...
    async def get_objective(self, objective_id: str) -> dict[str, Any] | None: ...
    async def patch_objective(
        self, objective_id: str, updates: dict[str, Any]) -> dict[str, Any] | None: ...
    async def list_active_objectives(
        self, project: str | None = None) -> list[dict[str, Any]]: ...


class ObjectiveTaskLister(Protocol):
    async def list_by_objective(self, objective_id: str) -> list[dict[str, Any]]: ...
    async def get_task(self, task_id: str) -> dict[str, Any] | None: ...


@dataclass
class ObjectiveEnv:
    """Everything the objective flows need (wired by dispatch)."""

    objectives: ObjectiveStore
    tasks: ObjectiveTaskLister
    create_ticket: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
    patch_task: Callable[[str, dict[str, Any]], Awaitable[None]]
    executor: Runner
    quota: Quota
    slots: review_mod.SlotStore
    reviews: review_mod.ReviewStore | None
    inbox: inbox_mod.InboxStore | None
    notify: Callable[[str, str], Awaitable[None]] | None
    repo: Path
    roles: dict[str, Any] = field(default_factory=dict)
    pool: list[review_mod.ReviewSlot] = field(default_factory=list)
    review_cfg: dict[str, Any] = field(default_factory=dict)
    objectives_cfg: dict[str, Any] = field(default_factory=dict)
    # Single global planner config (global objective_planner block: engine,
    # role, timeout, agents registry). Empty in bare unit envs: planning
    # then parks instead of validating against no registry.
    planner_cfg: dict[str, Any] = field(default_factory=dict)
    # Business identity (None in bare unit envs and legacy flows: the
    # objective then works without an ERP group, as before TASK-2026-00117).
    erp: ObjectiveErp | None = None
    timeouts: dict[str, int] = field(default_factory=dict)
    fix_tiers: dict[str, str] = field(default_factory=dict)
    inbox_rule: dict[str, Any] = field(default_factory=dict)
    auto_merge: bool = True
    base_branch: str = "main"
    vault: dict[str, str] | None = None  # {dir, project} (F7b)
    # Reactive CEO wake (F4 TASK-2026-00119): the engine calls it with a
    # reason when an objective parks or closes. Sync, never fatal, None
    # in bare unit envs and dev flows that do not wire the CEO loop.
    ceo_wake: Callable[[str], None] | None = None
    # Normalized per-project tier routing (TASK-2026-00161): the
    # ``dev.routing`` mapping for the objective's project (``{}`` when the
    # project carries no characteristic). Feeds the planner prompt guide.
    project_routing: dict[str, Any] = field(default_factory=dict)


class InMemoryObjectiveStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def create_objective(self, doc: dict[str, Any]) -> dict[str, Any]:
        stored = dict(doc)
        self.docs[stored["objective_id"]] = stored
        return dict(stored)

    async def get_objective(self, objective_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(objective_id)
        return dict(doc) if doc is not None else None

    async def patch_objective(
        self, objective_id: str, updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        doc = self.docs.get(objective_id)
        if doc is None:
            return None
        doc.update(updates)
        return dict(doc)

    async def list_active_objectives(
        self, project: str | None = None,
    ) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.docs.values()
                if doc.get("status") == "active"
                and (not project or doc.get("project") == project)]


class MongoObjectiveStore:
    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def create_objective(self, doc: dict[str, Any]) -> dict[str, Any]:
        return await self._repo.create_objective(doc)

    async def get_objective(self, objective_id: str) -> dict[str, Any] | None:
        return await self._repo.get_objective(objective_id)

    async def patch_objective(
        self, objective_id: str, updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._repo.patch_objective(objective_id, updates)

    async def list_active_objectives(
        self, project: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._repo.list_active_objectives(project)


class MongoObjectiveTaskLister:
    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def list_by_objective(self, objective_id: str) -> list[dict[str, Any]]:
        return await self._repo.list_tasks_by_objective(objective_id)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._repo.get_task(task_id)


class InMemoryTaskLister:
    """Unbound-dispatch lister (unit tests; production always has a repo)."""

    def __init__(self, tasks: dict[str, dict[str, Any]] | None = None) -> None:
        self.tasks = tasks if tasks is not None else {}

    async def list_by_objective(self, objective_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.tasks.values()
                if row.get("objective_id") == objective_id]

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return dict(row) if row is not None else None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_objective_id() -> str:
    return f"OBJ-{uuid.uuid4().hex[:8].upper()}"


#: Converted tickets never execute (TASK-2026-00122): the shell carries the
#: children and superseded tickets decompose inside, so counting one as
#: ready wedged every objective past its first batch (live: OBJ-BD88AAFA).
TERMINAL_TICKET_STATUS = frozenset({"completed", "cancelled", "converted"})

#: Failed tickets are history, not work (TASK-2026-00122): the dispatcher
#: never launches them, so counting one as ready (``on_ticket_closed``) or
#: as an open gap (``verify_delivery``) wedged every objective with a failed
#: child in an enqueuer/plan loop (live: OBJ-BD88AAFA + bc913cf7). Failed
#: stays OUT of TERMINAL_TICKET_STATUS on purpose: the planner prompt keeps
#: showing failures (``open_tickets``) and the stall brake keeps tripping on
#: runs without a terminal close (``closed_before``).
FAILED_HISTORY_STATUS = "failed"

#: Origin marker of the parent ticket minted for a top-down objective.
SHELL_ORIGIN = "objective_shell"

#: Valid ``entry`` values on the objective document.
ENTRIES = frozenset({"convert", "top_down", "horizon_promotion", "ceo_review"})


# ---------------------------------------------------------------------------
# Creation (§9.2, F2): conversion + top-down share one engine
# ---------------------------------------------------------------------------

def normalize_goal(goal: str) -> str:
    """Canonical goal text for the deterministic duplicate check."""
    return re.sub(r"\s+", " ", str(goal or "")).strip().lower()


def find_duplicate(
    candidates: Sequence[Mapping[str, Any]],
    project: str,
    goal: str,
) -> Mapping[str, Any] | None:
    """Active objective with the same project + normalized goal, if any."""
    want = normalize_goal(goal)
    if not want:
        return None
    for candidate in candidates:
        if normalize_goal((candidate.get("anchor") or {}).get("goal", "")) == want:
            return candidate
    return None


def erp_group_name(ticket: Mapping[str, Any] | None) -> str:
    """Business ``TASK-…`` name from the ticket's ERPNext mirror ("" when none)."""
    if not isinstance(ticket, Mapping):
        return ""
    params = ticket.get("params")
    mirrors = params.get("_mirrors") if isinstance(params, Mapping) else None
    mirror = mirrors.get("erpnext") if isinstance(mirrors, Mapping) else None
    if not isinstance(mirror, Mapping):
        return ""
    return str(mirror.get("name") or "").strip()


def _new_objective_doc(
    *,
    objective_id: str,
    project: str,
    entry: str,
    anchor: dict[str, Any],
    source_task_id: str,
    cause: str,
    sizing_hint: str,
    erp_group_ref: str | None,
    base_sha: str,
    branch: str,
) -> dict[str, Any]:
    """Shared initial document for every entry (convert/top-down/promotion)."""
    return {
        "objective_id": objective_id,
        "project": project,
        "entry": entry,
        "erp_group_ref": erp_group_ref,
        "anchor": anchor,
        "status": "active",
        "capsule": {"text": "", "compiled_at": None, "tokens": 0},
        "roadmap_remaining": [],
        "tickets": [],
        "tickets_log": [],
        "tickets_files": {},
        "planning_runs": [],
        "material_failures": [],
        "planner_telemetry": {
            "cycles": 0,
            "replans": 0,
            "no_progress_cycles": 0,
            "tickets_created": 0,
            "cancelled_unnecessary": 0,
            "agent_reassignments": 0,
            "tier_reclassifications": 0,
            "tier_c": blank_tier_c_telemetry(),
        },
        "final_reviews": 0,
        "last_final_review_sha": None,
        "base_sha": base_sha,
        "branch": branch,
        "conversion_source_task_id": source_task_id,
        "conversion_cause": cause,
        "sizing_hint": sizing_hint[:4000],
    }


async def ensure_erp_group(
    env: ObjectiveEnv, objective: Mapping[str, Any],
) -> str | None:
    """Return the ERP group name, converting + recording it when missing.

    ``None`` means business identity is unavailable (no wiring, no source
    ticket, no mirror): the caller keeps the legacy group-less flow.
    Raises :class:`ObjectiveErpError` when the conversion itself fails —
    the caller must park, never plan orphan children.
    """
    ref = str(objective.get("erp_group_ref") or "").strip()
    if ref:
        return ref
    if env.erp is None:
        return None
    source_id = str(objective.get("conversion_source_task_id") or "").strip()
    if not source_id:
        return None
    try:
        source = await env.tasks.get_task(source_id)
    except Exception:
        return None
    name = erp_group_name(source)
    if not name:
        return None
    try:
        await env.erp.convert_to_group(name)
    except Exception as exc:
        raise ObjectiveErpError(
            f"cannot convert {name} to the objective group: {exc}") from exc
    await env.objectives.patch_objective(
        str(objective.get("objective_id")), {"erp_group_ref": name})
    return name


async def _check_admission(
    env: ObjectiveEnv, project: str, goal: str,
) -> None:
    """Deterministic admission: limits + duplicates (raises when refused)."""
    cfg = env.objectives_cfg
    max_global = int(cfg.get("max_active_objectives_global", 3))
    max_per_project = int(cfg.get("max_active_objectives_per_project", 2))
    siblings = await env.objectives.list_active_objectives(project)
    if len(siblings) >= max_per_project:
        raise ObjectiveLimitReached(
            f"project {project} already has {len(siblings)} active objectives "
            f"(max {max_per_project})")
    if project:
        everyone = await env.objectives.list_active_objectives()
        if len(everyone) >= max_global:
            raise ObjectiveLimitReached(
                f"already {len(everyone)} active objectives "
                f"(max {max_global})")
    dupe = find_duplicate(siblings, project, goal)
    if dupe is not None:
        raise ObjectiveDuplicate(
            f"active objective {dupe.get('objective_id')} already covers "
            f"this goal in {project}",
            existing_id=str(dupe.get("objective_id") or ""))


async def create_objective(
    env: ObjectiveEnv,
    *,
    project: str,
    goal: str,
    acceptance: Sequence[str] | None = None,
    entry: str = "top_down",
    source: str = "api",
    source_id: str | None = None,
    requester: str | None = None,
) -> dict[str, Any]:
    """Create a top-down objective: shell ticket (ERP group) + plan (§9.2, F2).

    One objective = one project: ``project`` is required and the caller
    (service layer) resolves it against the known projects. Admission is
    deterministic (active limits + duplicates); the shell ticket is the ERP
    group (``is_group`` through the common creation, ERP-first); the shell
    moves to ``converted`` at birth so it never executes. Raises
    :class:`ObjectiveRejected` when refused, :class:`ObjectiveErpError`
    when business identity fails. Returns the objective document.
    """
    project = str(project or "").strip()
    if not project:
        raise ObjectiveRejected("an objective needs exactly one project")
    goal = str(goal or "").strip()
    if not goal:
        raise ObjectiveRejected("an objective needs a goal")
    if entry not in ENTRIES:
        raise ObjectiveRejected(f"unknown entry {entry!r}")
    await _check_admission(env, project, goal)

    objective_id = new_objective_id()
    criteria = [str(item).strip() for item in (acceptance or [])
                if str(item).strip()]
    shell = await env.create_ticket({
        "title": f"[Objetivo] {goal[:120]}",
        "description": "\n".join([
            f"GOAL: {goal}",
            "",
            "ACCEPTANCE (delivery criteria):",
            *([f"- {item}" for item in criteria]
              or ["- (the planner derives verifiable criteria)"]),
            "",
            f"Entry: {entry}."
            + (f" Requested by {requester}." if requester else ""),
        ]),
        "acceptance": criteria,
        "project": project,
        "objective_id": objective_id,
        "origin": SHELL_ORIGIN,
        "autorun": False,
        "is_group": True,
        "source": source,
        "source_id": source_id or f"obj-shell:{objective_id}",
        "tags": ["objetivo"],
    })
    shell_id = str((shell or {}).get("task_id") or "")
    if not shell_id:
        raise ObjectiveErpError(
            "the shell ticket was not registered; refusing a group-less "
            "objective")
    await env.patch_task(shell_id, {
        "status": "converted",
        "waiting_reason": None,
        "resume_at": None,
        "last_response_summary": (
            f"Shell of objective {objective_id}: the group Task carries "
            "the children; this ticket never executes."),
    })
    group = erp_group_name(shell)
    anchor = {
        "goal": goal,
        "acceptance": criteria,
        "constraints": [],
        "out_of_scope": [],
        "source_doc": None,
    }
    if requester:
        anchor["constraints"] = [f"requested by {requester}"]
    objective = await env.objectives.create_objective(_new_objective_doc(
        objective_id=objective_id,
        project=project,
        entry=entry,
        anchor=anchor,
        source_task_id=shell_id,
        cause=entry,
        sizing_hint="",
        erp_group_ref=group or None,
        base_sha=_git(env.repo, "rev-parse", "HEAD").strip(),
        branch="base" if env.auto_merge else f"objective/{objective_id}",
    ))
    if env.notify is not None:
        await env.notify(
            shell_id,
            f"Objective {objective_id} created ({entry}) in {project}. "
            f"Anchor goal: {goal[:200]}")
    await plan_objective(env, objective_id)
    final = await env.objectives.get_objective(objective_id)
    return final or objective


async def convert_ticket(
    env: ObjectiveEnv,
    ticket: Mapping[str, Any],
    cause: str,
    sizing_text: str = "",
) -> dict[str, Any]:
    """Turn an oversized ticket into an objective (§9.2).

    The ticket text becomes the immutable anchor; the ticket itself moves to
    ``converted`` (its children carry the work) — never ``completed``: the
    parent goes Completed in ERP only at objective close (§9.7). The parent
    ticket becomes the ERP group (no new record); planned children link
    under it. A ticket that already belongs to an active objective
    decomposes inside it instead of minting a second objective. When the
    group conversion fails the objective parks as ``erp_error`` — orphans
    are never planned. The next trigger retries the conversion. Returns the
    objective document.
    """
    task_id = str(ticket.get("task_id", ""))
    own_id = str(ticket.get("objective_id") or "").strip()
    if own_id:
        own = await env.objectives.get_objective(own_id)
        if own is not None and own.get("status") == "active":
            await env.patch_task(task_id, {
                "status": "converted",
                "waiting_reason": None,
                "resume_at": None,
                "last_response_summary": (
                    f"Decomposing inside objective {own_id} ({cause}). "
                    "The objective planner owns the follow-up tickets."),
            })
            if sizing_text.strip():
                await env.objectives.patch_objective(
                    own_id, {"sizing_hint": sizing_text[:4000]})
            if env.notify is not None:
                await env.notify(
                    str(ticket.get("ref") or task_id[:8]),
                    f"Ticket decomposes inside objective {own_id} ({cause}).")
            await plan_objective(env, own_id)
            final = await env.objectives.get_objective(own_id)
            return final or own
    objective_id = new_objective_id()
    project = str(ticket.get("project") or "")
    acceptance = ticket.get("acceptance") or []
    if isinstance(acceptance, str):
        acceptance = [acceptance]
    anchor = {
        "goal": f"{ticket.get('title', '')}\n{ticket.get('description', '')}".strip(),
        "acceptance": [str(item) for item in acceptance],
        "constraints": [],
        "out_of_scope": [],
        "source_doc": None,
    }
    objective = await env.objectives.create_objective(_new_objective_doc(
        objective_id=objective_id,
        project=project,
        entry="convert",
        anchor=anchor,
        source_task_id=task_id,
        cause=cause,
        sizing_hint=sizing_text,
        erp_group_ref=None,
        base_sha=_git(env.repo, "rev-parse", "HEAD").strip(),
        branch="base" if env.auto_merge else f"objective/{objective_id}",
    ))
    await env.patch_task(task_id, {
        "status": "converted",
        "objective_id": objective_id,
        "waiting_reason": None,
        "resume_at": None,
        "last_response_summary": (
            f"Converted to objective {objective_id} ({cause}). "
            "The objective planner owns the follow-up tickets."),
    })
    if env.notify is not None:
        await env.notify(
            str(ticket.get("ref") or task_id[:8]),
            f"Ticket converted to objective {objective_id} ({cause}). "
            f"Anchor goal: {str(ticket.get('title', ''))[:200]}")
    try:
        await ensure_erp_group(env, objective)
    except ObjectiveErpError as exc:
        await _objective_park(env, objective, "erp_error", str(exc)[:300])
        final = await env.objectives.get_objective(objective_id)
        return final or objective
    await plan_objective(env, objective_id)
    final = await env.objectives.get_objective(objective_id)
    return final or objective


# ---------------------------------------------------------------------------
# Objective planner (§9.3): Muse headless → PlannerResult JSON
# ---------------------------------------------------------------------------

#: Planner/closer result file names (TASK-2026-00176). Each lives under
#: the objective's own scratch dir
#: (``.tier-s/objectives/<id>/<name>``), never under the scratch root:
#: concurrent cycles of one repo must not share an output file. The
#: legacy global files are neither written nor read anymore.
PLANNER_RESULT_FILE = "PLANNER_RESULT.json"

#: Closer verdicts file (§16.8), per-objective like the planner result.
CLOSURE_FILE = "OBJECTIVE_CLOSURE.md"

#: Orchestrator-wide scratch dir inside the project repo (TASK-2026-00136).
#: Shared with the Tier S executor files (PLAN, reviews, HANDOFF) and the
#: common ``status`` signal bus: the objectives engine keeps
#: ``objectives/<id>/``, ``PLANNER_RESULT.json`` and
#: ``OBJECTIVE_CLOSURE.md`` under this same root. Deliberately a local
#: constant, not imported from ``tier_s``: the root is shared, the engines
#: are not (decoupling: TASK-2026-00141).
SCRATCH_DIRNAME = ".tier-s"


def scratch_root(repo: Path) -> Path:
    """Canonical objectives scratch root: ``repo/.tier-s`` (see above)."""
    return repo / SCRATCH_DIRNAME


def objective_scratch(repo: Path, objective_id: str) -> Path:
    """Per-objective scratch dir: ``repo/.tier-s/objectives/<id>``."""
    return scratch_root(repo) / "objectives" / objective_id


def _agents_section(agents: Mapping[str, Any]) -> str:
    """Authoritative registry rendered for the planner prompt."""
    lines = ["REGISTERED AGENTS (select target_agent ONLY from enabled):"]
    for name in sorted(agents):
        spec = agents.get(name)
        if not isinstance(spec, Mapping):
            continue
        if spec.get("enabled", False):
            lines.append(f"- {name}: enabled")
        else:
            reason = str(spec.get("reason") or "disabled").strip()
            lines.append(f"- {name}: DISABLED ({reason}) — never route here")
    lines.append("Never invent an agent. If no enabled agent can do required "
                 "work, return capability_gap with no tickets.")
    return "\n".join(lines)


def _planner_prompt(
    objective: Mapping[str, Any],
    capsule_text: str,
    open_tickets: Sequence[Mapping[str, Any]],
    inbox_md: str,
    deferred: Sequence[str],
    sizing_hint: str = "",
    blocked_tickets: Sequence[Mapping[str, Any]] = (),
    agents_md: str = "",
    policy_text: str = "",
    handoffs: Sequence[str] = (),
    roadmap: Sequence[str] = (),
    repo: Path | str | None = None,
    routing_guide: str = "",
) -> str:
    """Planner prompt: bounded state + JSON contract (self-contained).

    The prompt carries the full ``PlannerResult`` shape inline: the planner
    runs in the project repo and cannot rely on reading OpenCEO sources.
    """
    criteria = [f"- {item}" for item in (objective.get("anchor") or {}).get(
        "acceptance", [])] or ["- (derive verifiable criteria)"]

    def _ticket_line(item: Mapping[str, Any]) -> str:
        line = (f"- {item.get('task_id')}: {item.get('title')} "
                f"[{item.get('coding_tool')}] ({item.get('status')})")
        blockers = item.get("blocked_by") or []
        if blockers:
            line += f" blocked_by={list(blockers)}"
        return line

    def _failure_line(item: Any) -> str:
        if not isinstance(item, Mapping):
            return "- (unreadable failure entry)"
        return (f"- {item.get('title')} [{item.get('tier')}]: "
                f"{str(item.get('cause') or '')[:300]}")

    opened = [_ticket_line(item) for item in open_tickets] or ["- (none)"]
    blocked = [_ticket_line(item) for item in blocked_tickets] or ["- (none)"]
    failures = [_failure_line(item)
                for item in list(objective.get("material_failures") or [])[-3:]]
    lines = [
        "You are the global OpenCEO Objective Planner and Agent Router.",
        "This is one fresh headless planning cycle: reason from the durable",
        "state below, not from memory. Do not explore the full codebase;",
        "read a bounded number of files only when a decision materially",
        "depends on them.",
        f"Objective {objective.get('objective_id')}:",
        f"GOAL: {(objective.get('anchor') or {}).get('goal', '')}",
        "ACCEPTANCE:",
        *criteria,
        "",
        "CAPSULE (compiled state):",
        capsule_text or "(empty: first planning)",
        "",
        "OPEN TICKETS:",
        *opened,
        "",
        "BLOCKED TICKETS:",
        *blocked,
        "",
        "RECENT HANDOFFS (latest first):",
        *([f"- {item}" for item in handoffs] or ["- (none)"]),
        "",
        "ROADMAP_REMAINING:",
        *([f"- {item}" for item in roadmap] or ["- (none)"]),
        "",
        "MATERIAL FAILURES (latest last; never retry one unchanged "
        "in the same tier):",
        *(failures or ["- (none)"]),
        "",
        "INBOX (pre-existing findings touching objective files, "
        "claim via includes_findings):",
        inbox_md,
    ]
    if deferred:
        lines.extend(["", "DEFERRED (reviewer suggestions out of scope):",
                      *[f"- {item}" for item in deferred]])
    if sizing_hint:
        lines.extend(["", "SIZING HINT from the converted ticket:",
                      sizing_hint[:3000]])
    if agents_md:
        lines.extend(["", agents_md])
    lines.extend([
        "",
        "ROUTING (summary; the normative policy below rules on conflicts):",
        "- Choose target_agent FIRST, only from enabled registered agents.",
        "- DEV tiers apply only when target_agent == dev, in this order.",
        "  All four executors are top-tier models; letters order the work,",
        "  never the models. Never describe a tier as cheap or weaker.",
        "  1. tier_s: orchestrating several independent model families",
        "     (independent plan, challenge, build, multi-family review,",
        "     arbitration) is expected to give a materially more correct",
        "     result than one session. The criterion is that expected",
        "     value, in ANY domain (money, security, data, algorithms,",
        "     contracts, ambiguous specs...), never a domain list; also",
        "     when the owner or the ticket asks for maximum assurance.",
        "     Not rationed by percentage. Justify with",
        "     why_consensus_is_needed + why_single_session_is_insufficient,",
        "     naming the residual failure other stages would catch (or",
        "     citing the explicit request).",
        "  2. tier_a: discovering truth/root cause/architecture is the",
        "     dominant difficulty. Justify with why_not_c: what breaks",
        "     if the default tier executes it.",
        "  3. tier_b: really complex single-session work (needs",
        "     significant interpretation, or a significant blast radius",
        "     below S stakes; weak verification only aggravates those).",
        "     Justify with why_not_c. Never select B for a missing",
        "     oracle or weak verification alone.",
        "  4. tier_c: THE DEFAULT. Specified work with no S/A/B",
        "     justification. No oracle, reversibility proof, or",
        "     blast-radius analysis required.",
        "- Declared oracle is evidence, not protection: only qualifying",
        "  types declared before the build freeze on any simple tier",
        "  (preexisting_tests, contract_or_schema, bug_reproduction,",
        "  separate_ticket_tests, planner_executable_spec). builder_tests",
        "  NEVER triggers protection. Never declare as oracle a file the",
        "  ticket is expected to modify.",
        "- Quota never redefines the technical tier. Never choose",
        "  runtime, CLI account, or model.",
    ])
    if (routing_guide or "").strip():
        lines.extend(["", routing_guide.strip()])
    lines.extend([
        "",
        f"OUTPUT: write {SCRATCH_DIRNAME}/objectives/"
        f"{objective.get('objective_id')}/{PLANNER_RESULT_FILE} "
        "with exactly this JSON",
        "object (no YAML, no markdown fences around the file content):",
        "{",
        '  "objective_status": "active | complete_candidate | blocked | '
        'capability_gap",',
        '  "state_summary": "<what is resolved and what is next>",',
        '  "information_gaps": [{"gap": "...", "materiality": '
        '"low|medium|high|critical", "resolution": "..."}],',
        '  "tickets": [ <1-3, ONLY when active> {',
        '    "local_ref": "<unique short key>", "title": "...",',
        '    "target_agent": "<enabled agent>", "work_type": '
        '"<e.g. implementation|debugging|research|documentation>",',
        '    "goal": "...", "why_now": "...",',
        '    "inputs": [], "constraints": [], "out_of_scope": [],',
        '    "acceptance_criteria": ["<at least one>"],',
        '    "expected_artifacts": ["<at least one>"],',
        '    "blocked_by": ["<local_ref or existing task_id, acyclic>"],',
        '    "coding_tool": "<dev only: tier_s|tier_a|tier_b|tier_c>",',
        '    "tier_reason": "<dev only: why this tier>",',
        '    "oracle": {"type": "<oracle type>", "refs": [{"ref": "...",',
        '      "description?": "..."}]},',
        '    "why_consensus_is_needed?": "<tier_s only>",',
        '    "why_single_session_is_insufficient?": "<tier_s only>",',
        '    "why_not_c?": "<required for tier_a/tier_b: what breaks in C; '
        'omit for tier_s and tier_c>",',
        '    "includes_findings?": ["<inbox fingerprints claimed>"]',
        "  } ],",
        '  "roadmap_remaining": ["<speculative future work>"],',
        '  "replan_when": ["<conditions that should trigger replanning>"]',
        "}",
        "Rules: active carries 1-3 tickets; complete_candidate, blocked and",
        "capability_gap carry NO tickets. complete_candidate triggers final",
        "review, it never closes the objective directly. Every ticket has",
        "one responsibility and fits one bounded execution cycle. No",
        "duplicate of an open ticket. Then emit PLANNER_READY in",
        f"{SCRATCH_DIRNAME}/status.",
    ])
    if policy_text.strip():
        lines.extend(["", "NORMATIVE ROUTING POLICY (authoritative):",
                      policy_text.strip()[:60000]])
    return knowledge_mod.with_preamble("\n".join(lines), repo, "en")


async def _task_exists(env: ObjectiveEnv, task_id: str) -> bool:
    try:
        return await env.tasks.get_task(task_id) is not None
    except Exception:
        return False


def _trailing_stalled_runs(runs: Sequence[Mapping[str, Any]]) -> int:
    """Trailing runs that planned nothing without completing (§9.6).

    Only ``active`` runs count: ``blocked``/``capability_gap`` park the
    objective explicitly instead of stalling silently. Entries without a
    status predate the single planner and keep the legacy reading.
    """
    stalled = 0
    for run in reversed(runs):
        status = run.get("status")
        if status is not None and status != "active":
            break
        if run.get("tickets_created", 0) == 0 and not run.get("complete"):
            stalled += 1
        else:
            break
    return stalled


def _planner_retry_prompt(
    errors: Sequence[str],
    repo: Path | str | None,
) -> str:
    return knowledge_mod.with_preamble(
        f"Your {PLANNER_RESULT_FILE} is invalid: {'; '.join(errors)}\n"
        "Fix the file in place and emit PLANNER_READY again.",
        repo, "en")


def _telemetry_after_run(
    objective: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Planner telemetry: cycles so far (ADR: measured from day one).

    ``cancelled_unnecessary``/``agent_reassignments``/``tier_reclassifications``
    start at zero: their signals arrive with TASK-2026-00119. The ``tier_c``
    block is preserved (TASK-2026-00116 feeds it event by event).
    """
    tele = dict(objective.get("planner_telemetry") or {})
    tele.setdefault("tier_mix", {})
    tele["cycles"] = len(runs)
    tele["replans"] = max(0, len(runs) - 1)
    tele["no_progress_cycles"] = _trailing_stalled_runs(runs)
    tele.setdefault("tickets_created", 0)
    tele.setdefault("cancelled_unnecessary", 0)
    tele.setdefault("agent_reassignments", 0)
    tele.setdefault("tier_reclassifications", 0)
    tele.setdefault("tier_c", blank_tier_c_telemetry())
    return tele


def blank_tier_c_telemetry() -> dict[str, Any]:
    """Empty Tier C telemetry block (routing policy §32)."""
    return {
        "started": 0,
        "closed": 0,
        "closed_attributable": 0,
        "cycle_failures": 0,
        "material_replans": 0,
        "late_failures": 0,
        "late_failure_severity": {},
        "by_oracle_type": {},
        "by_link": {},
        "c_ticket_records": [],
        "late_failure_records": [],
    }


async def record_material_failure(
    objectives: ObjectiveStore,
    objective_id: str,
    *,
    task_id: str = "",
    title: str = "",
    tier: str = "",
    cause: str = "",
) -> dict[str, Any]:
    """Record a material simple-tier failure (policy §29).

    Appends ``{title, tier, cause, at}`` to ``material_failures``: that
    list feeds the same-tier-retry rule in
    :func:`planner_models.validate_planner_result`, so the planner can
    never reassign the unchanged ticket to the same tier. A failed C
    ticket also bumps the Tier C ``material_replans`` counter. Returns
    the entry ({} when the objective does not exist).
    """
    objective = await objectives.get_objective(objective_id)
    if objective is None:
        return {}
    entry = {
        "title": str(title or "").strip()[:300],
        "tier": str(tier or ""),
        "cause": str(cause or "")[:500],
        "at": _utcnow_iso(),
    }
    failures = list(objective.get("material_failures") or [])
    failures.append(entry)
    await objectives.patch_objective(objective_id, {"material_failures": failures})
    if entry["tier"] == "tier_c":
        await record_tier_c(objectives, objective_id, "material_replan",
                            task_id=task_id)
    return entry


async def record_tier_c(
    objectives: ObjectiveStore,
    objective_id: str,
    event: str,
    *,
    task_id: str = "",
    oracle_type: str | None = None,
    tier_reason: str = "",
    work_type: str = "",
    project: str = "",
    severity: str = "",
    detail: str = "",
    attributable: bool = False,
    executor_link: str = "",
) -> None:
    """Append one Tier C telemetry event (routing policy §32).

    Events: ``started`` (C dispatch), ``executor`` (the chain link that
    ran the ticket: muse/dsh/claudep, stamped on its open record and
    counted in ``by_link`` -- the chain is availability, not selection,
    so the link is what lets the owner contrast the primary link with
    the fallbacks), ``closed`` (C close; ``attributable`` when the close
    recorded a SHA in ``tickets_log``), ``cycle_failure`` (one failed
    gate/oracle round during the candidate cycle), ``material_replan``
    (a C ticket failed materially), ``late_failure`` (a defect later
    attributed to a closed C ticket; only attributed origins are
    counted, never guesses).
    Unknown objectives are ignored: telemetry never fails execution.
    """
    if event not in ("started", "executor", "closed", "cycle_failure",
                     "material_replan", "late_failure"):
        return
    objective = await objectives.get_objective(objective_id)
    if objective is None:
        return
    tele = dict(objective.get("planner_telemetry") or {})
    tier_c = dict(tele.get("tier_c") or blank_tier_c_telemetry())
    oracle_key = str(oracle_type or "none")
    by_type = dict(tier_c.get("by_oracle_type") or {})
    slot = dict(by_type.get(oracle_key) or {"started": 0, "closed": 0})
    records = list(tier_c.get("c_ticket_records") or [])
    if event == "started":
        tier_c["started"] = int(tier_c.get("started", 0)) + 1
        slot["started"] = int(slot.get("started", 0)) + 1
        records.append({
            "task_id": str(task_id or ""),
            "oracle_type": oracle_key,
            "tier_reason": str(tier_reason or "")[:500],
            "work_type": str(work_type or "")[:200],
            "project": str(project or "")[:200],
            "outcome": "open",
            "started_at": _utcnow_iso(),
        })
    elif event == "executor":
        link = str(executor_link or "unknown")[:40]
        by_link = dict(tier_c.get("by_link") or {})
        by_link[link] = int(by_link.get(link, 0)) + 1
        tier_c["by_link"] = by_link
        _stamp_tier_c_record(records, str(task_id or ""), executor_link=link)
    elif event == "closed":
        tier_c["closed"] = int(tier_c.get("closed", 0)) + 1
        slot["closed"] = int(slot.get("closed", 0)) + 1
        if attributable:
            tier_c["closed_attributable"] = int(
                tier_c.get("closed_attributable", 0)) + 1
        _close_tier_c_record(records, str(task_id or ""), "closed")
    elif event == "cycle_failure":
        tier_c["cycle_failures"] = int(tier_c.get("cycle_failures", 0)) + 1
    elif event == "material_replan":
        tier_c["material_replans"] = int(
            tier_c.get("material_replans", 0)) + 1
        _close_tier_c_record(records, str(task_id or ""), "failed")
    elif event == "late_failure":
        tier_c["late_failures"] = int(tier_c.get("late_failures", 0)) + 1
        severities = dict(tier_c.get("late_failure_severity") or {})
        key = str(severity or "unknown")
        severities[key] = int(severities.get(key, 0)) + 1
        tier_c["late_failure_severity"] = severities
        late = list(tier_c.get("late_failure_records") or [])
        late.append({
            "task_id": str(task_id or ""),
            "severity": key,
            "detail": str(detail or "")[:500],
            "at": _utcnow_iso(),
        })
        tier_c["late_failure_records"] = late
    if event in ("started", "closed"):
        by_type[oracle_key] = slot
        tier_c["by_oracle_type"] = by_type
    tier_c["c_ticket_records"] = records
    tele["tier_c"] = tier_c
    await objectives.patch_objective(objective_id, {"planner_telemetry": tele})


def _stamp_tier_c_record(
    records: list[dict[str, Any]], task_id: str, **fields: Any,
) -> None:
    """Set fields on the latest open record of a ticket (in place)."""
    if not task_id:
        return
    for record in reversed(records):
        if record.get("task_id") == task_id \
                and record.get("outcome") == "open":
            record.update(fields)
            return


def _close_tier_c_record(
    records: list[dict[str, Any]], task_id: str, outcome: str,
) -> None:
    """Mark the latest open record for a ticket (in place, newest first)."""
    if not task_id:
        return
    for record in reversed(records):
        if record.get("task_id") == task_id \
                and record.get("outcome") == "open":
            record["outcome"] = outcome
            record["closed_at"] = _utcnow_iso()
            return


def tier_c_late_failure_rate(tier_c: Mapping[str, Any] | None) -> float | None:
    """``late_failure_rate`` (§32): attributed C tickets proven wrong / attributable C closes.

    Unique originating tickets over attributable closes. None (unknown)
    when nothing attributable closed: a rate is never invented from
    unattributable history.
    """
    tele = tier_c or {}
    try:
        denominator = int(tele.get("closed_attributable") or 0)
    except (TypeError, ValueError):
        return None
    if denominator <= 0:
        return None
    offenders = {str(item.get("task_id") or "")
                 for item in tele.get("late_failure_records") or []
                 if isinstance(item, Mapping) and item.get("task_id")}
    offenders.discard("")
    return len(offenders) / denominator


@dataclass
class _PlanFlight:
    """One in-flight planning per objective (TASK-2026-00176)."""

    task: asyncio.Task
    rerun_requested: bool = False


#: Planning runs in flight by objective. Module-level (not on the env):
#: the service rebuilds the env per call, so per-env state would not
#: serialize anything. One entry per planning objective at most, removed
#: when its leader finishes; single process (instance rule), hence plain
#: asyncio tasks serialize all producers (API/MCP/CEO/convert/close).
_PLAN_FLIGHTS: dict[str, _PlanFlight] = {}


async def plan_objective(env: ObjectiveEnv, objective_id: str) -> dict[str, Any]:
    """Run the single global planner, coalescing concurrent triggers (§9.3).

    One logical planner with fresh runs: triggers for DIFFERENT
    objectives plan in parallel over disjoint per-objective files, while
    triggers for the SAME objective share one flight — latecomers mark
    ``rerun_requested`` and await the final result instead of launching
    a second planner, so no planning is lost or duplicated. A call made
    from inside the leader's own run (final-review recursion) runs
    inline. The leader completes even if its caller goes away (a
    detached plan beats a half-planned objective); its exception, if
    any, reaches every attached trigger like their own run failing.
    """
    flight = _PLAN_FLIGHTS.get(objective_id)
    if flight is not None and not flight.task.done():
        if flight.task is asyncio.current_task():
            return await _plan_objective_inner(env, objective_id)
        flight.rerun_requested = True
        return await flight.task

    rec: _PlanFlight | None = None

    async def _leader() -> dict[str, Any]:
        assert rec is not None  # assigned below before any await
        result = await _plan_objective_inner(env, objective_id)
        while _PLAN_FLIGHTS.get(objective_id) is rec \
                and rec.rerun_requested:
            rec.rerun_requested = False
            result = await _plan_objective_inner(env, objective_id)
        return result

    # Check-then-set without an await between: atomic, a single leader.
    task = asyncio.create_task(_leader())
    rec = _PlanFlight(task=task)
    _PLAN_FLIGHTS[objective_id] = rec
    try:
        return await task
    finally:
        if _PLAN_FLIGHTS.get(objective_id) is rec:
            del _PLAN_FLIGHTS[objective_id]


async def _plan_objective_inner(
    env: ObjectiveEnv, objective_id: str,
) -> dict[str, Any]:
    """Run the single global planner once (§9.3). Returns a result summary."""
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    cfg = env.objectives_cfg
    planner_cfg = env.planner_cfg
    tickets = await env.tasks.list_by_objective(objective_id)
    active = [item for item in tickets
              if str(item.get("status")) not in TERMINAL_TICKET_STATUS
              and str(item.get("status")) != "converted"]
    if len(tickets) >= int(cfg.get("max_tickets", 20)):
        # Cost brake, not an owner decision (TASK-2026-00118): no new
        # tickets are planned, but open work keeps running and the next
        # close re-triggers planning. With nothing open the objective
        # attempts completion instead of stalling.
        if active:
            return {"ok": False, "reason": "max_tickets_reached",
                    "detail": f"{len(tickets)} lifetime tickets "
                              f"(max {cfg.get('max_tickets', 20)}); "
                              f"{len(active)} still open"}
        return await run_final_review(env, objective_id)
    max_active = int(cfg.get("max_active_tickets_per_objective", 6))
    if len(active) >= max_active:
        # Routine admission deferral, not a park: the objective stays
        # active and the next close re-triggers planning. No owner ping.
        return {"ok": False, "reason": "objective-full",
                "detail": f"{len(active)} active tickets (max {max_active})"}
    try:
        await ensure_erp_group(env, objective)
    except ObjectiveErpError as exc:
        return await _objective_park(
            env, objective, "erp_error", str(exc)[:300])
    objective = await env.objectives.get_objective(objective_id) or objective
    runs = objective.get("planning_runs") or []
    stalled = _trailing_stalled_runs(runs)
    closed_before = sum(1 for item in tickets
                        if str(item.get("status")) in TERMINAL_TICKET_STATUS)
    if stalled >= int(cfg.get("max_planning_runs_without_progress", 2)) \
            and closed_before == int(objective.get("_closed_at_last_plan", 0) or 0):
        return await _objective_park(
            env, objective, "stalled",
            "planning without progress (tope §9.6)")
    agents = planner_cfg.get("agents", {}) or {}
    if not agents:
        return await _objective_park(
            env, objective, "config",
            "planner agent registry is empty "
            "(global objective_planner.agents)")
    policy_text = planner_mod.load_policy_text(planner_cfg.get("policy_path"))
    if not policy_text:
        return await _objective_park(
            env, objective, "config",
            "planner routing policy unreadable; refusing to plan blind")
    open_tickets = [item for item in tickets
                    if str(item.get("status")) not in TERMINAL_TICKET_STATUS]
    blocked: list[Mapping[str, Any]] = []
    ready: list[Mapping[str, Any]] = []
    for item in open_tickets:
        (blocked if await _blocked(env, item) else ready).append(item)
    files = sorted({name for paths in
                    (objective.get("tickets_files") or {}).values()
                    for name in paths})
    inbox_md = ""
    if env.inbox is not None and files:
        inbox_md = await inbox_mod.inbox_md_for_planner(
            env.inbox, files, int(cfg.get("inbox_items_for_planner", 15)))
    deferred = [str(item) for item in objective.get("deferred") or []]
    handoffs = [str(entry.get("handoff_excerpt") or "").strip()
                for entry in (objective.get("tickets_log") or [])[-3:]]
    handoffs = [item for item in reversed(handoffs) if item]
    prompt = _planner_prompt(
        objective, (objective.get("capsule") or {}).get("text", ""),
        ready, inbox_md or "INBOX\n(none)\n", deferred,
        str(objective.get("sizing_hint") or ""),
        blocked_tickets=blocked, agents_md=_agents_section(agents),
        policy_text=policy_text, handoffs=handoffs,
        roadmap=[str(item) for item in
                 objective.get("roadmap_remaining") or []],
        repo=env.repo,
        routing_guide=routing_guide_section(
            str(objective.get("project") or ""),
            env.project_routing))
    scratch = objective_scratch(env.repo, objective_id)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "OBJECTIVE_CAPSULE.md").write_text(
        (objective.get("capsule") or {}).get("text", "") or "(empty)\n",
        encoding="utf-8")
    (scratch / "INBOX.md").write_text(inbox_md or "INBOX\n(none)\n",
                                      encoding="utf-8")
    timeout_s = int(planner_cfg.get("timeout_minutes", 15)) * 60
    errors: list[str] = []
    result: planner_mod.PlannerResult | None = None
    # Exclusive per-objective output (TASK-2026-00176): this run reads
    # only its objective's file, so a concurrent cycle of the same repo
    # can neither feed us its plan nor eat ours. Same-objective runs
    # never overlap (plan_objective single-flight), hence dropping the
    # previous cycle's file here is safe.
    result_path = scratch / PLANNER_RESULT_FILE
    for _attempt in range(2):
        # Drop any previous cycle output: a run that writes nothing must
        # read as missing, never as the last cycle's valid file.
        try:
            result_path.unlink()
        except OSError:
            pass
        await env.executor.run("objective_planner", prompt, env.repo, timeout_s)
        text = _read(result_path)
        if not text:
            errors = [f"no {PLANNER_RESULT_FILE}"]
        else:
            try:
                candidate = planner_mod.parse_planner_result(text)
            except ValueError as exc:
                errors = [str(exc)]
            else:
                errors = await planner_mod.validate_planner_result(
                    candidate,
                    agents=agents,
                    open_titles=[str(item.get("title") or "")
                                 for item in open_tickets],
                    task_exists=lambda dep: _task_exists(env, dep),
                    material_failures=(objective.get("material_failures")
                                       or []),
                    horizon=planner_cfg.get("ticket_horizon"),
                )
                if not errors:
                    result = candidate
        if not errors:
            break
        prompt = _planner_retry_prompt(errors, env.repo)
    try:
        await env.executor.cleanup("objective_planner")
    except Exception:
        pass
    if errors or result is None:
        return await _objective_park(
            env, objective, "planner_error",
            f"invalid {PLANNER_RESULT_FILE} after retry: "
            f"{'; '.join(errors)}")
    status = result.objective_status.value
    runs.append({"at": _utcnow_iso(), "status": status,
                 "tickets_created": len(result.tickets),
                 "complete": status == "complete_candidate"})
    await env.objectives.patch_objective(objective_id, {
        "planning_runs": runs,
        "roadmap_remaining": list(result.roadmap_remaining),
        "_closed_at_last_plan": closed_before,
        "sizing_hint": "",
        "planner_telemetry": _telemetry_after_run(objective, runs),
    })
    if status == "complete_candidate":
        return await run_final_review(env, objective_id)
    if status == "blocked":
        return await _objective_park(
            env, objective, "blocked", result.state_summary[:300])
    if status == "capability_gap":
        await env.objectives.patch_objective(objective_id, {
            "capability_gap": {
                "declared_at": _utcnow_iso(),
                "summary": result.state_summary[:2000],
                "gaps": [gap.model_dump()
                         for gap in result.information_gaps],
            },
        })
        return await _objective_park(
            env, objective, "capability_gap", result.state_summary[:300])
    created = await _create_planned_tickets(env, objective, result)
    tele = dict((await env.objectives.get_objective(objective_id) or {}
                 ).get("planner_telemetry") or {})
    tele["tickets_created"] = int(tele.get("tickets_created", 0)) + len(created)
    mix = dict(tele.get("tier_mix") or {})
    for planned in result.tickets:
        if planned.coding_tool is not None:
            key = planned.coding_tool.value
            mix[key] = int(mix.get(key, 0)) + 1
    tele["tier_mix"] = mix
    await env.objectives.patch_objective(objective_id, {
        "tickets": [*(objective.get("tickets") or []),
                    *(item["task_id"] for item in created)],
        "planner_telemetry": tele,
    })
    return {"ok": True, "tickets_created": len(created),
            "task_ids": [item["task_id"] for item in created]}


async def _objective_park(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    reason: str,
    detail: str,
) -> dict[str, Any]:
    await env.objectives.patch_objective(
        str(objective.get("objective_id")),
        {"status": "waiting", "waiting_reason": f"{reason}: {detail}"})
    if env.notify is not None:
        await env.notify(
            _notify_ref(objective),
            f"Objective waiting ({reason}): {detail}")
    if env.ceo_wake is not None:
        try:
            env.ceo_wake(
                f"Objective {objective.get('objective_id')} parked "
                f"({reason}): {str(detail)[:160]}"
            )
        except Exception:
            pass
    return {"ok": False, "reason": reason, "detail": detail}


def _planned_description(ticket: planner_mod.PlannedTicket) -> str:
    """Self-contained executor description from a planned ticket."""
    sections = [f"GOAL: {ticket.goal}", "", f"WHY NOW: {ticket.why_now}"]
    if ticket.inputs:
        sections.extend(["", "INPUTS:",
                         *[f"- {item}" for item in ticket.inputs]])
    if ticket.constraints:
        sections.extend(["", "CONSTRAINTS:",
                         *[f"- {item}" for item in ticket.constraints]])
    if ticket.out_of_scope:
        sections.extend(["", "OUT OF SCOPE:",
                         *[f"- {item}" for item in ticket.out_of_scope]])
    sections.extend(["", "EXPECTED ARTIFACTS:",
                     *[f"- {item}" for item in ticket.expected_artifacts]])
    if ticket.oracle is not None:
        refs = ", ".join(ref.ref for ref in ticket.oracle.refs)
        sections.extend(["", f"ORACLE ({ticket.oracle.type.value}): {refs}"])
    if ticket.why_consensus_is_needed:
        sections.extend(
            ["", f"WHY CONSENSUS: {ticket.why_consensus_is_needed}"])
    if ticket.why_single_session_is_insufficient:
        sections.extend(
            ["", "WHY NOT SINGLE SESSION: "
             f"{ticket.why_single_session_is_insufficient}"])
    if ticket.why_not_c:
        sections.extend(
            ["", f"WHY NOT THE DEFAULT TIER (C): {ticket.why_not_c}"])
    return "\n".join(sections)


def _planned_params(ticket: planner_mod.PlannedTicket) -> dict[str, Any]:
    """Machine-readable planner fields carried on the ticket params."""
    params: dict[str, Any] = {
        "target_agent": ticket.target_agent,
        "work_type": ticket.work_type,
        # Delivery verification (F2): the final review checks every child
        # against its acceptance criteria and expected artefacts.
        "expected_artifacts": list(ticket.expected_artifacts),
    }
    if ticket.tier_reason:
        params["tier_reason"] = ticket.tier_reason
    if ticket.oracle is not None:
        params["oracle"] = {
            "type": ticket.oracle.type.value,
            "refs": [ref.model_dump(exclude_none=True)
                     for ref in ticket.oracle.refs],
        }
    if ticket.why_consensus_is_needed:
        params["why_consensus_is_needed"] = ticket.why_consensus_is_needed
    if ticket.why_single_session_is_insufficient:
        params["why_single_session_is_insufficient"] = (
            ticket.why_single_session_is_insufficient)
    if ticket.why_not_c:
        params["why_not_c"] = ticket.why_not_c
    return params


async def _create_planned_tickets(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    result: planner_mod.PlannerResult,
) -> list[dict[str, Any]]:
    """Create every validated ticket in topological order (§9.3)."""
    tickets = list(result.tickets)
    keys = [ticket.local_ref for ticket in tickets]
    order: list[str] = []
    permanent: set[str] = set()

    def visit(node: str) -> None:
        if node in permanent:
            return
        permanent.add(node)
        item = next(entry for entry in tickets
                    if entry.local_ref == node)
        for dep in item.blocked_by:
            if dep in keys:
                visit(dep)
        order.append(node)

    for key in keys:
        visit(key)
    by_key = {ticket.local_ref: ticket for ticket in tickets}
    created: list[dict[str, Any]] = []
    key_to_task: dict[str, str] = {}
    run_tag = uuid.uuid4().hex[:8]
    group = str(objective.get("erp_group_ref") or "").strip()
    for key in order:
        item = by_key[key]
        blocked = [key_to_task.get(dep, dep) for dep in item.blocked_by]
        payload = {
            "title": item.title,
            "description": _planned_description(item),
            "acceptance": list(item.acceptance_criteria),
            "coding_tool": (item.coding_tool.value
                            if item.coding_tool is not None else None),
            "agent_hint": item.target_agent,
            "params": _planned_params(item),
            "blocked_by": blocked,
            "objective_id": str(objective.get("objective_id")),
            "origin": "objective_planner",
            "project": str(objective.get("project") or ""),
            "source": "spillover",
            "source_id": f"obj:{objective.get('objective_id')}:{key}:{run_tag}",
            # Pilot hold: objectives_cfg["child_autorun"] false keeps planned
            # tickets queued for verification (default true = §9.3 behavior).
            "autorun": bool(env.objectives_cfg.get("child_autorun", True)),
        }
        if group:
            # Real ERP children (F2): no LLM involved in the linking.
            payload["parent_task"] = group
        row = await env.create_ticket(payload)
        task_id = str((row or {}).get("task_id") or "")
        if not task_id:
            continue
        key_to_task[key] = task_id
        created.append({"task_id": task_id, "key": key})
        if env.inbox is not None:
            for fp in item.includes_findings:
                try:
                    await inbox_mod.mark_assigned(env.inbox, str(fp), task_id)
                except Exception:
                    continue
    return created


# ---------------------------------------------------------------------------
# Triggers + capsule (§9.4, §5.3)
# ---------------------------------------------------------------------------

@dataclass
class TicketCloseContext:
    """What the cycle hands over when an objective ticket closes."""

    task_id: str
    title: str
    tier: str
    sha: str
    files: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)
    deviations: list[str] = field(default_factory=list)
    gotchas: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    handoff_excerpt: str = ""


async def on_ticket_closed(
    env: ObjectiveEnv,
    ticket: Mapping[str, Any],
    close: TicketCloseContext | None = None,
) -> dict[str, Any]:
    """Triggers + capsule recompile after an objective ticket closes (§9.4).

    Never raises: trigger failures are notified, never fatal to the close.
    """
    objective_id = str(ticket.get("objective_id") or "")
    if not objective_id:
        return {"ok": True, "action": "no-objective"}
    try:
        objective = await env.objectives.get_objective(objective_id)
        if objective is None or objective.get("status") != "active":
            return {"ok": True, "action": "objective-not-active"}
        if close is not None:
            await _absorb_close(env, objective, close)
            objective = await env.objectives.get_objective(objective_id) or objective
        await _recompile_capsule(env, objective)
        tickets = await env.tasks.list_by_objective(objective_id)
        # The notification asserts its subject is closed: never count it
        # as open work, whatever a racy store read says (TASK-2026-00169:
        # a pre-persist trigger — or the concurrent QA flip — reads the
        # subject back as non-terminal and wedges the objective).
        closed_ids = {str(ticket.get("task_id") or "")}
        if close is not None:
            closed_ids.add(str(close.task_id or ""))
        closed_ids.discard("")
        ready = [item for item in tickets
                 if str(item.get("status")) not in TERMINAL_TICKET_STATUS
                 and str(item.get("status")) != FAILED_HISTORY_STATUS
                 and str(item.get("task_id") or "") not in closed_ids
                 and not await _blocked(env, item)]
        if ready:
            if close is not None and _touches_objective(objective, close) \
                    and (close.deviations or close.gotchas):
                return await plan_objective(env, objective_id)
            return {"ok": True, "action": "enqueuer-continues",
                    "ready": len(ready)}
        return await plan_objective(env, objective_id)
    except Exception as exc:
        if env.notify is not None:
            await env.notify(objective_id, f"objective trigger failed: {exc}")
        return {"ok": False, "reason": str(exc)[:300]}


async def _blocked(env: ObjectiveEnv, ticket: Mapping[str, Any]) -> bool:
    for dep in ticket.get("blocked_by") or []:
        row = await env.tasks.get_task(str(dep))
        if str((row or {}).get("status") or "") not in TERMINAL_TICKET_STATUS:
            return True
    return False


def _touches_objective(
    objective: Mapping[str, Any], close: TicketCloseContext) -> bool:
    known = {name for paths in
             (objective.get("tickets_files") or {}).values()
             for name in paths}
    known.update(close.files)
    return bool(set(close.files) & known)


async def _absorb_close(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    close: TicketCloseContext,
) -> None:
    log = list(objective.get("tickets_log") or [])
    log.append({
        "task_id": close.task_id, "title": close.title, "tier": close.tier,
        "sha": close.sha, "files": list(close.files),
        "contracts": [str(entry)[:200] for entry in close.contracts[:10]],
        "decisions": [str(entry)[:200] for entry in close.decisions[:10]],
        "deviations": [str(entry)[:300] for entry in close.deviations[:5]],
        "gotchas": [str(entry)[:300] for entry in close.gotchas[:5]],
        "handoff_excerpt": str(close.handoff_excerpt or "")[:500],
    })
    files = dict(objective.get("tickets_files") or {})
    files[close.task_id] = list(close.files)
    await env.objectives.patch_objective(
        str(objective.get("objective_id")),
        {"tickets_log": log, "tickets_files": files})


async def _recompile_capsule(
    env: ObjectiveEnv, objective: Mapping[str, Any]) -> None:
    """Deterministic capsule compile (§5.3). No LLM writes this."""
    anchor = objective.get("anchor") or {}
    log = list(objective.get("tickets_log") or [])
    cap = int(env.objectives_cfg.get("capsule_tokens", 2000)) * 4  # ~chars
    lines = [
        f"GOAL: {anchor.get('goal', '')}",
        "ACCEPTANCE:",
        *[f"- {item}" for item in anchor.get("acceptance", [])],
        "COMPLETED:",
        *[f"- {entry.get('title')} [{entry.get('tier')}] "
          f"{str(entry.get('sha') or '')[:12]}" for entry in log],
        "CONTRACTS:",
        *[f"- {line}" for entry in log for line in entry.get("contracts", [])],
        "DECISIONS:",
        *[f"- {line}" for entry in log for line in entry.get("decisions", [])],
        "DEVIATIONS / GOTCHAS:",
        *[f"- {line}" for entry in log
          for line in [*entry.get("deviations", []), *entry.get("gotchas", [])]],
        "ROADMAP_REMAINING:",
        *[f"- {item}" for item in objective.get("roadmap_remaining", [])],
        "KEY FILES:",
        *[f"- {name}" for paths in
          (objective.get("tickets_files") or {}).values() for name in paths],
    ]
    text = "\n".join(lines) + "\n"
    if len(text) > cap and log:
        # Oldest tickets shrink to contracts only (§5.3).
        keep_contracts = [line for entry in log
                          for line in entry.get("contracts", [])]
        lines = [
            f"GOAL: {anchor.get('goal', '')}",
            "ACCEPTANCE:",
            *[f"- {item}" for item in anchor.get("acceptance", [])],
            "COMPLETED:",
            *[f"- {entry.get('title')} [{entry.get('tier')}]" for entry in log],
            "CONTRACTS:",
            *[f"- {line}" for line in keep_contracts],
            "ROADMAP_REMAINING:",
            *[f"- {item}" for item in objective.get("roadmap_remaining", [])],
        ]
        text = "\n".join(lines)[:cap] + "\n"
    await env.objectives.patch_objective(
        str(objective.get("objective_id")),
        {"capsule": {"text": text, "compiled_at": _utcnow_iso(),
                     "tokens": len(text) // 4}})


# ---------------------------------------------------------------------------
# Final review + close (§9.7, F2: review by domain)
# ---------------------------------------------------------------------------

_DEV_TIERS = frozenset({"tier_s", "tier_a", "tier_b", "tier_c"})


def _is_dev_ticket(ticket: Mapping[str, Any]) -> bool:
    """True when the ticket is DEV work (any tier or dev target)."""
    if str(ticket.get("coding_tool") or "") in _DEV_TIERS:
        return True
    params = ticket.get("params")
    if isinstance(params, Mapping) \
            and str(params.get("target_agent") or "") == "dev":
        return True
    return str(ticket.get("agent_hint") or "") == "dev"


def verify_delivery(
    objective: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Deterministic delivery check over the objective children.

    The parent/shell ticket and superseded (``converted``) tickets are not
    work and are excluded. ``open_gaps`` (work still running) send the
    objective back to planning; ``data_gaps`` (a settled terminal or
    failed child without acceptance criteria or expected artefacts)
    park it — no planner run can fix a closed ticket. Failed children
    are history (``failed_children``), never open gaps. No LLM is
    involved, ever.
    """
    source_id = str(objective.get("conversion_source_task_id") or "")
    work = [item for item in children
            if str(item.get("task_id") or "") != source_id
            and str(item.get("status") or "") != "converted"]
    open_gaps: list[str] = []
    data_gaps: list[str] = []
    failed: list[str] = []
    terminal = 0
    for item in work:
        short = str(item.get("task_id") or "")[:8]
        status = str(item.get("status") or "")
        if status == FAILED_HISTORY_STATUS:
            failed.append(short)
        elif status not in TERMINAL_TICKET_STATUS:
            open_gaps.append(f"{short}: still {status}")
            continue
        else:
            terminal += 1
        if not item.get("acceptance"):
            data_gaps.append(f"{short}: no acceptance criteria")
        params = item.get("params")
        artefacts = params.get("expected_artifacts") \
            if isinstance(params, Mapping) else None
        if not artefacts:
            data_gaps.append(f"{short}: no expected artefacts")
    dev = sum(1 for item in work if _is_dev_ticket(item))
    return {
        "ok": not open_gaps and not data_gaps,
        "open_gaps": open_gaps,
        "data_gaps": data_gaps,
        "failed_children": failed,
        "work_children": len(work),
        "terminal_children": terminal,
        "dev_children": dev,
    }


def _delivery_record(
    objective: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
    *,
    verified_by: str,
    review: str | None = None,
) -> dict[str, Any]:
    """Delivery verdict stored on close (criteria + counts + provenance).

    The objective closes on its DELIVERY criteria. The business outcome
    (KPI) is evaluated afterwards by the CEO (F4): ``outcome_kpi`` stays
    ``None`` here — closing every ticket never claims a metric moved.
    """
    verdict = verify_delivery(objective, children)
    return {
        "criteria": list(
            (objective.get("anchor") or {}).get("acceptance", [])),
        "verified_at": _utcnow_iso(),
        "verified_by": verified_by,
        "review": review
        or ("pool" if verdict["dev_children"] else "deterministic"),
        "work_children": verdict["work_children"],
        "terminal_children": verdict["terminal_children"],
        "failed_children": len(verdict["failed_children"]),
        "dev_children": verdict["dev_children"],
        "outcome_kpi": None,
    }

_CLOSURE_RE = re.compile(
    r"^F(\d+)\s*:\s*(REAL|FALSO)\b\s*(?:[—–-]\s*(.*))?$",
    re.IGNORECASE,
)


def parse_closure(text: str) -> dict[int, tuple[str, str]]:
    """Parse ``OBJECTIVE_CLOSURE.md`` (§16.8): id → (verdict, reason)."""
    verdicts: dict[int, tuple[str, str]] = {}
    for raw_line in (text or "").splitlines():
        match = _CLOSURE_RE.match(raw_line.strip())
        if match:
            verdicts[int(match.group(1))] = (
                match.group(2).upper(), (match.group(3) or "").strip())
    return verdicts


def _closer_no_verdict_detail(
    missing: Sequence[int],
    attempts: Sequence[Any],
    text: str,
    parsed: int,
) -> str:
    """Evidence for a ``closer_no_verdict`` park (TASK-2026-00170)."""
    runs: list[str] = []
    for result in attempts:
        tag = f"rc={result.returncode}"
        if result.returncode == TIMEOUT_RETURNCODE \
                or (result.usage or {}).get("timeout"):
            tag += "/timeout"
        runs.append(tag)
    names = ",".join(f"F{index}" for index in missing)
    if not (text or "").strip():
        state = "closure file empty"
    else:
        lines = len((text or "").splitlines())
        state = f"closure file has {lines} lines, {parsed} parsed verdicts"
    return (f"closer left {names} without a verdict after "
            f"{len(attempts)} runs ({','.join(runs)}); {state}")[:300]


async def run_final_review(env: ObjectiveEnv, objective_id: str) -> dict[str, Any]:
    """Final review by domain + close (§9.7).

    ``complete_candidate`` never closes directly: every path runs the
    deterministic delivery verification first (no tokens). Open work
    sends the objective back to planning; data gaps park it; clean
    non-dev work closes deterministically; clean dev work goes through
    the reviewer pool over the objective branch.
    """
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    reviews = int(objective.get("final_reviews") or 0)
    if reviews >= int(env.objectives_cfg.get("max_final_reviews", 2)):
        return await _objective_park(
            env, objective, "stalled", "max_final_reviews reached (§9.6)")
    try:
        await ensure_erp_group(env, objective)
    except ObjectiveErpError as exc:
        return await _objective_park(
            env, objective, "erp_error", str(exc)[:300])
    objective = await env.objectives.get_objective(objective_id) or objective
    children = await env.tasks.list_by_objective(objective_id)
    verdict = verify_delivery(objective, children)
    if verdict["data_gaps"]:
        return await _objective_park(
            env, objective, "data_gaps",
            f"delivery data gaps: {'; '.join(verdict['data_gaps'][:5])}")
    if verdict["open_gaps"]:
        await env.objectives.patch_objective(objective_id, {
            "status": "active", "final_reviews": reviews + 1})
        if env.notify is not None:
            await env.notify(
                _notify_ref(objective),
                f"Objective {objective_id} back to planning: "
                f"{'; '.join(verdict['open_gaps'][:5])}")
        return await plan_objective(env, objective_id)
    await env.objectives.patch_objective(
        objective_id, {"status": "final_review",
                       "final_reviews": reviews + 1})
    if not verdict["dev_children"]:
        # Non-dev: the deterministic verification above IS the review.
        objective = await env.objectives.get_objective(objective_id) \
            or objective
        return await _finish_objective(env, objective)
    anchor = objective.get("anchor") or {}
    capsule = (objective.get("capsule") or {}).get("text", "")
    since = objective.get("last_final_review_sha") or objective.get("base_sha")
    head = _integration_head(env, objective_id)
    diff_stat = _git(env.repo, "diff", "--stat", since or "HEAD", head)
    files = sorted({name for paths in
                    (objective.get("tickets_files") or {}).values()
                    for name in paths})
    request = (
        "OBJECTIVE FINAL REVIEW\n"
        f"Goal: {anchor.get('goal', '')}\n"
        "Anchor acceptance:\n"
        + "".join(f"- {item}\n" for item in anchor.get("acceptance", []))
        + "Capsule contracts:\n"
        + "\n".join(line for line in capsule.splitlines()
                    if line.startswith("- "))[:3000]
        + f"\nAggregate diff {str(since)[:12]}..{head[:12]} (stat):\n"
        + diff_stat[:4000]
        + "\nObjective files:\n"
        + "".join(f"- {name}\n" for name in files[:100])
        + "\nScope: anchor acceptance met? contracts between tickets fit? "
          "regressions across the set? Same report format as ticket rounds."
    )
    plan = await review_mod.select_round(
        env.pool, env.slots, 3, builder_family="muse")

    async def run_one(
        slot: review_mod.ReviewSlot,
        attempt: int,
        previous: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        return await _run_final_reviewer(
            env, slot, request, head, objective, attempt=attempt,
            previous=previous, review_no=reviews + 1)

    # Same round machinery as the ticket cycle (TASK-2026-00162): relief
    # slots plus one retry per slot before the objective parks.
    result = await review_mod.run_quorum_round(
        env.pool, env.slots, plan, run_one, builder_family="muse",
        quorum=int(env.review_cfg.get("quorum", 2)))
    if not result.met:
        return await _objective_park(
            env, objective, "no_quorum",
            f"final review without quorum: {result.summary() or 'empty pool'}")
    verdicts = result.verdicts
    consolidated = findings_mod.consolidate(verdicts)
    scratch = objective_scratch(env.repo, objective_id)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "OBJECTIVE_FINDINGS.md").write_text(
        findings_mod.render_findings(consolidated, 0, head,
                                     builder_family="muse"),
        encoding="utf-8")
    for item in verdicts:
        if item.destination == "inbox" and env.inbox is not None:
            await _inbox_final_finding(env, objective, item)
    closed = await _close_objective(env, objective, consolidated, head)
    if not closed.get("ok"):
        # The close parked (closer without verdicts): this head was not
        # reviewed, so the SHA must not advance — the next final review
        # re-derives the same findings (TASK-2026-00170).
        return closed
    await env.objectives.patch_objective(
        objective_id, {"last_final_review_sha": head})
    return closed


def _integration_head(env: ObjectiveEnv, objective_id: str) -> str:
    """Final review target: the objective's integration ref (TASK-2026-00162).

    Never the live checkout's HEAD: the service repo may sit on any branch
    (the F7 pilot reviewed unmerged scaffolding without the deliverable).
    Children merge into ``objective/<id>`` under ``auto_merge=false`` and
    into the base branch otherwise (§12).
    """
    refs = [] if env.auto_merge else [f"objective/{objective_id}"]
    refs.append(env.base_branch or "main")
    for ref in refs:
        sha = _git(env.repo, "rev-parse", "--verify", "--quiet",
                   f"{ref}^{{commit}}").strip()
        if sha:
            return sha
    return _git(env.repo, "rev-parse", "HEAD").strip()


async def _run_final_reviewer(
    env: ObjectiveEnv,
    slot: review_mod.ReviewSlot,
    request: str,
    head_sha: str,
    objective: Mapping[str, Any],
    attempt: int = 1,
    previous: Mapping[str, Any] | None = None,
    review_no: int = 0,
) -> dict[str, Any]:
    """One final-review attempt; every attempt lands in ``task_reviews``."""
    outcome = review_mod.new_outcome(slot, attempt)
    suffix = f"-a{attempt}" if attempt > 1 else ""
    try:
        worktree = await asyncio.to_thread(
            review_mod.create_worktree, env.repo, head_sha,
            f"final-{slot.label}{suffix}")
    except RuntimeError as exc:
        outcome.update(kind="worktree", reason=str(exc)[:200])
        await _persist_final_review(env, objective, outcome, review_no)
        return outcome
    try:
        (worktree / "OBJECTIVE_REVIEW_REQUEST.md").write_text(
            request, encoding="utf-8")
        before = await asyncio.to_thread(
            review_mod.worktree_snapshot, worktree)
        timeout_s = review_mod.slot_timeout_s(
            slot, env.review_cfg.get("reviewer_timeout_minutes", 15),
            previous)
        result = await env.executor.run(
            f"review:{slot.label}",
            review_mod.reviewer_prompt(
                slot.label, "OBJECTIVE_REVIEW_REQUEST.md",
                "objective_final", 0, repo=worktree,
                retry=review_mod.retry_note(previous)),
            worktree, timeout_s)
        outcome.update(elapsed_s=float(result.elapsed_s or 0.0),
                       exit_code=result.returncode)
        after = await asyncio.to_thread(
            review_mod.worktree_snapshot, worktree)
        if before and after and before != after:
            outcome.update(kind="write_attempt", reason="write_attempt")
            await _persist_final_review(env, objective, outcome, review_no)
            return outcome
        text = (result.output_tail or "").strip()
        if result.returncode != 0:
            if (result.returncode == TIMEOUT_RETURNCODE
                    or (result.usage or {}).get("timeout")):
                outcome.update(
                    kind="timeout",
                    reason=text[:200] or f"timeout after {timeout_s}s")
            else:
                action, patch = review_mod.classify_slot_error(text, slot)
                if action in {"cooling_down", "disabled"}:
                    await env.slots.set_slot(slot.label, patch)
                outcome.update(
                    kind=(action if action in {"cooling_down", "disabled"}
                          else "slot_error"),
                    reason=f"slot error ({action}): {text[:200]}")
            await _persist_final_review(env, objective, outcome, review_no)
            return outcome
        try:
            raw = findings_mod.parse_report(text)
        except findings_mod.ReportMalformed as exc:
            outcome.update(kind="malformed", reason=f"malformed: {exc}")
            await _persist_final_review(env, objective, outcome, review_no)
            return outcome
        base = str(objective.get("base_sha") or head_sha)
        acceptance = (objective.get("anchor") or {}).get("acceptance", [])
        verdicts = await asyncio.to_thread(
            findings_mod.verify_report, raw, env.repo, head_sha, base,
            slot.label, slot.family, acceptance)
        outcome.update(verdicts=verdicts, valid=True, kind="valid")
        await _persist_final_review(env, objective, outcome, review_no)
        return outcome
    finally:
        await asyncio.to_thread(
            review_mod.remove_worktree, env.repo, worktree)


async def _persist_final_review(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    outcome: Mapping[str, Any],
    review_no: int,
) -> None:
    """Append one final-review attempt to ``task_reviews``, valid or not.

    Invalid attempts used to vanish with ``no_quorum`` (TASK-2026-00162):
    the reason per slot (timeout/malformed/error) is now on record.
    """
    if env.reviews is None:
        return
    try:
        await env.reviews.create_review(findings_mod.review_document(
            review_id=f"rev-{uuid.uuid4().hex[:12]}",
            task_id=str(objective.get("conversion_source_task_id") or ""),
            run_id=f"final-{objective.get('objective_id')}",
            scope="objective_final", round_no=0,
            slot=str(outcome.get("slot") or ""),
            family=str(outcome.get("family") or ""),
            valid=bool(outcome.get("valid")),
            verdicts=list(outcome.get("verdicts") or []),
            invalid_reason=str(outcome.get("reason") or ""),
            meta={"attempt": int(outcome.get("attempt") or 1),
                  "kind": str(outcome.get("kind") or ""),
                  "elapsed_s": float(outcome.get("elapsed_s") or 0.0),
                  "exit_code": outcome.get("exit_code"),
                  "final_review": review_no,
                  "project": objective.get("project")}))
    except Exception:
        pass  # telemetry persistence never fails a review


async def _inbox_final_finding(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    item: Any,
) -> None:
    anchor = item.raw.anchors[0] if item.raw.anchors else None
    quote = item.raw.quotes[0] if item.raw.quotes else ""
    try:
        await inbox_mod.record_appearance(
            env.inbox, str(objective.get("project") or ""),
            inbox_mod.Appearance(
                file=anchor.path if anchor and anchor.path else "",
                category=item.raw.category, severity=item.raw.severity,
                quote=quote, strength=item.strength, source="pre_existing",
                family=item.family,
                task_id=str(objective.get("conversion_source_task_id") or ""),
                run_id=f"final-{objective.get('objective_id')}", round_no=0,
                anchor=findings_mod._anchor_text(anchor) if anchor else ""))
    except Exception:
        pass


def _closure_judge_prompt(
    count: int, goal: str, capsule: str, repo: Path,
    findings_path: str = "OBJECTIVE_FINDINGS.md",
    *,
    result_path: str,
) -> str:
    # Exact paths (TASK-2026-00162): the scratch holds one findings file
    # per objective, and a bare name let the closer read the wrong one.
    # The verdicts file is per-objective too (TASK-2026-00176): concurrent
    # closes of one repo must not share an output file.
    return knowledge_mod.with_preamble(
        f"Judge the objective final findings in {findings_path} "
        f"({count} findings, full text: problem, fix, quotes). They are "
        "verified: they point at real code.\n"
        f"Anchor goal: {goal[:500]}\n"
        f"Capsule contracts:\n{capsule[:2000]}\n"
        f"Write {result_path} with one line per finding: "
        "`F<n>: REAL — <why>` or `F<n>: FALSO — <verifiable technical "
        f"reason>`. Then emit CLOSURE_READY in {SCRATCH_DIRNAME}/status.",
        repo, "en")


def _closure_retry_prompt(
    missing: Sequence[int], repo: Path, *, result_path: str,
) -> str:
    return knowledge_mod.with_preamble(
        f"{result_path} is incomplete: judge each of "
        f"{['F' + str(index) for index in missing]} exactly once "
        "(REAL/FALSO), then emit CLOSURE_READY.", repo, "en")


async def _close_objective(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    consolidated: Sequence[Any],
    head_sha: str,
) -> dict[str, Any]:
    """``muse`` judges the final findings; REAL ones become tickets (§9.7)."""
    objective_id = str(objective.get("objective_id"))
    anchor = objective.get("anchor") or {}
    capsule = (objective.get("capsule") or {}).get("text", "")
    timeout_s = int(env.timeouts.get("closer", 15)) * 60
    if not consolidated:
        return await _finish_objective(env, objective)
    scratch = objective_scratch(env.repo, objective_id)
    findings_path = (scratch / "OBJECTIVE_FINDINGS.md").relative_to(env.repo)
    result_path = scratch / CLOSURE_FILE
    prompt = _closure_judge_prompt(
        len(consolidated), str(anchor.get("goal", "")), capsule, env.repo,
        findings_path=str(findings_path),
        result_path=str(result_path.relative_to(env.repo)))
    # Drop any previous closure: a run that writes nothing must read as
    # missing, never as another cycle's verdicts (same rule as the
    # planner result; per-objective: concurrent closes of one repo hold
    # disjoint files, same-objective closes never overlap).
    try:
        result_path.unlink()
    except OSError:
        pass
    first = await env.executor.run("closer", prompt, env.repo, timeout_s)
    try:
        await env.executor.cleanup("closer")
    except Exception:
        pass
    text = _read(result_path)
    verdicts = parse_closure(text)
    missing = [item.id for item in consolidated if item.id not in verdicts]
    attempts = [first]
    if missing:
        attempts.append(await env.executor.run(
            "closer",
            _closure_retry_prompt(
                missing, env.repo,
                result_path=str(result_path.relative_to(env.repo))),
            env.repo, timeout_s))
        try:
            await env.executor.cleanup("closer")
        except Exception:
            pass
        text = _read(result_path)
        verdicts = parse_closure(text)
        missing = [item.id for item in consolidated if item.id not in verdicts]
    if missing:
        # No verdict is never FALSO (TASK-2026-00170): a silent, failed
        # or malformed closer parks the objective with the attempts as
        # evidence. The findings stay unconsumed for the next review.
        return await _objective_park(
            env, objective, "closer_no_verdict",
            _closer_no_verdict_detail(missing, attempts, text,
                                       len(verdicts)))
    real: list[Any] = []
    for item in consolidated:
        verdict, reason = verdicts[item.id]
        if verdict != "REAL":
            if item.severity == "CRITICAL" and len(item.families) >= 2:
                if await _arbitrate_final(env, objective, item, reason):
                    real.append(item)
            continue
        real.append(item)
    if not real:
        return await _finish_objective(env, objective)
    created: list[str] = []
    for item in real:
        payload = _closure_fix_ticket(env, objective, item)
        row = await env.create_ticket(payload)
        task_id = str((row or {}).get("task_id") or "")
        if task_id:
            created.append(task_id)
    await env.objectives.patch_objective(objective_id, {
        "status": "active",
        "tickets": [*(objective.get("tickets") or []), *created],
    })
    return {"ok": True, "action": "fix-tickets", "task_ids": created}


async def _arbitrate_final(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    item: Any,
    muse_reason: str,
) -> bool:
    """Arbitrate a FALSO on a ≥2-family CRITICAL. True = treat as REAL."""
    arbiter = env.roles.get("arbiter", {}) or {}
    account = str(arbiter.get("account") or "")
    try:
        verdicts = await env.quota.check([account] if account else [])
        quota_ok = all(getattr(verdict, "ok", True)
                       for verdict in verdicts.values())
    except Exception:
        quota_ok = False
    if not arbiter or not account or not quota_ok:
        return True  # no arbitration possible: reproduce it (§9.7)
    timeout_s = int(env.timeouts.get("arbiter", 10)) * 60
    quote = "\n".join(item.quotes[:2])
    result = await env.executor.run(
        "arbiter", review_mod.arbiter_prompt(item, muse_reason, quote,
                                             repo=env.repo),
        env.repo, timeout_s)
    try:
        await env.executor.cleanup("arbiter")
    except Exception:
        pass
    for line in (result.output_tail or "").splitlines():
        upper = line.strip().upper()
        if upper.startswith("REAL"):
            return True
        if upper.startswith("FALSO"):
            return False
    return True  # unparseable: reproduce rather than drop a CRITICAL


def _closure_fix_ticket(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    item: Any,
) -> dict[str, Any]:
    tiers = dict(env.fix_tiers or inbox_mod.FIX_TICKET_TIERS_DEFAULTS)
    category = str(item.category or "").lower()
    if category == "security":
        tier = tiers.get("security", "tier_s")
    elif item.severity == "CRITICAL":
        tier = tiers.get("critical", "tier_a")
    else:
        tier = tiers.get("default", "tier_c")
    anchors = " + ".join(findings_mod._anchor_text(a) for a in item.anchors)
    priority = "high" if (item.severity == "CRITICAL" or category == "security") else "medium"
    payload: dict[str, Any] = {
        "priority": priority,
        "title": f"[objective-final] {item.severity} {category}: "
                 f"{item.problem[:100]}",
        "description": (
            f"Hallazgo REAL del cierre de {objective.get('objective_id')}.\n"
            f"Ubicación: {anchors}\n"
            f"Problema: {item.problem}\n"
            f"Corrección: {item.fix}\n"
            f"Familias: {', '.join(item.families)}"),
        "acceptance": [
            "reproducir el problema; si no se reproduce, cerrar como no válido",
        ],
        "coding_tool": tier,
        "objective_id": str(objective.get("objective_id")),
        "origin": "objective_closure",
        "project": str(objective.get("project") or ""),
        "source": "spillover",
        "source_id": f"objclose:{objective.get('objective_id')}:{item.id}:"
                     f"{uuid.uuid4().hex[:8]}",
        "autorun": True,
    }
    group = str(objective.get("erp_group_ref") or "").strip()
    if group:
        payload["parent_task"] = group
    return payload


def _notify_ref(objective: Mapping[str, Any]) -> str:
    """ERP comments land on the parent ticket (the objective's group Task)."""
    return (str(objective.get("conversion_source_task_id") or "")
            or str(objective.get("objective_id")))


async def _finish_objective(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
) -> dict[str, Any]:
    """Close the objective: delivery record, ERP note, vault note (§9.7).

    The parent ticket (the ERP group) completes here, so the business
    close pushes through the standard queue: children first, then the
    group — a 417 parks as ``pending_status`` and the startup reconcile
    retries it. No group push is done directly.
    """
    from orchestration import knowledge as knowledge_mod

    objective_id = str(objective.get("objective_id"))
    children = await env.tasks.list_by_objective(objective_id)
    await env.objectives.patch_objective(objective_id, {
        "status": "closed",
        "closed_at": _utcnow_iso(),
        "delivery": _delivery_record(
            objective, children, verified_by="final_review"),
    })
    if env.ceo_wake is not None:
        try:
            env.ceo_wake(
                f"Objective {objective_id} closed: final review clean."
            )
        except Exception:
            pass
    parent_id = str(objective.get("conversion_source_task_id") or "")
    if parent_id:
        parent = await env.tasks.get_task(parent_id)
        if parent is not None and str(parent.get("status") or "") == "converted":
            await env.patch_task(parent_id, {
                "status": "completed",
                "waiting_reason": None,
                "resume_at": None,
                "completed_at": _utcnow_iso(),
                "last_response_summary": (
                    f"Objective {objective_id} closed: final review clean."),
            })
    ref = _notify_ref(objective)
    if env.notify is not None:
        tickets = await env.tasks.list_by_objective(objective_id)
        by_tier: dict[str, int] = {}
        for item in tickets:
            tier = str(item.get("coding_tool") or "?")
            by_tier[tier] = by_tier.get(tier, 0) + 1
        mix = ", ".join(f"{count}×{tier}" for tier, count in sorted(
            by_tier.items()))
        await env.notify(
            ref,
            f"Objective {objective_id} closed: {len(tickets)} tickets "
            f"({mix}), final review clean.")
        if not env.auto_merge:
            await env.notify(
                ref,
                f"Human PR needed: objective/{objective_id} → "
                f"{env.base_branch} (policy auto_merge=false, §12).")
    if env.vault:
        try:
            await knowledge_mod.write_objective_note(env, objective)
        except Exception as exc:
            if env.notify is not None:
                await env.notify(
                    ref, f"vault note failed (close stands): {exc}")
    return {"ok": True, "action": "closed", "objective_id": objective_id}


# ---------------------------------------------------------------------------
# Inverse webhook: the ERP group decision reaches the objective (F2)
# ---------------------------------------------------------------------------

#: Children worth cancelling when the group dies in ERPNext. Terminal and
#: failed/escalated outcomes are history and are never rewritten.
CASCADE_CANCEL_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "interrupted"})


async def reflect_group_decision(
    env: ObjectiveEnv,
    objective_id: str,
    *,
    target: str,
    business_status: str,
) -> dict[str, Any]:
    """Reflect a group Task decision (Completed/Cancelled) on the objective.

    Called after the parent ticket itself was patched with the anti-echo
    guard: patching the objective never pushes back, so no loop is
    possible. A business close wins over any pending review (the human is
    the review); a cancel stops the open children in Mongo so nothing
    keeps executing for a dead objective. Idempotent on retries.
    """
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "no-objective"}
    if str(objective.get("status") or "") in {"closed", "cancelled"}:
        return {"ok": True, "action": "already-terminal",
                "objective_id": objective_id}
    source_id = str(objective.get("conversion_source_task_id") or "")
    children = await env.tasks.list_by_objective(objective_id)
    if target == "completed":
        await env.objectives.patch_objective(objective_id, {
            "status": "closed",
            "closed_at": _utcnow_iso(),
            "closed_by": "business_decision",
            "delivery": _delivery_record(
                objective, children, verified_by="business_decision"),
        })
        if env.notify is not None:
            await env.notify(
                source_id or objective_id,
                f"Objective {objective_id} closed from ERPNext "
                f"({business_status}).")
        return {"ok": True, "action": "closed-by-business",
                "objective_id": objective_id}
    cancelled: list[str] = []
    for item in children:
        child_id = str(item.get("task_id") or "")
        if not child_id or child_id == source_id:
            continue
        if str(item.get("status") or "") not in CASCADE_CANCEL_STATUSES:
            continue
        await env.patch_task(child_id, {
            "status": "cancelled",
            "waiting_reason": None,
            "resume_at": None,
            "error": (f"Objective {objective_id} cancelled in ERPNext "
                      f"({business_status})."),
        })
        cancelled.append(child_id)
    await env.objectives.patch_objective(objective_id, {
        "status": "cancelled",
        "cancelled_at": _utcnow_iso(),
        "cancel_reason": f"group {business_status} in ERPNext",
        "cancelled_children": cancelled,
    })
    if env.notify is not None:
        await env.notify(
            source_id or objective_id,
            f"Objective {objective_id} cancelled from ERPNext "
            f"({business_status}): {len(cancelled)} open children cancelled.")
    return {"ok": True, "action": "cancelled",
            "objective_id": objective_id,
            "cancelled_children": cancelled}


# ---------------------------------------------------------------------------
# Objective branches (§12)
# ---------------------------------------------------------------------------

class ObjectiveBranchConflict(RuntimeError):
    """The objective branch cannot merge the base: human owns it."""


def checkout_and_update_objective_branch(
    repo: Path, objective_id: str, base_branch: str) -> str:
    """Checkout ``objective/<id>`` (creating it) and merge the base (§12).

    Merge, never rebase: the ticket chain must never depend on rewritten
    history. Conflicts raise :class:`ObjectiveBranchConflict` (the ticket
    waits as ``merge_conflict`` for the owner).
    """
    branch = f"objective/{objective_id}"
    if subprocess.run(
            ["git", "rev-parse", "--verify", branch], cwd=repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode != 0:
        proc = subprocess.run(
            ["git", "checkout", "-b", branch, base_branch], cwd=repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace")
        if proc.returncode != 0:
            raise ObjectiveBranchConflict(
                f"cannot create {branch}: {proc.stderr.strip()[:200]}")
        return branch
    proc = subprocess.run(
        ["git", "checkout", "-q", branch], cwd=repo,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace")
    if proc.returncode != 0:
        raise ObjectiveBranchConflict(f"cannot checkout {branch}")
    merged = subprocess.run(
        ["git", "merge", "--no-edit", base_branch], cwd=repo,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace")
    if merged.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], cwd=repo,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raise ObjectiveBranchConflict(
            f"merge {base_branch} into {branch}: "
            f"{merged.stderr.strip()[:200]}")
    return branch


# ---------------------------------------------------------------------------
# git helpers (read-only except where noted)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
