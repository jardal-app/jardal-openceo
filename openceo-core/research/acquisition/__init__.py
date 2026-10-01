"""Adquisicion de research: clientes de proveedor y lectura HTTP/RSS
(TASK-2026-00060).

Operaciones cerradas
====================
* ``BraveClient``     — ``search_web`` / ``search_news``.
* ``SerperClient``    — ``search_web`` / ``search_news``.
* ``FirecrawlClient`` — ``search`` / ``scrape`` (adaptador **opcional**).
* ``HttpReader``      — ``read`` (unica ruta de lectura HTTP publica).
* ``RssReader``       — ``read_feed`` / ``open_entries``.

Reglas transversales
====================
* Toda lectura HTTP/RSS pasa por el egress de ``research/policies.py``.
* Cada intento reserva contra ``ResearchBudgetLedger`` y se liquida; el timeout
  ambiguo queda ``uncertain``, nunca gratis.
* Los fallos esperables se devuelven como ``degradations`` estructuradas; una
  ejecucion sin claves no lanza excepcion ni cae a un proveedor pagado.
* El SERP de Brave/Serper/Firecrawl es transitorio: la evidencia se construye
  abriendo la publicacion original.
"""

from __future__ import annotations

from .base import (
    AcquisitionError,
    Attempt,
    BudgetRefusedError,
    CircuitBreaker,
    CircuitOpenError,
    Clock,
    CostEstimate,
    Deadline,
    DeadlineExceededError,
    Degradation,
    DestinationBlockedError,
    DiscoveryItem,
    DiscoveryResult,
    MissingConfigError,
    ModelPolicyUnavailableError,
    ProviderAmbiguousTimeout,
    ProviderAuthError,
    ProviderCallResult,
    ProviderDisabledError,
    ProviderHTTPError,
    ProviderParseError,
    ProviderProtocolError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTooLarge,
    ProviderTransportError,
    ProviderUnsupportedMedia,
    SystemClock,
    discovery_from_call,
    parse_datetime,
    parse_optional_int,
    parse_retry_after,
    run_provider_call,
    safe_discovery_item,
)
from .brave import BRAVE_BASE_URL, BRAVE_SEARCH_MICRO_USD, BraveClient
from .firecrawl import (
    FIRECRAWL_BASE_URL,
    FIRECRAWL_SCRAPE_BASIC_CREDITS,
    FIRECRAWL_SEARCH_BLOCK_SIZE,
    FIRECRAWL_SEARCH_CREDITS_PER_BLOCK,
    FirecrawlClient,
    firecrawl_scrape_credits,
    firecrawl_search_credits,
)
from .http_reader import (
    DEFAULT_HTTP_LIMITS,
    HttpReader,
    PreviousReading,
    ReadOutcome,
    normalize_text,
    open_publications,
)
from .rss import FeedEntry, FeedOutcome, RssReader, assert_safe_xml, parse_feed
from .grounded import (
    GROUNDING_SEARCH_MICRO_USD,
    GroundedResult,
    GroundingCitation,
    GroundingClient,
    GroundingSource,
    token_cost_micro,
)
from .serper import SERPER_BASE_URL, SERPER_QUERY_MICRO_USD, SerperClient

__all__ = [
    "AcquisitionError",
    "Attempt",
    "BRAVE_BASE_URL",
    "BRAVE_SEARCH_MICRO_USD",
    "BraveClient",
    "BudgetRefusedError",
    "CircuitBreaker",
    "CircuitOpenError",
    "Clock",
    "CostEstimate",
    "DEFAULT_HTTP_LIMITS",
    "Deadline",
    "DeadlineExceededError",
    "Degradation",
    "DestinationBlockedError",
    "DiscoveryItem",
    "DiscoveryResult",
    "FIRECRAWL_BASE_URL",
    "FIRECRAWL_SCRAPE_BASIC_CREDITS",
    "FIRECRAWL_SEARCH_BLOCK_SIZE",
    "FIRECRAWL_SEARCH_CREDITS_PER_BLOCK",
    "FeedEntry",
    "FeedOutcome",
    "FirecrawlClient",
    "GROUNDING_SEARCH_MICRO_USD",
    "GroundedResult",
    "GroundingCitation",
    "GroundingClient",
    "GroundingSource",
    "HttpReader",
    "MissingConfigError",
    "ModelPolicyUnavailableError",
    "PreviousReading",
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
    "ReadOutcome",
    "RssReader",
    "SERPER_BASE_URL",
    "SERPER_QUERY_MICRO_USD",
    "SerperClient",
    "SystemClock",
    "assert_safe_xml",
    "discovery_from_call",
    "firecrawl_scrape_credits",
    "firecrawl_search_credits",
    "normalize_text",
    "open_publications",
    "parse_datetime",
    "parse_feed",
    "parse_optional_int",
    "parse_retry_after",
    "run_provider_call",
    "safe_discovery_item",
    "token_cost_micro",
]
