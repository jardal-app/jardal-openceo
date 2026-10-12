"""Autonomous CEO decision contract (autonomy epic TASK-2026-00110).

F4 TASK-2026-00119. The CEO (one Muse headless run per review, the same
mechanism as the objective planner) returns :class:`CEODecision` JSON;
Pydantic validates the shape and :func:`validate_ceo_decision` applies
the deterministic anti-bloat rules before anything persists. Invalid
output never creates, never escalates.

Not to be confused with :mod:`objectives.planner_models`: the planner
decides *which tickets exist* for one objective; the CEO decides
*whether*, *when* and *how much* company work starts at all.

``CEO_DECISION.json`` is the machine contract. There is no YAML mirror.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Collection, Mapping, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from company_mandate import (
    MandateError,
    MandatoEmpresa,
    OwnerAction,
    requires_owner,
)
from objectives.planner_models import PlannedCost

__all__ = [
    "CEOAction",
    "CEO_DECISION_FILE",
    "CEODecision",
    "CEOKpiReading",
    "CEOValidationContext",
    "DUPLICATE_ESCALATION_PREFIX",
    "EscalationSpec",
    "NewPedidoSpec",
    "normalize_title",
    "parse_ceo_decision",
    "validate_ceo_decision",
]

#: Result file the headless run writes into the orchestrator scratch root
#: (``.tier-s/``, TASK-2026-00136). Dropped before every run, like the
#: planner result: a run that writes nothing reads as missing.
CEO_DECISION_FILE = "CEO_DECISION.json"

#: Error prefix :func:`validate_ceo_decision` uses when the review wants
#: to escalate but the identical escalation is already open. The review
#: flow maps it to an ``escalation_exists`` outcome (no second ticket),
#: not to a failure.
DUPLICATE_ESCALATION_PREFIX = "duplicate_escalation:"


class CEOAction(str, Enum):
    """The four review outcomes (ARCH §Proactive CEO, CEO loop)."""

    KEEP_GOING = "keep_going"
    CREAR_PEDIDO = "crear_pedido"
    ESCALATE = "escalate"
    DO_NOTHING = "do_nothing"


def normalize_title(value: Any) -> str:
    """Duplicate-comparison form. Mirrors the planner rule (§17)."""
    return str(value or "").strip().lower()


def _string_list_nonblank(value: list[str]) -> list[str]:
    for item in value:
        if not str(item).strip():
            raise ValueError("must not contain blank items")
    return value


class NewPedidoSpec(BaseModel):
    """Work with no decided form (TASK-2026-00213).

    The strategy states the need; the single planner decides ticket or
    objective, agent, skill and (dev) tier. ``project`` is a known
    project where the pedido is filed; ``costs`` reuses the planner
    shape so the spend gate reads it unchanged; ``project_key`` (with
    ``display_name``) marks a new-project pedido for the operations
    ``crear-proyecto`` skill.
    """

    project: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    acceptance: list[str] = Field(default_factory=list)
    eje_id: str = Field(min_length=1)
    kpi_ids: list[str] = Field(min_length=1)
    costs: list[PlannedCost] = Field(default_factory=list)
    project_key: str | None = None
    display_name: str | None = None

    @field_validator("project", "title", "description", "eje_id")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("acceptance", "kpi_ids")
    @classmethod
    def _lists_nonblank(cls, value: list[str]) -> list[str]:
        return _string_list_nonblank(value)

    @field_validator("project_key", "display_name")
    @classmethod
    def _optional_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def validate_project_request(self) -> "NewPedidoSpec":
        if (self.project_key is None) != (self.display_name is None):
            raise ValueError(
                "project_key and display_name travel together "
                "(a new-project pedido names both, other pedidos neither)"
            )
        return self


class EscalationSpec(BaseModel):
    kind: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    project: str | None = None
    # JSON carries money as a number or a string; floats arrive from
    # JSON literals, whose shortest repr round-trips the literal the
    # model wrote, so float -> str -> Decimal is exact here. The mandate
    # still rejects float objects passed directly (binary money).
    amount: str | int | float | None = None
    uses_paid_quota: bool = False
    irreversible: bool = False
    strategic_replan: bool = False
    mandate_change: bool = False
    policy_requires_human_review: bool = False
    human_action_hint: str | None = None

    @field_validator("kind", "summary")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("project", "human_action_hint")
    @classmethod
    def _optional_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_json_safe(cls, value: Any) -> str | int | float | None:
        # Before the union coercion: a bool would silently become 1.
        if isinstance(value, bool):
            raise ValueError("amount must be a number or a decimal string")
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("amount must be finite")
            return str(value)
        if isinstance(value, str) and not value.strip():
            raise ValueError("amount must not be blank")
        return value


class CEODecision(BaseModel):
    action: CEOAction
    summary: str = Field(min_length=1)
    pedido: NewPedidoSpec | None = None
    escalation: EscalationSpec | None = None

    @field_validator("summary")
    @classmethod
    def _summary_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def validate_action_contract(self) -> "CEODecision":
        specs = {
            "pedido": self.pedido,
            "escalation": self.escalation,
        }
        present = {name for name, spec in specs.items() if spec is not None}
        expected = {
            CEOAction.CREAR_PEDIDO: {"pedido"},
            CEOAction.ESCALATE: {"escalation"},
            CEOAction.KEEP_GOING: set(),
            CEOAction.DO_NOTHING: set(),
        }[self.action]
        if present != expected:
            action = self.action.value
            if not expected:
                raise ValueError(
                    f"{action} carries no spec "
                    f"(got: {sorted(present) or 'none'})"
                )
            raise ValueError(
                f"{action} requires exactly the "
                f"{sorted(expected)[0]} spec "
                f"(got: {sorted(present) or 'none'})"
            )
        return self


def parse_ceo_decision(text: str) -> CEODecision:
    """Parse CEO JSON output. Raises ValueError when malformed."""
    try:
        data = json.loads(text or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid {CEO_DECISION_FILE}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{CEO_DECISION_FILE} must be a JSON object")
    try:
        return CEODecision.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: "
            f"{error['msg']}" for error in exc.errors()[:8])
        raise ValueError(f"invalid CEODecision: {details}") from exc


@dataclass(frozen=True)
class CEOKpiReading:
    """One KPI read during signal compilation.

    ``value`` is the measured value (``0`` is measured, never unknown);
    ``unknown`` means no reader covers the source or the read failed.
    """

    value: Any = None
    unknown: bool = True


@dataclass
class CEOValidationContext:
    """Deterministic facts the anti-bloat rules check against.

    ``open_escalations`` items carry ``{category, human_action}``: the
    dedupe key is the deterministic human action (same cause under the
    same mandate renders the same text), so no new mandate storage is
    needed. The ``*_unknown`` flags fail creation closed (blind limits
    or blind duplicates never create); escalation stays allowed when
    its dedupe view is blind — asking the owner is the safe direction.
    """

    mandate: MandatoEmpresa | None = None
    known_projects: Collection[str] = field(default_factory=list)
    ops_project: str = ""
    open_titles: Collection[str] = field(default_factory=list)
    open_titles_unknown: bool = False
    active_objectives: Sequence[Mapping[str, Any]] = field(
        default_factory=list)
    objectives_unknown: bool = False
    open_escalations: Sequence[Mapping[str, Any]] = field(
        default_factory=list)
    escalations_unknown: bool = False
    kpi_readings: Mapping[str, CEOKpiReading] = field(default_factory=dict)

    def reading_known(self, eje_id: str, kpi_id: str) -> bool:
        reading = self.kpi_readings.get(f"{eje_id}:{kpi_id}")
        return reading is not None and not reading.unknown


def _check_axis_kpis(
    decision_label: str,
    eje_id: str,
    kpi_ids: Sequence[str],
    ctx: CEOValidationContext,
    errors: list[str],
) -> None:
    """Axis/KPI link + measurability (scope §2/§4)."""
    mandate = ctx.mandate
    if mandate is None:
        errors.append(f"{decision_label}: no valid company mandate")
        return
    axes = {eje.id: eje for eje in mandate.ejes}
    eje = axes.get(str(eje_id or "").strip())
    if eje is None:
        errors.append(
            f"{decision_label}: unknown eje {str(eje_id)!r} "
            f"(mandate axes: {sorted(axes) or 'none'})"
        )
        return
    declared = [kpi.id for kpi in eje.kpis]
    if not declared:
        errors.append(
            f"{decision_label}: eje {eje.id!r} declares no KPI: "
            "an axis without a KPI yields a report, never new work"
        )
        return
    unknown = [kpi for kpi in kpi_ids if kpi not in declared]
    if unknown:
        errors.append(
            f"{decision_label}: kpi_ids {unknown} are not declared "
            f"on eje {eje.id!r} (declared: {declared})"
        )
        return
    measured = [
        kpi for kpi in kpi_ids if ctx.reading_known(eje.id, kpi)
    ]
    if not measured:
        errors.append(
            f"{decision_label}: eje {eje.id!r} has no measured KPI "
            f"this review (linked: {list(kpi_ids)}): an axis without "
            "a measurable KPI yields a report, never new work"
        )


def _validate_new_pedido(
    spec: NewPedidoSpec, ctx: CEOValidationContext
) -> list[str]:
    errors: list[str] = []
    label = "crear_pedido"
    if ctx.open_titles_unknown:
        errors.append(
            f"{label}: open titles unreadable: refusing to create "
            "against a blind duplicate check"
        )
    if ctx.objectives_unknown:
        errors.append(
            f"{label}: active objectives unreadable: refusing to "
            "create against a blind duplicate check"
        )
    if str(spec.project) not in set(ctx.known_projects):
        errors.append(
            f"{label}: unknown project {spec.project!r}: a pedido "
            "is filed in exactly one known project"
        )
    _check_axis_kpis(label, spec.eje_id, spec.kpi_ids, ctx, errors)
    normalized = normalize_title(spec.title)
    if normalized in {normalize_title(t) for t in ctx.open_titles}:
        errors.append(f"{label}: duplicates an open ticket")
    for item in ctx.active_objectives:
        if normalize_title(item.get("goal")) == normalized:
            errors.append(
                f"{label}: duplicates active objective "
                f"{item.get('objective_id')}"
            )
            break
    if spec.project_key is not None:
        assert spec.display_name is not None  # Shape-guarded.
        key = str(spec.project_key or "").strip()
        try:
            from ops.create_project import normalize_key
        except ImportError:  # pragma: no cover - packaging guard
            normalize_key = None  # type: ignore[assignment]
        if normalize_key is not None:
            try:
                normalized_key = normalize_key(key)
            except ValueError as exc:
                errors.append(f"{label}: invalid project_key: {exc}")
                normalized_key = ""
        else:  # pragma: no cover - packaging guard
            normalized_key = key
        if normalized_key and normalized_key in set(ctx.known_projects):
            errors.append(
                f"{label}: project {normalized_key!r} already exists: "
                "the CEO extends it instead of recreating it"
            )
        if str(spec.project) != str(ctx.ops_project or "").strip():
            errors.append(
                f"{label}: a new-project pedido is filed in the ops "
                f"project {ctx.ops_project!r} (got {spec.project!r})"
            )
    return errors


def _validate_escalation(
    spec: EscalationSpec, ctx: CEOValidationContext
) -> list[str]:
    errors: list[str] = []
    label = "escalate"
    action = OwnerAction(
        kind=str(spec.kind),
        summary=str(spec.summary),
        project=str(spec.project) if spec.project else None,
        amount=spec.amount,
        uses_paid_quota=bool(spec.uses_paid_quota),
        irreversible=bool(spec.irreversible),
        strategic_replan=bool(spec.strategic_replan),
        mandate_change=bool(spec.mandate_change),
        policy_requires_human_review=bool(
            spec.policy_requires_human_review),
        human_action_hint=spec.human_action_hint,
    )
    try:
        decision = requires_owner(action, ctx.mandate)
    except MandateError as exc:
        return [f"{label}: invalid escalation action: {exc}"]
    if not decision.escalate or decision.category is None:
        return [
            f"{label}: no escalation signal under the mandate "
            f"({decision.reason}): the CEO acts directly instead"
        ]
    wanted = (
        decision.category.value,
        normalize_title(decision.human_action),
    )
    for item in ctx.open_escalations:
        if not isinstance(item, Mapping):
            continue
        candidate = (
            str(item.get("category") or "").strip(),
            normalize_title(item.get("human_action")),
        )
        if candidate == wanted:
            return [
                f"{DUPLICATE_ESCALATION_PREFIX} {wanted[0]} already open: "
                f"{str(action.summary).strip()[:160]}"
            ]
    return errors


def validate_ceo_decision(
    decision: CEODecision, ctx: CEOValidationContext
) -> list[str]:
    """Deterministic anti-bloat validation. Returns errors (pure, sync).

    ``keep_going``/``do_nothing`` always validate (they persist nothing
    but the review record). A pedido validates project, axis/KPI link
    with a measured KPI, and duplicates against open tickets and active
    objectives; form limits apply where the form materializes (objective
    admission, ticket dispatch). Escalation validates the policy cause
    through the single mandate policy and dedupes against already-open
    escalations.
    """
    if decision.action == CEOAction.CREAR_PEDIDO:
        assert decision.pedido is not None  # Shape-guarded.
        return _validate_new_pedido(decision.pedido, ctx)
    if decision.action == CEOAction.ESCALATE:
        assert decision.escalation is not None  # Shape-guarded.
        return _validate_escalation(decision.escalation, ctx)
    return []
