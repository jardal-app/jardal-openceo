"""Dirty-checkout escalation instead of a silent stall (TASK-2026-00506).

Live case 2026-11-05: a builder force-added the ignored
``.openceo/HANDOFF.md`` so it would show in ``git status``; the merge
kept main clean but left the intent-to-add entry staged in the live
checkout, and every later attempt parked on ``repo_busy`` every 15 min,
silently and holding the repo lock, until a human cleaned it (~90 min).

The fix has three parts: the builder prompt forbids force-adding
ignored paths, the orchestrator unstages its own ``.openceo/`` scratch
before the dirt check, and dirt that survives its retry budget
escalates with an ERP comment instead of waiting forever. Real git in
temp dirs; no models, no network.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.builder_contract import builder_environment_prompt  # noqa: E402
from agents.dev.session import (  # noqa: E402
    REPO_BUSY_DIRT_COUNT_KEY,
    REPO_BUSY_DIRT_MAX_DECLINES,
    DevSessionManager,
)
from state import CodingTool, TaskStatus  # noqa: E402
from task_service import TaskService  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    (path / ".gitignore").write_text(".openceo/\n", encoding="utf-8")
    _git(path, "add", "a.txt", ".gitignore")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _manager() -> DevSessionManager:
    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "live"
    return DevSessionManager(settings, MagicMock())


def _open(task_params: dict | None, repo: Path):
    return _manager()._open_simple_branch(
        {"task_id": "t-1", "params": task_params}, {"dev": {}},
        CodingTool.TIER_C, repo)


def test_prompt_forbids_force_adding_ignored_paths() -> None:
    text = builder_environment_prompt([])
    assert "git add -f" in text
    assert ".openceo/HANDOFF.md" in text


def test_intent_to_add_scratch_is_reset_and_branch_opens(repo: Path) -> None:
    (repo / ".openceo").mkdir()
    (repo / ".openceo" / "HANDOFF.md").write_text("summary\n", encoding="utf-8")
    _git(repo, "add", "-N", "-f", ".openceo/HANDOFF.md")
    assert "HANDOFF.md" in _git(repo, "status", "--porcelain",
                                "--untracked-files=no")
    out = _open({"x": 1}, repo)
    assert getattr(out, "status", None) != TaskStatus.WAITING
    assert _git(repo, "status", "--porcelain",
                "--untracked-files=no") == ""
    assert (repo / ".openceo" / "HANDOFF.md").read_text(
        encoding="utf-8") == "summary\n"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("tier/")


def test_dirty_tree_parks_twice_then_escalates_without_resume(
        repo: Path) -> None:
    assert REPO_BUSY_DIRT_MAX_DECLINES == 3
    (repo / "a.txt").write_text("owner wip\n", encoding="utf-8")
    params: dict | None = {"x": 1}
    for attempt in (1, 2):
        out = _open(params, repo)
        assert out.status == TaskStatus.WAITING
        assert out.waiting_reason.startswith("repo_busy: ")
        assert out.resume_at is not None
        assert out.params["x"] == 1
        assert out.params[REPO_BUSY_DIRT_COUNT_KEY]["count"] == attempt
        params = out.params
    out = _open(params, repo)
    assert out.status == TaskStatus.WAITING
    assert out.waiting_reason.startswith("repo_busy_persistent: ")
    assert "a.txt" in out.waiting_reason
    assert out.resume_at is None
    assert out.params[REPO_BUSY_DIRT_COUNT_KEY]["count"] == 3
    assert (repo / "a.txt").read_text(encoding="utf-8") == "owner wip\n"


def test_stale_streak_restarts_its_count(repo: Path) -> None:
    (repo / "a.txt").write_text("owner wip\n", encoding="utf-8")
    stale = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    out = _open({REPO_BUSY_DIRT_COUNT_KEY: {"count": 2, "since": stale}},
                repo)
    assert out.status == TaskStatus.WAITING
    assert out.waiting_reason.startswith("repo_busy: ")
    assert out.resume_at is not None
    assert out.params[REPO_BUSY_DIRT_COUNT_KEY]["count"] == 1


class PersistentDirtCommentTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, adapter=None):
        service = TaskService.__new__(TaskService)
        service._business = adapter
        service._repo = MagicMock()
        service._repo.patch_task = AsyncMock()
        return service

    def _task(self, **overrides):
        task = {"task_id": "task-1",
                "params": {"_mirrors": {"erpnext": {"name": "TASK-1"}}}}
        task.update(overrides)
        return task

    async def test_persistent_dirt_comments_once_with_files(self) -> None:
        adapter = MagicMock()
        adapter.is_enabled = True
        adapter.add_comment = AsyncMock()
        service = self._service(adapter)
        reason = ("repo_busy_persistent: /repo has uncommitted changes on "
                  "main after 3 attempts (no re-park): M a.txt")
        await service._comment_wait_on_erp(self._task(), reason)
        adapter.add_comment.assert_awaited_once()
        args = adapter.add_comment.await_args.args
        assert args[0] == "TASK-1"
        assert "a.txt" in args[1]
        assert "relanza" in args[1]
        service._repo.patch_task.assert_awaited_once_with(
            "task-1", {"wait_commented_for": "repo_busy_persistent"})
