"""Auto memory stays off in OpenCEO-launched Claude runs (TASK-2026-00326, D5).

Tier A, Tier C/claudep and every live role on the claude provider carry
``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1``, fail-closed: neither provider env
nor role env can re-enable it. Learnings belong to the project vault,
never to the engine's own memory. No tokens, no subprocesses: executors
resolve against faked availability and the live overlay is captured
before any process spawns.
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agents.dev.session import CodingTool, DevSessionManager
from orchestration.role_claude import (
    CLAUDE_NO_AUTO_MEMORY_ENV,
    claude_no_auto_memory_env,
)


def _manager() -> DevSessionManager:
    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "live"
    return DevSessionManager(settings, MagicMock())


def _config() -> dict:
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
                    "claudep": {"command": ["claudep"],
                                "env": {"CLAUdep_KEEP": "yes"}},
                },
            },
            "providers": {
                "claude": {"command": ["/home/develop/.local/bin/claudej"],
                           "cwd": "/tmp/repo",
                           "env": {"PROVIDER_KEEP": "yes"}},
                "codex": {"command": ["/home/develop/.local/bin/codexf"],
                          "cwd": "/tmp/repo", "env": {}},
                "muse": {"command": ["/home/develop/.local/bin/musej"],
                         "cwd": "/tmp/repo", "env": {}},
            },
        },
    }


class NoAutoMemoryHelperTests(unittest.TestCase):
    def test_kill_switch_value(self) -> None:
        self.assertEqual(CLAUDE_NO_AUTO_MEMORY_ENV,
                         {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})

    def test_extra_keys_survive_beside_switch(self) -> None:
        env = claude_no_auto_memory_env({"PROVIDER_KEEP": "yes"})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(env["PROVIDER_KEEP"], "yes")

    def test_explicit_config_cannot_reenable(self) -> None:
        env = claude_no_auto_memory_env(
            {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")


class TierExecutorNoAutoMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tier_a_carries_switch_over_provider_env(self) -> None:
        executor = await _manager()._resolve_tier_executor(
            CodingTool.TIER_A, _config())
        self.assertEqual(executor.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(executor.env["PROVIDER_KEEP"], "yes")

    async def test_tier_a_carries_switch_with_empty_provider_env(self) -> None:
        config = _config()
        config["dev"]["providers"]["claude"]["env"] = {}
        executor = await _manager()._resolve_tier_executor(
            CodingTool.TIER_A, config)
        self.assertEqual(executor.env, {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})

    async def test_tier_c_claudep_carries_switch_over_link_env(self) -> None:
        manager = _manager()
        manager._rate_monitor = MagicMock()
        manager._rate_monitor.can_launch = AsyncMock(return_value=False)
        with patch.object(DevSessionManager, "_command_available",
                          return_value=False):
            executor = await manager._resolve_tier_executor(
                CodingTool.TIER_C, _config())
        self.assertEqual(executor.label, "tier_c:claudep")
        self.assertEqual(executor.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(executor.env["CLAUdep_KEEP"], "yes")

    async def test_tier_c_dsh_keeps_link_env_untouched(self) -> None:
        manager = _manager()
        manager._rate_monitor = MagicMock()
        manager._rate_monitor.can_launch = AsyncMock(return_value=False)
        with patch.object(DevSessionManager, "_command_available",
                          side_effect=lambda argv: argv[0] == "node"):
            executor = await manager._resolve_tier_executor(
                CodingTool.TIER_C, _config())
        self.assertEqual(executor.label, "tier_c:dsh")
        self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", executor.env)


class LiveClaudeRoleNoAutoMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def _overlay_for(self, role: str, spec: dict) -> dict:
        from orchestration.live import ProcessExecutor

        captured: dict = {}

        def fake_plan_agent(**kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop-before-spawn")

        badge_provider = MagicMock()
        badge_provider.mint = AsyncMock(return_value="badge-stub")
        badge_provider.mcp_servers = MagicMock(return_value={})
        badge_provider.revoke = AsyncMock()
        executor = ProcessExecutor(roles={role: spec},
                                   badge_provider=badge_provider)
        with tempfile.TemporaryDirectory(prefix="no-auto-memory-") as tmp:
            with patch("orchestration.live.plan_agent",
                       side_effect=fake_plan_agent):
                with self.assertRaises(RuntimeError):
                    await executor._run_once(role, "hello", Path(tmp), 60)
        return captured.get("overlay") or {}

    def _claude_spec(self, **overrides) -> dict:
        spec = {
            "provider": "claude",
            "account": "claudej",
            "label": "claudej",
            "model": "opus",
            "effort": "max",
            "active": True,
            "command": ["claudej"],
            "headless_args": ["--print"],
            "prompt_via": "argv",
        }
        spec.update(overrides)
        return spec

    async def test_live_claude_overlay_carries_switch(self) -> None:
        overlay = await self._overlay_for(
            "dev", self._claude_spec(role_profile="dev"))
        self.assertEqual(overlay.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")

    async def test_live_planner_without_profile_carries_switch(self) -> None:
        overlay = await self._overlay_for("planner", self._claude_spec())
        self.assertEqual(overlay.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")

    async def test_live_arbiter_without_profile_carries_switch(self) -> None:
        overlay = await self._overlay_for("arbiter", self._claude_spec())
        self.assertEqual(overlay.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")

    async def test_live_role_env_cannot_reenable(self) -> None:
        overlay = await self._overlay_for(
            "planner", self._claude_spec(
                env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}))
        self.assertEqual(overlay.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY"), "1")

    async def test_live_non_claude_role_untouched(self) -> None:
        overlay = await self._overlay_for(
            "builder", self._claude_spec(provider="codex"))
        self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", overlay)


class SandboxEnvCarriesSwitchTests(unittest.TestCase):
    def test_overlay_reaches_child_env(self) -> None:
        from orchestration.agent_sandbox import sandbox_env

        env = sandbox_env(
            {}, {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                 "KEEP": "yes"})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(env["KEEP"], "yes")


if __name__ == "__main__":
    unittest.main()
