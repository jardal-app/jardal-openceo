"""Contratos del servicio research (TASK-2026-00058).

Que fija este modulo
====================
1. La **solicitud** valida sus campos, pero la identidad (owner), los grants, el techo
   de clasificacion y los limites efectivos los deriva el servidor del principal
   autenticado. Un payload no eleva derechos: `owner_id` y `budget_ref` se aceptan
   para trazabilidad y jamas otorgan autorizacion.
2. El **resultado** separa hecho, declaracion, inferencia y rumor; cada afirmacion
   referencia evidencia y declara el extracto o la ubicacion que la respalda. Las
   fechas de publicacion, del suceso y de observacion son distintas por diseno.
3. El dinero se representa como entero escalado en micro-USD y se serializa como
   cadena decimal. Estimacion y reconciliacion nunca se confunden; desconocido es
   `None`, nunca 0.
4. Existe **un solo** enum de estado y una tabla de transiciones con terminales
   estables. No hay un septimo estado `interrupted`: un `running` huerfano pasa a
   `partial` (si conserva evidencia util) o `failed`, con causa
   `runtime_interrupted`, y no se relanza gasto de forma automatica.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research.money import MoneyFormatError, format_micro, parse_optional_micro

__all__ = [
    "AccessMethod",
    "ClaimKind",
    "Classification",
    "CoverageReason",
    "EvidenceSupport",
    "ResearchAccess",
    "ResearchClaim",
    "ResearchCoverageGap",
    "ResearchEvidence",
    "ResearchFreshness",
    "ResearchIdempotencyConflict",
    "ResearchJobRecord",
    "ResearchJobRequest",
    "ResearchJobResult",
    "ResearchJobStatus",
    "ResearchLimits",
    "ResearchMode",
    "ResearchTransitionError",
    "ResearchUsage",
    "RetentionPolicy",
    "SourceType",
    "TERMINAL_STATUSES",
    "ALLOWED_TRANSITIONS",
    "can_transition",
    "freshness_state",
    "interrupted_recovery",
    "most_restrictive",
    "new_research_id",
    "result_reusable",
    "utcnow",
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_research_id() -> str:
    return f"res_{uuid4().hex}"


class ResearchIdempotencyConflict(RuntimeError):
    """Misma clave idempotente con un payload incompatible.

    No es un error de programa: es un conflicto de cliente y la fachada lo traduce a
    409. Se comprueba con el fingerprint, no comparando texto crudo.
    """


class ResearchTransitionError(RuntimeError):
    """Transicion de estado no permitida.

    Los terminales son estables: no se reabre un job, se crea otro. Tampoco existe
    un estado `interrupted`; un `running` huerfano termina en `partial` o `failed`.
    """


class ResearchMode(str, Enum):
    QUICK = "quick"
    DEEP = "deep"
    WATCH = "watch"


class ResearchJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {
        ResearchJobStatus.COMPLETED,
        ResearchJobStatus.PARTIAL,
        ResearchJobStatus.FAILED,
        ResearchJobStatus.CANCELLED,
    }
)

# Cancelar es posible desde cualquier estado no terminal. Un terminal es estable:
# solo admite "transitar" a si mismo (idempotencia), nunca reabrirse.
ALLOWED_TRANSITIONS: dict[ResearchJobStatus, frozenset[ResearchJobStatus]] = {
    ResearchJobStatus.QUEUED: frozenset(
        {ResearchJobStatus.RUNNING, ResearchJobStatus.CANCELLED, ResearchJobStatus.FAILED}
    ),
    ResearchJobStatus.RUNNING: frozenset(
        {
            ResearchJobStatus.COMPLETED,
            ResearchJobStatus.PARTIAL,
            ResearchJobStatus.FAILED,
            ResearchJobStatus.CANCELLED,
            ResearchJobStatus.BLOCKED,
        }
    ),
    ResearchJobStatus.BLOCKED: frozenset({ResearchJobStatus.QUEUED, ResearchJobStatus.CANCELLED}),
    ResearchJobStatus.COMPLETED: frozenset({ResearchJobStatus.COMPLETED}),
    ResearchJobStatus.PARTIAL: frozenset({ResearchJobStatus.PARTIAL}),
    ResearchJobStatus.FAILED: frozenset({ResearchJobStatus.FAILED}),
    ResearchJobStatus.CANCELLED: frozenset({ResearchJobStatus.CANCELLED}),
}


def can_transition(current: ResearchJobStatus, new: ResearchJobStatus) -> bool:
    return new in ALLOWED_TRANSITIONS.get(current, frozenset())


def interrupted_recovery(has_useful_evidence: bool) -> tuple[ResearchJobStatus, str]:
    """Estado y causa de un `running` huerfano tras un reinicio.

    Nunca inventa un estado `interrupted` ni relanza gasto: si quedo evidencia util,
    el job es `partial`; si no, `failed`. Ambos con causa `runtime_interrupted`.
    """
    status = ResearchJobStatus.PARTIAL if has_useful_evidence else ResearchJobStatus.FAILED
    return status, "runtime_interrupted"


def freshness_state(
    observed_at: datetime | None,
    *,
    max_age_hours: int | None,
    now: datetime | None = None,
) -> str:
    """``fresh`` | ``stale`` | ``unknown`` para decidir reutilizacion de un resultado.

    Sin ventana pedida no hay requisito de frescura (`fresh`). Con ventana y sin fecha
    de observacion, la frescura **no puede acreditarse**: es `unknown` y no se
    reutiliza. Un resultado viejo se declara `stale`; no se borra la identidad de un
    reintento para "refrescar" en silencio.
    """
    if max_age_hours is None:
        return "fresh"
    if observed_at is None:
        return "unknown"
    moment = now or utcnow()
    observed = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    age_seconds = (moment - observed).total_seconds()
    return "stale" if age_seconds > max_age_hours * 3600 else "fresh"


def result_reusable(
    observed_at: datetime | None,
    *,
    max_age_hours: int | None,
    now: datetime | None = None,
) -> bool:
    return freshness_state(observed_at, max_age_hours=max_age_hours, now=now) == "fresh"


class ClaimKind(str, Enum):
    FACT = "fact"
    DECLARATION = "declaration"
    INFERENCE = "inference"
    RUMOR = "rumor"


class Classification(str, Enum):
    """Clasificacion de privacidad. Se compara por severidad, no por nombre."""

    PUBLIC = "public"
    RESTRICTED = "restricted"
    PRIVATE = "private"

    @property
    def severity(self) -> int:
        return {"public": 0, "restricted": 1, "private": 2}[self.value]


def most_restrictive(*values: Classification) -> Classification:
    return max(values, key=lambda c: c.severity) if values else Classification.PUBLIC


class AccessMethod(str, Enum):
    HTTP = "http"
    RSS = "rss"
    API = "api"
    FIRECRAWL = "firecrawl"
    BROWSER = "browser"
    GROUNDING = "grounding"
    SOCIAL = "social"


class SourceType(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    AGGREGATOR = "aggregator"
    SOCIAL = "social"
    UNKNOWN = "unknown"


class RetentionPolicy(str, Enum):
    SOURCE_SPECIFIC = "source-specific"
    PUBLIC_7D = "public-7d"
    WATCH_90D = "watch-90d"
    REPORT_180D = "report-180d"


class CoverageReason(str, Enum):
    """Causas de cobertura. No son estados de job ni significan "no hay noticias"."""

    QUOTA_EXHAUSTED = "quota_exhausted"
    LOGIN_REQUIRED = "login_required"
    SOURCE_UNAVAILABLE = "source_unavailable"
    MODEL_POLICY_UNAVAILABLE = "model_policy_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"


class VerificationVerdict(str, Enum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchAccess(_StrictModel):
    classification: Classification = Classification.PUBLIC
    account_refs: list[str] = Field(default_factory=list)


class ResearchFreshness(_StrictModel):
    max_age_hours: int | None = None

    @field_validator("max_age_hours")
    @classmethod
    def _non_negative(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("max_age_hours no puede ser negativo")
        return value


class ResearchLimits(_StrictModel):
    """Limites solicitados. El servidor los recorta contra su techo efectivo."""

    search_requests: int = 3
    pages: int = 5
    steps: int = 8
    seconds: int = 120

    @field_validator("search_requests", "pages", "steps", "seconds")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Un limite no puede ser negativo")
        return value


class ResearchJobRequest(_StrictModel):
    """Solicitud de investigacion.

    ``owner_id`` y ``budget_ref`` son **propuestas del cliente**: el servidor los
    contrasta con el principal autenticado y su politica, y puede reducirlos, pero
    nunca ampliarlos por lo que diga el payload.
    """

    project_key: str
    purpose: str
    mode: ResearchMode = ResearchMode.QUICK
    deep_paid_opt_in: bool = Field(default=False, strict=True)
    question: str
    seed_urls: list[str] = Field(default_factory=list)
    freshness: ResearchFreshness = Field(default_factory=ResearchFreshness)
    access: ResearchAccess = Field(default_factory=ResearchAccess)
    limits: ResearchLimits | None = None
    budget_ref: str | None = None
    idempotency_key: str | None = None
    owner_id: str | None = None

    @field_validator("project_key", "purpose", "question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("El campo no puede estar vacio")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _blank_key_is_absent(cls, value: str | None) -> str | None:
        # Cadena vacia = sin clave. Se normaliza a None para que el documento omita
        # el campo y el indice parcial no bloquee consultas nuevas.
        if value is None:
            return None
        text = value.strip()
        return text or None

    def fingerprint(self) -> str:
        """Hash canonico del payload que define la identidad de la solicitud.

        Excluye ``owner_id`` (lo deriva el servidor) y ``idempotency_key`` (es la
        clave, no el contenido). Dos payloads con la misma clave y fingerprints
        distintos son un conflicto, no una deduplicacion.
        """
        payload = self.model_dump(mode="json", exclude={"owner_id", "idempotency_key"})
        if not self.deep_paid_opt_in:
            payload.pop("deep_paid_opt_in")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EvidenceSupport(_StrictModel):
    """Ubicacion o extracto que respalda una afirmacion dentro de una evidencia."""

    location: str | None = None
    extract: str | None = None

    @model_validator(mode="after")
    def _needs_something(self) -> "EvidenceSupport":
        if not (self.location or self.extract):
            raise ValueError("El soporte necesita ubicacion o extracto")
        return self


class ResearchEvidence(_StrictModel):
    id: str
    url: str
    access_method: AccessMethod
    visibility: Classification = Classification.PUBLIC
    source_type: SourceType = SourceType.UNKNOWN
    title: str | None = None
    published_at: datetime | None = None
    event_at: datetime | None = None
    observed_at: datetime | None = None
    content_hash: str | None = None
    retention_policy: RetentionPolicy = RetentionPolicy.SOURCE_SPECIFIC
    # Scope de persistencia. En el formato de cable pueden ir vacios; al guardar y
    # al consultar son obligatorios y el filtro se aplica antes de leer.
    project_key: str | None = None
    owner_id: str | None = None


class ResearchClaim(_StrictModel):
    id: str
    statement: str
    kind: ClaimKind
    evidence_ids: list[str] = Field(default_factory=list)
    support: list[EvidenceSupport] = Field(default_factory=list)
    verification: VerificationVerdict | None = None


class ResearchEvent(_StrictModel):
    schema_version: int = 1
    event_id: str
    research_id: str
    project_key: str
    owner_id: str
    classification: Classification
    entity: str
    event_type: str = "research_result"
    event_at: datetime | None = None
    observed_at: datetime
    version: int = Field(default=1, ge=1)
    corrects_event_id: str | None = None
    claims: list[ResearchClaim] = Field(default_factory=list)
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    coverage_gaps: list["ResearchCoverageGap"] = Field(default_factory=list)
    verification_history: list[dict[str, Any]] = Field(default_factory=list)
    freshness: str = "unknown"
    status: ResearchJobStatus


class ResearchCoverageGap(_StrictModel):
    source: str
    reason: CoverageReason
    detail: str | None = None


class ResearchUsage(_StrictModel):
    """Consumo de una investigacion.

    ``estimated_micro_usd`` es la reserva/estimacion previa y
    ``reconciled_micro_usd`` el importe realmente liquidado. Si no puede afirmarse,
    ``reconciled_micro_usd`` es `None` (incierto) y `uncertain` queda marcado.
    """

    search_requests: int = 0
    pages_read: int = 0
    model_calls: int = 0
    estimated_micro_usd: int | None = None
    reconciled_micro_usd: int | None = None
    uncertain: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "search_requests": self.search_requests,
            "pages_read": self.pages_read,
            "model_calls": self.model_calls,
            "estimated_usd": format_micro(self.estimated_micro_usd),
            "reconciled_usd": None if self.uncertain else format_micro(self.reconciled_micro_usd),
            "uncertain": self.uncertain,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ResearchUsage":
        return cls(
            search_requests=int(payload.get("search_requests") or 0),
            pages_read=int(payload.get("pages_read") or 0),
            model_calls=int(payload.get("model_calls") or 0),
            estimated_micro_usd=parse_optional_micro(payload.get("estimated_usd")),
            reconciled_micro_usd=parse_optional_micro(payload.get("reconciled_usd")),
            uncertain=bool(payload.get("uncertain") or False),
        )


class ResearchJobResult(_StrictModel):
    research_id: str
    status: ResearchJobStatus
    project_key: str
    classification: Classification
    summary: str = ""
    claims: list[ResearchClaim] = Field(default_factory=list)
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    coverage_gaps: list[ResearchCoverageGap] = Field(default_factory=list)
    usage: ResearchUsage = Field(default_factory=ResearchUsage)
    report_ref: str | None = None
    failure_reason: str | None = None

    @model_validator(mode="after")
    def _status_is_result_like(self) -> "ResearchJobResult":
        if self.status in {ResearchJobStatus.QUEUED, ResearchJobStatus.RUNNING}:
            raise ValueError("Un resultado no puede tener estado queued/running")
        return self

    def mechanical_issues(self) -> list[str]:
        """Problemas mecanicos del informe: referencias y citas.

        Esto **no** prueba que la cita respalde el hecho; solo que la estructura es
        coherente. Un ID o una URL validos no acreditan soporte semantico, y el
        servicio debe conservar `insufficient_evidence`/`partial` cuando no pueda
        verificarse.
        """
        issues: list[str] = []
        known = {e.id for e in self.evidence}
        if len(known) != len(self.evidence):
            issues.append("evidence_ids_repetidos")
        for claim in self.claims:
            if claim.kind in {ClaimKind.FACT, ClaimKind.DECLARATION} and not claim.evidence_ids:
                issues.append(f"claim_sin_evidencia:{claim.id}")
            for evidence_id in claim.evidence_ids:
                if evidence_id not in known:
                    issues.append(f"claim_evidencia_inexistente:{claim.id}:{evidence_id}")
            if claim.evidence_ids and not claim.support:
                issues.append(f"claim_soporte_vacio:{claim.id}")
        for evidence in self.evidence:
            if not evidence.url.startswith(("http://", "https://")):
                issues.append(f"evidencia_url_invalida:{evidence.id}")
        return issues


class ResearchJobRecord(_StrictModel):
    """Documento persistido en ``research_jobs``.

    No es un ticket: no se escribe en ``tasks`` ni dispara el observador de Jardal ERP.
    """

    model_config = ConfigDict(extra="ignore")

    research_id: str
    project_key: str
    owner_id: str
    purpose: str
    mode: ResearchMode
    question: str
    request_fingerprint: str
    idempotency_key: str | None = None
    status: ResearchJobStatus = ResearchJobStatus.QUEUED
    classification: Classification = Classification.PUBLIC
    budget_ref: str | None = None
    effective_limits: dict[str, int] = Field(default_factory=dict)
    seed_urls: list[str] = Field(default_factory=list)
    attempts: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: ResearchJobResult | None = None
    failure_reason: str | None = None
    coverage_gaps: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


def normalize_money_input(value: Any) -> int | None:
    """Traduce una entrada de dinero a micro-USD o lanza; nunca devuelve 0 por error."""
    try:
        return parse_optional_micro(value)
    except MoneyFormatError:
        raise
