"""Serialize the squash+merge close window across processes (#560).

Concurrent ticket closes share a single checkout and used to race
``checkout <base>`` + ``merge`` with no lock: the loser parked as
``needs_owner`` although the condition was transient. Both engines run the
close window (checkout base + merge + branch deletion) under this
inter-process flock instead, so closes serialize and never interleave
(mirror of ``crystallization.lock``).
"""
from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

#: Merge-window lock inside the checkout (``.runtime/``).
LOCK_FILENAME = "merge.lock"

#: How long a close waits for another close before parking transient.
MERGE_LOCK_TIMEOUT_S = 120.0

#: Poll interval while waiting for the lock.
_MERGE_LOCK_POLL_S = 0.2


class MergeLockBusy(TimeoutError):
    """Another close held the merge lock past the timeout."""


def merge_lock_path(repo: Path | str) -> Path:
    """Lock file serializing closes on one checkout."""
    return Path(repo) / ".runtime" / LOCK_FILENAME


@contextmanager
def merge_locked(
    repo: Path | str, *, timeout: float = MERGE_LOCK_TIMEOUT_S,
) -> Iterator[None]:
    """Hold the checkout's merge lock (flock, shared across processes).

    Blocking up to ``timeout`` seconds; past it raises
    :class:`MergeLockBusy` so the caller parks transient instead of
    hanging the close. The kernel-held flock has no stale state: any
    death of the holder releases it.
    """
    lock_path = merge_lock_path(repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise MergeLockBusy(f"merge lock busy: {lock_path}")
                time.sleep(_MERGE_LOCK_POLL_S)
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.debug("merge unlock failed for %s", lock_path,
                             exc_info=True)
