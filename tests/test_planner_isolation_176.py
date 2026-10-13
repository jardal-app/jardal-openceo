"""Planner output isolation between concurrent cycles (TASK-2026-00176).

Contract fixed here:

- Exclusive per-objective output: the planner/closer prompt names
  ``.openceo/objectives/<id>/PLANNER_RESULT.json`` (resp.
  ``OBJECTIVE_CLOSURE.md``); each run reads only its objective's file,
  so concurrent cycles of one repo can neither feed each other nor eat
  each other's output. The legacy global files are never read.
- Single planner flight per objective: triggers for different
  objectives plan in parallel and independently; triggers for the same
  objective share one flight (latecomers mark a rerun and await the
  final result) — no lost or duplicated planning, no lost
  ``planning_runs`` updates. A call from inside the leader's own run
  (final-review recursion) runs inline instead of deadlocking.

Simulated executors with barriers force the interleavings; no models.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import engine as engine_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _close_objective,
    _closure_judge_prompt,
    _PlanFlight,
    _PLAN_FLIGHTS,
    _planner_prompt,
    plan_objective,
)

TIMEOUT = 30.0


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", check=True)
    return proc.stdout.strip()


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "t@e.invalid")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _output_target(repo: Path, prompt: str) -> Path:
    """File path the prompt orders (planner and closer alike)."""
    for line in prompt.splitlines():
        if line.startswith("OUTPUT: write "):
            rel = line[len("OUTPUT: write "):].split(" with exactly")[0]
            return repo / rel.strip()
        if line.startswith("Write ") and " with one line per finding" in line:
            rel = line[len("Write "):].split(" with one line")[0]
            return repo / rel.strip()
    raise AssertionError("prompt names no output target")


def _dev_ticket(local_ref: str, title: str) -> dict[str, Any]:
    return {
        "local_ref": local_ref,
        "title": title,
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": "g",
        "why_now": "now",
        "inputs": [],
        "constraints": [],
        "out_of_scope": [],
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["tracked.txt"],
        "blocked_by": [],
        "coding_tool": "tier_c",
        "tier_reason": "specified work (§25)",
    }


def _plan_payload(title: str, ref: str = "a") -> dict[str, Any]:
    return {
        "objective_status": "active",
        "state_summary": f"plan {title}",
        "tickets": [_dev_ticket(ref, title)],
    }


def _objective_doc(objective_id: str, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "objective_id": objective_id,
        "project": "proj",
        "status": "active",
        "anchor": {"goal": "g", "acceptance": ["ship"]},
        "capsule": {"text": "", "compiled_at": None, "tokens": 0},
        "tickets_log": [],
        "erp_group_ref": f"TASK-{objective_id}",
    }
    doc.update(over)
    return doc


def _env(repo: Path, executor: Any,
         tasks: dict[str, dict[str, Any]] | None = None,
         ) -> tuple[ObjectiveEnv, list[dict[str, Any]]]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: route by oracle.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    listed: dict[str, dict[str, Any]] = dict(tasks or {})
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        task_id = f"task-child-{counter['n']}"
        created.append({"task_id": task_id, **dict(payload)})
        listed[task_id] = {"task_id": task_id, "status": "queued",
                           "title": payload.get("title"),
                           "objective_id": payload.get("objective_id")}
        return {"task_id": task_id}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        if task_id in listed:
            listed[task_id].update(updates)

    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(listed),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=executor,  # type: ignore[arg-type]
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
    )
    return env, created


class _BarrierPlanner:
    """Two-objective planner: forces a write/write overlap per prompt."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self.runs = 0
        self._target: Path | None = None
        self.a_wrote = asyncio.Event()
        self.b_wrote = asyncio.Event()

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "objective_planner"
        self.runs += 1
        try:
            target = _output_target(self._repo, prompt)
            self._target = target
        except AssertionError:
            assert self._target is not None  # retry: fix in place
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        if "Objective OBJ-A:" in prompt:
            target.write_text(json.dumps(_plan_payload("A-child")),
                              encoding="utf-8")
            self.a_wrote.set()
            await asyncio.wait_for(self.b_wrote.wait(), TIMEOUT)
        else:
            assert "Objective OBJ-B:" in prompt
            await asyncio.wait_for(self.a_wrote.wait(), TIMEOUT)
            target.write_text(json.dumps(_plan_payload("B-child")),
                              encoding="utf-8")
            self.b_wrote.set()
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


class PlannerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_objectives_keep_their_own_plans(self) -> None:
        repo = _scratch_repo(self)
        planner = _BarrierPlanner(repo)
        env, created = _env(repo, planner)
        await env.objectives.create_objective(_objective_doc("OBJ-A"))
        await env.objectives.create_objective(_objective_doc("OBJ-B"))

        res_a, res_b = await asyncio.gather(
            plan_objective(env, "OBJ-A"), plan_objective(env, "OBJ-B"))

        self.assertTrue(res_a["ok"])
        self.assertTrue(res_b["ok"])
        self.assertEqual(planner.runs, 2)
        by_objective: dict[str, list[str]] = {}
        for row in created:
            by_objective.setdefault(str(row["objective_id"]), []).append(
                str(row["title"]))
            self.assertTrue(
                str(row["source_id"]).startswith(
                    f"obj:{row['objective_id']}:"))
        # Each plan created children only of its own objective: no loss,
        # no duplication, no cross-attribution.
        self.assertEqual(by_objective, {"OBJ-A": ["A-child"],
                                        "OBJ-B": ["B-child"]})
        for row in created:
            self.assertEqual(row["parent_task"],
                             f"TASK-{row['objective_id']}")
        runs_a = (await env.objectives.get_objective("OBJ-A") or {}
                  ).get("planning_runs") or []
        runs_b = (await env.objectives.get_objective("OBJ-B") or {}
                  ).get("planning_runs") or []
        self.assertEqual(len(runs_a), 1)
        self.assertEqual(len(runs_b), 1)
        self.assertNotIn("OBJ-A", _PLAN_FLIGHTS)
        self.assertNotIn("OBJ-B", _PLAN_FLIGHTS)

    async def test_same_objective_triggers_coalesce(self) -> None:
        repo = _scratch_repo(self)
        release = asyncio.Event()
        run_started = asyncio.Event()
        prompts: list[str] = []
        runs = {"n": 0}

        class _Gated:
            async def run(self, role: str, prompt: str, workdir: Path,
                          timeout_s: int) -> Any:
                del workdir, timeout_s
                assert role == "objective_planner"
                runs["n"] += 1
                prompts.append(prompt)
                target = _output_target(repo, prompt)
                target.parent.mkdir(parents=True, exist_ok=True)
                if runs["n"] == 1:
                    target.write_text(
                        json.dumps(_plan_payload("first")), encoding="utf-8")
                    run_started.set()
                    await asyncio.wait_for(release.wait(), TIMEOUT)
                else:
                    target.write_text(
                        json.dumps(_plan_payload("second", ref="b")),
                        encoding="utf-8")
                return SimpleNamespace(returncode=0,
                                       output_tail="PLANNER_READY")

            async def cleanup(self, role: str) -> None:
                del role

        env, created = _env(repo, _Gated())
        await env.objectives.create_objective(_objective_doc("OBJ-1"))
        first = asyncio.create_task(plan_objective(env, "OBJ-1"))
        await asyncio.wait_for(run_started.wait(), TIMEOUT)
        second = asyncio.create_task(plan_objective(env, "OBJ-1"))
        third = asyncio.create_task(plan_objective(env, "OBJ-1"))
        for _ in range(1000):
            flight = _PLAN_FLIGHTS.get("OBJ-1")
            if flight is not None and flight.rerun_requested:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("triggers never attached to the flight")
        release.set()
        results = await asyncio.gather(first, second, third)

        # Three triggers, two runs: the leader plus one rerun covering
        # every attached trigger — coalesced, not triple-planned.
        self.assertEqual(runs["n"], 2)
        for result in results:
            self.assertTrue(result["ok"])
            self.assertEqual(result["tickets_created"], 1)
        titles = sorted(str(row["title"]) for row in created)
        self.assertEqual(titles, ["first", "second"])
        # The rerun planned over fresh state: run 1's child is open.
        self.assertEqual(len(prompts), 2)
        self.assertIn("first", prompts[1])
        stored_runs = (await env.objectives.get_objective("OBJ-1") or {}
                       ).get("planning_runs") or []
        self.assertEqual(len(stored_runs), 2)
        self.assertNotIn("OBJ-1", _PLAN_FLIGHTS)

    async def test_reentrant_call_runs_inline(self) -> None:
        repo = _scratch_repo(self)

        class _Recorded:
            def __init__(self) -> None:
                self.runs = 0

            async def run(self, role: str, prompt: str, workdir: Path,
                          timeout_s: int) -> Any:
                del workdir, timeout_s
                self.runs += 1
                target = _output_target(repo, prompt)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(_plan_payload("solo")),
                                  encoding="utf-8")
                return SimpleNamespace(returncode=0,
                                       output_tail="PLANNER_READY")

            async def cleanup(self, role: str) -> None:
                del role

        planner = _Recorded()
        env, created = _env(repo, planner)
        await env.objectives.create_objective(_objective_doc("OBJ-1"))
        me = asyncio.current_task()
        holder = _PlanFlight(task=me)  # type: ignore[arg-type]
        _PLAN_FLIGHTS["OBJ-1"] = holder
        try:
            result = await plan_objective(env, "OBJ-1")
        finally:
            _PLAN_FLIGHTS.pop("OBJ-1", None)

        # Ran inline instead of awaiting our own task (no deadlock).
        self.assertTrue(result["ok"])
        self.assertEqual(planner.runs, 1)
        self.assertEqual([row["title"] for row in created], ["solo"])

    async def test_prompt_names_per_objective_output(self) -> None:
        repo = _scratch_repo(self)
        prompt = _planner_prompt(
            {"objective_id": "OBJ-9", "anchor": {"goal": "g"}},
            "", [], "INBOX\n(none)\n", [], repo=repo)
        self.assertIn(".openceo/objectives/OBJ-9/PLANNER_RESULT.json", prompt)
        judge = _closure_judge_prompt(
            1, "goal", "capsule", repo,
            findings_path=".openceo/objectives/OBJ-9/OBJECTIVE_FINDINGS.md",
            result_path=".openceo/objectives/OBJ-9/OBJECTIVE_CLOSURE.md")
        self.assertIn(".openceo/objectives/OBJ-9/OBJECTIVE_CLOSURE.md", judge)


def _finding(item_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=item_id, severity="MAJOR", category="bug",
                           anchors=[], problem="p", fix="f",
                           families=["f1"])


class _BarrierCloser:
    """Two-objective closer: forces a verdict/write overlap per prompt."""

    def __init__(self, repo: Path) -> None:
        self._repo = repo
        self.runs = 0
        self._target: Path | None = None
        self.a_wrote = asyncio.Event()
        self.b_wrote = asyncio.Event()

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "closer", role
        self.runs += 1
        try:
            target = _output_target(self._repo, prompt)
            self._target = target
        except AssertionError:
            assert self._target is not None  # retry: fix in place
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        if "OBJ-A" in prompt or "F11" in prompt:
            target.write_text("F11: REAL — a\n", encoding="utf-8")
            self.a_wrote.set()
            await asyncio.wait_for(self.b_wrote.wait(), TIMEOUT)
        else:
            assert "OBJ-B" in prompt or "F22" in prompt
            await asyncio.wait_for(self.a_wrote.wait(), TIMEOUT)
            target.write_text("F22: REAL — b\n", encoding="utf-8")
            self.b_wrote.set()
        return SimpleNamespace(returncode=0, output_tail="CLOSURE_READY")

    async def cleanup(self, role: str) -> None:
        del role


class CloserIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_closes_keep_their_own_verdicts(self) -> None:
        repo = _scratch_repo(self)
        closer = _BarrierCloser(repo)
        env, created = _env(repo, closer)
        await env.objectives.create_objective(_objective_doc("OBJ-A"))
        await env.objectives.create_objective(_objective_doc("OBJ-B"))
        obj_a = await env.objectives.get_objective("OBJ-A")
        obj_b = await env.objectives.get_objective("OBJ-B")
        assert obj_a is not None and obj_b is not None

        res_a, res_b = await asyncio.gather(
            _close_objective(env, obj_a, [_finding(11)], "head"),
            _close_objective(env, obj_b, [_finding(22)], "head"))

        self.assertTrue(res_a["ok"])
        self.assertTrue(res_b["ok"])
        by_objective: dict[str, list[str]] = {}
        for row in created:
            by_objective.setdefault(str(row["objective_id"]), []).append(
                str(row["source_id"]))
        self.assertEqual(len(by_objective.get("OBJ-A", [])), 1)
        self.assertEqual(len(by_objective.get("OBJ-B", [])), 1)
        self.assertIn(":11:", by_objective["OBJ-A"][0])
        self.assertIn(":22:", by_objective["OBJ-B"][0])


if __name__ == "__main__":
    unittest.main()
