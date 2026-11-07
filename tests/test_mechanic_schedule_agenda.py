"""Mechanic heartbeat agenda of the global schedule kernel: contract (ticket #573).

Logic/contract only, with doubles and a virtual clock; no live model calls
(Owner order 2026-10-14). Beyond the ``next_due``/``claim_due``/``run``/
``settle``/``recover_at_boot`` contract, this proves the migration
properties from TASK-2026-00441 §5.5: the heartbeat as the only
heartbeat-class agenda (the single no-polling exception, greppable), the
persisted ``next_heartbeat`` replacing the in-memory one, the hot wake
driving event-only work through the preserved in-process path, and the
pseudo-claim over the tick — with no wait cap anywhere.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from mechanic.schedule_agenda import MechanicHeartbeatAgenda  # noqa: E402
from mechanic.service import (  # noqa: E402
    DEFAULTS,
    HEARTBEAT_DOC_ID,
    STATE_COLLECTION,
    MechanicService,
    mechanic_config,
)
from mechanic.stall import STREAK_KEY  # noqa: E402
from schedule_kernel import Claim, Outcome, ScheduleKernel  # noqa: E402
from state import TaskStatus  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)


def _task(task_id: str = "t1", **overrides: Any) -> dict[str, Any]:
    doc = {"task_id": task_id, "status": TaskStatus.WAITING.value,
           "autorun": True, "project": "p",
           "waiting_reason": "needs_owner: invalid plan",
           "resume_at": None, "updated_at": NOW - timedelta(hours=1),
           "params": {}}
    doc.update(overrides)
    return doc


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class _VirtualWaiter:
    """As in the sisters: with the budget spent, wait on the real event."""

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


class _FakeColl:
    """In-memory state/runs collection, mirroring the oracle's fake.

    ``fail_reads``/``fail_writes`` inject a store outage to prove the
    propagate-on-read and fail-open-on-write paths.
    """

    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.state: dict[str, dict] = {}
        self.fail_reads = False
        self.fail_writes = False
        self.indexes: list[str] = []

    async def create_index(self, *args: Any, **_kw: Any) -> str:
        self.indexes.append(str(args[0]) if args else "")
        return "at_1"

    async def count_documents(self, query: dict) -> int:
        floor = (query.get("at") or {}).get("$gte")
        return sum(1 for doc in self.docs if floor is None or doc["at"] >= floor)

    async def insert_one(self, doc: dict) -> None:
        self.docs.append(doc)

    async def find_one(self, query: dict) -> dict | None:
        if self.fail_reads:
            raise RuntimeError("store down")
        row = self.state.get(query["_id"])
        return dict(row) if row is not None else None

    async def update_one(self, query: dict, update: dict,
                         upsert: bool = False) -> None:
        if self.fail_writes:
            raise RuntimeError("store down")
        doc = self.state.setdefault(query["_id"], {"_id": query["_id"]})
        doc.update(update.get("$set", {}))
        for key, spec in (update.get("$addToSet") or {}).items():
            values = spec["$each"] if isinstance(spec, dict) else [spec]
            doc.setdefault(key, [])
            doc[key] += [value for value in values if value not in doc[key]]

    async def delete_one(self, query: dict) -> None:
        self.state.pop(query["_id"], None)


class _FakeRepo:
    def __init__(self, tasks: list[dict] | None = None) -> None:
        self.rows = {task["task_id"]: task for task in (tasks or [])}
        self.colls: dict[str, _FakeColl] = {}
        self.db = self
        self.fail_lists = False
        self.list_calls = 0

    def __getitem__(self, name: str) -> _FakeColl:
        return self.colls.setdefault(name, _FakeColl())

    def state(self) -> _FakeColl:
        return self[STATE_COLLECTION]

    async def get_task(self, task_id: str) -> dict | None:
        return self.rows.get(task_id)

    async def list_tasks_by_status(self, status: TaskStatus,
                                   limit: int = 200) -> list[dict]:
        if self.fail_lists:
            raise RuntimeError("store down")
        self.list_calls += 1
        return [t for t in self.rows.values()
                if t["status"] == status.value][:limit]

    async def list_heartbeat_tasks(self, limit: int = 500) -> list[dict]:
        if self.fail_lists:
            raise RuntimeError("store down")
        self.list_calls += 1
        return [t for t in self.rows.values()
                if t["status"] == TaskStatus.WAITING.value
                or (t["status"] == TaskStatus.QUEUED.value
                    and t.get("blocked_by"))][:limit]

    async def patch_task(self, task_id: str, updates: dict) -> dict | None:
        task = self.rows.get(task_id)
        if task is None:
            return None
        for key, value in updates.items():
            if key.startswith("params."):
                task.setdefault("params", {})[key[7:]] = value
            else:
                task[key] = value
        return task


class _FakeTasks:
    """TaskService double: params merge with None popping a key, as in prod."""

    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo
        self.comments: list[tuple[str, str]] = []

    async def get_task(self, task_id: str) -> dict | None:
        return await self._repo.get_task(task_id)

    async def patch_task(self, task_id: str, updates: dict) -> dict | None:
        updates = dict(updates)
        if isinstance(updates.get("params"), dict):
            merged = dict(self._repo.rows[task_id].get("params") or {})
            for key, value in updates["params"].items():
                merged.pop(key, None) if value is None \
                    else merged.__setitem__(key, value)
            updates["params"] = merged
        return await self._repo.patch_task(task_id, updates)

    async def comment_on_ticket(self, task_id: str, text: str) -> bool:
        self.comments.append((task_id, text))
        return True


def _no_executor() -> Any:
    raise AssertionError("a contract test must never run the model")


def _service(repo: _FakeRepo, clock: _Clock, **cfg: Any) -> MechanicService:
    return MechanicService(
        repo=repo, task_service=_FakeTasks(repo),
        config_fn=lambda: mechanic_config({"mechanic": cfg}),
        executor_fn=_no_executor, repo_for_project=lambda _p: "/repo",
        create_ticket=AsyncMock(return_value={"task_id": "fix-1"}),
        wake_dispatcher=MagicMock(), clock=clock)


def _claim(boot_id: str = "boot-1") -> Claim:
    return Claim(item_id="cycle", boot_id=boot_id, fencing_token=0)


class AgendaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()

    def _agenda(self, service: MechanicService | None = None,
              **cfg: Any) -> MechanicHeartbeatAgenda:
        return MechanicHeartbeatAgenda(
            service=service or _service(self.repo, self.clock, **cfg))

    async def _recovered(self, **cfg: Any) -> MechanicHeartbeatAgenda:
        agenda = self._agenda(**cfg)
        await agenda.recover_at_boot("boot-1")
        return agenda

    async def test_agenda_declares_the_heartbeat_class(self) -> None:
        agenda = self._agenda()
        self.assertEqual(agenda.name, "mechanic-heartbeat")
        self.assertEqual(agenda.lanes, 1)
        self.assertTrue(agenda.hot_on_wake)
        # The single no-polling exception, in one greppable place.
        self.assertEqual(agenda.agenda_class, "heartbeat")

    async def test_next_due_none_when_never_scheduled(self) -> None:
        agenda = await self._recovered()
        self.assertIsNone(await agenda.next_due())

    async def test_next_due_returns_persisted_due(self) -> None:
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW + timedelta(minutes=30)}
        agenda = await self._recovered()
        self.assertEqual(await agenda.next_due(), NOW + timedelta(minutes=30))

    async def test_next_due_returns_overdue_due_as_is(self) -> None:
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW - timedelta(seconds=1)}
        agenda = await self._recovered()
        self.assertEqual(await agenda.next_due(), NOW - timedelta(seconds=1))

    async def test_next_due_none_on_garbage_value(self) -> None:
        """An unparsable due scans at once instead of wedging the wait."""
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": "not-a-date"}
        agenda = await self._recovered()
        self.assertIsNone(await agenda.next_due())

    async def test_next_due_propagates_read_errors(self) -> None:
        self.repo.state().fail_reads = True
        agenda = await self._recovered()
        with self.assertRaises(RuntimeError):
            await agenda.next_due()

    async def test_claim_due_without_boot_claims_nothing(self) -> None:
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 1), [])

    async def test_claim_due_zero_limit_claims_nothing(self) -> None:
        agenda = await self._recovered()
        self.assertEqual(await agenda.claim_due(NOW, 0), [])

    async def test_claim_due_pseudo_claim_stamped_with_boot(self) -> None:
        agenda = await self._recovered()
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].item_id, "cycle")
        self.assertEqual(claims[0].boot_id, "boot-1")
        self.assertEqual(claims[0].fencing_token, 0)

    async def test_run_drains_queued_wake_without_scanning_when_due_future(
            self) -> None:
        """A hot cycle with nothing due drains the queue and writes nothing."""
        self.repo.rows["q1"] = _task("q1", status=TaskStatus.QUEUED.value)
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW + timedelta(minutes=30)}
        service = _service(self.repo, self.clock)
        agenda = self._agenda(service)
        await agenda.recover_at_boot("boot-1")
        service.wake("q1", "gave_up")
        outcome = await agenda.run(_claim())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["ran"], 0)
        self.assertEqual(service._queue, {})
        self.assertEqual(
            self.repo.state().state[HEARTBEAT_DOC_ID]["next_heartbeat"],
            NOW + timedelta(minutes=30))

    async def test_run_scans_when_due_and_persists_next(self) -> None:
        """First cycle with no doc scans (as before) and persists the due."""
        self.repo.rows["cool"] = _task(
            "cool", params={"mechanic": {"history": [
                (NOW - timedelta(minutes=10)).isoformat()]}})
        agenda = await self._recovered()
        outcome = await agenda.run(_claim())
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.result["ran"], 0)
        self.assertEqual(
            self.repo.state().state[HEARTBEAT_DOC_ID]["next_heartbeat"],
            NOW + timedelta(minutes=30))

    async def test_run_scans_exactly_at_due(self) -> None:
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW}
        agenda = await self._recovered()
        await agenda.run(_claim())
        self.assertEqual(
            self.repo.state().state[HEARTBEAT_DOC_ID]["next_heartbeat"],
            NOW + timedelta(minutes=30))

    async def test_run_respects_heartbeat_minutes(self) -> None:
        agenda = await self._recovered(heartbeat_minutes=5)
        await agenda.run(_claim())
        self.assertEqual(
            self.repo.state().state[HEARTBEAT_DOC_ID]["next_heartbeat"],
            NOW + timedelta(minutes=5))

    async def test_heartbeat_default_stays_thirty_minutes(self) -> None:
        self.assertEqual(DEFAULTS["heartbeat_minutes"], 30)
        agenda = await self._recovered()
        await agenda.run(_claim())
        self.assertEqual(
            self.repo.state().state[HEARTBEAT_DOC_ID]["next_heartbeat"],
            NOW + timedelta(minutes=30))

    async def test_run_propagates_unexpected_errors(self) -> None:
        self.repo.fail_lists = True
        agenda = await self._recovered()
        with self.assertRaises(RuntimeError):
            await agenda.run(_claim())

    async def test_disabled_run_persists_hourly_recheck(self) -> None:
        self.repo.rows["t1"] = _task("t1")
        service = _service(self.repo, self.clock, enabled=False)
        agenda = self._agenda(service)
        await agenda.recover_at_boot("boot-1")
        service.wake("t1", "gave_up")
        outcome = await agenda.run(_claim())
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.result["disabled"])
        self.assertEqual(
            self.repo.state().state[HEARTBEAT_DOC_ID]["next_heartbeat"],
            NOW + timedelta(hours=1))

    async def test_due_write_failure_still_drains_queue(self) -> None:
        """A lost due write never loses queued findings; it only rescans."""
        self.repo.rows["q1"] = _task("q1", status=TaskStatus.QUEUED.value)
        self.repo.state().fail_writes = True
        service = _service(self.repo, self.clock)
        agenda = self._agenda(service)
        await agenda.recover_at_boot("boot-1")
        service.wake("q1", "gave_up")
        outcome = await agenda.run(_claim())
        self.assertTrue(outcome.ok)
        self.assertEqual(service._queue, {})
        self.assertEqual(service._next_heartbeat, NOW + timedelta(minutes=30))
        self.assertNotIn(HEARTBEAT_DOC_ID, self.repo.state().state)

    async def test_settle_leaves_state_untouched(self) -> None:
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW + timedelta(minutes=30)}
        agenda = await self._recovered()
        await agenda.settle(_claim(), Outcome(ok=True, result={"ran": 0}))
        self.assertEqual(
            self.repo.state().state[HEARTBEAT_DOC_ID]["next_heartbeat"],
            NOW + timedelta(minutes=30))

    async def test_settle_failed_outcome_never_raises(self) -> None:
        agenda = await self._recovered()
        await agenda.settle(_claim(), Outcome(ok=False, error="boom"))

    async def test_recover_remembers_boot_first(self) -> None:
        agenda = self._agenda()
        self.assertEqual(await agenda.recover_at_boot("boot-9"), 0)
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual([c.boot_id for c in claims], ["boot-9"])


class ProducerWakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo([_task("t1")])
        self.clock = _Clock()
        self.service = _service(self.repo, self.clock)
        self.woken: list[str] = []

    async def test_unbound_wake_drives_the_isolated_loop(self) -> None:
        self.service.wake("t1", "stall", "3x repo_busy")
        self.assertIn("t1", self.service._queue)
        self.assertTrue(self.service._wake.is_set())
        self.assertEqual(self.woken, [])

    async def test_bound_wake_delegates_and_skips_the_legacy_event(self) -> None:
        self.service.delegate_wake_to(lambda: self.woken.append("kernel"))
        self.service.wake("t1", "stall", "3x repo_busy")
        self.assertIn("t1", self.service._queue)
        self.assertEqual(self.woken, ["kernel"])
        self.assertFalse(self.service._wake.is_set())

    async def test_unbind_restores_the_legacy_event(self) -> None:
        self.service.delegate_wake_to(lambda: self.woken.append("kernel"))
        self.service.delegate_wake_to(None)
        self.service.wake("t1", "stall")
        self.assertTrue(self.service._wake.is_set())
        self.assertEqual(self.woken, [])

    async def test_disabled_wake_queues_nothing(self) -> None:
        service = _service(self.repo, self.clock, enabled=False)
        service.delegate_wake_to(lambda: self.woken.append("kernel"))
        service.wake("t1", "gave_up")
        self.assertEqual(service._queue, {})
        self.assertEqual(self.woken, [])

    async def test_fix_done_pokes_the_delegate(self) -> None:
        """A failed fix requeues a fresh diagnosis through the kernel."""
        self.repo.rows["fix-1"] = _task(
            "fix-1", status="failed", error="gates red",
            updated_at=NOW - timedelta(minutes=1),
            params={"mechanic_fix_for": ["t1"]})
        # In cooldown: the scan sees t1 but must not spend a model run.
        self.repo.rows["t1"]["params"] = {"mechanic": {"history": [
            (NOW - timedelta(minutes=10)).isoformat()]}}
        self.service.delegate_wake_to(lambda: self.woken.append("kernel"))
        self.service.wake("fix-1", "fix_done")
        await self.service.tick()
        self.assertIn("t1", self.service._queue)
        self.assertEqual(self.service._queue["t1"].trigger, "fix_failed")
        self.assertEqual(self.woken, ["kernel", "kernel"])

    async def test_boot_resume_runs_without_starting_the_loop(self) -> None:
        self.repo.state().state["deploy"] = {
            "_id": "deploy", "pending": True, "since": NOW,
            "resume": ["t1"], "fixes": ["fix-1"]}
        await self.service.boot_resume()
        self.assertEqual(self.repo.rows["t1"]["status"], "queued")
        self.assertNotIn("deploy", self.repo.state().state)
        self.assertIsNone(self.service._task)
        self.assertIn("at", self.repo["mechanic_runs"].indexes)

    async def test_boot_resume_never_raises(self) -> None:
        broken = MagicMock()
        broken.db = MagicMock()
        broken.db.__getitem__.side_effect = RuntimeError("store down")
        broken.get_task = AsyncMock(side_effect=RuntimeError("store down"))
        service = _service(broken, self.clock)
        await service.boot_resume()
        self.assertIsNone(service._task)


class AgendaKernelTests(unittest.IsolatedAsyncioTestCase):
    """The Agenda under the real kernel: hot wakes, persisted due, no cap."""

    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.service = _service(self.repo, self.clock)

    def _wired_kernel(self, budget: int = 0):
        waiter = _VirtualWaiter(self.clock, budget=budget)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = MechanicHeartbeatAgenda(service=self.service)
        kernel.register(agenda)
        self.service.delegate_wake_to(lambda: kernel.wake(agenda.name))
        return kernel, agenda, waiter

    def _due(self) -> datetime | None:
        doc = self.repo.state().state.get(HEARTBEAT_DOC_ID)
        return doc.get("next_heartbeat") if doc else None

    async def test_idle_kernel_waits_indefinitely(self) -> None:
        kernel, _agenda, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(waiter.calls, [None])
            self.assertIsNone(self._due())
        finally:
            await kernel.stop()

    async def test_boot_wake_ticks_and_persists_the_due(self) -> None:
        """The lifespan boot wake is the old immediate first tick."""
        kernel, _agenda, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertIsNone(self._due())
            kernel.wake("mechanic-heartbeat")
            await asyncio.sleep(0.15)
            self.assertEqual(self._due(), NOW + timedelta(minutes=30))
            # Idle first, then the exact interval (lane-busy passes add
            # their own indefinite waits in between).
            self.assertIsNone(waiter.calls[0])
            self.assertEqual(
                [c for c in waiter.calls if c is not None], [1800.0])
        finally:
            await kernel.stop()

    async def test_queued_wake_drives_a_hot_cycle(self) -> None:
        self.repo.rows["q1"] = _task("q1", status=TaskStatus.QUEUED.value)
        kernel, _agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.service.wake("q1", "gave_up")
            await asyncio.sleep(0.15)
            self.assertEqual(self.service._queue, {})
            self.assertEqual(self._due(), NOW + timedelta(minutes=30))
        finally:
            await kernel.stop()

    async def test_overdue_due_at_boot_scans_once_and_rearms(self) -> None:
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW - timedelta(seconds=1)}
        kernel, _agenda, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.2)
            self.assertEqual(self._due(), NOW + timedelta(minutes=30))
            # The exact interval, never a rounded cap.
            self.assertEqual(
                [c for c in waiter.calls if c is not None], [1800.0])
        finally:
            await kernel.stop()

    async def test_future_due_waits_the_exact_interval(self) -> None:
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW + timedelta(minutes=30)}
        kernel, _agenda, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(waiter.calls, [1800.0])
            self.assertEqual(self._due(), NOW + timedelta(minutes=30))
        finally:
            await kernel.stop()

    async def test_second_boot_skips_the_immediate_scan(self) -> None:
        """The persisted due survives a restart: no scan until it lapses."""
        self.repo.state().state[HEARTBEAT_DOC_ID] = {
            "_id": HEARTBEAT_DOC_ID, "next_heartbeat": NOW + timedelta(minutes=30)}
        kernel, _agenda, waiter = self._wired_kernel(budget=1)
        try:
            await kernel.start()
            kernel.wake("mechanic-heartbeat")
            await asyncio.sleep(0.2)
            # The hot boot tick drained nothing and scanned nothing; the
            # virtual wait then fired exactly one scan at the due.
            self.assertEqual(self.repo.list_calls, 1)
            self.assertEqual(self._due(), NOW + timedelta(hours=1))
            self.assertEqual(waiter.calls[0], 1800.0)
        finally:
            await kernel.stop()
