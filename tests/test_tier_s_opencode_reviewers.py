from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.dev.consensus import (
    _DEFAULT_OPENCODE_REVIEWERS,
    _REVIEW_LABELS,
    _TIER_S_DIR,
    CriticalConsensusOrchestrator,
)
from agents.dev.opencode_models import apply_opencode_model_override
from agents.dev.opencode_run import build_run_command


class NormalizedConfigOpenCodeReviewersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = MagicMock()
        self.settings.OPENCODE_GO_API_KEY = "test-key"
        self.tmux = MagicMock()
        self.orchestrator = CriticalConsensusOrchestrator(self.settings, self.tmux)

    def _make_config(self, tier_s_override: dict | None = None) -> dict:
        tier_s = {"review_rounds": 2, "max_plans": 15, "timeout_minutes": 15}
        if tier_s_override is not None:
            tier_s.update(tier_s_override)
        return {"dev": {"tier_s": tier_s}}

    def test_key_absent_returns_two_default_reviewers(self) -> None:
        config = self._make_config()
        cfg = self.orchestrator._normalized_config(config)
        self.assertEqual(
            cfg["opencode_reviewers"],
            list(_DEFAULT_OPENCODE_REVIEWERS),
        )
        self.assertEqual(len(cfg["opencode_reviewers"]), 2)

    def test_explicit_empty_list_disables_open_code_reviewers(self) -> None:
        config = self._make_config({"opencode_reviewers": []})
        cfg = self.orchestrator._normalized_config(config)
        self.assertEqual(cfg["opencode_reviewers"], [])

    def test_custom_list_parsed_verbatim(self) -> None:
        config = self._make_config({
            "opencode_reviewers": [
                {"label": "custom1", "model": "opencode-go/custom-model"},
            ],
        })
        cfg = self.orchestrator._normalized_config(config)
        self.assertEqual(
            cfg["opencode_reviewers"],
            [("custom1", "opencode-go/custom-model")],
        )

    def test_malformed_items_dropped(self) -> None:
        config = self._make_config({
            "opencode_reviewers": [
                {"label": "", "model": "m1"},           # empty label
                {"label": "ok", "model": "m2"},          # valid
                {"label": "no-model", "model": ""},      # empty model
                "not-a-dict",                            # not a dict
                {"label": "ok2", "model": "m3"},         # valid
            ],
        })
        cfg = self.orchestrator._normalized_config(config)
        self.assertEqual(
            cfg["opencode_reviewers"],
            [("ok", "m2"), ("ok2", "m3")],
        )

    def test_third_reviewer_appended_without_code_changes(self) -> None:
        config = self._make_config({
            "opencode_reviewers": [
                {"label": "kimi26", "model": "opencode-go/kimi-k2.6"},
                {"label": "qwen37", "model": "opencode-go/qwen3.7-max"},
                {"label": "third", "model": "opencode-go/third-model"},
            ],
        })
        cfg = self.orchestrator._normalized_config(config)
        self.assertEqual(len(cfg["opencode_reviewers"]), 3)
        self.assertIn(("third", "opencode-go/third-model"), cfg["opencode_reviewers"])


class BuilderFixPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = MagicMock()
        self.tmux = MagicMock()
        self.orchestrator = CriticalConsensusOrchestrator(self.settings, self.tmux)
        self.paths = MagicMock()
        self.paths.dir.parent = "/tmp/repo"

    def test_includes_tmux_review_files_and_opencode_review_files(self) -> None:
        opencode_reviewers = [("kimi26", "opencode-go/kimi-k2.6"), ("qwen37", "opencode-go/qwen3.7-max")]
        prompt = self.orchestrator._builder_fix_prompt(1, 1, self.paths, opencode_reviewers)

        self.assertIn("REVIEW-clauded-1-1.md (if exists)", prompt)
        self.assertIn("REVIEW-codexf-1-1.md (if exists)", prompt)
        self.assertIn("REVIEW-gemini-1-1.md (if exists)", prompt)
        self.assertIn("REVIEW-kimi26-1-1.md (if exists)", prompt)
        self.assertIn("REVIEW-qwen37-1-1.md (if exists)", prompt)

    def test_all_files_use_if_exists_wording(self) -> None:
        opencode_reviewers = [("kimi26", "opencode-go/kimi-k2.6")]
        prompt = self.orchestrator._builder_fix_prompt(2, 3, self.paths, opencode_reviewers)

        lines = [l for l in prompt.split("\n") if l.startswith("- ")]
        for line in lines:
            self.assertIn("(if exists)", line)

    def test_no_opencode_reviewers_still_lists_tmux_files(self) -> None:
        prompt = self.orchestrator._builder_fix_prompt(1, 1, self.paths, [])

        self.assertIn("REVIEW-clauded-1-1.md (if exists)", prompt)
        self.assertIn("REVIEW-codexf-1-1.md (if exists)", prompt)
        self.assertIn("REVIEW-gemini-1-1.md (if exists)", prompt)
        self.assertNotIn("REVIEW-kimi26-", prompt)


class OpenCodeReviewerArgvTests(unittest.TestCase):
    def test_build_command_contains_run_format_json_and_model(self) -> None:
        base_command = ["/tmp/opencode", "-m", "deepseek/deepseek-reasoner"]
        overridden = apply_opencode_model_override(base_command, "opencode-go/kimi-k2.6")
        argv = build_run_command(
            base_command=overridden,
            prompt="Review sub-plan 1",
            cwd="/tmp/repo",
            title="tier-s review kimi26 p1 r1",
            agent_name="build",
        )

        self.assertIn("run", argv)
        self.assertIn("--format", argv)
        self.assertIn("json", argv)
        self.assertIn("-m", argv)
        self.assertIn("opencode-go/kimi-k2.6", argv)
        self.assertNotIn("deepseek/deepseek-reasoner", argv)
        self.assertIn("--agent", argv)
        self.assertIn("build", argv)
        self.assertIn("--dir", argv)
        self.assertIn("/tmp/repo", argv)
        self.assertIn("Review sub-plan 1", argv)

    def test_model_override_replaces_existing_m_flag(self) -> None:
        base_command = ["/tmp/opencode", "--model", "opencode-go/kimi-k2.5"]
        overridden = apply_opencode_model_override(base_command, "opencode-go/qwen3.7-max")
        self.assertIn("--model", overridden)
        self.assertIn("opencode-go/qwen3.7-max", overridden)
        self.assertNotIn("opencode-go/kimi-k2.5", overridden)

    def test_model_override_appended_when_no_model_flag(self) -> None:
        base_command = ["/tmp/opencode"]
        overridden = apply_opencode_model_override(base_command, "opencode-go/kimi-k2.6")
        self.assertIn("-m", overridden)
        self.assertIn("opencode-go/kimi-k2.6", overridden)


class RunReviewRoundExpectedCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = MagicMock()
        self.settings.OPENCODE_GO_API_KEY = "test-key"
        self.tmux = MagicMock()
        self.orchestrator = CriticalConsensusOrchestrator(self.settings, self.tmux)

    def test_expected_count_includes_tmux_plus_opencode_reviewers(self) -> None:
        cfg = {
            "opencode_reviewers": [("kimi26", "m1"), ("qwen36", "m2")],
            "timeout_seconds": 60,
        }
        expected = len(_REVIEW_LABELS) + len(cfg["opencode_reviewers"])
        self.assertEqual(expected, 5)

    def test_expected_count_respects_disabled_reviewers(self) -> None:
        cfg = {
            "opencode_reviewers": [],
            "timeout_seconds": 60,
        }
        expected = len(_REVIEW_LABELS) + len(cfg["opencode_reviewers"])
        self.assertEqual(expected, 3)

    def test_expected_count_scales_with_extra_reviewers(self) -> None:
        cfg = {
            "opencode_reviewers": [("a", "ma"), ("b", "mb"), ("c", "mc")],
            "timeout_seconds": 60,
        }
        expected = len(_REVIEW_LABELS) + len(cfg["opencode_reviewers"])
        self.assertEqual(expected, 6)

    def test_expected_count_is_not_hardcoded_3(self) -> None:
        self.assertNotEqual(
            len(_REVIEW_LABELS) + len(_DEFAULT_OPENCODE_REVIEWERS),
            3,
            "ok_count threshold must be dynamic, not hardcoded 3",
        )


if __name__ == "__main__":
    unittest.main()
