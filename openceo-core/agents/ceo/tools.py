"""CEO agent tool helpers."""

from __future__ import annotations


def sanitize_summary(text: str, max_len: int = 240) -> str:
    stripped = " ".join(text.split())
    if len(stripped) <= max_len:
        return stripped
    return stripped[: max_len - 3] + "..."
