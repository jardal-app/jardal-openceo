"""Autonomy gates: only human-actionable waits page the owner (TASK-2026-00118, F4).

Decision table (policy: requires_owner, TASK-2026-00114):

- waiting -> Jardal ERP resolves per ticket: Pending Review only when the wait
  names a human action (pending approval, owner escalation, phase waits);
  operational waits (quota, transient, config, Tier S, blocked_by) keep the
  business state (Open/Working). Unknown waits default to review (safe).
- the default dev template skips the human qa_review phase unless the
  project policy demands it (dev.qa.require_human_review); explicit phases
  are untouched.
- the objective engine never parks as needs_owner: max_tickets defers with
  open work and attempts completion without it; the rest park with
  operational reasons (retried on the next trigger, consumed by the CEO).
- workflows no longer create tickets (TASK-2026-00199 removed pm.* from
  the registry); objective children run by default (autorun=True).

No network, no CLI, no LLM.
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
from unittest.mock import MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    ObjectiveEnv,
    plan_objective,
)
from settings import Settings  # noqa: E402
from state import AgentName  # noqa: E402
from task_service import TaskService  # noqa: E402
from tools.jardal_erp_tickets import (  # noqa: E402
    JardalERPTicketAdapter,
    map_status,
    resolve_jardal_erp_status,
    waiting_needs_human_review,
)
from workflow_service import (  # noqa: E402
    MissingWorkflowToolError,
    WorkflowToolRegistry,
)


def _waiting_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task-1",
        "status": "waiting",
        "phase": None,
        "waiting_reason": "quota: reset at 12:00",
        "approval_required": False,
        "approval_state": "not_required",
        "qa_review_required": False,
        "qa_review_state": "not_required",
    }
    task.update(overrides)
    return task


class WaitingResolveTests(unittest.TestCase):
    def test_operational_waits_do_not_push(self) -> None:
        reasons = [
            "quota: claude reset at 12:00",
            "transient: repo_busy",
            "transient: cannot open work branch",
            "config: unknown base branch main",
            "config: gates: missing cmd",
            "needs_owner: baseline_red: gates",
            "needs_owner: merge_conflict: branch",
            "needs_owner: builder produced no changes",
            "blocked_by: TASK-2026-00001",
            "rate_limit_queued",
            "workflow_blocked_tool",
            "routing_missing",
            "jardal_erp_pending",
            "waiting",
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                task = _waiting_task(waiting_reason=reason)
                self.assertFalse(waiting_needs_human_review(task))
                self.assertIsNone(resolve_jardal_erp_status(task))

    def test_human_waits_push_pending_review(self) -> None:
        cases = [
            # YAML human_in_the_loop gate.
            {"approval_required": True, "approval_state": "pending",
             "waiting_reason": "phase:qa_review"},
            # Explicit human phase.
            {"approval_required": True, "approval_state": "pending",
             "phase": "client_signoff",
             "waiting_reason": "phase:client_signoff"},
            # Owner escalation (TASK-2026-00114).
            {"phase": "owner_escalation",
             "waiting_reason": "owner_escalation:gasto_nuevo"},
            {"phase": None,
             "waiting_reason": "owner_escalation:accion_irreversible"},
            {"phase": "owner_escalation", "waiting_reason": None},
            # Phase wait even if the approval flags were cleared.
            {"approval_required": False, "approval_state": "not_required",
             "waiting_reason": "phase:pm_approval"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                task = _waiting_task(**overrides)
                self.assertTrue(waiting_needs_human_review(task))
                self.assertEqual(
                    resolve_jardal_erp_status(task), "Pending Review")

    def test_unknown_wait_defaults_to_review(self) -> None:
        # No context at all (bare status dict, as queued before the ticket
        # is re-read): the safe default is the review queue.
        self.assertEqual(
            resolve_jardal_erp_status({"status": "waiting"}), "Pending Review")
        self.assertTrue(waiting_needs_human_review(None))
        # A reason-less wait cannot prove it is operational.
        task = _waiting_task(waiting_reason=None)
        self.assertTrue(waiting_needs_human_review(task))
        self.assertEqual(
            resolve_jardal_erp_status(task), "Pending Review")

    def test_human_already_acted_does_not_push(self) -> None:
        # Approval granted: the machine must resume, not re-page the owner.
        for state in ("approved", "changes_requested", "rejected"):
            with self.subTest(state=state):
                task = _waiting_task(
                    approval_required=True, approval_state=state,
                    waiting_reason="quota: reset at 12:00")
                self.assertFalse(waiting_needs_human_review(task))
                self.assertIsNone(resolve_jardal_erp_status(task))

    def test_map_status_keeps_no_context_safe_default(self) -> None:
        self.assertEqual(map_status("waiting"), "Pending Review")
        self.assertEqual(map_status("completed"), "Pending Review")

    def test_completed_behavior_unchanged(self) -> None:
        self.assertEqual(
            resolve_jardal_erp_status({"status": "completed"}), "Completed")
        self.assertEqual(
            resolve_jardal_erp_status({
                "status": "completed", "qa_review_required": True,
                "qa_review_state": "pending"}),
            "Pending Review",
        )


class FakeClient:
    def __init__(self) -> None:
        self.is_configured = True
        self.updated: list[tuple[str, str, dict]] = []

    def config_error(self) -> str | None:
        return None

    async def update_doc(self, doctype: str, name: str, body: dict) -> dict:
        self.updated.append((doctype, name, body))
        return {"name": name, **body}


def _adapter(client: FakeClient) -> JardalERPTicketAdapter:
    settings = SimpleNamespace(ERPNEXT_TICKETS_PROJECT_COMPANY="Jardal")
    return JardalERPTicketAdapter(settings, client=client)  # type: ignore[arg-type]


class UpdateStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_with_task_resolves(self) -> None:
        client = FakeClient()
        adapter = _adapter(client)
        operational = _waiting_task(
            waiting_reason="needs_owner: baseline_red: gates")
        self.assertIsNone(
            await adapter.update_status("TASK-1", "waiting",
                                        task=operational))
        self.assertEqual(client.updated, [])

        human = _waiting_task(
            approval_required=True, approval_state="pending",
            waiting_reason="phase:qa_review")
        await adapter.update_status("TASK-1", "waiting", task=human)
        self.assertEqual(
            client.updated[-1], ("Task", "TASK-1", {"status": "Pending Review"}))

    async def test_waiting_without_context_stays_safe(self) -> None:
        client = FakeClient()
        adapter = _adapter(client)
        await adapter.update_status("TASK-1", "waiting")
        self.assertEqual(
            client.updated[-1], ("Task", "TASK-1", {"status": "Pending Review"}))


class _FakeProjects:
    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        del query
        return None


class _FakeRepo:
    def __init__(self) -> None:
        self.projects = _FakeProjects()


class _FakeErp:
    is_enabled = True


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    def _service(self) -> TaskService:
        service = TaskService(
            settings=Settings(),
            repo=_FakeRepo(),  # type: ignore[arg-type]
            graph=MagicMock(),
            realtime_hub=MagicMock(),
        )
        service._business = _FakeErp()  # noqa: SLF001
        return service

    def _mirrored(self, **overrides: Any) -> dict[str, Any]:
        task = _waiting_task(**overrides)
        task["params"] = {"_mirrors": {"erpnext": {
            "name": "TASK-2026-00001", "status_pushed": "in_progress"}}}
        return task

    async def test_operational_wait_is_not_enqueued(self) -> None:
        service = self._service()
        await service.observe_task_status(
            self._mirrored(waiting_reason="quota: reset"))
        self.assertTrue(service._jardal_erp_queue.empty())  # noqa: SLF001

    async def test_escalation_wait_is_enqueued(self) -> None:
        service = self._service()
        await service.observe_task_status(self._mirrored(
            phase="owner_escalation",
            waiting_reason="owner_escalation:gasto_nuevo"))
        item = service._jardal_erp_queue.get_nowait()  # noqa: SLF001
        self.assertEqual(item["status"], "waiting")
        self.assertEqual(item["name"], "TASK-2026-00001")

    async def test_approval_wait_is_enqueued(self) -> None:
        service = self._service()
        await service.observe_task_status(self._mirrored(
            approval_required=True, approval_state="pending",
            waiting_reason="phase:qa_review"))
        item = service._jardal_erp_queue.get_nowait()  # noqa: SLF001
        self.assertEqual(item["status"], "waiting")


def _doc(**overrides: Any) -> SimpleNamespace:
    doc = SimpleNamespace(
        workflow_phases=[],
        phase=None,
        phase_index=0,
        phase_assignments={},
        phase_started_at=None,
        workflow_key=None,
        agent_hint=AgentName.DEV,
        agent=AgentName.CEO,
        project="proj",
        params={},
    )
    for key, value in overrides.items():
        setattr(doc, key, value)
    return doc


def _phase_keys(doc: SimpleNamespace) -> list[str]:
    return [str(phase.get("key")) for phase in doc.workflow_phases]


class DefaultTemplateTests(unittest.IsolatedAsyncioTestCase):
    def _service(self, loader: Any | None) -> TaskService:
        return TaskService(
            settings=Settings(),
            repo=_FakeRepo(),  # type: ignore[arg-type]
            graph=MagicMock(),
            realtime_hub=MagicMock(),
            config_loader=loader,
        )

    async def test_default_skips_human_qa_without_policy(self) -> None:
        service = self._service(SimpleNamespace(load_project=lambda p: {}))
        doc = _doc()
        await service._apply_default_phase_workflow_to_document(doc)  # noqa: SLF001
        self.assertEqual(doc.workflow_key, "dev.default")
        self.assertEqual(_phase_keys(doc), ["dev_work", "completed"])
        self.assertEqual(doc.phase, "dev_work")

    async def test_policy_keeps_human_qa(self) -> None:
        config = {"dev": {"qa": {"require_human_review": True}}}
        service = self._service(
            SimpleNamespace(load_project=lambda p: config))
        doc = _doc()
        await service._apply_default_phase_workflow_to_document(doc)  # noqa: SLF001
        self.assertEqual(
            _phase_keys(doc), ["dev_work", "qa_review", "completed"])
        self.assertEqual(doc.phase, "dev_work")

    async def test_unreadable_policy_keeps_human_qa(self) -> None:
        # No loader (or a failing one): the policy is unknown, keep the gate.
        for loader in (
            None,
            SimpleNamespace(load_project=MagicMock(
                side_effect=RuntimeError("unreadable"))),
        ):
            with self.subTest(loader=loader):
                service = self._service(loader)
                doc = _doc()
                await service._apply_default_phase_workflow_to_document(doc)  # noqa: SLF001
                self.assertIn("qa_review", _phase_keys(doc))

    async def test_explicit_phases_are_untouched(self) -> None:
        service = self._service(SimpleNamespace(load_project=lambda p: {}))
        doc = _doc(workflow_phases=[
            {"key": "dev_work", "type": "agent", "agent": "dev"},
            {"key": "qa_review", "type": "human", "permission": "qa_review"},
            {"key": "completed", "type": "terminal",
             "status": "completed"},
        ])
        await service._apply_default_phase_workflow_to_document(doc)  # noqa: SLF001
        self.assertIn("qa_review", _phase_keys(doc))

    async def test_requested_template_keeps_qa(self) -> None:
        service = self._service(SimpleNamespace(load_project=lambda p: {}))
        doc = _doc(workflow_key="dev.client_visible")
        await service._apply_default_phase_workflow_to_document(doc)  # noqa: SLF001
        self.assertEqual(
            _phase_keys(doc),
            ["dev_work", "qa_review", "client_signoff", "completed"])

    async def test_non_dev_gets_no_phases(self) -> None:
        service = self._service(SimpleNamespace(load_project=lambda p: {}))
        doc = _doc(agent_hint=None, agent=AgentName.CEO)
        await service._apply_default_phase_workflow_to_document(doc)  # noqa: SLF001
        self.assertIsNone(doc.workflow_key)
        self.assertEqual(doc.workflow_phases, [])


# -- objective engine ----------------------------------------------------


def _scratch_repo(test: unittest.TestCase) -> Path:
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    repo = Path(temp.name)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo,
                   check=True)
    subprocess.run(["git", "config", "user.email", "t@e.invalid"], cwd=repo,
                   check=True)
    (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    return repo


class _ExplodingExecutor:
    """Fails the test if any role runs (proves the deterministic path)."""

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del prompt, workdir, timeout_s
        raise AssertionError(f"no executor runs expected, got {role!r}")

    async def cleanup(self, role: str) -> None:
        del role


def _output_target(repo: Path, prompt: str) -> Path:
    """OUTPUT path the prompt orders (the honest planner follows it)."""
    for line in prompt.splitlines():
        if line.startswith("OUTPUT: write "):
            rel = line[len("OUTPUT: write "):].split(" with exactly")[0]
            return repo / rel.strip()
    raise AssertionError("planner prompt names no OUTPUT target")


class _RecordedPlanner:
    """Simulated Muse headless run: writes the recorded JSON (0 tokens)."""

    def __init__(self, repo: Path, payloads: list[dict[str, Any]]) -> None:
        self._repo = repo
        self._payloads = payloads
        self.prompts: list[str] = []
        self.runs = 0
        self._target = None

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "objective_planner"
        self.prompts.append(prompt)
        payload = self._payloads[min(self.runs, len(self._payloads) - 1)]
        self.runs += 1
        try:
            target = _output_target(self._repo, prompt)
            self._target = target
        except AssertionError:
            # Retry prompt: fix the same file in place.
            assert self._target is not None
            target = self._target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="PLANNER_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _env_for(
    test: unittest.TestCase,
    repo: Path,
    payloads: list[dict[str, Any]],
    tasks: dict[str, dict[str, Any]] | None = None,
    objectives_cfg: dict[str, Any] | None = None,
    executor: Any | None = None,
) -> tuple[ObjectiveEnv, Any, list[dict[str, Any]]]:
    policy = repo / "policy.md"
    policy.write_text("POLICY: route by oracle, not quota.\n", encoding="utf-8")
    created: list[dict[str, Any]] = []
    counter = {"n": 0}

    async def create_ticket(payload: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        created.append(dict(payload))
        return {"task_id": f"task-new-{counter['n']}"}

    async def patch_task(task_id: str, updates: dict[str, Any]) -> None:
        del task_id, updates

    async def notify(ref: str, text: str) -> None:
        del ref, text

    planner = executor if executor is not None else _RecordedPlanner(
        repo, payloads)
    env = ObjectiveEnv(
        objectives=InMemoryObjectiveStore(),
        tasks=InMemoryTaskLister(dict(tasks or {})),
        create_ticket=create_ticket,
        patch_task=patch_task,
        executor=planner,  # type: ignore[arg-type]
        quota=MagicMock(),
        slots=MagicMock(),
        reviews=None,
        inbox=None,
        notify=notify,
        repo=repo,
        objectives_cfg=dict(objectives_cfg or {}),
        planner_cfg={
            "timeout_minutes": 15,
            "policy_path": str(policy),
            "agents": {"dev": {"enabled": True}},
        },
    )
    return env, planner, created


def _child(task_id: str, status: str) -> dict[str, Any]:
    return {
        "task_id": task_id, "title": "w", "status": status,
        "objective_id": "OBJ-1", "project": "proj",
        "acceptance": ["done"],
        "params": {"target_agent": "research",
                   "expected_artifacts": ["report"]},
    }


def _dev_ticket_planned(local_ref: str = "t1") -> dict[str, Any]:
    return {
        "local_ref": local_ref,
        "title": "Do the thing",
        "target_agent": "dev",
        "work_type": "implementation",
        "goal": "achieve it",
        "why_now": "next step",
        "acceptance_criteria": ["done"],
        "expected_artifacts": ["code"],
        "coding_tool": "tier_b",
        "tier_reason": "bounded work",
        "why_not_c": "a misjudged step ships wrong behavior",
    }


class EngineTopesTests(unittest.IsolatedAsyncioTestCase):
    async def _objective(self, env: ObjectiveEnv) -> None:
        await env.objectives.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": ["ship"]},
        })

    async def test_max_tickets_with_open_work_defers(self) -> None:
        repo = _scratch_repo(self)
        tasks = {"kid-1": _child("kid-1", "completed"),
                 "kid-2": _child("kid-2", "cancelled"),
                 "kid-3": _child("kid-3", "in_progress")}
        env, _planner, created = _env_for(
            self, repo, [{}], tasks=tasks,
            objectives_cfg={"max_tickets": 3},
            executor=_ExplodingExecutor())
        await self._objective(env)

        result = await plan_objective(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "max_tickets_reached")
        self.assertIn("still open", result["detail"])
        self.assertEqual(created, [])
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "active")

    async def test_max_tickets_without_open_work_not_deferred(self) -> None:
        # TASK-2026-00175 (deliberate contract change): max_tickets with
        # no open work still tries to close (never defers) — but the
        # research kids carry no domain verifier, so final review parks
        # as capability_gap instead of closing on metadata alone.
        repo = _scratch_repo(self)
        tasks = {"kid-1": _child("kid-1", "completed"),
                 "kid-2": _child("kid-2", "cancelled")}
        env, _planner, _created = _env_for(
            self, repo, [{}], tasks=tasks,
            objectives_cfg={"max_tickets": 2},
            executor=_ExplodingExecutor())
        await self._objective(env)

        result = await plan_objective(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "capability_gap")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertEqual(stored["status"], "waiting")
        self.assertEqual(stored.get("final_reviews", 0), 0)

    async def test_empty_planner_registry_parks_as_config(self) -> None:
        repo = _scratch_repo(self)
        env, _planner, _created = _env_for(
            self, repo, [{}], executor=_ExplodingExecutor())
        env.planner_cfg["agents"] = {}
        await self._objective(env)

        result = await plan_objective(env, "OBJ-1")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "config")
        stored = await env.objectives.get_objective("OBJ-1")
        assert stored is not None
        self.assertIn("config", stored["waiting_reason"])

    async def test_planned_children_run_by_default(self) -> None:
        repo = _scratch_repo(self)
        payload = {"objective_status": "active", "state_summary": "plan",
                   "tickets": [_dev_ticket_planned()]}
        env, planner, created = _env_for(self, repo, [payload])
        await self._objective(env)

        result = await plan_objective(env, "OBJ-1")

        self.assertTrue(result["ok"])
        self.assertEqual(planner.runs, 1)
        self.assertEqual(len(created), 1)
        self.assertIs(created[0]["autorun"], True)


class WorkflowCreationTests(unittest.IsolatedAsyncioTestCase):
    """Registry ticket creation was removed (TASK-2026-00199, scope 2).

    ``pm.create_ticket``, ``pm.create_tickets_batch`` and
    ``pm.extract_tickets`` duplicated the MCP ticket tools; workflows no
    longer create tickets. The run-by-default autonomy behavior these tests
    used to pin now lives in the objective engine
    (``test_planned_children_run_by_default`` above) and the spend gate
    (``tests/test_plan_spend_207.py``).
    """

    def _registry(self) -> WorkflowToolRegistry:
        return WorkflowToolRegistry()

    async def test_pm_create_ticket_is_gone(self) -> None:
        with self.assertRaises(MissingWorkflowToolError):
            await self._registry().call(
                "pm.create_ticket", {"title": "t"}, {})

    async def test_pm_create_tickets_batch_is_gone(self) -> None:
        with self.assertRaises(MissingWorkflowToolError):
            await self._registry().call(
                "pm.create_tickets_batch", {"tickets": [{"title": "a"}]},
                {})

    async def test_pm_extract_tickets_is_gone(self) -> None:
        with self.assertRaises(MissingWorkflowToolError):
            await self._registry().call(
                "pm.extract_tickets", {"text": "t"}, {})


if __name__ == "__main__":
    unittest.main()
