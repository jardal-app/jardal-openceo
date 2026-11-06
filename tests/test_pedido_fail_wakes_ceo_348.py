"""Unexpected pedido failures yield to the mechanic; personal scope stays private."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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


class PedidoMechanicHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_pedido_wakes_mechanic_without_a_ceo_incident(self) -> None:
        service, repo, wakes = _service(_company_task())
        mechanic = MagicMock()
        service.set_mechanic_wake(mechanic)
        result = await service._park_execution_error("task-1", "invalid planner output")
        self.assertEqual(result["status"], TaskStatus.WAITING.value)
        self.assertIsNone(result["resume_at"])
        mechanic.assert_called_once_with("task-1", "gave_up", "execution_exception")
        self.assertEqual(wakes, [])

    async def test_cancelled_pedido_is_not_reopened(self) -> None:
        service, repo, wakes = _service(
            _company_task(status=TaskStatus.CANCELLED.value))
        mechanic = MagicMock()
        service.set_mechanic_wake(mechanic)
        result = await service._park_execution_error("task-1", "late failure")
        self.assertEqual(result["status"], TaskStatus.CANCELLED.value)
        mechanic.assert_not_called()
        self.assertEqual(wakes, [])

    async def test_personal_scope_pedido_stays_out_of_company_agents(self) -> None:
        service, repo, wakes = _service(_company_task(params={"horizon_scope": "personal"}))
        mechanic = MagicMock()
        service.set_mechanic_wake(mechanic)
        result = await service._park_execution_error("task-1", "private failure")
        self.assertEqual(result["status"], TaskStatus.FAILED.value)
        mechanic.assert_not_called()
        self.assertEqual(wakes, [])


if __name__ == "__main__":
    unittest.main()
