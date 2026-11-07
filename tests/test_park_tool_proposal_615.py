"""Parked planner cycles still ask the Owner about proposals (ticket #615).

Contract fixed here (policy §16, planner prompt, loose-path precedent):

- An objective cycle with ``objective_status`` blocked/capability_gap
  parks AND opens one owner question per ``proposed_tools`` entry (plus
  the ``proposed_playbook_update`` one), before the status dispatch.
  Before the fix the park branches returned early and the proposals
  died silently: OBJ-8E2257EF parked blocked with an
  ``erp_verdict_publish`` proposal that never reached the Owner, so
  recovery retries replanned identically until the budgets ran out.
- Active cycles keep creating the same questions exactly once (the two
  calls moved, they were not duplicated).
- Cycles without proposals create no questions on any status.

Real engine (``_record_and_apply_plan``) + real question builders,
dict-backed stores with real update semantics, scripted tickets; no
models.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402


def _env(created: list[dict[str, Any]], patched: list[dict[str, Any]]):
    objectives = engine_mod.InMemoryObjectiveStore()
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        created.append(dict(payload))
        return {"task_id": f"q-new-{counter['n']}",
                "params": dict(payload.get("params") or {})}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patched.append({"task_id": task_id, "updates": dict(updates)})

    env = engine_mod.ObjectiveEnv(
        objectives=objectives,
        tasks=engine_mod.InMemoryTaskLister({}),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=MagicMock(),
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=Path.cwd(),
    )
    return env, objectives


def _result(status: str, **overrides: Any) -> planner_mod.PlannerResult:
    payload: dict[str, Any] = {
        "objective_status": status,
        "state_summary": "only the ERP paste remains",
        "roadmap_remaining": ["owner pastes the verdict texts"],
    }
    payload.update(overrides)
    return planner_mod.PlannerResult(**payload)


def _question_blocks(created: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [((item.get("params") or {}).get("owner_question") or {})
            for item in created]


class ParkProposalQuestionTests(unittest.IsolatedAsyncioTestCase):
    """A park carries its proposals to the Owner instead of dropping them."""

    async def _objective(self, objectives, obj_id: str) -> dict[str, Any]:
        doc = {"objective_id": obj_id, "project": "jardal-openceo",
               "status": "active", "anchor": {"goal": "g", "acceptance": []}}
        await objectives.create_objective(doc)
        return doc

    async def test_blocked_cycle_creates_tool_question_then_parks(self) -> None:
        created: list[dict[str, Any]] = []
        patched: list[dict[str, Any]] = []
        env, objectives = _env(created, patched)
        objective = await self._objective(objectives, "OBJ-615A")
        result = _result("blocked", proposed_tools=[
            {"name": "erp_verdict_publish",
             "need": "publish verdict texts cross-ticket",
             "spec": "idempotent, marker-based skip",
             "origin_task_id": "e4327b8d-22cf-4423-9d14-8b6e9aa31a46"},
        ])

        outcome = await engine_mod._record_and_apply_plan(
            env, objective, "OBJ-615A", result,
            closed_before=4, policy_sha="p")

        self.assertEqual(outcome["reason"], "blocked")
        self.assertEqual(len(created), 1)
        self.assertEqual(
            created[0]["source_id"],
            "tool-proposal:OBJ-615A:erp_verdict_publish")
        block = _question_blocks(created)[0]
        self.assertEqual(block.get("kind"), "new_tool")
        self.assertEqual(block.get("origin_task_id"),
                         "e4327b8d-22cf-4423-9d14-8b6e9aa31a46")
        self.assertEqual(patched[0]["task_id"], "q-new-1")
        self.assertEqual(patched[0]["updates"]["waiting_reason"],
                         "owner_question:new_tool")
        parked = await objectives.get_objective("OBJ-615A")
        assert parked is not None
        self.assertEqual(parked["status"], "waiting")
        self.assertTrue(str(parked["waiting_reason"]).startswith("blocked:"))

    async def test_capability_gap_cycle_creates_tool_and_playbook(self) -> None:
        created: list[dict[str, Any]] = []
        patched: list[dict[str, Any]] = []
        env, objectives = _env(created, patched)
        objective = await self._objective(objectives, "OBJ-615B")
        result = _result(
            "capability_gap",
            proposed_tools=[{"name": "erp_verdict_publish",
                             "need": "publish verdict texts cross-ticket"}],
            proposed_playbook_update={"playbook": "research-live",
                                      "change": "ask before enabling",
                                      "reason": "scheduler needs approval"},
        )

        outcome = await engine_mod._record_and_apply_plan(
            env, objective, "OBJ-615B", result,
            closed_before=4, policy_sha="p")

        self.assertEqual(outcome["reason"], "capability_gap")
        self.assertEqual(len(created), 2)
        kinds = sorted(block.get("kind") for block in _question_blocks(created))
        self.assertEqual(kinds, ["new_tool", "playbook"])
        parked = await objectives.get_objective("OBJ-615B")
        assert parked is not None
        self.assertEqual(parked["status"], "waiting")
        self.assertIn("capability_gap", parked)

    async def test_active_cycle_still_creates_questions_once(self) -> None:
        created: list[dict[str, Any]] = []
        patched: list[dict[str, Any]] = []
        env, objectives = _env(created, patched)
        objective = await self._objective(objectives, "OBJ-615C")
        result = _result(
            "active",
            tickets=[{
                "local_ref": "t1", "title": "Do the thing",
                "target_agent": "dev", "work_type": "implementation",
                "goal": "achieve it", "why_now": "next step",
                "acceptance_criteria": ["done"],
                "expected_artifacts": ["code"],
                "coding_tool": "tier_c", "tier_reason": "specified work",
            }],
            proposed_tools=[{"name": "erp_verdict_publish",
                             "need": "publish verdict texts cross-ticket"}],
        )

        outcome = await engine_mod._record_and_apply_plan(
            env, objective, "OBJ-615C", result,
            closed_before=4, policy_sha="p")

        self.assertEqual(outcome["tickets_created"], 1)
        questions = [item for item in created
                     if "tool-proposal:" in str(item.get("source_id") or "")]
        self.assertEqual(len(questions), 1)
        self.assertEqual(
            questions[0]["source_id"],
            "tool-proposal:OBJ-615C:erp_verdict_publish")

    async def test_park_without_proposals_creates_no_questions(self) -> None:
        created: list[dict[str, Any]] = []
        patched: list[dict[str, Any]] = []
        env, objectives = _env(created, patched)
        objective = await self._objective(objectives, "OBJ-615D")

        outcome = await engine_mod._record_and_apply_plan(
            env, objective, "OBJ-615D", _result("blocked"),
            closed_before=4, policy_sha="p")

        self.assertEqual(outcome["reason"], "blocked")
        self.assertEqual(created, [])
        parked = await objectives.get_objective("OBJ-615D")
        assert parked is not None
        self.assertEqual(parked["status"], "waiting")

    async def test_repeat_blocked_cycle_reuses_the_stable_key(self) -> None:
        created: list[dict[str, Any]] = []
        patched: list[dict[str, Any]] = []
        env, objectives = _env(created, patched)
        objective = await self._objective(objectives, "OBJ-615E")

        def _proposal() -> planner_mod.PlannerResult:
            return _result("blocked", proposed_tools=[
                {"name": "erp_verdict_publish",
                 "need": "publish verdict texts cross-ticket"},
            ])

        await engine_mod._record_and_apply_plan(
            env, objective, "OBJ-615E", _proposal(),
            closed_before=4, policy_sha="p")
        latest = await objectives.get_objective("OBJ-615E")
        assert latest is not None
        await engine_mod._record_and_apply_plan(
            env, latest, "OBJ-615E", _proposal(),
            closed_before=4, policy_sha="p")

        keys = [item.get("source_id") for item in created]
        self.assertEqual(keys, [
            "tool-proposal:OBJ-615E:erp_verdict_publish",
            "tool-proposal:OBJ-615E:erp_verdict_publish",
        ])


if __name__ == "__main__":
    unittest.main()
