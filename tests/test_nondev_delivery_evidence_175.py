"""Non-dev delivery needs domain evidence before verified close (TASK-2026-00175).

Every non-dev delivery must clear a registered deterministic verifier
per domain; cancelled/failed children are history (never proof, never
required); dev work still verifies through the reviewer pool. Missing
verifiers park as capability_gap, failed evidence or no verified
delivery as data_gaps. Stub verifiers only, 0 tokens.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for _path in (CORE_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from objectives.engine import run_final_review  # noqa: E402
from test_company_objectives import (  # noqa: E402
    _ExplodingExecutor,
    _child,
    _env_for,
    _mirrored,
    _plan_result,
    _scratch_repo,
)


def _ok_verifier(seen: list[str] | None = None) -> Any:
    async def check(child: dict[str, Any]) -> dict[str, Any]:
        if seen is not None:
            seen.append(str(child.get("task_id") or ""))
        return {"ok": True, "missing_artifacts": [], "unmet_criteria": []}

    return check


def _missing_verifier(artifact: str = "report.pdf") -> Any:
    async def check(child: dict[str, Any]) -> dict[str, Any]:
        del child
        return {"ok": False, "missing_artifacts": [artifact],
                "unmet_criteria": []}

    return check


def _unmet_verifier(criterion: str = "audited") -> Any:
    async def check(child: dict[str, Any]) -> dict[str, Any]:
        del child
        return {"ok": False, "missing_artifacts": [],
                "unmet_criteria": [criterion]}

    return check


async def _boom_verifier(child: dict[str, Any]) -> dict[str, Any]:
    del child
    raise RuntimeError("runner caido")


class NonDevEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def _objective(self, env: Any) -> None:
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })

    def _parent(self) -> dict[str, Any]:
        return _mirrored("task-parent", "TASK-1", status="converted",
                         objective_id="OBJ-1")

    async def test_missing_artifact_parks_data_gaps(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": _child("kid-1", "completed", dev=False)}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        env.delivery_verifiers = {"research": _missing_verifier()}
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "data_gaps")
        self.assertIn("missing artifact: report.pdf", result["detail"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")

    async def test_unmet_criterion_parks_data_gaps(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": _child("kid-1", "completed", dev=False)}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        env.delivery_verifiers = {"research": _unmet_verifier()}
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "data_gaps")
        self.assertIn("unmet criterion: audited", result["detail"])

    async def test_zero_children_parks_data_gaps(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent()}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        # Never consulted: there is no work to verify.
        env.delivery_verifiers = {"research": _boom_verifier}
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "data_gaps")
        self.assertIn("no work children", result["detail"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertNotIn("delivery", stored)

    async def test_all_history_parks_data_gaps(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": _child("kid-1", "cancelled", dev=False),
                 "kid-2": _child("kid-2", "failed", dev=False)}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        # Never consulted: history is not evidence.
        env.delivery_verifiers = {"research": _boom_verifier}
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "data_gaps")
        self.assertIn("history (cancelled/failed)", result["detail"])

    async def test_valid_delivery_closes_with_evidence(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": _child("kid-1", "completed", dev=False),
                 "kid-2": _child("kid-2", "cancelled", dev=False)}
        env, _planner, _created, patches, notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        seen: list[str] = []
        env.delivery_verifiers = {"research": _ok_verifier(seen)}
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertEqual(result["action"], "closed")
        # Only the completed delivery was consulted; the cancelled
        # sibling is history.
        self.assertEqual(seen, ["kid-1"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "closed")
        self.assertEqual(stored["final_reviews"], 1)
        delivery = stored["delivery"]
        self.assertEqual(delivery["review"], "deterministic")
        self.assertEqual(delivery["verified_by"], "final_review")
        self.assertEqual(delivery["criteria"], ["ship"])
        self.assertEqual(delivery["evidence"],
                         {"verified": {"kid-1": "research"}})
        self.assertEqual(patches["task-parent"]["status"], "completed")
        self.assertTrue(notices)

    async def test_mixed_work_reaches_pool_only_with_evidence(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-dev": _child("kid-dev", "completed", dev=True),
                 "kid-r": _child("kid-r", "completed", dev=False)}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.delivery_verifiers = {"research": _ok_verifier()}
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        # Empty pool: no quorum — but final_reviews == 1 proves the
        # evidence gates passed and the pool branch ran.
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_quorum")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["final_reviews"], 1)

    async def test_mixed_work_without_verifier_parks_capability_gap(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-dev": _child("kid-dev", "completed", dev=True),
                 "kid-r": _child("kid-r", "completed", dev=False)}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capability_gap")
        self.assertIn("research", result["detail"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored.get("final_reviews", 0), 0)

    async def test_verifier_error_parks_data_gaps(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-1": _child("kid-1", "completed", dev=False)}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        env.delivery_verifiers = {"research": _boom_verifier}
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "data_gaps")
        self.assertIn("verifier error: RuntimeError", result["detail"])

    async def test_unknown_domain_parks_capability_gap(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": self._parent(),
                 "kid-x": {"task_id": "kid-x", "title": "w",
                           "status": "completed", "objective_id": "OBJ-1",
                           "project": "proj", "acceptance": ["done"],
                           "params": {"expected_artifacts": ["x"]}}}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capability_gap")
        self.assertIn("unknown", result["detail"])


if __name__ == "__main__":
    unittest.main()
