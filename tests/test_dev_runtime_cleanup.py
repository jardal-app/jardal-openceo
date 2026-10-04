from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

if "fastmcp" not in sys.modules:
    fastmcp_stub = ModuleType("fastmcp")

    class _FastMCPStub:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def tool(self, *args, **kwargs):
            return None

    fastmcp_stub.FastMCP = _FastMCPStub
    sys.modules["fastmcp"] = fastmcp_stub

if "libtmux" not in sys.modules:
    libtmux_stub = ModuleType("libtmux")
    sys.modules["libtmux"] = libtmux_stub

if "motor" not in sys.modules:
    motor_stub = ModuleType("motor")
    motor_asyncio_stub = ModuleType("motor.motor_asyncio")

    class _AsyncIOMotorClientStub:
        def __init__(self, *args, **kwargs) -> None:
            pass

    motor_asyncio_stub.AsyncIOMotorClient = _AsyncIOMotorClientStub
    motor_stub.motor_asyncio = motor_asyncio_stub
    sys.modules["motor"] = motor_stub
    sys.modules["motor.motor_asyncio"] = motor_asyncio_stub

from agents.dev.session import DevExecutionOutput, DevSessionManager
from config_loader import ConfigLoader
from graph import OpenCEOGraph
from settings import Settings
from state import CodingTool, CodingTier, TaskStatus
from task_service import TaskService


class DevPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo")
        self.manager = DevSessionManager(settings, ConfigLoader(settings))

    def test_prompt_includes_ticket_metadata_and_acceptance_criteria_without_git_workflow(self) -> None:
        task = {
            "ticket_number": 24,
            "title": "Recalcular saldos automáticamente al editar una venta",
            "description": "Toda modificación de una venta debe actualizar automáticamente el stock afectado.",
            "params": {
                "pm_ticket": {
                    "module": "Ventas / Inventario",
                    "type": "Feature",
                    "acceptance_criteria": [
                        "El saldo se recalcula automáticamente al editar una venta",
                        "No quedan desfases de inventario después de guardar",
                    ],
                }
            },
            "attachments": [],
        }

        prompt = self.manager._build_coding_prompt(task, {})

        self.assertIn("Ticket: #24", prompt)
        self.assertIn("Módulo: Ventas / Inventario", prompt)
        self.assertIn("Tipo: Feature", prompt)
        self.assertIn("Criterios de aceptación:", prompt)
        self.assertIn("1. El saldo se recalcula automáticamente al editar una venta", prompt)
        self.assertIn("2. No quedan desfases de inventario después de guardar", prompt)
        self.assertNotIn("WORKFLOW", prompt)
        self.assertNotIn("git checkout", prompt)
        self.assertNotIn("Create a new branch", prompt)


class GraphDevPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_dev_node_forwards_ticket_number_and_params(self) -> None:
        captured: dict[str, object] = {}

        class FakeDevSession:
            async def run(self, task: dict, project_config: dict) -> DevExecutionOutput:
                captured["task"] = task
                return DevExecutionOutput(
                    status=TaskStatus.COMPLETED,
                    steps=[],
                    coding_tier=CodingTier.TIER3,
                    coding_tool=CodingTool.TIER_C,
                )

        graph = OpenCEOGraph.__new__(OpenCEOGraph)
        graph._dev_session = FakeDevSession()

        state = {
            "task_id": "task-1",
            "ticket_number": 24,
            "project": "melanyryr",
            "title": "Ticket",
            "description": "Descripcion",
            "attachments": ["a.txt"],
            "params": {"pm_ticket": {"module": "Ventas"}},
            "priority": "medium",
            "coding_tier": "tier4",
            "coding_tool": "tier_c",
            "project_config": {},
            "steps": [],
        }

        result = await OpenCEOGraph._dev_node(graph, state)

        forwarded = captured["task"]
        assert isinstance(forwarded, dict)
        self.assertEqual(forwarded["ticket_number"], 24)
        self.assertEqual(forwarded["params"], {"pm_ticket": {"module": "Ventas"}})
        self.assertEqual(result["status"], TaskStatus.COMPLETED.value)


class TaskRerunStateResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_task_replaces_stale_steps_on_rerun(self) -> None:
        class FakeRepo:
            def __init__(self) -> None:
                self.task = {
                    "task_id": "task-1",
                    "project": "melanyryr",
                    "agent": "dev",
                    "agent_hint": "dev",
                    "source": "api",
                    "title": "Ticket",
                    "description": "Descripcion",
                    "attachments": [],
                    "priority": "medium",
                    "status": TaskStatus.FAILED.value,
                    "steps": [{"step": "branch_setup", "status": "ok", "detail": "old"}],
                    "last_prompt_sent": "old prompt",
                    "last_response_summary": "old summary",
                    "error": "old error",
                }

            async def get_task(self, task_id: str):
                if task_id != "task-1":
                    return None
                return dict(self.task)

            async def patch_task(self, task_id: str, updates: dict):
                self.task.update(updates)
                return dict(self.task)

        class FakeHub:
            async def broadcast(self, event: str, payload: dict) -> None:
                return None

        class FakeGraph:
            async def execute(self, task: dict, agent_hint: str | None = None) -> dict:
                return {
                    "selected_agent": "dev",
                    "status": TaskStatus.COMPLETED.value,
                    "steps": [{"step": "code", "status": "ok", "detail": "new"}],
                    "last_prompt_sent": "new prompt",
                    "last_response_summary": "new summary",
                    "error": None,
                }

        service = TaskService(
            settings=Settings(),
            repo=FakeRepo(),
            graph=FakeGraph(),
            realtime_hub=FakeHub(),
            notion=None,
            rate_monitor=None,
        )

        updated = await service.run_task("task-1", force=True)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated["steps"], [{"step": "code", "status": "ok", "detail": "new"}])
        self.assertEqual(updated["last_prompt_sent"], "new prompt")
        self.assertEqual(updated["last_response_summary"], "new summary")
        self.assertIsNone(updated["error"])


if __name__ == "__main__":
    unittest.main()
