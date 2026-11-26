"""Deterministic wedge probes for exhausted parks (TASK-2026-00951).

An exhausted ``blocked`` park waits for the Owner signal by design (ticket
#654), but a wedge that cleared on its own strands the mission until a
human notices (live: ~36h on OBJ-B397EB1A). Probes re-check wedges a
machine can verify, so the recovery lane auto-resumes only on positive
proof; every other park keeps its Owner wait.

Budgets (both recorded on the objective ``auto_resume`` field, outside
``recovery`` so re-parks never drop them):
- a probe runs at most once per ``PROBE_COOLDOWN_MINUTES`` per objective;
- an automatic resume happens at most once per
  ``AUTO_RESUME_MIN_INTERVAL_HOURS`` per objective; a re-park lands
  exhausted-quiet again and re-fires the park notify channels.
"""
from __future__ import annotations

import os
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

__all__ = [
    "AUTO_RESUME_MIN_INTERVAL_HOURS",
    "PROBE_COOLDOWN_MINUTES",
    "PROBE_TIMEOUT_S",
    "probe_agent_browser",
    "probe_allowed",
    "record_probe",
    "resume_allowed",
]

#: Per-command timeout: a local open/snapshot/close takes seconds.
PROBE_TIMEOUT_S = 30
#: Min gap between probes of one objective (lane ticks ~10 min).
PROBE_COOLDOWN_MINUTES = 60
#: Min gap between automatic resumes of one objective.
AUTO_RESUME_MIN_INTERVAL_HOURS = 24


def _now(now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _parse_instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _auto_resume(objective: Mapping[str, Any]) -> dict[str, Any]:
    record = objective.get("auto_resume")
    return dict(record) if isinstance(record, Mapping) else {}


def probe_allowed(objective: Mapping[str, Any], *,
                  now: datetime | None = None) -> bool:
    """True when no probe ran inside the cooldown window."""
    last = _parse_instant(_auto_resume(objective).get("last_probe_at"))
    return last is None or _now(now) - last >= timedelta(
        minutes=PROBE_COOLDOWN_MINUTES)


def resume_allowed(objective: Mapping[str, Any], *,
                   now: datetime | None = None) -> bool:
    """True when no automatic resume ran inside the interval window."""
    last = _parse_instant(_auto_resume(objective).get("at"))
    return last is None or _now(now) - last >= timedelta(
        hours=AUTO_RESUME_MIN_INTERVAL_HOURS)


def record_probe(objective: Mapping[str, Any], *, ok: bool, resumed: bool,
                 now: datetime | None = None) -> dict[str, Any]:
    """Next ``auto_resume`` record after a probe (pure)."""
    moment = _now(now)
    record = _auto_resume(objective)
    record["last_probe_at"] = moment.isoformat()
    record["last_probe_ok"] = bool(ok)
    if resumed:
        record["at"] = moment.isoformat()
        try:
            record["count"] = int(record.get("count") or 0) + 1
        except (TypeError, ValueError):
            record["count"] = 1
    return record


def probe_agent_browser(
    *,
    timeout_s: int = PROBE_TIMEOUT_S,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Smoke agent-browser: open/snapshot/close about:blank (no net, no tokens).

    Runs in a dedicated session so it never touches another agent's page.
    ``ok`` needs open + snapshot at exit 0; close is best-effort.
    """
    run = runner if runner is not None else subprocess.run
    session = f"openceo-probe-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    env = {**os.environ, "AGENT_BROWSER_SESSION": session}
    started = time.monotonic()

    def _step(*argv: str) -> subprocess.CompletedProcess[str]:
        return run(list(argv), env=env, capture_output=True, text=True,
                   timeout=timeout_s)

    try:
        opened = _step("agent-browser", "open", "about:blank")
        if opened.returncode != 0:
            return {"ok": False, "reason": _short(
                f"open exit {opened.returncode}: {opened.stderr.strip()}")}
        snap = _step("agent-browser", "snapshot", "-i")
        if snap.returncode != 0:
            return {"ok": False, "reason": _short(
                f"snapshot exit {snap.returncode}: {snap.stderr.strip()}")}
        return {"ok": True, "elapsed_s": round(time.monotonic() - started, 1)}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "reason": f"probe timeout after {timeout_s}s: "
                                       f"{' '.join(exc.cmd or [])}"[:300]}
    except OSError as exc:
        return {"ok": False,
                "reason": f"probe cannot run agent-browser: {exc}"[:300]}
    except Exception as exc:  # noqa: BLE001 - recorded, never silent
        return {"ok": False, "reason": f"probe error: {exc}"[:300]}
    finally:
        try:
            _step("agent-browser", "close")
        except Exception:  # noqa: BLE001, S110 - best-effort cleanup
            pass


def _short(text: str, limit: int = 300) -> str:
    return text.strip()[:limit] or "agent-browser failed with no output"
