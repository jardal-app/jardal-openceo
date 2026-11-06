"""Boot frees repo locks orphaned by a restart (#582).

A Tier S run killed by a restart kept its ``repo_locks`` row (TTL 24h):
``mark_interrupted_tasks`` closed the run and freed ``active_repo`` but
never the lock, so every retry parked on ``transient: repo_busy`` until
the ticket stalled (live: #558). The boot pass now deletes exactly the
restart orphans — ``tier-s:<run_id>`` of runs carrying the restart
failure marker and ``<task_id>`` of newly interrupted tickets — and
never touches a waiting ticket's deliberately retained lock.

Fake-repo doubles only: the real ``MongoRepository`` methods run against
in-memory collections, so no Mongo is needed. Zero tokens.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from db.mongo import MongoRepository  # noqa: E402
from state import TaskStatus  # noqa: E402


def _matches(doc: dict[str, Any], filt: dict[str, Any]) -> bool:
    for key, want in filt.items():
        if key == "$or":
            if not any(_matches(doc, dict(sub)) for sub in want):
                return False
            continue
        have = doc.get(key)
        if isinstance(want, dict):
            for op, arg in want.items():
                if op == "$in":
                    if have not in list(arg):
                        return False
                elif op == "$lte":
                    if have is None or not have <= arg:
                        return False
                else:  # pragma: no cover - only $in/$lte are used here
                    raise AssertionError(f"unsupported op {op}")
        elif have != want:
            return False
    return True


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = [dict(row) for row in rows]
        self._limit: int | None = None

    def limit(self, n: int) -> "_FakeCursor":
        self._limit = max(0, int(n))
        return self

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def _gen():  # type: ignore[no-untyped-def]
            rows = self._rows
            if self._limit is not None:
                rows = rows[: self._limit]
            for row in rows:
                yield dict(row)

        return _gen()


class _FakeCollection:
    """Minimal async motor-like collection keyed by insertion order."""

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        stored = dict(doc)
        self.docs.append(stored)
        return SimpleNamespace(inserted_id=stored.get("_id"))

    def find(self, filt: dict[str, Any] | None = None,
             *args: Any, **kwargs: Any) -> _FakeCursor:
        del args, kwargs
        filt = filt or {}
        return _FakeCursor(
            [doc for doc in self.docs if _matches(doc, filt)])

    async def find_one(self, filt: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self.docs:
            if _matches(doc, filt):
                return dict(doc)
        return None

    async def update_many(self, filt: dict[str, Any],
                          update: dict[str, Any]) -> SimpleNamespace:
        changed = 0
        for doc in self.docs:
            if _matches(doc, filt):
                doc.update(update.get("$set", {}))
                changed += 1
        return SimpleNamespace(modified_count=changed)

    async def delete_many(self, filt: dict[str, Any]) -> SimpleNamespace:
        kept = [doc for doc in self.docs if not _matches(doc, filt)]
        removed = len(self.docs) - len(kept)
        self.docs = kept
        return SimpleNamespace(deleted_count=removed)

    async def find_one_and_update(self, filt: dict[str, Any],
                                  update: dict[str, Any],
                                  **kwargs: Any) -> dict[str, Any] | None:
        del kwargs
        repo_id = filt.get("_id")
        for doc in self.docs:
            if _matches(doc, filt):
                doc.update(update.get("$set", {}))
                return dict(doc)
        if repo_id is not None and not any(
                doc.get("_id") == repo_id for doc in self.docs):
            created = {"_id": repo_id}
            created.update(update.get("$set", {}))
            self.docs.append(created)
            return dict(created)
        # Another live holder owns the row: the real collection raises
        # DuplicateKeyError on the losing upsert and the caller maps it
        # to None; the fake returns None directly.
        return None


class _FakeMongo(MongoRepository):
    """Real repository methods over in-memory collections."""

    def __init__(self) -> None:
        self._tasks = _FakeCollection()
        self._runs = _FakeCollection()
        self._locks = _FakeCollection()

    @property
    def tasks(self) -> _FakeCollection:  # type: ignore[override]
        return self._tasks

    @property
    def tier_s_runs(self) -> _FakeCollection:  # type: ignore[override]
        return self._runs

    @property
    def repo_locks(self) -> _FakeCollection:  # type: ignore[override]
        return self._locks


class BootOrphanLocks582Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeMongo()

    async def _seed_lock(self, repo: str, holder: str,
                         ttl_seconds: int = 86400) -> None:
        now = datetime.now(timezone.utc)
        await self.repo.repo_locks.insert_one({
            "_id": repo, "holder": holder, "acquired_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds)})

    async def test_killed_tier_s_run_lock_is_acquirable_after_boot(self) -> None:
        await self.repo.tasks.insert_one({
            "task_id": "t-tier", "status": TaskStatus.IN_PROGRESS.value,
            "coding_tool": "tier_s"})
        await self.repo.tier_s_runs.insert_one({
            "run_id": "r-dead", "task_id": "t-tier", "status": "running",
            "active_repo": "/repo/erp"})
        await self._seed_lock("/repo/erp", "tier-s:r-dead")

        # Sanity: before the boot pass the orphan blocks a fresh holder.
        self.assertIsNone(
            await self.repo.acquire_repo_lock("/repo/erp", "tier-s:r-new"))

        marked = await self.repo.mark_interrupted_tasks()

        self.assertEqual(marked, 1)
        run = await self.repo.tier_s_runs.find_one({"run_id": "r-dead"})
        assert run is not None
        self.assertEqual(run["status"], "failed")
        task = await self.repo.tasks.find_one({"task_id": "t-tier"})
        assert task is not None
        self.assertEqual(task["status"], TaskStatus.WAITING.value)
        taken = await self.repo.acquire_repo_lock("/repo/erp", "tier-s:r-new")
        self.assertIsNotNone(taken)
        assert taken is not None
        self.assertEqual(taken["holder"], "tier-s:r-new")
        # Idempotent: a second pass frees nothing new.
        self.assertEqual(await self.repo.release_restart_orphan_repo_locks(), 0)

    async def test_interrupted_simple_tier_lock_is_freed(self) -> None:
        await self.repo.tasks.insert_one({
            "task_id": "t-simple", "status": TaskStatus.IN_PROGRESS.value,
            "coding_tool": "tier_c"})
        await self._seed_lock("/repo/app", "t-simple")

        await self.repo.mark_interrupted_tasks()

        task = await self.repo.tasks.find_one({"task_id": "t-simple"})
        assert task is not None
        self.assertEqual(task["status"], TaskStatus.INTERRUPTED.value)
        taken = await self.repo.acquire_repo_lock("/repo/app", "t-next")
        self.assertIsNotNone(taken)

    async def test_waiting_ticket_retained_lock_survives_boot(self) -> None:
        await self.repo.tasks.insert_one({
            "task_id": "t-wait", "status": TaskStatus.WAITING.value,
            "coding_tool": "tier_c",
            "waiting_reason": "transient: repo_busy: /repo/held",
            "resume_at": datetime.now(timezone.utc) + timedelta(minutes=2)})
        await self._seed_lock("/repo/held", "t-wait")

        await self.repo.mark_interrupted_tasks()

        task = await self.repo.tasks.find_one({"task_id": "t-wait"})
        assert task is not None
        self.assertEqual(task["status"], TaskStatus.WAITING.value)
        self.assertIsNone(
            await self.repo.acquire_repo_lock("/repo/held", "t-other"))
        current = await self.repo.repo_lock_holder("/repo/held")
        assert current is not None
        self.assertEqual(current["holder"], "t-wait")

    async def test_same_holder_reacquire_refreshes_lease(self) -> None:
        await self._seed_lock("/repo/lease", "t-own", ttl_seconds=-3600)

        again = await self.repo.acquire_repo_lock("/repo/lease", "t-own")

        self.assertIsNotNone(again)
        # Refreshed back into the future: a rival is blocked again.
        self.assertIsNone(
            await self.repo.acquire_repo_lock("/repo/lease", "t-rival"))


if __name__ == "__main__":
    unittest.main()
