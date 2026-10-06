"""Objective planner prompt: new tier semantics, never the old ladder.

Supersedes the 2026-10-05 regression framing (TASK-2026-00096 S2) per the
autonomy delta (TASK-2026-00112, routing policy §§18-27): letters no longer
order quota scarcity with tier_c as default. The prompt must define S/A/C/B
by technical meaning, gate C on a qualifying oracle plus reversible-or-bounded
risk, choose target_agent first, and emit the PlannerResult JSON contract.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s.objective_planner_models import DevTier
from agents.dev.tier_s.objectives import _planner_prompt


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

    def test_prompt_gates_tier_c_on_oracle_and_bounded_risk(self) -> None:
        prompt = _prompt()
        self.assertIn("qualifying independent", prompt)
        self.assertIn("reversible OR", prompt)
        self.assertIn("bounded", prompt)
        self.assertIn("builder_tests alone NEVER qualify", prompt)

    def test_prompt_keeps_quota_out_of_tier_definition(self) -> None:
        prompt = _prompt()
        self.assertIn("Quota never redefines the technical tier", prompt)
        self.assertNotIn("order quota scarcity", prompt)
        self.assertNotIn("Default to tier_c", prompt)
        self.assertNotIn("cheapest", prompt.lower())
        self.assertNotIn("cheap ", prompt.lower())

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
