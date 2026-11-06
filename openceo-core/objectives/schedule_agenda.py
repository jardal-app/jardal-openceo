"""Objectives recovery -> global schedule kernel adapter (ticket #570).

Moves objective recovery onto the kernel contract (TASK-2026-00441, phase 5,
§5.5 recovery row): planning lives in this Agenda and the isolated
``RecoveryScheduler`` loop no longer starts in production. The scheduler stays
behaviorally unchanged as the harness for the frozen oracle
(``tests/test_objective_recovery_481.py``, ``tests/test_recovery_deploy_483.py``).

Kept as-is: the persisted ``recovery.due_at`` per park, every
``recovery_policy`` decision (retry/resume/ticket/deploy/defer/none), the
compare-and-set attempt spend (``claim_objective_recovery``), the bounded
retry budget with backoff, the recovery ticket flow, the deploy restart on an
idle service, and the same producers (every park wakes).
Changed by the migration: the hourly re-read of up to 100 waiting objectives
plus their children (D4) becomes one indexed query over
``(status, recovery.due_at)``; the claim is sealed with the ``boot_id`` (D3:
boot ownership, no age threshold); the wait is owned by the kernel (single
point, no cap); spent parks carry no due, so the agenda is hot on wake and
the wait query reports now while an unsealed no-due exhausted park exists
(#586), draining a backlog one per lane end without further wakes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from schedule_kernel.agenda import Claim, Outcome

logger = logging.getLogger(__name__)

__all__ = ["ObjectivesRecoveryAgenda"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ObjectivesRecoveryAgenda:
    """Recovery agenda for the kernel: one serial lane, no waiting of its own.

    ``lanes = 1`` keeps the serial semantics of the old scheduler: one
    recovery in flight per process. ``hot_on_wake = True`` because spent
    parks carry no due: the wake every park already sends drives their
    ticket round at once, and the wait query stays due now while any
    unsealed no-due exhausted park remains (#586). The run handler is
    total (it decides and acts per objective, liquidating domain failures
    as re-parks or ticket state), so an exception out of ``run`` is
    unexpected and propagates to the kernel's supervisor; ``settle``
    releases the seal either way.
    """

    name = "objectives-recovery"
    lanes = 1
    hot_on_wake = True

    def __init__(
        self,
        *,
        repo: Any,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._handler = handler
        self._now = now_fn or _utcnow
        self._boot_id: str | None = None

    # ------------------------------------------------------------ Agenda

    async def next_due(self) -> datetime | None:
        """Nearest claimable due; now while a spent park waits (#586)."""
        return await self._repo.next_objective_recovery_due_at()

    async def claim_due(self, now: datetime, limit: int) -> list[Claim]:
        """Seal up to ``limit`` due recoveries, stamped with the boot_id.

        Same shape as the horizon sister: list the due candidates, then
        seal each with its own conditional update. A lost seal (the park
        moved under us) is skipped; the fence is ``claimed_boot_id``,
        enforced by the settle release, hence ``fencing_token=0``.
        """
        if self._boot_id is None or limit <= 0:
            return []
        due = await self._repo.list_due_objective_recoveries(now, limit=limit)
        claims: list[Claim] = []
        for candidate in due:
            objective_id = candidate.get("objective_id")
            if not objective_id:
                continue
            recovery = candidate.get("recovery")
            expected = recovery.get("due_at") \
                if isinstance(recovery, dict) else None
            sealed = await self._repo.seal_objective_recovery(
                str(objective_id), expected, self._boot_id)
            if sealed is None:
                continue  # Lost race: the park moved under us.
            claims.append(Claim(item_id=str(objective_id),
                                boot_id=self._boot_id,
                                fencing_token=0, payload=sealed))
        return claims

    async def run(self, claim: Claim) -> Outcome:
        """Run the handler. Domain failures are already liquidated inside it.

        The handler decides and acts per objective (retry/resume/ticket/
        deploy/defer/none) and persists the outcome itself; what returns is
        the record. An exception is unexpected (I/O down, a bug): it
        propagates so the kernel records the incident and backs off.
        """
        claimed = claim.payload if isinstance(claim.payload, dict) else {}
        result = await self._handler(claimed)
        return Outcome(ok=True, result=result or {})

    async def settle(self, claim: Claim, outcome: Outcome) -> None:
        """Release the seal when this boot still owns it; stale is ignored.

        The item already sits in its next state (active after a replan,
        waiting with a fresh due after a re-park, spent after a ticket):
        only the reservation is left to drop. A failed outcome means
        ``run`` raised and the kernel already recorded it; the release is
        the same, and the backoff paces the re-drive.
        """
        if not outcome.ok:
            logger.warning("objectives-recovery: run failed for %s: %s",
                           claim.item_id, (outcome.error or "unknown")[:200])
        released = await self._repo.release_objective_recovery_claim(
            claim.item_id, claim.boot_id)
        if released is None:
            logger.debug("objectives-recovery: stale settle ignored for %s",
                         claim.item_id)

    async def recover_at_boot(self, boot_id: str) -> int:
        """Remember ``boot_id`` FIRST, then release foreign seals (D3).

        No age threshold: with a single instance per host, any seal of
        another boot is orphaned at once. Deploy parks requested before
        this boot are due now: the restart they waited for happened. The
        legacy seed stays in the lifespan next to the registration, where
        the old scheduler start used to run it.
        """
        self._boot_id = boot_id
        moment = self._now()
        released = await self._repo.release_objective_recovery_claims_for_boot(
            boot_id)
        marked = await self._repo.mark_objective_deploys_due_for_boot(moment)
        return int(released or 0) + int(marked or 0)
