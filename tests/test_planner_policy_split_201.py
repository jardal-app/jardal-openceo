"""Planner policy on demand: core + per-agent detail (TASK-2026-00201).

The planner prompt used to carry ``routing_policy.md`` whole (40 KB,
every cycle). Now it carries the core plus an index of staged
per-agent detail files (``policy/<agent>.md`` under the objective
scratch, the only file channel the fenced planner provably sees);
the DEV tier rules (§18-35) live in ``routing_policy.d/dev.md`` and
are read on demand. The boot check and the per-cycle ``policy_sha``
cover every policy file. No network, no CLI, no LLM.
"""

from __future__ import annotations

import json
import os
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

from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _planner_prompt,
    objective_scratch,
    plan_objective,
)
from objectives.planner_models import POLICY_PROMPT_MAX_CHARS  # noqa: E402

_LIMIT = POLICY_PROMPT_MAX_CHARS


def _tmp_policy(test: unittest.TestCase, core: str,
                details: dict[str, str] | None = None) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    root = Path(temp.name)
    path = root / "policy.md"
    path.write_text(core, encoding="utf-8")
    for name, text in (details or {}).items():
        folder = root / "policy.d"
        folder.mkdir(exist_ok=True)
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    return path


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


def _dev_ticket(local_ref: str, title: str) -> dict[str, Any]:
    return {
        "local_ref": local_ref,
        "title": title,
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": f"achieve {title}",
        "why_now": "next step",
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_c",
        "tier_reason": "covered by tests; reversible",
    }


def _env(repo: Path, payloads: list[dict[str, Any]],
         core: str, details: dict[str, str] | None = None,
         ) -> tuple[ObjectiveEnv, _RecordedPlanner]:
    policy = repo / "policy.md"
    policy.write_text(core, encoding="utf-8")
    if details:
        folder = repo / "policy.d"
        folder.mkdir(exist_ok=True)
        for name, text in details.items():
            (folder / f"{name}.md").write_text(text, encoding="utf-8")
    planner = _RecordedPlanner(repo, payloads)
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        return {"task_id": f"task-child-{counter['n']}"}

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
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True}},
        },
    )
    return env, planner


async def _objective(env: ObjectiveEnv, objective_id: str = "OBJ-1") -> None:
    await env.objectives.create_objective({
        "objective_id": objective_id, "project": "p", "status": "active",
        "anchor": {"goal": "g", "acceptance": []},
        "capsule": {"text": "", "compiled_at": None, "tokens": 0},
        "tickets_log": [],
    })


class DiscoveryTests(unittest.TestCase):
    def test_details_discovered_sorted_next_to_core(self) -> None:
        path = _tmp_policy(self, "CORE\n",
                           {"dev": "DEV\n", "ops": "OPS\n"})
        found = planner_mod.policy_detail_files(path)
        self.assertEqual([name for name, _ in found], ["dev", "ops"])
        self.assertEqual(found[0][1], path.parent / "policy.d" / "dev.md")

    def test_no_details_dir_means_core_only(self) -> None:
        path = _tmp_policy(self, "CORE\n")
        self.assertEqual(planner_mod.policy_detail_files(path), [])
        self.assertEqual(planner_mod.load_policy_details(path), [])
        self.assertEqual(planner_mod.load_policy_text(path), "CORE\n")
        self.assertEqual(planner_mod.policy_detail_index([]), "")

    def test_composed_joins_core_then_details_in_order(self) -> None:
        path = _tmp_policy(self, "CORE", {"b": "BBB", "a": "AAA"})
        self.assertEqual(planner_mod.load_policy_text(path),
                         "CORE\n\nAAA\n\nBBB\n")

    def test_empty_detail_fails_closed(self) -> None:
        path = _tmp_policy(self, "CORE\n", {"dev": "  \n"})
        self.assertIsNone(planner_mod.load_policy_details(path))
        self.assertEqual(planner_mod.load_policy_text(path), "")

    def test_unreadable_detail_fails_closed(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        path = root / "policy.md"
        path.write_text("CORE\n", encoding="utf-8")
        (root / "policy.d").mkdir()
        (root / "policy.d" / "dev.md").mkdir()
        self.assertIsNone(planner_mod.load_policy_details(path))
        self.assertEqual(planner_mod.load_policy_text(path), "")


class PromptCompositionTests(unittest.TestCase):
    def test_index_names_every_staged_file(self) -> None:
        index = planner_mod.policy_detail_index(
            [("dev", "/repo/.openceo/objectives/OBJ-1/policy/dev.md")])
        self.assertIn("AGENT POLICY DETAIL", index)
        self.assertIn("dev: /repo/.openceo/objectives/OBJ-1/policy/dev.md",
                      index)

    def test_prompt_pastes_core_and_index(self) -> None:
        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "",
            [], policy_text="CORE-UNIQUE",
            policy_index="INDEX-UNIQUE", roadmap=[])
        self.assertIn("NORMATIVE ROUTING POLICY", prompt)
        self.assertIn("CORE-UNIQUE", prompt)
        self.assertIn("INDEX-UNIQUE", prompt)

    def test_prompt_without_index_is_the_old_shape(self) -> None:
        prompt = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {"title": "t"}}, "", [], "",
            [], policy_text="CORE-UNIQUE", roadmap=[])
        self.assertIn("CORE-UNIQUE", prompt)
        self.assertNotIn("AGENT POLICY DETAIL", prompt)


class ShippedPolicyTests(unittest.TestCase):
    def test_shipped_core_excludes_dev_detail(self) -> None:
        core = planner_mod.load_policy_core()
        self.assertIn("Compact runtime kernel", core)
        self.assertIn("## 17. Deterministic validation", core)
        self.assertNotIn("## 18. DEV tier semantics", core)
        self.assertNotIn("Builder-authored tests are encouraged", core)
        self.assertLess(len(core.strip()), _LIMIT)

    def test_every_section_survives_exactly_once(self) -> None:
        composed = planner_mod.load_policy_text()
        self.assertTrue(composed)
        for num in list(range(0, 44)):
            with self.subTest(section=num):
                self.assertEqual(composed.count(f"## {num}. "), 1)
        self.assertEqual(composed.count("# DEV ROUTING"), 1)
        self.assertNotIn("# DEV ROUTING", planner_mod.load_policy_core())

    def test_check_covers_core_and_dev(self) -> None:
        status = planner_mod.check_policy()
        self.assertTrue(status.ok, status.problem)
        core = planner_mod.policy_file(None)
        self.assertEqual(status.path, str(core))
        self.assertEqual(tuple(status.files),
                         (str(core),
                          str(core.parent / "routing_policy.d" / "dev.md")))
        self.assertEqual(status.sha,
                         planner_mod.policy_fingerprint(
                             planner_mod.load_policy_text()))

    def test_shipped_detail_files_exist_and_are_readable(self) -> None:
        found = planner_mod.policy_detail_files()
        self.assertEqual([name for name, _ in found], ["dev"])
        for _name, path in found:
            with self.subTest(path=str(path)):
                self.assertTrue(path.is_file())
                self.assertTrue(os.access(path, os.R_OK))


class CheckPolicyDetailTests(unittest.TestCase):
    def test_broken_detail_is_a_problem_naming_it(self) -> None:
        path = _tmp_policy(self, "CORE\n", {"dev": "  \n"})
        status = planner_mod.check_policy(path)
        self.assertFalse(status.ok)
        self.assertIn("dev", status.problem)
        self.assertEqual(status.chars, len("CORE\n".strip()))
        self.assertEqual(status.sha, "")

    def test_over_budget_core_still_names_the_cost(self) -> None:
        path = _tmp_policy(self, "p" * (_LIMIT + 1), {"dev": "d\n"})
        status = planner_mod.check_policy(path)
        self.assertFalse(status.ok)
        self.assertIn("exceed the prompt budget", status.problem)
        self.assertIn("compact runtime kernel", status.problem)

    def test_healthy_tmp_lists_covered_files(self) -> None:
        path = _tmp_policy(self, "CORE\n", {"dev": "DEV\n"})
        status = planner_mod.check_policy(path)
        self.assertTrue(status.ok, status.problem)
        self.assertEqual(
            tuple(status.files),
            (str(path), str(path.parent / "policy.d" / "dev.md")))
        self.assertEqual(status.sha,
                         planner_mod.policy_fingerprint("CORE\n\nDEV\n"))


class CycleStagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_stages_detail_and_indexes_it(self) -> None:
        repo = _scratch_repo(self)
        payload = {"objective_status": "active", "state_summary": "ok",
                   "tickets": [_dev_ticket("a", "Child")],
                   "roadmap_remaining": []}
        env, planner = _env(repo, [payload], "POLICY CORE\n",
                            {"dev": "DEV DETAIL UNIQUE\n"})
        await _objective(env)
        result = await plan_objective(env, "OBJ-1")
        self.assertTrue(result.get("ok"), result)
        prompt = planner.prompts[0]
        staged = objective_scratch(repo, "OBJ-1") / "policy" / "dev.md"
        self.assertIn(str(staged), prompt)
        self.assertIn("POLICY CORE", prompt)
        self.assertNotIn("DEV DETAIL UNIQUE", prompt)
        self.assertEqual(staged.read_text(encoding="utf-8"),
                         "DEV DETAIL UNIQUE\n")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        run = stored["planning_runs"][-1]
        self.assertEqual(
            run["policy_sha"],
            planner_mod.policy_fingerprint(
                "POLICY CORE\n\nDEV DETAIL UNIQUE\n"))
        self.assertNotEqual(run["policy_sha"],
                            planner_mod.policy_fingerprint("POLICY CORE\n"))

    async def test_cycle_without_details_stages_nothing(self) -> None:
        repo = _scratch_repo(self)
        payload = {"objective_status": "active", "state_summary": "ok",
                   "tickets": [_dev_ticket("a", "Child")],
                   "roadmap_remaining": []}
        env, planner = _env(repo, [payload], "POLICY CORE\n")
        await _objective(env)
        result = await plan_objective(env, "OBJ-1")
        self.assertTrue(result.get("ok"), result)
        self.assertNotIn("AGENT POLICY DETAIL", planner.prompts[0])
        self.assertFalse(
            (objective_scratch(repo, "OBJ-1") / "policy").exists())

    async def test_deleted_detail_purges_the_staged_copy(self) -> None:
        repo = _scratch_repo(self)
        first_payload = {"objective_status": "active", "state_summary": "ok",
                         "tickets": [_dev_ticket("a", "Child")],
                         "roadmap_remaining": []}
        second_payload = {"objective_status": "active", "state_summary": "ok",
                          "tickets": [_dev_ticket("b", "Second")],
                          "roadmap_remaining": []}
        env, planner = _env(repo, [first_payload, second_payload],
                            "POLICY CORE\n", {"dev": "DEV v1\n"})
        await _objective(env)
        first = await plan_objective(env, "OBJ-1")
        self.assertTrue(first.get("ok"), first)
        staged = objective_scratch(repo, "OBJ-1") / "policy" / "dev.md"
        self.assertTrue(staged.is_file())
        (repo / "policy.d" / "dev.md").unlink()
        second = await plan_objective(env, "OBJ-1")
        self.assertTrue(second.get("ok"), second)
        self.assertFalse(staged.exists())
        self.assertNotIn("AGENT POLICY DETAIL", planner.prompts[1])

    async def test_broken_detail_parks_naming_it(self) -> None:
        repo = _scratch_repo(self)
        payload = {"objective_status": "active", "state_summary": "ok",
                   "tickets": [_dev_ticket("a", "Child")],
                   "roadmap_remaining": []}
        env, planner = _env(repo, [payload], "POLICY CORE\n",
                            {"dev": "  \n"})
        await _objective(env)
        result = await plan_objective(env, "OBJ-1")
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("reason"), "config")
        self.assertIn("dev", str(result.get("detail") or ""))
        self.assertEqual(planner.prompts, [])

    async def test_detail_change_moves_the_sha(self) -> None:
        repo = _scratch_repo(self)
        first_payload = {"objective_status": "active", "state_summary": "ok",
                         "tickets": [_dev_ticket("a", "Child")],
                         "roadmap_remaining": []}
        second_payload = {"objective_status": "active", "state_summary": "ok",
                          "tickets": [_dev_ticket("b", "Second")],
                          "roadmap_remaining": []}
        env, _planner = _env(repo, [first_payload, second_payload],
                             "POLICY CORE\n", {"dev": "DEV v1\n"})
        await _objective(env)
        first = await plan_objective(env, "OBJ-1")
        self.assertTrue(first.get("ok"), first)
        (repo / "policy.d" / "dev.md").write_text("DEV v2\n",
                                                 encoding="utf-8")
        (repo / ".openceo" / "objectives" / "OBJ-1"
         / "PLANNER_RESULT.json").unlink(missing_ok=True)
        second = await plan_objective(env, "OBJ-1")
        self.assertTrue(second.get("ok"), second)
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        runs = stored["planning_runs"]
        self.assertEqual(len(runs), 2)
        self.assertNotEqual(runs[0]["policy_sha"], runs[1]["policy_sha"])
        self.assertEqual(
            runs[1]["policy_sha"],
            planner_mod.policy_fingerprint("POLICY CORE\n\nDEV v2\n"))


if __name__ == "__main__":
    unittest.main()
