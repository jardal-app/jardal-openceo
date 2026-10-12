"""Role profiles translated for Muse (TASK-2026-00197).

Loader + translator + wiring unit tests run everywhere. The echo smoke
tests at the end need the pinned binary, the live service and local
Mongo, and skip loudly without them; they measure prompt bytes before
and after the profile, exactly as the ticket acceptance requires.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

REPO = Path(__file__).resolve().parents[1]

from catalog import (  # noqa: E402
    CatalogError,
    load_catalog,
    load_role_profile,
)
from orchestration import agent_sandbox, live as live_mod  # noqa: E402
from orchestration.live import ProcessExecutor  # noqa: E402
from orchestration.role_muse import (  # noqa: E402
    MUSE_PINNED_VERSION,
    SETTINGS_OVERLAY_RELPATH,
    ObjectiveBadgeProvider,
    ReadonlyBadgeProvider,
    RoleProfileError,
    ServiceBadgeProvider,
    TicketBadgeProvider,
    check_pinned_version,
    dev_box_tools,
    enumerate_skills,
    issuer_for_repo,
    load_global_settings,
    measure_session_log,
    render_activation,
    render_argv_extras,
    render_card,
    render_card_argv,
    render_settings,
    resolve_muse_bin,
    settings_overlay_bytes,
    strip_echo_incompatible,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _minimal_catalog(root: Path) -> Path:
    base = root / "catalog"
    _write(base / "index.yaml",
           "version: 1\nagents:\n"
           "  - name: dev\n"
           "    summary: Writes repos. Never decides business work.\n"
           "    card: agents/dev.yaml\n")
    _write(base / "agents" / "dev.yaml",
           "name: dev\ntype: ai\nengine: muse-code\n"
           "profile: profiles/dev.yaml\n"
           "boundary_tools: []\nskills_dir: skills/dev\nskills: []\n"
           "inputs: []\noutputs: []\nenabled: true\n"
           "disabled_reason: null\ndecided_by_ticket: null\n")
    _write(base / "staff.yaml", "roster: []\n")
    return base


def _dev_profile_text(**overrides: str) -> str:
    fields = {
        "name": "dev",
        "agent": "dev",
        "engine": "muse-code",
        "who": "Company dev agent",
        "rules": "AGENTS.md",
        "skills": "[]",
        "output_schema": "null",
    }
    fields.update(overrides)
    return (
        f"name: {fields['name']}\nagent: {fields['agent']}\n"
        f"engine: {fields['engine']}\ncard:\n"
        f"  who: {fields['who']}\n"
        "  delivers:\n    - branch changes\n"
        "  never:\n    - decide business work\n"
        f"rules: {fields['rules']}\nskills: {fields['skills']}\n"
        f"output_schema: {fields['output_schema']}\n"
    )


class RoleProfileLoaderTests(unittest.TestCase):
    def test_real_profiles_load(self) -> None:
        for name in ("dev", "planner", "ceo", "operations", "secretary",
                     "finance", "research"):
            with self.subTest(profile=name):
                profile = load_role_profile(name)
                self.assertEqual(profile.name, name)
                self.assertEqual(profile.agent, name)
                self.assertEqual(profile.engine, "muse-code")
                self.assertTrue(profile.card.who)
                self.assertTrue(profile.card.delivers)
                self.assertTrue(profile.card.never)

    def test_real_catalog_validates_card_linkage(self) -> None:
        catalog = load_catalog()
        for name in ("dev", "operations", "secretary", "finance",
                     "research"):
            with self.subTest(card=name):
                self.assertEqual(catalog.agents[name].profile,
                                 f"profiles/{name}.yaml")

    def test_unknown_profile_refused(self) -> None:
        with self.assertRaises(CatalogError):
            load_role_profile("no-such-role")

    def test_escaping_profile_name_refused(self) -> None:
        with self.assertRaises(CatalogError):
            load_role_profile("../agents/dev")

    def test_missing_pointed_profile_fails_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catprof-") as tmp:
            base = _minimal_catalog(Path(tmp))
            with self.assertRaisesRegex(CatalogError, "missing"):
                load_catalog(base)

    def test_corrupt_profile_fails_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catprof-") as tmp:
            base = _minimal_catalog(Path(tmp))
            _write(base / "profiles" / "dev.yaml", "name: [unclosed\n")
            with self.assertRaises(CatalogError):
                load_catalog(base)

    def test_profile_name_mismatch_fails_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catprof-") as tmp:
            base = _minimal_catalog(Path(tmp))
            _write(base / "profiles" / "dev.yaml",
                   _dev_profile_text(name="other", agent="other"))
            with self.assertRaisesRegex(CatalogError, "must match"):
                load_catalog(base)

    def test_profile_skills_must_be_on_card(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catprof-") as tmp:
            base = _minimal_catalog(Path(tmp))
            _write(base / "profiles" / "dev.yaml",
                   _dev_profile_text(skills="[nope]"))
            with self.assertRaisesRegex(CatalogError, "not on the card"):
                load_catalog(base)

    def test_profile_engine_must_match_card(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catprof-") as tmp:
            base = _minimal_catalog(Path(tmp))
            text = _dev_profile_text().replace("engine: muse-code",
                                               "engine: nope-engine")
            _write(base / "profiles" / "dev.yaml", text)
            with self.assertRaises(CatalogError):
                load_catalog(base)

    def test_minimal_catalog_without_profiles_dir_passes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catprof-") as tmp:
            base = _minimal_catalog(Path(tmp))
            card = (base / "agents" / "dev.yaml").read_text()
            (base / "agents" / "dev.yaml").write_text(
                card.replace("profile: profiles/dev.yaml",
                             "profile: null"))
            catalog = load_catalog(base)
            self.assertIsNone(catalog.agents["dev"].profile)

    def test_profile_extra_keys_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catprof-") as tmp:
            base = _minimal_catalog(Path(tmp))
            _write(base / "profiles" / "dev.yaml",
                   _dev_profile_text() + "skill: [typo]\n")
            with self.assertRaises(CatalogError):
                load_catalog(base)

    def test_profile_paths_are_repo_relative(self) -> None:
        for name in ("dev", "operations", "secretary", "finance",
                     "research"):
            with self.subTest(profile=name):
                profile = load_role_profile(name)
                assert profile.output_schema is not None
                target = REPO / profile.output_schema
                self.assertTrue(target.is_file(),
                                f"{profile.output_schema} must exist")
                self.assertTrue((REPO / profile.rules).is_file())


class TranslatorTests(unittest.TestCase):
    def test_card_names_who_delivers_never(self) -> None:
        profile = load_role_profile("dev")
        card = render_card(profile)
        self.assertIn("Role: dev.", card)
        self.assertIn(profile.card.who, card)
        for item in (*profile.card.delivers, *profile.card.never):
            self.assertIn(item, card)

    def test_card_stays_small(self) -> None:
        for name in ("dev", "planner", "ceo", "operations", "secretary",
                     "finance", "research"):
            with self.subTest(profile=name):
                self.assertLessEqual(
                    len(render_card(load_role_profile(name)).encode()),
                    512, "role cards must stay small for the 60% budget")

    def test_argv_extras_switch_off_engine_extras(self) -> None:
        extras = render_argv_extras(load_role_profile("dev"))
        self.assertIn("--disable-reminders", extras)
        self.assertIn("--no-foreign-personal-context", extras)
        self.assertIn("--no-session-log", extras)
        joined = " ".join(extras)
        self.assertIn('run.workflow_trigger_mode="off"', joined)
        self.assertIn('run.subagent_delegation_mode="off"', joined)
        self.assertNotIn("--model", extras)

    def test_card_argv_carries_developer_prompt(self) -> None:
        argv = render_card_argv(load_role_profile("dev"))
        self.assertEqual(argv[0], "-c")
        self.assertTrue(argv[1].startswith("run.developer_prompt="))

    def test_strip_echo_incompatible(self) -> None:
        argv = ["musej", "exec", "--model", "x", "--reasoning-effort", "max",
               "--output-schema", "/tmp/s.json", "--effort=x",
               "--disable-reminders", "prompt"]
        self.assertEqual(strip_echo_incompatible(argv),
                         ["musej", "exec", "--disable-reminders", "prompt"])

    def test_resolve_muse_bin_missing(self) -> None:
        with patch("orchestration.role_muse.shutil.which",
                   return_value=None):
            with self.assertRaises(RoleProfileError):
                resolve_muse_bin()

    def test_check_pinned_version_ok_and_drift(self) -> None:
        proc = MagicMock(returncode=0,
                         stdout=f"Muse Code 1.4.6 ({MUSE_PINNED_VERSION})\n")
        with patch("orchestration.role_muse.subprocess.run",
                   return_value=proc) as runner:
            out = check_pinned_version("/bin/muse")
            self.assertIn(MUSE_PINNED_VERSION, out)
            _, kwargs = runner.call_args
            self.assertEqual(kwargs["env"]["MUSE_NO_AUTO_UPDATE"], "1")
        proc = MagicMock(returncode=0, stdout="Muse Code 9.9.9 (other)\n")
        with patch("orchestration.role_muse.subprocess.run",
                   return_value=proc):
            with self.assertRaisesRegex(RoleProfileError, "drift"):
                check_pinned_version("/bin/muse")

    def test_enumerate_skills_maps_scopes(self) -> None:
        payload = {"skills": [
            {"id": "bundled:git", "scope": "bundled", "path": "built-in"},
            {"id": "agent-browser", "scope": "user",
             "path": "$HOME/.agents/skills/agent-browser/SKILL.md"},
            {"id": "plugin:threejs:threejs", "scope": "plugin",
             "path": "plugin://threejs/skills/threejs/SKILL.md"},
            {"id": "notion-api-local", "scope": "project",
             "path": ".codex/skills/notion-api-local/SKILL.md"},
        ]}
        proc = MagicMock(returncode=0, stdout=json.dumps(payload))
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with patch("orchestration.role_muse.subprocess.run",
                       return_value=proc) as runner:
                off = enumerate_skills(tmp, muse_bin="/bin/muse")
            argv = runner.call_args[0][0]
            self.assertIn("--trust-workspace", argv)
            self.assertIn(tmp, argv)
        self.assertEqual(
            off["bundled"], ["bundled://muse-core/skills/git/SKILL.md"])
        self.assertEqual(
            off["user"], ["$HOME/.agents/skills/agent-browser/SKILL.md"])
        self.assertEqual(
            off["plugin"], ["plugin://threejs/skills/threejs/SKILL.md"])
        ws = str(Path(tmp).resolve())
        self.assertEqual(off["projects"][ws],
                         [{"id": "notion-api-local",
                           "path": ".codex/skills/notion-api-local/SKILL.md"}])

    def test_enumerate_skills_refuses_unknown_scope(self) -> None:
        payload = {"skills": [{"id": "x", "scope": "future",
                               "path": "???"}]}
        proc = MagicMock(returncode=0, stdout=json.dumps(payload))
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with patch("orchestration.role_muse.subprocess.run",
                       return_value=proc):
                with self.assertRaisesRegex(RoleProfileError, "future"):
                    enumerate_skills(tmp, muse_bin="/bin/muse")

    def test_enumerate_skills_refuses_bad_exit(self) -> None:
        proc = MagicMock(returncode=1, stdout="", stderr="boom")
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with patch("orchestration.role_muse.subprocess.run",
                       return_value=proc):
                with self.assertRaises(RoleProfileError):
                    enumerate_skills(tmp, muse_bin="/bin/muse")

    def test_enumerate_skills_refuses_non_object(self) -> None:
        proc = MagicMock(returncode=0, stdout="[1, 2]")
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with patch("orchestration.role_muse.subprocess.run",
                       return_value=proc):
                with self.assertRaises(RoleProfileError):
                    enumerate_skills(tmp, muse_bin="/bin/muse")

    def test_render_activation_switches_all_off(self) -> None:
        off = {"bundled": ["bundled://muse-core/skills/git/SKILL.md"],
               "user": ["$HOME/.agents/skills/a/SKILL.md"],
               "plugin": [], "projects": {"/ws": [
                   {"id": "n", "path": ".codex/skills/n/SKILL.md"}]}}
        activation = render_activation(off)
        self.assertEqual(
            activation["bundled"],
            {"bundled://muse-core/skills/git/SKILL.md": "off"})
        self.assertEqual(
            activation["user"],
            {"$HOME/.agents/skills/a/SKILL.md": "off"})
        self.assertNotIn("plugin", activation)
        self.assertEqual(activation["projects"]["/ws"],
                         {".codex/skills/n/SKILL.md": "off"})

    def test_render_activation_keeps_allowed(self) -> None:
        off = {"bundled": ["bundled://muse-core/skills/git/SKILL.md"],
               "user": [], "plugin": [], "projects": {}}
        self.assertEqual(render_activation(off, allowed=["git"]), {})

    def test_render_settings_empty_map_drops_owner_servers(self) -> None:
        profile = load_role_profile("dev")
        out = render_settings(profile,
                              {"mcpServers": {"personal": {"url": "u"}}},
                              {}, None)
        self.assertEqual(out["mcpServers"], {})

    def test_render_settings_replaces_mcp_and_merges_skills(self) -> None:
        profile = load_role_profile("dev")
        global_settings = {
            "run": {"workflow_trigger_mode": "auto"},
            "mcpServers": {"personal": {"url": "http://x/"}},
            "skills": {"activation": {"user": {"u": "off"}}},
        }
        servers = {"openceo-internal": {"url": "http://i/",
                                       "headers": {"Authorization": "B"}}}
        activation = {"bundled": {"b": "off"}}
        out = render_settings(profile, global_settings, servers,
                              activation)
        self.assertEqual(out["run"]["workflow_trigger_mode"], "off")
        self.assertEqual(out["run"]["subagent_delegation_mode"], "off")
        self.assertIn("Role: dev.", out["run"]["developer_prompt"])
        self.assertEqual(list(out["mcpServers"]), ["openceo-internal"])
        self.assertEqual(out["skills"]["activation"]["user"], {"u": "off"})
        self.assertEqual(out["skills"]["activation"]["bundled"],
                         {"b": "off"})
        self.assertEqual(global_settings["run"]["workflow_trigger_mode"],
                         "auto")

    def test_load_global_settings_refuses_garbage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as tmp:
            with self.assertRaises(RoleProfileError):
                load_global_settings(tmp)
            home = Path(tmp)
            _write(home / SETTINGS_OVERLAY_RELPATH, "{nope")
            with self.assertRaises(RoleProfileError):
                load_global_settings(tmp)

    def test_settings_overlay_bytes_composes(self) -> None:
        profile = load_role_profile("dev")
        with tempfile.TemporaryDirectory(prefix="home-") as tmp:
            _write(Path(tmp) / SETTINGS_OVERLAY_RELPATH,
                   json.dumps({"run": {}, "mcpServers": {}}))
            off = {"bundled": ["bundled://muse-core/skills/g/SKILL.md"],
                   "user": [], "plugin": [], "projects": {}}
            with patch("orchestration.role_muse.check_pinned_version",
                       return_value="ok"), \
                 patch("orchestration.role_muse.enumerate_skills",
                       return_value=off):
                raw = settings_overlay_bytes(
                    profile, tmp, tmp, {"openceo-internal": {"url": "u"}})
        data = json.loads(raw.decode())
        self.assertEqual(data["run"]["workflow_trigger_mode"], "off")
        self.assertIn("bundled://muse-core/skills/g/SKILL.md",
                      data["skills"]["activation"]["bundled"])
        self.assertEqual(list(data["mcpServers"]), ["openceo-internal"])


class BadgeProviderTests(unittest.IsolatedAsyncioTestCase):
    def _issuer(self):
        issuer = MagicMock()
        issuer.issue_agent = AsyncMock(return_value="badge-agent")
        issuer.issue_readonly = AsyncMock(return_value="badge-ro")
        issuer.issue_service = AsyncMock(return_value="badge-svc")
        issuer.revoke = AsyncMock(return_value=True)
        return issuer

    def test_issuer_for_repo_none_when_unbound(self) -> None:
        self.assertIsNone(issuer_for_repo(None))
        self.assertIsNone(issuer_for_repo(MagicMock(db=None)))

    async def test_ticket_provider_mints_agent_and_revokes(self) -> None:
        issuer = self._issuer()
        provider = TicketBadgeProvider(issuer, MagicMock(), "dev",
                                       "TASK-2026-00197", "proj")
        badge = await provider.mint("builder")
        self.assertEqual(badge, "badge-agent")
        issuer.issue_agent.assert_awaited_once_with(
            "dev", "TASK-2026-00197", "proj", None)
        await provider.revoke(badge)
        issuer.revoke.assert_awaited_once()

    async def test_revoke_never_raises(self) -> None:
        issuer = self._issuer()
        issuer.revoke = AsyncMock(side_effect=RuntimeError("mongo down"))
        provider = TicketBadgeProvider(issuer, MagicMock(), "dev",
                                       "TASK-2026-00197", "proj")
        await provider.revoke("badge-agent")

    async def test_readonly_provider_mints_principal(self) -> None:
        issuer = self._issuer()
        provider = ReadonlyBadgeProvider(issuer, MagicMock(), "planner")
        self.assertEqual(await provider.mint("objective_planner"),
                         "badge-ro")
        issuer.issue_readonly.assert_awaited_once_with("planner")

    async def test_service_provider_mints_with_tools(self) -> None:
        issuer = self._issuer()
        provider = ServiceBadgeProvider(issuer, MagicMock(), "closer",
                                        "proj", ["a", "b"])
        self.assertEqual(await provider.mint("closer"), "badge-svc")
        issuer.issue_service.assert_awaited_once_with(
            "closer", "proj", ["a", "b"], None)

    async def test_objective_provider_branches_on_role(self) -> None:
        issuer = self._issuer()
        provider = ObjectiveBadgeProvider(issuer, MagicMock(), "proj",
                                          ticket="TASK-2026-00001")
        self.assertEqual(await provider.mint("objective_planner"),
                         "badge-ro")
        self.assertEqual(await provider.mint("closer"), "badge-agent")
        issuer.issue_agent.assert_awaited_once_with(
            "dev", "TASK-2026-00001", "proj", None)
        with self.assertRaises(RoleProfileError):
            await provider.mint("arbiter")

    async def test_objective_closer_without_ticket_uses_dev_box(
            self) -> None:
        issuer = self._issuer()
        provider = ObjectiveBadgeProvider(issuer, MagicMock(), "proj")
        self.assertEqual(await provider.mint("closer"), "badge-svc")
        args = issuer.issue_service.await_args[0]
        self.assertEqual(args[:2], ("closer", "proj"))
        self.assertEqual(set(args[2]), set(dev_box_tools()))

    def test_dev_box_tools_come_from_catalog(self) -> None:
        self.assertIn("openceo_search_tickets", dev_box_tools())


class LiveExecutorProfileTests(unittest.IsolatedAsyncioTestCase):
    def _roles(self):
        return {"builder": {"command": ["musej", "exec"],
                            "prompt_via": "argv",
                            "role_profile": "dev"}}

    async def test_profile_without_provider_refused(self) -> None:
        executor = ProcessExecutor(roles=self._roles())
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            result = await executor.run("builder", "hi", Path(tmp), 5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("badge provider", result.output_tail)

    async def test_unknown_profile_refused(self) -> None:
        provider = MagicMock()
        provider.mint = AsyncMock(return_value="b")
        provider.revoke = AsyncMock()
        roles = {"builder": {"command": ["musej", "exec"],
                             "prompt_via": "argv",
                             "role_profile": "nope"}}
        executor = ProcessExecutor(roles=roles, badge_provider=provider)
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            result = await executor.run("builder", "hi", Path(tmp), 5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("role profile refused", result.output_tail)
        provider.mint.assert_not_awaited()

    async def test_profile_mints_renders_reuses_and_revokes(self) -> None:
        from research.runtimes.base import IsolationUnavailable
        provider = MagicMock()
        provider.mint = AsyncMock(return_value="badge-1")
        provider.mcp_servers = MagicMock(return_value={"s": {}})
        provider.revoke = AsyncMock()
        seen: dict[str, object] = {}

        def fake_plan(*, argv, workspace, base, overlay, config_args,
                      home_file_overlays=None, extra_read_roots=(),
                      extra_write_roots=()):
            seen["config_args"] = list(config_args)
            seen["overlays"] = home_file_overlays
            seen["extra_read_roots"] = tuple(extra_read_roots)
            seen["extra_write_roots"] = tuple(extra_write_roots)
            raise IsolationUnavailable("no bwrap here")

        executor = ProcessExecutor(roles=self._roles(),
                                   badge_provider=provider)
        with patch.object(live_mod, "plan_agent",
                          side_effect=fake_plan), \
             patch.object(live_mod, "settings_overlay_bytes",
                          return_value=b"{}") as overlay_fn, \
             tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            result = await executor.run("builder", "hi", Path(tmp), 5)
        self.assertIn("sandbox refused", result.output_tail)
        provider.mint.assert_awaited_once_with("builder")
        provider.revoke.assert_awaited_once_with("badge-1")
        overlay_fn.assert_called_once()
        config_args = seen["config_args"]
        assert isinstance(config_args, list)
        self.assertIn("--disable-reminders", config_args)
        self.assertIn("--no-session-log", config_args)
        self.assertTrue(any(str(token).startswith("run.developer_prompt=")
                            for token in config_args))
        overlays = seen["overlays"]
        assert isinstance(overlays, dict)
        self.assertEqual(overlays, {SETTINGS_OVERLAY_RELPATH: b"{}"})
        self.assertEqual((), seen["extra_read_roots"])
        self.assertEqual((), seen["extra_write_roots"])


class SandboxOverlayTests(unittest.TestCase):
    def test_plan_agent_binds_overlay_after_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            ws = Path(tmp)
            (ws / "f.txt").write_text("x", encoding="utf-8")
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", True):
                plan = agent_sandbox.plan_agent(
                    argv=["muse", "exec", "hi"], workspace=ws,
                    base={}, overlay={},
                    home_file_overlays={".config/muse/settings.json":
                                        b'{"a": 1}'})
            argv = plan.argv
            hits = [i for i, token in enumerate(argv)
                    if token == str(Path.home()
                                    / ".config/muse/settings.json")]
            self.assertEqual(len(hits), 1)
            self.assertEqual(argv[hits[0] - 2], "--ro-bind")
            src = Path(argv[hits[0] - 1])
            self.assertTrue(str(src).startswith(str(ws)))
            self.assertEqual(src.read_bytes(), b'{"a": 1}')
            self.assertEqual(oct(src.stat().st_mode & 0o777), "0o600")

    def test_plan_agent_refuses_escaping_overlay(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", True):
                with self.assertRaises(ValueError):
                    agent_sandbox.plan_agent(
                        argv=["muse"], workspace=Path(tmp), base={},
                        overlay={},
                        home_file_overlays={"../evil": b"x"})

    def test_plan_agent_overlay_sources_are_unique_per_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            ws = Path(tmp)
            (ws / "f.txt").write_text("x", encoding="utf-8")
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", True):
                first = agent_sandbox.plan_agent(
                    argv=["muse"], workspace=ws, base={}, overlay={},
                    home_file_overlays={".config/muse/settings.json":
                                        b'{"run": 1}'})
                second = agent_sandbox.plan_agent(
                    argv=["muse"], workspace=ws, base={}, overlay={},
                    home_file_overlays={".config/muse/settings.json":
                                        b'{"run": 2}'})
            dest = str(Path.home() / ".config/muse/settings.json")
            srcs = [argv[argv.index(dest) - 1]
                    for argv in (first.argv, second.argv)]
            self.assertNotEqual(srcs[0], srcs[1])
            self.assertEqual(Path(srcs[0]).read_bytes(), b'{"run": 1}')
            self.assertEqual(Path(srcs[1]).read_bytes(), b'{"run": 2}')

    def test_sandboxed_shell_command_passes_overlays(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", True):
                line = agent_sandbox.sandboxed_shell_command(
                    command=["muse"], workspace=Path(tmp), base={},
                    overlay={},
                    home_file_overlays={".config/muse/settings.json":
                                        b"{}"})
        self.assertIn(".config/muse/settings.json", line)


class TierProfileThreadingTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        from agents.dev.session import DevSessionManager
        return DevSessionManager(MagicMock(), MagicMock(), repo=None)

    def _provider(self):
        provider = MagicMock()
        provider.mint = AsyncMock(return_value="badge-1")
        provider.mcp_servers = MagicMock(return_value={})
        provider.revoke = AsyncMock()
        return provider

    async def test_tier_overlays_use_configured_profile(self) -> None:
        manager = self._manager()
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with patch("agents.dev.session.settings_overlay_bytes",
                       return_value=b"{}") as overlay_fn:
                overlays, badge = await manager._muse_role_overlays(
                    self._provider(), "tier_c:muse", tmp, "dev")
        self.assertEqual(badge, "badge-1")
        self.assertEqual(set(overlays), {SETTINGS_OVERLAY_RELPATH})
        profile = overlay_fn.call_args[0][0]
        self.assertEqual(profile.name, "dev")

    async def test_tier_overlays_refuse_non_dev_profile(self) -> None:
        manager = self._manager()
        provider = self._provider()
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            with self.assertRaises(RoleProfileError):
                await manager._muse_role_overlays(
                    provider, "tier_c:muse", tmp, "planner")
        provider.mint.assert_not_awaited()


class InteractiveBadgeRevocationTests(unittest.IsolatedAsyncioTestCase):
    def _service(self):
        from dev_session_service import DevSessionService
        from settings import Settings
        settings = Settings(_env_file=None)
        repo = MagicMock()
        repo.set_selected_project = AsyncMock()
        repo.find_active_project_session = AsyncMock(return_value=None)
        repo.create_dev_session = AsyncMock(return_value={"session_id": "s"})
        hub = MagicMock()
        hub.broadcast = AsyncMock()
        service = DevSessionService(settings, repo, MagicMock(),
                                    MagicMock(), hub)
        service._tmux = MagicMock()
        service._tmux.wait_until_ready = AsyncMock(return_value=True)
        return service

    def _payload(self):
        from state import DevSessionCreateRequest
        return DevSessionCreateRequest(title="t", objective="o")

    def _issuer(self):
        issuer = MagicMock()
        issuer.issue_service = AsyncMock(return_value="badge-1")
        issuer.revoke = AsyncMock(return_value=True)
        return issuer

    def _prime(self, service, tmp: str):
        from state import DevProvider
        service._project_service.resolve_project = AsyncMock(
            return_value="proj")
        service._config_loader.load_project = MagicMock(return_value={})
        service._config_loader.validate_project = MagicMock(
            return_value=[])
        service._resolve_provider = MagicMock(  # type: ignore[method-assign]
            return_value=DevProvider.MUSE)
        service._resolve_command = MagicMock(  # type: ignore[method-assign]
            return_value=(["musej"], {}, tmp))
        service._resolve_repo_path = MagicMock(  # type: ignore[method-assign]
            return_value=tmp)

    async def test_render_failure_revokes_badge(self) -> None:
        service = self._service()
        issuer = self._issuer()
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            self._prime(service, tmp)
            with patch("dev_session_service.issuer_for_repo",
                       return_value=issuer), \
                 patch("dev_session_service.settings_overlay_bytes",
                       side_effect=RoleProfileError("drift")):
                with self.assertRaisesRegex(ValueError,
                                            "role profile refused"):
                    await service.create_session(self._payload())
        issuer.revoke.assert_awaited_once()

    async def test_start_failure_revokes_badge(self) -> None:
        service = self._service()
        issuer = self._issuer()
        service._tmux.start_session = MagicMock(
            side_effect=RuntimeError("boom"))
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            self._prime(service, tmp)
            with patch("dev_session_service.issuer_for_repo",
                       return_value=issuer), \
                 patch("dev_session_service.settings_overlay_bytes",
                       return_value=b"{}"):
                with self.assertRaisesRegex(ValueError,
                                            "Failed to start"):
                    await service.create_session(self._payload())
        issuer.revoke.assert_awaited_once()

    async def test_existing_session_reconnect_revokes_badge(self) -> None:
        service = self._service()
        issuer = self._issuer()
        service._tmux.start_session = MagicMock(
            side_effect=ValueError("tmux session already exists"))
        with tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            self._prime(service, tmp)
            with patch("dev_session_service.issuer_for_repo",
                       return_value=issuer), \
                 patch("dev_session_service.settings_overlay_bytes",
                       return_value=b"{}"):
                await service.create_session(self._payload())
        issuer.revoke.assert_awaited_once()


def _session_fixture(path: Path) -> None:
    trace = {"payload_type": "runtime.session",
             "payload": {"event": {
                 "kind": "model_input_trace_recorded",
                 "bounded": {"total_lane_bytes": 12500}}}}
    diag_rules = {"payload_type": "runtime.session",
                  "payload": {"event": {
                      "kind": "context_block_diagnostic",
                      "block_id": "rules_file", "text_bytes": 4320}}}
    diag_off = {"payload_type": "runtime.session",
                "payload": {"event": {
                    "kind": "context_block_diagnostic",
                    "block_id": "workflow_availability_off",
                    "text_bytes": 539}}}
    configured = {"payload_type": "runtime.session",
                  "payload": {"event": {
                      "kind": "model_request_configured",
                      "developer_prompt": "Role: dev.",
                      "reminder_roster": {"agents": []},
                      "run_context_messages": [
                          {"id": "rules_file",
                           "text": "Sesiones del Owner: lee CLAUDE.md"},
                          {"id": "skills_catalog",
                           "text": '<skill-catalog><skill id="git">'}]}}}
    catalog = {"payload_type": "runtime.mcp_tool_identity_catalog",
               "payload": {"entries": [
                   {"raw_tool_name": "openceo_get_ticket"},
                   {"raw_tool_name": "openceo_search_tickets"}]}}
    path.write_text("\n".join(json.dumps(record) for record in
                              (trace, diag_rules, diag_off, configured,
                               catalog)) + "\n", encoding="utf-8")


class MeasureSessionLogTests(unittest.TestCase):
    def test_measure_parses_echo_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sess-") as tmp:
            log = Path(tmp) / "session.jsonl"
            _session_fixture(log)
            measure = measure_session_log(log)
        self.assertEqual(measure.total_lane_bytes, 12500)
        self.assertEqual(measure.skill_ids, frozenset({"git"}))
        self.assertEqual(measure.mcp_tool_names,
                         frozenset({"openceo_get_ticket",
                                    "openceo_search_tickets"}))
        self.assertEqual(measure.blocks["rules_file"], 4320)
        self.assertIn("Sesiones del Owner", measure.rules_text)
        self.assertEqual(measure.developer_prompt, "Role: dev.")
        self.assertEqual(measure.reminder_roster, 0)

    def test_measure_refuses_missing_trace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sess-") as tmp:
            log = Path(tmp) / "session.jsonl"
            log.write_text('{"payload_type": "other"}\n',
                           encoding="utf-8")
            with self.assertRaises(RoleProfileError):
                measure_session_log(log)

    def test_measure_refuses_trace_without_byte_count(self) -> None:
        trace = {"payload_type": "runtime.session",
                 "payload": {"event": {
                     "kind": "model_input_trace_recorded",
                     "bounded": {}}}}
        garbage = {"payload_type": "runtime.session",
                   "payload": {"event": {
                       "kind": "model_input_trace_recorded",
                       "bounded": {"total_lane_bytes": "many"}}}}
        with tempfile.TemporaryDirectory(prefix="sess-") as tmp:
            for name, record in (("empty.jsonl", trace),
                                 ("garbage.jsonl", garbage)):
                log = Path(tmp) / name
                log.write_text(json.dumps(record) + "\n", encoding="utf-8")
                with self.subTest(log=name):
                    with self.assertRaises(RoleProfileError):
                        measure_session_log(log)

    def test_measure_refuses_missing_log(self) -> None:
        with self.assertRaises(RoleProfileError):
            measure_session_log("/tmp/definitely-not-a-log-197.jsonl")


def _mcp_server_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 18765), timeout=5):
            return True
    except OSError:
        return False


async def _mint_smoke_badge(kind: str, agent: str = "dev") -> str | None:
    """Real badge for the smoke run (None when Mongo is down).

    ``kind`` is ``agent`` (ticket badge for the role), ``readonly``
    (planner) or ``none`` (disabled agents have no box yet).
    """
    if kind == "none":
        return None
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        from mcp_identity import MongoTokenStore, TokenIssuer
        from catalog import load_catalog
        from datetime import timedelta
    except ImportError:
        return None
    try:
        client = AsyncIOMotorClient("mongodb://localhost:27017",
                                    serverSelectionTimeoutMS=5000)
        try:
            issuer = TokenIssuer(
                MongoTokenStore(db=client["openceo"]),
                load_catalog().agents)
            if kind == "readonly":
                return await issuer.issue_readonly("planner")
            return await issuer.issue_agent(
                agent, "TASK-2026-00197", "jardal-openceo",
                timedelta(minutes=15))
        finally:
            client.close()
    except Exception:
        return None


async def _revoke_smoke_badge(badge: str | None) -> None:
    if not badge:
        return
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        from mcp_identity import MongoTokenStore, TokenIssuer
        from catalog import load_catalog
        client = AsyncIOMotorClient("mongodb://localhost:27017",
                                    serverSelectionTimeoutMS=5000)
        try:
            issuer = TokenIssuer(
                MongoTokenStore(db=client["openceo"]),
                load_catalog().agents)
            await issuer.revoke(badge, "smoke test done")
        finally:
            client.close()
    except Exception:
        pass


def _run_echo(argv: list[str], home: Path, session_id: str,
              timeout_s: int = 180) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home), "MUSE_NO_AUTO_UPDATE": "1"}
    return subprocess.run(
        [*argv, "--session-id", session_id, "SMOKE-PROBE-00197"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        errors="replace", timeout=timeout_s, env=env, cwd=str(REPO))


def _session_log(home: Path, session_id: str) -> Path:
    today = date.today()
    return (home / ".local/share/muse/sessions" / f"{today:%Y/%m/%d}"
            / session_id / "session.jsonl")


class RoleProfileSmokeTests(unittest.IsolatedAsyncioTestCase):
    """Echo acceptance: bytes, skills, tools and extras per role.

    The smoke runs drop ``--no-session-log`` (production keeps it: a
    unit test above pins the flag) so the session log stays readable.
    """

    def test_smoke_version_pin(self) -> None:
        try:
            binary = resolve_muse_bin()
        except RoleProfileError as exc:
            self.skipTest(f"no muse binary: {exc}")
        try:
            check_pinned_version(binary)
        except RoleProfileError as exc:
            self.fail(f"muse upgrade without smoke + pin update: {exc}")

    def test_smoke_reviewer_slot_untouched(self) -> None:
        from config_loader import ConfigLoader
        from settings import Settings
        with tempfile.TemporaryDirectory(prefix="cfg-") as tmp:
            cfg = ConfigLoader(Settings(_env_file=None,
                                        PROJECTS_DIR=tmp)).load_project(None)
        reviewers = cfg["review"]["reviewers"]
        muse_review = next(slot for slot in reviewers
                           if slot.get("label") == "muse-review")
        self.assertNotIn("role_profile", muse_review)
        self.assertEqual(muse_review["command"][0], "muse")

    async def test_smoke_all_profiles_under_60_percent(self) -> None:
        try:
            binary = resolve_muse_bin()
            check_pinned_version(binary)
        except RoleProfileError as exc:
            self.skipTest(f"no pinned muse binary: {exc}")
        if not _mcp_server_reachable():
            self.skipTest("no live MCP server on 127.0.0.1:18765")
        from mcp_identity import READONLY_BOX
        from mcp_server import build_mcp_provider_config
        from settings import Settings
        settings = Settings()
        base_argv = [binary, "exec", "--provider", "echo"]
        baseline_argv = strip_echo_incompatible([
            *base_argv, "--model", "muse-spark-1.3-contributor",
            "--yolo", "--disable-sandbox", "--disable-approval",
            "--approval-mode", "never", "--trust-workspace",
            "--reasoning-effort", "max"])
        baseline_id = str(uuid.uuid4())
        proc = _run_echo(baseline_argv, Path.home(), baseline_id)
        self.assertIn("echo:", proc.stdout,
                      f"baseline echo failed: {proc.stderr[:500]}")
        baseline = measure_session_log(_session_log(Path.home(),
                                                    baseline_id))
        operations_box = set(load_catalog(
        ).agents["operations"].boundary_tools)
        matrix = (("dev", "agent", set(dev_box_tools())),
                  ("planner", "readonly", set(READONLY_BOX)),
                  ("ceo", "readonly", set(READONLY_BOX)),
                  ("operations", "agent", operations_box),
                  ("secretary", "none", None),
                  ("finance", "none", None),
                  ("research", "none", None))
        for name, kind, expected_box in matrix:
            with self.subTest(profile=name):
                await self._smoke_one_profile(
                    name, kind, expected_box, settings, base_argv,
                    baseline.total_lane_bytes)

    async def _smoke_one_profile(self, name: str, kind: str,
                                 expected_box: set[str] | None,
                                 settings, base_argv: list[str],
                                 baseline_lanes: int) -> None:
        from mcp_server import build_mcp_provider_config
        badge = await _mint_smoke_badge(kind, name)
        if kind != "none" and not badge:
            self.skipTest("no local Mongo for a smoke badge")
        try:
            profile = load_role_profile(name)
            if badge:
                servers = build_mcp_provider_config(settings, badge)
                self.assertIsNotNone(servers, "MCP disabled in Settings?")
                assert servers is not None
                mcp_servers = dict(servers.get("mcpServers") or {})
            else:
                mcp_servers = {}
            with tempfile.TemporaryDirectory(
                    prefix="role-smoke-home-") as fake_home:
                fake = Path(fake_home)
                rendered = settings_overlay_bytes(
                    profile, Path.home(), REPO, mcp_servers)
                _write(fake / SETTINGS_OVERLAY_RELPATH,
                       rendered.decode("utf-8"))
                extras = [token for token in render_argv_extras(profile)
                          if token != "--no-session-log"]
                profile_argv = [*base_argv, "--yolo", "--disable-sandbox",
                                "--disable-approval", "--approval-mode",
                                "never", "--trust-workspace", *extras,
                                *render_card_argv(profile)]
                profile_id = str(uuid.uuid4())
                proc = _run_echo(profile_argv, fake, profile_id)
                self.assertIn("echo:", proc.stdout,
                              f"profile echo failed: {proc.stderr[:500]}")
                measured = measure_session_log(
                    _session_log(fake, profile_id))
        finally:
            await _revoke_smoke_badge(badge)
        ratio = measured.total_lane_bytes / baseline_lanes
        self.assertLess(
            ratio, 0.60,
            f"{name} prompt {measured.total_lane_bytes}B is "
            f"{ratio:.1%} of baseline {baseline_lanes}B")
        allowed = set(profile.skills)
        present = {skill.split(":")[-1] for skill in measured.skill_ids}
        foreign = {skill for skill in present if skill not in allowed}
        self.assertEqual(foreign, set(),
                         f"foreign skills loaded: {sorted(foreign)}")
        missing = allowed - present
        self.assertEqual(missing, set(),
                         f"role skills not loaded: {sorted(missing)}")
        if expected_box is not None:
            self.assertEqual(set(measured.mcp_tool_names), expected_box,
                             f"{name} run must see exactly its box")
        self.assertIn("Sesiones del Owner", measured.rules_text,
                      "the role must load AGENTS.md")
        self.assertNotIn("Continuar el roadmap", measured.rules_text,
                         "the role must not load CLAUDE.md")
        self.assertNotIn("workflow_choice", measured.blocks)
        self.assertIn("workflow_availability_off", measured.blocks)
        self.assertNotIn("subagent_delegation", measured.blocks)
        self.assertEqual(measured.reminder_roster, 0)
        self.assertIn(f"Role: {name}.", measured.developer_prompt)


if __name__ == "__main__":
    unittest.main()
