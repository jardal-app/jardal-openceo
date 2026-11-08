"""Cliente Serper (busqueda web y noticias) para research (TASK-2026-00060).

Serper es el **complemento** de Brave mientras tenga saldo, no un fallback
automatico. El saldo inicial publicado (2.500 consultas) es una referencia fechada
y no se presume renovable; ``RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES`` vacio
significa **desconocido**, y un saldo no medido no autoriza gasto: la operacion se
degrada con ``budget_refused`` en vez de llamar a ciegas.

Igual que Brave, solo se conservan URL y posicion. El snippet es transitorio.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx

from research.policies import FixedProviderEndpoint

from .base import (
    AcquisitionError,
    CircuitBreaker,
    Clock,
    CostEstimate,
    Deadline,
    DiscoveryItem,
    DiscoveryResult,
    MissingConfigError,
    ProviderDisabledError,
    ProviderParseError,
    SYSTEM_CLOCK,
    discovery_from_call,
    parse_optional_int,
    run_provider_call,
    safe_discovery_item,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SERPER_BASE_URL",
    "SERPER_QUERY_MICRO_USD",
    "SerperClient",
]

SERPER_BASE_URL = "https://google.serper.dev"
_SEARCH_PATH = "/search"
_NEWS_PATH = "/news"
# Paquete Starter publicado: $50 / 50.000 creditos => $0,001 por consulta.
SERPER_QUERY_MICRO_USD = 1_000


def _setting(settings: Any, name: str, default: Any) -> Any:
    if settings is None:
        return default
    return getattr(settings, name, default)


class SerperClient:
    """Descubrimiento con Serper. Saldo desconocido = degradacion, no gasto."""

    provider = "serper"

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
        max_attempts: int = 3,
        phase_timeout: float = 10.0,
        backoff_seconds: float = 0.5,
        account: str = "default",
        credit_budget: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client = http_client
        self._ledger = ledger
        self._endpoint = endpoint or FixedProviderEndpoint(SERPER_BASE_URL)
        self._breaker = breaker
        self._clock = clock or SYSTEM_CLOCK
        self._adapter_enabled = (
            bool(_setting(settings, "RESEARCH_SERPER_ENABLED", True))
            if adapter_enabled is None
            else bool(adapter_enabled)
        )
        self._api_key = str(_setting(settings, "RESEARCH_SERPER_API_KEY", "") or "").strip()
        # El saldo se declara en **consultas**; el ledger razona en micro-USD. Un
        # valor vacio es desconocido (None), nunca cero.
        self._credit_queries = parse_optional_int(
            _setting(settings, "RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES", "")
        )
        self._ceiling = (
            self._credit_queries * SERPER_QUERY_MICRO_USD
            if self._credit_queries is not None
            else None
        )
        self._max_attempts = int(max_attempts)
        self._phase_timeout = float(phase_timeout)
        self._backoff = float(backoff_seconds)
        self._account = account
        self._credit_budget = credit_budget

    # ------------------------------------------------------------ operaciones

    async def search_web(
        self,
        query: str,
        *,
        research_id: str,
        project_key: str,
        num: int = 10,
        gl: str | None = None,
        hl: str | None = None,
        deadline: Deadline | None = None,
    ) -> DiscoveryResult:
        return await self._search(
            _SEARCH_PATH,
            form="web",
            query=query,
            research_id=research_id,
            project_key=project_key,
            num=num,
            gl=gl,
            hl=hl,
            deadline=deadline,
        )

    async def search_news(
        self,
        query: str,
        *,
        research_id: str,
        project_key: str,
        num: int = 10,
        gl: str | None = None,
        hl: str | None = None,
        deadline: Deadline | None = None,
    ) -> DiscoveryResult:
        return await self._search(
            _NEWS_PATH,
            form="news",
            query=query,
            research_id=research_id,
            project_key=project_key,
            num=num,
            gl=gl,
            hl=hl,
            deadline=deadline,
        )

    async def _search(
        self,
        path: str,
        *,
        form: str,
        query: str,
        research_id: str,
        project_key: str,
        num: int,
        gl: str | None,
        hl: str | None,
        deadline: Deadline | None,
    ) -> DiscoveryResult:
        operation_id = f"{research_id}:serper:{form}:{uuid4().hex[:10]}"
        blocked = self._precondition()
        if blocked is not None:
            return DiscoveryResult(
                provider=self.provider,
                degradations=(blocked.degradation(),),
                operation_id=operation_id,
            )

        body: dict[str, Any] = {
            "q": query,
            "num": max(1, min(int(num), 100)),
        }
        if gl:
            body["gl"] = gl
        if hl:
            body["hl"] = hl
        url = self._endpoint.url(path)
        headers = {"accept": "application/json", "x-api-key": self._api_key}
        cost = CostEstimate(
            amount_micro_usd=SERPER_QUERY_MICRO_USD,
            credits=1,
            detail=f"serper:{form}",
        )

        async def request(timeout: float) -> httpx.Response:
            return await self._send(url, headers=headers, json_body=body, timeout=timeout)

        call = await run_provider_call(
            provider=self.provider,
            operation_id=operation_id,
            research_id=research_id,
            project_key=project_key,
            cost=cost,
            request=request,
            parse=lambda response: _parse(response, form=form),
            deadline=deadline,
            ledger=self._ledger,
            ceiling_micro_usd=self._ceiling,
            # Un saldo de consultas no medido no autoriza gasto.
            allow_unknown_ceiling=False,
            breaker=self._breaker,
            clock=self._clock,
            phase_timeout=self._phase_timeout,
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff,
            account=self._account,
        )
        return discovery_from_call(call, provider=self.provider, operation_id=operation_id)

    # ------------------------------------------------------------- internos

    def _precondition(self) -> AcquisitionError | None:
        if not self._adapter_enabled:
            return ProviderDisabledError("Adaptador Serper deshabilitado", provider=self.provider)
        if not self._api_key:
            return MissingConfigError("Falta RESEARCH_SERPER_API_KEY", provider=self.provider)
        return None

    async def _send(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout: float,
    ) -> httpx.Response:
        reservation = await self._credit_budget.reserve(1) if self._credit_budget is not None else None
        if self._client is not None:
            response = await self._client.post(url, headers=headers, json=json_body, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False) as client:
                response = await client.post(url, headers=headers, json=json_body, timeout=timeout)
        if reservation is not None:
            try:
                actual = response.json().get("credits")
            except (ValueError, AttributeError):
                actual = None
            await self._credit_budget.settle(reservation, actual)
        return response


def _parse(response: httpx.Response, *, form: str) -> dict[str, Any]:
    try:
        raw = response.json()
    except ValueError as exc:
        raise ProviderParseError("Serper devolvio un cuerpo no JSON", provider="serper") from exc
    if not isinstance(raw, dict):
        raise ProviderParseError("Serper devolvio un JSON que no es objeto", provider="serper")
    key = "news" if form == "news" else "organic"
    results = raw.get(key)
    if results is None:
        return {"items": []}
    if not isinstance(results, list):
        raise ProviderParseError(f"El bloque {key} de Serper no es una lista", provider="serper")
    items: list[DiscoveryItem] = []
    for rank, entry in enumerate(results, start=1):
        if not isinstance(entry, dict):
            continue
        item = safe_discovery_item(entry.get("link"), provider="serper", rank=rank)
        if item is not None:
            items.append(item)
    return {"items": items}
