"""Hunter ticket publisher and approval queue (design v2 sections 9.1-9.2).

Targeted unit tests with an ERP fake, an injected clock and deterministic
diagnosis evidence. No network, no CLI, no LLM (epic TASK-2026-00365,
ticket TASK-2026-00385).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import diagnosis as diag  # noqa: E402
from reliability import publisher as pub  # noqa: E402

ENABLED = {"enabled": True, "max_pending_owner": 5}


def _utc(day: int = 29) -> datetime:
    return datetime(2026, 10, day, tzinfo=timezone.utc)


def _problem(**override: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "engine|abc",
        "scope": "engine",
        "kind": "exception",
        "component": "dispatcher",
        "attribution": "engine",
        "attribution_rule": "A3",
        "severity_max": "high",
        "status": "diagnosed",
        "counts": {"occurrences": 5, "distinct_tickets": 3,
                   "distinct_projects": 1},
        "first_seen": _utc(20),
        "last_seen": _utc(29),
        "first_release": "abc123",
        "releases_seen": ["abc123"],
        "triage": {"eligible": True, "reason": "engine_normal_threshold"},
        "diagnosis": {"count": 1, "result": "fix"},
        "ticket": {"business_ref": None, "source_id": None, "type": None},
        "links": {"duplicate_of": None, "grouped_under": None},
        "mute": {"reason": None, "actor": None, "until": None},
        "affected_open_tasks": [],
        "fingerprint": "abc",
        "fingerprint_version": 1,
        "problem_key": "engine|abc",
        "version": 1,
        "updated_at": _utc(),
    }
    doc.update(override)
    return doc


def _proposal(**override: Any) -> dict[str, Any]:
    proposal: dict[str, Any] = {
        "hypothesis": {"cause": "race on requeue", "confidence": "supported"},
        "proposed_change": "guard the requeue",
        "target_components": ["dispatcher"],
        "regression_test": "replays the race with fakes",
        "recovery": "none",
    }
    proposal.update(override)
    return proposal


class FakeErp:
    """In-memory ERP adapter behind the publisher protocol."""

    def __init__(self, *, pending: int = 0,
                 timeout_on_create: bool = False) -> None:
        self.tickets: dict[str, dict[str, Any]] = {}
        self.pending = pending
        self.timeout_on_create = timeout_on_create
        self.creates = 0
        self.comments: list[tuple[str, str]] = []

    async def find_by_source_id(
            self, source: str, source_id: str) -> dict[str, Any] | None:
        found = self.tickets.get(f"{source}:{source_id}")
        return dict(found) if found is not None else None

    async def create_ticket(
            self, payload: dict[str, Any]) -> dict[str, Any]:
        self.creates += 1
        ticket = {"business_ref": f"TASK-2026-{90000 + self.creates}",
                  **payload}
        if self.timeout_on_create:
            self.tickets[
                f"{payload['source']}:{payload['source_id']}"] = ticket
            raise TimeoutError("created but the reply never arrived")
        self.tickets[
            f"{payload['source']}:{payload['source_id']}"] = ticket
        return dict(ticket)

    async def count_pending_hunter(self) -> int:
        return self.pending

    async def update_ticket(
            self, business_ref: str, patch: dict[str, Any]) -> dict[str, Any]:
        for ticket in self.tickets.values():
            if ticket.get("business_ref") == business_ref:
                ticket.update(patch)
                return dict(ticket)
        raise KeyError(business_ref)

    async def add_comment(self, business_ref: str, content: str) -> None:
        self.comments.append((business_ref, content))


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeProblems:
    """Minimal dict-backed Problems store (mirror worker fakes)."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def update_one(self, filt: dict[str, Any],
                         update: dict[str, Any]) -> None:
        for doc_id, doc in self.docs.items():
            if doc_id == filt.get("_id"):
                doc.update(update.get("$set", {}))
                self.docs[doc_id] = doc
                return None
        return None


class PublisherIdentityTest(unittest.TestCase):
    def test_source_id_destination_and_autorun(self) -> None:
        self.assertEqual(pub.source_id_for("engine|abc"),
                         "reliability:problem:engine|abc")
        self.assertEqual(pub.destination_for("engine"), "jardal-openceo")
        self.assertEqual(pub.destination_for("project:tienda"),
                         "tienda")
        self.assertIsNone(pub.destination_for("bogus"))
        payload = pub.build_ticket(_problem(), ticket_type="fix",
                                   proposal=_proposal())
        self.assertEqual(payload["source"], "spillover")
        self.assertEqual(payload["source_id"],
                         "reliability:problem:engine|abc")
        self.assertEqual(payload["project"], "jardal-openceo")
        self.assertIs(payload["autorun"], False)

    def test_fix_pending_review_with_human_action(self) -> None:
        payload = pub.build_ticket(_problem(), ticket_type="fix",
                                   proposal=_proposal())
        self.assertEqual(payload["initial_status"], "Pending Review")
        digest = pub.proposal_hash(_proposal())
        self.assertIn(digest, payload["human_action"] or "")
        self.assertIn("engine|abc", payload["human_action"] or "")

    def test_investigate_stays_open(self) -> None:
        payload = pub.build_ticket(_problem(), ticket_type="investigate")
        self.assertEqual(payload["initial_status"], "Open")
        self.assertIsNone(payload["human_action"])

    def test_fix_needs_complete_proposal(self) -> None:
        with self.assertRaises(ValueError):
            pub.build_ticket(_problem(), ticket_type="fix",
                             proposal=_proposal(regression_test=""))
        with self.assertRaises(ValueError):
            pub.build_ticket(_problem(), ticket_type="fix")


class PublisherContentTest(unittest.TestCase):
    def test_section_92_content_without_foreign_text(self) -> None:
        evidence = {"kind": "investigate", "role": "data",
                    "evidence_blocks": []}
        problem = _problem(transcript="full dump of the session",
                           logs=["line one", "line two"],
                           foreign_text="其他项目 secreto de otro proyecto",
                           other_project={"key": "tienda|xyz"})
        payload = pub.build_ticket(
            problem, ticket_type="fix", proposal=_proposal(),
            evidence=evidence,
            affected_open_tasks=[f"TASK-{number}" for number in range(25)],
            touches_protected=True)
        body = payload["body"]
        self.assertEqual(body["problem"]["attribution"], "engine")
        self.assertEqual(body["counts"]["occurrences"], 5)
        self.assertEqual(body["hypothesis"]["confidence"], "supported")
        self.assertEqual(body["proposed_change"], "guard the requeue")
        self.assertEqual(body["regression_test"],
                         "replays the race with fakes")
        self.assertEqual(body["affected_open_tasks"]["count"], 25)
        self.assertEqual(len(body["affected_open_tasks"]["refs"]), 20)
        self.assertIs(body["touches_protected_routes"], True)
        text = str(payload)
        self.assertNotIn("transcript", text.lower())
        self.assertNotIn("其他项目", text)

    def test_promotion_needs_new_hash_and_approval(self) -> None:
        problem = _problem()
        problem["status"] = "ticketed"
        problem["ticket"] = {"business_ref": "TASK-2026-90001",
                             "source_id": "reliability:problem:engine|abc",
                             "type": "investigate", "proposal_hash": None}
        promotion = pub.build_promotion(problem, _proposal())
        self.assertEqual(promotion["initial_status"], "Pending Review")
        self.assertIn(promotion["proposal_hash"],
                      promotion["human_action"] or "")
        problem["ticket"]["proposal_hash"] = promotion["proposal_hash"]
        with self.assertRaises(ValueError):
            pub.build_promotion(problem, _proposal())


class PublishFlowTest(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        erp = FakeErp()
        outcome = _run(pub.publish(_problem(), ticket_type="fix",
                                   proposal=_proposal(), erp=erp))
        self.assertEqual(outcome, {"status": "disabled",
                                   "reason": "tickets_disabled"})
        self.assertEqual(erp.creates, 0)

    def test_timeout_after_create_leaves_no_duplicate(self) -> None:
        erp = FakeErp(timeout_on_create=True)
        problem = _problem()
        first = _run(pub.publish(problem, ticket_type="fix",
                                 proposal=_proposal(), erp=erp,
                                 tickets_cfg=dict(ENABLED)))
        self.assertEqual(first["status"], "already_exists")
        second = _run(pub.publish(problem, ticket_type="fix",
                                  proposal=_proposal(), erp=erp,
                                  tickets_cfg=dict(ENABLED)))
        self.assertEqual(second["status"], "already_exists")
        self.assertEqual(len(erp.tickets), 1)
        self.assertEqual(
            second["ticket"]["business_ref"],
            first["ticket"]["business_ref"])

    def test_already_exists_syncs_store_as_ticketed(self) -> None:
        erp = FakeErp()
        problem = _problem()
        first = _run(pub.publish(problem, ticket_type="fix",
                                proposal=_proposal(), erp=erp,
                                tickets_cfg=dict(ENABLED)))
        self.assertEqual(first["status"], "published")
        store = FakeProblems()
        store.docs[str(problem["_id"])] = dict(problem)
        second = _run(pub.publish(dict(problem), ticket_type="fix",
                                  proposal=_proposal(), erp=erp,
                                  tickets_cfg=dict(ENABLED), store=store))
        self.assertEqual(second["status"], "already_exists")
        self.assertEqual(second["problem_status"], "ticketed")
        self.assertEqual(second["problem_update"]["status"], "ticketed")
        self.assertEqual(
            second["problem_update"]["ticket"]["business_ref"],
            first["ticket"]["business_ref"])
        self.assertEqual(store.docs[str(problem["_id"])]["status"],
                         "ticketed")

    def test_timeout_confirm_syncs_store_as_ticketed(self) -> None:
        erp = FakeErp(timeout_on_create=True)
        store = FakeProblems()
        problem = _problem()
        store.docs[str(problem["_id"])] = dict(problem)
        outcome = _run(pub.publish(problem, ticket_type="fix",
                                   proposal=_proposal(), erp=erp,
                                   tickets_cfg=dict(ENABLED), store=store))
        self.assertEqual(outcome["status"], "already_exists")
        self.assertEqual(outcome["problem_status"], "ticketed")
        self.assertEqual(store.docs[str(problem["_id"])]["status"],
                         "ticketed")

    def test_impact_score_accepts_severity_enum(self) -> None:
        from reliability.models import Severity
        self.assertEqual(pub.impact_score(occurrences_30d=3,
                                         severity_max=Severity.CRITICAL,
                                         distinct_tickets_30d=2), 30)
        self.assertEqual(pub.impact_score(occurrences_30d=3,
                                         severity_max=Severity.HIGH,
                                         distinct_tickets_30d=2), 18)
        ordered = pub.reevaluate_queue(
            [_problem(_id="enum", problem_key="enum",
                      severity_max=Severity.CRITICAL,
                      counts={"occurrences": 3, "distinct_tickets": 2,
                              "distinct_projects": 1},
                      status="diagnosed")])
        self.assertEqual([doc["_id"] for doc in ordered], ["enum"])

    def test_full_queue_parks_problem_as_diagnosed(self) -> None:
        erp = FakeErp(pending=5)
        outcome = _run(pub.publish(_problem(), ticket_type="fix",
                                   proposal=_proposal(), erp=erp,
                                   tickets_cfg=dict(ENABLED)))
        self.assertEqual(outcome["status"], "queued")
        self.assertEqual(outcome["problem_status"], "diagnosed")
        self.assertEqual(outcome["problem_update"]["status"], "diagnosed")
        self.assertEqual(erp.creates, 0)

    def test_diagnosis_off_over_threshold_makes_investigate(self) -> None:
        bundle = diag.build_bundle(
            _problem(), [_incident(number) for number in range(1, 4)])
        evidence = diag.investigate_evidence(bundle)
        erp = FakeErp()
        outcome = _run(pub.publish(
            _problem(), ticket_type="investigate", evidence=evidence,
            erp=erp, tickets_cfg=dict(ENABLED)))
        self.assertEqual(outcome["status"], "published")
        ticket = outcome["ticket"]
        self.assertIs(ticket["autorun"], False)
        self.assertEqual(ticket["initial_status"], "Open")
        self.assertEqual(ticket["body"]["evidence"]["kind"], "investigate")
        self.assertEqual(outcome["problem_status"], "ticketed")

    def test_rejected_problem_never_reproposed(self) -> None:
        erp = FakeErp()
        problem = pub.record_rejection(_problem())
        outcome = _run(pub.publish(problem, ticket_type="fix",
                                   proposal=_proposal(), erp=erp,
                                   tickets_cfg=dict(ENABLED)))
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["reason"], "rejected_never_reproposed")
        self.assertEqual(erp.creates, 0)

    def test_queue_order_by_impact(self) -> None:
        low = _problem(_id="low", problem_key="low",
                       severity_max="normal",
                       counts={"occurrences": 2, "distinct_tickets": 2,
                               "distinct_projects": 1},
                       status="diagnosed")
        high = _problem(_id="high", problem_key="high",
                        severity_max="critical",
                        counts={"occurrences": 3, "distinct_tickets": 2,
                                "distinct_projects": 1},
                        status="diagnosed")
        open_problem = _problem(_id="open", problem_key="open",
                                status="open")
        ordered = pub.reevaluate_queue([low, high, open_problem])
        self.assertEqual([doc["_id"] for doc in ordered], ["high", "low"])
        self.assertEqual(pub.impact_score(occurrences_30d=3,
                                         severity_max="critical",
                                         distinct_tickets_30d=2), 30)

    def test_comments_only_on_material_change(self) -> None:
        self.assertTrue(pub.needs_comment("regression"))
        self.assertTrue(pub.needs_comment("new_release_affected"))
        self.assertTrue(pub.needs_comment("fix_deployed"))
        self.assertFalse(pub.needs_comment("occurrence"))


def _incident(number: int) -> dict[str, Any]:
    return {
        "_id": f"inc-{number:02d}",
        "kind": "exception",
        "component": "dispatcher",
        "severity": "high",
        "created_at": _utc(number),
        "release": {"sha": "abc123", "dirty": False},
        "engine": {"task_id": f"TASK-{number}",
                   "project_key": "jardal-openceo",
                   "frames": ["dispatcher:dispatch:10"],
                   "reason_code": "timeout",
                   "brake": "none",
                   "guard_rule": "G1"},
        "context": {"message": "", "evidence_refs": []},
    }


if __name__ == "__main__":
    unittest.main()
