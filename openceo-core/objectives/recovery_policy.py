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

A recovery ticket that merged a fix parks its objective as
``recovery_deploy`` (TASK-2026-00483): the mission resumes only once the
service restarted with the fix live, or on the running code after a
bounded wait, never on the code that parked it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

__all__ = [
    "DEFAULT_RECOVERY",
    "DEPLOY_REASON",
    "RECOVERABLE_REASONS",
    "deploy_park_patch",
    "deploy_retry_due",
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
    # A merged recovery fix is made live by restarting the service while
    # it is idle; the objective waits for that restart, checking every
    # ``deploy_retry_minutes``, and resumes on the running code after
    # ``deploy_max_wait_minutes`` rather than wait forever.
    "deploy_on_close": True,
    "deploy_retry_minutes": 10,
    "deploy_max_wait_minutes": 120,
    # A none-decision on a stale due rechecks after this delay instead of
    # spinning: the kernel re-drives past-dues right after every lane end
    # (TASK-2026-00534).
    "none_recheck_minutes": 10,
}

#: Park reason while a merged recovery fix waits for its restart.
DEPLOY_REASON = "recovery_deploy"

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
    for key, low, high in (("max_tickets", 0, 10),
                           ("deploy_retry_minutes", 1, 240),
                           ("deploy_max_wait_minutes", 1, 24 * 60),
                           ("none_recheck_minutes", 1, 60)):
        try:
            cfg[key] = max(low, min(high, int(cfg[key])))
        except (TypeError, ValueError):
            cfg[key] = DEFAULT_RECOVERY[key]
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["deploy_on_close"] = bool(cfg.get("deploy_on_close", True))
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
        # The ticket budget spans the objective's life: dropping the round
        # here reopened round 1 forever (TASK-2026-00483).
        "ticket_round": int(existing.get("ticket_round") or 0),
        "ticket_at": existing.get("ticket_at"),
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


def deploy_park_patch(
    objective: Mapping[str, Any],
    ticket_id: str,
    objectives_cfg: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Park that holds the objective until its merged fix is live.

    Due at once (the timer deploys if the service is idle); a fresh retry
    budget, since the fix changed what the next plan runs on.
    """
    cfg = recovery_config(objectives_cfg)
    moment = _now(now)
    existing = objective.get("recovery")
    existing = dict(existing) if isinstance(existing, Mapping) else {}
    deadline = moment + timedelta(minutes=int(cfg["deploy_max_wait_minutes"]))
    return {
        "status": "waiting",
        "waiting_reason": f"{DEPLOY_REASON}: {ticket_id}",
        "recovery": {
            **existing,
            "attempts": 0, "exhausted": False, "last_reason": None,
            "due_at": _iso(moment),
            "deploy": {"ticket_id": ticket_id,
                       "requested_at": _iso(moment),
                       "deadline": _iso(deadline)},
        },
    }


def deploy_retry_due(objectives_cfg: Mapping[str, Any] | None,
                     now: datetime | None = None) -> str:
    """Next idle check of a deploy park, as a persisted ISO instant."""
    cfg = recovery_config(objectives_cfg)
    return _iso(_now(now) + timedelta(
        minutes=int(cfg["deploy_retry_minutes"])))


def _deploy_decision(
    recovery: Mapping[str, Any],
    moment: datetime,
    booted_at: datetime | None,
    idle: bool,
) -> dict[str, Any]:
    deploy = recovery.get("deploy")
    deploy = dict(deploy) if isinstance(deploy, Mapping) else {}
    requested = parse_due(deploy.get("requested_at"))
    if requested is None:
        return {"action": "resume", "reason": "deploy record missing"}
    if booted_at is not None and booted_at > requested:
        return {"action": "resume",
                "reason": "service restarted after the fix merged"}
    due = parse_due(recovery.get("due_at"))
    if due is not None and moment < due:
        return {"action": "none", "reason": "not due"}
    deadline = parse_due(deploy.get("deadline"))
    if deadline is not None and moment >= deadline:
        return {"action": "resume",
                "reason": "deploy wait expired: resuming on the running code"}
    if idle:
        return {"action": "deploy", "reason": "service idle"}
    return {"action": "defer", "reason": "service busy"}


def recovery_decision(
    objective: Mapping[str, Any],
    active_children: int,
    objectives_cfg: Mapping[str, Any] | None,
    *,
    ticket_open: bool = False,
    now: datetime | None = None,
    booted_at: datetime | None = None,
    idle: bool = False,
) -> dict[str, Any]:
    """One waiting objective: ``none`` | ``retry`` | ``ticket``, and for a
    ``recovery_deploy`` park ``deploy`` | ``defer`` | ``resume``.

    ``booted_at`` is this process's start and ``idle`` whether a restart
    would kill nothing in flight; only deploy parks read them.
    """
    cfg = recovery_config(objectives_cfg)
    if not cfg["enabled"]:
        return {"action": "none", "reason": "recovery disabled"}
    if str(objective.get("status") or "") != "waiting":
        return {"action": "none", "reason": "not waiting"}
    if isinstance(objective.get("pending_plan"), Mapping) \
            and objective.get("pending_plan"):
        return {"action": "none", "reason": "spend approval pending"}
    reason = park_reason(objective)
    recovery = objective.get("recovery")
    recovery = dict(recovery) if isinstance(recovery, Mapping) else {}
    if reason == DEPLOY_REASON:
        return _deploy_decision(recovery, _now(now), booted_at, idle)
    if reason not in RECOVERABLE_REASONS:
        return {"action": "none", "reason": f"park {reason or '<empty>'}"}
    attempts = int(recovery.get("attempts") or 0)
    if recovery.get("exhausted") or attempts >= int(cfg["max_attempts"]):
        # #588: a paced spent park (future due after a failed ticket
        # round, #586) keeps waiting on its due; without this check any
        # claim retries the ticket round immediately and defeats the pace.
        due = parse_due(recovery.get("due_at"))
        if due is not None and _now(now) < due:
            return {"action": "none", "reason": "not due"}
        if ticket_open:
            return {"action": "none", "reason": "recovery ticket open"}
        if int(recovery.get("ticket_round") or 0) >= int(cfg["max_tickets"]):
            return {"action": "none",
                    "reason": "recovery ticket budget spent"}
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
        if park_reason(objective) not in RECOVERABLE_REASONS | {DEPLOY_REASON}:
            continue
        due = parse_due((objective.get("recovery") or {}).get("due_at")) \
            if isinstance(objective.get("recovery"), Mapping) else None
        if due is not None and (nearest is None or due < nearest):
            nearest = due
    if nearest is None:
        return MAX_SLEEP_SECONDS
    seconds = int((nearest - moment).total_seconds())
    return max(MIN_SLEEP_SECONDS, min(MAX_SLEEP_SECONDS, seconds))
