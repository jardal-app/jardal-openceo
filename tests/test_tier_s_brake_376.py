"""Tier S capture and shadow brake (TASK-2026-00376, design v2 §4.5 + §5.2).

Fakes only: scripted executors, a dict-backed incidents collection and
synthetic git repos. No network, no CLI, no LLM, no real Tier S cycles.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.tier_s.cycle import TierSCycle, _Ctx
from agents.dev.tier_s.models import (
    BUDGETS_DEFAULTS,
    TIMEOUTS_DEFAULTS,
    WAITING_DEFAULTS,
    GateResult,
)
from agents.dev.tier_s.plan import Plan, PlannedFile
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives.agent_result import AgentResult
from orchestration.live import ExecResult
from reliability import tier_s_brake as brake_mod
from reliability.config import RELIABILITY_DEFAULTS


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


class _FakeIncidents:
    """Async dict-backed incidents collection for brake tests."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def insert_one(self, doc: dict) -> SimpleNamespace:
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def count_documents(self, filt: dict) -> int:
        since = (filt.get("created_at") or {}).get("$gte")
        want = filt.get("fingerprint")
        total = 0
        for doc in self.docs.values():
            if want is not None and doc.get("fingerprint") != want:
                continue
            if since is not None and doc.get("created_at") < since:
                continue
            total += 1
        return total


def incidents_source(fake: _FakeIncidents) -> SimpleNamespace:
    return SimpleNamespace(db={"reliability_incidents": fake})


def make_ctx(repo, params: dict, run_id: str = "run",
             branch: str = "tier-s/t-wip") -> _Ctx:
    ctx = _Ctx(
        ticket={"task_id": "t", "ref": "t", "params": dict(params)},
        repo=repo, ref="t", run_id=run_id,
        branch=branch, base_branch="main",
        budgets={"files_per_ticket": 8, "lines_per_ticket": 400,
                 "hard_brake_factor": 2, "gate_retries": 2},
        timeouts={"build": 60}, waiting_cfg={}, auto_merge=False,
        budget_exclude=[], gate_specs=[], roles={})
    ctx.base_sha = git(repo, "rev-parse", "HEAD")
    ctx.brake_phase = "build"
    return ctx


def make_cycle(runs, tasks, executor, incidents=None) -> TierSCycle:
    return TierSCycle(runs, InMemoryLockStore(), tasks, executor,
                      incidents=incidents)


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

    async def run(self, role, prompt, workdir, timeout_s):
        verdict = self._verdicts.pop(0)
        return ExecResult(0, 1, output_tail=verdict.model_dump_json())

    async def cleanup(self, role) -> None:
        return None


@run_async
async def test_same_head_different_tree_is_not_same_work(repo):
    """HEAD igual con working tree distinto → no es el mismo trabajo."""
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    for _ in range(3):
        stop = await cycle._reliability_boundary(
            ctx, "config", "boom estable", None)
        assert stop is None  # shadow: would-stop lets the loop continue
    stalls = [doc for doc in fake.docs.values() if doc["kind"] == "stall"]
    assert len(stalls) == 1
    assert stalls[0]["engine"]["guard_rule"] == "G1"
    assert stalls[0]["shadow"] is True
    (repo / "tracked.txt").write_text("trabajo nuevo\n")
    stop = await cycle._reliability_boundary(ctx, "config", "boom estable",
                                             None)
    assert stop is None
    assert len([doc for doc in fake.docs.values()
                if doc["kind"] == "stall"]) == 1
    window = tasks.patches["t"]["params"]["progress_guard"]["window"]
    assert len(window) == 4


@run_async
async def test_streak_continues_across_runs(repo):
    """La racha repartida entre dos runs (o un reinicio) continúa."""
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    first = make_ctx(repo, {}, run_id="run-1")
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))
    assert await cycle._reliability_boundary(
        first, "config", "boom estable", None) is None
    carried = dict(tasks.patches["t"]["params"])
    assert len(carried["progress_guard"]["window"]) == 1
    second = make_ctx(repo, carried, run_id="run-2")
    assert await cycle._reliability_boundary(
        second, "config", "boom estable", None) is None
    window = tasks.patches["t"]["params"]["progress_guard"]["window"]
    assert len(window) == 2
    assert second.ticket["params"]["progress_guard"]["generation"] == 0
    assert len(fake.docs) == 2
    assert {doc["kind"] for doc in fake.docs.values()} == {"terminal_failure"}


@run_async
async def test_build_done_without_diff_is_a_valid_milestone(repo):
    """Verificación aceptada sin diff → hito válido (acredita el hito)."""
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text("cambio mínimo verificado")
    cycle = make_cycle(runs, tasks, ScriptedExecutor([done_verdict()]))
    ctx = make_ctx(repo, {})
    assert await cycle._build(ctx, demo_plan()) is True
    assert brake_mod.MILESTONE_BUILD_DONE in ctx.pending_milestones
    assert await cycle._reliability_boundary(
        ctx, "config", "sonda", None) is None
    state = tasks.patches["t"]["params"]["progress_guard"]
    assert state["milestones"] == [brake_mod.MILESTONE_BUILD_DONE]
    assert len(state["window"]) == 1


@run_async
async def test_agent_blocked_captures_terminal_failure(repo):
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    assert await cycle._reliability_boundary(
        ctx, "agent_blocked", "bloqueado por revisión", None) is None
    assert len(fake.docs) == 1
    doc = next(iter(fake.docs.values()))
    assert doc["kind"] == "terminal_failure"
    assert doc["component"] == "tier_s"
    assert doc["engine"]["reason_code"].startswith("agent_blocked:")


@run_async
async def test_credential_terminal_failure_attributes_environment(repo):
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    assert await cycle._reliability_boundary(
        ctx, "config",
        "invalid API key: auth failed (credential expired)", None) is None
    doc = next(iter(fake.docs.values()))
    assert doc["kind"] == "terminal_failure"
    assert doc["engine"]["reason_code"].startswith("auth:")
    assert doc["attribution"] == "environment"


@run_async
async def test_exhausted_brakes_capture_with_brake_name(repo):
    cases = [
        ("needs_owner", "scheduled_wait declined 3 times in a row: x",
         "scheduled_wait"),
        ("needs_owner", "review pool short of quorum 4 times; restore", "quorum"),
        ("needs_owner", "gates failed after 2 retries: g1", "gates"),
        ("needs_owner", "fix1 gates failed: g1", "correction"),
        ("agent_blocked", "builder failed: exit 1", "provider"),
    ]
    for reason, detail, brake in cases:
        fake = _FakeIncidents()
        runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
        cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                           incidents=incidents_source(fake))
        git(repo, "checkout", "-qb", f"tier-s/t-{brake}")
        ctx = make_ctx(repo, {}, branch=f"tier-s/t-{brake}")
        assert await cycle._reliability_boundary(
            ctx, reason, detail, None) is None
        assert len(fake.docs) == 1, brake
        doc = next(iter(fake.docs.values()))
        assert doc["kind"] == "brake_exhausted", brake
        assert doc["engine"]["brake"] == brake, brake


@run_async
async def test_self_healing_wait_counts_nothing(repo):
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    resume_at = (datetime.now(timezone.utc)
                 + timedelta(minutes=15)).isoformat()
    assert await cycle._reliability_boundary(
        ctx, "scheduled_wait", "ventana de observación", resume_at) is None
    assert fake.docs == {}
    assert tasks.patches["t"]["params"]["progress_guard"]["window"] == []


@run_async
async def test_gate_fixed_milestone_on_recovered_gate(repo):
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]))
    ctx = make_ctx(repo, {})
    cycle._record_gates(ctx, "build", [GateResult("g1", "fail", True, 0.1)])
    assert brake_mod.MILESTONE_GATE_FIXED not in ctx.pending_milestones
    cycle._record_gates(ctx, "fix1", [GateResult("g1", "pass", True, 0.1)])
    assert brake_mod.MILESTONE_GATE_FIXED in ctx.pending_milestones


@run_async
async def test_all_orchestrator_milestones_credit_in_order(repo):
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]))
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    for name in (brake_mod.MILESTONE_PLAN_READY,
                 brake_mod.MILESTONE_BUILD_DONE,
                 brake_mod.MILESTONE_GATE_FIXED,
                 brake_mod.MILESTONE_REVIEW_REPORT,
                 brake_mod.MILESTONE_FINDING_RESOLVED):
        cycle._note_tier_s_milestone(ctx, name)
    assert await cycle._reliability_boundary(
        ctx, "needs_owner", "pregunta del Owner", None) is None
    state = tasks.patches["t"]["params"]["progress_guard"]
    assert state["milestones"] == [
        brake_mod.MILESTONE_PLAN_READY, brake_mod.MILESTONE_BUILD_DONE,
        brake_mod.MILESTONE_GATE_FIXED, brake_mod.MILESTONE_REVIEW_REPORT,
        brake_mod.MILESTONE_FINDING_RESOLVED]
    assert len(state["window"]) == 1


class RaisingExecutor:
    async def run(self, role, prompt, workdir, timeout_s):
        raise RuntimeError("boom-376")

    async def cleanup(self, role) -> None:
        return None


def exc_ticket(params: dict) -> dict:
    return {
        "task_id": "t-376-exc",
        "ref": "TASK-2026-99999",
        "title": "tope de espera",
        "description": "observar",
        "acceptance": ["salud verificada"],
        "coding_tool": "tier_s",
        "blocked_by": [],
        "origin": "owner",
        "params": dict(params),
    }


def exc_cfg(roles: dict) -> dict:
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
async def test_unhandled_exception_captures_and_parks_transient(repo):
    """La excepción no controlada se captura y el ciclo aparca en transient."""
    fake = _FakeIncidents()
    runs = InMemoryRunStore()
    tasks = InMemoryTaskStore()
    roles = {
        "planner": {"account": "a", "model": "m", "command": ["true"]},
        "plan_reviewer": {"account": "b", "model": "m", "command": ["true"]},
        "builder": {"account": "c", "model": "m", "command": ["true"]},
    }
    cycle = TierSCycle(runs, InMemoryLockStore(), tasks, RaisingExecutor(),
                       incidents=incidents_source(fake))
    outcome = await cycle.run(exc_ticket({}), repo, exc_cfg(roles),
                              gate_specs=[])
    assert outcome.status == "failed"
    assert outcome.reason == "transient"
    assert outcome.resume_at is not None
    assert len(fake.docs) == 1
    doc = next(iter(fake.docs.values()))
    assert doc["kind"] == "exception"
    assert doc["component"] == "tier_s"
    assert doc["engine"]["exc_type"] == "RuntimeError"
    assert outcome.params is not None
    assert outcome.params["progress_guard"]["window"] == []


@run_async
async def test_brake_failure_fails_open_and_records_reliability(repo,
                                                                monkeypatch):
    """Si el freno falla, el bucle sigue y hay un exception de reliability."""
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))

    def boom(state, observation, policy, *, now=None):
        raise RuntimeError("guardia rota")

    import reliability.guard as guard_mod
    monkeypatch.setattr(guard_mod, "evaluate", boom)
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    assert await cycle._reliability_boundary(
        ctx, "config", "boom estable", None) is None
    assert len(fake.docs) == 2
    by_component = {doc["component"] for doc in fake.docs.values()}
    assert by_component == {"reliability", "tier_s"}
    assert "guard_error" in tasks.patches["t"]["params"]


@run_async
async def test_enforce_stop_parks_without_resume_and_keeps_branch(
        repo, monkeypatch):
    """En enforce el freno detiene: sin resume_at, rama conservada, stall high."""
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))
    monkeypatch.setitem(RELIABILITY_DEFAULTS["guard"]["mode"], "tier_s",
                        "enforce")
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {}, branch="tier-s/t-wip")
    stop = None
    for _ in range(3):
        stop = await cycle._reliability_boundary(
            ctx, "config", "boom estable", None)
    assert stop is not None
    assert stop.reason == "stalled_no_progress"
    assert stop.resume_at is None
    assert stop.branch == "tier-s/t-wip"
    assert git(repo, "rev-parse", "--verify",
               "refs/heads/tier-s/t-wip").strip() != ""
    patch = tasks.patches["t"]
    assert patch["resume_at"] is None
    assert patch["waiting_reason"].startswith("stalled_no_progress: G1")
    stalls = [doc for doc in fake.docs.values() if doc["kind"] == "stall"]
    assert stalls and all(doc["shadow"] is False for doc in stalls)


@run_async
async def test_park_build_evaluates_boundary_once(repo):
    """_park_build evalúa la frontera una sola vez (sin doble incidente)."""
    fake = _FakeIncidents()
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t",
                           "status": "running"})
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]),
                       incidents=incidents_source(fake))
    ctx = make_ctx(repo, {})
    await cycle._park_build(ctx, "config", "boom estable")
    assert ctx.parked_outcome is not None
    assert ctx.parked_outcome.reason == "config"
    assert len(fake.docs) == 1
    doc = next(iter(fake.docs.values()))
    assert doc["kind"] == "terminal_failure"
    window = tasks.patches["t"]["params"]["progress_guard"]["window"]
    assert len(window) == 1
    assert tasks.patches["t"]["status"] == "waiting"
    assert "progress_guard" in tasks.patches["t"]["params"]


@run_async
async def test_milestone_survives_self_healing_wait(repo):
    """Un hito pendiente se acredita aunque el park lleve temporizador."""
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]))
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    cycle._note_tier_s_milestone(ctx, brake_mod.MILESTONE_PLAN_READY)
    resume_at = (datetime.now(timezone.utc)
                 + timedelta(minutes=15)).isoformat()
    assert await cycle._reliability_boundary(
        ctx, "scheduled_wait", "ventana de observación", resume_at) is None
    state = tasks.patches["t"]["params"]["progress_guard"]
    assert state["milestones"] == [brake_mod.MILESTONE_PLAN_READY]
    assert state["window"] == []
    assert ctx.pending_milestones == []


@run_async
async def test_milestone_clears_preexisting_window(repo):
    """Un hito posterior vacía una ventana con intentos fallidos."""
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    cycle = make_cycle(runs, tasks, ScriptedExecutor([]))
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    ctx = make_ctx(repo, {})
    assert await cycle._reliability_boundary(
        ctx, "config", "boom estable", None) is None
    assert await cycle._reliability_boundary(
        ctx, "config", "boom estable", None) is None
    assert len(tasks.patches["t"]["params"]["progress_guard"]["window"]) == 2
    cycle._note_tier_s_milestone(ctx, brake_mod.MILESTONE_BUILD_DONE)
    assert await cycle._reliability_boundary(
        ctx, "config", "sonda", None) is None
    state = tasks.patches["t"]["params"]["progress_guard"]
    assert state["milestones"] == [brake_mod.MILESTONE_BUILD_DONE]
    assert len(state["window"]) == 1
