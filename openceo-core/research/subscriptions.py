"""Servicio de suscripciones research (TASK-2026-00069).

Una suscripcion es un schedule persistido (``research_schedules``) con workflow
``research.watch_tick`` (vigilar una fuente) o ``research.discovery_tick`` (buscar fuentes
nuevas). Este servicio es la **unica** puerta por la que la API HTTP y la fachada MCP la
crean, leen y controlan; el scheduler solo la ejecuta.

Reglas de autoridad
===================
* El owner sale del principal autenticado, nunca del payload. Una suscripcion ajena
  responde **404**, no 403: no se revela que exista.
* Suscribirse no gasta ni toca la red: valida, persiste y despierta al timer. Cada tick
  vuelve a resolver la autoridad (``derive_effective_policy``), de modo que retirar el
  permiso, el proyecto o la politica la **pausa** con su motivo.
* La identidad de la suscripcion es ``(proyecto, owner, proposito, tipo, fuente
  normalizada, clave idempotente)``. La misma identidad con el mismo spec es idempotente;
  con otro spec es **409**: cambiar parametros es un ``update`` explicito del owner, y
  cambiar de fuente es cancelar y crear otra.
* Tope de suscripciones activas por owner y cadencia minima, para que programar trabajo
  recurrente no sea una via de gasto no aprobado.
* Cancelar detiene los ticks futuros y descarta los eventos ``pending``. Un job ya
  admitido sigue su curso (su gasto ya esta reservado y su resultado se consulta por su
  propio id); se cancela con la operacion de jobs.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from datetime import datetime
from typing import Any, Callable, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, field_validator

from research.contracts import ResearchJobRequest
from research.policies import (
    Principal,
    ResearchAccessDenied,
    ResearchPolicyError,
    derive_effective_policy,
)
from research.scheduler import (
    ResearchScheduleConflict,
    ResearchScheduleRequest,
    ResearchScheduleStatus,
    ResearchScheduleStore,
    ScheduleValidationError,
)
from research.watch import (
    WORKFLOW_DISCOVERY,
    WORKFLOW_WATCH,
    ItemFilter,
    SourceSpecError,
    digest,
    normalize_source,
    source_ref,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ResearchSubscriptions",
    "SubscriptionConflict",
    "SubscriptionError",
    "SubscriptionLimitExceeded",
    "SubscriptionNotFound",
    "SubscriptionRequest",
    "SubscriptionUpdate",
    "SubscriptionValidationError",
    "public_event",
    "public_subscription",
]

SUBSCRIPTION_WORKFLOWS = (WORKFLOW_WATCH, WORKFLOW_DISCOVERY)
MAX_QUESTION_LENGTH = 1000
MAX_PURPOSE_LENGTH = 120
MAX_SEARCHES_PER_DAY = 20
MAX_RESULT_COUNT = 20
KIND_BY_WORKFLOW = {WORKFLOW_WATCH: "watch", WORKFLOW_DISCOVERY: "discovery"}

SubscriptionKind = Literal["watch", "discovery"]


class SubscriptionError(RuntimeError):
    """Base de los errores del servicio. La fachada los traduce a un estado HTTP."""


class SubscriptionValidationError(SubscriptionError):
    """422: el spec no es valido."""


class SubscriptionConflict(SubscriptionError):
    """409: identidad repetida con otro spec, o estado que no admite la operacion."""


class SubscriptionLimitExceeded(SubscriptionError):
    """429: el owner alcanzo su tope de suscripciones activas."""


class SubscriptionNotFound(SubscriptionError):
    """404: no existe o no pertenece al principal."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubscriptionRequest(_Strict):
    project_key: str
    purpose: str
    question: str
    kind: SubscriptionKind = "watch"
    source: dict[str, Any] | None = None
    filter: dict[str, Any] | None = None
    interval_seconds: int | None = None
    daily_at: str | None = None
    timezone: str = "UTC"
    # Por defecto la primera pasada es una linea base: lo que ya existe no se analiza.
    backfill: bool = False
    max_searches_per_day: int | None = None
    count: int | None = None
    expires_at: datetime | None = None
    idempotency_key: str | None = None

    @field_validator("project_key", "purpose", "question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("El campo no puede estar vacio")
        return value.strip()

    @field_validator("question")
    @classmethod
    def _question_length(cls, value: str) -> str:
        if len(value) > MAX_QUESTION_LENGTH:
            raise ValueError(f"question admite hasta {MAX_QUESTION_LENGTH} caracteres")
        return value

    @field_validator("purpose")
    @classmethod
    def _purpose_length(cls, value: str) -> str:
        if len(value) > MAX_PURPOSE_LENGTH:
            raise ValueError(f"purpose admite hasta {MAX_PURPOSE_LENGTH} caracteres")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _blank_key_is_absent(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class SubscriptionUpdate(_Strict):
    """Campos editables. Solo los presentes cambian; ``null`` limpia ``expires_at``."""

    question: str | None = None
    filter: dict[str, Any] | None = None
    interval_seconds: int | None = None
    daily_at: str | None = None
    timezone: str | None = None
    backfill: bool | None = None
    max_searches_per_day: int | None = None
    count: int | None = None
    expires_at: datetime | None = None


async def _resolve(factory: Callable[[str], Any], key: str) -> Any:
    value = factory(key)
    if inspect.isawaitable(value):
        value = await value
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def public_subscription(document: Mapping[str, Any]) -> dict[str, Any]:
    """Vista publica de una suscripcion. Lista blanca: sin lease, fencing ni dedupe_key."""
    params = document.get("params") or {}
    cursor = document.get("cursor") or {}
    view: dict[str, Any] = {
        "subscription_id": document.get("schedule_id"),
        "kind": KIND_BY_WORKFLOW.get(str(document.get("workflow")), "unknown"),
        "project_key": document.get("project_key"),
        "purpose": document.get("purpose"),
        "status": document.get("status"),
        "status_reason": document.get("status_reason"),
        "question": params.get("question"),
        "filter": params.get("filter") or {},
        "backfill": bool(params.get("backfill", False)),
        "cadence": {
            "interval_seconds": document.get("interval_seconds"),
            "daily_at": document.get("daily_at"),
            "timezone": document.get("timezone"),
        },
        "next_run_at": document.get("next_run_at"),
        "last_run_at": document.get("last_run_at"),
        "last_result": document.get("last_result"),
        "last_error": document.get("last_error"),
        "coverage": cursor.get("coverage"),
        "coverage_reason": cursor.get("coverage_reason"),
        "last_checked_at": cursor.get("last_checked_at"),
        "consecutive_failures": int(cursor.get("consecutive_failures") or 0),
        "expires_at": document.get("expires_at"),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
    }
    if params.get("source"):
        view["source"] = dict(params["source"])
    for key in ("max_searches_per_day", "count"):
        if key in params:
            view[key] = params[key]
    return _jsonable(view)


def public_event(document: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("event_key", "kind", "status", "title", "url", "published_at", "first_seen_at", "research_id")
    return _jsonable({key: document.get(key) for key in keys})


class ResearchSubscriptions:
    """Crea, lee y controla suscripciones. No ejecuta ticks ni toca la red."""

    def __init__(
        self,
        *,
        store: ResearchScheduleStore,
        repo: Any,
        settings: Any,
        resolve_principal: Callable[[str], Any] | None = None,
    ) -> None:
        self._store = store
        self._repo = repo
        self._settings = settings
        self._resolve_principal = resolve_principal

    # ------------------------------------------------------------ autoridad

    def _authorize(self, principal: Principal, project_key: str, purpose: str, question: str,
                   project_cfg: Mapping[str, Any]):
        request = ResearchJobRequest(project_key=project_key, purpose=purpose,
                                     question=question, mode="quick")
        return derive_effective_policy(principal=principal, request=request,
                                       project_cfg=project_cfg, settings=self._settings)

    async def principal_for_consumer(self, owner_id: str, projects: Iterable[str]) -> Principal:
        """Principal real del owner (su cuenta), acotado a los proyectos del grant.

        La fachada MCP no inventa permisos: una suscripcion que el tick pausaria por falta
        de acceso no debe poder crearse. Un administrador tampoco hereda ``is_admin``: el
        grant del consumidor es el techo.
        """
        if self._resolve_principal is None:
            raise ResearchAccessDenied("No hay resolutor de cuentas para el owner")
        base = await self._resolve_principal(owner_id)
        granted = frozenset(str(p) for p in projects)
        if "*" in granted:
            allowed = base.allowed_projects
            if base.is_admin:
                allowed = frozenset({"*"})
        elif base.is_admin or "*" in base.allowed_projects:
            allowed = granted
        else:
            allowed = base.allowed_projects & granted
        return Principal(user_id=base.user_id, permissions=base.permissions,
                         allowed_projects=frozenset(allowed), is_admin=False, groups=base.groups)

    @staticmethod
    def _owned(document: Mapping[str, Any] | None, principal: Principal) -> dict[str, Any]:
        """Una suscripcion ajena (o de otro tipo de schedule) no existe para el principal."""
        if (document is None or document.get("workflow") not in SUBSCRIPTION_WORKFLOWS
                or str(document.get("owner_id")) != str(principal.user_id)):
            raise SubscriptionNotFound("Suscripcion no encontrada")
        project = str(document.get("project_key") or "")
        if not (principal.is_admin or "*" in principal.allowed_projects or project in principal.allowed_projects):
            raise SubscriptionNotFound("Suscripcion no encontrada")
        return dict(document)

    # ------------------------------------------------------------ validacion

    def _min_interval(self, workflow: str) -> int:
        name = ("RESEARCH_DISCOVERY_MIN_INTERVAL_SECONDS" if workflow == WORKFLOW_DISCOVERY
                else "RESEARCH_SUBSCRIPTIONS_MIN_INTERVAL_SECONDS")
        return int(getattr(self._settings, name, 900) or 0)

    def _check_cadence(self, workflow: str, interval_seconds: int | None) -> None:
        minimum = self._min_interval(workflow)
        if interval_seconds is not None and interval_seconds < minimum:
            raise SubscriptionValidationError(
                f"interval_seconds debe ser >= {minimum} para suscripciones de "
                f"{KIND_BY_WORKFLOW[workflow]}"
            )

    @staticmethod
    def _positive(name: str, value: int | None, ceiling: int) -> int | None:
        if value is None:
            return None
        if not 1 <= int(value) <= ceiling:
            raise SubscriptionValidationError(f"{name} debe estar entre 1 y {ceiling}")
        return int(value)

    def _build_params(self, request: SubscriptionRequest) -> tuple[str, dict[str, Any], str]:
        """(workflow, params, referencia de identidad). Lanza ``SubscriptionValidationError``."""
        try:
            item_filter = ItemFilter.from_params(request.filter)
            if request.kind == "watch":
                if request.source is None:
                    raise SubscriptionValidationError("source es obligatorio para una suscripcion watch")
                if request.max_searches_per_day is not None or request.count is not None:
                    raise SubscriptionValidationError(
                        "max_searches_per_day y count solo aplican a discovery")
                source = normalize_source(request.source)
                params: dict[str, Any] = {"source": source, "question": request.question,
                                          "filter": item_filter.to_params(), "backfill": request.backfill}
                return WORKFLOW_WATCH, params, source_ref(source)
            if request.source is not None:
                raise SubscriptionValidationError("discovery no admite source: busca por pregunta")
            self._check_discovery_filter(item_filter)
            params = {"question": request.question, "filter": item_filter.to_params(),
                      "backfill": request.backfill}
            cap = self._positive("max_searches_per_day", request.max_searches_per_day, MAX_SEARCHES_PER_DAY)
            count = self._positive("count", request.count, MAX_RESULT_COUNT)
            if cap is not None:
                params["max_searches_per_day"] = cap
            if count is not None:
                params["count"] = count
            return WORKFLOW_DISCOVERY, params, digest(" ".join(request.question.casefold().split()), size=24)
        except SourceSpecError as exc:
            raise SubscriptionValidationError(str(exc)) from exc

    @staticmethod
    def _check_discovery_filter(item_filter: ItemFilter) -> None:
        # El buscador entrega solo URLs, sin titulo ni resumen: ``include_terms`` compararia
        # contra la URL y descartaria en silencio fuentes pertinentes.
        if item_filter.include:
            raise SubscriptionValidationError(
                "discovery solo admite exclude_terms: sus resultados son URLs sin titulo ni resumen"
            )

    async def _reopen_filtered(self, subscription_id: str, item_filter: ItemFilter) -> int:
        """Reevalua los eventos ``filtered`` con el filtro nuevo (por su titulo y URL).

        Ampliar un filtro no deberia dejar fuera lo que ya se habia visto. El resumen de un
        tercero no se persiste, asi que lo que solo coincidia por resumen no se recupera.
        """
        rows = await self._repo.list_research_watch_events(
            subscription_id, statuses=["filtered"], limit=500)
        keys = [row["event_key"] for row in rows
                if item_filter.matches(row.get("title"), row.get("url"))]
        if not keys:
            return 0
        return await self._repo.mark_research_watch_events(
            subscription_id, keys, "pending", only_from="filtered")

    async def _check_capacity(self, owner_id: str) -> None:
        cap = int(getattr(self._settings, "RESEARCH_SUBSCRIPTIONS_MAX_ACTIVE_PER_OWNER", 10) or 0)
        if cap <= 0:
            raise SubscriptionLimitExceeded("Las suscripciones estan deshabilitadas para este despliegue")
        active = await self._store.list(owner_id=owner_id, statuses=[ResearchScheduleStatus.ACTIVE.value],
                                        limit=cap + 1, workflows=list(SUBSCRIPTION_WORKFLOWS))
        if len(active) >= cap:
            raise SubscriptionLimitExceeded(f"Maximo de {cap} suscripciones activas por owner")

    # ------------------------------------------------------------ operaciones

    async def subscribe(
        self, request: SubscriptionRequest, *, principal: Principal,
        project_cfg: Mapping[str, Any], created_by_consumer: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Crea (o devuelve, si es idempotente) una suscripcion. No gasta ni toca la red."""
        policy = self._authorize(principal, request.project_key, request.purpose,
                                 request.question, project_cfg)
        workflow, params, ref = self._build_params(request)
        if workflow == WORKFLOW_DISCOVERY and not policy.adapter_enabled("brave"):
            # Gasto no aprobado: sin buscador habilitado para el proyecto no hay descubrimiento.
            raise ResearchPolicyError("El proyecto no habilita descubrimiento (adapters.brave)")
        self._check_cadence(workflow, request.interval_seconds)
        try:
            spec = ResearchScheduleRequest(
                workflow=workflow, params=params, timezone=request.timezone,
                interval_seconds=request.interval_seconds, daily_at=request.daily_at,
                expires_at=request.expires_at,
            )
        except (ValueError, ScheduleValidationError) as exc:
            # ``daily_at`` invalido sale como ScheduleValidationError, que no es ValueError.
            raise SubscriptionValidationError(str(exc)) from exc
        dedupe_key = digest(request.project_key, principal.user_id, request.purpose,
                            KIND_BY_WORKFLOW[workflow], ref, request.idempotency_key or "", size=48)
        if await self._repo.find_research_schedule_by_dedupe(dedupe_key) is None:
            await self._check_capacity(principal.user_id)
        extra: dict[str, Any] = {"kind": KIND_BY_WORKFLOW[workflow]}
        if created_by_consumer:
            extra["created_by_consumer"] = created_by_consumer
        try:
            document, created = await self._store.create(
                project_key=request.project_key, owner_id=principal.user_id, purpose=request.purpose,
                spec=spec, dedupe_key=dedupe_key, extra=extra,
            )
        except ResearchScheduleConflict as exc:
            raise SubscriptionConflict(
                "La suscripcion ya existe con otros parametros. Edita la existente, o usa otra "
                "idempotency_key para una distinta"
            ) from exc
        except ScheduleValidationError as exc:
            raise SubscriptionValidationError(str(exc)) from exc
        return public_subscription(document), created

    async def get(self, subscription_id: str, *, principal: Principal) -> dict[str, Any]:
        document = self._owned(await self._store.get(subscription_id), principal)
        return public_subscription(document)

    async def list(self, *, principal: Principal, project_key: str | None = None,
                   statuses: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._store.list(
            project_key=project_key, owner_id=principal.user_id, statuses=statuses,
            limit=max(1, min(int(limit), 100)), workflows=list(SUBSCRIPTION_WORKFLOWS),
        )
        visible: list[dict[str, Any]] = []
        for row in rows:
            try:
                visible.append(public_subscription(self._owned(row, principal)))
            except SubscriptionNotFound:
                continue  # proyecto retirado del principal: no se lista
        return visible

    async def events(self, subscription_id: str, *, principal: Principal,
                     statuses: list[str] | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self._owned(await self._store.get(subscription_id), principal)
        rows = await self._repo.list_research_watch_events(
            subscription_id, statuses=statuses, limit=max(1, min(int(limit), 200)))
        return [public_event(row) for row in rows]

    async def update(
        self, subscription_id: str, update: SubscriptionUpdate, *, principal: Principal,
        project_cfg_for: Callable[[str], Any],
    ) -> dict[str, Any]:
        """Edita el spec de una suscripcion propia. La identidad (fuente, proposito) no cambia."""
        document = self._owned(await self._store.get(subscription_id), principal)
        if ResearchScheduleStatus(document["status"]).is_terminal:
            raise SubscriptionConflict("Una suscripcion cancelada o expirada no se edita")
        fields = update.model_fields_set
        workflow = str(document["workflow"])
        params = dict(document.get("params") or {})
        changes: dict[str, Any] = {}
        try:
            if "question" in fields:
                if not (update.question or "").strip() or len(update.question or "") > MAX_QUESTION_LENGTH:
                    raise SubscriptionValidationError("question vacia o demasiado larga")
                params["question"] = update.question.strip()  # type: ignore[union-attr]
            if "filter" in fields:
                new_filter = ItemFilter.from_params(update.filter or {})
                if workflow == WORKFLOW_DISCOVERY:
                    self._check_discovery_filter(new_filter)
                params["filter"] = new_filter.to_params()
            if "backfill" in fields and update.backfill is not None:
                params["backfill"] = update.backfill
            if workflow == WORKFLOW_DISCOVERY:
                for name, ceiling in (("max_searches_per_day", MAX_SEARCHES_PER_DAY), ("count", MAX_RESULT_COUNT)):
                    if name in fields:
                        value = self._positive(name, getattr(update, name), ceiling)
                        if value is None:
                            params.pop(name, None)
                        else:
                            params[name] = value
            elif {"max_searches_per_day", "count"} & fields:
                raise SubscriptionValidationError("max_searches_per_day y count solo aplican a discovery")
        except SourceSpecError as exc:
            raise SubscriptionValidationError(str(exc)) from exc
        if params != (document.get("params") or {}):
            changes["params"] = params
        for name in ("interval_seconds", "daily_at", "timezone"):
            if name in fields and getattr(update, name) is not None:
                changes[name] = getattr(update, name)
        if "expires_at" in fields:
            changes["expires_at"] = update.expires_at
        if not changes:
            return public_subscription(document)
        resulting_interval = changes.get("interval_seconds", None if "daily_at" in changes
                                         else document.get("interval_seconds"))
        self._check_cadence(workflow, resulting_interval)
        self._authorize(principal, document["project_key"], document["purpose"],
                        params.get("question") or "pregunta",
                        await _resolve(project_cfg_for, document["project_key"]))
        try:
            stored = await self._store.update(subscription_id, changes)
        except ScheduleValidationError as exc:
            raise SubscriptionValidationError(str(exc)) from exc
        if stored is None:
            raise SubscriptionNotFound("Suscripcion no encontrada")
        view = public_subscription(stored)
        if workflow == WORKFLOW_WATCH and "filter" in fields:
            view["reopened_events"] = await self._reopen_filtered(
                subscription_id, ItemFilter.from_params(params["filter"]))
        return view

    async def pause(self, subscription_id: str, *, principal: Principal) -> dict[str, Any]:
        document = self._owned(await self._store.get(subscription_id), principal)
        if ResearchScheduleStatus(document["status"]).is_terminal:
            raise SubscriptionConflict("Una suscripcion cancelada o expirada no se pausa")
        stored = await self._store.pause(subscription_id)
        if stored is None:  # se volvio terminal entre la lectura y la escritura
            raise SubscriptionConflict("La suscripcion ya no admite la operacion")
        return public_subscription(stored)

    async def resume(self, subscription_id: str, *, principal: Principal,
                     project_cfg_for: Callable[[str], Any]) -> dict[str, Any]:
        """Reanuda. Vuelve a exigir autoridad y cupo: no se reactiva lo que ya no se podria crear."""
        document = self._owned(await self._store.get(subscription_id), principal)
        status = ResearchScheduleStatus(document["status"])
        if status.is_terminal:
            raise SubscriptionConflict("Una suscripcion cancelada o expirada no se reanuda")
        if status is ResearchScheduleStatus.ACTIVE:
            return public_subscription(document)
        self._authorize(principal, document["project_key"], document["purpose"],
                        (document.get("params") or {}).get("question") or "pregunta",
                        await _resolve(project_cfg_for, document["project_key"]))
        await self._check_capacity(principal.user_id)
        stored = await self._store.resume(subscription_id)
        if stored is None:
            raise SubscriptionConflict("La suscripcion ya no admite la operacion")
        return public_subscription(stored)

    async def cancel(self, subscription_id: str, *, principal: Principal) -> dict[str, Any]:
        document = self._owned(await self._store.get(subscription_id), principal)
        if document["status"] == ResearchScheduleStatus.CANCELLED.value:
            return public_subscription(document)
        stored = await self._store.cancel(subscription_id)
        # Lo no admitido se descarta; un job ya admitido termina por su cuenta.
        await self._repo.drop_pending_research_watch_events(subscription_id)
        if stored is None:  # ya era terminal (p. ej. expiro): se devuelve su estado real
            stored = await self._store.get(subscription_id) or document
        return public_subscription(stored)

    async def pause_for_consumer(self, consumer_id: str, reason: str = "grant_revoked") -> int:
        """Pausa lo que un consumidor programo cuando se revoca su credencial."""
        rows = await self._store.list(statuses=[ResearchScheduleStatus.ACTIVE.value], limit=500,
                                      workflows=list(SUBSCRIPTION_WORKFLOWS),
                                      created_by_consumer=consumer_id)
        for row in rows:
            await self._store.pause(str(row["schedule_id"]), reason=reason)
        return len(rows)
