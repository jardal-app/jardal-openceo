"""Tier S run lifecycle: the repo guard is released on every cycle exit.

Regression for the S2 pilot wedge (2026-10-05): the first live Tier S run
parked in ``waiting/transient`` with E11000 on the unique partial
``tier_s_runs.active_repo`` index, because parked/terminal runs never
cleared the guard — wedging the repo for every later run, including the
task's own resume (which always opens a new run). The same crash path
also dropped the resume that ``_park`` had computed.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s.cycle import TierSCycle
from agents.dev.tier_s.models import (
    BUDGETS_DEFAULTS,
    TIMEOUTS_DEFAULTS,
    WAITING_DEFAULTS,
)
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)


class _QuietExecutor:
    """Executor stand-in; records calls, runs nothing (zero spend)."""

    def __init__(self, *, fail_on_run: bool = False) -> None:
        self.calls: list[str] = []
        self.cleaned: list[str] = []
        self.fail_on_run = fail_on_run

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del prompt, workdir, timeout_s
        self.calls.append(role)
        if self.fail_on_run:
            raise RuntimeError("boom")
        raise AssertionError(f"unexpected role {role}")

    async def cleanup(self, role: str) -> None:
        self.cleaned.append(role)


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                   check=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo,
                   check=True)
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _ticket(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "ref": "TASK-2026-99999",
        "title": "release probe",
        "description": "pin the repo-guard release",
        "acceptance": ["tracked.txt untouched"],
        "coding_tool": "tier_s",
        "blocked_by": [],
        "origin": "owner",
    }


def _cfg(roles: dict[str, Any]) -> dict[str, Any]:
    return {
        "roles": roles,
        "budgets": dict(BUDGETS_DEFAULTS),
        "timeouts": dict(TIMEOUTS_DEFAULTS),
        "waiting": dict(WAITING_DEFAULTS),
        "auto_merge": True,
        "base_branch": "main",
        "budget_exclude": [],
    }


class TierSRunReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_parked_run_releases_repo_guard(self) -> None:
        repo = _scratch_repo(self)
        runs = InMemoryRunStore()
        locks = InMemoryLockStore()
        tasks = InMemoryTaskStore()
        fake = _QuietExecutor()

        cycle = TierSCycle(runs, locks, tasks, fake)
        outcome = await cycle.run(_ticket("task-park"), repo, _cfg({}),
                                  gate_specs=[])

        self.assertEqual(outcome.status, "waiting")
        self.assertEqual(outcome.reason, "config")
        self.assertEqual(fake.calls, [])
        run = runs.runs[outcome.run_id]
        self.assertEqual(run["status"], "waiting")
        self.assertIsNone(run.get("active_repo"))
        self.assertEqual(tasks.statuses["task-park"], "waiting")
        self.assertEqual(locks.holders, {})

    async def test_crash_releases_guard_and_keeps_resume(self) -> None:
        repo = _scratch_repo(self)
        runs = InMemoryRunStore()
        locks = InMemoryLockStore()
        tasks = InMemoryTaskStore()
        fake = _QuietExecutor(fail_on_run=True)

        roles = {
            "planner": {"account": "claudej", "model": "opus",
                        "command": ["true"]},
            "plan_reviewer": {"account": "codexj", "model": "gpt-6.1-sol",
                              "command": ["true"]},
            "builder": {"account": "muse",
                        "model": "muse-spark-1.3-contributor",
                        "command": ["true"]},
        }
        cycle = TierSCycle(runs, locks, tasks, fake)
        outcome = await cycle.run(_ticket("task-crash"), repo, _cfg(roles),
                                  gate_specs=[])

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "transient")
        self.assertIn("RuntimeError: boom", outcome.detail)
        self.assertIsNotNone(outcome.resume_at)
        run = runs.runs[outcome.run_id]
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(run.get("active_repo"))
        self.assertEqual(tasks.statuses["task-crash"], "waiting")
        self.assertIsNotNone(
            tasks.patches["task-crash"].get("resume_at"))
        self.assertEqual(locks.holders, {})


if __name__ == "__main__":
    unittest.main()
