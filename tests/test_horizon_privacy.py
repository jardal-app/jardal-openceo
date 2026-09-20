"""Privacidad del espacio personal de Horizon.

Este archivo cubre el unico punto de la Fase 1 con efecto externo
irreversible: la proyeccion a un espejo (Notion). Hay dos rutas y **ambas**
estan probadas aqui, porque filtrar solo una deja los datos personales
publicados en el primer reinicio del servicio.
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

from horizon import privacy
from horizon.mandate import default_mandate, mandate_from_declared
from horizon_support import make_fake_mongo
from task_service import TaskService


def _personal_task(user_id: str = "usr_owner") -> dict:
    return {
        "task_id": "task-personal",
        "source": "api",
        "title": "Grabar 20 minutos de audio",
        "description": "Nota personal del espacio privado",
        "params": privacy.personal_scope_params(user_id),
        "post_actions_executed": [],
    }


def _company_task() -> dict:
    return {
        "task_id": "task-company",
        "source": "api",
        "title": "Corregir bug del dashboard",
        "description": "Ticket normal de proyecto",
        "params": {"workflow": "dev.default"},
        "post_actions_executed": [],
    }


class HorizonScopeTests(unittest.TestCase):
    def test_personal_scope_detected_from_params(self) -> None:
        self.assertTrue(privacy.is_personal_scope(_personal_task()))
        self.assertFalse(privacy.is_personal_scope(_company_task()))
        self.assertFalse(privacy.is_personal_scope(None))
        self.assertFalse(privacy.is_personal_scope({"params": None}))

    def test_owner_is_recorded_and_enforced(self) -> None:
        task = _personal_task("usr_owner")
        self.assertEqual(privacy.personal_owner(task), "usr_owner")
        self.assertTrue(privacy.is_visible_to(task, "usr_owner"))
        # Un tercero no ve el espacio personal aunque comparta el proyecto.
        self.assertFalse(privacy.is_visible_to(task, "usr_other"))

    def test_company_task_keeps_its_normal_visibility(self) -> None:
        task = _company_task()
        self.assertTrue(privacy.is_visible_to(task, "usr_anyone"))


class NotionMirrorRouteOneTests(unittest.TestCase):
    """Ruta 1: el guard reactivo que corre al crear o actualizar una tarea."""

    def _service_with_notion(self) -> TaskService:
        service = TaskService.__new__(TaskService)
        service._settings = SimpleNamespace(NOTION_MIRROR_ENABLED=True)
        service._notion = object()
        return service

    def test_personal_task_never_mirrors(self) -> None:
        service = self._service_with_notion()
        self.assertFalse(service._task_needs_notion_mirror(_personal_task()))

    def test_company_task_still_mirrors(self) -> None:
        service = self._service_with_notion()
        self.assertTrue(service._task_needs_notion_mirror(_company_task()))


class NotionMirrorRouteTwoTests(unittest.IsolatedAsyncioTestCase):
    """Ruta 2: el safety net de reconciliacion que corre al arrancar.

    Si este filtro falta, los datos personales se publican en el primer
    reinicio aunque el guard reactivo este correcto.
    """

    async def test_safety_net_query_excludes_personal_tasks(self) -> None:
        repo = make_fake_mongo([_personal_task(), _company_task()])
        rows = await repo.list_tasks_missing_notion_mirror()

        ids = [row["task_id"] for row in rows]
        self.assertNotIn("task-personal", ids)
        self.assertIn("task-company", ids)

    async def test_safety_net_query_carries_the_exclusion_filter(self) -> None:
        repo = make_fake_mongo([_personal_task()])
        await repo.list_tasks_missing_notion_mirror()

        query = repo.tasks.last_query
        self.assertIsNotNone(query)
        self.assertEqual(query["params.horizon_scope"], {"$ne": "personal"})

    async def test_safety_net_still_reconciles_legacy_tasks_without_params(self) -> None:
        legacy = {"task_id": "task-legacy", "source": "jira", "title": "Viejo"}
        repo = make_fake_mongo([legacy])
        rows = await repo.list_tasks_missing_notion_mirror()
        self.assertEqual([row["task_id"] for row in rows], ["task-legacy"])


class MandateSafetyDefaultsTests(unittest.TestCase):
    def test_default_mandate_disables_personal_mirror_and_money(self) -> None:
        mandate = default_mandate()
        self.assertFalse(mandate.personal_external_mirror_enabled)
        self.assertFalse(mandate.execute_money_movements)
        self.assertTrue(mandate.external_messages_require_human_review)
        self.assertEqual(mandate.personal_capacity_fraction, 0.70)

    def test_imported_mandate_cannot_relax_safety_ceilings(self) -> None:
        """Una importacion solo puede ser mas conservadora, nunca mas permisiva.

        El origen pide 95% de capacidad, 4 objetivos de crecimiento activos y
        ejecutar dinero. Los tres se recortan a la politica vigente.
        """
        declared = {
            "plan_version": "9.9",
            "planning": {
                "personal_capacity_fraction": 0.95,
                "active_growth_goals_max": 4,
                "personal_ready_tickets_max": 40,
                "personal_in_progress_max": 5,
            },
            "money": {"execute_money_movements": True},
        }
        mandate = mandate_from_declared(declared)
        self.assertFalse(mandate.personal_external_mirror_enabled)
        self.assertFalse(mandate.execute_money_movements)
        self.assertEqual(mandate.personal_capacity_fraction, 0.70)
        self.assertEqual(mandate.active_growth_goals_max, 1)
        self.assertEqual(mandate.personal_ready_actions_max, 6)
        self.assertEqual(mandate.personal_in_progress_max, 1)

    def test_imported_mandate_may_be_more_conservative(self) -> None:
        """Bajar un limite respecto a la politica si se respeta."""
        declared = {
            "planning": {"personal_capacity_fraction": 0.40, "personal_ready_tickets_max": 2}
        }
        mandate = mandate_from_declared(declared)
        self.assertEqual(mandate.personal_capacity_fraction, 0.40)
        self.assertEqual(mandate.personal_ready_actions_max, 2)

    def test_capacity_ceiling_can_be_tightened_from_settings(self) -> None:
        declared = {"planning": {"personal_capacity_fraction": 0.70}}
        mandate = mandate_from_declared(declared, capacity_ceiling=0.50)
        self.assertEqual(mandate.personal_capacity_fraction, 0.50)

    def test_missing_fields_keep_safe_defaults(self) -> None:
        mandate = mandate_from_declared({})
        self.assertEqual(mandate.personal_capacity_fraction, 0.70)
        self.assertEqual(mandate.personal_ready_actions_max, 6)
        self.assertFalse(mandate.execute_money_movements)


if __name__ == "__main__":
    unittest.main()