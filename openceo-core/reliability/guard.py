"""ProgressGuard: pure G1-G3 brake and work fingerprint (design v2 section 4).

Shared by the four execution loops (dev A/B/C, Tier S, generic agent,
automatico.py). `evaluate` is pure: the loops own Mongo, incidents and
checkpoints. `work_fingerprint` only runs read-only git commands, never
checkout, stash or reset.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

from .config import RELIABILITY_DEFAULTS

WINDOW_MAX = 8
MILESTONES_MAX = 256
FP_PREFIX = "sha256:"

_GUARD_DEFAULTS = RELIABILITY_DEFAULTS["guard"]

_EXCLUDED_DIRS = frozenset({
    ".git",
    ".openceo",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".hypothesis",
    "node_modules",
    ".venv",
    ".tox",
    "dist",
    "build",
})

_EXCLUDED_SUFFIXES = (".log",)

_WS = re.compile(r"\s+")

GENERIC_EMPTY_MARKER = "generic-agent:no-deliverables"
GENERIC_EMPTY_FINGERPRINT = FP_PREFIX + hashlib.sha256(
    GENERIC_EMPTY_MARKER.encode("utf-8")).hexdigest()

_UNAVAILABLE_FP = FP_PREFIX + "unavailable"


def empty_state(phase: str = "") -> dict[str, Any]:
    """Checkpoint-shaped guard state with an empty window."""
    return {
        "generation": 0,
        "phase": phase,
        "window": [],
        "stopped": None,
        "reset": None,
        "milestones": [],
    }


def result_signature(phase: str, result: str) -> str:
    """Phase plus whitespace-normalized result, the G1 comparison unit."""
    return f"{str(phase).strip()}:{_WS.sub(' ', str(result).strip())}"


def _coerce_state(raw: Any, phase: str) -> tuple[dict[str, Any], bool]:
    if not isinstance(raw, dict):
        return empty_state(phase), True
    recovered = False
    generation = raw.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) \
            or generation < 0:
        generation = 0
        recovered = True
    window: list[dict[str, Any]] = []
    for entry in raw.get("window") or []:
        if _is_entry(entry):
            window.append(dict(entry))
        else:
            recovered = True
    window = window[-WINDOW_MAX:]
    stopped = raw.get("stopped")
    if stopped is not None and not _is_stopped(stopped):
        stopped = None
        recovered = True
    reset = raw.get("reset")
    if reset is not None and not isinstance(reset, dict):
        reset = None
        recovered = True
    milestones = raw.get("milestones") or []
    if not isinstance(milestones, list) \
            or not all(isinstance(item, str) for item in milestones):
        milestones = [item for item in milestones
                      if isinstance(item, str)] \
            if isinstance(milestones, list) else []
        recovered = True
    return {
        "generation": generation,
        "phase": str(raw.get("phase") or phase or ""),
        "window": window,
        "stopped": dict(stopped) if isinstance(stopped, dict) else None,
        "reset": dict(reset) if isinstance(reset, dict) else None,
        "milestones": milestones[-MILESTONES_MAX:],
    }, recovered


def _is_entry(entry: Any) -> bool:
    return isinstance(entry, dict) \
        and isinstance(entry.get("attempt_id"), str) \
        and isinstance(entry.get("phase"), str) \
        and isinstance(entry.get("result_sig"), str) \
        and isinstance(entry.get("work_fp"), str) \
        and isinstance(entry.get("fp_complete"), bool)


def _is_stopped(stopped: Any) -> bool:
    return isinstance(stopped, dict) and stopped.get("rule") in ("G1", "G2")


def _coerce_policy(raw: Any) -> tuple[dict[str, Any], bool]:
    policy = dict(raw) if isinstance(raw, dict) else {}
    mode = policy.get("mode", "shadow")
    if mode not in ("off", "shadow", "enforce"):
        return _default_policy("shadow"), False
    return {
        "mode": mode,
        "same_work_attempts": _threshold(policy, "same_work_attempts"),
        "alternating_attempts": _threshold(policy, "alternating_attempts"),
        "long_phase_attempts": _threshold(policy, "long_phase_attempts"),
    }, True


def _threshold(policy: dict[str, Any], key: str) -> int:
    value = policy.get(key, _GUARD_DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) or value < 2:
        return int(_GUARD_DEFAULTS[key])
    return value


def _default_policy(mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "same_work_attempts": _GUARD_DEFAULTS["same_work_attempts"],
        "alternating_attempts": _GUARD_DEFAULTS["alternating_attempts"],
        "long_phase_attempts": _GUARD_DEFAULTS["long_phase_attempts"],
    }


def _continue(decision_shadow: bool, fail_open: bool) -> dict[str, Any]:
    return {"action": "continue", "rule": None, "incident": None,
            "shadow": decision_shadow, "fail_open": fail_open}


def evaluate(state: Any, observation: dict[str, Any],
             policy: dict[str, Any], *,
             now: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """One terminated attempt in, new checkpoint state plus brake decision.

    Never raises on corrupt input: it recovers to an empty state and lets
    the loop continue (fail-open). Rule evaluation is fail-open too: if
    the brake throws, the loop continues and the caller logs a
    reliability exception. Only executed sessions count; requeues by
    capacity or quota leave the window untouched, as does an observation
    without an explicit executed=True. The caller resolves
    policy["mode"] to one "off"/"shadow"/"enforce" string per producer.
    """
    obs = observation if isinstance(observation, dict) else {}
    phase = str(obs.get("phase") or "")
    parsed_policy, policy_ok = _coerce_policy(policy)
    mode = parsed_policy["mode"]
    current, recovered = _coerce_state(state, phase)
    fail_open = recovered or not policy_ok
    if not policy_ok or mode == "off":
        return current, _continue(False, True if not policy_ok else fail_open)
    if current["stopped"] is not None:
        rule = str(current["stopped"]["rule"])
        action = "stop" if mode == "enforce" else "would_stop"
        return current, {"action": action, "rule": rule, "incident": None,
                         "shadow": mode == "shadow", "fail_open": fail_open}
    if not obs.get("executed", False):
        return current, _continue(mode == "shadow", fail_open)
    milestone = obs.get("milestone")
    if isinstance(milestone, str) and milestone.strip():
        credited = current["milestones"]
        if milestone not in credited:
            return {
                "generation": current["generation"] + 1,
                "phase": phase or current["phase"],
                "window": [],
                "stopped": None,
                "reset": current["reset"],
                "milestones": (credited + [milestone])[-MILESTONES_MAX:],
            }, _continue(mode == "shadow", fail_open)
    attempt_id = obs.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        attempt_id = f"attempt-{current['generation']}-{len(current['window'])}"
    work_fp = obs.get("work_fp")
    fp_complete = bool(obs.get("fp_complete", False))
    if not isinstance(work_fp, str) or not work_fp:
        work_fp, fp_complete = f"unknown:{attempt_id}", False
    window = current["window"] + [{
        "attempt_id": attempt_id,
        "phase": phase,
        "result_sig": str(obs.get("result_sig") or ""),
        "work_fp": work_fp,
        "fp_complete": fp_complete,
    }]
    current = {**current, "phase": phase or current["phase"],
               "window": window[-WINDOW_MAX:]}
    try:
        rule = _match_rule(current["window"], phase, parsed_policy)
    except Exception:
        return current, _continue(mode == "shadow", True)
    if rule is None:
        return current, _continue(mode == "shadow", fail_open)
    severity = "normal" if rule == "G3" else "high"
    incident = {"kind": "stall", "severity": severity, "rule": rule,
                "shadow": mode == "shadow"}
    if mode == "shadow":
        action = "continue" if rule == "G3" else "would_stop"
        return current, {"action": action, "rule": rule, "incident": incident,
                         "shadow": True, "fail_open": fail_open}
    if rule == "G3":
        return current, {"action": "continue", "rule": rule,
                         "incident": incident, "shadow": False,
                         "fail_open": fail_open}
    current["stopped"] = {
        "rule": rule,
        "reason": _stop_reason(rule, phase, parsed_policy),
        "at": now,
    }
    return current, {"action": "stop", "rule": rule, "incident": incident,
                     "shadow": False, "fail_open": fail_open}


def _trailing_run(window: list[dict[str, Any]],
                  phase: str) -> list[dict[str, Any]]:
    run: list[dict[str, Any]] = []
    for entry in reversed(window):
        if entry["phase"] != phase:
            break
        run.append(entry)
    return run


def _match_rule(window: list[dict[str, Any]], phase: str,
                policy: dict[str, Any]) -> str | None:
    run = _trailing_run(window, phase)
    if len(run) >= policy["same_work_attempts"] and _is_same_work(
            run[:policy["same_work_attempts"]]):
        return "G1"
    if len(run) >= policy["alternating_attempts"] and _is_alternating(
            run[:policy["alternating_attempts"]]):
        return "G2"
    if len(run) >= policy["long_phase_attempts"]:
        return "G3"
    return None


def _is_same_work(entries: list[dict[str, Any]]) -> bool:
    if not all(entry["fp_complete"] for entry in entries):
        return False
    first = entries[0]
    return all(entry["result_sig"] == first["result_sig"]
               and entry["work_fp"] == first["work_fp"] for entry in entries)


def _is_alternating(entries: list[dict[str, Any]]) -> bool:
    if not all(entry["fp_complete"] for entry in entries):
        return False
    fps = [entry["work_fp"] for entry in entries]
    if len(set(fps)) != 2 or fps[0] == fps[1]:
        return False
    return all(fp == fps[index % 2] for index, fp in enumerate(fps))


def _stop_reason(rule: str, phase: str, policy: dict[str, Any]) -> str:
    if rule == "G1":
        count = policy["same_work_attempts"]
        return (f"G1 same_work: {count} terminated attempts in phase "
                f"'{phase}' with identical result and work fingerprint")
    count = policy["alternating_attempts"]
    return (f"G2 alternating: {count} terminated attempts in phase "
            f"'{phase}' alternating between 2 work fingerprints")


def reset_guard(state: Any, *, actor: str, reason: str,
                now: str | None = None) -> dict[str, Any]:
    """Owner (or authorized actor) re-arm: fresh generation, reason kept."""
    obs_phase = ""
    current, _ = _coerce_state(state, obs_phase)
    return {
        "generation": current["generation"] + 1,
        "phase": current["phase"],
        "window": [],
        "stopped": None,
        "reset": {"actor": actor, "at": now, "reason": reason},
        "milestones": current["milestones"],
    }


def generic_agent_fingerprint(deliverables: list[str],
                              evidence: str = "") -> str:
    """Work fingerprint of a validated AgentResult: deliverables + evidence.

    With no deliverables the fingerprint is constant by design.
    """
    items = sorted({str(item) for item in deliverables or [] if str(item)})
    if not items:
        return GENERIC_EMPTY_FINGERPRINT
    digest = hashlib.sha256()
    for item in items:
        digest.update(b"\x00" + item.encode("utf-8"))
    digest.update(b"\x00" + str(evidence or "").encode("utf-8"))
    return FP_PREFIX + digest.hexdigest()


def work_fingerprint(repo: str | Path, base_sha: str,
                     head_sha: str | None = None, *,
                     max_paths: int | None = None,
                     max_bytes: int | None = None) -> dict[str, Any]:
    """Dev/runner work fingerprint over a checkout, read-only git only.

    Over the limits the fingerprint stays usable but `complete` is False,
    so G1 and G2 skip the attempt while G3 still applies.
    """
    if max_paths is None:
        max_paths = int(_GUARD_DEFAULTS["fingerprint_max_paths"])
    if max_bytes is None:
        max_bytes = int(_GUARD_DEFAULTS["fingerprint_max_bytes"])
    root = _git_root(repo)
    if root is None or not base_sha:
        return _unavailable("not a git checkout or missing base_sha")
    raw_head = head_sha if head_sha else _git_output(root, "rev-parse", "HEAD")
    head = raw_head.decode().strip() if isinstance(raw_head, bytes) \
        else str(raw_head or "").strip()
    if not head:
        return _unavailable("cannot resolve HEAD")
    diff = _git_output(root, "diff", "--binary", base_sha, "--", ".")
    if diff is None:
        return _unavailable(f"cannot diff against base {base_sha[:12]}")
    files = _untracked_files(root)
    if files is None:
        return _unavailable("cannot list untracked files")
    hashed: list[tuple[str, str]] = []
    total_bytes = 0
    for relpath in files:
        if total_bytes > max_bytes or len(hashed) > max_paths:
            break
        content = _read_within(root, relpath)
        if content is None:
            continue
        total_bytes += len(content)
        hashed.append((relpath,
                       hashlib.sha256(content).hexdigest()))
    hashed.sort()
    digest = hashlib.sha256()
    digest.update(str(base_sha).encode("utf-8"))
    digest.update(b"\x00" + head.encode("utf-8"))
    digest.update(b"\x00" + hashlib.sha256(diff).hexdigest().encode("utf-8"))
    for relpath, hexdigest in hashed:
        digest.update(b"\x00" + relpath.encode("utf-8"))
        digest.update(b":" + hexdigest.encode("utf-8"))
    return {
        "fp": FP_PREFIX + digest.hexdigest(),
        "complete": len(hashed) <= max_paths and total_bytes <= max_bytes,
        "paths": len(hashed),
        "bytes": total_bytes,
        "head": head,
        "error": None,
    }


def _unavailable(error: str) -> dict[str, Any]:
    return {"fp": _UNAVAILABLE_FP, "complete": False, "paths": 0,
            "bytes": 0, "head": None, "error": error}


def _git_root(repo: str | Path) -> Path | None:
    out = _git_output(Path(repo), "rev-parse", "--show-toplevel")
    return Path(out.decode().strip()) if out is not None else None


def _git_output(root: Path, *args: str) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _untracked_files(root: Path) -> list[str] | None:
    out = _git_output(root, "ls-files", "--others", "--exclude-standard", "-z")
    if out is None:
        return None
    entries: list[str] = []
    for raw in out.split(b"\x00"):
        relpath = raw.decode("utf-8", errors="surrogateescape").strip()
        if not relpath or _is_excluded(relpath):
            continue
        entries.append(relpath)
    return entries


def _is_excluded(relpath: str) -> bool:
    lowered = relpath.lower()
    if lowered.endswith(_EXCLUDED_SUFFIXES):
        return True
    return any(part in _EXCLUDED_DIRS or part.endswith(".egg-info")
               for part in Path(relpath).parts)


def _read_within(root: Path, relpath: str) -> bytes | None:
    try:
        target = (root / relpath).resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    try:
        return target.read_bytes() if target.is_file() else None
    except OSError:
        return None

