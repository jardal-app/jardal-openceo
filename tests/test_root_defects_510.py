"""Design defects removed at the root (TASK-2026-00510).

Three classes where the system contradicted itself and stopped work:
an informational plan field rejected the whole plan (#549/#550), the
orchestrator's own scratch blocked or rode into commits (00506), and
components disagreed on whether a converted blocker releases its
dependents (#540). Real git in temp dirs; no models, no network.
"""
from __future__ import annotations

import asyncio
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
sys.path.insert(0, str(CORE))

from agents.dev import scratch  # noqa: E402
from agents.dev.builder_contract import builder_environment_prompt  # noqa: E402
from agents.dev.tier_s import cycle as cycle_mod  # noqa: E402
from agents.dev.tier_s.plan import parse_plan, validate_plan  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from state import BLOCKER_RELEASED_STATUSES  # noqa: E402
from task_service import TaskService  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def run_git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    (tmp_path / "tracked.txt").write_text("before\n")
    (tmp_path / ".gitignore").write_text(".openceo/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def plan_md(*, ticket: bool = True, estimate: str | None = None,
            files: bool = True) -> str:
    return ((("TICKET: t\n") if ticket else "")
            + "OBJECTIVE: none\nGOAL: grow tracked.txt\n"
            "ACCEPTANCE:\n- tracked.txt grows\n"
            + ("FILES:\n- tracked.txt (modify) — the lines go here\n" if files else "")
            + "STEPS:\n1. Append the lines\n"
            + (f"ESTIMATE: {estimate}\n" if estimate is not None else "")
            + "OUT_OF_SCOPE:\n- anything else\n")


# -- A: informational plan fields never stop the cycle -------------------------

def test_missing_informational_fields_are_warnings(repo: Path) -> None:
    validation = validate_plan(parse_plan(plan_md(ticket=False)), repo, 8,
                               budget_lines=400)
    assert validation.valid
    assert {w.split()[0] for w in validation.warnings} == {"TICKET", "ESTIMATE"}


def test_essential_fields_still_block(repo: Path) -> None:
    validation = validate_plan(parse_plan(plan_md(files=False)), repo, 8,
                               budget_lines=400)
    assert not validation.valid and "missing FILES" in validation.errors


@pytest.mark.parametrize("wording, expected", [
    ("files=3 lines=180", (3, 180)),
    ("3 files, ~180 lines", (3, 180)),
    ("files: 2 / lines: 90", (2, 90)),
    ("~120 líneas en 2 ficheros", (2, 120)),
    ("unos 150 lines", (None, 150)),
])
def test_estimate_wording_is_understood(wording: str, expected: tuple) -> None:
    plan = parse_plan(plan_md(estimate=wording))
    assert (plan.estimate_files, plan.estimate_lines) == expected


def test_estimate_without_colon_is_read(repo: Path) -> None:
    # Live #549/#550: the prompt showed the colon-less form and the planner
    # followed it; the section grammar required "ESTIMATE:" and lost it.
    text = plan_md().replace("OUT_OF_SCOPE:", "ESTIMATE files=7 lines=90\nOUT_OF_SCOPE:")
    plan = parse_plan(text)
    assert (plan.estimate_files, plan.estimate_lines) == (7, 90)
    validation = validate_plan(plan, repo, 8, budget_lines=400)
    assert validation.valid
    assert not any("ESTIMATE" in warning for warning in validation.warnings)


def test_tolerant_estimate_still_enforces_the_budget(repo: Path) -> None:
    validation = validate_plan(parse_plan(plan_md(estimate="2 files, 900 lines")),
                               repo, 8, budget_lines=400)
    assert any("line budget exceeded" in error for error in validation.errors)


def test_planner_prompt_asks_for_the_estimate_on_its_own_line() -> None:
    source = inspect.getsource(cycle_mod)
    assert "ESTIMATE: files=3 lines=180" in source


# -- B: orchestrator scratch is never committed nor left staged --------------

@pytest.mark.parametrize("flags", [("-f",), ("-N", "-f")])
def test_stage_work_never_stages_scratch(repo: Path, flags: tuple) -> None:
    (repo / ".openceo").mkdir()
    (repo / ".openceo" / "HANDOFF.md").write_text("summary\n")
    git(repo, "add", *flags, ".openceo/HANDOFF.md")
    (repo / "tracked.txt").write_text("after\n")
    scratch.stage_work(run_git, repo)
    staged = git(repo, "diff", "--cached", "--name-only").splitlines()
    assert staged == ["tracked.txt"]
    assert (repo / ".openceo" / "HANDOFF.md").read_text() == "summary\n"


def test_every_orchestrator_staging_goes_through_the_helper() -> None:
    offenders = [
        f"{path.relative_to(CORE)}:{number}"
        for path in (CORE / "agents").rglob("*.py")
        if path.name != "scratch.py"
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if '"add", "-A"' in line]
    assert offenders == []


def test_builder_rule_separates_deliverables_from_scratch() -> None:
    text = builder_environment_prompt([])
    assert "git add -f" in text and ".openceo/HANDOFF.md" in text
    assert "nunca entra en el commit" in text
    assert "Tus entregables deben aparecer en git status" not in text


# -- C: one definition of a blocker that releases its dependents --------------

def test_converted_never_releases_anywhere() -> None:
    assert BLOCKER_RELEASED_STATUSES == {"completed", "cancelled"}
    assert cycle_mod.TERMINAL_BLOCKER_STATUS is BLOCKER_RELEASED_STATUSES
    assert engine_mod.BLOCKER_DONE_STATUS is BLOCKER_RELEASED_STATUSES
    for method in (TaskService.blockers_open, TaskService._on_ticket_done):
        assert "BLOCKER_RELEASED_STATUSES" in inspect.getsource(method)


@pytest.mark.parametrize("status, still_open", [
    ("converted", True), ("failed", True), ("in_progress", True),
    ("completed", False), ("cancelled", False)])
def test_dispatcher_blocker_check(status: str, still_open: bool) -> None:
    service = TaskService.__new__(TaskService)
    service._repo = SimpleNamespace(get_task=AsyncMock(
        return_value={"status": status}))
    assert asyncio.run(service.blockers_open({"blocked_by": ["b"]})) is still_open
