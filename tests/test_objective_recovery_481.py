"""Bounded automatic recovery of parked objectives (TASK-2026-00481).

A park persists its own retry time; one event-driven timer replans when
due and, once the budget is spent, opens a musej recovery ticket in
OpenCEO's project. These tests use dict-backed stores and scripted
planner calls; no models, network or full mission run.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from objectives import engine as engine_mod
from objectives import recovery_policy as policy
from objectives.engine import InMemoryObjectiveStore, InMemoryTaskLister
from objectives.recovery_scheduler import RecoveryScheduler
from objectives.service import ObjectiveService

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
CFG = {"recovery": {"enabled": True, "max_attempts": 2,
                    "backoff_minutes": [10, 60],
                    "ticket_project": "jardal-openceo",
                    "ticket_tier": "tier_c"}}


def _objective(**overrides):
    doc = {
        "objective_id": "OBJ-TEST",
        "project": "jardal-erp",
        "status": "waiting",
        "waiting_reason": "capability_gap: no verified fix",
        "recovery": {"attempts": 0, "due_at": NOW.isoformat(),
                     "exhausted": False},
    }
    doc.update(overrides)
    return doc


class TestRecoveryPolicy:
    def test_park_schedules_first_backoff_and_keeps_the_nearest_due(self) -> None:
        patch_ = policy.recovery_park_patch(
            _objective(recovery={}), "capability_gap", "gap", CFG, NOW)
        assert patch_["recovery"]["attempts"] == 0
        due = datetime.fromisoformat(patch_["recovery"]["due_at"])
        assert due == NOW + timedelta(minutes=10)
        second = policy.recovery_park_patch(
            _objective(recovery=patch_["recovery"]), "capability_gap",
            "gap again", CFG, NOW + timedelta(minutes=1))
        assert datetime.fromisoformat(second["recovery"]["due_at"]) == due

    def test_attempts_spent_marks_exhausted(self) -> None:
        objective = _objective(recovery={"attempts": 2, "due_at": None,
                                         "exhausted": False})
        patch_ = policy.recovery_park_patch(
            objective, "capability_gap", "gap", CFG, NOW)
        assert patch_["recovery"]["exhausted"] is True
        assert patch_["recovery"]["due_at"] is None

    def test_non_recoverable_reason_or_disabled(self) -> None:
        assert policy.recovery_park_patch(
            _objective(), "data_gaps", "x",
            {"recovery": {"enabled": False}}, NOW) is None
        assert policy.recovery_park_patch(
            _objective(), "unknown", "x", CFG, NOW) is None

    def test_decision_respects_children_due_and_exhaustion(self) -> None:
        due = _objective()
        assert policy.recovery_decision(
            due, 0, CFG, now=NOW)["action"] == "retry"
        assert policy.recovery_decision(
            due, 1, CFG, now=NOW)["action"] == "none"
        later = _objective(recovery={
            "attempts": 0, "due_at": (NOW + timedelta(minutes=5)).isoformat()})
        assert policy.recovery_decision(
            later, 0, CFG, now=NOW)["action"] == "none"
        spent = _objective(recovery={"attempts": 2, "due_at": None,
                                     "exhausted": True})
        assert policy.recovery_decision(
            spent, 0, CFG, now=NOW)["action"] == "ticket"
        assert policy.recovery_decision(
            spent, 0, CFG, now=NOW, ticket_open=True)["action"] == "none"

    def test_legacy_park_is_seeded_and_delay_is_clamped(self) -> None:
        legacy = _objective()
        legacy.pop("recovery")
        seeded = policy.recovery_initial_patch(legacy, CFG, NOW)
        assert seeded["recovery"]["attempts"] == 0
        assert policy.recovery_seconds_to_next([legacy], NOW) == 3600
        due = _objective()
        assert policy.recovery_seconds_to_next([due], NOW) == 5


class FakeTasks:
    def __init__(self):
        self.rows = []

    async def find_one(self, query, *args, **kwargs):
        for row in self.rows:
            if (row.get("params") or {}).get("recovery_for") == \
                    (query.get("params.recovery_for")):
                return row
        return None


class FakeRepo:
    def __init__(self, objectives=None, tasks=None):
        self.docs = {doc["objective_id"]: dict(doc)
                     for doc in (objectives or [])}
        self.task_rows = list(tasks or [])
        self.tasks = FakeTasks()
        self.claims = 0

    async def get_objective(self, objective_id):
        doc = self.docs.get(objective_id)
        return dict(doc) if doc else None

    async def patch_objective(self, objective_id, updates):
        doc = self.docs.get(objective_id)
        if doc is None:
            return None
        doc.update(updates)
        return dict(doc)

    async def list_boot_resumable_objectives(self, limit=100):
        return [dict(doc) for doc in self.docs.values()][:limit]

    async def list_tasks_by_objective(self, objective_id):
        return [dict(row) for row in self.task_rows
                if row.get("objective_id") == objective_id]

    async def claim_objective_recovery(self, objective_id, due_at, *,
                                       count_attempt=True):
        doc = self.docs.get(objective_id)
        if doc is None or doc.get("status") != "waiting":
            return None
        recovery = doc.get("recovery") or {}
        if recovery.get("due_at") != due_at:
            return None
        if count_attempt:
            recovery["attempts"] = int(recovery.get("attempts") or 0) + 1
        recovery.pop("deploy", None)
        recovery["due_at"] = None
        doc["status"] = "active"
        doc["waiting_reason"] = None
        self.claims += 1
        return dict(doc)


def _service(repo, tasks=None):
    cfg = MagicMock()
    cfg.has_project = MagicMock(return_value=True)
    cfg.load_project = MagicMock(return_value={"objectives": CFG})
    service = ObjectiveService(
        repo=repo, task_service=MagicMock(), config_loader=cfg,
        settings=MagicMock())
    service._recovery_wake = MagicMock()
    service._env = SimpleNamespace(
        objectives=MagicMock(), tasks=MagicMock(), objectives_cfg=CFG)
    service.env_for = MagicMock(return_value=service._env)
    return service


class TestServiceRecovery:
    def test_tick_retries_due_and_tickets_spent(self) -> None:
        due = _objective()
        spent = _objective(objective_id="OBJ-SPENT", recovery={
            "attempts": 2, "due_at": None, "exhausted": True})
        repo = FakeRepo([due, spent])
        service = _service(repo)
        service.recover_objective = AsyncMock(return_value={"ok": True})
        service.open_recovery_ticket = AsyncMock(return_value={"ok": True})
        result = asyncio.run(service.recovery_tick())
        assert result["retried"] == ["OBJ-TEST"]
        assert result["ticketed"] == ["OBJ-SPENT"]

    def test_tick_reports_the_next_pending_due_only(self) -> None:
        future = (datetime.now(timezone.utc)
                  + timedelta(minutes=10)).isoformat()
        repo = FakeRepo([_objective(recovery={
            "attempts": 0, "due_at": future, "exhausted": False})])
        service = _service(repo)
        result = asyncio.run(service.recovery_tick())
        assert 540 <= result["next_seconds"] <= 600

        busy = FakeRepo([_objective()])
        busy.task_rows.append({"task_id": "child-1",
                               "objective_id": "OBJ-TEST",
                               "status": "in_progress"})
        service = _service(busy)
        result = asyncio.run(service.recovery_tick())
        assert result["next_seconds"] == 3600

    def test_none_decision_paces_a_stale_due(self) -> None:
        repo = FakeRepo([_objective()])
        repo.task_rows.append({"task_id": "child-1",
                               "objective_id": "OBJ-TEST",
                               "status": "in_progress"})
        service = _service(repo)
        before = datetime.now(timezone.utc)
        result = asyncio.run(service.recover_waiting_objective("OBJ-TEST"))
        assert result["action"] == "none"
        assert result["reason"] == "children still active"
        paced = policy.parse_due(
            repo.docs["OBJ-TEST"]["recovery"]["due_at"])
        assert paced is not None
        assert before + timedelta(minutes=10) <= paced <= \
            datetime.now(timezone.utc) + timedelta(minutes=10)

    def test_none_decision_leaves_a_future_due_alone(self) -> None:
        future = (datetime.now(timezone.utc)
                  + timedelta(minutes=30)).isoformat()
        repo = FakeRepo([_objective(recovery={
            "attempts": 0, "due_at": future, "exhausted": False})])
        repo.task_rows.append({"task_id": "child-1",
                               "objective_id": "OBJ-TEST",
                               "status": "in_progress"})
        service = _service(repo)
        result = asyncio.run(service.recover_waiting_objective("OBJ-TEST"))
        assert result["action"] == "none"
        assert repo.docs["OBJ-TEST"]["recovery"]["due_at"] == future

    def test_recover_claims_once_and_plans(self) -> None:
        repo = FakeRepo([_objective()])
        service = _service(repo)
        with patch.object(engine_mod, "plan_objective",
                          AsyncMock(return_value={"ok": True,
                                                  "tickets_created": 1})) as plan:
            result = asyncio.run(service.recover_objective("OBJ-TEST"))
        assert result["ok"] is True
        assert repo.claims == 1
        plan.assert_awaited_once()
        assert repo.docs["OBJ-TEST"]["status"] == "active"

    def test_recover_lost_claim_does_not_plan(self) -> None:
        repo = FakeRepo([_objective()])
        service = _service(repo)
        repo.claim_objective_recovery = AsyncMock(return_value=None)
        with patch.object(engine_mod, "plan_objective", AsyncMock()) as plan:
            result = asyncio.run(service.recover_objective("OBJ-TEST"))
        assert result["reason"] == "claim-lost"
        plan.assert_not_awaited()

    def test_ticket_payload_points_at_the_mission_objective(self) -> None:
        repo = FakeRepo([_objective(recovery={
            "attempts": 2, "due_at": None, "exhausted": True})])
        service = _service(repo)
        created = {"task_id": "rec-1"}
        with patch("objectives.service.create_internal_ticket",
                   AsyncMock(return_value=created)) as create:
            result = asyncio.run(service.open_recovery_ticket("OBJ-TEST"))
        assert result["ok"] is True and result["project"] == "jardal-openceo"
        payload = create.await_args.args[1]
        assert payload["project"] == "jardal-openceo"
        assert "objective_id" not in payload
        assert payload["params"]["recovery_for"] == "OBJ-TEST"
        assert payload["coding_tool"] == "tier_c"
        assert payload["agent_hint"] == "dev"
        assert "OBJ-TEST" in payload["description"]
        assert "MANDATO DE RECUPERACIÓN" in payload["description"]
        patched = repo.docs["OBJ-TEST"]["recovery"]
        assert patched["ticket_id"] == "rec-1"
        assert patched["ticket_round"] == 1

    def test_open_ticket_is_skipped_when_one_is_open(self) -> None:
        repo = FakeRepo([_objective(recovery={
            "attempts": 2, "due_at": None, "exhausted": True})])
        service = _service(repo)
        with patch.object(service, "_recovery_ticket_open",
                          AsyncMock(return_value=True)), \
             patch("objectives.service.create_internal_ticket",
                   AsyncMock()) as create:
            result = asyncio.run(service.open_recovery_ticket("OBJ-TEST"))
        assert result["reason"] == "ticket-open"
        create.assert_not_awaited()


class TestEnginePark:
    def test_park_writes_recovery_and_wakes_the_timer(self) -> None:
        async def scenario():
            store = InMemoryObjectiveStore()
            doc = await store.create_objective(_objective())
            wake = MagicMock()
            env = SimpleNamespace(
                objectives=store, tasks=InMemoryTaskLister(),
                notify=AsyncMock(), ceo_wake=None, recovery_wake=wake,
                objectives_cfg=CFG)
            await engine_mod._objective_park(
                env, doc, "capability_gap", "no verified fix")
            saved = await store.get_objective("OBJ-TEST")
            assert saved["status"] == "waiting"
            assert saved["recovery"]["due_at"] is not None
            assert saved["recovery"]["attempts"] == 0
            wake.assert_called_once()

        asyncio.run(scenario())

    def test_close_resets_the_recovery_budget(self) -> None:
        async def scenario():
            store = InMemoryObjectiveStore()
            doc = await store.create_objective(_objective(
                status="waiting", recovery={"attempts": 2,
                                            "due_at": None,
                                            "exhausted": True}))
            env = SimpleNamespace(
                objectives=store, tasks=InMemoryTaskLister(),
                notify=AsyncMock(), ceo_wake=None, recovery_wake=None,
                objectives_cfg=CFG, repo=Path("/tmp"))
            ticket = {"task_id": "t-1", "objective_id": "OBJ-TEST",
                      "params": {}}
            with patch.object(engine_mod, "plan_objective",
                              AsyncMock(return_value={"ok": True})):
                await engine_mod.on_ticket_closed(env, ticket)
            saved = await store.get_objective("OBJ-TEST")
            assert saved["status"] == "active"
            assert saved["recovery"]["attempts"] == 0
            assert saved["recovery"]["exhausted"] is False

        asyncio.run(scenario())


def test_recovery_ticket_close_resumes_any_park():
    asyncio.run(_recovery_ticket_close_scenario())


async def _recovery_ticket_close_scenario():
    store = InMemoryObjectiveStore()
    await store.create_objective(_objective(
        status="waiting", waiting_reason="blocked: infra",
        recovery={"attempts": 2, "due_at": None, "exhausted": True}))
    env = SimpleNamespace(
        objectives=store, tasks=InMemoryTaskLister(),
        notify=AsyncMock(), ceo_wake=None, recovery_wake=None,
        objectives_cfg=CFG, repo=Path("/tmp"))
    ticket = {"task_id": "rec-1", "project": "jardal-openceo",
              "objective_id": "OBJ-TEST",
              "params": {"recovery_for": "OBJ-TEST"}}
    with patch.object(engine_mod, "plan_objective",
                      AsyncMock(return_value={"ok": True})) as plan:
        result = await engine_mod.on_ticket_closed(env, ticket)
    saved = await store.get_objective("OBJ-TEST")
    assert result["ok"] is True
    assert saved["status"] == "active"
    assert saved["recovery"]["attempts"] == 0
    assert saved["recovery"]["exhausted"] is False
    plan.assert_awaited_once()


def test_cross_project_recovery_close_uses_the_objective_project():
    asyncio.run(_cross_project_close_scenario())


async def _cross_project_close_scenario():
    from task_service import TaskService

    env = SimpleNamespace(objectives=MagicMock(), tasks=MagicMock())
    service = SimpleNamespace(
        get_objective=AsyncMock(return_value={"project": "jardal-erp"}),
        env_for=MagicMock(return_value=env))
    stub = SimpleNamespace(
        _objective_service=service,
        _attribute_objective_child=AsyncMock(side_effect=lambda row: row))
    ticket = {"task_id": "rec-1", "project": "jardal-openceo",
              "params": {"recovery_for": "OBJ-TEST"}}
    with patch("task_service.on_ticket_closed",
               AsyncMock(return_value={"ok": True})) as closed:
        await TaskService._trigger_objective_close(stub, ticket, {})
    service.env_for.assert_called_once_with("jardal-erp")
    passed = closed.await_args.args[1]
    assert passed["objective_id"] == "OBJ-TEST"


class TestRecoveryScheduler:
    def test_scheduler_wakes_on_demand_and_stops(self) -> None:
        async def scenario():
            service = SimpleNamespace(
                seed_recovery_parks=AsyncMock(return_value={"ok": True}),
                recovery_tick=AsyncMock(return_value={"ok": True}),
                recovery_next_delay=AsyncMock(return_value=3600),
            )
            scheduler = RecoveryScheduler(service)
            await scheduler.start()
            await asyncio.sleep(0)
            service.recovery_tick.assert_awaited()
            scheduler.wake()
            await asyncio.sleep(0)
            await scheduler.stop()
            assert scheduler._task is None

        asyncio.run(scenario())
