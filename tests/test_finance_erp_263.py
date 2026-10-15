"""Finance migration to Jardal ERP (TASK-2026-00263).

Mocks and synthetic fixtures only; no live models, no network, no real money.
"""
import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from agents.finance import erp_operations as ops
from agents.finance.money_text import MissingExchangeRateError
from agents.finance.voice_expense import ParsedExpense, parse_expense
from agents.finance.voice_income import ParsedIncome, parse_income
from business.errors import BusinessPermissionError, BusinessUnavailable, BusinessValidationError
from business.finance import JardalERPFinance, finance_provider_is_erp


def settings(**changes):
    base = dict(
        FINANCE_PROVIDER="erpnext",
        BUSINESS_PROVIDER="erpnext",
        BUSINESS_AUTHORITY_FILE="",
        JARDAL_ERP_URL="http://127.0.0.1:31874",
        JARDAL_ERP_SERVICE_TOKEN="synthetic-service-token",
        JARDAL_ERP_WORKSPACE_ID="synthetic-workspace",
        ERPNEXT_TIMEOUT_SECONDS=1,
        ERPNEXT_CONNECT_TIMEOUT_SECONDS=1,
        MONGODB_DATABASE="synthetic_exec_db",
    )
    base.update(changes)
    return SimpleNamespace(**base)


def erp_settings(tmp_path, **changes):
    manifest = tmp_path / "authority.json"
    manifest.write_text(json.dumps({"schema_version": 1, "provider": "jardal_erp", "state": "ready",
                                    "workspace_id": "synthetic-workspace",
                                    "origin": "http://127.0.0.1:31874",
                                    "execution_database": "synthetic_exec_db"}))
    return settings(FINANCE_PROVIDER="jardal_erp", BUSINESS_AUTHORITY_FILE=str(manifest), **changes)


def parsed_expense(**changes):
    base = dict(concept="carne", amount=Decimal("50.00"), currency="BOB",
                expense_account="Gastos varios - J", posting_date="2026-10-14",
                remark="synthetic", exchange_rate=None)
    base.update(changes)
    return ParsedExpense(**base)


def parsed_income(**changes):
    base = dict(concept="sueldo", amount=Decimal("6200.00"), currency="BOB",
                income_account="Servicios - J", posting_date="2026-10-14",
                remark="synthetic", exchange_rate=None)
    base.update(changes)
    return ParsedIncome(**base)


def test_operation_dtos_are_closed_decimal_and_never_equate_scope():
    expense = ops.build_expense_operation(parsed_expense(), "op-1")
    assert expense["amount"] == "50.00" and expense["currency"] == "BOB"
    assert expense["nature"] == "expense" and expense["owner"] == "Jardal"
    assert expense["scope"] is None and expense["cost_center"] == "Principal - J"
    assert "exchange_rate" not in expense
    income = ops.build_income_operation(parsed_income(), "op-2", scope="synthetic-scope")
    assert income["nature"] == "income" and income["scope"] == "synthetic-scope"
    usdt = ops.build_expense_operation(
        parsed_expense(amount=Decimal("100.00"), currency="USDT", exchange_rate=Decimal("6.96")), "op-3")
    assert usdt["currency"] == "USDT" and usdt["amount"] == "100.00"
    assert usdt["company_amount"] == "696.00" and usdt["exchange_rate"] == "6.96"
    with pytest.raises(MissingExchangeRateError):
        ops.build_expense_operation(parsed_expense(currency="USD"), "op-4")
    with pytest.raises(MissingExchangeRateError):
        ops.build_income_operation(parsed_income(currency="USDT"), "op-5")


def test_core_parsing_stays_untouched():
    assert parse_expense("registra gasto personal carne 50 bolivianos").amount == Decimal("50")
    assert parse_income("registra ingreso sueldo 6200 bolivianos").amount == Decimal("6200")
    assert parse_expense("cuánto gastamos este mes") is None


def test_record_paths_keys_duplicate_and_replay(tmp_path):
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            if len(requests) == 1:
                raise httpx.ReadTimeout("synthetic timeout after effect", request=request)
            return httpx.Response(200, json={"operation": {"id": "fin-1", "entry": "JE-1",
                                                           "state": "posted"},
                                             "duplicate": True, "ref": "op-1"})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            result = await ops.register_expense_erp(adapter, parsed_expense(), ref="op-1")
        finally:
            await adapter.aclose()
        assert result["ok"] is True and result["duplicate"] is True
        assert result["entry"] == "JE-1" and "no lo dupliqué" in result["summary"]
        assert len(requests) == 2
        assert requests[0].url.path == "/api/v1/finance/expenses"
        assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"] == "op-1"
        assert requests[0].content == requests[1].content
        body = json.loads(requests[0].content)
        assert body["amount"] == "50.00" and body["currency"] == "BOB"
    asyncio.run(run())


def test_record_income_and_hard_failures(tmp_path):
    async def run():
        seen = []

        def handle(request):
            seen.append(request)
            return httpx.Response(200, json={"operation": {"id": "fin-2", "entry": "JE-2",
                                                           "state": "posted"}})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            result = await ops.register_income_erp(adapter, parsed_income(), ref="op-2")
            assert result["duplicate"] is False and result["entry"] == "JE-2"
            assert seen[0].url.path == "/api/v1/finance/incomes"
        finally:
            await adapter.aclose()

        def handle_401(request):
            return httpx.Response(401, json={"code": "UNAUTHORIZED"})

        denied = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle_401))
        try:
            with pytest.raises(BusinessPermissionError):
                await denied.record_expense({"amount": "1.00", "currency": "BOB",
                                             "account": "a", "date": "2026-10-14",
                                             "ref": "op-x"}, key="op-x")
        finally:
            await denied.aclose()

        def handle_422(request):
            return httpx.Response(422, json={"code": "BAD"})

        bad = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle_422))
        try:
            with pytest.raises(BusinessValidationError):
                await bad.record_income({"amount": "1.00", "currency": "BOB",
                                         "account": "a", "date": "2026-10-14",
                                         "ref": "op-y"}, key="op-y")
        finally:
            await bad.aclose()

        with pytest.raises(BusinessValidationError):
            await bad.record_expense({"amount": "no-decimal", "currency": "BOB",
                                      "account": "a", "date": "2026-10-14",
                                      "ref": "op-z"}, key="op-z")
    asyncio.run(run())


ERP_ENTRIES = [
    {"account": "Servicios - J", "currency": "BOB", "debit": "0", "credit": "6200.00",
     "date": "2026-10-05", "owner": "Jardal", "entry": "JE-2", "state": "posted"},
    {"account": "Gastos varios - J", "currency": "BOB", "debit": "50.00", "credit": "0",
     "date": "2026-10-06", "owner": "Jardal", "entry": "JE-1", "state": "posted"},
    {"account": "Gastos varios - J", "currency": "USD", "debit": "10.00", "credit": "0",
     "date": "2026-10-07", "owner": "Jardal", "entry": "JE-3", "state": "posted"},
]


def test_scope_summary_reuses_core_algorithm_over_entries(tmp_path):
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"as_of": "2026-10-14T12:00:00-04:00",
                                             "entries": ERP_ENTRIES})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            payload = await ops.scope_summary_erp(
                adapter, company="Jardal", cost_center="Principal - J", currency="BOB",
                date_from="2026-10-01", date_to="2026-10-14")
        finally:
            await adapter.aclose()
        assert payload["provider"] == "jardal_erp"
        assert payload["total_debit"] == "50.00" and payload["total_credit"] == "6200.00"
        assert payload["net"] == "-6150.00" and payload["entries"] == 2
        assert payload["excluded_entries"] == 1
        assert payload["voucher_nos"] == ["JE-2", "JE-1"]
        assert "netos" in payload["summary"]
        query = dict(requests[0].url.params)
        assert query["currency"] == "BOB" and query["date_from"] == "2026-10-01"
        assert query["date_to"] == "2026-10-14"
    asyncio.run(run())


def test_scope_summary_degrades_explicitly_on_aggregates_only(tmp_path):
    async def run():
        def handle(request):
            return httpx.Response(200, json={"as_of": "2026-10-14T12:00:00-04:00",
                                             "totals": {"debit": "50.00", "credit": "6200.00",
                                                        "net": "-6150.00"}})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            payload = await ops.scope_summary_erp(
                adapter, company="Jardal", cost_center="Principal - J", currency="BOB",
                date_from=None, date_to=None)
        finally:
            await adapter.aclose()
        assert payload["net"] == "-6150.00" and payload["provider"] == "jardal_erp"
        assert "agregados sin entradas" in payload["notes"][0]
        assert "Ojo" in payload["summary"]
    asyncio.run(run())


def test_contribution_single_computation_and_honest_unknowns(tmp_path):
    async def run():
        def handle(request):
            return httpx.Response(200, json={"entries": ERP_ENTRIES})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            payload = await ops.contribution_status_erp(
                adapter, company="Jardal", cost_center="Principal - J", currency="BOB",
                date_from="2026-10-01", date_to="2026-10-31")
        finally:
            await adapter.aclose()
        assert payload["eligible_income"] == "6200.00"
        assert payload["target_contribution"] == "620.00"
        assert payload["confirmed_contribution"] is None
        assert payload["state"] == "needs_input"
        assert payload["provider"] == "jardal_erp"

        def handle_bare(request):
            return httpx.Response(200, json={"as_of": "2026-10-14T12:00:00-04:00"})

        bare = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle_bare))
        try:
            payload = await ops.contribution_status_erp(
                bare, company="Jardal", cost_center="Principal - J", currency="BOB",
                date_from="2026-10-01", date_to="2026-10-31")
        finally:
            await bare.aclose()
        assert payload["state"] == "needs_input"
        assert payload["confirmed_contribution"] is None
        assert "no entrega entradas fechadas" in payload["reason"]
    asyncio.run(run())


def test_review_fixes_ref_submit_shape_and_status(tmp_path):
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"operation": {"id": "fin-1", "entry": "JE-9",
                                                           "state": "posted"}})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            generated = await ops.register_expense_erp(adapter, parsed_expense(), ref=None)
            assert generated["ref"].startswith("voice-expense:")
            assert requests[0].headers["Idempotency-Key"] == generated["ref"]
            assert generated["journal_entry"] == "JE-9" == generated["entry"]
        finally:
            await adapter.aclose()

        from workflow_service import WorkflowToolRegistry

        service = WorkflowToolRegistry.__new__(WorkflowToolRegistry)
        service._settings = erp_settings(tmp_path)
        draft = await service._finance_register_expense_erp(parsed_expense(), ref="op-d", submit=False)
        assert draft["status"] == "rejected" and draft["error"] == "draft_unsupported"
        assert service._finance_erp_write_status(BusinessPermissionError("x", status_code=401)) == "unavailable"
        assert service._finance_erp_write_status(BusinessUnavailable("x")) == "unavailable"
        assert service._finance_erp_write_status(BusinessValidationError("x", status_code=422)) == "rejected"
    asyncio.run(run())


def test_review_fixes_rows_totals_and_adapter_rate(tmp_path):
    assert ops._decimal_text(None) == "0"
    assert ops._decimal_text(0) == "0" and ops._decimal_text(50) == "50"
    assert ops._decimal_text(Decimal("50.10")) == "50.10"
    assert ops._decimal_text("  ") == "0"
    draft = ops.map_dated_row({"account": "a", "state": "draft", "debit": 0})
    assert draft["docstatus"] == 0 and draft["debit_in_account_currency"] == "0"
    cancelled = ops.map_dated_row({"account": "a", "state": "cancelled"})
    assert cancelled["is_cancelled"] == 1
    with pytest.raises(ValueError):
        ops._row_matches({"a": "1"}, [["a", "LIKE", "1"]])

    async def run():
        def handle(request):
            return httpx.Response(200, json={"as_of": "x"})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            payload = await ops.scope_summary_erp(
                adapter, company="Jardal", cost_center="Principal - J", currency="BOB",
                date_from=None, date_to=None)
            assert payload["state"] == "needs_input" and payload["net"] is None
            assert "no entrega totales" in payload["reason"]
        finally:
            await adapter.aclose()

        def handle_bad(request):
            return httpx.Response(200, json={"totals": {"debit": "xx", "credit": "yy"}})

        bad = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle_bad))
        try:
            payload = await ops.scope_summary_erp(
                bad, company="Jardal", cost_center="Principal - J", currency="BOB",
                date_from=None, date_to=None)
            assert payload["state"] == "needs_input" and payload["net"] is None
        finally:
            await bad.aclose()

        direct = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(BusinessValidationError):
                await direct.record_expense({"amount": "10.00", "currency": "USDT",
                                             "company_currency": "BOB", "account": "a",
                                             "date": "2026-10-14", "ref": "op-r"}, key="op-r")
            with pytest.raises(BusinessValidationError):
                await direct.record_expense({"amount": "10.00", "currency": "USDT",
                                             "company_currency": "BOB", "exchange_rate": "0",
                                             "account": "a", "date": "2026-10-14",
                                             "ref": "op-r"}, key="op-r")
        finally:
            await direct.aclose()
    asyncio.run(run())


def test_provider_gating_and_reconciliation_filters(tmp_path):
    assert finance_provider_is_erp(settings()) is False
    with pytest.raises(ValueError):
        finance_provider_is_erp(settings(FINANCE_PROVIDER="jardal_erp"))
    assert finance_provider_is_erp(erp_settings(tmp_path)) is True

    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"as_of": "x", "totals": {}})

        adapter = JardalERPFinance(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            await adapter.scope_summary(currency="USDT", account="Caja", date_from="2026-10-01",
                                        date_to="2026-10-14", unknown_filter="dropped")
            query = dict(requests[0].url.params)
            assert query["currency"] == "USDT" and query["account"] == "Caja"
            assert "unknown_filter" not in query
            await adapter.cash_position(currency="BOB")
            assert requests[1].url.path == "/api/v1/finance/cash-position"
        finally:
            await adapter.aclose()
    asyncio.run(run())
