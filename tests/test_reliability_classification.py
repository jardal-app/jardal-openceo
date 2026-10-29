"""Ordered attribution rules A1-A10 (design v2 section 6.2).

Pure unit tests, no network, no CLI, no LLM (epic TASK-2026-00365).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability.classification import (  # noqa: E402
    REASON_CODE_MAP_VERSION,
    classify,
    reason_code_attribution,
)
from reliability.models import Attribution, IncidentKind  # noqa: E402

MOTOR_FRAMES = [
    {"module": "openceo-core.session", "function": "step",
     "line": "1", "file": "openceo-core/session.py"},
]


class ClassificationTests(unittest.TestCase):
    def test_a1_invariant_violation_is_engine(self) -> None:
        self.assertEqual(
            classify(IncidentKind.INVARIANT_VIOLATION),
            (Attribution.ENGINE, "A1"),
        )

    def test_a2_transient_provider_error_is_environment(self) -> None:
        self.assertEqual(
            classify(IncidentKind.EXCEPTION, exc_type="RateLimitError",
                     exc_text="rate limit exceeded", frames=MOTOR_FRAMES),
            (Attribution.ENVIRONMENT, "A2"),
        )

    def test_a3_motor_frame_is_engine(self) -> None:
        self.assertEqual(
            classify(IncidentKind.EXCEPTION, exc_type="ValueError",
                     exc_text="bad value", frames=MOTOR_FRAMES),
            (Attribution.ENGINE, "A3"),
        )
        runner_frames = [
            {"module": "automatico", "function": "main",
             "line": "1", "file": "automatico.py"},
        ]
        self.assertEqual(
            classify(IncidentKind.EXCEPTION, exc_type="ValueError",
                     exc_text="bad value", frames=runner_frames),
            (Attribution.ENGINE, "A3"),
        )

    def test_str_kind_is_coerced_to_enum(self) -> None:
        self.assertEqual(
            classify("exception", exc_type="ValueError",
                     exc_text="bad value", frames=MOTOR_FRAMES),
            classify(IncidentKind.EXCEPTION, exc_type="ValueError",
                     exc_text="bad value", frames=MOTOR_FRAMES),
        )
        self.assertEqual(
            classify("not_a_kind"),
            (Attribution.UNKNOWN, "A10"),
        )

    def test_auth_error_is_not_transient(self) -> None:
        self.assertEqual(
            classify(IncidentKind.EXCEPTION, exc_type="AuthError",
                     exc_text="invalid api key", frames=MOTOR_FRAMES),
            (Attribution.ENGINE, "A3"),
        )

    def test_a4_deadline_is_engine(self) -> None:
        self.assertEqual(
            classify(IncidentKind.DEADLINE_MISSED),
            (Attribution.ENGINE, "A4"),
        )

    def test_a5_oom_is_environment_other_is_engine(self) -> None:
        self.assertEqual(
            classify(IncidentKind.UNCLEAN_EXIT, service_result="OOMKilled"),
            (Attribution.ENVIRONMENT, "A5"),
        )
        self.assertEqual(
            classify(IncidentKind.UNCLEAN_EXIT, service_result="exit 1"),
            (Attribution.ENGINE, "A5"),
        )

    def test_a6_provider_brake_is_environment(self) -> None:
        self.assertEqual(
            classify(IncidentKind.BRAKE_EXHAUSTED, brake="provider_recovery"),
            (Attribution.ENVIRONMENT, "A6"),
        )
        self.assertEqual(
            classify(IncidentKind.BRAKE_EXHAUSTED, brake="scheduled_wait"),
            (Attribution.ENVIRONMENT, "A6"),
        )
        for brake in ("transient", "quota", "review_quorum"):
            self.assertEqual(
                classify(IncidentKind.BRAKE_EXHAUSTED, brake=brake),
                (Attribution.ENVIRONMENT, "A6"),
            )

    def test_a7_gate_brake_is_task(self) -> None:
        self.assertEqual(
            classify(IncidentKind.BRAKE_EXHAUSTED, brake="gates"),
            (Attribution.TASK, "A7"),
        )

    def test_a8_continue_stall_is_engine(self) -> None:
        self.assertEqual(
            classify(IncidentKind.STALL, result_signature="TICKET_CONTINUE"),
            (Attribution.ENGINE, "A8"),
        )

    def test_a9_terminal_failure_uses_versioned_map(self) -> None:
        self.assertGreaterEqual(REASON_CODE_MAP_VERSION, 1)
        for code, expected in (("auth denied", Attribution.ENVIRONMENT),
                               ("pytest test_x failed", Attribution.PROJECT),
                               ("quota exceeded", Attribution.ENVIRONMENT)):
            self.assertEqual(
                classify(IncidentKind.TERMINAL_FAILURE, reason_code=code),
                (expected, "A9"),
            )
            self.assertEqual(reason_code_attribution(code), expected)
        self.assertIsNone(reason_code_attribution("mystery"))
        self.assertEqual(
            classify(IncidentKind.TERMINAL_FAILURE, reason_code="mystery"),
            (Attribution.UNKNOWN, "A9"),
        )

    def test_a10_covers_escape_and_leftovers(self) -> None:
        self.assertEqual(
            classify(IncidentKind.ESCAPE),
            (Attribution.UNKNOWN, "A10"),
        )
        self.assertEqual(
            classify(IncidentKind.STALL, result_signature="weird"),
            (Attribution.UNKNOWN, "A10"),
        )

    def test_free_text_never_changes_attribution(self) -> None:
        probe = {"kind": IncidentKind.EXCEPTION, "exc_type": "ValueError",
                 "exc_text": "bad", "frames": MOTOR_FRAMES}
        expected = classify(**probe)
        for noise in ("auth quota rate limit oom killed provider gate",
                      "this is clearly an environment issue, trust me",
                      "engine bug " * 50):
            self.assertEqual(classify(**probe, free_text=noise), expected)


if __name__ == "__main__":
    unittest.main()
