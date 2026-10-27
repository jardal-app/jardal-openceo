"""Spend declared in the plan: mandate check before launch (TASK-2026-00207).

The planner (and explicit requests) declare costs per ticket or objective;
a deterministic check calls requires_owner before anything persists or
launches. New spend waits for the Owner through the single human channel.
Unknown cost is never zero: it escalates. No tokens.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from company_mandate import parse_mandate  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    apply_approved_plan,
    plan_objective,
)
from objectives.plan_spend import (  # noqa: E402
    PlanSpendError,
    check_plan_spend,
    collect_plan_costs,
    parse_costs,
    spend_guide_section,
)
from owner_channel import (  # noqa: E402
    OWNER_QUESTION_PHASE,
    owner_question_block,
    question_answered,
)


def _mandate(threshold: str = "0.00") -> Any:
    return parse_mandate({
        "version": 1,
        "ejes": [],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": threshold,
            "cuota": "suscripciones_ya_pagadas",
        },
        "limites": {
            "objetivos_activos": 3,
            "objetivos_activos_por_proyecto": 2,
            "tickets_activos_por_objetivo": 6,
        },
        "revision_ceo": {"cadencia": "semanal"},
        "escalar_al_dueno": [
            "gasto_nuevo",
            "accion_irreversible",
            "replanificacion_estrategica",
            "cambio_de_mandato",
            "revision_exigida_por_politica",
        ],
    })


class PlannedCostSchemaTests(unittest.TestCase):
    def test_valid_amounts(self) -> None:
        for amount in ("12.50", "0", 7, 0, None):
            with self.subTest(amount=amount):
                cost = planner_mod.PlannedCost(
                    label="domain", amount=amount)
                self.assertEqual(cost.label, "domain")
                self.assertEqual(cost.amount, amount)

    def test_blank_label_rejected(self) -> None:
        with self.assertRaises(ValueError):
            planner_mod.PlannedCost(label="  ", amount="1")

    def test_float_bool_negative_unparseable_rejected(self) -> None:
        for amount in (1.5, True, False, "-1", "-0.01", "mucho", "", "  "):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    planner_mod.PlannedCost(label="x", amount=amount)

    def test_costs_default_empty(self) -> None:
        ticket = planner_mod.PlannedTicket(
            local_ref="a", title="t", target_agent="dev",
            work_type="implementation", goal="g", why_now="n",
            acceptance_criteria=["a"], expected_artifacts=["e"],
            coding_tool="tier_c", tier_reason="default",
        )
        self.assertEqual(ticket.costs, [])
        result = planner_mod.PlannerResult(
            objective_status="active", state_summary="s", tickets=[ticket])
        self.assertEqual(result.costs, [])

    def test_json_round_trip(self) -> None:
        result = planner_mod.PlannerResult(
            objective_status="active", state_summary="s",
            costs=[{"label": "hosting", "amount": "20"}],
            tickets=[{
                "local_ref": "a", "title": "t", "target_agent": "dev",
                "work_type": "implementation", "goal": "g", "why_now": "n",
                "acceptance_criteria": ["a"], "expected_artifacts": ["e"],
                "coding_tool": "tier_c", "tier_reason": "default",
                "costs": [{"label": "domain", "amount": None}],
            }],
        )
        dumped = json.loads(result.model_dump_json())
        self.assertEqual(dumped["costs"], [{"label": "hosting",
                                            "amount": "20"}])
        self.assertIsNone(dumped["tickets"][0]["costs"][0]["amount"])
        back = planner_mod.PlannerResult.model_validate(dumped)
        self.assertEqual(back.costs[0].amount, "20")
        self.assertIsNone(back.tickets[0].costs[0].amount)


class ParseCostsTests(unittest.TestCase):
    def test_sums_and_details(self) -> None:
        parsed = parse_costs([
            {"label": "domain", "amount": "12.50"},
            {"label": "hosting", "amount": 20},
        ])
        self.assertEqual(parsed["total"], Decimal("32.50"))
        self.assertEqual(parsed["unknown"], [])
        self.assertEqual(parsed["details"],
                         {"domain": "12.50", "hosting": "20"})

    def test_unknown_is_collected_never_zero(self) -> None:
        parsed = parse_costs([
            {"label": "domain", "amount": "10"},
            {"label": "license", "amount": None},
        ])
        self.assertEqual(parsed["total"], Decimal("10"))
        self.assertEqual(parsed["unknown"], ["license"])
        self.assertIsNone(parsed["details"]["license"])

    def test_empty_is_zero_without_unknown(self) -> None:
        for entries in ([], None):
            with self.subTest(entries=entries):
                parsed = parse_costs(entries)
                self.assertEqual(parsed["total"], Decimal("0"))
                self.assertEqual(parsed["unknown"], [])

    def test_malformed_entries_raise(self) -> None:
        bad: list[Any] = [
            [{"label": "  ", "amount": "1"}],
            [{"label": "x", "amount": 1.5}],
            [{"label": "x", "amount": True}],
            [{"label": "x", "amount": "-3"}],
            [{"label": "x", "amount": "mucho"}],
            ["no-mapping"],
        ]
        for entries in bad:
            with self.subTest(entries=entries):
                with self.assertRaises(PlanSpendError):
                    parse_costs(entries)

    def test_non_list_raises_plan_spend_error(self) -> None:
        for entries in (5, "no-list", {"label": "x"}, b"bytes"):
            with self.subTest(entries=entries):
                with self.assertRaises(PlanSpendError):
                    parse_costs(entries)  # type: ignore[arg-type]

    def test_duplicate_labels_keep_both_details(self) -> None:
        parsed = parse_costs([
            {"label": "fee", "amount": "5"},
            {"label": "fee", "amount": "7"},
        ])
        self.assertEqual(parsed["total"], Decimal("12"))
        self.assertEqual(parsed["details"],
                         {"fee": "5", "fee (2)": "7"})


class CheckPlanSpendTests(unittest.TestCase):
    def test_no_costs_is_automatic_without_mandate(self) -> None:
        for costs in ([], None):
            with self.subTest(costs=costs):
                decision = check_plan_spend(
                    costs=costs, summary="plan", mandate=None)
                self.assertFalse(decision["escalate"])
                self.assertIsNone(decision["category"])
                self.assertEqual(decision["amount"], "0")

    def test_over_threshold_escalates_new_spend(self) -> None:
        decision = check_plan_spend(
            costs=[{"label": "domain", "amount": "12"}],
            summary="Buy the domain", mandate=_mandate("0.00"))
        self.assertTrue(decision["escalate"])
        self.assertEqual(decision["category"], "gasto_nuevo")
        self.assertEqual(decision["amount"], "12")
        self.assertEqual(decision["currency"], "USD")
        self.assertIn("Buy the domain", decision["human_action"])
        self.assertEqual(decision["unknown"], [])

    def test_under_threshold_is_automatic(self) -> None:
        decision = check_plan_spend(
            costs=[{"label": "domain", "amount": "12"}],
            summary="Buy the domain", mandate=_mandate("100.00"))
        self.assertFalse(decision["escalate"])

    def test_unknown_cost_escalates_and_is_named(self) -> None:
        decision = check_plan_spend(
            costs=[{"label": "license", "amount": None}],
            summary="Renew tooling", mandate=_mandate("100.00"))
        self.assertTrue(decision["escalate"])
        self.assertEqual(decision["category"], "gasto_nuevo")
        self.assertIn("license", decision["reason"])
        self.assertIn("license", decision["human_action"])
        self.assertEqual(decision["unknown"], ["license"])

    def test_costs_without_mandate_escalate_fail_closed(self) -> None:
        decision = check_plan_spend(
            costs=[{"label": "domain", "amount": "1"}],
            summary="Buy the domain", mandate=None)
        self.assertTrue(decision["escalate"])
        self.assertEqual(decision["category"], "mandate_unavailable")

    def test_malformed_costs_raise(self) -> None:
        with self.assertRaises(PlanSpendError):
            check_plan_spend(costs=[{"label": "x", "amount": 2.5}],
                             summary="s", mandate=_mandate())


class CollectPlanCostsTests(unittest.TestCase):
    def test_collects_objective_and_ticket_costs(self) -> None:
        result = planner_mod.PlannerResult(
            objective_status="active", state_summary="s",
            costs=[{"label": "hosting", "amount": "20"}],
            tickets=[{
                "local_ref": "a", "title": "t", "target_agent": "dev",
                "work_type": "implementation", "goal": "g", "why_now": "n",
                "acceptance_criteria": ["a"], "expected_artifacts": ["e"],
                "coding_tool": "tier_c", "tier_reason": "default",
                "costs": [{"label": "domain", "amount": "12"}],
            }],
        )
        entries = collect_plan_costs(result)
        self.assertEqual(len(entries), 2)
        parsed = parse_costs(entries)
        self.assertEqual(parsed["total"], Decimal("32"))

    def test_empty_plan_has_no_entries(self) -> None:
        result = planner_mod.PlannerResult(
            objective_status="complete_candidate", state_summary="done")
        self.assertEqual(collect_plan_costs(result), [])


class SpendGuideTests(unittest.TestCase):
    def test_names_currency_and_threshold(self) -> None:
        guide = spend_guide_section(_mandate("100.00"))
        self.assertIn("USD", guide)
        self.assertIn("100.00", guide)
        self.assertIn("costs", guide)

    def test_without_mandate_still_declares(self) -> None:
        guide = spend_guide_section(None)
        self.assertIn("costs", guide)
        self.assertIn("Owner", guide)


class PlannerPromptSpendTests(unittest.TestCase):
    def test_prompt_carries_costs_schema_and_guide(self) -> None:
        from objectives.engine import _planner_prompt

        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "",
            [], spend_guide=spend_guide_section(_mandate("0.00")))
        self.assertIn('"costs?"', prompt)
        self.assertIn('"amount": "<decimal string', prompt)
        self.assertIn("SPEND", prompt)
        self.assertIn("0.00", prompt)
        self.assertIn("unknown", prompt)


class PolicySpendTests(unittest.TestCase):
    @staticmethod
    def _flat() -> str:
        return " ".join(planner_mod.load_policy_text().split())

    def test_schema_costs_declared_in_section_16(self) -> None:
        self.assertIn("optional `costs`", self._flat())

    def test_validation_checklist_covers_costs_shape(self) -> None:
        self.assertIn("costs declaration shape", self._flat())

    def test_kernel_names_the_spend_rule(self) -> None:
        text = self._flat()
        self.assertIn("Declare every new cost", text)
        self.assertIn("Unknown cost is never zero", text)


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                   check=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo,
                   check=True)
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


def _output_target(repo: Path, prompt: str) -> Path:
    for line in prompt.splitlines():
        if line.startswith("OUTPUT: write "):
            rel = line[len("OUTPUT: write "):].split(" with exactly")[0]
            return repo / rel.strip()
    raise AssertionError("planner prompt names no OUTPUT target")


class _RecordedPlanner:
    def __init__(self, repo: Path, payloads: list[dict[str, Any]]) -> None:
        self._repo = repo
        self._payloads = payloads
        self.prompts: list[str] = []
        self.runs = 0
        self._target = None

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "objective_planner"
        self.prompts.append(prompt)
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        try:
            target = _output_target(self._repo, prompt)
            self._target = target
        except AssertionError:
            assert self._target is not None
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _spend_env(test: unittest.TestCase, repo: Path,
               payloads: list[dict[str, Any]],
               tasks: dict[str, dict[str, Any]] | None = None,
               mandate: Any = "default",
               ) -> tuple[ObjectiveEnv, _RecordedPlanner,
                          list[dict[str, Any]], dict[str, dict[str, Any]]]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: route by oracle, not quota.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    patches: dict[str, dict[str, Any]] = {}
    counter = {"n": 0}
    store = tasks if tasks is not None else {}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        task_id = f"task-child-{counter['n']}"
        created.append(dict(payload))
        store[task_id] = {
            "task_id": task_id,
            "title": payload.get("title"),
            "status": "queued",
            "params": dict(payload.get("params") or {}),
            "objective_id": payload.get("objective_id"),
        }
        return {"task_id": task_id}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(deepcopy(updates))
        if task_id in store:
            params = updates.get("params")
            if isinstance(params, dict):
                merged = dict(store[task_id].get("params") or {})
                merged.update(params)
                store[task_id]["params"] = merged
            for key, value in updates.items():
                if key != "params":
                    store[task_id][key] = value

    planner = _RecordedPlanner(repo, payloads)
    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(store),
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
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True}},
        },
        mandate=_mandate() if mandate == "default" else mandate,
    )
    return env, planner, created, patches


def _dev_ticket(local_ref: str, title: str, **overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "local_ref": local_ref,
        "title": title,
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": f"achieve {title}",
        "why_now": "next step",
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_c",
        "tier_reason": "bounded work",
    }
    ticket.update(overrides)
    return ticket


def _seed_objective(env: ObjectiveEnv, objective_id: str = "OBJ-SPEND",
                    **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "objective_id": objective_id,
        "project": "proj",
        "status": "active",
        "anchor": {"goal": "ship", "acceptance": ["done"]},
        "tickets": [],
        "planning_runs": [],
    }
    doc.update(overrides)
    env.objectives.docs[objective_id] = doc
    return doc


class PlanSpendGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_without_costs_launches(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "plain work",
            "tickets": [_dev_ticket("a", "Plain child")],
        }
        env, planner, created, _patches = _spend_env(self, repo, [payload])
        _seed_objective(env)

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["tickets_created"], 1)
        self.assertEqual(created[0]["title"], "Plain child")
        self.assertEqual(planner.runs, 1)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertNotIn("pending_plan", stored)

    async def test_plan_with_costs_holds_and_asks_the_owner(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "needs a domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        env, planner, created, patches = _spend_env(self, repo, [payload])
        _seed_objective(env)

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "spend_approval_pending")
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)
        question = created[0]
        block = question["params"]["owner_question"]
        self.assertEqual(block["kind"], "question")
        self.assertIsNone(block["origin_task_id"])
        self.assertEqual(block["checkpoint"]["objective_id"], "OBJ-SPEND")
        self.assertIn("12", question["description"])
        question_id = "task-child-1"
        self.assertEqual(patches[question_id]["status"], "waiting")
        self.assertEqual(patches[question_id]["phase"], OWNER_QUESTION_PHASE)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        pending = stored.get("pending_plan") or {}
        self.assertEqual(pending.get("question_task_id"), question_id)
        self.assertEqual(pending["spend"]["amount"], "12")

    async def test_objective_level_costs_hold_too(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "needs hosting",
            "costs": [{"label": "hosting", "amount": "20"}],
            "tickets": [_dev_ticket("a", "Plain child")],
        }
        env, _planner, created, _patches = _spend_env(self, repo, [payload])
        _seed_objective(env)

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertEqual(result.get("reason"), "spend_approval_pending")
        self.assertEqual(len(created), 1)
        self.assertIn("owner_question", created[0]["params"])

    async def test_unknown_cost_holds(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "unknown license",
            "tickets": [_dev_ticket(
                "a", "Renew license",
                costs=[{"label": "license", "amount": None}])],
        }
        env, _planner, created, _patches = _spend_env(
            self, repo, [payload], mandate=_mandate("100.00"))
        _seed_objective(env)

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertEqual(result.get("reason"), "spend_approval_pending")
        self.assertIn("license", created[0]["description"])

    async def test_under_threshold_launches(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "cheap domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        env, _planner, created, _patches = _spend_env(
            self, repo, [payload], mandate=_mandate("100.00"))
        _seed_objective(env)

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(created[0]["title"], "Buy domain")

    async def test_no_mandate_with_costs_holds(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "needs a domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        env, _planner, created, _patches = _spend_env(
            self, repo, [payload], mandate=None)
        _seed_objective(env)

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertEqual(result.get("reason"), "spend_approval_pending")

    async def test_no_mandate_without_costs_launches(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "plain work",
            "tickets": [_dev_ticket("a", "Plain child")],
        }
        env, _planner, created, _patches = _spend_env(
            self, repo, [payload], mandate=None)
        _seed_objective(env)

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(created[0]["title"], "Plain child")

    async def test_second_cycle_while_pending_does_not_rerun(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "needs a domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        env, planner, created, _patches = _spend_env(self, repo, [payload])
        _seed_objective(env)

        first = await plan_objective(env, "OBJ-SPEND")
        second = await plan_objective(env, "OBJ-SPEND")

        self.assertEqual(first.get("reason"), "spend_approval_pending")
        self.assertEqual(second.get("reason"), "spend_approval_pending")
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)

    async def test_approved_question_applies_on_next_cycle(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "needs a domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        tasks: dict[str, dict[str, Any]] = {}
        env, planner, created, _patches = _spend_env(
            self, repo, [payload], tasks)
        _seed_objective(env)

        held = await plan_objective(env, "OBJ-SPEND")
        self.assertEqual(held.get("reason"), "spend_approval_pending")
        tasks["task-child-1"]["status"] = "completed"
        tasks["task-child-1"]["params"]["owner_question"]["decision"] = {
            "decision": "approved", "by": "owner", "at": "now", "comment": "",
        }

        applied = await plan_objective(env, "OBJ-SPEND")

        self.assertTrue(applied.get("ok"), applied)
        self.assertEqual(applied["tickets_created"], 1)
        self.assertEqual(planner.runs, 1)
        titles = [item["title"] for item in created]
        self.assertIn("Buy domain", titles)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertFalse(stored.get("pending_plan"))

    async def test_denied_question_returns_the_motive_to_the_planner(
        self,
    ) -> None:
        repo = _scratch_repo(self)
        spend_payload = {
            "objective_status": "active",
            "state_summary": "needs a domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        plain_payload = {
            "objective_status": "active",
            "state_summary": "free alternative",
            "tickets": [_dev_ticket("b", "Free alternative")],
        }
        tasks: dict[str, dict[str, Any]] = {}
        env, planner, created, _patches = _spend_env(
            self, repo, [spend_payload, plain_payload], tasks)
        _seed_objective(env)

        held = await plan_objective(env, "OBJ-SPEND")
        self.assertEqual(held.get("reason"), "spend_approval_pending")
        tasks["task-child-1"]["status"] = "completed"
        tasks["task-child-1"]["params"]["owner_question"]["decision"] = {
            "decision": "denied", "by": "owner", "at": "now",
            "comment": "too expensive",
        }

        replanned = await plan_objective(env, "OBJ-SPEND")

        self.assertTrue(replanned.get("ok"), replanned)
        self.assertEqual(planner.runs, 2)
        self.assertIn("too expensive", planner.prompts[1])
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertFalse(stored.get("pending_plan"))
        returns = stored.get("planner_returns") or []
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]["resultado"], "bloqueado")
        self.assertIn("too expensive", returns[0]["motivo"])

    async def test_completion_costs_hold_the_whole_plan(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "adopt with spend",
            "tickets": [_dev_ticket(
                "a", "Adopt stub",
                completes_task_id="task-stub",
                costs=[{"label": "fee", "amount": "5"}])],
        }
        env, _planner, created, patches = _spend_env(self, repo, [payload])
        _seed_objective(env)
        env.tasks.tasks["task-stub"] = {
            "task_id": "task-stub",
            "title": "Adopt stub",
            "status": "waiting",
            "waiting_reason": "routing_missing:dev",
            "objective_id": "OBJ-SPEND",
            "params": {},
        }

        result = await plan_objective(env, "OBJ-SPEND")

        self.assertEqual(result.get("reason"), "spend_approval_pending")
        self.assertEqual(len(created), 1)
        self.assertNotIn("task-stub", patches)


class ApplyApprovedPlanTests(unittest.IsolatedAsyncioTestCase):
    async def _held(
        self, test: unittest.TestCase,
    ) -> tuple[ObjectiveEnv, _RecordedPlanner, list[dict[str, Any]]]:
        repo = _scratch_repo(test)
        payload = {
            "objective_status": "active",
            "state_summary": "needs a domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        env, planner, created, _patches = _spend_env(test, repo, [payload])
        _seed_objective(env)
        held = await plan_objective(env, "OBJ-SPEND")
        assert held.get("reason") == "spend_approval_pending", held
        return env, planner, created

    async def test_apply_launches_the_held_plan(self) -> None:
        env, planner, created = await self._held(self)

        applied = await apply_approved_plan(env, "OBJ-SPEND")

        self.assertTrue(applied.get("ok"), applied)
        self.assertEqual(applied["tickets_created"], 1)
        self.assertEqual(planner.runs, 1)
        titles = [item["title"] for item in created]
        self.assertIn("Buy domain", titles)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertFalse(stored.get("pending_plan"))
        self.assertEqual(stored["planning_runs"][-1]["status"], "active")
        self.assertEqual(
            stored["planner_telemetry"]["tickets_created"], 1)

    async def test_apply_without_pending_is_a_noop(self) -> None:
        env, _planner, _created = await self._held(self)
        env.objectives.docs["OBJ-SPEND"].pop("pending_plan", None)

        applied = await apply_approved_plan(env, "OBJ-SPEND")

        self.assertFalse(applied.get("ok"))
        self.assertEqual(applied.get("reason"), "no-pending-plan")

    async def test_apply_refuses_a_non_active_objective(self) -> None:
        env, _planner, created = await self._held(self)
        env.objectives.docs["OBJ-SPEND"]["status"] = "closed"

        applied = await apply_approved_plan(env, "OBJ-SPEND")

        self.assertFalse(applied.get("ok"))
        self.assertEqual(applied.get("reason"), "objective-not-active")
        self.assertEqual(len(created), 1)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertFalse(stored.get("pending_plan"))

    async def test_apply_revalidates_and_drops_a_stale_plan(self) -> None:
        env, _planner, created = await self._held(self)
        env.planner_cfg["agents"] = {"dev": {"enabled": False,
                                             "reason": "retired"}}

        applied = await apply_approved_plan(env, "OBJ-SPEND")

        self.assertFalse(applied.get("ok"))
        self.assertEqual(applied.get("reason"), "plan-stale")
        self.assertEqual(len(created), 1)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertFalse(stored.get("pending_plan"))

    async def test_apply_keeps_pending_when_full(self) -> None:
        env, _planner, created = await self._held(self)
        env.objectives_cfg["max_active_tickets_per_objective"] = 1
        env.tasks.tasks["task-busy"] = {
            "task_id": "task-busy",
            "title": "Busy",
            "status": "in_progress",
            "objective_id": "OBJ-SPEND",
            "params": {},
        }

        applied = await apply_approved_plan(env, "OBJ-SPEND")

        self.assertFalse(applied.get("ok"))
        self.assertEqual(applied.get("reason"), "objective-full")
        self.assertEqual(len(created), 1)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertIn("pending_plan", stored)


def _svc_task(task_id: str, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "title": f"Ticket {task_id}",
        "description": f"Cuerpo de {task_id}",
        "project": "jardal-openceo",
        "status": "queued",
        "priority": "medium",
        "ticket_number": None,
        "autorun": True,
        "updated_at": datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _bson(value: Any) -> Any:
    """Mongo round-trip: enums land as their values."""
    from enum import Enum

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _bson(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_bson(item) for item in value]
    return value


class _SvcRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.reviews: list[dict[str, Any]] = []
        self._numbers = {"n": 100}
        self.projects = SimpleNamespace(
            find_one=AsyncMock(return_value=None))

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return deepcopy(row) if row is not None else None

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        task.update(deepcopy(updates))
        return deepcopy(task)

    async def create_task(self, doc: dict[str, Any]) -> dict[str, Any]:
        row = _bson(deepcopy(doc))
        self.tasks[row["task_id"]] = row
        return deepcopy(row)

    async def get_next_ticket_number(self) -> int:
        self._numbers["n"] += 1
        return self._numbers["n"]

    async def create_task_review(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.reviews.append(deepcopy(doc))
        return deepcopy(doc)

    async def latest_workflow_run_for_task(
        self, task_id: str
    ) -> dict[str, Any] | None:
        return None

    async def find_task_by_source(
        self, source: str, source_id: str
    ) -> dict[str, Any] | None:
        for row in self.tasks.values():
            if (row.get("source") == source
                    and row.get("source_id") == source_id):
                return deepcopy(row)
        return None

    async def list_tasks_blocked_by(
        self, task_id: str
    ) -> list[dict[str, Any]]:
        return []


class _SvcHub:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        del payload
        self.events.append(event)


class _FakeObjectiveService:
    def __init__(self) -> None:
        self.returns: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.approval_result: dict[str, Any] = {"ok": True,
                                                "tickets_created": 1}

    async def plan_return(
        self, objective_id: str, entry: Any
    ) -> dict[str, Any]:
        self.returns.append({"objective_id": objective_id, "entry": entry})
        return {"ok": True, "tickets_created": 1}

    async def apply_spend_approval(
        self, objective_id: str, question_task_id: str
    ) -> dict[str, Any]:
        self.approvals.append({"objective_id": objective_id,
                               "question_task_id": question_task_id})
        return dict(self.approval_result)


def _make_service(repo: _SvcRepo, mandate: Any = "default"):
    from settings import Settings
    from task_service import TaskService

    service = TaskService(
        settings=Settings(),
        repo=repo,  # type: ignore[arg-type]
        graph=MagicMock(),
        realtime_hub=_SvcHub(),  # type: ignore[arg-type]
        workflow_service=None,
    )
    resolved = _mandate() if mandate == "default" else mandate
    service._config_loader = SimpleNamespace(
        load_company_mandate=MagicMock(return_value=resolved))
    return service


def _spend_question(task_id: str, objective_id: str,
                    **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "status": "waiting",
        "phase": OWNER_QUESTION_PHASE,
        "waiting_reason": f"{OWNER_QUESTION_PHASE}:question",
        "params": {"owner_question": {
            "kind": "question",
            "question": "Autorizar el gasto declarado del plan?",
            "human_action": "Autorizar o denegar",
            "origin_task_id": None,
            "origin_title": None,
            "checkpoint": {"objective_id": objective_id,
                           "spend_hold": True},
        }},
    }
    fields.update(overrides)
    return _svc_task(task_id, **fields)


class LooseSpendGateTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, service, params: dict[str, Any] | None,
                      **fields: Any) -> dict[str, Any]:
        from state import TaskCreateRequest, TaskSource

        return await service.create_task(TaskCreateRequest(
            project="jardal-openceo",
            source=TaskSource.API,
            source_id=fields.pop("source_id", "loose-spend-1"),
            title=fields.pop("title", "Loose with spend"),
            description="body",
            params=params,
            **fields,
        ))

    async def test_loose_with_costs_parks_and_asks(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]})
        task_id = created["task_id"]

        stored = await repo.get_task(task_id)
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertEqual(stored["waiting_reason"], "spend_approval")
        question_id = (stored.get("params") or {}).get("owner_question_id")
        self.assertTrue(question_id)
        question = await repo.get_task(str(question_id))
        assert question is not None
        block = owner_question_block(question)
        self.assertEqual(block["kind"], "question")
        self.assertEqual(block["origin_task_id"], task_id)
        self.assertEqual(question["status"], "waiting")
        self.assertFalse(question_answered(question))

    async def test_loose_approve_resumes(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)
        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]})
        task_id = created["task_id"]
        stored = await repo.get_task(task_id)
        assert stored is not None
        question_id = str((stored.get("params") or {})["owner_question_id"])

        outcome = await service.apply_owner_decision(
            question_id, approve=True, actor_id="owner", comment="dale")

        self.assertTrue(outcome.get("applied"), outcome)
        self.assertEqual(outcome.get("route"), "owner_question_resume")
        origin = await repo.get_task(task_id)
        assert origin is not None
        self.assertEqual(origin["status"], "queued")
        self.assertIn("owner_answer", origin.get("params") or {})

    async def test_loose_deny_fails_with_verdict(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)
        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]})
        task_id = created["task_id"]
        stored = await repo.get_task(task_id)
        assert stored is not None
        question_id = str((stored.get("params") or {})["owner_question_id"])

        outcome = await service.apply_owner_decision(
            question_id, approve=False, actor_id="owner",
            comment="muy caro")

        self.assertTrue(outcome.get("applied"), outcome)
        self.assertEqual(outcome.get("route"), "owner_question_deny")
        origin = await repo.get_task(task_id)
        assert origin is not None
        self.assertEqual(origin["status"], "failed")
        verdict = (origin.get("params") or {}).get("agent_result") or {}
        self.assertEqual(verdict.get("resultado"), "bloqueado")
        self.assertIn("muy caro", verdict.get("motivo") or "")

    async def test_no_costs_launches_untouched(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        created = await self._create(service, {"target_agent": "dev"})

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertNotIn("owner_question_id", stored.get("params") or {})

    async def test_empty_costs_launch_untouched(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        created = await self._create(service, {"costs": []})

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")

    async def test_under_threshold_launches(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo, mandate=_mandate("100.00"))

        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]})

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")

    async def test_unknown_cost_holds(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo, mandate=_mandate("100.00"))

        created = await self._create(
            service, {"costs": [{"label": "license", "amount": None}]})

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")

    async def test_workflow_ticket_skips_creation_gate(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        created = await self._create(service, {
            "workflow": "finance.register_expense",
            "costs": [{"label": "domain", "amount": "12"}],
        })

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertNotIn("owner_question_id", stored.get("params") or {})

    async def test_objective_child_is_plan_gated_not_creation_gated(
        self,
    ) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]},
            objective_id="OBJ-1", origin="objective_planner")

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")

    async def test_autorun_off_is_not_gated(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]},
            autorun=False)

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertNotIn("owner_question_id", stored.get("params") or {})

    async def test_malformed_costs_reject_before_any_persist(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)

        with self.assertRaises(ValueError):
            await self._create(
                service, {"costs": [{"label": "x", "amount": 2.5}]})

        self.assertEqual(repo.tasks, {})

    async def test_stored_malformed_costs_hold_fail_closed(self) -> None:
        from state import TaskCreateRequest, TaskSource

        repo = _SvcRepo()
        repo.tasks["old-1"] = _svc_task(
            "old-1", source="api", source_id="old-malformed-1",
            params={"costs": [{"label": "x", "amount": 2.5}]})
        service = _make_service(repo)

        again = await service.create_task(TaskCreateRequest(
            project="jardal-openceo",
            source=TaskSource.API,
            source_id="old-malformed-1",
            title="Loose with spend",
            description="body",
            params={"costs": [{"label": "domain", "amount": "12"}]},
        ))

        self.assertEqual(again["task_id"], "old-1")
        stored = await repo.get_task("old-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertEqual(stored["waiting_reason"], "spend_approval")

    async def test_idempotent_reentry_reholds(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)
        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]},
            source_id="loose-retry-1")
        task_id = created["task_id"]
        stored = await repo.get_task(task_id)
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        await repo.patch_task(task_id, {
            "status": "queued",
            "waiting_reason": None,
            "params": {"costs": [{"label": "domain", "amount": "12"}],
                       "owner_question_id": None},
        })

        again = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]},
            source_id="loose-retry-1")

        self.assertEqual(again["task_id"], task_id)
        stored = await repo.get_task(task_id)
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")


class SpendDecisionRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_applies_the_held_plan(self) -> None:
        repo = _SvcRepo()
        repo.tasks["q-spend"] = _spend_question("q-spend", "OBJ-SPEND")
        service = _make_service(repo)
        objectives = _FakeObjectiveService()
        service.set_objective_service(objectives)

        outcome = await service.apply_owner_decision(
            "q-spend", approve=True, actor_id="owner", comment="dale")

        self.assertTrue(outcome.get("applied"), outcome)
        self.assertEqual(outcome.get("route"),
                         "owner_question_spend_approve")
        self.assertEqual(len(objectives.approvals), 1)
        self.assertEqual(objectives.approvals[0]["objective_id"],
                         "OBJ-SPEND")
        self.assertEqual(objectives.approvals[0]["question_task_id"],
                         "q-spend")
        question = await repo.get_task("q-spend")
        assert question is not None
        self.assertTrue(question_answered(question))

    async def test_deny_returns_the_motive_to_the_planner(self) -> None:
        repo = _SvcRepo()
        repo.tasks["q-spend"] = _spend_question("q-spend", "OBJ-SPEND")
        service = _make_service(repo)
        objectives = _FakeObjectiveService()
        service.set_objective_service(objectives)

        outcome = await service.apply_owner_decision(
            "q-spend", approve=False, actor_id="owner",
            comment="muy caro")

        self.assertTrue(outcome.get("applied"), outcome)
        self.assertEqual(outcome.get("route"), "owner_question_spend_deny")
        self.assertEqual(len(objectives.returns), 1)
        entry = objectives.returns[0]["entry"]
        self.assertEqual(objectives.returns[0]["objective_id"], "OBJ-SPEND")
        self.assertEqual(entry["task_id"], "q-spend")
        self.assertEqual(entry["resultado"], "bloqueado")
        self.assertIn("muy caro", entry["motivo"])

    async def test_recorded_deny_wins_on_retry(self) -> None:
        repo = _SvcRepo()
        question = _spend_question("q-spend", "OBJ-SPEND",
                                   status="completed")
        question["params"]["owner_question"]["decision"] = {
            "decision": "denied", "by": "erpnext-desk", "at": "now",
            "comment": "no",
        }
        repo.tasks["q-spend"] = question
        service = _make_service(repo)
        objectives = _FakeObjectiveService()
        service.set_objective_service(objectives)

        outcome = await service.apply_owner_decision(
            "q-spend", approve=True, actor_id="owner", comment="dale")

        self.assertFalse(outcome.get("applied"))
        self.assertEqual(len(objectives.approvals), 0)
        self.assertEqual(len(objectives.returns), 0)


class PendingSpendReadErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_read_keeps_the_hold(self) -> None:
        from objectives.engine import _pending_spend_state

        async def _boom(task_id: str) -> None:
            raise RuntimeError("mongo blip")

        env = SimpleNamespace(tasks=SimpleNamespace(get_task=_boom))

        state, question = await _pending_spend_state(
            env, {"question_task_id": "q-1"})

        self.assertEqual(state, "wait")
        self.assertIsNone(question)

    async def test_cycle_keeps_pending_on_read_error(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "needs a domain",
            "tickets": [_dev_ticket(
                "a", "Buy domain",
                costs=[{"label": "domain", "amount": "12"}])],
        }
        env, planner, _created, _patches = _spend_env(self, repo, [payload])
        _seed_objective(env)
        held = await plan_objective(env, "OBJ-SPEND")
        self.assertEqual(held.get("reason"), "spend_approval_pending")

        async def _boom(task_id: str) -> None:
            raise RuntimeError("mongo blip")

        env.tasks.get_task = _boom  # type: ignore[method-assign]

        again = await plan_objective(env, "OBJ-SPEND")

        self.assertEqual(again.get("reason"), "spend_approval_pending")
        self.assertEqual(planner.runs, 1)
        stored = await env.objectives.get_objective("OBJ-SPEND")
        assert stored is not None
        self.assertTrue(stored.get("pending_plan"))


class ApprovalPlanBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_for_another_plan_does_not_launch(self) -> None:
        from objectives.service import ObjectiveService

        repo = _SvcRepo()
        answered = _spend_question("q-old", "OBJ-SPEND", status="completed")
        answered["params"]["owner_question"]["decision"] = {
            "decision": "approved", "by": "owner", "at": "now", "comment": "",
        }
        repo.tasks["q-old"] = answered

        async def _get_objective(objective_id: str) -> dict[str, Any]:
            return {"objective_id": objective_id, "project": "proj",
                    "status": "active",
                    "pending_plan": {"question_task_id": "q-new"}}

        repo.get_objective = _get_objective  # type: ignore[attr-defined]
        service = ObjectiveService(
            repo=repo,  # type: ignore[arg-type]
            task_service=MagicMock(),
            config_loader=SimpleNamespace(
                has_project=MagicMock(return_value=True)),
            settings=MagicMock(),
        )

        outcome = await service.apply_spend_approval("OBJ-SPEND", "q-old")

        self.assertFalse(outcome.get("ok"))
        self.assertEqual(outcome.get("reason"), "approval-plan-mismatch")


class OrphanSpendApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_without_plan_signals_instead_of_silence(
        self,
    ) -> None:
        repo = _SvcRepo()
        repo.tasks["q-spend"] = _spend_question("q-spend", "OBJ-SPEND")
        service = _make_service(repo)
        objectives = _FakeObjectiveService()
        objectives.approval_result = {"ok": False,
                                      "reason": "no-pending-plan"}
        service.set_objective_service(objectives)

        outcome = await service.apply_owner_decision(
            "q-spend", approve=True, actor_id="owner", comment="dale")

        self.assertTrue(outcome.get("applied"), outcome)
        self.assertEqual(outcome.get("route"), "owner_question_spend_held")
        question = await repo.get_task("q-spend")
        assert question is not None
        self.assertIn("sin plan retenido",
                      str(question.get("last_response_summary") or ""))


class PendingLinkSpendTests(unittest.IsolatedAsyncioTestCase):
    async def _create(self, service, params: dict[str, Any] | None,
                      **fields: Any) -> dict[str, Any]:
        from state import TaskCreateRequest, TaskSource

        return await service.create_task(TaskCreateRequest(
            project="jardal-openceo",
            source=TaskSource.API,
            source_id=fields.pop("source_id", "loose-pending-1"),
            title=fields.pop("title", "Loose with spend"),
            description="body",
            params=params,
            **fields,
        ))

    async def test_spend_holds_even_with_link_pending(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)
        service._register_business_ticket = AsyncMock(  # type: ignore[method-assign]
            return_value=(None, True))

        created = await self._create(
            service, {"costs": [{"label": "domain", "amount": "12"}]})

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertEqual(stored["waiting_reason"], "spend_approval")
        question_id = (stored.get("params") or {}).get("owner_question_id")
        self.assertTrue(question_id)
        question = await repo.get_task(str(question_id))
        assert question is not None
        self.assertEqual(question["status"], "waiting")

    async def test_no_spend_pending_link_unchanged(self) -> None:
        repo = _SvcRepo()
        service = _make_service(repo)
        service._register_business_ticket = AsyncMock(  # type: ignore[method-assign]
            return_value=(None, True))

        created = await self._create(service, {"target_agent": "dev"})

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["waiting_reason"], "jardal_erp_pending")


class PreParkTests(unittest.IsolatedAsyncioTestCase):
    async def test_spend_ticket_never_persists_queued(self) -> None:
        from state import TaskCreateRequest, TaskSource

        repo = _SvcRepo()
        seen: list[str] = []
        raw_create = repo.create_task

        async def _recording_create(doc: dict[str, Any]) -> dict[str, Any]:
            if "spend-never-queued" in str(doc.get("source_id") or ""):
                seen.append(str(doc.get("status")))
            return await raw_create(doc)

        repo.create_task = _recording_create  # type: ignore[method-assign]
        service = _make_service(repo)

        await service.create_task(TaskCreateRequest(
            project="jardal-openceo",
            source=TaskSource.API,
            source_id="spend-never-queued-1",
            title="Loose with spend",
            description="body",
            params={"costs": [{"label": "domain", "amount": "12"}]},
        ))

        self.assertTrue(seen)
        for status in seen:
            self.assertNotIn("queued", status.lower())

    async def test_plain_ticket_still_persists_queued(self) -> None:
        from state import TaskCreateRequest, TaskSource

        repo = _SvcRepo()
        seen: list[str] = []
        raw_create = repo.create_task

        async def _recording_create(doc: dict[str, Any]) -> dict[str, Any]:
            if "plain-still-queued" in str(doc.get("source_id") or ""):
                seen.append(str(doc.get("status")))
            return await raw_create(doc)

        repo.create_task = _recording_create  # type: ignore[method-assign]
        service = _make_service(repo)

        await service.create_task(TaskCreateRequest(
            project="jardal-openceo",
            source=TaskSource.API,
            source_id="plain-still-queued-1",
            title="Plain loose",
            description="body",
            params={"target_agent": "dev"},
        ))

        self.assertEqual(len(seen), 1)
        self.assertIn("queued", seen[0].lower())


class UnpauseSpendTests(unittest.IsolatedAsyncioTestCase):
    async def test_unpause_reholds(self) -> None:
        from state import TaskCreateRequest, TaskSource

        repo = _SvcRepo()
        service = _make_service(repo)
        created = await service.create_task(TaskCreateRequest(
            project="jardal-openceo",
            source=TaskSource.API,
            source_id="loose-unpause-1",
            title="Loose with spend",
            description="body",
            params={"costs": [{"label": "domain", "amount": "12"}]},
            autorun=False,
        ))
        task_id = created["task_id"]
        stored = await repo.get_task(task_id)
        assert stored is not None
        self.assertEqual(stored["status"], "queued")

        await service.patch_task(task_id, {"autorun": True})

        stored = await repo.get_task(task_id)
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertEqual(stored["waiting_reason"], "spend_approval")
        self.assertTrue(
            (stored.get("params") or {}).get("owner_question_id"))

    async def test_unpause_without_costs_untouched(self) -> None:
        from state import TaskCreateRequest, TaskSource

        repo = _SvcRepo()
        service = _make_service(repo)
        created = await service.create_task(TaskCreateRequest(
            project="jardal-openceo",
            source=TaskSource.API,
            source_id="loose-unpause-plain-1",
            title="Plain loose",
            description="body",
            params={"target_agent": "dev"},
            autorun=False,
        ))

        await service.patch_task(created["task_id"], {"autorun": True})

        stored = await repo.get_task(created["task_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "queued")
        self.assertNotIn("owner_question_id", stored.get("params") or {})


class ApiSpendErrorTests(unittest.TestCase):
    def test_tasks_endpoint_maps_value_error_to_400(self) -> None:
        source = (CORE / "main.py").read_text(encoding="utf-8")
        start = source.index('@app.post("/api/tasks")')
        end = source.index("# ------------------------- objectives",
                           start)
        endpoint = source[start:end]
        self.assertIn("except ValueError as exc", endpoint)
        self.assertIn("status_code=400", endpoint)


if __name__ == "__main__":
    unittest.main()
