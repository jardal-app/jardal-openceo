"""Single resolver for the task routing field ``coding_tool``.

Tier S design v2 §3 (TASK-2026-00102, F0.1): ``TaskDocument.coding_tool`` is
the only routing field, written once at creation with a tier letter
(``tier_s``/``tier_a``/``tier_b``/``tier_c``) and read only through
:func:`resolve_coding_tool`.

Transition: pre-v2 provider values (``codex``, ``claude``, ``muse``,
``opencode``, ``kimi``, ``glm``, ``gemini``…) still stored on open tasks are
mapped to their tier with ``deprecated=True`` so they keep dispatching while
the one-shot migration promotes them. Legacy paths (``params.coding_tool``,
project ``dev.coding_tool``, ``coding_tier``) stay readable as deprecated
until F2 removes the old engine; new code must never write them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from state import CodingTier, CodingTool, TaskDocument

#: Typed wait reason when no routing value resolves (design §3.2, §14.2).
#: The ticket is not dispatched; there is no fallback.
ROUTING_MISSING_REASON = "routing_missing"

#: Pre-v2 provider value → tier (design v2 §19.2, plus the account siblings
#: and minor providers the design does not name: same account family for
#: ``clauded``/``codexf``, default tier_c for the rest).
_LEGACY_TOOL_TO_TIER: dict[str, CodingTool] = {
    "claude": CodingTool.TIER_A,
    "claude-code": CodingTool.TIER_A,
    "claudej": CodingTool.TIER_A,
    "clauded": CodingTool.TIER_A,
    "codex": CodingTool.TIER_B,
    "codexj": CodingTool.TIER_B,
    "codexf": CodingTool.TIER_B,
    "muse": CodingTool.TIER_C,
    "opencode": CodingTool.TIER_C,
    "kimi": CodingTool.TIER_C,
    "glm": CodingTool.TIER_C,
    "gemini": CodingTool.TIER_C,
    "low": CodingTool.TIER_C,
}

#: Legacy tier → tier mapping, preserving the previous dispatcher semantics
#: (TIER1 ran Codex, TIER2 ran Claude, TIER3 ran OpenCode one-shot).
#: Design v2 §19.2 pins ``tier3`` → ``tier_c``; the rest follow the same
#: provider map.
_TIER_TO_TOOL: dict[CodingTier, CodingTool] = {
    CodingTier.TIER1: CodingTool.TIER_B,
    CodingTier.TIER2: CodingTool.TIER_A,
    CodingTier.TIER3: CodingTool.TIER_C,
    CodingTier.TIER4: CodingTool.TIER_C,
}

#: Default Tier C executor chain (design v2 §3.1, §17.1): first available.
TIER_C_CHAIN: tuple[str, ...] = ("muse", "dsh", "claudep")


def tool_for_tier(tier: CodingTier) -> CodingTool:
    """Map a legacy :class:`CodingTier` to its tier (transition only)."""
    return _TIER_TO_TOOL[tier]


def _coerce_tier(value: Any) -> CodingTool | None:
    """Exact tier letter, or None (legacy providers are NOT tiers)."""
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


def _coerce_legacy_tool(value: Any) -> CodingTool | None:
    """Pre-v2 provider value mapped to its tier, or None."""
    if value is None or isinstance(value, CodingTool):
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return _LEGACY_TOOL_TO_TIER.get(normalized)


def _coerce_tier_value(value: Any) -> CodingTier | None:
    if value is None:
        return None
    if isinstance(value, CodingTier):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    try:
        return CodingTier(normalized)
    except ValueError:
        return None


@dataclass(frozen=True)
class RoutingDecision:
    """Result of :func:`resolve_coding_tool`.

    ``source`` is one of ``root`` (top-level field, the only non-deprecated
    source when it carries a tier letter), ``params``, ``project``, ``tier``
    or ``none``. A legacy provider value at the root still reports source
    ``root`` but with ``deprecated=True``.
    """

    tool: CodingTool | None
    source: str
    deprecated: bool
    detail: str


def resolve_coding_tool(
    task: Mapping[str, Any] | None,
    project_config: Mapping[str, Any] | None = None,
) -> RoutingDecision:
    """Resolve the execution tier for a task. The only routing reader.

    Precedence during the transition (behavior-preserving): top-level field
    (tier letter, or legacy provider mapped with ``deprecated=True``), then
    ``params.coding_tool``, then the project ``dev.coding_tool`` default,
    then the legacy ``coding_tier`` mapping. Anything else — including
    unknown values everywhere — resolves to ``None`` so the caller waits
    with ``routing_missing`` instead of dispatching.
    """
    task = task or {}
    params = task.get("params")
    if not isinstance(params, dict):
        params = {}

    raw_root = task.get("coding_tool")
    tool = _coerce_tier(raw_root)
    if tool is not None:
        return RoutingDecision(tool, "root", False, f"coding_tool={tool.value!r}")
    legacy = _coerce_legacy_tool(raw_root)
    if legacy is not None:
        return RoutingDecision(
            legacy, "root", True,
            f"legacy coding_tool={str(raw_root).strip()!r} -> {legacy.value!r}",
        )

    notes: list[str] = []
    if raw_root is not None and str(raw_root).strip() != "":
        notes.append(f"invalid coding_tool={raw_root!r}")

    raw_params = params.get("coding_tool")
    tool = _coerce_tier(raw_params) or _coerce_legacy_tool(raw_params)
    if tool is not None:
        return RoutingDecision(tool, "params", True, f"params.coding_tool={tool.value!r}")
    if raw_params is not None and str(raw_params).strip() != "":
        notes.append(f"invalid params.coding_tool={raw_params!r}")

    dev_cfg: Mapping[str, Any] = {}
    if isinstance(project_config, Mapping):
        candidate = project_config.get("dev")
        if isinstance(candidate, Mapping):
            dev_cfg = candidate
    raw_project = dev_cfg.get("coding_tool")
    tool = _coerce_tier(raw_project) or _coerce_legacy_tool(raw_project)
    if tool is not None:
        return RoutingDecision(tool, "project", True, f"dev.coding_tool={tool.value!r}")
    if raw_project is not None and str(raw_project).strip() != "":
        notes.append(f"invalid dev.coding_tool={raw_project!r}")

    raw_tier = task.get("coding_tier") or params.get("coding_tier")
    tier = _coerce_tier_value(raw_tier)
    if tier is not None:
        mapped = tool_for_tier(tier)
        return RoutingDecision(mapped, "tier", True, f"coding_tier={tier.value} -> {mapped.value}")
    if raw_tier is not None and str(raw_tier).strip() != "":
        notes.append(f"invalid coding_tier={raw_tier!r}")

    detail = "; ".join(notes) if notes else "no routing value"
    return RoutingDecision(None, "none", False, detail)


def resolved_tool_name(task: Mapping[str, Any] | None) -> str:
    """Tier value for slot/quota accounting (legacy-aware during transition)."""
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


def normalize_task_dict(task: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Promote legacy routing of a task dict to a top-level tier letter.

    Returns ``(patch, warnings)``. The patch only ever sets ``coding_tool``
    and only when the top-level field is missing, invalid, or a legacy
    provider value; it never overwrites a tier letter. Callers log each
    warning with the ``task_id``.
    """
    params = task.get("params")
    if not isinstance(params, dict):
        params = {}
    patch: dict[str, Any] = {}
    warnings: list[str] = []

    raw_root = task.get("coding_tool")
    if _coerce_tier(raw_root) is not None:
        return patch, warnings
    legacy_root = _coerce_legacy_tool(raw_root)
    if legacy_root is not None:
        patch["coding_tool"] = legacy_root.value
        warnings.append(
            f"legacy coding_tool={str(raw_root).strip()!r} promoted to {legacy_root.value!r}"
        )
        return patch, warnings
    if raw_root is not None and str(raw_root).strip() != "":
        warnings.append(f"ignoring invalid coding_tool={raw_root!r}")

    legacy = _coerce_tier(params.get("coding_tool")) or _coerce_legacy_tool(params.get("coding_tool"))
    if legacy is not None:
        patch["coding_tool"] = legacy.value
        warnings.append(f"deprecated params.coding_tool promoted to {legacy.value!r}")
        return patch, warnings

    tier = _coerce_tier_value(task.get("coding_tier") or params.get("coding_tier"))
    if tier is not None:
        mapped = tool_for_tier(tier)
        patch["coding_tool"] = mapped.value
        warnings.append(f"deprecated coding_tier={tier.value} promoted to {mapped.value!r}")
        return patch, warnings

    return patch, warnings


@dataclass
class RoutingMigrationPlan:
    """Result of :func:`plan_routing_migration` for the one-shot migration.

    ``promoted`` holds one ``{"task_id", "patch", "warnings"}`` dict per task
    whose legacy routing was promoted to the top-level field.
    ``needs_assignment`` holds the ids of open tasks with no routing value
    anywhere: the tier authority must set their ``coding_tool`` explicitly.
    """

    promoted: list[dict[str, Any]] = field(default_factory=list)
    needs_assignment: list[str] = field(default_factory=list)
    already_ok: int = 0


def plan_routing_migration(tasks: Iterable[Mapping[str, Any]]) -> RoutingMigrationPlan:
    """Plan the one-shot promotion of legacy routing (design v2 §19.2, F0.1).

    Pure function over task dicts; the caller's script applies ``promoted``
    patches with ``patch_task`` and reports ``needs_assignment``. Idempotent:
    re-running over migrated tasks promotes nothing.
    """
    plan = RoutingMigrationPlan()
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        if _coerce_tier(task.get("coding_tool")) is not None:
            plan.already_ok += 1
            continue
        patch, warnings = normalize_task_dict(dict(task))
        if patch:
            plan.promoted.append({"task_id": task_id, "patch": patch, "warnings": warnings})
        else:
            plan.needs_assignment.append(task_id)
    return plan


def normalize_document_routing(doc: TaskDocument) -> list[str]:
    """Ingest normalization for a new :class:`TaskDocument` (mutates ``doc``).

    Promotes legacy routing paths to the top-level field so the persisted
    document carries the single routing field. Returns deprecation warnings;
    the caller logs them with ``doc.task_id``.
    """
    view: dict[str, Any] = {
        "task_id": doc.task_id,
        "coding_tool": doc.coding_tool.value if doc.coding_tool is not None else None,
        "coding_tier": doc.coding_tier.value if doc.coding_tier is not None else None,
        "params": doc.params,
    }
    patch, warnings = normalize_task_dict(view)
    if "coding_tool" in patch:
        coerced = _coerce_tier(patch["coding_tool"])
        if coerced is not None:
            doc.coding_tool = coerced
    return warnings
