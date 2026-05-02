from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.session import DevSessionManager
from config_loader import ConfigLoader
from settings import Settings


class ConfigLoaderContextPathTests(unittest.TestCase):
    def test_context_path_uses_runtime_directory(self) -> None:
        settings = Settings(PROJECT_CONTEXTS_DIR="/tmp/openceo-test-contexts")
        loader = ConfigLoader(settings)

        path = loader.context_path("melanyryr")

        self.assertEqual(path, Path("/tmp/openceo-test-contexts/melanyryr/CONTEXT.md"))


class UpdateFilesStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_files_is_best_effort_when_context_is_read_only(self) -> None:
        settings = Settings()
        manager = DevSessionManager(settings, ConfigLoader(settings))
        manager._append_text = lambda _path, _text: (_ for _ in ()).throw(OSError(30, "Read-only file system"))

        step = await manager._step_update_files({"project": "melanyryr", "task_id": "t-1", "title": "demo", "description": "desc"}, {})

        self.assertEqual(step["step"], "update_files")
        self.assertEqual(step["status"], "skipped")
        self.assertIn("CONTEXT.md unavailable", step["detail"])
