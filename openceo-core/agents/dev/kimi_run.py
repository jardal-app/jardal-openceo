from __future__ import annotations

import re
from dataclasses import dataclass

KIMI_RETRYABLE_EXIT_CODE = 75

_RATE_LIMIT_PATTERNS = [
    re.compile(r"\brate limit\b", re.IGNORECASE),
    re.compile(r"\btoo many requests\b", re.IGNORECASE),
    re.compile(r"\bquota exceeded\b", re.IGNORECASE),
    re.compile(r"\bresource exhausted\b", re.IGNORECASE),
    re.compile(r"\b429\b"),
]
_TRY_AGAIN_IN_RE = re.compile(
    r"try again in\s+((?:\d+\s*[dhms]\s*)+)",
    re.IGNORECASE,
)
_RETRY_AFTER_RE = re.compile(
    r"retry[- ]after[: ]+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|s|m|h)?",
    re.IGNORECASE,
)


@dataclass
class KimiParsedOutput:
    session_id: str | None
    final_text: str
    raw_text: str


@dataclass
class KimiRetryableFailure:
    waiting_reason: str
    wait_seconds: int | None
    detail: str


def parse_kimi_output(content: str) -> KimiParsedOutput:
    """Parse kimi CLI --print mode output.

    Kimi print mode emits a repr-like event stream:
        TurnBegin(user_input='...')
        StepBegin(n=1)
        ThinkPart(type='think', think='...', encrypted=None)
        TextPart(type='text', text='...')
        StatusUpdate(...)
        TurnEnd()

    We extract all TextPart text values and the resume session ID.
    """
    session_id: str | None = None
    text_parts: list[str] = []

    # Extract resume session ID from the trailing hint
    resume_match = re.search(
        r"To resume this session:\s+kimi\s+(?:-r|--resume|–resum[eé])\s+([a-f0-9-]+)",
        content,
        re.IGNORECASE,
    )
    if resume_match:
        session_id = resume_match.group(1)

    # Extract every TextPart text=... value
    # Pattern handles both single-line and multi-line text values
    for m in re.finditer(
        r"TextPart\(\s*type='text',\s*text=(?:'([^']*?)'|\"([^\"]*?)\"|\*\*MULTI_LINE_PLACEHOLDER\*\*)\s*\)",
        content,
        re.DOTALL,
    ):
        text = m.group(1) if m.group(1) is not None else m.group(2)
        if text:
            text_parts.append(text)

    # Fallback: if no TextPart matched, try a simpler greedy approach for
    # malformed output (e.g. when text contains escaped quotes).
    if not text_parts:
        for m in re.finditer(
            r"TextPart\([^)]*text=['\"](.+?)['\"][^)]*\)",
            content,
            re.DOTALL,
        ):
            text = m.group(1).replace("\\'", "'").replace('\\"', '"')
            text_parts.append(text)

    final_text = text_parts[-1] if text_parts else ""
    raw_text = "\n".join(text_parts).strip()
    return KimiParsedOutput(
        session_id=session_id,
        final_text=final_text,
        raw_text=raw_text,
    )


def detect_kimi_retryable_failure(
    exit_code: int | None,
    content: str,
) -> KimiRetryableFailure | None:
    text = (content or "").strip()
    if exit_code != KIMI_RETRYABLE_EXIT_CODE and not any(p.search(text) for p in _RATE_LIMIT_PATTERNS):
        return None

    is_rate_limit = any(p.search(text) for p in _RATE_LIMIT_PATTERNS)
    wait_seconds = _extract_wait_seconds(text)
    waiting_reason = "rate_limit:kimi" if is_rate_limit else "retryable_error:kimi"
    detail = _first_meaningful_line(text)
    if not detail:
        if is_rate_limit:
            detail = "Kimi rate limit reached"
        else:
            detail = "Kimi retryable failure (429/5xx/timeout)"
    return KimiRetryableFailure(
        waiting_reason=waiting_reason,
        wait_seconds=wait_seconds,
        detail=detail,
    )


def _extract_wait_seconds(text: str) -> int | None:
    if not text:
        return None

    match = _TRY_AGAIN_IN_RE.search(text)
    if match:
        total = 0
        for amount, unit in re.findall(r"(\d+)\s*([dhms])", match.group(1), re.IGNORECASE):
            value = int(amount)
            unit = unit.lower()
            if unit == "d":
                total += value * 86400
            elif unit == "h":
                total += value * 3600
            elif unit == "m":
                total += value * 60
            else:
                total += value
        if total > 0:
            return total + 120

    match = _RETRY_AFTER_RE.search(text)
    if match:
        value = int(match.group(1))
        unit = (match.group(2) or "s").lower()
        if unit.startswith("h"):
            return value * 3600 + 120
        if unit.startswith("m"):
            return value * 60 + 120
        return value + 120

    return None


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""
