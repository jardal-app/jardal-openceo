"""Tier S scheduled_wait cap (TASK-2026-00366, diseño v2 §4.7).

Tier S comparte el contrato de TASK-2026-00350 con A/B/C y el agente
genérico: como máximo 3 declinaciones scheduled_wait consecutivas y
después needs_owner. El contador vive en los params del ticket y
sobrevive a la reanudación; la ruta hecho lo limpia.
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
from agents.dev.tier_s.plan import Plan, PlannedFile
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives.agent_result import (
    SCHEDULED_WAIT_COUNT_KEY,
    SCHEDULED_WAIT_MAX_DECLINES,
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
async def test_scheduled_wait_cap_escalates_after_three_declines(repo):
    assert SCHEDULED_WAIT_MAX_DECLINES == 3
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    base = git(repo, "rev-parse", "HEAD")
    persisted: dict = {}
    for expected in range(1, SCHEDULED_WAIT_MAX_DECLINES + 1):
        branch = f"tier-s/t-wip-{expected}"
        git(repo, "checkout", "-qb", branch)
        run_id = f"run-{expected}"
        await runs.create_run({"run_id": run_id, "task_id": "t",
                               "status": "running"})
        cycle = make_cycle(runs, tasks, ScriptedExecutor([wait_verdict()]))
        ctx = make_ctx(repo, persisted, run_id=run_id, branch=branch)
        ctx.base_sha = base
        assert await cycle._build(ctx, demo_plan()) is False
        outcome = ctx.parked_outcome
        assert outcome is not None
        persisted = dict(tasks.patches["t"]["params"])
        assert persisted[SCHEDULED_WAIT_COUNT_KEY] == expected
        if expected < SCHEDULED_WAIT_MAX_DECLINES:
            assert outcome.reason == "scheduled_wait"
            assert outcome.resume_at is not None
        else:
            assert outcome.reason == "needs_owner"
            assert outcome.resume_at is None
            assert outcome.detail.startswith("scheduled_wait declined 3 times")


@run_async
async def test_legit_wait_before_cap_keeps_timer_and_checkpoint(repo):
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
    assert tasks.patches["t"]["params"][SCHEDULED_WAIT_COUNT_KEY] == 1


@run_async
async def test_scheduled_wait_count_clears_on_hecho(repo):
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    base = git(repo, "rev-parse", "main")
    (repo / "tracked.txt").write_text("after\n")
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text("cambio mínimo verificado")
    cycle = make_cycle(runs, tasks, ScriptedExecutor([done_verdict()]))
    ctx = make_ctx(repo, {SCHEDULED_WAIT_COUNT_KEY: 2})
    ctx.base_sha = base
    assert await cycle._build(ctx, demo_plan()) is True
    assert SCHEDULED_WAIT_COUNT_KEY not in tasks.patches["t"]["params"]
