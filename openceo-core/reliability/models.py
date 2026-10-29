"""Reliability hunter base models (design v2 sections 5.1, 13.2).

Pure enums and constants shared by every later child of epic
TASK-2026-00365. No I/O.
"""

from __future__ import annotations

from enum import Enum

SCHEMA_VERSION = 1


class IncidentKind(str, Enum):
    EXCEPTION = "exception"
    TERMINAL_FAILURE = "terminal_failure"
    BRAKE_EXHAUSTED = "brake_exhausted"
    STALL = "stall"
    INVARIANT_VIOLATION = "invariant_violation"
    DEADLINE_MISSED = "deadline_missed"
    UNCLEAN_EXIT = "unclean_exit"
    ESCAPE = "escape"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"


class Component(str, Enum):
    DISPATCHER = "dispatcher"
    DEV_SESSION = "dev_session"
    TIER_S = "tier_s"
    GENERIC_AGENT = "generic_agent"
    RUNNER = "runner"
    REVIEW = "review"
    GATES = "gates"
    PROVIDER = "provider"
    BUSINESS_SYNC = "business_sync"
    OBJECTIVES = "objectives"
    CEO_LOOP = "ceo_loop"
    CORE_PROCESS = "core_process"
    RELIABILITY = "reliability"


class Attribution(str, Enum):
    ENGINE = "engine"
    ENVIRONMENT = "environment"
    PROJECT = "project"
    TASK = "task"
    UNKNOWN = "unknown"


class ProblemStatus(str, Enum):
    OPEN = "open"
    DIAGNOSING = "diagnosing"
    DIAGNOSED = "diagnosed"
    NEEDS_SPLIT = "needs_split"
    TICKETED = "ticketed"
    FIX_MERGED = "fix_merged"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    REGRESSED = "regressed"
    NO_ACTION = "no_action"
    REJECTED = "rejected"
    MUTED = "muted"
    SUPERSEDED = "superseded"


_DEFAULT_SEVERITY: dict[IncidentKind, Severity] = {
    IncidentKind.TERMINAL_FAILURE: Severity.NORMAL,
    IncidentKind.BRAKE_EXHAUSTED: Severity.NORMAL,
    IncidentKind.STALL: Severity.HIGH,
    IncidentKind.INVARIANT_VIOLATION: Severity.CRITICAL,
    IncidentKind.DEADLINE_MISSED: Severity.HIGH,
    IncidentKind.UNCLEAN_EXIT: Severity.HIGH,
    IncidentKind.ESCAPE: Severity.NORMAL,
}


def default_severity(kind: IncidentKind, *, motor_frame: bool = False,
                     guard_rule: str | None = None) -> Severity:
    if kind is IncidentKind.EXCEPTION:
        return Severity.HIGH if motor_frame else Severity.NORMAL
    if kind is IncidentKind.STALL and (guard_rule or "").strip().upper() == "G3":
        return Severity.NORMAL
    return _DEFAULT_SEVERITY[kind]
