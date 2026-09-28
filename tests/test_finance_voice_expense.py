"""Tests del registro ocasional de gastos por voz (TASK-2026-00032).

Sin red: parser, clasificacion, idempotencia y Decimal se prueban con un
cliente falso. La prueba viva contra ERPNext se corre a mano una vez y sus
documentos se eliminan (los borradores se borran limpio).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from decimal import Decimal
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.finance.voice_expense import (  # noqa: E402
    build_journal_entry,
    build_spoken_summary,
    classify_expense_account,
    looks_like_expense,
    new_openceo_ref,
    parse_amount,
    parse_expense,
    register_expense,
)


class FakeDuplicate(Exception):
    def __init__(self) -> None:
        super().__init__("duplicate")
        self.status_code = 417


class FakeERPNextClient:
    """Imita create/list/update del cliente real sobre Journal Entry."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}
        self.submitted: list[str] = []
        self.seq = 0

    async def create_doc(self, doctype: str, body: dict) -> dict:
        assert doctype == "Journal Entry"
        ref = body.get("openceo_ref")
        for doc in self.docs.values():
            if doc.get("openceo_ref") == ref:
                raise FakeDuplicate()
        self.seq += 1
        name = f"ACC-JV-TEST-{self.seq:05d}"
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
        if body.get("docstatus") == 1:
            self.submitted.append(name)
        return self.docs[name]

    async def aclose(self) -> None:
        return None


class ParseExpenseTests(unittest.TestCase):
    def test_carne_bolivianos(self) -> None:
        parsed = parse_expense("registra gastos personales carne 50 bolivianos")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.concept, "carne")
        self.assertEqual(parsed.amount, Decimal("50"))
        self.assertEqual(parsed.currency, "BOB")
        self.assertEqual(parsed.expense_account, "Gastos varios - J")
        self.assertIsInstance(parsed.amount, Decimal)

    def test_luz_casa_bs(self) -> None:
        parsed = parse_expense("pago de luz de casa 300 bs")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertIn("luz", parsed.concept)
        self.assertEqual(parsed.amount, Decimal("300"))
        self.assertEqual(parsed.expense_account, "Servicios públicos - J")

    def test_thousands_comma_decimal(self) -> None:
        parsed = parse_expense("registra gasto mercado 1.234,56 bolivianos")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.amount, Decimal("1234.56"))

    def test_usd_and_usdt_distinct(self) -> None:
        usd = parse_expense("registra gasto personal 10 usd")
        usdt = parse_expense("registra gasto personal 10 usdt")
        assert usd is not None and usdt is not None
        self.assertEqual(usd.currency, "USD")
        self.assertEqual(usdt.currency, "USDT")

    def test_no_amount_returns_none(self) -> None:
        self.assertIsNone(parse_expense("registra gasto personal carne"))

    def test_time_amount_is_not_money(self) -> None:
        self.assertFalse(looks_like_expense("anota 15 minutos de supervisión"))
        self.assertIsNone(parse_expense("anota 15 minutos de supervisión"))

    def test_dev_traffic_not_claimed(self) -> None:
        self.assertFalse(looks_like_expense("crea una sesión dev en trading"))
        self.assertFalse(looks_like_expense("pon el preset review 2"))
        self.assertFalse(looks_like_expense("¿cuántos tickets tenemos abiertos?"))


class ClassifyTests(unittest.TestCase):
    def test_utilities(self) -> None:
        self.assertEqual(classify_expense_account("luz casa"), "Servicios públicos - J")
        self.assertEqual(classify_expense_account("agua"), "Servicios públicos - J")

    def test_transport_and_phone(self) -> None:
        self.assertEqual(
            classify_expense_account("pasaje trufi"), "CARGOS DE TRANSITO Y TRANSPORTE - J"
        )
        self.assertEqual(classify_expense_account("recarga celular"), "Cuenta telefonica - J")

    def test_default_personal(self) -> None:
        self.assertEqual(classify_expense_account("carne"), "Gastos varios - J")
        self.assertEqual(classify_expense_account("xyz desconocido"), "Gastos varios - J")

    def test_parse_amount_never_float(self) -> None:
        self.assertEqual(parse_amount("50"), Decimal("50"))
        self.assertIsNone(parse_amount("0"))
        self.assertIsNone(parse_amount("-5"))
        self.assertIsNone(parse_amount("abc"))


class RegisterExpenseTests(unittest.TestCase):
    def test_create_and_submit(self) -> None:
        async def scenario() -> None:
            client = FakeERPNextClient()
            parsed = parse_expense("registra gastos personales carne 50 bolivianos")
            assert parsed is not None
            result = await register_expense(client, parsed, ref="voice-expense:test1")
            self.assertTrue(result["ok"])
            self.assertFalse(result["duplicate"])
            self.assertEqual(result["doc"]["docstatus"], 1)
            self.assertIn("ACC-JV-TEST-00001", client.submitted)
            payload = build_journal_entry(parsed, "voice-expense:test1")
            self.assertEqual(payload["company"], "Jardal")
            self.assertEqual(len(payload["accounts"]), 2)
            summary = build_spoken_summary(parsed, result["doc"], False)
            self.assertIn("50.00", summary)
            self.assertNotIn("voice-expense:test1", summary)

        asyncio.run(scenario())

    def test_retry_same_ref_is_idempotent(self) -> None:
        async def scenario() -> None:
            client = FakeERPNextClient()
            parsed = parse_expense("pago de luz de casa 300 bs")
            assert parsed is not None
            first = await register_expense(client, parsed, ref="voice-expense:retry")
            second = await register_expense(client, parsed, ref="voice-expense:retry")
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["doc"]["name"], second["doc"]["name"])
            self.assertEqual(len(client.docs), 1)

        asyncio.run(scenario())

    def test_different_ref_creates_distinct_operation(self) -> None:
        async def scenario() -> None:
            client = FakeERPNextClient()
            parsed = parse_expense("pago de luz de casa 300 bs")
            assert parsed is not None
            first = await register_expense(client, parsed, ref="voice-expense:a")
            second = await register_expense(client, parsed, ref="voice-expense:b")
            self.assertFalse(second["duplicate"])
            self.assertNotEqual(first["doc"]["name"], second["doc"]["name"])

        asyncio.run(scenario())

    def test_refs_are_unique(self) -> None:
        self.assertNotEqual(new_openceo_ref(), new_openceo_ref())


if __name__ == "__main__":
    unittest.main()
