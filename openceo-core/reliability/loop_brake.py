"""Attempt-boundary capture and ProgressGuard for dev A/B/C and generic agent.

Design v2 sections 4.5 and 5.2 (epic TASK-2026-00365, ticket TASK-2026-00375).
Shared by ``agents/dev/session.py`` and ``orchestration/generic_agent.py``;
Tier S has its own child (TASK-2026-00376) and never imports this module.

Never raises: guard failures fail open (the loop continues) and capture
failures only log. Mock or synthetic stores never capture: without a real
incidents collection the helpers record the guard state and skip the
incident (fail-open skip). With a real collection down, ``capture.py``
spools as usual.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEV_PRODUCER = "dev_session"
GENERIC_PRODUCER = "generic_agent"

BRAKE_PROVIDER = "provider"
BRAKE_QUORUM = "quorum"
BRAKE_GATES = "gates"
BRAKE_CORRECTION = "correction"

MILESTONE_GATE_FIXED = "gate-fixed"
MILESTONE_REVIEW_REPORT = "review-report"
MILESTONE_PHASE_CLOSED = "phase-closed"
MILESTONE_FINDING_RESOLVED = "finding-resolved"
MILESTONE_AGENT_HECHO = "agent-hecho"

PROGRESS_GUARD_KEY = "progress_guard"
MILESTONE_KEY = "_reliability_milestones"
CAPTURE_HINT_KEY = "_reliability_capture"
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


def guard_mode(producer: str) -> str:
    """Enforce by default: a stopped loop wakes the mechanic (TASK-2026-00510)."""
    try:
        from .config import RELIABILITY_DEFAULTS

        mode = RELIABILITY_DEFAULTS["guard"]["mode"].get(producer)
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


def take_milestones(params: Any) -> list[str]:
    """Consume the orchestrator-emitted milestone hints (§4.3)."""
    if not isinstance(params, dict):
        return []
    raw = params.pop(MILESTONE_KEY, [])
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def take_capture_hint(params: Any) -> dict[str, Any] | None:
    """Consume the explicit capture hint left at a decision point."""
    if not isinstance(params, dict):
        return None
    hint = params.pop(CAPTURE_HINT_KEY, None)
    return dict(hint) if isinstance(hint, dict) else None


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
    resolved_mode = mode or guard_mode(producer)
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


async def record_async(source: Any, *, kind: str, component: str,
                       **fields: Any) -> str | None:
    """Persist one incident; None when skipped or failed (never raises)."""
    try:
        from . import capture as capture_mod

        coll = resolve_incidents(source)
        if coll is None:
            return None
        return await capture_mod.record_incident(
            coll, kind=kind, component=component, **fields)
    except Exception:  # noqa: BLE001 - never break the loop
        logger.warning("reliability loop capture failed", exc_info=True)
        return None


def record_sync(source: Any, *, kind: str, component: str,
                **fields: Any) -> str | None:
    """Blocking variant for sync orchestrator paths; never raises."""
    try:
        from . import capture as capture_mod

        coll = resolve_incidents(source)
        if coll is None:
            return None
        return capture_mod.record_incident_sync(
            coll, kind=kind, component=component, **fields)
    except Exception:  # noqa: BLE001 - never break the loop
        logger.warning("reliability loop capture failed", exc_info=True)
        return None


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_result_sig(phase: str, marker: str) -> str:
    """Phase plus normalized result marker (G1 comparison unit)."""
    try:
        from . import guard as guard_mod

        return guard_mod.result_signature(phase, marker)
    except Exception:  # noqa: BLE001 - fail-open: plain normalization
        import re

        clean = re.sub(r"\s+", " ", str(marker).strip())[:120]
        return f"{str(phase).strip()}:{clean}"


def _commit_diff_fp(repo: Any, base_sha: str, head_sha: str) -> str | None:
    """Fingerprint of committed branch work; None when unresolvable."""
    try:
        from . import guard as guard_mod

        prefix = str(guard_mod.FP_PREFIX)
    except Exception:  # noqa: BLE001 - fail-open plain prefix
        prefix = "fp:"
    try:
        start = Path(str(repo))
        cwd = start if start.is_dir() else start.parent
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=60, check=False)
        if top.returncode != 0:
            return None
        diff = subprocess.run(
            ["git", "diff", "--binary", str(base_sha), str(head_sha),
             "--", "."],
            cwd=top.stdout.decode().strip(), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=60, check=False)
        if diff.returncode != 0:
            return None
        digest = hashlib.sha256()
        digest.update(str(base_sha).encode("utf-8"))
        digest.update(b"\x00" + str(head_sha).encode("utf-8"))
        digest.update(
            b"\x00" + hashlib.sha256(diff.stdout).hexdigest().encode())
        return prefix + digest.hexdigest()
    except Exception:
        logger.debug("commit work fingerprint unresolvable (fallback)",
                     exc_info=True)
        return None


def dev_work_fp(repo: Any, base_sha: str,
                head_sha: str | None = None) -> tuple[str, bool]:
    """Work fingerprint over a dev checkout; unknown when unresolvable.

    A checkpoint head hashes the committed base...head diff, surviving
    the release checkout to base. Without it the tree is hashed as before.
    """
    if head_sha:
        fp = _commit_diff_fp(repo, base_sha, head_sha)
        if fp is not None:
            return fp, True
    try:
        from . import guard as guard_mod

        info = guard_mod.work_fingerprint(repo, base_sha)
        return str(info.get("fp") or "unknown"), bool(info.get("complete"))
    except Exception:  # noqa: BLE001 - fail-open: G1/G2 skip, G3 applies
        return "unknown", False


def generic_work_fp(deliverables: Any, evidence: str = "") -> tuple[str, bool]:
    """Work fingerprint of a validated AgentResult; constant when empty."""
    try:
        from . import guard as guard_mod

        items = deliverables if isinstance(deliverables, list) else []
        fp = guard_mod.generic_agent_fingerprint(items, str(evidence or ""))
        return fp, bool(items)
    except Exception:  # noqa: BLE001 - fail-open
        return "unknown", False
