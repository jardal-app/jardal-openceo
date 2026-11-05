"""Generic stall detection: one rule for every wait reason (TASK-2026-00510).

A wait the engine re-parks with the same reason N times in a row is a
stall, whatever the reason says; a wait that names no resume and no human
is the engine giving up. Both wake the mechanic. Pure functions only.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from state import (
    WAIT_BLOCKED_BY,
    WAIT_LIMIT_GLOBAL,
    WAIT_LIMIT_OBJECTIVE,
    WAIT_LIMIT_PROJECT,
    WAIT_QUOTA,
    WAIT_QUOTA_RESERVED,
)

STREAK_KEY = "wait_streak"
STALLED_PREFIX = "stalled"
NO_PROGRESS_PREFIX = "stalled_no_progress"
OWNER_ACTION_PREFIX = "owner_action"
MECHANIC_WAIT_PREFIX = "mechanic_wait"

#: Waits owned by a person or an approval flow: the mechanic stays out.
HUMAN_WAIT_PREFIXES = (
    "phase:",
    "owner_escalation",
    OWNER_ACTION_PREFIX,
    "spend_approval",
    "tool_lock:",
    "workflow_discovery_pending_approval",
)

#: Waits that heal through their own event (dependency close, freed slot,
#: quota reset, mirror link, mechanic snooze); the heartbeat still reviews
#: them past ``self_heal_max_hours``.
SELF_HEALING_PREFIXES = (
    WAIT_BLOCKED_BY,
    WAIT_LIMIT_GLOBAL,
    WAIT_LIMIT_PROJECT,
    WAIT_LIMIT_OBJECTIVE,
    WAIT_QUOTA_RESERVED,
    WAIT_QUOTA,
    "jardal_erp_pending",
    "erpnext_pending",
    MECHANIC_WAIT_PREFIX,
)


def as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def wait_key(reason: Any) -> str:
    text = str(reason or "").strip().lower()
    return re.split(r"[:(]", text, maxsplit=1)[0].strip()[:80]


def is_stalled_reason(reason: Any) -> bool:
    return wait_key(reason) in {STALLED_PREFIX, NO_PROGRESS_PREFIX}


def human_wait(reason: Any, params: Mapping[str, Any] | None = None, *,
               phase: Any = None, qa_state: Any = None) -> bool:
    text = str(reason or "").strip()
    if text.startswith(HUMAN_WAIT_PREFIXES):
        return True
    params = params if isinstance(params, Mapping) else {}
    if params.get("owner_question_id") or params.get("secret_block"):
        return True
    return phase == "owner_question" or qa_state in {"pending", "changes_requested"}


def self_healing_wait(reason: Any) -> bool:
    return str(reason or "").strip().startswith(SELF_HEALING_PREFIXES)


def next_streak(previous: Any, reason: Any, now: datetime, *,
                gap: timedelta) -> dict[str, Any]:
    key = wait_key(reason)
    count, since = 1, now
    if isinstance(previous, Mapping) and previous.get("key") == key:
        last = as_utc(previous.get("last_at"))
        if last is not None and now - last <= gap:
            try:
                count = max(0, int(previous.get("count") or 0)) + 1
            except (TypeError, ValueError):
                count = 1
            since = as_utc(previous.get("since")) or now
    return {"key": key, "count": count, "since": since.isoformat(),
            "last_at": now.isoformat()}


def stalled_reason(reason: Any, count: int) -> str:
    return f"{STALLED_PREFIX}: {count}x {str(reason or '').strip()}"[:300]


def streak_outcome(*, status: str, reason: Any, resume_at: Any,
                   params: Mapping[str, Any] | None, now: datetime,
                   max_reparks: int, gap: timedelta,
                   phase: Any = None, qa_state: Any = None,
                   ) -> tuple[dict[str, Any], str | None]:
    """Fields to merge into a ticket write and the mechanic trigger, if any.

    ``fields`` may carry ``waiting_reason``/``resume_at`` (a stall stops
    re-parking) and ``wait_streak`` (``None`` clears it).
    """
    params = params if isinstance(params, Mapping) else {}
    previous = params.get(STREAK_KEY)
    if status != "waiting":
        return ({STREAK_KEY: None} if previous else {}), None
    if human_wait(reason, params, phase=phase, qa_state=qa_state):
        return {}, None
    healing = self_healing_wait(reason)
    if as_utc(resume_at) is None:
        if healing:
            return {}, None
        trigger = "no_progress" if wait_key(reason) == NO_PROGRESS_PREFIX \
            else "gave_up"
        return {}, trigger
    streak = next_streak(previous, reason, now, gap=gap)
    if not healing and streak["count"] >= max(1, int(max_reparks)):
        return {
            "waiting_reason": stalled_reason(reason, streak["count"]),
            "resume_at": None,
            STREAK_KEY: streak,
        }, "stall"
    return {STREAK_KEY: streak}, None
