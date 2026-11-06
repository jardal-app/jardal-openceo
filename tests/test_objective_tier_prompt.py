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
        self.assertIn("Never select B for a missing\n"
                      "     oracle or weak verification alone.", prompt)
        self.assertIn(
            '"why_not_c?": "<required for tier_a/tier_b: what breaks in C; '
            'omit for tier_s and tier_c>"',
            prompt)

    def test_weak_verification_only_aggravates_b(self) -> None:
        # TASK-2026-00178: weak verification is no B prong by itself.
        prompt = _prompt()
        self.assertIn("weak verification only aggravates those", prompt)
        self.assertNotIn("interpretation, weak verification, or", prompt)

    def test_prompt_reserves_s_for_critical_work(self) -> None:
        # Owner order 2026-11-06 (TASK-2026-00510): only the critical reaches S.
        prompt = _prompt()
        self.assertIn("tier_s: CRITICAL work only", prompt)
        self.assertIn("orchestrating several", prompt)
        self.assertIn("independent model families", prompt)
        self.assertIn("maximum assurance", prompt)
        self.assertIn("consumers (wiring, UI", prompt)
        self.assertIn("Not rationed by percentage or count.", prompt)
        self.assertNotIn("in ANY domain", prompt)

    def test_prompt_forbids_modified_files_as_oracle(self) -> None:
        prompt = _prompt()
        self.assertIn("Never declare as oracle a file the\n"
                      "  ticket is expected to modify.", prompt)

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


class RoadmapOutlookPromptTests(unittest.TestCase):
    """The planner is asked for an outlook, with the validator's numbers."""

    def test_prompt_asks_for_the_outlook_with_the_enforced_bounds(self) -> None:
        from objectives.planner_models import ROADMAP_MAX_CHARS, ROADMAP_MAX_LINES

        prompt = _prompt()
        flat = " ".join(prompt.split())
        self.assertIn("ROADMAP_REMAINING is your outlook", prompt)
        self.assertIn(f"(at most {ROADMAP_MAX_LINES} lines of "
                      f"{ROADMAP_MAX_CHARS} characters)", flat)
        self.assertIn("one line per step saying what it depends on", flat)
        self.assertIn("speculative notes for the next cycle, not commitments",
                      flat)
        self.assertIn("rewrite the whole list every cycle", flat)
        self.assertIn("A longer list or line is rejected.", prompt)

    def test_json_contract_describes_a_line_per_step(self) -> None:
        prompt = _prompt()
        self.assertIn('"roadmap_remaining": ["<one line per foreseen next '
                      'step: what, and what it depends on>"]', prompt)
        self.assertNotIn("<speculative future work>", prompt)

    def test_replan_when_is_no_longer_requested(self) -> None:
        self.assertNotIn("replan_when", _prompt())

    def test_the_contract_object_still_closes_cleanly(self) -> None:
        # roadmap_remaining is now the last key: no dangling comma.
        prompt = _prompt()
        start = prompt.index('"roadmap_remaining"')
        line_end = prompt.index("\n", start)
        self.assertFalse(prompt[start:line_end].rstrip().endswith(","))
        self.assertEqual(prompt[line_end + 1].strip(), "}")


class PlannedTicketDescriptionTests(unittest.TestCase):
    """The justifications travel into the ticket the executor and the owner read."""

    @staticmethod
    def _planned(**overrides):
        from objectives.planner_models import PlannedTicket

        base = {
            "local_ref": "t1", "title": "Do the thing", "target_agent": "dev",
            "work_type": "implementation", "goal": "ship it",
            "why_now": "unblocks the objective",
            "acceptance_criteria": ["it works"],
            "expected_artifacts": ["code"],
            "coding_tool": "tier_b", "tier_reason": "semantic refactor",
            "why_not_c": "a misjudged refactor ships behavior drift",
        }
        base.update(overrides)
        return PlannedTicket.model_validate(base)

    def test_why_not_c_reaches_the_ticket_description(self) -> None:
        from objectives.engine import _planned_description, _planned_params

        ticket = self._planned()
        description = _planned_description(ticket)
        self.assertIn(
            "WHY NOT THE DEFAULT TIER (C): a misjudged refactor ships "
            "behavior drift", description)
        self.assertEqual(_planned_params(ticket)["why_not_c"],
                         "a misjudged refactor ships behavior drift")

    def test_default_tier_description_carries_no_why_not_c(self) -> None:
        from objectives.engine import _planned_description

        ticket = self._planned(coding_tool="tier_c", why_not_c=None,
                               tier_reason="specified work one session owns")
        self.assertNotIn("WHY NOT THE DEFAULT TIER", _planned_description(ticket))


if __name__ == "__main__":
    unittest.main()
