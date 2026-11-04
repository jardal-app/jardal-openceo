"""Single event-driven timer for objective recovery (TASK-2026-00481).

One asyncio task per core, woken by ``_objective_park`` and sleeping to
the nearest persisted ``recovery.due_at``. It is not a poll loop: each
wake is either an event or the nearest due time, capped at one hour as
a defensive ceiling. All decisions are pure (``recovery_policy``);
the claim is a Mongo compare-and-set, so the timer and a concurrent
child close can never plan the same objective twice.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from objectives import recovery_policy as recovery_mod

logger = logging.getLogger(__name__)

__all__ = ["RecoveryScheduler"]


class RecoveryScheduler:
    """Owns the single recovery task; the service owns every decision."""

    def __init__(
        self,
        service: Any,
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._service = service
        self._clock = clock
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._started = False

    def wake(self) -> None:
        """Event-driven nudge (parks and closes call this)."""
        self._wake.set()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        seeded = 0
        try:
            seed = await self._service.seed_recovery_parks()
            seeded = int((seed or {}).get("seeded") or 0)
        except Exception:  # noqa: BLE001 - recovery never blocks boot
            logger.warning("recovery seed failed", exc_info=True)
        self._task = asyncio.create_task(
            self._run(), name="objective-recovery")
        logger.warning("Objective recovery scheduler started (seeded=%s)",
                       seeded)

    async def stop(self) -> None:
        self._stopped = True
        self.wake()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            logger.debug("recovery task cancelled")
        except Exception:  # noqa: BLE001 - shutdown never raises
            logger.warning("recovery task failed on stop", exc_info=True)

    async def _run(self) -> None:
        while not self._stopped:
            try:
                tick = await self._service.recovery_tick()
                delay = int(tick.get("next_seconds")
                            or recovery_mod.MAX_SLEEP_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad objective never stops it
                logger.warning("recovery tick failed", exc_info=True)
                delay = recovery_mod.MAX_SLEEP_SECONDS
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=max(recovery_mod.MIN_SLEEP_SECONDS, int(delay)))
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
