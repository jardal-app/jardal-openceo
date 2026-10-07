"""F6 end-to-end project creation: ops idempotency + Tier-0 workflow contract.

Covers TASK-2026-00115: `openceo-core/ops/create_project.py`, the `ops.*`
workflow tools and `workflows/operations/create_project.yaml`. Filesystem
fixtures live under `tempfile`; ERPNext and the owner-escalation path use
stubs. No tokens, no network, no real tickets.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from config_loader import ConfigLoader
from ops.create_project import (
    ProjectCreationError,
    check_spend,
    confined,
    default_remote_url,
    ensure_git_repo,
    ensure_project_config,
    ensure_repo_templates,
    ensure_vault_entry,
    escalation_source_id,
    next_project_number,
    normalize_key,
    normalize_org,
    remove_git_repo,
    remove_project_config,
    remove_vault_entry,
)
from settings import Settings
from state import TaskStatus, WorkflowRunStatus
from workflow_service import WorkflowService, _evaluate_when

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


def load_mandate(settings: Settings):
    return ConfigLoader(settings).load_company_mandate()


class FakeRunRepo:
    """Minimal workflow-run store for full-run tests."""

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}

    async def latest_workflow_run_for_task(self, task_id: str):
        runs = [run for run in self.runs.values() if run.get("task_id") == task_id]
        return runs[-1] if runs else None

    async def create_workflow_run(self, doc: dict):
        self.runs[doc["run_id"]] = dict(doc)
        return self.runs[doc["run_id"]]

    async def patch_workflow_run(self, run_id: str, patch: dict):
        self.runs[run_id].update(patch)
        return self.runs[run_id]


def make_service(settings: Settings, repo=None, hub=None) -> WorkflowService:
    hub = hub if hub is not None else Mock()
    hub.broadcast = AsyncMock()
    return WorkflowService(
        settings=settings, repo=repo if repo is not None else Mock(), realtime_hub=hub)


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
        self.assertEqual(escalation_source_id("demo"), "f6-create-project:demo")


class SpendTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self._tmp.name))
        self.mandate = load_mandate(self.settings)

    def tearDown(self):
        self._tmp.cleanup()

    def test_zero_spend_is_automatic(self):
        for costs in (None, [], [{"label": "nada", "amount": "0"}]):
            with self.subTest(costs=costs):
                result = check_spend(project_key="demo", summary="", costs=costs,
                                     mandate=self.mandate)
                self.assertFalse(result["escalate"])
                self.assertIsNone(result["category"])
                self.assertIsNone(result["human_action"])

    def test_positive_spend_escalates(self):
        result = check_spend(
            project_key="demo", summary="Crear demo",
            costs=[{"label": "dominio", "amount": "12.50"}],
            mandate=self.mandate)
        self.assertTrue(result["escalate"])
        self.assertEqual(result["category"], "gasto_nuevo")
        self.assertEqual(result["amount"], "12.50")
        self.assertIn("gasto", (result["human_action"] or "").lower())

    def test_amounts_sum(self):
        result = check_spend(
            project_key="demo", summary="",
            costs=[{"label": "a", "amount": "1.25"}, {"label": "b", "amount": 2}],
            mandate=self.mandate)
        self.assertEqual(result["amount"], "3.25")

    def test_bad_amounts_fail_closed(self):
        for costs in (
            [{"label": "a", "amount": 1.5}],
            [{"label": "a", "amount": True}],
            [{"label": "a", "amount": "-1"}],
            [{"label": "a", "amount": "mucho"}],
            ["dominio"],
        ):
            with self.subTest(costs=costs):
                with self.assertRaises(ProjectCreationError):
                    check_spend(project_key="demo", summary="", costs=costs,
                                mandate=self.mandate)

    def test_missing_mandate_escalates_fail_closed(self):
        result = check_spend(project_key="demo", summary="", costs=[],
                             mandate=None)
        self.assertTrue(result["escalate"])
        self.assertEqual(result["category"], "mandate_unavailable")


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


class WorkflowContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.settings = make_settings(self.tmp)
        self.repo = FakeRunRepo()
        self.service = make_service(self.settings, repo=self.repo)
        self.workflow = next(
            item for item in self.service.list_workflows(include_steps=True)
            if item["workflow"] == "operations.create_project")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _task(self, **params):
        return {
            "task_id": "task_f6_test",
            "title": "Crear proyecto demo-proj",
            "project": "jardal-openceo",
            "params": {"project_key": "demo-proj", **params},
        }

    def test_yaml_registered_with_steps(self):
        self.assertEqual(self.workflow["agent"], "operations")
        self.assertTrue(self.workflow["auto_match"])
        self.assertIn("crear proyecto", self.workflow["triggers"])
        step_ids = [step.get("id") for step in self.workflow["steps"]]
        self.assertEqual(step_ids, [
            "check_spend", "escalate", "wait_owner", "erpnext_project",
            "git_repo", "repo_templates", "project_config", "vault_entry", "dns",
        ])

    def test_all_step_tools_registered(self):
        registry = self.service.tool_registry()
        for step in self.workflow["steps"]:
            if (step.get("type") or "tool").lower() != "tool":
                continue
            with self.subTest(tool=step.get("tool")):
                self.assertIn(step["tool"], registry)
                self.assertTrue(registry[step["tool"]]["doc"])

    def test_when_gates(self):
        steps = {step["id"]: step for step in self.workflow["steps"]}
        spend_ctx = {"outputs": {"check_spend": {"escalate": True, "amount": "5"}}}
        free_ctx = {"outputs": {"check_spend": {"escalate": False, "amount": "0"}}}
        self.assertTrue(_evaluate_when(steps["escalate"]["when"], spend_ctx))
        self.assertTrue(_evaluate_when(steps["wait_owner"]["when"], spend_ctx))
        self.assertFalse(_evaluate_when(steps["escalate"]["when"], free_ctx))
        self.assertFalse(_evaluate_when(steps["wait_owner"]["when"], free_ctx))
        self.assertTrue(_evaluate_when(steps["dns"]["when"], {"params": {"is_web": True}}))
        for ctx in ({"params": {"is_web": False}}, {"params": {}}, {}):
            self.assertFalse(_evaluate_when(steps["dns"]["when"], ctx))

    def test_resolution_explicit_and_auto_match(self):
        explicit = self.service.resolve_task_workflow(
            self._task(workflow="operations.create_project"))
        self.assertIsNotNone(explicit)
        assert explicit is not None
        self.assertEqual(explicit.tier, "tier0")
        auto = self.service.resolve_task_workflow(self._task(use_workflow=True))
        self.assertIsNotNone(auto)
        assert auto is not None
        self.assertEqual(auto.workflow["workflow"], "operations.create_project")

    async def test_ops_tools_reject_bad_key(self):
        with self.assertRaises(ProjectCreationError):
            await self.service._tools.call(
                "ops.ensure_git_repo", {"project_key": "../x"}, {})

    async def test_dns_guard(self):
        with self.assertRaises(ValueError):
            await self.service._tools.call("ops.ensure_dns_record", {"hostname": ""}, {})
        with patch("tools.cloudflare.CloudflareDNSManager") as manager:
            manager.return_value.check_or_create.return_value = {"ok": True}
            result = await self.service._tools.call(
                "ops.ensure_dns_record", {"hostname": "demo.jardal.app"}, {})
            self.assertEqual(result, {"ok": True})
            manager.return_value.check_or_create.assert_called_once_with("demo.jardal.app")

    async def test_escalate_tool_without_task_service(self):
        result = await self.service._tools.call(
            "ops.escalate_project_spend",
            {"project_key": "demo-proj", "summary": "Crear demo", "amount": "5"},
            {})
        self.assertFalse(result["ok"])

    def _stub_erpnext(self):
        adapter = Mock()
        adapter.find_project = AsyncMock(return_value=None)
        adapter.ensure_project = AsyncMock(return_value="PROJ-TEST")
        self.service._tools.set_ops_erpnext(adapter)
        return adapter

    @unittest.skipUnless(GIT, "git is required for full-run tests")
    async def test_full_run_no_spend_not_web(self):
        adapter = self._stub_erpnext()
        outcome = await self.service.execute_task_workflow(self._task(), self.workflow)
        self.assertEqual(outcome.status, TaskStatus.COMPLETED.value)
        self.assertEqual(outcome.selected_agent, "operations")
        run = next(iter(self.repo.runs.values()))
        self.assertEqual(run["status"], WorkflowRunStatus.COMPLETED.value)
        events = {item["id"]: item["status"] for item in run["steps"]}
        self.assertEqual(events.get("escalate"), "skipped")
        self.assertEqual(events.get("wait_owner"), "skipped")
        self.assertEqual(events.get("dns"), "skipped")
        self.assertEqual(events.get("erpnext_project"), "ok")
        outputs = run["outputs"]
        self.assertTrue(outputs["erpnext_project"]["created"])
        self.assertTrue(outputs["git_repo"]["created"])
        self.assertTrue(outputs["repo_templates"]["committed"])
        self.assertTrue(outputs["project_config"]["created"])
        self.assertTrue(outputs["vault_entry"]["created_hub"])
        adapter.ensure_project.assert_awaited_once_with("demo-proj", None)
        repos = Path(self.settings.PROJECT_REPOS_ROOT)
        self.assertTrue((repos / "demo-proj" / ".git").exists())
        loader = ConfigLoader(self.settings)
        self.assertEqual(loader.validate_project("demo-proj"), [])
        self.assertIn("demo-proj", (Path(self.settings.VAULT_ROOT) / "README.md").read_text())

    @unittest.skipUnless(GIT, "git is required for full-run tests")
    async def test_rerun_reports_existed(self):
        self._stub_erpnext()
        await self.service.execute_task_workflow(self._task(), self.workflow)
        self.service._tools.set_ops_erpnext(
            Mock(find_project=AsyncMock(return_value="PROJ-TEST"),
                 ensure_project=AsyncMock(return_value="PROJ-TEST")))
        outcome = await self.service.execute_task_workflow(
            self._task(), self.workflow, resolution=None)
        self.assertEqual(outcome.status, TaskStatus.COMPLETED.value)
        self.assertEqual(len(self.repo.runs), 2)
        run = list(self.repo.runs.values())[-1]
        outputs = run["outputs"]
        self.assertFalse(outputs["erpnext_project"]["created"])
        self.assertFalse(outputs["git_repo"]["created"])
        self.assertFalse(outputs["repo_templates"]["committed"])
        self.assertFalse(outputs["project_config"]["created"])
        self.assertFalse(outputs["vault_entry"]["created_hub"])
        self.assertFalse(outputs["vault_entry"]["readme_updated"])
        readme = (Path(self.settings.VAULT_ROOT) / "README.md").read_text().splitlines()
        self.assertEqual(sum("demo-proj" in line for line in readme), 1)
        log = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=Path(self.settings.PROJECT_REPOS_ROOT) / "demo-proj",
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(log, "1")

    async def test_spend_blocks_before_side_effects(self):
        self._stub_erpnext()
        task_service = Mock()
        task_service.create_owner_escalation = AsyncMock(
            return_value={"task_id": "task_esc", "ticket_number": 999})
        self.service.set_task_service(task_service)
        outcome = await self.service.execute_task_workflow(
            self._task(costs=[{"label": "dominio", "amount": "12.50"}]),
            self.workflow)
        self.assertEqual(outcome.status, TaskStatus.WAITING.value)
        self.assertEqual(outcome.waiting_reason, "phase:owner_escalation")
        self.assertTrue(outcome.approval_required)
        task_service.create_owner_escalation.assert_awaited_once()
        kwargs = task_service.create_owner_escalation.call_args.kwargs
        self.assertEqual(kwargs["source_id"], "f6-create-project:demo-proj")
        self.assertTrue(kwargs["decision"].escalate)
        repos = Path(self.settings.PROJECT_REPOS_ROOT)
        projects = Path(self.settings.PROJECTS_DIR)
        self.assertFalse((repos / "demo-proj").exists())
        self.assertFalse((projects / "demo-proj").exists())


if __name__ == "__main__":
    unittest.main()
