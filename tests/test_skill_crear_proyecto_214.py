"""First real role skill: operations crear-proyecto (TASK-2026-00214).

The operations agent owns project creation with its skill (recipe, exact
CLI over ``ops.create_project``, Jardal ERP-project plus DNS boundary
tools). A skilled run stages its skill links into the ticket folder and
mounts the repo read-only; operations also mounts the creation roots
writable. Agent badges bind to the Jardal ERP mirror ticket, never to the
Mongo uuid (that mint path refused every badged run).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
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
    catalog_root,
    load_catalog,
    load_role_profile,
    load_role_skills,
)
from config_loader import ConfigLoader, _planner_agents_from_catalog  # noqa: E402
from graph import resolve_agent_gate  # noqa: E402
from mcp_identity import (  # noqa: E402
    McpDenied,
    TicketGrant,
    TokenIssuer,
    TokenKind,
    box_for_agent,
)
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import _planner_skills  # noqa: E402
from objectives.planner_models import (  # noqa: E402
    ObjectiveStatus,
    PlannedTicket,
    PlannerResult,
)
from orchestration.agent_sandbox import plan_agent  # noqa: E402
from orchestration.generic_agent import (  # noqa: E402
    GenericAgentWiring,
    role_extra_mounts,
    run_generic_agent,
    stage_skill_links,
)
from orchestration.live import ExecResult  # noqa: E402
from orchestration.role_muse import badge_ticket_for_task  # noqa: E402
from settings import Settings  # noqa: E402
from state import TaskStatus  # noqa: E402

SKILL_DIR = REPO / "skills" / "operations" / "crear-proyecto"
SCRIPT = SKILL_DIR / "scripts" / "crear_proyecto.py"

OPERATIONS_BOX = frozenset({
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_add_ticket_comment",
    "openceo_attach_ticket_file",
    "openceo_ensure_business_project",
    "manage_dns_record",
})

MIRROR = {"_mirrors": {"erpnext": {"name": "TASK-2026-00214"}}}


def _validate_skill_live(target: str, skill: str | None,
                         skills: dict[str, list[str]] | None) -> list[str]:
    fields: dict[str, Any] = {
        "local_ref": "t1", "title": "Create a project",
        "target_agent": target, "work_type": "operations", "goal": "g",
        "why_now": "now", "acceptance_criteria": ["created"],
        "expected_artifacts": ["project"],
    }
    if skill is not None:
        fields["skill"] = skill
    result = PlannerResult(objective_status=ObjectiveStatus.ACTIVE,
                           state_summary="s",
                           tickets=[PlannedTicket(**fields)])

    async def _run() -> list[str]:
        async def _exists(_: str) -> bool:
            return False

        return await planner_mod.validate_planner_result(
            result, agents={target: {"enabled": True}}, open_titles=[],
            task_exists=_exists, skills=skills)

    return asyncio.run(_run())


class OperationsCardTest(unittest.TestCase):
    def test_operations_enabled_with_box_and_skill(self) -> None:
        card = load_catalog().agents["operations"]
        self.assertTrue(card.enabled)
        self.assertIsNone(card.disabled_reason)
        self.assertIsNone(card.decided_by_ticket)
        self.assertEqual(OPERATIONS_BOX, set(card.boundary_tools))
        self.assertEqual(["crear-proyecto"], card.skills)

    def test_profile_loads_card_skill(self) -> None:
        profile = load_role_profile("operations")
        self.assertEqual({"create-skill", "crear-proyecto"},
                         set(profile.skills))

    def test_index_summary_dropped_disabled(self) -> None:
        import yaml
        entries = yaml.safe_load(
            (REPO / "catalog" / "index.yaml").read_text(encoding="utf-8"))
        summary = next(item["summary"] for item in entries["agents"]
                       if item["name"] == "operations")
        self.assertNotIn("Disabled", summary)


class SkillOnDiskTest(unittest.TestCase):
    def test_skill_parses_activa(self) -> None:
        found = load_role_skills("operations", "skills/operations")
        self.assertEqual(["crear-proyecto"],
                         [skill.name for skill in found])
        skill = found[0]
        self.assertEqual("activa", skill.status)
        self.assertEqual("TASK-2026-00214", skill.ticket)

    def test_exposure_symlink_resolves_to_source(self) -> None:
        link = REPO / ".codex" / "skills" / "crear-proyecto"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(SKILL_DIR),
                         os.path.realpath(link))

    def test_script_has_its_sibling_test(self) -> None:
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(
            (SKILL_DIR / "scripts" / "test_crear_proyecto.py").is_file())

    def test_recipe_stays_small(self) -> None:
        size = (SKILL_DIR / "SKILL.md").stat().st_size
        self.assertLess(size, 4096, "the role prompt budget is shared")


@unittest.skipUnless(shutil.which("bash"), "bash required")
class RecipeTest(unittest.TestCase):
    def _setup_fence(self) -> str:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        start = text.index("```bash")
        end = text.index("```", start + 7)
        return text[start + 7:end].strip()

    def test_setup_block_resolves_repo_python_and_repos(self) -> None:
        script = (self._setup_fence()
                  + '\nprintf "REPO=%s\\nPY=%s\\nREPOS=%s\\n" '
                    '"$REPO" "$PY" "$REPOS"\n'
                  + 'test -x "$PY"\n')
        proc = subprocess.run(["bash", "-c", script],
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              text=True, errors="replace", timeout=120,
                              cwd=str(REPO))
        self.assertEqual(0, proc.returncode, proc.stdout)
        values = dict(line.split("=", 1)
                      for line in proc.stdout.splitlines())
        self.assertEqual(str(REPO), values["REPO"])
        self.assertEqual(
            str(REPO / ".venv" / "bin" / "python"), values["PY"])
        self.assertEqual(
            str(Path(Settings().PROJECT_REPOS_ROOT).expanduser()
                .resolve()),
            values["REPOS"])


class PlannerSkillLiveTest(unittest.TestCase):
    def test_planner_sees_first_skill(self) -> None:
        block, mapping = _planner_skills()
        self.assertIn("crear-proyecto", block)
        self.assertEqual(["crear-proyecto"], mapping["operations"])
        self.assertEqual([], mapping["dev"])

    def test_suggested_skill_validated_live(self) -> None:
        _block, mapping = _planner_skills()
        live = {name: list(names) for name, names in mapping.items()}
        self.assertEqual(
            [], _validate_skill_live("operations", "crear-proyecto", live))
        errors = _validate_skill_live("operations", "frobnicate", live)
        self.assertTrue(any("frobnicate" in line for line in errors),
                        errors)

    def test_registry_enables_operations(self) -> None:
        registry = _planner_agents_from_catalog(load_catalog())
        self.assertEqual({"enabled": True}, registry["operations"])


class GateLiveTest(unittest.TestCase):
    def test_hint_proceeds(self) -> None:
        decision = resolve_agent_gate("operations", {}, None,
                                      load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("operations", decision.hint)

    def test_planner_target_proceeds(self) -> None:
        decision = resolve_agent_gate(
            None, {"target_agent": "operations"}, None, load_catalog())
        self.assertTrue(decision.proceed)
        self.assertEqual("operations", decision.hint)


class OperationsBoxTest(unittest.TestCase):
    def _grant(self, tools: frozenset[str]) -> TicketGrant:
        return TicketGrant(kind=TokenKind.AGENT, agent="operations",
                           ticket="TASK-2026-00214",
                           project="jardal-openceo", tools=tools)

    def test_box_comes_from_the_live_card(self) -> None:
        card = load_catalog().agents["operations"]
        self.assertEqual(OPERATIONS_BOX, box_for_agent(card))

    def test_creation_tools_allowed(self) -> None:
        grant = self._grant(OPERATIONS_BOX)
        grant.check("openceo_ensure_business_project",
                    {"project_key": "demo"})
        grant.check("manage_dns_record", {"hostname": "x.jardal.app"})

    def test_missing_tools_still_refused(self) -> None:
        grant = self._grant(frozenset({"openceo_get_ticket"}))
        with self.assertRaises(McpDenied):
            grant.check("manage_dns_record", {"hostname": "x.jardal.app"})

    def test_ticket_tools_stay_ticket_bound(self) -> None:
        grant = self._grant(OPERATIONS_BOX)
        grant.check("openceo_add_ticket_comment",
                    {"ticket": "TASK-2026-00214"})
        with self.assertRaises(McpDenied):
            grant.check("openceo_add_ticket_comment",
                        {"ticket": "TASK-2026-00001"})

    def test_operations_badge_mints(self) -> None:
        class _Store:
            async def save(self, *args: Any, **kwargs: Any) -> None:
                return None

        async def _mint() -> str:
            issuer = TokenIssuer(_Store(), load_catalog().agents)  # type: ignore[arg-type]
            return await issuer.issue_agent(
                "operations", "TASK-2026-00214", "jardal-openceo")

        badge = asyncio.run(_mint())
        self.assertTrue(badge)


class BadgeTicketTest(unittest.TestCase):
    def test_mirror_name_returned(self) -> None:
        self.assertEqual(
            "TASK-2026-00214",
            badge_ticket_for_task({"params": dict(MIRROR)}))

    def test_missing_mirror_is_none(self) -> None:
        self.assertIsNone(badge_ticket_for_task({"params": {}}))
        self.assertIsNone(badge_ticket_for_task({}))
        self.assertIsNone(badge_ticket_for_task({"params": None}))
        self.assertIsNone(badge_ticket_for_task(
            {"params": {"_mirrors": {"erpnext": {"name": ""}}}}))

    def test_session_provider_needs_a_ticket(self) -> None:
        from unittest.mock import MagicMock
        from agents.dev.session import DevSessionManager
        settings = MagicMock()
        loader = MagicMock()
        manager = DevSessionManager(settings, loader,
                                    repo=MagicMock(db=MagicMock()))
        self.assertIsNotNone(manager._ticket_badge_provider(
            "TASK-2026-00214", "jardal-openceo"))
        self.assertIsNone(manager._ticket_badge_provider("", "p"))
        manager = DevSessionManager(settings, loader, repo=None)
        self.assertIsNone(manager._ticket_badge_provider(
            "TASK-2026-00214", "p"))


def _scripted_factory(recorded: list[dict[str, Any]],
                      stdout: str):
    result = ExecResult(returncode=0, elapsed_s=1.0,
                        output_tail=stdout, usage={})

    class _Scripted:
        async def run(self, role: str, prompt: str, workdir: Path,
                      timeout_s: int) -> ExecResult:
            recorded.append({"role": role, "prompt": prompt,
                             "workdir": workdir})
            return result

        async def cleanup(self, role: str) -> None:
            recorded.append({"cleanup": role})

    def _factory(role: str, spec: dict[str, Any],
                 provider: Any) -> _Scripted:
        recorded.append({"factory_role": role, "spec": dict(spec)})
        return _Scripted()

    return _factory


def _badge_recorder(calls: list[dict[str, str]]) -> Any:
    class _Provider:
        async def mint(self, role: str) -> str:
            return "badge-scripted"

        def mcp_servers(self, badge: str) -> dict[str, Any]:
            return {}

        async def revoke(self, badge: str) -> None:
            return None

    def _factory(agent: str, ticket: str, project: str) -> _Provider:
        calls.append({"agent": agent, "ticket": ticket,
                      "project": project})
        return _Provider()

    return _factory


_HECHO = ("did the work\n```agent-result\n"
          '{"resultado": "hecho", "motivo": "project ready"}\n```\n')


def _live_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "661267c6-fe1e-4d51-ab40-ceb04e5264bd",
        "ticket_number": 9001, "project": "jardal-openceo",
        "title": "Crear el proyecto demo-214", "description": "spec",
        "params": dict(MIRROR),
    }
    task.update(overrides)
    return task


class BadgeWiringTest(unittest.TestCase):
    def _wiring(self, root: Path, recorded: list[dict[str, Any]],
                badge_calls: list[dict[str, str]]) -> GenericAgentWiring:
        return GenericAgentWiring(
            settings=Settings(),
            repo=None,
            executor_factory=_scripted_factory(recorded, _HECHO),
            badge_provider_factory=_badge_recorder(badge_calls),
            work_base_dir=root / "agent-work",
            catalog=load_catalog())

    def test_badge_binds_mirror_not_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded: list[dict[str, Any]] = []
            badge_calls: list[dict[str, str]] = []
            out = asyncio.run(run_generic_agent(
                "operations", _live_task(),
                self._wiring(root, recorded, badge_calls)))
        self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
        self.assertEqual([{"agent": "operations",
                           "ticket": "TASK-2026-00214",
                           "project": "jardal-openceo"}],
                         badge_calls)

    def test_missing_mirror_waits_on_real_issuer_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded: list[dict[str, Any]] = []
            wiring = GenericAgentWiring(
                settings=Settings(),
                repo=mock.MagicMock(db=mock.MagicMock()),
                executor_factory=_scripted_factory(recorded, _HECHO),
                badge_provider_factory=None,
                work_base_dir=root / "agent-work",
                catalog=load_catalog())
            task = _live_task(params={})
            out = asyncio.run(run_generic_agent("operations", task,
                                                wiring))
        self.assertEqual(TaskStatus.WAITING.value, out["status"])
        self.assertIn("mirror", out["waiting_reason"])
        self.assertFalse([call for call in recorded if "prompt" in call])


class StagingTest(unittest.TestCase):
    def _skill(self, source: Path) -> Any:
        skill = mock.MagicMock()
        skill.source = str(source)
        return skill

    def test_links_created_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            (source / "SKILL.md").parent.mkdir(parents=True)
            (source / "SKILL.md").write_text("# s\n", encoding="utf-8")
            ticket = root / "ticket"
            ticket.mkdir()
            skills = {"crear-proyecto": self._skill(source)}
            self.assertEqual(["crear-proyecto"],
                             stage_skill_links(ticket, skills))
            link = ticket / ".codex" / "skills" / "crear-proyecto"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.path.realpath(source),
                             os.path.realpath(link))
            self.assertEqual(["crear-proyecto"],
                             stage_skill_links(ticket, skills))

    def test_stale_link_replaced_real_path_left(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old"
            new = root / "new"
            old.mkdir()
            new.mkdir()
            ticket = root / "ticket"
            base = ticket / ".codex" / "skills"
            base.mkdir(parents=True)
            (base / "crear-proyecto").symlink_to(old)
            (base / "blocked").mkdir()
            skills = {"crear-proyecto": self._skill(new),
                      "blocked": self._skill(new)}
            with self.assertLogs("orchestration.generic_agent",
                                 level="WARNING"):
                staged = stage_skill_links(ticket, skills)
            self.assertEqual(["crear-proyecto"], staged)
            self.assertEqual(os.path.realpath(new),
                             os.path.realpath(
                                 base / "crear-proyecto"))
            self.assertTrue((base / "blocked").is_dir()
                            and not (base / "blocked").is_symlink())

    def test_bad_sources_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ticket = Path(tmp) / "ticket"
            ticket.mkdir()
            missing = Path(tmp) / "nope"
            skills = {"ghost": self._skill(missing),
                      "relative": self._skill(Path("skills/x"))}
            self.assertEqual([], stage_skill_links(ticket, skills))
            self.assertFalse((ticket / ".codex").exists())


class RoleMountsTest(unittest.TestCase):
    def test_operations_mounts_repo_and_creation_roots(self) -> None:
        settings = Settings()
        read, write = role_extra_mounts(
            "operations", has_skills=True, settings=settings)
        self.assertEqual([str(catalog_root().parent)], read)
        self.assertEqual([str(Path(settings.PROJECT_REPOS_ROOT)),
                          str(ConfigLoader(settings).projects_root()),
                          str(Path(settings.VAULT_ROOT))],
                         write)

    def test_other_agents_write_nothing(self) -> None:
        settings = Settings()
        read, write = role_extra_mounts(
            "dev", has_skills=False, settings=settings)
        self.assertEqual(([], []), (read, write))
        read, write = role_extra_mounts(
            "secretary", has_skills=True, settings=settings)
        self.assertEqual([str(catalog_root().parent)], read)
        self.assertEqual([], write)


class FenceMountsTest(unittest.TestCase):
    def _plan(self, root: Path, **kwargs: Any) -> Any:
        workspace = root / "ws"
        workspace.mkdir(parents=True, exist_ok=True)
        home = root / "home"
        home.mkdir(parents=True, exist_ok=True)
        return plan_agent(argv=["true"], workspace=workspace, base={},
                          config_args=[], home=home, **kwargs)

    def _binds(self, plan: Any) -> list[tuple[str, str]]:
        argv = plan.argv
        found = []
        index = 0
        while index < len(argv) - 2:
            if argv[index] in ("--bind", "--ro-bind"):
                found.append((argv[index], argv[index + 1]))
                index += 3
            else:
                index += 1
        return found

    def test_extra_roots_mount_shallow_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outer = root / "outer"
            inner = outer / "inner"
            inner.mkdir(parents=True)
            plan = self._plan(root, extra_write_roots=[inner, outer])
        binds = [value for _op, value in self._binds(plan)
                 if value in (str(outer), str(inner))]
        self.assertEqual([str(outer), str(inner)], binds)

    def test_covering_root_rebinds_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self._plan(root, extra_write_roots=[root])
        binds = self._binds(plan)
        positions = [index for index, (_op, value) in enumerate(binds)
                     if value in (str(root), str(root / "ws"))]
        self.assertGreaterEqual(len(positions), 2)
        kinds = [binds[index][0] for index in positions]
        self.assertEqual("--bind", kinds[-1])
        self.assertEqual(str(root / "ws"), binds[positions[-1]][1])

    def test_repo_runtime_mask_does_not_swallow_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            workspace = repo / ".runtime" / "agent-work" / "t1"
            workspace.mkdir(parents=True)
            home = root / "home"
            home.mkdir()
            plan = plan_agent(argv=["true"], workspace=workspace, base={},
                              config_args=[], home=home,
                              extra_write_roots=[root])
        argv = plan.argv
        masks = [index for index in range(len(argv) - 1)
                 if argv[index] == "--tmpfs"
                 and argv[index + 1] == str(repo / ".runtime")]
        self.assertEqual(1, len(masks))
        rebinds = [index for index in range(len(argv) - 2)
                   if argv[index] == "--bind"
                   and argv[index + 1] == str(workspace)
                   and argv[index + 2] == str(workspace)]
        self.assertGreaterEqual(len(rebinds), 2)
        self.assertGreater(rebinds[-1], masks[0])

    def test_same_depth_extras_keep_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "aaa"
            second = root / "bbb"
            first.mkdir()
            second.mkdir()
            plan = self._plan(root, extra_read_roots=[second, first])
        binds = [value for _op, value in self._binds(plan)
                 if value in (str(first), str(second))]
        self.assertEqual([str(second), str(first)], binds)

    def test_disjoint_roots_skip_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extra = root / "extra"
            extra.mkdir()
            plan = self._plan(root, extra_read_roots=[extra])
        binds = self._binds(plan)
        self.assertIn(("--ro-bind", str(extra)), binds)
        self.assertNotIn(("--bind", str(root / "ws")),
                         binds[binds.index(("--ro-bind", str(extra))) + 1:])

    def test_missing_or_broad_roots_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                self._plan(root,
                           extra_read_roots=[root / "missing"])
            with self.assertRaises(ValueError):
                self._plan(root, extra_write_roots=["/"])
            with self.assertRaises(ValueError):
                self._plan(root, extra_write_roots=[root / "home"])

    def test_secrets_masked_under_extra_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extra = root / "extra"
            (extra / ".runtime").mkdir(parents=True)
            (extra / ".env").write_text("K=v\n", encoding="utf-8")
            plan = self._plan(root, extra_write_roots=[extra])
        argv = plan.argv
        scratch = str(root / "ws" / ".openceo" / "sandbox" / "empty")
        self.assertIn(
            ["--ro-bind", scratch, str(extra / ".env")],
            [argv[index:index + 3] for index in range(len(argv) - 2)])
        tmpfs = [argv[index + 1]
                 for index in range(len(argv) - 1)
                 if argv[index] == "--tmpfs"]
        self.assertIn(str(extra / ".runtime"), tmpfs)


class CliSubprocessTest(unittest.TestCase):
    def _cli(self, *argv: str) -> tuple[int, dict[str, Any]]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=120, cwd=str(REPO))
        return proc.returncode, json.loads(proc.stdout)

    def test_roots_reports_three(self) -> None:
        code, payload = self._cli("roots")
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual({"repos", "projects", "vault"},
                         set(payload["result"]))

    @unittest.skipUnless(shutil.which("git"), "git required")
    def test_chain_is_idempotent_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roots = ["--repos-root", str(base / "repos"),
                     "--projects-root", str(base / "projects"),
                     "--vault-root", str(base / "vault")]
            (base / "repos").mkdir()
            (base / "vault").mkdir()
            (base / "vault" / "README.md").write_text(
                "# Vault\n", encoding="utf-8")
            repo = base / "repos" / "demo-214"
            subprocess.run(["git", "init", "-qb", "main", str(repo)],
                           check=True, capture_output=True, timeout=60)
            flags = ["--project-key", "demo-214",
                     "--display-name", "Demo 214"]
            code, first = self._cli("templates", *flags, *roots)
            self.assertEqual(0, code, first)
            self.assertTrue(first["result"]["committed"])
            code, second = self._cli("templates", *flags, *roots)
            self.assertEqual(0, code, second)
            self.assertFalse(second["result"]["committed"])
            code, config = self._cli("project-config", *flags, *roots)
            self.assertEqual(0, code, config)
            self.assertEqual([], config["result"]["errors"])
            loader = ConfigLoader(Settings(
                _env_file=None,
                PROJECTS_DIR=str(base / "projects")))
            self.assertEqual([], loader.validate_project("demo-214"))
            code, vault = self._cli("vault-entry", *flags, *roots)
            self.assertEqual(0, code, vault)
            self.assertTrue(vault["result"]["created_hub"])

    def test_dry_run_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roots = ["--repos-root", str(base / "repos"),
                     "--projects-root", str(base / "projects"),
                     "--vault-root", str(base / "vault")]
            for name in ("repos", "projects", "vault"):
                (base / name).mkdir()
            (base / "vault" / "README.md").write_text(
                "# Vault\n", encoding="utf-8")
            flags = ["--project-key", "demo-214", "--dry-run"]
            for command in ("templates", "project-config",
                            "vault-entry"):
                with self.subTest(command=command):
                    code, payload = self._cli(command, *flags, *roots)
                    self.assertEqual(0, code, payload)
                    self.assertTrue(payload["dry_run"])
            self.assertEqual([], list((base / "repos").iterdir()))
            self.assertEqual([], list((base / "projects").iterdir()))

    def test_bad_key_is_json_error(self) -> None:
        code, payload = self._cli("templates", "--project-key",
                                  "NOPE!!")
        self.assertEqual(1, code)
        self.assertFalse(payload["ok"])
        self.assertIn("project_key", payload["error"])


class GenericRunLiveTest(unittest.TestCase):
    def test_operations_run_stages_mounts_and_badges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorded: list[dict[str, Any]] = []
            badge_calls: list[dict[str, str]] = []
            wiring = GenericAgentWiring(
                settings=Settings(),
                repo=None,
                executor_factory=_scripted_factory(recorded, _HECHO),
                badge_provider_factory=_badge_recorder(badge_calls),
                work_base_dir=root / "agent-work",
                catalog=load_catalog())
            task = _live_task(params={**MIRROR, "skill": "crear-proyecto"})
            out = asyncio.run(run_generic_agent("operations", task,
                                                wiring))
            self.assertEqual(TaskStatus.COMPLETED.value, out["status"])
            self.assertIn("hecho", out["result_comment"])
            link = (root / "agent-work"
                    / "661267c6-fe1e-4d51-ab40-ceb04e5264bd"
                    / ".codex" / "skills" / "crear-proyecto")
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.path.realpath(SKILL_DIR),
                             os.path.realpath(link))
            built = [call for call in recorded if "factory_role" in call]
            self.assertEqual(1, len(built))
            spec = built[0]["spec"]
            self.assertEqual([str(REPO)], spec["extra_read_roots"])
            self.assertEqual(3, len(spec["extra_write_roots"]))
            runs = [call for call in recorded if "prompt" in call]
            self.assertIn("crear-proyecto", runs[0]["prompt"])
            self.assertEqual("TASK-2026-00214", badge_calls[0]["ticket"])


if __name__ == "__main__":
    unittest.main()
