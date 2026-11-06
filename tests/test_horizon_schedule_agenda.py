"""Horizon agenda of the global schedule kernel: contract with doubles (ticket #567).

Logic/contract only, with doubles and a virtual clock; no live model calls
(Owner order 2026-10-14). Beyond the ``next_due``/``claim_due``/``run``/
``settle``/``recover_at_boot`` contract, this proves the three migration
properties from TASK-2026-00441: enqueueing wakes the timer (D1), the next
occurrence is rearmed in ``settle`` (D2), and boot recovery releases a young
claim of another boot (D3) — with no wait cap anywhere.
"""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from horizon.models import (  # noqa: E402
    HorizonTrigger,
    TriggerKind,
    TriggerStatus,
    document_dump,
)
from horizon.schedule_agenda import HorizonScheduleAgenda  # noqa: E402
from horizon.service import HorizonService  # noqa: E402
from horizon.triggers import (  # noqa: E402
    HorizonTriggerStore,
    dedupe_key,
    expires_at_for,
)
from schedule_kernel import Outcome, ScheduleKernel  # noqa: E402

OWNER = "usr_admin"
NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)  # Monday, 06:00 in La Paz


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "HORIZON_DAILY_PLAN_TIME": "06:00",
        "HORIZON_CLOSE_DAY_TIME": "21:00",
        "HORIZON_WEEKLY_REVIEW_TIME": "18:00",
        "HORIZON_TRIGGER_MAX_ATTEMPTS": 3,
        "HORIZON_TRIGGER_BACKOFF_SECONDS": 60,
        "HORIZON_TIMEZONE": "America/La_Paz",
        "HORIZON_PERSONAL_MIRROR_ENABLED": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

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

    Conditional claim, fenced patch, boot-ownership recovery and dedupe; every
    boundary deep-copies, as the real driver returns snapshots. It proves the
    Agenda contract, not atomicity (covered against real Mongo by the oracle).
    """

    def __init__(self) -> None:
        self.triggers: dict[str, dict] = {}
        self.profiles: dict[str, dict] = {}
        self.next_due_calls = 0
        self.claim_calls = 0

    async def create_horizon_trigger(self, doc: dict) -> dict | None:
        key = doc.get("dedupe_key")
        if key and any(t.get("dedupe_key") == key for t in self.triggers.values()):
            return None
        payload = copy.deepcopy(dict(doc))
        payload.setdefault("attempts", 0)
        payload.setdefault("max_attempts", 3)
        self.triggers[payload["trigger_id"]] = payload
        return copy.deepcopy(payload)

    async def get_horizon_trigger_by_dedupe(self, dedupe: str) -> dict | None:
        for trigger in self.triggers.values():
            if trigger.get("dedupe_key") == dedupe:
                return copy.deepcopy(trigger)
        return None

    async def list_due_horizon_triggers(self, now: datetime, limit: int = 20) -> list[dict]:
        due = [
            copy.deepcopy(t)
            for t in self.triggers.values()
            if t.get("status") == "pending" and _aware(t["next_run_at"]) <= _aware(now)
        ]
        due.sort(key=lambda t: _aware(t["next_run_at"]))
        return due[: max(1, int(limit))]

    async def next_horizon_trigger_due_at(self) -> datetime | None:
        self.next_due_calls += 1
        pending = [
            _aware(t["next_run_at"])
            for t in self.triggers.values()
            if t.get("status") == "pending"
        ]
        return min(pending) if pending else None

    async def mark_horizon_trigger_running(
        self, trigger_id: str, *, boot_id: str | None = None
    ) -> dict | None:
        self.claim_calls += 1
        trigger = self.triggers.get(trigger_id)
        if trigger is None or trigger.get("status") != "pending":
            return None
        trigger["status"] = "running"
        trigger["started_at"] = datetime.now(timezone.utc)
        if boot_id is not None:
            trigger["claimed_boot_id"] = boot_id
        return copy.deepcopy(trigger)

    async def patch_horizon_trigger(
        self, trigger_id: str, updates: dict, *, boot_id: str | None = None
    ) -> dict | None:
        trigger = self.triggers.get(trigger_id)
        if trigger is None:
            return None
        if boot_id is not None and (
            trigger.get("status") != "running"
            or trigger.get("claimed_boot_id") != boot_id
        ):
            return None
        trigger.update(copy.deepcopy(dict(updates)))
        return copy.deepcopy(trigger)

    async def get_horizon_profile(self, user_id: str) -> dict | None:
        row = self.profiles.get(user_id)
        return copy.deepcopy(row) if row else None

    async def upsert_horizon_profile(self, doc: dict) -> dict:
        payload = copy.deepcopy(dict(doc))
        self.profiles[payload["user_id"]] = payload
        return copy.deepcopy(payload)

    async def recover_horizon_triggers_for_boot(
        self, boot_id: str, *, now: datetime | None = None
    ) -> int:
        moment = now or NOW
        recovered = 0
        for trigger in self.triggers.values():
            if trigger.get("status") != "running":
                continue
            if trigger.get("claimed_boot_id") == boot_id:
                continue
            attempts = int(trigger.get("attempts") or 0) + 1
            trigger["status"] = (
                "failed" if attempts >= int(trigger.get("max_attempts") or 3) else "pending"
            )
            trigger["attempts"] = attempts
            trigger["next_run_at"] = moment
            trigger["claimed_boot_id"] = None
            trigger["last_error"] = "recovered_after_restart"
            recovered += 1
        return recovered

    async def recover_stale_horizon_triggers(self, cutoff: datetime) -> int:
        """Age-based recovery of the old loop, kept for the D3 contrast."""
        recovered = 0
        for trigger in self.triggers.values():
            if trigger.get("status") != "running":
                continue
            started = trigger.get("started_at")
            if started is None or _aware(started) >= _aware(cutoff):
                continue
            trigger["status"] = "pending"
            trigger["attempts"] = int(trigger.get("attempts") or 0) + 1
            trigger["last_error"] = "recovered_after_restart"
            recovered += 1
        return recovered

    async def expire_horizon_triggers(self, now: datetime) -> int:
        expired = 0
        for trigger in self.triggers.values():
            if trigger.get("status") != "pending":
                continue
            expires_at = trigger.get("expires_at")
            if expires_at is not None and _aware(expires_at) <= _aware(now):
                trigger["status"] = "expired"
                expired += 1
        return expired


def _seed(repo: _FakeRepo, *, kind: str, dedupe: str, run_at: datetime, **overrides) -> dict:
    doc = document_dump(
        HorizonTrigger(
            user_id=OWNER,
            kind=TriggerKind(kind),
            status=TriggerStatus.PENDING,
            next_run_at=run_at,
            expires_at=expires_at_for(kind, now=run_at),
            attempts=0,
            max_attempts=3,
            dedupe_key=dedupe,
            payload={},
        )
    )
    doc.update(overrides)
    repo.triggers[doc["trigger_id"]] = doc
    return doc


def _seed_profile(repo: _FakeRepo, timezone_name: str = "America/La_Paz") -> None:
    repo.profiles[OWNER] = {"user_id": OWNER, "mandate": {"timezone": timezone_name}}


class AgendaContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.handled: list[dict] = []
        _seed_profile(self.repo)

    async def _handler(self, trigger: dict) -> dict:
        self.handled.append(trigger)
        return {"ok": True}

    def _agenda(self, handler=None, **kw) -> HorizonScheduleAgenda:
        kw.setdefault("settings", _settings())
        return HorizonScheduleAgenda(
            repo=self.repo, handler=handler or self._handler,
            now_fn=self.clock, **kw
        )

    async def _recovered_agenda(self, **kw) -> HorizonScheduleAgenda:
        agenda = self._agenda(**kw)
        await agenda.recover_at_boot("boot-1")
        return agenda

    async def test_next_due_is_one_query_for_the_nearest_claimable(self) -> None:
        _seed(self.repo, kind="daily_plan",
              dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
              run_at=NOW + timedelta(seconds=300))
        _seed(self.repo, kind="close_day",
              dedupe=dedupe_key("close_day", OWNER, date_key="2026-09-21"),
              run_at=NOW + timedelta(seconds=60))
        agenda = await self._recovered_agenda()
        self.assertEqual(await agenda.next_due(), NOW + timedelta(seconds=60))
        self.assertEqual(self.repo.next_due_calls, 1)

    async def test_next_due_none_when_nothing_is_claimable(self) -> None:
        agenda = await self._recovered_agenda()
        self.assertIsNone(await agenda.next_due())

    async def test_claim_due_takes_up_to_the_limit_and_seals_the_boot(self) -> None:
        first = _seed(self.repo, kind="daily_plan",
                      dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                      run_at=NOW - timedelta(seconds=10))
        second = _seed(self.repo, kind="close_day",
                       dedupe=dedupe_key("close_day", OWNER, date_key="2026-09-21"),
                       run_at=NOW - timedelta(seconds=5))
        _seed(self.repo, kind="daily_plan",
              dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-22"),
              run_at=NOW + timedelta(hours=1))
        agenda = await self._recovered_agenda()
        claims = await agenda.claim_due(NOW, 2)
        self.assertEqual([c.item_id for c in claims],
                         [first["trigger_id"], second["trigger_id"]])
        for claim in claims:
            self.assertEqual(claim.boot_id, "boot-1")
            stored = self.repo.triggers[claim.item_id]
            self.assertEqual(
                (stored["status"], stored["claimed_boot_id"]), ("running", "boot-1"))
        # The future one is untouched and a third claim finds nothing due.
        self.assertEqual(await agenda.claim_due(NOW, 2), [])

    async def test_claim_due_without_boot_claims_nothing(self) -> None:
        _seed(self.repo, kind="daily_plan",
              dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
              run_at=NOW - timedelta(seconds=1))
        agenda = self._agenda()
        self.assertEqual(await agenda.claim_due(NOW, 1), [])
        self.assertEqual(self.repo.claim_calls, 0)

    async def test_claim_due_skips_lost_races(self) -> None:
        taken = _seed(self.repo, kind="daily_plan",
                      dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                      run_at=NOW - timedelta(seconds=10))
        free = _seed(self.repo, kind="close_day",
                     dedupe=dedupe_key("close_day", OWNER, date_key="2026-09-21"),
                     run_at=NOW - timedelta(seconds=5))
        agenda = await self._recovered_agenda()
        real_mark = self.repo.mark_horizon_trigger_running

        async def flaky_mark(trigger_id: str, *, boot_id: str | None = None):
            if trigger_id == taken["trigger_id"]:
                return None  # Another worker won between our list and our mark.
            return await real_mark(trigger_id, boot_id=boot_id)

        self.repo.mark_horizon_trigger_running = flaky_mark  # type: ignore[method-assign]
        claims = await agenda.claim_due(NOW, 2)
        self.assertEqual([c.item_id for c in claims], [free["trigger_id"]])
        self.assertEqual(self.repo.triggers[taken["trigger_id"]]["status"], "pending")

    async def test_run_ok_wraps_the_handler_result(self) -> None:
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        outcome = await agenda.run(claim)
        self.assertEqual((outcome.ok, outcome.result), (True, {"ok": True}))
        self.assertEqual(self.handled[0]["trigger_id"], doc["trigger_id"])

    async def test_run_failure_returns_ok_false_with_the_error_text(self) -> None:
        async def failing(_trigger: dict) -> dict:
            raise RuntimeError("boom")

        _seed(self.repo, kind="daily_plan",
              dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
              run_at=NOW - timedelta(seconds=1))
        agenda = self._agenda(handler=failing)
        await agenda.recover_at_boot("boot-1")
        [claim] = await agenda.claim_due(NOW, 1)
        outcome = await agenda.run(claim)
        self.assertEqual((outcome.ok, outcome.error), (False, "boom"))

    async def test_run_expired_never_reaches_the_handler(self) -> None:
        agenda = self._agenda()
        await agenda.recover_at_boot("boot-1")
        # Obsolete after the boot sweep ran: claimed, then discarded in run.
        _seed(self.repo, kind="daily_plan",
              dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-19"),
              run_at=NOW - timedelta(days=2),
              expires_at=NOW - timedelta(hours=1))
        [claim] = await agenda.claim_due(NOW, 1)
        outcome = await agenda.run(claim)
        self.assertEqual((outcome.ok, outcome.result), (True, {"expired": True}))
        self.assertEqual(self.handled, [])

    async def test_settle_ok_marks_done_and_rearms_the_next_occurrence(self) -> None:
        """D2: after a daily_plan runs, the next daily_plan is pending."""
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(minutes=5),
                    payload={"date_key": "2026-09-21"})
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, await agenda.run(claim))
        stored = self.repo.triggers[doc["trigger_id"]]
        self.assertEqual(
            (stored["status"], stored["last_error"]), ("done", None))
        self.assertEqual(stored["result"], {"ok": True})
        # The history row stays done; a new row carries the next period.
        rearmed = await self.repo.get_horizon_trigger_by_dedupe(
            dedupe_key("daily_plan", OWNER, date_key="2026-09-22"))
        self.assertIsNotNone(rearmed)
        assert rearmed is not None
        self.assertEqual(
            (rearmed["status"], rearmed["next_run_at"]),
            ("pending", datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)))
        self.assertNotEqual(rearmed["trigger_id"], doc["trigger_id"])

    async def test_settle_expired_discards_and_rearms(self) -> None:
        agenda = self._agenda()
        await agenda.recover_at_boot("boot-1")
        # Obsolete after the boot sweep ran: claimed, then discarded in run.
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-19"),
                    run_at=NOW - timedelta(days=2),
                    expires_at=NOW - timedelta(hours=1))
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, await agenda.run(claim))
        stored = self.repo.triggers[doc["trigger_id"]]
        self.assertEqual(
            (stored["status"], stored["last_error"]), ("expired", "expired_before_run"))
        self.assertEqual(self.handled, [])
        # The missed period does not end the chain: tomorrow is pending.
        rearmed = await self.repo.get_horizon_trigger_by_dedupe(
            dedupe_key("daily_plan", OWNER, date_key="2026-09-22"))
        self.assertIsNotNone(rearmed)
        assert rearmed is not None
        self.assertEqual(rearmed["status"], "pending")

    async def test_settle_error_retries_with_backoff_and_does_not_rearm(self) -> None:
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, Outcome(ok=False, error="boom"))
        stored = self.repo.triggers[doc["trigger_id"]]
        self.assertEqual(
            (stored["status"], stored["attempts"], stored["last_error"]),
            ("pending", 1, "boom"))
        self.assertEqual(stored["next_run_at"], NOW + timedelta(seconds=60))
        # Same occurrence still pending: no next period yet.
        self.assertEqual(len(self.repo.triggers), 1)

    async def test_settle_error_when_exhausted_fails_but_keeps_the_chain(self) -> None:
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1),
                    max_attempts=1)
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, Outcome(ok=False, error="boom"))
        stored = self.repo.triggers[doc["trigger_id"]]
        self.assertEqual(
            (stored["status"], stored["attempts"]), ("failed", 1))
        # A failed period is history; the next period must still run.
        rearmed = await self.repo.get_horizon_trigger_by_dedupe(
            dedupe_key("daily_plan", OWNER, date_key="2026-09-22"))
        self.assertIsNotNone(rearmed)

    async def test_settle_with_a_stale_fence_is_ignored_without_rearm(self) -> None:
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        # Another boot recovered and reclaimed: this claim is stale.
        self.repo.triggers[doc["trigger_id"]]["claimed_boot_id"] = "boot-2"
        await agenda.settle(claim, Outcome(ok=True, result={"ok": True}))
        stored = self.repo.triggers[doc["trigger_id"]]
        self.assertEqual(
            (stored["status"], stored["claimed_boot_id"]), ("running", "boot-2"))
        self.assertEqual(len(self.repo.triggers), 1)

    async def test_settle_ok_for_a_replan_does_not_rearm(self) -> None:
        doc = _seed(self.repo, kind="reactive_replan",
                    dedupe=dedupe_key("reactive_replan", OWNER,
                                     task_id="t1", status="completed"),
                    run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, await agenda.run(claim))
        self.assertEqual(self.repo.triggers[doc["trigger_id"]]["status"], "done")
        self.assertEqual(len(self.repo.triggers), 1)

    async def test_settle_retries_a_transient_rearm_failure(self) -> None:
        """FIX_1 F1: one blip between patch and rearm must not break the chain."""
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        store = agenda.store
        original = store.enqueue
        calls = 0

        async def flaky(**kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient blip")
            return await original(**kw)

        store.enqueue = flaky  # type: ignore[method-assign]
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, await agenda.run(claim))
        self.assertEqual(self.repo.triggers[doc["trigger_id"]]["status"], "done")
        self.assertEqual(calls, 2)
        rearmed = await self.repo.get_horizon_trigger_by_dedupe(
            dedupe_key("daily_plan", OWNER, date_key="2026-09-22"))
        self.assertIsNotNone(rearmed)
        assert rearmed is not None
        self.assertEqual(rearmed["status"], "pending")

    async def test_settle_reraises_when_the_rearm_keeps_failing(self) -> None:
        """FIX_1 F1: past the bound the error propagates (kernel incident)."""
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        calls = 0

        async def broken(**kw):
            nonlocal calls
            calls += 1
            raise RuntimeError("store down")

        agenda.store.enqueue = broken  # type: ignore[method-assign]
        [claim] = await agenda.claim_due(NOW, 1)
        with self.assertRaises(RuntimeError):
            await agenda.settle(claim, await agenda.run(claim))
        self.assertEqual(calls, 3)  # the _REARM_ATTEMPTS bound: 1 + 2 retries
        # The terminal patch already applied: nothing to recover at boot.
        self.assertEqual(self.repo.triggers[doc["trigger_id"]]["status"], "done")
        self.assertIsNone(await self.repo.get_horizon_trigger_by_dedupe(
            dedupe_key("daily_plan", OWNER, date_key="2026-09-22")))

    async def test_recover_releases_a_young_claim_of_another_boot(self) -> None:
        """D3: seconds old, yet released; by age (900 s) it would be stuck."""
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(hours=1))
        old = self._agenda()
        await old.recover_at_boot("boot-old")
        [claimed] = await old.claim_due(NOW, 1)
        self.assertEqual(claimed.boot_id, "boot-old")
        # The old age rule would NOT release it: it is seconds old.
        stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=900)
        self.assertEqual(
            await self.repo.recover_stale_horizon_triggers(stale_cutoff), 0)
        agenda = self._agenda()
        recovered = await agenda.recover_at_boot("boot-new")
        self.assertEqual(recovered, 1)
        stored = self.repo.triggers[doc["trigger_id"]]
        self.assertEqual(
            (stored["status"], stored["attempts"], stored["last_error"],
             stored["next_run_at"], stored["claimed_boot_id"]),
            ("pending", 1, "recovered_after_restart", NOW, None))
        # And it fires once under the new boot.
        [fresh] = await agenda.claim_due(NOW, 1)
        self.assertEqual(fresh.boot_id, "boot-new")

    async def test_recover_ignores_own_boot_and_releases_legacy_and_expires(self) -> None:
        own = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1))
        legacy = _seed(self.repo, kind="close_day",
                       dedupe=dedupe_key("close_day", OWNER, date_key="2026-09-21"),
                       run_at=NOW - timedelta(seconds=1))
        obsolete = _seed(self.repo, kind="daily_plan",
                         dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-19"),
                         run_at=NOW - timedelta(days=2),
                         expires_at=NOW - timedelta(hours=1))
        agenda = self._agenda()
        # Stage the three shapes: own claim of this boot, legacy claim with no
        # boot field, and an obsolete pending row.
        self.repo.triggers[own["trigger_id"]].update(
            {"status": "running", "claimed_boot_id": "boot-new"})
        self.repo.triggers[legacy["trigger_id"]]["status"] = "running"
        self.assertEqual(await agenda.recover_at_boot("boot-new"), 1)
        self.assertEqual(self.repo.triggers[own["trigger_id"]]["status"], "running")
        self.assertEqual(self.repo.triggers[legacy["trigger_id"]]["status"], "pending")
        self.assertEqual(self.repo.triggers[obsolete["trigger_id"]]["status"], "expired")

    async def test_rearm_without_a_profile_falls_back_to_settings(self) -> None:
        self.repo.profiles.pop(OWNER, None)
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(seconds=1))
        agenda = await self._recovered_agenda()
        [claim] = await agenda.claim_due(NOW, 1)
        await agenda.settle(claim, await agenda.run(claim))
        self.assertEqual(self.repo.triggers[doc["trigger_id"]]["status"], "done")
        rearmed = await self.repo.get_horizon_trigger_by_dedupe(
            dedupe_key("daily_plan", OWNER, date_key="2026-09-22"))
        self.assertIsNotNone(rearmed)


class ProducerWakeTests(unittest.IsolatedAsyncioTestCase):
    """D1: every store that creates a document can wake the kernel."""

    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.wakes = 0

    def _waker(self) -> None:
        self.wakes += 1

    async def test_enqueue_notifies_only_when_it_creates(self) -> None:
        store = HorizonTriggerStore(self.repo, settings=_settings())
        store.bind_waker(self._waker)
        key = dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await store.enqueue(user_id=OWNER, kind="daily_plan", dedupe=key)
        await store.enqueue(user_id=OWNER, kind="daily_plan", dedupe=key)
        self.assertEqual(self.wakes, 1)

    async def test_service_replan_wakes_through_its_disposable_store(self) -> None:
        service = HorizonService(settings=_settings(), repo=self.repo)
        service.bind_waker(self._waker)
        created = await service.enqueue_replan(
            OWNER, task_id="t1", status="completed")
        self.assertTrue(created)
        self.assertEqual(self.wakes, 1)
        # A repeated terminal patch neither duplicates nor wakes again.
        repeated = await service.enqueue_replan(
            OWNER, task_id="t1", status="completed")
        self.assertFalse(repeated)
        self.assertEqual(self.wakes, 1)

    async def test_service_ensure_upcoming_wakes_through_its_disposable_store(self) -> None:
        service = HorizonService(settings=_settings(), repo=self.repo)
        service.bind_waker(self._waker)
        first = await service.ensure_upcoming_triggers(OWNER, now=NOW)
        second = await service.ensure_upcoming_triggers(OWNER, now=NOW)
        self.assertEqual((first, second), (4, 0))
        self.assertEqual(self.wakes, 4)

    async def test_unbound_stores_stay_silent(self) -> None:
        store = HorizonTriggerStore(self.repo, settings=_settings())
        service = HorizonService(settings=_settings(), repo=self.repo)
        key = dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await store.enqueue(user_id=OWNER, kind="daily_plan", dedupe=key)
        await service.enqueue_replan(OWNER, task_id="t1", status="completed")
        self.assertEqual(self.wakes, 0)


class AgendaKernelTests(unittest.IsolatedAsyncioTestCase):
    """The Agenda under the real kernel: D1/D2/D3 end to end, with no cap."""

    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.clock = _Clock()
        self.handled: list[dict] = []
        _seed_profile(self.repo)

    async def _handler(self, trigger: dict) -> dict:
        self.handled.append(trigger)
        return {"ok": True}

    def _wired_kernel(self) -> tuple[ScheduleKernel, HorizonScheduleAgenda, _VirtualWaiter]:
        waiter = _VirtualWaiter(self.clock)
        kernel = ScheduleKernel(now_fn=self.clock, waiter=waiter)
        agenda = HorizonScheduleAgenda(
            repo=self.repo, handler=self._handler,
            settings=_settings(), now_fn=self.clock)
        kernel.register(agenda)
        agenda.store.bind_waker(lambda: kernel.wake(agenda.name))
        return kernel, agenda, waiter

    async def test_enqueue_wakes_the_idle_kernel_with_an_indefinite_wait(self) -> None:
        """D1 with no cap: idle waits forever, enqueue fires at once."""
        kernel, _agenda, waiter = self._wired_kernel()
        store = HorizonTriggerStore(self.repo, settings=_settings())
        store.bind_waker(lambda: kernel.wake("horizon"))
        try:
            await kernel.start()
            await asyncio.sleep(0.05)
            self.assertEqual(self.handled, [])
            key = dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
            await store.enqueue(
                user_id=OWNER, kind="daily_plan", dedupe=key,
                run_at=NOW - timedelta(seconds=1))
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 1)
            # Idle means an indefinite wait, never a 3600 s safety net.
            self.assertIsNone(waiter.calls[0])
            for timeout in waiter.calls:
                self.assertTrue(timeout is None or timeout > 3600)
        finally:
            await kernel.stop()

    async def test_daily_run_rearms_next_and_the_wait_targets_it(self) -> None:
        """D2 under the kernel: one pass runs once and waits 24 h, uncapped."""
        kernel, _agenda, waiter = self._wired_kernel()
        store = HorizonTriggerStore(self.repo, settings=_settings())
        store.bind_waker(lambda: kernel.wake("horizon"))
        key = dedupe_key("daily_plan", OWNER, date_key="2026-09-21")
        await store.enqueue(
            user_id=OWNER, kind="daily_plan", dedupe=key,
            run_at=NOW - timedelta(minutes=5))
        try:
            await kernel.start()
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 1)
            rearmed = await self.repo.get_horizon_trigger_by_dedupe(
                dedupe_key("daily_plan", OWNER, date_key="2026-09-22"))
            self.assertIsNotNone(rearmed)
            # In flight: indefinite; settled: the full 24 h to tomorrow.
            self.assertEqual(waiter.calls, [None, 86400.0])
            self.assertEqual(kernel.status()["agendas"]["horizon"]["recovered"], 0)
        finally:
            await kernel.stop()

    async def test_young_claim_of_another_boot_recovers_and_fires_once(self) -> None:
        """D3 under the kernel: a 5 s old foreign claim is released, then runs."""
        doc = _seed(self.repo, kind="daily_plan",
                    dedupe=dedupe_key("daily_plan", OWNER, date_key="2026-09-21"),
                    run_at=NOW - timedelta(hours=1))
        doc["status"] = "running"
        doc["claimed_boot_id"] = "boot-old"
        doc["started_at"] = datetime.now(timezone.utc) - timedelta(seconds=5)
        kernel, _agenda, _waiter = self._wired_kernel()
        try:
            await kernel.start()
            await asyncio.sleep(0.15)
            self.assertEqual(len(self.handled), 1)
            self.assertTrue(self.handled[0]["next_run_at"] <= NOW)
            status = kernel.status()["agendas"]["horizon"]
            self.assertEqual(status["recovered"], 1)
            stored = self.repo.triggers[doc["trigger_id"]]
            self.assertEqual(stored["status"], "done")
        finally:
            await kernel.stop()


if __name__ == "__main__":
    unittest.main()
