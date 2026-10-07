"""Horizon cockpit F5 (TASK-2026-00120): plan + empresa + decisiones.

Contratos y reglas probadas:
1. "Que sigue" devuelve plan personal + status de empresa + decisiones.
2. La bandeja usa la misma fuente que Requiere humano (waiting + QA), sin
   esperas operativas ni alcance personal.
3. Urgencia determinista (critica, plazo de costo, bloqueo); lo agrupado se
   entrega una sola vez y lo urgente reaparece hasta responderse.
4. Responder aplica la ruta (resume YAML, fase humana, cierre de escalado) y
   el cierre viaja a ERPNext como Completed.
5. Privacidad en un solo sentido: empresa entra en solo lectura, nada
   personal sale (filtros, respuestas y marcas de entrega).
6. Clasificador: brief/bandeja/respuesta sin robar frases dev ni compromisos.
"""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for path in (CORE_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from horizon import classify, cockpit, privacy  # noqa: E402
from horizon.service import HorizonService  # noqa: E402
from horizon_support import FakeHorizonRepo  # noqa: E402
from settings import Settings  # noqa: E402
from state import TaskReviewAction, WorkflowRunStatus  # noqa: E402
from task_service import TaskService  # noqa: E402
from tools.erpnext_tickets import resolve_erpnext_status  # noqa: E402

OWNER = "usr_owner"


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "HORIZON_OWNER_USER_ID": OWNER,
        "HORIZON_TIMEZONE": "America/La_Paz",
        "HORIZON_CAPACITY_FRACTION": 0.70,
        "HORIZON_SOURCE_PATH": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _task(task_id: str, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "title": f"Ticket {task_id}",
        "description": f"Cuerpo de {task_id}",
        "project": "jardal-openceo",
        "status": "waiting",
        "priority": "medium",
        "ticket_number": None,
        "updated_at": datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _escalation_task(task_id: str, category: str = "gasto_nuevo", **overrides: Any) -> dict[str, Any]:
    params = {
        "escalation": {
            "category": category,
            "reason": "motivo",
            "human_action": "Autoriza el gasto del dominio",
            "ceo_action_kind": "crear-proyecto",
        }
    }
    extra = overrides.pop("params", None)
    if isinstance(extra, dict):
        merged_escalation = dict(params["escalation"])
        merged_escalation.update(extra.get("escalation") or {})
        params = {**extra, "escalation": merged_escalation}
    doc = _task(
        task_id,
        phase="owner_escalation",
        waiting_reason=f"owner_escalation:{category}",
        workflow_phases=[],
        params=params,
        **overrides,
    )
    return doc


class CockpitRepo(FakeHorizonRepo):
    """FakeHorizonRepo mas las lecturas de empresa que usa el cockpit."""

    def __init__(self) -> None:
        super().__init__()
        self.company_tasks: dict[str, dict[str, Any]] = {}
        self.company_objectives: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.fail_on: set[str] = set()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"boom:{name}")

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.company_tasks.get(task_id)
        return deepcopy(row) if row else None

    async def get_task_by_ticket_number(
        self, ticket_number: int, project: str | None = None
    ) -> dict[str, Any] | None:
        del project
        for row in self.company_tasks.values():
            if row.get("ticket_number") == ticket_number:
                return deepcopy(row)
        return None

    async def list_tasks_needing_human(self, limit: int = 50) -> list[dict[str, Any]]:
        self._maybe_fail("list_tasks_needing_human")
        rows = [
            deepcopy(t)
            for t in self.company_tasks.values()
            if not privacy.is_personal_scope(t)
            and (
                str(t.get("status")) == "waiting"
                or str(t.get("qa_review_state") or "") in ("pending", "changes_requested")
            )
        ]
        rows.sort(key=lambda t: str(t.get("updated_at") or ""), reverse=True)
        return rows[:limit]

    async def list_recently_closed_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        self._maybe_fail("list_recently_closed_tasks")
        rows = [
            deepcopy(t)
            for t in self.company_tasks.values()
            if not privacy.is_personal_scope(t)
            and str(t.get("status")) == "completed"
            and t.get("completed_at") is not None
        ]
        rows.sort(key=lambda t: str(t.get("completed_at") or ""), reverse=True)
        return rows[:limit]

    async def list_blocked_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        self._maybe_fail("list_blocked_tasks")
        rows = [
            deepcopy(t)
            for t in self.company_tasks.values()
            if not privacy.is_personal_scope(t)
            and str(t.get("status")) in ("failed", "escalated")
        ]
        rows.sort(key=lambda t: str(t.get("updated_at") or ""), reverse=True)
        return rows[:limit]

    async def list_active_objectives(self, project: str | None = None) -> list[dict[str, Any]]:
        self._maybe_fail("list_active_objectives")
        return [
            deepcopy(o)
            for o in self.company_objectives
            if str(o.get("status")) == "active" and (not project or o.get("project") == project)
        ]

    async def list_tasks_by_objective(
        self, objective_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return [
            deepcopy(t)
            for t in self.company_tasks.values()
            if str(t.get("objective_id") or "") == objective_id
        ][:limit]

    async def tasks_with_dependents(self, task_ids: list[str]) -> set[str]:
        wanted = {str(t) for t in task_ids}
        found: set[str] = set()
        for row in self.company_tasks.values():
            if str(row.get("status")) not in ("waiting", "queued"):
                continue
            for blocker in row.get("blocked_by") or []:
                if str(blocker) in wanted:
                    found.add(str(blocker))
        return found

    async def latest_blocked_runs(self, task_ids: list[str]) -> dict[str, dict[str, Any]]:
        wanted = {str(t) for t in task_ids}
        latest: dict[str, dict[str, Any]] = {}
        for run in self.runs.values():
            key = str(run.get("task_id") or "")
            if key in wanted and str(run.get("status") or "") == WorkflowRunStatus.BLOCKED_HUMAN.value:
                latest[key] = deepcopy(run)
        return latest


class FakeCompanyReader:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        self.rows = list(rows or [])
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def search_tickets(
        self,
        *,
        query: str | None = None,
        project_key: str | None = None,
        status: Any = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.calls.append({"status": status, "limit": limit})
        if self.fail:
            raise RuntimeError("erp caido")
        return deepcopy(self.rows)


class FakeOwnerTaskService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None

    async def apply_owner_decision(
        self, task_id: str, *, approve: bool, actor_id: str, comment: str = ""
    ) -> dict[str, Any]:
        self.calls.append(
            {"task_id": task_id, "approve": approve, "actor_id": actor_id, "comment": comment}
        )
        if self.result is not None:
            return deepcopy(self.result)
        return {"applied": True, "route": "stub", "task": {"task_id": task_id, "title": "X"}}


class CockpitBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = CockpitRepo()
        self.task_service = FakeOwnerTaskService()
        self.reader = FakeCompanyReader()
        self.service = HorizonService(_settings(), self.repo, task_service=self.task_service)
        self.service.set_company_reader(self.reader)
        await self.service.import_context(OWNER)


class CockpitPureTests(unittest.TestCase):
    def test_decision_kind_mirrors_human_inbox(self) -> None:
        self.assertEqual(cockpit.decision_kind(_task("a", qa_review_state="pending")), "qa")
        self.assertEqual(
            cockpit.decision_kind(_task("a", qa_review_state="changes_requested")), "qa"
        )
        self.assertEqual(cockpit.decision_kind(_task("a", phase="qa_review")), "qa")
        self.assertEqual(cockpit.decision_kind(_task("a", phase="client_signoff")), "cliente")
        self.assertEqual(cockpit.decision_kind(_task("a", phase="pm_approval")), "aprobacion")
        self.assertEqual(
            cockpit.decision_kind(
                _escalation_task("a", approval_state="pending", qa_review_state="pending")
            ),
            "owner_escalation",
        )

    def test_decision_question_prefers_human_action(self) -> None:
        item = _escalation_task("a", last_response_summary="resumen largo")
        self.assertEqual(cockpit.decision_question(item), "Autoriza el gasto del dominio")
        plain = _task("b", last_response_summary="  resumen  con   espacios ")
        self.assertEqual(cockpit.decision_question(plain), "resumen con espacios")
        titled = _task("c", title="Titulo")
        titled.pop("last_response_summary", None)
        self.assertEqual(cockpit.decision_question(titled), "Titulo")
        long = _task("d", last_response_summary="x" * 500)
        self.assertLessEqual(len(cockpit.decision_question(long)), 300)

    def test_erp_mirror_name(self) -> None:
        with_mirror = _task(
            "a", params={"_mirrors": {"erpnext": {"name": "TASK-2026-00170"}}}
        )
        self.assertEqual(cockpit.erp_mirror_name(with_mirror), "TASK-2026-00170")
        self.assertIsNone(cockpit.erp_mirror_name(_task("b")))
        self.assertIsNone(cockpit.erp_mirror_name(None))

    def test_urgency_rules(self) -> None:
        now = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
        critical = _task("a", priority="critical", waiting_reason="phase:qa_review")
        self.assertEqual(cockpit.urgency_reason(critical, now=now), "prioridad_critica")
        blocked = _task("b", waiting_reason="phase:qa_review")
        self.assertEqual(
            cockpit.urgency_reason(blocked, now=now, has_dependents=True), "bloqueo"
        )
        due_soon = (now + timedelta(hours=24)).strftime("%Y-%m-%d")
        cost = _escalation_task("c", params={"due_date": due_soon, "escalation": {"category": "gasto_nuevo", "human_action": "x"}})
        self.assertEqual(cockpit.urgency_reason(cost, now=now), "plazo_de_costo")
        due_far = (now + timedelta(days=30)).strftime("%Y-%m-%d")
        cost_far = _escalation_task(
            "d", params={"due_date": due_far, "escalation": {"category": "gasto_nuevo", "human_action": "x"}}
        )
        self.assertIsNone(cockpit.urgency_reason(cost_far, now=now))
        no_due = _escalation_task("e")
        self.assertIsNone(cockpit.urgency_reason(no_due, now=now))
        bad_due = _escalation_task(
            "f", params={"due_date": "manana", "escalation": {"category": "gasto_nuevo"}}
        )
        self.assertIsNone(cockpit.urgency_reason(bad_due, now=now))
        other_category = _escalation_task(
            "g",
            category="credencial",
            params={"due_date": due_soon, "escalation": {"category": "credencial"}},
        )
        self.assertIsNone(cockpit.urgency_reason(other_category, now=now))
        plain = _task("h", waiting_reason="phase:qa_review")
        self.assertIsNone(cockpit.urgency_reason(plain, now=now))

    def test_describe_company_never_invents(self) -> None:
        message = cockpit.describe_company(
            {
                "objectives": [],
                "recently_closed": [],
                "blocked": [],
                "business_open_without_execution": [],
                "business_unknown": True,
                "objectives_unknown": False,
            }
        )
        self.assertIn("No tenemos objetivos", message)
        self.assertIn("No pudimos leer el registro de negocio", message)
        self.assertNotIn("0 ticket", message)

    def test_describe_decisions(self) -> None:
        self.assertIn("No hay decisiones", cockpit.describe_decisions([], []))
        urgent = [{"title": "Gasto dominio", "question": "Autoriza el gasto"}]
        single = cockpit.describe_decisions(urgent, [])
        self.assertIn("Urgente", single)
        self.assertIn("Autoriza el gasto", single)
        multi = cockpit.describe_decisions(
            [{"title": "A", "question": "q"}, {"title": "B", "question": "q"}], []
        )
        self.assertIn("2 decisiones urgentes", multi)
        grouped = cockpit.describe_decisions([], [{"title": "QA x"}])
        self.assertIn("una decision agrupada", grouped)

    def test_describe_decision_list(self) -> None:
        self.assertIn(
            "No hay decisiones", cockpit.describe_decision_list([])
        )
        listing = cockpit.describe_decision_list(
            [
                {"ordinal": 1, "title": "Primera"},
                {"ordinal": 2, "title": "Segunda"},
            ]
        )
        self.assertIn("1) Primera", listing)
        self.assertIn("2) Segunda", listing)


class CompanyStatusTests(CockpitBase):
    async def test_objectives_rollup_counts_children(self) -> None:
        self.repo.company_objectives.append(
            {
                "objective_id": "OBJ-1",
                "project": "jardal-openceo",
                "entry": "top_down",
                "status": "active",
                "anchor": {"goal": "Vender mas"},
                "erp_group_ref": "TASK-2026-00900",
            }
        )
        self.repo.company_tasks["t1"] = _task("t1", status="in_progress", objective_id="OBJ-1")
        self.repo.company_tasks["t2"] = _task("t2", status="failed", objective_id="OBJ-1")
        self.repo.company_tasks["t3"] = _task(
            "t3",
            status="completed",
            objective_id="OBJ-1",
            completed_at=datetime(2026, 10, 6, tzinfo=timezone.utc),
        )
        self.repo.company_tasks["t4"] = _task("t4", status="queued")

        company = await self.service.company_status()

        self.assertFalse(company["objectives_unknown"])
        self.assertEqual(len(company["objectives"]), 1)
        objective = company["objectives"][0]
        self.assertEqual(objective["goal"], "Vender mas")
        self.assertEqual(objective["open_children"], 1)
        self.assertEqual(objective["blocked_children"], 1)
        self.assertEqual(objective["closed_children"], 1)
        self.assertEqual(
            [t["task_id"] for t in company["recently_closed"]], ["t3"]
        )
        self.assertEqual([t["task_id"] for t in company["blocked"]], ["t2"])
        self.assertIn("Vender mas", company["message"])

    async def test_business_lists_only_desk_created_tickets(self) -> None:
        self.reader.rows = [
            {"name": "TASK-2026-00001", "subject": "Con ejecucion", "status": "Working",
             "project": "PROJ-0001", "openceo_ref": "api:x"},
            {"name": "TASK-2026-00002", "subject": "Creado en desk", "status": "Open",
             "project": "PROJ-0001", "openceo_ref": ""},
            {"name": "TASK-2026-00003", "subject": "Sin ref", "status": "Open",
             "project": "PROJ-0001"},
        ]
        company = await self.service.company_status()

        self.assertFalse(company["business_unknown"])
        names = [t["erp_name"] for t in company["business_open_without_execution"]]
        self.assertEqual(names, ["TASK-2026-00002", "TASK-2026-00003"])
        self.assertEqual(len(self.reader.calls), 1)
        self.assertEqual(
            self.reader.calls[0]["status"], ["Open", "Working", "Pending Review"]
        )

    async def test_without_reader_business_is_unknown_not_zero(self) -> None:
        self.service.set_company_reader(None)
        company = await self.service.company_status()
        self.assertTrue(company["business_unknown"])
        self.assertEqual(company["business_open_without_execution"], [])

    async def test_each_source_degrades_on_its_own(self) -> None:
        self.repo.fail_on.add("list_recently_closed_tasks")
        self.reader.fail = True
        self.repo.company_objectives.append(
            {"objective_id": "OBJ-1", "project": "p", "status": "active", "anchor": {}}
        )
        company = await self.service.company_status()
        self.assertTrue(company["closed_unknown"])
        self.assertTrue(company["business_unknown"])
        self.assertFalse(company["objectives_unknown"])
        self.assertFalse(company["blocked_unknown"])
        self.assertEqual(len(company["objectives"]), 1)


class PendingDecisionsTests(CockpitBase):
    def _seed(self) -> None:
        human = _task("human", waiting_reason="phase:qa_review",
                      approval_required=True, approval_state="pending")
        operational = _task("operativa", waiting_reason="quota_reserved")
        qa = _task("qa", status="in_progress", qa_review_state="pending")
        personal = _task(
            "personal",
            waiting_reason="phase:qa_review",
            params=privacy.personal_scope_params(OWNER),
        )
        for row in (human, operational, qa, personal):
            self.repo.company_tasks[row["task_id"]] = row

    async def test_same_source_as_human_inbox_without_operational_or_personal(self) -> None:
        self._seed()
        tray = await self.service.pending_decisions(OWNER)
        ids = [i["task_id"] for i in tray["grouped"]] + [i["task_id"] for i in tray["urgent"]]
        self.assertIn("human", ids)
        self.assertIn("qa", ids)
        self.assertNotIn("operativa", ids)
        self.assertNotIn("personal", ids)
        self.assertEqual(tray["count"], 2)

    async def test_urgent_first_with_global_ordinals(self) -> None:
        self.repo.company_tasks["g1"] = _task("g1", waiting_reason="phase:qa_review")
        self.repo.company_tasks["u1"] = _task(
            "u1", waiting_reason="phase:qa_review", priority="critical"
        )
        tray = await self.service.pending_decisions(OWNER)
        self.assertEqual([i["task_id"] for i in tray["urgent"]], ["u1"])
        self.assertEqual(tray["urgent"][0]["urgency"], "prioridad_critica")
        self.assertEqual(tray["urgent"][0]["ordinal"], 1)
        self.assertEqual(tray["grouped"][0]["ordinal"], 2)
        self.assertIn("Urgente", tray["message"])

    async def test_blocked_run_and_human_phase_are_answerable(self) -> None:
        self.repo.company_tasks["run"] = _task("run", waiting_reason="phase:owner_escalation")
        self.repo.runs["r1"] = {
            "run_id": "r1",
            "task_id": "run",
            "status": WorkflowRunStatus.BLOCKED_HUMAN.value,
        }
        self.repo.company_tasks["phase"] = _task(
            "phase",
            waiting_reason="phase:qa_review",
            phase="qa_review",
            phase_index=0,
            workflow_phases=[{"key": "qa_review", "type": "human"}],
        )
        self.repo.company_tasks["esc"] = _escalation_task("esc")
        self.repo.company_tasks["plain"] = _task(
            "plain", status="in_progress", qa_review_state="pending"
        )
        tray = await self.service.pending_decisions(OWNER)
        by_id = {i["task_id"]: i for i in tray["urgent"] + tray["grouped"]}
        self.assertTrue(by_id["run"]["answerable"])
        self.assertTrue(by_id["phase"]["answerable"])
        self.assertTrue(by_id["esc"]["answerable"])
        self.assertFalse(by_id["plain"]["answerable"])

    async def test_agent_phase_is_not_answerable(self) -> None:
        self.repo.company_tasks["agent"] = _task(
            "agent",
            waiting_reason="phase:build",
            phase="build",
            phase_index=0,
            workflow_phases=[{"key": "build", "type": "agent"}],
        )
        tray = await self.service.pending_decisions(OWNER)
        by_id = {i["task_id"]: i for i in tray["urgent"] + tray["grouped"]}
        self.assertFalse(by_id["agent"]["answerable"])


class DeliveredOnceTests(CockpitBase):
    async def test_grouped_is_delivered_once_urgent_reappears(self) -> None:
        self.repo.company_tasks["g1"] = _task("g1", waiting_reason="phase:qa_review")
        self.repo.company_tasks["u1"] = _task(
            "u1", waiting_reason="phase:qa_review", priority="critical"
        )
        first = await self.service.pending_decisions(OWNER)
        self.assertEqual(first["undelivered_grouped_count"], 1)
        marked = await self.service.mark_decisions_delivered(
            OWNER, first["grouped"] + first["urgent"], channel="chat"
        )
        self.assertEqual(marked["marked"], 2)

        second = await self.service.pending_decisions(OWNER)
        self.assertEqual(second["undelivered_grouped_count"], 0)
        self.assertTrue(second["grouped"][0]["delivered"])
        # Lo agrupado ya no se repite; lo urgente sigue en el mensaje.
        self.assertNotIn("g1", second["message"])
        self.assertIn("Urgente", second["message"])

        again = await self.service.mark_decisions_delivered(
            OWNER, first["grouped"], channel="chat"
        )
        self.assertEqual(again["marked"], 0)

    async def test_delivery_mark_stores_reference_only(self) -> None:
        self.repo.company_tasks["g1"] = _task(
            "g1", waiting_reason="phase:qa_review", description="Cuerpo confidencial"
        )
        tray = await self.service.pending_decisions(OWNER)
        await self.service.mark_decisions_delivered(OWNER, tray["grouped"])
        stored = await self.repo.get_horizon_event_by_source(
            OWNER, cockpit.delivery_source_id("g1")
        )
        assert stored is not None
        self.assertEqual(stored["entity"], "decision")
        self.assertEqual(stored["entity_id"], "g1")
        self.assertNotIn("description", stored)
        self.assertNotIn("Cuerpo confidencial", stored.get("text") or "")


class AnswerDecisionTests(CockpitBase):
    async def test_answer_by_task_id_applies_and_marks_delivered(self) -> None:
        self.task_service.result = {
            "applied": True,
            "route": "escalation_close",
            "task": {"task_id": "esc", "title": "Gasto dominio"},
        }
        result = await self.service.answer_decision(
            OWNER, task_id="esc", approve=True, comment="ok", channel="chat"
        )
        self.assertTrue(result["applied"])
        self.assertIn("aprobé", result["message"])
        call = self.task_service.calls[0]
        self.assertEqual(call["task_id"], "esc")
        self.assertTrue(call["approve"])
        self.assertEqual(call["actor_id"], OWNER)
        self.assertEqual(call["comment"], "ok")
        stored = await self.repo.get_horizon_event_by_source(
            OWNER, cockpit.delivery_source_id("esc")
        )
        self.assertIsNotNone(stored)

    async def test_answer_by_ordinal_follows_tray_order(self) -> None:
        self.repo.company_tasks["g1"] = _task("g1", waiting_reason="phase:qa_review")
        self.repo.company_tasks["u1"] = _task(
            "u1", waiting_reason="phase:qa_review", priority="critical"
        )
        result = await self.service.answer_decision(OWNER, ordinal=1, approve=False)
        self.assertTrue(result["applied"])
        self.assertEqual(self.task_service.calls[0]["task_id"], "u1")
        self.assertFalse(self.task_service.calls[0]["approve"])
        missing = await self.service.answer_decision(OWNER, ordinal=9, approve=True)
        self.assertFalse(missing["applied"])
        self.assertEqual(missing["reason"], "ordinal_not_found")

    async def test_answer_by_ticket_number(self) -> None:
        self.repo.company_tasks["t"] = _task(
            "t", waiting_reason="phase:qa_review", ticket_number=170
        )
        result = await self.service.answer_decision(OWNER, ticket_number=170, approve=True)
        self.assertTrue(result["applied"])
        self.assertEqual(self.task_service.calls[0]["task_id"], "t")
        missing = await self.service.answer_decision(OWNER, ticket_number=999, approve=True)
        self.assertFalse(missing["applied"])
        self.assertEqual(missing["reason"], "ticket_not_found")

    async def test_ambiguous_target_and_missing_service_rejected(self) -> None:
        out = await self.service.answer_decision(
            OWNER, task_id="a", ordinal=1, approve=True
        )
        self.assertFalse(out["applied"])
        self.assertEqual(out["reason"], "ambiguous_target")
        out = await self.service.answer_decision(OWNER, approve=True)
        self.assertEqual(out["reason"], "ambiguous_target")
        lonely = HorizonService(_settings(), self.repo)
        out = await lonely.answer_decision(OWNER, task_id="a", approve=True)
        self.assertEqual(out["reason"], "task_service_unavailable")

    async def test_rejected_answer_is_not_marked_delivered(self) -> None:
        self.task_service.result = {"applied": False, "reason": "not_answerable"}
        result = await self.service.answer_decision(OWNER, task_id="x", approve=True)
        self.assertFalse(result["applied"])
        stored = await self.repo.get_horizon_event_by_source(
            OWNER, cockpit.delivery_source_id("x")
        )
        self.assertIsNone(stored)


class _DecisionRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.reviews: list[dict[str, Any]] = []
        self.patches: list[dict[str, Any]] = []

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return deepcopy(row) if row is not None else None

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        self.patches.append({"task_id": task_id, "updates": deepcopy(updates)})
        task = self.tasks.get(task_id)
        if task is None:
            return None
        task.update(deepcopy(updates))
        return deepcopy(task)

    async def latest_workflow_run_for_task(self, task_id: str) -> dict[str, Any] | None:
        for run in self.runs.values():
            if str(run.get("task_id")) == task_id:
                return deepcopy(run)
        return None

    async def patch_workflow_run(
        self, run_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        run = self.runs.get(run_id)
        if run is None:
            return None
        run.update(deepcopy(updates))
        return deepcopy(run)

    async def create_task_review(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.reviews.append(deepcopy(doc))
        return deepcopy(doc)


class _DecisionHub:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        del payload
        self.events.append(event)


class _DecisionWorkflows:
    def __init__(self, runs: dict[str, dict[str, Any]]) -> None:
        self.runs = runs
        self.resumed: list[str] = []

    async def resume_blocked_run_for_task(
        self, task_id: str, *, action: str = "approve", actor_id: str = "admin"
    ) -> dict[str, Any] | None:
        del action, actor_id
        for run in self.runs.values():
            if str(run.get("task_id")) == task_id and str(run.get("status") or "") == "blocked_human":
                self.resumed.append(task_id)
                return {"status": "running"}
        return None


class ApplyOwnerDecisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repo = _DecisionRepo()
        self.hub = _DecisionHub()
        self.workflows = _DecisionWorkflows(self.repo.runs)
        self.service = TaskService(
            settings=Settings(),
            repo=self.repo,  # type: ignore[arg-type]
            graph=MagicMock(),
            realtime_hub=self.hub,  # type: ignore[arg-type]
            workflow_service=self.workflows,  # type: ignore[arg-type]
        )

    async def test_blocked_run_approve_resumes_and_reviews(self) -> None:
        self.repo.tasks["w"] = _task("w", waiting_reason="phase:owner_escalation")
        self.repo.runs["r"] = {
            "run_id": "r",
            "task_id": "w",
            "status": WorkflowRunStatus.BLOCKED_HUMAN.value,
            "waiting": {"permission": "owner_escalation", "prompt": "Autoriza"},
        }
        result = await self.service.apply_owner_decision(
            "w", approve=True, actor_id=OWNER, comment="adelante"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "workflow_resume")
        self.assertEqual(self.workflows.resumed, ["w"])
        self.assertEqual(len(self.repo.reviews), 1)
        self.assertEqual(self.repo.reviews[0]["action"], TaskReviewAction.APPROVE.value)
        self.assertIn("task_review_created", self.hub.events)

    async def test_blocked_run_deny_fails_run_and_task(self) -> None:
        self.repo.tasks["w"] = _task("w", waiting_reason="phase:owner_escalation")
        self.repo.runs["r"] = {
            "run_id": "r",
            "task_id": "w",
            "status": WorkflowRunStatus.BLOCKED_HUMAN.value,
        }
        result = await self.service.apply_owner_decision(
            "w", approve=False, actor_id=OWNER, comment="muy caro"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "workflow_reject")
        self.assertEqual(self.workflows.resumed, [])
        self.assertEqual(self.repo.runs["r"]["status"], "failed")
        stored = await self.repo.get_task("w")
        assert stored is not None
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["approval_state"], "rejected")
        self.assertIn("muy caro", stored.get("last_response_summary") or "")

    async def test_blocked_run_without_workflow_service_falls_through(self) -> None:
        lonely = TaskService(
            settings=Settings(),
            repo=self.repo,  # type: ignore[arg-type]
            graph=MagicMock(),
            realtime_hub=self.hub,  # type: ignore[arg-type]
        )
        self.repo.tasks["w"] = _task("w", waiting_reason="phase:owner_escalation")
        self.repo.runs["r"] = {
            "run_id": "r",
            "task_id": "w",
            "status": WorkflowRunStatus.BLOCKED_HUMAN.value,
        }
        result = await lonely.apply_owner_decision("w", approve=True, actor_id=OWNER)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "not_answerable")

    async def test_human_phase_routes_to_phase_action(self) -> None:
        self.repo.tasks["p"] = _task(
            "p",
            waiting_reason="phase:qa_review",
            phase="qa_review",
            phase_index=0,
            workflow_phases=[{"key": "qa_review", "type": "human"}],
        )
        approved = await self.service.apply_owner_decision("p", approve=True, actor_id=OWNER)
        self.assertTrue(approved["applied"])
        self.assertEqual(approved["route"], "phase_action")
        stored = await self.repo.get_task("p")
        assert stored is not None
        self.assertEqual(stored["status"], "completed")

        self.repo.tasks["q"] = _task(
            "q",
            waiting_reason="phase:qa_review",
            phase="qa_review",
            phase_index=0,
            workflow_phases=[{"key": "qa_review", "type": "human"}],
        )
        denied = await self.service.apply_owner_decision(
            "q", approve=False, actor_id=OWNER, comment="rehacer"
        )
        self.assertTrue(denied["applied"])
        self.assertEqual(denied["route"], "phase_action")
        stored = await self.repo.get_task("q")
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(self.repo.reviews[-1]["action"], TaskReviewAction.REJECT.value)

    async def test_escalation_approve_closes_and_pushes_completed(self) -> None:
        self.repo.tasks["e"] = _escalation_task("e")
        result = await self.service.apply_owner_decision(
            "e", approve=True, actor_id=OWNER, comment="ok"
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["route"], "escalation_close")
        stored = await self.repo.get_task("e")
        assert stored is not None
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["approval_state"], "approved")
        self.assertIsNone(stored.get("waiting_reason"))
        decision = (stored.get("params") or {}).get("escalation", {}).get("decision")
        self.assertEqual(decision["decision"], "approved")
        self.assertEqual(decision["by"], OWNER)
        self.assertEqual(
            resolve_erpnext_status({"status": stored["status"]}), "Completed"
        )
        self.assertNotIn("horizon_scope", stored.get("params") or {})

    async def test_escalation_deny_closes_as_denied(self) -> None:
        self.repo.tasks["e"] = _escalation_task("e")
        result = await self.service.apply_owner_decision(
            "e", approve=False, actor_id=OWNER, comment="no hay presupuesto"
        )
        self.assertTrue(result["applied"])
        stored = await self.repo.get_task("e")
        assert stored is not None
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["approval_state"], "rejected")
        decision = (stored.get("params") or {}).get("escalation", {}).get("decision")
        self.assertEqual(decision["decision"], "denied")
        self.assertIn("denegada", stored.get("last_response_summary") or "")

    async def test_personal_scope_is_rejected_without_writes(self) -> None:
        self.repo.tasks["x"] = _task(
            "x",
            waiting_reason="phase:qa_review",
            params=privacy.personal_scope_params(OWNER),
        )
        result = await self.service.apply_owner_decision("x", approve=True, actor_id=OWNER)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "personal_scope")
        self.assertEqual(self.repo.patches, [])
        self.assertEqual(self.repo.reviews, [])

    async def test_unknown_and_non_human_rejected(self) -> None:
        missing = await self.service.apply_owner_decision("nope", approve=True, actor_id=OWNER)
        self.assertEqual(missing["reason"], "task_not_found")
        self.repo.tasks["plain"] = _task("plain", status="in_progress")
        out = await self.service.apply_owner_decision("plain", approve=True, actor_id=OWNER)
        self.assertEqual(out["reason"], "not_answerable")
        self.repo.tasks["queued-esc"] = _task(
            "queued-esc", status="queued", phase="owner_escalation"
        )
        out = await self.service.apply_owner_decision("queued-esc", approve=True, actor_id=OWNER)
        self.assertEqual(out["reason"], "not_answerable")


class ClassifyCockpitTests(unittest.TestCase):
    def test_brief_phrases(self) -> None:
        for text in (
            "dame el brief del día",
            "resumen de hoy",
            "qué avanzó el CEO",
            "cómo va la empresa",
            "estado de la empresa",
            "qué hay en la empresa",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_BRIEF)

    def test_decisions_phrases(self) -> None:
        for text in (
            "qué decisiones pendientes hay",
            "qué espera mi decisión",
            "qué tengo que decidir",
            "abre la bandeja requiere humano",
            "pendientes de aprobación",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_DECISIONS)

    def test_decide_phrases(self) -> None:
        for text in (
            "aprueba la decisión",
            "rechaza el escalado",
            "aprueba la primera",
            "rechaza el ticket 170",
            "deniega la aprobación",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify.classify(text), classify.INTENT_DECIDE)

    def test_cockpit_does_not_steal(self) -> None:
        for text in (
            "revisa el ticket 49",
            "mergea la rama",
            "corre el preset review",
            "crea una sesión dev",
            "sí",
            "dale",
            "apruébalo",
            "autorizo el pago al proveedor",
            "qué sigue",
        ):
            with self.subTest(text=text):
                self.assertNotIn(
                    classify.classify(text),
                    (classify.INTENT_BRIEF, classify.INTENT_DECISIONS, classify.INTENT_DECIDE),
                )
        self.assertEqual(classify.classify("qué sigue"), classify.INTENT_NEXT_ACTION)
        self.assertEqual(
            classify.classify("necesito un resumen de mis cuentas del curso"),
            classify.INTENT_CAPTURE,
        )

    def test_parse_decide_input(self) -> None:
        parsed = classify.parse_decide_input("aprueba la primera")
        self.assertEqual(parsed, {"approve": True, "ordinal": 1, "ticket_number": None})
        parsed = classify.parse_decide_input("rechaza el ticket 170")
        self.assertEqual(parsed, {"approve": False, "ordinal": None, "ticket_number": 170})
        parsed = classify.parse_decide_input("aprueba la 2")
        self.assertEqual(parsed["ordinal"], 2)
        self.assertTrue(parsed["approve"])
        parsed = classify.parse_decide_input("aprueba o rechaza la decisión")
        self.assertIsNone(parsed["approve"])


class _CaptureCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def limit(self, _count: int) -> "_CaptureCursor":
        return self

    def __aiter__(self):  # noqa: ANN204
        async def gen():
            for row in self._rows:
                yield row

        return gen()


class _CaptureTasks:
    def __init__(self) -> None:
        self.last_query: dict[str, Any] | None = None
        self.last_sort: Any = None

    def find(self, query: dict[str, Any], sort=None) -> _CaptureCursor:
        self.last_query = deepcopy(query)
        self.last_sort = sort
        return _CaptureCursor([])


def _capture_mongo() -> tuple[Any, _CaptureTasks]:
    from db.mongo import MongoRepository

    capture = _CaptureTasks()

    class _CaptureMongo(MongoRepository):
        @property
        def tasks(self) -> _CaptureTasks:  # type: ignore[override]
            return capture

    repo = _CaptureMongo.__new__(_CaptureMongo)
    return repo, capture


class CockpitQueryPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """Los metodos reales excluyen el alcance personal en la consulta."""

    async def test_needing_human_excludes_personal(self) -> None:
        repo, capture = _capture_mongo()
        await repo.list_tasks_needing_human(limit=5)
        assert capture.last_query is not None
        self.assertEqual(
            capture.last_query["params.horizon_scope"], {"$ne": "personal"}
        )
        ors = capture.last_query["$or"]
        self.assertIn({"status": "waiting"}, ors)
        self.assertIn({"qa_review_state": "pending"}, ors)
        self.assertIn({"qa_review_state": "changes_requested"}, ors)

    async def test_recently_closed_excludes_personal(self) -> None:
        repo, capture = _capture_mongo()
        await repo.list_recently_closed_tasks(limit=5)
        assert capture.last_query is not None
        self.assertEqual(
            capture.last_query["params.horizon_scope"], {"$ne": "personal"}
        )
        self.assertEqual(capture.last_query["status"], "completed")

    async def test_blocked_excludes_personal_and_interrupted(self) -> None:
        repo, capture = _capture_mongo()
        await repo.list_blocked_tasks(limit=5)
        assert capture.last_query is not None
        self.assertEqual(
            capture.last_query["params.horizon_scope"], {"$ne": "personal"}
        )
        self.assertEqual(
            capture.last_query["status"], {"$in": ["failed", "escalated"]}
        )


class NextActionCompanyTests(CockpitBase):
    async def test_next_action_includes_company_on_demand(self) -> None:
        self.repo.company_tasks["e"] = _escalation_task("e")
        result = await self.service.next_action(OWNER, include_company=True)
        self.assertTrue(result["has_action"])
        self.assertIn("company", result)
        self.assertIn("decisions", result)
        self.assertEqual(result["decisions"]["count"], 1)
        self.assertIn("Autoriza el gasto", result["message"])
        self.assertIn("Lo siguiente es", result["message"])

    async def test_next_action_defaults_to_personal_only(self) -> None:
        self.repo.company_tasks["e"] = _escalation_task("e")
        result = await self.service.next_action(OWNER)
        self.assertNotIn("company", result)
        self.assertNotIn("decisions", result)

    async def test_cockpit_shape(self) -> None:
        self.repo.company_tasks["e"] = _escalation_task("e")
        result = await self.service.cockpit(OWNER)
        self.assertIn("personal", result)
        self.assertIn("plan", result)
        self.assertIn("company", result)
        self.assertIn("decisions", result)
        self.assertIn("Autoriza el gasto", result["message"])
        self.assertTrue(result["personal"]["has_action"])


if __name__ == "__main__":
    unittest.main()
