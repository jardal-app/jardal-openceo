"""Per-ticket review switch (epic TASK-2026-00248, child 00249).

A/B/C review on by default; an explicit ``review=off`` keeps the tier,
builder and gates and only skips reviewer invocations. S always reviews
and rejects off. Covers the single resolver, the admission stamp, the
run/dispatcher holds, creation inputs (API/MCP/batch/planner/pedido)
and the planner contract. No network, no CLI, no LLM.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from agents.dev.review_policy import (  # noqa: E402
    REVIEW_CONFLICT_REASON,
    REVIEW_EFFECTIVE_KEY,
    REVIEW_INVALID_REASON,
    read_effective_review,
    resolve_review,
    review_wait_reason,
    stamp_effective_review,
)
from agents.dev.session import CodingStepResult, DevSessionManager  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
)
from objectives.service import create_internal_ticket  # noqa: E402
from state import CodingTool, ReviewMode, TaskCreateRequest, TaskPatchRequest, TaskStatus  # noqa: E402
from state import make_task_document  # noqa: E402


def _task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {"task_id": "task-1", "title": "t",
                            "description": "d"}
    task.update(overrides)
    return task


def _manager(**overrides: Any) -> DevSessionManager:
    settings = MagicMock()
    loader = MagicMock()
    loader.validate_project = MagicMock(
        return_value=overrides.get("config_errors") or [])
    return DevSessionManager(settings, loader,
                             repo=overrides.get("repo"))


def _ok_code() -> CodingStepResult:
    return CodingStepResult(step={"step": "code", "status": "ok"},
                            prompt="p", summary="s")


class ResolveReviewTests(unittest.TestCase):
    def test_absent_means_on_for_every_tier(self) -> None:
        for tier in ("tier_s", "tier_a", "tier_b", "tier_c", None):
            task = _task()
            if tier is not None:
                task["coding_tool"] = tier
            decision = resolve_review(task)
            self.assertEqual(decision.mode, "on", tier)
            self.assertEqual(decision.origin, "default", tier)

    def test_explicit_on_and_off(self) -> None:
        for tier in ("tier_a", "tier_b", "tier_c"):
            off = resolve_review(_task(coding_tool=tier, review="off"))
            self.assertEqual((off.mode, off.origin), ("off", "explicit"))
            on = resolve_review(_task(coding_tool=tier, review="on"))
            self.assertEqual((on.mode, on.origin), ("on", "explicit"))

    def test_accepts_enum_case_and_whitespace(self) -> None:
        decision = resolve_review(_task(review=ReviewMode.OFF))
        self.assertEqual((decision.mode, decision.origin),
                         ("off", "explicit"))
        decision = resolve_review(_task(review="  Off "))
        self.assertEqual((decision.mode, decision.origin),
                         ("off", "explicit"))

    def test_only_top_level_reads(self) -> None:
        decision = resolve_review(_task(params={"review": "off"}))
        self.assertEqual((decision.mode, decision.origin),
                         ("on", "default"))
        decision = resolve_review(
            _task(review="off", params={"review": "on"}))
        self.assertEqual((decision.mode, decision.origin),
                         ("off", "explicit"))

    def test_invalid_is_explicit(self) -> None:
        for value in ("maybe", "yes", "0", 123, True):
            decision = resolve_review(_task(review=value))
            self.assertIsNone(decision.mode, value)
            self.assertEqual(decision.origin, "none", value)
            self.assertIn("invalid review", decision.detail, value)


class ReviewWaitTests(unittest.TestCase):
    def test_simple_tiers_run_on_and_off(self) -> None:
        for tier in ("tier_a", "tier_b", "tier_c"):
            for mode in ("on", "off"):
                decision = resolve_review(_task(review=mode))
                self.assertIsNone(
                    review_wait_reason(decision, tier), (tier, mode))

    def test_s_runs_on_and_conflicts_off(self) -> None:
        self.assertIsNone(review_wait_reason(
            resolve_review(_task(review="on")), "tier_s"))
        self.assertIsNone(review_wait_reason(
            resolve_review(_task()), "tier_s"))
        hold = review_wait_reason(
            resolve_review(_task(review="off")), "tier_s")
        assert hold is not None
        self.assertTrue(hold.startswith(REVIEW_CONFLICT_REASON), hold)
        self.assertIn("tier_s", hold)

    def test_invalid_waits_on_any_tier(self) -> None:
        for tier in ("tier_s", "tier_a", "tier_b", "tier_c"):
            hold = review_wait_reason(
                resolve_review(_task(review="maybe")), tier)
            assert hold is not None
            self.assertTrue(hold.startswith(REVIEW_INVALID_REASON), hold)
            self.assertIn("maybe", hold)


class StampTests(unittest.TestCase):
    def test_stamp_freezes_mode_and_origin(self) -> None:
        stamped = stamp_effective_review(
            {"tier_reason": "x"}, resolve_review(_task(review="off")))
        self.assertEqual(stamped[REVIEW_EFFECTIVE_KEY],
                         {"mode": "off", "origin": "explicit"})
        self.assertEqual(stamped["tier_reason"], "x")

    def test_stamp_is_pure(self) -> None:
        params = {"a": 1}
        stamp_effective_review(params, resolve_review(_task()))
        self.assertEqual(params, {"a": 1})

    def test_read_rejects_malformed_stamps(self) -> None:
        for params in (None, {}, {"review_effective": None},
                       {"review_effective": {"mode": "maybe"}},
                       {"review_effective": {"mode": "on"}},
                       {"review_effective": {"mode": "on",
                                             "origin": "guess"}},
                       {"review_effective": "on"}):
            self.assertIsNone(read_effective_review(params), params)
        self.assertEqual(
            read_effective_review({"review_effective": {"mode": "on",
                                                       "origin": "default"}}),
            {"mode": "on", "origin": "default"})

    def test_resume_keeps_the_admitted_decision(self) -> None:
        task = _task(coding_tool="tier_b", review="on", params={
            REVIEW_EFFECTIVE_KEY: {"mode": "off", "origin": "explicit"}})
        decision = resolve_review(task)
        self.assertEqual((decision.mode, decision.origin),
                         ("off", "explicit"))
        self.assertIn("resumed", decision.detail)


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_s_off_waits_before_execution(self) -> None:
        manager = _manager()
        manager._run_tier_s = AsyncMock()  # type: ignore[method-assign]
        output = await manager.run(
            _task(coding_tool="tier_s", review="off"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(
            REVIEW_CONFLICT_REASON), output.waiting_reason)
        manager._run_tier_s.assert_not_awaited()
        self.assertNotIn(REVIEW_EFFECTIVE_KEY, output.params or {})

    async def test_invalid_waits_before_the_code_step(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock()
        output = await manager.run(
            _task(coding_tool="tier_b", review="maybe"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(
            REVIEW_INVALID_REASON), output.waiting_reason)
        manager._step_code.assert_not_called()

    async def test_off_runs_simple_tier_and_stamps(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock(return_value=_ok_code())
        output = await manager.run(
            _task(coding_tool="tier_b", review="off"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_B)
        assert output.params is not None
        self.assertEqual(output.params[REVIEW_EFFECTIVE_KEY],
                         {"mode": "off", "origin": "explicit"})

    async def test_absent_stamps_on_by_default(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock(return_value=_ok_code())
        output = await manager.run(_task(coding_tool="tier_c"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        assert output.params is not None
        self.assertEqual(output.params[REVIEW_EFFECTIVE_KEY],
                         {"mode": "on", "origin": "default"})

    async def test_resumed_run_keeps_the_stamp(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock(return_value=_ok_code())
        output = await manager.run(_task(
            coding_tool="tier_b", review="on",
            params={REVIEW_EFFECTIVE_KEY: {"mode": "off",
                                          "origin": "explicit"}}), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        assert output.params is not None
        self.assertEqual(output.params[REVIEW_EFFECTIVE_KEY],
                         {"mode": "off", "origin": "explicit"})

    async def test_explicit_dev_default_keeps_tier_and_stamp(self) -> None:
        manager = _manager()
        manager._step_code = AsyncMock(return_value=_ok_code())
        output = await manager.run(
            _task(agent_hint="dev", review="off"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_C)
        assert output.params is not None
        self.assertEqual(output.params[REVIEW_EFFECTIVE_KEY],
                         {"mode": "off", "origin": "explicit"})


class CreationContractTests(unittest.TestCase):
    def _payload(self, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"source": "api", "title": "t",
                                   "description": "d"}
        payload.update(overrides)
        return payload

    def test_create_request_carries_review(self) -> None:
        req = TaskCreateRequest(**self._payload(review="off"))
        self.assertEqual(req.review, ReviewMode.OFF)
        doc = make_task_document(req)
        self.assertEqual(doc.review, ReviewMode.OFF)

    def test_absent_review_stays_unset(self) -> None:
        req = TaskCreateRequest(**self._payload(coding_tool="tier_c"))
        self.assertIsNone(req.review)
        self.assertIsNone(make_task_document(req).review)

    def test_invalid_review_fails_at_creation(self) -> None:
        with self.assertRaises(ValidationError):
            TaskCreateRequest(**self._payload(review="maybe"))

    def test_patch_accepts_review_and_rejects_garbage(self) -> None:
        self.assertEqual(TaskPatchRequest(review="on").review,
                         ReviewMode.ON)
        with self.assertRaises(ValidationError):
            TaskPatchRequest(review="sometimes")


def _planned_ticket(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "local_ref": "t1",
        "title": "Do the thing",
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": "ship it",
        "why_now": "unblocks the objective",
        "acceptance_criteria": ["it works"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_b",
        "tier_reason": "semantic refactor where errors pass local tests",
        "why_not_c": "a misjudged refactor ships behavior drift",
    }
    base.update(overrides)
    return base


class PlannerContractTests(unittest.TestCase):
    def test_dev_review_parses(self) -> None:
        ticket = planner_mod.PlannedTicket(**_planned_ticket(
            review="off", review_reason="docs-only change"))
        self.assertEqual(ticket.review, "off")
        ticket = planner_mod.PlannedTicket(**_planned_ticket())
        self.assertIsNone(ticket.review)

    def test_non_dev_review_rejected(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            planner_mod.PlannedTicket(**_planned_ticket(
                target_agent="secretary", coding_tool=None,
                tier_reason=None, why_not_c=None, review="on"))
        self.assertIn("review is only valid for DEV tickets",
                      str(raised.exception))

    def test_s_off_rejected(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            planner_mod.PlannedTicket(**_planned_ticket(
                coding_tool="tier_s",
                why_consensus_is_needed="independent families",
                why_single_session_is_insufficient="one session drifts",
                why_not_c=None, review="off"))
        self.assertIn("rejects review=off", str(raised.exception))

    def test_s_on_accepted(self) -> None:
        ticket = planner_mod.PlannedTicket(**_planned_ticket(
            coding_tool="tier_s",
            why_consensus_is_needed="independent families",
            why_single_session_is_insufficient="one session drifts",
            why_not_c=None, review="on"))
        self.assertEqual(ticket.review, "on")

    def test_invalid_review_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            planner_mod.PlannedTicket(**_planned_ticket(review="maybe"))

    def test_prompts_carry_the_switch(self) -> None:
        planner_prompt = engine_mod._planner_prompt(
            {"anchor": {}}, "", [], "", [])
        self.assertIn('"review?"', planner_prompt)
        pedido_prompt = engine_mod._pedido_prompt(_task(task_id="t-1"))
        self.assertIn('"review?"', pedido_prompt)


class _RecordedPlanner:
    """Simulated headless run: writes the recorded JSON (0 tokens)."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.runs = 0

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del timeout_s
        assert role == "objective_planner"
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        target: Path | None = None
        for line in prompt.splitlines():
            if line.startswith("OUTPUT: write "):
                rel = line[len("OUTPUT: write "):].split(
                    " with exactly")[0]
                target = Path(str(workdir)) / rel.strip()
        assert target is not None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _pedido_env(payloads: list[dict[str, Any]], ticket: dict[str, Any],
                ) -> tuple[ObjectiveEnv, dict[str, dict[str, Any]], Any]:
    tmp = tempfile.TemporaryDirectory()
    repo = Path(tmp.name)
    (repo / "policy.md").write_text("POLICY: decide, then stop.\n",
                                    encoding="utf-8")
    patches: dict[str, dict[str, Any]] = {}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(updates)

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        return {"task_id": "task-new"}

    planner = _RecordedPlanner(payloads)
    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister({"task-pedido-1": ticket}),
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
            "policy_path": str(repo / "policy.md"),
            "agents": {"dev": {"enabled": True},
                       "secretary": {"enabled": True}},
        },
    )
    return env, patches, tmp


def _pedido(**overrides: Any) -> dict[str, Any]:
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
    return ticket


def _plan_result(ticket: dict[str, Any]) -> dict[str, Any]:
    return {"objective_status": "active",
            "state_summary": "the pedido is decided",
            "tickets": [ticket], "roadmap_remaining": []}


class PlannerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_creation_forwards_review(self) -> None:
        seen: dict[str, Any] = {}

        class _Service:
            async def create_task(self, req: Any) -> dict[str, Any]:
                seen["review"] = req.review
                seen["coding_tool"] = req.coding_tool
                return {"task_id": "task-1"}

        await create_internal_ticket(_Service(), {
            "title": "t", "description": "d", "source": "spillover",
            "coding_tool": "tier_c", "review": "off",
            "objective_id": "OBJ-1", "origin": "objective_planner"})
        self.assertEqual(seen["review"], ReviewMode.OFF)
        self.assertEqual(seen["coding_tool"], CodingTool.TIER_C)

    async def test_planned_children_carry_review(self) -> None:
        created: list[dict[str, Any]] = []

        async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
            created.append(dict(payload))
            return {"task_id": f"task-new-{len(created)}"}

        env = SimpleNamespace(create_ticket=create_ticket, inbox=None,
                              objectives_cfg={})
        result = planner_mod.PlannerResult(
            objective_status="active", state_summary="first cycle",
            tickets=[planner_mod.PlannedTicket(
                **_planned_ticket(review="off",
                                  review_reason="docs-only change"))])
        await engine_mod._create_planned_tickets(
            env, {"objective_id": "OBJ-1", "project": "proj",
                  "erp_group_ref": ""}, result)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["review"], "off")
        self.assertEqual(created[0]["coding_tool"], "tier_b")

    async def test_pedido_applies_planned_review(self) -> None:
        ticket = _pedido()
        env, patches, tmp = _pedido_env([_plan_result(_planned_ticket(
            local_ref="pedido-1", review="off",
            review_reason="user asked: sin reviewers"))], ticket)
        try:
            out = await engine_mod.plan_pedido(env, ticket)
        finally:
            tmp.cleanup()
        self.assertEqual(out.get("action"), "decided")
        self.assertEqual(patches["task-pedido-1"]["review"], "off")

    async def test_pedido_preserves_explicit_review(self) -> None:
        ticket = _pedido(review="off")
        env, patches, tmp = _pedido_env(
            [_plan_result(_planned_ticket(local_ref="pedido-1"))], ticket)
        try:
            out = await engine_mod.plan_pedido(env, ticket)
        finally:
            tmp.cleanup()
        self.assertEqual(out.get("action"), "decided")
        self.assertNotIn("review", patches["task-pedido-1"])

    async def test_pedido_clears_review_away_from_dev(self) -> None:
        ticket = _pedido(review="off")
        planned = _planned_ticket(local_ref="pedido-1",
                                  target_agent="secretary",
                                  coding_tool=None, tier_reason=None,
                                  why_not_c=None)
        planned.pop("review", None)
        env, patches, tmp = _pedido_env([_plan_result(planned)], ticket)
        try:
            out = await engine_mod.plan_pedido(env, ticket)
        finally:
            tmp.cleanup()
        self.assertEqual(out.get("action"), "decided")
        self.assertIsNone(patches["task-pedido-1"]["review"])

    async def test_pedido_redecision_on_drops_a_stale_review_reason(self) -> None:
        ticket = _pedido(review="off", params={"review_reason": "docs only"})
        env, patches, tmp = _pedido_env([_plan_result(_planned_ticket(
            local_ref="pedido-1", review="on"))], ticket)
        try:
            out = await engine_mod.plan_pedido(env, ticket)
        finally:
            tmp.cleanup()
        self.assertEqual(out.get("action"), "decided")
        self.assertEqual(patches["task-pedido-1"]["review"], "on")
        self.assertNotIn("review_reason", patches["task-pedido-1"]["params"])

    async def test_pedido_redecision_away_from_dev_drops_review_reason(
            self) -> None:
        ticket = _pedido(review="off", params={"review_reason": "docs only"})
        planned = _planned_ticket(local_ref="pedido-1",
                                  target_agent="secretary",
                                  coding_tool=None, tier_reason=None,
                                  why_not_c=None)
        env, patches, tmp = _pedido_env([_plan_result(planned)], ticket)
        try:
            out = await engine_mod.plan_pedido(env, ticket)
        finally:
            tmp.cleanup()
        self.assertEqual(out.get("action"), "decided")
        self.assertNotIn("review_reason", patches["task-pedido-1"]["params"])

    async def test_pedido_user_switch_outranks_the_planner_call(self) -> None:
        ticket = _pedido(review="on")
        env, patches, tmp = _pedido_env([_plan_result(_planned_ticket(
            local_ref="pedido-1", review="off",
            review_reason="looks mechanical"))], ticket)
        try:
            out = await engine_mod.plan_pedido(env, ticket)
        finally:
            tmp.cleanup()
        self.assertEqual(out.get("action"), "decided")
        self.assertNotIn("review", patches["task-pedido-1"])
        self.assertNotIn("review_reason", patches["task-pedido-1"]["params"])

    async def test_pedido_decided_off_carries_its_reason(self) -> None:
        ticket = _pedido()
        env, patches, tmp = _pedido_env([_plan_result(_planned_ticket(
            local_ref="pedido-1", review="off",
            review_reason="configuration only"))], ticket)
        try:
            out = await engine_mod.plan_pedido(env, ticket)
        finally:
            tmp.cleanup()
        self.assertEqual(out.get("action"), "decided")
        self.assertEqual(patches["task-pedido-1"]["review"], "off")
        self.assertEqual(
            patches["task-pedido-1"]["params"]["review_reason"],
            "configuration only")


class _FakeService:
    def __init__(self) -> None:
        self.patches: list[tuple[str, dict[str, Any]]] = []

    def _task_allows_autorun(self, task: dict[str, Any]) -> bool:
        autorun = task.get("autorun")
        return True if autorun is None else bool(autorun)

    async def blockers_open(self, task: dict[str, Any]) -> bool:
        return False

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> None:
        self.patches.append((task_id, dict(updates)))

    async def run_task(self, task_id: str) -> None:  # pragma: no cover
        raise AssertionError("use the injected launcher")


class _FakeRepo:
    def __init__(self) -> None:
        self.claims: list[str] = []

    async def claim_queued_ticket(self, task_id: str) -> dict[str, Any]:
        self.claims.append(task_id)
        return {"task_id": task_id}


class DispatcherReviewTests(unittest.IsolatedAsyncioTestCase):
    def _dispatcher(self, service: _FakeService, repo: _FakeRepo,
                    launched: list[str]) -> Dispatcher:
        async def launcher(task_id: str) -> None:
            launched.append(task_id)

        return Dispatcher(repo=repo, settings=SimpleNamespace(),
                          service=service, rate_monitor=None,
                          launcher=launcher)

    async def test_s_off_parks_before_claim(self) -> None:
        service, repo, launched = _FakeService(), _FakeRepo(), []
        dispatcher = self._dispatcher(service, repo, launched)
        ticket = _task(task_id="t-s", coding_tool="tier_s", review="off",
                       status=TaskStatus.QUEUED.value, autorun=True)
        outcome = await dispatcher._admit_and_launch(
            ticket, [], Counter(), Counter(),
            datetime.now(timezone.utc), {})
        self.assertEqual(outcome, "parked")
        self.assertEqual(repo.claims, [])
        self.assertEqual(launched, [])
        self.assertTrue(service.patches[0][1]["waiting_reason"].startswith(
            REVIEW_CONFLICT_REASON))

    async def test_invalid_parks_before_claim(self) -> None:
        service, repo, launched = _FakeService(), _FakeRepo(), []
        dispatcher = self._dispatcher(service, repo, launched)
        ticket = _task(task_id="t-i", coding_tool="tier_c",
                       review="sometimes",
                       status=TaskStatus.QUEUED.value, autorun=True)
        outcome = await dispatcher._admit_and_launch(
            ticket, [], Counter(), Counter(),
            datetime.now(timezone.utc), {})
        self.assertEqual(outcome, "parked")
        self.assertEqual(repo.claims, [])
        self.assertTrue(service.patches[0][1]["waiting_reason"].startswith(
            REVIEW_INVALID_REASON))

    async def test_off_simple_tier_launches(self) -> None:
        service, repo, launched = _FakeService(), _FakeRepo(), []
        dispatcher = self._dispatcher(service, repo, launched)
        ticket = _task(task_id="t-c", coding_tool="tier_c", review="off",
                       status=TaskStatus.QUEUED.value, autorun=True)
        outcome = await dispatcher._admit_and_launch(
            ticket, [], Counter(), Counter(),
            datetime.now(timezone.utc), {})
        self.assertEqual(outcome, "launched")
        self.assertEqual(repo.claims, ["t-c"])
        await asyncio.sleep(0)
        self.assertEqual(launched, ["t-c"])
