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

    def test_title_truncated_to_business_subject(self) -> None:
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

    async def list_tasks_blocked_by(
        self, task_id: str
    ) -> list[dict[str, Any]]:
        return []


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
    async def test_creates_business_first_and_parks_waiting(self) -> None:
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
        from tools.jardal_erp_tickets import (
            resolve_jardal_erp_status,
            waiting_needs_human_review,
        )

        task = _question_task("q1")
        self.assertTrue(waiting_needs_human_review(task))
        self.assertEqual(resolve_jardal_erp_status(task), "Pending Review")

    def test_tier_s_wait_stays_operational(self) -> None:
        from tools.jardal_erp_tickets import (
            resolve_jardal_erp_status,
            waiting_needs_human_review,
        )

        task = _svc_task("t1", waiting_reason="needs_owner: gates failed")
        self.assertFalse(waiting_needs_human_review(task))
        self.assertIsNone(resolve_jardal_erp_status(task))


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
        service._business = SimpleNamespace(is_enabled=True)
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


def _manager():
    from unittest.mock import MagicMock

    from agents.dev.session import DevSessionManager

    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "real"
    loader = MagicMock()
    loader.validate_project = MagicMock(return_value=[])
    return DevSessionManager(settings, loader, repo=None)


class OwnerAnswerPromptTests(unittest.TestCase):
    """H1: el texto del Owner llega a la ejecución reanudada."""

    def test_dev_prompt_carries_owner_answer(self) -> None:
        prompt = _manager()._build_coding_prompt(
            {"task_id": "t", "title": "t", "description": "d",
             "params": {"owner_answer": {
                 "decision": "approved", "by": "owner",
                 "comment": "adelante, 50 USD"}}},
            {},
        )
        self.assertIn("adelante, 50 USD", prompt)
        self.assertIn("dueño", prompt)

    def test_dev_prompt_without_answer_has_no_owner_section(self) -> None:
        prompt = _manager()._build_coding_prompt(
            {"task_id": "t", "title": "t", "description": "d"}, {})
        self.assertNotIn("dueño", prompt)

    def test_tier_s_planner_prompt_carries_owner_answer(self) -> None:
        from agents.dev.tier_s import cycle as cycle_mod

        ctx = SimpleNamespace(
            ticket={"title": "t", "description": "d", "acceptance": ["a1"],
                    "params": {"owner_answer": {
                        "decision": "approved", "comment": "usa el plan B"}}},
            repo=Path(tempfile.gettempdir()),
            ref="T-1",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400},
        )
        prompt = cycle_mod.TierSCycle._planner_prompt(None, ctx)
        self.assertIn("usa el plan B", prompt)
        self.assertIn("OWNER ANSWER", prompt)

    def test_tier_s_planner_prompt_without_answer_has_no_owner_section(
        self,
    ) -> None:
        from agents.dev.tier_s import cycle as cycle_mod

        ctx = SimpleNamespace(
            ticket={"title": "t", "description": "d", "acceptance": ["a1"]},
            repo=Path(tempfile.gettempdir()),
            ref="T-1",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400},
        )
        prompt = cycle_mod.TierSCycle._planner_prompt(None, ctx)
        self.assertNotIn("OWNER ANSWER", prompt)


class OwnerAnswerRetryTests(unittest.IsolatedAsyncioTestCase):
    """H3: la continuación es reintentable y honra la decisión grabada."""

    async def test_desk_close_with_dev_failure_retries_from_cockpit(
        self,
    ) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-1"] = _svc_task(
            "origin-1", waiting_reason="needs_owner: falta tool",
            params={"owner_question_id": "q1"},
        )
        question = _question_task("q1", kind="new_tool", origin="origin-1")
        question["params"]["owner_question"]["tool"] = {
            "name": "dns.delete_record", "need": "n", "spec": "s"
        }
        repo.tasks["q1"] = question
        service = _make_service(repo)
        service._business = SimpleNamespace(is_enabled=True)
        service.set_dispatcher_wake(lambda: None)
        calls: list[Any] = []

        async def _flaky(payload: Any) -> dict[str, Any]:
            calls.append(payload)
            if len(calls) == 1:
                raise RuntimeError("jardal_erp caido")
            return {"task_id": "dev-1"}

        service.create_task = _flaky  # type: ignore[method-assign]
        result = await service.apply_business_status(
            task_id="q1", business_status="Completed"
        )
        continuation = result.get("owner_answer") or {}
        self.assertFalse(continuation.get("continued"))
        stored = await repo.get_task("q1")
        assert stored is not None
        block = (stored.get("params") or {}).get("owner_question") or {}
        # La decisión del desk queda grabada aunque falle el resto.
        self.assertEqual(
            (block.get("decision") or {}).get("decision"), "approved"
        )
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "waiting")

        # Reintento desde cockpit: aunque pulse deny, manda lo grabado.
        retry = await service.apply_owner_decision(
            "q1", approve=False, actor_id="owner", comment="reintento"
        )
        self.assertTrue(retry["applied"])
        self.assertEqual(retry["route"], "owner_question_tool")
        self.assertEqual(retry.get("dev_task_id"), "dev-1")
        origin = await repo.get_task("origin-1")
        assert origin is not None
        self.assertEqual(origin["status"], "failed")

    async def test_recorded_approve_honored_on_retry(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-2"] = _svc_task(
            "origin-2", waiting_reason="needs_owner: cuanto?",
            params={"owner_question_id": "q2"},
        )
        question = _question_task(
            "q2", origin="origin-2", status="completed")
        question["params"]["owner_question"]["decision"] = {
            "decision": "approved", "by": "owner",
            "at": "2026-10-11T12:00:00+00:00", "comment": "adelante",
        }
        repo.tasks["q2"] = question
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)

        retry = await service.apply_owner_decision(
            "q2", approve=False, actor_id="owner", comment="no"
        )
        self.assertTrue(retry["applied"])
        self.assertEqual(retry["route"], "owner_question_resume")
        origin = await repo.get_task("origin-2")
        assert origin is not None
        self.assertEqual(origin["status"], "queued")

    async def test_recorded_deny_honored_on_retry(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-4"] = _svc_task(
            "origin-4", waiting_reason="needs_owner: cuanto?",
            params={"owner_question_id": "q4"},
        )
        question = _question_task(
            "q4", origin="origin-4", status="cancelled")
        question["params"]["owner_question"]["decision"] = {
            "decision": "denied", "by": "erpnext-desk",
            "at": "2026-10-11T12:00:00+00:00", "comment": "",
        }
        repo.tasks["q4"] = question
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)

        retry = await service.apply_owner_decision(
            "q4", approve=True, actor_id="owner", comment="reintento"
        )
        self.assertTrue(retry["applied"])
        self.assertEqual(retry["route"], "owner_question_deny")
        origin = await repo.get_task("origin-4")
        assert origin is not None
        self.assertEqual(origin["status"], "failed")
        # El desk-cancelled no flipa a completed en el reintento.
        stored = await repo.get_task("q4")
        assert stored is not None
        self.assertEqual(stored["status"], "cancelled")

    async def test_done_question_stays_not_answerable(self) -> None:
        repo = _SvcRepo()
        repo.tasks["origin-3"] = _svc_task("origin-3", status="completed")
        question = _question_task(
            "q3", origin="origin-3", status="completed")
        question["params"]["owner_question"]["decision"] = {
            "decision": "approved", "by": "owner",
            "at": "2026-10-11T12:00:00+00:00", "comment": "ok",
        }
        repo.tasks["q3"] = question
        service = _make_service(repo)

        result = await service.apply_owner_decision(
            "q3", approve=True, actor_id="owner", comment="otra vez"
        )
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "not_answerable")


class HookProducerHygieneTests(unittest.IsolatedAsyncioTestCase):
    """H4+H5: los productores por hook limpian fases y ponen la tag."""

    def _cycle(self, create_ticket=None):
        from unittest.mock import MagicMock

        from agents.dev.tier_s.cycle import TierSCycle
        from agents.dev.tier_s.stores import (
            InMemoryLockStore,
            InMemoryRunStore,
            InMemoryTaskStore,
        )

        runs = InMemoryRunStore()
        tasks = InMemoryTaskStore()

        async def notify(ref: str, text: str) -> None:
            del ref, text

        cycle = TierSCycle(
            runs, InMemoryLockStore(), tasks, MagicMock(),
            notify=notify, create_ticket=create_ticket,
        )
        return cycle, runs, tasks

    async def test_ask_owner_clears_phases_and_tags(self) -> None:
        from agents.dev.tier_s.cycle import _Ctx

        seen: list[dict[str, Any]] = []

        async def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": "q1", "params": payload.get("params")}

        cycle, runs, tasks = self._cycle(create_ticket=fake_create)
        await runs.create_run({"run_id": "run-1", "task_id": "t1",
                               "status": "running"})
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _Ctx(
                ticket={"task_id": "t1", "project": "jardal-openceo",
                        "title": "T", "params": {}},
                repo=Path(tmp), ref="T-1", run_id="run-1",
                branch="tier-s/t1-x", base_branch="main", budgets={},
                timeouts={}, waiting_cfg={}, auto_merge=False,
                budget_exclude=[], gate_specs=[], roles={},
            )
            await cycle.ask_owner(
                ctx, question="Apruebas?",
                human_action="Aprobar o rechazar",
            )
        self.assertIn("pregunta-dueno", seen[0].get("tags") or [])
        patch = tasks.patches.get("q1") or {}
        self.assertEqual(patch.get("workflow_phases"), [])
        self.assertIsNone(patch.get("workflow_key"))
        self.assertEqual(patch.get("phase_index"), 0)
        self.assertEqual(patch.get("phase_assignments"), {})

    async def test_tool_hook_clears_phases_and_tags(self) -> None:
        import asyncio

        from objectives.engine import create_tool_proposal_questions
        from objectives.planner_models import PlannerResult

        created: list[dict[str, Any]] = []
        patched: list[dict[str, Any]] = []

        async def fake_create(payload: dict[str, Any]) -> dict[str, Any]:
            created.append(payload)
            return {"task_id": "q-1", "params": payload.get("params")}

        async def fake_patch(task_id: str, updates: dict[str, Any]) -> None:
            patched.append({"task_id": task_id, "updates": updates})

        env = SimpleNamespace(create_ticket=fake_create, patch_task=fake_patch)
        result = PlannerResult.model_validate({
            "objective_status": "active", "state_summary": "s", "tickets": [],
            "proposed_tools": [{"name": "dns.x", "need": "borrar"}],
        })
        await asyncio.sleep(0)
        task_ids = await create_tool_proposal_questions(
            env, "OBJ-1", "jardal-openceo", result)
        self.assertEqual(task_ids, ["q-1"])
        self.assertIn("pregunta-dueno", created[0].get("tags") or [])
        updates = patched[0]["updates"]
        self.assertEqual(updates.get("workflow_phases"), [])
        self.assertIsNone(updates.get("workflow_key"))

    async def test_internal_ticket_passes_tags_through(self) -> None:
        from objectives.service import create_internal_ticket

        seen: list[Any] = []

        class _Service:
            async def create_task(self, payload: Any) -> dict[str, Any]:
                seen.append(payload)
                return {"task_id": "t9"}

        created = await create_internal_ticket(_Service(), {
            "title": "T", "description": "D", "project": "p",
            "source": "api", "source_id": "s1",
            "tags": ["pregunta-dueno", "question"],
        })
        self.assertEqual((created or {})["task_id"], "t9")
        self.assertEqual(
            seen[0].tags, ["pregunta-dueno", "question"]
        )

    async def test_legacy_phased_question_still_routes(self) -> None:
        # Tolerancia: preguntas creadas antes del fix conservan la
        # plantilla dev.default; la ruta del canal las sigue atendiendo.
        repo = _SvcRepo()
        repo.tasks["origin-5"] = _svc_task(
            "origin-5", waiting_reason="needs_owner: cuanto?",
            params={"owner_question_id": "q5"},
        )
        question = _question_task("q5", origin="origin-5")
        question["workflow_key"] = "dev.default"
        question["workflow_phases"] = [
            {"key": "dev_work", "type": "agent"},
            {"key": "qa_review", "type": "human",
             "permission": "qa_review"},
        ]
        question["phase_index"] = 0
        repo.tasks["q5"] = question
        service = _make_service(repo)
        service.set_dispatcher_wake(lambda: None)

        result = await service.apply_owner_decision(
            "q5", approve=True, actor_id="owner", comment="ok"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_resume")


class KnowledgeConfirmBlockTests(unittest.TestCase):
    """2.5 grouped block over the owner channel (TASK-2026-00332, D7)."""

    def test_empty_block_raises(self) -> None:
        from orchestration import knowledge_confirm as kc

        with self.assertRaises(ValueError):
            kc.build_confirm_question([], project="p")

    def test_source_id_stable_per_set(self) -> None:
        from orchestration import knowledge_confirm as kc

        first = [kc.PendingConfirm("o1", "a", "t/a.md", "0" * 64,
                                   Path("/tmp/a.yaml"))]
        again = [kc.PendingConfirm("o1", "a", "t/a.md", "0" * 64,
                                   Path("/tmp/a.yaml"))]
        other = [kc.PendingConfirm("o1", "a", "t/a.md", "1" * 64,
                                   Path("/tmp/a.yaml"))]
        self.assertEqual(kc.confirm_source_id("p", first),
                         kc.confirm_source_id("p", again))
        self.assertNotEqual(kc.confirm_source_id("p", first),
                            kc.confirm_source_id("p", other))

    def test_confirm_items_round_trip(self) -> None:
        from orchestration import knowledge_confirm as kc

        pending = [
            kc.PendingConfirm("o1", "a", "t/a.md", "0" * 64,
                              Path("/tmp/a.yaml")),
            kc.PendingConfirm("o2", "b", "t/b.md", "1" * 64,
                              Path("/tmp/b.yaml")),
        ]
        spec = kc.build_confirm_question(pending, project="p")
        block = spec["params"]["owner_question"]
        self.assertEqual(block["kind"], "question")
        self.assertIsNone(block["origin_task_id"])
        self.assertTrue(kc.is_confirm_question(block))
        self.assertTrue(kc.is_confirm_question(
            {"params": {"owner_question": block}}))
        self.assertFalse(kc.is_confirm_question({}))
        self.assertFalse(kc.is_confirm_question(
            {"params": {"owner_question": {"kind": "question"}}}))
        items = kc.confirm_items(block)
        self.assertEqual([(i["origin"], i["name"]) for i in items],
                         [("o1", "a"), ("o2", "b")])
        self.assertEqual(items[0]["sha256"], "0" * 64)

    def test_single_question_lists_every_item(self) -> None:
        from orchestration import knowledge_confirm as kc

        pending = [
            kc.PendingConfirm("o1", "rules", "t/r.md", "a" * 64,
                              Path("/tmp/r.yaml")),
            kc.PendingConfirm("o2", "flow", "t/f.md", "b" * 64,
                              Path("/tmp/f.yaml")),
        ]
        spec = kc.build_confirm_question(pending, project="p")
        self.assertIn("pregunta-dueno", spec["tags"])
        self.assertFalse(spec["autorun"])
        self.assertIn("o1/rules", spec["description"])
        self.assertIn("o2/flow", spec["description"])
        self.assertIn("a" * 16, spec["description"])
        self.assertIn("Aprobar", spec["description"])
        self.assertTrue(spec["source_id"].startswith("knowledge-confirm:"))

    def test_confirm_question_listed_in_owner_summary(self) -> None:
        import asyncio

        from horizon.service import HorizonService
        from orchestration import knowledge_confirm as kc
        from owner_channel import OWNER_QUESTION_PHASE

        pending = [kc.PendingConfirm("o1", "rules", "t/r.md", "a" * 64,
                                     Path("/tmp/r.yaml"))]
        spec = kc.build_confirm_question(pending, project="jardal-openceo")
        repo = _TrayRepo()
        repo.tasks["q1"] = _svc_task(
            "q1", status="waiting", phase=OWNER_QUESTION_PHASE,
            waiting_reason=f"{OWNER_QUESTION_PHASE}:question",
            params=spec["params"])
        settings = SimpleNamespace(
            HORIZON_OWNER_USER_ID="usr_owner",
            HORIZON_TIMEZONE="America/La_Paz",
            HORIZON_CAPACITY_FRACTION=0.70,
            HORIZON_SOURCE_PATH="",
        )
        service = HorizonService(settings, repo)  # type: ignore[arg-type]
        tray = asyncio.run(service.pending_decisions("usr_owner"))
        by_id = {i["task_id"]: i
                 for i in tray["urgent"] + tray["grouped"]}
        self.assertIn("q1", by_id)
        self.assertTrue(by_id["q1"]["answerable"])


CONFIRM_PROJECT = "projects/org/active"
CONFIRM_NOTE = ("---\ntype: practice\nproject: active\ndate: 2026-10-28\n---\n\n"
                "# Deploy rule\n\nDeploy only tagged releases.\n")
CONFIRM_HUB = ("---\ntype: hub\nproject: active\ndate: 2026-10-28\n---\n\n"
               "# Active\n\nProject hub.\n")
CONFIRM_CHECKER = Path(
    "/home/develop/development/Jardal/jardal-knowledge/.agents/skills"
    "/llm-vault-links/scripts/vault_check.py")


def _confirm_proposal(name: str, **overrides: object) -> dict:
    base: dict = {
        "target": f"{CONFIRM_PROJECT}/practices/{name}.md",
        "action": "create",
        "class": "normative",
        "base_version": None,
        "evidence": [{"ticket": "TASK-2026-00997"}],
        "content": CONFIRM_NOTE,
    }
    base.update(overrides)
    return base


class ConfirmApplyCase(unittest.TestCase):
    def setUp(self) -> None:
        import shutil
        import subprocess

        import yaml

        if not CONFIRM_CHECKER.is_file():
            self.skipTest(f"vault checker not present at {CONFIRM_CHECKER}")
        self._yaml = yaml
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        env = dict(__import__("os").environ, GIT_CONFIG_NOSYSTEM="1")
        self._env = env
        self.vault_repo = self._git_repo("vaultrepo")
        self.vault_dir = self.vault_repo / "vault"
        project_dir = self.vault_dir / CONFIRM_PROJECT
        project_dir.mkdir(parents=True)
        (project_dir / "active.md").write_text(CONFIRM_HUB, encoding="utf-8")
        checker_dir = (self.vault_repo / ".agents" / "skills"
                       / "llm-vault-links" / "scripts")
        checker_dir.mkdir(parents=True)
        shutil.copyfile(CONFIRM_CHECKER, checker_dir / "vault_check.py")
        self._git(self.vault_repo, "add", "--all")
        self._git(self.vault_repo, "commit", "--quiet", "-m", "Baseline")
        self.base = self.root / "work"
        self.base.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _git_repo(self, name: str) -> Path:
        import subprocess

        repo = self.root / name
        repo.mkdir()
        subprocess.run(["git", "init", "--initial-branch=main", str(repo)],
                       check=True, capture_output=True, env=self._env)
        subprocess.run(["git", "-C", str(repo), "config", "user.name",
                        "Confirm Test"], check=True, capture_output=True,
                       env=self._env)
        subprocess.run(["git", "-C", str(repo), "config", "user.email",
                        "confirm@example.invalid"], check=True,
                       capture_output=True, env=self._env)
        subprocess.run(["git", "-C", str(repo), "config", "commit.gpgsign",
                        "false"], check=True, capture_output=True,
                       env=self._env)
        return repo

    def _git(self, repo: Path, *args: str):
        import subprocess

        return subprocess.run(["git", "-C", str(repo), *args],
                              text=True, capture_output=True, check=True)

    def _config(self):
        from orchestration.vault_publisher import PublishConfig

        return PublishConfig(
            ticket_ref="TASK-2026-00332", vault_repo=self.vault_repo,
            vault_dir=self.vault_dir, project=CONFIRM_PROJECT,
            evidence_repo=self.root)

    def _queue(self, origin: str, name: str, **overrides: object) -> Path:
        from orchestration.agent_sandbox import ticket_dir

        folder = (ticket_dir(origin, base_dir=self.base)
                  / "knowledge_confirm_queue")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.yaml"
        path.write_text(self._yaml.safe_dump(_confirm_proposal(name,
                                                               **overrides)),
                        encoding="utf-8")
        return path

    async def _pending(self):
        import asyncio

        from orchestration import knowledge_confirm as kc

        return await asyncio.to_thread(
            kc.collect_pending_confirmations, self.base)

    async def _items(self):
        from orchestration import knowledge_confirm as kc

        pending = await self._pending()
        spec = kc.build_confirm_question(pending, project="p")
        return kc.confirm_items(spec["params"]["owner_question"])


class ConfirmApplyTests(ConfirmApplyCase, unittest.IsolatedAsyncioTestCase):
    async def test_approve_publishes_exactly_shown(self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        self._queue("origin-2", "rollback")
        pending = kc.collect_pending_confirmations(self.base)
        self.assertEqual(len(pending), 2)
        items = kc.confirm_items(kc.build_confirm_question(
            pending, project="p")["params"]["owner_question"])
        outcome = await kc.apply_approved_proposals(
            items, config=self._config(), base_dir=self.base)
        self.assertEqual(sorted(outcome.published),
                         ["origin-1/deploy", "origin-2/rollback"])
        self.assertEqual(outcome.skipped_changed, [])
        self.assertEqual(outcome.deferred, {})
        self.assertEqual(outcome.rejected, {})
        for name in ("deploy", "rollback"):
            note = (self.vault_dir / CONFIRM_PROJECT
                    / f"practices/{name}.md")
            self.assertEqual(note.read_text(encoding="utf-8"), CONFIRM_NOTE)

    async def test_changed_content_not_published(self) -> None:
        from orchestration import knowledge_confirm as kc
        from orchestration.agent_sandbox import ticket_dir

        self._queue("origin-1", "deploy")
        self._queue("origin-2", "rollback")
        pending = kc.collect_pending_confirmations(self.base)
        items = kc.confirm_items(kc.build_confirm_question(
            pending, project="p")["params"]["owner_question"])
        changed = (ticket_dir("origin-2", base_dir=self.base)
                   / "knowledge_confirm_queue/rollback.yaml")
        with changed.open("ab") as handle:
            handle.write(b"\n# tampered after the question\n")
        outcome = await kc.apply_approved_proposals(
            items, config=self._config(), base_dir=self.base)
        self.assertEqual(outcome.published, ["origin-1/deploy"])
        self.assertEqual(outcome.skipped_changed, ["origin-2/rollback"])
        self.assertTrue(changed.is_file())
        self.assertFalse((self.vault_dir / CONFIRM_PROJECT
                          / "practices/rollback.md").exists())
        self.assertIn("ya no vale", kc.format_confirm_comment(
            outcome, approved=True))

    async def test_deny_discards_with_reason(self) -> None:
        from orchestration import knowledge_confirm as kc
        from orchestration.agent_sandbox import ticket_dir

        self._queue("origin-1", "deploy")
        self._queue("origin-2", "rollback")
        items = await self._items()
        discarded = kc.apply_denied_proposals(items, base_dir=self.base)
        self.assertEqual(discarded,
                         {"origin-1": ["deploy"], "origin-2": ["rollback"]})
        for origin, name in (("origin-1", "deploy"),
                             ("origin-2", "rollback")):
            self.assertFalse((ticket_dir(origin, base_dir=self.base)
                              / f"knowledge_confirm_queue/{name}.yaml"
                              ).exists())

    async def test_deny_comment_names_reason(self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        items = await self._items()
        discarded = kc.apply_denied_proposals(items, base_dir=self.base)
        outcome = kc.ConfirmOutcome(
            rejected={f"{o}/{n}": "cuesta mucho"
                      for o, names in discarded.items() for n in names})
        text = kc.format_confirm_comment(outcome, approved=False,
                                         owners_reason="cuesta mucho")
        self.assertIn("denegado", text)
        self.assertIn("origin-1/deploy", text)
        self.assertIn("cuesta mucho", text)

    async def test_collect_filters_foreign_project(self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        self._queue("origin-9", "foreign",
                    target="projects/other/practices/foreign.md")
        scoped = kc.collect_pending_confirmations(
            self.base, CONFIRM_PROJECT)
        self.assertEqual([(p.origin, p.name) for p in scoped],
                         [("origin-1", "deploy")])
        unscoped = kc.collect_pending_confirmations(self.base)
        self.assertEqual(len(unscoped), 2)

    async def test_approve_keeps_foreign_proposal(self) -> None:
        from orchestration import knowledge_confirm as kc
        from orchestration.agent_sandbox import ticket_dir

        self._queue("origin-1", "deploy")
        foreign = self._queue("origin-9", "foreign",
                              target="projects/other/practices/foreign.md")
        pending = kc.collect_pending_confirmations(self.base)
        items = kc.confirm_items(kc.build_confirm_question(
            pending, project="p")["params"]["owner_question"])
        outcome = await kc.apply_approved_proposals(
            items, config=self._config(), base_dir=self.base)
        self.assertEqual(outcome.published, ["origin-1/deploy"])
        self.assertEqual(outcome.skipped_foreign, ["origin-9/foreign"])
        self.assertTrue(foreign.is_file())

    async def test_same_name_across_origins(self) -> None:
        from orchestration import knowledge_confirm as kc
        from orchestration.agent_sandbox import ticket_dir

        self._queue("origin-1", "deploy")
        self._queue("origin-2", "deploy",
                    target=f"{CONFIRM_PROJECT}/practices/deploy2.md")
        pending = kc.collect_pending_confirmations(self.base)
        items = kc.confirm_items(kc.build_confirm_question(
            pending, project="p")["params"]["owner_question"])
        outcome = await kc.apply_approved_proposals(
            items, config=self._config(), base_dir=self.base)
        self.assertEqual(sorted(outcome.published),
                         ["origin-1/deploy", "origin-2/deploy"])
        self.assertEqual(outcome.rejected, {})
        for origin in ("origin-1", "origin-2"):
            self.assertFalse((ticket_dir(origin, base_dir=self.base)
                              / "knowledge_confirm_queue/deploy.yaml"
                              ).exists())

    async def test_same_name_failure_only_rejects_its_origin(self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        self._queue("origin-2", "deploy",
                    target=f"{CONFIRM_PROJECT}/practices/deploy2.md",
                    action="update")
        pending = kc.collect_pending_confirmations(self.base)
        items = kc.confirm_items(kc.build_confirm_question(
            pending, project="p")["params"]["owner_question"])
        outcome = await kc.apply_approved_proposals(
            items, config=self._config(), base_dir=self.base)
        self.assertEqual(outcome.published, ["origin-1/deploy"])
        self.assertIn("origin-2/deploy", outcome.rejected)
        self.assertIn("missing", outcome.rejected["origin-2/deploy"])

    async def test_yml_suffix_applies(self) -> None:
        from orchestration import knowledge_confirm as kc
        from orchestration.agent_sandbox import ticket_dir

        folder = (ticket_dir("origin-1", base_dir=self.base)
                  / "knowledge_confirm_queue")
        folder.mkdir(parents=True, exist_ok=True)
        legacy = folder / "legacy.yml"
        legacy.write_text(self._yaml.safe_dump(
            _confirm_proposal("legacy")), encoding="utf-8")
        pending = kc.collect_pending_confirmations(self.base)
        self.assertEqual([(p.origin, p.name) for p in pending],
                         [("origin-1", "legacy")])
        items = kc.confirm_items(kc.build_confirm_question(
            pending, project="p")["params"]["owner_question"])
        outcome = await kc.apply_approved_proposals(
            items, config=self._config(), base_dir=self.base)
        self.assertEqual(outcome.published, ["origin-1/legacy"])
        self.assertFalse(legacy.exists())
        self.assertTrue((self.vault_dir / CONFIRM_PROJECT
                         / "practices/legacy.md").is_file())

    async def test_missing_file_reported(self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        items = await self._items()
        (self.base / "origin-1"
         / "knowledge_confirm_queue/deploy.yaml").unlink()
        outcome = await kc.apply_approved_proposals(
            items, config=self._config(), base_dir=self.base)
        self.assertEqual(outcome.published, [])
        self.assertEqual(outcome.missing, ["origin-1/deploy"])
        text = kc.format_confirm_comment(outcome, approved=True)
        self.assertIn("origin-1/deploy", text)

    def test_question_shows_excerpt(self) -> None:
        from pathlib import Path as _Path

        from orchestration import knowledge_confirm as kc

        pending = [kc.PendingConfirm(
            "o1", "rules", "t/r.md", "a" * 64, _Path("/tmp/r.yaml"),
            excerpt="Deploy only tagged releases.",
            proposal_class="normative")]
        parts = kc.confirm_question_parts(pending)
        self.assertIn("Deploy only tagged releases.", parts["question"])
        self.assertIn("[normative]", parts["question"])


class ConfirmServiceTests(ConfirmApplyCase,
                          unittest.IsolatedAsyncioTestCase):
    def _service(self, repo: _SvcRepo):
        service = _make_service(repo)
        service._graph = SimpleNamespace(_generic_wiring=SimpleNamespace(
            work_base_dir=str(self.base)))
        service._config_loader = SimpleNamespace(
            load_project=lambda name: {
                "dev": {"vault_project": {
                    "project": CONFIRM_PROJECT,
                    "dir": str(self.vault_dir)}},
                "project": {"repos": [{"local_path": str(self.root)}]},
            })
        return service

    def _confirm_task(self, task_id: str, project: str,
                      checkpoint: dict) -> dict:
        return _svc_task(
            task_id, project=project, status="waiting",
            phase="owner_question",
            waiting_reason="owner_question:question",
            params={"owner_question": dict(
                {"kind": "question",
                 "question": "bloque",
                 "human_action": "Aprobar o rechazar",
                 "origin_task_id": None},
                **{"checkpoint": checkpoint})})

    async def test_sync_creates_single_grouped_question(self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        self._queue("origin-2", "rollback")
        repo = _SvcRepo()
        service = self._service(repo)
        seen: list[Any] = []

        async def fake_create(payload: Any) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": "q1", "params": payload.params}

        service.create_task = fake_create  # type: ignore[method-assign]
        await service._sync_confirm_question("jardal-openceo")
        self.assertEqual(len(seen), 1)
        payload = seen[0]
        self.assertFalse(payload.autorun)
        block = (payload.params or {})["owner_question"]
        self.assertTrue(kc.is_confirm_question(block))
        items = kc.confirm_items(block)
        self.assertEqual(len(items), 2)
        self.assertTrue(str(payload.source_id).startswith(
            "knowledge-confirm:"))

    async def test_approve_route_publishes_and_closes(self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        pending = kc.collect_pending_confirmations(self.base)
        parts = kc.confirm_question_parts(pending)
        repo = _SvcRepo()
        repo.tasks["q1"] = self._confirm_task(
            "q1", "jardal-openceo", dict(parts["checkpoint"]))
        service = self._service(repo)
        service.set_dispatcher_wake(lambda: None)
        result = await service.apply_owner_decision(
            "q1", approve=True, actor_id="owner", comment="adelante")
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_close")
        note = (self.vault_dir / CONFIRM_PROJECT
                / "practices/deploy.md")
        self.assertEqual(note.read_text(encoding="utf-8"), CONFIRM_NOTE)
        question = await repo.get_task("q1")
        assert question is not None
        self.assertEqual(question["status"], "completed")
        block = (question.get("params") or {}).get("owner_question") or {}
        self.assertEqual(
            (block.get("decision") or {}).get("decision"), "approved")
        self.assertIn("Publicadas", result["task"]["last_response_summary"])

    async def test_deny_route_discards_with_reason(self) -> None:
        from orchestration import knowledge_confirm as kc
        from orchestration.agent_sandbox import ticket_dir

        self._queue("origin-1", "deploy")
        pending = kc.collect_pending_confirmations(self.base)
        parts = kc.confirm_question_parts(pending)
        repo = _SvcRepo()
        repo.tasks["q1"] = self._confirm_task(
            "q1", "jardal-openceo", dict(parts["checkpoint"]))
        service = self._service(repo)
        service.set_dispatcher_wake(lambda: None)
        result = await service.apply_owner_decision(
            "q1", approve=False, actor_id="owner", comment="cuesta mucho")
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_deny")
        self.assertFalse((ticket_dir("origin-1", base_dir=self.base)
                          / "knowledge_confirm_queue/deploy.yaml").exists())
        question = await repo.get_task("q1")
        assert question is not None
        block = (question.get("params") or {}).get("owner_question") or {}
        self.assertEqual(
            (block.get("decision") or {}).get("decision"), "denied")
        self.assertIn("cuesta mucho",
                      result["task"]["last_response_summary"])

    async def test_approve_without_vault_mapping_stays_open(self) -> None:
        from owner_channel import OwnerQuestionSkipped
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        pending = kc.collect_pending_confirmations(self.base)
        parts = kc.confirm_question_parts(pending)
        repo = _SvcRepo()
        repo.tasks["q1"] = self._confirm_task(
            "q1", "jardal-openceo", dict(parts["checkpoint"]))
        service = self._service(repo)
        service._config_loader = SimpleNamespace(
            load_project=lambda name: {})
        service.set_dispatcher_wake(lambda: None)
        with self.assertRaises(OwnerQuestionSkipped):
            await service.apply_owner_decision(
                "q1", approve=True, actor_id="owner", comment="adelante")
        question = await repo.get_task("q1")
        assert question is not None
        self.assertEqual(question["status"], "waiting")
        block = (question.get("params") or {}).get("owner_question") or {}
        self.assertNotIn("decision", block)

    async def test_deny_without_mapping_discards_and_comments_origin(
            self) -> None:
        from orchestration import knowledge_confirm as kc

        self._queue("origin-1", "deploy")
        pending = kc.collect_pending_confirmations(self.base)
        parts = kc.confirm_question_parts(pending)
        repo = _SvcRepo()
        repo.tasks["origin-1"] = _svc_task(
            "origin-1",
            params={"_mirrors": {"business": {"name": "TASK-2026-00997"}}})
        repo.tasks["q1"] = self._confirm_task(
            "q1", "jardal-openceo", dict(parts["checkpoint"]))
        service = self._service(repo)
        service._config_loader = SimpleNamespace(
            load_project=lambda name: {})
        calls: list[tuple[str, str]] = []

        async def fake_comment(name: str, text: str) -> None:
            calls.append((name, text))

        service._business = SimpleNamespace(  # type: ignore[attr-defined]
            is_enabled=True, add_comment=fake_comment)
        service.set_dispatcher_wake(lambda: None)
        result = await service.apply_owner_decision(
            "q1", approve=False, actor_id="owner", comment="cuesta mucho")
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "owner_question_deny")
        self.assertTrue(any(
            name == "TASK-2026-00997" and "cuesta mucho" in text
            for name, text in calls))

    async def test_sync_groups_only_own_project(self) -> None:
        from orchestration import knowledge_confirm as kc
        from orchestration.agent_sandbox import ticket_dir

        self._queue("origin-1", "deploy")
        self._queue("origin-9", "foreign",
                    target="projects/other/practices/foreign.md")
        repo = _SvcRepo()
        service = self._service(repo)
        seen: list[Any] = []

        async def fake_create(payload: Any) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": "q1", "params": payload.params}

        service.create_task = fake_create  # type: ignore[method-assign]
        await service._sync_confirm_question("jardal-openceo")
        self.assertEqual(len(seen), 1)
        items = kc.confirm_items(
            (seen[0].params or {})["owner_question"])
        self.assertEqual([(i["origin"], i["name"]) for i in items],
                         [("origin-1", "deploy")])
        self.assertTrue((ticket_dir("origin-9", base_dir=self.base)
                         / "knowledge_confirm_queue/foreign.yaml").exists())

    async def test_resync_same_set_keeps_source_id(self) -> None:
        self._queue("origin-1", "deploy")
        repo = _SvcRepo()
        service = self._service(repo)
        seen: list[Any] = []

        async def fake_create(payload: Any) -> dict[str, Any]:
            seen.append(payload)
            return {"task_id": f"q{len(seen)}", "params": payload.params}

        service.create_task = fake_create  # type: ignore[method-assign]
        await service._sync_confirm_question("jardal-openceo")
        await service._sync_confirm_question("jardal-openceo")
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0].source_id, seen[1].source_id)


if __name__ == "__main__":
    unittest.main()
