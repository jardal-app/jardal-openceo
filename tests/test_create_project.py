"""F6 end-to-end project creation: ops library idempotency.

Covers TASK-2026-00115: `openceo-core/ops/create_project.py`, the library
behind the operations `crear-proyecto` skill. The Tier-0 workflow and its
`ops.*` tools retired in TASK-2026-00223. Filesystem fixtures live under
`tempfile`. No tokens, no network, no real tickets.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from config_loader import ConfigLoader
from ops.create_project import (
    ProjectCreationError,
    confined,
    default_remote_url,
    ensure_git_repo,
    ensure_project_config,
    ensure_repo_templates,
    ensure_vault_entry,
    next_project_number,
    normalize_key,
    normalize_org,
    remove_git_repo,
    remove_project_config,
    remove_vault_entry,
)
from settings import Settings

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


def make_settings(tmp: Path, **kw) -> Settings:
    projects = tmp / "projects"
    projects.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "projects" / "company_mandate.yaml",
                    projects / "company_mandate.yaml")
    vault = tmp / "vault"
    (vault / "projects").mkdir(parents=True, exist_ok=True)
    (vault / "README.md").write_text(
        "# Vault\n\n## Organizations and projects\n\n", encoding="utf-8")
    repos = tmp / "repos"
    repos.mkdir(parents=True, exist_ok=True)
    return Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="",
        WORKFLOWS_DIR=str(ROOT / "workflows"),
        PROJECTS_DIR=str(projects),
        PROJECT_REPOS_ROOT=str(repos),
        VAULT_ROOT=str(vault),
        **kw,
    )


class IdentifierTests(unittest.TestCase):
    def test_key_ok_and_lowercases(self):
        self.assertEqual(normalize_key("  Jardal-Demo1 "), "jardal-demo1")

    def test_key_rejects(self):
        for bad in ["", None, "../x", "a/b", "a b", "A_B", "x" * 70, "-lead"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ProjectCreationError):
                    normalize_key(bad)

    def test_org_default_and_rejects(self):
        self.assertEqual(normalize_org(""), "jardal")
        self.assertEqual(normalize_org("Jardal"), "jardal")
        with self.assertRaises(ProjectCreationError):
            normalize_org("../x")

    def test_confined_refuses_escape(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaises(ProjectCreationError):
                confined(root, "..", "outside")
            self.assertEqual(confined(root, "a", "b"), (root / "a" / "b").resolve())

    def test_conventions(self):
        self.assertEqual(default_remote_url("demo"),
                         "git@github-jardal:jardal-app/demo.git")


@unittest.skipUnless(GIT, "git is required for repo tests")
class GitRepoTests(unittest.TestCase):
    def test_create_and_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            first = ensure_git_repo(raw, "demo-proj")
            self.assertTrue(first["created"])
            self.assertTrue(first["remote_changed"])
            self.assertEqual(first["remote_url"], default_remote_url("demo-proj"))
            url = subprocess.run(
                ["git", "remote", "get-url", "origin"], cwd=first["path"],
                capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(url, default_remote_url("demo-proj"))
            second = ensure_git_repo(raw, "demo-proj")
            self.assertFalse(second["created"])
            self.assertFalse(second["remote_changed"])

    def test_divergent_remote_is_updated(self):
        with tempfile.TemporaryDirectory() as raw:
            ensure_git_repo(raw, "demo-proj", remote_url="git@example.com:old.git")
            result = ensure_git_repo(raw, "demo-proj")
            self.assertTrue(result["remote_changed"])
            self.assertEqual(result["remote_url"], default_remote_url("demo-proj"))

    def test_rejects_bad_name(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ProjectCreationError):
                ensure_git_repo(raw, "../escape")


@unittest.skipUnless(GIT, "git is required for template tests")
class TemplateTests(unittest.TestCase):
    def _repo(self, tmp: Path) -> Path:
        result = ensure_git_repo(tmp, "demo-proj")
        return Path(result["path"])

    def _ensure(self, repo: Path) -> dict:
        return ensure_repo_templates(
            repo, project_key="demo-proj", display_name="Demo Proj",
            org="jardal", description="Proyecto de prueba.",
            vault_root="/vault", remote_url=default_remote_url("demo-proj"))

    def test_templates_and_initial_commit(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            repo = self._repo(tmp)
            first = self._ensure(repo)
            self.assertTrue(first["committed"])
            self.assertRegex(first["sha"] or "", r"^[0-9a-f]{40}$")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8").splitlines()[0],
                             "# Demo Proj")
            claude = (repo / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("projects/jardal/demo-proj", claude)
            self.assertIn("docs/work/TASK-2026-000NN-tema.md", claude)
            self.assertLessEqual(len(claude.split()), 3000 // 2)
            agents = repo / "AGENTS.md"
            self.assertTrue(agents.is_symlink())
            self.assertEqual(agents.readlink(), Path("CLAUDE.md"))
            gitignore = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertIn("docs/work/", gitignore)
            self.assertIn("tmp/", gitignore)
            self.assertTrue((repo / "docs" / "work" / ".gitkeep").exists())

            second = self._ensure(repo)
            self.assertEqual(second["written"], [])
            self.assertFalse(second["committed"])
            self.assertEqual(second["sha"], first["sha"])
            log = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"], cwd=repo,
                capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(log, "1")

    def test_never_overwrites_divergent_files(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = self._repo(Path(raw))
            (repo / "README.md").write_text("# Mio\n", encoding="utf-8")
            result = self._ensure(repo)
            self.assertIn("README.md", result["existed"])
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "# Mio\n")

    def test_divergent_agents_md_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = self._repo(Path(raw))
            (repo / "AGENTS.md").write_text("otro\n", encoding="utf-8")
            with self.assertRaises(ProjectCreationError):
                self._ensure(repo)

    def test_gitignore_gains_missing_lines_only(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = self._repo(Path(raw))
            (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
            result = self._ensure(repo)
            self.assertIn(".gitignore", result["written"])
            lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertIn("node_modules/", lines)
            self.assertIn("docs/work/", lines)
            self.assertIn("tmp/", lines)
            rerun = self._ensure(repo)
            self.assertIn(".gitignore", rerun["existed"])

    def test_requires_git_repo(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ProjectCreationError):
                self._ensure(Path(raw))


class ProjectConfigTests(unittest.TestCase):
    def test_write_validates_and_sequences_number(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = make_settings(tmp)
            projects = Path(settings.PROJECTS_DIR)
            for name, number in (("aaa", 2), ("bbb", 7)):
                directory = projects / name
                directory.mkdir()
                (directory / "project.yaml").write_text(
                    f"project:\n  number: {number}\n  name: {name}\n", encoding="utf-8")
            result = ensure_project_config(
                projects, "demo-proj", remote_url=default_remote_url("demo-proj"),
                repo_path=str(tmp / "repos" / "demo-proj"),
                vault_root=str(tmp / "vault"),
                vault_project="projects/jardal/demo-proj")
            self.assertTrue(result["created"])
            self.assertEqual(result["number"], 8)
            self.assertEqual(ConfigLoader(settings).validate_project("demo-proj"), [])
            loaded = ConfigLoader(settings).load_project("demo-proj")
            self.assertEqual(loaded["project"]["name"], "demo-proj")
            rerun = ensure_project_config(
                projects, "demo-proj", remote_url="git@example.com:other.git",
                repo_path="/other", vault_root="/other", vault_project="projects/x/y")
            self.assertFalse(rerun["created"])
            self.assertIn(default_remote_url("demo-proj"),
                          (projects / "demo-proj" / "project.yaml").read_text(encoding="utf-8"))

    def test_refuses_foreign_config(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = make_settings(tmp)
            projects = Path(settings.PROJECTS_DIR)
            directory = projects / "demo-proj"
            directory.mkdir()
            (directory / "project.yaml").write_text(
                "project:\n  number: 9\n  name: other\n", encoding="utf-8")
            with self.assertRaises(ProjectCreationError):
                ensure_project_config(
                    projects, "demo-proj", remote_url="u", repo_path="r",
                    vault_root="v", vault_project="p")

    def test_next_number_fail_closed_on_unreadable(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            bad = tmp / "bad"
            bad.mkdir()
            (bad / "project.yaml").write_text("project: [unclosed\n", encoding="utf-8")
            with self.assertRaises(ProjectCreationError):
                next_project_number(tmp)
            self.assertEqual(next_project_number(tmp / "missing"), 1)


class VaultEntryTests(unittest.TestCase):
    def test_hub_and_readme_line_are_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = make_settings(tmp)
            vault = Path(settings.VAULT_ROOT)
            (vault / "README.md").write_text(
                "# Vault\n\n- [[projects/jardal/other|Other]] — /repos/other\n",
                encoding="utf-8")
            first = ensure_vault_entry(
                vault, "jardal", "demo-proj", display_name="Demo Proj",
                description="Proyecto de prueba.", source_path="/repos/demo-proj")
            self.assertTrue(first["created_hub"])
            self.assertTrue(first["readme_updated"])
            hub = Path(first["hub"])
            front = hub.read_text(encoding="utf-8").split("---\n")[1]
            self.assertIn("project: demo-proj", front)
            self.assertIn("org: jardal", front)
            readme = (vault / "README.md").read_text(encoding="utf-8")
            self.assertIn("[[projects/jardal/other|Other]]", readme)
            self.assertIn("- [[projects/jardal/demo-proj|Demo Proj]] — /repos/demo-proj", readme)

            second = ensure_vault_entry(
                vault, "jardal", "demo-proj", display_name="Otro nombre",
                description="x", source_path="/repos/cambiado")
            self.assertFalse(second["created_hub"])
            self.assertFalse(second["readme_updated"])
            lines = (vault / "README.md").read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum("demo-proj" in line for line in lines), 1)

    def test_missing_readme_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            vault = Path(raw) / "vault"
            (vault / "projects").mkdir(parents=True)
            with self.assertRaises(ProjectCreationError):
                ensure_vault_entry(vault, "jardal", "demo-proj", display_name="",
                                   description="", source_path="/x")


@unittest.skipUnless(GIT, "git is required for teardown tests")
class TeardownTests(unittest.TestCase):
    def _create_all(self, tmp: Path):
        settings = make_settings(tmp)
        repos = Path(settings.PROJECT_REPOS_ROOT)
        projects = Path(settings.PROJECTS_DIR)
        vault = Path(settings.VAULT_ROOT)
        repo = ensure_git_repo(repos, "demo-proj")["path"]
        ensure_repo_templates(
            repo, project_key="demo-proj", display_name="", org="",
            description="", vault_root=str(vault),
            remote_url=default_remote_url("demo-proj"))
        ensure_project_config(
            projects, "demo-proj", remote_url=default_remote_url("demo-proj"),
            repo_path=repo, vault_root=str(vault),
            vault_project="projects/jardal/demo-proj")
        ensure_vault_entry(vault, "jardal", "demo-proj", display_name="",
                           description="", source_path=repo)
        return settings

    def test_remove_roundtrip(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = self._create_all(tmp)
            repos = Path(settings.PROJECT_REPOS_ROOT)
            projects = Path(settings.PROJECTS_DIR)
            vault = Path(settings.VAULT_ROOT)
            self.assertTrue(remove_git_repo(repos, "demo-proj")["removed"])
            self.assertTrue(remove_project_config(projects, "demo-proj")["removed"])
            removed = remove_vault_entry(vault, "jardal", "demo-proj")
            self.assertTrue(removed["removed_hub"])
            self.assertTrue(removed["removed_line"])
            self.assertFalse((repos / "demo-proj").exists())
            self.assertFalse((projects / "demo-proj").exists())
            self.assertNotIn("demo-proj", (vault / "README.md").read_text(encoding="utf-8"))
            self.assertFalse(remove_git_repo(repos, "demo-proj")["removed"])
            self.assertFalse(remove_project_config(projects, "demo-proj")["removed"])
            again = remove_vault_entry(vault, "jardal", "demo-proj")
            self.assertFalse(again["removed_hub"])
            self.assertFalse(again["removed_line"])

    def test_remove_refuses_foreign_content(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = make_settings(tmp)
            repos = Path(settings.PROJECT_REPOS_ROOT)
            projects = Path(settings.PROJECTS_DIR)
            vault = Path(settings.VAULT_ROOT)
            plain = repos / "plain"
            plain.mkdir()
            with self.assertRaises(ProjectCreationError):
                remove_git_repo(repos, "plain")
            foreign = projects / "demo-proj"
            foreign.mkdir()
            (foreign / "project.yaml").write_text(
                "project:\n  number: 1\n  name: other\n", encoding="utf-8")
            with self.assertRaises(ProjectCreationError):
                remove_project_config(projects, "demo-proj")
            hub_dir = vault / "projects" / "jardal" / "demo-proj"
            hub_dir.mkdir(parents=True)
            (hub_dir / "demo-proj.md").write_text(
                "---\nproject: other\n---\n\n# x\n", encoding="utf-8")
            with self.assertRaises(ProjectCreationError):
                remove_vault_entry(vault, "jardal", "demo-proj")

    def test_remove_refuses_hub_with_notes(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            settings = self._create_all(tmp)
            vault = Path(settings.VAULT_ROOT)
            hub_dir = vault / "projects" / "jardal" / "demo-proj"
            (hub_dir / "decisions").mkdir()
            (hub_dir / "decisions" / "2026-01-01-x.md").write_text("# x\n", encoding="utf-8")
            with self.assertRaises(ProjectCreationError):
                remove_vault_entry(vault, "jardal", "demo-proj")


if __name__ == "__main__":
    unittest.main()
