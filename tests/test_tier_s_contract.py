"""Kept Tier S contract tests (the old subplan engine is gone).

Covers what the Tier S engine in :mod:`agents.dev.tier_s` still uses:
snapshots, quorum (with self-review extras), TUI question handling, the
non-blocking plan-review format, the global roster/pool config and the child
environment allowlist. Old-orchestrator tests were removed with
``consensus.py`` (owner order); the engine's behavior is proven by
``tests/test_tier_s_final.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import DevSessionManager
from agents.dev.tier_s_contract import (
    child_env, detect_claude_question, git_snapshot, parse_plan_observations,
    parse_plan_responses, plan_response_gaps, review_quorum,
    valid_plan_review, verify_plan_anchor,
)
from settings import Settings
from agents.dev.tmux_runtime import TmuxRuntime
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

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_git_snapshot_includes_new_files_and_deletions(self) -> None:
        before = git_snapshot(self.repo)
        (self.repo / "tracked.txt").unlink()
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        after = git_snapshot(self.repo)
        self.assertEqual(after.changed_since(before), ["new.txt", "tracked.txt"])
        self.assertIsNone(after.files["tracked.txt"])

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

    async def test_global_tier_s_config_pins_roster_and_projects_stay_clean(self) -> None:
        from config_loader import ConfigLoader
        from settings import Settings
        repo = Path(__file__).resolve().parents[1]
        loader = ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(repo / "projects")))
        # Cycle roles and the shared review machinery are global:
        # identical for every project.
        for name in ("jardal-openceo", "jardal-trading"):
            cfg = loader.load_project(name)
            roles = cfg["dev"]["tier_s"]["roles"]
            builder = roles["builder"]
            self.assertEqual(builder["provider"], "muse", name)
            self.assertEqual(builder["command"][0], "musej", name)
            self.assertEqual(
                cfg["review"]["roles"]["closer"]["command"][0],
                "musej", name)
            command = " ".join(str(part) for part in builder["command"])
            self.assertIn("--model muse-spark-1.3-contributor", command, name)
            # musej owns access/effort flags (TASK-2026-00122): repeating
            # --reasoning-effort crashes clap (exit 2), so the command
            # carries subcommand+model only.
            self.assertNotIn("--reasoning-effort", command, name)
            self.assertNotIn("--yolo", command, name)
            reviewer = roles["plan-reviewer"]
            self.assertTrue(
                any("codexf" in str(part) for part in reviewer["command"]),
                f"{name}: plan-reviewer must run codexf",
            )
        cfg = loader.load_project("jardal-openceo")
        tier_s = cfg["dev"]["tier_s"]
        self.assertEqual(tier_s["min_available_families"], 3)
        self.assertEqual(tier_s["roles"]["planner"]["account"], "claudej")
        self.assertEqual(tier_s["roles"]["plan-reviewer"]["account"], "codexf")
        self.assertEqual(tier_s["roles"]["builder"]["account"], "muse")
        reviewers = cfg["review"]["reviewers"]
        by_label = {item["label"]: item for item in reviewers}
        self.assertTrue(by_label["gemini"]["active"], "gemini")
        for label in ("qwen-max", "qwen-glm", "mimo", "nemotron"):
            self.assertNotIn(label, by_label)
        self.assertFalse(by_label["dsh"]["active"])
        self.assertEqual(by_label["dsh"]["priority"], 3)
        self.assertEqual(by_label["dsh"]["family"], "deepseek")
        self.assertFalse(by_label["claudep"]["active"])
        self.assertEqual(by_label["claudep"]["family"], "deepseek")
        self.assertTrue(by_label["muse-review"]["extra"])
        self.assertEqual(by_label["muse-review"]["command"][0], "muse",
                         "muse-review stays isolated: plain muse, never musej")
        active_families = {item["family"] for item in reviewers
                           if item["active"] and not item.get("extra")}
        self.assertEqual(active_families, {"gemini", "openrouter"})
        tiers = loader.load_project("jardal-openceo")["dev"]["tiers"]
        self.assertEqual(tiers["tier_a"]["account"], "claudej")
        self.assertEqual(tiers["tier_b"]["account"], "codexf")
        self.assertEqual(tiers["tier_c"]["chain"], ["muse", "dsh", "claudep"])
        self.assertEqual(tiers["tier_c"]["muse"]["command"][0], "musej")
        # No checked-in project may carry routing or Tier S overrides.
        for name in loader.available_projects():
            with (repo / "projects" / name / "project.yaml").open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            dev = (data.get("dev") or {}) if isinstance(data, dict) else {}
            self.assertNotIn("coding_tool", dev, name)
            self.assertTrue(not dev.get("tier_s"), name)
            self.assertTrue(not (data.get("review") or {}), name)
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
        merged = child_env({"DEEPSEEK_API_KEY": "sk-service"}, {"DEEPSEEK_API_KEY": "[REDACTED]"})
        self.assertEqual(merged["DEEPSEEK_API_KEY"], "[REDACTED]")


class ChildEnvLeakTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_tier_executor_does_not_leak_service_key(self) -> None:
        mgr = DevSessionManager(Settings(), MagicMock())
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "[REDACTED]"}):
            result = await mgr._run_process_detailed(
                [sys.executable, "-c", _ENV_PROBE], cwd=str(self.cwd),
                env={"DSH_HOME": "/home/develop/.dsh"},
                # Env-only probe: the FS fence has its own tests (00192).
                sandbox=False)
        self.assertTrue(result.ok, result.stderr)
        self.assertEqual(result.stdout, "CLEAN")


if __name__ == "__main__":
    unittest.main()
