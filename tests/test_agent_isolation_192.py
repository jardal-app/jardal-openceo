"""Agent isolation: whitelist env, bwrap fence, ticket folders (TASK-2026-00192).

Red-first: these tests fail on the pre-00192 blacklist behavior (child_env
inherited the whole service environment minus DEEPSEEK_API_KEY).
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.tier_s_contract import CHILD_ENV_ALLOWLIST, child_env


SERVICE_SECRETS = [
    "CLOUDFLARE_API_TOKEN", "NOTION_API_TOKEN", "ERPNEXT_WEBHOOK_SECRET",
    "JARDAL_ERP_WEBHOOK_SECRET", "JARDAL_ERP_SERVICE_TOKEN",
    "MONGODB_URL", "OPENCEO_API_KEY", "MCP_SERVER_API_KEY",
    "SMTP_ACCOUNTS_JSON", "R2_SECRET_ACCESS_KEY", "N8N_API_KEY",
    "RESEARCH_BRAVE_API_KEY", "RESEARCH_DEEP_API_KEY",
    "RESEARCH_FIRECRAWL_API_KEY", "RESEARCH_SERPER_API_KEY",
    "RESEARCH_META_API_KEY", "GROQ_API_KEY", "MUSE_API_KEY",
    "DEEPSEEK_API_KEY", "OPENCODE_GO_API_KEY", "R2_ACCESS_KEY_ID",
    "ANTHROPIC_API_KEY",
]


class ChildEnvWhitelistTests(unittest.TestCase):
    def test_no_service_secret_survives_base(self) -> None:
        base = {key: "secret-value" for key in SERVICE_SECRETS}
        base.update({"PATH": "/usr/bin:/bin", "HOME": "/home/develop"})
        merged = child_env(base, None)
        for key in SERVICE_SECRETS:
            self.assertNotIn(key, merged)
        self.assertEqual(merged["PATH"], "/usr/bin:/bin")

    def test_unknown_base_keys_are_dropped(self) -> None:
        merged = child_env({"FOO": "1", "OPENCEO_PORT": "18765",
                            "PROJECTS_DIR": "/x", "TERM": "xterm"}, None)
        self.assertNotIn("FOO", merged)
        self.assertNotIn("OPENCEO_PORT", merged)
        self.assertNotIn("PROJECTS_DIR", merged)
        self.assertEqual(merged["TERM"], "xterm")

    def test_allowlist_keys_survive(self) -> None:
        base = {key: f"v-{key}" for key in CHILD_ENV_ALLOWLIST}
        merged = child_env(base, None)
        for key in CHILD_ENV_ALLOWLIST:
            self.assertEqual(merged[key], f"v-{key}")

    def test_allowlist_has_no_secret_pattern(self) -> None:
        from agents.dev.tier_s_contract import (
            _looks_like_secret as looks_like_secret,
        )
        for key in CHILD_ENV_ALLOWLIST:
            self.assertFalse(looks_like_secret(key), key)

    def test_explicit_overlay_wins_even_for_secrets(self) -> None:
        merged = child_env(
            {"OPENCODE_GO_API_KEY": "sk-service", "PATH": "/bin"},
            {"OPENCODE_GO_API_KEY": "sk-operator",
             "DSH_HOME": "/home/develop/.dsh",
             "OPENCEO_PM_MCP_TOKEN": "badge-1",
             "OPENCEO_INTERNAL_MCP_URL": "http://127.0.0.1:18765/mcp"})
        self.assertEqual(merged["OPENCODE_GO_API_KEY"], "sk-operator")
        self.assertEqual(merged["DSH_HOME"], "/home/develop/.dsh")
        self.assertEqual(merged["OPENCEO_PM_MCP_TOKEN"], "badge-1")
        self.assertEqual(
            merged["OPENCEO_INTERNAL_MCP_URL"],
            "http://127.0.0.1:18765/mcp")

    def test_overlay_keys_are_stringified(self) -> None:
        merged = child_env({}, {"N": 3, "P": None})
        self.assertEqual(merged["N"], "3")
        self.assertEqual(merged["P"], "None")


class SandboxEnvTests(unittest.TestCase):
    def test_forced_vars_win_over_overlay(self) -> None:
        from orchestration.agent_sandbox import SANDBOX_FORCED_ENV, sandbox_env
        merged = sandbox_env(
            {"PATH": "/bin", "MUSE_NO_AUTO_UPDATE": "0"},
            {"GIT_PAGER": "less"})
        for key, value in SANDBOX_FORCED_ENV.items():
            self.assertEqual(merged[key], value)

    def test_base_allowlist_flows_through(self) -> None:
        from orchestration.agent_sandbox import sandbox_env
        merged = sandbox_env({"HOME": "/h", "TERM": "x",
                              "MONGODB_URL": "x"}, None)
        self.assertEqual(merged["HOME"], "/h")
        self.assertNotIn("MONGODB_URL", merged)


def _fake_home(root: Path) -> Path:
    home = root / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".config" / "muse").mkdir(parents=True)
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "muse").write_text("#!/bin/sh\n")
    (home / ".local" / "bin" / "api_alibaba.txt").write_text("tok")
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("key")
    (home / ".dsh" / "sessions").mkdir(parents=True)
    (home / ".claude.json").write_text("{}")
    target = home / ".local" / "bin" / "codex-real"
    target.write_text("#!/bin/sh\n")
    (home / ".local" / "bin" / "codex").symlink_to(target)
    (home / "development" / "org" / "sibling").mkdir(parents=True)
    harness = home / "development" / "org" / "harness" / "apps"
    harness.mkdir(parents=True)
    (harness / "bin.js").write_text("js")
    return home


class PlanAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = _fake_home(self.root)
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan(self, **kwargs):
        from unittest.mock import patch
        from orchestration import agent_sandbox
        kwargs.setdefault("argv", ["muse", "exec", "prompt"])
        kwargs.setdefault("workspace", self.ws)
        kwargs.setdefault("base", {"PATH": "/bin", "HOME": str(self.home)})
        kwargs.setdefault("home", self.home)
        with patch.object(agent_sandbox.BubblewrapBackend, "available", True):
            return agent_sandbox.plan_agent(**kwargs)

    def test_fails_closed_without_bwrap(self) -> None:
        from unittest.mock import patch
        from orchestration import agent_sandbox
        from research.runtimes.base import IsolationUnavailable
        with patch.object(agent_sandbox.BubblewrapBackend, "available", False):
            with self.assertRaises(IsolationUnavailable):
                agent_sandbox.plan_agent(
                    argv=["muse"], workspace=self.ws, base={}, home=self.home)

    def test_requires_existing_workspace(self) -> None:
        with self.assertRaises(ValueError):
            self._plan(workspace=self.root / "nope")

    def test_fence_shape(self) -> None:
        plan = self._plan()
        argv = plan.argv
        self.assertEqual(argv[0], "bwrap")
        for flag in ("--unshare-pid", "--share-net", "--die-with-parent"):
            self.assertIn(flag, argv)
        self.assertIn("--tmpfs", argv)
        self.assertIn("/tmp", argv)
        self.assertIn(str(self.ws), argv)
        self.assertIn(str(self.home), plan.env["HOME"])
        self.assertEqual(plan.cwd, str(self.ws))
        self.assertTrue(plan.filesystem_isolated)
        self.assertFalse(plan.egress_restricted)

    def test_motor_paths_mounted_secrets_not(self) -> None:
        plan = self._plan()
        argv = plan.argv
        blob = "\n".join(argv)
        for mounted in (".codex", ".config/muse", ".local/bin/muse",
                        ".claude.json"):
            self.assertIn(str(self.home / mounted), blob)
        for hidden in ("api_alibaba.txt", "development/org/sibling",
                       ".ssh/id_ed25519", ".config/gh"):
            self.assertNotIn(hidden, blob)

    def test_symlinked_motor_resolves(self) -> None:
        plan = self._plan()
        argv = plan.argv
        real = str(self.home / ".local/bin/codex-real")
        link = str(self.home / ".local/bin/codex")
        self.assertIn(real, argv)
        idx = argv.index("--symlink")
        self.assertEqual(argv[idx + 1], real)
        self.assertEqual(argv[idx + 2], link)

    def test_secret_dirs_shadowed(self) -> None:
        plan = self._plan()
        argv = plan.argv
        for shadowed in (".dsh/sessions",
                         ".local/share/opencode", ".local/share/muse",
                         ".cache", ".npm", ".ssh"):
            target = str(self.home / shadowed)
            self.assertIn(target, argv)
            idx = argv.index(target)
            self.assertEqual(argv[idx - 1], "--tmpfs")

    def test_opencode_carve_out_binds_state_rw(self) -> None:
        state = self.home / ".local/share/opencode"
        state.mkdir(parents=True)
        (state / "opencode.db").write_text("db")
        plan = self._plan(opencode_state_rw=True)
        argv = plan.argv
        idx = argv.index(str(state))
        self.assertEqual(argv[idx - 1], "--bind")

    def test_claude_credentials_rebound_rw_for_refresh(self) -> None:
        # TASK-2026-00442: OAuth refresh rewrites the credentials file.
        for subdir in (".claude", ".claude-j"):
            cred = self.home / subdir / ".credentials.json"
            cred.parent.mkdir(parents=True, exist_ok=True)
            cred.write_text("{}")
        plan = self._plan(argv=["claudej", "--print", "prompt"])
        argv = plan.argv
        for subdir in (".claude", ".claude-j"):
            idx = argv.index(str(self.home / subdir / ".credentials.json"))
            self.assertEqual(argv[idx - 1], "--bind")

    def test_reviewer_profile_gets_no_claude_credentials(self) -> None:
        cred = self.home / ".claude-j" / ".credentials.json"
        cred.parent.mkdir(parents=True, exist_ok=True)
        cred.write_text("{}")
        plan = self._plan(argv=["opencode", "run", "prompt"],
                          home_profile="opencode")
        self.assertNotIn(str(cred), plan.argv)

    def test_workspace_secrets_masked_when_present(self) -> None:
        (self.ws / ".env").write_text("K=V")
        (self.ws / ".runtime").mkdir()
        plan = self._plan()
        argv = plan.argv
        env_target = str(self.ws / ".env")
        self.assertIn(env_target, argv)
        idx = argv.index(env_target)
        self.assertEqual(argv[idx - 2], "--ro-bind")
        self.assertTrue(argv[idx - 1].endswith("empty"))
        rt_target = str(self.ws / ".runtime")
        self.assertIn(rt_target, argv)
        self.assertEqual(argv[argv.index(rt_target) - 1], "--tmpfs")

    def test_workspace_masks_absent_when_clean(self) -> None:
        plan = self._plan()
        blob = "\n".join(plan.argv)
        self.assertNotIn(str(self.ws / ".env"), blob)
        self.assertNotIn(str(self.ws / ".runtime"), blob)

    def test_config_args_mount_harness_root_only(self) -> None:
        tool = (self.home / "development/org/harness/apps/bin.js")
        plan = self._plan(config_args=["node", str(tool)])
        blob = "\n".join(plan.argv)
        self.assertIn(str(self.home / "development/org/harness"), blob)
        self.assertNotIn("development/org/sibling", blob)

    def test_prompt_paths_never_mount(self) -> None:
        plan = self._plan(
            argv=["muse", "exec", f"read {self.home}/.ssh/id_ed25519"],
            config_args=["muse", "exec"])
        argv = plan.argv
        for i, token in enumerate(argv):
            if token in ("--ro-bind", "--bind") and i + 2 < len(argv):
                mounted = argv[i + 1] + argv[i + 2]
                self.assertNotIn("id_ed25519", mounted)

    def test_worktree_gets_main_git_dir(self) -> None:
        import subprocess
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                       check=True, timeout=30)
        (repo / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(repo), "add", "f.txt"],
                       check=True, timeout=30)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t",
             "-c", "user.name=t", "commit", "-qm", "init"],
            check=True, timeout=30)
        wt = self.root / "wt"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach",
             str(wt), "HEAD"],
            check=True, timeout=60)
        try:
            plan = self._plan(workspace=wt)
        finally:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force",
                 str(wt)],
                check=False, timeout=60)
        self.assertIn(str(repo / ".git"), "\n".join(plan.argv))

    def test_dsh_profile_snapshot_only_with_dsh_home(self) -> None:
        profile = self.home / ".dsh" / "profiles" / "headless"
        profile.mkdir(parents=True)
        (profile / "cordis.yml").write_text("boot: 1\n")
        plain = self._plan()
        self.assertNotIn("dsh-headless", "\n".join(plain.argv))
        snap = self._plan(overlay={"DSH_HOME": str(self.home / ".dsh")})
        argv = snap.argv
        self.assertIn("dsh-headless", "\n".join(argv))
        idx = argv.index(str(profile))
        self.assertEqual(argv[idx - 2], "--bind")
        self.assertTrue(argv[idx - 1].endswith("dsh-headless"))
        mirror = Path(argv[idx - 1]) / "cordis.yml"
        self.assertEqual(mirror.read_text(), "boot: 1\n")

    def test_nested_env_masked_examples_readable(self) -> None:
        nested = self.ws / "openceo-core"
        nested.mkdir()
        (nested / ".env").write_text("K=V")
        (nested / ".env.example").write_text("K=")
        (nested / ".env.finance.local").write_text("K=V")
        pruned = self.ws / ".venv" / "lib"
        pruned.mkdir(parents=True)
        (pruned / ".env").write_text("K=V")
        deep_rt = self.ws / "sub" / ".runtime"
        deep_rt.mkdir(parents=True)
        plan = self._plan()
        blob = "\n".join(plan.argv)
        self.assertIn(str(nested / ".env"), blob)
        self.assertIn(str(nested / ".env.finance.local"), blob)
        self.assertIn(str(deep_rt), blob)
        self.assertNotIn(".env.example", blob)
        self.assertNotIn(str(pruned / ".env"), blob)

    def test_config_root_inside_workspace_skipped(self) -> None:
        tool = self.ws / "tool.sh"
        tool.write_text("#!/bin/sh\n")
        plan = self._plan(config_args=[str(tool)])
        blob = "\n".join(plan.argv)
        self.assertNotIn("tool.sh", blob)

    def test_config_root_ancestor_of_workspace_skipped(self) -> None:
        plan = self._plan(config_args=[str(self.root)])
        blob = "\n".join(plan.argv)
        self.assertNotIn(f"--ro-bind\n{self.root}\n{self.root}", blob)

    def test_config_symlink_motor_recreated(self) -> None:
        link = self.home / ".local" / "bin" / "codex"
        plan = self._plan(config_args=[str(link)])
        argv = plan.argv
        real = str(self.home / ".local" / "bin" / "codex-real")
        self.assertIn(real, argv)
        idx = argv.index("--symlink")
        self.assertEqual(argv[idx + 1], real)
        self.assertEqual(argv[idx + 2], str(link))

    def test_config_symlink_outside_home_not_recreated(self) -> None:
        target = self.root / "tool-real"
        target.write_text("x")
        link = self.root / "tool-link"
        link.symlink_to(target)
        plan = self._plan(config_args=[str(link)])
        argv = plan.argv
        self.assertIn(str(target), argv)
        self.assertNotIn(str(link), argv)

    def test_gemini_state_only_for_agy(self) -> None:
        gemini = self.home / ".gemini"
        (gemini / "history").mkdir(parents=True)
        (gemini / "state.json").write_text("{}")
        (gemini / "google_accounts.json").write_text("{}")
        plain = self._plan(argv=["muse", "exec"])
        self.assertNotIn("gemini-state.json", "\n".join(plain.argv))
        agy = self._plan(argv=["agyd", "--print", "hi"])
        blob = "\n".join(agy.argv)
        self.assertIn(str(gemini / "history"), blob)
        self.assertIn("gemini-state.json", blob)
        self.assertIn("gemini-google_accounts.json", blob)

    def test_bad_extra_op_fails_closed(self) -> None:
        from research.runtimes.isolation import BubblewrapBackend
        backend = BubblewrapBackend()
        with self.assertRaises(ValueError):
            backend.plan(argv=["x"], workspace=self.ws,
                         worker_home=self.ws, worker_config=self.ws,
                         env={}, allow_network=True,
                         extra_ops=[["--evil", "x"]])


class TicketDirTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "work"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roundtrip(self) -> None:
        import os
        from orchestration.agent_sandbox import (
            ensure_ticket_dir, remove_ticket_dir,
        )
        path = ensure_ticket_dir("TASK-2026-00192", base_dir=self.base)
        self.assertTrue(path.is_dir())
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o700")
        (path / "note.txt").write_text("x")
        remove_ticket_dir("TASK-2026-00192", base_dir=self.base)
        self.assertFalse(path.exists())

    def test_invalid_id_rejected(self) -> None:
        from orchestration.agent_sandbox import ensure_ticket_dir
        for bad in ("", "../etc", "a/b", "a b", "TASK 1"):
            with self.assertRaises(ValueError, msg=bad):
                ensure_ticket_dir(bad, base_dir=self.base)

    def test_scratch_roundtrip(self) -> None:
        from orchestration.agent_sandbox import (
            cleanup_sandbox_scratch, ensure_sandbox_scratch,
        )
        ws = self.base / "ws"
        ws.mkdir(parents=True)
        scratch = ensure_sandbox_scratch(ws)
        self.assertTrue((scratch / "empty").is_file())
        cleanup_sandbox_scratch(ws)
        self.assertFalse(scratch.exists())


class SandboxedShellTests(unittest.TestCase):
    def test_shape(self) -> None:
        import tempfile
        from unittest.mock import patch
        from orchestration import agent_sandbox
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _fake_home(root)
            ws = root / "ws"
            ws.mkdir()
            with patch.object(
                    agent_sandbox.BubblewrapBackend, "available", True):
                line = agent_sandbox.sandboxed_shell_command(
                    command=["muse", "exec"], workspace=ws,
                    base={"PATH": "/bin", "HOME": str(home)}, home=home)
        self.assertTrue(line.startswith("env -i "))
        self.assertIn(" bwrap ", line)
        self.assertIn("MUSE_NO_AUTO_UPDATE=1", line)


BWRAP_AVAILABLE = shutil.which("bwrap") is not None
MUSE_AVAILABLE = shutil.which("muse") is not None


@unittest.skipUnless(
    BWRAP_AVAILABLE and MUSE_AVAILABLE,
    "bwrap + host motors required (host capability)")
class SandboxComponentTests(unittest.TestCase):
    def _run(self, argv, workspace, base, **kwargs):
        import subprocess
        from orchestration.agent_sandbox import plan_agent
        plan = plan_agent(argv=argv, workspace=workspace, base=base,
                          **kwargs)
        try:
            return subprocess.run(
                plan.argv, env=plan.env, cwd=plan.cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=180)
        finally:
            from orchestration.agent_sandbox import cleanup_sandbox_scratch
            cleanup_sandbox_scratch(workspace)

    def test_env_clean_secrets_unreadable_toolchain_works(self) -> None:
        import os
        import tempfile
        home = Path(os.path.expanduser("~"))
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            probe = (
                "test -z \"$ISOLATION_CANARY_X9\" && echo CANARY-ABSENT; "
                f"cat {home}/.ssh/id_ed25519 2>/dev/null && echo SSH-READABLE; "
                f"cat {CORE.parent}/openceo-core/.env 2>/dev/null"
                " && echo DOTENV-READABLE; "
                "touch probe-ok && echo WS-WRITABLE; "
                "python3 -c 'print(40 + 2)'")
            proc = self._run(
                ["sh", "-c", probe], ws,
                {"PATH": "/usr/bin:/bin", "HOME": str(home),
                 "ISOLATION_CANARY_X9": "leak",
                 "MONGODB_URL": "mongodb://secret"})
        out = proc.stdout
        self.assertIn("CANARY-ABSENT", out)
        self.assertNotIn("SSH-READABLE", out)
        self.assertNotIn("DOTENV-READABLE", out)
        self.assertIn("WS-WRITABLE", out)
        self.assertIn("42", out)

    def test_nested_env_unreadable_in_checkout_like_workspace(self) -> None:
        import os
        import tempfile
        home = Path(os.path.expanduser("~"))
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            core = ws / "openceo-core"
            core.mkdir(parents=True)
            (core / ".env").write_text("K=secret\n")
            (core / ".env.example").write_text("K=\n")
            proc = self._run(
                ["sh", "-c",
                 "echo NESTED-BEGIN; cat openceo-core/.env 2>/dev/null; "
                 "echo NESTED-END; cat openceo-core/.env.example 2>/dev/null"],
                ws, {"PATH": "/usr/bin:/bin", "HOME": str(home)})
        self.assertNotIn("K=secret", proc.stdout)
        self.assertIn("K=", proc.stdout)

    def test_dns_resolves_inside(self) -> None:
        import os
        import tempfile
        home = Path(os.path.expanduser("~"))
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            proc = self._run(["getent", "hosts", "localhost"], ws,
                             {"PATH": "/usr/bin:/bin", "HOME": str(home)})
        self.assertEqual(proc.returncode, 0, proc.stdout)

    def test_muse_echo_starts_fenced(self) -> None:
        import os
        import tempfile
        home = Path(os.path.expanduser("~"))
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            proc = self._run(
                ["muse", "exec", "--provider", "echo", "hola-fenced"],
                ws, {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "HOME": str(home)})
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:])
        self.assertIn("hola-fenced", proc.stdout)

    def test_peer_motors_start_fenced(self) -> None:
        import os
        import tempfile
        home = Path(os.path.expanduser("~"))
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home)}
        cases = []
        for binary, args in (("codex", ["--version"]),
                             ("claude", ["--version"]),
                             ("qwen", ["--version"]),
                             ("opencode", ["--version"]),
                             ("agy", ["--version"]),
                             ("kimi", ["--version"])):
            if shutil.which(binary) is not None:
                cases.append(([binary, *args], {}))
        harness = (home / "development/Jardal/jardal-deepseek-harness"
                   / "apps/cli/lib/bin.js")
        if harness.is_file():
            cases.append((["node", str(harness), "--help"],
                          {"config_args": ["node", str(harness)]}))
        self.assertTrue(cases, "no peer motor to probe")
        for argv, extra in cases:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp) / "ws"
                ws.mkdir()
                with self.subTest(argv=argv[0]):
                    proc = self._run(argv, ws, base, **extra)
            self.assertEqual(proc.returncode, 0, proc.stdout[-1000:])


class LiveExecutorFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_refusal_without_bwrap(self) -> None:
        import tempfile
        from unittest.mock import patch
        from orchestration import agent_sandbox
        from orchestration.live import ProcessExecutor
        roles = {"t": {"command": ["/bin/echo"], "prompt_via": "argv"}}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", False):
                result = await ProcessExecutor(roles=roles).run(
                    "t", "hi", Path(tmp), 30)
        self.assertEqual(result.returncode, 1)
        self.assertIn("sandbox refused", result.output_tail)

    @unittest.skipUnless(BWRAP_AVAILABLE, "bwrap required (host capability)")
    async def test_stub_runs_fenced_without_leak(self) -> None:
        import os
        import tempfile
        from orchestration.live import ProcessExecutor
        roles = {"t": {"command": ["/bin/sh", "-c", "echo hi-$FOO"],
                       "prompt_via": "argv"}}
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FOO")
            os.environ["FOO"] = "secret"
            try:
                result = await ProcessExecutor(roles=roles).run(
                    "t", "prompt-text", Path(tmp), 30)
            finally:
                if old is None:
                    del os.environ["FOO"]
                else:
                    os.environ["FOO"] = old
        self.assertEqual(result.returncode, 0, result.output_tail)
        self.assertIn("hi-", result.output_tail)
        self.assertNotIn("secret", result.output_tail)


class RunProcessDetailedFenceTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        from unittest.mock import MagicMock
        from agents.dev.session import DevSessionManager
        from settings import Settings
        return DevSessionManager(Settings(), MagicMock())

    async def test_refusal_without_bwrap(self) -> None:
        import tempfile
        from unittest.mock import patch
        from orchestration import agent_sandbox
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", False):
                result = await self._manager()._run_process_detailed(
                    ["/bin/echo", "hi"], cwd=tmp)
        self.assertFalse(result.ok)
        self.assertIn("sandbox refused", result.stderr)

    @unittest.skipUnless(BWRAP_AVAILABLE, "bwrap required (host capability)")
    async def test_stub_runs_fenced(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "stub.sh"
            exe.write_text("#!/bin/sh\necho STUB-OK\n")
            exe.chmod(0o755)
            result = await self._manager()._run_process_detailed(
                [str(exe)], cwd=tmp)
        self.assertTrue(result.ok, result.stderr)
        self.assertIn("STUB-OK", result.stdout)


@unittest.skipUnless(BWRAP_AVAILABLE, "bwrap required (host capability)")
class TmuxRestartFenceTests(unittest.TestCase):
    def test_restart_types_fenced_line(self) -> None:
        import tempfile
        from unittest.mock import MagicMock
        from agents.dev.tmux_runtime import TmuxRuntime
        with tempfile.TemporaryDirectory() as tmp:
            pane = MagicMock()
            TmuxRuntime._restart_interactive_command(
                pane, ["muse", "exec"], tmp, {"A": "b"})
        line = pane.send_keys.call_args[0][0]
        self.assertIn("env -i ", line)
        self.assertIn(" bwrap ", line)
        self.assertIn("MUSE_NO_AUTO_UPDATE=1", line)


class TmuxShellDefaultsTests(unittest.TestCase):
    def test_term_lang_default_when_missing(self) -> None:
        import tempfile
        from unittest.mock import patch
        from orchestration import agent_sandbox
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _fake_home(root)
            ws = root / "ws"
            ws.mkdir()
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", True):
                bare = agent_sandbox.sandboxed_shell_command(
                    command=["muse"], workspace=ws,
                    base={"PATH": "/bin", "HOME": str(home)}, home=home)
                kept = agent_sandbox.sandboxed_shell_command(
                    command=["muse"], workspace=ws,
                    base={"PATH": "/bin", "HOME": str(home),
                          "TERM": "xterm", "LANG": "en_US.UTF-8"},
                    home=home)
        self.assertIn("TERM=tmux-256color", bare)
        self.assertIn("LANG=C.UTF-8", bare)
        self.assertIn("TERM=xterm", kept)
        self.assertIn("LANG=en_US.UTF-8", kept)

    def test_start_failure_orphans_no_session(self) -> None:
        import tempfile
        from unittest.mock import MagicMock, patch
        from agents.dev.tmux_runtime import TmuxRuntime
        from settings import Settings
        from state import DevProvider
        runtime = TmuxRuntime(Settings())
        from research.runtimes.base import IsolationUnavailable
        with tempfile.TemporaryDirectory() as tmp:
            server = MagicMock()
            runtime._server = server
            with patch.object(
                    TmuxRuntime, "has_session_name", return_value=False):
                from orchestration import agent_sandbox
                with patch.object(agent_sandbox.BubblewrapBackend,
                                  "available", False):
                    with self.assertRaises(IsolationUnavailable):
                        runtime.start_session(
                            project="p", provider=DevProvider.MUSE,
                            command=["muse"], cwd=tmp)
        server.new_session.assert_not_called()


class RateMonitorFenceTests(unittest.TestCase):
    def _monitor(self, server):
        from unittest.mock import patch
        from settings import Settings
        import rate_monitor
        monitor = rate_monitor.RateMonitor(Settings(
            CODEXJ_CLI_PATH="/bin/echo-codex",
            CLAUDE_CODE_PATH="/bin/echo-claude",
            MUSE_CLI_PATH="/bin/echo-muse"))
        monitor._server = server
        return monitor

    def test_create_types_fenced_line(self) -> None:
        import tempfile
        from unittest.mock import MagicMock, patch
        from orchestration import agent_sandbox
        server = MagicMock()
        server.sessions.get.return_value = None
        pane = MagicMock()
        session = MagicMock()
        session.active_window.active_pane = pane
        server.new_session.return_value = session
        monitor = self._monitor(server)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "work"
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", True), \
                 patch.object(agent_sandbox, "runtime_base_dir",
                              return_value=base), \
                 patch("rate_monitor.time.sleep"):
                got = monitor._get_or_create_monitor_session("muse")
        self.assertIs(got, session)
        line = pane.send_keys.call_args[0][0]
        self.assertIn("env -i ", line)
        self.assertIn(" bwrap ", line)
        self.assertIn("dev-monitor-muse", monitor._owned_sessions)

    def test_prefence_session_reaped_once(self) -> None:
        from unittest.mock import MagicMock, patch
        from orchestration import agent_sandbox
        stale = MagicMock()
        server = MagicMock()
        server.sessions.get.return_value = stale
        fresh = MagicMock()
        server.new_session.return_value = fresh
        monitor = self._monitor(server)
        with patch.object(agent_sandbox.BubblewrapBackend,
                          "available", True), \
             patch.object(agent_sandbox, "ensure_ticket_dir") as ensured, \
             patch.object(agent_sandbox, "sandboxed_shell_command",
                          return_value="env -i bwrap x"), \
             patch("rate_monitor.time.sleep"):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                ensured.return_value = Path(tmp)
                monitor._get_or_create_monitor_session("codex")
        stale.kill_session.assert_called_once_with()
        self.assertIn("dev-monitor-codex", monitor._owned_sessions)


if __name__ == "__main__":
    unittest.main()
