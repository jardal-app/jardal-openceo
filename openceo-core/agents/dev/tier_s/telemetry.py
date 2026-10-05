"""Minimal telemetry from F1 (design §18).

Every session records its cost; every ticket records planned-vs-real sizes,
gate retries and plan-review verdicts. Persisted on the run document, so no
new collection is needed. Calibration stays manual (§18: "tras unos 30
tickets, a mano").
"""

from __future__ import annotations

from typing import Any

from agents.dev.tier_s.models import SessionMetric


def blank_ticket_metrics() -> dict[str, Any]:
    return {
        "tier": None,
        "origin": None,
        "files_planned": None,
        "files_real": None,
        "lines_planned": None,
        "lines_real": None,
        "gate_retries": 0,
        "plan_review": {"observations": 0, "accepted": 0, "rejected": 0,
                       "deferred": 0, "no_response": 0},
        "sessions": [],
    }


def record_session(metrics: dict[str, Any], session: SessionMetric) -> None:
    metrics.setdefault("sessions", []).append(session.to_dict())


def record_plan_review(
    metrics: dict[str, Any],
    observations: int,
    accepted: int,
    rejected: int,
    deferred: int,
    no_response: int,
) -> None:
    metrics["plan_review"] = {
        "observations": observations,
        "accepted": accepted,
        "rejected": rejected,
        "deferred": deferred,
        "no_response": no_response,
    }


def record_sizes(
    metrics: dict[str, Any],
    files_planned: int | None,
    lines_planned: int | None,
    files_real: int,
    lines_real: int,
) -> None:
    metrics["files_planned"] = files_planned
    metrics["lines_planned"] = lines_planned
    metrics["files_real"] = files_real
    metrics["lines_real"] = lines_real


def bump_gate_retries(metrics: dict[str, Any]) -> None:
    metrics["gate_retries"] = int(metrics.get("gate_retries", 0)) + 1
