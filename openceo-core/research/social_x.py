"""Pinned twscrape 0.20.1 in a separate, bounded, read-only helper process.

The CLI/model never receives this database or its pathname. Third-party logging,
telemetry, inherited proxies, global account pools and retry/rotation are excluded.

Request shape, paging and parsing are the pinned library's own. The helper only
swaps the library's ``QueueClient`` for one that performs a single authenticated
request, so a search carries exactly the parameters the library sends (including
``fieldToggles``) instead of a hand-copied variant that drifts on a version bump.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path

from research.social import SocialError

TWSCRAPE_VERSION = "0.20.1"
# Below the parent's 25 s kill: a slow read unwinds by itself and releases the
# library's account lock instead of being killed holding it.
HELPER_DEADLINE = 20


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
                with contextlib.suppress(ProcessLookupError):
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
    from twscrape import api as twscrape_api
    from twscrape.accounts_pool import NoAccountError
    from twscrape.http import HttpError, HttpStatusError
    from twscrape.queue_client import AbortReqError, QueueClient
    from twscrape.xclid import XClIdAccountError, XClIdParseError
    from loguru import logger
    logger.remove()

    class SingleRequest(QueueClient):
        """One authenticated request per client.

        The library's own paging asks ``get`` for more pages; the second call gets
        ``None``, which its generators read as "no more data". Its retry, account
        rotation and ban bookkeeping (``_check_rep``) are skipped on purpose: causes
        are classified here and the broker owns pause and cooldown state.
        """
        _requested = False

        async def req(self, method, url, params=None):
            if self._requested:
                return None
            self._requested = True
            ctx = await self._get_ctx()
            if ctx is None:
                raise SocialError("account_busy")
            try:
                rep = await ctx.req(method, url, params=params)
            except XClIdAccountError:
                # X served its logged-out web app: the session is no longer accepted.
                raise SocialError("login_required") from None
            except HttpStatusError as exc:
                raise SocialError("rate_limited" if exc.response.status_code == 429
                                  else "source_unavailable") from None
            except (XClIdParseError, AbortReqError, HttpError):
                raise SocialError("source_unavailable") from None
            reason = response_reason(rep)
            if reason:
                raise SocialError(reason)
            return rep

    original, twscrape_api.QueueClient = twscrape_api.QueueClient, SingleRequest
    try:
        api = API(pool=payload["path"], raise_when_no_account=True, wait_timeout=0)
        accounts = await api.pool.get_all()
        if len(accounts) != 1 or accounts[0].proxy:
            raise SocialError("single_account_required")
        if not accounts[0].has_session or not accounts[0].active:
            raise SocialError("login_required")
        # The broker's fenced Mongo lease is the only mutual exclusion that matters and
        # its cooldown the only rate-limit memory. The library locks the account for 15
        # minutes per request and a killed helper never releases that, so start clean.
        await api.pool.reset_locks()
        try:
            async with asyncio.timeout(HELPER_DEADLINE):
                return await _read(api, payload)
        except TimeoutError:
            raise SocialError("source_timeout") from None
        except NoAccountError:
            raise SocialError("account_busy") from None
    finally:
        twscrape_api.QueueClient = original


async def _read(api, payload):
    operation, source, limit = payload["operation"], payload["source"], payload["limit"]
    items = []
    if operation == "search":
        async with contextlib.aclosing(api.search(source, limit=limit)) as gen:
            async for post in gen:
                items.append(_post_payload(post))
                if len(items) >= limit:
                    break
    elif operation == "post":
        if not source.isdigit():
            raise SocialError("invalid_source")
        post = await api.tweet_details(int(source))
        if post is not None:
            items.append(_post_payload(post))
    elif operation == "account":
        user = await api.user_by_login(source)
        if user is not None:
            # Public bio only; follower graphs, emails, owner feeds and DMs excluded.
            items.append({"post_id": str(user.id), "url": f"https://x.com/{user.username}",
                          "text": user.rawDescription[:4000], "published_at": ""})
    else:
        raise SocialError("operation_denied")
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
