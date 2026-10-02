"""The X helper against the REAL pinned twscrape, with a synthetic session and a fake transport.

No network and no account: cookies are synthetic and every HTTP exchange is canned.
These tests catch what only shows with the real library: an expired session reported
as ``source_unavailable`` (so the account was never paused), a search request that lost
the library's own ``fieldToggles``, and a helper killed mid-request leaving the account
locked for 15 minutes. They do not assert that X itself is reachable or accepts a session.
"""
import asyncio
import dataclasses
import json
import os
import sqlite3
import tempfile
import time
import unittest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import research_support  # noqa: F401 - puts openceo-core on sys.path
from research import social_x
from research.social import SocialError

try:
    INSTALLED = version("twscrape")
except PackageNotFoundError:
    INSTALLED = None


def _user(uid="42", name="test"):
    return {"__typename": "User", "rest_id": uid, "legacy": {
        "screen_name": name, "name": "Test Account", "created_at": "Mon Jan 01 00:00:00 +0000 2018",
        "description": "bio publica", "followers_count": 1, "friends_count": 1, "statuses_count": 1,
        "favourites_count": 1, "listed_count": 0, "media_count": 0, "location": "",
        "profile_image_url_https": "https://pbs.twimg.com/x.jpg"}}


def _tweet(tid, text, uid="42", name="test"):
    return {"__typename": "Tweet", "rest_id": tid,
            "core": {"user_results": {"result": _user(uid, name)}},
            "legacy": {"id_str": tid, "user_id_str": uid, "created_at": "Fri Oct 02 12:00:00 +0000 2026",
                       "lang": "es", "full_text": text, "reply_count": 0, "retweet_count": 0,
                       "favorite_count": 0, "quote_count": 0, "conversation_id_str": tid,
                       "entities": {"hashtags": [], "symbols": [], "user_mentions": [], "urls": []}}}


def _entry(tweet):
    return {"entryId": f"tweet-{tweet['rest_id']}", "content": {
        "entryType": "TimelineTimelineItem", "itemContent": {"tweet_results": {"result": tweet}}}}


def search_page(tweets, cursor=None):
    entries = [_entry(t) for t in tweets]
    if cursor:
        entries.append({"entryId": "cursor-bottom-1", "content": {"cursorType": "Bottom", "value": cursor}})
    return {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": entries}]}}}}}


def detail_page(tweet):
    return {"data": {"threaded_conversation_with_injections_v2": {"instructions": [
        {"type": "TimelineAddEntries", "entries": [_entry(tweet)]}]}}}


def profile_page():
    return {"data": {"user": {"result": _user()}}}


def canned(status=200, body=None, headers=None):
    from twscrape.http import Response
    body = {} if body is None else body
    text = json.dumps(body)
    return Response(SimpleNamespace(
        status_code=status, text=text, content=text.encode(), json=lambda: body,
        headers=headers or {"content-type": "application/json"},
        url="https://x.com/i/api/graphql", request=SimpleNamespace(method="GET", url="https://x.com")))


@unittest.skipUnless(INSTALLED == social_x.TWSCRAPE_VERSION, "pinned twscrape not installed")
class HelperAgainstRealLibrary(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from twscrape import API, queue_client
        from twscrape.account import Account
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = str(Path(tmp.name) / "accounts.db")
        self.api = API(self.path, raise_when_no_account=True, wait_timeout=0)
        await self.api.pool.add_account_cookies("research-only", "auth_token=synthetic; ct0=synthetic")
        os.chmod(self.path, 0o600)
        self.calls = []
        self.respond = lambda method, url, params: canned(200, {"data": {}})
        test = self

        class FakeHttp:
            backend = "fake"

            async def request(self, method, url, **kw):
                test.calls.append({"method": method, "url": url, "params": kw.get("params")})
                out = test.respond(method, url, kw.get("params"))
                return await out if asyncio.iscoroutine(out) else out

            async def aclose(self):
                pass

        self.xclid = AsyncMock(return_value=SimpleNamespace(calc=lambda method, path: "synthetic"))
        for patcher in (patch.object(queue_client.XClIdGenStore, "get", new=self.xclid),
                        patch.object(Account, "make_client", new=lambda self_, proxy=None: FakeHttp())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def payload(self, operation="search", source="from:test", limit=5):
        return {"path": self.path, "operation": operation, "source": source, "limit": limit}

    def locks(self):
        con = sqlite3.connect(self.path)
        try:
            return json.loads(con.execute("select locks from accounts").fetchone()[0])
        finally:
            con.close()

    # -- causes -----------------------------------------------------------------
    async def test_expired_session_is_login_required_without_any_graphql_request(self):
        from twscrape.xclid import XClIdAccountError
        # X serves its logged-out web app when it no longer accepts the cookies.
        self.xclid.side_effect = XClIdAccountError("Logged-out X web app")
        with self.assertRaisesRegex(SocialError, "login_required"):
            await social_x._worker(self.payload())
        self.assertEqual(self.calls, [])

    async def test_signing_and_transport_failures_are_source_unavailable(self):
        from twscrape.http import ConnectError, HttpStatusError
        from twscrape.queue_client import AbortReqError
        from twscrape.xclid import XClIdParseError
        for exc in (XClIdParseError("page changed"), AbortReqError(), ConnectError("down"),
                    HttpStatusError("HTTP 500", response=canned(500))):
            with self.subTest(exc=type(exc).__name__):
                self.xclid.side_effect = exc
                with self.assertRaisesRegex(SocialError, "source_unavailable"):
                    await social_x._worker(self.payload())

    async def test_rate_limit_while_signing_is_a_rate_limit(self):
        from twscrape.http import HttpStatusError
        self.xclid.side_effect = HttpStatusError("HTTP 429", response=canned(429))
        with self.assertRaisesRegex(SocialError, "rate_limited"):
            await social_x._worker(self.payload())

    async def test_graphql_causes_stay_distinct(self):
        cases = ((429, {}, "rate_limited"), (401, {}, "login_required"), (403, {}, "access_restricted"),
                 (200, {"errors": [{"code": 326, "message": "locked"}]}, "challenge_required"),
                 (200, {"errors": [{"code": 32, "message": "auth"}]}, "login_required"),
                 (200, {"errors": [{"code": 88, "message": "limit"}]}, "rate_limited"),
                 (500, {}, "source_unavailable"))
        for status, body, reason in cases:
            with self.subTest(status=status, reason=reason):
                self.respond = lambda m, u, p, status=status, body=body: canned(status, body)
                with self.assertRaisesRegex(SocialError, reason):
                    await social_x._worker(self.payload())

    # -- the library's own request and parsers ---------------------------------
    async def test_search_request_is_the_librarys_own(self):
        from twscrape import API
        self.respond = lambda m, u, p: canned(200, search_page([_tweet("1001", "uno")]))
        _ = [post async for post in API(self.path, raise_when_no_account=True, wait_timeout=0).search("from:test", limit=5)]
        library = self.calls[-1]
        self.calls.clear()
        await social_x._worker(self.payload())
        helper = self.calls[-1]
        self.assertEqual(helper["url"], library["url"])
        self.assertEqual(helper["params"], library["params"])
        # The hand-copied request this replaced silently dropped this parameter.
        self.assertIn("fieldToggles", helper["params"])

    async def test_never_more_than_one_graphql_request(self):
        from twscrape import API
        # A short page with a next cursor makes the library itself ask for page two.
        self.respond = lambda m, u, p: canned(200, search_page([_tweet("1001", "uno")], cursor="next"))
        _ = [post async for post in API(self.path, raise_when_no_account=True, wait_timeout=0).search("from:test", limit=5)]
        self.assertGreater(len(self.calls), 1, "premise: the library alone paginates")
        self.calls.clear()
        items = await social_x._worker(self.payload())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([i["post_id"] for i in items], ["1001"])

    async def test_real_parsers_feed_exactly_the_selected_fields(self):
        self.respond = lambda m, u, p: canned(200, search_page([_tweet("1001", "primer post"),
                                                                _tweet("1002", "segundo post")]))
        items = await social_x._worker(self.payload(limit=5))
        self.assertEqual(items[0], {"post_id": "1001", "url": "https://x.com/test/status/1001",
                                    "text": "primer post", "published_at": "2026-10-02T12:00:00+00:00"})
        self.assertEqual([i["post_id"] for i in items], ["1001", "1002"])
        self.assertEqual([i["post_id"] for i in await social_x._worker(self.payload(limit=1))], ["1001"])

    async def test_post_and_account_operations_and_bad_post_id(self):
        self.respond = lambda m, u, p: canned(200, detail_page(_tweet("1001", "primer post")))
        items = await social_x._worker(self.payload("post", "1001"))
        self.assertEqual([i["text"] for i in items], ["primer post"])
        self.respond = lambda m, u, p: canned(200, profile_page())
        self.assertEqual(await social_x._worker(self.payload("account", "test")),
                         [{"post_id": "42", "url": "https://x.com/test", "text": "bio publica", "published_at": ""}])
        with self.assertRaisesRegex(SocialError, "invalid_source"):
            await social_x._worker(self.payload("post", "not-a-number"))

    # -- the library's account lock ---------------------------------------------
    async def test_a_killed_helper_cannot_lock_the_account_for_fifteen_minutes(self):
        # What a SIGKILL leaves behind: the library's own 15 minute lock on the queue.
        await self.api.pool.lock_until("research-only", "SearchTimeline", int(time.time()) + 900)
        self.assertIn("SearchTimeline", self.locks())
        self.respond = lambda m, u, p: canned(200, search_page([_tweet("1001", "uno")]))
        self.assertEqual(len(await social_x._worker(self.payload())), 1)
        self.assertEqual(self.locks(), {})

    async def test_a_slow_read_unwinds_by_itself_and_releases_the_lock(self):
        async def hang(method, url, params):
            await asyncio.sleep(5)
        self.respond = hang
        with patch.object(social_x, "HELPER_DEADLINE", 0.3):
            with self.assertRaisesRegex(SocialError, "source_timeout"):
                await social_x._worker(self.payload())
        self.assertEqual(self.locks(), {})

    # -- preconditions and contract ---------------------------------------------
    async def test_exactly_one_proxyless_account_with_a_session(self):
        con = sqlite3.connect(self.path)
        con.execute("update accounts set proxy = 'http://127.0.0.1:9'")
        con.commit()
        con.close()
        with self.assertRaisesRegex(SocialError, "single_account_required"):
            await social_x._worker(self.payload())
        con = sqlite3.connect(self.path)
        con.execute("update accounts set proxy = null")
        con.commit()
        con.close()
        await self.api.pool.add_account_cookies("second", "auth_token=x; ct0=y")
        with self.assertRaisesRegex(SocialError, "single_account_required"):
            await social_x._worker(self.payload())

    async def test_inactive_account_is_login_required(self):
        await self.api.pool.set_active("research-only", False)
        with self.assertRaisesRegex(SocialError, "login_required"):
            await social_x._worker(self.payload())

    async def test_worker_restores_the_library_client(self):
        from twscrape import api as twscrape_api, queue_client
        await social_x._worker(self.payload())
        self.assertIs(twscrape_api.QueueClient, queue_client.QueueClient)

    def test_library_surface_the_helper_relies_on(self):
        from twscrape import api as twscrape_api, queue_client
        from twscrape.models import Tweet, User
        names = lambda cls: {f.name for f in dataclasses.fields(cls)}  # noqa: E731
        self.assertLessEqual({"id", "url", "date", "rawContent"}, names(Tweet))
        self.assertLessEqual({"id", "username", "rawDescription"}, names(User))
        self.assertIs(twscrape_api.QueueClient, queue_client.QueueClient)
        self.assertTrue(asyncio.iscoroutinefunction(queue_client.Ctx.req))


if __name__ == "__main__":
    unittest.main()
