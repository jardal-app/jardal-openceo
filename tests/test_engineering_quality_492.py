"""Engineering quality bar and planner-decided review (TASK-2026-00492).

MIS-4804F506 shipped a hand-rolled UI because every prompt rewarded the
smallest change: the CEO and planner contracted "reuse existing web",
"restyle only", "no new framework"; no builder verified the rendered
result; and only an explicit Owner order could skip a review round.
Contract under test:

- every dev builder prompt (simple tiers and Tier S) carries the
  engineering standard; the Tier S planner never bans dependencies alone;
- the objective planner prompt and the normative policy carry the quality
  bar (§44) inside the prompt budget;
- the planner decides ``review: off`` for A/B/C with ``review_reason``;
  Tier S still rejects off;
- reviewers focus on user-visible breakage, the final review asks for
  production grade, the CEO never prescribes shortcuts, and the mandate
  carries the quality principle.

No network, no CLI, no LLM.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

REPO = Path(__file__).resolve().parents[1]
for _path in (REPO / "openceo-core", REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from agents.dev.builder_contract import (  # noqa: E402
    ENGINEERING_STANDARD,
    engineering_standard_prompt,
)
from agents.dev.session import DevSessionManager  # noqa: E402
from agents.dev.tier_s.cycle import TierSCycle, _Ctx  # noqa: E402
from agents.dev.tier_s.plan import Plan, PlannedFile  # noqa: E402
from agents.dev.tier_s.stores import (  # noqa: E402
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from ceo.review import CEOSignals, _ceo_prompt  # noqa: E402
from company_mandate import MANDATE_PRINCIPLES, load_mandate_file  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from test_company_objectives import (  # noqa: E402
    _child,
    _env_for,
    _mirrored,
    _plan_result,
    _scratch_repo,
)

POLICY = REPO / "openceo-core" / "objectives" / "routing_policy.md"
DEV_POLICY = REPO / "openceo-core" / "objectives" / "routing_policy.d" / "dev.md"


def _planned(**overrides):
    base = {
        "local_ref": "t1", "title": "ship it", "target_agent": "dev",
        "work_type": "implementation", "goal": "ship it",
        "why_now": "unblocks the objective",
        "acceptance_criteria": ["it works"], "expected_artifacts": ["code"],
        "coding_tool": "tier_c", "tier_reason": "specified single session",
    }
    base.update(overrides)
    return base


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


class EngineeringStandardTests(unittest.TestCase):
    def test_standard_names_the_four_failure_modes_for_any_stack(self) -> None:
        text = engineering_standard_prompt()
        self.assertTrue(text.startswith("ESTÁNDAR DE INGENIERÍA"))
        self.assertIn("biblioteca o el framework estándar y mantenido", text)
        self.assertIn("No escribas un sustituto casero", text)
        self.assertIn("no obligan a perpetuar una base", text)
        self.assertIn("sin prefijos de marca", text)
        self.assertIn("navegador real", text)
        self.assertIn("Una migración se aplica y se revierte", text)
        self.assertIn("no fijan detalles de implementación", text)
        self.assertEqual(text, "\n".join(ENGINEERING_STANDARD))

    def test_simple_tier_prompt_carries_the_standard_before_the_verdict(self) -> None:
        manager = DevSessionManager(
            SimpleNamespace(DEV_EXECUTION_MODE="cli"), MagicMock(), repo=None)
        prompt = manager._build_coding_prompt(
            {"task_id": "t", "title": "probe", "description": "d",
             "params": {}},
            {"project": {"repos": []}, "dev": {}})
        standard = engineering_standard_prompt()
        self.assertIn(standard, prompt)
        self.assertLess(prompt.index(standard),
                        prompt.index("RESULTADO DEL AGENTE"))
        self.assertNotIn("smoke test/browser exigido por AGENTS.md", prompt)
        self.assertIn("verifícalas renderizadas", prompt)


class TierSPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import tempfile
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

    async def test_builder_prompt_carries_the_standard(self) -> None:
        plan = Plan(
            ticket="t", objective="", goal="implement", acceptance=("ok",),
            files=(PlannedFile(path="tracked.txt", operation="modify",
                               reason="x"),),
            contracts=(), decisions=(), steps=("implement",),
            estimate_files=1, estimate_lines=10, out_of_scope=())
        prompt = self.cycle._builder_prompt(self.ctx, plan)
        standard = engineering_standard_prompt()
        self.assertIn(standard, prompt)
        self.assertLess(prompt.index(standard),
                        prompt.index("RESULTADO DEL AGENTE"))

    async def test_planner_never_bans_dependencies_alone(self) -> None:
        prompt = self.cycle._planner_prompt(self.ctx)
        self.assertIn("standard maintained libraries", prompt)
        self.assertIn("never decide 'no new dependencies'", prompt)


class PlannerQualityTests(unittest.TestCase):
    def test_planner_prompt_carries_the_quality_guide(self) -> None:
        prompt = engine_mod._planner_prompt({"anchor": {}}, "", [], "", [])
        self.assertIn("ENGINEERING QUALITY (policy §44", prompt)
        self.assertIn("never 'restyle only' or 'minimal diff'", prompt)
        self.assertIn("must exist", prompt)
        self.assertIn('"review_reason?"', prompt)
        self.assertLess(prompt.index("ENGINEERING QUALITY"),
                        prompt.index("OUTPUT: write"))

    def test_pedido_prompt_describes_the_decided_switch(self) -> None:
        prompt = engine_mod._pedido_prompt(
            {"task_id": "t-1", "title": "t", "description": "d"})
        self.assertIn('"review_reason?"', prompt)
        self.assertIn("tier_s always reviews", prompt)

    def test_policy_section_44_reaches_the_prompt(self) -> None:
        text = POLICY.read_text(encoding="utf-8")
        cut = planner_mod.policy_prompt_text(text)
        self.assertIn("## 44. Engineering quality bar", cut)
        self.assertIn("20. Hold every ticket contract to the engineering "
                      "quality bar (§44).", cut)
        self.assertIn("reuse what meets the bar", cut)
        self.assertNotIn("reuse what exists, and never", cut)

    def test_dev_policy_lets_the_planner_decide_review(self) -> None:
        text = DEV_POLICY.read_text(encoding="utf-8")
        self.assertIn("the planner decides it per ticket", text)
        self.assertIn("`review_reason`", text)
        self.assertIn("Tier S\nalways reviews and rejects off", text)
        self.assertNotIn("an explicit user instruction (`review: off`",
                         text)


class ReviewReasonContractTests(unittest.TestCase):
    def test_off_requires_a_reason(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            planner_mod.PlannedTicket(**_planned(review="off"))
        self.assertIn("review=off requires review_reason",
                      str(raised.exception))

    def test_off_with_reason_is_accepted_and_reaches_params(self) -> None:
        ticket = planner_mod.PlannedTicket(**_planned(
            review="off", review_reason="documentation only"))
        params = engine_mod._planned_params(ticket)
        self.assertEqual(params["review_reason"], "documentation only")

    def test_on_and_default_need_no_reason(self) -> None:
        for review in ("on", None):
            with self.subTest(review=review):
                ticket = planner_mod.PlannedTicket(**_planned(review=review))
                self.assertNotIn("review_reason",
                                 engine_mod._planned_params(ticket))

    def test_tier_s_still_rejects_off_even_with_reason(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            planner_mod.PlannedTicket(**_planned(
                coding_tool="tier_s",
                why_consensus_is_needed="independent families",
                why_single_session_is_insufficient="one session drifts",
                review="off", review_reason="looks simple"))
        self.assertIn("rejects review=off", str(raised.exception))

    def test_non_dev_rejects_review_reason(self) -> None:
        with self.assertRaises(ValidationError) as raised:
            planner_mod.PlannedTicket(**_planned(
                target_agent="secretary", coding_tool=None,
                tier_reason=None, review_reason="x"))
        self.assertIn("review_reason is only valid for DEV tickets",
                      str(raised.exception))

    def test_blank_reason_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            planner_mod.PlannedTicket(**_planned(
                review="off", review_reason="  "))


class ReviewerPromptTests(unittest.TestCase):
    def test_reviewer_prompt_focuses_on_user_visible_breakage(self) -> None:
        prompt = review_mod.reviewer_prompt("r1", "REQ.md", "ticket", 1)
        self.assertIn("Focus on what breaks for users and callers", prompt)
        self.assertIn("navigation and history", prompt)
        self.assertIn("hand-written substitute for a standard maintained "
                      "library", prompt)
        self.assertIn("The first line is a finding header or `No findings.`",
                      prompt)


class FinalReviewRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_review_request_asks_for_production_grade(self) -> None:
        tasks = {
            "task-parent": _mirrored("task-parent", "TASK-1",
                                     status="converted", objective_id="OBJ-1"),
            "kid-1": _child("kid-1", "completed", dev=True),
        }
        env, _planner, _created, _patches, _notices = _env_for(
            self, _scratch_repo(self), [_plan_result()], tasks=tasks,
            objectives_cfg={"max_final_reviews": 2})
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent", "final_reviews": 0,
        })
        seen: list[str] = []

        async def reviewer(_env, _slot, request, *_args, **_kwargs):
            seen.append(request)
            return {"kind": "unavailable"}

        async def one_round(_pool, _slots, _plan, run_one, **_kwargs):
            await run_one(SimpleNamespace(label="probe"), 1, None)
            return SimpleNamespace(met=False, summary=lambda: "probe")

        with patch.object(engine_mod, "_run_final_reviewer", reviewer), \
                patch.object(engine_mod.review_mod, "run_quorum_round",
                             one_round):
            result = await engine_mod.run_final_review(env, "OBJ-1")

        self.assertEqual(result.get("reason"), "no_quorum")
        self.assertEqual(len(seen), 1)
        self.assertIn("OBJECTIVE FINAL REVIEW", seen[0])
        self.assertIn("production-grade delivery: standard maintained "
                      "libraries instead of hand-written substitutes", seen[0])
        self.assertIn("verified the way users meet them", seen[0])


class CeoAndMandateTests(unittest.TestCase):
    def test_ceo_never_prescribes_shortcuts(self) -> None:
        prompt = _ceo_prompt(
            CEOSignals(principios=list(MANDATE_PRINCIPLES)),
            kind="scheduled", reasons=[], review_id="ceo-test",
            repo_root=REPO, ops_project="ops")
        self.assertIn("Describe outcomes at production grade", prompt)
        self.assertIn("Brand words describe the product", prompt)

    def test_mandate_carries_quality_and_qualified_reuse(self) -> None:
        mandate = load_mandate_file(REPO / "projects" / "company_mandate.yaml")
        principles = list(mandate.principios)
        self.assertEqual(principles, list(MANDATE_PRINCIPLES))
        self.assertIn("reutilizar lo que ya existe cuando cumple el "
                      "estándar profesional", principles)
        self.assertTrue(any(item.startswith("entregar calidad profesional")
                            for item in principles))
        self.assertNotIn("reutilizar lo que ya existe", principles)


if __name__ == "__main__":
    unittest.main()
