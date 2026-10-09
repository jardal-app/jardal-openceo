"""Promoted stubs are completed by the single planner (TASK-2026-00174).

Contract fixed here:

- A Horizon promotion linked to the exactly-one active objective used
  to park forever: a tierless child with no acceptance or artefacts
  (``routing_missing`` has no self-wake, and the delivery gate would
  park it later even with a manual tier). The stub now stays a stub
  on purpose: the single planner completes its contract
  (``completes_task_id`` adoption entries) instead of Horizon
  inventing tiers or artefacts it cannot ground.
- Adoption, not recreation: completions patch the listed task by id
  (tier, routing hint, acceptance, machine params merged over the
  promotion provenance); recreating a stub trips the existing
  duplicate-title check. Completions take no deps, claim no
  findings, and dev completions must name their tier. A healed
  routing hold refloats to queued and wakes the dispatcher once.
- Promotion triggers one planning cycle after a linked creation and
  re-drives it on retry while the stub stays tierless; zero/multiple
  objectives stay loose and untriggered. Only the closed spec
  travels, as before.

Simulated planners over real git; no models.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

TESTS_DIR = Path(__file__).resolve().parent
CORE_DIR = TESTS_DIR.parent / "openceo-core"
for path in (CORE_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from agents.dev.routing import resolve_coding_tool  # noqa: E402
from dispatcher import Dispatcher  # noqa: E402
from horizon.service import HorizonService  # noqa: E402
from horizon_support import FakeHorizonRepo  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from objectives import planner_models as planner_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    _apply_contract_completions,
    _planner_prompt,
    plan_objective,
)
from objectives.service import ObjectiveService  # noqa: E402
from orchestration.live import ExecResult  # noqa: E402
from orchestration.review import (  # noqa: E402
    InMemoryReviewStore,
    InMemorySlotStore,
    parse_pool,
)
from state import TaskStatus  # noqa: E402
from test_dispatcher import (  # noqa: E402
    _FakeMonitor,
    _FakeRepo as _DispatchRepo,
    _FakeService as _DispatchService,
    _dispatcher,
    _drain,
    _settings as _dispatch_settings,
)
from test_horizon_promotion import (  # noqa: E402
    CANARY_FAMILY,
    CANARY_FINANCE,
    CANARY_MISSION,
    CANARY_OBJECTIVE,
    FakeCompanyConfig,
    FakeTaskService,
    _action,
    _settings as _horizon_settings,
)


def _planned(local_ref: str = "t1", **overrides: Any) -> Any:
    base: dict[str, Any] = {
        "local_ref": local_ref,
        "title": "Do the thing",
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": "g",
        "why_now": "now",
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_c",
        "tier_reason": "default tier for specified work",
    }
    base.update(overrides)
    return planner_mod.PlannedTicket(**base)


def _result(*tickets: Any) -> Any:
    return planner_mod.PlannerResult(
        objective_status="active",
        state_summary="plan",
        tickets=list(tickets),
    )


async def _validate(result: Any, **overrides: Any) -> list[str]:
    kw: dict[str, Any] = {
        "agents": {"dev": {"enabled": True}},
        "open_titles": [],
        "task_exists": AsyncMock(return_value=True),
        "horizon": None,
    }
    kw.update(overrides)
    return await planner_mod.validate_planner_result(result, **kw)


class CompletionValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completions_only_plan_is_valid(self) -> None:
        result = _result(_planned(completes_task_id="task-stub"))
        self.assertEqual(await _validate(result), [])

    async def test_empty_plan_stays_invalid(self) -> None:
        errors = await _validate(_result())
        self.assertTrue(any("at least 1" in item for item in errors))

    async def test_dev_completion_without_tier_is_rejected(self) -> None:
        # The Pydantic routing contract enforces tier presence for dev
        # entries, completions included: a tierless dev completion never
        # parses, so the stub cannot stay tierless through it.
        payload = {
            "objective_status": "active",
            "state_summary": "plan",
            "tickets": [{
                "local_ref": "c1",
                "title": "Promoted stub work",
                "target_agent": "dev",
                "work_type": "implementation",
                "goal": "g",
                "why_now": "now",
                "acceptance_criteria": ["done"],
                "expected_artifacts": ["code"],
                "tier_reason": "default tier",
                "completes_task_id": "task-stub",
            }],
        }
        with self.assertRaises(ValueError) as ctx:
            planner_mod.parse_planner_result(json.dumps(payload))
        self.assertIn("require coding_tool", str(ctx.exception))

    async def test_completion_rejects_deps_and_findings(self) -> None:
        with_deps = _planned("a", completes_task_id="t1",
                             blocked_by=["t2"])
        with_findings = _planned("b", completes_task_id="t1",
                                 includes_findings=["fp"])
        errors = await _validate(_result(with_deps))
        self.assertTrue(any("no blocked_by" in item for item in errors))
        errors = await _validate(_result(with_findings))
        self.assertTrue(any("no findings" in item for item in errors))

    async def test_completion_for_unknown_task_is_rejected(self) -> None:
        result = _result(_planned(completes_task_id="task-ghost"))
        errors = await _validate(
            result, task_exists=AsyncMock(return_value=False))
        self.assertTrue(any("completes unknown" in item for item in errors))

    async def test_recreating_a_stub_trips_duplicate_titles(self) -> None:
        result = _result(_planned(title="Promoted stub work"))
        errors = await _validate(
            result, open_titles=["Promoted stub work"])
        self.assertTrue(any("duplicates an open ticket" in item
                            for item in errors))

    async def test_horizon_bounds_creations_and_completions(self) -> None:
        # Custom horizon [1, 2]: completions drain backlog without
        # consuming the creation budget (the structural 3-entry cap
        # still bounds the whole output).
        mixed = _result(*[_planned(f"t{i}", title=f"Work {i}")
                              for i in range(2)],
                        _planned("c1", completes_task_id="task-stub"))
        self.assertEqual(await _validate(mixed, horizon=[1, 2]), [])
        too_many_new = _result(*[_planned(f"t{i}", title=f"Work {i}")
                                 for i in range(3)])
        errors = await _validate(too_many_new, horizon=[1, 2])
        self.assertTrue(any("got 3 new" in item for item in errors))
        too_many_done = _result(*[
            _planned(f"c{i}", completes_task_id=f"t{i}") for i in range(3)])
        errors = await _validate(too_many_done, horizon=[1, 2])
        self.assertTrue(any("at most 2" in item for item in errors))

    async def test_non_dev_completion_needs_no_tier(self) -> None:
        ticket = _planned(completes_task_id="task-stub",
                          target_agent="research", coding_tool=None,
                          tier_reason=None)
        result = _result(ticket)
        errors = await _validate(
            result, agents={"research": {"enabled": True}})
        self.assertEqual(errors, [])

    async def test_creation_blocked_by_completion_ref_is_rejected(
            self) -> None:
        # A completion ref resolves to no creation key: depending on it
        # would store a dangling ref, so validation names the task to
        # depend on instead.
        dangling = _result(
            _planned("new", title="Follow-up work", blocked_by=["done"]),
            _planned("done", completes_task_id="task-stub"))
        errors = await _validate(dangling)
        self.assertTrue(
            any("blocked_by names completion 'done'" in item
                for item in errors))
        self.assertTrue(
            any("depend on 'task-stub' instead" in item for item in errors))
        # Depending on the completed task itself stays valid.
        anchored = _result(
            _planned("new", title="Follow-up work",
                     blocked_by=["task-stub"]),
            _planned("done", completes_task_id="task-stub"))
        self.assertEqual(await _validate(anchored), [])


class ApplyCompletionsTests(unittest.IsolatedAsyncioTestCase):
    def _env(self, tasks: dict[str, dict[str, Any]],
             ) -> tuple[ObjectiveEnv, MagicMock]:
        wakes = MagicMock()

        async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
            tasks[task_id].update(updates)

        env = ObjectiveEnv(
            objectives=InMemoryObjectiveStore(),
            tasks=InMemoryTaskLister(tasks),
            create_ticket=AsyncMock(return_value={"task_id": "t"}),
            patch_task=patch_task,
            executor=MagicMock(),
            quota=MagicMock(),
            slots=MagicMock(),
            reviews=None,
            inbox=None,
            notify=AsyncMock(),
            repo=Path.cwd(),
            dispatcher_wake=wakes,
        )
        return env, wakes

    def _stub(self, **overrides: Any) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "task_id": "task-stub", "title": "Promoted stub work",
            "status": "queued", "project": "acme",
            "agent_hint": "dev",
            "params": {"promoted_by": "horizon",
                       "horizon_action_id": "hact_1"},
            "objective_id": "OBJ-174",
        }
        doc.update(overrides)
        return doc

    async def test_completion_fills_the_contract(self) -> None:
        tasks = {"task-stub": self._stub()}
        env, wakes = self._env(tasks)
        result = _result(_planned(
            completes_task_id="task-stub",
            acceptance_criteria=["panel loads"],
            expected_artifacts=["web/panel.py"]))

        completed = await _apply_contract_completions(env, "OBJ-174", result)

        self.assertEqual(completed, ["task-stub"])
        stub = tasks["task-stub"]
        self.assertEqual(stub["coding_tool"], "tier_c")
        self.assertEqual(stub["agent_hint"], "dev")
        self.assertEqual(stub["acceptance"], ["panel loads"])
        self.assertEqual(stub["params"]["expected_artifacts"],
                         ["web/panel.py"])
        self.assertEqual(stub["params"]["target_agent"], "dev")
        self.assertEqual(stub["params"]["promoted_by"], "horizon")
        self.assertEqual(stub["params"]["horizon_action_id"], "hact_1")
        wakes.assert_called_once_with()

    async def test_routing_hold_refloats_to_queued(self) -> None:
        tasks = {"task-stub": self._stub(
            status="waiting", waiting_reason="routing_missing")}
        env, wakes = self._env(tasks)
        result = _result(_planned(completes_task_id="task-stub"))

        completed = await _apply_contract_completions(env, "OBJ-174", result)

        self.assertEqual(completed, ["task-stub"])
        stub = tasks["task-stub"]
        self.assertEqual(stub["status"], "queued")
        self.assertIsNone(stub.get("waiting_reason"))
        wakes.assert_called_once_with()

    async def test_other_waits_stay_put(self) -> None:
        tasks = {"task-stub": self._stub(
            status="waiting", waiting_reason="qa_review_pending")}
        env, wakes = self._env(tasks)
        result = _result(_planned(completes_task_id="task-stub"))

        completed = await _apply_contract_completions(env, "OBJ-174", result)

        self.assertEqual(completed, ["task-stub"])
        stub = tasks["task-stub"]
        self.assertEqual(stub["coding_tool"], "tier_c")
        self.assertEqual(stub["status"], "waiting")
        self.assertEqual(stub["waiting_reason"], "qa_review_pending")

    async def test_foreign_terminal_and_tiered_targets_skip(self) -> None:
        tasks = {
            "task-stub": self._stub(),
            "task-foreign": self._stub(task_id="task-foreign",
                                       objective_id="OBJ-OTHER"),
            "task-done": self._stub(task_id="task-done",
                                    status="completed",
                                    coding_tool="tier_c"),
            "task-tiered": self._stub(task_id="task-tiered",
                                      coding_tool="tier_b"),
        }
        env, wakes = self._env(tasks)
        first = _result(
            _planned("c1", completes_task_id="task-stub"),
            _planned("c2", completes_task_id="task-foreign"),
            _planned("c3", completes_task_id="task-done"),
        )
        second = _result(
            _planned("c4", completes_task_id="task-tiered"),
            _planned("c5", completes_task_id="task-ghost"),
        )

        completed = await _apply_contract_completions(env, "OBJ-174", first)
        completed += await _apply_contract_completions(env, "OBJ-174", second)

        self.assertEqual(completed, ["task-stub"])
        self.assertNotIn("coding_tool", tasks["task-foreign"])
        self.assertEqual(tasks["task-done"]["coding_tool"], "tier_c")
        self.assertEqual(tasks["task-tiered"]["coding_tool"], "tier_b")
        wakes.assert_called_once_with()

    async def test_prompt_lists_tierless_children(self) -> None:
        objective = {"objective_id": "OBJ-174",
                     "anchor": {"goal": "g", "acceptance": []}}
        stub = self._stub(description="company spec text")

        with_stubs = _planner_prompt(objective, "", [], "", [], tierless=[stub])
        self.assertIn("TIERLESS CHILDREN AWAITING CONTRACT", with_stubs)
        self.assertIn("task-stub", with_stubs)
        self.assertIn("company spec text", with_stubs)

        bare = _planner_prompt(objective, "", [], "", [])
        self.assertNotIn("TIERLESS CHILDREN", bare)


class PlanEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_objective_service_plan_guards(self) -> None:
        from task_service import TaskService as RealTaskService

        async def _get_objective(objective_id: str) -> dict[str, Any] | None:
            return {"objective_id": objective_id, "project": "ghost"}

        repo = SimpleNamespace(
            get_objective=_get_objective,
            patch_task=AsyncMock(),
        )
        config = SimpleNamespace(
            has_project=lambda project: False,
            load_project=lambda project: {},
        )
        service = ObjectiveService(
            repo, RealTaskService.__new__(RealTaskService), config,
            SimpleNamespace(TIER_S_OBJECTIVE_CHILD_AUTORUN=False))

        missing = await service.plan_objective("OBJ-NONE")
        self.assertFalse(missing["ok"])
        unknown = await service.plan_objective("OBJ-174")
        self.assertEqual(unknown["reason"], "unknown-project")

        stored = await ObjectiveService(
            None, RealTaskService.__new__(RealTaskService), config,
            SimpleNamespace(TIER_S_OBJECTIVE_CHILD_AUTORUN=False),
        ).plan_objective("OBJ-174")
        self.assertEqual(stored["reason"], "no-store")

    async def test_task_service_plan_delegates_or_reports(self) -> None:
        from task_service import TaskService as RealTaskService

        bare = RealTaskService.__new__(RealTaskService)
        via_none = await RealTaskService.plan_objective(bare, "OBJ-174")
        self.assertEqual(via_none["reason"], "no-objective-service")

        calls: list[str] = []

        async def _plan(objective_id: str) -> dict[str, Any]:
            calls.append(objective_id)
            return {"ok": True}

        bare._objective_service = SimpleNamespace(plan_objective=_plan)
        self.assertEqual(
            await RealTaskService.plan_objective(bare, "OBJ-174"),
            {"ok": True})
        self.assertEqual(calls, ["OBJ-174"])

    def test_public_wake_delegates_and_tolerates_missing(self) -> None:
        from task_service import TaskService as RealTaskService

        bare = RealTaskService.__new__(RealTaskService)
        bare.wake_dispatcher()  # no wiring: silent
        wakes: list[int] = []
        bare._dispatcher_wake = lambda: wakes.append(1)
        bare.wake_dispatcher()
        self.assertEqual(wakes, [1])


class _PlanningTaskService(FakeTaskService):
    """Promotion fake whose plans run the real engine, scripted planner."""

    def __init__(self, test: unittest.TestCase) -> None:
        super().__init__()
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.repo_path = Path(temp.name)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "t@e.invalid")
        (self.repo_path / "tracked.txt").write_text(
            "before\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "base")
        self.base_sha = self._git("rev-parse", "HEAD")
        (self.repo_path / "policy.md").write_text(
            "POLICY: adopt tierless stubs.\n", encoding="utf-8")
        self.objective_docs: dict[str, dict[str, Any]] = {}
        self.plan_calls: list[str] = []
        self.plan_payloads: list[dict[str, Any]] = []
        self.wakes = MagicMock()
        self.planner_created: list[dict[str, Any]] = []
        self._seq = 100

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=self.repo_path, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", check=True)
        return proc.stdout.strip()

    async def create_task(self, req: Any) -> dict[str, Any]:
        doc = await super().create_task(req)
        # Production tickets are born queued; autorun only gates
        # admission, never the initial status.
        self.tasks_by_id[doc["task_id"]]["status"] = "queued"
        doc["status"] = "queued"
        return doc

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        row = self.tasks_by_id.get(task_id)
        if row is None:
            return None
        row.update(updates)
        return dict(row)

    async def plan_objective(self, objective_id: str) -> dict[str, Any]:
        self.plan_calls.append(objective_id)
        payload = self.plan_payloads[
            min(len(self.plan_calls) - 1, len(self.plan_payloads) - 1)]

        last_target: list[Path] = []

        async def run(role: str, prompt: str, workdir: Path,
                      timeout_s: int) -> ExecResult:
            del workdir, timeout_s
            assert role == "objective_planner", role
            target: Path | None = None
            for line in prompt.splitlines():
                if line.startswith("OUTPUT: write "):
                    rel = line[len("OUTPUT: write "):].split(
                        " with exactly")[0]
                    target = self.repo_path / rel.strip()
            if target is None:
                # Retry prompt: fix the same file in place (same
                # contract as _RecordedPlanner in the planner-cycle
                # suite: only the first attempt names OUTPUT).
                assert last_target, \
                    "retry prompt without a previous OUTPUT target"
                target = last_target[-1]
            else:
                last_target.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload), encoding="utf-8")
            return ExecResult(0, 1.0, output_tail="PLANNER_READY")

        async def create_ticket(ticket_payload: dict[str, Any]) -> dict[str, Any]:
            self._seq += 1
            task_id = f"task-plan-{self._seq}"
            self.tasks_by_id[task_id] = {
                "task_id": task_id, "status": "queued",
                **dict(ticket_payload),
            }
            self.planner_created.append(dict(ticket_payload))
            return {"task_id": task_id}

        async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
            if task_id in self.tasks_by_id:
                self.tasks_by_id[task_id].update(updates)

        async def notify(ref: str, text: str) -> None:
            del ref, text

        objectives = InMemoryObjectiveStore()
        doc = self.objective_docs.get(objective_id)
        assert doc is not None
        await objectives.create_objective(dict(doc))
        env = ObjectiveEnv(
            objectives=objectives,
            tasks=InMemoryTaskLister(self.tasks_by_id),
            create_ticket=create_ticket,
            patch_task=patch_task,
            executor=SimpleNamespace(run=run, cleanup=AsyncMock()),
            quota=MagicMock(),
            slots=InMemorySlotStore(),
            reviews=InMemoryReviewStore(),
            inbox=None,
            notify=notify,
            repo=self.repo_path,
            planner_cfg={
                "timeout_minutes": 15,
                "policy_path": str(self.repo_path / "policy.md"),
                "agents": {"dev": {"enabled": True}},
            },
            pool=parse_pool([
                {"label": "r-a", "family": "fa", "priority": 10},
                {"label": "r-b", "family": "fb", "priority": 20},
            ]),
            dispatcher_wake=self.wakes,
        )
        result = await plan_objective(env, objective_id)
        stored = await objectives.get_objective(objective_id)
        assert stored is not None
        self.objective_docs[objective_id] = stored
        return result

    def seed_objective(self, objective_id: str, project: str) -> None:
        self.objective_docs[objective_id] = {
            "objective_id": objective_id,
            "project": project,
            "status": "active",
            "anchor": {"goal": "company goal",
                       "acceptance": ["ship the panel"]},
            "base_sha": self.base_sha,
            "conversion_source_task_id": "task-parent",
            "erp_group_ref": "TASK-2026-00999",
        }
        self.tasks_by_id["task-parent"] = {
            "task_id": "task-parent", "title": "t", "status": "converted",
            "project": project, "acceptance": [],
            "objective_id": objective_id,
        }


def _completion_payload(task_id: str) -> dict[str, Any]:
    return {
        "objective_status": "active",
        "state_summary": "adopt the promoted stub",
        "tickets": [{
            "local_ref": "adopt1",
            "title": "Promoted stub work",
            "target_agent": "dev",
            "work_type": "implementation",
            "goal": "g",
            "why_now": "now",
            "acceptance_criteria": ["panel loads in 2s"],
            "expected_artifacts": ["web/panel.py"],
            "coding_tool": "tier_c",
            "tier_reason": "default tier for specified work",
            "completes_task_id": task_id,
        }],
    }


class PromotionAdoptionTests(unittest.IsolatedAsyncioTestCase):
    async def _linked_world(self, payloads: list[dict[str, Any]],
                            action: dict[str, Any] | None = None,
                            ) -> tuple[HorizonService, _PlanningTaskService,
                                       FakeHorizonRepo, str]:
        repo = FakeHorizonRepo()
        svc = _PlanningTaskService(self)
        service = HorizonService(
            _horizon_settings(), repo, task_service=svc)
        service.set_company_config(FakeCompanyConfig())
        svc.seed_objective("obj_1", "acme")
        svc.plan_payloads.extend(payloads)

        async def _lister(project: str) -> list[dict[str, Any]]:
            del project
            return [{
                "objective_id": "obj_1",
                "project": "acme",
                "status": "active",
                "erp_group_ref": "TASK-2026-00999",
            }]

        repo.list_active_objectives = _lister  # type: ignore[attr-defined]
        stored = await repo.create_horizon_action(
            action if action is not None
            else _action(company_project="acme"))
        return service, svc, repo, stored["action_id"]

    def _q(self, svc: _PlanningTaskService, task_id: str) -> dict[str, Any]:
        row = svc.tasks_by_id.get(task_id)
        assert row is not None
        return row

    async def test_linked_stub_is_completed_dispatched_and_closed(self) -> None:
        svc_probe: _PlanningTaskService | None = None

        async def _payload_for_stub() -> dict[str, Any]:
            assert svc_probe is not None
            stubs = [row for row in svc_probe.tasks_by_id.values()
                     if row.get("objective_id") == "obj_1"
                     and not row.get("coding_tool")
                     and str(row.get("status") or "")
                     not in ("completed", "cancelled", "converted")]
            assert len(stubs) == 1
            return _completion_payload(stubs[0]["task_id"])

        # The planner payload needs the stub id, known only after the
        # promotion creates it: resolve lazily through the probe below.
        service, svc, _repo, aid = await self._linked_world([])
        svc_probe = svc
        svc.plan_payloads.append({})  # placeholder, replaced pre-plan
        original_plan = svc.plan_objective

        async def _plan_with_stub(objective_id: str) -> dict[str, Any]:
            svc.plan_payloads[-1] = await _payload_for_stub()
            return await original_plan(objective_id)

        svc.plan_objective = _plan_with_stub  # type: ignore[method-assign]
        result = await service.promote_action(
            "usr_owner", action_id=aid)

        self.assertTrue(result["promoted"])
        self.assertEqual(result["objective_id"], "obj_1")
        self.assertTrue(result["planning_triggered"])
        task_id = result["task_id"]
        self.assertEqual(result["contracts_completed"], [task_id])
        stub = self._q(svc, task_id)
        self.assertEqual(stub["coding_tool"], "tier_c")
        self.assertEqual(stub["acceptance"], ["panel loads in 2s"])
        self.assertEqual(stub["params"]["expected_artifacts"],
                         ["web/panel.py"])
        self.assertEqual(stub["params"]["promoted_by"], "horizon")
        self.assertEqual(stub["status"], "queued")
        svc.wakes.assert_called()
        # The routing guard that used to park it now resolves a tier.
        self.assertEqual(resolve_coding_tool(stub).tool, "tier_c")

        launched: list[str] = []
        repo = _DispatchRepo([dict(row) for row in svc.tasks_by_id.values()
                              if row.get("status") == "queued"])
        service_disp = _DispatchService(repo)
        dispatcher = _dispatcher(repo, service_disp, launched)
        out = await dispatcher.dispatch_cycle()
        await _drain()
        self.assertEqual(out["launched"], [task_id])
        self.assertEqual(launched, [task_id])

        # ... and the completed child lets the objective close.
        stub["status"] = "completed"
        await svc.patch_task(task_id, {"status": "completed"})

        async def _clean_review(role: str, prompt: str, workdir: Path,
                                timeout_s: int) -> ExecResult:
            del prompt, workdir, timeout_s
            assert role.startswith("review:")
            return ExecResult(
                0, 2.0, output_tail="No findings.\nREVIEW_DONE_ticket_1\n")

        objectives = InMemoryObjectiveStore()
        await objectives.create_objective(
            dict(svc.objective_docs["obj_1"]))

        async def _noop_notify(ref: str, text: str) -> None:
            del ref, text

        env = ObjectiveEnv(
            objectives=objectives,
            tasks=InMemoryTaskLister(svc.tasks_by_id),
            create_ticket=AsyncMock(return_value={"task_id": "t"}),
            patch_task=AsyncMock(),
            executor=SimpleNamespace(run=_clean_review,
                                     cleanup=AsyncMock()),
            quota=MagicMock(),
            slots=InMemorySlotStore(),
            reviews=InMemoryReviewStore(),
            inbox=None,
            notify=_noop_notify,
            repo=svc.repo_path,
            pool=parse_pool([
                {"label": "r-a", "family": "fa", "priority": 10},
                {"label": "r-b", "family": "fb", "priority": 20},
            ]),
        )
        closed = await engine_mod.run_final_review(env, "obj_1")
        self.assertTrue(closed["ok"])
        self.assertEqual(closed["action"], "closed")

    async def test_tierless_stub_parks_without_a_contract(self) -> None:
        repo = _DispatchRepo([{
            "task_id": "task-stub", "title": "Promoted stub work",
            "status": "queued", "project": "acme",
            "agent_hint": "dev", "autorun": True,
            "params": {"promoted_by": "horizon"},
            "objective_id": "obj_1",
        }])
        service = _DispatchService(repo)
        launched: list[str] = []
        dispatcher = _dispatcher(repo, service, launched)

        out = await dispatcher.dispatch_cycle()
        await _drain()

        self.assertEqual(out["launched"], [])
        self.assertEqual(launched, [])
        row = await repo.get_task("task-stub")
        assert row is not None
        self.assertTrue(
            str(row["waiting_reason"]).startswith("routing_missing"))

    async def test_retry_redrives_while_tierless(self) -> None:
        service, svc, _repo, aid = await self._linked_world([
            {
                "objective_status": "active",
                "state_summary": "unrelated work first",
                "tickets": [{
                    "local_ref": "other",
                    "title": "Other planned work",
                    "target_agent": "dev",
                    "work_type": "implementation",
                    "goal": "g",
                    "why_now": "now",
                    "acceptance_criteria": ["done"],
                    "expected_artifacts": ["code"],
                    "coding_tool": "tier_c",
                    "tier_reason": "default tier for specified work",
                }],
            },
        ])
        first = await service.promote_action("usr_owner", action_id=aid)
        self.assertTrue(first["promoted"])
        self.assertEqual(first["contracts_completed"], [])
        task_id = first["task_id"]
        self.assertFalse(self._q(svc, task_id).get("coding_tool"))

        stubs = [row["task_id"] for row in svc.tasks_by_id.values()
                 if row.get("objective_id") == "obj_1"
                 and not row.get("coding_tool")
                 and str(row.get("status") or "") not in (
                     "completed", "cancelled", "converted")]
        self.assertEqual(stubs, [task_id])
        svc.plan_payloads.append(_completion_payload(task_id))
        again = await service.promote_action("usr_owner", action_id=aid)

        self.assertTrue(again["already_promoted"])
        self.assertTrue(again["planning_redriven"])
        self.assertEqual(again["contracts_completed"], [task_id])
        self.assertEqual(len(svc.created_tasks), 1)
        self.assertEqual(svc.plan_calls, ["obj_1", "obj_1"])
        self.assertEqual(self._q(svc, task_id)["coding_tool"], "tier_c")

    async def test_loose_cases_trigger_no_planning(self) -> None:
        cases = {
            "none": [],
            "two": [
                {"objective_id": "o1", "status": "active",
                 "erp_group_ref": "TASK-1"},
                {"objective_id": "o2", "status": "active",
                 "erp_group_ref": "TASK-2"},
            ],
            "group_less": [
                {"objective_id": "o1", "status": "active",
                 "erp_group_ref": ""},
            ],
        }
        for name, rows in cases.items():
            with self.subTest(case=name):
                repo = FakeHorizonRepo()
                svc = _PlanningTaskService(self)
                service = HorizonService(
                    _horizon_settings(), repo, task_service=svc)
                service.set_company_config(FakeCompanyConfig())

                async def _lister(project: str,
                                  _rows: Any = rows) -> list[dict[str, Any]]:
                    del project
                    return list(_rows)

                repo.list_active_objectives = _lister  # type: ignore[attr-defined]
                stored = await repo.create_horizon_action(
                    _action(company_project="acme"))
                result = await service.promote_action(
                    "usr_owner", action_id=stored["action_id"])

                self.assertTrue(result["promoted"])
                self.assertIsNone(result["objective_id"])
                self.assertFalse(result["planning_triggered"])
                self.assertEqual(svc.plan_calls, [])

    async def test_linked_promotion_leaks_no_personal_data(self) -> None:
        hostile = _action(
            company_project="acme",
            title="Fix the report total",
            acceptance="Totals reconcile",
            mission=CANARY_MISSION,
            objective_title=CANARY_OBJECTIVE,
            finance_notes=CANARY_FINANCE,
            family=CANARY_FAMILY,
        )
        service, svc, _repo, aid = await self._linked_world(
            [{
                "objective_status": "active",
                "state_summary": "unrelated work",
                "tickets": [{
                    "local_ref": "other",
                    "title": "Other planned work",
                    "target_agent": "dev",
                    "work_type": "implementation",
                    "goal": "g",
                    "why_now": "now",
                    "acceptance_criteria": ["done"],
                    "expected_artifacts": ["code"],
                    "coding_tool": "tier_c",
                    "tier_reason": "default tier for specified work",
                }],
            }],
            action=hostile)
        result = await service.promote_action("usr_owner", action_id=aid)

        self.assertTrue(result["promoted"])
        req = svc.created_tasks[0]
        blob = req.title + "\n" + req.description + "\n" + json.dumps(
            req.params or {}, sort_keys=True)
        for canary in (CANARY_MISSION, CANARY_OBJECTIVE, CANARY_FINANCE,
                       CANARY_FAMILY):
            self.assertNotIn(canary, blob)


if __name__ == "__main__":
    unittest.main()
