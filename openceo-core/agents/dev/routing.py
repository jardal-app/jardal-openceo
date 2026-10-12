"""Single resolver for the task routing field ``coding_tool``.

Tier S design v2 §3 (TASK-2026-00102, F0.1): ``TaskDocument.coding_tool`` is
the only routing field, written once at creation with a tier letter
(``tier_s``/``tier_a``/``tier_b``/``tier_c``) and read only through
:func:`resolve_coding_tool`, at the top level only.

Retired by TASK-2026-00138 (the migration ran dry with nothing left to
promote): the numeric tiers, the pre-v2 provider values and the
``params``/``project``/``tier`` fallback sources, together with the ingest
normalization and the one-shot migration. New code must never write them;
stale values resolve to ``None`` so the caller waits with
``routing_missing`` (or explicit dev takes the safe C default at
dispatch, TASK-2026-00204).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from state import CodingTool

#: Typed wait reason when no routing value resolves (design §3.2, §14.2).
#: The ticket is not dispatched; there is no fallback.
ROUTING_MISSING_REASON = "routing_missing"

#: Default Tier C executor chain (design v2 §3.1, §17.1): first available.
TIER_C_CHAIN: tuple[str, ...] = ("muse", "dsh", "claudep")


def _coerce_tier(value: Any) -> CodingTool | None:
    """Exact tier letter, or None."""
    if value is None:
        return None
    if isinstance(value, CodingTool):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    try:
        return CodingTool(normalized)
    except ValueError:
        return None


@dataclass(frozen=True)
class RoutingDecision:
    """Result of :func:`resolve_coding_tool`.

    ``source`` is ``root`` (top-level field, the only source),
    ``explicit_default`` (built by the dev session for explicit dev
    without a tier, not the resolver) or ``none``.
    """

    tool: CodingTool | None
    source: str
    detail: str


def resolve_coding_tool(task: Mapping[str, Any] | None) -> RoutingDecision:
    """Resolve the execution tier for a task. The only routing reader.

    Reads the top-level ``coding_tool`` field only. Anything else —
    including unknown or retired values — resolves to ``None`` so the
    caller waits with ``routing_missing`` instead of dispatching.
    """
    task = task or {}
    raw_root = task.get("coding_tool")
    tool = _coerce_tier(raw_root)
    if tool is not None:
        return RoutingDecision(tool, "root", f"coding_tool={tool.value!r}")
    if raw_root is not None and str(raw_root).strip() != "":
        return RoutingDecision(None, "none", f"invalid coding_tool={raw_root!r}")
    return RoutingDecision(None, "none", "no routing value")


def resolved_tool_name(task: Mapping[str, Any] | None) -> str:
    """Tier value for slot/quota accounting."""
    decision = resolve_coding_tool(task)
    return decision.tool.value if decision.tool is not None else ""


def select_tier_c_link(
    availability: Mapping[str, bool],
    chain: Sequence[str] = TIER_C_CHAIN,
) -> str:
    """First available Tier C executor (design v2 §3.1).

    Pure: ``availability`` maps link name to probed availability and
    ``chain`` is the configured order. Falls back to the last link so the
    caller always has an executor; the links after the first are reactive
    (tried until they fail), so a conservative probe must not veto them.
    """
    ordered = [str(link) for link in chain if str(link)]
    if not ordered:
        raise ValueError("Tier C chain is empty")
    for link in ordered:
        if availability.get(link, False):
            return link
    return ordered[-1]
