"""Tests del resumen de saldos/gasto por Scope (TASK-2026-00044).

Sin red: detector, agregacion Decimal y solo-lectura se prueban con un
cliente falso. La prueba viva contra Jardal ERP se corre a mano una vez
(lectura, sin documentos que limpiar).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timedelta


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.finance import scope_summary as scope_module  # noqa: E402
from agents.finance.scope_summary import (  # noqa: E402
    build_spoken_summary,
    get_scope_summary,
    looks_like_balance_query,
    period_bounds,
)


class FakeReadClient:
    """Imita el lector dedicado del cliente real. Sin escritura."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.writes = 0

    async def list_gl_entries(self, *, filters=None, fields=None, limit=500):
        assert filters and any(f[0] == "company" for f in filters)
        assert any(f[0] == "is_cancelled" for f in filters)
        return self.rows[:limit]

    async def aclose(self) -> None:
        return None


class FakeLegacyClient(FakeReadClient):
    """Cliente sin lector dedicado: usa el fallback list_docs.

    `list_gl_entries = None` es deliberado: heredarlo dejaba el fallback sin
    ejercitar (el `getattr` encontraba el metodo heredado).
    """

    list_gl_entries = None

    async def list_docs(self, doctype: str, *, filters=None, fields=None, limit=20):
        assert doctype == "GL Entry"
        return await super().list_gl_entries(filters=filters, fields=fields, limit=limit)


class StrictReadClient(FakeReadClient):
    """Si el codigo intenta escribir, falla fuerte en vez de pasar en silencio."""

    async def create_doc(self, doctype: str, body: dict) -> dict:
        raise AssertionError(f"escritura no permitida: create {doctype}")

    async def update_doc(self, doctype: str, name: str, body: dict) -> dict:
        raise AssertionError(f"escritura no permitida: update {doctype} {name}")


def _row(account: str, debit: str, credit: str, **extra: object) -> dict:
    base = {
        "name": "ACC-GLE-TEST-1",
        "posting_date": "2026-09-28",
        "account": account,
        "account_currency": "BOB",
        "debit_in_account_currency": debit,
        "credit_in_account_currency": credit,
        "cost_center": "Principal - J",
        "company": "Jardal",
        "voucher_no": "ACC-JV-TEST-1",
        "is_cancelled": 0,
        "docstatus": 1,
    }
    base.update(extra)
    return base


class DetectorTests(unittest.TestCase):
    def test_balance_queries(self) -> None:
        self.assertTrue(looks_like_balance_query("cuánto gastamos este mes"))
        self.assertTrue(looks_like_balance_query("dame el saldo de la caja"))
        self.assertTrue(looks_like_balance_query("resumen de gastos"))
        self.assertTrue(looks_like_balance_query("cuánto gastamos"))

    def test_not_balance(self) -> None:
        self.assertFalse(looks_like_balance_query("registra gasto carne 50 bolivianos"))
        self.assertFalse(looks_like_balance_query("crea una sesión dev"))
        self.assertFalse(looks_like_balance_query(""))


class PeriodBoundsTests(unittest.TestCase):
    """R1-5: "este mes" acota la lectura; sin cotas se respondía el histórico."""

    def _today(self):
        return datetime.now(scope_module._LA_PAZ).date()

    def test_this_month(self) -> None:
        today = self._today()
        self.assertEqual(
            period_bounds("cuánto gastamos este mes"),
            (today.replace(day=1).isoformat(), today.isoformat()),
        )

    def test_past_month(self) -> None:
        today = self._today()
        previous = today.replace(day=1) - timedelta(days=1)
        self.assertEqual(
            period_bounds("cuánto gastamos el mes pasado"),
            (previous.replace(day=1).isoformat(), previous.isoformat()),
        )

    def test_today_and_week(self) -> None:
        today = self._today()
        self.assertEqual(
            period_bounds("cuánto gasté hoy"), (today.isoformat(), today.isoformat())
        )
        self.assertEqual(
            period_bounds("gastos de esta semana"),
            ((today - timedelta(days=today.weekday())).isoformat(), today.isoformat()),
        )

    def test_it_understands_accents_and_missing_ones(self) -> None:
        self.assertEqual(period_bounds("cuanto gastamos este ano"), period_bounds("cuánto gastamos este año"))
        today = self._today()
        self.assertEqual(period_bounds("cuanto gaste este ano")[1], today.isoformat())

    def test_without_period_words_there_is_no_bound(self) -> None:
        self.assertEqual(period_bounds("resumen de gastos"), (None, None))
        self.assertEqual(period_bounds(""), (None, None))

class SummaryTests(unittest.TestCase):
    def test_sums_decimal_no_float(self) -> None:
        rows = [
            _row("Gastos varios - J", "50.10", "0"),
            _row("Gastos varios - J", "0", "20.00"),
            _row("Efectivo - J", "0", "30.10"),
        ]
        summary = asyncio.run(get_scope_summary(FakeReadClient(rows)))
        self.assertIsInstance(summary.net, Decimal)
        self.assertEqual(summary.total_debit, Decimal("50.10"))
        self.assertEqual(summary.total_credit, Decimal("50.10"))
        self.assertEqual(summary.net, Decimal("0"))
        self.assertEqual(summary.entries, 3)
        self.assertEqual(summary.by_account["Gastos varios - J"], Decimal("30.10"))
        self.assertEqual(summary.as_of != "", True)
        self.assertEqual(summary.company, "Jardal")
        self.assertEqual(summary.currency, "BOB")

    def test_other_currency_ignored(self) -> None:
        rows = [_row("Gastos varios - J", "10", "0", account_currency="USD")]
        summary = asyncio.run(get_scope_summary(FakeReadClient(rows)))
        self.assertEqual(summary.entries, 0)
        self.assertEqual(summary.net, Decimal("0"))
        self.assertEqual(summary.excluded_entries, 1)
        self.assertIn("otra moneda", build_spoken_summary(summary))

    def test_fallback_list_docs(self) -> None:
        rows = [_row("Gastos varios - J", "7.50", "0")]
        summary = asyncio.run(get_scope_summary(FakeLegacyClient(rows)))
        self.assertEqual(summary.entries, 1)
        self.assertEqual(summary.net, Decimal("7.50"))

    def test_limit_reached_flag(self) -> None:
        rows = [_row("Gastos varios - J", "1", "0") for _ in range(3)]
        summary = asyncio.run(get_scope_summary(FakeReadClient(rows), limit=3))
        self.assertTrue(summary.limit_reached)
        summary = asyncio.run(get_scope_summary(FakeReadClient(rows), limit=10))
        self.assertFalse(summary.limit_reached)

    def test_read_only_never_writes(self) -> None:
        client = StrictReadClient([_row("Gastos varios - J", "1", "0")])
        summary = asyncio.run(get_scope_summary(client))
        self.assertEqual(summary.entries, 1)
        # Si el codigo intentara escribir, el fake estricto levantaria.

    def test_spoken_short_no_ids(self) -> None:
        rows = [_row("Gastos varios - J", "50", "0")]
        summary = asyncio.run(get_scope_summary(FakeReadClient(rows)))
        spoken = build_spoken_summary(summary)
        self.assertIn("50.00", spoken)
        self.assertNotIn("ACC-", spoken)
        self.assertNotIn("|", spoken)

    def test_spoken_names_the_period_measured(self) -> None:
        rows = [_row("Gastos varios - J", "50", "0")]
        summary = asyncio.run(
            get_scope_summary(
                FakeReadClient(rows), date_from="2026-09-01", date_to="2026-09-28"
            )
        )
        spoken = build_spoken_summary(summary)
        self.assertIn("2026-09-01", spoken)
        self.assertIn("2026-09-28", spoken)
        no_period = asyncio.run(get_scope_summary(FakeReadClient(rows)))
        self.assertIn("histórico completo", build_spoken_summary(no_period))

    def test_limit_reached_is_spoken(self) -> None:
        """R1-11: con `limit` alcanzado la base es parcial y hay que decirlo."""
        rows = [_row("Gastos varios - J", "1", "0") for _ in range(3)]
        summary = asyncio.run(get_scope_summary(FakeReadClient(rows), limit=2))
        self.assertTrue(summary.limit_reached)
        self.assertIn("parcial", build_spoken_summary(summary))
        full = asyncio.run(get_scope_summary(FakeReadClient(rows), limit=10))
        self.assertFalse(full.limit_reached)
        self.assertNotIn("parcial", build_spoken_summary(full))


if __name__ == "__main__":
    unittest.main()
