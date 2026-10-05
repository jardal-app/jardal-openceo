"""Tier S engine: deterministic orchestrator for one ticket.

Implements ``docs/work/TIER_S_DESIGN_FINAL.md`` phases F1–F7: telemetry and
gates (F1), the single-ticket cycle (F2), review rounds (F3), the findings
inbox (F4), the F6 objective flows and F7 knowledge. One ticket = one cycle
= one builder session, no subplans. The old subplan engine (``consensus.py``)
was removed; this package is the only Tier S.

Simple tiers (F5) live in the dispatch layer (``agents.dev.session``), which
shares this package's gates, locks, branches, inbox and objective flows.
"""

from agents.dev.tier_s.models import (
    BUDGETS_DEFAULTS,
    PHASES,
    REVIEW_DEFAULTS,
    RUN_STATUSES,
    SIGNALS,
    TIMEOUTS_DEFAULTS,
    WAIT_REASONS,
    WAITING_DEFAULTS,
    GateResult,
    GateSpec,
)
from agents.dev.tier_s.cycle import CycleOutcome, TierSCycle

__all__ = [
    "BUDGETS_DEFAULTS",
    "PHASES",
    "REVIEW_DEFAULTS",
    "RUN_STATUSES",
    "SIGNALS",
    "TIMEOUTS_DEFAULTS",
    "WAIT_REASONS",
    "WAITING_DEFAULTS",
    "CycleOutcome",
    "GateResult",
    "GateSpec",
    "TierSCycle",
]
