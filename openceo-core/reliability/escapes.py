"""Escaped defects: revert, linked bug and reopen escape incidents.

Design v2 section 5.5 (epic TASK-2026-00365, ticket TASK-2026-00382).
A defect that reached ``Completed`` past gates and review is recorded as
an ``escape`` incident, so the hunter can measure the escape rate per
tier and review policy. Fingerprint is ``escape|type|tier|review
policy`` and attribution stays ``unknown``: a reopen does not prove a
review failure.

Sources:

- revert: the engine integrates a commit reading ``This reverts commit
  <sha>`` and ``<sha>`` belongs to another ticket.
- linked bug: a ticket created with a ``caused_by`` relation toward a
  ``Completed`` ticket. The business API exposes no such relation, so
  this source stays declared not covered (see ``linked_bug_coverage``).
- reopen: a ``Completed`` -> ``Open``/``Working`` transition, through
  the OpenCEO semantic API (``task_service.transition_ticket``) or the
  ``task.status_changed`` ERP event (``business/events.py``), with its
  actor kept as evidence.

Recording is explicit and never raises; integration hooks are
best-effort and never change ticket flows. Reopen records share one
deterministic id per ticket and transition, so the semantic call and a
later ERP echo of the same reopen collapse into a single incident.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .capture import record_incident, resolve_collection

logger = logging.getLogger(__name__)

ESCAPE_REVERT = "revert"
ESCAPE_LINKED_BUG = "linked_bug"
ESCAPE_REOPEN = "reopen"

ESCAPE_TYPES = frozenset({ESCAPE_REVERT, ESCAPE_LINKED_BUG, ESCAPE_REOPEN})

_COMPLETED = "completed"
_REOPEN_TARGETS = frozenset({"open", "working"})

_REVERT_RE = re.compile(
    r"This\s+reverts\s+commit\s+([0-9a-fA-F]{4,64})\b", re.IGNORECASE)

LINKED_BUG_NOT_COVERED = (
    "ERP exposes no caused_by relation (business/ports.py, "
    "business/work.py, task_service.py carry dependencies only): "
    "linked-bug escapes stay declared not covered per design v2 5.5."
)


def parse_revert_sha(message: Any) -> str | None:
    """Reverted SHA from a git revert message, else None."""
    if not isinstance(message, str) or not message.strip():
        return None
    found = _REVERT_RE.search(message)
    if found is None:
        return None
    return found.group(1).lower()


def _normalized(status: Any) -> str:
    return str(status or "").strip().lower()


def is_reopen_escape(previous_status: Any, new_status: Any) -> bool:
    """True only for a Completed -> Open/Working reopen, any casing."""
    return (_normalized(previous_status) == _COMPLETED
            and _normalized(new_status) in _REOPEN_TARGETS)


def classify_revert(message: Any, *, reverted_ref: Any,
                    reverted_completed: bool,
                    integrating_ref: Any = None) -> dict[str, Any]:
    """Pure revert verdict; ``reverted_ref`` comes from the caller's store."""
    sha = parse_revert_sha(message)
    if sha is None:
        return {"escape": False, "reason": "not_a_revert", "sha": None}
    target = str(reverted_ref or "").strip() or None
    if target is None:
        return {"escape": False, "reason": "reverted_unknown", "sha": sha}
    if not reverted_completed:
        return {"escape": False, "reason": "reverted_not_completed",
                "sha": sha, "reverted_ref": target}
    current = str(integrating_ref or "").strip() or None
    if current is not None and current == target:
        return {"escape": False, "reason": "same_ticket",
                "sha": sha, "reverted_ref": target}
    return {"escape": True, "reason": "revert_of_completed",
            "sha": sha, "reverted_ref": target}


def linked_bug_coverage(
        capabilities: Any | None = None) -> dict[str, Any]:
    """Linked-bug source coverage; the ERP has no caused_by relation."""
    del capabilities
    return {"covered": False, "reason": LINKED_BUG_NOT_COVERED}


def classify_linked_bug(ticket: Any = None,
                        capabilities: Any | None = None) -> dict[str, Any]:
    """Pure linked-bug verdict: always not covered, never an escape."""
    del ticket, capabilities
    return {"escape": False, "reason": "not_covered",
            "detail": LINKED_BUG_NOT_COVERED}


def classify_reopen(previous_status: Any, new_status: Any,
                    *, actor: Any = None) -> dict[str, Any]:
    """Pure reopen verdict; the actor travels as evidence only."""
    if not is_reopen_escape(previous_status, new_status):
        return {"escape": False, "reason": "not_a_reopen"}
    entry = str(actor or "").strip() or None
    return {"escape": True, "reason": "reopen_of_completed",
            "previous": str(previous_status).strip(),
            "new": str(new_status).strip(), "actor": entry}


def tier_of(task: Any) -> str:
    """Escape tier: the ticket coding tool, ``unknown`` when absent."""
    if isinstance(task, dict):
        tier = str(task.get("coding_tool") or "").strip()
        if tier:
            return tier
    return "unknown"


def review_policy_of(task: Any) -> str:
    """Escape review policy: QA-gated tickets differ from standard ones."""
    if isinstance(task, dict) and bool(task.get("qa_review_required")):
        return "qa_required"
    return "standard"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def reopen_incident_id(task_id: Any, previous_status: Any,
                       new_status: Any) -> str:
    """Deterministic id shared by the semantic call and its ERP echo."""
    return _stable_id(
        "escape", str(task_id or "unknown"),
        "reopen|%s|%s" % (
            _normalized(previous_status), _normalized(new_status)))


def revert_incident_id(task_id: Any, sha: str) -> str:
    """Deterministic id per reverted ticket and reverted SHA."""
    return _stable_id(
        "escape", str(task_id or "unknown"),
        "revert|%s" % str(sha).lower())


def build_reopen_doc(*, previous_status: Any, new_status: Any,
                     task: Any = None, business_ref: Any = None,
                     project_key: Any = None, component: str = "business_sync",
                     actor: Any = None, event_id: Any = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Escape doc for a reopen; attribution stays unknown (A10)."""
    from .capture import build_incident_doc

    refs: list[str] = []
    if event_id is not None and str(event_id).strip():
        refs.append(str(event_id).strip())
    task_id: str | None = None
    if isinstance(task, dict):
        raw = str(task.get("task_id") or "").strip()
        task_id = raw or None
    message = "reopen %s -> %s" % (
        str(previous_status).strip(), str(new_status).strip())
    if actor is not None and str(actor).strip():
        message += " by %s" % str(actor).strip()
    return build_incident_doc(
        kind="escape", component=component, now=now or _utcnow(),
        escape_type=ESCAPE_REOPEN, tier=tier_of(task),
        review_policy=review_policy_of(task),
        task_id=task_id or (str(business_ref).strip() or None),
        business_ref=(str(business_ref).strip() or None),
        project_key=(str(project_key).strip() or None),
        message=message,
        evidence_refs=refs,
        incident_id=reopen_incident_id(
            task_id or business_ref, previous_status, new_status),
    )


def build_revert_doc(*, sha: str, reverted_ref: str,
                     task: Any = None, business_ref: Any = None,
                     project_key: Any = None,
                     component: str = "tier_s",
                     revert_ref: Any = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """Escape doc for a revert of another Completed ticket's commit."""
    from .capture import build_incident_doc

    refs = [str(reverted_ref).strip()]
    if revert_ref is not None and str(revert_ref).strip():
        refs.append(str(revert_ref).strip())
    task_id: str | None = None
    if isinstance(task, dict):
        raw = str(task.get("task_id") or "").strip()
        task_id = raw or None
    return build_incident_doc(
        kind="escape", component=component, now=now or _utcnow(),
        escape_type=ESCAPE_REVERT, tier=tier_of(task),
        review_policy=review_policy_of(task),
        task_id=task_id or (str(business_ref).strip() or None),
        business_ref=(str(business_ref).strip() or None),
        project_key=(str(project_key).strip() or None),
        message="revert of %s from %s" % (
            str(sha).lower(), str(reverted_ref).strip()),
        evidence_refs=refs,
        incident_id=revert_incident_id(task_id or business_ref, sha),
    )


async def record_reopen_escape(
        source: Any, *, previous_status: Any, new_status: Any,
        task: Any = None, business_ref: Any = None,
        project_key: Any = None, component: str = "business_sync",
        actor: Any = None, event_id: Any = None,
        runtime_dir: Any = None,
        now: datetime | None = None) -> str | None:
    """Record a reopen escape; None when the transition is not a reopen."""
    try:
        if not is_reopen_escape(previous_status, new_status):
            return None
        try:
            if resolve_collection(source) is None:
                return None
        except Exception:
            return None
        doc_task_id: str | None = None
        if isinstance(task, dict):
            raw = str(task.get("task_id") or "").strip()
            doc_task_id = raw or None
        doc = build_reopen_doc(
            previous_status=previous_status, new_status=new_status,
            task=task, business_ref=business_ref, project_key=project_key,
            component=component, actor=actor, event_id=event_id,
            now=now or _utcnow())
        return await record_incident(
            source, kind="escape", component=component,
            escape_type=ESCAPE_REOPEN, tier=tier_of(task),
            review_policy=review_policy_of(task),
            task_id=doc_task_id or (str(business_ref).strip() or None),
            business_ref=(str(business_ref).strip() or None),
            project_key=(str(project_key).strip() or None),
            message=str(doc["context"].get("message") or ""),
            evidence_refs=list(doc["context"].get("evidence_refs") or []),
            incident_id=str(doc["_id"]),
            runtime_dir=runtime_dir, now=now or _utcnow())
    except Exception:
        logger.warning("reopen escape record failed", exc_info=True)
        return None


async def record_revert_escape(
        source: Any, *, revert_message: Any, reverted_ref: Any,
        reverted_completed: bool, integrating_ref: Any = None,
        task: Any = None, business_ref: Any = None,
        project_key: Any = None, component: str = "tier_s",
        runtime_dir: Any = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Record a revert escape; explicit call from the integrating path."""
    try:
        try:
            if resolve_collection(source) is None:
                return {"recorded": False, "reason": "no_store"}
        except Exception:
            return {"recorded": False, "reason": "no_store"}
        verdict = classify_revert(
            revert_message, reverted_ref=reverted_ref,
            reverted_completed=reverted_completed,
            integrating_ref=integrating_ref)
        if not verdict["escape"]:
            return {"recorded": False, **verdict}
        sha = str(verdict["sha"])
        target = str(verdict["reverted_ref"])
        task_id: str | None = None
        if isinstance(task, dict):
            raw = str(task.get("task_id") or "").strip()
            task_id = raw or None
        task_id = task_id or (str(business_ref).strip() or None)
        doc = build_revert_doc(
            sha=sha, reverted_ref=target, task=task,
            business_ref=business_ref, project_key=project_key,
            component=component,
            revert_ref=(str(integrating_ref).strip() or None)
            if integrating_ref is not None else None,
            now=now or _utcnow())
        stored = await record_incident(
            source, kind="escape", component=component,
            escape_type=ESCAPE_REVERT, tier=tier_of(task),
            review_policy=review_policy_of(task),
            task_id=task_id,
            business_ref=(str(business_ref).strip() or None),
            project_key=(str(project_key).strip() or None),
            message=str(doc["context"].get("message") or ""),
            evidence_refs=list(doc["context"].get("evidence_refs") or []),
            reason_code=ESCAPE_REVERT,
            incident_id=str(doc["_id"]),
            runtime_dir=runtime_dir, now=now or _utcnow())
        return {"recorded": stored is not None, "incident_id": stored,
                **verdict}
    except Exception:
        logger.warning("revert escape record failed", exc_info=True)
        return {"recorded": False, "reason": "hook_failed"}
