"""Close checkpoint: survive the restart of our own §9 close (TASK-2026-00247).

Before restarting the service to publish a ticket, the agent persists what
is already published/verified and what is still pending (smoke, ticket
close, next pick). After boot it completes the pending half exactly once;
a second completion is a no-op, so merge/push/smoke/close never repeat.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "CHECKPOINT_FILENAME",
    "CloseCheckpoint",
    "clear_close_checkpoint",
    "complete_close_once",
    "load_close_checkpoint",
    "save_close_checkpoint",
]

#: File name under ``.runtime/`` (same host dir as the pid file and lock).
CHECKPOINT_FILENAME = "close_checkpoint.json"


@dataclass
class CloseCheckpoint:
    """Published vs pending halves of one ticket close."""

    task_id: str = ""
    ticket: str = ""
    published: dict[str, Any] = field(default_factory=dict)
    verified: list[str] = field(default_factory=list)
    pending: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "CloseCheckpoint":
        if not isinstance(data, dict):
            raise ValueError("close checkpoint must be an object")
        return cls(
            task_id=str(data.get("task_id") or ""),
            ticket=str(data.get("ticket") or ""),
            published=dict(data.get("published") or {}),
            verified=[str(v) for v in (data.get("verified") or [])],
            pending=dict(data.get("pending") or {}),
            created_at=str(data.get("created_at") or ""),
        )


def save_close_checkpoint(path: str | Path, checkpoint: CloseCheckpoint) -> Path:
    """Atomic write (tmp + fsync + replace)."""
    target = Path(path)
    if not checkpoint.created_at:
        checkpoint.created_at = datetime.now(timezone.utc).isoformat()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(checkpoint.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(target)
    return target


def load_close_checkpoint(path: str | Path) -> CloseCheckpoint | None:
    """Read back, None when absent. Corrupt content raises ValueError."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"corrupt close checkpoint at {target}") from exc
    try:
        return CloseCheckpoint.from_dict(data)
    except ValueError as exc:
        raise ValueError(f"corrupt close checkpoint at {target}") from exc


def clear_close_checkpoint(path: str | Path) -> bool:
    """Delete the checkpoint. True when one existed."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True


def complete_close_once(
    path: str | Path, complete: Callable[[CloseCheckpoint], Any]
) -> dict[str, Any]:
    """Run ``complete`` on the saved checkpoint, then clear it, exactly once.

    Returns ``{"completed": True}`` on the run that consumed the checkpoint
    and ``{"completed": False}`` when none was pending. A ``complete`` that
    raises leaves the checkpoint in place for the next boot.
    """
    checkpoint = load_close_checkpoint(path)
    if checkpoint is None:
        return {"completed": False}
    complete(checkpoint)
    clear_close_checkpoint(path)
    return {"completed": True, "ticket": checkpoint.ticket}
