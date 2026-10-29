"""DeadlineWatch: hung attempts and unresumed waits (design v2 section 7).

Epic TASK-2026-00365, ticket TASK-2026-00383 (F2). Detects absence of
signal: attempts that never finish and waits nobody resumes.

The loops of A/B/C, Tier S and the generic agent write
``attempt_deadline_at = start + effective timeout + max(5 min, 20 %)``
when the attempt starts and clear it when the attempt ends
(``mark_attempt_start`` / ``clear_attempt_deadline``, fail-open).
``DeadlineWatch`` arms a single asyncio timer to the nearest deadline;
the deadline comes from an indexed query at boot, on every new-deadline
registration and after each firing. On firing it rereads by id only the
affected tasks and records ``deadline_missed``:

- active attempt with the same attempt_id past its deadline: critical;
  it only warns and recommends an action, never kills processes;
- waiting task not requeued 10 min after resume_at, unless it was
  requeued and not admitted (capacity, quota, dependencies).

No polling: only incidents, exposures, the next deadline and boot
reconciliation drive work. Watching automatico.py itself is out of scope.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from state import TaskStatus

from .capture import build_incident_id, record_incident
from .config import RELIABILITY_DEFAULTS
from .models import Component, IncidentKind

logger = logging.getLogger(__name__)

ATTEMPT_ID_FIELD = "attempt_id"
ATTEMPT_DEADLINE_FIELD = "attempt_deadline_at"
RESUME_FIELD = "resume_at"

DEADLINE_TYPE_ATTEMPT = "attempt"
DEADLINE_TYPE_RESUME = "resume"

#: §8.5 budget window hook for the diagnosis child (TASK-2026-00381).
_BUDGET_HOOK: Callable[[datetime, datetime], Any | None] | None = None


def set_budget_deadline_hook(
    hook: Callable[[datetime, datetime], Any | None] | None,
) -> None:
    """Register the diagnosis budget-window hook; None disables."""
    global _BUDGET_HOOK
    _BUDGET_HOOK = hook


def budget_window_for(
    deadline: datetime, *, now: datetime | None = None,
) -> Any | None:
    """Budget window around a deadline for diagnosis (§8.5).

    Returns the registered hook result, None when no hook is set.
    Never raises.
    """
    try:
        if _BUDGET_HOOK is None:
            return None
        moment = _as_utc(now) if now is not None else _utcnow()
        return _BUDGET_HOOK(_as_utc(deadline), moment)
    except Exception:
        logger.warning("deadline budget hook failed (fail-open)", exc_info=True)
        return None


def deadlines_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Effective deadlines block: defaults plus an optional override."""
    base = dict(RELIABILITY_DEFAULTS["deadlines"])
    if isinstance(override, dict):
        for key, value in override.items():
            if key in base:
                base[key] = value
    return base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: Any) -> datetime:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment
    return _utcnow()


def attempt_deadline_for(
    started_at: datetime | None,
    timeout_seconds: int | float,
    cfg: dict[str, Any] | None = None,
) -> datetime:
    """Attempt deadline: start + timeout + max(grace_min, ratio * timeout)."""
    block = deadlines_config(cfg)
    try:
        timeout = max(0.0, float(timeout_seconds))
    except (TypeError, ValueError):
        timeout = 0.0
    try:
        grace_min = max(0.0, float(block.get("attempt_grace_min_seconds", 300)))
    except (TypeError, ValueError):
        grace_min = 300.0
    try:
        ratio = float(block.get("attempt_grace_ratio", 0.2))
    except (TypeError, ValueError):
        ratio = 0.2
    if not 0.0 <= ratio <= 1.0:
        ratio = 0.2
    start = _as_utc(started_at) if started_at is not None else _utcnow()
    return start + timedelta(seconds=timeout + max(grace_min, ratio * timeout))


def resume_deadline_for(
    resume_at: datetime, cfg: dict[str, Any] | None = None,
) -> datetime:
    """Resume deadline: resume_at + the overdue grace (§7: 10 min)."""
    block = deadlines_config(cfg)
    try:
        overdue = max(0.0, float(block.get("resume_overdue_seconds", 600)))
    except (TypeError, ValueError):
        overdue = 600.0
    return _as_utc(resume_at) + timedelta(seconds=overdue)


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def deadline_candidates_filter() -> dict[str, Any]:
    """Mongo filter for tasks holding a deadline (§13.5).

    Each $or branch matches only real datetimes (``$type: date``), so
    cleared attempts (``attempt_deadline_at: None``) never match. The
    attempt branch is served by ``idx_deadline_attempt`` and the resume
    branch by the pre-existing ``(status, resume_at)`` index; no sort is
    requested because ``DeadlineWatch.pending`` orders in memory.
    """
    return {
        "$or": [
            {
                "status": TaskStatus.IN_PROGRESS.value,
                ATTEMPT_DEADLINE_FIELD: {"$type": "date"},
            },
            {
                "status": TaskStatus.WAITING.value,
                RESUME_FIELD: {"$type": "date"},
            },
        ],
    }


async def mark_attempt_start(
    patch_task: Callable[..., Any] | None,
    *,
    task_id: str,
    attempt_id: str,
    timeout_seconds: int | float,
    started_at: datetime | None = None,
    cfg: dict[str, Any] | None = None,
    notify: Callable[[], Any] | None = None,
) -> datetime | None:
    """Write attempt_id + attempt_deadline_at. Fail-open, never raises.

    ``notify`` runs after a successful write so the owner of the watch
    re-arms its timer to the new deadline (registration, §7); it is
    sync-or-async and fail-open like everything else here.
    """
    try:
        if patch_task is None or not callable(patch_task):
            return None
        clean_id = str(task_id or "").strip()
        clean_attempt = str(attempt_id or "").strip()
        if not clean_id or not clean_attempt:
            return None
        deadline = attempt_deadline_for(started_at, timeout_seconds, cfg)
        await _maybe_await(patch_task(clean_id, {
            ATTEMPT_ID_FIELD: clean_attempt,
            ATTEMPT_DEADLINE_FIELD: deadline,
        }))
        if notify is not None:
            try:
                await _maybe_await(notify())
            except Exception:
                logger.warning("deadline notify failed (fail-open)",
                               exc_info=True)
        return deadline
    except Exception:
        logger.warning("deadline mark start failed (fail-open)", exc_info=True)
        return None


async def clear_attempt_deadline(
    patch_task: Callable[..., Any] | None, task_id: str,
) -> None:
    """Clear attempt_deadline_at when the attempt ends. Never raises."""
    try:
        if patch_task is None or not callable(patch_task):
            return
        clean_id = str(task_id or "").strip()
        if not clean_id:
            return
        await _maybe_await(patch_task(clean_id, {ATTEMPT_DEADLINE_FIELD: None}))
    except Exception:
        logger.warning("deadline clear failed (fail-open)", exc_info=True)


def deadline_index_specs() -> list[dict[str, Any]]:
    """Index specs of §13.5 for the watch queries.

    The resume side reuses the pre-existing ``(status, resume_at)``
    index, so only the attempt index carries a name to create.
    """
    return [
        {"name": "idx_deadline_attempt",
         "keys": [("status", 1), (ATTEMPT_DEADLINE_FIELD, 1)]},
        {"name": None,
         "keys": [("status", 1), (RESUME_FIELD, 1)]},
    ]


async def ensure_deadline_indexes(tasks_collection: Any) -> int:
    """Create the §13.5 indexes on a tasks collection. Never raises."""
    if tasks_collection is None:
        return 0
    created = 0
    for spec in deadline_index_specs():
        try:
            if spec.get("name"):
                result = tasks_collection.create_index(
                    spec["keys"], name=spec["name"])
            else:
                result = tasks_collection.create_index(spec["keys"])
            await _maybe_await(result)
            created += 1
        except Exception:
            logger.warning("deadline index %s failed (fail-open)",
                           spec.get("name") or spec["keys"], exc_info=True)
    return created


class DeadlineWatch:
    """Single-timer watch for hung attempts and unresumed waits.

    ``repo`` serves ``list_tasks_by_status(status, limit)`` and
    ``get_task(task_id)``; when it offers ``deadline_candidates(now,
    limit)`` that indexed query is used instead of the fallback scan.
    ``record`` persists incidents (defaults to ``capture.record_incident``
    over ``incidents``); ``clock`` is injected for tests.
    """

    SCAN_LIMIT = 500

    def __init__(
        self,
        repo: Any,
        *,
        incidents: Any = None,
        clock: Callable[[], datetime] | None = None,
        config: dict[str, Any] | None = None,
        record: Callable[..., Any] | None = None,
    ) -> None:
        self._repo = repo
        self._incidents = incidents
        self._clock = clock or _utcnow
        self._config = deadlines_config(config)
        self._record = record
        self._timer: asyncio.Task[Any] | None = None
        self._armed: list[dict[str, Any]] = []

    def _now(self) -> datetime:
        try:
            return _as_utc(self._clock())
        except Exception:
            logger.warning("deadline clock failed, using system time",
                           exc_info=True)
            return _utcnow()

    async def _candidates(self, now: datetime) -> list[dict[str, Any]]:
        repo = self._repo
        if repo is None:
            return []
        try:
            query = getattr(repo, "deadline_candidates", None)
            if callable(query):
                docs = await _maybe_await(query(now, self.SCAN_LIMIT))
                return [doc for doc in docs or [] if isinstance(doc, dict)]
            in_progress = await _maybe_await(repo.list_tasks_by_status(
                TaskStatus.IN_PROGRESS, limit=self.SCAN_LIMIT))
            waiting = await _maybe_await(repo.list_tasks_by_status(
                TaskStatus.WAITING, limit=self.SCAN_LIMIT))
            docs = [doc for doc in list(in_progress or [])
                    + list(waiting or []) if isinstance(doc, dict)]
            return docs[:self.SCAN_LIMIT]
        except Exception:
            logger.warning("deadline scan failed (fail-open)", exc_info=True)
            return []

    @staticmethod
    def _deadline_of(doc: dict[str, Any],
                     cfg: dict[str, Any]) -> tuple[str, datetime] | None:
        status = str(doc.get("status") or "")
        if status == TaskStatus.IN_PROGRESS.value:
            raw = doc.get(ATTEMPT_DEADLINE_FIELD)
            if not isinstance(raw, datetime):
                return None
            if not str(doc.get(ATTEMPT_ID_FIELD) or "").strip():
                return None
            moment = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
            return DEADLINE_TYPE_ATTEMPT, moment
        if status == TaskStatus.WAITING.value:
            raw = doc.get(RESUME_FIELD)
            if not isinstance(raw, datetime):
                return None
            return DEADLINE_TYPE_RESUME, resume_deadline_for(raw, cfg)
        return None

    async def pending(
        self, now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Tasks holding a deadline, sorted by it (attempt or resume)."""
        moment = _as_utc(now) if now is not None else self._now()
        out: list[dict[str, Any]] = []
        for doc in await self._candidates(moment):
            found = self._deadline_of(doc, self._config)
            if found is None:
                continue
            kind, deadline = found
            out.append({
                "task_id": str(doc.get("task_id") or ""),
                "attempt_id": str(doc.get(ATTEMPT_ID_FIELD) or ""),
                "deadline_type": kind,
                "deadline": deadline,
            })
        out.sort(key=lambda item: item["deadline"])
        return [item for item in out if item["task_id"]]

    async def next_deadline(
        self, now: datetime | None = None,
    ) -> datetime | None:
        """Nearest pending deadline, or None when nothing is pending."""
        items = await self.pending(now)
        if not items:
            return None
        return items[0]["deadline"]

    async def check_due(
        self, now: datetime | None = None,
    ) -> list[str]:
        """Reread by id every task at/past its deadline and record misses."""
        moment = _as_utc(now) if now is not None else self._now()
        recorded: list[str] = []
        for item in await self.pending(moment):
            if item["deadline"] > moment:
                break
            try:
                fresh = await _maybe_await(
                    self._repo.get_task(item["task_id"]))
            except Exception:
                logger.warning("deadline reread failed for %s (fail-open)",
                               item["task_id"], exc_info=True)
                continue
            if not isinstance(fresh, dict):
                continue
            incident_id = await self._evaluate(fresh, item, moment)
            if incident_id is not None:
                recorded.append(incident_id)
        return recorded

    async def _evaluate(
        self, fresh: dict[str, Any], item: dict[str, Any], now: datetime,
    ) -> str | None:
        if str(fresh.get("status") or "") == TaskStatus.IN_PROGRESS.value:
            return await self._evaluate_attempt(fresh, item, now)
        if str(fresh.get("status") or "") == TaskStatus.WAITING.value:
            return await self._evaluate_resume(fresh, now)
        return None

    def _armed_attempt(self, task_id: str) -> str:
        for entry in self._armed:
            if entry.get("task_id") == task_id:
                return str(entry.get("attempt_id") or "")
        return ""

    async def _evaluate_attempt(
        self, fresh: dict[str, Any], item: dict[str, Any], now: datetime,
    ) -> str | None:
        attempt_id = str(fresh.get(ATTEMPT_ID_FIELD) or "").strip()
        if not attempt_id:
            return None
        armed = self._armed_attempt(str(fresh.get("task_id") or ""))
        if armed and armed != attempt_id:
            return None
        raw = fresh.get(ATTEMPT_DEADLINE_FIELD)
        if not isinstance(raw, datetime):
            return None
        deadline = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if deadline > now:
            return None
        task_id = str(fresh.get("task_id") or "")
        incident_id = build_incident_id(
            IncidentKind.DEADLINE_MISSED, task_id=task_id,
            attempt_id=attempt_id, discriminator=deadline.isoformat())
        message = (
            f"attempt {attempt_id} of task {task_id} missed its deadline "
            f"{deadline.isoformat()}; the attempt may be hung. "
            "DeadlineWatch only warns: inspect or cancel it manually, "
            "it never kills processes. Recommended: check the run, "
            "then retry or park the ticket.")
        await self._store(
            task_id=task_id, attempt_id=attempt_id,
            deadline_type=DEADLINE_TYPE_ATTEMPT, severity="critical",
            message=message, incident_id=incident_id, now=now)
        return incident_id

    async def _evaluate_resume(
        self, fresh: dict[str, Any], now: datetime,
    ) -> str | None:
        raw = fresh.get(RESUME_FIELD)
        if not isinstance(raw, datetime):
            return None
        if resume_deadline_for(raw, self._config) > now:
            return None
        task_id = str(fresh.get("task_id") or "")
        resume_at = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        incident_id = build_incident_id(
            IncidentKind.DEADLINE_MISSED, task_id=task_id,
            attempt_id=f"resume-{resume_at.isoformat()}",
            discriminator=resume_at.isoformat())
        message = (
            f"task {task_id} is still waiting {self._config.get('resume_overdue_seconds', 600)}s "
            f"after resume_at {resume_at.isoformat()} without requeue; "
            "the wake may be lost. Recommended: check the dispatch cycle "
            "and requeue or park the ticket.")
        await self._store(
            task_id=task_id, attempt_id=None,
            deadline_type=DEADLINE_TYPE_RESUME, severity=None,
            message=message, incident_id=incident_id, now=now)
        return incident_id

    async def _store(self, *, task_id: str, attempt_id: str | None,
                     deadline_type: str, severity: str | None,
                     message: str, incident_id: str, now: datetime) -> None:
        try:
            if self._record is not None:
                await _maybe_await(self._record(
                    kind="deadline_missed", component="dispatcher",
                    task_id=task_id, attempt_id=attempt_id,
                    deadline_type=deadline_type, severity=severity,
                    message=message, incident_id=incident_id, now=now))
                return
            await record_incident(
                self._incidents, kind=IncidentKind.DEADLINE_MISSED,
                component=Component.DISPATCHER, task_id=task_id,
                attempt_id=attempt_id, deadline_type=deadline_type,
                severity=severity, message=message,
                incident_id=incident_id, now=now)
        except Exception:
            logger.warning("deadline record failed (fail-open)", exc_info=True)

    @property
    def has_timer(self) -> bool:
        """True while the single asyncio timer is armed."""
        task = self._timer
        return task is not None and not task.done()

    async def arm(self, now: datetime | None = None) -> datetime | None:
        """Arm the single timer to the nearest future deadline.

        Already-due deadlines are reported but never armed: their check
        just ran (or runs on the next registration), and arming one
        would spin the timer without advancing time. Overdue entries no
        longer block later deadlines: the timer goes to the first
        strictly-future deadline, or stays disarmed when none exists.
        """
        moment = _as_utc(now) if now is not None else self._now()
        self._armed = await self.pending(moment)
        self._cancel_timer()
        future = next(
            (item for item in self._armed if item["deadline"] > moment),
            None)
        if future is None:
            return self._armed[0]["deadline"] if self._armed else None
        delay = max(0.0, (future["deadline"] - moment).total_seconds())
        self._timer = asyncio.create_task(self._wait_and_fire(delay))
        return future["deadline"]

    def _cancel_timer(self) -> None:
        task = self._timer
        self._timer = None
        if task is not None and not task.done():
            task.cancel()

    async def _wait_and_fire(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.check_due(self._now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("deadline fire failed (fail-open)", exc_info=True)
        finally:
            try:
                await self.arm(self._now())
            except Exception:
                logger.warning("deadline re-arm failed (fail-open)",
                               exc_info=True)

    async def refresh(self, now: datetime | None = None) -> list[str]:
        """Check due deadlines, then re-arm to the nearest one."""
        moment = _as_utc(now) if now is not None else self._now()
        try:
            recorded = await self.check_due(moment)
        except Exception:
            logger.warning("deadline refresh check failed (fail-open)",
                           exc_info=True)
            recorded = []
        try:
            await self.arm(moment)
        except Exception:
            logger.warning("deadline refresh arm failed (fail-open)",
                           exc_info=True)
        return recorded

    async def close(self) -> None:
        """Disarm the timer. Never raises."""
        try:
            self._cancel_timer()
        except Exception:
            logger.warning("deadline close failed (fail-open)", exc_info=True)
        self._armed = []
