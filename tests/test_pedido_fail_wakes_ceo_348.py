"""Pedido terminally failed wakes the CEO as an incident (TASK-2026-00348).

Contract fixed here:

- ``_stop_pedido_failed`` signals the CEO loop via ``_wake_ceo_incident``
  on top of the ERP comment, so a pedido that fails 3/3 in planning never
  stalls silently until the next weekly review.
- No double wake: the signal fires only on the transition into a terminal
  state, mirroring the ``update_task`` funnel (an already-failed ticket
  stays silent).
- Personal scope stays out of the company review (privacy).

No network, no CLI, no LLM. Wake is simulated.
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


class _FakeRepo:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = dict(task)

    async def get_task(self, task_id: str):
        return dict(self.task) if task_id == self.task["task_id"] else None

    async def patch_task(self, task_id: str, updates: dict):
        self.task.update(updates)
        return dict(self.task)


def _company_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task-1", "project": "proj", "agent": "ceo",
        "source": "api", "title": "Pedido Shop necesita checkout",
        "description": "Descripcion", "attachments": [],
        "priority": "high", "status": TaskStatus.WAITING.value,
        "steps": [], "params": {"planner_attempts": 3},
    }
    task.update(overrides)
    return task


def _service(task: dict[str, Any]) -> tuple[TaskService, _FakeRepo, list]:
    repo = _FakeRepo(task)
    service = TaskService(
        settings=Settings(), repo=repo, graph=None,
        realtime_hub=_FakeHub(), notion=None, rate_monitor=None)
    wakes: list = []
    service.set_ceo_wake(wakes.append)
    return service, repo, wakes


class StopPedidoFailedWakesCeoTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_pedido_fail_wakes_ceo(self) -> None:
        service, repo, wakes = _service(_company_task())

        result = await service._stop_pedido_failed(
            "task-1", "planner failed 3x: information_gaps invalid")

        self.assertEqual(result["status"], TaskStatus.FAILED.value)
        self.assertEqual(len(wakes), 1)
        self.assertIn("task-1", wakes[0])

    async def test_already_failed_pedido_does_not_wake_again(self) -> None:
        service, repo, wakes = _service(
            _company_task(status=TaskStatus.FAILED.value))

        await service._stop_pedido_failed("task-1", "planner failed 3x: x")

        self.assertEqual(wakes, [])

    async def test_personal_scope_pedido_stays_silent(self) -> None:
        service, repo, wakes = _service(_company_task(params={
            "horizon_scope": "personal", "planner_attempts": 3}))

        await service._stop_pedido_failed("task-1", "planner failed 3x: x")

        self.assertEqual(wakes, [])


if __name__ == "__main__":
    unittest.main()
