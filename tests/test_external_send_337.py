"""External sends on behalf of the Owner (TASK-2026-00337). No tokens."""

import sys
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

import external_send as extmod  # noqa: E402
from external_send import (  # noqa: E402
    EXTERNAL_TOOLS,
    ExternalSendError,
    SimulatedExternalClient,
    check_external_send_lock,
    configure_external_client,
    execute_external_send,
    external_send_fingerprint,
    external_send_question_spec,
    external_scope,
    record_external_answer,
    reset_external_clients,
)
from orchestration import precedents as prec  # noqa: E402
from owner_channel import OWNER_QUESTION_PHASE  # noqa: E402


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


class CheckLockTests(unittest.TestCase):
    def test_every_channel_always_escalates(self) -> None:
        for tool in EXTERNAL_TOOLS:
            with self.subTest(tool=tool):
                decision = check_external_send_lock(
                    tool=tool, destination="canal-general",
                    text="Hola, aprueben esto.", mandate=_mandate())
                self.assertTrue(decision["escalate"])
                self.assertIn("canal-general",
                              decision["human_action"])
                self.assertIn("Hola, aprueben esto.",
                              decision["human_action"])

    def test_unknown_channel_and_blanks_fail_closed(self) -> None:
        with self.assertRaises(ExternalSendError):
            check_external_send_lock(
                tool="sms", destination="x", text="t",
                mandate=_mandate())
        with self.assertRaises(ExternalSendError):
            check_external_send_lock(
                tool="teams", destination="  ", text="t",
                mandate=_mandate())
        with self.assertRaises(ExternalSendError):
            check_external_send_lock(
                tool="teams", destination="x", text="  ",
                mandate=_mandate())


class FingerprintTests(unittest.TestCase):
    def test_stable_and_bound_to_exact_text(self) -> None:
        base = external_send_fingerprint("teams", "canal", "texto exacto")
        self.assertEqual(
            base, external_send_fingerprint("teams", "canal",
                                            "texto exacto"))
        self.assertNotEqual(
            base, external_send_fingerprint("teams", "canal",
                                            "texto exacto!"))
        self.assertNotEqual(
            base, external_send_fingerprint("teams", "otro",
                                            "texto exacto"))
        self.assertNotEqual(
            base, external_send_fingerprint("jira", "canal",
                                            "texto exacto"))


class QuestionSpecTests(unittest.TestCase):
    def test_lock_question_shows_destination_and_exact_text(self) -> None:
        decision = check_external_send_lock(
            tool="jira", destination="PROJ-1", text="Texto exacto",
            mandate=_mandate())
        spec = external_send_question_spec(
            tool="jira", destination="PROJ-1", text="Texto exacto",
            decision=decision, project="jardal-openceo")
        block = spec["params"]["owner_question"]
        self.assertEqual(block["kind"], "lock")
        self.assertTrue(block["checkpoint"]["tool_lock"])
        self.assertEqual(block["checkpoint"]["tool"], "jira")
        self.assertEqual(block["tool"]["name"], "jira_send")
        self.assertEqual(block["tool"]["action"]["text"], "Texto exacto")
        self.assertIn("PROJ-1", spec["description"])
        self.assertIn("Texto exacto", spec["description"])
        self.assertTrue(
            str(spec["source_id"]).startswith("tool-lock:"))

    def test_unknown_channel_rejected(self) -> None:
        decision = check_external_send_lock(
            tool="teams", destination="c", text="t",
            mandate=_mandate())
        with self.assertRaises(ExternalSendError):
            external_send_question_spec(
                tool="sms", destination="c", text="t",
                decision=decision, project="p")


class ClientRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_external_clients()

    def tearDown(self) -> None:
        reset_external_clients()

    def test_simulated_client_records_without_sending(self) -> None:
        client = SimulatedExternalClient()
        configure_external_client("teams", client)
        outcome = execute_external_send(
            {"tool": "teams", "destination": "canal",
             "text": "hola"})
        self.assertTrue(outcome["ok"])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["text"], "hola")

    def test_unconfigured_client_fails_closed(self) -> None:
        with self.assertRaises(ExternalSendError):
            execute_external_send(
                {"tool": "autotask", "destination": "c", "text": "t"})

    def test_unknown_channel_rejected(self) -> None:
        with self.assertRaises(ExternalSendError):
            configure_external_client("sms", SimulatedExternalClient())

    def test_scope_names_channel(self) -> None:
        self.assertEqual(external_scope("teams"), "external:teams")
        with self.assertRaises(ExternalSendError):
            external_scope("sms")


class RecordAnswerTests(unittest.TestCase):
    def test_approved_edited_denied_classes(self) -> None:
        store = prec.empty_store()
        record_external_answer(
            store, project="jardal-openceo", tool="teams",
            question_task_id="q1", question_summary="Enviar?",
            origin_task_id=None, approve=True, answered_by="owner",
            comment="", decided_at=None)
        record_external_answer(
            store, project="jardal-openceo", tool="teams",
            question_task_id="q2", question_summary="Enviar?",
            origin_task_id=None, approve=True, answered_by="owner",
            comment="adelante con este texto",
            decided_at="2026-10-28T12:00:00+00:00")
        record_external_answer(
            store, project="jardal-openceo", tool="jira",
            question_task_id="q3", question_summary="Enviar?",
            origin_task_id=None, approve=False, answered_by="owner",
            comment="no", decided_at=None)
        self.assertEqual(
            [store["precedents"][pid]["response"]
             for pid in ("PR-0001", "PR-0002", "PR-0003")],
            ["approved", "edited", "denied"])
        self.assertEqual(
            store["precedents"]["PR-0001"]["scope"], "external:teams")


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
        "updated_at": datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc),
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


def _ext_service(repo: _ExtRepo, store_path: Any = None):
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
    if store_path is not None:
        service._external_precedent_store_path = store_path  # noqa: SLF001

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


class ExternalSendFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_external_clients()

    def tearDown(self) -> None:
        reset_external_clients()

    async def _request(self, service, **kwargs) -> dict[str, Any]:
        params = {"channel": "teams", "destination": "canal-general",
                  "text": "Texto exacto", "project": "jardal-openceo"}
        params.update(kwargs)
        return await service.tool_external_send(**params)

    async def test_no_send_without_approval(self) -> None:
        client = SimulatedExternalClient()
        configure_external_client("teams", client)
        service = _ext_service(_ExtRepo())
        result = await self._request(service)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "pending_approval")
        self.assertEqual(client.sent, [])

    async def test_approval_sends_stored_text_once(self) -> None:
        client = SimulatedExternalClient()
        configure_external_client("teams", client)
        repo = _ExtRepo()
        service = _ext_service(repo)
        pending = await self._request(service)
        question_id = str(pending["question_task_id"])
        first = await service.apply_owner_decision(
            question_id, approve=True, actor_id="owner",
            comment="adelante")
        self.assertTrue(first["applied"])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["text"], "Texto exacto")
        second = await service.apply_owner_decision(
            question_id, approve=True, actor_id="owner",
            comment="adelante otra vez")
        self.assertEqual(len(client.sent), 1)
        self.assertFalse(second["applied"])

    async def test_changed_text_after_approval_needs_new_question(
        self,
    ) -> None:
        client = SimulatedExternalClient()
        configure_external_client("teams", client)
        repo = _ExtRepo()
        service = _ext_service(repo)
        pending = await self._request(service)
        await service.apply_owner_decision(
            str(pending["question_task_id"]), approve=True,
            actor_id="owner", comment="ok")
        self.assertEqual(len(client.sent), 1)
        changed = await self._request(service, text="Texto cambiado")
        self.assertEqual(changed["status"], "pending_approval")
        self.assertNotEqual(changed["question_task_id"],
                            pending["question_task_id"])
        self.assertEqual(len(client.sent), 1)
        question = await repo.get_task(str(pending["question_task_id"]))
        assert question is not None
        self.assertEqual(
            (await service.check_lock_state(
                str(question.get("source_id") or "")))["state"],
            "executed")

    async def test_responses_stay_as_precedents(self) -> None:
        import tempfile

        client = SimulatedExternalClient()
        configure_external_client("jira", client)
        repo = _ExtRepo()
        with tempfile.TemporaryDirectory() as tmp:
            store_path = str(Path(tmp) / "precedents.json")
            service = _ext_service(repo, store_path=store_path)
            approved = await service.tool_external_send(
                channel="jira", destination="PROJ-1",
                text="Parte A", project="jardal-openceo")
            await service.apply_owner_decision(
                str(approved["question_task_id"]), approve=True,
                actor_id="owner", comment="")
            denied = await service.tool_external_send(
                channel="jira", destination="PROJ-2",
                text="Parte B", project="jardal-openceo")
            await service.apply_owner_decision(
                str(denied["question_task_id"]), approve=False,
                actor_id="owner", comment="no todavia")
            stored = prec.load_store(store_path)
            by_question = {
                entry["proposal_ref"]: entry
                for entry in stored["precedents"].values()
            }
            self.assertEqual(
                by_question[
                    str(approved["question_task_id"])]["response"],
                "approved")
            self.assertEqual(
                by_question[
                    str(denied["question_task_id"])]["response"],
                "denied")
            self.assertEqual(
                by_question[
                    str(approved["question_task_id"])]["scope"],
                "external:jira")

    async def test_unconfigured_client_holds_without_sending(
        self,
    ) -> None:
        repo = _ExtRepo()
        service = _ext_service(repo)
        pending = await self._request(service, channel="autotask",
                                      destination="cola",
                                      text="hola")
        result = await service.apply_owner_decision(
            str(pending["question_task_id"]), approve=True,
            actor_id="owner", comment="ok")
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_lock_held")
