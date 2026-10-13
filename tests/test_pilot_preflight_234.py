"""Pilot 1 preflight checks (TASK-2026-00234, scope 3).

Fake probes per check, plus live hermetic probes (catalog, MCP,
skill dry-run, roster). No tokens anywhere.
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from pilot_preflight import (
    check_catalog_skills,
    check_credentials,
    check_health,
    check_mcp_identity,
    check_repo_main_sync,
    check_roster_clis,
    check_single_instance,
    check_skill_dry_run,
    required_binaries,
    run_preflight,
)

REPO = Path(__file__).resolve().parents[1]

_SS_ONE = (
    "tcp LISTEN 0 128 0.0.0.0:18765 0.0.0.0:* "
    'users:(("uvicorn",pid=2041992,fd=3))'
)
_SS_OTHER = (
    "tcp LISTEN 0 128 0.0.0.0:18765 0.0.0.0:* "
    'users:(("other",pid=999,fd=3))'
)
_VENV = "/repo/.venv/bin/python"
_PROC_ONE = {2041992: f"{_VENV} main.py"}
_PROC_TWO = {2041992: f"{_VENV} main.py", 111: f"{_VENV} main.py"}


def _single(**kwargs: Any) -> Any:
    kwargs.setdefault("venv_python", _VENV)
    return check_single_instance(**kwargs)


def _git_ok(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    del cwd
    if list(args[:2]) == ["rev-parse", "--abbrev-ref"]:
        return 0, "main"
    if list(args[:1]) == ["status"]:
        return 0, ""
    return 0, "abc1234def5678"


class SingleInstanceTests(unittest.TestCase):
    def test_one_owner_is_ok(self) -> None:
        check = _single(proc_cmdlines=dict(_PROC_ONE), ss_text=_SS_ONE)
        self.assertTrue(check.ok)
        self.assertIn("2041992", check.detail)

    def test_zero_instances_fails(self) -> None:
        check = _single(proc_cmdlines={}, ss_text=_SS_ONE)
        self.assertFalse(check.ok)
        self.assertIn("0 instancias", check.detail)

    def test_two_instances_fail(self) -> None:
        check = _single(proc_cmdlines=dict(_PROC_TWO), ss_text=_SS_ONE)
        self.assertFalse(check.ok)
        self.assertIn("2 instancias", check.detail)

    def test_other_python_ignored(self) -> None:
        procs = {7: "/usr/bin/python /tmp/probe.py",
                 2041992: f"{_VENV} main.py"}
        check = _single(proc_cmdlines=procs, ss_text=_SS_ONE)
        self.assertTrue(check.ok, check.detail)

    def test_foreign_port_owner_fails(self) -> None:
        check = _single(proc_cmdlines=dict(_PROC_ONE), ss_text=_SS_OTHER)
        self.assertFalse(check.ok)
        self.assertIn("999", check.detail)

    def test_missing_ss_fails_exact(self) -> None:
        check = _single(proc_cmdlines=dict(_PROC_ONE), ss_text="")
        self.assertFalse(check.ok)
        self.assertIn("ss no disponible", check.detail)


class RepoSyncTests(unittest.TestCase):
    def test_main_clean_and_level_is_ok(self) -> None:
        self.assertTrue(check_repo_main_sync(run=_git_ok).ok)

    def test_wrong_branch_fails(self) -> None:
        def run(args: Sequence[str], cwd: Path) -> tuple[int, str]:
            if list(args[:2]) == ["rev-parse", "--abbrev-ref"]:
                return 0, "feat/x"
            return _git_ok(args, cwd)

        check = check_repo_main_sync(run=run)
        self.assertFalse(check.ok)
        self.assertIn("feat/x", check.detail)

    def test_dirty_tree_fails(self) -> None:
        def run(args: Sequence[str], cwd: Path) -> tuple[int, str]:
            if list(args[:1]) == ["status"]:
                return 0, " M openceo-core/x.py\n"
            return _git_ok(args, cwd)

        check = check_repo_main_sync(run=run)
        self.assertFalse(check.ok)
        self.assertIn("sucio", check.detail)

    def test_status_failure_is_unreadable_repo(self) -> None:
        def run(args: Sequence[str], cwd: Path) -> tuple[int, str]:
            if list(args[:1]) == ["status"]:
                return 128, ""
            return _git_ok(args, cwd)

        check = check_repo_main_sync(run=run)
        self.assertFalse(check.ok)
        self.assertIn("ilegible", check.detail)

    def test_head_failure_is_unreadable_repo(self) -> None:
        def run(args: Sequence[str], cwd: Path) -> tuple[int, str]:
            if list(args) == ["rev-parse", "HEAD"]:
                return 128, ""
            return _git_ok(args, cwd)

        check = check_repo_main_sync(run=run)
        self.assertFalse(check.ok)
        self.assertIn("ilegible", check.detail)

    def test_missing_origin_ref_fails(self) -> None:
        def run(args: Sequence[str], cwd: Path) -> tuple[int, str]:
            if args[-1] == "refs/remotes/origin/main":
                return 1, ""
            return _git_ok(args, cwd)

        check = check_repo_main_sync(run=run)
        self.assertFalse(check.ok)
        self.assertIn("origin/main", check.detail)

    def test_diverged_main_fails(self) -> None:
        def run(args: Sequence[str], cwd: Path) -> tuple[int, str]:
            if args[-1] == "refs/remotes/origin/main":
                return 0, "fffffff0000000"
            return _git_ok(args, cwd)

        check = check_repo_main_sync(run=run)
        self.assertFalse(check.ok)
        self.assertIn("!=", check.detail)


class CatalogSkillsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("muse"), "muse required")
    def test_live_catalog_and_skill_validate(self) -> None:
        check = check_catalog_skills()
        self.assertTrue(check.ok, check.detail)

    def test_validate_disables_muse_autoupdate(self) -> None:
        seen: dict[str, Any] = {}

        def run(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        check = check_catalog_skills(
            which=lambda name: "/bin/muse" if name == "muse" else None,
            run=run)
        self.assertTrue(check.ok, check.detail)
        self.assertEqual(seen["env"]["MUSE_NO_AUTO_UPDATE"], "1")

    def test_missing_muse_fails(self) -> None:
        check = check_catalog_skills(which=lambda name: None)
        self.assertFalse(check.ok)
        self.assertIn("muse", check.detail)

    def test_failed_validate_fails(self) -> None:
        def run(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return SimpleNamespace(returncode=1, stdout="", stderr="bad")

        check = check_catalog_skills(
            which=lambda name: "/bin/muse" if name == "muse" else None,
            run=run)
        self.assertFalse(check.ok)
        self.assertIn("validate", check.detail)


class McpIdentityTests(unittest.TestCase):
    def test_live_in_process_probe(self) -> None:
        check = check_mcp_identity()
        self.assertTrue(check.ok, check.detail)
        self.assertIn("6 tools", check.detail)


class RosterClisTests(unittest.TestCase):
    def test_walker_collects_skips_inactive_and_absolutes(self) -> None:
        config = {
            "roles": {
                "builder": {"active": True,
                            "command": ["musej", "exec"]},
                "retired": {"active": False,
                            "command": ["gone-binary"]},
            },
            "dsh": {"command": ["node", "/opt/harness/bin.js", "-"]},
            "providers": {"muse": {"command": []}},
        }
        needed = required_binaries(config)
        self.assertIn("musej", needed)
        self.assertIn("node", needed)
        self.assertIn("/opt/harness/bin.js", needed)
        self.assertIn("git", needed)
        self.assertIn("muse", needed)
        self.assertNotIn("gone-binary", needed)

    def test_missing_binaries_listed_exact(self) -> None:
        config = {"a": {"command": ["present"]},
                  "b": {"command": ["absent-x234"]}}
        check = check_roster_clis(
            config=config,
            which=lambda name: "/bin/present" if name == "present"
            else ("/bin/x" if name in ("git", "muse") else None))
        self.assertFalse(check.ok)
        self.assertIn("absent-x234", check.detail)
        self.assertNotIn("present", check.detail)

    @unittest.skipUnless(
        shutil.which("musej") and shutil.which("claudej")
        and shutil.which("codexj"), "roster required")
    def test_live_roster_present(self) -> None:
        check = check_roster_clis()
        self.assertTrue(check.ok, check.detail)


class DefaultPortTests(unittest.TestCase):
    def test_env_overrides(self) -> None:
        import os
        from unittest.mock import patch

        from pilot_preflight import _default_port

        with patch.dict(os.environ, {"OPENCEO_HOST_PORT": "9999"}):
            self.assertEqual(_default_port(), 9999)
        with patch.dict(os.environ, {"OPENCEO_HOST_PORT": "",
                                     "OPENCEO_PORT": "8000"}):
            self.assertEqual(_default_port(), 8000)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_default_port(), 18765)


class CredentialsTests(unittest.TestCase):
    _FULL = {
        "ERPNEXT_URL": "https://erp.example",
        "ERPNEXT_API_KEY": "k",
        "ERPNEXT_API_SECRET": "s",
        "CLOUDFLARE_API_TOKEN": "t",
        "MONGODB_URL": "mongodb://x",
    }

    def test_full_set_is_ok(self) -> None:
        self.assertTrue(check_credentials(dotenv=dict(self._FULL)).ok)

    def test_env_only_set_is_ok(self) -> None:
        check = check_credentials(dotenv={}, environ=dict(self._FULL))
        self.assertTrue(check.ok, check.detail)

    def test_missing_named_exact(self) -> None:
        dotenv = dict(self._FULL)
        del dotenv["CLOUDFLARE_API_TOKEN"]
        dotenv["ERPNEXT_API_SECRET"] = "  "
        check = check_credentials(dotenv=dotenv)
        self.assertFalse(check.ok)
        self.assertIn("CLOUDFLARE_API_TOKEN", check.detail)
        self.assertIn("ERPNEXT_API_SECRET", check.detail)

    def test_values_never_surface(self) -> None:
        dotenv = {name: f"sentinel-{name.lower()}-234-xyz"
                  for name in self._FULL}
        check = check_credentials(dotenv=dotenv)
        self.assertTrue(check.ok)
        for value in dotenv.values():
            self.assertNotIn(value, check.detail)

    @unittest.skipUnless(
        (REPO / "openceo-core" / ".env").is_file(), "dotenv required")
    def test_live_dotenv_has_the_set(self) -> None:
        check = check_credentials()
        self.assertTrue(check.ok, check.detail)


class HealthTests(unittest.TestCase):
    def test_200_is_ok(self) -> None:
        self.assertTrue(check_health(get=lambda url: 200).ok)

    def test_500_fails(self) -> None:
        check = check_health(get=lambda url: 500)
        self.assertFalse(check.ok)
        self.assertIn("500", check.detail)

    def test_unreachable_fails(self) -> None:
        def down(url: str) -> int:
            raise ConnectionError("refused")

        check = check_health(get=down)
        self.assertFalse(check.ok)
        self.assertIn("refused", check.detail)


class SkillDryRunTests(unittest.TestCase):
    def test_live_dry_run(self) -> None:
        check = check_skill_dry_run()
        self.assertTrue(check.ok, check.detail)

    def test_failed_subcommand_fails(self) -> None:
        def run(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return SimpleNamespace(returncode=2, stdout="", stderr="nope")

        check = check_skill_dry_run(run=run)
        self.assertFalse(check.ok)
        self.assertIn("roots", check.detail)


class RunPreflightTests(unittest.TestCase):
    def _probes(self) -> dict[str, Any]:
        dotenv = dict(CredentialsTests._FULL)

        def skill_run(cmd: Any, **kwargs: Any) -> Any:
            del kwargs
            if "validate" in cmd:
                return SimpleNamespace(returncode=0, stdout="",
                                       stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout='{"ok": true, "dry_run": true, "result": {}}',
                stderr="")

        return {
            "proc_cmdlines": dict(_PROC_ONE),
            "ss_text": _SS_ONE,
            "venv_python": _VENV,
            "git_run": _git_ok,
            "which": lambda name: f"/bin/{name}" if name
            in ("present-cli", "git", "muse") else None,
            "is_file": lambda path: False,
            "config": {"a": {"command": ["present-cli"]}},
            "dotenv": dotenv,
            "health_get": lambda url: 200,
            "skill_run": skill_run,
        }

    def test_all_fake_ok_says_listo(self) -> None:
        result = run_preflight(**self._probes())
        self.assertTrue(result.ok)
        self.assertEqual(result.summary(), "listo")
        payload = result.to_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["checks"]), 8)

    def test_one_failure_names_the_check(self) -> None:
        probes = self._probes()
        probes["health_get"] = lambda url: 500
        result = run_preflight(**probes)
        self.assertFalse(result.ok)
        self.assertIn("NO LISTO", result.summary())
        self.assertIn("health", result.summary())


if __name__ == "__main__":
    unittest.main()
