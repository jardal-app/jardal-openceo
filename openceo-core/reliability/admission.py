"""Hunter admission gate: verified Owner approval via ERP (design v2 section 9.3).

Epic TASK-2026-00365, ticket TASK-2026-00387. No ``reliability:*`` ticket
executes without a verifiable Owner approval over the current version of
its proposal. The gate is checked at dispatcher admission, at manual
launch and at resume (which passes through admission).

Verification reads the ERP, never a local flag: a recorded ``approval.decision``
transition from a human actor plus a live ``get_approval`` read proving the
approval is still approved, references the current proposal hash and is not
expired. Anything unverifiable blocks (fail-closed).

ERP limitation (ticket item 4, verified against
``business/approvals.py`` and ``business/events.py``): the approval view
carries ``{id, status, resource, version, decision, expires_at}`` with no
actor or role, and decision events carry only ``actor {kind, id}``
(``human`` vs ``agent``). A human-vs-agent check is automated; an Owner
role is not exposed, so until the ERP exposes it hunter tickets are only
launched explicitly by the Owner on top of this gate.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Protocol

logger = logging.getLogger(__name__)

HUNTER_SOURCE_ID_PREFIX = "reliability:"
HUNTER_PARAMS_KEY = "_hunter"
HUNTER_DECISIONS_KEY = "_erp_decisions"

OWNER_ROLE_LIMITATION = (
    "ERP exposes approval actor kind (human/agent) but no Owner role "
    "(business/approvals.py, business/events.py): hunter tickets are only "
    "launched explicitly by the Owner, on top of this gate."
)


class ApprovalReader(Protocol):
    """Minimal ERP surface the gate needs; tests inject a fake."""

    async def get_approval(self, identifier: str) -> dict[str, Any]:
        ...


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_hunter_task(task: Any) -> bool:
    """True for hunter execution tickets, by source_id or params marker."""
    if not isinstance(task, dict):
        return False
    source_id = str(task.get("source_id") or "").strip()
    if source_id.startswith(HUNTER_SOURCE_ID_PREFIX):
        return True
    hunter = (task.get("params") or {}).get(HUNTER_PARAMS_KEY)
    return isinstance(hunter, dict) and bool(hunter)


def hunter_claim(task: dict[str, Any]) -> dict[str, Any] | None:
    """Current proposal binding of a hunter ticket, or None when absent."""
    params = task.get("params")
    hunter = (params or {}).get(HUNTER_PARAMS_KEY) if isinstance(
        params, dict) else None
    if isinstance(hunter, dict) and hunter:
        problem_key = str(hunter.get("problem_key") or "").strip()
        proposal_hash = str(hunter.get("proposal_hash") or "").strip()
        approval_id = str(hunter.get("approval_id") or "").strip()
        if problem_key and proposal_hash:
            return {
                "problem_key": problem_key,
                "proposal_hash": proposal_hash,
                "approval_id": approval_id or None,
            }
    source_id = str(task.get("source_id") or "").strip()
    if source_id.startswith(HUNTER_SOURCE_ID_PREFIX):
        return {
            "problem_key": source_id,
            "proposal_hash": "",
            "approval_id": None,
        }
    return None


def approval_resource_for(problem_key: str, proposal_hash: str) -> str:
    """Canonical approval resource referencing Problem and proposal hash."""
    return (f"reliability:problem:{problem_key}:"
            f"proposal:{proposal_hash}")


def build_approval_request(
        problem_key: str, proposal_hash: str, *,
        business_ref: str | None = None) -> tuple[dict[str, Any], str]:
    """Approval request plus idempotency key for a proposal (ticket item 1)."""
    resource = approval_resource_for(problem_key, proposal_hash)
    request: dict[str, Any] = {"resource": resource}
    if business_ref:
        request["business_reference"] = business_ref
    key = f"reliability:{problem_key}:{proposal_hash[:16]}"
    return request, key


def normalize_approval(approval: Any) -> dict[str, Any] | None:
    """Flatten the ERP approval view and raw shapes into one mapping."""
    if not isinstance(approval, dict) or not approval:
        return None
    if not str(approval.get("id") or "").strip():
        return None
    decision = approval.get("decision")
    verdict = ""
    actor_kind: str | None = None
    actor_id: str | None = None
    if isinstance(decision, dict):
        verdict = str(decision.get("verdict") or decision.get(
            "decision") or "").strip()
        actor = decision.get("actor")
        if isinstance(actor, dict):
            actor_kind = str(actor.get("kind") or "").strip() or None
            actor_id = str(actor.get("id") or "").strip() or None
    if not verdict:
        verdict = str(approval.get("status") or "").strip()
    resource = approval.get("resource")
    if isinstance(resource, dict):
        resource = str(resource.get("id") or resource.get(
            "resource") or "")
    return {
        "id": str(approval.get("id")),
        "verdict": verdict.lower(),
        "resource": str(resource or ""),
        "version": approval.get("version"),
        "expires_at": approval.get("expires_at"),
        "actor_kind": actor_kind,
        "actor_id": actor_id,
    }


def _expiry_blocked(expires_at: Any) -> str | None:
    if expires_at is None or (isinstance(expires_at, str)
                              and not expires_at.strip()):
        return None
    if not isinstance(expires_at, str):
        return "approval_unverifiable"
    try:
        moment = datetime.fromisoformat(
            expires_at.strip().replace("Z", "+00:00"))
    except ValueError:
        return "approval_unverifiable"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment <= _utcnow():
        return "approval_expired"
    return None


def verify_approval(
        approval: Any, *, proposal_hash: str,
        problem_key: str | None = None) -> tuple[bool, str]:
    """Pure live-approval check: approved, current, fresh (no recorded state)."""
    if not proposal_hash:
        return False, "no_hunter_claim"
    view = normalize_approval(approval)
    if view is None:
        return False, "approval_unverifiable"
    if view["verdict"] != "approved":
        return False, "approval_not_approved"
    resource = view["resource"]
    if proposal_hash not in resource:
        return False, "stale_approval"
    if problem_key and problem_key not in resource:
        return False, "stale_approval"
    if view["actor_kind"] is not None and view["actor_kind"] != "human":
        return False, "agent_decision"
    expired = _expiry_blocked(view["expires_at"])
    if expired is not None:
        return False, expired
    return True, "approved"


def _recorded_entry(books: dict[str, Any],
                    approval_id: str) -> dict[str, Any] | None:
    """Newest recorded decision for an approval id, or None when absent."""
    best: dict[str, Any] | None = None
    best_version = -1
    for entry in books.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("approval_id") or "") != approval_id:
            continue
        try:
            version = int(entry.get("version", 0) or 0)
        except (TypeError, ValueError):
            version = 0
        if best is None or version > best_version:
            best, best_version = entry, version
    return best


def _recorded_human_approval(task: dict[str, Any],
                             approval_id: str) -> tuple[bool, str]:
    """Approval.decision evidence written only from validated ERP events."""
    params = task.get("params")
    books = (params or {}).get(HUNTER_DECISIONS_KEY) if isinstance(
        params, dict) else None
    if not isinstance(books, dict):
        return False, "no_approved_human_decision"
    entry = _recorded_entry(books, approval_id)
    if entry is None:
        return False, "no_approved_human_decision"
    kind = str(entry.get("actor_kind") or "").strip()
    if kind and kind != "human":
        return False, "agent_decision"
    if str(entry.get("verdict") or "").strip().lower() != "approved":
        return False, "approval_denied"
    if not kind:
        return False, "actor_unverifiable"
    return True, "approved"


def recorded_approval_version(task: dict[str, Any],
                              approval_id: str) -> int | None:
    """Recorded decision version for an approval id, if any."""
    params = task.get("params")
    books = (params or {}).get(HUNTER_DECISIONS_KEY) if isinstance(
        params, dict) else None
    if not isinstance(books, dict):
        return None
    entry = _recorded_entry(books, approval_id)
    if entry is None:
        return None
    try:
        return int(entry.get("version", 0) or 0)
    except (TypeError, ValueError):
        return None


async def check_admission(
        task: dict[str, Any], *,
        approvals: ApprovalReader | None = None,
        now: datetime | None = None) -> dict[str, Any]:
    """Admission verdict for one execution ticket. Never raises, fail-closed.

    Non-hunter tickets are allowed without any ERP read. Hunter tickets
    need a params claim (problem_key, proposal_hash, approval_id), a
    recorded human ``approval.decision`` for that approval id, and a live
    read confirming it is still approved, current and fresh.
    """
    _ = now
    if not is_hunter_task(task):
        return {"allowed": True, "reason": "not_hunter"}
    claim = hunter_claim(task)
    if claim is None or not claim["proposal_hash"]:
        return {"allowed": False, "reason": "no_hunter_claim"}
    approval_id = claim["approval_id"]
    if not approval_id:
        return {"allowed": False, "reason": "no_approval",
                "problem_key": claim["problem_key"]}
    recorded_ok, recorded_reason = _recorded_human_approval(task, approval_id)
    if not recorded_ok:
        return {"allowed": False, "reason": recorded_reason,
                "problem_key": claim["problem_key"],
                "approval_id": approval_id}
    if approvals is None:
        return {"allowed": False, "reason": "approval_unverifiable",
                "problem_key": claim["problem_key"],
                "approval_id": approval_id}
    try:
        live = await _maybe_await(approvals.get_approval(approval_id))
    except Exception:
        logger.warning("Hunter approval read failed (fail-closed)",
                       exc_info=True)
        return {"allowed": False, "reason": "approval_unverifiable",
                "problem_key": claim["problem_key"],
                "approval_id": approval_id}
    live_ok, live_reason = verify_approval(
        live, proposal_hash=str(claim["proposal_hash"]),
        problem_key=str(claim["problem_key"]))
    if not live_ok:
        return {"allowed": False, "reason": live_reason,
                "problem_key": claim["problem_key"],
                "approval_id": approval_id}
    view = normalize_approval(live)
    live_version = view.get("version") if isinstance(view, dict) else None
    recorded_version = recorded_approval_version(task, approval_id)
    if live_version is not None and recorded_version is not None:
        try:
            regressed = int(live_version) < int(recorded_version)
        except (TypeError, ValueError):
            regressed = False
        if regressed:
            return {"allowed": False, "reason": "stale_approval",
                    "problem_key": claim["problem_key"],
                    "approval_id": approval_id}
    return {"allowed": True, "reason": "approved",
            "problem_key": claim["problem_key"],
            "approval_id": approval_id}


def attach_approval(task: dict[str, Any], *, approval_id: str,
                    problem_key: str, proposal_hash: str,
                    now: datetime | None = None) -> dict[str, Any]:
    """Register an approval id on an execution ticket claim (ticket item 1)."""
    updated = dict(task)
    params = dict(updated.get("params") or {})
    hunter = dict(params.get(HUNTER_PARAMS_KEY) or {})
    hunter.update({
        "problem_key": problem_key,
        "proposal_hash": proposal_hash,
        "approval_id": approval_id,
        "approval_attached_at": (now or _utcnow()).isoformat(),
    })
    params[HUNTER_PARAMS_KEY] = hunter
    updated["params"] = params
    return updated


def claim_fields(source: dict[str, Any] | None) -> dict[str, str] | None:
    """Hunter claim fields carried by an ERP payload or flat params.

    Returns ``{problem_key, proposal_hash, approval_id?}`` when the
    carrier names a complete proposal binding, else None. Pure helper
    for execution-ingress wiring (creation and ERP adoption); the
    authorization itself is always verified live at admission.
    """
    if not isinstance(source, dict):
        return None
    problem_key = str(source.get("problem_key") or "").strip()
    proposal_hash = str(source.get("proposal_hash") or "").strip()
    if not problem_key or not proposal_hash:
        return None
    approval_id = str(source.get("approval_id") or "").strip()
    return {
        "problem_key": problem_key,
        "proposal_hash": proposal_hash,
        "approval_id": approval_id or "",
    }
