"""CEO review loop (TASK-2026-00119, F4).

Scheduled cadence plus debounced reactive wake over one persisted
trigger. No sweeps: the loop sleeps until the next scheduled review
and any wake interrupts it. Deterministic clock, no sleeping except
one short lifecycle smoke.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from ceo.loop import (
    CEOLoop,
    MAX_QUEUED_REASONS,
    next_scheduled_after,
)


def _utc(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


class _FakeRepo:
    def __init__(self, trigger: dict[str, Any] | None = None) -> None:
        self.trigger = trigger
        self.saves = 0

    async def get_ceo_trigger(self) -> dict[str, Any] | None:
        return dict(self.trigger) if self.trigger is not None else None

    async def save_ceo_trigger(self, doc: dict[str, Any]) -> None:
        self.saves += 1
        self.trigger = dict(doc)


class _FakeRunner:
    def __init__(self, action: str = "do_nothing") -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.action = action

    async def __call__(
        self, kind: str, reasons: list[str]
    ) -> dict[str, Any]:
        self.calls.append((kind, list(reasons)))
        return {"ok": True, "action": self.action}


def _mandate_fn(cadence: str | None = "semanal"):
    def _fn() -> Any:
        if cadence is None:
            return None
        return SimpleNamespace(
            revision_ceo=SimpleNamespace(cadencia=cadence))

    return _fn


def _loop_for(
    test: unittest.TestCase,
    repo: _FakeRepo,
    runner: _FakeRunner,
    now: datetime,
    **overrides: Any,
) -> tuple[CEOLoop, dict[str, Any]]:
    cell = {"now": now}
    settings = overrides.pop(
        "settings", SimpleNamespace(CEO_REACTIVE_MIN_INTERVAL_SECONDS=3600))
    mandate_fn = overrides.pop("mandate_fn", _mandate_fn("semanal"))
    loop = CEOLoop(
        repo=repo,
        settings=settings,
        mandate_fn=mandate_fn,
        review_runner=runner,
        now_fn=lambda: cell["now"],
        **overrides,
    )
    test.addCleanup(lambda: cell.update(now=now))
    return loop, cell


class CadenceTests(unittest.TestCase):
    def test_intervals(self) -> None:
        base = _utc(2026, 10, 8)
        self.assertEqual(
            next_scheduled_after("diaria", base), base + timedelta(days=1))
        self.assertEqual(
            next_scheduled_after("semanal", base), base + timedelta(weeks=1))
        self.assertEqual(
            next_scheduled_after("quincenal", base),
            base + timedelta(weeks=2))
        self.assertEqual(
            next_scheduled_after("mensual", base),
            base + timedelta(days=28))

    def test_unknown_cadence_falls_back_to_weekly(self) -> None:
        base = _utc(2026, 10, 8)
        for cadence in (None, "", "hourly", "SEMANAL "):
            with self.subTest(cadence=cadence):
                if cadence == "SEMANAL ":
                    expected = base + timedelta(weeks=1)
                else:
                    expected = base + timedelta(weeks=1)
                self.assertEqual(next_scheduled_after(cadence, base), expected)

    def test_naive_datetime_assumed_utc(self) -> None:
        naive = datetime(2026, 10, 8, 12, 0)
        result = next_scheduled_after("diaria", naive)
        self.assertIsNotNone(result.tzinfo)
        self.assertEqual(
            result, _utc(2026, 10, 9))


class LoopTriggerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_creates_trigger_without_running(self) -> None:
        repo = _FakeRepo()
        runner = _FakeRunner()
        now = _utc(2026, 10, 8)
        loop, _ = _loop_for(self, repo, runner, now)
        try:
            await loop.start()
            trigger = await repo.get_ceo_trigger()
            assert trigger is not None
            self.assertEqual(
                trigger["next_scheduled_at"], now + timedelta(weeks=1))
            self.assertEqual(runner.calls, [])
        finally:
            await loop.stop()

    async def test_overdue_scheduled_runs_once_at_start(self) -> None:
        now = _utc(2026, 10, 8)
        repo = _FakeRepo({
            "next_scheduled_at": now - timedelta(hours=1),
            "last_scheduled_at": now - timedelta(weeks=1),
            "last_review_at": now - timedelta(weeks=1),
            "last_kind": "scheduled",
            "last_outcome": "do_nothing",
            "last_reactive_at": None,
            "pending_reasons": ["Incident: ticket t1 failed: x"],
            "updated_at": now - timedelta(weeks=1),
        })
        runner = _FakeRunner()
        loop, _ = _loop_for(self, repo, runner, now)
        try:
            await loop.start()
            self.assertEqual(len(runner.calls), 1)
            kind, reasons = runner.calls[0]
            self.assertEqual(kind, "scheduled")
            self.assertIn("Incident: ticket t1 failed: x", reasons)
            trigger = await repo.get_ceo_trigger()
            assert trigger is not None
            self.assertEqual(
                trigger["next_scheduled_at"], now + timedelta(weeks=1))
            self.assertEqual(trigger["pending_reasons"], [])
        finally:
            await loop.stop()

    async def test_corrupt_trigger_is_rebuilt(self) -> None:
        now = _utc(2026, 10, 8)
        repo = _FakeRepo({"next_scheduled_at": "not-a-date"})
        runner = _FakeRunner()
        loop, _ = _loop_for(self, repo, runner, now)
        try:
            await loop.start()
            trigger = await repo.get_ceo_trigger()
            assert trigger is not None
            self.assertEqual(
                trigger["next_scheduled_at"], now + timedelta(weeks=1))
            self.assertEqual(runner.calls, [])
        finally:
            await loop.stop()

    async def test_cadence_change_applies_without_restart(self) -> None:
        now = _utc(2026, 10, 8)
        repo = _FakeRepo({
            "next_scheduled_at": now - timedelta(minutes=1),
            "last_scheduled_at": None,
            "last_review_at": None,
            "last_kind": None,
            "last_outcome": None,
            "last_reactive_at": None,
            "pending_reasons": [],
            "updated_at": now,
        })
        runner = _FakeRunner()
        loop, _ = _loop_for(
            self, repo, runner, now, mandate_fn=_mandate_fn("diaria"))
        await loop._cycle()
        trigger = await repo.get_ceo_trigger()
        assert trigger is not None
        self.assertEqual(
            trigger["next_scheduled_at"], now + timedelta(days=1))

    async def test_missing_mandate_uses_weekly_default(self) -> None:
        now = _utc(2026, 10, 8)
        repo = _FakeRepo()
        runner = _FakeRunner()
        loop, _ = _loop_for(
            self, repo, runner, now, mandate_fn=_mandate_fn(None))
        trigger = await loop._ensure_trigger()
        self.assertEqual(
            trigger["next_scheduled_at"], now + timedelta(weeks=1))


class LoopCycleTests(unittest.IsolatedAsyncioTestCase):
    def _ready(self, now: datetime, **overrides: Any):
        repo = _FakeRepo({
            "next_scheduled_at": now + timedelta(weeks=1),
            "last_scheduled_at": now - timedelta(weeks=1),
            "last_review_at": now - timedelta(weeks=1),
            "last_kind": "scheduled",
            "last_outcome": "do_nothing",
            "last_reactive_at": None,
            "pending_reasons": [],
            "updated_at": now,
        })
        runner = _FakeRunner()
        loop, cell = _loop_for(self, repo, runner, now, **overrides)
        return loop, repo, runner, cell

    async def test_wake_runs_reactive_review(self) -> None:
        now = _utc(2026, 10, 8)
        loop, repo, runner, _ = self._ready(now)
        loop.wake("Objective OBJ-1 parked (blocked): vendor down")
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        kind, reasons = runner.calls[0]
        self.assertEqual(kind, "reactive")
        self.assertEqual(reasons, [
            "Objective OBJ-1 parked (blocked): vendor down"])
        trigger = await repo.get_ceo_trigger()
        assert trigger is not None
        self.assertEqual(trigger["last_reactive_at"], now)
        self.assertEqual(trigger["last_kind"], "reactive")
        self.assertEqual(trigger["last_outcome"], "do_nothing")
        self.assertEqual(trigger["pending_reasons"], [])

    async def test_second_wake_inside_debounce_accumulates(self) -> None:
        now = _utc(2026, 10, 8)
        loop, repo, runner, cell = self._ready(now)
        loop.wake("first incident")
        await loop._cycle()
        cell["now"] = now + timedelta(minutes=10)
        loop.wake("second incident")
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        trigger = await repo.get_ceo_trigger()
        assert trigger is not None
        self.assertEqual(trigger["pending_reasons"], ["second incident"])

    async def test_reactive_runs_again_after_interval(self) -> None:
        now = _utc(2026, 10, 8)
        loop, _, runner, cell = self._ready(now)
        loop.wake("first incident")
        await loop._cycle()
        cell["now"] = now + timedelta(minutes=10)
        loop.wake("second incident")
        await loop._cycle()
        cell["now"] = now + timedelta(hours=2)
        loop.wake("third incident")
        await loop._cycle()

        self.assertEqual(len(runner.calls), 2)
        kind, reasons = runner.calls[1]
        self.assertEqual(kind, "reactive")
        self.assertEqual(reasons, ["second incident", "third incident"])

    async def test_scheduled_due_consumes_pending_reasons(self) -> None:
        now = _utc(2026, 10, 8)
        loop, repo, runner, cell = self._ready(now)
        loop.wake("first incident")
        await loop._cycle()
        cell["now"] = now + timedelta(minutes=10)
        loop.wake("second incident")
        await loop._cycle()
        self.assertEqual(len(runner.calls), 1)
        # The schedule comes due while a reason is still debounced.
        trigger = await repo.get_ceo_trigger()
        assert trigger is not None
        trigger["next_scheduled_at"] = cell["now"]
        await repo.save_ceo_trigger(trigger)
        await loop._cycle()

        self.assertEqual(len(runner.calls), 2)
        kind, reasons = runner.calls[1]
        self.assertEqual(kind, "scheduled")
        self.assertIn("second incident", reasons)
        trigger = await repo.get_ceo_trigger()
        assert trigger is not None
        self.assertEqual(trigger["pending_reasons"], [])
        self.assertEqual(trigger["last_kind"], "scheduled")

    async def test_idle_cycle_runs_nothing(self) -> None:
        now = _utc(2026, 10, 8)
        loop, _, runner, _ = self._ready(now)
        await loop._cycle()
        self.assertEqual(runner.calls, [])

    async def test_idle_cycle_consumes_debounced_pending_after_interval(
        self,
    ) -> None:
        now = _utc(2026, 10, 8)
        loop, repo, runner, cell = self._ready(now)
        loop.wake("first incident")
        await loop._cycle()
        cell["now"] = now + timedelta(minutes=10)
        loop.wake("second incident")
        await loop._cycle()
        self.assertEqual(len(runner.calls), 1)
        # No new company event: the idle tick re-offers the debounced
        # pendings once the reactive interval elapsed.
        cell["now"] = now + timedelta(hours=2)
        await loop._cycle()

        self.assertEqual(len(runner.calls), 2)
        kind, reasons = runner.calls[1]
        self.assertEqual(kind, "reactive")
        self.assertEqual(reasons, ["second incident"])
        trigger = await repo.get_ceo_trigger()
        assert trigger is not None
        self.assertEqual(trigger["pending_reasons"], [])

    async def test_idle_cycle_inside_interval_keeps_debounce(self) -> None:
        now = _utc(2026, 10, 8)
        loop, repo, runner, cell = self._ready(now)
        loop.wake("first incident")
        await loop._cycle()
        cell["now"] = now + timedelta(minutes=10)
        loop.wake("second incident")
        await loop._cycle()
        cell["now"] = now + timedelta(minutes=20)
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        trigger = await repo.get_ceo_trigger()
        assert trigger is not None
        self.assertEqual(trigger["pending_reasons"], ["second incident"])

    async def test_wake_queue_is_bounded(self) -> None:
        now = _utc(2026, 10, 8)
        loop, _, _, _ = self._ready(now)
        for n in range(MAX_QUEUED_REASONS + 5):
            loop.wake(f"reason {n}")
        self.assertEqual(len(loop._queued), MAX_QUEUED_REASONS)
        self.assertEqual(loop._queued[0], "reason 5")

    async def test_wake_with_blank_reason_only_signals(self) -> None:
        now = _utc(2026, 10, 8)
        loop, _, runner, _ = self._ready(now)
        loop.wake("   ")
        await loop._cycle()
        self.assertEqual(runner.calls, [])

    async def test_loop_survives_runner_errors(self) -> None:
        now = _utc(2026, 10, 8)
        repo = _FakeRepo({
            "next_scheduled_at": now + timedelta(weeks=1),
            "last_scheduled_at": None,
            "last_review_at": None,
            "last_kind": None,
            "last_outcome": None,
            "last_reactive_at": None,
            "pending_reasons": [],
            "updated_at": now,
        })

        async def _raising(kind: str, reasons: list[str]) -> dict[str, Any]:
            del kind, reasons
            raise RuntimeError("review exploded")

        loop = CEOLoop(
            repo=repo,
            settings=SimpleNamespace(
                CEO_REACTIVE_MIN_INTERVAL_SECONDS=0),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=_raising,
            now_fn=lambda: now,
        )
        try:
            await loop.start()
            loop.wake("incident")
            await asyncio.sleep(0.3)
            assert loop._job is not None
            self.assertFalse(loop._job.done())
        finally:
            await loop.stop()


class ReliabilityWakeTests(unittest.IsolatedAsyncioTestCase):
    """Hunter wakes group by Problem + transition (TASK-2026-00380 §12):

    20 equivalent events render one identical reason, so the loop
    debounce keeps a single notice instead of a storm.
    """

    def _ready(self, test: unittest.TestCase, now: datetime):
        repo = _FakeRepo({
            "next_scheduled_at": now + timedelta(weeks=1),
            "last_scheduled_at": now - timedelta(weeks=1),
            "last_review_at": now - timedelta(weeks=1),
            "last_kind": "scheduled",
            "last_outcome": "do_nothing",
            "last_reactive_at": None,
            "pending_reasons": [],
            "updated_at": now,
        })
        runner = _FakeRunner()
        loop = CEOLoop(
            repo=repo,
            settings=SimpleNamespace(
                CEO_REACTIVE_MIN_INTERVAL_SECONDS=3600),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=runner,
            now_fn=lambda: now,
        )
        return loop, repo, runner

    def test_grouping_keeps_order_and_drops_unknown(self) -> None:
        from ceo.review import (
            collapse_reliability_wake_reasons,
            reliability_wake_reason,
        )

        self.assertIsNone(reliability_wake_reason(None))
        self.assertIsNone(reliability_wake_reason({}))
        self.assertIsNone(reliability_wake_reason(
            {"problem_key": "p1", "transition": "diagnosed"}))
        self.assertIsNone(reliability_wake_reason(
            {"transition": "new_critical"}))
        reasons = collapse_reliability_wake_reasons([
            {"problem_key": "p1", "transition": "new_critical"},
            {"problem_key": "p1", "transition": "new_critical"},
            {"problem_key": "p2", "transition": "regressed"},
            {"problem_key": "p1", "transition": "regressed"},
            {"problem_key": "p9", "transition": "closed"},
        ])
        self.assertEqual(reasons, [
            "Reliability p1 new_critical",
            "Reliability p2 regressed",
            "Reliability p1 regressed",
        ])

    def test_twenty_equivalent_events_collapse_to_one(self) -> None:
        from ceo.review import collapse_reliability_wake_reasons

        events = [
            {"problem_key": "engine:abc", "transition": "new_high"}
            for _ in range(20)
        ]
        self.assertEqual(
            collapse_reliability_wake_reasons(events),
            ["Reliability engine:abc new_high"])

    async def test_twenty_wakes_yield_a_single_notice(self) -> None:
        from ceo.review import collapse_reliability_wake_reasons

        now = _utc(2026, 10, 8)
        loop, _, runner = self._ready(self, now)
        reasons = collapse_reliability_wake_reasons([
            {"problem_key": "engine:abc", "transition": "new_high"}
            for _ in range(20)
        ])
        self.assertEqual(len(reasons), 1)
        for reason in reasons * 20:
            loop.wake(reason)
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        kind, noticed = runner.calls[0]
        self.assertEqual(kind, "reactive")
        self.assertEqual(noticed, ["Reliability engine:abc new_high"])


class RouterAlertTests(unittest.IsolatedAsyncioTestCase):
    """Fresh free-router alerts join review reasons (TASK-2026-00427)."""

    def _ready(self, now: datetime, alerts: list[dict] | None = None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name)
        if alerts is not None:
            with open(state / "alerts.jsonl", "w", encoding="utf-8") as handle:
                for alert in alerts:
                    handle.write(json.dumps(alert) + "\n")
        repo = _FakeRepo({
            "next_scheduled_at": now + timedelta(weeks=1),
            "last_scheduled_at": now - timedelta(weeks=1),
            "last_review_at": now - timedelta(weeks=1),
            "last_kind": "scheduled",
            "last_outcome": "do_nothing",
            "last_reactive_at": None,
            "pending_reasons": [],
            "updated_at": now,
        })
        runner = _FakeRunner()
        loop, _ = _loop_for(
            self, repo, runner, now, router_state_dir=state)
        return loop, runner

    def _alert(self, now: datetime, kind: str, age: timedelta) -> dict:
        return {
            "ts": (now - age).isoformat(),
            "kind": kind,
            "source": "update",
            "detail": "catalog.json publishes 668 entries with zero usable",
        }

    async def test_fresh_no_free_models_joins_reactive(self) -> None:
        now = _utc(2026, 10, 8)
        loop, runner = self._ready(
            now, [self._alert(now, "no_free_models", timedelta(hours=1))])
        loop.wake("first incident")
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        kind, reasons = runner.calls[0]
        self.assertEqual(kind, "reactive")
        self.assertEqual(len(reasons), 2)
        self.assertEqual(reasons[0], "first incident")
        self.assertIn("no_free_models", reasons[1])

    async def test_stale_alert_is_ignored(self) -> None:
        now = _utc(2026, 10, 8)
        loop, runner = self._ready(
            now, [self._alert(now, "no_free_models", timedelta(days=2))])
        loop.wake("first incident")
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        _, reasons = runner.calls[0]
        self.assertEqual(reasons, ["first incident"])

    async def test_missing_alerts_file_is_ignored(self) -> None:
        now = _utc(2026, 10, 8)
        loop, runner = self._ready(now, None)
        loop.wake("first incident")
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        _, reasons = runner.calls[0]
        self.assertEqual(reasons, ["first incident"])

    async def test_fresh_refresh_failed_joins_scheduled(self) -> None:
        now = _utc(2026, 10, 8)
        loop, runner = self._ready(
            now, [self._alert(now, "refresh_failed", timedelta(hours=2))])
        trigger = {
            "next_scheduled_at": now,
            "last_scheduled_at": now - timedelta(weeks=1),
            "last_review_at": now - timedelta(weeks=1),
            "last_kind": "scheduled",
            "last_outcome": "do_nothing",
            "last_reactive_at": None,
            "pending_reasons": [],
            "updated_at": now,
        }
        loop._repo.trigger = trigger
        await loop._cycle()

        self.assertEqual(len(runner.calls), 1)
        kind, reasons = runner.calls[0]
        self.assertEqual(kind, "scheduled")
        self.assertEqual(len(reasons), 1)
        self.assertIn("refresh_failed", reasons[0])
