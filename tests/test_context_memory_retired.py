"""Contract: CONTEXT.md is retired as runtime memory (TASK-2026-00143).

Session completion is recorded in Mongo ``dev_sessions``; Tier S flows write
``HANDOFF.md``. No writer, path helper, setting, template or preset may bring
the legacy per-project ``CONTEXT.md`` back.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE_DIR = REPO / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.session import DevSessionManager
from config_loader import ConfigLoader
from dev_session_service import DevSessionService
from ops.create_project import _presets_yaml
from settings import Settings


class ContextMemoryRetiredTests(unittest.TestCase):
    def test_config_loader_has_no_context_path(self) -> None:
        self.assertFalse(hasattr(ConfigLoader, "context_path"))
        self.assertFalse(hasattr(ConfigLoader, "_resolve_project_context_root"))

    def test_settings_have_no_context_memory_fields(self) -> None:
        self.assertNotIn("DEV_CONTEXT_MAX_CHARS", Settings.model_fields)
        self.assertNotIn("PROJECT_CONTEXTS_DIR", Settings.model_fields)

    def test_session_runners_have_no_context_writers(self) -> None:
        self.assertFalse(hasattr(DevSessionManager, "_step_update_files"))
        self.assertFalse(hasattr(DevSessionService, "_finalize_project_memory"))

    def test_new_project_template_avoids_context_file(self) -> None:
        template = _presets_yaml()
        self.assertNotIn("CONTEXT.md", template)
        self.assertIn("STATUS.md", template)
        self.assertIn("ticket Jardal ERP", template)

    def test_live_presets_do_not_reference_context_file(self) -> None:
        yamls = sorted((REPO / "projects").glob("*/project.yaml"))
        self.assertGreater(len(yamls), 0)
        for project_yaml in yamls:
            text = project_yaml.read_text(encoding="utf-8")
            self.assertNotIn("CONTEXT.md", text, f"stale CONTEXT.md in {project_yaml.name}")

    def test_no_project_context_files_remain(self) -> None:
        self.assertEqual(sorted((REPO / "projects").glob("*/CONTEXT.md")), [])
