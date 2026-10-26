"""Provider error classification and retry authority (TASK-2026-00291).

SPEC §9.2 as code: every signal maps to one class with a default action.
Text matching extends (never duplicates) ``orchestration.provider_recovery``
and ``orchestration.review.classify_slot_error``, which stay coarser
(cooling_down/disabled) at the reviewer-slot level. ``not_found`` extends
the §9.2 table for withdrawn models (acceptance requires classifying a
real nonexistent-model error). Unclassified errors keep a sanitized
excerpt only; prompts, responses and credentials are never stored.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

from model_routing.live import redact_text

__all__ = [
    "COOLDOWN_BASE_S",
    "COOLDOWN_CAP_S",
    "MAX_TOTAL_HOPS",
    "Action",
    "Anomaly",
    "ClassifiedError",
    "ErrorClass",
    "RetryBudget",
    "classify_http",
    "classify_response",
    "classify_text",
    "classify_timeout",
    "cooldown_seconds",
    "sanitize_excerpt",
]

ErrorClass = Literal[
    "rate_limited",
    "daily_quota",
    "server_error",
    "auth",
    "forbidden",
    "route_not_authorized",
    "billing",
    "not_found",
    "tools_unsupported",
    "context_exceeded",
    "stream_cut",
    "non_idempotent_tool",
    "bad_response",
    "unclassified",
]

Action = Literal[
    "cooldown_deployment",
    "disable_provider",
    "retry_next",
    "disable_credential",
    "inspect",
    "block_route",
    "exclude_tools_profiles",
    "escalate_context",
    "rebuild_from_checkpoint",
    "inspect_before_retry",
    "fail_safe",
    "record_only",
]

Anomaly = Literal["stream_cut", "empty", "bad_json", "corrupt_tool_call_id"]

MAX_TOTAL_HOPS = 5
COOLDOWN_BASE_S = 60
COOLDOWN_CAP_S = 3600
_EXCERPT_LIMIT = 300


@dataclass(frozen=True)
class ClassifiedError:
    """One §9.2 signal with its default action; carries codes, never content."""

    error_class: ErrorClass
    action: Action
    retryable: bool
    retry_after_s: int | None = None
    excerpt: str = ""


def sanitize_excerpt(text: str, limit: int = _EXCERPT_LIMIT) -> str:
    """Collapsed, key-pattern-free, truncated extract for the registry."""
    return redact_text(" ".join((text or "").split()), ())[:limit]


def _parse_retry_after(value: str | None) -> int | None:
    """Delta-seconds form only; HTTP dates are ignored, never guessed."""
    if value is None:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return min(seconds, 86400)


def classify_http(
    status: int,
    *,
    retry_after: str | None = None,
    detail: str = "",
    provider: str | None = None,
    client: str | None = None,
) -> ClassifiedError:
    """Map an HTTP failure to its §9.2 class; unknown codes stay unclassified.

    A 403 on OpenCode Free from an external client is ``route_not_authorized``
    (addendum v1.2 §7): the route is blocked, never retried nor counted as
    quota. Without explicit ``provider``/``client`` context a 403 keeps the
    v1.1 ``forbidden`` meaning.
    """
    excerpt = sanitize_excerpt(detail)
    retry_after_s = _parse_retry_after(retry_after)
    if status == 429:
        return ClassifiedError("rate_limited", "cooldown_deployment", True, retry_after_s, excerpt)
    if status == 401:
        return ClassifiedError("auth", "disable_credential", False, None, excerpt)
    if status == 403:
        if provider == "opencode" and client == "external":
            return ClassifiedError("route_not_authorized", "block_route", False, None, excerpt)
        return ClassifiedError("forbidden", "inspect", False, None, excerpt)
    if status == 402:
        return ClassifiedError("billing", "block_route", False, None, excerpt)
    if status == 404:
        return ClassifiedError("not_found", "block_route", False, None, excerpt)
    if status == 400:
        # Bare 400s are ambiguous: only body-proven tools, not-found
        # ("not a valid model ID", live 2026-10-23) and context cases promote.
        text_probe = classify_text(detail or "")
        if text_probe.error_class in ("tools_unsupported", "not_found", "context_exceeded"):
            return text_probe
        return ClassifiedError("unclassified", "record_only", False, None, excerpt)
    if 500 <= status <= 599:
        return ClassifiedError("server_error", "retry_next", True, retry_after_s, excerpt)
    return ClassifiedError("unclassified", "record_only", False, None, excerpt)


_DAILY_QUOTA_MARKS = (
    "daily", "quota", "freeusagelimiterror", "provider.quota",
    "free tier", "free-tier", "freetier",
)

_BILLING_MARKS = (
    "billing", "402", "payment", "purchase credits", "buy credits",
    "no longer free", "free trial ended", "trial ended", "requires payment",
)

_AUTH_MARKS = (
    "unauthorized", "invalid api key", "invalid_api_key", "authentication failed",
    "subscription expired", "subscription has expired",
)

_NOT_FOUND_MARKS = (
    "model not found", "model_not_found", "modelnotfound",
    "does not exist", "no such model", "unknown model",
    "not a valid model",
)

_TOOLS_MARKS = (
    "tools not supported", "tool not supported", "does not support tools",
    "function calling", "function_calling", "invalid tool", "tool_choice",
    "tools are not", "unsupported tools",
)

_CONTEXT_MARKS = (
    "context", "too long", "maximum context", "tokens exceed",
    "token limit", "context_length", "input too large",
)

_TRANSPORT_MARKS = (
    "connection reset", "connection closed", "connection was closed",
    "socket connection", "econnreset", "econnrefused", "fetch failed",
    "timed out", "timeout", "etimedout",
)

_BAD_RESPONSE_MARKS = (
    "empty response", "tool_call_id", "tool call id", "invalid json",
    "json inconsisten", "unexpected end", "truncated",
)


def classify_text(text: str, *, responded: bool = False) -> ClassifiedError:
    """Classify free-form provider output; specific marks win over heuristics.

    ``responded`` distinguishes a transport failure before any output
    (retryable ``server_error``) from a cut stream (``stream_cut``: never
    blindly repeated, SPEC §9.2). Anything unmatched defers to
    ``transient_provider_error`` before falling back to ``unclassified``.
    """
    from orchestration.provider_recovery import transient_provider_error

    lowered = (text or "").lower()
    excerpt = sanitize_excerpt(text)
    if any(mark in lowered for mark in _DAILY_QUOTA_MARKS):
        return ClassifiedError("daily_quota", "disable_provider", False, None, excerpt)
    if any(mark in lowered for mark in _BILLING_MARKS):
        return ClassifiedError("billing", "block_route", False, None, excerpt)
    if any(mark in lowered for mark in _AUTH_MARKS):
        return ClassifiedError("auth", "disable_credential", False, None, excerpt)
    if any(mark in lowered for mark in _NOT_FOUND_MARKS):
        return ClassifiedError("not_found", "block_route", False, None, excerpt)
    if any(mark in lowered for mark in _TOOLS_MARKS):
        return ClassifiedError("tools_unsupported", "exclude_tools_profiles", False, None, excerpt)
    if any(mark in lowered for mark in _CONTEXT_MARKS):
        return ClassifiedError("context_exceeded", "escalate_context", False, None, excerpt)
    if any(mark in lowered for mark in _TRANSPORT_MARKS):
        if responded:
            return ClassifiedError("stream_cut", "rebuild_from_checkpoint", False, None, excerpt)
        return ClassifiedError("server_error", "retry_next", True, None, excerpt)
    if any(mark in lowered for mark in _BAD_RESPONSE_MARKS):
        return ClassifiedError("bad_response", "fail_safe", False, None, excerpt)
    if transient_provider_error(text or ""):
        return ClassifiedError("server_error", "retry_next", True, None, excerpt)
    return ClassifiedError("unclassified", "record_only", False, None, excerpt)


def classify_response(anomaly: Anomaly) -> ClassifiedError:
    """Classify a structurally anomalous answer (no HTTP signal involved)."""
    if anomaly == "stream_cut":
        return ClassifiedError("stream_cut", "rebuild_from_checkpoint", False)
    return ClassifiedError("bad_response", "fail_safe", False)


def classify_timeout(*, responded: bool) -> ClassifiedError:
    """A timeout before output retries; after output it rebuilds, never replays."""
    if responded:
        return ClassifiedError("stream_cut", "rebuild_from_checkpoint", False)
    return ClassifiedError("server_error", "retry_next", True)


@dataclass(frozen=True)
class RetryBudget:
    """Single retry authority: bounded hops, exponential backoff, jitter."""

    max_hops: int = MAX_TOTAL_HOPS
    base_s: float = 1.0
    cap_s: float = 60.0

    def allows(self, hops_used: int) -> bool:
        """Hop ``hops_used`` (0-based) may proceed while under the budget."""
        return 0 <= hops_used < self.max_hops

    def delay(self, hops_used: int, rng: random.Random | None = None) -> float:
        """Full-jitter backoff for the next hop; deterministic with ``rng``."""
        ceiling = min(self.cap_s, self.base_s * (2 ** max(0, hops_used)))
        source = rng if rng is not None else random
        return source.uniform(0, ceiling)


def cooldown_seconds(
    consecutive_failures: int,
    retry_after_s: int | None = None,
    *,
    base_s: int = COOLDOWN_BASE_S,
    cap_s: int = COOLDOWN_CAP_S,
) -> int:
    """Progressive deployment cooldown honoring the server's Retry-After."""
    progressive = min(cap_s, base_s * (2 ** max(0, consecutive_failures - 1)))
    if retry_after_s is not None:
        return max(retry_after_s, progressive)
    return progressive
