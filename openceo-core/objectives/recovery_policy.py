"""Deterministic recovery of parked objectives (TASK-2026-00481).

A park is a wait with no trigger of its own: ``capability_gap`` resumes
only on a child close, ``blocked``/``erp_error``/``config``/``data_gaps``
resume only when something else plans again, and a mission can sit
there until a human restarts the service. This module decides when a
park may retry on its own, with a persisted due time and a bounded
number of attempts; the executor-side scheduler lives in
``recovery_scheduler.py`` so this file stays pure and testable.

No polling: the due time is written with the park and a single timer
wakes for the nearest one. The planner still runs only through
``plan_objective`` (spend gate, plan flights and CEO review untouched).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

__all__ = [
    "DEFAULT_RECOVERY",
    "RECOVERABLE_REASONS",
    "park_reason",
    "recovery_decision",
    "recovery_park_patch",
    "parse_due",
    "recovery_config",
    "recovery_initial_patch",
    "recovery_seconds_to_next",
]

#: Park reasons whose wedge can plausibly clear without a human. A
#: ``pending_plan`` spend hold never enters here: only the Owner answer
#: releases it.
RECOVERABLE_REASONS = frozenset({
    "capability_gap", "blocked", "data_gaps", "erp_error", "config",
    "planner_error", "no_quorum", "closer_no_verdict", "stalled",
})

DEFAULT_RECOVERY: dict[str, Any] = {
    "enabled": True,
    # Two automatic retries before a recovery ticket is opened: enough to
    # clear a transient/credential wedge, bounded against planner loops.
    "max_attempts": 2,
    # Minutes after the park for attempt 1 and 2 (the last value repeats
    # if the list is shorter than the budget).
    "backoff_minutes": [10, 60],
    # The recovery ticket lives in OpenCEO's own project: the agent that
    # diagnoses and fixes the orchestrator runs there, not in the mission
    # repo. One open per objective.
    "ticket_project": "jardal-openceo",
    "ticket_tier": "tier_c",
    "ticket_max_open": 1,
    # A recovery ticket that fails gets one more with the failure evidence;
    # past this the objective stays parked for the CEO/Owner signal.
    "max_tickets": 2,
}

#: Hard cap for the scheduler sleep, and the minimum wake after a due
#: instant already passed (never a busy loop).
MAX_SLEEP_SECONDS = 3600
MIN_SLEEP_SECONDS = 5


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _iso(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat()


def parse_due(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recovery_config(objectives_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalized recovery block; malformed values keep safe defaults."""
    raw = (objectives_cfg or {}).get("recovery")
    cfg = dict(DEFAULT_RECOVERY)
    if isinstance(raw, Mapping):
        for key in DEFAULT_RECOVERY:
            if key in raw:
                cfg[key] = raw[key]
    try:
        cfg["max_attempts"] = max(0, min(10, int(cfg["max_attempts"])))
    except (TypeError, ValueError):
        cfg["max_attempts"] = DEFAULT_RECOVERY["max_attempts"]
    backoff = cfg.get("backoff_minutes")
    if not isinstance(backoff, (list, tuple)):
        backoff = list(DEFAULT_RECOVERY["backoff_minutes"])
    clean: list[int] = []
    for value in backoff:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            clean.append(minutes)
    cfg["backoff_minutes"] = clean or list(
        DEFAULT_RECOVERY["backoff_minutes"])
    if not isinstance(cfg.get("ticket_project"), str) \
            or not cfg["ticket_project"].strip():
        cfg["ticket_project"] = DEFAULT_RECOVERY["ticket_project"]
    if cfg.get("ticket_tier") not in ("tier_a", "tier_b", "tier_c"):
        cfg["ticket_tier"] = DEFAULT_RECOVERY["ticket_tier"]
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def park_reason(objective: Mapping[str, Any]) -> str:
    """Reason prefix of ``waiting_reason`` (``capability_gap: ...``)."""
    return str(objective.get("waiting_reason") or "").partition(":")[0].strip()


def _backoff_minutes(cfg: Mapping[str, Any], attempts: int) -> int:
    values = list(cfg.get("backoff_minutes") or [10])
    return int(values[min(max(0, attempts), len(values) - 1)])


def recovery_park_patch(
    objective: Mapping[str, Any],
    reason: str,
    detail: str,
    objectives_cfg: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """``recovery`` field to write with a park, or None when not eligible.

    ``attempts`` counts claims already made; a claim increments it before
    planning, so the park that follows a failed recovery schedules the next
    backoff (or marks the budget exhausted). The previous due time is kept
    when it is nearer, so repeated parks never push the retry out.
    """
    cfg = recovery_config(objectives_cfg)
    if not cfg["enabled"] or reason not in RECOVERABLE_REASONS:
        return None
    moment = _now(now)
    existing = objective.get("recovery")
    existing = dict(existing) if isinstance(existing, Mapping) else {}
    attempts = int(existing.get("attempts") or 0)
    last_reason = f"{reason}: {detail}"[:800]
    base = {
        "attempts": attempts,
        "last_reason": last_reason,
        "last_park_at": _iso(moment),
        "last_attempt_at": existing.get("last_attempt_at"),
        "ticket_id": existing.get("ticket_id"),
    }
    if existing.get("exhausted"):
        return {"recovery": {**base, "exhausted": True, "due_at": None}}
    if attempts >= int(cfg["max_attempts"]):
        return {"recovery": {**base, "exhausted": True, "due_at": None}}
    due = moment + timedelta(minutes=_backoff_minutes(cfg, attempts))
    previous = parse_due(existing.get("due_at"))
    if previous is not None and moment < previous < due:
        due = previous
    return {"recovery": {**base, "exhausted": False, "due_at": _iso(due)}}


def recovery_initial_patch(
    objective: Mapping[str, Any],
    objectives_cfg: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Seed a legacy park (no ``recovery`` field) with its first due time."""
    if isinstance(objective.get("recovery"), Mapping):
        return None
    reason = park_reason(objective)
    if reason not in RECOVERABLE_REASONS:
        return None
    detail = str(objective.get("waiting_reason") or "").partition(":")[2] \
        .strip() or str(objective.get("waiting_reason") or "")
    return recovery_park_patch(
        dict(objective, recovery={}), reason, detail[:600],
        objectives_cfg, now)


def recovery_decision(
    objective: Mapping[str, Any],
    active_children: int,
    objectives_cfg: Mapping[str, Any] | None,
    *,
    ticket_open: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``none`` | ``retry`` | ``ticket`` for one waiting objective."""
    cfg = recovery_config(objectives_cfg)
    if not cfg["enabled"]:
        return {"action": "none", "reason": "recovery disabled"}
    if str(objective.get("status") or "") != "waiting":
        return {"action": "none", "reason": "not waiting"}
    if isinstance(objective.get("pending_plan"), Mapping) \
            and objective.get("pending_plan"):
        return {"action": "none", "reason": "spend approval pending"}
    reason = park_reason(objective)
    if reason not in RECOVERABLE_REASONS:
        return {"action": "none", "reason": f"park {reason or '<empty>'}"}
    recovery = objective.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, Mapping) else {}
    attempts = int(recovery.get("attempts") or 0)
    if recovery.get("exhausted") or attempts >= int(cfg["max_attempts"]):
        if ticket_open:
            return {"action": "none", "reason": "recovery ticket open"}
        return {"action": "ticket", "reason": reason}
    if active_children > 0:
        return {"action": "none", "reason": "children still active"}
    due = parse_due(recovery.get("due_at"))
    moment = _now(now)
    if due is not None and moment < due:
        return {"action": "none", "reason": "not due"}
    return {"action": "retry", "reason": reason}


def recovery_seconds_to_next(
    objectives: list[Mapping[str, Any]],
    now: datetime | None = None,
) -> int:
    """Seconds until the nearest persisted due time, clamped [5, 3600]."""
    moment = _now(now)
    nearest: datetime | None = None
    for objective in objectives:
        if str(objective.get("status") or "") != "waiting":
            continue
        if park_reason(objective) not in RECOVERABLE_REASONS:
            continue
        due = parse_due((objective.get("recovery") or {}).get("due_at")) \
            if isinstance(objective.get("recovery"), Mapping) else None
        if due is not None and (nearest is None or due < nearest):
            nearest = due
    if nearest is None:
        return MAX_SLEEP_SECONDS
    seconds = int((nearest - moment).total_seconds())
    return max(MIN_SLEEP_SECONDS, min(MAX_SLEEP_SECONDS, seconds))
