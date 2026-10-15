"""Business finance against Jardal ERP (TASK-2026-00263)."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

import httpx

from business.errors import BusinessValidationError
from business.http import JardalERPClient, erp_service_configured
from business.provider import validate_authority_manifest


def finance_provider_is_erp(settings: Any) -> bool:
    if str(getattr(settings, "FINANCE_PROVIDER", "erpnext")).strip() != "jardal_erp":
        return False
    validate_authority_manifest(settings)
    if not erp_service_configured(settings):
        raise ValueError("Finanzas jardal_erp sin config de servicio completa.")
    return True


def _operation_view(result: Any) -> dict[str, Any]:
    node = result.get("operation", result) if isinstance(result, dict) else None
    if not isinstance(node, dict) or not str(node.get("id") or "").strip():
        raise BusinessValidationError("Respuesta financiera inválida.", status_code=502)
    return {
        "id": str(node.get("id")),
        "entry": str(node.get("entry") or node.get("document") or ""),
        "state": str(node.get("state") or node.get("status") or "recorded"),
        "duplicate": bool(result.get("duplicate", node.get("duplicate", False))),
        "ref": str(node.get("ref") or result.get("ref") or ""),
    }


def _clean_filters(filters: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("currency", "account", "scope", "cost_center", "owner", "book",
                "date_from", "date_to", "as_of", "limit"):
        value = filters.get(key)
        if value is None or value == "":
            continue
        out[key] = value
    return out


class JardalERPFinance:
    provider = "jardal_erp"

    def __init__(self, settings: Any, client: JardalERPClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self.client = client or JardalERPClient(settings, transport=transport)
        self._owns_client = client is None

    @property
    def is_enabled(self) -> bool:
        return erp_service_configured(self._settings)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    @staticmethod
    def _checked_operation(operation: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(operation, dict):
            raise BusinessValidationError("Operación financiera inválida.", status_code=400)
        for field in ("amount", "currency", "account", "date", "ref"):
            if not str(operation.get(field) or "").strip():
                raise BusinessValidationError(f"Operación sin {field}.", status_code=400)
        try:
            amount = Decimal(str(operation["amount"]))
        except (InvalidOperation, ValueError):
            raise BusinessValidationError("Monto no decimal.", status_code=400) from None
        if amount <= 0:
            raise BusinessValidationError("Monto no positivo.", status_code=400)
        company_currency = str(operation.get("company_currency") or "BOB").strip().upper()
        if str(operation.get("currency") or "").strip().upper() != company_currency:
            rate = operation.get("exchange_rate")
            if rate is None or str(rate).strip() == "":
                raise BusinessValidationError("Falta tasa real confirmada.", status_code=400)
            try:
                rate_value = Decimal(str(rate))
            except (InvalidOperation, ValueError):
                raise BusinessValidationError("Tasa no decimal.", status_code=400) from None
            if rate_value <= 0:
                raise BusinessValidationError("Tasa no positiva.", status_code=400)
        return operation

    async def record_income(self, operation: dict[str, Any], *, key: str) -> dict[str, Any]:
        result = await self.client.request("POST", "/api/v1/finance/incomes",
                                           body=self._checked_operation(operation), key=key)
        return _operation_view(result)

    async def record_expense(self, operation: dict[str, Any], *, key: str) -> dict[str, Any]:
        result = await self.client.request("POST", "/api/v1/finance/expenses",
                                           body=self._checked_operation(operation), key=key)
        return _operation_view(result)

    async def scope_summary(self, **filters: Any) -> dict[str, Any]:
        params = _clean_filters(filters)
        path = "/api/v1/finance/scope-summary"
        if filters.get("scope"):
            path = "/api/v1/finance/scope-summary/" + quote(str(filters["scope"]), safe="")
            params.pop("scope", None)
        result = await self.client.request("GET", path, params=params or None)
        if not isinstance(result, dict):
            raise BusinessValidationError("Resumen financiero inválido.", status_code=502)
        return result

    async def cash_position(self, **filters: Any) -> dict[str, Any]:
        result = await self.client.request("GET", "/api/v1/finance/cash-position",
                                           params=_clean_filters(filters) or None)
        if not isinstance(result, dict):
            raise BusinessValidationError("Posición de caja inválida.", status_code=502)
        return result

    async def dated_entries(self, **filters: Any) -> list[dict[str, Any]] | None:
        """Dated ledger entries for Core-side computation, or None if absent."""
        report = await self.scope_summary(**filters)
        entries = report.get("entries", report.get("journal_entries"))
        if entries is None:
            return None
        if not isinstance(entries, list):
            raise BusinessValidationError("Entradas financieras inválidas.", status_code=502)
        return [row for row in entries if isinstance(row, dict)]
