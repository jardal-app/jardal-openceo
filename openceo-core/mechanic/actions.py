"""The core side of a mechanic verdict: each action, validated and applied.

Git actions never discard work: unstage touches only the index, stash is
recoverable from ``git stash list``, and a lock is removed only when it
is stale.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from mechanic.models import ActionKind, MechanicAction
from mechanic.stall import MECHANIC_WAIT_PREFIX, OWNER_ACTION_PREFIX, STREAK_KEY

logger = logging.getLogger(__name__)

STALE_INDEX_LOCK_SECONDS = 600
FIX_TICKET_PREFIX = "[mecánico] "


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", repo, *args], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True,
                          errors="replace", timeout=60)


class ActionRunner:
    """Applies one verdict's actions through the ticket service."""

    def __init__(
        self,
        *,
        task_service: Any,
        create_ticket: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]],
        repo_busy: Callable[[str], Awaitable[bool]],
        open_fix_tickets: Callable[[], Awaitable[int]],
        wake_dispatcher: Callable[[], None],
        config: Mapping[str, Any],
        clock: Callable[[], datetime],
        git: Callable[..., subprocess.CompletedProcess[str]] = _git,
    ) -> None:
        self._tasks = task_service
        self._create_ticket = create_ticket
        self._repo_busy = repo_busy
        self._open_fix_tickets = open_fix_tickets
        self._wake_dispatcher = wake_dispatcher
        self._config = config
        self._clock = clock
        self._git = git

    async def apply(self, action: MechanicAction, *, run_id: str,
                    finding_ids: list[str], diagnosis: str) -> dict[str, Any]:
        handler = getattr(self, f"_do_{action.kind.value}")
        try:
            detail = await handler(action, run_id=run_id,
                                   finding_ids=finding_ids, diagnosis=diagnosis)
        except Exception as exc:  # noqa: BLE001 - one action never stops the rest
            logger.warning("mechanic %s: %s failed", run_id, action.kind.value,
                           exc_info=True)
            return {"kind": action.kind.value, "ok": False, "detail": str(exc)[:300]}
        return {"kind": action.kind.value, "ok": True, "detail": detail,
                "task_id": action.task_id, "repo": action.repo}

    async def _patch(self, task_id: str, updates: dict[str, Any]) -> None:
        if await self._tasks.patch_task(task_id, updates) is None:
            raise RuntimeError(f"ticket {task_id} not found")

    async def _do_resume(self, action: MechanicAction, **_: Any) -> str:
        await self._patch(str(action.task_id), {
            "status": "queued", "waiting_reason": None, "resume_at": None,
            "params": {STREAK_KEY: None}})
        self._wake_dispatcher()
        return "requeued"

    async def _do_wait(self, action: MechanicAction, **_: Any) -> str:
        resume = self._clock() + timedelta(minutes=int(action.minutes or 0))
        await self._patch(str(action.task_id), {
            "status": "waiting",
            "waiting_reason": f"{MECHANIC_WAIT_PREFIX}: {action.text}"[:300],
            "resume_at": resume, "params": {STREAK_KEY: None}})
        return f"resume at {resume.isoformat()}"

    async def _do_fail(self, action: MechanicAction, *, run_id: str,
                       **_: Any) -> str:
        await self._patch(str(action.task_id), {
            "status": "failed", "waiting_reason": None, "resume_at": None,
            "error": f"mechanic {run_id}: {action.text}"[:800],
            "completed_at": self._clock()})
        return "failed"

    async def _do_escalate(self, action: MechanicAction, *, run_id: str,
                           **_: Any) -> str:
        await self._patch(str(action.task_id), {
            "status": "waiting", "resume_at": None,
            "waiting_reason": f"{OWNER_ACTION_PREFIX}: {action.text}"[:300]})
        await self._tasks.comment_on_ticket(
            str(action.task_id),
            f"[mecánico {run_id}] Necesito al Owner: {action.text}")
        return "escalated"

    async def _do_comment(self, action: MechanicAction, *, run_id: str,
                          **_: Any) -> str:
        posted = await self._tasks.comment_on_ticket(
            str(action.task_id), f"[mecánico {run_id}] {action.text}")
        return "posted" if posted else "no business mirror"

    async def _do_fix_ticket(self, action: MechanicAction, *, run_id: str,
                             finding_ids: list[str], diagnosis: str) -> str:
        limit = int(self._config.get("max_open_fix_tickets", 2))
        if await self._open_fix_tickets() >= limit:
            raise RuntimeError(f"{limit} mechanic fix tickets already open")
        digest = hashlib.sha1(action.title.strip().lower().encode()).hexdigest()[:12]
        created = await self._create_ticket({
            "source": "spillover",
            "source_id": f"mechanic:{digest}",
            "title": (FIX_TICKET_PREFIX + action.title.strip())[:180],
            "description": (f"{action.text}\n\nDIAGNÓSTICO DEL MECÁNICO "
                            f"({run_id})\n{diagnosis}")[:12000],
            "project": str(self._config.get("fix_ticket_project")
                           or "jardal-openceo"),
            "priority": "high",
            "tags": ["mechanic", run_id],
            "coding_tool": str(self._config.get("fix_ticket_tier") or "tier_c"),
            "agent_hint": "dev",
            "autorun": bool(self._config.get("fix_ticket_autorun", True)),
            "params": {"mechanic_fix_for": list(finding_ids),
                       "mechanic_run_id": run_id},
            "acceptance": [
                "Causa raíz corregida en el motor, no solo el caso",
                "Tests dirigidos que fallan sin el fix",
                "HANDOFF con evidencia",
            ],
        })
        if created is None:
            raise RuntimeError("ticket creation failed")
        return f"ticket {created.get('task_id')}"

    async def _do_git_unstage(self, action: MechanicAction, **_: Any) -> str:
        done = self._git(str(action.repo), "reset", "-q", "--", *action.paths)
        if done.returncode != 0:
            raise RuntimeError(done.stdout[:300])
        return f"unstaged {len(action.paths)} path(s)"

    async def _do_git_stash(self, action: MechanicAction, *, run_id: str,
                            **_: Any) -> str:
        repo = str(action.repo)
        if await self._repo_busy(repo):
            raise RuntimeError("a ticket is running on this checkout")
        done = self._git(repo, "stash", "push", "-m",
                         f"openceo-mechanic {run_id}: {action.text[:80]}")
        if done.returncode != 0:
            raise RuntimeError(done.stdout[:300])
        return done.stdout.strip()[:300] or "stashed"

    async def _do_remove_index_lock(self, action: MechanicAction,
                                    **_: Any) -> str:
        lock = Path(str(action.repo)) / ".git" / "index.lock"
        if not lock.exists():
            return "no lock"
        age = time.time() - lock.stat().st_mtime
        if age < STALE_INDEX_LOCK_SECONDS:
            raise RuntimeError(f"lock is only {int(age)}s old")
        lock.unlink()
        return f"removed lock ({int(age)}s old)"


__all__ = ["ActionRunner", "ActionKind", "FIX_TICKET_PREFIX"]
