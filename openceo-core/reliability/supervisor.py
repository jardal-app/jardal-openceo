"""Reusable supervisor for background loops (design v2 section 5.2).

A loop failure records one ``exception`` incident and restarts with backoff
instead of dying. Transient errors the engine already recovered never become
incidents: they go to WARNING. Never raises; the loop always survives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .capture import record_incident, resolve_collection, transient_recovered
from .models import Component, IncidentKind

logger = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0


def backoff_for(attempt: int, *,
                base: float = BACKOFF_BASE_SECONDS,
                cap: float = BACKOFF_MAX_SECONDS) -> float:
    """Exponential backoff in seconds for the nth consecutive failure."""
    try:
        streak = max(0, int(attempt))
    except (TypeError, ValueError):
        streak = 0
    return min(float(cap), float(base) * (2.0 ** streak))


class LoopSupervisor:
    """Streak-aware restart helper owned by one background loop."""

    def __init__(self, *, name: str, component: Component | str,
                 base: float = BACKOFF_BASE_SECONDS,
                 cap: float = BACKOFF_MAX_SECONDS) -> None:
        self._name = str(name)
        self._component = component
        self._base = base
        self._cap = cap
        self._streak = 0

    @property
    def streak(self) -> int:
        return self._streak

    def success(self) -> None:
        self._streak = 0

    def backoff(self) -> float:
        return backoff_for(self._streak, base=self._base, cap=self._cap)

    async def failure(
        self,
        exc: BaseException,
        *,
        source: Any = None,
        phase: str | None = None,
        release: dict[str, Any] | None = None,
        runtime_dir: Any = None,
        notify: Any = None,
    ) -> float:
        """Log, maybe record, and return the seconds the loop must wait."""
        try:
            self._streak += 1
            wait = self.backoff()
            if transient_recovered(str(exc)):
                logger.warning(
                    "%s recovered transient error; no incident (%s)",
                    self._name, exc)
                return wait
            logger.exception("%s cycle failed; restarting in %.1fs",
                             self._name, wait)
            if resolve_collection(source) is None:
                logger.warning(
                    "%s has no incidents collection; skipping capture",
                    self._name)
                return wait
            try:
                await record_incident(
                    source, kind=IncidentKind.EXCEPTION,
                    component=self._component, exc_type=type(exc).__name__,
                    exc_text=str(exc)[:500], phase=phase, release=release,
                    runtime_dir=runtime_dir, notify=notify)
            except Exception:
                logger.warning("%s incident capture failed",
                               self._name, exc_info=True)
            return wait
        except Exception:
            logger.warning("%s supervisor failed", self._name, exc_info=True)
            return BACKOFF_BASE_SECONDS


async def handle_loop_error(
    exc: BaseException,
    *,
    name: str,
    component: Component | str,
    source: Any = None,
    attempt: int = 0,
    phase: str | None = None,
    release: dict[str, Any] | None = None,
    runtime_dir: Any = None,
    notify: Any = None,
) -> float:
    """One-shot loop failure: record and return the backoff to sleep."""
    supervisor = LoopSupervisor(name=name, component=component)
    supervisor._streak = max(0, int(attempt))
    wait = await supervisor.failure(
        exc, source=source, phase=phase, release=release,
        runtime_dir=runtime_dir, notify=notify)
    await asyncio.sleep(wait)
    return wait
