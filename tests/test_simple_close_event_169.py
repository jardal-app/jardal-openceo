"""Objective close wakes after the terminal persist (TASK-2026-00169).

Contract fixed here:

- A simple-tier (A/B/C) close notifies the objectives engine ONCE,
  after ``run_task`` persists COMPLETED — never on the stale
  pre-persist read that wedged the objective ``active`` with every
  child done (the closing child counted as ``ready``).
- The session stashes its close context (SHA/files/handoff) in the
  fresh output params; the funnel builds the notification from it.
  Tier S keeps its inline post-persist trigger and marks it, so the
  funnel skips it (no double replan). Any other domain closing
  through the common funnel notifies with what the persisted ticket
  carries. QA gates are untouched.
- Belt and braces in the engine: the subject of ``on_ticket_closed``
  never counts as ``ready``, whatever a racy store read says.

Real funnel (``TaskService.run_task``), real engine, dict-backed
stores with real update semantics, scripted executors; no models.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import (  # noqa: E402
    DevExecutionOutput,
    DevSessionManager,
    _SimpleBranch,
)
from objectives import engine as engine_mod  # noqa: E402
from objectives.service import ObjectiveService  # noqa: E402
from orchestration.live import ExecResult  # noqa: E402
from settings import Settings  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402
from task_service import TaskService  # noqa: E402

CLEAN_REPORT = "No findings.\nREVIEW_DONE_ticket_1\n"

_POOL = [
    {"label": "r-deep", "family": "deepseek", "priority": 10},
    {"label": "r-qwen", "family": "qwen", "priority": 20},
    {"label": "r-glm", "family": "glm", "priority": 30},
]


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
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


class _FakeHub:
    async def broadcast(self, event: str, payload: dict) -> None:
        del event, payload


class _DictRepo:
    """Dict-backed store with real update semantics for funnel + engine."""

    def __init__(self, tasks: dict[str, dict[str, Any]]) -> None:
        self.tasks = {key: dict(row) for key, row in tasks.items()}
        self.objectives: dict[str, dict[str, Any]] = {}
        self.agent_status: dict[str, dict[str, Any]] = {}
        self.reviews: list[dict[str, Any]] = []
        self.inbox: dict[str, dict[str, Any]] = {}

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return dict(row) if row is not None else None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        if row is None:
            return None
        row.update(updates)
        return dict(row)

    async def list_tasks_blocked_by(self, task_id: str) -> list[dict]:
        del task_id
        return []

    async def list_tasks_by_objective(
            self, objective_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.tasks.values()
                if row.get("objective_id") == objective_id]

    async def create_objective(self, doc: dict[str, Any]) -> dict[str, Any]:
        stored = dict(doc)
        self.objectives[stored["objective_id"]] = stored
        return dict(stored)

    async def get_objective(
            self, objective_id: str) -> dict[str, Any] | None:
        doc = self.objectives.get(objective_id)
        return dict(doc) if doc is not None else None

    async def patch_objective(
        self, objective_id: str, updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        doc = self.objectives.get(objective_id)
        if doc is None:
            return None
        doc.update(updates)
        return dict(doc)

    async def list_active_objectives(
            self, project: str | None = None) -> list[dict[str, Any]]:
        del project
        return [dict(doc) for doc in self.objectives.values()
                if doc.get("status") == "active"]

    async def get_agent_status(self, key: str) -> dict[str, Any] | None:
        row = self.agent_status.get(key)
        return dict(row) if row is not None else None

    async def set_agent_status(self, key: str, state: dict[str, Any]) -> None:
        self.agent_status[key] = dict(state)

    async def create_task_review(self, doc: dict[str, Any]) -> None:
        self.reviews.append(dict(doc))

    async def get_findings_entry(self, fp: str) -> dict[str, Any] | None:
        entry = self.inbox.get(fp)
        return dict(entry) if entry is not None else None

    async def upsert_findings_entry(self, fp: str,
                                    entry: dict[str, Any]) -> None:
        self.inbox[fp] = dict(entry)

    async def list_open_findings_for_files(
            self, files: list[str], limit: int) -> list[dict[str, Any]]:
        del files, limit
        return []


class _FakeGraph:
    def __init__(self, graph_out: dict[str, Any]) -> None:
        self._graph_out = graph_out

    async def execute(self, task: dict,
                      agent_hint: str | None = None) -> dict[str, Any]:
        del task, agent_hint
        return dict(self._graph_out)


class _FakeConfig:
    def __init__(self, project_config: dict[str, Any]) -> None:
        self._project_config = project_config

    def load_project(self, project: str | None) -> dict[str, Any]:
        del project
        return self._project_config

    def has_project(self, project: str | None) -> bool:
        del project
        return True


class _ScriptedObjectiveExecutor:
    """Planner/reviewer roles over real git; no models, no CLI."""

    def __init__(self, repo: Path, planner_status: str = "complete_candidate",
                 ) -> None:
        self._repo = repo
        self._planner_status = planner_status
        self.calls: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> ExecResult:
        del workdir, timeout_s
        self.calls.append(role)
        if role == "objective_planner":
            target: Path | None = None
            for line in prompt.splitlines():
                if line.startswith("OUTPUT: write "):
                    rel = line[len("OUTPUT: write "):].split(
                        " with exactly")[0]
                    target = self._repo / rel.strip()
            assert target is not None, "planner prompt names no target"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({
                "objective_status": self._planner_status,
                "state_summary": "scripted plan",
                "tickets": [],
            }), encoding="utf-8")
            return ExecResult(0, 1.0, output_tail="PLANNER_READY")
        if role.startswith("review:"):
            return ExecResult(0, 2.0, output_tail=CLEAN_REPORT)
        raise AssertionError(f"unexpected role {role}")

    async def cleanup(self, role: str) -> None:
        del role


def _project_config(repo: Path) -> dict[str, Any]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: route by oracle.\n", encoding="utf-8")
    return {
        "project": {"repos": [{"local_path": str(repo),
                               "branch_base": "main"}]},
        "dev": {"auto_merge": True},
        "objectives": {},
        "objective_planner": {
            "agents": {"dev": {"enabled": True}},
            "role": {"command": ["true"]},
            "policy_path": str(policy),
            "timeout_minutes": 15,
        },
        "review": {
            "roles": {"closer": {"command": ["true"]},
                      "arbiter": {"command": ["true"]}},
            "reviewers": [dict(slot) for slot in _POOL],
            "review": {"quorum": 2},
            "timeouts_minutes": {"closer": 15, "arbiter": 10},
        },
    }


def _objective_doc(objective_id: str, base_sha: str) -> dict[str, Any]:
    return {
        "objective_id": objective_id,
        "project": "proj",
        "status": "active",
        "anchor": {"goal": "g", "acceptance": ["ship"]},
        "capsule": {"text": "", "compiled_at": None, "tokens": 0},
        "tickets_log": [],
        "tickets_files": {},
        "base_sha": base_sha,
        "conversion_source_task_id": "task-parent",
        "erp_group_ref": "TASK-GROUP",
    }


def _parent_doc(objective_id: str) -> dict[str, Any]:
    return {
        "task_id": "task-parent", "title": "t", "description": "d",
        "status": "converted", "project": "proj", "acceptance": [],
        "params": {"_mirrors": {"erpnext": {"name": "TASK-GROUP"}}},
        "objective_id": objective_id}


def _child_doc(task_id: str, objective_id: str, status: str) -> dict[str, Any]:
    return {
        "task_id": task_id, "title": "w", "description": "d",
        "status": status, "project": "proj",
        "acceptance": ["done"],
        "params": {"target_agent": "dev",
                   "expected_artifacts": ["code"]},
        "coding_tool": "tier_c",
        "objective_id": objective_id}


def _completed_graph_out(task_id: str) -> dict[str, Any]:
    return {
        "selected_agent": "dev", "status": TaskStatus.COMPLETED.value,
        "steps": [{"step": "close", "status": "ok", "detail": "d"}],
        "last_prompt_sent": "p",
        "last_response_summary": "tier_c closed: merged into main.",
        "error": None, "modified_files": ["tracked.txt"],
        "params": {"_objective_close": {
            "task_id": task_id, "title": "w", "tier": "tier_c",
            "sha": "abc123def456", "files": ["tracked.txt"],
            "handoff_excerpt": "did the thing",
        }},
    }


class FunnelCloseWakesObjectiveTests(unittest.IsolatedAsyncioTestCase):
    """Real funnel + real engine: the last C child advances the objective."""

    async def _world(self, objective_id: str, **extra_tasks: dict[str, Any],
                     ) -> tuple[TaskService, _DictRepo,
                                _ScriptedObjectiveExecutor, Path]:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "tracked.txt").write_text(
            "before\nchanged line\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "change")
        tasks = {"task-parent": _parent_doc(objective_id), **extra_tasks}
        store = _DictRepo(tasks)
        await store.create_objective(_objective_doc(objective_id, base))
        scripted = _ScriptedObjectiveExecutor(repo)
        service = TaskService(
            settings=Settings(), repo=store,
            graph=_FakeGraph(_completed_graph_out("kid-1")),
            realtime_hub=_FakeHub(), notion=None, rate_monitor=None)
        objective_service = ObjectiveService(
            store, service, _FakeConfig(_project_config(repo)), Settings())
        service.set_objective_service(objective_service)
        return service, store, scripted, repo

    async def test_last_child_close_plans_reviews_and_finishes(self) -> None:
        service, store, scripted, _repo = await self._world(
            "OBJ-169A", **{"kid-1": _child_doc("kid-1", "OBJ-169A",
                                               "queued")})

        with patch("objectives.service.objectives_process_executor",
                   return_value=scripted):
            result = await service.run_task("kid-1")

        assert result is not None
        self.assertEqual(result["status"], "completed")
        # The planner ran once and declared complete_candidate; the
        # final review ran clean over the integration ref.
        self.assertEqual(scripted.calls.count("objective_planner"), 1)
        self.assertTrue(any(call.startswith("review:")
                            for call in scripted.calls))
        objective = await store.get_objective("OBJ-169A")
        assert objective is not None
        self.assertEqual(objective["status"], "closed")
        self.assertEqual(len(objective.get("planning_runs") or []), 1)
        parent = await store.get_task("task-parent")
        assert parent is not None
        self.assertEqual(parent["status"], "completed")
        # Handoff/SHA/capsule preserved: the close was absorbed.
        log = objective.get("tickets_log") or []
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["task_id"], "kid-1")
        self.assertEqual(log[0]["sha"], "abc123def456")
        self.assertEqual(log[0]["files"], ["tracked.txt"])
        self.assertIn("did the thing", log[0]["handoff_excerpt"])
        # complete_candidate creates no children.
        self.assertEqual(
            sorted(store.tasks), ["kid-1", "task-parent"])

    async def test_open_sibling_defers_without_extra_planning(self) -> None:
        service, store, scripted, _repo = await self._world(
            "OBJ-169B",
            **{"kid-1": _child_doc("kid-1", "OBJ-169B", "queued"),
               "kid-2": {"task_id": "kid-2", "title": "s", "status": "queued",
                         "project": "proj", "objective_id": "OBJ-169B"}})

        with patch("objectives.service.objectives_process_executor",
                   return_value=scripted):
            result = await service.run_task("kid-1")

        assert result is not None
        self.assertEqual(result["status"], "completed")
        self.assertEqual(scripted.calls.count("objective_planner"), 0)
        objective = await store.get_objective("OBJ-169B")
        assert objective is not None
        self.assertEqual(objective["status"], "active")
        sibling = await store.get_task("kid-2")
        assert sibling is not None
        self.assertEqual(sibling["status"], "queued")
        log = objective.get("tickets_log") or []
        self.assertEqual([entry["task_id"] for entry in log], ["kid-1"])

    async def test_close_is_idempotent(self) -> None:
        service, store, scripted, _repo = await self._world(
            "OBJ-169C", **{"kid-1": _child_doc("kid-1", "OBJ-169C",
                                               "queued")})

        with patch("objectives.service.objectives_process_executor",
                   return_value=scripted):
            await service.run_task("kid-1")
            objective = await store.get_objective("OBJ-169C")
            assert objective is not None
            self.assertEqual(objective["status"], "closed")
            # A force relaunch of the completed child notifies again,
            # but a closed objective does not plan twice.
            await service.run_task("kid-1", force=True)

        self.assertEqual(scripted.calls.count("objective_planner"), 1)
        objective = await store.get_objective("OBJ-169C")
        assert objective is not None
        self.assertEqual(objective["status"], "closed")
        self.assertEqual(
            sorted(store.tasks), ["kid-1", "task-parent"])


class FunnelDispatchTests(unittest.IsolatedAsyncioTestCase):
    """One notification per close: S skips, others notify post-persist."""

    def _service(self, graph_out: dict[str, Any],
                 objective_service: Any = "dummy",
                 ) -> tuple[TaskService, _DictRepo]:
        store = _DictRepo({"kid-1": _child_doc("kid-1", "OBJ-169D",
                                               "queued")})
        service = TaskService(
            settings=Settings(), repo=store,
            graph=_FakeGraph(graph_out),
            realtime_hub=_FakeHub(), notion=None, rate_monitor=None)
        if objective_service == "dummy":
            dummy = MagicMock()
            dummy.env_for.return_value = MagicMock()
            service.set_objective_service(dummy)
        elif objective_service is not None:
            service.set_objective_service(objective_service)
        return service, store

    async def test_tier_s_mark_skips_without_double_replan(self) -> None:
        out = _completed_graph_out("kid-1")
        out["params"] = {"_objective_close_notified": True}
        service, _store = self._service(out)

        with patch("task_service.on_ticket_closed",
                   new=AsyncMock()) as notified:
            result = await service.run_task("kid-1")

        assert result is not None
        self.assertEqual(result["status"], "completed")
        notified.assert_not_awaited()

    async def test_stashed_context_notifies_with_sha_and_handoff(self) -> None:
        service, _store = self._service(_completed_graph_out("kid-1"))

        with patch("task_service.on_ticket_closed",
                   new=AsyncMock(return_value={"ok": True})) as notified:
            await service.run_task("kid-1")

        notified.assert_awaited_once()
        ticket = notified.await_args.args[1]
        close = notified.await_args.args[2]
        self.assertEqual(ticket["status"], "completed")
        self.assertEqual(close.task_id, "kid-1")
        self.assertEqual(close.sha, "abc123def456")
        self.assertEqual(close.files, ["tracked.txt"])
        self.assertEqual(close.handoff_excerpt, "did the thing")

    async def test_other_domains_notify_with_persisted_files(self) -> None:
        out = _completed_graph_out("kid-1")
        out["params"] = {}
        service, _store = self._service(out)

        with patch("task_service.on_ticket_closed",
                   new=AsyncMock(return_value={"ok": True})) as notified:
            await service.run_task("kid-1")

        notified.assert_awaited_once()
        close = notified.await_args.args[2]
        self.assertEqual(close.task_id, "kid-1")
        self.assertEqual(close.sha, "")
        self.assertEqual(close.files, ["tracked.txt"])

    async def test_no_objective_service_skips_silently(self) -> None:
        service, _store = self._service(
            _completed_graph_out("kid-1"), objective_service=None)

        with patch("task_service.on_ticket_closed",
                   new=AsyncMock()) as notified:
            result = await service.run_task("kid-1")

        assert result is not None
        self.assertEqual(result["status"], "completed")
        notified.assert_not_awaited()

    async def test_trigger_failure_never_fails_the_close(self) -> None:
        failing = MagicMock()
        failing.env_for.side_effect = RuntimeError("no env")
        service, _store = self._service(
            _completed_graph_out("kid-1"), objective_service=failing)

        result = await service.run_task("kid-1")

        assert result is not None
        self.assertEqual(result["status"], "completed")

    async def test_failed_close_does_not_notify(self) -> None:
        out = _completed_graph_out("kid-1")
        out["status"] = TaskStatus.FAILED.value
        service, _store = self._service(out)

        with patch("task_service.on_ticket_closed",
                   new=AsyncMock()) as notified:
            result = await service.run_task("kid-1")

        assert result is not None
        self.assertEqual(result["status"], "failed")
        notified.assert_not_awaited()


class ClosingTicketNeverReadyTests(unittest.IsolatedAsyncioTestCase):
    """The notification subject counts as closed, never as open work."""

    def _env(self, tasks: dict[str, dict[str, Any]]):
        objectives = engine_mod.InMemoryObjectiveStore()
        return engine_mod.ObjectiveEnv(
            objectives=objectives,
            tasks=engine_mod.InMemoryTaskLister(dict(tasks)),
            create_ticket=AsyncMock(return_value={"task_id": "t"}),
            patch_task=AsyncMock(),
            executor=MagicMock(),
            quota=MagicMock(),
            slots=MagicMock(),
            reviews=None,
            inbox=None,
            notify=AsyncMock(),
            repo=Path.cwd(),
        ), objectives

    async def test_stale_in_progress_subject_still_plans(self) -> None:
        env, objectives = self._env({
            "kid-1": {"task_id": "kid-1", "status": "in_progress",
                      "objective_id": "OBJ-169E"},
            "kid-2": {"task_id": "kid-2", "status": "completed",
                      "objective_id": "OBJ-169E"},
        })
        await objectives.create_objective({
            "objective_id": "OBJ-169E", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []}})
        ticket = {"task_id": "kid-1", "status": "in_progress",
                  "objective_id": "OBJ-169E"}
        close = engine_mod.TicketCloseContext(
            task_id="kid-1", title="w", tier="tier_c", sha="s")

        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True,
                                                      "planned": True})
                          ) as planned:
            result = await engine_mod.on_ticket_closed(env, ticket, close)

        planned.assert_awaited_once_with(env, "OBJ-169E")
        self.assertEqual(result, {"ok": True, "planned": True})

    async def test_subject_without_context_counts_as_closed(self) -> None:
        env, objectives = self._env({
            "kid-1": {"task_id": "kid-1", "status": "waiting",
                      "waiting_reason": "qa_review_pending",
                      "objective_id": "OBJ-169F"},
        })
        await objectives.create_objective({
            "objective_id": "OBJ-169F", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []}})
        ticket = {"task_id": "kid-1", "status": "waiting",
                  "objective_id": "OBJ-169F"}

        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True})
                          ) as planned:
            await engine_mod.on_ticket_closed(env, ticket, None)

        # A QA-held subject still counts as closed work: the execution
        # finished, QA is a business gate.
        planned.assert_awaited_once_with(env, "OBJ-169F")

    async def test_empty_ids_exclude_nothing(self) -> None:
        env, objectives = self._env({
            "kid-1": {"task_id": "kid-1", "status": "queued",
                      "objective_id": "OBJ-169G"},
        })
        await objectives.create_objective({
            "objective_id": "OBJ-169G", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []}})

        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True})
                          ) as planned:
            result = await engine_mod.on_ticket_closed(
                env, {"objective_id": "OBJ-169G"}, None)

        planned.assert_not_awaited()
        self.assertEqual(result["action"], "enqueuer-continues")
        self.assertEqual(result["ready"], 1)


class SessionCloseContractTests(unittest.IsolatedAsyncioTestCase):
    """Simple tiers stash; Tier S marks its inline notification."""

    async def test_simple_close_stashes_context_for_the_funnel(self) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-qb", "work")
        (repo / "tracked.txt").write_text(
            "before\nchanged line\n", encoding="utf-8")
        handoff_dir = repo / ".tier-s"
        handoff_dir.mkdir(exist_ok=True)
        (handoff_dir / "HANDOFF.md").write_text(
            "# Handoff\nSHA:\nFiles changed:\nDone.\n", encoding="utf-8")
        manager = DevSessionManager(Settings(), MagicMock())
        manager._task_service = None
        manager._rate_monitor = None
        manager._repo = None
        config = {"project": {"repos": [{"branch_base": "main"}]},
                  "dev": {"tier_s": {"budgets": {"gate_retries": 0}}}}
        task = {"task_id": "task-c1", "ref": "c1", "title": "C work",
                "coding_tool": "tier_c", "project": "proj",
                "objective_id": "OBJ-169H", "params": {}}
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="c1")

        output = await manager._finish_simple_tool(
            task, config, CodingTool.TIER_C,
            DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)

        self.assertEqual(output.status, TaskStatus.COMPLETED)
        assert output.params is not None
        stashed = output.params.get("_objective_close")
        assert isinstance(stashed, dict)
        self.assertEqual(stashed["task_id"], "task-c1")
        self.assertEqual(stashed["tier"], "tier_c")
        self.assertTrue(stashed["sha"])
        self.assertEqual(stashed["files"], ["tracked.txt"])
        self.assertIn("Handoff", stashed["handoff_excerpt"])
        self.assertNotIn("_objective_close_notified", output.params)
        self.assertNotIn("Objective trigger",
                         output.last_response_summary or "")

    async def test_tier_s_close_marks_its_inline_notification(self) -> None:
        repo = _scratch_repo(self)
        manager = DevSessionManager(Settings(), MagicMock())
        outcome = SimpleNamespace(status="completed", run_id="ts-x",
                                  reason="", detail="", branch="tier-s/x",
                                  commit_sha="deadbeef", resume_at=None)
        project_config = {
            "project": {"repos": [{"local_path": str(repo)}]},
            "dev": {},
        }
        task = {"task_id": "task-s1", "ref": "TASK-1", "title": "t",
                "project": "p", "objective_id": "OBJ-169I",
                "params": {"_objective_close": {"stale": True}}}

        with patch("agents.dev.session.TierSCycle") as cycle_cls:
            cycle_cls.return_value.run = AsyncMock(return_value=outcome)
            output = await manager._run_tier_s(task, project_config, {})

        self.assertEqual(output.status, TaskStatus.COMPLETED)
        assert output.params is not None
        self.assertTrue(output.params.get("_objective_close_notified"))
        self.assertNotIn("_objective_close", output.params)

    async def test_tier_s_convert_marks_nothing(self) -> None:
        repo = _scratch_repo(self)
        manager = DevSessionManager(Settings(), MagicMock())
        outcome = SimpleNamespace(status="completed", run_id="ts-x",
                                  reason="converted", detail="OBJ-1",
                                  branch="tier-s/x", commit_sha="",
                                  resume_at=None)
        project_config = {
            "project": {"repos": [{"local_path": str(repo)}]},
            "dev": {},
        }
        task = {"task_id": "task-s1", "ref": "TASK-1", "title": "t",
                "project": "p", "objective_id": "OBJ-1", "params": {}}

        with patch("agents.dev.session.TierSCycle") as cycle_cls:
            cycle_cls.return_value.run = AsyncMock(return_value=outcome)
            output = await manager._run_tier_s(task, project_config, {})

        self.assertEqual(output.status, TaskStatus.CONVERTED)
        assert output.params is not None
        self.assertNotIn("_objective_close_notified", output.params)


if __name__ == "__main__":
    unittest.main()
