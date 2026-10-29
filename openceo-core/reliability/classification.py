"""Ordered attribution rules A1-A10 (design v2 section 6.2).

The first matching rule wins and its id stays on the incident. Diagnosis may
later confirm or correct the attribution; agent free text never changes it.
Operates on the raw incident, before sanitize_incident redacts it.

Provider brakes are checked before task brakes, so review_quorum stays
ENVIRONMENT; transient/quota name Tier S waits (WAIT_REASONS).
"""

from __future__ import annotations

from orchestration.provider_recovery import transient_provider_error

from .models import Attribution, Component, IncidentKind
from .sanitize import is_in_app_frame

REASON_CODE_MAP_VERSION = 1

_ENVIRONMENT_REASONS = (
    "auth", "unauthorized", "forbidden", "quota", "rate_limit", "rate limit",
    "billing", "subscription", "payment", "429",
)

_PROJECT_REASONS = ("pytest", "unittest", "test_", "_test", "test-")

_PROVIDER_BRAKES = ("provider", "quorum", "scheduled_wait", "recovery",
                    "resume", "retry", "transient", "quota")

_TASK_BRAKES = ("gate", "review", "correction", "fix")

_OOM_MARKERS = ("oom", "out of memory", "sigkill", "signal 9",
                "exit 137", "killed")

_CONTINUE_MARKERS = ("TICKET_CONTINUE", "CONTINUE")


def _innermost_in_app(frames: list[dict[str, str]]) -> dict[str, str] | None:
    in_app = [frame for frame in frames or []
              if is_in_app_frame(str(frame.get("file") or ""))]
    return in_app[-1] if in_app else None


def classify(kind: IncidentKind,
             exc_type: str | None = None,
             exc_text: str | None = None,
             frames: list[dict[str, str]] | None = None,
             reason_code: str | None = None,
             brake: str | None = None,
             result_signature: str | None = None,
             service_result: str | None = None,
             free_text: str | None = None) -> tuple[Attribution, str]:
    del free_text
    if isinstance(kind, str):
        try:
            kind = IncidentKind(kind)
        except ValueError:
            return Attribution.UNKNOWN, "A10"
    if kind is IncidentKind.INVARIANT_VIOLATION:
        return Attribution.ENGINE, "A1"
    if kind is IncidentKind.EXCEPTION:
        probe = f"{exc_type or ''} {exc_text or ''} {reason_code or ''}"
        if transient_provider_error(probe):
            return Attribution.ENVIRONMENT, "A2"
        if _innermost_in_app(frames or []) is not None:
            return Attribution.ENGINE, "A3"
        return Attribution.UNKNOWN, "A10"
    if kind is IncidentKind.DEADLINE_MISSED:
        return Attribution.ENGINE, "A4"
    if kind is IncidentKind.UNCLEAN_EXIT:
        lowered = (service_result or "").lower()
        if any(marker in lowered for marker in _OOM_MARKERS):
            return Attribution.ENVIRONMENT, "A5"
        return Attribution.ENGINE, "A5"
    if kind is IncidentKind.BRAKE_EXHAUSTED:
        lowered = (brake or "").lower()
        if any(marker in lowered for marker in _PROVIDER_BRAKES):
            return Attribution.ENVIRONMENT, "A6"
        if any(marker in lowered for marker in _TASK_BRAKES):
            return Attribution.TASK, "A7"
        return Attribution.UNKNOWN, "A10"
    if kind is IncidentKind.STALL:
        signature = (result_signature or "").upper()
        if any(marker in signature for marker in _CONTINUE_MARKERS):
            return Attribution.ENGINE, "A8"
        return Attribution.UNKNOWN, "A10"
    if kind is IncidentKind.TERMINAL_FAILURE:
        lowered = (reason_code or "").lower()
        if any(marker in lowered for marker in _ENVIRONMENT_REASONS):
            return Attribution.ENVIRONMENT, "A9"
        if any(marker in lowered for marker in _PROJECT_REASONS):
            return Attribution.PROJECT, "A9"
        return Attribution.UNKNOWN, "A9"
    return Attribution.UNKNOWN, "A10"


def reason_code_attribution(reason_code: str | None) -> Attribution | None:
    lowered = (reason_code or "").lower()
    if any(marker in lowered for marker in _ENVIRONMENT_REASONS):
        return Attribution.ENVIRONMENT
    if any(marker in lowered for marker in _PROJECT_REASONS):
        return Attribution.PROJECT
    return None


ENGINE_ONLY_COMPONENTS = frozenset({Component.RELIABILITY})
