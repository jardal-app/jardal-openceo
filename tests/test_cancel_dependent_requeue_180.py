"""Dependents requeue when their blocker is cancelled (TASK-2026-00180).

The cancel doors (desk webhook, explicit transition, objective cascade)
land `cancelled` without going through `run_task`, so `_on_ticket_done`
never runs for them. Before this fix the event-held `waiting(blocked_by)`
parks stranded: the dispatcher cycle only requeues due (resume_at) waits
and the backup never retains waits without resume — while `blockers_open`
already counts cancelled as closed. Fakes only, 0 tokens.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from state import TaskSource  # noqa: E402
from task_service import TaskService  # noqa: E402
from tools.erpnext_client import ERPNextValidationError  # noqa: E402


class FakeRepo:
    """Dict-backed repo with the task surface the funnel uses."""

    def __init__(self, tasks: list[dict[str, Any]] | None = None) -> None:
        self.tasks = {t["task_id"]: dict(t) for t in (tasks or [])}
        self.patches: list[tuple[str, dict[str, Any]]] = []

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        doc = self.tasks.get(str(task_id))
        return dict(doc) if doc is not None else None

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        doc = self.tasks.get(str(task_id))
        if doc is None:
            return None
        merged = {**doc, **dict(updates)}
        self.tasks[str(task_id)] = merged
        self.patches.append((str(task_id), dict(updates)))
        return dict(merged)

    async def list_tasks_blocked_by(self, task_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.tasks.values()
            if str(task_id) in [str(b) for b in (row.get("blocked_by") or [])]
            and str(row.get("status") or "") in {"waiting", "queued"}
        ]

    async def find_task_by_source(
        self, source: str, source_id: str
    ) -> dict[str, Any] | None:
        for doc in self.tasks.values():
            if str(doc.get("source") or "") == str(source) and str(
                doc.get("source_id") or ""
            ) == str(source_id):
                return dict(doc)
        return None


class FakeHub:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def broadcast(self, event: str, payload: Any) -> None:
        self.events.append((event, payload))


class FakeBusinessAdapter:
    """ERPNext en memoria: resuelve, transiciona y relee."""

    is_enabled = True

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = {doc["name"]: dict(doc) for doc in docs}

    async def resolve_task(self, identifier: str) -> dict[str, Any]:
        key = str(identifier or "").strip()
        if key in self.docs:
            return dict(self.docs[key])
        raise ERPNextValidationError(
            f"No existe el ticket {identifier}.", status_code=404
        )

    async def transition_status(
        self, identifier: str, to_status: str, *, work_dates: Any = None
    ) -> dict[str, Any]:
        doc = await self.resolve_task(identifier)
        name = str(doc.get("name"))
        self.docs[name] = {**doc, "status": to_status}
        return {"ok": True, "ticket": {"name": name, "status": to_status}}


def _mirror(name: str, pushed: str) -> dict[str, Any]:
    return {"_mirrors": {"erpnext": {"name": name, "status_pushed": pushed}}}


def _exec(
    task_id: str,
    status: str,
    *,
    blocked_by: list[str] | None = None,
    waiting_reason: str | None = None,
    autorun: bool = True,
    **over: Any,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "status": status,
        "project": "jardal-openceo",
        "source": TaskSource.API.value,
        "source_id": f"src-{task_id}",
        "blocked_by": list(blocked_by or []),
        "waiting_reason": waiting_reason,
        "resume_at": None,
        "autorun": autorun,
        "params": dict(_mirror(f"TASK-{task_id}", status)),
    }
    doc.update(over)
    return doc


def _service(repo: FakeRepo, adapter: Any = None) -> TaskService:
    service = TaskService.__new__(TaskService)
    service._repo = repo  # type: ignore[attr-defined]
    service._hub = FakeHub()  # type: ignore[attr-defined]
    service._horizon_terminal = None  # type: ignore[attr-defined]
    service._erpnext = adapter if adapter is not None else SimpleNamespace()
    service._qa_service = None  # type: ignore[attr-defined]
    service._rate_monitor = None  # type: ignore[attr-defined]
    return service


class CancelRequeueTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_cancel_requeues_direct_dependent(self) -> None:
        repo = FakeRepo([
            _exec("blocker", "in_progress"),
            _exec("dep", "waiting", blocked_by=["blocker"],
                  waiting_reason="blocked_by: blocker"),
        ])
        service = _service(repo)
        result = await service.apply_business_status(
            task_id="blocker", business_status="Cancelled")
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["task_status"], "cancelled")
        blocker = await repo.get_task("blocker")
        assert blocker is not None
        self.assertEqual(blocker["status"], "cancelled")
        dep = await repo.get_task("dep")
        assert dep is not None
        self.assertEqual(dep["status"], "queued")
        self.assertIsNone(dep["waiting_reason"])
        self.assertIsNone(dep["resume_at"])

    async def test_transition_cancel_requeues_direct_dependent(self) -> None:
        adapter = FakeBusinessAdapter([{
            "name": "TASK-2026-00901",
            "subject": "agent work",
            "status": "Working",
            "project": "PROJ-0001",
            "openceo_ref": "api:TASK-2026-00901",
            "openceo_task_id": "blocker",
            "is_group": False,
            "parent_task": None,
        }])
        repo = FakeRepo([
            _exec("blocker", "in_progress", objective_id="OBJ-1"),
            _exec("dep", "waiting", blocked_by=["blocker"],
                  waiting_reason="blocked_by: blocker"),
        ])
        service = _service(repo, adapter)
        result = await service.transition_ticket(
            "TASK-2026-00901", "Cancelled", "ya no hace falta")
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "applied")
        blocker = await repo.get_task("blocker")
        assert blocker is not None
        self.assertEqual(blocker["status"], "cancelled")
        dep = await repo.get_task("dep")
        assert dep is not None
        self.assertEqual(dep["status"], "queued")
        self.assertIsNone(dep["waiting_reason"])
        self.assertIsNone(dep["resume_at"])

    async def test_autorun_off_dependent_stays_waiting(self) -> None:
        repo = FakeRepo([
            _exec("blocker", "in_progress"),
            _exec("dep", "waiting", blocked_by=["blocker"],
                  waiting_reason="blocked_by: blocker", autorun=False),
        ])
        service = _service(repo)
        result = await service.apply_business_status(
            task_id="blocker", business_status="Cancelled")
        self.assertEqual(result["task_status"], "cancelled")
        dep = await repo.get_task("dep")
        assert dep is not None
        self.assertEqual(dep["status"], "waiting")
        self.assertEqual(dep["waiting_reason"], "blocked_by: blocker")

    async def test_two_level_chain_wakes_level_by_level(self) -> None:
        repo = FakeRepo([
            _exec("blocker", "in_progress"),
            _exec("mid", "waiting", blocked_by=["blocker"],
                  waiting_reason="blocked_by: blocker"),
            _exec("leaf", "waiting", blocked_by=["mid"],
                  waiting_reason="blocked_by: mid"),
        ])
        service = _service(repo)
        await service.apply_business_status(
            task_id="blocker", business_status="Cancelled")
        mid = await repo.get_task("mid")
        assert mid is not None
        self.assertEqual(mid["status"], "queued")
        leaf = await repo.get_task("leaf")
        assert leaf is not None
        # Only direct dependents: the leaf still waits on mid.
        self.assertEqual(leaf["status"], "waiting")
        # The existing finish path wakes the next level.
        await service._on_ticket_done(
            {"task_id": "mid", "status": "completed"})
        leaf = await repo.get_task("leaf")
        assert leaf is not None
        self.assertEqual(leaf["status"], "queued")
        self.assertIsNone(leaf["waiting_reason"])

    async def test_patch_without_cancel_transition_does_not_requeue(self) -> None:
        repo = FakeRepo([
            _exec("open-x", "waiting"),
            _exec("dep-open", "waiting", blocked_by=["open-x"],
                  waiting_reason="blocked_by: open-x"),
            _exec("dead-x", "cancelled",
                  error="El ticket fue cancelado en ERPNext."),
            _exec("dep-dead", "waiting", blocked_by=["dead-x"],
                  waiting_reason="blocked_by: dead-x"),
        ])
        service = _service(repo)
        await service.patch_task("open-x", {"status": "in_progress"})
        await service.patch_task("dead-x", {"error": "nota posterior"})
        for dep_id in ("dep-open", "dep-dead"):
            dep = await repo.get_task(dep_id)
            assert dep is not None
            self.assertEqual(dep["status"], "waiting", dep_id)
        requeued = [
            task_id for task_id, updates in repo.patches
            if updates.get("status") == "queued"
        ]
        self.assertEqual(requeued, [])

    async def test_requeue_failure_never_breaks_the_cancel(self) -> None:
        repo = FakeRepo([
            _exec("blocker", "in_progress"),
            _exec("dep", "waiting", blocked_by=["blocker"],
                  waiting_reason="blocked_by: blocker"),
        ])

        async def _boom(task_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("repo caido")

        repo.list_tasks_blocked_by = _boom  # type: ignore[method-assign]
        service = _service(repo)
        result = await service.apply_business_status(
            task_id="blocker", business_status="Cancelled")
        self.assertEqual(result["status"], "applied")
        blocker = await repo.get_task("blocker")
        assert blocker is not None
        self.assertEqual(blocker["status"], "cancelled")


class _FakeObjectiveRepo(FakeRepo):
    """FakeRepo plus the objective surface the cascade uses."""

    def __init__(self, tasks: list[dict[str, Any]] | None = None) -> None:
        super().__init__(tasks)
        self.objectives: dict[str, dict[str, Any]] = {}

    async def create_objective(
        self, doc: dict[str, Any]
    ) -> dict[str, Any]:
        stored = dict(doc)
        self.objectives[stored["objective_id"]] = stored
        return dict(stored)

    async def get_objective(
        self, objective_id: str
    ) -> dict[str, Any] | None:
        doc = self.objectives.get(objective_id)
        return dict(doc) if doc is not None else None

    async def patch_objective(
        self, objective_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        doc = self.objectives.get(objective_id)
        if doc is None:
            return None
        doc.update(updates)
        return dict(doc)

    async def list_tasks_by_objective(
        self, objective_id: str
    ) -> list[dict[str, Any]]:
        return [dict(row) for row in self.tasks.values()
                if row.get("objective_id") == objective_id]


class _FakeConfig:
    def __init__(self, projects: dict[str, dict[str, Any]]) -> None:
        self._projects = projects

    def has_project(self, project: str | None) -> bool:
        return bool(project) and project in self._projects

    def load_project(self, project: str) -> dict[str, Any]:
        return dict(self._projects.get(project) or {})


class CascadeRequeueTests(unittest.IsolatedAsyncioTestCase):
    def _objective_service(self, repo: _FakeObjectiveRepo,
                           task_service: Any) -> Any:
        from objectives.service import ObjectiveService

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        policy = Path(tmp.name) / "policy.md"
        policy.write_text("POLICY\n", encoding="utf-8")
        config = _FakeConfig({"proj": {
            "project": {"repos": []},
            "objectives": {"max_tickets": 20,
                           "max_active_objectives_global": 3},
            "objective_planner": {
                "timeout_minutes": 15, "policy_path": str(policy),
                "agents": {"dev": {"enabled": True}}},
        }})
        return ObjectiveService(
            repo=repo, task_service=task_service, config_loader=config,
            settings=SimpleNamespace(TIER_S_OBJECTIVE_CHILD_AUTORUN=True),
            rate_monitor=None)

    async def test_cascade_cancel_requeues_external_dependent(self) -> None:
        repo = _FakeObjectiveRepo([
            _exec("task-parent", "cancelled", objective_id="OBJ-1"),
            _exec("kid-1", "queued", objective_id="OBJ-1"),
            _exec("sib-1", "queued", objective_id="OBJ-1",
                  blocked_by=["kid-1"]),
            _exec("ext-1", "waiting", blocked_by=["kid-1"],
                  waiting_reason="blocked_by: kid-1"),
        ])
        await repo.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        service = self._objective_service(repo, _service(repo))
        result = await service.propagate_group_decision(
            {"task_id": "task-parent", "objective_id": "OBJ-1"},
            target="cancelled", business_status="Cancelled")
        self.assertEqual(result["action"], "cancelled")
        self.assertEqual(sorted(result["cancelled_children"]),
                         ["kid-1", "sib-1"])
        kid = await repo.get_task("kid-1")
        assert kid is not None
        self.assertEqual(kid["status"], "cancelled")
        # No resurrection: the internal sibling stays cancelled even
        # though it was blocked by a cancelled child.
        sib = await repo.get_task("sib-1")
        assert sib is not None
        self.assertEqual(sib["status"], "cancelled")
        # The external dependent continues.
        ext = await repo.get_task("ext-1")
        assert ext is not None
        self.assertEqual(ext["status"], "queued")
        self.assertIsNone(ext["waiting_reason"])
        self.assertIsNone(ext["resume_at"])

    async def test_cascade_without_task_service_stays_silent(self) -> None:
        repo = _FakeObjectiveRepo([
            _exec("task-parent", "cancelled", objective_id="OBJ-1"),
            _exec("kid-1", "queued", objective_id="OBJ-1"),
        ])
        await repo.create_objective({
            "objective_id": "OBJ-1", "project": "proj", "status": "active",
            "anchor": {"goal": "g", "acceptance": []},
            "conversion_source_task_id": "task-parent",
        })
        service = self._objective_service(repo, None)
        result = await service.propagate_group_decision(
            {"task_id": "task-parent", "objective_id": "OBJ-1"},
            target="cancelled", business_status="Cancelled")
        self.assertEqual(result["action"], "cancelled")
        self.assertEqual(result["cancelled_children"], ["kid-1"])


if __name__ == "__main__":
    unittest.main()
