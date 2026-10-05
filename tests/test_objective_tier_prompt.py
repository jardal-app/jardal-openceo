"""Objective planner prompt: tiers must be defined, never guessed.

Regression (2026-10-05, TASK-2026-00096 S2): the prompt only said
``cheapest tier that suffices`` with no tier table and no quota data, so
the planner improvised an A > B > C quality ladder (S2 children came
out b/b/a, A3: "highest capability, not the cheapest"). The prompt must
enumerate the tiers, state that letters order quota scarcity — not
quality — and default to tier_c without "cheap" framing.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s.objectives import VALID_TIERS, _planner_prompt


def _prompt() -> str:
    return _planner_prompt({"anchor": {"title": "t"}}, "", [], "", [])


class ObjectiveTierPromptTests(unittest.TestCase):
    def test_prompt_defines_every_valid_tier(self) -> None:
        prompt = _prompt()
        for tier in VALID_TIERS:
            self.assertIn(tier, prompt)

    def test_prompt_defaults_to_tier_c_without_cheap_framing(self) -> None:
        prompt = _prompt()
        self.assertIn("DEFAULT", prompt)
        self.assertIn("NOT quality", prompt)
        self.assertNotIn("cheapest", prompt.lower())
        self.assertNotIn("cheap ", prompt.lower())


if __name__ == "__main__":
    unittest.main()
