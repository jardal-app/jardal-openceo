"""ProgressGuard: pure G1-G3 brake and work fingerprint (epic TASK-2026-00365).

Ticket TASK-2026-00373, design v2 sections 4.1-4.4 and 4.6. Pure unit
tests with synthetic temporary git repos: no network, no CLI, no LLM.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability.config import RELIABILITY_DEFAULTS  # noqa: E402
from reliability.guard import (  # noqa: E402
    GENERIC_EMPTY_FINGERPRINT,
    WINDOW_MAX,
    empty_state,
    evaluate,
    generic_agent_fingerprint,
    reset_guard,
    result_signature,
    work_fingerprint,
)

import tempfile  # noqa: E402

NOW = "2026-10-29T06:00:00+00:00"


def policy(**over):
    base = {"mode": "enforce"}
    base.update(over)
    return base


def obs(**over):
    base = {"attempt_id": "a-1", "phase": "building",
            "result_sig": result_signature("building", "TICKET_CONTINUE"),
            "work_fp": "sha256:aaa", "fp_complete": True,
            "executed": True}
    base.update(over)
    return base


def run(state, attempt, **over):
    return evaluate(state, obs(attempt_id=attempt, **over),
                    policy(mode="enforce"), now=NOW)


def make_repo():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=root,
                   check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "app.py").write_text("print(1)\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                          capture_output=True, text=True,
                          check=True).stdout.strip()
    return tmp, root, base


class GuardRulesTests(unittest.TestCase):
    def test_g1_stops_on_third_identical_attempt(self) -> None:
        state = empty_state("building")
        for attempt in ("a-1", "a-2"):
            state, decision = run(state, attempt)
            self.assertEqual(decision["action"], "continue")
            self.assertIsNone(decision["rule"])
        state, decision = run(state, "a-3")
        self.assertEqual(decision["action"], "stop")
        self.assertEqual(decision["rule"], "G1")
        self.assertEqual(decision["incident"]["kind"], "stall")
        self.assertEqual(decision["incident"]["severity"], "high")
        self.assertFalse(decision["incident"]["shadow"])
        self.assertIsNotNone(state["stopped"])
        self.assertEqual(state["stopped"]["rule"], "G1")
        self.assertEqual(state["stopped"]["at"], NOW)

    def test_g1_needs_identical_result_and_work(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "a-1")
        state, decision = run(state, "a-2", work_fp="sha256:bbb")
        self.assertEqual(decision["action"], "continue")
        state, decision = run(
            state, "a-3",
            result_sig=result_signature("building", "bloqueado:cuota"))
        self.assertEqual(decision["action"], "continue")
        self.assertIsNone(state["stopped"])

    def test_g1_ignores_requeued_sessions(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "a-1")
        state, decision = run(state, "q-1", executed=False)
        self.assertEqual(decision["action"], "continue")
        self.assertEqual(len(state["window"]), 1)
        state, _ = run(state, "a-2")
        state, decision = run(state, "a-3")
        self.assertEqual(decision["action"], "stop")

    def test_stopped_state_stays_stopped(self) -> None:
        state = empty_state("building")
        for attempt in ("a-1", "a-2", "a-3"):
            state, _ = run(state, attempt)
        again, decision = run(state, "a-4")
        self.assertEqual(decision["action"], "stop")
        self.assertEqual(decision["rule"], "G1")
        self.assertIsNone(decision["incident"])
        self.assertEqual(len(again["window"]), len(state["window"]))

    def test_g2_stops_on_abab_alternation(self) -> None:
        state = empty_state("building")
        fps = ["sha256:aaa", "sha256:bbb", "sha256:aaa", "sha256:bbb"]
        for index, fp in enumerate(fps[:3]):
            state, decision = run(state, f"a-{index + 1}", work_fp=fp)
            self.assertEqual(decision["action"], "continue")
        state, decision = run(state, "a-4", work_fp=fps[3])
        self.assertEqual(decision["action"], "stop")
        self.assertEqual(decision["rule"], "G2")
        self.assertEqual(decision["incident"]["severity"], "high")

    def test_g2_rejects_non_alternating_pair(self) -> None:
        state = empty_state("building")
        for index, fp in enumerate(["sha256:aaa", "sha256:aaa",
                                    "sha256:bbb", "sha256:bbb"]):
            state, decision = run(state, f"a-{index + 1}", work_fp=fp)
            self.assertEqual(decision["action"], "continue")
            self.assertIsNone(decision["rule"])

    def test_g3_emits_normal_incident_without_stopping(self) -> None:
        state = empty_state("building")
        last = None
        for index in range(8):
            state, last = run(state, f"a-{index + 1}",
                              work_fp=f"sha256:fp-{index}")
            if index < 7:
                self.assertIsNone(last["rule"])
        self.assertEqual(last["action"], "continue")
        self.assertEqual(last["rule"], "G3")
        self.assertEqual(last["incident"]["severity"], "normal")
        self.assertIsNone(state["stopped"])

    def test_incomplete_fingerprint_disables_g1_g2_only(self) -> None:
        state = empty_state("building")
        last = None
        for index in range(8):
            state, last = run(state, f"a-{index + 1}", fp_complete=False)
            if index == 2:
                self.assertIsNone(last["rule"])
        self.assertEqual(last["rule"], "G3")
        self.assertEqual(last["action"], "continue")

    def test_phase_change_starts_a_new_run(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "a-1")
        state, _ = run(state, "a-2")
        state, decision = run(state, "b-1", phase="testing")
        self.assertEqual(decision["action"], "continue")
        self.assertIsNone(decision["rule"])
        state, _ = run(state, "b-2", phase="testing")
        state, decision = run(state, "b-3", phase="testing")
        self.assertEqual(decision["rule"], "G1")

    def test_streak_survives_serialization_across_runs(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "run1-a1")
        state = json.loads(json.dumps(state))
        state, _ = run(state, "run2-a2")
        state = json.loads(json.dumps(state))
        state, decision = run(state, "run3-a3")
        self.assertEqual(decision["action"], "stop")
        self.assertEqual(decision["rule"], "G1")

    def test_new_run_id_comment_or_restart_breaks_nothing(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "run-1-attempt-1")
        state, _ = run(state, "run-2-attempt-9")
        state, decision = run(state, "run-9-attempt-42")
        self.assertEqual(decision["rule"], "G1")


class GuardMilestonesTests(unittest.TestCase):
    def test_milestone_clears_window_and_bumps_generation(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "a-1")
        state, _ = run(state, "a-2")
        state, decision = evaluate(
            state, obs(attempt_id="a-3", milestone="verify:pass"),
            policy(mode="enforce"), now=NOW)
        self.assertEqual(decision["action"], "continue")
        self.assertEqual(state["window"], [])
        self.assertEqual(state["generation"], 1)

    def test_verification_without_diff_counts_as_milestone(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "a-1")
        state, decision = evaluate(
            state, obs(attempt_id="a-2", milestone="verify:pass"),
            policy(mode="enforce"), now=NOW)
        self.assertEqual(decision["action"], "continue")
        state, decision = run(state, "a-3")
        self.assertEqual(len(state["window"]), 1)
        self.assertIsNone(decision["rule"])

    def test_repeated_milestone_counts_as_plain_attempt(self) -> None:
        state = empty_state("building")
        state, _ = evaluate(
            state, obs(attempt_id="a-1", milestone="gate:lint:pass"),
            policy(mode="enforce"), now=NOW)
        state, decision = evaluate(
            state, obs(attempt_id="a-2", milestone="gate:lint:pass"),
            policy(mode="enforce"), now=NOW)
        self.assertEqual(len(state["window"]), 1)
        self.assertEqual(state["generation"], 1)

    def test_reset_re_arms_with_actor_and_reason(self) -> None:
        state = empty_state("building")
        for attempt in ("a-1", "a-2", "a-3"):
            state, _ = run(state, attempt)
        self.assertIsNotNone(state["stopped"])
        state = reset_guard(state, actor="owner", reason="fix merged",
                            now=NOW)
        self.assertIsNone(state["stopped"])
        self.assertEqual(state["window"], [])
        self.assertEqual(state["generation"], 1)
        self.assertEqual(state["reset"],
                         {"actor": "owner", "at": NOW,
                          "reason": "fix merged"})
        state, decision = run(state, "a-4")
        self.assertEqual(decision["action"], "continue")


class GuardModesTests(unittest.TestCase):
    def test_shadow_records_without_stopping(self) -> None:
        state = empty_state("building")
        last = None
        for index in range(3):
            state, last = evaluate(
                state, obs(attempt_id=f"a-{index + 1}"),
                policy(mode="shadow"), now=NOW)
        self.assertEqual(last["action"], "would_stop")
        self.assertEqual(last["rule"], "G1")
        self.assertTrue(last["incident"]["shadow"])
        self.assertEqual(last["incident"]["severity"], "high")
        self.assertIsNone(state["stopped"])

    def test_shadow_g3_stays_continue(self) -> None:
        state = empty_state("building")
        last = None
        for index in range(8):
            state, last = evaluate(
                state, obs(attempt_id=f"a-{index + 1}",
                           work_fp=f"sha256:fp-{index}"),
                policy(mode="shadow"), now=NOW)
        self.assertEqual(last["action"], "continue")
        self.assertEqual(last["rule"], "G3")
        self.assertTrue(last["shadow"])

    def test_off_leaves_state_untouched(self) -> None:
        before = empty_state("building")
        state, decision = evaluate(before, obs(), policy(mode="off"),
                                   now=NOW)
        self.assertEqual(decision["action"], "continue")
        self.assertEqual(state, before)

    def test_off_applies_after_enforce_history(self) -> None:
        state = empty_state("building")
        state, _ = run(state, "a-1")
        frozen = json.loads(json.dumps(state))
        state, decision = evaluate(state, obs(attempt_id="a-2"),
                                   policy(mode="off"), now=NOW)
        self.assertEqual(decision["action"], "continue")
        self.assertEqual(state, frozen)

    def test_unknown_mode_fails_open(self) -> None:
        state, decision = evaluate(empty_state("building"), obs(),
                                   {"mode": "turbo"}, now=NOW)
        self.assertEqual(decision["action"], "continue")
        self.assertTrue(decision["fail_open"])
        self.assertEqual(state["window"], [])

    def test_corrupt_state_fails_open_and_heals(self) -> None:
        for bad in ("garbage", None, 42, {"window": "nope"},
                    {"generation": -1, "window": []}):
            state, decision = evaluate(bad, obs(attempt_id="a-1"),
                                       policy(mode="enforce"), now=NOW)
            self.assertEqual(decision["action"], "continue", bad)
            self.assertTrue(decision["fail_open"], bad)
            self.assertEqual(state["window"][0]["attempt_id"], "a-1")
            self.assertEqual(state["generation"], 0)

    def test_policy_thresholds_apply(self) -> None:
        self.assertEqual(
            RELIABILITY_DEFAULTS["guard"]["same_work_attempts"], 3)
        self.assertEqual(
            RELIABILITY_DEFAULTS["guard"]["alternating_attempts"], 4)
        self.assertEqual(
            RELIABILITY_DEFAULTS["guard"]["long_phase_attempts"], 8)
        state = empty_state("building")
        state, decision = evaluate(
            state, obs(attempt_id="a-1"), policy(mode="enforce"), now=NOW)
        self.assertEqual(decision["action"], "continue")
        state, decision = evaluate(
            state, obs(attempt_id="a-2"),
            {"mode": "enforce", "same_work_attempts": 2}, now=NOW)
        self.assertEqual(decision["rule"], "G1")

    def test_result_signature_normalizes_whitespace(self) -> None:
        self.assertEqual(result_signature("building", "TICKET_CONTINUE"),
                         "building:TICKET_CONTINUE")
        self.assertEqual(result_signature(" building ", "a  b"),
                         result_signature("building", "a b"))


class WorkFingerprintTests(unittest.TestCase):
    def test_same_head_distinct_working_tree(self) -> None:
        tmp, root, base = make_repo()
        try:
            clean = work_fingerprint(root, base)
            self.assertTrue(clean["complete"])
            self.assertIsNone(clean["error"])
            self.assertTrue(clean["fp"].startswith("sha256:"))
            (root / "app.py").write_text("print(2)\n")
            dirty = work_fingerprint(root, base)
            self.assertTrue(dirty["complete"])
            self.assertNotEqual(clean["fp"], dirty["fp"])
            self.assertEqual(clean["head"], dirty["head"])
        finally:
            tmp.cleanup()

    def test_identical_checkout_is_stable(self) -> None:
        tmp, root, base = make_repo()
        try:
            (root / "new.py").write_text("x = 1\n")
            first = work_fingerprint(root, base)
            second = work_fingerprint(root, base)
            self.assertEqual(first["fp"], second["fp"])
            self.assertEqual(first["paths"], 1)
        finally:
            tmp.cleanup()

    def test_excludes_caches_logs_and_openceo(self) -> None:
        tmp, root, base = make_repo()
        try:
            before = work_fingerprint(root, base)
            (root / ".openceo").mkdir()
            (root / ".openceo" / "PLAN.md").write_text("scratch\n")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "mod.pyc").write_bytes(b"\x00" * 64)
            (root / "app.log").write_text("noise\n")
            after = work_fingerprint(root, base)
            self.assertEqual(before["fp"], after["fp"])
            self.assertEqual(after["paths"], 0)
        finally:
            tmp.cleanup()

    def test_commit_moves_head_and_changes_fp(self) -> None:
        tmp, root, base = make_repo()
        try:
            before = work_fingerprint(root, base)
            (root / "notes.txt").write_text("work\n")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "notes"], cwd=root,
                           check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                  capture_output=True, text=True,
                                  check=True).stdout.strip()
            moved = work_fingerprint(root, base)
            self.assertNotEqual(before["fp"], moved["fp"])
            self.assertNotEqual(moved["head"], before["head"])
            self.assertEqual(moved["head"], head)
        finally:
            tmp.cleanup()

    def test_over_one_hundred_paths_is_incomplete(self) -> None:
        tmp, root, base = make_repo()
        try:
            for index in range(101):
                (root / f"f{index}.txt").write_text("x\n")
            result = work_fingerprint(root, base)
            self.assertFalse(result["complete"])
            self.assertGreater(result["paths"], 100)
        finally:
            tmp.cleanup()

    def test_over_two_mib_is_incomplete(self) -> None:
        tmp, root, base = make_repo()
        try:
            (root / "big.bin").write_bytes(b"\x00" * (2 * 1024 * 1024 + 1))
            result = work_fingerprint(root, base)
            self.assertFalse(result["complete"])
            self.assertGreater(result["bytes"], 2 * 1024 * 1024)
        finally:
            tmp.cleanup()

    def test_symlink_inside_repo_resolves(self) -> None:
        tmp, root, base = make_repo()
        try:
            (root / "real.txt").write_text("content\n")
            (root / "link.txt").symlink_to("real.txt")
            result = work_fingerprint(root, base)
            self.assertTrue(result["complete"])
            self.assertEqual(result["paths"], 2)
            (root / "real.txt").write_text("changed\n")
            changed = work_fingerprint(root, base)
            self.assertNotEqual(result["fp"], changed["fp"])
        finally:
            tmp.cleanup()

    def test_symlink_escaping_repo_is_skipped(self) -> None:
        tmp, root, base = make_repo()
        outer = tempfile.TemporaryDirectory()
        try:
            outside = Path(outer.name)
            (outside / "secret.txt").write_text("s\n")
            (root / "escape.txt").symlink_to(outside / "secret.txt")
            before = work_fingerprint(root, base)
            (outside / "secret.txt").write_text("changed\n")
            after = work_fingerprint(root, base)
            self.assertEqual(before["fp"], after["fp"])
        finally:
            tmp.cleanup()
            outer.cleanup()

    def test_missing_repo_is_unavailable_but_complete_false(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            result = work_fingerprint(plain, "deadbeef" * 5)
            self.assertFalse(result["complete"])
            self.assertIsNotNone(result["error"])


class GenericAgentFingerprintTests(unittest.TestCase):
    def test_no_deliverables_is_constant(self) -> None:
        self.assertEqual(generic_agent_fingerprint([]),
                         GENERIC_EMPTY_FINGERPRINT)
        self.assertEqual(generic_agent_fingerprint([], "anything"),
                         GENERIC_EMPTY_FINGERPRINT)

    def test_deliverables_plus_evidence_shape(self) -> None:
        fp = generic_agent_fingerprint(["a.py", "b.py"], "motivo x")
        self.assertTrue(fp.startswith("sha256:"))
        self.assertNotEqual(fp, GENERIC_EMPTY_FINGERPRINT)
        self.assertEqual(fp, generic_agent_fingerprint(["b.py", "a.py"],
                                                       "motivo x"))
        self.assertNotEqual(
            fp, generic_agent_fingerprint(["a.py", "b.py"], "motivo y"))
        self.assertNotEqual(
            fp, generic_agent_fingerprint(["a.py"], "motivo x"))


if __name__ == "__main__":
    unittest.main()
