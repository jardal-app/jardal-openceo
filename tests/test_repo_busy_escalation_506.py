"""Dirty-checkout handling (TASK-2026-00506, generalized by TASK-2026-00510).

Live case 2026-11-05: a builder force-added the ignored
``.openceo/HANDOFF.md``; every later attempt parked on ``repo_busy``
every 15 min, silently, until a human cleaned it. The prompt forbids
force-adding ignored paths and the orchestrator unstages its own
scratch; a dirt park that keeps repeating is no longer special-cased:
the generic stall rule stops it and wakes the mechanic
(``tests/test_mechanic_510.py``). Real git in temp dirs.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.builder_contract import builder_environment_prompt  # noqa: E402
from agents.dev.session import DevSessionManager  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402


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


def _open(task_params: dict | None, repo: Path):
    settings = MagicMock()
    settings.DEV_EXECUTION_MODE = "live"
    return DevSessionManager(settings, MagicMock())._open_simple_branch(
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
    out = _open({"x": 1}, repo)
    assert getattr(out, "status", None) != TaskStatus.WAITING
    assert _git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert (repo / ".openceo" / "HANDOFF.md").read_text(
        encoding="utf-8") == "summary\n"


def test_foreign_dirt_parks_with_resume_and_keeps_work(repo: Path) -> None:
    (repo / "a.txt").write_text("owner wip\n", encoding="utf-8")
    out = _open({"x": 1}, repo)
    assert out.status == TaskStatus.WAITING
    assert out.waiting_reason.startswith("repo_busy: ")
    assert out.resume_at is not None
    assert out.params == {"x": 1}
    assert (repo / "a.txt").read_text(encoding="utf-8") == "owner wip\n"
