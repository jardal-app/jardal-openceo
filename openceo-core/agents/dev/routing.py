"""Single resolver for the task routing field ``coding_tool``.

Tier S design §3 (TASK-2026-00085): ``TaskDocument.coding_tool`` is the only
routing field, written once at creation and read only through
:func:`resolve_coding_tool`.

Transition (one version): legacy paths are still accepted — normalized to the
top-level field at ingest (:func:`normalize_document_routing` /
:func:`normalize_task_dict`) with a deprecation warning carrying the
``task_id``. The next version rejects them; ``resolve_coding_tool`` then only
reads the top-level field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from state import CodingTier, CodingTool, TaskDocument

#: Typed wait reason when no routing value resolves (design §3.2, §11.2).
#: The ticket is not dispatched; there is no fallback.
ROUTING_MISSING_REASON = "routing_missing"

#: Legacy tier → tool mapping, preserved from the previous dispatcher.
_TIER_TO_TOOL: dict[CodingTier, CodingTool] = {
    CodingTier.TIER1: CodingTool.CODEX,
    CodingTier.TIER2: CodingTool.CLAUDE,
    CodingTier.TIER3: CodingTool.OPENCODE,
    CodingTier.TIER4: CodingTool.KIMI,
}

_TOOL_ALIASES = {"claude-code": CodingTool.CLAUDE}


def tool_for_tier(tier: CodingTier) -> CodingTool:
    """Map a legacy :class:`CodingTier` to its tool (transition only)."""
    return _TIER_TO_TOOL[tier]


def _coerce_tool(value: Any) -> CodingTool | None:
    if value is None:
        return None
    if isinstance(value, CodingTool):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in _TOOL_ALIASES:
        return _TOOL_ALIASES[normalized]
    try:
        return CodingTool(normalized)
    except ValueError:
        return None


def _coerce_tier(value: Any) -> CodingTier | None:
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
    source), ``params``, ``project``, ``tier`` or ``none``.
    """

    tool: CodingTool | None
    source: str
    deprecated: bool
    detail: str


def resolve_coding_tool(
    task: Mapping[str, Any] | None,
    project_config: Mapping[str, Any] | None = None,
) -> RoutingDecision:
    """Resolve the executor for a task. The only routing reader.

    Precedence during the transition (behavior-preserving): top-level field,
    then ``params.coding_tool``, then the project ``dev.coding_tool`` default,
    then the legacy ``coding_tier`` mapping. Anything else — including unknown
    values everywhere — resolves to ``None`` so the caller waits with
    ``routing_missing`` instead of dispatching.
    """
    task = task or {}
    params = task.get("params")
    if not isinstance(params, dict):
        params = {}

    raw_root = task.get("coding_tool")
    tool = _coerce_tool(raw_root)
    if tool is not None:
        return RoutingDecision(tool, "root", False, f"coding_tool={tool.value!r}")

    notes: list[str] = []
    if raw_root is not None and str(raw_root).strip() != "":
        notes.append(f"invalid coding_tool={raw_root!r}")

    raw_params = params.get("coding_tool")
    tool = _coerce_tool(raw_params)
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
    tool = _coerce_tool(raw_project)
    if tool is not None:
        return RoutingDecision(tool, "project", True, f"dev.coding_tool={tool.value!r}")
    if raw_project is not None and str(raw_project).strip() != "":
        notes.append(f"invalid dev.coding_tool={raw_project!r}")

    raw_tier = task.get("coding_tier") or params.get("coding_tier")
    tier = _coerce_tier(raw_tier)
    if tier is not None:
        mapped = tool_for_tier(tier)
        return RoutingDecision(mapped, "tier", True, f"coding_tier={tier.value} -> {mapped.value}")
    if raw_tier is not None and str(raw_tier).strip() != "":
        notes.append(f"invalid coding_tier={raw_tier!r}")

    detail = "; ".join(notes) if notes else "no routing value"
    return RoutingDecision(None, "none", False, detail)


def resolved_tool_name(task: Mapping[str, Any] | None) -> str:
    """Tool value for slot/quota accounting (legacy-aware during transition)."""
    decision = resolve_coding_tool(task)
    return decision.tool.value if decision.tool is not None else ""


def normalize_task_dict(task: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Promote legacy routing paths of a task dict to top-level ``coding_tool``.

    Returns ``(patch, warnings)``. The patch only ever sets ``coding_tool`` and
    only when the top-level field is missing or invalid; it never overwrites a
    valid value. Callers log each warning with the ``task_id``.
    """
    params = task.get("params")
    if not isinstance(params, dict):
        params = {}
    patch: dict[str, Any] = {}
    warnings: list[str] = []

    raw_root = task.get("coding_tool")
    if _coerce_tool(raw_root) is not None:
        return patch, warnings
    if raw_root is not None and str(raw_root).strip() != "":
        warnings.append(f"ignoring invalid coding_tool={raw_root!r}")

    legacy = _coerce_tool(params.get("coding_tool"))
    if legacy is not None:
        patch["coding_tool"] = legacy.value
        warnings.append(f"deprecated params.coding_tool promoted to {legacy.value!r}")
        return patch, warnings

    tier = _coerce_tier(task.get("coding_tier") or params.get("coding_tier"))
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
    """Plan the one-shot promotion of legacy routing (design §3.3, F0).

    Pure function over task dicts; the caller's script applies ``promoted``
    patches with ``patch_task`` and reports ``needs_assignment``. Idempotent:
    re-running over migrated tasks promotes nothing.
    """
    plan = RoutingMigrationPlan()
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        if _coerce_tool(task.get("coding_tool")) is not None:
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
        coerced = _coerce_tool(patch["coding_tool"])
        if coerced is not None:
            doc.coding_tool = coerced
    return warnings
