"""Reactive global schedule kernel: one wait point, N domain agendas.

Design: TASK-2026-00441 §5.2-5.3. Each domain keeps its agenda (collection,
handler, rules) and wakes the kernel; the kernel owns the only wait in the
process, per-agenda lanes so a slow handler never blocks another agenda, and
``status()``. Phase 1: contract only, not wired yet — no production import
path reaches this package.

Name map to the §5.3 pseudocode: ``due[a]`` is ``state.due``, ``sucias`` is
``state.dirty``, ``en_vuelo[a]`` is ``len(state.in_flight)``, ``libres`` is
``free``, ``lanzado`` is ``launched``, and «caliente» is ``state.hot``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from reliability.capture import record_incident, resolve_collection
from reliability.models import Component, IncidentKind
from reliability.supervisor import LoopSupervisor
from schedule_kernel.agenda import Agenda, Claim, Outcome

logger = logging.getLogger(__name__)

_METHODS = ("next_due", "claim_due", "run", "settle", "recover_at_boot")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


async def _default_wait(event: asyncio.Event, timeout: float | None) -> bool:
    """Wait for the event; with ``timeout is None`` wait indefinitely.

    There is no default cap: a kernel without a due must not wake up every
    hour. Returns whether it woke by event (``True``) or timeout (``False``).
    """
    if timeout is None:
        await event.wait()
        return True
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


@dataclass(eq=False)
class _AgendaState:
    agenda: Agenda
    name: str
    lanes: int
    hot_on_wake: bool
    lane_supervisor: LoopSupervisor
    query_supervisor: LoopSupervisor
    due: datetime | None = None
    dirty: bool = True
    hot: bool = False
    backoff_until: datetime | None = None
    recovered: int | None = None
    last_error: dict[str, Any] | None = None
    in_flight: dict[str, asyncio.Task] = field(default_factory=dict)

    def backing_off(self, now: datetime) -> bool:
        return self.backoff_until is not None and self.backoff_until > now


class ScheduleKernel:
    """One wait point for every registered agenda (§5.2 surface)."""

    def __init__(
        self,
        *,
        now_fn: Callable[[], datetime] | None = None,
        waiter: Callable[[asyncio.Event, float | None], Awaitable[bool]] | None = None,
        safety_net_seconds: float | None = None,
        incidents: Any = None,
        runtime_dir: Any = None,
    ) -> None:
        if safety_net_seconds is not None:
            if (
                isinstance(safety_net_seconds, bool)
                or not isinstance(safety_net_seconds, (int, float))
                or not safety_net_seconds > 0
            ):
                raise ValueError("safety_net_seconds must be None or > 0")
            self._safety_net: float | None = float(safety_net_seconds)
        else:
            self._safety_net = None
        self._now_fn = now_fn or _utcnow
        self._waiter = waiter or _default_wait
        self._incidents = incidents
        self._runtime_dir = runtime_dir
        self._states: dict[str, _AgendaState] = {}
        self._wake = asyncio.Event()
        self._active = False
        self._stopping = False
        self._boot_id: str | None = None
        self._loop_task: asyncio.Task | None = None
        self._loop_supervisor = LoopSupervisor(
            name="schedule-kernel", component=Component.CORE_PROCESS)
        self._last_error: dict[str, Any] | None = None

    def register(self, agenda: Agenda) -> None:
        """Register an agenda. Before start only; fail-closed declarations."""
        if self._active:
            raise RuntimeError("cannot register while the kernel is running")
        name = getattr(agenda, "name", None)
        if not isinstance(name, str) or not name:
            raise ValueError("agenda name must be a non-empty str")
        if name in self._states:
            raise ValueError(f"duplicate agenda: {name!r}")
        lanes = getattr(agenda, "lanes", 1)
        if isinstance(lanes, bool) or not isinstance(lanes, int) or lanes < 1:
            raise ValueError(f"agenda {name!r}: lanes must be an int >= 1")
        for method in _METHODS:
            if not callable(getattr(agenda, method, None)):
                raise ValueError(f"agenda {name!r} has no callable {method}")
        hot_on_wake = bool(getattr(agenda, "hot_on_wake", False))
        self._states[name] = _AgendaState(
            agenda=agenda,
            name=name,
            lanes=lanes,
            hot_on_wake=hot_on_wake,
            lane_supervisor=LoopSupervisor(
                name=f"schedule-kernel:{name}:lane",
                component=Component.CORE_PROCESS),
            query_supervisor=LoopSupervisor(
                name=f"schedule-kernel:{name}:query",
                component=Component.CORE_PROCESS),
        )

    def wake(self, agenda: str) -> None:
        """Mark one agenda dirty. Synchronous, coalesced, never raises."""
        if not isinstance(agenda, str):
            logger.warning("schedule-kernel: wake with non-str agenda %r", agenda)
            return
        state = self._states.get(agenda)
        if state is None:
            logger.warning("schedule-kernel: wake for unknown agenda %r", agenda)
            return
        state.dirty = True
        state.hot = state.hot or state.hot_on_wake
        self._wake.set()

    async def start(self) -> None:
        """Recover every agenda once, then run the single loop."""
        if self._active:
            return
        self._active = True
        self._stopping = False
        boot_id = uuid.uuid4().hex
        self._boot_id = boot_id
        for state in self._states.values():
            state.dirty = True
            state.due = None
            state.backoff_until = None
            try:
                state.recovered = int(await state.agenda.recover_at_boot(boot_id) or 0)
            except Exception as exc:  # noqa: BLE001 - one broken agenda never blocks boot
                await self._failure(state, state.query_supervisor, exc, "recover_at_boot")
        self._loop_task = asyncio.create_task(self._loop(), name="schedule-kernel")
        logger.warning(
            "schedule-kernel started boot_id=%s agendas=%s",
            boot_id, sorted(self._states))

    async def stop(self) -> None:
        """Cancel the loop first, then the lanes. Idempotent, never raises."""
        self._active = False
        self._stopping = True
        self._wake.set()
        loop, self._loop_task = self._loop_task, None
        if loop is not None:
            loop.cancel()
            try:
                await loop
            except asyncio.CancelledError:
                logger.debug("schedule-kernel loop cancelled")
            except Exception:  # noqa: BLE001 - stop reports, never raises
                logger.warning("schedule-kernel loop failed during stop", exc_info=True)
        lanes = [
            task
            for state in self._states.values()
            for task in state.in_flight.values()
        ]
        for task in lanes:
            task.cancel()
        if lanes:
            await asyncio.gather(*lanes, return_exceptions=True)
        for state in self._states.values():
            state.in_flight.clear()

    def status(self) -> dict[str, Any]:
        """JSON-serializable snapshot: no I/O, class names only in errors."""
        return {
            "running": self._active,
            "boot_id": self._boot_id,
            "safety_net_seconds": self._safety_net,
            "last_error": dict(self._last_error) if self._last_error else None,
            "agendas": {
                name: {
                    "lanes": state.lanes,
                    "in_flight": len(state.in_flight),
                    "next_due": state.due.isoformat() if state.due else None,
                    "hot": state.hot,
                    "backoff_until": state.backoff_until.isoformat()
                    if state.backoff_until else None,
                    "recovered": state.recovered,
                    "last_error": dict(state.last_error) if state.last_error else None,
                }
                for name, state in self._states.items()
            },
        }

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                self._wake.clear()                     # BEFORE working
                now = _aware(self._now_fn())           # one instant per pass
                await self._refresh(now)
                if await self._claim_and_launch(now):
                    continue                           # launched: re-read first
                timeout = self._timeout(now)
                self._loop_supervisor.success()
            except Exception as exc:  # noqa: BLE001 - shared timer reports and backs off; cancellation propagates
                timeout = await self._failure(None, self._loop_supervisor, exc, "loop")
                for state in self._states.values():
                    state.dirty = True
            await self._waiter(self._wake, timeout)    # THE single wait point

    async def _refresh(self, now: datetime) -> None:
        for state in self._states.values():
            if state.dirty and not state.backing_off(now):
                await self._read_due(state)

    async def _read_due(self, state: _AgendaState) -> bool:
        state.dirty = False                            # before the await
        try:
            due = await state.agenda.next_due()
            state.due = None if due is None else _aware(due)
        except Exception as exc:  # noqa: BLE001 - a broken agenda never stops the others
            state.dirty = True
            await self._failure(state, state.query_supervisor, exc, "next_due")
            return False
        state.query_supervisor.success()
        return True

    async def _claim_and_launch(self, now: datetime) -> bool:
        launched = False
        for state in self._states.values():
            overdue = state.due is not None and state.due <= now
            free = state.lanes - len(state.in_flight)
            if state.backing_off(now) or not (state.hot or overdue) or free <= 0:
                continue                               # full lane: the lane end wakes us
            was_hot, state.hot = state.hot, False      # before the await
            try:
                claims = list(await state.agenda.claim_due(now, free) or [])
            except Exception as exc:  # noqa: BLE001 - a broken agenda never stops the others
                state.hot = state.hot or was_hot
                state.dirty = True
                await self._failure(state, state.query_supervisor, exc, "claim_due")
                continue
            state.query_supervisor.success()
            accepted, breaches = self._triage(state, claims, free)
            for claim in accepted:
                self._launch(state, claim)             # no await before the launch
            if accepted:
                state.dirty = True
                launched = True
            for code, detail in breaches:
                await self._breach(state, code, detail)
            if not accepted and overdue and not (state.dirty or state.hot):
                await self._reconsult(state, now)      # lost race: state changed
        return launched

    async def _reconsult(self, state: _AgendaState, now: datetime) -> None:
        if not await self._read_due(state):
            return
        if state.due is not None and state.due <= now and not (state.dirty or state.hot):
            state.due = None                           # no hot retry: next event decides
            await self._breach(
                state, "next_due_not_claimable",
                f"{state.name}: next_due stayed overdue after an empty claim")

    def _triage(
        self, state: _AgendaState, claims: list[Claim], free: int
    ) -> tuple[list[Claim], list[tuple[str, str]]]:
        accepted: list[Claim] = []
        breaches: list[tuple[str, str]] = []
        seen: set[str] = set()
        for claim in claims:
            if claim.boot_id != self._boot_id:
                breaches.append((
                    "foreign_boot_claim",
                    f"{state.name}: claim {claim.item_id!r} has another boot_id"))
            elif claim.item_id in state.in_flight or claim.item_id in seen:
                breaches.append((
                    "duplicate_claim",
                    f"{state.name}: claim {claim.item_id!r} already in flight"))
            elif len(accepted) >= free:
                breaches.append((
                    "claim_over_limit",
                    f"{state.name}: claim {claim.item_id!r} over limit {free}"))
            else:
                seen.add(claim.item_id)
                accepted.append(claim)
        return accepted, breaches

    def _timeout(self, now: datetime) -> float | None:
        # A full-lane overdue agenda contributes nothing: only strictly
        # future candidates count, so the wait is > 0 by construction.
        # Same `now` as the filter: re-reading the clock here would let a
        # due that expired during the pass fall out of the wait.
        candidates = [
            state.backoff_until if state.backing_off(now) else state.due
            for state in self._states.values()
        ]
        futures = [m for m in candidates if m is not None and m > now]
        timeout = (min(futures) - now).total_seconds() if futures else None
        if self._safety_net is None:
            return timeout
        if timeout is None:
            return self._safety_net
        return min(timeout, self._safety_net)

    def _launch(self, state: _AgendaState, claim: Claim) -> None:
        state.in_flight[claim.item_id] = asyncio.create_task(
            self._lane(state, claim), name=f"schedule-kernel:{state.name}")

    async def _lane(self, state: _AgendaState, claim: Claim) -> None:
        try:
            raised = False
            try:
                outcome = await state.agenda.run(claim)
            except asyncio.CancelledError as exc:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise                              # stop(): never settled
                raised = True                          # raised inside: item failure
                outcome = await self._failed_outcome(state, exc)
            except Exception as exc:  # noqa: BLE001 - a handler failure stays in its lane
                raised = True
                outcome = await self._failed_outcome(state, exc)
            try:
                await state.agenda.settle(claim, outcome)
            except Exception as exc:  # noqa: BLE001 - the claim stays for recover_at_boot
                await self._failure(state, state.lane_supervisor, exc, "settle")
            else:
                if not raised:
                    state.lane_supervisor.success()
        finally:                                       # the slot frees with the lane
            state.in_flight.pop(claim.item_id, None)
            state.dirty = True
            self._wake.set()

    async def _failed_outcome(self, state: _AgendaState, exc: BaseException) -> Outcome:
        await self._failure(state, state.lane_supervisor, exc, "run")
        return Outcome(ok=False, error=(str(exc) or type(exc).__name__)[:500])

    async def _failure(
        self,
        state: _AgendaState | None,
        supervisor: LoopSupervisor,
        exc: BaseException,
        phase: str,
    ) -> float:
        label = phase if state is None else f"{state.name}:{phase}"
        wait = await supervisor.failure(
            exc, source=self._incidents, phase=label, runtime_dir=self._runtime_dir)
        error = {"phase": phase, "error": type(exc).__name__, "at": _utcnow().isoformat()}
        if state is None:
            self._last_error = error
        else:
            state.last_error = error
            state.backoff_until = _aware(self._now_fn()) + timedelta(seconds=wait)
        return wait

    async def _breach(self, state: _AgendaState, code: str, detail: str) -> None:
        logger.warning("schedule-kernel: agenda %s breached %s", state.name, code)
        state.last_error = {
            "phase": code, "error": "contract_breach", "at": _utcnow().isoformat()}
        if resolve_collection(self._incidents) is None:
            return                                     # same guard as supervisor.py:94
        await record_incident(
            self._incidents, kind=IncidentKind.INVARIANT_VIOLATION,
            component=Component.CORE_PROCESS, phase=f"{state.name}:{code}",
            invariant_id=f"schedule_kernel.{state.name}.{code}",
            message=detail[:500], runtime_dir=self._runtime_dir)
