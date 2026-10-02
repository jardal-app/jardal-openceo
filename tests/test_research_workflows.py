"""Schedule -> YAML -> registry -> real job persistence, without business Tasks."""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))
import research_support as support
from research.policies import Principal, ResearchAccessDenied, RESEARCH_RUN
from research.service import ResearchDependencies, ResearchService
from research.scheduler import ResearchScheduleRequest, build_schedule_document, execution_key_for
from research.workflows import ResearchExecutionContext, ResearchScheduleHandler, ResearchScheduleRuntime
from settings import Settings
from workflow_service import WorkflowService, WorkflowToolRegistry

ROOT = Path(__file__).resolve().parents[1]
OWNER = "usr_schedule_test"
PROJECT = "jardal-openceo"
CFG = {"research": {"enabled": True, "allowed_modes": ["quick"],
        "classification_ceiling": "public", "paid_budget_usd": "0.00",
        "adapters": {"brave": False, "serper": False, "firecrawl": False, "grounding": False}}}


def settings(**kw):
    return Settings(_env_file=None, DEEPSEEK_API_KEY="", RESEARCH_ENABLED=True,
                    WORKFLOWS_DIR=str(ROOT / "workflows"), **kw)


def principal(**kw):
    return Principal(user_id=OWNER, permissions=frozenset({RESEARCH_RUN}),
                     allowed_projects=frozenset({PROJECT}), **kw)


def context(**kw):
    return {"scope": "research", "research_context": ResearchExecutionContext(
        project_key=PROJECT, owner_id=OWNER, purpose="watch", principal=principal(),
        project_cfg=CFG, execution_key="sch_test:occurrence", **kw)}


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = Mock()
        self.service = Mock()
        self.service.submit = AsyncMock(return_value={"research_id": "res_1", "status": "queued", "_created": True})
        self.workflows = WorkflowService(settings=settings(), repo=self.repo, realtime_hub=Mock())
        self.workflows.set_research_service(self.service)

    async def test_yaml_admits_with_scope_and_documents_tool(self):
        wake = Mock()
        result = await self.workflows.execute_research_workflow(
            "research.investigate", {"question": "Cambios publicos"}, context=context(on_admitted=wake))
        request = self.service.submit.call_args.args[0]
        self.assertEqual((request.project_key, request.owner_id, request.idempotency_key),
                         (PROJECT, OWNER, "sch_test:occurrence"))
        self.assertEqual(result["research_id"], "res_1")
        wake.assert_called_once_with("res_1")
        self.assertIn("context", self.workflows.tool_registry()["research.investigate"]["signature"])
        self.assertTrue(self.workflows.tool_registry()["research.investigate"]["doc"])
        self.repo.assert_not_called()
        self.assertEqual(self.repo.method_calls, [])

    async def test_task_context_cannot_call_research_tool(self):
        for ctx in [{"task": {}}, {**context(), "task_id": "task_test"}, {**context(), "task": {}}]:
            with self.assertRaises(ResearchAccessDenied):
                await self.workflows._tools.call("research.investigate", {"request": {"question": "q"}}, ctx)
        self.service.submit.assert_not_called()

    async def test_identity_params_cannot_override_context(self):
        for field in ["owner_id", "project_key", "purpose", "mode", "idempotency_key"]:
            with self.assertRaises(ResearchAccessDenied):
                await self.workflows.execute_research_workflow(
                    "research.investigate", {"question": "q", field: "override"}, context=context())
        self.service.submit.assert_not_called()

    async def test_other_workflow_rejected_before_execution(self):
        with self.assertRaises(ValueError):
            await self.workflows.execute_research_workflow("ceo.horizon_daily", {}, context=context())
        self.service.submit.assert_not_called()

    async def test_yaml_cannot_call_business_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "research"
            folder.mkdir()
            (folder / "investigate.yaml").write_text(
                "workflow: research.investigate\nauto_match: false\nsteps:\n  - tool: core.complete\n")
            self.workflows._settings.WORKFLOWS_DIR = temp
            with self.assertRaises(ValueError):
                await self.workflows.execute_research_workflow("research.investigate", {}, context=context())
        self.assertEqual(self.repo.method_calls, [])

    async def test_terminal_replay_does_not_wake_dispatcher(self):
        self.service.submit.return_value = {"research_id": "res_1", "status": "completed", "_created": False}
        wake = Mock()
        result = await self.workflows.execute_research_workflow(
            "research.investigate", {"question": "q"}, context=context(on_admitted=wake))
        self.assertFalse(result["created"])
        wake.assert_not_called()

    async def test_disabled_coordinator_never_queries_repo(self):
        for research_enabled, scheduler_enabled in [(False, True), (True, False)]:
            cfg = settings(RESEARCH_SCHEDULER_ENABLED=scheduler_enabled)
            cfg.RESEARCH_ENABLED = research_enabled
            runtime = ResearchScheduleRuntime(repo=self.repo, workflows=self.workflows,
                service=self.service, settings=cfg, project_cfg_for=lambda _: CFG)
            self.assertFalse(await runtime.start())
            await runtime.stop()
        self.assertEqual(self.repo.method_calls, [])


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required for schedule replay integration")
class ScheduleIntegrationTests(unittest.IsolatedAsyncioTestCase):
    DB = "openceo_test_research_workflows"

    async def asyncSetUp(self):
        from motor.motor_asyncio import AsyncIOMotorClient
        self.client = AsyncIOMotorClient(support.MONGO_URL)
        await self.client.drop_database(self.DB)
        self.repo = support.build_repo(self.DB)
        self.repo._client = self.client
        await self.repo.ensure_indexes()
        self.now = datetime.now(timezone.utc)
        await self.repo.db.users.insert_one({"user_id": OWNER, "is_active": True,
            "all_permissions": [RESEARCH_RUN], "allowed_projects": [PROJECT]})
        self.service = ResearchService(settings=settings(), deps=ResearchDependencies(repo=self.repo))
        self.workflows = WorkflowService(settings=settings(), repo=self.repo, realtime_hub=Mock())
        self.workflows.set_research_service(self.service)
        self.wake = Mock()
        self.handler = ResearchScheduleHandler(repo=self.repo, workflows=self.workflows,
            resolve_principal=self.resolve, project_cfg_for=lambda _: CFG, on_admitted=self.wake)
        self.repo.create_task = AsyncMock(side_effect=AssertionError("business task created"))
        self.repo.patch_task = AsyncMock(side_effect=AssertionError("business task patched"))

    async def resolve(self, owner):
        from research.workflows import principal_resolver_for
        return await principal_resolver_for(self.repo)(owner)

    async def asyncTearDown(self):
        await self.client.drop_database(self.DB)
        self.client.close()

    async def claim(self):
        doc = build_schedule_document(project_key=PROJECT, owner_id=OWNER, purpose="watch",
            spec=ResearchScheduleRequest(workflow="research.investigate", interval_seconds=60,
                                        params={"question": "Cambios en fuente publica"}),
            dedupe_key=None, now=self.now - timedelta(minutes=1))
        await self.repo.create_research_schedule(doc)
        return await self.repo.claim_next_research_schedule(lease_owner="test", lease_seconds=120, now=self.now)

    async def test_tick_admits_once_and_never_writes_business_task(self):
        claimed = await self.claim()
        first = await self.handler(claimed)
        replay = await self.handler(claimed)
        self.assertEqual(first["research_id"], replay["research_id"])
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 1)
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)
        self.repo.create_task.assert_not_called()
        self.repo.patch_task.assert_not_called()

    async def test_lost_wake_retry_keeps_occurrence_key_and_job(self):
        claimed = await self.claim()
        self.wake.side_effect = RuntimeError("lost wake after persistence")
        from research.scheduler import ResearchScheduler
        scheduler = ResearchScheduler(repo=self.repo, handler=self.handler, coordinator_id="test", backoff_base=0)
        await scheduler._process(claimed)
        original_key = execution_key_for(claimed)
        self.wake.side_effect = None
        retry = await self.repo.claim_next_research_schedule(lease_owner="test", lease_seconds=120)
        self.assertEqual(execution_key_for(retry), original_key)
        await scheduler._process(retry)
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 1)
        stored = await self.repo.get_research_schedule(claimed["schedule_id"])
        self.assertEqual(stored["execution_key"], original_key)
        self.assertIsNone(stored["scheduled_for"])

    async def test_restart_after_admission_reconciles_same_occurrence(self):
        claimed = await self.claim()
        first = await self.handler(claimed)
        await self.repo.patch_research_schedule(claimed["schedule_id"], {
            "lease_expires_at": self.now - timedelta(hours=1)})
        await self.repo.recover_stale_research_schedules(self.now - timedelta(minutes=15), now=self.now)
        retry = await self.repo.claim_next_research_schedule(lease_owner="test", lease_seconds=120, now=self.now)
        self.assertTrue(retry["pending_reconcile"])
        self.assertEqual(execution_key_for(retry), execution_key_for(claimed))
        result = await self.handler(retry)
        self.assertEqual(first["research_id"], result["research_id"])
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 1)

    async def test_inactive_owner_or_revoked_project_denied(self):
        claimed = await self.claim()
        await self.repo.db.users.update_one({"user_id": OWNER}, {"$set": {"is_active": False}})
        with self.assertRaises(ResearchAccessDenied):
            await self.handler(claimed)
        await self.repo.db.users.update_one({"user_id": OWNER}, {"$set": {
            "is_active": True, "allowed_projects": []}})
        with self.assertRaises(ResearchAccessDenied):
            await self.handler(claimed)
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 0)

    async def test_pause_cancel_and_stale_lease_skip_admission(self):
        claimed = await self.claim()
        await self.repo.patch_research_schedule(claimed["schedule_id"], {"fencing_token": 2})
        self.assertEqual(await self.handler(claimed), {"skipped": "stale_claim"})
        await self.repo.set_research_schedule_status(claimed["schedule_id"], "paused")
        self.assertEqual(await self.handler(claimed), {"skipped": "schedule_inactive"})
        await self.repo.set_research_schedule_status(claimed["schedule_id"], "cancelled")
        self.assertEqual(await self.handler(claimed), {"skipped": "schedule_inactive"})
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 0)

    async def test_pause_during_auth_lookup_prevents_admission(self):
        claimed = await self.claim()

        async def resolve_then_pause(owner):
            resolved = await self.resolve(owner)
            await self.repo.set_research_schedule_status(claimed["schedule_id"], "paused")
            return resolved

        self.handler.resolve_principal = resolve_then_pause
        self.assertEqual(await self.handler(claimed), {"skipped": "schedule_inactive"})
        self.assertEqual(await self.repo.research_jobs.count_documents({}), 0)

    async def test_enabled_runtime_wakes_from_store_and_stops(self):
        admitted = asyncio.Event()
        runtime = ResearchScheduleRuntime(repo=self.repo, workflows=self.workflows,
            service=self.service, settings=settings(RESEARCH_SCHEDULER_ENABLED=True),
            project_cfg_for=lambda _: CFG, on_admitted=lambda _: admitted.set())
        self.assertTrue(await runtime.start())
        try:
            schedule, _ = await runtime.store.create(project_key=PROJECT, owner_id=OWNER, purpose="watch",
                spec={"workflow": "research.investigate", "interval_seconds": 60,
                      "params": {"question": "Cambios publicos"}}, now=self.now - timedelta(minutes=1))
            await asyncio.wait_for(admitted.wait(), timeout=3)
            self.assertEqual(await self.repo.research_jobs.count_documents({}), 1)
        finally:
            await runtime.stop()
        self.assertIsNone(runtime.scheduler._job)
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)
