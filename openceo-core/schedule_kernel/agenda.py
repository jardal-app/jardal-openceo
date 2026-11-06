"""Agenda contract for the reactive global schedule kernel.

A domain keeps its collection, handler and rules; this protocol is the only
surface the kernel speaks to it through (TASK-2026-00441 §5.2). No I/O here:
pure value types plus the protocol every adapter implements.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Claim:
    """One claimed unit of work.

    ``item_id`` identifies the unit inside its agenda; pseudo-claims over a
    single document (CEO trigger, dispatcher cycle) use a constant such as
    "cycle". ``boot_id`` is the id the agenda received in
    ``recover_at_boot``; ``fencing_token`` orders claims so a stale settle is
    ignored (pseudo-claims use 0). ``payload`` is the domain document; the
    kernel carries it but never reads it.
    """

    item_id: str
    boot_id: str
    fencing_token: int
    payload: Any = None


@dataclass(frozen=True)
class Outcome:
    """What a handler run produced.

    ``ok=False`` is a domain-level failure the adapter already accounted for:
    the kernel settles it without recording an incident. Raising from ``run``
    is different: an unexpected failure the kernel records and backs off on.
    """

    ok: bool = True
    result: Any = None
    error: str | None = None


class Agenda(Protocol):
    """What a domain agenda owes the schedule kernel.

    Not runtime_checkable: the optional attributes would break isinstance.
    """

    name: str
    lanes: int = 1
    hot_on_wake: bool = False

    async def next_due(self) -> datetime | None:
        """One indexed query; the nearest CLAIMABLE due.

        An in-flight claim is not claimable, so a lane that just launched
        never shows up here again. None means nothing to wait for. Runs
        inline in the single loop, so it stays short: one indexed read, no
        handlers, no retries of its own.
        """
        ...

    async def claim_due(self, now: datetime, limit: int) -> list[Claim]:
        """Atomically claim up to ``limit`` due units as of ``now``.

        Conditional update (find_one_and_update style): idle and due wins,
        anything else is left alone. Every returned claim stamps the
        ``boot_id`` from ``recover_at_boot`` and a fresh ``fencing_token``.
        Empty list when there is nothing, e.g. a lost race. Runs inline in
        the single loop, so it stays short like ``next_due``.
        """
        ...

    async def run(self, claim: Claim) -> Outcome:
        """The domain handler. May be long; runs in its own lane task.

        Raising is a failure: the kernel records it and settles with
        ``Outcome(ok=False)``. Returning ``Outcome(ok=False)`` is a settled
        domain failure instead: no incident, no backoff.
        """
        ...

    async def settle(self, claim: Claim, outcome: Outcome) -> None:
        """One fenced patch: result plus next occurrence, bounded retry, or
        terminal state.

        The patch must leave the item unclaimable or with a strictly future
        due; the kernel never moves a due itself. A stale ``fencing_token``
        means a newer claim owns the item: ignore it.
        """
        ...

    async def recover_at_boot(self, boot_id: str) -> int:
        """Run once per start, before any other call. Remember ``boot_id``
        FIRST, then release every claim of another ``boot_id``.

        No age threshold: with a single instance per host, any claim of an
        older boot is orphaned at once. Returns how many were released.
        """
        ...
