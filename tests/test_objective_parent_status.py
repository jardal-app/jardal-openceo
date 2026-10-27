"""Objective parent lifecycle: conversion never completes the parent (§9.7).

Regression (2026-10-05, TASK-2026-00096 S2): ``convert_ticket`` patched
the source ticket ``completed``, so the observer pushed business
Completed while the objective's children were still queued. The parent
must go to ``converted`` (terminal, never pushed) and reach
``completed`` only when the objective closes.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import DevSessionManager
from objectives import engine as objectives_mod
from agents.dev.tier_s.cycle import CycleOutcome
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _finish_objective,
    convert_ticket,
)
from settings import Settings
from state import TaskStatus
from task_service import TaskService
from tools.jardal_erp_tickets import map_status


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


def _objective_env(repo: Path, objectives: Any, tasks: Any,
                   patches: dict[str, dict[str, Any]],
                   notices: list[tuple[str, str]]) -> ObjectiveEnv:
    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(updates)

    async def notify(ref: str, text: str) -> None:
        notices.append((ref, text))

    return ObjectiveEnv(
        objectives=objectives,
        tasks=tasks,
        create_ticket=AsyncMock(return_value=None),
        patch_task=patch_task,
        executor=MagicMock(),
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=notify,
        repo=repo,
    )


class ObjectiveParentStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_converted_status_is_never_pushed(self) -> None:
        self.assertIsNone(map_status("converted"))
        self.assertIsNone(map_status("CONVERTED"))

    async def test_convert_moves_parent_to_converted(self) -> None:
        repo = _scratch_repo(self)
        store = InMemoryObjectiveStore()
        patches: dict[str, dict[str, Any]] = {}
        notices: list[tuple[str, str]] = []
        env = _objective_env(repo, store, InMemoryTaskLister(), patches,
                             notices)
        ticket = {
            "task_id": "task-parent",
            "ref": "TASK-2026-99999",
            "title": "too big",
            "description": "needs an objective",
            "acceptance": ["done"],
            "project": "proj",
        }
        with patch.object(objectives_mod, "plan_objective",
                          new=AsyncMock(return_value={})):
            objective = await convert_ticket(
                env, ticket, "OBJECTIVE_NEEDED", "sizing")

        patched = patches["task-parent"]
        self.assertEqual(patched["status"], "converted")
        self.assertNotIn("completed_at", patched)
        self.assertEqual(patched["objective_id"],
                         objective["objective_id"])
        self.assertTrue(notices)
        stored = await store.get_objective(objective["objective_id"])
        assert stored is not None
        self.assertEqual(stored["conversion_source_task_id"], "task-parent")

    async def test_session_maps_converted_outcome(self) -> None:
        repo = _scratch_repo(self)
        manager = DevSessionManager(Settings(), MagicMock())
        outcome = CycleOutcome(status="completed", run_id="ts-x",
                               reason="converted", detail="OBJ-1",
                               branch="tier-s/x")
        project_config = {
            "project": {"repos": [{"local_path": str(repo)}]},
            "dev": {},
        }
        task = {"task_id": "task-parent", "ref": "TASK-1", "title": "t",
                "project": "p"}
        with patch("agents.dev.session.TierSCycle") as cycle_cls:
            cycle_cls.return_value.run = AsyncMock(return_value=outcome)
            output = await manager._run_tier_s(task, project_config, {})

        self.assertEqual(output.status, TaskStatus.CONVERTED)
        self.assertIn("OBJ-1", output.last_response_summary or "")

    async def test_run_task_skips_converted_without_force(self) -> None:
        repo = MagicMock()
        repo.get_task = AsyncMock(return_value={
            "task_id": "task-parent", "status": "converted",
            "project": "p"})
        graph = MagicMock()
        graph.execute = AsyncMock()
        service = TaskService(Settings(), repo, graph, MagicMock())

        result = await service.run_task("task-parent")

        assert result is not None
        self.assertEqual(result["status"], "converted")
        graph.execute.assert_not_called()

    async def test_observer_skips_converted_push(self) -> None:
        service = TaskService(Settings(), MagicMock(), MagicMock(),
                              MagicMock())
        service._business = MagicMock()
        service._business.is_enabled = True
        task = {
            "task_id": "task-parent",
            "status": "converted",
            "params": {"_mirrors": {"erpnext": {
                "name": "TASK-1", "status_pushed": "in_progress"}}},
        }
        await service.observe_task_status(task)
        self.assertTrue(service._jardal_erp_queue.empty())

    async def test_finish_completes_converted_parent(self) -> None:
        repo = _scratch_repo(self)
        store = InMemoryObjectiveStore()
        objective = await store.create_objective({
            "objective_id": "OBJ-1",
            "project": "p",
            "status": "active",
            "conversion_source_task_id": "task-parent",
        })
        lister = InMemoryTaskLister({
            "task-parent": {"task_id": "task-parent", "status": "converted"},
        })
        patches: dict[str, dict[str, Any]] = {}
        notices: list[tuple[str, str]] = []
        env = _objective_env(repo, store, lister, patches, notices)

        result = await _finish_objective(env, objective)

        self.assertEqual(result["action"], "closed")
        patched = patches["task-parent"]
        self.assertEqual(patched["status"], "completed")
        self.assertIn("completed_at", patched)

    async def test_finish_leaves_non_converted_parent_alone(self) -> None:
        repo = _scratch_repo(self)
        store = InMemoryObjectiveStore()
        objective = await store.create_objective({
            "objective_id": "OBJ-1",
            "project": "p",
            "status": "active",
            "conversion_source_task_id": "task-parent",
        })
        lister = InMemoryTaskLister({
            "task-parent": {"task_id": "task-parent", "status": "cancelled"},
        })
        patches: dict[str, dict[str, Any]] = {}
        notices: list[tuple[str, str]] = []
        env = _objective_env(repo, store, lister, patches, notices)

        await _finish_objective(env, objective)

        self.assertNotIn("task-parent", patches)


if __name__ == "__main__":
    unittest.main()
