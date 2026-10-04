"""Single routing reader for ``coding_tool`` (Tier S design §3, TASK-2026-00086).

Covers the resolver precedence, the legacy ingest promotion and the
``routing_missing`` wait of the dev dispatcher. No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.routing import (  # noqa: E402
    ROUTING_MISSING_REASON,
    normalize_document_routing,
    normalize_task_dict,
    resolve_coding_tool,
    resolved_tool_name,
    tool_for_tier,
)
from agents.dev.session import CodingStepResult, DevSessionManager  # noqa: E402
from state import CodingTier, CodingTool, TaskDocument, TaskSource, TaskStatus  # noqa: E402


def _task(**overrides):
    task = {"task_id": "task-1", "title": "t", "description": "d"}
    task.update(overrides)
    return task


class ResolveCodingToolTests(unittest.TestCase):
    def test_root_value_wins_and_is_not_deprecated(self) -> None:
        for value in ("codex", "claude", "glm", "gemini", "opencode", "kimi", "tier_s", "muse"):
            decision = resolve_coding_tool(_task(coding_tool=value))
            self.assertEqual(decision.tool, CodingTool(value))
            self.assertEqual(decision.source, "root")
            self.assertFalse(decision.deprecated)

    def test_root_accepts_alias_case_and_whitespace(self) -> None:
        decision = resolve_coding_tool(_task(coding_tool="  Claude-Code "))
        self.assertEqual(decision.tool, CodingTool.CLAUDE)
        self.assertEqual(decision.source, "root")

    def test_precedence_root_params_project_tier(self) -> None:
        config = {"dev": {"coding_tool": "gemini"}}
        task = _task(coding_tool="codex", coding_tier="tier3",
                     params={"coding_tool": "claude"})
        self.assertEqual(resolve_coding_tool(task, config).tool, CodingTool.CODEX)
        del task["coding_tool"]
        decision = resolve_coding_tool(task, config)
        self.assertEqual((decision.tool, decision.source), (CodingTool.CLAUDE, "params"))
        self.assertTrue(decision.deprecated)
        del task["params"]
        decision = resolve_coding_tool(task, config)
        self.assertEqual((decision.tool, decision.source), (CodingTool.GEMINI, "project"))
        self.assertTrue(decision.deprecated)
        decision = resolve_coding_tool(task, {})
        self.assertEqual((decision.tool, decision.source), (CodingTool.OPENCODE, "tier"))
        self.assertTrue(decision.deprecated)

    def test_tier_mapping(self) -> None:
        expected = {
            CodingTier.TIER1: CodingTool.CODEX,
            CodingTier.TIER2: CodingTool.CLAUDE,
            CodingTier.TIER3: CodingTool.OPENCODE,
            CodingTier.TIER4: CodingTool.KIMI,
        }
        for tier, tool in expected.items():
            self.assertEqual(tool_for_tier(tier), tool)
            decision = resolve_coding_tool(_task(coding_tier=tier.value))
            self.assertEqual(decision.tool, tool)
            decision = resolve_coding_tool(_task(params={"coding_tier": tier.value}))
            self.assertEqual(decision.tool, tool)

    def test_invalid_root_falls_through_during_transition(self) -> None:
        decision = resolve_coding_tool(
            _task(coding_tool="not-a-tool"), {"dev": {"coding_tool": "codex"}})
        self.assertEqual((decision.tool, decision.source), (CodingTool.CODEX, "project"))

    def test_nothing_resolves_to_routing_missing(self) -> None:
        decision = resolve_coding_tool(_task(), {})
        self.assertIsNone(decision.tool)
        self.assertEqual(decision.source, "none")
        self.assertIn("no routing value", decision.detail)

    def test_invalid_everywhere_reports_values(self) -> None:
        decision = resolve_coding_tool(
            _task(coding_tool="nope", coding_tier="tier9",
                  params={"coding_tool": "alsono"}),
            {"dev": {"coding_tool": "  "}},
        )
        self.assertIsNone(decision.tool)
        self.assertIn("nope", decision.detail)
        self.assertIn("alsono", decision.detail)
        self.assertIn("tier9", decision.detail)

    def test_resolved_tool_name(self) -> None:
        self.assertEqual(resolved_tool_name(_task(coding_tool="tier_s")), "tier_s")
        self.assertEqual(resolved_tool_name(_task(coding_tier="tier3")), "opencode")
        self.assertEqual(resolved_tool_name(_task()), "")


class NormalizeRoutingTests(unittest.TestCase):
    def test_valid_root_is_untouched(self) -> None:
        patch, warnings = normalize_task_dict(_task(coding_tool="codex"))
        self.assertEqual((patch, warnings), ({}, []))

    def test_params_promoted(self) -> None:
        patch, warnings = normalize_task_dict(_task(params={"coding_tool": "kimi"}))
        self.assertEqual(patch, {"coding_tool": "kimi"})
        self.assertTrue(any("params.coding_tool" in warning for warning in warnings))

    def test_tier_promoted(self) -> None:
        patch, warnings = normalize_task_dict(_task(coding_tier="tier3"))
        self.assertEqual(patch, {"coding_tool": "opencode"})
        self.assertTrue(any("coding_tier" in warning for warning in warnings))

    def test_invalid_root_ignored_with_warning(self) -> None:
        patch, warnings = normalize_task_dict(
            _task(coding_tool="nope", params={"coding_tool": "codex"}))
        self.assertEqual(patch, {"coding_tool": "codex"})
        self.assertTrue(any("invalid coding_tool" in warning for warning in warnings))

    def test_nothing_to_promote(self) -> None:
        self.assertEqual(normalize_task_dict(_task()), ({}, []))

    def test_document_mutation(self) -> None:
        doc = TaskDocument(source=TaskSource.API, title="t", description="d",
                           params={"coding_tool": "gemini"})
        warnings = normalize_document_routing(doc)
        self.assertEqual(doc.coding_tool, CodingTool.GEMINI)
        self.assertEqual(len(warnings), 1)


class DispatcherRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, *, config_errors: list | None = None, repo=None) -> DevSessionManager:
        settings = MagicMock()
        settings.DEEPSEEK_API_KEY = None  # keep TierSelector offline
        loader = MagicMock()
        loader.validate_project = MagicMock(return_value=config_errors or [])
        return DevSessionManager(settings, loader, repo=repo)

    async def test_routing_missing_waits_without_tier_selection(self) -> None:
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(
            side_effect=AssertionError("selector must not run without routing"))
        output = await manager.run(_task(), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(ROUTING_MISSING_REASON))
        manager._tier_selector.select.assert_not_called()

    async def test_root_tool_reaches_code_step(self) -> None:
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER1)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(_task(coding_tool="codex"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.CODEX)
        manager._step_code.assert_awaited_once()
        tool_arg = manager._step_code.await_args.args[2]
        self.assertEqual(tool_arg, CodingTool.CODEX)

    async def test_invalid_tier_falls_back_to_selector(self) -> None:
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER2)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(
            _task(coding_tool="claude", coding_tier="tier9"), {})
        self.assertEqual(output.coding_tier, CodingTier.TIER2)
        self.assertEqual(output.coding_tool, CodingTool.CLAUDE)
        manager._tier_selector.select.assert_awaited_once()

    async def test_invalid_config_waits_without_dispatch(self) -> None:
        manager = self._manager(config_errors=["dev.coding_tool is obsolete"])
        manager._tier_selector.select = AsyncMock(
            side_effect=AssertionError("selector must not run on invalid config"))
        manager._step_code = AsyncMock()
        output = await manager.run(
            _task(coding_tool="codex", project="proj"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith("config"))
        self.assertIsNone(output.resume_at)
        manager._tier_selector.select.assert_not_called()
        manager._step_code.assert_not_called()

    async def test_repo_busy_waits_with_resume(self) -> None:
        repo = MagicMock()
        repo.acquire_repo_lock = AsyncMock(return_value=None)
        manager = self._manager(repo=repo)
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER1)
        manager._step_code = AsyncMock()
        config = {"project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]}}
        output = await manager.run(_task(coding_tool="codex"), config)
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith("repo_busy"))
        self.assertIsNotNone(output.resume_at)
        manager._step_code.assert_not_called()

    async def test_lock_released_on_terminal_and_kept_on_waiting(self) -> None:
        repo = MagicMock()
        repo.acquire_repo_lock = AsyncMock(return_value={"holder": "task-1"})
        repo.release_repo_lock = AsyncMock(return_value=True)
        repo.touch_repo_lock = AsyncMock(return_value=True)
        config = {"project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]}}
        manager = self._manager(repo=repo)
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER1)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(_task(coding_tool="codex"), config)
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        repo.release_repo_lock.assert_awaited_once_with("/tmp/definitely-not-a-repo", "task-1")
        repo.touch_repo_lock.assert_not_called()

        repo2 = MagicMock()
        repo2.acquire_repo_lock = AsyncMock(return_value={"holder": "task-1"})
        repo2.release_repo_lock = AsyncMock(return_value=True)
        repo2.touch_repo_lock = AsyncMock(return_value=True)
        manager2 = self._manager(repo=repo2)
        manager2._tier_selector.select = AsyncMock(return_value=CodingTier.TIER1)
        manager2._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "waiting"}, prompt="p", summary="s",
            waiting_reason="rate_limit_queued"))
        output2 = await manager2.run(_task(coding_tool="codex"), config)
        self.assertEqual(output2.status, TaskStatus.WAITING)
        repo2.touch_repo_lock.assert_awaited_once_with("/tmp/definitely-not-a-repo", "task-1")
        repo2.release_repo_lock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
