"""Grounding nativo de Muse: ``research_grounded_lookup`` (TASK-2026-00064).

Que hace
========
Adaptador **opcional** que llama al endpoint fijo de Meta ``/responses`` con
``tools=[{"type": "web_search"}]`` e ``include=["web_search_call.results"]``, y
devuelve al motor principal citas, fuentes observadas y consumo.

Que NO hace, y por que importa
==============================
* **No confunde habilitar la herramienta con haber buscado.** Enviar `web_search`
  no garantiza que el modelo la use: el resultado distingue `tool_enabled` de
  `searched` y, si el cliente exige actualidad, declara el hueco en vez de afirmar
  cobertura.
* **No trata la cita como lectura independiente.** Una `url_citation` es evidencia
  **derivada**: no acredita que se leyera el articulo ni permite calcular su hash.
  Para verificar la pagina original hay que abrirla con ``acquisition/http_reader``
  (egress + retencion de esa fuente).
* **No infiere el coste del numero de resultados.** Liquida a partir del uso
  reportado (busquedas y tokens). Si falta el contador, ``reconciled`` queda en
  ``None`` con la reserva marcada incierta, nunca en 0.
* **No hay fallback publico desde datos restringidos.** Contributor entrena con los
  prompts: la clasificacion distinta de `public` se degrada como
  ``model_policy_unavailable`` sin llamar.

Contabilidad
============
A diferencia de los buscadores, aqui el coste real **no** se conoce antes de la
llamada: depende de cuantas busquedas internas ejecute el modelo y de los tokens.
Por eso el adaptador no usa ``run_provider_call`` (que liquida el coste estimado):
hace su propia reserva con ``ResearchBudgetLedger`` y liquida el importe derivado
del uso reportado, o lo deja incierto si el proveedor no lo reporta.

Referencias de precio **fechadas** (2026-10-01, no saldo medido):
$0,0025 por busqueda de grounding mas tokens; Contributor $0,10/M de entrada,
$0,20/M de salida y $0,002/M de entrada cacheada.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any
from uuid import uuid4

import httpx

from research.budget import BudgetError
from research.contracts import Classification, CoverageReason
from research.money import format_micro, parse_optional_micro
from research.policies import FixedProviderEndpoint

from .base import (
    AcquisitionError,
    Attempt,
    BudgetRefusedError,
    CircuitBreaker,
    Clock,
    Deadline,
    DeadlineExceededError,
    Degradation,
    MissingConfigError,
    ModelPolicyUnavailableError,
    ProviderAmbiguousTimeout,
    ProviderAuthError,
    ProviderDisabledError,
    ProviderHTTPError,
    ProviderParseError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTransportError,
    SYSTEM_CLOCK,
    parse_retry_after,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CACHED_INPUT_MICRO_PER_MILLION",
    "DEFAULT_MUSE_BASE_URL",
    "GROUNDING_SEARCH_MICRO_USD",
    "GroundingCitation",
    "GroundingSource",
    "GroundedResult",
    "GroundingClient",
    "INPUT_MICRO_PER_MILLION",
    "OUTPUT_MICRO_PER_MILLION",
    "token_cost_micro",
]

DEFAULT_MUSE_BASE_URL = "https://api.meta.ai/v1"
_RESPONSES_PATH = "/responses"

# $0,0025 por busqueda de grounding. En micro-USD: 2.500. Un centavo entero lo
# redondearia a 0 y el libro diria que una operacion pagada fue gratis.
GROUNDING_SEARCH_MICRO_USD = 2_500

# Contributor: $0,10/M entrada, $0,20/M salida, $0,002/M entrada cacheada.
INPUT_MICRO_PER_MILLION = 100_000
OUTPUT_MICRO_PER_MILLION = 200_000
CACHED_INPUT_MICRO_PER_MILLION = 2_000


def token_cost_micro(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None = None,
) -> int | None:
    """Coste de tokens en micro-USD, o ``None`` si falta el contador.

    ``cached_tokens`` se descuenta de ``input_tokens`` cuando la API reporta el total
    de entrada incluyendo los cacheados: cobrar ambos seria contar dos veces el mismo
    token. Se redondea **hacia arriba** al micro, para no subestimar el gasto.
    """
    if input_tokens is None and output_tokens is None:
        return None
    entries = (input_tokens or 0, output_tokens or 0, cached_tokens or 0)
    if any(value < 0 for value in entries):
        return None
    cached = min(cached_tokens or 0, input_tokens or 0)
    billable_input = max(0, (input_tokens or 0) - cached)
    total = (
        Decimal(billable_input) * Decimal(INPUT_MICRO_PER_MILLION)
        + Decimal(cached) * Decimal(CACHED_INPUT_MICRO_PER_MILLION)
        + Decimal(output_tokens or 0) * Decimal(OUTPUT_MICRO_PER_MILLION)
    ) / Decimal(1_000_000)
    return int(total.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class GroundingSource:
    """Fuente observada en ``web_search_call.results`` (no es una lectura)."""

    url: str
    title: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {"url": self.url, "title": self.title}


@dataclass(frozen=True)
class GroundingCitation:
    """Cita ``url_citation`` devuelta por el modelo: evidencia **derivada**."""

    url: str
    title: str | None = None
    cited_text: str | None = None
    start_index: int | None = None
    end_index: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "cited_text": self.cited_text,
            "start_index": self.start_index,
            "end_index": self.end_index,
        }


@dataclass(frozen=True)
class GroundedResult:
    """Salida del grounding. Lista blanca: el cuerpo crudo del proveedor no viaja."""

    provider: str
    operation_id: str
    model: str = ""
    answer: str = ""
    tool_enabled: bool = False
    searched: bool = False
    searches_performed: int = 0
    citations: tuple[GroundingCitation, ...] = ()
    sources: tuple[GroundingSource, ...] = ()
    duplicate_sources: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    estimated_micro_usd: int = 0
    reconciled_micro_usd: int | None = None
    charged_micro_usd: int | None = 0
    hard_cap: bool = False
    attempts: tuple[Attempt, ...] = ()
    degradations: tuple[Degradation, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.answer) and not self.degradations

    @property
    def usage_uncertain(self) -> bool:
        return self.reconciled_micro_usd is None

    def coverage_gaps(self) -> list[dict[str, Any]]:
        """Huecos de cobertura tipados. No son "no hay noticias"."""
        gaps: list[dict[str, Any]] = []
        if not self.searched:
            gaps.append(
                {
                    "source": "grounding",
                    "reason": CoverageReason.SOURCE_UNAVAILABLE.value,
                    "detail": "La herramienta de busqueda estaba habilitada pero el modelo no busco",
                }
            )
        if self.searched and not self.sources and not self.citations:
            gaps.append(
                {
                    "source": "grounding",
                    "reason": CoverageReason.SOURCE_UNAVAILABLE.value,
                    "detail": "Hubo busqueda pero el proveedor no devolvio fuentes ni citas",
                }
            )
        for degradation in self.degradations:
            gaps.append(
                {
                    "source": degradation.provider,
                    "reason": degradation.coverage_reason.value,
                    "detail": degradation.detail or degradation.reason,
                }
            )
        return gaps

    def derived_evidence(self) -> list[dict[str, Any]]:
        """Evidencia derivada, con ``content_hash=None`` y acceso ``grounding``.

        El hash es nulo **a proposito**: no se leyo el articulo, asi que no puede
        afirmarse un hash de su contenido. Verificar la pagina exige abrirla.
        """
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for source in self.sources:
            if source.url in seen:
                continue
            seen.add(source.url)
            rows.append(
                {
                    "url": source.url,
                    "title": source.title,
                    "access_method": "grounding",
                    "source_type": "unknown",
                    "content_hash": None,
                    "retention_policy": "source-specific",
                    "note": "Evidencia derivada del grounding; abrir la pagina original para verificarla",
                }
            )
        return rows

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "operation_id": self.operation_id,
            "model": self.model,
            "answer": self.answer,
            "tool_enabled": self.tool_enabled,
            "searched": self.searched,
            "searches_performed": self.searches_performed,
            "citations": [c.to_payload() for c in self.citations],
            "sources": [s.to_payload() for s in self.sources],
            "duplicate_sources": self.duplicate_sources,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_tokens": self.cached_tokens,
                "searches": self.searches_performed,
                "estimated_usd": format_micro(self.estimated_micro_usd),
                "reconciled_usd": format_micro(self.reconciled_micro_usd),
                "uncertain": self.usage_uncertain,
                "hard_cap": self.hard_cap,
            },
            "attempts": [a.to_payload() for a in self.attempts],
            "degradations": [d.to_payload() for d in self.degradations],
            "warnings": list(self.warnings),
            "coverage_gaps": self.coverage_gaps(),
        }


def _setting(settings: Any, name: str, default: Any) -> Any:
    if settings is None:
        return default
    return getattr(settings, name, default)


class GroundingClient:
    """``research_grounded_lookup`` sobre Responses + web_search.

    Es un adaptador **opcional**: deshabilitado o sin clave se degrada de forma
    estructurada y el resto del servicio sigue funcionando. La ruta gratuita del
    gateway opencode **no** valida esta API directa; son rutas distintas.
    """

    provider = "muse"

    def __init__(
        self,
        *,
        settings: Any = None,
        http_client: httpx.AsyncClient | None = None,
        ledger: Any | None = None,
        endpoint: FixedProviderEndpoint | None = None,
        breaker: CircuitBreaker | None = None,
        clock: Clock | None = None,
        adapter_enabled: bool | None = None,
        api_key: str | None = None,
        model: str | None = None,
        monthly_budget_micro_usd: int | None = None,
        max_attempts: int = 3,
        phase_timeout: float = 30.0,
        backoff_seconds: float = 0.5,
        account: str = "default",
    ) -> None:
        base_url = str(_setting(settings, "RESEARCH_META_BASE_URL", DEFAULT_MUSE_BASE_URL) or DEFAULT_MUSE_BASE_URL)
        self._settings = settings
        self._client = http_client
        self._ledger = ledger
        self._endpoint = endpoint or FixedProviderEndpoint(base_url)
        self._breaker = breaker
        self._clock = clock or SYSTEM_CLOCK
        self._adapter_enabled = (
            bool(_setting(settings, "RESEARCH_MUSE_GROUNDING_ENABLED", False))
            if adapter_enabled is None
            else bool(adapter_enabled)
        )
        self._api_key = (
            str(_setting(settings, "RESEARCH_META_API_KEY", "") or "").strip()
            if api_key is None
            else str(api_key).strip()
        )
        self._model = str(
            model
            if model is not None
            else _setting(settings, "RESEARCH_META_MODEL", "muse-spark-1.3-contributor") or ""
        )
        budget = (
            _setting(settings, "RESEARCH_MUSE_GROUNDING_MONTHLY_BUDGET_USD", "0.00")
            if monthly_budget_micro_usd is None
            else monthly_budget_micro_usd
        )
        parsed = parse_optional_micro(budget)
        # Cadena vacia = desconocido. Un techo desconocido no autoriza gasto.
        self._budget_micro = 0 if parsed is None else int(parsed)
        self._max_attempts = max(1, int(max_attempts))
        self._phase_timeout = float(phase_timeout)
        self._backoff = float(backoff_seconds)
        self._account = account

    # ------------------------------------------------------------- operacion

    async def query(
        self,
        question: str,
        *,
        research_id: str,
        project_key: str,
        classification: Classification = Classification.PUBLIC,
        requires_currency: bool = False,
        deadline: Deadline | None = None,
        max_searches_estimate: int = 1,
        project_limit_micro_usd: int | None = None,
        job_limit_micro_usd: int | None = None,
    ) -> GroundedResult:
        """Consulta grounded. Nunca llama si el pago esta en cero o falta autorizacion."""
        operation_id = f"{research_id}:muse:grounded:{uuid4().hex[:10]}"
        blocked = self._precondition(classification)
        if blocked is not None:
            return GroundedResult(
                provider=self.provider,
                operation_id=operation_id,
                model=self._model,
                estimated_micro_usd=0,
                reconciled_micro_usd=0,
                charged_micro_usd=0,
                degradations=(blocked.degradation(),),
            )
        if not (question or "").strip():
            raise ValueError("El grounding necesita una pregunta")

        estimate = self._estimate(max_searches_estimate)
        attempts: list[Attempt] = []
        degradations: list[Degradation] = []
        charged_total = 0
        uncertain = False
        payload: dict[str, Any] | None = None

        for index in range(1, self._max_attempts + 1):
            if deadline is not None and deadline.expired:
                degradations.append(
                    DeadlineExceededError(
                        "Deadline agotado antes del intento", provider=self.provider
                    ).degradation()
                )
                break

            reservation = await self._reserve(
                operation_id=operation_id,
                research_id=research_id,
                project_key=project_key,
                amount=estimate,
                index=index,
                project_limit_micro_usd=project_limit_micro_usd,
                job_limit_micro_usd=job_limit_micro_usd,
                degradations=degradations,
            )
            if reservation is _REFUSED:
                break

            try:
                timeout = (
                    deadline.phase_timeout(self._phase_timeout, provider=self.provider)
                    if deadline is not None
                    else self._phase_timeout
                )
            except AcquisitionError as exc:
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, exc.kind))
                degradations.append(exc.degradation())
                break

            try:
                response = await self._send(question, timeout=timeout)
            except httpx.ConnectTimeout as exc:
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, "connect_timeout"))
                if index < self._max_attempts:
                    await self._sleep()
                    continue
                degradations.append(
                    ProviderTransportError(f"Conexion agotada: {exc}", provider=self.provider).degradation()
                )
                break
            except httpx.ConnectError as exc:
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, "connect_error"))
                if index < self._max_attempts:
                    await self._sleep()
                    continue
                degradations.append(
                    ProviderTransportError(f"Fallo de conexion: {exc}", provider=self.provider).degradation()
                )
                break
            except httpx.TimeoutException as exc:
                # Enviada sin respuesta: pudo facturarse. No se reintenta y no es gratis.
                await self._mark_uncertain(reservation)
                attempts.append(self._attempt(operation_id, index, "uncertain", None, "ambiguous_timeout"))
                uncertain = True
                degradations.append(
                    ProviderAmbiguousTimeout(
                        f"Timeout ambiguo tras enviar: {exc}", provider=self.provider
                    ).degradation()
                )
                break
            except httpx.TransportError as exc:
                await self._mark_uncertain(reservation)
                attempts.append(self._attempt(operation_id, index, "uncertain", None, "ambiguous_transport"))
                uncertain = True
                degradations.append(
                    ProviderAmbiguousTimeout(
                        f"Transporte ambiguo tras enviar: {exc}", provider=self.provider
                    ).degradation()
                )
                break
            except httpx.HTTPError as exc:
                await self._mark_uncertain(reservation)
                attempts.append(self._attempt(operation_id, index, "uncertain", None, "ambiguous_http_error"))
                uncertain = True
                degradations.append(
                    ProviderAmbiguousTimeout(
                        f"Error HTTP ambiguo: {exc}", provider=self.provider
                    ).degradation()
                )
                break

            status = response.status_code
            if status in {401, 403}:
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, f"http_{status}"))
                degradations.append(
                    ProviderAuthError(f"Acceso denegado por el proveedor ({status})", provider=self.provider).degradation()
                )
                break
            if status == 402:
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, "http_402"))
                degradations.append(
                    ProviderQuotaExhausted(
                        "El proveedor informa cuota agotada (402)", provider=self.provider
                    ).degradation()
                )
                break
            if status == 429:
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, "http_429"))
                wait = retry_after if retry_after is not None else self._backoff
                if index < self._max_attempts and self._fits(deadline, wait):
                    await self._sleep(wait)
                    continue
                degradations.append(
                    ProviderRateLimited(
                        "Rate limit del proveedor (429)",
                        provider=self.provider,
                        retry_after=retry_after,
                    ).degradation()
                )
                break
            if status >= 500:
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, f"http_{status}"))
                if index < self._max_attempts:
                    await self._sleep()
                    continue
                degradations.append(
                    ProviderServerError(f"El proveedor fallo ({status})", provider=self.provider).degradation()
                )
                break
            if status >= 400:
                await self._release(reservation)
                attempts.append(self._attempt(operation_id, index, "released", 0, f"http_{status}"))
                degradations.append(
                    ProviderHTTPError(f"Respuesta {status} del proveedor", provider=self.provider).degradation()
                )
                break

            try:
                payload = _parse(response)
            except AcquisitionError as exc:
                # La llamada ocurrio: se liquida el estimado y se reporta el parseo.
                await self._commit(reservation, estimate)
                charged_total += estimate
                attempts.append(self._attempt(operation_id, index, "committed", estimate, exc.kind))
                degradations.append(exc.degradation())
                break

            charged, reconciled, uncertain_usage = self._settle_amounts(estimate, payload)
            if uncertain_usage:
                # El proveedor no reporto uso: la reserva sigue incierta y el importe
                # reconciliado es None. Nunca se infiere del numero de resultados.
                await self._mark_uncertain(reservation)
                attempts.append(self._attempt(operation_id, index, "uncertain", None, "usage_unreported"))
                uncertain = True
            else:
                await self._commit(reservation, charged)
                charged_total += charged
                attempts.append(
                    self._attempt(operation_id, index, "committed", charged, "ok", searched=payload.get("searched"))
                )
            break

        result = _build_result(
            payload,
            provider=self.provider,
            operation_id=operation_id,
            model=self._model,
            estimate=estimate,
            determined_charge=charged_total if payload is not None else None,
            uncertain=uncertain,
            attempts=attempts,
            degradations=degradations,
            requires_currency=requires_currency,
        )
        return result

    # ------------------------------------------------------------ internos

    def _precondition(self, classification: Classification) -> AcquisitionError | None:
        if classification is not Classification.PUBLIC:
            # Contributor entrena con prompts y completions. Enviarle datos
            # restringidos o privados es la peor degradacion posible: se declara la
            # falta de ruta compatible en vez de caer a la ruta publica.
            return ModelPolicyUnavailableError(
                "Sin ruta de modelo compatible: grounding publico no admite datos "
                f"{classification.value}; no hay fallback a Contributor",
                provider=self.provider,
            )
        if not self._adapter_enabled:
            return ProviderDisabledError(
                "Grounding de Muse deshabilitado (RESEARCH_MUSE_GROUNDING_ENABLED=false)",
                provider=self.provider,
            )
        if not self._api_key:
            return MissingConfigError("Falta RESEARCH_META_API_KEY", provider=self.provider)
        if self._budget_micro <= 0:
            # Pago cero = cero llamadas. No se "prueba" con una llamada pagada.
            return BudgetRefusedError(
                "Presupuesto pagado de grounding en cero; no se llama al proveedor",
                provider=self.provider,
            )
        if self._ledger is None:
            # Sin ledger no hay contabilidad: no se autoriza gasto que no se puede
            # contabilizar ni reconciliar.
            return BudgetRefusedError(
                "Sin ResearchBudgetLedger no hay contabilidad; no se autoriza una llamada pagada",
                provider=self.provider,
            )
        return None

    def _estimate(self, max_searches_estimate: int) -> int:
        searches = max(1, int(max_searches_estimate))
        return searches * GROUNDING_SEARCH_MICRO_USD

    def _settle_amounts(
        self, estimate: int, payload: dict[str, Any]
    ) -> tuple[int, int | None, bool]:
        """(importe a liquidar, reconciliado, uso incierto) a partir del uso reportado.

        Una busqueda de grounding puede disparar varias busquedas facturables
        internas y el proveedor no permite acotarlas: por eso no se promete un tope
        exacto y el importe sale del uso reportado, no del numero de resultados.
        """
        searches = int(payload.get("searches_performed") or 0)
        token_cost = token_cost_micro(
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            cached_tokens=payload.get("cached_tokens"),
        )
        if token_cost is None and searches == 0:
            return estimate, None, True
        search_cost = searches * GROUNDING_SEARCH_MICRO_USD
        if token_cost is None:
            # Hay busquedas pero no hay contador de tokens: el importe total es
            # incierto (faltan tokens), asi que no se afirma un reconciliado exacto.
            return max(search_cost, 1), None, True
        return search_cost + token_cost, search_cost + token_cost, False

    async def _reserve(
        self,
        *,
        operation_id: str,
        research_id: str,
        project_key: str,
        amount: int,
        index: int,
        project_limit_micro_usd: int | None,
        job_limit_micro_usd: int | None,
        degradations: list[Degradation],
    ) -> Any:
        try:
            decision = await self._ledger.reserve(
                provider=self.provider,
                amount_micro_usd=amount,
                operation_id=f"{operation_id}#{index}",
                project_key=project_key,
                research_id=research_id,
                ceiling_micro_usd=self._budget_micro,
                project_limit_micro_usd=project_limit_micro_usd,
                job_limit_micro_usd=job_limit_micro_usd,
                account=self._account,
            )
        except AcquisitionError:
            raise
        except BudgetError as exc:
            degradations.append(
                BudgetRefusedError(str(exc), provider=self.provider).degradation()
            )
            return _REFUSED
        if not decision.accepted:
            degradations.append(
                BudgetRefusedError(
                    f"Reserva rechazada: {decision.reason or 'sin motivo'}", provider=self.provider
                ).degradation()
            )
            return _REFUSED
        return decision.reservation

    async def _commit(self, reservation: Any, amount: int) -> None:
        if self._ledger is not None and reservation is not None:
            await self._ledger.commit(reservation, charged_micro_usd=int(amount))

    async def _release(self, reservation: Any) -> None:
        if self._ledger is not None and reservation is not None:
            await self._ledger.release(reservation)

    async def _mark_uncertain(self, reservation: Any) -> None:
        if self._ledger is not None and reservation is not None:
            await self._ledger.mark_uncertain(reservation)

    def _attempt(
        self,
        operation_id: str,
        index: int,
        outcome: str,
        charged: int | None,
        detail: str,
        *,
        searched: Any = None,
    ) -> Attempt:
        suffix = "" if searched is None else f";searched={bool(searched)}"
        return Attempt(
            provider=self.provider,
            operation_id=f"{operation_id}#{index}",
            index=index,
            outcome=outcome,
            charged_micro_usd=charged,
            detail=f"{detail}{suffix}",
        )

    async def _sleep(self, seconds: float | None = None) -> None:
        delay = self._backoff if seconds is None else seconds
        if delay > 0:
            await self._clock.sleep(delay)

    def _fits(self, deadline: Deadline | None, delay: float) -> bool:
        # ``allows`` compara contra el tiempo restante: nunca se duerme mas alla del
        # deadline del job para respetar un Retry-After.
        return deadline is None or deadline.allows(delay)

    async def _send(self, question: str, *, timeout: float) -> httpx.Response:
        url = self._endpoint.url(_RESPONSES_PATH)
        body = {
            "model": self._model,
            "input": question,
            "tools": [{"type": "web_search"}],
            "include": ["web_search_call.results"],
        }
        headers = {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        if self._client is not None:
            return await self._client.post(url, headers=headers, json=body, timeout=timeout)
        async with httpx.AsyncClient(timeout=None) as client:
            return await client.post(url, headers=headers, json=body, timeout=timeout)


_REFUSED = object()


# ------------------------------------------------------------------- parseo


def _parse(response: httpx.Response) -> dict[str, Any]:
    """Extrae respuesta, busquedas, citas, fuentes y uso. No propaga el cuerpo crudo."""
    try:
        raw = response.json()
    except ValueError as exc:
        raise ProviderParseError("Muse devolvio un cuerpo no JSON", provider="muse") from exc
    if not isinstance(raw, dict):
        raise ProviderParseError("Muse devolvio un JSON que no es objeto", provider="muse")

    answer_parts: list[str] = []
    citations: list[GroundingCitation] = []
    sources: list[GroundingSource] = []
    searches = 0
    seen_sources: set[str] = set()
    duplicates = 0

    output = raw.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "web_search_call":
                # Se cuenta como busqueda **real** solo si el proveedor la reporta
                # con accion de busqueda o estado completado: habilitar la
                # herramienta no acredita que se haya usado.
                if _is_real_search(item):
                    searches += 1
                for entry in _iter_results(item):
                    url = entry.get("url")
                    if not isinstance(url, str) or not url:
                        continue
                    if url in seen_sources:
                        duplicates += 1
                        continue
                    seen_sources.add(url)
                    title = entry.get("title")
                    sources.append(
                        GroundingSource(url=url, title=title if isinstance(title, str) else None)
                    )
                continue
            if item_type in {"message", "output_text"}:
                text, found = _extract_message(item)
                if text:
                    answer_parts.append(text)
                citations.extend(found)

    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    return {
        "answer": "".join(answer_parts).strip(),
        "citations": citations,
        "sources": sources,
        "duplicate_sources": duplicates,
        "searches_performed": searches,
        "input_tokens": _int_or_none(usage.get("input_tokens")),
        "output_tokens": _int_or_none(usage.get("output_tokens")),
        "cached_tokens": _int_or_none(details.get("cached_tokens")),
        "model": raw.get("model") if isinstance(raw.get("model"), str) else None,
    }


def _is_real_search(item: dict[str, Any]) -> bool:
    action = item.get("action")
    if isinstance(action, dict) and action.get("type") in {"search", "web_search"}:
        return True
    status = item.get("status")
    if isinstance(status, str) and status.lower() in {"completed", "searched"}:
        return True
    # Un item `web_search_call` sin accion ni estado es la herramienta habilitada,
    # no una busqueda demostrada.
    return False


def _iter_results(item: dict[str, Any]):
    results = item.get("results")
    if isinstance(results, list):
        for entry in results:
            if isinstance(entry, dict):
                yield entry


def _extract_message(item: dict[str, Any]) -> tuple[str, list[GroundingCitation]]:
    parts: list[str] = []
    citations: list[GroundingCitation] = []
    content = item.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
            annotations = block.get("annotations")
            if not isinstance(annotations, list):
                continue
            for annotation in annotations:
                if not isinstance(annotation, dict):
                    continue
                if annotation.get("type") != "url_citation":
                    continue
                url = annotation.get("url")
                if not isinstance(url, str) or not url:
                    continue
                citations.append(
                    GroundingCitation(
                        url=url,
                        title=annotation.get("title") if isinstance(annotation.get("title"), str) else None,
                        cited_text=annotation.get("cited_text") if isinstance(annotation.get("cited_text"), str) else None,
                        start_index=_int_or_none(annotation.get("start_index")),
                        end_index=_int_or_none(annotation.get("end_index")),
                    )
                )
    top = item.get("text")
    if isinstance(top, str) and top:
        parts.append(top)
    return "".join(parts), citations


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _build_result(
    payload: dict[str, Any] | None,
    *,
    provider: str,
    operation_id: str,
    model: str,
    estimate: int,
    determined_charge: int | None,
    uncertain: bool,
    attempts: list[Attempt],
    degradations: list[Degradation],
    requires_currency: bool,
) -> GroundedResult:
    if payload is None:
        return GroundedResult(
            provider=provider,
            operation_id=operation_id,
            model=model,
            estimated_micro_usd=estimate,
            reconciled_micro_usd=None if uncertain else determined_charge,
            charged_micro_usd=None if uncertain else (determined_charge or 0),
            attempts=tuple(attempts),
            degradations=tuple(degradations),
        )

    sources = tuple(payload.get("sources") or ())
    citations = tuple(payload.get("citations") or ())
    searched = bool(payload.get("searches_performed"))
    warnings: list[str] = []
    if not searched:
        warnings.append(
            "La herramienta de busqueda estaba habilitada pero no se observo ninguna busqueda"
        )
    if requires_currency and not searched:
        warnings.append(
            "Se exigia actualidad y no hay constancia de busqueda: la respuesta no puede "
            "afirmar cobertura reciente"
        )
    if searched and not sources and not citations:
        warnings.append("Hubo busqueda pero no llegaron fuentes ni citas")
    if any(c.url for c in citations) and not sources:
        warnings.append(
            "Hay citas sin fuentes observadas: tratalas como evidencia derivada, no como lectura"
        )

    return GroundedResult(
        provider=provider,
        operation_id=operation_id,
        model=str(payload.get("model") or model),
        answer=str(payload.get("answer") or ""),
        tool_enabled=True,
        searched=searched,
        searches_performed=int(payload.get("searches_performed") or 0),
        citations=citations,
        sources=sources,
        duplicate_sources=int(payload.get("duplicate_sources") or 0),
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        cached_tokens=payload.get("cached_tokens"),
        estimated_micro_usd=estimate,
        reconciled_micro_usd=None if uncertain else determined_charge,
        charged_micro_usd=None if uncertain else (determined_charge or 0),
        hard_cap=False,
        attempts=tuple(attempts),
        degradations=tuple(degradations),
        warnings=tuple(warnings),
    )
