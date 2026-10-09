"""Single planner contract: PlannerResult shape + deterministic rules.

Acceptance for TASK-2026-00112 (routing policy §17): Pydantic owns the
shape (dev-only coding_tool, Tier S justification, Tier A/B why_not_c,
Tier C default without oracle, no-ticket outcomes, horizon, unique
refs); deterministic validation owns agent registry, blocked_by,
duplicates and the same-tier-retry rule.
Recalibrated by TASK-2026-00148 (policy §§25-27).
No network, no CLI, no LLM.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import planner_models as planner_mod
from objectives.planner_models import (
    ROADMAP_MAX_CHARS,
    ROADMAP_MAX_LINES,
    PlannerResult,
    parse_planner_result,
    validate_planner_result,
)

AGENTS = {
    "dev": {"enabled": True},
    "research": {"enabled": False, "reason": "no ticket execution bridge yet"},
}


def _ticket(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "local_ref": "t1",
        "title": "Do the thing",
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": "ship it",
        "why_now": "unblocks the objective",
        "acceptance_criteria": ["it works"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_b",
        "tier_reason": "semantic refactor where errors pass local tests",
        "why_not_c": "a misjudged refactor ships behavior drift",
    }
    base.update(overrides)
    return base


def _result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "objective_status": "active",
        "state_summary": "first cycle",
        "tickets": [_ticket()],
    }
    base.update(overrides)
    return base


async def _missing(task_id: str) -> bool:
    del task_id
    return False


class ParseTests(unittest.TestCase):
    def test_valid_result_parses(self) -> None:
        result = parse_planner_result(json.dumps(_result()))
        self.assertEqual(result.objective_status.value, "active")
        self.assertEqual(result.tickets[0].coding_tool.value, "tier_b")

    def test_tier_c_with_qualifying_oracle_parses(self) -> None:
        ticket = _ticket(
            coding_tool="tier_c",
            tier_reason="preexisting tests cover it; reversible migration",
            why_not_c=None,
            oracle={"type": "preexisting_tests",
                    "refs": [{"ref": "tests/test_x.py"}]},
        )
        result = parse_planner_result(json.dumps(_result(tickets=[ticket])))
        oracle = result.tickets[0].oracle
        assert oracle is not None
        self.assertEqual(oracle.type.value, "preexisting_tests")

    def test_tier_s_with_justification_parses(self) -> None:
        ticket = _ticket(
            coding_tool="tier_s",
            tier_reason="auth boundary; independent review catches bypasses",
            why_consensus_is_needed="a second family catches auth bypasses",
            why_single_session_is_insufficient="one session wrote the flaw",
        )
        result = parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertEqual(result.tickets[0].coding_tool.value, "tier_s")

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(ValueError) as raised:
            parse_planner_result("{not json")
        self.assertIn("invalid PLANNER_RESULT.json", str(raised.exception))

    def test_non_object_rejected(self) -> None:
        with self.assertRaises(ValueError) as raised:
            parse_planner_result("[1, 2]")
        self.assertIn("must be a JSON object", str(raised.exception))

    def test_coding_tool_only_for_dev(self) -> None:
        ticket = _ticket(target_agent="research", coding_tool="tier_b",
                         tier_reason=None)
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertIn("coding_tool is only valid", str(raised.exception))

    def test_tier_reason_only_for_dev(self) -> None:
        ticket = _ticket(target_agent="research", coding_tool=None,
                         tier_reason="x")
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertIn("tier_reason is only valid", str(raised.exception))

    def test_dev_requires_coding_tool_and_tier_reason(self) -> None:
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(
                _result(tickets=[_ticket(coding_tool=None)])))
        self.assertIn("require coding_tool", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(
                _result(tickets=[_ticket(tier_reason=None)])))
        self.assertIn("require tier_reason", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(
                _result(tickets=[_ticket(tier_reason="  ")])))
        self.assertIn("must not be blank", str(raised.exception))

    def test_tier_s_requires_both_justifications(self) -> None:
        ticket = _ticket(coding_tool="tier_s",
                         why_consensus_is_needed="needed",
                         why_single_session_is_insufficient=None)
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertIn("why_single_session_is_insufficient",
                      str(raised.exception))

    def test_tier_c_needs_no_oracle(self) -> None:
        # Recalibration (TASK-2026-00148): C is the default (§25).
        ticket = _ticket(coding_tool="tier_c", why_not_c=None)
        result = parse_planner_result(json.dumps(
            _result(tickets=[ticket])))
        self.assertEqual(result.tickets[0].coding_tool.value, "tier_c")
        self.assertIsNone(result.tickets[0].oracle)

    def test_tier_c_accepts_builder_tests_as_evidence(self) -> None:
        # Declared oracle of any type is evidence; only qualifying
        # types freeze, enforced by the orchestrator (§24), not here.
        ticket = _ticket(
            coding_tool="tier_c",
            why_not_c=None,
            oracle={"type": "builder_tests",
                    "refs": [{"ref": "tests/new.py"}]},
        )
        result = parse_planner_result(json.dumps(_result(tickets=[ticket])))
        oracle = result.tickets[0].oracle
        assert oracle is not None
        self.assertEqual(oracle.type.value, "builder_tests")

    def test_tier_a_and_b_require_why_not_c(self) -> None:
        for tier in ("tier_a", "tier_b"):
            with self.subTest(tier=tier):
                with self.assertRaises(ValueError) as raised:
                    parse_planner_result(json.dumps(
                        _result(tickets=[_ticket(coding_tool=tier,
                                                why_not_c=None)])))
                self.assertIn("requires why_not_c", str(raised.exception))
                with self.assertRaises(ValueError) as raised:
                    parse_planner_result(json.dumps(
                        _result(tickets=[_ticket(coding_tool=tier,
                                                why_not_c="  ")])))
                self.assertIn("must not be blank", str(raised.exception))

    def test_tier_s_needs_no_why_not_c(self) -> None:
        # The S pair already states why one session is not enough (§20).
        ticket = _ticket(
            coding_tool="tier_s",
            tier_reason="auth boundary; independent review catches bypasses",
            why_consensus_is_needed="a second family catches auth bypasses",
            why_single_session_is_insufficient="one session wrote the flaw",
            why_not_c=None,
        )
        result = parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertEqual(result.tickets[0].coding_tool.value, "tier_s")

    def test_tier_s_accepts_an_optional_why_not_c(self) -> None:
        ticket = _ticket(
            coding_tool="tier_s",
            tier_reason="ordering logic where the first solution is often wrong",
            why_consensus_is_needed="independent readings catch the ordering "
                                    "assumption one session would bake in",
            why_single_session_is_insufficient="one session owns plan and code",
            why_not_c="the default tier would ship the first plausible order",
        )
        result = parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertEqual(result.tickets[0].coding_tool.value, "tier_s")

    def test_tier_s_is_not_tied_to_a_domain(self) -> None:
        # TASK-2026-00178: the schema checks the justification pair, never
        # a domain list -- a non-money, non-auth S ticket is valid.
        ticket = _ticket(
            coding_tool="tier_s",
            tier_reason="owner asked for a near fault-free result on the "
                        "scheduling algorithm",
            why_consensus_is_needed="the owner's explicit request for maximum "
                                    "assurance; independent plans compare the "
                                    "tie-breaking rules",
            why_single_session_is_insufficient="a single session would bake "
                                               "one tie-breaking reading in",
            why_not_c=None,
        )
        result = parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertEqual(result.tickets[0].coding_tool.value, "tier_s")

    def test_tier_c_rejects_why_not_c(self) -> None:
        # A why_not_c on the default tier contradicts itself: the planner
        # meant tier_a/tier_b (TASK-2026-00178).
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(_result(tickets=[
                _ticket(coding_tool="tier_c",
                        why_not_c="a misjudged refactor ships drift")])))
        self.assertIn("tier_c is the default and carries no why_not_c",
                      str(raised.exception))

    def test_why_not_c_only_for_dev(self) -> None:
        ticket = _ticket(target_agent="research", coding_tool=None,
                         tier_reason=None,
                         why_not_c="discovery must come first")
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertIn("why_not_c is only valid", str(raised.exception))

    def test_complete_candidate_carries_no_tickets(self) -> None:
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(
                _result(objective_status="complete_candidate")))
        self.assertIn("must not create child tickets", str(raised.exception))
        result = parse_planner_result(json.dumps(
            _result(objective_status="complete_candidate", tickets=[])))
        self.assertEqual(result.tickets, [])

    def test_blocked_and_gap_carry_no_tickets(self) -> None:
        for status in ("blocked", "capability_gap"):
            with self.subTest(status=status):
                with self.assertRaises(ValueError):
                    parse_planner_result(json.dumps(
                        _result(objective_status=status)))

    def test_duplicate_local_ref_rejected(self) -> None:
        other = _ticket(local_ref="t1", title="Other")
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(
                _result(tickets=[_ticket(), other])))
        self.assertIn("must be unique", str(raised.exception))

    def test_horizon_capped_at_three(self) -> None:
        tickets = [_ticket(local_ref=f"t{n}", title=f"T{n}")
                   for n in range(4)]
        with self.assertRaises(ValueError):
            parse_planner_result(json.dumps(_result(tickets=tickets)))

    def test_blank_strings_rejected(self) -> None:
        cases = [
            _ticket(title="  "),
            _ticket(goal="   "),
            _ticket(local_ref=" "),
            _ticket(acceptance_criteria=["ok", "  "]),
            _ticket(expected_artifacts=[""]),
            _ticket(blocked_by=[" "]),
        ]
        for ticket in cases:
            with self.subTest(ticket=ticket):
                with self.assertRaises(ValueError) as raised:
                    parse_planner_result(json.dumps(
                        _result(tickets=[ticket])))
                self.assertIn("blank", str(raised.exception))

    def test_blank_summary_and_roadmap_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_planner_result(json.dumps(
                _result(state_summary="  ")))
        with self.assertRaises(ValueError):
            parse_planner_result(json.dumps(
                _result(roadmap_remaining=["later", ""])))

    def test_oracle_only_for_dev(self) -> None:
        ticket = _ticket(
            target_agent="research", coding_tool=None, tier_reason=None,
            oracle={"type": "preexisting_tests",
                    "refs": [{"ref": "tests/test_x.py"}]},
        )
        with self.assertRaises(ValueError) as raised:
            parse_planner_result(json.dumps(_result(tickets=[ticket])))
        self.assertIn("oracle is only valid", str(raised.exception))


class DeterministicTests(unittest.IsolatedAsyncioTestCase):
    async def _validate(self, payload: dict[str, Any],
                        **kwargs: Any) -> list[str]:
        result = PlannerResult.model_validate(payload)
        params: dict[str, Any] = {
            "agents": AGENTS,
            "open_titles": [],
            "task_exists": _missing,
        }
        params.update(kwargs)
        return await validate_planner_result(result, **params)

    async def test_valid_result_passes(self) -> None:
        self.assertEqual(await self._validate(_result()), [])

    async def test_active_requires_a_ticket(self) -> None:
        errors = await self._validate(_result(tickets=[]))
        self.assertTrue(any("at least 1 ticket" in err for err in errors))

    async def test_unknown_agent_rejected(self) -> None:
        errors = await self._validate(
            _result(tickets=[_ticket(target_agent="payments",
                                     coding_tool=None, tier_reason=None,
                                     why_not_c=None)]))
        self.assertTrue(any("unknown target_agent" in err for err in errors))

    async def test_disabled_agent_rejected_with_reason(self) -> None:
        errors = await self._validate(
            _result(tickets=[_ticket(target_agent="research",
                                     coding_tool=None, tier_reason=None,
                                     why_not_c=None)]))
        self.assertEqual(len(errors), 1)
        self.assertIn("disabled", errors[0])
        self.assertIn("no ticket execution bridge", errors[0])

    async def test_local_blocked_by_accepted(self) -> None:
        first = _ticket(local_ref="a", title="First")
        second = _ticket(local_ref="b", title="Second", blocked_by=["a"])
        errors = await self._validate(
            _result(tickets=[first, second]))
        self.assertEqual(errors, [])

    async def test_unknown_blocked_by_rejected(self) -> None:
        errors = await self._validate(
            _result(tickets=[_ticket(blocked_by=["nope"])]))
        self.assertTrue(any("blocked_by unknown" in err for err in errors))

    async def test_existing_task_blocked_by_accepted(self) -> None:
        async def exists(task_id: str) -> bool:
            return task_id == "task-1"

        errors = await self._validate(
            _result(tickets=[_ticket(blocked_by=["task-1"])]),
            task_exists=exists)
        self.assertEqual(errors, [])

    async def test_cycle_rejected(self) -> None:
        first = _ticket(local_ref="a", title="First", blocked_by=["b"])
        second = _ticket(local_ref="b", title="Second", blocked_by=["a"])
        errors = await self._validate(
            _result(tickets=[first, second]))
        self.assertTrue(any("cycle" in err for err in errors))

    async def test_duplicate_open_title_rejected(self) -> None:
        errors = await self._validate(
            _result(), open_titles=["  DO THE THING "])
        self.assertTrue(any("duplicates an open ticket" in err
                            for err in errors))

    async def test_in_batch_duplicate_title_rejected(self) -> None:
        first = _ticket(local_ref="a", title="Same thing")
        second = _ticket(local_ref="b", title="same THING")
        errors = await self._validate(_result(tickets=[first, second]))
        self.assertEqual(len(errors), 1)
        self.assertIn("duplicates an open ticket", errors[0])
        self.assertIn("'b'", errors[0])

    async def test_malformed_material_failures_tolerated(self) -> None:
        errors = await self._validate(
            _result(), material_failures=["oops", None, 42,
                                          {"title": "x", "tier": "tier_c"}])
        self.assertEqual(errors, [])

    async def test_configured_horizon_enforced(self) -> None:
        one = [_ticket()]
        errors = await self._validate(_result(tickets=one), horizon=[2, 3])
        self.assertTrue(any("at least 2 ticket" in err for err in errors))
        three = [_ticket(local_ref=f"t{n}", title=f"T{n}") for n in range(3)]
        errors = await self._validate(_result(tickets=three),
                                      horizon=[1, 2])
        self.assertTrue(any("horizon is 1-2" in err for err in errors))

    async def test_misconfigured_horizon_rejects(self) -> None:
        for bad in ("x", [3, 1], [-1, 2], [1], None.__class__):
            with self.subTest(horizon=bad):
                errors = await self._validate(_result(), horizon=bad)
                self.assertTrue(any("misconfigured" in err
                                    for err in errors))

    async def test_same_tier_retry_after_material_failure_rejected(
        self,
    ) -> None:
        failures = [{"title": "do the thing", "tier": "tier_b"}]
        errors = await self._validate(
            _result(), material_failures=failures)
        self.assertTrue(any("same-tier retry" in err for err in errors))
        other_tier = _ticket(coding_tool="tier_a",
                             tier_reason="needs discovery now")
        errors = await self._validate(
            _result(tickets=[other_tier]), material_failures=failures)
        self.assertEqual(errors, [])

    async def test_no_ticket_outcomes_need_no_tickets(self) -> None:
        for status in ("complete_candidate", "blocked", "capability_gap"):
            with self.subTest(status=status):
                errors = await self._validate(
                    _result(objective_status=status, tickets=[]))
                self.assertEqual(errors, [])


class RoadmapBoundsTests(unittest.TestCase):
    """``roadmap_remaining`` is the planner's outlook, bounded (TASK-2026-00183)."""

    @staticmethod
    def _parse(roadmap: list[str], **extra: Any) -> PlannerResult:
        return parse_planner_result(json.dumps(
            _result(roadmap_remaining=roadmap, **extra)))

    def test_the_limits_are_the_documented_ones(self) -> None:
        self.assertEqual((ROADMAP_MAX_LINES, ROADMAP_MAX_CHARS), (10, 300))

    def test_an_outlook_of_seven_or_eight_steps_is_accepted(self) -> None:
        steps = [f"step {n}: migrate the module; depends on T{n - 1}"
                 for n in range(1, 8)]
        self.assertEqual(self._parse(steps).roadmap_remaining, steps)

    def test_exactly_at_the_limits_is_accepted(self) -> None:
        full = ["x" * ROADMAP_MAX_CHARS for _ in range(ROADMAP_MAX_LINES)]
        self.assertEqual(len(self._parse(full).roadmap_remaining),
                         ROADMAP_MAX_LINES)

    def test_one_line_too_many_is_rejected_with_a_clear_message(self) -> None:
        with self.assertRaises(ValueError) as raised:
            self._parse(["note"] * (ROADMAP_MAX_LINES + 1))
        message = str(raised.exception)
        self.assertIn(f"at most {ROADMAP_MAX_LINES} lines", message)
        self.assertIn(f"got {ROADMAP_MAX_LINES + 1}", message)

    def test_one_character_too_long_is_rejected_naming_the_line(self) -> None:
        notes = ["fine", "y" * (ROADMAP_MAX_CHARS + 1)]
        with self.assertRaises(ValueError) as raised:
            self._parse(notes)
        message = str(raised.exception)
        self.assertIn("roadmap_remaining line 2", message)
        self.assertIn(f"{ROADMAP_MAX_CHARS + 1} characters", message)
        self.assertIn(f"max {ROADMAP_MAX_CHARS}", message)

    def test_blank_lines_are_still_rejected(self) -> None:
        with self.assertRaises(ValueError) as raised:
            self._parse(["ok", "   "])
        self.assertIn("must not contain blank items", str(raised.exception))

    def test_surrounding_whitespace_does_not_count_against_the_limit(self) -> None:
        padded = [("  " + "z" * ROADMAP_MAX_CHARS + "  ")]
        self.assertEqual(len(self._parse(padded).roadmap_remaining), 1)

    def test_replan_when_is_gone_from_the_contract_but_tolerated(self) -> None:
        # Dropped in TASK-2026-00183: nothing ever stored or used it. A
        # planner that still emits it must not fail (unknown keys ignored).
        result = self._parse(["later"], replan_when=["if the migration fails"])
        self.assertFalse(hasattr(result, "replan_when"))
        self.assertNotIn("replan_when", PlannerResult.model_fields)


class PolicyTests(unittest.TestCase):
    def test_default_policy_loads(self) -> None:
        text = planner_mod.load_policy_text()
        self.assertIn("Objective Planner Routing Policy", text)
        self.assertIn("target_agent", text)

    def test_missing_policy_is_empty(self) -> None:
        self.assertEqual(
            planner_mod.load_policy_text("/nonexistent/policy.md"), "")

    @staticmethod
    def _flat() -> str:
        return " ".join(planner_mod.load_policy_text().split())

    def test_policy_fits_the_prompt_budget(self) -> None:
        # The planner prompt keeps the first 60000 chars of the policy.
        self.assertLess(len(planner_mod.load_policy_text().strip()), 60000)

    def test_tier_s_is_defined_by_orchestration_value_not_domain(self) -> None:
        # TASK-2026-00178 (owner direction 2026-10-09).
        text = self._flat()
        self.assertIn("orchestrating several independent model families", text)
        self.assertIn("The criterion is the expected value of independent "
                      "challenge, never a domain.", text)
        self.assertIn("no domain is excluded", text)
        self.assertIn("illustrative, not a closed list", text)
        self.assertIn("maximum assurance, a near fault-free result or "
                      "multi-model orchestration is itself a valid reason",
                      text)
        self.assertIn("S is not rationed by percentage", text)
        self.assertIn("limit Tier S to a fixed list of domains", text)
        self.assertNotIn("Strong signals include", text)

    def test_weak_verification_is_never_a_b_prong_by_itself(self) -> None:
        text = self._flat()
        self.assertIn("Weak verification (builder-authored tests only, no "
                      "usable oracle) is never a prong by itself", text)
        self.assertNotIn("verification is weak (builder-authored", text)
        self.assertIn("select B for a missing oracle or weak verification "
                      "alone", text)

    def test_oracle_and_chain_contracts_are_written_down(self) -> None:
        text = self._flat()
        self.assertIn("dev.oracle_cmd", text)
        self.assertIn("is never declared as an oracle", text)
        self.assertIn("is already red there, it cannot accept or reject", text)
        self.assertIn("executor_link", text)
        self.assertIn("provides availability, not selection", text)
        self.assertIn("Their quota gate never applies the reservation", text)
        self.assertNotIn("Tier-C oracle eligibility", text)

    def test_policy_describes_the_system_as_it_is_today(self) -> None:
        # TASK-2026-00182: the policy stated things the code does not do.
        text = self._flat()
        # §17 lists only what the validator really checks.
        self.assertIn("oracle declaration shape (known oracle type, at "
                      "least one ref)", text)
        self.assertNotIn("refs the ticket does not modify)", text)
        self.assertIn("Not validated deterministically at this point", text)
        # §6 / §10 / §34: project creation, the research agent, spec authoring.
        self.assertIn("carries no `params.workflow`", text)
        self.assertIn("launched by the CEO review's `create_project` action",
                      text)
        # (blockquote: each wrapped line starts with "> ")
        self.assertIn("is registered but disabled in the agent", text)
        self.assertIn("It declares; it does not write", text)
        # §12 / §15: the horizon is a hard maximum; triggers are split.
        self.assertIn("enforces 3 as a hard maximum", text)
        self.assertNotIn("Create more only when downstream tickets", text)
        self.assertIn("Wired today", text)
        self.assertIn("Reserved, not wired yet", text)
        # Header: no stale baseline marker, still a runtime document.
        self.assertNotIn("INITIAL_NORMATIVE_BASELINE", text)
        self.assertIn("runtime_document: true", text)

    def test_policy_quotes_the_roadmap_bounds_the_validator_enforces(self) -> None:
        # TASK-2026-00183: the numbers live in planner_models; the policy,
        # the prompt and the validator must not drift apart.
        text = self._flat()
        bounds = f"at most {ROADMAP_MAX_LINES} lines of {ROADMAP_MAX_CHARS} characters"
        self.assertIn(f"Use `roadmap_remaining` as your outlook", text)
        self.assertGreaterEqual(text.count(bounds), 2)   # §12 and §17
        self.assertIn(f"at most {ROADMAP_MAX_LINES} one-line notes of "
                      f"{ROADMAP_MAX_CHARS} characters", text)   # §42 rule 13
        self.assertIn("There is no separate field for re-planning conditions",
                      text)
        self.assertNotIn("replan_when", text)


if __name__ == "__main__":
    unittest.main()
