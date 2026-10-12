"""Per-project money-sensitive routing guide (TASK-2026-00161).

Contract for ``dev.routing`` in project.yaml: an optional money-sensitive
characteristic that GUIDES (never forces) DEV tier selection in one
project only, as planner context. Since TASK-2026-00204 the guide no
longer feeds any keyword classifier: objective and pedido planner
prompts carry a PROJECT TIER GUIDE section, dispatch admits
tier-unaware, and an explicit ``coding_tool`` always wins. Other
projects are unchanged. No network, no CLI, no LLM.
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

from agents.dev.session import (  # noqa: E402
    CodingStepResult,
    DevSessionManager,
)
from config_loader import ConfigLoader  # noqa: E402
from objectives.engine import _planner_prompt  # noqa: E402
from objectives.money_guide import (  # noqa: E402
    project_money_routing,
    routing_guide_section,
)
from objectives.service import ObjectiveService  # noqa: E402
from settings import Settings  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402

ROUTING = {"money_sensitive": True}


class MoneyGuideNormalizeTests(unittest.TestCase):
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

    def test_malformed_routing_normalizes_without_raising(self) -> None:
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
                normalized = project_money_routing(
                    {"dev": {"routing": routing}})
                self.assertIsInstance(normalized, dict)
                if normalized:
                    self.assertTrue(normalized["money_sensitive"])

    def test_custom_vocabulary_ignored_without_flag(self) -> None:
        for routing in ({"money_domain": ["quiniela"],
                         "money_stake": ["apuesta"]},
                        {"money_sensitive": False,
                         "money_domain": ["quiniela"],
                         "money_stake": ["apuesta"]}):
            self.assertEqual(
                project_money_routing({"dev": {"routing": routing}}), {},
                str(routing))


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

    async def test_explicit_field_beats_money_guide(self) -> None:
        output = await self._manager().run(
            {"task_id": "task-1", "title": "El bot ejecuta órdenes live",
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


class DispatcherNoPeekTests(unittest.IsolatedAsyncioTestCase):
    async def _admission_seen(self) -> dict[str, Any]:
        repo = _FakeRepo([
            _ticket("loose-1", project="jardal-trading",
                    title="El bot ejecuta órdenes live con capital real",
                    description="", coding_tool=None, objective_id=None),
        ])
        service = _FakeService(repo)
        launched: list[str] = []
        monitor = _RecordingMonitor()
        dispatcher = _dispatcher(repo, service, launched, monitor=monitor)
        await dispatcher.dispatch_cycle(now=_NOW)
        await _drain()
        self.assertEqual(launched, ["loose-1"])
        self.assertEqual(len(monitor.seen), 1)
        return monitor.seen[0]

    async def test_undecided_launches_tier_unaware(self) -> None:
        # Deliberate contract change (TASK-2026-00204): no keyword peek,
        # whatever the text says. The run pre-stage plans the pedido.
        seen = await self._admission_seen()
        self.assertIsNone(seen.get("coding_tool"))


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
