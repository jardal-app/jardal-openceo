"""Dev objectives: multi-ticket work chains (design §9, F6).

An objective is a chain of mixed-tier tickets created gradually by an
objective planner (``claudej``), with OpenCEO-side dependencies, a
deterministically compiled capsule, triggers, loop guardrails, a final pool
review and a ``muse`` close. Not to be confused with Horizon objectives
(``horizon_objectives``): those are the CEO's personal goals; these live in
the ``objectives`` collection (§15.2) and drive dev execution.

Entry points: :func:`convert_ticket` (a Tier S ticket outgrew one session),
:func:`plan_objective` (next horizon of tickets), :func:`on_ticket_closed`
(triggers + capsule after any objective ticket closes) and
:func:`run_final_review` (planner declared ``OBJECTIVE_COMPLETE``).
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

import yaml

from agents.dev.tier_s import findings as findings_mod
from agents.dev.tier_s import inbox as inbox_mod
from agents.dev.tier_s import review as review_mod


class Runner(Protocol):
    """Structural executor (see :class:`tier_s.cycle.Executor`)."""

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any: ...
    async def cleanup(self, role: str) -> None: ...


class Quota(Protocol):
    async def check(self, accounts: Sequence[str]) -> Mapping[str, Any]: ...


class ObjectiveStore(Protocol):
    async def create_objective(self, doc: dict[str, Any]) -> dict[str, Any]: ...
    async def get_objective(self, objective_id: str) -> dict[str, Any] | None: ...
    async def patch_objective(
        self, objective_id: str, updates: dict[str, Any]) -> dict[str, Any] | None: ...


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
    timeouts: dict[str, int] = field(default_factory=dict)
    fix_tiers: dict[str, str] = field(default_factory=dict)
    inbox_rule: dict[str, Any] = field(default_factory=dict)
    auto_merge: bool = True
    base_branch: str = "main"
    vault: dict[str, str] | None = None  # {dir, project} (F7b)


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


TERMINAL_TICKET_STATUS = frozenset({"completed", "cancelled"})

VALID_TIERS = ("tier_s", "tier_a", "tier_b", "tier_c")


# ---------------------------------------------------------------------------
# Conversion (§9.2)
# ---------------------------------------------------------------------------

async def convert_ticket(
    env: ObjectiveEnv,
    ticket: Mapping[str, Any],
    cause: str,
    sizing_text: str = "",
) -> dict[str, Any]:
    """Turn an oversized Tier S ticket into an objective (§9.2).

    The ticket text becomes the immutable anchor; the ticket itself moves to
    ``converted`` (its children carry the work) — never ``completed``: the
    parent goes Completed in ERP only at objective close (§9.7). The ERP
    parent mirror stays empty — the adapter has no parent/group task
    support (§20-17) — and the objective planner launches immediately with
    ``SIZING.md`` as a hint. Returns the objective document.
    """
    objective_id = new_objective_id()
    task_id = str(ticket.get("task_id", ""))
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
    base_sha = _git(env.repo, "rev-parse", "HEAD").strip()
    objective = await env.objectives.create_objective({
        "objective_id": objective_id,
        "project": project,
        "erp_parent_ref": None,  # §20-17: no adapter support; children comment it
        "anchor": anchor,
        "status": "active",
        "capsule": {"text": "", "compiled_at": None, "tokens": 0},
        "roadmap_remaining": [],
        "tickets": [],
        "tickets_log": [],
        "tickets_files": {},
        "planning_runs": [],
        "final_reviews": 0,
        "last_final_review_sha": None,
        "base_sha": base_sha,
        "branch": "base" if env.auto_merge else f"objective/{objective_id}",
        "conversion_source_task_id": task_id,
        "conversion_cause": cause,
        "sizing_hint": sizing_text[:4000],
    })
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
    await plan_objective(env, objective_id)
    final = await env.objectives.get_objective(objective_id)
    return final or objective


# ---------------------------------------------------------------------------
# Objective planner (§9.3)
# ---------------------------------------------------------------------------

def parse_tickets_yaml(text: str) -> dict[str, Any]:
    """Parse ``TICKETS.yaml`` (§16.7). Raises ValueError when malformed."""
    try:
        data = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid TICKETS.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("TICKETS.yaml must be a mapping")
    return data


def _local_cycles(tickets: Sequence[Mapping[str, Any]]) -> list[str]:
    keys = {str(item.get("key") or "") for item in tickets
            if str(item.get("key") or "")}
    edges = {str(item.get("key") or ""):
             [dep for dep in (item.get("blocked_by") or [])
              if str(dep) in keys]
             for item in tickets if str(item.get("key") or "")}
    errors: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            errors.append(f"blocked_by cycle: {' -> '.join([*stack, node])}")
            return
        visiting.add(node)
        for dep in edges.get(node, []):
            visit(dep, [*stack, node])
        visiting.discard(node)
        done.add(node)

    for key in edges:
        visit(key, [])
    return errors


async def validate_tickets(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    data: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Deterministic validation (§9.3). Returns (errors, tickets)."""
    errors: list[str] = []
    if str(data.get("objective") or "") != objective.get("objective_id"):
        errors.append("objective id mismatch")
    complete = bool(data.get("complete", False))
    raw_tickets = data.get("tickets") or []
    if complete and raw_tickets:
        errors.append("complete:true must carry no tickets")
    if complete:
        return errors, []
    if not isinstance(raw_tickets, list) or not raw_tickets:
        return [*errors, "no tickets and complete is not true"], []
    if len(raw_tickets) > 3:
        errors.append(f"horizon is 2-3 tickets, got {len(raw_tickets)}")
    tickets: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(raw_tickets):
        where = f"tickets[{index}]"
        if not isinstance(item, Mapping):
            errors.append(f"{where}: must be a mapping")
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            errors.append(f"{where}: missing key")
        elif key in seen_keys:
            errors.append(f"{where}: duplicate key {key!r}")
        seen_keys.add(key)
        if not str(item.get("title") or "").strip():
            errors.append(f"{where}: missing title")
        if str(item.get("coding_tool") or "") not in VALID_TIERS:
            errors.append(f"{where}: bad coding_tool (tier_s/a/b/c)")
        if not str(item.get("tier_reason") or "").strip():
            errors.append(f"{where}: missing tier_reason")
        if not str(item.get("description") or "").strip():
            errors.append(f"{where}: missing description")
        acceptance = item.get("acceptance") or []
        if not isinstance(acceptance, list) or not [
                entry for entry in acceptance if str(entry).strip()]:
            errors.append(f"{where}: missing acceptance criteria")
        tickets.append(dict(item))
    errors.extend(_local_cycles(tickets))
    # blocked_by targets: local keys or existing tasks; no dupes of open ones.
    keys = {str(item.get("key")) for item in tickets}
    for item in tickets:
        for dep in item.get("blocked_by") or []:
            dep = str(dep)
            if dep in keys:
                continue
            exists = await env.tasks.get_task(dep)
            if exists is None:
                errors.append(
                    f"{item.get('key')}: blocked_by unknown {dep!r}")
    siblings = await env.tasks.list_by_objective(
        str(objective.get("objective_id")))
    open_titles = {str(item.get("title") or "").strip().lower()
                   for item in siblings
                   if str(item.get("status") or "") not in TERMINAL_TICKET_STATUS}
    for item in tickets:
        if str(item.get("title") or "").strip().lower() in open_titles:
            errors.append(f"{item.get('key')}: duplicates an open ticket")
    return errors, tickets


def _planner_prompt(
    objective: Mapping[str, Any],
    capsule_text: str,
    open_tickets: Sequence[Mapping[str, Any]],
    inbox_md: str,
    deferred: Sequence[str],
    sizing_hint: str = "",
) -> str:
    criteria = [f"- {item}" for item in (objective.get("anchor") or {}).get(
        "acceptance", [])] or ["- (derive verifiable criteria)"]
    opened = [f"- {item.get('task_id')}: {item.get('title')} "
              f"[{item.get('coding_tool')}] ({item.get('status')})"
              for item in open_tickets] or ["- (none)"]
    lines = [
        "You are the objective planner. You do NOT explore code.",
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
        "INBOX (pre-existing findings touching objective files, "
        "claim via includes_findings):",
        inbox_md,
    ]
    if deferred:
        lines.extend(["", "DEFERRED (codexj suggestions out of scope):",
                      *[f"- {item}" for item in deferred]])
    if sizing_hint:
        lines.extend(["", "SIZING HINT from the converted ticket:",
                      sizing_hint[:3000]])
    lines.extend([
        "",
        "Write .tier-s/TICKETS.yaml (§16.7): objective, complete flag,",
        "roadmap_remaining, and a horizon of 2-3 tickets, each with key,",
        "title, coding_tool (cheapest tier that suffices), tier_reason,",
        "self-contained description, acceptance, files_hint, blocked_by",
        "(local keys or existing task_ids, acyclic) and includes_findings.",
        "If the anchor acceptance is met, write complete:true with no",
        "tickets instead. Then emit TICKETS_READY in .tier-s/status.",
    ])
    return "\n".join(lines)


async def plan_objective(env: ObjectiveEnv, objective_id: str) -> dict[str, Any]:
    """Run the objective planner once (§9.3). Returns a result summary."""
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    cfg = env.objectives_cfg
    tickets = await env.tasks.list_by_objective(objective_id)
    if len(tickets) >= int(cfg.get("max_tickets", 20)):
        return await _objective_park(
            env, objective, "needs_owner",
            f"max_tickets ({cfg.get('max_tickets', 20)}) reached")
    runs = objective.get("planning_runs") or []
    stalled = 0
    for run in reversed(runs):
        if run.get("tickets_created", 0) == 0 and not run.get("complete"):
            stalled += 1
        else:
            break
    closed_before = sum(1 for item in tickets
                        if str(item.get("status")) in TERMINAL_TICKET_STATUS)
    if stalled >= int(cfg.get("max_planning_runs_without_progress", 2)) \
            and closed_before == int(objective.get("_closed_at_last_plan", 0) or 0):
        return await _objective_park(
            env, objective, "needs_owner",
            "planning without progress (tope §9.6)")
    open_tickets = [item for item in tickets
                    if str(item.get("status")) not in TERMINAL_TICKET_STATUS]
    files = sorted({name for paths in
                    (objective.get("tickets_files") or {}).values()
                    for name in paths})
    inbox_md = ""
    if env.inbox is not None and files:
        inbox_md = await inbox_mod.inbox_md_for_planner(
            env.inbox, files, int(cfg.get("inbox_items_for_planner", 15)))
    deferred = [str(item) for item in objective.get("deferred") or []]
    prompt = _planner_prompt(
        objective, (objective.get("capsule") or {}).get("text", ""),
        open_tickets, inbox_md or "INBOX\n(none)\n", deferred,
        str(objective.get("sizing_hint") or ""))
    scratch = env.repo / ".tier-s" / "objectives" / objective_id
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "OBJECTIVE_CAPSULE.md").write_text(
        (objective.get("capsule") or {}).get("text", "") or "(empty)\n",
        encoding="utf-8")
    (scratch / "INBOX.md").write_text(inbox_md or "INBOX\n(none)\n",
                                      encoding="utf-8")
    timeout_s = int(env.timeouts.get("objective_planner", 15)) * 60
    errors: list[str] = []
    data: dict[str, Any] = {}
    for _attempt in range(2):
        await env.executor.run("objective_planner", prompt, env.repo, timeout_s)
        text = _read(env.repo / ".tier-s" / "TICKETS.yaml")
        if not text:
            text = _read(scratch / "TICKETS.yaml")
        if not text:
            errors = ["no TICKETS.yaml"]
        else:
            try:
                data = parse_tickets_yaml(text)
            except ValueError as exc:
                errors = [str(exc)]
            else:
                errors, _tickets = await validate_tickets(env, objective, data)
        if not errors:
            break
        prompt = (f"Your TICKETS.yaml is invalid: {'; '.join(errors)}\n"
                  "Fix it in place and emit TICKETS_READY again.")
    try:
        await env.executor.cleanup("objective_planner")
    except Exception:
        pass
    if errors:
        return await _objective_park(
            env, objective, "needs_owner",
            f"invalid TICKETS.yaml after retry: {'; '.join(errors)}")
    runs.append({"at": _utcnow_iso(),
                 "tickets_created": len(data.get("tickets") or []),
                 "complete": bool(data.get("complete", False))})
    await env.objectives.patch_objective(objective_id, {
        "planning_runs": runs,
        "roadmap_remaining": list(data.get("roadmap_remaining") or []),
        "_closed_at_last_plan": closed_before,
        "sizing_hint": "",
    })
    if data.get("complete", False):
        return await run_final_review(env, objective_id)
    created = await _create_planned_tickets(env, objective, data)
    await env.objectives.patch_objective(objective_id, {
        "tickets": [*(objective.get("tickets") or []),
                    *(item["task_id"] for item in created)],
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
    return {"ok": False, "reason": reason, "detail": detail}


async def _create_planned_tickets(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
    data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Create every validated ticket in topological order (§9.3)."""
    tickets = [dict(item) for item in data.get("tickets") or []]
    keys = [str(item.get("key")) for item in tickets]
    order: list[str] = []
    permanent: set[str] = set()

    def visit(node: str) -> None:
        if node in permanent:
            return
        permanent.add(node)
        item = next(entry for entry in tickets
                    if str(entry.get("key")) == node)
        for dep in item.get("blocked_by") or []:
            if str(dep) in keys:
                visit(str(dep))
        order.append(node)

    for key in keys:
        visit(key)
    by_key = {str(item.get("key")): item for item in tickets}
    created: list[dict[str, Any]] = []
    key_to_task: dict[str, str] = {}
    run_tag = uuid.uuid4().hex[:8]
    for key in order:
        item = by_key[key]
        blocked = [key_to_task.get(str(dep), str(dep))
                   for dep in item.get("blocked_by") or []]
        payload = {
            "title": str(item.get("title")),
            "description": str(item.get("description")),
            "acceptance": [str(entry) for entry in item.get("acceptance") or []],
            "coding_tool": str(item.get("coding_tool")),
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
        row = await env.create_ticket(payload)
        task_id = str((row or {}).get("task_id") or "")
        if not task_id:
            continue
        key_to_task[key] = task_id
        created.append({"task_id": task_id, "key": key})
        if env.inbox is not None:
            for fp in item.get("includes_findings") or []:
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
        ready = [item for item in tickets
                 if str(item.get("status")) not in TERMINAL_TICKET_STATUS
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
# Final review + close (§9.7)
# ---------------------------------------------------------------------------

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


async def run_final_review(env: ObjectiveEnv, objective_id: str) -> dict[str, Any]:
    """Final pool review + ``muse`` close (§9.7)."""
    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "reason": "objective not found"}
    reviews = int(objective.get("final_reviews") or 0)
    if reviews >= int(env.objectives_cfg.get("max_final_reviews", 2)):
        return await _objective_park(
            env, objective, "needs_owner", "max_final_reviews reached (§9.6)")
    await env.objectives.patch_objective(
        objective_id, {"status": "final_review",
                       "final_reviews": reviews + 1})
    anchor = objective.get("anchor") or {}
    capsule = (objective.get("capsule") or {}).get("text", "")
    since = objective.get("last_final_review_sha") or objective.get("base_sha")
    head = _git(env.repo, "rev-parse", "HEAD").strip()
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
    outcomes = await asyncio.gather(*[
        _run_final_reviewer(env, slot, request, head, objective)
        for slot in [*plan.reviewers, *plan.extras]
    ])
    valid = {item["slot"]: item["valid"] for item in outcomes}
    if not review_mod.quorum_met(valid, list(plan.reviewers), "muse", 2):
        return await _objective_park(
            env, objective, "needs_owner",
            "final review without quorum (pool exhausted)")
    verdicts: list[Any] = []
    for item in outcomes:
        verdicts.extend(item["verdicts"])
    consolidated = findings_mod.consolidate(verdicts)
    scratch = env.repo / ".tier-s" / "objectives" / objective_id
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "OBJECTIVE_FINDINGS.md").write_text(
        findings_mod.render_findings(consolidated, 0, head,
                                     builder_family="muse"),
        encoding="utf-8")
    for item in verdicts:
        if item.destination == "inbox" and env.inbox is not None:
            await _inbox_final_finding(env, objective, item)
    closed = await _close_objective(env, objective, consolidated, head)
    await env.objectives.patch_objective(
        objective_id, {"last_final_review_sha": head})
    return closed


async def _run_final_reviewer(
    env: ObjectiveEnv,
    slot: review_mod.ReviewSlot,
    request: str,
    head_sha: str,
    objective: Mapping[str, Any],
) -> dict[str, Any]:
    outcome: dict[str, Any] = {"slot": slot.label, "valid": False,
                               "verdicts": [], "reason": ""}
    try:
        worktree = await asyncio.to_thread(
            review_mod.create_worktree, env.repo, head_sha,
            f"final-{slot.label}")
    except RuntimeError as exc:
        outcome["reason"] = str(exc)[:200]
        return outcome
    try:
        (worktree / "OBJECTIVE_REVIEW_REQUEST.md").write_text(
            request, encoding="utf-8")
        before = await asyncio.to_thread(
            review_mod.worktree_snapshot, worktree)
        timeout_s = int(env.review_cfg.get(
            "reviewer_timeout_minutes", 15)) * 60
        result = await env.executor.run(
            f"review:{slot.label}",
            review_mod.reviewer_prompt(
                slot.label, "OBJECTIVE_REVIEW_REQUEST.md",
                "objective_final", 0),
            worktree, timeout_s)
        after = await asyncio.to_thread(
            review_mod.worktree_snapshot, worktree)
        if before and after and before != after:
            outcome["reason"] = "write_attempt"
            return outcome
        text = (result.output_tail or "").strip()
        if result.returncode != 0:
            action, patch = review_mod.classify_slot_error(text, slot)
            if action in {"cooling_down", "disabled"}:
                await env.slots.set_slot(slot.label, patch)
            outcome["reason"] = f"slot error ({action})"
            return outcome
        try:
            raw = findings_mod.parse_report(text)
        except findings_mod.ReportMalformed as exc:
            outcome["reason"] = f"malformed: {exc}"
            return outcome
        base = str(objective.get("base_sha") or head_sha)
        acceptance = (objective.get("anchor") or {}).get("acceptance", [])
        verdicts = await asyncio.to_thread(
            findings_mod.verify_report, raw, env.repo, head_sha, base,
            slot.label, slot.family, acceptance)
        outcome["verdicts"] = verdicts
        outcome["valid"] = True
        if env.reviews is not None:
            try:
                await env.reviews.create_review(findings_mod.review_document(
                    review_id=f"rev-{uuid.uuid4().hex[:12]}",
                    task_id=str(objective.get("conversion_source_task_id") or ""),
                    run_id=f"final-{objective.get('objective_id')}",
                    scope="objective_final", round_no=0,
                    slot=slot.label, family=slot.family, valid=True,
                    verdicts=verdicts))
            except Exception:
                pass
        return outcome
    finally:
        await asyncio.to_thread(
            review_mod.remove_worktree, env.repo, worktree)


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
    prompt = (
        "Judge the objective final findings in OBJECTIVE_FINDINGS.md "
        f"({len(consolidated)} findings). They are verified: they point at "
        "real code.\n"
        f"Anchor goal: {str(anchor.get('goal', ''))[:500]}\n"
        f"Capsule contracts:\n{capsule[:2000]}\n"
        "Write OBJECTIVE_CLOSURE.md with one line per finding: "
        "`F<n>: REAL — <why>` or `F<n>: FALSO — <verifiable technical "
        "reason>`. Then emit CLOSURE_READY in .tier-s/status."
    )
    await env.executor.run("closer", prompt, env.repo, timeout_s)
    try:
        await env.executor.cleanup("closer")
    except Exception:
        pass
    scratch = env.repo / ".tier-s" / "objectives" / objective_id
    text = _read(env.repo / ".tier-s" / "OBJECTIVE_CLOSURE.md") or _read(
        scratch / "OBJECTIVE_CLOSURE.md")
    verdicts = parse_closure(text)
    missing = [item.id for item in consolidated if item.id not in verdicts]
    if missing:
        await env.executor.run(
            "closer",
            f"OBJECTIVE_CLOSURE.md is incomplete: judge each of "
            f"{['F' + str(index) for index in missing]} exactly once "
            "(REAL/FALSO), then emit CLOSURE_READY.",
            env.repo, timeout_s)
        try:
            await env.executor.cleanup("closer")
        except Exception:
            pass
        text = _read(env.repo / ".tier-s" / "OBJECTIVE_CLOSURE.md") or _read(
            scratch / "OBJECTIVE_CLOSURE.md")
        verdicts = parse_closure(text)
    real: list[Any] = []
    for item in consolidated:
        verdict = verdicts.get(item.id, ("FALSO", "no verdict"))[0]
        reason = verdicts.get(item.id, ("", ""))[1]
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
        "arbiter", review_mod.arbiter_prompt(item, muse_reason, quote),
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
    return {
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


def _notify_ref(objective: Mapping[str, Any]) -> str:
    """ERP comments land on the conversion-source ticket (objectives have no
    business mirror of their own: no parent/group support in the adapter)."""
    return (str(objective.get("conversion_source_task_id") or "")
            or str(objective.get("objective_id")))


async def _finish_objective(
    env: ObjectiveEnv,
    objective: Mapping[str, Any],
) -> dict[str, Any]:
    """Close the objective: ERP note, vault note, objective-branch PR (§9.7)."""
    from agents.dev.tier_s import knowledge as knowledge_mod

    objective_id = str(objective.get("objective_id"))
    await env.objectives.patch_objective(
        objective_id, {"status": "closed", "closed_at": _utcnow_iso()})
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
