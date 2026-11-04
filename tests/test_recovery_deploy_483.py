"""Recovery deploy persisted on the objective (TASK-2026-00483).

A recovery ticket that merged a fix parks its objective as
``recovery_deploy``; the timer restarts the idle service in a transient
unit that spares the new core, and the boot that runs the fix resumes the
mission without spending a retry. Dict-backed stores, scripted planner,
patched restart; no models, no network, no real restart.
"""
from __future__ import annotations

import asyncio
import copy
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from objectives import engine as engine_mod
from objectives import recovery_deploy as deploy_mod
from objectives import recovery_policy as policy
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    TicketCloseContext,
)
from objectives.recovery_scheduler import RecoveryScheduler
from objectives.service import ObjectiveService

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
CFG = {"recovery": {"enabled": True, "max_attempts": 2,
                    "backoff_minutes": [10, 60], "max_tickets": 2,
                    "deploy_on_close": True, "deploy_retry_minutes": 10,
                    "deploy_max_wait_minutes": 120}}


def _deploy_objective(requested=NOW, due=NOW, deadline=None):
    deadline = deadline or requested + timedelta(hours=2)
    return {
        "objective_id": "OBJ-D", "project": "jardal-erp",
        "status": "waiting", "waiting_reason": "recovery_deploy: rec-1",
        "recovery": {
            "attempts": 0, "exhausted": False, "ticket_round": 1,
            "ticket_id": "rec-1", "due_at": due.isoformat(),
            "deploy": {"ticket_id": "rec-1",
                       "requested_at": requested.isoformat(),
                       "deadline": deadline.isoformat()},
        },
    }


class TestDeployPolicy:
    def test_patch_parks_with_a_fresh_budget_and_keeps_the_round(self) -> None:
        patched = policy.deploy_park_patch(
            {"recovery": {"attempts": 2, "exhausted": True,
                          "ticket_round": 1, "ticket_id": "rec-1"}},
            "rec-1", CFG, now=NOW)
        assert patched["status"] == "waiting"
        assert patched["waiting_reason"] == "recovery_deploy: rec-1"
        recovery = patched["recovery"]
        assert recovery["attempts"] == 0 and recovery["exhausted"] is False
        assert recovery["ticket_round"] == 1
        assert recovery["due_at"] == NOW.isoformat()
        assert recovery["deploy"]["deadline"] == (
            NOW + timedelta(minutes=120)).isoformat()

    def test_decisions_follow_boot_idle_and_deadline(self) -> None:
        later = NOW + timedelta(minutes=1)
        before_request = NOW - timedelta(hours=1)

        def decide(objective, **kwargs):
            return policy.recovery_decision(
                objective, 0, CFG, now=later, **kwargs)["action"]

        assert decide(_deploy_objective(),
                      booted_at=NOW + timedelta(seconds=30)) == "resume"
        assert decide(_deploy_objective(), booted_at=before_request,
                      idle=True) == "deploy"
        assert decide(_deploy_objective(), booted_at=before_request,
                      idle=False) == "defer"
        assert decide(_deploy_objective(due=NOW + timedelta(minutes=10)),
                      booted_at=before_request, idle=True) == "none"
        assert decide(_deploy_objective(deadline=NOW),
                      booted_at=before_request) == "resume"
        broken = _deploy_objective()
        broken["recovery"].pop("deploy")
        assert decide(broken) == "resume"

    def test_the_boot_that_applied_the_fix_resumes_before_the_due(self) -> None:
        objective = _deploy_objective(due=NOW + timedelta(minutes=10))
        assert policy.recovery_decision(
            objective, 0, CFG, now=NOW + timedelta(minutes=1),
            booted_at=NOW + timedelta(seconds=40))["action"] == "resume"

    def test_ticket_round_survives_a_park_and_caps_the_budget(self) -> None:
        parked = policy.recovery_park_patch(
            {"recovery": {"attempts": 2, "ticket_round": 2,
                          "ticket_id": "rec-2"}},
            "capability_gap", "x", CFG, now=NOW)["recovery"]
        assert parked["ticket_round"] == 2 and parked["exhausted"] is True
        spent = {"status": "waiting", "waiting_reason": "capability_gap: x",
                 "recovery": parked}
        assert policy.recovery_decision(spent, 0, CFG, now=NOW) == {
            "action": "none", "reason": "recovery ticket budget spent"}
        one_left = dict(spent, recovery=dict(parked, ticket_round=1))
        assert policy.recovery_decision(
            one_left, 0, CFG, now=NOW)["action"] == "ticket"

    def test_next_delay_counts_deploy_parks(self) -> None:
        objective = _deploy_objective(due=NOW + timedelta(minutes=10))
        assert policy.recovery_seconds_to_next([objective], now=NOW) == 600


def _env(store):
    return SimpleNamespace(
        objectives=store, tasks=InMemoryTaskLister(), notify=AsyncMock(),
        ceo_wake=None, recovery_wake=MagicMock(), objectives_cfg=CFG,
        repo=Path("/tmp"))


def _exhausted(status="waiting"):
    return {"objective_id": "OBJ-D", "project": "jardal-erp",
            "status": status, "waiting_reason": "capability_gap: x",
            "anchor": {"goal": "g", "acceptance": []},
            "recovery": {"attempts": 2, "exhausted": True,
                         "ticket_round": 1, "ticket_id": "rec-1"}}


RECOVERY_TICKET = {"task_id": "rec-1", "project": "jardal-openceo",
                   "objective_id": "OBJ-D",
                   "params": {"recovery_for": "OBJ-D"}}


class TestRecoveryClose:
    def test_merged_fix_waits_for_the_deploy_instead_of_replanning(self) -> None:
        async def scenario():
            store = InMemoryObjectiveStore()
            await store.create_objective(_exhausted())
            env = _env(store)
            close = TicketCloseContext(
                task_id="rec-1", title="[recovery]", tier="tier_c",
                sha="abc", files=["openceo-core/x.py"],
                handoff_excerpt="fixed the gate")
            with patch.object(engine_mod, "plan_objective",
                              AsyncMock()) as plan:
                result = await engine_mod.on_ticket_closed(
                    env, RECOVERY_TICKET, close)
            saved = await store.get_objective("OBJ-D")
            assert result["action"] == "await-deploy"
            plan.assert_not_awaited()
            assert saved["status"] == "waiting"
            assert saved["waiting_reason"] == "recovery_deploy: rec-1"
            assert saved["recovery"]["deploy"]["ticket_id"] == "rec-1"
            assert saved["recovery"]["ticket_round"] == 1
            assert saved["tickets_files"] == {"rec-1": []}
            assert saved["tickets_log"][-1]["handoff_excerpt"] == \
                "fixed the gate"
            env.recovery_wake.assert_called()

        asyncio.run(scenario())

    def test_diagnosis_only_close_resumes_now(self) -> None:
        async def scenario():
            store = InMemoryObjectiveStore()
            await store.create_objective(_exhausted())
            close = TicketCloseContext(task_id="rec-1", title="[recovery]",
                                       tier="tier_c", sha="", files=[])
            with patch.object(engine_mod, "plan_objective",
                              AsyncMock(return_value={"ok": True})) as plan:
                await engine_mod.on_ticket_closed(
                    _env(store), RECOVERY_TICKET, close)
            saved = await store.get_objective("OBJ-D")
            assert saved["status"] == "active"
            plan.assert_awaited_once()

        asyncio.run(scenario())

    def test_closed_objective_is_never_resurrected(self) -> None:
        async def scenario():
            store = InMemoryObjectiveStore()
            await store.create_objective(_exhausted(status="closed"))
            with patch.object(engine_mod, "plan_objective",
                              AsyncMock()) as plan:
                result = await engine_mod.on_ticket_closed(
                    _env(store), RECOVERY_TICKET)
            saved = await store.get_objective("OBJ-D")
            assert result["action"] == "objective-not-active"
            assert saved["status"] == "closed"
            plan.assert_not_awaited()

        asyncio.run(scenario())


class FakeRepo:
    def __init__(self, docs, busy=0):
        self.docs = {doc["objective_id"]: copy.deepcopy(doc) for doc in docs}
        self.tasks = SimpleNamespace(
            count_documents=AsyncMock(return_value=busy),
            find_one=AsyncMock(return_value=None))
        self.claims: list[bool] = []
        self.get_task = AsyncMock(return_value=None)

    async def get_objective(self, objective_id):
        doc = self.docs.get(objective_id)
        return copy.deepcopy(doc) if doc else None

    async def patch_objective(self, objective_id, updates):
        self.docs[objective_id].update(copy.deepcopy(updates))
        return copy.deepcopy(self.docs[objective_id])

    async def list_boot_resumable_objectives(self, limit=100):
        return [copy.deepcopy(doc) for doc in self.docs.values()][:limit]

    async def list_tasks_by_objective(self, objective_id):
        return []

    async def claim_objective_recovery(self, objective_id, due_at, *,
                                       count_attempt=True):
        self.claims.append(count_attempt)
        doc = self.docs[objective_id]
        recovery = doc["recovery"]
        if doc["status"] != "waiting" or recovery.get("due_at") != due_at:
            return None
        if count_attempt:
            recovery["attempts"] = int(recovery.get("attempts") or 0) + 1
        recovery.pop("deploy", None)
        recovery["due_at"] = None
        doc["status"] = "active"
        doc["waiting_reason"] = None
        return copy.deepcopy(doc)


def _service(repo, booted_at):
    loader = MagicMock()
    loader.has_project = MagicMock(return_value=True)
    loader.load_project = MagicMock(return_value={"objectives": CFG})
    service = ObjectiveService(repo=repo, task_service=MagicMock(),
                               config_loader=loader, settings=MagicMock())
    service._booted_at = booted_at
    service.env_for = MagicMock(return_value=SimpleNamespace(
        objectives=MagicMock(), tasks=MagicMock(), objectives_cfg=CFG))
    return service


def _due_now_park():
    now = datetime.now(timezone.utc)
    return now, _deploy_objective(requested=now - timedelta(minutes=1),
                                  due=now - timedelta(seconds=1))


class TestDeployTick:
    def _tick(self, repo, booted_at, *, ready=True, in_flight=False):
        service = _service(repo, booted_at)
        with patch("objectives.service.deploy_mod.spawn_service_restart",
                   return_value=True) as spawn, \
             patch("objectives.service.deploy_mod.live_checkout_ready",
                   return_value=ready), \
             patch.object(engine_mod, "planning_in_flight",
                          return_value=in_flight):
            result = asyncio.run(service.recovery_tick())
        return result, spawn

    def test_idle_service_restarts_and_rechecks_later(self) -> None:
        now, park = _due_now_park()
        repo = FakeRepo([park])
        result, spawn = self._tick(repo, now - timedelta(hours=1))
        spawn.assert_called_once()
        assert result["deployed"] == ["OBJ-D"]
        due = policy.parse_due(repo.docs["OBJ-D"]["recovery"]["due_at"])
        assert now + timedelta(minutes=9) < due <= now + timedelta(minutes=11)
        assert repo.docs["OBJ-D"]["status"] == "waiting"
        assert 500 <= result["next_seconds"] <= 600

    def test_one_restart_per_tick_and_nothing_starts_after_it(self) -> None:
        now, first = _due_now_park()
        second = copy.deepcopy(first)
        second["objective_id"] = "OBJ-E"
        repo = FakeRepo([first, second])
        result, spawn = self._tick(repo, now - timedelta(hours=1))
        spawn.assert_called_once()
        assert result["deployed"] == ["OBJ-D"]
        assert repo.docs["OBJ-E"]["recovery"]["due_at"] == \
            second["recovery"]["due_at"]

    @pytest.mark.parametrize("busy,ready,in_flight", [
        (1, True, False), (0, False, False), (0, True, True)])
    def test_busy_dirty_or_planning_defers_without_restarting(
            self, busy, ready, in_flight) -> None:
        now, park = _due_now_park()
        repo = FakeRepo([park], busy=busy)
        result, spawn = self._tick(repo, now - timedelta(hours=1),
                                   ready=ready, in_flight=in_flight)
        spawn.assert_not_called()
        assert result["deployed"] == []
        due = policy.parse_due(repo.docs["OBJ-D"]["recovery"]["due_at"])
        assert due > now + timedelta(minutes=9)

    def test_boot_after_the_request_resumes_without_spending_an_attempt(
            self) -> None:
        now = datetime.now(timezone.utc)
        repo = FakeRepo([_deploy_objective(
            requested=now - timedelta(minutes=5),
            due=now + timedelta(minutes=5))])
        with patch.object(engine_mod, "plan_objective",
                          AsyncMock(return_value={"ok": True})) as plan:
            result, spawn = self._tick(repo, now - timedelta(minutes=1))
        spawn.assert_not_called()
        plan.assert_awaited_once()
        assert result["retried"] == ["OBJ-D"]
        assert repo.claims == [False]
        doc = repo.docs["OBJ-D"]
        assert doc["status"] == "active"
        assert doc["recovery"]["attempts"] == 0
        assert "deploy" not in doc["recovery"]

    def test_round_two_names_why_round_one_failed(self) -> None:
        exhausted = _exhausted()
        repo = FakeRepo([exhausted])
        repo.get_task = AsyncMock(return_value={
            "task_id": "rec-1", "status": "failed",
            "error": "gate ruff failed"})
        service = _service(repo, NOW)
        with patch("objectives.service.create_internal_ticket",
                   AsyncMock(return_value={"task_id": "rec-2"})) as create:
            result = asyncio.run(service.open_recovery_ticket("OBJ-D"))
        assert result["ok"] is True
        payload = create.await_args.args[1]
        assert payload["source_id"] == "objective-recovery:OBJ-D:2"
        assert ("RONDA DE RECUPERACIÓN ANTERIOR: rec-1 [failed] "
                "gate ruff failed") in payload["description"]
        assert repo.docs["OBJ-D"]["recovery"]["ticket_round"] == 2


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class TestRestartHost:
    def _script(self, root: Path) -> Path:
        script = root / "scripts" / "openceo-core-host.sh"
        script.parent.mkdir(parents=True)
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        return script

    def test_restart_runs_in_a_unit_that_spares_the_new_core(
            self, tmp_path: Path) -> None:
        script = self._script(tmp_path)
        with patch("objectives.recovery_deploy.shutil.which",
                   return_value="/usr/bin/systemd-run"), \
             patch("objectives.recovery_deploy.subprocess.run") as run:
            assert deploy_mod.spawn_service_restart(tmp_path) is True
        cmd = run.call_args.args[0]
        assert cmd[:3] == ["/usr/bin/systemd-run", "--user", "--collect"]
        assert cmd[cmd.index("-p") + 1] == "KillMode=process"
        assert "--on-active=2s" in cmd
        assert cmd[-2:] == [str(script), "restart"]

    def test_without_systemd_run_nothing_restarts(self, tmp_path: Path) -> None:
        self._script(tmp_path)
        with patch("objectives.recovery_deploy.shutil.which",
                   return_value=None), \
             patch("objectives.recovery_deploy.subprocess.run") as run:
            assert deploy_mod.spawn_service_restart(tmp_path) is False
        run.assert_not_called()

    def test_checkout_is_ready_only_clean_on_the_base(
            self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "user.name", "t")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-q", "-m", "init")
        assert deploy_mod.live_checkout_ready(repo, "main") is True
        (repo / "a.txt").write_text("wip\n", encoding="utf-8")
        assert deploy_mod.live_checkout_ready(repo, "main") is False
        _git(repo, "checkout", "--", "a.txt")
        _git(repo, "checkout", "-q", "-b", "tier/tier_c-1-abc")
        assert deploy_mod.live_checkout_ready(repo, "main") is False


def test_a_wake_during_the_tick_is_not_lost() -> None:
    async def scenario():
        calls: list[int] = []
        holder: dict[str, RecoveryScheduler] = {}

        async def tick():
            calls.append(1)
            if len(calls) == 1:
                holder["scheduler"].wake()
            await asyncio.sleep(0)
            return {"next_seconds": 3600}

        service = SimpleNamespace(
            seed_recovery_parks=AsyncMock(return_value={"seeded": 0}),
            recovery_tick=tick)
        holder["scheduler"] = RecoveryScheduler(service)
        await holder["scheduler"].start()
        for _ in range(50):
            await asyncio.sleep(0)
            if len(calls) >= 2:
                break
        await holder["scheduler"].stop()
        assert len(calls) >= 2

    asyncio.run(scenario())


def test_a_recovery_ticket_that_did_not_land_wakes_the_timer() -> None:
    from task_service import TaskService

    wake = MagicMock()
    stub = SimpleNamespace(
        _objective_service=SimpleNamespace(wake_recovery=wake))
    TaskService._wake_objective_recovery(stub)
    wake.assert_called_once()
