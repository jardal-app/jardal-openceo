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

from agents.finance.money_text import (  # noqa: E402
    MissingExchangeRateError,
    detect_currency,
    is_question,
)
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

    def test_percent_is_not_the_amount(self) -> None:
        """R1-20: "10 %" es una tasa, no dinero; el importe es lo que la sigue."""
        parsed = parse_expense("anota el gasto del 10 % de comisión del banco 120 bs")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.amount, Decimal("120"))

    def test_question_only_when_it_opens_the_phrase(self) -> None:
        """R1-8: la voz no trae tildes; "como"/"donde" a mitad de frase no son pregunta."""
        for text in [
            "registra como gasto 50 bs",
            "anota el pago del internet 150 bs como servicio",
            "compré donde Anita 35 bs",
            "gasté como 50 bs en pan",
        ]:
            with self.subTest(text=text):
                self.assertFalse(is_question(text))
                self.assertTrue(looks_like_expense(text))
        for text in ["¿cuándo me pagaron los 500 dólares?", "cuando me pagaron los 500 dolares"]:
            with self.subTest(text=text):
                self.assertTrue(is_question(text))
                self.assertFalse(looks_like_expense(text))

    def test_receipt_verbs_are_not_expenses(self) -> None:
        """R1-7: dinero que ENTRA no se escribe con el signo del gasto."""
        for text in [
            "me pagaron 500 dólares del contrato",
            "cobré 1200 bs del cliente",
            "registra ingreso sueldo 6200 bolivianos",
        ]:
            with self.subTest(text=text):
                self.assertFalse(looks_like_expense(text))
        # El egreso que en la base ya era correcto sigue siéndolo: "pagaron"
        # desnudo dejó de convertir un pago en un ingreso.
        for text in [
            "pagaron la luz 200 bs",
            "honorarios del contador 1500 bs",
            "pasaje al coworking remoto 60 bs",
            "registra el deposito de garantia del alquiler 1500 bs",
        ]:
            with self.subTest(text=text):
                self.assertTrue(looks_like_expense(text))
        # Una declarativa futura no es un registro de nada.
        self.assertFalse(
            looks_like_expense("el sueldo del empleado sube a 3000 el próximo mes")
        )


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

    def test_salaries_have_their_own_account(self) -> None:
        """R1-14: sin esta rama todo sueldo caia en 'Gastos varios - J'."""
        for concept in [
            "sueldo del empleado",
            "salario",
            "nomina",
            "planilla",
            "pago de sueldo",
            "salarios atrasados",
            "sueldos del mes",
        ]:
            with self.subTest(concept=concept):
                self.assertEqual(classify_expense_account(concept), "Salario. - J")
        self.assertEqual(
            classify_expense_account("pago de luz de casa"), "Servicios públicos - J"
        )

    def test_parse_amount_never_float(self) -> None:
        self.assertEqual(parse_amount("50"), Decimal("50"))
        self.assertIsNone(parse_amount("0"))
        self.assertIsNone(parse_amount("-5"))
        self.assertIsNone(parse_amount("abc"))

    def test_parse_amount_reads_thousands_separator(self) -> None:
        """R1-1: en Bolivia "6.200" son seis mil doscientos, no seis con veinte."""
        cases = {
            "6.200": Decimal("6200"),
            "12.345": Decimal("12345"),
            "1,234": Decimal("1234"),
            "1.234.567": Decimal("1234567"),
            "1.234,56": Decimal("1234.56"),
            "1,234.56": Decimal("1234.56"),
            "1234.56": Decimal("1234.56"),
            "6.20": Decimal("6.20"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_amount(raw), expected)

    def test_detect_currency_reaches_the_dollar_sign(self) -> None:
        """R1-3: `$` dentro de \\b...\\b era inalcanzable y $500 se leia como BOB."""
        self.assertEqual(detect_currency("$500", 1, 4), "USD")
        self.assertEqual(detect_currency("gasto 500 $ en comida", 6, 9), "USD")
        self.assertEqual(detect_currency("gasto 500$ en comida", 6, 10), "USD")
        self.assertEqual(detect_currency("gasto 500 bs en comida", 6, 9), "BOB")


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

    def test_zero_legs_are_decimal_strings(self) -> None:
        """R1-22: las patas cero viajaban como float `0.0`."""
        parsed = parse_expense("registra gastos personales carne 50 bolivianos")
        assert parsed is not None
        payload = build_journal_entry(parsed, "voice-expense:test1")
        debit_leg, credit_leg = payload["accounts"]
        self.assertEqual(debit_leg["credit_in_account_currency"], "0.00")
        self.assertEqual(credit_leg["debit_in_account_currency"], "0.00")
        for leg in (debit_leg, credit_leg):
            self.assertIsInstance(leg["debit_in_account_currency"], str)
            self.assertIsInstance(leg["credit_in_account_currency"], str)

    def test_foreign_currency_without_rate_does_not_build_a_payload(self) -> None:
        """R1-2 (gemelo): sin tasa real no se escribe el mismo numero en BOB."""
        parsed = parse_expense("registra gasto personal 10 usd")
        assert parsed is not None
        self.assertEqual(parsed.currency, "USD")
        with self.assertRaises(MissingExchangeRateError):
            build_journal_entry(parsed, "voice-expense:usd")

    def test_foreign_currency_with_rate_converts_to_company_currency(self) -> None:
        parsed = parse_expense("registra gasto personal 10 usd", exchange_rate=Decimal("6.96"))
        assert parsed is not None
        payload = build_journal_entry(parsed, "voice-expense:usd")
        debit_leg, credit_leg = payload["accounts"]
        self.assertEqual(debit_leg["debit_in_account_currency"], "69.60")
        self.assertEqual(credit_leg["credit_in_account_currency"], "69.60")
        self.assertIn("USD", payload["user_remark"])
        self.assertIn("6.96", payload["user_remark"])


if __name__ == "__main__":
    unittest.main()
