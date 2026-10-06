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
from .routing import ROUTING_MISSING_REASON, TIER_C_CHAIN, resolve_coding_tool, select_tier_c_link
from .tier_s.cycle import AllowAllQuota, TierSCycle
from .tier_s.gates import baseline_blocked, gates_failed, handoff_gates_section, load_gates, run_gates
from .tier_s.inbox import InMemoryInboxStore, MongoInboxStore
from .tier_s.knowledge import detect_prompt_lang, with_preamble
from .tier_s.live import ProcessExecutor, RateMonitorQuota
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    MongoObjectiveStore,
    MongoObjectiveTaskLister,
    ObjectiveEnv,
    TicketCloseContext,
    checkout_and_update_objective_branch,
    on_ticket_closed,
)
from objectives.service import create_internal_ticket, erp_for_task_service
from .tier_s.review import InMemoryReviewStore, InMemorySlotStore, MongoReviewStore, MongoSlotStore, parse_pool
from .tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
    MongoLockStore,
    MongoRunStore,
    MongoTaskStore,
)
from .tmux_runtime import TmuxRuntime
from config_loader import ConfigLoader
from rate_monitor import RateMonitor
from settings import Settings
from state import CodingTier, CodingTool, TaskStatus

from .tier_s_contract import child_env
from .tier_selector import TierSelector

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
    coding_tier: CodingTier | None = None
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
            "coding_tier": self.coding_tier.value if self.coding_tier else None,
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


@dataclass
class _SimpleBranch:
    """Work branch owned by one simple-tier ticket (F5, §10/§12)."""

    repo: Path
    branch: str
    base: str
    base_sha: str
    ref: str


class DevSessionManager:
    """Runs deterministic step-based Dev Agent sessions."""

    def __init__(self, settings: Settings, config_loader: ConfigLoader,
                 rate_monitor: RateMonitor | None = None, repo: Any | None = None) -> None:
        self._settings = settings
        self._config_loader = config_loader
        self._tier_selector = TierSelector(settings)
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
        dev_cfg = project_config.get("dev", {})
        qa_cfg = dev_cfg.get("qa", {})

        # Single routing reader (Tier S design §3): the top-level coding_tool,
        # with legacy fallbacks during the deprecation transition.
        decision = resolve_coding_tool(task, project_config)
        if decision.deprecated:
            logger.warning(
                "deprecated routing source=%s task_id=%s (%s)",
                decision.source, task.get("task_id"), decision.detail,
            )
        if decision.tool is None:
            # Not dispatched: no tier selection (it may call an LLM) and no
            # fallback. The ERP comment for the wait is posted by task_service.
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{ROUTING_MISSING_REASON}: {decision.detail}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )

        # Invalid project config parks every ticket, whatever the tier.
        config_errors = self._config_loader.validate_project(task.get("project"))
        if config_errors:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{CONFIG_INVALID_REASON}: {'; '.join(config_errors)}",
                params=task.get("params") if isinstance(task.get("params"), dict) else None,
            )

        # Respect ticket-level tier overrides (e.g. manual classification)
        task_params = task.get("params") if isinstance(task.get("params"), dict) else {}
        task_tier = task.get("coding_tier") or task_params.get("coding_tier")

        if task_tier:
            try:
                tier = CodingTier(task_tier)
            except ValueError:
                logger.warning(
                    "invalid coding_tier=%r task_id=%s; falling back to selector",
                    task_tier, task.get("task_id"),
                )
                tier = await self._tier_selector.select(
                    str(dev_cfg.get("coding_tier", "auto")),
                    task.get("title", ""),
                    task.get("description", ""),
                )
        else:
            tier = await self._tier_selector.select(
                str(dev_cfg.get("coding_tier", "auto")),
                task.get("title", ""),
                task.get("description", ""),
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
                output = await self._run_tier_s(task, project_config, tier, qa_cfg)
            else:
                output = await self._run_simple_tool(task, project_config, decision.tool, tier, qa_cfg)
        except Exception:
            if outer_lock:
                assert repo is not None
                await self._repo.release_repo_lock(repo, holder)
            raise
        if outer_lock:
            assert repo is not None
            if output.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ESCALATED):
                await self._repo.release_repo_lock(repo, holder)
            elif output.status == TaskStatus.WAITING:
                await self._repo.touch_repo_lock(repo, holder)
        return output

    async def _run_simple_tool(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tool: CodingTool,
        tier: CodingTier,
        qa_cfg: dict[str, Any],
    ) -> DevExecutionOutput:
        """Simple tiers (design §10, F5): one ticket, one session, gates.

        With a resolvable repo the ticket runs the full flow — work branch,
        baseline, one executor session, orchestrator gates with retries,
        size brake with escalation to ``tier_s``, mandatory handoff, squash
        and merge. Without a repo (synthetic configs, unit tests) it keeps
        the single code step. ``dev.qa`` is retired here: gates replaced it.
        """
        del qa_cfg
        output = DevExecutionOutput(
            status=TaskStatus.IN_PROGRESS,
            coding_tier=tier,
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
        return _SimpleBranch(repo=repo, branch=branch, base=base,
                             base_sha=base_sha, ref=ref)

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

        try:
            specs = load_gates(dev_cfg)
        except ValueError as exc:
            self._drop_simple_branch(branch)
            output.status = TaskStatus.WAITING
            output.waiting_reason = f"config: gates: {exc}"
            return output
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
            if attempt >= retries:
                names = ", ".join(item.name for item in failed)
                return await self._escalate_simple_to_tier_s(
                    task, branch, f"gates failed after {retries} retries: {names}")
            tail = "\n\n".join(
                f"[{item.name}] {item.status}: {item.detail}\n{item.output_tail}"
                for item in failed)
            await self._simple_followup(
                tool, project_config, repo,
                f"Gates failed:\n{tail[:6000]}\nFix the code (no commits), "
                "keep HANDOFF.md truthful.")
            attempt += 1

        files_n, lines_n = self._simple_diff_sizes(branch, exclude)
        files_budget = int(budgets.get("files_per_ticket", 8) or 8)
        lines_budget = int(budgets.get("lines_per_ticket", 400) or 400)
        brake = int(budgets.get("hard_brake_factor", 2) or 2) * lines_budget
        if files_n > files_budget or lines_n > brake:
            return await self._escalate_simple_to_tier_s(
                task, branch,
                f"size {files_n} files/{lines_n} lines over budget "
                f"({files_budget}/{brake})")

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
                "Do not touch code.")
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

        trigger_note = ""
        if task.get("objective_id"):
            trigger_note = await self._simple_objective_trigger(
                task, project_config, branch, files, sha, completed_handoff)
        output.status = TaskStatus.COMPLETED
        output.modified_files = files
        output.last_response_summary = (
            f"{tool.value} closed: {branch_note}, {sha[:12]}, "
            f"{len(files)} files.{trigger_note}")
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
        await self._run_process(
            command, stdin_payload=stdin_payload, timeout=1800,
            cwd=str(repo), env=executor.env or None)

    async def _escalate_simple_to_tier_s(
        self,
        task: dict[str, Any],
        branch: _SimpleBranch,
        cause: str,
    ) -> DevExecutionOutput:
        """Escalate an oversized simple ticket (§3.4): reset, retier, requeue.

        Never converts in place: the branch is reset and dropped,
        ``coding_tool`` becomes ``tier_s``, and the ticket requeues so the
        Tier S cycle picks it up fresh.
        """
        self._simple_git(branch.repo, "reset", "--hard", "-q", branch.base_sha)
        self._simple_git(branch.repo, "clean", "-fdq")
        self._drop_simple_branch(branch)
        task_id = str(task.get("task_id") or "")
        if self._repo is not None and task_id:
            try:
                await self._repo.patch_task(task_id, {"coding_tool": "tier_s"})
            except Exception:
                logger.warning("escalation retier failed for %s", task_id,
                               exc_info=True)
        notice = (f"Escalated {self._simple_ref(task)} to tier_s ({cause}). "
                  "Simple-tier work was reset; the Tier S cycle starts fresh.")
        if self._task_service is not None and task_id:
            try:
                await self._task_service.comment_on_ticket(task_id, notice)
            except Exception:
                logger.warning("escalation comment failed for %s", task_id,
                               exc_info=True)
        return DevExecutionOutput(
            status=TaskStatus.WAITING,
            coding_tool=CodingTool.TIER_S,
            waiting_reason=f"escalated:tier_s:{cause}",
            resume_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            last_response_summary=notice,
            steps=[self._step("escalate", "ok", notice)],
            params=task.get("params") if isinstance(task.get("params"), dict) else None,
        )

    @staticmethod
    def _executor_roles_with_planner(
        project_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Tier S roles plus the single global planner role (TASK-2026-00112).

        The planner role lives in the global ``objective_planner`` block,
        outside ``dev.tier_s``; the live executor resolves every engine
        role (including ``objective_planner``) from one roles mapping.
        """
        dev_cfg = project_config.get("dev", {}) or {}
        tier_s = dev_cfg.get("tier_s", {}) or {}
        roles = dict(tier_s.get("roles", {}) or {})
        planner_role = (project_config.get("objective_planner", {}) or {}
                        ).get("role")
        if isinstance(planner_role, dict) \
                and planner_role.get("active", True):
            roles["objective_planner"] = planner_role
        return roles

    def _objective_quota(self) -> Any:
        return (RateMonitorQuota(self._rate_monitor)
                if self._rate_monitor is not None else AllowAllQuota())

    async def _simple_objective_trigger(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        branch: _SimpleBranch,
        files: list[str],
        sha: str,
        handoff: str,
    ) -> str:
        """F6 triggers + capsule after an objective simple ticket closes."""
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
                            "objective trigger comment failed for %s",
                            task_id, exc_info=True)

            tier_s = (project_config.get("dev", {}) or {}).get(
                "tier_s", {}) or {}
            env = self._objective_env_for(
                project_config, branch.repo,
                ProcessExecutor(
                    roles=self._executor_roles_with_planner(project_config),
                    pool=tier_s.get("reviewers", []) or []),
                self._objective_quota(), notify)
            await on_ticket_closed(
                env, task,
                TicketCloseContext(
                    task_id=task_id,
                    title=str(task.get("title", "")),
                    tier=str(task.get("coding_tool") or ""),
                    sha=sha, files=list(files),
                    handoff_excerpt=handoff[:1500]))
            return " Objective triggers evaluated."
        except Exception as exc:
            return f" Objective trigger failed: {exc}"

    async def _run_tier_s(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any],
        tier: CodingTier,
        qa_cfg: dict[str, Any],
    ) -> DevExecutionOutput:
        """Tier S dispatch: full engine cycle with live CLI executors.

        The cycle patches the task directly (status/waiting/resume); this
        method mirrors its outcome into :class:`DevExecutionOutput` so the
        graph and ``run_task`` converge on the same values. No fallback to
        another tier, ever.
        """
        del tier, qa_cfg
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
        tier_s = dev_cfg.get("tier_s", {}) or {}
        cycle_cfg = TierSCycle.config_from_project(project_config)
        try:
            gate_specs = load_gates(dev_cfg)
        except ValueError as exc:
            return DevExecutionOutput(
                status=TaskStatus.WAITING,
                waiting_reason=f"{CONFIG_INVALID_REASON}: gates: {exc}",
                params=params,
            )
        executor = ProcessExecutor(
            roles=self._executor_roles_with_planner(project_config),
            pool=tier_s.get("reviewers", []) or [])
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
        obj_env = self._objective_env_for(
            project_config, Path(repo_path), executor, quota, notify)
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
            return DevExecutionOutput(
                status=(TaskStatus.CONVERTED if converted
                        else TaskStatus.COMPLETED),
                coding_tier=None,
                coding_tool=CodingTool.TIER_S,
                last_response_summary=(
                    f"Tier S cycle {detail} (run {outcome.run_id}). {summary}"
                ).strip(),
                steps=[self._step("cycle", "ok", f"Tier S {detail}"),
                       *(self._step("notice", "ok", note) for note in notices[-5:])],
                params=params,
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
        tier_s = dev_cfg.get("tier_s", {}) or {}
        repo = self._repo
        repos = project_config.get("project", {}).get("repos", []) or []
        base = str((repos[0].get("branch_base") if repos else None) or "main")

        async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
            if repo is not None:
                await repo.patch_task(task_id, updates)

        # Global block wins; dev.tier_s.objectives stays as legacy fallback.
        objectives_cfg = {**(tier_s.get("objectives", {}) or {}),
                          **(project_config.get("objectives", {}) or {})}
        objectives_cfg["child_autorun"] = bool(
            self._settings.TIER_S_OBJECTIVE_CHILD_AUTORUN)

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
            roles=dict(tier_s.get("roles", {}) or {}),
            pool=list(parse_pool(tier_s.get("reviewers", []) or [])),
            review_cfg=dict(tier_s.get("review", {}) or {}),
            objectives_cfg=objectives_cfg,
            planner_cfg=dict(
                project_config.get("objective_planner", {}) or {}),
            erp=(erp_for_task_service(self._task_service)
                 if self._task_service is not None else None),
            timeouts=dict(tier_s.get("timeouts_minutes", {}) or {}),
            fix_tiers=dict(tier_s.get("fix_ticket_tiers", {}) or {}),
            inbox_rule=dict((tier_s.get("inbox", {}) or {}).get(
                "promotion", {}) or {}),
            auto_merge=bool(dev_cfg.get("auto_merge", True)),
            base_branch=base,
            vault=dict(dev_cfg.get("vault_project") or {})
            if dev_cfg.get("vault_project") else None,
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
        ok, output, err = await self._run_process(
            command, stdin_payload=stdin_payload, timeout=1800,
            cwd=executor.cwd, env=executor.env or None,
        )

        if not ok:
            detail = self._summarize_failure_output(
                output, err, fallback=f"{executor.label} execution failed")
            return CodingStepResult(
                step=self._step("code", "failed", detail),
                prompt=prompt,
                summary="",
                error=detail,
            )

        text = output or "ok"
        review_links = extract_review_links(text, self._review_base_url(config))
        summary = self._truncate(text, 500)
        return CodingStepResult(
            step=self._step("code", "ok", f"Coding CLI finished ({executor.label})"),
            prompt=prompt,
            summary=summary,
            review_links=review_links,
        )

    async def _step_update_files(self, task: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        """Legacy CONTEXT.md writer (kept; tier flows write HANDOFF.md, F7)."""
        try:
            project = task.get("project", "default")
            context_path = self._config_loader.context_path(project)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            context_line = (
                f"\n## {now} - {task.get('task_id')}\n"
                f"- Title: {task.get('title')}\n"
                f"- Summary: {self._truncate(task.get('description', ''), 300)}\n"
            )
            self._append_text(context_path, context_line)

            return self._step("update_files", "ok", "CONTEXT.md updated")
        except Exception as exc:
            return self._step("update_files", "skipped", f"CONTEXT.md unavailable: {exc}")

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
                argv=self._with_model_args(
                    [account, "exec", "--ephemeral", "-"],
                    self._model_args(tier_cfg)),
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
                return TierExecutor(
                    argv=argv + self._model_args(link_cfg), prompt_via="argv",
                    cwd=self._provider_cwd(provider_cfg, repo_cwd),
                    env=self._provider_env(provider_cfg),
                    label="tier_c:muse",
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
    ) -> tuple[bool, str, str]:
        result = await self._run_process_detailed(
            command,
            stdin_payload=stdin_payload,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        return result.ok, result.stdout, result.stderr

    async def _run_process_detailed(
        self,
        command: list[str],
        stdin_payload: str | None = None,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> "ProcessRunResult":
        try:
            stdin_target = (
                asyncio.subprocess.PIPE
                if stdin_payload is not None
                else asyncio.subprocess.DEVNULL
            )
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=stdin_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=child_env(os.environ, env),
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

        output = (stdout or b"").decode("utf-8", errors="replace").strip()
        err = (stderr or b"").decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            return ProcessRunResult(False, output, err, process.returncode)

        return ProcessRunResult(True, output or "ok", err, process.returncode)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

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


@dataclass
class ProcessRunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
