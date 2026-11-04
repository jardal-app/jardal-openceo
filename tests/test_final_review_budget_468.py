"""A spent final-review budget never strands an objective (TASK-2026-00468).

Contract fixed here:

- Clean dev delivery with ``max_final_reviews`` spent closes on the
  deterministic checks: no reviewer or closer runs, the delivery record
  says ``budget_spent`` and every notice says how the close was earned
  (never "final review clean").
- Open work with the budget spent keeps the objective active, with no
  planner bounce and no park; the open child's close re-plans it.
- With budget left the pool review still runs (regression guard).

Real engine over in-memory stores and a scratch git repo; no models.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for _path in (CORE_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from objectives import engine as engine_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    ObjectiveEnv,
    on_ticket_closed,
    run_final_review,
)
from test_company_objectives import (  # noqa: E402
    _ExplodingExecutor,
    _child,
    _env_for,
    _mirrored,
    _plan_result,
    _scratch_repo,
)


class FinalReviewBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def _world(
        self, children: list[dict[str, Any]], *, final_reviews: int,
    ) -> tuple[ObjectiveEnv, _ExplodingExecutor, dict[str, dict[str, Any]],
               list[tuple[str, str]], list[str]]:
        tasks = {"task-parent": _mirrored(
            "task-parent", "TASK-1", status="converted",
            objective_id="OBJ-1")}
        tasks.update({str(child["task_id"]): child for child in children})
        env, _planner, _created, patches, notices = _env_for(
            self, _scratch_repo(self), [_plan_result()], tasks=tasks,
            objectives_cfg={"max_final_reviews": 2})
        executor = _ExplodingExecutor()
        env.executor = executor  # type: ignore[assignment]
        wakes: list[str] = []
        env.ceo_wake = wakes.append
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
            "mission_id": "MIS-1",
            "final_reviews": final_reviews,
        })
        return env, executor, patches, notices, wakes

    async def test_spent_budget_clean_delivery_closes_without_review(
        self,
    ) -> None:
        env, executor, patches, notices, wakes = await self._world(
            [_child("kid-1", "completed", dev=True)], final_reviews=2)

        result = await run_final_review(env, "OBJ-1")

        self.assertEqual(result["action"], "closed")
        self.assertEqual(executor.runs, [])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "closed")
        self.assertEqual(stored["final_reviews"], 2)
        self.assertEqual(stored["delivery"]["review"], "budget_spent")
        self.assertEqual(stored["delivery"]["verified_by"], "final_review")
        self.assertEqual(patches["task-parent"]["status"], "completed")
        self.assertIn("final review budget spent (2 reviews",
                      patches["task-parent"]["last_response_summary"])
        self.assertEqual(len(wakes), 1)
        self.assertIn("final review budget spent (2 reviews", wakes[0])
        self.assertIn("mission MIS-1", wakes[0])
        texts = " ".join(text for _ref, text in notices)
        self.assertIn("final review budget spent", texts)
        self.assertNotIn("final review clean", texts)

    async def test_spent_budget_open_work_waits_active_then_replans(
        self,
    ) -> None:
        env, executor, _patches, notices, wakes = await self._world(
            [_child("kid-1", "completed", dev=True),
             _child("kid-2", "queued", dev=True)], final_reviews=2)
        plan = AsyncMock(return_value={"ok": True, "planned": True})

        with patch.object(engine_mod, "plan_objective", new=plan):
            result = await run_final_review(env, "OBJ-1")

            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "awaiting_open_work")
            self.assertIn("kid-2", result["detail"])
            plan.assert_not_awaited()
            stored = await env.objectives.get_objective("OBJ-1")
            assert stored is not None
            self.assertEqual(stored["status"], "active")
            self.assertIsNone(stored.get("waiting_reason"))
            self.assertEqual(stored["final_reviews"], 2)
            self.assertEqual(wakes, [])
            self.assertTrue(any("waits for its open work" in text
                                for _ref, text in notices))

            env.tasks.tasks["kid-2"]["status"] = "completed"  # type: ignore[attr-defined]
            closed = await on_ticket_closed(
                env, {"task_id": "kid-2", "status": "completed",
                      "objective_id": "OBJ-1"})

        plan.assert_awaited_once_with(env, "OBJ-1")
        self.assertEqual(closed, {"ok": True, "planned": True})
        self.assertEqual(executor.runs, [])

    async def test_budget_left_still_runs_the_pool(self) -> None:
        env, executor, _patches, _notices, _wakes = await self._world(
            [_child("kid-1", "completed", dev=True)], final_reviews=1)

        result = await run_final_review(env, "OBJ-1")

        # Empty pool: no quorum, but the counter proves the pool branch ran.
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_quorum")
        self.assertEqual(executor.runs, [])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["final_reviews"], 2)


if __name__ == "__main__":
    unittest.main()
