"""Documented verification with zero changes (TASK-2026-00566).

Contract fixed here:

- The planner marks verification/decision tickets whose legitimate
  success is zero changes with ``allow_empty_diff`` (DEV-only, rejects
  oracle and claimed findings); the flag travels to task params.
- Both dev engines close 0 files as COMPLETED only with the flag plus
  hecho+evidencia (gates still run; merge/production skipped as no-op;
  evidence + handoff excerpt persisted). Without the flag the
  no-changes guard stays intact so hecho+evidencia alone never closes.

Real git scratch repos, scripted executors; no models, no network.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from agents.dev.session import (  # noqa: E402
    DevExecutionOutput,
    DevSessionManager,
    _SimpleBranch,
)
from agents.dev.tier_s.cycle import (  # noqa: E402
    TierSCycle,
    _Ctx,
)
from agents.dev.tier_s.gates import GateSpec  # noqa: E402
from agents.dev.tier_s.plan import (  # noqa: E402
    Plan,
    PlannedFile,
    parse_plan,
    validate_plan,
)
from agents.dev.tier_s.stores import (  # noqa: E402
    InMemoryLockStore,
    InMemoryRunStore,
    InMemoryTaskStore,
)
from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.agent_result import (  # noqa: E402
    AGENT_RESULT_PROMPT,
    empty_diff_close_allowed,
)
from orchestration.live import ExecResult  # noqa: E402
from settings import Settings  # noqa: E402
from state import CodingTool, TaskStatus  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, errors="replace", check=True)
    return proc.stdout.strip()


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "t@e.invalid")
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _planned(**overrides: Any) -> planner_mod.PlannedTicket:
    data: dict[str, Any] = {
        "local_ref": "verify-1",
        "title": "Verify routing decision",
        "target_agent": "dev",
        "work_type": "verify",
        "goal": "Confirm the decision still holds",
        "why_now": "Close the verification",
        "acceptance_criteria": ["Decision recorded"],
        "expected_artifacts": ["ticket evidence"],
        "coding_tool": "tier_c",
        "tier_reason": "A single session owns the read-only check",
        "review": "off",
        "review_reason": "Documental verification",
    }
    data.update(overrides)
    return planner_mod.PlannedTicket.model_validate(data)


def _hecho_empty() -> dict[str, Any]:
    return {
        "resultado": "hecho",
        "motivo": "verified, nothing must change",
        "necesidad": "",
        "entregables": [],
        "evidencia": "schedule.py read-only confirms the decision",
        "resume_at": None,
    }


class PlannerFlagTests(unittest.TestCase):
    def test_dev_flag_without_oracle_is_valid(self) -> None:
        ticket = _planned(allow_empty_diff=True)
        self.assertTrue(ticket.allow_empty_diff)

    def test_non_dev_flag_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "allow_empty_diff"):
            _planned(target_agent="research", coding_tool=None,
                     tier_reason=None, review=None, review_reason=None,
                     allow_empty_diff=True)

    def test_oracle_with_flag_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "allow_empty_diff"):
            _planned(allow_empty_diff=True,
                     oracle={"type": "preexisting_tests",
                             "refs": [{"ref": "tests/x.py"}]})

    def test_claimed_findings_with_flag_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "allow_empty_diff"):
            _planned(allow_empty_diff=True, includes_findings=["fp1"])

    def test_flag_propagates_to_params_and_description(self) -> None:
        params = engine_mod._planned_params(_planned(allow_empty_diff=True))
        self.assertIs(params.get("allow_empty_diff"), True)
        desc = engine_mod._planned_description(
            _planned(allow_empty_diff=True))
        self.assertIn("EMPTY DIFF ALLOWED", desc)
        self.assertNotIn(
            "allow_empty_diff", engine_mod._planned_params(_planned()))

    def test_verdict_prompt_counts_a_recorded_decision(self) -> None:
        self.assertIn("cuenta como entregable", AGENT_RESULT_PROMPT)

    def test_both_planner_prompts_advertise_the_flag(self) -> None:
        prompt = engine_mod._planner_prompt({"anchor": {}}, "", [], "", [])
        self.assertIn('"allow_empty_diff?"', prompt)
        self.assertIn("zero changes are a valid success", prompt)
        pedido = engine_mod._pedido_prompt(
            {"task_id": "t-1", "title": "t", "description": "d"})
        self.assertIn('"allow_empty_diff?"', pedido)
        self.assertIn("Rejects oracle and includes_findings", pedido)

    def test_shared_helper_is_the_single_empty_diff_rule(self) -> None:
        allowed = {"allow_empty_diff": True}
        self.assertTrue(
            empty_diff_close_allowed(allowed, "hecho", "decision here"))
        self.assertFalse(empty_diff_close_allowed(allowed, "hecho", "  "))
        self.assertFalse(
            empty_diff_close_allowed(allowed, "bloqueado", "decision"))
        self.assertFalse(
            empty_diff_close_allowed({}, "hecho", "decision here"))
        self.assertFalse(empty_diff_close_allowed(
            {"allow_empty_diff": "yes"}, "hecho", "decision here"))
        self.assertFalse(
            empty_diff_close_allowed(None, "hecho", "decision here"))


class PlanValidationTests(unittest.TestCase):
    _EMPTY_PLAN = """TICKET: TASK-566
OBJECTIVE: none
GOAL: Verify the decision
ACCEPTANCE:
- decision recorded
FILES:
CONTRACTS:
- produces none
DECISIONS:
- D1 verification only
STEPS:
1. Read the code read-only
2. Record the decision
ESTIMATE: files=0 lines=0
OUT_OF_SCOPE:
- any code change
"""

    def test_empty_files_allowed_with_the_flag(self) -> None:
        repo = Path(tempfile.mkdtemp())
        plan = parse_plan(self._EMPTY_PLAN)
        self.assertEqual(plan.files, ())
        validation = validate_plan(
            plan, repo, 8, (), budget_lines=400,
            fallback_goal="g", fallback_acceptance=["a"],
            allow_empty_diff=True)
        self.assertTrue(validation.valid, validation.errors)

    def test_empty_files_rejected_without_the_flag(self) -> None:
        repo = Path(tempfile.mkdtemp())
        plan = parse_plan(self._EMPTY_PLAN)
        validation = validate_plan(
            plan, repo, 8, (), budget_lines=400,
            fallback_goal="g", fallback_acceptance=["a"])
        self.assertFalse(validation.valid)
        self.assertIn("missing FILES", "; ".join(validation.errors))


def _simple_manager() -> DevSessionManager:
    manager = DevSessionManager(Settings(), MagicMock())
    manager._task_service = None
    manager._rate_monitor = None
    manager._repo = None
    return manager


_SIMPLE_CONFIG = {
    "project": {"repos": [{"branch_base": "main"}]},
    "dev": {"tier_s": {"budgets": {"gate_retries": 0}},
            "auto_merge": True},
}
_HANDOFF = "# Handoff\nSHA:\nFiles changed:\nDone.\n"


class SimpleEmptyDiffTests(unittest.IsolatedAsyncioTestCase):
    async def _close(self, task_params: dict[str, Any],
                     verdict: dict[str, Any]) -> Any:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-qb", "work")
        handoff_dir = repo / ".openceo"
        handoff_dir.mkdir(exist_ok=True)
        (handoff_dir / "HANDOFF.md").write_text(
            _HANDOFF, encoding="utf-8")
        task = {"task_id": "task-566", "ref": "566", "title": "Verify",
                "coding_tool": "tier_c", "project": "p",
                "objective_id": "OBJ-566", "params": dict(task_params)}
        output = DevExecutionOutput(
            status=TaskStatus.IN_PROGRESS,
            params={"agent_result": dict(verdict)})
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="566")
        result = await _simple_manager()._finish_simple_tool(
            task, _SIMPLE_CONFIG, CodingTool.TIER_C, output, branch)
        return result, repo, base

    async def test_flag_with_hecho_evidencia_completes_without_files(
            self) -> None:
        result, repo, base = await self._close(
            {"allow_empty_diff": True}, _hecho_empty())
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.modified_files or [], [])
        self.assertEqual(_git(repo, "rev-parse", "HEAD"), base)
        self.assertEqual(_git(repo, "branch", "--list", "work"), "")
        production = [step for step in (result.steps or [])
                      if step.get("step") == "production"]
        self.assertTrue(production)
        self.assertEqual(production[0]["status"], "skipped")
        assert result.params is not None
        stashed = result.params.get("_objective_close") or {}
        self.assertEqual(stashed.get("files"), [])
        self.assertTrue(stashed.get("handoff_excerpt"))
        self.assertTrue(stashed.get("evidencia"))
        self.assertIn("Evidence:", result.last_response_summary or "")

    async def test_same_ticket_without_the_flag_stays_needs_owner(
            self) -> None:
        result, _, _ = await self._close({}, _hecho_empty())
        self.assertEqual(result.status, TaskStatus.WAITING)
        self.assertEqual(result.waiting_reason,
                         "needs_owner: executor produced no changes")

    async def test_flag_without_evidencia_stays_needs_owner(self) -> None:
        verdict = _hecho_empty()
        verdict["evidencia"] = "  "
        result, _, _ = await self._close(
            {"allow_empty_diff": True}, verdict)
        self.assertEqual(result.status, TaskStatus.WAITING)
        self.assertEqual(result.waiting_reason,
                         "needs_owner: executor produced no changes")


class _TierSBuilder:
    """Scripted builder: writes HANDOFF.md, touches no code, answers."""

    def __init__(self, repo: Path, tail: str) -> None:
        self.repo = repo
        self.tail = tail

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> ExecResult:
        del prompt, timeout_s
        assert role == "builder"
        tier_dir = self.repo / ".openceo"
        tier_dir.mkdir(exist_ok=True)
        (tier_dir / "HANDOFF.md").write_text(
            "HANDOFF T\nBehavior implemented: verified read-only\n",
            encoding="utf-8")
        return ExecResult(0, 1.0, output_tail=self.tail)

    async def cleanup(self, role: str) -> None:
        del role


def _tier_s_plan() -> Plan:
    return Plan(
        ticket="t", objective="", goal="verify", acceptance=("ok",),
        files=(PlannedFile(path="tracked.txt", operation="modify",
                            reason="x"),),
        contracts=(), decisions=(), steps=("verify",),
        estimate_files=1, estimate_lines=10, out_of_scope=())


class TierSBuildGuardTests(unittest.IsolatedAsyncioTestCase):
    async def _build(self, params: dict[str, Any]) -> Any:
        repo = _scratch_repo(self)
        runs = InMemoryRunStore()
        await runs.create_run(
            {"run_id": "run", "task_id": "t", "status": "running"})
        _git(repo, "checkout", "-qb", "tier-s/t-wip")
        base = _git(repo, "rev-parse", "main")
        tail = ("```agent-result\n" + json.dumps(_hecho_empty())
                + "\n```")
        cycle = TierSCycle(runs, InMemoryLockStore(),
                           InMemoryTaskStore(),
                           _TierSBuilder(repo, tail))
        ctx = _Ctx(
            ticket={"task_id": "t", "ref": "t", "params": dict(params)},
            repo=repo, ref="t", run_id="run",
            branch="tier-s/t-wip", base_branch="main",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400,
                     "hard_brake_factor": 2, "gate_retries": 0},
            timeouts={"build": 60}, waiting_cfg={},
            auto_merge=False, budget_exclude=[],
            gate_specs=[GateSpec("probe", "true")], roles={})
        ctx.base_sha = base
        ok = await cycle._build(ctx, _tier_s_plan())
        return ok, ctx

    async def test_flag_lets_zero_files_pass_the_build(self) -> None:
        ok, ctx = await self._build({"allow_empty_diff": True})
        self.assertTrue(ok)
        self.assertIsNone(ctx.parked_outcome)
        self.assertIn("empty_diff_evidence",
                      ctx.ticket.get("params") or {})

    async def test_same_ticket_without_the_flag_parks(self) -> None:
        ok, ctx = await self._build({})
        self.assertFalse(ok)
        assert ctx.parked_outcome is not None
        self.assertEqual(ctx.parked_outcome.reason, "needs_owner")
        self.assertIn("builder produced no changes",
                      ctx.parked_outcome.detail)


class TierSCloseTests(unittest.IsolatedAsyncioTestCase):
    async def _close(self, params: dict[str, Any]) -> Any:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-qb", "tier-s/t-wip")
        tier_dir = repo / ".openceo"
        tier_dir.mkdir(exist_ok=True)
        (tier_dir / "HANDOFF.md").write_text(_HANDOFF, encoding="utf-8")
        runs = InMemoryRunStore()
        await runs.create_run(
            {"run_id": "run", "task_id": "t", "status": "running"})
        tasks = InMemoryTaskStore()
        notices: list[str] = []

        async def _notify(ref: str, text: str) -> None:
            notices.append(f"{ref}: {text}")

        pushed: list[str] = []

        async def _push(repo_path: Path, branch: str) -> None:
            pushed.append(branch)

        cycle = TierSCycle(runs, InMemoryLockStore(), tasks,
                           _TierSBuilder(repo, ""),
                           notify=_notify, push=_push,
                           objective_env=object())
        ctx = _Ctx(
            ticket={"task_id": "t", "ref": "t", "title": "Verify",
                    "objective_id": "OBJ-566", "params": dict(params)},
            repo=repo, ref="t", run_id="run",
            branch="tier-s/t-wip", base_branch="main",
            budgets={"files_per_ticket": 8, "lines_per_ticket": 400,
                     "hard_brake_factor": 2, "gate_retries": 0},
            timeouts={"build": 60}, waiting_cfg={},
            auto_merge=True, budget_exclude=[],
            gate_specs=[GateSpec("probe", "true")], roles={})
        ctx.base_sha = base
        with patch.object(engine_mod, "on_ticket_closed",
                          new=AsyncMock()) as closed:
            outcome = await cycle._close(ctx)
        return outcome, ctx, repo, base, runs, tasks, notices, pushed, closed

    async def test_flag_with_carried_evidence_closes_without_files(
            self) -> None:
        evidence = "schedule.py read-only confirms the decision"
        (outcome, ctx, repo, base, runs, tasks, notices, pushed,
         closed) = await self._close(
             {"allow_empty_diff": True, "empty_diff_evidence": evidence})
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(ctx.final_sha, base)
        self.assertEqual(_git(repo, "rev-parse", "HEAD"), base)
        self.assertEqual(_git(repo, "branch", "--list", "tier-s/t-wip"), "")
        self.assertEqual(runs.runs["run"]["status"], "completed")
        self.assertEqual(tasks.patches["t"]["status"],
                         TaskStatus.COMPLETED.value)
        self.assertEqual(pushed, [])
        said = " ".join(notices)
        self.assertIn("Evidence:", said)
        self.assertIn(evidence, said)
        self.assertNotIn("Production:", said)
        closed.assert_awaited_once()
        _, _, close = closed.await_args.args
        self.assertEqual(close.files, [])
        self.assertEqual(close.evidencia, evidence)


if __name__ == "__main__":
    unittest.main()
