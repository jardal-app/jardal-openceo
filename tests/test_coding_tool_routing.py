"""Single routing reader for ``coding_tool`` (Tier S design v2 §3, TASK-2026-00102).

Covers the tier resolver, the Tier C chain selection and the
``routing_missing`` wait of the dev dispatcher. TASK-2026-00138 retired
the numeric tiers, the pre-v2 provider values and every deprecated
source: only a top-level tier letter resolves. No network, no CLI, no LLM.
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
    resolve_coding_tool,
    resolved_tool_name,
    select_tier_c_link,
)
from agents.dev.session import CodingStepResult, DevSessionManager  # noqa: E402
from objectives.agent_result import SCHEMA_PATH as AGENT_RESULT_SCHEMA  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402


def _task(**overrides):
    task = {"task_id": "task-1", "title": "t", "description": "d"}
    task.update(overrides)
    return task


class ResolveCodingToolTests(unittest.TestCase):
    def test_root_tier_wins(self) -> None:
        for value in ("tier_s", "tier_a", "tier_b", "tier_c"):
            decision = resolve_coding_tool(_task(coding_tool=value))
            self.assertEqual(decision.tool, CodingTool(value))
            self.assertEqual(decision.source, "root")

    def test_root_accepts_case_and_whitespace(self) -> None:
        decision = resolve_coding_tool(_task(coding_tool="  Tier_A "))
        self.assertEqual(decision.tool, CodingTool.TIER_A)
        self.assertEqual(decision.source, "root")

    def test_retired_provider_values_do_not_resolve(self) -> None:
        for value in ("claude", "claude-code", "claudej", "clauded",
                      "codex", "codexj", "codexf", "muse", "opencode",
                      "kimi", "glm", "gemini", "low"):
            decision = resolve_coding_tool(_task(coding_tool=value))
            self.assertIsNone(decision.tool, value)
            self.assertEqual(decision.source, "none", value)

    def test_only_root_letter_resolves(self) -> None:
        task = _task(coding_tool="tier_b", coding_tier="tier3",
                     params={"coding_tool": "claude"})
        self.assertEqual(resolve_coding_tool(task).tool, CodingTool.TIER_B)
        # Retired sources never resolve, even with valid-looking values.
        del task["coding_tool"]
        decision = resolve_coding_tool(task)
        self.assertIsNone(decision.tool)
        self.assertEqual(decision.source, "none")
        decision = resolve_coding_tool(_task(params={"coding_tool": "tier_a"}))
        self.assertIsNone(decision.tool)
        self.assertEqual(decision.source, "none")

    def test_invalid_root_resolves_to_none(self) -> None:
        decision = resolve_coding_tool(_task(coding_tool="not-a-tool"))
        self.assertIsNone(decision.tool)
        self.assertEqual(decision.source, "none")
        self.assertIn("not-a-tool", decision.detail)

    def test_nothing_resolves_to_routing_missing(self) -> None:
        decision = resolve_coding_tool(_task())
        self.assertIsNone(decision.tool)
        self.assertEqual(decision.source, "none")
        self.assertIn("no routing value", decision.detail)

    def test_stale_values_are_ignored(self) -> None:
        decision = resolve_coding_tool(
            _task(coding_tool="nope", coding_tier="tier9",
                  params={"coding_tool": "alsono"}))
        self.assertIsNone(decision.tool)
        self.assertIn("nope", decision.detail)
        self.assertNotIn("alsono", decision.detail)
        self.assertNotIn("tier9", decision.detail)

    def test_resolved_tool_name(self) -> None:
        self.assertEqual(resolved_tool_name(_task(coding_tool="tier_s")), "tier_s")
        self.assertEqual(resolved_tool_name(_task(coding_tool="codex")), "")
        self.assertEqual(resolved_tool_name(_task(coding_tier="tier3")), "")
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


class DispatcherRoutingTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self, *, config_errors: list | None = None, repo=None) -> DevSessionManager:
        settings = MagicMock()
        loader = MagicMock()
        loader.validate_project = MagicMock(return_value=config_errors or [])
        return DevSessionManager(settings, loader, repo=repo)

    async def test_objective_child_without_tier_waits_without_dispatch(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock()
        output = await manager.run(_task(objective_id="OBJ-1"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(ROUTING_MISSING_REASON))
        manager._step_code.assert_not_called()

    async def test_explicit_dev_without_tier_runs_default_c(self) -> None:
        # Deliberate contract change (TASK-2026-00204): the keyword
        # classifier is gone. Explicit dev without a tier runs the safe
        # C default (§25); undecided work waits fail-closed (below) —
        # the run pre-stage plans every pedido before dispatch.
        manager = self._manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(_task(agent_hint="dev"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_C)
        assert output.params is not None
        self.assertIn("tier_reason", output.params)
        manager._step_code.assert_awaited_once()

    async def test_loose_ticket_without_decision_waits(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock()
        output = await manager.run(_task(), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(ROUTING_MISSING_REASON))
        manager._step_code.assert_not_called()

    async def test_root_tier_reaches_code_step(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(_task(coding_tool="tier_b"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_B)
        manager._step_code.assert_awaited_once()
        tool_arg = manager._step_code.await_args.args[2]
        self.assertEqual(tool_arg, CodingTool.TIER_B)

    async def test_retired_root_value_waits_without_decision(self) -> None:
        # Retired values (TASK-2026-00138) do not resolve; without any
        # other decision the ticket waits fail-closed (TASK-2026-00204).
        manager = self._manager()
        manager._step_code = AsyncMock()
        output = await manager.run(_task(coding_tool="opencode"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith(ROUTING_MISSING_REASON))
        manager._step_code.assert_not_called()

    async def test_stale_numeric_tier_is_ignored(self) -> None:
        manager = self._manager()
        manager._step_code = AsyncMock(return_value=CodingStepResult(
            step={"step": "code", "status": "ok"}, prompt="p", summary="s"))
        output = await manager.run(
            _task(coding_tool="tier_a", coding_tier="tier9"), {})
        self.assertEqual(output.status, TaskStatus.COMPLETED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_A)

    async def test_invalid_config_waits_without_dispatch(self) -> None:
        manager = self._manager(config_errors=["dev.coding_tool is obsolete"])
        manager._step_code = AsyncMock()
        output = await manager.run(
            _task(coding_tool="tier_b", project="proj"), {})
        self.assertEqual(output.status, TaskStatus.WAITING)
        assert output.waiting_reason is not None
        self.assertTrue(output.waiting_reason.startswith("config"))
        self.assertIsNone(output.resume_at)
        manager._step_code.assert_not_called()

    async def test_repo_busy_waits_with_resume(self) -> None:
        repo = MagicMock()
        repo.acquire_repo_lock = AsyncMock(return_value=None)
        manager = self._manager(repo=repo)
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
        self.assertEqual(
            executor.argv,
            ["musej", "exec", "--json", "--output-schema", str(AGENT_RESULT_SCHEMA)])
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

        async def fake_run_process(command, stdin_payload=None, timeout=120, cwd=None, env=None,
                                   sandbox=True, config_argv=None, home_file_overlays=None,
                                   extra_read_roots=()):
            calls.append({"command": command, "stdin": stdin_payload, "cwd": cwd, "env": env})
            return True, "did the thing", ""

        async def fake_overlays(provider, role, workspace, profile_name="dev"):
            return {}, "badge-stub"

        manager._run_process = fake_run_process  # type: ignore[method-assign]
        provider_stub = MagicMock()
        provider_stub.revoke = AsyncMock()
        with tempfile.TemporaryDirectory(prefix="tier-preamble-") as tmp:
            repo = Path(tmp)
            (repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            config = self._config()
            config["project"]["repos"][0]["local_path"] = str(repo)
            with patch.object(DevSessionManager, "_resolve_tier_executor",
                              AsyncMock(return_value=MagicMock(
                                  argv=["musej", "exec"], prompt_via="argv",
                                  cwd="/tmp/repo", env={}, label="tier_c:muse"))), \
                 patch.object(DevSessionManager, "_ticket_badge_provider",
                              return_value=provider_stub), \
                 patch.object(DevSessionManager, "_muse_role_overlays",
                              AsyncMock(side_effect=fake_overlays)):
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
                                  cwd="/tmp/repo", env={}, label="tier_b",
                                  role_profile=None))):
                await manager._step_code(_task(), config, CodingTool.TIER_B)
        self.assertEqual(calls[0]["command"], ["codexj", "exec", "--ephemeral", "-"])
        assert calls[0]["stdin"] is not None
        self.assertTrue(calls[0]["stdin"].startswith("Lee CLAUDE.md y "))

    def test_ensure_codex_exec_converts_interactive_argv(self) -> None:
        # TASK-2026-00157 regression (root cause TASK-2026-00153): the
        # legacy interactive codex argv (project.yaml@3128146, no `exec`)
        # trips the TUI TTY gate on a piped stdin (rc=1). The guard
        # inserts `exec` after the binary, mirroring _ensure_print_flag.
        legacy = ["/home/develop/.local/bin/codex", "--sandbox",
                  "workspace-write", "-a", "never", "--no-alt-screen"]
        self.assertEqual(
            DevSessionManager._ensure_codex_exec(legacy),
            ["/home/develop/.local/bin/codex", "exec", "--sandbox",
             "workspace-write", "-a", "never", "--no-alt-screen"])
        # Idempotent on the headless form; non-codex argv pass through.
        headless = ["codexj", "exec", "--ephemeral", "-"]
        self.assertEqual(
            DevSessionManager._ensure_codex_exec(headless), headless)
        other = ["claudej", "--print"]
        self.assertEqual(
            DevSessionManager._ensure_codex_exec(other), other)

    async def test_codex_exec_guard_eliminates_tty_gate(self) -> None:
        # End-to-end with a stub modeling the codex TTY gate only (no
        # tokens, no real CLI): interactive argv via pipe reproduces the
        # signature; the same argv through the guard runs headless.
        stub = ('#!/bin/sh\nfor a in "$@"; do case "$a" in exec)'
                ' cat >/dev/null; echo OK; exit 0;; esac; done\n'
                'if [ ! -t 0 ]; then echo "Error: stdin is not a terminal"'
                ' >&2; exit 1; fi\necho TUI; exit 0\n')
        with tempfile.TemporaryDirectory(prefix="codex-guard-") as tmp:
            exe = Path(tmp) / "codex"
            exe.write_text(stub, encoding="utf-8")
            exe.chmod(0o755)
            manager = self._manager()
            buggy = await manager._run_process_detailed(
                [str(exe), "--sandbox", "workspace-write", "-a", "never",
                 "--no-alt-screen"],
                stdin_payload="prompt", timeout=30, cwd=tmp, env={},
                # Plumbing probe, not a fencing test (00192).
                sandbox=False)
            self.assertFalse(buggy.ok)
            self.assertIn("stdin is not a terminal", buggy.stderr)
            guarded = DevSessionManager._ensure_codex_exec(
                [str(exe), "--sandbox", "workspace-write", "-a", "never",
                 "--no-alt-screen"])
            self.assertIn("exec", guarded)
            fixed = await manager._run_process_detailed(
                guarded, stdin_payload="prompt",
                timeout=30, cwd=tmp, env={},
                # Plumbing probe, not a fencing test (00192).
                sandbox=False)
            self.assertTrue(fixed.ok, fixed.stderr)
            self.assertNotIn("stdin is not a terminal", fixed.stderr)

    async def test_step_code_without_repo_files_has_no_preamble(self) -> None:
        manager = self._manager()
        calls = []

        async def fake_run_process(command, stdin_payload=None, timeout=120, cwd=None, env=None,
                                   sandbox=True, config_argv=None, home_file_overlays=None,
                                   extra_read_roots=()):
            calls.append({"command": command, "stdin": stdin_payload})
            return True, "did the thing", ""

        async def fake_overlays(provider, role, workspace, profile_name="dev"):
            return {}, "badge-stub"

        manager._run_process = fake_run_process  # type: ignore[method-assign]
        provider_stub = MagicMock()
        provider_stub.revoke = AsyncMock()
        with patch.object(DevSessionManager, "_resolve_tier_executor",
                          AsyncMock(return_value=MagicMock(
                              argv=["musej", "exec"], prompt_via="argv",
                              cwd="/tmp/repo", env={}, label="tier_c:muse"))), \
             patch.object(DevSessionManager, "_ticket_badge_provider",
                          return_value=provider_stub), \
             patch.object(DevSessionManager, "_muse_role_overlays",
                          AsyncMock(side_effect=fake_overlays)):
            await manager._step_code(_task(), self._config(), CodingTool.TIER_C)
        # _config() points at /tmp/definitely-not-a-repo: nothing to name.
        self.assertTrue(calls[0]["command"][-1].startswith("TAREA"))


if __name__ == "__main__":
    unittest.main()
