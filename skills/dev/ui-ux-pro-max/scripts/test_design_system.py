"""Module tests for the vendored ui-ux-pro-max generator (TASK-2026-00896).

Lives beside the script because the catalog loader requires every
``scripts/**/*.py`` to have its ``test_<stem>.py``. Runs standalone::

    pytest skills/dev/ui-ux-pro-max/scripts/test_design_system.py

Never persists: generation without ``persist=True`` writes no files.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import design_system  # noqa: E402


class DesignSystemTests(unittest.TestCase):
    def test_generate_returns_all_sections(self) -> None:
        out = design_system.generate_design_system(
            "internal analytics dashboard", "Ops Console")
        system = out["design_system"]
        self.assertEqual("Ops Console", system["project_name"])
        for section in ("pattern", "style", "colors", "typography"):
            self.assertIn(section, system)
        self.assertTrue(system["colors"]["primary"].startswith("#"))
        self.assertIn("Ops Console", out["text"])
        self.assertIsNone(out["persistence"])

    def test_tokens_export_is_dtcg(self) -> None:
        out = design_system.generate_design_system(
            "internal analytics dashboard", token_format="dtcg")
        tokens = out["tokens"]
        self.assertIn("color", tokens)
        self.assertIn("semantic", tokens)
        self.assertIn("$value", tokens["color"]["primary"])

    def test_safe_slug_never_escapes(self) -> None:
        self.assertEqual("etc", design_system.safe_slug("../../etc"))
        self.assertEqual("default", design_system.safe_slug(""))


if __name__ == "__main__":
    unittest.main()
