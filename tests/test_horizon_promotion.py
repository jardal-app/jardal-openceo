"""Tests de F5: promocion automatica Horizon -> empresa (TASK-2026-00121).

Contratos y reglas probadas:
1. Clasificacion determinista: sin toque declarado -> personal; toque
   conocido -> empresa; toque desconocido -> rechazo sin crear nada.
2. Frontera de privacidad: a ERPNext solo viaja la especificacion del
   trabajo (canarios personales nunca aparecen en el borde ERP real).
3. Promocion idempotente por accion y por `source_id` de la API.
4. Costo/gasto/replanificacion -> escalado al dueno; el resto, automatico.
5. Lo personal sigue privado y la delegacion HZ-003 intacta.
6. La capacidad del dueno no frena la promocion.
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

from company_mandate import EscalationCategory, parse_mandate
from horizon import privacy, promotion
from horizon.models import ActionOwner, ActionStatus
from horizon.service import HorizonService
from horizon_support import FakeHorizonRepo
from state import make_task_document

# Canarios: si alguno aparece en un payload de empresa, la frontera filtro.
CANARY_MISSION = "CANARY_MISION_VIDA_SECRETA"
CANARY_OBJECTIVE = "CANARY_OBJETIVO_PERSONAL"
CANARY_FINANCE = "CANARY_FINANZAS_12345"
CANARY_FAMILY = "CANARY_FAMILIA_GARZON"


def _settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "HORIZON_OWNER_USER_ID": "usr_owner",
        "HORIZON_TIMEZONE": "America/La_Paz",
        "HORIZON_CAPACITY_FRACTION": 0.70,
        "HORIZON_SOURCE_PATH": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _mandate_dict(threshold: str = "0.00") -> dict[str, Any]:
    return {
        "version": 1,
        "ejes": [
            {
                "id": "crecimiento",
                "meta": "crecer de forma sostenible",
                "kpis": [{"id": "mrr", "fuente": "erpnext.finance"}],
            },
        ],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": threshold,
            "cuota": "suscripciones_ya_pagadas",
        },
        "limites": {
            "objetivos_activos": 3,
            "objetivos_activos_por_proyecto": 2,
            "tickets_activos_por_objetivo": 6,
        },
        "revision_ceo": {"cadencia": "semanal"},
        "escalar_al_dueno": [
            category.value
            for category in EscalationCategory
            if category != EscalationCategory.MANDATE_UNAVAILABLE
        ],
    }


class FakeCompanyConfig:
    """Configuracion de empresa minima: proyectos, repos y mandato."""

    def __init__(
        self,
        projects: dict[str, list[dict[str, Any]]] | None = None,
        mandate_threshold: str = "0.00",
        mandate_error: bool = False,
    ) -> None:
        self.projects = (
            {"acme": [{"name": "web", "local_path": "/repos/acme-web"}]}
            if projects is None
            else projects
        )
        self.mandate_threshold = mandate_threshold
        self.mandate_error = mandate_error

    def has_project(self, project: str | None) -> bool:
        return str(project or "") in self.projects

    def available_projects(self) -> list[str]:
        return sorted(self.projects)

    def load_project(self, project: str | None) -> dict[str, Any]:
        return {"project": {"repos": list(self.projects.get(str(project or ""), []))}}

    def load_company_mandate(self) -> Any:
        if self.mandate_error:
            raise RuntimeError("mandate file missing")
        return parse_mandate(_mandate_dict(self.mandate_threshold))


class FakeTaskService:
    """TaskService falso con idempotencia por source_id, como el real."""

    def __init__(self) -> None:
        self.created_tasks: list[Any] = []
        self.by_source_id: dict[str, dict[str, Any]] = {}
        self.tasks_by_id: dict[str, dict[str, Any]] = {}
        self.escalations: list[dict[str, Any]] = []
        self.objectives: list[dict[str, Any]] = []
        self.missions: list[dict[str, Any]] = []
        self.plan_calls: list[str] = []
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:03d}"

    async def create_task(self, req: Any) -> dict[str, Any]:
        source_id = str(req.source_id or "").strip()
        if source_id and source_id in self.by_source_id:
            return dict(self.by_source_id[source_id])
        task_id = self._next_id("task")
        doc = req.model_dump() if hasattr(req, "model_dump") else dict(req)
        doc["task_id"] = task_id
        doc["status"] = "in_progress" if doc.get("autorun") else "queued"
        self.created_tasks.append(req)
        self.tasks_by_id[task_id] = doc
        if source_id:
            self.by_source_id[source_id] = doc
        return dict(doc)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        doc = self.tasks_by_id.get(task_id)
        return dict(doc) if doc is not None else None

    async def plan_objective(self, objective_id: str) -> dict[str, Any]:
        self.plan_calls.append(objective_id)
        return {"ok": True, "contracts_completed": []}

    async def create_owner_escalation(
        self, *, action: Any, decision: Any, source_id: str | None = None,
        priority: Any = None,
    ) -> dict[str, Any]:
        key = str(source_id or "").strip()
        if key and key in self.by_source_id:
            return dict(self.by_source_id[key])
        task_id = self._next_id("esc")
        doc = {
            "task_id": task_id,
            "status": "waiting",
            "phase": "owner_escalation",
            "project": getattr(action, "project", None),
            "params": {"escalation": {"category": str(getattr(decision, "category", ""))}},
        }
        self.escalations.append({"action": action, "decision": decision, "task": doc})
        self.tasks_by_id[task_id] = doc
        if key:
            self.by_source_id[key] = doc
        return dict(doc)

    async def create_objective(
        self, *, project: str, goal: str, acceptance: Any = None,
        entry: str = "top_down", source: str = "api",
        source_id: str | None = None, requester: str | None = None,
    ) -> dict[str, Any]:
        key = str(source_id or "").strip()
        if key and key in self.by_source_id:
            return dict(self.by_source_id[key])
        objective_id = self._next_id("obj")
        doc = {
            "objective_id": objective_id,
            "project": project,
            "entry": entry,
            "goal": goal,
            "acceptance": acceptance,
            "conversion_source_task_id": self._next_id("shell"),
        }
        self.objectives.append(doc)
        if key:
            self.by_source_id[key] = doc
        return dict(doc)

    async def create_mission(
        self, *, project: str, goal: str, acceptance: Any = None,
        entry: str = "top_down", source: str = "api",
        source_id: str | None = None, requester: str | None = None,
    ) -> dict[str, Any]:
        key = str(source_id or "").strip()
        if key and key in self.by_source_id:
            return dict(self.by_source_id[key])
        mission_id = self._next_id("mis")
        doc = {
            "mission_id": mission_id,
            "project": project,
            "entry": entry,
            "goal": goal,
            "acceptance": acceptance,
            "conversion_source_task_id": self._next_id("shell"),
        }
        self.missions.append(doc)
        if key:
            self.by_source_id[key] = doc
        return dict(doc)


class FakeErpAdapter:
    """Adapter ERPNext: graba el payload que cruzaria a negocio."""

    def __init__(self) -> None:
        self.is_enabled = True
        self.calls: list[dict[str, Any]] = []

    async def create_ticket(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"task": {"name": f"TASK-2026-99{len(self.calls):03d}"}}


def _action(
    action_id: str = "hact_1",
    user_id: str = "usr_owner",
    **overrides: Any,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "action_id": action_id,
        "user_id": user_id,
        "title": "Revisar el panel de control",
        "status": ActionStatus.READY.value,
        "owner": ActionOwner.HUMAN.value,
        "priority": 100,
    }
    doc.update(overrides)
    return doc


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = FakeHorizonRepo()
        self.task_service = FakeTaskService()
        self.config = FakeCompanyConfig()
        self.service = HorizonService(_settings(), self.repo, task_service=self.task_service)
        self.service.set_company_config(self.config)

    async def _store(self, action: dict[str, Any]) -> dict[str, Any]:
        return await self.repo.create_horizon_action(action)


class PromotionClassificationTests(unittest.TestCase):
    def test_no_touch_is_personal(self) -> None:
        result = promotion.classify_action(
            _action(), company_config=FakeCompanyConfig()
        )
        self.assertEqual(result["scope"], promotion.SCOPE_PERSONAL)
        self.assertEqual(result["reason"], promotion.REASON_NO_TOUCH)

    def test_stored_project_is_company(self) -> None:
        result = promotion.classify_action(
            _action(company_project="acme"), company_config=FakeCompanyConfig()
        )
        self.assertEqual(result["scope"], promotion.SCOPE_COMPANY)
        self.assertEqual(result["project"], "acme")

    def test_unknown_project_is_refused(self) -> None:
        result = promotion.classify_action(
            _action(company_project="desconocido"), company_config=FakeCompanyConfig()
        )
        self.assertEqual(result["scope"], promotion.SCOPE_REFUSED)
        self.assertEqual(result["reason"], promotion.REASON_UNKNOWN_PROJECT)
        self.assertIn(
            promotion.REASON_UNKNOWN_PROJECT, promotion.REFUSAL_MESSAGES
        )

    def test_repo_resolves_by_name_and_path(self) -> None:
        for repo in ("web", "/repos/acme-web"):
            with self.subTest(repo=repo):
                result = promotion.classify_action(
                    _action(company_repo=repo), company_config=FakeCompanyConfig()
                )
                self.assertEqual(result["scope"], promotion.SCOPE_COMPANY)
                self.assertEqual(result["project"], "acme")

    def test_unknown_repo_is_refused(self) -> None:
        result = promotion.classify_action(
            _action(company_repo="/repos/fantasma"), company_config=FakeCompanyConfig()
        )
        self.assertEqual(result["scope"], promotion.SCOPE_REFUSED)
        self.assertEqual(result["reason"], promotion.REASON_UNRESOLVABLE_REPO)

    def test_project_repo_mismatch_is_refused(self) -> None:
        config = FakeCompanyConfig(
            projects={
                "acme": [{"name": "web", "local_path": "/repos/acme-web"}],
                "otro": [{"name": "api", "local_path": "/repos/otro-api"}],
            }
        )
        result = promotion.classify_action(
            _action(company_project="otro", company_repo="web"),
            company_config=config,
        )
        self.assertEqual(result["scope"], promotion.SCOPE_REFUSED)
        self.assertEqual(result["reason"], promotion.REASON_PROJECT_REPO_MISMATCH)

    def test_explicit_overrides_and_clears(self) -> None:
        config = FakeCompanyConfig(
            projects={
                "acme": [{"name": "web", "local_path": "/repos/acme-web"}],
                "otro": [{"name": "api", "local_path": "/repos/otro-api"}],
            }
        )
        stored = _action(company_project="acme")
        overridden = promotion.classify_action(
            stored, explicit_project="otro", company_config=config
        )
        self.assertEqual(overridden["project"], "otro")
        cleared = promotion.classify_action(
            stored, explicit_project="", company_config=config
        )
        self.assertEqual(cleared["scope"], promotion.SCOPE_PERSONAL)

    def test_declared_touch_without_config_is_refused(self) -> None:
        result = promotion.classify_action(
            _action(company_project="acme"), company_config=None
        )
        self.assertEqual(result["scope"], promotion.SCOPE_REFUSED)
        self.assertEqual(result["reason"], promotion.REASON_NO_COMPANY_CONFIG)

    def test_source_ids_are_namespaced_and_distinct(self) -> None:
        ticket = promotion.promotion_source_id("hact_1")
        objective = promotion.promotion_objective_source_id("hact_1")
        escalation = promotion.promotion_escalation_source_id("hact_1")
        other = promotion.promotion_source_id("hact_2")
        self.assertEqual(len({ticket, objective, escalation, other}), 4)
        for value in (ticket, objective, escalation):
            self.assertIn("hact_1", value)


class WorkSpecTests(unittest.TestCase):
    def test_spec_carries_the_closed_list(self) -> None:
        action = _action(
            title="  Migrar el reporte  ",
            acceptance="El reporte sale en PDF",
            next_step="Abrir rama feature",
            effort_estimated_minutes=45,
        )
        spec = promotion.build_work_spec(action, "Prioriza el lunes")
        self.assertEqual(spec["title"], "Migrar el reporte")
        for snippet in (
            "El reporte sale en PDF",
            "Abrir rama feature",
            "45 min",
            "Prioriza el lunes",
            "hact_1",
        ):
            self.assertIn(snippet, spec["description"])

    def test_spec_ignores_personal_context(self) -> None:
        hostile = _action(
            title="Llamar al proveedor",
            mission=CANARY_MISSION,
            objective_title=CANARY_OBJECTIVE,
            finance_notes=CANARY_FINANCE,
            family=CANARY_FAMILY,
            health="CANARY_SALUD",
        )
        spec = promotion.build_work_spec(hostile)
        blob = spec["title"] + "\n" + spec["description"]
        for canary in (
            CANARY_MISSION, CANARY_OBJECTIVE, CANARY_FINANCE, CANARY_FAMILY,
            "CANARY_SALUD",
        ):
            self.assertNotIn(canary, blob)

    def test_promoted_predicate_is_disjoint_from_personal(self) -> None:
        promoted = {"params": promotion.promotion_params("usr_owner", "hact_1")}
        self.assertTrue(promotion.is_horizon_promoted(promoted))
        self.assertFalse(privacy.is_personal_scope(promoted))
        personal = {"params": privacy.personal_scope_params("usr_owner")}
        self.assertFalse(promotion.is_horizon_promoted(personal))
        self.assertFalse(promotion.is_horizon_promoted(None))
        self.assertFalse(promotion.is_horizon_promoted({"params": None}))
        self.assertFalse(promotion.is_horizon_promoted({"params": {"promoted_by": "horizon"}}))

    def test_escalation_outcome_reads_the_recorded_decision(self) -> None:
        self.assertEqual(promotion.escalation_outcome(None), promotion.ESCALATION_MISSING)
        waiting = {"status": "waiting", "params": {}}
        self.assertEqual(
            promotion.escalation_outcome(waiting), promotion.ESCALATION_WAITING
        )
        approved = {
            "status": "completed",
            "params": {"escalation": {"decision": {"decision": "approved"}}},
        }
        self.assertEqual(
            promotion.escalation_outcome(approved), promotion.ESCALATION_APPROVED
        )
        denied = {
            "status": "completed",
            "params": {"escalation": {"decision": {"decision": "denied"}}},
        }
        self.assertEqual(
            promotion.escalation_outcome(denied), promotion.ESCALATION_DENIED
        )
        # Completed sin decision grabada (cierre manual): fail-closed.
        manual = {"status": "completed", "params": {}}
        self.assertEqual(
            promotion.escalation_outcome(manual), promotion.ESCALATION_DENIED
        )
        self.assertEqual(
            promotion.escalation_outcome({"status": "cancelled"}),
            promotion.ESCALATION_CLOSED,
        )


class PromoteServiceTests(_Base):
    async def test_personal_action_is_not_promoted(self) -> None:
        stored = await self._store(_action())
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "not_company_work")
        self.assertEqual(self.task_service.created_tasks, [])
        self.assertEqual(self.task_service.escalations, [])
        self.assertEqual(self.task_service.objectives, [])

    async def test_company_ticket_promoted_through_the_common_api(self) -> None:
        stored = await self._store(
            _action(
                company_project="acme",
                priority=30,
                acceptance="El panel carga en 2s",
            )
        )
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(result["promoted"])
        self.assertFalse(result.get("already_promoted", False))
        self.assertEqual(result["project"], "acme")
        self.assertEqual(result["kind"], "ticket")

        self.assertEqual(len(self.task_service.created_tasks), 1)
        req = self.task_service.created_tasks[0]
        self.assertEqual(req.project, "acme")
        self.assertEqual(req.source_id, f"horizon-promotion:{stored['action_id']}")
        self.assertTrue(req.autorun)
        self.assertEqual(str(req.priority.value), "high")
        self.assertNotIn("[Horizon]", req.title)
        params = req.params or {}
        self.assertNotIn("horizon_scope", params)
        self.assertEqual(params.get("promoted_by"), "horizon")
        self.assertEqual(params.get("horizon_action_id"), stored["action_id"])
        self.assertEqual(params.get("horizon_owner"), "usr_owner")
        self.assertIn("El panel carga en 2s", req.description)

        updated = await self.repo.get_horizon_action(stored["action_id"])
        self.assertEqual(updated["status"], ActionStatus.IN_PROGRESS.value)
        self.assertEqual(updated["owner"], ActionOwner.AGENT.value)
        self.assertEqual(updated["task_id"], result["task_id"])
        self.assertEqual(updated["delegated_project"], "acme")
        self.assertIsNotNone(updated["promoted_at"])
        self.assertEqual(updated["promotion_kind"], "ticket")

    async def test_promote_twice_creates_one_ticket(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        first = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        second = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(first["promoted"])
        self.assertTrue(second["promoted"])
        self.assertTrue(second["already_promoted"])
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual(len(self.task_service.created_tasks), 1)

    async def test_unknown_project_creates_nothing(self) -> None:
        stored = await self._store(_action(company_project="fantasma"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "unknown_project")
        # Sin degradacion a personal: crear un personal ocultaria trabajo.
        self.assertEqual(self.task_service.created_tasks, [])
        self.assertEqual(self.task_service.escalations, [])

    async def test_kind_objective_creates_an_objective(self) -> None:
        stored = await self._store(
            _action(company_project="acme", acceptance="Panel en verde")
        )
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], kind="objective"
        )
        self.assertTrue(result["promoted"])
        self.assertEqual(result["kind"], "objective")
        self.assertEqual(self.task_service.created_tasks, [])
        self.assertEqual(len(self.task_service.objectives), 1)
        created = self.task_service.objectives[0]
        self.assertEqual(created["entry"], "horizon_promotion")
        self.assertEqual(created["project"], "acme")
        self.assertIn("Revisar el panel", created["goal"])
        self.assertEqual(created["acceptance"], ["Panel en verde"])
        updated = await self.repo.get_horizon_action(stored["action_id"])
        self.assertEqual(updated["promotion_kind"], "objective")
        self.assertEqual(updated["promotion_ref"], created["objective_id"])

    async def test_unknown_kind_creates_nothing(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], kind="cohete"
        )
        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "unknown_kind")
        self.assertEqual(self.task_service.created_tasks, [])
        self.assertEqual(self.task_service.escalations, [])

    async def test_kind_mission_creates_a_mission(self) -> None:
        stored = await self._store(
            _action(company_project="acme", acceptance="Panel en verde")
        )
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], kind="mission"
        )
        self.assertTrue(result["promoted"])
        self.assertEqual(result["kind"], "mission")
        self.assertEqual(self.task_service.created_tasks, [])
        self.assertEqual(len(self.task_service.missions), 1)
        created = self.task_service.missions[0]
        self.assertEqual(created["entry"], "horizon_promotion")
        self.assertEqual(created["project"], "acme")
        self.assertIn("Revisar el panel", created["goal"])
        self.assertEqual(created["acceptance"], ["Panel en verde"])
        updated = await self.repo.get_horizon_action(stored["action_id"])
        self.assertEqual(updated["promotion_kind"], "mission")
        self.assertEqual(updated["promotion_ref"], created["mission_id"])

    async def test_cost_escalates_and_parks_the_work(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], amount="10.00"
        )
        self.assertFalse(result["promoted"])
        self.assertTrue(result["escalated"])
        self.assertFalse(result["already_escalated"])
        self.assertEqual(result["category"], "gasto_nuevo")
        self.assertTrue(result["human_action"])
        self.assertEqual(len(self.task_service.escalations), 1)
        self.assertEqual(self.task_service.created_tasks, [])
        updated = await self.repo.get_horizon_action(stored["action_id"])
        self.assertEqual(updated["status"], ActionStatus.WAITING.value)
        self.assertEqual(updated["blocked_reason"], "owner_escalation")
        self.assertEqual(
            updated["promotion_escalation_task_id"], result["escalation_task_id"]
        )

    async def test_retry_while_waiting_returns_the_same_escalation(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        first = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], amount="10.00"
        )
        second = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(second["escalated"])
        self.assertTrue(second["already_escalated"])
        self.assertEqual(second["escalation_task_id"], first["escalation_task_id"])
        self.assertEqual(len(self.task_service.escalations), 1)
        self.assertEqual(self.task_service.created_tasks, [])

    async def test_approved_escalation_proceeds(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        first = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], amount="10.00"
        )
        esc_id = first["escalation_task_id"]
        self.task_service.tasks_by_id[esc_id] = {
            "task_id": esc_id,
            "status": "completed",
            "params": {"escalation": {"decision": {"decision": "approved"}}},
        }
        second = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(second["promoted"])
        self.assertEqual(len(self.task_service.created_tasks), 1)
        self.assertEqual(len(self.task_service.escalations), 1)
        updated = await self.repo.get_horizon_action(stored["action_id"])
        self.assertEqual(updated["status"], ActionStatus.IN_PROGRESS.value)
        self.assertIsNotNone(updated["promoted_at"])

    async def test_denied_escalation_blocks(self) -> None:
        for verdict, params in (
            ("denied", {"escalation": {"decision": {"decision": "denied"}}}),
            ("manual", {}),
        ):
            with self.subTest(verdict=verdict):
                repo = FakeHorizonRepo()
                svc = FakeTaskService()
                service = HorizonService(_settings(), repo, task_service=svc)
                service.set_company_config(FakeCompanyConfig())
                stored = await repo.create_horizon_action(_action())
                await repo.patch_horizon_action(
                    stored["action_id"], {"company_project": "acme"}
                )
                first = await service.promote_action(
                    "usr_owner", action_id=stored["action_id"], amount="10.00"
                )
                esc_id = first["escalation_task_id"]
                svc.tasks_by_id[esc_id] = {
                    "task_id": esc_id,
                    "status": "completed",
                    "params": params,
                }
                second = await service.promote_action(
                    "usr_owner", action_id=stored["action_id"]
                )
                self.assertFalse(second["promoted"])
                self.assertEqual(second["reason"], "escalation_denied")
                self.assertEqual(svc.created_tasks, [])

    async def test_invalid_amount_rejected(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], amount="mucho"
        )
        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "invalid_signals")
        self.assertEqual(self.task_service.created_tasks, [])
        self.assertEqual(self.task_service.escalations, [])

    async def test_unreadable_mandate_escalates_fail_closed(self) -> None:
        self.service.set_company_config(FakeCompanyConfig(mandate_error=True))
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertFalse(result["promoted"])
        self.assertTrue(result["escalated"])
        self.assertEqual(self.task_service.created_tasks, [])

    async def test_explicit_project_persists_the_declaration(self) -> None:
        stored = await self._store(_action())
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"], project="acme"
        )
        self.assertTrue(result["promoted"])
        updated = await self.repo.get_horizon_action(stored["action_id"])
        self.assertEqual(updated["company_project"], "acme")
        # La proxima delegacion ya promueve sola (aqui: ya promovida).
        again = await self.service.delegate_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(again.get("promoted"))
        self.assertEqual(len(self.task_service.created_tasks), 1)

    async def test_single_active_objective_with_group_links(self) -> None:
        async def _lister(project: str) -> list[dict[str, Any]]:
            self.assertEqual(project, "acme")
            return [
                {
                    "objective_id": "obj_1",
                    "project": "acme",
                    "status": "active",
                    "erp_group_ref": "TASK-2026-00999",
                }
            ]

        self.repo.list_active_objectives = _lister  # type: ignore[attr-defined]
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(result["promoted"])
        req = self.task_service.created_tasks[0]
        self.assertEqual(req.parent_task, "TASK-2026-00999")
        self.assertEqual(req.objective_id, "obj_1")
        self.assertEqual(result["objective_id"], "obj_1")
        # The link is not the end: planning completes the stub's
        # contract (TASK-2026-00174).
        self.assertEqual(self.task_service.plan_calls, ["obj_1"])
        self.assertTrue(result["planning_triggered"])

    async def test_objective_link_stays_loose_otherwise(self) -> None:
        cases = {
            "none": [],
            "two": [
                {"objective_id": "o1", "status": "active", "erp_group_ref": "TASK-1"},
                {"objective_id": "o2", "status": "active", "erp_group_ref": "TASK-2"},
            ],
            "group_less": [
                {"objective_id": "o1", "status": "active", "erp_group_ref": ""},
            ],
        }
        for name, rows in cases.items():
            with self.subTest(case=name):
                repo = FakeHorizonRepo()
                svc = FakeTaskService()
                service = HorizonService(_settings(), repo, task_service=svc)
                service.set_company_config(FakeCompanyConfig())

                async def _lister(project: str, _rows: Any = rows) -> list[dict[str, Any]]:
                    return list(_rows)

                repo.list_active_objectives = _lister  # type: ignore[attr-defined]
                stored = await repo.create_horizon_action(
                    _action(company_project="acme")
                )
                result = await service.promote_action(
                    "usr_owner", action_id=stored["action_id"]
                )
                self.assertTrue(result["promoted"])
                req = svc.created_tasks[0]
                self.assertIsNone(req.parent_task)
                self.assertIsNone(req.objective_id)
                self.assertFalse(result["planning_triggered"])
                self.assertEqual(svc.plan_calls, [])

    async def test_promotion_needs_no_capacity(self) -> None:
        # Usuario fresco, sin plan ni capacidad declarada: promueve igual.
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(result["promoted"])

    async def test_already_delegated_personal_is_not_duplicated(self) -> None:
        stored = await self._store(_action())
        delegated = await self.service.delegate_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(delegated["delegated"])
        await self.repo.patch_horizon_action(
            stored["action_id"], {"company_project": "acme"}
        )
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertFalse(result["promoted"])
        self.assertEqual(result["reason"], "already_delegated_personal")
        self.assertEqual(len(self.task_service.created_tasks), 1)


class DelegateRoutingTests(_Base):
    async def test_delegate_company_action_promotes(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.delegate_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(result.get("promoted"))
        self.assertTrue(result.get("delegated"))
        self.assertEqual(len(self.task_service.created_tasks), 1)
        req = self.task_service.created_tasks[0]
        self.assertNotIn("[Horizon]", req.title)
        self.assertNotIn("horizon_scope", req.params or {})

    async def test_delegate_refused_touch_creates_nothing(self) -> None:
        stored = await self._store(_action(company_project="fantasma"))
        result = await self.service.delegate_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertFalse(result.get("delegated"))
        self.assertEqual(result["reason"], "unknown_project")
        self.assertEqual(self.task_service.created_tasks, [])

    async def test_delegate_personal_unchanged(self) -> None:
        stored = await self._store(_action())
        result = await self.service.delegate_action(
            "usr_owner",
            action_id=stored["action_id"],
            project="acme",
            instruction="Trabajo personal sobre substrato de empresa",
        )
        self.assertTrue(result["delegated"])
        self.assertNotIn("promoted", result)
        req = self.task_service.created_tasks[0]
        self.assertIn("[Horizon]", req.title)
        self.assertTrue(privacy.is_personal_scope(req.model_dump()))


class ErpBoundaryTests(_Base):
    """El borde ERP real: lo promovido llega con spec; lo personal, nunca."""

    def _business_service(self) -> Any:
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._settings = _settings(HORIZON_PERSONAL_MIRROR_ENABLED=False)
        service._erpnext = FakeErpAdapter()
        service._repo = None
        return service

    async def test_promoted_reaches_erpnext_with_spec_only(self) -> None:
        await self.repo.upsert_horizon_profile(
            {
                "user_id": "usr_owner",
                "mission": {"statement": CANARY_MISSION},
                "mandate": {},
            }
        )
        stored = await self._store(
            _action(
                company_project="acme",
                title="Corregir el total del reporte",
                acceptance="El total cuadra",
                objective_title=CANARY_OBJECTIVE,
                finance_notes=CANARY_FINANCE,
                family=CANARY_FAMILY,
            )
        )
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(result["promoted"])

        req = self.task_service.created_tasks[0]
        doc = make_task_document(req)
        service = self._business_service()
        mirror, pending = await service._register_business_ticket(doc)
        self.assertFalse(pending)
        self.assertTrue(mirror and mirror.get("name"))

        adapter: FakeErpAdapter = service._erpnext
        self.assertEqual(len(adapter.calls), 1)
        payload = adapter.calls[0]
        blob = "\n".join(
            str(payload.get(key) or "")
            for key in ("ref", "title", "description", "project_key", "priority")
        )
        self.assertIn("Corregir el total del reporte", blob)
        self.assertIn("El total cuadra", blob)
        for canary in (
            CANARY_MISSION, CANARY_OBJECTIVE, CANARY_FINANCE, CANARY_FAMILY,
        ):
            self.assertNotIn(canary, blob)

    async def test_personal_stays_out_of_erpnext(self) -> None:
        stored = await self._store(_action())
        delegated = await self.service.delegate_action(
            "usr_owner", action_id=stored["action_id"]
        )
        self.assertTrue(delegated["delegated"])
        req = self.task_service.created_tasks[0]
        doc = make_task_document(req)
        service = self._business_service()
        mirror, pending = await service._register_business_ticket(doc)
        self.assertIsNone(mirror)
        self.assertFalse(pending)
        adapter: FakeErpAdapter = service._erpnext
        self.assertEqual(adapter.calls, [])


class TerminalHookTests(_Base):
    async def test_notify_forwards_promoted_and_personal_only(self) -> None:
        from task_service import TaskService

        seen: list[str] = []

        async def _hook(task: dict[str, Any]) -> None:
            seen.append(task["task_id"])

        service = TaskService.__new__(TaskService)
        service._horizon_terminal = _hook
        personal = {
            "task_id": "t1",
            "params": privacy.personal_scope_params("usr_owner"),
        }
        promoted = {
            "task_id": "t2",
            "params": promotion.promotion_params("usr_owner", "hact_1"),
        }
        company = {"task_id": "t3", "params": {"workflow": "dev.default"}}
        await service._notify_horizon_terminal(personal)
        await service._notify_horizon_terminal(promoted)
        await service._notify_horizon_terminal(company)
        await service._notify_horizon_terminal(None)
        self.assertEqual(seen, ["t1", "t2"])

    async def test_acknowledge_promoted_completion_delivers_once(self) -> None:
        stored = await self._store(_action(company_project="acme"))
        result = await self.service.promote_action(
            "usr_owner", action_id=stored["action_id"]
        )
        task_id = result["task_id"]
        ack = await self.service.acknowledge_task_result(
            {
                "task_id": task_id,
                "status": "completed",
                "last_response_summary": "Panel corregido y verificado",
                "params": promotion.promotion_params("usr_owner", stored["action_id"]),
            }
        )
        self.assertTrue(ack["recorded"])
        updated = await self.repo.get_horizon_action(stored["action_id"])
        self.assertEqual(updated["status"], ActionStatus.DONE.value)
        first = await self.service.deliver_action_result(
            "usr_owner", action_id=stored["action_id"], channel="api"
        )
        self.assertTrue(first["delivered"])
        self.assertFalse(first["already_delivered"])
        self.assertIn("Panel corregido", first["result_summary"])
        second = await self.service.deliver_action_result(
            "usr_owner", action_id=stored["action_id"], channel="api"
        )
        self.assertTrue(second["already_delivered"])

    async def test_acknowledge_ordinary_company_ignored(self) -> None:
        ack = await self.service.acknowledge_task_result(
            {
                "task_id": "task_x",
                "status": "completed",
                "params": {"workflow": "dev.default"},
            }
        )
        self.assertFalse(ack["recorded"])


if __name__ == "__main__":
    unittest.main()
