from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from agents.dev.session import CodingStepResult, DevExecutionOutput, DevSessionManager, _SimpleBranch
from agents.dev.tier_s.cycle import TierSCycle, _Ctx
from agents.dev.tier_s.stores import InMemoryLockStore, InMemoryRunStore, InMemoryTaskStore
from config_loader import ConfigLoader
from dev_session_service import DevSessionService
from objectives.agent_result import AgentResult, scheduled_resume
from orchestration.live import ExecResult, ProcessExecutor, _reviewer_output
from orchestration.provider_recovery import model_responded, transient_provider_error
from settings import Settings
from state import CodingTool, DevProvider, TaskStatus


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


def manager():
    settings = SimpleNamespace(DEV_EXECUTION_MODE="cli")
    return DevSessionManager(settings, MagicMock(), repo=None)


def wait_result():
    return AgentResult(resultado="bloqueado", motivo="Ventana de observación pendiente",
                       evidencia="Inicio y plazo conservados en el registro de despliegue",
                       resume_at=datetime.now(timezone.utc) + timedelta(hours=24))


@pytest.mark.parametrize("message", ["503 service unavailable", "fetch failed",
                                     "FreeUsageLimitError", "provider.quota"])
def test_provider_outages_are_retryable(message):
    assert transient_provider_error(message)


def test_ambiguous_model_error_needs_response_evidence():
    error = "model 'muse-spark' does not exist or you lack access"
    assert not transient_provider_error(error)
    assert transient_provider_error(error, responded=True)
    assert not transient_provider_error("subscription expired", responded=True)
    usage = json.dumps({"payload_type": "run.usage", "payload": {
        "models": [{"model": "muse-spark", "output_tokens": 12}]}})
    assert model_responded(usage, "muse-spark")
    assert not model_responded(usage, "a-different-model")


@run_async
async def test_role_retries_keep_request_model_and_sum_attempt_time(tmp_path):
    executor = ProcessExecutor()
    executor._run_once = AsyncMock(side_effect=[
        ExecResult(0, 1, model="m", slot="muse"),
        ExecResult(1, 2, output_tail="model does not exist or you lack access", model="m", slot="muse"),
        ExecResult(1, 3, output_tail="503 unavailable", model="m", slot="muse"),
        ExecResult(0, 4, output_tail="done", model="m", slot="muse")])
    with patch("orchestration.live.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await executor.run("builder", "first phase", tmp_path, 30)
        result = await executor.run("builder", "pending phase", tmp_path, 30)
    assert [c.args[0] for c in sleep.call_args_list] == [300, 900]
    assert result.elapsed_s == 9
    assert len(result.usage["provider_attempts"]) == 3
    assert all(c.args[0] == "builder" and c.args[2] == tmp_path
               for c in executor._run_once.call_args_list)
    assert executor._run_once.call_args_list[-1].args[1].endswith("pending phase")


@run_async
@pytest.mark.parametrize("role,error", [("builder", "invalid API key"),
                                        ("builder", "model not found"),
                                        ("finance", "503 unavailable")])
async def test_auth_config_and_effectful_agents_are_not_blindly_retried(tmp_path, role, error):
    executor = ProcessExecutor()
    executor._run_once = AsyncMock(return_value=ExecResult(1, 1, output_tail=error))
    with patch("orchestration.live.asyncio.sleep", new_callable=AsyncMock) as sleep:
        await executor.run(role, "request", tmp_path, 30)
    executor._run_once.assert_awaited_once()
    sleep.assert_not_awaited()


@run_async
async def test_provider_retries_are_bounded_and_cancelable(tmp_path):
    executor = ProcessExecutor()
    executor._run_once = AsyncMock(return_value=ExecResult(1, 1, output_tail="503 unavailable"))
    with patch("orchestration.live.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await executor.run("objective_planner", "request", tmp_path, 30)
    assert executor._run_once.await_count == 4
    assert [c.args[0] for c in sleep.call_args_list] == [300, 900, 1800]
    assert result.returncode == 1
    executor._run_once.reset_mock()
    with patch("orchestration.live.asyncio.sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await executor.run("builder", "request", tmp_path, 30)
    executor._run_once.assert_awaited_once()


def test_failure_keeps_stderr_alongside_partial_stdout():
    code, text = _reviewer_output("partial work", "503 unavailable", "muse", 1)
    assert code == 1 and "partial work" in text and "503 unavailable" in text


@run_async
async def test_inactive_or_commandless_role_never_spawns(tmp_path):
    for spec in ({"provider": "muse", "command": []},
                 {"provider": "qwen", "command": ["qwen"], "active": False}):
        executor = ProcessExecutor(roles={"builder": spec})
        with patch("orchestration.live.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            result = await executor._run_once("builder", "request", tmp_path, 1)
        assert result.returncode == 1
        spawn.assert_not_awaited()


def test_muse_stream_keeps_terminal_verdict_and_response_evidence():
    usage = {"payload_type": "run.usage", "payload": {
        "models": [{"model": "m", "output_tokens": 50}]}}
    terminal = {"payload_type": "run.terminal.completed", "payload": {
        "terminal": "completed", "text": '{"resultado":"hecho","motivo":"ok"}'}}
    stream = "\n".join(json.dumps(e) for e in [usage, terminal])
    assert model_responded(stream, "m")
    assert _reviewer_output(stream, "", "muse", 0) == (0, terminal["payload"]["text"])
    terminal["payload"]["terminal"] = "failed"
    assert _reviewer_output(json.dumps(terminal), "", "muse", 0)[0] == 1


def test_scheduled_wait_requires_blocked_and_timezone():
    verdict = wait_result()
    assert scheduled_resume(verdict) == verdict.resume_at
    for changes in ({"resultado": "hecho"}, {"resume_at": "2026-10-22T12:00:00"},
                    {"resume_at": 1000}):
        with pytest.raises(ValueError):
            AgentResult.model_validate({**verdict.model_dump(), **changes})
    with pytest.raises(ValueError):
        scheduled_resume(verdict, now=verdict.resume_at + timedelta(seconds=1))


@run_async
async def test_simple_wait_is_waiting_with_no_planner_return():
    dev = manager()
    verdict = wait_result()
    dev._step_code = AsyncMock(return_value=CodingStepResult(
        step={"step": "code", "status": "ok"}, prompt="p", summary="wait",
        agent_result=verdict.model_dump(mode="json")))
    output = await dev._run_simple_tool(
        {"task_id": "t", "params": {"target_agent": "dev"}}, {}, CodingTool.TIER_C, {})
    assert output.status == TaskStatus.WAITING
    assert output.resume_at == verdict.resume_at
    assert output.waiting_reason.startswith("scheduled_wait:")
    assert output.params["target_agent"] == "dev"


def test_simple_checkpoint_preserves_changes_and_evidence(repo):
    dev = manager()
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "tier/wait")
    (repo / "tracked.txt").write_text("after\n")
    (repo / ".openceo").mkdir()
    (repo / ".openceo" / "HANDOFF.md").write_text("Observation evidence")
    params = {}
    dev._save_simple_checkpoint(_SimpleBranch(repo, "tier/wait", "main", base, "t"), params)
    assert git(repo, "branch", "--show-current") == "main"
    assert (repo / "tracked.txt").read_text() == "before\n"
    restored = dev._restore_simple_checkpoint(repo, params["dev_checkpoint"], {"params": params})
    assert isinstance(restored, _SimpleBranch)
    assert (repo / "tracked.txt").read_text() == "after\n"
    assert (repo / ".openceo" / "HANDOFF.md").read_text() == "Observation evidence"
    assert git(repo, "status", "--porcelain") == ""


@run_async
async def test_s_build_checkpoint_and_wait_survive_session_mapping(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "tier-s/wait")
    (repo / "tracked.txt").write_text("partial\n")
    (repo / ".openceo").mkdir()
    (repo / ".openceo" / "PLAN.md").write_text("saved plan")
    runs = InMemoryRunStore()
    tasks = InMemoryTaskStore()
    await runs.create_run({"run_id": "run", "task_id": "t", "status": "running"})
    cycle = TierSCycle(runs, InMemoryLockStore(), tasks, MagicMock())
    ctx = _Ctx(ticket={"task_id": "t"}, repo=repo, ref="t", run_id="run",
               branch="tier-s/wait", base_branch="main", base_sha=base,
               budgets={}, timeouts={}, waiting_cfg={}, auto_merge=True,
               budget_exclude=[], gate_specs=[], roles={})
    verdict = wait_result()
    await cycle._park_build(ctx, "scheduled_wait", verdict.motivo,
                            resume_at=verdict.resume_at.isoformat(),
                            agent_result=verdict.model_dump(mode="json"))
    outcome = await cycle._run_waited(ctx)
    assert outcome.reason == "scheduled_wait"
    assert outcome.resume_at == verdict.resume_at.isoformat()
    assert outcome.agent_result["evidencia"] == verdict.evidencia
    saved = await runs.resumable_run("t")
    assert saved["checkpoint"]["phase"] == "build"
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "show", "tier-s/wait:tracked.txt") == "partial"
    assert (await cycle._resume_point("t", repo))["run_id"] == "run"


def test_expired_accounts_are_retained_but_disabled():
    root = Path(__file__).resolve().parents[1]
    config = ConfigLoader(Settings(_env_file=None, PROJECTS_DIR=str(root / "projects"))).load_project("jardal-openceo")
    slots = {s["label"]: s for s in config["review"]["reviewers"]}
    assert not slots["qwen-max"]["active"] and not slots["qwen-glm"]["active"]
    assert slots["reserve-glm-5.3-flash-free"]["active"]
    assert config["dev"]["providers"]["codex"]["command"][0].endswith("codexj")
    service = DevSessionService.__new__(DevSessionService)
    with pytest.raises(ValueError, match="disabled"):
        service._resolve_command(DevProvider.GLM, config)


@run_async
async def test_scheduled_wait_uses_exact_timer_and_respects_human_holds():
    from test_dispatcher import _FakeRepo, _FakeService, _dispatcher, _ticket, _NOW

    due = _NOW + timedelta(hours=24)
    record = _ticket("wait", status="waiting", waiting_reason="scheduled_wait: observe", resume_at=due)
    store = _FakeRepo([record])
    service = _FakeService(store)
    dispatcher = _dispatcher(store, service, [])
    assert await dispatcher._refresh_backup(_NOW, []) == due
    await store.patch_task("wait", {"params": {"owner_question_id": "human-question"}})
    assert await dispatcher._requeue_due_waiting(due) == 0
    await store.patch_task("wait", {"params": {}})
    assert await dispatcher._requeue_due_waiting(due) == 1


@run_async
async def test_generic_agent_wait_keeps_evidence_without_failing(tmp_path):
    from orchestration.generic_agent import run_generic_agent
    from test_generic_agent_205 import generic_task, scripted_executor, tiny_catalog, wiring_with

    verdict = wait_result()
    factory, calls = scripted_executor(ExecResult(0, 1, output_tail=verdict.model_dump_json()))
    wiring = wiring_with(tmp_path, tiny_catalog({"operations": {"enabled": True}}), factory)
    output = await run_generic_agent("operations", generic_task(), wiring)
    assert output["status"] == "waiting"
    assert output["resume_at"] == verdict.resume_at
    assert output["params"]["agent_result"]["evidencia"] == verdict.evidencia
    assert sum("prompt" in call for call in calls) == 1


@run_async
async def test_failed_planner_cannot_publish_valid_looking_partial_result(tmp_path):
    import unittest
    from objectives.engine import _plan_pedido_inner
    from test_single_planner_204 import _pedido_env, _ticket_decision

    env, planner, created, patches = _pedido_env(unittest.TestCase(), tmp_path, [_ticket_decision()])
    original = planner.run

    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        return SimpleNamespace(returncode=1, output_tail="503 unavailable")

    planner.run = crash
    output = await _plan_pedido_inner(env, {"task_id": "p", "title": "request", "params": {}})
    assert output["reason"] == "planner_error"
    assert not created
    assert planner.runs == 2
    assert "OUTPUT: write " in planner.prompts[-1]


@run_async
async def test_simple_provider_retries_preserve_executor_and_time():
    dev = manager()
    executor = SimpleNamespace(argv=["codexj"], label="tier_b")
    dev._resolve_tier_executor = AsyncMock(return_value=executor)
    dev._step_code_once = AsyncMock(side_effect=[
        CodingStepResult({}, "request", "", error="503", provider_retryable=True,
                         cost_run={"duration_s": 2}),
        CodingStepResult({}, "request", "done", cost_run={"duration_s": 3})])
    with patch("agents.dev.session.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await dev._step_code({"task_id": "t"}, {}, CodingTool.TIER_B)
    assert result.cost_run["duration_s"] == 5
    sleep.assert_awaited_once_with(300)
    dev._resolve_tier_executor.assert_awaited_once()
    assert all(c.kwargs["executor"] is executor for c in dev._step_code_once.call_args_list)


@run_async
async def test_timed_out_coding_process_is_reaped_and_prompt_not_logged():
    dev = manager()
    process = SimpleNamespace(returncode=None, kill=MagicMock(), wait=AsyncMock(),
                              communicate=AsyncMock(side_effect=asyncio.TimeoutError))
    with patch("agents.dev.session.asyncio.create_subprocess_exec", return_value=process):
        result = await dev._run_process_detailed(["fake-cli", "PRIVATE-PROMPT"], sandbox=False)
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    assert not result.ok and "PRIVATE-PROMPT" not in result.stderr


@run_async
async def test_verified_no_change_delivery_runs_review_without_empty_commit(repo):
    dev = manager()
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "tier/verification")
    (repo / ".openceo").mkdir()
    (repo / ".openceo" / "HANDOFF.md").write_text("HANDOFF t\nBehavior implemented: observed health\n")
    output = DevExecutionOutput(status=TaskStatus.IN_PROGRESS, params={"agent_result": {
        "resultado": "hecho", "motivo": "observación completa",
        "entregables": ["registro de observación"], "evidencia": "salud verificada"}})
    dev._maybe_simple_review = AsyncMock(return_value=None)
    result = await dev._finish_simple_tool(
        {"task_id": "t", "title": "verify", "params": {}}, {}, CodingTool.TIER_B,
        output, _SimpleBranch(repo, "tier/verification", "main", base, "t"))
    assert result.status == TaskStatus.COMPLETED
    dev._maybe_simple_review.assert_awaited_once()
    assert git(repo, "rev-parse", "HEAD") == base


@run_async
async def test_s_failed_builder_cannot_close_partial_changes(repo):
    from agents.dev.tier_s.plan import parse_plan
    from test_tier_s_final import PLAN_MD

    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "tier-s/failed")
    (repo / "tracked.txt").write_text("partial\n")
    runs = InMemoryRunStore()
    await runs.create_run({"run_id": "run", "task_id": "t", "status": "running"})
    cycle = TierSCycle(runs, InMemoryLockStore(), InMemoryTaskStore(),
                       SimpleNamespace(run=AsyncMock(return_value=ExecResult(1, 1, output_tail="503 unavailable"))))
    ctx = _Ctx(ticket={"task_id": "t"}, repo=repo, ref="t", run_id="run",
               branch="tier-s/failed", base_branch="main", base_sha=base,
               budgets={}, timeouts={}, waiting_cfg={}, auto_merge=True,
               budget_exclude=[], gate_specs=[], roles={})
    assert not await cycle._build(ctx, parse_plan(PLAN_MD))
    outcome = await cycle._run_waited(ctx)
    assert outcome.reason == "transient" and outcome.resume_at
    assert (await runs.resumable_run("t"))["checkpoint"]["phase"] == "build"
    assert git(repo, "rev-parse", "main") == base


@run_async
async def test_s_temporarily_cooling_pool_has_a_resume_date(repo):
    from orchestration.review import InMemorySlotStore, ReviewSlot

    slots = InMemorySlotStore()
    until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    await slots.set_slot("a", {"status": "cooling_down", "until": until})
    cycle = TierSCycle(InMemoryRunStore(), InMemoryLockStore(), InMemoryTaskStore(), MagicMock(), slots=slots)
    ctx = _Ctx(ticket={"task_id": "t"}, repo=repo, ref="t", run_id="run",
               branch="unused", base_branch="main", budgets={}, timeouts={},
               waiting_cfg={}, auto_merge=True, budget_exclude=[], gate_specs=[], roles={},
               pool=[ReviewSlot("a", "one"), ReviewSlot("b", "two"), ReviewSlot("c", "three")])
    assert await cycle._pool_gate(ctx)
    assert ctx.pool_resume_at


def test_scheduled_wait_cap_escalates_after_three_declines():
    from objectives.agent_result import SCHEDULED_WAIT_MAX_DECLINES
    dev = manager()
    verdict = wait_result().model_dump(mode="json")
    params: dict = {}
    for expected in range(1, SCHEDULED_WAIT_MAX_DECLINES):
        output = dev._return_agent_verdict(
            {"task_id": "t", "params": params}, CodingTool.TIER_B, None,
            CodingStepResult(step={"step": "code", "status": "ok"},
                             prompt="p", summary="wait",
                             agent_result=dict(verdict)),
            dict(params))
        assert output.status == TaskStatus.WAITING
        assert output.waiting_reason.startswith("scheduled_wait:")
        assert output.resume_at is not None
        params = dict(output.params)
        assert params["scheduled_wait_count"] == expected
    output = dev._return_agent_verdict(
        {"task_id": "t", "params": params}, CodingTool.TIER_B, None,
        CodingStepResult(step={"step": "code", "status": "ok"},
                         prompt="p", summary="wait",
                         agent_result=dict(verdict)),
        dict(params))
    assert output.status == TaskStatus.WAITING
    assert output.waiting_reason.startswith("needs_owner: scheduled_wait")
    assert output.resume_at is None
    assert output.params["scheduled_wait_count"] == SCHEDULED_WAIT_MAX_DECLINES


def test_scheduled_wait_count_resets_on_terminal_verdict():
    dev = manager()
    output = dev._return_agent_verdict(
        {"task_id": "t", "params": {"scheduled_wait_count": 2}},
        CodingTool.TIER_B, None,
        CodingStepResult(step={"step": "code", "status": "ok"},
                         prompt="p", summary="declined",
                         agent_result={"resultado": "bloqueado",
                                       "motivo": "sin espera"}),
        {"scheduled_wait_count": 2})
    assert output.status == TaskStatus.FAILED
    assert "scheduled_wait_count" not in (output.params or {})


@run_async
async def test_generic_agent_scheduled_wait_cap_escalates(tmp_path):
    from objectives.agent_result import SCHEDULED_WAIT_MAX_DECLINES
    from orchestration.generic_agent import run_generic_agent
    from test_generic_agent_205 import generic_task, scripted_executor, tiny_catalog, wiring_with

    params: dict = {}
    for expected in range(1, SCHEDULED_WAIT_MAX_DECLINES):
        verdict = wait_result()
        factory, _calls = scripted_executor(
            ExecResult(0, 1, output_tail=verdict.model_dump_json()))
        wiring = wiring_with(tmp_path, tiny_catalog({"operations": {"enabled": True}}), factory)
        output = await run_generic_agent(
            "operations", generic_task(params=dict(params)), wiring)
        assert output["status"] == "waiting"
        assert output["resume_at"] == verdict.resume_at
        params = dict(output["params"])
        assert params["scheduled_wait_count"] == expected
    verdict = wait_result()
    factory, _calls = scripted_executor(
        ExecResult(0, 1, output_tail=verdict.model_dump_json()))
    wiring = wiring_with(tmp_path, tiny_catalog({"operations": {"enabled": True}}), factory)
    output = await run_generic_agent(
        "operations", generic_task(params=dict(params)), wiring)
    assert output["status"] == "waiting"
    assert "needs_owner: scheduled_wait" in output["waiting_reason"]
    assert output.get("resume_at") is None
    assert output["params"]["scheduled_wait_count"] == SCHEDULED_WAIT_MAX_DECLINES


@run_async
async def test_generic_agent_past_resume_stays_rejected(tmp_path):
    from objectives.agent_result import AgentResult
    from orchestration.generic_agent import run_generic_agent
    from test_generic_agent_205 import generic_task, scripted_executor, tiny_catalog, wiring_with

    stale = AgentResult(
        resultado="bloqueado", motivo="la ventana ya paso",
        resume_at=datetime.now(timezone.utc) - timedelta(hours=1))
    factory, _calls = scripted_executor(
        ExecResult(0, 1, output_tail=stale.model_dump_json()))
    wiring = wiring_with(tmp_path, tiny_catalog({"operations": {"enabled": True}}), factory)
    output = await run_generic_agent("operations", generic_task(), wiring)
    assert output["status"] == "failed"
    assert output.get("resume_at") is None


@run_async
async def test_s_no_change_close_preserves_review_and_creates_no_empty_commit(repo):
    from test_tier_s_final import HANDOFF_MD

    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "tier-s/verification")
    (repo / ".openceo").mkdir()
    (repo / ".openceo" / "HANDOFF.md").write_text(HANDOFF_MD)
    runs = InMemoryRunStore()
    await runs.create_run({"run_id": "run", "task_id": "t", "status": "running"})
    cycle = TierSCycle(runs, InMemoryLockStore(), InMemoryTaskStore(), MagicMock())
    cycle._review_rounds = AsyncMock(return_value=None)
    cycle._ticket_knowledge_note = AsyncMock()
    cycle._objective_trigger = AsyncMock()
    ctx = _Ctx(ticket={"task_id": "t", "title": "Verify"}, repo=repo, ref="t", run_id="run",
               branch="tier-s/verification", base_branch="main", base_sha=base,
               budgets={}, timeouts={}, waiting_cfg={}, auto_merge=True,
               budget_exclude=[], gate_specs=[], roles={})
    result = await cycle._close(ctx)
    assert result.status == "completed"
    cycle._review_rounds.assert_awaited_once()
    assert git(repo, "rev-parse", "HEAD") == base
