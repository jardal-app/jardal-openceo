"""Simple-tier review round (epic TASK-2026-00248, child 00251).

Build → gates → one quorum round (three reviewers, two independent
families) → builder fix verdicts → final gates → close; review=off
skips reviewers entirely. Covers phase order on/off for A/B/C,
quorum with mixed outcomes, off-mode zero-touch, snapshot resume,
FIX verdicts and the untouched S contract. Real throwaway git repos,
scripted reviewer/followup fakes. No network, no CLI, no LLM.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from agents.dev.session import (  # noqa: E402
    REVIEW_ROUND_PARAMS_KEY,
    CodingStepResult,
    DevExecutionOutput,
    DevSessionManager,
    _read_review_checkpoint,
    _rehydrate_findings,
    _simple_builder_family,
)
from agents.dev.tier_s.cycle import TierSCycle  # noqa: E402
from config_loader import ConfigLoader  # noqa: E402
from orchestration import findings as findings_mod  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from settings import Settings  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402

CLEAN = "No findings.\nREVIEW_DONE_simple_1\n"
FINDING = ("[WARN] [bug] tracked.txt:2 — off by one → bound the index\n"
           "> after\n"
           "REVIEW_DONE_simple_1\n")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        text=True).stdout.strip()


def _repo() -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="simple-review-")
    repo = Path(tmp.name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    (repo / "tracked.txt").write_text("one\nbefore\nthree\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return tmp


def _pool() -> list[dict[str, Any]]:
    return [
        {"label": "nemotron", "family": "nemotron", "priority": 1,
         "command": ["true"]},
        {"label": "mimo", "family": "mimo", "priority": 2,
         "command": ["true"]},
        {"label": "gemini", "family": "gemini", "provider": "gemini",
         "model": "gemini", "account": "agyd", "priority": 3,
         "headless_args": ["--print"]},
        {"label": "qwen-max", "family": "qwen", "priority": 4,
         "command": ["true"]},
    ]


def _config(repo: Path, pool: list[dict[str, Any]] | None = None,
            gates: list[dict[str, Any]] | None = None,
            auto_merge: bool | None = None,
            budgets: dict[str, Any] | None = None) -> dict[str, Any]:
    dev: dict[str, Any] = {"gates": gates if gates is not None
                           else [{"name": "t", "cmd": "true"}]}
    config: dict[str, Any] = {
        "project": {"repos": [{"local_path": str(repo),
                               "branch_base": "main"}]},
        "dev": dev}
    if pool is not None:
        # The live free-router replaces static slots from the local catalog;
        # these tests script the slots, so pin the router off (same opt-out
        # as the other routing suites).
        config["review"] = {
            "reviewers": pool,
            "review": {"free_router": {"enabled": False}},
        }
    if auto_merge is not None:
        dev["auto_merge"] = auto_merge
    if budgets is not None:
        dev["tier_s"] = {"budgets": budgets}
    return config


def _task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {"task_id": "task-1", "title": "t",
                            "description": "d"}
    task.update(overrides)
    return task


def _manager() -> DevSessionManager:
    return DevSessionManager(MagicMock(), MagicMock())


def _run_manager() -> DevSessionManager:
    loader = MagicMock()
    loader.validate_project = MagicMock(return_value=[])
    return DevSessionManager(MagicMock(), loader)


class _ScriptedReviewers:
    """Fake reviewer executor: scripted (rc, text, usage) per label."""

    def __init__(self, script: dict[str, tuple[int, str, dict]] | None = None,
                 default: str = CLEAN) -> None:
        self._script = script or {}
        self._default = default
        self.calls: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        self.calls.append(role)
        label = role.split(":", 1)[1]
        rc, text, usage = self._script.get(label, (0, self._default, {}))
        return SimpleNamespace(returncode=rc, elapsed_s=0.1,
                               output_tail=text, usage=usage)


def _followup_script(seen: list[str], fix_text: str | None = None,
                     cost: dict[str, Any] | None = None):
    async def fake(tool: Any, config: Any, repo: Path, prompt: str, *,
                   task_id: str = "", project: str = "",
                   badge_ticket: str = "") -> dict[str, Any] | None:
        seen.append(prompt)
        if fix_text is not None:
            (repo / ".openceo").mkdir(parents=True, exist_ok=True)
            (repo / ".openceo" / "FIX_1.md").write_text(fix_text)
        return dict(cost or {"engine": "fake", "duration_s": 0.1})
    return fake


def _open_branch(manager: DevSessionManager, task: dict[str, Any],
                   config: dict[str, Any], tool: CodingTool,
                   repo: Path) -> Any:
    branch = manager._open_simple_branch(task, config, tool, repo)
    assert branch is not None
    (repo / "tracked.txt").write_text("one\nafter\nthree\n")
    (repo / ".openceo").mkdir(parents=True, exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text("did the thing")
    return branch


def _output() -> DevExecutionOutput:
    return DevExecutionOutput(status=TaskStatus.IN_PROGRESS)


class PhaseOrderTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, tool: CodingTool, review: str | None,
                   pool: list[dict[str, Any]] | None) -> tuple[Any, Any]:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool=tool.value)
        if review is not None:
            task["review"] = review
        config = _config(repo, pool=pool)
        branch = _open_branch(manager, task, config, tool, repo)
        reviewers = _ScriptedReviewers()
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            output = await manager._finish_simple_tool(
                task, config, tool, _output(), branch)
        return output, reviewers

    async def test_on_runs_gates_review_close_in_order(self) -> None:
        for tool in (CodingTool.TIER_A, CodingTool.TIER_B,
                     CodingTool.TIER_C):
            with self.subTest(tool=tool.value):
                output, reviewers = await self._run(tool, None, _pool())
                self.assertEqual(output.status, TaskStatus.COMPLETED,
                                 output.error)
                kinds = [step["step"] for step in output.steps]
                self.assertEqual(
                    [kind for kind in kinds
                     if kind in ("gates", "review", "close")],
                    ["gates", "review", "close"])
                self.assertCountEqual(reviewers.calls,
                                        ["review:nemotron", "review:mimo",
                                         "review:gemini"])
                checkpoint = output.params[REVIEW_ROUND_PARAMS_KEY]
                self.assertEqual(checkpoint["phase"], "done")
                self.assertEqual(checkpoint["mode"], "on")
                self.assertTrue(checkpoint["quorum"])

    async def test_merge_stays_single_after_checkpoint(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        base_count = int(_git(repo, "rev-list", "--count", "main"))
        manager = _manager()
        task = _task(coding_tool="tier_c")
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        reviewers = _ScriptedReviewers()
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(
            int(_git(repo, "rev-list", "--count", "main")), base_count + 1)

    async def test_off_skips_reviewers_entirely(self) -> None:
        for tool in (CodingTool.TIER_A, CodingTool.TIER_B,
                     CodingTool.TIER_C):
            with self.subTest(tool=tool.value):
                tmp = _repo()
                self.addCleanup(tmp.cleanup)
                repo = Path(tmp.name)
                manager = _manager()
                task = _task(coding_tool=tool.value, review="off")
                config = _config(repo, pool=_pool())
                branch = _open_branch(manager, task, config, tool,
                                            repo)
                with patch.object(
                        manager, "_simple_reviewer_executor",
                        side_effect=AssertionError("must not build")):
                    output = await manager._finish_simple_tool(
                        task, config, tool, _output(), branch)
                self.assertEqual(output.status, TaskStatus.COMPLETED,
                                 output.error)
                self.assertNotIn("review", [step["step"]
                                            for step in output.steps])
                self.assertNotIn(REVIEW_ROUND_PARAMS_KEY,
                                 output.params or {})

    async def test_off_needs_no_pool(self) -> None:
        output, _reviewers = await self._run(
            CodingTool.TIER_C, "off", None)
        self.assertEqual(output.status, TaskStatus.COMPLETED)


class QuorumTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_outcomes_reach_quorum(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_b")
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_B, repo)
        reviewers = _ScriptedReviewers({
            "nemotron": (0, FINDING, {}),
            "mimo": (124, "timed out", {"timeout": True}),
            "gemini": (0, "garbage without marker\n", {}),
            "qwen-max": (0, CLEAN, {})})
        seen: list[str] = []
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers), \
                patch.object(manager, "_simple_followup",
                             new=_followup_script(
                                 seen, "F1: CORREGIDO — bounded\n")):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_B, _output(), branch)
        self.assertEqual(output.status, TaskStatus.COMPLETED, output.error)
        checkpoint = output.params[REVIEW_ROUND_PARAMS_KEY]
        self.assertTrue(checkpoint["quorum"])
        kinds = sorted(report["kind"] for report in checkpoint["reports"])
        self.assertIn("timeout", kinds)
        self.assertIn("malformed", kinds)
        self.assertGreaterEqual(
            sum(1 for report in checkpoint["reports"]
                if report["valid"]), 2)
        self.assertEqual(len(seen), 1)
        self.assertIn(".openceo/FIX_1.md", seen[0])
        self.assertEqual(checkpoint["resolutions"][0]["verdict"],
                         "CORREGIDO")

    async def test_exhausted_round_waits_recoverable(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c")
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        reviewers = _ScriptedReviewers(
            default="garbage without marker\n")
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(
            "review_no_quorum"), output.waiting_reason)
        self.assertIsNotNone(output.resume_at)
        checkpoint = output.params[REVIEW_ROUND_PARAMS_KEY]
        self.assertEqual(checkpoint["phase"], "round")
        self.assertEqual(checkpoint["no_quorum_waits"], 1)
        self.assertEqual(
            output.params["dev_checkpoint"]["branch"], branch.branch)
        self.assertEqual(
            _git(repo, "branch", "--list", branch.branch), branch.branch)

    async def test_same_family_alias_cannot_fabricate_quorum(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c")
        pool = [
            {"label": "gemini", "family": "gemini", "provider": "gemini",
             "account": "agyd", "priority": 1},
            {"label": "gemini2", "family": "gemini", "provider": "gemini",
             "account": "agyj", "priority": 2},
        ]
        config = _config(repo, pool=pool)
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        reviewers = _ScriptedReviewers()
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(
            "review_no_quorum"), output.waiting_reason)

    async def test_third_no_quorum_parks_for_owner(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_a",
                     params={"review_round": {"phase": "round",
                                             "no_quorum_waits": 2}})
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_A, repo)
        reviewers = _ScriptedReviewers(
            default="garbage without marker\n")
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_A, _output(), branch)
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith("needs_owner"),
                        output.waiting_reason)
        self.assertIsNone(output.resume_at)
        self.assertEqual(
            output.params[REVIEW_ROUND_PARAMS_KEY]["no_quorum_waits"], 3)
        checkpoint = output.params["dev_checkpoint"]
        self.assertEqual(checkpoint["branch"], branch.branch)
        self.assertTrue(checkpoint["build_done"])
        self.assertNotEqual(
            _git(repo, "branch", "--list", branch.branch), "")


class OffGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_gates_still_block(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c", review="off")
        config = _config(
            repo, pool=_pool(),
            gates=[{"name": "t", "cmd": "! grep -q after tracked.txt"}],
            budgets={"gate_retries": 0})
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        seen: list[str] = []
        with patch.object(
                manager, "_simple_reviewer_executor",
                side_effect=AssertionError("must not build")), \
                patch.object(manager, "_simple_followup",
                             new=_followup_script(seen)):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertNotIn("review", [step["step"]
                                    for step in output.steps])


async def _park_missing_verdicts(
        test: unittest.TestCase) -> tuple[Any, Path]:
    tmp = _repo()
    test.addCleanup(tmp.cleanup)
    repo = Path(tmp.name)
    manager = _manager()
    task = _task(coding_tool="tier_b")
    config = _config(repo, pool=_pool())
    branch = _open_branch(manager, task, config,
                                CodingTool.TIER_B, repo)
    reviewers = _ScriptedReviewers({"nemotron": (0, FINDING, {})})
    with patch.object(manager, "_simple_reviewer_executor",
                      return_value=reviewers), \
            patch.object(manager, "_simple_followup",
                         new=_followup_script([], None)):
        output = await manager._finish_simple_tool(
            task, config, CodingTool.TIER_B, _output(), branch)
    return output, repo


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_snapshot_reuses_reports(self) -> None:
        output, repo = await _park_missing_verdicts(self)
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertIn("without verdict", output.waiting_reason)
        manager = _manager()
        task = _task(coding_tool="tier_b", params=dict(output.params))
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_B, repo)
        reviewers = _ScriptedReviewers()
        seen: list[str] = []
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers), \
                patch.object(manager, "_simple_followup",
                             new=_followup_script(
                                 seen, "F1: DESCARTADO — pre-existing\n")):
            rerun = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_B, _output(), branch)
        self.assertEqual(reviewers.calls, [])
        self.assertEqual(rerun.status, TaskStatus.COMPLETED, rerun.error)
        self.assertIn("reused", [step["status"] for step in rerun.steps
                                 if step["step"] == "review"])
        self.assertEqual(
            rerun.params[REVIEW_ROUND_PARAMS_KEY]["resolutions"][0]
            ["verdict"], "DESCARTADO")

    async def test_other_snapshot_runs_fresh_round(self) -> None:
        output, repo = await _park_missing_verdicts(self)
        manager = _manager()
        task = _task(coding_tool="tier_b", params=dict(output.params))
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_B, repo)
        (repo / "tracked.txt").write_text("one\nchanged-again\nthree\n")
        reviewers = _ScriptedReviewers()
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            rerun = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_B, _output(), branch)
        self.assertEqual(rerun.status, TaskStatus.COMPLETED)
        self.assertEqual(len(reviewers.calls), 3)
        self.assertNotIn("reused", [step["status"] for step in rerun.steps
                                    if step["step"] == "review"])

    async def test_done_phase_skips_round_and_fixes(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c")
        config = _config(repo, pool=_pool(), auto_merge=False)
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        reviewers = _ScriptedReviewers()
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers):
            first = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(first.status, TaskStatus.COMPLETED)
        rerun_task = _task(coding_tool="tier_c",
                           params=dict(first.params))
        rerun_branch = _open_branch(manager, rerun_task, config,
                                          CodingTool.TIER_C, repo)
        rerun_reviewers = _ScriptedReviewers()
        seen: list[str] = []
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=rerun_reviewers), \
                patch.object(manager, "_simple_followup",
                             new=_followup_script(seen)):
            rerun = await manager._finish_simple_tool(
                rerun_task, config, CodingTool.TIER_C, _output(),
                rerun_branch)
        self.assertEqual(rerun_reviewers.calls, [])
        self.assertEqual(seen, [])
        self.assertEqual(rerun.status, TaskStatus.COMPLETED)


class FixVerdictTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_verdicts_park_with_ids(self) -> None:
        output, _repo = await _park_missing_verdicts(self)
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith("needs_owner"),
                        output.waiting_reason)
        self.assertIn("F1", output.waiting_reason)
        checkpoint = output.params[REVIEW_ROUND_PARAMS_KEY]
        self.assertEqual(checkpoint["phase"], "reviewed")
        self.assertEqual(checkpoint["fix_turns"], 2)
        self.assertEqual(checkpoint["resolutions"][0]["verdict"],
                         "missing")

    async def test_stale_fix_file_never_satisfies_verdicts(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c")
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                              CodingTool.TIER_C, repo)
        (repo / ".openceo" / "FIX_1.md").write_text(
            "F1: CORREGIDO — stale verdict from another ticket\n")
        reviewers = _ScriptedReviewers({"nemotron": (0, FINDING, {})})

        async def refused(*args: Any, **kwargs: Any) -> None:
            return None

        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers), \
                patch.object(manager, "_simple_followup", new=refused):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith("needs_owner"),
                        output.waiting_reason)
        self.assertIn("F1", output.waiting_reason)

    async def test_out_of_scope_dismissal_recorded(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c")
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        reviewers = _ScriptedReviewers({"nemotron": (0, FINDING, {})})
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers), \
                patch.object(manager, "_simple_followup",
                             new=_followup_script(
                                 [], "F1: DESCARTADO — fuera de alcance\n")):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(output.status, TaskStatus.COMPLETED, output.error)
        checkpoint = output.params[REVIEW_ROUND_PARAMS_KEY]
        self.assertTrue(checkpoint["resolutions"][0]["out_of_scope"])
        self.assertIn("out-of-scope", " ".join(
            step["detail"] for step in output.steps))

    async def test_final_gates_rerun_after_fixes(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c")
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        reviewers = _ScriptedReviewers({"nemotron": (0, FINDING, {})})
        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers), \
                patch.object(manager, "_simple_followup",
                             new=_followup_script(
                                 [], "F1: CORREGIDO — bounded\n")):
            output = await manager._finish_simple_tool(
                task, config, CodingTool.TIER_C, _output(), branch)
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        gates = [step for step in output.steps if step["step"] == "gates"]
        self.assertEqual(len(gates), 2)


class BuilderFamilyTests(unittest.TestCase):
    def test_tier_families(self) -> None:
        self.assertEqual(
            _simple_builder_family(CodingTool.TIER_A, None), "claude")
        self.assertEqual(
            _simple_builder_family(CodingTool.TIER_B, None), "codex")

    def test_c_link_families(self) -> None:
        self.assertEqual(_simple_builder_family(
            CodingTool.TIER_C, {"executor_link": "dsh"}), "deepseek")
        self.assertEqual(_simple_builder_family(
            CodingTool.TIER_C, {"executor_link": "claudep"}), "deepseek")
        self.assertEqual(_simple_builder_family(
            CodingTool.TIER_C, {"executor_link": "muse"}), "muse")
        self.assertEqual(
            _simple_builder_family(CodingTool.TIER_C, None), "muse")
        self.assertEqual(_simple_builder_family(
            CodingTool.TIER_C, {"executor_link": "unknown"}), "muse")


class CheckpointHelperTests(unittest.TestCase):
    def test_read_validates_shape(self) -> None:
        self.assertIsNone(_read_review_checkpoint(None))
        self.assertIsNone(_read_review_checkpoint({}))
        self.assertIsNone(_read_review_checkpoint(
            {REVIEW_ROUND_PARAMS_KEY: {"phase": "nope"}}))
        checkpoint = _read_review_checkpoint(
            {REVIEW_ROUND_PARAMS_KEY: {"phase": "reviewed"}})
        assert checkpoint is not None
        self.assertEqual(checkpoint["phase"], "reviewed")

    def test_rehydrate_round_trip(self) -> None:
        anchor = findings_mod.Anchor(
            raw="tracked.txt:2", path="tracked.txt", line=2,
            base=False, criterion=None)
        finding = findings_mod.Finding(
            id=1, severity="WARN", category="bug", anchors=[anchor],
            problem="p", fix="f", strength="strong", families=["qwen"])
        records = [{
            "id": finding.id, "severity": finding.severity,
            "category": finding.category,
            "anchors": [{"raw": anchor.raw, "path": anchor.path,
                         "line": anchor.line, "base": anchor.base,
                         "criterion": anchor.criterion}],
            "problem": finding.problem, "fix": finding.fix,
            "strength": finding.strength,
            "families": list(finding.families)}]
        (back,) = _rehydrate_findings(records)
        self.assertEqual(back.id, 1)
        self.assertEqual(back.anchors[0].path, "tracked.txt")
        self.assertEqual(_rehydrate_findings([None, "x", {}]), [])

    def test_fix_prompt_format(self) -> None:
        manager = _manager()
        prompt = manager._simple_fix_prompt(3, [])
        self.assertIn(".openceo/FINDINGS_1.md", prompt)
        self.assertIn(".openceo/FIX_1.md", prompt)
        self.assertIn("F<n>: CORREGIDO", prompt)
        self.assertIn("F<n>: DESCARTADO", prompt)
        retry = manager._simple_fix_prompt(3, [1, 3])
        self.assertIn("F1", retry)
        self.assertIn("F3", retry)


class NoRepoSkipTests(unittest.IsolatedAsyncioTestCase):
    async def test_on_without_repo_completes_with_note(self) -> None:
        manager = _run_manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p",
            summary="s"))
        output = await manager.run(_task(coding_tool="tier_c"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        review = [step for step in output.steps
                  if step["step"] == "review"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["status"], "skipped")

    async def test_off_without_repo_has_no_review_step(self) -> None:
        manager = _run_manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p",
            summary="s"))
        output = await manager.run(
            _task(coding_tool="tier_c", review="off"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(
            [step for step in output.steps if step["step"] == "review"],
            [])


class SimpleScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_persist_with_simple_scope(self) -> None:
        tmp = _repo()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        manager = _manager()
        task = _task(coding_tool="tier_c")
        config = _config(repo, pool=_pool())
        branch = _open_branch(manager, task, config,
                                    CodingTool.TIER_C, repo)
        reviewers = _ScriptedReviewers()
        docs: list[dict[str, Any]] = []

        class _Reviews:
            def __init__(self, repo: Any) -> None:
                del repo

            async def create_review(self, doc: dict[str, Any]) -> None:
                docs.append(doc)

        with patch.object(manager, "_simple_reviewer_executor",
                          return_value=reviewers), \
                patch("agents.dev.session.MongoSlotStore",
                      lambda repo: review_mod.InMemorySlotStore()), \
                patch("agents.dev.session.MongoReviewStore", _Reviews):
            manager._repo = MagicMock()
            try:
                output = await manager._finish_simple_tool(
                    task, config, CodingTool.TIER_C, _output(), branch)
            finally:
                manager._repo = None
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(len(docs), 3)
        self.assertTrue(all(doc["scope"] == "simple" for doc in docs))
        self.assertEqual(
            {doc["slot"] for doc in docs}, {"nemotron", "mimo", "gemini"})
        gemini = next(doc for doc in docs if doc["slot"] == "gemini")
        self.assertEqual(gemini["account"], "agyd")
        self.assertEqual(gemini["model"], "gemini")


class TierSContractTests(unittest.TestCase):
    def _review(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        repo = Path(__file__).resolve().parents[1]
        loader = ConfigLoader(Settings(
            _env_file=None, PROJECTS_DIR=str(repo / "projects")))
        review = loader.load_project("jardal-openceo")["review"]
        return list(review["reviewers"]), dict(review["review"])

    def test_s_rounds_and_quorum_kept(self) -> None:
        _reviewers, cfg = self._review()
        self.assertEqual(cfg["max_rounds"], 2)
        self.assertEqual(cfg["round1_parallel"], 3)
        self.assertEqual(cfg["round2_parallel"], 2)
        self.assertEqual(cfg["quorum"], 2)
        self.assertEqual(cfg["distinct_by"], "family")

    def test_s_pool_selects_gemini(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        loader = ConfigLoader(Settings(
            _env_file=None, PROJECTS_DIR=str(repo / "projects")))
        cfg = TierSCycle.config_from_project(
            loader.load_project("jardal-openceo"))
        pool = review_mod.parse_pool(cfg["reviewers"])
        self.assertIn("gemini", [slot.label for slot in pool
                                 if slot.active])
        self.assertEqual(
            sum(1 for slot in pool
                if slot.active and slot.family == "gemini"), 1)


class TierSOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_s_rejects_off_at_run(self) -> None:
        manager = _run_manager()
        manager._run_tier_s = AsyncMock()  # type: ignore[method-assign]
        output = await manager.run(
            _task(coding_tool="tier_s", review="off"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(
            "review_conflict"), output.waiting_reason)
        manager._run_tier_s.assert_not_awaited()
