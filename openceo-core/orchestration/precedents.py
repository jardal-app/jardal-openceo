"""Owner precedent log: proposal, Owner answer and observed result (TASK-2026-00335).

D10: OpenCEO links the agent proposal, the Owner response (approved,
edited with a concrete change, or denied) and the observed result. It
reuses what is already recorded -- owner questions
(:mod:`owner_channel`), CEO reviews (``CEODecision``) and ticket
results -- and stores only references plus dated summaries in Mongo;
the authority stays in Jardal ERP, Jira or Autotask (tickets are
never copied here).

The result completes by events, never by polling: callers invoke
:func:`complete_outcome` from the close path once the result is
verified. An inconclusive result stays ``None`` and never counts as
evidence. Hits, failures and correct abstentions are all recorded.

No tokens, no ticket outcome changes. Thresholds and the response
classification below are agent decisions, reopenable by the Owner
(recorded in TASK-2026-00335).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "OUTCOMES",
    "RESPONSES",
    "STORE_FILENAME",
    "Precedent",
    "classify_response",
    "complete_outcome",
    "empty_store",
    "evidenced",
    "load_store",
    "outcome_from_verdict",
    "precedents_for_scope",
    "record_ceo_precedent",
    "record_owner_precedent",
    "save_store",
]

#: Owner responses (D10: approved, edited with a concrete change, denied).
RESPONSES = ("approved", "edited", "denied")

#: Observed results (D10: hits, failures and correct abstentions).
#: ``None`` means inconclusive: not evidence, never retrieved.
OUTCOMES = ("success", "failure", "abstention")

#: Runtime store file, next to the skill metrics (gitignored, committed never).
STORE_FILENAME = "precedents.json"

#: Reference summaries never carry ticket bodies, only pointers.
MAX_SUMMARY_CHARS = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(text: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit] + "…(truncated)"


def classify_response(*, approve: bool, comment: str) -> str:
    """Owner answer class: denied, edited, or approved.

    An approval carrying Owner text counts as edited: the Owner added
    a concrete change on top of the proposal. A bare approval counts
    as approved. A denial stays denied whatever it says.
    """
    if not approve:
        return "denied"
    if str(comment or "").strip():
        return "edited"
    return "approved"


def outcome_from_verdict(resultado: str, *, verified: bool) -> str | None:
    """Map a close verdict to an observed outcome, None when inconclusive.

    Only OpenCEO-verified results count (principle 7): an unverified
    ``hecho`` stays inconclusive instead of guessing a hit.
    """
    verdict = str(resultado or "").strip().lower()
    if verdict == "hecho":
        return "success" if verified else None
    if verdict == "bloqueado":
        return "failure"
    if verdict == "no_aplica":
        return "abstention"
    return None


@dataclass
class Precedent:
    """One linked proposal -> Owner response -> observed result."""

    precedent_id: str
    project: str
    scope: str
    created_at: str
    proposal_kind: str
    proposal_ref: str
    proposal_summary: str
    origin_ref: str | None = None
    response: str | None = None
    response_by: str | None = None
    response_at: str | None = None
    response_note: str = ""
    outcome: str | None = None
    outcome_at: str | None = None
    outcome_ref: str | None = None


def empty_store() -> dict[str, Any]:
    """A fresh precedent store: one sequence plus precedents by id."""
    return {"seq": 0, "precedents": {}}


def _next_id(store: dict[str, Any]) -> str:
    store["seq"] = int(store.get("seq", 0)) + 1
    return f"PR-{store['seq']:04d}"


def _entry(precedent: Precedent) -> dict[str, Any]:
    return asdict(precedent)


def record_owner_precedent(
    store: dict[str, Any],
    *,
    project: str,
    scope: str,
    question_task_id: str,
    question_kind: str,
    question_summary: str,
    origin_task_id: str | None,
    approve: bool,
    answered_by: str,
    comment: str,
    decided_at: str | None = None,
) -> Precedent:
    """Log one Owner answer as a precedent with references only.

    Raises ``ValueError`` when the question reference or the project
    is missing: a precedent without its pointers is untraceable.
    """
    question = str(question_task_id or "").strip()
    if not question:
        raise ValueError("a precedent needs its owner-question reference")
    if not str(project or "").strip():
        raise ValueError("a precedent needs its project")
    precedent = Precedent(
        precedent_id=_next_id(store),
        project=str(project).strip(),
        scope=str(scope or "").strip() or "dev",
        created_at=_now(),
        proposal_kind=str(question_kind or "").strip() or "question",
        proposal_ref=question,
        proposal_summary=_short(question_summary),
        origin_ref=str(origin_task_id or "").strip() or None,
        response=classify_response(approve=approve, comment=comment),
        response_by=str(answered_by or "").strip() or "owner",
        response_at=str(decided_at or "").strip() or _now(),
        response_note=_short(comment),
    )
    store.setdefault("precedents", {})[precedent.precedent_id] = _entry(
        precedent)
    return precedent


def record_ceo_precedent(
    store: dict[str, Any],
    *,
    project: str,
    scope: str,
    review_id: str,
    action: str,
    decision_summary: str,
    created_ref: str | None = None,
    decided_at: str | None = None,
) -> Precedent:
    """Log one CEO review: the decision is the response under study.

    ``do_nothing`` counts as the abstention under study, not as an
    empty review: D11 learns from the times doing nothing was right.
    The observed result still completes later by event.
    """
    review = str(review_id or "").strip()
    if not review:
        raise ValueError("a precedent needs its CEO review reference")
    if not str(project or "").strip():
        raise ValueError("a precedent needs its project")
    clean_action = str(action or "").strip() or "do_nothing"
    precedent = Precedent(
        precedent_id=_next_id(store),
        project=str(project).strip(),
        scope=str(scope or "").strip() or "ceo",
        created_at=_now(),
        proposal_kind=f"ceo_review:{clean_action}",
        proposal_ref=review,
        proposal_summary=_short(decision_summary),
        origin_ref=str(created_ref or "").strip() or None,
        response=clean_action,
        response_by="ceo",
        response_at=str(decided_at or "").strip() or _now(),
    )
    store.setdefault("precedents", {})[precedent.precedent_id] = _entry(
        precedent)
    return precedent


def complete_outcome(
    store: dict[str, Any],
    precedent_id: str,
    *,
    outcome: str | None,
    evidence_ref: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Complete the observed result by event. Never raises past the caller.

    ``outcome=None`` marks the inconclusive close: the entry keeps
    ``outcome=None`` and never counts as evidence. An unknown outcome
    label is rejected with ``ValueError`` (fail-closed: only verified
    results count, never guesses). The first completion wins: a later
    event never rewrites an already observed result.
    """
    entry = store.get("precedents", {}).get(str(precedent_id))
    if not isinstance(entry, dict):
        return {"applied": False, "reason": "unknown precedent"}
    if entry.get("outcome") is not None:
        return {"applied": False, "reason": "outcome already recorded"}
    if outcome is None:
        entry["outcome_at"] = str(at or "").strip() or _now()
        entry["outcome_ref"] = str(evidence_ref or "").strip() or None
        return {"applied": True, "outcome": None}
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown precedent outcome {outcome!r}")
    entry["outcome"] = outcome
    entry["outcome_at"] = str(at or "").strip() or _now()
    entry["outcome_ref"] = str(evidence_ref or "").strip() or None
    return {"applied": True, "outcome": outcome}


def precedents_for_scope(
    store: Mapping[str, Any],
    project: str,
    scope: str,
) -> list[dict[str, Any]]:
    """Precedents of one project and scope, oldest first (3.4 builds on this)."""
    rows = []
    raw = store.get("precedents", {})
    if not isinstance(raw, dict):
        return []
    for key in sorted(raw):
        entry = raw[key]
        if not isinstance(entry, dict):
            continue
        if entry.get("project") != project or entry.get("scope") != scope:
            continue
        rows.append(dict(entry))
    return rows


def evidenced(store: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Precedents with an observed result; inconclusive ones never count."""
    raw = store.get("precedents", {})
    if not isinstance(raw, dict):
        return []
    return [dict(entry) for key, entry in sorted(raw.items())
            if isinstance(entry, dict) and entry.get("outcome") is not None]


def default_store_path(repo_root: str | Path) -> Path:
    """Runtime precedent file for one checkout (gitignored, never committed)."""
    return Path(repo_root) / ".runtime" / STORE_FILENAME


def load_store(path: str | Path) -> dict[str, Any]:
    """Read a precedent store; missing or corrupt reads as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_store()
    if not isinstance(data, dict) or not isinstance(
            data.get("precedents"), dict):
        return empty_store()
    return data


def save_store(store: dict[str, Any], path: str | Path) -> None:
    """Persist a precedent store, creating the runtime dir as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8",
                                     dir=str(target.parent),
                                     delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    os.replace(tmp_path, target)


