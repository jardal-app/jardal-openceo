"""Planner prompt teaches the enforced FILES shape (TASK-2026-00437).

Contract fixed here: the validator only accepts
``- <path> (<create|modify|delete>) — <why>``, but the planner prompt
said just "FILES with create|modify|delete + why" and opus twice wrote
``- create <path> — <why>`` (ticket 480 parked needs_owner). The prompt
now pins the exact shape with an example, and the retry prompt repeats
it when the errors name bad FILES lines.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s import cycle as cycle_mod  # noqa: E402
from agents.dev.tier_s.plan import parse_plan, validate_plan  # noqa: E402

SHAPE = "- <path> (<create|modify|delete>) — <why>"


def _ctx(repo: Path) -> Any:
    return cycle_mod._Ctx(  # noqa: SLF001
        ticket={"task_id": "t", "title": "T", "description": "D",
                "acceptance": ["a1"], "params": {}},
        repo=repo, ref="t", run_id="run", branch="b", base_branch="main",
        base_sha="base", budgets={}, timeouts={}, waiting_cfg={},
        auto_merge=True, budget_exclude=[], gate_specs=[], roles={})


class PlanFormatPromptTests(unittest.TestCase):
    def test_planner_prompt_pins_files_shape(self) -> None:
        cycle = cycle_mod.TierSCycle(
            MagicMock(), MagicMock(), MagicMock(), MagicMock())
        with tempfile.TemporaryDirectory() as tmp:
            prompt = cycle._planner_prompt(_ctx(Path(tmp)))  # noqa: SLF001
        self.assertIn(SHAPE, prompt)
        self.assertIn("- apps/web/src/auth/login.ts (create) — login",
                      prompt)

    def test_retry_prompt_teaches_shape_on_files_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            retry = cycle_mod._plan_retry_prompt(  # noqa: SLF001
                "bad FILES line: - create x — y", "PLAN_READY", repo)
            other = cycle_mod._plan_retry_prompt(  # noqa: SLF001
                "missing GOAL", "PLAN_READY", repo)
        self.assertIn(SHAPE, retry)
        self.assertNotIn(SHAPE, other)

    def test_taught_shape_roundtrips_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            text = "\n".join([
                "TICKET: TASK-2026-99999", "OBJECTIVE: none",
                "GOAL: g", "ACCEPTANCE:", "- ok", "FILES:",
                "- tracked.txt (modify) — change it",
                "- new.py (create) — new file", "CONTRACTS:", "- none",
                "DECISIONS:", "- d", "STEPS:", "1. Do it",
                "ESTIMATE: files=2 lines=3", "OUT_OF_SCOPE:", "- none", "",
            ])
            plan = parse_plan(text)
            self.assertEqual(
                [(item.path, item.operation) for item in plan.files],
                [("tracked.txt", "modify"), ("new.py", "create")])
            validation = validate_plan(plan, repo, 8, ())
            self.assertEqual(validation.errors, ())
            self.assertTrue(validation.valid)

    def test_operation_first_shape_stays_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            text = "\n".join([
                "TICKET: T", "OBJECTIVE: none", "GOAL: g", "ACCEPTANCE:",
                "- ok", "FILES:", "- create new2.py — why", "CONTRACTS:",
                "- none", "DECISIONS:", "- d", "STEPS:", "1. Do it",
                "ESTIMATE: files=1 lines=1", "OUT_OF_SCOPE:", "- none", "",
            ])
            validation = validate_plan(parse_plan(text), repo, 8, ())
            self.assertFalse(validation.valid)
            self.assertTrue(any("bad FILES line" in err
                                for err in validation.errors))


if __name__ == "__main__":
    unittest.main()
