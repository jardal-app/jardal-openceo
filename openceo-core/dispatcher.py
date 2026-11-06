"""Deterministic ticket dispatcher: events + one conditional backup trigger.

TASK-2026-00113 (F3). Replaces the legacy ``WaitingTaskScheduler`` poll loop
(``resume_waiting_tasks(); resume_queued_tickets(); sleep(60)``) with the
hybrid the owner decided: wake on CRUD events, and while ready tickets wait
for a slot keep a SINGLE persisted backup trigger; with an empty queue there
is no timer at all.

Admission is deterministic and spends zero tokens: active limits (global
weighted slots, per project, per objective), Claude/Codex quota with Tier S
reserves (``RateMonitor.admission_for_ticket``), a free repo (peek only --
enforcement stays at build time in ``agents.dev.session``, which owns the
atomic acquire), and an atomic claim against double dispatch
(``MongoRepository.claim_queued_ticket``).

Routing is enforced before admission: objective children without the planner
tier park fail-closed as ``routing_missing``; loose tickets without a tier
launch tier-unaware and the run pre-stage plans the pedido before executing
(TASK-2026-00204). Tickets
whose Jardal ERP business link is still pending never launch. Wait reasons are
typed (``state.WAIT_*``) and stored on the ticket's ``waiting_reason`` so
the dashboard and Horizon read them through the existing queries. All of
them classify as operational (``waiting_needs_human_review`` allowlists
human patterns only), so parked tickets never queue the owner.
"""

from __future__ import annotations

from business.links import business_mirror

import asyncio
import logging
from collections import Counter
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from agents.dev.review_policy import resolve_review, review_wait_reason
from agents.dev.routing import ROUTING_MISSING_REASON, resolve_coding_tool
from state import (
    WAIT_BLOCKED_BY,
    WAIT_LIMIT_GLOBAL,
    WAIT_LIMIT_OBJECTIVE,
    WAIT_LIMIT_PROJECT,
    WAIT_QUOTA,
    WAIT_REPO_BUSY,
    CodingTool,
    TaskStatus,
)

logger = logging.getLogger(__name__)

#: Admission outcome of a ticket parked only for lack of global slots.
PARKED_FOR_CAPACITY = "parked_capacity"

__all__ = [
    "Dispatcher",
    "execution_repo_for_project_config",
    "priority_rank",
]

_PRIORITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}

#: Corrupt non-datetime values land here: holding is safer than launching.
_FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)


def priority_rank(value: Any) -> int:
    """Sort rank for dispatch order. Unknown or missing means medium."""
    return _PRIORITY_RANK.get(str(value or "").strip().lower(), 2)


def _aware(moment: Any) -> datetime | None:
    """Normalize a stored datetime. Never crashes, never launches on garbage.

    Legacy motor rows come back naive (UTC): assume UTC. ISO-8601
    strings (Tier S parks) parse; a corrupt value holds (far future) with
    a warning instead of the old tick-killing TypeError: the backup
    re-checks, so a fixed row recovers.
    """
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    if isinstance(moment, str):
        text = moment.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    logger.warning("Dispatcher: corrupt datetime %r treated as far future (hold)", moment)
    return _FAR_FUTURE


def _business_link_pending(ticket: dict[str, Any]) -> bool:
    """True when the business link is still pending (ERP outage at creation).

    Reads only the local mirror (``pending`` flag or a ref without a name):
    no live Jardal ERP reads. Tickets without any mirror section (personal
    scope, pre-integration legacy) are not pending.
    """
    params = ticket.get("params")
    if not isinstance(params, dict):
        return False
    mirrors = params.get("_mirrors")
    if not isinstance(mirrors, dict):
        return False
    mirror = business_mirror(mirrors)
    if not isinstance(mirror, dict):
        return False
    if mirror.get("pending"):
        return True
    if not str(mirror.get("name") or "").strip() and mirror.get("ref"):
        return True
    return False


def execution_repo_for_project_config(project_config: Any) -> str | None:
    """Repo path an execution on this project config would work on.

    Advisory-only peek for the dispatcher: the real enforcement is the
    atomic acquire at build time (``agents.dev.session``), which stays the
    single owner of the lock. Mirrors its resolution (first repo,
    ``local_path`` or ``path``, resolved when it exists on disk).
    """
    if not isinstance(project_config, dict):
        return None
    repos = (project_config.get("project") or {}).get("repos") or []
    if not repos or not isinstance(repos[0], dict):
        return None
    local_path = repos[0].get("local_path") or repos[0].get("path")
    if not local_path:
        return None
    from pathlib import Path

    path = Path(str(local_path))
    try:
        if path.exists():
            return str(path.resolve())
    except OSError:
        pass
    return str(local_path)


class Dispatcher:
    """Event-driven dispatch with a single conditional backup trigger.

    ``service`` is the duck-typed ``TaskService`` (autorun gate, blocker
    check, patch funnel with broadcast). ``launcher`` defaults to
    ``service.run_task``; tests inject a simulated executor.
    ``repo_for_project`` maps a project key to its execution repo for the
    lock peek; ``None`` disables the peek (the build-time lock still
    enforces). ``rate_monitor`` may be ``None`` only outside production
    (quota/slot checks then pass); production always wires it.

    Legacy since ticket #572: in production the global schedule kernel
    runs ``DispatcherScheduleAgenda`` and this loop no longer starts. The
    class stays behaviorally unchanged as the harness for the frozen
    oracle (``tests/test_dispatcher.py``); ``delegate_wake_to`` redirects
    ``wake`` to the kernel without touching the producers.
    """

    def __init__(
        self,
        *,
        repo: Any,
        settings: Any,
        service: Any,
        rate_monitor: Any | None = None,
        launcher: Callable[[str], Awaitable[Any]] | None = None,
        repo_for_project: Callable[[str], str | None] | None = None,
        deadline_watch: Any | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._service = service
        self._rate_monitor = rate_monitor
        self._launcher = launcher or service.run_task
        self._repo_for_project = repo_for_project
        self._deadline_watch = deadline_watch
        self._job: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._wake_delegate: Callable[[], None] | None = None

    # ------------------------- lifecycle -------------------------

    def delegate_wake_to(self, waker: Callable[[], None] | None) -> None:
        """Redirect ``wake`` to the kernel (ticket #572). Unbound, as before."""
        self._wake_delegate = waker

    def wake(self) -> None:
        """A CRUD event says the queue may have changed. Coalesced."""
        if self._wake_delegate is not None:
            self._wake_delegate()
            return
        self._wake.set()

    def set_deadline_watch(self, watch: Any | None) -> None:
        """Attach the DeadlineWatch (TASK-2026-00383, §7); None disables."""
        self._deadline_watch = watch

    async def close_deadline_watch(self) -> None:
        """Disarm the watch timer. Never raises."""
        watch = self._deadline_watch
        self._deadline_watch = None
        if watch is None:
            return
        try:
            await watch.close()
        except Exception:
            logger.warning("Dispatcher: deadline watch close failed",
                           exc_info=True)

    async def start(self) -> None:
        """Bounded startup reconciliation, then the reactive loop."""
        if self._job is not None and not self._job.done():
            return
        self._stop.clear()
        self._wake.clear()
        try:
            await self.dispatch_cycle()
        except Exception:
            # A transient hiccup at boot must not fail the lifespan: the
            # loop re-runs the cycle on the first wake or backup.
            logger.exception("Dispatcher startup reconcile failed; loop will retry")
        self._job = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None

    def _backup_seconds(self) -> int:
        return max(5, int(getattr(self._settings, "DISPATCHER_BACKUP_SECONDS", 300) or 300))

    def _max_per_project(self) -> int:
        return max(1, int(getattr(self._settings, "DISPATCHER_MAX_ACTIVE_PER_PROJECT", 1) or 1))

    def _max_per_objective(self) -> int:
        return max(1, int(getattr(self._settings, "DISPATCHER_MAX_ACTIVE_PER_OBJECTIVE", 6) or 6))

    # ------------------------- loop -------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            # Clear BEFORE working: a wake landing mid-cycle must not be
            # lost. The only safe order (Horizon/research pattern).
            self._wake.clear()
            try:
                await self.dispatch_cycle()
                supervisor = self.__dict__.get("_loop_supervisor")
                if supervisor is not None:
                    supervisor.success()
            except Exception as exc:
                # Reliability hunter (TASK-2026-00372): record one exception
                # incident and restart with backoff; transients recovered by
                # the engine only log a WARNING inside the supervisor.
                try:
                    from reliability.supervisor import LoopSupervisor
                except Exception:
                    logger.warning("Dispatcher reliability wiring unavailable",
                                   exc_info=True)
                    supervisor = None
                else:
                    supervisor = self.__dict__.get("_loop_supervisor")
                    if supervisor is None:
                        supervisor = LoopSupervisor(
                            name="dispatcher", component="dispatcher")
                        self.__dict__["_loop_supervisor"] = supervisor
                if supervisor is not None:
                    try:
                        await asyncio.sleep(await supervisor.failure(
                            exc, source=self._repo, phase="dispatch_cycle"))
                    except Exception:
                        logger.warning("Dispatcher incident capture failed",
                                       exc_info=True)
                logger.exception("Dispatcher cycle failed; will retry on next wake")
            delay = await self._seconds_until_backup()
            try:
                if delay is None:
                    await self._wake.wait()
                else:
                    await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, delay))
            except asyncio.TimeoutError:
                pass

    async def backup_due_at(self, now: datetime | None = None) -> datetime | None:
        """Backup due as an aware datetime; None when no backup exists.

        Shared by the isolated loop and the kernel agenda (ticket #572):
        one normalization, one fail-open rule. An unreadable store fails
        open to ``now + interval`` (the lost-event net); a missing backup
        means an empty queue and no timer at all.
        """
        try:
            backup = await self._repo.get_dispatcher_backup()
        except Exception:
            logger.warning("Dispatcher: backup unreadable; retrying shortly", exc_info=True)
            moment = _aware(now) or datetime.now(timezone.utc)
            return moment + timedelta(seconds=self._backup_seconds())
        if not backup or not backup.get("next_run_at"):
            return None
        return _aware(backup.get("next_run_at"))

    async def _seconds_until_backup(self) -> float | None:
        due_at = await self.backup_due_at()
        if due_at is None:
            return None
        return (due_at - datetime.now(timezone.utc)).total_seconds()

    # ------------------------- cycle -------------------------

    async def dispatch_cycle(
        self, now: datetime | None = None, *, arm_deadlines: bool = True,
    ) -> dict[str, Any]:
        """One bounded pass: requeue due waiting, launch ready, park the rest.

        Returns counts plus the backup due time (``None`` when the queue is
        empty and no backup exists). A bad ticket never starves the others.
        ``arm_deadlines=False`` checks due deadlines without arming the
        watch timer: the kernel path (ticket #572), where the kernel owns
        the only wait and the deadline due arrives via the agenda.
        """
        moment = _aware(now) or datetime.now(timezone.utc)
        requeued = await self._requeue_due_waiting(moment)
        await self._refresh_deadlines(moment, arm=arm_deadlines)

        queued = await self._repo.list_tasks_by_status(TaskStatus.QUEUED)
        active = await self._repo.list_tasks_by_status(TaskStatus.IN_PROGRESS)
        active_view = list(active)
        project_active: Counter[str] = Counter()
        objective_active: Counter[str] = Counter()
        for row in active:
            project_active[str(row.get("project") or "default")] += 1
            objective_id = row.get("objective_id")
            if objective_id:
                objective_active[str(objective_id)] += 1

        ordered = sorted(
            queued,
            key=lambda t: (
                -priority_rank(t.get("priority")),
                _aware(t.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        launched: list[str] = []
        parked = 0
        repo_cache: dict[str, str | None] = {}
        capacity_reserved = False
        for ticket in ordered:
            try:
                outcome = await self._admit_and_launch(
                    ticket, active_view, project_active, objective_active,
                    moment, repo_cache, capacity_reserved=capacity_reserved,
                )
            except Exception:
                logger.warning(
                    "Dispatcher: skipping ticket %s after an admission error",
                    ticket.get("task_id"), exc_info=True,
                )
                continue
            if outcome == "launched":
                launched.append(str(ticket.get("task_id") or ""))
                project_active[str(ticket.get("project") or "default")] += 1
                objective_id = ticket.get("objective_id")
                if objective_id:
                    objective_active[str(objective_id)] += 1
                active_view.append({**ticket, "status": TaskStatus.IN_PROGRESS.value})
            elif outcome == PARKED_FOR_CAPACITY:
                parked += 1
                capacity_reserved = True
            elif outcome == "parked":
                parked += 1

        backup_at = await self._refresh_backup(moment, set(launched))
        return {
            "requeued": requeued,
            "launched": launched,
            "parked": parked,
            "backup_at": backup_at,
        }

    async def _refresh_deadlines(self, now: datetime, *, arm: bool = True) -> None:
        """DeadlineWatch pass: record due misses, re-arm the timer.

        Fail-open and off by default: without a watch this is a no-op,
        so existing cycles behave exactly as before. With ``arm=False``
        the due check runs but no timer is armed (kernel path).
        """
        watch = self._deadline_watch
        if watch is None:
            return
        try:
            if arm:
                await watch.refresh(now)
            else:
                await watch.check_due(now)
        except Exception:
            logger.warning("Dispatcher: deadline refresh failed (fail-open)",
                           exc_info=True)

    async def _requeue_due_waiting(self, now: datetime) -> int:
        due = await self._repo.due_waiting_tasks(now)
        requeued = 0
        for task in due:
            try:
                if not self._service._task_allows_autorun(task):
                    continue
                params = task.get("params") or {}
                if (params.get("owner_question_id") or params.get("secret_block")
                        or task.get("phase") == "owner_question"
                        or task.get("qa_review_state") in {"pending", "changes_requested"}):
                    continue
                if task.get("blocked_by") and await self._service.blockers_open(task):
                    continue
                await self._service.patch_task(
                    str(task.get("task_id") or ""),
                    {
                        "status": TaskStatus.QUEUED.value,
                        "waiting_reason": None,
                        "resume_at": None,
                    },
                )
                requeued += 1
            except Exception:
                logger.warning(
                    "Dispatcher: cannot requeue waiting ticket %s",
                    task.get("task_id"), exc_info=True,
                )
        return requeued

    async def _admit_and_launch(
        self,
        ticket: dict[str, Any],
        active_view: list[dict[str, Any]],
        project_active: Counter[str],
        objective_active: Counter[str],
        now: datetime,
        repo_cache: dict[str, str | None],
        *,
        capacity_reserved: bool = False,
    ) -> str:
        """Admit one ticket: ``launched``, ``parked`` or ``skipped``.

        ``skipped`` is silent (held tickets: autorun off, pending business
        link, future resume, or a lost claim race). ``parked`` writes a
        typed reason when it changed.
        """
        task_id = str(ticket.get("task_id") or "")
        if not self._service._task_allows_autorun(ticket):
            return "skipped"
        if _business_link_pending(ticket):
            # No execution on work with no business record (§8/§10). The
            # repair path wakes when the link heals; the backup is the net.
            return "skipped"
        resume_at = _aware(ticket.get("resume_at"))
        if resume_at is not None and resume_at > now:
            return "skipped"
        decision = resolve_coding_tool(ticket)
        effective = ticket
        effective_tier = decision.tool
        if effective_tier is None and ticket.get("objective_id"):
            # Planner/validation gap: assigning a tier would mask it, so
            # this stays fail-closed (same shape as the build-time path).
            await self._park(
                ticket, f"{ROUTING_MISSING_REASON}: {decision.detail}",
                None, now,
            )
            return "parked"
        if effective_tier is not None:
            # Per-ticket review switch (00249): invalid input and S+off
            # park before the claim; the run re-checks (same resolver).
            review_hold = review_wait_reason(
                resolve_review(ticket), effective_tier.value)
            if review_hold is not None:
                await self._park(ticket, review_hold, None, now)
                return "parked"
        # A loose ticket without a tier is either a pedido (the run
        # pre-stage plans it before executing, TASK-2026-00204) or
        # explicit non-dev work: admission proceeds tier-unaware and
        # the run decides. No keyword peek anymore.
        if ticket.get("blocked_by") and await self._service.blockers_open(ticket):
            blockers = ",".join(str(b) for b in (ticket.get("blocked_by") or []))
            await self._park(ticket, f"{WAIT_BLOCKED_BY}: {blockers}", None, now)
            return "parked"

        # Limit and repo parks are event-held (a finish wakes the cycle in
        # seconds; the backup is the net), so they carry no resume_at. Only
        # quota parks are time-held: the reset instant is known.
        project = str(ticket.get("project") or "default")
        if project_active[project] >= self._max_per_project():
            await self._park(ticket, WAIT_LIMIT_PROJECT, None, now)
            return "parked"
        objective_id = ticket.get("objective_id")
        if objective_id and objective_active[str(objective_id)] >= self._max_per_objective():
            await self._park(ticket, WAIT_LIMIT_OBJECTIVE, None, now)
            return "parked"

        if self._rate_monitor is not None:
            if capacity_reserved:
                # An older ticket waits for free slots: newer ones never
                # take them first, or a heavy ticket could wait forever.
                await self._park(ticket, WAIT_LIMIT_GLOBAL, None, now)
                return "parked"
            can, new_resume, reason = await self._rate_monitor.admission_for_ticket(
                effective, active_tickets=active_view,
            )
            if not can:
                await self._park(ticket, reason or WAIT_QUOTA, new_resume, now)
                return PARKED_FOR_CAPACITY if reason == WAIT_LIMIT_GLOBAL \
                    else "parked"

        if effective_tier is not CodingTool.TIER_S:
            # Tier S owns its locking inside the cycle (run-scoped holder
            # with transient backoff on contention): peeking the outer lock
            # would only park it over-conservatively.
            repo_busy = await self._repo_busy(ticket, project, now, repo_cache)
            if repo_busy is not None:
                await self._park(ticket, f"{WAIT_REPO_BUSY}: {repo_busy}", None, now)
                return "parked"

        claimed = await self._repo.claim_queued_ticket(task_id)
        if claimed is None:
            # Lost the race: another cycle (or a manual run) took it.
            return "skipped"
        job = asyncio.create_task(self._launcher(task_id))
        job.add_done_callback(self._on_launch_done)
        return "launched"

    def _on_launch_done(self, job: asyncio.Task[Any]) -> None:
        try:
            job.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("Dispatcher: launched execution failed", exc_info=True)
        self.wake()

    async def _repo_busy(
        self,
        ticket: dict[str, Any],
        project: str,
        now: datetime,
        repo_cache: dict[str, str | None],
    ) -> str | None:
        """Held repo path, or None when free/unknown (peek only)."""
        if self._repo_for_project is None:
            return None
        if project not in repo_cache:
            try:
                repo_cache[project] = self._repo_for_project(project)
            except Exception:
                logger.warning(
                    "Dispatcher: repo resolution failed for %s", project, exc_info=True)
                repo_cache[project] = None
        repo = repo_cache[project]
        if not repo:
            return None
        try:
            lock = await self._repo.repo_lock_holder(repo)
        except Exception:
            logger.warning("Dispatcher: repo lock unreadable for %s", repo, exc_info=True)
            return None
        if not lock:
            return None
        if str(lock.get("holder") or "") == str(ticket.get("task_id") or ""):
            # Own (possibly stale) lock: the build-time acquire refreshes it.
            return None
        expires_at = _aware(lock.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            return None
        return str(repo)

    async def _park(
        self,
        ticket: dict[str, Any],
        reason: str,
        resume_at: datetime | None,
        now: datetime,
    ) -> None:
        current_reason = ticket.get("waiting_reason")
        current_resume = _aware(ticket.get("resume_at"))
        new_resume = _aware(resume_at)
        if current_reason == reason and current_resume == new_resume:
            return
        await self._service.patch_task(
            str(ticket.get("task_id") or ""),
            {"waiting_reason": reason, "resume_at": new_resume},
        )

    async def _refresh_backup(self, now: datetime, launched: set[str]) -> datetime | None:
        """Ensure the single backup iff tickets still wait; else delete it.

        Holders are queued autorun-ok tickets that this cycle did not launch
        (limited, quota, blocked, repo -- any reason: the backup is the
        lost-event net) plus waiting autorun-ok tickets with a future resume.
        Human/phase waits (no resume) never hold it: their wake is explicit.
        """
        queued = await self._repo.list_tasks_by_status(TaskStatus.QUEUED)
        waiting = await self._repo.list_tasks_by_status(TaskStatus.WAITING)
        earliest: datetime | None = None
        holders = 0
        scheduled_only = True
        for ticket in queued:
            if str(ticket.get("task_id") or "") in launched:
                continue
            if not self._service._task_allows_autorun(ticket):
                continue
            holders += 1
            scheduled_only = False
            resume_at = _aware(ticket.get("resume_at"))
            if resume_at is not None and resume_at > now and (
                earliest is None or resume_at < earliest
            ):
                earliest = resume_at
        for ticket in waiting:
            if not self._service._task_allows_autorun(ticket):
                continue
            resume_at = _aware(ticket.get("resume_at"))
            if resume_at is not None and resume_at > now:
                holders += 1
                if not str(ticket.get("waiting_reason") or "").startswith("scheduled_wait:"):
                    scheduled_only = False
                if earliest is None or resume_at < earliest:
                    earliest = resume_at
        if holders == 0:
            await self._repo.clear_dispatcher_backup()
            return None
        due_at = now + timedelta(seconds=self._backup_seconds())
        if earliest is not None and (scheduled_only or earliest < due_at):
            due_at = earliest
        await self._repo.ensure_dispatcher_backup(due_at, f"{holders} waiting")
        return due_at
