"""Single-instance lock for the host service (see CLAUDE.md §3).

Exactly one openceo-core may run per host: lifespan startup has
deployment-wide side effects (interrupted-task marking, tmux reconcile,
schedulers) and uvicorn runs it BEFORE binding the port, so a second
instance corrupts shared state and only then dies on Errno 98.
The kernel-held flock has no stale state: any death releases it.
"""

from __future__ import annotations

import fcntl
from pathlib import Path
from typing import TextIO

LOCK_FILENAME = "openceo-core-host.lock"


def lock_path_for(app_dir: str | Path) -> Path:
    """Runtime lock path for an openceo-core checkout (``.runtime/``)."""
    return Path(app_dir).resolve().parent / ".runtime" / LOCK_FILENAME


def acquire(lock_path: str | Path) -> TextIO | None:
    """Take the instance lock. Returns the open file (keep it!), else None."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def stamp(lock_file: TextIO, pid: int) -> None:
    """Record the owner's pid in the lock file (humans + scripts)."""
    lock_file.write(str(pid))
    lock_file.flush()
