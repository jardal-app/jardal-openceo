"""Closure children carry the delivery contract (TASK-2026-00173).

Contract fixed here:

- A corrective child created by the final close carries the same
  minimum verifiable contract as planner children: ``agent_hint``
  dev plus ``params`` with ``target_agent``, ``work_type`` and
  ``expected_artifacts`` (the anchored repro/fix files, deduplicated;
  criterion refs when the finding cites only criteria). Without it
  the fixed child parked the next final review in ``data_gaps``
  forever, and misrouted as non-dev. Provenance already rode along
  (``origin`` + ``objclose:`` source id); the gate itself is
  untouched.
- Full simulated cycle: final review with a REAL finding → fix child
  created → child fixed → second review → close without data gaps.
  The not-reproduced case closes with explicit evidence, not with a
  second child.

Simulated executors over real git; no models.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives import engine as engine_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _closure_expected_artifacts,
    run_final_review,
)
from objectives.service import create_internal_ticket  # noqa: E402
from orchestration.live import ExecResult  # noqa: E402
from orchestration.review import (  # noqa: E402
    InMemoryReviewStore,
    InMemorySlotStore,
    parse_pool,
)
from state import AgentName  # noqa: E402

REPORT = (
    "[WARN] [bug] tracked.txt:2 — changed line is bare → annotate\n"
    "> changed line\n"
    "REVIEW_DONE_objective_final_0\n"
)
CLEAN_REPORT = "No findings.\nREVIEW_DONE_ticket_1\n"

_POOL = [
    {"label": "r-deep", "family": "deepseek", "priority": 10},
    {"label": "r-qwen", "family": "qwen", "priority": 20},
    {"label": "r-glm", "family": "glm", "priority": 30},
]


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


class _CloseCycleExecutor:
    """Review reports per call; closer verdicts per close."""

    def __init__(self, repo: Path, reports: list[str],
                 verdicts: list[str | None]) -> None:
        self._repo = repo
        self._reports = reports
        self._verdicts = verdicts
        self._target: Path | None = None
        self.calls: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> ExecResult:
        del workdir, timeout_s
        self.calls.append(role)
        if role.startswith("review:"):
            index = min(len(self._reports) - 1, sum(
                1 for call in self.calls if call == role) - 1)
            return ExecResult(0, 2.0, output_tail=self._reports[index])
        assert role == "closer", role
        index = min(len(self._verdicts) - 1,
                    self.calls.count("closer") - 1)
        text = self._verdicts[index]
        if text is not None:
            target = self._target
            if target is None:
                for line in prompt.splitlines():
                    if line.startswith("Write ") \
                            and " with one line per finding" in line:
                        rel = line[len("Write "):].split(
                            " with one line")[0]
                        target = self._repo / rel.strip()
                        self._target = target
                        break
            assert target is not None, "closer prompt names no target"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return ExecResult(0, 1.0, output_tail="")

    async def cleanup(self, role: str) -> None:
        del role


def _close_env(repo: Path, tasks: dict[str, dict[str, Any]],
               fake: _CloseCycleExecutor,
               ) -> tuple[ObjectiveEnv, list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []
    listed = tasks

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        task_id = f"task-fix-{len(created) + 1}"
        created.append({"task_id": task_id, **dict(payload)})
        return {"task_id": task_id}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        if task_id in listed:
            listed[task_id].update(updates)

    async def notify(ref: str, text: str) -> None:
        del ref, text

    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(listed),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=fake,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=InMemorySlotStore(),
        reviews=InMemoryReviewStore(),
        inbox=None,
        notify=notify,
        repo=repo,
        pool=parse_pool(_POOL),
        # Static pool pinned: these tests target close mechanics, not
        # routing (TASK-2026-00403 covers the dynamic assembly).
        pool_raw=list(_POOL),
        review_cfg={"free_router": {"enabled": False}},
    )
    return env, created


def _parent(objective_id: str) -> dict[str, Any]:
    return {
        "task_id": "task-parent", "title": "t", "description": "d",
        "status": "converted", "project": "proj", "acceptance": [],
        "params": {"_mirrors": {"erpnext": {"name": "TASK-GROUP"}}},
        "objective_id": objective_id}


def _dev_child(task_id: str, objective_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id, "title": "w", "status": "completed",
        "objective_id": objective_id, "project": "proj",
        "acceptance": ["done"],
        "params": {"target_agent": "dev",
                   "expected_artifacts": ["code"]},
        "coding_tool": "tier_c"}


class ClosureChildContractTests(unittest.IsolatedAsyncioTestCase):
    async def _world(self, objective_id: str, reports: list[str],
                     verdicts: list[str | None],
                     ) -> tuple[ObjectiveEnv, list[dict[str, Any]],
                                _CloseCycleExecutor, dict[str, Any]]:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "tracked.txt").write_text(
            "before\nchanged line\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "change")
        tasks = {"task-parent": _parent(objective_id),
                 "kid-1": _dev_child("kid-1", "objective_id")}
        tasks["kid-1"]["objective_id"] = objective_id
        fake = _CloseCycleExecutor(repo, reports, verdicts)
        env, created = _close_env(repo, tasks, fake)
        await env.objectives.create_objective({
            "objective_id": objective_id, "project": "proj",
            "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
            "base_sha": base,
            "conversion_source_task_id": "task-parent",
            "erp_group_ref": "TASK-GROUP",
        })
        return env, created, fake, tasks

    async def test_fixed_child_closes_without_data_gaps(self) -> None:
        env, created, fake, tasks = await self._world(
            "OBJ-173A", [REPORT, CLEAN_REPORT],
            ["F1: REAL — confirmed on tracked.txt:2\n"])
        first = await run_final_review(env, "OBJ-173A")

        self.assertEqual(first["action"], "fix-tickets")
        self.assertEqual(len(created), 1)
        payload = created[0]
        self.assertTrue(payload["acceptance"])
        self.assertEqual(payload["agent_hint"], "dev")
        self.assertEqual(payload["coding_tool"], "tier_c")
        self.assertEqual(payload["params"]["target_agent"], "dev")
        self.assertEqual(payload["params"]["work_type"], "debugging")
        self.assertEqual(payload["params"]["expected_artifacts"],
                         ["tracked.txt"])
        self.assertEqual(payload["origin"], "objective_closure")
        self.assertTrue(
            str(payload["source_id"]).startswith("objclose:OBJ-173A:1:"))
        self.assertEqual(payload["parent_task"], "TASK-GROUP")
        self.assertTrue(engine_mod._is_dev_ticket({
            "agent_hint": payload["agent_hint"],
            "params": payload["params"]}))
        # Implement the child with the contract it was given.
        tasks[payload["task_id"]] = {
            "task_id": payload["task_id"], "title": payload["title"],
            "status": "completed", "project": "proj",
            "acceptance": list(payload["acceptance"]),
            "params": dict(payload["params"]),
            "coding_tool": payload["coding_tool"],
            "agent_hint": payload["agent_hint"],
            "objective_id": "OBJ-173A",
        }

        second = await run_final_review(env, "OBJ-173A")

        self.assertTrue(second["ok"])
        self.assertEqual(second["action"], "closed")
        self.assertEqual(
            sum(1 for call in fake.calls if call.startswith("review:")),
            6)  # the dev branch ran its pool on both reviews
        objective = await env.objectives.get_objective("OBJ-173A")
        assert objective is not None
        self.assertEqual(objective["status"], "closed")
        self.assertEqual(objective["final_reviews"], 2)
        parent = tasks["task-parent"]
        self.assertEqual(parent["status"], "completed")

    async def test_not_reproduced_closes_with_explicit_evidence(self) -> None:
        env, created, _fake, tasks = await self._world(
            "OBJ-173B", [REPORT, REPORT],
            ["F1: REAL — confirmed on tracked.txt:2\n",
             "F1: FALSO — not reproduced: tracked.txt:2 holds a comment, "
             "no bare line on this branch\n"])
        first = await run_final_review(env, "OBJ-173B")
        self.assertEqual(first["action"], "fix-tickets")
        self.assertEqual(len(created), 1)
        payload = created[0]
        # The executor reproduces, finds nothing, and closes with the
        # evidence recorded on the ticket.
        tasks[payload["task_id"]] = {
            "task_id": payload["task_id"], "title": payload["title"],
            "status": "completed", "project": "proj",
            "acceptance": list(payload["acceptance"]),
            "params": dict(payload["params"]),
            "coding_tool": payload["coding_tool"],
            "agent_hint": payload["agent_hint"],
            "objective_id": "OBJ-173B",
            "last_response_summary": (
                "NOT REPRODUCED: tracked.txt:2 holds a comment, no bare "
                "line; no code changed."),
        }

        second = await run_final_review(env, "OBJ-173B")

        self.assertTrue(second["ok"])
        self.assertEqual(second["action"], "closed")
        self.assertEqual(len(created), 1)  # no second child
        child = tasks[payload["task_id"]]
        self.assertIn("NOT REPRODUCED", child["last_response_summary"])
        objective = await env.objectives.get_objective("OBJ-173B")
        assert objective is not None
        self.assertEqual(objective["status"], "closed")


class ClosureArtifactsTests(unittest.TestCase):
    def test_anchor_paths_are_deduplicated(self) -> None:
        item = SimpleNamespace(anchors=[
            SimpleNamespace(path="a.py", criterion=None),
            SimpleNamespace(path="b.py", criterion=None),
            SimpleNamespace(path="a.py", criterion=None),
        ])
        self.assertEqual(_closure_expected_artifacts(item),
                         ["a.py", "b.py"])

    def test_criterion_only_findings_name_their_criteria(self) -> None:
        item = SimpleNamespace(anchors=[
            SimpleNamespace(path=None, criterion="ship"),
            SimpleNamespace(path=None, criterion="ship"),
        ])
        self.assertEqual(_closure_expected_artifacts(item),
                         ["criterion:ship"])

    def test_paths_win_over_criteria(self) -> None:
        item = SimpleNamespace(anchors=[
            SimpleNamespace(path="a.py", criterion=None),
            SimpleNamespace(path=None, criterion="ship"),
        ])
        self.assertEqual(_closure_expected_artifacts(item), ["a.py"])


class InternalTicketPreservesContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_params_and_hint_ride_through_business_first(self) -> None:
        captured: dict[str, Any] = {}

        async def create_task(request: Any) -> dict[str, Any]:
            captured["request"] = request
            return {"task_id": "task-new"}

        task_service = SimpleNamespace(create_task=create_task)
        payload = {
            "title": "[objective-final] MAJOR bug: p",
            "description": "d",
            "acceptance": ["reproducir el problema"],
            "coding_tool": "tier_c",
            "agent_hint": "dev",
            "params": {"target_agent": "dev", "work_type": "debugging",
                       "expected_artifacts": ["tracked.txt"]},
            "objective_id": "OBJ-173C",
            "origin": "objective_closure",
            "project": "proj",
            "source": "spillover",
            "source_id": "objclose:OBJ-173C:1:deadbeef",
            "autorun": True,
        }

        row = await create_internal_ticket(task_service, payload)

        self.assertEqual(row, {"task_id": "task-new"})
        request = captured["request"]
        self.assertEqual(request.params["expected_artifacts"],
                         ["tracked.txt"])
        self.assertEqual(request.params["target_agent"], "dev")
        self.assertEqual(request.agent_hint, AgentName.DEV)
        self.assertEqual(request.acceptance, ["reproducir el problema"])


class FinalReviewRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Dynamic pool assembly in objective final review (00403)."""

    async def test_assembly_called_with_final_run_id(self) -> None:
        env, _created, _fake, _tasks = \
            await ClosureChildContractTests._world(
                self, "OBJ-ROUTE", [REPORT, CLEAN_REPORT],
                ["F1: REAL — confirmed on tracked.txt:2\n"])
        real = engine_mod.wiring_mod.assemble_review_pool
        with patch.object(engine_mod.wiring_mod, "assemble_review_pool",
                          wraps=real) as spy:
            result = await run_final_review(env, "OBJ-ROUTE")
        self.assertEqual(result["action"], "fix-tickets")
        spy.assert_called_once()
        _args, kwargs = spy.call_args
        self.assertEqual(kwargs["run_id"], "final-OBJ-ROUTE")
        self.assertEqual(kwargs["round"], "0")
        self.assertEqual(kwargs["builder_family"], "muse")
        self.assertEqual(kwargs["review_cfg"],
                         {"free_router": {"enabled": False}})
        self.assertTrue(str(kwargs["receipts_dir"]).endswith(
            "objectives/OBJ-ROUTE/review_receipts"))

    async def test_routing_error_parks_config(self) -> None:
        from model_routing.review_wiring import ReviewRoutingError
        env, _c, _f, _t = await ClosureChildContractTests._world(
            self, "OBJ-CFG", [REPORT, CLEAN_REPORT],
            ["F1: REAL — confirmed on tracked.txt:2\n"])
        with patch.object(
                engine_mod.wiring_mod, "assemble_review_pool",
                side_effect=ReviewRoutingError("no snapshot")):
            result = await run_final_review(env, "OBJ-CFG")
        self.assertEqual(result["reason"], "config")
        stored = await env.objectives.get_objective("OBJ-CFG")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertTrue(
            str(stored["waiting_reason"]).startswith("config:"))


class RoutedReviewerTests(unittest.IsolatedAsyncioTestCase):
    """Fallback rotation across ranked models (00403)."""

    async def test_fallback_rotates_until_valid(self) -> None:
        from model_routing import review_wiring as wiring_mod
        slot = SimpleNamespace(label="free-review-1", priority=10)
        invalid = {"valid": False, "kind": "cooling_down",
                   "reason": "quota"}
        run_attempt = AsyncMock(side_effect=[invalid, {"valid": True}])
        slots = SimpleNamespace(set_slot=AsyncMock())
        nxt = SimpleNamespace(deployment_id="d2")
        derived = SimpleNamespace(label="free-review-1", priority=10)
        with patch.object(wiring_mod, "plan_fallback",
                          return_value=nxt) as fallback, \
             patch.object(wiring_mod, "sync_executor_pool") as sync, \
             patch.object(wiring_mod, "dynamic_entry",
                          return_value={"label": "free-review-1"}), \
             patch.object(wiring_mod, "derive_slot",
                          return_value=derived):
            outcome = await engine_mod._run_routed_reviewer(
                run_attempt, routing=SimpleNamespace(states={
                    slot.label: SimpleNamespace(effective=SimpleNamespace())}),
                slots=slots, executor=SimpleNamespace(),
                slot=slot, attempt=1, previous=None)
        self.assertTrue(outcome["valid"])
        self.assertEqual(run_attempt.await_count, 2)
        fallback.assert_called_once()
        sync.assert_called_once()
        run_attempt.assert_awaited_with(derived, None)

    async def test_no_fallback_returns_outcome(self) -> None:
        from model_routing import review_wiring as wiring_mod
        slot = SimpleNamespace(label="free-review-1", priority=10)
        invalid = {"valid": False, "kind": "denied", "reason": "cap"}
        run_attempt = AsyncMock(return_value=invalid)
        with patch.object(wiring_mod, "plan_fallback",
                          return_value=None), \
             patch.object(wiring_mod, "derive_slot", return_value=slot):
            outcome = await engine_mod._run_routed_reviewer(
                run_attempt, routing=SimpleNamespace(states={
                    slot.label: SimpleNamespace(effective=SimpleNamespace())}),
                slots=SimpleNamespace(set_slot=AsyncMock()),
                executor=SimpleNamespace(), slot=slot, attempt=1,
                previous=None)
        self.assertFalse(outcome["valid"])
        run_attempt.assert_awaited_once_with(slot, None)


if __name__ == "__main__":
    unittest.main()
