"""Global Tier S config and project validation (Tier S design §3/§15, F0).

Covers ConfigLoader.validate_project against synthetic project dirs, the
global roster defaults, the one-shot migration planner (pure) and the
one-shot ERP wait comment. No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import yaml

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import tempfile  # noqa: E402

from agents.dev.consensus import CriticalConsensusOrchestrator  # noqa: E402
from agents.dev.routing import plan_routing_migration  # noqa: E402
from config_loader import ConfigLoader  # noqa: E402
from dev_session_service import DevSessionService  # noqa: E402
from settings import Settings  # noqa: E402
from state import DevProvider, DevSessionCreateRequest  # noqa: E402
from task_service import TaskService  # noqa: E402


def _loader_for(tmp: Path) -> ConfigLoader:
    return ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(tmp)))


class ValidateProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, project: str, body: str) -> None:
        path = self.root / project
        path.mkdir(parents=True, exist_ok=True)
        (path / "project.yaml").write_text(body, encoding="utf-8")

    def test_clean_project_is_valid(self) -> None:
        self._write("proj", "dev:\n  coding_tier: auto\n  default_provider: codex\n")
        self.assertEqual(_loader_for(self.root).validate_project("proj"), [])

    def test_missing_project_is_valid(self) -> None:
        self.assertEqual(_loader_for(self.root).validate_project("nope"), [])

    def test_coding_tool_is_obsolete(self) -> None:
        self._write("proj", "dev:\n  coding_tool: tier_s\n")
        errors = _loader_for(self.root).validate_project("proj")
        self.assertEqual(len(errors), 1)
        self.assertIn("dev.coding_tool", errors[0])

    def test_tier_s_section_is_rejected(self) -> None:
        self._write("proj", "dev:\n  tier_s:\n    review_rounds: 2\n    roles: {}\n")
        errors = _loader_for(self.root).validate_project("proj")
        self.assertEqual(len(errors), 1)
        self.assertIn("dev.tier_s", errors[0])
        self.assertIn("global", errors[0])

    def test_both_keys_reported_together(self) -> None:
        self._write("proj", "dev:\n  coding_tool: codex\n  tier_s:\n    enabled: true\n")
        errors = _loader_for(self.root).validate_project("proj")
        self.assertEqual(len(errors), 2)

    def test_global_defaults_carry_no_project_routing(self) -> None:
        loader = _loader_for(self.root)
        dev = loader.load_project("anything")["dev"]
        self.assertNotIn("coding_tool", dev)
        self.assertNotIn("review_rounds", dev["tier_s"])
        self.assertNotIn("max_plans", dev["tier_s"])
        roles = dev["tier_s"]["roles"]
        self.assertEqual(roles["planner"]["account"], "claudej")
        self.assertTrue(roles["planner"]["active"])
        self.assertEqual(roles["plan-reviewer"]["model"], "gpt-6.1-sol")
        reviewers = dev["tier_s"]["reviewers"]
        self.assertTrue(all(item.get("family") for item in reviewers))
        active_families = {item["family"] for item in reviewers
                           if item["active"] and not item.get("extra")}
        self.assertGreaterEqual(len(active_families), 3)
        tiers = dev["tiers"]
        self.assertEqual(tiers["tier_a"]["account"], "claudej")
        self.assertEqual(tiers["tier_b"]["account"], "codexj")
        self.assertEqual(tiers["tier_c"]["chain"], ["muse", "dsh", "claudep"])

    def test_tiers_section_is_rejected(self) -> None:
        self._write("proj", "dev:\n  tiers:\n    tier_a:\n      account: clauded\n")
        errors = _loader_for(self.root).validate_project("proj")
        self.assertEqual(len(errors), 1)
        self.assertIn("dev.tiers", errors[0])
        self.assertIn("global", errors[0])


class CreateSessionGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_project_refuses_session_creation(self) -> None:
        loader = MagicMock()
        loader.load_project = MagicMock(return_value={"dev": {}})
        loader.validate_project = MagicMock(return_value=["dev.coding_tool is obsolete"])
        project_service = MagicMock()
        project_service.resolve_project = AsyncMock(return_value="proj")
        service = DevSessionService(MagicMock(), AsyncMock(), loader, project_service, MagicMock())
        payload = DevSessionCreateRequest(project="proj", title="t", objective="o")
        with self.assertRaises(ValueError) as raised:
            await service.create_session(payload)
        self.assertIn("Invalid project config", str(raised.exception))


class InteractiveTriggerCleanupTests(unittest.TestCase):
    """Interactive Tier S has no trigger (product decision, F0 cleanup).

    ``dev.coding_tool`` was removed from project.yaml (§15.3), so the old
    AUTO + project-flag trigger can never fire and no consumer may read the
    key. The consensus machinery stays dormant for the autonomous queue;
    loose sessions resolve AUTO via default_provider only.
    """

    def test_consensus_is_never_enabled_from_config(self) -> None:
        self.assertFalse(CriticalConsensusOrchestrator.is_enabled({}))
        self.assertFalse(
            CriticalConsensusOrchestrator.is_enabled({"dev": {"coding_tool": "tier_s"}})
        )

    def test_auto_ignores_removed_coding_tool_key(self) -> None:
        service = DevSessionService(MagicMock(), AsyncMock(), MagicMock(), MagicMock(), MagicMock())
        config = {"dev": {"coding_tool": "opencode", "default_provider": "codex"}}
        session = {"provider": DevProvider.AUTO.value}
        self.assertEqual(service._resolve_provider(session, config), DevProvider.CODEX)


class PlanRoutingMigrationTests(unittest.TestCase):
    def test_buckets(self) -> None:
        tasks = [
            {"task_id": "ok", "coding_tool": "tier_s"},
            {"task_id": "legacy", "coding_tool": "codex"},
            {"task_id": "params", "params": {"coding_tool": "claude"}},
            {"task_id": "tier", "coding_tier": "tier3"},
            {"task_id": "bad-root", "coding_tool": "nope", "params": {"coding_tool": "kimi"}},
            {"task_id": "none"},
        ]
        plan = plan_routing_migration(tasks)
        self.assertEqual(plan.already_ok, 1)
        self.assertEqual(
            {entry["task_id"]: entry["patch"] for entry in plan.promoted},
            {"legacy": {"coding_tool": "tier_b"},
             "params": {"coding_tool": "tier_a"},
             "tier": {"coding_tool": "tier_c"},
             "bad-root": {"coding_tool": "tier_c"}},
        )
        self.assertTrue(all(entry["warnings"] for entry in plan.promoted))
        self.assertEqual(plan.needs_assignment, ["none"])

    def test_idempotent(self) -> None:
        tasks = [{"task_id": "a", "params": {"coding_tool": "codex"}}]
        plan = plan_routing_migration(tasks)
        migrated = [{**tasks[0], **plan.promoted[0]["patch"]}]
        rerun = plan_routing_migration(migrated)
        self.assertEqual(rerun.promoted, [])
        self.assertEqual(rerun.needs_assignment, [])
        self.assertEqual(rerun.already_ok, 1)


class WaitErpCommentTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, adapter=None):
        service = TaskService.__new__(TaskService)
        service._erpnext = adapter
        service._repo = MagicMock()
        service._repo.patch_task = AsyncMock()
        return service

    def _task(self, **overrides):
        task = {"task_id": "task-1",
                "params": {"_mirrors": {"erpnext": {"name": "TASK-1"}}}}
        task.update(overrides)
        return task

    async def test_routing_wait_comments_once(self) -> None:
        adapter = MagicMock()
        adapter.is_enabled = True
        adapter.add_comment = AsyncMock()
        service = self._service(adapter)
        await service._comment_wait_on_erp(self._task(), "routing_missing: no value")
        adapter.add_comment.assert_awaited_once()
        args = adapter.add_comment.await_args.args
        self.assertEqual(args[0], "TASK-1")
        self.assertIn("coding_tool", args[1])
        service._repo.patch_task.assert_awaited_once_with(
            "task-1", {"wait_commented_for": "routing_missing"})

    async def test_already_commented_reason_stays_silent(self) -> None:
        adapter = MagicMock()
        adapter.is_enabled = True
        adapter.add_comment = AsyncMock()
        service = self._service(adapter)
        await service._comment_wait_on_erp(
            self._task(wait_commented_for="config"), "config: bad key")
        adapter.add_comment.assert_not_called()

    async def test_missing_mirror_or_adapter_stays_silent(self) -> None:
        adapter = MagicMock()
        adapter.is_enabled = True
        adapter.add_comment = AsyncMock()
        service = self._service(adapter)
        await service._comment_wait_on_erp({"task_id": "t", "params": {}}, "config: bad")
        adapter.add_comment.assert_not_called()
        await self._service(None)._comment_wait_on_erp(self._task(), "config: bad")


class LooseSessionDefaultsTests(unittest.TestCase):
    """Loose dev sessions use the owner's wrappers, muse by default.

    Product decision (F0 cleanup): every checked-in project resolves AUTO
    to plain muse (no builder flags), and the explicit codex/claude entries
    run the owner's account wrappers. codexj stays Tier S-only.
    """

    def test_all_projects_default_to_muse_with_owner_wrappers(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        loader = _loader_for(repo / "projects")
        self.assertTrue(loader.available_projects())
        for name in loader.available_projects():
            with (repo / "projects" / name / "project.yaml").open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
            dev = (data.get("dev") or {}) if isinstance(data, dict) else {}
            self.assertEqual(dev.get("default_provider"), "muse", name)
            providers = dev.get("providers") or {}
            self.assertEqual(
                (providers.get("muse") or {}).get("command"),
                ["/home/develop/.local/bin/muse"], name)
            codex_cmd = (providers.get("codex") or {}).get("command") or []
            self.assertTrue(codex_cmd and str(codex_cmd[0]).endswith("codexf"), name)
            claude_cmd = (providers.get("claude") or {}).get("command") or []
            self.assertTrue(claude_cmd and str(claude_cmd[0]).endswith("claudej"), name)


if __name__ == "__main__":
    unittest.main()
