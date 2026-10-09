"""Company-level objectives (TASK-2026-00117, F2).

Acceptance with simulated planner/reviewers (zero tokens): a top-down
objective and a conversion reach the same engine, are born in ERPNext as a
group Task with real children (the linking uses no LLM) and close through
final review; legacy objectives (ORION pilot shape) keep working. No
network, no CLI, no LLM.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import engine as engine_mod
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveDuplicate,
    ObjectiveErpError,
    ObjectiveLimitReached,
    ObjectiveRejected,
    ObjectiveEnv,
    create_objective,
    convert_ticket,
    plan_objective,
    reflect_group_decision,
    run_final_review,
    verify_delivery,
)


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
    """OUTPUT path the prompt orders (the honest planner follows it)."""
    for line in prompt.splitlines():
        if line.startswith("OUTPUT: write "):
            rel = line[len("OUTPUT: write "):].split(" with exactly")[0]
            return repo / rel.strip()
    raise AssertionError("planner prompt names no OUTPUT target")


class _RecordedPlanner:
    """Simulated Muse headless run: writes the recorded JSON (0 tokens)."""

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
            # Retry prompt: fix the same file in place.
            assert self._target is not None
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


class _ExplodingExecutor:
    """Fails the test if any role runs (proves the deterministic path)."""

    def __init__(self) -> None:
        self.runs: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del prompt, workdir, timeout_s
        self.runs.append(role)
        raise AssertionError(f"no executor runs expected, got {role!r}")

    async def cleanup(self, role: str) -> None:
        del role


class _FakeErp:
    def __init__(self, fail: Exception | None = None) -> None:
        self.groups: list[str] = []
        self.fail = fail

    async def convert_to_group(self, identifier: str) -> dict[str, Any]:
        if self.fail is not None:
            raise self.fail
        self.groups.append(identifier)
        return {"ok": True, "ticket": {"name": identifier}}


def _env_for(test: unittest.TestCase, repo: Path,
             payloads: list[dict[str, Any]],
             tasks: dict[str, dict[str, Any]] | None = None,
             erp: Any = None,
             objectives_cfg: dict[str, Any] | None = None,
             shell_mirror: str | None = "TASK-2026-00999",
             ) -> tuple[ObjectiveEnv, _RecordedPlanner,
                        list[dict[str, Any]], dict[str, dict[str, Any]],
                        list[tuple[str, str]]]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: route by oracle, not quota.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    patches: dict[str, dict[str, Any]] = {}
    notices: list[tuple[str, str]] = []
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        created.append(dict(payload))
        row: dict[str, Any] = {"task_id": f"task-new-{counter['n']}"}
        if shell_mirror:
            row["params"] = {"_mirrors": {"erpnext": {"name": shell_mirror}}}
        return row

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(updates)

    async def notify(ref: str, text: str) -> None:
        notices.append((ref, text))

    planner = _RecordedPlanner(repo, payloads)
    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(dict(tasks or {})),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=planner,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=notify,
        repo=repo,
        objectives_cfg=dict(objectives_cfg or {}),
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True}},
        },
        erp=erp,
    )
    return env, planner, created, patches, notices


def _dev_ticket(local_ref: str = "t1", title: str = "Do the thing",
                **overrides: Any) -> dict[str, Any]:
    ticket: dict[str, Any] = {
        "local_ref": local_ref,
        "title": title,
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": f"achieve {title}",
        "why_now": "next step",
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_b",
        "tier_reason": "bounded work",
        "why_not_c": "a misjudged step ships wrong behavior",
    }
    ticket.update(overrides)
    return ticket


def _plan_result(*tickets: dict[str, Any],
                 status: str = "active") -> dict[str, Any]:
    return {"objective_status": status, "state_summary": "plan",
            "tickets": list(tickets)}


class TopDownCreationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_group_plans_and_links_children(self) -> None:
        repo = _scratch_repo(self)
        env, planner, created, patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())])
        objective = await create_objective(
            env, project="proj", goal="Ship the thing",
            acceptance=["it works"])

        self.assertEqual(objective["entry"], "top_down")
        self.assertEqual(objective["erp_group_ref"], "TASK-2026-00999")
        self.assertEqual(objective["anchor"]["goal"], "Ship the thing")
        # The shell is the ERP group: born converted, never executes.
        shell = created[0]
        self.assertTrue(shell["is_group"])
        self.assertFalse(shell["autorun"])
        self.assertEqual(shell["origin"], "objective_shell")
        self.assertEqual(shell["objective_id"], objective["objective_id"])
        shell_id = "task-new-1"
        self.assertEqual(patches[shell_id]["status"], "converted")
        self.assertEqual(objective["conversion_source_task_id"], shell_id)
        # The planner ran and its children link under the group (no LLM).
        self.assertEqual(planner.runs, 1)
        self.assertEqual(created[1]["parent_task"], "TASK-2026-00999")
        self.assertEqual(created[1]["objective_id"],
                         objective["objective_id"])

    async def test_children_carry_expected_artifacts(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())])
        await create_objective(env, project="proj", goal="Ship it")
        params = created[1]["params"]
        self.assertEqual(params["expected_artifacts"], ["code"])
        self.assertEqual(params["target_agent"], "dev")

    async def test_shell_failure_refuses_group_less_objective(self) -> None:
        repo = _scratch_repo(self)
        env, planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()])

        async def _no_ticket(payload: dict[str, Any]) -> None:
            del payload
            return None

        env.create_ticket = _no_ticket  # type: ignore[method-assign]
        with self.assertRaises(ObjectiveErpError):
            await create_objective(env, project="proj", goal="Ship it")
        self.assertEqual(planner.runs, 0)
        stored = await env.objectives.list_active_objectives()
        self.assertEqual(stored, [])

    async def test_rejects_empty_project_goal_and_entry(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()])
        with self.assertRaises(ObjectiveRejected):
            await create_objective(env, project="  ", goal="g")
        with self.assertRaises(ObjectiveRejected):
            await create_objective(env, project="proj", goal="  ")
        with self.assertRaises(ObjectiveRejected):
            await create_objective(env, project="proj", goal="g",
                                   entry="sideways")

    async def test_horizon_promotion_uses_the_same_entry(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result(status="blocked")])
        objective = await create_objective(
            env, project="proj", goal="Promoted work",
            entry="horizon_promotion", source="api")
        self.assertEqual(objective["entry"], "horizon_promotion")
        self.assertTrue(created[0]["is_group"])


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def _active(self, env: ObjectiveEnv, objective_id: str,
                      project: str, goal: str) -> None:
        await env.objectives.create_objective({
            "objective_id": objective_id, "project": project,
            "status": "active", "anchor": {"goal": goal, "acceptance": []},
            "conversion_source_task_id": f"parent-{objective_id}",
        })

    async def test_per_project_limit(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()])
        await self._active(env, "OBJ-1", "proj", "one")
        await self._active(env, "OBJ-2", "proj", "two")
        with self.assertRaises(ObjectiveLimitReached) as ctx:
            await create_objective(env, project="proj", goal="three")
        self.assertIn("proj", str(ctx.exception))

    async def test_global_limit(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()])
        await self._active(env, "OBJ-1", "a", "one")
        await self._active(env, "OBJ-2", "b", "two")
        await self._active(env, "OBJ-3", "c", "three")
        with self.assertRaises(ObjectiveLimitReached):
            await create_objective(env, project="d", goal="four")

    async def test_duplicate_goal_refused(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()])
        await self._active(env, "OBJ-1", "proj", "Ship  the\nthing")
        with self.assertRaises(ObjectiveDuplicate) as ctx:
            await create_objective(env, project="proj", goal="ship the thing")
        self.assertEqual(ctx.exception.existing_id, "OBJ-1")

    async def test_same_goal_another_project_is_allowed(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result(status="blocked")])
        await self._active(env, "OBJ-1", "a", "Ship the thing")
        objective = await create_objective(
            env, project="b", goal="ship the thing")
        self.assertNotEqual(objective["objective_id"], "OBJ-1")

    async def test_closed_objective_does_not_block(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result(status="blocked")])
        await env.objectives.create_objective({
            "objective_id": "OBJ-9", "project": "proj", "status": "closed",
            "anchor": {"goal": "Ship the thing", "acceptance": []},
            "conversion_source_task_id": "parent-9",
        })
        objective = await create_objective(
            env, project="proj", goal="Ship the thing")
        self.assertEqual(objective["entry"], "top_down")

    async def test_active_tickets_cap_defers_without_parking(self) -> None:
        repo = _scratch_repo(self)
        tasks = {f"kid-{n}": {"task_id": f"kid-{n}", "title": "w",
                              "status": "queued", "objective_id": "OBJ-1"}
                 for n in range(6)}
        env, planner, _created, _patches, notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks)
        await self._active(env, "OBJ-1", "proj", "one")
        result = await plan_objective(env, "OBJ-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "objective-full")
        self.assertEqual(planner.runs, 0)
        self.assertEqual(notices, [])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "active")


def _mirrored(task_id: str, name: str | None, **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {"task_id": task_id, "title": "t",
                            "description": "d", "status": "in_progress",
                            "project": "proj", "acceptance": []}
    if name:
        task["params"] = {"_mirrors": {"erpnext": {"name": name}}}
    task.update(overrides)
    return task


class ConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_becomes_the_group(self) -> None:
        repo = _scratch_repo(self)
        erp = _FakeErp()
        tasks = {"task-parent": _mirrored("task-parent", "TASK-2026-00096")}
        env, planner, created, patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks, erp=erp)
        objective = await convert_ticket(
            env, tasks["task-parent"], "OBJECTIVE_NEEDED", "")

        self.assertEqual(objective["entry"], "convert")
        self.assertEqual(erp.groups, ["TASK-2026-00096"])
        self.assertEqual(objective["erp_group_ref"], "TASK-2026-00096")
        self.assertEqual(patches["task-parent"]["status"], "converted")
        self.assertEqual(planner.runs, 1)
        self.assertEqual(created[0]["parent_task"], "TASK-2026-00096")

    async def test_legacy_flow_without_erp(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": _mirrored("task-parent", "TASK-2026-00096")}
        env, planner, created, patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks)
        objective = await convert_ticket(
            env, tasks["task-parent"], "OBJECTIVE_NEEDED", "")

        self.assertIsNone(objective["erp_group_ref"])
        self.assertNotIn("parent_task", created[0])
        self.assertEqual(patches["task-parent"]["status"], "converted")
        self.assertEqual(planner.runs, 1)

    async def test_group_failure_parks_before_planning(self) -> None:
        repo = _scratch_repo(self)
        erp = _FakeErp(fail=RuntimeError("erp down"))
        tasks = {"task-parent": _mirrored("task-parent", "TASK-2026-00096")}
        env, planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks, erp=erp)
        objective = await convert_ticket(
            env, tasks["task-parent"], "OBJECTIVE_NEEDED", "")

        self.assertEqual(objective["status"], "waiting")
        # TASK-2026-00118: an ERP write failure is operational (retried on
        # the next trigger), not an owner decision.
        self.assertIn("erp_error", objective["waiting_reason"])
        self.assertEqual(planner.runs, 0)
        self.assertEqual(created, [])

    async def test_ticket_inside_active_objective_decomposes_within(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-kid": _mirrored("task-kid", "TASK-2026-00107",
                                       objective_id="OBJ-1")}
        env, planner, created, patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks)
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        objective = await convert_ticket(
            env, tasks["task-kid"], "TOO_BIG", "sizing words")

        self.assertEqual(objective["objective_id"], "OBJ-1")
        stored = await env.objectives.list_active_objectives()
        self.assertEqual(len(stored), 1)
        self.assertEqual(patches["task-kid"]["status"], "converted")
        # The sizing reaches the replan prompt (the run then clears it).
        self.assertIn("sizing words", planner.prompts[0])
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["objective_id"], "OBJ-1")

    async def test_dead_objective_membership_makes_a_new_one(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-kid": _mirrored("task-kid", None,
                                       objective_id="OBJ-dead")}
        env, _planner, _created, patches, _notices = _env_for(
            self, repo, [_plan_result(status="blocked")], tasks=tasks)
        objective = await convert_ticket(
            env, tasks["task-kid"], "TOO_BIG", "")

        self.assertNotEqual(objective["objective_id"], "OBJ-dead")
        self.assertEqual(patches["task-kid"]["objective_id"],
                         objective["objective_id"])

    async def test_lazy_group_upgrade_on_legacy_objective(self) -> None:
        repo = _scratch_repo(self)
        erp = _FakeErp()
        tasks = {"task-parent": _mirrored("task-parent", "TASK-2026-00096")}
        env, planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result(status="blocked")], tasks=tasks,
            erp=erp)
        # ORION shape: converted before groups existed.
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        await plan_objective(env, "OBJ-1")
        self.assertEqual(erp.groups, ["TASK-2026-00096"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["erp_group_ref"], "TASK-2026-00096")
        self.assertEqual(planner.runs, 1)


def _child(task_id: str, status: str, *, dev: bool,
           acceptance: list[str] | None = None,
           artefacts: list[str] | None = None) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": task_id, "title": "w", "status": status,
        "objective_id": "OBJ-1", "project": "proj",
        "acceptance": ["done"] if acceptance is None else acceptance,
        "params": {"target_agent": "dev" if dev else "research",
                   "expected_artifacts": (["code"] if artefacts is None
                                          else artefacts)},
    }
    if dev:
        task["coding_tool"] = "tier_b"
    return task


class ConvertedShellNeverReadyTests(unittest.IsolatedAsyncioTestCase):
    """Converted tickets are terminal, never ready (TASK-2026-00122).

    F7 proved it live: with only a converted shell and a completed child,
    ``on_ticket_closed`` answered ``enqueuer-continues``, so no objective
    could replan past its first batch (OBJ-BD88AAFA). Converted tickets
    never execute: the shell carries the children and superseded tickets
    decompose inside.
    """

    async def test_close_last_child_replans_past_converted_shell(self) -> None:
        repo = _scratch_repo(self)
        tasks = {
            "shell-1": {**_child("shell-1", "converted", dev=True),
                        "title": "shell"},
            "kid-1": {**_child("kid-1", "completed", dev=True),
                      "title": "kid"},
        }
        env, planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks)
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "shell-1",
        })
        result = await engine_mod.on_ticket_closed(env, tasks["kid-1"], None)
        self.assertNotEqual(result.get("action"), "enqueuer-continues")
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["objective_id"], "OBJ-1")

    async def test_converted_dependency_does_not_block(self) -> None:
        repo = _scratch_repo(self)
        tasks = {
            "shell-1": {**_child("shell-1", "converted", dev=True)},
            "kid-1": {**_child("kid-1", "queued", dev=True),
                      "blocked_by": ["shell-1"]},
        }
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks)
        self.assertFalse(await engine_mod._blocked(env, tasks["kid-1"]))


class FailedChildrenAreHistoryTests(unittest.IsolatedAsyncioTestCase):
    """Failed tickets are history, not work (TASK-2026-00122).

    F7 proved it live: with a failed probe (bc913cf7) beside completed
    children, ``on_ticket_closed`` answered ``enqueuer-continues`` and
    ``verify_delivery`` reported ``still failed`` open gaps, so E1
    (OBJ-BD88AAFA) could neither replan nor reach final review. The
    dispatcher never launches failed tickets: they are evidence for the
    planner, never dispatchable or close-blocking work. Failed stays out
    of TERMINAL_TICKET_STATUS so the planner prompt keeps showing
    failures and the stall brake keeps tripping without terminal closes.
    """

    async def test_close_last_child_replans_past_failed_sibling(self) -> None:
        repo = _scratch_repo(self)
        tasks = {
            "shell-1": {**_child("shell-1", "converted", dev=True),
                        "title": "shell"},
            "kid-fail": {**_child("kid-fail", "failed", dev=True),
                         "title": "probe"},
            "kid-1": {**_child("kid-1", "completed", dev=True),
                      "title": "kid"},
        }
        env, planner, created, _patches, _notices = _env_for(
            self, repo, [_plan_result(_dev_ticket())], tasks=tasks)
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "shell-1",
        })
        result = await engine_mod.on_ticket_closed(env, tasks["kid-1"], None)
        self.assertNotEqual(result.get("action"), "enqueuer-continues")
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["objective_id"], "OBJ-1")

    async def test_verify_delivery_excludes_failed_from_open_gaps(self) -> None:
        objective = {"objective_id": "OBJ-1",
                     "conversion_source_task_id": "shell-1"}
        children = [
            _child("shell-1", "converted", dev=True),
            _child("kid-fail", "failed", dev=True),
            _child("kid-1", "completed", dev=True),
        ]
        verdict = verify_delivery(objective, children)
        self.assertEqual(verdict["open_gaps"], [])
        self.assertEqual(verdict["data_gaps"], [])
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["failed_children"], ["kid-fail"])
        self.assertEqual(verdict["work_children"], 2)
        self.assertEqual(verdict["terminal_children"], 1)

    async def test_failed_child_without_contract_is_data_gap(self) -> None:
        objective = {"objective_id": "OBJ-1",
                     "conversion_source_task_id": "shell-1"}
        children = [
            _child("shell-1", "converted", dev=True),
            _child("kid-fail", "failed", dev=True,
                   acceptance=[], artefacts=[]),
        ]
        verdict = verify_delivery(objective, children)
        self.assertEqual(verdict["open_gaps"], [])
        self.assertFalse(verdict["ok"])
        self.assertEqual(len(verdict["data_gaps"]), 2)
        self.assertEqual(verdict["failed_children"], ["kid-fail"])


class FinalReviewTests(unittest.IsolatedAsyncioTestCase):
    async def _objective(self, env: ObjectiveEnv) -> None:
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })

    async def test_nondev_without_verifier_parks_capability_gap(self) -> None:
        # TASK-2026-00175 (deliberate contract change, supersedes the
        # 00117 deterministic close): non-dev delivery without a
        # registered domain verifier parks as capability_gap instead
        # of closing on metadata alone. Verified delivery still closes
        # (see tests/test_nondev_delivery_evidence_175.py).
        repo = _scratch_repo(self)
        parent = _mirrored("task-parent", "TASK-1", status="converted",
                           objective_id="OBJ-1")
        tasks = {"task-parent": parent,
                 "kid-1": _child("kid-1", "completed", dev=False),
                 "kid-2": _child("kid-2", "cancelled", dev=False)}
        env, _planner, _created, patches, notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capability_gap")
        self.assertIn("research", result["detail"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertEqual(stored.get("final_reviews", 0), 0)
        self.assertIn("capability_gap", stored["waiting_reason"])
        gap = stored["capability_gap"]
        self.assertIn("research", gap["summary"])
        self.assertEqual(gap["gaps"], [{"domain": "research",
                                       "children": ["kid-1"]}])
        self.assertNotIn("task-parent", patches)
        self.assertTrue(notices)

    async def test_dev_goes_through_the_pool(self) -> None:
        repo = _scratch_repo(self)
        parent = _mirrored("task-parent", "TASK-1", status="converted",
                           objective_id="OBJ-1")
        tasks = {"task-parent": parent,
                 "kid-1": _child("kid-1", "completed", dev=True)}
        env, _planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        # Empty pool: no quorum, parked with a note (relocated from the
        # pre-F2 complete_candidate test, now on the dev-domain branch).
        self.assertFalse(result["ok"])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["final_reviews"], 1)
        self.assertEqual(stored["status"], "waiting")
        self.assertIn("quorum", stored["waiting_reason"])

    async def test_open_children_send_back_to_planning(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"kid-1": _child("kid-1", "completed", dev=False),
                 "kid-2": _child("kid-2", "in_progress", dev=False)}
        env, planner, _created, _patches, notices = _env_for(
            self, repo, [_plan_result(status="blocked")], tasks=tasks)
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertEqual(planner.runs, 1)
        self.assertTrue(any("back to planning" in text
                            for _ref, text in notices))
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["final_reviews"], 1)
        self.assertIn(stored["status"], {"active", "waiting"})
        self.assertEqual(result["ok"], False)

    async def test_data_gaps_park_without_spending(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"kid-1": _child("kid-1", "completed", dev=False,
                                 acceptance=[])}
        env, planner, _created, _patches, _notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        env.executor = _ExplodingExecutor()  # type: ignore[assignment]
        await self._objective(env)
        result = await run_final_review(env, "OBJ-1")

        self.assertFalse(result["ok"])
        # TASK-2026-00118: missing delivery data parks operationally; the
        # engine never pages the owner.
        self.assertEqual(result["reason"], "data_gaps")
        self.assertIn("data gaps", result["detail"])
        self.assertEqual(planner.runs, 0)
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")

    async def test_verify_delivery_ignores_parent_and_converted(self) -> None:
        objective = {"objective_id": "OBJ-1",
                     "conversion_source_task_id": "task-parent"}
        children = [
            {"task_id": "task-parent", "status": "converted"},
            {"task_id": "kid-old", "status": "converted"},
            _child("kid-1", "completed", dev=False),
        ]
        verdict = verify_delivery(objective, children)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["work_children"], 1)


class GroupDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def _seed(self, env: ObjectiveEnv, tasks: dict[str, Any]) -> None:
        for task_id, task in tasks.items():
            env.tasks.tasks[task_id] = task
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "conversion_source_task_id": "task-parent",
        })

    async def test_group_completed_closes(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"task-parent": _mirrored("task-parent", "TASK-1",
                                          status="converted",
                                          objective_id="OBJ-1")}
        env, _planner, _created, _patches, notices = _env_for(
            self, repo, [_plan_result()], tasks=tasks)
        await self._seed(env, {})
        result = await reflect_group_decision(
            env, "OBJ-1", target="completed", business_status="Completed")

        self.assertEqual(result["action"], "closed-by-business")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "closed")
        self.assertEqual(stored["closed_by"], "business_decision")
        self.assertEqual(stored["delivery"]["verified_by"],
                         "business_decision")
        self.assertTrue(notices)

    async def test_group_cancel_cascades_to_open_children(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, patches, _notices = _env_for(
            self, repo, [_plan_result()])
        await self._seed(env, {
            "task-parent": _mirrored("task-parent", "TASK-1",
                                     status="converted",
                                     objective_id="OBJ-1"),
            "kid-q": _child("kid-q", "queued", dev=False),
            "kid-p": _child("kid-p", "in_progress", dev=True),
            "kid-w": _child("kid-w", "waiting", dev=False),
            "kid-i": _child("kid-i", "interrupted", dev=False),
            "kid-done": _child("kid-done", "completed", dev=False),
            "kid-fail": _child("kid-fail", "failed", dev=False),
        })
        result = await reflect_group_decision(
            env, "OBJ-1", target="cancelled", business_status="Cancelled")

        self.assertEqual(result["action"], "cancelled")
        self.assertEqual(sorted(result["cancelled_children"]),
                         ["kid-i", "kid-p", "kid-q", "kid-w"])
        for kid in ("kid-q", "kid-p", "kid-w", "kid-i"):
            self.assertEqual(patches[kid]["status"], "cancelled")
            self.assertIn("cancelled in ERPNext", patches[kid]["error"])
        self.assertNotIn("kid-done", patches)
        self.assertNotIn("kid-fail", patches)
        self.assertNotIn("task-parent", patches)
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "cancelled")
        self.assertIn("Cancelled", stored["cancel_reason"])

    async def test_reflect_is_idempotent_and_missing_is_clean(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created, patches, _notices = _env_for(
            self, repo, [_plan_result()])
        missing = await reflect_group_decision(
            env, "OBJ-nope", target="cancelled", business_status="Cancelled")
        self.assertFalse(missing["ok"])
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "closed",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        again = await reflect_group_decision(
            env, "OBJ-1", target="cancelled", business_status="Cancelled")
        self.assertEqual(again["action"], "already-terminal")
        self.assertEqual(patches, {})


class _FakeObjectiveRepo:
    """Dict-backed repo with the objective + task surface the service uses."""

    def __init__(self) -> None:
        self.objectives: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def create_objective(self, doc: dict[str, Any]) -> dict[str, Any]:
        stored = dict(doc)
        self.objectives[stored["objective_id"]] = stored
        return dict(stored)

    async def get_objective(self, objective_id: str) -> dict[str, Any] | None:
        doc = self.objectives.get(objective_id)
        return dict(doc) if doc is not None else None

    async def patch_objective(self, objective_id: str,
                              updates: dict[str, Any]) -> dict[str, Any] | None:
        doc = self.objectives.get(objective_id)
        if doc is None:
            return None
        doc.update(updates)
        return dict(doc)

    async def list_active_objectives(
            self, project: str | None = None) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.objectives.values()
                if doc.get("status") == "active"
                and (not project or doc.get("project") == project)]

    async def list_tasks_by_objective(
            self, objective_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.tasks.values()
                if row.get("objective_id") == objective_id]

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.tasks.get(task_id)
        return dict(row) if row is not None else None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        self.patches.append((task_id, dict(updates)))
        row = self.tasks.get(task_id, {"task_id": task_id})
        row = {**row, **updates}
        self.tasks[task_id] = row
        return dict(row)


class _FakeConfig:
    def __init__(self, projects: dict[str, dict[str, Any]]) -> None:
        self._projects = projects

    def has_project(self, project: str | None) -> bool:
        return bool(project) and project in self._projects

    def load_project(self, project: str) -> dict[str, Any]:
        return dict(self._projects.get(project) or {})


class _FakeTaskService:
    def __init__(self, mirror: str | None = "TASK-2026-00999") -> None:
        self.requests: list[Any] = []
        self.comments: list[tuple[str, str]] = []
        self._mirror = mirror
        self._n = 0

    async def create_task(self, request: Any) -> dict[str, Any]:
        self.requests.append(request)
        self._n += 1
        row: dict[str, Any] = {"task_id": f"shell-{self._n}"}
        if self._mirror:
            row["params"] = {"_mirrors": {"erpnext": {"name": self._mirror}}}
        return row

    async def comment_on_ticket(self, task_id: str, text: str) -> bool:
        self.comments.append((task_id, text))
        return True


def _service(repo: Any, task_service: Any, config: Any) -> Any:
    from objectives.service import ObjectiveService

    settings = SimpleNamespace(TIER_S_OBJECTIVE_CHILD_AUTORUN=True)
    return ObjectiveService(repo=repo, task_service=task_service,
                            config_loader=config, settings=settings,
                            rate_monitor=None)


class ObjectiveServiceTests(unittest.IsolatedAsyncioTestCase):
    def _project(self, policy: str) -> dict[str, Any]:
        return {
            "project": {"repos": []},
            "objectives": {"max_tickets": 20,
                           "max_active_objectives_global": 3},
            "objective_planner": {
                "timeout_minutes": 15, "policy_path": policy,
                "agents": {"dev": {"enabled": True}}},
        }

    async def test_create_validates_project_then_runs_engine(self) -> None:
        repo = _FakeObjectiveRepo()
        task_service = _FakeTaskService()
        policy = _scratch_repo(self) / "policy.md"
        policy.write_text("POLICY\n", encoding="utf-8")
        config = _FakeConfig({"proj": self._project(str(policy))})
        service = _service(repo, task_service, config)

        with self.assertRaises(ObjectiveRejected):
            await service.create_objective(project="nope", goal="g")
        # Unknown live role: the planner run fails closed without spawning.
        objective = await service.create_objective(
            project="proj", goal="Ship it", acceptance=["works"])
        self.assertEqual(objective["entry"], "top_down")
        self.assertEqual(objective["erp_group_ref"], "TASK-2026-00999")
        request = task_service.requests[0]
        self.assertTrue(request.is_group)
        self.assertFalse(request.autorun)
        self.assertEqual(request.objective_id,
                         objective["objective_id"])
        self.assertEqual(objective["status"], "waiting")

    async def test_env_ignores_legacy_tier_s_objectives(self) -> None:
        # TASK-2026-00141 retired the dev.tier_s.objectives fallback: the
        # engine reads the global objectives block only.
        from objectives.service import ObjectiveService

        config = _FakeConfig({"proj": {
            "project": {"repos": []},
            "dev": {"tier_s": {"objectives": {"max_tickets": 5,
                                              "legacy_only": 1}}},
            "objectives": {"max_tickets": 7, "max_active_objectives_global": 9},
            "objective_planner": {"agents": {}},
        }})
        service = ObjectiveService(
            repo=None, task_service=None, config_loader=config,
            settings=SimpleNamespace(TIER_S_OBJECTIVE_CHILD_AUTORUN=False),
            rate_monitor=None)
        env = service.env_for("proj")
        self.assertEqual(env.objectives_cfg["max_tickets"], 7)
        self.assertNotIn("legacy_only", env.objectives_cfg)
        self.assertEqual(env.objectives_cfg["max_active_objectives_global"], 9)
        self.assertFalse(env.objectives_cfg["child_autorun"])
        self.assertIsNone(env.erp)
        self.assertEqual(env.repo, Path(__file__).resolve().parents[1])

    async def test_workdir_prefers_the_primary_repo(self) -> None:
        from objectives.service import ObjectiveService

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = _FakeConfig({"proj": {
            "project": {"repos": [{"local_path": tmp.name}]},
            "dev": {},
        }})
        service = ObjectiveService(
            repo=None, task_service=None, config_loader=config,
            settings=SimpleNamespace(), rate_monitor=None)
        self.assertEqual(service.env_for("proj").repo, Path(tmp.name))

    async def test_propagate_rejects_non_parent(self) -> None:
        repo = _FakeObjectiveRepo()
        await repo.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        config = _FakeConfig({"proj": {"project": {"repos": []}, "dev": {}}})
        service = _service(repo, _FakeTaskService(), config)
        result = await service.propagate_group_decision(
            {"task_id": "task-kid", "objective_id": "OBJ-1"},
            target="cancelled", business_status="Cancelled")
        self.assertEqual(result["action"], "not-parent")
        stored = await repo.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "active")

    async def test_propagate_unknown_project_or_objective(self) -> None:
        repo = _FakeObjectiveRepo()
        await repo.create_objective({
            "objective_id": "OBJ-1", "project": "gone", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        config = _FakeConfig({"proj": {"project": {"repos": []}, "dev": {}}})
        service = _service(repo, _FakeTaskService(), config)
        unknown = await service.propagate_group_decision(
            {"task_id": "task-parent", "objective_id": "OBJ-1"},
            target="cancelled", business_status="Cancelled")
        self.assertFalse(unknown["ok"])
        missing = await service.propagate_group_decision(
            {"task_id": "task-x", "objective_id": "OBJ-nope"},
            target="cancelled", business_status="Cancelled")
        self.assertFalse(missing["ok"])

    async def test_propagate_cancel_runs_the_engine(self) -> None:
        repo = _FakeObjectiveRepo()
        await repo.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        repo.tasks["task-parent"] = {"task_id": "task-parent",
                                     "status": "cancelled",
                                     "objective_id": "OBJ-1"}
        repo.tasks["kid-1"] = dict(_child("kid-1", "queued", dev=False))
        policy = _scratch_repo(self) / "policy.md"
        policy.write_text("POLICY\n", encoding="utf-8")
        config = _FakeConfig({"proj": self._project(str(policy))})
        task_service = _FakeTaskService()
        service = _service(repo, task_service, config)
        result = await service.propagate_group_decision(
            {"task_id": "task-parent", "objective_id": "OBJ-1"},
            target="cancelled", business_status="Cancelled")
        self.assertEqual(result["action"], "cancelled")
        self.assertEqual(result["cancelled_children"], ["kid-1"])
        stored = await repo.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "cancelled")
        self.assertEqual(task_service.comments[0][0], "task-parent")


class WebhookPropagationTests(unittest.TestCase):
    def _service(self, adapter: Any, repo: Any) -> Any:
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._settings = SimpleNamespace(
            TASK_AUTORUN=False, HORIZON_PERSONAL_MIRROR_ENABLED=False)
        service._erpnext = adapter
        service._repo = repo
        service._hub = SimpleNamespace(broadcast=AsyncMock())
        service._horizon_terminal = None
        return service

    def test_converted_parent_propagates_to_objective(self) -> None:
        import asyncio

        from task_service import TaskService  # noqa: F401

        repo = _FakeObjectiveRepo()
        repo.tasks["task-parent"] = {
            "task_id": "task-parent", "status": "converted",
            "objective_id": "OBJ-1",
            "params": {"_mirrors": {"erpnext": {"name": "TASK-1"}}},
        }
        service = self._service(object(), repo)
        objective_service = MagicMock()
        objective_service.propagate_group_decision = AsyncMock(
            return_value={"ok": True, "action": "cancelled"})
        service.set_objective_service(objective_service)
        result = asyncio.run(service.apply_business_status(
            task_id="task-parent", business_status="Cancelled",
            deleted=False))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["task_status"], "cancelled")
        objective_service.propagate_group_decision.assert_awaited_once()
        _ticket, kwargs = objective_service.propagate_group_decision.call_args
        self.assertEqual(kwargs["target"], "cancelled")

    def test_plain_child_does_not_propagate(self) -> None:
        import asyncio

        repo = _FakeObjectiveRepo()
        repo.tasks["kid-1"] = {
            "task_id": "kid-1", "status": "in_progress",
            "objective_id": "OBJ-1",
            "params": {"_mirrors": {"erpnext": {"name": "TASK-2"}}},
        }
        service = self._service(object(), repo)
        objective_service = MagicMock()
        objective_service.propagate_group_decision = AsyncMock()
        service.set_objective_service(objective_service)
        result = asyncio.run(service.apply_business_status(
            task_id="kid-1", business_status="Completed", deleted=False))
        self.assertEqual(result["status"], "applied")
        self.assertNotIn("objective", result)
        objective_service.propagate_group_decision.assert_not_awaited()

    def test_propagation_failure_keeps_the_ticket_patch(self) -> None:
        import asyncio

        repo = _FakeObjectiveRepo()
        repo.tasks["task-parent"] = {
            "task_id": "task-parent", "status": "converted",
            "objective_id": "OBJ-1",
            "params": {"_mirrors": {"erpnext": {"name": "TASK-1"}}},
        }
        service = self._service(object(), repo)
        objective_service = MagicMock()
        objective_service.propagate_group_decision = AsyncMock(
            side_effect=RuntimeError("boom"))
        service.set_objective_service(objective_service)
        result = asyncio.run(service.apply_business_status(
            task_id="task-parent", business_status="Completed",
            deleted=False))
        self.assertEqual(result["status"], "applied")
        self.assertFalse(result["objective"]["ok"])
        _task_id, updates = repo.patches[-1]
        self.assertEqual(updates["status"], "completed")


class ApiSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_and_get_objective(self) -> None:
        import main
        from state import ObjectiveCreateRequest

        doc = {"objective_id": "OBJ-1", "status": "active"}
        user = SimpleNamespace(user_id="owner")
        payload = ObjectiveCreateRequest(project="proj", goal="g",
                                         acceptance=["a"])
        with patch.object(main.task_service, "create_objective",
                          AsyncMock(return_value=doc)) as create:
            result = await main.create_objective(payload, user)
        self.assertEqual(result, doc)
        _args, kwargs = create.call_args
        self.assertEqual(kwargs["requester"], "owner")
        self.assertEqual(kwargs["project"], "proj")
        with patch.object(main.task_service, "get_objective",
                          AsyncMock(return_value=doc)):
            self.assertEqual(await main.get_objective("OBJ-1", user), doc)
        with patch.object(main.task_service, "get_objective",
                          AsyncMock(return_value=None)):
            with self.assertRaises(Exception) as ctx:
                await main.get_objective("OBJ-nope", user)
            self.assertEqual(ctx.exception.status_code, 404)

    async def test_post_maps_rejections(self) -> None:
        import main
        from objectives.engine import ObjectiveErpError
        from state import ObjectiveCreateRequest

        user = SimpleNamespace(user_id="owner")
        payload = ObjectiveCreateRequest(project="proj", goal="g")
        cases = [(ObjectiveDuplicate("dupe", existing_id="OBJ-1"), 409),
                 (ObjectiveLimitReached("full"), 409),
                 (ObjectiveRejected("bad"), 400),
                 (ObjectiveErpError("erp down"), 502)]
        for error, status in cases:
            with patch.object(main.task_service, "create_objective",
                              AsyncMock(side_effect=error)):
                with self.assertRaises(Exception) as ctx:
                    await main.create_objective(payload, user)
                self.assertEqual(ctx.exception.status_code, status,
                                 f"{error!r} should map to {status}")


class FakeMCPServer:
    def __init__(self) -> None:
        self.tools: dict[str, tuple] = {}

    def tool(self, fn, *, name, description):  # noqa: ANN001 - firma de FastMCP
        self.tools[name] = (fn, description)


class McpSurfaceTests(unittest.TestCase):
    def _tools(self, task_service: Any) -> dict[str, Any]:
        import mcp_server

        server = FakeMCPServer()
        mcp_server._register_openceo_tools(
            server, SimpleNamespace(), task_service, None)
        return {name: fn for name, (fn, _desc) in server.tools.items()}

    def test_create_objective_tool(self) -> None:
        import asyncio

        task_service = _FakeTaskService()
        task_service.create_objective = AsyncMock(  # type: ignore[attr-defined]
            return_value={"objective_id": "OBJ-1"})
        tools = self._tools(task_service)
        result = asyncio.run(tools["openceo_create_objective"](
            project="proj", goal="Ship it", acceptance=["works"]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["objective_id"], "OBJ-1")
        _args, kwargs = task_service.create_objective.call_args
        self.assertEqual(kwargs["project"], "proj")
        self.assertEqual(kwargs["acceptance"], ["works"])

    def test_create_objective_tool_error(self) -> None:
        import asyncio

        task_service = _FakeTaskService()
        task_service.create_objective = AsyncMock(  # type: ignore[attr-defined]
            side_effect=ObjectiveRejected("bad"))
        tools = self._tools(task_service)
        result = asyncio.run(tools["openceo_create_objective"](
            project="proj", goal=""))
        self.assertFalse(result["ok"])
        self.assertIn("bad", result["message"])


class VoiceSurfaceTests(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, intent_json: dict[str, Any],
                 task_service: Any) -> Any:
        from first_layer import FirstLayerGateway

        gateway = FirstLayerGateway.__new__(FirstLayerGateway)
        gateway.horizon = None
        gateway._finance_handler = None

        class _Llm:
            async def ainvoke(self, _messages: Any) -> Any:
                return SimpleNamespace(content=json.dumps(intent_json))

        gateway._llm = _Llm()
        gateway.dev_session_service = SimpleNamespace(_tmux=None)
        gateway.task_service = task_service

        async def _ctx(_user_id: str) -> dict[str, Any]:
            return {"selected_project": "proj",
                    "projects": [{"name": "proj"}],
                    "active_ticket": None, "open_tasks": [], "open_count": 0,
                    "active_session": None, "all_tmux": []}

        gateway.gather_context = _ctx  # type: ignore[method-assign]
        return gateway

    async def test_voice_creates_objective(self) -> None:
        task_service = _FakeTaskService()
        task_service.create_objective = AsyncMock(  # type: ignore[attr-defined]
            return_value={"objective_id": "OBJ-1"})
        gateway = self._gateway(
            {"intent": "objective_create",
             "dictation": "ship the billing revamp",
             "project": None, "spoken_reply": ""},
            task_service)
        handled, reply = await gateway.process_turn(
            "owner", "crea un objetivo para renovar la facturación",
            SimpleNamespace())
        self.assertTrue(handled)
        self.assertIn("objetivo nuevo", reply)
        self.assertIn("Tenemos", reply)
        _args, kwargs = task_service.create_objective.call_args
        self.assertEqual(kwargs["goal"], "ship the billing revamp")
        self.assertEqual(kwargs["project"], "proj")
        self.assertEqual(kwargs["source"], "voice")

    async def test_voice_failure_is_friendly(self) -> None:
        task_service = _FakeTaskService()
        task_service.create_objective = AsyncMock(  # type: ignore[attr-defined]
            side_effect=ObjectiveRejected("unknown project nope"))
        gateway = self._gateway(
            {"intent": "objective_create", "dictation": "do things",
             "project": None, "spoken_reply": ""},
            task_service)
        handled, reply = await gateway.process_turn(
            "owner", "nuevo objetivo: haz cosas", SimpleNamespace())
        self.assertTrue(handled)
        self.assertIn("No pudimos crear el objetivo", reply)


class ConfigSurfaceTests(unittest.TestCase):
    def test_global_objectives_defaults(self) -> None:
        from config_loader import ConfigLoader
        from settings import Settings

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        loader = ConfigLoader(
            Settings(_env_file=None, PROJECTS_DIR=tmp.name))
        objectives = loader.load_project("anything")["objectives"]
        self.assertEqual(objectives["max_tickets"], 20)
        self.assertEqual(objectives["max_planning_runs_without_progress"], 2)
        self.assertEqual(objectives["max_final_reviews"], 2)
        self.assertEqual(objectives["inbox_items_for_planner"], 15)
        self.assertEqual(objectives["max_active_objectives_global"], 3)
        self.assertEqual(objectives["max_active_objectives_per_project"], 2)
        self.assertEqual(objectives["max_active_tickets_per_objective"], 6)

    def test_objectives_section_is_rejected(self) -> None:
        from config_loader import ConfigLoader
        from settings import Settings

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "proj").mkdir(parents=True, exist_ok=True)
        (root / "proj" / "project.yaml").write_text(
            "objectives:\n  max_tickets: 99\n", encoding="utf-8")
        loader = ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(root)))
        errors = loader.validate_project("proj")
        self.assertEqual(len(errors), 1)
        self.assertIn("objectives", errors[0])
        self.assertIn("global", errors[0])


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_is_group_reaches_adapter_and_mirror(self) -> None:
        from state import (  # noqa: F401 - registra el modelo
            TaskCreateRequest,
            TaskSource,
            make_task_document,
        )
        from task_service import TaskService

        calls: list[dict[str, Any]] = []

        class _Adapter:
            is_enabled = True

            class client:  # noqa: D106 - doble minimo
                @staticmethod
                def config_error() -> None:
                    return None

            async def create_ticket(self, **kwargs: Any) -> dict[str, Any]:
                calls.append(kwargs)
                return {"ok": True, "task": {"name": "TASK-9"},
                        "ref": kwargs.get("ref")}

        service = TaskService.__new__(TaskService)
        service._settings = SimpleNamespace(
            TASK_AUTORUN=False, HORIZON_PERSONAL_MIRROR_ENABLED=False)
        service._erpnext = _Adapter()
        service._repo = SimpleNamespace()
        doc = make_task_document(TaskCreateRequest(
            source=TaskSource.API, title="t", description="d", project="p"))
        mirror, pending = await service._register_business_ticket(
            doc, is_group=True)
        self.assertFalse(pending)
        self.assertTrue(calls[0]["is_group"])
        self.assertTrue(mirror["is_group"])

    async def test_internal_create_forwards_hierarchy(self) -> None:
        from objectives.service import create_internal_ticket

        task_service = _FakeTaskService()
        row = await create_internal_ticket(task_service, {
            "title": "kid", "description": "d", "project": "proj",
            "objective_id": "OBJ-1", "parent_task": "TASK-1",
            "depends_on": ["TASK-2"], "is_group": False,
            "source": "spillover", "source_id": "s1",
        })
        self.assertIsNotNone(row)
        request = task_service.requests[0]
        self.assertEqual(request.parent_task, "TASK-1")
        self.assertEqual(request.depends_on, ["TASK-2"])
        self.assertFalse(request.is_group)
        self.assertIsNone(await create_internal_ticket(None, {"title": "x"}))

    async def test_group_flags_never_enter_the_document(self) -> None:
        from state import TaskCreateRequest, TaskSource, make_task_document

        doc = make_task_document(TaskCreateRequest(
            source=TaskSource.API, title="t", description="d",
            parent_task="TASK-1", depends_on=["TASK-2"], is_group=True))
        dumped = doc.model_dump()
        self.assertNotIn("is_group", dumped)
        self.assertNotIn("parent_task", dumped)
        self.assertNotIn("depends_on", dumped)


_TIER_S_PACKAGE_RE = re.compile(r"agents\.dev\.tier_s(?![\w])")


def _tier_s_package_hits(package: Path) -> list[str]:
    """``file:line`` hits importing the Tier S engine package.

    The sibling ``agents.dev.tier_s_contract`` module is explicitly allowed
    (shared git/env helpers, not the engine); the negative lookahead keeps
    it out of the hits.
    """
    hits: list[str] = []
    for path in sorted(package.glob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _TIER_S_PACKAGE_RE.search(line):
                hits.append(f"{path.name}:{lineno}: {line.strip()}")
    return hits


class PackageLocationTests(unittest.TestCase):
    def test_engine_lives_outside_tier_s(self) -> None:
        import objectives.engine

        location = Path(objectives.engine.__file__ or "")
        self.assertEqual(location.parent.name, "objectives")
        tier_s = CORE / "agents" / "dev" / "tier_s"
        self.assertFalse((tier_s / "objectives.py").exists())
        self.assertFalse((tier_s / "objective_planner_models.py").exists())

    def test_objectives_never_imports_tier_s(self) -> None:
        """Acceptance of TASK-2026-00141: the grep stays empty."""
        self.assertEqual([], _tier_s_package_hits(CORE / "objectives"))

    def test_orchestration_never_imports_tier_s(self) -> None:
        """The shared package must not reach back into the engine."""
        self.assertEqual([], _tier_s_package_hits(CORE / "orchestration"))

    def test_planner_role_resolves_without_tier_s(self) -> None:
        from objectives.service import objectives_wiring_config

        wiring = objectives_wiring_config({
            # No "dev" key at all: global blocks only.
            "objective_planner": {
                "role": {"command": ["musej"], "active": True}},
            "objectives": {"max_tickets": 20},
            "review": {
                "roles": {"closer": {"command": ["musej"]},
                          "arbiter": {"command": ["claudej"]}},
                "reviewers": [{"label": "r1"}],
                "review": {"quorum": 2},
                "timeouts_minutes": {"closer": 15},
                "fix_ticket_tiers": {"default": "tier_c"},
                "inbox": {"promotion": {"severity": "CRITICAL"}},
            },
        })
        self.assertEqual(wiring["roles"]["objective_planner"]["command"],
                         ["musej"])
        self.assertIn("closer", wiring["roles"])
        self.assertIn("arbiter", wiring["roles"])
        self.assertEqual(wiring["pool"], [{"label": "r1"}])
        self.assertEqual(wiring["review_cfg"], {"quorum": 2})
        self.assertEqual(wiring["objectives_cfg"], {"max_tickets": 20})
        self.assertEqual(wiring["timeouts"], {"closer": 15})
        self.assertEqual(wiring["fix_tiers"], {"default": "tier_c"})
        self.assertEqual(wiring["inbox_rule"], {"severity": "CRITICAL"})

    def test_inactive_planner_role_is_skipped(self) -> None:
        from objectives.service import objectives_wiring_config

        wiring = objectives_wiring_config({
            "objective_planner": {"role": {"active": False}},
            "objectives": {},
            "review": {},
        })
        self.assertNotIn("objective_planner", wiring["roles"])
        self.assertEqual(wiring["roles"], {})


class SharedScratchRootTests(unittest.TestCase):
    """`.tier-s/` is the orchestrator-wide scratch (TASK-2026-00136).

    The objectives engine keeps ``objectives/<id>/``,
    ``PLANNER_RESULT.json`` and ``OBJECTIVE_CLOSURE.md`` under the same
    root as the Tier S executor files and the shared ``status`` signal
    bus. The simulated planners in these tests keep pinning the literal
    paths independently (wire format); this class pins the engine side.
    TASK-2026-00141 owns any future split of the root.
    """

    def test_dirname_is_pinned(self) -> None:
        self.assertEqual(engine_mod.SCRATCH_DIRNAME, ".tier-s")

    def test_helpers_resolve_under_repo(self) -> None:
        repo = _scratch_repo(self)
        self.assertEqual(engine_mod.scratch_root(repo), repo / ".tier-s")
        self.assertEqual(engine_mod.objective_scratch(repo, "OBJ-X"),
                         repo / ".tier-s" / "objectives" / "OBJ-X")

    def test_root_is_shared_with_tier_s(self) -> None:
        from agents.dev.tier_s import cycle as cycle_mod

        repo = _scratch_repo(self)
        self.assertEqual(engine_mod.scratch_root(repo), cycle_mod._tier_dir(repo))

    def test_closure_prompt_names_canonical_status(self) -> None:
        repo = _scratch_repo(self)
        prompt = engine_mod._closure_judge_prompt(
            1, "goal", "capsule", repo,
            result_path=".tier-s/objectives/OBJ-1/OBJECTIVE_CLOSURE.md")
        self.assertIn(f"{engine_mod.SCRATCH_DIRNAME}/status", prompt)


if __name__ == "__main__":
    unittest.main()
