"""Single-ticket Tier S cycle (design §6, F2).

One ticket = one cycle = one builder session. The orchestrator verifies
(gates, structural plan checks, git diffs, SHAs); the LLM reasons. No step
that Python can do is delegated to a model.

Seams for later phases: review rounds + arbitration (F3) slot between the
build gates and the final gates; the findings inbox (F4) receives
out-of-scope verdicts; simple tiers (F5) reuse :mod:`gates`; objectives
(F6) add conversion, the ``PlannerResult`` planner and the capsule.
Live CLI executors
(tmux/process) implement :class:`Executor`; tests inject scripted fakes.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from agents.dev.routing import resolve_coding_tool
from agents.dev.tier_s_contract import (
    parse_plan_observations,
    parse_plan_responses,
    plan_response_gaps,
    verify_plan_anchor,
)
from agents.dev.tier_s import gates as gate_mod
from model_routing import review_wiring as wiring_mod
from objectives import engine as objectives_mod
from objectives import ticket_cost as cost_mod
from objectives.agent_result import AGENT_RESULT_PROMPT, parse_agent_result, scheduled_resume
from orchestration.provider_recovery import transient_provider_error
from orchestration import findings as findings_mod
from orchestration import inbox as inbox_mod
from orchestration import knowledge as knowledge_mod
from orchestration import review as review_mod
from orchestration.live import (
    TIMEOUT_RETURNCODE,
    AllowAllQuota,
    ExecResult,
    Executor,
    QuotaChecker,
)
from owner_channel import build_owner_question
from agents.dev.tier_s.models import (
    BUDGETS_DEFAULTS,
    REVIEW_DEFAULTS,
    SIGNALS,
    TIMEOUTS_DEFAULTS,
    WAITING_DEFAULTS,
    GateResult,
    GateSpec,
    SessionMetric,
    WaitState,
)
from agents.dev.tier_s.plan import (
    Plan,
    parse_plan,
    plan_declares_objective_needed,
    validate_plan,
)
from agents.dev.tier_s.stores import LockStore, RunStore, TaskStore
from agents.dev.tier_s.telemetry import (
    blank_ticket_metrics,
    bump_gate_retries,
    record_plan_review,
    record_session,
    record_sizes,
)
from state import CodingTool, TaskStatus

logger = logging.getLogger(__name__)

#: Dependency statuses that unblock a ticket (design §9.5: the enqueuer never
#: dispatches while a blocker is open; failed counts as open — no work done).
TERMINAL_BLOCKER_STATUS = frozenset({"completed", "converted", "cancelled"})

#: The Tier S engine reads roles from the same global block F0/F0.1 built.
_ROLE_KEYS = ("planner", "plan_reviewer", "plan-reviewer", "builder")

_ORCHESTRATOR_USER = ("OpenCEO", "openceo@jardal.app")

#: Scratch artifacts a review checkpoint carries back on resume (§14.2: the
#: approved plan and the written responses are reused, never redone).
_CHECKPOINT_ARTIFACTS = ("PLAN.md", "PLAN_RESPONSE.md", "HANDOFF.md", "WAIT.md")
_CHECKPOINT_ARTIFACT_CAP = 64000


@dataclass(frozen=True)
class CycleOutcome:
    status: str  # completed | waiting | blocked | failed
    run_id: str | None = None
    reason: str = ""
    detail: str = ""
    branch: str | None = None
    commit_sha: str | None = None
    resume_at: str | None = None  # ISO-8601 UTC when the wait self-heals
    cost_runs: tuple[dict[str, Any], ...] = ()  # sessions of THIS execution
    agent_result: dict[str, Any] | None = None


@dataclass
class _Ctx:
    ticket: dict[str, Any]
    repo: Path
    ref: str
    run_id: str
    branch: str
    base_branch: str
    budgets: dict[str, Any]
    timeouts: dict[str, int]
    waiting_cfg: dict[str, Any]
    auto_merge: bool
    budget_exclude: list[str]
    gate_specs: list[GateSpec]
    roles: dict[str, dict[str, Any]]
    metrics: dict[str, Any] = field(default_factory=blank_ticket_metrics)
    base_sha: str = ""
    build_sha: str = ""
    head_sha: str = ""  # latest wip (build → fix1 → fix2 → arbitration)
    final_sha: str = ""
    gate_runs: list[dict[str, Any]] = field(default_factory=list)
    lock_held: bool = False
    pool: list[Any] = field(default_factory=list)  # review.ReviewSlot
    reviewers_raw: list[dict[str, Any]] = field(default_factory=list)
    review_cfg: dict[str, Any] = field(default_factory=dict)
    inbox_rule: dict[str, Any] = field(default_factory=dict)
    fix_tiers: dict[str, str] = field(default_factory=dict)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    arbitrations: list[dict[str, Any]] = field(default_factory=list)
    inbox_fps: list[str] = field(default_factory=list)
    discarded_notes: list[str] = field(default_factory=list)
    used_review_roles: list[str] = field(default_factory=list)
    converted: str | None = None  # objective id after F6 conversion
    quorum_detail: str = ""  # per-slot story of the last failed round
    # Resume at review (design §14.2, TASK-2026-00162): set from the parked
    # run's checkpoint; review_parks counts no-quorum parks across resumes.
    resumed: bool = False
    resumes: int = 0
    review_parks: int = 0
    resume_artifacts: dict[str, str] = field(default_factory=dict)
    # Set once the cycle observes a business cancellation mid-flight
    # (TASK-2026-00177): parks and closes fence instead of acting.
    fenced_cancelled: bool = False
    # Sessions already folded into the ticket cost by a previous execution
    # (TASK-2026-00212): only sessions past this index travel as cost_runs,
    # so resumes never double-count.
    cost_sessions_base: int = 0
    resume_phase: str = "review"
    provider_parks: int = 0
    parked_outcome: CycleOutcome | None = None
    pool_resume_at: str | None = None
    vault_index: str = ""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", timeout=timeout,
    )


def _git_identity(repo: Path) -> list[str]:
    """Explicit identity so commits never depend on ambient git config."""
    name = _git(repo, "config", "user.name").stdout.strip()
    email = _git(repo, "config", "user.email").stdout.strip()
    if name and email:
        return []
    user, mail = _ORCHESTRATOR_USER
    return ["-c", f"user.name={user}", "-c", f"user.email={mail}"]


def _tier_dir(repo: Path) -> Path:
    return repo / ".openceo"


def _read_status(repo: Path) -> str:
    try:
        return (_tier_dir(repo) / "status").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _write_status(repo: Path, signal: str) -> None:
    _tier_dir(repo).mkdir(exist_ok=True)
    (_tier_dir(repo) / "status").write_text(signal + "\n", encoding="utf-8")


def _diff_sizes(repo: Path, base_sha: str, exclude: Sequence[str]) -> tuple[int, int]:
    """(files, lines) changed vs base, minus tests and budget_exclude."""
    proc = _git(repo, "diff", "--no-renames", "--numstat", "-z",
                base_sha, "--", ".")
    files = 0
    lines = 0
    for record in proc.stdout.split("\0"):
        fields = record.split("\t")
        if len(fields) < 3:
            continue
        added_s, deleted_s, path = fields[0], fields[1], fields[2]
        if not path:
            continue
        if path == ".openceo" or path.startswith(".openceo/"):
            continue
        try:
            added = int(added_s) if added_s != "-" else 0
            deleted = int(deleted_s) if deleted_s != "-" else 0
        except ValueError:
            continue
        lowered = path.lower()
        if lowered.startswith(("tests/", "test/")) or "/tests/" in lowered:
            continue
        if any(fnmatch.fnmatch(path, pat) for pat in exclude):
            continue
        files += 1
        lines += added + deleted
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for path in untracked.stdout.split("\0"):
        if not path or path == ".openceo" or path.startswith(".openceo/"):
            continue
        lowered = path.lower()
        if lowered.startswith(("tests/", "test/")) or "/tests/" in lowered:
            continue
        if any(fnmatch.fnmatch(path, pat) for pat in exclude):
            continue
        try:
            content = (repo / path).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            continue
        files += 1
        lines += len(content.splitlines())
    return files, lines


def _handoff_word_count(text: str) -> int:
    return len(text.split())


def _complete_handoff(
    text: str, sha: str, files_changed: Sequence[str], gates_section: str,
    findings_discarded: str = "none",
) -> str:
    """Fill the orchestrator-owned handoff lines (SHA, files, gates, FIX)."""
    files_line = ", ".join(files_changed) if files_changed else "none"
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("SHA:"):
            out.append(f"SHA: {sha}")
        elif line.startswith("Files changed:"):
            out.append(f"Files changed: {files_line}")
        elif line.startswith("Gates:"):
            out.append(gates_section.replace("\n", " | "))
        elif line.startswith("Findings discarded:"):
            out.append(f"Findings discarded: {findings_discarded}")
        else:
            out.append(line)
    if not any(line.startswith("SHA:") for line in out):
        out.insert(1, f"SHA: {sha}")
    if not any(line.startswith("Gates:") for line in out):
        out.append(gates_section.replace("\n", " | "))
    return "\n".join(out) + "\n"


# -- retry/follow-up prompts (one-shot: each carries the preamble) -----------


def _plan_retry_prompt(errors: str, signal: str, repo: Path) -> str:
    return knowledge_mod.with_preamble(
        f"Your PLAN.md is invalid: {errors}\nFix it in place "
        f"and emit {signal} again.", repo, "en")


def _plan_response_retry_prompt(
    missing: Sequence[int], signal: str, repo: Path,
) -> str:
    return knowledge_mod.with_preamble(
        f"PLAN_RESPONSE.md is incomplete: answer each of {list(missing)} "
        f"exactly once (ACEPTADA/RECHAZADA/DIFERIDA), then emit {signal}.",
        repo, "en")


def _handoff_fix_prompt(signal: str, repo: Path) -> str:
    return knowledge_mod.with_preamble(
        "Write .openceo/HANDOFF.md (executor sections ≤300 words) and emit "
        f"{signal} again. Do not touch code.", repo, "en")


def _gates_fix_prompt(tail: str, signal: str, repo: Path) -> str:
    return knowledge_mod.with_preamble(
        f"Gates failed:\n{tail[:6000]}\nFix the code (no commits), keep "
        f"HANDOFF.md truthful, and emit {signal} again.", repo, "en")


def _fix_retry_prompt(
    fix_name: str, missing: Sequence[int], round_no: int, repo: Path,
) -> str:
    return knowledge_mod.with_preamble(
        f"{fix_name} is incomplete: answer each of "
        f"{['F' + str(index) for index in missing]} exactly once "
        f"(CORREGIDO/DESCARTADO), then emit FIX_DONE_{round_no}.",
        repo, "en")


def _fix_gates_retry_prompt(round_no: int, tail: str, repo: Path) -> str:
    return knowledge_mod.with_preamble(
        f"Fix gates failed after FIX_{round_no}:\n{tail[:6000]}\n"
        "Fix the code (no commits), keep HANDOFF.md truthful, and "
        f"emit FIX_DONE_{round_no} again.", repo, "en")


def _arbiter_retry_prompt(repo: Path) -> str:
    return knowledge_mod.with_preamble(
        "Reply exactly one line: `REAL — <why>` or "
        "`FALSO — <verifiable technical reason>`.", repo, "en")


def _arbitration_fix_prompt(
    finding_id: int, category: str, problem: str, fix: str,
    reason: str, repo: Path,
) -> str:
    return knowledge_mod.with_preamble(
        f"Arbitration verdict REAL on F{finding_id} "
        f"([{category}] {problem[:300]} → "
        f"{fix[:300]}): {reason[:600]}\n"
        "Fix the code (no commits) and emit "
        "FIX_DONE_2 again.", repo, "en")


class TierSCycle:
    """The F2 orchestrator. Deterministic; every LLM call goes via Executor."""

    def __init__(
        self,
        run_store: RunStore,
        lock_store: LockStore,
        task_store: TaskStore,
        executor: Executor,
        quota: QuotaChecker | None = None,
        notify: Callable[[str, str], Awaitable[None]] | None = None,
        push: Callable[[Path, str], Awaitable[None]] | None = None,
        slots: review_mod.SlotStore | None = None,
        reviews: review_mod.ReviewStore | None = None,
        inbox: inbox_mod.InboxStore | None = None,
        create_ticket: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]] | None = None,
        objective_env: Any | None = None,
    ) -> None:
        self._runs = run_store
        self._locks = lock_store
        self._tasks = task_store
        self._exec = executor
        self._quota = quota or AllowAllQuota()
        self._notify = notify
        self._push = push
        # F3/F4 seams. Slot states default to memory (availability always
        # fresh); review persistence and the inbox stay off until wired, and
        # promotions wait for a ticket-creation callback (internal API).
        self._slots = slots or review_mod.InMemorySlotStore()
        self._reviews = reviews
        self._inbox = inbox
        self._create_ticket = create_ticket
        # F6: without an objective env, oversized tickets park in
        # waiting(needs_owner) with their SIZING.md (pre-F6 behavior).
        self._objective_env = objective_env

    # -- configuration ---------------------------------------------------

    @staticmethod
    def config_from_project(cfg: Mapping[str, Any]) -> dict[str, Any]:
        """Build the cycle config from a loaded project config + defaults.

        Cycle-only keys (budgets, waiting, its roles and timeouts) come from
        ``dev.tier_s``; the review machinery (pool, rounds, inbox, fix tiers,
        closer/arbiter roles and timeouts) comes from the shared global
        ``review`` block both engines read (TASK-2026-00141).
        """
        dev = dict(cfg.get("dev", {}) or {})
        tier_s = dict(dev.get("tier_s", {}) or {})
        review = dict(cfg.get("review", {}) or {})
        budgets = dict(BUDGETS_DEFAULTS)
        budgets.update(tier_s.get("budgets", {}) or {})
        timeouts = dict(TIMEOUTS_DEFAULTS)
        timeouts.update(tier_s.get("timeouts_minutes", {}) or {})
        timeouts.update(review.get("timeouts_minutes", {}) or {})
        waiting_cfg = dict(WAITING_DEFAULTS)
        waiting_cfg.update(tier_s.get("waiting", {}) or {})
        review_cfg = dict(REVIEW_DEFAULTS)
        review_cfg.update(review.get("review", {}) or {})
        inbox_cfg = review.get("inbox", {}) or {}
        repos = cfg.get("project", {}).get("repos", []) if isinstance(
            cfg.get("project"), Mapping) else []
        base_branch = "main"
        if repos and isinstance(repos[0], Mapping):
            base_branch = str(repos[0].get("branch_base", "main") or "main")
        return {
            "roles": {**dict(tier_s.get("roles", {}) or {}),
                      **dict(review.get("roles", {}) or {})},
            "reviewers": list(review.get("reviewers", []) or []),
            "budgets": budgets,
            "timeouts": timeouts,
            "waiting": waiting_cfg,
            "review": review_cfg,
            "inbox_rule": dict(inbox_cfg.get("promotion", {}) or {}),
            "fix_tiers": dict(review.get("fix_ticket_tiers", {}) or {}),
            "auto_merge": bool(dev.get("auto_merge", True)),
            "base_branch": base_branch,
            "budget_exclude": list(dev.get("budget_exclude", []) or []),
        }

    # -- main entry ------------------------------------------------------

    async def run(
        self,
        ticket: Mapping[str, Any],
        repo: Path,
        cycle_cfg: Mapping[str, Any],
        gate_specs: Sequence[GateSpec] | None = None,
        dev_cfg: Mapping[str, Any] | None = None,
    ) -> CycleOutcome:
        task_id = str(ticket.get("task_id", ""))
        ref = str(ticket.get("ref") or ticket.get("ticket_number")
                  or task_id[:8])
        resume = await self._resume_point(task_id, repo)
        checkpoint = (resume or {}).get("checkpoint") or {}
        ctx = _Ctx(
            ticket=dict(ticket), repo=repo, ref=ref,
            run_id=(str(resume["run_id"]) if resume
                    else f"ts-{uuid.uuid4().hex[:12]}"),
            branch=(str(checkpoint["branch"]) if resume
                    else f"tier-s/{ref}-{uuid.uuid4().hex[:8]}"),
            base_branch=str(cycle_cfg.get("base_branch", "main")),
            budgets=dict(cycle_cfg.get("budgets", BUDGETS_DEFAULTS)),
            timeouts=dict(cycle_cfg.get("timeouts", TIMEOUTS_DEFAULTS)),
            waiting_cfg=dict(cycle_cfg.get("waiting", WAITING_DEFAULTS)),
            auto_merge=bool(cycle_cfg.get("auto_merge", True)),
            budget_exclude=list(cycle_cfg.get("budget_exclude", []) or []),
            gate_specs=list(gate_specs) if gate_specs is not None
            else gate_mod.load_gates(dev_cfg),
            roles=dict(cycle_cfg.get("roles", {}) or {}),
            pool=list(review_mod.parse_pool(
                cycle_cfg.get("reviewers", []) or [])),
            reviewers_raw=[dict(entry)
                           for entry in cycle_cfg.get("reviewers", []) or []
                           if isinstance(entry, Mapping)],
            review_cfg={**REVIEW_DEFAULTS,
                        **dict(cycle_cfg.get("review", {}) or {})},
            inbox_rule=dict(cycle_cfg.get("inbox_rule", {}) or {}),
            fix_tiers=dict(cycle_cfg.get("fix_tiers", {}) or {}),
            vault_index=str(cycle_cfg.get("vault_index", "") or ""),
        )
        if resume is not None:
            self._restore_checkpoint(ctx, resume)
        ctx.cost_sessions_base = len(ctx.metrics.get("sessions") or [])
        try:
            outcome = await self._run_guarded(ctx)
            return replace(outcome, cost_runs=self._cost_delta(ctx))
        except Exception as exc:  # fail explicit, resumable, lock always freed
            detail = f"{type(exc).__name__}: {exc}"[:500]
            parked = await self._park(
                ctx, "transient", detail,
                resume_in_min=self._backoff(ctx, 0), patch_run=True,
                notify=False, run_status="failed")
            return CycleOutcome(
                status="failed", run_id=ctx.run_id,
                reason="transient", detail=detail,
                resume_at=parked.resume_at,
                cost_runs=self._cost_delta(ctx))
        finally:
            await self._cleanup(ctx)

    def _cost_delta(self, ctx: _Ctx) -> tuple[dict[str, Any], ...]:
        """Sessions recorded by this execution as ticket cost runs."""
        sessions = ctx.metrics.get("sessions") or []
        fresh = list(sessions)[max(0, ctx.cost_sessions_base):]
        return tuple(cost_mod.run_from_session(dict(item))
                     for item in fresh if isinstance(item, Mapping))

    # -- guarded flow ----------------------------------------------------

    async def _run_guarded(self, ctx: _Ctx) -> CycleOutcome:
        task_id = str(ctx.ticket.get("task_id", ""))
        # §6.1 admission: tier_s only, no open blockers.
        decision = resolve_coding_tool(ctx.ticket)
        if decision.tool is not CodingTool.TIER_S:
            await self._tasks.patch_task(task_id, {
                "status": TaskStatus.WAITING.value,
                "waiting_reason": f"routing_missing: {decision.detail}",
                "resume_at": None,
            })
            await self._say(ctx, f"routing_missing: {decision.detail}")
            return CycleOutcome(
                status="blocked", reason="routing_missing",
                detail=decision.detail)
        for blocker in ctx.ticket.get("blocked_by", []) or []:
            status = await self._tasks.task_status(str(blocker))
            if status not in TERMINAL_BLOCKER_STATUS:
                await self._tasks.patch_task(task_id, {
                    "status": TaskStatus.WAITING.value,
                    "waiting_reason": f"blocked_by: {blocker} ({status})",
                    "resume_at": None,
                })
                return CycleOutcome(
                    status="blocked", reason="blocked_by",
                    detail=f"{blocker} ({status})")

        roster = self._freeze_roster(ctx)
        ctx.metrics["tier"] = CodingTool.TIER_S.value
        ctx.metrics["origin"] = ctx.ticket.get("origin")
        # One task, one open run (TASK-2026-00162, pilot: 16 orphaned
        # waiting runs): every other open run of the task is superseded —
        # only a resumed run is kept. Before create_run, so a dead run's
        # repo marker cannot refuse this one.
        await self._runs.supersede_runs(task_id, ctx.run_id)
        if ctx.resumed:
            await self._runs.patch_run(ctx.run_id, {
                "status": "running", "phase": ctx.resume_phase, "waiting": None,
                "resumes": ctx.resumes, "active_repo": str(ctx.repo),
                "updated_at": _utcnow_iso()})
        else:
            await self._runs.create_run({
                "run_id": ctx.run_id,
                "task_id": task_id,
                "project": ctx.ticket.get("project"),
                "objective_id": ctx.ticket.get("objective_id"),
                "engine": "tier_s",
                "status": "running",
                "phase": "planning",
                "roster": roster,
                "branch": ctx.branch,
                "metrics": ctx.metrics,
                "active_repo": str(ctx.repo),
                "created_at": _utcnow_iso(),
                "updated_at": _utcnow_iso(),
            })
        await self._tasks.patch_task(task_id, {
            "status": TaskStatus.IN_PROGRESS.value,
            "waiting_reason": None,
            "resume_at": None,
        })

        holder = f"tier-s:{ctx.run_id}"
        if not await self._locks.acquire(str(ctx.repo), holder):
            return await self._park(
                ctx, "transient", f"repo_busy: {ctx.repo}",
                resume_in_min=self._backoff(ctx, 0), patch_run=True,
                notify=False)
        ctx.lock_held = True

        # §6.1 preflight: commands, config, scarce quota, review pool.
        problems = self._preflight(ctx)
        if problems:
            return await self._park(
                ctx, "config", "; ".join(problems), patch_run=True)
        # The scarce planner/plan-reviewer accounts only gate a fresh run:
        # a resume never calls them (arbitration checks its own quota).
        quota_hit = None if ctx.resumed else await self._quota_gate(ctx)
        if quota_hit is not None:
            return await self._park(
                ctx, "quota", quota_hit[1], resume_at=quota_hit[0],
                patch_run=True)
        pool_block = await self._pool_gate(ctx)
        if pool_block is not None:
            return await self._park(
                ctx, "review_quorum" if ctx.pool_resume_at else "config", pool_block,
                resume_at=ctx.pool_resume_at, patch_run=True)
        if ctx.resumed:
            return await self._resume_review(ctx)

        objective_id = str(ctx.ticket.get("objective_id") or "")
        if objective_id and not ctx.auto_merge:
            # Objective tickets integrate into objective/<id> (§12): prepare
            # the branch (merge the base, never rebase) and work from it.
            try:
                await asyncio.to_thread(
                    objectives_mod.checkout_and_update_objective_branch,
                    ctx.repo, objective_id, ctx.base_branch)
            except objectives_mod.ObjectiveBranchConflict as exc:
                return await self._park(
                    ctx, "needs_owner", f"merge_conflict: {exc}",
                    patch_run=True)
            ctx.base_branch = f"objective/{objective_id}"
        if not self._open_branch(ctx):
            return await self._park(
                ctx, "transient", "cannot open work branch", patch_run=True)
        ctx.base_sha = _git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
        (_tier_dir(ctx.repo) / "WAIT.md").unlink(missing_ok=True)
        await self._runs.patch_run(ctx.run_id, {
            "base_sha": ctx.base_sha, "updated_at": _utcnow_iso()})

        # §6.1 baseline: required gates run on the clean base first.
        baseline = await asyncio.to_thread(
            gate_mod.run_gates, ctx.repo, ctx.gate_specs)
        self._record_gates(ctx, "baseline", baseline)
        blocked = gate_mod.baseline_blocked(baseline, ctx.gate_specs)
        if blocked:
            names = ", ".join(item.name for item in blocked)
            return await self._park(
                ctx, "needs_owner", f"baseline_red: {names}",
                patch_run=True, drop_branch=True)

        plan = await self._planning(ctx)
        if ctx.converted:  # F6: the ticket became an objective mid-planning
            return CycleOutcome(
                status="completed", run_id=ctx.run_id, reason="converted",
                detail=str(ctx.converted), branch=ctx.branch)
        if plan is None:  # parked inside (OBJECTIVE_NEEDED / invalid / crash)
            run = await self._run_waited(ctx)
            return run
        # Non-blocking by design (§6.4): gaps become SIN_RESPUESTA telemetry.
        await self._plan_review(ctx, plan)
        built = await self._build(ctx, plan)
        if ctx.converted:  # F6: size brake converted the ticket
            return CycleOutcome(
                status="completed", run_id=ctx.run_id, reason="converted",
                detail=str(ctx.converted), branch=ctx.branch)
        if not built:
            return await self._run_waited(ctx)
        return await self._close(ctx)

    # -- admission helpers ------------------------------------------------

    def _freeze_roster(self, ctx: _Ctx) -> dict[str, Any]:
        roles = ctx.roles
        reviewer = roles.get("plan_reviewer") or roles.get("plan-reviewer") or {}
        return {
            "planner": dict(roles.get("planner", {})),
            "plan_reviewer": dict(reviewer),
            "builder": dict(roles.get("builder", {})),
            "pool": [
                {"label": slot.label, "family": slot.family,
                 "model": slot.model}
                for slot in ctx.pool if slot.active
            ],
        }

    async def _pool_gate(self, ctx: _Ctx) -> str | None:
        """Preflight: ≥3 eligible families when a pool is configured (§6.1).

        No pool configured at all means review machinery was never set up:
        rounds are skipped with a telemetry note (same pattern as gates),
        and the cycle still delivers plan + build + gates + handoff.
        """
        if not ctx.pool:
            ctx.metrics["review"] = {"rounds": 0,
                                     "skipped": "no pool configured"}
            return None
        for slot in ctx.pool:
            warning = review_mod.expiry_warning(slot)
            if warning:
                await self._say(ctx, warning)
        families = await review_mod.available_families(
            ctx.pool, self._slots,
            builder_family=self._builder_family(ctx))
        need = int(ctx.review_cfg.get("min_available_families", 3))
        if len(families) < need:
            potential = set()
            for slot in ctx.pool:
                state = await self._slots.get_slot(slot.label)
                eligible, _ = review_mod.slot_available(slot, {})
                if (eligible and state.get("status") != "disabled"
                        and not slot.extra and slot.family != self._builder_family(ctx)):
                    potential.add(slot.family)
            if len(potential) >= need:
                ctx.pool_resume_at = (datetime.now(timezone.utc) + timedelta(
                    minutes=self._backoff(ctx, 0))).isoformat()
            return (f"review pool has {len(families)} eligible families "
                    f"({sorted(families) or 'none'}), need {need}")
        return None

    @staticmethod
    def _builder_family(ctx: _Ctx) -> str:
        """Family of the builder model (self-review rule, §7.3)."""
        builder = ctx.roles.get("builder", {}) or {}
        model = str(builder.get("model") or "muse-spark").lower()
        if "muse" in model or "spark" in model:
            return "muse"
        for slot in ctx.pool:
            if slot.model and slot.model.lower() in model:
                return slot.family
        return "muse"

    def _preflight(self, ctx: _Ctx) -> list[str]:
        problems: list[str] = []
        roles = ctx.roles
        reviewer = roles.get("plan_reviewer") or roles.get("plan-reviewer")
        for key, role in (("planner", roles.get("planner")),
                          ("plan_reviewer", reviewer),
                          ("builder", roles.get("builder"))):
            if not isinstance(role, Mapping):
                problems.append(f"missing tier_s role {key}")
                continue
            if role.get("active") is False:
                problems.append(f"tier_s role {key} inactive")
                continue
            cmd = role.get("command", [])
            argv0 = cmd[0] if isinstance(cmd, Sequence) and cmd else ""
            if not argv0 or shutil.which(str(argv0)) is None:
                problems.append(f"tier_s role {key}: command not found")
        gate_errors = gate_mod.validate_gates_config({"gates": [
            {"name": spec.name, "cmd": spec.cmd,
             "timeout_minutes": spec.timeout_minutes,
             "required": spec.required, "baseline_red": spec.baseline_red}
            for spec in ctx.gate_specs]})
        problems.extend(f"gates: {err}" for err in gate_errors)
        try:
            if int(ctx.budgets.get("files_per_ticket", 0)) <= 0:
                problems.append("budgets.files_per_ticket must be > 0")
            if int(ctx.budgets.get("lines_per_ticket", 0)) <= 0:
                problems.append("budgets.lines_per_ticket must be > 0")
        except (TypeError, ValueError):
            problems.append("budgets must be numeric")
        try:
            tokens_budget = int(ctx.budgets.get("agents_md_tokens", 3000))
        except (TypeError, ValueError):
            tokens_budget = 3000
        ctx.metrics["agents_md"] = knowledge_mod.check_agents_md(
            ctx.repo, tokens_budget)
        return problems

    async def _quota_gate(self, ctx: _Ctx) -> tuple[str | None, str] | None:
        """Scarce accounts (planner + plan reviewer) before the cycle (§4.3)."""
        accounts: list[str] = []
        for key in ("planner", "plan_reviewer", "plan-reviewer"):
            role = ctx.roles.get(key)
            if isinstance(role, Mapping) and role.get("account"):
                accounts.append(str(role["account"]))
        verdicts = await self._quota.check(accounts)
        for account, verdict in verdicts.items():
            if not verdict.ok:
                return verdict.resume_at, (
                    f"quota: {account} exhausted"
                    + (f" ({verdict.detail})" if verdict.detail else ""))
        return None

    def _backoff(self, ctx: _Ctx, attempt: int) -> int:
        steps = ctx.waiting_cfg.get("backoff_minutes", [15, 30, 60]) or [15]
        return int(steps[min(attempt, len(steps) - 1)])

    # -- resume at review (design §14.2, TASK-2026-00162) ------------------

    async def _resume_point(
        self, task_id: str, repo: Path,
    ) -> dict[str, Any] | None:
        try:
            run = await self._runs.resumable_run(task_id)
            checkpoint = (run or {}).get("checkpoint") or {}
            branch = str(checkpoint.get("branch") or "")
            head = str(checkpoint.get("head_sha") or "")
            base = str(checkpoint.get("base_sha") or "")
            if not (run and run.get("run_id") and branch and head and base):
                return None
            tip = _git(repo, "rev-parse", "--verify", "--quiet",
                       f"refs/heads/{branch}").stdout.strip()
            if tip != head:
                return None
            if _git(repo, "cat-file", "-e",
                    f"{base}^{{commit}}").returncode != 0:
                return None
        except Exception:  # no resume is always safe: a fresh run follows
            return None
        return run

    def _restore_checkpoint(self, ctx: _Ctx, run: Mapping[str, Any]) -> None:
        checkpoint = dict(run.get("checkpoint") or {})
        ctx.resumed = True
        ctx.resume_phase = str(checkpoint.get("phase") or "review")
        ctx.provider_parks = int(checkpoint.get("provider_parks") or 0)
        ctx.resumes = int(run.get("resumes") or 0) + 1
        ctx.review_parks = int(checkpoint.get("review_parks") or 0)
        ctx.base_branch = str(checkpoint.get("base_branch") or ctx.base_branch)
        ctx.base_sha = str(checkpoint.get("base_sha") or "")
        ctx.head_sha = str(checkpoint.get("head_sha") or "")
        ctx.build_sha = str(checkpoint.get("build_sha") or ctx.head_sha)
        ctx.resume_artifacts = {
            str(name): str(text)
            for name, text in dict(checkpoint.get("artifacts") or {}).items()
            if name in _CHECKPOINT_ARTIFACTS}
        if isinstance(run.get("metrics"), Mapping):
            ctx.metrics = dict(run["metrics"])
        ctx.gate_runs = [dict(item) for item in run.get("gates") or []
                         if isinstance(item, Mapping)]
        ctx.rounds = [dict(item) for item in run.get("rounds") or []
                      if isinstance(item, Mapping)]

    def _review_checkpoint(self, ctx: _Ctx) -> dict[str, Any]:
        artifacts: dict[str, str] = {}
        for name in _CHECKPOINT_ARTIFACTS:
            text = self._read_artifact(ctx, name)
            if text:
                artifacts[name] = text[:_CHECKPOINT_ARTIFACT_CAP]
        return {
            "phase": "review",
            "branch": ctx.branch,
            "base_branch": ctx.base_branch,
            "base_sha": ctx.base_sha,
            "build_sha": ctx.build_sha,
            "head_sha": ctx.head_sha or ctx.build_sha,
            "review_parks": ctx.review_parks,
            "provider_parks": ctx.provider_parks,
            "artifacts": artifacts,
            "at": _utcnow_iso(),
        }

    async def _resume_review(self, ctx: _Ctx) -> CycleOutcome:
        checked_out = _git(ctx.repo, "checkout", "-q", ctx.branch).returncode == 0
        head = _git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
        if not checked_out or head != ctx.head_sha:
            await self._runs.patch_run(ctx.run_id, {
                "checkpoint": None, "updated_at": _utcnow_iso()})
            return await self._park(
                ctx, "transient", f"cannot resume {ctx.branch} at "
                f"{ctx.head_sha[:12]} (fresh run next)",
                resume_in_min=self._backoff(ctx, 0), patch_run=True,
                notify=False)
        tier_dir = _tier_dir(ctx.repo)
        tier_dir.mkdir(exist_ok=True)
        for name, text in ctx.resume_artifacts.items():
            (tier_dir / name).write_text(text, encoding="utf-8")
        if ctx.resume_phase == "build":
            plan = parse_plan(self._read_artifact(ctx, "PLAN.md"))
            if plan is None:
                return await self._park(ctx, "config", "checkpoint has no valid plan", patch_run=True)
            if not await self._build(ctx, plan):
                return await self._run_waited(ctx)
        return await self._close(ctx)

    async def _park_build(
        self, ctx: _Ctx, reason: str, detail: str,
        *, resume_at: str | None = None, agent_result: dict[str, Any] | None = None,
    ) -> None:
        fenced = await self._fenced_cancelled(ctx)
        if fenced is not None:
            ctx.parked_outcome = fenced
            return
        if agent_result is not None:
            _tier_dir(ctx.repo).mkdir(exist_ok=True)
            (_tier_dir(ctx.repo) / "WAIT.md").write_text(
                json.dumps(agent_result, ensure_ascii=False), encoding="utf-8")
        self._commit_if_changed(ctx, f"checkpoint {ctx.ref}: {reason}")
        ctx.head_sha = ctx.head_sha or ctx.base_sha
        checkpoint = self._review_checkpoint(ctx)
        checkpoint["phase"] = "build"
        await self._runs.patch_run(ctx.run_id, {"checkpoint": checkpoint})
        if _git(ctx.repo, "checkout", "-q", ctx.base_branch).returncode != 0:
            resume_at = None
            detail = f"checkpoint saved but cannot release branch: {detail}"
            reason = "config"
        outcome = await self._park(ctx, reason, detail, resume_at=resume_at,
                                   patch_run=True, notify=True)
        if agent_result is not None:
            ctx.parked_outcome = replace(outcome, agent_result=agent_result)

    async def _park_builder_failure(self, ctx: _Ctx, result: ExecResult) -> None:
        ctx.provider_parks += 1
        transient = transient_provider_error(
            result.output_tail, responded=bool((result.usage or {}).get("model_responded")))
        resume_at = None
        if transient and ctx.provider_parks <= 3:
            resume_at = (datetime.now(timezone.utc) + timedelta(
                minutes=self._backoff(ctx, ctx.provider_parks - 1))).isoformat()
        detail = f"builder failed: {result.output_tail[-600:]}"
        verdict = None if resume_at else {
            "resultado": "bloqueado", "motivo": detail,
            "necesidad": "Resolver el fallo del proveedor antes de continuar este checkpoint"}
        await self._park_build(
            ctx, "transient" if resume_at else "agent_blocked", detail,
            resume_at=resume_at, agent_result=verdict)

    async def _park_review_quorum(self, ctx: _Ctx, round_no: int) -> CycleOutcome:
        """No quorum: checkpoint, backoff 15/30/60, then needs_owner (§14.2).

        The checkpoint makes the next launch resume this run at review (no
        new planning or build). Parks are counted across resumes; past the
        last backoff step the pool is broken beyond self-healing and the
        ticket stops retrying instead of burning quota (no livelock).
        """
        ctx.review_parks += 1
        await self._runs.patch_run(ctx.run_id, {
            "checkpoint": self._review_checkpoint(ctx),
            "updated_at": _utcnow_iso()})
        detail = f"round {round_no} without quorum: {ctx.quorum_detail}"
        steps = ctx.waiting_cfg.get("backoff_minutes", [15, 30, 60]) or [15]
        if ctx.review_parks > len(steps):
            return await self._park(
                ctx, "needs_owner",
                f"review pool short of quorum {ctx.review_parks} times; "
                "restore the reviewer pool, then relaunch (resumes at "
                f"review). Last: {detail}",
                patch_run=True)
        return await self._park(
            ctx, "review_quorum", detail,
            resume_in_min=self._backoff(ctx, ctx.review_parks - 1),
            patch_run=True, notify=False)

    async def _convert(
        self, ctx: _Ctx, cause: str, sizing: str, role: str) -> None:
        """F6: kill the session, create the objective, launch its planner.

        Sets ``ctx.converted``; the guarded flow returns a converted outcome
        and the task closes via :func:`objectives.convert_ticket`.
        """
        try:
            await self._exec.cleanup(role)
        except Exception:
            pass
        try:
            objective = await objectives_mod.convert_ticket(
                self._objective_env, ctx.ticket, cause, sizing)
        except Exception as exc:
            await self._park(
                ctx, "needs_owner", f"conversion failed: {exc}",
                patch_run=True, drop_branch=True)
            return
        ctx.converted = str(objective.get("objective_id"))
        await self._runs.patch_run(ctx.run_id, {
            "status": "converted", "objective_id": ctx.converted,
            "metrics": ctx.metrics, "updated_at": _utcnow_iso()})
        await asyncio.to_thread(self._drop_branch, ctx)

    # -- git ---------------------------------------------------------------

    def _open_branch(self, ctx: _Ctx) -> bool:
        base = ctx.base_branch
        if _git(ctx.repo, "rev-parse", "--verify", base).returncode != 0:
            return False
        if _git(ctx.repo, "checkout", "-b", ctx.branch, base).returncode != 0:
            _git(ctx.repo, "checkout", "-q", base)
            return False
        return True

    def _drop_branch(self, ctx: _Ctx) -> None:
        _git(ctx.repo, "checkout", "-q", ctx.base_branch)
        _git(ctx.repo, "branch", "-D", ctx.branch)

    def _changed_files(self, ctx: _Ctx) -> list[str]:
        proc = _git(ctx.repo, "diff", "--name-only", "-z", ctx.base_sha, "--", ".")
        names = [part for part in proc.stdout.split("\0") if part]
        untracked = _git(
            ctx.repo, "ls-files", "--others", "--exclude-standard", "-z")
        names.extend(part for part in untracked.stdout.split("\0") if part)
        return sorted({name for name in names
                       if name != ".openceo" and not name.startswith(".openceo/")})

    def _commit(self, ctx: _Ctx, message: str) -> str:
        identity = _git_identity(ctx.repo)
        _git(ctx.repo, "add", "-A", "--", ".", ":!.openceo")
        proc = _git(ctx.repo, *identity, "commit", "-qm", message)
        if proc.returncode != 0:
            raise RuntimeError(f"commit failed: {proc.stderr.strip()[:300]}")
        return _git(ctx.repo, "rev-parse", "HEAD").stdout.strip()

    # -- planning (§6.2–§6.4) -------------------------------------------------

    async def _planning(self, ctx: _Ctx) -> Plan | None:
        tier_dir = _tier_dir(ctx.repo)
        tier_dir.mkdir(exist_ok=True)
        prompt = self._planner_prompt(ctx)
        timeout_s = int(ctx.timeouts.get("planner", 20)) * 60
        for attempt in range(2):
            result = await self._exec.run("planner", prompt, ctx.repo, timeout_s)
            self._record_session(
                ctx, "planner", "planner", result)
            if result.returncode != 0:
                await self._park(
                    ctx, "transient" if transient_provider_error(result.output_tail) else "config",
                    f"planner failed: {result.output_tail[-600:]}",
                    resume_in_min=self._backoff(ctx, 0) if transient_provider_error(result.output_tail) else None,
                    patch_run=True, drop_branch=True)
                return None
            status = _read_status(ctx.repo)
            if plan_declares_objective_needed(status or result.signal or ""):
                sizing = self._read_artifact(ctx, "SIZING.md")
                if self._objective_env is not None:
                    await self._convert(
                        ctx, "OBJECTIVE_NEEDED", sizing, "planner")
                    return None
                await self._park(
                    ctx, "needs_owner",
                    "OBJECTIVE_NEEDED (no objective env wired)",
                    patch_run=True, drop_branch=True,
                    extra_comment=f"SIZING.md:\n{sizing}" if sizing else "")
                return None
            plan_text = self._read_artifact(ctx, "PLAN.md")
            plan = parse_plan(plan_text) if plan_text else None
            validation = validate_plan(
                plan, ctx.repo, int(ctx.budgets.get("files_per_ticket", 8)),
                ctx.budget_exclude) if plan else None
            if plan is not None and validation is not None and validation.valid:
                await self._runs.patch_run(ctx.run_id, {
                    "phase": "plan_review",
                    "plan": {"files_declared": len(plan.files),
                             "lines_estimated": plan.estimate_lines,
                             "files_counted": validation.files_counted},
                    "updated_at": _utcnow_iso(),
                })
                return plan
            errors = "; ".join(validation.errors) if validation else "no PLAN.md"
            if "file budget exceeded" in errors:
                if self._objective_env is not None:
                    await self._convert(
                        ctx, f"plan exceeds file budget ({errors})",
                        plan_text or "", "planner")
                    return None
                await self._park(
                    ctx, "needs_owner",
                    f"plan exceeds file budget ({errors}; "
                    "no objective env wired)",
                    patch_run=True, drop_branch=True)
                return None
            if attempt == 0:
                prompt = _plan_retry_prompt(
                    errors, SIGNALS["plan_ready"], ctx.repo)
                continue
            await self._park(
                ctx, "needs_owner", f"invalid plan after retry: {errors}",
                patch_run=True, drop_branch=True)
            return None
        await self._park(ctx, "transient", "planner unreachable",
                         resume_in_min=self._backoff(ctx, 0), patch_run=True)
        return None

    async def _plan_review(self, ctx: _Ctx, plan: Plan) -> None:
        plan_path = _tier_dir(ctx.repo) / "PLAN.md"
        timeout_s = int(ctx.timeouts.get("plan_review", 15)) * 60
        review = await self._exec.run(
            "plan_reviewer", self._plan_review_prompt(ctx, plan),
            ctx.repo, timeout_s)
        self._record_session(ctx, "plan_reviewer", "plan_reviewer", review)
        observations = parse_plan_observations(
            self._read_artifact(ctx, "PLAN_REVIEW.md")) if review.returncode == 0 else []
        verified = [obs for obs in observations
                    if verify_plan_anchor(obs, plan_path, ctx.repo)]
        response = await self._exec.run(
            "planner", self._plan_response_prompt(ctx, verified),
            ctx.repo, timeout_s)
        self._record_session(ctx, "planner", "planner", response)
        responses = parse_plan_responses(
            self._read_artifact(ctx, "PLAN_RESPONSE.md")) if response.returncode == 0 else {}
        missing, _dup = plan_response_gaps(verified, responses)
        if missing:
            retry = await self._exec.run(
                "planner",
                _plan_response_retry_prompt(
                    missing, SIGNALS["plan_responded"], ctx.repo),
                ctx.repo, timeout_s)
            self._record_session(ctx, "planner", "planner", retry)
            responses = parse_plan_responses(
                self._read_artifact(ctx, "PLAN_RESPONSE.md")) if retry.returncode == 0 else {}
            missing, _dup = plan_response_gaps(verified, responses)
        accepted = sum(1 for obs in verified
                       if responses.get(obs.id, [""])[0] == "ACEPTADA")
        rejected = sum(1 for obs in verified
                       if responses.get(obs.id, [""])[0] == "RECHAZADA")
        deferred = sum(1 for obs in verified
                       if responses.get(obs.id, [""])[0] == "DIFERIDA")
        record_plan_review(ctx.metrics, len(verified), accepted, rejected,
                           deferred, len(missing))
        await self._runs.patch_run(ctx.run_id, {
            "phase": "building",
            "plan_review": {"observations": len(verified),
                            "accepted": accepted, "rejected": rejected,
                            "deferred": deferred, "no_response": len(missing)},
            "metrics": ctx.metrics,
            "updated_at": _utcnow_iso(),
        })

    # -- build (§6.5–§6.6) ----------------------------------------------------

    async def _build(self, ctx: _Ctx, plan: Plan) -> bool:
        timeout_s = int(ctx.timeouts.get("build", 60)) * 60
        result = await self._exec.run(
            "builder", self._builder_prompt(ctx, plan), ctx.repo, timeout_s)
        self._record_session(ctx, "builder", "builder", result)
        current = _git(ctx.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if current != ctx.branch:
            await self._park(ctx, "needs_owner",
                             f"builder left branch {ctx.branch} for {current}",
                             patch_run=True)
            return False
        if result.returncode != 0:
            await self._park_builder_failure(ctx, result)
            return False
        verdict = parse_agent_result(result.output_tail)
        if verdict is None and "```agent-result" in result.output_tail:
            await self._park_build(ctx, "config", "invalid builder agent-result")
            return False
        if verdict is not None and verdict.resultado.value in {"bloqueado", "no_aplica"}:
            try:
                resume_at = scheduled_resume(verdict)
            except ValueError as exc:
                await self._park_build(ctx, "config", str(exc))
                return False
            await self._park_build(
                ctx, "scheduled_wait" if resume_at else "agent_blocked", verdict.motivo,
                resume_at=resume_at.isoformat() if resume_at else None,
                agent_result=verdict.model_dump(mode="json"))
            return False
        head = _git(ctx.repo, "rev-parse", "HEAD").stdout.strip()
        if head != ctx.base_sha:
            _git(ctx.repo, "reset", "--soft", ctx.base_sha)
        if not self._changed_files(ctx):
            if not (verdict is not None and verdict.resultado.value == "hecho"
                    and verdict.entregables and verdict.evidencia.strip()):
                await self._park(ctx, "needs_owner", "builder produced no changes",
                                 patch_run=True, drop_branch=True)
                return False
        handoff = self._read_artifact(ctx, "HANDOFF.md")
        if not handoff or _handoff_word_count(handoff) > 600:
            fix = await self._exec.run(
                "builder", _handoff_fix_prompt(SIGNALS["build_done"], ctx.repo),
                ctx.repo, timeout_s)
            self._record_session(ctx, "builder", "builder", fix)
            if fix.returncode != 0:
                await self._park_builder_failure(ctx, fix)
                return False
            handoff = self._read_artifact(ctx, "HANDOFF.md")
            if not handoff:
                await self._park(ctx, "needs_owner", "missing HANDOFF.md",
                                 patch_run=True)
                return False
        # Gates with retries on the same session (§6.6).
        retries = int(ctx.budgets.get("gate_retries", 2))
        attempt = 0
        while True:
            results = await asyncio.to_thread(
                gate_mod.run_gates, ctx.repo, ctx.gate_specs)
            self._record_gates(ctx, "build" if attempt == 0 else f"fix{attempt}",
                               results)
            failed = gate_mod.gates_failed(results)
            if not failed:
                break
            if attempt >= retries:
                names = ", ".join(item.name for item in failed)
                await self._park(
                    ctx, "needs_owner", f"gates failed after {retries} retries: "
                    f"{names}", patch_run=True)
                return False
            bump_gate_retries(ctx.metrics)
            tail = "\n\n".join(
                f"[{item.name}] {item.status}: {item.detail}\n{item.output_tail}"
                for item in failed)
            fix = await self._exec.run(
                "builder",
                _gates_fix_prompt(tail, SIGNALS["build_done"], ctx.repo),
                ctx.repo, timeout_s)
            self._record_session(ctx, "builder", "builder", fix)
            if fix.returncode != 0:
                await self._park_builder_failure(ctx, fix)
                return False
            attempt += 1
        # Deterministic size brake on the real diff (§6.6).
        files_real, lines_real = _diff_sizes(
            ctx.repo, ctx.base_sha, ctx.budget_exclude)
        record_sizes(ctx.metrics, plan.estimate_files, plan.estimate_lines,
                     files_real, lines_real)
        brake = (int(ctx.budgets.get("hard_brake_factor", 2))
                 * int(ctx.budgets.get("lines_per_ticket", 400)))
        if lines_real > brake:
            _git(ctx.repo, "reset", "--hard", "-q", ctx.base_sha)
            _git(ctx.repo, "clean", "-fdq")
            if self._objective_env is not None:
                await self._convert(
                    ctx,
                    f"size brake: {lines_real} lines > {brake} "
                    "(work discarded)",
                    "", "builder")
                return False
            await self._park(
                ctx, "needs_owner",
                f"size brake: {lines_real} lines > {brake} "
                "(work discarded; no objective env wired)",
                patch_run=True, drop_branch=True)
            return False
        ctx.build_sha = (self._commit(ctx, f"wip({ctx.ref}) build")
                         if self._changed_files(ctx) else ctx.base_sha)
        ctx.head_sha = ctx.build_sha
        await self._runs.patch_run(ctx.run_id, {
            "head_sha": ctx.build_sha, "metrics": ctx.metrics,
            "updated_at": _utcnow_iso(),
        })
        return True

    # -- review rounds (§6.7–§6.9, F3) ---------------------------------------------

    def _review_summary(self, ctx: _Ctx) -> str:
        metrics = ctx.metrics.get("review") or {}
        if metrics.get("skipped"):
            return f"Review skipped ({metrics['skipped']})."
        return (f"Review: {metrics.get('rounds', 0)} rounds, "
                f"{metrics.get('fixed', 0)} fixed, "
                f"{metrics.get('discarded', 0)} discarded, "
                f"{metrics.get('arbitrations', 0)} arbitrations.")

    @staticmethod
    def _r1_result(
        pool: Sequence[Any],
        routing: wiring_mod.RoundRouting | None,
        round1: Mapping[str, Any],
    ) -> wiring_mod.R1Result:
        """Valid R1 families plus tried ones for the §11.3 R2 policy."""
        by_label = {slot.label: slot for slot in pool}
        receipts: list[wiring_mod.ReviewReceipt] = []
        fams: set[str] = set()
        for item in round1.get("receipts") or []:
            if not isinstance(item, dict):
                continue
            receipt = wiring_mod.ReviewReceipt.from_dict(item)
            receipts.append(receipt)
            fams.add(receipt.family_id)
        for label, ok in (round1.get("valid") or {}).items():
            if not ok or label in {item.slot for item in receipts}:
                continue
            slot = by_label.get(label)
            if slot is None or not slot.family or slot.extra:
                continue
            fams.add(slot.family)
        tried = set(fams)
        if routing is not None:
            for state in routing.states.values():
                tried |= set(state.tried_families)
        return wiring_mod.R1Result(
            receipts=tuple(receipts),
            round_families=frozenset(fams), tried_families=frozenset(tried))

    async def _review_rounds(self, ctx: _Ctx) -> CycleOutcome | None:
        """R1 → FIX → R2 → FIX → arbitration. None = proceed to final gates."""
        if not ctx.pool:
            return None
        builder_family = self._builder_family(ctx)
        quorum = int(ctx.review_cfg.get("quorum", 2))
        receipts_dir = _tier_dir(ctx.repo) / "review_receipts"

        await self._runs.patch_run(ctx.run_id, {
            "phase": "round1", "updated_at": _utcnow_iso()})
        try:
            assembly1 = wiring_mod.assemble_review_pool(
                ctx.reviewers_raw, review_cfg=ctx.review_cfg,
                run_id=ctx.run_id, round="1",
                builder_family=builder_family, receipts_dir=receipts_dir)
        except wiring_mod.ReviewRoutingError as exc:
            return await self._park(
                ctx, "config", f"free-router: {exc}", patch_run=True)
        wiring_mod.sync_executor_pool(self._exec, assembly1.raw_pool)
        pool1 = (review_mod.parse_pool(assembly1.raw_pool)
                 if assembly1.routing is not None else ctx.pool)
        plan1 = await review_mod.select_round(
            pool1, self._slots,
            int(ctx.review_cfg.get("round1_parallel", 3)),
            builder_family=builder_family,
            cascade_enabled=review_mod.cascade_enabled(
                ctx.review_cfg.get("cascade")))
        try:
            round1 = await self._run_round(
                ctx, 1, plan1, "", quorum, builder_family,
                pool=pool1, routing=assembly1.routing)
        finally:
            if assembly1.routing is not None:
                assembly1.routing.close()
        if round1 is None:
            return await self._park_review_quorum(ctx, 1)
        fix1 = await self._muse_fix(ctx, 1, round1["findings"])
        if fix1 is None:  # parked inside (fix gates unrecoverable)
            return await self._run_waited(ctx)

        prefer = self._dismissed_critical_families(
            round1["findings"], fix1)
        await self._runs.patch_run(ctx.run_id, {
            "phase": "round2", "updated_at": _utcnow_iso()})
        r1info = self._r1_result(pool1, assembly1.routing, round1)
        try:
            assembly2 = wiring_mod.assemble_review_pool(
                ctx.reviewers_raw, review_cfg=ctx.review_cfg,
                run_id=ctx.run_id, round="2",
                builder_family=builder_family, receipts_dir=receipts_dir,
                r1=r1info)
        except wiring_mod.RoundBlocked as exc:
            ctx.quorum_detail = str(exc)
            return await self._park_review_quorum(ctx, 2)
        except wiring_mod.ReviewRoutingError as exc:
            return await self._park(
                ctx, "config", f"free-router: {exc}", patch_run=True)
        wiring_mod.sync_executor_pool(self._exec, assembly2.raw_pool)
        pool2 = (review_mod.parse_pool(assembly2.raw_pool)
                 if assembly2.routing is not None else ctx.pool)
        plan2 = await review_mod.select_round(
            pool2, self._slots,
            int(ctx.review_cfg.get("round2_parallel", 2)),
            builder_family=builder_family,
            prefer_families=[*assembly2.prefer_families, *prefer],
            cascade_enabled=review_mod.cascade_enabled(
                ctx.review_cfg.get("cascade")))
        extra = self._round2_context(ctx, round1, fix1)
        try:
            round2 = await self._run_round(
                ctx, 2, plan2, extra, quorum, builder_family,
                pool=pool2, routing=assembly2.routing)
        finally:
            if assembly2.routing is not None:
                assembly2.routing.close()
        if round2 is None:
            return await self._park_review_quorum(ctx, 2)
        fix2 = await self._muse_fix(ctx, 2, round2["findings"])
        if fix2 is None:
            return await self._run_waited(ctx)

        dismissed_r1 = self._dismissed_verdicts(round1, fix1)
        candidates = review_mod.arbitration_candidates(
            round2["verdicts"], fix2, round2["findings"], dismissed_r1)
        if candidates:
            await self._runs.patch_run(ctx.run_id, {
                "phase": "arbitration", "updated_at": _utcnow_iso()})
            await self._arbitrate(ctx, candidates, fix2)

        fixed = self._count_verdict(fix1, "CORREGIDO") + self._count_verdict(
            fix2, "CORREGIDO")
        discarded = self._count_verdict(fix1, "DESCARTADO") + self._count_verdict(
            fix2, "DESCARTADO")
        ctx.metrics["review"] = {
            "rounds": 2, "fixed": fixed, "discarded": discarded,
            "arbitrations": len(ctx.arbitrations),
        }
        return None

    @staticmethod
    def _count_verdict(
        fix: Mapping[int, list[Any]], verdict: str) -> int:
        return sum(1 for items in fix.values() for item in items[:1]
                   if item.verdict == verdict)

    @staticmethod
    def _dismissed_critical_families(
        findings: Sequence[Any],
        fix: Mapping[int, list[Any]],
    ) -> list[str]:
        """Families behind dismissed R1 CRITICALs (R2 prefers them, §6.8)."""
        families: list[str] = []
        for item in findings:
            if item.severity != "CRITICAL":
                continue
            verdicts = fix.get(item.id, [])
            if verdicts and verdicts[0].verdict == "DESCARTADO" \
                    and not verdicts[0].out_of_scope:
                families.extend(item.families)
        return families

    @staticmethod
    def _dismissed_verdicts(
        round_result: dict[str, Any],
        fix: Mapping[int, list[Any]],
    ) -> list[Any]:
        """R1 verified findings ``muse`` dismissed (arbitration input)."""
        dismissed_ids = {index for index, items in fix.items()
                         if items and items[0].verdict == "DESCARTADO"
                         and not items[0].out_of_scope}
        if not dismissed_ids:
            return []
        findings = {item.id: item for item in round_result["findings"]}
        anchors: set[str] = set()
        for index in dismissed_ids:
            item = findings.get(index)
            if item is not None:
                anchors.update(findings_mod._anchor_text(a) for a in item.anchors)
        return [item for item in round_result["verdicts"]
                if item.destination == "findings"
                and any(findings_mod._anchor_text(a) in anchors
                        for a in item.raw.anchors)]

    def _round2_context(
        self,
        ctx: _Ctx,
        round1: dict[str, Any],
        fix1: Mapping[int, list[Any]],
    ) -> str:
        """R2 entry: FINDINGS_1 + muse motives + fix1 delta (§6.8)."""
        findings_text = self._read_artifact(ctx, "FINDINGS_1.md")
        fix_text = self._read_artifact(ctx, "FIX_1.md")
        delta = _git(ctx.repo, "diff", "--stat",
                     ctx.build_sha, ctx.head_sha).stdout.strip()
        return (
            "ROUND 2 scope: are the round-1 fixes correct, do muse's "
            "discards hold, and does the delta below introduce new "
            "CRITICALs? Round-1 findings and muse's motives follow, then "
            "the fix1 delta and the full ticket range.\n"
            f"--- FINDINGS_1 ---\n{findings_text[:4000]}\n"
            f"--- FIX_1 ---\n{fix_text[:3000]}\n"
            f"--- fix1 delta (stat) ---\n{delta[:2000]}"
        )

    async def _run_round(
        self,
        ctx: _Ctx,
        round_no: int,
        plan: review_mod.RoundPlan,
        extra_context: str,
        quorum: int,
        builder_family: str,
        pool: Sequence[review_mod.ReviewSlot] | None = None,
        routing: wiring_mod.RoundRouting | None = None,
    ) -> dict[str, Any] | None:
        """Run one round; None when quorum is unreachable (§7.2)."""
        head = ctx.head_sha or ctx.build_sha
        request = review_mod.build_review_request(
            round_no, goal=str(ctx.ticket.get("title", "")),
            acceptance=self._acceptance(ctx),
            base_sha=ctx.base_sha, head_sha=head,
            diff_stat=_git(ctx.repo, "diff", "--stat",
                           ctx.base_sha, head).stdout,
            files=self._changed_files(ctx),
            out_of_plan=self._out_of_plan_files(ctx),
            gates_section=gate_mod.handoff_gates_section(
                [self._gate_result(entry) for entry in ctx.gate_runs]),
            handoff_path=".openceo/HANDOFF.md",
            extra_context=extra_context)
        request_name = f"REVIEW_REQUEST_{round_no}.md"
        (_tier_dir(ctx.repo) / request_name).write_text(
            request, encoding="utf-8")
        handoff = self._read_artifact(ctx, "HANDOFF.md")

        async def run_one(
            slot: review_mod.ReviewSlot,
            attempt: int,
            previous: Mapping[str, Any] | None,
        ) -> dict[str, Any]:
            async def run_attempt(
                current: review_mod.ReviewSlot,
                tried: Mapping[str, Any] | None,
            ) -> dict[str, Any]:
                return await self._run_reviewer(
                    ctx, current, round_no, request, request_name,
                    handoff, head, attempt=attempt, previous=tried,
                    routing=routing)
            if (routing is not None and slot.label in routing.states
                    and attempt == 1 and previous is None):
                hit = wiring_mod.load_valid_receipt(
                    routing.receipts_dir, run_id=routing.run_id,
                    round=routing.round, slot=slot.label,
                    reviewed_commit=head)
                if hit is not None:
                    receipt, text = hit
                    try:
                        raw = findings_mod.parse_report(text)
                    except findings_mod.ReportMalformed:
                        raw = None
                    if raw is not None:
                        verdicts = await asyncio.to_thread(
                            findings_mod.verify_report, raw, ctx.repo,
                            head, ctx.base_sha, slot.label,
                            receipt.family_id, self._acceptance(ctx))
                        routing.receipts[slot.label] = receipt
                        outcome = review_mod.new_outcome(slot, attempt)
                        outcome.update(
                            verdicts=verdicts, valid=True, kind="valid",
                            receipt=receipt.to_dict())
                        return outcome
            outcome = await run_attempt(slot, previous)
            last = previous
            while (routing is not None and slot.label in routing.states
                   and not outcome.get("valid")):
                kind = str(outcome.get("kind") or "")
                last_kind = str((last or {}).get("kind") or "")
                nxt = wiring_mod.plan_fallback(
                    routing, slot.label, outcome_kind=kind,
                    text=str(outcome.get("reason") or ""),
                    timeout_repeat=(kind == "timeout"
                                    and last_kind == "timeout"))
                if nxt is None:
                    break
                await self._slots.set_slot(
                    slot.label, {"status": "available"})
                wiring_mod.sync_executor_pool(self._exec, [
                    wiring_mod.dynamic_entry(
                        slot.label, slot.priority, nxt)])
                last = outcome
                outcome = await run_attempt(
                    wiring_mod.derive_slot(slot, nxt), None)
            return outcome

        round_pool = pool if pool is not None else ctx.pool
        critical = (str(ctx.ticket.get("priority") or "").lower()
                    == "critical")
        cascade = review_mod.arm_premium_slots(
            self._exec, round_pool, ctx.review_cfg.get("cascade"), critical)
        try:
            result = await review_mod.run_quorum_round(
                round_pool, self._slots, plan,
                run_one, builder_family=builder_family, quorum=quorum,
                recovery=ctx.review_cfg.get("recovery"),
                cascade=cascade)
        finally:
            review_mod.disarm_premium_slots(self._exec, round_pool)
        slots_run = sorted(result.valid)
        valid_reports = sum(1 for label in slots_run if result.valid.get(label))
        verdicts = result.verdicts
        if not result.met:
            # The park detail and the run doc keep why each slot fell short.
            ctx.quorum_detail = result.summary()
            ctx.rounds.append(review_mod.round_telemetry(
                round_no, slots_run, valid_reports, verdicts, {},
                attempts=result.outcomes, met=False))
            return None

        consolidated = findings_mod.consolidate(verdicts)
        text = findings_mod.render_findings(
            consolidated, round_no, head, builder_family=builder_family)
        (_tier_dir(ctx.repo) / f"FINDINGS_{round_no}.md").write_text(
            text, encoding="utf-8")
        await self._record_inbox(ctx, round_no, verdicts)
        await self._evaluate_promotions(ctx)
        ctx.rounds.append(review_mod.round_telemetry(
            round_no, slots_run, valid_reports, verdicts, {},
            attempts=result.outcomes, met=True))
        await self._runs.patch_run(ctx.run_id, {
            "rounds": ctx.rounds, "metrics": ctx.metrics,
            "updated_at": _utcnow_iso()})
        return {"findings": consolidated, "verdicts": verdicts,
                "slots": slots_run, "valid": result.valid,
                "receipts": [dict(outcome["receipt"])
                             for outcome in result.outcomes
                             if isinstance(outcome.get("receipt"), dict)]}

    async def _run_reviewer(
        self,
        ctx: _Ctx,
        slot: review_mod.ReviewSlot,
        round_no: int,
        request: str,
        request_name: str,
        handoff: str,
        head_sha: str,
        attempt: int = 1,
        previous: Mapping[str, Any] | None = None,
        routing: wiring_mod.RoundRouting | None = None,
    ) -> dict[str, Any]:
        """One reviewer attempt on its own detached read-only worktree (§7.5).

        The outcome's ``kind`` (see :func:`review_mod.new_outcome`) decides
        whether the round may retry the slot; every attempt is persisted.
        """
        role = f"review:{slot.label}"
        if role not in ctx.used_review_roles:
            ctx.used_review_roles.append(role)
        outcome = review_mod.new_outcome(slot, attempt)
        suffix = f"-a{attempt}" if attempt > 1 else ""
        try:
            worktree = await asyncio.to_thread(
                review_mod.create_worktree, ctx.repo, head_sha,
                f"r{round_no}-{slot.label}{suffix}")
        except RuntimeError as exc:
            outcome.update(kind="worktree", reason=str(exc)[:200])
            await self._persist_review(ctx, slot, round_no, outcome)
            return outcome
        try:
            (worktree / request_name).write_text(request, encoding="utf-8")
            (worktree / "HANDOFF.md").write_text(handoff, encoding="utf-8")
            before = await asyncio.to_thread(
                review_mod.worktree_snapshot, worktree)
            timeout_s = review_mod.slot_timeout_s(
                slot, ctx.review_cfg.get("reviewer_timeout_minutes", 15),
                previous)
            started = datetime.now(timezone.utc).isoformat()
            result = await self._exec.run(
                role, review_mod.reviewer_prompt(
                    slot.label, request_name, "ticket", round_no,
                    repo=worktree, retry=review_mod.retry_note(previous)),
                worktree, timeout_s)
            finished = datetime.now(timezone.utc).isoformat()
            self._record_session(ctx, "reviewer", role, result)
            outcome.update(elapsed_s=float(result.elapsed_s or 0.0),
                           exit_code=result.returncode)
            after = await asyncio.to_thread(
                review_mod.worktree_snapshot, worktree)
            if before and after and before != after:
                outcome.update(kind="write_attempt", reason="write_attempt")
                await self._persist_review(ctx, slot, round_no, outcome)
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
                        await self._slots.set_slot(slot.label, patch)
                    outcome.update(
                        kind=(action if action in {"cooling_down", "disabled"}
                              else "slot_error"),
                        reason=f"slot error ({action}): {text[:200]}")
                await self._persist_review(ctx, slot, round_no, outcome)
                return outcome
            (_tier_dir(ctx.repo) / f"REVIEW-{slot.label}-{round_no}{suffix}.md"
             ).write_text(text + "\n", encoding="utf-8")
            try:
                raw = findings_mod.parse_report(text)
            except findings_mod.ReportMalformed as exc:
                outcome.update(kind="malformed", reason=f"malformed: {exc}")
                await self._persist_review(ctx, slot, round_no, outcome)
                return outcome
            verdicts = await asyncio.to_thread(
                findings_mod.verify_report, raw, ctx.repo, head_sha,
                ctx.base_sha, slot.label, slot.family, self._acceptance(ctx))
            outcome.update(verdicts=verdicts, valid=True, kind="valid")
            if routing is not None and slot.label in routing.states:
                receipt = wiring_mod.record_receipt(
                    routing, slot.label, report_text=text,
                    reviewed_commit=head_sha, started_at=started,
                    finished_at=finished)
                outcome["receipt"] = receipt.to_dict()
            await self._persist_review(ctx, slot, round_no, outcome)
            return outcome
        finally:
            await asyncio.to_thread(
                review_mod.remove_worktree, ctx.repo, worktree)

    async def _persist_review(
        self,
        ctx: _Ctx,
        slot: review_mod.ReviewSlot,
        round_no: int,
        outcome: Mapping[str, Any],
    ) -> None:
        """Append the ``task_reviews`` document (no-op when unwired)."""
        if self._reviews is None:
            return
        try:
            meta: dict[str, Any] = {
                "attempt": int(outcome.get("attempt") or 1),
                "kind": str(outcome.get("kind") or ""),
                "elapsed_s": float(outcome.get("elapsed_s") or 0.0),
                "exit_code": outcome.get("exit_code"),
                "account": slot.account, "model": slot.model,
                "tier": slot.cost_tier,
                "project": ctx.ticket.get("project")}
            if isinstance(outcome.get("receipt"), dict):
                meta["receipt"] = outcome["receipt"]
            if outcome.get("spend"):
                meta["spend"] = outcome["spend"]
            await self._reviews.create_review(findings_mod.review_document(
                review_id=f"rev-{uuid.uuid4().hex[:12]}",
                task_id=str(ctx.ticket.get("task_id", "")),
                run_id=ctx.run_id, scope="ticket", round_no=round_no,
                slot=slot.label, family=slot.family,
                valid=bool(outcome.get("valid")),
                verdicts=list(outcome.get("verdicts") or []),
                invalid_reason=str(outcome.get("reason") or ""),
                meta=meta))
        except Exception:
            pass  # telemetry persistence never fails a round

    async def _muse_fix(
        self,
        ctx: _Ctx,
        round_no: int,
        findings: Sequence[Any],
    ) -> dict[int, list[Any]] | None:
        """``muse`` decides every finding, then gates + wip commit (§6.7).

        None means the ticket was parked (fix gates unrecoverable).
        """
        fix_name = f"FIX_{round_no}.md"
        if not findings:
            (_tier_dir(ctx.repo) / fix_name).write_text(
                "No findings.\n", encoding="utf-8")
            return {}
        timeout_s = int(ctx.timeouts.get("fix", 30)) * 60
        result = await self._exec.run(
            "builder", review_mod.muse_fix_prompt(
                round_no, f"FINDINGS_{round_no}.md", len(findings),
                repo=ctx.repo),
            ctx.repo, timeout_s)
        self._record_session(ctx, "builder", "builder", result)
        if result.returncode != 0:
            await self._park_builder_failure(ctx, result)
            return None
        verdicts = review_mod.parse_fix(self._read_artifact(ctx, fix_name))
        missing, _dup = review_mod.fix_gaps(findings, verdicts)
        if missing:
            retry = await self._exec.run(
                "builder",
                _fix_retry_prompt(fix_name, missing, round_no, ctx.repo),
                ctx.repo, timeout_s)
            self._record_session(ctx, "builder", "builder", retry)
            if retry.returncode != 0:
                await self._park_builder_failure(ctx, retry)
                return None
            verdicts = review_mod.parse_fix(self._read_artifact(ctx, fix_name))
            missing, _dup = review_mod.fix_gaps(findings, verdicts)
        if ctx.rounds:
            ctx.rounds[-1]["muse"]["no_response"] = len(missing)
        for item in findings:
            items = verdicts.get(item.id, [])
            if not items:
                continue
            first = items[0]
            if first.verdict == "DESCARTADO":
                ctx.discarded_notes.append(
                    f"F{item.id} (R{round_no}): {first.reason[:160]}")
                if first.out_of_scope:
                    await self._record_out_of_scope(
                        ctx, round_no, item, first.reason)
        # Fix gates: one correction round, then the ticket waits (§6.9 spirit).
        for attempt in range(2):
            results = await asyncio.to_thread(
                gate_mod.run_gates, ctx.repo, ctx.gate_specs)
            self._record_gates(ctx, f"fix{round_no}" if attempt == 0
                               else f"fix{round_no}b", results)
            failed = gate_mod.gates_failed(results)
            if not failed:
                break
            if attempt == 1:
                names = ", ".join(item.name for item in failed)
                await self._park(
                    ctx, "needs_owner",
                    f"fix{round_no} gates failed: {names}", patch_run=True)
                return None
            tail = "\n\n".join(
                f"[{item.name}] {item.status}: {item.detail}\n{item.output_tail}"
                for item in failed)
            bump_gate_retries(ctx.metrics)
            fix = await self._exec.run(
                "builder", _fix_gates_retry_prompt(round_no, tail, ctx.repo),
                ctx.repo, timeout_s)
            self._record_session(ctx, "builder", "builder", fix)
            if fix.returncode != 0:
                await self._park_builder_failure(ctx, fix)
                return None
        self._commit_if_changed(ctx, f"wip({ctx.ref}) fix{round_no}")
        return verdicts

    async def _arbitrate(
        self,
        ctx: _Ctx,
        candidates: Sequence[Any],
        fix2: Mapping[int, list[Any]],
    ) -> None:
        """``claudej`` arbitrates disputed CRITICALs (§7.10, §6.9)."""
        arbiter = ctx.roles.get("arbiter", {}) or {}
        account = str(arbiter.get("account") or "")
        verdicts = await self._quota.check([account] if account else [])
        quota_ok = all(item.ok for item in verdicts.values())
        for finding in candidates:
            items = fix2.get(finding.id, [])
            muse_reason = items[0].reason if items else ""
            if not arbiter or not account or not quota_ok:
                await self._record_disputed(ctx, finding)
                ctx.arbitrations.append({
                    "finding": finding.id, "verdict": "disputed",
                    "reason": "no arbiter quota (→ inbox, promotes)",
                })
                continue
            timeout_s = int(ctx.timeouts.get("arbiter", 10)) * 60
            quote = "\n".join(finding.quotes[:2])
            result = await self._exec.run(
                "arbiter", review_mod.arbiter_prompt(
                    finding, muse_reason, quote, repo=ctx.repo),
                ctx.repo, timeout_s)
            self._record_session(ctx, "arbiter", "arbiter", result)
            verdict = self._parse_arbiter_verdict(result.output_tail or "") if result.returncode == 0 else None
            if verdict is None:
                retry = await self._exec.run(
                    "arbiter", _arbiter_retry_prompt(ctx.repo),
                    ctx.repo, timeout_s)
                self._record_session(ctx, "arbiter", "arbiter", retry)
                verdict = self._parse_arbiter_verdict(retry.output_tail or "") if retry.returncode == 0 else None
            if verdict is None:
                await self._record_disputed(ctx, finding)
                ctx.arbitrations.append({
                    "finding": finding.id, "verdict": "disputed",
                    "reason": "unparseable arbiter verdict (→ inbox)",
                })
                continue
            kind, reason = verdict
            if kind == "FALSO":
                ctx.arbitrations.append({
                    "finding": finding.id, "verdict": "false",
                    "reason": reason[:300],
                })
                ctx.discarded_notes.append(
                    f"F{finding.id} (arbitration): {reason[:160]}")
                continue
            ctx.arbitrations.append({
                "finding": finding.id, "verdict": "real",
                "reason": reason[:300],
            })
            timeout_fix = int(ctx.timeouts.get("fix", 30)) * 60
            fix = await self._exec.run(
                "builder",
                _arbitration_fix_prompt(
                    finding.id, finding.category, finding.problem,
                    finding.fix, reason, ctx.repo),
                ctx.repo, timeout_fix)
            self._record_session(ctx, "builder", "builder", fix)
            results = await asyncio.to_thread(
                gate_mod.run_gates, ctx.repo, ctx.gate_specs)
            self._record_gates(ctx, "arbitrated", results)
            self._commit_if_changed(ctx, f"wip({ctx.ref}) arbitrated")

    @staticmethod
    def _parse_arbiter_verdict(text: str) -> tuple[str, str] | None:
        for line in (text or "").splitlines():
            match = re.match(
                r"^\s*(REAL|FALSO)\s*[—–-]\s*(.+?)\s*$", line, re.IGNORECASE)
            if match:
                return match.group(1).upper(), match.group(2).strip()
        return None

    # -- F4 hooks --------------------------------------------------------------

    async def _record_inbox(
        self,
        ctx: _Ctx,
        round_no: int,
        verdicts: Sequence[Any],
    ) -> None:
        """Verified pre-existing findings go to the inbox (F4, §7.7)."""
        if self._inbox is None:
            return
        task_id = str(ctx.ticket.get("task_id", ""))
        project = str(ctx.ticket.get("project") or "")
        for item in verdicts:
            if item.destination != "inbox":
                continue
            anchor = item.raw.anchors[0] if item.raw.anchors else None
            quote = item.raw.quotes[0] if item.raw.quotes else ""
            try:
                fp = await inbox_mod.record_appearance(
                    self._inbox, project, inbox_mod.Appearance(
                        file=anchor.path if anchor and anchor.path else "",
                        category=item.raw.category,
                        severity=item.raw.severity,
                        quote=quote, strength=item.strength,
                        source="pre_existing", family=item.family,
                        task_id=task_id, run_id=ctx.run_id,
                        round_no=round_no,
                        anchor=findings_mod._anchor_text(anchor)
                        if anchor else ""))
            except Exception:
                continue
            if fp not in ctx.inbox_fps:
                ctx.inbox_fps.append(fp)

    async def _record_out_of_scope(
        self,
        ctx: _Ctx,
        round_no: int,
        finding: Any,
        reason: str,
    ) -> None:
        """``DESCARTADO — fuera de alcance`` also feeds the inbox (§7.9)."""
        if self._inbox is None:
            return
        anchor = finding.anchors[0] if finding.anchors else None
        quote = finding.quotes[0] if finding.quotes else ""
        try:
            fp = await inbox_mod.record_appearance(
                self._inbox, str(ctx.ticket.get("project") or ""),
                inbox_mod.Appearance(
                    file=anchor.path if anchor and anchor.path else "",
                    category=finding.category, severity=finding.severity,
                    quote=quote, strength=finding.strength,
                    source="out_of_scope", family=",".join(finding.families),
                    task_id=str(ctx.ticket.get("task_id", "")),
                    run_id=ctx.run_id, round_no=round_no,
                    anchor=findings_mod._anchor_text(anchor) if anchor else ""))
        except Exception:
            return
        if fp not in ctx.inbox_fps:
            ctx.inbox_fps.append(fp)
        await self._evaluate_promotions(ctx)

    async def _record_disputed(self, ctx: _Ctx, finding: Any) -> None:
        """CRITICAL without arbitration: disputed → inbox → ticket (§6.9)."""
        if self._inbox is not None:
            anchor = finding.anchors[0] if finding.anchors else None
            quote = finding.quotes[0] if finding.quotes else ""
            try:
                fp = await inbox_mod.record_appearance(
                    self._inbox, str(ctx.ticket.get("project") or ""),
                    inbox_mod.Appearance(
                        file=anchor.path if anchor and anchor.path else "",
                        category=finding.category, severity="CRITICAL",
                        quote=quote, strength=finding.strength,
                        source="disputed", family=",".join(finding.families),
                        task_id=str(ctx.ticket.get("task_id", "")),
                        run_id=ctx.run_id, round_no=2,
                        anchor=findings_mod._anchor_text(anchor)
                        if anchor else ""))
                if fp not in ctx.inbox_fps:
                    ctx.inbox_fps.append(fp)
            except Exception:
                pass
        await self._evaluate_promotions(ctx)

    async def _evaluate_promotions(self, ctx: _Ctx) -> None:
        """Promotion event over just-changed entries (F4, §8.3)."""
        if self._inbox is None or not ctx.inbox_fps:
            return
        pending = list(ctx.inbox_fps)
        ctx.inbox_fps = []
        for fp in pending:
            try:
                entry = await self._inbox.get_entry(fp)
            except Exception:
                continue
            if entry is None or not inbox_mod.promotion_ready(
                    entry, ctx.inbox_rule or None):
                continue
            if self._create_ticket is None:
                ctx.metrics.setdefault("promotion_pending", []).append(fp)
                continue
            attribution = await self._attribute_promotion(ctx, entry)
            payload = inbox_mod.promotion_ticket(
                entry, ctx.fix_tiers or None, attribution)
            payload["project"] = str(ctx.ticket.get("project") or "")
            payload["source"] = "spillover"
            payload["source_id"] = f"inbox:{fp}"
            try:
                created = await self._create_ticket(payload)
            except Exception as exc:
                await self._say(
                    ctx, f"inbox promotion failed for {fp[:12]}: {exc}")
                continue
            task_id = str((created or {}).get("task_id") or "")
            if task_id:
                try:
                    await inbox_mod.mark_promoted(
                        self._inbox, fp, task_id, attribution)
                except Exception:
                    pass
                ctx.metrics.setdefault("promoted", []).append(
                    {"fp": fp, "task_id": task_id})

    async def _attribute_promotion(
        self, ctx: _Ctx, entry: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Best-effort §31 attribution for a promoted finding.

        Unknown origins return None (the payload stays unchanged); a C
        origin also records the Tier C late failure (§32). Never raises:
        attribution must not break a promotion.
        """
        env = self._objective_env
        objective_id = str(ctx.ticket.get("objective_id") or "")
        if env is None or not objective_id:
            return None
        try:
            objective = await env.objectives.get_objective(objective_id)
            log = (objective or {}).get("tickets_log") or []
            candidates = []
            for item in log:
                if not isinstance(item, Mapping):
                    continue
                sha = str(item.get("sha") or "")
                origin_id = str(item.get("task_id") or "")
                if not sha or not origin_id:
                    continue
                try:
                    detail = await env.tasks.get_task(origin_id) or {}
                except Exception:
                    detail = {}
                params = detail.get("params") or {} \
                    if isinstance(detail, Mapping) else {}
                oracle = params.get("oracle") or {}
                oracle = oracle if isinstance(oracle, Mapping) else {}
                tool = str(detail.get("coding_tool") or "") \
                    if isinstance(detail, Mapping) else ""
                candidates.append({
                    "sha": sha,
                    "task_id": origin_id,
                    "objective_id": objective_id,
                    "coding_tool": tool or str(item.get("tier") or ""),
                    "tier_reason": str(params.get("tier_reason") or ""),
                    "oracle_type": str(oracle.get("type") or "") or None,
                    "oracle_refs": [
                        str(ref.get("ref") or "")
                        for ref in oracle.get("refs") or []
                        if isinstance(ref, Mapping)],
                    "files": [str(name)
                              for name in item.get("files") or []],
                })
            result = inbox_mod.attribute_finding(
                ctx.repo, str(entry.get("file") or ""),
                str(entry.get("quote") or ""), candidates)
            if result.get("status") != "attributed":
                return None
            if result.get("coding_tool") == "tier_c" \
                    and result.get("ticket_ids"):
                short = ",".join(
                    str(item)[:12] for item in result.get("shas") or [])
                try:
                    await objectives_mod.record_tier_c(
                        env.objectives, objective_id, "late_failure",
                        task_id=str(result["ticket_ids"][0]),
                        severity=str(entry.get("severity_max") or ""),
                        detail=f"{entry.get('_id')}: {short}")
                except Exception:
                    pass
            return result
        except Exception:
            return None

    # -- round helpers -----------------------------------------------------------

    def _acceptance(self, ctx: _Ctx) -> list[str]:
        acceptance = ctx.ticket.get("acceptance") or []
        if isinstance(acceptance, str):
            return [acceptance]
        return [str(item) for item in acceptance]

    def _out_of_plan_files(self, ctx: _Ctx) -> list[str]:
        plan_text = self._read_artifact(ctx, "PLAN.md")
        planned = set()
        for line in plan_text.splitlines():
            match = re.match(r"^-\s+(\S+)\s+\((create|modify|delete)\)", line.strip())
            if match:
                planned.add(match.group(1))
        return [name for name in self._changed_files(ctx) if name not in planned]

    @staticmethod
    def _gate_result(entry: Mapping[str, Any]) -> GateResult:
        return GateResult(
            name=str(entry.get("name", "")), status=str(entry.get("status", "")),
            required=bool(entry.get("required", True)),
            elapsed_s=float(entry.get("elapsed_s", 0.0)),
            output_tail=str(entry.get("output_tail", "")),
            detail=str(entry.get("detail", "")))

    def _commit_if_changed(self, ctx: _Ctx, message: str) -> None:
        """Wip checkpoint only when the worktree moved since the last one."""
        since = ctx.head_sha or ctx.build_sha or ctx.base_sha
        proc = _git(ctx.repo, "diff", "--quiet", since, "--", ".", ":!.openceo")
        untracked = _git(
            ctx.repo, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", ":!.openceo")
        dirty = proc.returncode != 0 or bool(untracked.stdout.strip("\0"))
        if not dirty:
            return
        ctx.head_sha = self._commit(ctx, message)

    # -- close (§6.9) ------------------------------------------------------------

    async def _close(self, ctx: _Ctx) -> CycleOutcome:
        await self._runs.patch_run(ctx.run_id, {
            "phase": "closing", "updated_at": _utcnow_iso()})
        # F3: round1 → muse FIX → round2 → muse FIX → arbitration (§6.7–§6.9).
        parked = await self._review_rounds(ctx)
        if parked is not None:
            return parked
        await self._runs.patch_run(ctx.run_id, {
            "phase": "closing", "rounds": ctx.rounds,
            "arbitrations": ctx.arbitrations, "metrics": ctx.metrics,
            "updated_at": _utcnow_iso()})
        final = await asyncio.to_thread(
            gate_mod.run_gates, ctx.repo, ctx.gate_specs)
        self._record_gates(ctx, "final", final)
        if gate_mod.gates_failed(final):
            names = ", ".join(item.name for item in gate_mod.gates_failed(final))
            return await self._park(
                ctx, "needs_owner", f"final gates failed: {names}",
                patch_run=True)
        # Last look before the irreversible effects: a business
        # cancellation that landed mid-cycle stops the squash, the merge,
        # the push and the objective trigger (TASK-2026-00177).
        fenced = await self._fenced_cancelled(ctx)
        if fenced is not None:
            return fenced
        # Squash the ticket into one commit; keep wips for audit (§12).
        _git(ctx.repo, "update-ref", f"refs/tier-s/{ctx.run_id}/wip", "HEAD")
        _git(ctx.repo, "reset", "--soft", ctx.base_sha)
        title = re.sub(r"\s+", " ", ctx.ticket.get("title", "")).strip()[:72]
        ctx.final_sha = (self._commit(ctx, f"{ctx.ref}: {title}")
                         if self._changed_files(ctx) else ctx.base_sha)
        files = self._changed_files(ctx)
        handoff = self._read_artifact(ctx, "HANDOFF.md")
        discarded = ("none" if not ctx.discarded_notes
                     else "; ".join(ctx.discarded_notes)[:2000])
        completed = _complete_handoff(
            handoff, ctx.final_sha, files,
            gate_mod.handoff_gates_section(final),
            findings_discarded=discarded)
        (_tier_dir(ctx.repo) / "HANDOFF.md").write_text(
            completed, encoding="utf-8")
        task_id = str(ctx.ticket.get("task_id", ""))
        if ctx.auto_merge:
            merged, conflict = self._merge(ctx)
            if not merged:
                return await self._park(
                    ctx, "needs_owner", f"merge_conflict: {conflict}",
                    patch_run=True)
            self._drop_branch(ctx)
            if self._push is not None:
                await self._push(ctx.repo, ctx.base_branch)
        await self._runs.patch_run(ctx.run_id, {
            "status": "completed", "head_sha": ctx.final_sha,
            "metrics": ctx.metrics, "gates": ctx.gate_runs,
            "updated_at": _utcnow_iso(),
        })
        await self._tasks.patch_task(task_id, {
            "status": TaskStatus.COMPLETED.value,
            "waiting_reason": None,
            "resume_at": None,
            "completed_at": _utcnow_iso(),
        })
        await self._objective_trigger(ctx, files, completed)
        await self._ticket_knowledge_note(ctx, completed)
        _write_status(ctx.repo, SIGNALS["ticket_complete"])
        self._archive(ctx)
        branch_note = (f"merged into {ctx.base_branch}"
                       if ctx.auto_merge else f"branch kept: {ctx.branch}")
        review_note = self._review_summary(ctx)
        await self._say(
            ctx, f"Tier S cycle closed: {branch_note}, {ctx.final_sha[:12]}, "
            f"{len(files)} files. {review_note}")
        return CycleOutcome(
            status="completed", run_id=ctx.run_id, branch=ctx.branch,
            commit_sha=ctx.final_sha)

    def _merge(self, ctx: _Ctx) -> tuple[bool, str]:
        if _git(ctx.repo, "checkout", "-q", ctx.base_branch).returncode != 0:
            return False, f"cannot checkout {ctx.base_branch}"
        # Fast-forward keeps "one commit per ticket" on the base; a moved
        # base falls back to an explicit merge (§12).
        if _git(ctx.repo, "merge", "--ff-only", "-q", ctx.branch).returncode == 0:
            return True, ""
        proc = _git(ctx.repo, "merge", "--no-ff", "--no-edit", ctx.branch)
        if proc.returncode != 0:
            _git(ctx.repo, "merge", "--abort")
            return False, proc.stderr.strip()[:300] or "conflict"
        return True, ""

    async def _objective_trigger(
        self, ctx: _Ctx, files: Sequence[str], handoff: str) -> None:
        """F6: capsule absorb + triggers after an objective ticket closes."""
        if not ctx.ticket.get("objective_id") or self._objective_env is None:
            return
        plan_text = self._read_artifact(ctx, "PLAN.md")
        plan = parse_plan(plan_text) if plan_text else None
        close = objectives_mod.TicketCloseContext(
            task_id=str(ctx.ticket.get("task_id", "")),
            title=str(ctx.ticket.get("title", "")),
            tier="tier_s", sha=ctx.final_sha, files=list(files),
            contracts=self._handoff_list(handoff,
                                         "Contracts introduced/changed"),
            deviations=self._handoff_list(handoff, "Deviations from plan"),
            gotchas=self._handoff_list(handoff, "Gotchas"),
            decisions=[str(entry) for entry in plan.decisions]
            if plan else [],
            handoff_excerpt=handoff[:1500])
        try:
            await objectives_mod.on_ticket_closed(
                self._objective_env, ctx.ticket, close)
        except Exception as exc:  # triggers never fail a close
            await self._say(ctx, f"objective trigger failed: {exc}")

    @staticmethod
    def _handoff_list(handoff: str, section: str) -> list[str]:
        for line in handoff.splitlines():
            if line.startswith(section + ":"):
                value = line.split(":", 1)[1].strip()
                if not value or value.lower() == "none":
                    return []
                return [value]
        return []

    async def _ticket_knowledge_note(self, ctx: _Ctx, handoff: str) -> None:
        """F7b: vault note for plans with ``[durable: yes]`` (§9.8)."""
        env = self._objective_env
        if env is None or not getattr(env, "vault", None):
            return
        decisions = knowledge_mod.durable_plan_decisions(
            self._read_artifact(ctx, "PLAN.md"))
        if not decisions:
            return
        try:
            await knowledge_mod.write_ticket_note(
                env, ctx.ticket, decisions, handoff[:3000])
        except Exception as exc:
            await self._say(ctx, f"vault note failed (close stands): {exc}")

    def _archive(self, ctx: _Ctx) -> None:
        tier_dir = _tier_dir(ctx.repo)
        dest = tier_dir / "runs" / ctx.run_id
        dest.mkdir(parents=True, exist_ok=True)
        for item in tier_dir.iterdir():
            if item.name == "runs":
                continue
            try:
                item.rename(dest / item.name)
            except OSError:
                continue

    # -- waiting, cleanup, telemetry ----------------------------------------------

    async def _fenced_cancelled(self, ctx: _Ctx) -> CycleOutcome | None:
        """Fence the attempt when the business cancelled the ticket.

        Returns the outcome to return instead of parking/closing (run
        superseded so nothing can resume, work branch dropped, one
        audit note, no task patch/merge/objective trigger), or None
        when the ticket is live. A failed status read is fail-open:
        the patch-level net still fences the task writes
        (TASK-2026-00177).
        """
        task_id = str(ctx.ticket.get("task_id", ""))
        try:
            status = await self._tasks.task_status(task_id)
        except Exception:
            logger.warning("cancel check failed for %s", task_id,
                           exc_info=True)
            return None
        if status != TaskStatus.CANCELLED.value:
            return None
        ctx.fenced_cancelled = True
        try:
            await asyncio.to_thread(self._drop_branch, ctx)
        except Exception:
            logger.warning("cancelled branch drop failed for %s", task_id,
                           exc_info=True)
        await self._runs.patch_run(ctx.run_id, {
            "status": "superseded",
            "superseded_reason": "ticket cancelled in Jardal ERP",
            "waiting": None,
            "active_repo": None,
            "metrics": ctx.metrics,
            "updated_at": _utcnow_iso(),
        })
        await self._say(ctx, "ticket cancelled in Jardal ERP; cycle fenced "
                        "(no merge, work branch dropped)")
        return CycleOutcome(
            status="failed", run_id=ctx.run_id, reason="cancelled",
            detail="ticket cancelled in Jardal ERP; close fenced",
            branch=None)

    async def ask_owner(
        self,
        ctx: _Ctx,
        *,
        question: str,
        human_action: str,
        checkpoint: dict[str, Any] | None = None,
        detail: str = "",
    ) -> CycleOutcome:
        """Park on an owner-data question and open its owner ticket (00206).

        Only for questions only the Owner can answer from their own data;
        technical parks keep using :meth:`_park` (they return to the
        planner as verdicts, never as questions). Without a ticket hook
        the park still lands and the question degrades to today's manual
        resume. The Owner's answer requeues the origin from the cockpit,
        by voice, or from the Jardal ERP desk.
        """
        outcome = await self._park(
            ctx, "needs_owner", detail or question, patch_run=True)
        if outcome.status != "waiting" or self._create_ticket is None:
            return outcome
        task_id = str(ctx.ticket.get("task_id", ""))
        seq = int(ctx.metrics.get("owner_questions_asked", 0)) + 1
        ctx.metrics["owner_questions_asked"] = seq
        spec = build_owner_question(
            kind="question",
            question=question,
            human_action=human_action,
            origin_task_id=task_id,
            origin_title=str(ctx.ticket.get("title") or ""),
            checkpoint=checkpoint,
            project=str(ctx.ticket.get("project") or "") or None,
            source_id=f"owner-question:{task_id}:{ctx.run_id}:{seq}",
        )
        try:
            created = await self._create_ticket({
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
        except Exception as exc:
            await self._say(ctx, f"owner question failed: {exc}")
            return outcome
        question_id = str((created or {}).get("task_id") or "")
        if not question_id:
            return outcome
        await self._tasks.patch_task(question_id, {
            "status": TaskStatus.WAITING.value,
            "phase": spec["phase"],
            "waiting_reason": spec["waiting_reason"],
            # H4 (revisión 00206): same phase cleanup as
            # TaskService.create_owner_question, so no project template
            # can ever route the question away from the channel.
            "workflow_key": None,
            "workflow_phases": [],
            "phase_index": 0,
            "phase_assignments": {},
        })
        # The cycle holds the repo lock: no one else writes the origin
        # params mid-run, so merging from the context snapshot is safe.
        params = dict(ctx.ticket.get("params") or {})
        params["owner_question_id"] = question_id
        await self._tasks.patch_task(task_id, {"params": params})
        return outcome

    async def _park(
        self,
        ctx: _Ctx,
        reason: str,
        detail: str,
        resume_at: str | None = None,
        resume_in_min: int | None = None,
        patch_run: bool = False,
        notify: bool | None = None,
        run_status: str = "waiting",
        drop_branch: bool = False,
        extra_comment: str = "",
    ) -> CycleOutcome:
        fenced = await self._fenced_cancelled(ctx)
        if fenced is not None:
            return fenced
        if resume_at is None and resume_in_min is not None:
            resume_at = (datetime.now(timezone.utc)
                         + timedelta(minutes=resume_in_min)).isoformat()
        task_id = str(ctx.ticket.get("task_id", ""))
        if patch_run:
            await self._runs.patch_run(ctx.run_id, {
                "status": run_status,
                "waiting": {"reason": reason, "detail": detail,
                            "since": _utcnow_iso(), "resume_at": resume_at},
                "metrics": ctx.metrics,
                "gates": ctx.gate_runs,
                "rounds": ctx.rounds,
                "updated_at": _utcnow_iso(),
            })
        await self._tasks.patch_task(task_id, {
            "status": TaskStatus.WAITING.value,
            "waiting_reason": f"{reason}: {detail}"[:500],
            "resume_at": resume_at,
        })
        if drop_branch:
            await asyncio.to_thread(self._drop_branch, ctx)
        wants_note = reason in {"needs_owner", "config", "quota", "blocked_by"}
        if notify is None:
            notify = wants_note
        if notify:
            await self._say(ctx, f"{reason}: {detail}"
                            + (f"\n{extra_comment}" if extra_comment else ""))
        ctx.parked_outcome = CycleOutcome(
            status="waiting", run_id=ctx.run_id, reason=reason,
            detail=detail, branch=ctx.branch, resume_at=resume_at)
        return ctx.parked_outcome

    async def _cleanup(self, ctx: _Ctx) -> None:
        roles = ["planner", "plan_reviewer", "builder", "arbiter",
                 *ctx.used_review_roles]
        for role in roles:
            try:
                await self._exec.cleanup(role)
            except Exception:
                continue
        if ctx.lock_held:
            try:
                await self._locks.release(
                    str(ctx.repo), f"tier-s:{ctx.run_id}")
            except Exception:
                pass
            ctx.lock_held = False
        # Release the repo guard on every exit: only a string active_repo
        # means live work (db/mongo.py contract). Parks and terminals keep
        # no process behind, so holding it would wedge the repo for the
        # next run (unique partial index) — including this task's own
        # resume, which always opens a new run.
        try:
            await self._runs.patch_run(ctx.run_id, {"active_repo": None})
        except Exception:
            pass

    async def _say(self, ctx: _Ctx, text: str) -> None:
        if self._notify is not None:
            await self._notify(ctx.ref, text)

    async def _run_waited(self, ctx: _Ctx) -> CycleOutcome:
        # Helper for early exits: the run doc already carries the wait state.
        if ctx.fenced_cancelled:
            return CycleOutcome(status="failed", run_id=ctx.run_id,
                                reason="cancelled",
                                detail="ticket cancelled in Jardal ERP; "
                                "park fenced",
                                branch=None)
        if ctx.parked_outcome is not None:
            return ctx.parked_outcome
        return CycleOutcome(status="waiting", run_id=ctx.run_id,
                            branch=ctx.branch)

    def _record_session(
        self, ctx: _Ctx, role: str, key: str, result: ExecResult) -> None:
        role_cfg = ctx.roles.get(key, {}) or {}
        record_session(ctx.metrics, SessionMetric(
            role=role, slot=str(role_cfg.get("account", role)),
            model=result.model or str(role_cfg.get("model", "")),
            duration_s=result.elapsed_s, exit_code=result.returncode,
            usage=dict(result.usage or {})))

    def _record_gates(
        self, ctx: _Ctx, phase: str, results: Sequence[GateResult]) -> None:
        for item in results:
            ctx.gate_runs.append({"phase": phase, **item.to_dict()})

    @staticmethod
    def _read_artifact(ctx: _Ctx, name: str) -> str:
        try:
            return (_tier_dir(ctx.repo) / name).read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # -- prompts --------------------------------------------------------------------

    def _planner_prompt(self, ctx: _Ctx) -> str:
        ticket = ctx.ticket
        acceptance = ticket.get("acceptance") or []
        if isinstance(acceptance, str):
            acceptance = [acceptance]
        criteria = "\n".join(f"- {line}" for line in acceptance)
        answer = ((ticket.get("params") or {}).get("owner_answer") or {})
        owner_note = ""
        if isinstance(answer, dict):
            comment = str(answer.get("comment") or "").strip()
            decision = str(answer.get("decision") or "").strip()
            if comment or decision:
                # H1 (revisión 00206): el plan reanudado debe saber qué
                # decidió el Owner; sin esto planea a ciegas.
                owner_note = (
                    f"\nOWNER ANSWER ({decision or 'no decision'}): "
                    f"{comment or '(no text)'}\n"
                    "Use it as a binding decision for this plan.\n"
                )
        body = (
            f"You plan ticket {ctx.ref}: {ticket.get('title', '')}\n"
            f"{ticket.get('description', '')}\n"
            f"Acceptance:\n{criteria or '- (derive verifiable criteria in PLAN.md)'}\n"
            f"{owner_note}"
            "First decide SIZE: if this cannot fit one session "
            f"(≤{ctx.budgets.get('files_per_ticket', 8)} files, "
            f"≤{ctx.budgets.get('lines_per_ticket', 400)} lines, tests excluded), "
            "write .openceo/SIZING.md (why + rough split) and emit "
            f"{SIGNALS['objective_needed']} in .openceo/status, then stop.\n"
            "Otherwise write .openceo/PLAN.md (§16.1: TICKET/OBJECTIVE/GOAL/"
            "ACCEPTANCE/FILES with create|modify|delete + why/CONTRACTS/DECISIONS/"
            "STEPS/ESTIMATE files=<n> lines=<n>/OUT_OF_SCOPE) and emit "
            f"{SIGNALS['plan_ready']} in .openceo/status.\n"
            "Decide open questions yourself and record them in DECISIONS; only "
            "use [OWNER] when the ticket explicitly marks a decision so."
        )
        vault_index = getattr(ctx, "vault_index", "")
        if vault_index:
            body += ("\n" + vault_index + "\nRead the pertinent notes "
                     "before planning: they are in the read-only project "
                     "folder, on demand.")
        return knowledge_mod.with_preamble(body, ctx.repo, "en")

    def _plan_review_prompt(self, ctx: _Ctx, plan: Plan) -> str:
        files = ", ".join(item.path for item in plan.files[:20])
        body = (
            "Read-only review of .openceo/PLAN.md (pointers, no pasted diffs).\n"
            f"Ticket goal: {plan.goal}\nPlanned files: {files}\n"
            "Write .openceo/PLAN_REVIEW.md with numbered observations "
            "(O<n> [CRITICAL|WARN] <PLAN.md|repo-path>:<line> — <problem> → "
            "<suggestion>, each with a literal `>` quote), or `No findings.`.\n"
            "End the report with REVIEW_DONE_plan_0."
        )
        return knowledge_mod.with_preamble(body, ctx.repo, "en")

    def _plan_response_prompt(
        self, ctx: _Ctx, observations: Sequence[Any]) -> str:
        if not observations:
            return knowledge_mod.with_preamble(
                "No plan observations. Emit "
                f"{SIGNALS['plan_responded']} in .openceo/status.",
                ctx.repo, "en")
        ids = ", ".join(f"O{obs.id}" for obs in observations)
        body = (
            f"Answer each plan observation ({ids}) in .openceo/PLAN_RESPONSE.md: "
            "`O<n>: ACEPTADA — <what changed>` (and apply it to PLAN.md), "
            "`RECHAZADA — <reason>` or `DIFERIDA — <reason>`. "
            f"Then emit {SIGNALS['plan_responded']} in .openceo/status."
        )
        return knowledge_mod.with_preamble(body, ctx.repo, "en")

    def _builder_prompt(self, ctx: _Ctx, plan: Plan) -> str:
        files = "\n".join(f"- {item.path} ({item.operation}): {item.reason}"
                          for item in plan.files)
        steps = "\n".join(f"{num}. {step}"
                          for num, step in enumerate(plan.steps, 1))
        body = (
            f"Implement ticket {ctx.ref} from .openceo/PLAN.md + "
            ".openceo/PLAN_RESPONSE.md.\n"
            f"Goal: {plan.goal}\nFiles:\n{files}\nSteps:\n{steps}\n"
            "Do NOT commit, push, rebase, stash, or change branches: leave your "
            "work uncommitted; the orchestrator integrates it.\n"
            "Write .openceo/HANDOFF.md (§16.6, executor sections ≤300 words) "
            f"and emit {SIGNALS['build_done']} in .openceo/status.\n"
            f"Previous checkpoint (recheck; never repeat confirmed effects):\n{TierSCycle._read_artifact(ctx, 'WAIT.md')}\n"
            + AGENT_RESULT_PROMPT
        )
        vault_index = getattr(ctx, "vault_index", "")
        if vault_index:
            body += ("\n" + vault_index + "\nRead the pertinent notes "
                     "before implementing: they are in the read-only project "
                     "folder, on demand.")
        return knowledge_mod.with_preamble(body, ctx.repo, "en")
