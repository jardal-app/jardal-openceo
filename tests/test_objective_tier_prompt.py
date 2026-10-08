"""Objective planner prompt: recalibrated tier semantics (TASK-2026-00148).

Supersedes the oracle-gated framing (TASK-2026-00112, routing policy
§§18-27): letters order the work by nature between four top-tier
models, tier_c is the default, and A/B leave it only with why_not_c.
The prompt must define S/A/B/C by technical meaning, require the S
pair and why_not_c for A/B, treat a declared oracle as evidence (only
qualifying types freeze, on any simple tier), choose target_agent
first, and emit the PlannerResult JSON contract.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives.planner_models import DevTier
from objectives.engine import _planner_prompt


def _prompt() -> str:
    return _planner_prompt(
        {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "", [],
        blocked_tickets=[], agents_md="REGISTERED AGENTS", policy_text="POLICY",
        handoffs=["did x"], roadmap=["later"])


class ObjectiveTierPromptTests(unittest.TestCase):
    def test_prompt_defines_every_valid_tier(self) -> None:
        prompt = _prompt()
        for tier in DevTier:
            self.assertIn(tier.value, prompt)

    def test_prompt_routes_agent_first_and_tiers_for_dev_only(self) -> None:
        prompt = _prompt()
        self.assertIn("target_agent", prompt)
        self.assertIn("only when target_agent == dev", prompt)

    def test_prompt_defaults_to_tier_c(self) -> None:
        prompt = _prompt()
        self.assertIn("tier_c: THE DEFAULT", prompt)
        self.assertIn("No oracle, reversibility proof, or", prompt)
        self.assertNotIn("qualifying independent frozen oracle exists AND",
                         prompt)

    def test_prompt_requires_why_not_c_for_a_and_b(self) -> None:
        prompt = _prompt()
        self.assertIn("why_not_c", prompt)
        self.assertIn("Never\n     select B for a missing oracle alone.",
                      prompt)
        self.assertIn(
            '"why_not_c?": "<required for tier_a/tier_b: what breaks in C>"',
            prompt)

    def test_prompt_treats_oracle_as_evidence(self) -> None:
        prompt = _prompt()
        self.assertIn("Declared oracle is evidence, not protection", prompt)
        self.assertIn("builder_tests", prompt)
        self.assertIn("NEVER triggers protection", prompt)

    def test_prompt_keeps_quota_out_of_tier_definition(self) -> None:
        prompt = _prompt()
        self.assertIn("Quota never redefines the technical tier", prompt)
        self.assertIn("letters order the work", prompt)
        self.assertIn("Never describe a tier as cheap or weaker.", prompt)
        self.assertNotIn("order quota scarcity", prompt)
        self.assertNotIn("cheapest", prompt.lower())

    def test_prompt_requires_tier_s_justification(self) -> None:
        prompt = _prompt()
        self.assertIn("why_consensus_is_needed", prompt)
        self.assertIn("why_single_session_is_insufficient", prompt)

    def test_prompt_emits_json_contract(self) -> None:
        prompt = _prompt()
        self.assertIn("PLANNER_RESULT.json", prompt)
        self.assertIn("complete_candidate", prompt)
        self.assertIn("capability_gap", prompt)
        self.assertIn("PLANNER_READY", prompt)
        self.assertNotIn("TICKETS.yaml", prompt)

    def test_prompt_carries_bounded_state_sections(self) -> None:
        prompt = _prompt()
        for section in ("BLOCKED TICKETS:", "RECENT HANDOFFS",
                        "ROADMAP_REMAINING:", "REGISTERED AGENTS",
                        "NORMATIVE ROUTING POLICY"):
            self.assertIn(section, prompt)
        self.assertIn("did x", prompt)


if __name__ == "__main__":
    unittest.main()
