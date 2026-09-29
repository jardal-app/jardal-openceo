"""Tests del despacho de intención financiera (TASK-2026-00053).

El orden entre ingreso, aporte, saldo y gasto es el contrato: un error ahí
invierte el signo de un asiento o contesta el saldo cuando se pedía un
ingreso. Incluye la frontera con Horizon, que corre antes en `first_layer` y
antes de esta guarda se quedaba con las frases de dinero.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.finance.intent import (  # noqa: E402
    BALANCE,
    CONTRIBUTION,
    EXPENSE,
    INCOME,
    classify,
    is_finance_text,
)
from horizon import classify as horizon_classify  # noqa: E402


class PrecedenceTests(unittest.TestCase):
    def test_income_wins_over_expense(self) -> None:
        """Los verbos se solapan: 'registra ... 500 bs' vale para los dos."""
        cases = [
            "registra ingreso sueldo 6200 bolivianos",
            "anota ingreso docencia 3800 bs",
            "me pagaron 500 dólares del contrato",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify(text), INCOME)

    def test_contribution_wins_over_balance_and_expense(self) -> None:
        cases = [
            "cómo vamos con el diezmo",
            "cuánto debo del 10 % de lo que cobré",
            "registra mi diezmo 500 bs",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify(text), CONTRIBUTION)

    def test_balance_before_expense(self) -> None:
        for text in ["cuánto gastamos este mes", "dame el saldo de la caja"]:
            with self.subTest(text=text):
                self.assertEqual(classify(text), BALANCE)

    def test_expense_is_the_write_default(self) -> None:
        cases = [
            "registra gasto personal carne 50 bolivianos",
            "pago de luz de casa 300 bs",
            "pago de sueldo del empleado 3000 bs",
            # R1-7: "pagaron" desnudo y los sustantivos de ingreso sueltos no
            # pueden invertir un egreso.
            "pagaron la luz 200 bs",
            "honorarios del contador 1500 bs",
            "pasaje al coworking remoto 60 bs",
            "registra el deposito de garantia del alquiler 1500 bs",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify(text), EXPENSE)

    def test_contribution_words_do_not_swallow_expense_orders(self) -> None:
        """R1-9: "aporte"/"10 %" sueltos interceptaban ordenes de gasto."""
        cases = [
            "registra gasto del aporte patronal 1200 bs",
            "registra pago de luz de casa 300 bs con 10 % de recargo",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify(text), EXPENSE)

    def test_declarative_money_is_not_a_registration(self) -> None:
        """R1-7: una declarativa a futuro no contabiliza nada."""
        self.assertIsNone(classify("el sueldo del empleado sube a 3000 el próximo mes"))

    def test_not_finance(self) -> None:
        cases = [
            "",
            "crea una sesión dev en trading",
            "pon el preset review 2",
            "¿cuántos tickets tenemos abiertos?",
            # Sin monto no hay registro posible: no se reclama la frase.
            "me pagaron el remoto",
            "¿cuándo me pagaron los 500 dólares?",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(classify(text))
                self.assertFalse(is_finance_text(text))

    def test_is_finance_text_agrees_with_classify(self) -> None:
        self.assertTrue(is_finance_text("cuánto gastamos este mes"))
        self.assertFalse(is_finance_text("lista los tickets"))


class HorizonBoundaryTests(unittest.TestCase):
    """Horizon corre antes que Finance en `first_layer`: no debe robar dinero."""

    def test_money_phrases_are_not_horizon_captures(self) -> None:
        cases = [
            # Verbo de captura explícita: sin la guarda sería INTENT_CAPTURE.
            "registra ingreso sueldo 6200 bolivianos",
            "anota ingreso docencia 3800 bs",
            # "me pagaron" está en los patrones de avance: sería INTENT_PROGRESS.
            "me pagaron 500 dólares del contrato",
            # "cómo vamos con" está en las consultas de plan: sería PLAN_QUERY.
            "cómo vamos con el diezmo",
            # Gasto, el caso que ya estaba cubierto antes de este cambio.
            "registra gasto personal carne 50 bolivianos",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(horizon_classify.classify(text))

    def test_horizon_keeps_its_own_phrases(self) -> None:
        self.assertEqual(
            horizon_classify.classify("me pagaron el remoto"),
            horizon_classify.INTENT_PROGRESS,
        )
        self.assertEqual(
            horizon_classify.classify("grabé 4 minutos útiles"),
            horizon_classify.INTENT_PROGRESS,
        )
        self.assertEqual(
            horizon_classify.classify("cómo va el plan?"),
            horizon_classify.INTENT_PLAN_QUERY,
        )
        self.assertEqual(
            horizon_classify.classify("anota que mi hermana necesita ayuda"),
            horizon_classify.INTENT_CAPTURE,
        )

    def test_balance_vocabulary_does_not_hand_personal_phrases_to_finance(self) -> None:
        """R1-10: el vocabulario de saldo es español ordinario, no una señal."""
        cases = [
            ("tengo que ver las cuentas del banco con mi padre", horizon_classify.INTENT_CAPTURE),
            ("me gustaria un resumen del plan de hoy", horizon_classify.INTENT_PLAN_DAY),
            ("hoy solo tengo 15 minutos en total", horizon_classify.INTENT_CAPACITY),
            ("necesito un resumen de mis cuentas del curso", horizon_classify.INTENT_CAPTURE),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertFalse(horizon_classify.is_finance_operative(text))
                self.assertEqual(horizon_classify.classify(text), expected)
        # Las lecturas de saldo siguen siendo finanzas para el ruteo del agente.
        for text in ["cuánto gastamos este mes", "dame el saldo de la caja"]:
            with self.subTest(text=text):
                self.assertFalse(horizon_classify.is_finance_operative(text))
                self.assertTrue(is_finance_text(text))

    def test_horizon_operative_is_ledger_and_contribution_only(self) -> None:
        for text in [
            "registra ingreso sueldo 6200 bolivianos",
            "registra gasto personal carne 50 bolivianos",
            "cómo vamos con el diezmo",
        ]:
            with self.subTest(text=text):
                self.assertTrue(horizon_classify.is_finance_operative(text))

    def test_horizon_guard_survives_a_broken_finance_import(self) -> None:
        """Si Finance no importa, Horizon conserva la frase (fail-open con log)."""
        ledger_phrase = "registra gasto personal carne 50 bolivianos"
        self.assertTrue(horizon_classify.is_finance_operative(ledger_phrase))
        with mock.patch.dict(sys.modules, {"agents.finance.intent": None}):
            with self.assertLogs("horizon.classify", level="WARNING") as captured:
                self.assertFalse(horizon_classify.is_finance_operative(ledger_phrase))
        self.assertTrue(
            any("guard unavailable" in line for line in captured.output),
            captured.output,
        )


if __name__ == "__main__":
    unittest.main()
