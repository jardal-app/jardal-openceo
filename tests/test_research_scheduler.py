"""Scheduler persistido de research: reactivo, con lease y recuperacion acotada.

La propiedad central (TASK-2026-00068) no se demuestra con un test de resultado: un
timer que sondea tambien "pasa" los tests de ejecucion. Por eso el reloj y la espera son
**inyectables**: el test avanza el reloj virtual mas alla del ``max_sleep`` legacy (3600)
sin trabajo y comprueba que **no** hay consultas nuevas hasta el wake. Los unitarios usan
un reloj virtual; la integracion de lease/fencing corre contra Mongo **real** aislado,
porque un fake del motor no demuestra la atomicidad de la toma.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import research.scheduler as scheduler_module  # noqa: E402
from research.scheduler import (  # noqa: E402
    OverlapPolicy,
    ResearchScheduleConflict,
    ResearchScheduleRequest,
    ResearchScheduleStatus,
    ResearchScheduleStore,
    ResearchScheduler,
    ScheduleValidationError,
    _overlap_counters,
    build_schedule_document,
    compute_next_run_at,
    execution_key_for,
    next_occurrence_after,
    schedule_fingerprint,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _VirtualWaiter:
    """Espera simulada: registra el timeout y, si hay presupuesto, avanza el reloj.

    Con el presupuesto agotado se comporta como el timer real sin vencimiento: se queda
    esperando el evento. Eso evita que un test con vencimientos periodicos gire sin
    control y permite observar "cuantas consultas hubo hasta el wake".
    """

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
        self.clock.advance(timeout)
        return False


class _FakeRepo:
    """Repo en memoria con los contadores que la prueba de no-sondeo necesita."""

    def __init__(self) -> None:
        self.schedules: dict[str, dict] = {}
        self.claim_calls = 0
        self.next_due_calls = 0

    async def create_research_schedule(self, doc: dict) -> tuple[dict, bool]:
        sid = doc["schedule_id"]
        dedupe = doc.get("dedupe_key")
        if dedupe:
            for row in self.schedules.values():
                if row.get("dedupe_key") == dedupe:
                    return dict(row), False
        if sid in self.schedules:
            return dict(self.schedules[sid]), False
        self.schedules[sid] = dict(doc)
        return dict(doc), True

    async def get_research_schedule(self, schedule_id: str) -> dict | None:
        row = self.schedules.get(schedule_id)
        return dict(row) if row else None

    async def list_research_schedules(self, **kwargs) -> list[dict]:
        rows = list(self.schedules.values())
        statuses = kwargs.get("statuses")
        if statuses:
            rows = [r for r in rows if r.get("status") in statuses]
        return [dict(r) for r in rows]

    async def claim_next_research_schedule(
        self, *, lease_owner: str, lease_seconds: int, now: datetime
    ) -> dict | None:
        self.claim_calls += 1
        due = [
            r
            for r in self.schedules.values()
            if r.get("status") == "active"
            and r.get("run_state") == "idle"
            and _aware(r["next_run_at"]) <= now
        ]
        if not due:
            return None
        due.sort(key=lambda r: _aware(r["next_run_at"]))
        row = due[0]
        row["run_state"] = "running"
        row["lease_owner"] = lease_owner
        row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        row["scheduled_for"] = row.get("scheduled_for") or row["next_run_at"]
        row["fencing_token"] = int(row.get("fencing_token") or 0) + 1
        return dict(row)

    async def patch_research_schedule(
        self,
        schedule_id: str,
        updates: dict,
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        row = self.schedules.get(schedule_id)
        if row is None:
            return None
        if lease_owner is not None and (
            row.get("lease_owner") != lease_owner or row.get("run_state") != "running"
        ):
            return None
        if fencing_token is not None and int(row.get("fencing_token") or 0) != int(fencing_token):
            return None
        row.update(updates)
        return dict(row)

    async def set_research_schedule_status(
        self, schedule_id: str, status: str, *, now: datetime | None = None, reason: str | None = None
    ) -> dict | None:
        row = self.schedules.get(schedule_id)
        if row is None:
            return None
        row["status"] = status
        row["status_reason"] = reason
        return dict(row)

    async def next_research_schedule_due_at(self) -> datetime | None:
        self.next_due_calls += 1
        active = [
            r
            for r in self.schedules.values()
            if r.get("status") == "active" and r.get("run_state") == "idle"
        ]
        if not active:
            return None
        return min(_aware(r["next_run_at"]) for r in active)

    async def recover_stale_research_schedules(
        self, cutoff: datetime, *, now: datetime | None = None
    ) -> int:
        recovered = 0
        for row in self.schedules.values():
            lease = row.get("lease_expires_at")
            if row.get("run_state") != "running" or lease is None:
                continue
            if _aware(lease) > cutoff:
                continue
            row["run_state"] = "idle"
            row["lease_owner"] = None
            row["lease_expires_at"] = None
            row["pending_reconcile"] = True
            if row.get("status") == "active" and _aware(row["next_run_at"]) <= now:
                row["next_run_at"] = now
            recovered += 1
        return recovered

    async def expire_research_schedules(self, now: datetime) -> int:
        count = 0
        for row in self.schedules.values():
            expires_at = row.get("expires_at")
            if row.get("status") == "active" and expires_at and _aware(expires_at) <= now:
                row["status"] = "expired"
                count += 1
        return count


# =========================================================================
# Matematica de ocurrencias: intervalo sin deriva y DST explicito
# =========================================================================


class OccurrenceMathTests(unittest.TestCase):
    def test_interval_does_not_drift(self) -> None:
        anchor = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        schedule = {"interval_seconds": 1800, "anchor_at": anchor, "timezone": "UTC"}
        after = anchor + timedelta(hours=1, minutes=10)
        result = next_occurrence_after(schedule, after)
        self.assertEqual(result, anchor + timedelta(seconds=3 * 1800))

    def test_interval_result_is_strictly_future(self) -> None:
        anchor = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        schedule = {"interval_seconds": 3600, "anchor_at": anchor, "timezone": "UTC"}
        result = next_occurrence_after(schedule, anchor)
        self.assertGreater(result, anchor)
        self.assertEqual(result, anchor + timedelta(hours=1))

    def test_daily_at_skips_a_nonexistent_dst_hour(self) -> None:
        """Spring forward (US): 02:30 no existe; el vencimiento es la hora valida."""
        zone = ZoneInfo("America/New_York")
        schedule = {"daily_at": "02:30", "timezone": "America/New_York"}
        after = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
        result = next_occurrence_after(schedule, after)
        self.assertGreater(result, after)
        self.assertEqual(result.astimezone(zone).strftime("%H:%M"), "03:30")
        self.assertEqual(result, datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))

    def test_daily_at_picks_the_first_pass_of_an_ambiguous_hour(self) -> None:
        """Fall back (US): 01:30 se repite; fold=0 elige la primera pasada (EDT)."""
        zone = ZoneInfo("America/New_York")
        schedule = {"daily_at": "01:30", "timezone": "America/New_York"}
        after = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        result = next_occurrence_after(schedule, after)
        self.assertEqual(result.astimezone(zone).strftime("%H:%M"), "01:30")
        self.assertEqual(result, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))

    def test_overlap_counters_skip_and_merge(self) -> None:
        claimed = {
            "interval_seconds": 1800,
            "scheduled_for": NOW - timedelta(hours=3),
            "overlap_policy": OverlapPolicy.SKIP.value,
        }
        skipped, merged = _overlap_counters(claimed, NOW)
        self.assertEqual((skipped, merged), (6, 0))
        claimed["overlap_policy"] = OverlapPolicy.MERGE.value
        skipped, merged = _overlap_counters(claimed, NOW)
        self.assertEqual((skipped, merged), (0, 6))

    def test_execution_key_is_stable_for_the_same_occurrence(self) -> None:
        schedule = {"schedule_id": "sch_1", "scheduled_for": NOW}
        self.assertEqual(execution_key_for(schedule), execution_key_for(dict(schedule)))
        self.assertNotEqual(
            execution_key_for(schedule),
            execution_key_for({"schedule_id": "sch_1", "scheduled_for": NOW + timedelta(minutes=1)}),
        )

    def test_compute_next_run_at_is_always_future(self) -> None:
        schedule = build_schedule_document(
            project_key="p",
            owner_id="o",
            purpose="watch",
            spec=ResearchScheduleRequest(workflow="research.watch_tick", interval_seconds=1800),
            dedupe_key=None,
            now=NOW,
        )
        self.assertGreater(compute_next_run_at(schedule, NOW), NOW)


# =========================================================================
# Validacion del spec
# =========================================================================


class ScheduleValidationTests(unittest.TestCase):
    def test_interval_spec_is_valid(self) -> None:
        spec = ResearchScheduleRequest(workflow="research.watch_tick", interval_seconds=1800)
        self.assertEqual(spec.interval_seconds, 1800)

    def test_daily_spec_is_valid(self) -> None:
        spec = ResearchScheduleRequest(
            workflow="research.watch_tick", daily_at="06:30", timezone="America/La_Paz"
        )
        self.assertEqual(spec.daily_at, "06:30")

    def test_both_cadences_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            ResearchScheduleRequest(
                workflow="research.watch_tick", interval_seconds=1800, daily_at="06:30"
            )

    def test_no_cadence_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            ResearchScheduleRequest(workflow="research.watch_tick")

    def test_invalid_timezone_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            ResearchScheduleRequest(
                workflow="research.watch_tick", interval_seconds=1800, timezone="Mars/Olympus"
            )

    def test_too_short_interval_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            ResearchScheduleRequest(workflow="research.watch_tick", interval_seconds=30)

    def test_blank_workflow_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            ResearchScheduleRequest(workflow="   ", interval_seconds=1800)

    def test_invalid_daily_at_is_rejected_by_parser(self) -> None:
        with self.assertRaises(ScheduleValidationError):
            scheduler_module.parse_daily_at("25:00")

    def test_empty_interval_is_not_read_as_zero(self) -> None:
        """Un intervalo ausente/vacio es spec invalido, no "0" ni el minimo."""
        with self.assertRaises(Exception):
            ResearchScheduleRequest(workflow="research.watch_tick", interval_seconds=0)


# =========================================================================
# Store: idempotencia, conflicto y aviso al timer
# =========================================================================


class ScheduleStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.store = ResearchScheduleStore(self.repo)
        self.notified = 0
        self.store.bind_waker(self._count)

    def _count(self) -> None:
        self.notified += 1

    async def _create(self, **overrides) -> tuple[dict, bool]:
        payload: dict = {"workflow": "research.watch_tick", "interval_seconds": 1800}
        payload.update(overrides)
        spec = ResearchScheduleRequest(**payload)
        return await self.store.create(
            project_key="jardal-openceo",
            owner_id="usr_test",
            purpose="watch",
            spec=spec,
            dedupe_key="sub:1",
            now=NOW,
        )

    async def test_create_persists_and_notifies(self) -> None:
        doc, created = await self._create()
        self.assertTrue(created)
        self.assertEqual(doc["status"], ResearchScheduleStatus.ACTIVE.value)
        self.assertEqual(self.notified, 1)

    async def test_create_is_idempotent_by_dedupe_key(self) -> None:
        first, created_one = await self._create()
        second, created_two = await self._create()
        self.assertTrue(created_one)
        self.assertFalse(created_two)
        self.assertEqual(first["schedule_id"], second["schedule_id"])

    async def test_same_dedupe_with_different_spec_is_a_conflict(self) -> None:
        await self._create()
        with self.assertRaises(ResearchScheduleConflict):
            await self._create(interval_seconds=3600)

    async def test_pause_and_resume_notify(self) -> None:
        doc, _ = await self._create()
        paused = await self.store.pause(doc["schedule_id"], now=NOW)
        self.assertEqual(paused["status"], ResearchScheduleStatus.PAUSED.value)
        resumed = await self.store.resume(doc["schedule_id"], now=NOW)
        self.assertEqual(resumed["status"], ResearchScheduleStatus.ACTIVE.value)

    async def test_update_cadence_recomputes_next_run(self) -> None:
        doc, _ = await self._create()
        updated = await self.store.update(
            doc["schedule_id"], {"interval_seconds": 3600}, now=NOW + timedelta(minutes=5)
        )
        self.assertGreater(_aware(updated["next_run_at"]), NOW + timedelta(minutes=5))

    async def test_update_persists_the_new_anchor_and_due_time(self) -> None:
        # Antes el vencimiento se calculaba y se descartaba: el cambio de cadencia no
        # tenia efecto hasta el siguiente vencimiento del calendario anterior.
        doc, _ = await self._create(interval_seconds=86400)
        later = NOW + timedelta(minutes=7)
        updated = await self.store.update(doc["schedule_id"], {"interval_seconds": 600}, now=later)
        self.assertEqual(_aware(updated["anchor_at"]), later)
        self.assertEqual(_aware(updated["next_run_at"]), later + timedelta(seconds=600))
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(_aware(stored["next_run_at"]), later + timedelta(seconds=600))

    async def test_update_replaces_the_other_cadence_and_rejects_invalid_results(self) -> None:
        doc, _ = await self._create()
        daily = await self.store.update(doc["schedule_id"], {"daily_at": "08:30"}, now=NOW)
        self.assertEqual((daily["daily_at"], daily["interval_seconds"]), ("08:30", None))
        back = await self.store.update(doc["schedule_id"], {"interval_seconds": 120}, now=NOW)
        self.assertEqual((back["daily_at"], back["interval_seconds"]), (None, 120))
        for bad in [{"interval_seconds": 5}, {"daily_at": "25:00"}, {"timezone": "No/Existe"},
                    {"params": "no es dict"}, {"max_attempts": 0}]:
            with self.assertRaises(ScheduleValidationError):
                await self.store.update(doc["schedule_id"], bad, now=NOW)

    async def test_update_cannot_touch_identity_state_lease_or_cursor(self) -> None:
        doc, _ = await self._create()
        for field in ["owner_id", "project_key", "purpose", "workflow", "status", "run_state", "lease_owner",
                      "fencing_token", "cursor", "next_run_at", "dedupe_key", "attempts"]:
            with self.assertRaises(ScheduleValidationError):
                await self.store.update(doc["schedule_id"], {field: "x"}, now=NOW)
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(stored["owner_id"], doc["owner_id"])

    async def test_fingerprint_ignores_param_key_order_at_any_depth(self) -> None:
        base = {"workflow": "w", "timezone": "UTC", "interval_seconds": 60, "daily_at": None}
        one = schedule_fingerprint({**base, "params": {"a": 1, "b": {"x": [1, 2], "y": 2}}})
        two = schedule_fingerprint({**base, "params": {"b": {"y": 2, "x": [1, 2]}, "a": 1}})
        self.assertEqual(one, two)
        self.assertNotEqual(one, schedule_fingerprint({**base, "params": {"a": 1, "b": {"x": [2, 1], "y": 2}}}))

    async def test_pause_with_a_reason_and_resume_clears_it(self) -> None:
        doc, _ = await self._create()
        paused = await self.store.pause(doc["schedule_id"], now=NOW, reason="access_revoked")
        self.assertEqual((paused["status"], paused["status_reason"]), ("paused", "access_revoked"))
        resumed = await self.store.resume(doc["schedule_id"], now=NOW)
        self.assertEqual((resumed["status"], resumed["status_reason"]), ("active", None))

    async def test_record_cursor_with_fencing_rejects_a_stale_writer(self) -> None:
        doc, _ = await self._create()
        claimed = await self.repo.claim_next_research_schedule(
            lease_owner="a", lease_seconds=60, now=NOW + timedelta(hours=2))
        stored = await self.store.record_cursor(
            doc["schedule_id"], {"etag": "1"}, lease_owner="a", fencing_token=claimed["fencing_token"])
        self.assertEqual(stored["cursor"], {"etag": "1"})
        self.assertIsNone(await self.store.record_cursor(
            doc["schedule_id"], {"etag": "2"}, lease_owner="a", fencing_token=claimed["fencing_token"] + 1))
        self.assertEqual((await self.repo.get_research_schedule(doc["schedule_id"]))["cursor"], {"etag": "1"})


# =========================================================================
# Bucle del timer: sin barrido, vencimiento preciso y wake
# =========================================================================


class SchedulerLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.handled: list[dict] = []

    async def _handler(self, schedule: dict) -> dict:
        self.handled.append(schedule)
        return {"ok": True}

    def _seed(self, *, run_at: datetime, **overrides) -> dict:
        payload: dict = {"workflow": "research.watch_tick", "interval_seconds": 1800}
        payload.update(overrides)
        spec = ResearchScheduleRequest(**payload)
        doc = build_schedule_document(
            project_key="p",
            owner_id="o",
            purpose="watch",
            spec=spec,
            dedupe_key=None,
            now=NOW,
        )
        doc["next_run_at"] = run_at
        self.repo.schedules[doc["schedule_id"]] = doc
        return doc

    def _scheduler(self, waiter: _VirtualWaiter, **overrides) -> ResearchScheduler:
        return ResearchScheduler(
            repo=self.repo,
            handler=self._handler,
            now_fn=self.clock,
            waiter=waiter,
            max_sleep_seconds=None,
            **overrides,
        )

    async def test_idle_timer_does_not_poll(self) -> None:
        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        # Sin vencimiento, el timer espera al evento: no hay timeout que agotar.
        self.assertIn(None, waiter.calls)
        self.assertLessEqual(self.repo.claim_calls, 2)
        await scheduler.stop()

    async def test_advancing_time_past_legacy_max_sleep_does_not_query(self) -> None:
        """El bug que el ticket exige corregir: Horizon reconsultaba cada 3600 s."""
        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)
        before = (self.repo.claim_calls, self.repo.next_due_calls)

        # Mas alla del max_sleep legacy (3600) y del doble, sin trabajo pendiente.
        self.clock.advance(7200)
        await asyncio.sleep(0.05)

        self.assertEqual((self.repo.claim_calls, self.repo.next_due_calls), before)
        self.assertEqual(waiter.calls, [None])
        await scheduler.stop()

    async def test_future_due_is_waited_precisely_then_runs(self) -> None:
        self._seed(run_at=NOW + timedelta(seconds=1800))
        waiter = _VirtualWaiter(self.clock, budget=1)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        self.assertEqual(waiter.calls[0], 1800.0)
        self.assertEqual(len(self.handled), 1)
        await scheduler.stop()

    async def test_overdue_schedule_runs_once(self) -> None:
        self._seed(run_at=NOW - timedelta(minutes=5))
        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        self.assertEqual(len(self.handled), 1)
        # El siguiente vencimiento queda futuro: no hay rafaga de catch-up.
        doc = self.repo.schedules[self.handled[0]["schedule_id"]]
        self.assertGreater(_aware(doc["next_run_at"]), self.clock())
        await scheduler.stop()

    async def test_wake_runs_a_schedule_created_after_start(self) -> None:
        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        self._seed(run_at=NOW - timedelta(seconds=1))
        scheduler.wake()
        await asyncio.sleep(0.05)

        self.assertEqual(len(self.handled), 1)
        await scheduler.stop()

    async def test_a_future_schedule_is_not_run_early(self) -> None:
        self._seed(run_at=NOW + timedelta(hours=5))
        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        self.assertEqual(self.handled, [])
        await scheduler.stop()

    async def test_restart_recovers_a_stale_lease_with_one_catchup(self) -> None:
        doc = self._seed(run_at=NOW - timedelta(hours=5))
        # Simula una caida entre la toma y el cierre: quedó `running` con lease vencida.
        doc["run_state"] = "running"
        doc["lease_owner"] = "dead-coordinator"
        doc["lease_expires_at"] = NOW - timedelta(hours=1)

        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        self.assertEqual(len(self.handled), 1)
        # La reentrega no se afirma exactly-once: se marca para reconciliar.
        self.assertTrue(self.handled[0]["pending_reconcile"])
        stored = self.repo.schedules[doc["schedule_id"]]
        self.assertFalse(stored["pending_reconcile"])
        self.assertGreater(_aware(stored["next_run_at"]), self.clock())
        await scheduler.stop()

    async def test_a_failing_schedule_retries_with_backoff_then_resumes_cadence(self) -> None:
        doc = self._seed(run_at=NOW - timedelta(seconds=1), max_attempts=2)

        async def failing(_schedule: dict) -> dict:
            raise RuntimeError("boom")

        scheduler = ResearchScheduler(
            repo=self.repo,
            handler=failing,
            now_fn=self.clock,
            waiter=_VirtualWaiter(self.clock, budget=3),
            backoff_base=60,
        )
        await scheduler.start()
        await asyncio.sleep(0.05)

        stored = self.repo.schedules[doc["schedule_id"]]
        self.assertEqual(stored["last_error"], "boom")
        await scheduler.stop()

    async def test_paused_schedule_is_not_claimed(self) -> None:
        doc = self._seed(run_at=NOW - timedelta(seconds=1))
        doc["status"] = ResearchScheduleStatus.PAUSED.value
        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        self.assertEqual(self.handled, [])
        await scheduler.stop()

    async def test_expired_schedule_is_discarded_not_run(self) -> None:
        doc = self._seed(run_at=NOW - timedelta(seconds=1))
        doc["expires_at"] = NOW - timedelta(minutes=1)
        waiter = _VirtualWaiter(self.clock)
        scheduler = self._scheduler(waiter)
        await scheduler.start()
        await asyncio.sleep(0.05)

        self.assertEqual(self.handled, [])
        self.assertEqual(
            self.repo.schedules[doc["schedule_id"]]["status"],
            ResearchScheduleStatus.EXPIRED.value,
        )
        await scheduler.stop()

    async def test_a_stale_executor_cannot_close_a_taken_occurrence(self) -> None:
        """Fencing: el dueño viejo no puede cerrar una toma que ya no es suya."""
        doc = self._seed(run_at=NOW - timedelta(seconds=1))
        claimed = await self.repo.claim_next_research_schedule(
            lease_owner="owner-a", lease_seconds=120, now=self.clock()
        )
        token = claimed["fencing_token"]
        # Otro ejecutor toma de nuevo tras una recuperacion: el token cambia.
        self.repo.schedules[doc["schedule_id"]]["run_state"] = "idle"
        self.repo.schedules[doc["schedule_id"]]["lease_owner"] = None
        await self.repo.claim_next_research_schedule(
            lease_owner="owner-b", lease_seconds=120, now=self.clock()
        )
        stale = await self.repo.patch_research_schedule(
            doc["schedule_id"],
            {"last_error": "stale"},
            lease_owner="owner-a",
            fencing_token=token,
            now=self.clock(),
        )
        self.assertIsNone(stale)


# =========================================================================
# Integracion: lease y fencing contra Mongo real y aislado
# =========================================================================


try:
    from tests import research_support as support  # type: ignore
except Exception:  # noqa: BLE001 - ejecucion directa del archivo
    import research_support as support  # type: ignore


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo no disponible en este entorno")
class ScheduleRepoMongoTests(unittest.IsolatedAsyncioTestCase):
    DB = "openceo_test_research_scheduler"

    async def asyncSetUp(self) -> None:
        from motor.motor_asyncio import AsyncIOMotorClient

        self.client = AsyncIOMotorClient(support.MONGO_URL, serverSelectionTimeoutMS=1000)
        await self.client.drop_database(self.DB)
        self.repo = support.build_repo(self.DB)
        self.repo._client = self.client  # noqa: SLF001 - cliente del test
        await self.repo.ensure_indexes()

    async def asyncTearDown(self) -> None:
        await self.client.drop_database(self.DB)
        self.client.close()

    async def _create(self, *, run_at: datetime = NOW, dedupe: str | None = "sub:1") -> dict:
        doc = build_schedule_document(
            project_key="p",
            owner_id="o",
            purpose="watch",
            spec=ResearchScheduleRequest(workflow="research.watch_tick", interval_seconds=1800),
            dedupe_key=dedupe,
            settings=None,
            now=NOW,
        )
        doc["next_run_at"] = run_at
        stored, _ = await self.repo.create_research_schedule(doc)
        return stored

    async def test_terminal_status_is_stable_and_releases_the_identity(self) -> None:
        doc = await self._create(dedupe="sub:term")
        sid = doc["schedule_id"]
        cancelled = await self.repo.set_research_schedule_status(sid, "cancelled")
        self.assertEqual((cancelled["status"], cancelled["dedupe_key"]), ("cancelled", None))
        # ni una pausa tardia ni una reanudacion pueden sacarlo del estado terminal
        for status in ("paused", "active", "cancelled"):
            self.assertIsNone(await self.repo.set_research_schedule_status(sid, status, reason="x"))
        self.assertEqual((await self.repo.get_research_schedule(sid))["status"], "cancelled")
        # la identidad quedo libre: otro schedule con la misma clave es nuevo, no el cancelado
        again = await self._create(dedupe="sub:term")
        self.assertNotEqual(again["schedule_id"], sid)
        # expirar libera igual y tambien es terminal
        await self.repo.patch_research_schedule(again["schedule_id"], {"expires_at": NOW - timedelta(hours=1)})
        self.assertEqual(await self.repo.expire_research_schedules(NOW), 1)
        expired = await self.repo.get_research_schedule(again["schedule_id"])
        self.assertEqual((expired["status"], expired["dedupe_key"]), ("expired", None))
        self.assertIsNone(await self.repo.set_research_schedule_status(again["schedule_id"], "active"))
        third = await self._create(dedupe="sub:term")
        self.assertEqual(third["status"], "active")

    async def test_event_insert_is_one_round_trip_and_ignores_only_duplicates(self) -> None:
        base = {"subscription_id": "s", "owner_id": "o", "project_key": "p", "content_fp": "f",
                "kind": "new", "status": "pending", "first_seen_at": NOW}
        docs = [{**base, "event_key": f"k{n}", "item_key": f"k{n}"} for n in range(5)]
        self.assertEqual(await self.repo.insert_research_watch_events(docs), [f"k{n}" for n in range(5)])
        replay = docs + [{**base, "event_key": "nuevo", "item_key": "nuevo"}]
        self.assertEqual(await self.repo.insert_research_watch_events(replay), ["nuevo"])
        self.assertEqual(await self.repo.insert_research_watch_events([]), [])
        self.assertEqual(await self.repo.research_watch_events.count_documents({}), 6)
        # un fallo que no es duplicado no se traga
        from unittest.mock import AsyncMock, Mock, PropertyMock, patch
        from pymongo.errors import BulkWriteError
        failing = Mock()
        failing.insert_many = AsyncMock(side_effect=BulkWriteError({"writeErrors": [{"index": 0, "code": 121}]}))
        with patch.object(type(self.repo), "research_watch_events", new_callable=PropertyMock, return_value=failing):
            with self.assertRaises(BulkWriteError):
                await self.repo.insert_research_watch_events([{**base, "event_key": "x", "item_key": "x"}])

    async def test_claim_is_atomic_and_advances_fencing(self) -> None:
        await self._create(run_at=NOW - timedelta(seconds=1))
        first = await self.repo.claim_next_research_schedule(
            lease_owner="a", lease_seconds=120, now=NOW
        )
        second = await self.repo.claim_next_research_schedule(
            lease_owner="b", lease_seconds=120, now=NOW
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(first["run_state"], "running")
        self.assertEqual(first["fencing_token"], 1)
        self.assertEqual(_aware(first["scheduled_for"]), _aware(first["next_run_at"]))

    async def test_fencing_rejects_a_stale_completion(self) -> None:
        doc = await self._create(run_at=NOW - timedelta(seconds=1))
        claimed = await self.repo.claim_next_research_schedule(
            lease_owner="a", lease_seconds=120, now=NOW
        )
        stale = await self.repo.patch_research_schedule(
            doc["schedule_id"],
            {"last_error": "stale"},
            lease_owner="a",
            fencing_token=claimed["fencing_token"] + 1,
            now=NOW,
        )
        self.assertIsNone(stale)
        good = await self.repo.patch_research_schedule(
            doc["schedule_id"],
            {"last_error": "ok"},
            lease_owner="a",
            fencing_token=claimed["fencing_token"],
            now=NOW,
        )
        self.assertIsNotNone(good)

    async def test_recover_stale_lease_marks_reconcile_and_single_catchup(self) -> None:
        doc = await self._create(run_at=NOW - timedelta(hours=4))
        await self.repo.claim_next_research_schedule(
            lease_owner="dead", lease_seconds=1, now=NOW - timedelta(hours=1)
        )
        recovered = await self.repo.recover_stale_research_schedules(
            NOW - timedelta(seconds=30), now=NOW
        )
        self.assertEqual(recovered, 1)
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(stored["run_state"], "idle")
        self.assertTrue(stored["pending_reconcile"])
        self.assertEqual(_aware(stored["next_run_at"]), NOW)

    async def test_recover_ignores_a_lease_that_has_not_expired(self) -> None:
        await self._create(run_at=NOW - timedelta(seconds=1))
        await self.repo.claim_next_research_schedule(lease_owner="live", lease_seconds=600, now=NOW)
        recovered = await self.repo.recover_stale_research_schedules(
            NOW - timedelta(seconds=30), now=NOW
        )
        self.assertEqual(recovered, 0)

    async def test_expire_is_a_state_not_a_delete(self) -> None:
        doc = await self._create(run_at=NOW)
        await self.repo.patch_research_schedule(
            doc["schedule_id"], {"expires_at": NOW - timedelta(minutes=1)}, now=NOW
        )
        expired = await self.repo.expire_research_schedules(NOW)
        self.assertEqual(expired, 1)
        stored = await self.repo.get_research_schedule(doc["schedule_id"])
        self.assertEqual(stored["status"], ResearchScheduleStatus.EXPIRED.value)

    async def test_dedupe_key_is_unique(self) -> None:
        first = await self._create(dedupe="dup")
        second_doc = build_schedule_document(
            project_key="p",
            owner_id="o",
            purpose="watch",
            spec=ResearchScheduleRequest(workflow="research.watch_tick", interval_seconds=1800),
            dedupe_key="dup",
            now=NOW + timedelta(minutes=1),
        )
        second, created = await self.repo.create_research_schedule(second_doc)
        self.assertFalse(created)
        self.assertEqual(first["schedule_id"], second["schedule_id"])

    async def test_schedules_do_not_touch_the_tasks_collection(self) -> None:
        """Un tick de schedule es ejecucion, no un ticket de negocio."""
        await self._create(run_at=NOW - timedelta(seconds=1))
        await self.repo.claim_next_research_schedule(lease_owner="a", lease_seconds=120, now=NOW)
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)


if __name__ == "__main__":
    unittest.main()
