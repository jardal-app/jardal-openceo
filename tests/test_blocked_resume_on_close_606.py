"""Spent blocked parks resume on new child evidence (ticket #606).

Contract fixed here (extends TASK-2026-00430 without changing it):

- A completed child close under a ``waiting``/``blocked`` objective
  whose recovery budget is spent (``recovery.exhausted``) resumes it
  (active, park cleared, fresh recovery budget) and replans: the retry
  trigger is gone, boot leaves blocked parks, and a free-text Owner
  wait (prod flag, restart, ERP paste) emits no event the engine sees,
  so without the close the objective strands even after the Owner
  acted (OBJ-8E2257EF).
- A blocked park with a live recovery budget (or none) keeps its own
  trigger and stays parked, as do data_gaps/erp_error/stalled/config
  parks whatever their recovery block says.

Real engine + real close trigger, dict-backed stores with real update
semantics, scripted planner; no models.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import engine as engine_mod  # noqa: E402


def _env(tasks: dict[str, dict[str, Any]]):
    objectives = engine_mod.InMemoryObjectiveStore()
    return engine_mod.ObjectiveEnv(
        objectives=objectives,
        tasks=engine_mod.InMemoryTaskLister(dict(tasks)),
        create_ticket=AsyncMock(return_value={"task_id": "t"}),
        patch_task=AsyncMock(),
        executor=MagicMock(),
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=Path.cwd(),
    ), objectives


def _close(task_id: str = "kid-1") -> engine_mod.TicketCloseContext:
    return engine_mod.TicketCloseContext(
        task_id=task_id, title="w", tier="tier_c", sha="s")


class SpentBlockedResumeTests(unittest.IsolatedAsyncioTestCase):
    """A spent blocked park treats a completion as its resume trigger."""

    async def _objective(self, objectives, obj_id: str,
                         waiting_reason: str, recovery: Any) -> None:
        doc: dict[str, Any] = {
            "objective_id": obj_id, "project": "p",
            "status": "waiting", "waiting_reason": waiting_reason,
            "anchor": {"goal": "g", "acceptance": []}}
        if recovery is not None:
            doc["recovery"] = recovery
        await objectives.create_objective(doc)

    async def test_spent_blocked_close_resumes_and_replans(self) -> None:
        env, objectives = _env({
            "kid-1": {"task_id": "kid-1", "status": "completed",
                      "objective_id": "OBJ-606A"},
        })
        await self._objective(
            objectives, "OBJ-606A",
            "blocked: Todo lo ejecutable sin el Owner esta verificado",
            {"attempts": 2, "exhausted": True, "ticket_round": 2,
             "ticket_id": "rec-2", "due_at": None,
             "last_reason": "blocked: x"})
        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True,
                                                      "planned": True})
                          ) as planned:
            result = await engine_mod.on_ticket_closed(
                env, {"task_id": "kid-1", "status": "completed",
                      "objective_id": "OBJ-606A"}, _close())

        planned.assert_awaited_once_with(env, "OBJ-606A")
        self.assertEqual(result, {"ok": True, "planned": True})
        resumed = await objectives.get_objective("OBJ-606A")
        assert resumed is not None
        self.assertEqual(resumed["status"], "active")
        self.assertIsNone(resumed["waiting_reason"])
        self.assertEqual(resumed["recovery"]["attempts"], 0)
        self.assertIsNone(resumed["recovery"]["due_at"])
        self.assertFalse(resumed["recovery"]["exhausted"])
        self.assertEqual(resumed["recovery"]["ticket_round"], 2)

    async def test_spent_blocked_with_round_open_close_resumes(self) -> None:
        env, objectives = _env({
            "kid-1": {"task_id": "kid-1", "status": "completed",
                      "objective_id": "OBJ-606B"},
        })
        await self._objective(
            objectives, "OBJ-606B", "blocked: waiting on the Owner",
            {"attempts": 2, "exhausted": True, "ticket_round": 1,
             "ticket_id": "rec-1", "due_at": "2026-11-07T16:18:56+00:00"})
        with patch.object(engine_mod, "plan_objective",
                          new=AsyncMock(return_value={"ok": True})
                          ) as planned:
            result = await engine_mod.on_ticket_closed(
                env, {"task_id": "kid-1", "status": "completed",
                      "objective_id": "OBJ-606B"}, _close())

        planned.assert_awaited_once_with(env, "OBJ-606B")
        self.assertEqual(result, {"ok": True})
        resumed = await objectives.get_objective("OBJ-606B")
        assert resumed is not None
        self.assertEqual(resumed["status"], "active")

    async def test_unspent_blocked_close_stays_parked(self) -> None:
        cases = {
            "live budget": {"attempts": 0, "exhausted": False,
                            "due_at": "2026-11-07T17:00:00+00:00",
                            "ticket_round": 0},
            "no recovery block": None,
        }
        for name, recovery in cases.items():
            with self.subTest(name=name):
                obj_id = f"OBJ-606C-{name[:3]}"
                env, objectives = _env({
                    "kid-1": {"task_id": "kid-1", "status": "completed",
                              "objective_id": obj_id},
                })
                await self._objective(
                    objectives, obj_id,
                    "blocked: Todo lo ejecutable sin el Owner esta verificado",
                    recovery)
                with patch.object(engine_mod, "plan_objective",
                                  new=AsyncMock(return_value={"ok": True})
                                  ) as planned:
                    result = await engine_mod.on_ticket_closed(
                        env, {"task_id": "kid-1", "status": "completed",
                              "objective_id": obj_id}, None)
                planned.assert_not_awaited()
                self.assertEqual(result["action"], "objective-not-active")
                parked = await objectives.get_objective(obj_id)
                assert parked is not None
                self.assertEqual(parked["status"], "waiting")

    async def test_other_spent_parks_stay_parked(self) -> None:
        for index, reason in enumerate([
                "data_gaps: evidence missing",
                "erp_error: group conversion failed",
                "stalled: planning without progress",
                "config: planner registry empty",
        ]):
            with self.subTest(reason=reason):
                obj_id = f"OBJ-606D{index}"
                env, objectives = _env({
                    "kid-1": {"task_id": "kid-1", "status": "completed",
                              "objective_id": obj_id},
                })
                await self._objective(
                    objectives, obj_id, reason,
                    {"attempts": 2, "exhausted": True, "ticket_round": 2,
                     "due_at": None})
                with patch.object(engine_mod, "plan_objective",
                                  new=AsyncMock(return_value={"ok": True})
                                  ) as planned:
                    result = await engine_mod.on_ticket_closed(
                        env, {"task_id": "kid-1", "status": "completed",
                              "objective_id": obj_id}, None)
                planned.assert_not_awaited()
                self.assertEqual(result["action"], "objective-not-active")
                parked = await objectives.get_objective(obj_id)
                assert parked is not None
                self.assertEqual(parked["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
