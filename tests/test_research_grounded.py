"""Grounding de Muse (TASK-2026-00064).

Lo que fijan estos tests, y que es facil de romper sin notarlo:

* habilitar `web_search` no es haber buscado;
* una cita es evidencia **derivada**, no una lectura ni un hash del articulo;
* el coste sale del uso reportado (subcentavo incluido); sin contador queda
  ``reconciled=None`` e incierto, nunca 0 ni inferido de los resultados;
* con pago cero o clasificacion no publica **no se llama** y no se cae a Contributor.

Todo corre offline: transporte falso, ledger y reloj inyectados.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import httpx  # noqa: E402

from research.acquisition.grounded import (  # noqa: E402
    CACHED_INPUT_MICRO_PER_MILLION,
    GROUNDING_SEARCH_MICRO_USD,
    INPUT_MICRO_PER_MILLION,
    OUTPUT_MICRO_PER_MILLION,
    GroundingClient,
    token_cost_micro,
)
from research.contracts import Classification, CoverageReason  # noqa: E402

MUSE_BASE = "https://api.meta.ai/v1"


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.value += seconds

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _Reservation:
    def __init__(self, operation_id: str, amount: int) -> None:
        self.operation_id = operation_id
        self.amount_micro_usd = amount


class _Decision:
    def __init__(self, accepted: bool, reservation: Any = None, reason: str | None = None) -> None:
        self.accepted = accepted
        self.reservation = reservation
        self.reason = reason


class _FakeLedger:
    """Ledger de prueba: registra cada operacion contable."""

    def __init__(self, *, accept: bool = True, reason: str | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._accept = accept
        self._reason = reason
        self._counter = 0

    async def reserve(self, **kwargs: Any) -> _Decision:
        self.calls.append(("reserve", kwargs))
        self._counter += 1
        if not self._accept:
            return _Decision(False, None, self._reason or "account_ceiling")
        return _Decision(True, _Reservation(kwargs["operation_id"], kwargs["amount_micro_usd"]))

    async def commit(self, reservation: Any, *, charged_micro_usd: int) -> None:
        self.calls.append(("commit", charged_micro_usd))

    async def release(self, reservation: Any) -> None:
        self.calls.append(("release", None))

    async def mark_uncertain(self, reservation: Any) -> None:
        self.calls.append(("mark_uncertain", None))

    def kinds(self) -> list[str]:
        return [name for name, _ in self.calls]

    def amounts(self, kind: str) -> list[Any]:
        return [value for name, value in self.calls if name == kind]


class _Transport:
    """Transporte falso con guion de respuestas y peticiones registradas."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        import json

        try:
            self.bodies.append(json.loads(request.content.decode("utf-8")))
        except Exception:
            self.bodies.append({})
        nxt = self._script.pop(0) if self._script else httpx.Response(500, json={})
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _client(script: list[Any], **kwargs: Any):
    transport = _Transport(script)
    http = httpx.AsyncClient(transport=httpx.MockTransport(transport.handler))
    client = GroundingClient(
        settings=None,
        http_client=http,
        endpoint=kwargs.pop("endpoint", None),
        **kwargs,
    )
    return client, transport


def _ok_body(
    *,
    searches: int = 1,
    results: list[dict[str, Any]] | None = None,
    citations: list[dict[str, Any]] | None = None,
    answer: str = "Respuesta",
    usage: dict[str, Any] | None = None,
    model: str = "muse-spark-1.3-contributor",
) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    if searches:
        for index in range(searches):
            item: dict[str, Any] = {
                "type": "web_search_call",
                "id": f"ws_{index}",
                "status": "completed",
                "action": {"type": "search", "query": "q"},
            }
            if results is not None and index == 0:
                item["results"] = results
            output.append(item)
    content: list[dict[str, Any]] = [{"type": "output_text", "text": answer, "annotations": citations or []}]
    output.append({"type": "message", "role": "assistant", "content": content})
    body: dict[str, Any] = {"id": "resp_1", "model": model, "output": output}
    if usage is not None:
        body["usage"] = usage
    return body


class _Base(unittest.IsolatedAsyncioTestCase):
    def make(self, script, *, budget="1.00", key="mk-test", enabled=True, ledger=None, clock=None, **kwargs):
        ledger = ledger if ledger is not None else _FakeLedger()
        clock = clock or _FakeClock()
        client, transport = _client(
            script,
            ledger=ledger,
            clock=clock,
            adapter_enabled=enabled,
            api_key=key,
            monthly_budget_micro_usd=budget,
            backoff_seconds=0.0,
            phase_timeout=5.0,
            **kwargs,
        )
        return client, transport, ledger, clock

    async def ask(self, client, **kwargs):
        params = {
            "research_id": "res_1",
            "project_key": "jardal-openceo",
            "classification": Classification.PUBLIC,
        }
        params.update(kwargs)
        return await client.query("Pregunta", **params)


class RequestShapeTests(_Base):
    async def test_the_request_enables_web_search_and_asks_for_results(self) -> None:
        client, transport, _ledger, _clock = self.make([httpx.Response(200, json=_ok_body(usage={"input_tokens": 10, "output_tokens": 5}))])
        await self.ask(client)
        self.assertEqual(len(transport.bodies), 1)
        body = transport.bodies[0]
        self.assertEqual(body["tools"], [{"type": "web_search"}])
        self.assertEqual(body["include"], ["web_search_call.results"])
        self.assertEqual(body["model"], "muse-spark-1.3-contributor")
        # Endpoint fijo: el modelo no puede cambiar el host.
        self.assertEqual(str(transport.requests[0].url), f"{MUSE_BASE}/responses")

    async def test_the_raw_provider_body_does_not_travel(self) -> None:
        client, _transport, _ledger, _clock = self.make([httpx.Response(200, json=_ok_body(usage={"input_tokens": 1, "output_tokens": 1}))])
        result = await self.ask(client)
        payload = result.to_payload()
        self.assertNotIn("output", payload)
        self.assertNotIn("usage_raw", payload)
        self.assertEqual(
            set(payload),
            {
                "provider",
                "operation_id",
                "model",
                "answer",
                "tool_enabled",
                "searched",
                "searches_performed",
                "citations",
                "sources",
                "duplicate_sources",
                "usage",
                "attempts",
                "degradations",
                "warnings",
                "coverage_gaps",
            },
        )


class ToolVsSearchTests(_Base):
    async def test_enabling_the_tool_is_not_the_same_as_searching(self) -> None:
        body = {
            "id": "resp_1",
            "model": "muse-spark-1.3-contributor",
            "output": [
                {"type": "web_search_call", "id": "ws_1"},  # habilitada, no usada
                {"type": "message", "content": [{"type": "output_text", "text": "Sin buscar"}]},
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        client, _transport, _ledger, _clock = self.make([httpx.Response(200, json=body)])
        result = await self.ask(client)
        self.assertTrue(result.tool_enabled)
        self.assertFalse(result.searched)
        self.assertEqual(result.searches_performed, 0)
        self.assertTrue(any("no se observo ninguna busqueda" in w for w in result.warnings))
        self.assertIn(CoverageReason.SOURCE_UNAVAILABLE.value, [g["reason"] for g in result.coverage_gaps()])

    async def test_a_completed_search_is_counted(self) -> None:
        results = [{"url": "https://example.org/a", "title": "A"}]
        client, _transport, _ledger, _clock = self.make(
            [httpx.Response(200, json=_ok_body(results=results, usage={"input_tokens": 10, "output_tokens": 5}))]
        )
        result = await self.ask(client)
        self.assertTrue(result.searched)
        self.assertEqual(result.searches_performed, 1)
        self.assertEqual(result.coverage_gaps(), [])

    async def test_a_search_without_any_source_is_a_coverage_gap(self) -> None:
        client, _transport, _ledger, _clock = self.make(
            [httpx.Response(200, json=_ok_body(usage={"input_tokens": 10, "output_tokens": 5}))]
        )
        result = await self.ask(client)
        self.assertTrue(result.searched)
        self.assertTrue(any("no devolvio fuentes" in g["detail"] for g in result.coverage_gaps()))

    async def test_currency_required_without_a_search_declares_the_gap(self) -> None:
        body = {
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "sin busqueda"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client, _transport, _ledger, _clock = self.make([httpx.Response(200, json=body)])
        result = await self.ask(client, requires_currency=True)
        self.assertTrue(any("actualidad" in w for w in result.warnings))


class CitationTests(_Base):
    async def test_duplicate_sources_are_counted_once(self) -> None:
        results = [
            {"url": "https://example.org/a", "title": "A"},
            {"url": "https://example.org/a", "title": "A repetida"},
            {"url": "https://example.org/b"},
        ]
        client, _transport, _ledger, _clock = self.make(
            [httpx.Response(200, json=_ok_body(results=results, usage={"input_tokens": 1, "output_tokens": 1}))]
        )
        result = await self.ask(client)
        self.assertEqual([s.url for s in result.sources], ["https://example.org/a", "https://example.org/b"])
        self.assertEqual(result.duplicate_sources, 1)

    async def test_citations_without_results_are_flagged_as_derived_only(self) -> None:
        citations = [{"type": "url_citation", "url": "https://example.org/a", "title": "A", "start_index": 0, "end_index": 3}]
        body = _ok_body(searches=0, citations=citations, usage={"input_tokens": 1, "output_tokens": 1})
        body["output"].insert(0, {"type": "web_search_call", "id": "ws", "status": "completed", "action": {"type": "search"}})
        client, _transport, _ledger, _clock = self.make([httpx.Response(200, json=body)])
        result = await self.ask(client)
        self.assertEqual(len(result.citations), 1)
        self.assertEqual(result.sources, ())
        self.assertTrue(any("citas sin fuentes" in w for w in result.warnings))

    async def test_derived_evidence_has_no_content_hash(self) -> None:
        client, _transport, _ledger, _clock = self.make(
            [httpx.Response(200, json=_ok_body(results=[{"url": "https://example.org/a"}], usage={"input_tokens": 1, "output_tokens": 1}))]
        )
        result = await self.ask(client)
        rows = result.derived_evidence()
        self.assertEqual(len(rows), 1)
        # No se leyo el articulo: no puede afirmarse un hash de su contenido.
        self.assertIsNone(rows[0]["content_hash"])
        self.assertEqual(rows[0]["access_method"], "grounding")
        self.assertIn("abrir la pagina original", rows[0]["note"])


class CostTests(unittest.TestCase):
    def test_the_published_prices_are_represented_exactly(self) -> None:
        self.assertEqual(GROUNDING_SEARCH_MICRO_USD, 2_500)  # $0,0025
        self.assertEqual(INPUT_MICRO_PER_MILLION, 100_000)  # $0,10 / M
        self.assertEqual(OUTPUT_MICRO_PER_MILLION, 200_000)  # $0,20 / M
        self.assertEqual(CACHED_INPUT_MICRO_PER_MILLION, 2_000)  # $0,002 / M

    def test_a_subcent_search_is_not_rounded_away(self) -> None:
        self.assertEqual(token_cost_micro(input_tokens=0, output_tokens=0), 0)
        self.assertEqual(token_cost_micro(input_tokens=1, output_tokens=0), 1)  # 0,1 micro -> 1

    def test_cached_input_is_not_billed_twice(self) -> None:
        # 1.000 de entrada de los que 1.000 estan cacheados: solo se cobra la cache.
        cost = token_cost_micro(input_tokens=1_000, output_tokens=0, cached_tokens=1_000)
        self.assertEqual(cost, 2)  # 1.000 * 2.000 / 1e6 = 2 micro

    def test_missing_counters_are_unknown_not_zero(self) -> None:
        self.assertIsNone(token_cost_micro(input_tokens=None, output_tokens=None))


class AccountingTests(_Base):
    async def test_the_charge_comes_from_reported_usage(self) -> None:
        usage = {"input_tokens": 1_000, "output_tokens": 500, "input_tokens_details": {"cached_tokens": 200}}
        client, _transport, ledger, _clock = self.make([httpx.Response(200, json=_ok_body(usage=usage))])
        result = await self.ask(client)
        # 1 busqueda (2.500) + tokens (80 + 0,4 -> 1 + 100 = 181) = 2.681 micro.
        self.assertEqual(result.reconciled_micro_usd, 2_681)
        self.assertEqual(result.charged_micro_usd, 2_681)
        self.assertFalse(result.usage_uncertain)
        self.assertEqual(ledger.amounts("commit"), [2_681])

    async def test_without_reported_usage_the_amount_stays_uncertain(self) -> None:
        client, _transport, ledger, _clock = self.make([httpx.Response(200, json=_ok_body(usage=None))])
        result = await self.ask(client)
        self.assertIsNone(result.reconciled_micro_usd)
        self.assertIsNone(result.charged_micro_usd)
        self.assertTrue(result.usage_uncertain)
        self.assertIn("mark_uncertain", ledger.kinds())
        self.assertEqual(ledger.amounts("commit"), [])

    async def test_searches_without_a_token_counter_are_uncertain_not_guessed(self) -> None:
        usage = {"input_tokens": None, "output_tokens": None}
        client, _transport, ledger, _clock = self.make([httpx.Response(200, json=_ok_body(usage=usage))])
        result = await self.ask(client)
        self.assertIsNone(result.reconciled_micro_usd)
        self.assertIn("mark_uncertain", ledger.kinds())

    async def test_no_hard_cap_is_promised(self) -> None:
        client, _transport, _ledger, _clock = self.make([httpx.Response(200, json=_ok_body(usage={"input_tokens": 1, "output_tokens": 1}))])
        result = await self.ask(client)
        self.assertFalse(result.hard_cap)
        self.assertFalse(result.to_payload()["usage"]["hard_cap"])

    async def test_the_reservation_uses_the_configured_ceiling(self) -> None:
        client, _transport, ledger, _clock = self.make(
            [httpx.Response(200, json=_ok_body(usage={"input_tokens": 1, "output_tokens": 1}))],
            budget="2.50",
        )
        await self.ask(client)
        reserve = ledger.amounts("reserve")[0]
        self.assertEqual(reserve["ceiling_micro_usd"], 2_500_000)
        self.assertEqual(reserve["amount_micro_usd"], GROUNDING_SEARCH_MICRO_USD)


class GuardTests(_Base):
    async def test_zero_paid_budget_means_zero_calls(self) -> None:
        ledger = _FakeLedger()
        client, transport, ledger, _clock = self.make(
            [httpx.Response(200, json=_ok_body())], budget="0.00", ledger=ledger
        )
        result = await self.ask(client)
        self.assertEqual(transport.requests, [])
        self.assertEqual(ledger.calls, [])
        self.assertTrue(result.degradations)
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.BUDGET_EXHAUSTED)

    async def test_unknown_budget_does_not_authorize_spend(self) -> None:
        client, transport, _ledger, _clock = self.make([httpx.Response(200, json=_ok_body())], budget="")
        result = await self.ask(client)
        self.assertEqual(transport.requests, [])
        self.assertTrue(result.degradations)

    async def test_restricted_data_never_reaches_contributor(self) -> None:
        client, transport, ledger, _clock = self.make([httpx.Response(200, json=_ok_body())])
        result = await self.ask(client, classification=Classification.RESTRICTED)
        self.assertEqual(transport.requests, [])
        self.assertEqual(ledger.calls, [])
        self.assertTrue(result.degradations)
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.MODEL_POLICY_UNAVAILABLE)
        self.assertIn("no hay fallback", result.degradations[0].detail)

    async def test_private_data_never_reaches_contributor(self) -> None:
        client, transport, _ledger, _clock = self.make([httpx.Response(200, json=_ok_body())])
        result = await self.ask(client, classification=Classification.PRIVATE)
        self.assertEqual(transport.requests, [])
        self.assertTrue(result.degradations)

    async def test_disabled_adapter_is_structured(self) -> None:
        client, transport, _ledger, _clock = self.make([httpx.Response(200, json=_ok_body())], enabled=False)
        result = await self.ask(client)
        self.assertEqual(transport.requests, [])
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.SOURCE_UNAVAILABLE)
        self.assertIn("deshabilitado", result.degradations[0].detail)

    async def test_missing_key_is_structured(self) -> None:
        client, transport, _ledger, _clock = self.make([httpx.Response(200, json=_ok_body())], key="")
        result = await self.ask(client)
        self.assertEqual(transport.requests, [])
        self.assertTrue(result.degradations)

    async def test_without_a_ledger_a_paid_call_is_refused(self) -> None:
        transport = _Transport([httpx.Response(200, json=_ok_body())])
        http = httpx.AsyncClient(transport=httpx.MockTransport(transport.handler))
        client = GroundingClient(
            settings=None,
            http_client=http,
            ledger=None,
            adapter_enabled=True,
            api_key="k",
            monthly_budget_micro_usd="1.00",
        )
        result = await self.ask(client)
        self.assertEqual(transport.requests, [])
        self.assertTrue(result.degradations)
        self.assertIn("contabilidad", result.degradations[0].detail)

    async def test_a_ledger_refusal_is_structured_and_does_not_call(self) -> None:
        ledger = _FakeLedger(accept=False, reason="account_ceiling")
        client, transport, _ledger, _clock = self.make(
            [httpx.Response(200, json=_ok_body())], ledger=ledger
        )
        result = await self.ask(client)
        self.assertEqual(transport.requests, [])
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.BUDGET_EXHAUSTED)
        self.assertIn("account_ceiling", result.degradations[0].detail)


class FailureTests(_Base):
    async def test_a_server_error_is_retried_within_the_limit(self) -> None:
        client, transport, ledger, _clock = self.make(
            [
                httpx.Response(500, json={}),
                httpx.Response(500, json={}),
                httpx.Response(200, json=_ok_body(usage={"input_tokens": 1, "output_tokens": 1})),
            ]
        )
        result = await self.ask(client)
        self.assertEqual(len(transport.requests), 3)
        self.assertTrue(result.searched)
        self.assertEqual([a.outcome for a in result.attempts], ["released", "released", "committed"])

    async def test_a_connect_error_is_retried(self) -> None:
        client, transport, _ledger, _clock = self.make(
            [
                httpx.ConnectError("sin ruta"),
                httpx.Response(200, json=_ok_body(usage={"input_tokens": 1, "output_tokens": 1})),
            ]
        )
        result = await self.ask(client)
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(result.attempts[0].outcome, "released")

    async def test_an_ambiguous_timeout_is_never_retried_and_never_free(self) -> None:
        client, transport, ledger, _clock = self.make([httpx.ReadTimeout("sin respuesta")])
        result = await self.ask(client)
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(result.usage_uncertain)
        self.assertIn("mark_uncertain", ledger.kinds())
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.SOURCE_UNAVAILABLE)

    async def test_402_is_quota_exhausted_without_retry(self) -> None:
        client, transport, _ledger, _clock = self.make([httpx.Response(402, json={})])
        result = await self.ask(client)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.QUOTA_EXHAUSTED)

    async def test_401_is_an_auth_failure_without_retry(self) -> None:
        client, transport, _ledger, _clock = self.make([httpx.Response(401, json={})])
        result = await self.ask(client)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(result.degradations[0].coverage_reason, CoverageReason.ACCESS_DENIED)

    async def test_429_honours_retry_after(self) -> None:
        client, transport, _ledger, clock = self.make(
            [
                httpx.Response(429, headers={"retry-after": "7"}, json={}),
                httpx.Response(200, json=_ok_body(usage={"input_tokens": 1, "output_tokens": 1})),
            ]
        )
        await self.ask(client)
        self.assertEqual(clock.slept, [7.0])
        self.assertEqual(len(transport.requests), 2)

    async def test_a_broken_body_is_a_parse_error_that_still_charges(self) -> None:
        client, _transport, ledger, _clock = self.make([httpx.Response(200, text="no json", headers={"content-type": "text/plain"})])
        result = await self.ask(client)
        self.assertTrue(result.degradations)
        # La llamada ocurrio: se liquida el estimado, no se contabiliza como gratis.
        self.assertEqual(ledger.amounts("commit"), [GROUNDING_SEARCH_MICRO_USD])

    async def test_an_expired_deadline_does_not_call(self) -> None:
        from research.acquisition.base import Deadline

        client, transport, _ledger, clock = self.make([httpx.Response(200, json=_ok_body())])
        deadline = Deadline(1.0, clock=clock)
        clock.advance(5.0)
        result = await self.ask(client, deadline=deadline)
        self.assertEqual(transport.requests, [])
        self.assertTrue(result.degradations)


if __name__ == "__main__":
    unittest.main()
