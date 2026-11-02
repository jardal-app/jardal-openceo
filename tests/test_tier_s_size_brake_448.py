"""Tier S size budget: one test rule, plan-time lines, kept oversize build.

TASK-2026-00448. Fakes only: scripted executors, in-memory stores and
synthetic git repos. No network, no CLI, no LLM, no real Tier S cycles.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from functools import wraps
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.tier_s import cycle as cycle_mod
from agents.dev.tier_s.cycle import TierSCycle, _Ctx, _diff_sizes
from agents.dev.tier_s.plan import (
    Plan,
    PlannedFile,
    is_test_path,
    parse_plan,
    validate_plan,
)
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives.agent_result import AgentResult
from orchestration.live import ExecResult


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "tracked.txt").write_text("before\n")
    (tmp_path / ".gitignore").write_text(".openceo/\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def plan_md(lines: int | None) -> str:
    estimate = f"ESTIMATE: files=1 lines={lines}\n" if lines is not None else ""
    return ("TICKET: t\nOBJECTIVE: none\nGOAL: grow tracked.txt\n"
            "ACCEPTANCE:\n- tracked.txt grows\n"
            "FILES:\n- tracked.txt (modify) — the lines go here\n"
            "STEPS:\n1. Append the lines\n"
            f"{estimate}OUT_OF_SCOPE:\n- anything else\n")


def make_ctx(repo) -> _Ctx:
    ctx = _Ctx(
        ticket={"task_id": "t", "ref": "t", "title": "grow", "params": {}},
        repo=repo, ref="t", run_id="run",
        branch="tier-s/t-wip", base_branch="main",
        budgets={"files_per_ticket": 8, "lines_per_ticket": 400,
                 "hard_brake_factor": 2, "gate_retries": 2},
        timeouts={"planner": 1, "build": 1}, waiting_cfg={},
        auto_merge=False, budget_exclude=[], gate_specs=[], roles={})
    ctx.base_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", ctx.branch)
    return ctx


class ScriptedExecutor:
    """Planner writes PLAN.md; builder writes code, tests and HANDOFF.md."""

    def __init__(self, plan_text: str, code_lines: int = 0,
                 test_lines: int = 0) -> None:
        self.plan_text = plan_text
        self.code_lines = code_lines
        self.test_lines = test_lines
        self.calls: list[str] = []

    async def run(self, role, prompt, workdir, timeout_s):
        del prompt, timeout_s
        self.calls.append(role)
        repo = Path(workdir)
        tier = repo / ".openceo"
        tier.mkdir(exist_ok=True)
        if role == "planner":
            (tier / "PLAN.md").write_text(self.plan_text)
            (tier / "status").write_text("PLAN_READY\n")
            return ExecResult(0, 1.0, signal="PLAN_READY")
        if role == "builder":
            (repo / "src").mkdir(exist_ok=True)
            (repo / "src" / "big.ts").write_text(
                "".join(f"export const v{i} = {i};\n"
                        for i in range(self.code_lines)))
            (repo / "apps" / "web" / "test").mkdir(parents=True,
                                                   exist_ok=True)
            (repo / "apps" / "web" / "test" / "big.test.tsx").write_text(
                "".join(f"it('t{i}', () => {{}});\n"
                        for i in range(self.test_lines)))
            (tier / "HANDOFF.md").write_text("HANDOFF t\nBehavior: grew\n")
            verdict = AgentResult(resultado="hecho", motivo="grew",
                                  entregables=["src/big.ts"],
                                  evidencia="lines written")
            return ExecResult(0, 1.0, output_tail=verdict.model_dump_json())
        raise AssertionError(f"unexpected role {role}")

    async def cleanup(self, role) -> None:
        return None


@pytest.fixture
def conversions(monkeypatch):
    seen: list[dict] = []

    async def fake_convert(env, ticket, cause, sizing_text=""):
        seen.append({"cause": cause, "sizing": sizing_text})
        return {"objective_id": "OBJ-TEST"}

    monkeypatch.setattr(cycle_mod.objectives_mod, "convert_ticket",
                        fake_convert)
    return seen


async def make_cycle(executor) -> TierSCycle:
    runs = InMemoryRunStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    return TierSCycle(runs, InMemoryLockStore(), InMemoryTaskStore(),
                      executor, objective_env=object())


@pytest.mark.parametrize("path, expected", [
    ("apps/web/test/login.test.tsx", True),
    ("apps/web/src/__tests__/guard.ts", True),
    ("src/auth/guard.spec.ts", True),
    ("pkg/server_test.go", True),
    ("tests/test_cycle.py", True),
    ("openceo-core/agents/test_helpers.py", True),
    ("conftest.py", True),
    ("apps/web/src/auth/login.ts", False),
    ("src/contest.py", False),
    ("src/testing.ts", False),
])
def test_one_test_rule_for_plan_and_brake(path, expected):
    assert is_test_path(path) is expected


def test_diff_sizes_skips_nested_js_tests(repo):
    (repo / "apps" / "web" / "test").mkdir(parents=True)
    (repo / "apps" / "web" / "test" / "a.test.tsx").write_text("x\n" * 900)
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("y\n" * 10)
    base = git(repo, "rev-parse", "HEAD")
    assert _diff_sizes(repo, base, []) == (1, 10)


def test_simple_tiers_share_the_test_rule(repo):
    from types import SimpleNamespace

    from agents.dev.session import DevSessionManager, _SimpleBranch

    (repo / "apps" / "web" / "test").mkdir(parents=True)
    (repo / "apps" / "web" / "test" / "a.test.tsx").write_text("x\n" * 900)
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("y\n" * 10)
    git(repo, "add", "-A")
    base = git(repo, "rev-parse", "HEAD")
    manager = SimpleNamespace(_simple_git=DevSessionManager._simple_git)
    branch = _SimpleBranch(repo=repo, branch="c", base="main",
                           base_sha=base, ref="t")
    assert DevSessionManager._simple_diff_sizes(manager, branch, []) == (1, 10)


def test_plan_without_estimate_is_invalid(repo):
    errors = validate_plan(parse_plan(plan_md(None)), repo, 8,
                           budget_lines=400).errors
    assert "missing ESTIMATE files=<n> lines=<n>" in errors


def test_plan_over_line_budget_is_flagged(repo):
    errors = validate_plan(parse_plan(plan_md(401)), repo, 8,
                           budget_lines=400).errors
    assert any(e.startswith("line budget exceeded: 401 lines > 400")
               for e in errors)
    assert validate_plan(parse_plan(plan_md(400)), repo, 8,
                         budget_lines=400).valid


@run_async
async def test_plan_over_budget_converts_before_build(repo, conversions):
    executor = ScriptedExecutor(plan_md(900))
    cycle = await make_cycle(executor)
    ctx = make_ctx(repo)
    assert await cycle._planning(ctx) is None
    assert executor.calls == ["planner"]
    assert ctx.converted == "OBJ-TEST"
    [conv] = conversions
    assert "line budget exceeded: 900 lines > 400" in conv["cause"]
    assert conv["sizing"].startswith(cycle_mod._SIZE_CONVERSION_NOTE)
    assert "PLAN.md (split basis):\nTICKET: t" in conv["sizing"]


@run_async
async def test_size_brake_keeps_build_and_hands_plan_to_replan(
        repo, conversions):
    executor = ScriptedExecutor(plan_md(300), code_lines=900,
                                test_lines=900)
    cycle = await make_cycle(executor)
    ctx = make_ctx(repo)
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "PLAN.md").write_text(plan_md(300))
    plan = Plan(ticket="t", objective="none", goal="grow",
                acceptance=("tracked.txt grows",),
                files=(PlannedFile("src/big.ts", "create", "grow"),),
                contracts=(), decisions=(), steps=("grow",),
                estimate_files=1, estimate_lines=300, out_of_scope=())
    assert await cycle._build(ctx, plan) is False
    [conv] = conversions
    kept = "oversize/tier-s/t-wip"
    assert conv["cause"] == (
        f"size brake: 900 non-test lines > 800 (build kept on branch {kept})")
    assert conv["sizing"].startswith(cycle_mod._SIZE_CONVERSION_NOTE)
    assert f"git show {kept}:.openceo/PLAN.md" in conv["sizing"]
    assert "src/big.ts" in conv["sizing"]
    assert "PLAN.md (split basis):\nTICKET: t" in conv["sizing"]
    # The build survives on its own branch; the ticket branch is gone.
    assert len(git(repo, "show", f"{kept}:src/big.ts").splitlines()) == 900
    assert git(repo, "show", f"{kept}:.openceo/PLAN.md").startswith("TICKET")
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(repo, "branch", "--list", ctx.branch) == ""
    assert not (repo / "src" / "big.ts").exists()
    assert (repo / ".openceo" / "PLAN.md").read_text().startswith("TICKET")


@run_async
async def test_test_lines_never_trip_the_brake(repo, conversions):
    executor = ScriptedExecutor(plan_md(300), code_lines=10,
                                test_lines=2000)
    cycle = await make_cycle(executor)
    ctx = make_ctx(repo)
    plan = Plan(ticket="t", objective="none", goal="grow",
                acceptance=("tracked.txt grows",),
                files=(PlannedFile("src/big.ts", "create", "grow"),),
                contracts=(), decisions=(), steps=("grow",),
                estimate_files=1, estimate_lines=10, out_of_scope=())
    assert await cycle._build(ctx, plan) is True
    assert conversions == []
