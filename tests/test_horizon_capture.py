"""Ciclo de captura, progreso y decision del espacio personal.

Cubre el criterio de aceptacion de HZ-001 (seccion 9.8): capturar una idea y un
avance, recordar los objetivos, decidir el proximo paso y **continuar despues de
reiniciar sin repetir datos**.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from horizon.models import ActionStatus, CaptureChannel, CaptureKind, CaptureStatus
from horizon.service import HorizonService
from horizon_support import FakeHorizonRepo


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "HORIZON_TIMEZONE": "America/La_Paz",
        "HORIZON_CAPACITY_FRACTION": 0.70,
        "HORIZON_SOURCE_PATH": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = FakeHorizonRepo()
        self.service = HorizonService(_settings(), self.repo)
        await self.service.import_context("usr_owner")

    async def _milestone(self, source_key: str = "VOZ-001") -> dict:
        growth = await self.repo.get_horizon_objective_by_source_key(
            "usr_owner", "growth_employment"
        )
        return next(m for m in growth["milestones"] if m["source_key"] == source_key)


class CaptureTests(_Base):
    async def test_capture_stores_the_substance_and_links_an_objective(self) -> None:
        result = await self.service.capture(
            "usr_owner",
            "el cliente me pidió otro cambio",
            kind=CaptureKind.PENDING,
            channel=CaptureChannel.CHAT,
        )
        self.assertTrue(result["captured"])
        capture = result["capture"]
        self.assertEqual(capture["user_id"], "usr_owner")
        self.assertEqual(capture["status"], CaptureStatus.LINKED.value)

        clients = await self.repo.get_horizon_objective_by_source_key(
            "usr_owner", "clients_commitments"
        )
        self.assertEqual(capture["linked_objective_id"], clients["objective_id"])

    async def test_repeated_idea_is_not_duplicated(self) -> None:
        text = "quiero hacer un videojuego biblico"
        first = await self.service.capture("usr_owner", text)
        second = await self.service.capture("usr_owner", text)

        self.assertTrue(first["captured"])
        self.assertFalse(second["captured"])
        self.assertTrue(second["duplicate"])
        captures = await self.repo.list_horizon_captures("usr_owner")
        self.assertEqual(len(captures), 1)

    async def test_same_idea_in_different_wording_is_still_one_capture(self) -> None:
        """La deduplicacion normaliza acentos, mayusculas y espacios."""
        await self.service.capture("usr_owner", "Quiero hacer un videojuego bíblico")
        await self.service.capture("usr_owner", "  quiero  hacer un videojuego biblico ")
        captures = await self.repo.list_horizon_captures("usr_owner")
        self.assertEqual(len(captures), 1)

    async def test_idea_linked_to_a_parked_objective_keeps_its_review_condition(self) -> None:
        result = await self.service.capture("usr_owner", "quiero retomar el videojuego de la biblia")
        capture = result["capture"]
        self.assertIsNotNone(capture["review_condition"])
        self.assertNotIn(capture["status"], (CaptureStatus.PLANNED.value,))

    async def test_commitment_requires_human_decision(self) -> None:
        result = await self.service.capture(
            "usr_owner",
            "me comprometí a pagar la deuda del banco",
            kind=CaptureKind.COMMITMENT,
        )
        self.assertTrue(result["requires_decision"])
        self.assertTrue(result["capture"]["requires_decision"])
        self.assertIn("decisión", result["message"])

    async def test_empty_capture_is_rejected(self) -> None:
        result = await self.service.capture("usr_owner", "   ")
        self.assertFalse(result["captured"])
        self.assertEqual(result["reason"], "empty")

    async def test_two_users_do_not_share_captures(self) -> None:
        await self.service.capture("usr_a", "idea privada de A")
        await self.service.capture("usr_b", "idea privada de B")

        captures_a = await self.repo.list_horizon_captures("usr_a")
        captures_b = await self.repo.list_horizon_captures("usr_b")

        self.assertEqual([c["text"] for c in captures_a], ["idea privada de A"])
        self.assertEqual([c["text"] for c in captures_b], ["idea privada de B"])


class ProgressTests(_Base):
    async def test_measurement_updates_the_linked_milestone(self) -> None:
        result = await self.service.record_progress("usr_owner", "grabé 4 minutos útiles")

        self.assertTrue(result["recorded"])
        self.assertEqual(result["metric"], "voice_usable_minutes")
        milestone = result["milestone"]
        self.assertEqual(milestone["base"]["value"], 4)
        self.assertEqual(milestone["base"]["uncertainty"], "declared")
        self.assertEqual(milestone["target"]["value"], 180)
        self.assertTrue(milestone["evidence"])

    async def test_measurements_accumulate_from_unknown(self) -> None:
        voz = await self._milestone()
        self.assertIsNone(voz["base"]["value"])

        await self.service.record_progress("usr_owner", "grabé 4 minutos útiles")
        second = await self.service.record_progress("usr_owner", "grabé 6 minutos útiles")

        self.assertEqual(second["milestone"]["base"]["value"], 10)

    async def test_progress_message_is_voice_friendly(self) -> None:
        result = await self.service.record_progress("usr_owner", "grabé 4 minutos útiles")
        message = result["message"]
        self.assertIn("180", message)
        self.assertNotIn("hml_", message)
        self.assertNotIn("hev_", message)

    async def test_repeated_source_id_does_not_duplicate_progress(self) -> None:
        first = await self.service.record_progress(
            "usr_owner", "grabé 4 minutos útiles", source_id="recibo-123"
        )
        second = await self.service.record_progress(
            "usr_owner", "grabé 4 minutos útiles", source_id="recibo-123"
        )

        self.assertTrue(first["recorded"])
        self.assertFalse(second["recorded"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(self.repo.events), 1)

    async def test_duplicate_event_does_not_double_count_the_metric(self) -> None:
        await self.service.record_progress("usr_owner", "grabé 4 minutos útiles", source_id="r-1")
        await self.service.record_progress("usr_owner", "grabé 4 minutos útiles", source_id="r-1")

        voz = await self._milestone()
        self.assertEqual(voz["base"]["value"], 4)

    async def test_same_phrase_on_another_day_is_a_separate_event(self) -> None:
        """Sin origen externo la clave incluye el dia: no pierde avances reales."""
        first = await self.service.record_progress("usr_owner", "grabé 4 minutos útiles")
        # Se registra el segundo dia simulando un identificador distinto.
        second = await self.service.record_progress(
            "usr_owner", "grabé 4 minutos útiles", source_id="dia-siguiente"
        )
        self.assertTrue(first["recorded"])
        self.assertTrue(second["recorded"])
        self.assertEqual(second["milestone"]["base"]["value"], 8)

    async def test_progress_without_measurement_is_recorded_as_unknown(self) -> None:
        result = await self.service.record_progress("usr_owner", "terminé el CV")
        self.assertTrue(result["recorded"])
        self.assertIsNone(result["metric"])
        self.assertIsNone(result["event"]["value"]["value"])
        self.assertEqual(result["event"]["value"]["uncertainty"], "unknown")


class NextActionTests(_Base):
    async def test_next_action_returns_one_primary_action(self) -> None:
        result = await self.service.next_action("usr_owner")

        self.assertTrue(result["has_action"])
        self.assertIsNotNone(result["objective"])
        self.assertEqual(result["ready_count"], 2)
        self.assertEqual(result["in_progress_count"], 0)
        self.assertEqual(result["limits"]["capacity_fraction"], 0.70)

    async def test_in_progress_action_keeps_continuity_over_a_new_one(self) -> None:
        # Se muta el estado almacenado, no una copia devuelta por el repo.
        financial = next(
            a for a in self.repo.actions.values() if int(a.get("priority") or 0) == 20
        )
        financial["status"] = ActionStatus.IN_PROGRESS.value

        result = await self.service.next_action("usr_owner")

        self.assertEqual(result["action"]["action_id"], financial["action_id"])
        self.assertEqual(result["in_progress_count"], 1)

    async def test_next_action_message_is_short_and_has_no_identifiers(self) -> None:
        result = await self.service.next_action("usr_owner")
        message = result["message"]
        self.assertIn("Lo siguiente es", message)
        self.assertNotIn("hact_", message)
        self.assertNotIn("hob_", message)

    async def test_action_carries_acceptance_and_next_step(self) -> None:
        result = await self.service.next_action("usr_owner")
        action = result["action"]
        self.assertTrue(action["acceptance"])
        self.assertTrue(action["next_step"])


class PlanSummaryTests(_Base):
    async def test_plan_exposes_mission_growth_goal_and_voice_state(self) -> None:
        summary = await self.service.plan_summary("usr_owner")

        self.assertIn("trabajo digno", summary["mission"])
        self.assertEqual(summary["plan_version"], "2.1")
        self.assertIsNotNone(summary["growth_goal"])
        self.assertEqual(summary["growth_goal"]["source_key"], "growth_employment")

    async def test_unmeasured_voice_does_not_invent_a_date(self) -> None:
        summary = await self.service.plan_summary("usr_owner")
        voice = summary["voice"]
        self.assertFalse(voice["confirmed"])
        self.assertIsNone(voice["usable_minutes"])
        self.assertIsNone(voice["remaining_minutes"])
        self.assertIn("no tenemos minutos", summary["message"].lower())

    async def test_voice_projection_appears_once_measured(self) -> None:
        await self.service.record_progress("usr_owner", "grabé 30 minutos útiles")
        summary = await self.service.plan_summary("usr_owner")
        self.assertTrue(summary["voice"]["confirmed"])
        self.assertEqual(summary["voice"]["remaining_minutes"], 150)

    async def test_parked_ideas_are_listed_but_not_as_todays_work(self) -> None:
        summary = await self.service.plan_summary("usr_owner")
        self.assertGreaterEqual(len(summary["parked_objectives"]), 4)
        self.assertNotIn("parked", [o["status"] for o in summary["active_objectives"]])


class RestartContinuityTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_survives_a_new_service_instance(self) -> None:
        repo = FakeHorizonRepo()
        first_run = HorizonService(_settings(), repo)
        await first_run.import_context("usr_owner")
        await first_run.capture("usr_owner", "quiero hacer un videojuego biblico")
        await first_run.record_progress("usr_owner", "grabé 12 minutos útiles")

        # Reinicio del servicio: nueva instancia, mismo almacenamiento.
        second_run = HorizonService(_settings(), repo)
        profile = await second_run.ensure_profile("usr_owner")
        self.assertEqual(
            profile["declared_context"]["plan_version"],
            "2.1",
        )

        summary = await second_run.plan_summary("usr_owner")
        self.assertTrue(summary["voice"]["confirmed"])
        self.assertEqual(summary["voice"]["usable_minutes"], 12)

        # No vuelve a preguntar por lo ya importado.
        reimport = await second_run.import_context("usr_owner")
        self.assertFalse(reimport["imported"])
        self.assertEqual(reimport["reason"], "already_current")

        # La captura sigue registrada una sola vez.
        captures = await repo.list_horizon_captures("usr_owner")
        self.assertEqual(len(captures), 1)

        # Y la decision sigue disponible.
        action = await second_run.next_action("usr_owner")
        self.assertTrue(action["has_action"])


class EmptySpaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_space_without_import_does_not_invent_objectives(self) -> None:
        repo = FakeHorizonRepo()
        service = HorizonService(_settings(), repo)

        profile = await service.ensure_profile("usr_new")
        self.assertIsNone(profile["declared_context"])

        action = await service.next_action("usr_new")
        self.assertFalse(action["has_action"])
        self.assertIn("importar", action["message"].lower())

    async def test_capture_works_before_any_import(self) -> None:
        repo = FakeHorizonRepo()
        service = HorizonService(_settings(), repo)

        result = await service.capture("usr_new", "quiero aprender ingles")

        self.assertTrue(result["captured"])
        self.assertIsNone(result["capture"]["linked_objective_id"])


if __name__ == "__main__":
    unittest.main()