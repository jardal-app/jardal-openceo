"""Dev reads the active project vault read-only (TASK-2026-00328, D6).

Synthetic vaults under tmp dirs only: never the real vault, never other
projects, never shared/. No tokens.
"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from orchestration.agent_sandbox import vault_project_read_root


def _vault(root: Path) -> Path:
    active = root / "vault" / "projects" / "org" / "active"
    other = root / "vault" / "projects" / "org" / "other"
    (active / "practices").mkdir(parents=True)
    (active / "active.md").write_text("# hub\n")
    (active / "practices" / "notes.md").write_text("# practice\n")
    (other).mkdir(parents=True)
    (other / "other.md").write_text("# other\n")
    (root / "vault" / "shared").mkdir(parents=True)
    (root / "vault" / "inbox").mkdir(parents=True)
    return root / "vault"


def _cfg(vault: Path, project: str = "projects/org/active") -> dict:
    return {"dev": {"vault_project": {"dir": str(vault), "project": project}}}


class VaultProjectReadRootTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = _vault(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_active_project_resolves(self) -> None:
        roots = vault_project_read_root(_cfg(self.vault))
        self.assertEqual(roots, [str(self.vault / "projects/org/active")])

    def test_only_active_project_returned(self) -> None:
        roots = vault_project_read_root(_cfg(self.vault))
        self.assertEqual(len(roots), 1)
        for forbidden in ("other", "shared", "inbox"):
            self.assertNotIn(forbidden, roots[0])

    def test_missing_vault_project_is_noop(self) -> None:
        for cfg in (None, {}, {"dev": {}}, {"dev": {"vault_project": None}},
                    {"dev": {"vault_project": {"dir": "", "project": ""}}},
                    {"dev": {"vault_project": "nope"}},
                    {"dev": {"vault_project": {"dir": str(self.vault)}}},
                    "not-a-mapping"):
            self.assertEqual(vault_project_read_root(cfg), [], msg=repr(cfg))

    def test_missing_dir_is_noop(self) -> None:
        cfg = {"dev": {"vault_project": {
            "dir": str(self.vault / "nope"), "project": "projects/org/active"}}}
        self.assertEqual(vault_project_read_root(cfg), [])

    def test_missing_project_dir_is_noop(self) -> None:
        self.assertEqual(
            vault_project_read_root(_cfg(self.vault, "projects/org/ghost")),
            [])

    def test_dotdot_escape_rejected(self) -> None:
        self.assertEqual(
            vault_project_read_root(_cfg(self.vault, "projects/org/active/../../..")),
            [])

    def test_absolute_project_escape_rejected(self) -> None:
        cfg = {"dev": {"vault_project": {
            "dir": str(self.vault), "project": "/etc"}}}
        self.assertEqual(vault_project_read_root(cfg), [])

    def test_symlink_escape_rejected(self) -> None:
        link = self.vault / "projects" / "org" / "link"
        link.symlink_to(Path("/etc"))
        self.assertEqual(
            vault_project_read_root(_cfg(self.vault, "projects/org/link")), [])

    def test_file_target_rejected(self) -> None:
        target = self.vault / "projects" / "org" / "active" / "active.md"
        rel = target.relative_to(self.vault)
        self.assertEqual(vault_project_read_root(_cfg(self.vault, str(rel))), [])

    def test_dot_project_rejected(self) -> None:
        self.assertEqual(vault_project_read_root(_cfg(self.vault, ".")), [])


class VaultMountPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = _vault(self.root)
        self.home = self.root / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.ws = self.root / "ws"
        self.ws.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _plan(self, roots):
        from orchestration import agent_sandbox
        with patch.object(agent_sandbox.BubblewrapBackend, "available", True):
            return agent_sandbox.plan_agent(
                argv=["muse", "exec", "prompt"], workspace=self.ws,
                base={"PATH": "/bin", "HOME": str(self.home)},
                home=self.home, extra_read_roots=roots)

    def test_active_project_mounts_read_only(self) -> None:
        target = str(self.vault / "projects/org/active")
        plan = self._plan(vault_project_read_root(_cfg(self.vault)))
        argv = plan.argv
        idx = argv.index(target)
        self.assertEqual(argv[idx - 1], "--ro-bind")
        pairs = [(argv[i - 1], argv[i])
                 for i, token in enumerate(argv) if token == target]
        self.assertTrue(pairs)
        for flag, _dest in pairs:
            self.assertNotEqual(flag, "--bind")

    def test_other_project_shared_inbox_never_mount(self) -> None:
        target = str(self.vault / "projects/org/active")
        plan = self._plan([target])
        blob = "\n".join(plan.argv)
        for forbidden in ("projects/org/other",
                          "shared", "inbox"):
            self.assertNotIn(str(self.vault / forbidden), blob)

    def test_no_vault_project_no_mount(self) -> None:
        plan = self._plan(vault_project_read_root({"dev": {}}))
        blob = "\n".join(plan.argv)
        self.assertNotIn(str(self.vault), blob)


class TierSRolesWithVaultTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = _vault(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_roles_gain_read_root_pool_untouched(self) -> None:
        from agents.dev.session import _tier_s_roles_with_vault
        target = str(self.vault / "projects/org/active")
        roles = {"builder": {"command": ["muse"]},
                 "closer": {"command": ["muse"],
                            "extra_read_roots": ["/opt/toolchain"]}}
        mounted = _tier_s_roles_with_vault(roles, _cfg(self.vault))
        self.assertEqual(
            mounted["builder"]["extra_read_roots"], [target])
        self.assertEqual(
            mounted["closer"]["extra_read_roots"], ["/opt/toolchain", target])
        self.assertEqual(mounted["builder"]["command"], ["muse"])
        self.assertNotIn("extra_read_roots", roles["builder"])
        self.assertEqual(roles["closer"]["extra_read_roots"],
                         ["/opt/toolchain"])

    def test_no_double_mount(self) -> None:
        from agents.dev.session import _tier_s_roles_with_vault
        target = str(self.vault / "projects/org/active")
        roles = {"builder": {"extra_read_roots": [target]}}
        mounted = _tier_s_roles_with_vault(roles, _cfg(self.vault))
        self.assertEqual(mounted["builder"]["extra_read_roots"], [target])

    def test_without_vault_specs_unchanged(self) -> None:
        from agents.dev.session import _tier_s_roles_with_vault
        roles = {"builder": {"command": ["muse"]}}
        mounted = _tier_s_roles_with_vault(roles, {"dev": {}})
        self.assertEqual(mounted, roles)
        self.assertIsNot(mounted, roles)


@unittest.skipUnless(
    shutil.which("bwrap") is not None,
    "bwrap required (host capability)")
class VaultMountEnforcementTests(unittest.TestCase):
    def test_read_own_deny_other_and_write(self) -> None:
        import subprocess
        import tempfile
        from orchestration import agent_sandbox
        from orchestration.agent_sandbox import cleanup_sandbox_scratch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _vault(root)
            active = vault / "projects/org/active"
            home = root / "home"
            home.mkdir()
            ws = root / "ws"
            ws.mkdir()
            probe = (
                f"cat {active}/active.md && "
                f"cat {active}/practices/notes.md && "
                f"echo OWN-READABLE; "
                f"cat {vault}/projects/org/other/other.md "
                f"2>/dev/null && echo OTHER-READABLE; "
                f"touch {active}/no-write 2>/dev/null "
                f"&& echo WRITE-OK; "
                f"true")
            with patch.object(agent_sandbox.BubblewrapBackend,
                              "available", True):
                plan = agent_sandbox.plan_agent(
                    argv=["sh", "-c", probe], workspace=ws,
                    base={"PATH": "/usr/bin:/bin",
                          "HOME": str(home)},
                    home=home,
                    extra_read_roots=vault_project_read_root(
                        _cfg(vault)))
            try:
                proc = subprocess.run(
                    plan.argv, env=plan.env, cwd=plan.cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", timeout=120)
            finally:
                cleanup_sandbox_scratch(ws)
        out = proc.stdout
        self.assertIn("# hub", out)
        self.assertIn("# practice", out)
        self.assertIn("OWN-READABLE", out)
        self.assertNotIn("OTHER-READABLE", out)
        self.assertNotIn("WRITE-OK", out)


if __name__ == "__main__":
    unittest.main()
