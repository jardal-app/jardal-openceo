"""Delegation by category and project (TASK-2026-00340). No tokens."""

import sys
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

import delegation as dlgmod  # noqa: E402
from delegation import (  # noqa: E402
    DelegationError,
    SimulatedDelegatedClient,
    configure_delegated_client,
    delegated_fingerprint,
    delegation_record,
    grant_delegation,
    is_delegated,
    list_delegations,
    reset_delegated_clients,
    reset_delegations,
    revoke_delegation,
)
from orchestration import precedents as prec  # noqa: E402
from tool_locks import check_delegated_action  # noqa: E402


def _mandate() -> Any:
    from company_mandate import parse_mandate

    return parse_mandate({
        "version": 1,
        "ejes": [],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": "0.00",
            "cuota": "suscripciones_ya_pagadas",
        },
        "limites": {
            "objetivos_activos": 3,
            "objetivos_activos_por_proyecto": 2,
            "tickets_activos_por_objetivo": 6,
        },
        "revision_ceo": {"cadencia": "semanal"},
        "escalar_al_dueno": [
            "gasto_nuevo",
            "accion_irreversible",
            "replanificacion_estrategica",
            "cambio_de_mandato",
            "revision_exigida_por_politica",
        ],
    })


class GrantRevokeTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_delegations()

    def tearDown(self) -> None:
        reset_delegations()

    def test_grant_needs_evidence_project_and_category(self) -> None:
        with self.assertRaises(DelegationError):
            grant_delegation(project="p", category="jira", evidence="  ")
        with self.assertRaises(DelegationError):
            grant_delegation(project="  ", category="jira", evidence="e")
        with self.assertRaises(DelegationError):
            grant_delegation(project="p", category="  ", evidence="e")

    def test_grant_revoke_and_scope(self) -> None:
        self.assertFalse(
            is_delegated(project="jardal-openceo", category="jira"))
        record = grant_delegation(
            project="jardal-openceo", category="jira",
            evidence="3 propuestas aceptadas sin cambios, sin incidentes")
        self.assertTrue(
            is_delegated(project="jardal-openceo", category="jira"))
        self.assertEqual(record["evidence"][:1], "3")
        self.assertEqual(
            delegation_record("jardal-openceo", "jira")["evidence"],
            record["evidence"])
        self.assertFalse(
            is_delegated(project="otro-proyecto", category="jira"))
        self.assertFalse(
            is_delegated(project="jardal-openceo", category="autotask"))
        self.assertTrue(
            revoke_delegation(project="jardal-openceo", category="jira"))
        self.assertFalse(
            is_delegated(project="jardal-openceo", category="jira"))
        self.assertFalse(
            revoke_delegation(project="jardal-openceo", category="jira"))
        self.assertEqual(list_delegations(project="jardal-openceo"), [])


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_delegations()

    def tearDown(self) -> None:
        reset_delegations()

    def test_delegated_runs_without_escalation(self) -> None:
        grant_delegation(
            project="jardal-openceo", category="jira", evidence="owner-note-1")
        decision = check_delegated_action(
            project="jardal-openceo", category="jira")
        self.assertFalse(decision["escalate"])
        self.assertTrue(decision["delegated"])
        self.assertIsNone(decision["human_action"])

    def test_outside_delegation_escalates_with_action(self) -> None:
        decision = check_delegated_action(
            project="jardal-openceo", category="jira")
        self.assertTrue(decision["escalate"])
        self.assertFalse(decision["delegated"])
        self.assertIn("jira", str(decision["human_action"]))
        self.assertIn("jardal-openceo", str(decision["human_action"]))


class FingerprintTests(unittest.TestCase):
    def test_stable_and_bound_to_exact_params(self) -> None:
        base = delegated_fingerprint("jira", "transition_issue", "PROJ-1", "")
        self.assertEqual(
            base, delegated_fingerprint(
                "jira", "transition_issue", "PROJ-1", ""))
        self.assertNotEqual(
            base, delegated_fingerprint(
                "jira", "transition_issue", "PROJ-2", ""))
        self.assertNotEqual(
            base, delegated_fingerprint(
                "jira", "close_issue", "PROJ-1", ""))
        self.assertNotEqual(
            base, delegated_fingerprint(
                "autotask", "transition_issue", "PROJ-1", ""))


class PrecedentNeverDelegatesTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_delegations()

    def tearDown(self) -> None:
        reset_delegations()

    def test_answers_leave_no_delegation_behind(self) -> None:
        store = prec.empty_store()
        prec.record_owner_precedent(
            store, project="jardal-openceo", scope="external:jira",
            question_task_id="q1", question_kind="lock",
            question_summary="Enviar?", origin_task_id=None,
            approve=True, answered_by="owner", comment="",
            decided_at=None)
        from external_send import record_external_answer

        record_external_answer(
            store, project="jardal-openceo", tool="jira",
            question_task_id="q2", question_summary="Enviar?",
            origin_task_id=None, approve=True, answered_by="owner",
            comment="", decided_at=None)
        for category in ("jira", "teams", "autotask", "external:jira"):
            self.assertFalse(
                is_delegated(
                    project="jardal-openceo", category=category))


def _ext_task(task_id: str, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "title": f"Ticket {task_id}",
        "description": f"Cuerpo de {task_id}",
        "project": "jardal-openceo",
        "status": "in_progress",
        "priority": "medium",
        "ticket_number": None,
        "params": {},
        "updated_at": datetime(2026, 10, 28, 12, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


class _ExtRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.reviews: list[dict[str, Any]] = []

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return deepcopy(row) if row is not None else None

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        task.update(deepcopy(updates))
        return deepcopy(task)

    async def create_task_review(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.reviews.append(deepcopy(doc))
        return deepcopy(doc)

    async def latest_workflow_run_for_task(
        self, task_id: str
    ) -> dict[str, Any] | None:
        return None

    async def find_task_by_source(
        self, source: str, source_id: str
    ) -> dict[str, Any] | None:
        for row in self.tasks.values():
            if row.get("source") == source \
                    and row.get("source_id") == source_id:
                return deepcopy(row)
        return None

    async def find_task_by_business_name(
        self, name: str
    ) -> dict[str, Any] | None:
        return None

    async def list_tasks_blocked_by(
        self, task_id: str
    ) -> list[dict[str, Any]]:
        return []


class _ExtHub:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        del payload
        self.events.append(event)


class _ExtConfig:
    def load_project(self, project: str | None) -> dict[str, Any]:
        del project
        return {}

    def load_company_mandate(self) -> Any:
        return _mandate()


def _ext_service(repo: _ExtRepo):
    from settings import Settings
    from task_service import TaskService

    service = TaskService(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        repo=repo,  # type: ignore[arg-type]
        graph=MagicMock(),
        realtime_hub=_ExtHub(),  # type: ignore[arg-type]
        workflow_service=None,
        config_loader=_ExtConfig(),
    )

    async def fake_create(payload: Any) -> dict[str, Any]:
        task_id = f"q{len(repo.tasks) + 1}"
        doc = _ext_task(
            task_id, status="queued",
            title=getattr(payload, "title", f"Ticket {task_id}"),
            description=getattr(payload, "description", ""),
            source=getattr(
                getattr(payload, "source", "api"), "value",
                getattr(payload, "source", "api")),
            source_id=getattr(payload, "source_id", None),
            params=deepcopy(getattr(payload, "params", None) or {}),
        )
        repo.tasks[task_id] = doc
        return deepcopy(doc)

    service.create_task = fake_create  # type: ignore[method-assign]
    return service


class DelegatedFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_delegations()
        reset_delegated_clients()

    def tearDown(self) -> None:
        reset_delegations()
        reset_delegated_clients()

    async def _request(self, service, **kwargs) -> dict[str, Any]:
        params = {"channel": "jira", "operation": "transition_issue",
                  "target": "PROJ-1", "project": "jardal-openceo"}
        params.update(kwargs)
        return await service.tool_delegated_external(**params)

    async def test_delegated_executes_without_asking(self) -> None:
        client = SimulatedDelegatedClient()
        configure_delegated_client("jira", client)
        grant_delegation(
            project="jardal-openceo", category="jira", evidence="owner-note-1")
        service = _ext_service(_ExtRepo())
        result = await self._request(service)
        self.assertTrue(result["ok"])
        self.assertTrue(result["delegated"])
        self.assertEqual(result["status"], "executed")
        self.assertEqual(len(client.executed), 1)
        self.assertEqual(client.executed[0]["target"], "PROJ-1")
        self.assertEqual(service._repo.tasks, {})

    async def test_outside_delegation_asks_and_approves_once(self) -> None:
        client = SimulatedDelegatedClient()
        configure_delegated_client("jira", client)
        repo = _ExtRepo()
        service = _ext_service(repo)
        pending = await self._request(service)
        self.assertTrue(pending["ok"])
        self.assertEqual(pending["status"], "pending_approval")
        self.assertEqual(client.executed, [])
        first = await service.apply_owner_decision(
            str(pending["question_task_id"]), approve=True,
            actor_id="owner", comment="adelante")
        self.assertTrue(first["applied"])
        self.assertEqual(len(client.executed), 1)
        second = await service.apply_owner_decision(
            str(pending["question_task_id"]), approve=True,
            actor_id="owner", comment="otra vez")
        self.assertEqual(len(client.executed), 1)
        self.assertFalse(second["applied"])

    async def test_changed_params_after_approval_need_new_question(
        self,
    ) -> None:
        client = SimulatedDelegatedClient()
        configure_delegated_client("jira", client)
        service = _ext_service(_ExtRepo())
        pending = await self._request(service)
        await service.apply_owner_decision(
            str(pending["question_task_id"]), approve=True,
            actor_id="owner", comment="ok")
        changed = await self._request(service, target="PROJ-2")
        self.assertEqual(changed["status"], "pending_approval")
        self.assertNotEqual(changed["question_task_id"],
                            pending["question_task_id"])
        self.assertEqual(len(client.executed), 1)

    async def test_revoked_delegation_asks_again(self) -> None:
        client = SimulatedDelegatedClient()
        configure_delegated_client("jira", client)
        grant_delegation(
            project="jardal-openceo", category="jira", evidence="owner-note-1")
        service = _ext_service(_ExtRepo())
        first = await self._request(service)
        self.assertEqual(first["status"], "executed")
        self.assertTrue(revoke_delegation(
            project="jardal-openceo", category="jira"))
        second = await self._request(service)
        self.assertEqual(second["status"], "pending_approval")
        self.assertEqual(len(client.executed), 1)

    async def test_message_operation_asks_despite_delegation(self) -> None:
        client = SimulatedDelegatedClient()
        configure_delegated_client("teams", client)
        grant_delegation(
            project="jardal-openceo", category="teams", evidence="owner-note-1")
        service = _ext_service(_ExtRepo())
        result = await service.tool_delegated_external(
            channel="teams", operation="send", target="canal-general",
            detail="Hola.", project="jardal-openceo")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "pending_approval")
        self.assertEqual(client.executed, [])

    async def test_send_tool_asks_despite_delegation(self) -> None:
        from external_send import SimulatedExternalClient
        from external_send import configure_external_client as configure_send
        from external_send import reset_external_clients

        send_client = SimulatedExternalClient()
        configure_send("teams", send_client)
        self.addCleanup(reset_external_clients)
        grant_delegation(
            project="jardal-openceo", category="teams", evidence="owner-note-1")
        service = _ext_service(_ExtRepo())
        result = await service.tool_external_send(
            channel="teams", destination="canal-general", text="Hola.",
            project="jardal-openceo")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "pending_approval")
        self.assertEqual(send_client.sent, [])

    async def test_read_operation_refused_and_unknown_rejected(self) -> None:
        service = _ext_service(_ExtRepo())
        read = await self._request(service, operation="get_issue")
        self.assertFalse(read["ok"])
        self.assertEqual(read["status"], "invalid_input")
        unknown = await self._request(service, channel="sms")
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["status"], "invalid_input")
        blank = await self._request(service, target="  ")
        self.assertFalse(blank["ok"])
        self.assertEqual(blank["status"], "invalid_input")

    async def test_other_project_asks(self) -> None:
        client = SimulatedDelegatedClient()
        configure_delegated_client("jira", client)
        grant_delegation(
            project="jardal-openceo", category="jira", evidence="owner-note-1")
        service = _ext_service(_ExtRepo())
        result = await self._request(service, project="otro-proyecto")
        self.assertEqual(result["status"], "pending_approval")
        self.assertEqual(client.executed, [])

    async def test_delegated_without_client_holds_misconfigured(self) -> None:
        grant_delegation(
            project="jardal-openceo", category="jira", evidence="owner-note-1")
        service = _ext_service(_ExtRepo())
        result = await self._request(service)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "misconfigured")

    async def test_unknown_channel_never_executes(self) -> None:
        client = SimulatedDelegatedClient()
        configure_delegated_client("sms", client)
        grant_delegation(
            project="jardal-openceo", category="sms", evidence="owner-note-1")
        service = _ext_service(_ExtRepo())
        result = await self._request(service, channel="sms")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(client.executed, [])


if __name__ == "__main__":
    unittest.main()
