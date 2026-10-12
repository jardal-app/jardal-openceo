"""Module tests for the crear-proyecto skill CLI (TASK-2026-00214).

Lives beside the script because the catalog loader requires every
``scripts/**/*.py`` to have its ``test_<stem>.py``. Runs standalone::

    pytest skills/operations/crear-proyecto/scripts/test_crear_proyecto.py

Tmp roots only; the CLI contract over subprocess is covered by
``tests/test_skill_crear_proyecto_214.py``.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crear_proyecto  # noqa: E402


def _run(argv: list[str]) -> tuple[int, dict]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = crear_proyecto.main(argv)
    return code, json.loads(buffer.getvalue())


class RootsTests(unittest.TestCase):
    def test_explicit_roots_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = _run(["roots", "--repos-root", f"{tmp}/r",
                                  "--projects-root", f"{tmp}/p",
                                  "--vault-root", f"{tmp}/v"])
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        result = payload["result"]
        self.assertEqual(f"{tmp}/r", result["repos"])
        self.assertEqual(f"{tmp}/p", result["projects"])
        self.assertEqual(f"{tmp}/v", result["vault"])

    def test_default_roots_match_service_settings(self) -> None:
        _code, payload = _run(["roots"])
        self.assertTrue(payload["ok"])
        expected = {name: str(path)
                    for name, path in crear_proyecto.service_roots().items()}
        self.assertEqual(expected, payload["result"])


class TemplatesTests(unittest.TestCase):
    def _roots(self, base: Path) -> list[str]:
        return ["--repos-root", str(base / "repos"),
                "--projects-root", str(base / "projects"),
                "--vault-root", str(base / "vault")]

    def test_templates_are_idempotent(self) -> None:
        from ops.create_project import ensure_git_repo
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "repos").mkdir()
            ensure_git_repo(base / "repos", "demo-214")
            argv = ["templates", "--project-key", "demo-214",
                   "--display-name", "Demo 214", "--description", "d",
                   *self._roots(base)]
            code, first = _run(argv)
            self.assertEqual(0, code)
            self.assertTrue(first["ok"])
            self.assertIn("README.md", first["result"]["written"])
            self.assertTrue(first["result"]["committed"])
            _code, second = _run(argv)
            self.assertTrue(second["ok"])
            self.assertEqual([], second["result"]["written"])
            self.assertIn("README.md", second["result"]["existed"])
            self.assertFalse(second["result"]["committed"])
            repo = base / "repos" / "demo-214"
            self.assertTrue((repo / "CLAUDE.md").is_file())
            self.assertEqual("CLAUDE.md",
                             (repo / "AGENTS.md").readlink().as_posix())

    def test_bad_key_fails_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, payload = _run(
                ["templates", "--project-key", "NOPE!!",
                 *self._roots(Path(tmp))])
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertIn("project_key", payload["error"])


class ProjectConfigTests(unittest.TestCase):
    def _roots(self, base: Path) -> list[str]:
        return ["--repos-root", str(base / "repos"),
                "--projects-root", str(base / "projects"),
                "--vault-root", str(base / "vault")]

    def test_config_is_created_validated_and_idempotent(self) -> None:
        from config_loader import ConfigLoader
        from settings import Settings
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            argv = ["project-config", "--project-key", "demo-214",
                   *self._roots(base)]
            code, first = _run(argv)
            self.assertEqual(0, code)
            self.assertTrue(first["ok"])
            self.assertTrue(first["result"]["created"])
            self.assertEqual([], first["result"]["errors"])
            loader = ConfigLoader(Settings(
                _env_file=None,
                PROJECTS_DIR=str(base / "projects")))
            self.assertEqual([], loader.validate_project("demo-214"))
            _code, second = _run(argv)
            self.assertTrue(second["ok"])
            self.assertFalse(second["result"]["created"])

    def test_validation_errors_fail_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["project-config", "--project-key", "demo-214",
                   *self._roots(Path(tmp))]
            with mock.patch.object(crear_proyecto, "_validate_project",
                                   return_value=["boom"]):
                code, payload = _run(argv)
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertIn("invalid", payload["error"])


class VaultEntryTests(unittest.TestCase):
    def test_entry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            vault = base / "vault"
            (base / "repos").mkdir(parents=True)
            vault.mkdir(parents=True)
            (vault / "README.md").write_text("# Vault\n", encoding="utf-8")
            argv = ["vault-entry", "--project-key", "demo-214",
                   "--display-name", "Demo 214",
                   "--repos-root", str(base / "repos"),
                   "--projects-root", str(base / "projects"),
                   "--vault-root", str(vault)]
            code, first = _run(argv)
            self.assertEqual(0, code)
            self.assertTrue(first["ok"])
            self.assertTrue(first["result"]["created_hub"])
            self.assertTrue(first["result"]["readme_updated"])
            _code, second = _run(argv)
            self.assertTrue(second["ok"])
            self.assertFalse(second["result"]["created_hub"])
            self.assertFalse(second["result"]["readme_updated"])
            hub = vault / "projects" / "jardal" / "demo-214" / "demo-214.md"
            self.assertTrue(hub.is_file())
            self.assertIn("[[projects/jardal/demo-214",
                          (vault / "README.md").read_text(encoding="utf-8"))


class DryRunTests(unittest.TestCase):
    def test_dry_run_writes_only_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            prod = [str(base / "repos"), str(base / "projects"),
                    str(base / "vault")]
            for path in prod:
                Path(path).mkdir(parents=True)
            (base / "vault" / "README.md").write_text(
                "# Vault\n", encoding="utf-8")
            argv = ["templates", "--project-key", "demo-214",
                   "--repos-root", prod[0], "--projects-root", prod[1],
                   "--vault-root", prod[2], "--dry-run"]
            code, payload = _run(argv)
            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["dry_run"])
            self.assertIn("written", payload["result"])
            self.assertEqual([], list((base / "repos").iterdir()))

    def test_dry_run_config_validates_the_real_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            argv = ["project-config", "--project-key", "demo-214",
                   "--repos-root", str(base / "repos"),
                   "--projects-root", str(base / "projects"),
                   "--vault-root", str(base / "vault"), "--dry-run"]
            code, payload = _run(argv)
            self.assertEqual(0, code)
            self.assertTrue(payload["ok"])
            self.assertEqual([], payload["result"]["errors"])


if __name__ == "__main__":
    unittest.main()
