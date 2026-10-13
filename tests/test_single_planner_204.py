"""Single planner for every pedido (TASK-2026-00204).

A loose ticket without an explicit decision is a pedido: the single global
planner decides it (ticket or objective, agent, suggested skill and, for
dev, the tier). Explicit requests only pass deterministic validation
against the catalog and never run the planner. No route classifies by
keywords, config triggers or an LLM fallback anymore, and the loose-tier
classifier is gone: explicit dev without a tier runs the safe C default
(policy §25), everything undecided waits fail-closed.

No network, no CLI, no LLM: the planner runs simulated.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from objectives import engine as engine_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
)
from objectives.pedido import is_pedido  # noqa: E402
from state import TaskStatus  # noqa: E402


def _output_target(repo: Path, prompt: str) -> Path:
    for line in prompt.splitlines():
        if line.startswith("OUTPUT: write "):
            rel = line[len("OUTPUT: write "):].split(" with exactly")[0]
            return repo / rel.strip()
    raise AssertionError("planner prompt names no OUTPUT target")


class _RecordedPlanner:
    """Simulated Muse headless run: writes the recorded JSON (0 tokens)."""

    def __init__(self, repo: Path, payloads: list[dict[str, Any]]) -> None:
        self._repo = repo
        self._payloads = payloads
        self.prompts: list[str] = []
        self.runs = 0
        self._target: Path | None = None

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "objective_planner"
        self.prompts.append(prompt)
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        try:
            target = _output_target(self._repo, prompt)
            self._target = target
        except AssertionError:
            assert self._target is not None
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload["__raw__"]
                          if "__raw__" in payload
                          else json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _pedido_env(test: unittest.TestCase, repo: Path,
                payloads: list[dict[str, Any]],
                ) -> tuple[ObjectiveEnv, _RecordedPlanner,
                           list[dict[str, Any]], dict[str, dict[str, Any]]]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: decide the pedido, then stop.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    patches: dict[str, dict[str, Any]] = {}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        created.append(dict(payload))
        return {"task_id": f"task-new-{len(created)}"}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(updates)

    planner = _RecordedPlanner(repo, payloads)
    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister({}),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=planner,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=repo,
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True},
                       "secretary": {"enabled": True}},
        },
    )
    test.addCleanup(lambda: None)
    return env, planner, created, patches


def _ticket_decision(**overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "local_ref": "pedido-1",
        "title": "Decided pedido",
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": "decide this pedido",
        "why_now": "it waits for a decision",
        "acceptance_criteria": ["it runs"],
        "expected_artifacts": ["a run"],
        "coding_tool": "tier_b",
        "tier_reason": "complex strong solving",
        "why_not_c": "a single default session would drift",
    }
    ticket.update(overrides)
    return ticket


def _plan_result(ticket: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "objective_status": "active",
        "state_summary": "the pedido is decided",
        "tickets": [ticket or _ticket_decision()],
        "roadmap_remaining": [],
    }
    result.update(overrides)
    return result


def _pedido(**overrides: Any) -> dict[str, Any]:
    # Persisted shape: real rows carry the agent="ceo" default, which is
    # undecided, not a decision.
    ticket: dict[str, Any] = {
        "task_id": "task-pedido-1",
        "title": "Undecided loose ticket",
        "description": "no agent, no tier, no target",
        "project": "proj",
        "source": "api",
        "agent": "ceo",
        "status": TaskStatus.QUEUED.value,
        "params": {},
    }
    ticket.update(overrides)
    params = dict(ticket.get("params") or {})
    if "params" in overrides and isinstance(overrides["params"], dict):
        params = dict(overrides["params"])
    ticket["params"] = params
    return ticket


class PedidoPredicateTests(unittest.TestCase):
    def test_loose_without_decision_is_pedido(self) -> None:
        self.assertTrue(is_pedido(_pedido()))

    def test_coding_tool_is_explicit(self) -> None:
        self.assertFalse(is_pedido(_pedido(coding_tool="tier_c")))

    def test_agent_hint_is_explicit(self) -> None:
        self.assertFalse(is_pedido(_pedido(agent_hint="secretary")))
        self.assertFalse(is_pedido(_pedido(agent="dev")))

    def test_target_agent_is_explicit(self) -> None:
        self.assertFalse(is_pedido(
            _pedido(params={"target_agent": "dev"})))

    def test_objective_child_is_not_pedido(self) -> None:
        self.assertFalse(is_pedido(_pedido(objective_id="OBJ-1")))

    def test_workflow_targets_are_explicit(self) -> None:
        for params in ({"workflow": "finance.register_expense"},
                       {"use_workflow": True}):
            with self.subTest(params=params):
                self.assertFalse(is_pedido(_pedido(params=params)))
        self.assertFalse(is_pedido(_pedido(
            workflow_phases=[{"key": "qa_review", "type": "human"}])))

    def test_retired_discovery_flags_decide_nothing(self) -> None:
        for params in ({"discover_workflow": True},
                       {"workflow_discovery": True}):
            with self.subTest(params=params):
                self.assertTrue(is_pedido(_pedido(params=params)))

    def test_skill_alone_is_not_explicit(self) -> None:
        self.assertTrue(is_pedido(
            _pedido(params={"skill": "enviar-correo"})))

    def test_blank_values_do_not_count(self) -> None:
        self.assertTrue(is_pedido(_pedido(
            coding_tool="  ", agent_hint="", params={"target_agent": None})))

    def test_owner_question_is_never_a_pedido(self) -> None:
        self.assertFalse(is_pedido(_pedido(
            params={"owner_question": {"kind": "spend_approval"}})))

    def test_blank_workflow_does_not_count(self) -> None:
        self.assertTrue(is_pedido(_pedido(params={"workflow": "  "})))
        self.assertTrue(is_pedido(_pedido(params={"use_workflow": False})))
        self.assertFalse(is_pedido(_pedido(params={"use_workflow": True})))


class PedidoPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_pedido_gets_validated_decision(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, planner, _created, patches = _pedido_env(
                self, repo, [_plan_result()])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["action"], "decided")
            self.assertEqual(planner.runs, 1)
            patched = patches.get("task-pedido-1") or {}
            self.assertEqual(patched.get("agent_hint"), "dev")
            self.assertEqual(patched.get("coding_tool"), "tier_b")
            params = patched.get("params") or {}
            self.assertEqual(params.get("target_agent"), "dev")
            self.assertEqual(params.get("tier_reason"), "complex strong solving")
            self.assertEqual(params.get("decided_by"), "planner")
            self.assertRegex(params.get("planner_policy_sha") or "",
                             r"^[0-9a-f]{12}$")
            self.assertNotIn("planner_attempts", params)

    async def test_skill_suggestion_patched(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ticket = _ticket_decision(target_agent="operations",
                                      skill="crear-proyecto")
            for key in ("coding_tool", "tier_reason", "why_not_c"):
                del ticket[key]
            env, _planner, _created, patches = _pedido_env(
                self, repo, [_plan_result(ticket)])
            env.planner_cfg["agents"]["operations"] = {"enabled": True}
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            patched = patches.get("task-pedido-1") or {}
            self.assertEqual(patched.get("agent_hint"), "operations")
            self.assertNotIn("coding_tool", patched)
            params = patched.get("params") or {}
            self.assertEqual(params.get("skill"), "crear-proyecto")

    async def test_prompt_carries_pedido_and_staged_policy(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, planner, _created, _patches = _pedido_env(
                self, repo, [_plan_result()])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            prompt = planner.prompts[0]
            self.assertIn("Undecided loose ticket", prompt)
            self.assertIn("no agent, no tier, no target", prompt)
            self.assertIn("POLICY: decide the pedido", prompt)
            self.assertIn("REGISTERED AGENTS", prompt)
            self.assertIn("exactly one ticket", prompt)
            target = _output_target(repo, prompt)
            self.assertTrue(str(target).endswith("PLANNER_RESULT.json"))
            self.assertIn("task-pedido-1", str(target))

    async def test_money_guide_reaches_pedido_prompt(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, planner, _created, _patches = _pedido_env(
                self, repo, [_plan_result()])
            env.project_routing = {"money_sensitive": True}
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            self.assertIn("PROJECT TIER GUIDE", planner.prompts[0])

    async def test_unknown_agent_rejected_and_counted(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ticket = _ticket_decision(target_agent="nope")
            for key in ("coding_tool", "tier_reason", "why_not_c"):
                del ticket[key]
            env, planner, _created, patches = _pedido_env(
                self, repo, [_plan_result(ticket)])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "planner_error")
            self.assertIn("unknown target_agent", out["detail"])
            self.assertEqual(planner.runs, 2)
            params = (patches.get("task-pedido-1") or {}).get("params") or {}
            self.assertEqual(params.get("planner_attempts"), 1)

    async def test_second_ticket_rejected_by_horizon(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, _patches = _pedido_env(self, repo, [{
                "objective_status": "active",
                "state_summary": "two",
                "tickets": [_ticket_decision(local_ref="a"),
                            _ticket_decision(local_ref="b")],
                "roadmap_remaining": [],
            }])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertFalse(out["ok"])
            self.assertIn("1-1", out["detail"])

    async def test_dev_without_tier_is_rejected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ticket = _ticket_decision()
            del ticket["coding_tool"]
            del ticket["tier_reason"]
            del ticket["why_not_c"]
            env, _planner, _created, _patches = _pedido_env(
                self, repo, [_plan_result(ticket)])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertFalse(out["ok"])
            self.assertIn("coding_tool", out["detail"])

    async def test_completion_entry_is_rejected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, _patches = _pedido_env(self, repo, [
                _plan_result(_ticket_decision(completes_task_id="task-x"))])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertFalse(out["ok"])
            self.assertIn("completes_task_id", out["detail"])

    async def test_blocked_status_is_rejected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, _patches = _pedido_env(self, repo, [{
                "objective_status": "blocked",
                "state_summary": "stuck",
                "tickets": [],
                "roadmap_remaining": [],
            }])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertFalse(out["ok"])
            self.assertIn("capability_gap", out["detail"])

    async def test_capability_gap_converts(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, patches = _pedido_env(self, repo, [{
                "objective_status": "capability_gap",
                "state_summary": "outgrows one ticket",
                "information_gaps": [{"gap": "needs a team",
                                      "materiality": "high",
                                      "resolution": "objective"}],
                "tickets": [],
                "costs": [{"label": "team time", "amount": "100"}],
                "roadmap_remaining": ["first step"],
            }])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["action"], "convert")
            self.assertIn("goal", out)
            self.assertEqual(len(out["costs"]), 1)
            patched = patches.get("task-pedido-1") or {}
            self.assertNotIn("agent_hint", patched)

    async def test_tool_proposals_open_questions(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ticket = _ticket_decision()
            env, _planner, created, _patches = _pedido_env(self, repo, [
                _plan_result(ticket, proposed_tools=[{
                    "name": "dns.delete_record",
                    "need": "delete stale subdomains",
                }])])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            self.assertEqual(len(created), 1)
            self.assertIn("task-pedido-1", created[0].get("source_id") or "")

    async def test_spend_verdict_rides_the_decision(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ticket = _ticket_decision(costs=[{"label": "api calls",
                                               "amount": "999999"}])
            env, _planner, _created, _patches = _pedido_env(
                self, repo, [_plan_result(ticket)])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            self.assertIn("spend", out)
            self.assertTrue(out["spend"]["escalate"])

    async def test_return_carries_agent_returns(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, planner, _created, patches = _pedido_env(
                self, repo, [_plan_result()])
            entry = {"task_id": "task-pedido-1", "title": "Undecided",
                     "agent": "dev", "resultado": "no_aplica",
                     "motivo": "needs design first",
                     "necesidad": "a design doc"}
            out = await engine_mod.plan_pedido(
                env, _pedido(params={"agent_result": {"resultado": "no_aplica"}}),
                return_from=entry)
            self.assertTrue(out["ok"], out)
            self.assertIn("AGENT RETURNS", planner.prompts[0])
            self.assertIn("needs design first", planner.prompts[0])
            params = (patches.get("task-pedido-1") or {}).get("params") or {}
            self.assertEqual(params.get("returns_from"), "task-pedido-1")
            self.assertNotIn("agent_result", params)

    async def test_success_resets_attempts(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, patches = _pedido_env(
                self, repo, [_plan_result()])
            out = await engine_mod.plan_pedido(
                env, _pedido(params={"planner_attempts": 2}))
            self.assertTrue(out["ok"], out)
            params = (patches.get("task-pedido-1") or {}).get("params") or {}
            self.assertNotIn("planner_attempts", params)

    async def test_retry_carries_full_context(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            bad = _plan_result(_ticket_decision(local_ref="a"))
            bad["tickets"].append(_ticket_decision(local_ref="b"))
            env, planner, _created, _patches = _pedido_env(
                self, repo, [bad, _plan_result()])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            self.assertEqual(planner.runs, 2)
            retry = planner.prompts[1]
            self.assertIn("Undecided loose ticket", retry)
            self.assertIn("OUTPUT: write", retry)
            self.assertIn("was invalid", retry)

    async def test_redecision_away_from_dev_clears_tier(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            ticket = _ticket_decision(target_agent="secretary")
            for key in ("coding_tool", "tier_reason", "why_not_c"):
                del ticket[key]
            env, _planner, _created, patches = _pedido_env(
                self, repo, [_plan_result(ticket)])
            decided = _pedido(
                agent_hint="dev", coding_tool="tier_b",
                params={"target_agent": "dev", "tier_reason": "old",
                        "tier_assigned_by": "x", "oracle": {"type": "t"},
                        "why_not_c": "old", "executor_link": "keep"})
            entry = {"task_id": "task-pedido-1", "title": "t", "agent": "dev",
                     "resultado": "no_aplica", "motivo": "m", "necesidad": "n"}
            out = await engine_mod.plan_pedido(
                env, decided, return_from=entry)
            self.assertTrue(out["ok"], out)
            patched = patches.get("task-pedido-1") or {}
            self.assertEqual(patched.get("agent_hint"), "secretary")
            self.assertIsNone(patched.get("coding_tool"))
            params = patched.get("params") or {}
            for key in ("tier_reason", "tier_assigned_by", "oracle",
                        "why_not_c"):
                self.assertNotIn(key, params)
            self.assertEqual(params.get("executor_link"), "keep")

    async def test_top_level_costs_collected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, _patches = _pedido_env(self, repo, [
                _plan_result(
                    costs=[{"label": "runway", "amount": "1000000"}])])
            out = await engine_mod.plan_pedido(env, _pedido())
            self.assertTrue(out["ok"], out)
            labels = [entry["label"]
                      for entry in out["decision"]["costs"]]
            self.assertTrue(any("runway" in label for label in labels))


class _FakeHub:
    async def broadcast(self, event: str, payload: dict) -> None:
        return None


class _FakeRepo:
    def __init__(self, task: dict[str, Any]) -> None:
        self.task = dict(task)
        self.comments: list[str] = []

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return dict(self.task) if task_id == self.task["task_id"] else None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        if task_id != self.task["task_id"]:
            return None
        self.task.update(updates)
        return dict(self.task)

    async def list_tasks_blocked_by(self, task_id: str) -> list:
        return []


class _FakeGraph:
    def __init__(self, out: dict[str, Any]) -> None:
        self._out = out
        self.seen: list[dict[str, Any]] = []
        self.hints: list[str | None] = []

    async def execute(self, task: dict, agent_hint: str | None = None):
        self.seen.append(dict(task))
        self.hints.append(agent_hint)
        return dict(self._out)


def _run_service(task: dict[str, Any], graph_out: dict[str, Any]):
    from settings import Settings  # noqa: E402
    from task_service import TaskService  # noqa: E402
    repo = _FakeRepo(task)
    graph = _FakeGraph(graph_out)
    service = TaskService(
        settings=Settings(), repo=repo, graph=graph,
        realtime_hub=_FakeHub(), notion=None, rate_monitor=None)
    service._workflow_service = None
    return service, repo, graph


def _completed_out(**overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "selected_agent": "dev", "status": TaskStatus.COMPLETED.value,
        "steps": [], "last_prompt_sent": None, "last_response_summary": None,
        "error": None,
    }
    out.update(overrides)
    return out


class ExplicitSkipsPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_ticket_never_touches_planner(self) -> None:
        objectives = MagicMock()
        service, _repo, graph = _run_service(
            _pedido(agent_hint="secretary",
                    status=TaskStatus.QUEUED.value),
            _completed_out(selected_agent="secretary"))
        service.set_objective_service(objectives)
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock()) as planned:
            await service.run_task("task-pedido-1", force=True)
        planned.assert_not_called()
        objectives.env_for.assert_not_called()
        self.assertEqual(len(graph.seen), 1)

    async def test_pedido_runs_planner_then_executes(self) -> None:
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        service, repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value), _completed_out())
        service.set_objective_service(objectives)

        async def fake_plan(env: Any, ticket: dict[str, Any],
                            **kwargs: Any) -> dict[str, Any]:
            del env, kwargs
            await repo.patch_task(ticket["task_id"], {
                "agent_hint": "dev", "coding_tool": "tier_c",
                "params": {"target_agent": "dev",
                           "decided_by": "planner"}})
            return {"ok": True, "action": "decided",
                    "decision": {"target_agent": "dev"},
                    "spend": {"escalate": False}}

        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(side_effect=fake_plan)):
            await service.run_task("task-pedido-1", force=True)
        objectives.env_for.assert_called_once()
        self.assertEqual(len(graph.seen), 1)
        self.assertEqual(graph.seen[0].get("coding_tool"), "tier_c")

    async def test_planner_convert_stops_run(self) -> None:
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        objectives.plan_objective = AsyncMock(return_value={"ok": True})
        service, repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value), _completed_out())
        service.set_objective_service(objectives)
        service.comment_on_ticket = AsyncMock()  # type: ignore[method-assign]
        planned = {"ok": True, "action": "convert", "goal": "big work",
                   "acceptance": ["done"], "state_summary": "huge",
                   "costs": [{"label": "objective: team time",
                              "amount": "100"}]}
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(return_value=planned)), \
             patch.object(engine_mod, "convert_ticket",
                          new=AsyncMock(return_value={
                              "objective_id": "OBJ-9"})) as converted:
            out = await service.run_task("task-pedido-1", force=True)
        converted.assert_awaited_once()
        # convert_ticket plans the first horizon internally: no outer plan.
        objectives.plan_objective.assert_not_called()
        self.assertEqual(graph.seen, [])
        self.assertIsNotNone(out)
        service.comment_on_ticket.assert_awaited_once()
        note = service.comment_on_ticket.await_args.args[1]
        self.assertIn("OBJ-9", note)
        self.assertIn("Declared spend", note)

    async def test_pre_stage_crash_fails_terminally(self) -> None:
        from task_service import TaskService  # noqa: E402
        objectives = MagicMock()
        service, repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value), _completed_out())
        service.set_objective_service(objectives)
        service.comment_on_ticket = AsyncMock()  # type: ignore[method-assign]
        with patch.object(TaskService, "_plan_pedido_pre_stage",
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            out = await service.run_task("task-pedido-1", force=True)
        self.assertEqual(graph.seen, [])
        assert out is not None
        self.assertEqual(out["status"], TaskStatus.FAILED.value)
        self.assertIn("boom", out["error"])
        service.comment_on_ticket.assert_awaited_once()

    async def test_spend_hold_stops_run(self) -> None:
        # Production shape: the dispatcher already claimed the ticket
        # (in_progress) before run_task; the hold must revert the claim.
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        service, repo, graph = _run_service(
            _pedido(status=TaskStatus.IN_PROGRESS.value), _completed_out())
        service.set_objective_service(objectives)
        service.create_owner_question = AsyncMock(  # type: ignore[method-assign]
            return_value={"task_id": "q-spend"})
        service.patch_task = repo.patch_task  # type: ignore[method-assign]
        planned = {"ok": True, "action": "decided",
                   "decision": {"target_agent": "dev",
                                "costs": [{"label": "x", "amount": "5"}]},
                   "spend": {"escalate": True, "category": "gasto_nuevo",
                             "reason": "declared", "human_action": "Autorizar",
                             "amount": "5", "currency": "USD", "unknown": []}}
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(return_value=planned)):
            await service.run_task("task-pedido-1", force=True)
        self.assertEqual(graph.seen, [])
        self.assertEqual(repo.task["status"], TaskStatus.WAITING.value)
        service.create_owner_question.assert_awaited_once()

    async def test_decided_abs_stops_queued_for_readmission(self) -> None:
        # Production shape: claimed (in_progress) before run_task.
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        service, repo, graph = _run_service(
            _pedido(status=TaskStatus.IN_PROGRESS.value), _completed_out())
        service.set_objective_service(objectives)

        async def fake_plan(env: Any, ticket: dict[str, Any],
                            **kwargs: Any) -> dict[str, Any]:
            del env, kwargs
            await repo.patch_task(ticket["task_id"], {
                "agent_hint": "dev", "coding_tool": "tier_a",
                "params": {"target_agent": "dev",
                           "tier_reason": "discovery",
                           "decided_by": "planner"}})
            return {"ok": True, "action": "decided",
                    "decision": {"target_agent": "dev",
                                 "coding_tool": "tier_a",
                                 "tier_reason": "discovery"},
                    "spend": {"escalate": False}}

        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(side_effect=fake_plan)):
            out = await service.run_task("task-pedido-1", force=True)
        # Stops queued (decision kept): the next dispatch re-admits with
        # the exact tier — full concurrency, quota and reserve semantics.
        self.assertEqual(graph.seen, [])
        assert out is not None
        self.assertEqual(out["status"], TaskStatus.QUEUED.value)
        self.assertEqual(repo.task["agent_hint"], "dev")
        self.assertEqual(repo.task["coding_tool"], "tier_a")
        self.assertEqual(repo.task["params"]["decided_by"], "planner")

    async def test_planner_error_backs_off(self) -> None:
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        service, repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value), _completed_out())
        service.set_objective_service(objectives)
        service.comment_on_ticket = AsyncMock()  # type: ignore[method-assign]
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(return_value={
                              "ok": False, "reason": "planner_error",
                              "detail": "bad json"})):
            await service.run_task("task-pedido-1", force=True)
        self.assertEqual(graph.seen, [])
        self.assertEqual(repo.task["status"], TaskStatus.WAITING.value)
        self.assertIn("planner_error", repo.task["waiting_reason"])
        self.assertIsNotNone(repo.task["resume_at"])

    async def test_planner_error_third_strike_fails(self) -> None:
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        service, repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value,
                    params={"planner_attempts": 3}),
            _completed_out())
        service.set_objective_service(objectives)
        service.comment_on_ticket = AsyncMock()  # type: ignore[method-assign]
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(return_value={
                              "ok": False, "reason": "planner_error",
                              "detail": "bad json"})):
            # No force: force resets attempts as fresh operator intent.
            out = await service.run_task("task-pedido-1")
        self.assertEqual(graph.seen, [])
        assert out is not None
        self.assertEqual(out["status"], TaskStatus.FAILED.value)
        self.assertIn("planner failed 3x", out["error"])

    async def test_no_objective_service_fails_fast(self) -> None:
        service, _repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value), _completed_out())
        service.comment_on_ticket = AsyncMock()  # type: ignore[method-assign]
        out = await service.run_task("task-pedido-1", force=True)
        self.assertEqual(graph.seen, [])
        assert out is not None
        self.assertEqual(out["status"], TaskStatus.FAILED.value)
        self.assertIn("no objective service", out["error"])

    async def test_ceo_agent_does_not_shadow_target(self) -> None:
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        service, _repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value,
                    params={"target_agent": "dev"}),
            _completed_out())
        service.set_objective_service(objectives)
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock()) as planned:
            await service.run_task("task-pedido-1", force=True)
        planned.assert_not_called()
        self.assertEqual(graph.hints, [None])

    async def test_unknown_workflow_without_agent_fails_loudly(self) -> None:
        objectives = MagicMock()
        objectives.env_for.return_value = MagicMock()
        service, _repo, graph = _run_service(
            _pedido(status=TaskStatus.QUEUED.value,
                    params={"workflow": "no.such.template"}),
            _completed_out())
        service.set_objective_service(objectives)
        service.comment_on_ticket = AsyncMock()  # type: ignore[method-assign]
        workflow = MagicMock()
        workflow.run_for_task = AsyncMock(return_value=None)
        service._workflow_service = workflow
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock()) as planned:
            out = await service.run_task("task-pedido-1", force=True)
        planned.assert_not_called()
        self.assertEqual(graph.seen, [])
        assert out is not None
        self.assertEqual(out["status"], TaskStatus.FAILED.value)
        self.assertIn("no.such.template", out["error"])
        service.comment_on_ticket.assert_awaited_once()


class LooseReturnTests(unittest.IsolatedAsyncioTestCase):
    def _stub(self, ticket: dict[str, Any]):
        from task_service import TaskService  # noqa: E402
        repo = _FakeRepo(ticket)
        stub = SimpleNamespace(
            _repo=repo,
            _objective_service=MagicMock(),
            comment_on_ticket=AsyncMock(),
            _wake_dispatcher=MagicMock(),
            create_owner_question=AsyncMock(return_value={"task_id": "q-1"}),
            patch_task=repo.patch_task,
            _task_allows_autorun=lambda task: True,
        )
        stub._trigger_loose_return = (  # type: ignore[attr-defined]
            lambda *args, **kwargs:
            TaskService._trigger_loose_return(stub, *args, **kwargs))
        stub._hold_decided_spend = (  # type: ignore[attr-defined]
            lambda *args, **kwargs:
            TaskService._hold_decided_spend(stub, *args, **kwargs))
        stub._hold_loose_spend = (  # type: ignore[attr-defined]
            lambda *args, **kwargs:
            TaskService._hold_loose_spend(stub, *args, **kwargs))
        return TaskService, stub

    async def test_first_no_aplica_returns_to_planner(self) -> None:
        service_cls, stub = self._stub(
            _pedido(status=TaskStatus.FAILED.value))
        params = {"agent_result": {"resultado": "no_aplica",
                                   "motivo": "needs design",
                                   "necesidad": "a design doc"}}

        async def fake_plan(env: Any, ticket: dict[str, Any],
                            **kwargs: Any) -> dict[str, Any]:
            del env, kwargs
            await stub._repo.patch_task(ticket["task_id"], {
                "agent_hint": "secretary",
                "params": {"target_agent": "secretary",
                           "decided_by": "planner"}})
            return {"ok": True, "action": "decided",
                    "decision": {"target_agent": "secretary"},
                    "spend": {"escalate": False}}

        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(side_effect=fake_plan)) as plan:
            await service_cls._trigger_objective_return(
                stub, _pedido(status=TaskStatus.FAILED.value), params)
        plan.assert_awaited_once()
        _args, kwargs = plan.await_args
        self.assertIn("return_from", kwargs)
        self.assertEqual(kwargs["return_from"]["resultado"], "no_aplica")
        row = await stub._repo.get_task("task-pedido-1")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.QUEUED.value)
        self.assertEqual(row["params"].get("returns_from"), "task-pedido-1")
        self.assertNotIn("agent_result", row["params"])
        self.assertEqual(row["params"].get("target_agent"), "secretary")
        self.assertEqual(row["params"].get("decided_by"), "planner")
        self.assertEqual(row.get("agent_hint"), "secretary")
        stub.comment_on_ticket.assert_awaited()

    async def test_first_bloqueado_returns_to_planner(self) -> None:
        service_cls, stub = self._stub(
            _pedido(status=TaskStatus.FAILED.value))
        params = {"agent_result": {"resultado": "bloqueado",
                                   "motivo": "waiting on api"}}
        planned = {"ok": True, "action": "decided",
                   "decision": {"target_agent": "dev",
                                "coding_tool": "tier_c"},
                   "spend": {"escalate": False}}
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(return_value=planned)) as plan:
            await service_cls._trigger_objective_return(
                stub, _pedido(status=TaskStatus.FAILED.value), params)
        plan.assert_awaited_once()
        row = await stub._repo.get_task("task-pedido-1")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.QUEUED.value)

    async def test_second_verdict_stays_failed(self) -> None:
        ticket = _pedido(status=TaskStatus.FAILED.value,
                         params={"returns_from": "task-pedido-1"})
        service_cls, stub = self._stub(ticket)
        params = {"returns_from": "task-pedido-1",
                  "agent_result": {"resultado": "bloqueado",
                                   "motivo": "still stuck"}}
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock()) as plan:
            await service_cls._trigger_objective_return(
                stub, ticket, params)
        plan.assert_not_called()
        row = await stub._repo.get_task("task-pedido-1")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.FAILED.value)
        stub.comment_on_ticket.assert_awaited()

    async def test_return_replan_spend_is_held(self) -> None:
        service_cls, stub = self._stub(
            _pedido(status=TaskStatus.FAILED.value))
        params = {"agent_result": {"resultado": "bloqueado",
                                   "motivo": "needs paid api"}}
        planned = {"ok": True, "action": "decided",
                   "decision": {"target_agent": "dev",
                                "coding_tool": "tier_c",
                                "costs": [{"label": "objective: api",
                                           "amount": "50"}]},
                   "spend": {"escalate": True, "category": "gasto_nuevo",
                             "reason": "declared", "human_action": "OK?",
                             "amount": "50", "currency": "USD",
                             "unknown": []}}
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock(return_value=planned)):
            await service_cls._trigger_objective_return(
                stub, _pedido(status=TaskStatus.FAILED.value), params)
        row = await stub._repo.get_task("task-pedido-1")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.WAITING.value)
        stub.create_owner_question.assert_awaited_once()

    async def test_hecho_is_noop(self) -> None:
        service_cls, stub = self._stub(_pedido())
        with patch.object(engine_mod, "plan_pedido",
                          new=AsyncMock()) as plan:
            await service_cls._trigger_objective_return(
                stub, _pedido(),
                {"agent_result": {"resultado": "hecho"}})
        plan.assert_not_called()
        stub.comment_on_ticket.assert_not_awaited()


class RouterRetirementTests(unittest.TestCase):
    def test_no_keyword_or_llm_routing_left(self) -> None:
        from router import CEORouter  # noqa: E402
        for name in ("select_agent", "_classify_with_llm",
                     "_classify_by_config", "_looks_like_pm_payload",
                     "_llm_fallback_enabled", "_default_agent", "_llm"):
            self.assertFalse(hasattr(CEORouter, name), name)

    def test_resolve_explicit(self) -> None:
        from router import CEORouter  # noqa: E402
        from state import AgentName  # noqa: E402
        router = CEORouter()
        self.assertEqual(router.resolve_explicit("operations", None),
                         AgentName.OPERATIONS)
        self.assertEqual(router.resolve_explicit("developer", None),
                         AgentName.DEV)
        self.assertEqual(router.resolve_explicit(None, "tier_s"),
                         AgentName.DEV)
        self.assertEqual(router.resolve_explicit("nope", None), None)
        self.assertEqual(router.resolve_explicit(None, None), None)

    def test_agent_hint_vocabulary_is_single(self) -> None:
        from integrations.openwebui_compat import parse_agent_hint  # noqa: E402
        from router import CEORouter  # noqa: E402
        from state import AgentName  # noqa: E402
        self.assertEqual(parse_agent_hint("@operations do it"),
                         AgentName.OPERATIONS)
        self.assertEqual(parse_agent_hint("@dev do it"), AgentName.DEV)
        self.assertIsNone(parse_agent_hint("@nope do it"))
        self.assertIsNone(parse_agent_hint("no prefix here"))
        self.assertIn("operations", CEORouter.AGENT_ALIASES)

    def test_loose_tier_module_is_gone(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            import agents.dev.loose_tier  # noqa: F401


class GraphFailClosedTests(unittest.IsolatedAsyncioTestCase):
    # TASK-2026-00221 retired the finance node: the question-wait merge
    # now travels the generic exact path (params preserved, owner
    # question id added by the engine; see test_finance_agent_221.py).
    def _graph(self):
        from graph import OpenCEOGraph  # noqa: E402
        loader = MagicMock()
        loader.load_project.return_value = {}
        return OpenCEOGraph(MagicMock(), loader, None)

    async def test_execute_undecided_loose_waits(self) -> None:
        graph = self._graph()
        out = await graph.execute(_pedido())
        self.assertEqual(out["status"], TaskStatus.WAITING.value)
        self.assertIn("routing_missing", out["waiting_reason"])

    async def test_execute_unknown_hint_waits(self) -> None:
        graph = self._graph()
        out = await graph.execute(_pedido(), agent_hint="nope")
        self.assertEqual(out["status"], TaskStatus.WAITING.value)
        self.assertIn("routing_missing", out["waiting_reason"])
        self.assertIn("unknown", out["waiting_reason"])

    async def test_decided_finance_without_wiring_waits(self) -> None:
        from graph import OpenCEOGraph  # noqa: E402
        loader = MagicMock()
        loader.load_project.return_value = {}
        graph = OpenCEOGraph(MagicMock(), loader, None)
        out = await graph._generic_node({
            "task_id": "task-pedido-1", "title": "t", "description": "d",
            "selected_agent": "finance",
            "params": {"target_agent": "finance",
                       "decided_by": "planner"}})
        self.assertEqual(out["status"], TaskStatus.WAITING.value)
        self.assertIn("config:", out["waiting_reason"])


class ExplicitDevDefaultTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        from agents.dev.session import DevSessionManager  # noqa: E402
        loader = MagicMock()
        loader.validate_project = MagicMock(return_value=[])
        return DevSessionManager(MagicMock(), loader, repo=None)

    async def test_explicit_dev_without_tier_runs_c(self) -> None:
        from agents.dev.session import CodingStepResult  # noqa: E402
        from state import CodingTool  # noqa: E402
        manager = self._manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        out = await manager.run(_pedido(agent_hint="dev"), {})
        self.assertEqual(out.status, TaskStatus.COMPLETED)
        self.assertEqual(out.coding_tool, CodingTool.TIER_C)
        assert out.params is not None
        self.assertIn("tier_reason", out.params)
        self.assertIn("§25", out.params["tier_reason"])

    async def test_loose_without_decision_waits(self) -> None:
        from agents.dev.routing import ROUTING_MISSING_REASON  # noqa: E402
        manager = self._manager()
        manager._step_code = AsyncMock()
        out = await manager.run(_pedido(), {})
        self.assertEqual(out.status, TaskStatus.WAITING)
        assert out.waiting_reason is not None
        self.assertTrue(
            out.waiting_reason.startswith(ROUTING_MISSING_REASON))
        manager._step_code.assert_not_called()


if __name__ == "__main__":
    unittest.main()
