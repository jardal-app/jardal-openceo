"""Terminal business cancellation vs in-flight execution (TASK-2026-00177).

Contract fixed here:

- ``run_task`` never starts, continues or lands on a cancelled ticket:
  entry guard (force relaunches completed/converted, never cancelled),
  re-read after acquiring the project lock, and fencing before
  incorporating the graph result (success and exception paths). A fenced
  attempt only wakes the dispatcher and returns the current ticket.
- ``MongoRepository.patch_task`` is the atomic net: a status move OUT of
  cancelled is refused whole (cancel itself always lands, idempotent
  rewrites land, missing stays None, the observer stays silent).
- The Tier S cycle fences before parking/closing: run superseded (never
  a waiting run with a resume), work branch dropped, one audit note, no
  squash/merge/push, no task patch, no objective trigger.
- A simple-tier material failure on a freshly-cancelled ticket skips
  record + planner replan (evidence only, never new tickets).

No network, no CLI, no LLM. The net tests use real Mongo in an isolated
database (atomicity cannot be proven with a fake) and skip when down.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import (  # noqa: E402
    DevSessionManager,
    _SimpleBranch,
)
from agents.dev.tier_s.cycle import TierSCycle, _Ctx  # noqa: E402
from agents.dev.tier_s.stores import (  # noqa: E402
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from db.mongo import MongoRepository  # noqa: E402
from settings import Settings  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402
from task_service import TaskService  # noqa: E402


class _FakeHub:
    async def broadcast(self, event: str, payload: dict) -> None:
        return None


class _FakeRepo:
    def __init__(self, status: str = TaskStatus.QUEUED.value) -> None:
        self.task: dict[str, Any] = {
            "task_id": "task-1", "project": "proj", "agent": "dev",
            "agent_hint": "dev", "source": "api", "title": "Ticket",
            "description": "Descripcion", "attachments": [],
            "priority": "medium", "status": status,
            "steps": [], "params": {},
        }
        self.patches: list[dict[str, Any]] = []

    async def get_task(self, task_id: str):
        return dict(self.task) if task_id == "task-1" else None

    async def patch_task(self, task_id: str, updates: dict):
        self.patches.append(dict(updates))
        self.task.update(updates)
        return dict(self.task)

    async def list_tasks_blocked_by(self, task_id: str):
        return []

    def written_statuses(self) -> list[str]:
        return [str(p["status"]) for p in self.patches if "status" in p]


class _ScriptedGraph:
    """A blocked simulated executor. ``behavior`` runs inside execute."""

    def __init__(self, behavior) -> None:
        self._behavior = behavior
        self.calls = 0
        self.entered = asyncio.Event()

    async def execute(self, task: dict, agent_hint: str | None = None):
        self.calls += 1
        self.entered.set()
        result = self._behavior(task)
        if asyncio.iscoroutine(result):
            result = await result
        return result


def _graph_out(**overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "selected_agent": "dev", "status": TaskStatus.COMPLETED.value,
        "steps": [{"step": "code", "status": "ok", "detail": "d"}],
        "last_prompt_sent": "p", "last_response_summary": "s", "error": None,
    }
    out.update(overrides)
    return out


def _service(status: str, behavior
             ) -> tuple[TaskService, _FakeRepo, _ScriptedGraph]:
    repo = _FakeRepo(status)
    graph = _ScriptedGraph(behavior)
    service = TaskService(
        settings=Settings(), repo=repo, graph=graph,
        realtime_hub=_FakeHub(), notion=None, rate_monitor=None)
    return service, repo, graph


class RunTaskCancelFencingTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_cancelled_with_force_does_not_run(self) -> None:
        service, repo, graph = _service(
            TaskStatus.CANCELLED.value,
            lambda task: _graph_out())

        result = await service.run_task("task-1", force=True)

        self.assertEqual(graph.calls, 0)
        self.assertEqual(repo.patches, [])
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(repo.task["status"], TaskStatus.CANCELLED.value)

    async def test_entry_cancelled_without_force_does_not_run(self) -> None:
        service, repo, graph = _service(
            TaskStatus.CANCELLED.value,
            lambda task: _graph_out())

        result = await service.run_task("task-1")

        self.assertEqual(graph.calls, 0)
        self.assertEqual(repo.patches, [])
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)

    async def test_force_still_relaunches_completed(self) -> None:
        service, repo, graph = _service(
            TaskStatus.COMPLETED.value,
            lambda task: _graph_out())

        result = await service.run_task("task-1", force=True)

        self.assertEqual(graph.calls, 1)
        self.assertEqual(result["status"], TaskStatus.COMPLETED.value)

    async def test_cancel_while_waiting_project_lock_no_start(
            self) -> None:
        async def behavior(task):
            return _graph_out()

        service, repo, graph = _service(TaskStatus.QUEUED.value, behavior)
        project_lock = service._project_locks.setdefault(
            "proj", asyncio.Lock())
        await project_lock.acquire()
        try:
            run = asyncio.create_task(service.run_task("task-1"))
            await asyncio.sleep(0.05)
            repo.task["status"] = TaskStatus.CANCELLED.value
            repo.task["error"] = "El ticket fue cancelado en ERPNext."
        finally:
            project_lock.release()
        result = await run

        self.assertEqual(graph.calls, 0)
        self.assertNotIn(TaskStatus.IN_PROGRESS.value,
                         repo.written_statuses())
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(repo.task["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(repo.task["error"],
                         "El ticket fue cancelado en ERPNext.")

    async def test_completed_while_waiting_project_lock_does_not_start(
            self) -> None:
        async def behavior(task):
            return _graph_out()

        service, repo, graph = _service(TaskStatus.QUEUED.value, behavior)
        project_lock = service._project_locks.setdefault(
            "proj", asyncio.Lock())
        await project_lock.acquire()
        try:
            run = asyncio.create_task(service.run_task("task-1"))
            await asyncio.sleep(0.05)
            repo.task["status"] = TaskStatus.COMPLETED.value
        finally:
            project_lock.release()
        result = await run

        self.assertEqual(graph.calls, 0)
        self.assertNotIn(TaskStatus.IN_PROGRESS.value,
                         repo.written_statuses())
        self.assertEqual(result["status"], TaskStatus.COMPLETED.value)

    async def test_cancel_during_execution_discards_result(self) -> None:
        release = asyncio.Event()

        async def behavior(task):
            await release.wait()
            return _graph_out()

        service, repo, graph = _service(TaskStatus.QUEUED.value, behavior)
        wakes: list[None] = []
        service._dispatcher_wake = lambda: wakes.append(None)
        with patch.object(service, "_on_ticket_done",
                          new=AsyncMock()) as done:
            run = asyncio.create_task(service.run_task("task-1"))
            await graph.entered.wait()
            repo.task["status"] = TaskStatus.CANCELLED.value
            repo.task["error"] = "El ticket fue cancelado en ERPNext."
            release.set()
            result = await run
            await asyncio.sleep(0.05)
            done.assert_not_awaited()

        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(repo.task["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(repo.task["error"],
                         "El ticket fue cancelado en ERPNext.")
        for status in repo.written_statuses():
            self.assertNotIn(status, (TaskStatus.COMPLETED.value,
                                      TaskStatus.FAILED.value,
                                      TaskStatus.WAITING.value))
        self.assertTrue(wakes, "a fenced run still frees its slot")

    async def test_cancel_just_before_result_discards_it(self) -> None:
        repo_holder: dict[str, _FakeRepo] = {}

        async def behavior(task):
            repo = repo_holder["repo"]
            repo.task["status"] = TaskStatus.CANCELLED.value
            repo.task["error"] = "El ticket fue cancelado en ERPNext."
            return _graph_out()

        service, repo, graph = _service(TaskStatus.QUEUED.value, behavior)
        repo_holder["repo"] = repo

        result = await service.run_task("task-1")

        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(repo.task["status"], TaskStatus.CANCELLED.value)
        self.assertNotIn(TaskStatus.COMPLETED.value,
                         repo.written_statuses())

    async def test_cancel_during_execution_discards_failure(self) -> None:
        release = asyncio.Event()

        async def behavior(task):
            await release.wait()
            raise RuntimeError("boom")

        service, repo, graph = _service(TaskStatus.QUEUED.value, behavior)
        run = asyncio.create_task(service.run_task("task-1"))
        await graph.entered.wait()
        repo.task["status"] = TaskStatus.CANCELLED.value
        repo.task["error"] = "El ticket fue cancelado en ERPNext."
        release.set()
        result = await run

        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(repo.task["status"], TaskStatus.CANCELLED.value)
        self.assertNotIn(TaskStatus.FAILED.value, repo.written_statuses())


MONGO_URL = "mongodb://localhost:27017"
TEST_DB = "openceo_test_cancel_fencing"


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=400)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_mongo_available(), "MongoDB not available")
class CancelPatchNetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = Settings(_env_file=None, MONGODB_URL=MONGO_URL,
                            MONGODB_DATABASE=TEST_DB)
        self.repo = MongoRepository(settings)
        await self.repo.connect()
        await self.repo.tasks.delete_many({})
        await self.repo.tier_s_runs.delete_many({})
        self._ids = 0

    async def asyncTearDown(self) -> None:
        await self.repo.tasks.delete_many({})
        await self.repo.tier_s_runs.delete_many({})
        await self.repo.close()

    async def _insert(self, status: str, **over: Any) -> str:
        self._ids += 1
        task_id = f"cancel-net-{self._ids}-{uuid.uuid4().hex[:6]}"
        doc: dict[str, Any] = {"task_id": task_id, "status": status}
        doc.update(over)
        await self.repo.tasks.insert_one(doc)
        return task_id

    async def test_exit_from_cancelled_is_refused_whole(self) -> None:
        task_id = await self._insert(
            TaskStatus.CANCELLED.value,
            error="El ticket fue cancelado en ERPNext.")

        result = await self.repo.patch_task(task_id, {
            "status": TaskStatus.IN_PROGRESS.value,
            "error": "resurrected",
            "waiting_reason": "quota: x",
        })

        assert result is not None
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(result["error"],
                         "El ticket fue cancelado en ERPNext.")
        self.assertNotIn("waiting_reason", result)
        stored = await self.repo.get_task(task_id)
        assert stored is not None
        self.assertEqual(stored["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(stored["error"],
                         "El ticket fue cancelado en ERPNext.")

    async def test_completed_relabel_of_cancelled_is_refused(self) -> None:
        task_id = await self._insert(TaskStatus.CANCELLED.value)

        result = await self.repo.patch_task(
            task_id, {"status": TaskStatus.COMPLETED.value})

        assert result is not None
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)

    async def test_cancel_always_lands(self) -> None:
        task_id = await self._insert(TaskStatus.IN_PROGRESS.value)

        result = await self.repo.patch_task(
            task_id, {"status": TaskStatus.CANCELLED.value,
                      "error": "cancelado"})

        assert result is not None
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        self.assertEqual(result["error"], "cancelado")

    async def test_idempotent_cancelled_rewrite_applies(self) -> None:
        task_id = await self._insert(TaskStatus.CANCELLED.value)

        result = await self.repo.patch_task(
            task_id, {"status": TaskStatus.CANCELLED.value,
                      "waiting_reason": None})

        assert result is not None
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)

    async def test_missing_task_stays_none(self) -> None:
        result = await self.repo.patch_task(
            "cancel-net-missing", {"status": TaskStatus.FAILED.value})
        self.assertIsNone(result)

    async def test_member_status_value_is_fenced(self) -> None:
        task_id = await self._insert(TaskStatus.CANCELLED.value)

        result = await self.repo.patch_task(
            task_id, {"status": TaskStatus.IN_PROGRESS})

        assert result is not None
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)

    async def test_ordinary_transition_still_applies(self) -> None:
        task_id = await self._insert(TaskStatus.QUEUED.value)

        result = await self.repo.patch_task(
            task_id, {"status": TaskStatus.IN_PROGRESS.value})

        assert result is not None
        self.assertEqual(result["status"], TaskStatus.IN_PROGRESS.value)

    async def test_fence_stays_silent_to_the_observer(self) -> None:
        task_id = await self._insert(TaskStatus.CANCELLED.value)
        observer = AsyncMock()
        self.repo.set_status_observer(observer)
        try:
            await self.repo.patch_task(
                task_id, {"status": TaskStatus.IN_PROGRESS.value})
            observer.assert_not_awaited()
        finally:
            self.repo.set_status_observer(None)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", check=True)
    return proc.stdout.strip()


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "t@e.invalid")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _ctx(ticket: dict[str, Any], repo: Path, run_id: str = "run-1") -> _Ctx:
    return _Ctx(
        ticket=dict(ticket), repo=repo, ref="TASK-1", run_id=run_id,
        branch="tier-s/work", base_branch="main", budgets={},
        timeouts={}, waiting_cfg={}, auto_merge=True,
        budget_exclude=[], gate_specs=[], roles={})


def _cycle(tasks: InMemoryTaskStore, runs: InMemoryRunStore,
           ) -> tuple[TierSCycle, list[tuple[str, str]]]:
    notices: list[tuple[str, str]] = []

    async def notify(ref: str, text: str) -> None:
        notices.append((ref, text))

    cycle = TierSCycle(runs, InMemoryLockStore(), tasks, MagicMock(),
                       notify=notify)
    return cycle, notices


class CycleCancelFencingTests(unittest.IsolatedAsyncioTestCase):
    async def test_park_on_cancelled_supersedes_without_task_patch(
            self) -> None:
        repo = _scratch_repo(self)
        tasks = InMemoryTaskStore(
            statuses={"task-1": TaskStatus.CANCELLED.value})
        runs = InMemoryRunStore()
        await runs.create_run({"run_id": "run-1", "task_id": "task-1",
                               "status": "running"})
        cycle, notices = _cycle(tasks, runs)
        ctx = _ctx({"task_id": "task-1"}, repo)

        outcome = await cycle._park(ctx, "quota", "q",
                                    patch_run=True, resume_in_min=5)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "cancelled")
        self.assertIsNone(outcome.branch)
        self.assertTrue(ctx.fenced_cancelled)
        self.assertEqual(runs.runs["run-1"]["status"], "superseded")
        self.assertIsNone(runs.runs["run-1"]["active_repo"])
        self.assertEqual(tasks.patches, {})
        self.assertEqual(tasks.statuses["task-1"],
                         TaskStatus.CANCELLED.value)
        self.assertTrue(any("fenced" in text for _, text in notices))

    async def test_park_on_live_ticket_parks_normally(self) -> None:
        repo = _scratch_repo(self)
        tasks = InMemoryTaskStore(
            statuses={"task-1": TaskStatus.IN_PROGRESS.value})
        runs = InMemoryRunStore()
        await runs.create_run({"run_id": "run-1", "task_id": "task-1",
                               "status": "running"})
        cycle, _notices = _cycle(tasks, runs)
        ctx = _ctx({"task_id": "task-1"}, repo)

        outcome = await cycle._park(ctx, "quota", "q", patch_run=True,
                                    resume_in_min=5)

        self.assertEqual(outcome.status, "waiting")
        self.assertFalse(ctx.fenced_cancelled)
        self.assertEqual(
            tasks.patches["task-1"]["status"], TaskStatus.WAITING.value)

    async def test_close_on_cancelled_never_merges(self) -> None:
        repo = _scratch_repo(self)
        tasks = InMemoryTaskStore(
            statuses={"task-1": TaskStatus.CANCELLED.value})
        runs = InMemoryRunStore()
        await runs.create_run({"run_id": "run-1", "task_id": "task-1",
                               "status": "running"})
        push = AsyncMock()
        cycle, _notices = _cycle(tasks, runs)
        cycle._push = push
        ctx = _ctx({"task_id": "task-1", "title": "t"}, repo)
        with patch.object(cycle, "_review_rounds",
                          new=AsyncMock(return_value=None)), \
                patch.object(cycle, "_merge",
                             side_effect=AssertionError("must not merge")):
            outcome = await cycle._close(ctx)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "cancelled")
        push.assert_not_awaited()
        self.assertEqual(runs.runs["run-1"]["status"], "superseded")
        self.assertEqual(tasks.patches, {})

    async def test_run_waited_after_fence_reports_cancelled(self) -> None:
        repo = _scratch_repo(self)
        tasks = InMemoryTaskStore()
        runs = InMemoryRunStore()
        cycle, _notices = _cycle(tasks, runs)
        ctx = _ctx({"task_id": "task-1"}, repo)
        ctx.fenced_cancelled = True

        outcome = await cycle._run_waited(ctx)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason, "cancelled")
        self.assertIsNone(outcome.branch)


def _manager() -> DevSessionManager:
    manager = DevSessionManager(Settings(), MagicMock())
    manager._task_service = None
    manager._rate_monitor = None
    manager._repo = None
    return manager


def _c_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task-c1",
        "ref": "c1",
        "title": "C work",
        "coding_tool": "tier_c",
        "objective_id": "OBJ-1",
        "project": "proj",
        "params": {"work_type": "implementation"},
    }
    task.update(overrides)
    return task


def _dev_config() -> dict[str, Any]:
    return {
        "project": {"repos": [{"branch_base": "main"}]},
        "dev": {"tier_s": {"budgets": {"gate_retries": 0}}},
    }


def _open_work_branch(repo: Path, name: str = "work") -> str:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", name)
    return base


class _CancelRepo:
    def __init__(self, status: str) -> None:
        self._status = status
        self.patched: list[dict[str, Any]] = []

    async def get_task(self, task_id: str):
        return {"task_id": task_id, "status": self._status}

    async def patch_task(self, task_id: str, updates: dict):
        self.patched.append(dict(updates))
        return {"task_id": task_id, "status": self._status}


class SessionReplanSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_material_failure_on_cancelled_skips_replan(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        manager = _manager()
        stored = _CancelRepo(TaskStatus.CANCELLED.value)
        manager._repo = stored
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="c1")
        with patch("agents.dev.session.record_material_failure",
                   new=AsyncMock()) as recorded, \
                patch("agents.dev.session.plan_objective",
                      new=AsyncMock(return_value={"ok": True})) as planned:
            output = await manager._material_simple_failure(
                _c_task(), _dev_config(), CodingTool.TIER_C, branch,
                "gates failed")

        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("replan skipped",
                      output.last_response_summary or "")
        recorded.assert_not_awaited()
        planned.assert_not_awaited()
        self.assertEqual(stored.patched, [])

    async def test_material_failure_on_live_ticket_replans(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        manager = _manager()
        stored = _CancelRepo(TaskStatus.IN_PROGRESS.value)
        manager._repo = stored
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="c1")
        with patch("agents.dev.session.record_material_failure",
                   new=AsyncMock()) as recorded, \
                patch("agents.dev.session.plan_objective",
                      new=AsyncMock(return_value={
                          "ok": False, "reason": "max_tickets_reached",
                          "detail": "20 lifetime tickets"})) as planned:
            output = await manager._material_simple_failure(
                _c_task(), _dev_config(), CodingTool.TIER_C, branch,
                "gates failed")

        self.assertEqual(output.status, TaskStatus.FAILED)
        recorded.assert_awaited_once()
        planned.assert_awaited_once()
        self.assertTrue(any(p.get("status") == TaskStatus.FAILED.value
                            for p in stored.patched))


if __name__ == "__main__":
    unittest.main()
