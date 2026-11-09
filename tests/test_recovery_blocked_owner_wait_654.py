"""Owner-gated blocked parks wait for the Owner signal (ticket #654).

Root cause of OBJ-0D2EB295's park (TASK-2026-00609): a blocked park with no
children is a pure human wait (prod flags, restart, ERP actions) that emits
no engine event when the Owner acts. The recovery ladder spent 2 retries +
up to 2 dev tickets as an expensive finite keep-alive and then stranded the
mission in a paced-quiet park even after the Owner did everything: boot
leaves blocked parks, ERP actions on non-children emit no objective event,
and no manual replan existed.

Contract after this ticket:
- an exhausted blocked park never opens a dev recovery ticket: it waits for
  the Owner signal (retries and every other reason unchanged);
- an exhausted blocked park re-checks once per boot (the restart may be the
  awaited Owner event), under its surviving budgets;
- POST /api/objectives/{id}/plan re-drives a waiting objective on demand
  (the concrete Owner/CEO signal); external triggers activate waiting
  objectives before planning so continued children keep their close trigger.
- the poke refuses without mutating: a spend-held park (the hold owns its
  trigger), a recovery_deploy park (it replans only on the live fix after
  the restart, TASK-2026-00483), and a closed/cancelled objective (never
  resurrected).

Logic/contract only, with doubles; no live model calls (Owner order
2026-10-14). ``main`` is imported as in the auth inventory suite.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from objectives import engine as engine_mod  # noqa: E402
from objectives import recovery_policy as policy  # noqa: E402
from objectives.service import ObjectiveService  # noqa: E402

NOW = datetime(2026, 11, 9, 0, 30, tzinfo=timezone.utc)
CFG = {"recovery": {"enabled": True, "max_attempts": 2,
                    "backoff_minutes": [10, 60],
                    "ticket_project": "jardal-openceo",
                    "ticket_tier": "tier_c"}}


def _blocked_owner_wait(**overrides: Any) -> dict[str, Any]:
    doc = {
        "objective_id": "OBJ-0D2EB295",
        "project": "jardal-openceo",
        "status": "waiting",
        "waiting_reason": (
            "blocked: Code tranche complete and park holds: 5/5 merged on "
            "main, no open/blocked children, inbox empty, no material "
            "failures. Next is Owner-only, in order: (1) prod .env flip "
            "RESEARCH_ENABLED=true + RESEARCH_SCHEDULER_ENABLED=true"),
        "recovery": {"attempts": 2, "due_at": None, "exhausted": True,
                     "ticket_round": 0},
    }
    doc.update(overrides)
    return doc


class BlockedTicketExemptionTests(unittest.TestCase):
    def test_exhausted_blocked_park_waits_for_owner_signal(self) -> None:
        for round_no in (0, 1):
            objective = _blocked_owner_wait(recovery={
                "attempts": 2, "due_at": None, "exhausted": True,
                "ticket_round": round_no})
            decision = policy.recovery_decision(objective, 0, CFG, now=NOW)
            self.assertEqual(decision["action"], "none", round_no)
            self.assertIn("Owner", str(decision["reason"]))

    def test_open_ticket_still_reported_while_round_in_flight(self) -> None:
        objective = _blocked_owner_wait()
        decision = policy.recovery_decision(
            objective, 0, CFG, now=NOW, ticket_open=True)
        self.assertEqual(decision, {"action": "none",
                                    "reason": "recovery ticket open"})

    def test_system_wedges_still_open_tickets(self) -> None:
        for reason in ("capability_gap", "planner_error", "config",
                       "erp_error", "stalled", "no_quorum",
                       "closer_no_verdict", "data_gaps"):
            objective = _blocked_owner_wait(
                waiting_reason=f"{reason}: wedge the engineers must fix")
            decision = policy.recovery_decision(objective, 0, CFG, now=NOW)
            self.assertEqual(decision["action"], "ticket", reason)

    def test_blocked_retries_unchanged(self) -> None:
        due = _blocked_owner_wait(recovery={
            "attempts": 0, "due_at": NOW.isoformat(), "exhausted": False})
        self.assertEqual(policy.recovery_decision(
            due, 0, CFG, now=NOW)["action"], "retry")
        self.assertEqual(policy.recovery_decision(
            due, 1, CFG, now=NOW)["action"], "none")
        future = _blocked_owner_wait(recovery={
            "attempts": 0,
            "due_at": (NOW + timedelta(minutes=5)).isoformat(),
            "exhausted": False})
        self.assertEqual(policy.recovery_decision(
            future, 0, CFG, now=NOW)["action"], "none")

    def test_blocked_park_still_schedules_its_backoff(self) -> None:
        patch_ = policy.recovery_park_patch(
            _blocked_owner_wait(recovery={}), "blocked",
            "Next is Owner-only", CFG, NOW)
        assert patch_ is not None
        self.assertEqual(patch_["recovery"]["attempts"], 0)
        self.assertEqual(
            datetime.fromisoformat(patch_["recovery"]["due_at"]),
            NOW + timedelta(minutes=10))


class BlockedBootResumeTests(unittest.TestCase):
    def test_boot_replans_an_exhausted_blocked_park(self) -> None:
        call = engine_mod.boot_resume_decision(_blocked_owner_wait(), [])
        self.assertEqual(call["action"], "replan")
        self.assertIn("boot", str(call["reason"]))

    def test_boot_leaves_a_blocked_park_with_retries_pending(self) -> None:
        objective = _blocked_owner_wait(recovery={
            "attempts": 0,
            "due_at": (NOW + timedelta(minutes=9)).isoformat(),
            "exhausted": False})
        call = engine_mod.boot_resume_decision(objective, [])
        self.assertEqual(call["action"], "leave")

    def test_boot_leaves_other_parks_on_their_trigger(self) -> None:
        for reason in ("erp_error", "capability_gap", "data_gaps",
                       "config", "recovery_deploy"):
            objective = _blocked_owner_wait(
                waiting_reason=f"{reason}: keep waiting")
            call = engine_mod.boot_resume_decision(objective, [])
            self.assertEqual(call["action"], "leave", reason)


class ManualReplanTests(unittest.IsolatedAsyncioTestCase):
    async def _service(self, doc: dict[str, Any]) -> tuple[Any, list]:
        patches: list = []

        async def _get_objective(objective_id: str):
            return dict(doc) if objective_id == doc["objective_id"] else None

        async def _patch_objective(objective_id: str, updates: dict):
            patches.append((objective_id, dict(updates)))
            doc.update(updates)
            return dict(doc)

        repo = SimpleNamespace(get_objective=_get_objective,
                               patch_objective=_patch_objective)
        config = SimpleNamespace(has_project=lambda project: True,
                                 load_project=lambda project: {})
        service = ObjectiveService(
            repo, SimpleNamespace(), config, SimpleNamespace())
        return service, patches

    async def test_external_plan_activates_a_waiting_objective(self) -> None:
        doc = _blocked_owner_wait()
        service, patches = await self._service(doc)
        notify = AsyncMock()
        env = SimpleNamespace(notify=notify)
        with patch.object(service, "env_for", return_value=env), \
                patch.object(engine_mod, "plan_objective",
                             AsyncMock(return_value={"ok": True})) as plan:
            result = await service.plan_objective("OBJ-0D2EB295")
        self.assertTrue(result["ok"])
        plan.assert_awaited_once()
        self.assertEqual(patches, [("OBJ-0D2EB295", {
            "status": "active", "waiting_reason": None})])
        self.assertEqual(doc["status"], "active")
        notify.assert_awaited_once()

    async def test_external_plan_leaves_an_active_objective_untouched(
            self) -> None:
        doc = _blocked_owner_wait(status="active", waiting_reason=None)
        service, patches = await self._service(doc)
        notify = AsyncMock()
        env = SimpleNamespace(notify=notify)
        with patch.object(service, "env_for", return_value=env), \
                patch.object(engine_mod, "plan_objective",
                             AsyncMock(return_value={"ok": True})) as plan:
            result = await service.plan_objective("OBJ-0D2EB295")
        self.assertTrue(result["ok"])
        plan.assert_awaited_once()
        self.assertEqual(patches, [])
        notify.assert_not_awaited()

    async def test_external_plan_refuses_a_spend_held_park(self) -> None:
        doc = _blocked_owner_wait(pending_plan={
            "question_task_id": "TASK-2026-00001",
            "held_at": NOW.isoformat()})
        service, patches = await self._service(doc)
        with patch.object(engine_mod, "plan_objective",
                             AsyncMock(return_value={"ok": True})) as plan:
            result = await service.plan_objective("OBJ-0D2EB295")
        self.assertEqual(result, {"ok": False,
                                 "reason": "spend_approval_pending"})
        plan.assert_not_awaited()
        self.assertEqual(patches, [])
        self.assertEqual(doc["status"], "waiting")
        self.assertTrue(str(doc["waiting_reason"]).startswith("blocked:"))

    async def test_external_plan_refuses_a_deploy_park(self) -> None:
        doc = _blocked_owner_wait(
            waiting_reason="recovery_deploy: TASK-2026-00999",
            recovery={"attempts": 0, "due_at": NOW.isoformat(),
                      "exhausted": False,
                      "deploy": {"ticket_id": "TASK-2026-00999",
                                 "requested_at": NOW.isoformat(),
                                 "deadline": (NOW + timedelta(
                                     minutes=120)).isoformat()}})
        service, patches = await self._service(doc)
        with patch.object(engine_mod, "plan_objective",
                             AsyncMock(return_value={"ok": True})) as plan:
            result = await service.plan_objective("OBJ-0D2EB295")
        self.assertEqual(result, {"ok": False,
                                 "reason": "recovery_deploy_pending"})
        plan.assert_not_awaited()
        self.assertEqual(patches, [])
        self.assertEqual(doc["status"], "waiting")
        self.assertIn("deploy", doc["recovery"])

    async def test_external_plan_refuses_terminal_objectives(self) -> None:
        for status in ("closed", "cancelled"):
            doc = _blocked_owner_wait(status=status)
            service, patches = await self._service(doc)
            with patch.object(engine_mod, "plan_objective",
                                 AsyncMock(return_value={"ok": True})
                                 ) as plan:
                result = await service.plan_objective("OBJ-0D2EB295")
            self.assertEqual(result, {"ok": False,
                                     "reason": "objective-terminal"}, status)
            plan.assert_not_awaited()
            self.assertEqual(patches, [], status)
            self.assertEqual(doc["status"], status)


class PlanRouteTests(unittest.TestCase):
    def test_plan_route_is_registered_guarded_and_delegates(self) -> None:
        import main
        from fastapi.routing import APIRoute

        routes = [route for route in main.app.routes
                  if isinstance(route, APIRoute)
                  and route.path == "/api/objectives/{objective_id}/plan"]
        self.assertEqual(len(routes), 1)
        route = routes[0]
        self.assertIn("POST", set(route.methods or set()))
        calls = [getattr(dep, "dependency", None)
                 for dep in route.dependencies or []]
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            calls.extend(getattr(dep, "call", None)
                         for dep in dependant.dependencies or [])
        self.assertIn(main.get_current_user, calls)

        async def scenario() -> None:
            with patch.object(main.task_service, "plan_objective",
                              AsyncMock(return_value={"ok": True,
                                                      "tickets_created": 2})
                              ) as plan:
                result = await route.endpoint(
                    "OBJ-0D2EB295", current_user=SimpleNamespace())
            plan.assert_awaited_once_with("OBJ-0D2EB295")
            self.assertEqual(result, {"ok": True, "tickets_created": 2})

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
