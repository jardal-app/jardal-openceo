"""Phase 1 acceptance for the reactive global schedule kernel.

Doubles plus a virtual clock: every wait is injected, so idle costs zero
queries and wakes never hide behind real sleeps — except T41, which fires
the default waiter and clock in real time.
"""

from __future__ import annotations

import asyncio
import ast
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from reliability.capture import spool_path  # noqa: E402
from reliability.supervisor import backoff_for  # noqa: E402
from schedule_kernel import Agenda, Claim, Outcome, ScheduleKernel  # noqa: E402
from schedule_kernel.kernel import _default_wait  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

_MISSING = object()


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
    """Simulated wait: records timeouts, parks until wake or expire()."""

    def __init__(self, clock: _Clock, budget: int = 0) -> None:
        self.clock = clock
        self.budget = budget
        self.calls: list[float | None] = []
        self.nonpositive: list[float] = []
        self._pending: float | None = None
        self._expire = asyncio.Event()

    async def __call__(self, event: asyncio.Event, timeout: float | None) -> bool:
        self.calls.append(timeout)
        if timeout is not None and timeout <= 0:
            self.nonpositive.append(timeout)
        if event.is_set():
            return True
        if timeout is not None and self.budget > 0:
            self.budget -= 1
            self.clock.advance(timeout)
            return False
        self._pending = timeout
        self._expire.clear()
        waiter = asyncio.create_task(event.wait())
        expirer = asyncio.create_task(self._expire.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, expirer}, return_when=asyncio.FIRST_COMPLETED)
            return expirer not in done
        finally:
            for task in (waiter, expirer):
                task.cancel()
            await asyncio.gather(waiter, expirer, return_exceptions=True)

    def expire(self) -> None:
        """Advance the clock by the pending timeout; the wait ends False."""
        if self._pending is not None:
            self.clock.advance(self._pending)
        self._expire.set()


async def _fire_hook(hook) -> None:
    if hook is None:
        return
    result = hook()
    if asyncio.iscoroutine(result):
        await result


def _item(due, state="idle", boot_id=None, fencing=0, interval=None):
    return {"due": due, "state": state, "boot_id": boot_id,
            "fencing": fencing, "interval": interval}


class _FakeAgenda:
    """In-memory agenda double with a shareable store and one-shot hooks."""

    def __init__(self, name, clock, lanes=None, hot_on_wake=None,
                 store=None, order=None):
        self.name = name
        self._clock = clock
        self.store = store if store is not None else {}
        self.order = order if order is not None else []
        if lanes is not None:
            self.lanes = lanes
        if hot_on_wake is not None:
            self.hot_on_wake = hot_on_wake
        self._boot = ""
        self.next_due_calls = 0
        self.claim_limits: list[int] = []
        self.recover_ids: list[str] = []
        self.runs: list[str] = []
        self.run_claims: list[Claim] = []
        self.settles: list[tuple] = []
        self.cancelled: list[str] = []
        self.run_outcomes: list[Outcome] = []
        self.gates: dict[str, asyncio.Event] = {}
        self._in_run = 0
        self.peak = 0
        self.on_next_due = None
        self.before_claim = None
        self.after_claim = None
        self.fail_next_due: list = []
        self.fail_claim: list = []
        self.fail_run: list = []
        self.fail_settle: list = []
        self.fail_recover: list = []

    async def next_due(self):
        await asyncio.sleep(0)
        self.next_due_calls += 1
        self.order.append("next_due")
        if self.fail_next_due:
            exc = self.fail_next_due.pop(0)
            if exc is not None:
                raise exc
        dues = [item["due"] for item in self.store.values()
                if item["state"] == "idle"]
        snapshot = min(dues, default=None, key=_aware)
        hook, self.on_next_due = self.on_next_due, None
        await _fire_hook(hook)
        return snapshot

    async def claim_due(self, now, limit):
        await asyncio.sleep(0)
        self.claim_limits.append(limit)
        self.order.append("claim_due")
        if self.fail_claim:
            exc = self.fail_claim.pop(0)
            if exc is not None:
                raise exc
        hook, self.before_claim = self.before_claim, None
        await _fire_hook(hook)
        now = _aware(now)
        ready = sorted(
            (item_id for item_id, item in self.store.items()
             if item["state"] == "idle" and _aware(item["due"]) <= now),
            key=lambda item_id: (_aware(self.store[item_id]["due"]), item_id))
        claims = []
        for item_id in ready[:limit]:
            item = self.store[item_id]
            item["state"] = "running"
            item["boot_id"] = self._boot
            item["fencing"] += 1
            claims.append(Claim(item_id, self._boot, item["fencing"]))
        hook, self.after_claim = self.after_claim, None
        await _fire_hook(hook)
        return claims

    async def run(self, claim):
        await asyncio.sleep(0)
        self.order.append("run")
        self.runs.append(claim.item_id)
        self.run_claims.append(claim)
        if self.fail_run:
            exc = self.fail_run.pop(0)
            if exc is not None:
                raise exc
        self._in_run += 1
        self.peak = max(self.peak, self._in_run)
        try:
            gate = self.gates.get(claim.item_id)
            if gate is not None:
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.cancelled.append(claim.item_id)
                    raise
        finally:
            self._in_run -= 1
        if self.run_outcomes:
            return self.run_outcomes.pop(0)
        return Outcome(ok=True, result=claim.item_id)

    async def settle(self, claim, outcome):
        await asyncio.sleep(0)
        if self.fail_settle:
            exc = self.fail_settle.pop(0)
            if exc is not None:
                raise exc
        self.order.append("settle")
        self.settles.append((claim, outcome))
        item = self.store.get(claim.item_id)
        if item is None:
            return
        if item.get("fencing") != claim.fencing_token:
            return
        if not outcome.ok:
            item["state"] = "idle"
            item["due"] = self._clock() + timedelta(seconds=300)
            return
        if item.get("interval"):
            due = _aware(item["due"])
            now = _aware(self._clock())
            step = timedelta(seconds=item["interval"])
            nxt = due
            while nxt <= now:
                nxt += step
            item["due"] = nxt
            item["state"] = "idle"
        else:
            item["state"] = "done"

    async def recover_at_boot(self, boot_id):
        await asyncio.sleep(0)
        self.order.append("recover")
        self._boot = boot_id
        self.recover_ids.append(boot_id)
        if self.fail_recover:
            exc = self.fail_recover.pop(0)
            if exc is not None:
                raise exc
        released = 0
        for item in self.store.values():
            if item["state"] == "running" and item.get("boot_id") != boot_id:
                item["state"] = "idle"
                released += 1
        return released


class _EventAgenda(_FakeAgenda):
    """Agenda with events but no persisted intent: hot on every wake."""

    def __init__(self, name, clock, **kwargs):
        kwargs["hot_on_wake"] = True
        super().__init__(name, clock, **kwargs)
        self.events = 0

    async def next_due(self):
        await asyncio.sleep(0)
        self.next_due_calls += 1
        self.order.append("next_due")
        if self.fail_next_due:
            exc = self.fail_next_due.pop(0)
            if exc is not None:
                raise exc
        hook, self.on_next_due = self.on_next_due, None
        await _fire_hook(hook)
        return None

    async def claim_due(self, now, limit):
        await asyncio.sleep(0)
        self.claim_limits.append(limit)
        self.order.append("claim_due")
        if self.fail_claim:
            exc = self.fail_claim.pop(0)
            if exc is not None:
                raise exc
        hook, self.before_claim = self.before_claim, None
        await _fire_hook(hook)
        claims = []
        if self.events > 0:
            self.events = 0
            claims = [Claim("cycle", self._boot, 0)]
        hook, self.after_claim = self.after_claim, None
        await _fire_hook(hook)
        return claims


class _BrokenDueAgenda(_FakeAgenda):
    """next_due always overdue, claim always empty: a contract breach."""

    async def next_due(self):
        await asyncio.sleep(0)
        self.next_due_calls += 1
        self.order.append("next_due")
        return _aware(self._clock()) - timedelta(seconds=1)

    async def claim_due(self, now, limit):
        await asyncio.sleep(0)
        self.claim_limits.append(limit)
        self.order.append("claim_due")
        return []


class _RepeatClaimAgenda(_FakeAgenda):
    """Non-atomic adapter: next_due always overdue, always the same claim."""

    async def next_due(self):
        await asyncio.sleep(0)
        self.next_due_calls += 1
        self.order.append("next_due")
        return _aware(self._clock()) - timedelta(seconds=1)

    async def claim_due(self, now, limit):
        await asyncio.sleep(0)
        self.claim_limits.append(limit)
        self.order.append("claim_due")
        return [Claim("X", self._boot, 1)]


class _ForeignClaimAgenda(_FakeAgenda):
    """Claims stamped with another boot id: never run here."""

    async def next_due(self):
        await asyncio.sleep(0)
        self.next_due_calls += 1
        self.order.append("next_due")
        return _aware(self._clock()) - timedelta(seconds=1)

    async def claim_due(self, now, limit):
        await asyncio.sleep(0)
        self.claim_limits.append(limit)
        self.order.append("claim_due")
        return [Claim("X", "boot-foreign", 1)]


class _OverLimitAgenda(_FakeAgenda):
    """First claim returns two claims for one lane; then quiet."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._armed = True

    async def next_due(self):
        await asyncio.sleep(0)
        self.next_due_calls += 1
        self.order.append("next_due")
        if self._armed:
            return _aware(self._clock()) - timedelta(seconds=1)
        return None

    async def claim_due(self, now, limit):
        await asyncio.sleep(0)
        self.claim_limits.append(limit)
        self.order.append("claim_due")
        if self._armed:
            self._armed = False
            return [Claim("X1", self._boot, 1), Claim("X2", self._boot, 2)]
        return []


class _ObjectClaimAgenda(_FakeAgenda):
    """First claim returns a non-Claim once, then behaves."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._armed = True

    async def claim_due(self, now, limit):
        if self._armed:
            self._armed = False
            await asyncio.sleep(0)
            self.claim_limits.append(limit)
            self.order.append("claim_due")
            return [object()]
        return await super().claim_due(now, limit)


class _FakeIncidents:
    """Dict-backed incidents collection for the real capture path."""

    def __init__(self):
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


class ScheduleKernelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._waiters: list[_VirtualWaiter] = []

    def _waiter(self, clock: _Clock, budget: int = 0) -> _VirtualWaiter:
        waiter = _VirtualWaiter(clock, budget)
        self._waiters.append(waiter)
        return waiter

    def _kernel(self, clock: _Clock, waiter: _VirtualWaiter,
                incidents=_MISSING, **kwargs):
        if incidents is _MISSING:
            incidents = _FakeIncidents()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kernel = ScheduleKernel(
            now_fn=clock, waiter=waiter, incidents=incidents,
            runtime_dir=tmp.name, **kwargs)
        self.addAsyncCleanup(kernel.stop)
        return kernel, incidents, tmp.name

    async def _pump(self, rounds: int = 200) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def asyncTearDown(self) -> None:
        for waiter in self._waiters:
            self.assertEqual(waiter.nonpositive, [])

    async def test_idle_costs_no_queries_and_waits_without_ceiling(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.next_due_calls, 1)
        self.assertEqual(agenda.claim_limits, [])
        self.assertEqual(waiter.calls, [None])
        clock.advance(10 * 24 * 3600)
        await self._pump()
        self.assertEqual(agenda.next_due_calls, 1)
        self.assertEqual(agenda.claim_limits, [])
        self.assertEqual(waiter.calls, [None])

    async def test_wake_during_next_due_is_not_lost(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()

        def _insert_and_wake():
            agenda.store["B"] = _item(clock() - timedelta(seconds=1))
            kernel.wake("a")

        agenda.on_next_due = _insert_and_wake
        kernel.wake("a")
        await self._pump()
        self.assertEqual(agenda.runs, ["B"])

    async def test_wake_during_claim_due_is_not_lost_and_is_no_breach(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=10))}
        agenda = _FakeAgenda("a", clock, store=store)
        kernel.register(agenda)

        def _take():
            store["A"]["state"] = "running"
            store["A"]["boot_id"] = "other-process"

        def _insert_and_wake():
            store["B"] = _item(clock() - timedelta(seconds=1))
            kernel.wake("a")

        agenda.before_claim = _take
        agenda.after_claim = _insert_and_wake
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, ["B"])
        self.assertEqual(incidents.docs, {})
        self.assertIsNone(kernel.status()["agendas"]["a"]["last_error"])

    async def test_hot_wake_during_run_yields_exactly_one_more_claim(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _EventAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        gate = asyncio.Event()
        agenda.gates["cycle"] = gate
        agenda.events = 1
        kernel.wake("a")
        await self._pump()
        self.assertEqual(len(agenda.runs), 1)
        self.assertEqual(len(agenda.claim_limits), 1)
        for _ in range(3):
            agenda.events = 1
            kernel.wake("a")
            await self._pump()
            self.assertEqual(len(agenda.runs), 1)
            self.assertEqual(len(agenda.claim_limits), 1)
        gate.set()
        await self._pump()
        self.assertEqual(len(agenda.runs), 2)
        self.assertEqual(len(agenda.claim_limits), 2)
        await self._pump()
        self.assertEqual(len(agenda.runs), 2)
        self.assertEqual(len(agenda.claim_limits), 2)

    async def test_hot_wake_during_claim_due_is_not_lost(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _EventAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()

        def _arrive_and_wake():
            agenda.events = 1
            kernel.wake("a")

        agenda.after_claim = _arrive_and_wake
        kernel.wake("a")
        await self._pump()
        self.assertEqual(agenda.runs, ["cycle"])

    async def test_wake_during_run_on_a_persisted_agenda_is_not_lost(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=10))}
        agenda = _FakeAgenda("a", clock, store=store)
        agenda.gates["A"] = asyncio.Event()
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, ["A"])
        store["B"] = _item(clock() - timedelta(seconds=1))
        kernel.wake("a")
        await self._pump()
        agenda.gates["A"].set()
        await self._pump()
        self.assertEqual(agenda.runs, ["A", "B"])

    async def test_wake_requeries_only_its_agenda_and_coalesces(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        a = _FakeAgenda("a", clock)
        b = _FakeAgenda("b", clock)
        kernel.register(a)
        kernel.register(b)
        await kernel.start()
        await self._pump()
        for _ in range(5):
            kernel.wake("a")
        await self._pump()
        self.assertEqual(a.next_due_calls, 2)
        self.assertEqual(a.claim_limits, [])
        self.assertEqual(b.next_due_calls, 1)
        self.assertEqual(waiter.calls, [None, None])

    async def test_hot_wake_lets_claim_due_decide_with_no_persisted_due(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        agenda = _EventAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.next_due_calls, 1)
        kernel.wake("a")
        await self._pump()
        self.assertEqual(agenda.next_due_calls, 2)
        self.assertEqual(len(agenda.claim_limits), 1)
        self.assertEqual(incidents.docs, {})
        agenda.events = 1
        kernel.wake("a")
        await self._pump()
        self.assertEqual(agenda.runs, ["cycle"])

    async def test_slow_lane_does_not_block_another_agenda(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        a = _FakeAgenda("a", clock, store={"A": _item(NOW - timedelta(seconds=5))})
        b = _FakeAgenda("b", clock, store={"B": _item(NOW - timedelta(seconds=5))})
        a.gates["A"] = asyncio.Event()
        kernel.register(a)
        kernel.register(b)
        await kernel.start()
        await self._pump()
        self.assertEqual(b.runs, ["B"])
        self.assertEqual(len(b.settles), 1)
        self.assertEqual(kernel.status()["agendas"]["a"]["in_flight"], 1)

    async def test_overdue_agenda_with_full_lane_does_not_spin(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store_a = {"X": _item(NOW - timedelta(seconds=2)),
                   "Y": _item(NOW - timedelta(seconds=1))}
        a = _FakeAgenda("a", clock, store=store_a)
        b = _FakeAgenda("b", clock, store={"B": _item(NOW + timedelta(seconds=300))})
        gate = asyncio.Event()
        a.gates["X"] = gate
        kernel.register(a)
        kernel.register(b)
        await kernel.start()
        await self._pump()
        self.assertEqual(a.claim_limits, [1])
        self.assertEqual(a.next_due_calls, 2)
        self.assertEqual(waiter.calls, [300.0])
        snapshot = (a.next_due_calls, list(a.claim_limits), list(waiter.calls))
        await self._pump()
        clock.advance(3600)
        await self._pump()
        self.assertEqual(
            (a.next_due_calls, list(a.claim_limits), list(waiter.calls)), snapshot)
        gate.set()
        await self._pump()
        self.assertEqual(a.claim_limits, [1, 1])
        self.assertIn("Y", a.runs)

    async def test_empty_claim_with_overdue_next_due_records_once_and_does_not_spin(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        a = _BrokenDueAgenda("a", clock)
        b = _FakeAgenda("b", clock)
        kernel.register(a)
        kernel.register(b)
        await kernel.start()
        await self._pump()
        self.assertEqual(a.next_due_calls, 2)
        self.assertEqual(a.claim_limits, [1])
        self.assertEqual(len(incidents.docs), 1)
        (doc,) = incidents.docs.values()
        self.assertEqual(doc["phase"], "a:next_due_not_claimable")
        self.assertEqual(waiter.calls, [None])
        self.assertIsNone(kernel.status()["agendas"]["a"]["next_due"])
        before = (a.next_due_calls, list(a.claim_limits), len(incidents.docs))
        kernel.wake("b")
        await self._pump()
        self.assertEqual(
            (a.next_due_calls, list(a.claim_limits), len(incidents.docs)), before)

    async def test_double_claim_between_two_kernels_fires_once(self):
        clock = _Clock()
        waiter1 = self._waiter(clock)
        waiter2 = self._waiter(clock)
        kernel1, incidents1, _ = self._kernel(clock, waiter1)
        kernel2, incidents2, _ = self._kernel(clock, waiter2)
        store: dict = {}
        a1 = _FakeAgenda("a", clock, store=store)
        a2 = _FakeAgenda("a", clock, store=store)
        kernel1.register(a1)
        kernel2.register(a2)
        await kernel1.start()
        await kernel2.start()
        await self._pump()
        store["X"] = _item(clock() - timedelta(seconds=1))
        kernel1.wake("a")
        kernel2.wake("a")
        await self._pump(rounds=400)
        self.assertEqual(len(a1.runs) + len(a2.runs), 1)
        self.assertEqual(incidents1.docs, {})
        self.assertEqual(incidents2.docs, {})

    async def test_duplicate_claim_from_a_non_atomic_agenda_fires_once(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        agenda = _RepeatClaimAgenda("a", clock, lanes=2)
        agenda.gates["X"] = asyncio.Event()
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, ["X"])
        phases = {doc["phase"] for doc in incidents.docs.values()}
        self.assertIn("a:duplicate_claim", phases)
        self.assertIn("a:next_due_not_claimable", phases)
        snapshot = (agenda.next_due_calls, list(agenda.claim_limits),
                    list(waiter.calls), len(incidents.docs))
        await self._pump()
        self.assertEqual(
            (agenda.next_due_calls, list(agenda.claim_limits),
             list(waiter.calls), len(incidents.docs)), snapshot)

    async def test_young_claim_of_another_boot_is_released_and_fires_once(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"X": _item(NOW - timedelta(seconds=10), state="running",
                            boot_id="boot-old", fencing=3)}
        agenda = _FakeAgenda("a", clock, store=store)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        status = kernel.status()
        self.assertEqual(status["agendas"]["a"]["recovered"], 1)
        self.assertEqual(agenda.runs, ["X"])
        self.assertEqual(agenda.run_claims[0].boot_id, status["boot_id"])
        self.assertNotEqual(status["boot_id"], "boot-old")

    async def test_overdue_recurring_item_gets_one_catchup(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"R": _item(NOW - timedelta(seconds=600), interval=60)}
        agenda = _FakeAgenda("a", clock, store=store)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, ["R"])
        expected = (store["R"]["due"] - clock()).total_seconds()
        self.assertEqual(expected, 60.0)
        self.assertEqual(waiter.calls[-1], expected)
        waiter.expire()
        await self._pump()
        self.assertEqual(agenda.runs, ["R", "R"])
        await self._pump()
        self.assertEqual(agenda.runs, ["R", "R"])

    async def test_stop_cancels_lanes_without_settling(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=5))}
        agenda = _FakeAgenda("a", clock, store=store)
        agenda.gates["A"] = asyncio.Event()
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, ["A"])
        boot_id = kernel.status()["boot_id"]
        await asyncio.wait_for(kernel.stop(), timeout=10)
        self.assertEqual(agenda.cancelled, ["A"])
        self.assertEqual(agenda.settles, [])
        self.assertEqual(store["A"]["state"], "running")
        self.assertEqual(store["A"]["boot_id"], boot_id)
        status = kernel.status()
        self.assertEqual(status["agendas"]["a"]["in_flight"], 0)
        self.assertFalse(status["running"])
        waiter2 = self._waiter(clock)
        kernel2, _, _ = self._kernel(clock, waiter2)
        agenda2 = _FakeAgenda("a", clock, store=store)
        kernel2.register(agenda2)
        await kernel2.start()
        await self._pump()
        self.assertEqual(kernel2.status()["agendas"]["a"]["recovered"], 1)
        self.assertEqual(agenda2.runs, ["A"])

    async def test_future_due_is_waited_exactly_then_claimed_once(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW + timedelta(seconds=1800))}
        agenda = _FakeAgenda("a", clock, store=store)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(waiter.calls, [1800.0])
        self.assertEqual(agenda.next_due_calls, 1)
        waiter.expire()
        await self._pump()
        self.assertEqual(agenda.claim_limits, [1])
        self.assertEqual(agenda.runs, ["A"])
        first_claim = agenda.order.index("claim_due")
        reads = [i for i, op in enumerate(agenda.order) if op == "next_due"]
        self.assertGreaterEqual(len(reads), 2)
        self.assertLess(first_claim, reads[1])

    async def test_due_expiring_during_the_pass_still_bounds_the_wait(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store_a = {"A": _item(NOW - timedelta(seconds=5))}
        a = _FakeAgenda("a", clock, store=store_a)
        b = _FakeAgenda("b", clock, store={"B": _item(NOW + timedelta(seconds=10))})
        kernel.register(a)
        kernel.register(b)

        def _advance_and_take():
            clock.advance(20)
            store_a["A"]["state"] = "running"
            store_a["A"]["boot_id"] = "other-process"

        a.before_claim = _advance_and_take
        await kernel.start()
        await self._pump()
        self.assertEqual(waiter.calls[-1], 10.0)
        waiter.expire()
        await self._pump()
        self.assertEqual(b.runs, ["B"])

    async def test_lanes_bound_concurrency_and_limit_is_free_slots(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=3)),
                 "B": _item(NOW - timedelta(seconds=2)),
                 "C": _item(NOW - timedelta(seconds=1))}
        agenda = _FakeAgenda("a", clock, lanes=2, store=store)
        gates = {name: asyncio.Event() for name in store}
        agenda.gates.update(gates)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.claim_limits, [2])
        self.assertEqual(agenda.peak, 2)
        gates["A"].set()
        await self._pump()
        self.assertEqual(agenda.claim_limits, [2, 1])

    async def test_lanes_default_to_one_when_not_declared(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=2)),
                 "B": _item(NOW - timedelta(seconds=1))}
        agenda = _FakeAgenda("a", clock, store=store)
        self.assertFalse(hasattr(agenda, "lanes"))
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.claim_limits, [1, 1])
        self.assertEqual(agenda.runs, ["A", "B"])
        self.assertEqual(agenda.peak, 1)

    async def test_recover_runs_once_before_any_query_with_a_fresh_boot_id(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock,
                             store={"A": _item(NOW - timedelta(seconds=5))})
        kernel.register(agenda)
        await kernel.start()
        await kernel.start()
        await self._pump()
        self.assertEqual(len(agenda.recover_ids), 1)
        self.assertEqual(agenda.order[0], "recover")
        loops = [task for task in asyncio.all_tasks()
                 if task.get_name() == "schedule-kernel"]
        self.assertEqual(len(loops), 1)
        waiter2 = self._waiter(clock)
        kernel2, _, _ = self._kernel(clock, waiter2)
        agenda2 = _FakeAgenda("a", clock)
        kernel2.register(agenda2)
        await kernel2.start()
        await self._pump()
        self.assertNotEqual(kernel.status()["boot_id"],
                            kernel2.status()["boot_id"])
        self.assertEqual(agenda.run_claims[0].boot_id, kernel.status()["boot_id"])

    async def test_failed_recovery_does_not_block_start_or_other_agendas(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        a = _FakeAgenda("a", clock,
                        store={"A": _item(NOW - timedelta(seconds=5))})
        b = _FakeAgenda("b", clock,
                        store={"B": _item(NOW - timedelta(seconds=5))})
        a.fail_recover = [RuntimeError("nope")]
        kernel.register(a)
        kernel.register(b)
        await kernel.start()
        await self._pump()
        self.assertEqual(b.runs, ["B"])
        waiter.expire()
        await self._pump()
        self.assertEqual(a.runs, ["A"])
        self.assertEqual(
            kernel.status()["agendas"]["a"]["last_error"]["phase"],
            "recover_at_boot")
        self.assertEqual(len(incidents.docs), 1)

    async def test_restart_uses_a_new_boot_id(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        await kernel.stop()
        await kernel.start()
        await self._pump()
        self.assertEqual(len(agenda.recover_ids), 2)
        self.assertNotEqual(agenda.recover_ids[0], agenda.recover_ids[1])

    async def test_stop_is_idempotent_and_wake_after_stop_is_harmless(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        kernel.register(_FakeAgenda("a", clock))
        await kernel.stop()
        await kernel.start()
        await self._pump()
        await kernel.stop()
        await kernel.stop()
        kernel.wake("a")
        await self._pump()
        self.assertFalse(kernel.status()["running"])

    async def test_claim_with_foreign_boot_id_is_not_run(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        agenda = _ForeignClaimAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, [])
        phases = {doc["phase"] for doc in incidents.docs.values()}
        self.assertIn("a:foreign_boot_claim", phases)

    async def test_claims_over_limit_are_not_run(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        agenda = _OverLimitAgenda("a", clock, lanes=1)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, ["X1"])
        phases = {doc["phase"] for doc in incidents.docs.values()}
        self.assertIn("a:claim_over_limit", phases)

    async def test_run_failure_settles_records_incident_and_backs_off_in_the_single_wait(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock,
                             store={"A": _item(NOW - timedelta(seconds=5))})
        agenda.fail_run = [RuntimeError("boom")]
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(len(agenda.settles), 1)
        self.assertEqual(agenda.settles[0][1], Outcome(ok=False, error="boom"))
        self.assertEqual(len(incidents.docs), 1)
        (doc,) = incidents.docs.values()
        self.assertEqual(doc["kind"], "exception")
        self.assertIn(backoff_for(1), waiter.calls)
        self.assertEqual(agenda.claim_limits, [1])
        waiter.expire()
        await self._pump()
        self.assertEqual(waiter.calls[-1], 300.0 - backoff_for(1))
        self.assertEqual(
            kernel.status()["agendas"]["a"]["last_error"]["phase"], "run")

    async def test_success_resets_the_lane_streak(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=2)),
                 "B": _item(NOW - timedelta(seconds=1))}
        agenda = _FakeAgenda("a", clock, store=store)
        agenda.fail_run = [RuntimeError("boom"), None, RuntimeError("boom")]
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertIn(backoff_for(1), waiter.calls)
        waiter.expire()
        await self._pump()
        self.assertEqual(agenda.runs, ["A", "B"])
        waiter.expire()
        await self._pump()
        self.assertEqual(agenda.runs, ["A", "B", "A"])
        failures = [c for c in waiter.calls if c == backoff_for(1)]
        self.assertEqual(len(failures), 2)

    async def test_returned_failed_outcome_is_settled_without_incident(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock,
                             store={"A": _item(NOW - timedelta(seconds=5))})
        agenda.run_outcomes = [Outcome(ok=False, error="domain-says-no")]
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.settles[0][1],
                         Outcome(ok=False, error="domain-says-no"))
        self.assertEqual(incidents.docs, {})
        self.assertEqual(waiter.calls[-1], 300.0)
        self.assertIsNone(kernel.status()["agendas"]["a"]["backoff_until"])

    async def test_settle_failure_frees_the_lane_and_records_it(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=5))}
        agenda = _FakeAgenda("a", clock, store=store)
        agenda.fail_settle = [RuntimeError("store down")]
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(kernel.status()["agendas"]["a"]["in_flight"], 0)
        self.assertEqual(len(incidents.docs), 1)
        self.assertEqual(
            kernel.status()["agendas"]["a"]["last_error"]["phase"], "settle")
        self.assertEqual(store["A"]["state"], "running")

    async def test_run_cancelled_from_inside_is_a_failure_not_a_stop(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, incidents, _ = self._kernel(clock, waiter)
        store = {"A": _item(NOW - timedelta(seconds=5))}
        agenda = _FakeAgenda("a", clock, store=store)
        agenda.fail_run = [asyncio.CancelledError()]
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.settles[0][1].error, "CancelledError")
        self.assertEqual(kernel.status()["agendas"]["a"]["in_flight"], 0)
        self.assertEqual(len(incidents.docs), 1)
        store["B"] = _item(clock() - timedelta(seconds=1))
        waiter.expire()
        kernel.wake("a")
        await self._pump()
        self.assertEqual(agenda.runs[-1], "B")

    async def test_next_due_failure_retries_after_backoff_without_spinning(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock)
        agenda.fail_next_due = [RuntimeError("db down"), RuntimeError("db down")]
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        waiter.expire()
        await self._pump()
        waiter.expire()
        await self._pump()
        self.assertEqual(waiter.calls, [backoff_for(1), backoff_for(2), None])
        self.assertEqual(agenda.next_due_calls, 3)

    async def test_claim_failure_keeps_the_hot_mark(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _EventAgenda("a", clock)
        agenda.events = 1
        agenda.fail_claim = [RuntimeError("store down")]
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        kernel.wake("a")
        await self._pump()
        self.assertEqual(waiter.calls[-1], backoff_for(1))
        waiter.expire()
        await self._pump()
        self.assertEqual(agenda.runs, ["cycle"])

    async def test_internal_error_survives_and_rereads_everything(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        a = _ObjectClaimAgenda("a", clock,
                               store={"A": _item(NOW - timedelta(seconds=5))})
        b = _FakeAgenda("b", clock)
        kernel.register(a)
        kernel.register(b)
        await kernel.start()
        await self._pump()
        self.assertEqual(kernel.status()["last_error"]["phase"], "loop")
        self.assertGreater(waiter.calls[-1], 0)
        reads_before = (a.next_due_calls, b.next_due_calls)
        waiter.expire()
        await self._pump()
        self.assertGreater(a.next_due_calls, reads_before[0])
        self.assertGreater(b.next_due_calls, reads_before[1])
        a.store["C"] = _item(clock() - timedelta(seconds=1))
        kernel.wake("a")
        await self._pump()
        self.assertIn("C", a.runs)

    async def test_no_incident_source_logs_and_never_spools(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, tmp = self._kernel(clock, waiter, incidents=None)
        a = _BrokenDueAgenda("a", clock)
        store_b = {"B": _item(NOW - timedelta(seconds=5))}
        b = _FakeAgenda("b", clock, store=store_b)
        b.fail_run = [RuntimeError("boom")]
        kernel.register(a)
        kernel.register(b)
        await kernel.start()
        await self._pump()
        self.assertFalse(spool_path(tmp).exists())
        status = kernel.status()
        self.assertIsNotNone(status["agendas"]["a"]["last_error"])
        self.assertIsNotNone(status["agendas"]["b"]["last_error"])

    async def test_register_rejects_bad_declarations_and_late_calls(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        kernel.register(_FakeAgenda("a", clock))
        with self.assertRaises(ValueError):
            kernel.register(_FakeAgenda("a", clock))
        with self.assertRaises(ValueError):
            kernel.register(_FakeAgenda("", clock))
        with self.assertRaises(ValueError):
            kernel.register(_FakeAgenda("zero", clock, lanes=0))
        with self.assertRaises(ValueError):
            kernel.register(_FakeAgenda("bool", clock, lanes=True))
        partial = SimpleNamespace(
            name="partial",
            next_due=lambda: None,
            claim_due=lambda now, limit: [],
            run=lambda claim: None,
            recover_at_boot=lambda boot_id: 0)
        with self.assertRaises(ValueError):
            kernel.register(partial)
        await kernel.start()
        await self._pump()
        with self.assertRaises(RuntimeError):
            kernel.register(_FakeAgenda("late", clock))
        self.assertEqual(Agenda.lanes, 1)
        self.assertIs(Agenda.hot_on_wake, False)

    async def test_wake_of_an_unknown_agenda_does_not_raise(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        agenda = _FakeAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        kernel.wake("nope")
        await self._pump()
        self.assertEqual(agenda.next_due_calls, 1)

    async def test_status_is_json_serializable_and_reports_each_agenda(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        due_y = NOW + timedelta(seconds=600)
        store = {"X": _item(NOW - timedelta(seconds=5)), "Y": _item(due_y)}
        agenda = _FakeAgenda("a", clock, store=store)
        agenda.gates["X"] = asyncio.Event()
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        status = kernel.status()
        json.dumps(status)
        self.assertEqual(set(status), {"running", "boot_id", "safety_net_seconds",
                                      "last_error", "agendas"})
        self.assertEqual(set(status["agendas"]["a"]),
                         {"lanes", "in_flight", "next_due", "hot",
                          "backoff_until", "recovered", "last_error"})
        self.assertTrue(status["running"])
        self.assertEqual(status["boot_id"], agenda.recover_ids[0])
        entry = status["agendas"]["a"]
        self.assertEqual(entry["lanes"], 1)
        self.assertEqual(entry["in_flight"], 1)
        self.assertEqual(entry["next_due"], due_y.isoformat())
        self.assertFalse(entry["hot"])
        self.assertIsNone(entry["backoff_until"])
        self.assertEqual(entry["recovered"], 0)
        self.assertIsNone(entry["last_error"])

    async def test_safety_net_caps_the_wait_without_queries(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter, safety_net_seconds=60)
        agenda = _FakeAgenda("a", clock)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(waiter.calls, [60])
        waiter.expire()
        await self._pump()
        self.assertEqual(waiter.calls, [60, 60])
        self.assertEqual(agenda.next_due_calls, 1)

        clock2 = _Clock()
        waiter2 = self._waiter(clock2)
        kernel2, _, _ = self._kernel(clock2, waiter2, safety_net_seconds=60)
        store2 = {"A": _item(clock2() + timedelta(seconds=30))}
        kernel2.register(_FakeAgenda("a", clock2, store=store2))
        await kernel2.start()
        await self._pump()
        self.assertEqual(waiter2.calls, [30])

        clock3 = _Clock()
        waiter3 = self._waiter(clock3)
        kernel3, _, _ = self._kernel(clock3, waiter3, safety_net_seconds=60)
        store3 = {"A": _item(clock3() + timedelta(seconds=120))}
        kernel3.register(_FakeAgenda("a", clock3, store=store3))
        await kernel3.start()
        await self._pump()
        self.assertEqual(waiter3.calls, [60])

        with self.assertRaises(ValueError):
            ScheduleKernel(safety_net_seconds=0)
        with self.assertRaises(ValueError):
            ScheduleKernel(safety_net_seconds=-1)

    async def test_naive_due_is_treated_as_utc(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        naive = NOW.replace(tzinfo=None)
        store = {"A": _item(naive - timedelta(seconds=60)),
                 "B": _item(naive + timedelta(seconds=300))}
        agenda = _FakeAgenda("a", clock, store=store)
        kernel.register(agenda)
        await kernel.start()
        await self._pump()
        self.assertEqual(agenda.runs, ["A"])
        self.assertEqual(waiter.calls[-1], 300.0)

    async def test_default_waiter_and_clock_fire_in_real_time(self):
        event = asyncio.Event()
        event.set()
        self.assertTrue(await _default_wait(event, 5.0))
        self.assertFalse(await _default_wait(asyncio.Event(), 0.01))
        parked = asyncio.Event()
        waiting = asyncio.create_task(_default_wait(parked, None))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(waiting.done())
            parked.set()
            self.assertTrue(await waiting)
        finally:
            if not waiting.done():
                waiting.cancel()
        due = datetime.now(timezone.utc) + timedelta(milliseconds=20)
        agenda = _FakeAgenda("live", _Clock(), store={"A": _item(due)})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kernel = ScheduleKernel(incidents=_FakeIncidents(), runtime_dir=tmp.name)
        self.addAsyncCleanup(kernel.stop)
        kernel.register(agenda)
        await kernel.start()

        async def _poll():
            while not agenda.runs:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(_poll(), timeout=2.0)
        self.assertEqual(agenda.runs, ["A"])

    async def test_package_has_a_single_wait_point_and_no_domain_imports(self):
        banned = {"dispatcher", "task_service", "horizon", "research", "ceo",
                  "objectives", "mechanic", "db", "model_routing", "agents",
                  "orchestration", "main", "state", "apscheduler", "croniter",
                  "motor", "pymongo"}
        package = CORE_DIR / "schedule_kernel"
        files = sorted(package.glob("*.py"))
        self.assertEqual([path.name for path in files],
                         ["__init__.py", "agenda.py", "kernel.py"])
        waits_for = 0
        waiter_calls = 0
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, {"sleep", "call_later", "call_at"},
                                     f"{path.name}: timer primitive")
                    if node.func.attr == "_waiter":
                        waiter_calls += 1
                if isinstance(node, ast.Attribute) and node.attr == "wait_for":
                    waits_for += 1
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], banned,
                                         f"{path.name}: domain import")
                if isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module:
                        self.assertNotIn(node.module.split(".")[0], banned,
                                         f"{path.name}: domain import")
        self.assertEqual(waits_for, 1)
        self.assertEqual(waiter_calls, 1)

    async def test_wake_before_start_is_honoured(self):
        clock = _Clock()
        waiter = self._waiter(clock)
        kernel, _, _ = self._kernel(clock, waiter)
        hot = _EventAgenda("hot", clock)
        hot.events = 1
        cold = _FakeAgenda("cold", clock,
                           store={"P": _item(NOW - timedelta(seconds=5))})
        kernel.register(hot)
        kernel.register(cold)
        kernel.wake("hot")
        kernel.wake("cold")
        await kernel.start()
        await self._pump()
        self.assertEqual(hot.runs, ["cycle"])
        self.assertEqual(cold.runs, ["P"])
