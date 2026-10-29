"""Hunter ticket publisher and approval queue (design v2 sections 9.1-9.2).

Epic TASK-2026-00365, ticket TASK-2026-00385. One ticket per Problem with
evidence, never flooding the Owner: identity by source_id, destination by
scope, initial state with autorun=false, a bounded Pending Review queue
ordered by impact, section 9.2 content without foreign text, comments only
on material change, and no reproposal after an Owner rejection.

Pure decision helpers plus one thin async publish flow behind an injected
ERP adapter (typing.Protocol, as in diagnosis_exec.py), so tests use fakes
and no live model, clock or network exists here. Executing a hunter ticket
needs a verified Owner approval (admission gate, ticket TASK-2026-00387).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Protocol

from .config import RELIABILITY_DEFAULTS
from .models import ProblemStatus

logger = logging.getLogger(__name__)

SOURCE = "spillover"
SOURCE_ID_PREFIX = "reliability:problem:"
ENGINE_PROJECT = "jardal-openceo"

SEVERITY_WEIGHT = {"critical": 5, "high": 3, "normal": 1}

TICKET_TYPE_FIX = "fix"
TICKET_TYPE_INVESTIGATE = "investigate"

STATUS_PENDING_REVIEW = "Pending Review"
STATUS_OPEN = "Open"

HANDS_OFF_FOR_PUBLISH = frozenset({
    ProblemStatus.TICKETED.value,
    ProblemStatus.FIX_MERGED.value,
    ProblemStatus.VERIFYING.value,
    ProblemStatus.RESOLVED.value,
    ProblemStatus.REGRESSED.value,
    ProblemStatus.NEEDS_SPLIT.value,
    ProblemStatus.SUPERSEDED.value,
    ProblemStatus.MUTED.value,
    ProblemStatus.NO_ACTION.value,
    ProblemStatus.REJECTED.value,
})

MATERIAL_COMMENT_REASONS = frozenset({
    "regression",
    "new_release_affected",
    "fix_deployed",
})

MAX_AFFECTED_TASK_REFS = 20


class ErpAdapter(Protocol):
    """Minimal ERP surface the publisher needs; tests inject a fake."""

    async def find_by_source_id(
            self, source: str, source_id: str) -> dict[str, Any] | None:
        ...

    async def create_ticket(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    async def count_pending_hunter(self) -> int:
        ...

    async def update_ticket(
            self, business_ref: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    async def add_comment(self, business_ref: str, content: str) -> None:
        ...


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def source_id_for(problem_key: str) -> str:
    return f"{SOURCE_ID_PREFIX}{problem_key}"


def destination_for(scope: str) -> str | None:
    """Project for a Problem scope; None refuses unknown scopes."""
    if scope == "engine":
        return ENGINE_PROJECT
    if scope.startswith("project:") and len(scope) > len("project:"):
        return scope.split(":", 1)[1]
    return None


def impact_score(*, occurrences_30d: int, severity_max: str,
                 distinct_tickets_30d: int) -> int:
    weight = SEVERITY_WEIGHT.get(str(severity_max or "normal"), 1)
    return max(0, int(occurrences_30d)) * weight * max(
        0, int(distinct_tickets_30d))


def proposal_hash(proposal: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(proposal, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()


def needs_comment(reason: str) -> bool:
    return str(reason or "") in MATERIAL_COMMENT_REASONS


def mark_diagnosed(problem: dict[str, Any],
                   now: datetime | None = None) -> dict[str, Any]:
    updated = dict(problem)
    updated["status"] = ProblemStatus.DIAGNOSED.value
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = now or _utcnow()
    return updated


def record_rejection(problem: dict[str, Any],
                     now: datetime | None = None) -> dict[str, Any]:
    updated = dict(problem)
    updated["status"] = ProblemStatus.REJECTED.value
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = now or _utcnow()
    return updated


def mark_ticketed(problem: dict[str, Any], *, business_ref: str,
                  source_id: str, ticket_type: str,
                  proposal_hash: str | None = None,
                  now: datetime | None = None) -> dict[str, Any]:
    updated = dict(problem)
    updated["status"] = ProblemStatus.TICKETED.value
    updated["ticket"] = {
        "business_ref": business_ref, "source_id": source_id,
        "type": ticket_type, "proposal_hash": proposal_hash,
    }
    updated["version"] = int(problem.get("version", 1)) + 1
    updated["updated_at"] = now or _utcnow()
    return updated


def reevaluate_queue(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Diagnosed Problems waiting for a queue slot, highest impact first."""
    waiting = [doc for doc in problems
               if str(doc.get("status") or "")
               == ProblemStatus.DIAGNOSED.value
               and not bool((doc.get("links") or {}).get("grouped_under"))]
    counts_of = lambda doc: doc.get("counts") or {}
    return sorted(
        waiting,
        key=lambda doc: (
            -impact_score(
                occurrences_30d=int(counts_of(doc).get("occurrences", 0)),
                severity_max=str(doc.get("severity_max") or "normal"),
                distinct_tickets_30d=int(
                    counts_of(doc).get("distinct_tickets", 0))),
            str(doc.get("_id") or "")))


def build_ticket(problem: dict[str, Any], *, ticket_type: str,
                 proposal: dict[str, Any] | None = None,
                 evidence: dict[str, Any] | None = None,
                 affected_open_tasks: list[str] | None = None,
                 touches_protected: bool = False) -> dict[str, Any]:
    """Section 9.2 ticket payload.

    Reads only allowlisted Problem keys, so no transcript, full log or
    foreign-project text can leak in: free text travels exclusively inside
    the evidence blocks the caller passes.
    """
    if ticket_type not in (TICKET_TYPE_FIX, TICKET_TYPE_INVESTIGATE):
        raise ValueError(f"unknown hunter ticket type {ticket_type!r}")
    problem_key = str(problem.get("problem_key") or problem.get("_id") or "")
    scope = str(problem.get("scope") or "")
    project = destination_for(scope)
    if project is None:
        raise ValueError(f"refusing unknown scope {scope!r}")
    counts = problem.get("counts") or {}
    summary = {
        "problem": problem_key,
        "scope": scope,
        "kind": str(problem.get("kind") or ""),
        "component": str(problem.get("component") or ""),
        "attribution": str(problem.get("attribution") or ""),
        "attribution_rule": problem.get("attribution_rule"),
    }
    counting = {
        "occurrences": int(counts.get("occurrences", 0)),
        "distinct_tickets": int(counts.get("distinct_tickets", 0)),
        "distinct_projects": int(counts.get("distinct_projects", 0)),
        "releases": list(problem.get("releases_seen") or []),
        "first_seen": str(problem.get("first_seen") or ""),
        "last_seen": str(problem.get("last_seen") or ""),
    }
    body: dict[str, Any] = {
        "problem": summary,
        "counts": counting,
        "hypothesis": dict(proposal.get("hypothesis") or {})
        if isinstance(proposal, dict) else None,
        "proposed_change": (proposal.get("proposed_change")
                            if isinstance(proposal, dict) else None),
        "target_components": list((proposal.get("target_components") or [])
                                  if isinstance(proposal, dict) else []),
        "regression_test": (proposal.get("regression_test")
                            if isinstance(proposal, dict) else None),
        "recovery": (proposal.get("recovery")
                     if isinstance(proposal, dict) else None),
        "evidence": evidence,
        "affected_open_tasks": {
            "count": len(affected_open_tasks or []),
            "refs": list((affected_open_tasks or [])[:MAX_AFFECTED_TASK_REFS]),
        },
        "touches_protected_routes": bool(touches_protected),
    }
    if ticket_type == TICKET_TYPE_FIX:
        if not isinstance(proposal, dict) or not proposal.get(
                "proposed_change") or not proposal.get("regression_test"):
            raise ValueError("a fix ticket needs a complete proposal")
        digest = proposal_hash(proposal)
        status = STATUS_PENDING_REVIEW
        human_action = ("Aprobar o rechazar la propuesta "
                        f"{digest} del Problem {problem_key}")
    else:
        digest = None
        status = STATUS_OPEN
        human_action = None
    return {
        "source": SOURCE,
        "source_id": source_id_for(problem_key),
        "project": project,
        "title": f"[Fiabilidad] {ticket_type}: {problem_key}",
        "body": body,
        "autorun": False,
        "initial_status": status,
        "human_action": human_action,
        "proposal_hash": digest,
    }


def build_promotion(problem: dict[str, Any],
                    proposal: dict[str, Any]) -> dict[str, Any]:
    """Investigate to fix promotion: a new hash and a new approval."""
    if not isinstance(proposal, dict) or not proposal.get(
            "proposed_change") or not proposal.get("regression_test"):
        raise ValueError("promotion needs a complete proposal")
    problem_key = str(problem.get("problem_key") or problem.get("_id") or "")
    digest = proposal_hash(proposal)
    previous = ((problem.get("ticket") or {}).get("proposal_hash")
                if isinstance(problem.get("ticket"), dict) else None)
    if previous is not None and previous == digest:
        raise ValueError("promotion needs a new proposal hash")
    return {
        "business_ref": (problem.get("ticket") or {}).get("business_ref"),
        "proposal": proposal,
        "proposal_hash": digest,
        "initial_status": STATUS_PENDING_REVIEW,
        "human_action": ("Aprobar o rechazar la propuesta "
                         f"{digest} del Problem {problem_key}"),
    }


async def _persist_problem(store: Any, problem_id: str,
                           update: dict[str, Any]) -> None:
    if store is None:
        return
    patch: dict[str, Any] = {
        "status": update.get("status"),
        "updated_at": update.get("updated_at"),
        "version": update.get("version"),
    }
    if "ticket" in update:
        patch["ticket"] = update.get("ticket")
    try:
        await _maybe_await(store.update_one(
            {"_id": problem_id}, {"$set": patch}))
    except Exception:
        logger.warning("Hunter problem persist failed", exc_info=True)


async def publish(problem: dict[str, Any], *, ticket_type: str,
                  erp: ErpAdapter,
                  proposal: dict[str, Any] | None = None,
                  evidence: dict[str, Any] | None = None,
                  affected_open_tasks: list[str] | None = None,
                  touches_protected: bool = False,
                  tickets_cfg: dict[str, Any] | None = None,
                  store: Any = None,
                  now: datetime | None = None) -> dict[str, Any]:
    """Publish one ticket per Problem; never raises."""
    moment = now or _utcnow()
    cfg = tickets_cfg if isinstance(tickets_cfg, dict) \
        else RELIABILITY_DEFAULTS["tickets"]
    if not bool(cfg.get("enabled", False)):
        return {"status": "disabled", "reason": "tickets_disabled"}
    status = str(problem.get("status") or "")
    if status == ProblemStatus.REJECTED.value:
        return {"status": "refused", "reason": "rejected_never_reproposed"}
    if status in HANDS_OFF_FOR_PUBLISH - {ProblemStatus.REJECTED.value}:
        return {"status": "refused", "reason": f"status_{status}"}
    if bool((problem.get("links") or {}).get("grouped_under")):
        return {"status": "refused", "reason": "grouped_under_storm"}
    try:
        payload = build_ticket(
            problem, ticket_type=ticket_type, proposal=proposal,
            evidence=evidence, affected_open_tasks=affected_open_tasks,
            touches_protected=touches_protected)
    except ValueError as exc:
        return {"status": "refused", "reason": str(exc)}
    source_id = str(payload["source_id"])
    try:
        existing = await _maybe_await(
            erp.find_by_source_id(SOURCE, source_id))
    except Exception:
        logger.warning("Hunter source_id lookup failed", exc_info=True)
        return {"status": "unknown", "reason": "lookup_failed",
                "source_id": source_id}
    if isinstance(existing, dict) and existing:
        return {"status": "already_exists",
                "ticket": existing, "source_id": source_id}
    try:
        pending = await _maybe_await(erp.count_pending_hunter())
    except Exception:
        logger.warning("Hunter queue count failed", exc_info=True)
        return {"status": "unknown", "reason": "queue_count_failed",
                "source_id": source_id}
    if int(pending) >= int(cfg.get("max_pending_owner", 5)):
        diagnosed = mark_diagnosed(problem, moment)
        await _persist_problem(store, str(problem.get("_id")), diagnosed)
        return {"status": "queued", "problem_status": "diagnosed",
                "source_id": source_id, "problem_update": diagnosed}
    try:
        created = await _maybe_await(erp.create_ticket(payload))
    except TimeoutError:
        confirmed = None
        try:
            confirmed = await _maybe_await(
                erp.find_by_source_id(SOURCE, source_id))
        except Exception:
            logger.warning("Hunter confirm-after-timeout failed",
                           exc_info=True)
        if isinstance(confirmed, dict) and confirmed:
            return {"status": "already_exists", "ticket": confirmed,
                    "source_id": source_id}
        return {"status": "unknown",
                "reason": "create_timeout_unconfirmed",
                "source_id": source_id}
    except Exception:
        logger.warning("Hunter ticket creation failed", exc_info=True)
        return {"status": "error", "reason": "create_failed",
                "source_id": source_id}
    if not isinstance(created, dict) or not created:
        return {"status": "error", "reason": "create_empty",
                "source_id": source_id}
    ticketed = mark_ticketed(
        problem, business_ref=str(created.get("business_ref") or ""),
        source_id=source_id, ticket_type=ticket_type,
        proposal_hash=str(payload.get("proposal_hash") or None)
        if payload.get("proposal_hash") else None,
        now=moment)
    await _persist_problem(store, str(problem.get("_id")), ticketed)
    return {"status": "published", "ticket": created,
            "problem_status": str(ticketed["status"]),
            "source_id": source_id, "problem_update": ticketed}

