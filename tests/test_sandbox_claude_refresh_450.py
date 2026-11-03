"""Claude OAuth refresh inside the sandbox (TASK-2026-00450).

Claude Code locks ``<config dir>/.oauth_refresh.lock`` before refreshing;
on a read-only config dir the lock fails and every post-expiry run gets a
401. Synthetic homes only: no real credentials, no network, no models.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from orchestration import agent_sandbox
from orchestration.agent_sandbox import (
    cleanup_sandbox_scratch,
    user_instruction_masks,
)


def _fake_home(root: Path) -> Path:
    home = root / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("OWNER RULES\n")
    (home / ".claude" / ".credentials.json").write_text('{"t": "old"}')
    (home / ".claude.json").write_text('{"numStartups": 1}')
    claude_j = home / ".claude-j"
    (claude_j / "projects").mkdir(parents=True)
    (claude_j / "settings.json").write_text('{"model": "opus"}')
    (claude_j / ".credentials.json").write_text('{"t": "old"}')
    (claude_j / ".claude.json").write_text('{"oauthAccount": {}}')
    (claude_j / "CLAUDE.md").symlink_to(home / ".claude" / "CLAUDE.md")
    (claude_j / "skills").symlink_to(home / ".claude" / "skills")
    return home


def _plan(home: Path, ws: Path, argv: list[str], **kwargs):
    with patch.object(agent_sandbox.BubblewrapBackend, "available", True):
        return agent_sandbox.plan_agent(
            argv=argv, workspace=ws,
            base={"PATH": "/usr/bin:/bin", "HOME": str(home)},
            home=home, **kwargs)


def _ops(argv: list[str]) -> list[tuple[str, ...]]:
    """(op, src, dest) / (op, dest) tuples of the mount ops, in order."""
    found = []
    for index, token in enumerate(argv):
        if token in ("--bind", "--ro-bind", "--symlink"):
            found.append((token, argv[index + 1], argv[index + 2]))
        elif token == "--tmpfs":
            found.append((token, argv[index + 1]))
    return found


class ClaudeConfigDirPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = _fake_home(self.root)
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        cleanup_sandbox_scratch(self.ws)
        self._tmp.cleanup()

    def test_config_dir_is_a_tmpfs_over_read_only_entries(self) -> None:
        dir_j = self.home / ".claude-j"
        (dir_j / ".oauth_refresh.lock").mkdir()  # host caught mid-refresh
        ops = _ops(_plan(self.home, self.ws, ["claudej", "-p", "x"]).argv)
        tmpfs = ops.index(("--tmpfs", str(dir_j)))
        settings = str(dir_j / "settings.json")
        self.assertGreater(ops.index(("--ro-bind", settings, settings)),
                           tmpfs)
        projects = str(dir_j / "projects")
        self.assertIn(("--ro-bind", projects, projects), ops)
        # Aliases stay symlinks: the masks cover the real file only.
        self.assertIn(("--symlink", str(self.home / ".claude" / "CLAUDE.md"),
                       str(dir_j / "CLAUDE.md")), ops)
        self.assertNotIn(str(dir_j / ".oauth_refresh.lock"),
                         {op[-1] for op in ops})
        cred = str(dir_j / ".credentials.json")
        self.assertGreater(ops.index(("--bind", cred, cred)), tmpfs)

    def test_global_configs_get_writable_copies(self) -> None:
        ops = _ops(_plan(self.home, self.ws, ["claudej", "-p", "x"]).argv)
        for config in (self.home / ".claude-j" / ".claude.json",
                       self.home / ".claude.json"):
            [copy] = [op[1] for op in ops
                      if op[0] == "--bind" and op[2] == str(config)]
            self.assertNotEqual(copy, str(config))
            self.assertEqual(Path(copy).read_bytes(), config.read_bytes())
            self.assertEqual(os.stat(copy).st_mode & 0o777, 0o600)
        home_config = str(self.home / ".claude.json")
        self.assertLess(ops.index(("--ro-bind", home_config, home_config)),
                        [i for i, op in enumerate(ops)
                         if op[0] == "--bind" and op[2] == home_config][0])

    def test_invalid_global_config_stays_read_only(self) -> None:
        config = self.home / ".claude-j" / ".claude.json"
        config.write_text("{not json")
        ops = _ops(_plan(self.home, self.ws, ["claudej", "-p", "x"]).argv)
        self.assertIn(("--ro-bind", str(config), str(config)), ops)
        self.assertNotIn("--bind", {op[0] for op in ops
                                    if op[-1] == str(config)})

    def test_reviewer_profiles_get_no_claude_dir(self) -> None:
        argv = _plan(self.home, self.ws, ["opencode", "run", "x"],
                     home_profile="opencode").argv
        self.assertNotIn(str(self.home / ".claude-j"), argv)
        self.assertNotIn(str(self.home / ".claude.json"), argv)


@unittest.skipUnless(shutil.which("bwrap") is not None,
                     "bwrap required (host capability)")
class ClaudeRefreshEnforcementTests(unittest.TestCase):
    def test_lock_and_credentials_write_while_entries_stay_read_only(
            self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _fake_home(root)
            ws = root / "ws"
            ws.mkdir()
            dir_j = home / ".claude-j"
            probe = (
                f"mkdir {dir_j}/.oauth_refresh.lock && echo LOCK-OK; "
                f"printf refreshed > {dir_j}/.credentials.json "
                f"&& echo CRED-OK; "
                f"echo x > {dir_j}/settings.json 2>/dev/null "
                f"&& echo SETTINGS-WRITABLE; "
                f"touch {dir_j}/projects/x 2>/dev/null "
                f"&& echo PROJECTS-WRITABLE; "
                f"touch {dir_j}/new-entry && echo NEW-OK; "
                f"echo '{{}}' > {dir_j}/.claude.json && echo CONFIG-OK; "
                f"echo MASK:$(cat {dir_j}/CLAUDE.md):END; "
                f"ls {dir_j}/skills >/dev/null && echo SKILLS-OK; true")
            plan = _plan(home, ws, ["sh", "-c", probe],
                         home_file_overlays=user_instruction_masks(home))
            try:
                proc = subprocess.run(
                    plan.argv, env=plan.env, cwd=plan.cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", timeout=120)
            finally:
                cleanup_sandbox_scratch(ws)
            out = proc.stdout
            for marker in ("LOCK-OK", "CRED-OK", "NEW-OK", "CONFIG-OK",
                           "MASK::END", "SKILLS-OK"):
                self.assertIn(marker, out)
            self.assertNotIn("SETTINGS-WRITABLE", out)
            self.assertNotIn("PROJECTS-WRITABLE", out)
            # The credentials reach the host; nothing else does.
            self.assertEqual((dir_j / ".credentials.json").read_text(),
                             "refreshed")
            self.assertEqual(json.loads((dir_j / "settings.json")
                                        .read_text()), {"model": "opus"})
            self.assertEqual(json.loads((dir_j / ".claude.json")
                                        .read_text()), {"oauthAccount": {}})
            self.assertFalse((dir_j / ".oauth_refresh.lock").exists())
            self.assertFalse((dir_j / "new-entry").exists())
            self.assertFalse((dir_j / "projects" / "x").exists())


if __name__ == "__main__":
    unittest.main()
