"""Product-design bar for every interface a builder ships (TASK-2026-00734).

jardal-erp shipped screens as plain label lines joined by <br>: the first
flat screen (#491) became the convention five tickets copied the next day.
The engineering standard only asked an interface not to be broken, the
Tier S planner decided the visual composition without rendering it, and
the objective planner contracted parity over flat screens. Contract under
test:

- the builder standard carries the product-design bar, scopes conventions
  to code style and asks the browser check to judge the finish;
- simple-tier and Tier S builder prompts carry that bar;
- the Tier S planner leaves the visual composition to the builder;
- the objective planner guide and the normative policy (§44) carry it and
  keep reviewers on code (no visual review loop).

No network, no CLI, no LLM.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]
for _path in (REPO / "openceo-core", REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agents.dev.builder_contract import engineering_standard_prompt  # noqa: E402
from agents.dev.session import DevSessionManager  # noqa: E402
from agents.dev.tier_s.cycle import TierSCycle, _Ctx  # noqa: E402
from agents.dev.tier_s.plan import Plan, PlannedFile  # noqa: E402
from agents.dev.tier_s.stores import (  # noqa: E402
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from orchestration.role_contracts import with_role_contract  # noqa: E402

POLICY = REPO / "openceo-core" / "objectives" / "routing_policy.md"
DESIGN_BAR = "Una interfaz se diseña, no se transcribe"


class BuilderStandardTests(unittest.TestCase):
    def test_standard_carries_the_product_design_bar(self) -> None:
        text = engineering_standard_prompt()
        self.assertIn(DESIGN_BAR, text)
        self.assertIn("diseñador de producto senior", text)
        self.assertIn("fija identidad y accesibilidad, no un techo", text)
        self.assertIn("su documentación para agentes, si la trae", text)
        self.assertIn("Nunca maquetes con <br>", text)
        self.assertIn("es deuda, no convención", text)

    def test_conventions_rule_code_style_not_screens(self) -> None:
        text = engineering_standard_prompt()
        self.assertIn("deciden estilo de código y herramientas", text)
        self.assertNotIn("deciden estilo y herramientas", text)

    def test_browser_check_judges_the_finish_once(self) -> None:
        text = engineering_standard_prompt()
        self.assertIn("acabado de producto comercial", text)
        self.assertIn("mejórala antes de entregar", text)

    def test_simple_tier_builder_prompt_carries_the_bar(self) -> None:
        manager = DevSessionManager(
            SimpleNamespace(DEV_EXECUTION_MODE="cli"), MagicMock(), repo=None)
        prompt = manager._build_coding_prompt(
            {"task_id": "t", "title": "probe", "description": "d",
             "params": {}},
            {"project": {"repos": []}, "dev": {}})
        self.assertIn(DESIGN_BAR, prompt)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


class TierSDesignTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.name", "Test")
        _git(self.repo, "config", "user.email", "test@example.invalid")
        (self.repo / "tracked.txt").write_text("before\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-qm", "base")
        runs = InMemoryRunStore()
        await runs.create_run({"run_id": "run", "task_id": "t",
                               "status": "running"})
        self.cycle = TierSCycle(runs, InMemoryLockStore(),
                                InMemoryTaskStore(), MagicMock())
        self.ctx = _Ctx(
            ticket={"task_id": "t", "ref": "t", "title": "probe",
                    "description": "d", "params": {}},
            repo=self.repo, ref="t", run_id="run", branch="tier-s/t-wip",
            base_branch="main",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400,
                     "hard_brake_factor": 2, "gate_retries": 2},
            timeouts={"build": 60}, waiting_cfg={}, auto_merge=False,
            budget_exclude=[], gate_specs=[], roles={})

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_builder_prompt_carries_the_bar(self) -> None:
        plan = Plan(
            ticket="t", objective="", goal="implement", acceptance=("ok",),
            files=(PlannedFile(path="tracked.txt", operation="modify",
                               reason="x"),),
            contracts=(), decisions=(), steps=("implement",),
            estimate_files=1, estimate_lines=10, out_of_scope=())
        self.assertIn(DESIGN_BAR, self.cycle._builder_prompt(self.ctx, plan))

    async def test_planner_leaves_the_composition_to_the_builder(self) -> None:
        prompt = self.cycle._planner_prompt(self.ctx)
        self.assertIn("leave the visual composition to the builder", prompt)
        self.assertIn("never plan copying an existing screen's markup",
                      prompt)

    def test_planner_contract_keeps_only_engineering_decisions(self) -> None:
        text = with_role_contract("planner", "x")
        self.assertIn("no engineering decision of its own", text)
        self.assertIn("visual composition stays with the builder", text)
        self.assertNotIn("no design decision", text)


class ObjectivePlannerDesignTests(unittest.TestCase):
    def test_quality_guide_carries_the_design_bar(self) -> None:
        prompt = engine_mod._planner_prompt({"anchor": {}}, "", [], "", [])
        self.assertIn("carries the product-design", prompt)
        self.assertIn("parity-only port below that bar", prompt)
        self.assertLess(prompt.index("carries the product-design"),
                        prompt.index("OUTPUT: write"))

    def test_policy_section_44_carries_the_bar_in_the_prompt(self) -> None:
        cut = planner_mod.policy_prompt_text(
            POLICY.read_text(encoding="utf-8"))
        self.assertIn("Interfaces carry a product-design bar", cut)
        self.assertIn("floor, never a ceiling", cut)
        self.assertIn("reviewers stay on code (no visual review loop)", cut)
        self.assertLess(cut.index("## 44. Engineering quality bar"),
                        cut.index("Interfaces carry a product-design bar"))


if __name__ == "__main__":
    unittest.main()
