"""Measured cost per ticket and per objective (TASK-2026-00212).

Executors report one run record per invocation; the ticket folds them
into ``cost`` and the objective aggregates its tickets for the CEO
strategy, the planner and the cockpit. Unknown is null, never 0.
No tokens.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

TESTS_DIR = Path(__file__).resolve().parent
CORE = TESTS_DIR.parent / "openceo-core"
for _path in (CORE, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from objectives import ticket_cost as cost_mod  # noqa: E402
from test_generic_agent_205 import (  # noqa: E402
    ECHO_SKILL_WORKFLOW,
    generic_task,
    hecho_stdout,
    scripted_executor,
    tiny_catalog,
    wiring_with,
    write_skill,
)
from test_horizon_cockpit import CockpitBase, _task as _cockpit_task  # noqa: E402


class NormalizeRunTests(unittest.TestCase):
    def test_generic_executor_shape(self) -> None:
        run = cost_mod.normalize_run({
            "engine": "muse-code",
            "model": "muse-spark-1.3-contributor",
            "duration_s": 12.5,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        })
        self.assertEqual(run["tokens_in"], 100)
        self.assertEqual(run["tokens_out"], 50)
        self.assertIsNone(run["tokens_cached"])
        self.assertEqual(run["duration_s"], 12.5)
        self.assertEqual(run["engine"], "muse-code")

    def test_alternate_engine_vocabulary(self) -> None:
        run = cost_mod.normalize_run({
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "cached_tokens": 5,
            "elapsed_s": 3,
        })
        self.assertEqual(
            (run["tokens_in"], run["tokens_out"], run["tokens_cached"]),
            (10, 20, 5),
        )
        self.assertEqual(run["duration_s"], 3.0)

    def test_top_level_beats_nested_usage(self) -> None:
        run = cost_mod.normalize_run({
            "tokens_in": 7,
            "usage": {"input_tokens": 999},
        })
        self.assertEqual(run["tokens_in"], 7)

    def test_garbage_degrades_to_null_never_zero(self) -> None:
        run = cost_mod.normalize_run({
            "tokens_in": -5,
            "tokens_out": True,
            "tokens_cached": "many",
            "duration_s": -1.0,
            "engine": "  ",
            "usage": {"input_tokens": 1.5},
        })
        self.assertIsNone(run["tokens_in"])
        self.assertIsNone(run["tokens_out"])
        self.assertIsNone(run["tokens_cached"])
        self.assertIsNone(run["duration_s"])
        self.assertIsNone(run["engine"])
        self.assertIsNone(run["model"])

    def test_exact_skill_zero_is_measured_not_unknown(self) -> None:
        run = cost_mod.normalize_run({
            "engine": "exact-skill",
            "duration_s": 0.4,
            "tokens_in": 0,
            "tokens_out": 0,
            "tokens_cached": 0,
        })
        self.assertEqual(
            (run["tokens_in"], run["tokens_out"], run["tokens_cached"]),
            (0, 0, 0),
        )

    def test_none_input_is_all_null(self) -> None:
        run = cost_mod.normalize_run(None)
        self.assertIsNone(run["tokens_in"])
        self.assertIsNone(run["duration_s"])
        self.assertIsNone(run["engine"])


class RunBuildersTests(unittest.TestCase):
    def test_from_exec_result(self) -> None:
        run = cost_mod.run_from_exec_result(
            usage={"stdout_chars": 10},
            elapsed_s=4.2,
            engine="muse-code",
            model="m",
        )
        self.assertEqual(run["engine"], "muse-code")
        self.assertEqual(run["model"], "m")
        self.assertEqual(run["duration_s"], 4.2)
        self.assertIsNone(run["tokens_in"])

    def test_from_tier_s_session(self) -> None:
        run = cost_mod.run_from_session({
            "role": "builder",
            "slot": "musej",
            "model": "m",
            "duration_s": 9.0,
            "exit_code": 0,
            "usage": {"stdout_chars": 3},
        })
        self.assertEqual(run["engine"], "musej")
        self.assertEqual(run["model"], "m")
        self.assertEqual(run["duration_s"], 9.0)
        self.assertIsNone(run["tokens_out"])


class RecordFoldTests(unittest.TestCase):
    def test_first_unmeasured_run_is_null_tokens(self) -> None:
        cost = cost_mod.record_run(None, {"engine": "muse-code",
                                          "duration_s": 2.0})
        self.assertEqual(cost["runs"], 1)
        self.assertIsNone(cost["tokens_in"])
        self.assertIsNone(cost["tokens_out"])
        self.assertIsNone(cost["tokens_cached"])
        self.assertEqual(cost["tokens_unknown_runs"], 1)
        self.assertEqual(cost["duration_s"], 2.0)
        self.assertEqual(cost["engines"], ["muse-code"])
        self.assertIsNone(cost["money"])

    def test_measured_and_unmeasured_mix(self) -> None:
        cost = cost_mod.record_runs(None, [
            {"tokens_in": 100, "tokens_out": 50, "duration_s": 1.0,
             "engine": "b"},
            {"duration_s": 2.0, "engine": "a"},
            {"tokens_in": 0, "tokens_out": 0, "tokens_cached": 0,
             "duration_s": 0.5, "engine": "exact-skill"},
        ])
        self.assertEqual(cost["runs"], 3)
        self.assertEqual(cost["tokens_in"], 100)
        self.assertEqual(cost["tokens_out"], 50)
        self.assertEqual(cost["tokens_cached"], 0)
        self.assertEqual(cost["tokens_unknown_runs"], 1)
        self.assertEqual(cost["duration_s"], 3.5)
        self.assertEqual(cost["engines"], ["a", "b", "exact-skill"])

    def test_partial_legacy_cost_is_tolerated(self) -> None:
        cost = cost_mod.record_run({"runs": 2, "junk": True}, None)
        self.assertEqual(cost["runs"], 3)
        self.assertIsNone(cost["tokens_in"])
        self.assertEqual(cost["tokens_unknown_runs"], 1)
        self.assertNotIn("junk", cost)

    def test_money_survives_folds(self) -> None:
        cost = cost_mod.record_money(None, amount="12.50", currency="USD")
        folded = cost_mod.record_run(cost, {"duration_s": 1.0})
        self.assertEqual(folded["money"], "12.50")
        self.assertEqual(folded["money_currency"], "USD")

    def test_blank_money_is_refused_never_zero(self) -> None:
        cost = cost_mod.record_money(None, amount=" ", currency="USD")
        self.assertIsNone(cost["money"])
        self.assertIsNone(cost["money_currency"])


class AggregateTests(unittest.TestCase):
    def test_empty_is_none_never_zeros(self) -> None:
        self.assertIsNone(cost_mod.aggregate_objective_cost([]))
        self.assertIsNone(cost_mod.aggregate_objective_cost(None))
        self.assertIsNone(cost_mod.aggregate_objective_cost([None, None]))

    def test_sums_tickets(self) -> None:
        first = cost_mod.record_runs(None, [
            {"tokens_in": 10, "duration_s": 1.0, "engine": "a",
             "model": "m1"},
        ])
        second = cost_mod.record_runs(None, [
            {"duration_s": 2.0, "engine": "b", "model": "m2"},
            {"tokens_in": 5, "tokens_out": 1, "duration_s": 3.0,
             "engine": "a", "model": "m1"},
        ])
        total = cost_mod.aggregate_objective_cost([first, second, None])
        assert total is not None
        self.assertEqual(total["runs"], 3)
        self.assertEqual(total["tokens_in"], 15)
        self.assertEqual(total["tokens_out"], 1)
        self.assertIsNone(total["tokens_cached"])
        self.assertEqual(total["tokens_unknown_runs"], 1)
        self.assertEqual(total["duration_s"], 6.0)
        self.assertEqual(total["engines"], ["a", "b"])
        self.assertEqual(total["models"], ["m1", "m2"])
        self.assertIsNone(total["money"])


class RenderTests(unittest.TestCase):
    def test_summary_without_runs(self) -> None:
        self.assertEqual(cost_mod.cost_summary_line(None),
                         "(no measured runs)")
        self.assertEqual(cost_mod.cost_summary_line({"runs": 0}),
                         "(no measured runs)")

    def test_summary_names_unknowns(self) -> None:
        cost = cost_mod.record_runs(None, [
            {"tokens_in": 10, "duration_s": 1.5, "engine": "a"},
            {"duration_s": 2.0, "engine": "b"},
        ])
        line = cost_mod.cost_summary_line(cost)
        self.assertIn("runs=2", line)
        self.assertIn("tokens_in=10", line)
        self.assertIn("tokens_out=?", line)
        self.assertIn("1 run(s) unmeasured", line)
        self.assertIn("duration_s=3.5", line)
        self.assertIn("engines=[a, b]", line)
        self.assertIn("money=unknown", line)

    def test_sections_skip_empty_cost(self) -> None:
        self.assertEqual(cost_mod.objective_cost_section(None), "")
        self.assertEqual(cost_mod.pedido_cost_line({"runs": 0}), "")

    def test_sections_carry_the_line(self) -> None:
        cost = cost_mod.record_run(None, {"duration_s": 1.0})
        section = cost_mod.objective_cost_section(cost)
        self.assertIn("MEASURED COST", section)
        self.assertIn("runs=1", section)
        self.assertIn("information only", section)
        line = cost_mod.pedido_cost_line(cost)
        self.assertTrue(line.startswith("PRIOR RUNS: "))
        self.assertIn("runs=1", line)

    def test_render_tolerates_mangled_stored_cost(self) -> None:
        # Review 00013/H5: a legacy or hand-edited cost renders as
        # unmeasured instead of raising inside a planner prompt.
        mangled: dict[str, Any] = {"runs": "many", "duration_s": "soon",
                                   "engines": "muse", "tokens_in": "x"}
        self.assertEqual(cost_mod.cost_summary_line(mangled),
                         "(no measured runs)")
        self.assertEqual(cost_mod.objective_cost_section(mangled), "")
        self.assertEqual(cost_mod.pedido_cost_line(mangled), "")
        readable = cost_mod.cost_summary_line(
            {"runs": 2, "tokens_in": 5, "duration_s": 3.0,
             "engines": ["a"]})
        self.assertIn("runs=2", readable)
        self.assertIn("tokens_in=5", readable)


class GenericAgentCostTests(unittest.TestCase):
    def _world(self, stdout: str, returncode: int = 0,
               usage: dict[str, Any] | None = None):
        from orchestration.generic_agent import run_generic_agent
        from orchestration.live import ExecResult

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        catalog = tiny_catalog({"operations": {"enabled": True}})
        factory, _ = scripted_executor(
            ExecResult(returncode=returncode, elapsed_s=1.0,
                       output_tail=stdout, usage=usage,
                       model="muse-spark-1.3-contributor"))
        wiring = wiring_with(root, catalog, factory)
        out = asyncio.run(run_generic_agent(
            "operations", generic_task(), wiring))
        return out

    def test_llm_run_reports_its_cost(self) -> None:
        out = self._world(hecho_stdout("done"),
                          usage={"input_tokens": 30, "output_tokens": 12})
        runs = out[cost_mod.COST_RUNS_KEY]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["engine"], "muse-code")
        self.assertEqual(runs[0]["model"], "muse-spark-1.3-contributor")
        self.assertEqual(runs[0]["tokens_in"], 30)
        self.assertEqual(runs[0]["tokens_out"], 12)
        self.assertEqual(runs[0]["duration_s"], 1.0)

    def test_failed_run_still_reports(self) -> None:
        out = self._world("boom", returncode=1)
        runs = out[cost_mod.COST_RUNS_KEY]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["engine"], "muse-code")

    def test_pre_run_wait_reports_nothing(self) -> None:
        from orchestration.generic_agent import run_generic_agent

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        catalog = tiny_catalog({"operations": {"enabled": True}})

        def _forbidden(role: str, spec: dict[str, Any],
                       provider: Any) -> Any:
            raise AssertionError("no run happened")

        wiring = wiring_with(root, catalog, _forbidden)
        out = asyncio.run(run_generic_agent(
            "operations",
            generic_task(params={"skill": "missing-skill"}), wiring))
        self.assertNotIn(cost_mod.COST_RUNS_KEY, out)

    def test_exact_skill_reports_zero_measured_tokens(self) -> None:
        from orchestration.generic_agent import run_generic_agent

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write_skill(root, "operations", "probe", ECHO_SKILL_WORKFLOW)

        def _forbidden(role: str, spec: dict[str, Any],
                       provider: Any) -> Any:
            raise AssertionError("exact skills never invoke the LLM")

        catalog = tiny_catalog({"operations": {"enabled": True}})
        wiring = wiring_with(root, catalog, _forbidden, repo=None)
        out = asyncio.run(run_generic_agent(
            "operations", generic_task(params={"skill": "probe"}), wiring))
        runs = out[cost_mod.COST_RUNS_KEY]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["engine"], "exact-skill")
        self.assertEqual(
            (runs[0]["tokens_in"], runs[0]["tokens_out"],
             runs[0]["tokens_cached"]),
            (0, 0, 0),
        )


class SimpleTierCostTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        from agents.dev.session import DevSessionManager

        settings = MagicMock()
        settings.DEV_EXECUTION_MODE = "real"
        loader = MagicMock()
        loader.validate_project = MagicMock(return_value=[])
        return DevSessionManager(settings, loader, repo=None)

    async def test_code_step_attaches_its_run(self) -> None:
        from agents.dev.session import CodingTool

        manager = self._manager()
        manager._resolve_tier_executor = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(argv=["musej", "exec"], prompt_via="argv",
                                   cwd=None, env={}, label="tier_c:muse"))

        async def _ok(command, stdin_payload=None, timeout=120, cwd=None,
                      env=None, sandbox=True, config_argv=None,
                      home_file_overlays=None, extra_read_roots=()):
            return True, "did the thing", ""

        manager._run_process = _ok  # type: ignore[method-assign]
        provider_stub = MagicMock()
        provider_stub.revoke = AsyncMock()
        manager._ticket_badge_provider = MagicMock(  # type: ignore[method-assign]
            return_value=provider_stub)
        manager._muse_role_overlays = AsyncMock(  # type: ignore[method-assign]
            return_value=({}, "badge-stub"))
        result = await manager._step_code(
            {"task_id": "t", "title": "t", "description": "d"}, {},
            CodingTool.TIER_C)
        assert result.cost_run is not None
        self.assertEqual(result.cost_run["engine"], "tier_c:muse")
        self.assertIsNotNone(result.cost_run["duration_s"])
        self.assertIsNone(result.cost_run["tokens_in"])

    async def test_failed_code_step_still_attaches(self) -> None:
        from agents.dev.session import CodingTool

        manager = self._manager()
        manager._resolve_tier_executor = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(argv=["x"], prompt_via="argv",
                                   cwd=None, env={}, label="tier_a",
                                   role_profile=None))

        async def _fail(command, stdin_payload=None, timeout=120, cwd=None,
                        env=None, sandbox=True, config_argv=None,
                        home_file_overlays=None, extra_read_roots=()):
            return False, "", "cli exploded"

        manager._run_process = _fail  # type: ignore[method-assign]
        result = await manager._step_code(
            {"task_id": "t", "title": "t", "description": "d"}, {},
            CodingTool.TIER_A)
        assert result.cost_run is not None
        self.assertEqual(result.cost_run["engine"], "tier_a")

    async def test_followup_turn_returns_its_run(self) -> None:
        # Review 00013/H1: gate/handoff fix turns are executor turns on
        # the ticket and report their cost like the code step.
        from agents.dev.session import CodingTool

        manager = self._manager()
        manager._resolve_tier_executor = AsyncMock(  # type: ignore[method-assign]
            return_value=MagicMock(argv=["musej", "exec"], prompt_via="argv",
                                   cwd=None, env={}, label="tier_c:muse"))

        async def _ok(command, stdin_payload=None, timeout=120, cwd=None,
                      env=None, sandbox=True, config_argv=None,
                      home_file_overlays=None, extra_read_roots=()):
            return True, "fixed", ""

        manager._run_process = _ok  # type: ignore[method-assign]
        provider_stub = MagicMock()
        provider_stub.revoke = AsyncMock()
        manager._ticket_badge_provider = MagicMock(  # type: ignore[method-assign]
            return_value=provider_stub)
        manager._muse_role_overlays = AsyncMock(  # type: ignore[method-assign]
            return_value=({}, "badge-stub"))
        with tempfile.TemporaryDirectory() as tmp:
            run = await manager._simple_followup(
                CodingTool.TIER_C, {}, Path(tmp), "Fix the code.")
        assert run is not None
        self.assertEqual(run["engine"], "tier_c:muse")
        self.assertIsNotNone(run["duration_s"])


class TierSCostDeltaTests(unittest.TestCase):
    def _ctx(self, sessions, base):
        from agents.dev.tier_s.cycle import TierSCycle, _Ctx

        ctx = _Ctx(ticket={"task_id": "t"}, repo=Path("/tmp"),
                   ref="t", run_id="r", branch="b", base_branch="main",
                   budgets={}, timeouts={}, waiting_cfg={}, auto_merge=True,
                   budget_exclude=[], gate_specs=[], roles={})
        ctx.metrics["sessions"] = sessions
        ctx.cost_sessions_base = base
        cycle = TierSCycle(MagicMock(), MagicMock(), MagicMock(),
                           MagicMock())
        return cycle, ctx

    def test_delta_covers_only_fresh_sessions(self) -> None:
        old = {"role": "planner", "slot": "claudej", "model": "m",
               "duration_s": 5.0, "exit_code": 0, "usage": {}}
        fresh = {"role": "builder", "slot": "musej", "model": "m",
                 "duration_s": 7.0, "exit_code": 0,
                 "usage": {"input_tokens": 40}}
        cycle, ctx = self._ctx([old, fresh], 1)
        delta = cycle._cost_delta(ctx)
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["engine"], "musej")
        self.assertEqual(delta[0]["tokens_in"], 40)
        self.assertEqual(delta[0]["duration_s"], 7.0)

    def test_empty_delta_on_resume_without_new_sessions(self) -> None:
        old = {"role": "planner", "slot": "claudej", "model": "m",
               "duration_s": 5.0, "exit_code": 0, "usage": {}}
        cycle, ctx = self._ctx([old], 1)
        self.assertEqual(cycle._cost_delta(ctx), ())


class RunTaskFoldTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, behavior, task=None):
        from settings import Settings
        from state import TaskStatus
        from task_service import TaskService

        repo_task = {
            "task_id": "task-1", "project": "proj", "agent": "dev",
            "agent_hint": "dev", "source": "api", "title": "Ticket",
            "description": "Descripcion", "attachments": [],
            "priority": "medium", "status": TaskStatus.QUEUED.value,
            "steps": [], "params": {},
        }
        if task:
            repo_task.update(task)

        class _Repo:
            def __init__(self) -> None:
                self.task = dict(repo_task)

            async def get_task(self, task_id: str):
                return dict(self.task) if task_id == "task-1" else None

            async def patch_task(self, task_id: str, updates: dict):
                self.task.update(updates)
                return dict(self.task)

            async def list_tasks_blocked_by(self, task_id: str):
                return []

        class _Graph:
            async def execute(self, task: dict, agent_hint=None):
                return behavior(task)

        class _Hub:
            async def broadcast(self, event: str, payload: dict) -> None:
                del event, payload

        repo = _Repo()
        service = TaskService(
            settings=Settings(), repo=repo, graph=_Graph(),
            realtime_hub=_Hub(), notion=None, rate_monitor=None)
        return service, repo

    def _out(self, **overrides):
        from state import TaskStatus

        out = {
            "selected_agent": "dev", "status": TaskStatus.COMPLETED.value,
            "steps": [], "last_prompt_sent": None,
            "last_response_summary": "s", "error": None,
        }
        out.update(overrides)
        return out

    async def test_graph_runs_fold_onto_the_ticket(self) -> None:
        service, repo = self._service(
            lambda task: self._out(cost_runs=[
                {"engine": "a", "duration_s": 2.0}]))
        result = await service.run_task("task-1")
        assert result is not None
        self.assertEqual(result["cost"]["runs"], 1)
        self.assertEqual(result["cost"]["engines"], ["a"])
        self.assertIsNone(result["cost"]["tokens_in"])

    async def test_second_run_accumulates(self) -> None:
        stored = cost_mod.record_run(None, {"engine": "a",
                                            "duration_s": 2.0})
        service, repo = self._service(
            lambda task: self._out(cost_runs=[
                {"engine": "b", "duration_s": 3.0,
                 "tokens_in": 10}]),
            task={"cost": stored})
        result = await service.run_task("task-1")
        assert result is not None
        self.assertEqual(result["cost"]["runs"], 2)
        self.assertEqual(result["cost"]["tokens_in"], 10)
        self.assertEqual(result["cost"]["engines"], ["a", "b"])

    async def test_no_runs_leaves_cost_absent(self) -> None:
        service, repo = self._service(lambda task: self._out())
        result = await service.run_task("task-1")
        assert result is not None
        self.assertNotIn("cost", result)


class CeoCostTests(unittest.TestCase):
    def test_digest_carries_the_objective_cost(self) -> None:
        from ceo.review import _objective_digest
        from objectives.engine import InMemoryTaskLister

        first = cost_mod.record_run(None, {"engine": "a",
                                           "duration_s": 2.0})
        lister = InMemoryTaskLister(tasks={
            "t1": {"task_id": "t1", "objective_id": "OBJ-1",
                   "status": "in_progress", "cost": first},
            "t2": {"task_id": "t2", "objective_id": "OBJ-1",
                   "status": "completed"},
        })
        env = SimpleNamespace(tasks=lister)
        digest = asyncio.run(_objective_digest(
            env, {"objective_id": "OBJ-1", "project": "p",
                  "status": "active"}))
        assert digest["cost"] is not None
        self.assertEqual(digest["cost"]["runs"], 1)
        self.assertEqual(digest["cost"]["duration_s"], 2.0)

    def test_section_renders_cost_only_when_measured(self) -> None:
        from ceo.review import CEOSignals, _objectives_section

        with_cost = {"objective_id": "OBJ-1", "project": "p",
                     "status": "active", "goal": "g",
                     "open_children": 1, "blocked_children": 0,
                     "closed_children": 0, "planning_runs": 1,
                     "tickets_created": 1, "material_failures": 0,
                     "tier_c_late_failure_rate": None,
                     "waiting_reason": "", "capability_gap": "",
                     "deferred": [], "roadmap_remaining": [],
                     "cost": cost_mod.record_run(None, {"engine": "a",
                                                        "duration_s": 2.0})}
        section = _objectives_section(
            CEOSignals(objectives=[with_cost]))
        self.assertIn("cost: runs=1", section)
        bare = dict(with_cost, cost=None)
        section = _objectives_section(CEOSignals(objectives=[bare]))
        self.assertNotIn("cost:", section)


class PlannerCostTests(unittest.TestCase):
    def _objective(self):
        return {"objective_id": "OBJ-1",
                "anchor": {"goal": "g", "acceptance": []}}

    def test_objective_prompt_carries_cost_section(self) -> None:
        from objectives.engine import _planner_prompt

        cost = cost_mod.record_run(None, {"engine": "a",
                                          "duration_s": 2.0})
        prompt = _planner_prompt(
            self._objective(), "", [], "INBOX\n(none)\n", [],
            cost_guide=cost_mod.objective_cost_section(cost))
        self.assertIn("MEASURED COST", prompt)
        self.assertIn("runs=1", prompt)
        bare = _planner_prompt(self._objective(), "", [],
                               "INBOX\n(none)\n", [])
        self.assertNotIn("MEASURED COST", bare)

    def test_pedido_prompt_carries_prior_runs(self) -> None:
        from objectives.engine import _pedido_prompt

        cost = cost_mod.record_run(None, {"engine": "a",
                                          "duration_s": 2.0})
        ticket: dict[str, Any] = {
            "task_id": "t", "title": "t", "project": "p",
            "priority": "medium", "description": "d", "cost": cost,
        }
        prompt = _pedido_prompt(ticket, agents_md="AGENTS")
        self.assertIn("PRIOR RUNS: runs=1", prompt)
        bare = _pedido_prompt({**ticket, "cost": None}, agents_md="AGENTS")
        self.assertNotIn("PRIOR RUNS", bare)


class CockpitCostTests(unittest.TestCase):
    def _company(self, objectives):
        return {
            "objectives": objectives,
            "recently_closed": [],
            "blocked": [],
            "business_open_without_execution": [],
            "business_unknown": False,
            "objectives_unknown": False,
        }

    def test_voice_names_measured_runs(self) -> None:
        from horizon import cockpit

        cost = cost_mod.record_runs(None, [
            {"engine": "a", "duration_s": 90.0},
            {"engine": "b", "duration_s": 90.0},
        ])
        message = cockpit.describe_company(self._company(
            [{"goal": "Vender", "cost": cost}]))
        self.assertIn("2 corridas de agentes", message)
        self.assertIn("3 minutos", message)

    def test_voice_stays_silent_without_runs(self) -> None:
        from horizon import cockpit

        message = cockpit.describe_company(self._company(
            [{"goal": "Vender", "cost": None}]))
        self.assertNotIn("corrida", message)
        message = cockpit.describe_company(self._company(
            [{"goal": "Vender"}]))
        self.assertNotIn("corrida", message)


class CompanyStatusCostTests(CockpitBase):
    async def test_objective_entry_carries_cost(self) -> None:
        self.repo.company_objectives.append(
            {
                "objective_id": "OBJ-1",
                "project": "jardal-openceo",
                "entry": "top_down",
                "status": "active",
                "anchor": {"goal": "Vender mas"},
                "erp_group_ref": "TASK-2026-00900",
            }
        )
        self.repo.company_tasks["t1"] = _cockpit_task(
            "t1", status="in_progress", objective_id="OBJ-1",
            cost=cost_mod.record_run(None, {"engine": "a",
                                            "duration_s": 120.0}),
        )
        company = await self.service.company_status()
        objective = company["objectives"][0]
        assert objective["cost"] is not None
        self.assertEqual(objective["cost"]["runs"], 1)
        self.assertIn("una corrida", company["message"])
        self.assertIn("2 minutos", company["message"])


if __name__ == "__main__":
    unittest.main()
