"""Cliente Brave Search (Web y News) para adquisicion de research (TASK-2026-00060).

Retencion y transitoriedad
==========================
Las condiciones de Brave restringen almacenar/cachear sus resultados y prohiben
usarlos para entrenar modelos sin derechos adicionales (documento del epico,
seccion 11.2). Por eso este cliente **solo** conserva de cada resultado la URL y su
posicion: el titulo, la descripcion/snippet y el resto del SERP viven en variables
locales durante el parseo y se descartan. La operacion devuelve ``DiscoveryItem``
sin snippet y ``DiscoveryResult.to_payload()`` es una lista blanca que no incluye el
SERP. La evidencia final se construye abriendo la publicacion original.

Operaciones cerradas: ``search_web`` y ``search_news``. No existe un metodo generico
que acepte ruta ni host: el ``FixedProviderEndpoint`` fija la base y el modelo solo
puede aportar parametros de consulta.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx

from research.money import parse_optional_micro
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
    run_provider_call,
    safe_discovery_item,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BRAVE_BASE_URL",
    "BRAVE_SEARCH_MICRO_USD",
    "BraveClient",
]

BRAVE_BASE_URL = "https://api.search.brave.com"
_WEB_PATH = "/res/v1/web/search"
_NEWS_PATH = "/res/v1/news/search"
# $5 / 1.000 requests publicados en la revision del epico (seccion 5).
BRAVE_SEARCH_MICRO_USD = 5_000
_MAX_COUNT = 20

# Campos del SERP que se descartan de forma explicita. Se documenta la lista para
# que una revision futura note si alguien intenta conservarlos "por comodidad".
_SERP_TRANSIENT_FIELDS = ("title", "description", "snippet", "meta_url", "profile", "thumbnail")


def _setting(settings: Any, name: str, default: Any) -> Any:
    if settings is None:
        return default
    return getattr(settings, name, default)


class BraveClient:
    """Descubrimiento con Brave. Sin clave produce ``missing_config``, no excepcion."""

    provider = "brave"

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
    ) -> None:
        self._settings = settings
        self._client = http_client
        self._ledger = ledger
        self._endpoint = endpoint or FixedProviderEndpoint(BRAVE_BASE_URL)
        self._breaker = breaker
        self._clock = clock or SYSTEM_CLOCK
        self._adapter_enabled = (
            bool(_setting(settings, "RESEARCH_BRAVE_ENABLED", True))
            if adapter_enabled is None
            else bool(adapter_enabled)
        )
        self._api_key = str(_setting(settings, "RESEARCH_BRAVE_API_KEY", "") or "").strip()
        self._ceiling = parse_optional_micro(
            _setting(settings, "RESEARCH_BRAVE_ACCOUNT_CEILING_USD", "")
        )
        self._max_attempts = int(max_attempts)
        self._phase_timeout = float(phase_timeout)
        self._backoff = float(backoff_seconds)
        self._account = account

    # ------------------------------------------------------------ operaciones

    async def search_web(
        self,
        query: str,
        *,
        research_id: str,
        project_key: str,
        count: int = 10,
        country: str | None = "us",
        search_lang: str | None = "en",
        freshness: str | None = None,
        offset: int = 0,
        deadline: Deadline | None = None,
    ) -> DiscoveryResult:
        """Busqueda web. Devuelve candidatos sin snippet."""
        return await self._search(
            _WEB_PATH,
            form="web",
            query=query,
            research_id=research_id,
            project_key=project_key,
            count=count,
            country=country,
            search_lang=search_lang,
            freshness=freshness,
            offset=offset,
            deadline=deadline,
        )

    async def search_news(
        self,
        query: str,
        *,
        research_id: str,
        project_key: str,
        count: int = 10,
        country: str | None = "us",
        search_lang: str | None = "en",
        freshness: str | None = None,
        offset: int = 0,
        deadline: Deadline | None = None,
    ) -> DiscoveryResult:
        """Busqueda de noticias. Misma retencion transitoria que la web."""
        return await self._search(
            _NEWS_PATH,
            form="news",
            query=query,
            research_id=research_id,
            project_key=project_key,
            count=count,
            country=country,
            search_lang=search_lang,
            freshness=freshness,
            offset=offset,
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
        count: int,
        country: str | None,
        search_lang: str | None,
        freshness: str | None,
        offset: int,
        deadline: Deadline | None,
    ) -> DiscoveryResult:
        operation_id = f"{research_id}:brave:{form}:{uuid4().hex[:10]}"
        blocked = self._precondition()
        if blocked is not None:
            return DiscoveryResult(
                provider=self.provider,
                degradations=(blocked.degradation(),),
                operation_id=operation_id,
            )

        params = {
            "q": query,
            "count": max(1, min(int(count), _MAX_COUNT)),
            "country": country,
            "search_lang": search_lang,
            "freshness": freshness,
            "offset": int(offset) or None,
        }
        url = self._endpoint.url(path, params)
        headers = {
            "accept": "application/json",
            "x-subscription-token": self._api_key,
        }
        cost = CostEstimate(
            amount_micro_usd=BRAVE_SEARCH_MICRO_USD,
            credits=1,
            detail=f"brave:{form}",
        )

        async def request(timeout: float) -> httpx.Response:
            return await self._send("GET", url, headers=headers, json_body=None, timeout=timeout)

        call = await run_provider_call(
            provider=self.provider,
            operation_id=operation_id,
            research_id=research_id,
            project_key=project_key,
            cost=cost,
            request=request,
            parse=_parse,
            deadline=deadline,
            ledger=self._ledger,
            ceiling_micro_usd=self._ceiling,
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
        """Motivo de degradacion antes de tocar la red. ``None`` = se puede buscar."""
        if not self._adapter_enabled:
            return ProviderDisabledError("Adaptador Brave deshabilitado", provider=self.provider)
        if not self._api_key:
            # Sin clave no se llama y, sobre todo, no se cae a otro proveedor pagado.
            return MissingConfigError(
                "Falta RESEARCH_BRAVE_API_KEY", provider=self.provider
            )
        return None

    async def _send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None,
        timeout: float,
    ) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(
                method, url, headers=headers, json=json_body, timeout=timeout
            )
        async with httpx.AsyncClient(timeout=None) as client:
            return await client.request(
                method, url, headers=headers, json=json_body, timeout=timeout
            )


def _parse(response: httpx.Response) -> dict[str, Any]:
    """Extrae **solo** URLs del SERP. Todo lo demas se descarta aqui mismo."""
    try:
        raw = response.json()
    except ValueError as exc:
        raise ProviderParseError("Brave devolvio un cuerpo no JSON", provider="brave") from exc
    if not isinstance(raw, dict):
        raise ProviderParseError("Brave devolvio un JSON que no es objeto", provider="brave")

    results: Any = None
    web = raw.get("web")
    if isinstance(web, dict):
        results = web.get("results")
    if results is None:
        results = raw.get("results")
    if results is None:
        return {"items": []}
    if not isinstance(results, list):
        raise ProviderParseError("La lista de resultados de Brave no es una lista", provider="brave")

    items: list[DiscoveryItem] = []
    for rank, entry in enumerate(results, start=1):
        if not isinstance(entry, dict):
            continue
        item = safe_discovery_item(entry.get("url"), provider="brave", rank=rank)
        if item is not None:
            items.append(item)
    # El diccionario `raw` (SERP completo) no se propaga: el payload de salida solo
    # contiene los items minimos.
    raw = None  # noqa: F841 - descarte explicito y visible para la revision
    return {"items": items}
