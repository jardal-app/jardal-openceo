"""Orchestrator scratch (``.openceo/``): agents write it, commits never carry it.

``git add`` never unstages, so a scratch file an agent force-added would
ride into the ticket commit or block the next ticket (TASK-2026-00506).
Every orchestrator staging goes through :func:`stage_work`
(TASK-2026-00510).
"""

from __future__ import annotations

from typing import Any, Callable

SCRATCH_DIR = ".openceo"
WORK_PATHSPEC = ("--", ".", f":!{SCRATCH_DIR}")


def unstage_scratch(run_git: Callable[..., Any], repo: Any) -> Any:
    """Drop scratch entries from the index; the files stay on disk."""
    return run_git(repo, "reset", "-q", "--", SCRATCH_DIR)


def stage_work(run_git: Callable[..., Any], repo: Any) -> Any:
    """Stage the ticket's work, never the scratch. Returns the add result."""
    result = run_git(repo, "add", "-A", *WORK_PATHSPEC)
    unstage_scratch(run_git, repo)
    return result
