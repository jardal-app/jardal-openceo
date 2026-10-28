"""Precedent recall: similar past cases as information (TASK-2026-00336).

D10/D11: when Dev proposes, or the CEO reviews, OpenCEO recalls up to
:data:`MAX_RECALL` similar past cases from the same project (Dev) or
the same scope (CEO), failures and correct abstentions included.
Recalled cases are information, never a threshold: nothing here
passes, blocks or scores a proposal; callers present the lines from
:func:`format_recall_lines` verbatim.

D11 rules enforced in code, not in prose:

- The CEO receives its previous decisions with their observed result
  (:func:`ceo_review_context`); a KPI move alone never implies
  causality (the context always carries :data:`NO_CAUSALITY_NOTE`).
- The CEO never edits its mandate: this module offers no write path
  to it. Requested threshold, allowlist or mandate changes leave
  through :func:`propose_changes_as_questions` as one grouped Owner
  question; the sanctioned writer stays
  :func:`company_mandate.apply_mandate_change`, which rejects any
  ``requested_by`` other than the owner.
- v1 ships no lessons file.

Project isolation (D2) is structural: ``project`` is required and a
recall never leaves it. Historical replay stays honest through the
``before`` cutoff: only precedents created strictly earlier count,
so a case never learns from its own future.

Seam note (same as ``precedents`` in 3.3): this step ships the pure
in-memory recall plus formatting helpers. No live Dev-prompt or CEO
review path calls it yet; that wiring reuses these functions without
changing them.

No tokens, no ticket outcome changes. :data:`MAX_RECALL` and the
ranking below are agent decisions, reopenable by the Owner (recorded
in TASK-2026-00336).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from orchestration.precedents import evidenced

__all__ = [
    "ALLOWED_CHANGE_KINDS",
    "MAX_RECALL",
    "NO_CAUSALITY_NOTE",
    "ceo_review_context",
    "format_recall_lines",
    "propose_changes_as_questions",
    "recall",
    "recall_for_proposal",
]

#: Recall bound (D10: up to 3 similar cases). Agent decision (TASK-2026-00336).
MAX_RECALL = 3

#: Change kinds a recall may turn into Owner questions (D11/D12).
ALLOWED_CHANGE_KINDS = ("threshold", "allowlist", "mandate")

#: Attached to every CEO context: a KPI move alone proves nothing.
NO_CAUSALITY_NOTE = (
    "A KPI move alone never implies causality: a change observed "
    "after a decision is not evidence that the decision caused it."
)

_WORD = re.compile(r"[a-z0-9áéíóúñü]+", re.IGNORECASE)


def _words(text: Any) -> set[str]:
    return set(_WORD.findall(str(text or "").lower()))


def _score(query_words: set[str], entry: Mapping[str, Any]) -> int:
    haystack = " ".join((
        str(entry.get("proposal_summary") or ""),
        str(entry.get("response_note") or ""),
        str(entry.get("proposal_kind") or ""),
        str(entry.get("response") or ""),
    ))
    return len(query_words & _words(haystack))


def recall(
    store: Mapping[str, Any],
    *,
    project: str,
    scope: str,
    query: str = "",
    limit: int = MAX_RECALL,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Up to ``limit`` similar evidenced precedents, same project and scope.

    Only concluded results count: success, failure and abstention are
    all retrievable; inconclusive (``outcome=None``) entries never are.
    ``before`` keeps historical replay honest: entries created at or
    after the cutoff are skipped. Ranking is deterministic --
    word-overlap score first, then most recent, then id -- and
    ``limit`` clamps to :data:`MAX_RECALL` (fail-closed: never more
    than 3). Raises ``ValueError`` when the project or scope is
    missing: a recall without its pointers could leak across projects.
    """
    if not str(project or "").strip():
        raise ValueError("recall needs its project")
    if not str(scope or "").strip():
        raise ValueError("recall needs its scope")
    try:
        wanted = int(limit)
    except (TypeError, ValueError):
        wanted = MAX_RECALL
    wanted = max(0, min(wanted, MAX_RECALL))
    if wanted == 0:
        return []
    query_words = _words(query)
    cutoff = str(before or "").strip() or None
    ranked: list[tuple[int, str, str, dict[str, Any]]] = []
    for entry in evidenced(store, str(project).strip()):
        if entry.get("scope") != str(scope).strip():
            continue
        created = str(entry.get("created_at") or "")
        if cutoff is not None and created >= cutoff:
            continue
        ranked.append((
            _score(query_words, entry),
            created,
            str(entry.get("precedent_id") or ""),
            dict(entry),
        ))
    ranked.sort(key=lambda item: item[2])
    ranked.sort(key=lambda item: item[1], reverse=True)
    ranked.sort(key=lambda item: -item[0])
    return [dict(item[3]) for item in ranked[:wanted]]


def recall_for_proposal(
    store: Mapping[str, Any],
    *,
    project: str,
    proposal_summary: str,
    limit: int = MAX_RECALL,
    before: str | None = None,
) -> list[dict[str, Any]]:
    """Dev path: similar past cases of the same project, scope ``dev``."""
    return recall(
        store,
        project=project,
        scope="dev",
        query=proposal_summary,
        limit=limit,
        before=before,
    )


def ceo_review_context(
    store: Mapping[str, Any],
    *,
    project: str,
    query: str = "",
    limit: int = MAX_RECALL,
    before: str | None = None,
) -> dict[str, Any]:
    """Previous CEO decisions of this project with their observed result.

    The mandate is never attached for editing: the context carries its
    name only as a read marker (``mandate_editable: False``). Requested
    changes belong to :func:`propose_changes_as_questions`.
    """
    if not str(project or "").strip():
        raise ValueError("a CEO context needs its project")
    precedents = recall(
        store,
        project=project,
        scope="ceo",
        query=query,
        limit=limit,
        before=before,
    )
    return {
        "project": str(project).strip(),
        "scope": "ceo",
        "mandate_editable": False,
        "causality_note": NO_CAUSALITY_NOTE,
        "precedents": precedents,
    }


def propose_changes_as_questions(
    changes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Group requested threshold/allowlist/mandate changes as one question.

    The only sanctioned exit for a change the recall surfaced: a single
    grouped Owner question. Nothing is applied here; applying stays the
    owner's sanctioned writer. Raises ``ValueError`` on an empty list,
    a missing text, or an unknown kind (fail-closed).
    """
    items = list(changes or [])
    if not items:
        raise ValueError("a grouped question needs at least one change")
    grouped: list[dict[str, Any]] = []
    for change in items:
        if not isinstance(change, Mapping):
            raise ValueError("a change must be a mapping")
        kind = str(change.get("kind") or "").strip()
        text = " ".join(str(change.get("text") or "").split())
        if kind not in ALLOWED_CHANGE_KINDS:
            raise ValueError(f"unknown change kind {kind!r}")
        if not text:
            raise ValueError("a change needs its text")
        grouped.append({"kind": kind, "text": text})
    return {"kind": "grouped_owner_question", "items": grouped}


def format_recall_lines(cases: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render recalled cases as informational lines, never a threshold."""
    lines = [
        "Similar past cases (information only, not a threshold):",
    ]
    for entry in cases or []:
        outcome = entry.get("outcome")
        lines.append(
            f"- {entry.get('precedent_id')}: "
            f"{entry.get('proposal_summary')} -> "
            f"{entry.get('response')} / {outcome}"
        )
    return lines
