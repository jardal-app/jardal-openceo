from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.consensus import CriticalConsensusOrchestrator, ConsensusParticipant
from agents.dev.session import DevSessionManager
from agents.dev.tier_s_contract import (
    child_env, detect_claude_question, git_snapshot, parse_plan_observations,
    parse_plan_responses, plan_response_gaps, review_quorum,
    valid_plan_review, verify_plan_anchor,
)
from settings import Settings
from agents.dev.tmux_runtime import TmuxRuntime, TmuxTurnResult, _DEFAULT_PROMPT_PATTERNS
from state import DevProvider

import yaml


class TierSContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.orch = CriticalConsensusOrchestrator(MagicMock(), MagicMock())
        command = [sys.executable]
        self.config = {
            "project": {"repos": [{"local_path": str(self.repo)}]},
            "dev": {"tier_s": {
                "roles": {
                    "planner": {"active": True, "command": command},
                    "plan-reviewer": {"active": True, "command": command},
                    "builder": {"active": True, "command": command},
                },
                "reviewers": [
                    {"label": "qwen", "provider": "qwen", "model": "qwen3.8-max", "family": "qwen", "active": True, "command": command},
                    {"label": "claudeg", "provider": "claude", "model": "glm-5.3", "family": "glm", "active": True, "command": command},
                    {"label": "dsh", "provider": "dsh", "model": "deepseek-v4.1-flash", "family": "deepseek", "active": True, "command": command},
                    {"label": "agyd", "provider": "gemini", "model": "gemini", "family": "gemini", "active": False, "command": command},
                ],
            }},
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_inactive_planner_waits_without_starting_sessions(self) -> None:
        self.config["dev"]["tier_s"]["roles"]["planner"]["active"] = False
        self.orch.ensure_project_sessions = AsyncMock()
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.waiting)
        self.assertIn("claudej", result.summary)
        self.orch.ensure_project_sessions.assert_not_called()

    async def test_git_snapshot_includes_new_files_and_deletions(self) -> None:
        before = git_snapshot(self.repo)
        (self.repo / "tracked.txt").unlink()
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        after = git_snapshot(self.repo)
        self.assertEqual(after.changed_since(before), ["new.txt", "tracked.txt"])
        self.assertIsNone(after.files["tracked.txt"])

    async def test_isolated_codex_review_sees_snapshot_without_writing_source(self) -> None:
        (self.repo / "tracked.txt").write_text("working tree\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        with self.orch._isolated_review_worktree(self.repo) as isolated:
            self.assertEqual((isolated / "tracked.txt").read_text(), "working tree\n")
            self.assertEqual((isolated / "new.txt").read_text(), "new\n")
            (isolated / "tracked.txt").write_text("reviewer edit\n", encoding="utf-8")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "working tree\n")

    async def test_quorum_requires_two_valid_reports_from_distinct_families(self) -> None:
        families = {"qwen": "qwen", "claudeg": "glm", "other": "qwen"}
        # Same family twice is not a quorum, even with two valid reports.
        self.assertFalse(review_quorum({"qwen": "No findings.", "other": "No findings."}, families))
        # An invalid report never counts.
        self.assertFalse(review_quorum({"qwen": "", "claudeg": "No findings."}, families))
        # Two valid reports from two families form a quorum.
        self.assertTrue(review_quorum({"qwen": "No findings.", "claudeg": "[WARN] a.py:1 — issue → fix"}, families))
        # Unknown labels never count (fail-closed).
        self.assertFalse(review_quorum({"qwen": "No findings.", "ghost": "No findings."}, families))

    async def test_native_question_only_while_selection_is_visible(self) -> None:
        pane = ("Which color do you choose: red or blue?\n"
                "❯ 1. Red\n  2. Blue\n  3. Type something.\n"
                "Enter to select · ↑/↓ to navigate · Esc to cancel\n")
        question = detect_claude_question(pane)
        self.assertEqual(question["options"], ["Red", "Blue"])
        self.assertIsNone(detect_claude_question(pane + "\nSelected Blue.\n"))

    async def test_spanish_question_preserves_panel_option_indices(self) -> None:
        pane = ("¿Prefieres la primera o la segunda?\n"
                "❯ 1. Type something.\n  2. Primera\n  3. Segunda\n"
                "Enter to select · ↑/↓ to navigate\n")
        question = detect_claude_question(pane)
        self.assertEqual(question["options"], ["Primera", "Segunda"])
        self.assertEqual(question["option_indices"], [1, 2])

    async def test_answer_uses_panel_index_after_freeform_entry(self) -> None:
        pane_text = ("¿Prefieres la primera o la segunda?\n"
                     "❯ 1. Type something.\n  2. Primera\n  3. Segunda\n"
                     "Enter to select · ↑/↓ to navigate\n")
        settings = MagicMock(TMUX_POLL_INTERVAL_MS=1)
        runtime = TmuxRuntime(settings)
        runtime._sessions["builder"] = SimpleNamespace(
            tmux_session_name="builder", provider=DevProvider.CLAUDE)
        pane = MagicMock()
        runtime._server = MagicMock()
        runtime._server.sessions.get.return_value.active_window.active_pane = pane
        runtime._capture_pane = MagicMock(return_value=pane_text)
        result = await runtime.answer_question("builder", "Primera", timeout=1,
                                               completion_check=lambda: True)
        self.assertTrue(result.ok)
        pane.send_keys.assert_called_once_with("Down", enter=False)

    async def test_second_subplan_reviews_only_its_delta(self) -> None:
        planner = ConsensusParticipant("planner", DevProvider.CLAUDE, "planner", "planner")
        builder = ConsensusParticipant("builder", DevProvider.CLAUDE, "builder", "builder")
        self.orch.ensure_project_sessions = AsyncMock(return_value=(planner, [builder]))
        self.orch._ensure_participant_session = AsyncMock()
        self.orch._readonly_review = AsyncMock(return_value="No findings.")
        scopes = []

        async def review(*args, **kwargs):
            scopes.append((args[1], list(args[3])))
            return ({"qwen": "No findings.", "claudeg": "No findings."}, {})

        async def plan(*args, **kwargs):
            paths = args[2]
            paths.plan_path.write_text("TOTAL_PLANS: 2\n", encoding="utf-8")
            paths.status_path.write_text("PLAN_READY_2", encoding="utf-8")
            return 2, TmuxTurnResult(True, "PLAN_READY_2", True)

        async def build(*args, **kwargs):
            p, paths = args[1:3]
            (self.repo / ("tracked.txt" if p == 1 else "second.txt")).write_text(
                f"subplan {p}\n", encoding="utf-8")
            paths.build_summary(p).write_text("summary", encoding="utf-8")
            paths.status_path.write_text(f"BUILD_DONE_{p}", encoding="utf-8")
            return TmuxTurnResult(True, f"BUILD_DONE_{p}", True)

        self.orch._run_planner = plan
        self.orch._run_builder = build
        self.orch._review_snapshot = review
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(scopes, [(1, ["tracked.txt"]), (2, ["second.txt"]),
                                   (0, ["second.txt", "tracked.txt"])])

    async def test_completed_fix_signal_resumes_without_rerunning_builder(self) -> None:
        (self.repo / "tracked.txt").write_text("draft\n", encoding="utf-8")
        before = git_snapshot(self.repo)
        (self.repo / "tracked.txt").write_text("fixed\n", encoding="utf-8")
        paths = self.orch._tier_s_paths(self.repo)
        paths.dir.mkdir()
        paths.plan_path.write_text("TOTAL_PLANS: 1\n", encoding="utf-8")
        paths.status_path.write_text("FIX_DONE_1_1", encoding="utf-8")
        roster = self.orch._frozen_roster(self.orch._normalized_config(self.config))
        run = {"run_id": "resume-fix", "repo_path": str(self.repo), "status": "waiting",
               "phase": "fix", "subplan": 1, "round": 1, "total_plans": 1,
               "fix_baseline": before.files, "initial_snapshot": before.files,
               "roster": roster, "reviews_received": 3}
        self.orch._run_cache[run["run_id"]] = run
        self.orch._open_run = AsyncMock(return_value=run)
        planner = ConsensusParticipant("planner", DevProvider.CLAUDE, "planner", "planner")
        builder = ConsensusParticipant("builder", DevProvider.CLAUDE, "builder", "builder")
        self.orch.ensure_project_sessions = AsyncMock(return_value=(planner, [builder]))
        self.orch._ensure_participant_session = AsyncMock()
        self.orch._run_builder_fix = AsyncMock()
        self.orch._readonly_review = AsyncMock(return_value="No findings.")
        self.orch._review_snapshot = AsyncMock(return_value=(
            {"qwen": "No findings.", "claudeg": "No findings."}, {}))
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.ok, result.summary)
        self.orch._run_builder_fix.assert_not_called()

    async def test_consumed_question_is_cleared_even_when_planner_stops(self) -> None:
        question = {"text": "Which?", "options": ["Red", "Blue"],
                    "option_indices": [0, 1], "selected_index": 0, "input_index": None}
        run = {"run_id": "resume-question", "repo_path": str(self.repo),
               "phase": "planner", "status": "waiting", "question": question,
               "human_answer": "Blue", "roster": self.orch._frozen_roster(
                   self.orch._normalized_config(self.config))}
        self.orch._run_cache[run["run_id"]] = run
        self.orch._open_run = AsyncMock(return_value=run)
        planner = ConsensusParticipant("planner", DevProvider.CLAUDE, "planner", "planner")
        builder = ConsensusParticipant("builder", DevProvider.CLAUDE, "builder", "builder")
        self.orch.ensure_project_sessions = AsyncMock(return_value=(planner, [builder]))
        self.orch._tmux.answer_question = AsyncMock(return_value=TmuxTurnResult(False, "", False))
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.waiting)
        self.assertIsNone(run["question"])
        self.assertIsNone(run["human_answer"])
        self.orch._run_planner = AsyncMock(return_value=(None, TmuxTurnResult(False, "", False)))
        await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.orch._tmux.answer_question.assert_awaited_once()

    async def test_erpnext_projection_keeps_latest_phase_without_blocking_checkpoint(self) -> None:
        repo = MagicMock()
        repo.patch_tier_s_run = AsyncMock()
        repo.patch_task = AsyncMock(return_value={
            "params": {"_mirrors": {"erpnext": {"name": "TASK-1"}}}})
        adapter = MagicMock()
        release = asyncio.Event()
        phases = []

        async def project(_name, summary):
            phases.append(summary["phase"])
            if len(phases) == 1:
                await release.wait()

        adapter.update_tier_s_progress = AsyncMock(side_effect=project)
        orch = CriticalConsensusOrchestrator(MagicMock(), MagicMock(), repo=repo)
        orch._erpnext = adapter
        orch._run_cache["run-1"] = {"run_id": "run-1", "phase": "planner",
                                    "status": "active", "repo_path": str(self.repo)}
        await orch._checkpoint("run-1", "task-1", None, status="waiting")
        await asyncio.sleep(0)
        await orch._checkpoint("run-1", "task-1", None, phase="build")
        await orch._checkpoint("run-1", "task-1", None, phase="review")
        self.assertEqual(phases, ["planner"])
        release.set()
        await orch._projection_tasks["task-1"]
        self.assertEqual(phases, ["planner", "review"])

    async def test_small_ticket_completes_only_after_all_reviews(self) -> None:
        planner = ConsensusParticipant("planner", DevProvider.CLAUDE, "planner", "planner")
        builder = ConsensusParticipant("builder", DevProvider.CLAUDE, "builder", "builder")
        self.orch.ensure_project_sessions = AsyncMock(return_value=(planner, [builder]))
        self.orch._ensure_participant_session = AsyncMock()
        self.orch._readonly_review = AsyncMock(return_value="No findings.")

        async def plan(*args, **kwargs):
            paths = args[2]
            paths.plan_path.write_text("TOTAL_PLANS: 1\n\n## Sub-Plan 1\nImplement.\n", encoding="utf-8")
            paths.status_path.write_text("PLAN_READY_1", encoding="utf-8")
            return 1, TmuxTurnResult(True, "PLAN_READY_1", True)

        async def build(*args, **kwargs):
            paths = args[2]
            (self.repo / "tracked.txt").write_text("after\n", encoding="utf-8")
            paths.build_summary(1).write_text("Changed tracked.txt", encoding="utf-8")
            paths.status_path.write_text("BUILD_DONE_1", encoding="utf-8")
            return TmuxTurnResult(True, "BUILD_DONE_1", True)

        self.orch._run_planner = plan
        self.orch._run_builder = build
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(result.reviews_received, 6)
        archived = self.repo / ".tier-s" / "runs" / result.run_id
        self.assertEqual((archived / "status").read_text(), "TICKET_COMPLETE")
        self.assertFalse((archived / "REVIEW-codexj-1-1.md").exists())
        self.assertTrue((archived / "REVIEW-qwen-1-1.md").exists())
        self.assertTrue((archived / "REVIEW-claudeg-1-1.md").exists())
        self.assertTrue((archived / "REVIEW-dsh-1-1.md").exists())
        self.assertTrue((archived / "REVIEW-qwen-0-1.md").exists())
        self.assertTrue((archived / "REVIEW-claudeg-0-1.md").exists())
        self.assertTrue((archived / "REVIEW-dsh-0-1.md").exists())

    async def test_roster_defaults_muse_builder_and_codexj_reviewer(self) -> None:
        cfg = self.orch._normalized_config({"dev": {"tier_s": {"roles": {
            "planner": {"command": ["x"]},
            "plan-reviewer": {"command": ["x"]},
            "builder": {"command": ["x"]},
        }}}})
        builder = cfg["roles"]["builder"]
        self.assertEqual(builder["provider"], "muse")
        self.assertEqual(builder["account"], "muse")
        self.assertEqual(builder["label"], "muse")
        self.assertEqual(builder["model"], "muse-spark-1.3-contributor")
        self.assertEqual(cfg["roles"]["plan-reviewer"]["provider"], "codex")
        self.assertEqual(cfg["roles"]["plan-reviewer"]["account"], "codexj")
        self.assertEqual(cfg["roles"]["plan-reviewer"]["label"], "codexj")
        self.assertEqual(cfg["roles"]["plan-reviewer"]["model"], "gpt-6.1-sol")
        self.assertEqual(cfg["roles"]["planner"]["account"], "claudej")
        self.assertEqual(cfg["min_available_families"], 3)
        self.assertEqual(DevProvider("muse"), DevProvider.MUSE)
        self.assertIn("muse", _DEFAULT_PROMPT_PATTERNS)
        # Quorum is two pool families; unknown labels never count.
        self.assertTrue(review_quorum(
            {"qwen": "No findings.", "claudeg": "No findings."},
            {"qwen": "qwen", "claudeg": "glm"}))
        self.assertFalse(review_quorum(
            {"qwen": "No findings.", "ghost": "No findings."},
            {"qwen": "qwen"}))
        # Extra self-review slots never count, even when valid.
        self.assertFalse(review_quorum(
            {"muse-review": "No findings.", "qwen": "No findings."},
            {"muse-review": "muse", "qwen": "qwen"},
            {"muse-review"}))
        self.assertTrue(review_quorum(
            {"muse-review": "No findings.", "qwen": "No findings.",
             "claudeg": "No findings."},
            {"muse-review": "muse", "qwen": "qwen", "claudeg": "glm"},
            {"muse-review"}))

    async def test_global_tier_s_config_pins_roster_and_projects_stay_clean(self) -> None:
        from config_loader import ConfigLoader
        from settings import Settings
        repo = Path(__file__).resolve().parents[1]
        loader = ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(repo / "projects")))
        # Tier S roles and pool are global: identical for every project.
        for name in ("jardal-openceo", "jardal-trading"):
            roles = loader.load_project(name)["dev"]["tier_s"]["roles"]
            builder = roles["builder"]
            self.assertEqual(builder["provider"], "muse", name)
            command = " ".join(str(part) for part in builder["command"])
            self.assertIn("--model muse-spark-1.3-contributor", command, name)
            self.assertIn("--reasoning-effort max", command, name)
            self.assertIn("--yolo", command, name)
            reviewer = roles["plan-reviewer"]
            self.assertTrue(
                any("codexj" in str(part) for part in reviewer["command"]),
                f"{name}: plan-reviewer must run codexj",
            )
        tier_s = loader.load_project("jardal-openceo")["dev"]["tier_s"]
        self.assertEqual(tier_s["min_available_families"], 3)
        self.assertEqual(tier_s["roles"]["planner"]["account"], "claudej")
        self.assertEqual(tier_s["roles"]["plan-reviewer"]["account"], "codexj")
        self.assertEqual(tier_s["roles"]["builder"]["account"], "muse")
        reviewers = tier_s["reviewers"]
        by_label = {item["label"]: item for item in reviewers}
        # Pool §7.1: qwen + glm + deepseek (dsh) active, claudep reserve,
        # muse-review extra.
        for label in ("qwen-max", "qwen-glm", "dsh"):
            self.assertTrue(by_label[label]["active"], label)
        self.assertEqual(by_label["dsh"]["family"], "deepseek")
        self.assertFalse(by_label["claudep"]["active"])
        self.assertEqual(by_label["claudep"]["family"], "deepseek")
        self.assertTrue(by_label["muse-review"]["extra"])
        active_families = {item["family"] for item in reviewers
                           if item["active"] and not item.get("extra")}
        self.assertGreaterEqual(len(active_families), 3)
        tiers = loader.load_project("jardal-openceo")["dev"]["tiers"]
        self.assertEqual(tiers["tier_a"]["account"], "claudej")
        self.assertEqual(tiers["tier_b"]["account"], "codexj")
        self.assertEqual(tiers["tier_c"]["chain"], ["muse", "dsh", "claudep"])
        # No checked-in project may carry routing or Tier S overrides.
        for name in loader.available_projects():
            with (repo / "projects" / name / "project.yaml").open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            dev = (data.get("dev") or {}) if isinstance(data, dict) else {}
            self.assertNotIn("coding_tool", dev, name)
            self.assertTrue(not dev.get("tier_s"), name)
            self.assertEqual(loader.validate_project(name), [], name)


class PlanReviewContractTests(unittest.TestCase):
    def test_observations_parse_with_anchors(self) -> None:
        report = (
            "O1 [CRITICAL] CURRENT_PLAN.md:2 — missing step → add it\n"
            "> TOTAL_PLANS: 1\n"
            "O2 [WARN] src/a.py:42 — risky call → guard it\n"
            "REVIEW_DONE_plan_0\n"
        )
        obs = parse_plan_observations(report)
        self.assertEqual([o.id for o in obs], [1, 2])
        self.assertEqual(obs[0].severity, "CRITICAL")
        self.assertEqual((obs[0].path, obs[0].line), ("CURRENT_PLAN.md", 2))
        self.assertEqual((obs[1].path, obs[1].line), ("src/a.py", 42))
        self.assertTrue(valid_plan_review(report))

    def test_no_findings_is_valid_and_empty(self) -> None:
        self.assertTrue(valid_plan_review("No findings.\nREVIEW_DONE_plan_0\n"))
        self.assertEqual(parse_plan_observations("No findings.\n"), [])

    def test_garbage_is_invalid(self) -> None:
        self.assertFalse(valid_plan_review(""))
        self.assertFalse(valid_plan_review("looks fine to me\n"))
        self.assertFalse(valid_plan_review("[WARN] a.py:1 — no number → fix\n"))

    def test_responses_parse_verdicts(self) -> None:
        responses = parse_plan_responses(
            "O1: ACEPTADA — added the step\n"
            "O2: rechazada — wrong, tests cover it\n"
            "O3: DIFERIDA — out of scope\n"
        )
        self.assertEqual(responses, {1: ["ACEPTADA"], 2: ["RECHAZADA"], 3: ["DIFERIDA"]})

    def test_gaps_find_missing_and_duplicated(self) -> None:
        obs = parse_plan_observations(
            "O1 [WARN] CURRENT_PLAN.md:1 — a → b\n"
            "O2 [WARN] CURRENT_PLAN.md:2 — c → d\n"
            "O3 [WARN] CURRENT_PLAN.md:3 — e → f\n"
        )
        missing, duplicated = plan_response_gaps(
            obs, {1: ["ACEPTADA"], 2: ["RECHAZADA"], 3: ["ACEPTADA", "RECHAZADA"]})
        self.assertEqual((missing, duplicated), ([], [3]))
        missing, duplicated = plan_response_gaps(obs, {2: ["ACEPTADA"]})
        self.assertEqual((missing, duplicated), ([1, 3], []))
        # Answers to unknown ids are ignored.
        missing, duplicated = plan_response_gaps(
            obs, {1: ["ACEPTADA"], 2: ["ACEPTADA"], 3: ["ACEPTADA"], 9: ["ACEPTADA"]})
        self.assertEqual((missing, duplicated), ([], []))

    def test_anchor_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "src").mkdir()
            (repo / "src" / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
            plan = repo / "CURRENT_PLAN.md"
            plan.write_text("TOTAL_PLANS: 1\n\n## Sub-Plan 1\n", encoding="utf-8")

            def obs_at(path: str, line: int | None):
                anchor = path if line is None else f"{path}:{line}"
                found = parse_plan_observations(
                    f"O1 [WARN] {anchor} — problem → fix\n")
                assert len(found) == 1
                return found[0]

            self.assertTrue(verify_plan_anchor(obs_at("CURRENT_PLAN.md", 2), plan, repo))
            self.assertTrue(verify_plan_anchor(obs_at("src/a.py", 2), plan, repo))
            self.assertFalse(verify_plan_anchor(obs_at("src/a.py", 99), plan, repo))
            self.assertFalse(verify_plan_anchor(obs_at("src/missing.py", 1), plan, repo))
            self.assertFalse(verify_plan_anchor(obs_at("../escape.py", 1), plan, repo))


class NonBlockingPlanReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        (self.repo / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.orch = CriticalConsensusOrchestrator(MagicMock(), MagicMock())
        command = [sys.executable]
        self.config = {
            "project": {"repos": [{"local_path": str(self.repo)}]},
            "dev": {"tier_s": {
                "roles": {
                    "planner": {"active": True, "command": command},
                    "plan-reviewer": {"active": True, "command": command},
                    "builder": {"active": True, "command": command},
                },
                "reviewers": [
                    {"label": "qwen", "provider": "qwen", "model": "m", "family": "qwen", "active": True, "command": command},
                    {"label": "glm", "provider": "qwen", "model": "m", "family": "glm", "active": True, "command": command},
                    {"label": "dsh", "provider": "dsh", "model": "m", "family": "deepseek", "active": True, "command": command},
                ],
            }},
        }
        planner = ConsensusParticipant("planner", DevProvider.CLAUDE, "planner", "planner")
        builder = ConsensusParticipant("builder", DevProvider.CLAUDE, "builder", "builder")
        self.orch.ensure_project_sessions = AsyncMock(return_value=(planner, [builder]))
        self.orch._ensure_participant_session = AsyncMock()
        self.checkpoints: list[dict] = []
        orig_checkpoint = self.orch._checkpoint

        async def spy(run_id, task_id, session_id, **updates):
            self.checkpoints.append(updates)
            return await orig_checkpoint(run_id, task_id, session_id, **updates)

        self.orch._checkpoint = spy  # type: ignore[method-assign]

        async def plan(*args, **kwargs):
            paths = args[2]
            paths.plan_path.write_text(
                "TOTAL_PLANS: 1\n\n## Sub-Plan 1\nImplement.\n", encoding="utf-8")
            paths.status_path.write_text("PLAN_READY_1", encoding="utf-8")
            return 1, TmuxTurnResult(True, "PLAN_READY_1", True)

        async def build(*args, **kwargs):
            paths = args[2]
            (self.repo / "tracked.txt").write_text("after\n", encoding="utf-8")
            paths.build_summary(1).write_text("Changed tracked.txt", encoding="utf-8")
            paths.status_path.write_text("BUILD_DONE_1", encoding="utf-8")
            return TmuxTurnResult(True, "BUILD_DONE_1", True)

        async def review(*args, **kwargs):
            return ({"qwen": "No findings.", "glm": "No findings.",
                     "dsh": "No findings."}, {})

        self.orch._run_planner = plan
        self.orch._run_builder = build
        self.orch._review_snapshot = review

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _plan_stats(self) -> dict:
        for updates in self.checkpoints:
            if "plan_review" in updates:
                return updates["plan_review"]
        raise AssertionError("no plan_review checkpoint")

    async def test_critical_observation_does_not_veto(self) -> None:
        self.orch._readonly_review = AsyncMock(return_value=(
            "O1 [CRITICAL] CURRENT_PLAN.md:3 — missing test step → add it\n"
            "> ## Sub-Plan 1\n"
            "O2 [WARN] src/nowhere.py:1 — ghost → ignore\n"
            "REVIEW_DONE_plan_0\n"
        ))

        async def respond(*args, **kwargs):
            paths = self.orch._tier_s_paths(self.repo)
            paths.dir.joinpath("PLAN_RESPONSE.md").write_text(
                "O1: RECHAZADA — tests run in the build step\n", encoding="utf-8")
            paths.status_path.write_text("PLAN_RESPONDED", encoding="utf-8")
            return TmuxTurnResult(True, "PLAN_RESPONDED", True)

        self.orch._send_participant_instruction = respond  # type: ignore[method-assign]
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.ok, result.summary)
        # The ghost-anchored O2 was discarded mechanically; O1 rejected.
        self.assertEqual(self._plan_stats(),
                         {"observations": 1, "accepted": 0, "rejected": 1,
                          "deferred": 0, "no_response": []})

    async def test_incomplete_response_continues_with_sin_respuesta(self) -> None:
        self.orch._readonly_review = AsyncMock(return_value=(
            "O1 [WARN] CURRENT_PLAN.md:1 — vague → specify\n"
            "O2 [WARN] CURRENT_PLAN.md:3 — risky → split\n"
        ))
        prompts: list[str] = []

        async def respond(*args, **kwargs):
            prompts.append(args[3])
            paths = self.orch._tier_s_paths(self.repo)
            paths.dir.joinpath("PLAN_RESPONSE.md").write_text(
                "O1: ACEPTADA — specified\n", encoding="utf-8")
            paths.status_path.write_text("PLAN_RESPONDED", encoding="utf-8")
            return TmuxTurnResult(True, "PLAN_RESPONDED", True)

        self.orch._send_participant_instruction = respond  # type: ignore[method-assign]
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.ok, result.summary)
        stats = self._plan_stats()
        self.assertEqual(stats["observations"], 2)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["no_response"], [2])
        # Asked once, then retried once with the gap note.
        self.assertEqual(len(prompts), 2)
        self.assertIn("O2", prompts[1])


_ENV_PROBE = (
    "import os; print('LEAK:' + os.environ['DEEPSEEK_API_KEY']) "
    "if os.environ.get('DEEPSEEK_API_KEY') else print('CLEAN')"
)


class ChildEnvTests(unittest.TestCase):
    def test_drops_blocked_credential(self) -> None:
        merged = child_env({"DEEPSEEK_API_KEY": "sk-service", "PATH": "/bin"}, {"FOO": "1"})
        self.assertNotIn("DEEPSEEK_API_KEY", merged)
        self.assertEqual(merged["PATH"], "/bin")
        self.assertEqual(merged["FOO"], "1")

    def test_drops_blocked_credential_without_overlay(self) -> None:
        merged = child_env({"DEEPSEEK_API_KEY": "sk-service", "PATH": "/bin"}, None)
        self.assertNotIn("DEEPSEEK_API_KEY", merged)
        self.assertEqual(merged["PATH"], "/bin")

    def test_explicit_overlay_wins(self) -> None:
        merged = child_env({"DEEPSEEK_API_KEY": "sk-service"}, {"DEEPSEEK_API_KEY": "sk-explicit"})
        self.assertEqual(merged["DEEPSEEK_API_KEY"], "sk-explicit")


class ChildEnvLeakTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_review_process_does_not_leak_service_key(self) -> None:
        orch = CriticalConsensusOrchestrator(MagicMock(), MagicMock())
        account = {"label": "dsh", "provider": "dsh"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-service-fake"}):
            report = await orch._run_review_process(
                account, [sys.executable, "-c", _ENV_PROBE], "", self.cwd, 60,
                env={"DSH_HOME": "/home/develop/.dsh"}, origin_repo=self.cwd)
        self.assertEqual(report, "CLEAN")

    async def test_review_process_honors_explicit_overlay_key(self) -> None:
        orch = CriticalConsensusOrchestrator(MagicMock(), MagicMock())
        account = {"label": "dsh", "provider": "dsh"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-service-fake"}):
            report = await orch._run_review_process(
                account, [sys.executable, "-c", _ENV_PROBE], "", self.cwd, 60,
                env={"DEEPSEEK_API_KEY": "sk-explicit-fake"}, origin_repo=self.cwd)
        self.assertEqual(report, "LEAK:sk-explicit-fake")

    async def test_tier_executor_does_not_leak_service_key(self) -> None:
        mgr = DevSessionManager(Settings(), MagicMock())
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-service-fake"}):
            result = await mgr._run_process_detailed(
                [sys.executable, "-c", _ENV_PROBE], cwd=str(self.cwd),
                env={"DSH_HOME": "/home/develop/.dsh"})
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout, "CLEAN")


if __name__ == "__main__":
    unittest.main()
