"""CEO reviews -> global schedule kernel adapter (ticket #575).

Moves CEO scheduling onto the kernel contract (TASK-2026-00441, phase 4,
§5.5 CEO row): planning lives in this Agenda and the isolated ``CEOLoop``
no longer starts in production. The loop stays behaviorally unchanged as
the harness for the frozen oracle (``tests/test_ceo_loop.py``).

Kept as-is: the single trigger document (additive ``claimed_boot_id``
only), the mandate cadence intervals, the reactive debounce, the
pending-reasons merge (deduped, newest win), the scheduled-beats-reactive
precedence, the same review runner, the same router-alerts piggyback, and
the same producers (``CEOLoop.wake`` stays the entry point: it stages the
reason, persists it, then delegates to ``kernel.wake("ceo")``).
Changed by the migration: the wait is owned by the kernel (single point,
no 3600 s cap); the debounce due is derived from ``last_reactive_at`` plus
the interval while reasons are pending (no new persisted due field), so a
debounced burst drains on its own due instead of on the cap (D4); the
claim is atomic and sealed with the boot id (D3: boot ownership, no age
threshold). D5 stays out: out-of-process writers get their wake route in
the follow-up.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ceo.loop import (
    _aware,
    _cadence_for,
    _merge_reasons,
    _reactive_min_interval_for,
    _router_reasons_for,
    next_scheduled_after,
)
from schedule_kernel.agenda import Claim, Outcome

logger = logging.getLogger(__name__)

__all__ = ["CeoScheduleAgenda"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _valid_trigger(trigger: Any) -> bool:
    """A trigger the agenda can plan from: a dict with a due date."""
    return (
        isinstance(trigger, dict)
        and _aware(trigger.get("next_scheduled_at")) is not None
    )


def _due_kind(trigger: dict[str, Any], now: datetime, interval: int) -> str | None:
    """Which review the trigger owes: scheduled, reactive, or none.

    Same precedence as the old cycle: an overdue cadence wins and consumes
    the pending reasons; otherwise pending reasons run once the debounce
    elapsed (or at once when no reactive review ever ran).
    """
    due_at = _aware(trigger.get("next_scheduled_at"))
    if due_at is not None and due_at <= now:
        return "scheduled"
    pending = [
        reason
        for reason in (trigger.get("pending_reasons") or [])
        if str(reason or "").strip()
    ]
    if not pending:
        return None
    last_reactive = _aware(trigger.get("last_reactive_at"))
    if last_reactive is None:
        return "reactive"
    if (now - last_reactive).total_seconds() >= interval:
        return "reactive"
    return None


class CeoScheduleAgenda:
    """CEO agenda for the kernel: one serial lane, hot on wake.

    ``lanes = 1`` keeps the serial semantics of the old loop: one review
    in flight per process. ``hot_on_wake = True`` because a wake carries a
    fresh reason the persisted dues cannot see yet: the poke drives a
    claim at once instead of waiting for the next due (§5.3 reactive
    path). A wake inside the debounce claims nothing and the derived
    debounce due owns the wait; the kernel never spins on it.

    The claim is a real conditional seal on the single document
    (``item_id="trigger"``), stamped with the boot id from
    ``recover_at_boot``; ``fencing_token=0`` because the fence is the
    seal itself, enforced by the settle patch. A failed settle never
    wedges the agenda: the claim re-seals the own boot, so the next
    drive retries instead of waiting for a restart.
    """

    name = "ceo"
    lanes = 1
    hot_on_wake = True

    #: Claim id: the single trigger document (``ceo_review_trigger``).
    TRIGGER_ITEM_ID = "trigger"

    def __init__(
        self,
        *,
        repo: Any,
        review_runner: Callable[[str, list[str]], Awaitable[dict[str, Any]]],
        mandate_fn: Callable[[], Any],
        settings: Any = None,
        router_state_dir: Any = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._review_runner = review_runner
        self._mandate_fn = mandate_fn
        self._settings = settings
        self._router_state_dir = router_state_dir
        self._now = now_fn or _utcnow
        self._boot_id: str | None = None

    def _interval(self) -> int:
        return _reactive_min_interval_for(self._settings)

    # ------------------------------------------------------------ Agenda

    async def next_due(self) -> datetime | None:
        """Nearest of the scheduled due and the derived debounce due.

        One single-doc read. The debounce due is derived
        (``last_reactive_at`` plus the interval while reasons are pending),
        never persisted; with pending reasons and no reactive review yet,
        the review is due now. A missing or corrupt trigger fails open to
        now so the claim promptly ensures the document. A seal of another
        boot means nothing claimable for us; our own seal stays readable
        so a settle that failed to land re-drives instead of wedging.
        """
        trigger = await self._repo.get_ceo_trigger()
        if not isinstance(trigger, dict):
            return self._now()
        seal = trigger.get("claimed_boot_id")
        if seal is not None and seal != self._boot_id:
            return None
        due_at = _aware(trigger.get("next_scheduled_at"))
        if due_at is None:
            return self._now()
        candidates = [due_at]
        pending = trigger.get("pending_reasons") or []
        if any(str(reason or "").strip() for reason in pending):
            last_reactive = _aware(trigger.get("last_reactive_at"))
            if last_reactive is None:
                candidates.append(self._now())
            else:
                candidates.append(
                    last_reactive
                    + timedelta(seconds=self._interval())
                )
        return min(candidates)

    async def claim_due(self, now: datetime, limit: int) -> list[Claim]:
        """Seal the trigger when a review is due, stamped with the boot id.

        The ensure runs first (a missing trigger is built, never claimed),
        then the same due-ness the old cycle decided. A foreign seal or a
        debounce that has not elapsed claims nothing; a lost seal (another
        driver won the race) is skipped.
        """
        if self._boot_id is None or limit <= 0:
            return []
        trigger = await self._ensure_trigger()
        seal = trigger.get("claimed_boot_id")
        if seal is not None and seal != self._boot_id:
            return []
        if _due_kind(trigger, now, self._interval()) is None:
            return []
        sealed = await self._repo.claim_ceo_trigger(self._boot_id)
        if sealed is None:
            return []  # Lost race: someone else sealed it first.
        return [Claim(item_id=self.TRIGGER_ITEM_ID, boot_id=self._boot_id,
                      fencing_token=0, payload=sealed)]

    async def run(self, claim: Claim) -> Outcome:
        """One review: scheduled when the cadence is due, else reactive.

        The kind re-derives from the sealed payload, so a cadence that came
        due between the claim and the run still wins, as before. The review
        sees the claim-time reasons plus fresh router alerts; reasons
        flushed during the run stay for the next round, like the wakes
        that used to land mid-review. An exception is unexpected (store
        down, a bug): it propagates so the kernel records the incident
        and backs off, and the settle then releases the seal without
        consuming anything.
        """
        trigger = claim.payload if isinstance(claim.payload, dict) else {}
        now = self._now()
        kind = _due_kind(trigger, now, self._interval())
        if kind is None:  # Unreachable via our own due claim; stay loud.
            logger.warning("ceo: run without a due kind; reviewing anyway")
            kind = "reactive"
        stored = trigger.get("pending_reasons")
        stored = list(stored) if isinstance(stored, list) else []
        reasons = _merge_reasons(stored, [])
        reasons = _merge_reasons(
            reasons, _router_reasons_for(self._router_state_dir, now))
        outcome = await self._review_runner(kind, reasons)
        return Outcome(ok=True, result={
            "kind": kind,
            "action": str((outcome or {}).get("action") or ""),
            "reviewed": len(stored),
            "reviewed_reasons": stored,
            "ran_at": now,
        })

    async def settle(self, claim: Claim, outcome: Outcome) -> None:
        """One fenced patch: record the review and release the seal.

        Success records the same fields as the old loop (the cadence
        advances from the run start) and subtracts the reviewed
        snapshot by multiset: arrivals from during the run survive for
        the next round, even at the cap. Failure (``run`` raised and
        the kernel already recorded it) releases the seal without
        consuming anything, so the next drive retries. A stale settle
        (the fence no longer holds) is ignored.
        """
        if not outcome.ok:
            logger.warning("ceo: review failed for %s: %s",
                           claim.item_id, (outcome.error or "unknown")[:200])
            await self._repo.settle_ceo_trigger(
                claim.boot_id, {}, reviewed=0)
            return
        result = outcome.result if isinstance(outcome.result, dict) else {}
        ran_at = _aware(result.get("ran_at")) or self._now()
        kind = result.get("kind") or "reactive"
        action = str(result.get("action") or "")
        reviewed = int(result.get("reviewed") or 0)
        snapshot = result.get("reviewed_reasons")
        snapshot = list(snapshot) if isinstance(snapshot, list) else None
        if kind == "scheduled":
            updates = {
                "last_scheduled_at": ran_at,
                "next_scheduled_at": next_scheduled_after(
                    _cadence_for(self._mandate_fn), ran_at),
                "last_review_at": ran_at,
                "last_kind": "scheduled",
                "last_outcome": action,
            }
        else:
            updates = {
                "last_reactive_at": ran_at,
                "last_review_at": ran_at,
                "last_kind": "reactive",
                "last_outcome": action,
            }
        if snapshot is None:
            patched = await self._repo.settle_ceo_trigger(
                claim.boot_id, updates, reviewed=reviewed)
        else:
            patched = await self._repo.settle_ceo_trigger(
                claim.boot_id, updates, reviewed=reviewed,
                reviewed_reasons=snapshot)
        if patched is None:
            logger.debug("ceo: stale settle ignored for %s", claim.item_id)

    async def recover_at_boot(self, boot_id: str) -> int:
        """Remember ``boot_id`` FIRST, ensure the trigger, free foreign seals.

        No age threshold: with a single instance per host, a seal of
        another boot is orphaned at once (D3). A rebuild drops an
        orphaned seal the same way the release does, and counts it.
        Overdue work needs no extra kick: the kernel first pass claims
        every due the ensure leaves behind.
        """
        self._boot_id = boot_id
        before = await self._repo.get_ceo_trigger()
        await self._ensure_trigger()
        released = 0
        if not _valid_trigger(before) and isinstance(before, dict):
            seal = before.get("claimed_boot_id")
            if seal is not None and seal != boot_id:
                released = 1
        released += await self._repo.recover_ceo_trigger_for_boot(boot_id)
        return released

    # ------------------------------------------------------------ trigger

    async def _ensure_trigger(self) -> dict[str, Any]:
        """The trigger, rebuilt when missing or corrupt (same shape as the
        loop, and keeping the live pending reasons so a skeleton left by
        an early wake — or a flush landing between the read and the
        write — loses no company event; the atomic ensure owns it)."""
        trigger = await self._repo.get_ceo_trigger()
        if _valid_trigger(trigger):
            assert isinstance(trigger, dict)
            return trigger
        if trigger is not None:
            logger.warning(
                "CEO: trigger missing or corrupt, rebuilding")
        now = self._now()
        pending: list[str] = []
        if isinstance(trigger, dict):
            pending = _merge_reasons(trigger.get("pending_reasons"), [])
        fresh = {
            "next_scheduled_at": next_scheduled_after(
                _cadence_for(self._mandate_fn), now),
            "last_scheduled_at": None,
            "last_review_at": None,
            "last_kind": None,
            "last_outcome": None,
            "last_reactive_at": None,
            "pending_reasons": pending,
            "updated_at": now,
        }
        saved = await self._repo.ensure_ceo_trigger(fresh)
        return saved if isinstance(saved, dict) else fresh
