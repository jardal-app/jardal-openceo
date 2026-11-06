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
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "CADENCE_INTERVALS",
    "CEOLoop",
    "DEFAULT_CADENCE",
    "FLUSH_RETRY_DELAY_SECONDS",
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

#: Failed-push retry delay (#587). Not polling: a single timer armed only
#: while staged reasons wait for a persist that failed, plus the flush on
#: the next agenda drive. The old hourly cap tick that re-drained the
#: queue no longer exists (the kernel owns the wait, uncapped), so without
#: this a failed push parked company events in memory until the next wake.
FLUSH_RETRY_DELAY_SECONDS = 60.0

#: Free-router alert freshness. The 6 h refresh re-emits while the
#: fault persists, so anything older saw at least one clean cycle.
ROUTER_ALERT_MAX_AGE_HOURS = 12

#: Router alert kinds that join review reasons (TASK-2026-00427).
ROUTER_ALERT_KINDS = ("no_free_models", "refresh_failed")


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


def _reactive_min_interval_for(settings: Any) -> int:
    """Reactive debounce, shared by the loop and the kernel agenda (#575).

    One normalization, one fail-open rule: a missing or unreadable value
    keeps the 3600 s default, a negative one means no debounce.
    """
    try:
        value = int(
            getattr(
                settings,
                "CEO_REACTIVE_MIN_INTERVAL_SECONDS", 3600,
            )
        )
    except (TypeError, ValueError):
        return 3600
    return max(0, value)


def _cadence_for(mandate_fn: Callable[[], Any]) -> str:
    """Mandate cadence, shared by the loop and the kernel agenda (#575).

    Missing, unreadable or unknown cadences fall back to weekly: without
    a valid mandate there is no autonomy, but the schedule still exists.
    """
    try:
        mandate = mandate_fn()
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


def _router_reasons_for(
    router_state_dir: Any, now: datetime
) -> list[str]:
    """Fresh free-router alerts, shared by the loop and the agenda (#575).

    Never raises (fail-open): one file read piggybacked on an existing
    review, never a new poll.
    """
    if router_state_dir is None:
        return []
    try:
        from model_routing.alerts import list_alerts
    except Exception:
        logger.warning("CEO: router alerts unreadable", exc_info=True)
        return []
    try:
        alerts = list_alerts(Path(router_state_dir))
    except Exception:
        logger.warning("CEO: router alerts unreadable", exc_info=True)
        return []
    cutoff = now - timedelta(hours=ROUTER_ALERT_MAX_AGE_HOURS)
    latest: dict[str, str] = {}
    for alert in alerts:
        if alert.kind not in ROUTER_ALERT_KINDS:
            continue
        try:
            moment = datetime.fromisoformat(alert.ts)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment < cutoff or moment > now:
            continue
        latest[alert.kind] = (
            f"free-router {alert.kind} at {alert.ts}: {alert.detail}"
        )
    return [latest[kind] for kind in ROUTER_ALERT_KINDS if kind in latest]


class CEOLoop:
    """Scheduled + reactive CEO reviews over one persisted trigger.

    ``mandate_fn`` returns the current mandate (or None) every
    iteration, so cadence changes apply without a restart.
    ``review_runner`` runs one review: ``(kind, reasons) -> outcome``.
    ``repo`` persists the trigger (``get_ceo_trigger`` /
    ``save_ceo_trigger``); ``settings`` carries
    ``CEO_REACTIVE_MIN_INTERVAL_SECONDS``.

    Legacy since ticket #575: in production the global schedule kernel
    runs ``CeoScheduleAgenda`` and this loop no longer starts. The
    class stays behaviorally unchanged as the harness for the frozen
    oracle (``tests/test_ceo_loop.py``); ``delegate_wake_to`` redirects
    ``wake`` to the kernel without touching the producers.
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
        router_state_dir: Any = None,
        flush_retry_delay: float = FLUSH_RETRY_DELAY_SECONDS,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._mandate_fn = mandate_fn
        self._review_runner = review_runner
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._router_state_dir = router_state_dir
        self._flush_retry_delay = flush_retry_delay
        self._job: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._queued: list[str] = []
        self._wake_delegate: Callable[[], None] | None = None
        self._flush_lock = asyncio.Lock()
        self._flush_retry_handle: asyncio.TimerHandle | None = None

    # ------------------------- lifecycle -------------------------

    def delegate_wake_to(self, waker: Callable[[], None] | None) -> None:
        """Redirect ``wake`` to the kernel (ticket #575). Unbound, as before."""
        self._wake_delegate = waker

    def wake(self, reason: str) -> None:
        """A company event asks for a reactive review. Coalesced."""
        if self._wake_delegate is not None:
            self._wake_via_kernel(reason)
            return
        try:
            text = str(reason or "").strip()
            if text:
                self._queued.append(text)
                del self._queued[:-MAX_QUEUED_REASONS]
            self._wake.set()
        except Exception:
            logger.warning("CEO loop wake failed", exc_info=True)

    def _wake_via_kernel(self, reason: str) -> None:
        """Stage the reason, persist it, then poke the kernel (#575).

        The producers (TaskService, ObjectiveService, missions) keep
        calling this synchronously and unchanged; the persistence runs as
        a task on the running loop. The immediate poke drives the kernel
        at once, and the flush re-pokes after its push lands, so a claim
        that raced ahead of the push never loses the reason: the second
        poke drives the claim that sees it. A flush that fails (or no
        running loop to flush on) leaves the reasons staged for the next
        agenda drive and the bounded-delay retry (#587). Never raises.
        """
        try:
            text = str(reason or "").strip()
            if text:
                self._queued.append(text)
                del self._queued[:-MAX_QUEUED_REASONS]
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.create_task(self._flush_queued_reasons())
            self._wake_delegate()
        except Exception:
            logger.warning("CEO loop wake failed", exc_info=True)

    async def flush_queued_reasons(self) -> None:
        """Push staged reasons now; the agenda-drive retry hook (#587).

        The kernel agenda awaits this before planning, so reasons staged
        while the store was down (or with no running loop to flush them)
        land on the next drive instead of waiting for the next wake.
        Never raises.
        """
        await self._flush_queued_reasons()

    async def _flush_queued_reasons(self) -> None:
        """Push staged reasons, then re-poke the kernel (ticket #575).

        Serialized so concurrent wakes land in order; the push itself is
        atomic and seal-safe (it never touches the claim). A failed push
        re-stages its reasons and retries them: on the next agenda drive
        (via :meth:`flush_queued_reasons`) and after
        ``FLUSH_RETRY_DELAY_SECONDS`` (#587). Never raises: this runs
        fire-and-forget.
        """
        try:
            async with self._flush_lock:
                reasons, self._queued = self._queued, []
                reasons = [
                    text for text in reasons if str(text or "").strip()]
                if not reasons:
                    return
                try:
                    await self._repo.push_ceo_pending_reasons(reasons)
                except Exception:
                    logger.warning(
                        "CEO: wake reasons persist failed; restaged",
                        exc_info=True)
                    self._queued = _merge_reasons(
                        reasons, self._queued, MAX_QUEUED_REASONS)
                    self._schedule_flush_retry()
                    return
                try:
                    delegate = self._wake_delegate
                    if delegate is not None:
                        delegate()
                except Exception:
                    logger.warning("CEO loop wake failed", exc_info=True)
        except Exception:
            logger.warning("CEO loop wake flush failed", exc_info=True)

    def _schedule_flush_retry(self) -> None:
        """Arm one bounded-delay retry while reasons wait (#587).

        Coalesced: a single timer, re-armed by each failed flush. Firing
        with an empty queue is a no-op (a wake or an agenda drive already
        drained it). With no running loop there is nothing to arm on and
        the next agenda drive owns the retry. Never raises.
        """
        try:
            if not self._queued:
                return
            handle = self._flush_retry_handle
            if handle is not None and not handle.cancelled():
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                return
            delay = max(0.0, float(self._flush_retry_delay))
            self._flush_retry_handle = running.call_later(
                delay, self._on_flush_retry_due)
        except Exception:
            logger.warning(
                "CEO loop flush retry not scheduled", exc_info=True)

    def _on_flush_retry_due(self) -> None:
        """Fire the bounded-delay retry as a flush task (#587)."""
        self._flush_retry_handle = None
        try:
            if not self._queued:
                return
            running = asyncio.get_running_loop()
        except RuntimeError:
            return
        except Exception:
            logger.warning("CEO loop flush retry failed", exc_info=True)
            return
        try:
            running.create_task(self._flush_queued_reasons())
        except Exception:
            logger.warning("CEO loop flush retry failed", exc_info=True)

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
        handle, self._flush_retry_handle = self._flush_retry_handle, None
        if handle is not None and not handle.cancelled():
            handle.cancel()
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None

    def _min_reactive_interval(self) -> int:
        return _reactive_min_interval_for(self._settings)

    def _cadence(self) -> str:
        return _cadence_for(self._mandate_fn)

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
                try:
                    from reliability.supervisor import LoopSupervisor
                except Exception:
                    logger.warning("CEO loop reliability wiring unavailable",
                                   exc_info=True)
                    supervisor = None
                else:
                    supervisor = self.__dict__.get("_loop_supervisor")
                    if supervisor is None:
                        supervisor = LoopSupervisor(
                            name="ceo-loop", component="ceo_loop")
                        self.__dict__["_loop_supervisor"] = supervisor
                if supervisor is not None:
                    try:
                        await asyncio.sleep(await supervisor.failure(
                            exc, source=self._repo, phase="ceo_cycle"))
                    except Exception:
                        logger.warning("CEO loop incident capture failed",
                                       exc_info=True)
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
            # Idle tick with a debounced backlog: re-offer the persisted
            # pendings so a burst with no follow-up event still gets its
            # deferred review once the interval elapsed (merge dedupes;
            # the check below keeps the debounce inside the interval).
            reasons = _merge_reasons(trigger.get("pending_reasons"), [])
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

    def _router_reasons(self) -> list[str]:
        """Fresh free-router alerts; never raises (fail-open).

        The refresh already screams into alerts.jsonl, but nothing
        reads it (TASK-2026-00427 went unnoticed for two days). One
        file read piggybacked on an existing review, never a new poll.
        """
        return _router_reasons_for(self._router_state_dir, self._now())

    async def _run_scheduled(
        self, trigger: dict[str, Any], *, reasons: list[str]
    ) -> None:
        now = self._now()
        pending = _merge_reasons(trigger.get("pending_reasons"), reasons)
        pending = _merge_reasons(pending, self._router_reasons())
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
        pending = _merge_reasons(pending, self._router_reasons())
        outcome = await self._review_runner("reactive", pending)
        trigger["last_reactive_at"] = now
        trigger["last_review_at"] = now
        trigger["last_kind"] = "reactive"
        trigger["last_outcome"] = str((outcome or {}).get("action") or "")
        trigger["pending_reasons"] = []
        trigger["updated_at"] = now
        await self._repo.save_ceo_trigger(trigger)
