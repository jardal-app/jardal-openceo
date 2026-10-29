"""Reliability hunter global configuration (design v2 section 14).

Global, not per-project: engine Problems cross projects. Values below are
the initial thresholds; any change is an agent decision reopenable by the
Owner. Code invariants (hunter tickets always autorun=false, admission and
route gates, engine scope without business text, tool-less diagnosis, no
self-diagnosis of the reliability component) are structural and no
configuration key can change them.
"""

from __future__ import annotations

from typing import Any

RELIABILITY_DEFAULTS: dict[str, Any] = {
    "capture": True,
    "guard": {
        "mode": {"runner": "enforce", "dev_session": "shadow",
                 "tier_s": "shadow", "generic_agent": "shadow"},
        "same_work_attempts": 3,
        "alternating_attempts": 4,
        "long_phase_attempts": 8,
        "fingerprint_max_paths": 100,
        "fingerprint_max_bytes": 2097152,
    },
    "deadlines": {
        "attempt_grace_min_seconds": 300,
        "attempt_grace_ratio": 0.2,
        "resume_overdue_seconds": 600,
    },
    "triage": {
        "engine_normal": {"occurrences": 3, "tickets": 2},
        "project_or_unknown": {"occurrences": 3, "tickets": 2},
        "task": {"tickets": 5, "window_days": 14},
        "release_storm": {"new_problems": 3, "window_hours": 24},
        "max_incidents_per_fingerprint_hour": 20,
    },
    "diagnosis": {
        "enabled": False,
        "max_concurrent": 1,
        "max_starts_per_hour": 3,
        "max_starts_per_day": 20,
        "max_per_problem": 3,
        "processes_per_diagnosis": 2,
        "timeout_seconds": 300,
        "claim_seconds": 600,
        "max_input_bytes": 65536,
        "max_input_tokens_estimated": 12000,
        "max_output_tokens": 2000,
    },
    "tickets": {
        "enabled": False,
        "max_pending_owner": 5,
    },
    "endpoint": {
        "max_event_bytes": 32768,
        "max_per_minute": 120,
    },
    "verification": {
        "min_exposures": 20,
        "max_exposures": 500,
        "default_baseline_rate": 0.05,
    },
    "routes_gate": {
        "enabled": False,
    },
    "recovery": {
        "reconcile_business_state": False,
    },
    "spool_max_bytes": 10485760,
    "retention_days": {"incidents": 90, "exposures": 180},
    "invariants": {
        "mode": {"INV-001": "record", "INV-002": "record",
                 "INV-003": "record", "INV-004": "record",
                 "INV-005": "record"},
    },
}

GUARD_MODES = frozenset({"off", "shadow", "enforce"})

GUARD_PRODUCERS = frozenset({"runner", "dev_session", "tier_s", "generic_agent"})

HUNTER_TICKET_AUTORUN = False

SELF_DIAGNOSIS_EXCLUDED = frozenset({"reliability"})


def validate_reliability_config(block: Any) -> list[str]:
    if not isinstance(block, dict):
        return ["reliability: must be a mapping"]
    errors: list[str] = []
    for key in block:
        if key not in RELIABILITY_DEFAULTS:
            errors.append(f"reliability.{key}: unknown key (fail-closed)")
    for key, spec in RELIABILITY_DEFAULTS.items():
        if key not in block:
            continue
        errors.extend(_validate_value(block[key], spec, f"reliability.{key}"))
    return errors


def _validate_value(value: Any, spec: Any, label: str) -> list[str]:
    if isinstance(spec, bool):
        if not isinstance(value, bool):
            return [f"{label}: must be true or false"]
        return []
    if isinstance(spec, int):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return [f"{label}: must be a non-negative integer"]
        return []
    if isinstance(spec, float):
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not 0 <= float(value) <= 1:
            return [f"{label}: must be a ratio between 0 and 1"]
        return []
    if isinstance(spec, str):
        if not isinstance(value, str) or not value:
            return [f"{label}: must be a non-empty string"]
        return []
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return [f"{label}: must be a mapping"]
        if label == "reliability.guard.mode":
            return _validate_guard_mode(value)
        if label == "reliability.invariants.mode":
            return _validate_invariant_mode(value)
        errors: list[str] = []
        for key in value:
            if key not in spec:
                errors.append(f"{label}.{key}: unknown key (fail-closed)")
        for key, sub in spec.items():
            if key in value:
                errors.extend(_validate_value(value[key], sub, f"{label}.{key}"))
        return errors
    return [f"{label}: unsupported default shape"]


def _validate_guard_mode(value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for producer, mode in value.items():
        if producer not in GUARD_PRODUCERS:
            errors.append(
                f"reliability.guard.mode.{producer}: unknown producer (fail-closed)")
        elif mode not in GUARD_MODES:
            errors.append(
                f"reliability.guard.mode.{producer}: must be one of "
                f"{sorted(GUARD_MODES)}")
    return errors


def _validate_invariant_mode(value: dict[str, Any]) -> list[str]:
    from .invariants import INVARIANTS, INVARIANT_MODES

    errors: list[str] = []
    for invariant_id, mode in value.items():
        if invariant_id not in INVARIANTS:
            errors.append(
                f"reliability.invariants.mode.{invariant_id}: unknown "
                "invariant (fail-closed)")
        elif mode not in INVARIANT_MODES:
            errors.append(
                f"reliability.invariants.mode.{invariant_id}: must be one of "
                f"{sorted(INVARIANT_MODES)}")
    return errors


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_dict(out[key], value)
        else:
            import copy

            out[key] = copy.deepcopy(value)
    return out


def get_reliability_config(merged: dict[str, Any]) -> dict[str, Any]:
    import copy

    out = copy.deepcopy(RELIABILITY_DEFAULTS)
    block = merged.get("reliability")
    if isinstance(block, dict):
        out = _deep_merge_dict(out, copy.deepcopy(block))
    return out
