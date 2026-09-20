"""Los workflows de Horizon corren por el motor YAML real.

Comprobar que el archivo parsea no basta: el contrato del motor (templating,
resolucion de parametros, llamada a la herramienta) es donde aparecen las
trampas. El caso que mas importa es el parametro ausente, porque el motor lo
convierte en cadena vacia y leerlo como cero convertiria "no lo se" en
"dia saturado".
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

from events import RealtimeHub
from horizon.models import PlanStatus
from horizon.service import HorizonService
from horizon_support import FakeHorizonRepo
from workflow_service import WorkflowService

REPO_ROOT = Path(__file__).resolve().parents[1]
OWNER = "usr_admin"
MONDAY = "2026-09-21"


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "DEEPSEEK_API_KEY": "",
        "WORKFLOWS_DIR": str(REPO_ROOT / "workflows"),
        "HORIZON_TIMEZONE": "America/La_Paz",
        "HORIZON_CAPACITY_FRACTION": 0.70,
        "HORIZON_SOURCE_PATH": "",
        "HORIZON_OWNER_USER_ID": OWNER,
        "HORIZON_DAILY_PLAN_TIME": "06:00",
        "HORIZON_CLOSE_DAY_TIME": "21:00",
        "HORIZON_WEEKLY_REVIEW_TIME": "18:00",
        "HORIZON_TRIGGER_MAX_ATTEMPTS": 3,
        "HORIZON_TRIGGER_BACKOFF_SECONDS": 60,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _WorkflowRunRepo(FakeHorizonRepo):
    """Repositorio minimo para que el motor pueda ejecutar un workflow."""

    def __init__(self) -> None:
        super().__init__()
        self.runs: dict[str, dict] = {}
        self._counter = 0

    async def latest_workflow_run_for_task(self, task_id: str):
        for run in self.runs.values():
            if run.get("task_id") == task_id:
                return dict(run)
        return None

    async def create_workflow_run(self, doc: dict):
        self._counter += 1
        payload = dict(doc)
        payload.setdefault("run_id", f"run_{self._counter}")
        self.runs[payload["run_id"]] = payload
        return dict(payload)

    async def patch_workflow_run(self, run_id: str, updates: dict):
        run = self.runs.get(run_id)
        if run is None:
            return None
        run.update(updates)
        return dict(run)


class WorkflowCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _WorkflowRunRepo()
        self.service = WorkflowService(
            settings=_settings(), repo=self.repo, realtime_hub=RealtimeHub()
        )

    def test_the_horizon_workflows_are_discovered(self) -> None:
        workflows = self.service._load_workflows()
        self.assertIn("ceo.horizon_daily", workflows)
        self.assertIn("ceo.horizon_review", workflows)

    def test_the_agent_is_derived_from_the_workflow_id(self) -> None:
        workflows = self.service._load_workflows()
        self.assertEqual(workflows["ceo.horizon_daily"]["agent"], "ceo")

    def test_the_tools_are_registered(self) -> None:
        names = self.service._tools.names()
        self.assertIn("ceo.plan_day", names)
        self.assertIn("ceo.review_objectives", names)

    def test_no_condition_in_the_horizon_workflows_uses_llm_eval(self) -> None:
        # El motor lanza ValueError con llm_eval (workflow_service.py:1356).
        workflows = self.service._load_workflows()
        for workflow_id in ("ceo.horizon_daily", "ceo.horizon_review"):
            for step in workflows[workflow_id]["steps"]:
                condition = step.get("condition")
                if isinstance(condition, dict):
                    self.assertNotEqual(condition.get("type"), "llm_eval")


class WorkflowExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _WorkflowRunRepo()
        self.settings = _settings()
        self.horizon = HorizonService(self.settings, self.repo)
        await self.horizon.import_context(OWNER)
        self.service = WorkflowService(
            settings=self.settings, repo=self.repo, realtime_hub=RealtimeHub()
        )
        self.service.set_horizon_service(self.horizon)

    async def _run(self, workflow_id: str, params: dict) -> dict:
        workflow = self.service._load_workflows()[workflow_id]
        task = {
            "task_id": f"task_{workflow_id.replace('.', '_')}",
            "project": None,
            "title": workflow["name"],
            "params": {"horizon_owner": OWNER, **params},
        }
        outcome = await self.service.execute_task_workflow(task, workflow)
        run = list(self.repo.runs.values())[-1]
        return {"outcome": outcome, "run": run}

    async def test_the_daily_workflow_publishes_a_plan(self) -> None:
        result = await self._run("ceo.horizon_daily", {})
        self.assertEqual(result["outcome"].status, "completed")
        self.assertEqual(len([p for p in self.repo.plans.values() if p["is_current"]]), 1)

    async def test_a_missing_free_minutes_is_not_read_as_zero(self) -> None:
        """La trampa del templating: ausente debe seguir siendo desconocido."""
        # Sin ``horizon_free_minutes`` en los params, el motor resuelve el
        # template a cadena vacia.
        result = await self._run("ceo.horizon_daily", {})
        outputs = result["run"]["outputs"]["plan"]
        self.assertTrue(outputs["ok"])
        self.assertEqual(outputs["status"], PlanStatus.NEEDS_INPUT.value)
        self.assertIsNone(outputs["growth_budget_minutes"])

    async def test_a_declared_budget_reaches_the_plan(self) -> None:
        result = await self._run(
            "ceo.horizon_daily", {"horizon_free_minutes": 60, "horizon_date_key": MONDAY}
        )
        outputs = result["run"]["outputs"]["plan"]
        # El workflow no declara si el ejercicio ya estaba incluido, asi que se
        # reserva: 60 - 20 (lunes) = 40, y 40 * 0.70 = 28 -> 25. La reserva
        # conservadora es la que el mandato ordena.
        self.assertEqual(outputs["growth_budget_minutes"], 25)
        self.assertEqual(outputs["status"], PlanStatus.READY.value)

    async def test_the_review_workflow_returns_a_review(self) -> None:
        result = await self._run("ceo.horizon_review", {})
        outputs = result["run"]["outputs"]["review"]
        self.assertTrue(outputs["ok"])
        self.assertIn("pending_decisions", outputs)

    async def test_a_task_without_a_personal_owner_is_refused(self) -> None:
        """Un workflow no puede elegir de quien es el espacio personal."""
        workflow = self.service._load_workflows()["ceo.horizon_daily"]
        task = {
            "task_id": "task_sin_owner",
            "project": "jardal-openceo",
            "title": workflow["name"],
            "params": {},
        }
        outcome = await self.service.execute_task_workflow(task, workflow)
        run = list(self.repo.runs.values())[-1]
        self.assertEqual(outcome.status, "completed")
        self.assertFalse(run["outputs"]["plan"]["ok"])
        self.assertEqual(run["outputs"]["plan"]["reason"], "not_personal_scope")
        self.assertEqual(self.repo.plans, {})


if __name__ == "__main__":
    unittest.main()
