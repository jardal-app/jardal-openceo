from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone


PROVIDER_RETRY_DELAYS = (300, 900, 1800)
PROVIDER_RESUME_PROMPT = (
    "Resume the same request from the current checkpoint. Inspect the ticket, "
    "files and confirmed effects before acting; do not repeat completed actions "
    "or advance to another ticket. The pending request is:\n\n"
)


def model_responded(text: str, model: str) -> bool:
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("payload_type") != "run.usage":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        for usage in payload.get("models") or []:
            if (isinstance(usage, dict) and usage.get("model") == model
                    and isinstance(usage.get("output_tokens"), int)
                    and usage["output_tokens"] > 0):
                return True
    return False


def transient_provider_error(text: str, *, responded: bool = False) -> bool:
    lowered = (text or "").lower()
    if any(pattern in lowered for pattern in (
            "unauthorized", "invalid api key", "authentication failed",
            "subscription expired", "subscription has expired", "forbidden",
            "permission denied", "sandbox refused", "command not found")):
        return False
    if any(pattern in lowered for pattern in (
            "does not exist or you lack access", "model not found",
            "model_not_found", "modelnotfound")):
        return responded
    return any(pattern in lowered for pattern in (
        "unavailable", "overloaded", "rate limit", "rate_limit",
        "too many requests", "resource_exhausted", "freeusagelimiterror",
        "provider.quota", "connection reset", "connection closed",
        "connection was closed", "socket connection", "econnreset",
        "econnrefused", "etimedout", "timed out", "timeout", "fetch failed",
        "bad gateway", "internal server error", "gateway timeout",
    )) or bool(re.search(r"\b(?:429|500|502|503|504)\b", lowered))


_LIMIT_RESET_RE = re.compile(
    r"resets?(?:\s+at)?\s+(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?"
    r"\s*(\(UTC\)|UTC)?",
    re.IGNORECASE,
)


def provider_limit_reset(
    text: str, *, now: datetime | None = None,
) -> datetime | None:
    """Absolute reset clock from a quota-limit message (00438).

    Matches "<limit> ... resets H:MMam/pm [(UTC)]" (e.g. a 5-hour
    limit); returns the aware UTC reset or None when absent or
    unparseable. A time already past today rolls to tomorrow.
    """
    lowered = str(text or "").lower()
    if "limit" not in lowered:
        return None
    match = _LIMIT_RESET_RE.search(str(text or ""))
    if match is None:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour < 1 or hour > 12 or minute > 59:
        return None
    hour24 = (hour % 12) + (12 if match.group(3).lower() == "p" else 0)
    moment = now or datetime.now(timezone.utc)
    reset = moment.replace(hour=hour24, minute=minute, second=0,
                           microsecond=0)
    if reset <= moment:
        reset += timedelta(days=1)
    return reset
