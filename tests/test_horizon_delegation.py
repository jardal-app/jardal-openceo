"""Tests de HZ-003: Delegación autónoma y dev móvil en Horizon.

Contratos y reglas probadas:
1. Encargar un ticket de alcance personal sobre las sesiones dev existentes.
2. Alcance personal garantizado: is_personal_scope(task) == True y nunca va al espejo de Notion.
3. Desconexión móvil e idempotencia: un reintento recupera el mismo trabajo sin duplicarlo.
4. Entrega única: el resultado se entrega una sola vez ("delivered once") tolerante a reconexión.
5. Medición de tiempo humano: tiempo de supervisión, retrabajo y conteo de intervenciones (§10.3).
6. Clasificador determinista: detecta delegación, entregas y métricas antes del LLM sin robar comandos dev.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for path in (CORE_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from horizon import classify, privacy
from horizon.models import ActionOwner, ActionStatus
from horizon.service import HorizonService
from horizon_support import FakeHorizonRepo


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "HORIZON_OWNER_USER_ID": "usr_owner",
        "HORIZON_TIMEZONE": "America/La_Paz",
        "HORIZON_CAPACITY_FRACTION": 0.70,
        "HORIZON_SOURCE_PATH": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeTaskService:
    def __init__(self) -> None:
        self.created_tasks: list[dict[str, Any]] = []

    async def create_task(self, req: Any) -> dict[str, Any]:
        task_id = f"task_{len(self.created_tasks) + 1:03d}"
        doc = req.model_dump() if hasattr(req, "model_dump") else dict(req)
        doc["task_id"] = task_id
        doc["status"] = "in_progress" if doc.get("autorun") else "queued"
        self.created_tasks.append(doc)
        return doc


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = FakeHorizonRepo()
        self.task_service = FakeTaskService()
        self.service = HorizonService(_settings(), self.repo, task_service=self.task_service)
        await self.service.import_context("usr_owner")


class DelegationExecutionTests(_Base):
    async def test_delegate_action_creates_personal_task_and_updates_action(self) -> None:
        actions = await self.repo.list_horizon_actions("usr_owner")
        self.assertTrue(actions, "Debe haber acciones tras la importación")
        action = actions[0]
        aid = action["action_id"]

        result = await self.service.delegate_action(
            "usr_owner",
            action_id=aid,
            project="jardal-openceo",
            instruction="Implementar pruebas unitarias",
        )

        self.assertTrue(result["delegated"])
        self.assertFalse(result["already_in_progress"])
        task_id = result["task_id"]
        self.assertTrue(task_id)

        # Verificar que la tarea fue creada con alcance personal
        self.assertEqual(len(self.task_service.created_tasks), 1)
        created_task = self.task_service.created_tasks[0]
        self.assertEqual(created_task["task_id"], task_id)
        self.assertTrue(privacy.is_personal_scope(created_task))
        self.assertEqual(privacy.personal_owner(created_task), "usr_owner")
        self.assertTrue(created_task.get("autorun"))

        # Verificar que la accion en Horizon paso a in_progress con owner agente
        updated = await self.repo.get_horizon_action(aid)
        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], ActionStatus.IN_PROGRESS.value)
        self.assertEqual(updated["owner"], ActionOwner.AGENT.value)
        self.assertEqual(updated["task_id"], task_id)
        self.assertIsNotNone(updated["delegated_at"])

    async def test_delegate_next_action_without_explicit_id(self) -> None:
        result = await self.service.delegate_action("usr_owner")
        self.assertTrue(result["delegated"])
        self.assertTrue(result["action_id"])
        self.assertTrue(result["task_id"])

    async def test_mobile_disconnect_idempotent_no_duplicate_task(self) -> None:
        actions = await self.repo.list_horizon_actions("usr_owner")
        aid = actions[0]["action_id"]

        # Primer encargo (cliente movil envia la orden)
        res1 = await self.service.delegate_action("usr_owner", action_id=aid)
        self.assertTrue(res1["delegated"])
        self.assertFalse(res1["already_in_progress"])
        first_task_id = res1["task_id"]
        self.assertEqual(len(self.task_service.created_tasks), 1)

        # Supongamos que el movil se desconecto y reintenta
        res2 = await self.service.delegate_action("usr_owner", action_id=aid)
        self.assertTrue(res2["delegated"])
        self.assertTrue(res2["already_in_progress"])
        self.assertEqual(res2["task_id"], first_task_id)
        # NO debe haber creado una segunda tarea
        self.assertEqual(len(self.task_service.created_tasks), 1)


class ResultAcknowledgmentAndDeliveryTests(_Base):
    async def test_acknowledge_terminal_captures_evidence_and_prepares_delivery(self) -> None:
        actions = await self.repo.list_horizon_actions("usr_owner")
        aid = actions[0]["action_id"]

        res = await self.service.delegate_action("usr_owner", action_id=aid)
        task_id = res["task_id"]

        simulated_task = {
            "task_id": task_id,
            "status": "completed",
            "last_response_summary": "Tests completados al 100% y pull request creado.",
            "params": {
                privacy.PERSONAL_SCOPE_KEY: privacy.PERSONAL_SCOPE_VALUE,
                privacy.PERSONAL_OWNER_KEY: "usr_owner",
                "review_links": ["https://github.com/org/repo/pull/123"],
                "test_url": "https://test.dev.internal",
            },
        }

        ack = await self.service.acknowledge_task_result(simulated_task)
        self.assertTrue(ack["recorded"])

        # Comprobar que la accion tiene evidencia y esta lista para entrega unica
        action = await self.repo.get_horizon_action(aid)
        self.assertEqual(action["status"], ActionStatus.DONE.value)
        self.assertIn("Tests completados", action["result_summary"])
        self.assertIn("https://github.com/org/repo/pull/123", action["evidence_links"])
        self.assertIn("https://test.dev.internal", action["evidence_links"])
        self.assertIsNone(action["delivered_at"], "Debe quedar pendiente de entrega humana")

    async def test_delivery_delivered_once_rule(self) -> None:
        actions = await self.repo.list_horizon_actions("usr_owner")
        aid = actions[0]["action_id"]

        res = await self.service.delegate_action("usr_owner", action_id=aid)
        task_id = res["task_id"]

        simulated_task = {
            "task_id": task_id,
            "status": "completed",
            "last_response_summary": "Código desplegado con éxito.",
            "params": {
                privacy.PERSONAL_SCOPE_KEY: privacy.PERSONAL_SCOPE_VALUE,
                privacy.PERSONAL_OWNER_KEY: "usr_owner",
            },
        }
        await self.service.acknowledge_task_result(simulated_task)

        # Primera entrega: no ha sido entregado antes
        delivery1 = await self.service.deliver_action_result("usr_owner", action_id=aid, channel="chat")
        self.assertTrue(delivery1["delivered"])
        self.assertFalse(delivery1["already_delivered"])
        self.assertIsNotNone(delivery1["delivered_at"])
        self.assertIn("Resultado de la tarea delegada", delivery1["message"])

        # Segunda entrega (ej. el móvil se reconecta y consulta de nuevo): entregado una sola vez
        delivery2 = await self.service.deliver_action_result("usr_owner", action_id=aid, channel="chat")
        self.assertTrue(delivery2["delivered"])
        self.assertTrue(delivery2["already_delivered"])
        self.assertEqual(delivery2["delivered_at"], delivery1["delivered_at"])
        self.assertIn("Resultado ya entregado previamente", delivery2["message"])


class HumanMetricsTests(_Base):
    async def test_record_supervision_and_rework_metrics(self) -> None:
        actions = await self.repo.list_horizon_actions("usr_owner")
        aid = actions[0]["action_id"]

        # Registrar primera supervision
        res1 = await self.service.record_delegation_metrics(
            "usr_owner",
            aid,
            human_supervision_minutes=15,
            human_rework_minutes=0,
            rework=False,
        )
        self.assertTrue(res1["recorded"])
        self.assertEqual(res1["human_supervision_minutes"], 15)
        self.assertEqual(res1["human_rework_minutes"], 0)
        self.assertEqual(res1["rework_count"], 0)

        # Registrar retrabajo posterior
        res2 = await self.service.record_delegation_metrics(
            "usr_owner",
            aid,
            human_supervision_minutes=5,
            human_rework_minutes=20,
            rework=True,
        )
        self.assertTrue(res2["recorded"])
        self.assertEqual(res2["human_supervision_minutes"], 20)
        self.assertEqual(res2["human_rework_minutes"], 20)
        self.assertEqual(res2["rework_count"], 1)

    async def test_delegation_summary_aggregation(self) -> None:
        actions = await self.repo.list_horizon_actions("usr_owner")
        aid = actions[0]["action_id"]

        res = await self.service.delegate_action("usr_owner", action_id=aid)
        task_id = res["task_id"]

        await self.service.record_delegation_metrics(
            "usr_owner",
            aid,
            human_supervision_minutes=10,
            human_rework_minutes=5,
            rework=True,
        )

        simulated_task = {
            "task_id": task_id,
            "status": "completed",
            "last_response_summary": "Listo.",
            "params": {
                privacy.PERSONAL_SCOPE_KEY: privacy.PERSONAL_SCOPE_VALUE,
                privacy.PERSONAL_OWNER_KEY: "usr_owner",
            },
        }
        await self.service.acknowledge_task_result(simulated_task)

        summary = await self.service.get_delegation_summary("usr_owner")
        self.assertEqual(summary["total_delegated"], 1)
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["human_supervision_minutes"], 10)
        self.assertEqual(summary["human_rework_minutes"], 5)
        self.assertEqual(summary["rework_count"], 1)
        self.assertEqual(summary["pending_delivery"], 1)


class DeterministicClassificationTests(unittest.TestCase):
    def test_delegation_intents_classified(self) -> None:
        cases = [
            "delega la siguiente acción",
            "encarga la siguiente tarea",
            "delega esta acción",
            "haz la siguiente acción en dev",
            "encarga la siguiente",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_DELEGATE)

    def test_delivery_intents_classified(self) -> None:
        cases = [
            "qué resultado dio la tarea delegada",
            "cómo va lo delegado",
            "cómo va la delegación",
            "qué entregó el dev",
            "resultado de lo delegado",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_DELIVERY)

    def test_metrics_intents_classified(self) -> None:
        cases = [
            "anota 15 minutos de supervisión",
            "fueron 20 minutos de retrabajo",
            "hubo que corregir",
            "hubo retrabajo",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_METRICS)

    def test_parse_metrics_input(self) -> None:
        parsed = classify.parse_metrics_input("fueron 15 minutos de supervisión y 20 minutos de retrabajo")
        self.assertEqual(parsed["human_supervision_minutes"], 15)
        self.assertEqual(parsed["human_rework_minutes"], 20)
        self.assertTrue(parsed["rework"])

    def test_dev_commands_not_hijacked(self) -> None:
        cases = [
            "crea una sesion dev en jardal-trading",
            "corre el preset review",
            "cual es el status del ticket 48",
            "arregla el bug del tmux",
            "lista los tickets",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(classify.classify(text))


if __name__ == "__main__":
    unittest.main()
