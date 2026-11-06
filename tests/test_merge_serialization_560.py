"""Cierres concurrentes: merge serializado, stderr conservado y checkpoint (#560).

Diagnóstico del mecánico (MEC-20261106T004427-E959): los cierres
squash+merge de tickets concurrentes comparten un solo checkout y competían
por `checkout <base>` + `merge` sin ningún cerrojo. El perdedor aparcaba
como needs_owner aunque fuera transitorio, la razón descartaba el stderr de
git y esa rama no guardaba checkpoint, así que el resume reconstruía desde
cero y la rama tier/* quedaba huérfana. Contrato:

- la ventana checkout-base + merge + borrado corre bajo un flock entre
  procesos (`.runtime/merge.lock`) en ambos motores;
- la razón de cannot-checkout conserva el stderr de git (truncado);
- los aparcados por fallo de merge guardan checkpoint (o registro del
  candidato) y el fallo de checkout es transitorio con resume_at, no
  needs_owner: el resume reutiliza la rama commiteada y no deja huérfanas.

Git real y ejecutores simulados; sin modelos, red ni Docker.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev import merge_lock as merge_lock_mod
from agents.dev.merge_lock import MergeLockBusy, merge_lock_path, merge_locked
from agents.dev.session import DevExecutionOutput, DevSessionManager, _SimpleBranch
from agents.dev.tier_s.cycle import TierSCycle, _Ctx
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from state import CodingTool, TaskStatus


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("before\n")
    (tmp_path / ".gitignore").write_text(".openceo/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def manager() -> DevSessionManager:
    settings = SimpleNamespace(DEV_EXECUTION_MODE="cli")
    return DevSessionManager(settings, MagicMock(), repo=None)


def dev_project(repo, **dev) -> dict:
    return {"project": {"repos": [{"local_path": str(repo)}]}, "dev": dev}


def _built_branch(repo, name="tier/tier_b-560-wait"):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", name)
    (repo / "tracked.txt").write_text("after\n")
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text("resumen")
    return _SimpleBranch(repo, name, "main", base, "t560")


def _off_task(task_id: str = "t-560", **extra: Any) -> dict:
    task = {"task_id": task_id, "title": "probe", "review": "off",
            "params": {}}
    task.update(extra)
    return task


def _tier_s_ctx(repo, *, branch: str, base_sha: str, run_id: str = "run",
                task: dict | None = None) -> _Ctx:
    return _Ctx(ticket=dict(task) if task is not None
                else {"task_id": "t", "title": "Verify"},
                repo=repo, ref="t", run_id=run_id, branch=branch,
                base_branch="main", base_sha=base_sha, budgets={},
                timeouts={}, waiting_cfg={}, auto_merge=True,
                budget_exclude=[], gate_specs=[], roles={})


def _tier_s_cycle(runs=None, tasks=None):
    runs = runs if runs is not None else InMemoryRunStore()
    tasks = tasks if tasks is not None else InMemoryTaskStore()
    return TierSCycle(runs, InMemoryLockStore(), tasks, MagicMock())


# -- the merge lock ------------------------------------------------------------


def test_merge_lock_path_lives_under_runtime(tmp_path):
    assert merge_lock_path(tmp_path) == tmp_path / ".runtime" / "merge.lock"


def test_merge_lock_serializes_concurrent_holders(tmp_path):
    value = 0

    def bump():
        nonlocal value
        for _ in range(30):
            with merge_locked(tmp_path):
                seen = value
                time.sleep(0.001)
                value = seen + 1

    threads = [threading.Thread(target=bump) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert value == 60


def test_merge_lock_busy_after_timeout(tmp_path):
    with merge_locked(tmp_path):
        with pytest.raises(MergeLockBusy):
            with merge_locked(tmp_path, timeout=0):
                pass
        outcome: dict[str, bool] = {}

        def worker():
            try:
                with merge_locked(tmp_path, timeout=0.2):
                    outcome["held"] = True
            except MergeLockBusy:
                outcome["busy"] = True

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(10)
        assert outcome == {"busy": True}


# -- simple tiers --------------------------------------------------------------


@run_async
async def test_simple_checkout_failure_parks_transient_with_stderr(repo):
    dev = manager()
    branch = _built_branch(repo)
    git(repo, "branch", "-D", "main")
    output = await dev._finish_simple_tool(
        _off_task(), dev_project(repo, gates=[{"name": "t", "cmd": "true"}]),
        CodingTool.TIER_B, DevExecutionOutput(status=TaskStatus.IN_PROGRESS),
        branch)
    assert output.status == TaskStatus.WAITING
    assert output.waiting_reason.startswith(
        "transient: merge_conflict: cannot checkout main:")
    assert "did not match" in output.waiting_reason  # git stderr, kept
    assert f"(branch kept: {branch.branch})" in output.waiting_reason
    assert output.resume_at is not None
    checkpoint = output.params["dev_checkpoint"]
    assert checkpoint["branch"] == branch.branch
    assert checkpoint["build_done"] is True
    assert len(checkpoint["head_sha"]) == 40
    assert git(repo, "branch", "--list", branch.branch).lstrip("* ") == branch.branch
    assert git(repo, "branch", "--show-current") == branch.branch


@run_async
async def test_simple_close_resume_reuses_the_committed_branch(repo):
    dev = manager()
    branch = _built_branch(repo)
    base_sha = branch.base_sha
    git(repo, "branch", "-D", "main")
    parked = await dev._finish_simple_tool(
        _off_task(), dev_project(repo, gates=[{"name": "t", "cmd": "true"}]),
        CodingTool.TIER_B, DevExecutionOutput(status=TaskStatus.IN_PROGRESS),
        branch)
    assert parked.status == TaskStatus.WAITING
    # The transient clears: the base is back, the dispatcher resumes.
    git(repo, "branch", "main", base_sha)
    task = _off_task("t-560-resume", params=dict(parked.params))
    builder = AsyncMock(side_effect=AssertionError("builder re-executed"))
    dev._step_code = builder
    out = await dev._run_simple_tool(
        task, dev_project(repo, gates=[{"name": "t", "cmd": "true"}]),
        CodingTool.TIER_B, {})
    builder.assert_not_awaited()
    assert out.status == TaskStatus.COMPLETED
    assert any("skipping the build turn" in step.get("detail", "")
               for step in out.steps)
    assert (repo / "tracked.txt").read_text() == "after\n"
    assert git(repo, "branch", "--list", "tier/*") == ""


@run_async
async def test_simple_merge_conflict_stays_needs_owner_with_checkpoint(repo):
    dev = manager()
    base = git(repo, "rev-parse", "HEAD")
    name = "tier/tier_b-560-conflict"
    git(repo, "checkout", "-qb", name)
    (repo / "tracked.txt").write_text("branch\n")
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text("resumen")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-qm", "wip")
    git(repo, "checkout", "-q", "main")
    (repo / "tracked.txt").write_text("main\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-qm", "other ticket")
    git(repo, "checkout", "-q", name)
    branch = _SimpleBranch(repo, name, "main", base, "t560")
    output = await dev._finish_simple_tool(
        _off_task(), dev_project(repo, gates=[{"name": "t", "cmd": "true"}]),
        CodingTool.TIER_B, DevExecutionOutput(status=TaskStatus.IN_PROGRESS),
        branch)
    assert output.status == TaskStatus.WAITING
    assert output.waiting_reason.startswith("needs_owner: merge_conflict:")
    assert output.resume_at is None
    assert output.params["dev_checkpoint"]["branch"] == branch.branch
    assert git(repo, "branch", "--list", branch.branch) == branch.branch


def test_simple_close_window_runs_under_the_merge_lock(repo):
    import agents.dev.session as session_mod

    entered: list[str] = []
    real = merge_lock_mod.merge_locked

    @contextmanager
    def spy(repo_path, **kwargs):
        entered.append(str(repo_path))
        with real(repo_path, **kwargs):
            yield

    dev = manager()
    branch = _built_branch(repo, "tier/tier_b-560-lock")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "squash")
    with patch.object(session_mod, "merge_locked", spy):
        merged, kind, detail = dev._close_simple_merge(branch)
    assert (merged, kind, detail) == (True, "", "")
    assert entered == [str(repo)]
    assert (repo / "tracked.txt").read_text() == "after\n"


def test_simple_concurrent_close_windows_serialize(repo):
    dev = manager()
    base = git(repo, "rev-parse", "HEAD")
    branches = []
    for name, fname in (("tier/tier_b-560-a1", "a.txt"),
                        ("tier/tier_b-560-b2", "b.txt")):
        git(repo, "checkout", "-qb", name, "main")
        (repo / fname).write_text("work\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "squash")
        branches.append(_SimpleBranch(repo, name, "main", base, "t560"))
    git(repo, "checkout", "-q", "main")
    barrier = threading.Barrier(2)
    results: dict[int, tuple] = {}
    errors: dict[int, BaseException] = {}

    def close(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            results[index] = dev._close_simple_merge(branches[index])
        except BaseException as exc:  # noqa: BLE001 - collected, then asserted
            errors[index] = exc

    threads = [threading.Thread(target=close, args=(index,))
               for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors
    assert results[0][0] and results[1][0]
    assert (repo / "a.txt").read_text() == "work\n"
    assert (repo / "b.txt").read_text() == "work\n"
    assert git(repo, "branch", "--list", "tier/*") == ""


# -- Tier S --------------------------------------------------------------------


def _tier_s_branch(repo, name="tier-s/m1"):
    from test_tier_s_final import HANDOFF_MD

    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", name)
    (repo / "tracked.txt").write_text("after\n")
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text(HANDOFF_MD)
    return base


def test_s_merge_checkout_failure_reports_stderr(repo):
    base = _tier_s_branch(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "squash")
    git(repo, "branch", "-D", "main")
    cycle = _tier_s_cycle()
    ctx = _tier_s_ctx(repo, branch="tier-s/m1", base_sha=base)
    merged, kind, detail = cycle._merge(ctx)
    assert merged is False
    assert kind == "checkout"
    assert detail.startswith("cannot checkout main:")
    assert "did not match" in detail  # git stderr, kept


def test_s_merge_conflict_reports_conflict(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "tier-s/m2")
    (repo / "tracked.txt").write_text("branch\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "squash")
    git(repo, "checkout", "-q", "main")
    (repo / "tracked.txt").write_text("main\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "other ticket")
    git(repo, "checkout", "-q", "tier-s/m2")
    cycle = _tier_s_cycle()
    ctx = _tier_s_ctx(repo, branch="tier-s/m2", base_sha=base)
    merged, kind, detail = cycle._merge(ctx)
    assert (merged, kind) == (False, "conflict")
    assert "conflict" in detail


@run_async
async def test_s_close_merges_under_the_merge_lock(repo):
    import agents.dev.tier_s.cycle as cycle_mod

    entered: list[str] = []
    real = merge_lock_mod.merge_locked

    @contextmanager
    def spy(repo_path, **kwargs):
        entered.append(str(repo_path))
        with real(repo_path, **kwargs):
            yield

    base = _tier_s_branch(repo, "tier-s/lock")
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    cycle = _tier_s_cycle(runs, tasks)
    cycle._review_rounds = AsyncMock(return_value=None)
    ctx = _tier_s_ctx(repo, branch="tier-s/lock", base_sha=base)
    with patch.object(cycle_mod, "merge_locked", spy):
        result = await cycle._close(ctx)
    assert result.status == "completed"
    assert entered == [str(repo)]
    assert git(repo, "show", "main:tracked.txt") == "after"
    assert git(repo, "branch", "--list", "tier-s/*") == ""


@run_async
async def test_s_close_checkout_failure_parks_transient_with_checkpoint(repo):
    base = _tier_s_branch(repo, "tier-s/wait")
    git(repo, "branch", "-D", "main")
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    cycle = _tier_s_cycle(runs, tasks)
    cycle._review_rounds = AsyncMock(return_value=None)
    ctx = _tier_s_ctx(repo, branch="tier-s/wait", base_sha=base)
    outcome = await cycle._close(ctx)
    assert outcome.status == "waiting"
    assert outcome.reason == "transient"
    assert outcome.resume_at
    assert "merge_conflict: cannot checkout main:" in outcome.detail
    assert "did not match" in outcome.detail  # git stderr, kept
    cycle._review_rounds.assert_awaited_once()
    saved = await runs.resumable_run("t")
    assert saved is not None
    assert saved["checkpoint"]["phase"] == "close"
    assert saved["checkpoint"]["branch"] == "tier-s/wait"
    assert len(saved["checkpoint"]["head_sha"]) == 40
    assert (await cycle._resume_point("t", repo))["run_id"] == "run"
    assert tasks.patches["t"]["waiting_reason"].startswith(
        "transient: merge_conflict:")
    assert isinstance(tasks.patches["t"]["resume_at"], datetime)
    assert git(repo, "branch", "--list", "tier-s/wait").lstrip("* ") == "tier-s/wait"


@run_async
async def test_s_close_resume_retries_only_the_merge(repo):
    base = _tier_s_branch(repo, "tier-s/resume")
    git(repo, "branch", "-D", "main")
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    cycle = _tier_s_cycle(runs, tasks)
    cycle._review_rounds = AsyncMock(return_value=None)
    ctx = _tier_s_ctx(repo, branch="tier-s/resume", base_sha=base)
    parked = await cycle._close(ctx)
    assert parked.status == "waiting" and parked.reason == "transient"
    # The transient clears: the base is back, the dispatcher resumes.
    git(repo, "branch", "main", base)
    resume = await cycle._resume_point("t", repo)
    assert resume is not None
    resumed = _tier_s_ctx(repo, branch=resume["checkpoint"]["branch"],
                           base_sha="", run_id="run")
    cycle._restore_checkpoint(resumed, resume)
    assert resumed.resume_phase == "close"
    followup = _tier_s_cycle(runs, tasks)
    followup._review_rounds = AsyncMock(return_value=None)
    result = await followup._resume_review(resumed)
    assert result.status == "completed"
    followup._review_rounds.assert_not_awaited()
    assert git(repo, "show", "main:tracked.txt") == "after"
    assert git(repo, "branch", "--list", "tier-s/*") == ""
