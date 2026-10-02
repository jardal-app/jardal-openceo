"""Offline D E2E. Public source/model fixtures do not measure real model quality."""
import asyncio
import tempfile
import unittest

import httpx
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
import uvicorn

import research_support as support
from test_research_facade import _FakeAuthDB, COOKIE
from test_research_deep import Model
from research.contracts import ResearchJobRequest
from research.consumers import ConsumerRegistry
from research.evidence import EvidenceVault
from research.events import ResearchEvents
from research.mcp_facade import build_research_mcp_facade
from research.policies import Principal
from research.router import build_research_router
from research.service import ResearchDependencies, ResearchService
from research.wiring import ProjectEvidence
from auth.jwt import create_access_token
from settings import Settings


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required")
class AcceptanceD(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = support.build_repo("openceo_test_acceptance_d")
        await self.repo.connect()
        await self.repo.db.client.drop_database("openceo_test_acceptance_d")
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = EvidenceVault(root=self.tmp.name)
        self.evidence = ProjectEvidence(self.vault)
        self.events = ResearchEvents(self.repo.db, self.evidence)
        await self.events.initialize()
        self.model = Model()
        async def reader(**kw):
            return {"url": kw["url"], "text": "Revenue is 10"}
        self.service = ResearchService(settings=Settings(_env_file=None, RESEARCH_ENABLED=True),
            deps=ResearchDependencies(repo=self.repo, read=reader, evidence=self.evidence, deep_model=self.model, events=self.events))
        self.cfg = {"research": {"enabled": True, "allowed_modes": ["deep"], "classification_ceiling": "public",
                                "paid_budget_usd": "0.00", "limits": {"deep": {"search_requests": 0, "pages": 2, "steps": 30, "seconds": 30}}}}
        principal = Principal(user_id="alice", permissions=frozenset({"run_research", "read_research"}), allowed_projects=frozenset({"jardal-openceo"}))
        self.job = await self.service.submit(ResearchJobRequest(project_key="jardal-openceo", purpose="test",
            question="Revenue?", mode="deep", seed_urls=["https://example.org/public"],
            limits={"search_requests": 0, "pages": 2, "steps": 30, "seconds": 30}), principal=principal, project_cfg=self.cfg)
    async def asyncTearDown(self):
        self.vault.close()
        self.tmp.cleanup()
        await self.repo.db.client.drop_database("openceo_test_acceptance_d")
        await self.repo.close()
    def app(self):
        app = FastAPI()
        app.state.auth_db = _FakeAuthDB([{"user_id": x, "username": x, "email": x + "@example.org", "is_active": True,
            "all_permissions": ["run_research", "read_research"], "allowed_projects": ["jardal-openceo"]} for x in ["alice", "bob"]])
        app.include_router(build_research_router(get_service=lambda r: self.service, project_cfg_for=lambda r,p:self.cfg))
        return app
    async def test_deep_persisted_then_events_with_actual_jwt_and_scope(self):
        job = await self.service.run_once()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["usage"]["model_calls"], 2)
        self.assertFalse(job["event_delivery_pending"])
        self.assertEqual(await self.repo.tasks.count_documents({}), 0)
        for owner, expected in (("alice", 1), ("bob", 0)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app()), base_url="http://test",
                cookies={COOKIE: create_access_token(owner, owner)}) as c:
                r = await c.get("/api/research/events", params={"project_key": "jardal-openceo"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(len(r.json()["events"]), expected)
    async def test_restart_replays_pending_delivery_without_model_spend(self):
        self.service._deps.events = None
        job = await self.service.run_once()
        await self.repo.patch_research_job(job["research_id"], {"event_delivery_pending": True})
        self.service._deps.events = self.events
        count = len(self.model.calls)
        self.assertEqual(await self.service.recover_event_delivery(), 1)
        self.assertEqual(await self.service.recover_event_delivery(), 0)
        self.assertEqual(len(self.model.calls), count)
        self.assertEqual(len(await self.events.list(project="jardal-openceo", owner="alice")), 1)
    async def _completed_job_with_pending_delivery(self, question):
        """A terminal job with its result stored and its event not yet delivered."""
        principal = Principal(user_id="alice", permissions=frozenset({"run_research", "read_research"}),
                              allowed_projects=frozenset({"jardal-openceo"}))
        await self.service.submit(ResearchJobRequest(project_key="jardal-openceo", purpose="test",
            question=question, mode="deep", seed_urls=["https://example.org/public"],
            limits={"search_requests": 0, "pages": 2, "steps": 30, "seconds": 30}), principal=principal, project_cfg=self.cfg)
        events, self.service._deps.events = self.service._deps.events, None
        try:
            job = await self.service.run_once()
        finally:
            self.service._deps.events = events
        await self.repo.patch_research_job(job["research_id"], {"event_delivery_pending": True})
        return job

    async def test_a_row_whose_event_can_never_publish_neither_blocks_startup_nor_repeats(self):
        from copy import deepcopy
        good = await self._completed_job_with_pending_delivery("Revenue?")
        for label, damage in (("scope", lambda r: r.update(project_key="synthetic_other")),
                              ("schema", lambda r: r.clear())):
            with self.subTest(label):
                bad = await self._completed_job_with_pending_delivery("Revenue " + label + "?")
                result = deepcopy(bad["result"])
                damage(result)
                await self.repo.patch_research_job(bad["research_id"], {"result": result})
                # The boot-time reconciliation must survive the poisoned row and still serve the rest.
                await self.service.recover_event_delivery()
                row = await self.repo.get_research_job(bad["research_id"])
                self.assertFalse(row["event_delivery_pending"])
                self.assertTrue(row["event_delivery_failed"])
                self.assertEqual(await self.service.recover_event_delivery(), 0, "must not retry at every boot")
        self.assertFalse((await self.repo.get_research_job(good["research_id"]))["event_delivery_pending"])
        self.assertEqual(len(await self.events.list(project="jardal-openceo", owner="alice")), 1)

    async def test_a_publish_failure_never_loses_the_terminal_job(self):
        class Refusing:
            async def publish(self_, job, **kw):
                raise RuntimeError("storage down")
        self.service._deps.events = Refusing()
        job = await self.service.run_once()
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["result"]["claims"])
        # A transient failure keeps the mark so the next startup retries it.
        self.assertTrue((await self.repo.get_research_job(job["research_id"]))["event_delivery_pending"])
        self.service._deps.events = self.events
        self.assertEqual(await self.service.recover_event_delivery(), 1)

    async def test_real_mcp_transport_requires_explicit_event_grant_and_revocation(self):
        await self.service.run_once()
        registry = ConsumerRegistry()
        denied = registry.register(consumer_id="test", owner_id="alice", projects=["jardal-openceo"])
        allowed = registry.register(consumer_id="test-events", owner_id="alice", projects=["jardal-openceo"], operations=["events"])
        server = build_research_mcp_facade(consumers=registry, get_service=lambda:self.service, project_cfg_for=lambda p:self.cfg)
        mcp_app = server.http_app(path="/")
        uv = uvicorn.Server(uvicorn.Config(mcp_app, host="127.0.0.1", port=0, log_level="error"))
        task = asyncio.create_task(uv.serve())
        try:
            async with asyncio.timeout(5):
                while not uv.started:
                    await asyncio.sleep(.01)
            port = uv.servers[0].sockets[0].getsockname()[1]
            for token, ok in ((denied, False), (allowed, True)):
                transport = StreamableHttpTransport(f"http://127.0.0.1:{port}/", headers={"Authorization": "Bearer " + token})
                async with Client(transport) as client:
                    result = (await client.call_tool("openceo_research_events", {"project_key": "jardal-openceo"})).data
                self.assertEqual(result["ok"], ok)
                if ok:
                    self.assertEqual(len(result["events"]), 1)
            registry.revoke(allowed)
            async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/", headers={"Authorization": "Bearer " + allowed})) as client:
                self.assertFalse((await client.call_tool("openceo_research_events", {"project_key": "jardal-openceo"})).data["ok"])
        finally:
            uv.should_exit = True
            await task
