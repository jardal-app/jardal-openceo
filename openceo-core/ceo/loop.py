"""CEO review loop: scheduled cadence + reactive wake (TASK-2026-00119).

The same persisted-trigger pattern as Horizon and the dispatcher, with
its own company-scope trigger: one ``ceo_review_trigger`` document
carries ``next_scheduled_at`` and the pending reactive reasons; the
loop sleeps until the next scheduled review and any wake interrupts
it. No sweeps, no polling. Reactive reviews are debounced: each one
costs a headless run, so a burst of company events collapses into a
single review instead of a storm.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_INTERVALS",
    "CEOLoop",
    "DEFAULT_CADENCE",
    "next_scheduled_after",
]

#: Mandate cadence -> interval from the last scheduled review.
#: Interval-based on purpose: deterministic, testable, no timezone.
CADENCE_INTERVALS = {
    "diaria": timedelta(days=1),
    "semanal": timedelta(weeks=1),
    "quincenal": timedelta(weeks=2),
    "mensual": timedelta(days=28),
}

#: Cadence when the mandate is missing or unreadable. Without a valid
#: mandate there is no autonomy (reviews escalate deterministically),
#: but the schedule itself must still exist.
DEFAULT_CADENCE = "semanal"

#: Persisted pending reasons cap. Wake bursts collapse; the review
#: sees the latest causes, not an unbounded log.
MAX_PENDING_REASONS = 20

#: In-memory wake queue cap. Same collapse policy before persistence.
MAX_QUEUED_REASONS = 50

#: Sleep cap. Not polling: the clock-jump/suspend net, same as
#: Horizon's (one single-doc read per hour while idle).
MAX_SLEEP_SECONDS = 3600


def _aware(moment: Any) -> datetime | None:
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment if moment.tzinfo else moment.replace(
            tzinfo=timezone.utc)
    return None


def next_scheduled_after(cadence: Any, after: datetime) -> datetime:
    """Next scheduled review. Unknown cadences fall back to weekly."""
    interval = CADENCE_INTERVALS.get(str(cadence or "").strip().lower())
    if interval is None:
        interval = CADENCE_INTERVALS[DEFAULT_CADENCE]
    base = after if after.tzinfo else after.replace(tzinfo=timezone.utc)
    return base + interval


def _merge_reasons(
    stored: Any, fresh: list[str], limit: int = MAX_PENDING_REASONS
) -> list[str]:
    merged: list[str] = []
    for reason in list(stored or []) + list(fresh or []):
        text = str(reason or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged[-limit:]


class CEOLoop:
    """Scheduled + reactive CEO reviews over one persisted trigger.

    ``mandate_fn`` returns the current mandate (or None) every
    iteration, so cadence changes apply without a restart.
    ``review_runner`` runs one review: ``(kind, reasons) -> outcome``.
    ``repo`` persists the trigger (``get_ceo_trigger`` /
    ``save_ceo_trigger``); ``settings`` carries
    ``CEO_REACTIVE_MIN_INTERVAL_SECONDS``.
    """

    def __init__(
        self,
        *,
        repo: Any,
        settings: Any,
        mandate_fn: Callable[[], Any],
        review_runner: Callable[
            [str, list[str]], Awaitable[dict[str, Any]]
        ],
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._mandate_fn = mandate_fn
        self._review_runner = review_runner
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._job: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._queued: list[str] = []

    # ------------------------- lifecycle -------------------------

    def wake(self, reason: str) -> None:
        """A company event asks for a reactive review. Coalesced."""
        try:
            text = str(reason or "").strip()
            if text:
                self._queued.append(text)
                del self._queued[:-MAX_QUEUED_REASONS]
            self._wake.set()
        except Exception:
            logger.warning("CEO loop wake failed", exc_info=True)

    async def start(self) -> None:
        """Ensure the trigger, run an overdue scheduled review, loop."""
        if self._job is not None and not self._job.done():
            return
        self._stop.clear()
        self._wake.clear()
        try:
            await self._ensure_trigger()
            await self._run_overdue_once()
        except Exception:
            # A transient hiccup at boot must not fail the lifespan:
            # the loop re-evaluates on the first wake or sleep cap.
            logger.exception("CEO loop startup reconcile failed")
        self._job = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None

    def _min_reactive_interval(self) -> int:
        try:
            value = int(
                getattr(
                    self._settings,
                    "CEO_REACTIVE_MIN_INTERVAL_SECONDS", 3600,
                )
            )
        except (TypeError, ValueError):
            return 3600
        return max(0, value)

    def _cadence(self) -> str:
        try:
            mandate = self._mandate_fn()
        except Exception:
            logger.warning("CEO: mandate unreadable", exc_info=True)
            return DEFAULT_CADENCE
        if mandate is None:
            return DEFAULT_CADENCE
        revision = getattr(mandate, "revision_ceo", None)
        cadence = str(getattr(revision, "cadencia", "") or "").strip()
        if cadence not in CADENCE_INTERVALS:
            return DEFAULT_CADENCE
        return cadence

    # ------------------------- trigger -------------------------

    async def _ensure_trigger(self) -> dict[str, Any]:
        trigger = await self._repo.get_ceo_trigger()
        if isinstance(trigger, dict) and _aware(
            trigger.get("next_scheduled_at")
        ) is not None:
            return trigger
        if trigger is not None:
            logger.warning(
                "CEO: trigger missing or corrupt, rebuilding")
        now = self._now()
        fresh = {
            "next_scheduled_at": next_scheduled_after(
                self._cadence(), now),
            "last_scheduled_at": None,
            "last_review_at": None,
            "last_kind": None,
            "last_outcome": None,
            "last_reactive_at": None,
            "pending_reasons": [],
            "updated_at": now,
        }
        await self._repo.save_ceo_trigger(fresh)
        return fresh

    async def _run_overdue_once(self) -> None:
        trigger = await self._repo.get_ceo_trigger()
        if not isinstance(trigger, dict):
            return
        due_at = _aware(trigger.get("next_scheduled_at"))
        if due_at is None or due_at > self._now():
            return
        logger.info("CEO: scheduled review overdue, running once")
        await self._run_scheduled(trigger, reasons=[])

    # ------------------------- loop -------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            # Clear BEFORE working: a wake landing mid-review must
            # not be lost. The only safe order (Horizon pattern).
            self._wake.clear()
            try:
                await self._cycle()
                supervisor = self.__dict__.get("_loop_supervisor")
                if supervisor is not None:
                    supervisor.success()
            except Exception as exc:
                # Reliability hunter (TASK-2026-00372): record one exception
                # incident and restart with backoff; transients recovered by
                # the engine only log a WARNING inside the supervisor.
                with suppress(Exception):
                    from reliability.supervisor import LoopSupervisor

                    supervisor = self.__dict__.get("_loop_supervisor")
                    if supervisor is None:
                        supervisor = LoopSupervisor(
                            name="ceo-loop", component="ceo_loop")
                        self.__dict__["_loop_supervisor"] = supervisor
                    await asyncio.sleep(await supervisor.failure(
                        exc, source=self._repo, phase="ceo_cycle"))
                logger.exception(
                    "CEO loop cycle failed; will retry on next wake")
            delay = await self._seconds_until_scheduled()
            try:
                if delay is None:
                    await self._wake.wait()
                else:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=max(0.0, min(delay, MAX_SLEEP_SECONDS)),
                    )
            except asyncio.TimeoutError:
                pass

    async def _seconds_until_scheduled(self) -> float | None:
        try:
            trigger = await self._repo.get_ceo_trigger()
        except Exception:
            logger.warning(
                "CEO: trigger unreadable; retrying shortly",
                exc_info=True,
            )
            return float(MAX_SLEEP_SECONDS)
        if not isinstance(trigger, dict):
            return float(MAX_SLEEP_SECONDS)
        due_at = _aware(trigger.get("next_scheduled_at"))
        if due_at is None:
            return 0.0
        return (due_at - self._now()).total_seconds()

    async def _cycle(self) -> None:
        reasons = self._drain()
        trigger = await self._ensure_trigger()
        now = self._now()
        due_at = _aware(trigger.get("next_scheduled_at"))
        if due_at is not None and due_at <= now:
            await self._run_scheduled(trigger, reasons=reasons)
            return
        if not reasons:
            return
        last_reactive = _aware(trigger.get("last_reactive_at"))
        elapsed = (
            (now - last_reactive).total_seconds()
            if last_reactive is not None else None
        )
        if elapsed is None or elapsed >= self._min_reactive_interval():
            await self._run_reactive(trigger, reasons=reasons)
            return
        # Debounced: the reasons persist for the next review instead
        # of spending a headless run on every burst.
        trigger["pending_reasons"] = _merge_reasons(
            trigger.get("pending_reasons"), reasons)
        trigger["updated_at"] = now
        await self._repo.save_ceo_trigger(trigger)
        logger.info(
            "CEO: reactive review debounced (%d pending reasons)",
            len(trigger["pending_reasons"]),
        )

    def _drain(self) -> list[str]:
        reasons, self._queued = self._queued, []
        return [text for text in reasons if str(text or "").strip()]

    async def _run_scheduled(
        self, trigger: dict[str, Any], *, reasons: list[str]
    ) -> None:
        now = self._now()
        pending = _merge_reasons(trigger.get("pending_reasons"), reasons)
        outcome = await self._review_runner("scheduled", pending)
        trigger["last_scheduled_at"] = now
        trigger["next_scheduled_at"] = next_scheduled_after(
            self._cadence(), now)
        trigger["last_review_at"] = now
        trigger["last_kind"] = "scheduled"
        trigger["last_outcome"] = str((outcome or {}).get("action") or "")
        trigger["pending_reasons"] = []
        trigger["updated_at"] = now
        await self._repo.save_ceo_trigger(trigger)

    async def _run_reactive(
        self, trigger: dict[str, Any], *, reasons: list[str]
    ) -> None:
        now = self._now()
        pending = _merge_reasons(trigger.get("pending_reasons"), reasons)
        outcome = await self._review_runner("reactive", pending)
        trigger["last_reactive_at"] = now
        trigger["last_review_at"] = now
        trigger["last_kind"] = "reactive"
        trigger["last_outcome"] = str((outcome or {}).get("action") or "")
        trigger["pending_reasons"] = []
        trigger["updated_at"] = now
        await self._repo.save_ceo_trigger(trigger)
