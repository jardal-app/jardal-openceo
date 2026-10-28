"""Owner precedent log: proposal, answer and observed result (TASK-2026-00335).

D10: every Owner answer generates a precedent with references; the
result completes by event; inconclusive stays null; no precedent
copies the ticket. No network, no LLM.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration import precedents as prec  # noqa: E402


def owner_kwargs(**over: object) -> dict:
    base: dict = {
        "project": "jardal-openceo",
        "scope": "dev",
        "question_task_id": "TASK-2026-00901",
        "question_kind": "question",
        "question_summary": "Deploy at noon?",
        "origin_task_id": "TASK-2026-00900",
        "approve": True,
        "answered_by": "owner",
        "comment": "",
    }
    base.update(over)
    return base


class ResponseTests(unittest.TestCase):
    def test_each_owner_answer_generates_a_precedent(self) -> None:
        store = prec.empty_store()
        approved = prec.record_owner_precedent(store, **owner_kwargs())
        edited = prec.record_owner_precedent(
            store, **owner_kwargs(approve=True, comment="yes, at 14:00"))
        denied = prec.record_owner_precedent(
            store, **owner_kwargs(approve=False, comment="too risky"))
        self.assertEqual(approved.response, "approved")
        self.assertEqual(edited.response, "edited")
        self.assertEqual(denied.response, "denied")
        self.assertEqual(
            {approved.precedent_id, edited.precedent_id,
             denied.precedent_id},
            set(store["precedents"]))
        for entry in (approved, edited, denied):
            self.assertIsNone(entry.outcome)

    def test_references_not_copies(self) -> None:
        store = prec.empty_store()
        body = "Deploy at noon? " + "x" * 2000
        entry = prec.record_owner_precedent(
            store, **owner_kwargs(question_summary=body, comment=body))
        stored = store["precedents"][entry.precedent_id]
        payload = json.dumps(stored)
        self.assertIn("TASK-2026-00901", payload)
        self.assertIn("TASK-2026-00900", payload)
        self.assertNotIn("x" * 2000, payload)
        self.assertLessEqual(
            len(stored["proposal_summary"]), prec.MAX_SUMMARY_CHARS + 20)

    def test_missing_reference_is_rejected(self) -> None:
        store = prec.empty_store()
        with self.assertRaises(ValueError):
            prec.record_owner_precedent(
                store, **owner_kwargs(question_task_id=" "))
        with self.assertRaises(ValueError):
            prec.record_owner_precedent(store, **owner_kwargs(project=" "))


class OutcomeTests(unittest.TestCase):
    def test_result_completes_by_event(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        result = prec.complete_outcome(
            store, entry.precedent_id, outcome="success",
            evidence_ref="TASK-2026-00900")
        self.assertEqual(result, {"applied": True, "outcome": "success"})
        stored = store["precedents"][entry.precedent_id]
        self.assertEqual(stored["outcome"], "success")
        self.assertTrue(stored["outcome_at"])
        self.assertEqual(len(prec.evidenced(store, "jardal-openceo")), 1)

    def test_inconclusive_stays_null_and_is_not_evidence(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        result = prec.complete_outcome(
            store, entry.precedent_id, outcome=None,
            evidence_ref="TASK-2026-00900")
        self.assertEqual(result, {"applied": True, "outcome": None})
        stored = store["precedents"][entry.precedent_id]
        self.assertIsNone(stored["outcome"])
        self.assertEqual(prec.evidenced(store, "jardal-openceo"), [])

    def test_unknown_outcome_is_rejected(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        with self.assertRaises(ValueError):
            prec.complete_outcome(
                store, entry.precedent_id, outcome="hecho",
                evidence_ref="TASK-2026-00900")

    def test_first_completion_wins(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        prec.complete_outcome(
            store, entry.precedent_id, outcome="failure",
            evidence_ref="TASK-2026-00900")
        again = prec.complete_outcome(
            store, entry.precedent_id, outcome="success",
            evidence_ref="TASK-2026-00900")
        self.assertEqual(
            again, {"applied": False, "reason": "outcome already recorded"})
        self.assertEqual(
            store["precedents"][entry.precedent_id]["outcome"], "failure")

    def test_unknown_precedent_never_raises(self) -> None:
        store = prec.empty_store()
        self.assertEqual(
            prec.complete_outcome(store, "PR-9999", outcome="success",
                                  evidence_ref="TASK-1"),
            {"applied": False, "reason": "unknown precedent"})


class VerdictTests(unittest.TestCase):
    def test_verified_hit_counts_unverified_does_not(self) -> None:
        self.assertEqual(
            prec.outcome_from_verdict("hecho", verified=True), "success")
        self.assertIsNone(prec.outcome_from_verdict("hecho", verified=False))

    def test_hits_failures_and_abstentions(self) -> None:
        self.assertEqual(
            prec.outcome_from_verdict("bloqueado", verified=True), "failure")
        self.assertEqual(
            prec.outcome_from_verdict("no_aplica", verified=True),
            "abstention")
        self.assertIsNone(prec.outcome_from_verdict("otro", verified=True))

    def test_unverified_block_or_abstention_stays_inconclusive(self) -> None:
        self.assertIsNone(
            prec.outcome_from_verdict("bloqueado", verified=False))
        self.assertIsNone(
            prec.outcome_from_verdict("no_aplica", verified=False))


class CeoTests(unittest.TestCase):
    def test_ceo_review_is_a_precedent_with_later_result(self) -> None:
        store = prec.empty_store()
        entry = prec.record_ceo_precedent(
            store, project="jardal-openceo", scope="ceo",
            review_id="ceo-2026-10-28-001", action="do_nothing",
            decision_summary="Nothing to start this week.")
        self.assertEqual(entry.response, "do_nothing")
        self.assertEqual(entry.proposal_ref, "ceo-2026-10-28-001")
        self.assertIsNone(entry.outcome)
        prec.complete_outcome(
            store, entry.precedent_id, outcome="abstention",
            evidence_ref="ceo-2026-11-04-001")
        self.assertEqual(
            store["precedents"][entry.precedent_id]["outcome"], "abstention")

    def test_missing_review_reference_is_rejected(self) -> None:
        store = prec.empty_store()
        with self.assertRaises(ValueError):
            prec.record_ceo_precedent(
                store, project="jardal-openceo", scope="ceo", review_id=" ",
                action="escalate", decision_summary="Spend.")


class ScopeTests(unittest.TestCase):
    def test_scope_filter_is_project_isolated(self) -> None:
        store = prec.empty_store()
        prec.record_owner_precedent(store, **owner_kwargs())
        prec.record_owner_precedent(
            store, **owner_kwargs(project="other", scope="dev"))
        prec.record_ceo_precedent(
            store, project="jardal-openceo", scope="ceo",
            review_id="ceo-1", action="escalate", decision_summary="Spend.")
        dev = prec.precedents_for_scope(store, "jardal-openceo", "dev")
        ceo = prec.precedents_for_scope(store, "jardal-openceo", "ceo")
        self.assertEqual(len(dev), 1)
        self.assertEqual(len(ceo), 1)

    def test_evidenced_is_project_isolated(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        prec.record_owner_precedent(
            store, **owner_kwargs(project="other", scope="dev"))
        prec.complete_outcome(
            store, entry.precedent_id, outcome="success",
            evidence_ref="TASK-2026-00900")
        mine = prec.evidenced(store, "jardal-openceo")
        self.assertEqual([row["precedent_id"] for row in mine],
                         [entry.precedent_id])
        self.assertEqual(prec.evidenced(store, "other"), [])

    def test_first_inconclusive_completion_wins(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        first = prec.complete_outcome(
            store, entry.precedent_id, outcome=None,
            evidence_ref="TASK-2026-00900", at="2026-10-28T00:00:00+00:00")
        second = prec.complete_outcome(
            store, entry.precedent_id, outcome="success",
            evidence_ref="TASK-2026-00900")
        self.assertEqual(first, {"applied": True, "outcome": None})
        self.assertEqual(
            second, {"applied": False, "reason": "outcome already recorded"})
        stored = store["precedents"][entry.precedent_id]
        self.assertIsNone(stored["outcome"])
        self.assertEqual(stored["outcome_at"], "2026-10-28T00:00:00+00:00")

    def test_seq_reconciles_with_stored_ids(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        store["seq"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / prec.STORE_FILENAME
            prec.save_store(store, path)
            loaded = prec.load_store(path)
        self.assertEqual(loaded["seq"], 1)
        again = prec.record_owner_precedent(loaded, **owner_kwargs())
        self.assertNotEqual(again.precedent_id, entry.precedent_id)

    def test_store_round_trip(self) -> None:
        store = prec.empty_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        prec.complete_outcome(
            store, entry.precedent_id, outcome="success",
            evidence_ref="TASK-2026-00900")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / prec.STORE_FILENAME
            prec.save_store(store, path)
            loaded = prec.load_store(path)
        self.assertEqual(
            loaded["precedents"][entry.precedent_id]["outcome"], "success")
        self.assertEqual(prec.load_store(Path(tmp) / "missing.json"),
                         prec.empty_store())


if __name__ == "__main__":
    unittest.main()
