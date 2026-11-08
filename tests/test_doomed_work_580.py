"""Doomed objective work is retired before planning (TASK-2026-00580).

Live case: in OBJ-E7A67DBA, #559 waited on #601/#602 (superseded by #603)
and on #605 (failed, replaced by #607). A failed or converted blocker never
releases, the planner can neither cancel nor rewire existing tickets, and a
replan even queued #620-#622 behind #559. The engine now retires every
doomed child (dependents first, so no cancel releases work early), the
validator refuses dependencies that never release or release without their
work, and retired tickets that never ran leave the max_tickets budget.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from test_company_objectives import (  # noqa: E402
    _dev_ticket,
    _env_for,
    _plan_result,
    _scratch_repo,
)


def _row(task_id: str, status: str, *blocked_by: str, number: int = 0,
         name: str | None = None, started: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "task_id": task_id, "title": f"work {task_id}", "status": status,
        "objective_id": "OBJ-1", "project": "proj",
        "blocked_by": list(blocked_by), "ticket_number": number or None,
        "acceptance": ["done"], "coding_tool": "tier_c",
        "params": {"target_agent": "dev", "expected_artifacts": ["code"]},
    }
    if name:
        row["params"]["_mirrors"] = {"business": {"name": name}}
    if started:
        row["started_at"] = "2026-11-07T16:45:52+00:00"
    return row


class _FakeErp:
    """Business port double: retire cancels in the shared task rows."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None,
                 fail_on: str = "", outcome: str = "applied") -> None:
        self.rows = rows
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.outcome = outcome

    async def retire(self, identifier: str, reason: str) -> dict[str, Any]:
        self.calls.append((identifier, reason))
        if identifier == self.fail_on:
            raise RuntimeError("ERP down")
        if self.rows is not None:
            for row in self.rows.values():
                mirror = row.get("params", {}).get("_mirrors", {})
                if mirror.get("business", {}).get("name") == identifier:
                    row["status"] = "cancelled"
                    row["error"] = f"Cancelado por transicion de {identifier}: {reason}"
        return {"ok": True, "outcome": self.outcome}

    async def convert_to_group(self, identifier: str) -> dict[str, Any]:
        return {"ok": True, "name": identifier}

    async def set_dependencies(self, identifier: str,
                               depends_on: list[str]) -> dict[str, Any]:
        return {"ok": True}

    async def thread_of(self, task_name: str, *,
                        limit: int = 20) -> dict[str, Any]:
        return {"comments": []}


def _live_chain() -> list[dict[str, Any]]:
    """#605 failed; #601 waits on it, #602 on #601, #559 on both, #620 on #559."""
    return [
        _row("t603", "completed", number=603),
        _row("t605", "failed", number=605, started=True),
        _row("t601", "queued", "t603", "t605", number=601, name="TASK-601"),
        _row("t602", "queued", "t601", number=602, name="TASK-602"),
        _row("t559", "queued", "t601", "t602", "t603", "t605", number=559,
             name="TASK-559"),
        _row("t620", "queued", "t559", number=620, name="TASK-620"),
    ]


class DoomedIdsTests(unittest.TestCase):
    def test_a_failed_blocker_dooms_the_whole_chain(self) -> None:
        self.assertEqual(engine_mod.doomed_ids(_live_chain()),
                         {"t601", "t602", "t559", "t620"})

    def test_a_live_blocker_beside_a_dead_one_does_not_save_it(self) -> None:
        rows = [_row("f", "failed"), _row("live", "in_progress"),
                _row("x", "queued", "live", "f")]
        self.assertEqual(engine_mod.doomed_ids(rows), {"x"})

    def test_released_or_absent_blockers_are_not_death(self) -> None:
        rows = [_row("done", "completed"), _row("gone", "cancelled"),
                _row("a", "queued", "done", "gone"), _row("b", "queued"),
                _row("c", "queued", "not-in-objective")]
        self.assertEqual(engine_mod.doomed_ids(rows), set())

    def test_a_converted_blocker_dooms_unless_a_rewire_is_pending(self) -> None:
        rows = [_row("conv", "converted"), _row("a", "queued", "conv"),
                _row("b", "queued", "a")]
        self.assertEqual(engine_mod.doomed_ids(rows), {"a", "b"})
        self.assertEqual(engine_mod.doomed_ids(rows, frozenset({"a"})), set())

    def test_a_cycle_is_no_evidence_of_death(self) -> None:
        rows = [_row("x", "queued", "y"), _row("y", "queued", "x")]
        self.assertEqual(engine_mod.doomed_ids(rows), set())

    def test_only_queued_work_is_doomed(self) -> None:
        rows = [_row("f", "failed"), _row("run", "in_progress", "f"),
                _row("wait", "waiting", "f")]
        self.assertEqual(engine_mod.doomed_ids(rows), set())


class UnreleasableTests(unittest.TestCase):
    def test_each_dead_or_empty_dependency_has_its_reason(self) -> None:
        rows = [_row("f", "failed"), _row("conv", "converted"),
                _row("gone", "cancelled"), _row("done", "completed"),
                _row("live", "queued"), _row("doomed", "queued", "f")]
        reasons = engine_mod.unreleasable_blockers(rows)
        self.assertEqual(set(reasons), {"f", "conv", "gone", "doomed"})
        self.assertIn("never releases", reasons["f"])
        self.assertIn("without its work", reasons["gone"])

    def test_a_dependent_awaiting_a_rewire_stays_a_valid_target(self) -> None:
        rows = [_row("conv", "converted"), _row("a", "queued", "conv")]
        self.assertNotIn(
            "a", engine_mod.unreleasable_blockers(rows, frozenset({"a"})))


class RetirementOrderTests(unittest.TestCase):
    def test_dependents_go_before_their_blockers(self) -> None:
        rows = _live_chain()
        order = engine_mod._retirement_order(
            rows, engine_mod.doomed_ids(rows))
        self.assertEqual(order, ["t620", "t559", "t602", "t601"])

    def test_a_doomed_ticket_with_an_open_dependent_outside_stays(self) -> None:
        # "rescued" awaits a conversion rewire but also waits on "a":
        # cancelling "a" would release it without a's work.
        rows = [_row("f", "failed"), _row("conv", "converted"),
                _row("a", "queued", "f"),
                _row("rescued", "queued", "conv", "a")]
        doomed = engine_mod.doomed_ids(rows, frozenset({"rescued"}))
        self.assertEqual(doomed, {"a"})
        self.assertEqual(engine_mod._retirement_order(rows, doomed), [])


class RetireDoomedWorkTests(unittest.IsolatedAsyncioTestCase):
    async def _env(self, erp: Any) -> Any:
        env, *_ = _env_for(self, _scratch_repo(self), [_plan_result()],
                           erp=erp)
        return env

    async def test_retires_the_chain_leaves_first_with_the_dead_blockers(
            self) -> None:
        erp = _FakeErp()
        env = await self._env(erp)
        retired = await engine_mod._retire_doomed_work(
            env, {"objective_id": "OBJ-1"}, _live_chain())
        self.assertEqual(retired, ["t620", "t559", "t602", "t601"])
        self.assertEqual([name for name, _ in erp.calls],
                         ["TASK-620", "TASK-559", "TASK-602", "TASK-601"])
        self.assertIn("#605 failed", erp.calls[3][1])
        self.assertIn("#559 queued", erp.calls[0][1])

    async def test_without_business_wiring_nothing_is_retired(self) -> None:
        env = await self._env(None)
        self.assertEqual(await engine_mod._retire_doomed_work(
            env, {"objective_id": "OBJ-1"}, _live_chain()), [])

    async def test_a_failed_retire_stops_before_releasing_its_dependent(
            self) -> None:
        erp = _FakeErp(fail_on="TASK-620")
        env = await self._env(erp)
        retired = await engine_mod._retire_doomed_work(
            env, {"objective_id": "OBJ-1"}, _live_chain())
        self.assertEqual(retired, [])
        self.assertEqual([name for name, _ in erp.calls], ["TASK-620"])

    async def test_an_escalated_cancel_also_stops_the_pass(self) -> None:
        erp = _FakeErp(outcome="escalated")
        env = await self._env(erp)
        retired = await engine_mod._retire_doomed_work(
            env, {"objective_id": "OBJ-1"}, _live_chain())
        self.assertEqual(retired, [])
        self.assertEqual(len(erp.calls), 1)

    async def test_a_leaf_without_business_name_stops_the_pass(self) -> None:
        rows = _live_chain()
        rows[-1]["params"].pop("_mirrors")
        erp = _FakeErp()
        env = await self._env(erp)
        self.assertEqual(await engine_mod._retire_doomed_work(
            env, {"objective_id": "OBJ-1"}, rows), [])
        self.assertEqual(erp.calls, [])

    async def test_a_pending_rewire_keeps_its_dependents(self) -> None:
        rows = [_row("conv", "converted", number=550),
                _row("a", "queued", "conv", number=559, name="TASK-559")]
        erp = _FakeErp()
        env = await self._env(erp)
        objective = {"objective_id": "OBJ-1", "pending_rewires": [
            {"from": "conv", "dependents": ["a"]}]}
        self.assertEqual(
            await engine_mod._retire_doomed_work(env, objective, rows), [])
        self.assertEqual(erp.calls, [])


class PlanCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_cycle_retires_first_and_refuses_dead_dependencies(
            self) -> None:
        rows = {row["task_id"]: row for row in _live_chain()}
        erp = _FakeErp(rows)
        bad = _plan_result(_dev_ticket("b", "Split B again",
                                       blocked_by=["t605"]))
        good = _plan_result(_dev_ticket("b", "Split B again",
                                        blocked_by=["t603"]))
        env, planner, created, _patches, notices = _env_for(
            self, _scratch_repo(self), [bad, good], erp=erp)
        env.tasks.tasks = rows
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "erp_group_ref": "TASK-2026-00900",
        })
        await engine_mod.plan_objective(env, "OBJ-1")
        self.assertEqual(len(erp.calls), 4)
        self.assertIn("Retired by the objective engine", planner.prompts[0])
        self.assertEqual(planner.runs, 2)
        self.assertIn("'t605' is failed", planner.prompts[1])
        self.assertEqual([item["blocked_by"] for item in created], [["t603"]])
        self.assertTrue(any("retired doomed work" in text
                            for _ref, text in notices))


class ValidatorTests(unittest.IsolatedAsyncioTestCase):
    async def _errors(self, blocked_by: list[str]) -> list[str]:
        result = planner_mod.parse_planner_result(__import__("json").dumps(
            _plan_result(_dev_ticket("x", "Next", blocked_by=blocked_by))))

        async def exists(_dep: str) -> bool:
            return True

        return await planner_mod.validate_planner_result(
            result, agents={"dev": {"enabled": True}}, open_titles=[],
            task_exists=exists,
            unreleasable={"dead": "failed, so it never releases"})

    async def test_a_dependency_that_never_releases_is_refused(self) -> None:
        errors = await self._errors(["dead"])
        self.assertEqual(len(errors), 1)
        self.assertIn("'dead' is failed, so it never releases", errors[0])

    async def test_live_dependencies_pass(self) -> None:
        self.assertEqual(await self._errors(["alive"]), [])


class BudgetTests(unittest.TestCase):
    def test_engine_retired_work_leaves_the_budget(self) -> None:
        retired = _row("retired", "cancelled", name="TASK-1")
        retired["params"]["_mirrors"]["business"]["transition"] = {
            "to": "Cancelled", "actor": engine_mod.ENGINE_ACTOR}
        human = _row("human", "cancelled", name="TASK-2")
        human["params"]["_mirrors"]["business"]["transition"] = {
            "to": "Cancelled"}
        rows = [_row("conv", "converted"), retired, human,
                _row("f", "failed", started=True), _row("q", "queued")]
        spent = {row["task_id"] for row in engine_mod._budget_children(rows)}
        self.assertEqual(spent, {"human", "f", "q"})


class AttributionTests(unittest.TestCase):
    def _cancelled(self, **transition: Any) -> dict[str, Any]:
        row = _row("x", "cancelled")
        row["params"]["_mirrors"] = {"business": {"name": "TASK-1",
            "transition": {"to": "Cancelled", "at": "2026-11-08T05:10",
                           "reason": "obsolete", **transition}}}
        return row

    def test_an_engine_retirement_is_no_business_decision(self) -> None:
        self.assertIsNone(engine_mod.business_decision(
            self._cancelled(actor=engine_mod.ENGINE_ACTOR)))

    def test_a_person_transition_still_is(self) -> None:
        decision = engine_mod.business_decision(self._cancelled())
        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision["channel"], "Owner session")


if __name__ == "__main__":
    unittest.main()
