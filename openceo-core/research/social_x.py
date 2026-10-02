"""Pinned twscrape 0.20.1 in a separate, bounded, read-only helper process.

The CLI/model never receives this database or its pathname. Third-party logging,
telemetry, inherited proxies, global account pools and retry/rotation are excluded.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from research.social import SocialError

TWSCRAPE_VERSION = "0.20.1"


class XReader:
    async def read(self, *, path: Path, operation: str, source: str, limit: int):
        if operation not in {"search", "post", "account"} or not source or len(source) > 500:
            raise SocialError("invalid_source")
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "TWS_TELEMETRY": "0", "DO_NOT_TRACK": "1",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "research.social_x", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, start_new_session=True)
        try:
            async with asyncio.timeout(25):
                proc.stdin.write(json.dumps({"path": str(path), "operation": operation,
                                             "source": source, "limit": min(limit, 5)}).encode())
                await proc.stdin.drain()
                proc.stdin.close()
                output = bytearray()
                while chunk := await proc.stdout.read(16_384):
                    output.extend(chunk)
                    if len(output) > 100_000:
                        raise SocialError("source_output_limit")
                await proc.wait()
            payload = json.loads(output)
            if not payload.get("ok"):
                raise SocialError(payload.get("reason", "source_unavailable"))
            return payload["items"]
        except (TimeoutError, json.JSONDecodeError):
            raise SocialError("source_timeout" if proc.returncode is None else "source_unavailable") from None
        finally:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()


def response_reason(rep) -> str | None:
    """Classify from structured codes, never infer expired login from any 403."""
    try:
        codes = {int(x.get("code", -1)) for x in rep.json().get("errors", [])}
    except (ValueError, TypeError, AttributeError):
        codes = set()
    if rep.status_code == 429 or 88 in codes or rep.headers.get("x-rate-limit-remaining") == "0":
        return "rate_limited"
    if 326 in codes:
        return "challenge_required"
    if rep.status_code == 401 or 32 in codes:
        return "login_required"
    if rep.status_code == 403:
        return "access_restricted"
    if rep.status_code >= 400:
        return "source_unavailable"
    return None


async def _worker(payload):
    from importlib.metadata import version
    if version("twscrape") != TWSCRAPE_VERSION:
        raise SocialError("version_mismatch")
    # umask also protects SQLite journals created by the library, in this process only.
    os.umask(0o077)
    from twscrape import API
    from twscrape.api import GQL_FEATURES, GQL_URL, encode_params
    from twscrape.queue_client import QueueClient
    from twscrape.accounts_pool import NoAccountError
    from loguru import logger
    logger.remove()

    class SingleRequest(QueueClient):
        async def req(self, method, url, params=None):
            ctx = await self._get_ctx()
            if ctx is None:
                raise SocialError("account_busy")
            rep = await ctx.req(method, url, params=params)
            reason = response_reason(rep)
            if reason:
                if reason == "rate_limited":
                    reset = int(rep.headers.get("x-rate-limit-reset", "0"))
                    await self._close_ctx(max(reset, int(__import__("time").time()) + 60))
                raise SocialError(reason)
            return rep

    class BoundedAPI(API):
        # Version-specific hooks verified against the 0.20.1 wheel. One GQL page,
        # no empty-page retries; parsers remain those of the pinned library.
        async def _gql_item(self, op, kv, ft=None):
            async with SingleRequest(self.pool, op.split("/")[-1]) as client:
                return await client.get(f"{GQL_URL}/{op}", params=encode_params(
                    {"variables": kv, "features": {**GQL_FEATURES, **(ft or {})}}))

        async def _gql_items(self, op, kv, ft=None, limit=-1, cursor_type="Bottom"):
            rep = await self._gql_item(op, kv, ft)
            if rep is not None:
                yield rep

    api = BoundedAPI(pool=payload["path"], raise_when_no_account=True, wait_timeout=0)
    accounts = await api.pool.get_all()
    if len(accounts) != 1 or accounts[0].proxy:
        raise SocialError("single_account_required")
    if not accounts[0].has_session or not accounts[0].active:
        raise SocialError("login_required")
    items = []
    try:
        if payload["operation"] == "search":
            from contextlib import aclosing
            async with aclosing(api.search(payload["source"], limit=payload["limit"])) as gen:
                async for post in gen:
                    items.append(_post_payload(post))
                    if len(items) >= payload["limit"]:
                        break
        elif payload["operation"] == "post":
            post = await api.tweet_details(int(payload["source"]))
            if post is not None:
                items.append(_post_payload(post))
        elif payload["operation"] == "account":
            user = await api.user_by_login(payload["source"])
            if user is not None:
                # Public bio only; follower graphs, emails, owner feeds and DMs excluded.
                items.append({"post_id": str(user.id), "url": f"https://x.com/{user.username}",
                              "text": user.rawDescription[:4000], "published_at": ""})
        else:
            raise SocialError("operation_denied")
    except NoAccountError:
        raise SocialError("account_busy") from None
    return items


def _post_payload(post):
    return {"post_id": str(post.id), "url": post.url, "text": post.rawContent[:4000],
            "published_at": post.date.isoformat()}


if __name__ == "__main__":
    try:
        items = asyncio.run(_worker(json.load(sys.stdin)))
        print(json.dumps({"ok": True, "items": items}))
    except SocialError as exc:
        print(json.dumps({"ok": False, "reason": exc.reason}))
    except Exception:
        print(json.dumps({"ok": False, "reason": "source_unavailable"}))
