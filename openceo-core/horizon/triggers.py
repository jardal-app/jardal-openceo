"""Trabajo intencional persistido y el unico timer que despierta a Horizon.

Por que esto no reutilizo ``WaitingTaskScheduler`` (retirado en
TASK-2026-00113; el despacho de tickets vive ahora en
``openceo-core/dispatcher.py``, con este mismo patron reactivo):

1. **Ese scheduler hacia un barrido.** Su bucle era
   ``resume_waiting_tasks(); resume_queued_tickets(); sleep(60)`` — dos consultas
   sin alcance cada minuto, para siempre. Las reglas permanentes de AGENTS.md lo
   prohiben y la
   seccion 9.2 punto 4 lo prohibe expresamente para esta capacidad.
2. **Su trabajo es derivable; el nuestro no.** Las tareas en espera se pueden
   recalcular desde el estado de las tareas, asi que un reinicio no pierde nada.
   El plan diario no se deriva de nada: si no esta persistido, se perdio. Por eso
   Horizon guarda la intencion (``horizon_triggers``), no solo el estado.

El contrato es: un documento por trabajo pendiente con ``next_run_at``, y **un
solo timer** que calcula el proximo vencimiento y duerme exactamente hasta el.
No hay sondeo. Cuando el vencimiento no existe, el timer espera a que lo
despierten; cuando existe, espera el tiempo justo.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from horizon.models import HorizonTrigger, TriggerKind, TriggerStatus, document_dump

logger = logging.getLogger(__name__)

# Reintentos acotados (seccion 9.2 punto 4: "reintentos acotados").
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 3600

# Un disparador que quedo "running" porque el proceso murio se recupera tras
# este margen. Sin esto, un reinicio a mitad de ejecucion dejaria el trabajo
# colgado para siempre.
STALE_RUNNING_SECONDS = 900

# Tope de espera del timer. No es sondeo: es la red de seguridad para un salto de
# reloj o una maquina suspendida. Con vencimientos normales el timer nunca llega
# a agotarlo.
DEFAULT_MAX_SLEEP_SECONDS = 3600

# Vencimientos por tipo, en horas. Es la regla de "descarta avisos obsoletos"
# de la seccion 9.7 convertida en codigo: un plan de anteayer ya no sirve.
EXPIRY_HOURS: dict[str, int] = {
    TriggerKind.DAILY_PLAN.value: 18,
    TriggerKind.CLOSE_DAY.value: 12,
    TriggerKind.REACTIVE_REPLAN.value: 6,
    TriggerKind.WEEKLY_REVIEW.value: 24 * 7,
    TriggerKind.MONTHLY_REVIEW.value: 24 * 28,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _zone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def local_date_key(moment: datetime, timezone_name: str) -> str:
    """Fecha local ``YYYY-MM-DD`` de un instante dado."""
    return moment.astimezone(_zone(timezone_name)).date().isoformat()


def parse_hhmm(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
    """Interpreta ``HH:MM``. Un valor invalido cae al respaldo, no revienta."""
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError):
        return fallback
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fallback
    return hour, minute


def dedupe_key(
    kind: str,
    user_id: str,
    *,
    date_key: str | None = None,
    iso_week: str | None = None,
    month: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
) -> str:
    """Clave natural del disparador, acotada al periodo.

    La clave **no** puede ser solo ``kind:user``: con eso el plan diario se
    deduplicaria una vez y no volveria a crearse nunca. El periodo forma parte
    de la identidad del trabajo.
    """
    if kind == TriggerKind.REACTIVE_REPLAN.value:
        return f"reactive_replan:{user_id}:{task_id}:{status}"
    if kind == TriggerKind.WEEKLY_REVIEW.value:
        return f"weekly_review:{user_id}:{iso_week}"
    if kind == TriggerKind.MONTHLY_REVIEW.value:
        return f"monthly_review:{user_id}:{month}"
    return f"{kind}:{user_id}:{date_key}"


def expires_at_for(kind: str, *, now: datetime) -> datetime | None:
    hours = EXPIRY_HOURS.get(kind)
    if hours is None:
        return None
    return now + timedelta(hours=hours)


def backoff_seconds(attempts: int, *, base: int = DEFAULT_BACKOFF_SECONDS) -> int:
    """Espera creciente y acotada entre reintentos: 60, 120, 240... hasta 1 hora."""
    exponent = max(0, int(attempts) - 1)
    return min(base * (2**exponent), MAX_BACKOFF_SECONDS)


def upcoming_occurrences(
    *,
    now: datetime,
    timezone_name: str,
    daily_plan_time: str = "06:00",
    close_day_time: str = "21:00",
    weekly_review_time: str = "18:00",
) -> list[dict[str, Any]]:
    """Proximas ocurrencias de los trabajos recurrentes.

    Devuelve siempre la ocurrencia **siguiente**, no la de hoy si ya paso: el
    trabajo de hoy ya no sirve de nada a las once de la noche.
    """
    zone = _zone(timezone_name)
    local_now = now.astimezone(zone)
    today = local_now.date()
    occurrences: list[dict[str, Any]] = []

    plan_hour, plan_minute = parse_hhmm(daily_plan_time, (6, 0))
    plan_at = datetime.combine(
        today, datetime.min.time().replace(hour=plan_hour, minute=plan_minute), tzinfo=zone
    )
    if plan_at <= local_now:
        plan_at += timedelta(days=1)
    occurrences.append(
        {
            "kind": TriggerKind.DAILY_PLAN.value,
            "run_at": plan_at,
            "date_key": plan_at.date().isoformat(),
        }
    )

    close_hour, close_minute = parse_hhmm(close_day_time, (21, 0))
    close_at = datetime.combine(
        today, datetime.min.time().replace(hour=close_hour, minute=close_minute), tzinfo=zone
    )
    if close_at <= local_now:
        close_at += timedelta(days=1)
    occurrences.append(
        {
            "kind": TriggerKind.CLOSE_DAY.value,
            "run_at": close_at,
            "date_key": close_at.date().isoformat(),
        }
    )

    review_hour, review_minute = parse_hhmm(weekly_review_time, (18, 0))
    # La revision semanal vive el domingo (weekday 6) de la semana en curso o de
    # la siguiente, segun si la hora ya paso.
    days_until_sunday = (6 - local_now.weekday()) % 7
    review_at = datetime.combine(
        today + timedelta(days=days_until_sunday),
        datetime.min.time().replace(hour=review_hour, minute=review_minute),
        tzinfo=zone,
    )
    if review_at <= local_now:
        review_at += timedelta(days=7)
    iso = review_at.isocalendar()
    occurrences.append(
        {
            "kind": TriggerKind.WEEKLY_REVIEW.value,
            "run_at": review_at,
            "iso_week": f"{iso[0]}-W{iso[1]:02d}",
        }
    )

    # La revision mensual se prepara el dia 1 del mes siguiente.
    year, month = local_now.year, local_now.month
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    month_at = datetime.combine(
        today.replace(year=year, month=month, day=1),
        datetime.min.time().replace(hour=review_hour, minute=review_minute),
        tzinfo=zone,
    )
    occurrences.append(
        {
            "kind": TriggerKind.MONTHLY_REVIEW.value,
            "run_at": month_at,
            "month": f"{year}-{month:02d}",
        }
    )
    return occurrences


class HorizonTriggerStore:
    """Acceso a los disparadores persistidos, con deduplicacion."""

    def __init__(self, repo: Any, *, settings: Any = None) -> None:
        self._repo = repo
        self._settings = settings

    def _max_attempts(self) -> int:
        try:
            return int(getattr(self._settings, "HORIZON_TRIGGER_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
        except (TypeError, ValueError):
            return DEFAULT_MAX_ATTEMPTS

    def _backoff_base(self) -> int:
        try:
            return int(
                getattr(self._settings, "HORIZON_TRIGGER_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS)
            )
        except (TypeError, ValueError):
            return DEFAULT_BACKOFF_SECONDS

    async def enqueue(
        self,
        *,
        user_id: str,
        kind: str,
        dedupe: str,
        run_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Registra un trabajo pendiente. Repetir la misma clave no duplica.

        Devuelve el disparador existente si ya estaba, para que quien encola
        pueda distinguir "lo cree" de "ya estaba".
        """
        moment = run_at or _now()
        existing = await self._repo.get_horizon_trigger_by_dedupe(dedupe)
        if existing:
            return existing

        # Se construye con el modelo, no con un dict suelto: asi el disparador
        # recibe su identificador y sus fechas por defecto. Un dict crudo naceria
        # sin ``trigger_id`` y no se podria reclamar ni marcar despues.
        document = document_dump(
            HorizonTrigger(
                user_id=user_id,
                kind=TriggerKind(kind),
                status=TriggerStatus.PENDING,
                next_run_at=moment,
                expires_at=expires_at_for(kind, now=moment),
                attempts=0,
                max_attempts=self._max_attempts(),
                dedupe_key=dedupe,
                payload=payload or {},
            )
        )
        created = await self._repo.create_horizon_trigger(document)
        if created is None:
            # Carrera perdida contra otro escritor con la misma clave: el indice
            # unico hizo su trabajo y devolvemos el que gano.
            return await self._repo.get_horizon_trigger_by_dedupe(dedupe)
        return created

    async def ensure_upcoming(self, *, user_id: str, timezone_name: str, now: datetime | None = None) -> int:
        """Asegura la proxima ocurrencia de cada trabajo recurrente.

        Es idempotente por ``dedupe_key``. La ocurrencia que ya se ejecuto queda
        como historial: no se reprograma mutando el documento, porque la revision
        posterior necesita poder leer lo que paso.
        """
        moment = now or _now()
        settings = self._settings
        occurrences = upcoming_occurrences(
            now=moment,
            timezone_name=timezone_name,
            daily_plan_time=str(getattr(settings, "HORIZON_DAILY_PLAN_TIME", "06:00")),
            close_day_time=str(getattr(settings, "HORIZON_CLOSE_DAY_TIME", "21:00")),
            weekly_review_time=str(getattr(settings, "HORIZON_WEEKLY_REVIEW_TIME", "18:00")),
        )
        created = 0
        for occurrence in occurrences:
            key = dedupe_key(
                occurrence["kind"],
                user_id,
                date_key=occurrence.get("date_key"),
                iso_week=occurrence.get("iso_week"),
                month=occurrence.get("month"),
            )
            before = await self._repo.get_horizon_trigger_by_dedupe(key)
            if before:
                continue
            result = await self.enqueue(
                user_id=user_id,
                kind=occurrence["kind"],
                dedupe=key,
                run_at=occurrence["run_at"],
                payload={"date_key": occurrence.get("date_key")},
            )
            if result:
                created += 1
        return created


class HorizonScheduler:
    """El unico timer de Horizon. Duerme hasta el proximo vencimiento.

    No sondea: pide el proximo vencimiento, duerme exactamente ese tiempo, y
    ademas lo despierta cualquier encolado (``wake``). Los dos juntos cubren lo
    que un ``sleep`` fijo no cubre: precision sin espera desperdiciada.
    """

    def __init__(
        self,
        *,
        repo: Any,
        store: HorizonTriggerStore,
        handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        settings: Any = None,
        now_fn: Callable[[], datetime] | None = None,
        max_sleep_seconds: int = DEFAULT_MAX_SLEEP_SECONDS,
        batch_limit: int = 20,
    ) -> None:
        self._repo = repo
        self._store = store
        self._handler = handler
        self._settings = settings
        self._now = now_fn or _now
        self._max_sleep = max(1, int(max_sleep_seconds))
        self._batch_limit = max(1, int(batch_limit))
        self._job: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    # ------------------------- ciclo de vida -------------------------

    async def start(self) -> None:
        if self._job is not None and not self._job.done():
            return
        self._stop.clear()
        self._wake.clear()
        await self._recover()
        self._job = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        # Despertar ademas del stop para que el bucle lo note de inmediato en vez
        # de esperar hasta el proximo vencimiento.
        self._wake.set()
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None

    def wake(self) -> None:
        """Avisa al timer de que hay trabajo nuevo. Lo llama el encolado."""
        self._wake.set()

    async def _recover(self) -> None:
        """Recupera el trabajo persistido antes de entrar al bucle.

        Dos cosas distintas (seccion 9.7: "al reiniciar recupera el estado y
        trabajo previamente persistidos"):
        - un disparador que quedo a medias vuelve a la cola;
        - un disparador vencido por caducidad se descarta en vez de ejecutarse.
        Los pendientes ya vencidos no necesitan nada especial: la primera
        consulta del bucle los encuentra.
        """
        cutoff = self._now() - timedelta(seconds=STALE_RUNNING_SECONDS)
        with suppress(Exception):
            await self._repo.recover_stale_horizon_triggers(cutoff)
        with suppress(Exception):
            await self._repo.expire_horizon_triggers(self._now())

    # ------------------------- bucle -------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            # Limpiar ANTES de consultar: asi un encolado que llegue durante la
            # consulta o el procesado no se pierde. Es el unico orden seguro.
            self._wake.clear()
            due = await self._due_now()
            if due:
                for trigger in due:
                    if self._stop.is_set():
                        break
                    await self._process(trigger)
                continue

            delay = await self._seconds_until_next()
            if delay is None:
                delay = float(self._max_sleep)
            delay = max(0.0, min(float(delay), float(self._max_sleep)))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise

    async def _due_now(self) -> list[dict[str, Any]]:
        try:
            return await self._repo.list_due_horizon_triggers(
                self._now(), limit=self._batch_limit
            )
        except Exception:
            logger.warning("Horizon: no se pudieron leer los disparadores vencidos", exc_info=True)
            return []

    async def _seconds_until_next(self) -> float | None:
        try:
            next_at = await self._repo.next_horizon_trigger_due_at()
        except Exception:
            logger.warning("Horizon: no se pudo calcular el proximo vencimiento", exc_info=True)
            return None
        if next_at is None:
            return None
        if next_at.tzinfo is None:
            next_at = next_at.replace(tzinfo=timezone.utc)
        return (next_at - self._now()).total_seconds()

    # ------------------------- ejecucion -------------------------

    async def _process(self, trigger: dict[str, Any]) -> None:
        trigger_id = str(trigger.get("trigger_id") or "")
        if not trigger_id:
            return

        expires_at = trigger.get("expires_at")
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= self._now():
                with suppress(Exception):
                    await self._repo.patch_horizon_trigger(
                        trigger_id,
                        {
                            "status": TriggerStatus.EXPIRED.value,
                            "finished_at": self._now(),
                            "last_error": "expired_before_run",
                        },
                    )
                return

        # Toma atomica: si otro proceso ya lo tomo, esta devuelve None y no se
        # ejecuta dos veces.
        claimed = await self._repo.mark_horizon_trigger_running(trigger_id)
        if not claimed:
            return

        try:
            result = await self._handler(claimed)
        except Exception as exc:
            logger.warning("Horizon: fallo el disparador %s", trigger_id, exc_info=True)
            await self._reschedule(claimed, error=str(exc))
            return

        with suppress(Exception):
            await self._repo.patch_horizon_trigger(
                trigger_id,
                {
                    "status": TriggerStatus.DONE.value,
                    "finished_at": self._now(),
                    "result": result or {},
                    "last_error": None,
                },
            )

    async def _reschedule(self, trigger: dict[str, Any], *, error: str) -> None:
        """Reintento acotado con espera creciente. Al agotarse, se declara."""
        trigger_id = str(trigger.get("trigger_id") or "")
        attempts = int(trigger.get("attempts") or 0) + 1
        max_attempts = int(trigger.get("max_attempts") or DEFAULT_MAX_ATTEMPTS)

        if attempts >= max_attempts:
            with suppress(Exception):
                await self._repo.patch_horizon_trigger(
                    trigger_id,
                    {
                        "status": TriggerStatus.FAILED.value,
                        "attempts": attempts,
                        "finished_at": self._now(),
                        "last_error": error,
                    },
                )
            return

        with suppress(Exception):
            await self._repo.patch_horizon_trigger(
                trigger_id,
                {
                    "status": TriggerStatus.PENDING.value,
                    "attempts": attempts,
                    "next_run_at": self._now()
                    + timedelta(seconds=backoff_seconds(attempts, base=self._store._backoff_base())),
                    "last_error": error,
                },
            )
