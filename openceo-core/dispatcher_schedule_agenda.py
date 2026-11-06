"""Dispatcher (+ DeadlineWatch) -> global schedule kernel adapter (ticket #572).

Moves dispatch onto the kernel contract (TASK-2026-00441, phase 6, §5.5
dispatcher row): planning lives in this Agenda and the isolated
``Dispatcher`` loop no longer starts in production. The dispatcher stays
behaviorally unchanged as the harness for the frozen oracle
(``tests/test_dispatcher.py``).

Kept as-is: the event-driven admission (global/project/objective limits,
quota with Tier S reserves, the advisory repo peek, the atomic
per-ticket claim), the conditional 5-minute backup exactly as today (it
exists only while tickets wait), the requeue of due waiting tickets,
and the same producers (``Dispatcher.wake`` stays the entry point, now
a thin delegate to ``kernel.wake("dispatcher")``).
Changed by the migration: the wait is owned by the kernel (single
point, no cap); DeadlineWatch is absorbed as the second ``next_due``
of this same agenda (nearest strictly-future deadline) with a
check-only cycle, so its asyncio timer never arms in production.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from schedule_kernel.agenda import Claim, Outcome

logger = logging.getLogger(__name__)

__all__ = ["DispatcherScheduleAgenda"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DispatcherScheduleAgenda:
    """Dispatcher agenda for the kernel: one serial lane, hot on wake.

    ``lanes = 1`` keeps the serial semantics of the old loop: one
    dispatch cycle in flight per process. ``hot_on_wake = True`` because
    most wakes carry no persisted due (a new queued ticket lands with no
    backup): the wake itself drives the cycle, which always has work to
    check (requeue scan, deadline check, admission, backup refresh).

    The claim is a pseudo-claim over the cycle (``item_id="cycle"``):
    the cycle is idempotent and the atomic per-ticket claim stays inside
    ``dispatch_cycle``, so there is no document to seal and nothing to
    release in ``settle``. ``fencing_token=0``: the kernel only checks
    ownership, and with one lane the id is never in flight twice.
    """

    name = "dispatcher"
    lanes = 1
    hot_on_wake = True

    #: Pseudo-claim id: the cycle, not a document.
    CYCLE_ITEM_ID = "cycle"

    def __init__(
        self,
        *,
        dispatcher: Any,
        watch: Any | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._watch = watch
        self._now = now_fn or _utcnow
        self._boot_id: str | None = None

    # ------------------------------------------------------------ Agenda

    async def next_due(self) -> datetime | None:
        """Nearest of the backup due and the nearest future deadline.

        The backup due may be overdue (it is self-advancing: every cycle
        refreshes it to a strictly future instant or clears it). The
        deadline side only ever returns a strictly-future instant:
        already-due deadlines are checked on every cycle but never drive
        the wait, or a still-hung attempt would re-drive the agenda
        without advancing time. None means an empty queue with nothing
        pending: no timer at all, as today.
        """
        moment = self._now()
        backup_due = await self._dispatcher.backup_due_at(moment)
        deadline_due = None
        if self._watch is not None:
            try:
                deadline_due = await self._watch.next_future_deadline(moment)
            except Exception:  # noqa: BLE001 - deadlines are fail-open; the backup still drives
                logger.warning("dispatcher: deadline due unreadable (fail-open)",
                               exc_info=True)
        candidates = [due for due in (backup_due, deadline_due)
                      if due is not None]
        return min(candidates) if candidates else None

    async def claim_due(self, now: datetime, limit: int) -> list[Claim]:
        """Pseudo-claim the cycle, stamped with the boot_id.

        The kernel only calls this when the agenda is hot or overdue,
        and either always means a cycle is due (an event to admit, a
        backup to refresh, a deadline to check). No persisted
        reservation: the per-ticket atomic claim inside the cycle is
        what serializes against concurrent drivers.
        """
        if self._boot_id is None or limit <= 0:
            return []
        return [Claim(item_id=self.CYCLE_ITEM_ID, boot_id=self._boot_id,
                      fencing_token=0)]

    async def run(self, claim: Claim) -> Outcome:
        """One dispatch cycle, check-only on deadlines.

        Admission, backup and per-ticket claim semantics are exactly the
        dispatcher's; ``arm_deadlines=False`` keeps the watch timer
        disarmed because the kernel owns the only wait. An exception is
        unexpected (store down, a bug): it propagates so the kernel
        records the incident and backs off, as the isolated loop's
        supervisor did.
        """
        result = await self._dispatcher.dispatch_cycle(
            now=self._now(), arm_deadlines=False)
        return Outcome(ok=True, result=result or {})

    async def settle(self, claim: Claim, outcome: Outcome) -> None:
        """No-op: the cycle already persisted everything it decided.

        Parks, launches, claims and the backup refresh all landed inside
        ``run``; no reservation is left to drop. A failed outcome means
        ``run`` raised and the kernel already recorded it; the backoff
        paces the re-drive.
        """
        if not outcome.ok:
            logger.warning("dispatcher: cycle failed for %s: %s",
                           claim.item_id, (outcome.error or "unknown")[:200])

    async def recover_at_boot(self, boot_id: str) -> int:
        """Remember ``boot_id`` FIRST; nothing persisted to release.

        Pseudo-claims live only in the kernel's in-flight map, which dies
        with the process, and the backup doc needs no fence: it is
        recomputed from the queue by every cycle. The boot wake drives
        the startup reconcile where the old ``start`` ran it.
        """
        self._boot_id = boot_id
        return 0
