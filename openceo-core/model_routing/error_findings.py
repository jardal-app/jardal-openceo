"""Router errors to OpenCEO findings (TASK-2026-00297).

Every recorded error is offered here at event time
(:meth:`AvailabilityStore.record_error`): the offer counts the
sighting and queues at most one pending row per fingerprint, so a
new or recurrent error class yields a single deduplicated finding.
:meth:`drain_pending` forwards queued rows to the findings inbox
with the existing :mod:`orchestration.inbox` mechanism; promotion
payloads carry ``autorun=false``. No polling anywhere: offers
happen on the error event, drains on explicit invocation with an
inbox store and project; ``errors --sync-findings`` only reports
the queued previews.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from orchestration import inbox as inbox_mod

__all__ = [
    "FINDING_SOURCE",
    "appearance_for_record",
    "drain_pending",
    "finding_fingerprint",
    "offer_record",
    "preview_payload",
    "queued_previews",
]

FINDING_SOURCE = "router_errors"

logger = logging.getLogger(__name__)

_FINDING_FILE = "openceo-core/model_routing/errors.py"

_SECURITY_CLASSES = frozenset({"auth", "billing", "forbidden"})

_CRITICAL_CLASSES = frozenset({"auth", "billing"})

_PERFORMANCE_CLASSES = frozenset({"rate_limited", "daily_quota", "server_error"})

_ROUTER_ORIGIN = "router_errors"


def _category(error_class: str) -> str:
    if error_class in _SECURITY_CLASSES:
        return "security"
    if error_class in _PERFORMANCE_CLASSES:
        return "performance"
    return "bug"


def _severity(error_class: str) -> str:
    if error_class in _CRITICAL_CLASSES:
        return "CRITICAL"
    return "WARN"


def _quote(provider: str, error_class: str, action: str) -> str:
    return f"{error_class} from {provider} (action {action})"


def appearance_for_record(record: Any) -> inbox_mod.Appearance:
    """Stable inbox appearance for one registry row.

    The quote names class, provider and action only: per-occurrence
    excerpts would defeat fingerprint deduplication.
    """
    provider = str(record.provider or "unknown")
    error_class = str(record.error_class or "unclassified")
    action = str(record.action or "record_only")
    run_id = str(record.run_id or "")
    return inbox_mod.Appearance(
        file=_FINDING_FILE, category=_category(error_class),
        severity=_severity(error_class), quote=_quote(provider, error_class, action),
        strength="weak", source=FINDING_SOURCE, family=provider,
        task_id=run_id, run_id=run_id, round_no=0,
        anchor=f"{provider}/{record.deployment_id}")


def finding_fingerprint(appearance: inbox_mod.Appearance) -> str:
    """Inbox fingerprint of one router-error appearance."""
    return inbox_mod.fingerprint(appearance.file, appearance.category, appearance.quote)


def _appearance_payload(appearance: inbox_mod.Appearance) -> str:
    return json.dumps({
        "file": appearance.file, "category": appearance.category,
        "severity": appearance.severity, "quote": appearance.quote,
        "strength": appearance.strength, "source": appearance.source,
        "family": appearance.family, "task_id": appearance.task_id,
        "run_id": appearance.run_id, "round_no": appearance.round_no,
        "anchor": appearance.anchor,
    })


def _appearance_from_payload(payload: str) -> inbox_mod.Appearance:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("finding payload is not an object")
    return inbox_mod.Appearance(
        file=str(data.get("file") or _FINDING_FILE),
        category=str(data.get("category") or "bug"),
        severity=str(data.get("severity") or "WARN"),
        quote=str(data.get("quote") or ""),
        strength=str(data.get("strength") or "weak"),
        source=str(data.get("source") or FINDING_SOURCE),
        family=str(data.get("family") or ""),
        task_id=str(data.get("task_id") or ""),
        run_id=str(data.get("run_id") or ""),
        round_no=int(data.get("round_no") or 0),
        anchor=str(data.get("anchor") or ""))


def offer_record(store: Any, record: Any) -> str:
    """Count one error sighting and queue its fingerprint when absent.

    Returns the inbox fingerprint. Re-offers of the same class only
    grow the sighting count: the pending queue holds one row per
    fingerprint, and the inbox holds one entry per fingerprint.
    """
    appearance = appearance_for_record(record)
    fingerprint = finding_fingerprint(appearance)
    store.note_finding_seen(fingerprint, str(record.error_class or "unclassified"))
    store.queue_finding(fingerprint, _appearance_payload(appearance))
    return fingerprint


async def drain_pending(store: Any, inbox_store: Any, project: str) -> list[str]:
    """Forward queued rows to the inbox; returns forwarded fingerprints."""
    forwarded: list[str] = []
    for fingerprint, payload in store.pending_findings():
        try:
            appearance = _appearance_from_payload(payload)
        except (ValueError, TypeError):
            logger.warning("router finding payload corrupt; dropping %s", fingerprint)
            store.drop_pending(fingerprint)
            continue
        await inbox_mod.record_appearance(inbox_store, project, appearance)
        store.drop_pending(fingerprint)
        forwarded.append(fingerprint)
    return forwarded


def preview_payload(store: Any, fingerprint: str) -> dict[str, Any] | None:
    """Promotion payload preview for one queued fingerprint, if still queued."""
    for queued, payload in store.pending_findings():
        if queued != fingerprint:
            continue
        try:
            appearance = _appearance_from_payload(payload)
        except (ValueError, TypeError):
            return None
        entry = {
            "_id": fingerprint, "status": "open", "file": appearance.file,
            "category": appearance.category, "severity_max": appearance.severity,
            "quote": appearance.quote, "anchor": appearance.anchor,
            "families": [appearance.family] if appearance.family else [],
            "seen_in": [{"task_id": appearance.task_id, "run_id": appearance.run_id,
                         "round": appearance.round_no}],
        }
        payload_out = inbox_mod.promotion_ticket(entry)
        payload_out["autorun"] = False
        payload_out["origin"] = _ROUTER_ORIGIN
        return payload_out
    return None


def queued_previews(store: Any) -> list[tuple[str, Mapping[str, Any] | None]]:
    """(fingerprint, preview) for every queued row, oldest first."""
    return [(fingerprint, preview_payload(store, fingerprint))
            for fingerprint, _payload in store.pending_findings()]
