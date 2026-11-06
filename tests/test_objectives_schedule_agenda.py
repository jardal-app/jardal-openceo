"""Objectives recovery agenda of the global schedule kernel: contract with doubles (ticket #570).

Logic/contract only, with doubles and a virtual clock; no live model calls
(Owner order 2026-10-14). Beyond the ``next_due``/``claim_due``/``run``/
``settle``/``recover_at_boot`` contract, this proves the migration properties
from TASK-2026-00441 §5.5: the indexed wait query instead of the hourly
re-read (D4), the boot-stamped atomic seal with no age threshold (D3), the
boot-time deploy immediacy, and the hot wake driving spent parks — with no
wait cap anywhere.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from objectives import engine as engine_mod  # noqa: E402
from objectives import recovery_policy as policy  # noqa: E402
from objectives.schedule_agenda import ObjectivesRecoveryAgenda  # noqa: E402
from objectives.service import ObjectiveService  # noqa: E402
from schedule_kernel import ScheduleKernel  # noqa: E402

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
CFG = {"recovery": {"enabled": True, "max_attempts": 2,
                    "backoff_minutes": [10, 60],
                    "ticket_project": "jardal-openceo",
                    "ticket_tier": "tier_c", "max_tickets": 2,
                    "deploy_on_close": True, "deploy_retry_minutes": 10,
                    "deploy_max_wait_minutes": 120}}


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _objective(objective_id: str = "OBJ-TEST", **overrides) -> dict:
    doc = {
        "objective_id": objective_id,
        "project": "jardal-erp",
        "status": "waiting",
        "waiting_reason": "capability_gap: no verified fix",
        "recovery": {"attempts": 0, "due_at": _iso(NOW),
                     "exhausted": False},
    }
    doc.update(overrides)
    return doc


def _deploy_objective(objective_id: str = "OBJ-D", requested=NOW,
                      due=NOW, deadline=None) -> dict:
    deadline = deadline or requested + timedelta(hours=2)
    return {
        "objective_id": objective_id, "project": "jardal-erp",
        "status": "waiting", "waiting_reason": "recovery_deploy: rec-1",
        "recovery": {
            "attempts": 0, "exhausted": False, "ticket_round": 1,
            "ticket_id": "rec-1", "due_at": _iso(due),
            "deploy": {"ticket_id": "rec-1",
                       "requested_at": _iso(requested),
                       "deadline": _iso(deadline)},
        },
    }


class _Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or NOW

    def __call__(self) -> datetime:
        return self.now


class _VirtualWaiter:
    """As in the oracle: with the budget spent, wait on the real event."""

    def __init__(self, clock: _Clock, budget: int = 0) -> None:
        self.clock = clock
        self.budget = budget
        self.calls: list[float | None] = []
        self._timeouts = 0

    async def __call__(self, event: asyncio.Event, timeout: float | None) -> bool:
        self.calls.append(timeout)
        if timeout is None or self._timeouts >= self.budget:
            await event.wait()
            return True
        self._timeouts += 1
        self.clock.now = self.clock.now + timedelta(seconds=timeout)
        return False


class _FakeRepo:
    """In-memory repo with the Mongo semantics the Agenda relies on.

    ISO-string due comparison, None-matches-missing, the conditional seal,
    the fenced release, boot-ownership recovery and the deploy catch-up;
    every boundary deep-copies, as the real driver returns snapshots. It
    proves the Agenda contract, not atomicity (covered against real Mongo
    by the oracle's claim tests).
    """

    def __init__(self, now_fn=None) -> None:
        self.docs: dict[str, dict] = {}
        self.task_rows: list[dict] = []
        self.busy = 0
        self.next_due_calls = 0
        self.seal_calls = 0
        self.flips = 0
        self.fail_seal_once: set[str] = set()
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.tasks = SimpleNamespace(
            find_one=self._tasks_find_one,
            count_documents=self._tasks_count,
        )

    async def _tasks_find_one(self, query: dict) -> dict | None:
        for row in self.task_rows:
            if (row.get("params") or {}).get("recovery_for") == \
                    query.get("params.recovery_for"):
                return copy.deepcopy(row)
        return None

    async def _tasks_count(self, query: dict) -> int:
        return self.busy

    async def get_objective(self, objective_id: str) -> dict | None:
        doc = self.docs.get(objective_id)
        return copy.deepcopy(doc) if doc else None

    async def patch_objective(self, objective_id: str, updates: dict) -> dict | None:
        doc = self.docs.get(objective_id)
        if doc is None:
            return None
        doc.update(copy.deepcopy(dict(updates)))
        return copy.deepcopy(doc)

    async def list_tasks_by_objective(self, objective_id: str) -> list[dict]:
        return [copy.deepcopy(row) for row in self.task_rows
                if row.get("objective_id") == objective_id]

    async def get_task(self, task_id: str) -> dict | None:
        return None

    async def claim_objective_recovery(self, objective_id: str, due_at,
                                       *, count_attempt: bool = True) -> dict | None:
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
        self.flips += 1
        return copy.deepcopy(doc)

    @staticmethod
    def _sealed(doc: dict) -> bool:
        return doc.get("claimed_boot_id") is not None

    async def next_objective_recovery_due_at(self) -> datetime | None:
        self.next_due_calls += 1
        now = self._now_fn()
        spent = False
        best: str | None = None
        for doc in self.docs.values():
            if doc.get("status") != "waiting" or self._sealed(doc):
                continue
            recovery = doc.get("recovery") or {}
            due = recovery.get("due_at")
            if recovery.get("exhausted") is True and due is None:
                spent = True
            if due is None:
                continue
            if best is None or due < best:
                best = due
        nearest = policy.parse_due(best) if best is not None else None
        if spent and (nearest is None or nearest > now):
            return now
        return nearest

    async def list_due_objective_recoveries(self, now: datetime, limit: int = 20) -> list[dict]:
        now_iso = now.astimezone(timezone.utc).isoformat()
        out = []
        for doc in self.docs.values():
            if doc.get("status") != "waiting" or self._sealed(doc):
                continue
            recovery = doc.get("recovery") or {}
            due = recovery.get("due_at")
            if due is not None and due <= now_iso:
                out.append(doc)
            elif recovery.get("exhausted") is True:
                out.append(doc)
        out.sort(key=lambda d: ((d.get("recovery") or {}).get("due_at") is not None,
                                (d.get("recovery") or {}).get("due_at") or ""))
        return [copy.deepcopy(d) for d in out[: max(1, int(limit))]]

    async def seal_objective_recovery(self, objective_id: str, due_at, boot_id: str) -> dict | None:
        self.seal_calls += 1
        if objective_id in self.fail_seal_once:
            self.fail_seal_once.discard(objective_id)
            return None
        doc = self.docs.get(objective_id)
        if doc is None or doc.get("status") != "waiting":
            return None
        if doc.get("claimed_boot_id") is not None:
            return None
        if (doc.get("recovery") or {}).get("due_at") != due_at:
            return None
        doc["claimed_boot_id"] = boot_id
        return copy.deepcopy(doc)

    async def release_objective_recovery_claim(self, objective_id: str, boot_id: str) -> dict | None:
        doc = self.docs.get(objective_id)
        if doc is None or doc.get("claimed_boot_id") != boot_id:
            return None
        doc["claimed_boot_id"] = None
        return copy.deepcopy(doc)

    async def release_objective_recovery_claims_for_boot(self, boot_id: str) -> int:
        released = 0
        for doc in self.docs.values():
            if "claimed_boot_id" not in doc:
                continue
            if doc["claimed_boot_id"] is None or doc["claimed_boot_id"] == boot_id:
                continue
            doc["claimed_boot_id"] = None
            released += 1
        return released

    async def mark_objective_deploys_due_for_boot(self, now: datetime) -> int:
        now_iso = now.astimezone(timezone.utc).isoformat()
        marked = 0
        for doc in self.docs.values():
            if doc.get("status") != "waiting":
                continue
            requested = ((doc.get("recovery") or {}).get("deploy") or {}).get("requested_at")
            if requested is None or not requested < now_iso:
                continue
            doc["recovery"]["due_at"] = now_iso
            marked += 1
        return marked


def _service(repo) -> ObjectiveService:
    cfg = MagicMock()
    cfg.has_project = MagicMock(return_value=True)
    cfg.load_project = MagicMock(return_value={"objectives": CFG})
    service = ObjectiveService(repo=repo, task_service=MagicMock(),
                               config_loader=cfg, settings=MagicMock())
    service._booted_at = NOW - timedelta(hours=1)
    return service


class AgendaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = _Clock()
        self.repo = _FakeRepo(now_fn=self.clock)
        self.handled: list[dict] = []

    async def _handler(self, claimed: dict) -> dict:
        self.handled.append(copy.deepcopy(claimed))
        return {"ok": True, "action": "retried"}

    def _agenda(self, handler=None, **kw) -> ObjectivesRecoveryAgenda:
        return ObjectivesRecoveryAgenda(
            repo=self.repo, handler=handler or self._handler,
            now_fn=self.clock, **kw)

    async def _recovered_agenda(self, **kw) -> ObjectivesRecoveryAgenda:
        agenda = self._agenda(**kw)
        await agenda.recover_at_boot("boot-1")
        return agenda

    def _seed(self, doc: dict) -> dict:
        self.repo.docs[doc["objective_id"]] = copy.deepcopy(doc)
        return doc

    async def test_next_due_is_one_query_for_the_nearest_claimable(self) -> None:
        self._seed(_objective("OBJ-A", recovery={
            "attempts": 0, "due_at": _iso(NOW + timedelta(seconds=300)),
            "exhausted": False}))
        self._seed(_objective("OBJ-B", recovery={
            "attempts": 0, "due_at": _iso(NOW + timedelta(seconds=60)),
            "exhausted": False}))
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=60))
        self.assertEqual(self.repo.next_due_calls, 1)

    async def test_next_due_none_when_nothing_is_claimable(self) -> None:
        self._seed(_objective("OBJ-A", status="active"))
        sealed = self._seed(_objective("OBJ-B", recovery={
            "attempts": 0, "due_at": _iso(NOW + timedelta(seconds=60)),
            "exhausted": False}))
        sealed["claimed_boot_id"] = "boot-1"
        self.repo.docs["OBJ-B"] = sealed
        agenda = await self._recovered_agenda()
        self.assertIsNone(await agenda.next_due())

    async def test_next_due_is_now_while_a_spent_park_waits(self) -> None:
        """#586: the wait sees what the claim takes, so a backlog drains."""
        self._seed(_objective("OBJ-S", recovery={
            "attempts": 2, "due_at": None, "exhausted": True,
            "ticket_round": 0}))
        self._seed(_objective("OBJ-F", recovery={
            "attempts": 0, "due_at": _iso(NOW + timedelta(hours=1)),
            "exhausted": False}))
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), NOW)

    async def test_next_due_prefers_an_earlier_past_due_over_spent_now(self) -> None:
        past = NOW - timedelta(minutes=5)
        self._seed(_objective("OBJ-S", recovery={
            "attempts": 2, "due_at": None, "exhausted": True,
            "ticket_round": 0}))
        self._seed(_objective("OBJ-P", recovery={
            "attempts": 0, "due_at": _iso(past), "exhausted": False}))
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), past)

    async def test_next_due_ignores_sealed_spent_parks(self) -> None:
        sealed = self._seed(_objective("OBJ-S", recovery={
            "attempts": 2, "due_at": None, "exhausted": True,
            "ticket_round": 0}))
        sealed["claimed_boot_id"] = "boot-1"
        self.repo.docs["OBJ-S"] = sealed
        agenda = await self._recovered_agenda()
        self.assertIsNone(await agenda.next_due())

    async def test_next_due_waits_on_a_paced_spent_due(self) -> None:
        """#586: a spent park with a future pacing due is not due now."""
        future = NOW + timedelta(minutes=10)
        self._seed(_objective("OBJ-S", recovery={
            "attempts": 2, "due_at": _iso(future), "exhausted": True,
            "ticket_round": 1}))
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), future)

    async def test_next_due_skips_sealed_dues(self) -> None:
        sealed = self._seed(_objective("OBJ-A", recovery={
            "attempts": 0, "due_at": _iso(NOW - timedelta(seconds=10)),
            "exhausted": False}))
        sealed["claimed_boot_id"] = "boot-1"
        self.repo.docs["OBJ-A"] = sealed
        self._seed(_objective("OBJ-B", recovery={
            "attempts": 0, "due_at": _iso(NOW + timedelta(seconds=60)),
            "exhausted": False}))
        agenda = await self._recovered_agenda()
        # The sealed due is in flight, so the wait targets the later one.
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=60))

    async def test_claim_due_seals_up_to_the_limit_stamped_with_the_boot(self) -> None:
        self._seed(_objective("OBJ-A", recovery={
            "attempts": 0, "due_at": _iso(NOW - timedelta(seconds=10)),
            "exhausted": False}))
        self._seed(_objective("OBJ-B", recovery={
            "attempts": 0, "due_at": _iso(NOW - timedelta(seconds=5)),
            "exhausted": False}))
        self._seed(_objective("OBJ-C", recovery={
            "attempts": 0, "due_at": _iso(NOW + timedelta(hours=1)),
            "exhausted": False}))
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 2)
        self.assertEqual([c.item_id for c in claims], ["OBJ-A", "OBJ-B"])
        for claim in claims:
            self.assertEqual(claim.boot_id, "boot-1")
            self.assertEqual(claim.fencing_token, 0)
            stored = self.repo.docs[claim.item_id]
            self.assertEqual(stored["claimed_boot_id"], "boot-1")
            self.assertEqual(stored["status"], "waiting")

    async def test_claim_due_without_boot_claims_nothing(self) -> None:
        self._seed(_objective())
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 5), [])
        self.assertEqual(self.repo.seal_calls, 0)

    async def test_claim_due_skips_lost_races(self) -> None:
        self._seed(_objective("OBJ-A"))
        self._seed(_objective("OBJ-B"))
        self.repo.fail_seal_once.add("OBJ-A")
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 5)
        self.assertEqual([c.item_id for c in claims], ["OBJ-B"])

    async def test_claim_due_lists_spent_parks_without_a_due(self) -> None:
        self._seed(_objective("OBJ-S", recovery={
            "attempts": 2, "due_at": None, "exhausted": True,
            "ticket_round": 0}))
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 5)
        self.assertEqual([c.item_id for c in claims], ["OBJ-S"])
        self.assertEqual(
            self.repo.docs["OBJ-S"]["claimed_boot_id"], "boot-1")

    async def test_run_ok_wraps_the_handler_result(self) -> None:
        agenda = await self._recovered_agenda()
        outcome = await agenda.run(SimpleNamespace(
            item_id="OBJ-TEST", boot_id="boot-1", fencing_token=0,
            payload=_objective()))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result, {"ok": True, "action": "retried"})
        self.assertEqual(len(self.handled), 1)

    async def test_run_propagates_unexpected_errors(self) -> None:
        async def failing(claimed: dict) -> dict:
            raise RuntimeError("store down")

        agenda = await self._recovered_agenda(handler=failing)
        with self.assertRaises(RuntimeError):
            await agenda.run(SimpleNamespace(
                item_id="OBJ-TEST", boot_id="boot-1", fencing_token=0,
                payload=_objective()))

    async def _claim_one(self, agenda, objective_id="OBJ-TEST"):
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        return claims[0]

    async def test_settle_releases_the_seal_of_a_replanned_objective(self) -> None:
        self._seed(_objective())
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        self.repo.docs["OBJ-TEST"]["status"] = "active"
        await agenda.settle(claim, outcome)
        stored = self.repo.docs["OBJ-TEST"]
        self.assertEqual(stored["status"], "active")
        self.assertIsNone(stored["claimed_boot_id"])

    async def test_settle_releases_the_seal_of_a_reparked_objective(self) -> None:
        self._seed(_objective())
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        self.repo.docs["OBJ-TEST"]["recovery"]["due_at"] = \
            _iso(NOW + timedelta(minutes=60))
        await agenda.settle(claim, outcome)
        stored = self.repo.docs["OBJ-TEST"]
        self.assertEqual(stored["status"], "waiting")
        self.assertIsNone(stored["claimed_boot_id"])

    async def test_settle_with_a_stale_seal_is_ignored(self) -> None:
        self._seed(_objective())
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        self.repo.docs["OBJ-TEST"]["claimed_boot_id"] = "boot-2"
        await agenda.settle(claim, outcome)
        self.assertEqual(
            self.repo.docs["OBJ-TEST"]["claimed_boot_id"], "boot-2")

    async def test_recover_releases_a_young_seal_of_another_boot(self) -> None:
        foreign = self._seed(_objective("OBJ-A"))
        foreign["claimed_boot_id"] = "boot-old"
        self.repo.docs["OBJ-A"] = foreign
        agenda = self._agenda()
        released = await agenda.recover_at_boot("boot-1")
        self.assertEqual(released, 1)
        self.assertIsNone(self.repo.docs["OBJ-A"]["claimed_boot_id"])
        # The released due is claimable again under the new boot.
        claims = await agenda.claim_due(NOW, 5)
        self.assertEqual([c.item_id for c in claims], ["OBJ-A"])

    async def test_recover_ignores_own_boot_and_field_less_docs(self) -> None:
        own = self._seed(_objective("OBJ-A"))
        own["claimed_boot_id"] = "boot-1"
        self.repo.docs["OBJ-A"] = own
        self._seed(_objective("OBJ-B", status="active"))
        agenda = self._agenda()
        self.assertEqual(await agenda.recover_at_boot("boot-1"), 0)
        self.assertEqual(self.repo.docs["OBJ-A"]["claimed_boot_id"], "boot-1")
        self.assertNotIn("claimed_boot_id", self.repo.docs["OBJ-B"])

    async def test_recover_marks_preboot_deploys_due(self) -> None:
        self._seed(_deploy_objective(
            "OBJ-D", requested=NOW - timedelta(minutes=5),
            due=NOW + timedelta(minutes=5)))
        self._seed(_deploy_objective(
            "OBJ-E", requested=NOW + timedelta(minutes=5),
            due=NOW + timedelta(minutes=15)))
        agenda = self._agenda()
        self.assertEqual(await agenda.recover_at_boot("boot-1"), 1)
        self.assertEqual(
            self.repo.docs["OBJ-D"]["recovery"]["due_at"], _iso(NOW))
        self.assertEqual(
            self.repo.docs["OBJ-E"]["recovery"]["due_at"],
            _iso(NOW + timedelta(minutes=15)))

    async def test_recover_remembers_the_boot_first(self) -> None:
        self._seed(_objective())
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 5), [])
        await agenda.recover_at_boot("boot-1")
        claims = await agenda.claim_due(NOW, 5)
        self.assertEqual([c.item_id for c in claims], ["OBJ-TEST"])
        self.assertEqual(claims[0].boot_id, "boot-1")


class RecoverWaitingObjectiveTests(unittest.IsolatedAsyncioTestCase):
    """The shared per-objective body: the tick and the agenda run it."""

    def _repo_with(self, doc: dict) -> _FakeRepo:
        repo = _FakeRepo()
        repo.docs[doc["objective_id"]] = copy.deepcopy(doc)
        return repo

    async def test_retry_is_routed_to_recover_objective(self) -> None:
        service = _service(self._repo_with(_objective()))
        service.recover_objective = AsyncMock(return_value={"ok": True})
        service.open_recovery_ticket = AsyncMock()
        result = await service.recover_waiting_objective("OBJ-TEST", now=NOW)
        self.assertEqual(result["action"], "retry")
        self.assertTrue(result["ok"])
        service.recover_objective.assert_awaited_once_with("OBJ-TEST")
        service.open_recovery_ticket.assert_not_awaited()

    async def test_spent_is_routed_to_open_recovery_ticket(self) -> None:
        repo = self._repo_with(_objective(recovery={
            "attempts": 2, "due_at": None, "exhausted": True,
            "ticket_round": 0}))
        service = _service(repo)
        service.recover_objective = AsyncMock()
        service.open_recovery_ticket = AsyncMock(return_value={"ok": True})
        result = await service.recover_waiting_objective("OBJ-TEST", now=NOW)
        self.assertEqual(result["action"], "ticket")
        self.assertTrue(result["ok"])
        service.open_recovery_ticket.assert_awaited_once_with("OBJ-TEST")
        service.recover_objective.assert_not_awaited()

    async def test_active_children_plan_nothing(self) -> None:
        repo = self._repo_with(_objective())
        repo.task_rows.append({"task_id": "child-1",
                               "objective_id": "OBJ-TEST",
                               "status": "in_progress"})
        service = _service(repo)
        service.recover_objective = AsyncMock()
        service.open_recovery_ticket = AsyncMock()
        result = await service.recover_waiting_objective("OBJ-TEST", now=NOW)
        self.assertEqual(result["action"], "none")
        self.assertIsNone(result["due_after"])
        service.recover_objective.assert_not_awaited()
        service.open_recovery_ticket.assert_not_awaited()

    async def test_not_due_reports_the_due(self) -> None:
        due = _iso(NOW + timedelta(minutes=5))
        repo = self._repo_with(_objective(recovery={
            "attempts": 0, "due_at": due, "exhausted": False}))
        service = _service(repo)
        result = await service.recover_waiting_objective("OBJ-TEST", now=NOW)
        self.assertEqual(result["action"], "none")
        self.assertEqual(result["reason"], "not due")
        self.assertEqual(result["due_after"], due)

    async def test_missing_store_or_objective_is_not_ok(self) -> None:
        service = _service(None)
        result = await service.recover_waiting_objective("OBJ-X", now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no-store")
        service = _service(_FakeRepo())
        result = await service.recover_waiting_objective("OBJ-X", now=NOW)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not-found")

    def _deploy_service(self, repo):
        service = _service(repo)
        return service, patch(
            "objectives.service.deploy_mod.spawn_service_restart",
            return_value=True), patch(
            "objectives.service.deploy_mod.live_checkout_ready",
            return_value=True), patch.object(
            engine_mod, "planning_in_flight", return_value=False)

    async def test_deploy_spawns_once_per_process(self) -> None:
        repo = self._repo_with(_deploy_objective(
            requested=NOW - timedelta(minutes=1),
            due=NOW - timedelta(seconds=1)))
        service, spawn_p, ready_p, flight_p = self._deploy_service(repo)
        with spawn_p as spawn, ready_p, flight_p:
            first = await service.recover_waiting_objective("OBJ-D", now=NOW)
            self.assertEqual(first["action"], "deploy")
            self.assertTrue(first["deployed"])
            spawn.assert_called_once()
            # The retry due re-drives the same park; the pending restart
            # already owns this process, so the second deploy defers.
            repo.docs["OBJ-D"]["recovery"]["due_at"] = \
                _iso(NOW - timedelta(seconds=1))
            second = await service.recover_waiting_objective("OBJ-D", now=NOW)
            self.assertEqual(second["action"], "defer")
            self.assertFalse(second["deployed"])
            spawn.assert_called_once()
            self.assertIsNotNone(policy.parse_due(second["due_after"]))

    async def test_busy_service_defers_without_restarting(self) -> None:
        repo = self._repo_with(_deploy_objective(
            requested=NOW - timedelta(minutes=1),
            due=NOW - timedelta(seconds=1)))
        repo.busy = 1
        service, spawn_p, ready_p, flight_p = self._deploy_service(repo)
        with spawn_p as spawn, ready_p, flight_p:
            result = await service.recover_waiting_objective("OBJ-D", now=NOW)
        spawn.assert_not_called()
        self.assertEqual(result["action"], "defer")
        self.assertFalse(result["deployed"])
        self.assertIsNotNone(policy.parse_due(result["due_after"]))

    async def test_preboot_deploy_resumes_through_recover_objective(self) -> None:
        repo = self._repo_with(_deploy_objective(
            requested=NOW - timedelta(minutes=5),
            due=NOW + timedelta(minutes=5)))
        service = _service(repo)
        service._booted_at = NOW - timedelta(minutes=1)
        service.recover_objective = AsyncMock(return_value={"ok": True})
        result = await service.recover_waiting_objective("OBJ-D", now=NOW)
        self.assertEqual(result["action"], "resume")
        service.recover_objective.assert_awaited_once_with("OBJ-D")


class AgendaKernelTests(unittest.IsolatedAsyncioTestCase):
    """The Agenda under the real kernel: wake, boot and no cap."""

    async def asyncSetUp(self) -> None:
        self.clock = _Clock(datetime.now(timezone.utc))
        self.repo = _FakeRepo(now_fn=self.clock)
        self.handled: list[dict] = []

    async def _handler(self, claimed: dict) -> dict:
        """Liquidate like the domain does: replan, or open the ticket.

        The ticket round collapses production's ticket-then-pacing into
        one liquidation (the pacing due lands at once): the Agenda
        contract asks for an unclaimable item or a strictly future due
        after the settle, and #586 keeps the wait due now while any
        no-due spent park remains.
        """
        self.handled.append(copy.deepcopy(claimed))
        recovery = dict(claimed.get("recovery") or {})
        if recovery.get("exhausted"):
            recovery.update({"ticket_id": "rec-1", "ticket_round": 1,
                             "ticket_at": _iso(self.clock()),
                             "exhausted": True,
                             "due_at": _iso(self.clock()
                                            + timedelta(minutes=10))})
            await self.repo.patch_objective(
                claimed["objective_id"], {"recovery": recovery})
            return {"ok": True, "action": "ticket"}
        await self.repo.patch_objective(
            claimed["objective_id"],
            {"status": "active", "waiting_reason": None})
        return {"ok": True, "action": "retried"}

    def _wired_kernel(self):
        waiter = _VirtualWaiter(self.clock)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = ObjectivesRecoveryAgenda(
            repo=self.repo, handler=self._handler, now_fn=self.clock)
        kernel.register(agenda)
        return kernel, agenda, waiter

    async def test_due_park_wakes_the_idle_kernel_with_an_indefinite_wait(self) -> None:
        """Idle waits forever; a due park plus its wake fires at once."""
        kernel, agenda, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.handled, [])
            self.repo.docs["OBJ-TEST"] = _objective(recovery={
                "attempts": 0, "due_at": _iso(self.clock() - timedelta(seconds=1)),
                "exhausted": False})
            kernel.wake(agenda.name)
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 1)
            # Idle means an indefinite wait, never a 3600 s safety net.
            self.assertIsNone(waiter.calls[0])
        finally:
            await kernel.stop()

    async def test_real_park_wakes_and_the_wait_targets_its_due(self) -> None:
        """A real ``_objective_park`` drives the kernel wait, uncapped."""
        kernel, agenda, waiter = self._wired_kernel()
        self.repo.docs["OBJ-TEST"] = _objective(status="active")
        env = SimpleNamespace(
            objectives=self.repo, tasks=None, notify=None, ceo_wake=None,
            recovery_wake=lambda: kernel.wake(agenda.name),
            objectives_cfg=CFG)
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            await engine_mod._objective_park(
                env, {"objective_id": "OBJ-TEST"}, "capability_gap",
                "no verified fix")
            await asyncio.sleep(0.15)
            self.assertEqual(self.handled, [])
            self.assertIsNone(waiter.calls[0])
            self.assertEqual(len(waiter.calls), 2)
            # The fixed clock predates the park, so the wait lands a
            # fraction over the 10 min backoff: it targets the due.
            self.assertGreaterEqual(waiter.calls[1], 590)
            self.assertLessEqual(waiter.calls[1], 605)
        finally:
            await kernel.stop()

    async def test_young_seal_of_another_boot_recovers_and_fires_once(self) -> None:
        """D3 under the kernel: a foreign seal is released, then runs."""
        doc = _objective(recovery={
            "attempts": 0,
            "due_at": _iso(self.clock() - timedelta(hours=1)),
            "exhausted": False})
        doc["claimed_boot_id"] = "boot-old"
        self.repo.docs["OBJ-TEST"] = doc
        kernel, agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 1)
            status = kernel.status()["agendas"]["objectives-recovery"]
            self.assertEqual(status["recovered"], 1)
            stored = self.repo.docs["OBJ-TEST"]
            self.assertEqual(stored["status"], "active")
            self.assertIsNone(stored["claimed_boot_id"])
        finally:
            await kernel.stop()

    async def test_preboot_deploy_fires_at_boot_despite_its_future_due(self) -> None:
        """The restart the park waited for happened: due now, not in 5 min."""
        self.repo.docs["OBJ-D"] = _deploy_objective(
            requested=self.clock() - timedelta(minutes=5),
            due=self.clock() + timedelta(minutes=5))
        kernel, _agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 1)
            status = kernel.status()["agendas"]["objectives-recovery"]
            self.assertEqual(status["recovered"], 1)
        finally:
            await kernel.stop()

    async def test_spent_park_fires_at_boot_and_never_loops(self) -> None:
        """#586: a spent park is due now, so the boot pass drives it once."""
        self.repo.docs["OBJ-S"] = _objective("OBJ-S", recovery={
            "attempts": 2, "due_at": None, "exhausted": True,
            "ticket_round": 0})
        kernel, _agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.2)
            self.assertEqual(len(self.handled), 1)
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 1)
        finally:
            await kernel.stop()

    async def test_spent_backlog_drains_on_a_single_wake(self) -> None:
        """#586: one wake drains every spent park; none stall behind it."""
        kernel, agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            for index in range(3):
                objective_id = f"OBJ-S{index}"
                self.repo.docs[objective_id] = _objective(
                    objective_id, recovery={
                        "attempts": 2, "due_at": None, "exhausted": True,
                        "ticket_round": 0})
            kernel.wake(agenda.name)
            await asyncio.sleep(0.5)
            self.assertEqual(len(self.handled), 3)
            self.assertEqual(
                {row["objective_id"] for row in self.handled},
                {"OBJ-S0", "OBJ-S1", "OBJ-S2"})
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 3)
        finally:
            await kernel.stop()

    async def test_ticket_failure_paces_and_never_loops(self) -> None:
        """F1 #586: a failed ticket round paces instead of spinning."""
        self.repo.docs["OBJ-S"] = _objective("OBJ-S", recovery={
            "attempts": 2, "due_at": None, "exhausted": True,
            "ticket_round": 0})
        service = _service(self.repo)
        service._booted_at = self.clock() - timedelta(hours=1)
        handled: list[str] = []

        async def failing_handler(claimed: dict) -> dict:
            handled.append(str(claimed.get("objective_id")))
            return await service.recover_waiting_objective(
                str(claimed.get("objective_id")), now=self.clock())

        waiter = _VirtualWaiter(self.clock)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = ObjectivesRecoveryAgenda(
            repo=self.repo, handler=failing_handler, now_fn=self.clock)
        kernel.register(agenda)
        try:
            with patch("objectives.service.create_internal_ticket",
                       AsyncMock(return_value=None)):
                await kernel.start()
                await asyncio.sleep(0.3)
                self.assertEqual(len(handled), 1)
                paced = policy.parse_due(
                    self.repo.docs["OBJ-S"]["recovery"]["due_at"])
                self.assertIsNotNone(paced)
                assert paced is not None
                self.assertGreater(paced, self.clock())
                await asyncio.sleep(0.2)
                self.assertEqual(len(handled), 1)
        finally:
            await kernel.stop()


if __name__ == "__main__":
    unittest.main()
