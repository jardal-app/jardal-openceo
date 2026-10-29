"""Bounded recovery: business-state reconciliation (design v2 section 10).

Epic TASK-2026-00365, ticket TASK-2026-00390. V1 recovers only what is
safe: the ERP mismatch after a transition (INV-002). Pure decisions plus
one thin async flow behind injected ERP surfaces; tests use fakes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Protocol

from .config import RELIABILITY_DEFAULTS

logger = logging.getLogger(__name__)

MAX_RECOVERY_ATTEMPTS = 2

MAX_AFFECTED_REFS = 20

REFUSED_RECOVERY_KINDS = frozenset({
    "blind_retry_uncertain_effect",
    "change_tier",
    "relaunch_confirmed_from_scratch",
})

RESUME_RECOMMENDATION = (
    "Reanudar por el mecanismo existente (admision + checkpoint), "
    "nunca reanudacion directa."
)


class BusinessReader(Protocol):
    """Minimal ERP read surface; tests inject a fake."""

    async def resolve_task(self, business_ref: str) -> dict[str, Any]:
        ...


class BusinessApplier(Protocol):
    """Semantic reapply surface (transition API, never raw patch)."""

    async def transition_status(
        self, business_ref: str, target: str, **kwargs: Any
    ) -> dict[str, Any]:
        ...


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def recovery_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """Gate for automatic reconciliation; default False, fail-closed."""
    try:
        block = cfg if isinstance(cfg, dict) else RELIABILITY_DEFAULTS.get(
            "recovery")
        if not isinstance(block, dict):
            return False
        return bool(block.get("reconcile_business_state", False))
    except Exception:
        logger.warning("recovery config unreadable, staying disabled",
                       exc_info=True)
        return False


def attempts_of(incident: Any) -> int:
    try:
        return max(0, int((incident or {}).get("recovery_attempts", 0)))
    except (TypeError, ValueError):
        return 0


def is_refused_recovery(kind: str) -> bool:
    return str(kind or "").strip() in REFUSED_RECOVERY_KINDS


def decide_recovery(
    *,
    target: str,
    observed_version: Any,
    reread: Any,
    attempts: int = 0,
) -> dict[str, Any]:
    """Pure INV-002 decision from a fresh ERP reread."""
    want = str(target or "").strip()
    if not want:
        return {"decision": "unknown", "reason": "no_target"}
    if int(attempts or 0) >= MAX_RECOVERY_ATTEMPTS:
        return {"decision": "give_up", "reason": "attempts_exhausted"}
    if not isinstance(reread, dict) or not reread:
        return {"decision": "unknown", "reason": "reread_failed"}
    actual = str(reread.get("status") or "").strip()
    if actual == want:
        return {"decision": "already_reconciled", "reason": "at_target"}
    seen = str(observed_version or "").strip()
    current_version = str(reread.get("version") or "").strip()
    if not seen or not current_version:
        return {"decision": "needs_owner", "reason": "version_unknown"}
    if current_version != seen:
        return {"decision": "needs_owner", "reason": "changed_by_other_actor"}
    return {"decision": "reapply", "reason": "version_matches"}


def affected_summary(refs: Any) -> dict[str, Any]:
    """Bounded affected-task view with the resume recommendation."""
    seen: list[str] = []
    for ref in refs or []:
        name = str(ref or "").strip()
        if name and name not in seen:
            seen.append(name)
    return {
        "count": len(seen),
        "refs": seen[:MAX_AFFECTED_REFS],
        "recommendation": RESUME_RECOMMENDATION,
    }


def note_verifying_affected(
    problem: dict[str, Any], refs: Any
) -> tuple[dict[str, Any], str]:
    """Snapshot affected refs when a Problem enters verifying."""
    if str(problem.get("status") or "") != "verifying":
        return problem, "not_verifying"
    summary = affected_summary(refs)
    updated = dict(problem)
    updated["affected_open_tasks"] = list(summary["refs"])
    updated["affected_recommendation"] = summary["recommendation"]
    return updated, "snapshot"


async def reconcile_incident(
    incident: dict[str, Any],
    *,
    reader: BusinessReader | None,
    applier: BusinessApplier | None,
    cfg: dict[str, Any] | None = None,
    reason: str = "Reconciliacion INV-002: relectura tras transicion",
) -> dict[str, Any]:
    """Reconcile one INV-002 incident; never raises, never overwrites humans."""
    try:
        ref = str((incident or {}).get("business_ref") or "").strip()
        target = str((incident or {}).get("target") or "").strip()
        if not ref or not target:
            return {"status": "unknown", "reason": "bad_incident"}
        if not recovery_enabled(cfg):
            return {"status": "disabled", "reason": "recovery_disabled",
                    "ticket": ref}
        tries = attempts_of(incident)
        if tries >= MAX_RECOVERY_ATTEMPTS:
            return {"status": "give_up", "reason": "attempts_exhausted",
                    "ticket": ref, "attempts": tries}
        if reader is None:
            return {"status": "unknown", "reason": "no_reader",
                    "ticket": ref}
        try:
            reread = await _maybe_await(reader.resolve_task(ref))
        except Exception:
            logger.warning("recovery reread failed for %s", ref,
                           exc_info=True)
            return {"status": "unknown", "reason": "reread_failed",
                    "ticket": ref, "attempts": tries}
        choice = decide_recovery(
            target=target,
            observed_version=(incident or {}).get("observed_version"),
            reread=reread, attempts=tries)
        decision = str(choice.get("decision") or "")
        if decision == "already_reconciled":
            return {"status": "already_reconciled", "ticket": ref,
                    "attempts": tries}
        if decision in ("needs_owner", "give_up", "unknown"):
            return {"status": decision,
                    "reason": str(choice.get("reason")),
                    "ticket": ref, "attempts": tries}
        if applier is None:
            return {"status": "unknown", "reason": "no_applier",
                    "ticket": ref, "attempts": tries}
        try:
            await _maybe_await(applier.transition_status(
                ref, target, reason=reason))
        except Exception:
            logger.warning("recovery reapply failed for %s", ref,
                           exc_info=True)
            return {"status": "unknown", "reason": "reapply_failed",
                    "ticket": ref, "attempts": tries + 1}
        return {"status": "reapplied", "ticket": ref, "attempts": tries + 1}
    except Exception:
        logger.warning("recovery flow failed", exc_info=True)
        return {"status": "unknown", "reason": "flow_failed"}
