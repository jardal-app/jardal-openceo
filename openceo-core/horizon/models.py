"""Contratos de datos de Horizon.

Reglas de la especificacion que estos modelos hacen cumplir:

- Jerarquia unica: mision -> objetivo -> hito -> accion -> resultado.
- Los objetivos NO son tickets. Solo la accion enlaza un ``task_id`` canonico,
  porque un compromiso personal puede no tener repositorio (seccion 9.1).
- El dinero se guarda en unidades menores (enteros) para evitar error de coma
  flotante, y nunca se suman monedas distintas sin conversion registrada.
  El modelo de importe vive en la fase que realmente mueve dinero, no aqui.
- Un dato desconocido es ``None`` con incertidumbre ``unknown``. Nunca cero.
  Cero es un valor medido; desconocido es ausencia de medicion.
- Plazo deseado y vencimiento real son campos distintos (seccion 9.3).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from state import utc_now


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class CaptureKind(str, Enum):
    IDEA = "idea"
    PENDING = "pending"
    PROGRESS = "progress"
    COMMITMENT = "commitment"


class CaptureChannel(str, Enum):
    CHAT = "chat"
    VOICE = "voice"
    API = "api"
    IMPORT = "import"


class CaptureStatus(str, Enum):
    NEW = "new"
    LINKED = "linked"
    PARKED = "parked"
    PLANNED = "planned"
    DISMISSED = "dismissed"


class ObjectiveKind(str, Enum):
    GROWTH = "growth"
    FINANCIAL = "financial"
    HEALTH = "health"
    CLIENTS = "clients"
    FAMILY = "family"
    BUSINESS = "business"
    PLATFORM = "platform"
    MISSION = "mission"


class ObjectiveHorizon(str, Enum):
    STAGE = "stage"
    QUARTER = "quarter"
    YEAR = "year"
    SOMEDAY = "someday"


class ObjectiveStatus(str, Enum):
    ACTIVE = "active"
    PARKED = "parked"
    BLOCKED = "blocked"
    DONE = "done"


class ActionStatus(str, Enum):
    READY = "ready"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    DONE = "done"
    BLOCKED = "blocked"


class ActionOwner(str, Enum):
    HUMAN = "human"
    AGENT = "agent"


class Uncertainty(str, Enum):
    """Grado de certeza de un dato (seccion 9.3)."""

    DECLARED = "declared"
    VERIFIED = "verified"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


class ProgressKind(str, Enum):
    PROGRESS = "progress"
    RESULT = "result"
    CORRECTION = "correction"


class DeclaredValue(BaseModel):
    """Un dato con procedencia. Ausencia de dato es ``value=None``, no cero."""

    value: Any | None = None
    uncertainty: Uncertainty = Uncertainty.UNKNOWN
    source: str | None = None
    as_of: datetime | None = None

    @property
    def is_known(self) -> bool:
        return self.value is not None

    @classmethod
    def unknown(cls, source: str | None = None) -> "DeclaredValue":
        return cls(value=None, uncertainty=Uncertainty.UNKNOWN, source=source)

    @classmethod
    def declared(cls, value: Any, source: str, as_of: datetime | None = None) -> "DeclaredValue":
        return cls(value=value, uncertainty=Uncertainty.DECLARED, source=source, as_of=as_of)


class HorizonMission(BaseModel):
    """Mision mundial y preferencias. Solo cambia por decision del usuario."""

    statement: str = ""
    values: list[str] = Field(default_factory=list)
    version: str = "2.1"
    updated_at: datetime = Field(default_factory=utc_now)


class HorizonMandate(BaseModel):
    """Mandato persistente de la seccion 9.9.

    Una autorizacion ya aportada se conserva hasta que el usuario la cambie.
    No se pide renovar la delegacion en cada tarea.
    """

    plan_id: str = "horizon"
    plan_version: str = "2.1"
    operating_mode: str = "autonomous_within_mandate"

    # Limites de planificacion
    active_growth_goals_max: int = 1
    personal_ready_actions_max: int = 6
    personal_in_progress_max: int = 1
    personal_capacity_fraction: float = 0.70
    weekly_human_exceptions_minutes_target: int = 10
    exercise_reserved_before_growth: bool = True
    missing_data_blocks_only_dependent_actions: bool = True

    # Privacidad y efectos externos
    timezone: str = "America/La_Paz"
    personal_external_mirror_enabled: bool = False
    execute_money_movements: bool = False
    external_messages_require_human_review: bool = True

    # Reglas de comportamiento
    infer_completion_from_silence: bool = False
    require_deduplication: bool = True
    allow_scope_expansion_without_approval: bool = False
    capture_ideas_without_reconfirmation: bool = True

    # Procedencia
    source: str | None = None
    imported_at: datetime | None = None


class DeclaredSnapshot(BaseModel):
    """Contexto personal importado, conservado verbatim con version y procedencia.

    El README no es la base diaria: se importa como dato versionado y no
    sobrescribe el progreso al releerse (seccion 9.3).
    """

    source: str
    plan_version: str
    imported_at: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list)


class HorizonMilestone(BaseModel):
    milestone_id: str = Field(default_factory=lambda: _new_id("hml"))
    # Clave natural estable del origen (ej. "VOZ-001"). Permite importar de
    # forma idempotente sin sobrescribir progreso ya registrado.
    source_key: str | None = None
    title: str
    measure: str | None = None
    base: DeclaredValue = Field(default_factory=DeclaredValue)
    target: DeclaredValue = Field(default_factory=DeclaredValue)
    horizon: ObjectiveHorizon = ObjectiveHorizon.STAGE
    status: ObjectiveStatus = ObjectiveStatus.ACTIVE
    # Los hitos avanzan por condicion, no por fecha prometida (seccion 3).
    condition: str | None = None
    desired_date: datetime | None = None
    due_date: datetime | None = None
    evidence: list[str] = Field(default_factory=list)
    blocker: str | None = None


class HorizonObjective(BaseModel):
    objective_id: str = Field(default_factory=lambda: _new_id("hob"))
    user_id: str
    # Clave natural estable del origen (ej. "growth_employment"). Clave del
    # upsert idempotente: reimportar no duplica ni pisa el avance.
    source_key: str | None = None
    title: str
    kind: ObjectiveKind = ObjectiveKind.GROWTH
    horizon: ObjectiveHorizon = ObjectiveHorizon.STAGE
    status: ObjectiveStatus = ObjectiveStatus.ACTIVE
    measure: str | None = None
    milestones: list[HorizonMilestone] = Field(default_factory=list)
    # Una idea aparcada conserva su condicion de revision, no se vuelve obligacion.
    parked_review_condition: str | None = None
    priority: int = 100
    source: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PlanStatus(str, Enum):
    """Estado del plan vigente.

    ``NEEDS_INPUT`` no es lo mismo que ``EMPTY``: el primero significa que falta
    un dato (no se conoce la capacidad) y el segundo que el dia esta saturado de
    verdad. Confundirlos convertiria "no lo se" en "no puedes".
    """

    READY = "ready"
    EMPTY = "empty"
    NEEDS_INPUT = "needs_input"


class BlockKind(str, Enum):
    SCHEDULED = "scheduled"
    ON_DEMAND = "on_demand"
    RESERVED_EXERCISE = "reserved_exercise"
    RESERVED_REST = "reserved_rest"


class TriggerKind(str, Enum):
    DAILY_PLAN = "daily_plan"
    CLOSE_DAY = "close_day"
    WEEKLY_REVIEW = "weekly_review"
    MONTHLY_REVIEW = "monthly_review"
    REACTIVE_REPLAN = "reactive_replan"


class TriggerStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AvisoKind(str, Enum):
    DAILY_PLAN = "daily_plan"
    NEXT_ACTION = "next_action"
    REMINDER = "reminder"


class PlanCapacity(BaseModel):
    """Capacidad del dia, con desconocido explicito.

    ``growth_budget_minutes`` es ``None`` cuando no se conoce el tiempo libre y
    ``0`` cuando se sabe que no hay. Esa distincion es la regla del modelo
    (``DeclaredValue``): desconocido no es cero.
    """

    free_minutes: DeclaredValue = Field(default_factory=DeclaredValue)
    exercise_included: bool | None = None
    exercise_reserved_minutes: int = 0
    transition_minutes_per_block: int = 5
    billable_minutes: int | None = None
    fraction: float = 0.70
    growth_budget_minutes: int | None = None
    scheduled_minutes: int = 0
    uncertainty: Uncertainty = Uncertainty.UNKNOWN
    source: str | None = None
    assumptions: list[str] = Field(default_factory=list)


class PlanBlock(BaseModel):
    """Bloque del dia.

    ``start_at`` es ``None`` en los bloques a demanda: sin horario conocido no
    se inventa una hora libre (seccion 9.5). El bloque existe y se recomienda,
    pero no se le pone reloj.
    """

    block_id: str = Field(default_factory=lambda: _new_id("hblk"))
    kind: BlockKind = BlockKind.SCHEDULED
    action_id: str | None = None
    label: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    minutes: int = 0
    is_growth: bool = True


class PlanExclusion(BaseModel):
    """Motivo por el que una accion no entro en el plan de hoy.

    Es la trazabilidad del paso 4 de la seccion 9.5: "por que no esta X hoy"
    tiene que ser respondible desde el propio plan.
    """

    action_id: str
    title: str = ""
    reason: str


class PlanDecision(BaseModel):
    """Decision reservada al usuario, con recomendacion lista (seccion 9.4)."""

    decision_id: str = Field(default_factory=lambda: _new_id("hdec"))
    kind: str
    recommendation: str
    consequence: str = ""
    blocked_action_ids: list[str] = Field(default_factory=list)
    requires_authorization: bool = False


class PlanAviso(BaseModel):
    """Aviso mostrado al usuario, con la accion que referenciaba.

    Esto es lo que hace posible la fila de la seccion 9.10 "respondes tarde
    'hecho' a un aviso anterior": el aviso guarda que accion se mostro, y una
    confirmacion tardia se resuelve contra esa accion, no contra la vigente.
    """

    aviso_id: str = Field(default_factory=lambda: _new_id("havs"))
    action_id: str
    kind: AvisoKind = AvisoKind.DAILY_PLAN
    channel: str = "api"
    text: str = ""
    shown_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    resolved_action_id: str | None = None


class HorizonPlan(BaseModel):
    """Plan del dia. Una sola version vigente por usuario.

    ``date_key`` es texto ``YYYY-MM-DD`` en la zona del plan, no un ``datetime``
    a medianoche local: Mongo no tiene tipo fecha sin hora y una medianoche
    ingenua se rompe al cambiar de zona horaria o en un salto de horario.
    """

    plan_id: str = Field(default_factory=lambda: _new_id("hplan"))
    user_id: str
    date_key: str
    timezone: str = "America/La_Paz"
    version: int = 1
    is_current: bool = True
    superseded_by: str | None = None
    superseded_at: datetime | None = None
    generated_at: datetime = Field(default_factory=utc_now)
    trigger_kind: str = TriggerKind.DAILY_PLAN.value
    status: PlanStatus = PlanStatus.READY
    reason: str = ""
    decision_factors: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    capacity: PlanCapacity = Field(default_factory=PlanCapacity)
    blocks: list[PlanBlock] = Field(default_factory=list)
    primary_action_id: str | None = None
    secondary_action_ids: list[str] = Field(default_factory=list)
    objective_id: str | None = None
    milestone_id: str | None = None
    candidate_count: int = 0
    excluded: list[PlanExclusion] = Field(default_factory=list)
    pending_decisions: list[PlanDecision] = Field(default_factory=list)
    avisos: list[PlanAviso] = Field(default_factory=list)
    delivered_at: datetime | None = None
    delivery_channel: str | None = None
    message: str = ""


class HorizonTrigger(BaseModel):
    """Trabajo intencional persistido, con vencimiento.

    La seccion 9.2 punto 4 exige persistir el trabajo pendiente: un evento por
    WebSocket no garantiza recuperacion tras un reinicio. ``next_run_at`` es el
    unico campo por el que el timer selecciona.
    """

    trigger_id: str = Field(default_factory=lambda: _new_id("htrg"))
    user_id: str
    kind: TriggerKind = TriggerKind.DAILY_PLAN
    status: TriggerStatus = TriggerStatus.PENDING
    next_run_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    attempts: int = 0
    max_attempts: int = 3
    dedupe_key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class HorizonCapture(BaseModel):
    """Idea, pendiente, avance o compromiso. Una sola coleccion para todo."""

    capture_id: str = Field(default_factory=lambda: _new_id("hcap"))
    user_id: str
    kind: CaptureKind = CaptureKind.IDEA
    text: str
    channel: CaptureChannel = CaptureChannel.CHAT
    origin: str | None = None
    dedupe_key: str
    status: CaptureStatus = CaptureStatus.NEW
    linked_objective_id: str | None = None
    linked_task_id: str | None = None
    review_condition: str | None = None
    requires_decision: bool = False
    decision_reason: str | None = None
    captured_at: datetime = Field(default_factory=utc_now)


class HorizonAction(BaseModel):
    """Accion planificada. Es el unico nivel que enlaza un ticket canonico."""

    action_id: str = Field(default_factory=lambda: _new_id("hact"))
    user_id: str
    objective_id: str | None = None
    milestone_id: str | None = None
    title: str
    task_id: str | None = None
    owner: ActionOwner = ActionOwner.HUMAN
    status: ActionStatus = ActionStatus.READY
    effort_estimated_minutes: int | None = None
    effort_actual_minutes: int | None = None
    dependency: str | None = None
    acceptance: str | None = None
    next_step: str | None = None
    blocked_reason: str | None = None
    priority: int = 100
    # Bloques previstos que se perdieron y por que. La seccion 9.5 pide que tras
    # tres perdidas por la misma causa no se vuelva a programar igual: hay que
    # revisar la capacidad o descomponer. Sin el contador esa regla es
    # inaplicable, porque la memoria del fracaso no vive en ningun sitio.
    lost_blocks: int = 0
    lost_reason: str | None = None
    last_replan_at: datetime | None = None
    # Un vencimiento externo no se mueve desde el planificador (seccion 9.10):
    # una fecha comprometida con un tercero no es una palanca interna.
    external_due_date: bool = False
    # F5: promocion automatica a empresa (TASK-2026-00121). Declaracion
    # explicita de que la accion toca trabajo de empresa; sin ella la accion
    # es personal (fail-closed hacia privacidad). `promotion_*` guardan el
    # resultado de promover, para idempotencia y reintento tras escalado.
    company_project: str | None = None
    company_repo: str | None = None
    promoted_at: datetime | None = None
    promotion_kind: str | None = None
    promotion_ref: str | None = None
    promotion_escalation_task_id: str | None = None
    # HZ-003: Delegacion y dev movil
    delegated_at: datetime | None = None
    delegated_project: str | None = None
    human_supervision_minutes: int = 0
    human_rework_minutes: int = 0
    rework_count: int = 0
    delivered_at: datetime | None = None
    delivery_channel: str | None = None
    result_summary: str | None = None
    evidence_links: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HorizonProgressEvent(BaseModel):
    """Evento de progreso con identificador de origen.

    El indice unico ``(user_id, source_id, entity)`` es lo que impide que un
    reintento de notificacion duplique progreso (secciones 9.3 y 9.10).
    """

    event_id: str = Field(default_factory=lambda: _new_id("hev"))
    user_id: str
    kind: ProgressKind = ProgressKind.PROGRESS
    entity: str
    entity_id: str | None = None
    source: str = "user"
    source_id: str
    objective_id: str | None = None
    text: str = ""
    value: DeclaredValue = Field(default_factory=DeclaredValue)
    occurred_at: datetime = Field(default_factory=utc_now)
    recorded_at: datetime = Field(default_factory=utc_now)


class HorizonProfile(BaseModel):
    """Documento unico por usuario. Conserva mision, mandato y procedencia."""

    user_id: str
    display_name: str = ""
    mission: HorizonMission = Field(default_factory=HorizonMission)
    mandate: HorizonMandate = Field(default_factory=HorizonMandate)
    declared_context: DeclaredSnapshot | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HorizonCaptureRequest(BaseModel):
    text: str
    kind: CaptureKind | None = None
    channel: CaptureChannel = CaptureChannel.API
    origin: str | None = None


class HorizonProgressRequest(BaseModel):
    text: str
    source_id: str | None = None
    objective_id: str | None = None
    # Aviso al que responde el usuario. Sin esto, un "hecho" tardio se
    # resolveria contra la accion vigente en vez de contra la que se le mostro.
    aviso_id: str | None = None


class HorizonPlanRequest(BaseModel):
    """Peticion de plan del dia con la disponibilidad declarada."""

    date_key: str | None = None
    free_minutes: int | None = None
    exercise_included: bool | None = None
    note: str | None = None


class HorizonReviewRequest(BaseModel):
    kind: str = TriggerKind.WEEKLY_REVIEW.value


class HorizonImportRequest(BaseModel):
    source_path: str | None = None
    force: bool = False


class HorizonDelegateRequest(BaseModel):
    action_id: str | None = None
    project: str | None = None
    instruction: str | None = None
    dedupe_key: str | None = None


class HorizonPromoteRequest(BaseModel):
    """Promocion de una accion a trabajo de empresa (F5, TASK-2026-00121)."""

    action_id: str | None = None
    project: str | None = None
    repo: str | None = None
    kind: str = "ticket"
    instruction: str | None = None
    dedupe_key: str | None = None
    # Dinero nuevo en cadena decimal; nunca float (company_mandate lo rechaza).
    amount: str | None = None
    needs_owner_credential: bool = False
    irreversible: bool = False
    strategic_replan: bool = False
    human_action_hint: str | None = None


class HorizonMetricsRequest(BaseModel):
    human_supervision_minutes: int | None = None
    human_rework_minutes: int | None = None
    rework: bool = False
    note: str | None = None


class HorizonDecisionAnswerRequest(BaseModel):
    """Respuesta del dueno a una decision (F5, TASK-2026-00120)."""

    decision: str = "approve"
    comment: str = ""


class HorizonDecisionsDeliverRequest(BaseModel):
    """Marca decisiones como entregadas. Sin ids, marca las agrupadas vistas."""

    task_ids: list[str] | None = None


def normalize_document(value: Any) -> Any:
    """Convierte enums a su valor y recorre dicts y listas.

    ``model_dump()`` conserva el miembro del enum, y en Python 3.11+ su
    ``str()`` es ``"ObjectiveStatus.ACTIVE"`` en lugar de ``"active"``. El
    codigo existente se salva porque compara con ``==`` (los enums heredan de
    ``str``), pero cualquier ``str()`` o comparacion de texto se rompe. Se
    normaliza aqui, en el unico borde donde el dato pasa a almacenamiento.
    """
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: normalize_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_document(item) for item in value]
    return value


def document_dump(model: BaseModel) -> dict[str, Any]:
    """Serializa un modelo de Horizon para Mongo.

    Deja los enums como su valor de texto y conserva las fechas como
    ``datetime``, que es lo que el resto del runtime espera.
    """
    return normalize_document(model.model_dump(mode="python"))