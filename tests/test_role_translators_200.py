"""Role profile translators for OpenCode and Claude Code (TASK-2026-00200).

Equivalence tests: the same engine-neutral profile renders the same card,
rules, skills and badged MCP server for Muse, OpenCode and Claude Code —
each in its own CLI/config surface. Plus the run-engine dispatch in the
live executor (Tier S roles) and the simple-tier executors (tiers A/B).
No live engines here; live verification lands in the epic Acceptance.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import RoleProfile, load_role_profile  # noqa: E402
from mcp_identity import READONLY_BOX  # noqa: E402
from orchestration import live as live_mod  # noqa: E402
from orchestration.live import ProcessExecutor  # noqa: E402
from orchestration.role_claude import (  # noqa: E402
    CLAUDE_BUILTIN_TOOLS,
    CLAUDE_MCP_OVERLAY_RELPATH,
    CLAUDE_MCP_SERVER,
    claude_config_bytes,
    ensure_mcp_placeholder,
    profile_box_tools,
    render_allowed_tools,
    render_argv_extras as claude_argv_extras,
    render_mcp_config,
    render_system_prompt,
)
from orchestration.role_muse import (  # noqa: E402
    RoleProfileError,
    render_card,
)
from orchestration.role_opencode import (  # noqa: E402
    OPENCODE_OVERLAY_RELPATH,
    opencode_config_bytes,
    render_agent_entry,
    render_argv_extras as opencode_argv_extras,
    render_config as opencode_render_config,
    render_mcp_servers as opencode_mcp_servers,
)

REAL_PROFILES = ("dev", "planner", "ceo", "operations", "secretary",
                 "finance", "research")

SERVERS = {"openceo-internal": {
    "type": "http",
    "url": "http://127.0.0.1:18765/internal/mcp/",
    "headers": {"Authorization": "Bearer badge-1"},
}}


def _dev_profile() -> RoleProfile:
    return load_role_profile("dev")


class ClaudeTranslatorTests(unittest.TestCase):
    def test_system_prompt_is_the_role_card(self) -> None:
        for name in REAL_PROFILES:
            with self.subTest(profile=name):
                profile = load_role_profile(name)
                self.assertEqual(render_system_prompt(profile),
                                 render_card(profile))

    def test_allowed_tools_name_box_tools_explicitly(self) -> None:
        box = ("openceo_get_ticket", "openceo_add_ticket_comment")
        rendered = render_allowed_tools(box)
        self.assertNotIn("*", rendered)
        for tool in box:
            self.assertIn(f"mcp__{CLAUDE_MCP_SERVER}__{tool}", rendered)
        for builtin in CLAUDE_BUILTIN_TOOLS:
            self.assertIn(builtin, rendered)

    def test_allowed_tools_stable_order(self) -> None:
        box = ("openceo_get_ticket", "openceo_add_ticket_comment")
        self.assertEqual(render_allowed_tools(box),
                         render_allowed_tools(tuple(reversed(box))))

    def test_argv_extras_carry_card_tools_and_badged_config(self) -> None:
        profile = _dev_profile()
        box = profile_box_tools(profile)
        argv = claude_argv_extras(profile, box, "/home/u/.claude/mcp.json")
        self.assertEqual(argv[0], "--append-system-prompt")
        self.assertEqual(argv[1], render_card(profile))
        self.assertEqual(argv[2], "--allowedTools")
        self.assertIn("mcp__openceo-internal__openceo_get_ticket", argv[3])
        self.assertEqual(argv[4:6],
                         ["--mcp-config", "/home/u/.claude/mcp.json"])
        self.assertEqual(argv[6], "--strict-mcp-config")

    def test_mcp_config_replaces_servers_with_badge(self) -> None:
        config = render_mcp_config(SERVERS)
        self.assertEqual(set(config), {"mcpServers"})
        server = config["mcpServers"]["openceo-internal"]
        self.assertEqual(server["headers"]["Authorization"],
                         "Bearer badge-1")

    def test_config_bytes_are_json(self) -> None:
        payload = json.loads(claude_config_bytes(SERVERS).decode("utf-8"))
        self.assertEqual(payload["mcpServers"], SERVERS)

    def test_ensure_placeholder_creates_0600_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir()
            path = ensure_mcp_placeholder(home)
            self.assertEqual(path, home / CLAUDE_MCP_OVERLAY_RELPATH)
            self.assertTrue(path.is_file())
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_ensure_placeholder_refuses_a_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as tmp:
            home = Path(tmp)
            target = home / CLAUDE_MCP_OVERLAY_RELPATH
            target.parent.mkdir(parents=True)
            target.mkdir()
            with self.assertRaises(RoleProfileError):
                ensure_mcp_placeholder(home)

    def test_box_tools_come_from_the_card(self) -> None:
        box = profile_box_tools(_dev_profile())
        self.assertEqual(set(box), {
            "openceo_search_tickets", "openceo_get_ticket",
            "openceo_add_ticket_comment", "openceo_attach_ticket_file"})

    def test_box_tools_readonly_principals(self) -> None:
        for name in ("planner", "ceo"):
            with self.subTest(profile=name):
                self.assertEqual(set(profile_box_tools(
                    load_role_profile(name))), set(READONLY_BOX))

    def test_box_tools_unknown_agent_refused(self) -> None:
        profile = _dev_profile().model_copy(update={"agent": "nope"})
        with self.assertRaises(RoleProfileError):
            profile_box_tools(profile)


class OpenCodeTranslatorTests(unittest.TestCase):
    def test_agent_entry_carries_card_rules_and_skills(self) -> None:
        profile = load_role_profile("operations")
        entry = render_agent_entry(profile)
        self.assertEqual(entry["mode"], "primary")
        self.assertIn(render_card(profile), entry["prompt"])
        self.assertIn("AGENTS.md", entry["prompt"])
        self.assertIn("crear-proyecto", entry["prompt"])
        self.assertEqual(entry["permission"]["skill"],
                         {"create-skill": "allow",
                          "crear-proyecto": "allow"})

    def test_agent_entry_without_skills_denies_skill_tool(self) -> None:
        profile = _dev_profile().model_copy(update={"skills": []})
        entry = render_agent_entry(profile)
        self.assertEqual(entry["permission"]["skill"], "deny")

    def test_agent_entry_model_optional(self) -> None:
        entry = render_agent_entry(_dev_profile())
        self.assertNotIn("model", entry)
        entry = render_agent_entry(_dev_profile(), model="m/n")
        self.assertEqual(entry["model"], "m/n")

    def test_mcp_servers_translate_to_remote_with_bearer(self) -> None:
        servers = opencode_mcp_servers(SERVERS)
        server = servers["openceo-internal"]
        self.assertEqual(server["type"], "remote")
        self.assertEqual(server["url"], SERVERS["openceo-internal"]["url"])
        self.assertTrue(server["enabled"])
        self.assertEqual(server["headers"]["Authorization"],
                         "Bearer badge-1")

    def test_mcp_servers_without_url_refused(self) -> None:
        with self.assertRaises(RoleProfileError):
            opencode_mcp_servers({"openceo-internal": {"type": "http"}})

    def test_render_config_replaces_agent_and_mcp_only(self) -> None:
        profile = _dev_profile()
        global_config = {"$schema": "https://opencode.ai/config.json",
                         "permission": "allow",
                         "agent": {"build": {"mode": "primary"}},
                         "mcp": {"other": {"type": "local"}}}
        rendered = opencode_render_config(profile, global_config, SERVERS)
        self.assertEqual(rendered["permission"], "allow")
        self.assertEqual(rendered["agent"]["build"], {"mode": "primary"})
        self.assertIn("dev", rendered["agent"])
        self.assertEqual(set(rendered["mcp"]), {"openceo-internal"})
        self.assertEqual(global_config["mcp"], {"other": {"type": "local"}})

    def test_argv_selects_the_role_agent(self) -> None:
        self.assertEqual(opencode_argv_extras("operations"),
                         ["--agent", "operations"])

    def test_config_bytes_read_global_and_render(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as tmp:
            home = Path(tmp)
            target = home / OPENCODE_OVERLAY_RELPATH
            target.parent.mkdir(parents=True)
            target.write_text('{"permission": "allow"}', encoding="utf-8")
            payload = json.loads(opencode_config_bytes(
                _dev_profile(), home, SERVERS).decode("utf-8"))
        self.assertEqual(payload["permission"], "allow")
        self.assertIn("dev", payload["agent"])
        self.assertEqual(set(payload["mcp"]), {"openceo-internal"})

    def test_config_bytes_refuse_unreadable_global(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as tmp:
            with self.assertRaises(RoleProfileError):
                opencode_config_bytes(_dev_profile(), Path(tmp), SERVERS)


class TranslatorEquivalenceTests(unittest.TestCase):
    def test_same_card_everywhere(self) -> None:
        for name in REAL_PROFILES:
            with self.subTest(profile=name):
                profile = load_role_profile(name)
                card = render_card(profile)
                self.assertEqual(render_system_prompt(profile), card)
                self.assertIn(card, render_agent_entry(profile)["prompt"])

    def test_same_skills_everywhere(self) -> None:
        for name in REAL_PROFILES:
            with self.subTest(profile=name):
                profile = load_role_profile(name)
                permission = render_agent_entry(profile)["permission"]
                if profile.skills:
                    self.assertEqual(set(permission["skill"]),
                                     set(profile.skills))
                else:
                    self.assertEqual(permission["skill"], "deny")

    def test_same_badge_everywhere(self) -> None:
        profile = _dev_profile()
        bearer = "Bearer badge-1"
        claude = json.loads(claude_config_bytes(SERVERS).decode("utf-8"))
        self.assertEqual(
            claude["mcpServers"]["openceo-internal"]["headers"]
            ["Authorization"], bearer)
        opencode = opencode_mcp_servers(SERVERS)
        self.assertEqual(
            opencode["openceo-internal"]["headers"]["Authorization"],
            bearer)


class LiveDispatchTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self):
        provider = MagicMock()
        provider.mint = AsyncMock(return_value="badge-1")
        provider.mcp_servers = MagicMock(return_value=dict(SERVERS))
        provider.revoke = AsyncMock()
        return provider

    async def _run(self, spec, provider=None):
        from research.runtimes.base import IsolationUnavailable
        seen: dict[str, object] = {}

        def fake_plan(*, argv, workspace, base, overlay, config_args,
                      home_file_overlays=None, extra_read_roots=(),
                      extra_write_roots=(), extra_ops=()):
            seen["config_args"] = list(config_args)
            seen["overlays"] = dict(home_file_overlays or {})
            raise IsolationUnavailable("no bwrap here")

        executor = ProcessExecutor(
            roles={"builder": spec},
            badge_provider=provider or self._provider())
        with patch.object(live_mod, "plan_agent",
                          side_effect=fake_plan), \
             tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            result = await executor.run("builder", "hi", Path(tmp), 5)
        return result, seen

    async def test_claude_provider_renders_claude(self) -> None:
        spec = {"provider": "claude", "command": ["claudej"],
                "headless_args": ["--print"], "prompt_via": "argv",
                "role_profile": "dev"}
        with tempfile.TemporaryDirectory(prefix="home-") as home:
            (Path(home) / ".claude").mkdir()
            with patch("os.path.expanduser", return_value=home):
                result, seen = await self._run(spec)
        self.assertIn("sandbox refused", result.output_tail)
        overlays = seen["overlays"]
        assert isinstance(overlays, dict)
        self.assertEqual(set(overlays), {CLAUDE_MCP_OVERLAY_RELPATH})
        payload = json.loads(next(iter(overlays.values())).decode("utf-8"))
        self.assertEqual(payload["mcpServers"], SERVERS)
        config_args = seen["config_args"]
        assert isinstance(config_args, list)
        self.assertIn("--append-system-prompt", config_args)
        self.assertIn("--strict-mcp-config", config_args)
        allowed = config_args[config_args.index("--allowedTools") + 1]
        self.assertIn("mcp__openceo-internal__openceo_get_ticket", allowed)
        mcp_path = config_args[config_args.index("--mcp-config") + 1]
        self.assertTrue(mcp_path.endswith(CLAUDE_MCP_OVERLAY_RELPATH))

    async def test_opencode_provider_renders_opencode(self) -> None:
        spec = {"provider": "opencode",
                "command": ["opencode", "run"],
                "prompt_via": "argv", "role_profile": "operations"}
        with tempfile.TemporaryDirectory(prefix="home-") as home:
            target = Path(home) / OPENCODE_OVERLAY_RELPATH
            target.parent.mkdir(parents=True)
            target.write_text('{"permission": "allow"}', encoding="utf-8")
            with patch("os.path.expanduser", return_value=home):
                result, seen = await self._run(spec)
        self.assertIn("sandbox refused", result.output_tail)
        overlays = seen["overlays"]
        assert isinstance(overlays, dict)
        self.assertEqual(set(overlays), {OPENCODE_OVERLAY_RELPATH})
        payload = json.loads(next(iter(overlays.values())).decode("utf-8"))
        self.assertIn("operations", payload["agent"])
        self.assertEqual(set(payload["mcp"]), {"openceo-internal"})
        config_args = seen["config_args"]
        assert isinstance(config_args, list)
        self.assertIn("--agent", config_args)
        self.assertIn("operations", config_args)

    async def test_codex_provider_renders_override(self) -> None:
        # TASK-2026-00246 redefines this: codex has a translator now
        # (badged MCP via -c override, Tier B pattern), so the run
        # renders instead of refusing.
        spec = {"provider": "codex", "command": ["codexj", "exec"],
                "prompt_via": "stdin", "role_profile": "dev"}
        result, seen = await self._run(spec)
        self.assertIn("sandbox refused", result.output_tail)
        config_args = seen["config_args"]
        assert isinstance(config_args, list)
        self.assertIn("-c", config_args)
        override = config_args[config_args.index("-c") + 1]
        self.assertIn("mcp_servers.openceo-internal.bearer_token_env_var",
                      override)
        self.assertEqual({}, seen["overlays"])


class TierExecutorTranslatorTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        from agents.dev.session import DevSessionManager
        settings = MagicMock()
        settings.DEV_EXECUTION_MODE = "live"
        return DevSessionManager(settings, MagicMock())

    def _config(self, tier_a=None, tier_b=None) -> dict:
        return {
            "project": {"repos": [{"local_path": "/tmp/no-repo"}]},
            "dev": {
                "tiers": {
                    "tier_a": {"account": "claudej", **(tier_a or {})},
                    "tier_b": {"account": "codexj", **(tier_b or {})},
                },
                "providers": {
                    "claude": {"command": ["claudej"], "cwd": "/tmp/repo",
                               "env": {}},
                    "codex": {"command": ["codexj"], "cwd": "/tmp/repo",
                              "env": {}},
                },
            },
        }

    async def test_tier_a_profile_renders_claude(self) -> None:
        from agents.dev.session import CodingTool
        config = self._config(tier_a={"role_profile": "dev"})
        executor = await self._manager()._resolve_tier_executor(
            CodingTool.TIER_A, config)
        self.assertEqual(executor.label, "tier_a")
        self.assertEqual(executor.role_profile, "dev")
        self.assertIn("--append-system-prompt", executor.argv)
        self.assertIn("--strict-mcp-config", executor.argv)
        allowed = executor.argv[executor.argv.index("--allowedTools") + 1]
        self.assertIn("mcp__openceo-internal__openceo_get_ticket", allowed)

    async def test_tier_b_profile_carries_mcp_token_override(self) -> None:
        from agents.dev.session import CodingTool, TIER_CODEX_TOKEN_ENV
        config = self._config(tier_b={"role_profile": "dev"})
        executor = await self._manager()._resolve_tier_executor(
            CodingTool.TIER_B, config)
        self.assertEqual(executor.label, "tier_b")
        self.assertEqual(executor.role_profile, "dev")
        self.assertIn("-c", executor.argv)
        override = executor.argv[executor.argv.index("-c") + 1]
        self.assertIn("mcp_servers.openceo-internal."
                      "bearer_token_env_var", override)
        self.assertIn(TIER_CODEX_TOKEN_ENV, override)
        self.assertEqual(executor.argv[-1], "-")

    async def test_global_tiers_pin_dev_profile(self) -> None:
        from config_loader import ConfigLoader
        from settings import Settings
        with tempfile.TemporaryDirectory(prefix="proj-") as tmp:
            loader = ConfigLoader(
                Settings(_env_file=None, PROJECTS_DIR=str(tmp)))
            tiers = loader.load_project("anything")["dev"]["tiers"]
        self.assertEqual(tiers["tier_a"]["role_profile"], "dev")
        self.assertEqual(tiers["tier_b"]["role_profile"], "dev")


class PlaceholderRaceTests(unittest.TestCase):
    def test_cold_start_race_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as tmp:
            home = Path(tmp)
            target = home / CLAUDE_MCP_OVERLAY_RELPATH
            target.parent.mkdir(parents=True)
            target.write_text("", encoding="utf-8")
            with patch.object(Path, "exists", return_value=False):
                path = ensure_mcp_placeholder(home)
            self.assertEqual(path, target)
            self.assertTrue(path.is_file())


class LiveRevokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_render_error_still_revokes(self) -> None:
        provider = MagicMock()
        provider.mint = AsyncMock(return_value="badge-1")
        provider.mcp_servers = MagicMock(return_value=dict(SERVERS))
        provider.revoke = AsyncMock()
        spec = {"provider": "claude", "command": ["claudej"],
                "headless_args": ["--print"], "prompt_via": "argv",
                "role_profile": "dev"}
        executor = ProcessExecutor(roles={"builder": spec},
                                   badge_provider=provider)
        with patch.object(live_mod, "profile_box_tools",
                          side_effect=RuntimeError("boom")), \
             tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with self.assertRaises(RuntimeError):
                await executor.run("builder", "hi", Path(tmp), 5)
        provider.revoke.assert_awaited_once_with("badge-1")


class FollowUpParityTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        from agents.dev.session import DevSessionManager
        settings = MagicMock()
        settings.DEV_EXECUTION_MODE = "live"
        return DevSessionManager(settings, MagicMock())

    def _provider(self):
        provider = MagicMock()
        provider.mint = AsyncMock(return_value="badge-x")
        provider.mcp_servers = MagicMock(return_value=dict(SERVERS))
        provider.revoke = AsyncMock()
        return provider

    async def _followup(self, executor, provider):
        from agents.dev.session import CodingTool
        manager = self._manager()
        seen: dict[str, object] = {}
        tool = CodingTool.TIER_A if executor.label == "tier_a" \
            else CodingTool.TIER_B

        async def fake_run(command, stdin_payload=None, timeout=120,
                           cwd=None, env=None, sandbox=True,
                           config_argv=None, home_file_overlays=None,
                           extra_read_roots=()):
            seen["command"] = list(command)
            seen["env"] = env
            seen["overlays"] = home_file_overlays
            return True, "ok", ""

        manager._run_process = fake_run  # type: ignore[method-assign]
        manager._resolve_tier_executor = AsyncMock(  # type: ignore[method-assign]
            return_value=executor)
        manager._ticket_badge_provider = MagicMock(  # type: ignore[method-assign]
            return_value=provider)
        with tempfile.TemporaryDirectory(prefix="repo-") as tmp:
            repo = Path(tmp)
            (repo / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            with patch("agents.dev.session.ensure_mcp_placeholder",
                       return_value=repo / "mcp.json"):
                result = await manager._simple_followup(
                    tool, {}, repo, "Fix the code.",
                    task_id="t", project="p",
                    badge_ticket="TASK-2026-00001")
        return result, seen

    async def test_tier_a_followup_carries_overlay(self) -> None:
        from agents.dev.session import TierExecutor
        executor = TierExecutor(
            argv=["claudej", "--print"], prompt_via="argv", cwd=None,
            env={}, label="tier_a", role_profile="dev")
        provider = self._provider()
        result, seen = await self._followup(executor, provider)
        self.assertIsNotNone(result)
        overlays = seen["overlays"]
        assert isinstance(overlays, dict)
        self.assertEqual(set(overlays), {CLAUDE_MCP_OVERLAY_RELPATH})
        payload = json.loads(next(iter(overlays.values())).decode("utf-8"))
        self.assertEqual(payload["mcpServers"], SERVERS)
        provider.revoke.assert_awaited_once_with("badge-x")

    async def test_tier_b_followup_carries_token_env(self) -> None:
        from agents.dev.session import TIER_CODEX_TOKEN_ENV, TierExecutor
        executor = TierExecutor(
            argv=["codexj", "exec", "--ephemeral", "-"],
            prompt_via="stdin", cwd=None, env={}, label="tier_b",
            role_profile="dev")
        provider = self._provider()
        result, seen = await self._followup(executor, provider)
        self.assertIsNotNone(result)
        self.assertIsNone(seen["overlays"])
        self.assertEqual(seen["env"][TIER_CODEX_TOKEN_ENV], "badge-x")  # type: ignore[index]
        provider.revoke.assert_awaited_once_with("badge-x")

    async def test_tier_a_followup_without_repo_refused(self) -> None:
        from agents.dev.session import TierExecutor
        executor = TierExecutor(
            argv=["claudej", "--print"], prompt_via="argv", cwd=None,
            env={}, label="tier_a", role_profile="dev")
        result, _seen = await self._followup(executor, None)
        self.assertIsNone(result)

    async def test_claude_overlays_refuse_non_dev_profile(self) -> None:
        manager = self._manager()
        provider = self._provider()
        with self.assertRaises(RoleProfileError):
            await manager._claude_role_overlays(  # noqa: SLF001
                provider, "tier_a", "operations")
        provider.mint.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
