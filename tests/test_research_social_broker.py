"""Real Mongo authority shared by two brokers; no X or LLM calls."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import research_support as support
from research.social import SocialBroker, SocialError, SocialSessions
from research.social_x import response_reason


@unittest.skipUnless(support.MONGO_AVAILABLE, "Mongo required")
class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = support.build_repo("openceo_test_social")
        await self.repo.connect()
        await self.repo.db.client.drop_database("openceo_test_social")
        self.tmp = tempfile.TemporaryDirectory()
        self.sessions = SocialSessions(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.b = SocialBroker(self.repo.db, self.sessions, clock=lambda: self.now)
        self.b2 = SocialBroker(self.repo.db, self.sessions, clock=lambda: self.now)
        await self.b.initialize()
        self.account = await self.b.register(owner_id="alice", expires_at=self.now + timedelta(days=1))
        self.scope = dict(account_id=self.account, project="jardal-openceo", owner="alice",
                          consumer="http:alice", operation="search", source="from:test")
        self.grant = await self.b.grant(account_id=self.account, actor_owner="alice",
                                       project=self.scope["project"], owner="alice", consumer="http:alice",
                                       sources=["from:test"], operations=["search"], expires_at=self.now + timedelta(hours=1))
        p = self.sessions.path(self.account, must_exist=False)
        p.touch(mode=0o600)

    async def asyncTearDown(self):
        await self.repo.db.client.drop_database("openceo_test_social")
        await self.repo.close()
        self.tmp.cleanup()

    async def test_all_scope_components_and_source_revalidated(self):
        await self.b.authorize(**self.scope)
        for key, value in (("owner", "bob"), ("project", "synthetic_other"), ("consumer", "other"),
                           ("source", "bookmarks"), ("operation", "dm"), ("account_id", "arbitrary")):
            with self.subTest(key=key), self.assertRaises(SocialError):
                await self.b.authorize(**{**self.scope, key: value})

    async def test_no_write_grants(self):
        with self.assertRaisesRegex(SocialError, "invalid_grant"):
            await self.b.grant(account_id=self.account, actor_owner="alice", project="jardal-openceo",
                               owner="alice", consumer="http:alice", sources=["post"],
                               operations=["publish"], expires_at=self.now + timedelta(hours=1))

    async def test_two_brokers_serialized_and_finally_release(self):
        async with self.b.lease(self.scope):
            with self.assertRaisesRegex(SocialError, "account_busy"):
                async with self.b2.lease(self.scope):
                    self.fail("second worker entered")
        async with self.b2.lease(self.scope):
            pass

    async def test_setup_and_retirement_serialize_and_remove_secret_files(self):
        async with self.b.lease(self.scope):
            with self.assertRaises(SocialError):
                await self.b2.retire(account_id=self.account, actor_owner="alice")
        self.assertTrue(self.sessions.path(self.account).exists())
        with self.assertRaises(SocialError):
            await self.b.retire(account_id=self.account, actor_owner="bob")
        await self.b.retire(account_id=self.account, actor_owner="alice")
        self.assertFalse(self.sessions.path(self.account, must_exist=False).exists())
        with self.assertRaisesRegex(SocialError, "access_denied"):
            await self.b.authorize(**self.scope)
        self.assertFalse((await self.b.grants.find_one({"grant_id": self.grant}))["active"])

    async def test_expired_holder_cannot_commit_or_release_new_lease(self):
        async with self.b.lease(self.scope, seconds=1) as old:
            self.now += timedelta(seconds=2)
            async with self.b2.lease(self.scope) as new:
                self.assertGreater(new["fence"], old["fence"])
                with self.assertRaisesRegex(SocialError, "lease_lost"):
                    await self.b._valid_lease(old)
                await self.b.accounts.update_one(old, {"$set": {"lease_until": self.now}})
                await self.b2._valid_lease(new)

    async def test_revocation_expiry_and_previous_artifact(self):
        class Adapter:
            async def read(self, **kw):
                return [{"post_id": "1", "url": "https://x.com/test/status/1", "text": "statement",
                         "cookies": "SECRET_CANARY", "raw_html": "SECRET_CANARY"}]
        self.b.adapter = Adapter()
        result = await self.b.read(scope=self.scope)
        self.assertEqual(result["classification"], "restricted")
        self.assertEqual(result["kind"], "declaration")
        self.assertNotIn("SECRET_CANARY", str(result))
        self.assertNotIn(self.tmp.name, str(result))
        await self.b.revoke(grant_id=self.grant, actor_owner="alice")
        with self.assertRaisesRegex(SocialError, "access_denied"):
            await self.b.get_artifact(result["artifact_id"], scope=self.scope)

    async def test_revocation_during_io_discards_artifact(self):
        broker = self.b
        class Adapter:
            async def read(self, **kw):
                await broker.revoke(grant_id=self_grant, actor_owner="alice")
                return [{"text": "discard"}]
        self_grant = self.grant
        self.b.adapter = Adapter()
        with self.assertRaisesRegex(SocialError, "access_denied"):
            await self.b.read(scope=self.scope)
        self.assertEqual(await self.b.artifacts.count_documents({}), 0)

    async def test_cancel_releases_shared_lease(self):
        entered = asyncio.Event()
        async def hold():
            async with self.b.lease(self.scope):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with self.b2.lease(self.scope):
            pass

    async def test_metadata_contains_no_session_and_expiry_denies(self):
        doc = await self.b.accounts.find_one({"account_id": self.account})
        self.assertNotIn("cookies", str(doc))
        self.assertNotIn(self.tmp.name, str(doc))
        self.now += timedelta(hours=2)
        with self.assertRaisesRegex(SocialError, "access_denied"):
            await self.b.authorize(**self.scope)

    async def test_provider_causes_and_no_leaked_body(self):
        class Adapter:
            async def read(self, **kw):
                raise SocialError("challenge_required")
        self.b.adapter = Adapter()
        with self.assertRaisesRegex(SocialError, "challenge_required"):
            await self.b.read(scope=self.scope)
        doc = await self.b.accounts.find_one({"account_id": self.account})
        self.assertEqual(doc["status"], "login_required")

    async def test_rate_limit_is_a_cooldown_that_spares_the_provider(self):
        calls = []

        class Adapter:
            async def read(self_, **kw):
                calls.append(1)
                if len(calls) == 1:
                    raise SocialError("rate_limited")
                return [{"post_id": "1", "url": "u", "text": "t", "published_at": ""}]
        self.b.adapter = Adapter()
        for _ in range(2):
            with self.assertRaisesRegex(SocialError, "rate_limited"):
                await self.b.read(scope=self.scope)
        self.assertEqual(len(calls), 1, "the second read must not reach X during the cooldown")
        self.now += timedelta(minutes=16)
        result = await self.b.read(scope=self.scope)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["items"][0]["post_id"], "1")

    async def test_paused_account_explains_itself_only_to_a_grant_holder(self):
        class Adapter:
            calls = 0
            reason = ""

            async def read(self_, **kw):
                Adapter.calls += 1
                raise SocialError(Adapter.reason)
        self.b.adapter = Adapter()
        for reason in ("challenge_required", "login_required"):
            with self.subTest(reason=reason):
                await self.b.accounts.update_one({"account_id": self.account}, {
                    "$set": {"status": "active"}, "$unset": {"status_reason": ""}})
                Adapter.reason, Adapter.calls = reason, 0
                with self.assertRaisesRegex(SocialError, reason):
                    await self.b.read(scope=self.scope)
                self.assertEqual(Adapter.calls, 1)
                # Paused: the holder is told why, and X is not contacted again.
                for _ in range(2):
                    with self.assertRaisesRegex(SocialError, reason):
                        await self.b.read(scope=self.scope)
                self.assertEqual(Adapter.calls, 1)
                # Anyone without a matching grant still sees the same access_denied.
                for key, value in (("consumer", "other"), ("owner", "bob"), ("source", "from:other")):
                    with self.assertRaisesRegex(SocialError, "access_denied"):
                        await self.b.authorize(**{**self.scope, key: value})
        # A new local login re-activates the account and reads work again.
        await self.b.accounts.update_one({"account_id": self.account}, {
            "$set": {"status": "active"}, "$unset": {"status_reason": ""}})

        class Healthy:
            async def read(self_, **kw):
                return [{"post_id": "9", "url": "u", "text": "t", "published_at": ""}]
        self.b.adapter = Healthy()
        self.assertEqual((await self.b.read(scope=self.scope))["items"][0]["post_id"], "9")


class LocalBoundaryTests(unittest.TestCase):
    def test_symlink_and_public_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            sessions = SocialSessions(root)
            account = "soc_" + "0" * 32
            p = sessions.path(account, must_exist=False)
            p.touch(mode=0o644)
            os.chmod(p, 0o644)
            with self.assertRaises(SocialError):
                sessions.path(account)
            p.unlink()
            p.symlink_to("/etc/passwd")
            with self.assertRaises(SocialError):
                sessions.path(account)
            with self.assertRaises(SocialError):
                sessions.path("../escape")

    def test_response_causes(self):
        class Response:
            headers = {}
            status_code = 403
            def json(self):
                return {}
        rep = Response()
        self.assertEqual(response_reason(rep), "access_restricted")
        rep.status_code = 429
        self.assertEqual(response_reason(rep), "rate_limited")
        rep.status_code = 401
        self.assertEqual(response_reason(rep), "login_required")
        rep.status_code = 200
        rep.json = lambda: {"errors": [{"code": 326}]}
        self.assertEqual(response_reason(rep), "challenge_required")
