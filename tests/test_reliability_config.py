"""Reliability global config: defaults, fail-closed validation, invariants.

Pure unit tests, no network, no CLI, no LLM (epic TASK-2026-00365).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability.config import (  # noqa: E402
    HUNTER_TICKET_AUTORUN,
    RELIABILITY_DEFAULTS,
    SELF_DIAGNOSIS_EXCLUDED,
    get_reliability_config,
    validate_reliability_config,
)


class ReliabilityConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self) -> None:
        self.assertEqual(validate_reliability_config(RELIABILITY_DEFAULTS), [])

    def test_unknown_keys_fail_closed(self) -> None:
        errors = validate_reliability_config({**RELIABILITY_DEFAULTS,
                                              "autopatch": True})
        self.assertTrue(any("autopatch" in error for error in errors))
        nested = {"guard": {**RELIABILITY_DEFAULTS["guard"], "turbo": 1}}
        errors = validate_reliability_config({**RELIABILITY_DEFAULTS, **nested})
        self.assertTrue(any("turbo" in error for error in errors))

    def test_wrong_types_and_modes_fail_closed(self) -> None:
        self.assertTrue(validate_reliability_config("nope"))
        self.assertTrue(validate_reliability_config(
            {**RELIABILITY_DEFAULTS, "capture": "yes"}))
        self.assertTrue(validate_reliability_config(
            {**RELIABILITY_DEFAULTS, "spool_max_bytes": -1}))
        bad_mode = {"guard": {**RELIABILITY_DEFAULTS["guard"],
                              "mode": {"runner": "enforce-now"}}}
        self.assertTrue(validate_reliability_config(
            {**RELIABILITY_DEFAULTS, **bad_mode}))
        bad_producer = {"guard": {**RELIABILITY_DEFAULTS["guard"],
                                  "mode": {"agent_x": "shadow"}}}
        self.assertTrue(validate_reliability_config(
            {**RELIABILITY_DEFAULTS, **bad_producer}))

    def test_effective_config_defaults_without_project_override(self) -> None:
        effective = get_reliability_config({})
        self.assertEqual(effective, RELIABILITY_DEFAULTS)
        self.assertIsNot(effective["guard"], RELIABILITY_DEFAULTS["guard"])

    def test_code_invariants_are_not_configurable(self) -> None:
        self.assertIs(HUNTER_TICKET_AUTORUN, False)
        self.assertIn("reliability", SELF_DIAGNOSIS_EXCLUDED)
        self.assertNotIn("autorun", RELIABILITY_DEFAULTS["tickets"])
        for key in RELIABILITY_DEFAULTS:
            self.assertNotIn("autorun", key)


if __name__ == "__main__":
    unittest.main()
