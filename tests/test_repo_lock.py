"""Common per-repo execution lock (Tier S design §11.1, F0).

Real Mongo in an isolated database, following the research test pattern: a
fake lock would prove nothing about atomicity. Skipped when Mongo is down.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from db.mongo import MongoRepository  # noqa: E402
from settings import Settings  # noqa: E402

MONGO_URL = "mongodb://localhost:27017"
TEST_DB = "openceo_test_repo_lock"


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=400)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_mongo_available(), "MongoDB not available")
class RepoLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = Settings(_env_file=None, MONGODB_URL=MONGO_URL, MONGODB_DATABASE=TEST_DB)
        self.repo = MongoRepository(settings)
        await self.repo.connect()
        await self.repo.repo_locks.delete_many({})

    async def asyncTearDown(self) -> None:
        await self.repo.repo_locks.delete_many({})
        await self.repo.close()

    def _repo_name(self) -> str:
        return f"/tmp/repo-lock-test-{uuid.uuid4().hex}"

    async def test_acquire_blocks_second_holder(self) -> None:
        repo = self._repo_name()
        first = await self.repo.acquire_repo_lock(repo, "task-a")
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first["holder"], "task-a")
        self.assertIsNone(await self.repo.acquire_repo_lock(repo, "task-b"))
        current = await self.repo.repo_lock_holder(repo)
        assert current is not None
        self.assertEqual(current["holder"], "task-a")

    async def test_same_holder_reacquires(self) -> None:
        repo = self._repo_name()
        await self.repo.acquire_repo_lock(repo, "task-a")
        again = await self.repo.acquire_repo_lock(repo, "task-a")
        self.assertIsNotNone(again)

    async def test_expired_lock_is_taken_over(self) -> None:
        repo = self._repo_name()
        await self.repo.acquire_repo_lock(repo, "task-a", ttl_seconds=-3600)
        taken = await self.repo.acquire_repo_lock(repo, "task-b")
        self.assertIsNotNone(taken)
        assert taken is not None
        self.assertEqual(taken["holder"], "task-b")

    async def test_release_only_by_owner(self) -> None:
        repo = self._repo_name()
        await self.repo.acquire_repo_lock(repo, "task-a")
        self.assertFalse(await self.repo.release_repo_lock(repo, "task-b"))
        self.assertIsNone(await self.repo.acquire_repo_lock(repo, "task-b"))
        self.assertTrue(await self.repo.release_repo_lock(repo, "task-a"))
        self.assertIsNotNone(await self.repo.acquire_repo_lock(repo, "task-b"))

    async def test_touch_extends_lease(self) -> None:
        repo = self._repo_name()
        await self.repo.acquire_repo_lock(repo, "task-a", ttl_seconds=-5)
        self.assertTrue(await self.repo.touch_repo_lock(repo, "task-a"))
        # Touched back into the future: the other holder is blocked again.
        self.assertIsNone(await self.repo.acquire_repo_lock(repo, "task-b"))
        self.assertFalse(await self.repo.touch_repo_lock(repo, "nobody"))

    async def test_ttl_index_exists(self) -> None:
        indexes = await self.repo.repo_locks.index_information()
        ttl = [spec for spec in indexes.values()
               if spec.get("key") == [("expires_at", 1)]
               and spec.get("expireAfterSeconds") == 0]
        self.assertEqual(len(ttl), 1)

if __name__ == "__main__":
    unittest.main()
