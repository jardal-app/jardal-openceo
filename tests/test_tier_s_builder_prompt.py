from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

motor_module = types.ModuleType("motor")
motor_asyncio_module = types.ModuleType("motor.motor_asyncio")
pymongo_module = types.ModuleType("pymongo")
pymongo_errors_module = types.ModuleType("pymongo.errors")


class _DummyAsyncIOMotorClient:
    pass


class _DummyDuplicateKeyError(Exception):
    pass


motor_asyncio_module.AsyncIOMotorClient = _DummyAsyncIOMotorClient
pymongo_module.ASCENDING = 1
pymongo_module.DESCENDING = -1
pymongo_module.ReturnDocument = object()
pymongo_errors_module.DuplicateKeyError = _DummyDuplicateKeyError
sys.modules.setdefault("motor", motor_module)
sys.modules.setdefault("motor.motor_asyncio", motor_asyncio_module)
sys.modules.setdefault("pymongo", pymongo_module)
sys.modules.setdefault("pymongo.errors", pymongo_errors_module)

if "libtmux" not in sys.modules:
    sys.modules["libtmux"] = ModuleType("libtmux")


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.consensus import CriticalConsensusOrchestrator


class BuilderPromptGuardsTests(unittest.TestCase):
    def _prompt(self, kind: str) -> str:
        orch = object.__new__(CriticalConsensusOrchestrator)
        paths = SimpleNamespace(dir=Path("/repo/.tier-s"))
        if kind == "build":
            return CriticalConsensusOrchestrator._builder_prompt(orch, 1, paths)
        return CriticalConsensusOrchestrator._builder_fix_prompt(orch, 1, 1, paths, labels=["qwen-max"])

    def test_build_prompt_forbids_git_writes(self) -> None:
        """A builder commit hides its work from the diff-based snapshot and
        stalls the run with 'no produjo cambios verificables'."""
        prompt = self._prompt("build")
        self.assertIn("Do NOT commit, push, rebase, stash, or change branches", prompt)
        self.assertIn("BUILD_DONE_1", prompt)

    def test_fix_prompt_forbids_git_writes(self) -> None:
        prompt = self._prompt("fix")
        self.assertIn("Do NOT commit, push, rebase, stash, or change branches", prompt)
        self.assertIn("FIX_DONE_1_1", prompt)
