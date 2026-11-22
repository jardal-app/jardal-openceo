"""Module tests for the vendored ui-ux-pro-max core engine (TASK-2026-00896).

Lives beside the script because the catalog loader requires every
``scripts/**/*.py`` to have its ``test_<stem>.py``. Runs standalone::

    pytest skills/dev/ui-ux-pro-max/scripts/test_core.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core  # noqa: E402


class CoreSearchTests(unittest.TestCase):
    def test_search_returns_domain_rows(self) -> None:
        out = core.search("keyboard focus modal", "ux", 3)
        self.assertEqual("ux", out["domain"])
        self.assertGreater(out["count"], 0)
        self.assertIn("Do", out["results"][0])

    def test_search_stack_returns_guidelines(self) -> None:
        out = core.search_stack("suspense streaming bundle", "nextjs", 2)
        self.assertEqual("nextjs", out["stack"])
        self.assertGreater(out["count"], 0)

    def test_detect_domain_routes_accessibility(self) -> None:
        self.assertEqual("ux", core.detect_domain("keyboard focus modal"))

    def test_unknown_stack_is_an_error_not_a_crash(self) -> None:
        out = core.search_stack("anything", "no-such-stack", 3)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
