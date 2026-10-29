"""Reliability base models: enums, severities and Problem states.

Pure unit tests, no network, no CLI, no LLM (epic TASK-2026-00365).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability.models import (  # noqa: E402
    Attribution,
    Component,
    IncidentKind,
    ProblemStatus,
    Severity,
    default_severity,
)


class ModelsTests(unittest.TestCase):
    def test_kinds_cover_design_section_5_1(self) -> None:
        self.assertEqual(
            {kind.value for kind in IncidentKind},
            {"exception", "terminal_failure", "brake_exhausted", "stall",
             "invariant_violation", "deadline_missed", "unclean_exit", "escape"},
        )

    def test_components_cover_design_section_5_1(self) -> None:
        self.assertEqual(len(Component), 13)
        for name in ("dispatcher", "dev_session", "tier_s", "generic_agent",
                     "runner", "review", "gates", "provider", "business_sync",
                     "objectives", "ceo_loop", "core_process", "reliability"):
            self.assertIn(name, {component.value for component in Component})

    def test_attributions_cover_design_section_6(self) -> None:
        self.assertEqual(
            {attribution.value for attribution in Attribution},
            {"engine", "environment", "project", "task", "unknown"},
        )

    def test_problem_states_cover_design_section_13_2(self) -> None:
        self.assertEqual(
            {status.value for status in ProblemStatus},
            {"open", "diagnosing", "diagnosed", "needs_split", "ticketed",
             "fix_merged", "verifying", "resolved", "regressed", "no_action",
             "rejected", "muted", "superseded"},
        )

    def test_exception_severity_follows_motor_frame(self) -> None:
        self.assertEqual(
            default_severity(IncidentKind.EXCEPTION, motor_frame=True),
            Severity.HIGH,
        )
        self.assertEqual(
            default_severity(IncidentKind.EXCEPTION, motor_frame=False),
            Severity.NORMAL,
        )

    def test_g3_stall_is_normal_others_high(self) -> None:
        self.assertEqual(
            default_severity(IncidentKind.STALL, guard_rule="G3"),
            Severity.NORMAL,
        )
        self.assertEqual(
            default_severity(IncidentKind.STALL, guard_rule="G1"),
            Severity.HIGH,
        )

    def test_invariant_violation_is_critical(self) -> None:
        self.assertEqual(
            default_severity(IncidentKind.INVARIANT_VIOLATION),
            Severity.CRITICAL,
        )


if __name__ == "__main__":
    unittest.main()
