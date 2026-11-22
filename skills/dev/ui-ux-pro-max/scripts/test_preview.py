"""Module tests for the vendored ui-ux-pro-max preview (TASK-2026-00896).

Lives beside the script because the catalog loader requires every
``scripts/**/*.py`` to have its ``test_<stem>.py``. Runs standalone::

    pytest skills/dev/ui-ux-pro-max/scripts/test_preview.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import design_system  # noqa: E402
import preview  # noqa: E402


class PreviewTests(unittest.TestCase):
    def test_format_renders_standalone_html(self) -> None:
        system = design_system.generate_design_system(
            "internal analytics dashboard", "Ops Console")["design_system"]
        html = preview.format_preview_html(system)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("Ops Console", html)
        self.assertIn("Palette", html)

    def test_write_respects_output_dir(self) -> None:
        system = design_system.generate_design_system(
            "internal analytics dashboard", "Ops Console")["design_system"]
        with tempfile.TemporaryDirectory() as tmp:
            out = preview.write_preview_html(system, output_dir=tmp)
            self.assertEqual("success", out["status"])
            self.assertTrue(Path(tmp, "preview.html").is_file())


if __name__ == "__main__":
    unittest.main()
