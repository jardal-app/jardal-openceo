"""Finance operations against Jardal ERP (TASK-2026-00263).

Core keeps interpretation, classification, Decimal policy and explanation
(the `voice_*`, `money_text`, `scope_summary` and `contribution` modules are
untouched); the ERP owns ledger, reports, wallets and taxes. Operations use
closed DTOs; reads reuse the Core algorithms over mapped dated entries.
`execute_money_movements=false` still holds: this books past movements only.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from agents.finance.money_text import COMPANY_CURRENCY, MissingExchangeRateError, company_amount


def build_expense_operation(parsed: Any, ref: str, *, scope: str | None = None) -> dict[str, Any]:
    """Closed ERP expense DTO. Never equates Scope with Cost Center."""
    company_total = company_amount(parsed.amount, parsed.currency, parsed.exchange_rate)
    operation: dict[str, Any] = {
        "amount": f"{parsed.amount:.2f}",
        "currency": parsed.currency,
        "company_amount": f"{company_total:.2f}",
        "company_currency": COMPANY_CURRENCY,
        "owner": parsed.company,
        "nature": "expense",
        "account": parsed.expense_account,
        "counterpart": parsed.cash_account,
        "cost_center": parsed.cost_center,
        "scope": scope,
        "date": parsed.posting_date,
        "concept": parsed.concept,
        "remark": parsed.remark,
        "ref": ref,
    }
    if str(parsed.currency or "").upper() != COMPANY_CURRENCY:
        if parsed.exchange_rate is None:
            raise MissingExchangeRateError(str(parsed.currency))
        operation["exchange_rate"] = f"{Decimal(parsed.exchange_rate).normalize():f}"
    return operation


def build_income_operation(parsed: Any, ref: str, *, scope: str | None = None) -> dict[str, Any]:
    """Closed ERP income DTO. Never equates Scope with Cost Center."""
    company_total = company_amount(parsed.amount, parsed.currency, parsed.exchange_rate)
    operation: dict[str, Any] = {
        "amount": f"{parsed.amount:.2f}",
        "currency": parsed.currency,
        "company_amount": f"{company_total:.2f}",
        "company_currency": COMPANY_CURRENCY,
        "owner": parsed.company,
        "nature": "income",
        "account": parsed.income_account,
        "counterpart": parsed.cash_account,
        "cost_center": parsed.cost_center,
        "scope": scope,
        "date": parsed.posting_date,
        "concept": parsed.concept,
        "remark": parsed.remark,
        "ref": ref,
    }
    if str(parsed.currency or "").upper() != COMPANY_CURRENCY:
        if parsed.exchange_rate is None:
            raise MissingExchangeRateError(str(parsed.currency))
        operation["exchange_rate"] = f"{Decimal(parsed.exchange_rate).normalize():f}"
    return operation


async def register_expense_erp(adapter: Any, parsed: Any, *, ref: str | None = None,
                               scope: str | None = None) -> dict[str, Any]:
    """Book an expense in the ERP. Idempotent by ref; same result shape."""
    from agents.finance import voice_expense as _voice_expense

    operation_ref = (ref or "").strip() or _voice_expense.new_openceo_ref()
    result = await adapter.record_expense(
        build_expense_operation(parsed, operation_ref, scope=scope), key=operation_ref)
    doc = {"name": result.get("entry") or "", "state": result.get("state") or ""}
    return {"ok": True, "duplicate": bool(result.get("duplicate")), "ref": operation_ref,
            "entry": result.get("entry") or "", "journal_entry": result.get("entry") or "",
            "state": result.get("state") or "",
            "summary": _voice_expense.build_spoken_summary(parsed, doc, bool(result.get("duplicate")))}


async def register_income_erp(adapter: Any, parsed: Any, *, ref: str | None = None,
                              scope: str | None = None) -> dict[str, Any]:
    """Book an income in the ERP. Idempotent by ref; same result shape."""
    from agents.finance import voice_income as _voice_income

    operation_ref = (ref or "").strip() or _voice_income.new_openceo_ref()
    result = await adapter.record_income(
        build_income_operation(parsed, operation_ref, scope=scope), key=operation_ref)
    doc = {"name": result.get("entry") or "", "state": result.get("state") or ""}
    return {"ok": True, "duplicate": bool(result.get("duplicate")), "ref": operation_ref,
            "entry": result.get("entry") or "", "journal_entry": result.get("entry") or "",
            "state": result.get("state") or "",
            "summary": _voice_income.build_spoken_summary(parsed, doc, bool(result.get("duplicate")))}


def _decimal_text(value: Any) -> str:
    if value is None:
        return "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, Decimal):
        return f"{value:f}"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).strip()
    return text if text else "0"


def map_dated_row(row: dict[str, Any]) -> dict[str, Any]:
    """ERP dated entry -> GL-shaped row for the Core algorithms."""
    state = str(row.get("state") or row.get("status") or "").strip().lower()
    posted = state in {"", "posted", "submitted", "accounted"}
    return {
        "account": str(row.get("account") or ""),
        "account_currency": str(row.get("currency") or row.get("account_currency") or ""),
        "debit_in_account_currency": _decimal_text(
            row.get("debit", row.get("debit_in_account_currency"))),
        "credit_in_account_currency": _decimal_text(
            row.get("credit", row.get("credit_in_account_currency"))),
        "posting_date": str(row.get("date") or row.get("posting_date") or ""),
        "cost_center": row.get("cost_center"),
        "company": str(row.get("owner") or row.get("company") or ""),
        "voucher_no": str(row.get("entry") or row.get("voucher_no") or row.get("id") or ""),
        "is_cancelled": 1 if state in {"cancelled", "void"} else 0,
        "docstatus": 1 if posted else 0,
    }


class _MappedGLEntries:
    """Minimal GL reader over mapped ERP rows for the Core algorithms."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def list_gl_entries(self, *, filters: list[list[Any]] | None = None,
                              fields: list[str] | None = None, limit: int = 500) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in self._rows:
            if not _row_matches(row, filters or []):
                continue
            out.append({key: row.get(key) for key in (fields or list(row))})
            if len(out) >= limit:
                break
        return out


def _row_matches(row: dict[str, Any], filters: list[list[Any]]) -> bool:
    for filtr in filters:
        if len(filtr) != 3:
            continue
        field, operator, expected = filtr
        if operator not in {"=", ">=", "<="}:
            raise ValueError(f"Operador de filtro no soportado: {operator}.")
        actual = row.get(field)
        text = "" if actual is None else str(actual)
        if operator == "=" and text != str(expected):
            return False
        if operator == ">=" and text < str(expected):
            return False
        if operator == "<=" and text > str(expected):
            return False
    return True


async def scope_summary_erp(adapter: Any, *, company: str, cost_center: str | None,
                            currency: str, date_from: str | None, date_to: str | None,
                            limit: int = 500) -> dict[str, Any]:
    """Scope summary from the ERP, reusing the Core algorithm when entries exist."""
    from agents.finance import scope_summary as _scope

    report = await adapter.scope_summary(currency=currency, cost_center=cost_center,
                                         date_from=date_from, date_to=date_to, limit=limit)
    raw_entries = report.get("entries", report.get("journal_entries"))
    if isinstance(raw_entries, list):
        rows = [map_dated_row(row) for row in raw_entries if isinstance(row, dict)]
        carries_cc = any(row.get("cost_center") for row in rows)
        summary = await _scope.get_scope_summary(
            _MappedGLEntries(rows), company=company,
            cost_center=cost_center if carries_cc else None,
            currency=currency, date_from=date_from, date_to=date_to, limit=limit)
        payload = summary.to_dict()
        payload["provider"] = "jardal_erp"
        if cost_center and not carries_cc:
            payload["cost_center"] = cost_center
            payload["notes"] = ["Filtrado por centro de costo aplicado por el ERP; las entradas no traen el eje."]
        payload["summary"] = _scope.build_spoken_summary(summary)
        return payload
    totals = report.get("totals", {}) if isinstance(report, dict) else {}
    by_account = report.get("by_account", {}) if isinstance(report, dict) else {}
    if not isinstance(totals, dict) or "debit" not in totals or "credit" not in totals:
        return {
            "as_of": report.get("as_of"),
            "company": company,
            "currency": currency,
            "cost_center": cost_center,
            "date_from": date_from,
            "date_to": date_to,
            "net": None,
            "state": "needs_input",
            "reason": "El ERP no entrega totales para el período; el resumen no es medible todavía.",
            "provider": "jardal_erp",
            "summary": ("Todavía no podemos resumir el período: el ERP no entrega "
                        "totales ni entradas fechadas."),
        }
    try:
        net_value = totals.get("net")
        net = f"{Decimal(str(net_value)):.2f}" if net_value is not None else \
            f"{(Decimal(str(totals['debit'])) - Decimal(str(totals['credit']))):.2f}"
        debit = f"{Decimal(str(totals['debit'])):.2f}"
        credit = f"{Decimal(str(totals['credit'])):.2f}"
    except Exception:
        return {
            "as_of": report.get("as_of"),
            "company": company,
            "currency": currency,
            "cost_center": cost_center,
            "date_from": date_from,
            "date_to": date_to,
            "net": None,
            "state": "needs_input",
            "reason": "El ERP entrega totales no decimales; el resumen no es medible todavía.",
            "provider": "jardal_erp",
            "summary": ("Todavía no podemos resumir el período: el ERP entrega "
                        "totales no decimales."),
        }
    payload = {
        "as_of": report.get("as_of"),
        "company": company,
        "currency": currency,
        "cost_center": cost_center,
        "date_from": date_from,
        "date_to": date_to,
        "total_debit": debit,
        "total_credit": credit,
        "net": net,
        "entries": report.get("entries_count"),
        "excluded_entries": 0,
        "limit_reached": False,
        "by_account": {str(k): str(v) for k, v in (by_account or {}).items()} if isinstance(by_account, dict) else {},
        "voucher_nos": [],
        "provider": "jardal_erp",
        "notes": ["ERP entrega agregados sin entradas fechadas; vouchers y conteo exacto no disponibles."],
    }
    payload["summary"] = (
        f"Tenemos {payload['net']} {currency} netos"
        f"{' en ' + cost_center if cost_center else ''}"
        f"{' del ' + str(date_from) + ' al ' + str(date_to) if date_from and date_to else ' del histórico consultado'} "
        f"según el ERP. Ojo: {payload['notes'][0]}")
    return payload


async def contribution_status_erp(adapter: Any, *, company: str, cost_center: str | None,
                                  currency: str, date_from: str | None, date_to: str | None,
                                  limit: int = 500) -> dict[str, Any]:
    """10% rule from the ERP. Single computation, still in Core; no duplicate."""
    from agents.finance import contribution as _contribution

    entries = await adapter.dated_entries(currency=currency, cost_center=cost_center,
                                          date_from=date_from, date_to=date_to, limit=limit)
    if entries is None:
        period_from = date_from
        period_to = date_to
        if not (period_from and period_to):
            period_from, period_to = _contribution.current_month_period()
        status = _contribution.ContributionStatus(
            as_of=_contribution.now_local_iso(), company=company, currency=currency,
            cost_center=cost_center, date_from=period_from, date_to=period_to,
            state="needs_input",
            reason="El ERP no entrega entradas fechadas para el período; la regla no es medible todavía.",
        )
        payload = status.to_dict()
        payload["provider"] = "jardal_erp"
        payload["summary"] = _contribution.build_spoken_summary(status)
        return payload
    rows = [map_dated_row(row) for row in entries]
    carries_cc = any(row.get("cost_center") for row in rows)
    status = await _contribution.get_contribution_10_status(
        _MappedGLEntries(rows), company=company,
        cost_center=cost_center if carries_cc else None,
        currency=currency, date_from=date_from, date_to=date_to, limit=limit)
    payload = status.to_dict()
    payload["provider"] = "jardal_erp"
    if cost_center and not carries_cc:
        payload["cost_center"] = cost_center
        payload["notes"] = ["Filtrado por centro de costo aplicado por el ERP; las entradas no traen el eje."]
    payload["summary"] = _contribution.build_spoken_summary(status)
    return payload
