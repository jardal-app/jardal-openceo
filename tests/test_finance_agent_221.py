"""Finance as a catalog agent: five exact skills, explicit voice (TASK-2026-00221).

Finance leaves its keyword dispatch and fixed node: the catalog card is
enabled with its ticket + finance box, the five skills run exact over
the registry (the LLM is never invoked), and voice/chat names the skill
through the first layer, validated against the catalog. Refused inputs
fail the run as invalid_input with the refused output on the record,
never a silent hecho. No tokens.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    load_catalog,
    load_role_profile,
    load_role_skills,
)
from graph import (  # noqa: E402
    WIRED_AGENTS,
    OpenCEOGraph,
    resolve_agent_gate,
)
from mcp_identity import (  # noqa: E402
    McpDenied,
    TicketGrant,
    TokenIssuer,
    TokenKind,
    box_for_agent,
)
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import _planner_skills  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    ObjectiveStatus,
    PlannedTicket,
    PlannerResult,
)
from orchestration.finance_voice import (  # noqa: E402
    FinanceVoiceWiring,
    run_finance_skill,
)
from orchestration.generic_agent import (  # noqa: E402
    GenericAgentWiring,
    run_generic_agent,
)
from settings import Settings  # noqa: E402
from state import TaskStatus  # noqa: E402
from tool_locks import pending_lock_result  # noqa: E402
from workflow_service import WorkflowService  # noqa: E402

FINANCE_SKILLS = ["consultar-resumen", "pago-elfec", "registrar-gasto",
                  "registrar-ingreso", "regla-10"]

FINANCE_BOX = frozenset({
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_add_ticket_comment",
    "openceo_attach_ticket_file",
    "openceo_register_expense",
    "openceo_register_income",
    "openceo_scope_summary",
    "openceo_contribution_10_status",
    "openceo_elfec_house_payment",
})

MIRROR = {"_mirrors": {"erpnext": {"name": "TASK-2026-00221"}}}


def _settings() -> Settings:
    return Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects",
                    DEFAULT_PROJECT="jardal-openceo")


class _RunStore:
    """Dict-backed workflow store with task-indexed latest runs."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.by_task: dict[str, list[dict[str, Any]]] = {}

    async def latest_workflow_run_for_task(
            self, task_id: str) -> dict[str, Any] | None:
        runs = self.by_task.get(str(task_id)) or []
        return dict(runs[-1]) if runs else None

    async def create_workflow_run(
            self, run_doc: dict[str, Any]) -> dict[str, Any]:
        stored = dict(run_doc)
        stored.setdefault("steps", [])
        self.runs[stored["run_id"]] = stored
        self.by_task.setdefault(str(stored["task_id"]), []).append(stored)
        return dict(stored)

    async def patch_workflow_run(
            self, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        self.runs[run_id].update(updates)
        return dict(self.runs[run_id])

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        del task_id
        return None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        del task_id, updates
        return None


class _StubHub:
    async def broadcast(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


def _service(store: _RunStore | None = None) -> tuple[WorkflowService,
                                                     _RunStore]:
    store = store or _RunStore()
    service = WorkflowService(settings=_settings(), repo=store,  # type: ignore[arg-type]
                              realtime_hub=_StubHub())  # type: ignore[arg-type]
    return service, store


def _live_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "9e6dd3b8-1f2e-4b3a-9d4e-8f7a6b5c4d32",
        "ticket_number": 9003, "project": "jardal-openceo",
        "title": "Registra gasto personal carne 50 bolivianos",
        "description": "registra gasto personal carne 50 bolivianos",
        "source": "test",
        "params": dict(MIRROR),
    }
    task.update(overrides)
    return task


def _raising_factory(role: str, spec: Any, provider: Any) -> Any:
    raise AssertionError("exact skill must not invoke the LLM")


def _validate_skill_live(target: str, skill: str | None,
                         skills: dict[str, list[str]] | None) -> list[str]:
    fields: dict[str, Any] = {
        "local_ref": "t1", "title": "Registra gasto personal carne 50",
        "target_agent": target, "work_type": "finance", "goal": "g",
        "why_now": "now", "acceptance_criteria": ["recorded"],
        "expected_artifacts": ["journal entry"],
    }
    if skill is not None:
        fields["skill"] = skill
    result = PlannerResult(objective_status=ObjectiveStatus.ACTIVE,
                           state_summary="s",
                           tickets=[PlannedTicket(**fields)])

    async def _run() -> list[str]:
        async def _exists(_: str) -> bool:
            return False

        return await planner_mod.validate_planner_result(
            result, agents={target: {"enabled": True}}, open_titles=[],
            task_exists=_exists, skills=skills)

    return asyncio.run(_run())


class FinanceCardTest(unittest.TestCase):
    def test_finance_enabled_with_box_and_skills(self) -> None:
        card = load_catalog().agents["finance"]
        self.assertTrue(card.enabled)
        self.assertIsNone(card.disabled_reason)
        self.assertIsNone(card.decided_by_ticket)
        self.assertEqual(FINANCE_BOX, set(card.boundary_tools))
        self.assertEqual(FINANCE_SKILLS, card.skills)

    def test_profile_loads_card_skills(self) -> None:
        profile = load_role_profile("finance")
        self.assertEqual({"create-skill", *FINANCE_SKILLS},
                         set(profile.skills))

    def test_index_summary_names_no_disabled_state(self) -> None:
        entry = next(item for item in load_catalog().entries
                     if item.name == "finance")
        self.assertNotIn("Disabled", entry.summary)

    def test_planner_index_lists_the_five_skills(self) -> None:
        from catalog import load_all_role_skills, skills_index_for_planner
        by_agent = load_all_role_skills(load_catalog())
        index = skills_index_for_planner(by_agent)
        for skill in FINANCE_SKILLS:
            self.assertIn(skill, index)


class SkillsOnDiskTest(unittest.TestCase):
    def test_each_skill_has_recipe_and_exact_workflow(self) -> None:
        service, _store = _service()
        for skill in FINANCE_SKILLS:
            with self.subTest(skill=skill):
                skill_dir = REPO / "skills" / "finance" / skill
                recipe = (skill_dir / "SKILL.md").read_text(
                    encoding="utf-8")
                self.assertIn("status: activa", recipe)
                self.assertIn("TASK-2026-00221", recipe)
                workflow = service.parse_workflow_text(
                    (skill_dir / "workflow.yaml").read_text(
                        encoding="utf-8"),
                    source=str(skill_dir / "workflow.yaml"))
                self.assertEqual(1, len(workflow["steps"]))
                self.assertEqual("finance", workflow["agent"])

    def test_workflows_call_their_registry_tool(self) -> None:
        service, _store = _service()
        expected = {
            "registrar-gasto": "finance.register_expense",
            "registrar-ingreso": "finance.register_income",
            "consultar-resumen": "finance.get_scope_summary",
            "regla-10": "finance.get_contribution_10_status",
            "pago-elfec": "finance.pago_elfec_house",
        }
        for skill, tool in expected.items():
            with self.subTest(skill=skill):
                workflow = service.parse_workflow_text(
                    (REPO / "skills" / "finance" / skill
                     / "workflow.yaml").read_text(encoding="utf-8"),
                    source=skill)
                self.assertEqual(tool, workflow["steps"][0]["tool"])

    def test_no_scripts_no_tests_needed(self) -> None:
        for skill in FINANCE_SKILLS:
            with self.subTest(skill=skill):
                self.assertFalse(
                    (REPO / "skills" / "finance" / skill
                     / "scripts").exists())

    def test_exposure_symlinks_resolve(self) -> None:
        import os
        for skill in FINANCE_SKILLS:
            with self.subTest(skill=skill):
                link = REPO / ".codex" / "skills" / skill
                self.assertTrue(link.is_symlink())
                self.assertEqual(
                    os.path.realpath(REPO / "skills" / "finance" / skill),
                    os.path.realpath(link))

    def test_loader_sees_exactly_the_card_skills(self) -> None:
        card = load_catalog().agents["finance"]
        on_disk = [skill.name for skill in load_role_skills(
            "finance", card.skills_dir)]
        self.assertEqual(sorted(card.skills), on_disk)

    def test_pago_elfec_stays_practice(self) -> None:
        import yaml
        workflow = yaml.safe_load(
            (REPO / "skills" / "finance" / "pago-elfec"
             / "workflow.yaml").read_text(encoding="utf-8"))
        cfg = workflow["steps"][0]["params"]["workflow"]
        self.assertTrue(cfg["stop_before_card"])
        self.assertEqual("House", cfg["expected_row_label"])


class PlannerSkillLiveTest(unittest.TestCase):
    def test_real_skill_passes(self) -> None:
        skills = {"finance": list(FINANCE_SKILLS)}
        for skill in FINANCE_SKILLS:
            with self.subTest(skill=skill):
                self.assertEqual(
                    [], _validate_skill_live("finance", skill, skills))

    def test_unknown_skill_fails(self) -> None:
        skills = {"finance": list(FINANCE_SKILLS)}
        problems = _validate_skill_live("finance", "pagar-todo", skills)
        self.assertTrue(problems)


class GateLiveTest(unittest.TestCase):
    def test_hint_proceeds(self) -> None:
        decision = resolve_agent_gate("finance", {}, None,
                                      load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("finance", decision.hint)

    def test_planner_target_proceeds(self) -> None:
        decision = resolve_agent_gate(
            None, {"target_agent": "finance"}, None, load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("finance", decision.hint)


class FinanceBoxTest(unittest.TestCase):
    def _grant(self, tools: frozenset[str]) -> TicketGrant:
        return TicketGrant(kind=TokenKind.AGENT, agent="finance",
                           ticket="TASK-2026-00221",
                           project="jardal-openceo", tools=tools)

    def test_box_comes_from_the_live_card(self) -> None:
        card = load_catalog().agents["finance"]
        self.assertEqual(FINANCE_BOX, box_for_agent(card))

    def test_money_tools_answer_inside_the_box(self) -> None:
        grant = self._grant(FINANCE_BOX)
        for tool in ("openceo_register_expense", "openceo_register_income",
                     "openceo_scope_summary",
                     "openceo_contribution_10_status"):
            with self.subTest(tool=tool):
                grant.check(tool, {"text": "x", "openceo_ref": "r1"})
        short = self._grant(frozenset({"openceo_get_ticket"}))
        with self.assertRaises(McpDenied):
            short.check("openceo_register_expense",
                        {"text": "x", "openceo_ref": "r1"})

    def test_elfec_binds_own_ticket_and_project(self) -> None:
        grant = self._grant(FINANCE_BOX)
        grant.check("openceo_elfec_house_payment", {
            "ticket": "TASK-2026-00221", "project": "jardal-openceo"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_elfec_house_payment", {
                "ticket": "TASK-2026-00001", "project": "jardal-openceo"})

    def test_foreign_tools_refused(self) -> None:
        grant = self._grant(FINANCE_BOX)
        for foreign in ("manage_dns_record", "openceo_send_email",
                        "openceo_transition_ticket",
                        "openceo_ensure_erpnext_project"):
            with self.subTest(tool=foreign), self.assertRaises(McpDenied):
                grant.check(foreign, {"ticket": "TASK-2026-00221",
                                      "project": "jardal-openceo"})

    def test_finance_badge_mints(self) -> None:
        class _Store:
            async def save(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _mint() -> str:
            issuer = TokenIssuer(_Store(), load_catalog().agents)  # type: ignore[arg-type]
            return await issuer.issue_agent(
                "finance", "TASK-2026-00221", "jardal-openceo")

        badge = asyncio.run(_mint())
        self.assertTrue(badge)


class ExactSkillLiveTest(unittest.TestCase):
    def _wiring(self, service: WorkflowService) -> GenericAgentWiring:
        return GenericAgentWiring(
            settings=_settings(), repo=None,
            workflow_service=service,
            executor_factory=_raising_factory,
            badge_provider_factory=lambda a, t, p: MagicMock(),
            catalog=load_catalog())

    def test_registrar_gasto_runs_exact_without_llm(self) -> None:
        from workflow_service import WorkflowToolRegistry
        answer = {"ok": True, "duplicate": False, "ref": "r",
                  "journal_entry": "JE-1", "docstatus": 1,
                  "summary": "Registré gasto de 50 Bs en Carne"}
        # The registry binds its tools at build: patch first, then build.
        tool = AsyncMock(return_value=answer)
        with patch.object(WorkflowToolRegistry,
                          "_finance_register_expense", new=tool):
            service, _store = _service()
            out = asyncio.run(run_generic_agent(
                "finance", _live_task(params={**MIRROR,
                                             "skill": "registrar-gasto"}),
                self._wiring(service)))
        tool.assert_awaited_once()
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        self.assertIn("hecho", out["result_comment"])
        verdict = out["params"]["agent_result"]
        self.assertEqual("hecho", verdict["resultado"])

    def test_no_amount_fails_invalid_input_on_the_record(self) -> None:
        service, store = _service()
        task = _live_task(
            title="Registra gasto carne",
            description="registra gasto carne",
            params={**MIRROR, "skill": "registrar-gasto"})
        out = asyncio.run(run_generic_agent(
            "finance", task, self._wiring(service)))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        self.assertEqual("bloqueado",
                         out["params"]["agent_result"]["resultado"])
        self.assertIn("invalid_input", out["error"])
        run = asyncio.run(store.latest_workflow_run_for_task(
            task["task_id"]))
        assert run is not None
        refused = [step for step in run["steps"]
                   if isinstance(step.get("output"), dict)]
        self.assertEqual(1, len(refused))
        self.assertEqual("invalid_input",
                         refused[0]["output"]["status"])
        self.assertIn("monto", refused[0]["output"]["summary"])

    def test_regla_10_and_resumen_run_exact(self) -> None:
        from workflow_service import WorkflowToolRegistry
        answers = {
            "_finance_get_scope_summary": {
                "ok": True, "summary": "Gastamos 100 Bs este mes"},
            "_finance_get_contribution_10_status": {
                "ok": True, "summary": "El diezmo va al día"},
        }
        for attr, answer in answers.items():
            skill = ("consultar-resumen" if "scope" in attr
                     else "regla-10")
            tool = AsyncMock(return_value=answer)
            with self.subTest(skill=skill), patch.object(
                    WorkflowToolRegistry, attr, new=tool):
                service, _store = _service()
                out = asyncio.run(run_generic_agent(
                    "finance", _live_task(params={**MIRROR,
                                                 "skill": skill}),
                    self._wiring(service)))
            tool.assert_awaited_once()
            self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
            self.assertIn("hecho", out["result_comment"])

    def test_resumen_derives_bounds_from_text(self) -> None:
        from agents.finance import scope_summary as scope_mod
        from workflow_service import WorkflowToolRegistry
        service, _store = _service()
        seen: dict[str, Any] = {}

        async def _fake_summary(client: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            summary = MagicMock()
            summary.to_dict.return_value = {"total": "0"}
            return summary

        class _Client:
            def __init__(self, settings: Any) -> None:
                del settings

            async def aclose(self) -> None:
                return None

        text = "cuánto gastamos este mes"
        with patch.object(scope_mod, "get_scope_summary",
                          new=_fake_summary), \
                patch.object(scope_mod, "build_spoken_summary",
                             return_value="spoken"), \
                patch("tools.erpnext_client.ERPNextClient", _Client):
            out = asyncio.run(service._tools.call(
                "finance.get_scope_summary", {"text": text}, {}))
        self.assertTrue(out["ok"])
        want_from, want_to = scope_mod.period_bounds(text)
        self.assertEqual(want_from, seen["date_from"])
        self.assertEqual(want_to, seen["date_to"])

    def test_resumen_explicit_bounds_win(self) -> None:
        from agents.finance import scope_summary as scope_mod
        from workflow_service import WorkflowToolRegistry
        service, _store = _service()
        seen: dict[str, Any] = {}

        async def _fake_summary(client: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            summary = MagicMock()
            summary.to_dict.return_value = {"total": "0"}
            return summary

        class _Client:
            def __init__(self, settings: Any) -> None:
                del settings

            async def aclose(self) -> None:
                return None

        with patch.object(scope_mod, "get_scope_summary",
                          new=_fake_summary), \
                patch.object(scope_mod, "build_spoken_summary",
                             return_value="spoken"), \
                patch("tools.erpnext_client.ERPNextClient", _Client):
            out = asyncio.run(service._tools.call(
                "finance.get_scope_summary",
                {"text": "cuánto gastamos este mes",
                 "date_from": "2026-01-01", "date_to": "2026-01-31"},
                {}))
        self.assertTrue(out["ok"])
        self.assertEqual("2026-01-01", seen["date_from"])
        self.assertEqual("2026-01-31", seen["date_to"])

    def test_pago_elfec_parks_with_question_and_params(self) -> None:
        service, _store = _service()
        fake = MagicMock()
        fake.tool_elfec_pay = AsyncMock(return_value=pending_lock_result(
            tool="elfec", question_task_id="q4",
            waiting_reason="tool_lock:elfec", detail="hold"))
        service.set_task_service(fake)
        out = asyncio.run(run_generic_agent(
            "finance", _live_task(params={**MIRROR, "skill": "pago-elfec",
                                         "target_agent": "finance",
                                         "decided_by": "planner"}),
            self._wiring(service)))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertEqual("tool_lock:elfec", out["waiting_reason"])
        self.assertEqual("blocked_tool",
                         out["params"]["_workflow"]["status"])
        self.assertEqual("q4", out["params"]["owner_question_id"])
        self.assertEqual("finance", out["params"]["target_agent"])
        self.assertEqual("planner", out["params"]["decided_by"])

    def test_elfec_denied_is_bloqueado(self) -> None:
        service, _store = _service()
        fake = MagicMock()
        fake.tool_elfec_pay = AsyncMock(return_value={
            "ok": False, "status": "denied", "reason": "no"})
        service.set_task_service(fake)
        out = asyncio.run(run_generic_agent(
            "finance", _live_task(params={**MIRROR, "skill": "pago-elfec"}),
            self._wiring(service)))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        self.assertEqual("bloqueado",
                         out["params"]["agent_result"]["resultado"])

    def test_unknown_skill_waits(self) -> None:
        service, _store = _service()
        out = asyncio.run(run_generic_agent(
            "finance", _live_task(params={**MIRROR, "skill": "pagar-todo"}),
            self._wiring(service)))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("pagar-todo", out["waiting_reason"])


class GraphRemovalTest(unittest.TestCase):
    def test_wired_agents_are_the_two_nodes(self) -> None:
        self.assertEqual({"dev", "pm"}, set(WIRED_AGENTS))

    def test_finance_routes_to_the_generic_node(self) -> None:
        graph = OpenCEOGraph(None, None, None)
        self.assertEqual(
            "generic_agent",
            graph._after_ceo_route({"selected_agent": "finance"}))

    def test_finance_hint_waits_without_wiring(self) -> None:
        from unittest import mock
        loader = mock.MagicMock()
        loader.load_project.return_value = {}
        graph = OpenCEOGraph(None, loader, None)
        out = asyncio.run(graph.execute(
            _live_task(), agent_hint="finance"))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("config:", out["waiting_reason"])

    def test_finance_failed_keeps_no_legacy_verdict(self) -> None:
        out = OpenCEOGraph._attach_legacy_agent_result(
            {"params": {}},
            {"status": TaskStatus.FAILED.value,
             "selected_agent": "finance",
             "error": "boom", "params": {}})
        self.assertNotIn("agent_result", out["params"])

    def test_retired_modules_stay_gone(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            import agents.finance.intent  # noqa: F401
        with self.assertRaises(ModuleNotFoundError):
            import agents.finance.session  # noqa: F401


class VoiceSkillTest(unittest.TestCase):
    def _wiring(self, service: WorkflowService,
                store: _RunStore) -> FinanceVoiceWiring:
        return FinanceVoiceWiring(settings=_settings(),
                                  workflow_service=service, repo=store,
                                  catalog=load_catalog())

    def test_ok_returns_the_spoken_summary(self) -> None:
        from workflow_service import WorkflowToolRegistry
        answer = {"ok": True, "duplicate": False, "ref": "r",
                  "journal_entry": "JE-1", "docstatus": 1,
                  "summary": "Registré gasto de 50 Bs en Carne"}
        with patch.object(WorkflowToolRegistry,
                          "_finance_register_expense",
                          new=AsyncMock(return_value=answer)):
            service, store = _service()
            reply = asyncio.run(run_finance_skill(
                "registrar-gasto",
                "registra gasto personal carne 50 bolivianos",
                self._wiring(service, store)))
        self.assertEqual("Registré gasto de 50 Bs en Carne", reply)

    def test_no_amount_asks_for_it(self) -> None:
        service, store = _service()
        reply = asyncio.run(run_finance_skill(
            "registrar-gasto", "registra gasto carne",
            self._wiring(service, store)))
        assert reply is not None
        self.assertIn("monto", reply)

    def test_lock_park_speaks_pending(self) -> None:
        service, store = _service()
        fake = MagicMock()
        fake.tool_elfec_pay = AsyncMock(return_value=pending_lock_result(
            tool="elfec", question_task_id="q4",
            waiting_reason="tool_lock:elfec", detail="hold"))
        service.set_task_service(fake)
        reply = asyncio.run(run_finance_skill(
            "pago-elfec", "paga la luz de la casa",
            self._wiring(service, store)))
        assert reply is not None
        self.assertIn("pendiente", reply.lower())

    def test_unknown_skill_refuses(self) -> None:
        service, store = _service()
        reply = asyncio.run(run_finance_skill(
            "pagar-todo", "paga todo",
            self._wiring(service, store)))
        self.assertIsNone(reply)

    def test_disabled_finance_refuses(self) -> None:
        service, store = _service()
        catalog = load_catalog()
        card = catalog.agents["finance"]
        catalog.agents["finance"] = card.model_copy(
            update={"enabled": False})
        wiring = FinanceVoiceWiring(settings=_settings(),
                                    workflow_service=service, repo=store,
                                    catalog=catalog)
        reply = asyncio.run(run_finance_skill(
            "registrar-gasto", "registra gasto carne 50 bs", wiring))
        self.assertIsNone(reply)


class FirstLayerFinanceTest(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, intent_json: dict[str, Any],
                 handler: Any) -> Any:
        from first_layer import FirstLayerGateway

        gateway = FirstLayerGateway.__new__(FirstLayerGateway)
        gateway.horizon = None
        gateway._finance_handler = handler

        class _Llm:
            async def ainvoke(self, _messages: Any) -> Any:
                return SimpleNamespace(content=json.dumps(intent_json))

        gateway._llm = _Llm()
        gateway.dev_session_service = SimpleNamespace(_tmux=None)
        gateway.task_service = MagicMock()

        async def _ctx(_user_id: str) -> dict[str, Any]:
            return {"selected_project": "proj",
                    "projects": [{"name": "proj"}],
                    "active_ticket": None, "open_tasks": [],
                    "open_count": 0,
                    "active_session": None, "all_tmux": []}

        gateway.gather_context = _ctx  # type: ignore[method-assign]
        return gateway

    async def test_valid_skill_calls_handler(self) -> None:
        handler = AsyncMock(return_value="Registré gasto de 50 Bs")
        gateway = self._gateway(
            {"intent": "finance_skill", "skill": "registrar-gasto",
             "dictation": "registra gasto carne 50 bs",
             "spoken_reply": ""},
            handler)
        handled, reply = await gateway.process_turn(
            "owner", "registra gasto carne 50 bs", SimpleNamespace())
        self.assertTrue(handled)
        self.assertEqual("Registré gasto de 50 Bs", reply)
        handler.assert_awaited_once_with("registrar-gasto",
                                         "registra gasto carne 50 bs")

    async def test_unknown_skill_declines_without_handler(self) -> None:
        handler = AsyncMock(return_value="never")
        gateway = self._gateway(
            {"intent": "finance_skill", "skill": "pagar-todo",
             "dictation": "paga todo", "spoken_reply": ""},
            handler)
        handled, reply = await gateway.process_turn(
            "owner", "paga todo", SimpleNamespace())
        self.assertTrue(handled)
        self.assertIn("No reconocí", reply)
        handler.assert_not_called()

    async def test_missing_handler_declines(self) -> None:
        gateway = self._gateway(
            {"intent": "finance_skill", "skill": "registrar-gasto",
             "dictation": "registra gasto carne 50 bs",
             "spoken_reply": ""},
            None)
        handled, reply = await gateway.process_turn(
            "owner", "registra gasto carne 50 bs", SimpleNamespace())
        self.assertTrue(handled)
        self.assertIn("No reconocí", reply)

    async def test_handler_failure_is_friendly(self) -> None:
        handler = AsyncMock(side_effect=RuntimeError("boom"))
        gateway = self._gateway(
            {"intent": "finance_skill", "skill": "registrar-gasto",
             "dictation": "registra gasto carne 50 bs",
             "spoken_reply": ""},
            handler)
        handled, reply = await gateway.process_turn(
            "owner", "registra gasto carne 50 bs", SimpleNamespace())
        self.assertTrue(handled)
        self.assertIn("No pude resolver eso con finanzas", reply)

    async def test_dictation_falls_back_to_text(self) -> None:
        handler = AsyncMock(return_value="ok")
        gateway = self._gateway(
            {"intent": "finance_skill", "skill": "regla-10",
             "dictation": None, "spoken_reply": ""},
            handler)
        handled, _reply = await gateway.process_turn(
            "owner", "cómo vamos con el diezmo", SimpleNamespace())
        self.assertTrue(handled)
        handler.assert_awaited_once_with("regla-10",
                                         "cómo vamos con el diezmo")


class HorizonVetoTest(unittest.TestCase):
    def test_money_writes_skip_horizon(self) -> None:
        from horizon.classify import classify
        for text in ("registra gasto personal carne 50 bolivianos",
                     "registra ingreso sueldo 6200 bolivianos",
                     "registra mi diezmo 500 bs",
                     "me pagaron 500 bs del contrato"):
            with self.subTest(text=text):
                self.assertIsNone(classify(text))

    def test_queries_reach_the_chat_layer(self) -> None:
        from horizon.classify import classify
        self.assertIsNone(classify("cómo vamos con el diezmo"))
        self.assertIsNone(classify("cuánto gastamos este mes"))

    def test_plain_notes_still_capture(self) -> None:
        from horizon.classify import INTENT_CAPTURE, classify
        self.assertEqual(INTENT_CAPTURE, classify("registra comprar pan"))

    def test_personal_phrases_stay_personal(self) -> None:
        from horizon.classify import INTENT_PROGRESS, classify
        self.assertIsNotNone(classify("hoy solo tengo 15 minutos"))
        self.assertEqual(INTENT_PROGRESS,
                         classify("me pagaron el remoto"))


class ConfigRetiredTest(unittest.TestCase):
    def test_no_finance_workflows_in_project_or_defaults(self) -> None:
        import tempfile

        import yaml
        from config_loader import ConfigLoader
        project = yaml.safe_load(
            (REPO / "projects" / "jardal-openceo"
             / "project.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("finance", project)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ConfigLoader(
                Settings(_env_file=None, PROJECTS_DIR=tmp)
            ).load_project(None)
        self.assertNotIn("finance", cfg)
