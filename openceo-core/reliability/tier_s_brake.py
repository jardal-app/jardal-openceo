"""Attempt-boundary capture and ProgressGuard for the Tier S cycle.

Design v2 sections 4.5 and 5.2 (epic TASK-2026-00365, ticket TASK-2026-00376).
Used by ``agents/dev/tier_s/cycle.py``; dev A/B/C and the generic agent use
``loop_brake`` instead and Tier S never imports that module.

Never raises: guard failures fail open (the loop continues) and capture
failures only log. ``None`` or mock/synthetic stores never capture: without
a real incidents collection the helpers record the guard state and skip the
incident (fail-open skip). With a real collection down, ``capture.py``
spools as usual.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

TIER_S_PRODUCER = "tier_s"
COMPONENT = "tier_s"

MILESTONE_PLAN_READY = "plan-ready"
MILESTONE_BUILD_DONE = "build-done"
MILESTONE_GATE_FIXED = "gate-fixed"
MILESTONE_REVIEW_REPORT = "review-report"
MILESTONE_FINDING_RESOLVED = "finding-resolved"

PROGRESS_GUARD_KEY = "progress_guard"
BRAKE_KEY = "brake"
GUARD_ERROR_KEY = "guard_error"

_CREDENTIAL_MARKERS = (
    "credential", "api key", "apikey", "unauthorized", "forbidden",
    "auth fail", "authentication", "token expired", "invalid_api_key",
)


def _is_mock(obj: Any) -> bool:
    return type(obj).__module__.split(".")[0] == "unittest" \
        and "mock" in type(obj).__module__


def resolve_incidents(source: Any) -> Any | None:
    """Strict incidents collection behind a repo/store, else None (skip)."""
    try:
        if source is None or _is_mock(source):
            return None
        if callable(getattr(source, "insert_one", None)) \
                and callable(getattr(source, "count_documents", None)):
            return source
        db = getattr(source, "db", None)
        if db is None or _is_mock(db):
            return None
        try:
            coll = db["reliability_incidents"]
        except Exception:
            logger.debug("incidents collection unresolvable (skip capture)",
                         exc_info=True)
            return None
        if _is_mock(coll):
            return None
        if callable(getattr(coll, "insert_one", None)) \
                and callable(getattr(coll, "count_documents", None)):
            return coll
        return None
    except Exception:
        logger.debug("incidents source unreadable (skip capture)",
                     exc_info=True)
        return None


def guard_mode() -> str:
    """Shadow by default; the calibration child owns the enforce switch."""
    try:
        from .config import RELIABILITY_DEFAULTS

        mode = RELIABILITY_DEFAULTS["guard"]["mode"].get(TIER_S_PRODUCER)
        if mode in ("off", "shadow", "enforce"):
            return str(mode)
    except Exception:  # noqa: BLE001 - fail-open: shadow
        logger.warning("reliability guard mode unreadable, using shadow",
                       exc_info=True)
    return "shadow"


def next_attempt_id(task_id: str, params: Any) -> str:
    """Deterministic attempt id from the stored guard state."""
    state = params.get(PROGRESS_GUARD_KEY) if isinstance(params, dict) else None
    generation = 0
    window_len = 0
    if isinstance(state, dict):
        try:
            generation = int(state.get("generation") or 0)
            window = state.get("window") or []
            window_len = len(window) if isinstance(window, list) else 0
        except (TypeError, ValueError):
            pass
    clean = "".join(ch for ch in str(task_id) if ch.isalnum() or ch in "-_")[:48]
    return f"{clean or 'task'}-g{generation}-w{window_len}"


def terminal_reason_code(error: str) -> str:
    """Map a terminal error to the engine reason_code enum vocabulary.

    Credential failures carry the ``auth:`` prefix so the A9 map attributes
    them to the environment; anything else keeps its own text truncated.
    """
    text = str(error or "").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        return f"auth: {text[:100]}".strip()
    return text[:120] or "terminal_failure"


def evaluate_attempt(
    params: dict[str, Any],
    *,
    producer: str,
    phase: str,
    attempt_id: str,
    result_sig: str,
    work_fp: str,
    fp_complete: bool,
    executed: bool,
    milestone: str = "",
    mode: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Evaluate the brake and store ``progress_guard`` in params.

    The caller persists ``params`` through its own checkpoint write, so the
    guard state travels in the same atomic write. Never raises: on failure
    ``params["guard_error"]`` records it and the loop continues.
    """
    try:
        from . import guard as guard_mod
    except Exception as exc:  # noqa: BLE001 - fail-open
        params[GUARD_ERROR_KEY] = f"guard unavailable: {exc}"[:300]
        return {"action": "continue", "rule": None, "incident": None,
                "shadow": True, "fail_open": True, "brake_error": str(exc)[:200]}
    resolved_mode = mode or guard_mode()
    if resolved_mode == "off":
        return {"action": "continue", "rule": None, "incident": None,
                "shadow": False, "fail_open": False}
    if milestone:
        credited = params.get(PROGRESS_GUARD_KEY)
        names = credited.get("milestones") \
            if isinstance(credited, dict) else []
        if isinstance(names, list) and milestone in names:
            return {"action": "continue", "rule": None, "incident": None,
                    "shadow": resolved_mode == "shadow", "fail_open": False}
    observation: dict[str, Any] = {
        "phase": phase, "executed": executed, "attempt_id": attempt_id,
        "result_sig": result_sig, "work_fp": work_fp,
        "fp_complete": fp_complete,
    }
    if milestone:
        observation["milestone"] = milestone
    try:
        new_state, decision = guard_mod.evaluate(
            params.get(PROGRESS_GUARD_KEY), observation,
            {"mode": resolved_mode}, now=now)
    except Exception as exc:  # noqa: BLE001 - fail-open, caller logs incident
        params[GUARD_ERROR_KEY] = f"evaluate: {exc}"[:300]
        return {"action": "continue", "rule": None, "incident": None,
                "shadow": resolved_mode == "shadow", "fail_open": True,
                "brake_error": str(exc)[:200]}
    params[PROGRESS_GUARD_KEY] = new_state
    params.pop(GUARD_ERROR_KEY, None)
    return dict(decision)


def brake_record(params: dict[str, Any], decision: dict[str, Any],
                 *, mode: str, now: str | None = None) -> None:
    """Keep the advisory brake record next to the guard state."""
    rule = decision.get("rule")
    state = params.get(PROGRESS_GUARD_KEY)
    stopped = isinstance(state, dict) and isinstance(state.get("stopped"), dict) \
        and state["stopped"].get("rule") in ("G1", "G2")
    if rule:
        params[BRAKE_KEY] = {"rule": rule, "mode": mode, "at": now,
                             "shadow": bool(decision.get("shadow", True))}
    elif not stopped:
        params.pop(BRAKE_KEY, None)


async def record_async(source: Any, *, kind: str, **fields: Any) -> str | None:
    """Persist one incident; None when skipped or failed (never raises)."""
    try:
        from . import capture as capture_mod

        coll = resolve_incidents(source)
        if coll is None:
            return None
        component = str(fields.pop("component", COMPONENT))
        return await capture_mod.record_incident(
            coll, kind=kind, component=component, **fields)
    except Exception:  # noqa: BLE001 - never break the loop
        logger.warning("tier-s reliability capture failed", exc_info=True)
        return None


def utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def make_result_sig(phase: str, marker: str) -> str:
    """Phase plus normalized result marker (G1 comparison unit)."""
    try:
        from . import guard as guard_mod

        return guard_mod.result_signature(phase, marker)
    except Exception:  # noqa: BLE001 - fail-open: plain normalization
        clean = re.sub(r"\s+", " ", str(marker).strip())[:120]
        return f"{str(phase).strip()}:{clean}"


def tier_s_work_fp(repo: Any, base_sha: str) -> tuple[str, bool]:
    """Work fingerprint over a Tier S checkout, working tree included.

    ``HEAD`` may not move between attempts while the tree does, so the
    fingerprint hashes the base...tree diff plus untracked files (``.openceo/``
    excluded: it counts as activity, never as progress). Over the path/byte
    budget the fingerprint stays usable but ``complete`` is False, so G1 and
    G2 skip the attempt while G3 still applies.
    """
    try:
        from . import guard as guard_mod

        info = guard_mod.work_fingerprint(repo, base_sha)
        return str(info.get("fp") or "unknown"), bool(info.get("complete"))
    except Exception:  # noqa: BLE001 - fail-open: G1/G2 skip, G3 applies
        return "unknown", False


def classify_park(reason: str, detail: str,
                  resume_at: str | None) -> dict[str, Any]:
    """Map one Tier S park to executed/marker/capture (no I/O).

    Self-healing waits (a resume timer is set) never count and never
    capture: capacity/quota requeues leave the window untouched. Exhausted
    brakes capture ``brake_exhausted``; terminal parks without a more
    specific incident capture ``terminal_failure``. Waits without a timer
    count as stalled attempts but capture nothing.
    """
    text = str(detail or "")
    if resume_at is not None:
        return {"executed": False,
                "marker": f"waiting:{reason}:{text[:60]}".strip(),
                "capture": None}
    if reason == "needs_owner" and "scheduled_wait declined" in text:
        return {"executed": True, "marker": "brake_exhausted:scheduled_wait",
                "capture": {"kind": "brake_exhausted", "brake": "scheduled_wait"}}
    if reason == "needs_owner" and "short of quorum" in text:
        return {"executed": True, "marker": "brake_exhausted:quorum",
                "capture": {"kind": "brake_exhausted", "brake": "quorum"}}
    if "gates failed after" in text or "final gates failed" in text:
        return {"executed": True, "marker": "brake_exhausted:gates",
                "capture": {"kind": "brake_exhausted", "brake": "gates"}}
    if text.startswith("fix") and "gates failed" in text:
        return {"executed": True, "marker": "brake_exhausted:correction",
                "capture": {"kind": "brake_exhausted", "brake": "correction"}}
    if reason == "agent_blocked" and "builder failed:" in text:
        return {"executed": True, "marker": "brake_exhausted:provider",
                "capture": {"kind": "brake_exhausted", "brake": "provider"}}
    if reason in ("agent_blocked", "config", "transient", "failed"):
        return {"executed": True, "marker": f"terminal:{reason}",
                "capture": {"kind": "terminal_failure"}}
    return {"executed": True, "marker": f"waiting:{reason}:{text[:60]}".strip(),
            "capture": None}
