"""Single resolver for the per-ticket review switch ``review``.

Epic TASK-2026-00248 (child 00249): A/B/C review on by default with an
explicit per-ticket off; S always reviews. ``TaskDocument.review`` is the
only input, written at creation (or patched before the run) with ``on``
or ``off`` and read only through :func:`resolve_review`, at the top level
only. Absent means on.

The effective mode freezes at admission into
``params["review_effective"]`` (``{"mode", "origin"}``): a resume keeps
the admitted decision and later input or config changes never flip a
running attempt. Invalid input and S+off never execute: the caller waits
with a typed reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: Effective review modes (functional vocabulary ``review=on|off``).
REVIEW_ON = "on"
REVIEW_OFF = "off"

#: Params key carrying the frozen admission decision.
REVIEW_EFFECTIVE_KEY = "review_effective"

#: Typed wait reasons (fail-closed, fixable through the ticket patch API).
REVIEW_CONFLICT_REASON = "review_conflict"
REVIEW_INVALID_REASON = "review_invalid"

_TIER_S = "tier_s"


@dataclass(frozen=True)
class ReviewDecision:
    """Result of :func:`resolve_review`.

    ``mode`` is ``on``/``off`` (None when the input is invalid);
    ``origin`` is ``explicit`` (ticket input), ``default`` (absent input)
    or ``none`` (invalid input).
    """

    mode: str | None
    origin: str
    detail: str


def _coerce_mode(value: Any) -> str | None:
    """Exact on/off word, or None."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    normalized = value.value if hasattr(value, "value") else str(value)
    normalized = str(normalized).strip().lower()
    if normalized in (REVIEW_ON, REVIEW_OFF):
        return normalized
    return None


def read_effective_review(params: Mapping[str, Any] | None) -> dict[str, str] | None:
    """Persisted admission stamp, or None when absent/malformed."""
    if not isinstance(params, Mapping):
        return None
    stamped = params.get(REVIEW_EFFECTIVE_KEY)
    if not isinstance(stamped, Mapping):
        return None
    mode = stamped.get("mode")
    origin = stamped.get("origin")
    if mode not in (REVIEW_ON, REVIEW_OFF):
        return None
    if origin not in ("explicit", "default"):
        return None
    return {"mode": str(mode), "origin": str(origin)}


def resolve_review(task: Mapping[str, Any] | None) -> ReviewDecision:
    """Resolve the effective review mode for a ticket. The only reader.

    A valid admission stamp wins (resume keeps the admitted decision);
    otherwise the top-level ``review`` input decides and absent means on.
    """
    task = task or {}
    params = task.get("params")
    stamped = read_effective_review(params if isinstance(params, Mapping) else None)
    if stamped is not None:
        return ReviewDecision(
            stamped["mode"], stamped["origin"],
            f"resumed {REVIEW_EFFECTIVE_KEY}={stamped['mode']!r}")
    raw = task.get("review")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ReviewDecision(REVIEW_ON, "default", "no review value: on")
    mode = _coerce_mode(raw)
    if mode is not None:
        return ReviewDecision(mode, "explicit", f"review={mode!r}")
    return ReviewDecision(None, "none", f"invalid review={raw!r}")


def review_wait_reason(decision: ReviewDecision, tier: str | None) -> str | None:
    """Typed wait reason, or None when the run may proceed.

    Invalid input waits; S+off conflicts (S always reviews, the fast
    path keeps its A/B/C tier). Anything else runs.
    """
    if decision.mode is None:
        return (f"{REVIEW_INVALID_REASON}: {decision.detail} "
                "(use review=on|off)")
    if str(tier or "").strip().lower() == _TIER_S \
            and decision.mode == REVIEW_OFF:
        return (f"{REVIEW_CONFLICT_REASON}: tier_s always reviews; "
                "review=off is rejected (drop the override or keep the "
                "A/B/C tier for the fast path)")
    return None


def stamp_effective_review(
    params: Mapping[str, Any] | None,
    decision: ReviewDecision,
) -> dict[str, Any]:
    """Copy of ``params`` with the admission stamp. Pure; needs a mode."""
    stamped = dict(params) if isinstance(params, Mapping) else {}
    if decision.mode is not None:
        stamped[REVIEW_EFFECTIVE_KEY] = {
            "mode": decision.mode, "origin": decision.origin}
    return stamped
