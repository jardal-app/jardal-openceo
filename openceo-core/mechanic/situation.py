"""What the mechanic sees: tickets, live checkouts and logs, read-only.

The core gathers what the fenced run cannot reach (other repos, the
journal) and hands it over as data. Nothing here writes.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from business.links import business_mirror
from mechanic.stall import STREAK_KEY, as_utc
from reliability.sanitize import sanitize_message

JARDAL_ROOT = str(Path(__file__).resolve().parents[3])
SERVICE_UNIT = "openceo-core-host.service"
LOG_FILE = Path(__file__).resolve().parents[2] / ".runtime" / "openceo-core-host.log"
MAX_LOG_LINES = 40
MAX_STATUS_LINES = 40


@dataclass(frozen=True)
class Finding:
    task_id: str
    trigger: str
    detail: str = ""


def _run(argv: list[str], cwd: str | None = None) -> str:
    try:
        done = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<unavailable: {exc}>"
    return done.stdout


def git_facts(repo: str) -> dict[str, Any]:
    root = Path(repo)
    if not (root / ".git").exists():
        return {"error": "not a git checkout"}
    git = ["git", "--no-optional-locks", "-C", repo]
    status = _run([*git, "status", "--porcelain=v1", "--branch",
                   "--untracked-files=no"]).splitlines()
    lock = root / ".git" / "index.lock"
    facts: dict[str, Any] = {
        "status": status[:MAX_STATUS_LINES],
        "head": _run([*git, "log", "-3", "--format=%h %cI %s"]).splitlines(),
        "stashes": len(_run([*git, "stash", "list"]).splitlines()),
        "index_lock": lock.exists(),
    }
    if lock.exists():
        try:
            facts["index_lock_mtime"] = lock.stat().st_mtime
        except OSError:
            pass
    return facts


def log_lines(needles: Iterable[str], *, since_hours: int = 6) -> list[str]:
    keys = [needle for needle in needles if needle]
    if not keys:
        return []
    text = _run(["journalctl", "--user", "-u", SERVICE_UNIT, "--since",
                 f"-{since_hours}h", "-o", "cat", "--no-pager"])
    if text.startswith("<unavailable") or not text.strip():
        try:
            text = "\n".join(LOG_FILE.read_text(
                encoding="utf-8", errors="replace").splitlines()[-5000:])
        except OSError:
            return []
    hits = [line for line in text.splitlines()
            if any(key in line for key in keys)]
    return [sanitize_message(line, JARDAL_ROOT)[:400]
            for line in hits[-MAX_LOG_LINES:]]


def _iso(value: Any) -> str | None:
    moment = as_utc(value)
    return moment.isoformat() if moment else None


def task_digest(task: Mapping[str, Any]) -> dict[str, Any]:
    raw_params = task.get("params")
    params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}
    mirror = business_mirror(params.get("_mirrors") or {})
    raw_steps = task.get("steps")
    steps: list[Any] = raw_steps if isinstance(raw_steps, list) else []
    return {
        "task_id": task.get("task_id"),
        "ticket_number": task.get("ticket_number"),
        "business_ref": mirror.get("name") if isinstance(mirror, Mapping) else None,
        "title": str(task.get("title") or "")[:200],
        "project": task.get("project"),
        "status": task.get("status"),
        "coding_tool": task.get("coding_tool"),
        "objective_id": task.get("objective_id"),
        "waiting_reason": str(task.get("waiting_reason") or "")[:500] or None,
        "resume_at": _iso(task.get("resume_at")),
        "updated_at": _iso(task.get("updated_at")),
        "error": str(task.get("error") or "")[:800] or None,
        "last_response_summary":
            str(task.get("last_response_summary") or "")[:800] or None,
        STREAK_KEY: params.get(STREAK_KEY),
        "progress_guard": params.get("progress_guard"),
        "mechanic": params.get("mechanic"),
        "steps": [
            {key: str(step.get(key) or "")[:400]
             for key in ("step", "status", "detail", "timestamp")}
            for step in steps[-8:] if isinstance(step, Mapping)
        ],
    }


async def compile_situation(
    findings: list[Finding],
    *,
    get_task: Callable[[str], Awaitable[Mapping[str, Any] | None]],
    repo_for_project: Callable[[str], str | None],
    release: Mapping[str, Any] | None = None,
    git: Callable[[str], dict[str, Any]] = git_facts,
    logs: Callable[[Iterable[str]], list[str]] = log_lines,
    tier_s_run: Callable[[str], Awaitable[Mapping[str, Any] | None]] | None = None,
) -> dict[str, Any]:
    tickets: list[dict[str, Any]] = []
    repos: dict[str, dict[str, Any]] = {}
    for finding in findings:
        task = await get_task(finding.task_id)
        if task is None:
            continue
        digest = task_digest(task)
        digest["trigger"] = finding.trigger
        digest["trigger_detail"] = finding.detail[:500]
        repo = repo_for_project(str(task.get("project") or "")) \
            if task.get("project") else None
        digest["repo"] = repo
        if repo and repo not in repos:
            repos[repo] = git(repo)
        number = task.get("ticket_number")
        digest["log"] = logs([str(task.get("task_id") or ""),
                              f"#{number}" if number else "",
                              str(digest.get("business_ref") or "")])
        if tier_s_run is not None and task.get("coding_tool") == "tier_s":
            run = await tier_s_run(finding.task_id)
            if run:
                digest["tier_s_run"] = {
                    key: run.get(key) for key in (
                        "run_id", "status", "phase", "waiting", "branch",
                        "base_sha", "active_repo", "updated_at")}
                digest["tier_s_run"]["rounds"] = len(run.get("rounds") or [])
                digest["tier_s_run"]["metrics"] = str(run.get("metrics") or "")[:1500]
        tickets.append(digest)
    return {"release": dict(release or {}), "tickets": tickets, "repos": repos}
