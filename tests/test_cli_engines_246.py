"""CLI per agent: engine registry, translators and dispatch (TASK-2026-00246).

Seven selections (muse, opencode, claude, codex, agyd, agyj, agy2) resolve
from card + ticket overrides through one registry; codex and agy join the
live translators; each run records its engine and never hot-swaps; only
installed, effectively configured CLIs run.

No network, no CLI, no LLM: in-memory catalogs, tmp trees and scripted
executors. Real install/auth probes are ticket evidence, not tests.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    ENGINE_ACCOUNTS,
    KNOWN_ENGINES,
    AgentCard,
    Catalog,
    CatalogError,
    IndexEntry,
    RoleProfile,
    StaffRoster,
    default_engine_account,
    load_catalog,
)
from graph import resolve_agent_gate  # noqa: E402
from orchestration.engines import (  # noqa: E402
    ATTEMPT_PARAMS_KEY,
    ENGINE_SPECS,
    EngineResolutionError,
    EngineSelection,
    check_engine_available,
    command_for,
    family_of,
    probe_engine_version,
    resolve_engine,
)
from orchestration.generic_agent import (  # noqa: E402
    GENERIC_MUSE_MODEL,
    build_agent_prompt,
    muse_role_spec,
    role_spec_for_engine,
    run_generic_agent,
)
from orchestration.live import ExecResult, ProcessExecutor  # noqa: E402
from orchestration.role_agy import (  # noqa: E402
    AGY_HOME_SUBDIRS,
    agy_config_bytes,
    overlay_relpath,
    render_argv_extras as agy_argv_extras,
    render_mcp_config as agy_mcp_config,
)
from orchestration.role_codex import (  # noqa: E402
    CODEX_MCP_SERVER,
    CODEX_MCP_TOKEN_ENV,
    codex_mcp_env,
    render_argv_extras as codex_argv_extras,
)
from orchestration.role_muse import RoleProfileError  # noqa: E402
from state import TaskStatus  # noqa: E402
from test_departments_alta_244 import (  # noqa: E402
    write_tree as write_catalog_tree,
)
from test_departments_alta_244 import (  # noqa: E402
    _card as _tree_card,
)
from test_departments_alta_244 import (  # noqa: E402
    _profile as _tree_profile,
)
from test_generic_agent_205 import (  # noqa: E402
    FakeBadgeProvider,
    generic_task,
    hecho_stdout,
    scripted_executor,
    tiny_profile,
    wiring_with,
)

SERVERS = {"openceo-internal": {
    "type": "http",
    "url": "http://127.0.0.1:18765/internal/mcp/",
    "headers": {"Authorization": "Bearer [REDACTED]"},
}}

SELECTIONS = (
    ("muse-code", "muse"),
    ("claude-code", "claudej"),
    ("opencode", "opencode"),
    ("codex-cli", "codexj"),
    ("agy", "agyd"),
    ("agy", "agyj"),
    ("agy", "agy2"),
)


def tiny_catalog(specs: dict[str, dict[str, Any]]) -> Catalog:
    """In-memory catalog: name -> {enabled, engine?, account?, profile?}."""
    entries = [
        IndexEntry(name=name, summary=f"{name} does things, not others.",
                   card=f"agents/{name}.yaml")
        for name in specs
    ]
    agents = {}
    for name, spec in specs.items():
        enabled = bool(spec.get("enabled", True))
        agents[name] = AgentCard(
            name=name, type=spec.get("type", "ai"),
            engine=None if spec.get("type") == "human"
            else spec.get("engine", "muse-code"),
            profile=spec.get("profile", f"profiles/{name}.yaml"),
            department=None,
            account=spec.get("account"),
            boundary_tools=[], skills_dir=f"skills/{name}", skills=[],
            inputs=[], outputs=[], enabled=enabled,
            disabled_reason=None if enabled else spec.get("reason", "off"),
            decided_by_ticket=(None if enabled
                               else spec.get("ticket", "TASK-2026-00246")))
    return Catalog(version=1, entries=entries, agents=agents,
                   staff=StaffRoster())


class RegistryVocabTest(unittest.TestCase):
    def test_specs_match_catalog_vocabulary(self) -> None:
        self.assertEqual(set(KNOWN_ENGINES), set(ENGINE_SPECS))
        for engine in ENGINE_SPECS:
            with self.subTest(engine=engine):
                self.assertTrue(ENGINE_ACCOUNTS.get(engine))

    def test_seven_selections(self) -> None:
        got = [(engine, account)
               for engine in sorted(ENGINE_SPECS)
               for account in ENGINE_ACCOUNTS[engine]]
        self.assertEqual(7, len(got))
        self.assertEqual(set(SELECTIONS), set(got))

    def test_muse_registry_mirrors_house_model(self) -> None:
        self.assertEqual(GENERIC_MUSE_MODEL,
                         ENGINE_SPECS["muse-code"].model)


class ResolveEngineTest(unittest.TestCase):
    def test_card_engine_and_account_used(self) -> None:
        selection = resolve_engine("agy", "agyj", {})
        self.assertEqual(EngineSelection("agy", "agyj", "card"), selection)

    def test_defaults(self) -> None:
        self.assertEqual(
            EngineSelection("agy", "agyd", "card"),
            resolve_engine("agy", None, {}))
        self.assertEqual(
            EngineSelection("muse-code", "muse", "card"),
            resolve_engine("muse-code", None, {}))
        self.assertEqual("agyd", default_engine_account("agy"))
        self.assertIsNone(default_engine_account("not-a-cli"))

    def test_params_override_wins(self) -> None:
        selection = resolve_engine(
            "muse-code", "muse",
            {"engine": "codex-cli", "engine_account": "codexj"})
        self.assertEqual(
            EngineSelection("codex-cli", "codexj", "params"), selection)

    def test_account_only_override_keeps_card_via(self) -> None:
        # Review 00002/mimo-H3: via names the engine origin, so an
        # account-only override still records via="card".
        selection = resolve_engine(
            "agy", "agyd", {"engine_account": "agy2"})
        self.assertEqual(
            EngineSelection("agy", "agy2", "card"), selection)

    def test_engine_override_resets_foreign_card_account(self) -> None:
        selection = resolve_engine("agy", "agy2", {"engine": "codex-cli"})
        self.assertEqual(
            EngineSelection("codex-cli", "codexj", "params"), selection)

    def test_unknown_engine_fails_without_fallback(self) -> None:
        with self.assertRaisesRegex(EngineResolutionError,
                                    "unknown engine 'nope'"):
            resolve_engine("muse-code", "muse", {"engine": "nope"})
        with self.assertRaisesRegex(EngineResolutionError, "unknown engine"):
            resolve_engine("nope", None, {})

    def test_foreign_account_fails(self) -> None:
        with self.assertRaisesRegex(EngineResolutionError, "agy2"):
            resolve_engine("codex-cli", "codexj",
                           {"engine_account": "agy2"})
        with self.assertRaisesRegex(EngineResolutionError, "agy2"):
            resolve_engine("codex-cli", "agy2", {})


class AvailabilityTest(unittest.TestCase):
    def test_missing_binary_names_engine_and_command(self) -> None:
        reason = check_engine_available(
            "agy", "agyj", which=lambda _binary: None)
        assert reason is not None
        self.assertIn("agy/agyj", reason)
        self.assertIn("agyj", reason)

    def test_present_binary_passes(self) -> None:
        self.assertIsNone(check_engine_available(
            "agy", "agy2", which=lambda binary: f"/bin/{binary}"))

    def test_unknown_engine_reports(self) -> None:
        reason = check_engine_available("nope", "x")
        assert reason is not None
        self.assertIn("unknown engine", reason)

    def test_probe_version_ok(self) -> None:
        binaries = {"muse-code": "musej", "claude-code": "claudej",
                    "opencode": "opencode", "codex-cli": "codexj"}
        for engine, account in SELECTIONS:
            with self.subTest(engine=engine, account=account):
                proc = mock.Mock(returncode=0, stdout="v1\n", stderr="")
                run = mock.Mock(return_value=proc)
                report = probe_engine_version(engine, account, run=run)
                self.assertTrue(report["ok"])
                self.assertEqual("v1", report["version"])
                binary = binaries.get(engine, account)
                self.assertEqual([binary, "--version"],
                                 report["command"])
                run.assert_called_once()

    def test_probe_version_failure(self) -> None:
        proc = mock.Mock(returncode=1, stdout="", stderr="boom\n")
        report = probe_engine_version(
            "agy", "agyd", run=mock.Mock(return_value=proc))
        self.assertFalse(report["ok"])
        self.assertIn("rc=1", report["reason"])
        missing = probe_engine_version(
            "nope", "x", run=mock.Mock(side_effect=AssertionError))
        self.assertFalse(missing["ok"])

    def test_probe_version_spawn_error(self) -> None:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("no exec")
        report = probe_engine_version("agy", "agyd", run=_boom)
        self.assertFalse(report["ok"])
        self.assertIn("OSError", report["reason"])


class FamilyTest(unittest.TestCase):
    def test_agy_accounts_share_one_family(self) -> None:
        families = {family_of("agy")}
        self.assertEqual({"gemini"}, families)

    def test_other_engines_have_distinct_families(self) -> None:
        families = [family_of(engine) for engine in
                    ("muse-code", "claude-code", "opencode", "codex-cli")]
        self.assertEqual(4, len(set(families)))
        self.assertNotIn("gemini", families)

    def test_unknown_engine_raises(self) -> None:
        with self.assertRaises(EngineResolutionError):
            family_of("nope")


class CodexTranslatorTest(unittest.TestCase):
    def test_override_points_at_token_env(self) -> None:
        argv = codex_argv_extras()
        self.assertEqual("-c", argv[0])
        self.assertIn(
            f"mcp_servers.{CODEX_MCP_SERVER}.bearer_token_env_var",
            argv[1])
        self.assertIn(CODEX_MCP_TOKEN_ENV, argv[1])

    def test_env_carries_badge(self) -> None:
        self.assertEqual({CODEX_MCP_TOKEN_ENV: "badge-1"},
                         codex_mcp_env("badge-1"))


class AgyTranslatorTest(unittest.TestCase):
    def test_argv_print_takes_prompt_value(self) -> None:
        self.assertEqual(["--print"], agy_argv_extras())

    def test_overlay_relpath_per_account(self) -> None:
        self.assertEqual(".gemini/config/mcp_config.json",
                         overlay_relpath("agyd"))
        self.assertEqual(".gemini-j/config/mcp_config.json",
                         overlay_relpath("agyj"))
        self.assertEqual(".gemini-2/config/mcp_config.json",
                         overlay_relpath("agy2"))
        with self.assertRaisesRegex(RoleProfileError, "agy4"):
            overlay_relpath("agy4")

    def test_account_dirs_cover_registry(self) -> None:
        self.assertEqual(set(ENGINE_ACCOUNTS["agy"]),
                         set(AGY_HOME_SUBDIRS))

    def test_render_merges_badged_server_over_base(self) -> None:
        base = {"mcpServers": {
            "openceo-internal": {
                "serverUrl": "http://127.0.0.1:18765/internal/mcp/"},
            "other": {"serverUrl": "http://x/y"}}}
        rendered = agy_mcp_config(base, SERVERS)
        badged = rendered["mcpServers"]["openceo-internal"]
        self.assertEqual("http://127.0.0.1:18765/internal/mcp/",
                         badged["serverUrl"])
        self.assertEqual("Bearer [REDACTED]",
                         badged["headers"]["Authorization"])
        self.assertFalse(badged["disabled"])
        self.assertEqual({"serverUrl": "http://x/y"},
                         rendered["mcpServers"]["other"])

    def test_render_refuses_server_without_url(self) -> None:
        with self.assertRaisesRegex(RoleProfileError, "no url"):
            agy_mcp_config({}, {"broken": {"headers": {}}})

    def test_config_bytes_refuse_unreadable_base(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as home:
            with self.assertRaisesRegex(RoleProfileError, "unreadable"):
                agy_config_bytes(home, "agyj", SERVERS)
            target = Path(home) / ".gemini-j" / "config"
            target.mkdir(parents=True)
            (target / "mcp_config.json").write_text("not json",
                                                   encoding="utf-8")
            with self.assertRaisesRegex(RoleProfileError, "not JSON"):
                agy_config_bytes(home, "agyj", SERVERS)

    def test_config_bytes_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="home-") as home:
            target = Path(home) / ".gemini-2" / "config"
            target.mkdir(parents=True)
            (target / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {}}), encoding="utf-8")
            payload = json.loads(agy_config_bytes(
                home, "agy2", SERVERS).decode("utf-8"))
        self.assertEqual(
            "Bearer [REDACTED]",
            payload["mcpServers"]["openceo-internal"]["headers"]
            ["Authorization"])


class LiveDispatchEnginesTest(unittest.IsolatedAsyncioTestCase):
    def _provider(self):
        provider = mock.MagicMock()
        provider.mint = mock.AsyncMock(return_value="badge-1")
        provider.mcp_servers = mock.MagicMock(return_value=dict(SERVERS))
        provider.revoke = mock.AsyncMock()
        return provider

    async def _run(self, spec):
        from research.runtimes.base import IsolationUnavailable
        seen: dict[str, object] = {}

        def fake_plan(*, argv, workspace, base, overlay, config_args,
                      home_file_overlays=None, extra_read_roots=(),
                      extra_write_roots=(), extra_ops=()):
            seen["config_args"] = list(config_args)
            seen["overlays"] = dict(home_file_overlays or {})
            seen["env"] = dict(overlay)
            raise IsolationUnavailable("no bwrap here")

        executor = ProcessExecutor(
            roles={"builder": spec}, badge_provider=self._provider())
        with mock.patch("orchestration.live.plan_agent",
                        side_effect=fake_plan), \
             tempfile.TemporaryDirectory(prefix="ws-") as tmp:
            result = await executor.run("builder", "hi", Path(tmp), 5)
        return result, seen

    async def test_codex_dispatch_carries_override_and_token(self) -> None:
        spec = {"provider": "codex",
                "command": ["codexj", "exec", "--ephemeral"],
                "prompt_via": "stdin", "role_profile": "dev"}
        result, seen = await self._run(spec)
        self.assertIn("sandbox refused", result.output_tail)
        config_args = seen["config_args"]
        assert isinstance(config_args, list)
        override = config_args[config_args.index("-c") + 1]
        self.assertIn("bearer_token_env_var", override)
        env = seen["env"]
        assert isinstance(env, dict)
        self.assertEqual("badge-1", env[CODEX_MCP_TOKEN_ENV])

    async def test_spec_env_wins_over_translator_env(self) -> None:
        spec = {"provider": "codex",
                "command": ["codexj", "exec", "--ephemeral"],
                "prompt_via": "stdin", "role_profile": "dev",
                "env": {CODEX_MCP_TOKEN_ENV: "owner-set"}}
        _result, seen = await self._run(spec)
        env = seen["env"]
        assert isinstance(env, dict)
        self.assertEqual("owner-set", env[CODEX_MCP_TOKEN_ENV])

    async def test_agy_dispatch_overlays_account_config(self) -> None:
        spec = {"provider": "agy", "account": "agyj",
                "command": ["agyj"], "prompt_via": "argv",
                "role_profile": "dev"}
        with tempfile.TemporaryDirectory(prefix="home-") as home:
            target = Path(home) / ".gemini-j" / "config"
            target.mkdir(parents=True)
            (target / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {}}), encoding="utf-8")
            with mock.patch("os.path.expanduser", return_value=home):
                result, seen = await self._run(spec)
        self.assertIn("sandbox refused", result.output_tail)
        overlays = seen["overlays"]
        assert isinstance(overlays, dict)
        self.assertEqual(set(overlays),
                         {".gemini-j/config/mcp_config.json"})
        payload = json.loads(next(iter(overlays.values())).decode("utf-8"))
        self.assertEqual(
            "Bearer [REDACTED]",
            payload["mcpServers"]["openceo-internal"]["headers"]
            ["Authorization"])
        config_args = seen["config_args"]
        assert isinstance(config_args, list)
        self.assertEqual(["agyj", "--print"], config_args)

    async def test_agy_unknown_account_refused(self) -> None:
        spec = {"provider": "agy", "account": "agy4",
                "command": ["agy4"], "prompt_via": "argv",
                "role_profile": "dev"}
        result, _seen = await self._run(spec)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unknown agy account", result.output_tail)

    async def test_agy_missing_base_config_refused(self) -> None:
        spec = {"provider": "agy", "account": "agyj",
                "command": ["agyj"], "prompt_via": "argv",
                "role_profile": "dev"}
        with tempfile.TemporaryDirectory(prefix="home-") as home:
            with mock.patch("os.path.expanduser", return_value=home):
                result, _seen = await self._run(spec)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("unreadable", result.output_tail)

    async def test_unknown_provider_still_refused(self) -> None:
        spec = {"provider": "nope", "command": ["nope"],
                "prompt_via": "argv", "role_profile": "dev"}
        result, _seen = await self._run(spec)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("no translator", result.output_tail)


class RoleSpecTest(unittest.TestCase):
    def test_muse_keeps_pinned_builder(self) -> None:
        spec = role_spec_for_engine(
            EngineSelection("muse-code", "muse", "card"),
            tiny_profile("operations"), "operations")
        self.assertEqual(
            ["musej", "exec", "--model", GENERIC_MUSE_MODEL],
            spec["command"])
        self.assertEqual("argv", spec["prompt_via"])
        self.assertEqual("operations", spec["role_profile"])
        self.assertIn("--output-schema", spec["headless_args"])
        self.assertEqual(
            muse_role_spec(tiny_profile("operations"), "operations"), spec)

    def test_specs_per_selection(self) -> None:
        wants = {
            ("muse-code", "muse"): ("muse", ["musej"], "argv"),
            ("claude-code", "claudej"): ("claude", ["claudej"], "argv"),
            ("opencode", "opencode"): ("opencode", ["opencode"], "argv"),
            ("codex-cli", "codexj"): ("codex", ["codexj"], "stdin"),
            ("agy", "agyd"): ("agy", ["agyd"], "argv"),
            ("agy", "agyj"): ("agy", ["agyj"], "argv"),
            ("agy", "agy2"): ("agy", ["agy2"], "argv"),
        }
        for (engine, account), (provider, head, via) in wants.items():
            with self.subTest(engine=engine, account=account):
                spec = role_spec_for_engine(
                    EngineSelection(engine, account, "card"),
                    tiny_profile("operations"), "operations")
                self.assertEqual(provider, spec["provider"])
                self.assertEqual(account, spec["account"])
                self.assertEqual(head, spec["command"][:1])
                self.assertEqual(via, spec["prompt_via"])
                self.assertEqual("operations", spec["role_profile"])
                self.assertTrue(spec["active"])


ATTEMPT_MODELS = {
    "muse-code": GENERIC_MUSE_MODEL,
    "claude-code": "opus",
    "opencode": "",
    "codex-cli": "gpt-6.1-sol",
    "agy": "",
}


class GenericRunEnginesTest(unittest.TestCase):
    def _world(self, engine, account, stdout, returncode=0,
               params=None, usage=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        catalog = tiny_catalog(
            {"operations": {"enabled": True, "engine": engine,
                            "account": account}})
        factory, recorded = scripted_executor(
            ExecResult(returncode=returncode, elapsed_s=1.0,
                       output_tail=stdout, usage=usage))
        badge_calls: list[str] = []

        def _badge_factory(agent: str, ticket: str,
                           project: str) -> FakeBadgeProvider:
            badge_calls.append(f"factory:{agent}:{ticket}:{project}")
            return FakeBadgeProvider(badge_calls)

        wiring = wiring_with(root, catalog, factory,
                             badge_provider_factory=_badge_factory)
        available = mock.patch(
            "orchestration.generic_agent.check_engine_available",
            return_value=None)
        self.addCleanup(available.stop)
        available.start()
        return wiring, recorded, badge_calls, generic_task(
            params=dict(params or {}))

    def test_all_selections_complete_hecho(self) -> None:
        for engine, account in SELECTIONS:
            with self.subTest(engine=engine, account=account):
                wiring, recorded, badge_calls, task = self._world(
                    engine, account, hecho_stdout("done"))
                out = asyncio.run(run_generic_agent(
                    "operations", task, wiring))
                self.assertEqual(TaskStatus.COMPLETED.value,
                                 out["status"])
                self.assertEqual(
                    {"engine": engine, "account": account, "via": "card",
                     "model": ATTEMPT_MODELS[engine]},
                    out["params"][ATTEMPT_PARAMS_KEY])
                specs = [call["spec"] for call in recorded
                         if "spec" in call]
                self.assertEqual(1, len(specs))
                self.assertEqual(account, specs[0]["account"])
                prompts = [call["prompt"] for call in recorded
                           if "prompt" in call]
                self.assertEqual(1, len(prompts))
                self.assertIn("operations agent", prompts[0])
                self.assertEqual(
                    ["factory:operations:task-205:jardal-openceo"],
                    badge_calls)

    def test_error_communicated_per_selection(self) -> None:
        for engine, account in SELECTIONS:
            with self.subTest(engine=engine, account=account):
                wiring, _recorded, _calls, task = self._world(
                    engine, account, "cli exploded", returncode=1)
                out = asyncio.run(run_generic_agent(
                    "operations", task, wiring))
                self.assertEqual(TaskStatus.FAILED.value, out["status"])
                self.assertIn("returncode 1", out["error"])
                self.assertIn("cli exploded", out["error"])
                self.assertEqual(
                    engine, out["params"][ATTEMPT_PARAMS_KEY]["engine"])

    def test_crash_rewrites_hecho_to_bloqueado(self) -> None:
        for engine, account in (("muse-code", "muse"), ("agy", "agy2")):
            with self.subTest(engine=engine, account=account):
                wiring, _recorded, _calls, task = self._world(
                    engine, account, hecho_stdout("claimed"), returncode=1)
                out = asyncio.run(run_generic_agent(
                    "operations", task, wiring))
                verdict = out["params"]["agent_result"]
                self.assertEqual("bloqueado", verdict["resultado"])
                self.assertIn("run crashed", verdict["motivo"])

    def test_truncated_output_unreadable(self) -> None:
        wiring, _recorded, _calls, task = self._world(
            "codex-cli", "codexj", hecho_stdout("late claim"),
            usage={"truncated": True, "stdout_chars": 99999})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.FAILED.value, out["status"])
        self.assertIn("truncated", out["error"])

    def test_unknown_engine_card_waits_without_fallback(self) -> None:
        wiring, recorded, _calls, task = self._world(
            "nope", None, hecho_stdout("never runs"))
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("no runner", out["waiting_reason"])
        self.assertEqual([], recorded)

    def test_unknown_override_waits_as_config(self) -> None:
        wiring, recorded, _calls, task = self._world(
            "muse-code", "muse", hecho_stdout("never runs"),
            params={"engine": "nope"})
        out = asyncio.run(run_generic_agent("operations", task, wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("invalid engine override", out["waiting_reason"])
        self.assertEqual([], recorded)

    def test_missing_binary_waits_with_command(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        catalog = tiny_catalog(
            {"operations": {"enabled": True, "engine": "agy",
                            "account": "agyj"}})
        factory, recorded = scripted_executor(
            ExecResult(returncode=0, elapsed_s=1.0,
                       output_tail=hecho_stdout("never runs")))
        wiring = wiring_with(root, catalog, factory)
        with mock.patch(
                "orchestration.generic_agent.check_engine_available",
                return_value="engine agy/agyj unavailable: command "
                             "'agyj' not found on PATH"):
            out = asyncio.run(run_generic_agent(
                "operations", generic_task(), wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("agyj", out["waiting_reason"])
        self.assertEqual([], recorded)


class EngineChangeTest(unittest.TestCase):
    def _run(self, catalog, params=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        factory, recorded = scripted_executor(
            ExecResult(returncode=0, elapsed_s=1.0,
                       output_tail=hecho_stdout("done")))
        wiring = wiring_with(root, catalog, factory)
        with mock.patch(
                "orchestration.generic_agent.check_engine_available",
                return_value=None):
            out = asyncio.run(run_generic_agent(
                "operations", generic_task(params=dict(params or {})),
                wiring))
        return out, recorded

    def test_params_override_applies_and_records(self) -> None:
        catalog = tiny_catalog(
            {"operations": {"enabled": True, "engine": "muse-code",
                            "account": "muse"}})
        out, recorded = self._run(
            catalog, {"engine": "agy", "engine_account": "agyj"})
        self.assertEqual(
            {"engine": "agy", "account": "agyj", "via": "params",
             "model": ""},
            out["params"][ATTEMPT_PARAMS_KEY])
        specs = [call["spec"] for call in recorded if "spec" in call]
        self.assertEqual(["agyj"], specs[0]["command"][:1])

    def test_next_invocation_reresolves(self) -> None:
        catalog = tiny_catalog(
            {"operations": {"enabled": True, "engine": "muse-code",
                            "account": "muse"}})
        first, _recorded = self._run(catalog)
        self.assertEqual(
            "muse-code", first["params"][ATTEMPT_PARAMS_KEY]["engine"])
        catalog.agents["operations"].engine = "codex-cli"
        catalog.agents["operations"].account = "codexj"
        second, recorded = self._run(catalog)
        self.assertEqual(
            "codex-cli", second["params"][ATTEMPT_PARAMS_KEY]["engine"])
        self.assertEqual(
            "muse-code", first["params"][ATTEMPT_PARAMS_KEY]["engine"])
        specs = [call["spec"] for call in recorded if "spec" in call]
        self.assertEqual(["codexj"], specs[0]["command"][:1])


class CardAccountTest(unittest.TestCase):
    def test_agyj_card_loads(self) -> None:
        card = _tree_card("accountant", department="finance")
        card["engine"] = "agy"
        card["account"] = "agyj"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_catalog_tree(root, {"accountant": card})
            catalog = load_catalog(tree)
        self.assertEqual("agyj",
                         catalog.agents["accountant"].account)

    def test_foreign_account_fails(self) -> None:
        card = _tree_card("accountant", department="finance")
        card["engine"] = "codex-cli"
        card["account"] = "agyj"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_catalog_tree(root, {"accountant": card})
            with self.assertRaisesRegex(CatalogError, "not an account"):
                load_catalog(tree)

    def test_human_account_fails(self) -> None:
        card = _tree_card("owner", card_type="human", profile=False)
        card["account"] = "muse"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_catalog_tree(root, {"owner": card})
            with self.assertRaisesRegex(CatalogError, "only ai agents"):
                load_catalog(tree)

    def test_unknown_engine_with_account_fails(self) -> None:
        card = _tree_card("accountant", department="finance")
        card["engine"] = "nope"
        card["account"] = "x"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = write_catalog_tree(root, {"accountant": card})
            with self.assertRaisesRegex(CatalogError, "not an account"):
                load_catalog(tree)

    def test_profiles_accept_new_engines(self) -> None:
        for engine in ("muse-code", "claude-code", "opencode",
                       "codex-cli", "agy"):
            with self.subTest(engine=engine):
                profile = RoleProfile.model_validate(
                    {"name": "ops", "agent": "ops", "engine": engine,
                     "card": {"who": "w", "delivers": ["d"],
                             "never": ["n"]},
                     "rules": "AGENTS.md", "skills": []})
                self.assertEqual(engine, profile.engine)
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            RoleProfile.model_validate(
                {"name": "ops", "agent": "ops", "engine": "nope",
                 "card": {"who": "w", "delivers": ["d"], "never": ["n"]},
                 "rules": "AGENTS.md", "skills": []})


class SandboxAgyTest(unittest.TestCase):
    def test_agyj_state_over_own_dir(self) -> None:
        from orchestration.agent_sandbox import _gemini_state_ops
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".gemini-j"
            (target / "history").mkdir(parents=True)
            (target / "state.json").write_text("{}", encoding="utf-8")
            scratch = home / "scratch"
            scratch.mkdir()
            ops = _gemini_state_ops(home, scratch, ["agyj", "--print"])
            blob = "\n".join(" ".join(op) for op in ops)
        self.assertIn(str(home / ".gemini-j" / "history"), blob)
        self.assertIn("gemini-gemini-j-state.json", blob)
        self.assertNotIn(str(home / ".gemini" / "history"), blob)

    def test_agyd_uses_default_dir(self) -> None:
        from orchestration.agent_sandbox import _gemini_state_ops
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".gemini"
            (target / "history").mkdir(parents=True)
            (target / "state.json").write_text("{}", encoding="utf-8")
            scratch = home / "scratch"
            scratch.mkdir()
            ops = _gemini_state_ops(home, scratch, ["agyd"])
            blob = "\n".join(" ".join(op) for op in ops)
        self.assertIn(str(home / ".gemini" / "history"), blob)
        self.assertIn("gemini-state.json", blob)

    def test_non_agy_gets_nothing(self) -> None:
        from orchestration.agent_sandbox import _gemini_state_ops
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ops = _gemini_state_ops(home, home, ["musej", "exec"])
        self.assertEqual([], ops)


class PromptSkillPathTest(unittest.TestCase):
    def test_skill_source_pinpoints_recipe(self) -> None:
        prompt = build_agent_prompt(
            tiny_profile("operations"), generic_task(), skill="triage",
            skill_source="/repo/skills/operations/triage")
        self.assertIn("read its SKILL.md at /repo/skills/operations/triage "
                      "and follow it", prompt)

    def test_without_source_keeps_old_text(self) -> None:
        prompt = build_agent_prompt(
            tiny_profile("operations"), generic_task(), skill="triage")
        self.assertIn("read its SKILL.md and follow it", prompt)


class RealTreeTest(unittest.TestCase):
    def test_real_cards_resolve_muse_defaults(self) -> None:
        catalog = load_catalog()
        for name, card in catalog.agents.items():
            with self.subTest(agent=name):
                if card.type.value != "ai":
                    continue
                selection = resolve_engine(card.engine, card.account, {})
                self.assertEqual("muse-code", selection.engine)
                self.assertEqual("muse", selection.account)

    def test_gate_admits_configured_engines(self) -> None:
        for engine, account in SELECTIONS:
            with self.subTest(engine=engine, account=account):
                catalog = tiny_catalog(
                    {"operations": {"enabled": True, "engine": engine,
                                    "account": account}})
                decision = resolve_agent_gate(
                    "operations", {}, None, catalog)
                self.assertTrue(decision.proceed)
                self.assertEqual("operations", decision.hint)
