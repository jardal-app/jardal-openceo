"""Params persistence after a dev run (TASK-2026-00178).

Regression: when a run modified files, ``TaskService.run_task`` rebuilt
``params`` from the ticket's params at run start and discarded what the
dev session returned -- the loose-tier assignment (``tier_reason``,
``tier_assigned_by``, ``why_not_c``), ``oracle_frozen``, ``executor_link``
and ``material_failure``. Verified live: ticket #196 (a completed C run)
kept only the planner keys. The session's params are merged over the
ticket's; runs that touched no files keep the replace behavior.

No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from settings import Settings  # noqa: E402
from state import TaskStatus  # noqa: E402
from task_service import TaskService  # noqa: E402


class _FakeHub:
    async def broadcast(self, event: str, payload: dict) -> None:
        return None


def _service(task_params: dict[str, Any], graph_out: dict[str, Any],
             ) -> tuple[TaskService, Any]:
    class FakeRepo:
        def __init__(self) -> None:
            self.task = {
                "task_id": "task-1", "project": "proj", "agent": "dev",
                "agent_hint": "dev", "source": "api", "title": "Ticket",
                "description": "Descripcion", "attachments": [],
                "priority": "medium", "status": TaskStatus.FAILED.value,
                "steps": [], "params": dict(task_params),
            }

        async def get_task(self, task_id: str):
            return dict(self.task) if task_id == "task-1" else None

        async def patch_task(self, task_id: str, updates: dict):
            self.task.update(updates)
            return dict(self.task)

        async def list_tasks_blocked_by(self, task_id: str):
            return []

    class FakeGraph:
        async def execute(self, task: dict, agent_hint: str | None = None):
            return dict(graph_out)

    repo = FakeRepo()
    service = TaskService(
        settings=Settings(), repo=repo, graph=FakeGraph(),
        realtime_hub=_FakeHub(), notion=None, rate_monitor=None)
    return service, repo


def _graph_out(**overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "selected_agent": "dev", "status": TaskStatus.COMPLETED.value,
        "steps": [{"step": "code", "status": "ok", "detail": "d"}],
        "last_prompt_sent": "p", "last_response_summary": "s", "error": None,
    }
    out.update(overrides)
    return out


class ModifiedFilesParamsTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_params_survive_a_run_that_touched_files(self) -> None:
        ticket_params = {"_mirrors": {"erpnext": {"name": "TASK-1"}},
                         "oracle": {"type": "preexisting_tests",
                                    "refs": [{"ref": "tests/test_a.py"}]},
                         "work_type": "implementation"}
        session_params = {**ticket_params,
                          "tier_reason": "specified work (§25)",
                          "tier_assigned_by": "loose_classifier/v4",
                          "why_not_c": "a misjudged refactor ships drift",
                          "oracle_frozen": {"oracle_type": "preexisting_tests",
                                            "refs": []},
                          "executor_link": "dsh"}
        service, repo = _service(ticket_params, _graph_out(
            modified_files=["app.py"], params=session_params))

        await service.run_task("task-1", force=True)

        params = repo.task["params"]
        for key in ("tier_reason", "tier_assigned_by", "why_not_c",
                    "oracle_frozen", "executor_link"):
            self.assertIn(key, params, key)
        self.assertEqual(params["executor_link"], "dsh")
        self.assertEqual(params["modified_files"], ["app.py"])
        # The ticket's own keys are still there.
        self.assertEqual(params["work_type"], "implementation")
        self.assertIn("_mirrors", params)
        self.assertIn("oracle", params)

    async def test_a_partial_session_dict_never_drops_ticket_params(self) -> None:
        # Merge, not replace: a session that returns only what it added
        # (the shape _finish_simple_tool used to build) loses nothing.
        service, repo = _service(
            {"oracle": {"type": "preexisting_tests", "refs": []},
             "target_agent": "dev"},
            _graph_out(modified_files=["a.py", "b.py"],
                       params={"oracle_frozen": {"refs": []}}))

        await service.run_task("task-1", force=True)

        params = repo.task["params"]
        self.assertEqual(params["target_agent"], "dev")
        self.assertIn("oracle", params)
        self.assertIn("oracle_frozen", params)
        self.assertEqual(params["modified_files"], ["a.py", "b.py"])

    async def test_session_value_wins_on_conflict(self) -> None:
        service, repo = _service(
            {"tier_reason": "old"},
            _graph_out(modified_files=["a.py"],
                       params={"tier_reason": "new"}))

        await service.run_task("task-1", force=True)

        self.assertEqual(repo.task["params"]["tier_reason"], "new")

    async def test_no_session_params_keeps_the_ticket_params(self) -> None:
        service, repo = _service(
            {"work_type": "implementation"},
            _graph_out(modified_files=["a.py"]))

        await service.run_task("task-1", force=True)

        self.assertEqual(repo.task["params"],
                         {"work_type": "implementation",
                          "modified_files": ["a.py"]})


class NoFilesTouchedParamsTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_params_replace_when_nothing_was_modified(self) -> None:
        # Unchanged behavior: no modified files, the session's params are
        # the persisted set.
        service, repo = _service(
            {"work_type": "old"},
            _graph_out(params={"work_type": "new", "executor_link": "muse"}))

        await service.run_task("task-1", force=True)

        self.assertEqual(repo.task["params"],
                         {"work_type": "new", "executor_link": "muse"})

    async def test_no_params_from_the_session_leaves_them_alone(self) -> None:
        service, repo = _service({"work_type": "kept"}, _graph_out())

        await service.run_task("task-1", force=True)

        self.assertEqual(repo.task["params"], {"work_type": "kept"})


if __name__ == "__main__":
    unittest.main()
