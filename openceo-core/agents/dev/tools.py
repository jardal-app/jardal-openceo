"""Dev agent tool helpers."""

from __future__ import annotations

import re


def slugify(text: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] if slug else "task"
