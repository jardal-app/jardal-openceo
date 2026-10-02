#!/usr/bin/env python3
"""Local owner setup. Cookies are prompted invisibly; never CLI arguments or .env.

Run from root using .venv Python. The account/database is dedicated to this broker.
This tool does not create an X account or solve login/2FA/challenges.
"""
import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))


async def main(args):
    from settings import get_settings
    from db.mongo import MongoRepository
    from research.social import SocialBroker, SocialSessions
    s = get_settings()
    if not s.RESEARCH_SOCIAL_SESSION_DIR:
        raise SystemExit("Configure RESEARCH_SOCIAL_SESSION_DIR in openceo-core/.env")
    repo = MongoRepository(s)
    await repo.connect()
    try:
        broker = SocialBroker(repo.db, SocialSessions(s.RESEARCH_SOCIAL_SESSION_DIR))
        await broker.initialize()
        expires = datetime.now(timezone.utc) + timedelta(days=args.days)
        if args.action == "create":
            account_id = await broker.register(owner_id=args.owner, expires_at=expires)
            print(account_id)
        elif args.action == "cookies":
            # Local operator must name an account they own; identifiers never grant.
            if not await broker.accounts.find_one({"account_id": args.account, "owner_id": args.owner, "status": {"$ne": "retired"}}):
                raise SystemExit("Account unavailable")
            os.environ["TWS_TELEMETRY"] = "0"
            os.umask(0o077)
            from importlib.metadata import version
            from research.social_x import TWSCRAPE_VERSION
            if version("twscrape") != TWSCRAPE_VERSION:
                raise SystemExit("Install research/requirements-social.txt first")
            from twscrape import API
            from loguru import logger
            logger.remove()
            cookies = getpass.getpass("X cookies (auth_token and ct0; input hidden): ")
            async with broker.setup_lease(account_id=args.account, actor_owner=args.owner) as token:
                async with asyncio.timeout(30):
                    path = broker.sessions.path(args.account, must_exist=False)
                    api = API(str(path), raise_when_no_account=True, wait_timeout=0)
                    await api.pool.add_account_cookies("research-only", cookies)
                    os.chmod(path, 0o600)
                    broker.sessions.path(args.account)
                    updated = await broker.accounts.update_one({**token, "lease_until": {"$gt": datetime.now(timezone.utc)}},
                        {"$set": {"status": "active", "expires_at": expires}, "$unset": {"retry_after": "", "status_reason": ""}})
                    if not updated.matched_count:
                        raise SystemExit("Setup lease lost")
            print("Dedicated session saved")
        elif args.action == "grant":
            print(await broker.grant(account_id=args.account, actor_owner=args.owner, project="jardal-openceo",
                owner=args.owner, consumer="http:" + args.owner, operations=["search"], sources=[args.source], expires_at=expires))
        elif args.action == "revoke":
            await broker.revoke(grant_id=args.grant, actor_owner=args.owner)
            print("Grant revoked")
        elif args.action == "retire":
            await broker.retire(account_id=args.account, actor_owner=args.owner)
            print("Session, grants and artifacts removed")
    finally:
        await repo.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["create", "cookies", "grant", "revoke", "retire"])
    p.add_argument("--owner", required=True, help="Authenticated OpenCEO user_id")
    p.add_argument("--account", default="")
    p.add_argument("--source", default="")
    p.add_argument("--grant", default="")
    p.add_argument("--days", type=int, choices=range(1, 31), default=7)
    asyncio.run(main(p.parse_args()))
