"""CEO agenda of the global schedule kernel: contract with doubles (ticket #575).

Logic/contract only, with doubles and a virtual clock; no live model calls
(Owner order 2026-10-14). Beyond the ``next_due``/``claim_due``/``run``/
``settle``/``recover_at_boot`` contract, this proves the migration
properties from TASK-2026-00441 §5.5: the derived debounce due with no new
persisted field and no 3600 s cap (D4), the boot-stamped atomic seal with
no age threshold (D3), the wake that stages, persists and delegates with
the producers untouched, and the hot reactive path that never spins.
Ticket #587 adds the failed-push retry: restaged reasons flush on the
next agenda drive and after a bounded delay, never waiting for a wake.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from ceo.loop import (  # noqa: E402
    CEOLoop,
    FLUSH_RETRY_DELAY_SECONDS,
    MAX_PENDING_REASONS,
)
from ceo.schedule_agenda import CeoScheduleAgenda  # noqa: E402
from schedule_kernel import ScheduleKernel  # noqa: E402

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
WEEK = timedelta(weeks=1)


def _mandate_fn(cadence: str | None = "semanal"):
    def _fn() -> Any:
        if cadence is None:
            return None
        return SimpleNamespace(
            revision_ceo=SimpleNamespace(cadencia=cadence))

    return _fn


def _settings(interval: int = 3600) -> SimpleNamespace:
    return SimpleNamespace(CEO_REACTIVE_MIN_INTERVAL_SECONDS=interval)


def _trigger(now: datetime = NOW, **overrides: Any) -> dict[str, Any]:
    doc = {
        "next_scheduled_at": now + WEEK,
        "last_scheduled_at": now - WEEK,
        "last_review_at": now - WEEK,
        "last_kind": "scheduled",
        "last_outcome": "do_nothing",
        "last_reactive_at": None,
        "pending_reasons": [],
        "updated_at": now,
    }
    doc.update(overrides)
    return doc


class _Clock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or NOW

    def __call__(self) -> datetime:
        return self.now


class _VirtualWaiter:
    """As in the sibling agendas: with the budget spent, park for real."""

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

    Deep-copies every boundary like the driver; the atomic ensure
    keeping live pendings, the conditional seal, the atomic capped
    push, the fenced multiset settle and the boot-ownership recovery
    mirror ``db/mongo.py`` (ticket #575). It proves the Agenda
    contract, not atomicity.
    """

    def __init__(self, trigger: dict[str, Any] | None = None) -> None:
        self.trigger = copy.deepcopy(trigger)
        self.push_calls = 0
        self.fail_push_once = False
        self.fail_seal_once = False

    async def get_ceo_trigger(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.trigger)

    async def save_ceo_trigger(self, doc: dict[str, Any]) -> None:
        self.trigger = copy.deepcopy(dict(doc))

    async def ensure_ceo_trigger(
        self, doc: dict[str, Any]
    ) -> dict[str, Any] | None:
        payload = copy.deepcopy(dict(doc))
        payload.pop("_id", None)
        snapshot = payload.pop("pending_reasons", [])
        snapshot = list(snapshot) if isinstance(snapshot, list) else []
        payload["claimed_boot_id"] = None
        if self.trigger is None:
            self.trigger = {**payload, "pending_reasons": snapshot}
        else:
            self.trigger.update(payload)
            self.trigger["claimed_boot_id"] = None
            if self.trigger.get("pending_reasons") is None:
                self.trigger["pending_reasons"] = snapshot
        return copy.deepcopy(self.trigger)

    async def claim_ceo_trigger(self, boot_id: str) -> dict[str, Any] | None:
        if self.fail_seal_once:
            self.fail_seal_once = False
            return None
        doc = self.trigger
        if doc is None:
            return None
        seal = doc.get("claimed_boot_id")
        if seal is not None and seal != boot_id:
            return None
        doc["claimed_boot_id"] = boot_id
        return copy.deepcopy(doc)

    async def push_ceo_pending_reasons(
        self, reasons: list[str]
    ) -> dict[str, Any] | None:
        self.push_calls += 1
        if self.fail_push_once:
            self.fail_push_once = False
            raise RuntimeError("store down")
        texts = [str(reason or "").strip() for reason in reasons or []]
        texts = [text for text in texts if text]
        if self.trigger is None:
            self.trigger = {"pending_reasons": []}
        pending = self.trigger.get("pending_reasons")
        pending = list(pending) if isinstance(pending, list) else []
        pending.extend(texts)
        self.trigger["pending_reasons"] = pending[-MAX_PENDING_REASONS:]
        return copy.deepcopy(self.trigger)

    async def settle_ceo_trigger(
        self, boot_id: str, updates: dict[str, Any], *,
        reviewed: int = 0,
        reviewed_reasons: list[Any] | None = None,
    ) -> dict[str, Any] | None:
        doc = self.trigger
        if doc is None or doc.get("claimed_boot_id") != boot_id:
            return None
        payload = dict(updates)
        payload.pop("_id", None)
        payload.pop("pending_reasons", None)
        doc.update(payload)
        doc["claimed_boot_id"] = None
        pending = doc.get("pending_reasons")
        pending = list(pending) if isinstance(pending, list) else []
        if reviewed_reasons is None:
            doc["pending_reasons"] = pending[max(0, int(reviewed)):]
        else:
            remaining = list(reviewed_reasons)
            kept: list[Any] = []
            for entry in pending:
                try:
                    remaining.remove(entry)
                except ValueError:
                    kept.append(entry)
            doc["pending_reasons"] = kept
        return copy.deepcopy(doc)

    async def recover_ceo_trigger_for_boot(self, boot_id: str) -> int:
        doc = self.trigger
        if doc is None or "claimed_boot_id" not in doc:
            return 0
        seal = doc.get("claimed_boot_id")
        if seal is None or seal == boot_id:
            return 0
        doc["claimed_boot_id"] = None
        return 1


class _FakeRunner:
    def __init__(self, action: str = "do_nothing") -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.action = action

    async def __call__(
        self, kind: str, reasons: list[str]
    ) -> dict[str, Any]:
        self.calls.append((kind, list(reasons)))
        return {"ok": True, "action": self.action}


class AgendaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo(_trigger())
        self.clock = _Clock()
        self.runner = _FakeRunner()

    def _agenda(self, **kw: Any) -> CeoScheduleAgenda:
        return CeoScheduleAgenda(
            repo=kw.pop("repo", self.repo),
            review_runner=kw.pop("runner", self.runner),
            mandate_fn=kw.pop("mandate_fn", _mandate_fn("semanal")),
            settings=kw.pop("settings", _settings()),
            now_fn=self.clock,
            **kw,
        )

    async def _recovered_agenda(self, **kw: Any) -> CeoScheduleAgenda:
        agenda = self._agenda(**kw)
        await agenda.recover_at_boot("boot-1")
        return agenda

    async def test_shape(self) -> None:
        agenda = self._agenda()
        self.assertEqual(agenda.name, "ceo")
        self.assertEqual(agenda.lanes, 1)
        self.assertTrue(agenda.hot_on_wake)
        self.assertEqual(agenda.TRIGGER_ITEM_ID, "trigger")

    async def test_next_due_is_the_scheduled_due_without_pending(self) -> None:
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), NOW + WEEK)

    async def test_next_due_is_the_minimum_with_pending(self) -> None:
        self.repo.trigger["last_reactive_at"] = NOW - timedelta(seconds=3000)
        self.repo.trigger["pending_reasons"] = ["burst reason"]
        agenda = await self._recovered_agenda()
        # Debounce due (NOW + 600 s) beats the scheduled one (+1 week).
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=600))

    async def test_next_due_is_now_with_pending_and_no_reactive_yet(self) -> None:
        self.repo.trigger["pending_reasons"] = ["first incident"]
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), NOW)

    async def test_next_due_fails_open_to_now_when_missing_or_corrupt(self) -> None:
        # Unrecovered: recover itself would ensure the document.
        agenda = self._agenda(repo=_FakeRepo(None))
        self.assertEqual(await agenda.next_due(), NOW)
        agenda = self._agenda(
            repo=_FakeRepo({"next_scheduled_at": "not-a-date"}))
        self.assertEqual(await agenda.next_due(), NOW)

    async def test_next_due_ignores_blank_only_pending(self) -> None:
        self.repo.trigger["pending_reasons"] = ["   "]
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), NOW + WEEK)

    async def test_next_due_none_under_a_foreign_seal(self) -> None:
        agenda = await self._recovered_agenda()
        # Sealed after the recover (a manual write mid-boot): not ours.
        self.repo.trigger["claimed_boot_id"] = "boot-old"
        self.assertIsNone(await agenda.next_due())

    async def test_next_due_reads_through_the_own_seal(self) -> None:
        self.repo.trigger["claimed_boot_id"] = "boot-1"
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        agenda = await self._recovered_agenda()
        # Self-healing: a settle that failed to land re-drives.
        self.assertEqual(await agenda.next_due(), NOW - timedelta(minutes=1))

    async def test_next_due_propagates_store_errors(self) -> None:
        class _Broken:
            async def get_ceo_trigger(self) -> Any:
                raise RuntimeError("store down")

        agenda = self._agenda(repo=_Broken())
        with self.assertRaises(RuntimeError):
            await agenda.next_due()

    async def test_claim_without_boot_or_limit_claims_nothing(self) -> None:
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 1), [])
        await agenda.recover_at_boot("boot-1")
        self.assertEqual(await agenda.claim_due(NOW, 0), [])

    async def test_claim_seals_a_scheduled_due(self) -> None:
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        self.repo.trigger["pending_reasons"] = ["parked objective"]
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        claim = claims[0]
        self.assertEqual(claim.item_id, "trigger")
        self.assertEqual(claim.boot_id, "boot-1")
        self.assertEqual(claim.fencing_token, 0)
        self.assertEqual(
            claim.payload["pending_reasons"], ["parked objective"])
        self.assertEqual(
            self.repo.trigger["claimed_boot_id"], "boot-1")

    async def test_claim_seals_a_reactive_due(self) -> None:
        self.repo.trigger["pending_reasons"] = ["first incident"]
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].boot_id, "boot-1")

    async def test_claim_inside_the_debounce_claims_nothing(self) -> None:
        self.repo.trigger["last_reactive_at"] = NOW - timedelta(minutes=10)
        self.repo.trigger["pending_reasons"] = ["burst reason"]
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.claim_due(NOW, 1), [])
        self.assertNotIn("claimed_boot_id", self.repo.trigger)

    async def test_claim_idle_claims_nothing(self) -> None:
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.claim_due(NOW, 1), [])

    async def test_claim_never_adopts_a_foreign_seal(self) -> None:
        agenda = await self._recovered_agenda()
        # Sealed after the recover (a manual write mid-boot): not ours.
        self.repo.trigger["claimed_boot_id"] = "boot-old"
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        self.assertEqual(await agenda.claim_due(NOW, 1), [])
        self.assertEqual(
            self.repo.trigger["claimed_boot_id"], "boot-old")

    async def test_claim_reseals_the_own_boot(self) -> None:
        self.repo.trigger["claimed_boot_id"] = "boot-1"
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].boot_id, "boot-1")

    async def test_claim_skips_a_lost_race(self) -> None:
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        self.repo.fail_seal_once = True
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.claim_due(NOW, 1), [])

    async def test_claim_ensures_a_missing_trigger_without_running(self) -> None:
        agenda = await self._recovered_agenda(repo=_FakeRepo(None))
        # recover already ensured it; a second ensure is a no-op.
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(claims, [])
        trigger = agenda._repo.trigger
        self.assertEqual(trigger["next_scheduled_at"], NOW + WEEK)
        self.assertEqual(trigger["pending_reasons"], [])

    async def test_claim_rebuilds_a_corrupt_trigger_carrying_pending(self) -> None:
        repo = _FakeRepo({"next_scheduled_at": "not-a-date",
                          "pending_reasons": ["kept reason"]})
        agenda = await self._recovered_agenda(repo=repo)
        # recover rebuilt it (no seal to claim then); the carried pending
        # plus no reactive yet is due now.
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(
            claims[0].payload["pending_reasons"], ["kept reason"])

    async def test_ensure_keeps_live_pending_when_snapshot_is_stale(
        self,
    ) -> None:
        # R1F1: a flush pushed between the agenda read and the write
        # survives the rebuild; the snapshot only seeds a missing list.
        repo = _FakeRepo({"pending_reasons": ["live incident"]})
        saved = await repo.ensure_ceo_trigger(_trigger(pending_reasons=[]))
        assert saved is not None
        self.assertEqual(
            saved["pending_reasons"], ["live incident"])
        self.assertEqual(
            repo.trigger["pending_reasons"], ["live incident"])
        self.assertEqual(repo.trigger["next_scheduled_at"], NOW + WEEK)

    async def test_ensure_seeds_pending_on_insert(self) -> None:
        repo = _FakeRepo(None)
        saved = await repo.ensure_ceo_trigger(
            _trigger(pending_reasons=["seeded reason"]))
        assert saved is not None
        self.assertEqual(
            saved["pending_reasons"], ["seeded reason"])

    async def _claim_one(self, agenda: CeoScheduleAgenda):
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        return claims[0]

    async def test_run_scheduled_reviews_pending(self) -> None:
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        self.repo.trigger["pending_reasons"] = ["parked objective"]
        agenda = await self._recovered_agenda()
        outcome = await agenda.run(await self._claim_one(agenda))
        self.assertTrue(outcome.ok)
        self.assertEqual(self.runner.calls,
                         [("scheduled", ["parked objective"])])
        self.assertEqual(outcome.result["kind"], "scheduled")
        self.assertEqual(outcome.result["action"], "do_nothing")
        self.assertEqual(outcome.result["reviewed"], 1)
        self.assertEqual(outcome.result["ran_at"], NOW)

    async def test_run_reactive_after_the_debounce(self) -> None:
        self.repo.trigger["last_reactive_at"] = NOW - timedelta(hours=2)
        self.repo.trigger["pending_reasons"] = ["second incident"]
        agenda = await self._recovered_agenda()
        outcome = await agenda.run(await self._claim_one(agenda))
        self.assertTrue(outcome.ok)
        self.assertEqual(self.runner.calls,
                         [("reactive", ["second incident"])])

    async def test_run_propagates_unexpected_errors(self) -> None:
        async def failing(kind: str, reasons: list[str]) -> dict[str, Any]:
            raise RuntimeError("review exploded")

        self.repo.trigger["pending_reasons"] = ["incident"]
        agenda = await self._recovered_agenda(runner=failing)
        with self.assertRaises(RuntimeError):
            await agenda.run(await self._claim_one(agenda))

    async def test_run_adds_fresh_router_alerts(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        alert = {
            "ts": (NOW - timedelta(hours=1)).isoformat(),
            "kind": "no_free_models",
            "source": "update",
            "detail": "catalog publishes zero usable",
        }
        with open(Path(tmp.name) / "alerts.jsonl", "w", encoding="utf-8") as handle:
            handle.write(json.dumps(alert) + "\n")
        self.repo.trigger["pending_reasons"] = ["incident"]
        agenda = self._agenda(router_state_dir=tmp.name)
        await agenda.recover_at_boot("boot-1")
        await agenda.run(await self._claim_one(agenda))
        kind, reasons = self.runner.calls[0]
        self.assertEqual(kind, "reactive")
        self.assertEqual(len(reasons), 2)
        self.assertEqual(reasons[0], "incident")
        self.assertIn("no_free_models", reasons[1])

    async def test_run_counts_raw_pending_slots_as_reviewed(self) -> None:
        self.repo.trigger["pending_reasons"] = ["  ", "incident"]
        agenda = await self._recovered_agenda()
        outcome = await agenda.run(await self._claim_one(agenda))
        # The review sees the merged reasons, but the settle must drop
        # both prefix slots.
        self.assertEqual(self.runner.calls, [("reactive", ["incident"])])
        self.assertEqual(outcome.result["reviewed"], 2)

    async def test_settle_scheduled_advances_and_keeps_arrivals(self) -> None:
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        self.repo.trigger["pending_reasons"] = ["parked objective"]
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        # A wake landing mid-review: it must survive the settle.
        await self.repo.push_ceo_pending_reasons(["mid-review incident"])
        await agenda.settle(claim, outcome)
        stored = self.repo.trigger
        self.assertEqual(stored["next_scheduled_at"], NOW + WEEK)
        self.assertEqual(stored["last_scheduled_at"], NOW)
        self.assertEqual(stored["last_review_at"], NOW)
        self.assertEqual(stored["last_kind"], "scheduled")
        self.assertEqual(stored["last_outcome"], "do_nothing")
        self.assertEqual(stored["pending_reasons"], ["mid-review incident"])
        self.assertIsNone(stored["claimed_boot_id"])

    async def test_settle_reactive_sets_last_reactive_only(self) -> None:
        self.repo.trigger["pending_reasons"] = ["incident"]
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        await agenda.settle(claim, outcome)
        stored = self.repo.trigger
        self.assertEqual(stored["next_scheduled_at"], NOW + WEEK)
        self.assertEqual(stored["last_reactive_at"], NOW)
        self.assertEqual(stored["last_kind"], "reactive")
        self.assertEqual(stored["pending_reasons"], [])
        self.assertIsNone(stored["claimed_boot_id"])

    async def test_settle_keeps_one_copy_of_a_mid_run_rearrival(self) -> None:
        self.repo.trigger["pending_reasons"] = ["flapping incident"]
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        # The same incident re-arrives mid-review: a plain pull would
        # drop both copies, the multiset keeps one for the next round.
        await self.repo.push_ceo_pending_reasons(["flapping incident"])
        self.assertEqual(self.repo.trigger["pending_reasons"],
                         ["flapping incident", "flapping incident"])
        await agenda.settle(claim, outcome)
        self.assertEqual(self.repo.trigger["pending_reasons"],
                         ["flapping incident"])

    async def test_settle_keeps_mid_run_arrivals_at_the_cap(self) -> None:
        # R1F2: 20 pending at claim, 5 arrivals during the run. The
        # capped push evicts 5 already-reviewed oldest entries; the
        # multiset still keeps the 5 new arrivals.
        stored = [f"old-{index}" for index in range(MAX_PENDING_REASONS)]
        self.repo.trigger["pending_reasons"] = stored
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        fresh = [f"new-{index}" for index in range(5)]
        await self.repo.push_ceo_pending_reasons(fresh)
        self.assertEqual(len(self.repo.trigger["pending_reasons"]), 20)
        await agenda.settle(claim, outcome)
        self.assertEqual(self.repo.trigger["pending_reasons"], fresh)

    async def test_settle_failure_releases_without_consuming(self) -> None:
        self.repo.trigger["pending_reasons"] = ["incident"]
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        try:
            raise RuntimeError("review exploded")
        except RuntimeError as exc:
            from schedule_kernel.agenda import Outcome
            failed = Outcome(ok=False, error=str(exc))
        await agenda.settle(claim, failed)
        stored = self.repo.trigger
        self.assertIsNone(stored["claimed_boot_id"])
        self.assertEqual(stored["pending_reasons"], ["incident"])
        self.assertIsNone(stored["last_reactive_at"])
        # Nothing consumed: the next drive retries the same review.
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)

    async def test_settle_stale_is_ignored(self) -> None:
        self.repo.trigger["pending_reasons"] = ["incident"]
        agenda = await self._recovered_agenda()
        claim = await self._claim_one(agenda)
        outcome = await agenda.run(claim)
        self.repo.trigger["claimed_boot_id"] = "boot-2"
        await agenda.settle(claim, outcome)
        stored = self.repo.trigger
        self.assertEqual(stored["claimed_boot_id"], "boot-2")
        self.assertEqual(stored["pending_reasons"], ["incident"])
        self.assertIsNone(stored["last_reactive_at"])

    async def test_recover_remembers_the_boot_first(self) -> None:
        self.repo.trigger["pending_reasons"] = ["incident"]
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 1), [])
        released = await agenda.recover_at_boot("boot-1")
        self.assertEqual(released, 0)
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].boot_id, "boot-1")

    async def test_recover_releases_a_young_foreign_seal(self) -> None:
        self.repo.trigger["claimed_boot_id"] = "boot-old"
        self.repo.trigger["next_scheduled_at"] = NOW - timedelta(minutes=1)
        agenda = self._agenda()
        released = await agenda.recover_at_boot("boot-1")
        self.assertEqual(released, 1)
        self.assertIsNone(self.repo.trigger["claimed_boot_id"])
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)

    async def test_recover_keeps_the_own_seal(self) -> None:
        self.repo.trigger["claimed_boot_id"] = "boot-1"
        agenda = self._agenda()
        self.assertEqual(await agenda.recover_at_boot("boot-1"), 0)
        self.assertEqual(
            self.repo.trigger["claimed_boot_id"], "boot-1")

    async def test_recover_creates_a_missing_trigger(self) -> None:
        repo = _FakeRepo(None)
        agenda = self._agenda(repo=repo)
        self.assertEqual(await agenda.recover_at_boot("boot-1"), 0)
        self.assertEqual(repo.trigger["next_scheduled_at"], NOW + WEEK)
        self.assertEqual(repo.trigger["pending_reasons"], [])

    async def test_recover_rebuilds_corrupt_and_counts_a_foreign_seal(self) -> None:
        repo = _FakeRepo({"next_scheduled_at": "not-a-date",
                          "pending_reasons": ["kept reason"],
                          "claimed_boot_id": "boot-old"})
        agenda = self._agenda(repo=repo)
        self.assertEqual(await agenda.recover_at_boot("boot-1"), 1)
        self.assertEqual(repo.trigger["pending_reasons"], ["kept reason"])
        self.assertIsNone(repo.trigger.get("claimed_boot_id"))
        claims = await agenda.claim_due(NOW, 1)
        self.assertEqual(len(claims), 1)


class WakePathTests(unittest.IsolatedAsyncioTestCase):
    """``CEOLoop.wake`` delegated: stage, persist, then poke the kernel."""

    def _loop(self, repo: _FakeRepo, pokes: list[int]) -> CEOLoop:
        loop = CEOLoop(
            repo=repo,
            settings=_settings(),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=_FakeRunner(),
            now_fn=_Clock(),
        )
        loop.delegate_wake_to(lambda: pokes.append(1))
        return loop

    async def test_wake_persists_then_pokes(self) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        loop.wake("Objective OBJ-1 parked (blocked): vendor down")
        await asyncio.sleep(0.01)
        self.assertEqual(
            repo.trigger["pending_reasons"],
            ["Objective OBJ-1 parked (blocked): vendor down"])
        # Immediate poke plus the re-poke after the push lands.
        self.assertEqual(pokes, [1, 1])
        self.assertEqual(loop._queued, [])

    async def test_concurrent_wakes_land_in_order(self) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        loop.wake("first incident")
        loop.wake("second incident")
        await asyncio.sleep(0.01)
        self.assertEqual(repo.trigger["pending_reasons"],
                         ["first incident", "second incident"])

    async def test_blank_reason_only_pokes(self) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        loop.wake("   ")
        await asyncio.sleep(0.01)
        self.assertEqual(pokes, [1])
        self.assertEqual(repo.push_calls, 0)
        self.assertEqual(repo.trigger["pending_reasons"], [])

    async def test_failed_push_restages_without_raising(self) -> None:
        repo = _FakeRepo(_trigger())
        repo.fail_push_once = True
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        loop.wake("first incident")
        await asyncio.sleep(0.01)
        # No re-poke (nothing landed), the reason waits for the next flush.
        self.assertEqual(pokes, [1])
        self.assertEqual(repo.trigger["pending_reasons"], [])
        loop.wake("second incident")
        await asyncio.sleep(0.01)
        self.assertEqual(repo.trigger["pending_reasons"],
                         ["first incident", "second incident"])

    async def test_undelegated_wake_stays_in_memory(self) -> None:
        repo = _FakeRepo(_trigger())
        loop = CEOLoop(
            repo=repo,
            settings=_settings(),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=_FakeRunner(),
            now_fn=_Clock(),
        )
        loop.wake("first incident")
        self.assertEqual(loop._queued, ["first incident"])
        self.assertEqual(repo.push_calls, 0)
        self.assertTrue(loop._wake.is_set())

    def test_wake_without_a_running_loop_stages_and_pokes(self) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = CEOLoop(
            repo=repo,
            settings=_settings(),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=_FakeRunner(),
            now_fn=_Clock(),
        )
        loop.delegate_wake_to(lambda: pokes.append(1))
        loop.wake("first incident")
        self.assertEqual(loop._queued, ["first incident"])
        self.assertEqual(pokes, [1])


class FlushRetryTests(unittest.IsolatedAsyncioTestCase):
    """Ticket #587: a failed push retries, never waiting for a wake."""

    def _loop(
        self,
        repo: _FakeRepo,
        pokes: list[int],
        delay: float = FLUSH_RETRY_DELAY_SECONDS,
    ) -> CEOLoop:
        loop = CEOLoop(
            repo=repo,
            settings=_settings(),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=_FakeRunner(),
            now_fn=_Clock(),
            flush_retry_delay=delay,
        )
        loop.delegate_wake_to(lambda: pokes.append(1))
        return loop

    def _agenda(self, repo: _FakeRepo) -> CeoScheduleAgenda:
        return CeoScheduleAgenda(
            repo=repo,
            review_runner=_FakeRunner(),
            mandate_fn=_mandate_fn("semanal"),
            settings=_settings(),
            now_fn=_Clock(),
        )

    async def _staged_by_failure(
        self, repo: _FakeRepo, loop: CEOLoop, pokes: list[int]
    ) -> None:
        repo.fail_push_once = True
        loop.wake("first incident")
        await asyncio.sleep(0.01)
        self.assertEqual(pokes, [1])
        self.assertEqual(repo.trigger["pending_reasons"], [])
        self.assertEqual(loop._queued, ["first incident"])

    async def test_failed_push_retries_after_a_bounded_delay(self) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes, delay=0.02)
        try:
            await self._staged_by_failure(repo, loop, pokes)
            await asyncio.sleep(0.1)
            # No second wake: the timer flushed and re-poked.
            self.assertEqual(repo.push_calls, 2)
            self.assertEqual(
                repo.trigger["pending_reasons"], ["first incident"])
            self.assertEqual(pokes, [1, 1])
            self.assertEqual(loop._queued, [])
        finally:
            await loop.stop()

    async def test_concurrent_failures_coalesce_to_one_retry(self) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        try:
            await self._staged_by_failure(repo, loop, pokes)
            first = loop._flush_retry_handle
            self.assertIsNotNone(first)
            repo.fail_push_once = True
            loop.wake("second incident")
            await asyncio.sleep(0.05)
            self.assertIs(loop._flush_retry_handle, first)
            self.assertEqual(
                loop._queued, ["first incident", "second incident"])
        finally:
            await loop.stop()
            self.assertIsNone(loop._flush_retry_handle)

    async def test_next_due_flushes_staged_reasons_before_planning(
        self,
    ) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        try:
            await self._staged_by_failure(repo, loop, pokes)
            agenda = self._agenda(repo)
            await agenda.recover_at_boot("boot-1")
            agenda.bind_pending_flush(loop.flush_queued_reasons)
            due = await agenda.next_due()
            self.assertEqual(
                repo.trigger["pending_reasons"], ["first incident"])
            self.assertEqual(loop._queued, [])
            # Pending with no reactive review yet is due now.
            self.assertEqual(due, NOW)
        finally:
            await loop.stop()

    async def test_claim_due_carries_reasons_flushed_on_a_hot_drive(
        self,
    ) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        try:
            await self._staged_by_failure(repo, loop, pokes)
            agenda = self._agenda(repo)
            await agenda.recover_at_boot("boot-1")
            agenda.bind_pending_flush(loop.flush_queued_reasons)
            claims = await agenda.claim_due(NOW, 1)
            self.assertEqual(len(claims), 1)
            self.assertEqual(
                claims[0].payload["pending_reasons"], ["first incident"])
        finally:
            await loop.stop()

    async def test_drive_flush_failure_stays_fail_open(self) -> None:
        repo = _FakeRepo(_trigger())
        pokes: list[int] = []
        loop = self._loop(repo, pokes)
        try:
            loop._queued.append("staged incident")
            repo.fail_push_once = True
            agenda = self._agenda(repo)
            await agenda.recover_at_boot("boot-1")
            agenda.bind_pending_flush(loop.flush_queued_reasons)
            due = await agenda.next_due()
            self.assertEqual(due, NOW + WEEK)
            self.assertEqual(loop._queued, ["staged incident"])
            self.assertIsNotNone(loop._flush_retry_handle)
        finally:
            await loop.stop()

    async def test_failed_push_reviews_without_a_second_wake(self) -> None:
        start = datetime.now(timezone.utc)
        clock = _Clock(start)
        repo = _FakeRepo(_trigger(start))
        repo.fail_push_once = True
        runner = _FakeRunner()
        kernel = ScheduleKernel(
            now_fn=clock, waiter=_VirtualWaiter(clock))
        agenda = CeoScheduleAgenda(
            repo=repo,
            review_runner=runner,
            mandate_fn=_mandate_fn("semanal"),
            settings=_settings(),
            now_fn=clock,
        )
        kernel.register(agenda)
        loop = CEOLoop(
            repo=repo,
            settings=_settings(),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=runner,
            now_fn=clock,
            flush_retry_delay=0.05,
        )
        loop.delegate_wake_to(lambda: kernel.wake(agenda.name))
        agenda.bind_pending_flush(loop.flush_queued_reasons)
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            loop.wake("first incident")
            await asyncio.sleep(0.3)
            self.assertEqual(
                runner.calls, [("reactive", ["first incident"])])
            self.assertEqual(repo.trigger["pending_reasons"], [])
        finally:
            await kernel.stop()
            await loop.stop()


class AgendaKernelTests(unittest.IsolatedAsyncioTestCase):
    """The Agenda under the real kernel: wake, boot, debounce and no cap."""

    async def asyncSetUp(self) -> None:
        self.clock = _Clock(datetime.now(timezone.utc))
        self.repo = _FakeRepo(_trigger(self.clock()))
        self.runner = _FakeRunner()

    def _wired_kernel(self, budget: int = 0, **kw: Any):
        waiter = _VirtualWaiter(self.clock, budget=budget)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = CeoScheduleAgenda(
            repo=self.repo,
            review_runner=self.runner,
            mandate_fn=_mandate_fn("semanal"),
            settings=kw.pop("settings", _settings()),
            now_fn=self.clock,
        )
        kernel.register(agenda)
        loop = CEOLoop(
            repo=self.repo,
            settings=_settings(),
            mandate_fn=_mandate_fn("semanal"),
            review_runner=self.runner,
            now_fn=self.clock,
        )
        loop.delegate_wake_to(lambda: kernel.wake(agenda.name))
        return kernel, agenda, loop, waiter

    async def test_idle_kernel_waits_uncapped_until_the_schedule(self) -> None:
        kernel, _agenda, _loop, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.runner.calls, [])
            # A full week out: the old loop would have capped this at 3600 s.
            self.assertEqual(waiter.calls, [WEEK.total_seconds()])
        finally:
            await kernel.stop()

    async def test_debounced_burst_drains_on_its_derived_due(self) -> None:
        self.repo.trigger["last_reactive_at"] = self.clock()
        self.repo.trigger["pending_reasons"] = ["burst reason"]
        kernel, _agenda, _loop, waiter = self._wired_kernel(
            budget=1, settings=_settings(interval=7200))
        try:
            await kernel.start()
            await asyncio.sleep(0.15)
            # The wait targets the derived debounce due (2 h), past the
            # old 3600 s cap, and the burst runs when it elapses.
            self.assertEqual(waiter.calls[0], 7200.0)
            self.assertEqual(self.runner.calls,
                             [("reactive", ["burst reason"])])
            self.assertEqual(self.repo.trigger["pending_reasons"], [])
        finally:
            await kernel.stop()

    async def test_wake_runs_a_reactive_review_end_to_end(self) -> None:
        kernel, _agenda, loop, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            loop.wake("Objective OBJ-1 parked (blocked): vendor down")
            await asyncio.sleep(0.15)
            self.assertEqual(self.runner.calls, [(
                "reactive",
                ["Objective OBJ-1 parked (blocked): vendor down"])])
            stored = self.repo.trigger
            self.assertEqual(stored["last_kind"], "reactive")
            self.assertEqual(stored["pending_reasons"], [])
            self.assertIsNone(stored["claimed_boot_id"])
        finally:
            await kernel.stop()

    async def test_overdue_schedule_runs_once_at_boot_without_a_wake(self) -> None:
        self.repo.trigger["next_scheduled_at"] = (
            self.clock() - timedelta(hours=1))
        self.repo.trigger["pending_reasons"] = ["parked objective"]
        kernel, _agenda, _loop, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.runner.calls), 1)
            kind, reasons = self.runner.calls[0]
            self.assertEqual(kind, "scheduled")
            self.assertEqual(reasons, ["parked objective"])
            status = kernel.status()["agendas"]["ceo"]
            self.assertEqual(status["recovered"], 0)
            stored = self.repo.trigger
            self.assertGreater(stored["next_scheduled_at"], self.clock())
            self.assertEqual(stored["pending_reasons"], [])
        finally:
            await kernel.stop()

    async def test_young_foreign_seal_recovers_and_fires_once(self) -> None:
        """D3 under the kernel: a foreign seal is released, then runs."""
        self.repo.trigger["claimed_boot_id"] = "boot-old"
        self.repo.trigger["next_scheduled_at"] = (
            self.clock() - timedelta(hours=1))
        kernel, _agenda, _loop, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.runner.calls), 1)
            status = kernel.status()["agendas"]["ceo"]
            self.assertEqual(status["recovered"], 1)
            self.assertIsNone(self.repo.trigger["claimed_boot_id"])
        finally:
            await kernel.stop()

    async def test_burst_inside_the_debounce_collapses(self) -> None:
        kernel, _agenda, loop, _waiter = self._wired_kernel()
        start = self.clock()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            loop.wake("first incident")
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.runner.calls), 1)
            self.clock.now = start + timedelta(minutes=10)
            loop.wake("second incident")
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.runner.calls), 1)
            self.assertEqual(self.repo.trigger["pending_reasons"],
                             ["second incident"])
            self.clock.now = start + timedelta(hours=1, seconds=1)
            kernel.wake("ceo")
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.runner.calls), 2)
            kind, reasons = self.runner.calls[1]
            self.assertEqual(kind, "reactive")
            self.assertEqual(reasons, ["second incident"])
        finally:
            await kernel.stop()

    async def test_hot_wake_inside_the_debounce_never_spins(self) -> None:
        self.repo.trigger["last_reactive_at"] = self.clock()
        self.repo.trigger["pending_reasons"] = ["burst reason"]
        kernel, _agenda, _loop, waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            kernel.wake("ceo")
            await asyncio.sleep(0.1)
            kernel.wake("ceo")
            await asyncio.sleep(0.1)
            self.assertEqual(self.runner.calls, [])
            # One wait per wake, each targeting the debounce due: hot and
            # empty claims re-arm, they never loop.
            self.assertEqual(len(waiter.calls), 3)
            for timeout in waiter.calls:
                assert timeout is not None
                self.assertGreater(timeout, 3500)
                self.assertLessEqual(timeout, 3600)
        finally:
            await kernel.stop()
