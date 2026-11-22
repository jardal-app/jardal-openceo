"""Module tests for the vendored ui-ux-pro-max rules grammar (TASK-2026-00896).

Lives beside the script because the catalog loader requires every
``scripts/**/*.py`` to have its ``test_<stem>.py``. Runs standalone::

    pytest skills/dev/ui-ux-pro-max/scripts/test_reasoning_contract.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import reasoning_contract as rc  # noqa: E402


class ReasoningContractTests(unittest.TestCase):
    def test_parse_and_apply_round_trip(self) -> None:
        rules = rc.parse_decision_rules(
            '{"must_have": ["constraint:no-emoji"],'
            ' "if_dashboard": ["mode:dark"]}')
        applied = rc.apply_decision_rules(rules, "sales dashboard")
        self.assertIn("no-emoji", applied["constraints"])
        self.assertEqual("dark", applied["mode"])

    def test_unknown_condition_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rc.parse_decision_rules('{"if_nope": ["mode:dark"]}')


if __name__ == "__main__":
    unittest.main()
