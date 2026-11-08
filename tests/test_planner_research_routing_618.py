"""Planner routes internet/library/tech questions to research (ticket #618).

RESEARCH ROUTING (``routing_policy.d/research.md`` §§45-49) puts the live
quick base to use: every internet, library or technology question routes
to ``research`` with the ``investigar`` skill, quick live by default,
deep selected only by explicit criteria (live deep execution belongs
to the deep stretch), the brief attached to the requesting ticket and
the result back to the planner. Verified with a simulated service and
a scripted planner: no live models, no network.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from config_loader import ConfigLoader  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _planned_params,
    objective_scratch,
    plan_objective,
)
from objectives.planner_models import PlannedTicket  # noqa: E402
from research.contracts import ResearchJobRequest  # noqa: E402
from research.policies import (  # noqa: E402
    RESEARCH_READ,
    RESEARCH_RUN,
    Principal,
    ResearchPolicyError,
    derive_effective_policy,
)
from research.ticket_bridge import TicketResearchBridge  # noqa: E402
from settings import Settings  # noqa: E402
from test_planner_policy_split_201 import (  # noqa: E402
    _objective,
    _RecordedPlanner,
    _scratch_repo,
)
from test_research_agent_222 import _bridge  # noqa: E402

POLICY_DIR = CORE / "objectives" / "routing_policy.d"
RESEARCH_DETAIL = POLICY_DIR / "research.md"
QUESTION = "Which vector store fits our stack, with versions and limits?"


def _live_project_cfg() -> dict[str, Any]:
    return ConfigLoader(Settings(_env_file=None)).load_project(
        "jardal-openceo")


def _principal() -> Principal:
    return Principal(
        user_id="ticket:TASK-2026-00618",
        permissions=frozenset({RESEARCH_RUN, RESEARCH_READ}),
        allowed_projects=frozenset({"jardal-openceo"}),
    )


def _research_ticket() -> dict[str, Any]:
    return {
        "local_ref": "r1",
        "title": "Which vector store fits our stack?",
        "target_agent": "research",
        "skill": "investigar",
        "work_type": "research",
        "goal": QUESTION,
        "why_now": "The architecture ticket depends on this answer",
        "acceptance_criteria": [
            "Brief attached with versions, limits and sources"],
        "expected_artifacts": ["grounded research brief"],
    }


def _shipped_env(repo: Path, payloads: list[dict[str, Any]]
                 ) -> tuple[ObjectiveEnv, _RecordedPlanner,
                            list[dict[str, Any]]]:
    planner = _RecordedPlanner(repo, payloads)
    created: list[dict[str, Any]] = []

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        created.append(payload)
        return {"task_id": f"task-child-{len(created)}"}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        del task_id, updates

    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister({}),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=planner,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=AsyncMock(),
        repo=repo,
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": None,
            "agents": {"research": {"enabled": True}},
        },
    )
    return env, planner, created


class ResearchDetailShippedTest(unittest.TestCase):
    def test_details_discovered_sorted_dev_then_research(self) -> None:
        found = planner_mod.policy_detail_files()
        self.assertEqual([name for name, _ in found], ["dev", "research"])
        by_name = dict(found)
        self.assertEqual(by_name["research"], RESEARCH_DETAIL)
        self.assertTrue(RESEARCH_DETAIL.is_file())

    def test_research_detail_loads_with_the_composed_policy(self) -> None:
        details = planner_mod.load_policy_details()
        self.assertIsNotNone(details)
        assert details is not None
        self.assertEqual([name for name, _ in details], ["dev", "research"])
        composed = planner_mod.load_policy_text()
        self.assertEqual(composed.count("# RESEARCH ROUTING"), 1)

    def test_sections_45_to_49_survive_exactly_once(self) -> None:
        composed = planner_mod.load_policy_text()
        for num in range(45, 50):
            with self.subTest(section=num):
                self.assertEqual(composed.count(f"## {num}. "), 1)
        self.assertNotIn("## 45. ", planner_mod.load_policy_core())
        dev = (POLICY_DIR / "dev.md").read_text(encoding="utf-8")
        self.assertNotIn("## 45. ", dev)

    def test_check_covers_core_dev_and_research(self) -> None:
        status = planner_mod.check_policy()
        self.assertTrue(status.ok, status.problem)
        core = planner_mod.policy_file(None)
        self.assertEqual(
            tuple(status.files),
            (str(core), str(POLICY_DIR / "dev.md"), str(RESEARCH_DETAIL)))
        self.assertEqual(status.sha, planner_mod.policy_fingerprint(
            planner_mod.load_policy_text()))


class RoutingContractTest(unittest.TestCase):
    def test_detail_names_the_trigger_skill_and_flow(self) -> None:
        text = RESEARCH_DETAIL.read_text(encoding="utf-8")
        for term in ("investigar", "quick", "deep",
                     "openceo_research_submit", "openceo_research_get",
                     "openceo_attach_ticket_file", "hecho"):
            with self.subTest(term=term):
                self.assertIn(term, text)

    def test_core_points_at_the_research_detail(self) -> None:
        core = planner_mod.load_policy_core()
        self.assertIn("RESEARCH ROUTING", core)
        self.assertIn(
            "openceo-core/objectives/routing_policy.d/research.md", core)

    def test_planned_params_carry_the_investigar_hint(self) -> None:
        ticket = PlannedTicket(
            local_ref="r1", title="Which vector store fits?",
            target_agent="research", skill="investigar",
            work_type="research", goal=QUESTION, why_now="now",
            acceptance_criteria=["brief attached"],
            expected_artifacts=["grounded research brief"])
        params = _planned_params(ticket)
        self.assertEqual(params["skill"], "investigar")
        self.assertEqual(params["target_agent"], "research")

    def test_prompt_index_names_the_research_staged_path(self) -> None:
        block = planner_mod.policy_detail_index([
            ("dev", "/repo/.openceo/objectives/OBJ-1/policy/dev.md"),
            ("research", "/repo/.openceo/objectives/OBJ-1/policy/research.md"),
        ])
        self.assertIn(
            "research: /repo/.openceo/objectives/OBJ-1/policy/research.md",
            block)


class QuickDefaultTest(unittest.IsolatedAsyncioTestCase):
    async def test_bridge_submit_without_mode_admits_quick(self) -> None:
        bridge, _consumers, live, _woken = _bridge()
        out = await bridge.submit(
            ticket="TASK-2026-00618", project="jardal-openceo",
            purpose="pick the store", question=QUESTION)
        self.assertTrue(out["ok"], out)
        self.assertEqual(live.jobs[out["research_id"]]["mode"], "quick")

    def test_submit_signature_defaults_to_quick(self) -> None:
        params = inspect.signature(TicketResearchBridge.submit).parameters
        self.assertEqual(params["mode"].default, "quick")

    def test_investigar_recipe_defaults_to_quick(self) -> None:
        text = (REPO / "skills" / "research" / "investigar" / "SKILL.md"
                ).read_text(encoding="utf-8")
        self.assertIn("(`quick` unless the ticket", " ".join(text.split()))

    def test_live_project_is_quick_only(self) -> None:
        research = _live_project_cfg()["research"]
        self.assertTrue(research["enabled"])
        self.assertEqual(research["default_mode"], "quick")
        self.assertEqual(research["allowed_modes"], ["quick"])
        self.assertEqual(research["paid_budget_usd"], "0.00")


class DeepSelectionTest(unittest.TestCase):
    def test_detail_defines_explicit_deep_criteria(self) -> None:
        section = RESEARCH_DETAIL.read_text(
            encoding="utf-8").split("## 47.")[1].split("## 48.")[0]
        for marker in ("coverage gap", "independent verification",
                       "decision stakes", "paid budget"):
            with self.subTest(marker=marker):
                self.assertIn(marker, section)

    def test_live_deep_denied_on_the_quick_only_project(self) -> None:
        cfg = _live_project_cfg()
        settings = Settings(_env_file=None)
        quick = derive_effective_policy(
            principal=_principal(),
            request=ResearchJobRequest(
                project_key="jardal-openceo", purpose="pick",
                question=QUESTION, mode="quick"),
            project_cfg=cfg, settings=settings)
        self.assertTrue(quick.enabled)
        with self.assertRaises(ResearchPolicyError):
            derive_effective_policy(
                principal=_principal(),
                request=ResearchJobRequest(
                    project_key="jardal-openceo", purpose="pick",
                    question=QUESTION, mode="deep"),
                project_cfg=cfg, settings=settings)


class PlannerRoutesToResearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_research_ticket_carries_skill_question_and_detail(
            self) -> None:
        repo = _scratch_repo(self)
        payload = {"objective_status": "active", "state_summary": "ask first",
                   "tickets": [_research_ticket()], "roadmap_remaining": []}
        env, planner, created = _shipped_env(repo, [payload])
        await _objective(env)

        result = await plan_objective(env, "OBJ-1")

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("tickets_created"), 1)
        staged = objective_scratch(repo, "OBJ-1") / "policy" / "research.md"
        self.assertIn(str(staged), planner.prompts[0])
        self.assertEqual(staged.read_text(encoding="utf-8"),
                         RESEARCH_DETAIL.read_text(encoding="utf-8"))
        (ticket,) = created
        self.assertEqual(ticket["agent_hint"], "research")
        self.assertEqual(ticket["params"]["skill"], "investigar")
        self.assertEqual(ticket["params"]["target_agent"], "research")
        self.assertIn(QUESTION, ticket["description"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(
            stored["planning_runs"][-1]["policy_sha"],
            planner_mod.policy_fingerprint(planner_mod.load_policy_text()))


if __name__ == "__main__":
    unittest.main()
