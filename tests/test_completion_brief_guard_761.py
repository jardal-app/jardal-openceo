"""Completion-path brief guard mirrors the creates-path check (761).

Real finding from the OBJ-0D2EB295 close: a brief present at
validation can vanish before the apply. The creates path raises the
typed ``ObjectiveErpError`` naming the unavailable briefs; the
completion path indexed ``research_briefs`` directly and raised a bare
``KeyError``. The completion attach now mirrors the guard.

Targeted logic tests with mocks; no network/models.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for path in (CORE_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    ObjectiveErpError,
    _apply_contract_completions,
)


def _planned(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "local_ref": "c1",
        "title": "Do the thing",
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": "g",
        "why_now": "now",
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_c",
        "tier_reason": "default tier for specified work",
        "completes_task_id": "task-stub",
        "research": {
            "needed": True,
            "reason": "Use the verified brief",
            "brief_ids": ["ghost-brief"],
        },
    }
    base.update(overrides)
    return planner_mod.PlannedTicket(**base)


def _result(*tickets: Any) -> Any:
    return planner_mod.PlannerResult(
        objective_status="active",
        state_summary="Adopt the stub",
        tickets=list(tickets),
        roadmap_remaining=[],
    )


def _stub() -> dict[str, Any]:
    return {
        "task_id": "task-stub",
        "title": "Promoted stub work",
        "status": "queued",
        "project": "acme",
        "agent_hint": "dev",
        "params": {"_mirrors": {"erpnext": {"name": "TASK-STUB"}}},
        "objective_id": "OBJ-761",
    }


def _env(tasks: dict[str, dict[str, Any]]) -> tuple[ObjectiveEnv, AsyncMock]:
    erp = AsyncMock()
    erp.attach_research_briefs = AsyncMock()
    erp.update_description = AsyncMock()

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        tasks[task_id].update(updates)

    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(tasks),
        create_ticket=AsyncMock(return_value={"task_id": "t"}),
        patch_task=patch_task,
        executor=MagicMock(),
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=Path.cwd(),
        erp=erp,
        dispatcher_wake=MagicMock(),
    )
    return env, erp


class CompletionBriefGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_vanished_brief_raises_typed_error_naming_the_brief(self) -> None:
        tasks = {"task-stub": _stub()}
        env, erp = _env(tasks)
        result = _result(_planned())
        with self.assertRaises(ObjectiveErpError) as ctx:
            await _apply_contract_completions(
                env, "OBJ-761", result, research_briefs={})
        self.assertIn("ghost-brief", str(ctx.exception))
        self.assertIn("task-stub", str(ctx.exception))
        erp.attach_research_briefs.assert_not_awaited()
        self.assertNotIn("coding_tool", tasks["task-stub"])

    async def test_present_brief_still_attaches(self) -> None:
        tasks = {"task-stub": _stub()}
        env, erp = _env(tasks)
        brief = {"id": "brief-761", "content_text": "Verified."}
        planned = _planned(research={
            "needed": True,
            "reason": "Use the verified brief",
            "brief_ids": ["brief-761"],
        })
        completed = await _apply_contract_completions(
            env, "OBJ-761", _result(planned),
            research_briefs={"brief-761": brief})
        self.assertEqual(completed, ["task-stub"])
        erp.attach_research_briefs.assert_awaited_once_with("TASK-STUB", [brief])
        self.assertEqual(tasks["task-stub"]["coding_tool"], "tier_c")


if __name__ == "__main__":
    unittest.main()
