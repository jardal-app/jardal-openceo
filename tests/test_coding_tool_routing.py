"""Single routing reader for ``coding_tool`` (Tier S design v2 §3, TASK-2026-00102).

Covers the tier resolver, the legacy provider→tier mapping, the Tier C
chain selection and the ``routing_missing`` wait of the dev dispatcher.
No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.routing import (  # noqa: E402
    ROUTING_MISSING_REASON,
    TIER_C_CHAIN,
    normalize_document_routing,
    normalize_task_dict,
    resolve_coding_tool,
    resolved_tool_name,
    select_tier_c_link,
    tool_for_tier,
)
from agents.dev.session import CodingStepResult, DevSessionManager  # noqa: E402
from state import CodingTier, CodingTool, TaskDocument, TaskSource, TaskStatus  # noqa: E402


def _task(**overrides):
    task = {"task_id": "task-1", "title": "t", "description": "d"}
    task.update(overrides)
    return task


class ResolveCodingToolTests(unittest.TestCase):
    def test_root_tier_wins_and_is_not_deprecated(self) -> None:
        for value in ("tier_s", "tier_a", "tier_b", "tier_c"):
            decision = resolve_coding_tool(_task(coding_tool=value))
            self.assertEqual(decision.tool, CodingTool(value))
            self.assertEqual(decision.source, "root")
            self.assertFalse(decision.deprecated)

    def test_root_accepts_case_and_whitespace(self) -> None:
        decision = resolve_coding_tool(_task(coding_tool="  Tier_A "))
        self.assertEqual(decision.tool, CodingTool.TIER_A)
        self.assertEqual(decision.source, "root")
        self.assertFalse(decision.deprecated)

    def test_legacy_provider_values_map_to_tiers_deprecated(self) -> None:
        expected = {
            "claude": CodingTool.TIER_A,
            "claude-code": CodingTool.TIER_A,
            "claudej": CodingTool.TIER_A,
            "clauded": CodingTool.TIER_A,
            "codex": CodingTool.TIER_B,
            "codexj": CodingTool.TIER_B,
            "codexf": CodingTool.TIER_B,
            "muse": CodingTool.TIER_C,
            "opencode": CodingTool.TIER_C,
            "kimi": CodingTool.TIER_C,
            "glm": CodingTool.TIER_C,
            "gemini": CodingTool.TIER_C,
            "low": CodingTool.TIER_C,
        }
        for value, tier in expected.items():
            decision = resolve_coding_tool(_task(coding_tool=value))
            self.assertEqual(decision.tool, tier, value)
            self.assertEqual(decision.source, "root", value)
            self.assertTrue(decision.deprecated, value)

    def test_precedence_root_params_project_tier(self) -> None:
        config = {"dev": {"coding_tool": "gemini"}}
        task = _task(coding_tool="tier_b", coding_tier="tier3",
                     params={"coding_tool": "claude"})
        self.assertEqual(resolve_coding_tool(task, config).tool, CodingTool.TIER_B)
        del task["coding_tool"]
        decision = resolve_coding_tool(task, config)
        self.assertEqual((decision.tool, decision.source), (CodingTool.TIER_A, "params"))
        self.assertTrue(decision.deprecated)
        del task["params"]
        decision = resolve_coding_tool(task, config)
        self.assertEqual((decision.tool, decision.source), (CodingTool.TIER_C, "project"))
        self.assertTrue(decision.deprecated)
        decision = resolve_coding_tool(task, {})
        self.assertEqual((decision.tool, decision.source), (CodingTool.TIER_C, "tier"))
        self.assertTrue(decision.deprecated)

    def test_tier_mapping(self) -> None:
        expected = {
            CodingTier.TIER1: CodingTool.TIER_B,
            CodingTier.TIER2: CodingTool.TIER_A,
            CodingTier.TIER3: CodingTool.TIER_C,
            CodingTier.TIER4: CodingTool.TIER_C,
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
        self.assertEqual((decision.tool, decision.source), (CodingTool.TIER_B, "project"))

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
        self.assertEqual(resolved_tool_name(_task(coding_tool="codex")), "tier_b")
        self.assertEqual(resolved_tool_name(_task(coding_tier="tier3")), "tier_c")
        self.assertEqual(resolved_tool_name(_task()), "")


class SelectTierCLinkTests(unittest.TestCase):
    def test_first_available_wins(self) -> None:
        self.assertEqual(
            select_tier_c_link({"muse": True, "dsh": True, "claudep": True}),
            "muse")
        self.assertEqual(
            select_tier_c_link({"muse": False, "dsh": True, "claudep": True}),
            "dsh")
        self.assertEqual(
            select_tier_c_link({"muse": False, "dsh": False, "claudep": True}),
            "claudep")

    def test_all_unavailable_falls_back_to_last_link(self) -> None:
        # The reactive links are tried until they fail: a conservative probe
        # must not veto the chain.
        self.assertEqual(
            select_tier_c_link({"muse": False, "dsh": False, "claudep": False}),
            "claudep")

    def test_custom_chain_order(self) -> None:
        self.assertEqual(
            select_tier_c_link({"dsh": True, "muse": True}, ["dsh", "muse"]),
            "dsh")

    def test_default_chain(self) -> None:
        self.assertEqual(TIER_C_CHAIN, ("muse", "dsh", "claudep"))

    def test_empty_chain_is_explicit(self) -> None:
        with self.assertRaises(ValueError):
            select_tier_c_link({}, [])


class NormalizeRoutingTests(unittest.TestCase):
    def test_valid_root_is_untouched(self) -> None:
        patch, warnings = normalize_task_dict(_task(coding_tool="tier_b"))
        self.assertEqual((patch, warnings), ({}, []))

    def test_legacy_root_promoted_to_tier(self) -> None:
        patch, warnings = normalize_task_dict(_task(coding_tool="codex"))
        self.assertEqual(patch, {"coding_tool": "tier_b"})
        self.assertTrue(any("codex" in warning and "tier_b" in warning
                            for warning in warnings))
        patch, _ = normalize_task_dict(_task(coding_tool="opencode"))
        self.assertEqual(patch, {"coding_tool": "tier_c"})

    def test_params_promoted(self) -> None:
        patch, warnings = normalize_task_dict(_task(params={"coding_tool": "kimi"}))
        self.assertEqual(patch, {"coding_tool": "tier_c"})
        self.assertTrue(any("params.coding_tool" in warning for warning in warnings))

    def test_tier_promoted(self) -> None:
        patch, warnings = normalize_task_dict(_task(coding_tier="tier3"))
        self.assertEqual(patch, {"coding_tool": "tier_c"})
        self.assertTrue(any("coding_tier" in warning for warning in warnings))

    def test_invalid_root_ignored_with_warning(self) -> None:
        patch, warnings = normalize_task_dict(
            _task(coding_tool="nope", params={"coding_tool": "codex"}))
        self.assertEqual(patch, {"coding_tool": "tier_b"})
        self.assertTrue(any("invalid coding_tool" in warning for warning in warnings))

    def test_nothing_to_promote(self) -> None:
        self.assertEqual(normalize_task_dict(_task()), ({}, []))

    def test_document_mutation(self) -> None:
        doc = TaskDocument(source=TaskSource.API, title="t", description="d",
                           params={"coding_tool": "gemini"})
        warnings = normalize_document_routing(doc)
        self.assertEqual(doc.coding_tool, CodingTool.TIER_C)
        self.assertEqual(len(warnings), 1)


class DispatcherRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, *, config_errors: list | None = None, repo=None) -> DevSessionManager:
        settings = MagicMock()
        settings.DEEPSEEK_API_KEY = None  # keep TierSelector offline
        loader = MagicMock()
        loader.validate_project = MagicMock(return_value=config_errors or [])
        return DevSessionManager(settings, loader, repo=repo)

    async def test_objective_child_without_tier_waits_without_tier_selection(self) -> None:
        # Deliberate contract change (TASK-2026-00137): loose tickets without
        # a tier self-classify at dispatch instead of parking; only objective
        # children (which must arrive with the planner tier) still wait.
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(
            side_effect=AssertionError("selector must not run without routing"))
        output = await manager.run(_task(objective_id="OBJ-1"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(ROUTING_MISSING_REASON))
        manager._tier_selector.select.assert_not_called()

    async def test_loose_ticket_without_tier_is_classified(self) -> None:
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER1)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(_task(), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_B)
        assert output.params is not None
        self.assertIn("tier_reason", output.params)
        manager._step_code.assert_awaited_once()

    async def test_root_tier_reaches_code_step(self) -> None:
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER1)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(_task(coding_tool="tier_b"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_B)
        manager._step_code.assert_awaited_once()
        tool_arg = manager._step_code.await_args.args[2]
        self.assertEqual(tool_arg, CodingTool.TIER_B)

    async def test_legacy_root_value_still_dispatches_deprecated(self) -> None:
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER1)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(_task(coding_tool="opencode"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_C)

    async def test_invalid_tier_falls_back_to_selector(self) -> None:
        manager = self._manager()
        manager._tier_selector.select = AsyncMock(return_value=CodingTier.TIER2)
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(
            _task(coding_tool="tier_a", coding_tier="tier9"), {})
        self.assertEqual(output.coding_tier, CodingTier.TIER2)
        self.assertEqual(output.coding_tool, CodingTool.TIER_A)
        manager._tier_selector.select.assert_awaited_once()

    async def test_invalid_config_waits_without_dispatch(self) -> None:
        manager = self._manager(config_errors=["dev.coding_tool is obsolete"])
        manager._tier_selector.select = AsyncMock(
            side_effect=AssertionError("selector must not run on invalid config"))
        manager._step_code = AsyncMock()
        output = await manager.run(
            _task(coding_tool="tier_b", project="proj"), {})
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
        output = await manager.run(_task(coding_tool="tier_b"), config)
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
        output = await manager.run(_task(coding_tool="tier_b"), config)
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
        output2 = await manager2.run(_task(coding_tool="tier_b"), config)
        self.assertEqual(output2.status, TaskStatus.WAITING)
        repo2.touch_repo_lock.assert_awaited_once_with("/tmp/definitely-not-a-repo", "task-1")
        repo2.release_repo_lock.assert_not_called()


class TierExecutorTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self) -> DevSessionManager:
        settings = MagicMock()
        settings.DEEPSEEK_API_KEY = None  # keep TierSelector offline
        settings.DEV_EXECUTION_MODE = "live"
        return DevSessionManager(settings, MagicMock())

    def _config(self) -> dict:
        return {
            "project": {"repos": [{"local_path": "/tmp/definitely-not-a-repo"}]},
            "dev": {
                "tiers": {
                    "tier_a": {"account": "claudej"},
                    "tier_b": {"account": "codexj"},
                    "tier_c": {
                        "chain": ["muse", "dsh", "claudep"],
                        "muse": {"command": ["musej", "exec"]},
                        "dsh": {"command": ["node", "/opt/dsh/bin.js", "--profile", "headless", "-"],
                                "env": {"DSH_HOME": "/tmp/dsh"}},
                        "claudep": {"command": ["claudep"]},
                    },
                },
                "providers": {
                    "claude": {"command": ["/home/develop/.local/bin/claudej"], "cwd": "/tmp/repo", "env": {}},
                    "codex": {"command": ["/home/develop/.local/bin/codexf"], "cwd": "/tmp/repo", "env": {}},
                    "muse": {"command": ["/home/develop/.local/bin/musej"], "cwd": "/tmp/repo", "env": {}},
                },
            },
        }

    async def test_tier_a_runs_claude_account_one_shot(self) -> None:
        executor = await self._manager()._resolve_tier_executor(
            CodingTool.TIER_A, self._config())
        self.assertEqual(executor.argv, ["/home/develop/.local/bin/claudej", "--print"])
        self.assertEqual(executor.prompt_via, "argv")
        self.assertEqual(executor.cwd, "/tmp/repo")
        self.assertEqual(executor.label, "tier_a")

    async def test_tier_b_runs_codex_account_from_global_config(self) -> None:
        # The loose codex entry points at codexf; Tier B uses the codexj
        # account from the global tiers config.
        executor = await self._manager()._resolve_tier_executor(
            CodingTool.TIER_B, self._config())
        self.assertEqual(executor.argv, ["codexj", "exec", "--ephemeral", "-"])
        self.assertEqual(executor.prompt_via, "stdin")
        self.assertEqual(executor.cwd, "/tmp/repo")
        self.assertEqual(executor.label, "tier_b")

    async def test_tier_c_prefers_muse_when_quota_available(self) -> None:
        manager = self._manager()
        manager._rate_monitor = MagicMock()
        manager._rate_monitor.can_launch = AsyncMock(return_value=True)
        executor = await manager._resolve_tier_executor(CodingTool.TIER_C, self._config())
        self.assertEqual(executor.argv, ["musej", "exec"])
        self.assertEqual(executor.prompt_via, "argv")
        self.assertEqual(executor.label, "tier_c:muse")

    async def test_tier_c_falls_through_chain_by_availability(self) -> None:
        manager = self._manager()
        manager._rate_monitor = MagicMock()
        manager._rate_monitor.can_launch = AsyncMock(return_value=False)
        with patch.object(DevSessionManager, "_command_available",
                          side_effect=lambda argv: argv[0] == "node"):
            executor = await manager._resolve_tier_executor(CodingTool.TIER_C, self._config())
        self.assertEqual(executor.argv[0], "node")
        self.assertEqual(executor.prompt_via, "stdin")
        self.assertEqual(executor.env, {"DSH_HOME": "/tmp/dsh"})
        self.assertEqual(executor.label, "tier_c:dsh")

    async def test_tier_c_ends_at_claudep(self) -> None:
        manager = self._manager()
        manager._rate_monitor = MagicMock()
        manager._rate_monitor.can_launch = AsyncMock(return_value=False)
        with patch.object(DevSessionManager, "_command_available", return_value=False):
            executor = await manager._resolve_tier_executor(CodingTool.TIER_C, self._config())
        self.assertEqual(executor.argv, ["claudep", "--print"])
        self.assertEqual(executor.prompt_via, "argv")
        self.assertEqual(executor.label, "tier_c:claudep")

    async def test_step_code_delivers_prompt_per_executor(self) -> None:
        manager = self._manager()
        calls = []

        async def fake_run_process(command, stdin_payload=None, timeout=120, cwd=None, env=None):
            calls.append({"command": command, "stdin": stdin_payload, "cwd": cwd, "env": env})
            return True, "did the thing", ""

        manager._run_process = fake_run_process  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory(prefix="tier-preamble-") as tmp:
            repo = Path(tmp)
            (repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            config = self._config()
            config["project"]["repos"][0]["local_path"] = str(repo)
            with patch.object(DevSessionManager, "_resolve_tier_executor",
                              AsyncMock(return_value=MagicMock(
                                  argv=["musej", "exec"], prompt_via="argv",
                                  cwd="/tmp/repo", env={}, label="tier_c:muse"))):
                result = await manager._step_code(_task(), config, CodingTool.TIER_C)
        self.assertTrue(calls[0]["command"][-1].startswith("Lee AGENTS.md y "))
        self.assertIsNone(calls[0]["stdin"])
        self.assertIn("tier_c:muse", result.step["detail"])

        calls.clear()
        with tempfile.TemporaryDirectory(prefix="tier-preamble-") as tmp:
            repo = Path(tmp)
            (repo / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
            config = self._config()
            config["project"]["repos"][0]["local_path"] = str(repo)
            with patch.object(DevSessionManager, "_resolve_tier_executor",
                              AsyncMock(return_value=MagicMock(
                                  argv=["codexj", "exec", "--ephemeral", "-"], prompt_via="stdin",
                                  cwd="/tmp/repo", env={}, label="tier_b"))):
                await manager._step_code(_task(), config, CodingTool.TIER_B)
        self.assertEqual(calls[0]["command"], ["codexj", "exec", "--ephemeral", "-"])
        assert calls[0]["stdin"] is not None
        self.assertTrue(calls[0]["stdin"].startswith("Lee CLAUDE.md y "))

    async def test_step_code_without_repo_files_has_no_preamble(self) -> None:
        manager = self._manager()
        calls = []

        async def fake_run_process(command, stdin_payload=None, timeout=120, cwd=None, env=None):
            calls.append({"command": command, "stdin": stdin_payload})
            return True, "did the thing", ""

        manager._run_process = fake_run_process  # type: ignore[method-assign]
        with patch.object(DevSessionManager, "_resolve_tier_executor",
                          AsyncMock(return_value=MagicMock(
                              argv=["musej", "exec"], prompt_via="argv",
                              cwd="/tmp/repo", env={}, label="tier_c:muse"))):
            await manager._step_code(_task(), self._config(), CodingTool.TIER_C)
        # _config() points at /tmp/definitely-not-a-repo: nothing to name.
        self.assertTrue(calls[0]["command"][-1].startswith("TAREA"))


if __name__ == "__main__":
    unittest.main()
