"""Single human channel: owner questions (TASK-2026-00206). No tokens."""

import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from owner_channel import (  # noqa: E402
    OWNER_QUESTION_PHASE,
    OwnerChannelError,
    build_owner_question,
    build_tool_ticket,
    denied_origin_verdict,
    is_owner_question,
    owner_answer_record,
    question_answered,
    tool_proposal_source_id,
)


def _question(**overrides):
    params = {
        "kind": "question",
        "question": "Apruebas el gasto de 50 USD en el dominio?",
        "human_action": "Aprobar o rechazar el gasto",
        "origin_task_id": "origin-1",
        "origin_title": "Comprar dominio",
    }
    params.update(overrides)
    return build_owner_question(**params)


class BuildOwnerQuestionTests(unittest.TestCase):
    def test_spec_shape(self) -> None:
        spec = _question()
        self.assertEqual(spec["phase"], OWNER_QUESTION_PHASE)
        self.assertEqual(spec["waiting_reason"], "owner_question:question")
        self.assertFalse(spec["autorun"])
        self.assertIn("pregunta-dueno", spec["tags"])
        block = spec["params"]["owner_question"]
        self.assertEqual(block["kind"], "question")
        self.assertEqual(block["origin_task_id"], "origin-1")
        self.assertNotIn("decision", block)
        self.assertIn("Apruebas el gasto", spec["title"])
        self.assertIn("Accion humana: Aprobar o rechazar el gasto", spec["description"])
        self.assertIn("origin-1", spec["description"])

    def test_lock_kind_titles_and_checkpoint(self) -> None:
        spec = build_owner_question(
            kind="lock",
            question="Enviar correo a nuevo destinatario?",
            human_action="Aprobar o rechazar el envio",
            origin_task_id="origin-2",
            checkpoint={"tool": "secretary.send_email", "to": "nuevo@x.com"},
        )
        self.assertTrue(spec["title"].startswith("[Candado]"))
        self.assertEqual(spec["waiting_reason"], "owner_question:lock")
        self.assertIn("secretary.send_email", spec["description"])
        self.assertEqual(
            spec["params"]["owner_question"]["checkpoint"]["to"], "nuevo@x.com"
        )

    def test_new_tool_kind_standalone(self) -> None:
        spec = build_owner_question(
            kind="new_tool",
            question="Crear la tool de frontera dns.delete_record?",
            human_action="Aprobar la tool nueva o rechazarla",
            source_id="tool-proposal:OBJ-1:dns.delete_record",
        )
        self.assertTrue(spec["title"].startswith("[Tool nueva]"))
        self.assertIsNone(spec["params"]["owner_question"]["origin_task_id"])
        self.assertEqual(
            spec["source_id"], "tool-proposal:OBJ-1:dns.delete_record"
        )

    def test_title_truncated_to_erpnext_subject(self) -> None:
        spec = build_owner_question(
            kind="question",
            question="x" * 300,
            human_action="Responder",
        )
        self.assertLessEqual(len(spec["title"]), 140)

    def test_context_and_tool_ride_along(self) -> None:
        spec = build_owner_question(
            kind="new_tool",
            question="Crear la tool dns.delete_record?",
            human_action="Aprobar la tool nueva o rechazarla",
            context="Necesidad: borrar registros queda pendiente",
            tool={"name": "dns.delete_record", "need": "n", "spec": "s"},
        )
        self.assertIn("Contexto: Necesidad:", spec["description"])
        self.assertEqual(
            spec["params"]["owner_question"]["tool"]["name"],
            "dns.delete_record",
        )

    def test_filter_rejects_unknown_kind(self) -> None:
        with self.assertRaises(OwnerChannelError):
            build_owner_question(
                kind="reviewers_down",
                question="Se cayeron los revisores",
                human_action="Arreglar el pool",
            )

    def test_filter_rejects_blank_human_action(self) -> None:
        with self.assertRaises(OwnerChannelError):
            build_owner_question(
                kind="question", question="Algo?", human_action="  "
            )

    def test_filter_rejects_blank_question(self) -> None:
        with self.assertRaises(OwnerChannelError):
            build_owner_question(
                kind="question", question=" ", human_action="Responder"
            )


class OwnerQuestionPredicatesTests(unittest.TestCase):
    def test_is_owner_question(self) -> None:
        task = {
            "task_id": "q1",
            "status": "waiting",
            "phase": OWNER_QUESTION_PHASE,
            "params": {"owner_question": {"kind": "lock"}},
        }
        self.assertTrue(is_owner_question(task))
        self.assertFalse(question_answered(task))

    def test_answered_when_decision_recorded(self) -> None:
        task = {
            "task_id": "q1",
            "status": "completed",
            "phase": OWNER_QUESTION_PHASE,
            "params": {"owner_question": {
                "kind": "question",
                "decision": {"decision": "approved"},
            }},
        }
        self.assertTrue(is_owner_question(task))
        self.assertTrue(question_answered(task))

    def test_escalation_is_not_a_question(self) -> None:
        task = {
            "task_id": "e1",
            "status": "waiting",
            "phase": "owner_escalation",
            "params": {"escalation": {"category": "new_spend"}},
        }
        self.assertFalse(is_owner_question(task))

    def test_tier_s_wait_is_not_a_question(self) -> None:
        task = {
            "task_id": "t1",
            "status": "waiting",
            "waiting_reason": "needs_owner: gates failed",
            "params": {},
        }
        self.assertFalse(is_owner_question(task))


class OwnerAnswerRecordTests(unittest.TestCase):
    def test_approve_and_deny_shapes(self) -> None:
        approved = owner_answer_record(
            approve=True, actor_id="owner",
            comment="adelante", decided_at="2026-10-11T12:00:00+00:00",
        )
        self.assertEqual(approved["decision"], "approved")
        self.assertEqual(approved["by"], "owner")
        self.assertEqual(approved["comment"], "adelante")
        denied = owner_answer_record(
            approve=False, actor_id="owner",
            comment="muy caro", decided_at="2026-10-11T12:00:00+00:00",
        )
        self.assertEqual(denied["decision"], "denied")


class DeniedOriginVerdictTests(unittest.TestCase):
    def test_bloqueado_verdict_validates_against_agent_result(self) -> None:
        from objectives.agent_result import AgentResult

        verdict = denied_origin_verdict(
            kind="question", owner_text="Rechazado por el dueno: muy caro."
        )
        parsed = AgentResult.model_validate(verdict)
        self.assertEqual(parsed.resultado.value, "bloqueado")
        self.assertIn("muy caro", parsed.motivo)

    def test_blank_owner_text_still_validates(self) -> None:
        from objectives.agent_result import AgentResult

        verdict = denied_origin_verdict(kind="lock", owner_text="  ")
        AgentResult.model_validate(verdict)


class BuildToolTicketTests(unittest.TestCase):
    def test_dev_ticket_spec(self) -> None:
        spec = build_tool_ticket(
            name="dns.delete_record",
            need="Borrar registros DNS queda pendiente de aprobacion",
            spec_text="Tool de frontera con candado: borrar solo escala al Owner.",
            question_task_id="q9",
            origin_task_id="origin-9",
            project="jardal-openceo",
        )
        self.assertIn("dns.delete_record", spec["title"])
        self.assertIn("candado", spec["description"])
        self.assertEqual(spec["project"], "jardal-openceo")
        self.assertEqual(
            spec["params"]["proposed_tool"]["question_task_id"], "q9"
        )
        self.assertTrue(spec["source_id"].startswith("tool-approved:"))

    def test_proposal_source_id_is_stable(self) -> None:
        self.assertEqual(
            tool_proposal_source_id("OBJ-1", "dns.delete_record"),
            "tool-proposal:OBJ-1:dns.delete_record",
        )


def _svc_task(task_id: str, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "title": f"Ticket {task_id}",
        "description": f"Cuerpo de {task_id}",
        "project": "jardal-openceo",
        "status": "waiting",
        "priority": "medium",
        "ticket_number": None,
        "updated_at": datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _question_task(
    task_id: str, kind: str = "question", origin: str | None = "origin-1",
    **overrides: Any,
) -> dict[str, Any]:
    return _svc_task(
        task_id,
        phase=OWNER_QUESTION_PHASE,
        waiting_reason=f"{OWNER_QUESTION_PHASE}:{kind}",
        params={"owner_question": {
            "kind": kind,
            "question": "Apruebas el gasto?",
            "human_action": "Aprobar o rechazar",
            "origin_task_id": origin,
            "origin_title": None,
            "checkpoint": {"phase": "review"},
        }},
        **overrides,
    )


class _SvcRepo:
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
        return None


class _SvcHub:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        del payload
        self.events.append(event)


class _FakeObjectiveService:
    def __init__(self) -> None:
        self.returns: list[dict[str, Any]] = []
        self.gaps: list[dict[str, Any]] = []

    async def plan_return(
        self, objective_id: str, entry: Any
    ) -> dict[str, Any]:
        self.returns.append({"objective_id": objective_id, "entry": entry})
        return {"ok": True, "tickets_created": 1}

    async def park_return_gap(
        self, objective_id: str, entry: Any
    ) -> dict[str, Any]:
        self.gaps.append({"objective_id": objective_id, "entry": entry})
        return {"ok": True, "reason": "capability_gap"}


def _make_service(repo: _SvcRepo):
    from settings import Settings
    from task_service import TaskService

    return TaskService(
        settings=Settings(),
        repo=repo,  # type: ignore[arg-type]
        graph=MagicMock(),
        realtime_hub=_SvcHub(),  # type: ignore[arg-type]
        workflow_service=None,
    )


class CreateOwnerQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_erpnext_first_and_parks_waiting(self) -> None:
        from state import TaskCreateRequest

        repo = _SvcRepo()
        repo.tasks["q1"] = _svc_task("q1", status="queued")
        service = _make_service(repo)
        seen: list[Any] = []

        async def fake_create(payload: Any) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": "q1", "project": payload.project}

        service.create_task = fake_create  # type: ignore[method-assign]
        result = await service.create_owner_question(
            kind="question",
            question="Apruebas el gasto de 50 USD?",
            human_action="Aprobar o rechazar el gasto",
            origin_task_id="origin-1",
            project="jardal-openceo",
            source_id="owner-q:test-1",
        )
        self.assertEqual(len(seen), 1)
        payload = seen[0]
        self.assertIsInstance(payload, TaskCreateRequest)
        self.assertFalse(payload.autorun)
        self.assertIn("pregunta-dueno", payload.tags or [])
        self.assertEqual(
            (payload.params or {})["owner_question"]["origin_task_id"],
            "origin-1",
        )
        parked = await repo.get_task("q1")
        assert parked is not None
        self.assertEqual(parked["status"], "waiting")
        self.assertEqual(parked["phase"], OWNER_QUESTION_PHASE)
        self.assertEqual(
            parked["waiting_reason"], "owner_question:question"
        )
        self.assertEqual(result["task_id"], "q1")

    async def test_idempotent_hit_on_answered_question_does_not_resurrect(
        self,
    ) -> None:
        repo = _SvcRepo()
        answered = _question_task("q1", origin=None, status="completed")
        answered["params"]["owner_question"]["decision"] = {
            "decision": "approved"
        }
        repo.tasks["q1"] = answered
        service = _make_service(repo)

        async def fake_create(payload: Any) -> dict[str, Any]:
            return deepcopy(answered)

        service.create_task = fake_create  # type: ignore[method-assign]
        result = await service.create_owner_question(
            kind="question",
            question="Otra vez?",
            human_action="Responder",
            project="jardal-openceo",
            source_id="owner-q:dup-1",
        )
        self.assertEqual(result["status"], "completed")
        stored = await repo.get_task("q1")
        assert stored is not None
        self.assertEqual(stored["status"], "completed")

    async def test_filter_error_propagates_without_writes(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        async def _boom(payload: Any) -> dict[str, Any]:
            raise AssertionError("must not create")

        service.create_task = _boom  # type: ignore[method-assign]
        with self.assertRaises(OwnerChannelError):
            await service.create_owner_question(
                kind="reviewers_down",
                question="Se cayeron",
                human_action="Arreglar",
                project="jardal-openceo",
            )


class ApplyOwnerQuestionApproveTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_approve_resumes_origin_with_owner_text(self) -> None:
        from state import TaskReviewAction

        repo = _SvcRepo()
        repo.tasks["origin-1"] = _svc_task(
            "origin-1", waiting_reason="needs_owner: cuanto gasto?",
            params={"owner_question_id": "q1"},
        )
        repo.tasks["q1"] = _question_task("q1")
        service = _make_service(repo)
        wakes: list[bool] = []
        service.set_dispatcher_wake(lambda: wakes.append(True))

        result = await service.apply_owner_decision(
            "q1", approve=True, actor_id="owner", comment="adelante, 50 USD"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_resume")
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "queued")
        self.assertIsNone(origin.get("waiting_reason"))
        answer = (origin.get("params") or {}).get("owner_answer") or {}
        self.assertEqual(answer.get("decision"), "approved")
        self.assertIn("50 USD", answer.get("comment") or "")
        self.assertEqual(wakes, [True])
        question = await repo.get_task("q1")
        assert question is not None
        self.assertEqual(question["status"], "completed")
        block = (question.get("params") or {}).get("owner_question") or {}
        self.assertEqual(
            (block.get("decision") or {}).get("decision"), "approved"
        )
        self.assertEqual(len(repo.reviews), 1)
        self.assertEqual(
            repo.reviews[0]["action"], TaskReviewAction.APPROVE.value
        )

    async def test_lock_approve_resumes_origin(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-2"] = _svc_task(
            "origin-2", waiting_reason="needs_owner: candado correo",
            params={"owner_question_id": "q2"},
        )
        repo.tasks["q2"] = _question_task("q2", kind="lock", origin="origin-2")
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)

        result = await service.apply_owner_decision(
            "q2", approve=True, actor_id="owner", comment="envialo"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_resume")
        origin = await repo.get_task("origin-2")
        assert origin is not None
        self.assertEqual(origin["status"], "queued")
        self.assertIn(
            "envialo",
            ((origin.get("params") or {}).get("owner_answer") or {}).get(
                "comment") or "",
        )

    async def test_approve_without_origin_closes_question(self) -> None:
        repo = _SvcRepo()
        repo.tasks["q9"] = _question_task("q9", origin=None)
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)

        result = await service.apply_owner_decision(
            "q9", approve=True, actor_id="owner", comment="ok"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_close")
        question = await repo.get_task("q9")
        assert question is not None
        self.assertEqual(question["status"], "completed")

    async def test_second_answer_is_rejected(self) -> None:
        repo = _SvcRepo()
        repo.tasks["q1"] = _question_task("q1", origin=None, status="completed")
        service = _make_service(repo)

        result = await service.apply_owner_decision(
            "q1", approve=True, actor_id="owner", comment="otra vez"
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "not_answerable")


class ApplyOwnerQuestionDenyTests(unittest.IsolatedAsyncioTestCase):
    async def test_deny_fails_origin_and_returns_to_planner(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-3"] = _svc_task(
            "origin-3", waiting_reason="needs_owner: gasto?",
            params={"owner_question_id": "q3"},
            objective_id="OBJ-1",
        )
        repo.tasks["q3"] = _question_task("q3", origin="origin-3")
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)
        objectives = _FakeObjectiveService()
        service.set_objective_service(objectives)

        result = await service.apply_owner_decision(
            "q3", approve=False, actor_id="owner", comment="muy caro"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_deny")
        origin = await repo.get_task("origin-3")
        assert origin is not None
        self.assertEqual(origin["status"], "failed")
        self.assertIn("muy caro", origin.get("error") or "")
        verdict = (origin.get("params") or {}).get("agent_result") or {}
        self.assertEqual(verdict.get("resultado"), "bloqueado")
        self.assertEqual(len(objectives.returns), 1)
        self.assertEqual(objectives.returns[0]["objective_id"], "OBJ-1")

    async def test_deny_loose_origin_records_verdict_without_return(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-4"] = _svc_task(
            "origin-4", waiting_reason="needs_owner: gasto?",
            params={"owner_question_id": "q4"},
        )
        repo.tasks["q4"] = _question_task("q4", origin="origin-4")
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)
        objectives = _FakeObjectiveService()
        service.set_objective_service(objectives)

        result = await service.apply_owner_decision(
            "q4", approve=False, actor_id="owner", comment="no"
        )
        self.assertTrue(result["applied"])
        origin = await repo.get_task("origin-4")
        assert origin is not None
        self.assertEqual(origin["status"], "failed")
        self.assertEqual(
            ((origin.get("params") or {}).get("agent_result") or {}).get(
                "resultado"),
            "bloqueado",
        )
        self.assertEqual(objectives.returns, [])


class NewToolApproveTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_creates_dev_ticket_and_returns_origin(self) -> None:
        from state import AgentName

        repo = _SvcRepo()
        repo.tasks["origin-9"] = _svc_task(
            "origin-9", waiting_reason="needs_owner: falta tool",
            params={"owner_question_id": "q9"},
            objective_id="OBJ-9",
        )
        question = _question_task("q9", kind="new_tool", origin="origin-9")
        question["params"]["owner_question"]["tool"] = {
            "name": "dns.delete_record",
            "need": "Borrar registros queda pendiente",
            "spec": "Candado: borrar escala al Owner.",
        }
        repo.tasks["q9"] = question
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)
        objectives = _FakeObjectiveService()
        service.set_objective_service(objectives)
        seen: list[Any] = []

        async def fake_create(payload: Any) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": "dev-1"}

        service.create_task = fake_create  # type: ignore[method-assign]
        result = await service.apply_owner_decision(
            "q9", approve=True, actor_id="owner", comment="creala"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_tool")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].agent_hint, AgentName.DEV)
        self.assertIn("dns.delete_record", seen[0].title)
        self.assertIn("candado", (seen[0].description or "").lower())
        self.assertEqual(seen[0].project, "jardal-openceo")
        origin = await repo.get_task("origin-9")
        assert origin is not None
        self.assertEqual(origin["status"], "failed")
        self.assertEqual(len(objectives.returns), 1)

    async def test_deny_creates_no_dev_ticket(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-9"] = _svc_task(
            "origin-9", waiting_reason="needs_owner: falta tool",
            params={"owner_question_id": "q9"},
        )
        question = _question_task("q9", kind="new_tool", origin="origin-9")
        question["params"]["owner_question"]["tool"] = {
            "name": "dns.delete_record", "need": "", "spec": ""
        }
        repo.tasks["q9"] = question
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)

        async def _boom(payload: Any) -> dict[str, Any]:
            raise AssertionError("denied tool must not create")

        service.create_task = _boom  # type: ignore[method-assign]
        result = await service.apply_owner_decision(
            "q9", approve=False, actor_id="owner", comment="no hace falta"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_deny")


class WaitingNeedsHumanReviewTests(unittest.TestCase):
    def test_owner_question_wait_pushes_pending_review(self) -> None:
        from tools.erpnext_tickets import (
            resolve_erpnext_status,
            waiting_needs_human_review,
        )

        task = _question_task("q1")
        self.assertTrue(waiting_needs_human_review(task))
        self.assertEqual(resolve_erpnext_status(task), "Pending Review")

    def test_tier_s_wait_stays_operational(self) -> None:
        from tools.erpnext_tickets import (
            resolve_erpnext_status,
            waiting_needs_human_review,
        )

        task = _svc_task("t1", waiting_reason="needs_owner: gates failed")
        self.assertFalse(waiting_needs_human_review(task))
        self.assertIsNone(resolve_erpnext_status(task))


class _TrayRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}

    async def list_tasks_needing_human(
        self, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = [
            deepcopy(t)
            for t in self.tasks.values()
            if str(t.get("status")) == "waiting"
        ]
        return rows[:limit]

    async def tasks_with_dependents(self, task_ids: list[str]) -> set[str]:
        return set()

    async def latest_blocked_runs(
        self, task_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        return {}

    async def get_horizon_event_by_source(
        self, user_id: str, source_id: str
    ) -> dict[str, Any] | None:
        return None


class PendingDecisionsOwnerQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_listed_and_answerable_tier_s_wait_absent(self) -> None:
        from horizon.service import HorizonService

        repo = _TrayRepo()
        repo.tasks["q1"] = _question_task("q1")
        repo.tasks["t1"] = _svc_task("t1", waiting_reason="needs_owner: gates")
        settings = SimpleNamespace(
            HORIZON_OWNER_USER_ID="usr_owner",
            HORIZON_TIMEZONE="America/La_Paz",
            HORIZON_CAPACITY_FRACTION=0.70,
            HORIZON_SOURCE_PATH="",
        )
        service = HorizonService(settings, repo)  # type: ignore[arg-type]
        tray = await service.pending_decisions("usr_owner")
        by_id = {i["task_id"]: i for i in tray["urgent"] + tray["grouped"]}
        self.assertIn("q1", by_id)
        self.assertTrue(by_id["q1"]["answerable"])
        self.assertNotIn("t1", by_id)


class DeskAnswerTests(unittest.IsolatedAsyncioTestCase):
    def _desk_service(self, repo: _SvcRepo):
        service = _make_service(repo)
        service._erpnext = SimpleNamespace(is_enabled=True)
        service.set_dispatcher_wake(lambda: None)
        return service

    async def test_desk_close_approves_and_resumes_origin(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-1"] = _svc_task(
            "origin-1", waiting_reason="needs_owner: cuanto?",
            params={"owner_question_id": "q1"},
        )
        repo.tasks["q1"] = _question_task("q1")
        service = self._desk_service(repo)

        result = await service.apply_business_status(
            task_id="q1", business_status="Completed"
        )
        self.assertTrue(result["ok"])
        question = await repo.get_task("q1")
        assert question is not None
        self.assertEqual(question["status"], "completed")
        block = (question.get("params") or {}).get("owner_question") or {}
        self.assertEqual(
            (block.get("decision") or {}).get("decision"), "approved"
        )
        self.assertEqual(
            (block.get("decision") or {}).get("by"), "erpnext-desk"
        )
        mirror = ((question.get("params") or {}).get("_mirrors") or {}).get(
            "erpnext") or {}
        self.assertEqual(mirror.get("status_pushed"), "completed")
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "queued")

    async def test_desk_cancel_denies_and_fails_origin(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-1"] = _svc_task(
            "origin-1", waiting_reason="needs_owner: cuanto?",
            params={"owner_question_id": "q1"},
        )
        repo.tasks["q1"] = _question_task("q1")
        service = self._desk_service(repo)

        result = await service.apply_business_status(
            task_id="q1", business_status="Cancelled"
        )
        self.assertTrue(result["ok"])
        question = await repo.get_task("q1")
        assert question is not None
        block = (question.get("params") or {}).get("owner_question") or {}
        self.assertEqual(
            (block.get("decision") or {}).get("decision"), "denied"
        )
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "failed")

    async def test_ordinary_wait_untouched_by_desk_close(self) -> None:
        repo = _SvcRepo()
        repo.tasks["t1"] = _svc_task("t1", waiting_reason="needs_owner: gates")
        service = self._desk_service(repo)

        result = await service.apply_business_status(
            task_id="t1", business_status="Completed"
        )
        self.assertTrue(result["ok"])
        task = await repo.get_task("t1")
        assert task is not None
        self.assertEqual(task["status"], "completed")
        self.assertNotIn("owner_answer", task.get("params") or {})


class TierSAskOwnerTests(unittest.IsolatedAsyncioTestCase):
    def _cycle(self, create_ticket=None):
        from agents.dev.tier_s.cycle import TierSCycle
        from agents.dev.tier_s.stores import (
            InMemoryLockStore,
            InMemoryRunStore,
            InMemoryTaskStore,
        )

        runs = InMemoryRunStore()
        tasks = InMemoryTaskStore()
        notices: list[tuple[str, str]] = []

        async def notify(ref: str, text: str) -> None:
            notices.append((ref, text))

        cycle = TierSCycle(
            runs,
            InMemoryLockStore(),
            tasks,
            MagicMock(),
            notify=notify,
            create_ticket=create_ticket,
        )
        return cycle, runs, tasks, notices

    def _ctx(self, tmp: str):
        from agents.dev.tier_s.cycle import _Ctx

        return _Ctx(
            ticket={"task_id": "t1", "project": "jardal-openceo",
                    "title": "Trabajo S", "params": {}},
            repo=Path(tmp),
            ref="T-1",
            run_id="run-1",
            branch="tier-s/t1-abc123",
            base_branch="main",
            budgets={},
            timeouts={},
            waiting_cfg={},
            auto_merge=False,
            budget_exclude=[],
            gate_specs=[],
            roles={},
        )

    async def test_ask_owner_parks_and_creates_owner_ticket(self) -> None:
        seen: list[dict[str, Any]] = []

        async def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": "q1", "params": payload.get("params")}

        cycle, runs, tasks, _notices = self._cycle(create_ticket=fake_create)
        await runs.create_run({"run_id": "run-1", "task_id": "t1",
                               "status": "running"})
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            outcome = await cycle.ask_owner(
                ctx,
                question="Apruebas el gasto de 50 USD?",
                human_action="Aprobar o rechazar el gasto",
                checkpoint={"phase": "review"},
            )
        self.assertEqual(outcome.status, "waiting")
        self.assertEqual(outcome.reason, "needs_owner")
        origin_patch = tasks.patches.get("t1") or {}
        self.assertEqual(origin_patch.get("status"), "waiting")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["title"].startswith("[Pregunta]"))
        block = (seen[0].get("params") or {}).get("owner_question") or {}
        self.assertEqual(block.get("origin_task_id"), "t1")
        self.assertEqual(block.get("checkpoint"), {"phase": "review"})
        self.assertFalse(seen[0].get("autorun"))
        question_patch = tasks.patches.get("q1") or {}
        self.assertEqual(question_patch.get("phase"), OWNER_QUESTION_PHASE)
        self.assertEqual(
            (origin_patch.get("params") or {}).get("owner_question_id"), "q1"
        )

    async def test_ask_owner_without_hook_parks_only(self) -> None:
        cycle, runs, tasks, _notices = self._cycle(create_ticket=None)
        await runs.create_run({"run_id": "run-1", "task_id": "t1",
                               "status": "running"})
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self._ctx(tmp)
            outcome = await cycle.ask_owner(
                ctx,
                question="Apruebas?",
                human_action="Aprobar o rechazar",
            )
        self.assertEqual(outcome.status, "waiting")
        self.assertNotIn("q1", tasks.patches)


class PlannerProposedToolTests(unittest.TestCase):
    def _result(self, **overrides: Any):
        from objectives.planner_models import ObjectiveStatus, PlannerResult

        fields: dict[str, Any] = {
            "objective_status": ObjectiveStatus.ACTIVE,
            "state_summary": "s",
            "tickets": [{
                "local_ref": "t1", "title": "Do work", "target_agent": "dev",
                "work_type": "general", "goal": "g", "why_now": "now",
                "acceptance_criteria": ["done"],
                "expected_artifacts": ["report"],
                "coding_tool": "tier_c", "tier_reason": "simple",
            }],
        }
        fields.update(overrides)
        return PlannerResult.model_validate(fields)

    def test_valid_proposal_parses(self) -> None:
        result = self._result(proposed_tools=[{
            "name": "dns.delete_record",
            "need": "Borrar registros queda pendiente de aprobacion",
            "spec": "Candado: borrar escala al Owner.",
            "origin_task_id": "task-1",
        }])
        self.assertEqual(result.proposed_tools[0].name, "dns.delete_record")

    def test_blank_name_rejected(self) -> None:
        from objectives.planner_models import PlannerResult
        import pydantic

        with self.assertRaises(pydantic.ValidationError):
            PlannerResult.model_validate({
                "objective_status": "active", "state_summary": "s",
                "tickets": [],
                "proposed_tools": [{"name": "  ", "need": "x"}],
            })

    def test_more_than_two_rejected(self) -> None:
        from objectives.planner_models import PlannerResult
        import pydantic

        with self.assertRaises(pydantic.ValidationError):
            PlannerResult.model_validate({
                "objective_status": "active", "state_summary": "s",
                "tickets": [],
                "proposed_tools": [
                    {"name": f"t{i}", "need": "x"} for i in range(3)
                ],
            })

    def test_duplicate_names_rejected_by_deterministic_validation(self) -> None:
        import asyncio

        from objectives import planner_models as planner_mod

        result = self._result(proposed_tools=[
            {"name": "dns.delete_record", "need": "x"},
            {"name": "DNS.DELETE_RECORD", "need": "y"},
        ])

        async def _exists(task_id: str) -> bool:
            return False

        errors = asyncio.run(planner_mod.validate_planner_result(
            result, agents={"dev": {"enabled": True}}, open_titles=[],
            task_exists=_exists))
        self.assertTrue(
            [e for e in errors if "proposed_tools" in e], errors
        )

    def test_prompt_documents_proposed_tools(self) -> None:
        from objectives.engine import _planner_prompt

        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"goal": "g"}},
            "", [], "INBOX\n(none)\n", [],
        )
        self.assertIn("proposed_tools", prompt)
        self.assertIn("Owner", prompt)

    def test_hook_creates_one_question_per_proposal(self) -> None:
        import asyncio

        from objectives.engine import create_tool_proposal_questions

        created: list[dict[str, Any]] = []
        patched: list[dict[str, Any]] = []

        async def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            created.append(payload)
            return {"task_id": f"q-{len(created)}",
                    "params": payload.get("params")}

        async def fake_patch(task_id: str, updates: dict[str, Any]) -> None:
            patched.append({"task_id": task_id, "updates": updates})

        env = SimpleNamespace(create_ticket=fake_create, patch_task=fake_patch)
        result = self._result(proposed_tools=[
            {"name": "dns.delete_record", "need": "Borrar queda pendiente",
             "origin_task_id": "task-1"},
        ])
        task_ids = asyncio.run(create_tool_proposal_questions(
            env, "OBJ-1", "jardal-openceo", result))
        self.assertEqual(task_ids, ["q-1"])
        self.assertEqual(
            created[0]["source_id"],
            "tool-proposal:OBJ-1:dns.delete_record",
        )
        block = (created[0].get("params") or {}).get("owner_question") or {}
        self.assertEqual(block.get("kind"), "new_tool")
        self.assertEqual(block.get("origin_task_id"), "task-1")
        self.assertIn("Borrar queda pendiente", created[0]["description"])
        self.assertEqual(patched[0]["task_id"], "q-1")
        self.assertEqual(
            patched[0]["updates"]["waiting_reason"], "owner_question:new_tool"
        )

    def test_hook_skips_answered_proposal(self) -> None:
        import asyncio

        from objectives.engine import create_tool_proposal_questions

        async def already_answered(payload: dict[str, Any]) -> dict[str, Any]:
            params = dict(payload.get("params") or {})
            block = dict(params.get("owner_question") or {})
            block["decision"] = {"decision": "approved"}
            params["owner_question"] = block
            return {"task_id": "q-old", "params": params}

        patched: list[dict[str, Any]] = []

        async def fake_patch(task_id: str, updates: dict[str, Any]) -> None:
            patched.append({"task_id": task_id})

        env = SimpleNamespace(
            create_ticket=already_answered, patch_task=fake_patch
        )
        result = self._result(proposed_tools=[
            {"name": "dns.delete_record", "need": "x"},
        ])
        task_ids = asyncio.run(create_tool_proposal_questions(
            env, "OBJ-1", "jardal-openceo", result))
        self.assertEqual(task_ids, [])
        self.assertEqual(patched, [])


if __name__ == "__main__":
    unittest.main()
