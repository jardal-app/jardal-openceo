"""Read-only social authority. Mongo coordinates grants and fenced account leases.

Session databases stay in a private directory outside the repo, evidence and model
workspace. Account references are identifiers, never credentials. No model is used.
"""
from __future__ import annotations

import asyncio
import os
import re
import stat
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument

READ_OPERATIONS = frozenset({"search", "post", "account"})


class SocialError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)  # never interpolate provider bodies or session paths


class SocialSessions:
    """One protected SQLite per opaque account, with no backup by this service.

    Sessions persist until explicit removal or local re-login. Never include this
    directory in evidence/vault backups; revocation denies access immediately.
    """
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise SocialError("unsafe_session_directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = self.root.stat().st_mode
        if self.root.stat().st_uid != os.getuid() or mode & 0o077:
            raise SocialError("unsafe_session_directory")

    def path(self, account_id: str, *, must_exist: bool = True) -> Path:
        if not re.fullmatch(r"soc_[0-9a-f]{32}", account_id):
            raise SocialError("account_unavailable")
        path = self.root / f"{account_id}.db"
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
            if candidate.is_symlink():
                raise SocialError("unsafe_session_file")
            if candidate.exists():
                st = candidate.stat()
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise SocialError("unsafe_session_file")
        if must_exist and not path.exists():
            raise SocialError("login_required")
        return path


class SocialBroker:
    def __init__(self, db: Any, sessions: SocialSessions, *, adapter: Any = None, clock=None):
        self.accounts = db.research_social_accounts
        self.grants = db.research_social_grants
        self.artifacts = db.research_social_artifacts
        self.sessions = sessions
        self.adapter = adapter
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def initialize(self):
        await self.accounts.create_index("account_id", unique=True)
        await self.grants.create_index("grant_id", unique=True)
        await self.artifacts.create_index("artifact_id", unique=True)
        await self.artifacts.create_index("expires_at", expireAfterSeconds=0)

    async def register(self, *, owner_id: str, expires_at: datetime) -> str:
        if not owner_id or expires_at <= self.clock():
            raise SocialError("invalid_account")
        account_id = "soc_" + uuid4().hex
        await self.accounts.insert_one({
            "account_id": account_id, "owner_id": owner_id, "platform": "x",
            "status": "active", "expires_at": expires_at, "fence": 0,
            "lease_until": self.clock(), "created_at": self.clock(),
        })
        return account_id

    async def grant(self, *, account_id: str, actor_owner: str, project: str,
                    owner: str, consumer: str, sources: list[str],
                    operations: list[str], expires_at: datetime) -> str:
        account = await self.accounts.find_one({"account_id": account_id, "owner_id": actor_owner,
                                                 "status": "active"})
        if account is None:
            raise SocialError("account_unavailable")
        if (not project or not owner or not consumer or not sources or not operations
                or not set(operations) <= READ_OPERATIONS or expires_at <= self.clock()):
            raise SocialError("invalid_grant")
        grant_id = "sg_" + uuid4().hex
        await self.grants.insert_one({
            "grant_id": grant_id, "account_id": account_id, "project": project,
            "owner": owner, "consumer": consumer, "sources": list(sources),
            "operations": list(operations), "expires_at": expires_at, "active": True,
        })
        return grant_id

    async def authorize(self, *, account_id: str, project: str, owner: str,
                        consumer: str, operation: str, source: str):
        if operation not in READ_OPERATIONS:
            raise SocialError("operation_denied")
        now = self.clock()
        account = await self.accounts.find_one({"account_id": account_id, "status": "active",
                                                 "expires_at": {"$gt": now}})
        grant = await self.grants.find_one({
            "account_id": account_id, "project": project, "owner": owner, "consumer": consumer,
            "active": True, "expires_at": {"$gt": now}, "operations": operation,
            "sources": source,
        })
        if account is None and grant is not None:
            # Only a caller that already holds a valid grant learns why the account is
            # paused; everyone else keeps seeing the same access_denied.
            paused = await self.accounts.find_one({"account_id": account_id, "status": "login_required",
                                                    "expires_at": {"$gt": now}})
            if paused:
                reason = paused.get("status_reason")
                raise SocialError(reason if reason in {"login_required", "challenge_required"} else "login_required")
        if account is None or grant is None:
            raise SocialError("access_denied")
        return grant

    async def revoke(self, *, grant_id: str, actor_owner: str):
        grant = await self.grants.find_one({"grant_id": grant_id})
        if not grant or not await self.accounts.find_one({"account_id": grant["account_id"], "owner_id": actor_owner}):
            raise SocialError("access_denied")
        await self.grants.update_one({"grant_id": grant_id}, {"$set": {"active": False}})

    async def retire(self, *, account_id: str, actor_owner: str):
        """Explicit local disposal, serialized against readers and cookie setup.

        Expiry/revocation denies use immediately; this operation also removes local
        secrets and artifacts. Metadata is retained as the revocation tombstone.
        """
        async with self.setup_lease(account_id=account_id, actor_owner=actor_owner) as token:
            await self.accounts.update_one(token, {"$set": {"status": "retired"}})
            await self.grants.update_many({"account_id": account_id}, {"$set": {"active": False}})
            path = self.sessions.path(account_id, must_exist=False)
            for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
                candidate.unlink(missing_ok=True)
            await self.artifacts.delete_many({"account_id": account_id})

    @asynccontextmanager
    async def setup_lease(self, *, account_id, actor_owner):
        now, holder = self.clock(), uuid4().hex
        account = await self.accounts.find_one_and_update(
            {"account_id": account_id, "owner_id": actor_owner, "lease_until": {"$lte": now}},
            {"$set": {"lease_holder": holder, "lease_until": now + timedelta(seconds=120)},
             "$inc": {"fence": 1}}, return_document=ReturnDocument.AFTER)
        if account is None:
            raise SocialError("account_busy_or_unavailable")
        token = {"account_id": account_id, "lease_holder": holder, "fence": account["fence"]}
        try:
            yield token
        finally:
            await self.accounts.update_one(token, {"$set": {"lease_until": self.clock()}, "$unset": {"lease_holder": ""}})

    @asynccontextmanager
    async def lease(self, scope: dict[str, str], *, seconds: int = 40):
        await self.authorize(**scope)
        now, holder = self.clock(), uuid4().hex
        cooldown = await self.accounts.find_one({"account_id": scope["account_id"], "retry_after": {"$gt": now}})
        if cooldown:
            raise SocialError("rate_limited")
        account = await self.accounts.find_one_and_update(
            {"account_id": scope["account_id"], "status": "active", "expires_at": {"$gt": now},
             "lease_until": {"$lte": now}},
            {"$set": {"lease_holder": holder, "lease_until": now + timedelta(seconds=seconds)},
             "$inc": {"fence": 1}}, return_document=ReturnDocument.AFTER)
        if account is None:
            raise SocialError("account_busy")
        try:
            await self.authorize(**scope)
            yield {"account_id": account["account_id"], "lease_holder": holder, "fence": account["fence"]}
        finally:
            await self.accounts.update_one(
                {"account_id": account["account_id"], "lease_holder": holder, "fence": account["fence"]},
                {"$set": {"lease_until": self.clock()}, "$unset": {"lease_holder": ""}})

    async def _valid_lease(self, token):
        if not await self.accounts.find_one({**token, "lease_until": {"$gt": self.clock()}, "status": "active"}):
            raise SocialError("lease_lost")

    async def read(self, *, scope: dict[str, str], limit: int = 5) -> dict:
        if self.adapter is None:
            raise SocialError("missing_config")
        bounded = max(1, min(int(limit), 5))
        async with self.lease(scope) as token:
            path = self.sessions.path(scope["account_id"])
            try:
                async with asyncio.timeout(30):
                    rows = await self.adapter.read(path=path, operation=scope["operation"],
                                                   source=scope["source"], limit=bounded)
            except SocialError as exc:
                if exc.reason in {"login_required", "challenge_required"}:
                    await self.accounts.update_one(token, {"$set": {"status": "login_required", "status_reason": exc.reason}})
                elif exc.reason == "rate_limited":
                    await self.accounts.update_one(token, {"$set": {"retry_after": self.clock() + timedelta(minutes=15), "status_reason": "rate_limited"}})
                raise
            except TimeoutError:
                raise SocialError("source_timeout") from None
            except asyncio.CancelledError:
                raise
            except Exception:
                raise SocialError("source_unavailable") from None
            await self._valid_lease(token)
            grant = await self.authorize(**scope)  # revoke/expiry during I/O discards payload
            # Only adapter-selected fields. Never raw JSON, HTML, cookies or profile data.
            selected = []
            for row in rows[:bounded]:
                selected.append({k: str(row.get(k) or "")[:4000] for k in ("post_id", "url", "text", "published_at")})
            artifact_id = "sa_" + uuid4().hex
            document = {"artifact_id": artifact_id, "account_id": scope["account_id"],
                        "project": scope["project"], "owner": scope["owner"], "consumer": scope["consumer"],
                        "operation": scope["operation"], "source": scope["source"],
                        "classification": "restricted", "kind": "declaration", "trust": "untrusted",
                        "observed_at": self.clock(), "expires_at": min(grant["expires_at"].replace(tzinfo=timezone.utc), self.clock() + timedelta(days=7)),
                        "items": selected}
            await self.artifacts.insert_one(document)
            # A stale holder may have inserted an orphan, but cannot publish it.
            committed = await self.accounts.update_one(
                {**token, "lease_until": {"$gt": self.clock()}, "status": "active"},
                {"$set": {"last_artifact_id": artifact_id}})
            if not committed.matched_count:
                raise SocialError("lease_lost")
            await self.artifacts.update_one({"artifact_id": artifact_id}, {"$set": {"committed": True}})
            return await self.get_artifact(artifact_id, scope=scope)

    async def get_artifact(self, artifact_id: str, *, scope: dict[str, str]):
        await self.authorize(**scope)
        row = await self.artifacts.find_one({"artifact_id": artifact_id, "account_id": scope["account_id"],
                                           "project": scope["project"], "owner": scope["owner"],
                                           "consumer": scope["consumer"], "operation": scope["operation"],
                                           "source": scope["source"], "committed": True,
                                           "expires_at": {"$gt": self.clock()}})
        if row is None:
            raise SocialError("artifact_unavailable")
        return {k: row[k] for k in ("artifact_id", "classification", "kind", "trust", "observed_at", "items")}
