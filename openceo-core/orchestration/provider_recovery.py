from __future__ import annotations

import json
import re


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
