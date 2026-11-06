"""Horizon -> global schedule kernel adapter (ticket #567).

Moves Horizon scheduling onto the kernel contract (TASK-2026-00441, phase 3,
§5.5 Horizon row): planning lives in this Agenda and the isolated
``HorizonScheduler`` loop no longer starts in production. The scheduler stays
behaviorally unchanged as the harness for the frozen oracle.

Kept as-is: atomic claim (now also stamping ``boot_id``), expiry before run,
close-out and bounded retry with backoff (60*2^n, cap 1 h, 3 tries, then
failed) in one fenced patch, the same handler, and the same producers.
Changed by the migration: D1 (enqueueing wakes the timer), D2 (every terminal
settle of a recurrent kind rearms that kind's next occurrence, idempotent by
dedupe key; history rows are never mutated, so the rearm is a second write
next to the fenced close-out patch), D3 (boot recovery by boot ownership, no
age threshold; additive ``claimed_boot_id``), and no wait cap (the kernel owns
the single wait; the lane never sleeps).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from horizon.mandate import DEFAULT_TIMEZONE
from horizon.models import TriggerKind, TriggerStatus
from horizon.triggers import (
    DEFAULT_MAX_ATTEMPTS,
    HorizonTriggerStore,
    backoff_seconds,
    dedupe_key,
    upcoming_occurrences,
)
from schedule_kernel.agenda import Claim, Outcome

logger = logging.getLogger(__name__)

__all__ = ["HorizonScheduleAgenda"]

# Kinds with a calendar: each terminal settle rearms the next one. A reactive
# replan is one-shot and an unknown kind has no next occurrence.
_RECURRENT_KINDS = frozenset({
    TriggerKind.DAILY_PLAN.value, TriggerKind.CLOSE_DAY.value,
    TriggerKind.WEEKLY_REVIEW.value, TriggerKind.MONTHLY_REVIEW.value,
})

# Inline attempts for the D2 rearm. The close-out patch above already made the
# item terminal, so a transient I/O failure here would break the chain until
# the next boot re-seeds it; the rearm is idempotent by dedupe key, hence safe
# to retry. No sleep between attempts: lanes=1 stays free. Past the bound the
# error propagates and the kernel records the settle incident (visible).
_REARM_ATTEMPTS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


class HorizonScheduleAgenda:
    """Horizon agenda for the kernel: one serial lane, no waiting of its own.

    ``lanes = 1`` keeps the serial semantics of the old scheduler. The trigger
    math (cadence, dedupe, backoff) stays in ``horizon/triggers.py``; this
    adapter only speaks the kernel contract.
    """

    name = "horizon"
    lanes = 1

    def __init__(
        self,
        *,
        repo: Any,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        settings: Any = None,
        store: HorizonTriggerStore | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._handler = handler
        self._settings = settings
        self._store = store or HorizonTriggerStore(repo, settings=settings)
        self._now = now_fn or _utcnow
        self._boot_id: str | None = None

    @property
    def store(self) -> HorizonTriggerStore:
        """The store used for the D2 rearm; boot binds its waker to the kernel."""
        return self._store

    # ------------------------------------------------------------ Agenda

    async def next_due(self) -> datetime | None:
        """One indexed query for the nearest claimable due."""
        return await self._repo.next_horizon_trigger_due_at()

    async def claim_due(self, now: datetime, limit: int) -> list[Claim]:
        """Claim up to ``limit`` due triggers, sealed with the boot_id.

        Same conditional ``pending`` -> ``running`` update the old loop used.
        The fence is ``(status, claimed_boot_id)``, enforced by the settle
        patch, hence ``fencing_token=0``: the kernel only checks ownership.
        """
        if self._boot_id is None or limit <= 0:
            return []
        due = await self._repo.list_due_horizon_triggers(now, limit=limit)
        claims: list[Claim] = []
        for trigger in due:
            trigger_id = trigger.get("trigger_id")
            if not trigger_id:
                continue
            claimed = await self._repo.mark_horizon_trigger_running(
                str(trigger_id), boot_id=self._boot_id)
            if claimed is None:
                continue  # Lost race: someone else claimed it first.
            claims.append(Claim(item_id=str(trigger_id), boot_id=self._boot_id,
                                fencing_token=0, payload=claimed))
        return claims

    async def run(self, claim: Claim) -> Outcome:
        """Run the handler. A failure is ``ok=False``, never an incident.

        The old scheduler caught every handler exception, logged it and
        retried; ``Outcome(ok=False)`` keeps that semantics. An expired claim
        never reaches the handler: it settles as expired, as before.
        """
        claimed = claim.payload if isinstance(claim.payload, dict) else {}
        if self._is_expired(claimed):
            return Outcome(ok=True, result={"expired": True})
        try:
            result = await self._handler(claimed)
        except Exception as exc:  # noqa: BLE001 - a failure retries, it never takes the lane down
            logger.warning("horizon: trigger %s failed", claim.item_id, exc_info=True)
            return Outcome(ok=False, error=(str(exc) or type(exc).__name__)[:500])
        return Outcome(ok=True, result=result or {})

    async def settle(self, claim: Claim, outcome: Outcome) -> None:
        """One fenced patch, plus the rearm on every terminal settle (D2).

        A stale settle (the boot fence no longer holds) is ignored WITHOUT
        rearming: a newer claim owns the item. A retry does not rearm: the
        same occurrence stays pending. The rearm retries boundedly; past the
        bound it raises so the kernel records the settle incident.
        """
        claimed = claim.payload if isinstance(claim.payload, dict) else None
        if claimed is None or not claim.item_id:
            logger.warning("horizon: settle without a document for %r; left for the next boot",
                           claim.item_id)
            return
        moment = self._now()
        rearm = False
        if outcome.ok:
            if isinstance(outcome.result, dict) and outcome.result.get("expired"):
                updates = {"status": TriggerStatus.EXPIRED.value, "finished_at": moment,
                           "last_error": "expired_before_run"}
            else:
                updates = {"status": TriggerStatus.DONE.value, "finished_at": moment,
                           "result": outcome.result or {}, "last_error": None}
            rearm = True
        else:
            updates, rearm = self._retry_updates(claimed, outcome.error, moment)
        patched = await self._repo.patch_horizon_trigger(
            claim.item_id, updates, boot_id=self._boot_id)
        if patched is None:
            logger.debug("horizon: stale settle ignored for %s", claim.item_id)
            return
        if rearm:
            await self._rearm_with_retry(claimed, moment)

    async def recover_at_boot(self, boot_id: str) -> int:
        """Remember ``boot_id`` FIRST, then release every foreign claim (D3).

        No age threshold: with a single instance per host, any ``running``
        claim of another boot is orphaned at once. One catch-up per item.
        """
        self._boot_id = boot_id
        moment = self._now()
        recovered = await self._repo.recover_horizon_triggers_for_boot(boot_id, now=moment)
        await self._repo.expire_horizon_triggers(moment)
        return int(recovered or 0)

    # ------------------------------------------------------------ settle

    def _is_expired(self, claimed: dict[str, Any]) -> bool:
        expires_at = claimed.get("expires_at")
        if expires_at is None:
            return False
        return _aware(expires_at) <= self._now()

    def _retry_updates(
        self, claimed: dict[str, Any], error: str | None, moment: datetime
    ) -> tuple[dict[str, Any], bool]:
        """Bounded retry with backoff; exhaustion still rearms (D2).

        A failed period is history, but the next period must run anyway:
        without that, one failed occurrence would silently end the chain.
        """
        attempts = int(claimed.get("attempts") or 0) + 1
        max_attempts = int(claimed.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
        if attempts >= max_attempts:
            return {"status": TriggerStatus.FAILED.value, "attempts": attempts,
                    "finished_at": moment, "last_error": error or "unknown"}, True
        return {"status": TriggerStatus.PENDING.value, "attempts": attempts,
                "next_run_at": moment + timedelta(
                    seconds=backoff_seconds(attempts, base=self._store._backoff_base())),
                "last_error": error or "unknown"}, False

    async def _rearm_with_retry(self, claimed: dict[str, Any], moment: datetime) -> None:
        """Rearm with bounded inline retries (FIX_1 F1).

        The item is already terminal when this runs: letting a transient
        failure propagate on the first attempt would break the chain until
        the next boot. On exhaustion the error propagates (never swallowed):
        the kernel records the settle incident and backs off.
        """
        kind = str(claimed.get("kind") or "?")
        user_id = str(claimed.get("user_id") or "?")
        for attempt in range(1, _REARM_ATTEMPTS + 1):
            try:
                await self._rearm(claimed, moment)
                return
            except Exception:  # noqa: BLE001 - transient I/O retries, then the kernel sees it
                if attempt >= _REARM_ATTEMPTS:
                    logger.error(
                        "horizon: rearm failed after %d attempts for %s/%s; "
                        "chain resumes at the next boot", _REARM_ATTEMPTS, kind, user_id)
                    raise
                logger.warning(
                    "horizon: rearm attempt %d/%d failed for %s/%s; retrying",
                    attempt, _REARM_ATTEMPTS, kind, user_id)

    async def _rearm(self, claimed: dict[str, Any], moment: datetime) -> None:
        """Enqueue this kind's next occurrence after ``moment`` (D2).

        Same cadence, times and payload shape as ``ensure_upcoming``; a dedupe
        hit (the occurrence already exists in any state) creates nothing.
        """
        kind = str(claimed.get("kind") or "")
        user_id = str(claimed.get("user_id") or "")
        if kind not in _RECURRENT_KINDS or not user_id:
            return
        settings = self._settings
        occurrences = upcoming_occurrences(
            now=moment, timezone_name=await self._timezone_for(user_id),
            daily_plan_time=str(getattr(settings, "HORIZON_DAILY_PLAN_TIME", "06:00")),
            close_day_time=str(getattr(settings, "HORIZON_CLOSE_DAY_TIME", "21:00")),
            weekly_review_time=str(getattr(settings, "HORIZON_WEEKLY_REVIEW_TIME", "18:00")))
        occurrence = next((o for o in occurrences if o["kind"] == kind), None)
        if occurrence is None:
            return
        key = dedupe_key(kind, user_id, date_key=occurrence.get("date_key"),
                         iso_week=occurrence.get("iso_week"), month=occurrence.get("month"))
        await self._store.enqueue(user_id=user_id, kind=kind, dedupe=key,
                                 run_at=occurrence["run_at"],
                                 payload={"date_key": occurrence.get("date_key")})

    async def _timezone_for(self, user_id: str) -> str:
        """Owner timezone from the profile, settings default when unknown."""
        try:
            profile = await self._repo.get_horizon_profile(user_id)
        except Exception:  # noqa: BLE001 - a broken profile read must not break the rearm
            logger.warning("horizon: profile read failed; using the default timezone")
            profile = None
        mandate = (profile or {}).get("mandate") or {}
        return str(mandate.get("timezone")
                   or getattr(self._settings, "HORIZON_TIMEZONE", None) or DEFAULT_TIMEZONE)
