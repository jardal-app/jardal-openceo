"""Mechanic heartbeat -> global schedule kernel adapter (ticket #573).

Moves the mechanic heartbeat onto the kernel contract (TASK-2026-00441,
final phase, §5.5 heartbeat row): planning lives in this Agenda and the
isolated ``MechanicService`` loop no longer starts in production. The
service stays behaviorally unchanged as the harness for the frozen oracle
(``tests/test_mechanic_510.py``).

Kept as-is: the in-process wake path (``MechanicService.wake`` stays
injected into TaskService and DeadlineWatch ``on_missed``; the queue is
still in memory), the verdict and action vocabulary, the admission and
budget rules, the fix -> deploy -> resume flow, and the
``heartbeat_minutes=30`` default.
Changed by the migration: ``next_heartbeat`` is persisted in the
``mechanic_state`` heartbeat doc instead of living in memory, so a
restart no longer scans at once; the wait is owned by the kernel (single
point, no cap); the post-deploy resume runs from ``boot_resume`` where
``start()`` ran.

This is the only ``heartbeat``-class agenda: the single no-polling
exception in the process (AGENTS.md), kept in one greppable place.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from schedule_kernel.agenda import Claim, Outcome

logger = logging.getLogger(__name__)

__all__ = ["MechanicHeartbeatAgenda"]


class MechanicHeartbeatAgenda:
    """Heartbeat agenda for the kernel: one serial lane, hot on wake.

    ``lanes = 1`` keeps the serial semantics of the old loop: one tick
    in flight per process. ``hot_on_wake = True`` because wakes carry no
    persisted due (the finding queue is in memory): the wake itself
    drives the cycle, which always has work to check (queue drain, and
    the heartbeat scan when due).

    The claim is a pseudo-claim over the tick (``item_id="cycle"``):
    the tick is idempotent and the due doc is self-advancing (every
    scan recomputes it to a strictly future instant), so there is no
    reservation to seal and nothing to release in ``settle``.
    ``fencing_token=0``: the kernel only checks ownership, and with one
    lane the id is never in flight twice.
    """

    name = "mechanic-heartbeat"
    lanes = 1
    hot_on_wake = True

    #: Agenda class. The heartbeat is the only no-polling exception, and
    #: this attribute keeps it in one greppable place (TASK-2026-00441 §5.5).
    agenda_class = "heartbeat"

    #: Pseudo-claim id: the tick, not a document.
    CYCLE_ITEM_ID = "cycle"

    def __init__(self, *, service: Any) -> None:
        self._service = service
        self._boot_id: str | None = None

    # ------------------------------------------------------------ Agenda

    async def next_due(self) -> datetime | None:
        """Persisted next heartbeat; None when never scheduled.

        Shared read with the isolated loop (``tick`` uses the same
        method), so the due the kernel waits on is the one the tick
        advances. None means no scan is scheduled yet: the boot wake
        drives the first cycle, which persists the due.
        """
        return await self._service.heartbeat_due_at()

    async def claim_due(self, now: datetime, limit: int) -> list[Claim]:
        """Pseudo-claim the tick, stamped with the boot_id.

        The kernel only calls this when the agenda is hot or overdue,
        and either always means a tick is due (queued findings to drain,
        a heartbeat scan, or both). No persisted reservation: the tick
        already serializes on its single lane.
        """
        if self._boot_id is None or limit <= 0:
            return []
        return [Claim(item_id=self.CYCLE_ITEM_ID, boot_id=self._boot_id,
                      fencing_token=0)]

    async def run(self, claim: Claim) -> Outcome:
        """One mechanic tick: queued findings first, scan when due.

        Queue drain, heartbeat scan, admission, budgets and the verdict
        run are exactly the service's; the tick also persists the next
        due. An exception is unexpected (store down, a bug): it
        propagates so the kernel records the incident and backs off, as
        the dispatcher's cycle does.
        """
        result = await self._service.tick()
        return Outcome(ok=True, result=result or {})

    async def settle(self, claim: Claim, outcome: Outcome) -> None:
        """No-op: the tick already persisted everything it decided.

        Findings were admitted and run, and the next due was stored
        inside ``run``; no reservation is left to drop. A failed outcome
        means ``run`` raised and the kernel already recorded it; the
        backoff paces the re-drive.
        """
        if not outcome.ok:
            logger.warning("mechanic-heartbeat: tick failed for %s: %s",
                           claim.item_id, (outcome.error or "unknown")[:200])

    async def recover_at_boot(self, boot_id: str) -> int:
        """Remember ``boot_id`` FIRST; nothing persisted to release.

        Pseudo-claims live only in the kernel's in-flight map, which dies
        with the process, and the due doc needs no fence: it is
        recomputed from the clock by every scan. The boot wake drives
        the startup tick where the old ``start`` ran it.
        """
        self._boot_id = boot_id
        return 0
