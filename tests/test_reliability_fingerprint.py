"""Stable v1 fingerprints (design v2 section 6.1).

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
)
from reliability.fingerprint import (  # noqa: E402
    FINGERPRINT_VERSION,
    fingerprint_deadline,
    fingerprint_escape,
    fingerprint_exception,
    fingerprint_invariant,
    fingerprint_stall,
    fingerprint_terminal,
    fingerprint_unclean,
    normalize_reason_code,
    problem_key,
)
from reliability.models import Component, IncidentKind  # noqa: E402


def _frames() -> list[dict[str, str]]:
    return [
        {"module": "openceo-core.dispatcher", "function": "run",
         "line": "10", "file": "openceo-core/dispatcher.py"},
        {"module": "openceo-core.session", "function": "step",
         "line": "20", "file": "openceo-core/session.py"},
    ]


class FingerprintTests(unittest.TestCase):
    def test_exception_ignores_lines_paths_message(self) -> None:
        first = fingerprint_exception("ValueError", _frames(), "dev_session")
        moved = [
            {"module": "openceo-core.dispatcher", "function": "run",
             "line": "999", "file": "/home/a/repo/openceo-core/dispatcher.py"},
            {"module": "openceo-core.session", "function": "step",
             "line": "1", "file": "/tmp/b/openceo-core/session.py"},
        ]
        self.assertEqual(
            first, fingerprint_exception("ValueError", moved, "dev_session"))

    def test_exception_uses_last_five_in_app_frames(self) -> None:
        frames = [
            {"module": f"openceo-core.m{i}", "function": "f",
             "line": "1", "file": f"openceo-core/m{i}.py"}
            for i in range(8)
        ]
        self.assertEqual(
            fingerprint_exception("E", frames, "c"),
            fingerprint_exception("E", frames[-5:], "c"),
        )

    def test_exception_without_in_app_uses_last_packages(self) -> None:
        frames = [{"module": "httpx._client", "function": "send",
                   "line": "1", "file": "site/httpx.py"}]
        self.assertEqual(
            fingerprint_exception("E", frames, "c"),
            fingerprint_exception("E", frames, "c"),
        )
        self.assertNotEqual(
            fingerprint_exception("E", frames, "c"),
            fingerprint_exception("E", frames, "other"),
        )
        many = [{"module": f"pkg{i}.mod", "function": "f",
                 "line": "1", "file": f"site/pkg{i}.py"} for i in range(5)]
        self.assertEqual(
            fingerprint_exception("E", many, "c"),
            fingerprint_exception("E", many[-3:], "c"),
        )
        self.assertNotEqual(
            fingerprint_exception("E", many, "c"),
            fingerprint_exception("E", many[:3], "c"),
        )

    def test_enum_and_str_inputs_share_fingerprint(self) -> None:
        self.assertEqual(
            fingerprint_terminal(IncidentKind.TERMINAL_FAILURE,
                                 Component.DISPATCHER, "build",
                                 "gate failed", None),
            fingerprint_terminal("terminal_failure", "dispatcher", "build",
                                 "gate failed", None),
        )
        self.assertEqual(
            fingerprint_exception("ValueError", _frames(),
                                  Component.DEV_SESSION),
            fingerprint_exception("ValueError", _frames(), "dev_session"),
        )

    def test_reason_code_normalization(self) -> None:
        self.assertEqual(
            normalize_reason_code("Error 429 at 2026-10-29T05:00:00"),
            "error <n> at <time>",
        )
        code = normalize_reason_code("x" * 200)
        self.assertLessEqual(len(code), 120)
        self.assertEqual(
            normalize_reason_code("Ticket TASK-2026-00368 failed"),
            "ticket task-<n> failed",
        )
        self.assertEqual(
            normalize_reason_code("failed reading /var/log/2026/run.log"),
            "failed reading <path>",
        )

    def test_terminal_covers_matrix_row(self) -> None:
        left = fingerprint_terminal(
            "terminal_failure", "dispatcher", "build", "gate failed", None)
        self.assertEqual(
            left,
            fingerprint_terminal(
                "terminal_failure", "dispatcher", "build",
                "GATE FAILED", None),
        )
        self.assertNotEqual(
            left,
            fingerprint_terminal(
                "terminal_failure", "dispatcher", "build", "timeout", None),
        )

    def test_stall_normalizes_volatile_signatures(self) -> None:
        first = fingerprint_stall(
            "c", "build", "g1",
            "TICKET_CONTINUE run TASK-2026-00368 at 2026-10-29T05:00")
        self.assertEqual(
            first,
            fingerprint_stall(
                "c", "build", "G1",
                "ticket_continue run task-2026-00369 at 2026-10-29T05:01"),
        )
        self.assertNotEqual(
            first,
            fingerprint_stall("c", "build", "g1", "TICKET_DONE"),
        )

    def test_terminal_binds_reason_code_map_version(self) -> None:
        self.assertGreaterEqual(REASON_CODE_MAP_VERSION, 1)
        plain = fingerprint_terminal(
            "terminal_failure", "dispatcher", "build", "gate failed", None)
        self.assertEqual(
            plain,
            fingerprint_terminal(
                "terminal_failure", "dispatcher", "build",
                "GATE FAILED", None),
        )
        self.assertNotEqual(
            plain,
            fingerprint_terminal(
                "terminal_failure", "dispatcher", "build", "gate failed",
                None, map_version=REASON_CODE_MAP_VERSION + 1),
        )

    def test_remaining_kinds_are_stable(self) -> None:
        self.assertEqual(fingerprint_invariant("inv-001"),
                         fingerprint_invariant("INV-001"))
        self.assertEqual(fingerprint_deadline("c", "attempt"),
                         fingerprint_deadline("c", "attempt"))
        self.assertNotEqual(fingerprint_deadline("c", "attempt"),
                            fingerprint_deadline("c", "resume"))
        self.assertEqual(fingerprint_unclean("OOM"),
                         fingerprint_unclean("oom"))
        self.assertEqual(fingerprint_escape("revert", "tier_s", "quorum"),
                         fingerprint_escape("revert", "tier_s", "quorum"))
        self.assertNotEqual(fingerprint_escape("revert", "tier_s", "quorum"),
                            fingerprint_escape("revert", "tier_a", "quorum"))

    def test_problem_key_binds_version(self) -> None:
        key = problem_key("engine", "abc")
        self.assertEqual(key, problem_key("engine", "abc", FINGERPRINT_VERSION))
        self.assertNotEqual(key, problem_key("engine", "abc", FINGERPRINT_VERSION + 1))
        self.assertNotEqual(key, problem_key("project:x", "abc"))


if __name__ == "__main__":
    unittest.main()
