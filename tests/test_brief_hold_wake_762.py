"""Brief-hold retry schedules the next application (762).

Real finding from the OBJ-0D2EB295 close: a failed brief attach
recorded a pending hold and raised, but nothing scheduled the next
application. The waiting child kept the objective's delivery open, so
boot/orphan passes left the active objective and the ticket could wait
forever. After recording the hold, held tickets stop counting as
in-flight work, and boot/orphan replan an active objective with
pending holds despite the open gap. Delayed retry via the liveness
passes, no immediate wake: the recovery lane only sees waiting
objectives, and the hold bypasses the orphan idle gate so the first
heartbeat after it acts.

Targeted logic tests with mocks; no network/models.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for path in (CORE_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    BRIEF_ATTACH_PENDING_FIELD,
    BRIEF_ATTACH_WAITING_REASON,
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
)


def _child(task_id: str, status: str, **over: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": task_id, "title": "w", "status": status,
        "objective_id": "OBJ-762", "project": "proj",
        "acceptance": ["done"],
        "params": {"target_agent": "dev",
                   "expected_artifacts": ["code"]},
        "coding_tool": "tier_b",
    }
    task.update(over)
    return task


def _objective(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "objective_id": "OBJ-762",
        "conversion_source_task_id": "shell-1",
        "status": "active",
        "waiting_reason": None,
    }
    base.update(over)
    return base


def _held_entry() -> dict[str, Any]:
    return {"task_id": "held-1", "key": "dev",
            "brief_ids": ["brief-762"], "run_tag": "abc12345",
            "held_at": "2026-11-11T00:00:00+00:00"}


class BrakeActiveExcludesHeldTests(unittest.TestCase):
    def test_held_waiting_is_not_in_flight(self) -> None:
        held = _child("held-1", "waiting",
                      waiting_reason=BRIEF_ATTACH_WAITING_REASON)
        queued = _child("run-1", "queued")
        done = _child("done-1", "completed")
        active = engine_mod._brake_active_children([held, queued, done])
        self.assertEqual([row["task_id"] for row in active], ["run-1"])

    def test_other_waiting_stays_in_flight(self) -> None:
        waiting = _child("wait-1", "waiting",
                         waiting_reason="needs_owner: decide")
        active = engine_mod._brake_active_children([waiting])
        self.assertEqual([row["task_id"] for row in active], ["wait-1"])


class BootReplansPendingHoldTests(unittest.TestCase):
    def _children(self) -> list[dict[str, Any]]:
        shell = _child("shell-1", "converted")
        held = _child("held-1", "waiting",
                      waiting_reason=BRIEF_ATTACH_WAITING_REASON)
        return [shell, held]

    def test_active_with_hold_replans_despite_open_gap(self) -> None:
        call = engine_mod.boot_resume_decision(
            _objective(**{BRIEF_ATTACH_PENDING_FIELD: [_held_entry()]}),
            self._children())
        self.assertEqual(call["action"], "replan")
        self.assertIn("brief", str(call["reason"]))

    def test_active_with_open_work_and_no_hold_leaves(self) -> None:
        call = engine_mod.boot_resume_decision(
            _objective(), self._children())
        self.assertEqual(call["action"], "leave")


class FailedAttachSchedulesHealTests(unittest.IsolatedAsyncioTestCase):
    async def test_hold_records_and_schedules_heal(self) -> None:
        tasks: dict[str, dict[str, Any]] = {}
        recovery_wake = MagicMock()
        dispatcher_wake = MagicMock()
        created_row = {
            "task_id": "child-1", "status": "queued",
            "params": {"_mirrors": {"erpnext": {"name": "TASK-CHILD-1"}}},
        }

        async def create(payload: dict[str, Any]) -> dict[str, Any]:
            row = dict(created_row, title=payload.get("title"))
            tasks[row["task_id"]] = dict(row)
            return dict(row)

        async def patch_task(task_id: str, changes: dict[str, Any]) -> None:
            tasks[task_id].update(dict(changes))

        erp = MagicMock()
        erp.attach_research_briefs = AsyncMock(
            side_effect=RuntimeError("simulated storage failure"))
        store = InMemoryObjectiveStore()
        await store.create_objective(_objective())
        env = ObjectiveEnv(
            objectives=store,
            tasks=InMemoryTaskLister(tasks),
            create_ticket=create, patch_task=patch_task,
            executor=MagicMock(), quota=MagicMock(), slots=MagicMock(),
            reviews=None, inbox=None, notify=AsyncMock(),
            repo=Path.cwd(), erp=erp,
            dispatcher_wake=dispatcher_wake,
            recovery_wake=recovery_wake)
        planned = planner_mod.PlannedTicket(
            local_ref="dev", title="Implement safe retries",
            target_agent="dev", work_type="implementation",
            goal="Implement retries", why_now="Safe delivery",
            acceptance_criteria=["Retries preserve idempotency"],
            expected_artifacts=["implementation"],
            coding_tool="tier_c",
            tier_reason="default tier for specified work",
            research={"needed": True, "reason": "Use verified contract",
                      "brief_ids": ["brief-762"]})
        result = planner_mod.PlannerResult(
            objective_status="active", state_summary="Next justified work",
            tickets=[planned], roadmap_remaining=[])
        briefs = {"brief-762": {"id": "brief-762",
                                "content_text": "Verified."}}
        with self.assertRaisesRegex(RuntimeError, "storage failure"):
            await engine_mod._create_planned_tickets(
                env, _objective(), result, research_briefs=briefs)
        self.assertEqual(tasks["child-1"]["status"], "waiting")
        self.assertEqual(tasks["child-1"]["waiting_reason"],
                         BRIEF_ATTACH_WAITING_REASON)
        held = (await store.get_objective("OBJ-762") or {}).get(
            BRIEF_ATTACH_PENDING_FIELD)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["task_id"], "child-1")
        # No immediate wake: the recovery lane only sees waiting
        # objectives, so it could never schedule this active one; the
        # dispatcher must not run a held ticket either.
        recovery_wake.assert_not_called()
        dispatcher_wake.assert_not_called()
        # Scheduling contract end to end: the recorded hold makes the
        # liveness passes replan despite the open delivery gap.
        stored = await store.get_objective("OBJ-762")
        assert stored is not None
        call = engine_mod.boot_resume_decision(
            stored, list(tasks.values()))
        self.assertEqual(call["action"], "replan")
        self.assertIn("brief", str(call["reason"]))


class OrphanReplansPendingHoldTests(unittest.IsolatedAsyncioTestCase):
    OLD_IDLE = datetime(2026, 11, 11, tzinfo=timezone.utc)

    def _env(self, objective: dict[str, Any],
             children: list[dict[str, Any]]) -> ObjectiveEnv:
        store = InMemoryObjectiveStore()
        store.docs[str(objective["objective_id"])] = dict(objective)
        by_id = {str(row["task_id"]): dict(row) for row in children}
        return ObjectiveEnv(
            objectives=store, tasks=InMemoryTaskLister(by_id),
            create_ticket=AsyncMock(return_value=None),
            patch_task=AsyncMock(),
            executor=MagicMock(), quota=MagicMock(), slots=MagicMock(),
            reviews=None, inbox=None, notify=AsyncMock(),
            repo=Path("/tmp"),
            objectives_cfg={"max_final_reviews": 2})

    def _children(self) -> list[dict[str, Any]]:
        return [_child("shell-1", "converted"),
                _child("held-1", "waiting",
                       waiting_reason=BRIEF_ATTACH_WAITING_REASON)]

    async def test_orphan_replans_hold_despite_open_gap(self) -> None:
        objective = _objective(**{
            BRIEF_ATTACH_PENDING_FIELD: [_held_entry()],
            "updated_at": "2026-11-10T00:00:00+00:00"})
        env = self._env(objective, self._children())
        plan = AsyncMock(return_value={"ok": True})
        with patch.object(engine_mod, "plan_objective", new=plan):
            result = await engine_mod.resume_orphaned_one(
                env, "OBJ-762", idle_before=self.OLD_IDLE)
        self.assertEqual(result["action"], "replan")
        plan.assert_awaited_once_with(env, "OBJ-762")

    async def test_orphan_leaves_open_work_without_hold(self) -> None:
        objective = _objective(
            updated_at="2026-11-10T00:00:00+00:00")
        env = self._env(objective, self._children())
        plan = AsyncMock(return_value={"ok": True})
        with patch.object(engine_mod, "plan_objective", new=plan):
            result = await engine_mod.resume_orphaned_one(
                env, "OBJ-762", idle_before=self.OLD_IDLE)
        self.assertEqual(result["action"], "leave")
        plan.assert_not_awaited()

    async def test_orphan_replans_hold_despite_recent_activity(self) -> None:
        # The hold patch just bumped updated_at (Mongo always does), so
        # without the bypass the first heartbeat after a hold would read
        # recent activity and leave; the hold itself is the reason to
        # replan, so it acts instead of waiting out the idle gate.
        objective = _objective(**{
            BRIEF_ATTACH_PENDING_FIELD: [_held_entry()],
            "updated_at": "2026-11-11T12:00:00+00:00"})
        env = self._env(objective, self._children())
        plan = AsyncMock(return_value={"ok": True})
        with patch.object(engine_mod, "plan_objective", new=plan):
            result = await engine_mod.resume_orphaned_one(
                env, "OBJ-762", idle_before=self.OLD_IDLE)
        self.assertEqual(result["action"], "replan")
        plan.assert_awaited_once_with(env, "OBJ-762")

    async def test_orphan_leaves_recent_activity_without_hold(self) -> None:
        # The bypass is hold-specific: a clean orphan with recent
        # activity and no pending hold still waits out the idle gate.
        objective = _objective(
            updated_at="2026-11-11T12:00:00+00:00")
        env = self._env(objective, [_child("shell-1", "converted")])
        plan = AsyncMock(return_value={"ok": True})
        with patch.object(engine_mod, "plan_objective", new=plan):
            result = await engine_mod.resume_orphaned_one(
                env, "OBJ-762", idle_before=self.OLD_IDLE)
        self.assertEqual(result["action"], "leave")
        self.assertEqual(result["reason"], "recent activity")
        plan.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
