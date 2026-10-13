from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from config_loader import ConfigLoader
from router import CEORouter
from settings import Settings
from state import AgentName


class RouterExplicitTests(unittest.TestCase):
    """The router resolves explicit names only (TASK-2026-00204).

    Config triggers, requirement heuristics, default agents and the LLM
    fallback were retired with the single planner: ticket text never
    selects an agent anymore.
    """

    def setUp(self) -> None:
        self.settings = Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo")
        self.router = CEORouter(self.settings)

    def test_hint_resolves_through_aliases(self) -> None:
        cases = [
            ("dev", AgentName.DEV),
            ("developer", AgentName.DEV),
            ("finance", AgentName.FINANCE),
            ("operations", AgentName.OPERATIONS),
            ("research", AgentName.RESEARCH),
            ("secretary", AgentName.SECRETARY),
        ]
        for hint, expected in cases:
            with self.subTest(hint=hint):
                self.assertEqual(
                    self.router.resolve_explicit(hint, None), expected)

    def test_unknown_hint_resolves_to_none(self) -> None:
        for hint in ("nope", "", "   ", None):
            with self.subTest(hint=hint):
                self.assertIsNone(self.router.resolve_explicit(hint, None))

    def test_coding_tool_forces_dev_over_hint(self) -> None:
        # F0 pilot 00097 misrouted to pm: a persisted coding_tool is a dev
        # executor by definition and wins over any hint.
        for tool in ("tier_s", "tier_c", "codex", "muse"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    self.router.resolve_explicit("pm", tool), AgentName.DEV)

    def test_blank_coding_tool_defers_to_hint(self) -> None:
        for tool in (None, "", "   "):
            with self.subTest(tool=tool):
                self.assertEqual(
                    self.router.resolve_explicit("dev", tool), AgentName.DEV)
                self.assertIsNone(self.router.resolve_explicit(None, tool))


class RoutingConfigLoaderTests(unittest.TestCase):
    def test_project_defaults_carry_no_routing_block(self) -> None:
        # Deliberate contract change (TASK-2026-00204): the keyword
        # routing block (triggers/patterns/default_agent/llm_fallback)
        # retired with the classifying router. dev.routing (the 00161
        # money guide) lives on, tested in test_routing_money_sensitive.
        loader = ConfigLoader(Settings(DEEPSEEK_API_KEY="", PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo"))

        config = loader.load_project("jardal-openceo")

        self.assertNotIn("routing", config)

    def test_top_level_routing_block_is_rejected(self) -> None:
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir(parents=True)
            (root / "project.yaml").write_text(
                "routing:\n  default_agent: dev\n", encoding="utf-8")
            loader = ConfigLoader(Settings(_env_file=None,
                                            PROJECTS_DIR=str(tmp)))
            errors = loader.validate_project("proj")
        self.assertTrue(any("routing is obsolete" in err for err in errors),
                        errors)


if __name__ == "__main__":
    unittest.main()
