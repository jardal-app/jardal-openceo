"""Adquisicion: clientes, retry, presupuesto, procedencia y seguridad
(TASK-2026-00060).

Todas las pruebas son offline: el HTTP se sustituye por un transporte falso que
registra a que destino se conecto de verdad, y los proveedores usan un
``httpx.AsyncBaseTransport`` controlado. Los relojes (monotono y de pared) se
inyectan para no dormir ni depender del tiempo real.
"""

from __future__ import annotations

import gzip
import json
import logging
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import httpx  # noqa: E402

import research.acquisition as acquisition  # noqa: E402
import research.acquisition.brave as brave_module  # noqa: E402
from research.acquisition import (  # noqa: E402
    BRAVE_SEARCH_MICRO_USD,
    SERPER_QUERY_MICRO_USD,
    BraveClient,
    CircuitBreaker,
    Clock,
    CostEstimate,
    Deadline,
    DeadlineExceededError,
    ProviderAmbiguousTimeout,
    ProviderAuthError,
    ProviderDisabledError,
    ProviderParseError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTransportError,
    FirecrawlClient,
    HttpReader,
    MissingConfigError,
    PreviousReading,
    RssReader,
    SerperClient,
    firecrawl_scrape_credits,
    firecrawl_search_credits,
    open_publications,
    parse_feed,
    run_provider_call,
)
from research.acquisition.base import DiscoveryItem  # noqa: E402
from research.acquisition.rss import UnsafeXmlError  # noqa: E402
from research.budget import Reservation, ReserveDecision  # noqa: E402
from research.contracts import (  # noqa: E402
    AccessMethod,
    Classification,
    CoverageReason,
    RetentionPolicy,
)
from research.policies import (  # noqa: E402
    EgressGuard,
    FetchLimits,
    GuardedFetcher,
    PinnedEgressTransport,
)
from settings import Settings  # noqa: E402

UTC = timezone.utc
D1 = datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)
D2 = datetime(2026, 9, 30, 11, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 30, 10, 0, 5, tzinfo=UTC)
T2 = datetime(2026, 9, 30, 11, 0, 5, tzinfo=UTC)

SNIPPET = "SNIPPET-TRANSITORIO-QUE-NO-DEBE-PERSISTIR"


# --------------------------------------------------------------------- dobles


class _FakeClock(Clock):
    """Reloj monotono falso: registra las esperas y avanza en vez de dormir."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.slept: list[float] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(float(seconds))
        self._now += float(seconds)

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


class _Wall:
    """Reloj de pared falso para fechas de evidencia."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class _Transport(httpx.AsyncBaseTransport):
    """Transporte HTTP falso que registra cada peticion entregada."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self._handler(request)
        if isinstance(result, BaseException):
            raise result
        status, headers, body = result
        return httpx.Response(status, headers=headers, content=body, request=request)


def _sequence(*specs):
    """Handler que devuelve specs en orden (el ultimo se repite)."""
    state = {"index": 0}

    def handler(request):
        index = min(state["index"], len(specs) - 1)
        state["index"] += 1
        return specs[index]

    return handler


def _json_response(payload, status: int = 200):
    return (status, {"content-type": "application/json"}, json.dumps(payload).encode("utf-8"))


class _Ledger:
    """Ledger falso que imita las reglas de techo desconocido/cero del real."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.reserves: list[dict] = []
        self.commits: list[tuple[str, int | None]] = []
        self.releases: list[str] = []
        self.uncertain: list[str] = []

    async def reserve(self, **kwargs) -> ReserveDecision:
        self.reserves.append(kwargs)
        if not self.accept:
            return ReserveDecision(accepted=False, reason="account_ceiling")
        amount = int(kwargs["amount_micro_usd"])
        ceiling = kwargs.get("ceiling_micro_usd")
        if amount > 0 and ceiling == 0:
            return ReserveDecision(accepted=False, reason="account_ceiling")
        if amount > 0 and ceiling is None and not kwargs.get("allow_unknown_ceiling"):
            return ReserveDecision(accepted=False, reason="unknown_ceiling")
        reservation = Reservation(
            operation_id=kwargs["operation_id"],
            provider=kwargs["provider"],
            account_id=kwargs.get("account", "default"),
            amount_micro_usd=amount,
            project_key=kwargs["project_key"],
            research_id=kwargs["research_id"],
        )
        return ReserveDecision(accepted=True, reservation=reservation)

    async def commit(self, reservation, *, charged_micro_usd) -> None:
        self.commits.append((reservation.operation_id, charged_micro_usd))

    async def release(self, reservation) -> None:
        self.releases.append(reservation.operation_id)

    async def mark_uncertain(self, reservation) -> None:
        self.uncertain.append(reservation.operation_id)


def _resolver(mapping):
    async def resolve(host: str) -> list[str]:
        return mapping.get(host, [])

    return resolve


def _guarded_reader(
    handler,
    *,
    mapping=None,
    limits=None,
    wall=None,
    fetcher_clock=None,
    clock=None,
    max_attempts: int = 2,
    backoff: float = 0.5,
):
    transport = _Transport(handler)
    guard = EgressGuard(resolver=_resolver(mapping or {"example.org": ["93.184.216.34"]}))
    pinned = PinnedEgressTransport(guard, inner=transport)
    fetcher = GuardedFetcher(guard=guard, transport=pinned, limits=limits, clock=fetcher_clock)
    reader = HttpReader(
        fetcher=fetcher,
        limits=limits,
        wall_clock=wall,
        clock=clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff,
    )
    return reader, transport


def _provider_client(handler) -> tuple[httpx.AsyncClient, _Transport]:
    transport = _Transport(handler)
    client = httpx.AsyncClient(transport=transport)
    return client, transport


def _brave_settings(**overrides) -> Settings:
    payload = {"RESEARCH_BRAVE_API_KEY": "test-key"}
    payload.update(overrides)
    return Settings(_env_file=None, **payload)


async def _call_brave(client, **kwargs):
    return await client.search_web(
        "consulta", research_id="res_1", project_key="proj", **kwargs
    )


# ---------------------------------------------------------------- taxonomia


class ErrorTaxonomyTests(unittest.TestCase):
    def test_kinds_are_distinct_and_closed(self) -> None:
        kinds = {
            MissingConfigError.kind,
            ProviderDisabledError.kind,
            ProviderAuthError.kind,
            ProviderQuotaExhausted.kind,
            ProviderRateLimited.kind,
            ProviderTransportError.kind,
            ProviderAmbiguousTimeout.kind,
            ProviderServerError.kind,
            ProviderParseError.kind,
        }
        self.assertEqual(len(kinds), 9)
        self.assertNotIn("duplicate", kinds)

    def test_no_erpnext_duplicate_exception_is_copied(self) -> None:
        # El proveedor no tiene semantica de duplicado de negocio: traer la excepcion
        # `Duplicate` de ERPNext haria pasar un resultado repetido por error de proveedor.
        self.assertFalse(hasattr(acquisition, "Duplicate"))
        self.assertFalse(hasattr(brave_module, "Duplicate"))

    def test_each_failure_maps_to_a_coverage_reason(self) -> None:
        self.assertEqual(ProviderQuotaExhausted.coverage_reason, CoverageReason.QUOTA_EXHAUSTED)
        self.assertEqual(ProviderRateLimited.coverage_reason, CoverageReason.RATE_LIMITED)
        self.assertEqual(ProviderAuthError.coverage_reason, CoverageReason.ACCESS_DENIED)
        self.assertEqual(
            ProviderAmbiguousTimeout.coverage_reason, CoverageReason.SOURCE_UNAVAILABLE
        )
        self.assertFalse(ProviderAuthError.retryable)
        self.assertTrue(ProviderTransportError.retryable)
        self.assertFalse(ProviderAmbiguousTimeout.retryable)

    def test_degradation_payload_is_serializable(self) -> None:
        error = ProviderRateLimited("x", provider="brave", retry_after=12.0)
        payload = error.degradation().to_payload()
        self.assertEqual(payload["reason"], "rate_limited")
        self.assertEqual(payload["coverage_reason"], "rate_limited")
        self.assertEqual(payload["retry_after_seconds"], 12.0)


class DeadlineTests(unittest.TestCase):
    def test_phase_timeout_is_capped_by_remaining(self) -> None:
        clock = _FakeClock(0.0)
        deadline = Deadline.start(5.0, clock=clock)
        self.assertEqual(deadline.phase_timeout(30.0), 5.0)
        clock.advance(3.0)
        self.assertAlmostEqual(deadline.phase_timeout(30.0), 2.0)

    def test_expired_deadline_refuses_to_start(self) -> None:
        clock = _FakeClock(0.0)
        deadline = Deadline.start(1.0, clock=clock)
        clock.advance(1.5)
        self.assertTrue(deadline.expired)
        with self.assertRaises(DeadlineExceededError):
            deadline.phase_timeout(10.0)


class CircuitBreakerTests(unittest.TestCase):
    def test_opens_after_threshold_and_recovers_after_the_window(self) -> None:
        clock = _FakeClock(0.0)
        breaker = CircuitBreaker(failure_threshold=2, open_seconds=30.0, clock=clock)
        breaker.record_failure("brave:default")
        breaker.check("brave:default")
        breaker.record_failure("brave:default")
        with self.assertRaises(Exception):
            breaker.check("brave:default")
        clock.advance(31.0)
        breaker.check("brave:default")  # ya no lanza

    def test_state_is_shared_not_per_call(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, open_seconds=60.0, clock=_FakeClock())
        breaker.record_failure("brave:default")
        # Dos clientes distintos que reciben el mismo objeto comparten el circuito.
        self.assertTrue(breaker.is_open("brave:default"))


# -------------------------------------------------------------------- Brave


class BraveClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_degrades_without_any_request(self) -> None:
        ledger = _Ledger()
        client, transport = _provider_client(
            lambda request: _json_response({"web": {"results": []}})
        )
        async with client:
            brave = BraveClient(
                settings=_brave_settings(RESEARCH_BRAVE_API_KEY=""),
                http_client=client,
                ledger=ledger,
            )
            result = await _call_brave(brave)
        self.assertEqual(result.items, ())
        self.assertEqual(result.degradations[0].reason, "missing_config")
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.SOURCE_UNAVAILABLE)
        self.assertEqual(transport.requests, [])
        self.assertEqual(ledger.reserves, [])

    async def test_disabled_adapter_degrades(self) -> None:
        client, transport = _provider_client(lambda request: _json_response({}))
        async with client:
            brave = BraveClient(
                settings=_brave_settings(RESEARCH_BRAVE_ENABLED=False),
                http_client=client,
                ledger=_Ledger(),
            )
            result = await _call_brave(brave)
        self.assertEqual(result.degradations[0].reason, "disabled")
        self.assertEqual(transport.requests, [])

    async def test_success_charges_the_published_tariff_and_records_the_attempt(self) -> None:
        ledger = _Ledger()
        client, transport = _provider_client(
            lambda request: _json_response(
                {
                    "web": {
                        "results": [
                            {"title": "t", "url": "https://example.org/a", "description": SNIPPET},
                            {"title": "u", "url": "https://example.org/b", "description": SNIPPET},
                        ]
                    }
                }
            )
        )
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger
            )
            result = await _call_brave(brave, count=2)
        self.assertEqual([item.url for item in result.items], ["https://example.org/a", "https://example.org/b"])
        self.assertEqual(result.charged_micro_usd, BRAVE_SEARCH_MICRO_USD)
        self.assertEqual(ledger.reserves[0]["amount_micro_usd"], BRAVE_SEARCH_MICRO_USD)
        self.assertEqual(ledger.reserves[0]["ceiling_micro_usd"], 5_000_000)
        self.assertEqual(len(ledger.commits), 1)
        self.assertEqual(result.attempts[0].outcome, "committed")
        self.assertEqual(len(transport.requests), 1)

    async def test_402_is_quota_exhausted_without_retry(self) -> None:
        ledger = _Ledger()
        client, transport = _provider_client(lambda request: (402, {}, b""))
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger
            )
            result = await _call_brave(brave)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(result.degradations[0].reason, "quota_exhausted")
        self.assertEqual(result.charged_micro_usd, 0)
        self.assertEqual(len(ledger.releases), 1)
        self.assertEqual(ledger.commits, [])

    async def test_429_honours_retry_after_within_the_deadline(self) -> None:
        clock = _FakeClock(0.0)
        ledger = _Ledger()
        handler = _sequence(
            (429, {"retry-after": "2"}, b""),
            _json_response({"web": {"results": [{"url": "https://example.org/a"}]}}),
        )
        client, transport = _provider_client(handler)
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger, clock=clock
            )
            deadline = Deadline.start(30.0, clock=clock)
            result = await _call_brave(brave, deadline=deadline)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(clock.slept, [2.0])
        self.assertTrue(result.items)
        self.assertEqual(result.degradations, ())

    async def test_429_retry_after_beyond_the_deadline_is_not_slept(self) -> None:
        clock = _FakeClock(0.0)
        ledger = _Ledger()
        client, transport = _provider_client(lambda request: (429, {"retry-after": "600"}, b""))
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger, clock=clock
            )
            deadline = Deadline.start(5.0, clock=clock)
            result = await _call_brave(brave, deadline=deadline)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(clock.slept, [])
        self.assertEqual(result.degradations[0].reason, "rate_limited")
        self.assertEqual(result.degradations[0].retry_after, 600.0)
        self.assertEqual(result.charged_micro_usd, 0)

    async def test_401_and_403_are_auth_errors(self) -> None:
        for status in (401, 403):
            ledger = _Ledger()
            client, transport = _provider_client(lambda request: (status, {}, b""))
            async with client:
                brave = BraveClient(
                    settings=_brave_settings(), http_client=client, ledger=ledger
                )
                result = await _call_brave(brave)
            self.assertEqual(len(transport.requests), 1)
            self.assertEqual(result.degradations[0].reason, "auth")
            self.assertEqual(
                result.degradations[0].coverage_reason, CoverageReason.ACCESS_DENIED
            )
            self.assertEqual(len(ledger.releases), 1)

    async def test_connect_failure_is_retried_bounded(self) -> None:
        ledger = _Ledger()
        request_holder = {}

        def handler(request):
            request_holder.setdefault("n", 0)
            request_holder["n"] += 1
            if request_holder["n"] <= 2:
                raise httpx.ConnectError("no llego", request=request)
            return _json_response({"web": {"results": [{"url": "https://example.org/a"}]}})

        client, transport = _provider_client(handler)
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger, max_attempts=3
            )
            result = await _call_brave(brave)
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(len(ledger.releases), 2)
        self.assertEqual(len(ledger.commits), 1)
        self.assertTrue(result.items)

    async def test_ambiguous_timeout_is_never_free_and_is_not_retried(self) -> None:
        ledger = _Ledger()

        def handler(request):
            raise httpx.ReadTimeout("timeout tras enviar", request=request)

        client, transport = _provider_client(handler)
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger, max_attempts=3
            )
            result = await _call_brave(brave)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(ledger.uncertain), 1)
        self.assertEqual(ledger.releases, [])
        self.assertEqual(ledger.commits, [])
        self.assertIsNone(result.charged_micro_usd)
        self.assertTrue(result.to_payload()["uncertain"])
        self.assertEqual(result.degradations[0].reason, "timeout_ambiguous")

    async def test_server_error_retries_while_the_error_response_is_not_billed(self) -> None:
        ledger = _Ledger()
        handler = _sequence(
            (500, {}, b""),
            (503, {}, b""),
            _json_response({"web": {"results": [{"url": "https://example.org/a"}]}}),
        )
        client, transport = _provider_client(handler)
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger, max_attempts=3
            )
            result = await _call_brave(brave)
        self.assertEqual(len(transport.requests), 3)
        self.assertTrue(result.items)
        self.assertEqual(len(ledger.releases), 2)

    async def test_server_error_is_not_retried_when_it_could_double_charge(self) -> None:
        transport = _Transport(lambda request: (500, {}, b""))
        client = httpx.AsyncClient(transport=transport)
        async with client:
            call = await run_provider_call(
                provider="brave",
                operation_id="op",
                research_id="res_1",
                project_key="proj",
                cost=CostEstimate(amount_micro_usd=BRAVE_SEARCH_MICRO_USD),
                request=lambda timeout: client.get(
                    "https://api.search.brave.com/res/v1/web/search"
                ),
                parse=lambda response: {"items": []},
                retry_server_errors=False,
            )
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(call.degradations[0].reason, "server_error")

    async def test_circuit_breaker_short_circuits_a_shared_account(self) -> None:
        clock = _FakeClock(0.0)
        breaker = CircuitBreaker(failure_threshold=2, open_seconds=60.0, clock=clock)
        ledger = _Ledger()
        client, transport = _provider_client(lambda request: (500, {}, b""))
        async with client:
            brave = BraveClient(
                settings=_brave_settings(),
                http_client=client,
                ledger=ledger,
                breaker=breaker,
                clock=clock,
                max_attempts=1,
            )
            first = await _call_brave(brave)
            second = await _call_brave(brave)
            third = await _call_brave(brave)
        self.assertEqual(first.degradations[0].reason, "server_error")
        self.assertEqual(second.degradations[0].reason, "server_error")
        self.assertEqual(third.degradations[0].reason, "circuit_open")
        # El circuito abierto no toca la red ni reserva.
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(len(ledger.reserves), 2)

    async def test_budget_refused_stops_before_the_request(self) -> None:
        ledger = _Ledger(accept=False)
        client, transport = _provider_client(lambda request: _json_response({}))
        async with client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=client, ledger=ledger
            )
            result = await _call_brave(brave)
        self.assertEqual(transport.requests, [])
        self.assertEqual(result.degradations[0].reason, "budget_refused")
        self.assertEqual(
            result.degradations[0].coverage_reason, CoverageReason.BUDGET_EXHAUSTED
        )

    async def test_snippets_never_reach_payload_logs_or_the_client_state(self) -> None:
        response_body = {
            "web": {
                "results": [
                    {
                        "title": "Titulo transitorio",
                        "url": "https://example.org/a",
                        "description": SNIPPET,
                    }
                ]
            }
        }
        client, transport = _provider_client(lambda request: _json_response(response_body))
        logger = logging.getLogger("research.acquisition")
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _Capture()
        logger.addHandler(capture)
        try:
            async with client:
                brave = BraveClient(
                    settings=_brave_settings(), http_client=client, ledger=_Ledger()
                )
                result = await _call_brave(brave)
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)

        payload_text = json.dumps(result.to_payload())
        self.assertNotIn(SNIPPET, payload_text)
        self.assertNotIn("snippet", payload_text)
        self.assertNotIn("description", payload_text)
        self.assertNotIn(SNIPPET, "\n".join(record.getMessage() for record in records))
        self.assertNotIn(SNIPPET, repr(vars(brave)))
        self.assertEqual(
            {field.name for field in DiscoveryItem.__dataclass_fields__.values()},
            {"url", "provider", "rank"},
        )

    async def test_the_parse_output_contains_only_sanitized_items(self) -> None:
        response = httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"title": "t", "url": "https://example.org/a", "description": SNIPPET}
                    ]
                }
            },
        )
        parsed = brave_module._parse(response)
        self.assertEqual(set(parsed), {"items"})
        self.assertEqual(parsed["items"][0].to_payload(), {
            "url": "https://example.org/a",
            "provider": "brave",
            "rank": 1,
        })


# ------------------------------------------------------------------- Serper


class SerperClientTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, **overrides):
        payload = {
            "RESEARCH_SERPER_API_KEY": "test-key",
            "RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES": "2500",
        }
        payload.update(overrides)
        return Settings(_env_file=None, **payload)

    async def test_missing_key_degrades_structurally(self) -> None:
        client, transport = _provider_client(lambda request: _json_response({"organic": []}))
        async with client:
            serper = SerperClient(
                settings=self._settings(RESEARCH_SERPER_API_KEY=""),
                http_client=client,
                ledger=_Ledger(),
            )
            result = await serper.search_web("q", research_id="res_1", project_key="proj")
        self.assertEqual(result.degradations[0].reason, "missing_config")
        self.assertEqual(transport.requests, [])

    async def test_unknown_credit_balance_does_not_authorize_spending(self) -> None:
        ledger = _Ledger()
        client, transport = _provider_client(lambda request: _json_response({"organic": []}))
        async with client:
            serper = SerperClient(
                settings=self._settings(RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES=""),
                http_client=client,
                ledger=ledger,
            )
            result = await serper.search_web("q", research_id="res_1", project_key="proj")
        self.assertEqual(transport.requests, [])
        self.assertEqual(result.degradations[0].reason, "budget_refused")

    async def test_success_charges_one_query_and_returns_urls(self) -> None:
        ledger = _Ledger()
        body = {"organic": [{"title": "t", "link": "https://example.org/a", "snippet": SNIPPET}]}
        client, transport = _provider_client(lambda request: _json_response(body))
        async with client:
            serper = SerperClient(
                settings=self._settings(), http_client=client, ledger=ledger
            )
            result = await serper.search_web("q", research_id="res_1", project_key="proj")
        self.assertEqual([item.url for item in result.items], ["https://example.org/a"])
        self.assertEqual(result.charged_micro_usd, SERPER_QUERY_MICRO_USD)
        self.assertEqual(ledger.reserves[0]["ceiling_micro_usd"], 2500 * SERPER_QUERY_MICRO_USD)
        self.assertNotIn(SNIPPET, json.dumps(result.to_payload()))
        self.assertEqual(len(transport.requests), 1)

    async def test_news_operation_uses_the_news_block(self) -> None:
        body = {"news": [{"title": "n", "link": "https://example.org/n", "snippet": SNIPPET}]}
        client, _ = _provider_client(lambda request: _json_response(body))
        async with client:
            serper = SerperClient(
                settings=self._settings(), http_client=client, ledger=_Ledger()
            )
            result = await serper.search_news("q", research_id="res_1", project_key="proj")
        self.assertEqual([item.url for item in result.items], ["https://example.org/n"])


# ---------------------------------------------------------------- Firecrawl


class FirecrawlCreditTests(unittest.TestCase):
    def test_search_credits_are_two_per_started_block_of_ten(self) -> None:
        self.assertEqual(firecrawl_search_credits(1), 2)
        self.assertEqual(firecrawl_search_credits(10), 2)
        self.assertEqual(firecrawl_search_credits(11), 4)
        self.assertEqual(firecrawl_search_credits(20), 4)
        self.assertEqual(firecrawl_search_credits(21), 6)

    def test_scrape_credits_are_one_page_plus_llm_extraction(self) -> None:
        self.assertEqual(firecrawl_scrape_credits(("markdown",)), 1)
        self.assertEqual(firecrawl_scrape_credits(("markdown", "json")), 5)

    def test_no_fictional_browser_adapter_is_introduced(self) -> None:
        self.assertFalse(hasattr(acquisition, "BrowserClient"))


class FirecrawlClientTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, **overrides):
        payload = {"RESEARCH_FIRECRAWL_API_KEY": "test-key"}
        payload.update(overrides)
        return Settings(_env_file=None, **payload)

    async def test_disabled_adapter_degrades_without_request(self) -> None:
        client, transport = _provider_client(lambda request: _json_response({}))
        async with client:
            firecrawl = FirecrawlClient(
                settings=self._settings(RESEARCH_FIRECRAWL_ENABLED=False),
                http_client=client,
                ledger=_Ledger(),
            )
            result = await firecrawl.search("q", research_id="res_1", project_key="proj")
        self.assertEqual(result.degradations[0].reason, "disabled")
        self.assertEqual(transport.requests, [])

    async def test_search_reserves_the_real_block_credits(self) -> None:
        ledger = _Ledger()
        body = {"success": True, "data": {"web": [{"url": "https://example.org/a"}]}}
        client, _ = _provider_client(lambda request: _json_response(body))
        async with client:
            firecrawl = FirecrawlClient(
                settings=self._settings(), http_client=client, ledger=ledger
            )
            result = await firecrawl.search(
                "q", research_id="res_1", project_key="proj", limit=11
            )
        self.assertEqual(result.credits, 4)
        # Plan gratuito sin compra autorizada: coste monetario real 0, creditos 4.
        self.assertEqual(ledger.reserves[0]["amount_micro_usd"], 0)
        self.assertEqual(result.charged_micro_usd, 0)

    async def test_scrape_returns_markdown_evidence_with_one_credit(self) -> None:
        ledger = _Ledger()
        body = {
            "success": True,
            "data": {
                "markdown": "# Titulo\n\nContenido material",
                "metadata": {
                    "title": "Titulo",
                    "sourceURL": "https://example.org/final",
                    "publishedTime": "2026-09-30T10:00:00Z",
                },
            },
        }
        client, _ = _provider_client(lambda request: _json_response(body))
        async with client:
            firecrawl = FirecrawlClient(
                settings=self._settings(), http_client=client, ledger=ledger
            )
            outcome = await firecrawl.scrape(
                "https://example.org/a", research_id="res_1", project_key="proj"
            )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.access_method, AccessMethod.FIRECRAWL)
        self.assertEqual(outcome.evidence.access_method, AccessMethod.FIRECRAWL)
        self.assertIn("Contenido material", outcome.text)
        self.assertTrue(outcome.content_hash)
        self.assertEqual(outcome.final_url, "https://example.org/final")
        self.assertIsNotNone(outcome.published_at)
        self.assertEqual(outcome.evidence.retention_policy, RetentionPolicy.PUBLIC_7D)

    async def test_scrape_402_is_quota_exhausted(self) -> None:
        client, transport = _provider_client(lambda request: (402, {}, b""))
        async with client:
            firecrawl = FirecrawlClient(
                settings=self._settings(), http_client=client, ledger=_Ledger()
            )
            outcome = await firecrawl.scrape(
                "https://example.org/a", research_id="res_1", project_key="proj"
            )
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(outcome.degradations[0].reason, "quota_exhausted")


# --------------------------------------------------------------- HTTP reader


_ARTICLE = (
    "<html><head><title>Titulo material</title>"
    '<meta property="article:published_time" content="2026-09-30T10:00:00Z">'
    "<script>alert('no material')</script><style>.a{color:red}</style>"
    "</head><body><p>Contenido material</p><p>Segundo parrafo</p>"
    "<nav>menu</nav></body></html>"
).encode("utf-8")


class HttpReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_page_evidence_has_provenance_and_keeps_material_text(self) -> None:
        reader, transport = _guarded_reader(
            lambda request: (200, {"content-type": "text/html; charset=utf-8"}, _ARTICLE),
            wall=_Wall(T1),
            fetcher_clock=_Wall(D1),
        )
        outcome = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.evidence.url, "https://example.org/a")
        self.assertEqual(outcome.evidence.access_method, AccessMethod.HTTP)
        self.assertIsNotNone(outcome.evidence.observed_at)
        self.assertEqual(outcome.evidence.observed_at, D1)
        self.assertTrue(outcome.evidence.content_hash)
        self.assertEqual(outcome.evidence.retention_policy, RetentionPolicy.PUBLIC_7D)
        self.assertEqual(outcome.title, "Titulo material")
        self.assertEqual(outcome.published_at, D1)
        self.assertEqual(outcome.observed_at, D1)
        self.assertEqual(outcome.last_checked_at, T1)
        self.assertIn("Contenido material", outcome.text)
        self.assertIn("Segundo parrafo", outcome.text)
        # La normalizacion de ruido no elimina contenido material ni deja scripts.
        self.assertNotIn("alert", outcome.text)
        self.assertNotIn("color:red", outcome.text)
        self.assertEqual(len(transport.requests), 1)

    async def test_304_preserves_previous_evidence_and_separates_the_clocks(self) -> None:
        fetcher_clock = _Wall(D1)
        wall = _Wall(T1)
        reader, transport = _guarded_reader(
            _sequence(
                (200, {"content-type": "text/html", "etag": "v1"}, _ARTICLE),
                (304, {"etag": "v1"}, b""),
            ),
            wall=wall,
            fetcher_clock=fetcher_clock,
        )
        first = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertEqual(first.etag, "v1")
        fetcher_clock.advance(3600)
        wall.advance(3600)
        second = await reader.read(
            "https://example.org/a",
            research_id="res_1",
            project_key="proj",
            previous=PreviousReading(
                evidence=first.evidence,
                text=first.text,
                body=first.body,
                etag=first.etag,
                last_modified=first.last_modified,
            ),
        )
        self.assertTrue(second.not_modified)
        self.assertEqual(second.content_hash, first.content_hash)
        self.assertEqual(second.text, first.text)
        self.assertEqual(second.body, first.body)
        self.assertEqual(second.evidence.url, first.evidence.url)
        # La fecha de descarga no se falsea: sigue siendo D1. Solo avanza la
        # comprobacion, que es un instante distinto.
        self.assertEqual(second.observed_at, D1)
        self.assertEqual(second.last_checked_at, T2)
        self.assertNotEqual(second.last_checked_at, second.observed_at)
        self.assertEqual(transport.requests[1].headers["if-none-match"], "v1")

    async def test_304_without_previous_evidence_is_reported_not_invented(self) -> None:
        reader, _ = _guarded_reader(lambda request: (304, {}, b""))
        outcome = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertIsNone(outcome.evidence)
        self.assertEqual(outcome.degradations[0].reason, "protocol")

    async def test_excessive_decompressed_body_is_rejected(self) -> None:
        payload = gzip.compress(b"x" * 200_000)
        reader, transport = _guarded_reader(
            lambda request: (
                200,
                {"content-type": "text/html", "content-encoding": "gzip"},
                payload,
            ),
            limits=FetchLimits(max_bytes=1_000),
        )
        outcome = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertIsNone(outcome.evidence)
        self.assertEqual(outcome.degradations[0].reason, "too_large")
        self.assertEqual(len(transport.requests), 1)

    async def test_unsupported_mime_is_rejected(self) -> None:
        reader, _ = _guarded_reader(
            lambda request: (200, {"content-type": "application/octet-stream"}, b"bin")
        )
        outcome = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertEqual(outcome.degradations[0].reason, "unsupported_media")

    async def test_relative_redirect_records_the_final_url(self) -> None:
        reader, transport = _guarded_reader(
            _sequence(
                (302, {"location": "/b"}, b""),
                (200, {"content-type": "text/html"}, _ARTICLE),
            )
        )
        outcome = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertEqual(outcome.final_url, "https://example.org/b")
        self.assertEqual(outcome.redirects, 1)
        self.assertTrue(outcome.ok)
        self.assertEqual(len(transport.requests), 2)

    async def test_redirect_to_an_internal_host_is_blocked_before_connecting(self) -> None:
        reader, transport = _guarded_reader(
            lambda request: (302, {"location": "http://interno.example/admin"}, b""),
            mapping={
                "example.org": ["93.184.216.34"],
                "interno.example": ["10.0.0.7"],
            },
        )
        outcome = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertEqual(outcome.degradations[0].reason, "egress_blocked")
        self.assertEqual(len(transport.requests), 1)

    async def test_transport_failure_is_retried_bounded_for_a_free_source(self) -> None:
        state = {"n": 0}

        def handler(request):
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ConnectError("no llego", request=request)
            return (200, {"content-type": "text/html"}, _ARTICLE)

        reader, transport = _guarded_reader(handler, max_attempts=2)
        outcome = await reader.read(
            "https://example.org/a", research_id="res_1", project_key="proj"
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(len(transport.requests), 2)

    async def test_429_respects_retry_after_within_the_deadline(self) -> None:
        clock = _FakeClock(0.0)
        reader, transport = _guarded_reader(
            _sequence(
                (429, {"retry-after": "1"}, b""),
                (200, {"content-type": "text/html"}, _ARTICLE),
            ),
            clock=clock,
        )
        deadline = Deadline.start(10.0, clock=clock)
        outcome = await reader.read(
            "https://example.org/a",
            research_id="res_1",
            project_key="proj",
            deadline=deadline,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(clock.slept, [1.0])
        self.assertEqual(len(transport.requests), 2)

    async def test_expired_deadline_degrades_without_touching_the_network(self) -> None:
        clock = _FakeClock(0.0)
        reader, transport = _guarded_reader(
            lambda request: (200, {"content-type": "text/html"}, _ARTICLE), clock=clock
        )
        deadline = Deadline.start(1.0, clock=clock)
        clock.advance(5.0)
        outcome = await reader.read(
            "https://example.org/a",
            research_id="res_1",
            project_key="proj",
            deadline=deadline,
        )
        self.assertEqual(outcome.degradations[0].reason, "deadline_exceeded")
        self.assertEqual(transport.requests, [])


# ---------------------------------------------------------------------- RSS


_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Feed de prueba</title>
    <item>
      <title>Entrada uno</title>
      <link>https://example.org/uno</link>
      <pubDate>Wed, 30 Sep 2026 10:00:00 GMT</pubDate>
      <description>Resumen de la entrada</description>
      <enclosure url="https://example.org/audio.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Entrada dos</title>
      <link>/dos</link>
      <pubDate>2026-09-29T09:00:00Z</pubDate>
    </item>
  </channel>
</rss>
""".encode("utf-8")

_UNSAFE_DOCTYPE = b"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><title>malo</title>
<item><title>&xxe;</title><link>https://example.org/x</link></item>
</channel></rss>
"""

_ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom de prueba</title>
  <entry>
    <title>Entrada atom</title>
    <link rel="alternate" href="https://example.org/atom"/>
    <published>2026-09-30T08:00:00Z</published>
    <summary>Resumen atom</summary>
  </entry>
</feed>
"""


class RssParsingTests(unittest.TestCase):
    def test_doctype_and_entities_are_rejected_before_parsing(self) -> None:
        with self.assertRaises(UnsafeXmlError):
            parse_feed(_UNSAFE_DOCTYPE.decode("utf-8"), feed_url="https://example.org/feed")

    def test_plain_entity_is_rejected(self) -> None:
        with self.assertRaises(UnsafeXmlError):
            parse_feed(
                '<?xml version="1.0"?><!ENTITY x "y"><rss><channel/></rss>',
                feed_url="https://example.org/feed",
            )

    def test_malformed_xml_is_a_parse_error(self) -> None:
        with self.assertRaises(ProviderParseError):
            parse_feed("<rss><channel>", feed_url="https://example.org/feed")

    def test_valid_rss_parses_absolute_and_relative_links(self) -> None:
        title, entries = parse_feed(_RSS.decode("utf-8"), feed_url="https://example.org/feed")
        self.assertEqual(title, "Feed de prueba")
        self.assertEqual([entry.url for entry in entries], [
            "https://example.org/uno",
            "https://example.org/dos",
        ])
        self.assertIsNotNone(entries[0].published_at)
        self.assertEqual(entries[1].published_at, datetime(2026, 9, 29, 9, 0, tzinfo=UTC))

    def test_atom_is_supported(self) -> None:
        title, entries = parse_feed(_ATOM.decode("utf-8"), feed_url="https://example.org/feed")
        self.assertEqual(title, "Atom de prueba")
        self.assertEqual(entries[0].url, "https://example.org/atom")
        self.assertEqual(entries[0].published_at, datetime(2026, 9, 30, 8, 0, tzinfo=UTC))


class RssReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsafe_feed_is_rejected_and_evidence_preserved(self) -> None:
        reader, transport = _guarded_reader(
            lambda request: (200, {"content-type": "application/rss+xml"}, _UNSAFE_DOCTYPE)
        )
        rss = RssReader(reader=reader)
        outcome = await rss.read_feed(
            "https://example.org/feed", research_id="res_1", project_key="proj"
        )
        self.assertEqual(outcome.entries, ())
        self.assertEqual(outcome.degradations[0].reason, "unsafe_xml")
        self.assertIsNotNone(outcome.evidence)
        self.assertEqual(len(transport.requests), 1)

    async def test_parser_does_not_download_entry_or_enclosure_resources(self) -> None:
        reader, transport = _guarded_reader(
            _sequence(
                (200, {"content-type": "application/rss+xml"}, _RSS),
                (200, {"content-type": "text/html"}, _ARTICLE),
            )
        )
        rss = RssReader(reader=reader)
        outcome = await rss.read_feed(
            "https://example.org/feed", research_id="res_1", project_key="proj"
        )
        # Solo se descargo el feed: ni el enclosure ni los enlaces de las entradas.
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(len(outcome.entries), 2)
        opened = await rss.open_entries(
            outcome, research_id="res_1", project_key="proj", limit=1
        )
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(opened[0].evidence.url, "https://example.org/uno")

    async def test_feed_304_preserves_entries_and_download_date(self) -> None:
        fetcher_clock = _Wall(D1)
        wall = _Wall(T1)
        reader, transport = _guarded_reader(
            _sequence(
                (200, {"content-type": "application/rss+xml", "etag": "f1"}, _RSS),
                (304, {"etag": "f1"}, b""),
            ),
            wall=wall,
            fetcher_clock=fetcher_clock,
        )
        rss = RssReader(reader=reader)
        first = await rss.read_feed(
            "https://example.org/feed", research_id="res_1", project_key="proj"
        )
        fetcher_clock.advance(3600)
        wall.advance(3600)
        second = await rss.read_feed(
            "https://example.org/feed",
            research_id="res_1",
            project_key="proj",
            previous=first,
        )
        self.assertTrue(second.not_modified)
        self.assertEqual(second.entries, first.entries)
        self.assertIsNotNone(second.evidence)
        self.assertEqual(second.observed_at, D1)
        self.assertEqual(second.last_checked_at, T2)
        self.assertEqual(transport.requests[1].headers["if-none-match"], "f1")

    async def test_feed_304_does_not_reparse_the_cached_xml(self) -> None:
        reader, _ = _guarded_reader(
            _sequence(
                (200, {"content-type": "application/rss+xml", "etag": "f1"}, _RSS),
                (304, {"etag": "f1"}, b""),
            )
        )
        rss = RssReader(reader=reader)
        first = await rss.read_feed(
            "https://example.org/feed", research_id="res_1", project_key="proj"
        )
        # Si el 304 reparseara el XML cacheado, un texto invalido lo delataria.
        corrupted = replace(first, feed_text="<no-es-xml", feed_body=b"<no-es-xml")
        second = await rss.read_feed(
            "https://example.org/feed",
            research_id="res_1",
            project_key="proj",
            previous=corrupted,
        )
        self.assertTrue(second.not_modified)
        self.assertEqual(second.entries, first.entries)


# --------------------------------------------------------------- integracion


class ProvenanceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_brave_discovery_plus_public_http_produces_evidence_with_provenance(self) -> None:
        # 1) Brave falso devuelve un SERP con snippet transitorio.
        brave_body = {
            "web": {
                "results": [
                    {"title": "Titulo", "url": "https://example.org/a", "description": SNIPPET}
                ]
            }
        }
        provider_client, provider_transport = _provider_client(
            lambda request: _json_response(brave_body)
        )
        # 2) Fuente publica falsa servida por el fetcher con guard de egress.
        reader, source_transport = _guarded_reader(
            lambda request: (200, {"content-type": "text/html"}, _ARTICLE),
            wall=_Wall(T1),
            fetcher_clock=_Wall(D1),
        )
        async with provider_client:
            brave = BraveClient(
                settings=_brave_settings(), http_client=provider_client, ledger=_Ledger()
            )
            discovery = await brave.search_web(
                "consulta", research_id="res_1", project_key="proj"
            )
            self.assertEqual(len(discovery.items), 1)
            opened = await open_publications(
                reader,
                discovery,
                research_id="res_1",
                project_key="proj",
                classification=Classification.PUBLIC,
            )

        self.assertEqual(len(opened), 1)
        outcome = opened[0]
        evidence = outcome.evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.url, "https://example.org/a")
        self.assertIsNotNone(evidence.observed_at)
        self.assertTrue(evidence.content_hash)
        self.assertEqual(evidence.retention_policy, RetentionPolicy.PUBLIC_7D)
        self.assertEqual(evidence.project_key, "proj")
        # La evidencia viene de la publicacion original, no del snippet del buscador.
        payload_text = json.dumps(outcome.to_payload())
        self.assertNotIn(SNIPPET, payload_text)
        self.assertIn("Contenido material", payload_text)
        self.assertEqual(len(provider_transport.requests), 1)
        self.assertEqual(len(source_transport.requests), 1)


if __name__ == "__main__":
    unittest.main()
