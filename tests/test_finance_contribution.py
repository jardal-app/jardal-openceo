"""Tests de la regla del 10% desde ERPNext (TASK-2026-00054).

Sin red: aritmética Decimal, base elegible, estado `needs_input` cuando no hay
cuenta de aportes y solo-lectura se prueban con un cliente falso.

El punto que no puede fallar: aportes confirmados es `None` (no medible), nunca
`0`. Cero es un valor medido y diría que no se aportó nada.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.finance import contribution  # noqa: E402
from agents.finance.contribution import (  # noqa: E402
    build_spoken_summary,
    current_month_period,
    get_contribution_10_status,
    looks_like_contribution_query,
)


class FakeReadClient:
    """Imita el lector dedicado del cliente real. Sin escritura."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.filters: list | None = None

    async def list_gl_entries(self, *, filters=None, fields=None, limit=500):
        self.filters = filters
        assert any(f[0] == "company" for f in filters)
        assert any(f[0] == "is_cancelled" for f in filters)
        assert any(f[0] == "posting_date" and f[1] == ">=" for f in filters)
        assert any(f[0] == "posting_date" and f[1] == "<=" for f in filters)
        return self.rows[:limit]

    async def create_doc(self, doctype: str, body: dict) -> dict:
        raise AssertionError(f"escritura no permitida: create {doctype}")

    async def update_doc(self, doctype: str, name: str, body: dict) -> dict:
        raise AssertionError(f"escritura no permitida: update {doctype} {name}")

    async def aclose(self) -> None:
        return None


class FakeLegacyClient(FakeReadClient):
    """Cliente sin lector dedicado: usa el fallback list_docs.

    `list_gl_entries = None` es deliberado (R1-15): heredarlo dejaba el
    fallback sin ejercitar, porque `getattr(...)` encontraba el metodo heredado
    y `list_docs` quedaba como codigo muerto.
    """

    list_gl_entries = None

    async def list_docs(self, doctype: str, *, filters=None, fields=None, limit=20):
        assert doctype == "GL Entry"
        return await super().list_gl_entries(filters=filters, fields=fields, limit=limit)


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
    def test_contribution_queries(self) -> None:
        cases = [
            "cómo vamos con el diezmo",
            "cuánto debo del 10 % de lo que cobré",
            "estado del aporte",
            "registra mi diezmo 500 bs",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(looks_like_contribution_query(text))

    def test_other_finance_not_claimed(self) -> None:
        cases = [
            "cuánto gastamos este mes",
            "registra gasto personal carne 50 bolivianos",
            "registra ingreso sueldo 6200 bolivianos",
            "crea una sesión dev en trading",
            "",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(looks_like_contribution_query(text))

    def test_expense_orders_are_not_contribution_queries(self) -> None:
        """R1-9: "aporte"/"donación" sueltos no interceptan una orden de gasto."""
        cases = [
            "registra gasto del aporte patronal 1200 bs",
            "registra pago de luz de casa 300 bs con 10 % de recargo",
            "anota mi aporte a la reunion de hoy",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(looks_like_contribution_query(text))

    def test_ambiguous_words_still_work_as_queries(self) -> None:
        for text in [
            "estado del aporte",
            "cómo vamos con el aporte",
            "cuánto llevo aportado",
            "cuánto debo del 10 % de lo que cobré",
        ]:
            with self.subTest(text=text):
                self.assertTrue(looks_like_contribution_query(text))


class ContributionMathTests(unittest.TestCase):
    def test_ten_percent_of_collected_income(self) -> None:
        rows = [
            _row("Servicios - J", "0", "6200"),
            _row("Ventas - J", "0", "800"),
            _row("Efectivo - J", "7000", "0"),
            _row("Gastos varios - J", "50", "0"),
        ]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertIsInstance(status.eligible_income, Decimal)
        self.assertEqual(status.eligible_income, Decimal("7000.00"))
        self.assertEqual(status.target_contribution, Decimal("700.00"))
        self.assertEqual(status.rate, Decimal("0.10"))
        # Efectivo y gastos no son ingreso elegible.
        self.assertEqual(set(status.income_by_account), {"Servicios - J", "Ventas - J"})

    def test_only_income_accounts_count(self) -> None:
        rows = [_row("Efectivo - J", "900", "0"), _row("Gastos varios - J", "0", "0")]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertEqual(status.eligible_income, Decimal("0"))
        self.assertEqual(status.entries, 0)

    def test_other_currency_excluded(self) -> None:
        rows = [_row("Servicios - J", "0", "100", account_currency="USD")]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertEqual(status.eligible_income, Decimal("0"))
        self.assertEqual(status.excluded_entries, 1)
        self.assertIn("otra moneda", build_spoken_summary(status))

    def test_fallback_list_docs(self) -> None:
        rows = [_row("Servicios - J", "0", "1000")]
        status = asyncio.run(get_contribution_10_status(FakeLegacyClient(rows)))
        self.assertEqual(status.target_contribution, Decimal("100.00"))

    def test_default_period_is_current_month(self) -> None:
        client = FakeReadClient([])
        status = asyncio.run(get_contribution_10_status(client))
        month_from, month_to = current_month_period()
        self.assertEqual(status.date_from, month_from)
        self.assertEqual(status.date_to, month_to)
        self.assertIn("este mes", status.period_label)

    def test_explicit_period_label(self) -> None:
        status = asyncio.run(
            get_contribution_10_status(
                FakeReadClient([]), date_from="2026-08-01", date_to="2026-08-31"
            )
        )
        self.assertIn("2026-08-01", status.period_label)

    def test_read_only_never_writes(self) -> None:
        rows = [_row("Servicios - J", "0", "100")]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertEqual(status.eligible_income, Decimal("100.00"))

    def test_limit_reached_is_spoken(self) -> None:
        """R1-11: una base truncada no puede presentarse como completa."""
        rows = [_row("Servicios - J", "0", "1000") for _ in range(3)]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows), limit=2))
        self.assertTrue(status.limit_reached)
        self.assertEqual(status.eligible_income, Decimal("2000.00"))
        self.assertIn("parcial", build_spoken_summary(status))
        full = asyncio.run(get_contribution_10_status(FakeReadClient(rows), limit=10))
        self.assertFalse(full.limit_reached)
        self.assertNotIn("parcial", build_spoken_summary(full))
        self.assertEqual(full.eligible_income, Decimal("3000.00"))

    def test_limit_and_currency_notes_also_reach_the_ok_branch(self) -> None:
        """R1-11: la rama `ok` retornaba antes de contar los avisos."""
        rows = [
            _row("Servicios - J", "0", "1000"),
            _row("Servicios - J", "0", "1000", account_currency="USD"),
        ]
        with mock.patch.object(contribution, "CONTRIBUTION_ACCOUNTS", ("Aportes - J",)):
            status = asyncio.run(get_contribution_10_status(FakeReadClient(rows), limit=2))
        self.assertEqual(status.state, "ok")
        self.assertTrue(status.limit_reached)
        self.assertEqual(status.excluded_entries, 1)
        spoken = build_spoken_summary(status)
        self.assertIn("otra moneda", spoken)
        self.assertIn("parcial", spoken)

    def test_speech_does_not_claim_collected_cash(self) -> None:
        """R1-17: la medición no verifica la contrapartida de caja.

        Un devengado (débito a Deudores / crédito a la cuenta de ingreso)
        cuenta igual que un cobro, así que la voz no puede decir "cobramos".
        """
        rows = [
            _row("Deudores - J", "6200", "0"),
            _row("Servicios - J", "0", "6200"),
        ]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertEqual(status.eligible_income, Decimal("6200.00"))
        spoken = build_spoken_summary(status)
        self.assertNotIn("cobramos", spoken)
        self.assertIn("cuentas de ingreso", spoken)


class UnknownIsNotZeroTests(unittest.TestCase):
    """Sin cuenta de aportes configurada, lo aportado no es medible.

    El estado se fija con `patch` a `()` para que estos tests midan el
    COMPORTAMIENTO y no el plan de cuentas vivo: poblar la constante —el cambio
    que la documentación promete— no debe ponerlos rojos como si la lógica de
    dinero hubiera regresado (R1-16).
    """

    def setUp(self) -> None:
        patcher = mock.patch.object(contribution, "CONTRIBUTION_ACCOUNTS", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_contribution_account_means_none_not_zero(self) -> None:
        rows = [_row("Servicios - J", "0", "6200")]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertIsNone(status.confirmed_contribution)
        self.assertIsNone(status.pending_contribution)
        self.assertEqual(status.state, "needs_input")
        self.assertIsNotNone(status.reason)
        self.assertIn("cuenta", status.reason)

    def test_payload_reports_the_accounts_actually_measured(self) -> None:
        """R1-19: el dataclass congelaba el default y el payload decía `[]`."""
        rows = [
            _row("Servicios - J", "0", "6200"),
            _row("Aportes - J", "620", "0"),
        ]
        with mock.patch.object(contribution, "CONTRIBUTION_ACCOUNTS", ("Aportes - J",)):
            status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertEqual(status.state, "ok")
        self.assertEqual(status.to_dict()["contribution_accounts"], ["Aportes - J"])

    def test_to_dict_keeps_unknown_as_null(self) -> None:
        rows = [_row("Servicios - J", "0", "6200")]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        payload = status.to_dict()
        self.assertIsNone(payload["confirmed_contribution"])
        self.assertIsNone(payload["pending_contribution"])
        self.assertEqual(payload["eligible_income"], "6200.00")
        self.assertEqual(payload["target_contribution"], "620.00")
        self.assertEqual(payload["as_of"] != "", True)
        self.assertEqual(payload["company"], "Jardal")
        self.assertEqual(payload["currency"], "BOB")
        self.assertEqual(payload["state"], "needs_input")

    def test_spoken_summary_says_what_is_missing(self) -> None:
        rows = [_row("Servicios - J", "0", "6200")]
        status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        spoken = build_spoken_summary(status)
        self.assertIn("6200.00", spoken)
        self.assertIn("620.00", spoken)
        self.assertIn("falta definir", spoken)
        self.assertNotIn("ACC-", spoken)
        self.assertNotIn("|", spoken)

    def test_configured_account_switches_to_ok(self) -> None:
        """En cuanto el dueño defina la cuenta, el estado pasa a `ok` solo."""
        rows = [
            _row("Servicios - J", "0", "6200"),
            _row("Aportes - J", "620", "0"),
        ]
        with mock.patch.object(contribution, "CONTRIBUTION_ACCOUNTS", ("Aportes - J",)):
            status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertEqual(status.state, "ok")
        self.assertEqual(status.confirmed_contribution, Decimal("620.00"))
        self.assertEqual(status.pending_contribution, Decimal("0.00"))
        self.assertIn("aportados", build_spoken_summary(status))

    def test_partial_contribution_leaves_pending(self) -> None:
        rows = [
            _row("Servicios - J", "0", "6200"),
            _row("Aportes - J", "200", "0"),
        ]
        with mock.patch.object(contribution, "CONTRIBUTION_ACCOUNTS", ("Aportes - J",)):
            status = asyncio.run(get_contribution_10_status(FakeReadClient(rows)))
        self.assertEqual(status.target_contribution, Decimal("620.00"))
        self.assertEqual(status.pending_contribution, Decimal("420.00"))


if __name__ == "__main__":
    unittest.main()
