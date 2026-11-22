"""Module tests for the vendored ui-ux-pro-max search CLI (TASK-2026-00896).

Lives beside the script because the catalog loader requires every
``scripts/**/*.py`` to have its ``test_<stem>.py``. Runs standalone::

    pytest skills/dev/ui-ux-pro-max/scripts/test_search.py

Offline by construction: the CLI only reads the vendored ``data/*.csv``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "search.py"


def _run(*argv: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[:500]
    return json.loads(proc.stdout)


class SearchCliTests(unittest.TestCase):
    def test_domain_query_hits_the_csv(self) -> None:
        payload = _run("keyboard focus modal", "--domain", "ux", "--json")
        self.assertEqual("ux", payload["domain"])
        self.assertGreater(payload["count"], 0)
        issues = [row.get("Issue", "") for row in payload["results"]]
        self.assertTrue(any("Focus" in issue for issue in issues),
                        f"no focus issue in {issues}")

    def test_stack_query_hits_the_stack_csv(self) -> None:
        payload = _run("suspense streaming bundle", "--stack", "nextjs",
                       "--json")
        self.assertEqual("nextjs", payload["stack"])
        self.assertGreater(payload["count"], 0)

    def test_unknown_query_reports_no_match(self) -> None:
        payload = _run("zzzqqq nonexistent term xyz", "--domain", "ux",
                       "--json")
        self.assertEqual(0, payload["count"])


if __name__ == "__main__":
    unittest.main()
