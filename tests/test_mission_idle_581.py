"""A judged mission left idle asks the Owner (TASK-2026-00581).

Live case: closing OBJ-F3AAEE49 woke the CEO to continue or close
MIS-47F6240D; it answered keep_going on a ticket that was interrupted and
not autorun, so the mission sat with no active objective and nobody was
asked about the Owner decisions its roadmap waited on.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from ceo.review import run_review  # noqa: E402
from objectives.missions import (  # noqa: E402
    InMemoryMissionStore,
    served_mission_reason,
    served_missions,
)
from test_ceo_review import _RecordedCEO, _make_env, _scratch_repo  # noqa: E402

_ROADMAP = ["Tramo deep con presupuesto; depende de la decision del Owner",
            "Scheduler recurrente; depende de autorizacion del Owner"]


def _reason(mission_id: str = "MIS-47F6240D") -> str:
    return ("Objective OBJ-F3AAEE49 closed: final review clean. "
            + served_mission_reason(mission_id))


class ServedMissionsTests(unittest.TestCase):
    def test_the_engine_reason_round_trips(self) -> None:
        self.assertEqual(served_missions([_reason()]), ["MIS-47F6240D"])

    def test_several_reasons_keep_order_without_repeats(self) -> None:
        reasons = [_reason("MIS-B"), "free-router refresh_failed",
                   _reason("MIS-A"), _reason("MIS-B")]
        self.assertEqual(served_missions(reasons), ["MIS-B", "MIS-A"])

    def test_unrelated_text_names_no_mission(self) -> None:
        self.assertEqual(served_missions(["mission MIS-1 mentioned"]), [])


class IdleMissionReviewTests(unittest.IsolatedAsyncioTestCase):
    async def _env(self, decision: dict[str, Any], *,
                   objective_status: str = "closed") -> Any:
        repo = _scratch_repo(self)
        missions = InMemoryMissionStore()
        await missions.create_mission({
            "mission_id": "MIS-47F6240D", "project": "ops",
            "status": "active", "goal": "research alive",
            "acceptance": ["deep live"], "acceptance_status": "set",
            "objectives": [{"objective_id": "OBJ-F3AAEE49",
                            "status": objective_status}],
        })
        env, calls = _make_env(self, repo, executor=_RecordedCEO(
            repo, [decision]), missions=missions)
        await env.objectives.create_objective({
            "objective_id": "OBJ-F3AAEE49", "project": "ops",
            "status": objective_status, "mission_id": "MIS-47F6240D",
            "roadmap_remaining": _ROADMAP,
        })
        return env, calls, missions

    async def test_keep_going_on_an_idle_mission_asks_the_owner_once(
            self) -> None:
        env, calls, missions = await self._env(
            {"action": "keep_going", "summary": "open work suffices"})
        outcome = await run_review(env, kind="reactive", reasons=[_reason()])
        self.assertEqual(outcome["idle_missions_asked"], ["MIS-47F6240D"])
        self.assertEqual(len(calls["escalations"]), 1)
        escalation = calls["escalations"][0]
        self.assertEqual(escalation["source_id"],
                         "mission-idle:MIS-47F6240D:OBJ-F3AAEE49")
        self.assertTrue(escalation["action"].strategic_replan)
        self.assertIn("Scheduler recurrente",
                      escalation["decision"].human_action)
        stored = await missions.get_mission("MIS-47F6240D")
        assert stored is not None
        self.assertEqual(stored["idle_question"]["objective_id"],
                         "OBJ-F3AAEE49")

        second = await run_review(env, kind="reactive", reasons=[_reason()])
        self.assertNotIn("idle_missions_asked", second)
        self.assertEqual(len(calls["escalations"]), 1)

    async def test_a_mission_with_an_active_objective_is_not_idle(
            self) -> None:
        env, calls, _missions = await self._env(
            {"action": "keep_going", "summary": "open work suffices"},
            objective_status="active")
        outcome = await run_review(env, kind="reactive", reasons=[_reason()])
        self.assertNotIn("idle_missions_asked", outcome)
        self.assertEqual(calls["escalations"], [])

    async def test_a_review_not_woken_for_the_mission_asks_nothing(
            self) -> None:
        env, calls, _missions = await self._env(
            {"action": "keep_going", "summary": "open work suffices"})
        outcome = await run_review(env, kind="scheduled", reasons=[])
        self.assertNotIn("idle_missions_asked", outcome)
        self.assertEqual(calls["escalations"], [])


if __name__ == "__main__":
    unittest.main()
