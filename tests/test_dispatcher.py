"""Dispatcher tests (TASK-2026-00113, F3): admission, race, backup, restart.

Zero tokens: fake repo/service/rate-monitor plus a simulated executor. The
reserve matrix runs against the real ``RateMonitor`` with stubbed provider
status (no tmux).
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

# task_service pulls db.mongo (motor/pymongo): stub them like the retired
# queue tests did. setdefault: other suites may provide the real ones.
motor_module = types.ModuleType("motor")
motor_asyncio_module = types.ModuleType("motor.motor_asyncio")
pymongo_module = types.ModuleType("pymongo")


class _DummyAsyncIOMotorClient:
    pass


motor_asyncio_module.AsyncIOMotorClient = _DummyAsyncIOMotorClient
pymongo_module.ASCENDING = 1
pymongo_module.DESCENDING = -1
pymongo_module.ReturnDocument = object()
sys.modules.setdefault("motor", motor_module)
sys.modules.setdefault("motor.motor_asyncio", motor_asyncio_module)
sys.modules.setdefault("pymongo", pymongo_module)

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from dispatcher import Dispatcher  # noqa: E402
from rate_monitor import ProviderStatus, RateMonitor  # noqa: E402
from reliability import deadlines as deadline_mod  # noqa: E402
from state import (  # noqa: E402
    WAIT_BLOCKED_BY,
    WAIT_LIMIT_GLOBAL,
    WAIT_LIMIT_OBJECTIVE,
    WAIT_LIMIT_PROJECT,
    WAIT_QUOTA,
    WAIT_QUOTA_RESERVED,
    WAIT_REPO_BUSY,
    TaskReviewAction,
    TaskReviewActorType,
    TaskStatus,
)
from task_service import TaskService  # noqa: E402

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _ticket(task_id: str, **overrides: object) -> dict:
    doc = {
        "task_id": task_id,
        "status": TaskStatus.QUEUED.value,
        "project": "proj",
        "priority": "medium",
        "created_at": _NOW - timedelta(hours=1),
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
    """In-memory tasks + backup doc. Claim is atomic via a lock."""

    def __init__(self, tasks: list[dict] | None = None) -> None:
        self.tasks: dict[str, dict] = {t["task_id"]: dict(t) for t in (tasks or [])}
        self.backup: dict | None = None
        self.locks: dict[str, dict] = {}
        self.reviews: list[dict] = []
        self._claim_lock = asyncio.Lock()

    async def list_tasks_by_status(self, status: TaskStatus, limit: int = 200) -> list[dict]:
        return [dict(t) for t in self.tasks.values() if t.get("status") == status.value][:limit]

    async def due_waiting_tasks(self, now_utc: datetime) -> list[dict]:
        out = []
        for t in self.tasks.values():
            if t.get("status") != TaskStatus.WAITING.value:
                continue
            resume_at = t.get("resume_at")
            if not isinstance(resume_at, datetime):
                # BSON `$lte` never matches a non-datetime: mirror that.
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
                "started_at": _NOW,
                "waiting_reason": None,
                "resume_at": None,
            })
            return dict(row)

    async def get_dispatcher_backup(self) -> dict | None:
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

    async def clear_quota_park_resumes(self) -> int:
        cleared = 0
        for row in self.tasks.values():
            if (
                row.get("status") == TaskStatus.QUEUED.value
                and row.get("waiting_reason") in {WAIT_QUOTA, WAIT_QUOTA_RESERVED}
                and row.get("resume_at") is not None
            ):
                row["resume_at"] = None
                cleared += 1
        return cleared

    async def list_tasks_blocked_by(self, task_id: str) -> list[dict]:
        return [
            dict(t) for t in self.tasks.values()
            if task_id in (t.get("blocked_by") or [])
        ]

    async def create_task_review(self, doc: dict) -> dict:
        stored = dict(doc)
        self.reviews.append(stored)
        return stored


class _FakeHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event: str, payload: dict) -> None:
        self.events.append((event, dict(payload)))


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

    async def run_task(self, task_id: str) -> dict | None:  # pragma: no cover
        raise AssertionError("use the injected simulated launcher")


class _FakeMonitor:
    def __init__(self, verdict: tuple[bool, datetime | None, str | None] | None = None) -> None:
        self.verdict = verdict or (True, None, None)
        self.calls: list[str] = []

    async def admission_for_ticket(
        self, ticket: dict, active_tickets: list[dict] | None = None
    ) -> tuple[bool, datetime | None, str | None]:
        self.calls.append(str(ticket.get("task_id")))
        return self.verdict


async def _drain() -> None:
    """Let fire-and-forget launchers run before asserting side effects."""
    await asyncio.sleep(0)


def _dispatcher(
    repo: _FakeRepo,
    service: _FakeService,
    launched: list[str],
    monitor: _FakeMonitor | None = None,
    settings: SimpleNamespace | None = None,
    repo_for_project=None,
) -> Dispatcher:
    async def launcher(task_id: str) -> None:
        launched.append(task_id)

    return Dispatcher(
        repo=repo,
        settings=settings or _settings(),
        service=service,
        rate_monitor=monitor if monitor is not None else _FakeMonitor(),
        launcher=launcher,
        repo_for_project=repo_for_project,
    )


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_active_per_project_by_default(self) -> None:
        repo = _FakeRepo([
            _ticket("active-1", status=TaskStatus.IN_PROGRESS.value, project="proj"),
            _ticket("queued-1", project="proj"),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 1)
        row = await repo.get_task("queued-1")
        assert row is not None
        self.assertEqual(row["waiting_reason"], WAIT_LIMIT_PROJECT)
        # Event-held: no resume_at (a finish wakes the cycle in seconds).
        self.assertIsNone(row["resume_at"])

    async def test_per_objective_limit(self) -> None:
        tasks = [
            _ticket(f"active-{i}", status=TaskStatus.IN_PROGRESS.value,
                    project=f"proj-{i}", objective_id="OBJ-1")
            for i in range(6)
        ]
        tasks.append(_ticket("queued-1", project="proj-x", objective_id="OBJ-1"))
        repo = _FakeRepo(tasks)
        service = _FakeService(repo)
        launched: list[str] = []
        settings = _settings(DISPATCHER_MAX_ACTIVE_PER_PROJECT=10)
        out = await _dispatcher(repo, service, launched, settings=settings).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 1)
        row = await repo.get_task("queued-1")
        assert row is not None
        self.assertEqual(row["waiting_reason"], WAIT_LIMIT_OBJECTIVE)

    async def test_global_slots_come_from_rate_admission(self) -> None:
        repo = _FakeRepo([_ticket("queued-1", project="free")])
        service = _FakeService(repo)
        launched: list[str] = []
        monitor = _FakeMonitor((False, None, WAIT_LIMIT_GLOBAL))
        out = await _dispatcher(repo, service, launched, monitor=monitor).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 1)
        row = await repo.get_task("queued-1")
        assert row is not None
        self.assertEqual(row["waiting_reason"], WAIT_LIMIT_GLOBAL)

    async def test_priority_then_age_order(self) -> None:
        repo = _FakeRepo([
            _ticket("med-old", priority="medium",
                    created_at=_NOW - timedelta(hours=3), project="p1"),
            _ticket("crit-new", priority="critical",
                    created_at=_NOW - timedelta(minutes=1), project="p2"),
            _ticket("med-new", priority="medium",
                    created_at=_NOW - timedelta(minutes=1), project="p3"),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        settings = _settings(DISPATCHER_MAX_ACTIVE_PER_PROJECT=10)
        monitor = _FakeMonitor()
        dispatcher = _dispatcher(repo, service, launched, monitor=monitor, settings=settings)
        # Only one slot per cycle: shrink the pool by pre-claiming via a
        # monitor that admits once, then parks.
        admissions = {"left": 1}

        async def one_shot(ticket: dict, active_tickets=None):  # type: ignore[no-untyped-def]
            if admissions["left"] > 0:
                admissions["left"] -= 1
                return True, None, None
            return False, None, WAIT_LIMIT_GLOBAL

        monitor.admission_for_ticket = one_shot  # type: ignore[method-assign]
        await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, ["crit-new"])
        # Next cycle: same priority → oldest first.
        admissions["left"] = 1
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["launched"], ["med-old"])

    async def test_blocked_by_parks_with_reason_and_no_resume(self) -> None:
        repo = _FakeRepo([
            _ticket("blocker", status=TaskStatus.IN_PROGRESS.value, project="other"),
            _ticket("queued-1", project="free", blocked_by=["blocker"]),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 1)
        row = await repo.get_task("queued-1")
        assert row is not None
        self.assertEqual(row["waiting_reason"], f"{WAIT_BLOCKED_BY}: blocker")
        self.assertIsNone(row["resume_at"])

    async def test_explicit_autorun_false_is_silently_skipped(self) -> None:
        repo = _FakeRepo([_ticket("queued-1", project="free", autorun=False)])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 0)
        self.assertEqual(service.patches, [])
        self.assertIsNone(out["backup_at"])
        self.assertIsNone(await repo.get_dispatcher_backup())

    async def test_future_resume_skips_silently_past_resume_launches(self) -> None:
        repo = _FakeRepo([
            _ticket("future", project="p1", waiting_reason=WAIT_QUOTA,
                    resume_at=_NOW + timedelta(minutes=10)),
            _ticket("past", project="p2", waiting_reason=WAIT_QUOTA,
                    resume_at=_NOW - timedelta(minutes=1)),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, ["past"])
        self.assertEqual(out["parked"], 0)
        row = await repo.get_task("past")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.IN_PROGRESS.value)

    async def test_naive_datetimes_from_mongo_do_not_crash(self) -> None:
        naive = datetime(2026, 10, 7, 11, 0, 0)  # no tzinfo, legacy row
        repo = _FakeRepo([
            _ticket("queued-1", project="free", created_at=naive, resume_at=naive),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, ["queued-1"])
        self.assertEqual(out["parked"], 0)

    async def test_iso_string_resumes_honored_garbage_holds(self) -> None:
        # TASK-2026-00439: Tier S parks store resume_at as ISO strings.
        repo = _FakeRepo([
            _ticket("future", project="p1", waiting_reason=WAIT_QUOTA,
                    resume_at=(_NOW + timedelta(minutes=10)).isoformat()),
            _ticket("past", project="p2", waiting_reason=WAIT_QUOTA,
                    resume_at=(_NOW - timedelta(minutes=1)).isoformat()),
            _ticket("past-z", project="p3", waiting_reason=WAIT_QUOTA,
                    resume_at=(_NOW - timedelta(minutes=2)).strftime(
                        "%Y-%m-%dT%H:%M:%SZ")),
            _ticket("garbage", project="p4", waiting_reason=WAIT_QUOTA,
                    resume_at="not-a-datetime"),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, ["past", "past-z"])
        self.assertEqual(out["parked"], 0)

    async def test_repo_lock_held_parks_with_repo_busy(self) -> None:
        repo = _FakeRepo([_ticket("queued-1", project="proj")])
        repo.locks["/repo/proj"] = {"holder": "other-task",
                                    "expires_at": _NOW + timedelta(hours=1)}
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched,
                                 repo_for_project=lambda _p: "/repo/proj")
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 1)
        row = await repo.get_task("queued-1")
        assert row is not None
        self.assertEqual(row["waiting_reason"], f"{WAIT_REPO_BUSY}: /repo/proj")

    async def test_repo_lock_own_or_expired_does_not_block(self) -> None:
        for holder, expires in [
            ("queued-1", _NOW + timedelta(hours=1)),  # own stale lock
            ("other", _NOW - timedelta(minutes=1)),  # expired lock
        ]:
            with self.subTest(holder=holder):
                repo = _FakeRepo([_ticket("queued-1", project="proj")])
                repo.locks["/repo/proj"] = {"holder": holder, "expires_at": expires}
                service = _FakeService(repo)
                launched: list[str] = []
                dispatcher = _dispatcher(repo, service, launched,
                                         repo_for_project=lambda _p: "/repo/proj")
                out = await dispatcher.dispatch_cycle(now=_NOW)
                await _drain()
                self.assertEqual(launched, ["queued-1"])
                self.assertEqual(out["parked"], 0)

    async def test_park_writes_only_on_change(self) -> None:
        repo = _FakeRepo([
            _ticket("active-1", status=TaskStatus.IN_PROGRESS.value, project="proj"),
            _ticket("queued-1", project="proj"),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched)
        await dispatcher.dispatch_cycle(now=_NOW)
        first_patches = len(service.patches)
        self.assertGreater(first_patches, 0)
        await dispatcher.dispatch_cycle(now=_NOW)
        self.assertEqual(len(service.patches), first_patches)

    async def test_due_waiting_requeues_and_blocked_stays(self) -> None:
        repo = _FakeRepo([
            _ticket("due-1", status=TaskStatus.WAITING.value, project="p1",
                    waiting_reason=WAIT_QUOTA, resume_at=_NOW - timedelta(seconds=1)),
            _ticket("blocker", status=TaskStatus.IN_PROGRESS.value, project="other"),
            _ticket("due-blocked", status=TaskStatus.WAITING.value, project="p2",
                    waiting_reason=WAIT_QUOTA, resume_at=_NOW - timedelta(seconds=1),
                    blocked_by=["blocker"]),
            _ticket("held", status=TaskStatus.WAITING.value, project="p3",
                    waiting_reason=WAIT_QUOTA, resume_at=_NOW - timedelta(seconds=1),
                    autorun=False),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["requeued"], 1)
        self.assertIn("due-1", launched)
        row = await repo.get_task("due-blocked")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.WAITING.value)
        row = await repo.get_task("held")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.WAITING.value)


class ClaimRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_cycles_launch_once(self) -> None:
        repo = _FakeRepo([_ticket("queued-1", project="free")])
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched)
        results = await asyncio.gather(
            dispatcher.dispatch_cycle(now=_NOW),
            dispatcher.dispatch_cycle(now=_NOW),
        )
        await _drain()
        self.assertEqual(launched, ["queued-1"])
        total = sum(len(r["launched"]) for r in results)
        self.assertEqual(total, 1)


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_queue_has_no_backup(self) -> None:
        repo = _FakeRepo()
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertIsNone(out["backup_at"])
        self.assertIsNone(await repo.get_dispatcher_backup())

    async def test_backup_appears_while_limited_and_clears_when_drained(self) -> None:
        repo = _FakeRepo([
            _ticket("active-1", status=TaskStatus.IN_PROGRESS.value, project="proj"),
            _ticket("queued-1", project="proj"),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched)
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        assert out["backup_at"] is not None
        # Event-held park, no resume: the backup fires at the full interval.
        self.assertEqual(out["backup_at"], _NOW + timedelta(seconds=300))
        backup = await repo.get_dispatcher_backup()
        assert backup is not None
        self.assertEqual(backup["next_run_at"], _NOW + timedelta(seconds=300))
        # Slot frees: the ticket launches and the backup disappears.
        await repo.patch_task("active-1", {"status": TaskStatus.COMPLETED.value})
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["launched"], ["queued-1"])
        self.assertIsNone(out["backup_at"])
        self.assertIsNone(await repo.get_dispatcher_backup())

    async def test_backup_fires_at_earliest_quota_reset(self) -> None:
        reset_at = _NOW + timedelta(minutes=1)
        repo = _FakeRepo([_ticket("queued-1", project="free", waiting_reason=WAIT_QUOTA,
                                  resume_at=reset_at + timedelta(minutes=2))])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["backup_at"], reset_at + timedelta(minutes=2))

    async def test_waiting_with_future_resume_holds_backup(self) -> None:
        repo = _FakeRepo([
            _ticket("wait-1", status=TaskStatus.WAITING.value, project="p1",
                    waiting_reason=WAIT_QUOTA, resume_at=_NOW + timedelta(minutes=10)),
            _ticket("human", status=TaskStatus.WAITING.value, project="p2",
                    waiting_reason="phase:qa_review", resume_at=None),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        # min(interval, earliest holder): 12:05 wins over the 12:10 resume.
        self.assertEqual(out["backup_at"], _NOW + timedelta(seconds=300))
        # Only the future-resume waiter holds it: expire it and the human
        # wait alone must not keep a timer.
        await repo.patch_task("wait-1", {"status": TaskStatus.COMPLETED.value})
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertIsNone(out["backup_at"])
        self.assertIsNone(await repo.get_dispatcher_backup())


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_reconciles_ready_tickets(self) -> None:
        repo = _FakeRepo([_ticket("queued-1", project="free")])
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched)
        try:
            await dispatcher.start()
            await _drain()
            self.assertEqual(launched, ["queued-1"])
        finally:
            await dispatcher.stop()

    async def test_wake_dispatches_without_a_timer(self) -> None:
        repo = _FakeRepo()
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched)
        try:
            await dispatcher.start()
            self.assertIsNone(await repo.get_dispatcher_backup())
            repo.tasks["queued-1"] = _ticket("queued-1", project="free")
            dispatcher.wake()

            async def _wait_for_launch() -> None:
                while not launched:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(_wait_for_launch(), timeout=5)
            self.assertEqual(launched, ["queued-1"])
        finally:
            await dispatcher.stop()


class SimulatedExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_with_simulated_executor(self) -> None:
        """Two tickets, one slot: launch → finish → next, backup clears."""
        repo = _FakeRepo([
            _ticket("first", project="proj"),
            _ticket("second", project="proj"),
        ])
        service = _FakeService(repo)
        finished: list[str] = []

        async def simulated_run(task_id: str) -> None:
            finished.append(task_id)
            await repo.patch_task(task_id, {"status": TaskStatus.COMPLETED.value})

        dispatcher = Dispatcher(
            repo=repo, settings=_settings(), service=service,
            rate_monitor=_FakeMonitor(), launcher=simulated_run,
        )
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["launched"], ["first"])
        row = await repo.get_task("second")
        assert row is not None
        self.assertEqual(row["waiting_reason"], WAIT_LIMIT_PROJECT)
        assert out["backup_at"] is not None
        # The finish wakes the dispatcher; give the done-callback a tick.
        await asyncio.sleep(0)
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["launched"], ["second"])
        self.assertEqual(finished, ["first", "second"])
        self.assertIsNone(out["backup_at"])


def _rate_settings(**overrides: object) -> SimpleNamespace:
    cfg = {
        "RATE_MAX_CONCURRENT_TICKETS": 3,
        "RATE_TIER_S_WEIGHT": 3,
        "RATE_NORMAL_WEIGHT": 1,
        "RATE_CODEX_RESERVE_5H": 0.0,
        "RATE_CODEX_RESERVE_WEEKLY": 0.0,
        "RATE_CLAUDE_RESERVE_5H": 0.0,
        "RATE_CLAUDE_RESERVE_WEEKLY": 0.0,
        "RATE_MUSE_RESERVE_5H": 0.0,
        "RATE_MUSE_RESERVE_WEEKLY": 0.0,
        "RATE_STATUS_STALE_SECONDS": 300,
    }
    cfg.update(overrides)
    return SimpleNamespace(**cfg)


def _provider_status(provider: str, five_h: float, weekly: float) -> ProviderStatus:
    return ProviderStatus(
        provider=provider,
        remaining_5h=five_h,
        remaining_weekly=weekly,
        resets_5h_at=_NOW + timedelta(hours=1),
        resets_weekly_at=_NOW + timedelta(days=1),
        updated_at=_NOW,
    )


class ReserveTests(unittest.IsolatedAsyncioTestCase):
    def _monitor(self, levels: dict[str, tuple[float, float]], **reserves: object) -> RateMonitor:
        monitor = RateMonitor(_rate_settings(**reserves))

        async def fake_get_status(provider: str) -> ProviderStatus:
            five_h, weekly = levels.get(provider, (1.0, 1.0))
            return _provider_status(provider, five_h, weekly)

        monitor.get_status = fake_get_status  # type: ignore[method-assign]
        return monitor

    async def test_tier_b_invading_reserve_waits_quota_reserved(self) -> None:
        monitor = self._monitor(
            {"codex": (0.1, 0.9)},
            RATE_CODEX_RESERVE_5H=0.2, RATE_CODEX_RESERVE_WEEKLY=0.2,
        )
        can, resume_at, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_b"), active_tickets=[])
        self.assertFalse(can)
        self.assertEqual(reason, WAIT_QUOTA_RESERVED)
        assert resume_at is not None
        self.assertEqual(resume_at, _NOW + timedelta(hours=1, minutes=2))

    async def test_tier_s_consumes_into_the_reserve(self) -> None:
        monitor = self._monitor(
            {"claude": (0.1, 0.9), "codex": (0.1, 0.9), "muse": (0.1, 0.9)},
            RATE_CODEX_RESERVE_5H=0.2, RATE_CODEX_RESERVE_WEEKLY=0.2,
            RATE_CLAUDE_RESERVE_5H=0.2, RATE_CLAUDE_RESERVE_WEEKLY=0.2,
            RATE_MUSE_RESERVE_5H=0.2, RATE_MUSE_RESERVE_WEEKLY=0.2,
        )
        can, resume_at, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_s"), active_tickets=[])
        self.assertTrue(can)
        self.assertIsNone(resume_at)
        self.assertIsNone(reason)

    async def test_empty_quota_is_a_quota_wait_not_reserved(self) -> None:
        monitor = self._monitor(
            {"codex": (0.0, 0.9)},
            RATE_CODEX_RESERVE_5H=0.2, RATE_CODEX_RESERVE_WEEKLY=0.2,
        )
        can, _, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_b"), active_tickets=[])
        self.assertFalse(can)
        self.assertEqual(reason, WAIT_QUOTA)

    async def test_tier_s_blocked_only_at_zero(self) -> None:
        monitor = self._monitor({"claude": (0.0, 0.9)})
        can, _, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_s"), active_tickets=[])
        self.assertFalse(can)
        self.assertEqual(reason, WAIT_QUOTA)

    async def test_configured_codex_reserve_holds_tier_b(self) -> None:
        # Configured values (TASK-2026-00148, policy §28): Codex 0.20/0.05.
        monitor = self._monitor(
            {"codex": (0.15, 0.9)},
            RATE_CODEX_RESERVE_5H=0.20, RATE_CODEX_RESERVE_WEEKLY=0.05,
        )
        can, _, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_b"), active_tickets=[])
        self.assertFalse(can)
        self.assertEqual(reason, WAIT_QUOTA_RESERVED)

    async def test_configured_claude_reserve_holds_tier_a(self) -> None:
        # Configured values (TASK-2026-00148, policy §28): Claude 0.20/0.05.
        monitor = self._monitor(
            {"claude": (0.15, 0.9)},
            RATE_CLAUDE_RESERVE_5H=0.20, RATE_CLAUDE_RESERVE_WEEKLY=0.05,
        )
        can, _, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_a"), active_tickets=[])
        self.assertFalse(can)
        self.assertEqual(reason, WAIT_QUOTA_RESERVED)

    async def test_configured_reserves_let_tier_s_through(self) -> None:
        monitor = self._monitor(
            {"claude": (0.15, 0.9), "codex": (0.15, 0.9),
             "muse": (0.15, 0.9)},
            RATE_CODEX_RESERVE_5H=0.20, RATE_CODEX_RESERVE_WEEKLY=0.05,
            RATE_CLAUDE_RESERVE_5H=0.20, RATE_CLAUDE_RESERVE_WEEKLY=0.05,
        )
        can, resume_at, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_s"), active_tickets=[])
        self.assertTrue(can)
        self.assertIsNone(resume_at)
        self.assertIsNone(reason)

    async def test_configured_weekly_reserve_binds(self) -> None:
        monitor = self._monitor(
            {"codex": (0.9, 0.04)},
            RATE_CODEX_RESERVE_5H=0.20, RATE_CODEX_RESERVE_WEEKLY=0.05,
        )
        can, _, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_b"), active_tickets=[])
        self.assertFalse(can)
        self.assertEqual(reason, WAIT_QUOTA_RESERVED)

    async def test_configured_reserves_never_bind_healthy_quota(self) -> None:
        monitor = self._monitor(
            {"claude": (0.9, 0.9), "codex": (0.9, 0.9)},
            RATE_CODEX_RESERVE_5H=0.20, RATE_CODEX_RESERVE_WEEKLY=0.05,
            RATE_CLAUDE_RESERVE_5H=0.20, RATE_CLAUDE_RESERVE_WEEKLY=0.05,
        )
        for tier in ("tier_a", "tier_b"):
            with self.subTest(tier=tier):
                can, _, reason = await monitor.admission_for_ticket(
                    _ticket("t", coding_tool=tier), active_tickets=[])
                self.assertTrue(can)
                self.assertIsNone(reason)

    async def test_tier_c_always_launches(self) -> None:
        monitor = self._monitor({"codex": (0.0, 0.0), "claude": (0.0, 0.0)})
        can, resume_at, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_c"), active_tickets=[])
        self.assertTrue(can)
        self.assertIsNone(resume_at)
        self.assertIsNone(reason)

    async def test_crowded_slots_park_with_limit_global(self) -> None:
        monitor = self._monitor({})
        active = [_ticket("s", coding_tool="tier_s", status=TaskStatus.IN_PROGRESS.value)]
        can, resume_at, reason = await monitor.admission_for_ticket(
            _ticket("t", coding_tool="tier_b"), active_tickets=active)
        self.assertFalse(can)
        self.assertEqual(reason, WAIT_LIMIT_GLOBAL)
        self.assertIsNone(resume_at)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_objective_child_without_tier_parks_routing_missing(self) -> None:
        repo = _FakeRepo([
            _ticket("child-1", project="free", objective_id="OBJ-1", coding_tool=None),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        monitor = _FakeMonitor()
        out = await _dispatcher(repo, service, launched, monitor=monitor).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 1)
        self.assertEqual(monitor.calls, [])
        row = await repo.get_task("child-1")
        assert row is not None
        self.assertTrue(str(row["waiting_reason"]).startswith("routing_missing:"))
        self.assertIsNone(row["resume_at"])

    async def test_loose_ticket_launches_tier_unaware(self) -> None:
        # Deliberate contract change (TASK-2026-00204): no keyword peek.
        # A loose ticket without a tier launches undecided whatever its
        # text says; the run pre-stage plans the pedido.
        repo = _FakeRepo([
            _ticket("loose-1", project="free", coding_tool=None,
                    title="Cambiar el margen del panel; no toca pagos ni login",
                    description="Per routing policy §19, S examples include "
                    "authentication and payments."),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        seen: dict = {}

        async def capture(ticket: dict, active_tickets=None):  # type: ignore[no-untyped-def]
            seen.update(ticket)
            return True, None, None

        monitor = _FakeMonitor()
        monitor.admission_for_ticket = capture  # type: ignore[method-assign]
        out = await _dispatcher(repo, service, launched, monitor=monitor).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, ["loose-1"])
        self.assertEqual(out["parked"], 0)
        self.assertIsNone(seen.get("coding_tool"))
        row = await repo.get_task("loose-1")
        assert row is not None
        self.assertIsNone(row.get("coding_tool"))

    async def test_tier_s_skips_the_repo_peek(self) -> None:
        repo = _FakeRepo([_ticket("s-1", project="proj", coding_tool="tier_s")])
        repo.locks["/repo/proj"] = {"holder": "other-task",
                                    "expires_at": _NOW + timedelta(hours=1)}
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched,
                                 repo_for_project=lambda _p: "/repo/proj")
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        # The Tier S cycle owns its locking (transient backoff on
        # contention): the dispatcher must not park it on the outer lock.
        self.assertEqual(launched, ["s-1"])
        self.assertEqual(out["parked"], 0)


class PendingLinkTests(unittest.IsolatedAsyncioTestCase):
    def _pending(self, mirror: dict) -> dict:
        return _ticket("queued-1", project="free",
                       waiting_reason="jardal_erp_pending",
                       params={"_mirrors": {"erpnext": mirror}})

    async def test_pending_mirror_skips_but_holds_backup(self) -> None:
        repo = _FakeRepo([self._pending({"name": None, "ref": "r1", "pending": True})])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 0)
        self.assertEqual(service.patches, [])
        self.assertEqual(out["backup_at"], _NOW + timedelta(seconds=300))

    async def test_nameless_mirror_with_ref_skips(self) -> None:
        repo = _FakeRepo([self._pending({"name": None, "ref": "r1"})])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["parked"], 0)

    async def test_healthy_or_missing_mirror_launches(self) -> None:
        for params in [
            {"_mirrors": {"erpnext": {"name": "TASK-1", "ref": "r1", "pending": False}}},
            {},
        ]:
            with self.subTest(params=params):
                repo = _FakeRepo([_ticket("queued-1", project="free", params=params)])
                service = _FakeService(repo)
                launched: list[str] = []
                out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
                await _drain()
                self.assertEqual(launched, ["queued-1"])
                self.assertEqual(out["parked"], 0)


class GarbageDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_garbage_resume_holds_instead_of_launching(self) -> None:
        repo = _FakeRepo([_ticket("queued-1", project="free", resume_at="not-a-date")])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, [])
        self.assertEqual(out["backup_at"], _NOW + timedelta(seconds=300))

    async def test_garbage_waiting_resume_is_not_due(self) -> None:
        repo = _FakeRepo([
            _ticket("wait-1", status=TaskStatus.WAITING.value, project="p1",
                    waiting_reason=WAIT_QUOTA, resume_at="not-a-date"),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        out = await _dispatcher(repo, service, launched).dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["requeued"], 0)
        self.assertEqual(launched, [])
        row = await repo.get_task("wait-1")
        assert row is not None
        self.assertEqual(row["status"], TaskStatus.WAITING.value)


def _bare_service(repo: _FakeRepo, hub: _FakeHub, wakes: list[int]) -> TaskService:
    service = TaskService.__new__(TaskService)
    service._repo = repo  # type: ignore[attr-defined]
    service._hub = hub  # type: ignore[attr-defined]
    service._workflow_service = None  # type: ignore[attr-defined]
    service._notion = None  # type: ignore[attr-defined]
    service._qa_service = None  # type: ignore[attr-defined]
    service._dispatcher_wake = lambda: wakes.append(1)  # type: ignore[attr-defined]
    return service


class _FakeQuotaMonitor:
    def __init__(self) -> None:
        self.refreshed: list[str] = []

    async def on_ticket_completed(self, ticket: dict) -> None:
        self.refreshed.append(str(ticket.get("task_id")))


class ServiceWakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reject_requeues_and_wakes(self) -> None:
        task = _ticket(
            "phase-1", project="free", status=TaskStatus.WAITING.value,
            phase="qa_review", phase_index=1,
            workflow_phases=[
                {"key": "dev_work", "type": "agent", "agent": "dev"},
                {"key": "qa_review", "type": "human"},
            ],
        )
        repo = _FakeRepo([task])
        hub = _FakeHub()
        wakes: list[int] = []
        service = _bare_service(repo, hub, wakes)
        out = await service.create_phase_action(
            "phase-1", actor_id="owner", actor_type=TaskReviewActorType.INTERNAL,
            action=TaskReviewAction.REJECT, comment="redo",
        )
        self.assertEqual(out["task"]["status"], TaskStatus.QUEUED.value)
        self.assertEqual(wakes, [1])

    async def test_on_ticket_done_clears_quota_parks_and_wakes(self) -> None:
        repo = _FakeRepo([
            _ticket("stale", project="free", waiting_reason=WAIT_QUOTA,
                    resume_at=_NOW + timedelta(hours=2)),
        ])
        hub = _FakeHub()
        wakes: list[int] = []
        service = _bare_service(repo, hub, wakes)
        monitor = _FakeQuotaMonitor()
        service._rate_monitor = monitor  # type: ignore[attr-defined]
        await service._on_ticket_done({"task_id": "done-1", "status": "completed"})
        self.assertEqual(monitor.refreshed, ["done-1"])
        row = await repo.get_task("stale")
        assert row is not None
        self.assertIsNone(row["resume_at"])
        self.assertEqual(row["waiting_reason"], WAIT_QUOTA)
        self.assertEqual(wakes, [1])

    async def test_approve_to_agent_requeues_and_wakes(self) -> None:
        task = _ticket(
            "phase-1", project="free", status=TaskStatus.WAITING.value,
            phase="q1", phase_index=0,
            workflow_phases=[
                {"key": "q1", "type": "human"},
                {"key": "dev_work", "type": "agent", "agent": "dev"},
                {"key": "q2", "type": "human"},
            ],
        )
        repo = _FakeRepo([task])
        hub = _FakeHub()
        wakes: list[int] = []
        service = _bare_service(repo, hub, wakes)
        out = await service.create_phase_action(
            "phase-1", actor_id="owner", actor_type=TaskReviewActorType.INTERNAL,
            action=TaskReviewAction.APPROVE, comment="go",
        )
        self.assertEqual(out["task"]["status"], TaskStatus.QUEUED.value)
        self.assertEqual(wakes, [1])

    async def test_revert_to_human_does_not_wake(self) -> None:
        task = _ticket(
            "phase-1", project="free", status=TaskStatus.WAITING.value,
            phase="q2", phase_index=1,
            workflow_phases=[
                {"key": "q1", "type": "human"},
                {"key": "q2", "type": "human"},
            ],
        )
        repo = _FakeRepo([task])
        hub = _FakeHub()
        wakes: list[int] = []
        service = _bare_service(repo, hub, wakes)
        out = await service.revert_phase_approval(
            "phase-1", actor_id="owner", actor_type=TaskReviewActorType.INTERNAL,
        )
        self.assertEqual(out["task"]["status"], TaskStatus.WAITING.value)
        self.assertEqual(wakes, [])

    async def test_apply_workflow_reset_to_agent_wakes(self) -> None:
        task = _ticket("phase-1", project="free", status=TaskStatus.WAITING.value)
        repo = _FakeRepo([task])
        hub = _FakeHub()
        wakes: list[int] = []
        service = _bare_service(repo, hub, wakes)
        patched = await service.apply_phase_workflow_to_task(
            "phase-1",
            workflow_phases=[{"key": "dev_work", "type": "agent", "agent": "dev"}],
            reset_phase=True,
        )
        assert patched is not None
        self.assertEqual(patched["status"], TaskStatus.QUEUED.value)
        self.assertEqual(wakes, [1])

    async def test_autorun_flip_wakes_but_nothing_else_does(self) -> None:
        repo = _FakeRepo([
            _ticket("paused", project="free", autorun=False),
            _ticket("other", project="free"),
        ])
        hub = _FakeHub()
        wakes: list[int] = []
        service = _bare_service(repo, hub, wakes)
        await service.patch_task("paused", {"autorun": True})
        self.assertEqual(wakes, [1])
        # No self-wake loop: dispatcher-style writes stay silent.
        await service.patch_task("other", {"waiting_reason": WAIT_QUOTA})
        await service.patch_task("other", {"autorun": True})
        self.assertEqual(wakes, [1])


class _FlakyRepo(_FakeRepo):
    def __init__(self, tasks: list[dict] | None = None) -> None:
        super().__init__(tasks)
        self.attempts = 0

    async def list_tasks_by_status(self, status: TaskStatus, limit: int = 200) -> list[dict]:
        self.attempts += 1
        raise RuntimeError("transient outage")


class StartGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_survives_a_failed_reconcile(self) -> None:
        repo = _FlakyRepo()
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched)
        try:
            await dispatcher.start()
            self.assertGreater(repo.attempts, 0)
            job = dispatcher._job
            assert job is not None
            self.assertFalse(job.done())
        finally:
            await dispatcher.stop()


class _FakeIncidents:
    """Dict-backed incidents collection for the real capture path."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def count_documents(self, filt: dict) -> int:
        total = 0
        for doc in self.docs.values():
            if filt.get("fingerprint") is not None \
                    and doc.get("fingerprint") != filt["fingerprint"]:
                continue
            gte = (filt.get("created_at") or {}).get("$gte")
            if gte is not None and doc.get("created_at") < gte:
                continue
            total += 1
        return total


def _recorder(recorded: list[dict]):
    async def _record(**fields: object) -> str:
        recorded.append(dict(fields))
        return f"incident-{len(recorded)}"
    return _record


def _watch(repo: _FakeRepo, recorded: list[dict],
           now: datetime = _NOW) -> deadline_mod.DeadlineWatch:
    return deadline_mod.DeadlineWatch(
        repo, record=_recorder(recorded), clock=lambda: now)


class DeadlineMathTests(unittest.IsolatedAsyncioTestCase):
    async def test_attempt_deadline_adds_timeout_plus_grace(self) -> None:
        start = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            deadline_mod.attempt_deadline_for(start, 1800),
            start + timedelta(seconds=1800 + 360))

    async def test_attempt_deadline_obeys_grace_floor(self) -> None:
        start = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            deadline_mod.attempt_deadline_for(start, 60),
            start + timedelta(seconds=60 + 300))

    async def test_resume_deadline_adds_ten_minutes(self) -> None:
        resume_at = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            deadline_mod.resume_deadline_for(resume_at),
            resume_at + timedelta(seconds=600))

    async def test_mark_and_clear_roundtrip(self) -> None:
        patches: list[tuple[str, dict]] = []

        async def patch(task_id: str, updates: dict) -> None:
            patches.append((task_id, dict(updates)))

        wrote = await deadline_mod.mark_attempt_start(
            patch, task_id="t1", attempt_id="t1-g0-w0", timeout_seconds=600,
            started_at=_NOW)
        assert wrote is not None
        self.assertEqual(patches[-1][0], "t1")
        self.assertEqual(patches[-1][1]["attempt_id"], "t1-g0-w0")
        self.assertEqual(patches[-1][1]["attempt_deadline_at"], wrote)
        await deadline_mod.clear_attempt_deadline(patch, "t1")
        self.assertEqual(patches[-1], ("t1", {"attempt_deadline_at": None}))

    async def test_mark_without_patcher_is_fail_open(self) -> None:
        self.assertIsNone(await deadline_mod.mark_attempt_start(
            None, task_id="t1", attempt_id="a", timeout_seconds=60))


class DeadlineWatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_overdue_records_deadline_missed(self) -> None:
        repo = _FakeRepo([
            _ticket("late", status=TaskStatus.WAITING.value,
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW - timedelta(minutes=11)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            found = await watch.check_due(_NOW)
            self.assertEqual(len(found), 1)
            self.assertEqual(recorded[0]["deadline_type"], "resume")
            self.assertEqual(recorded[0]["task_id"], "late")
        finally:
            await watch.close()

    async def test_resume_within_grace_records_nothing(self) -> None:
        repo = _FakeRepo([
            _ticket("early", status=TaskStatus.WAITING.value,
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW - timedelta(minutes=9)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            self.assertEqual(await watch.check_due(_NOW), [])
            self.assertEqual(recorded, [])
        finally:
            await watch.close()

    async def test_requeued_then_parked_for_capacity_records_nothing(
        self,
    ) -> None:
        repo = _FakeRepo([
            _ticket("requeued", status=TaskStatus.QUEUED.value,
                    waiting_reason=WAIT_LIMIT_PROJECT,
                    resume_at=_NOW - timedelta(minutes=30)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            self.assertEqual(await watch.check_due(_NOW), [])
            self.assertEqual(recorded, [])
        finally:
            await watch.close()

    async def test_no_pending_leaves_no_timer(self) -> None:
        repo = _FakeRepo()
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            self.assertIsNone(await watch.next_deadline(_NOW))
            self.assertIsNone(await watch.arm(_NOW))
            self.assertFalse(watch.has_timer)
        finally:
            await watch.close()

    async def test_future_deadline_arms_single_timer(self) -> None:
        repo = _FakeRepo([
            _ticket("waiter", status=TaskStatus.WAITING.value,
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW + timedelta(minutes=5)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            nearest = await watch.arm(_NOW)
            self.assertEqual(nearest, _NOW + timedelta(minutes=15))
            self.assertTrue(watch.has_timer)
            self.assertEqual(await watch.check_due(_NOW), [])
        finally:
            await watch.close()

    async def test_attempt_past_deadline_records_critical(self) -> None:
        repo = _FakeRepo([
            _ticket("hung", status=TaskStatus.IN_PROGRESS.value,
                    attempt_id="hung-g0-w0",
                    attempt_deadline_at=_NOW - timedelta(seconds=1)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            found = await watch.check_due(_NOW)
            self.assertEqual(len(found), 1)
            self.assertEqual(recorded[0]["deadline_type"], "attempt")
            self.assertEqual(recorded[0]["severity"], "critical")
            self.assertEqual(recorded[0]["attempt_id"], "hung-g0-w0")
        finally:
            await watch.close()

    async def test_attempt_cleared_or_finished_records_nothing(self) -> None:
        repo = _FakeRepo([
            _ticket("done", status=TaskStatus.COMPLETED.value,
                    attempt_id="done-g0-w0",
                    attempt_deadline_at=_NOW - timedelta(hours=1)),
            _ticket("cleared", status=TaskStatus.IN_PROGRESS.value,
                    attempt_id="cleared-g0-w1",
                    attempt_deadline_at=None),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            self.assertEqual(await watch.check_due(_NOW), [])
        finally:
            await watch.close()

    async def test_attempt_id_changed_after_arm_records_nothing(self) -> None:
        repo = _FakeRepo([
            _ticket("rotated", status=TaskStatus.IN_PROGRESS.value,
                    attempt_id="rotated-g0-w0",
                    attempt_deadline_at=_NOW - timedelta(seconds=1)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            await watch.arm(_NOW)
            await repo.patch_task("rotated", {
                "attempt_id": "rotated-g0-w1",
                "attempt_deadline_at": _NOW + timedelta(hours=1),
            })
            self.assertEqual(await watch.check_due(_NOW), [])
            self.assertEqual(recorded, [])
        finally:
            await watch.close()

    async def test_real_capture_path_severity_and_idempotency(self) -> None:
        repo = _FakeRepo([
            _ticket("hung", status=TaskStatus.IN_PROGRESS.value,
                    attempt_id="hung-g0-w0",
                    attempt_deadline_at=_NOW - timedelta(seconds=1)),
            _ticket("late", status=TaskStatus.WAITING.value,
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW - timedelta(minutes=11)),
        ])
        incidents = _FakeIncidents()
        watch = deadline_mod.DeadlineWatch(
            repo, incidents=incidents, clock=lambda: _NOW)
        try:
            first = await watch.check_due(_NOW)
            second = await watch.check_due(_NOW)
            self.assertEqual(len(first), 2)
            self.assertEqual(first, second)
            self.assertEqual(len(incidents.docs), 2)
            by_task = {(doc.get("engine") or {}).get("task_id"): doc
                       for doc in incidents.docs.values()}
            self.assertEqual(by_task["hung"]["severity"], "critical")
            self.assertEqual(by_task["hung"]["kind"], "deadline_missed")
            self.assertEqual(by_task["late"]["severity"], "high")
            self.assertEqual(by_task["late"]["kind"], "deadline_missed")
        finally:
            await watch.close()


class DeadlineReviewFixTests(unittest.IsolatedAsyncioTestCase):
    """Regression tests for the agy2 review triage (TASK-2026-00383)."""

    async def test_arm_skips_overdue_and_arms_next_future(self) -> None:
        repo = _FakeRepo([
            _ticket("hung", status=TaskStatus.IN_PROGRESS.value,
                    attempt_id="hung-g0-w0",
                    attempt_deadline_at=_NOW - timedelta(seconds=1)),
            _ticket("waiter", status=TaskStatus.WAITING.value,
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW + timedelta(minutes=5)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            nearest = await watch.arm(_NOW)
            self.assertEqual(nearest, _NOW + timedelta(minutes=15))
            self.assertTrue(watch.has_timer)
        finally:
            await watch.close()

    async def test_wait_and_fire_records_due_then_rearms_future(self) -> None:
        repo = _FakeRepo([
            _ticket("hung", status=TaskStatus.IN_PROGRESS.value,
                    attempt_id="hung-g0-w0",
                    attempt_deadline_at=_NOW - timedelta(seconds=1)),
            _ticket("waiter", status=TaskStatus.WAITING.value,
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW + timedelta(minutes=5)),
        ])
        recorded: list[dict] = []
        watch = _watch(repo, recorded)
        try:
            await watch._wait_and_fire(0)
            await _drain()
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["task_id"], "hung")
            self.assertTrue(watch.has_timer)
        finally:
            await watch.close()

    async def test_mark_attempt_start_notifies_after_write(self) -> None:
        patched: list[tuple[str, dict]] = []
        notified: list[bool] = []

        async def patch(task_id: str, updates: dict) -> None:
            patched.append((task_id, dict(updates)))

        def notify() -> None:
            notified.append(True)

        wrote = await deadline_mod.mark_attempt_start(
            patch, task_id="t1", attempt_id="t1-g0-w0", timeout_seconds=600,
            started_at=_NOW, notify=notify)
        assert wrote is not None
        self.assertEqual(patched[-1][1]["attempt_deadline_at"], wrote)
        self.assertEqual(notified, [True])

    async def test_mark_without_patcher_never_notifies(self) -> None:
        notified: list[bool] = []

        def notify() -> None:
            notified.append(True)

        self.assertIsNone(await deadline_mod.mark_attempt_start(
            None, task_id="t1", attempt_id="a", timeout_seconds=60,
            notify=notify))
        self.assertEqual(notified, [])

    async def test_candidates_filter_uses_date_type(self) -> None:
        filt = deadline_mod.deadline_candidates_filter()
        branches = filt["$or"]
        self.assertEqual(len(branches), 2)
        self.assertEqual(
            branches[0]["attempt_deadline_at"], {"$type": "date"})
        self.assertEqual(branches[1]["resume_at"], {"$type": "date"})

    async def test_budget_hook_none_without_registration(self) -> None:
        deadline_mod.set_budget_deadline_hook(None)
        self.assertIsNone(deadline_mod.budget_window_for(_NOW, now=_NOW))

    async def test_budget_hook_passthrough(self) -> None:
        deadline_mod.set_budget_deadline_hook(
            lambda deadline, now: ("window", deadline, now))
        try:
            self.assertEqual(
                deadline_mod.budget_window_for(_NOW, now=_NOW),
                ("window", _NOW, _NOW))
        finally:
            deadline_mod.set_budget_deadline_hook(None)

    async def test_budget_hook_failure_is_fail_open(self) -> None:
        def bad_hook(deadline: datetime, now: datetime) -> object:
            raise RuntimeError("boom")

        deadline_mod.set_budget_deadline_hook(bad_hook)
        try:
            self.assertIsNone(
                deadline_mod.budget_window_for(_NOW, now=_NOW))
        finally:
            deadline_mod.set_budget_deadline_hook(None)

    async def test_set_deadline_watch_wires_refresh(self) -> None:
        repo = _FakeRepo([
            _ticket("blocker", status=TaskStatus.IN_PROGRESS.value,
                    project="other"),
            _ticket("late", status=TaskStatus.WAITING.value, project="p1",
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW - timedelta(minutes=11),
                    blocked_by=["blocker"]),
        ])
        recorded: list[dict] = []
        service = _FakeService(repo)
        launched: list[str] = []
        dispatcher = Dispatcher(
            repo=repo, settings=_settings(), service=service,
            rate_monitor=_FakeMonitor(), launcher=launched.append)
        watch = _watch(repo, recorded)
        try:
            dispatcher.set_deadline_watch(watch)
            await dispatcher.dispatch_cycle(now=_NOW)
            await _drain()
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["deadline_type"], "resume")
        finally:
            await watch.close()


class DispatcherDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def _dispatcher_with_watch(
        self, repo: _FakeRepo, recorded: list[dict],
    ) -> tuple[Dispatcher, deadline_mod.DeadlineWatch]:
        service = _FakeService(repo)
        launched: list[str] = []
        watch = _watch(repo, recorded)
        dispatcher = Dispatcher(
            repo=repo, settings=_settings(), service=service,
            rate_monitor=_FakeMonitor(), launcher=launched.append,
            deadline_watch=watch)
        return dispatcher, watch

    async def test_blocked_overdue_wait_records_through_cycle(self) -> None:
        repo = _FakeRepo([
            _ticket("blocker", status=TaskStatus.IN_PROGRESS.value,
                    project="other"),
            _ticket("held", status=TaskStatus.WAITING.value, project="p1",
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW - timedelta(minutes=11),
                    blocked_by=["blocker"]),
        ])
        recorded: list[dict] = []
        dispatcher, watch = self._dispatcher_with_watch(repo, recorded)
        try:
            await dispatcher.dispatch_cycle(now=_NOW)
            await _drain()
            row = await repo.get_task("held")
            assert row is not None
            self.assertEqual(row["status"], TaskStatus.WAITING.value)
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["deadline_type"], "resume")
        finally:
            await watch.close()

    async def test_requeued_then_capacity_parked_records_nothing(
        self,
    ) -> None:
        repo = _FakeRepo([
            _ticket("active-1", status=TaskStatus.IN_PROGRESS.value,
                    project="proj"),
            _ticket("queued-1", project="proj",
                    waiting_reason="rate_limit_queued",
                    resume_at=_NOW - timedelta(minutes=30)),
        ])
        recorded: list[dict] = []
        dispatcher, watch = self._dispatcher_with_watch(repo, recorded)
        try:
            out = await dispatcher.dispatch_cycle(now=_NOW)
            await _drain()
            self.assertEqual(out["parked"], 1)
            row = await repo.get_task("queued-1")
            assert row is not None
            self.assertEqual(row["waiting_reason"], WAIT_LIMIT_PROJECT)
            self.assertEqual(recorded, [])
            self.assertFalse(watch.has_timer)
        finally:
            await watch.close()


if __name__ == "__main__":
    unittest.main()


class FairQueueTests(unittest.IsolatedAsyncioTestCase):
    """An older ticket waiting for slots is never overtaken (TASK-2026-00510)."""

    def _monitor(self, levels: dict[str, tuple[float, float]] | None = None) -> RateMonitor:
        monitor = RateMonitor(_rate_settings())

        async def fake_get_status(provider: str) -> ProviderStatus:
            five_h, weekly = (levels or {}).get(provider, (1.0, 1.0))
            return _provider_status(provider, five_h, weekly)

        monitor.get_status = fake_get_status  # type: ignore[method-assign]
        return monitor

    def _repo(self) -> _FakeRepo:
        return _FakeRepo([
            _ticket("running-c", status=TaskStatus.IN_PROGRESS.value,
                    project="other", coding_tool="tier_c"),
            _ticket("heavy-s", project="erp", coding_tool="tier_s",
                    created_at=_NOW - timedelta(hours=3)),
            _ticket("light-c", project="web", coding_tool="tier_c",
                    created_at=_NOW - timedelta(hours=1)),
        ])

    async def test_newer_ticket_waits_behind_one_short_of_slots(self) -> None:
        repo = self._repo()
        launched: list[str] = []
        dispatcher = _dispatcher(repo, _FakeService(repo), launched,
                                 monitor=self._monitor())  # type: ignore[arg-type]
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["launched"], [])
        self.assertEqual((await repo.get_task("heavy-s"))["waiting_reason"],
                         WAIT_LIMIT_GLOBAL)
        self.assertEqual((await repo.get_task("light-c"))["waiting_reason"],
                         WAIT_LIMIT_GLOBAL)
        await repo.patch_task("running-c", {"status": TaskStatus.COMPLETED.value})
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["launched"], ["heavy-s"])

    async def test_a_quota_wait_reserves_nothing(self) -> None:
        repo = self._repo()
        await repo.patch_task("running-c", {"status": TaskStatus.COMPLETED.value})
        launched: list[str] = []
        exhausted = {"claude": (0.0, 0.9)}
        dispatcher = _dispatcher(repo, _FakeService(repo), launched,
                                 monitor=self._monitor(exhausted))  # type: ignore[arg-type]
        out = await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(out["launched"], ["light-c"])
