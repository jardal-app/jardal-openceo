"""Precedent recall in Dev proposals and CEO reviews (TASK-2026-00336).

D10/D11 over the 3.3 record: up to 3 similar cases of the same
project or scope, failures and correct abstentions included, as
information and never as a threshold; historical replay uses only
precedents earlier than each case; one project never recalls
another's; the CEO never edits its mandate. No network, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from company_mandate import MandateError, apply_mandate_change  # noqa: E402
from orchestration import precedents as prec  # noqa: E402
from orchestration import precedent_recall as recall_mod  # noqa: E402


def owner_kwargs(**over: object) -> dict:
    base: dict = {
        "project": "jardal-openceo",
        "scope": "dev",
        "question_task_id": "TASK-2026-00901",
        "question_kind": "question",
        "question_summary": "Deploy the dashboard at noon?",
        "origin_task_id": "TASK-2026-00900",
        "approve": True,
        "answered_by": "owner",
        "comment": "",
    }
    base.update(over)
    return base


def make_store() -> dict:
    return prec.empty_store()


def record_concluded(store: dict, seq: int, outcome: str,
                     **over: object) -> object:
    entry = prec.record_owner_precedent(
        store, **owner_kwargs(question_task_id=f"TASK-2026-009{seq:02d}",
                              **over))
    prec.complete_outcome(store, entry.precedent_id, outcome=outcome,
                          evidence_ref=f"TASK-2026-009{seq:02d}")
    stored = store["precedents"][entry.precedent_id]
    stored["created_at"] = f"2026-10-20T10:{seq:02d}:00+00:00"
    return entry


def record_ceo(store: dict, seq: int, outcome: str,
               **over: object) -> object:
    base: dict = {
        "project": "jardal-openceo",
        "scope": "ceo",
        "review_id": f"CEO-2026-00{seq:02d}",
        "action": "keep_going",
        "decision_summary": "Keep going with the nightly backup rollout",
        "created_ref": f"TASK-2026-008{seq:02d}",
    }
    base.update(over)
    entry = prec.record_ceo_precedent(store, **base)
    prec.complete_outcome(store, entry.precedent_id, outcome=outcome,
                          evidence_ref=f"CEO-2026-00{seq:02d}")
    store["precedents"][entry.precedent_id]["created_at"] = (
        f"2026-10-21T10:{seq:02d}:00+00:00")
    return entry


class RecallBoundsTests(unittest.TestCase):
    def test_at_most_three_cases(self) -> None:
        store = make_store()
        for seq in range(5):
            record_concluded(store, seq, "success")
        found = recall_mod.recall(store, project="jardal-openceo",
                                  scope="dev", query="dashboard")
        self.assertEqual(len(found), 3)

    def test_limit_clamps_to_three(self) -> None:
        store = make_store()
        for seq in range(5):
            record_concluded(store, seq, "success")
        found = recall_mod.recall(store, project="jardal-openceo",
                                  scope="dev", query="dashboard",
                                  limit=10)
        self.assertEqual(len(found), 3)
        self.assertEqual(
            recall_mod.recall(store, project="jardal-openceo",
                              scope="dev", limit=0), [])

    def test_failures_and_abstentions_are_recalled(self) -> None:
        store = make_store()
        record_concluded(store, 1, "success")
        record_concluded(store, 2, "failure")
        record_concluded(store, 3, "abstention")
        found = recall_mod.recall(store, project="jardal-openceo",
                                  scope="dev", query="dashboard")
        self.assertEqual(
            {item["outcome"] for item in found},
            {"success", "failure", "abstention"})

    def test_inconclusive_is_never_recalled(self) -> None:
        store = make_store()
        entry = prec.record_owner_precedent(store, **owner_kwargs())
        prec.complete_outcome(store, entry.precedent_id, outcome=None,
                              evidence_ref="TASK-2026-00901")
        record_concluded(store, 2, "success")
        found = recall_mod.recall(store, project="jardal-openceo",
                                  scope="dev", query="dashboard")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["outcome"], "success")

    def test_missing_pointers_are_rejected(self) -> None:
        store = make_store()
        with self.assertRaises(ValueError):
            recall_mod.recall(store, project=" ", scope="dev")
        with self.assertRaises(ValueError):
            recall_mod.recall(store, project="jardal-openceo",
                              scope=" ")


class IsolationTests(unittest.TestCase):
    def test_no_project_recalls_another(self) -> None:
        store = make_store()
        record_concluded(store, 1, "success")
        record_concluded(store, 2, "failure",
                         project="jardal-otro")
        found = recall_mod.recall(store, project="jardal-openceo",
                                  scope="dev", query="dashboard")
        self.assertTrue(found)
        self.assertTrue(
            all(item["project"] == "jardal-openceo" for item in found))
        other = recall_mod.recall(store, project="jardal-otro",
                                  scope="dev", query="dashboard")
        self.assertEqual(len(other), 1)

    def test_dev_recall_excludes_ceo_scope(self) -> None:
        store = make_store()
        record_concluded(store, 1, "success")
        record_ceo(store, 1, "success")
        found = recall_mod.recall_for_proposal(
            store, project="jardal-openceo",
            proposal_summary="dashboard deploy")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["scope"], "dev")


class HistoryTests(unittest.TestCase):
    def test_replay_uses_only_earlier_precedents(self) -> None:
        store = make_store()
        record_concluded(store, 1, "success")
        record_concluded(store, 2, "failure")
        record_concluded(store, 3, "success")
        found = recall_mod.recall(
            store, project="jardal-openceo", scope="dev",
            query="dashboard", before="2026-10-20T10:02:00+00:00")
        self.assertEqual(
            [item["precedent_id"] for item in found], ["PR-0001"])

    def test_most_similar_ranks_first(self) -> None:
        store = make_store()
        record_concluded(store, 1, "success",
                         question_summary="Fix the login redirect loop")
        record_concluded(store, 2, "failure",
                         question_summary="Deploy the dashboard at noon")
        found = recall_mod.recall(store, project="jardal-openceo",
                                  scope="dev",
                                  query="dashboard deploy at noon")
        self.assertEqual(found[0]["precedent_id"], "PR-0002")


class CeoReviewTests(unittest.TestCase):
    def test_context_brings_decisions_with_results(self) -> None:
        store = make_store()
        record_ceo(store, 1, "success")
        record_ceo(store, 2, "failure", action="do_nothing",
                   decision_summary="Do nothing on the stale alert")
        context = recall_mod.ceo_review_context(
            store, project="jardal-openceo", query="rollout")
        self.assertEqual(context["scope"], "ceo")
        self.assertEqual(len(context["precedents"]), 2)
        self.assertTrue(
            all(item["outcome"] is not None
                for item in context["precedents"]))
        self.assertIn("causality", context["causality_note"])

    def test_context_never_edits_the_mandate(self) -> None:
        store = make_store()
        record_ceo(store, 1, "success")
        context = recall_mod.ceo_review_context(
            store, project="jardal-openceo")
        self.assertFalse(context["mandate_editable"])
        self.assertNotIn("mandate", str(context["precedents"]))
        with self.assertRaises(MandateError):
            apply_mandate_change(
                "projects/company_mandate.yaml",
                {"thresholds": {"x": 1}},
                requested_by="ceo",
                channel="ticket",
            )

    def test_changes_leave_as_one_grouped_question(self) -> None:
        grouped = recall_mod.propose_changes_as_questions([
            {"kind": "threshold", "text": "Raise the spend bar to 5"},
            {"kind": "mandate", "text": "Allow noon deploys"},
        ])
        self.assertEqual(grouped["kind"], "grouped_owner_question")
        self.assertEqual(len(grouped["items"]), 2)

    def test_grouped_question_rejects_garbage(self) -> None:
        with self.assertRaises(ValueError):
            recall_mod.propose_changes_as_questions([])
        with self.assertRaises(ValueError):
            recall_mod.propose_changes_as_questions(
                [{"kind": "unknown", "text": "Change everything"}])
        with self.assertRaises(ValueError):
            recall_mod.propose_changes_as_questions(
                [{"kind": "threshold", "text": "  "}])


class FormatTests(unittest.TestCase):
    def test_lines_are_information_not_threshold(self) -> None:
        store = make_store()
        record_concluded(store, 1, "failure")
        found = recall_mod.recall(store, project="jardal-openceo",
                                  scope="dev", query="dashboard")
        lines = recall_mod.format_recall_lines(found)
        self.assertIn("not a threshold", lines[0])
        self.assertIn("PR-0001", lines[1])
        self.assertIn("failure", lines[1])


if __name__ == "__main__":
    unittest.main()
