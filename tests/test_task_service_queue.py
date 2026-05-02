from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

motor_module = types.ModuleType("motor")
motor_asyncio_module = types.ModuleType("motor.motor_asyncio")
pymongo_module = types.ModuleType("pymongo")


class _DummyAsyncIOMotorClient:
    pass


motor_asyncio_module.AsyncIOMotorClient = _DummyAsyncIOMotorClient
pymongo_module.ASCENDING = 1
pymongo_module.DESCENDING = -1
pymongo_module.ReturnDocument = object()
sys.modules.setdefault("motor", motor_module)
sys.modules.setdefault("motor.motor_asyncio", motor_asyncio_module)
sys.modules.setdefault("pymongo", pymongo_module)


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from state import TaskStatus
from task_service import TaskService


class _FakeRepo:
    def __init__(self, queued: list[dict], active: list[dict]) -> None:
        self.queued = queued
        self.active = active
        self.repo_patches: list[tuple[str, dict]] = []

    async def list_tasks_by_status(self, status: TaskStatus, limit: int = 200) -> list[dict]:
        if status == TaskStatus.QUEUED:
            return list(self.queued)
        if status == TaskStatus.IN_PROGRESS:
            return list(self.active)
        return []

    async def patch_task(self, task_id: str, updates: dict[str, object]) -> dict[str, object]:
        self.repo_patches.append((task_id, updates))
        return {"task_id": task_id, **updates}


class _FakeRateMonitor:
    async def can_launch_ticket(self, ticket: dict, active_tickets: list[dict]) -> tuple[bool, None]:
        return True, None


class ResumeQueuedTicketsTests(unittest.IsolatedAsyncioTestCase):
    async def test_resumes_only_one_ticket_per_project_per_cycle(self) -> None:
        queued = [
            {"task_id": "t1", "project": "melanyryr", "status": "queued"},
            {"task_id": "t2", "project": "melanyryr", "status": "queued"},
        ]
        service = object.__new__(TaskService)
        service._rate_monitor = _FakeRateMonitor()
        service._repo = _FakeRepo(queued=queued, active=[])
        patched: list[str] = []
        launched: list[str] = []

        service._task_allows_autorun = lambda ticket: True

        async def fake_patch_task(task_id: str, updates: dict[str, object]) -> dict[str, object]:
            patched.append(task_id)
            return {"task_id": task_id, **updates}

        async def fake_run_task(task_id: str) -> None:
            launched.append(task_id)

        service.patch_task = fake_patch_task
        service.run_task = fake_run_task

        scheduled: list[asyncio.coroutines] = []

        def fake_create_task(coro):
            scheduled.append(coro)
            coro.close()
            return None

        with patch("task_service.asyncio.create_task", side_effect=fake_create_task):
            resumed = await TaskService.resume_queued_tickets(service)

        self.assertEqual(resumed, 1)
        self.assertEqual(patched, ["t1"])
        self.assertEqual(len(scheduled), 1)
