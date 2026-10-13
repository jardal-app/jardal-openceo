"""Workflow discovery is gone (TASK-2026-00215): no route creates it.

Regression with zero tokens: the YAML engine stays (catalog,
deterministic executor, live workflows), but no code path starts a
DeepSeek discovery, no discovery-runs API exists, and no model or
store method serves it. No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from workflow_service import WorkflowService  # noqa: E402


class DiscoveryRemovedTests(unittest.TestCase):
    def test_service_has_no_discovery_surface(self) -> None:
        for name in (
            "start_discovery_for_task",
            "approve_discovery_run",
            "_build_discovery_proposal",
            "_fallback_workflow_yaml",
            "_should_start_discovery",
            "_save_workflow_yaml",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(WorkflowService, name))

    def test_models_and_store_have_no_discovery(self) -> None:
        import db.mongo as mongo_mod
        import state as state_mod

        self.assertFalse(hasattr(state_mod, "DiscoveryRunStatus"))
        self.assertFalse(
            hasattr(state_mod, "DiscoveryApprovalRequest"))
        self.assertFalse(
            hasattr(mongo_mod.MongoRepository, "create_discovery_run"))
        self.assertFalse(
            hasattr(mongo_mod.MongoRepository, "get_discovery_run"))
        self.assertFalse(
            hasattr(mongo_mod.MongoRepository, "list_discovery_runs"))
        self.assertFalse(
            hasattr(mongo_mod.MongoRepository, "patch_discovery_run"))

    def test_api_has_no_discovery_routes(self) -> None:
        import main as main_mod

        paths = [
            getattr(route, "path", "")
            for route in main_mod.app.routes
        ]
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path):
                self.assertNotIn("discovery-runs", path)
                self.assertNotIn("discover-workflow", path)

    def test_engine_still_runs_live_workflows(self) -> None:
        import asyncio

        from settings import Settings

        service = WorkflowService(
            settings=Settings(_env_file=None),  # type: ignore[call-arg]
            repo=None,  # type: ignore[arg-type]
            realtime_hub=None,  # type: ignore[arg-type]
        )
        workflows = service.list_workflows()
        self.assertGreater(len(workflows), 0)
        self.assertIsNone(
            asyncio.run(service.run_for_task(
                {"task_id": "t-1", "title": "No workflow matches",
                 "params": {"discover_workflow": True}})))


if __name__ == "__main__":
    unittest.main()
