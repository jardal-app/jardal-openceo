from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .review_links import ReviewLinksExtraction, apply_review_links_to_params, extract_review_links
from .loose_tier import (ASSIGNED_BY, LooseTierDecision, classify_loose_tier,
                          project_money_routing, record_loose_tier_decision)
from .routing import (ROUTING_MISSING_REASON, TIER_C_CHAIN, RoutingDecision,
                      resolve_coding_tool, select_tier_c_link)
from .tier_oracle import (
    freeze_oracle,
    oracle_run_plan,
    unresolved_oracle_refs,
    verify_oracle,
)
from .tier_s.cycle import TierSCycle
from .tier_s.gates import (baseline_blocked, gates_failed, handoff_gates_section,
                         load_gates, run_gate, run_gates)
from .tier_s.models import GateSpec
from catalog import CatalogError, load_role_profile
from mcp_identity import McpAuthError
from orchestration.inbox import InMemoryInboxStore, MongoInboxStore
from orchestration.knowledge import detect_prompt_lang, with_preamble
from orchestration.live import AllowAllQuota, ProcessExecutor, RateMonitorQuota
from orchestration.role_muse import (
    SETTINGS_OVERLAY_RELPATH,
    ObjectiveBadgeProvider,
    RoleProfileError,
    TicketBadgeProvider,
    issuer_for_repo,
    render_argv_extras,
    render_card_argv,
    settings_overlay_bytes,
)
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    MongoObjectiveStore,
    MongoObjectiveTaskLister,
    ObjectiveEnv,
    checkout_and_update_objective_branch,
    plan_objective,
    record_material_failure,
    record_tier_c,
)
from objectives.service import (
    create_internal_ticket,
    erp_for_task_service,
    objectives_process_executor,
    objectives_wiring_config,
)
from orchestration.review import InMemoryReviewStore, InMemorySlotStore, MongoReviewStore, MongoSlotStore, parse_pool
from .tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
    MongoLockStore,
    MongoRunStore,
    MongoTaskStore,
)
from objectives.agent_result import (
    AGENT_RESULT_PROMPT,
    SCHEMA_PATH as AGENT_RESULT_SCHEMA_PATH,
    parse_agent_result,
)
from .tmux_runtime import TmuxRuntime
from config_loader import ConfigLoader
from rate_monitor import RateMonitor
from settings import Settings
from state import CodingTool, TaskStatus

from .tier_s_contract import child_env

logger = logging.getLogger(__name__)

#: Typed wait reason when the project's config is invalid (Tier S design §15).
CONFIG_INVALID_REASON = "config"

#: Typed wait reason when another execution holds the repo lock (§11.1).
REPO_BUSY_REASON = "repo_busy"


def _resolve_execution_repo(project_config: dict[str, Any]) -> str | None:
    """Repo path this execution would work on, for the common lock."""
    repos = (project_config.get("project") or {}).get("repos") or []
    if not repos:
        return None
    local_path = repos[0].get("local_path") or repos[0].get("path")
    if not local_path:
        return None
    path = Path(str(local_path))
    try:
        if path.exists():
            return str(path.resolve())
    except OSError:
        pass
    return str(local_path)


@dataclass
class DevExecutionOutput:
    status: TaskStatus
    steps: list[dict[str, Any]] = field(default_factory=list)
    coding_tool: CodingTool | None = None
    coding_session_id: str | None = None
    coding_session_title: str | None = None
    last_prompt_sent: str | None = None
    last_response_summary: str | None = None
    waiting_reason: str | None = None
    resume_at: datetime | None = None
    error: str | None = None
    modified_files: list[str] = field(default_factory=list)
    params: dict[str, Any] | None = None

    def as_state_patch(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "steps": self.steps,
            "coding_tool": self.coding_tool.value if self.coding_tool else None,
            "coding_session_id": self.coding_session_id,
            "coding_session_title": self.coding_session_title,
            "last_prompt_sent": self.last_prompt_sent,
            "last_response_summary": self.last_response_summary,
            "waiting_reason": self.waiting_reason,
            "resume_at": self.resume_at,
            "error": self.error,
            "modified_files": self.modified_files,
            "params": self.params,
        }


@dataclass
class TierExecutor:
    """One-shot command resolved from a tier (design v2 §3.1, F0.1).

    ``argv`` is the base command without the prompt; ``prompt_via`` tells
    the caller whether the prompt travels as the last argv element or on
    stdin. ``label`` (``tier_a``, ``tier_c:muse``…) identifies the executor
    in steps and logs.
    """

    argv: list[str]
    prompt_via: str  # "argv" | "stdin"
    cwd: str | None
    env: dict[str, str]
    label: str
    role_profile: str | None = None


@dataclass
class _SimpleBranch:
    """Work branch owned by one simple-tier ticket (F5, §10/§12)."""

    repo: Path
    branch: str
    base: str
    base_sha: str
    ref: str
    oracle_frozen: dict[str, Any] | None = None
    #: Why a frozen ``preexisting_tests`` oracle is NOT run after the build:
    #: it was already red on the base branch, so it can neither accept nor
    #: reject this work (TASK-2026-00178). "" = it runs as a gate.
    oracle_baseline_red: str = ""


def _task_with_loose_tier(
    task: dict[str, Any], classified: LooseTierDecision,
) -> dict[str, Any]:
    """Copy of ``task`` carrying the loose-tier assignment in ``params``.

    Same keys the planner path uses (``tier_reason``, ``oracle``,
    ``why_*`` for S) plus provenance, so downstream (oracle freeze,
    telemetry, persistence funnel) treats both paths uniformly.
    """
    params = dict(task.get("params") or {})
    params["tier_reason"] = classified.reason
    params["tier_assigned_by"] = ASSIGNED_BY
    params["tier_signals"] = list(classified.signals)
    if classified.oracle is not None:
        params["oracle"] = classified.oracle
    if classified.why_consensus_is_needed is not None:
        params["why_consensus_is_needed"] = classified.why_consensus_is_needed
    if classified.why_single_session_is_insufficient is not None:
        params["why_single_session_is_insufficient"] = (
            classified.why_single_session_is_insufficient)
    if classified.why_not_c is not None:
        params["why_not_c"] = classified.why_not_c
    return {**task, "params": params}


def _merge_loose_params(
    enriched: dict[str, Any] | None, downstream: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reattach classification keys dropped by the execution path.

    Downstream keys win on conflict; keys only differ in practice for
    ``tier_reason``-style classification outputs, which downstream never
    writes.
    """
    if not enriched:
        return downstream
    if downstream is None:
        return dict(enriched)
    merged = dict(enriched)
    merged.update(downstream)
    return merged


class DevSessionManager:
    """Runs deterministic step-based Dev Agent sessions."""

    def __init__(self, settings: Settings, config_loader: ConfigLoader,
                 rate_monitor: RateMonitor | None = None, repo: Any | None = None) -> None:
        self._settings = settings
        self._config_loader = config_loader
        self._rate_monitor = rate_monitor
        self._repo = repo
        self._task_service: Any | None = None

    def set_task_service(self, task_service: Any) -> None:
        """Late-bind the service (ERP comments + internal tickets, F3–F6).

        main.py binds after construction, like pm_step_manager. Unbound
        dispatch still runs cycles; notices stay in steps/summaries and
        inbox promotions defer to telemetry.
        """
        self._task_service = task_service

    async def run(self, task: dict[str, Any], project_config: dict[str, Any]) -> DevExecutionOutput:
        qa_cfg = project_config.get("dev", {}).get("qa", {})

        # Single routing reader (Tier S design §3): the top-level
        # coding_tool, a tier letter written at creation.
        decision = resolve_coding_tool(task)
        classified: LooseTierDecision | None = None
        if decision.tool is None:
            if task.get("objective_id"):
                # Objective children must arrive with the planner tier; auto-
                # assigning one would mask a planner/validation gap, so this
                # path stays fail-closed. The ERP comment for the wait is
                # posted by task_service.
                return DevExecutionOutput(
                    status=TaskStatus.WAITING,
                    waiting_reason=f"{ROUTING_MISSING_REASON}: {decision.detail}",
                    params=task.get("params") if isinstance(task.get("params"), dict) else None,
                )
            # Loose ticket (TASK-2026-00137): deterministic tier authority,
            # zero tokens. Never parks in routing_missing. A money-sensitive
            # project (TASK-2026-00161) adds its domain signal here, same as
            # the dispatcher peek.
            classified = classify_loose_tier(
                str(task.get("title") or ""),
                str(task.get("description") or ""),
                task.get("attachments") or [],
                project_routing=project_money_routing(project_config),
            )
            logger.info(
                "loose tier task_id=%s tier=%s signals=%s",
                task.get("task_id"), classified.tier.value,
                ",".join(classified.signals) or "default",
            )
            task = _task_with_loose_tier(task, classified)
            await record_loose_tier_decision(
                self._repo, task_id=str(task.get("task_id") or ""),
                ticket_number=task.get("ticket_number"),
                decision=classified)
            decision = RoutingDecision(
                classified.tier, "loose_classifier",
                f"loose_classifier={classified.tier.value}",
            )

        # Invalid project config parks every ticket, whatever the tier.
        config_errors = self._config_loader.validate_project(task.get("project"))
        if config_errors:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{CONFIG_INVALID_REASON}: {'; '.join(config_errors)}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )

        # Common execution lock (§11.1): one repo, one execution, every tier.
        # Tier S owns its own locking inside the cycle (run-scoped holder);
        # taking the outer lock here too would deadlock the cycle against
        # itself, so Tier S skips it.
        repo = _resolve_execution_repo(project_config) if self._repo is not None else None
        holder = str(task.get("task_id") or "")
        outer_lock = (repo is not None and bool(holder)
                      and decision.tool is not CodingTool.TIER_S)
        if outer_lock:
            assert repo is not None
            if await self._repo.acquire_repo_lock(repo, holder) is None:
                return DevExecutionOutput(
                    status=TaskStatus.WAITING,
                    waiting_reason=f"{REPO_BUSY_REASON}: {repo}",
                    resume_at=datetime.now(timezone.utc) + timedelta(minutes=2),
                    params=task.get("params") if isinstance(task.get("params"), dict) else None,
                )
        try:
            if decision.tool == CodingTool.TIER_S:
                output = await self._run_tier_s(task, project_config, qa_cfg)
            else:
                output = await self._run_simple_tool(task, project_config, decision.tool, qa_cfg)
        except Exception:
            if outer_lock:
                assert repo is not None
                await self._repo.release_repo_lock(repo, holder)
            raise
        if outer_lock:
            assert repo is not None
            if output.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED):
                await self._repo.release_repo_lock(repo, holder)
            elif output.status == TaskStatus.WAITING and output.resume_at is not None:
                # Resumable wait (transient/quota): the session comes back.
                await self._repo.touch_repo_lock(repo, holder)
            elif output.status == TaskStatus.WAITING:
                # Human wait (needs_owner, no resume): the session never comes
                # back by itself (TASK-2026-00122); holding the lock would wedge
                # the repo for 24h (live: E1 diagnostic blocked E2 review).
                await self._repo.release_repo_lock(repo, holder)
        if classified is not None:
            # The simple-tier path drops input params when no review links
            # change; reattach the classification so it persists.
            output.params = _merge_loose_params(
                task.get("params"), output.params)
        return output

    async def _run_simple_tool(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        qa_cfg: dict[str, Any],
    ) -> DevExecutionOutput:
        """Simple tiers (design §10, F5): one ticket, one session, gates.

        With a resolvable repo the ticket runs the full flow — work branch
        (+ frozen oracle when a qualifying one is declared, policy §24), baseline, one
        executor session, orchestrator gates with retries, size brake with
        material failure (policy §29, no automatic retier to ``tier_s``),
        mandatory handoff, squash and merge. Without a repo (synthetic
        configs, unit tests) it keeps the single code step. ``dev.qa`` is
        retired here: gates replaced it.
        """
        del qa_cfg
        output = DevExecutionOutput(
            status=TaskStatus.IN_PROGRESS,
            coding_tool=tool,
        )

        repo_path = self._resolve_repo_path(project_config)
        branch: _SimpleBranch | None = None
        if repo_path is not None and self._settings.DEV_EXECUTION_MODE != "mock":
            opened = self._open_simple_branch(
                task, project_config, tool, Path(repo_path))
            if isinstance(opened, DevExecutionOutput):
                return opened
            branch = opened

        if tool == CodingTool.TIER_C:
            await self._record_tier_c_event(task, "started")
        code_result = await self._step_code(task, project_config, tool)
        output.steps.append(code_result.step)
        output.coding_session_id = code_result.session_id
        output.coding_session_title = code_result.session_title
        output.last_prompt_sent = code_result.prompt
        output.last_response_summary = code_result.summary
        output.params = apply_review_links_to_params(
            task.get("params"),
            code_result.review_links,
        )
        if code_result.executor_link:
            # The Tier C chain link that ran this ticket (policy §25/§32):
            # the chain is availability, not selection, so the link is
            # recorded per ticket. The funnel replaces ``params``, so start
            # from the full set (never a partial dict).
            params = dict(output.params if output.params is not None
                          else (task.get("params") or {}))
            params["executor_link"] = code_result.executor_link
            output.params = params
            await self._record_tier_c_event(
                task, "executor", executor_link=code_result.executor_link)
        if code_result.waiting_reason:
            if branch is not None:
                self._drop_simple_branch(branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = code_result.waiting_reason
            output.resume_at = code_result.resume_at or (datetime.now(timezone.utc) + timedelta(hours=2))
            return output
        if code_result.error:
            if branch is not None:
                self._drop_simple_branch(branch)
            if "rate limit" in code_result.error.lower():
                output.status = TaskStatus.WAITING
                output.waiting_reason = code_result.error
                output.resume_at = datetime.now(timezone.utc) + timedelta(hours=2)
            else:
                output.status = TaskStatus.FAILED
                output.error = code_result.error
            return output

        verdict = (code_result.agent_result or {}).get("resultado")
        if verdict in {"bloqueado", "no_aplica"}:
            # The agent declined: no gates, no merge. The verdict rides
            # in params for the post-persist return (TASK-2026-00202).
            return self._return_agent_verdict(
                task, tool, branch, code_result, output.params)

        if branch is None:
            output.status = TaskStatus.COMPLETED
            return output
        return await self._finish_simple_tool(
            task, project_config, tool, output, branch)

    # -- simple-tier git flow (F5, §10/§12) -------------------------------------

    @staticmethod
    def _simple_ref(task: dict[str, Any]) -> str:
        return str(task.get("ref") or task.get("ticket_number")
                   or str(task.get("task_id") or "")[:8])

    @staticmethod
    def _simple_git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=repo, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            timeout=timeout)

    def _reset_and_drop_branch(self, branch: _SimpleBranch) -> None:
        """Reset the work branch to base and drop it (declined/failed)."""
        self._simple_git(branch.repo, "reset", "--hard", "-q", branch.base_sha)
        self._simple_git(branch.repo, "clean", "-fdq")
        self._drop_simple_branch(branch)

    def _open_simple_branch(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        repo: Path,
    ) -> "_SimpleBranch | DevExecutionOutput":
        """Open the ticket work branch (+ baseline gates, §10/§12)."""
        dev_cfg = project_config.get("dev", {}) or {}
        repos = project_config.get("project", {}).get("repos", []) or []
        base = str((repos[0].get("branch_base") if repos else None) or "main")
        ref = self._simple_ref(task)
        objective_id = str(task.get("objective_id") or "")
        auto_merge = bool(dev_cfg.get("auto_merge", True))
        if objective_id and not auto_merge:
            try:
                checkout_and_update_objective_branch(
                    repo, objective_id, base)
            except Exception as exc:
                return DevExecutionOutput(
                    status=TaskStatus.WAITING,
                    waiting_reason=f"needs_owner: merge_conflict: {exc}",
                    params=task.get("params") if isinstance(task.get("params"), dict) else None,
                )
            base = f"objective/{objective_id}"
        branch = f"tier/{tool.value}-{ref}-{uuid.uuid4().hex[:6]}"
        if self._simple_git(repo, "rev-parse", "--verify", base).returncode != 0:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"config: unknown base branch {base}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )
        if self._simple_git(repo, "checkout", "-b", branch, base).returncode != 0:
            self._simple_git(repo, "checkout", "-q", base)
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason="transient: cannot open work branch",
                resume_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )
        base_sha = self._simple_git(repo, "rev-parse", "HEAD").stdout.strip()
        try:
            specs = load_gates(dev_cfg)
        except ValueError as exc:
            self._simple_git(repo, "checkout", "-q", base)
            self._simple_git(repo, "branch", "-D", branch)
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"config: gates: {exc}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )
        blocked = baseline_blocked(run_gates(repo, specs), specs)
        if blocked:
            names = ", ".join(item.name for item in blocked)
            self._simple_git(repo, "checkout", "-q", base)
            self._simple_git(repo, "branch", "-D", branch)
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"needs_owner: baseline_red: {names}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )
        # Oracle protection (§24) applies to any simple tier with a
        # declared qualifying oracle; freeze_oracle returns None when
        # there is nothing qualifying to protect.
        params = task.get("params") \
            if isinstance(task.get("params"), dict) else {}
        oracle_frozen = freeze_oracle(
            repo, base_sha, (params or {}).get("oracle"), ref)
        # A preexisting oracle must be green on the base to judge the work.
        # Red there (an environment problem, or a test cited for the very
        # failure the ticket fixes) it only stays hash-protected: parking
        # the ticket would stall the autonomy and failing it after the
        # build would discard the work. Other types are red at baseline by
        # design (a reproduction, a spec written before the build).
        oracle_baseline_red = ""
        if oracle_frozen is not None \
                and oracle_frozen.get("oracle_type") == "preexisting_tests":
            run, _ = oracle_run_plan(oracle_frozen, dev_cfg)
            if run is not None:
                probe = run_gate(repo, GateSpec(
                    name="oracle", cmd=run.cmd,
                    timeout_minutes=run.timeout_minutes, required=True))
                if probe.status != "pass":
                    oracle_baseline_red = probe.status + (
                        f" ({probe.detail})" if probe.detail else "")
        return _SimpleBranch(repo=repo, branch=branch, base=base,
                             base_sha=base_sha, ref=ref,
                             oracle_frozen=oracle_frozen,
                             oracle_baseline_red=oracle_baseline_red)

    def _drop_simple_branch(self, branch: _SimpleBranch) -> None:
        self._simple_git(branch.repo, "checkout", "-q", branch.base)
        self._simple_git(branch.repo, "branch", "-D", branch.branch)

    def _simple_diff_sizes(
        self, branch: _SimpleBranch, exclude: list[str],
    ) -> tuple[int, int]:
        """(files, lines) vs the base, minus tests and budget_exclude."""
        proc = self._simple_git(
            branch.repo, "diff", "--no-renames", "--numstat", "-z",
            branch.base_sha, "--", ".")
        files = 0
        lines = 0
        for record in proc.stdout.split("\0"):
            fields = record.split("\t")
            if len(fields) < 3 or not fields[2]:
                continue
            path = fields[2]
            if path == ".tier-s" or path.startswith(".tier-s/"):
                continue
            try:
                added = int(fields[0]) if fields[0] != "-" else 0
                deleted = int(fields[1]) if fields[1] != "-" else 0
            except ValueError:
                continue
            lowered = path.lower()
            if lowered.startswith(("tests/", "test/")) or "/tests/" in lowered:
                continue
            if any(fnmatch.fnmatch(path, pat) for pat in exclude):
                continue
            files += 1
            lines += added + deleted
        return files, lines

    def _simple_changed_files(self, branch: _SimpleBranch) -> list[str]:
        proc = self._simple_git(
            branch.repo, "diff", "--name-only", "-z",
            branch.base_sha, "--", ".")
        names = [part for part in proc.stdout.split("\0") if part]
        untracked = self._simple_git(
            branch.repo, "ls-files", "--others", "--exclude-standard", "-z")
        names.extend(part for part in untracked.stdout.split("\0") if part)
        return sorted({name for name in names
                       if name != ".tier-s" and not name.startswith(".tier-s/")})

    async def _oracle_integrity(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        branch: _SimpleBranch,
        output: DevExecutionOutput,
        *,
        recheck: bool = False,
    ) -> DevExecutionOutput | None:
        """Verify the frozen oracle hashes (policy §24).

        ``None`` when intact or when nothing was frozen. When the builder
        touched a protected ref, the material failure (policy §29) is
        returned. ``recheck`` is the verification after a gate-retry turn:
        it only reports a failure, so a clean recheck adds no step. The
        failure names where the oracle came from: a ref the loose
        classifier read out of the ticket text is the likelier false
        positive, and the owner should see that at a glance.
        """
        if branch.oracle_frozen is None:
            return None
        tampered = verify_oracle(branch.repo, branch.oracle_frozen)
        if tampered:
            names = ", ".join(tampered)
            params = task.get("params") if isinstance(task.get("params"), dict) else {}
            origin = ("frozen from the ticket text by the loose classifier"
                      if (params or {}).get("tier_assigned_by")
                      else "declared by the planner")
            output.steps.append(self._step(
                "oracle", "failed",
                f"protected oracle refs modified by builder: {names} ({origin})"))
            if tool == CodingTool.TIER_C:
                await self._record_tier_c_event(task, "cycle_failure")
            return await self._material_simple_failure(
                task, project_config, tool, branch,
                f"oracle modified: {names} ({origin})",
                prior_steps=output.steps, params_base=output.params)
        if not recheck:
            protected = sum(1 for item in
                            branch.oracle_frozen.get("refs", [])
                            if isinstance(item, dict) and item.get("resolved"))
            output.steps.append(self._step(
                "oracle", "ok",
                f"{protected} protected oracle refs intact "
                f"({branch.oracle_frozen.get('oracle_type')})"))
        return None

    async def _finish_simple_tool(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        output: DevExecutionOutput,
        branch: _SimpleBranch,
    ) -> DevExecutionOutput:
        """Gates, brake, handoff, squash, merge after the session (§10)."""
        dev_cfg = project_config.get("dev", {}) or {}
        budgets = (dev_cfg.get("tier_s", {}) or {}).get("budgets", {}) or {}
        exclude = list(dev_cfg.get("budget_exclude", []) or [])
        repo = branch.repo
        if branch.oracle_frozen is not None:
            # The funnel replaces ``params``: build on the FULL set, never
            # on a partial dict (``output.params`` is None when no review
            # link changed; TASK-2026-00178).
            params = dict(output.params if output.params is not None
                          else (task.get("params") or {}))
            params["oracle_frozen"] = branch.oracle_frozen
            output.params = params

        current = self._simple_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if current != branch.branch:
            output.status = TaskStatus.WAITING
            output.waiting_reason = (
                f"needs_owner: executor left branch {branch.branch} for {current}")
            return output
        head = self._simple_git(repo, "rev-parse", "HEAD").stdout.strip()
        if head != branch.base_sha:
            self._simple_git(repo, "reset", "--soft", branch.base_sha)
        if not self._simple_changed_files(branch):
            self._drop_simple_branch(branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = "needs_owner: executor produced no changes"
            return output

        tampered_out = await self._oracle_integrity(
            task, project_config, tool, branch, output)
        if tampered_out is not None:
            return tampered_out

        try:
            specs = load_gates(dev_cfg)
        except ValueError as exc:
            self._drop_simple_branch(branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = f"config: gates: {exc}"
            return output
        # Policy §24: the orchestrator, not the builder, RUNS the frozen
        # oracle -- a required gate after the build, with the gates'
        # retries. Not at baseline: a bug reproduction fails there by
        # design. Without ``dev.oracle_cmd`` the limit is stated, never
        # silent (TASK-2026-00178).
        oracle_run, oracle_note = oracle_run_plan(branch.oracle_frozen, dev_cfg)
        if oracle_run is not None and branch.oracle_baseline_red:
            oracle_run, oracle_note = None, (
                "frozen oracle was already red on the base branch "
                f"({branch.oracle_baseline_red}): it cannot accept or reject "
                "this work, so it stays hash-protected only")
        if oracle_run is not None:
            specs = [*specs, GateSpec(
                name="oracle", cmd=oracle_run.cmd,
                timeout_minutes=oracle_run.timeout_minutes, required=True)]
            # Mixed refs (TASK-2026-00167): the gate runs the resolved
            # ones; the rest must figure as not verified, never hide
            # behind the "oracle ok" verdict. The note branches below
            # already cover the fully-unverified cases.
            unverified = unresolved_oracle_refs(branch.oracle_frozen)
            if unverified:
                output.steps.append(self._step(
                    "oracle_refs", "skipped",
                    "declared oracle refs not verified "
                    "(resolved to no file): " + ", ".join(unverified)))
        elif oracle_note:
            output.steps.append(self._step("oracle_run", "skipped", oracle_note))
        retries = int(budgets.get("gate_retries", 2) or 0)
        attempt = 0
        while True:
            results = await asyncio.to_thread(run_gates, repo, specs)
            failed = gates_failed(results)
            output.steps.append(self._step(
                "gates", "ok" if not failed else "failed",
                "; ".join(f"{item.name}={item.status}" for item in results)
                or "no gates configured"))
            if not failed:
                break
            if tool == CodingTool.TIER_C:
                await self._record_tier_c_event(task, "cycle_failure")
            if attempt >= retries:
                names = ", ".join(item.name for item in failed)
                return await self._material_simple_failure(
                    task, project_config, tool, branch,
                    f"gates failed after {retries} retries: {names}",
                    prior_steps=output.steps, params_base=output.params)
            tail = "\n\n".join(
                f"[{item.name}] {item.status}: {item.detail}\n{item.output_tail}"
                for item in failed)
            await self._simple_followup(
                tool, project_config, repo,
                f"Gates failed:\n{tail[:6000]}\nFix the code (no commits), "
                "keep HANDOFF.md truthful. Never edit the frozen oracle "
                "files: fix the code under test.",
                task_id=str(task.get("task_id") or ""),
                project=str(task.get("project") or ""))
            # The retry saw the failing oracle's output: re-verify its hashes
            # before running the gates again (a "fix" by editing the test is
            # the oracle being redefined).
            tampered_out = await self._oracle_integrity(
                task, project_config, tool, branch, output, recheck=True)
            if tampered_out is not None:
                return tampered_out
            attempt += 1

        files_n, lines_n = self._simple_diff_sizes(branch, exclude)
        files_budget = int(budgets.get("files_per_ticket", 8) or 8)
        lines_budget = int(budgets.get("lines_per_ticket", 400) or 400)
        brake = int(budgets.get("hard_brake_factor", 2) or 2) * lines_budget
        if files_n > files_budget or lines_n > brake:
            return await self._material_simple_failure(
                task, project_config, tool, branch,
                f"size {files_n} files/{lines_n} lines over budget "
                f"({files_budget}/{brake})",
                prior_steps=output.steps, params_base=output.params)

        handoff_path = repo / ".tier-s" / "HANDOFF.md"
        handoff = ""
        try:
            handoff = handoff_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            handoff = ""
        if not handoff or len(handoff.split()) > 600:
            await self._simple_followup(
                tool, project_config, repo,
                "Write .tier-s/HANDOFF.md (executor sections ≤300 words). "
                "Do not touch code.",
                task_id=str(task.get("task_id") or ""),
                project=str(task.get("project") or ""))
            try:
                handoff = handoff_path.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                handoff = ""
            if not handoff:
                output.status = TaskStatus.WAITING
                output.waiting_reason = (
                    f"needs_owner: missing HANDOFF.md (branch kept: {branch.branch})")
                return output

        # Squash + merge (§12): only the orchestrator commits.
        self._simple_git(repo, "add", "-A", "--", ".", ":!.tier-s")
        identity: list[str] = []
        name = self._simple_git(repo, "config", "user.name").stdout.strip()
        email = self._simple_git(repo, "config", "user.email").stdout.strip()
        if not (name and email):
            identity = ["-c", "user.name=OpenCEO",
                        "-c", "user.email=openceo@jardal.app"]
        title = " ".join(str(task.get("title", "")).split())[:72]
        commit = self._simple_git(
            repo, *identity, "commit", "-qm", f"{branch.ref}: {title}")
        if commit.returncode != 0:
            self._drop_simple_branch(branch)
            output.status = TaskStatus.FAILED
            output.error = commit.stderr.strip()[:300] or "commit failed"
            return output
        sha = self._simple_git(repo, "rev-parse", "HEAD").stdout.strip()
        files = self._simple_changed_files(branch)
        completed_handoff = self._complete_simple_handoff(
            handoff, sha, files, results)
        try:
            handoff_path.write_text(completed_handoff, encoding="utf-8")
        except OSError:
            pass
        auto_merge = bool(dev_cfg.get("auto_merge", True))
        if auto_merge or task.get("objective_id"):
            if self._simple_git(repo, "checkout", "-q", branch.base).returncode != 0:
                output.status = TaskStatus.WAITING
                output.waiting_reason = (
                    f"needs_owner: merge_conflict: cannot checkout {branch.base} "
                    f"(branch kept: {branch.branch})")
                return output
            if self._simple_git(repo, "merge", "--ff-only", "-q", branch.branch).returncode != 0:
                merged = self._simple_git(
                    repo, "merge", "--no-ff", "--no-edit", branch.branch)
                if merged.returncode != 0:
                    self._simple_git(repo, "merge", "--abort")
                    output.status = TaskStatus.WAITING
                    output.waiting_reason = (
                        "needs_owner: merge_conflict: "
                        f"{merged.stderr.strip()[:200]} "
                        f"(branch kept: {branch.branch})")
                    return output
            self._drop_simple_branch(branch)
            branch_note = f"merged into {branch.base}"
        else:
            branch_note = (f"branch kept: {branch.branch} "
                           "(open a PR; policy auto_merge=false, §12)")

        if task.get("objective_id"):
            # The funnel notifies the objective after this COMPLETED is
            # persisted (TASK-2026-00169): stash the close context for it
            # instead of triggering on a stale pre-persist read.
            params = dict(output.params if output.params is not None
                          else (task.get("params") or {}))
            params["_objective_close"] = {
                "task_id": str(task.get("task_id") or ""),
                "title": str(task.get("title") or ""),
                "tier": str(task.get("coding_tool") or ""),
                "sha": sha,
                "files": list(files),
                "handoff_excerpt": completed_handoff[:1500],
            }
            params.pop("_objective_close_notified", None)
            output.params = params
        if tool == CodingTool.TIER_C:
            # The funnel absorbs the SHA into tickets_log post-persist.
            await self._record_tier_c_event(task, "closed", attributable=True)
        output.status = TaskStatus.COMPLETED
        output.modified_files = files
        output.last_response_summary = (
            f"{tool.value} closed: {branch_note}, {sha[:12]}, "
            f"{len(files)} files.")
        output.steps.append(self._step(
            "close", "ok", output.last_response_summary))
        return output

    @staticmethod
    def _complete_simple_handoff(
        text: str, sha: str, files: list[str], gates: list[Any],
    ) -> str:
        """Fill the orchestrator-owned handoff lines (SHA, files, gates)."""
        files_line = ", ".join(files) if files else "none"
        out: list[str] = []
        for line in text.splitlines():
            if line.startswith("SHA:"):
                out.append(f"SHA: {sha}")
            elif line.startswith("Files changed:"):
                out.append(f"Files changed: {files_line}")
            elif line.startswith("Gates:"):
                out.append(handoff_gates_section(gates).replace("\n", " | "))
            else:
                out.append(line)
        if not any(line.startswith("SHA:") for line in out):
            out.insert(1, f"SHA: {sha}")
        return "\n".join(out) + "\n"

    async def _simple_followup(
        self,
        tool: CodingTool,
        project_config: dict[str, Any],
        repo: Path,
        prompt: str,
        *,
        task_id: str = "",
        project: str = "",
    ) -> None:
        """One follow-up turn on the work branch (gate/handoff fixes)."""
        prompt = with_preamble(prompt, repo, detect_prompt_lang(prompt))
        try:
            executor = await self._resolve_tier_executor(tool, project_config)
        except ValueError:
            return
        if executor.prompt_via == "argv":
            command = [*executor.argv, prompt]
            stdin_payload: str | None = None
        else:
            command = list(executor.argv)
            stdin_payload = prompt
        overlays: dict[str, bytes] | None = None
        badge: str | None = None
        badge_provider: TicketBadgeProvider | None = None
        if executor.label == "tier_c:muse":
            badge_provider = self._ticket_badge_provider(task_id, project)
            if badge_provider is None:
                logger.error("follow-up refused without repository: %s",
                             task_id or "unknown task")
                return
            try:
                overlays, badge = await self._muse_role_overlays(
                    badge_provider, str(executor.label), str(repo),
                    executor.role_profile or "dev")
            except (RoleProfileError, CatalogError, McpAuthError,
                    ValueError, OSError) as exc:
                logger.error("follow-up refused without role profile: %s",
                             exc)
                return
        try:
            await self._run_process(
                command, stdin_payload=stdin_payload, timeout=1800,
                cwd=str(repo), env=executor.env or None,
                config_argv=list(executor.argv),
                home_file_overlays=overlays)
        finally:
            if badge is not None and badge_provider is not None:
                await badge_provider.revoke(badge)

    async def _task_cancelled(self, task_id: str) -> bool:
        """Fresh cancel check against the stored ticket (fail-open).

        A missing repo, a store without ``get_task`` or a failed read
        answers False: the patch-level net still fences the writes
        (TASK-2026-00177).
        """
        if not task_id or self._repo is None:
            return False
        get_task = getattr(self._repo, "get_task", None)
        if not callable(get_task):
            return False
        try:
            fresh = await get_task(task_id)
        except Exception:
            logger.warning("cancel check failed for %s", task_id,
                           exc_info=True)
            return False
        return (fresh is not None
                and str(fresh.get("status") or "")
                == TaskStatus.CANCELLED.value)

    def _return_agent_verdict(
        self,
        task: dict[str, Any],
        tool: CodingTool,
        branch: _SimpleBranch | None,
        code_result: CodingStepResult,
        params_base: dict[str, Any] | None = None,
    ) -> DevExecutionOutput:
        """Agent-declined simple tier (bloqueado/no_aplica, 00202).

        Drops the work branch without running gates or merging: the
        verdict was the agent's, not the orchestrator's. The failed
        ticket carries ``params.agent_result`` for the post-persist
        return; without an objective the verdict is just recorded.
        """
        if branch is not None:
            self._reset_and_drop_branch(branch)
        verdict = dict(code_result.agent_result or {})
        resultado = str(verdict.get("resultado") or "")
        motivo = str(verdict.get("motivo") or "")
        necesidad = str(verdict.get("necesidad") or "").strip()
        tier_label = tool.value if isinstance(tool, CodingTool) else str(tool)
        branch_note = "work branch dropped" if branch is not None \
            else "no work branch"
        evidence = (
            f"Agent {tier_label} returned {resultado} on "
            f"{self._simple_ref(task)}: {motivo}."
            f"{' Necesidad: ' + necesidad[:300] if necesidad else ''} "
            f"No gates ran; {branch_note}.")
        if isinstance(params_base, dict):
            params = dict(params_base)
        else:
            params = dict(task.get("params") or {}) \
                if isinstance(task.get("params"), dict) else {}
        params["agent_result"] = verdict
        if code_result.executor_link:
            params["executor_link"] = code_result.executor_link
        return DevExecutionOutput(
            status=TaskStatus.FAILED,
            coding_tool=tool if isinstance(tool, CodingTool) else None,
            error=evidence[:1500],
            last_response_summary=evidence,
            steps=[code_result.step,
                   self._step("agent_return", "failed", evidence)],
            params=params,
        )

    async def _material_simple_failure(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        branch: _SimpleBranch,
        cause: str,
        prior_steps: list[dict[str, Any]] | None = None,
        params_base: dict[str, Any] | None = None,
    ) -> DevExecutionOutput:
        """Material simple-tier failure (policy §29, replaces F5 escalation).

        Reset the candidate, preserve deterministic evidence, update the
        objective and invoke the planner exactly once. The failed ticket
        stays failed with its tier as history: no automatic staircase to
        another tier and no same-tier requeue of the unchanged ticket.
        Without an objective there is no planner to invoke: the ticket
        fails terminally with evidence, like a code-step error.
        ``prior_steps`` (oracle/gates rounds) lead the returned trail.
        """
        self._reset_and_drop_branch(branch)
        task_id = str(task.get("task_id") or "")
        tier_label = tool.value if isinstance(tool, CodingTool) else str(tool)
        evidence = (
            f"Material {tier_label} failure on {self._simple_ref(task)}: "
            f"{cause}. Candidate reset to {branch.base_sha[:12]}; "
            "no automatic retier.")
        # A business cancellation that landed mid-run wins over the
        # failure: failing the ticket and replanning would undo the
        # owner's decision, so the failure stays evidence on the
        # outcome/comment only, never a replan (TASK-2026-00177).
        cancelled = await self._task_cancelled(task_id)
        if cancelled:
            evidence += " Ticket cancelled in ERPNext; replan skipped."
        # Terminal first: the planner then sees a failed ticket rather than
        # an open duplicate, while the same-tier rule still blocks an
        # unchanged retry (record_material_failure feeds it).
        if self._repo is not None and task_id and not cancelled:
            try:
                await self._repo.patch_task(task_id, {"status": "failed"})
            except Exception:
                logger.warning("material-failure patch failed for %s",
                               task_id, exc_info=True)
        objective_id = str(task.get("objective_id") or "")
        if objective_id and not cancelled:
            evidence += await self._material_failure_replan(
                task, project_config, branch.repo, objective_id,
                tier_label, cause)
        if self._task_service is not None and task_id:
            try:
                await self._task_service.comment_on_ticket(task_id, evidence)
            except Exception:
                logger.warning("material-failure comment failed for %s",
                               task_id, exc_info=True)
        # ``params_base`` carries what this run already added (executor_link,
        # oracle_frozen); the funnel replaces params, so keep it whole.
        if isinstance(params_base, dict):
            params = dict(params_base)
        else:
            params = dict(task.get("params") or {}) \
                if isinstance(task.get("params"), dict) else {}
        if branch.oracle_frozen is not None:
            params["oracle_frozen"] = branch.oracle_frozen
        params["material_failure"] = {
            "tier": tier_label,
            "cause": cause[:500],
            "at": datetime.now(timezone.utc).isoformat(),
        }
        return DevExecutionOutput(
            status=TaskStatus.FAILED,
            coding_tool=tool if isinstance(tool, CodingTool) else None,
            error=evidence[:1500],
            last_response_summary=evidence,
            steps=[*(prior_steps or []),
                   self._step("material_failure", "failed", evidence)],
            params=params,
        )

    async def _material_failure_replan(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        repo: Path,
        objective_id: str,
        tier_label: str,
        cause: str,
    ) -> str:
        """Record the failure and invoke the planner exactly once (§29).

        Never raises: a replan breakdown is evidence on the failed ticket,
        not a second failure. The planning run counts against the
        objective anti-loop limits like any other run.
        """
        try:
            task_id = str(task.get("task_id", ""))

            async def notify(ref: str, text: str) -> None:
                del ref
                if self._task_service is not None and task_id:
                    try:
                        await self._task_service.comment_on_ticket(
                            task_id, text)
                    except Exception:
                        logger.warning(
                            "material-failure notify failed for %s",
                            task_id, exc_info=True)

            replan_provider: ObjectiveBadgeProvider | None = None
            replan_project = str(task.get("project") or "")
            replan_issuer = issuer_for_repo(self._repo)
            if replan_issuer is not None and task_id and replan_project:
                replan_provider = ObjectiveBadgeProvider(
                    replan_issuer, self._settings,
                    replan_project, ticket=task_id)
            env = self._objective_env_for(
                project_config, repo,
                objectives_process_executor(
                    project_config, badge_provider=replan_provider),
                self._objective_quota(), notify)
            await record_material_failure(
                env.objectives, objective_id, task_id=task_id,
                title=str(task.get("title", "")), tier=tier_label,
                cause=cause)
            result = await plan_objective(env, objective_id)
            if result.get("ok"):
                return (f" Planner replan: "
                        f"{result.get('tickets_created', 0)} ticket(s).")
            detail = str(result.get("detail") or "").strip()
            return (f" Planner replan parked "
                    f"({result.get('reason')}"
                    f"{': ' + detail[:200] if detail else ''}).")
        except Exception as exc:
            return f" Planner replan failed: {exc}"

    async def _record_tier_c_event(
        self, task: dict[str, Any], event: str, **kwargs: Any,
    ) -> None:
        """Best-effort Tier C telemetry (policy §32).

        Objective-bound C tickets only; never fails the execution.
        """
        if self._repo is None:
            return
        objective_id = str(task.get("objective_id") or "")
        if not objective_id:
            return
        try:
            params = task.get("params") \
                if isinstance(task.get("params"), dict) else {}
            oracle = (params or {}).get("oracle")
            oracle = oracle if isinstance(oracle, dict) else {}
            await record_tier_c(
                MongoObjectiveStore(self._repo), objective_id, event,
                task_id=str(task.get("task_id") or ""),
                oracle_type=str(oracle.get("type"))
                if oracle.get("type") else None,
                tier_reason=str((params or {}).get("tier_reason") or ""),
                work_type=str((params or {}).get("work_type") or ""),
                project=str(task.get("project") or ""),
                **kwargs)
        except Exception:
            logger.warning("tier_c telemetry failed for %s",
                           task.get("task_id"), exc_info=True)

    def _objective_quota(self) -> Any:
        return (RateMonitorQuota(self._rate_monitor)
                if self._rate_monitor is not None else AllowAllQuota())

    async def _run_tier_s(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        qa_cfg: dict[str, Any],
    ) -> DevExecutionOutput:
        """Tier S dispatch: full engine cycle with live CLI executors.

        The cycle patches the task directly (status/waiting/resume); this
        method mirrors its outcome into :class:`DevExecutionOutput` so the
        graph and ``run_task`` converge on the same values. No fallback to
        another tier, ever.
        """
        del qa_cfg
        task_id = str(task.get("task_id") or "")
        params = task.get("params") if isinstance(task.get("params"), dict) else None
        if self._settings.DEV_EXECUTION_MODE == "mock":
            summary = (
                f"MOCK tier_s execution completed for task {task_id}. "
                "No external coding CLI was executed."
            )
            return DevExecutionOutput(
                status=TaskStatus.COMPLETED,
                coding_tool=CodingTool.TIER_S,
                last_response_summary=summary,
                steps=[self._step("cycle", "ok", summary)],
                params=params,
            )
        repo_path = self._resolve_repo_path(project_config)
        if repo_path is None:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{CONFIG_INVALID_REASON}: tier_s needs a project repo",
                params=params,
            )
        dev_cfg = project_config.get("dev", {}) or {}
        cycle_cfg = TierSCycle.config_from_project(project_config)
        try:
            gate_specs = load_gates(dev_cfg)
        except ValueError as exc:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{CONFIG_INVALID_REASON}: gates: {exc}",
                params=params,
            )
        # Each engine gets its own executor (TASK-2026-00141): the cycle
        # runs its roles from cycle_cfg, the objective env resolves the
        # planner role and the review machinery from the global wiring.
        # No planner role is fused into the Tier S roles anymore.
        project = str(task.get("project") or "")
        badge_provider: TicketBadgeProvider | None = None
        cycle_issuer = issuer_for_repo(self._repo)
        if cycle_issuer is not None and task_id and project:
            badge_provider = TicketBadgeProvider(
                cycle_issuer, self._settings, "dev", task_id, project)
        executor = ProcessExecutor(
            roles=cycle_cfg["roles"], pool=cycle_cfg["reviewers"],
            badge_provider=badge_provider)
        quota: Any = self._objective_quota()
        notices: list[str] = []

        async def notify(ref: str, text: str) -> None:
            notices.append(text)
            if self._task_service is not None and task_id:
                try:
                    await self._task_service.comment_on_ticket(task_id, text)
                except Exception:
                    logger.warning("tier_s comment failed for %s", task_id,
                                   exc_info=True)

        repo = self._repo
        obj_provider: ObjectiveBadgeProvider | None = None
        obj_issuer = issuer_for_repo(repo)
        if obj_issuer is not None and task_id and project:
            obj_provider = ObjectiveBadgeProvider(
                obj_issuer, self._settings, project, ticket=task_id)
        obj_env = self._objective_env_for(
            project_config, Path(repo_path),
            objectives_process_executor(
                project_config, badge_provider=obj_provider),
            quota, notify)
        cycle = TierSCycle(
            MongoRunStore(repo) if repo is not None else InMemoryRunStore(),
            MongoLockStore(repo) if repo is not None else InMemoryLockStore(),
            MongoTaskStore(repo) if repo is not None else InMemoryTaskStore(),
            executor, quota=quota, notify=notify,
            slots=(MongoSlotStore(repo) if repo is not None
                   else InMemorySlotStore()),
            reviews=(MongoReviewStore(repo) if repo is not None
                     else InMemoryReviewStore()),
            inbox=(MongoInboxStore(repo) if repo is not None
                   else InMemoryInboxStore()),
            create_ticket=self._internal_create_ticket,
            objective_env=obj_env)
        outcome = await cycle.run(
            dict(task), Path(repo_path), cycle_cfg, gate_specs=gate_specs)
        summary = "; ".join(notices[-3:]) if notices else ""
        if outcome.status == "completed":
            converted = outcome.reason == "converted"
            detail = (f"converted to objective {outcome.detail}"
                      if converted
                      else f"closed {outcome.commit_sha[:12] if outcome.commit_sha else ''}")
            s_params = dict(params or {})
            s_params.pop("_objective_close", None)
            if not converted and task.get("objective_id"):
                # The cycle already notified the objective post-persist:
                # the funnel must not notify twice (TASK-2026-00169).
                s_params["_objective_close_notified"] = True
            else:
                s_params.pop("_objective_close_notified", None)
            return DevExecutionOutput(
                status=(TaskStatus.CONVERTED if converted
                        else TaskStatus.COMPLETED),
                coding_tool=CodingTool.TIER_S,
                last_response_summary=(
                    f"Tier S cycle {detail} (run {outcome.run_id}). {summary}"
                ).strip(),
                steps=[self._step("cycle", "ok", f"Tier S {detail}"),
                       *(self._step("notice", "ok", note) for note in notices[-5:])],
                params=s_params,
            )
        # Any non-completed cycle outcome mirrors as WAITING: the cycle
        # already patched the task itself (waiting/transient/needs_owner),
        # and this output must converge on those values, never override a
        # needs_owner wait with a terminal FAILED.
        return DevExecutionOutput(
            status=TaskStatus.WAITING,
            coding_tool=CodingTool.TIER_S,
            waiting_reason=(f"{outcome.reason}: {outcome.detail}"
                            if outcome.reason else "waiting"),
            resume_at=self._parse_resume_at(outcome.resume_at),
            last_response_summary=summary,
            steps=[self._step("cycle", "waiting",
                              f"{outcome.reason}: {outcome.detail}")],
            params=params,
        )

    @staticmethod
    def _parse_resume_at(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _objective_env_for(
        self,
        project_config: dict[str, Any],
        repo_path: Path,
        executor: Any,
        quota: Any,
        notify: Any,
    ) -> ObjectiveEnv:
        """Shared F6 env for Tier S cycles and simple-tier triggers."""
        dev_cfg = project_config.get("dev", {}) or {}
        repo = self._repo
        task_service = self._task_service
        repos = project_config.get("project", {}).get("repos", []) or []
        base = str((repos[0].get("branch_base") if repos else None) or "main")

        async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
            if repo is not None:
                await repo.patch_task(task_id, updates)
            revoke = getattr(task_service, "revoke_badges_for_patch", None)
            if revoke is not None:
                await revoke(task_id, str((updates or {}).get("status") or "") or None)

        wiring = objectives_wiring_config(project_config)
        objectives_cfg = dict(wiring["objectives_cfg"])
        objectives_cfg["child_autorun"] = bool(
            self._settings.TIER_S_OBJECTIVE_CHILD_AUTORUN)
        try:
            mandate = self._config_loader.load_company_mandate()
        except Exception:
            mandate = None

        return ObjectiveEnv(
            objectives=(MongoObjectiveStore(repo) if repo is not None
                        else InMemoryObjectiveStore()),
            tasks=MongoObjectiveTaskLister(repo) if repo is not None
            else InMemoryTaskLister(),
            create_ticket=self._internal_create_ticket,
            patch_task=patch_task,
            executor=executor,
            quota=quota,
            slots=(MongoSlotStore(repo) if repo is not None
                   else InMemorySlotStore()),
            reviews=(MongoReviewStore(repo) if repo is not None
                     else InMemoryReviewStore()),
            inbox=(MongoInboxStore(repo) if repo is not None
                   else InMemoryInboxStore()),
            notify=notify,
            repo=repo_path,
            roles=wiring["roles"],
            pool=list(parse_pool(wiring["pool"])),
            review_cfg=wiring["review_cfg"],
            objectives_cfg=objectives_cfg,
            planner_cfg=wiring["planner_cfg"],
            erp=(erp_for_task_service(self._task_service)
                 if self._task_service is not None else None),
            timeouts=wiring["timeouts"],
            fix_tiers=wiring["fix_tiers"],
            inbox_rule=wiring["inbox_rule"],
            auto_merge=bool(dev_cfg.get("auto_merge", True)),
            base_branch=base,
            vault=dict(dev_cfg.get("vault_project") or {})
            if dev_cfg.get("vault_project") else None,
            project_routing=project_money_routing(project_config),
            dispatcher_wake=(getattr(self._task_service, "wake_dispatcher",
                                     None)
                             if self._task_service is not None else None),
            mandate=mandate,
        )

    async def _internal_create_ticket(
        self, payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create inbox/objective tickets through the internal API (F4/F6).

        Single implementation in :mod:`objectives.service` (ERPNext first,
        idempotent per ``source_id``): dev and company-level flows share
        it, including the business hierarchy (``parent_task``/``depends_on``).
        None when unbound: promotions defer to telemetry instead of
        forking Mongo-only tickets.
        """
        return await create_internal_ticket(self._task_service, payload)

    def _ticket_badge_provider(
        self, task_id: str, project: str,
    ) -> TicketBadgeProvider | None:
        """Ticket badge provider, None when the manager is unbound."""
        issuer = issuer_for_repo(self._repo)
        if issuer is None:
            return None
        return TicketBadgeProvider(
            issuer, self._settings, "dev", task_id, project)

    async def _muse_role_overlays(
        self,
        provider: TicketBadgeProvider,
        role: str,
        workspace: str,
        profile_name: str,
    ) -> tuple[dict[str, bytes], str]:
        """Badge + settings overlay for one muse tier run (refuses loudly).

        The profile comes from the same config value as the argv card;
        tier runs are dev-only by design.
        """
        profile = load_role_profile(profile_name)
        if profile.agent != "dev":
            raise RoleProfileError(
                f"tier runs need the dev profile, not {profile_name!r}")
        badge = await provider.mint(role)
        try:
            rendered = settings_overlay_bytes(
                profile, Path(os.path.expanduser("~")), workspace,
                provider.mcp_servers(badge))
        except Exception:
            await provider.revoke(badge)
            raise
        return {SETTINGS_OVERLAY_RELPATH: rendered}, badge

    @staticmethod
    def _executor_link(executor: "TierExecutor") -> str | None:
        """Tier C chain link of ``executor`` (``muse``/``dsh``/``claudep``).

        None for tiers without a chain (A/B): only C degrades to another
        link, and that is what policy §25/§32 records per ticket.
        """
        label = str(executor.label or "")
        return label.split(":", 1)[1] if label.startswith("tier_c:") else None

    async def _step_code(
        self,
        task: dict[str, Any],
        config: dict[str, Any],
        tool: CodingTool,
    ) -> "CodingStepResult":
        prompt = self._build_coding_prompt(task, config)

        if self._settings.DEV_EXECUTION_MODE == "mock":
            summary = (
                f"MOCK {tool.value} execution completed for task {task.get('task_id')}. "
                "No external coding CLI was executed."
            )
            return CodingStepResult(
                step=self._step("code", "ok", summary),
                prompt=prompt,
                summary=summary,
            )

        executor = await self._resolve_tier_executor(tool, config)
        if executor.prompt_via == "argv":
            command = [*executor.argv, prompt]
            stdin_payload: str | None = None
        else:
            command = list(executor.argv)
            stdin_payload = prompt
        overlays: dict[str, bytes] | None = None
        badge: str | None = None
        badge_provider: TicketBadgeProvider | None = None
        if executor.label == "tier_c:muse":
            task_id = str(task.get("task_id") or "")
            project = str(task.get("project") or "")
            badge_provider = self._ticket_badge_provider(task_id, project)
            if badge_provider is None:
                detail = "role badge needs a bound repository"
                return CodingStepResult(
                    step=self._step("code", "failed", detail),
                    prompt=prompt,
                    summary="",
                    error=detail,
                    executor_link=self._executor_link(executor),
                )
            try:
                overlays, badge = await self._muse_role_overlays(
                    badge_provider, str(executor.label),
                    executor.cwd or os.getcwd(),
                    executor.role_profile or "dev")
            except (RoleProfileError, CatalogError, McpAuthError,
                    ValueError, OSError) as exc:
                detail = f"role profile refused: {exc}"
                return CodingStepResult(
                    step=self._step("code", "failed", detail),
                    prompt=prompt,
                    summary="",
                    error=detail,
                    executor_link=self._executor_link(executor),
                )
        try:
            ok, output, err = await self._run_process(
                command, stdin_payload=stdin_payload, timeout=1800,
                cwd=executor.cwd, env=executor.env or None,
                config_argv=list(executor.argv),
                home_file_overlays=overlays,
            )
        finally:
            if badge is not None and badge_provider is not None:
                await badge_provider.revoke(badge)

        if not ok:
            detail = self._summarize_failure_output(
                output, err, fallback=f"{executor.label} execution failed")
            return CodingStepResult(
                step=self._step("code", "failed", detail),
                prompt=prompt,
                summary="",
                error=detail,
                executor_link=self._executor_link(executor),
            )

        text = output or "ok"
        review_links = extract_review_links(text, self._review_base_url(config))
        summary = self._truncate(text, 500)
        verdict = parse_agent_result(output)
        return CodingStepResult(
            step=self._step("code", "ok", f"Coding CLI finished ({executor.label})"),
            prompt=prompt,
            summary=summary,
            executor_link=self._executor_link(executor),
            review_links=review_links,
            agent_result=(verdict.model_dump(mode="json")
                          if verdict is not None else None),
        )

    async def _resolve_tier_executor(
        self,
        tool: CodingTool,
        config: dict[str, Any],
    ) -> TierExecutor:
        """Resolve a tier to its one-shot executor (design v2 §3.1, F0.1).

        Tier A runs the configured Claude account, Tier B the configured
        Codex account, Tier C the first available link of its chain
        (muse → dsh → claudep). Model/effort stay wrapper-owned until F5
        enforces them explicitly (§4.4); the account always comes from the
        global ``tiers`` config, never from the ticket.
        """
        dev_cfg = config.get("dev", {}) or {}
        tiers_cfg = dev_cfg.get("tiers", {}) or {}
        providers = dev_cfg.get("providers", {}) or {}
        repo_cwd = self._resolve_repo_path(config)

        if tool == CodingTool.TIER_A:
            tier_cfg = tiers_cfg.get("tier_a") or {}
            account = str(tier_cfg.get("account") or "claudej")
            provider_cfg = providers.get("claude", {}) or {}
            argv = self._as_argv(provider_cfg.get("command")) or [account]
            return TierExecutor(
                argv=self._ensure_print_flag(argv) + self._model_args(tier_cfg),
                prompt_via="argv",
                cwd=self._provider_cwd(provider_cfg, repo_cwd),
                env=self._provider_env(provider_cfg),
                label="tier_a",
            )
        if tool == CodingTool.TIER_B:
            tier_cfg = tiers_cfg.get("tier_b") or {}
            account = str(tier_cfg.get("account") or "codexj")
            provider_cfg = providers.get("codex", {}) or {}
            return TierExecutor(
                argv=self._ensure_codex_exec(self._with_model_args(
                    [account, "exec", "--ephemeral", "-"],
                    self._model_args(tier_cfg))),
                prompt_via="stdin",
                cwd=self._provider_cwd(provider_cfg, repo_cwd),
                env=self._provider_env(provider_cfg),
                label="tier_b",
            )
        if tool == CodingTool.TIER_C:
            tier_c = tiers_cfg.get("tier_c", {}) or {}
            chain = [str(link) for link in tier_c.get("chain", TIER_C_CHAIN) if str(link)]
            availability = await self._tier_c_availability(chain, tier_c)
            link = select_tier_c_link(availability, chain)
            link_cfg = tier_c.get(link, {}) or {}
            if link == "muse":
                provider_cfg = providers.get("muse", {}) or {}
                argv = self._as_argv(link_cfg.get("command")) or self._as_argv(
                    provider_cfg.get("command")) or ["musej", "exec"]
                argv = self._with_output_schema(
                    argv + self._model_args(link_cfg))
                profile_name = (link_cfg.get("role_profile")
                                or provider_cfg.get("role_profile"))
                if profile_name:
                    profile = load_role_profile(str(profile_name))
                    argv = [*argv, *render_argv_extras(profile),
                            *render_card_argv(profile)]
                return TierExecutor(
                    argv=argv,
                    prompt_via="argv",
                    cwd=self._provider_cwd(provider_cfg, repo_cwd),
                    env=self._provider_env(provider_cfg),
                    label="tier_c:muse",
                    role_profile=(str(profile_name)
                                  if profile_name else None),
                )
            argv = self._as_argv(link_cfg.get("command")) or [link]
            if link == "claudep":
                argv = self._ensure_print_flag(argv)
            return TierExecutor(
                argv=self._with_model_args(
                    argv, self._model_args(link_cfg)),
                prompt_via="stdin" if link == "dsh" else "argv",
                cwd=repo_cwd,
                env={str(k): str(v) for k, v in dict(link_cfg.get("env") or {}).items()},
                label=f"tier_c:{link}",
            )
        raise ValueError(f"no simple executor for tier {tool!r}")

    @staticmethod
    def _model_args(tier_cfg: dict[str, Any]) -> list[str]:
        """Explicit model/effort argv from tier config (design §4.4, F5).

        Empty when unconfigured: legacy shapes stay byte-identical and the
        model remains wrapper-owned until the global config declares it.
        """
        raw = (tier_cfg or {}).get("model_args") or []
        return [str(token) for token in raw] if isinstance(raw, list) else []

    @staticmethod
    def _with_model_args(argv: list[str], model_args: list[str]) -> list[str]:
        """Splice model args before a trailing stdin ``-`` (codex/dsh)."""
        if not model_args:
            return list(argv)
        if argv and argv[-1] == "-":
            return [*argv[:-1], *model_args, "-"]
        return [*argv, *model_args]

    async def _tier_c_availability(
        self,
        chain: list[str],
        tier_c: dict[str, Any],
    ) -> dict[str, bool]:
        """Probe each Tier C link: muse via its quota monitor, the reactive
        links by command existence (tried until they fail, §7.2)."""
        availability: dict[str, bool] = {}
        for link in chain:
            if link == "muse":
                availability[link] = await self._muse_available()
                continue
            link_cfg = tier_c.get(link, {}) or {}
            argv = self._as_argv(link_cfg.get("command"))
            availability[link] = self._command_available(argv or [link])
        return availability

    async def _muse_available(self) -> bool:
        """Is the Tier C primary link (muse) launchable?

        Honors the Muse reserve on purpose (policy §28): Muse also carries
        the Tier S builder/closer, the objective planner and the CEO
        review, so below its reserve the same C ticket runs on the next
        chain link instead. Tier S itself consumes into the reserve.
        """
        if self._rate_monitor is None:
            return True
        try:
            return await self._rate_monitor.can_launch("muse")
        except Exception:
            logger.warning("muse availability probe failed; assuming available", exc_info=True)
            return True

    @staticmethod
    def _command_available(argv: list[str]) -> bool:
        if not argv:
            return False
        if shutil.which(argv[0]) is None:
            return False
        return all(Path(token).exists() for token in argv[1:]
                   if token.startswith("/") and "." in Path(token).name)

    @staticmethod
    def _as_argv(raw: Any) -> list[str]:
        if isinstance(raw, list) and raw:
            return [str(token) for token in raw]
        if isinstance(raw, str) and raw.strip():
            return [raw.strip()]
        return []

    @staticmethod
    def _ensure_print_flag(argv: list[str]) -> list[str]:
        if any(token in ("-p", "--print") for token in argv):
            return list(argv)
        return [*argv, "--print"]

    @staticmethod
    def _with_output_schema(argv: list[str]) -> list[str]:
        """Shape the muse answer with the agent-result schema (00202).

        Only muse-family headless commands take ``--output-schema``;
        anything else passes through unchanged. Idempotent. The flag
        needs the meta provider (production default); echo runs reject
        it loudly, so manual ``--provider echo`` probes must drop it.
        """
        if not argv or "--output-schema" in argv:
            return list(argv)
        if Path(argv[0]).name not in {"muse", "musej"}:
            return list(argv)
        if "exec" not in argv[1:]:
            return list(argv)
        return [*argv, "--output-schema", str(AGENT_RESULT_SCHEMA_PATH)]

    @staticmethod
    def _ensure_codex_exec(argv: list[str]) -> list[str]:
        """Headless guard for codex one-shot (TASK-2026-00157).

        The codex TUI refuses a piped stdin with rc=1
        ("Error: stdin is not a terminal"); the non-interactive form is
        ``codex exec`` (root cause TASK-2026-00153). Insert ``exec``
        after the binary when the argv names a codex* binary without
        it. Idempotent; non-codex argv pass through unchanged.
        """
        if not argv or "exec" in argv[1:]:
            return list(argv)
        if not Path(argv[0]).name.lower().startswith("codex"):
            return list(argv)
        return [argv[0], "exec", *argv[1:]]

    @staticmethod
    def _provider_cwd(provider_cfg: dict[str, Any], repo_cwd: str | None) -> str | None:
        cwd = provider_cfg.get("cwd")
        if cwd is not None:
            return str(cwd)
        return repo_cwd

    @staticmethod
    def _provider_env(provider_cfg: dict[str, Any]) -> dict[str, str]:
        return {str(k): str(v) for k, v in dict(provider_cfg.get("env") or {}).items()}

    def _build_coding_prompt(self, task: dict[str, Any], config: dict[str, Any]) -> str:
        pm = (task.get("params") or {}).get("pm_ticket") or {}
        ticket_num = task.get("ticket_number")
        review_base_url = self._review_base_url(config)

        lines: list[str] = ["TAREA"]

        if ticket_num is not None:
            lines.append(f"Ticket: #{ticket_num}")
        lines.append(f"Título: {task.get('title')}")
        lines.append(f"Descripción: {task.get('description')}")

        answer = (task.get("params") or {}).get("owner_answer") or {}
        if isinstance(answer, dict):
            comment = str(answer.get("comment") or "").strip()
            decision = str(answer.get("decision") or "").strip()
            if comment or decision:
                # H1 (revisión 00206): el trabajo reanudado debe saber qué
                # decidió el Owner; sin esto planea a ciegas. Se sobrescribe
                # con cada respuesta nueva, así que nunca acumula.
                lines.append("")
                lines.append(
                    "Respuesta del dueño a la pregunta que bloqueó este "
                    f"ticket ({decision or 'sin decisión'}): "
                    f"{comment or '(sin texto)'}"
                )
                lines.append("Úsala como decisión vinculante para este trabajo.")

        module = pm.get("module")
        if module:
            lines.append(f"Módulo: {module}")

        ticket_type = pm.get("type")
        if ticket_type:
            lines.append(f"Tipo: {ticket_type}")

        acceptance = pm.get("acceptance_criteria")
        if acceptance:
            lines.append("")
            lines.append("Criterios de aceptación:")
            for idx, criterion in enumerate(acceptance, 1):
                lines.append(f"  {idx}. {criterion}")

        attachments = task.get("attachments")
        if attachments:
            lines.append("")
            lines.append(f"Adjuntos: {json.dumps(attachments)}")

        lines.extend(
            [
                "",
                "REGLAS DE EJECUCIÓN (Tier S v2, F5)",
                "Do NOT commit, push, rebase, stash, or change branches: leave your work uncommitted; the orchestrator integrates it.",
                "Write .tier-s/HANDOFF.md (§16.6: HANDOFF <ref>, Contracts introduced/changed, Behavior implemented, Deviations from plan, Discovered work, Gotchas; executor sections ≤300 words).",
                "",
                "FORMATO FINAL OBLIGATORIO",
                "Al final de tu respuesta incluye exactamente un bloque REVIEW_LINKS con los links directos para QA/cliente de las vistas afectadas.",
                "Si hay varias vistas afectadas, escribe un link por línea dentro del bloque.",
                "Si el ticket toca vistas, router, menú, componentes visuales o renderizado, cumple también el smoke test/browser exigido por AGENTS.md.",
            ]
        )
        if review_base_url:
            lines.append(f"Usa URLs completas bajo: {review_base_url}")
        else:
            lines.append("Si no conoces el dominio, usa rutas directas tipo /dashboard/...")
        lines.extend(
            [
                "Si no hay vistas involucradas, escribe NONE dentro del bloque.",
                "",
                "REVIEW_LINKS_START",
                (
                    f"{review_base_url.rstrip('/')}/dashboard/ejemplo"
                    if review_base_url
                    else "/dashboard/ejemplo"
                ),
                "REVIEW_LINKS_END",
                "",
                "o",
                "",
                "REVIEW_LINKS_START",
                "NONE",
                "REVIEW_LINKS_END",
            ]
        )
        lines.extend(["", AGENT_RESULT_PROMPT])

        return with_preamble(
            "\n".join(lines), self._resolve_repo_path(config), "es")

    def _resolve_repo_path(self, config: dict[str, Any]) -> str | None:
        repos = config.get("project", {}).get("repos", [])
        if not repos:
            return None
        first = repos[0]
        local_path = first.get("local_path") or first.get("path")
        if not local_path:
            return None
        path = Path(str(local_path))
        return str(path) if path.exists() else None

    def _build_session_title(self, task: dict[str, Any]) -> str:
        parts: list[str] = []
        project = str(task.get("project") or "").strip()
        if project:
            parts.append(project)

        ticket_number = task.get("ticket_number")
        if ticket_number is not None:
            parts.append(f"#{ticket_number}")

        title = str(task.get("title") or "").strip() or "Task"
        parts.append(title)
        session_title = " | ".join(parts)
        return self._truncate(" ".join(session_title.split()), 180)

    @staticmethod
    def _review_base_url(config: dict[str, Any]) -> str | None:
        raw_value = str(config.get("dev", {}).get("review_base_url") or "").strip()
        return raw_value.rstrip("/") if raw_value else None

    async def _run_command(
        self,
        command: list[str],
        stdin_payload: str | None = None,
        timeout: int = 120,
    ) -> tuple[bool, str]:
        ok, output, err = await self._run_process(
            command,
            stdin_payload=stdin_payload,
            timeout=timeout,
        )
        if not ok:
            return False, self._truncate(err or output or "Unknown command error", 600)

        return True, self._truncate(output or "ok", 600)

    async def _run_process(
        self,
        command: list[str],
        stdin_payload: str | None = None,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sandbox: bool = True,
        config_argv: list[str] | None = None,
        home_file_overlays: dict[str, bytes] | None = None,
    ) -> tuple[bool, str, str]:
        result = await self._run_process_detailed(
            command,
            stdin_payload=stdin_payload,
            timeout=timeout,
            cwd=cwd,
            env=env,
            sandbox=sandbox,
            config_argv=config_argv,
            home_file_overlays=home_file_overlays,
        )
        return result.ok, result.stdout, result.stderr

    async def _run_process_detailed(
        self,
        command: list[str],
        stdin_payload: str | None = None,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        sandbox: bool = True,
        config_argv: list[str] | None = None,
        home_file_overlays: dict[str, bytes] | None = None,
    ) -> "ProcessRunResult":
        plan = None
        if sandbox:
            from orchestration.agent_sandbox import (
                cleanup_sandbox_scratch, plan_agent,
            )
            from research.runtimes.base import IsolationUnavailable
            try:
                plan = plan_agent(
                    argv=[str(token) for token in command],
                    workspace=cwd or os.getcwd(),
                    base=os.environ, overlay=env,
                    config_args=(config_argv if config_argv is not None
                                 else [str(command[0])] if command else []),
                    home_file_overlays=home_file_overlays)
            except (IsolationUnavailable, ValueError, OSError) as exc:
                return ProcessRunResult(
                    False, "", f"sandbox refused: {exc}", None)
        try:
            stdin_target = (
                asyncio.subprocess.PIPE
                if stdin_payload is not None
                else asyncio.subprocess.DEVNULL
            )
            if plan is None:
                run_argv: list[str] = command
                run_cwd: str | None = cwd
                run_env: dict[str, str] | None = child_env(os.environ, env)
            else:
                run_argv = plan.argv
                run_cwd = plan.cwd
                run_env = plan.env
            process = await asyncio.create_subprocess_exec(
                *run_argv,
                stdin=stdin_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_cwd,
                env=run_env,
            )
            input_bytes = None if stdin_payload is None else stdin_payload.encode("utf-8")
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return ProcessRunResult(False, "", f"Command timeout: {' '.join(command)}", None)
        except FileNotFoundError:
            return ProcessRunResult(False, "", f"Command not found: {command[0]}", None)
        except Exception as exc:
            return ProcessRunResult(False, "", f"Command execution error: {exc}", None)
        finally:
            if plan is not None:
                cleanup_sandbox_scratch(cwd or os.getcwd())

        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            return ProcessRunResult(False, output, err, process.returncode)

        return ProcessRunResult(True, output or "ok", err, process.returncode)

    @staticmethod
    def _truncate(value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[: max_len - 3] + "..."

    @classmethod
    def _summarize_failure_output(
        cls,
        stdout: str,
        stderr: str,
        *,
        fallback: str,
        max_len: int = 600,
    ) -> str:
        for candidate in (stderr, stdout, fallback):
            text = (candidate or "").strip()
            if not text:
                continue
            if len(text) <= max_len:
                return text
            return "..." + text[-(max_len - 3):]
        return fallback

    @staticmethod
    def _step(step: str, status: str, detail: str, result: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step": step,
            "status": status,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc),
        }
        if result is not None:
            payload["result"] = result
        return payload


@dataclass
class CodingStepResult:
    step: dict[str, Any]
    prompt: str
    summary: str
    error: str | None = None
    session_id: str | None = None
    session_title: str | None = None
    waiting_reason: str | None = None
    resume_at: datetime | None = None
    review_links: ReviewLinksExtraction = field(default_factory=lambda: ReviewLinksExtraction([], False))
    #: Tier C chain link that ran the step (policy §25/§32), None otherwise.
    executor_link: str | None = None
    #: Parsed agent verdict (hecho/bloqueado/no_aplica), None when absent.
    agent_result: dict[str, Any] | None = None


@dataclass
class ProcessRunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
