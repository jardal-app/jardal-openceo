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
    "MissionCloseSpec",
    "NewPedidoSpec",
    "normalize_title",
    "parse_ceo_decision",
    "validate_ceo_decision",
]

#: Result file the headless run writes into the orchestrator scratch root
#: (``.openceo/``, TASK-2026-00136). Dropped before every run, like the
#: planner result: a run that writes nothing reads as missing.
CEO_DECISION_FILE = "CEO_DECISION.json"

#: Error prefix :func:`validate_ceo_decision` uses when the review wants
#: to escalate but the identical escalation is already open. The review
#: flow maps it to an ``escalation_exists`` outcome (no second ticket),
#: not to a failure.
DUPLICATE_ESCALATION_PREFIX = "duplicate_escalation:"


class CEOAction(str, Enum):
    """The five review outcomes (ARCH §Proactive CEO, CEO loop)."""

    KEEP_GOING = "keep_going"
    CREAR_PEDIDO = "crear_pedido"
    ESCALATE = "escalate"
    DO_NOTHING = "do_nothing"
    CLOSE_MISSION = "close_mission"


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
    ``crear-proyecto`` skill. The mandate link (TASK-2026-00193) is a
    measured KPI or, when none is measured, a written ``justificacion``.
    ``mission_id`` (TASK-2026-00218) chains the pedido as the next
    objective of that mission.
    """

    project: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    acceptance: list[str] = Field(default_factory=list)
    eje_id: str | None = None
    kpi_ids: list[str] = Field(default_factory=list)
    justificacion: str | None = None
    mission_id: str | None = None
    costs: list[PlannedCost] = Field(default_factory=list)
    project_key: str | None = None
    display_name: str | None = None

    @field_validator("project", "title", "description")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("acceptance", "kpi_ids")
    @classmethod
    def _lists_nonblank(cls, value: list[str]) -> list[str]:
        return _string_list_nonblank(value)

    @field_validator(
        "eje_id", "justificacion", "mission_id", "project_key",
        "display_name",
    )
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
    # The mission the question is about: the answer joins its record.
    mission_id: str | None = None
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

    @field_validator("project", "human_action_hint", "mission_id")
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


class MissionCloseSpec(BaseModel):
    """Mission the CEO declares complete (TASK-2026-00218).

    The review judged the mission acceptance met; the engine closes
    with evidence (every mission objective and its delivery record).
    """

    mission_id: str = Field(min_length=1)

    @field_validator("mission_id")
    @classmethod
    def _mission_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class CEODecision(BaseModel):
    action: CEOAction
    summary: str = Field(min_length=1)
    pedido: NewPedidoSpec | None = None
    escalation: EscalationSpec | None = None
    mission: MissionCloseSpec | None = None

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
            "mission": self.mission,
        }
        present = {name for name, spec in specs.items() if spec is not None}
        expected = {
            CEOAction.CREAR_PEDIDO: {"pedido"},
            CEOAction.ESCALATE: {"escalation"},
            CEOAction.CLOSE_MISSION: {"mission"},
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
    missions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    mission_objectives: Mapping[str, Sequence[Mapping[str, Any]]] = field(
        default_factory=dict)
    missions_unknown: bool = False
    #: Missions the review was woken to judge with nothing in flight.
    judged_idle_missions: Collection[str] = frozenset()

    def reading_known(self, eje_id: str, kpi_id: str) -> bool:
        reading = self.kpi_readings.get(f"{eje_id}:{kpi_id}")
        return reading is not None and not reading.unknown


def _check_axis_kpis(
    decision_label: str,
    spec: NewPedidoSpec,
    ctx: CEOValidationContext,
    errors: list[str],
) -> None:
    """Mandate link: a measured KPI or a written justificacion (§2/§4).

    The KPI is optional since TASK-2026-00193: a pedido without a
    measured KPI carries the reason in words instead. Unknown axes and
    undeclared links still fail; KPIs need their axis to mean anything.
    """
    mandate = ctx.mandate
    if mandate is None:
        errors.append(f"{decision_label}: no valid company mandate")
        return
    eje_id = str(spec.eje_id or "").strip()
    kpi_ids = [str(item) for item in spec.kpi_ids]
    justified = bool(str(spec.justificacion or "").strip())
    if not eje_id:
        if kpi_ids:
            errors.append(
                f"{decision_label}: kpi_ids {kpi_ids} without eje_id: "
                "a KPI link needs its axis"
            )
        elif not justified:
            errors.append(
                f"{decision_label}: without a mandate eje, a written "
                "justificacion is required"
            )
        return
    axes = {eje.id: eje for eje in mandate.ejes}
    eje = axes.get(eje_id)
    if eje is None:
        errors.append(
            f"{decision_label}: unknown eje {eje_id!r} "
            f"(mandate axes: {sorted(axes) or 'none'})"
        )
        return
    declared = [kpi.id for kpi in eje.kpis]
    unknown = [kpi for kpi in kpi_ids if kpi not in declared]
    if unknown:
        errors.append(
            f"{decision_label}: kpi_ids {unknown} are not declared "
            f"on eje {eje.id!r} (declared: {declared or 'none'})"
        )
        return
    measured = [
        kpi for kpi in kpi_ids if ctx.reading_known(eje.id, kpi)
    ]
    if not measured and not justified:
        errors.append(
            f"{decision_label}: eje {eje.id!r} has no measured KPI "
            f"this review (linked: {kpi_ids or 'none'}): link a "
            "measured KPI or write the justificacion"
        )


#: Objective statuses that end a mission entry (00218).
_MISSION_TERMINAL_OBJECTIVE = frozenset({"closed", "cancelled"})


def _check_mission_link(
    decision_label: str,
    mission_id: str,
    ctx: CEOValidationContext,
    errors: list[str],
) -> Mapping[str, Any] | None:
    """Mission exists, is active, and its shell takes children."""
    if ctx.missions_unknown:
        errors.append(
            f"{decision_label}: missions unreadable: refusing to chain "
            "against a blind mission check"
        )
        return None
    mission = ctx.missions.get(str(mission_id or "").strip())
    if mission is None:
        errors.append(
            f"{decision_label}: unknown mission {str(mission_id)!r}")
        return None
    if str(mission.get("status") or "") != "active":
        errors.append(
            f"{decision_label}: mission {mission_id} is not active "
            f"({mission.get('status')})")
        return None
    shell = str(mission.get("shell_status") or "").strip()
    if shell in {"cancelled", "completed"}:
        errors.append(
            f"{decision_label}: mission {mission_id} shell is {shell} "
            "in ERP: no new objectives chain under it")
        return None
    return mission


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
    _check_axis_kpis(label, spec, ctx, errors)
    if str(spec.mission_id or "").strip():
        _check_mission_link(
            label, str(spec.mission_id or "").strip(), ctx, errors)
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
    mission_id = str(spec.mission_id or "").strip()
    if mission_id and not ctx.missions_unknown \
            and mission_id not in ctx.missions:
        errors.append(f"{label}: mission_id {mission_id} is not an "
                      "active mission")
    return errors


def _validate_mission_close(
    spec: MissionCloseSpec, ctx: CEOValidationContext
) -> list[str]:
    errors: list[str] = []
    label = "close_mission"
    if ctx.missions_unknown:
        return [f"{label}: missions unreadable: refusing to close blind"]
    mission_id = str(spec.mission_id or "").strip()
    mission = ctx.missions.get(mission_id)
    if mission is None:
        return [f"{label}: unknown mission {mission_id!r}"]
    if str(mission.get("status") or "") != "active":
        return [f"{label}: mission {mission_id} is not active "
                f"({mission.get('status')})"]
    if str(mission.get("shell_status") or "") == "cancelled":
        return [f"{label}: mission {mission_id} shell is cancelled "
                "in ERP: engine convergence needs a cancel flow"]
    if not [str(item).strip()
            for item in mission.get("acceptance") or []
            if str(item).strip()]:
        errors.append(
            f"{label}: mission {mission_id} has no acceptance "
            "criteria: nothing judges completion")
    children = list(ctx.mission_objectives.get(mission_id) or [])
    open_children = [
        str(item.get("objective_id") or "")
        for item in children
        if str(item.get("status") or "") not in _MISSION_TERMINAL_OBJECTIVE
    ]
    if open_children:
        errors.append(
            f"{label}: mission {mission_id} still runs "
            f"{len(open_children)} objectives "
            f"({', '.join(open_children[:5])}): close_mission needs "
            "every mission objective closed or cancelled")
    if not any(str(item.get("status") or "") == "closed"
               for item in children):
        errors.append(
            f"{label}: mission {mission_id} closed no objective yet: "
            "a mission closes on delivered evidence, never empty")
    return errors


def validate_ceo_decision(
    decision: CEODecision, ctx: CEOValidationContext
) -> list[str]:
    """Deterministic anti-bloat validation. Returns errors (pure, sync).

    ``keep_going``/``do_nothing`` validate (they persist nothing but the
    review record) unless the review was woken to judge a mission with
    nothing in flight (TASK-2026-00594). A pedido validates project, the mandate
    link (a measured KPI or a written justificacion), and duplicates
    against open tickets and active objectives; form limits apply where
    the form materializes (objective admission, ticket dispatch).
    Escalation validates the policy cause through the single mandate
    policy and dedupes against already-open escalations.
    ``close_mission`` validates the mission is active with acceptance,
    every mission objective terminal, and at least one closed.
    """
    if decision.action == CEOAction.CREAR_PEDIDO:
        assert decision.pedido is not None  # Shape-guarded.
        return _validate_new_pedido(decision.pedido, ctx)
    if decision.action == CEOAction.ESCALATE:
        assert decision.escalation is not None  # Shape-guarded.
        return _validate_escalation(decision.escalation, ctx)
    if decision.action == CEOAction.CLOSE_MISSION:
        assert decision.mission is not None  # Shape-guarded.
        return _validate_mission_close(decision.mission, ctx)
    if ctx.judged_idle_missions:
        stuck = ", ".join(sorted(ctx.judged_idle_missions))
        return [
            f"{decision.action.value}: mission {stuck} has nothing in "
            "flight and this review was woken to judge it, so it would "
            "stay stuck. Continue it (crear_pedido with mission_id), close "
            "it (close_mission) if its acceptance is met, or escalate with "
            "mission_id exactly what only the Owner can still decide, "
            "never what its notes already decided"
        ]
    return []
