"""Scheduler persistido de research: reactivo, sin barrido (TASK-2026-00068).

Por que no reutiliza ``HorizonScheduler`` tal cual
==================================================
Se evaluo extraer su kernel, y se descarto **copiarlo sin mas** por un motivo medido:
su ``_loop`` acota cada espera con ``DEFAULT_MAX_SLEEP_SECONDS = 3600`` y vuelve a
consultar aunque no haya trabajo. Es una red de seguridad razonable para Horizon, pero
para research es un barrido horario: AGENTS.md §5.1 lo prohibe y el plan del epico lo
señala expresamente ("Research debe esperar evento indefinidamente si no hay
vencimiento"). Aqui el timer **duerme sin tope** cuando no hay vencimiento y solo
despierta por evento (CRUD) o por el vencimiento exacto.

Lo que si se conserva del patron es la **forma** del contrato, no una copia ciega:
un documento por trabajo con ``next_run_at``, toma atomica con lease, un solo timer por
despliegue y recuperacion acotada al arranque. La semantica de estados es propia de
schedules (``ScheduleStatus``), no se reutiliza ``ResearchJobStatus``.

Reglas que implementa
=====================
* **Persistir la intencion y luego despertar.** El CRUD escribe el schedule y avisa al
  timer; el timer no se entera de nada por si solo.
* **Toma atomica por (schedule, vencimiento).** ``find_one_and_update`` con
  ``run_state: idle -> running`` y lease; el ``fencing_token`` impide que un ejecutor
  viejo cierre una toma que ya no es suya.
* **Catch-up unico.** Tras un reinicio solo se recuperan leases **vencidas**, y un
  schedule atrasado produce **una** ejecucion util, no una rafaga por cada intervalo
  perdido; el resto se declara (skip/merge) y el siguiente vencimiento queda futuro.
* **Redelivery != exactly-once.** Una reentrega tras un crash no se afirma como
  exactamente-una-vez: ``pending_reconcile`` marca que el handler debe reconciliar
  antes de repetir gasto.
* **Sin TTL que borre schedules activos.** La expiracion es un estado explicito.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from contextlib import suppress
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BACKOFF_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "STALE_RUNNING_SECONDS",
    "CATCHUP_MISSED",
    "OverlapPolicy",
    "ResearchScheduleConflict",
    "ResearchScheduleError",
    "ResearchScheduleRequest",
    "ResearchScheduleStatus",
    "ResearchScheduleStore",
    "ResearchScheduler",
    "ScheduleValidationError",
    "backoff_seconds",
    "build_schedule_document",
    "compute_next_run_at",
    "execution_key_for",
    "next_occurrence_after",
    "parse_daily_at",
]

# Margen de un `running` huerfano: un lease que vencio pertenece a un proceso que ya no
# existe. No es un barrido; es la recuperacion de arranque.
STALE_RUNNING_SECONDS = 900

DEFAULT_LEASE_SECONDS = 120
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600
DEFAULT_BATCH_LIMIT = 20

# Un intervalo mas corto que un minuto no es una suscripcion, es un bucle.
MIN_INTERVAL_SECONDS = 60

# Razon de catch-up: la ocurrencia que se ejecuta cubre las perdidas por un reinicio.
CATCHUP_MISSED = "catchup_after_restart"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


class ResearchScheduleError(RuntimeError):
    """Fallo del scheduler que no es una ocurrencia fallida."""


class ScheduleValidationError(ResearchScheduleError):
    """El spec del schedule no es valido (se traduce a 422 en la fachada)."""


class ResearchScheduleConflict(ResearchScheduleError):
    """La identidad del schedule ya existe con un spec incompatible (409)."""


class ResearchScheduleStatus(str, Enum):
    """Ciclo de vida del schedule. Es propio: no se usa ``ResearchJobStatus``."""

    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {ResearchScheduleStatus.CANCELLED, ResearchScheduleStatus.EXPIRED}


class OverlapPolicy(str, Enum):
    """Que hacer cuando el ejecutor sigue ocupado y llega otro vencimiento."""

    SKIP = "skip"
    MERGE = "merge"


# Estados de la ocurrencia en curso: viven en el mismo documento porque la toma es
# atomica sobre un unico documento (no una transaccion multi-documento).
RUN_IDLE = "idle"
RUN_RUNNING = "running"


def parse_daily_at(value: str) -> tuple[int, int]:
    """Interpreta ``HH:MM``. Un valor invalido **no** cae a un respaldo silencioso."""
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScheduleValidationError(f"daily_at invalido: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleValidationError(f"daily_at fuera de rango: {value!r}")
    return hour, minute


def _wall_time_on(zone: ZoneInfo, day: date, hour: int, minute: int) -> datetime:
    """Instante local de una hora de pared, resolviendo el DST explicitamente.

    * Hora **inexistente** (salto hacia adelante): se avanza el hueco real del salto,
      que es la hora valida mas cercana; no se devuelve una hora que no existe.
    * Hora **repetida** (salto hacia atras): ``fold=0`` elige la **primera** pasada.
      La eleccion es determinista y esta documentada; no se deja al azar del sistema.
    """
    naive = datetime.combine(day, time(hour, minute))
    candidate = naive.replace(tzinfo=zone, fold=0)
    represented = candidate.astimezone(zone).replace(tzinfo=None)
    if represented != naive:
        # El hueco del salto: p. ej. 02:30 con +1h se representa como 03:30.
        gap = represented - naive
        naive = naive + gap
        candidate = naive.replace(tzinfo=zone, fold=0)
    return candidate


def next_occurrence_after(schedule: Mapping[str, Any], after: datetime) -> datetime:
    """Primer vencimiento **estrictamente posterior** a ``after`` (UTC).

    ``after`` es el instante de referencia (normalmente "ahora"); el resultado se
    calcula en la zona del schedule y se devuelve en UTC.
    """
    moment = _aware(after)
    zone = _zone(str(schedule.get("timezone") or "UTC"))
    interval = schedule.get("interval_seconds")
    if interval:
        anchor = _aware(
            schedule.get("anchor_at") or schedule.get("created_at") or moment
        )
        step = max(1, int(interval))
        elapsed = (moment - anchor).total_seconds()
        # Se cuenta desde el ancla, no desde el ultimo vencimiento: asi el intervalo no
        # acumula deriva y "cada 30 minutos" sigue siendo :00 y :30.
        periods = math.floor(elapsed / step) + 1
        return anchor + timedelta(seconds=periods * step)
    daily_at = schedule.get("daily_at")
    if daily_at:
        hour, minute = parse_daily_at(str(daily_at))
        local_now = moment.astimezone(zone)
        for day_offset in range(0, 3):
            day = local_now.date() + timedelta(days=day_offset)
            candidate = _wall_time_on(zone, day, hour, minute)
            if candidate.astimezone(timezone.utc) > moment:
                return candidate.astimezone(timezone.utc)
        # Inalcanzable con 3 dias de margen, pero nunca se devuelve una fecha pasada.
        return (local_now + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        ).astimezone(timezone.utc)
    raise ScheduleValidationError("El schedule no tiene interval_seconds ni daily_at")


def compute_next_run_at(schedule: Mapping[str, Any], after: datetime) -> datetime:
    return next_occurrence_after(schedule, after)


def backoff_seconds(attempts: int, *, base: int = DEFAULT_BACKOFF_SECONDS) -> int:
    """Espera creciente y acotada: 60, 120, 240... hasta 1 hora."""
    exponent = max(0, int(attempts) - 1)
    return min(base * (2**exponent), MAX_BACKOFF_SECONDS)


def execution_key_for(schedule: Mapping[str, Any]) -> str:
    """Clave estable de la ocurrencia: (schedule, vencimiento ejecutado).

    Estable entre redeliveries y a prueba de crash: la misma ocurrencia produce la
    misma clave, de modo que un consumidor puede deduplicar sus eventos. No promete
    exactly-once de la red; solo una identidad reproducible.
    """
    scheduled_for = _aware(
        schedule.get("scheduled_for") or schedule.get("next_run_at") or _utcnow()
    )
    stamp = scheduled_for.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{schedule.get('schedule_id')}:{stamp}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchScheduleRequest(_StrictModel):
    """Spec validado de un schedule. La identidad (owner) la deriva el servidor.

    Exactamente uno de ``interval_seconds`` o ``daily_at`` define la cadencia. Un
    intervalo vacio **no** se interpreta como 0 ni como uno por defecto: es un spec
    invalido.
    """

    workflow: str
    params: dict[str, Any] = {}
    timezone: str = "UTC"
    interval_seconds: int | None = None
    daily_at: str | None = None
    overlap_policy: OverlapPolicy = OverlapPolicy.SKIP
    priority: int = 0
    deadline_seconds: int | None = None
    max_attempts: int | None = None
    expires_at: datetime | None = None

    @field_validator("workflow")
    @classmethod
    def _workflow_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("workflow no puede estar vacio")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:  # noqa: BLE001 - ZoneInfoError y KeyError segun version
            raise ValueError(f"timezone IANA invalida: {value!r}") from exc
        return value

    @field_validator("interval_seconds")
    @classmethod
    def _interval_min(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value < MIN_INTERVAL_SECONDS:
            raise ValueError(f"interval_seconds debe ser >= {MIN_INTERVAL_SECONDS}")
        return value

    @field_validator("daily_at")
    @classmethod
    def _daily_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parse_daily_at(value)
        return value

    @field_validator("deadline_seconds", "max_attempts")
    @classmethod
    def _positive(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("El valor debe ser positivo")
        return value

    @model_validator(mode="after")
    def _exactly_one_cadence(self) -> "ResearchScheduleRequest":
        has_interval = self.interval_seconds is not None
        has_daily = self.daily_at is not None
        if has_interval == has_daily:
            raise ValueError("Define exactamente uno: interval_seconds o daily_at")
        return self


def build_schedule_document(
    *,
    project_key: str,
    owner_id: str,
    purpose: str,
    spec: ResearchScheduleRequest | Mapping[str, Any],
    dedupe_key: str | None,
    settings: Any = None,
    now: datetime | None = None,
    schedule_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Documento persistible de un schedule, con la intencion ya validada.

    ``extra`` agrega metadatos de origen (p. ej. el consumidor que lo creo). Nunca pisa un
    campo del schedule: el estado y la identidad los fija esta funcion.
    """
    request = spec if isinstance(spec, ResearchScheduleRequest) else ResearchScheduleRequest(**spec)
    moment = _aware(now or _utcnow())
    deadline = request.deadline_seconds or int(
        getattr(settings, "RESEARCH_SCHEDULER_DEADLINE_SECONDS", 300) or 300
    )
    max_attempts = request.max_attempts or int(
        getattr(settings, "RESEARCH_SCHEDULER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
        or DEFAULT_MAX_ATTEMPTS
    )
    document: dict[str, Any] = {
        "schedule_id": schedule_id or f"sch_{uuid4().hex}",
        "project_key": project_key,
        "owner_id": owner_id,
        "purpose": purpose,
        "workflow": request.workflow,
        "params": dict(request.params),
        "timezone": request.timezone,
        "interval_seconds": request.interval_seconds,
        "daily_at": request.daily_at,
        "overlap_policy": request.overlap_policy.value,
        "priority": int(request.priority),
        "deadline_seconds": deadline,
        "max_attempts": max_attempts,
        "attempts": 0,
        "status": ResearchScheduleStatus.ACTIVE.value,
        "run_state": RUN_IDLE,
        "lease_owner": None,
        "lease_expires_at": None,
        "fencing_token": 0,
        "execution_key": None,
        "scheduled_for": None,
        "last_run_at": None,
        "last_result": None,
        "last_error": None,
        "skipped_runs": 0,
        "merged_runs": 0,
        "cursor": {},
        "pending_reconcile": False,
        "dedupe_key": dedupe_key or None,
        "anchor_at": moment,
        "expires_at": _aware(request.expires_at) if request.expires_at else None,
        "created_at": moment,
        "updated_at": moment,
    }
    for key, value in (extra or {}).items():
        document.setdefault(key, value)
    document["next_run_at"] = next_occurrence_after(document, moment)
    return document


# Campos del spec que se pueden editar despues de crear el schedule. Identidad
# (proyecto, owner, proposito, workflow), estado, lease y cursor quedan fuera a proposito.
_CADENCE_FIELDS = frozenset({"interval_seconds", "daily_at", "timezone"})
_EDITABLE_FIELDS = _CADENCE_FIELDS | frozenset(
    {"params", "overlap_policy", "priority", "deadline_seconds", "max_attempts", "expires_at"}
)


def schedule_fingerprint(document: Mapping[str, Any]) -> str:
    """Hash canonico del spec que define la identidad de un schedule."""
    payload = {
        "workflow": document.get("workflow"),
        "params": document.get("params") or {},
        "timezone": document.get("timezone"),
        "interval_seconds": document.get("interval_seconds"),
        "daily_at": document.get("daily_at"),
    }
    # JSON con claves ordenadas: ``repr`` de un dict anidado depende del orden de
    # insercion, asi que los mismos params en otro orden daban un 409 falso.
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResearchScheduleStore:
    """Acceso a los schedules persistidos y aviso al timer tras cada cambio.

    El aviso es **en proceso**: el contrato del epico exige una sola autoridad de
    timer/coordinator por despliegue, y ``asyncio.Event`` no cruza procesos. Un
    despliegue multi-proceso necesitaria notificar por IPC o por un change stream
    soportado; eso no esta implementado aqui y se declara, en vez de fingir que un
    evento local despierta a otro proceso.
    """

    def __init__(self, repo: Any, *, settings: Any = None) -> None:
        self._repo = repo
        self._settings = settings
        self._waker: Callable[[], None] | None = None

    def bind_waker(self, waker: Callable[[], None] | None) -> None:
        self._waker = waker

    def notify(self) -> None:
        if self._waker is not None:
            self._waker()

    async def create(
        self,
        *,
        project_key: str,
        owner_id: str,
        purpose: str,
        spec: ResearchScheduleRequest | Mapping[str, Any],
        dedupe_key: str | None = None,
        now: datetime | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persiste un schedule y despierta al timer. Idempotente por ``dedupe_key``."""
        document = build_schedule_document(
            project_key=project_key,
            owner_id=owner_id,
            purpose=purpose,
            spec=spec,
            dedupe_key=dedupe_key,
            settings=self._settings,
            now=now,
            extra=extra,
        )
        stored, created = await self._repo.create_research_schedule(document)
        if not created:
            existing_fp = schedule_fingerprint(stored)
            if existing_fp != schedule_fingerprint(document):
                raise ResearchScheduleConflict(
                    "El schedule ya existe con un spec distinto"
                )
            return stored, False
        self.notify()
        return stored, True

    async def get(self, schedule_id: str) -> dict[str, Any] | None:
        return await self._repo.get_research_schedule(schedule_id)

    async def list(
        self,
        *,
        project_key: str | None = None,
        owner_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
        workflows: list[str] | None = None,
        created_by_consumer: str | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        if workflows:
            kwargs["workflows"] = workflows
        if created_by_consumer is not None:
            kwargs["created_by_consumer"] = created_by_consumer
        return await self._repo.list_research_schedules(
            project_key=project_key, owner_id=owner_id, statuses=statuses, limit=limit, **kwargs
        )

    async def update(
        self, schedule_id: str, updates: Mapping[str, Any], *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """Edita el spec. Cambiar la cadencia recalcula (y **persiste**) el vencimiento.

        Solo se editan campos del spec: identidad, estado, lease y cursor no pasan por
        aqui. El resultado se valida completo con ``ResearchScheduleRequest``, de modo
        que un cambio no puede dejar un schedule con dos cadencias o con ninguna.
        """
        current = await self._repo.get_research_schedule(schedule_id)
        if current is None:
            return None
        payload = dict(updates)
        unknown = set(payload) - _EDITABLE_FIELDS
        if unknown:
            raise ScheduleValidationError(f"Campos no editables: {sorted(unknown)}")
        # Fijar una cadencia reemplaza a la otra: ``interval_seconds`` XOR ``daily_at``.
        if payload.get("interval_seconds") is not None and "daily_at" not in payload:
            payload["daily_at"] = None
        if payload.get("daily_at") is not None and "interval_seconds" not in payload:
            payload["interval_seconds"] = None
        merged = dict(current)
        merged.update(payload)
        try:
            ResearchScheduleRequest(
                **{key: merged.get(key) for key in _EDITABLE_FIELDS | {"workflow"} if key in merged}
            )
        except ValueError as exc:
            raise ScheduleValidationError(str(exc)) from exc
        moment = _aware(now or _utcnow())
        if any(key in payload for key in _CADENCE_FIELDS):
            # La cadencia nueva manda: el ancla y el proximo vencimiento se calculan
            # desde ahora, no desde el calendario anterior. Antes el valor se calculaba
            # y se descartaba: el cambio no tenia efecto hasta el siguiente vencimiento.
            merged["anchor_at"] = moment
            payload["anchor_at"] = moment
            payload["next_run_at"] = next_occurrence_after(merged, moment)
        result = await self._repo.patch_research_schedule(schedule_id, payload, now=now)
        self.notify()
        return result

    async def pause(
        self, schedule_id: str, *, now: datetime | None = None, reason: str | None = None
    ) -> dict[str, Any] | None:
        return await self._set_status(
            schedule_id, ResearchScheduleStatus.PAUSED, now=now, reason=reason
        )

    async def resume(self, schedule_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
        return await self._set_status(schedule_id, ResearchScheduleStatus.ACTIVE, now=now)

    async def cancel(
        self, schedule_id: str, *, now: datetime | None = None, reason: str | None = None
    ) -> dict[str, Any] | None:
        return await self._set_status(
            schedule_id, ResearchScheduleStatus.CANCELLED, now=now, reason=reason
        )

    async def _set_status(
        self,
        schedule_id: str,
        status: ResearchScheduleStatus,
        *,
        now: datetime | None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {"now": now}
        if reason is not None:
            kwargs["reason"] = reason
        result = await self._repo.set_research_schedule_status(schedule_id, status.value, **kwargs)
        self.notify()
        return result

    async def record_cursor(
        self,
        schedule_id: str,
        cursor: Mapping[str, Any],
        *,
        now: datetime | None = None,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any] | None:
        """Avanza el cursor incremental **solo** tras persistir los items (00069).

        Con ``lease_owner``/``fencing_token`` la escritura solo prospera si la toma sigue
        siendo la de este ejecutor: un tick viejo no puede mover el cursor de otro.
        """
        return await self._repo.patch_research_schedule(
            schedule_id,
            {"cursor": dict(cursor)},
            lease_owner=lease_owner,
            fencing_token=fencing_token,
            now=now,
        )


class ResearchScheduler:
    """El unico timer de research por despliegue. Duerme hasta el vencimiento exacto.

    Sin vencimiento **no sondea**: espera a que lo despierten (CRUD o parada). Con
    vencimiento espera la fecha concreta. ``max_sleep_seconds`` por defecto es ``None``
    (sin tope); fijarlo solo tiene sentido como red de seguridad explicita, nunca para
    reproducir el barrido horario que el epico prohibe.
    """

    def __init__(
        self,
        *,
        repo: Any,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        store: ResearchScheduleStore | None = None,
        settings: Any = None,
        coordinator_id: str = "research-scheduler-1",
        now_fn: Callable[[], datetime] | None = None,
        waiter: Callable[[asyncio.Event, float | None], Awaitable[bool]] | None = None,
        max_sleep_seconds: int | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        backoff_base: int = DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self._repo = repo
        self._handler = handler
        self._store = store
        self._settings = settings
        self._coordinator = coordinator_id
        self._now = now_fn or _utcnow
        self._waiter = waiter or _default_wait
        self._max_sleep = max_sleep_seconds
        self._lease_seconds = max(1, int(lease_seconds))
        self._batch_limit = max(1, int(batch_limit))
        self._backoff_base = max(0, int(backoff_base))
        self._job: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        if store is not None:
            store.bind_waker(self.wake)

    # ------------------------------------------------------------ ciclo de vida

    async def start(self) -> None:
        if self._job is not None and not self._job.done():
            return
        self._stop.clear()
        self._wake.clear()
        await self._recover()
        self._job = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None

    def wake(self) -> None:
        """Avisa al timer de que hay trabajo nuevo (CRUD o parada)."""
        self._wake.set()

    async def _recover(self) -> None:
        """Recuperacion de arranque: **solo** leases vencidas y **un** catch-up util."""
        cutoff = self._now() - timedelta(seconds=STALE_RUNNING_SECONDS)
        with suppress(Exception):
            await self._repo.recover_stale_research_schedules(cutoff, now=self._now())
        with suppress(Exception):
            await self._repo.expire_research_schedules(self._now())

    # ------------------------------------------------------------ bucle

    async def _loop(self) -> None:
        while not self._stop.is_set():
            # Limpiar ANTES de consultar: un wake que llegue durante la consulta o el
            # procesado no se pierde. Es el unico orden seguro.
            self._wake.clear()
            if await self._drain_due():
                continue
            delay = await self._seconds_until_next()
            timeout = None if delay is None else max(0.0, delay)
            if timeout is not None and self._max_sleep is not None:
                timeout = min(timeout, float(max(1, int(self._max_sleep))))
            try:
                await self._waiter(self._wake, timeout)
            except asyncio.CancelledError:
                raise

    async def _drain_due(self) -> bool:
        processed = 0
        for _ in range(self._batch_limit):
            if self._stop.is_set():
                break
            claimed = await self._repo.claim_next_research_schedule(
                lease_owner=self._coordinator,
                lease_seconds=self._lease_seconds,
                now=self._now(),
            )
            if claimed is None:
                break
            processed += 1
            await self._process(claimed)
        return processed > 0

    async def _seconds_until_next(self) -> float | None:
        try:
            next_at = await self._repo.next_research_schedule_due_at()
        except Exception:
            logger.warning("research: no se pudo calcular el proximo vencimiento", exc_info=True)
            return None
        if next_at is None:
            return None
        return (_aware(next_at) - self._now()).total_seconds()

    # ------------------------------------------------------------ ejecucion

    async def _process(self, claimed: dict[str, Any]) -> None:
        schedule_id = str(claimed.get("schedule_id") or "")
        if not schedule_id:
            return
        execution_key = execution_key_for(claimed)
        try:
            result = await self._handler(claimed)
        except Exception as exc:  # noqa: BLE001 - un fallo no puede tumbar el timer
            logger.warning("research: fallo el schedule %s", schedule_id, exc_info=True)
            await self._retry(claimed, error=str(exc))
            return
        await self._complete(claimed, result or {}, execution_key=execution_key)

    async def _complete(
        self, claimed: dict[str, Any], result: dict[str, Any], *, execution_key: str
    ) -> None:
        moment = self._now()
        next_run_at = compute_next_run_at(claimed, moment)
        skipped, merged = _overlap_counters(claimed, moment)
        updates: dict[str, Any] = {
            "run_state": RUN_IDLE,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_run_at": moment,
            "last_result": result,
            "last_error": None,
            "execution_key": execution_key,
            "scheduled_for": None,
            "attempts": 0,
            "pending_reconcile": False,
            "next_run_at": next_run_at,
        }
        if skipped:
            updates["skipped_runs"] = int(claimed.get("skipped_runs") or 0) + skipped
        if merged:
            updates["merged_runs"] = int(claimed.get("merged_runs") or 0) + merged
        await self._repo.patch_research_schedule(
            str(claimed["schedule_id"]),
            updates,
            lease_owner=self._coordinator,
            fencing_token=claimed.get("fencing_token"),
            now=moment,
        )

    async def _retry(self, claimed: dict[str, Any], *, error: str) -> None:
        """Reintento acotado con backoff. Agotados los intentos, retoma su cadencia."""
        schedule_id = str(claimed.get("schedule_id") or "")
        attempts = int(claimed.get("attempts") or 0) + 1
        max_attempts = int(claimed.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)
        moment = self._now()
        updates: dict[str, Any] = {
            "run_state": RUN_IDLE,
            "lease_owner": None,
            "lease_expires_at": None,
            "attempts": attempts,
            "last_error": error,
            "last_run_at": moment,
        }
        if attempts >= max_attempts:
            # No se reintenta la misma ocurrencia para siempre: se declara y se vuelve a
            # la cadencia normal. La ocurrencia perdida queda en el historial, no en un
            # bucle de gasto.
            updates["attempts"] = 0
            updates["scheduled_for"] = None
            updates["pending_reconcile"] = False
            updates["next_run_at"] = compute_next_run_at(claimed, moment)
        else:
            updates["next_run_at"] = moment + timedelta(
                seconds=backoff_seconds(attempts, base=self._backoff_base)
            )
        await self._repo.patch_research_schedule(
            schedule_id,
            updates,
            lease_owner=self._coordinator,
            fencing_token=claimed.get("fencing_token"),
            now=moment,
        )


def _overlap_counters(claimed: Mapping[str, Any], now: datetime) -> tuple[int, int]:
    """Cuenta las ocurrencias que la ejecucion actual cubre (skip/merge).

    Con la toma por documento unico no puede haber dos ejecuciones simultaneas del
    mismo schedule; lo que si puede pasar es que el ejecutor tarde mas que el intervalo
    y el vencimiento siguiente ya haya pasado. La politica decide como se declara:
    ``skip`` las descarta, ``merge`` las contabiliza como cubiertas por esta corrida.
    """
    scheduled_for = claimed.get("scheduled_for") or claimed.get("next_run_at")
    interval = claimed.get("interval_seconds")
    if not scheduled_for or not interval:
        return 0, 0
    elapsed = (now - _aware(scheduled_for)).total_seconds()
    missed = int(elapsed // int(interval))
    if missed <= 0:
        return 0, 0
    if str(claimed.get("overlap_policy") or OverlapPolicy.SKIP.value) == OverlapPolicy.MERGE.value:
        return 0, missed
    return missed, 0


async def _default_wait(event: asyncio.Event, timeout: float | None) -> bool:
    """Espera al evento; con ``timeout is None`` espera **indefinidamente**.

    No hay tope por defecto: un timer sin vencimiento no debe resucitar cada hora. El
    valor devuelto dice si desperto por evento (``True``) o por vencimiento (``False``).
    """
    if timeout is None:
        await event.wait()
        return True
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False
