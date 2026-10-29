"""Transition invariants v1: INV-001 to INV-005 (design v2 section 5.3).

Pure checks over plain data, no I/O: the callers in ``task_service.py``
(business transitions) and ``business/`` (ERP adapter transitions) own
Mongo, the ERP client and the incident spool. Every checker returns a
violation dict or None; ``evaluate_transition`` aggregates them in INV
order from one context dict so every transition point shares one
contract. A violation becomes an ``invariant_violation`` incident
(severity critical, attribution engine) via :func:`violation_doc`.

Modes are per invariant, ``record`` -> ``enforce``; every invariant
starts in ``record``. In ``record`` nothing changes any current flow:
callers only register the incident. ``INV-001`` never rejects Owner or
manual-session transitions, only engine ones.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

INVARIANTS = ("INV-001", "INV-002", "INV-003", "INV-004", "INV-005")

INVARIANT_MODES = frozenset({"record", "enforce"})

DEFAULT_INVARIANT_MODE = "record"

ENGINE_ACTORS = frozenset({"engine", "agent", "runner", ""})

OWNER_ACTORS = frozenset({"owner", "manual"})

OPEN_RUN_STATUSES = frozenset({"waiting", "running", "active", "resuming"})


def invariant_mode(invariant_id: str, config: Any = None) -> str:
    """Mode for one invariant; fail-open ``record`` on unreadable config."""
    try:
        block = config if isinstance(config, dict) else None
        if block is None:
            from .config import RELIABILITY_DEFAULTS

            block = RELIABILITY_DEFAULTS.get("invariants") or {}
        mode = (block.get("mode") or {}).get(str(invariant_id))
        if mode in INVARIANT_MODES:
            return str(mode)
    except Exception:  # noqa: BLE001 - fail-open: record
        logger.warning("reliability invariant mode unreadable, using record",
                       exc_info=True)
    return DEFAULT_INVARIANT_MODE


def is_owner_actor(actor: str | None) -> bool:
    """True for Owner or manual-session transitions (INV-001 never rejects)."""
    return str(actor or "").strip().lower() in OWNER_ACTORS


def should_reject(invariant_id: str, *, actor: str | None = None,
                  config: Any = None) -> bool:
    """Whether a violation rejects its transition when rejection is possible."""
    if invariant_mode(invariant_id, config) != "enforce":
        return False
    if str(invariant_id) == "INV-001" and is_owner_actor(actor):
        return False
    return True


def _violation(invariant_id: str, detail: str, *,
               task_id: str | None = None,
               business_ref: str | None = None,
               run_id: str | None = None) -> dict[str, Any]:
    return {
        "invariant_id": str(invariant_id),
        "detail": str(detail)[:500],
        "task_id": task_id,
        "business_ref": business_ref,
        "run_id": run_id,
    }


def check_completion(*, gates_passed: bool | None,
                     review_satisfied: bool,
                     evidence: str | None) -> dict[str, Any] | None:
    """INV-001: a ticket reaches Completed only with gates, review, evidence.

    ``gates_passed=None`` means gates do not apply to this ticket.
    """
    failed = []
    if gates_passed is False:
        failed.append("gates_red")
    if not review_satisfied:
        failed.append("review_open")
    if not str(evidence or "").strip():
        failed.append("evidence_missing")
    if not failed:
        return None
    return _violation("INV-001",
                      "completed without " + "+".join(failed))


def completion_inputs_from_task(task: dict[str, Any] | None,
                                evidence: str | None) -> dict[str, Any]:
    """Derive INV-001 inputs from an execution record.

    The business-transition path carries no gate record (Tier S gate runs
    live on the run, enforced by the cycle), so gates do not apply here.
    Review follows the QA signal: required and unapproved QA means the
    review policy is unmet.
    """
    doc = task if isinstance(task, dict) else {}
    review_open = bool(doc.get("qa_review_required")) and str(
        doc.get("qa_review_state") or "").strip().lower() != "approved"
    return {
        "gates_passed": None,
        "review_satisfied": not review_open,
        "evidence": evidence,
    }


def check_reread(*, target: str, actual: str | None,
                 ticket: str | None = None) -> dict[str, Any] | None:
    """INV-002: the ERP reread after a transition returns the target state."""
    want = str(target or "").strip()
    got = str(actual or "").strip()
    if got == want:
        return None
    return _violation(
        "INV-002",
        f"{ticket or 'ticket'} landed on {got or 'unknown'}, not {want}",
        business_ref=ticket)


def _live_runs(runs: Any) -> list[dict[str, Any]]:
    live = []
    for run in runs or []:
        if isinstance(run, dict) \
                and str(run.get("status") or "") in OPEN_RUN_STATUSES:
            live.append(run)
    return live


def check_active_runs(*, task_ref: str | None,
                      active_runs: Any) -> dict[str, Any] | None:
    """INV-003 (runs): one ticket holds at most one active run."""
    live = _live_runs(active_runs)
    if len(live) < 2:
        return None
    ids = sorted({str(run.get("run_id") or "?") for run in live})
    return _violation(
        "INV-003",
        f"{task_ref or 'ticket'} holds {len(live)} active runs "
        f"({', '.join(ids)})")


def check_lock_holders(*, repo: str | None,
                       holders: Any) -> dict[str, Any] | None:
    """INV-003 (lock): one repo has at most one live lock holder."""
    seen = sorted({str(holder).strip() for holder in (holders or [])
                   if str(holder or "").strip()})
    if len(seen) < 2:
        return None
    return _violation(
        "INV-003",
        f"repo {repo or 'unknown'} has {len(seen)} lock holders "
        f"({', '.join(seen)})")


def check_checkpoint(*, checkpoint: Any, run_id: str | None,
                     owner_run_id: str | None = None) -> dict[str, Any] | None:
    """INV-004: a referenced checkpoint exists and belongs to its run."""
    if not isinstance(checkpoint, dict) or not checkpoint:
        return None
    owner = str(checkpoint.get("run_id") or owner_run_id or "").strip()
    if owner and str(run_id or "").strip() and owner != str(run_id).strip():
        return _violation(
            "INV-004",
            f"checkpoint of run {owner} resumed as run {run_id}",
            run_id=str(run_id))
    if str(checkpoint.get("phase") or "") in {"review", "build"}:
        missing = [key for key in ("branch", "head_sha", "base_sha")
                   if not str(checkpoint.get(key) or "").strip()]
        if missing:
            return _violation(
                "INV-004",
                f"checkpoint for run {run_id or '?'} "
                f"misses {', '.join(missing)}",
                run_id=str(run_id) if run_id else None)
    return None


def check_run_executable(*, run: Any) -> dict[str, Any] | None:
    """INV-005: a superseded run never executes again."""
    if not isinstance(run, dict) or not run:
        return None
    if str(run.get("status") or "").strip() == "superseded":
        run_id = str(run.get("run_id") or "?")
        return _violation(
            "INV-005",
            f"superseded run {run_id} executes again",
            run_id=run_id)
    return None


def evaluate_transition(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Check every invariant with a transition's observations, in INV order.

    Missing observations fail open (no data, no violation); only supplied
    evidence can fail. Keys: ``target``, ``actual``, ``ticket``,
    ``task``, ``evidence``, ``active_runs``, ``lock_repo``,
    ``lock_holders``, ``checkpoint``, ``run_id``, ``checkpoint_owner``,
    ``executing_run``.
    """
    ctx = context if isinstance(context, dict) else {}
    found: list[dict[str, Any] | None] = []
    target = str(ctx.get("target") or "").strip()
    if target == "Completed":
        inputs = completion_inputs_from_task(ctx.get("task"),
                                             ctx.get("evidence"))
        found.append(check_completion(**inputs))
    if "actual" in ctx:
        found.append(check_reread(target=target, actual=ctx.get("actual"),
                                  ticket=ctx.get("ticket")))
    found.append(check_active_runs(task_ref=ctx.get("ticket"),
                                   active_runs=ctx.get("active_runs")))
    found.append(check_lock_holders(repo=ctx.get("lock_repo"),
                                    holders=ctx.get("lock_holders")))
    found.append(check_checkpoint(checkpoint=ctx.get("checkpoint"),
                                  run_id=ctx.get("run_id"),
                                  owner_run_id=ctx.get("checkpoint_owner")))
    found.append(check_run_executable(run=ctx.get("executing_run")))
    return [item for item in found if item is not None]


def violation_doc(violation: dict[str, Any], *,
                  component: str = "business_sync",
                  task_id: str | None = None,
                  business_ref: str | None = None,
                  run_id: str | None = None,
                  release: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the critical engine-attributed incident doc for a violation."""
    from .capture import build_incident_doc

    invariant_id = str(violation.get("invariant_id") or "INV-?")
    return build_incident_doc(
        kind="invariant_violation", component=component,
        invariant_id=invariant_id, reason_code=invariant_id,
        task_id=task_id or violation.get("task_id") or business_ref,
        run_id=run_id or violation.get("run_id"),
        business_ref=business_ref or violation.get("business_ref"),
        message=str(violation.get("detail") or invariant_id),
        release=release)
