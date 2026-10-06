"""Company mandate and owner-escalation policy (autonomy epic TASK-2026-00110).

F4 TASK-2026-00114. The company mandate is the rules the owner gives the CEO
once: axes with KPIs, money/quota budget, active-work limits, CEO review
cadence, and the escalation policy. It lives versioned beside the project
configs (``projects/company_mandate.yaml``), loads validated, and fails
closed: without a valid mandate there is no autonomy, everything escalates.

Not to be confused with Horizon's personal mandate (``horizon/mandate.py``):
that one governs private planning; this one governs company work. Normative
contract: ``docs/ARCHITECTURE_REFERENCE_2026.md`` §Proactive CEO.

Schema language: the YAML keys are Spanish, faithful to the §3.3 example the
owner already approved in the epic workfile. Code API (functions, models of
the decision) is English. Money is Decimal/decimal-string, never float.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

__all__ = [
    "MANDATE_VERSION",
    "EscalationCategory",
    "EscalationDecision",
    "MandateError",
    "OwnerAction",
    "MandatoEmpresa",
    "apply_mandate_change",
    "build_escalation_ticket",
    "load_mandate_file",
    "parse_mandate",
    "project_requires_human_review",
    "requires_owner",
]

MANDATE_VERSION = 1

#: Owner channels allowed to change the mandate. Voice, Horizon or a ticket
#: carrying the owner's instruction; an agent applies the change.
MANDATE_CHANGE_CHANNELS = ("voice", "horizon", "ticket")

#: CEO review cadences the mandate accepts. The CEO (TASK-2026-00119)
#: resolves them against the persisted-trigger engine.
CEO_CADENCES = ("diaria", "semanal", "quincenal", "mensual")


class MandateError(Exception):
    """The mandate is missing, invalid, or its change was rejected."""


class EscalationCategory(str, Enum):
    """The six contract categories, plus the internal fail-closed one."""

    NEW_SPEND = "gasto_nuevo"
    CREDENTIAL = "credencial"
    IRREVERSIBLE = "accion_irreversible"
    STRATEGIC_REPLAN = "replanificacion_estrategica"
    MANDATE_CHANGE = "cambio_de_mandato"
    POLICY_REVIEW = "revision_exigida_por_politica"
    #: Internal: no valid mandate to decide under. Never part of the YAML
    #: declaration; `requires_owner` returns it when mandate is None.
    MANDATE_UNAVAILABLE = "mandate_unavailable"


#: The declaration every valid mandate carries, no more and no less. A
#: missing entry cannot silently widen autonomy (fail-open), and an unknown
#: entry cannot silently pass (typo): both fail the load.
CONTRACT_CATEGORIES = frozenset(
    {
        EscalationCategory.NEW_SPEND.value,
        EscalationCategory.CREDENTIAL.value,
        EscalationCategory.IRREVERSIBLE.value,
        EscalationCategory.STRATEGIC_REPLAN.value,
        EscalationCategory.MANDATE_CHANGE.value,
        EscalationCategory.POLICY_REVIEW.value,
    }
)

#: Evaluation order = contract order. Deterministic when several signals
#: fire on the same action: the first one wins and is reported.
_CATEGORY_ORDER = (
    EscalationCategory.NEW_SPEND,
    EscalationCategory.CREDENTIAL,
    EscalationCategory.IRREVERSIBLE,
    EscalationCategory.STRATEGIC_REPLAN,
    EscalationCategory.MANDATE_CHANGE,
    EscalationCategory.POLICY_REVIEW,
)


def _nonblank_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must not be blank")
    return text


class KpiMandato(BaseModel):
    id: str = Field(min_length=1)
    fuente: str = Field(min_length=1)

    @field_validator("id", "fuente")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        return _nonblank_text(value, "kpi")


class EjeMandato(BaseModel):
    id: str = Field(min_length=1)
    meta: str = Field(min_length=1)
    kpis: list[KpiMandato] = Field(default_factory=list)

    @field_validator("id", "meta")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        return _nonblank_text(value, "eje")

    @model_validator(mode="after")
    def _kpi_ids_unique(self) -> "EjeMandato":
        ids = [kpi.id for kpi in self.kpis]
        if len(ids) != len(set(ids)):
            raise ValueError(f"eje {self.id!r}: kpi ids must be unique")
        return self


class PresupuestoMandato(BaseModel):
    moneda: str = Field(min_length=1)
    gasto_nuevo_sin_preguntar: Decimal = Field(ge=0)
    cuota: str = Field(min_length=1)

    @field_validator("moneda")
    @classmethod
    def _currency_iso(cls, value: str) -> str:
        code = str(value or "").strip().upper()
        if len(code) != 3 or not code.isalpha():
            raise ValueError("moneda must be a 3-letter ISO 4217 code")
        return code

    @field_validator("cuota")
    @classmethod
    def _cuota_nonblank(cls, value: str) -> str:
        return _nonblank_text(value, "cuota")


class LimitesMandato(BaseModel):
    objetivos_activos: int = Field(ge=1)
    objetivos_activos_por_proyecto: int = Field(ge=1)
    tickets_activos_por_objetivo: int = Field(ge=1)


class RevisionCEOMandato(BaseModel):
    cadencia: str = Field(min_length=1)

    @field_validator("cadencia")
    @classmethod
    def _cadence_known(cls, value: str) -> str:
        cadence = str(value or "").strip().lower()
        if cadence not in CEO_CADENCES:
            raise ValueError(
                f"cadencia must be one of {', '.join(CEO_CADENCES)}"
            )
        return cadence


class MandatoEmpresa(BaseModel):
    version: int = MANDATE_VERSION
    ejes: list[EjeMandato] = Field(default_factory=list)
    presupuesto: PresupuestoMandato
    limites: LimitesMandato
    revision_ceo: RevisionCEOMandato
    escalar_al_dueno: list[str] = Field(min_length=6, max_length=6)

    @field_validator("version")
    @classmethod
    def _version_supported(cls, value: int) -> int:
        if value != MANDATE_VERSION:
            raise ValueError(f"unsupported mandate version: {value!r}")
        return value

    @model_validator(mode="after")
    def _validate_policy(self) -> "MandatoEmpresa":
        ids = [eje.id for eje in self.ejes]
        if len(ids) != len(set(ids)):
            raise ValueError("eje ids must be unique")
        declared = {str(entry or "").strip() for entry in self.escalar_al_dueno}
        if declared != CONTRACT_CATEGORIES:
            missing = sorted(CONTRACT_CATEGORIES - declared)
            unknown = sorted(declared - CONTRACT_CATEGORIES)
            raise ValueError(
                "escalar_al_dueno must declare exactly the six contract "
                f"categories (missing: {missing or 'none'}; "
                f"unknown: {unknown or 'none'})"
            )
        return self


def parse_mandate(data: Any) -> MandatoEmpresa:
    """Validate raw data into a mandate. Raises MandateError when invalid."""
    if not isinstance(data, dict):
        raise MandateError("mandate must be a mapping")
    try:
        return MandatoEmpresa.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: "
            f"{error['msg']}"
            for error in exc.errors()[:8]
        )
        raise MandateError(f"invalid company mandate: {details}") from exc


def load_mandate_file(path: str | Path) -> MandatoEmpresa:
    """Load and validate the mandate file. Raises MandateError on any error."""
    candidate = Path(path)
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise MandateError(f"cannot read mandate file {candidate}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MandateError(f"mandate file {candidate} is not valid YAML: {exc}") from exc
    if data is None:
        raise MandateError(f"mandate file {candidate} is empty")
    try:
        return parse_mandate(data)
    except MandateError as exc:
        raise MandateError(f"mandate file {candidate}: {exc}") from exc


@dataclass(frozen=True)
class OwnerAction:
    """Something the CEO wants to do, described by its escalation signals.

    `kind` is free text (the contract automates "everything else", so new
    kinds need no code change); safety lives in the explicit signals, which
    the caller sets consciously. `amount` is new spend in the mandate
    currency (Decimal/decimal-string/int; float is rejected, money is never
    binary). `uses_paid_quota` covers consuming already-paid subscription
    quota, which is not new spend — but a positive amount next to it is
    incoherent and escalates fail-closed.
    """

    kind: str
    summary: str
    project: str | None = None
    amount: Decimal | str | int | None = None
    uses_paid_quota: bool = False
    needs_owner_credential: bool = False
    irreversible: bool = False
    strategic_replan: bool = False
    mandate_change: bool = False
    policy_requires_human_review: bool = False
    human_action_hint: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EscalationDecision:
    escalate: bool
    category: EscalationCategory | None
    reason: str
    human_action: str | None


def _action_amount(action: OwnerAction) -> Decimal:
    if action.amount is None:
        return Decimal("0")
    if isinstance(action.amount, bool) or isinstance(action.amount, float):
        raise MandateError(
            f"action amount must be Decimal, decimal-string or int, "
            f"got {type(action.amount).__name__}"
        )
    try:
        value = Decimal(str(action.amount).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise MandateError(f"action amount is not a number: {action.amount!r}") from exc
    if value < 0:
        raise MandateError(f"action amount must not be negative: {action.amount!r}")
    return value


def _human_action_for(
    category: EscalationCategory,
    action: OwnerAction,
    mandate: MandatoEmpresa | None,
    amount: Decimal,
) -> str:
    hint = str(action.human_action_hint or "").strip()
    if hint:
        return hint
    summary = action.summary.strip()
    if category == EscalationCategory.NEW_SPEND and mandate is not None:
        threshold = mandate.presupuesto.gasto_nuevo_sin_preguntar
        currency = mandate.presupuesto.moneda
        return (
            f"Decide si autorizas el gasto: {summary} "
            f"(monto declarado {amount} {currency}, "
            f"umbral sin preguntar {threshold} {currency})."
        )
    if category == EscalationCategory.CREDENTIAL:
        return f"Aporta la credencial o acceso que solo tu tienes: {summary}."
    if category == EscalationCategory.IRREVERSIBLE:
        return f"Autoriza o ejecuta tu la accion irreversible: {summary}."
    if category == EscalationCategory.STRATEGIC_REPLAN:
        return f"Decide la replanificacion estrategica: {summary}."
    if category == EscalationCategory.MANDATE_CHANGE:
        return (
            f"El CEO propone un cambio de mandato: {summary}. "
            "Solo tu puedes modificarlo (por voz, Horizon o ticket)."
        )
    if category == EscalationCategory.POLICY_REVIEW:
        project = f" del proyecto {action.project}" if action.project else ""
        return f"La politica{project} exige revision humana: {summary}."
    return (
        "El mandato de empresa falta o es invalido: revisalo en "
        "projects/company_mandate.yaml antes de que el CEO actue."
    )


def requires_owner(
    action: OwnerAction, mandate: MandatoEmpresa | None = None
) -> EscalationDecision:
    """The single deterministic escalation policy. No tokens, no I/O.

    Asks the owner ONLY for the six contract categories; everything else
    (routine replans, ticket creation, development, agent reviews, merges,
    restarts, retries, paid-quota consumption) is automatic. Without a valid
    mandate there is no autonomy: everything escalates (fail-closed).
    """
    kind = str(action.kind or "").strip()
    summary = str(action.summary or "").strip()
    if not kind:
        raise MandateError("action kind must not be blank")
    if not summary:
        raise MandateError("action summary must not be blank")
    amount = _action_amount(action)
    if mandate is None:
        return EscalationDecision(
            escalate=True,
            category=EscalationCategory.MANDATE_UNAVAILABLE,
            reason="no valid company mandate: autonomy is closed",
            human_action=_human_action_for(
                EscalationCategory.MANDATE_UNAVAILABLE, action, None, amount
            ),
        )

    signals: dict[EscalationCategory, str] = {}
    if action.uses_paid_quota:
        if amount > 0:
            signals[EscalationCategory.NEW_SPEND] = (
                f"incoherent: paid quota with a positive amount ({amount})"
            )
    elif amount > mandate.presupuesto.gasto_nuevo_sin_preguntar:
        signals[EscalationCategory.NEW_SPEND] = (
            f"new spend {amount} {mandate.presupuesto.moneda} over the "
            f"{mandate.presupuesto.gasto_nuevo_sin_preguntar} threshold"
        )
    if action.needs_owner_credential:
        signals[EscalationCategory.CREDENTIAL] = "needs a credential only the owner holds"
    if action.irreversible:
        signals[EscalationCategory.IRREVERSIBLE] = "external irreversible action"
    if action.strategic_replan:
        signals[EscalationCategory.STRATEGIC_REPLAN] = "strategic replanning"
    if action.mandate_change:
        signals[EscalationCategory.MANDATE_CHANGE] = "mandate change"
    if action.policy_requires_human_review:
        signals[EscalationCategory.POLICY_REVIEW] = "project policy mandates human review"

    for category in _CATEGORY_ORDER:
        reason = signals.get(category)
        if reason is not None:
            return EscalationDecision(
                escalate=True,
                category=category,
                reason=reason,
                human_action=_human_action_for(category, action, mandate, amount),
            )
    return EscalationDecision(
        escalate=False,
        category=None,
        reason=f"automatic: {kind} carries no escalation signal",
        human_action=None,
    )


def project_requires_human_review(project_config: Mapping[str, Any] | None) -> bool:
    """Sixth category signal, read from merged project config.

    A project whose policy mandates human review (e.g. a client demanding
    human QA) sets ``dev.qa.require_human_review: true`` in its project.yaml.
    Default False; unknown shapes stay False (a missing policy cannot
    silently demand review, and review demand needs an explicit True).
    """
    if not isinstance(project_config, Mapping):
        return False
    dev = project_config.get("dev")
    if not isinstance(dev, Mapping):
        return False
    qa = dev.get("qa")
    if not isinstance(qa, Mapping):
        return False
    return qa.get("require_human_review") is True


# ---------------------------------------------------------------------------
# Escalation ticket spec (pure; TaskService persists it ERPNext-first)
# ---------------------------------------------------------------------------

ESCALATION_PHASE = "owner_escalation"
ESCALATION_TAG = "escalado-dueno"


def build_escalation_ticket(
    action: OwnerAction,
    decision: EscalationDecision,
) -> dict[str, Any]:
    """Spec for the owner-escalation ticket. Pure: no I/O, no tokens.

    The ticket lands in ERPNext as ``Pending Review`` with the exact human
    action, shows in the "Requiere humano" tray (TASK-2026-00041 consumes
    waiting tickets by phase through the search API, no new backend), and
    carries the fields Horizon F5 reads (phase, waiting_reason, tag).
    Raises MandateError when there is nothing to escalate.
    """
    if not decision.escalate or decision.category is None:
        raise MandateError("nothing to escalate: the decision is automatic")
    human_action = str(decision.human_action or "").strip()
    if not human_action:
        raise MandateError("escalation needs the exact human action")
    summary = str(action.summary or "").strip()
    category = decision.category.value
    description_lines = [
        f"Escalado al dueno ({category}).",
        "",
        f"Accion humana: {human_action}",
        "",
        f"Motivo: {decision.reason}",
        f"Accion del CEO: [{action.kind}] {summary}",
    ]
    if action.project:
        description_lines.append(f"Proyecto: {action.project}")
    details = dict(action.details or {})
    if details:
        rendered = "; ".join(f"{key}={details[key]}" for key in sorted(details))
        description_lines.append(f"Contexto: {rendered}")
    return {
        "title": f"[Escalado] {summary}",
        "description": "\n".join(description_lines),
        "tags": [ESCALATION_TAG, f"escalado-{category}"],
        "phase": ESCALATION_PHASE,
        "waiting_reason": f"{ESCALATION_PHASE}:{category}",
        "params": {
            "escalation": {
                "category": category,
                "reason": decision.reason,
                "human_action": human_action,
                "ceo_action_kind": action.kind,
            }
        },
    }


# ---------------------------------------------------------------------------
# Mandate writer: the CEO never edits its own mandate
# ---------------------------------------------------------------------------

def _deep_merge_dict(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _deep_merge_dict(merged.get(key), value)
        return merged
    return override


def apply_mandate_change(
    path: str | Path,
    changes: Mapping[str, Any],
    *,
    requested_by: str,
    channel: str,
) -> MandatoEmpresa:
    """Apply an owner-instructed mandate change. The sanctioned writer.

    Only ``requested_by="owner"`` through ``channel`` in voice/horizon/ticket
    may change the mandate; anything else (notably the CEO itself) is
    rejected before touching the file. The merged result validates: an
    invalid change is rejected and nothing is written. Returns the new
    mandate. Raises MandateError on any rejection.
    """
    if requested_by != "owner":
        raise MandateError(
            "only the owner may change the company mandate "
            f"(requested_by={requested_by!r})"
        )
    if channel not in MANDATE_CHANGE_CHANNELS:
        raise MandateError(
            f"mandate changes arrive by voice, Horizon or ticket "
            f"(channel={channel!r})"
        )
    if not isinstance(changes, Mapping) or not changes:
        raise MandateError("mandate change must be a non-empty mapping")
    candidate = Path(path)
    if candidate.exists():
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            raise MandateError(f"cannot read mandate file {candidate}: {exc}") from exc
        try:
            current = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise MandateError(
                f"mandate file {candidate} is not valid YAML: {exc}"
            ) from exc
        if current is None:
            current = {}
        if not isinstance(current, dict):
            raise MandateError(f"mandate file {candidate} must hold a mapping")
    else:
        current = {}
    merged = _deep_merge_dict(current, dict(changes))
    mandate = parse_mandate(merged)
    try:
        candidate.write_text(
            yaml.safe_dump(
                merged, sort_keys=False, allow_unicode=True, default_flow_style=False
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise MandateError(f"cannot write mandate file {candidate}: {exc}") from exc
    return mandate
