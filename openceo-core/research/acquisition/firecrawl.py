"""Cliente Firecrawl (search y scrape) — adaptador **opcional** (TASK-2026-00060).

Opcionalidad
============
Firecrawl puede estar deshabilitado (sin clave, sin cuota o por politica) **sin
bloquear la entrega A**: HTTP/RSS siguen cubriendo la lectura publica. Un adaptador
apagado produce degradacion ``disabled``/``missing_config``, nunca una excepcion ni
un navegador ficticio. No se implementa aqui ruta de navegador porque no existe un
worker aislado de verdad (documento del epico, seccion 6.2): inventarlo daria una
capacidad que no se puede ejecutar.

Tarifa reservada
================
Search consume **2 creditos por bloque de hasta 10 resultados**; scrape basico, **1
credito por pagina**. Si se piden formatos que usan extraccion con LLM, se anaden
sus creditos. La reserva se calcula sobre las opciones concretas de la llamada, no
sobre un maximo teorico, y los creditos quedan registrados aunque el plan gratuito
no tenga coste monetario.
"""

from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

import httpx

from research.contracts import (
    AccessMethod,
    Classification,
    ResearchEvidence,
    RetentionPolicy,
    SourceType,
)
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
    parse_datetime,
    run_provider_call,
    safe_discovery_item,
)
from .http_reader import ReadOutcome, normalize_text

logger = logging.getLogger(__name__)

__all__ = [
    "FIRECRAWL_BASE_URL",
    "FIRECRAWL_SCRAPE_BASIC_CREDITS",
    "FIRECRAWL_SEARCH_BLOCK_SIZE",
    "FIRECRAWL_SEARCH_CREDITS_PER_BLOCK",
    "FirecrawlClient",
    "firecrawl_scrape_credits",
    "firecrawl_search_credits",
]

FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"
_SEARCH_PATH = "/v2/search"
_SCRAPE_PATH = "/v2/scrape"
FIRECRAWL_SEARCH_BLOCK_SIZE = 10
FIRECRAWL_SEARCH_CREDITS_PER_BLOCK = 2
FIRECRAWL_SCRAPE_BASIC_CREDITS = 1
FIRECRAWL_LLM_FORMAT_EXTRA_CREDITS = 4
_LLM_FORMATS = frozenset({"json", "extract", "llm-extraction", "changeTracking"})


def firecrawl_search_credits(limit: int) -> int:
    """2 creditos por cada bloque **iniciado** de hasta 10 resultados."""
    blocks = max(1, math.ceil(max(1, int(limit)) / FIRECRAWL_SEARCH_BLOCK_SIZE))
    return blocks * FIRECRAWL_SEARCH_CREDITS_PER_BLOCK


def firecrawl_scrape_credits(formats: list[str] | tuple[str, ...] | None = None) -> int:
    """1 credito por pagina basica + 4 si la extraccion usa LLM."""
    credits = FIRECRAWL_SCRAPE_BASIC_CREDITS
    if formats and any(str(fmt).lower() in _LLM_FORMATS for fmt in formats):
        credits += FIRECRAWL_LLM_FORMAT_EXTRA_CREDITS
    return credits


def _setting(settings: Any, name: str, default: Any) -> Any:
    if settings is None:
        return default
    return getattr(settings, name, default)


class FirecrawlClient:
    """Descubrimiento y extraccion con Firecrawl, siempre opcional."""

    provider = "firecrawl"

    def __init__(
        self,
        *,
        settings: Any = None,
        http_client: httpx.AsyncClient | None = None,
        ledger: Any | None = None,
        endpoint: FixedProviderEndpoint | None = None,
        breaker: CircuitBreaker | None = None,
        clock: Clock | None = None,
        wall_clock: Callable[[], datetime] | None = None,
        adapter_enabled: bool | None = None,
        micro_per_credit: int = 0,
        max_attempts: int = 3,
        phase_timeout: float = 10.0,
        backoff_seconds: float = 0.5,
        account: str = "default",
        credit_budget: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client = http_client
        self._ledger = ledger
        self._endpoint = endpoint or FixedProviderEndpoint(FIRECRAWL_BASE_URL)
        self._breaker = breaker
        self._clock = clock or SYSTEM_CLOCK
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._adapter_enabled = (
            bool(_setting(settings, "RESEARCH_FIRECRAWL_ENABLED", True))
            if adapter_enabled is None
            else bool(adapter_enabled)
        )
        self._api_key = str(_setting(settings, "RESEARCH_FIRECRAWL_API_KEY", "") or "").strip()
        # Plan gratuito sin recargas: coste monetario real 0 mientras no se autorice
        # compra. Los creditos se reservan igual y quedan registrados.
        self._micro_per_credit = int(micro_per_credit)
        self._max_attempts = int(max_attempts)
        self._phase_timeout = float(phase_timeout)
        self._backoff = float(backoff_seconds)
        self._account = account
        self._credit_budget = credit_budget

    # ------------------------------------------------------------ operaciones

    async def search(
        self,
        query: str,
        *,
        research_id: str,
        project_key: str,
        limit: int = 10,
        deadline: Deadline | None = None,
    ) -> DiscoveryResult:
        """Descubrimiento. Reserva los 2 creditos por bloque realmente pedidos."""
        operation_id = f"{research_id}:firecrawl:search:{uuid4().hex[:10]}"
        blocked = self._precondition()
        if blocked is not None:
            return DiscoveryResult(
                provider=self.provider,
                degradations=(blocked.degradation(),),
                operation_id=operation_id,
            )
        credits = firecrawl_search_credits(limit)
        url = self._endpoint.url(_SEARCH_PATH)
        headers = self._auth_headers()
        body = {"query": query, "limit": max(1, min(int(limit), 100))}
        cost = CostEstimate(
            amount_micro_usd=credits * self._micro_per_credit,
            credits=credits,
            detail=f"firecrawl:search limit={limit}",
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
            parse=_parse_search,
            deadline=deadline,
            ledger=self._ledger,
            # Sin precio por credito no se inventa techo monetario; si algun dia
            # se configura, el ledger exige techo conocido (conservador).
            ceiling_micro_usd=0 if self._micro_per_credit == 0 else None,
            breaker=self._breaker,
            clock=self._clock,
            phase_timeout=self._phase_timeout,
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff,
            account=self._account,
        )
        return discovery_from_call(call, provider=self.provider, operation_id=operation_id)

    async def scrape(
        self,
        url: str,
        *,
        research_id: str,
        project_key: str,
        owner_id: str | None = None,
        formats: tuple[str, ...] = ("markdown",),
        source_type: SourceType = SourceType.UNKNOWN,
        retention_policy: RetentionPolicy = RetentionPolicy.PUBLIC_7D,
        classification: Classification = Classification.PUBLIC,
        previous: Any | None = None,
        deadline: Deadline | None = None,
    ) -> ReadOutcome:
        """Extraccion de una pagina. 1 credito basico; +4 si se pide LLM."""
        operation_id = f"{research_id}:firecrawl:scrape:{uuid4().hex[:10]}"
        blocked = self._precondition()
        if blocked is not None:
            return ReadOutcome(
                url=url,
                final_url=url,
                access_method=AccessMethod.FIRECRAWL,
                evidence=None,
                degradations=(blocked.degradation(),),
                last_checked_at=self._wall_clock(),
            )
        credits = firecrawl_scrape_credits(formats)
        endpoint_url = self._endpoint.url(_SCRAPE_PATH)
        headers = self._auth_headers()
        body = {"url": url, "formats": list(formats), "parsers": [], "proxy": "basic",
                "storeInCache": False, "maxAge": 0}
        cost = CostEstimate(
            amount_micro_usd=credits * self._micro_per_credit,
            credits=credits,
            detail=f"firecrawl:scrape formats={','.join(formats)}",
        )

        async def request(timeout: float) -> httpx.Response:
            return await self._send(endpoint_url, headers=headers, json_body=body, timeout=timeout)

        call = await run_provider_call(
            provider=self.provider,
            operation_id=operation_id,
            research_id=research_id,
            project_key=project_key,
            cost=cost,
            request=request,
            parse=_parse_scrape,
            deadline=deadline,
            ledger=self._ledger,
            # Sin precio por credito no se inventa techo monetario; si algun dia
            # se configura, el ledger exige techo conocido (conservador).
            ceiling_micro_usd=0 if self._micro_per_credit == 0 else None,
            breaker=self._breaker,
            clock=self._clock,
            phase_timeout=self._phase_timeout,
            max_attempts=self._max_attempts,
            backoff_seconds=self._backoff,
            account=self._account,
        )
        return self._scrape_outcome(
            call,
            url=url,
            project_key=project_key,
            owner_id=owner_id,
            source_type=source_type,
            retention_policy=retention_policy,
            classification=classification,
        )

    # ------------------------------------------------------------- internos

    def _precondition(self) -> AcquisitionError | None:
        if not self._adapter_enabled:
            return ProviderDisabledError("Adaptador Firecrawl deshabilitado", provider=self.provider)
        if not self._api_key:
            return MissingConfigError(
                "Falta RESEARCH_FIRECRAWL_API_KEY", provider=self.provider
            )
        return None

    def _auth_headers(self) -> dict[str, str]:
        return {"accept": "application/json", "authorization": f"Bearer {self._api_key}"}

    async def _send(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any],
        timeout: float,
    ) -> httpx.Response:
        reservation = None
        if self._credit_budget is not None:
            credits = (firecrawl_scrape_credits(json_body.get("formats")) if url.endswith(_SCRAPE_PATH)
                       else firecrawl_search_credits(json_body.get("limit", 10)))
            reservation = await self._credit_budget.reserve(credits)
        if self._client is not None:
            response = await self._client.post(url, headers=headers, json=json_body, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False) as client:
                async with client.stream("POST", url, headers=headers, json=json_body, timeout=timeout) as incoming:
                    body = bytearray()
                    async for chunk in incoming.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 1_000_000:
                            from .base import ProviderTooLarge
                            raise ProviderTooLarge("Respuesta Firecrawl supera el limite", provider=self.provider)
                    decoded_headers = {k: v for k, v in incoming.headers.items()
                                       if k.lower() not in {"content-encoding", "content-length"}}
                    response = httpx.Response(incoming.status_code, headers=decoded_headers,
                                              content=bytes(body), request=incoming.request)
        if reservation:
            try:
                actual = response.json().get("creditsUsed")
            except (ValueError, AttributeError):
                actual = None
            await self._credit_budget.settle(reservation, actual)
        return response

    def _scrape_outcome(
        self,
        call: Any,
        *,
        url: str,
        project_key: str,
        owner_id: str | None,
        source_type: SourceType,
        retention_policy: RetentionPolicy,
        classification: Classification,
    ) -> ReadOutcome:
        checked_at = self._wall_clock()
        if call.payload is None:
            return ReadOutcome(
                url=url,
                final_url=url,
                access_method=AccessMethod.FIRECRAWL,
                evidence=None,
                degradations=call.degradations,
                last_checked_at=checked_at,
            )
        markdown = normalize_text(str(call.payload.get("markdown") or ""))
        metadata = call.payload.get("metadata") or {}
        title = metadata.get("title") if isinstance(metadata, dict) else None
        published_at = parse_datetime(
            (metadata.get("publishedTime") if isinstance(metadata, dict) else None)
        )
        final_url = (
            metadata.get("sourceURL")
            if isinstance(metadata, dict) and metadata.get("sourceURL")
            else url
        )
        content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest() if markdown else None
        evidence = ResearchEvidence(
            id=f"ev_{content_hash[:16]}" if content_hash else f"ev_{uuid4().hex[:16]}",
            url=url,
            access_method=AccessMethod.FIRECRAWL,
            visibility=classification,
            source_type=source_type,
            title=title,
            published_at=published_at,
            observed_at=checked_at,
            content_hash=content_hash,
            retention_policy=retention_policy,
            project_key=project_key,
            owner_id=owner_id,
        )
        return ReadOutcome(
            url=url,
            final_url=str(final_url),
            access_method=AccessMethod.FIRECRAWL,
            evidence=evidence,
            text=markdown,
            title=title,
            published_at=published_at,
            not_modified=False,
            last_checked_at=checked_at,
            observed_at=checked_at,
            content_hash=content_hash,
            degradations=call.degradations,
        )


def _parse_search(response: httpx.Response) -> dict[str, Any]:
    try:
        raw = response.json()
    except ValueError as exc:
        raise ProviderParseError("Firecrawl devolvio un cuerpo no JSON", provider="firecrawl") from exc
    if not isinstance(raw, dict):
        raise ProviderParseError("Firecrawl devolvio un JSON que no es objeto", provider="firecrawl")
    if raw.get("success") is False:
        raise ProviderParseError("Firecrawl rechazo la busqueda", provider="firecrawl")
    data = raw.get("data")
    results: Any = None
    if isinstance(data, dict):
        results = data.get("web") or data.get("results")
    elif isinstance(data, list):
        results = data
    if results is None:
        results = raw.get("web")
    if results is None:
        return {"items": []}
    if not isinstance(results, list):
        raise ProviderParseError("La lista de Firecrawl no es una lista", provider="firecrawl")
    items: list[DiscoveryItem] = []
    for rank, entry in enumerate(results, start=1):
        if not isinstance(entry, dict):
            continue
        item = safe_discovery_item(entry.get("url"), provider="firecrawl", rank=rank)
        if item is not None:
            items.append(item)
    return {"items": items}


def _parse_scrape(response: httpx.Response) -> dict[str, Any]:
    try:
        raw = response.json()
    except ValueError as exc:
        raise ProviderParseError("Firecrawl devolvio un cuerpo no JSON", provider="firecrawl") from exc
    if not isinstance(raw, dict):
        raise ProviderParseError("Firecrawl devolvio un JSON que no es objeto", provider="firecrawl")
    if raw.get("success") is False:
        raise ProviderParseError("Firecrawl rechazo la extraccion", provider="firecrawl")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise ProviderParseError("La extraccion de Firecrawl no trae datos", provider="firecrawl")
    return {
        "markdown": data.get("markdown") or data.get("content") or "",
        "metadata": data.get("metadata") or {},
    }
