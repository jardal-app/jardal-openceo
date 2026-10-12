"""One catalog for router, graph, planner and dispatch (TASK-2026-00195).

The planner registry derives from the single catalog; the enum, the router
aliases and the graph wiring validate against it at boot, fail-closed. An
unknown or disabled agent waits with ``routing_missing`` and its reason:
nothing reclassifies silently.

No network, no CLI, no LLM.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    AgentCard,
    Catalog,
    IndexEntry,
    StaffRoster,
    find_consumer_mismatches,
    load_catalog,
)
from config_loader import ConfigLoader  # noqa: E402
from graph import (  # noqa: E402
    WIRED_AGENTS,
    OpenCEOGraph,
    resolve_agent_gate,
)
from router import CEORouter  # noqa: E402
from state import AgentName, TaskStatus  # noqa: E402
from task_service import wait_comment_text  # noqa: E402


def planner_registry() -> dict[str, Any]:
    # The global registry (no project overlay): _merge_with_defaults is
    # static, no settings needed.
    cfg = ConfigLoader._merge_with_defaults("", {})
    return dict(cfg["objective_planner"]["agents"])


def tiny_catalog(specs: dict[str, dict[str, Any]]) -> Catalog:
    """In-memory catalog: name -> {enabled, reason?, type?}."""
    entries = [
        IndexEntry(name=name, summary=f"{name} does things, not others.",
                   card=f"agents/{name}.yaml")
        for name in specs
    ]
    agents = {}
    for name, spec in specs.items():
        enabled = bool(spec.get("enabled", True))
        agents[name] = AgentCard(
            name=name, type=spec.get("type", "ai"),
            engine=None if spec.get("type") == "human" else "muse-code",
            profile=None, boundary_tools=[],
            skills_dir=f"skills/{name}",
            skills=(["decidir", "aprobar", "responder", "hacer"]
                    if name == "owner" else []),
            inputs=[], outputs=[], enabled=enabled,
            disabled_reason=None if enabled else spec.get("reason", "off"),
            decided_by_ticket=(None if enabled
                               else spec.get("ticket", "TASK-2026-00209")))
    return Catalog(version=1, entries=entries, agents=agents,
                   staff=StaffRoster())


class ConsumerConsistencyTest(unittest.TestCase):
    """The four views match the shipped catalog (acceptance)."""

    def test_real_views_match(self):
        catalog = load_catalog()
        mismatches = find_consumer_mismatches(
            [member.value for member in AgentName],
            [member.value for member in CEORouter.AGENT_ALIASES.values()],
            planner_registry(),
            WIRED_AGENTS,
            catalog)
        self.assertEqual([], mismatches)

    def test_enum_name_outside_catalog_fails(self):
        catalog = tiny_catalog({"dev": {"enabled": True}})
        mismatches = find_consumer_mismatches(
            ["dev", "ghost"], ["dev"],
            {"dev": {"enabled": True}}, ["dev"], catalog)
        self.assertTrue(any("ghost" in line for line in mismatches))

    def test_alias_value_outside_catalog_fails(self):
        catalog = tiny_catalog({"dev": {"enabled": True}})
        mismatches = find_consumer_mismatches(
            ["dev", "pm", "marketing", "sales", "media"], ["dev", "ghost"],
            {"dev": {"enabled": True}}, ["dev"], catalog)
        self.assertTrue(any("ghost" in line for line in mismatches))

    def test_registry_must_equal_catalog_ai_agents(self):
        catalog = tiny_catalog({"dev": {"enabled": True},
                                "pm": {"enabled": False, "reason": "retired"}})
        mismatches = find_consumer_mismatches(
            ["dev", "pm", "marketing", "sales", "media"], ["dev", "pm"],
            {"dev": {"enabled": True}}, ["dev"], catalog)
        self.assertTrue(any("pm" in line for line in mismatches))

    def test_registry_reason_must_match_catalog(self):
        catalog = tiny_catalog({"dev": {"enabled": True},
                                "pm": {"enabled": False, "reason": "retired"}})
        mismatches = find_consumer_mismatches(
            ["dev", "pm", "marketing", "sales", "media"], ["dev", "pm"],
            {"dev": {"enabled": True},
             "pm": {"enabled": False, "reason": "another reason"}},
            ["dev"], catalog)
        self.assertTrue(any("pm" in line for line in mismatches))

    def test_wired_agent_outside_catalog_fails(self):
        catalog = tiny_catalog({"dev": {"enabled": True}})
        mismatches = find_consumer_mismatches(
            ["dev", "pm", "marketing", "sales", "media"], ["dev"],
            {"dev": {"enabled": True}}, ["dev", "ghost"], catalog)
        self.assertTrue(any("ghost" in line for line in mismatches))

    def test_legacy_disabled_names_stay_in_enum(self):
        catalog = tiny_catalog({"dev": {"enabled": True},
                                "pm": {"enabled": False, "reason": "retired"}})
        mismatches = find_consumer_mismatches(
            ["dev"], ["dev"], {"dev": {"enabled": True},
                               "pm": {"enabled": False, "reason": "retired"}},
            ["dev"], catalog)
        self.assertTrue(any("pm" in line for line in mismatches))

    def test_enabled_ai_missing_from_enum_fails_r6(self):
        catalog = tiny_catalog({"dev": {"enabled": True}})
        mismatches = find_consumer_mismatches(
            ["ceo"], ["dev"], {"dev": {"enabled": True}}, ["dev"],
            catalog)
        self.assertTrue(any(line.startswith("R6:") and "dev" in line
                            and "enum" in line for line in mismatches))

    def test_enabled_ai_missing_from_aliases_fails_r6(self):
        catalog = tiny_catalog({"dev": {"enabled": True}})
        mismatches = find_consumer_mismatches(
            ["ceo", "dev"], [], {"dev": {"enabled": True}}, ["dev"],
            catalog)
        self.assertTrue(any(line.startswith("R6:") and "dev" in line
                            and "aliases" in line
                            for line in mismatches))

    def test_disabled_ai_is_exempt_from_r6(self):
        catalog = tiny_catalog({"dev": {"enabled": True},
                                "ops": {"enabled": False,
                                        "reason": "no runner yet"}})
        mismatches = find_consumer_mismatches(
            ["ceo", "dev"], ["dev"],
            {"dev": {"enabled": True},
             "ops": {"enabled": False, "reason": "no runner yet"}},
            ["dev"], catalog)
        self.assertFalse(any(line.startswith("R6:") for line in mismatches))


class PlannerRegistryDerivationTest(unittest.TestCase):
    def test_registry_is_catalog_agents_plus_staff(self):
        catalog = load_catalog()
        registry = planner_registry()
        expected = {name for name, card in catalog.agents.items()
                    if card.type.value == "ai" or name == "owner"}
        expected |= {entry.name for entry in catalog.staff.roster}
        self.assertEqual(expected, set(registry))

    def test_owner_included_dev_enabled(self):
        registry = planner_registry()
        self.assertEqual({"enabled": True}, registry["owner"])
        self.assertEqual({"enabled": True}, registry["dev"])

    def test_disabled_carry_catalog_reasons(self):
        catalog = load_catalog()
        registry = planner_registry()
        for name, spec in registry.items():
            card = catalog.agents[name]
            with self.subTest(agent=name):
                self.assertEqual(card.enabled, spec["enabled"])
                if not card.enabled:
                    self.assertEqual(card.disabled_reason, spec["reason"])


class AgentGateTest(unittest.TestCase):
    """Unknown/disabled agents wait; wired ones run by name."""

    def setUp(self):
        self.catalog = load_catalog()

    def gate(self, hint=None, target=None, tool=None):
        params = {"target_agent": target} if target else {}
        return resolve_agent_gate(hint, params, tool, self.catalog)

    def test_loose_ticket_passes_gate_undecided(self):
        # No hint and no target: the gate proceeds and the graph node
        # waits routing_missing (TASK-2026-00204). In production the
        # run pre-stage plans every pedido, so the node never sees one.
        decision = self.gate()
        self.assertTrue(decision.proceed)
        self.assertIsNone(decision.hint)

    def test_wired_hint_runs(self):
        decision = self.gate(hint="dev")
        self.assertTrue(decision.proceed)
        self.assertEqual("dev", decision.hint)

    def test_disabled_but_wired_stays_alive(self):
        for name in ("pm", "finance", "secretary"):
            with self.subTest(agent=name):
                decision = self.gate(hint=name)
                self.assertTrue(decision.proceed)
                self.assertEqual(name, decision.hint)

    def test_disabled_unwired_waits_with_reason(self):
        decision = self.gate(hint="marketing")
        self.assertFalse(decision.proceed)
        self.assertIn("routing_missing", decision.reason)
        self.assertIn("disabled", decision.reason)
        self.assertIn("TASK-2026-00209", decision.reason)

    def test_unknown_hint_waits_never_classifies(self):
        decision = self.gate(hint="financ")
        self.assertFalse(decision.proceed)
        self.assertIn("routing_missing", decision.reason)
        self.assertIn("unknown", decision.reason)

    def test_ceo_never_executes(self):
        decision = self.gate(hint="ceo")
        self.assertFalse(decision.proceed)
        self.assertIn("routing_missing", decision.reason)

    def test_planner_research_target_waits(self):
        decision = self.gate(target="research")
        self.assertFalse(decision.proceed)
        self.assertIn("routing_missing", decision.reason)
        self.assertIn("no ticket execution bridge yet", decision.reason)

    def test_planner_wired_target_routes_by_name(self):
        decision = self.gate(target="finance")
        self.assertTrue(decision.proceed)
        self.assertEqual("finance", decision.hint)

    def test_planner_unknown_target_waits(self):
        decision = self.gate(target="frobnicate")
        self.assertFalse(decision.proceed)
        self.assertIn("unknown", decision.reason)

    def test_owner_has_no_runner(self):
        decision = self.gate(target="owner")
        self.assertFalse(decision.proceed)
        self.assertIn("routing_missing", decision.reason)

    def test_coding_tool_still_forces_dev(self):
        decision = self.gate(target="research", tool="tier_s")
        self.assertTrue(decision.proceed)
        self.assertEqual("dev", decision.hint)

    def test_hint_wins_over_stale_target(self):
        decision = self.gate(hint="dev", target="research")
        self.assertTrue(decision.proceed)
        self.assertEqual("dev", decision.hint)

    def test_names_normalized(self):
        decision = self.gate(hint=" Dev ")
        self.assertTrue(decision.proceed)
        self.assertEqual("dev", decision.hint)


class GraphWiringTest(unittest.TestCase):
    def test_wired_agents_are_the_four_nodes(self):
        self.assertEqual({"dev", "pm", "finance", "secretary"},
                         set(WIRED_AGENTS))

    def test_after_route_maps_wired_to_nodes(self):
        graph = OpenCEOGraph(None, None, None, None, None)
        for name in WIRED_AGENTS:
            with self.subTest(agent=name):
                self.assertEqual(
                    name, graph._after_ceo_route({"selected_agent": name}))

    def test_after_route_sends_rest_to_unsupported(self):
        graph = OpenCEOGraph(None, None, None, None, None)
        for name in ("marketing", "ceo", "unknown"):
            with self.subTest(agent=name):
                self.assertEqual(
                    "unsupported",
                    graph._after_ceo_route({"selected_agent": name}))

    def test_unsupported_disabled_agent_waits(self):
        graph = OpenCEOGraph(None, None, None, None, None)
        out = asyncio.run(graph._unsupported_agent_node(
            {"selected_agent": "marketing", "steps": []}))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("routing_missing", out["waiting_reason"])

    def test_unsupported_ceo_waits(self):
        graph = OpenCEOGraph(None, None, None, None, None)
        out = asyncio.run(graph._unsupported_agent_node(
            {"selected_agent": "ceo", "steps": []}))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])

    def test_enabled_ai_agents_have_routes(self):
        # Review 00006/mimo-W3, route leg: an enabled AI agent the gate
        # approves must land on a real node, never unsupported.
        catalog = load_catalog()
        graph = OpenCEOGraph(None, None, None, None, None)
        routed = [name for name, card in catalog.agents.items()
                  if card.enabled and card.type.value == "ai"]
        self.assertTrue(routed)
        for name in routed:
            with self.subTest(agent=name):
                self.assertNotEqual(
                    "unsupported",
                    graph._after_ceo_route({"selected_agent": name}))


class WaitCommentTextTest(unittest.TestCase):
    def test_agent_wait_names_agent(self):
        text = wait_comment_text(
            {"objective_id": "OBJ-1"},
            "routing_missing: agent 'research' is disabled (x)")
        self.assertIn("research", text)
        self.assertNotIn("coding_tool", text)

    def test_tier_gap_text_unchanged(self):
        text = wait_comment_text({"objective_id": "OBJ-1"}, "routing_missing")
        self.assertIn("coding_tool", text)

    def test_loose_wait_text_unchanged(self):
        # Loose routing_missing keeps telling the operator to fix an
        # explicit agent or coding_tool (TASK-2026-00204).
        text = wait_comment_text({}, "routing_missing")
        self.assertIn("coding_tool", text)


if __name__ == "__main__":
    unittest.main()
