"""Single planner cycle: convert -> children through PlannerResult.

Acceptance for TASK-2026-00112: one convert_ticket -> children cycle with a
simulated Muse planner (recorded PLANNER_RESULT.json, zero tokens), the
complete_candidate / blocked / capability_gap outcomes, invalid-output
retry, and the simple-tier trigger wiring fix. No network, no CLI, no LLM.
"""

from __future__ import annotations

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

from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    convert_ticket,
    plan_objective,
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
        target.write_text(payload["__raw__"]
                          if "__raw__" in payload
                          else json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _planner_env(test: unittest.TestCase, repo: Path,
                 payloads: list[dict[str, Any]],
                 tasks: dict[str, dict[str, Any]] | None = None,
                 ) -> tuple[ObjectiveEnv, _RecordedPlanner,
                            list[dict[str, Any]], dict[str, Any]]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: route by oracle, not quota.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    patches: dict[str, dict[str, Any]] = {}
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        created.append(dict(payload))
        return {"task_id": f"task-child-{counter['n']}"}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        patches.setdefault(task_id, {}).update(updates)

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
        notify=AsyncMock(),
        repo=repo,
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True}},
        },
    )
    return env, planner, created, patches


def _dev_ticket(local_ref: str, title: str, tier: str = "tier_b",
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
        "coding_tool": tier,
        "tier_reason": "bounded work",
    }
    if tier in ("tier_a", "tier_b") and "why_not_c" not in overrides:
        ticket["why_not_c"] = "a misjudged step ships wrong behavior"
    ticket.update(overrides)
    return ticket


class PlannerCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_convert_creates_children_through_planner(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "active",
            "state_summary": "start with two tickets",
            "tickets": [
                _dev_ticket("a", "First child"),
                _dev_ticket("b", "Second child", blocked_by=["a"],
                            tier="tier_c",
                            tier_reason="covered by tests; reversible",
                            oracle={"type": "preexisting_tests",
                                    "refs": [{"ref": "tests/test_x.py"}]}),
            ],
            "roadmap_remaining": ["later"],
        }
        env, planner, created, patches = _planner_env(self, repo, [payload])
        ticket = {"task_id": "task-parent", "title": "too big",
                  "description": "needs an objective",
                  "acceptance": ["done"], "project": "proj"}

        objective = await convert_ticket(env, ticket, "OBJECTIVE_NEEDED", "")

        self.assertEqual(patches["task-parent"]["status"], "converted")
        self.assertEqual(len(created), 2)
        # Topological order with local refs remapped to task ids.
        self.assertEqual(created[0]["title"], "First child")
        self.assertEqual(created[1]["blocked_by"], ["task-child-1"])
        self.assertEqual(created[0]["coding_tool"], "tier_b")
        self.assertEqual(created[0]["agent_hint"], "dev")
        self.assertEqual(created[0]["params"]["target_agent"], "dev")
        self.assertEqual(created[0]["params"]["tier_reason"], "bounded work")
        self.assertEqual(created[0]["params"]["why_not_c"],
                         "a misjudged step ships wrong behavior")
        self.assertEqual(created[1]["params"]["oracle"]["type"],
                         "preexisting_tests")
        self.assertIn("GOAL:", created[0]["description"])
        for item in created:
            self.assertEqual(item["origin"], "objective_planner")
            self.assertTrue(item["source_id"].startswith(
                f"obj:{objective['objective_id']}:"))
        stored = await env.objectives.get_objective(
            objective["objective_id"])
        assert stored is not None
        self.assertEqual(stored["planning_runs"][-1]["status"], "active")
        self.assertEqual(stored["roadmap_remaining"], ["later"])
        tele = stored["planner_telemetry"]
        self.assertEqual(tele["cycles"], 1)
        self.assertEqual(tele["tickets_created"], 2)
        self.assertEqual(tele["tier_mix"], {"tier_b": 1, "tier_c": 1})
        self.assertEqual(planner.runs, 1)
        self.assertIn("PLANNER_RESULT.json", planner.prompts[0])

    async def test_complete_candidate_enters_final_review(self) -> None:
        repo = _scratch_repo(self)
        payload = {"objective_status": "complete_candidate",
                   "state_summary": "all acceptance met", "tickets": []}
        env, _planner, _created, _patches = _planner_env(
            self, repo, [payload])
        ticket = {"task_id": "task-parent", "title": "t", "description": "d",
                  "acceptance": [], "project": "proj"}

        objective = await convert_ticket(env, ticket, "OBJECTIVE_NEEDED", "")

        stored = await env.objectives.get_objective(
            objective["objective_id"])
        assert stored is not None
        # TASK-2026-00175 (deliberate contract change, supersedes the
        # 00117 vacuous close): complete_candidate with no work children
        # parks as data_gaps — closing would stamp verified delivery
        # with zero acceptance coverage. It still went through final
        # review; it never closed directly.
        self.assertEqual(stored.get("final_reviews", 0), 0)
        self.assertEqual(stored["status"], "waiting")
        assert stored["waiting_reason"].startswith("data_gaps:")
        assert "no work children" in stored["waiting_reason"]
        assert "delivery" not in stored

    async def test_blocked_parks_with_summary(self) -> None:
        repo = _scratch_repo(self)
        payload = {"objective_status": "blocked",
                   "state_summary": "waiting on owner credential",
                   "tickets": []}
        env, _planner, created, _patches = _planner_env(self, repo, [payload])
        ticket = {"task_id": "task-parent", "title": "t", "description": "d",
                  "acceptance": [], "project": "proj"}

        objective = await convert_ticket(env, ticket, "OBJECTIVE_NEEDED", "")

        self.assertEqual(created, [])
        stored = await env.objectives.get_objective(
            objective["objective_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertIn("blocked", stored["waiting_reason"])
        self.assertIn("credential", stored["waiting_reason"])

    async def test_capability_gap_persists_signal(self) -> None:
        repo = _scratch_repo(self)
        payload = {
            "objective_status": "capability_gap",
            "state_summary": "needs a payments agent",
            "information_gaps": [{"gap": "no payments agent",
                                  "materiality": "high",
                                  "resolution": "CEO registers one"}],
            "tickets": [],
        }
        env, _planner, _created, _patches = _planner_env(self, repo, [payload])
        ticket = {"task_id": "task-parent", "title": "t", "description": "d",
                  "acceptance": [], "project": "proj"}

        objective = await convert_ticket(env, ticket, "OBJECTIVE_NEEDED", "")

        stored = await env.objectives.get_objective(
            objective["objective_id"])
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertIn("capability_gap", stored["waiting_reason"])
        signal = stored["capability_gap"]
        self.assertIn("payments agent", signal["summary"])
        self.assertEqual(signal["gaps"][0]["materiality"], "high")
        self.assertIn("declared_at", signal)

    async def test_invalid_output_retries_then_parks(self) -> None:
        repo = _scratch_repo(self)
        bad = {"__raw__": "{not valid json"}
        env, planner, created, _patches = _planner_env(self, repo, [bad])
        store = env.objectives
        await store.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })

        result = await plan_objective(env, "OBJ-1")

        self.assertFalse(result["ok"])
        # TASK-2026-00118: an executor fault is operational, not an owner
        # decision; the next trigger retries planning.
        self.assertEqual(result["reason"], "planner_error")
        self.assertIn("PLANNER_RESULT", result["detail"])
        self.assertEqual(created, [])
        self.assertEqual(planner.runs, 2)  # initial + one retry
        self.assertIn("invalid", planner.prompts[1])

    async def test_retry_recovers_with_valid_output(self) -> None:
        repo = _scratch_repo(self)
        bad = {"__raw__": "nope"}
        good = {"objective_status": "active", "state_summary": "ok",
                "tickets": [_dev_ticket("a", "Child")]}

        # First attempt writes garbage; the retry writes valid JSON.
        class _Flaky(_RecordedPlanner):
            async def run(self, role: str, prompt: str, workdir: Path,
                          timeout_s: int) -> Any:
                if self.runs == 0:
                    self.prompts.append(prompt)
                    self.runs += 1
                    target = _output_target(self._repo, prompt)
                    self._target = target
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("nope", encoding="utf-8")
                    return SimpleNamespace(returncode=0, output_tail="")
                self._payloads = [good]
                return await super().run(role, prompt, workdir, timeout_s)

        env, _planner, created, _patches = _planner_env(self, repo, [bad])
        flaky = _Flaky(repo, [bad])
        env.executor = flaky  # type: ignore[assignment]
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })

        result = await plan_objective(env, "OBJ-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["tickets_created"], 1)
        self.assertEqual(created[0]["title"], "Child")

    async def test_oversized_roadmap_is_rejected_then_corrected_on_retry(self) -> None:
        # TASK-2026-00183: the outlook is bounded; the planner rewrites it in
        # the retry the cycle already has, and only the valid list persists.
        repo = _scratch_repo(self)
        too_long = {"objective_status": "active", "state_summary": "ok",
                    "tickets": [_dev_ticket("a", "Child")],
                    "roadmap_remaining": [f"step {n}" for n in range(11)]}
        fixed = {"objective_status": "active", "state_summary": "ok",
                 "tickets": [_dev_ticket("a", "Child")],
                 "roadmap_remaining": ["migrate data; depends on Child",
                                       "retire the old path; depends on the migration"]}
        env, planner, created, _patches = _planner_env(
            self, repo, [too_long, fixed])
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })

        result = await plan_objective(env, "OBJ-1")

        self.assertTrue(result["ok"], result)
        self.assertEqual(planner.runs, 2)            # initial + the retry
        self.assertIn("roadmap_remaining carries at most 10 lines",
                      planner.prompts[1])
        self.assertEqual(len(created), 1)            # created once, after the fix
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["roadmap_remaining"],
                         fixed["roadmap_remaining"])

    async def test_the_outlook_returns_to_the_next_cycle_prompt(self) -> None:
        repo = _scratch_repo(self)
        first = {"objective_status": "active", "state_summary": "ok",
                 "tickets": [_dev_ticket("a", "Child")],
                 "roadmap_remaining": ["ship the API; depends on Child"]}
        env, planner, _created, _patches = _planner_env(self, repo, [first])
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })
        await plan_objective(env, "OBJ-1")
        self.assertNotIn("ship the API", planner.prompts[0])

        env.executor._payloads = [{**first, "tickets": [_dev_ticket("b", "Second")],
                                   "roadmap_remaining": ["only what is left"]}]
        await plan_objective(env, "OBJ-1")

        self.assertIn("- ship the API; depends on Child", planner.prompts[1])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["roadmap_remaining"], ["only what is left"])

    async def test_every_run_records_the_policy_it_saw(self) -> None:
        # TASK-2026-00184: planning_runs[].policy_sha traces a decision to the
        # exact policy text the prompt carried (never part of the prompt).
        from objectives.planner_models import policy_fingerprint

        repo = _scratch_repo(self)
        first = {"objective_status": "active", "state_summary": "ok",
                 "tickets": [_dev_ticket("a", "Child")]}
        second = {"objective_status": "active", "state_summary": "ok",
                  "tickets": [_dev_ticket("b", "Second")]}
        env, planner, _created, _patches = _planner_env(
            self, repo, [first, second])
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })

        await plan_objective(env, "OBJ-1")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        original = "POLICY: route by oracle, not quota.\n"
        self.assertEqual(stored["planning_runs"][-1]["policy_sha"],
                         policy_fingerprint(original))
        self.assertNotIn(policy_fingerprint(original), planner.prompts[0])

        # Editing the policy changes what the next run records.
        (repo / "policy.md").write_text(
            "POLICY: route by nature, S in any domain.\n", encoding="utf-8")
        await plan_objective(env, "OBJ-1")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        runs = stored["planning_runs"]
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[1]["policy_sha"], policy_fingerprint(
            "POLICY: route by nature, S in any domain.\n"))
        self.assertNotEqual(runs[0]["policy_sha"], runs[1]["policy_sha"])
        self.assertIn("S in any domain", planner.prompts[1])

    async def test_missing_registry_parks_before_invocation(self) -> None:
        repo = _scratch_repo(self)
        env, planner, _created, _patches = _planner_env(
            self, repo, [{"objective_status": "active",
                          "state_summary": "x", "tickets": []}])
        env.planner_cfg = dict(env.planner_cfg, agents={})
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })

        result = await plan_objective(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertIn("registry", result["detail"])
        self.assertEqual(planner.runs, 0)

    async def test_unreadable_policy_parks_before_invocation(self) -> None:
        repo = _scratch_repo(self)
        env, planner, _created, _patches = _planner_env(
            self, repo, [{"objective_status": "active",
                          "state_summary": "x", "tickets": []}])
        env.planner_cfg = dict(env.planner_cfg,
                               policy_path=str(repo / "nowhere.md"))
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })

        result = await plan_objective(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertIn("policy", result["detail"])
        self.assertEqual(planner.runs, 0)

    async def test_stale_result_file_is_never_reused(self) -> None:
        repo = _scratch_repo(self)
        stale = {
            "objective_status": "active", "state_summary": "old cycle",
            "tickets": [_dev_ticket("old", "Stale child")],
        }
        target = (repo / ".tier-s" / "objectives" / "OBJ-1"
                  / "PLANNER_RESULT.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(stale), encoding="utf-8")
        legacy = repo / ".tier-s" / "PLANNER_RESULT.json"
        legacy.write_text(json.dumps(stale), encoding="utf-8")
        env, _planner, created, _patches = _planner_env(self, repo, [stale])

        class _Silent:
            runs = 0

            async def run(self, role: str, prompt: str, workdir: Path,
                          timeout_s: int) -> Any:
                del role, prompt, workdir, timeout_s
                type(self).runs += 1
                return SimpleNamespace(returncode=0, output_tail="")

            async def cleanup(self, role: str) -> None:
                del role

        silent = _Silent()
        env.executor = silent  # type: ignore[assignment]
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "p", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "capsule": {"text": "", "compiled_at": None, "tokens": 0},
            "tickets_log": [],
        })

        result = await plan_objective(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertIn("no PLANNER_RESULT.json", result["detail"])
        self.assertEqual(created, [])
        self.assertFalse(target.exists())
        # The legacy global file is neither read nor swept anymore.
        self.assertTrue(legacy.exists())


class StallCounterTests(unittest.TestCase):
    def test_only_active_runs_count_as_stalled(self) -> None:
        from objectives.engine import _trailing_stalled_runs

        active_empty = {"status": "active", "tickets_created": 0,
                        "complete": False}
        self.assertEqual(_trailing_stalled_runs(
            [active_empty, active_empty]), 2)
        self.assertEqual(_trailing_stalled_runs(
            [{"status": "blocked", "tickets_created": 0,
              "complete": False}]), 0)
        self.assertEqual(_trailing_stalled_runs(
            [active_empty,
             {"status": "capability_gap", "tickets_created": 0,
              "complete": False}]), 0)
        # Legacy entries without status keep the old reading.
        self.assertEqual(_trailing_stalled_runs(
            [{"tickets_created": 0}]), 1)
        self.assertEqual(_trailing_stalled_runs(
            [active_empty,
             {"status": "active", "tickets_created": 2,
              "complete": False}]), 0)


# NOTE (TASK-2026-00169): the simple close no longer triggers the
# objective inline (that pre-persist read wedged the objective). The
# session stashes the close context and the funnel notifies once after
# persisting COMPLETED; the contract lives in
# tests/test_simple_close_event_169.py.


if __name__ == "__main__":
    unittest.main()
