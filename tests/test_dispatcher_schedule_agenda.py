"""Dispatcher agenda of the global schedule kernel: contract with doubles (ticket #572).

Logic/contract only, with doubles and a virtual clock; no live model calls
(Owner order 2026-10-14). Beyond the ``next_due``/``claim_due``/``run``/
``settle``/``recover_at_boot`` contract, this proves the migration
properties from TASK-2026-00441 §5.5: the backup due plus the absorbed
DeadlineWatch as one agenda, the hot wake driving event-only work, the
pseudo-claim with the per-ticket atomic claim preserved, and the
check-only cycle that never arms the watch timer — with no wait cap
anywhere.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from dispatcher import Dispatcher  # noqa: E402
from dispatcher_schedule_agenda import DispatcherScheduleAgenda  # noqa: E402
from reliability import deadlines as deadline_mod  # noqa: E402
from schedule_kernel import Claim, Outcome, ScheduleKernel  # noqa: E402
from state import TaskStatus  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, 0, tzinfo=timezone.utc)


def _ticket(task_id: str, **overrides: object) -> dict:
    doc = {
        "task_id": task_id,
        "status": TaskStatus.QUEUED.value,
        "project": "proj",
        "priority": "medium",
        "created_at": NOW - timedelta(hours=1),
        "waiting_reason": None,
        "resume_at": None,
        "autorun": True,
        "blocked_by": [],
        "objective_id": None,
        "coding_tool": "tier_b",
    }
    doc.update(overrides)
    return doc


def _settings(**overrides: object) -> SimpleNamespace:
    cfg = {
        "DISPATCHER_BACKUP_SECONDS": 300,
        "DISPATCHER_MAX_ACTIVE_PER_PROJECT": 1,
        "DISPATCHER_MAX_ACTIVE_PER_OBJECTIVE": 6,
    }
    cfg.update(overrides)
    return SimpleNamespace(**cfg)


class _FakeRepo:
    """In-memory tasks + backup doc, mirroring the oracle's fake.

    ``fail_backup_reads``/``fail_lists`` inject a store outage to prove
    the fail-open paths; the atomic claim keeps its lock.
    """

    def __init__(self, tasks: list[dict] | None = None) -> None:
        self.tasks: dict[str, dict] = {t["task_id"]: dict(t) for t in (tasks or [])}
        self.backup: dict | None = None
        self.locks: dict[str, dict] = {}
        self.fail_backup_reads = False
        self.fail_lists = False
        self._claim_lock = asyncio.Lock()

    async def list_tasks_by_status(self, status: TaskStatus, limit: int = 200) -> list[dict]:
        if self.fail_lists:
            raise RuntimeError("store down")
        return [dict(t) for t in self.tasks.values() if t.get("status") == status.value][:limit]

    async def due_waiting_tasks(self, now_utc: datetime) -> list[dict]:
        out = []
        for t in self.tasks.values():
            if t.get("status") != TaskStatus.WAITING.value:
                continue
            resume_at = t.get("resume_at")
            if not isinstance(resume_at, datetime):
                continue
            if resume_at.tzinfo is None:
                resume_at = resume_at.replace(tzinfo=timezone.utc)
            if resume_at <= now_utc:
                out.append(dict(t))
        return out

    async def get_task(self, task_id: str) -> dict | None:
        row = self.tasks.get(task_id)
        return dict(row) if row else None

    async def patch_task(self, task_id: str, updates: dict) -> dict | None:
        row = self.tasks.get(task_id)
        if row is None:
            return None
        row.update(updates)
        return dict(row)

    async def claim_queued_ticket(self, task_id: str) -> dict | None:
        async with self._claim_lock:
            row = self.tasks.get(task_id)
            if row is None or row.get("status") != TaskStatus.QUEUED.value:
                return None
            row.update({
                "status": TaskStatus.IN_PROGRESS.value,
                "started_at": NOW,
                "waiting_reason": None,
                "resume_at": None,
            })
            return dict(row)

    async def get_dispatcher_backup(self) -> dict | None:
        if self.fail_backup_reads:
            raise RuntimeError("store down")
        return dict(self.backup) if self.backup else None

    async def ensure_dispatcher_backup(self, next_run_at: datetime, reason: str = "") -> dict:
        self.backup = {"_id": "backup", "next_run_at": next_run_at, "reason": reason}
        return dict(self.backup)

    async def clear_dispatcher_backup(self) -> bool:
        existed = self.backup is not None
        self.backup = None
        return existed

    async def repo_lock_holder(self, repo: str) -> dict | None:
        row = self.locks.get(repo)
        return dict(row) if row else None


class _FakeService:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo
        self.patches: list[tuple[str, dict]] = []

    def _task_allows_autorun(self, task: dict) -> bool:
        autorun = task.get("autorun")
        if autorun is None:
            return True
        return bool(autorun)

    async def blockers_open(self, task: dict) -> bool:
        for blocker in task.get("blocked_by") or []:
            row = await self._repo.get_task(str(blocker))
            if str((row or {}).get("status") or "") not in {"completed", "cancelled"}:
                return True
        return False

    async def patch_task(self, task_id: str, updates: dict) -> dict | None:
        self.patches.append((task_id, dict(updates)))
        return await self._repo.patch_task(task_id, updates)


class _FakeMonitor:
    def __init__(self, verdict: tuple | None = None) -> None:
        self.verdict = verdict or (True, None, None)

    async def admission_for_ticket(
        self, ticket: dict, active_tickets: list[dict] | None = None
    ) -> tuple:
        return self.verdict


class _FailingWatch:
    """A watch whose due read is down: the backup must still drive."""

    async def next_future_deadline(self, now: datetime | None = None) -> datetime | None:
        raise RuntimeError("store down")


async def _drain() -> None:
    """Let fire-and-forget launchers run before asserting side effects."""
    await asyncio.sleep(0)


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


def _recorder(recorded: list[dict]):
    async def _record(**fields: object) -> str:
        recorded.append(dict(fields))
        return f"incident-{len(recorded)}"
    return _record


def _watch(repo: _FakeRepo, recorded: list[dict],
           clock: _Clock) -> deadline_mod.DeadlineWatch:
    return deadline_mod.DeadlineWatch(
        repo, record=_recorder(recorded), clock=clock)


def _dispatcher(repo: _FakeRepo, service: _FakeService,
                launched: list[str], watch=None) -> Dispatcher:
    async def launcher(task_id: str) -> None:
        launched.append(task_id)

    return Dispatcher(
        repo=repo, settings=_settings(), service=service,
        rate_monitor=_FakeMonitor(), launcher=launcher,
        deadline_watch=watch,
    )


def _claim() -> Claim:
    return Claim(item_id="cycle", boot_id="boot-1", fencing_token=0)


class AgendaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.launched: list[str] = []
        self.recorded: list[dict] = []
        self.service = _FakeService(self.repo)
        self.watch = _watch(self.repo, self.recorded, self.clock)
        self.dispatcher = _dispatcher(
            self.repo, self.service, self.launched, watch=self.watch)

    def _agenda(self, dispatcher=None, watch="default") -> DispatcherScheduleAgenda:
        if watch == "default":
            watch = self.watch
        return DispatcherScheduleAgenda(
            dispatcher=dispatcher or self.dispatcher, watch=watch,
            now_fn=self.clock)

    async def _recovered(self, **kw) -> DispatcherScheduleAgenda:
        agenda = self._agenda(**kw)
        await agenda.recover_at_boot("boot-1")
        return agenda

    async def test_next_due_none_when_queue_empty_and_nothing_pending(self) -> None:
        agenda = await self._recovered()
        self.assertIsNone(await agenda.next_due())

    async def test_next_due_returns_backup_due(self) -> None:
        await self.repo.ensure_dispatcher_backup(
            NOW + timedelta(seconds=300), "1 waiting")
        agenda = await self._recovered()
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=300))

    async def test_next_due_returns_sooner_of_backup_and_deadline(self) -> None:
        cases = [
            # Backup sooner than the resume deadline (+60 s resume => +660 s deadline).
            (timedelta(seconds=300), timedelta(seconds=300)),
            # A far backup loses against the nearer deadline.
            (timedelta(seconds=900), timedelta(seconds=660)),
        ]
        for backup_in, expected_in in cases:
            with self.subTest(backup_in=backup_in):
                repo = _FakeRepo([
                    _ticket("waiter", status=TaskStatus.WAITING.value,
                            project="p1", waiting_reason="rate_limit_queued",
                            resume_at=NOW + timedelta(seconds=60)),
                ])
                await repo.ensure_dispatcher_backup(
                    NOW + backup_in, "1 waiting")
                service = _FakeService(repo)
                recorded: list[dict] = []
                clock = _Clock()
                watch = _watch(repo, recorded, clock)
                agenda = DispatcherScheduleAgenda(
                    dispatcher=_dispatcher(repo, service, [], watch=watch),
                    watch=watch, now_fn=clock)
                await agenda.recover_at_boot("boot-1")
                self.assertEqual(await agenda.next_due(), NOW + expected_in)

    async def test_next_due_ignores_overdue_deadlines(self) -> None:
        """Already-due deadlines never drive the wait (no hot re-drive)."""
        self.repo.tasks["hung"] = _ticket(
            "hung", status=TaskStatus.IN_PROGRESS.value, project="other",
            attempt_id="hung-g0-w0",
            attempt_deadline_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered()
        self.assertIsNone(await agenda.next_due())
        await self.repo.ensure_dispatcher_backup(
            NOW + timedelta(seconds=300), "1 waiting")
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=300))

    async def test_next_due_fail_open_on_unreadable_backup(self) -> None:
        self.repo.fail_backup_reads = True
        agenda = await self._recovered()
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=300))

    async def test_next_due_fail_open_on_deadline_failure(self) -> None:
        agenda = await self._recovered(watch=_FailingWatch())
        self.assertIsNone(await agenda.next_due())
        await self.repo.ensure_dispatcher_backup(
            NOW + timedelta(seconds=300), "1 waiting")
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=300))

    async def test_next_due_holds_on_garbage_backup(self) -> None:
        self.repo.backup = {"_id": "backup", "next_run_at": "not-a-date"}
        agenda = await self._recovered()
        due = await agenda.next_due()
        assert due is not None
        self.assertEqual(due.year, 9999)

    async def test_claim_due_without_boot_claims_nothing(self) -> None:
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 5), [])

    async def test_claim_due_pseudo_claim_stamped_with_boot(self) -> None:
        agenda = await self._recovered()
        claims = await agenda.claim_due(NOW, 5)
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim.item_id, "cycle")
        self.assertEqual(claim.boot_id, "boot-1")
        self.assertEqual(claim.fencing_token, 0)

    async def test_claim_due_zero_limit_claims_nothing(self) -> None:
        agenda = await self._recovered()
        self.assertEqual(await agenda.claim_due(NOW, 0), [])

    async def test_run_launches_ready_and_reports_counts(self) -> None:
        self.repo.tasks["queued-1"] = _ticket("queued-1", project="free")
        agenda = await self._recovered()
        outcome = await agenda.run(_claim())
        await _drain()
        self.assertTrue(outcome.ok)
        self.assertEqual(self.launched, ["queued-1"])
        self.assertEqual(outcome.result["launched"], ["queued-1"])
        self.assertIsNone(await self.repo.get_dispatcher_backup())

    async def test_run_checks_deadlines_without_arming(self) -> None:
        """The kernel owns the wait: misses are recorded, no timer arms."""
        self.repo.tasks["blocker"] = _ticket(
            "blocker", status=TaskStatus.IN_PROGRESS.value, project="other")
        self.repo.tasks["held"] = _ticket(
            "held", status=TaskStatus.WAITING.value, project="p1",
            waiting_reason="rate_limit_queued",
            resume_at=NOW - timedelta(minutes=11),
            blocked_by=["blocker"])
        # A future deadline the old refresh would arm its timer to.
        self.repo.tasks["waiter"] = _ticket(
            "waiter", status=TaskStatus.WAITING.value, project="p2",
            waiting_reason="rate_limit_queued",
            resume_at=NOW + timedelta(minutes=5))
        agenda = await self._recovered()
        outcome = await agenda.run(_claim())
        await _drain()
        self.assertTrue(outcome.ok)
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.recorded[0]["deadline_type"], "resume")
        self.assertEqual(self.recorded[0]["task_id"], "held")
        self.assertFalse(self.watch.has_timer)

    async def test_run_without_watch_skips_deadlines(self) -> None:
        self.repo.tasks["queued-1"] = _ticket("queued-1", project="free")
        agenda = await self._recovered(watch=None)
        outcome = await agenda.run(_claim())
        await _drain()
        self.assertTrue(outcome.ok)
        self.assertEqual(self.launched, ["queued-1"])
        self.assertEqual(self.recorded, [])

    async def test_run_propagates_unexpected_errors(self) -> None:
        self.repo.fail_lists = True
        agenda = await self._recovered(watch=None)
        with self.assertRaises(RuntimeError):
            await agenda.run(_claim())

    async def test_settle_leaves_state_untouched(self) -> None:
        self.repo.tasks["active-1"] = _ticket(
            "active-1", status=TaskStatus.IN_PROGRESS.value, project="proj")
        self.repo.tasks["queued-1"] = _ticket("queued-1", project="proj")
        agenda = await self._recovered()
        outcome = await agenda.run(_claim())
        await _drain()
        backup_before = await self.repo.get_dispatcher_backup()
        row_before = await self.repo.get_task("queued-1")
        await agenda.settle(_claim(), outcome)
        self.assertEqual(await self.repo.get_dispatcher_backup(), backup_before)
        self.assertEqual(await self.repo.get_task("queued-1"), row_before)

    async def test_settle_failed_outcome_never_raises(self) -> None:
        agenda = await self._recovered()
        await agenda.settle(_claim(), Outcome(ok=False, error="boom"))

    async def test_recover_remembers_boot_first(self) -> None:
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 5), [])
        self.assertEqual(await agenda.recover_at_boot("boot-1"), 0)
        claims = await agenda.claim_due(NOW, 5)
        self.assertEqual([c.item_id for c in claims], ["cycle"])
        self.assertEqual(claims[0].boot_id, "boot-1")


class ProducerWakeTests(unittest.IsolatedAsyncioTestCase):
    """``Dispatcher.wake`` stays the entry point; the delegate moves it."""

    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.launched: list[str] = []
        self.service = _FakeService(self.repo)
        self.dispatcher = _dispatcher(self.repo, self.service, self.launched)

    async def test_unbound_wake_drives_the_isolated_loop(self) -> None:
        try:
            await self.dispatcher.start()
            self.repo.tasks["queued-1"] = _ticket("queued-1", project="free")
            self.dispatcher.wake()

            async def _wait_for_launch() -> None:
                while not self.launched:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_wait_for_launch(), timeout=5)
            self.assertEqual(self.launched, ["queued-1"])
        finally:
            await self.dispatcher.stop()

    async def test_bound_wake_delegates_and_skips_the_legacy_event(self) -> None:
        calls: list[int] = []
        self.dispatcher.delegate_wake_to(lambda: calls.append(1))
        self.dispatcher.wake()
        self.assertEqual(calls, [1])
        self.assertFalse(self.dispatcher._wake.is_set())

    async def test_launch_end_wakes_through_the_delegate(self) -> None:
        calls: list[int] = []
        self.dispatcher.delegate_wake_to(lambda: calls.append(1))
        self.repo.tasks["queued-1"] = _ticket("queued-1", project="free")
        await self.dispatcher.dispatch_cycle(now=NOW)
        await _drain()
        await _drain()
        self.assertEqual(self.launched, ["queued-1"])
        self.assertTrue(calls)

    async def test_unbind_restores_the_legacy_event(self) -> None:
        calls: list[int] = []
        self.dispatcher.delegate_wake_to(lambda: calls.append(1))
        self.dispatcher.delegate_wake_to(None)
        self.dispatcher.wake()
        self.assertEqual(calls, [])
        self.assertTrue(self.dispatcher._wake.is_set())


class AgendaKernelTests(unittest.IsolatedAsyncioTestCase):
    """The Agenda under the real kernel: hot wakes, backup, deadlines."""

    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.launched: list[str] = []
        self.recorded: list[dict] = []
        self.service = _FakeService(self.repo)
        self.watch = _watch(self.repo, self.recorded, self.clock)
        self.dispatcher = _dispatcher(
            self.repo, self.service, self.launched, watch=self.watch)

    def _wired_kernel(self, budget: int = 0):
        waiter = _VirtualWaiter(self.clock, budget=budget)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = DispatcherScheduleAgenda(
            dispatcher=self.dispatcher, watch=self.watch, now_fn=self.clock)
        kernel.register(agenda)
        self.dispatcher.delegate_wake_to(lambda: kernel.wake(agenda.name))
        return kernel, agenda, waiter

    async def test_idle_kernel_waits_indefinitely(self) -> None:
        kernel, _agenda, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.launched, [])
            self.assertEqual(self.recorded, [])
            self.assertEqual(waiter.calls, [None])
        finally:
            await kernel.stop()

    async def test_boot_wake_reconciles_ready_tickets(self) -> None:
        """The lifespan boot wake is the old ``start`` reconcile."""
        self.repo.tasks["queued-1"] = _ticket("queued-1", project="free")
        kernel, _agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.launched, [])
            kernel.wake("dispatcher")
            await asyncio.sleep(0.15)
            self.assertEqual(self.launched, ["queued-1"])
            self.assertIsNone(await self.repo.get_dispatcher_backup())
        finally:
            await kernel.stop()

    async def test_new_ticket_wakes_hot_and_launches(self) -> None:
        kernel, _agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.repo.tasks["queued-1"] = _ticket("queued-1", project="free")
            self.dispatcher.wake()
            await asyncio.sleep(0.15)
            self.assertEqual(self.launched, ["queued-1"])
        finally:
            await kernel.stop()

    async def test_backup_due_drives_a_parked_cycle(self) -> None:
        """The conditional backup still fires at the full interval, uncapped."""
        self.repo.tasks["active-1"] = _ticket(
            "active-1", status=TaskStatus.IN_PROGRESS.value, project="proj")
        self.repo.tasks["queued-1"] = _ticket("queued-1", project="proj")
        kernel, _agenda, waiter = self._wired_kernel(budget=1)
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            kernel.wake("dispatcher")
            await asyncio.sleep(0.2)
            self.assertEqual(self.launched, [])
            row = await self.repo.get_task("queued-1")
            assert row is not None
            self.assertIsNotNone(row["waiting_reason"])
            backup = await self.repo.get_dispatcher_backup()
            assert backup is not None
            # Boot-wake cycle armed +300; the virtual due fired it again.
            self.assertEqual(backup["next_run_at"], NOW + timedelta(seconds=600))
            # Idle first, then the exact backup interval (lane-busy passes
            # add their own indefinite waits in between).
            self.assertIsNone(waiter.calls[0])
            self.assertEqual(
                [c for c in waiter.calls if c is not None], [300.0, 300.0])
        finally:
            await kernel.stop()

    async def test_future_attempt_deadline_drives_a_miss_without_timer(self) -> None:
        """A future deadline fires a check-only cycle; the watch never arms."""
        self.repo.tasks["running"] = _ticket(
            "running", status=TaskStatus.IN_PROGRESS.value, project="other",
            attempt_id="running-g0-w0",
            attempt_deadline_at=NOW + timedelta(seconds=60))
        kernel, _agenda, waiter = self._wired_kernel(budget=1)
        try:
            await kernel.start()
            await asyncio.sleep(0.2)
            self.assertEqual(len(self.recorded), 1)
            self.assertEqual(self.recorded[0]["deadline_type"], "attempt")
            self.assertFalse(self.watch.has_timer)
            # The wait targets the exact due, never a rounded cap.
            self.assertEqual(waiter.calls[0], 60.0)
            for timeout in waiter.calls[1:]:
                self.assertIsNone(timeout)
        finally:
            await kernel.stop()

    async def test_overdue_at_boot_records_once_without_spinning(self) -> None:
        """Overdue deadlines piggyback on the boot wake, then stay quiet."""
        self.repo.tasks["hung"] = _ticket(
            "hung", status=TaskStatus.IN_PROGRESS.value, project="other",
            attempt_id="hung-g0-w0",
            attempt_deadline_at=NOW - timedelta(seconds=1))
        kernel, _agenda, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.recorded, [])
            kernel.wake("dispatcher")
            await asyncio.sleep(0.2)
            self.assertEqual(len(self.recorded), 1)
            for timeout in waiter.calls:
                self.assertIsNone(timeout)
        finally:
            await kernel.stop()

    async def test_attempt_registration_wakes_through_notify(self) -> None:
        """``mark_attempt_start(notify=dispatcher.wake)`` drives a cycle."""
        self.repo.tasks["t-run"] = _ticket(
            "t-run", status=TaskStatus.IN_PROGRESS.value, project="other")
        self.repo.tasks["queued-1"] = _ticket("queued-1", project="free")
        kernel, _agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.launched, [])
            wrote = await deadline_mod.mark_attempt_start(
                self.repo.patch_task, task_id="t-run",
                attempt_id="t-run-g0-w0", timeout_seconds=600,
                started_at=self.clock.now, notify=self.dispatcher.wake)
            self.assertIsNotNone(wrote)
            await asyncio.sleep(0.15)
            self.assertEqual(self.launched, ["queued-1"])
        finally:
            await kernel.stop()
