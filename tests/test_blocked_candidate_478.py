"""Un bloqueado no destruye el trabajo del builder (TASK-2026-00478).

TASK-2026-00474 (#519, tier_b/codexf): el builder implementó el incremento
y cerró `bloqueado` porque la suite global pedía Docker, que el sandbox no
tiene por diseño. Los tiers simples hicieron reset --hard + clean + branch -D
y 20 minutos de trabajo se perdieron. Contrato:

- antes de cualquier descarte el candidato queda en `<kind>/<rama>`, con su
  HANDOFF, nombrado en la evidencia y en `params.kept_work`;
- un `bloqueado` con trabajo en el árbol gana UN turno de continuación en el
  mismo run (hechos del entorno + gates): `hecho` sigue a gates y merge,
  `bloqueado` otra vez, un turno sin respuesta o sin veredicto dejan el
  veredicto original en pie;
- los prompts del builder (simples y Tier S) nombran los gates del proyecto y
  la ausencia de Docker.

Git real y ejecutores simulados; sin modelos, red ni Docker.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.builder_contract import (
    blocked_continuation_due,
    blocked_continuation_prompt,
    builder_environment_prompt,
    kept_work_note,
)
from agents.dev.session import (
    CodingStepResult,
    DevSessionManager,
    _SimpleBranch,
)
from agents.dev.tier_s.cycle import TierSCycle, _Ctx
from agents.dev.tier_s.models import GateSpec
from agents.dev.tier_s.plan import Plan, PlannedFile
from agents.dev.tier_s.stores import (
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives.agent_result import AGENT_RESULT_PROMPT
from orchestration.live import ExecResult
from state import CodingTool, TaskStatus

MOTIVO = "la suite global requiere contenedores indisponibles"


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


def manager() -> DevSessionManager:
    settings = SimpleNamespace(DEV_EXECUTION_MODE="cli")
    return DevSessionManager(settings, MagicMock(), repo=None)


def dev_project(repo, **dev) -> dict:
    return {"project": {"repos": [{"local_path": str(repo)}]}, "dev": dev}


def blocked(motivo: str = MOTIVO, **extra) -> dict:
    return {"resultado": "bloqueado", "motivo": motivo,
            "necesidad": "Docker para la suite global", **extra}


def hecho() -> dict:
    return {"resultado": "hecho", "motivo": "incremento listo",
            "entregables": ["tracked.txt"],
            "evidencia": "typecheck verde; suite con contenedores no ejecutable aquí"}


WORK = {"tracked.txt": "after\n",
        ".openceo/HANDOFF.md": "resumen del incremento implementado"}


class ScriptedBuilder:
    """Stand-in for ``dev._step_code``: each turn writes into the tree and answers."""

    def __init__(self, repo: Path, turns: list[dict]) -> None:
        self.repo = repo
        self.turns = list(turns)
        self.prefixes: list[str] = []
        self.retries: list[bool] = []

    async def __call__(self, task, config, tool, *, prompt_prefix: str = "",
                       retry: bool = True):
        self.prefixes.append(prompt_prefix)
        self.retries.append(retry)
        turn = self.turns.pop(0)
        for rel, text in turn.get("writes", {}).items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if turn.get("commit"):
            git(self.repo, "add", "-A")
            git(self.repo, "commit", "-qm", "builder commit")
        return CodingStepResult(
            step={"step": "code", "status": turn.get("status", "ok")},
            prompt="p", summary="s", error=turn.get("error"),
            agent_result=turn.get("verdict"),
            executor_link=turn.get("executor_link"),
            cost_run={"engine": "tier_b", "duration_s": 1.0})


async def run_simple(dev, repo, builder, tool=CodingTool.TIER_B, params=None,
                     **dev_cfg):
    dev._step_code = builder
    with patch("agents.dev.session.run_gates", return_value=[]):
        return await dev._run_simple_tool(
            {"task_id": "t-478", "title": "probe", "params": params or {}},
            dev_project(repo, **dev_cfg), tool, {})


def no_kept_branches(repo) -> bool:
    return git(repo, "branch", "--list", "blocked/*", "failed/*",
               "declined/*") == ""


# -- the candidate is kept ----------------------------------------------------


@run_async
async def test_blocked_twice_keeps_the_candidate_with_its_handoff(repo):
    builder = ScriptedBuilder(repo, [
        {"writes": WORK, "verdict": blocked("primero")},
        {"verdict": blocked("sigue sin credencial del cliente")},
    ])
    output = await run_simple(manager(), repo, builder)
    assert output.status == TaskStatus.FAILED
    assert len(builder.prefixes) == 2
    kept = output.params["kept_work"]
    assert kept["branch"].startswith("blocked/tier/tier_b-")
    assert kept["kind"] == "blocked" and kept["files"] == 1
    assert git(repo, "show", f"{kept['branch']}:tracked.txt") == "after"
    assert "resumen del incremento" in git(
        repo, "show", f"{kept['branch']}:.openceo/HANDOFF.md")
    assert kept["branch"] in output.error
    assert "work branch dropped" in output.error
    # The base is clean again and the work branch is gone.
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "tracked.txt").read_text() == "before\n"
    assert git(repo, "branch", "--list", "tier/*") == ""
    # The latest verdict and the whole trail ride in the output.
    assert output.params["agent_result"]["motivo"] == "sigue sin credencial del cliente"
    assert [step["step"] for step in output.steps] == [
        "code", "blocked_continuation", "code", "agent_return"]
    assert len(output.cost_runs) == 2


@run_async
async def test_blocked_without_work_keeps_nothing_and_asks_once(repo):
    builder = ScriptedBuilder(repo, [
        {"writes": {".openceo/HANDOFF.md": "solo nota"},
         "verdict": blocked("falta una credencial")}])
    output = await run_simple(manager(), repo, builder)
    assert output.status == TaskStatus.FAILED
    assert len(builder.prefixes) == 1
    assert "kept_work" not in output.params
    assert no_kept_branches(repo)
    assert "work branch dropped" in output.error
    assert "candidate kept" not in output.error


@run_async
async def test_no_aplica_keeps_the_work_without_a_second_turn(repo):
    builder = ScriptedBuilder(repo, [{
        "writes": WORK,
        "verdict": {"resultado": "no_aplica", "motivo": "no es de mi rol"}}])
    output = await run_simple(manager(), repo, builder)
    assert output.status == TaskStatus.FAILED
    assert len(builder.prefixes) == 1
    assert output.params["kept_work"]["branch"].startswith("declined/tier/")


@run_async
async def test_a_commit_made_by_the_builder_is_kept_too(repo):
    builder = ScriptedBuilder(repo, [
        {"writes": WORK, "commit": True, "verdict": blocked("a")},
        {"verdict": blocked("b")}])
    output = await run_simple(manager(), repo, builder)
    kept = output.params["kept_work"]["branch"]
    assert git(repo, "show", f"{kept}:tracked.txt") == "after"
    assert (repo / "tracked.txt").read_text() == "before\n"


@run_async
async def test_material_failure_keeps_the_candidate_and_says_where(repo):
    dev = manager()
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "tier/tier_b-t478-abc123")
    (repo / "tracked.txt").write_text("after\n")
    branch = _SimpleBranch(repo, "tier/tier_b-t478-abc123", "main", base, "t478")
    output = await dev._material_simple_failure(
        {"task_id": "t-478", "params": {}}, {}, CodingTool.TIER_B, branch,
        "gates failed after 2 retries: typecheck")
    kept = output.params["kept_work"]
    assert kept["branch"] == "failed/tier/tier_b-t478-abc123"
    assert git(repo, "show", f"{kept['branch']}:tracked.txt") == "after"
    assert kept["branch"] in output.error
    assert "Candidate reset to" in output.error
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "tracked.txt").read_text() == "before\n"


# -- one continuation turn ----------------------------------------------------


@run_async
async def test_hecho_after_the_turn_goes_to_gates_and_merge(repo):
    builder = ScriptedBuilder(repo, [
        {"writes": WORK, "verdict": blocked()},
        {"verdict": hecho()}])
    output = await run_simple(
        manager(), repo, builder,
        gates=[{"name": "typecheck", "cmd": "bun run typecheck"}],
        oracle_cmd="bun test {refs}")
    assert output.status == TaskStatus.COMPLETED, output.error
    assert builder.prefixes[0] == ""
    assert builder.retries == [True, False]
    turn = builder.prefixes[1]
    assert turn.startswith("CONTINUACIÓN TRAS TU VEREDICTO bloqueado")
    assert MOTIVO in turn and "Docker para la suite global" in turn
    assert "sin Docker" not in turn and "no tiene Docker" in turn
    assert "bun run typecheck" in turn and "bun test {refs}" in turn
    assert "resultado=hecho" in turn
    assert (repo / "tracked.txt").read_text() == "after\n"  # merged into main
    assert no_kept_branches(repo)
    assert "kept_work" not in output.params
    assert output.params["agent_result"]["resultado"] == "hecho"
    assert [step["step"] for step in output.steps].count("code") == 2
    assert any(step["step"] == "blocked_continuation" for step in output.steps)
    assert len(output.cost_runs) == 2


@run_async
async def test_a_turn_that_fails_leaves_the_original_verdict_standing(repo):
    builder = ScriptedBuilder(repo, [
        {"writes": WORK, "verdict": blocked()},
        {"status": "failed", "error": "provider exploded", "verdict": None}])
    output = await run_simple(manager(), repo, builder)
    assert output.status == TaskStatus.FAILED
    assert output.params["agent_result"]["motivo"] == MOTIVO
    assert "kept_work" in output.params
    assert len(output.cost_runs) == 2
    assert any(step["status"] == "failed" for step in output.steps
               if step["step"] == "code")


@run_async
async def test_a_turn_without_a_verdict_does_not_retract(repo):
    builder = ScriptedBuilder(repo, [
        {"writes": WORK, "verdict": blocked()},
        {"verdict": None}])
    output = await run_simple(manager(), repo, builder)
    assert output.status == TaskStatus.FAILED
    assert output.params["agent_result"]["motivo"] == MOTIVO
    assert "kept_work" in output.params


@run_async
async def test_a_scheduled_wait_keeps_its_checkpoint_path(repo):
    resume_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    builder = ScriptedBuilder(repo, [{
        "writes": WORK,
        "verdict": blocked("observación de 24 h", resume_at=resume_at)}])
    output = await run_simple(manager(), repo, builder)
    assert output.status == TaskStatus.WAITING
    assert len(builder.prefixes) == 1
    assert "dev_checkpoint" in output.params and "kept_work" not in output.params


@run_async
async def test_a_cancelled_ticket_gets_no_second_turn(repo):
    dev = manager()
    dev._task_cancelled = AsyncMock(return_value=True)
    builder = ScriptedBuilder(repo, [{"writes": WORK, "verdict": blocked()}])
    output = await run_simple(dev, repo, builder)
    assert output.status == TaskStatus.FAILED
    assert len(builder.prefixes) == 1
    assert "kept_work" in output.params


@run_async
async def test_hecho_first_never_asks_again(repo):
    builder = ScriptedBuilder(repo, [{"writes": WORK, "verdict": hecho()}])
    output = await run_simple(manager(), repo, builder)
    assert output.status == TaskStatus.COMPLETED, output.error
    assert builder.prefixes == [""]


@run_async
async def test_tier_c_executor_event_is_recorded_once_per_ticket(repo):
    dev = manager()
    dev._record_tier_c_event = AsyncMock()
    builder = ScriptedBuilder(repo, [
        {"writes": WORK, "verdict": blocked(), "executor_link": "muse"},
        {"verdict": hecho(), "executor_link": "muse"}])
    output = await run_simple(dev, repo, builder, tool=CodingTool.TIER_C)
    assert output.status == TaskStatus.COMPLETED, output.error
    events = [call.args[1] for call in dev._record_tier_c_event.await_args_list]
    assert events == ["started", "executor", "closed"]
    assert output.params["executor_link"] == "muse"


@run_async
async def test_a_stale_kept_work_from_an_older_attempt_is_not_carried(repo):
    builder = ScriptedBuilder(repo, [{"writes": WORK, "verdict": hecho()}])
    output = await run_simple(
        manager(), repo, builder,
        params={"kept_work": {"branch": "blocked/tier/old"}})
    assert output.status == TaskStatus.COMPLETED, output.error
    assert "kept_work" not in output.params


# -- the contract text --------------------------------------------------------


def test_continuation_is_due_only_for_a_plain_bloqueado_with_work():
    assert blocked_continuation_due(blocked(), True)
    assert not blocked_continuation_due(blocked(), False)
    assert not blocked_continuation_due(None, True)
    assert not blocked_continuation_due(hecho(), True)
    assert not blocked_continuation_due(
        {"resultado": "no_aplica", "motivo": "x"}, True)
    assert not blocked_continuation_due(
        blocked(resume_at="2099-01-01T00:00:00+00:00"), True)


def test_environment_prompt_names_gates_oracle_and_the_missing_docker():
    gates = [GateSpec("typecheck", "bun run typecheck"),
             GateSpec("lint", "bun run lint", required=False)]
    text = builder_environment_prompt(gates, "bun test {refs}")
    assert "no tiene Docker" in text
    assert "typecheck: `bun run typecheck`" in text
    assert "lint (opcional): `bun run lint`" in text
    assert "oráculo congelado del ticket, si declara uno: `bun test {refs}`" in text
    assert "no es un impedimento externo" in text and "resultado=hecho" in text
    bare = builder_environment_prompt([])
    assert "gates del proyecto" in bare and "`" not in bare


def test_continuation_prompt_informs_and_leaves_the_decision_to_the_builder():
    text = blocked_continuation_prompt(
        blocked(), [GateSpec("typecheck", "bun run typecheck")])
    assert text.endswith("\n\n")
    assert MOTIVO in text and "bun run typecheck" in text
    assert "resultado=hecho" in text and "resultado=bloqueado" in text
    assert "no lo rehagas" in text


def test_the_shared_verdict_rule_no_longer_pushes_unrunnable_checks_to_bloqueado():
    assert "Si dudas entre hecho y bloqueado sobre si terminaste lo pedido" in AGENT_RESULT_PROMPT
    assert "no es un impedimento: cierra hecho" in AGENT_RESULT_PROMPT


def test_kept_work_note_points_at_the_branch_and_the_diff():
    note = kept_work_note({"branch": "blocked/x", "base_sha": "a" * 40, "files": 3})
    assert note == f"candidate kept on branch blocked/x (3 file(s); git diff {'a' * 12}..blocked/x)"


def test_simple_tier_builder_prompt_carries_gates_and_the_docker_fact(repo):
    prompt = manager()._build_coding_prompt(
        {"task_id": "t", "title": "probe", "description": "d", "params": {}},
        dev_project(repo, gates=[{"name": "typecheck", "cmd": "bun run typecheck"}],
                    oracle_cmd="bun test {refs}"))
    assert "ENTORNO Y VERIFICACIÓN" in prompt
    assert "bun run typecheck" in prompt and "bun test {refs}" in prompt
    assert prompt.index("ENTORNO Y VERIFICACIÓN") < prompt.index("RESULTADO DEL AGENTE")


def test_a_broken_gates_block_does_not_break_the_prompt(repo):
    prompt = manager()._build_coding_prompt(
        {"task_id": "t", "title": "probe", "description": "d", "params": {}},
        dev_project(repo, gates="not-a-list"))
    assert "ENTORNO Y VERIFICACIÓN" in prompt


# -- Tier S -------------------------------------------------------------------


def tier_s_plan() -> Plan:
    return Plan(
        ticket="t", objective="", goal="implementar", acceptance=("ok",),
        files=(PlannedFile(path="tracked.txt", operation="modify", reason="x"),),
        contracts=(), decisions=(), steps=("implementar",),
        estimate_files=1, estimate_lines=10, out_of_scope=())


class TierSBuilder:
    """Role-aware scripted executor: each turn may write files, then answers."""

    def __init__(self, repo: Path, turns: list[dict]) -> None:
        self.repo = repo
        self.turns = list(turns)
        self.prompts: list[str] = []

    async def run(self, role, prompt, repo, timeout_s):
        self.prompts.append(prompt)
        turn = self.turns.pop(0)
        for rel, text in turn.get("writes", {}).items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return ExecResult(turn.get("rc", 0), 1,
                          output_tail=turn.get("tail", ""))

    async def cleanup(self, role) -> None:
        return None


def verdict_tail(verdict: dict) -> str:
    import json
    return "```agent-result\n" + json.dumps(verdict) + "\n```"


async def tier_s_build(repo, turns, gate_specs=None):
    runs, tasks = InMemoryRunStore(), InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t", "status": "running"})
    git(repo, "checkout", "-qb", "tier-s/t-wip")
    base = git(repo, "rev-parse", "main")
    executor = TierSBuilder(repo, turns)
    cycle = TierSCycle(runs, InMemoryLockStore(), tasks, executor)
    ctx = _Ctx(
        ticket={"task_id": "t", "ref": "t", "params": {}}, repo=repo, ref="t",
        run_id="run", branch="tier-s/t-wip", base_branch="main",
        budgets={"files_per_ticket": 8, "lines_per_ticket": 400,
                 "hard_brake_factor": 2, "gate_retries": 2},
        timeouts={"build": 60}, waiting_cfg={}, auto_merge=False,
        budget_exclude=[], gate_specs=list(gate_specs or []), roles={})
    ctx.base_sha = base
    return cycle, ctx, executor


@run_async
async def test_tier_s_builder_prompt_carries_gates_and_the_docker_fact(repo):
    cycle, ctx, executor = await tier_s_build(
        repo, [], [GateSpec("typecheck", "bun run typecheck")])
    prompt = cycle._builder_prompt(ctx, tier_s_plan())
    assert "ENTORNO Y VERIFICACIÓN" in prompt and "bun run typecheck" in prompt
    assert prompt.index("ENTORNO Y VERIFICACIÓN") < prompt.index("RESULTADO DEL AGENTE")


@run_async
async def test_tier_s_bloqueado_with_work_gets_one_turn_and_hecho_proceeds(repo):
    cycle, ctx, executor = await tier_s_build(repo, [
        {"writes": WORK, "tail": verdict_tail(blocked())},
        {"tail": verdict_tail(hecho())}],
        [GateSpec("typecheck", "true")])
    assert await cycle._build(ctx, tier_s_plan()) is True
    assert len(executor.prompts) == 2
    assert executor.prompts[1].startswith("CONTINUACIÓN TRAS TU VEREDICTO bloqueado")
    assert executor.prompts[1].endswith(executor.prompts[0])
    assert ctx.parked_outcome is None


@run_async
async def test_tier_s_bloqueado_twice_parks_with_the_work_checkpointed(repo):
    cycle, ctx, executor = await tier_s_build(repo, [
        {"writes": WORK, "tail": verdict_tail(blocked("primero"))},
        {"tail": verdict_tail(blocked("segundo"))}])
    assert await cycle._build(ctx, tier_s_plan()) is False
    assert len(executor.prompts) == 2
    outcome = ctx.parked_outcome
    assert outcome.reason == "agent_blocked" and outcome.detail == "segundo"
    assert git(repo, "show", "tier-s/t-wip:tracked.txt") == "after"
    kept = ctx.ticket["params"]["kept_work"]
    assert kept["branch"] == ctx.branch and kept["kind"] == "blocked"
    assert kept["files"] == 1 and kept["head_sha"] == ctx.head_sha


@run_async
async def test_tier_s_a_failed_turn_leaves_the_first_verdict_standing(repo):
    cycle, ctx, executor = await tier_s_build(repo, [
        {"writes": WORK, "tail": verdict_tail(blocked())},
        {"rc": 1, "tail": "provider exploded"}])
    assert await cycle._build(ctx, tier_s_plan()) is False
    outcome = ctx.parked_outcome
    assert outcome.reason == "agent_blocked" and outcome.detail == MOTIVO


@run_async
async def test_tier_s_bloqueado_without_work_asks_only_once(repo):
    cycle, ctx, executor = await tier_s_build(repo, [
        {"tail": verdict_tail(blocked("falta una credencial"))}])
    assert await cycle._build(ctx, tier_s_plan()) is False
    assert len(executor.prompts) == 1
    assert ctx.parked_outcome.reason == "agent_blocked"


# -- the planner learns where the work is -------------------------------------


def _return_stub():
    repo_mock = MagicMock()
    repo_mock.get_task = AsyncMock(
        return_value={"task_id": "task-1", "status": "failed"})
    service = MagicMock()
    service.plan_return = AsyncMock(return_value={"ok": True})
    stub = SimpleNamespace(_repo=repo_mock, _objective_service=service,
                           comment_on_ticket=AsyncMock(return_value=True))
    ticket = {"task_id": "task-1", "title": "Do", "agent": "dev",
              "status": "failed", "objective_id": "OBJ-1"}
    return stub, service, ticket


@run_async
async def test_the_planner_return_names_the_kept_branch():
    from task_service import TaskService
    stub, service, ticket = _return_stub()
    params = {"agent_result": blocked(),
              "kept_work": {"branch": "blocked/tier/x", "base_sha": "a" * 40,
                            "files": 2}}
    await TaskService._trigger_objective_return(stub, ticket, params)
    entry = service.plan_return.await_args.args[1]
    assert entry["necesidad"].startswith("Docker para la suite global | ")
    assert "candidate kept on branch blocked/tier/x" in entry["necesidad"]
    assert len(entry["necesidad"]) <= 500


@run_async
async def test_the_planner_return_is_unchanged_without_a_kept_branch():
    from task_service import TaskService
    stub, service, ticket = _return_stub()
    await TaskService._trigger_objective_return(
        stub, ticket, {"agent_result": blocked()})
    entry = service.plan_return.await_args.args[1]
    assert entry["necesidad"] == "Docker para la suite global"


# -- waits that resume keep the built work ------------------------------------


def _built_branch(repo, name="tier/tier_b-t478-wait"):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", name)
    (repo / "tracked.txt").write_text("after\n")
    (repo / ".openceo").mkdir(exist_ok=True)
    (repo / ".openceo" / "HANDOFF.md").write_text("resumen")
    return _SimpleBranch(repo, name, "main", base, "t478")


def test_a_wait_that_resumes_checkpoints_the_build_and_the_resume_restores_it(repo):
    from agents.dev.session import DevExecutionOutput
    dev = manager()
    branch = _built_branch(repo)
    output = DevExecutionOutput(status=TaskStatus.IN_PROGRESS)
    dev._park_branch_for_wait({"params": {"keep": 1}}, output, branch)
    assert output.params["keep"] == 1
    assert output.params["dev_checkpoint"]["branch"] == branch.branch
    assert output.params["dev_checkpoint"]["build_done"] is True
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "tracked.txt").read_text() == "before\n"
    restored = dev._restore_simple_checkpoint(
        repo, output.params["dev_checkpoint"], {"params": output.params})
    assert isinstance(restored, _SimpleBranch)
    assert restored.build_done is True
    assert (repo / "tracked.txt").read_text() == "after\n"


@run_async
async def test_a_resumed_build_checkpoint_skips_the_builder(repo):
    from agents.dev.session import DevExecutionOutput
    dev = manager()
    branch = _built_branch(repo)
    parked = DevExecutionOutput(status=TaskStatus.IN_PROGRESS)
    dev._park_branch_for_wait({"params": {}}, parked, branch)
    task = {"task_id": "t-478-resume", "title": "resume",
            "params": dict(parked.params)}
    config = dev_project(repo, gates=[{"name": "t", "cmd": "true"}])
    builder = AsyncMock(side_effect=AssertionError("builder re-executed"))
    dev._step_code = builder
    async def finish(_task, _config, _tool, output, _branch):
        output.status = TaskStatus.COMPLETED
        return output

    with patch.object(dev, "_finish_simple_tool",
                      AsyncMock(side_effect=finish)):
        out = await dev._run_simple_tool(
            task, config, CodingTool.TIER_B, {})
    builder.assert_not_awaited()
    assert out.status == TaskStatus.COMPLETED
    assert any("skipping the build turn" in step.get("detail", "")
               for step in out.steps)
    assert "dev_checkpoint" not in (out.params or {})


def test_a_red_baseline_retries_before_parking_for_the_owner(repo):
    from agents.dev.session import DevExecutionOutput
    dev = manager()
    config = dev_project(repo, gates=[{"name": "t", "cmd": "exit 1"}])
    first = dev._open_simple_branch(
        {"task_id": "t-1", "title": "x"}, config, CodingTool.TIER_B, repo)
    assert isinstance(first, DevExecutionOutput)
    assert first.status == TaskStatus.WAITING
    assert first.resume_at is not None
    assert first.params["baseline_retries"] == 1
    assert first.waiting_reason.startswith("baseline_red:")
    second = dev._open_simple_branch(
        {"task_id": "t-1", "title": "x",
         "params": {"baseline_retries": 2}},
        config, CodingTool.TIER_B, repo)
    assert isinstance(second, DevExecutionOutput)
    assert second.status == TaskStatus.WAITING
    assert second.waiting_reason.startswith("needs_owner: baseline_red:")
    assert second.resume_at is None


def test_a_wait_checkpoint_that_git_refuses_falls_back_to_the_old_drop(repo):
    from agents.dev.session import DevExecutionOutput
    dev = manager()
    branch = _built_branch(repo)
    dev._save_simple_checkpoint = MagicMock(side_effect=ValueError("refused"))
    output = DevExecutionOutput(status=TaskStatus.IN_PROGRESS)
    dev._park_branch_for_wait({"params": {}}, output, branch)
    assert output.params is None
    assert git(repo, "branch", "--list", branch.branch) == ""
    assert git(repo, "branch", "--show-current") == "main"


@run_async
async def test_a_gates_config_error_waits_without_losing_the_build(repo):
    from agents.dev.session import DevExecutionOutput
    dev = manager()
    branch = _built_branch(repo)
    output = await dev._finish_simple_tool(
        {"task_id": "t-478", "title": "probe", "params": {}},
        dev_project(repo, gates="not-a-list"), CodingTool.TIER_B,
        DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)
    assert output.status == TaskStatus.WAITING
    assert output.waiting_reason.startswith("config: gates:")
    assert output.params["dev_checkpoint"]["branch"] == branch.branch
    assert git(repo, "branch", "--list", branch.branch) == branch.branch


def test_a_restored_checkpoint_tells_the_builder_not_to_redo_its_work(repo):
    base = {"task_id": "t", "title": "probe", "description": "d"}
    plain = manager()._build_coding_prompt({**base, "params": {}}, dev_project(repo))
    resumed = manager()._build_coding_prompt(
        {**base, "params": {"dev_checkpoint": {"branch": "tier/x"}}},
        dev_project(repo))
    assert "REANUDACIÓN DESDE CHECKPOINT" not in plain
    assert "REANUDACIÓN DESDE CHECKPOINT" in resumed
    assert "no lo rehagas" in resumed


def test_a_failing_pre_commit_hook_cannot_block_keeping_the_work(repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    kept = manager()._keep_candidate(_built_branch(repo), "blocked")
    assert kept is not None
    assert git(repo, "show", f"{kept['branch']}:tracked.txt") == "after"


@run_async
async def test_a_single_attempt_turn_never_sleeps_through_provider_retries():
    dev = manager()
    dev._resolve_tier_executor = AsyncMock(return_value=SimpleNamespace(argv=["x"]))
    failing = CodingStepResult(
        step={"step": "code", "status": "failed"}, prompt="p", summary="",
        error="503 service unavailable", provider_retryable=True)
    dev._step_code_once = AsyncMock(return_value=failing)
    with patch("agents.dev.session.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await dev._step_code(
            {"task_id": "t"}, {}, CodingTool.TIER_B, retry=False)
    assert result.error == "503 service unavailable"
    assert dev._step_code_once.await_count == 1
    assert sleep.await_count == 0
