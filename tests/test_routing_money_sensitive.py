"""Per-project money-sensitive routing guide (TASK-2026-00161).

Contract for ``dev.routing`` in project.yaml: an optional money-sensitive
characteristic that GUIDES (never forces) DEV tier selection in one
project only. Loose tickets gain an extra Tier S money-domain signal
(conjunction of domain context + money at stake, so bare ambiguous
words never fire); objective children gain a PROJECT TIER GUIDE section
in the planner prompt. An explicit ``coding_tool`` always wins, A/B/C
stay reachable with their normal justification, and other projects are
unchanged. No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from test_dispatcher import (  # noqa: E402
    _NOW,
    _dispatcher,
    _drain,
    _FakeRepo,
    _FakeService,
    _ticket,
)

from agents.dev.loose_tier import (  # noqa: E402
    ASSIGNED_BY,
    classify_loose_tier,
    project_money_routing,
)
from agents.dev.session import (  # noqa: E402
    CodingStepResult,
    DevExecutionOutput,
    DevSessionManager,
)
from config_loader import ConfigLoader  # noqa: E402
from objectives.engine import (  # noqa: E402
    _planner_prompt,
    routing_guide_section,
)
from objectives.service import ObjectiveService  # noqa: E402
from settings import Settings  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402

ROUTING = {"money_sensitive": True}

#: Trading-money texts with NO base S/A/B signal: S only via the guide.
MONEY_TEXTS = [
    ("El bot ejecuta órdenes live con capital real", ""),
    ("Stop loss no salta en el exchange", ""),
    ("Revisar el cálculo del riesgo por posición en el bot", ""),
    ("Apalancamiento x10 en la cuenta live de trading", ""),
    ("Broker rechaza órdenes de mercado", ""),
    ("Slippage alto en las posiciones del mercado", ""),
    ("Live trading bot mishandles market orders", ""),
]

#: Ambiguous words without domain context: never S, even with the guide.
BARE_WORDS = [
    ("Poner en orden el backlog", ""),
    ("El problema real es el timeout", ""),
    ("Riesgo de retraso en la entrega", ""),
    ("Actualizar la cifra de capital en el informe", ""),
    ("Reordenar las posiciones del menú lateral", ""),
    ("Añadir un bot de avisos al canal", ""),
    ("Actualizar la lista de exchanges soportados", ""),
]


def _decision(title: str, description: str = "",
              routing: dict[str, Any] | None = None):
    return classify_loose_tier(title, description, [], project_routing=routing)


class MoneyDomainClassifierTests(unittest.TestCase):
    def test_trading_money_selects_tier_s_with_routing(self) -> None:
        for title, description in MONEY_TEXTS:
            with self.subTest(title=title):
                decision = _decision(title, description, ROUTING)
                self.assertEqual(decision.tier, CodingTool.TIER_S, title)
                self.assertIn("19", decision.reason, title)
                self.assertTrue(decision.signals, title)
                self.assertTrue(decision.signals[0].startswith(
                    "s_money_domain"), title)
                self.assertTrue(
                    (decision.why_consensus_is_needed or "").strip(), title)
                self.assertTrue(
                    (decision.why_single_session_is_insufficient or "").strip(),
                    title)

    def test_same_text_without_routing_is_not_tier_s(self) -> None:
        for title, description in MONEY_TEXTS:
            with self.subTest(title=title):
                decision = _decision(title, description, None)
                self.assertNotEqual(decision.tier, CodingTool.TIER_S, title)

    def test_bare_words_without_domain_stay_tier_c(self) -> None:
        for title, description in BARE_WORDS:
            with self.subTest(title=title):
                for routing in (ROUTING, None):
                    decision = _decision(title, description, routing)
                    self.assertEqual(decision.tier, CodingTool.TIER_C,
                                     f"{title} routing={routing is not None}")

    def test_custom_vocabulary_extends_signals(self) -> None:
        routing = {"money_sensitive": True,
                   "money_domain": ["quiniela"],
                   "money_stake": ["apuesta", "boleto premiado"]}
        for title in ("Apuesta fuerte en la quiniela del viernes",
                      "Revisar el boleto premiado de la quiniela"):
            with self.subTest(title=title):
                decision = _decision(title, "", routing)
                self.assertEqual(decision.tier, CodingTool.TIER_S, title)
                self.assertIn("s_money_domain", decision.signals[0], title)
                self.assertEqual(
                    _decision(title, "", None).tier, CodingTool.TIER_C, title)

    def test_custom_vocabulary_ignored_without_flag(self) -> None:
        title = "Apuesta fuerte en la quiniela del viernes"
        for routing in ({"money_domain": ["quiniela"],
                         "money_stake": ["apuesta"]},
                        {"money_sensitive": False,
                         "money_domain": ["quiniela"],
                         "money_stake": ["apuesta"]}):
            self.assertEqual(
                _decision(title, "", routing).tier, CodingTool.TIER_C,
                str(routing))

    def test_custom_entries_are_literal_not_regex(self) -> None:
        routing = {"money_sensitive": True, "money_stake": ["c++"]}
        decision = _decision("Reescribir el bot de c++", "", routing)
        self.assertEqual(decision.tier, CodingTool.TIER_S)

    def test_malformed_routing_never_raises(self) -> None:
        title, description = MONEY_TEXTS[0]
        cases: list[Any] = [
            "nope", 42, ["money_sensitive"],
            {"money_sensitive": "yes"},
            {"money_sensitive": 1},
            {"money_sensitive": True, "money_domain": "bot"},
            {"money_sensitive": True, "money_stake": [123, ""]},
            {"money_sensitive": True, "money_domain": ["ok"] * 200},
        ]
        for routing in cases:
            with self.subTest(routing=str(routing)[:40]):
                decision = classify_loose_tier(
                    title, description, [], project_routing=routing)
                self.assertIn(decision.tier,
                              (CodingTool.TIER_S, CodingTool.TIER_C))

    def test_all_four_tiers_reachable_with_routing(self) -> None:
        cases = [
            ("Fix typo in README", "", CodingTool.TIER_C),
            ("Semantic refactor of the reporting module", "",
             CodingTool.TIER_B),
            ("Investigar la causa desconocida del timeout", "",
             CodingTool.TIER_A),
            (MONEY_TEXTS[0][0], "", CodingTool.TIER_S),
        ]
        for title, description, expected in cases:
            with self.subTest(title=title):
                decision = _decision(title, description, ROUTING)
                self.assertEqual(decision.tier, expected, title)

    def test_project_money_routing_normalizes_project_config(self) -> None:
        self.assertEqual(project_money_routing({}), {})
        self.assertEqual(project_money_routing(None), {})
        self.assertEqual(
            project_money_routing({"dev": {"routing": ROUTING}}), ROUTING)
        self.assertEqual(
            project_money_routing({"dev": {"routing": {"money_domain": ["x"]}}}),
            {})
        normalized = project_money_routing({"dev": {"routing": {
            "money_sensitive": True, "money_domain": ["  ", "bot"],
            "money_stake": ["caja"]}}})
        self.assertEqual(normalized, {
            "money_sensitive": True, "money_domain": ["bot"],
            "money_stake": ["caja"]})


class MoneyRoutingDispatchTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> DevSessionManager:
        loader = MagicMock()
        loader.validate_project = MagicMock(return_value=[])
        manager = DevSessionManager(MagicMock(), loader, repo=None)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        return manager

    def _project_config(self, routing: dict[str, Any] | None):
        return {"dev": {"routing": routing}} if routing is not None else {}

    async def test_money_routing_selects_tier_at_dispatch(self) -> None:
        manager = self._manager()
        manager._run_tier_s = AsyncMock(return_value=DevExecutionOutput(
            status=TaskStatus.COMPLETED))
        output = await manager.run(
            {"task_id": "task-1", "title": MONEY_TEXTS[0][0],
             "description": ""},
            self._project_config(ROUTING),
        )
        manager._run_tier_s.assert_awaited_once()
        manager._step_code.assert_not_called()
        assert output.params is not None
        self.assertTrue(output.params["tier_signals"][0].startswith(
            "s_money_domain"))
        self.assertIn("19", output.params["tier_reason"])
        self.assertEqual(output.params["tier_assigned_by"], ASSIGNED_BY)
        self.assertTrue(output.params["why_consensus_is_needed"].strip())
        self.assertTrue(
            output.params["why_single_session_is_insufficient"].strip())

    async def test_same_text_without_routing_stays_c_at_dispatch(self) -> None:
        output = await self._manager().run(
            {"task_id": "task-1", "title": MONEY_TEXTS[0][0],
             "description": ""},
            self._project_config(None),
        )
        self.assertEqual(output.coding_tool, CodingTool.TIER_C)

    async def test_explicit_field_beats_money_guide(self) -> None:
        output = await self._manager().run(
            {"task_id": "task-1", "title": MONEY_TEXTS[0][0],
             "description": "", "coding_tool": "tier_c"},
            self._project_config(ROUTING),
        )
        self.assertEqual(output.coding_tool, CodingTool.TIER_C)

    def test_dev_objective_env_carries_routing(self) -> None:
        manager = self._manager()
        env = manager._objective_env_for(
            self._project_config(ROUTING), Path("/tmp"),
            object(), object(), None)
        self.assertEqual(env.project_routing, ROUTING)
        bare = manager._objective_env_for(
            self._project_config(None), Path("/tmp"),
            object(), object(), None)
        self.assertEqual(bare.project_routing, {})


class _RecordingMonitor:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def admission_for_ticket(self, ticket: dict[str, Any],
                                   active_tickets=None):
        self.seen.append(dict(ticket))
        return (True, None, None)


class DispatcherPeekRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def _peeked_tier(self, project_config: dict[str, Any]) -> str:
        repo = _FakeRepo([
            _ticket("loose-1", project="jardal-trading",
                    title=MONEY_TEXTS[0][0], description="",
                    coding_tool=None, objective_id=None),
        ])
        service = _FakeService(repo)
        service._config_loader = SimpleNamespace(
            load_project=lambda project: project_config)
        launched: list[str] = []
        monitor = _RecordingMonitor()
        dispatcher = _dispatcher(repo, service, launched, monitor=monitor)
        await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, ["loose-1"])
        self.assertEqual(len(monitor.seen), 1)
        return str(monitor.seen[0].get("coding_tool"))

    async def test_peek_honors_money_routing(self) -> None:
        tier = await self._peeked_tier({"dev": {"routing": ROUTING}})
        self.assertEqual(tier, CodingTool.TIER_S.value)

    async def test_peek_without_routing_stays_default(self) -> None:
        tier = await self._peeked_tier({})
        self.assertEqual(tier, CodingTool.TIER_C.value)

    async def test_peek_without_loader_stays_default(self) -> None:
        repo = _FakeRepo([
            _ticket("loose-1", project="jardal-trading",
                    title=MONEY_TEXTS[0][0], description="",
                    coding_tool=None, objective_id=None),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        monitor = _RecordingMonitor()
        dispatcher = _dispatcher(repo, service, launched, monitor=monitor)
        await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(monitor.seen[0].get("coding_tool"),
                         CodingTool.TIER_C.value)


class PlannerRoutingGuideTests(unittest.TestCase):
    def test_guide_empty_without_routing(self) -> None:
        for routing in (None, {}, {"money_sensitive": False},
                        {"money_domain": ["bot"]}, "nope", 42):
            self.assertEqual(routing_guide_section("proj", routing), "",
                             str(routing)[:40])

    def test_guide_prefers_s_without_forcing(self) -> None:
        guide = routing_guide_section("jardal-trading", ROUTING)
        self.assertIn("PROJECT TIER GUIDE", guide)
        self.assertIn("jardal-trading", guide)
        self.assertIn("money-sensitive", guide)
        self.assertIn("guidance, not a rule", guide)
        self.assertIn("prefer tier_s", guide)
        self.assertIn("why_consensus_is_needed", guide)
        self.assertIn("why_single_session_is_insufficient", guide)
        for tier in ("tier_a", "tier_b", "tier_c"):
            self.assertIn(tier, guide)
        self.assertIn("why_not_c", guide)
        self.assertIn("explicit coding_tool", guide)
        self.assertIn("never collapses", guide)
        self.assertNotIn("cheap", guide)

    def test_guide_names_domain_vocabulary(self) -> None:
        guide = routing_guide_section("jardal-trading", ROUTING)
        for word in ("exchange", "orden", "live", "capital", "bot"):
            self.assertIn(word, guide)
        self.assertIn('"stop loss"', guide)
        custom = routing_guide_section(
            "proj", {"money_sensitive": True,
                     "money_domain": ["quiniela"],
                     "money_stake": ["boleto premiado"]})
        self.assertIn("quiniela", custom)
        self.assertIn('"boleto premiado"', custom)

    def test_prompt_carries_guide_only_when_given(self) -> None:
        base = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "",
            [], blocked_tickets=[], agents_md="REGISTERED AGENTS",
            policy_text="POLICY", handoffs=[], roadmap=[])
        self.assertNotIn("PROJECT TIER GUIDE", base)
        guide = routing_guide_section("jardal-trading", ROUTING)
        with_guide = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "",
            [], blocked_tickets=[], agents_md="REGISTERED AGENTS",
            policy_text="POLICY", handoffs=[], roadmap=[],
            routing_guide=guide)
        self.assertIn("PROJECT TIER GUIDE", with_guide)
        self.assertIn("prefer tier_s", with_guide)


class RoutingConfigValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _validate(self, body: str) -> list[str]:
        path = self.root / "proj"
        path.mkdir(parents=True, exist_ok=True)
        (path / "project.yaml").write_text(body, encoding="utf-8")
        loader = ConfigLoader(Settings(_env_file=None,
                                        PROJECTS_DIR=str(self.root)))
        return loader.validate_project("proj")

    def test_well_formed_routing_is_valid(self) -> None:
        errors = self._validate(
            "dev:\n"
            "  routing:\n"
            "    money_sensitive: true\n"
            "    money_domain: [quiniela]\n"
            "    money_stake: [apuesta, boleto premiado]\n"
        )
        self.assertEqual([err for err in errors if "routing" in err], [])

    def test_absent_routing_is_valid(self) -> None:
        errors = self._validate("dev:\n  default_provider: codex\n")
        self.assertEqual(errors, [])

    def test_malformed_routing_is_rejected(self) -> None:
        cases = [
            ("dev:\n  routing: yes\n", "dev.routing"),
            ("dev:\n  routing:\n    money_sensitive: yes-please\n",
             "dev.routing.money_sensitive"),
            ("dev:\n  routing:\n    money_sensitive: 1\n",
             "dev.routing.money_sensitive"),
            ("dev:\n  routing:\n    money_domain: bot\n",
             "dev.routing.money_domain"),
            ("dev:\n  routing:\n    money_domain: [bot, 42]\n",
             "dev.routing.money_domain"),
            ("dev:\n  routing:\n    money_stake: [apuesta, '']\n",
             "dev.routing.money_stake"),
            ("dev:\n  routing:\n    money_stake: [ok]\n"
             "    money_domain: [" + ", ".join(["w"] * 65) + "]\n",
             "dev.routing.money_domain"),
            ("dev:\n  routing:\n    money_stake: [" + "x" * 81 + "]\n",
             "dev.routing.money_stake"),
        ]
        for body, needle in cases:
            with self.subTest(body=body.replace("\n", "|")[:60]):
                errors = self._validate(body)
                self.assertTrue(any(needle in err for err in errors),
                                f"{needle} not in {errors}")


class ObjectiveServiceRoutingTests(unittest.TestCase):
    def _service(self, project_config: dict[str, Any]) -> ObjectiveService:
        loader = SimpleNamespace(
            load_project=lambda project: project_config)
        settings = SimpleNamespace(TIER_S_OBJECTIVE_CHILD_AUTORUN=True)
        return ObjectiveService(repo=None, task_service=None,
                                config_loader=loader, settings=settings)

    def test_env_carries_project_routing(self) -> None:
        env = self._service(
            {"dev": {"routing": ROUTING},
             "project": {"repos": []}}).env_for("jardal-trading")
        self.assertEqual(env.project_routing, ROUTING)

    def test_env_routing_empty_without_characteristic(self) -> None:
        env = self._service({"project": {"repos": []}}).env_for("proj")
        self.assertEqual(env.project_routing, {})


if __name__ == "__main__":
    unittest.main()
