"""Contract: every dev prompt starts with the repo preamble (TASK-2026-00123).

Oracle fixed before the build. Four repo cases per constructor:
  1. CLAUDE.md exists          -> "Lee/Read CLAUDE.md y/and ..."
  2. only AGENTS.md exists     -> "Lee/Read AGENTS.md y/and ..."
  3. AGENTS.md symlinks CLAUDE.md -> names CLAUDE.md only
  4. neither exists            -> no preamble
Plus the unspecified fifth (both are separate files -> names both).

Tier S prompts are English; simple-tier/QA/loose prompts are Spanish
(detected for free-text turns). One-shot executors keep no context between
calls, so retries and follow-ups also carry the preamble.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import DevSessionManager  # noqa: E402
from agents.dev.tier_s import cycle as cycle_mod  # noqa: E402
from objectives import engine as objectives_mod  # noqa: E402
from orchestration import knowledge as knowledge_mod  # noqa: E402
from orchestration import review as review_mod  # noqa: E402
from orchestration.findings import Anchor, Finding  # noqa: E402
from orchestration.knowledge import (  # noqa: E402
    detect_prompt_lang,
    repo_preamble,
    with_preamble,
)
from agents.dev.tier_s.models import SIGNALS  # noqa: E402
from agents.dev.tier_s.plan import PlannedFile, Plan  # noqa: E402
from dev_session_service import prepend_first_turn_preamble  # noqa: E402
from qa_review_service import QAReviewService  # noqa: E402
from state import CodingTool  # noqa: E402
from workflow_service import WorkflowToolRegistry  # noqa: E402

ES_CLAUDE = "Lee CLAUDE.md y "
ES_AGENTS = "Lee AGENTS.md y "
ES_BOTH = "Lee CLAUDE.md y AGENTS.md; "
EN_CLAUDE = "Read CLAUDE.md and "
EN_AGENTS = "Read AGENTS.md and "
EN_BOTH = "Read CLAUDE.md and AGENTS.md; "
PREAMBLE_MARKS = ("Lee CLAUDE.md", "Lee AGENTS.md", "Read CLAUDE.md", "Read AGENTS.md")


def _plan() -> Plan:
    return Plan(
        ticket="t", objective="", goal="g", acceptance=(),
        files=(PlannedFile(path="a.py", operation="modify", reason="r"),),
        contracts=(), decisions=(), steps=("s1",),
        estimate_files=1, estimate_lines=10, out_of_scope=(),
    )


def _ctx(repo: Path) -> SimpleNamespace:
    return SimpleNamespace(
        ticket={"title": "t", "description": "d", "acceptance": ["a1"]},
        repo=repo, ref="T-1",
        budgets={"files_per_ticket": 8, "lines_per_ticket": 400},
    )


def _finding() -> Finding:
    return Finding(
        id=1, severity="CRITICAL", category="bug",
        anchors=[Anchor(raw="a.py:1", path="a.py", line=1, base=False, criterion=None)],
        problem="p", fix="f", strength="s", families=["qwen", "glm"],
        quotes=["code"], explanations={},
    )


class _RepoCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="preamble-contract-")
        root = Path(cls._tmp.name)
        cls.claude_only = root / "claude_only"
        cls.agents_only = root / "agents_only"
        cls.symlink = root / "symlink"
        cls.neither = root / "neither"
        cls.both = root / "both"
        for path in (cls.claude_only, cls.agents_only, cls.symlink, cls.neither, cls.both):
            path.mkdir()
        (cls.claude_only / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
        (cls.agents_only / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        (cls.symlink / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
        (cls.symlink / "AGENTS.md").symlink_to(cls.symlink / "CLAUDE.md")
        (cls.both / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
        (cls.both / "AGENTS.md").write_text("# other rules\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def assertPreamble(self, prompt: str, repo: Path, lang: str) -> None:  # noqa: N802
        if repo is self.neither:
            for mark in PREAMBLE_MARKS:
                self.assertFalse(
                    prompt.startswith(mark), f"unexpected preamble in: {prompt[:80]!r}")
            return
        if lang == "es":
            if repo is self.agents_only:
                want = ES_AGENTS
            elif repo is self.both:
                want = ES_BOTH
            else:
                want = ES_CLAUDE
        else:
            if repo is self.agents_only:
                want = EN_AGENTS
            elif repo is self.both:
                want = EN_BOTH
            else:
                want = EN_CLAUDE
        self.assertTrue(
            prompt.startswith(want), f"want {want!r}, got: {prompt[:80]!r}")
        self.assertEqual(prompt.count("CLAUDE.md y CLAUDE.md"), 0)
        self.assertEqual(prompt.count("AGENTS.md y AGENTS.md"), 0)


class HelperTests(_RepoCases):
    def test_four_cases_spanish(self) -> None:
        self.assertTrue(repo_preamble(self.claude_only, "es").startswith(ES_CLAUDE))
        self.assertTrue(repo_preamble(self.agents_only, "es").startswith(ES_AGENTS))
        self.assertTrue(repo_preamble(self.symlink, "es").startswith(ES_CLAUDE))
        self.assertNotIn("AGENTS.md", repo_preamble(self.symlink, "es"))
        self.assertEqual(repo_preamble(self.neither, "es"), "")

    def test_four_cases_english(self) -> None:
        self.assertTrue(repo_preamble(self.claude_only, "en").startswith(EN_CLAUDE))
        self.assertTrue(repo_preamble(self.agents_only, "en").startswith(EN_AGENTS))
        self.assertTrue(repo_preamble(self.symlink, "en").startswith(EN_CLAUDE))
        self.assertNotIn("AGENTS.md", repo_preamble(self.symlink, "en"))
        self.assertEqual(repo_preamble(self.neither, "en"), "")

    def test_both_separate_files_names_both_once(self) -> None:
        self.assertTrue(repo_preamble(self.both, "es").startswith(ES_BOTH))
        self.assertTrue(repo_preamble(self.both, "en").startswith(EN_BOTH))

    def test_unknown_repo_has_no_preamble(self) -> None:
        self.assertEqual(repo_preamble(None, "es"), "")
        self.assertEqual(repo_preamble(None, "en"), "")
        self.assertEqual(repo_preamble("/tmp/definitely-not-a-repo", "es"), "")

    def test_with_preamble_joins_or_passes_through(self) -> None:
        joined = with_preamble("Body.", self.claude_only, "en")
        self.assertTrue(joined.startswith(EN_CLAUDE))
        self.assertTrue(joined.endswith("Body."))
        self.assertEqual(with_preamble("Body.", self.neither, "en"), "Body.")
        self.assertEqual(with_preamble("Body.", None, "es"), "Body.")

    def test_detect_prompt_lang(self) -> None:
        self.assertEqual(detect_prompt_lang("¿Qué sale en el preset build?"), "es")
        self.assertEqual(detect_prompt_lang("Revisa la rama y dime qué falla"), "es")
        self.assertEqual(detect_prompt_lang("sigue"), "es")
        self.assertEqual(detect_prompt_lang("Gates failed:\n[x] failed: boom"), "en")
        self.assertEqual(detect_prompt_lang("Write .tier-s/HANDOFF.md now."), "en")
        self.assertEqual(detect_prompt_lang("Fix the code (no commits)."), "en")


class TierSPromptsTests(_RepoCases):
    def test_planner_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod.TierSCycle._planner_prompt(None, _ctx(repo))
                self.assertPreamble(prompt, repo, "en")

    def test_plan_review_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod.TierSCycle._plan_review_prompt(
                    None, _ctx(repo), _plan())
                self.assertPreamble(prompt, repo, "en")

    def test_plan_response_prompt(self) -> None:
        obs = SimpleNamespace(id=3)
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod.TierSCycle._plan_response_prompt(
                    None, _ctx(repo), [obs])
                self.assertPreamble(prompt, repo, "en")
                empty = cycle_mod.TierSCycle._plan_response_prompt(
                    None, _ctx(repo), [])
                self.assertPreamble(empty, repo, "en")

    def test_builder_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod.TierSCycle._builder_prompt(
                    None, _ctx(repo), _plan())
                self.assertPreamble(prompt, repo, "en")

    def test_plan_retry_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._plan_retry_prompt(
                    "no PLAN.md", SIGNALS["plan_ready"], repo)
                self.assertPreamble(prompt, repo, "en")

    def test_plan_response_retry_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._plan_response_retry_prompt(
                    [1, 2], SIGNALS["plan_responded"], repo)
                self.assertPreamble(prompt, repo, "en")

    def test_handoff_fix_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._handoff_fix_prompt(SIGNALS["build_done"], repo)
                self.assertPreamble(prompt, repo, "en")

    def test_gates_fix_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._gates_fix_prompt(
                    "[g] failed: x", SIGNALS["build_done"], repo)
                self.assertPreamble(prompt, repo, "en")

    def test_reviewer_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = review_mod.reviewer_prompt(
                    "qwen-max", "REVIEW_REQUEST_1.md", "ticket", 1, repo=repo)
                self.assertPreamble(prompt, repo, "en")

    def test_muse_fix_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = review_mod.muse_fix_prompt(1, "FINDINGS_1.md", 2, repo=repo)
                self.assertPreamble(prompt, repo, "en")

    def test_fix_retry_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._fix_retry_prompt(
                    "FIX_1.md", [1], 1, repo)
                self.assertPreamble(prompt, repo, "en")

    def test_fix_gates_retry_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._fix_gates_retry_prompt(1, "[g] failed", repo)
                self.assertPreamble(prompt, repo, "en")

    def test_arbiter_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = review_mod.arbiter_prompt(
                    _finding(), "dudoso", "code", repo=repo)
                self.assertPreamble(prompt, repo, "en")

    def test_arbiter_retry_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._arbiter_retry_prompt(repo)
                self.assertPreamble(prompt, repo, "en")

    def test_arbitration_fix_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = cycle_mod._arbitration_fix_prompt(
                    1, "bug", "crash", "guard", "real", repo)
                self.assertPreamble(prompt, repo, "en")

    def test_objective_planner_prompt(self) -> None:
        objective = {"objective_id": "OBJ-1", "anchor": {"goal": "g"}}
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = objectives_mod._planner_prompt(
                    objective, "", [], "INBOX\n(none)\n", [], repo=repo)
                self.assertPreamble(prompt, repo, "en")

    def test_closure_judge_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = objectives_mod._closure_judge_prompt(
                    2, "goal", "contracts", repo)
                self.assertPreamble(prompt, repo, "en")

    def test_closure_retry_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = objectives_mod._closure_retry_prompt([1], repo)
                self.assertPreamble(prompt, repo, "en")

    def test_objective_planner_retry_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = objectives_mod._planner_retry_prompt(
                    ["bad json"], repo)
                self.assertPreamble(prompt, repo, "en")
                self.assertIn("PLANNER_RESULT.json", prompt)

    def test_vault_note_prompt(self) -> None:
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = knowledge_mod._note_prompt(
                    repo / "n.md", "p", "s", "e", repo=repo)
                self.assertPreamble(prompt, repo, "en")


class SimpleTierPromptsTests(_RepoCases):
    def _manager(self) -> DevSessionManager:
        settings = MagicMock()
        settings.DEEPSEEK_API_KEY = None
        settings.DEV_EXECUTION_MODE = "live"
        return DevSessionManager(settings, MagicMock())

    def _config(self, repo: Path) -> dict:
        return {"project": {"repos": [{"local_path": str(repo)}]}}

    def test_coding_prompt(self) -> None:
        task = {"task_id": "t", "title": "ti", "description": "de"}
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = self._manager()._build_coding_prompt(
                    task, self._config(repo))
                self.assertPreamble(prompt, repo, "es")

    def test_coding_prompt_unknown_repo_has_no_preamble(self) -> None:
        task = {"task_id": "t", "title": "ti", "description": "de"}
        prompt = self._manager()._build_coding_prompt(task, {})
        for mark in PREAMBLE_MARKS:
            self.assertFalse(prompt.startswith(mark))

    def test_qa_replica_matches_coding_prompt(self) -> None:
        service = QAReviewService(MagicMock(), MagicMock())
        ticket = {"task_id": "t", "title": "ti", "description": "de",
                  "ticket_number": 7}
        for repo in (self.claude_only, self.agents_only, self.symlink,
                     self.neither, self.both):
            with self.subTest(repo=repo.name):
                prompt = service._build_coding_prompt_replica(ticket, repo=repo)
                self.assertPreamble(prompt, repo, "es")

    def test_simple_followup_carries_preamble(self) -> None:
        import asyncio

        manager = self._manager()
        calls: list[dict] = []

        async def fake_run_process(command, stdin_payload=None, timeout=120,
                                   cwd=None, env=None):
            calls.append({"command": command, "stdin": stdin_payload})
            return True, "ok", ""

        async def go() -> None:
            manager._run_process = fake_run_process  # type: ignore[method-assign]
            manager._resolve_tier_executor = AsyncMock(  # type: ignore[method-assign]
                return_value=MagicMock(argv=["musej", "exec"], prompt_via="argv",
                                       env={}, label="tier_c:muse"))
            await manager._simple_followup(
                CodingTool.TIER_C, {}, self.claude_only,
                "Fix the code (no commits).")

        asyncio.run(go())
        self.assertTrue(calls[0]["command"][-1].startswith(EN_CLAUDE))


class LooseSessionTests(_RepoCases):
    def test_first_turn_gets_preamble_in_instruction_lang(self) -> None:
        session = {"messages": [{"role": "user", "content": "hola"}],
                   "repo_path": str(self.claude_only)}
        out = prepend_first_turn_preamble(session, "Revisa la rama")
        self.assertTrue(out.startswith(ES_CLAUDE))
        self.assertTrue(out.endswith("Revisa la rama"))

        out_en = prepend_first_turn_preamble(session, "Review the branch now")
        self.assertTrue(out_en.startswith(EN_CLAUDE))

    def test_later_turns_untouched(self) -> None:
        session = {"messages": [{"role": "user", "content": "a"},
                                {"role": "assistant", "content": "b"},
                                {"role": "user", "content": "c"}],
                   "repo_path": str(self.claude_only)}
        self.assertEqual(
            prepend_first_turn_preamble(session, "Revisa la rama"), "Revisa la rama")

    def test_first_turn_without_repo_files_untouched(self) -> None:
        session = {"messages": [{"role": "user", "content": "hola"}],
                   "repo_path": str(self.neither)}
        self.assertEqual(
            prepend_first_turn_preamble(session, "Revisa la rama"), "Revisa la rama")


class WorkflowRegistryTests(unittest.TestCase):
    def test_no_workflow_tool_launches_a_dev_cli(self) -> None:
        names = WorkflowToolRegistry().names()
        self.assertTrue(names)
        for name in names:
            lowered = name.lower()
            for marker in ("dev", "coding", "tier", "codex", "claude",
                           "muse", "qwen", "prompt"):
                self.assertNotIn(
                    marker, lowered,
                    f"workflow tool {name!r} may build dev prompts: wire the preamble")


if __name__ == "__main__":
    unittest.main()
