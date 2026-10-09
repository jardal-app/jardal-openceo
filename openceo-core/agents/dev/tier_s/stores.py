"""Persistence seams for the Tier S engine (design §15).

The cycle talks to narrow protocols. Production wires them to
:class:`db.mongo.MongoRepository` (same ``tier_s_runs`` / ``repo_locks`` /
``tasks`` collections the old engine uses, so the orphan-cleanup guard that
spares live Tier S runs keeps working — incident
``2026-10-04-tier-s-orphan-killer``). Tests use the in-memory stores.
"""

from __future__ import annotations

from typing import Any, Protocol

#: Run statuses that still claim a future (legacy engine: active/resuming).
#: A new run of the same task supersedes them (TASK-2026-00162).
OPEN_RUN_STATUSES = frozenset({"waiting", "running", "active", "resuming"})


class RunStore(Protocol):
    async def create_run(self, doc: dict[str, Any]) -> dict[str, Any]: ...
    async def patch_run(
        self, run_id: str, updates: dict[str, Any]) -> None: ...
    # Close the task's other open runs (TASK-2026-00162).
    async def supersede_runs(
        self, task_id: str, keep_run_id: str) -> int: ...


class LockStore(Protocol):
    async def acquire(self, repo: str, holder: str) -> bool: ...
    async def release(self, repo: str, holder: str) -> bool: ...


class TaskStore(Protocol):
    async def patch_task(
        self, task_id: str, updates: dict[str, Any]) -> None: ...
    async def task_status(self, task_id: str) -> str | None: ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}

    async def create_run(self, doc: dict[str, Any]) -> dict[str, Any]:
        stored = dict(doc)
        self.runs[stored["run_id"]] = stored
        return dict(stored)

    async def patch_run(self, run_id: str, updates: dict[str, Any]) -> None:
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        run.update(updates)

    async def supersede_runs(self, task_id: str, keep_run_id: str) -> int:
        closed = 0
        for run in self.runs.values():
            if (run.get("task_id") == task_id
                    and run.get("run_id") != keep_run_id
                    and run.get("status") in OPEN_RUN_STATUSES):
                run.update({"status": "superseded",
                            "superseded_reason": f"run {keep_run_id}",
                            "superseded_by": keep_run_id,
                            "active_repo": None})
                closed += 1
        return closed


class InMemoryLockStore:
    def __init__(self) -> None:
        self.holders: dict[str, str] = {}

    async def acquire(self, repo: str, holder: str) -> bool:
        if repo in self.holders:
            return False
        self.holders[repo] = holder
        return True

    async def release(self, repo: str, holder: str) -> bool:
        if self.holders.get(repo) != holder:
            return False
        del self.holders[repo]
        return True


class InMemoryTaskStore:
    def __init__(self, statuses: dict[str, str] | None = None) -> None:
        self.patches: dict[str, dict[str, Any]] = {}
        self.statuses: dict[str, str] = dict(statuses or {})

    async def patch_task(self, task_id: str, updates: dict[str, Any]) -> None:
        self.patches.setdefault(task_id, {}).update(updates)
        if "status" in updates:
            self.statuses[task_id] = str(updates["status"])

    async def task_status(self, task_id: str) -> str | None:
        return self.statuses.get(task_id)


class MongoRunStore:
    """Production runs on ``tier_s_runs`` (same collection)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def create_run(self, doc: dict[str, Any]) -> dict[str, Any]:
        return await self._repo.create_tier_s_run(doc)

    async def patch_run(self, run_id: str, updates: dict[str, Any]) -> None:
        await self._repo.patch_tier_s_run(run_id, updates)

    async def supersede_runs(self, task_id: str, keep_run_id: str) -> int:
        return await self._repo.supersede_tier_s_runs(task_id, keep_run_id)


class MongoLockStore:
    """Production repo lock (design §14.1: one lock per repo, all tiers)."""

    def __init__(self, repo: Any, ttl_seconds: int = 86400) -> None:
        self._repo = repo
        self._ttl = ttl_seconds

    async def acquire(self, repo: str, holder: str) -> bool:
        return await self._repo.acquire_repo_lock(
            repo, holder, self._ttl) is not None

    async def release(self, repo: str, holder: str) -> bool:
        return bool(await self._repo.release_repo_lock(repo, holder))


class MongoTaskStore:
    """Production task patches (status/waiting propagate via the observer)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def patch_task(self, task_id: str, updates: dict[str, Any]) -> None:
        await self._repo.patch_task(task_id, updates)

    async def task_status(self, task_id: str) -> str | None:
        doc = await self._repo.get_task(task_id)
        if not doc:
            return None
        return str(doc.get("status"))
