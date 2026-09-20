"""Plan del dia y ciclo proactivo: las filas de la seccion 9.10.

Cada test de este archivo corresponde a un escenario que la especificacion exige
por escrito. No son casos inventados: son el criterio de aceptacion de HZ-002.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from horizon import privacy, triggers
from horizon.models import ActionStatus, CaptureChannel, PlanStatus
from horizon.service import HorizonService
from horizon_support import FakeHorizonRepo

OWNER = "usr_admin"
# 2026-09-21 es lunes: 20 minutos de ejercicio en la agenda.
MONDAY = "2026-09-21"
NOW = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> SimpleNamespace:
    base = {
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


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = FakeHorizonRepo()
        self.service = HorizonService(_settings(), self.repo)
        await self.service.import_context(OWNER)


class IdentityTests(_Base):
    """El espacio personal es uno solo, sin importar por donde entre."""

    def test_chat_and_voice_resolve_to_the_configured_owner(self) -> None:
        # El cliente de chat manda su propio nombre; la app movil no manda nada.
        # Sin esto, la captura se guardaria bajo un dueño que el dashboard nunca
        # consulta y el espacio pareceria vacio sin que nada fallara.
        self.assertEqual(self.service.owner_for(CaptureChannel.CHAT, "jorge"), OWNER)
        self.assertEqual(self.service.owner_for(CaptureChannel.VOICE, None), OWNER)

    def test_the_authenticated_api_user_is_preserved(self) -> None:
        self.assertEqual(self.service.owner_for(CaptureChannel.API, "usr_otro"), "usr_otro")

    async def test_importing_from_chat_lands_where_the_dashboard_reads(self) -> None:
        """El criterio 5 del ticket: importar sin sesion web, sin fallar en silencio.

        Se importa por el mismo camino que usa el chat (resolviendo el
        propietario desde el canal) y se comprueba que los objetivos quedan bajo
        el dueño configurado, que es el que consulta la API autenticada.
        """
        repo = FakeHorizonRepo()
        service = HorizonService(_settings(), repo)
        # El chat manda su propio nombre; el servicio lo resuelve al dueño real.
        owner = service.owner_for(CaptureChannel.CHAT, "jorge")
        result = await service.import_context(owner, display_name="")
        self.assertTrue(result["imported"])
        self.assertGreater(result["objectives_created"], 0)

        objectives = await repo.list_horizon_objectives(owner)
        self.assertTrue(objectives)
        # Y el mismo dueño que resuelve la API autenticada ve ese espacio.
        self.assertEqual(service.owner_for(CaptureChannel.API, owner), owner)
        self.assertEqual(await repo.list_horizon_objectives("jorge"), [])


class PlanCapacityTests(_Base):
    async def test_unknown_capacity_plans_without_inventing_an_hour(self) -> None:
        plan = await self.service.plan_day(OWNER, date_key=MONDAY, now=NOW)
        self.assertEqual(plan["status"], PlanStatus.NEEDS_INPUT.value)
        self.assertIsNone(plan["capacity"]["growth_budget_minutes"])
        growth = [b for b in plan["blocks"] if b["is_growth"]]
        # Hay accion recomendada, pero sin reloj: no se inventa una hora libre.
        self.assertTrue(growth)
        self.assertEqual(growth[0]["kind"], "on_demand")
        self.assertIsNone(growth[0]["start_at"])

    async def test_a_declared_quarter_hour_reduces_the_plan(self) -> None:
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=15, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertEqual(plan["capacity"]["growth_budget_minutes"], 10)
        # La accion de grabacion estima 25 minutos: no cabe en 10.
        titles = [e["title"] for e in plan["excluded"]]
        self.assertTrue(any("grabacion" in t.lower() for t in titles))

    async def test_a_zero_day_schedules_no_growth(self) -> None:
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=0, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertEqual(plan["capacity"]["growth_budget_minutes"], 0)
        self.assertEqual(plan["status"], PlanStatus.EMPTY.value)
        self.assertFalse([b for b in plan["blocks"] if b["is_growth"]])

    async def test_a_day_with_margin_but_no_fitting_action_says_so_coherently(self) -> None:
        """Un dia lleno tiene dos causas distintas y no se cuentan igual.

        Con 15 minutos libres hay margen (10 minutos) pero la accion mas corta
        pide mas. Decir "podemos dedicar 10 minutos" y a continuacion "no
        programamos crecimiento" se contradice.
        """
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=15, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertEqual(plan["status"], PlanStatus.EMPTY.value)
        message = plan["message"].lower()
        self.assertIn("ninguna acción lista entra", message)
        self.assertNotIn("no programamos crecimiento", message)

    async def test_a_truly_full_day_says_the_day_is_full(self) -> None:
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=0, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertIn("no programamos crecimiento", plan["message"].lower())

    async def test_the_exercise_block_survives_a_saturated_day(self) -> None:
        # El ejercicio se reserva antes de repartir tiempo entre proyectos.
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=0, exercise_included=False, date_key=MONDAY, now=NOW
        )
        reserved = [b for b in plan["blocks"] if b["kind"] == "reserved_exercise"]
        self.assertEqual(len(reserved), 1)
        self.assertEqual(reserved[0]["minutes"], 20)


class PlanVersionTests(_Base):
    async def test_each_publish_increments_the_version(self) -> None:
        first = await self.service.plan_day(OWNER, date_key=MONDAY, now=NOW)
        second = await self.service.plan_day(
            OWNER, declared_free_minutes=60, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)

    async def test_only_one_plan_is_current_after_a_replan(self) -> None:
        await self.service.plan_day(OWNER, date_key=MONDAY, now=NOW)
        await self.service.plan_day(
            OWNER, declared_free_minutes=60, exercise_included=True, date_key=MONDAY, now=NOW
        )
        current = [p for p in self.repo.plans.values() if p.get("is_current")]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["version"], 2)

    async def test_a_lost_race_is_discarded_and_the_winner_is_returned(self) -> None:
        """Dos eventos intentan replanificar a la vez (seccion 9.10)."""
        await self.service.plan_day(OWNER, date_key=MONDAY, now=NOW)
        winner_id = str(
            next(p for p in self.repo.plans.values() if p.get("is_current"))["plan_id"]
        )
        # Otro escritor publico primero: el indice unico parcial rechaza esta.
        self.repo.stale_plan_wins = winner_id

        result = await self.service.plan_day(
            OWNER, declared_free_minutes=60, exercise_included=True, date_key=MONDAY, now=NOW
        )

        self.assertEqual(result["publish_outcome"], "stale_discarded")
        self.assertEqual(str(result["plan_id"]), winner_id)
        current = [p for p in self.repo.plans.values() if p.get("is_current")]
        self.assertEqual(len(current), 1)

    async def test_a_discarded_write_applies_no_side_effects(self) -> None:
        """El perdedor de la carrera no debe mutar acciones."""
        await self.service.plan_day(OWNER, date_key=MONDAY, now=NOW)
        winner_id = str(
            next(p for p in self.repo.plans.values() if p.get("is_current"))["plan_id"]
        )
        blocked = await self.repo.create_horizon_action(
            {
                "action_id": "hact_dep",
                "user_id": OWNER,
                "title": "Depende de un tercero",
                "status": ActionStatus.READY.value,
                "dependency": "respuesta del banco",
                "priority": 5,
            }
        )
        self.repo.stale_plan_wins = winner_id

        await self.service.plan_day(
            OWNER, declared_free_minutes=60, exercise_included=True, date_key=MONDAY, now=NOW
        )

        # La accion sigue lista: el plan que la habria movido a espera no gano.
        self.assertEqual(self.repo.actions[blocked["action_id"]]["status"], "ready")


class PlanFilterTests(_Base):
    async def test_a_parked_objective_never_appears_today(self) -> None:
        parked = await self.repo.get_horizon_objective_by_source_key(OWNER, "bible_videogame")
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_parked",
                "user_id": OWNER,
                "objective_id": parked["objective_id"],
                "title": "Prototipo del videojuego",
                "status": ActionStatus.READY.value,
                "effort_estimated_minutes": 10,
                "priority": 1,
            }
        )
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=120, exercise_included=True, date_key=MONDAY, now=NOW
        )
        reasons = {e["action_id"]: e["reason"] for e in plan["excluded"]}
        self.assertEqual(reasons.get("hact_parked"), "objetivo_aparcado")
        self.assertNotEqual(plan["primary_action_id"], "hact_parked")

    async def test_an_unmet_dependency_becomes_a_wait_not_todays_work(self) -> None:
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_wait",
                "user_id": OWNER,
                "title": "Cerrar el contrato",
                "status": ActionStatus.READY.value,
                "effort_estimated_minutes": 10,
                "dependency": "respuesta del cliente",
                "priority": 1,
            }
        )
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=120, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertNotEqual(plan["primary_action_id"], "hact_wait")
        self.assertEqual(self.repo.actions["hact_wait"]["status"], ActionStatus.WAITING.value)

    async def test_an_external_due_date_is_never_moved(self) -> None:
        """Seccion 9.10: reducir el plan sin mover vencimientos externos."""
        committed = datetime(2026, 10, 1, tzinfo=timezone.utc)
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_ext",
                "user_id": OWNER,
                "title": "Devolución del contrato",
                "status": ActionStatus.READY.value,
                "effort_estimated_minutes": 10,
                "due_date": committed,
                "external_due_date": True,
                "priority": 1,
            }
        )
        await self.service.plan_day(
            OWNER, declared_free_minutes=15, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertEqual(self.repo.actions["hact_ext"]["due_date"], committed)

    async def test_three_lost_blocks_change_the_estimate_instead_of_repeating(self) -> None:
        """Seccion 9.5: no mover el mismo ticket indefinidamente."""
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_lost",
                "user_id": OWNER,
                "title": "Sesión de grabación",
                "status": ActionStatus.READY.value,
                "effort_estimated_minutes": 25,
                "effort_actual_minutes": 60,
                "lost_blocks": 3,
                "lost_reason": "interrupciones",
                "priority": 1,
            }
        )
        await self.service.plan_day(
            OWNER, declared_free_minutes=120, exercise_included=True, date_key=MONDAY, now=NOW
        )
        updated = self.repo.actions["hact_lost"]
        self.assertEqual(updated["effort_estimated_minutes"], 60)
        self.assertTrue(updated["needs_decomposition"])


class ReactiveReplanTests(_Base):
    async def test_a_task_result_updates_its_referenced_action(self) -> None:
        action = await self.repo.create_horizon_action(
            {
                "action_id": "hact_task",
                "user_id": OWNER,
                "title": "Reunir documentos",
                "status": ActionStatus.READY.value,
                "task_id": "task_99",
                "effort_estimated_minutes": 10,
            }
        )
        result = await self.service.acknowledge_task_result(
            {
                "task_id": "task_99",
                "status": "completed",
                "params": privacy.personal_scope_params(OWNER),
            }
        )
        self.assertTrue(result["recorded"])
        self.assertEqual(result["action_id"], action["action_id"])
        self.assertEqual(self.repo.actions["hact_task"]["status"], ActionStatus.DONE.value)

    async def test_a_repeated_result_does_not_duplicate_work(self) -> None:
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_dup",
                "user_id": OWNER,
                "title": "Reunir documentos",
                "status": ActionStatus.READY.value,
                "task_id": "task_dup",
            }
        )
        task = {
            "task_id": "task_dup",
            "status": "completed",
            "params": privacy.personal_scope_params(OWNER),
        }
        first = await self.service.acknowledge_task_result(task)
        second = await self.service.acknowledge_task_result(task)
        self.assertTrue(first["recorded"])
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["replan_enqueued"])
        self.assertEqual(len(self.repo.triggers), 1)

    async def test_a_company_task_is_ignored(self) -> None:
        result = await self.service.acknowledge_task_result(
            {"task_id": "task_dev", "status": "completed", "params": {"project": "jardal-opex"}}
        )
        self.assertFalse(result["recorded"])
        self.assertEqual(len(self.repo.triggers), 0)


class LateConfirmationTests(_Base):
    async def test_a_late_done_closes_the_action_that_was_shown(self) -> None:
        """Seccion 9.10: responder tarde "hecho" no completa la accion actual."""
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=120, exercise_included=True, date_key=MONDAY, now=NOW
        )
        shown_action_id = plan["primary_action_id"]
        plan = await self.service.deliver_plan(OWNER, plan, channel="voice")
        self.assertTrue(plan["avisos"])

        # El dia cambia: aparece otra accion principal.
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_nueva",
                "user_id": OWNER,
                "title": "Acción de hoy",
                "status": ActionStatus.IN_PROGRESS.value,
                "effort_estimated_minutes": 10,
                "priority": 1,
            }
        )

        result = await self.service.confirm_done(OWNER, text="hecho")
        self.assertTrue(result["confirmed"])
        # Se cierra la accion del aviso, no la nueva.
        self.assertEqual(result["action_id"], shown_action_id)
        self.assertEqual(self.repo.actions[shown_action_id]["status"], ActionStatus.DONE.value)
        self.assertEqual(
            self.repo.actions["hact_nueva"]["status"], ActionStatus.IN_PROGRESS.value
        )

    async def test_an_ambiguous_confirmation_asks_instead_of_guessing(self) -> None:
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_a",
                "user_id": OWNER,
                "title": "Preparar CV",
                "status": ActionStatus.READY.value,
            }
        )
        await self.repo.create_horizon_action(
            {
                "action_id": "hact_b",
                "user_id": OWNER,
                "title": "Preparar casos",
                "status": ActionStatus.READY.value,
            }
        )
        result = await self.service.confirm_done(OWNER, text="hecho")
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "ambiguo")
        self.assertIn("cuál", result["message"].lower())


class TriggerTests(_Base):
    async def test_upcoming_triggers_are_enqueued_once(self) -> None:
        first = await self.service.ensure_upcoming_triggers(OWNER, now=NOW)
        second = await self.service.ensure_upcoming_triggers(OWNER, now=NOW)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    async def test_the_daily_trigger_prepares_the_plan(self) -> None:
        await self.service.ensure_upcoming_triggers(OWNER, now=NOW)
        daily = next(
            t for t in self.repo.triggers.values() if t["kind"] == "daily_plan"
        )
        result = await self.service.handle_trigger(daily)
        self.assertIn("plan_id", result)
        self.assertEqual(len([p for p in self.repo.plans.values() if p["is_current"]]), 1)

    async def test_the_weekly_trigger_reviews_instead_of_planning(self) -> None:
        await self.service.ensure_upcoming_triggers(OWNER, now=NOW)
        weekly = next(t for t in self.repo.triggers.values() if t["kind"] == "weekly_review")
        result = await self.service.handle_trigger(weekly)
        self.assertEqual(result["kind"], "weekly_review")
        self.assertIn("pending_decisions", result)


class DegradedModeTests(_Base):
    async def test_the_cycle_never_needs_a_model(self) -> None:
        """Seccion 9.7: sin presupuesto de IA el ciclo sigue respondiendo.

        No es un flag: el camino entero -captura, calculo, plan y consulta- es
        deterministico. Se comprueba que ninguna pieza del ciclo arrastre un LLM.
        """
        import ast
        import inspect

        from horizon import capacity, planner, review, triggers as triggers_module

        # Se miran los imports reales, no el texto: los docstrings hablan de "sin
        # LLM" a proposito, y buscar la cadena daria un falso positivo.
        forbidden_roots = {"openai", "langchain", "langchain_openai", "anthropic", "httpx", "requests"}
        for module in (capacity, planner, review, triggers_module):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".", 1)[0]
                    self.assertNotIn(
                        root,
                        forbidden_roots,
                        f"{module.__name__} importa {name}: el ciclo debe ser determinista",
                    )

        # Y el ciclo funciona de punta a punta sin modelo alguno.
        captured = await self.service.capture(OWNER, "se me ocurrió un negocio de rescate animal")
        self.assertTrue(captured["captured"])
        plan = await self.service.plan_day(
            OWNER, declared_free_minutes=60, exercise_included=True, date_key=MONDAY, now=NOW
        )
        self.assertIn("message", plan)
        self.assertTrue(plan["message"])


if __name__ == "__main__":
    unittest.main()
