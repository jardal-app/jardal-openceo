"""Global candidate contract + Owner gate (TASK-2026-00342, 1.7, D2).

Synthetic ticket dirs under tmp dirs only: never the real vault, never
other projects. No tokens. The Owner gate is simulated with plain
approve/deny answers, mirroring the acceptance wording.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration import global_candidates as gc  # noqa: E402
from orchestration.agent_sandbox import ticket_dir  # noqa: E402

TICKET = "TASK-2026-00998"
P1 = "projects/jardal/alpha"
P2 = "projects/jardal/beta"


def _candidate(**overrides: object) -> dict:
    base: dict = {
        "rule": "Scope vault checks with --project before the subcommand.",
        "evidence": [{"ticket": TICKET}],
        "projects": [P1, P2],
        "risk": "low",
        "owner_approved": False,
    }
    base.update(overrides)
    return base


class CollectGlobalCandidatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str = "scope.yaml",
               **overrides: object) -> Path:
        folder = ticket_dir(TICKET, base_dir=self.base) \
            / gc.GLOBAL_CANDIDATES_DIRNAME
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(yaml.safe_dump(_candidate(**overrides)),
                        encoding="utf-8")
        return path

    def _collect(self, task_id: str = TICKET):
        return gc.collect_global_candidates(task_id, base_dir=self.base)

    def test_valid_two_project_candidate(self) -> None:
        self._write()
        valid, rejected = self._collect()
        self.assertEqual(rejected, {})
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0].rule, _candidate()["rule"])
        eligible, _ = gc.eligible_for_global(
            gc.GlobalCandidate.model_validate(_candidate()))
        self.assertTrue(eligible)

    def test_single_project_without_approval_is_not_eligible(self) -> None:
        candidate = gc.GlobalCandidate.model_validate(
            _candidate(projects=[P1]))
        eligible, reason = gc.eligible_for_global(candidate)
        self.assertFalse(eligible)
        self.assertIn("second project", reason)

    def test_single_project_with_owner_approval_is_eligible(self) -> None:
        candidate = gc.GlobalCandidate.model_validate(
            _candidate(projects=[P1], owner_approved=True))
        eligible, _ = gc.eligible_for_global(candidate)
        self.assertTrue(eligible)

    def test_single_project_practice_rejected_as_global(self) -> None:
        self._write(projects=[P1])
        valid, rejected = self._collect()
        self.assertEqual(rejected, {})
        self.assertEqual(len(valid), 1)
        verdict, _ = gc.apply_decision(valid[0], "approve")
        self.assertEqual(verdict, "deferred")

    def test_secret_rule_rejected(self) -> None:
        self._write(rule="Set password: hunter2-fixed for the hook")
        _, rejected = self._collect()
        self.assertIn("scope.yaml", rejected)
        self.assertIn("secret", rejected["scope.yaml"])

    def test_missing_evidence_rejected(self) -> None:
        self._write(evidence=[{"ticket": "", "session": ""}])
        _, rejected = self._collect()
        self.assertIn("scope.yaml", rejected)
        self.assertIn("evidence", rejected["scope.yaml"])

    def test_confirmation_gate_always_required(self) -> None:
        self.assertTrue(gc.requires_owner_confirmation())
        self._write()
        valid, _ = self._collect()
        verdict, _ = gc.apply_decision(valid[0], None)
        self.assertEqual(verdict, "pending")
        verdict, _ = gc.apply_decision(valid[0], "deny")
        self.assertEqual(verdict, "discarded")
        verdict, _ = gc.apply_decision(valid[0], "approve")
        self.assertEqual(verdict, "ready_to_apply")

    def test_comment_names_valid_and_rejected(self) -> None:
        self._write("good.yaml")
        self._write("bad.yaml", evidence=[])
        valid, rejected = self._collect()
        text = gc.format_candidate_comment(valid, rejected)
        assert text is not None
        self.assertIn("good", text)
        self.assertIn("bad.yaml", text)
        self.assertIn("Owner confirmation", text)


if __name__ == "__main__":
    unittest.main()
