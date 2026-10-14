"""Deterministic restart recovery (TASK-2026-00247).

Boot marks live executions interrupted, but the dispatcher only launches
queued tickets, so interrupted work stranded until a manual relaunch. One
bounded pass after ``mark_interrupted_tasks`` requeues it: silent writes
(no ERPNext echo, the business stays Working), human waits preserved as
waiting, Tier S stays manual, and quotas/limits/repos/blockers are left to
the dispatcher's admission on launch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from state import TaskStatus

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_RECOVERY_ATTEMPTS",
    "RECOVERY_PARAM_KEY",
    "RecoveryDecision",
    "decide",
    "recover_interrupted",
]

#: Execution record slot for the resume identity (attempt count survives).
RECOVERY_PARAM_KEY = "_recovery"

#: After this many lifetime recoveries the ticket parks in waiting for a
#: manual relaunch. Cumulative on purpose: every recovery is followed by a
#: claim, so resetting on claim would disarm the brake; six restarts without
#: a completion means the ticket cannot finish between boots.
MAX_RECOVERY_ATTEMPTS = 5

_HUMAN_QA_STATES = frozenset({"pending", "changes_requested"})


@dataclass(frozen=True)
class RecoveryDecision:
    """What one boot pass does with a single interrupted ticket."""

    action: str  # "requeue" | "to_waiting" | "skip"
    reason: str
    status: str = ""
    waiting_reason: str | None = None
    recovery_meta: Mapping[str, Any] | None = None


def _attempt_of(task: Mapping[str, Any]) -> int:
    params = task.get("params")
    if not isinstance(params, dict):
        return 0
    meta = params.get(RECOVERY_PARAM_KEY)
    if not isinstance(meta, dict):
        return 0
    try:
        return max(0, int(meta.get("attempt") or 0))
    except (TypeError, ValueError):
        return 0


def _human_hold(task: Mapping[str, Any]) -> str | None:
    params = task.get("params")
    params = params if isinstance(params, dict) else {}
    if params.get("secret_block"):
        return "secret_block"
    if params.get("owner_question_id"):
        return "owner_question"
    if str(task.get("qa_review_state") or "") in _HUMAN_QA_STATES:
        return "qa_review"
    return None


def decide(
    task: Mapping[str, Any],
    *,
    is_alive: Callable[[str], bool] | None = None,
    now: datetime | None = None,
) -> RecoveryDecision:
    """Pure per-ticket recovery decision. Never raises on odd rows."""
    try:
        task_id = str(task.get("task_id") or "")
        if str(task.get("status") or "") != TaskStatus.INTERRUPTED.value:
            return RecoveryDecision(action="skip", reason="not_interrupted")
        if str(task.get("coding_tool") or "") == "tier_s":
            return RecoveryDecision(action="skip", reason="tier_s_manual")
        if task.get("autorun") is False:
            return RecoveryDecision(action="skip", reason="autorun_off")
        if is_alive is not None and task_id and is_alive(task_id):
            return RecoveryDecision(action="skip", reason="process_alive")
        moment = now or datetime.now(timezone.utc)
        meta = {
            "attempt": _attempt_of(task) + 1,
            "at": moment.isoformat(),
            "from": TaskStatus.INTERRUPTED.value,
        }
        hold = _human_hold(task)
        if hold is not None:
            return RecoveryDecision(
                action="to_waiting",
                reason=f"human_wait_preserved:{hold}",
                status=TaskStatus.WAITING.value,
                waiting_reason=task.get("waiting_reason") or f"recovery_hold:{hold}",
                recovery_meta={**meta, "reason": "human_wait_preserved"},
            )
        if _attempt_of(task) >= MAX_RECOVERY_ATTEMPTS:
            return RecoveryDecision(
                action="to_waiting",
                reason="recovery_exhausted",
                status=TaskStatus.WAITING.value,
                waiting_reason="restart_recovery_exhausted",
                recovery_meta={**meta, "reason": "recovery_exhausted"},
            )
        return RecoveryDecision(
            action="requeue",
            reason="restart_recovery",
            status=TaskStatus.QUEUED.value,
            waiting_reason=None,
            recovery_meta={**meta, "reason": "restart_recovery"},
        )
    except Exception:
        logger.warning("Recovery: undecidable row skipped", exc_info=True)
        return RecoveryDecision(action="skip", reason="undecidable")


async def recover_interrupted(
    repo: Any,
    *,
    is_alive: Callable[[str], bool] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """One bounded boot pass over interrupted tickets (no polling).

    Returns ids requeued/parked plus skip counts by reason. A bad row never
    starves the others; running twice applies nothing twice (the repo write
    is conditional on still being interrupted).
    """
    try:
        rows = await repo.list_tasks_by_status(TaskStatus.INTERRUPTED, limit=limit)
    except Exception:
        logger.warning("Recovery: interrupted list unreadable", exc_info=True)
        return {"requeued": [], "to_waiting": [], "skipped": {"list_failed": 1}}
    requeued: list[str] = []
    to_waiting: list[str] = []
    skipped: dict[str, int] = {}
    for task in rows or []:
        if not isinstance(task, dict):
            skipped["undecidable"] = skipped.get("undecidable", 0) + 1
            continue
        decision = decide(task, is_alive=is_alive)
        task_id = str(task.get("task_id") or "")
        if decision.action == "skip" or not task_id:
            key = decision.reason or "undecidable"
            skipped[key] = skipped.get(key, 0) + 1
            continue
        try:
            applied = await repo.apply_restart_recovery(
                task_id,
                status=decision.status,
                waiting_reason=decision.waiting_reason,
                recovery_meta=dict(decision.recovery_meta or {}),
            )
        except Exception:
            logger.warning("Recovery: ticket %s not rewritten", task_id, exc_info=True)
            skipped["write_failed"] = skipped.get("write_failed", 0) + 1
            continue
        if not applied:
            skipped["already_moved"] = skipped.get("already_moved", 0) + 1
            continue
        if decision.action == "to_waiting":
            to_waiting.append(task_id)
        else:
            requeued.append(task_id)
    return {"requeued": requeued, "to_waiting": to_waiting, "skipped": skipped}
