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
  carries the quality principle;
- the Owner's mission goal and every acceptance criterion reach the CEO,
  the mission pedido and the objective planner (the CEO used to see 300
  goal and 400 criteria characters);
- a ticket that decomposes inside its objective hands its dependents to
  the replacements the next plan creates, and ``converted`` never
  releases a dependent (MIS-8C06FB6C #541 waited on converted #540).

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
from ceo.review import (  # noqa: E402
    CEOSignals,
    _ceo_prompt,
    _mission_digest,
    _missions_section,
)
from company_mandate import MANDATE_PRINCIPLES, load_mandate_file  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from objectives import missions as missions_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from test_company_objectives import (  # noqa: E402
    _child,
    _dev_ticket,
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


def _long_mission() -> dict:
    criteria = [f"Criterion {index}: " + "x" * 260 for index in range(1, 13)]
    return {"mission_id": "MIS-T", "project": "erp", "status": "active",
            "goal": "Rebuild the web UI " + "y" * 900,
            "acceptance": criteria, "objectives": []}


class MissionBriefTests(unittest.IsolatedAsyncioTestCase):
    def test_brief_keeps_every_criterion_of_a_large_mission(self) -> None:
        lines = missions_mod.mission_brief_lines(_long_mission())
        text = "\n".join(lines)
        self.assertTrue(lines[0].startswith("Goal: Rebuild the web UI"))
        self.assertIn("y" * 900, text)
        for index in range(1, 13):
            self.assertIn(f"{index}. Criterion {index}:", text)
        self.assertNotIn("more criteria", text)

    def test_brief_counts_what_the_budget_cuts(self) -> None:
        lines = missions_mod.mission_brief_lines(
            _long_mission(), goal_chars=50, criteria_chars=700)
        self.assertTrue(lines[0].endswith("…"))
        self.assertLessEqual(len(lines[0]), len("Goal: ") + 50)
        self.assertIn("1. Criterion 1:", lines[2])
        self.assertTrue(any(line.endswith("…") for line in lines[3:]))
        self.assertEqual(lines[-1], "(+9 more criteria past the prompt budget)")

    def test_brief_without_criteria(self) -> None:
        self.assertEqual(
            missions_mod.mission_brief_lines({"goal": "g"}),
            ["Goal: g", "Acceptance: (none yet)"])

    async def test_objective_planner_sees_the_whole_mission(self) -> None:
        store = missions_mod.InMemoryMissionStore()
        await store.create_mission(_long_mission())
        env = SimpleNamespace(missions=store)
        section = await engine_mod._objective_mission_section(
            env, {"objective_id": "OBJ-1", "mission_id": "MIS-T"})
        self.assertIn("MISSION MIS-T", section)
        self.assertIn("12. Criterion 12:", section)
        prompt = engine_mod._planner_prompt(
            {"objective_id": "OBJ-1",
             "anchor": {"goal": "g", "acceptance": ["a"]}},
            "", [], "", [], mission_md=section)
        self.assertLess(prompt.index("ACCEPTANCE:"),
                        prompt.index("MISSION MIS-T"))
        self.assertLess(prompt.index("MISSION MIS-T"),
                        prompt.index("CAPSULE (compiled state):"))
        self.assertEqual(await engine_mod._objective_mission_section(
            env, {"objective_id": "OBJ-2"}), "")

    async def test_plan_objective_sends_the_mission_to_the_planner(self) -> None:
        env, planner, _created, _patches, _notices = _env_for(
            self, _scratch_repo(self), [_plan_result()])
        env.missions = missions_mod.InMemoryMissionStore()
        await env.missions.create_mission(_long_mission())
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "mission_id": "MIS-T",
        })
        await engine_mod.plan_objective(env, "OBJ-1")
        self.assertTrue(planner.prompts)
        self.assertIn("MISSION MIS-T", planner.prompts[0])
        self.assertIn("12. Criterion 12:", planner.prompts[0])

    async def test_mission_pedido_sees_the_whole_mission(self) -> None:
        store = missions_mod.InMemoryMissionStore()
        await store.create_mission(_long_mission())
        section = await engine_mod._mission_guide_section(
            SimpleNamespace(missions=store),
            {"task_id": "t", "params": {"mission_id": "MIS-T"}})
        self.assertIn("12. Criterion 12:", section)
        self.assertIn("y" * 900, section)

    async def test_ceo_sees_the_whole_mission(self) -> None:
        mission = {**_long_mission(), "acceptance_source": "owner",
                   "acceptance_status": "confirmed"}
        signal = await _mission_digest(SimpleNamespace(), mission)
        section = _missions_section(CEOSignals(missions=[signal]))
        self.assertIn("MIS-T [erp] (active); acceptance owner/confirmed",
                      section)
        self.assertIn("12. Criterion 12:", section)
        self.assertIn("y" * 900, section)


class DecompositionRewireTests(unittest.IsolatedAsyncioTestCase):
    async def _world(self, payloads):
        tasks = {
            "task-big": _mirrored("task-big", "TASK-2026-00107",
                                  objective_id="OBJ-1"),
            "task-dep": {**_mirrored("task-dep", "TASK-2026-00108",
                                     objective_id="OBJ-1", status="queued"),
                         "blocked_by": ["task-big"]},
        }
        env, _planner, created, patches, _notices = _env_for(
            self, _scratch_repo(self), payloads, tasks=tasks)
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        return env, tasks, created, patches

    async def test_dependents_wait_for_the_replacements(self) -> None:
        env, tasks, created, patches = await self._world([_plan_result(
            _dev_ticket("part-a", "Part A"), _dev_ticket("part-b", "Part B"))])
        await engine_mod.convert_ticket(
            env, tasks["task-big"], "TOO_BIG", "split in two")
        self.assertEqual(patches["task-big"]["status"], "converted")
        self.assertEqual(len(created), 2)
        self.assertEqual(patches["task-dep"]["blocked_by"],
                         ["task-new-1", "task-new-2"])
        stored = await env.objectives.get_objective("OBJ-1")
        self.assertEqual(stored.get("pending_rewires"), [])

    async def test_a_plan_without_tickets_keeps_the_rewire_pending(self) -> None:
        env, tasks, created, patches = await self._world(
            [_plan_result(status="blocked")])
        await engine_mod.convert_ticket(
            env, tasks["task-big"], "TOO_BIG", "")
        self.assertEqual(created, [])
        self.assertNotIn("blocked_by", patches.get("task-dep", {}))
        stored = await env.objectives.get_objective("OBJ-1")
        self.assertEqual(stored.get("pending_rewires"),
                         [{"from": "task-big", "dependents": ["task-dep"]}])

    async def test_an_empty_run_never_strips_the_blocker(self) -> None:
        env, tasks, _created, patches = await self._world([_plan_result()])
        pending = [{"from": "task-big", "dependents": ["task-dep"]}]
        await env.objectives.patch_objective(
            "OBJ-1", {"pending_rewires": pending})
        await engine_mod._apply_pending_rewires(env, "OBJ-1", [])
        self.assertNotIn("task-dep", patches)
        stored = await env.objectives.get_objective("OBJ-1")
        self.assertEqual(stored.get("pending_rewires"), pending)

    async def test_converted_blocker_never_releases_a_dependent(self) -> None:
        lister = engine_mod.InMemoryTaskLister(
            {"x": {"task_id": "x", "status": "converted"}})
        env = SimpleNamespace(tasks=lister)
        ticket = {"status": "queued", "blocked_by": ["x"]}
        self.assertTrue(await engine_mod._blocked(env, ticket))
        lister.tasks["x"]["status"] = "completed"
        self.assertFalse(await engine_mod._blocked(env, ticket))


if __name__ == "__main__":
    unittest.main()
