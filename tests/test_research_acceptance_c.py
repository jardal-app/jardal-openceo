"""Offline C acceptance through JWT/HTTP, real Mongo and dedicated secret state.

Does not assert live X availability. That needs the owner's local session.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI
import research_support as support
from test_research_facade import _FakeAuthDB, COOKIE
from auth.jwt import create_access_token
from research.social import SocialBroker, SocialSessions
from research.social_router import build_social_router


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required")
class AcceptanceC(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = support.build_repo("openceo_test_acceptance_c")
        await self.repo.connect()
        await self.repo.db.client.drop_database("openceo_test_acceptance_c")
        self.tmp = tempfile.TemporaryDirectory()
        class XFixture:
            calls = 0
            async def read(adapter, **kw):
                adapter.calls += 1
                return [{"post_id": "1", "url": "https://x.com/test/status/1", "text": "Account stated X",
                         "cookies": "SECRET_CANARY", "html": "SECRET_CANARY"}]
        self.adapter = XFixture()
        self.b = SocialBroker(self.repo.db, SocialSessions(self.tmp.name), adapter=self.adapter)
        await self.b.initialize()
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        self.account = await self.b.register(owner_id="alice", expires_at=expires)
        p = self.b.sessions.path(self.account, must_exist=False)
        p.write_text("SECRET_CANARY")
        p.chmod(0o600)
        self.grant = await self.b.grant(account_id=self.account, actor_owner="alice", project="jardal-openceo",
            owner="alice", consumer="http:alice", sources=["from:test"], operations=["search"], expires_at=expires)
        self.app = FastAPI()
        self.app.state.auth_db = _FakeAuthDB([{"user_id": x, "username": x, "email": x + "@example.org", "is_active": True,
            "all_permissions": ["run_research", "read_research"], "allowed_projects": ["jardal-openceo"]} for x in ["alice", "bob"]])
        self.app.include_router(build_social_router(get_broker=lambda r: self.b,
            project_cfg_for=lambda r,p: {"research": {"enabled": True, "allowed_account_refs": [self.account]}}, enabled=lambda: True))
        self.body = {"project_key": "jardal-openceo", "account_id": self.account, "operation": "search", "source": "from:test"}
    async def asyncTearDown(self):
        self.tmp.cleanup()
        await self.repo.db.client.drop_database("openceo_test_acceptance_c")
        await self.repo.close()
    def client(self, owner=None):
        cookies = {COOKIE: create_access_token(owner, owner)} if owner else {}
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test", cookies=cookies)
    async def test_unauthenticated_and_other_owner_cannot_read(self):
        async with self.client() as c:
            self.assertEqual((await c.post("/api/research/social/read", json=self.body)).status_code, 401)
        async with self.client("bob") as c:
            self.assertEqual((await c.post("/api/research/social/read", json=self.body)).status_code, 403)
        self.assertEqual(self.adapter.calls, 0)
    async def test_restricted_payload_no_model_and_revoke_old_evidence(self):
        async with self.client("alice") as c:
            r = await c.post("/api/research/social/read", json=self.body)
            self.assertEqual(r.status_code, 200)
            payload = r.json()
            self.assertEqual(payload["classification"], "restricted")
            self.assertEqual(payload["kind"], "declaration")
            self.assertNotIn("SECRET_CANARY", r.text)
            self.assertNotIn(self.tmp.name, r.text)
            await self.b.revoke(grant_id=self.grant, actor_owner="alice")
            old = await c.post("/api/research/social/artifacts/" + payload["artifact_id"], json=self.body)
            self.assertEqual(old.status_code, 404)
            self.assertEqual((await c.post("/api/research/social/read", json=self.body)).status_code, 403)
        self.assertEqual(self.adapter.calls, 1)
    async def test_restart_preserves_metadata_and_allows_only_scoped_read(self):
        self.b = SocialBroker(self.repo.db, SocialSessions(self.tmp.name), adapter=self.adapter)
        await self.b.initialize()
        async with self.client("alice") as c:
            self.assertEqual((await c.post("/api/research/social/read", json=self.body)).status_code, 200)
            invalid = {**self.body, "operation": "bookmarks"}
            self.assertEqual((await c.post("/api/research/social/read", json=invalid)).status_code, 403)
