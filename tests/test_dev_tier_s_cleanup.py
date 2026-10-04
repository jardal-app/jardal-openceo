from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import ModuleType

motor_module = types.ModuleType("motor")
motor_asyncio_module = types.ModuleType("motor.motor_asyncio")
pymongo_module = types.ModuleType("pymongo")
pymongo_errors_module = types.ModuleType("pymongo.errors")


class _DummyAsyncIOMotorClient:
    pass


class _DummyDuplicateKeyError(Exception):
    pass


class _DummyBulkWriteError(Exception):
    pass


motor_asyncio_module.AsyncIOMotorClient = _DummyAsyncIOMotorClient
pymongo_module.ASCENDING = 1
pymongo_module.DESCENDING = -1
pymongo_module.ReturnDocument = object()
pymongo_errors_module.DuplicateKeyError = _DummyDuplicateKeyError
pymongo_errors_module.BulkWriteError = _DummyBulkWriteError
sys.modules.setdefault("motor", motor_module)
sys.modules.setdefault("motor.motor_asyncio", motor_asyncio_module)
sys.modules.setdefault("pymongo", pymongo_module)
sys.modules.setdefault("pymongo.errors", pymongo_errors_module)

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
    sys.modules["libtmux"] = ModuleType("libtmux")


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from dev_session_service import DevSessionService


class _FakeRepo:
    def __init__(self, live_tier_s_projects: set[str]) -> None:
        self.live_tier_s_projects = live_tier_s_projects

    async def list_dev_sessions(self, **kwargs) -> list[dict]:
        return []

    async def live_tier_s_run_for_project(self, project: str) -> dict | None:
        if project in self.live_tier_s_projects:
            return {"run_id": "r1", "project": project, "active_repo": "/repo"}
        return None


class _FakeTmux:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.killed: list[str] = []

    def reconcile(self, active_sessions: list[dict]) -> dict:
        return {}

    def all_session_names(self) -> list[str]:
        return list(self.names)

    def close_session(self, project: str, tmux_session_name: str | None = None) -> None:
        self.killed.append(tmux_session_name or project)


class TierSCleanupSparesLiveRunsTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, names: list[str], live: set[str]) -> tuple[DevSessionService, _FakeTmux]:
        service = object.__new__(DevSessionService)
        tmux = _FakeTmux(names)
        service._repo = _FakeRepo(live)
        service._tmux = tmux
        return service, tmux

    async def test_kills_true_orphan_matching_tier_s_pattern(self) -> None:
        service, tmux = self._service(["dev-nametest-planner"], set())
        await DevSessionService.cleanup_runtime_orphans(service)
        self.assertEqual(tmux.killed, ["dev-nametest-planner"])

    async def test_spares_planner_backed_by_live_tier_s_run(self) -> None:
        """Un run Tier S vivo (active_repo set, aunque esté waiting) conserva
        su sesión: matar la TUI rompe el ciclo y el resume la reutiliza."""
        service, tmux = self._service(
            ["dev-jardal-openceo-planner", "dev-nametest-planner"],
            {"jardal-openceo"},
        )
        await DevSessionService.cleanup_runtime_orphans(service)
        self.assertEqual(tmux.killed, ["dev-nametest-planner"])

    async def test_ignores_non_tier_s_session_names(self) -> None:
        service, tmux = self._service(
            ["dev-monitor-claude", "dev-nametest-xyz", "openceo", "agentboard"],
            set(),
        )
        await DevSessionService.cleanup_runtime_orphans(service)
        self.assertEqual(tmux.killed, [])
