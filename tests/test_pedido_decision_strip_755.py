"""Pedido re-decision replaces the §9 block instead of appending (755).

Real finding from the OBJ-0D2EB295 close: every pedido re-decision
appended another ``RESEARCH DECISION (§9)`` line to the live
description, and every research re-decision nested another executor
contract above ``ORIGINAL REQUEST``. The decision now strips prior
blocks and builds on the original request.

No network, no CLI, no LLM: the planner runs simulated.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from objectives import engine as engine_mod  # noqa: E402
from test_single_planner_204 import (  # noqa: E402
    _pedido,
    _pedido_env,
    _plan_result,
    _ticket_decision,
)


def _dev_research(reason: str) -> dict[str, Any]:
    ticket = _ticket_decision(coding_tool="tier_c")
    ticket.pop("why_not_c", None)
    ticket["research"] = {"needed": False, "reason": reason}
    return ticket


def _research_decision() -> dict[str, Any]:
    ticket = _ticket_decision(target_agent="research", work_type="research",
                              skill="investigar")
    for key in ("coding_tool", "tier_reason", "why_not_c"):
        ticket.pop(key, None)
    return ticket


class PedidoDecisionStripTests(unittest.IsolatedAsyncioTestCase):
    async def _decide(self, ticket: dict[str, Any],
                      payload: dict[str, Any]) -> dict[str, Any]:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env, _planner, _created, patches = _pedido_env(
                self, repo, [payload])
            env.planner_cfg["agents"]["research"] = {"enabled": True}
            out = await engine_mod.plan_pedido(env, ticket)
            self.assertTrue(out["ok"], out)
            self.assertEqual(out.get("action"), "decided")
            patched = dict(ticket)
            patched.update(patches.get(ticket["task_id"]) or {})
            return patched

    async def test_redecision_replaces_research_block(self) -> None:
        first = await self._decide(
            _pedido(description="Ship idempotent retries"),
            _plan_result(_dev_research("Repo contracts fully specify retries")))
        self.assertEqual(
            first["description"],
            "Ship idempotent retries\n\n"
            "RESEARCH DECISION (§9): Repo contracts fully specify retries")
        second = await self._decide(
            first, _plan_result(_dev_research("Brief confirms the key reuse")))
        self.assertEqual(
            second["description"],
            "Ship idempotent retries\n\n"
            "RESEARCH DECISION (§9): Brief confirms the key reuse")
        self.assertEqual(second["description"].count("RESEARCH DECISION"), 1)

    async def test_redecision_heals_backlog_of_blocks(self) -> None:
        ticket = _pedido(description=(
            "Ship idempotent retries\n\n"
            "RESEARCH DECISION (§9): stale one\n\n"
            "RESEARCH DECISION (§9): stale two"))
        patched = await self._decide(
            ticket, _plan_result(_dev_research("fresh reason")))
        self.assertEqual(
            patched["description"],
            "Ship idempotent retries\n\nRESEARCH DECISION (§9): fresh reason")

    async def test_research_redecision_does_not_nest_contract(self) -> None:
        first = await self._decide(
            _pedido(description="Which retry policy is supported?"),
            _plan_result(_research_decision()))
        self.assertEqual(first["description"].count("ORIGINAL REQUEST"), 1)
        self.assertIn("Which retry policy is supported?",
                      first["description"])
        second = await self._decide(first, _plan_result(_research_decision()))
        self.assertEqual(second["description"].count("ORIGINAL REQUEST"), 1)
        self.assertEqual(second["description"].count("GOAL:"), 1)
        self.assertIn("Which retry policy is supported?",
                      second["description"])

    async def test_dev_decision_after_research_builds_on_original(self) -> None:
        decided = await self._decide(
            _pedido(description="Which retry policy is supported?"),
            _plan_result(_research_decision()))
        patched = await self._decide(
            decided, _plan_result(_dev_research("Brief answers it")))
        self.assertEqual(
            patched["description"],
            "Which retry policy is supported?\n\n"
            "RESEARCH DECISION (§9): Brief answers it")

    async def test_redecision_without_research_clears_stale_block(self) -> None:
        decided = await self._decide(
            _pedido(description="Ship idempotent retries"),
            _plan_result(_dev_research("Repo contracts fully specify retries")))
        self.assertIn("RESEARCH DECISION", decided["description"])
        plain = _ticket_decision(coding_tool="tier_c")
        plain.pop("why_not_c", None)
        patched = await self._decide(decided, _plan_result(plain))
        self.assertEqual(patched["description"], "Ship idempotent retries")
        self.assertNotIn("research", patched["params"])


if __name__ == "__main__":
    unittest.main()
