"""Conditional research-first planning with simulated services; no network/models."""

from __future__ import annotations

import base64
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "openceo-core"))
sys.path.insert(0, str(ROOT / "tests"))

from mcp_identity import McpDenied, TicketGrant, TokenKind, box_for_agent  # noqa: E402
from catalog import load_catalog  # noqa: E402
from objectives import engine as engine  # noqa: E402
from objectives.planner_models import PlannedTicket, PlannerResult, validate_planner_result  # noqa: E402
from objectives.service import ObjectiveErpAdapter  # noqa: E402
from test_research_agent_222 import _bridge  # noqa: E402
from test_single_planner_204 import _RecordedPlanner  # noqa: E402


def _research(**extra: Any) -> dict[str, Any]:
    return dict(local_ref="research", title="Verify provider retry semantics",
                target_agent="research", work_type="research", skill="investigar",
                goal="Which retry policy is supported?", why_now="Unlock safe retries",
                acceptance_criteria=["Private grounded brief"],
                expected_artifacts=["research brief"], **extra)


def _dev(**extra: Any) -> dict[str, Any]:
    return dict(local_ref="dev", title="Implement safe retries", target_agent="dev",
                work_type="implementation", goal="Implement retries", why_now="Safe delivery",
                acceptance_criteria=["Retries preserve idempotency"],
                expected_artifacts=["implementation"], coding_tool="tier_s",
                tier_reason="Consensus protects idempotency",
                why_consensus_is_needed="Different models challenge failure modes",
                why_single_session_is_insufficient="One model can miss duplicate charges",
                **extra)


def _plan(*tickets: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = dict(objective_status="active", state_summary="Next justified work",
                  tickets=list(tickets), roadmap_remaining=[])
    result.update(extra)
    return result


class _Files:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}
        self.descriptions: dict[str, str] = {}
        self.events: list[str] = []
        self.fail_copy = False

    async def update_ticket(self, target: str, **kwargs: Any) -> dict[str, Any]:
        self.descriptions[target] = kwargs["description"]
        return {"ok": True}

    async def thread_of(self, source: str, **_: Any) -> dict[str, Any]:
        return {"attachments": [copy.deepcopy(item) for item in self.files.values()
                                if item["attached_to"] == source]}

    async def get_attachment(self, key: str, **_: Any) -> dict[str, Any]:
        self.events.append("read:" + key)
        return {"ok": True, "attachment": copy.deepcopy(self.files[key])}

    async def attach_file(self, target: str, **kwargs: Any) -> dict[str, Any]:
        self.events.append("copy:" + target)
        if self.fail_copy:
            raise RuntimeError("simulated storage failure")
        key = "copy-" + target
        item = dict(id=key, file_name=kwargs["file_name"], private=kwargs["private"],
                    attached_to=target, content_text=base64.b64decode(
                        kwargs["content_base64"]).decode("utf-8"))
        self.files[key] = item
        return {"ok": True, "attachment": copy.deepcopy(item),
                "ticket": {"name": target}}


class ResearchFirstTests(unittest.IsolatedAsyncioTestCase):
    def _env(self, *plans: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name)
        tasks: dict[str, dict[str, Any]] = {}
        created: list[dict[str, Any]] = []
        files = _Files()
        planner = _RecordedPlanner(repo, list(plans))

        async def create(payload: dict[str, Any]) -> dict[str, Any]:
            row = dict(copy.deepcopy(payload), task_id=f"child-{len(created) + 1}",
                       status="queued")
            params = dict(row.get("params") or {})
            params["_mirrors"] = {
                "erpnext": {"name": f"TASK-{row['task_id']}".upper()}}
            row["params"] = params
            created.append(copy.deepcopy(row))
            tasks[row["task_id"]] = row
            files.events.append("create:" + row["agent_hint"])
            return row

        async def patch(task_id: str, changes: dict[str, Any]) -> None:
            tasks[task_id].update(copy.deepcopy(changes))
            if changes.get("autorun"):
                files.events.append("release:" + task_id)

        env = engine.ObjectiveEnv(
            objectives=engine.InMemoryObjectiveStore(),
            tasks=engine.InMemoryTaskLister(tasks), create_ticket=create,
            patch_task=patch, executor=planner, quota=MagicMock(), slots=MagicMock(),
            reviews=None, inbox=None, notify=AsyncMock(), repo=repo,
            erp=ObjectiveErpAdapter(files), dispatcher_wake=MagicMock(),
            planner_cfg={"agents": {"dev": {"enabled": True},
                                    "research": {"enabled": True}}})
        return env, planner, created, files

    async def _objective(self, env: Any) -> None:
        await env.objectives.create_objective({
            "objective_id": "OBJ-642", "project": "jardal-openceo", "status": "active",
            "anchor": {"goal": "Safe retries", "acceptance": ["Idempotent"]},
            "capsule": {}, "tickets_log": []})

    def _completed_brief(self, env: Any, files: _Files, task_id: str = "research") -> None:
        env.tasks.tasks[task_id] = dict(
            task_id=task_id, objective_id="OBJ-642", status="completed",
            title="Verify provider", agent_hint="research",
            params={"_mirrors": {"erpnext": {"name": "TASK-RESEARCH"}}})
        files.files["brief-642"] = dict(
            id="brief-642", attached_to="TASK-RESEARCH", private=True,
            file_name="brief.md", content_text="Verified: retries need the same idempotency key.")

    async def test_research_finishes_then_planner_decides_then_private_copy_before_release(self) -> None:
        decision = {"needed": True, "reason": "Reuse the idempotency key per the brief",
                    "brief_ids": ["brief-642"]}
        env, planner, created, files = self._env(
            _plan(_research(), roadmap_remaining=["Develop after retry semantics are verified"]),
            _plan(_dev(research=decision)))
        await self._objective(env)
        first = await engine.plan_objective(env, "OBJ-642")
        self.assertTrue(first["ok"], first)
        self.assertEqual([item["agent_hint"] for item in created], ["research"])
        self.assertEqual(created[0]["params"]["research_mode"], "quick")
        bridge, _, service, _ = _bridge()
        submitted = await bridge.submit(ticket="TASK-RESEARCH", project="jardal-openceo",
                                        purpose="Unlock safe retries",
                                        question="Which retry policy is supported?")
        research_id = submitted["research_id"]
        self.assertEqual(service.jobs[research_id]["mode"], "quick")
        service.jobs[research_id]["status"] = "completed"
        result = await bridge.get(ticket="TASK-RESEARCH", project="jardal-openceo",
                                  research_id=research_id)
        self.assertEqual(result["job"]["status"], "completed")
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)
        self._completed_brief(env, files, "child-1")
        closed = env.tasks.tasks["child-1"]
        await engine.on_ticket_closed(env, closed)
        self.assertEqual(planner.runs, 2)
        staged = list((env.repo / ".openceo/objectives/OBJ-642/research").glob("*.md"))
        self.assertEqual(len(staged), 1)
        self.assertIn(str(staged[0]), planner.prompts[-1])
        self.assertEqual(staged[0].read_text(), files.files["brief-642"]["content_text"])
        self.assertEqual([item["agent_hint"] for item in created], ["research", "dev"])
        self.assertIn(decision["reason"], created[-1]["description"])
        self.assertFalse(created[-1]["autorun"])
        copied = files.files["copy-TASK-CHILD-2"]
        self.assertTrue(copied["private"])
        self.assertEqual(copied["attached_to"], "TASK-CHILD-2")
        self.assertEqual(copied["content_text"], files.files["brief-642"]["content_text"])
        self.assertLess(files.events.index("read:copy-TASK-CHILD-2"),
                        files.events.index("release:child-2"))
        self.assertTrue(env.tasks.tasks["child-2"]["autorun"])
        self.assertEqual(len(service.jobs), 1)
        self.assertEqual(service.principals[0].user_id, "ticket:TASK-RESEARCH")

    async def test_no_unknown_means_no_research_for_objective_and_loose_pedido(self) -> None:
        no_need = {"needed": False, "reason": "Repo contracts fully specify retries"}
        env, planner, created, files = self._env(_plan(_dev(research=no_need)))
        await self._objective(env)
        self.assertTrue((await engine.plan_objective(env, "OBJ-642"))["ok"])
        self.assertEqual([item["agent_hint"] for item in created], ["dev"])
        self.assertEqual(files.files, {})
        pedido = dict(task_id="loose", title="Safe retries", project="jardal-openceo",
                        params={"_mirrors": {"erpnext": {"name": "TASK-LOOSE"}}})
        env.tasks.tasks["loose"] = pedido
        out = await engine.plan_pedido(env, pedido)
        self.assertEqual(out["action"], "decided")
        self.assertEqual(out["decision"]["coding_tool"], "tier_s")
        self.assertEqual(len(created), 1)

    async def test_loose_research_first_converts_and_retains_development_goal(self) -> None:
        env, planner, created, _ = self._env(
            _plan(objective_status="capability_gap", state_summary="External retry semantics before DEV"),
            _plan(_research(), roadmap_remaining=["Develop safe retries after research"]))
        pedido = dict(task_id="loose", title="Build safe retries", description="Deliver idempotency",
                      project="jardal-openceo", acceptance=["Idempotent retries"], params={})
        env.tasks.tasks["loose"] = pedido
        out = await engine.plan_pedido(env, pedido)
        self.assertEqual(out["action"], "convert")
        self.assertEqual(created, [])
        objective = await engine.convert_ticket(env, pedido, "planner pedido conversion",
                                               sizing_text=out["state_summary"])
        self.assertIn(pedido["description"], objective["anchor"]["goal"])
        self.assertEqual(objective["roadmap_remaining"], ["Develop safe retries after research"])
        self.assertEqual([row["agent_hint"] for row in created], ["research"])
        self.assertEqual(planner.runs, 2)

    async def test_loose_research_with_downstream_roadmap_converts_instead_of_replacing_goal(self) -> None:
        env, _, created, _ = self._env(
            _plan(_research(), roadmap_remaining=["Develop after research"]))
        out = await engine.plan_pedido(env, dict(task_id="loose", project="jardal-openceo"))
        self.assertEqual(out["action"], "convert")
        self.assertEqual(created, [])

    async def test_explicit_loose_tier_s_can_convert_before_research(self) -> None:
        env, _, created, _ = self._env(_plan(
            _research(), roadmap_remaining=["Implement after the external answer"]))
        ticket = dict(task_id="loose-s", title="Implement retries", description="Unknown provider contract",
                      coding_tool="tier_s", project="jardal-openceo", params={})
        env.tasks.tasks["loose-s"] = ticket
        objective = await engine.convert_ticket(env, ticket, "§9 external information prerequisite")
        self.assertEqual(env.tasks.tasks["loose-s"]["status"], "converted")
        self.assertIn(ticket["description"], objective["anchor"]["goal"])
        self.assertEqual([row["agent_hint"] for row in created], ["research"])

    async def test_research_close_replans_even_with_unrelated_ready_work(self) -> None:
        env, planner, _, files = self._env(_plan(_research()))
        await self._objective(env)
        self._completed_brief(env, files)
        env.tasks.tasks["other"] = dict(task_id="other", objective_id="OBJ-642",
                                        title="Unrelated", status="queued", coding_tool="tier_c")
        await engine.on_ticket_closed(env, env.tasks.tasks["research"])
        self.assertEqual(planner.runs, 1)
        self.assertIn("brief-642", planner.prompts[0])

    async def test_copy_failure_holds_dev_execution(self) -> None:
        decision = dict(needed=True, reason="Use verified retry contract", brief_ids=["brief-642"])
        env, _, created, files = self._env(_plan(_dev(research=decision)))
        await self._objective(env)
        self._completed_brief(env, files)
        files.fail_copy = True
        with self.assertRaisesRegex(RuntimeError, "storage failure"):
            await engine.plan_objective(env, "OBJ-642")
        self.assertFalse(created[0]["autorun"])
        self.assertEqual(env.tasks.tasks["child-1"]["status"], "waiting")
        env.dispatcher_wake.assert_not_called()

    async def test_adoption_attaches_brief_before_filling_tier_s_contract(self) -> None:
        decision = dict(needed=True, reason="Use verified retries", brief_ids=["brief-642"])
        env, _, created, files = self._env(_plan(_dev(
            research=decision, completes_task_id="stub")))
        await self._objective(env)
        self._completed_brief(env, files)
        env.tasks.tasks["stub"] = dict(
            task_id="stub", objective_id="OBJ-642",
            params={"_mirrors": {"erpnext": {"name": "TASK-STUB"}}},
            status="waiting", waiting_reason="routing_missing: tier")
        out = await engine.plan_objective(env, "OBJ-642")
        self.assertTrue(out["ok"], out)
        self.assertEqual(created, [])
        self.assertTrue(files.files["copy-TASK-STUB"]["private"])
        self.assertEqual(env.tasks.tasks["stub"]["coding_tool"], "tier_s")
        self.assertIn(decision["reason"], env.tasks.tasks["stub"]["description"])
        self.assertIn(decision["reason"], files.descriptions["TASK-STUB"])

    async def test_deep_selection_reaches_simulated_service_without_downgrade(self) -> None:
        parsed = PlannedTicket.model_validate(_research(research_mode="deep", deep_criteria=dict(
            coverage_gap="Prior quick brief lacks retries", independent_verification="Conflicting docs",
            decision_stakes="Duplicate charges", paid_budget_usd="1.00")))
        env, _, _, files = self._env(_plan(parsed.model_dump(mode="json")))
        ticket = dict(task_id="loose", project="jardal-openceo", description="Verify provider contract",
                      params={"_mirrors": {"erpnext": {"name": "TASK-LOOSE"}}})
        original_description = ticket["description"]
        env.tasks.tasks["loose"] = ticket
        decision = await engine.plan_pedido(env, ticket)
        self.assertEqual(decision["action"], "decided")
        patched = env.tasks.tasks["loose"]
        for key in parsed.deep_criteria.model_dump():
            self.assertIn(key, patched["description"])
        self.assertIn(original_description, patched["description"])
        self.assertEqual(patched["params"]["research_mode"], "deep")
        self.assertEqual(files.descriptions["TASK-LOOSE"], patched["description"])
        bridge, _, service, _ = _bridge()
        out = await bridge.submit(ticket="TASK-RESEARCH", project="jardal-openceo",
                                  purpose=parsed.why_now, question=parsed.goal,
                                  mode=parsed.research_mode)
        self.assertTrue(out["ok"], out)
        self.assertEqual(service.jobs[out["research_id"]]["mode"], "deep")

    async def test_pedido_without_business_mirror_fails_closed(self) -> None:
        env, _, _, files = self._env(_plan(_research()))
        ticket = dict(task_id="loose", project="jardal-openceo",
                      description="Verify provider contract")
        env.tasks.tasks["loose"] = ticket
        with self.assertRaises(engine.ObjectiveErpError):
            await engine.plan_pedido(env, ticket)
        self.assertEqual(files.descriptions, {})

    async def test_only_private_briefs_from_completed_research_in_same_objective_qualify(self) -> None:
        decision = dict(needed=True, reason="Use the brief", brief_ids=["brief-642"])
        for variation in ("public", "unfinished", "other_objective", "wrong_source", "not_research"):
            with self.subTest(variation=variation):
                env, _, created, files = self._env(_plan(_dev(research=decision)))
                await self._objective(env)
                self._completed_brief(env, files)
                if variation == "public":
                    files.files["brief-642"]["private"] = False
                elif variation == "unfinished":
                    env.tasks.tasks["research"]["status"] = "queued"
                elif variation == "other_objective":
                    env.tasks.tasks["research"]["objective_id"] = "OBJ-OTHER"
                elif variation == "wrong_source":
                    files.files["brief-642"]["attached_to"] = "TASK-OTHER"
                else:
                    env.tasks.tasks["research"]["agent_hint"] = "dev"
                out = await engine.plan_objective(env, "OBJ-642")
                self.assertFalse(out["ok"], out)
                self.assertEqual(created, [])

    async def test_precreating_downstream_tier_s_blocked_by_research_is_rejected(self) -> None:
        for plan, pending in (
                (_plan(_research(), _dev(blocked_by=["research"])), ()),
                (_plan(_dev(blocked_by=["prior-research"])), ("prior-research",))):
            errors = await validate_planner_result(
                PlannerResult.model_validate(plan),
                agents={"dev": {"enabled": True}, "research": {"enabled": True}},
                open_titles=[], task_exists=AsyncMock(return_value=True),
                pending_research=pending)
            self.assertTrue(any("research first" in error for error in errors), errors)


class ContractTests(unittest.TestCase):
    def test_unresolved_external_dependency_cannot_become_dev(self) -> None:
        with self.assertRaises(ValidationError):
            PlannedTicket.model_validate(_dev(research=dict(needed=True, reason="Unknown retries")))

    def test_deep_requires_all_named_criteria_and_positive_budget(self) -> None:
        criteria = dict(coverage_gap="Quick brief lacks webhook semantics",
                        independent_verification="Conflicting retry docs",
                        decision_stakes="Duplicate charges", paid_budget_usd="1.00")
        for missing in criteria:
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                PlannedTicket.model_validate(_research(research_mode="deep", deep_criteria={
                    key: value for key, value in criteria.items() if key != missing}))
        with self.assertRaises(ValidationError):
            PlannedTicket.model_validate(_research(research_mode="deep"))
        for budget in ("0", "-1", "NaN", True, 1.01):
            with self.subTest(budget=budget), self.assertRaises(ValidationError):
                PlannedTicket.model_validate(_research(research_mode="deep", deep_criteria={
                    **criteria, "paid_budget_usd": budget}))
        parsed = PlannedTicket.model_validate(_research(research_mode="deep", deep_criteria=criteria))
        text = engine._planned_description(parsed)
        self.assertEqual(engine._planned_params(parsed)["research_mode"], "deep")
        for key in criteria:
            self.assertIn(key, text)

    def test_dev_cannot_spend_research_tokens_even_with_overbroad_badge(self) -> None:
        tools = {"openceo_research_submit", "openceo_research_get"}
        self.assertFalse(tools.intersection(box_for_agent(load_catalog().agents["dev"])))
        for kind in (TokenKind.AGENT, TokenKind.SERVICE, TokenKind.READONLY):
            for tool in tools:
                grant = TicketGrant(kind=kind, agent="dev", ticket="TASK-DEV",
                                    project="jardal-openceo", tools=frozenset(tools))
                with self.subTest(tool=tool, kind=kind), self.assertRaises(McpDenied):
                    grant.check(tool, dict(ticket="TASK-DEV", project="jardal-openceo"))


if __name__ == "__main__":
    unittest.main()
