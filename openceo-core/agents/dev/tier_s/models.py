"""Shared value objects for the Tier S engine (design §13, §15, §17).

Pure data, no I/O. All budgets and timeouts are the design's initial values;
telemetry from real cycles calibrates them by hand (§18).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


#: Agent → orchestrator signals (design §15.7). Only the orchestrator emits
#: TICKET_COMPLETE; agents never decide a close.
SIGNALS: dict[str, str] = {
    "plan_ready": "PLAN_READY",
    "objective_needed": "OBJECTIVE_NEEDED",
    "plan_responded": "PLAN_RESPONDED",
    "build_done": "BUILD_DONE",
    "fix_done_prefix": "FIX_DONE_",
    "review_done_prefix": "REVIEW_DONE_",
    "tickets_ready": "TICKETS_READY",  # F6
    "closure_ready": "CLOSURE_READY",  # F6
    "ticket_complete": "TICKET_COMPLETE",
    "objective_closed": "OBJECTIVE_CLOSED",  # F6
}

#: Run lifecycle (design §15.3).
RUN_STATUSES: tuple[str, ...] = (
    "running", "waiting", "completed", "failed", "canceled", "converted",
)

#: Typed wait reasons (design §14.2). ``review_quorum`` is recorded here but
#: only reachable once F3 wires review rounds.
WAIT_REASONS: tuple[str, ...] = (
    "routing_missing",
    "config",
    "quota",
    "transient",
    "review_quorum",
    "needs_owner",
)

#: Cycle phases (design §15.3).
PHASES: tuple[str, ...] = (
    "planning",
    "plan_review",
    "building",
    "round1",  # F3
    "round2",  # F3
    "arbitration",  # F3
    "closing",
)

#: Size and retry budgets (design §13, §17.1).
BUDGETS_DEFAULTS: dict[str, Any] = {
    "files_per_ticket": 8,
    "lines_per_ticket": 400,
    "hard_brake_factor": 2,
    "gate_retries": 2,
    "escalate_after": 2,  # F5
    "capsule_tokens": 2000,  # F6
    "handoff_words": 300,
    "agents_md_tokens": 3000,  # F7a
}

#: Waiting behavior (design §14.2, §17.1).
WAITING_DEFAULTS: dict[str, Any] = {
    "mid_cycle_hold_minutes": 30,
    "backoff_minutes": [15, 30, 60],
}

#: Review rounds policy (design §7.2, §17.1).
REVIEW_DEFAULTS: dict[str, Any] = {
    "round1_parallel": 3,
    "round2_parallel": 2,
    "quorum": 2,
    "distinct_by": "family",
    "min_available_families": 3,
    "max_rounds": 2,
    "reviewer_timeout_minutes": 15,
}

#: Per-role timeouts in minutes (design §17.1, initial).
TIMEOUTS_DEFAULTS: dict[str, int] = {
    "planner": 20,
    "objective_planner": 15,  # F6
    "plan_review": 15,
    "build": 60,
    "fix": 30,  # F3
    "arbiter": 10,  # F3
    "closer": 15,  # F6
}


@dataclass(frozen=True)
class GateSpec:
    """One project gate (design §11, §17.2)."""

    name: str
    cmd: str
    timeout_minutes: int = 15
    required: bool = True
    baseline_red: str = "fail"  # or "ignore"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "GateSpec":
        if not isinstance(raw, Mapping):
            raise ValueError("gate must be a mapping")
        name = str(raw.get("name", "")).strip()
        cmd = str(raw.get("cmd", "")).strip()
        if not name or not cmd:
            raise ValueError("gate needs 'name' and 'cmd'")
        try:
            timeout = int(raw.get("timeout_minutes", 15))
        except (TypeError, ValueError):
            raise ValueError(f"gate {name!r}: bad timeout_minutes")
        if timeout <= 0:
            raise ValueError(f"gate {name!r}: bad timeout_minutes")
        return cls(
            name=name,
            cmd=cmd,
            timeout_minutes=timeout,
            required=bool(raw.get("required", True)),
            baseline_red=str(raw.get("baseline_red", "fail")).strip() or "fail",
        )


@dataclass(frozen=True)
class GateResult:
    """Outcome of one gate execution, recorded by the orchestrator."""

    name: str
    status: str  # "pass" | "fail" | "timeout" | "error" | "skipped"
    required: bool
    elapsed_s: float
    output_tail: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "elapsed_s": round(self.elapsed_s, 3),
            "output_tail": self.output_tail,
            "detail": self.detail,
        }


@dataclass
class WaitState:
    """Typed wait (design §14.2): reason, detail, optional resume."""

    reason: str
    detail: str = ""
    resume_at: str | None = None  # ISO-8601 UTC, when the scheduler resumes

    def to_dict(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail,
                "resume_at": self.resume_at}


@dataclass
class SessionMetric:
    """Per-session telemetry (design §18). Usage is best-effort per account."""

    role: str
    slot: str
    model: str
    duration_s: float
    exit_code: int | None
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "slot": self.slot,
            "model": self.model,
            "duration_s": round(self.duration_s, 3),
            "exit_code": self.exit_code,
            "usage": dict(self.usage),
        }
