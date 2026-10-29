"""Bounded recovery: business-state reconciliation (v2 section 10).

Targeted unit tests with a fake ERP, no network, no CLI, no LLM
(epic TASK-2026-00365, ticket TASK-2026-00390).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import recovery as rec  # noqa: E402

ENABLED = {"reconcile_business_state": True}
DISABLED = {"reconcile_business_state": False}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeErp:
    """In-memory ERP behind the recovery reader/applier protocols."""

    def __init__(self, status: str, version: int) -> None:
        self.status = status
        self.version = version
        self.reapplies: list[tuple[str, str]] = []

    async def resolve_task(self, business_ref: str) -> dict[str, Any]:
        return {"name": business_ref, "status": self.status,
                "version": self.version}

    async def transition_status(
        self, business_ref: str, target: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.reapplies.append((business_ref, target))
        self.status = target
        return {"ok": True}


def _incident(**override: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "business_ref": "TASK-2026-00001",
        "target": "Working",
        "observed_version": 7,
        "recovery_attempts": 0,
    }
    doc.update(override)
    return doc


class RecoveryGateTests(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        self.assertFalse(rec.recovery_enabled(None))
        self.assertFalse(rec.recovery_enabled(DISABLED))
        self.assertFalse(rec.recovery_enabled({}))
        self.assertTrue(rec.recovery_enabled(ENABLED))

    def test_refuses_unsafe_kinds(self) -> None:
        for kind in ("blind_retry_uncertain_effect", "change_tier",
                     "relaunch_confirmed_from_scratch"):
            self.assertTrue(rec.is_refused_recovery(kind))
        self.assertFalse(rec.is_refused_recovery("reconcile_business_state"))


class DecideRecoveryTests(unittest.TestCase):
    def test_already_at_target_closes(self) -> None:
        out = rec.decide_recovery(
            target="Working", observed_version=7,
            reread={"status": "Working", "version": 7}, attempts=0)
        self.assertEqual(out["decision"], "already_reconciled")

    def test_version_match_reapplies(self) -> None:
        out = rec.decide_recovery(
            target="Working", observed_version=7,
            reread={"status": "Open", "version": 7}, attempts=0)
        self.assertEqual(out["decision"], "reapply")

    def test_human_change_needs_owner(self) -> None:
        out = rec.decide_recovery(
            target="Working", observed_version=7,
            reread={"status": "Open", "version": 8}, attempts=0)
        self.assertEqual(out["decision"], "needs_owner")

    def test_attempts_capped_at_two(self) -> None:
        out = rec.decide_recovery(
            target="Working", observed_version=7,
            reread={"status": "Open", "version": 7}, attempts=2)
        self.assertEqual(out["decision"], "give_up")


class ReconcileIncidentTests(unittest.TestCase):
    def test_mismatch_reconciles_once(self) -> None:
        erp = FakeErp("Open", 7)
        out = _run(rec.reconcile_incident(
            _incident(), reader=erp, applier=erp, cfg=ENABLED))
        self.assertEqual(out["status"], "reapplied")
        self.assertEqual(out["attempts"], 1)
        self.assertEqual(erp.reapplies, [("TASK-2026-00001", "Working")])

    def test_human_version_needs_owner_without_overwrite(self) -> None:
        erp = FakeErp("Open", 8)
        out = _run(rec.reconcile_incident(
            _incident(observed_version=7), reader=erp, applier=erp,
            cfg=ENABLED))
        self.assertEqual(out["status"], "needs_owner")
        self.assertEqual(erp.reapplies, [])
        self.assertEqual(erp.status, "Open")

    def test_already_reconciled_closes_without_reapply(self) -> None:
        erp = FakeErp("Working", 7)
        out = _run(rec.reconcile_incident(
            _incident(), reader=erp, applier=erp, cfg=ENABLED))
        self.assertEqual(out["status"], "already_reconciled")
        self.assertEqual(erp.reapplies, [])

    def test_disabled_never_touches_erp(self) -> None:
        erp = FakeErp("Open", 7)
        out = _run(rec.reconcile_incident(
            _incident(), reader=erp, applier=erp, cfg=DISABLED))
        self.assertEqual(out["status"], "disabled")
        self.assertEqual(erp.reapplies, [])


class AffectedTasksTests(unittest.TestCase):
    def test_verifying_snapshot_lists_with_recommendation(self) -> None:
        problem = {"status": "verifying", "affected_open_tasks": []}
        updated, event = rec.note_verifying_affected(
            problem, ["TASK-2026-00002", "TASK-2026-00002",
                      "TASK-2026-00003"])
        self.assertEqual(event, "snapshot")
        self.assertEqual(updated["affected_open_tasks"],
                         ["TASK-2026-00002", "TASK-2026-00003"])
        self.assertIn("admision", updated["affected_recommendation"])
        summary = rec.affected_summary(updated["affected_open_tasks"])
        self.assertEqual(summary["count"], 2)
        self.assertIn("admision", summary["recommendation"])

    def test_non_verifying_never_snapshots(self) -> None:
        problem = {"status": "resolved", "affected_open_tasks": []}
        updated, event = rec.note_verifying_affected(
            problem, ["TASK-2026-00002"])
        self.assertEqual(event, "not_verifying")
        self.assertEqual(updated["affected_open_tasks"], [])


if __name__ == "__main__":
    unittest.main()
