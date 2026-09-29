"""Tests de los tools de escritura de Finance en el registro de workflows (R1-12).

El tool acuñaba un `openceo_ref` nuevo por llamada, así que la única guarda
contra duplicados del camino de escritura no podía disparar nunca: un
re-despacho de la misma tarea (POST /run con force) o un parche a Mongo perdido
tras la escritura dejaban dos asientos sometidos, ambos `duplicate=False`.

La referencia se deriva del contexto del run (`task_id`), no del azar: mismo
intento, mismo valor (contrato §6.1).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from workflow_service import WorkflowToolRegistry  # noqa: E402


class FakeDuplicate(Exception):
    def __init__(self) -> None:
        super().__init__("duplicate")
        self.status_code = 417


class FakeERPNextClient:
    """Imita create/list/update del cliente real sobre Journal Entry."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.closed = 0
        self.seq = 0

    async def create_doc(self, doctype: str, body: dict) -> dict:
        assert doctype == "Journal Entry"
        ref = body.get("openceo_ref")
        for doc in self.docs.values():
            if doc.get("openceo_ref") == ref:
                raise FakeDuplicate()
        self.seq += 1
        name = f"ACC-JV-TOOL-{self.seq:05d}"
        doc = {"name": name, "docstatus": 0, **body}
        self.docs[name] = doc
        return doc

    async def list_docs(self, doctype: str, *, filters=None, fields=None, limit=20):
        assert doctype == "Journal Entry"
        ref = filters[0][2] if filters else None
        return [d for d in self.docs.values() if d.get("openceo_ref") == ref][:limit]

    async def update_doc(self, doctype: str, name: str, body: dict) -> dict:
        assert doctype == "Journal Entry"
        self.docs[name].update(body)
        return self.docs[name]

    async def aclose(self) -> None:
        self.closed += 1


class FinanceToolIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeERPNextClient()
        self.registry = WorkflowToolRegistry(settings=mock.Mock())
        self.context = {"task": {"task_id": "TASK-2026-00053"}, "params": {}}

    def _call(self, tool: str, text: str, **kwargs):
        with mock.patch("tools.erpnext_client.ERPNextClient", lambda _settings: self.client):
            return asyncio.run(
                getattr(self.registry, tool)(
                    text=text, context=self.context, **kwargs
                )
            )

    def test_same_task_does_not_duplicate_the_income(self) -> None:
        first = self._call("_finance_register_income", "registra ingreso sueldo 6200 bolivianos")
        second = self._call("_finance_register_income", "registra ingreso sueldo 6200 bolivianos")
        self.assertTrue(first["ok"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.client.docs), 1)
        self.assertEqual(first["journal_entry"], second["journal_entry"])
        self.assertEqual(self.client.closed, 2)

    def test_same_task_does_not_duplicate_the_expense(self) -> None:
        """El gemelo de gasto tenia el mismo agujero (`voice_expense.yaml` no pasa ref)."""
        first = self._call(
            "_finance_register_expense", "registra gasto personal carne 50 bolivianos"
        )
        second = self._call(
            "_finance_register_expense", "registra gasto personal carne 50 bolivianos"
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.client.docs), 1)

    def test_explicit_ref_wins_over_the_task_ref(self) -> None:
        result = self._call(
            "_finance_register_income",
            "registra ingreso sueldo 6200 bolivianos",
            openceo_ref="voice-income:explicito",
        )
        self.assertEqual(result["ref"], "voice-income:explicito")

    def test_different_tasks_are_different_operations(self) -> None:
        other = {"task": {"task_id": "TASK-2026-00099"}, "params": {}}
        self._call("_finance_register_income", "registra ingreso sueldo 6200 bolivianos")
        with mock.patch("tools.erpnext_client.ERPNextClient", lambda _settings: self.client):
            second = asyncio.run(
                self.registry._finance_register_income(
                    text="registra ingreso sueldo 6200 bolivianos", context=other
                )
            )
        self.assertFalse(second["duplicate"])
        self.assertEqual(len(self.client.docs), 2)

    def test_raw_questions_and_expenses_are_refused(self) -> None:
        """R1-6: el tool recibe texto crudo; las guardas del modulo lo frenan."""
        for text in [
            "¿cuándo me pagaron los 500 dólares?",
            "pago de sueldo del empleado 3000 bs",
        ]:
            with self.subTest(text=text):
                result = self._call("_finance_register_income", text)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "no_amount")
        self.assertEqual(self.client.docs, {})

    def test_foreign_currency_without_rate_is_refused(self) -> None:
        result = self._call("_finance_register_income", "me pagaron 500 dólares del contrato")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_exchange_rate")
        self.assertIn("tasa de cambio", result["summary"])
        self.assertEqual(self.client.docs, {})
        self.assertEqual(self.client.closed, 1)

    def test_foreign_currency_with_rate_is_converted(self) -> None:
        result = self._call(
            "_finance_register_income",
            "me pagaron 500 dólares del contrato",
            exchange_rate="6.96",
        )
        self.assertTrue(result["ok"])
        doc = next(iter(self.client.docs.values()))
        self.assertEqual(doc["accounts"][0]["debit_in_account_currency"], "3480.00")
        self.assertIn("6.96", doc["user_remark"])


if __name__ == "__main__":
    unittest.main()
