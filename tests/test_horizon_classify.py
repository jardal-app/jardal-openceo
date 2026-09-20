"""Clasificador determinista de Horizon.

La propiedad mas importante de este archivo es negativa: una frase sobre la
vida personal **nunca** puede convertirse en una instruccion de codigo. Con una
sesion dev activa, un intents de tipo ``dev_instruction`` escribe el texto en
tmux; una nota familiar ahi seria una fuga.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from horizon import classify
from horizon.models import CaptureKind


class CaptureClassificationTests(unittest.TestCase):
    def test_personal_notes_are_captured(self) -> None:
        cases = [
            "anota que el cliente me pidió otro cambio",
            "apunta que debo revisar el contrato del banco",
            "idea: un asistente musical para el libro de mi padre",
            "se me ocurrió un negocio de rescate animal",
            "quiero hacer un videojuego biblico",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_CAPTURE)

    def test_spec_example_client_request_is_captured(self) -> None:
        """La seccion 9.3 usa esta frase como ejemplo explicito de captura."""
        self.assertEqual(
            classify.classify("el cliente me pidió otro cambio"),
            classify.INTENT_CAPTURE,
        )

    def test_direction_queries(self) -> None:
        for text in ["qué sigue?", "cuál es la siguiente acción", "por dónde empiezo"]:
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_NEXT_ACTION)

    def test_plan_queries(self) -> None:
        for text in ["cómo va el plan?", "qué objetivos tenemos", "cuál es la misión"]:
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_PLAN_QUERY)

    def test_measurable_progress(self) -> None:
        for text in ["grabé 4 minutos útiles", "hice 30 min de ejercicio", "me pagaron el remoto"]:
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_PROGRESS)


class DevIntentsAreNotHijackedTests(unittest.TestCase):
    """El clasificador no debe robarle frases al flujo dev ni a tmux."""

    def test_dev_commands_stay_out_of_horizon(self) -> None:
        cases = [
            "crea una sesion dev en jardal-trading",
            "corre el preset review",
            "cual es el status del ticket 48",
            "arregla el bug del tmux",
            "lista los tickets",
            "cambia al proyecto trading",
            "haz un commit con los cambios",
            "corre pytest en el proyecto",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(classify.classify(text))

    def test_explicit_capture_verb_wins_over_a_technical_word(self) -> None:
        """El usuario declaro su intencion; la nota no debe ir a tmux."""
        text = "anota: revisar el ticket 48 con calma"
        self.assertEqual(classify.classify(text), classify.INTENT_CAPTURE)
        self.assertTrue(classify.is_personal_only(text))

    def test_personal_only_guards_the_dev_paths(self) -> None:
        self.assertTrue(classify.is_personal_only("el cliente me pidió otro cambio"))
        self.assertFalse(classify.is_personal_only("crea una sesion dev"))


class MeasurementExtractionTests(unittest.TestCase):
    def test_minutes_and_hours(self) -> None:
        self.assertEqual(classify.extract_measurement("grabé 4 minutos útiles"), (4, "minutes"))
        self.assertEqual(classify.extract_measurement("terminé 2 horas de revisión"), (120, "minutes"))
        self.assertEqual(classify.extract_measurement("hice 30 min de ejercicio"), (30, "minutes"))

    def test_no_measurement_returns_none(self) -> None:
        self.assertIsNone(classify.extract_measurement("terminé el CV"))


class CaptureKindTests(unittest.TestCase):
    def test_kinds(self) -> None:
        self.assertEqual(classify.classify_capture_kind("pendiente: llamar al banco"), CaptureKind.PENDING)
        self.assertEqual(classify.classify_capture_kind("me comprometí a entregar el lunes"), CaptureKind.COMMITMENT)
        self.assertEqual(classify.classify_capture_kind("grabe cuatro minutos"), CaptureKind.PROGRESS)
        self.assertEqual(classify.classify_capture_kind("quiero hacer un juego"), CaptureKind.IDEA)


class DecisionDetectionTests(unittest.TestCase):
    def test_money_and_commitments_require_human_decision(self) -> None:
        needs, reason = classify.wants_decision("pagar la deuda del banco", CaptureKind.PENDING)
        self.assertTrue(needs)
        self.assertIn("dinero", reason.lower())

        needs, _ = classify.wants_decision("me comprometí con el cliente", CaptureKind.COMMITMENT)
        self.assertTrue(needs)

    def test_ordinary_ideas_need_no_decision(self) -> None:
        needs, reason = classify.wants_decision("quiero hacer un videojuego", CaptureKind.IDEA)
        self.assertFalse(needs)
        self.assertIsNone(reason)


class PrefixStrippingTests(unittest.TestCase):
    def test_imperative_is_removed_but_substance_kept(self) -> None:
        self.assertEqual(
            classify.strip_capture_prefix("anota que el cliente me pidió otro cambio"),
            "el cliente me pidió otro cambio",
        )
        self.assertEqual(
            classify.strip_capture_prefix("pendiente: revisar el contrato"),
            "revisar el contrato",
        )
        self.assertEqual(
            classify.strip_capture_prefix("quiero hacer un videojuego"),
            "quiero hacer un videojuego",
        )

    def test_colon_form_of_the_imperative_is_also_removed(self) -> None:
        self.assertEqual(
            classify.strip_capture_prefix("anota: revisar el ticket 48 con calma"),
            "revisar el ticket 48 con calma",
        )
        self.assertEqual(
            classify.strip_capture_prefix("recuerda: llamar al banco"),
            "llamar al banco",
        )

    def test_stripping_never_returns_an_empty_string(self) -> None:
        self.assertEqual(classify.strip_capture_prefix("anota"), "anota")


class NormalizationTests(unittest.TestCase):
    def test_accents_and_case_collapse(self) -> None:
        self.assertEqual(
            classify.normalize_text("  Grabé CUATRO Minutos  "),
            classify.normalize_text("grabe cuatro minutos"),
        )


if __name__ == "__main__":
    unittest.main()