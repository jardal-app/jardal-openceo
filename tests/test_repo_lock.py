"""Common per-repo execution lock (Tier S design §11.1, F0).

Real Mongo in an isolated database, following the research test pattern: a
fake lock would prove nothing about atomicity. Skipped when Mongo is down.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.consensus import _RepoBusy, CriticalConsensusOrchestrator  # noqa: E402
from db.mongo import MongoRepository  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402
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


@unittest.skipUnless(_mongo_available(), "MongoDB not available")
class OpenRunLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        settings = Settings(_env_file=None, MONGODB_URL=MONGO_URL, MONGODB_DATABASE=TEST_DB)
        self.repo = MongoRepository(settings)
        await self.repo.connect()
        await self.repo.repo_locks.delete_many({})
        await self.repo.tier_s_runs.delete_many({})
        self.temp = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo_path, check=True)
        self.orch = CriticalConsensusOrchestrator(MagicMock(), MagicMock(), repo=self.repo)

    async def asyncTearDown(self) -> None:
        await self.repo.repo_locks.delete_many({})
        await self.repo.tier_s_runs.delete_many({})
        await self.repo.close()
        self.temp.cleanup()

    async def test_reopen_same_owner_refreshes_lock(self) -> None:
        first = await self.orch._open_run("proj", self.repo_path, {}, task_id="task-a",
                                          session_id=None)
        second = await self.orch._open_run("proj", self.repo_path, {}, task_id="task-a",
                                           session_id=None)
        self.assertEqual(first["run_id"], second["run_id"])
        holder = await self.repo.repo_lock_holder(str(self.repo_path))
        assert holder is not None
        self.assertEqual(holder["holder"], "task-a")

    async def test_other_owner_blocked_by_lock_without_tier_s_run(self) -> None:
        # A simple tier holds the repo (lock only, no Tier S run).
        await self.repo.acquire_repo_lock(str(self.repo_path), "task-simple")
        with self.assertRaises(_RepoBusy):
            await self.orch._open_run("proj", self.repo_path, {}, task_id="task-b",
                                      session_id=None)

    async def test_lost_create_race_releases_orphan_lock(self) -> None:
        async def flaky_create(doc):
            raise DuplicateKeyError("simulated race")

        self.repo.create_tier_s_run = flaky_create  # type: ignore[method-assign]
        with self.assertRaises(_RepoBusy):
            await self.orch._open_run("proj", self.repo_path, {}, task_id="task-b",
                                      session_id=None)
        self.assertIsNone(await self.repo.repo_lock_holder(str(self.repo_path)))


if __name__ == "__main__":
    unittest.main()
