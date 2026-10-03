from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.consensus import CriticalConsensusOrchestrator, ConsensusParticipant
from agents.dev.tier_s_contract import detect_claude_question, git_snapshot, review_quorum
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
                "review_rounds": 1, "max_plans": 2,
                "roles": {
                    "planner": {"active": True, "command": command},
                    "plan-reviewer": {"active": True, "command": command},
                    "builder": {"active": True, "command": command},
                },
                "reviewers": [
                    {"label": "qwen", "provider": "qwen", "model": "qwen3.8-max", "active": True, "command": command},
                    {"label": "claudeg", "provider": "claude", "model": "glm-5.3", "active": True, "command": command},
                    {"label": "agyd", "provider": "gemini", "model": "gemini", "active": False, "command": command},
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

    async def test_legacy_opencode_reviewers_cannot_silently_enter_roster(self) -> None:
        self.config["dev"]["tier_s"]["opencode_reviewers"] = [
            {"label": "mimo", "model": "mimo-v2.6-pro"}]
        self.orch.ensure_project_sessions = AsyncMock()
        result = await self.orch.execute("test", "ticket", self.config, AsyncMock())
        self.assertTrue(result.waiting)
        self.assertIn("opencode_reviewers", result.summary)
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

    async def test_quorum_requires_codex_and_distinct_valid_model(self) -> None:
        models = {"codexj": "gpt-6-sol", "qwen": "qwen3.8-max", "other": "gpt-6-sol"}
        self.assertFalse(review_quorum({"codexj": "No findings.", "other": "No findings."}, models))
        self.assertFalse(review_quorum({"codexj": "", "qwen": "No findings."}, models))
        self.assertTrue(review_quorum({"codexj": "No findings.", "qwen": "[WARN] a.py:1 — issue → fix"}, models))
        self.assertTrue(review_quorum({"codexr": "No findings.", "qwen": "No findings."},
                                      {"codexr": "gpt", "qwen": "qwen"}, "codexr"))

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

        async def review(*args):
            scopes.append((args[2], list(args[4])))
            return ({"codexj": "No findings.", "qwen": "No findings.",
                     "claudeg": "No findings."}, {})

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
        self.assertEqual(scopes, [(1, ["tracked.txt"]), (2, ["second.txt"])])

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
            {"codexj": "No findings.", "qwen": "No findings.", "claudeg": "No findings."}, {}))
        self.config["dev"]["tier_s"]["review_rounds"] = 2
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
        self.assertEqual(result.reviews_received, 3)
        archived = self.repo / ".tier-s" / "runs" / result.run_id
        self.assertEqual((archived / "status").read_text(), "TICKET_COMPLETE")
        self.assertTrue((archived / "REVIEW-codexj-1-1.md").exists())
        self.assertTrue((archived / "REVIEW-qwen-1-1.md").exists())
        self.assertTrue((archived / "REVIEW-claudeg-1-1.md").exists())

    async def test_roster_defaults_muse_builder_and_codexj_reviewer(self) -> None:
        cfg = self.orch._normalized_config({"dev": {"tier_s": {"roles": {
            "planner": {"command": ["x"]},
            "plan-reviewer": {"command": ["x"]},
            "builder": {"command": ["x"]},
        }}}})
        builder = cfg["roles"]["builder"]
        self.assertEqual(builder["provider"], "muse")
        self.assertEqual(builder["label"], "muse")
        self.assertEqual(builder["model"], "muse-spark-1.3-contributor")
        self.assertEqual(cfg["roles"]["plan-reviewer"]["provider"], "codex")
        self.assertEqual(cfg["roles"]["plan-reviewer"]["label"], "codexj")
        self.assertEqual(DevProvider("muse"), DevProvider.MUSE)
        self.assertIn("muse", _DEFAULT_PROMPT_PATTERNS)
        # The legacy codexf label no longer satisfies the default quorum.
        self.assertTrue(review_quorum(
            {"codexj": "No findings.", "qwen": "No findings."},
            {"codexj": "gpt", "qwen": "qwen"}))
        self.assertFalse(review_quorum(
            {"codexf": "No findings.", "qwen": "No findings."},
            {"codexf": "gpt", "qwen": "qwen"}))

    async def test_project_yamls_pin_muse_builder_and_claudep_reviewer(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for name in ("jardal-openceo", "jardal-trading"):
            with (repo / "projects" / name / "project.yaml").open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            roles = (data.get("dev") or {}).get("tier_s", {}).get("roles", {}) or {}
            builder = roles.get("builder", {}) or {}
            self.assertEqual(builder.get("provider"), "muse", name)
            command = " ".join(str(part) for part in (builder.get("command") or []))
            self.assertIn("--model muse-spark-1.3-contributor", command, name)
            self.assertIn("--reasoning-effort max", command, name)
            self.assertIn("--yolo", command, name)
            reviewer = roles.get("plan-reviewer", roles.get("plan_reviewer", {})) or {}
            self.assertTrue(
                any("codexj" in str(part) for part in (reviewer.get("command") or [])),
                f"{name}: plan-reviewer must run codexj",
            )
        with (repo / "projects" / "jardal-openceo" / "project.yaml").open(encoding="utf-8") as handle:
            openceo = yaml.safe_load(handle) or {}
        reviewers = (openceo.get("dev") or {}).get("tier_s", {}).get("reviewers", []) or []
        claudep = next((item for item in reviewers if item.get("label") == "claudep"), None)
        self.assertIsNotNone(claudep)
        assert claudep is not None
        self.assertEqual(claudep.get("provider"), "claude")
        self.assertTrue(claudep.get("active"))


if __name__ == "__main__":
    unittest.main()
