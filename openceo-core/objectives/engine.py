"""Company objectives: multi-ticket work chains (design §9, F6; F2 TASK-2026-00117).

One Objective type for every domain. Identity lives in Jardal ERP — a group Task
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
:func:`plan_objective` (next horizon of tickets), :func:`plan_pedido`
(the single planner decides one loose ticket without an explicit
decision: ticket or objective, agent, suggested skill and, for dev,
the tier — TASK-2026-00204), :func:`on_ticket_closed`
(triggers + capsule after any objective ticket closes) and
:func:`run_final_review` (planner declared ``complete_candidate``).
"""

from __future__ import annotations

from business.links import business_mirror

import asyncio
import logging
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from catalog import (Catalog, agent_detail_text, load_all_role_skills,
                     load_catalog, load_role_profile,
                     skills_index_for_planner, suggestable_skills)
from company_mandate import principles_prompt_section
from model_routing import review_wiring as wiring_mod
from objectives.money_guide import routing_guide_section
from orchestration import findings as findings_mod
from orchestration import inbox as inbox_mod
from orchestration import knowledge as knowledge_mod
from orchestration import review as review_mod
from orchestration import skill_metrics as skill_metrics_mod
from orchestration.live import TIMEOUT_RETURNCODE, ProcessExecutor
from objectives import planner_models as planner_mod
from objectives import plan_spend as spend_mod
from objectives import playbooks as playbooks_mod
from objectives import recovery_policy as recovery_mod
from objectives import ticket_cost as cost_mod
from owner_channel import (
    build_owner_question,
    owner_question_block,
    question_answered,
    tool_proposal_source_id,
)
from state import BLOCKER_RELEASED_STATUSES

logger = logging.getLogger(__name__)

#: A converted ticket's planner analysis (SIZING.md or the oversize PLAN.md)
#: is kept whole for the objective planner: it cost a full planner run
#: (TASK-2026-00510). The prompt carries a head; the rest rides a file.
SIZING_HINT_MAX_CHARS = 60000
SIZING_HINT_INLINE_CHARS = 3000


class Runner(Protocol):
    """Structural executor (see :class:`orchestration.live.Executor`)."""

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any: ...
    async def cleanup(self, role: str) -> None: ...


class Quota(Protocol):
    async def check(self, accounts: Sequence[str]) -> Mapping[str, Any]: ...


class ObjectiveErp(Protocol):
    """Business-identity operations the engine needs (implemented by wiring).

    The engine never imports the Jardal ERP adapter: group conversion is the
    only ERP write it performs directly (children link through the common
    ticket creation, close pushes through the parent ticket).
    """

    async def convert_to_group(self, identifier: str) -> dict[str, Any]: ...

    async def set_dependencies(
        self, identifier: str, depends_on: list[str]) -> dict[str, Any]: ...

    async def thread_of(
        self, task_name: str, *, limit: int = 20) -> dict[str, Any]: ...


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
    async def list_boot_resumable_objectives(
        self, limit: int = 100) -> list[dict[str, Any]]: ...
    async def claim_recovery(
        self, objective_id: str, due_at: Any, *,
        count_attempt: bool = True) -> dict[str, Any] | None: ...


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
    # Raw reviewer entries behind ``pool`` (TASK-2026-00403): the final
    # review assembles the round pool from these so the free-router can
    # swap static OpenCode entries for ranked dynamic slots. Empty in
    # bare unit envs: assembly then falls back to the static pool.
    pool_raw: list[dict[str, Any]] = field(default_factory=list)
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
    # Dispatcher wake hook (TASK-2026-00174): filled contracts re-enter
    # the queue, and routing-held tickets never self-wake. Wired by the
    # service layers; None in bare unit envs.
    dispatcher_wake: Callable[[], None] | None = None
    # Reactive CEO wake (F4 TASK-2026-00119): the engine calls it with a
    # reason when an objective parks or closes. Sync, never fatal, None
    # in bare unit envs and dev flows that do not wire the CEO loop.
    ceo_wake: Callable[[str], None] | None = None
    # Recovery scheduler wake (TASK-2026-00481): parks persist their due
    # time and this event-driven hook wakes the single timer; None in bare
    # unit envs (the park still records the due time).
    recovery_wake: Callable[[], None] | None = None
    # Normalized per-project tier routing (TASK-2026-00161): the
    # ``dev.routing`` mapping for the objective's project (``{}`` when the
    # project carries no characteristic). Feeds the planner prompt guide.
    project_routing: dict[str, Any] = field(default_factory=dict)
    # Deterministic delivery verifiers by non-dev domain
    # (TASK-2026-00175): ``domain -> DeliveryVerifier``. Dev work verifies
    # through the reviewer pool, never here. Prod registers the generic
    # check per enabled AI agent (TASK-2026-00216); domains outside the
    # catalog still park as capability_gap.
    delivery_verifiers: dict[str, DeliveryVerifier] = field(
        default_factory=dict)
    # Playbook proposals on close (TASK-2026-00217): the planner judges
    # recurrence once the close stands. Off in bare envs (tests opt in),
    # on in service-built envs.
    playbook_proposals: bool = False
    # Company mandate for the spend gate (TASK-2026-00207): declared
    # plan spend is checked against it before anything persists or
    # launches. None in bare unit envs and when the file is unreadable;
    # then any declared spend escalates, while spend-free plans launch.
    mandate: Any | None = None
    # Mission store (TASK-2026-00218): conversion links objectives to
    # their mission and the close records the ledger. None in bare
    # unit envs and mission-less flows: mission linkage then degrades
    # to the ticket params the planner already sees.
    missions: Any | None = None


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

    async def list_boot_resumable_objectives(
        self, limit: int = 100,
    ) -> list[dict[str, Any]]:
        # ``active`` rows ride along: the decision leaves gapped ones
        # alone and only clean orphans resume (TASK-2026-00406).
        return [dict(doc) for doc in self.docs.values()
                if doc.get("status") in (
                    "active", "final_review", "waiting")][:limit]

    async def claim_recovery(
        self, objective_id: str, due_at: Any, *, count_attempt: bool = True,
    ) -> dict[str, Any] | None:
        """Test double of the Mongo compare-and-set claim."""
        doc = self.docs.get(objective_id)
        if doc is None or doc.get("status") != "waiting":
            return None
        recovery = doc.get("recovery")
        if not isinstance(recovery, dict) or recovery.get("due_at") != due_at:
            return None
        if count_attempt:
            recovery["attempts"] = int(recovery.get("attempts") or 0) + 1
        recovery.pop("deploy", None)
        recovery["last_attempt_at"] = _utcnow_iso()
        recovery["due_at"] = None
        doc["status"] = "active"
        doc["waiting_reason"] = None
        return dict(doc)


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

    async def list_boot_resumable_objectives(
        self, limit: int = 100,
    ) -> list[dict[str, Any]]:
        return await self._repo.list_boot_resumable_objectives(limit)

    async def claim_recovery(
        self, objective_id: str, due_at: Any, *, count_attempt: bool = True,
    ) -> dict[str, Any] | None:
        return await self._repo.claim_objective_recovery(
            objective_id, due_at, count_attempt=count_attempt)


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

#: Process-wide recovery wake (TASK-2026-00481): dev-flow envs do not
#: carry the scheduler on every construction, so the single timer registers
#: here and every park wakes it without a DB sweep.
RECOVERY_WAKE: Callable[[], None] | None = None

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
    """Business ``TASK-…`` name from the ticket's business link ("" when none)."""
    if not isinstance(ticket, Mapping):
        return ""
    params = ticket.get("params")
    mirrors = params.get("_mirrors") if isinstance(params, Mapping) else None
    mirror = business_mirror(mirrors)
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
        "sizing_hint": sizing_hint[:SIZING_HINT_MAX_CHARS],
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
    except Exception:  # noqa: BLE001
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


async def _adopt_mission_proposal(
    env: ObjectiveEnv,
    mission_id: str,
    objective_id: str,
    acceptance: Any,
) -> None:
    """Link the converted objective; adopt + ask first proposal (00218).

    Never breaks the conversion: every failure degrades to a log line
    and the mission keeps whatever it already had.
    """
    store = getattr(env, "missions", None)
    if store is None:
        return
    from objectives import missions as missions_mod

    try:
        mission = await store.get_mission(mission_id)
    except Exception:  # noqa: BLE001
        logger.warning("mission %s unreadable (convert stands)",
                       mission_id)
        return
    if mission is None:
        return
    try:
        await missions_mod.record_objective_linked(
            store, mission_id, objective_id)
    except Exception:  # noqa: BLE001
        logger.warning("mission ledger failed for %s (convert stands)",
                       mission_id)
    patch = missions_mod.adopt_proposal(mission, acceptance or [])
    if patch is None:
        return
    merged = dict(mission)
    merged.update(patch)
    try:
        spec = missions_mod.acceptance_question_spec(merged)
    except missions_mod.MissionRejected:
        return
    try:
        question = await env.create_ticket({
            "title": spec["title"],
            "description": spec["description"],
            "project": spec["project"],
            "source": spec["source"],
            "source_id": spec["source_id"],
            "params": spec["params"],
            "priority": spec["priority"],
            "tags": spec["tags"],
            "autorun": False,
        })
    except Exception:
        logger.warning("mission question failed for %s (proposal "
                       "unasked)", mission_id, exc_info=True)
        return
    question_id = str((question or {}).get("task_id") or "")
    if not question_id:
        logger.warning("mission question failed for %s (proposal "
                       "unasked)", mission_id)
        return
    try:
        await env.patch_task(question_id, {
            "status": "waiting",
            "phase": spec["phase"],
            "waiting_reason": spec["waiting_reason"],
            "workflow_key": None,
            "workflow_phases": [],
            "phase_index": 0,
            "phase_assignments": {},
        })
    except Exception:
        logger.warning("mission question failed for %s (proposal "
                       "unasked)", mission_id, exc_info=True)
        return
    checkpoint = ((spec.get("params") or {}).get("owner_question") or {}
                  ).get("checkpoint") or {}
    try:
        await store.patch_mission(mission_id, {
            **patch,
            "question_task_id": question_id,
            "question_deadline": str(checkpoint.get("deadline") or ""),
        })
    except Exception:  # noqa: BLE001
        logger.warning("mission adoption failed for %s (convert stands)",
                       mission_id)


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
    decomposes inside it instead of minting a second objective; its open
    dependents then wait for the replacements the next plan creates
    (``pending_rewires``, TASK-2026-00492). When the
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
            patch: dict[str, Any] = {}
            if sizing_text.strip():
                patch["sizing_hint"] = sizing_text[:SIZING_HINT_MAX_CHARS]
            dependents = await _open_dependents(env, own_id, task_id)
            if dependents:
                patch["pending_rewires"] = [
                    *(entry for entry in own.get("pending_rewires") or []
                      if isinstance(entry, Mapping)),
                    {"from": task_id, "dependents": dependents}]
            if patch:
                await env.objectives.patch_objective(own_id, patch)
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
    mission_id = ""
    params = ticket.get("params")
    if isinstance(params, Mapping):
        mission_id = str(params.get("mission_id") or "").strip()
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
    if mission_id:
        try:
            await env.objectives.patch_objective(
                objective_id, {"mission_id": mission_id})
        except Exception:  # noqa: BLE001
            logger.warning("mission link failed for %s (convert "
                           "stands): %s", objective_id, mission_id)
        await _adopt_mission_proposal(env, mission_id, objective_id,
                                      acceptance)
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
#: (``.openceo/objectives/<id>/<name>``), never under the scratch root:
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
SCRATCH_DIRNAME = ".openceo"


def scratch_root(repo: Path) -> Path:
    """Canonical objectives scratch root: ``repo/.openceo`` (see above)."""
    return repo / SCRATCH_DIRNAME


def objective_scratch(repo: Path, objective_id: str) -> Path:
    """Per-objective scratch dir: ``repo/.openceo/objectives/<id>``."""
    return scratch_root(repo) / "objectives" / objective_id


def _stage_policy_details(
        scratch: Path,
        details: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """Stage per-agent policy texts where the fenced planner can read them.

    The planner runs with the project repo as its workspace; the
    install dir is outside its fence. Each detail is written verbatim
    (the exact hashed bytes) to ``policy/<agent>.md`` under the
    objective scratch; the paths ride the prompt index.
    """
    staged: list[tuple[str, str]] = []
    folder = scratch / "policy"
    # Purge first: a detail deleted upstream must not linger unindexed.
    shutil.rmtree(folder, ignore_errors=True)
    if not details:
        return staged
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in details:
        target = folder / f"{name}.md"
        target.write_text(text, encoding="utf-8")
        staged.append((name, str(target)))
    return staged


def _planner_skill_metrics() -> dict[str, str] | None:
    """Latest-version success annotation per skill, None when unavailable.

    Fail-open (TASK-2026-00334): metrics are runtime state next to the
    crystallization lock, and the planner prompt renders as before when
    the store is missing or unreadable.
    """
    try:
        from catalog import catalog_root
        store = skill_metrics_mod.load_store(
            skill_metrics_mod.default_store_path(
                catalog_root().parent))
        notes: dict[str, str] = {}
        for key in store.get("skills", {}):
            role, _, name = key.partition("/")
            metrics = skill_metrics_mod.latest_version(store, role, name)
            if metrics is not None and metrics.uses > 0:
                notes[key] = (
                    f"v{metrics.version} "
                    f"{skill_metrics_mod.success_rate(metrics):.0%} "
                    f"verified over {metrics.uses} uses")
        return notes or None
    except Exception:  # noqa: BLE001
        return None


def _planner_skills() -> tuple[str, dict[str, list[str]]]:
    """Skills index block + suggestable map for one planning cycle.

    Loaded fresh from the catalog so a newly crystallized skill is
    suggestable without a restart. Fail-open: the boot gate already
    refuses an invalid catalog, and a suggestion is only a hint, so a
    mid-run load failure skips the skill check instead of breaking the
    whole planning cycle.
    """
    try:
        catalog = load_catalog()
        by_agent = load_all_role_skills(catalog)
        index = skills_index_for_planner(
            by_agent, _planner_skill_metrics())
    except Exception as exc:  # noqa: BLE001
        logger.warning("planner skills unavailable, skipping skill "
                       "check this cycle: %s", exc)
        return "", {}
    block = ("AGENT SKILLS (suggest `skill` only from the target "
             "agent's list; the agent decides):\n" + index) if index else ""
    return block, suggestable_skills(catalog, by_agent)


def _planner_playbooks(
    repo: Path | str | None = None,
) -> tuple[str, set[str] | None]:
    """Playbook index block + approved names for one cycle.

    Fail-open like skills: a load failure skips the playbook check
    (None), while an empty shelf validates strictly (no approved
    names to cite).
    """
    try:
        books = playbooks_mod.load_playbooks(repo)
    except Exception as exc:  # noqa: BLE001
        logger.warning("playbook index unavailable, skipping playbook "
                       "check this cycle: %s", exc)
        return "", None
    return playbooks_mod.playbooks_index_text(books), set(books)


def _planner_catalog() -> Catalog | None:
    """Fresh catalog for planner context, None when unloadable.

    Fail-open like skills: the boot gate already refuses an invalid
    catalog, so a mid-run load failure degrades the registry lines to
    bare names instead of breaking the planning cycle.
    """
    try:
        return load_catalog()
    except Exception as exc:  # noqa: BLE001
        logger.warning("planner catalog unavailable, registry lines stay "
                       "bare this cycle: %s", exc)
        return None


def stage_sizing_hint(scratch: Path, text: str) -> str:
    """Write the full sizing hint where the fenced planner can read it."""
    target = scratch / "SIZING_HINT.md"
    if not text.strip():
        target.unlink(missing_ok=True)
        return ""
    target.write_text(text, encoding="utf-8")
    return str(target)


def _stage_agent_details(
    scratch: Path,
    catalog: Catalog,
    root: str | Path | None = None,
) -> list[tuple[str, str]]:
    """Stage one compact agent detail file per catalog agent.

    Mirror of :func:`_stage_policy_details` for TASK-2026-00244: the
    planner runs fenced in the project repo while the catalog lives in
    the install dir, so each detail text lands verbatim under
    ``agents/<name>.md`` and the paths ride the prompt index. A profile
    that fails to load degrades that agent to card-only detail (the
    registry line still carries its department and summary).
    """
    staged: list[tuple[str, str]] = []
    folder = scratch / "agents"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    for name in sorted(catalog.agents):
        card = catalog.agents[name]
        profile = None
        if (card.profile or "").strip():
            try:
                profile = load_role_profile(
                    Path(str(card.profile)).stem, root)
            except Exception as exc:  # noqa: BLE001
                logger.warning("agent detail for %s without profile: %s",
                               name, exc)
        target = folder / f"{name}.md"
        target.write_text(agent_detail_text(catalog, name, profile),
                          encoding="utf-8")
        staged.append((name, str(target)))
    return staged


def _agents_section(
    agents: Mapping[str, Any],
    skills_md: str = "",
    catalog: Catalog | None = None,
    detail_paths: Mapping[str, str] | None = None,
) -> str:
    """Authoritative registry rendered for the planner prompt.

    With a catalog each line also carries the department and the index
    summary, and staged detail paths ride a closing block
    (TASK-2026-00244); without one the bare name + enablement shape
    stays. Non-catalog registry names (staff) always render bare.
    """
    lines = ["REGISTERED AGENTS (select target_agent ONLY from enabled):"]
    summaries = {entry.name: entry.summary for entry in catalog.entries} \
        if catalog is not None else {}
    for name in sorted(agents):
        spec = agents.get(name)
        if not isinstance(spec, Mapping):
            continue
        label = name
        card = catalog.agents.get(name) if catalog is not None else None
        if card is not None and card.department:
            label += f" [{card.department}]"
        summary = summaries.get(name, "")
        if summary:
            label += f": {summary}"
        head = f"- {label}"
        if spec.get("enabled", False):
            lines.append(head + (" (enabled)" if summary else ": enabled"))
        else:
            reason = str(spec.get("reason") or "disabled").strip()
            if summary:
                lines.append(
                    f"{head} — DISABLED ({reason}) — never route here")
            else:
                lines.append(
                    f"{head}: DISABLED ({reason}) — never route here")
    lines.append("Never invent an agent. If no enabled agent can do required "
                 "work, return capability_gap with no tickets.")
    lines.append(_AGENT_REACH)
    if skills_md.strip():
        lines.extend(["", skills_md.strip()])
    staged = {name: str(path) for name, path in (detail_paths or {}).items()
              if str(path).strip()}
    if staged:
        lines.extend([
            "",
            "AGENT DETAIL (read the agent's file only when the assignment "
            "materially depends on it; skill recipes stay unloaded):",
            *[f"- {name}: {staged[name]}" for name in sorted(staged)],
        ])
    return "\n".join(lines)


#: Planner-side quality bar (routing policy §44, TASK-2026-00492).
_QUALITY_GUIDE = (
    "ENGINEERING QUALITY (policy §44; every agent and stack):",
    "- Contracts aim at production-grade work. Constraints never forbid",
    "  the stack's standard maintained libraries or frameworks unless a",
    "  project rule or the Owner does (cite it). Reuse what meets the",
    "  bar; a missing foundation gets a ticket that builds it with the",
    "  standard tool, never 'restyle only' or 'minimal diff' over it.",
    "- Brand or business words describe the product, not names: write",
    "  conventional engineering terms into the contract.",
    "- Acceptance says how the result is verified as its user meets it",
    "  (UI rendered in a real browser, API answering real requests,",
    "  migration applied and rolled back); tests check behavior.",
    "- An input the goal names (e.g. an updated design doc) must exist",
    "  on the base branch; if it is missing or stale there, block and",
    "  ask instead of building on the old one.",
    "- One ticket owns a coherent slice inside the size budget (by",
    "  default 8 files and 400 non-test lines; past 800 the diff fails):",
    "  split bigger work along its seams, never into micro-tickets that",
    "  each pay a build and review cycle.",
)

#: The per-ticket review switch as both planner prompts describe it.
_REVIEW_FIELD_LINES = (
    '    "review?": "<dev only: on|off, omit = on. off for an A/B/C '
    'ticket whose risk does not pay for a review round (docs/config, '
    'mechanical or fully specified change with a qualifying oracle, '
    'trivial fix); keep on for logic, data flow, events, state, auth, '
    'money, persistence, concurrency or public contracts. Preserve an '
    'explicit user instruction; tier_s always reviews (off rejected)>",',
    '    "review_reason?": "<required when review is off: why no review '
    'round is needed, or the user instruction>",',
)

#: The per-ticket empty-diff flag as both planner prompts describe it.
_EMPTY_DIFF_FIELD_LINES = (
    '    "allow_empty_diff?": "<dev only: true when zero changes are a '
    'valid success (read-only verification or decision ticket); omit '
    'otherwise. Rejects oracle and includes_findings>",',
)


# ---------------------------------------------------------------------------
# Planner memory: business record, objective notes, agent reach (00571)
# ---------------------------------------------------------------------------

#: Heads of the comments OpenCEO posts by itself (engine notices, mechanic,
#: return notes): they echo its own conclusions and are never evidence.
AUTOMATED_NOTE_HEADS = (
    "Objective ", "Planner ", "Ticket converted to objective",
    "Ticket decomposes inside objective", "Plan aprobado",
    "El plan retenido", "Gasto aprobado", "objective trigger failed",
    "[mecánico", "Agent ", "Ticket failed without verdict",
)
OBJECTIVE_NOTES_MAX = 6
OBJECTIVE_NOTES_SCAN = 60
CLOSED_CHILDREN_MAX = 15

_AGENT_REACH = (
    "BUSINESS-RECORD REACH (enforced by each agent's badge, not by the "
    "prompt): an AI agent reads, comments and attaches only on its OWN "
    "ticket and searches only its own project. Work that must read or "
    "write OTHER business tickets (publish comments, change states, paste "
    "texts into another ticket) is Owner work: return blocked with the "
    "exact Owner action, or propose a boundary tool when it should become "
    "automatable. Never route it to an AI agent.")


def _ticket_mirror(ticket: Mapping[str, Any]) -> Mapping[str, Any]:
    params = ticket.get("params")
    if not isinstance(params, Mapping):
        return {}
    mirror = business_mirror(params.get("_mirrors") or {})
    return mirror if isinstance(mirror, Mapping) else {}


def business_decision(ticket: Mapping[str, Any]) -> dict[str, str] | None:
    """The human business decision that closed a child, if any.

    Both human channels audit it on the business mirror: an Owner
    session's ``openceo_transition_ticket`` (reason + evidence) and a
    close in the Jardal ERP desk (``channel: desk``). The agent cycle
    never writes it.
    """
    transition = _ticket_mirror(ticket).get("transition")
    if not isinstance(transition, Mapping):
        return None
    target = str(transition.get("to") or "")
    if target not in ("Completed", "Cancelled") \
            or target.lower() != str(ticket.get("status") or ""):
        return None
    channel = "ERP desk" if transition.get("channel") == "desk" \
        else "Owner session"
    return {"to": target, "at": str(transition.get("at") or "")[:16],
            "channel": channel,
            "reason": str(transition.get("reason") or "").strip(),
            "evidence": str(transition.get("evidence") or "").strip()}


def business_completion(ticket: Mapping[str, Any]) -> str:
    """Evidence of a completion a person decided in the business, or ``""``.

    Jardal ERP business status is authoritative: work a person closed as
    Completed is delivered, with no agent verdict behind it.
    """
    decision = business_decision(ticket)
    if decision is None or decision["to"] != "Completed":
        return ""
    return decision["evidence"] or decision["reason"] \
        or f"closed in the {decision['channel']}"


def _closed_at(ticket: Mapping[str, Any]) -> datetime:
    moment = recovery_mod.parse_due(
        ticket.get("completed_at") or ticket.get("updated_at"))
    return moment or datetime.min.replace(tzinfo=timezone.utc)


def _closed_child_line(ticket: Mapping[str, Any],
                       logged: Mapping[str, Any] | None) -> str:
    params = ticket.get("params")
    params = params if isinstance(params, Mapping) else {}
    number = ticket.get("ticket_number")
    ref = str(_ticket_mirror(ticket).get("name") or "") \
        or (f"#{number}" if number else str(ticket.get("task_id") or "")[:8])
    who = str(params.get("target_agent") or ticket.get("agent_hint")
              or ticket.get("agent") or "?")
    if ticket.get("coding_tool"):
        who += f"/{ticket['coding_tool']}"
    status = str(ticket.get("status") or "")
    decision = business_decision(ticket)
    if decision is not None:
        label = (f"{status.upper()} by business decision "
                 f"({decision['channel']} {decision['at']})")
        detail = decision["reason"]
        if decision["evidence"]:
            detail += f" | evidence: {decision['evidence']}"
    elif status == "cancelled":
        label, detail = "CANCELLED", str(ticket.get("error") or "")
    else:
        verdict = params.get("agent_result")
        label = "COMPLETED"
        detail = str((logged or {}).get("evidencia")
                     or (verdict.get("evidencia")
                         if isinstance(verdict, Mapping) else "")
                     or ticket.get("last_response_summary") or "")
    detail = " ".join(detail.split())[:400] or "(no detail recorded)"
    return (f"- {ref} {str(ticket.get('title') or '')[:140]} [{who}] "
            f"{label}: {detail}")


def closed_children_lines(
    objective: Mapping[str, Any],
    tickets: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Completed and cancelled children, newest last, whoever closed them."""
    shell = str(objective.get("conversion_source_task_id") or "")
    logged = {str(entry.get("task_id") or ""): entry
              for entry in objective.get("tickets_log") or []
              if isinstance(entry, Mapping)}
    closed = sorted(
        (item for item in tickets
         if str(item.get("status") or "") in ("completed", "cancelled")
         and str(item.get("task_id") or "") != shell),
        key=_closed_at)
    return [_closed_child_line(item, logged.get(str(item.get("task_id"))))
            for item in closed[-CLOSED_CHILDREN_MAX:]]


def superseded_returns(
    objective: Mapping[str, Any],
    tickets: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Returned tickets a later close settled: their return is history."""
    returned = {str(entry.get("task_id") or "")
                for entry in objective.get("planner_returns") or []
                if isinstance(entry, Mapping)}
    settled: dict[str, str] = {}
    for item in tickets:
        task_id = str(item.get("task_id") or "")
        status = str(item.get("status") or "")
        if task_id not in returned or status not in ("completed", "cancelled"):
            continue
        decision = business_decision(item)
        why = (decision or {}).get("reason") or (
            str(item.get("error") or "") if status == "cancelled" else "")
        why = " ".join(why.split())[:160]
        settled[task_id] = f"since {status}" + (f": {why}" if why else "")
    return settled


def repeated_boundary_denials(
    objective: Mapping[str, Any], denials: Sequence[str],
) -> list[str]:
    """Denials an earlier return of this objective already hit.

    A badge does not widen by replanning: the same refusal on a second
    ticket means the need is outside every agent's reach.
    """
    seen = {str(item) for entry in objective.get("planner_returns") or []
            if isinstance(entry, Mapping)
            for item in entry.get("denials") or []}
    return [item for item in denials if item in seen]


async def objective_notes(
    env: ObjectiveEnv, objective: Mapping[str, Any],
) -> list[str]:
    """Comments on the objective ticket written by people, newest last.

    Fail-soft: without the business reader or on any error the planner
    plans without notes, as before.
    """
    reader = getattr(getattr(env, "erp", None), "thread_of", None)
    name = str(objective.get("erp_group_ref") or "").strip()
    if reader is None or not name:
        return []
    try:
        thread = await reader(name, limit=OBJECTIVE_NOTES_SCAN)
    except Exception:  # noqa: BLE001 - notes enrich, never block planning
        logger.warning("objective notes unreadable for %s", name,
                       exc_info=True)
        return []
    rows = thread.get("comments") if isinstance(thread, Mapping) else None
    notes: list[str] = []
    for row in rows or []:
        if not isinstance(row, Mapping):
            continue
        text = " ".join(str(row.get("content") or "").split())
        if not text or text.startswith(AUTOMATED_NOTE_HEADS):
            continue
        when = str(row.get("createdAt") or row.get("creation") or "")[:16]
        notes.append(f"- {when} {text[:700]}")
        if len(notes) >= OBJECTIVE_NOTES_MAX:
            break
    return list(reversed(notes))


def _planner_prompt(
    objective: Mapping[str, Any],
    capsule_text: str,
    open_tickets: Sequence[Mapping[str, Any]],
    inbox_md: str,
    deferred: Sequence[str],
    sizing_hint: str = "",
    blocked_tickets: Sequence[Mapping[str, Any]] = (),
    tierless: Sequence[Mapping[str, Any]] = (),
    agents_md: str = "",
    policy_text: str = "",
    policy_index: str = "",
    handoffs: Sequence[str] = (),
    roadmap: Sequence[str] = (),
    repo: Path | str | None = None,
    routing_guide: str = "",
    spend_guide: str = "",
    principles_guide: str = "",
    cost_guide: str = "",
    playbooks_md: str = "",
    parked_md: str = "",
    mission_md: str = "",
    sizing_path: str = "",
    closed_children: Sequence[str] = (),
    superseded: Mapping[str, str] | None = None,
    notes: Sequence[str] = (),
    group_ref: str = "",
) -> str:
    """Planner prompt: bounded state + JSON contract (self-contained).

    The prompt carries the full ``PlannerResult`` shape inline: the planner
    runs in the project repo and cannot rely on reading OpenCEO sources.
    Closed children, settled returns and the objective notes are the
    business memory (TASK-2026-00571): every replan starts from them.
    """
    settled = dict(superseded or {})
    criteria = [f"- {item}" for item in (objective.get("anchor") or {}).get(
        "acceptance", [])] or ["- (derive verifiable criteria)"]

    def _ticket_line(item: Mapping[str, Any]) -> str:
        line = (f"- {item.get('task_id')}: {item.get('title')} "
                f"[{item.get('coding_tool')}] ({item.get('status')})")
        blockers = item.get("blocked_by") or []
        if blockers:
            line += f" blocked_by={list(blockers)}"
        if item.get("waiting_reason"):
            line += f" wait={str(item['waiting_reason'])[:300]}"
        if item.get("resume_at"):
            line += f" resume_at={item['resume_at']}"
        return line

    def _failure_line(item: Any) -> str:
        if not isinstance(item, Mapping):
            return "- (unreadable failure entry)"
        return (f"- {item.get('title')} [{item.get('tier')}]: "
                f"{str(item.get('cause') or '')[:300]}")

    def _return_line(item: Any) -> str:
        if not isinstance(item, Mapping):
            return "- (unreadable return entry)"
        need = str(item.get("necesidad") or "").strip() or "-"
        line = (f"- {item.get('title')} [{item.get('agent')}/"
                f"{item.get('resultado')}]: "
                f"{str(item.get('motivo') or '')[:300]} | "
                f"Necesidad: {need[:200]}")
        denials = [str(entry) for entry in item.get("denials") or []]
        if denials:
            line += f" | Badge denied: {'; '.join(denials)[:300]}"
        settled_as = settled.get(str(item.get("task_id") or ""))
        if settled_as:
            line += f" | SUPERSEDED, {settled_as}"
        return line

    opened = [_ticket_line(item) for item in open_tickets] or ["- (none)"]
    blocked = [_ticket_line(item) for item in blocked_tickets] or ["- (none)"]
    bare = [f"- {item.get('task_id')}: {item.get('title')} :: "
            f"{str(item.get('description') or '')[:300]}"
            for item in tierless]
    failures = [_failure_line(item)
                for item in list(objective.get("material_failures") or [])[-3:]]
    returns = [_return_line(item)
               for item in list(objective.get("planner_returns") or [])[-3:]]
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
        *(["", mission_md.strip()] if mission_md.strip() else []),
        "",
        "CAPSULE (compiled state):",
        capsule_text or "(empty: first planning)",
        "",
        "CLOSED CHILDREN (business record, newest last; terminal evidence",
        "whoever closed them: agent cycle, Owner session or ERP desk).",
        "Never plan again what a completed child delivered; a cancelled",
        "child's reason binds (e.g. a duplicate). Rework names a new",
        "defect in the delivery, never the same goal again:",
        *(list(closed_children) or ["- (none)"]),
        "",
        "OBJECTIVE NOTES (comments by people on the objective ticket"
        + (f" {group_ref}" if group_ref else "") + ", newest last; engine",
        "notices left out). Terminal evidence: what a note reports done or",
        "decided is done, and its scope decisions bind this plan:",
        *(list(notes) or ["- (none)"]),
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
        "AGENT RETURNS (latest last; relaunch with guidance in the ticket",
        "description, reassign, split, or return capability_gap; a",
        "SUPERSEDED return was settled by a later close and is history;",
        "a badge denial never clears by replanning: see the reach rule):",
        *(returns or ["- (none)"]),
        "",
        "INBOX (pre-existing findings touching objective files, "
        "claim via includes_findings):",
        inbox_md,
    ]
    if cost_guide.strip():
        lines.extend(["", cost_guide.strip()])
    if deferred:
        lines.extend(["", "DEFERRED (reviewer suggestions out of scope):",
                      *[f"- {item}" for item in deferred]])
    if bare:
        lines.extend([
            "",
            "TIERLESS CHILDREN AWAITING CONTRACT (complete each with a",
            "full-contract entry carrying its completes_task_id; never",
            "recreate them as new tickets):",
            *bare])
    if sizing_hint:
        lines.extend(["", "SIZING HINT from the converted ticket (its planner "
                      "already analyzed the code: build on it, do not "
                      "re-investigate what it covers):",
                      sizing_hint[:SIZING_HINT_INLINE_CHARS]])
        if sizing_path and len(sizing_hint) > SIZING_HINT_INLINE_CHARS:
            lines.append(f"Full analysis ({len(sizing_hint)} chars), read it "
                         f"before splitting: {sizing_path}")
    if agents_md:
        lines.extend(["", agents_md])
    if playbooks_md.strip():
        lines.extend(["", playbooks_md.strip()])
    if parked_md.strip():
        lines.extend([
            "",
            "RECOVERY CONTEXT: the objective was parked and this planning",
            "run is an automatic re-check (bounded). First test the park",
            "against the evidence above (closed children, objective notes,",
            "agent returns): if it is resolved, continue, or declare",
            "complete_candidate when the acceptance is met. If it still",
            "needs a human, return blocked with the exact Owner action. Only",
            "a failed approach changes (split, another agent or tier); never",
            "repeat a ticket unchanged and never hand an AI agent work its",
            "reach denies.",
            parked_md.strip(),
        ])
    lines.extend([
        "",
        "ROUTING (summary; the normative policy below rules on conflicts):",
        "- Choose target_agent FIRST, only from enabled registered agents.",
        "- skill is an optional suggestion for any agent; the agent decides.",
        "- Follow an approved PLAYBOOK by default when one fits; name it",
        "  in `playbook`, explain deviations in `playbook_deviations`, and",
        "  propose improvements in `proposed_playbook_update` (the Owner",
        "  approves through the human channel).",
        "- DEV tiers apply only when target_agent == dev, in this order.",
        "  All four executors are top-tier models; letters order the work,",
        "  never the models. Never describe a tier as cheap or weaker.",
        "  1. tier_s: CRITICAL work only. A residual failure this ticket",
        "     could leave would silently cause money loss, a security or",
        "     privacy breach (auth, permissions, data isolation between",
        "     companies), irreversible data loss, or a broken core",
        "     contract many components rely on; AND orchestrating several",
        "     independent model families (independent plan, challenge,",
        "     build, multi-family review, arbitration) would catch it",
        "     better than one session. Also when the owner or the ticket",
        "     asks for maximum assurance. Inside a critical area S goes to",
        "     the ticket that defines the rule; consumers (wiring, UI,",
        "     tests, docs) take A/B/C. Not rationed by percentage or count.",
        "     Justify with why_consensus_is_needed (name the critical",
        "     category and the residual failure) +",
        "     why_single_session_is_insufficient (or cite the request).",
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
        "- SPEND: declare every new cost in `costs` (per ticket, or",
        "  result-level for shared costs): `label` plus `amount` as a",
        "  decimal string in the mandate currency, or null when unknown.",
        "  Unknown cost is never zero: it waits for the Owner. Omit the",
        "  key when the work spends nothing.",
        "- NEW BOUNDARY TOOL (rare; at most "
        f"{planner_mod.PROPOSED_TOOLS_MAX} per cycle): when an agent need "
        "names a missing boundary tool — a secret the agent must not "
        "hold, a lock that must not depend on the model obeying, or a "
        "business-record write with its own rules — list it in "
        "proposed_tools. The Owner approves through the human channel; "
        "approval creates the dev ticket that builds it. Never propose "
        "for technical failures (broken tests, fallen reviewers, plan "
        "errors): replan those. Omit the key when nothing is missing.",
        "",
        *_QUALITY_GUIDE,
    ])
    if (routing_guide or "").strip():
        lines.extend(["", routing_guide.strip()])
    if (spend_guide or "").strip():
        lines.extend(["", spend_guide.strip()])
    if (principles_guide or "").strip():
        lines.extend(["", principles_guide.strip()])
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
        '"low|medium|high|critical", "resolution": "..."}], '
        '(materiality is exactly one of the four, e.g. "high"),',
        '  "tickets": [ <1-3, ONLY when active> {',
        '    "local_ref": "<unique short key>", "title": "...",',
        '    "target_agent": "<enabled agent>", "work_type": '
        '"<e.g. implementation|debugging|research|documentation>",',
        '    "skill?": "<optional suggested skill, any agent>",',
        '    "goal": "...", "why_now": "...",',
        '    "inputs": [], "constraints": [], "out_of_scope": [],',
        '    "acceptance_criteria": ["<at least one>"],',
        '    "expected_artifacts": ["<at least one>"],',
        '    "blocked_by": ["<local_ref or existing task_id, acyclic>"],',
        '    "coding_tool": "<dev only: tier_s|tier_a|tier_b|tier_c>",',
        *_REVIEW_FIELD_LINES,
        *_EMPTY_DIFF_FIELD_LINES,
        '    "tier_reason": "<dev only: why this tier>",',
        '    "oracle": {"type": "<oracle type>", "refs": [{"ref": "...",',
        '      "description?": "..."}]},',
        '    "why_consensus_is_needed?": "<tier_s only>",',
        '    "why_single_session_is_insufficient?": "<tier_s only>",',
        '    "why_not_c?": "<required for tier_a/tier_b: what breaks in C; '
        'omit for tier_s and tier_c>",',
        '    "includes_findings?": ["<inbox fingerprints claimed>"],',
        '    "completes_task_id?": "<to COMPLETE a tierless child above '
        'instead of creating: its task_id>",',
        '    "costs?": [{"label": "<what it buys>",',
        '      "amount": "<decimal string in the mandate currency, '
        'or null when unknown>"}]',
        "  } ],",
        '  "costs?": [{"label": "<objective-level cost>",',
        '    "amount": "<decimal string, or null when unknown>"}],',
        '  "proposed_tools?": [{"name": "<missing boundary tool>",',
        '    "need": "<what the agent needs, in words>",',
        '    "spec?": "<how it should behave>",',
        '    "origin_task_id?": "<blocked ticket, when known>"}],',
        '  "playbook?": "<approved playbook this plan follows>",',
        '  "playbook_deviations?": ["<why each step differs>"],',
        '  "proposed_playbook_update?": {"playbook": "<approved name>",',
        '    "change": "<what to change>", "reason": "<why>"},',
        '  "roadmap_remaining": ["<one line per foreseen next step: what, '
        'and what it depends on>"]',
        "}",
        "ROADMAP_REMAINING is your outlook: after choosing this cycle's",
        "tickets, list the next steps you already foresee (at most",
        f"{planner_mod.ROADMAP_MAX_LINES} lines of "
        f"{planner_mod.ROADMAP_MAX_CHARS} characters), one line per step",
        "saying what it depends on. They are speculative notes for the next",
        "cycle, not commitments: rewrite the whole list every cycle and drop",
        "what stopped being valid. A longer list or line is rejected.",
        "Rules: active carries 1-3 NEW tickets plus any completions of",
        "tierless children above (a completion never recreates: it fills",
        "the listed task; blocked_by never names a completion's",
        "local_ref, depend on its task instead). Complete_candidate,",
        "blocked and capability_gap carry NO tickets. complete_candidate",
        "triggers final review, it",
        "never closes the objective directly. Every ticket has one",
        "responsibility and fits one bounded execution cycle. No",
        "duplicate of an open ticket, of a closed child's delivered goal",
        "or of work an objective note reports done. Then emit",
        f"PLANNER_READY in {SCRATCH_DIRNAME}/status.",
    ])
    if policy_text.strip():
        lines.extend(["", "NORMATIVE ROUTING POLICY (authoritative):",
                      planner_mod.policy_prompt_text(policy_text)])
    if policy_index.strip():
        lines.extend(["", policy_index.strip()])
    return knowledge_mod.with_preamble("\n".join(lines), repo, "en")


async def _task_exists(env: ObjectiveEnv, task_id: str) -> bool:
    try:
        return await env.tasks.get_task(task_id) is not None
    except Exception:  # noqa: BLE001
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
        if (run.get("tickets_created", 0) == 0
                and not run.get("contracts_completed", 0)
                and not run.get("complete")):
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


async def record_planner_return(
    objectives: ObjectiveStore,
    objective_id: str,
    *,
    task_id: str = "",
    title: str = "",
    agent: str = "",
    resultado: str = "",
    motivo: str = "",
    necesidad: str = "",
    denials: Sequence[str] = (),
) -> dict[str, Any]:
    """Record an agent verdict returned to the planner (TASK-2026-00202).

    Appends ``{task_id, title, agent, resultado, motivo, necesidad, at}``
    to ``planner_returns``: the prompt shows the latest entries so the
    planner relaunches with guidance, reassigns, splits or returns
    capability_gap. Unlike material failures this feeds no retry rule:
    the single return is enforced by the ``returns_from`` tag on the
    relaunched tickets. ``denials`` are the exact badge refusals of the
    run (TASK-2026-00571). Returns the entry ({} when missing).
    """
    objective = await objectives.get_objective(objective_id)
    if objective is None:
        return {}
    entry: dict[str, Any] = {
        "task_id": str(task_id or ""),
        "title": str(title or "").strip()[:300],
        "agent": str(agent or ""),
        "resultado": str(resultado or ""),
        "motivo": str(motivo or "")[:500],
        "necesidad": str(necesidad or "")[:500],
        "at": _utcnow_iso(),
    }
    if denials:
        entry["denials"] = [str(item)[:200] for item in list(denials)[:10]]
    returns = list(objective.get("planner_returns") or [])
    returns.append(entry)
    await objectives.patch_objective(objective_id, {"planner_returns": returns})
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


def planning_in_flight() -> bool:
    """True while any objective planner runs (a restart would kill it)."""
    return bool(_PLAN_FLIGHTS)


async def plan_objective(
    env: ObjectiveEnv, objective_id: str,
    *,
    return_from: str | None = None,
) -> dict[str, Any]:
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

    ``return_from`` tags this run as an agent-return replan
    (TASK-2026-00202): created tickets carry
    ``params.returns_from`` with the returned ticket id, so a second
    unresolved return parks capability_gap instead of looping. The
    leader's tag wins when triggers coalesce.
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
        result = await _plan_objective_inner(env, objective_id, return_from)
        while _PLAN_FLIGHTS.get(objective_id) is rec \
                and rec.rerun_requested:
            rec.rerun_requested = False
            result = await _plan_objective_inner(env, objective_id, return_from)
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
    return_from: str | None = None,
) -> dict[str, Any]:
    """Run the single global planner once (§9.3). Returns a result summary."""
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    pending = objective.get("pending_plan")
    if isinstance(pending, dict) and pending:
        state, question = await _pending_spend_state(env, pending)
        if state == "wait":
            return {"ok": False, "reason": "spend_approval_pending",
                    "question_task_id": pending.get("question_task_id")}
        if state == "apply":
            return await apply_approved_plan(env, objective_id)
        if state == "denied":
            await _ledger_missing_spend_deny(
                env, objective, objective_id, pending, question)
        await env.objectives.patch_objective(
            objective_id, {"pending_plan": None})
        objective = await env.objectives.get_objective(objective_id) \
            or objective
    cfg = env.objectives_cfg
    planner_cfg = env.planner_cfg
    tickets = await env.tasks.list_by_objective(objective_id)
    active = _brake_active_children(tickets)
    spent = len(_budget_children(tickets))
    if spent >= int(cfg.get("max_tickets", 30)):
        # Cost brake, not an owner decision (TASK-2026-00118): no new
        # tickets are planned, but open work keeps running and the next
        # close re-triggers planning. With nothing open the objective
        # attempts completion instead of stalling.
        if active:
            return {"ok": False, "reason": "max_tickets_reached",
                    "detail": f"{spent} lifetime tickets "
                              f"(max {cfg.get('max_tickets', 30)}); "
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
    policy_path = planner_cfg.get("policy_path")
    policy_core = planner_mod.load_policy_core(policy_path)
    policy_details = planner_mod.load_policy_details(policy_path)
    if not policy_core or policy_details is None:
        return await _objective_park(
            env, objective, "config",
            f"planner routing policy unreadable "
            f"({planner_mod.check_policy(policy_path).problem}); "
            "refusing to plan blind")
    policy_text = planner_mod.compose_policy(
        policy_core, [text for _, text in policy_details])
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
    skills_md, skill_map = _planner_skills()
    playbooks_md, playbook_names = _planner_playbooks()
    recovery = objective.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, Mapping) else {}
    parked_md = ""
    if str(recovery.get("last_reason") or "").strip():
        parked_md = (
            f"Previous park: {recovery['last_reason']}\n"
            f"Recovery attempts: {int(recovery.get('attempts') or 0)}/"
            f"{recovery_mod.recovery_config(getattr(env, 'objectives_cfg', None))['max_attempts']}")
    mission_md = await _objective_mission_section(env, objective)
    scratch = objective_scratch(env.repo, objective_id)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "OBJECTIVE_CAPSULE.md").write_text(
        (objective.get("capsule") or {}).get("text", "") or "(empty)\n",
        encoding="utf-8")
    (scratch / "INBOX.md").write_text(inbox_md or "INBOX\n(none)\n",
                                      encoding="utf-8")
    staged = _stage_policy_details(scratch, policy_details)
    agent_catalog = _planner_catalog()
    agent_staged = _stage_agent_details(scratch, agent_catalog) \
        if agent_catalog is not None else []
    sizing_path = stage_sizing_hint(scratch, str(objective.get("sizing_hint") or ""))
    notes = await objective_notes(env, objective)
    prompt = _planner_prompt(
        objective, (objective.get("capsule") or {}).get("text", ""),
        ready, inbox_md or "INBOX\n(none)\n", deferred,
        str(objective.get("sizing_hint") or ""),
        blocked_tickets=blocked,
        tierless=[item for item in open_tickets
                  if not item.get("coding_tool")],
        agents_md=_agents_section(agents, skills_md, agent_catalog,
                                  dict(agent_staged)),
        policy_text=policy_core,
        policy_index=planner_mod.policy_detail_index(staged),
        handoffs=handoffs,
        roadmap=[str(item) for item in
                 objective.get("roadmap_remaining") or []],
        repo=env.repo,
        routing_guide=routing_guide_section(
            str(objective.get("project") or ""),
            env.project_routing),
        spend_guide=spend_mod.spend_guide_section(env.mandate),
        principles_guide=principles_prompt_section(env.mandate),
        cost_guide=cost_mod.objective_cost_section(
            cost_mod.aggregate_objective_cost(
                [item.get("cost") for item in tickets])),
        playbooks_md=playbooks_md,
        parked_md=parked_md,
        sizing_path=sizing_path,
        mission_md=mission_md,
        closed_children=closed_children_lines(objective, tickets),
        superseded=superseded_returns(objective, tickets),
        notes=notes,
        group_ref=str(objective.get("erp_group_ref") or ""))
    timeout_s = int(planner_cfg.get("timeout_minutes", 15)) * 60
    errors: list[str] = []
    result: planner_mod.PlannerResult | None = None
    # Exclusive per-objective output (TASK-2026-00176): this run reads
    # only its objective's file, so a concurrent cycle of the same repo
    # can neither feed us its plan nor eat ours. Same-objective runs
    # never overlap (plan_objective single-flight), hence dropping the
    # previous cycle's file here is safe.
    result_path = scratch / PLANNER_RESULT_FILE
    base_prompt = prompt
    for _attempt in range(2):
        # Drop any previous cycle output: a run that writes nothing must
        # read as missing, never as the last cycle's valid file.
        try:
            result_path.unlink()
        except OSError:
            pass
        execution = await env.executor.run("objective_planner", prompt, env.repo, timeout_s)
        text = _read(result_path)
        if execution.returncode != 0:
            errors = [f"planner process failed ({execution.returncode}): {execution.output_tail[-500:]}"]
        elif not text:
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
                    skills=skill_map,
                    playbooks=playbook_names,
                )
                if not errors:
                    result = candidate
        if not errors:
            break
        prompt = base_prompt + "\n\n" + _planner_retry_prompt(errors, env.repo)
    try:
        await env.executor.cleanup("objective_planner")
    except Exception:  # noqa: BLE001, S110
        pass
    if errors or result is None:
        return await _objective_park(
            env, objective, "planner_error",
            f"invalid {PLANNER_RESULT_FILE} after retry: "
            f"{'; '.join(errors)}")
    spend = spend_mod.check_plan_spend(
        costs=spend_mod.collect_plan_costs(result),
        summary=f"Plan for objective {objective_id}: "
                f"{result.state_summary[:160]}",
        mandate=env.mandate,
    )
    if spend["escalate"]:
        return await _hold_plan_for_spend(
            env, objective, objective_id, result, spend,
            return_from=return_from, closed_before=closed_before,
            policy_text=policy_text)
    return await _record_and_apply_plan(
        env, objective, objective_id, result, return_from=return_from,
        closed_before=closed_before,
        policy_sha=planner_mod.policy_fingerprint(policy_text))


def pedido_scratch(repo: Path, task_id: str) -> Path:
    """Per-pedido scratch dir: ``repo/.openceo/loose/<task_id>``."""
    return scratch_root(repo) / "loose" / task_id


def _pedido_prompt(
    ticket: Mapping[str, Any],
    *,
    agents_md: str = "",
    policy_text: str = "",
    policy_index: str = "",
    routing_guide: str = "",
    spend_guide: str = "",
    principles_guide: str = "",
    mission_guide: str = "",
    agent_return: Mapping[str, Any] | None = None,
) -> str:
    """Planner prompt for one pedido (self-contained decision contract)."""
    from mechanic.handoff import handoff_prompt

    task_id = str(ticket.get("task_id") or "")
    lines = [
        handoff_prompt(ticket),
        "You are the single OpenCEO planner. Decide this ONE pedido: a "
        "loose ticket with no explicit decision.",
        "",
        f"TASK_ID: {task_id}",
        f"TITLE: {str(ticket.get('title') or '')[:500]}",
        f"PROJECT: {str(ticket.get('project') or '')}",
        f"PRIORITY: {str(ticket.get('priority') or 'medium')}",
        "DESCRIPTION:",
        (str(ticket.get("description") or "")[:6000] or "(empty)"),
    ]
    attachments = ticket.get("attachments") or []
    if attachments:
        names = ", ".join(str(name) for name in attachments[:10])
        lines.extend(["", f"ATTACHMENTS: {names}"])
    if agent_return is not None:
        lines.extend([
            "",
            "AGENT RETURNS (this pedido already ran once and the agent "
            "could not do it; relaunch it with guidance, reassign it, or "
            "convert it — never ignore the return):",
            f"- {agent_return.get('title')} [{agent_return.get('agent')}/"
            f"{agent_return.get('resultado')}]: "
            f"{agent_return.get('motivo')}",
            f"  necesidad: {agent_return.get('necesidad')}",
        ])
    prior_runs = cost_mod.pedido_cost_line(ticket.get("cost"))
    if prior_runs:
        lines.extend(["", prior_runs])
    lines.extend([
        "",
        agents_md.strip(),
        "",
        "Decide exactly one ticket when the pedido fits one execution, "
        "with target_agent, an optional skill suggestion, and — dev only "
        "— the tier with its justification (read the dev detail file "
        "before choosing a tier).",
        "When the pedido outgrows one ticket, return capability_gap with "
        "no tickets: the system converts it to an objective and the "
        "objective planner takes over. blocked and complete_candidate "
        "are not pedido outcomes: a stuck pedido converts, and whether "
        "work is needed is judged by execution, never by declining to "
        "decide.",
        "The decision patches the pedido itself: blocked_by must stay "
        "empty and completes_task_id is forbidden.",
    ])
    if policy_text.strip():
        lines.extend(["", policy_text.strip()])
    if policy_index.strip():
        lines.extend(["", policy_index.strip()])
    lines.extend([
        "",
        "- NEW BOUNDARY TOOL (rare; at most "
        f"{planner_mod.PROPOSED_TOOLS_MAX}): when the pedido needs a "
        "missing boundary tool — a secret the agent must not hold, a "
        "lock that must not depend on the model obeying, or a "
        "business-record write with its own rules — list it in "
        "proposed_tools. The Owner approves through the human channel. "
        "Never propose for technical failures. Omit the key when "
        "nothing is missing.",
    ])
    if (routing_guide or "").strip():
        lines.extend(["", routing_guide.strip()])
    if (spend_guide or "").strip():
        lines.extend(["", spend_guide.strip()])
    if (principles_guide or "").strip():
        lines.extend(["", principles_guide.strip()])
    if (mission_guide or "").strip():
        lines.extend(["", mission_guide.strip()])
    lines.extend([
        "",
        f"OUTPUT: write {SCRATCH_DIRNAME}/loose/"
        f"{task_id}/{PLANNER_RESULT_FILE} "
        "with exactly this JSON",
        "object (no YAML, no markdown fences around the file content):",
        "{",
        '  "objective_status": "active | capability_gap",',
        '  "state_summary": "<the decision in one line>",',
        '  "information_gaps": [{"gap": "<what is unknown>", '
        '"materiality": "low|medium|high|critical", "resolution": '
        '"<how to resolve it>"}] (capability_gap only: why it '
        'outgrows one ticket; materiality is exactly one of low, '
        'medium, high, critical, e.g. "high"),',
        '  "tickets": [ <exactly one, ONLY when active> {',
        '    "local_ref": "pedido-1", "title": "...",',
        '    "target_agent": "<enabled agent>", "work_type": "...",',
        '    "skill?": "<optional suggested skill, any agent>",',
        '    "goal": "...", "why_now": "...",',
        '    "inputs": [], "constraints": [], "out_of_scope": [],',
        '    "acceptance_criteria": ["<at least one>"],',
        '    "expected_artifacts": ["<at least one>"],',
        '    "blocked_by": [],',
        '    "coding_tool": "<REQUIRED for dev: tier_s|tier_a|tier_b|'
        'tier_c; FORBIDDEN otherwise>",',
        *_REVIEW_FIELD_LINES,
        *_EMPTY_DIFF_FIELD_LINES,
        '    "tier_reason": "<REQUIRED for dev: why this tier>",',
        '    "oracle?": {"type": "<oracle type>", "refs": [{"ref": '
        '"..."}]},',
        '    "why_consensus_is_needed?": "<tier_s only>",',
        '    "why_single_session_is_insufficient?": "<tier_s only>",',
        '    "why_not_c?": "<required for tier_a/tier_b>",',
        '    "costs?": [{"label": "<what it buys>", "amount": '
        '"<decimal string, or null when unknown>"}]',
        "  } ],",
        '  "proposed_tools?": [{"name": "<missing boundary tool>",',
        '    "need": "<in words>", "spec?": "<how it should behave>"}],',
        '  "roadmap_remaining": []',
        "}",
    ])
    return "\n".join(lines)


def _pedido_extra_errors(result: planner_mod.PlannerResult) -> list[str]:
    """Pedido-only validation over an already-parsed result."""
    errors: list[str] = []
    if result.objective_status not in {
            planner_mod.ObjectiveStatus.ACTIVE,
            planner_mod.ObjectiveStatus.CAPABILITY_GAP}:
        errors.append(
            "a pedido decides (active, exactly one ticket) or converts "
            "(capability_gap, no tickets); "
            f"{result.objective_status.value} is not a pedido outcome")
    for ticket in result.tickets:
        if ticket.completes_task_id:
            errors.append(
                f"ticket {ticket.local_ref!r}: completes_task_id is "
                "for objective children, never for a pedido")
    return errors


async def plan_pedido(
    env: ObjectiveEnv,
    ticket: Mapping[str, Any],
    *,
    return_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the single planner for one pedido (TASK-2026-00204).

    Same planner function as objectives (``objective_planner`` role,
    one fresh headless run, ``PlannerResult`` validated with Pydantic
    + deterministic rules), over the pedido alone. Single-flight per
    ticket: concurrent triggers share one run.

    Decided: the decision is patched onto the ticket (agent hint,
    target, skill, tier, provenance) and the run returns
    ``{"ok": True, "action": "decided", ...}`` with the spend verdict
    for the caller to hold on. capability_gap: nothing is patched and
    the caller converts (``action: convert``). Invalid twice: attempts
    are counted on the ticket and the caller requests mechanic diagnosis
    (``reason: planner_error``); unreadable policy or an empty
    registry fail fast (``reason: config``).
    """
    task_id = str(ticket.get("task_id") or "")
    flight_key = f"loose:{task_id}"
    flight = _PLAN_FLIGHTS.get(flight_key)
    if flight is not None and not flight.task.done():
        if flight.task is asyncio.current_task():
            return await _plan_pedido_inner(env, ticket, return_from)
        flight.rerun_requested = True
        return await flight.task

    rec: _PlanFlight | None = None

    async def _leader() -> dict[str, Any]:
        assert rec is not None
        result = await _plan_pedido_inner(env, ticket, return_from)
        while _PLAN_FLIGHTS.get(flight_key) is rec \
                and rec.rerun_requested:
            rec.rerun_requested = False
            result = await _plan_pedido_inner(env, ticket, return_from)
        return result

    task = asyncio.create_task(_leader())
    rec = _PlanFlight(task=task)
    _PLAN_FLIGHTS[flight_key] = rec
    try:
        return await task
    finally:
        if _PLAN_FLIGHTS.get(flight_key) is rec:
            del _PLAN_FLIGHTS[flight_key]


async def _objective_mission_section(
    env: ObjectiveEnv, objective: Mapping[str, Any],
) -> str:
    """The Owner's mission brief for a mission-linked objective, or ""."""
    mission_id = str(objective.get("mission_id") or "").strip()
    store = getattr(env, "missions", None)
    if not mission_id or store is None:
        return ""
    try:
        mission = await store.get_mission(mission_id)
    except Exception:  # noqa: BLE001
        logger.warning("planner: mission %s unreadable", mission_id,
                       exc_info=True)
        return ""
    if mission is None:
        return ""
    from objectives import missions as missions_mod
    return "\n".join([
        f"MISSION {mission_id} (the Owner's outcome this objective serves; "
        "every ticket stays consistent with its goal and acceptance):",
        *missions_mod.mission_brief_lines(mission),
    ])


async def _mission_guide_section(
    env: ObjectiveEnv, ticket: Mapping[str, Any],
) -> str:
    """MISSION context for a mission-linked pedido ("" when none)."""
    store = getattr(env, "missions", None)
    if store is None:
        return ""
    params = ticket.get("params")
    mission_id = ""
    if isinstance(params, Mapping):
        mission_id = str(params.get("mission_id") or "").strip()
    if not mission_id:
        return ""
    try:
        mission = await store.get_mission(mission_id)
    except Exception:  # noqa: BLE001
        return ""
    if mission is None:
        return ""
    from objectives import missions as missions_mod
    ledger = [item for item in mission.get("objectives") or []
              if isinstance(item, Mapping)]
    states = ", ".join(
        f"{item.get('objective_id')}={item.get('status')}"
        for item in ledger) or "(first objective)"
    lines = [
        f"MISSION {mission_id}",
        *missions_mod.mission_brief_lines(mission),
        f"Objectives so far: {states}",
        "This pedido continues the mission as its next objective: it "
        "outgrows one ticket by definition, so return capability_gap "
        "and the system converts it (linked, under the mission group).",
    ]
    return "\n".join(lines)


async def _plan_pedido_inner(
    env: ObjectiveEnv,
    ticket: Mapping[str, Any],
    return_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the single planner for one pedido once. Returns the outcome."""
    task_id = str(ticket.get("task_id") or "")
    planner_cfg = env.planner_cfg
    agents = planner_cfg.get("agents", {}) or {}
    if not agents:
        return {"ok": False, "reason": "config",
                "detail": "planner agent registry is empty "
                          "(global objective_planner.agents)"}
    policy_path = planner_cfg.get("policy_path")
    policy_core = planner_mod.load_policy_core(policy_path)
    policy_details = planner_mod.load_policy_details(policy_path)
    if not policy_core or policy_details is None:
        return {"ok": False, "reason": "config",
                "detail": "planner routing policy unreadable "
                f"({planner_mod.check_policy(policy_path).problem}); "
                "refusing to plan blind"}
    policy_text = planner_mod.compose_policy(
        policy_core, [text for _, text in policy_details])
    skills_md, skill_map = _planner_skills()
    scratch = pedido_scratch(env.repo, task_id)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "PEDIDO.md").write_text(
        f"# {ticket.get('title')}\n\n"
        f"{str(ticket.get('description') or '')[:6000] or '(empty)'}\n",
        encoding="utf-8")
    staged = _stage_policy_details(scratch, policy_details)
    agent_catalog = _planner_catalog()
    agent_staged = _stage_agent_details(scratch, agent_catalog) \
        if agent_catalog is not None else []
    project = str(ticket.get("project") or "")
    prompt = _pedido_prompt(
        ticket,
        agents_md=_agents_section(agents, skills_md, agent_catalog,
                                  dict(agent_staged)),
        policy_text=policy_core,
        policy_index=planner_mod.policy_detail_index(staged),
        routing_guide=routing_guide_section(project, env.project_routing),
        spend_guide=spend_mod.spend_guide_section(env.mandate),
        principles_guide=principles_prompt_section(env.mandate),
        mission_guide=await _mission_guide_section(env, ticket),
        agent_return=return_from)
    timeout_s = int(planner_cfg.get("timeout_minutes", 15)) * 60
    errors: list[str] = []
    result: planner_mod.PlannerResult | None = None
    result_path = scratch / PLANNER_RESULT_FILE
    base_prompt = prompt
    last_invalid_text = ""
    for _attempt in range(2):
        try:
            result_path.unlink()
        except OSError:
            pass
        execution = await env.executor.run("objective_planner", prompt, env.repo, timeout_s)
        text = _read(result_path)
        if execution.returncode != 0:
            errors = [f"planner process failed ({execution.returncode}): {execution.output_tail[-500:]}"]
        elif not text:
            errors = [f"no {PLANNER_RESULT_FILE}"]
        else:
            last_invalid_text = text
            try:
                candidate = planner_mod.parse_planner_result(text)
            except ValueError as exc:
                errors = [str(exc)]
            else:
                errors = await planner_mod.validate_planner_result(
                    candidate,
                    agents=agents,
                    open_titles=[],
                    task_exists=_pedido_no_tasks,
                    material_failures=[],
                    horizon=[1, 1],
                    skills=skill_map,
                    playbooks=_planner_playbooks()[1],
                )
                errors.extend(_pedido_extra_errors(candidate))
                if not errors:
                    result = candidate
        if not errors:
            break
        # Retry with the full context: each executor run is a fresh
        # process, so an errors-only prompt could never succeed.
        prompt = (base_prompt + "\n\n"
                  f"Your previous {PLANNER_RESULT_FILE} was invalid: "
                  f"{'; '.join(errors)}\n"
                  "Fix it and rewrite the same OUTPUT file.")
    try:
        await env.executor.cleanup("objective_planner")
    except Exception:  # noqa: BLE001, S110
        pass
    if errors or result is None:
        params = dict(ticket.get("params") or {})
        params["planner_attempts"] = int(params.get("planner_attempts") or 0) + 1
        if last_invalid_text.strip():
            params["planner_last_invalid"] = last_invalid_text[:2000]
            try:
                result_path.parent.mkdir(parents=True, exist_ok=True)
                result_path.write_text(last_invalid_text, encoding="utf-8")
            except OSError:
                pass
        await env.patch_task(task_id, {"params": params})
        return {"ok": False, "reason": "planner_error",
                "detail": f"invalid {PLANNER_RESULT_FILE} after retry: "
                          f"{'; '.join(errors)}"}
    await create_tool_proposal_questions(
        env, f"loose:{task_id}", project, result)
    await create_playbook_update_questions(
        env, f"loose:{task_id}", project, result)
    if result.objective_status != planner_mod.ObjectiveStatus.ACTIVE:
        return {"ok": True, "action": "convert",
                "goal": str(ticket.get("title") or ""),
                "acceptance": [result.state_summary[:500]],
                "state_summary": result.state_summary,
                "costs": spend_mod.collect_plan_costs(result)}
    planned = result.tickets[0]
    params = dict(ticket.get("params") or {})
    params.update(_planned_params(planned))
    params["decided_by"] = "planner"
    params["planner_policy_sha"] = planner_mod.policy_fingerprint(policy_text)
    params.pop("planner_attempts", None)
    params.pop("planner_last_invalid", None)
    params.pop("agent_result", None)
    if return_from is not None:
        params["returns_from"] = task_id
    updates: dict[str, Any] = {
        "agent_hint": planned.target_agent,
        "params": params,
    }
    if planned.coding_tool is not None:
        updates["coding_tool"] = planned.coding_tool.value
    elif ticket.get("coding_tool"):
        # A re-decision away from dev clears the stale tier: the gate
        # forces DEV on any coding_tool. Fresh pedidos stay tier-free.
        updates["coding_tool"] = None
    # A switch the user set (no planner review_reason on record) outranks
    # the planner's own risk call (TASK-2026-00492).
    user_review = (ticket.get("review") is not None
                   and not (ticket.get("params") or {}).get("review_reason"))
    if planned.review is not None and planned.coding_tool is not None \
            and user_review:
        params.pop("review_reason", None)
    elif planned.review is not None:
        updates["review"] = planned.review
        if not planned.review_reason:
            params.pop("review_reason", None)
    elif planned.coding_tool is None and ticket.get("review"):
        # Same rule as the tier above: a re-decision away from dev
        # clears the stale switch; a dev decision that omits it
        # preserves the ticket's explicit instruction.
        updates["review"] = None
    if planned.coding_tool is None:
        for key in ("tier_reason", "tier_assigned_by", "tier_signals",
                    "oracle", "why_consensus_is_needed",
                    "why_single_session_is_insufficient", "why_not_c",
                    "material_failure", "review_reason"):
            params.pop(key, None)
    await env.patch_task(task_id, updates)
    spend = spend_mod.check_plan_spend(
        costs=spend_mod.collect_plan_costs(result),
        summary=f"Plan for pedido {task_id}: {result.state_summary[:160]}",
        mandate=env.mandate)
    if env.notify is not None:
        tier = planned.coding_tool.value if planned.coding_tool else "-"
        try:
            await env.notify(
                task_id,
                f"Planner decided: agent {planned.target_agent}, "
                f"skill {planned.skill or '-'}, tier {tier}. "
                f"{planned.tier_reason or ''} "
                f"(policy {params['planner_policy_sha']})".strip())
        except Exception:
            logger.warning("pedido decision comment failed for %s",
                           task_id, exc_info=True)
    return {"ok": True, "action": "decided",
            "decision": {"target_agent": planned.target_agent,
                         "skill": planned.skill,
                         "coding_tool": planned.coding_tool.value
                         if planned.coding_tool else None,
                         "tier_reason": planned.tier_reason,
                         "costs": spend_mod.collect_plan_costs(result)},
            "spend": spend}


async def _pedido_no_tasks(task_id: str) -> bool:
    """A pedido decision carries no dependencies: every ref is unknown."""
    del task_id
    return False


async def _record_and_apply_plan(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    objective_id: str,
    result: planner_mod.PlannerResult,
    *,
    return_from: str | None = None,
    closed_before: int | None = None,
    policy_sha: str | None = None,
    run_tag: str | None = None,
) -> dict[str, Any]:
    """Record a valid plan and apply it: completions, tickets, tools.

    Shared by the planning cycle and by the spend-approval apply path
    (TASK-2026-00207): an approved plan launches exactly what the
    planner decided, with the run entry describing the run that
    decided it. ``closed_before``/``policy_sha``/``run_tag`` ride
    along from the held cycle; the live cycle computes them.
    """
    runs = list(objective.get("planning_runs") or [])
    status = result.objective_status.value
    if closed_before is None:
        tickets = await env.tasks.list_by_objective(objective_id)
        closed_before = sum(
            1 for item in tickets
            if str(item.get("status")) in TERMINAL_TICKET_STATUS)
    if policy_sha is None:
        policy_sha = planner_mod.policy_fingerprint(
            planner_mod.load_policy_text(
                env.planner_cfg.get("policy_path")))
    runs.append({"at": _utcnow_iso(), "status": status,
                 # Which policy text this decision saw (TASK-2026-00184).
                 "policy_sha": policy_sha,
                 "tickets_created": len(
                     [item for item in result.tickets
                      if not item.completes_task_id]),
                 "contracts_completed": len(
                     [item for item in result.tickets
                      if item.completes_task_id]),
                 "complete": status == "complete_candidate"})
    await env.objectives.patch_objective(objective_id, {
        "planning_runs": runs,
        "roadmap_remaining": list(result.roadmap_remaining),
        "_closed_at_last_plan": closed_before,
        "sizing_hint": "",
        "planner_telemetry": _telemetry_after_run(objective, runs),
    })
    # Owner questions ride on every outcome (#615): a blocked or
    # capability_gap cycle parks below, and a proposal that never reaches
    # the Owner leaves a park with no trigger. The loose path already asks
    # before dispatching; idempotent per source_id, answered skipped.
    await create_tool_proposal_questions(
        env, objective_id, str(objective.get("project") or ""), result)
    await create_playbook_update_questions(
        env, objective_id, str(objective.get("project") or ""), result)
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
    completed = await _apply_contract_completions(env, objective_id, result)
    created = await _create_planned_tickets(
        env, objective, result, return_from, run_tag=run_tag)
    await _apply_pending_rewires(
        env, objective_id, [item["task_id"] for item in created])
    tele = dict((await env.objectives.get_objective(objective_id) or {}
                 ).get("planner_telemetry") or {})
    tele["tickets_created"] = int(tele.get("tickets_created", 0)) + len(created)
    tele["contracts_completed"] = int(tele.get("contracts_completed", 0)) \
        + len(completed)
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
            "task_ids": [item["task_id"] for item in created],
            "contracts_completed": completed}


async def create_spend_approval_question(
    env: ObjectiveEnv,
    objective_id: str,
    project: str,
    spend: Mapping[str, Any],
    state_summary: str,
    run_tag: str,
) -> str | None:
    """Open the Owner question for a spend-held plan (TASK-2026-00207).

    Kind ``question``, no origin ticket: the approval launches the
    stored plan, the denial returns the motive to the planner. Returns
    the question task id, None when the ticket service is unbound.
    """
    total = spend_mod.spend_total_text(spend)
    spec = build_owner_question(
        kind="question",
        question=f"Autorizar el gasto declarado del plan ({total})?",
        human_action=str(spend.get("human_action") or "Autorizar o denegar"),
        checkpoint=spend_mod.spend_hold_checkpoint(objective_id),
        context=f"Plan: {state_summary[:300]} Motivo: "
                f"{spend.get('reason')}",
        project=project,
        source_id=f"spend-plan:{objective_id}:{run_tag}",
    )
    created = await env.create_ticket({
        "title": spec["title"],
        "description": spec["description"],
        "project": spec["project"],
        "source": spec["source"],
        "source_id": spec["source_id"],
        "params": spec["params"],
        "priority": spec["priority"],
        "tags": spec["tags"],
        "autorun": False,
    })
    if not created:
        return None
    if question_answered(created):
        return None
    question_id = str(created.get("task_id") or "")
    if not question_id:
        return None
    await env.patch_task(question_id, {
        "status": "waiting",
        "phase": spec["phase"],
        "waiting_reason": spec["waiting_reason"],
        "workflow_key": None,
        "workflow_phases": [],
        "phase_index": 0,
        "phase_assignments": {},
    })
    return question_id


async def _hold_plan_for_spend(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    objective_id: str,
    result: planner_mod.PlannerResult,
    spend: Mapping[str, Any],
    *,
    return_from: str | None,
    closed_before: int,
    policy_text: str,
) -> dict[str, Any]:
    """Store a valid plan and wait for the Owner's spend approval."""
    run_tag = uuid.uuid4().hex[:8]
    question_id = await create_spend_approval_question(
        env, objective_id, str(objective.get("project") or ""), spend,
        result.state_summary, run_tag)
    if not question_id:
        return {"ok": False, "reason": "spend_approval_unasked",
                "detail": "the owner question could not be created; "
                          "the plan launches on no path without approval"}
    await env.objectives.patch_objective(objective_id, {
        "pending_plan": {
            "result": result.model_dump(mode="json"),
            "return_from": return_from,
            "closed_before": closed_before,
            "policy_sha": planner_mod.policy_fingerprint(policy_text),
            "run_tag": run_tag,
            "spend": {
                "amount": str(spend.get("amount") or "0"),
                "currency": str(spend.get("currency") or "?"),
                "category": spend.get("category"),
                "reason": str(spend.get("reason") or ""),
                "unknown": [str(item) for item in spend.get("unknown") or []],
            },
            "question_task_id": question_id,
            "held_at": _utcnow_iso(),
        },
    })
    return {"ok": False, "reason": "spend_approval_pending",
            "question_task_id": question_id,
            "amount": str(spend.get("amount") or "0"),
            "currency": str(spend.get("currency") or "?")}


async def _pending_spend_state(
    env: ObjectiveEnv, pending: Mapping[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Held-plan question state: wait, apply, denied or gone.

    A read failure is not a missing question: the hold stays for the
    next trigger instead of discarding the plan (and a given approval)
    on a transient store error.
    """
    question_id = str(pending.get("question_task_id") or "")
    if not question_id:
        return "gone", None
    try:
        question = await env.tasks.get_task(question_id)
    except Exception:  # noqa: BLE001
        logger.warning("spend-hold question %s unreadable; keeping the hold",
                       question_id)
        return "wait", None
    if question is None:
        return "gone", None
    if not question_answered(question):
        return "wait", question
    decision = (owner_question_block(question).get("decision") or {}
                ).get("decision")
    if decision == "approved":
        return "apply", question
    return "denied", question


async def _ledger_missing_spend_deny(
    env: ObjectiveEnv, objective: Mapping[str, Any], objective_id: str,
    pending: Mapping[str, Any], question: Mapping[str, Any] | None,
) -> None:
    """Record a denial motive the planner has not seen yet.

    The normal deny path records through ``plan_return``; this covers
    the crash window (decision recorded, return lost): the next cycle
    ledgers the motive so its planner still receives it, once.
    """
    question_id = str(pending.get("question_task_id") or "")
    seen = [str(entry.get("task_id") or "")
            for entry in (objective.get("planner_returns") or [])
            if isinstance(entry, Mapping)]
    if question_id in seen:
        return
    block = owner_question_block(question)
    record = block.get("decision") if isinstance(
        block.get("decision"), Mapping) else {}
    motive = str(record.get("comment") or "").strip() \
        or str(block.get("question") or "")
    await record_planner_return(
        env.objectives, objective_id,
        task_id=question_id,
        title=str((question or {}).get("title") or "")[:300],
        agent="owner",
        resultado="bloqueado",
        motivo=motive,
        necesidad="")


async def apply_approved_plan(
    env: ObjectiveEnv, objective_id: str,
) -> dict[str, Any]:
    """Launch a spend-held plan after Owner approval (TASK-2026-00207).

    Re-validates the stored plan and the admission limits: a stale
    plan or a non-active objective drops the hold, a full objective
    keeps it for the next trigger. A retried apply reuses the held
    idempotency keys, so it never duplicates tickets.
    """
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    pending = objective.get("pending_plan")
    if not isinstance(pending, dict) or not pending:
        return {"ok": False, "reason": "no-pending-plan"}
    question_id = str(pending.get("question_task_id") or "")
    if str(objective.get("status") or "") != "active":
        await env.objectives.patch_objective(
            objective_id, {"pending_plan": None})
        if env.notify is not None and question_id:
            await env.notify(
                question_id,
                "Plan aprobado pero el objetivo ya no esta activo: "
                "no se lanza.")
        return {"ok": False, "reason": "objective-not-active"}
    try:
        result = planner_mod.PlannerResult.model_validate(
            pending.get("result") or {})
    except ValueError as exc:
        await env.objectives.patch_objective(
            objective_id, {"pending_plan": None})
        return {"ok": False, "reason": "plan-stale",
                "detail": str(exc)[:300]}
    cfg = env.objectives_cfg
    tickets = await env.tasks.list_by_objective(objective_id)
    active = _brake_active_children(tickets)
    if len(_budget_children(tickets)) >= int(cfg.get("max_tickets", 30)):
        await env.objectives.patch_objective(
            objective_id, {"pending_plan": None})
        return {"ok": False, "reason": "max_tickets_reached",
                "detail": "the held plan no longer fits the ticket budget"}
    if len(active) >= int(cfg.get("max_active_tickets_per_objective", 6)):
        return {"ok": False, "reason": "objective-full",
                "detail": "the hold stays for the next trigger"}
    open_titles = [str(item.get("title") or "") for item in tickets
                   if str(item.get("status")) not in TERMINAL_TICKET_STATUS]
    _, skill_map = _planner_skills()
    errors = await planner_mod.validate_planner_result(
        result,
        agents=env.planner_cfg.get("agents", {}) or {},
        open_titles=open_titles,
        task_exists=lambda dep: _task_exists(env, dep),
        material_failures=(objective.get("material_failures") or []),
        horizon=env.planner_cfg.get("ticket_horizon"),
        skills=skill_map,
        playbooks=_planner_playbooks()[1],
    )
    if errors:
        await env.objectives.patch_objective(
            objective_id, {"pending_plan": None})
        if env.notify is not None and question_id:
            await env.notify(
                question_id,
                "El plan retenido quedo obsoleto y no se lanza: "
                f"{'; '.join(errors)[:300]}")
        return {"ok": False, "reason": "plan-stale",
                "detail": "; ".join(errors)[:300]}
    outcome = await _record_and_apply_plan(
        env, objective, objective_id, result,
        return_from=pending.get("return_from"),
        closed_before=pending.get("closed_before"),
        policy_sha=pending.get("policy_sha"),
        run_tag=pending.get("run_tag"),
    )
    await env.objectives.patch_objective(
        objective_id, {"pending_plan": None})
    if env.notify is not None and question_id and outcome.get("ok"):
        await env.notify(
            question_id,
            f"Gasto aprobado: plan lanzado "
            f"({outcome.get('tickets_created', 0)} ticket(s)).")
    outcome["spend_approved"] = True
    return outcome


def _wake_recovery(env: ObjectiveEnv) -> None:
    wake = getattr(env, "recovery_wake", None) or RECOVERY_WAKE
    if wake is not None:
        try:
            wake()
        except Exception:  # noqa: BLE001 - a wake never breaks the park
            logger.warning("recovery wake failed", exc_info=True)


async def _await_recovery_deploy(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    ticket: Mapping[str, Any],
    close: "TicketCloseContext | None",
) -> dict[str, Any]:
    """Hold the objective until its merged recovery fix is live (00483).

    Replanning now would run the mission on the code that parked it; the
    recovery timer restarts the idle service and resumes it after boot.
    """
    objective_id = str(objective.get("objective_id"))
    if close is not None:
        await _absorb_close(env, objective, close)
        objective = await env.objectives.get_objective(objective_id) \
            or objective
    await _recompile_capsule(env, objective)
    ticket_id = str(ticket.get("task_id") or "")
    await env.objectives.patch_objective(
        objective_id, recovery_mod.deploy_park_patch(
            objective, ticket_id, getattr(env, "objectives_cfg", None)))
    _wake_recovery(env)
    if env.notify is not None:
        await env.notify(
            _notify_ref(objective),
            f"Objective {objective_id}: recovery fix {ticket_id} merged; "
            "it resumes once the service restarts with the fix live.")
    return {"ok": True, "action": "await-deploy", "ticket": ticket_id}


async def _objective_park(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    reason: str,
    detail: str,
) -> dict[str, Any]:
    patch: dict[str, Any] = {
        "status": "waiting", "waiting_reason": f"{reason}: {detail}"}
    recovery = recovery_mod.recovery_park_patch(
        objective, reason, detail, getattr(env, "objectives_cfg", None))
    if recovery is not None:
        patch.update(recovery)
    await env.objectives.patch_objective(
        str(objective.get("objective_id")), patch)
    _wake_recovery(env)
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
        except Exception:  # noqa: BLE001, S110
            pass
    return {"ok": False, "reason": reason, "detail": detail}


async def park_return_gap(
    env: ObjectiveEnv,
    objective_id: str,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    """Park the objective: a return replan stayed unresolved (00202).

    Same ``capability_gap`` shape the planner-declared and evidence
    parks use, so the CEO review reads it as one signal; the Owner is
    reached only through the mandate, never from here.
    """
    title = str(entry.get("title") or "").strip() or "(untitled)"
    agent = str(entry.get("agent") or "").strip() or "?"
    resultado = str(entry.get("resultado") or "").strip() or "?"
    motivo = str(entry.get("motivo") or "").strip()
    necesidad = str(entry.get("necesidad") or "").strip()
    summary = (f"agent return unresolved after one replan: {title} "
               f"[{agent}/{resultado}]: {motivo}")
    if necesidad:
        summary += f" Necesidad: {necesidad}"
    await env.objectives.patch_objective(objective_id, {
        "capability_gap": {
            "declared_at": _utcnow_iso(),
            "summary": summary[:2000],
            "gaps": [{
                "task_id": str(entry.get("task_id") or ""),
                "agent": agent,
                "resultado": resultado,
                "motivo": motivo[:500],
                "necesidad": necesidad[:500],
            }],
        },
    })
    objective = await env.objectives.get_objective(objective_id) or {
        "objective_id": objective_id}
    return await _objective_park(
        env, objective, "capability_gap", summary[:300])


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
    if ticket.allow_empty_diff:
        sections.extend(
            ["", "EMPTY DIFF ALLOWED: zero changes are a valid success "
             "when the verification confirms nothing must change; close "
             "with hecho and the decision recorded in evidencia."])
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
    if ticket.skill:
        params["skill"] = ticket.skill
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
    if ticket.review_reason:
        params["review_reason"] = ticket.review_reason
    if ticket.allow_empty_diff:
        params["allow_empty_diff"] = True
    return params


async def _apply_contract_completions(
    env: ObjectiveEnv,
    objective_id: str,
    result: planner_mod.PlannerResult,
) -> list[str]:
    """Fill tierless linked children from completion entries (00174).

    Adoption, not creation: the stub keeps its task_id, promotion
    params and provenance; only the planner-authored contract lands
    (tier, routing hint, acceptance, machine params merged over the
    existing ones). A target that is missing, foreign, terminal,
    already tiered or held for another reason is skipped with a
    warning — suitability can change between prompt and patch, so a
    stale directive never fails the plan. A routing hold healed here
    refloats the ticket; the dispatcher is woken once afterwards.
    """
    completed: list[str] = []
    for planned in result.tickets:
        task_id = planned.completes_task_id or ""
        if not task_id:
            continue
        try:
            current = await env.tasks.get_task(task_id)
        except Exception:  # noqa: BLE001
            logger.warning("completion target %s unreadable; skipping",
                           task_id)
            continue
        if current is None:
            logger.warning("completion target %s gone; skipping", task_id)
            continue
        if str(current.get("objective_id") or "") != objective_id:
            logger.warning("completion target %s is not ours; skipping",
                           task_id)
            continue
        if str(current.get("status")) in TERMINAL_TICKET_STATUS:
            logger.warning("completion target %s already terminal; skipping",
                           task_id)
            continue
        if current.get("coding_tool"):
            continue  # already contracted: idempotent re-drive
        params = dict(current.get("params") or {})
        params.update(_planned_params(planned))
        updates: dict[str, Any] = {
            "agent_hint": planned.target_agent,
            "acceptance": list(planned.acceptance_criteria),
            "params": params,
        }
        if planned.coding_tool is not None:
            updates["coding_tool"] = planned.coding_tool.value
        if planned.review is not None:
            updates["review"] = planned.review
        if str(current.get("waiting_reason") or "").startswith(
                "routing_missing"):
            updates["status"] = "queued"
            updates["waiting_reason"] = None
            updates["resume_at"] = None
        await env.patch_task(task_id, updates)
        completed.append(task_id)
    if completed and env.dispatcher_wake is not None:
        try:
            env.dispatcher_wake()
        except Exception:
            logger.warning("dispatcher wake after completion failed",
                           exc_info=True)
    return completed


async def _park_owner_questions(
    env: ObjectiveEnv, specs: list[dict[str, Any]],
) -> list[str]:
    """Create owner-question tickets and park them waiting (shared).

    Idempotent per ``source_id``: a re-proposed item hits the same key
    and is skipped when already answered, so a second cycle never
    resurrects a decided question. Returns the parked question ids.
    """
    task_ids: list[str] = []
    for spec in specs:
        created = await env.create_ticket({
            "title": spec["title"],
            "description": spec["description"],
            "project": spec["project"],
            "source": spec["source"],
            "source_id": spec["source_id"],
            "params": spec["params"],
            "priority": spec["priority"],
            "tags": spec["tags"],
            "autorun": False,
        })
        if not created:
            continue
        if question_answered(created):
            continue
        question_id = str(created.get("task_id") or "")
        if not question_id:
            continue
        await env.patch_task(question_id, {
            "status": "waiting",
            "phase": spec["phase"],
            "waiting_reason": spec["waiting_reason"],
            # H4 (revisión 00206): same phase cleanup as
            # TaskService.create_owner_question.
            "workflow_key": None,
            "workflow_phases": [],
            "phase_index": 0,
            "phase_assignments": {},
        })
        task_ids.append(question_id)
    return task_ids


async def create_tool_proposal_questions(
    env: ObjectiveEnv,
    objective_id: str,
    project: str,
    result: planner_mod.PlannerResult,
) -> list[str]:
    """Open one owner question per proposed boundary tool (TASK-2026-00206)."""
    specs = []
    for proposal in result.proposed_tools or []:
        source_id = tool_proposal_source_id(objective_id, proposal.name)
        specs.append(build_owner_question(
            kind="new_tool",
            question=f"Crear la tool de frontera {proposal.name}?",
            human_action="Aprobar la tool nueva o rechazarla",
            origin_task_id=proposal.origin_task_id,
            context=f"Necesidad: {proposal.need}"
            + (f" Especificacion: {proposal.spec}" if proposal.spec else ""),
            tool={
                "name": proposal.name,
                "need": proposal.need,
                "spec": proposal.spec,
            },
            project=project,
            source_id=source_id,
        ))
    return await _park_owner_questions(env, specs)


async def create_playbook_update_questions(
    env: ObjectiveEnv,
    objective_id: str,
    project: str,
    result: planner_mod.PlannerResult,
) -> list[str]:
    """Open one owner question for a proposed playbook change (00217)."""
    update = result.proposed_playbook_update
    if update is None:
        return []
    spec = build_owner_question(
        kind="playbook",
        question=f"Actualizar el playbook {update.playbook}?",
        human_action="Aprobar el cambio del playbook o rechazarlo",
        context=f"Cambio: {update.change} Motivo: {update.reason}",
        playbook={
            "update": update.playbook,
            "change": update.change,
            "reason": update.reason,
        },
        project=project,
        source_id=playbooks_mod.update_source_id(
            update.playbook, objective_id),
    )
    return await _park_owner_questions(env, [spec])


async def _create_planned_tickets(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    result: planner_mod.PlannerResult,
    return_from: str | None = None,
    *,
    run_tag: str | None = None,
) -> list[dict[str, Any]]:
    """Create every validated ticket in topological order (§9.3).

    On a return replan, created tickets carry ``params.returns_from``
    with the returned ticket id (TASK-2026-00202 single-return limit).
    A spend-approved plan reuses its held ``run_tag`` (TASK-2026-00207)
    so a retried apply hits the same idempotency keys.
    """
    tickets = [item for item in result.tickets
               if not item.completes_task_id]
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
    run_tag = run_tag or uuid.uuid4().hex[:8]
    group = str(objective.get("erp_group_ref") or "").strip()
    # The objective's own shell completes only after its children: a child
    # waiting on it would never run (TASK-2026-00492).
    shell = str(objective.get("conversion_source_task_id") or "")
    for key in order:
        item = by_key[key]
        blocked = [key_to_task.get(dep, dep) for dep in item.blocked_by
                   if key_to_task.get(dep, dep) != shell]
        params = _planned_params(item)
        if return_from:
            # Single-return limit: this ticket IS the return's resolution
            # attempt; an unresolved verdict on it parks capability_gap.
            params["returns_from"] = return_from
        payload = {
            "title": item.title,
            "description": _planned_description(item),
            "acceptance": list(item.acceptance_criteria),
            "coding_tool": (item.coding_tool.value
                            if item.coding_tool is not None else None),
            "review": item.review,
            "agent_hint": item.target_agent,
            "params": params,
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
                except Exception:  # noqa: BLE001, S112
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
    evidencia: str = ""
    # A person closed it in the business (Owner session or ERP desk).
    business: bool = False


def _park_resumes_on_close(
    objective: Mapping[str, Any],
    close: TicketCloseContext | None = None,
) -> bool:
    """Which waiting parks a completed child close resumes (00430, #606).

    capability_gap parks always resume: the completion is terminal
    evidence the park never considered. Other parks keep their own
    trigger (owner answer, ERP fix, spend approval, boot wedge check,
    recovery retry) — except a blocked park whose recovery budget is
    spent: its retry trigger is gone, boot leaves it, and a free-text
    Owner wait (prod flag, restart, ERP paste) emits no event the
    engine sees, so without the close it strands even after the Owner
    acted (#606: OBJ-8E2257EF). A child a person closed in the business
    is that Owner event, so it resumes any blocked park
    (TASK-2026-00571). Each close fires once, so a resume consumes a
    distinct completion and the loop stays bounded.
    """
    if objective.get("status") != "waiting":
        return False
    waiting_reason = str(objective.get("waiting_reason") or "")
    if waiting_reason.startswith("capability_gap"):
        return True
    if waiting_reason.partition(":")[0].strip() != "blocked":
        return False
    if close is not None and close.business:
        return True
    recovery = objective.get("recovery")
    return isinstance(recovery, Mapping) and recovery.get("exhausted") is True


async def on_ticket_closed(
    env: ObjectiveEnv,
    ticket: Mapping[str, Any],
    close: TicketCloseContext | None = None,
) -> dict[str, Any]:
    """Triggers + capsule recompile after an objective ticket closes (§9.4).

    A close under a capability_gap park resumes it: the completion is
    terminal evidence the park never considered (TASK-2026-00430), as
    does a close under a blocked park whose recovery budget is spent
    (#606). Never raises: trigger failures are notified, never fatal.
    """
    objective_id = str(ticket.get("objective_id") or "")
    if not objective_id:
        return {"ok": True, "action": "no-objective"}
    try:
        objective = await env.objectives.get_objective(objective_id)
        if objective is None:
            return {"ok": True, "action": "objective-not-active"}
        is_recovery_ticket = str(
            (ticket.get("params") or {}).get("recovery_for") or "") == objective_id
        if is_recovery_ticket:
            if objective.get("status") not in ("waiting", "active"):
                # A closed/cancelled objective is never resurrected.
                return {"ok": True, "action": "objective-not-active"}
            merged = list(close.files) if close is not None else []
            if not merged:
                merged = list((ticket.get("params") or {}).get(
                    "modified_files") or [])
            if close is not None:
                # Its files are OpenCEO's, not the mission's key files.
                close = replace(close, files=[])
            if merged and recovery_mod.recovery_config(getattr(
                    env, "objectives_cfg", None))["deploy_on_close"]:
                return await _await_recovery_deploy(
                    env, objective, ticket, close)
        if objective.get("status") != "active":
            if not (_park_resumes_on_close(objective, close)
                    or is_recovery_ticket):
                if close is not None and objective.get("status") == "waiting":
                    # A park never drops memory (TASK-2026-00571): the
                    # capsule and the final review read this close later.
                    await _absorb_close(env, objective, close)
                    await _recompile_capsule(
                        env, await env.objectives.get_objective(objective_id)
                        or objective)
                return {"ok": True, "action": "objective-not-active"}
            await env.objectives.patch_objective(objective_id, {
                "status": "active", "waiting_reason": None})
            if env.notify is not None:
                await env.notify(
                    _notify_ref(objective),
                    f"Objective {objective_id} resumed: child evidence "
                    "arrived while parked ("
                    f"{'recovery ticket' if is_recovery_ticket else recovery_mod.park_reason(objective)}).")
            objective = await env.objectives.get_objective(objective_id) \
                or objective
        # A child close (including a recovery ticket) is progress: the next
        # park starts a fresh recovery budget (TASK-2026-00481).
        previous_recovery = objective.get("recovery")
        if isinstance(previous_recovery, Mapping) and previous_recovery:
            await env.objectives.patch_objective(objective_id, {
                "recovery": {
                    **dict(previous_recovery),
                    "attempts": 0, "due_at": None, "exhausted": False,
                    "last_reason": None,
                }})
            objective = await env.objectives.get_objective(objective_id) \
                or objective
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
    except Exception as exc:  # noqa: BLE001
        if env.notify is not None:
            await env.notify(objective_id, f"objective trigger failed: {exc}")
        return {"ok": False, "reason": str(exc)[:300]}


#: Blocker states that release a dependent, the same set the dispatcher
#: uses (``TaskService.blockers_open``). ``converted`` is not one: its work
#: moved to replacements (TASK-2026-00492).
BLOCKER_DONE_STATUS = BLOCKER_RELEASED_STATUSES


async def _blocked(env: ObjectiveEnv, ticket: Mapping[str, Any]) -> bool:
    if str(ticket.get("status") or "") == "waiting":
        return True
    for dep in ticket.get("blocked_by") or []:
        row = await env.tasks.get_task(str(dep))
        if str((row or {}).get("status") or "") not in BLOCKER_DONE_STATUS:
            return True
    return False


async def _open_dependents(
    env: ObjectiveEnv, objective_id: str, task_id: str,
) -> list[str]:
    """Open tickets of the objective that wait on ``task_id``."""
    rows = await env.tasks.list_by_objective(objective_id)
    return [str(row.get("task_id")) for row in rows
            if task_id in (row.get("blocked_by") or [])
            and str(row.get("task_id") or "") != task_id
            and str(row.get("status") or "") not in TERMINAL_TICKET_STATUS]


async def _apply_pending_rewires(
    env: ObjectiveEnv, objective_id: str, new_ids: Sequence[str],
) -> None:
    """Point the dependents of a decomposed ticket at its replacements.

    An in-objective decomposition leaves its ticket ``converted`` for good,
    which never releases a dependent; the first plan that creates tickets
    after it creates the replacements (TASK-2026-00492).
    """
    if not new_ids:
        return
    current = await env.objectives.get_objective(objective_id) or {}
    pending = [entry for entry in current.get("pending_rewires") or []
               if isinstance(entry, Mapping)]
    if not pending:
        return
    for entry in pending:
        old = str(entry.get("from") or "")
        for dep_id in entry.get("dependents") or []:
            row = await env.tasks.get_task(str(dep_id))
            if row is None or str(row.get("status") or "") \
                    in TERMINAL_TICKET_STATUS:
                continue
            blockers = [str(item) for item in row.get("blocked_by") or []
                        if str(item) != old]
            blockers += [item for item in new_ids
                         if item not in blockers and item != str(dep_id)]
            await env.patch_task(str(dep_id), {"blocked_by": blockers})
            await _mirror_dependencies(env, row, blockers)
    await env.objectives.patch_objective(objective_id, {"pending_rewires": []})


async def _mirror_dependencies(
    env: ObjectiveEnv, row: Mapping[str, Any], blockers: Sequence[str],
) -> None:
    """Carry a rewire to Jardal ERP ``depends_on`` (only set at creation).

    Without it the ERP keeps the converted ticket as a dependency and
    refuses the dependent's close with 417. Fail-soft: execution order is
    already correct in Mongo.
    """
    setter = getattr(getattr(env, "erp", None), "set_dependencies", None)
    name = erp_group_name(row)
    if setter is None or not name:
        return
    names = [erp_group_name(await env.tasks.get_task(item))
             for item in blockers]
    try:
        await setter(name, [item for item in names if item])
    except Exception:  # noqa: BLE001
        logger.warning("ERP dependencies of %s not updated", name,
                       exc_info=True)


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
        "evidencia": str(close.evidencia or "")[:500],
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


def _delivery_domain(ticket: Mapping[str, Any]) -> str:
    """Non-dev delivery domain, for the evidence-verifier lookup."""
    params = ticket.get("params")
    if isinstance(params, Mapping):
        agent = str(params.get("target_agent") or "").strip()
        if agent:
            return agent
    hint = str(ticket.get("agent_hint") or "").strip()
    return hint or "unknown"


def _work_children(
    objective: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Work children: everything but the parent shell and converted tickets."""
    source_id = str(objective.get("conversion_source_task_id") or "")
    return [item for item in children
            if str(item.get("task_id") or "") != source_id
            and str(item.get("status") or "") != "converted"]


#: Blockers that never release a dependent by themselves: the planner
#: replaces work queued behind a failed one, and its next created tickets
#: rewire a converted one's dependents (TASK-2026-00492).
NON_RELEASING_BLOCKER_STATUS = frozenset({FAILED_HISTORY_STATUS, "converted"})


def _is_dead_queued(
    by_id: Mapping[str, Mapping[str, Any]],
    item: Mapping[str, Any],
    _visiting: frozenset[str] = frozenset(),
) -> bool:
    """Queued work no live blocker can release: only a new plan moves it.

    Dead means at least one non-releasing blocker (failed, converted or
    itself dead-queued) and nothing else live: released blockers
    (completed/cancelled) are already satisfied, so a mix with them is as
    dead as the bare case (TASK-2026-00534). The dispatcher skips it
    (``_blocked``), so counting it as in flight or as an open gap wedges
    the objective: final review (TASK-2026-00396) and max_tickets, held
    forever behind a converted blocker (TASK-2026-00553). Unknown or live
    blockers and dependency cycles stay open (fail-closed).
    """
    if str(item.get("status") or "") != "queued":
        return False
    deps = item.get("blocked_by") or []
    if not deps:
        return False
    visiting = _visiting | {str(item.get("task_id") or "")}
    seen_dead = False
    for dep in deps:
        blocker = by_id.get(str(dep))
        if blocker is None or str(dep) in visiting:
            return False
        status = str(blocker.get("status") or "")
        if status in BLOCKER_RELEASED_STATUSES:
            continue
        if (status in NON_RELEASING_BLOCKER_STATUS
                or _is_dead_queued(by_id, blocker, visiting)):
            seen_dead = True
            continue
        return False
    return seen_dead


def _budget_children(
    tickets: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Children that spend the lifetime ``max_tickets`` budget.

    A converted ticket is superseded, not work: its decomposition replaces
    budget already admitted, and counting it refused the very substitutes
    its dependents wait for (TASK-2026-00553).
    """
    return [item for item in tickets
            if str(item.get("status") or "") != "converted"]


def _brake_active_children(
    tickets: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """In-flight children for the admission brake (TASK-2026-00406).

    Failed tickets are settled history (never slots), like terminal
    ones, and dead-queued work (behind non-releasing blockers only) can
    never run alone: counting either wedges planning once history fills
    the cap. Fail-closed: unknown or live blockers stay in flight.
    """
    by_id = {str(item.get("task_id") or ""): item for item in tickets}
    return [item for item in tickets
            if str(item.get("status")) not in TERMINAL_TICKET_STATUS
            and str(item.get("status")) != FAILED_HISTORY_STATUS
            and str(item.get("status")) != "converted"
            and not _is_dead_queued(by_id, item)]


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
    are history (``failed_children``), never open gaps, and so is queued
    work behind non-releasing blockers only (``dead_children``).
    No LLM is involved, ever.
    """
    work = _work_children(objective, children)
    by_id = {str(item.get("task_id") or ""): item for item in children}
    open_gaps: list[str] = []
    data_gaps: list[str] = []
    failed: list[str] = []
    dead: list[str] = []
    terminal = 0
    for item in work:
        short = str(item.get("task_id") or "")[:8]
        status = str(item.get("status") or "")
        if status == FAILED_HISTORY_STATUS:
            failed.append(short)
        elif _is_dead_queued(by_id, item):
            dead.append(short)
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
        "dead_children": dead,
        "work_children": len(work),
        "terminal_children": terminal,
        "dev_children": dev,
    }


#: Deterministic delivery verifier: async over one child record, report
#: ``{"ok": bool, "missing_artifacts": [...], "unmet_criteria": [...]}``.
#: Context (repo, anchor) rides via closure. Doubts are gaps, never
#: crashes: a raising verifier parks the objective (TASK-2026-00175).
DeliveryVerifier = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


async def verify_evidence(
    objective: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
    verifiers: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Evidence layer over ``verify_delivery``'s metadata check.

    Every non-dev delivery needs a deterministic domain runner:
    cancelled/failed children are history (never proof, never required),
    dev work verifies through the reviewer pool, and each completed
    non-dev child must clear its domain verifier — unless a person closed
    it as Completed in the business, which is the delivery verdict
    (``verified_by`` ``business``, TASK-2026-00571). Missing verifiers and
    failed evidence park the objective (capability_gap / data_gaps in
    ``run_final_review``); an empty ``verified`` map means the acceptance
    has no coverage. No LLM is involved, ever.
    """
    runs = verifiers or {}
    gaps: list[str] = []
    unverified: dict[str, list[str]] = {}
    verified: dict[str, str] = {}
    for item in _work_children(objective, children):
        short = str(item.get("task_id") or "")[:8]
        status = str(item.get("status") or "")
        if status != "completed":
            # History (cancelled/failed) or still open (the metadata
            # layer already routed those): never evidence.
            continue
        if _is_dev_ticket(item):
            continue
        if business_completion(item):
            verified[short] = "business"
            continue
        domain = _delivery_domain(item)
        verifier = runs.get(domain)
        if verifier is None:
            unverified.setdefault(domain, []).append(short)
            continue
        try:
            report = await verifier(item)
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"{short}: verifier error: {type(exc).__name__}")
            continue
        if not isinstance(report, Mapping):
            gaps.append(f"{short}: verifier error: bad report")
            continue
        if report.get("ok"):
            verified[short] = domain
            continue
        missing = report.get("missing_artifacts") or []
        unmet = report.get("unmet_criteria") or []
        gaps.extend(f"{short}: missing artifact: {name}" for name in missing)
        gaps.extend(f"{short}: unmet criterion: {name}" for name in unmet)
        if not missing and not unmet:
            gaps.append(f"{short}: not verified")
    return {
        "ok": not gaps and not unverified,
        "evidence_gaps": gaps,
        "unverified_domains": unverified,
        "verified": verified,
    }


def _delivery_record(
    objective: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
    *,
    verified_by: str,
    review: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Delivery verdict stored on close (criteria + counts + provenance).

    The objective closes on its DELIVERY criteria. The business outcome
    (KPI) is evaluated afterwards by the CEO (F4): ``outcome_kpi`` stays
    ``None`` here — closing every ticket never claims a metric moved.
    ``evidence`` links each non-dev delivery to the domain runner that
    verified it (TASK-2026-00175); dev evidence lives in the pool
    findings/closure files.
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
        "dead_children": len(verdict["dead_children"]),
        "dev_children": verdict["dev_children"],
        "outcome_kpi": None,
        "evidence": dict(evidence or {}),
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


#: Waiting reasons the boot resume reconsiders (TASK-2026-00396):
#: loop-guard stalls whose wedge may have cleared. Other parks stay
#: parked at boot: erp_error/data_gaps need their trigger, and
#: capability_gap resumes on child close (TASK-2026-00430).
_BOOT_RESUMABLE_PARKS = frozenset({"stalled"})


def boot_resume_decision(
    objective: Mapping[str, Any],
    children: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Pure boot-resume call: resume_fresh, replan or leave.

    No I/O, no tokens: the deterministic delivery check decides. A
    ``final_review`` objective is always orphaned at boot (no review
    task survives a restart); with open work it replans even on a spent
    review budget, which the final review handles without parking
    (TASK-2026-00468). A ``stalled`` park resumes only when its
    wedge provably cleared, into a fresh review episode. Still-wedged
    stalls stay parked silently: they already escalated. An ``active``
    objective with clean delivery is an orphaned cycle (no healthy
    objective rests there with nothing in flight), so it replans;
    spend-gated objectives keep their own trigger (00403).
    """
    status = str(objective.get("status") or "")
    pending = objective.get("pending_plan")
    if isinstance(pending, dict) and pending:
        return {"action": "leave", "reason": "spend approval pending"}
    if status == "active":
        verdict = verify_delivery(objective, children)
        gaps = list(verdict["open_gaps"]) + list(verdict["data_gaps"])
        if gaps:
            return {"action": "leave",
                    "reason": "active with open work"}
        return {"action": "resume_fresh",
                "reason": "active with clean delivery: orphaned cycle"}
    if status == "final_review":
        verdict = verify_delivery(objective, children)
        gaps = list(verdict["open_gaps"]) + list(verdict["data_gaps"])
        if not gaps:
            return {"action": "resume_fresh",
                    "reason": "orphaned final review, delivery clean"}
        return {"action": "replan",
                "reason": "orphaned final review, gaps open: "
                          f"{'; '.join(gaps[:5])}"}
    if status == "waiting":
        reason = str(objective.get("waiting_reason") or "")
        if reason.partition(":")[0] not in _BOOT_RESUMABLE_PARKS:
            return {"action": "leave", "reason": "park needs its trigger"}
        verdict = verify_delivery(objective, children)
        gaps = list(verdict["open_gaps"]) + list(verdict["data_gaps"])
        if gaps:
            return {"action": "leave",
                    "reason": "stall wedge still present"}
        return {"action": "resume_fresh",
                "reason": "stall wedge cleared, delivery clean"}
    return {"action": "leave",
            "reason": f"status {status or '<empty>'}"}


async def boot_resume_one(
    env: ObjectiveEnv, objective_id: str,
) -> dict[str, Any]:
    """Apply one boot-resume call: reset, notify, plan (TASK-2026-00396).

    ``resume_fresh`` clears the park and the spent review budget: the
    deterministic check proved the old wedge gone, so this is a new
    episode, not the same loop. ``replan`` sends an orphaned review with
    real gaps back to planning under whatever budget survives; ``leave``
    touches nothing.
    """
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    call = boot_resume_decision(
        objective, await env.tasks.list_by_objective(objective_id))
    action = str(call.get("action") or "leave")
    if action == "leave":
        return {"ok": True, "action": "leave",
                "reason": str(call.get("reason") or "")}
    patch: dict[str, Any] = {"status": "active", "waiting_reason": None}
    if action == "resume_fresh":
        patch["final_reviews"] = 0
    await env.objectives.patch_objective(objective_id, patch)
    if env.notify is not None:
        await env.notify(
            _notify_ref(objective),
            f"Objective {objective_id} resumed at boot "
            f"({action}): {call.get('reason')}.")
    plan = await plan_objective(env, objective_id)
    return {"ok": True, "action": action, "plan": plan}


async def resume_orphaned_one(
    env: ObjectiveEnv, objective_id: str, *, idle_before: datetime,
) -> dict[str, Any]:
    """Replan an active objective that no event can move (TASK-2026-00553).

    Runtime twin of the boot resume, driven by the mechanic heartbeat:
    the same deterministic call finds an ``active`` objective with no open
    work and no data gap, which no close will ever re-trigger. An
    objective with a plan in flight or activity after ``idle_before`` is
    left alone, and the final-review budget is kept: only a boot starts a
    fresh episode.
    """
    objective = await env.objectives.get_objective(objective_id)
    if objective is None or str(objective.get("status") or "") != "active":
        return {"ok": True, "action": "leave", "reason": "not active"}
    flight = _PLAN_FLIGHTS.get(objective_id)
    if flight is not None and not flight.task.done():
        return {"ok": True, "action": "leave", "reason": "plan in flight"}
    touched = recovery_mod.parse_due(objective.get("updated_at"))
    if touched is not None and touched > idle_before:
        return {"ok": True, "action": "leave", "reason": "recent activity"}
    call = boot_resume_decision(
        objective, await env.tasks.list_by_objective(objective_id))
    if call.get("action") != "resume_fresh":
        return {"ok": True, "action": "leave",
                "reason": str(call.get("reason") or "")}
    if env.notify is not None:
        await env.notify(
            _notify_ref(objective),
            f"Objective {objective_id} replanned by the heartbeat: nothing "
            "in flight and no event pending (orphaned cycle).")
    plan = await plan_objective(env, objective_id)
    return {"ok": True, "action": "replan", "plan": plan}


async def _run_routed_reviewer(
    run_attempt: Callable[..., Awaitable[dict[str, Any]]],
    *,
    routing: Any,
    slots: Any,
    executor: Any,
    slot: review_mod.ReviewSlot,
    attempt: int,
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return await wiring_mod.run_routed_reviewer(
        run_attempt, routing=routing, slots=slots, executor=executor,
        slot=slot, previous=previous)


async def run_final_review(env: ObjectiveEnv, objective_id: str) -> dict[str, Any]:
    """Final review by domain + close (§9.7).

    ``complete_candidate`` never closes directly: every path runs the
    deterministic delivery verification first (no tokens). Open work
    sends the objective back to planning; data gaps park it; clean
    non-dev work closes deterministically; clean dev work goes through
    the reviewer pool over the objective branch. Non-dev deliveries
    clear a registered deterministic verifier per domain (missing
    verifiers park as capability_gap, failed evidence or no verified
    delivery as data_gaps).

    A spent review budget (``max_final_reviews``, §9.6) never parks
    (TASK-2026-00468): open work keeps the objective active until its
    own closes re-plan, and a clean dev delivery closes on the
    deterministic checks, its last fixes cleared by their ticket reviews.
    """
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    reviews = int(objective.get("final_reviews") or 0)
    budget_spent = reviews >= int(
        env.objectives_cfg.get("max_final_reviews", 2))
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
    if verdict["open_gaps"] and budget_spent:
        # No planner bounce past the budget: every open child's close
        # re-plans this still-active objective, so the wait is bounded.
        gaps = "; ".join(verdict["open_gaps"][:5])
        if env.notify is not None:
            await env.notify(
                _notify_ref(objective),
                f"Objective {objective_id} waits for its open work "
                f"(final review budget spent): {gaps}")
        return {"ok": False, "reason": "awaiting_open_work",
                "detail": gaps}
    if verdict["open_gaps"]:
        await env.objectives.patch_objective(objective_id, {
            "status": "active", "final_reviews": reviews + 1})
        if env.notify is not None:
            await env.notify(
                _notify_ref(objective),
                f"Objective {objective_id} back to planning: "
                f"{'; '.join(verdict['open_gaps'][:5])}")
        return await plan_objective(env, objective_id)
    evidence = await verify_evidence(
        objective, children, env.delivery_verifiers)
    if evidence["unverified_domains"]:
        pending = "; ".join(
            f"{domain}: {','.join(shorts)}"
            for domain, shorts in sorted(
                evidence["unverified_domains"].items()))
        summary = (f"no delivery verifier for non-dev work ({pending}): "
                   "register a deterministic runner per domain")
        await env.objectives.patch_objective(objective_id, {
            "capability_gap": {
                "declared_at": _utcnow_iso(),
                "summary": summary[:2000],
                "gaps": [{"domain": domain, "children": list(shorts)}
                         for domain, shorts in sorted(
                             evidence["unverified_domains"].items())],
            },
        })
        return await _objective_park(
            env, objective, "capability_gap", summary[:300])
    if evidence["evidence_gaps"]:
        return await _objective_park(
            env, objective, "data_gaps",
            "delivery evidence gaps: "
            f"{'; '.join(evidence['evidence_gaps'][:5])}")
    if not verdict["dev_children"] and not evidence["verified"]:
        if not verdict["work_children"]:
            detail = ("no verified delivery: objective has no work "
                      "children")
        else:
            detail = ("no verified delivery: all "
                      f"{verdict['work_children']} work children are "
                      "history (cancelled/failed)")
        return await _objective_park(env, objective, "data_gaps", detail)
    if budget_spent and verdict["dev_children"]:
        return await _finish_objective(
            env, objective, {"verified": evidence["verified"]},
            closure=(f"final review budget spent ({reviews} reviews, "
                     "§9.6); the last fixes closed through their own "
                     "ticket reviews"),
            review="budget_spent")
    await env.objectives.patch_objective(
        objective_id, {"status": "final_review",
                       "final_reviews": reviews + 1})
    if not verdict["dev_children"]:
        # Non-dev: the deterministic verification above IS the review,
        # now backed by the domain evidence it cleared.
        objective = await env.objectives.get_objective(objective_id) \
            or objective
        return await _finish_objective(
            env, objective, {"verified": evidence["verified"]})
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
          "regressions across the set? production-grade delivery: standard "
          "maintained libraries instead of hand-written substitutes, "
          "conventional names, and handoff evidence that user-facing "
          "results were verified the way users meet them? Same report "
          "format as ticket rounds."
    )
    scratch = objective_scratch(env.repo, objective_id)
    try:
        assembly = wiring_mod.assemble_review_pool(
            list(env.pool_raw), review_cfg=env.review_cfg,
            run_id=f"final-{objective_id}", round="0",
            builder_family="muse",
            receipts_dir=scratch / "review_receipts")
    except wiring_mod.ReviewRoutingError as exc:
        return await _objective_park(
            env, objective, "config", f"free-router: {exc}"[:300])
    routing = assembly.routing
    if routing is not None:
        pool = review_mod.parse_pool(assembly.raw_pool)
        prefer: Sequence[str] = tuple(assembly.prefer_families)
        review_executor: Any = ProcessExecutor(
            pool=list(assembly.raw_pool))
        run_env = replace(env, executor=review_executor)
    else:
        pool = env.pool
        prefer = ()
        review_executor = None
        run_env = env
    await wiring_mod.activate_routed_slots(routing, env.slots)
    plan = await review_mod.select_round(
        pool, env.slots, 3, builder_family="muse",
        prefer_families=prefer,
        cascade_enabled=review_mod.cascade_enabled(
            env.review_cfg.get("cascade")))

    async def run_one(
        slot: review_mod.ReviewSlot,
        attempt: int,
        previous: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        async def run_attempt(
            current: review_mod.ReviewSlot,
            tried: Mapping[str, Any] | None,
        ) -> dict[str, Any]:
            return await _run_final_reviewer(
                run_env, current, request, head, objective,
                attempt=attempt, previous=tried,
                review_no=reviews + 1)
        if routing is None or slot.label not in routing.states:
            return await run_attempt(slot, previous)
        return await _run_routed_reviewer(
            run_attempt, routing=routing, slots=env.slots,
            executor=review_executor, slot=slot, attempt=attempt,
            previous=previous)

    critical = any(
        isinstance(child, Mapping)
        and str(child.get("priority") or "").lower() == "critical"
        for child in children)
    cascade = review_mod.arm_premium_slots(
        run_env.executor, pool, env.review_cfg.get("cascade"), critical)
    try:
        result = await review_mod.run_quorum_round(
            pool, env.slots, plan, run_one, builder_family="muse",
            quorum=int(env.review_cfg.get("quorum", 2)),
            recovery=env.review_cfg.get("recovery"),
            cascade=cascade)
    finally:
        review_mod.disarm_premium_slots(run_env.executor, pool)
        if routing is not None:
            routing.close()
    if not result.met:
        return await _objective_park(
            env, objective, "no_quorum",
            f"final review without quorum: {result.summary() or 'empty pool'}")
    verdicts = result.verdicts
    consolidated = findings_mod.consolidate(verdicts)
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "OBJECTIVE_FINDINGS.md").write_text(
        findings_mod.render_findings(consolidated, 0, head,
                                     builder_family="muse"),
        encoding="utf-8")
    for item in verdicts:
        if item.destination == "inbox" and env.inbox is not None:
            await _inbox_final_finding(env, objective, item)
    closed = await _close_objective(
        env, objective, consolidated, head,
        {"verified": evidence["verified"]})
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
        outcome["account"] = review_mod.executed_account(
            slot, result.slot)
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
                  "account": str(outcome.get("account") or ""),
                  "model": str(outcome.get("model") or ""),
                  "tier": str(outcome.get("tier") or ""),
                  "spend": outcome.get("spend") or {},
                  "project": objective.get("project")}))
    except Exception:  # noqa: BLE001, S110
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
    except Exception:  # noqa: BLE001, S110
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
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``muse`` judges the final findings; REAL ones become tickets (§9.7)."""
    objective_id = str(objective.get("objective_id"))
    anchor = objective.get("anchor") or {}
    capsule = (objective.get("capsule") or {}).get("text", "")
    timeout_s = int(env.timeouts.get("closer", 15)) * 60
    if not consolidated:
        return await _finish_objective(env, objective, evidence)
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
    except Exception:  # noqa: BLE001, S110
        pass
    text = _read(result_path)
    verdicts = parse_closure(text) if first.returncode == 0 else {}
    missing = [item.id for item in consolidated if item.id not in verdicts]
    attempts = [first]
    if missing:
        attempts.append(await env.executor.run(
            "closer",
            prompt + "\n\n" + _closure_retry_prompt(
                missing, env.repo,
                result_path=str(result_path.relative_to(env.repo))),
            env.repo, timeout_s))
        try:
            await env.executor.cleanup("closer")
        except Exception:  # noqa: BLE001, S110
            pass
        text = _read(result_path)
        verdicts = parse_closure(text) if attempts[-1].returncode == 0 else {}
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
        return await _finish_objective(env, objective, evidence)
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
    except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001, S110
        pass
    if result.returncode != 0:
        return True
    for line in (result.output_tail or "").splitlines():
        upper = line.strip().upper()
        if upper.startswith("REAL"):
            return True
        if upper.startswith("FALSO"):
            return False
    return True  # unparseable: reproduce rather than drop a CRITICAL


def _closure_expected_artifacts(item: Any) -> list[str]:
    """Repro/fix artefacts for a closure child (TASK-2026-00173).

    The anchored files, deduplicated. A finding that only cites
    acceptance criteria names them instead — the parser guarantees at
    least one anchor, so this is never empty on real findings.
    """
    artefacts: list[str] = []
    for anchor in item.anchors or []:
        path = getattr(anchor, "path", None)
        if path and path not in artefacts:
            artefacts.append(path)
    if not artefacts:
        for anchor in item.anchors or []:
            criterion = getattr(anchor, "criterion", None)
            ref = f"criterion:{criterion}" if criterion else ""
            if ref and ref not in artefacts:
                artefacts.append(ref)
    return artefacts


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
        # Same minimum delivery contract as planner children
        # (TASK-2026-00173): without it the fixed child parks the next
        # final review in data_gaps, and misroutes as non-dev.
        "agent_hint": "dev",
        "params": {
            "target_agent": "dev",
            "work_type": "debugging",
            "expected_artifacts": _closure_expected_artifacts(item),
        },
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
    evidence: Mapping[str, Any] | None = None,
    *,
    closure: str = "final review clean",
    review: str | None = None,
) -> dict[str, Any]:
    """Close the objective: delivery record, ERP note, vault note (§9.7).

    The parent ticket (the ERP group) completes here, so the business
    close pushes through the standard queue: children first, then the
    group — a 417 parks as ``pending_status`` and the startup reconcile
    retries it. No group push is done directly. ``evidence`` is the
    ``{"verified": ...}`` link the final review cleared (None when the
    review ran elsewhere). ``closure`` states how the close was earned in
    every notice; ``review`` overrides the delivery record's review kind.
    """
    from orchestration import knowledge as knowledge_mod

    objective_id = str(objective.get("objective_id"))
    children = await env.tasks.list_by_objective(objective_id)
    await env.objectives.patch_objective(objective_id, {
        "status": "closed",
        "closed_at": _utcnow_iso(),
        "delivery": _delivery_record(
            objective, children, verified_by="final_review",
            review=review, evidence=evidence),
    })
    mission_id = str(objective.get("mission_id") or "").strip()
    if mission_id and getattr(env, "missions", None) is not None:
        from objectives import missions as missions_mod

        try:
            await missions_mod.record_objective_closed(
                getattr(env, "missions", None), mission_id,
                objective_id)
        except Exception:  # noqa: BLE001
            logger.warning("mission ledger failed for %s (close stands)",
                           mission_id)
    if env.ceo_wake is not None:
        reason = f"Objective {objective_id} closed: {closure}."
        if mission_id:
            reason += (
                f" It served mission {mission_id}: judge the mission "
                "acceptance and emit its next objective pedido, or "
                "close the mission."
            )
        try:
            env.ceo_wake(reason)
        except Exception:  # noqa: BLE001, S110
            pass
    parent_id = str(objective.get("conversion_source_task_id") or "")
    # Tickets that decomposed inside the objective close with it, children
    # before the group, or their ERP record stays open (TASK-2026-00492).
    for child in children:
        child_id = str(child.get("task_id") or "")
        if child_id and child_id != parent_id \
                and str(child.get("status") or "") == "converted":
            await env.patch_task(child_id, {
                "status": "completed",
                "waiting_reason": None,
                "resume_at": None,
                "completed_at": _utcnow_iso(),
                "last_response_summary": (
                    f"Decomposed inside objective {objective_id}; its "
                    f"replacements delivered it ({closure})."),
            })
    if parent_id:
        parent = await env.tasks.get_task(parent_id)
        if parent is not None and str(parent.get("status") or "") == "converted":
            await env.patch_task(parent_id, {
                "status": "completed",
                "waiting_reason": None,
                "resume_at": None,
                "completed_at": _utcnow_iso(),
                "last_response_summary": (
                    f"Objective {objective_id} closed: {closure}."),
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
            f"({mix}), {closure}.")
        if not env.auto_merge:
            await env.notify(
                ref,
                f"Human PR needed: objective/{objective_id} → "
                f"{env.base_branch} (policy auto_merge=false, §12).")
    if env.vault:
        try:
            await knowledge_mod.write_objective_note(env, objective)
        except Exception as exc:  # noqa: BLE001
            if env.notify is not None:
                await env.notify(
                    ref, f"vault note failed (close stands): {exc}")
    if getattr(env, "playbook_proposals", False):
        # Playbook crystallization (TASK-2026-00217): the planner judges
        # recurrence once the close stands. Never breaks the close.
        try:
            _, hook_skills = _planner_skills()
            await playbooks_mod.propose_playbook(
                env, objective_id, "close",
                agents=env.planner_cfg.get("agents", {}) or {},
                skills=hook_skills)
        except Exception as exc:  # noqa: BLE001
            logger.warning("playbook proposal failed for %s (close "
                           "stands): %s", objective_id, exc)
    return {"ok": True, "action": "closed", "objective_id": objective_id}


# ---------------------------------------------------------------------------
# Inverse webhook: the ERP group decision reaches the objective (F2)
# ---------------------------------------------------------------------------

#: Children worth cancelling when the group dies in Jardal ERP. Terminal and
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
                f"Objective {objective_id} closed from Jardal ERP "
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
            "error": (f"Objective {objective_id} cancelled in Jardal ERP "
                      f"({business_status})."),
        })
        cancelled.append(child_id)
    await env.objectives.patch_objective(objective_id, {
        "status": "cancelled",
        "cancelled_at": _utcnow_iso(),
        "cancel_reason": f"group {business_status} in Jardal ERP",
        "cancelled_children": cancelled,
    })
    if env.notify is not None:
        await env.notify(
            source_id or objective_id,
            f"Objective {objective_id} cancelled from Jardal ERP "
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
