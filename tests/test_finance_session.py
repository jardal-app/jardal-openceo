"""Tests del runtime determinista de finanzas (R1-13).

Cubre lo que ningun otro archivo tocaba: el despacho de cuatro vias del
`FinanceSessionManager` y el ciclo de vida del cliente en `_with_client`.

Dos mutaciones deben poner esto en rojo:
- borrar la rama `intent == INCOME` (la orden de ingreso responde FAILED y no
  contabiliza);
- borrar el `finally: await client.aclose()` (el cliente httpx se fuga).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.finance import contribution as contribution_module  # noqa: E402
from agents.finance import scope_summary as scope_module  # noqa: E402
from agents.finance.session import FinanceSessionManager  # noqa: E402
from state import TaskStatus  # noqa: E402


class FakeERPNextClient:
    """Cliente falso: asiento, GL y contador de cierres."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.closed = 0
        self.seq = 0
        self.gl_rows: list[dict] = []
        self.gl_filters: list | None = None
        self.fail_on_create = False

    async def create_doc(self, doctype: str, body: dict) -> dict:
        if self.fail_on_create:
            raise RuntimeError("ERPNext no responde")
        self.seq += 1
        name = f"ACC-JV-SESSION-{self.seq:05d}"
        doc = {"name": name, "docstatus": 0, **body}
        self.docs[name] = doc
        return doc

    async def list_docs(self, doctype: str, *, filters=None, fields=None, limit=20):
        if doctype == "Journal Entry":
            ref = filters[0][2] if filters else None
            return [d for d in self.docs.values() if d.get("openceo_ref") == ref][:limit]
        return []

    async def update_doc(self, doctype: str, name: str, body: dict) -> dict:
        self.docs[name].update(body)
        return self.docs[name]

    async def list_gl_entries(self, *, filters=None, fields=None, limit=500):
        self.gl_filters = filters
        date_from = date_to = None
        for f in filters or []:
            if f[0] == "posting_date" and f[1] == ">=":
                date_from = f[2]
            if f[0] == "posting_date" and f[1] == "<=":
                date_to = f[2]
        rows = [
            row
            for row in self.gl_rows
            if (date_from is None or row["posting_date"] >= date_from)
            and (date_to is None or row["posting_date"] <= date_to)
        ]
        return rows[:limit]

    async def aclose(self) -> None:
        self.closed += 1


def _gl_row(posting_date: str, account: str, debit: str, credit: str) -> dict:
    return {
        "name": f"ACC-GLE-{posting_date}",
        "posting_date": posting_date,
        "account": account,
        "account_currency": "BOB",
        "debit_in_account_currency": debit,
        "credit_in_account_currency": credit,
        "cost_center": "Principal - J",
        "company": "Jardal",
        "voucher_no": f"ACC-JV-{posting_date}",
        "is_cancelled": 0,
        "docstatus": 1,
    }


class FinanceSessionManagerTests(unittest.TestCase):
    def _run(self, text: str, client: FakeERPNextClient):
        manager = FinanceSessionManager(mock.Mock())
        with mock.patch("tools.erpnext_client.ERPNextClient", lambda _settings: client):
            return asyncio.run(manager.run({"title": text}))

    def test_income_phrase_writes_the_inverted_entry(self) -> None:
        client = FakeERPNextClient()
        output = self._run("registra ingreso sueldo 6200 bolivianos", client)
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertIn("ingreso", output.last_response_summary or "")
        doc = next(iter(client.docs.values()))
        debit, credit = doc["accounts"]
        self.assertEqual(debit["account"], "Efectivo - J")
        self.assertEqual(debit["debit_in_account_currency"], "6200.00")
        self.assertEqual(credit["account"], "Servicios - J")
        self.assertEqual(credit["credit_in_account_currency"], "6200.00")
        self.assertEqual(client.closed, 1)

    def test_expense_phrase_writes_the_expense_entry(self) -> None:
        client = FakeERPNextClient()
        output = self._run("registra gasto personal carne 50 bolivianos", client)
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        doc = next(iter(client.docs.values()))
        debit, credit = doc["accounts"]
        self.assertEqual(debit["account"], "Gastos varios - J")
        self.assertEqual(debit["debit_in_account_currency"], "50.00")
        self.assertEqual(credit["account"], "Efectivo - J")
        self.assertEqual(client.closed, 1)

    def test_contribution_phrase_reads_and_speaks(self) -> None:
        client = FakeERPNextClient()
        in_scope = contribution_module.current_month_period()[1]
        client.gl_rows = [_gl_row(in_scope, "Servicios - J", "0", "6200")]
        output = self._run("cómo vamos con el diezmo", client)
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertIn("6200.00", output.last_response_summary or "")
        self.assertIn("cuentas de ingreso", output.last_response_summary or "")
        self.assertTrue(client.gl_filters)
        self.assertEqual(client.closed, 1)

    def test_balance_phrase_scopes_the_period(self) -> None:
        """R1-5: "este mes" no puede responder el total histórico."""
        today = datetime.now(scope_module._LA_PAZ).date()
        first_of_month = today.replace(day=1)
        previous_month_day = (first_of_month - timedelta(days=1)).isoformat()
        client = FakeERPNextClient()
        client.gl_rows = [
            _gl_row(previous_month_day, "Gastos varios - J", "100.00", "0"),
            _gl_row(first_of_month.isoformat(), "Gastos varios - J", "50.00", "0"),
        ]
        output = self._run("cuánto gastamos este mes", client)
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        spoken = output.last_response_summary or ""
        self.assertIn("50.00", spoken)
        self.assertNotIn("100.00", spoken)
        predicates = {(f[0], f[1]): f[2] for f in client.gl_filters or []}
        self.assertEqual(predicates[("posting_date", ">=")], first_of_month.isoformat())
        self.assertEqual(predicates[("posting_date", "<=")], today.isoformat())
        self.assertEqual(client.closed, 1)

    def test_unknown_phrase_fails_with_the_workflow_message(self) -> None:
        client = FakeERPNextClient()
        output = self._run("hola, buenos días", client)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("No encontré un workflow de finanzas compatible", output.error or "")
        self.assertEqual(client.docs, {})
        # Sin workflow no se construye cliente: nada que cerrar.
        self.assertEqual(client.closed, 0)

    def test_client_is_closed_even_when_the_write_fails(self) -> None:
        client = FakeERPNextClient()
        client.fail_on_create = True
        output = self._run("registra ingreso sueldo 6200 bolivianos", client)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertEqual(client.closed, 1)

    def test_foreign_currency_without_rate_writes_nothing(self) -> None:
        client = FakeERPNextClient()
        output = self._run("me pagaron 500 dólares del contrato", client)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("tasa de cambio", output.error or "")
        self.assertEqual(client.docs, {})
        self.assertEqual(client.closed, 1)


if __name__ == "__main__":
    unittest.main()
