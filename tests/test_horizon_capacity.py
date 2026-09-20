"""Capacidad diaria: la tabla de la seccion 10.2, literal.

Esta tabla es el contrato mas facil de romper sin darse cuenta, porque el 70% de
la seccion 9.5 y los numeros redondos de la 10.2 no coinciden a simple vista.
Se fijan aqui antes de que ningun otro modulo los consuma.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from horizon import capacity as capacity_module
from horizon.mandate import HorizonMandate
from horizon.models import Uncertainty


def _mandate(**overrides) -> HorizonMandate:
    base = {"personal_capacity_fraction": 0.70}
    base.update(overrides)
    return HorizonMandate(**base)


# 2026-09-21 es lunes (weekday 0), el dia de 20 minutos de la agenda.
MONDAY = "2026-09-21"
# 2026-09-22 es martes (weekday 1), 15 minutos.
TUESDAY = "2026-09-22"


class AgendaTests(unittest.TestCase):
    def test_weekly_agenda_sums_the_declared_115_minutes(self) -> None:
        self.assertEqual(
            sum(capacity_module.EXERCISE_AGENDA_MINUTES.values()),
            capacity_module.WEEKLY_EXERCISE_MINUTES,
        )

    def test_exercise_minutes_follow_the_weekday(self) -> None:
        self.assertEqual(capacity_module.exercise_minutes_for(MONDAY), 20)
        self.assertEqual(capacity_module.exercise_minutes_for(TUESDAY), 15)

    def test_an_unparseable_date_reserves_nothing(self) -> None:
        self.assertEqual(capacity_module.exercise_minutes_for("no-es-fecha"), 0)

    def test_monday_is_really_a_monday(self) -> None:
        # Si el calendario del test se desplaza, los casos de abajo dejan de
        # probar lo que dicen probar.
        self.assertEqual(date.fromisoformat(MONDAY).weekday(), 0)
        self.assertEqual(date.fromisoformat(TUESDAY).weekday(), 1)


class FloorToStepTests(unittest.TestCase):
    def test_rounds_down_to_the_step(self) -> None:
        self.assertEqual(capacity_module.floor_to_step(42), 40)
        self.assertEqual(capacity_module.floor_to_step(84), 80)
        self.assertEqual(capacity_module.floor_to_step(10.5), 10)
        self.assertEqual(capacity_module.floor_to_step(4.9), 0)

    def test_never_returns_negative(self) -> None:
        self.assertEqual(capacity_module.floor_to_step(-30), 0)


class CapacityTableTests(unittest.TestCase):
    """Las filas de la seccion 10.2, con el ejercicio ya descontado."""

    def _budget(self, free_minutes, *, exercise_included=True, date_key=TUESDAY):
        return capacity_module.compute_capacity(
            mandate=_mandate(),
            date_key=date_key,
            declared_free_minutes=free_minutes,
            exercise_included=exercise_included,
            source="user",
        )

    def test_saturated_day_of_fifteen_minutes_yields_ten(self) -> None:
        capacity = self._budget(15)
        self.assertEqual(capacity.growth_budget_minutes, 10)

    def test_normal_day_of_sixty_minutes_yields_forty(self) -> None:
        # 60 * 0.70 = 42, y la tabla dice "hasta 40".
        capacity = self._budget(60)
        self.assertEqual(capacity.growth_budget_minutes, 40)

    def test_light_day_of_one_twenty_yields_eighty(self) -> None:
        # 120 * 0.70 = 84, y la tabla dice "hasta 80".
        capacity = self._budget(120)
        self.assertEqual(capacity.growth_budget_minutes, 80)

    def test_a_measured_zero_is_zero_and_not_unknown(self) -> None:
        capacity = self._budget(0)
        self.assertEqual(capacity.growth_budget_minutes, 0)
        self.assertEqual(capacity.uncertainty, Uncertainty.DECLARED)

    def test_unknown_free_time_is_not_zero(self) -> None:
        """La regla del modelo: desconocido no es cero (seccion 9.3)."""
        capacity = self._budget(None)
        self.assertIsNone(capacity.growth_budget_minutes)
        self.assertIsNone(capacity.billable_minutes)
        self.assertEqual(capacity.uncertainty, Uncertainty.UNKNOWN)
        self.assertFalse(capacity.free_minutes.is_known)

    def test_the_cap_never_exceeds_the_mandate_fraction(self) -> None:
        strict = capacity_module.compute_capacity(
            mandate=_mandate(personal_capacity_fraction=0.50),
            date_key=TUESDAY,
            declared_free_minutes=120,
            exercise_included=True,
        )
        # 120 * 0.50 = 60, no los 80 de la fraccion por defecto.
        self.assertEqual(strict.growth_budget_minutes, 60)


class ExerciseReserveTests(unittest.TestCase):
    def test_exercise_already_in_declared_time_is_not_discounted_twice(self) -> None:
        capacity = capacity_module.compute_capacity(
            mandate=_mandate(),
            date_key=MONDAY,
            declared_free_minutes=60,
            exercise_included=True,
        )
        self.assertEqual(capacity.exercise_reserved_minutes, 0)
        # 60 * 0.70 = 42 -> 40, sin restar los 20 del lunes.
        self.assertEqual(capacity.growth_budget_minutes, 40)

    def test_unspecified_exercise_is_reserved_and_recorded(self) -> None:
        capacity = capacity_module.compute_capacity(
            mandate=_mandate(),
            date_key=MONDAY,
            declared_free_minutes=60,
            exercise_included=None,
        )
        self.assertEqual(capacity.exercise_reserved_minutes, 20)
        self.assertIn(
            "reserve_exercise:availability_did_not_state_it", capacity.assumptions
        )
        # 60 - 20 = 40; 40 * 0.70 = 28 -> 25.
        self.assertEqual(capacity.growth_budget_minutes, 25)

    def test_declared_time_without_exercise_still_reserves_it(self) -> None:
        capacity = capacity_module.compute_capacity(
            mandate=_mandate(),
            date_key=MONDAY,
            declared_free_minutes=60,
            exercise_included=False,
        )
        self.assertEqual(capacity.exercise_reserved_minutes, 20)


class BlockFittingTests(unittest.TestCase):
    def test_a_short_block_never_receives_a_long_action(self) -> None:
        # La seccion 9.5: un bloque de quince minutos no recibe una tarea de una hora.
        self.assertFalse(capacity_module.fits_block(60, 15))
        self.assertFalse(capacity_module.fits_block(25, 10))

    def test_the_transition_is_charged_against_the_block(self) -> None:
        # 25 minutos de accion + 5 de transicion no caben en 25.
        self.assertFalse(capacity_module.fits_block(25, 25))
        self.assertTrue(capacity_module.fits_block(25, 30))

    def test_an_unknown_effort_does_not_fit_a_short_block(self) -> None:
        self.assertFalse(capacity_module.fits_block(None, 10))

    def test_nothing_fits_a_zero_block(self) -> None:
        self.assertFalse(capacity_module.fits_block(0, 0))


class DescribeCapacityTests(unittest.TestCase):
    def test_unknown_capacity_does_not_invent_an_hour(self) -> None:
        capacity = capacity_module.compute_capacity(
            mandate=_mandate(),
            date_key=TUESDAY,
            declared_free_minutes=None,
        )
        message = capacity_module.describe_capacity(capacity)
        self.assertIn("no te invento una hora", message.lower())

    def test_zero_capacity_says_so_without_apologising(self) -> None:
        capacity = capacity_module.compute_capacity(
            mandate=_mandate(),
            date_key=TUESDAY,
            declared_free_minutes=0,
            exercise_included=True,
        )
        message = capacity_module.describe_capacity(capacity)
        self.assertIn("no programamos crecimiento", message.lower())

    def test_a_real_budget_is_stated_plainly(self) -> None:
        capacity = capacity_module.compute_capacity(
            mandate=_mandate(),
            date_key=TUESDAY,
            declared_free_minutes=60,
            exercise_included=True,
        )
        self.assertIn("40 minutos", capacity_module.describe_capacity(capacity))


if __name__ == "__main__":
    unittest.main()
