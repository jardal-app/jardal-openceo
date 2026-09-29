"""Tests del registro ocasional de ingresos por voz (TASK-2026-00053).

Sin red: detector, parser, clasificacion a cuentas de ingreso reales,
idempotencia y Decimal se prueban con un cliente falso. La prueba viva contra
ERPNext se corre a mano una vez (ver STATUS.md).
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

from agents.finance.money_text import MissingExchangeRateError  # noqa: E402
from agents.finance.voice_income import (  # noqa: E402
    build_journal_entry,
    build_spoken_summary,
    classify_income_account,
    looks_like_income,
    new_openceo_ref,
    parse_income,
    register_income,
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


class ParseIncomeTests(unittest.TestCase):
    def test_sueldo_bolivianos(self) -> None:
        parsed = parse_income("registra ingreso sueldo 6200 bolivianos")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.concept, "sueldo")
        self.assertEqual(parsed.amount, Decimal("6200"))
        self.assertIsInstance(parsed.amount, Decimal)
        self.assertEqual(parsed.currency, "BOB")
        self.assertEqual(parsed.income_account, "Servicios - J")

    def test_pago_recibido_en_dolares(self) -> None:
        parsed = parse_income("me pagaron 500 dólares del contrato")
        assert parsed is not None
        self.assertEqual(parsed.amount, Decimal("500"))
        self.assertEqual(parsed.currency, "USD")
        self.assertEqual(parsed.concept, "contrato")

    def test_docencia(self) -> None:
        parsed = parse_income("anota ingreso docencia 3800 bs")
        assert parsed is not None
        self.assertEqual(parsed.concept, "docencia")
        self.assertEqual(parsed.amount, Decimal("3800"))
        self.assertEqual(parsed.income_account, "Servicios - J")

    def test_thousands_comma_decimal(self) -> None:
        parsed = parse_income("registra ingreso 1.234,56 bolivianos")
        assert parsed is not None
        self.assertEqual(parsed.amount, Decimal("1234.56"))

    def test_usd_and_usdt_distinct_and_no_stray_letter(self) -> None:
        usdt = parse_income("registra ingreso freelance 100 usdt")
        usd = parse_income("registra ingreso freelance 100 usd")
        assert usdt is not None and usd is not None
        self.assertEqual(usdt.currency, "USDT")
        self.assertEqual(usd.currency, "USD")
        # "usdt" no debe dejar una "t" suelta pegada al concepto.
        self.assertEqual(usdt.concept, "freelance")

    def test_no_amount_returns_none(self) -> None:
        self.assertIsNone(parse_income("registra ingreso sueldo"))

    def test_time_amount_is_not_money(self) -> None:
        self.assertFalse(looks_like_income("me pagaron 15 minutos de supervisión"))
        self.assertIsNone(parse_income("anota 15 minutos de supervisión"))

    def test_parse_income_applies_the_module_guards(self) -> None:
        """R1-6: el tool recibe texto crudo; sin guardas escribia preguntas y egresos."""
        for text in [
            "¿cuándo me pagaron los 500 dólares?",
            "cuando me pagaron los 500 dolares",
            "registra ingreso del ticket 48 500 bs",
            "pago de sueldo del empleado 3000 bs",
            "pagaron la luz 200 bs",
            "el sueldo del empleado sube a 3000 el próximo mes",
            "anota mi aporte a la reunion de hoy",
        ]:
            with self.subTest(text=text):
                self.assertIsNone(parse_income(text))

    def test_amount_is_the_number_carrying_the_currency(self) -> None:
        """R1-4: el importe no es el primer numero del texto."""
        cases = {
            "recibí 3 pagos de 500 bolivianos": Decimal("500"),
            "me pagaron 2 cuotas de 1500 bs": Decimal("1500"),
            "cobré la factura 12 del cliente 3800 bs": Decimal("3800"),
            "ingreso venta del 15/09 900 bs": Decimal("900"),
            "me pagaron el sueldo 6200 bolivianos": Decimal("6200"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = parse_income(text)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertEqual(parsed.amount, expected)

    def test_thousands_separator_is_read_as_thousands(self) -> None:
        """R1-1: "6.200" son 6200, no 6.20 (1000x menos en el libro)."""
        parsed = parse_income("me pagaron 6.200 bolivianos")
        assert parsed is not None
        self.assertEqual(parsed.amount, Decimal("6200"))
        self.assertEqual(parsed.currency, "BOB")


class ClassifyIncomeAccountTests(unittest.TestCase):
    def test_services_is_default(self) -> None:
        for concept in ["sueldo", "docencia", "cliente", "honorarios", "xyz desconocido"]:
            with self.subTest(concept=concept):
                self.assertEqual(classify_income_account(concept), "Servicios - J")

    def test_sales(self) -> None:
        self.assertEqual(classify_income_account("venta bicicleta"), "Ventas - J")
        self.assertEqual(classify_income_account("vendí bicicleta"), "Ventas - J")

    def test_interest_accounts_are_distinct(self) -> None:
        self.assertEqual(classify_income_account("intereses"), "Interest Income - J")
        self.assertEqual(
            classify_income_account("intereses a plazo fijo"),
            "Interest on Fixed Deposits - J",
        )


class DetectorTests(unittest.TestCase):
    def test_income_phrases(self) -> None:
        cases = [
            "registra ingreso sueldo 6200 bolivianos",
            "me pagaron 500 dólares del contrato",
            "cobré 1200 bs del cliente",
            "recibí un depósito de 300 bolivianos",
            "sueldo 6200 bolivianos",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(looks_like_income(text))

    def test_expense_phrases_are_not_income(self) -> None:
        cases = [
            "registra gasto personal carne 50 bolivianos",
            "pago de luz de casa 300 bs",
            # Sustantivo de ingreso con orden de pago: es gasto, no ingreso.
            "pago de sueldo del empleado 3000 bs",
            "registra pago de intereses del préstamo 300 bs",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(looks_like_income(text))

    def test_question_is_not_a_write_order(self) -> None:
        self.assertFalse(looks_like_income("¿cuándo me pagaron los 500 dólares?"))
        self.assertFalse(looks_like_income("cuánto me pagaron el mes pasado 500 bs"))

    def test_without_amount_nothing_is_claimed(self) -> None:
        self.assertFalse(looks_like_income("me pagaron el remoto"))

    def test_dev_traffic_not_claimed(self) -> None:
        self.assertFalse(looks_like_income("registra ingreso del ticket 48 500 bs"))

    def test_expense_phrases_with_income_nouns_are_not_income(self) -> None:
        """R1-7: un sustantivo suelto no invierte una frase de egreso."""
        for text in [
            "honorarios del contador 1500 bs",
            "pagaron la luz 200 bs",
            "pasaje al coworking remoto 60 bs",
            "registra el deposito de garantia del alquiler 1500 bs",
            "pago de sueldo del empleado 3000 bs",
        ]:
            with self.subTest(text=text):
                self.assertFalse(looks_like_income(text))
                self.assertIsNone(parse_income(text))

    def test_income_noun_labeling_an_amount_is_income(self) -> None:
        self.assertTrue(looks_like_income("sueldo 6200 bolivianos"))
        self.assertTrue(looks_like_income("depósito de 300 bolivianos"))


class JournalEntryShapeTests(unittest.TestCase):
    def test_debit_cash_credit_income(self) -> None:
        parsed = parse_income("registra ingreso sueldo 6200 bolivianos")
        assert parsed is not None
        payload = build_journal_entry(parsed, "voice-income:test1")
        self.assertEqual(payload["doctype"], "Journal Entry")
        self.assertEqual(payload["company"], "Jardal")
        self.assertEqual(payload["openceo_ref"], "voice-income:test1")
        cash, income = payload["accounts"]
        self.assertEqual(cash["account"], "Efectivo - J")
        self.assertEqual(cash["debit_in_account_currency"], "6200.00")
        self.assertEqual(income["account"], "Servicios - J")
        self.assertEqual(income["credit_in_account_currency"], "6200.00")
        self.assertEqual(cash["cost_center"], "Principal - J")

    def test_zero_legs_are_decimal_strings(self) -> None:
        """R1-22: las patas cero viajaban como float `0.0`."""
        parsed = parse_income("registra ingreso sueldo 6200 bolivianos")
        assert parsed is not None
        payload = build_journal_entry(parsed, "voice-income:test1")
        cash, income = payload["accounts"]
        self.assertEqual(cash["credit_in_account_currency"], "0.00")
        self.assertEqual(income["debit_in_account_currency"], "0.00")
        for leg in (cash, income):
            self.assertIsInstance(leg["debit_in_account_currency"], str)
            self.assertIsInstance(leg["credit_in_account_currency"], str)

    def test_foreign_currency_without_rate_does_not_build_a_payload(self) -> None:
        """R1-2: US$500 no puede entrar al libro como Bs 500."""
        parsed = parse_income("me pagaron 500 dólares del contrato")
        assert parsed is not None
        self.assertEqual(parsed.currency, "USD")
        self.assertIsNone(parsed.exchange_rate)
        with self.assertRaises(MissingExchangeRateError):
            build_journal_entry(parsed, "voice-income:usd")

    def test_foreign_currency_with_rate_converts_to_company_currency(self) -> None:
        parsed = parse_income(
            "me pagaron 500 dólares del contrato", exchange_rate=Decimal("6.96")
        )
        assert parsed is not None
        payload = build_journal_entry(parsed, "voice-income:usd")
        cash, income = payload["accounts"]
        self.assertEqual(cash["debit_in_account_currency"], "3480.00")
        self.assertEqual(income["credit_in_account_currency"], "3480.00")
        self.assertIn("USD", payload["user_remark"])
        self.assertIn("6.96", payload["user_remark"])


class RegisterIncomeTests(unittest.TestCase):
    def test_create_and_submit(self) -> None:
        async def scenario() -> None:
            client = FakeERPNextClient()
            parsed = parse_income("registra ingreso sueldo 6200 bolivianos")
            assert parsed is not None
            result = await register_income(client, parsed, ref="voice-income:test1")
            self.assertTrue(result["ok"])
            self.assertFalse(result["duplicate"])
            self.assertEqual(result["doc"]["docstatus"], 1)
            self.assertIn("ACC-JV-TEST-00001", client.submitted)
            summary = build_spoken_summary(parsed, result["doc"], False)
            self.assertIn("6200.00", summary)
            self.assertIn("ingreso", summary)
            self.assertNotIn("voice-income:test1", summary)

        asyncio.run(scenario())

    def test_retry_same_ref_is_idempotent(self) -> None:
        async def scenario() -> None:
            client = FakeERPNextClient()
            parsed = parse_income("anota ingreso docencia 3800 bs")
            assert parsed is not None
            first = await register_income(client, parsed, ref="voice-income:retry")
            second = await register_income(client, parsed, ref="voice-income:retry")
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["doc"]["name"], second["doc"]["name"])
            self.assertEqual(len(client.docs), 1)
            self.assertIn("no lo dupliqué", build_spoken_summary(parsed, second["doc"], True))

        asyncio.run(scenario())

    def test_different_ref_creates_distinct_operation(self) -> None:
        async def scenario() -> None:
            client = FakeERPNextClient()
            parsed = parse_income("anota ingreso docencia 3800 bs")
            assert parsed is not None
            first = await register_income(client, parsed, ref="voice-income:a")
            second = await register_income(client, parsed, ref="voice-income:b")
            self.assertFalse(second["duplicate"])
            self.assertNotEqual(first["doc"]["name"], second["doc"]["name"])

        asyncio.run(scenario())

    def test_refs_are_unique_and_prefixed(self) -> None:
        ref = new_openceo_ref()
        self.assertTrue(ref.startswith("voice-income:"))
        self.assertNotEqual(ref, new_openceo_ref())


if __name__ == "__main__":
    unittest.main()
