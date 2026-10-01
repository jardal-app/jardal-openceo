"""Tipos compartidos, taxonomia de errores y motor de intentos de adquisicion
(TASK-2026-00060).

Por que existe este modulo
==========================
Los clientes de adquisicion (Brave, Serper, Firecrawl, HTTP/RSS) comparten four
cosas que no deben reimplementarse a mano en cada uno porque un error ahi se paga
con dinero o con cuota:

1. **Taxonomia diferenciada.** Configuracion ausente, auth, cuota agotada,
   rate-limit, transporte y parseo son causas distintas, con recuperacion distinta.
   Mezclarlas produce dos fallos clasicos: reintentar un 402 (gasta sin poder) o
   dar por gratis un timeout ambiguo (gasta sin registrar). Aqui no se copia la
   excepcion ``Duplicate`` de ERPNext: un proveedor de busqueda no tiene semantica
   de duplicado de negocio, y arrastrarla haria que un resultado repetido se
   tratara como error transitorio.

2. **Deadline por fase.** El job tiene un presupuesto de segundos; cada intento y
   cada espera de ``Retry-After`` se recorta contra lo que queda. Sin esto, un 429
   con ``Retry-After: 600`` mantendria el job vivo mucho mas alla de su deadline.

3. **Reserva/registro por intento.** Antes de cada I/O se reserva; despues se
   liquida. Un fallo *seguro* (la peticion no llego al proveedor) libera; un fallo
   *ambiguo* (timeout tras enviar) se marca incierto y **no** se reintenta ni se
   cuenta como cero.

4. **Disyuntor temporal compartido.** Errores repetidos de un proveedor abren un
   circuito durante un plazo; el estado es un objeto compartido (no una variable
   local) para que dos jobs concurrentes no sigan golpeando la misma cuenta caida.

La degradacion es **estructurada**: las operaciones publicas devuelven un resultado
con ``degradations``, no lanzan excepciones esperables. Una ejecucion sin claves no
rompe el servicio y jamas cae implicitamente a un proveedor pagado.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping

import httpx

from research.budget import BudgetError
from research.contracts import CoverageReason
from research.money import format_micro

logger = logging.getLogger(__name__)

__all__ = [
    "AcquisitionError",
    "Attempt",
    "BudgetRefusedError",
    "CircuitBreaker",
    "CircuitOpenError",
    "Clock",
    "CostEstimate",
    "Deadline",
    "DeadlineExceededError",
    "Degradation",
    "DestinationBlockedError",
    "DiscoveryItem",
    "DiscoveryResult",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PHASE_TIMEOUT_SECONDS",
    "MAX_RETRY_AFTER_WITHOUT_DEADLINE",
    "MissingConfigError",
    "ProviderAmbiguousTimeout",
    "ProviderAuthError",
    "ProviderCallResult",
    "ProviderDisabledError",
    "ProviderHTTPError",
    "ProviderParseError",
    "ProviderProtocolError",
    "ProviderQuotaExhausted",
    "ProviderRateLimited",
    "ProviderServerError",
    "ProviderTooLarge",
    "ProviderTransportError",
    "ProviderUnsupportedMedia",
    "SYSTEM_CLOCK",
    "SystemClock",
    "discovery_from_call",
    "parse_datetime",
    "parse_optional_int",
    "parse_retry_after",
    "run_provider_call",
    "safe_discovery_item",
]

# Presupuestos por defecto. Son techos, no promesas: el deadline del job siempre
# puede recortarlos aun mas.
DEFAULT_PHASE_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
# Sin deadline de job, una espera de Retry-After mayor que esto se rechaza en vez de
# dormir: un proveedor no puede fijar por si solo cuanto tiempo vive el proceso.
MAX_RETRY_AFTER_WITHOUT_DEADLINE = 60.0


# --------------------------------------------------------------------------- reloj


class Clock:
    """Reloj monotono inyectable (real en produccion, falso en pruebas)."""

    def now(self) -> float:  # pragma: no cover - interfaz
        raise NotImplementedError

    async def sleep(self, seconds: float) -> None:  # pragma: no cover - interfaz
        raise NotImplementedError


class SystemClock(Clock):
    """Reloj real: ``time.monotonic`` para medir y ``asyncio.sleep`` para esperar."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


SYSTEM_CLOCK = SystemClock()


class Deadline:
    """Presupuesto de tiempo de un job, medido con el reloj inyectado.

    ``remaining`` nunca es negativo y ``phase_timeout`` recorta el timeout de una
    fase para que el ultimo intento no cruce el deadline. Un deadline agotado no se
    "redondea": lanza, y el motor lo traduce a degradacion.
    """

    def __init__(self, total_seconds: float, *, clock: Clock | None = None) -> None:
        if total_seconds <= 0:
            raise ValueError("Un deadline necesita segundos positivos")
        self._clock = clock or SYSTEM_CLOCK
        self._total = float(total_seconds)
        self._started_at = self._clock.now()

    @classmethod
    def start(cls, total_seconds: float, *, clock: Clock | None = None) -> "Deadline":
        return cls(total_seconds, clock=clock)

    @property
    def remaining(self) -> float:
        return max(0.0, self._total - (self._clock.now() - self._started_at))

    @property
    def expired(self) -> bool:
        return self.remaining <= 0.0

    def phase_timeout(self, requested: float, *, provider: str = "") -> float:
        remaining = self.remaining
        if remaining <= 0.0:
            raise DeadlineExceededError(
                "El deadline del job se agoto antes de iniciar la fase", provider=provider
            )
        return min(float(requested), remaining)

    def allows(self, delay: float) -> bool:
        return float(delay) <= self.remaining


# ------------------------------------------------------------------ taxonomia


class AcquisitionError(RuntimeError):
    """Fallo esperable de una operacion de adquisicion.

    ``kind`` es el vocabulario estable que fachada, ledger y cobertura comparten.
    Las subclases no son intercambiables: cada una implica una recuperacion
    distinta.
    """

    kind = "acquisition_error"
    coverage_reason = CoverageReason.SOURCE_UNAVAILABLE
    retryable = False

    def __init__(
        self,
        detail: str,
        *,
        provider: str = "",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.provider = provider
        self.retry_after = retry_after

    def degradation(self) -> "Degradation":
        return Degradation(
            provider=self.provider,
            reason=self.kind,
            coverage_reason=self.coverage_reason,
            detail=self.detail,
            retry_after=self.retry_after,
        )


class MissingConfigError(AcquisitionError):
    """Falta la clave o el adaptador esta apagado. Degradacion, nunca excepcion."""

    kind = "missing_config"


class ProviderDisabledError(MissingConfigError):
    """El adaptador esta apagado por politica, no por falta de clave."""

    kind = "disabled"


class ModelPolicyUnavailableError(AcquisitionError):
    """No hay ruta de modelo compatible con la clasificacion del trabajo.

    No es un fallo de configuracion que se arregle con otra clave ni un hueco de
    cobertura del proveedor: es una decision de privacidad. El trabajo no se ejecuta
    y **no** se cae a la ruta publica, porque esa ruta puede entrenar con los
    prompts (``TASK-2026-00064``).
    """

    kind = "model_policy_unavailable"
    coverage_reason = CoverageReason.MODEL_POLICY_UNAVAILABLE


class ProviderAuthError(AcquisitionError):
    """401/403: la credencial no autoriza el acceso."""

    kind = "auth"
    coverage_reason = CoverageReason.ACCESS_DENIED


class ProviderQuotaExhausted(AcquisitionError):
    """402: cuota agotada. No se reintenta: reintentar no crea saldo."""

    kind = "quota_exhausted"
    coverage_reason = CoverageReason.QUOTA_EXHAUSTED


class ProviderRateLimited(AcquisitionError):
    """429: espera indicada por el proveedor, recortada por el deadline."""

    kind = "rate_limited"
    coverage_reason = CoverageReason.RATE_LIMITED


class ProviderTransportError(AcquisitionError):
    """Fallo de red **antes** de entregar la peticion: reintento seguro y acotado."""

    kind = "transport"
    retryable = True


class ProviderAmbiguousTimeout(ProviderTransportError):
    """Timeout tras enviar: no se sabe si el proveedor cobro.

    Deliberadamente **no** reintentable: reintentar podria pagar dos veces y ademas
    la reserva queda ``uncertain``, no liberada.
    """

    kind = "timeout_ambiguous"
    retryable = False


class ProviderServerError(AcquisitionError):
    """5xx: el proveedor respondio con error. Se reintenta solo si el contrato
    declara que una respuesta de error no se factura."""

    kind = "server_error"
    retryable = True


class ProviderHTTPError(AcquisitionError):
    """Otro 4xx no clasificado: la peticion no es valida. No se reintenta."""

    kind = "http_error"


class ProviderProtocolError(AcquisitionError):
    """Respuesta que rompe el protocolo (p. ej. redirect sin Location)."""

    kind = "protocol"


class ProviderTooLarge(AcquisitionError):
    """El cuerpo descomprimido supera el limite de bytes."""

    kind = "too_large"


class ProviderUnsupportedMedia(AcquisitionError):
    """El MIME no esta en la lista autorizada."""

    kind = "unsupported_media"


class ProviderParseError(AcquisitionError):
    """El cuerpo no cumple el contrato del proveedor."""

    kind = "parse_error"


class DestinationBlockedError(AcquisitionError):
    """El guard de egress bloqueo el destino (SSRF, puerto, esquema o redireccion)."""

    kind = "egress_blocked"


class DeadlineExceededError(AcquisitionError):
    """No queda tiempo del job para iniciar o esperar la fase."""

    kind = "deadline_exceeded"


class CircuitOpenError(AcquisitionError):
    """El disyuntor temporal del proveedor esta abierto."""

    kind = "circuit_open"


class BudgetRefusedError(AcquisitionError):
    """La reserva fue rechazada (techo, limite de proyecto/job o saldo desconocido)."""

    kind = "budget_refused"
    coverage_reason = CoverageReason.BUDGET_EXHAUSTED


@dataclass(frozen=True)
class Degradation:
    """Causa estructurada de cobertura parcial. No es una excepcion ni un estado."""

    provider: str
    reason: str
    coverage_reason: CoverageReason
    detail: str = ""
    retry_after: float | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "reason": self.reason,
            "coverage_reason": self.coverage_reason.value,
            "detail": self.detail,
        }
        if self.retry_after is not None:
            payload["retry_after_seconds"] = self.retry_after
        return payload


# ------------------------------------------------------------------ disyuntor


class CircuitBreaker:
    """Disyuntor temporal con estado compartido por ``proveedor:cuenta``.

    El estado vive en este objeto y no en cada llamada: dos jobs concurrentes que
    comparten cuenta comparten el circuito. El candado es de hilo (``threading``)
    porque el servidor puede atender peticiones en varios hilos y la comprobacion
    es sincrona y muy corta.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_seconds: float = 60.0,
        clock: Clock | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold debe ser >= 1")
        if open_seconds <= 0:
            raise ValueError("open_seconds debe ser positivo")
        self._threshold = int(failure_threshold)
        self._open_seconds = float(open_seconds)
        self._clock = clock or SYSTEM_CLOCK
        self._failures: dict[str, int] = {}
        self._opened_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, provider: str = "") -> None:
        """Lanza ``CircuitOpenError`` si el circuito sigue abierto."""
        with self._lock:
            opened_until = self._opened_until.get(key)
            if opened_until is None:
                return
            if self._clock.now() >= opened_until:
                self._opened_until.pop(key, None)
                self._failures[key] = 0
                return
            remaining = opened_until - self._clock.now()
        raise CircuitOpenError(
            f"Circuito abierto para {provider or key}; reintentar en {remaining:.1f}s",
            provider=provider,
            retry_after=remaining,
        )

    def record_failure(self, key: str) -> None:
        with self._lock:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count >= self._threshold:
                self._opened_until[key] = self._clock.now() + self._open_seconds

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._opened_until.pop(key, None)

    def is_open(self, key: str) -> bool:
        with self._lock:
            opened_until = self._opened_until.get(key)
            return opened_until is not None and self._clock.now() < opened_until

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._failures.clear()
                self._opened_until.clear()
            else:
                self._failures.pop(key, None)
                self._opened_until.pop(key, None)


# ------------------------------------------------------------------ resultados


@dataclass(frozen=True)
class CostEstimate:
    """Coste de **las opciones concretas** de una operacion, no el maximo teorico."""

    amount_micro_usd: int
    credits: int = 0
    detail: str = ""


@dataclass(frozen=True)
class Attempt:
    """Registro de un intento. Todo intento termina en un estado contable."""

    provider: str
    operation_id: str
    index: int
    outcome: str  # committed | released | uncertain
    charged_micro_usd: int | None
    credits: int = 0
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "index": self.index,
            "outcome": self.outcome,
            "charged_usd": format_micro(self.charged_micro_usd),
            "credits": self.credits,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DiscoveryItem:
    """Candidato de URL descubierto por un buscador.

    **No tiene snippet ni titulo a proposito.** Las condiciones de Brave prohiben
    almacenar/cachear sus resultados y entrenar con ellos; el SERP es transitorio
    dentro del broker. La evidencia se construye abriendo la publicacion original y
    leyendo su texto, no copiando el ranking del buscador.
    """

    url: str
    provider: str
    rank: int

    def to_payload(self) -> dict[str, Any]:
        return {"url": self.url, "provider": self.provider, "rank": self.rank}


@dataclass(frozen=True)
class DiscoveryResult:
    """Resultado de una operacion de descubrimiento.

    ``charged_micro_usd is None`` significa consumo **incierto** (timeout ambiguo);
    nunca se serializa como 0.
    """

    provider: str
    items: tuple[DiscoveryItem, ...] = ()
    degradations: tuple[Degradation, ...] = ()
    attempts: tuple[Attempt, ...] = ()
    charged_micro_usd: int | None = 0
    credits: int = 0
    operation_id: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.items) and not self.degradations

    def to_payload(self) -> dict[str, Any]:
        # Lista blanca explicita: lo que no esta aqui no llega al modelo, al log ni
        # al almacen. El SERP completo no es un campo de este objeto.
        return {
            "provider": self.provider,
            "operation_id": self.operation_id,
            "items": [item.to_payload() for item in self.items],
            "degradations": [d.to_payload() for d in self.degradations],
            "attempts": [a.to_payload() for a in self.attempts],
            "charged_usd": format_micro(self.charged_micro_usd),
            "uncertain": self.charged_micro_usd is None,
            "credits": self.credits,
        }


@dataclass(frozen=True)
class ProviderCallResult:
    """Salida cruda del motor de intentos, antes de interpretar el payload."""

    provider: str
    payload: dict[str, Any] | None = None
    attempts: tuple[Attempt, ...] = ()
    degradations: tuple[Degradation, ...] = ()
    charged_micro_usd: int | None = 0
    credits: int = 0


# ------------------------------------------------------------------ utilidades


def parse_retry_after(value: str | None) -> float | None:
    """Segundos de ``Retry-After`` (numero o fecha HTTP). Cadena invalida = None."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if moment is None:
            return None
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (moment - now).total_seconds())
    return max(0.0, seconds)


def parse_datetime(value: str | None):
    """Fecha verificable desde ISO-8601 o RFC-2822. Sin dato devuelve ``None``.

    No se inventa fecha de publicacion: si la fuente no la declara, la evidencia
    queda con ``published_at=None`` y eso es informacion, no un hueco a rellenar.
    """
    if not value:
        return None
    from datetime import datetime, timezone

    text = value.strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_optional_int(value: Any) -> int | None:
    """Entero de configuracion (p. ej. consultas de saldo). Vacio = desconocido."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def safe_discovery_item(url: Any, *, provider: str, rank: int) -> DiscoveryItem | None:
    """Item minimo a partir de una URL de buscador; descarta todo lo demas.

    Es la unica puerta por la que un resultado de proveedor entra al resto del
    sistema: sin snippet, sin titulo, sin ranking completo.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    return DiscoveryItem(url=url, provider=provider, rank=rank)


def discovery_from_call(
    call: "ProviderCallResult", *, provider: str, operation_id: str
) -> DiscoveryResult:
    items: tuple[DiscoveryItem, ...] = ()
    if call.payload is not None:
        items = tuple(call.payload.get("items") or ())
    return DiscoveryResult(
        provider=provider,
        items=items,
        degradations=call.degradations,
        attempts=call.attempts,
        charged_micro_usd=call.charged_micro_usd,
        credits=call.credits,
        operation_id=operation_id,
    )


# ------------------------------------------------------------------- motor


def _breaker_key(provider: str, account: str | None) -> str:
    return f"{provider}:{account or 'default'}"


async def _release(ledger: Any | None, reservation: Any | None) -> None:
    if ledger is not None and reservation is not None:
        await ledger.release(reservation)


async def _commit(ledger: Any | None, reservation: Any | None, charged: int) -> None:
    if ledger is not None and reservation is not None:
        await ledger.commit(reservation, charged_micro_usd=charged)


async def _mark_uncertain(ledger: Any | None, reservation: Any | None) -> None:
    if ledger is not None and reservation is not None:
        await ledger.mark_uncertain(reservation)


async def run_provider_call(
    *,
    provider: str,
    operation_id: str,
    research_id: str,
    project_key: str,
    cost: CostEstimate,
    request: Callable[[float], Awaitable[httpx.Response]],
    parse: Callable[[httpx.Response], Mapping[str, Any]],
    deadline: Deadline | None = None,
    ledger: Any | None = None,
    ceiling_micro_usd: int | None = None,
    project_limit_micro_usd: int | None = None,
    job_limit_micro_usd: int | None = None,
    account: str = "default",
    allow_unknown_ceiling: bool = False,
    breaker: CircuitBreaker | None = None,
    clock: Clock | None = None,
    phase_timeout: float = DEFAULT_PHASE_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    retry_server_errors: bool = True,
) -> ProviderCallResult:
    """Ejecuta una operacion de proveedor con reserva, reintento acotado y deadline.

    Reglas economicas que implementa (y que las pruebas fijan):

    * **402** = agotamiento: se libera la reserva y **no** se reintenta.
    * **401/403** = auth/acceso: se libera y no se reintenta.
    * **429** = espera indicada por el proveedor, recortada por el deadline; si la
      espera no cabe, se degrada sin dormir.
    * **timeout tras enviar** = ambiguo: ``mark_uncertain`` y sin reintento. No es
      gratis.
    * **fallo de conexion** (la peticion no llego) = liberacion y reintento acotado.
    * **5xx** = reintento acotado **solo** si el contrato declara que una respuesta
      de error no se factura (``retry_server_errors``); la reserva se libera porque
      el intento no produjo resultado facturable.
    * **parseo** = la llamada si ocurrio: se liquida el coste y se degrada.
    """
    attempts: list[Attempt] = []
    degradations: list[Degradation] = []
    charged_total = 0
    credits_total = 0
    uncertain = False
    payload: dict[str, Any] | None = None
    active_clock = clock or SYSTEM_CLOCK
    key = _breaker_key(provider, account)
    attempts_limit = max(1, int(max_attempts))

    def record_attempt(outcome: str, charged: int | None, detail: str, credits: int = 0) -> None:
        attempts.append(
            Attempt(
                provider=provider,
                operation_id=f"{operation_id}#{index}",
                index=index,
                outcome=outcome,
                charged_micro_usd=charged,
                credits=credits,
                detail=detail,
            )
        )

    for index in range(1, attempts_limit + 1):
        if deadline is not None and deadline.expired:
            degradations.append(
                DeadlineExceededError("Deadline agotado antes del intento", provider=provider).degradation()
            )
            break
        if breaker is not None:
            try:
                breaker.check(key, provider=provider)
            except CircuitOpenError as exc:
                degradations.append(exc.degradation())
                break

        reservation: Any | None = None
        if ledger is not None:
            try:
                decision = await ledger.reserve(
                    provider=provider,
                    amount_micro_usd=cost.amount_micro_usd,
                    operation_id=f"{operation_id}#{index}",
                    project_key=project_key,
                    research_id=research_id,
                    ceiling_micro_usd=ceiling_micro_usd,
                    project_limit_micro_usd=project_limit_micro_usd,
                    job_limit_micro_usd=job_limit_micro_usd,
                    allow_unknown_ceiling=allow_unknown_ceiling,
                    account=account,
                )
            except BudgetError as exc:
                degradations.append(
                    BudgetRefusedError(str(exc), provider=provider).degradation()
                )
                break
            if not decision.accepted:
                degradations.append(
                    BudgetRefusedError(
                        f"Reserva rechazada: {decision.reason or 'sin motivo'}",
                        provider=provider,
                    ).degradation()
                )
                break
            reservation = decision.reservation

        try:
            timeout = (
                deadline.phase_timeout(phase_timeout, provider=provider)
                if deadline is not None
                else float(phase_timeout)
            )
        except DeadlineExceededError as exc:
            await _release(ledger, reservation)
            record_attempt("released", 0, "deadline_exceeded")
            degradations.append(exc.degradation())
            break

        try:
            response = await request(timeout)
        except httpx.ConnectTimeout as exc:
            # La conexion no se establecio: la peticion no llego al proveedor.
            await _release(ledger, reservation)
            record_attempt("released", 0, "connect_timeout")
            if breaker is not None:
                breaker.record_failure(key)
            if index < attempts_limit and _fits(deadline, backoff_seconds):
                if backoff_seconds > 0:
                    await active_clock.sleep(_bounded_wait(deadline, backoff_seconds))
                continue
            degradations.append(
                ProviderTransportError(f"Conexion agotada: {exc}", provider=provider).degradation()
            )
            break
        except httpx.TimeoutException as exc:
            # Enviada sin respuesta: no se sabe si el proveedor cobro.
            await _mark_uncertain(ledger, reservation)
            record_attempt("uncertain", None, "ambiguous_timeout")
            uncertain = True
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(
                ProviderAmbiguousTimeout(
                    f"Timeout ambiguo tras enviar: {exc}", provider=provider
                ).degradation()
            )
            break
        except httpx.ConnectError as exc:
            await _release(ledger, reservation)
            record_attempt("released", 0, "connect_error")
            if breaker is not None:
                breaker.record_failure(key)
            if index < attempts_limit and _fits(deadline, backoff_seconds):
                if backoff_seconds > 0:
                    await active_clock.sleep(_bounded_wait(deadline, backoff_seconds))
                continue
            degradations.append(
                ProviderTransportError(f"Fallo de conexion: {exc}", provider=provider).degradation()
            )
            break
        except httpx.TransportError as exc:
            # Error de lectura/escritura/protocolo con la peticion ya enviada.
            await _mark_uncertain(ledger, reservation)
            record_attempt("uncertain", None, "ambiguous_transport")
            uncertain = True
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(
                ProviderAmbiguousTimeout(
                    f"Transporte ambiguo tras enviar: {exc}", provider=provider
                ).degradation()
            )
            break
        except httpx.HTTPError as exc:
            await _mark_uncertain(ledger, reservation)
            record_attempt("uncertain", None, "ambiguous_http_error")
            uncertain = True
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(
                ProviderAmbiguousTimeout(
                    f"Error HTTP ambiguo: {exc}", provider=provider
                ).degradation()
            )
            break

        status = response.status_code

        if status in {401, 403}:
            await _release(ledger, reservation)
            record_attempt("released", 0, f"http_{status}")
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(
                ProviderAuthError(f"Acceso denegado por el proveedor ({status})", provider=provider).degradation()
            )
            break

        if status == 402:
            await _release(ledger, reservation)
            record_attempt("released", 0, "http_402")
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(
                ProviderQuotaExhausted(
                    "El proveedor informa cuota agotada (402)", provider=provider
                ).degradation()
            )
            break

        if status == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            wait = retry_after if retry_after is not None else backoff_seconds
            await _release(ledger, reservation)
            record_attempt("released", 0, "http_429")
            if breaker is not None:
                breaker.record_failure(key)
            if index < attempts_limit and _fits(deadline, wait, default_cap=True):
                if wait > 0:
                    await active_clock.sleep(_bounded_wait(deadline, wait))
                continue
            degradations.append(
                ProviderRateLimited(
                    "Rate limit del proveedor (429)",
                    provider=provider,
                    retry_after=retry_after,
                ).degradation()
            )
            break

        if status >= 500:
            await _release(ledger, reservation)
            record_attempt("released", 0, f"http_{status}")
            if breaker is not None:
                breaker.record_failure(key)
            if (
                retry_server_errors
                and index < attempts_limit
                and _fits(deadline, backoff_seconds)
            ):
                if backoff_seconds > 0:
                    await active_clock.sleep(_bounded_wait(deadline, backoff_seconds))
                continue
            degradations.append(
                ProviderServerError(f"El proveedor fallo ({status})", provider=provider).degradation()
            )
            break

        if status >= 400:
            await _release(ledger, reservation)
            record_attempt("released", 0, f"http_{status}")
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(
                ProviderHTTPError(f"Respuesta {status} del proveedor", provider=provider).degradation()
            )
            break

        try:
            parsed = parse(response)
        except AcquisitionError as exc:
            # La llamada se completo: se liquida igualmente para no contabilizarla
            # como gratis, y se reporta el parseo como causa.
            await _commit(ledger, reservation, cost.amount_micro_usd)
            charged_total += cost.amount_micro_usd
            record_attempt("committed", cost.amount_micro_usd, exc.kind)
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(exc.degradation())
            break
        except Exception as exc:  # noqa: BLE001 - cuerpo malformado no puede tumbar el job
            await _commit(ledger, reservation, cost.amount_micro_usd)
            charged_total += cost.amount_micro_usd
            record_attempt("committed", cost.amount_micro_usd, "parse_error")
            if breaker is not None:
                breaker.record_failure(key)
            degradations.append(
                ProviderParseError(
                    f"Respuesta ilegible del proveedor: {exc.__class__.__name__}",
                    provider=provider,
                ).degradation()
            )
            break

        payload = dict(parsed)
        await _commit(ledger, reservation, cost.amount_micro_usd)
        charged_total += cost.amount_micro_usd
        credits_total += cost.credits
        record_attempt("committed", cost.amount_micro_usd, "ok", credits=cost.credits)
        if breaker is not None:
            breaker.record_success(key)
        break

    return ProviderCallResult(
        provider=provider,
        payload=payload,
        attempts=tuple(attempts),
        degradations=tuple(degradations),
        charged_micro_usd=None if uncertain else charged_total,
        credits=credits_total,
    )


def _fits(deadline: Deadline | None, delay: float, *, default_cap: bool = False) -> bool:
    if deadline is None:
        if not default_cap:
            return True
        return delay <= MAX_RETRY_AFTER_WITHOUT_DEADLINE
    return deadline.allows(delay)


def _bounded_wait(deadline: Deadline | None, delay: float) -> float:
    if deadline is None:
        return max(0.0, float(delay))
    return max(0.0, min(float(delay), deadline.remaining))
