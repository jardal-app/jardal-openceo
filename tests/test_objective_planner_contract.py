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


class PolicyTests(unittest.TestCase):
    def test_default_policy_loads(self) -> None:
        text = planner_mod.load_policy_text()
        self.assertIn("Objective Planner Routing Policy", text)
        self.assertIn("target_agent", text)

    def test_missing_policy_is_empty(self) -> None:
        self.assertEqual(
            planner_mod.load_policy_text("/nonexistent/policy.md"), "")


if __name__ == "__main__":
    unittest.main()
