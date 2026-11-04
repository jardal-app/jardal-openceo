from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .builder_contract import (
    blocked_continuation_due,
    blocked_continuation_prompt,
    builder_environment_prompt,
    kept_work_note,
)
from .review_links import ReviewLinksExtraction, apply_review_links_to_params, extract_review_links
from .review_policy import (REVIEW_OFF, resolve_review, review_wait_reason,
                            stamp_effective_review)
from .routing import (ROUTING_MISSING_REASON, TIER_C_CHAIN, RoutingDecision,
                      resolve_coding_tool, select_tier_c_link)
from .production_deploy import normalize_deploy, run_production_step
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
from .tier_s.plan import is_test_path
from catalog import CatalogError, load_role_profile
from mcp_identity import McpAuthError
from objectives.money_guide import project_money_routing
from objectives.pedido import is_explicit_dev
from objectives.ticket_cost import COST_RUNS_KEY
from objectives import ticket_cost as cost_mod
from orchestration.agent_sandbox import project_isolation
from orchestration.inbox import InMemoryInboxStore, MongoInboxStore
from orchestration.knowledge import detect_prompt_lang, with_preamble
from orchestration.knowledge_proposals import (
    knowledge_target_prefix,
    proposal_prompt_for,
)
from orchestration.live import (TIMEOUT_RETURNCODE, AllowAllQuota,
                                ProcessExecutor, RateMonitorQuota, _reviewer_output)
from orchestration.provider_recovery import (
    PROVIDER_RESUME_PROMPT,
    PROVIDER_RETRY_DELAYS,
    model_responded,
    transient_provider_error,
)
from orchestration.role_claude import (
    CLAUDE_MCP_OVERLAY_RELPATH,
    claude_config_bytes,
    claude_no_auto_memory_env,
    ensure_mcp_placeholder,
    profile_box_tools,
)
from orchestration.role_claude import (
    render_argv_extras as claude_argv_extras,
)
from orchestration.role_muse import (
    SETTINGS_OVERLAY_RELPATH,
    ObjectiveBadgeProvider,
    RoleProfileError,
    TicketBadgeProvider,
    badge_ticket_for_task,
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
from model_routing import review_wiring as wiring_mod
from orchestration import findings as findings_mod
from orchestration import review as review_mod
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
    SCHEDULED_WAIT_COUNT_KEY,
    SCHEDULED_WAIT_MAX_DECLINES,
    AgentResult,
    SCHEMA_PATH as AGENT_RESULT_SCHEMA_PATH,
    parse_agent_result,
    scheduled_resume,
    scheduled_wait_declines,
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
    cost_runs: list[dict[str, Any]] = field(default_factory=list)

    def as_state_patch(self) -> dict[str, Any]:
        patch: dict[str, Any] = {
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
        if self.cost_runs:
            patch[COST_RUNS_KEY] = self.cost_runs
        return patch


#: Env var carrying the run's MCP ticket token on Tier B (TASK-2026-00200).
#: Codex reads it via a per-run bearer_token_env_var override (the PM
#: extraction precedent); never persisted, never on argv.
TIER_CODEX_TOKEN_ENV = "OPENCEO_TIER_MCP_TOKEN"


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
    #: ``False`` when the project sets ``dev.isolation: host``: the builder
    #: runs with the host toolchain instead of the bwrap fence.
    sandbox: bool = True


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
    #: The checkpoint was taken after the builder finished (TASK-2026-00481):
    #: a resume goes straight to gates/review instead of paying another build.
    build_done: bool = False


#: Provenance marker for the explicit-dev tier default (TASK-2026-00204).
EXPLICIT_DEFAULT_BY = "explicit_default/v1"

#: Params key carrying the simple-tier review checkpoint (00251).
REVIEW_ROUND_PARAMS_KEY = "review_round"

#: The simple round launches three reviewers with these families first;
#: alternatives still fill in when preferred slots are unavailable.
SIMPLE_REVIEW_PREFER_FAMILIES = ("gemini",)

#: One round, three candidates, quorum of two independent families.
SIMPLE_REVIEW_COUNT = 3
SIMPLE_REVIEW_QUORUM = 2

#: No-quorum waits before the ticket parks for the owner (00251).
SIMPLE_REVIEW_MAX_NO_QUORUM_WAITS = 3

#: Builder fix turns before unaddressed findings park for the owner.
SIMPLE_REVIEW_MAX_FIX_TURNS = 2

#: Effective builder family per simple tier/link (self-review rule).
#: Tier C links resolve through the recorded executor link; unknown
#: links default to the primary, like the historical default.
_SIMPLE_BUILDER_FAMILIES = {"tier_a": "claude", "tier_b": "codex"}
_SIMPLE_C_LINK_FAMILIES = {"muse": "muse", "dsh": "deepseek",
                           "claudep": "deepseek"}


def _simple_builder_family(tool: CodingTool,
                           params: dict[str, Any] | None) -> str:
    """Family of the effective simple-tier builder (00251)."""
    if tool == CodingTool.TIER_C:
        link = str((params or {}).get("executor_link") or "").strip()
        return _SIMPLE_C_LINK_FAMILIES.get(link, "muse")
    return _SIMPLE_BUILDER_FAMILIES.get(tool.value, "muse")


def _read_review_checkpoint(
        params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validated simple-round checkpoint, or None when absent/malformed."""
    if not isinstance(params, dict):
        return None
    checkpoint = params.get(REVIEW_ROUND_PARAMS_KEY)
    if not isinstance(checkpoint, dict):
        return None
    if checkpoint.get("phase") not in ("round", "reviewed", "done",
                                       "skipped"):
        return None
    return dict(checkpoint)


def _rehydrate_findings(records: list[Any]) -> list[Any]:
    """Finding objects back from checkpoint records (resume path)."""
    findings: list[Any] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        try:
            anchors = [findings_mod.Anchor(
                raw=str(item.get("raw") or ""), path=item.get("path"),
                line=item.get("line"), base=bool(item.get("base")),
                criterion=item.get("criterion"))
                for item in record.get("anchors") or []]
            findings.append(findings_mod.Finding(
                id=int(record.get("id") or 0),
                severity=str(record.get("severity") or ""),
                category=str(record.get("category") or ""),
                anchors=anchors, problem=str(record.get("problem") or ""),
                fix=str(record.get("fix") or ""),
                strength=str(record.get("strength") or ""),
                families=list(record.get("families") or [])))
        except (TypeError, ValueError):
            continue
    return [item for item in findings if item.id > 0]


def _task_with_default_tier(task: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``task`` carrying the explicit-dev C default in ``params``."""
    params = dict(task.get("params") or {})
    params["tier_reason"] = (
        "explicit dev without a tier runs the safe C default (§25)")
    params["tier_assigned_by"] = EXPLICIT_DEFAULT_BY
    return {**task, "params": params}


def _merge_session_params(
    enriched: dict[str, Any] | None, downstream: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Reattach session-assigned keys dropped by the execution path.

    Downstream keys win on conflict; keys only differ in practice for
    ``tier_reason``-style session outputs, which downstream never
    writes.
    """
    if not enriched:
        return downstream
    if downstream is None:
        return dict(enriched)
    merged = dict(enriched)
    merged.update(downstream)
    return merged


def _dev_vault_roots(
    project_config: Mapping[str, Any] | None,
) -> list[str]:
    """Active-project vault folder for Dev read-only mounts (00328)."""
    from orchestration.agent_sandbox import vault_project_read_root
    return vault_project_read_root(project_config)


def _dev_vault_index(
    project_config: Mapping[str, Any] | None,
) -> str:
    """Bounded hub + practices index for Dev prompts (00329, D6).

    Never fails: without a vault the prompt runs exactly as before.
    """
    from orchestration.vault_index import build_vault_index
    try:
        return build_vault_index(project_config)
    except Exception:
        logger.warning("vault index unavailable; prompt runs without it",
                       exc_info=True)
        return ""


def _tier_s_roles_with_vault(
    roles: Mapping[str, Any],
    project_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Tier S cycle roles carrying the project vault read-only (00328).

    Each role spec keeps its own ``extra_read_roots`` with the active
    project vault folder appended; the reviewer pool is untouched
    (reviewers are not Dev). Projects without ``vault_project`` keep
    their specs unchanged.
    """
    roots = _dev_vault_roots(project_config)
    if not roots:
        return dict(roles)
    mounted: dict[str, Any] = {}
    for name, spec in dict(roles).items():
        if not isinstance(spec, Mapping):
            mounted[str(name)] = spec
            continue
        extra = list(spec.get("extra_read_roots") or [])
        seen = {str(entry) for entry in extra}
        extra.extend(root for root in roots if root not in seen)
        mounted[str(name)] = {**spec, "extra_read_roots": extra}
    return mounted


class DevSessionManager:
    """Runs deterministic step-based Dev Agent sessions."""

    def __init__(self, settings: Settings, config_loader: ConfigLoader,
                 rate_monitor: RateMonitor | None = None, repo: Any | None = None) -> None:
        self._settings = settings
        self._provider_responses: set[tuple[str, ...]] = set()
        self._config_loader = config_loader
        self._rate_monitor = rate_monitor
        self._repo = repo
        self._task_service: Any | None = None
        # Routes gate (TASK-2026-00389, §9.4): ERP adapter for protected
        # diff approvals (request + live read). None keeps the gate off
        # the network; the activation child wires the production adapter.
        self._routes_gate_adapter: Any | None = None

    def set_routes_gate_adapter(self, adapter: Any | None) -> None:
        """Inject the ERP adapter for protected-diff approvals.

        ``None`` (default) keeps the gate off the network: an enabled
        gate with no adapter blocks protected diffs with their human
        action. The activation (TASK-2026-00391) wires the production
        adapter here. Untouched diffs never need it.
        """
        self._routes_gate_adapter = adapter

    async def _protected_routes_check(
            self, branch: _SimpleBranch, files: list[str],
            head_sha: str, ref: str) -> str | None:
        """Protected-routes gate (§9.4): None allows, text waits with detail.

        Off while the calibration child owns activation; untouched diffs
        pass without any ERP read. A protected diff requests its approval
        (idempotent on the diff hash) and verifies it live; anything
        unverifiable blocks fail-closed with the exact human action.
        """
        import logging as _logging

        try:
            from reliability import protected_routes as routes_mod
        except Exception:  # noqa: BLE001 - logged, gate skipped
            routes_mod = None  # type: ignore[assignment]
        if routes_mod is None:
            _logging.getLogger(__name__).warning(
                "routes gate module unreadable, gate skipped")
            return None
        try:
            if not routes_mod.routes_gate_enabled():
                return None
            touched = routes_mod.touched_protected(files)
            if not touched:
                return None
            _, diff = await asyncio.to_thread(
                routes_mod.collect_diff,
                str(branch.repo), branch.base_sha, head_sha)
            digest = routes_mod.diff_digest(files, diff)
            created = await routes_mod.request_diff_approval(
                self._routes_gate_adapter, digest, touched,
                business_ref=ref)
            approval_id = str((created or {}).get("id") or "").strip()
            verdict = await routes_mod.check_integration(
                files, diff, approval_id=approval_id or None,
                approvals=self._routes_gate_adapter)
        except Exception:  # noqa: BLE001 - logged, merge withheld
            _logging.getLogger(__name__).warning(
                "routes gate check failed (fail-closed)", exc_info=True)
            return "protected_routes: gate error, merge withheld"
        if verdict.get("allowed"):
            return None
        detail = str(verdict.get("human_action") or "protected diff")
        if approval_id:
            detail += f" (approval {approval_id} pending)"
        return f"protected_routes: {detail}"

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
        defaulted = False
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
            if not is_explicit_dev(task):
                # Undecided loose work never reaches dev: the run
                # pre-stage plans every pedido and the graph waits
                # routing_missing without a hint (TASK-2026-00204).
                # Fail closed.
                return DevExecutionOutput(
                    status=TaskStatus.WAITING,
                    waiting_reason=f"{ROUTING_MISSING_REASON}: {decision.detail}",
                    params=task.get("params") if isinstance(task.get("params"), dict) else None,
                )
            # Explicit dev without a tier runs the safe C default
            # (policy §25): specified work with no S/A/B justification.
            # Only the planner ever assigns S/A/B or picks agents; this
            # default assigns neither.
            logger.info("explicit dev task_id=%s runs default tier_c",
                        task.get("task_id"))
            task = _task_with_default_tier(task)
            defaulted = True
            decision = RoutingDecision(
                CodingTool.TIER_C, "explicit_default",
                "explicit_default=tier_c",
            )

        # Invalid project config parks every ticket, whatever the tier.
        config_errors = self._config_loader.validate_project(task.get("project"))
        if config_errors:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{CONFIG_INVALID_REASON}: {'; '.join(config_errors)}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )

        # Per-ticket review switch (00249): resolve before any lock or
        # execution. Invalid input and S+off wait with a typed reason;
        # the admitted mode freezes into params on the way out.
        review_decision = resolve_review(task)
        review_hold = review_wait_reason(
            review_decision,
            decision.tool.value if decision.tool is not None else None)
        if review_hold is not None:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=review_hold,
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
                    params=stamp_effective_review(
                        task.get("params"), review_decision),
                )
        deadline_armed = await self._deadline_mark_start(task, decision.tool)
        try:
            if decision.tool == CodingTool.TIER_S:
                output = await self._run_tier_s(task, project_config, qa_cfg)
            else:
                output = await self._run_simple_tool(task, project_config, decision.tool, qa_cfg)
        except Exception as exc:
            if decision.tool is not None and decision.tool is not CodingTool.TIER_S:
                await self._capture_simple_exception(task, decision.tool, exc)
            if outer_lock:
                assert repo is not None
                await self._repo.release_repo_lock(repo, holder)
            raise
        finally:
            # The attempt ended on every path (success, early return,
            # exception): clear its deadline so the watch stops tracking
            # it. The resume side, if any, is tracked via resume_at.
            if deadline_armed:
                await self._deadline_mark_end(holder)
        output.params = stamp_effective_review(
            output.params if isinstance(output.params, dict)
            else task.get("params"),
            review_decision)
        if outer_lock:
            assert repo is not None
            if output.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED):
                await self._repo.release_repo_lock(repo, holder)
            elif output.status == TaskStatus.WAITING and output.resume_at is not None:
                if str(output.waiting_reason or "").startswith("scheduled_wait:"):
                    await self._repo.release_repo_lock(repo, holder)
                else:
                    await self._repo.touch_repo_lock(repo, holder)
            elif output.status == TaskStatus.WAITING:
                # Human wait (needs_owner, no resume): the session never comes
                # back by itself (TASK-2026-00122); holding the lock would wedge
                # the repo for 24h (live: E1 diagnostic blocked E2 review).
                await self._repo.release_repo_lock(repo, holder)
        if defaulted:
            # The simple-tier path drops input params when no review links
            # change; reattach the default assignment so it persists.
            output.params = _merge_session_params(
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
        await self._note_exposure(task, "dev_session")
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
            if isinstance(task.get("params"), dict) \
                    and "baseline_retries" in task["params"]:
                task["params"] = {key: value for key, value
                                  in task["params"].items()
                                  if key != "baseline_retries"}
            if not isinstance((task.get("params") or {}).get(
                    "dev_checkpoint"), dict):
                await self._note_exposure(task, "gates")

        if tool == CodingTool.TIER_C:
            await self._record_tier_c_event(task, "started")
        if branch is not None and branch.build_done:
            # A wait that resumed (review quorum, review-pool/router or gates
            # config) left a checkpoint taken after the build (TASK-2026-00481):
            # the paid work is committed, so go straight to gates/review.
            output.steps.append(self._step(
                "code", "ok",
                "resumed from checkpoint: builder work already committed; "
                "skipping the build turn"))
            resumed_params = dict(task.get("params") or {})
            resumed_params.pop("dev_checkpoint", None)
            output.params = resumed_params
            return await self._reliability_finish(
                task, tool, branch,
                await self._finish_simple_tool(
                    task, project_config, tool, output, branch))
        code_result = await self._step_code(task, project_config, tool)
        await self._absorb_code_result(task, output, code_result)
        code_result = await self._continue_blocked_candidate(
            task, project_config, tool, branch, output, code_result)
        if code_result.waiting_reason:
            if branch is not None:
                self._drop_simple_branch(branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = code_result.waiting_reason
            output.resume_at = code_result.resume_at or (datetime.now(timezone.utc) + timedelta(hours=2))
            return await self._reliability_finish(task, tool, branch, output)
        if code_result.error:
            if branch is not None:
                self._save_simple_checkpoint(branch, output.params)
            if "rate limit" in code_result.error.lower():
                output.status = TaskStatus.WAITING
                output.waiting_reason = code_result.error
                output.resume_at = datetime.now(timezone.utc) + timedelta(hours=2)
            else:
                output.status = TaskStatus.FAILED
                output.error = code_result.error
                if code_result.provider_retryable:
                    self._hint_reliability_capture(output, {
                        "kind": "brake_exhausted", "brake": "provider"})
            return await self._reliability_finish(task, tool, branch, output)

        verdict = (code_result.agent_result or {}).get("resultado")
        if verdict in {"bloqueado", "no_aplica"}:
            # The agent declined: no gates, no merge. The verdict rides
            # in params for the post-persist return (TASK-2026-00202).
            return await self._reliability_finish(
                task, tool, branch, self._return_agent_verdict(
                    task, tool, branch, code_result, output.params,
                    prior_steps=output.steps, cost_runs=output.cost_runs))

        if branch is None:
            if resolve_review(task).mode != REVIEW_OFF:
                output.steps.append(self._step(
                    "review", "skipped", "no repo snapshot available"))
            output.status = TaskStatus.COMPLETED
            return await self._reliability_finish(task, tool, branch, output)
        return await self._reliability_finish(task, tool, branch,
            await self._finish_simple_tool(
                task, project_config, tool, output, branch))

    async def _absorb_code_result(
        self,
        task: dict[str, Any],
        output: DevExecutionOutput,
        code_result: CodingStepResult,
        *, first_turn: bool = True,
    ) -> None:
        """Fold one builder turn into the run output (cost, trail, params)."""
        if code_result.cost_run is not None:
            output.cost_runs.append(code_result.cost_run)
        output.steps.append(code_result.step)
        output.coding_session_id = code_result.session_id
        output.coding_session_title = code_result.session_title
        output.last_prompt_sent = code_result.prompt
        output.last_response_summary = code_result.summary
        base = output.params if output.params is not None else task.get("params")
        output.params = apply_review_links_to_params(
            base, code_result.review_links)
        output.params = dict(
            output.params if output.params is not None else base or {})
        output.params.pop("dev_checkpoint", None)
        output.params.pop("agent_result", None)
        output.params.pop("kept_work", None)
        output.params.pop(SCHEDULED_WAIT_COUNT_KEY, None)
        if code_result.agent_result is not None:
            output.params["agent_result"] = code_result.agent_result
        if code_result.executor_link:
            # The Tier C chain link that ran this ticket (policy §25/§32):
            # the chain is availability, not selection, so the link is
            # recorded per ticket. The funnel replaces ``params``, so start
            # from the full set (never a partial dict).
            params = dict(output.params)
            params["executor_link"] = code_result.executor_link
            output.params = params
            if first_turn:
                await self._record_tier_c_event(
                    task, "executor", executor_link=code_result.executor_link)

    @staticmethod
    def _builder_gate_context(
        dev_cfg: Mapping[str, Any],
    ) -> tuple[list[GateSpec], str]:
        """Gates and oracle command the orchestrator runs for the builder."""
        try:
            gates = load_gates(dev_cfg)
        except ValueError:
            gates = []
        oracle_cmd = dev_cfg.get("oracle_cmd")
        return gates, oracle_cmd if isinstance(oracle_cmd, str) else ""

    async def _continue_blocked_candidate(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        branch: _SimpleBranch | None,
        output: DevExecutionOutput,
        code_result: CodingStepResult,
    ) -> CodingStepResult:
        """One more builder turn on the same tree before a bloqueado discards it.

        The builder is told the sandbox facts and the gates, then answers
        again: hecho goes on to the gates; bloqueado, an error or a turn
        without verdict leave the original verdict standing. One turn only.
        """
        if (branch is None or code_result.error or code_result.waiting_reason
                or not blocked_continuation_due(code_result.agent_result, True)
                or not self._simple_changed_files(branch)):
            return code_result
        if await self._task_cancelled(str(task.get("task_id") or "")):
            return code_result
        gates, oracle_cmd = self._builder_gate_context(
            project_config.get("dev", {}) or {})
        output.steps.append(self._step(
            "blocked_continuation", "ok",
            "bloqueado with work in the tree: one continuation turn "
            "(sandbox facts and gates) before the candidate is set aside"))
        follow = await self._step_code(
            task, project_config, tool, retry=False,
            prompt_prefix=blocked_continuation_prompt(
                code_result.agent_result or {}, gates, oracle_cmd,
                sandboxed=project_isolation(
                    project_config.get("dev", {}) or {}) != "host"))
        if (follow.error or follow.waiting_reason
                or follow.agent_result is None):
            if follow.cost_run is not None:
                output.cost_runs.append(follow.cost_run)
            output.steps.append(follow.step)
            return code_result
        await self._absorb_code_result(
            task, output, follow, first_turn=False)
        return follow

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

    def _keep_candidate(
        self, branch: _SimpleBranch, kind: str,
    ) -> dict[str, Any] | None:
        """Commit the candidate to ``<kind>/<work branch>`` before a discard.

        The builder leaves its work uncommitted, so a reset destroys it.
        Returns the kept-work record; None when there is nothing to keep
        or git refuses (the caller then drops as before).
        """
        repo = branch.repo
        git = self._simple_git
        try:
            if git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() \
                    == branch.branch:
                git(repo, "add", "-A", "--", ".", ":!.openceo")
                if git(repo, "diff", "--cached", "--quiet").returncode != 0:
                    if (repo / ".openceo" / "HANDOFF.md").is_file():
                        git(repo, "add", "-f", "--", ".openceo/HANDOFF.md")
                    name = git(repo, "config", "user.name").stdout.strip()
                    email = git(repo, "config", "user.email").stdout.strip()
                    identity = [] if name and email else [
                        "-c", "user.name=OpenCEO",
                        "-c", "user.email=openceo@jardal.app"]
                    if git(repo, *identity, "commit", "-qm",
                           f"wip({branch.ref}) {kind} candidate",
                           "--no-verify", "--no-gpg-sign").returncode != 0:
                        return None
            head = git(repo, "rev-parse",
                       f"refs/heads/{branch.branch}").stdout.strip()
            if not head or head == branch.base_sha:
                return None
            kept = f"{kind}/{branch.branch}"
            if git(repo, "branch", "-f", kept, head).returncode != 0:
                return None
            stat = git(repo, "diff", "--stat=100", "--stat-count=20",
                       branch.base_sha, kept, "--", ".", ":!.openceo"
                       ).stdout.strip()
            files = [path for path in git(
                repo, "diff", "--name-only", branch.base_sha, kept,
                "--", ".", ":!.openceo").stdout.splitlines() if path]
        except (OSError, subprocess.SubprocessError):
            logger.warning("keeping the candidate of %s failed",
                           branch.branch, exc_info=True)
            return None
        return {"branch": kept, "kind": kind, "base_sha": branch.base_sha,
                "head_sha": head, "files": len(files), "diffstat": stat[:1500]}

    def _reset_and_drop_branch(
        self, branch: _SimpleBranch, kind: str = "discarded",
    ) -> dict[str, Any] | None:
        """Keep the candidate, reset the work branch to base and drop it.

        Declined/failed work never dies with the branch: the kept-work
        record names where it lives (None when there was nothing to keep).
        """
        kept = self._keep_candidate(branch, kind)
        self._simple_git(branch.repo, "reset", "--hard", "-q", branch.base_sha)
        self._simple_git(branch.repo, "clean", "-fdq")
        self._drop_simple_branch(branch)
        return kept

    def _open_simple_branch(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        repo: Path,
    ) -> "_SimpleBranch | DevExecutionOutput":
        """Open the ticket work branch (+ baseline gates, §10/§12)."""
        dev_cfg = project_config.get("dev", {}) or {}
        checkpoint = (task.get("params") or {}).get("dev_checkpoint")
        if isinstance(checkpoint, dict):
            return self._restore_simple_checkpoint(repo, checkpoint, task)
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
                logger.warning("objective branch %s unusable; parking",
                               objective_id, exc_info=True)
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
            params = dict(task.get("params") or {})
            retries = int(params.get("baseline_retries") or 0)
            if retries < 2:
                # A red baseline can be transient (another merge in flight,
                # a gate environment that just changed): retry before
                # parking for the owner (TASK-2026-00481).
                params["baseline_retries"] = retries + 1
                return DevExecutionOutput(
                    status=TaskStatus.WAITING,
                    waiting_reason=(
                        f"baseline_red: {names} (automatic retry "
                        f"{retries + 1}/2)"),
                    resume_at=(datetime.now(timezone.utc)
                               + timedelta(minutes=15)),
                    params=params,
                )
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"needs_owner: baseline_red: {names}",
                params=params or None,
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

    def _park_branch_for_wait(
        self, task: dict[str, Any], output: DevExecutionOutput,
        branch: _SimpleBranch,
    ) -> None:
        """Keep the built candidate across a wait that resumes.

        Dropping the branch there threw the whole build away and the
        resume rebuilt from scratch (TASK-2026-00478). A checkpoint keeps
        the branch and its params so the next run restores it; when git
        refuses, the old drop stands.
        """
        params = dict(output.params if isinstance(output.params, dict)
                      else (task.get("params") or {}))
        try:
            self._save_simple_checkpoint(branch, params, build_done=True)
        except (ValueError, OSError, subprocess.SubprocessError):
            logger.warning("wait checkpoint failed for %s; dropping the branch",
                           branch.branch, exc_info=True)
            self._drop_simple_branch(branch)
            return
        output.params = params

    def _save_simple_checkpoint(
        self, branch: _SimpleBranch, params: dict[str, Any] | None,
        *, build_done: bool = False,
    ) -> None:
        if params is None:
            raise ValueError("cannot preserve a branch without execution params")
        repo = branch.repo
        if self._simple_git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != branch.branch:
            raise ValueError("cannot checkpoint a different branch")
        self._simple_git(repo, "add", "-A", "--", ".", ":!.openceo")
        if self._simple_git(repo, "diff", "--cached", "--quiet").returncode != 0:
            commit = self._simple_git(
                repo, "-c", "user.name=OpenCEO", "-c", "user.email=openceo@jardal.app",
                "commit", "-qm", f"checkpoint {branch.ref}")
            if commit.returncode != 0:
                raise ValueError("cannot commit execution checkpoint")
        artifacts = {}
        handoff = repo / ".openceo" / "HANDOFF.md"
        if handoff.is_file():
            artifacts["HANDOFF.md"] = handoff.read_text(encoding="utf-8")[:64000]
        params["dev_checkpoint"] = {
            "branch": branch.branch, "base": branch.base,
            "base_sha": branch.base_sha, "ref": branch.ref,
            "head_sha": self._simple_git(repo, "rev-parse", "HEAD").stdout.strip(),
            "oracle_frozen": branch.oracle_frozen,
            "oracle_baseline_red": branch.oracle_baseline_red,
            "build_done": bool(build_done),
            "artifacts": artifacts,
        }
        if self._simple_git(repo, "checkout", "-q", branch.base).returncode != 0:
            raise ValueError("cannot release checkpoint branch")

    def _restore_simple_checkpoint(
        self, repo: Path, checkpoint: dict[str, Any], task: dict[str, Any],
    ) -> _SimpleBranch | DevExecutionOutput:
        branch = str(checkpoint.get("branch") or "")
        expected = str(checkpoint.get("head_sha") or "")
        tip = self._simple_git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").stdout.strip()
        if (not branch or not expected or tip != expected
                or self._simple_git(repo, "checkout", "-q", branch).returncode != 0):
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason="config: execution checkpoint branch is unavailable or changed",
                params=dict(task.get("params") or {}))
        scratch = repo / ".openceo"
        scratch.mkdir(exist_ok=True)
        for name, content in (checkpoint.get("artifacts") or {}).items():
            if name == "HANDOFF.md":
                (scratch / name).write_text(str(content), encoding="utf-8")
        return _SimpleBranch(
            repo=repo, branch=branch, base=str(checkpoint["base"]),
            base_sha=str(checkpoint["base_sha"]), ref=str(checkpoint["ref"]),
            oracle_frozen=checkpoint.get("oracle_frozen"),
            oracle_baseline_red=str(checkpoint.get("oracle_baseline_red") or ""),
            build_done=bool(checkpoint.get("build_done")))

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
            if path == ".openceo" or path.startswith(".openceo/"):
                continue
            try:
                added = int(fields[0]) if fields[0] != "-" else 0
                deleted = int(fields[1]) if fields[1] != "-" else 0
            except ValueError:
                continue
            if is_test_path(path):
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
                       if name != ".openceo" and not name.startswith(".openceo/")})

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
            assigned = str((params or {}).get("tier_assigned_by") or "")
            if assigned.startswith("loose_classifier"):
                origin = ("frozen from the ticket text by the retired "
                          "loose classifier")
            elif assigned.startswith("explicit_default"):
                origin = "defaulted on explicit dev (§25)"
            else:
                origin = "declared by the planner"
            output.steps.append(self._step(
                "oracle", "failed",
                f"protected oracle refs modified by builder: {names} ({origin})"))
            if tool == CodingTool.TIER_C:
                await self._record_tier_c_event(task, "cycle_failure")
            return await self._material_simple_failure(
                task, project_config, tool, branch,
                f"oracle modified: {names} ({origin})",
                prior_steps=output.steps, params_base=output.params,
                cost_runs=output.cost_runs)
        if not recheck:
            protected = sum(1 for item in
                            branch.oracle_frozen.get("refs", [])
                            if isinstance(item, dict) and item.get("resolved"))
            output.steps.append(self._step(
                "oracle", "ok",
                f"{protected} protected oracle refs intact "
                f"({branch.oracle_frozen.get('oracle_type')})"))
        return None

    async def _run_simple_gates(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        branch: _SimpleBranch,
        output: DevExecutionOutput,
        specs: list[Any],
        budgets: dict[str, Any],
    ) -> Any:
        """Gate loop with builder fix retries. Results when green, else the
        wait/fail output to return (material failure, tampered oracle)."""
        repo = branch.repo
        retries = int(budgets.get("gate_retries", 2) or 0)
        attempt = 0
        while True:
            results = await asyncio.to_thread(run_gates, repo, specs)
            try:
                from reliability import exposures as _exposures

                await _exposures.record_exposure(
                    self._repo, "gates",
                    project_key=str(task.get("project") or ""),
                    task_id=str(task.get("task_id") or "") or None)
            except Exception:
                logger.warning("gates exposure failed (fail-open)",
                               exc_info=True)
            failed = gates_failed(results)
            output.steps.append(self._step(
                "gates", "ok" if not failed else "failed",
                "; ".join(f"{item.name}={item.status}" for item in results)
                or "no gates configured"))
            if not failed:
                if attempt:
                    module = self._loop_brake()
                    if module is not None:
                        self._note_reliability_milestone(
                            output, module.MILESTONE_GATE_FIXED)
                return results
            if tool == CodingTool.TIER_C:
                await self._record_tier_c_event(task, "cycle_failure")
            if attempt >= retries:
                names = ", ".join(item.name for item in failed)
                return await self._material_simple_failure(
                    task, project_config, tool, branch,
                    f"gates failed after {retries} retries: {names}",
                    prior_steps=output.steps, params_base=output.params,
                    cost_runs=output.cost_runs)
            tail = "\n\n".join(
                f"[{item.name}] {item.status}: {item.detail}\n{item.output_tail}"
                for item in failed)
            followup = await self._simple_followup(
                tool, project_config, repo,
                f"Gates failed:\n{tail[:6000]}\nFix the code (no commits), "
                "keep HANDOFF.md truthful. Never edit the frozen oracle "
                "files: fix the code under test.",
                task_id=str(task.get("task_id") or ""),
                project=str(task.get("project") or ""),
                badge_ticket=badge_ticket_for_task(task) or "")
            if followup is not None:
                output.cost_runs.append(followup)
                if self._failed_followup(followup, branch, output):
                    return output
            # The retry saw the failing oracle's output: re-verify its hashes
            # before running the gates again (a "fix" by editing the test is
            # the oracle being redefined).
            tampered_out = await self._oracle_integrity(
                task, project_config, tool, branch, output, recheck=True)
            if tampered_out is not None:
                return tampered_out
            attempt += 1

    def _simple_reviewer_executor(
        self, pool: Sequence[Mapping[str, Any]],
    ) -> ProcessExecutor:
        """Reviewer executor over the pool (seam for scripted tests)."""
        return ProcessExecutor(pool=list(pool))

    @staticmethod
    def _store_review_checkpoint(
        task: dict[str, Any],
        output: DevExecutionOutput,
        checkpoint: dict[str, Any],
    ) -> None:
        """Checkpoint on the FULL params (the funnel replaces them)."""
        full = dict(output.params if isinstance(output.params, dict)
                    else (task.get("params") or {}))
        full[REVIEW_ROUND_PARAMS_KEY] = checkpoint
        output.params = full

    # -- reliability hunter (TASK-2026-00375 + TASK-2026-00383) --------

    async def _deadline_mark_start(
        self, task: dict[str, Any], tool: Any,
    ) -> bool:
        """Write attempt_id + attempt_deadline_at (TASK-2026-00383, §7).

        Tier S owns its own write inside the cycle; this covers A/B/C.
        Fail-open: without a task service patcher nothing is written.
        After a successful write the dispatcher is woken so its next
        cycle re-arms the watch to the new deadline (registration, §7).
        """
        try:
            if tool is not None and tool is CodingTool.TIER_S:
                return False
            patch = getattr(self._task_service, "patch_task", None)
            if not callable(patch):
                return False
            from reliability import deadlines as deadline_mod
            module = self._loop_brake()
            if module is None:
                return False
            task_id = str(task.get("task_id") or "")
            params = task.get("params")
            attempt_id = module.next_attempt_id(
                task_id, params if isinstance(params, dict) else {})
            timeout_s = getattr(
                self._settings, "DEV_TURN_TIMEOUT_SECONDS", 1800) or 1800
            wake = getattr(self._task_service, "wake_dispatcher", None)
            wrote = await deadline_mod.mark_attempt_start(
                patch, task_id=task_id, attempt_id=attempt_id,
                timeout_seconds=timeout_s,
                notify=wake if callable(wake) else None)
            return wrote is not None
        except Exception:
            logger.warning("deadline mark start failed (fail-open)",
                           exc_info=True)
            return False

    async def _deadline_mark_end(self, task_id: str) -> None:
        """Clear attempt_deadline_at when the attempt ends. Never raises."""
        try:
            patch = getattr(self._task_service, "patch_task", None)
            if not callable(patch):
                return
            from reliability import deadlines as deadline_mod
            await deadline_mod.clear_attempt_deadline(patch, task_id)
        except Exception:
            logger.warning("deadline mark end failed (fail-open)",
                           exc_info=True)

    @staticmethod
    def _loop_brake():
        """Shared capture/brake helper; None when unavailable (fail-open)."""
        try:
            from reliability import loop_brake as module
            return module
        except Exception:
            logger.warning("reliability loop brake unavailable (fail-open)",
                           exc_info=True)
            return None

    def _note_reliability_milestone(
        self, output: DevExecutionOutput, name: str,
    ) -> None:
        """Record an orchestrator-emitted milestone (§4.3) for the finish."""
        module = self._loop_brake()
        if module is None:
            return
        params = output.params if isinstance(output.params, dict) else {}
        output.params = params
        milestones = params.get(module.MILESTONE_KEY)
        if not isinstance(milestones, list):
            milestones = []
            params[module.MILESTONE_KEY] = milestones
        if name not in milestones:
            milestones.append(name)

    def _hint_reliability_capture(
        self, output: DevExecutionOutput, hint: dict[str, Any],
    ) -> None:
        """Leave an explicit capture hint for the attempt finish."""
        module = self._loop_brake()
        if module is None:
            return
        params = output.params if isinstance(output.params, dict) else {}
        output.params = params
        params[module.CAPTURE_HINT_KEY] = dict(hint)

    @staticmethod
    def _reliability_plan(
        output: DevExecutionOutput, hint: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Map one attempt outcome to executed/marker/capture (no I/O)."""
        reason = str(output.waiting_reason or "")
        error = str(output.error or "")
        if output.status == TaskStatus.COMPLETED:
            return {"executed": True, "marker": "hecho", "capture": None}
        if output.status == TaskStatus.WAITING:
            if reason.startswith("scheduled_wait:"):
                return {"executed": False, "marker": "waiting:scheduled",
                        "capture": None}
            if "needs_owner: scheduled_wait" in reason:
                return {"executed": True, "marker": "brake_exhausted:scheduled_wait",
                        "capture": {"kind": "brake_exhausted", "brake": "scheduled_wait"}}
            if "needs_owner: review quorum unreachable" in reason:
                return {"executed": True, "marker": "brake_exhausted:quorum",
                        "capture": {"kind": "brake_exhausted", "brake": "quorum"}}
            if "needs_owner: review findings without verdict" in reason:
                return {"executed": True, "marker": "brake_exhausted:correction",
                        "capture": {"kind": "brake_exhausted", "brake": "correction"}}
            if hint is not None and hint.get("capture") == "none":
                return {"executed": bool(hint.get("executed", False)),
                        "marker": "waiting:held", "capture": None}
            return {"executed": output.resume_at is None,
                    "marker": f"waiting:{reason[:60]}".strip(),
                    "capture": None}
        if hint is not None and hint.get("kind"):
            brake = str(hint.get("brake") or "")
            marker = f"brake_exhausted:{brake}" if brake else "failed"
            return {"executed": True, "marker": marker, "capture": dict(hint)}
        if "gates failed after" in error:
            return {"executed": True, "marker": "brake_exhausted:gates",
                    "capture": {"kind": "brake_exhausted", "brake": "gates"}}
        verdict = ""
        params = output.params if isinstance(output.params, dict) else {}
        agent_result = params.get("agent_result")
        if isinstance(agent_result, dict):
            verdict = str(agent_result.get("resultado") or "")
        marker = verdict if verdict in ("bloqueado", "no_aplica") else "terminal_failure"
        return {"executed": True, "marker": marker,
                "capture": {"kind": "terminal_failure"}}

    async def _note_exposure(
        self, task: dict[str, Any], component: str,
    ) -> None:
        """Count one pass through ``component``; never raises (fail-open)."""
        try:
            from reliability import exposures as _exposures

            await _exposures.record_exposure(
                self._repo, component,
                project_key=str(task.get("project") or ""),
                task_id=str(task.get("task_id") or "") or None)
        except Exception:
            logger.warning("%s exposure failed (fail-open)",
                           component, exc_info=True)

    async def _reliability_finish(
        self,
        task: dict[str, Any],
        tool: CodingTool,
        branch: _SimpleBranch | None,
        output: DevExecutionOutput,
    ) -> DevExecutionOutput:
        """Attempt-boundary capture plus ProgressGuard (shadow by default).

        The guard state lands in ``output.params`` next to ``dev_checkpoint``,
        so the funnel persists both in the same atomic write. Shadow
        would-stops record a ``stall`` with ``shadow=true`` and let the loop
        continue; enforce stops park the ticket as stalled with no resume.
        """
        module = self._loop_brake()
        if module is None:
            return output
        params = output.params if isinstance(output.params, dict) else {}
        output.params = params
        hint = module.take_capture_hint(params)
        milestones = module.take_milestones(params)
        if output.status == TaskStatus.COMPLETED \
                and module.MILESTONE_PHASE_CLOSED not in milestones:
            milestones.append(module.MILESTONE_PHASE_CLOSED)
        task_id = str(task.get("task_id") or "")
        phase = tool.value if isinstance(tool, CodingTool) else str(tool or "build")
        attempt_id = module.next_attempt_id(task_id, params)
        plan = self._reliability_plan(output, hint)
        marker = str(plan["marker"])
        result_sig = module.make_result_sig(phase, marker)
        if branch is not None:
            checkpoint = params.get("dev_checkpoint")
            head_sha = checkpoint.get("head_sha") \
                if isinstance(checkpoint, dict) else None
            work_fp, fp_complete = module.dev_work_fp(
                str(branch.repo), branch.base_sha,
                head_sha if isinstance(head_sha, str) else None)
        else:
            work_fp, fp_complete = "unknown", False
        mode = module.guard_mode(module.DEV_PRODUCER)
        now = module.utcnow_iso()
        for milestone in dict.fromkeys(milestones):
            module.evaluate_attempt(
                params, producer=module.DEV_PRODUCER, phase=phase,
                attempt_id=attempt_id, result_sig=result_sig, work_fp=work_fp,
                fp_complete=fp_complete, executed=bool(plan["executed"]),
                milestone=milestone, mode=mode, now=now)
        decision = module.evaluate_attempt(
            params, producer=module.DEV_PRODUCER, phase=phase,
            attempt_id=attempt_id, result_sig=result_sig, work_fp=work_fp,
            fp_complete=fp_complete, executed=bool(plan["executed"]),
            mode=mode, now=now)
        module.brake_record(params, decision, mode=mode, now=now)
        if decision.get("brake_error"):
            await module.record_async(
                self._repo, kind="exception", component="reliability",
                task_id=task_id, attempt_id=attempt_id, phase=phase,
                exc_text=str(decision["brake_error"])[:300],
                message="progress guard failed open; the loop continues"[:300])
        capture = plan["capture"]
        if capture is not None:
            await self._capture_attempt(
                module, task_id, phase, attempt_id, capture, output)
        if decision.get("incident"):
            await module.record_async(
                self._repo, kind="stall", component="dev_session",
                task_id=task_id, attempt_id=attempt_id, phase=phase,
                guard_rule=str(decision.get("rule") or ""),
                result_signature=result_sig,
                shadow=bool(decision.get("shadow", True)),
                message=f"progress brake {decision.get('rule')} "
                        f"({decision.get('action')})"[:300])
        if decision.get("action") == "stop":
            if branch is not None:
                try:
                    self._save_simple_checkpoint(branch, params)
                except Exception:
                    logger.warning("stalled checkpoint failed (fail-open)",
                                   exc_info=True)
            rule = str(decision.get("rule") or "")
            kept = branch.branch if branch is not None else "n/a"
            output.status = TaskStatus.WAITING
            output.waiting_reason = (
                f"stalled_no_progress: {rule} in phase '{phase}'. "
                "Motivo: el freno nativo detuvo el bucle tras intentos sin "
                "avance. Condición de reanudación: nuevo diff en la rama, "
                "informe de review nuevo o gate en verde; solo el Owner u "
                f"otro actor autorizado rearma el freno (rama conservada: {kept}).")
            output.resume_at = None
        await self._deadline_mark_end(task_id)
        return output

    async def _capture_attempt(
        self,
        module: Any,
        task_id: str,
        phase: str,
        attempt_id: str,
        capture: dict[str, Any],
        output: DevExecutionOutput,
    ) -> None:
        """Record one attempt-boundary incident (never breaks the loop)."""
        kind = str(capture.get("kind") or "terminal_failure")
        brake = str(capture.get("brake") or "")
        error = str(output.error or output.waiting_reason or "")
        if kind == "brake_exhausted":
            reason = brake or error[:120]
            await module.record_async(
                self._repo, kind=kind, component="dev_session",
                task_id=task_id, attempt_id=attempt_id, phase=phase,
                brake=brake or None, reason_code=reason or None,
                message=error[:500] or None)
            return
        reason_code = module.terminal_reason_code(error)
        await module.record_async(
            self._repo, kind="terminal_failure", component="dev_session",
            task_id=task_id, attempt_id=attempt_id, phase=phase,
            reason_code=reason_code, message=error[:500] or None)

    async def _capture_simple_exception(
        self, task: dict[str, Any], tool: Any, exc: BaseException,
    ) -> None:
        """Record one uncontrolled exception, then let the caller re-raise."""
        try:
            module = self._loop_brake()
            if module is None:
                return
            phase = tool.value if isinstance(tool, CodingTool) else str(tool or "build")
            await module.record_async(
                self._repo, kind="exception", component="dev_session",
                task_id=str(task.get("task_id") or ""), phase=phase,
                exc_type=type(exc).__name__, exc_text=str(exc)[:300],
                message=f"unhandled: {exc}"[:500])
        except Exception:
            logger.warning("exception capture failed (fail-open)", exc_info=True)

    @staticmethod
    def _simple_fix_prompt(count: int, missing: list[int]) -> str:
        """Fix-or-discard prompt reusing the FIX verdict contract (§7.9)."""
        scope = (f"Decide every finding in .openceo/FINDINGS_1.md "
                 f"({count} findings).")
        if missing:
            names = ", ".join(f"F{index}" for index in missing)
            scope = (f"Still missing verdicts for {names} in "
                     f".openceo/FINDINGS_1.md ({count} findings). "
                     f"Decide them now.")
        return (
            f"{scope} They are verified: they point at real code. Judge "
            "the reasoning: is the problem real, is the proposed fix "
            "correct, is it in this ticket's scope?\n"
            "Write .openceo/FIX_1.md with one line per finding: "
            "`F<n>: CORREGIDO — <what you changed>` (and change the code, "
            "no commits) or `F<n>: DESCARTADO — <verifiable technical "
            "reason>`. `I disagree` is not a reason. Keep HANDOFF.md "
            "truthful. Do not rewrite uninvolved code.")

    async def _run_simple_reviewer(
        self,
        *,
        executor: Any,
        slots: Any,
        reviews: Any,
        repo: Path,
        review_sha: str,
        request: str,
        request_name: str,
        handoff: str,
        base_sha: str,
        acceptance: list[str],
        task_id: str,
        project: str,
        timeout_default: Any,
        cost_runs: list[dict[str, Any]],
        tag_prefix: str,
        slot: review_mod.ReviewSlot,
        attempt: int,
        previous: Mapping[str, Any] | None,
        routing: wiring_mod.RoundRouting | None = None,
    ) -> dict[str, Any]:
        """One reviewer attempt on a detached read-only worktree (00251).

        Mirrors the Tier S attempt (classify, write detection, parse,
        verify, persist) without the cycle context; scope ``simple`` keeps
        these reports out of the S precision stats.
        """
        outcome = review_mod.new_outcome(slot, attempt)
        outcome["review_id"] = f"rev-{uuid.uuid4().hex[:12]}"
        suffix = f"-a{attempt}" if attempt > 1 else ""
        try:
            worktree = await asyncio.to_thread(
                review_mod.create_worktree, repo, review_sha,
                f"{tag_prefix}-{slot.label}{suffix}")
        except RuntimeError as exc:
            outcome.update(kind="worktree", reason=str(exc)[:200])
            return outcome
        try:
            (worktree / request_name).write_text(request, encoding="utf-8")
            (worktree / "HANDOFF.md").write_text(handoff, encoding="utf-8")
            before = await asyncio.to_thread(
                review_mod.worktree_snapshot, worktree)
            timeout_s = review_mod.slot_timeout_s(
                slot, timeout_default, previous)
            started = datetime.now(timezone.utc).isoformat()
            result = await executor.run(
                f"review:{slot.label}",
                review_mod.reviewer_prompt(
                    slot.label, request_name, "simple", 1,
                    repo=worktree, retry=review_mod.retry_note(previous)),
                worktree, timeout_s)
            finished = datetime.now(timezone.utc).isoformat()
            elapsed = float(result.elapsed_s or 0.0)
            outcome.update(elapsed_s=elapsed,
                           exit_code=result.returncode)
            cost_runs.append(cost_mod.normalize_run({
                "engine": f"review:{slot.label}", "duration_s": elapsed}))
            after = await asyncio.to_thread(
                review_mod.worktree_snapshot, worktree)
            if before and after and before != after:
                outcome.update(kind="write_attempt", reason="write_attempt")
                return outcome
            text = (result.output_tail or "").strip()
            if result.returncode != 0:
                if (result.returncode == TIMEOUT_RETURNCODE
                        or (result.usage or {}).get("timeout")):
                    outcome.update(
                        kind="timeout",
                        reason=text[:200] or f"timeout after {timeout_s}s")
                else:
                    action, patch = review_mod.classify_slot_error(
                        text, slot)
                    if action in {"cooling_down", "disabled"}:
                        await slots.set_slot(slot.label, patch)
                    outcome.update(
                        kind=(action if action in {"cooling_down",
                                                  "disabled"}
                              else "slot_error"),
                        reason=f"slot error ({action}): {text[:200]}")
                return outcome
            try:
                raw = findings_mod.parse_report(text)
            except findings_mod.ReportMalformed as exc:
                outcome.update(kind="malformed",
                               reason=f"malformed: {exc}")
                return outcome
            verdicts = await asyncio.to_thread(
                findings_mod.verify_report, raw, repo, review_sha,
                base_sha, slot.label, slot.family, acceptance)
            outcome.update(verdicts=verdicts, valid=True, kind="valid")
            if routing is not None and slot.label in routing.states:
                receipt = wiring_mod.record_receipt(
                    routing, slot.label, report_text=text,
                    reviewed_commit=review_sha, started_at=started,
                    finished_at=finished)
                outcome["receipt"] = receipt.to_dict()
            return outcome
        finally:
            await asyncio.to_thread(
                review_mod.remove_worktree, repo, worktree)
            if reviews is not None:
                try:
                    meta: dict[str, Any] = {
                        "attempt": attempt,
                        "kind": str(outcome.get("kind") or ""),
                        "elapsed_s": float(
                            outcome.get("elapsed_s") or 0.0),
                        "exit_code": outcome.get("exit_code"),
                        "project": project,
                        "account": slot.account,
                        "model": slot.model,
                        "tier": slot.cost_tier}
                    if isinstance(outcome.get("receipt"), dict):
                        meta["receipt"] = outcome["receipt"]
                    if outcome.get("spend"):
                        meta["spend"] = outcome["spend"]
                    await reviews.create_review(
                        findings_mod.review_document(
                            review_id=str(outcome.get("review_id") or ""),
                            task_id=task_id, run_id=f"simple-{tag_prefix}",
                            scope="simple", round_no=1, slot=slot.label,
                            family=slot.family,
                            valid=bool(outcome.get("valid")),
                            verdicts=list(outcome.get("verdicts") or []),
                            invalid_reason=str(outcome.get("reason") or ""),
                            meta=meta))
                except Exception:
                    logger.warning("review telemetry persistence failed",
                                   exc_info=True)

    async def _maybe_simple_review(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        output: DevExecutionOutput,
        branch: _SimpleBranch,
        gate_results: list[Any],
        specs: list[Any],
        budgets: dict[str, Any],
    ) -> DevExecutionOutput | None:
        """Single optional review round (00251). None = proceed to squash.

        Review=off returns untouched: zero reviewer invocations and no
        pool, quota or auth touched. Review=on fixes a snapshot commit,
        runs one quorum round over the common pool, drives builder fix
        verdicts and re-runs the final gates. Waits keep the checkpoint
        in params; unrecoverable states park for the owner.
        """
        # run() gates invalid input and S+off first; only on/off arrive.
        if resolve_review(task).mode == REVIEW_OFF:
            return None
        pool_raw = project_config.get("review", {}).get("reviewers") or []
        if not pool_raw:
            output.steps.append(self._step(
                "review", "skipped", "no review pool configured"))
            self._store_review_checkpoint(task, output, {
                "phase": "skipped", "mode": "on",
                "reason": "no review pool configured"})
            return None
        repo = branch.repo
        try:
            pool = review_mod.parse_pool(pool_raw)
        except review_mod.ReviewerConfigError as exc:
            self._park_branch_for_wait(task, output, branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = f"config: review pool: {exc}"
            return output
        self._simple_git(repo, "add", "-A", "--", ".", ":!.openceo")
        tree_sha = self._simple_git(repo, "write-tree").stdout.strip()
        full = dict(output.params if isinstance(output.params, dict)
                    else (task.get("params") or {}))
        checkpoint = _read_review_checkpoint(full)
        if (checkpoint is not None
                and checkpoint.get("tree_sha") == tree_sha
                and checkpoint.get("base_sha") == branch.base_sha
                and checkpoint.get("phase") in ("reviewed", "done")):
            kept = checkpoint.get("reports") or []
            output.steps.append(self._step(
                "review", "reused",
                f"same snapshot {tree_sha[:12]}: {len(kept)} stored "
                "report(s) kept, no reviewer runs"))
            if checkpoint.get("phase") == "done":
                return None
            findings = _rehydrate_findings(checkpoint.get("findings") or [])
            self._write_simple_findings(repo, checkpoint)
            return await self._simple_review_fixes(
                task, project_config, tool, output, branch, specs,
                budgets, findings, checkpoint)
        identity: list[str] = []
        name = self._simple_git(repo, "config", "user.name").stdout.strip()
        email = self._simple_git(repo, "config", "user.email").stdout.strip()
        if not (name and email):
            identity = ["-c", "user.name=OpenCEO",
                        "-c", "user.email=openceo@jardal.app"]
        if (self._simple_git(repo, "diff", "--cached", "--quiet").returncode != 0
                and self._simple_git(
                repo, *identity, "commit", "-qm",
                f"review checkpoint {branch.ref}").returncode != 0):
            output.status = TaskStatus.FAILED
            output.error = "review checkpoint commit failed"
            return output
        review_sha = self._simple_git(
            repo, "rev-parse", "HEAD").stdout.strip()
        builder_family = _simple_builder_family(tool, full)
        review_cfg = project_config.get("review", {}).get("review", {})
        try:
            assembly = wiring_mod.assemble_review_pool(
                pool_raw, review_cfg=review_cfg,
                run_id=f"simple-{task.get('task_id') or branch.ref}",
                round="1", builder_family=builder_family,
                receipts_dir=repo / ".openceo" / "review_receipts")
        except wiring_mod.ReviewRoutingError as exc:
            self._park_branch_for_wait(task, output, branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = f"config: free-router: {exc}"
            return output
        routing = assembly.routing
        if routing is not None:
            pool = review_mod.parse_pool(assembly.raw_pool)
            prefer_families = (*assembly.prefer_families, "gemini")
        else:
            prefer_families = SIMPLE_REVIEW_PREFER_FAMILIES
        slots = (MongoSlotStore(self._repo)
                 if self._repo is not None else InMemorySlotStore())
        reviews = (MongoReviewStore(self._repo)
                   if self._repo is not None else InMemoryReviewStore())
        await wiring_mod.activate_routed_slots(routing, slots)
        plan = await review_mod.select_round(
            pool, slots, SIMPLE_REVIEW_COUNT,
            builder_family=builder_family,
            prefer_families=prefer_families,
            include_extras=False,
            cascade_enabled=review_mod.cascade_enabled(
                review_cfg.get("cascade")))
        try:
            from reliability import exposures as _exposures

            await _exposures.record_exposure(
                self._repo, "review",
                project_key=str(task.get("project") or ""),
                task_id=str(task.get("task_id") or "") or None)
        except Exception:
            logger.warning("review exposure failed (fail-open)",
                           exc_info=True)
        acceptance = [str(item) for item in
                      (task.get("acceptance") or [])]
        handoff_path = repo / ".openceo" / "HANDOFF.md"
        try:
            handoff = handoff_path.read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            handoff = ""
        diff_stat = self._simple_git(
            repo, "diff", "--stat",
            branch.base_sha, review_sha).stdout
        request = review_mod.build_review_request(
            1, goal=str(task.get("title") or ""),
            acceptance=acceptance, base_sha=branch.base_sha,
            head_sha=review_sha, diff_stat=diff_stat,
            files=self._simple_changed_files(branch), out_of_plan=[],
            gates_section=handoff_gates_section(gate_results),
            handoff_path=".openceo/HANDOFF.md")
        request_name = "REVIEW_REQUEST_1.md"
        (repo / ".openceo" / request_name).write_text(
            request, encoding="utf-8")
        executor = self._simple_reviewer_executor(assembly.raw_pool)

        async def run_one(
            slot: review_mod.ReviewSlot, attempt: int,
            previous: Mapping[str, Any] | None,
        ) -> dict[str, Any]:
            async def run_attempt(
                current: review_mod.ReviewSlot,
                tried: Mapping[str, Any] | None,
            ) -> dict[str, Any]:
                return await self._run_simple_reviewer(
                    executor=executor, slots=slots, reviews=reviews,
                    repo=repo, review_sha=review_sha, request=request,
                    request_name=request_name, handoff=handoff,
                    base_sha=branch.base_sha, acceptance=acceptance,
                    task_id=str(task.get("task_id") or ""),
                    project=str(task.get("project") or ""),
                    timeout_default=review_cfg.get(
                        "reviewer_timeout_minutes", 15),
                    cost_runs=output.cost_runs, tag_prefix=branch.ref,
                    slot=current, attempt=attempt, previous=tried,
                    routing=routing)
            if (routing is not None and slot.label in routing.states
                    and attempt == 1 and previous is None):
                hit = wiring_mod.load_valid_receipt(
                    routing.receipts_dir, run_id=routing.run_id,
                    round=routing.round, slot=slot.label,
                    reviewed_commit=review_sha)
                if hit is not None:
                    receipt, text = hit
                    try:
                        raw = findings_mod.parse_report(text)
                    except findings_mod.ReportMalformed:
                        raw = None
                    if raw is not None:
                        verdicts = await asyncio.to_thread(
                            findings_mod.verify_report, raw, repo,
                            review_sha, branch.base_sha, slot.label,
                            receipt.family_id, acceptance)
                        routing.receipts[slot.label] = receipt
                        outcome = review_mod.new_outcome(slot, attempt)
                        outcome["review_id"] = f"rev-{uuid.uuid4().hex[:12]}"
                        outcome.update(
                            verdicts=verdicts, valid=True, kind="valid",
                            receipt=receipt.to_dict())
                        return outcome
            if routing is None or slot.label not in routing.states:
                return await run_attempt(slot, previous)
            return await wiring_mod.run_routed_reviewer(
                run_attempt, routing=routing, slots=slots,
                executor=executor, slot=slot, previous=previous)

        try:
            result = await review_mod.run_quorum_round(
                pool, slots, plan, run_one, builder_family=builder_family,
                quorum=SIMPLE_REVIEW_QUORUM,
                recovery=review_cfg.get("recovery"),
                cascade=review_cfg.get("cascade"))
        finally:
            if routing is not None:
                routing.close()
        reports = [{
            "slot": str(item.get("slot")),
            "family": str(item.get("family") or next(
                (slot.family for slot in pool
                 if slot.label == str(item.get("slot"))), "")),
            "account": str(item.get("account") or next(
                (slot.account for slot in pool
                 if slot.label == str(item.get("slot"))), "")),
            "model": str(item.get("model") or next(
                (slot.model for slot in pool
                 if slot.label == str(item.get("slot"))), "")),
            "review_id": str(item.get("review_id") or ""),
            "valid": bool(item.get("valid")),
            "kind": str(item.get("kind") or ""),
            **({"receipt": item["receipt"]}
               if isinstance(item.get("receipt"), dict) else {})}
            for item in result.outcomes]
        if not result.met:
            waits = int(
                (checkpoint or {}).get("no_quorum_waits") or 0) + 1
            self._store_review_checkpoint(task, output, {
                "phase": "round", "mode": "on",
                "base_sha": branch.base_sha, "review_sha": review_sha,
                "tree_sha": tree_sha, "builder_family": builder_family,
                "reviewers": [slot.label for slot in result.reviewers],
                "reports": reports, "quorum": False,
                "no_quorum_waits": waits})
            if waits >= SIMPLE_REVIEW_MAX_NO_QUORUM_WAITS:
                self._park_branch_for_wait(task, output, branch)
                output.status = TaskStatus.WAITING
                output.waiting_reason = (
                    "needs_owner: review quorum unreachable after "
                    f"{waits} attempts: {result.summary()} "
                    f"(branch kept: {branch.branch})")
                return output
            self._park_branch_for_wait(task, output, branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = (
                f"review_no_quorum: {result.summary()}")
            output.resume_at = (datetime.now(timezone.utc)
                                + timedelta(minutes=60))
            return output
        consolidated = findings_mod.consolidate(result.verdicts)
        findings_text = findings_mod.render_findings(
            consolidated, 1, review_sha, builder_family=builder_family)
        (repo / ".openceo" / "FINDINGS_1.md").write_text(
            findings_text, encoding="utf-8")
        prior_waits = int((checkpoint or {}).get(
            "no_quorum_waits") or 0)
        checkpoint = {
            "phase": "reviewed", "mode": "on",
            "base_sha": branch.base_sha, "review_sha": review_sha,
            "tree_sha": tree_sha, "builder_family": builder_family,
            "reviewers": [slot.label for slot in result.reviewers],
            "reports": reports, "quorum": True,
            "findings_text": findings_text,
            "findings": [{
                "id": item.id, "severity": item.severity,
                "category": item.category,
                "anchors": [{
                    "raw": anchor.raw, "path": anchor.path,
                    "line": anchor.line, "base": anchor.base,
                    "criterion": anchor.criterion}
                    for anchor in item.anchors],
                "problem": item.problem, "fix": item.fix,
                "strength": item.strength,
                "families": list(item.families)} for item in consolidated],
            "no_quorum_waits": prior_waits}
        self._store_review_checkpoint(task, output, checkpoint)
        valid_n = sum(1 for report in reports if report["valid"])
        if valid_n:
            module = self._loop_brake()
            if module is not None:
                self._note_reliability_milestone(
                    output, module.MILESTONE_REVIEW_REPORT)
        output.steps.append(self._step(
            "review", "ok",
            f"quorum {valid_n} valid report(s) from "
            f"{len({report['family'] for report in reports if report['valid']})} "
            f"families; {len(consolidated)} consolidated finding(s)"))
        if not consolidated:
            checkpoint["phase"] = "done"
            self._store_review_checkpoint(task, output, checkpoint)
            return None
        return await self._simple_review_fixes(
            task, project_config, tool, output, branch, specs,
            budgets, consolidated, checkpoint)

    def _write_simple_findings(
        self, repo: Path, checkpoint: Mapping[str, Any],
    ) -> None:
        """Rewrite FINDINGS_1.md from the checkpoint (resume path)."""
        try:
            (repo / ".openceo").mkdir(parents=True, exist_ok=True)
            (repo / ".openceo" / "FINDINGS_1.md").write_text(
                str(checkpoint.get("findings_text") or ""), encoding="utf-8")
        except OSError:
            pass

    async def _simple_review_fixes(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        output: DevExecutionOutput,
        branch: _SimpleBranch,
        specs: list[Any],
        budgets: dict[str, Any],
        findings: list[Any],
        checkpoint: dict[str, Any],
    ) -> DevExecutionOutput | None:
        """Builder fix verdicts + final gates. None = proceed to squash."""
        repo = branch.repo
        fix_path = repo / ".openceo" / "FIX_1.md"
        # .openceo/ is gitignored and survives branch drops: a stale FIX_1.md
        # from another ticket must never satisfy this round's verdicts.
        fix_path.unlink(missing_ok=True)
        verdicts: dict[int, list[Any]] = {}
        missing = [item.id for item in findings]
        for turn in range(1, SIMPLE_REVIEW_MAX_FIX_TURNS + 1):
            followup = await self._simple_followup(
                tool, project_config, repo,
                self._simple_fix_prompt(
                    len(findings), missing if turn > 1 else []),
                task_id=str(task.get("task_id") or ""),
                project=str(task.get("project") or ""),
                badge_ticket=badge_ticket_for_task(task) or "")
            if followup is not None:
                output.cost_runs.append(followup)
                if self._failed_followup(followup, branch, output):
                    return output
            try:
                text = fix_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            verdicts = review_mod.parse_fix(text)
            missing, _dup = review_mod.fix_gaps(findings, verdicts)
            if not missing:
                break
        resolutions = []
        for item in findings:
            first = (verdicts.get(item.id) or [None])[0]
            resolutions.append({
                "id": item.id,
                "verdict": first.verdict if first is not None else "missing",
                "reason": first.reason if first is not None else "",
                "out_of_scope": bool(first is not None
                                     and first.out_of_scope)})
        fix_files = sorted({
            part for part in self._simple_git(
                repo, "diff", "--name-only", "-z", checkpoint.get("review_sha", ""),
                "--", ".").stdout.split("\0") if part})
        checkpoint.update(fix_turns=turn, resolutions=resolutions,
                          fix_files=fix_files)
        if missing:
            names = ", ".join(f"F{index}" for index in missing)
            self._store_review_checkpoint(task, output, checkpoint)
            self._park_branch_for_wait(task, output, branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = (
                f"needs_owner: review findings without verdict: {names} "
                f"(branch kept: {branch.branch}; see .openceo/FIX_1.md)")
            return output
        out_of_scope = sum(1 for entry in resolutions
                           if entry["out_of_scope"])
        if out_of_scope:
            output.steps.append(self._step(
                "review", "ok",
                f"{out_of_scope} out-of-scope dismissal(s) recorded; "
                "simple tiers keep no inbox"))
        gated = await self._run_simple_gates(
            task, project_config, tool, branch, output, specs, budgets)
        if isinstance(gated, DevExecutionOutput):
            return gated
        checkpoint["phase"] = "done"
        self._store_review_checkpoint(task, output, checkpoint)
        module = self._loop_brake()
        if module is not None:
            self._note_reliability_milestone(
                output, module.MILESTONE_FINDING_RESOLVED)
        output.steps.append(self._step(
            "review", "ok",
            f"{len(resolutions)} finding(s) resolved, final gates green"))
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
            verdict = (output.params or {}).get("agent_result") or {}
            if not (verdict.get("resultado") == "hecho" and verdict.get("entregables")
                    and str(verdict.get("evidencia") or "").strip()):
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
            self._park_branch_for_wait(task, output, branch)
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
        gated = await self._run_simple_gates(
            task, project_config, tool, branch, output, specs, budgets)
        if isinstance(gated, DevExecutionOutput):
            return gated
        results = gated

        files_n, lines_n = self._simple_diff_sizes(branch, exclude)
        files_budget = int(budgets.get("files_per_ticket", 8) or 8)
        lines_budget = int(budgets.get("lines_per_ticket", 400) or 400)
        brake = int(budgets.get("hard_brake_factor", 2) or 2) * lines_budget
        if files_n > files_budget or lines_n > brake:
            return await self._material_simple_failure(
                task, project_config, tool, branch,
                f"size {files_n} files/{lines_n} lines over budget "
                f"({files_budget}/{brake})",
                prior_steps=output.steps, params_base=output.params,
                cost_runs=output.cost_runs)

        handoff_path = repo / ".openceo" / "HANDOFF.md"
        handoff = ""
        try:
            handoff = handoff_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            handoff = ""
        if not handoff or len(handoff.split()) > 600:
            followup = await self._simple_followup(
                tool, project_config, repo,
                "Write .openceo/HANDOFF.md (executor sections ≤300 words). "
                "Do not touch code.",
                task_id=str(task.get("task_id") or ""),
                project=str(task.get("project") or ""),
                badge_ticket=badge_ticket_for_task(task) or "")
            if followup is not None:
                output.cost_runs.append(followup)
                if self._failed_followup(followup, branch, output):
                    return output
            try:
                handoff = handoff_path.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                handoff = ""
            if not handoff:
                self._park_branch_for_wait(task, output, branch)
                output.status = TaskStatus.WAITING
                output.waiting_reason = (
                    f"needs_owner: missing HANDOFF.md (branch kept: {branch.branch})")
                return output

        reviewed = await self._maybe_simple_review(
            task, project_config, tool, output, branch, results, specs,
            budgets)
        if reviewed is not None:
            return reviewed

        # Squash + merge (§12): only the orchestrator commits.
        # Undo the review checkpoint commit when the round ran (no-op
        # when HEAD is still the base): the squash below stays single.
        self._simple_git(repo, "reset", "--soft", branch.base_sha)
        self._simple_git(repo, "add", "-A", "--", ".", ":!.openceo")
        identity: list[str] = []
        name = self._simple_git(repo, "config", "user.name").stdout.strip()
        email = self._simple_git(repo, "config", "user.email").stdout.strip()
        if not (name and email):
            identity = ["-c", "user.name=OpenCEO",
                        "-c", "user.email=openceo@jardal.app"]
        title = " ".join(str(task.get("title", "")).split())[:72]
        has_changes = self._simple_git(repo, "diff", "--cached", "--quiet").returncode != 0
        commit = (self._simple_git(
            repo, *identity, "commit", "-qm", f"{branch.ref}: {title}") if has_changes else None)
        if commit is not None and commit.returncode != 0:
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
        # Routes gate (§9.4, TASK-2026-00389): a protected diff waits with
        # the exact human action instead of merging. The branch is kept.
        routes_block = await self._protected_routes_check(
            branch, files, sha, branch.ref)
        if routes_block is not None:
            self._park_branch_for_wait(task, output, branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = (
                f"needs_owner: {routes_block} "
                f"(branch kept: {branch.branch})")
            return output
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
            if auto_merge:
                # Post-merge production step (TASK-2026-00415): with
                # auto_merge the base is main (no objective branch exists),
                # so publish it and apply the project's deploy mode.
                production = await asyncio.to_thread(
                    run_production_step, repo, branch.base,
                    normalize_deploy(dev_cfg.get("deploy")))
                branch_note += f"; production: {production.summary}"
                output.steps.append(self._step(
                    "production", "ok" if production.ok else "error",
                    production.detail))
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
        self, tool: CodingTool, project_config: dict[str, Any], repo: Path,
        prompt: str, *, task_id: str = "", project: str = "", badge_ticket: str = "",
    ) -> dict[str, Any] | None:
        elapsed = 0.0
        for attempt in range(len(PROVIDER_RETRY_DELAYS) + 1):
            result = await self._simple_followup_once(
                tool, project_config, repo, PROVIDER_RESUME_PROMPT + prompt if attempt else prompt,
                task_id=task_id, project=project, badge_ticket=badge_ticket)
            if result is None:
                return None
            elapsed += float(result.get("duration_s") or 0)
            result["duration_s"] = elapsed
            if (not result.get("provider_retryable") or attempt == len(PROVIDER_RETRY_DELAYS)
                    or await self._task_cancelled(task_id)):
                return result
            await asyncio.sleep(PROVIDER_RETRY_DELAYS[attempt])
            if await self._task_cancelled(task_id):
                return result
        raise AssertionError("provider retry exhausted without a result")

    def _failed_followup(
        self, run: dict[str, Any], branch: _SimpleBranch, output: DevExecutionOutput,
    ) -> bool:
        if not run.get("error"):
            return False
        if output.params is None:
            output.params = {}
        self._save_simple_checkpoint(branch, output.params)
        output.status = TaskStatus.FAILED
        output.error = str(run["error"])
        return True

    async def _simple_followup_once(
        self,
        tool: CodingTool,
        project_config: dict[str, Any],
        repo: Path,
        prompt: str,
        *,
        task_id: str = "",
        project: str = "",
        badge_ticket: str = "",
    ) -> dict[str, Any] | None:
        """One follow-up turn on the work branch (gate/handoff fixes).

        Returns the turn's cost run (review 00013/H1), or None when no
        executor turn happened (refusals before spawn).
        """
        prompt = with_preamble(prompt, repo, detect_prompt_lang(prompt))
        try:
            executor = await self._resolve_tier_executor(tool, project_config)
        except ValueError:
            return None
        if executor.prompt_via == "argv":
            command = [*executor.argv, prompt]
            stdin_payload: str | None = None
        else:
            command = list(executor.argv)
            stdin_payload = prompt
        overlays: dict[str, bytes] | None = None
        badge: str | None = None
        badge_provider: TicketBadgeProvider | None = None
        extra_env: dict[str, str] = {}
        if executor.label == "tier_c:muse" or (
                executor.role_profile
                and executor.label in ("tier_a", "tier_b")):
            badge_provider = self._ticket_badge_provider(
                badge_ticket, project)
            if badge_provider is None:
                logger.error("follow-up refused without repository: %s",
                             task_id or "unknown task")
                return None
            try:
                overlays, badge, extra_env = await self._profile_run_extras(
                    executor, badge_provider, str(repo))
            except (RoleProfileError, CatalogError, McpAuthError,
                    ValueError, OSError) as exc:
                logger.error("follow-up refused without role profile: %s",
                             exc)
                return None
        started = time.monotonic()
        run_env = {**(executor.env or {}), **extra_env} or None
        try:
            ok, output, err = await self._run_process(
                command, stdin_payload=stdin_payload, timeout=1800,
                cwd=str(repo), env=run_env,
                config_argv=list(executor.argv),
                home_file_overlays=overlays,
                extra_read_roots=_dev_vault_roots(project_config))
        finally:
            if badge is not None and badge_provider is not None:
                await badge_provider.revoke(badge)
        run = cost_mod.normalize_run({
            "engine": str(executor.label),
            "duration_s": time.monotonic() - started,
        })
        if executor.label == "tier_c:muse":
            code, output = _reviewer_output(output, err, "muse", 0 if ok else 1)
            ok = code == 0
        if ok:
            self._provider_responses.add(tuple(executor.argv))
        else:
            run["error"] = self._summarize_failure_output(output, err, fallback="follow-up failed")
            run["provider_retryable"] = transient_provider_error(
                err + "\n" + output, responded=tuple(executor.argv) in self._provider_responses)
        return run

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
        *, prior_steps: list[dict[str, Any]] | None = None,
        cost_runs: list[dict[str, Any]] | None = None,
    ) -> DevExecutionOutput:
        verdict = dict(code_result.agent_result or {})
        params = dict(params_base if params_base is not None else task.get("params") or {})
        params["agent_result"] = verdict
        # A continuation turn adds a step and a cost run to the first turn's:
        # the return carries the whole trail, never just the last turn.
        trail = (list(prior_steps) if prior_steps is not None
                 else [code_result.step])
        runs = (list(cost_runs) if cost_runs is not None
                else ([code_result.cost_run] if code_result.cost_run else []))
        try:
            resume_at = scheduled_resume(AgentResult.model_validate(verdict))
        except ValueError as exc:
            if branch is not None:
                self._save_simple_checkpoint(branch, params)
            return DevExecutionOutput(
                status=TaskStatus.FAILED, error=f"invalid scheduled wait: {exc}",
                params=params, coding_tool=tool)
        if resume_at is not None:
            declines = max(scheduled_wait_declines(task.get("params")),
                           scheduled_wait_declines(params_base)) + 1
            params[SCHEDULED_WAIT_COUNT_KEY] = declines
            if declines >= SCHEDULED_WAIT_MAX_DECLINES:
                if branch is not None:
                    self._save_simple_checkpoint(branch, params)
                reason = (
                    "needs_owner: scheduled_wait declined "
                    f"{declines} times in a row: {verdict['motivo']} "
                    f"(last resume_at={resume_at.isoformat()}; no re-park)")
                return DevExecutionOutput(
                    status=TaskStatus.WAITING, coding_tool=tool,
                    waiting_reason=reason, params=params,
                    last_response_summary=reason,
                    steps=[*trail, self._step("scheduled_wait", "waiting", reason)],
                    cost_runs=runs)
            if branch is not None:
                self._save_simple_checkpoint(branch, params)
            reason = f"scheduled_wait: {verdict['motivo']}"
            return DevExecutionOutput(
                status=TaskStatus.WAITING, coding_tool=tool,
                waiting_reason=reason, resume_at=resume_at, params=params,
                last_response_summary=reason,
                steps=[*trail, self._step("scheduled_wait", "waiting", reason)],
                cost_runs=runs)
        resultado = str(verdict.get("resultado") or "")
        kept = (self._reset_and_drop_branch(
            branch, "blocked" if resultado == "bloqueado" else "declined")
            if branch is not None else None)
        motivo = str(verdict.get("motivo") or "")
        necesidad = str(verdict.get("necesidad") or "").strip()
        tier_label = tool.value if isinstance(tool, CodingTool) else str(tool)
        if branch is None:
            branch_note = "no work branch"
        elif kept is not None:
            branch_note = f"work branch dropped, {kept_work_note(kept)}"
        else:
            branch_note = "work branch dropped"
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
        params.pop(SCHEDULED_WAIT_COUNT_KEY, None)
        params.pop("kept_work", None)
        if kept is not None:
            params["kept_work"] = kept
        if code_result.executor_link:
            params["executor_link"] = code_result.executor_link
        return DevExecutionOutput(
            status=TaskStatus.FAILED,
            coding_tool=tool if isinstance(tool, CodingTool) else None,
            error=evidence[:1500],
            last_response_summary=evidence,
            steps=[*trail, self._step("agent_return", "failed", evidence)],
            params=params,
            cost_runs=runs,
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
        cost_runs: list[dict[str, Any]] | None = None,
    ) -> DevExecutionOutput:
        """Material simple-tier failure (policy §29, replaces F5 escalation).

        Reset the candidate, preserve deterministic evidence, update the
        objective and invoke the planner exactly once. The failed ticket
        stays failed with its tier as history: no automatic staircase to
        another tier and no same-tier requeue of the unchanged ticket.
        Without an objective there is no planner to invoke: the ticket
        fails terminally with evidence, like a code-step error.
        ``prior_steps`` (oracle/gates rounds) lead the returned trail. The
        reset keeps the candidate on ``failed/<branch>`` and the planner is
        told where (TASK-2026-00478).
        """
        kept = self._reset_and_drop_branch(branch, "failed")
        task_id = str(task.get("task_id") or "")
        tier_label = tool.value if isinstance(tool, CodingTool) else str(tool)
        kept_note = f" ({kept_work_note(kept)})" if kept is not None else ""
        evidence = (
            f"Material {tier_label} failure on {self._simple_ref(task)}: "
            f"{cause}. Candidate reset to {branch.base_sha[:12]}{kept_note}; "
            "no automatic retier.")
        # A business cancellation that landed mid-run wins over the
        # failure: failing the ticket and replanning would undo the
        # owner's decision, so the failure stays evidence on the
        # outcome/comment only, never a replan (TASK-2026-00177).
        cancelled = await self._task_cancelled(task_id)
        if cancelled:
            evidence += " Ticket cancelled in Jardal ERP; replan skipped."
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
                tier_label,
                f"{cause}; {kept_work_note(kept)}" if kept is not None
                else cause)
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
        params.pop("kept_work", None)
        if kept is not None:
            params["kept_work"] = kept
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
            cost_runs=list(cost_runs or []),
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
            logger.exception("planner replan failed")
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
        badge_ticket = badge_ticket_for_task(task)
        if cycle_issuer is not None and badge_ticket and project:
            badge_provider = TicketBadgeProvider(
                cycle_issuer, self._settings, "dev", badge_ticket, project)
        cycle_cfg = {**cycle_cfg,
                       "vault_index": _dev_vault_index(project_config)}
        executor = ProcessExecutor(
            roles=_tier_s_roles_with_vault(cycle_cfg["roles"],
                                           project_config),
            pool=cycle_cfg["reviewers"],
            badge_provider=badge_provider,
            host_mode=project_isolation(dev_cfg) == "host")
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
        if obj_issuer is not None and project:
            obj_provider = ObjectiveBadgeProvider(
                obj_issuer, self._settings, project,
                ticket=badge_ticket_for_task(task))
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
            incidents=repo,
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
            s_params = dict(
                outcome.params if outcome.params is not None
                else (params or {}))
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
                cost_runs=list(getattr(outcome, "cost_runs", None) or []),
            )
        # The cycle may have patched the ticket params mid-run (TASK-2026-00366):
        # prefer its closing snapshot over the run-start one, or run_task
        # would persist the stale snapshot over the cycle's writes.
        outcome_params = dict(
            outcome.params if outcome.params is not None else (params or {}))
        if outcome.agent_result is not None:
            outcome_params["agent_result"] = outcome.agent_result
        declined = outcome.reason == "agent_blocked"
        return DevExecutionOutput(
            status=TaskStatus.FAILED if declined else TaskStatus.WAITING,
            error=outcome.detail if declined else None,
            coding_tool=CodingTool.TIER_S,
            waiting_reason=(f"{outcome.reason}: {outcome.detail}"
                            if outcome.reason else "waiting"),
            resume_at=self._parse_resume_at(outcome.resume_at),
            last_response_summary=summary,
            steps=[self._step("cycle", "waiting",
                              f"{outcome.reason}: {outcome.detail}")],
            params=outcome_params,
            cost_runs=list(getattr(outcome, "cost_runs", None) or []),
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
            logger.warning("company mandate unreadable; continuing without it",
                           exc_info=True)
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
            pool_raw=list(wiring["pool"]),
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

        Single implementation in :mod:`objectives.service` (Jardal ERP first,
        idempotent per ``source_id``): dev and company-level flows share
        it, including the business hierarchy (``parent_task``/``depends_on``).
        None when unbound: promotions defer to telemetry instead of
        forking Mongo-only tickets.
        """
        return await create_internal_ticket(self._task_service, payload)

    def _ticket_badge_provider(
        self, badge_ticket: str, project: str,
    ) -> TicketBadgeProvider | None:
        """Ticket badge provider; None when unbound or ticketless.

        The badge binds to the business ticket (``TASK-2026-NNNNN``),
        never to the Mongo uuid: the issuer refuses anything else
        (TASK-2026-00214). Callers resolve it with
        :func:`badge_ticket_for_task`.
        """
        issuer = issuer_for_repo(self._repo)
        if issuer is None:
            return None
        if not (badge_ticket or "").strip() or not (project or "").strip():
            return None
        return TicketBadgeProvider(
            issuer, self._settings, "dev", badge_ticket.strip(), project)

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

    async def _claude_role_overlays(
        self,
        provider: TicketBadgeProvider,
        role: str,
        profile_name: str,
    ) -> tuple[dict[str, bytes], str]:
        """Badge + MCP config overlay for one claude tier run.

        The overlay path is fixed under home (the resolve-time argv points
        at it); the placeholder must exist for the fence to bind over it.
        """
        profile = load_role_profile(profile_name)
        if profile.agent != "dev":
            raise RoleProfileError(
                f"tier runs need the dev profile, not {profile_name!r}")
        badge = await provider.mint(role)
        try:
            home = Path(os.path.expanduser("~"))
            ensure_mcp_placeholder(home)
            rendered = claude_config_bytes(provider.mcp_servers(badge))
        except Exception:
            await provider.revoke(badge)
            raise
        return {CLAUDE_MCP_OVERLAY_RELPATH: rendered}, badge

    async def _profile_run_extras(
        self,
        executor: "TierExecutor",
        badge_provider: TicketBadgeProvider,
        workspace: str,
    ) -> tuple[dict[str, bytes] | None, str | None, dict[str, str]]:
        """Badge, overlay and env for one profiled simple-tier run.

        Shared by the code step and the follow-up turns, so a profiled
        tier never runs with role argv but no credentials (review F1).
        """
        if executor.label == "tier_a":
            overlays, badge = await self._claude_role_overlays(
                badge_provider, str(executor.label),
                executor.role_profile or "dev")
            return overlays, badge, {}
        if executor.label == "tier_b":
            badge = await badge_provider.mint(str(executor.label))
            return None, badge, {TIER_CODEX_TOKEN_ENV: badge}
        if executor.label == "tier_c:muse":
            overlays, badge = await self._muse_role_overlays(
                badge_provider, str(executor.label), workspace,
                executor.role_profile or "dev")
            return overlays, badge, {}
        return None, None, {}

    @staticmethod
    def _executor_link(executor: "TierExecutor") -> str | None:
        """Tier C chain link of ``executor`` (``muse``/``dsh``/``claudep``).

        None for tiers without a chain (A/B): only C degrades to another
        link, and that is what policy §25/§32 records per ticket.
        """
        label = str(executor.label or "")
        return label.split(":", 1)[1] if label.startswith("tier_c:") else None

    async def _step_code(
        self, task: dict[str, Any], config: dict[str, Any], tool: CodingTool,
        *, prompt_prefix: str = "", retry: bool = True,
    ) -> CodingStepResult:
        if self._settings.DEV_EXECUTION_MODE == "mock":
            return await self._step_code_once(task, config, tool)
        executor = await self._resolve_tier_executor(tool, config)
        elapsed = 0.0
        responded = tuple(executor.argv) in self._provider_responses
        last_attempt = len(PROVIDER_RETRY_DELAYS) if retry else 0
        for attempt in range(last_attempt + 1):
            result = await self._step_code_once(
                task, config, tool, executor=executor,
                prompt_prefix=prompt_prefix + (
                    PROVIDER_RESUME_PROMPT if attempt else ""),
                responded=responded)
            if result.cost_run is not None:
                elapsed += float(result.cost_run.get("duration_s") or 0)
                result.cost_run["duration_s"] = elapsed
            responded = responded or result.provider_responded
            if (not result.provider_retryable or attempt == last_attempt
                    or await self._task_cancelled(str(task.get("task_id") or ""))):
                if attempt:
                    result.step["provider_attempts"] = attempt + 1
                return result
            delay = PROVIDER_RETRY_DELAYS[attempt]
            logger.warning("Coding provider failed for %s; retry %s in %ss",
                           task.get("task_id"), attempt + 1, delay)
            await asyncio.sleep(delay)
            if await self._task_cancelled(str(task.get("task_id") or "")):
                return result
        raise AssertionError("provider retry exhausted without a result")

    async def _step_code_once(
        self,
        task: dict[str, Any],
        config: dict[str, Any],
        tool: CodingTool,
        *, executor: TierExecutor | None = None, prompt_prefix: str = "",
        responded: bool = False,
    ) -> "CodingStepResult":
        prompt = prompt_prefix + self._build_coding_prompt(task, config)

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

        executor = executor or await self._resolve_tier_executor(tool, config)
        if executor.prompt_via == "argv":
            command = [*executor.argv, prompt]
            stdin_payload: str | None = None
        else:
            command = list(executor.argv)
            stdin_payload = prompt
        overlays: dict[str, bytes] | None = None
        badge: str | None = None
        badge_provider: TicketBadgeProvider | None = None
        extra_env: dict[str, str] = {}
        if executor.label == "tier_c:muse" or (
                executor.role_profile
                and executor.label in ("tier_a", "tier_b")):
            project = str(task.get("project") or "")
            badge_provider = self._ticket_badge_provider(
                badge_ticket_for_task(task) or "", project)
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
                overlays, badge, extra_env = await self._profile_run_extras(
                    executor, badge_provider,
                    executor.cwd or os.getcwd())
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
        started = time.monotonic()
        run_env = {**(executor.env or {}), **extra_env} or None
        try:
            ok, output, err = await self._run_process(
                command, stdin_payload=stdin_payload, timeout=1800,
                cwd=executor.cwd, env=run_env,
                sandbox=executor.sandbox,
                config_argv=list(executor.argv),
                home_file_overlays=overlays,
                extra_read_roots=_dev_vault_roots(config),
            )
        finally:
            if badge is not None and badge_provider is not None:
                await badge_provider.revoke(badge)
        run = cost_mod.normalize_run({
            "engine": str(executor.label),
            "duration_s": time.monotonic() - started,
        })
        raw_output = output
        if executor.label == "tier_c:muse":
            code, output = _reviewer_output(output, err, "muse", 0 if ok else 1)
            ok = code == 0
        if not ok:
            model = ""
            for flag in ("--model", "-m"):
                if flag in executor.argv and executor.argv.index(flag) + 1 < len(executor.argv):
                    model = str(executor.argv[executor.argv.index(flag) + 1])
                    break
            responded = responded or model_responded(raw_output, model)
            detail = self._summarize_failure_output(
                output, err, fallback=f"{executor.label} execution failed")
            return CodingStepResult(
                step=self._step("code", "failed", detail),
                prompt=prompt,
                summary="",
                error=detail,
                executor_link=self._executor_link(executor),
                cost_run=run,
                provider_retryable=transient_provider_error(
                    err + "\n" + output, responded=responded),
                provider_responded=responded,
            )

        self._provider_responses.add(tuple(executor.argv))
        text = output or "ok"
        review_links = extract_review_links(text, self._review_base_url(config))
        summary = self._truncate(text, 500)
        verdict = parse_agent_result(output)
        if verdict is None and "```agent-result" in output:
            return CodingStepResult(
                step=self._step("code", "failed", "invalid agent-result"),
                prompt=prompt, summary="", error="invalid agent-result",
                executor_link=self._executor_link(executor), cost_run=run)
        return CodingStepResult(
            step=self._step("code", "ok", f"Coding CLI finished ({executor.label})"),
            prompt=prompt,
            summary=summary,
            executor_link=self._executor_link(executor),
            review_links=review_links,
            agent_result=(verdict.model_dump(mode="json")
                          if verdict is not None else None),
            cost_run=run,
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
        builder_sandbox = project_isolation(dev_cfg) != "host"

        if tool == CodingTool.TIER_A:
            tier_cfg = tiers_cfg.get("tier_a") or {}
            account = str(tier_cfg.get("account") or "claudej")
            provider_cfg = providers.get("claude", {}) or {}
            argv = self._as_argv(provider_cfg.get("command")) or [account]
            argv = self._ensure_print_flag(argv) + self._model_args(tier_cfg)
            profile_name = (tier_cfg.get("role_profile")
                            or provider_cfg.get("role_profile"))
            if profile_name:
                profile = load_role_profile(str(profile_name))
                mcp_path = (Path(os.path.expanduser("~"))
                            / CLAUDE_MCP_OVERLAY_RELPATH)
                argv = [*argv, *claude_argv_extras(
                    profile, profile_box_tools(profile), str(mcp_path))]
            return TierExecutor(
                argv=argv,
                prompt_via="argv",
                cwd=self._provider_cwd(provider_cfg, repo_cwd),
                env=claude_no_auto_memory_env(
                    self._provider_env(provider_cfg)),
                label="tier_a",
                role_profile=(str(profile_name) if profile_name else None),
                sandbox=builder_sandbox,
            )
        if tool == CodingTool.TIER_B:
            tier_cfg = tiers_cfg.get("tier_b") or {}
            account = str(tier_cfg.get("account") or "codexf")
            provider_cfg = providers.get("codex", {}) or {}
            argv = self._ensure_codex_exec(self._with_model_args(
                [account, "exec", "--ephemeral", "-"],
                self._model_args(tier_cfg)))
            profile_name = (tier_cfg.get("role_profile")
                            or provider_cfg.get("role_profile"))
            if profile_name:
                load_role_profile(str(profile_name))
                override = [
                    "-c",
                    "mcp_servers.openceo-internal.bearer_token_env_var="
                    f'"{TIER_CODEX_TOKEN_ENV}"',
                ]
                argv = ([*argv[:-1], *override, argv[-1]]
                        if argv and argv[-1] == "-" else [*argv, *override])
            return TierExecutor(
                argv=argv,
                prompt_via="stdin",
                cwd=self._provider_cwd(provider_cfg, repo_cwd),
                env=self._provider_env(provider_cfg),
                label="tier_b",
                role_profile=(str(profile_name) if profile_name else None),
                sandbox=builder_sandbox,
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
                argv = argv + self._model_args(link_cfg)
                if "--json" not in argv:
                    argv.append("--json")
                argv = self._with_output_schema(argv)
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
                    sandbox=builder_sandbox,
                )
            argv = self._as_argv(link_cfg.get("command")) or [link]
            if link == "claudep":
                argv = self._ensure_print_flag(argv)
            link_env = {str(k): str(v)
                        for k, v in dict(link_cfg.get("env") or {}).items()}
            return TierExecutor(
                argv=self._with_model_args(
                    argv, self._model_args(link_cfg)),
                prompt_via="stdin" if link == "dsh" else "argv",
                cwd=repo_cwd,
                env=(claude_no_auto_memory_env(link_env)
                     if link == "claudep" else link_env),
                label=f"tier_c:{link}",
                sandbox=builder_sandbox,
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
        previous = (task.get("params") or {}).get("agent_result")
        if isinstance(previous, dict) and previous.get("resume_at"):
            lines.extend(["", "REANUDACIÓN DE ESPERA PROGRAMADA",
                          json.dumps(previous, ensure_ascii=False),
                          "Verifica la condición pendiente y conserva los efectos ya confirmados."])
        if isinstance((task.get("params") or {}).get("dev_checkpoint"), dict):
            lines.extend(["", "REANUDACIÓN DESDE CHECKPOINT",
                          "Tu trabajo previo ya está en el árbol de trabajo (git status y "
                          "git diff) y .openceo/HANDOFF.md lo resume: verifica y completa "
                          "lo que falte, no lo rehagas."])

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
                "Write .openceo/HANDOFF.md (§16.6: HANDOFF <ref>, Contracts introduced/changed, Behavior implemented, Deviations from plan, Discovered work, Gotchas; executor sections ≤300 words).",
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
        knowledge_prefix = knowledge_target_prefix(config)
        if knowledge_prefix:
            lines.extend(["", proposal_prompt_for(
                str(task.get("task_id") or ""),
                project_prefix=knowledge_prefix)])
        dev_cfg = config.get("dev", {}) or {}
        gates, oracle_cmd = self._builder_gate_context(dev_cfg)
        lines.extend(["", builder_environment_prompt(
            gates, oracle_cmd,
            sandboxed=project_isolation(dev_cfg) != "host"),
                      "", AGENT_RESULT_PROMPT])

        vault_index = _dev_vault_index(config)
        if vault_index:
            lines.extend(["", vault_index])

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
        extra_read_roots: Sequence[str | Path] = (),
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
            extra_read_roots=extra_read_roots,
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
        extra_read_roots: Sequence[str | Path] = (),
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
                    home_file_overlays=home_file_overlays,
                    extra_read_roots=extra_read_roots)
            except (IsolationUnavailable, ValueError, OSError) as exc:
                return ProcessRunResult(
                    False, "", f"sandbox refused: {exc}", None)
        process = None
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
            return ProcessRunResult(False, "", "Coding command timeout", None)
        except FileNotFoundError:
            return ProcessRunResult(False, "", f"Command not found: {command[0]}", None)
        except Exception as exc:
            logger.exception("coding command failed unexpectedly")
            return ProcessRunResult(False, "", f"Command execution error: {exc}", None)
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
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
    #: Measured cost of the coding CLI invocation, None when never spawned.
    cost_run: dict[str, Any] | None = None
    provider_retryable: bool = False
    provider_responded: bool = False


@dataclass
class ProcessRunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
