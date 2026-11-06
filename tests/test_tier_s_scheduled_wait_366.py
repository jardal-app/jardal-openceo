"""Tier S scheduled waits (TASK-2026-00366, generalized by TASK-2026-00510).

Every scheduled_wait parks with its timer and checkpoint; the engine keeps
no counter of its own. Repeats are bounded by the generic stall rule on
the run outcome write (tests/test_mechanic_510.py), which sees the
resume_at this outcome carries.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.tier_s.cycle import TierSCycle, _Ctx
from agents.dev.tier_s.models import (
    BUDGETS_DEFAULTS,
    TIMEOUTS_DEFAULTS,
    WAITING_DEFAULTS,
)
from agents.dev.tier_s.plan import Plan, PlannedFile
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives.agent_result import (
    AgentResult,
)
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


def wait_verdict():
    return AgentResult(
        resultado="bloqueado",
        motivo="Ventana de observación pendiente",
        evidencia="Inicio y plazo conservados en el registro de despliegue",
        resume_at=datetime.now(timezone.utc) + timedelta(hours=24))


def done_verdict():
    return AgentResult(
        resultado="hecho",
        motivo="observación completa",
        entregables=["tracked.txt"],
        evidencia="salud verificada en el registro de despliegue")


def demo_plan() -> Plan:
    return Plan(
        ticket="t", objective="", goal="observar",
        acceptance=("salud verificada",),
        files=(PlannedFile(path="tracked.txt", operation="modify",
                           reason="observar"),),
        contracts=(), decisions=(), steps=("observar",),
        estimate_files=1, estimate_lines=10, out_of_scope=())


class ScriptedExecutor:
    def __init__(self, verdicts: list[AgentResult]) -> None:
        self._verdicts = list(verdicts)

    async def run(self, role, prompt, repo, timeout_s):
        verdict = self._verdicts.pop(0)
        return ExecResult(0, 1, output_tail=verdict.model_dump_json())

    async def cleanup(self, role) -> None:
        return None


def make_cycle(runs, tasks, executor) -> TierSCycle:
    return TierSCycle(runs, InMemoryLockStore(), tasks, executor)


def make_ctx(repo, tasks_ticket_params: dict, run_id: str = "run",
             branch: str = "tier-s/t-wip") -> _Ctx:
    return _Ctx(
        ticket={"task_id": "t", "ref": "t", "params": dict(tasks_ticket_params)},
        repo=repo, ref="t", run_id=run_id,
        branch=branch, base_branch="main",
        budgets={"files_per_ticket": 8, "lines_per_ticket": 400,
                 "hard_brake_factor": 2, "gate_retries": 2},
        timeouts={"build": 60}, waiting_cfg={}, auto_merge=False,
        budget_exclude=[], gate_specs=[], roles={})


@run_async
async def test_repeated_waits_always_park_with_a_timer(repo):
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    base = git(repo, "rev-parse", "HEAD")
    for attempt in range(1, 5):
        branch = f"tier-s/t-wip-{attempt}"
        git(repo, "checkout", "-qb", branch)
        run_id = f"run-{attempt}"
        await runs.create_run({"run_id": run_id, "task_id": "t",
                               "status": "running"})
        cycle = make_cycle(runs, tasks, ScriptedExecutor([wait_verdict()]))
        ctx = make_ctx(repo, {}, run_id=run_id, branch=branch)
        ctx.base_sha = base
        assert await cycle._build(ctx, demo_plan()) is False
        outcome = ctx.parked_outcome
        assert outcome is not None
        assert outcome.reason == "scheduled_wait"
        assert outcome.resume_at is not None
        assert "scheduled_wait_count" not in (tasks.patches.get("t") or {}).get("params", {})


@run_async
async def test_legit_wait_keeps_timer_and_checkpoint(repo):
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    base = git(repo, "rev-parse", "main")
    verdict = wait_verdict()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([verdict]))
    ctx = make_ctx(repo, {})
    ctx.base_sha = base
    assert await cycle._build(ctx, demo_plan()) is False
    outcome = ctx.parked_outcome
    assert outcome is not None
    assert outcome.reason == "scheduled_wait"
    assert outcome.resume_at == verdict.resume_at.isoformat()
    assert outcome.agent_result is not None
    assert outcome.agent_result["evidencia"] == verdict.evidencia
    saved = await runs.resumable_run("t")
    assert saved is not None and saved["checkpoint"]["phase"] == "build"


_RUN_PLAN_MD = """TICKET: TASK-2026-99999
OBJECTIVE: none
GOAL: Observar una ventana de despliegue
ACCEPTANCE:
- salud verificada en el registro de despliegue
FILES:
- tracked.txt (modify) — la ventana se observa aquí
CONTRACTS:
- produces none
DECISIONS:
- D1 [durable: no] solo observar — el alcance lo pide
STEPS:
1. Observar la ventana
2. Registrar el resultado
ESTIMATE: files=1 lines=1
OUT_OF_SCOPE:
- anything else
"""

_RUN_PLAN_REVIEW_MD = """O1 [WARN] PLAN.md:7 — la ventana es corta → registrar el inicio y el plazo
> ACCEPTANCE:
REVIEW_DONE_plan_0
"""


class RunBoundaryExecutor:
    """Role-aware stand-in for full TierSCycle.run() (no CLI, no spend).

    Planner/reviewer answer with files like the F1+F2 final test; the
    builder answers the queued AgentResult verdicts.
    """

    def __init__(self, verdicts: list[AgentResult]) -> None:
        self._verdicts = list(verdicts)

    async def run(self, role, prompt, workdir, timeout_s):
        del timeout_s
        tier_dir = Path(workdir) / ".openceo"
        tier_dir.mkdir(exist_ok=True)
        if role == "planner" and "PLAN_RESPONSE.md is incomplete" not in prompt \
                and "Answer each plan observation" not in prompt \
                and "Your PLAN.md is invalid" not in prompt:
            (tier_dir / "PLAN.md").write_text(_RUN_PLAN_MD, encoding="utf-8")
            (tier_dir / "status").write_text("PLAN_READY\n", encoding="utf-8")
            return ExecResult(0, 1.0, signal="PLAN_READY", model="opus",
                              slot="claudej")
        if role == "planner":
            (tier_dir / "PLAN_RESPONSE.md").write_text(
                "O1: ACEPTADA — inicio y plazo registrados\n", encoding="utf-8")
            (tier_dir / "status").write_text("PLAN_RESPONDED\n",
                                             encoding="utf-8")
            return ExecResult(0, 0.5, signal="PLAN_RESPONDED", model="opus",
                              slot="claudej")
        if role == "plan_reviewer":
            (tier_dir / "PLAN_REVIEW.md").write_text(
                _RUN_PLAN_REVIEW_MD, encoding="utf-8")
            return ExecResult(0, 0.5, model="gpt-6.1-sol", slot="codexj")
        if role == "builder":
            verdict = self._verdicts.pop(0)
            return ExecResult(0, 1, output_tail=verdict.model_dump_json())
        raise AssertionError(f"unexpected role {role}")

    async def cleanup(self, role) -> None:
        return None


def run_ticket(task_id: str, params: dict) -> dict:
    return {
        "task_id": task_id,
        "ref": "TASK-2026-99999",
        "title": "tope de esperas",
        "description": "observar una ventana de despliegue",
        "acceptance": ["salud verificada en el registro de despliegue"],
        "coding_tool": "tier_s",
        "blocked_by": [],
        "origin": "owner",
        "params": dict(params),
    }


def run_cfg(roles: dict) -> dict:
    return {
        "roles": roles,
        "budgets": dict(BUDGETS_DEFAULTS),
        "timeouts": dict(TIMEOUTS_DEFAULTS),
        "waiting": dict(WAITING_DEFAULTS),
        "auto_merge": True,
        "base_branch": "main",
        "budget_exclude": [],
    }


@run_async
async def test_every_resume_parks_with_a_timer_for_the_generic_rule(repo):
    """Each outcome carries reason and resume_at to the run outcome write.

    That write is where the generic stall rule counts repeats; the cycle
    itself keeps no counter, so nothing escalates inside the engine.
    """
    runs = InMemoryRunStore()
    locks = InMemoryLockStore()
    tasks = InMemoryTaskStore()
    roles = {
        "planner": {"account": "claudej", "model": "opus",
                    "command": ["true"]},
        "plan_reviewer": {"account": "codexj", "model": "gpt-6.1-sol",
                          "command": ["true"]},
        "builder": {"account": "muse",
                    "model": "muse-spark-1.3-contributor",
                    "command": ["true"]},
    }
    carried: dict = {}
    for _attempt in range(4):
        cycle = TierSCycle(runs, locks, tasks,
                           RunBoundaryExecutor([wait_verdict()]))
        outcome = await cycle.run(run_ticket("t", carried), repo,
                                  run_cfg(roles), gate_specs=[])
        assert outcome.reason == "scheduled_wait"
        assert outcome.resume_at is not None
        carried = dict(outcome.params or {})
        assert "scheduled_wait_count" not in carried
