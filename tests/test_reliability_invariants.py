"""Transition invariants v1: violation and non-violation per invariant.

Fakes of the ERP and Mongo only; no network, no CLI, no LLM (epic
TASK-2026-00365, ticket TASK-2026-00377). INV-001 fires on Completed
without gates, review or evidence; INV-002 on an ERP reread that misses
the target; INV-003 on two active runs or two lock holders; INV-004 on
a foreign checkpoint; INV-005 on a superseded run that executes again.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import invariants as inv  # noqa: E402
from reliability.config import (  # noqa: E402
    RELIABILITY_DEFAULTS,
    get_reliability_config,
    validate_reliability_config,
)
from task_service import TaskService  # noqa: E402


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class PureCheckTests(unittest.TestCase):
    def test_inv001_violation_without_gates_review_evidence(self) -> None:
        violation = inv.check_completion(
            gates_passed=False, review_satisfied=False, evidence=None)
        self.assertIsNotNone(violation)
        self.assertEqual((violation or {})["invariant_id"], "INV-001")

    def test_inv001_clean_completion_passes(self) -> None:
        self.assertIsNone(inv.check_completion(
            gates_passed=True, review_satisfied=True, evidence="verified"))
        self.assertIsNone(inv.check_completion(
            gates_passed=None, review_satisfied=True, evidence="verified"))

    def test_inv002_violation_on_reread_miss(self) -> None:
        violation = inv.check_reread(
            target="Completed", actual="Working", ticket="TASK-1")
        self.assertIsNotNone(violation)
        self.assertEqual((violation or {})["invariant_id"], "INV-002")

    def test_inv002_matching_reread_passes(self) -> None:
        self.assertIsNone(inv.check_reread(
            target="Completed", actual="Completed", ticket="TASK-1"))

    def test_inv003_violation_on_two_active_runs(self) -> None:
        violation = inv.check_active_runs(task_ref="TASK-1", active_runs=[
            {"run_id": "a", "status": "running"},
            {"run_id": "b", "status": "waiting"}])
        self.assertIsNotNone(violation)
        self.assertEqual((violation or {})["invariant_id"], "INV-003")

    def test_inv003_single_or_terminal_runs_pass(self) -> None:
        self.assertIsNone(inv.check_active_runs(
            task_ref="TASK-1",
            active_runs=[{"run_id": "a", "status": "running"}]))
        self.assertIsNone(inv.check_active_runs(
            task_ref="TASK-1",
            active_runs=[{"run_id": "a", "status": "superseded"},
                         {"run_id": "b", "status": "failed"}]))
        self.assertIsNone(inv.check_active_runs(
            task_ref="TASK-1", active_runs=None))

    def test_inv003_violation_on_two_lock_holders(self) -> None:
        violation = inv.check_lock_holders(
            repo="/repo", holders=["tier-s:aaa", "dev:bbb"])
        self.assertIsNotNone(violation)
        self.assertEqual((violation or {})["invariant_id"], "INV-003")

    def test_inv003_single_holder_passes(self) -> None:
        self.assertIsNone(inv.check_lock_holders(
            repo="/repo", holders=["tier-s:aaa", "tier-s:aaa"]))
        self.assertIsNone(inv.check_lock_holders(repo="/repo", holders=[]))

    def test_inv004_violation_on_foreign_checkpoint(self) -> None:
        violation = inv.check_checkpoint(
            checkpoint={"run_id": "r-1", "phase": "review",
                        "branch": "b", "head_sha": "h", "base_sha": "s"},
            run_id="r-2")
        self.assertIsNotNone(violation)
        self.assertEqual((violation or {})["invariant_id"], "INV-004")

    def test_inv004_violation_on_missing_checkpoint_refs(self) -> None:
        violation = inv.check_checkpoint(
            checkpoint={"phase": "build", "branch": "b"}, run_id="r-1")
        self.assertIsNotNone(violation)

    def test_inv004_matching_or_empty_checkpoint_passes(self) -> None:
        self.assertIsNone(inv.check_checkpoint(
            checkpoint={"run_id": "r-1", "phase": "review",
                        "branch": "b", "head_sha": "h", "base_sha": "s"},
            run_id="r-1"))
        self.assertIsNone(inv.check_checkpoint(checkpoint=None, run_id="r-1"))
        self.assertIsNone(inv.check_checkpoint(checkpoint={}, run_id="r-1"))

    def test_inv005_violation_on_superseded_run(self) -> None:
        violation = inv.check_run_executable(
            run={"run_id": "r-1", "status": "superseded"})
        self.assertIsNotNone(violation)
        self.assertEqual((violation or {})["invariant_id"], "INV-005")

    def test_inv005_live_or_missing_run_passes(self) -> None:
        self.assertIsNone(inv.check_run_executable(
            run={"run_id": "r-1", "status": "waiting"}))
        self.assertIsNone(inv.check_run_executable(run=None))

    def test_evaluate_transition_aggregates_in_inv_order(self) -> None:
        found = inv.evaluate_transition({
            "target": "Completed", "ticket": "TASK-1", "task": {},
            "evidence": None, "actual": "Working",
            "active_runs": [{"run_id": "a", "status": "running"},
                            {"run_id": "b", "status": "active"}],
            "lock_holders": ["x", "y"], "lock_repo": "/repo",
            "checkpoint": {"run_id": "r-9"}, "run_id": "r-1",
            "executing_run": {"run_id": "r-0", "status": "superseded"},
        })
        self.assertEqual([item["invariant_id"] for item in found],
                         ["INV-001", "INV-002", "INV-003", "INV-003",
                          "INV-004", "INV-005"])

    def test_evaluate_transition_passes_on_missing_observations(self) -> None:
        self.assertEqual(inv.evaluate_transition({}), [])
        self.assertEqual(inv.evaluate_transition(None), [])


class ModePolicyTests(unittest.TestCase):
    def test_defaults_start_in_record(self) -> None:
        for invariant_id in inv.INVARIANTS:
            self.assertEqual(inv.invariant_mode(invariant_id), "record")
            self.assertFalse(inv.should_reject(invariant_id, actor="engine"))

    def test_enforce_rejects_engine_transitions(self) -> None:
        config = {"mode": {"INV-003": "enforce"}}
        self.assertTrue(inv.should_reject("INV-003", actor="engine",
                                          config=config))
        self.assertTrue(inv.should_reject("INV-003", actor="owner",
                                          config=config))

    def test_inv001_never_rejects_owner_or_manual(self) -> None:
        config = {"mode": {"INV-001": "enforce", "INV-002": "enforce"}}
        self.assertFalse(inv.should_reject("INV-001", actor="owner",
                                           config=config))
        self.assertFalse(inv.should_reject("INV-001", actor="manual",
                                           config=config))
        self.assertTrue(inv.should_reject("INV-001", actor="engine",
                                          config=config))
        self.assertTrue(inv.should_reject("INV-002", actor="owner",
                                          config=config))

    def test_unreadable_config_fails_open_to_record(self) -> None:
        self.assertEqual(inv.invariant_mode("INV-001", config="garbage"),
                         "record")
        self.assertEqual(inv.invariant_mode("INV-999"), "record")

    def test_violation_doc_is_critical_engine_incident(self) -> None:
        doc = inv.violation_doc(
            {"invariant_id": "INV-002", "detail": "missed",
             "task_id": None, "business_ref": "TASK-1", "run_id": None},
            business_ref="TASK-1")
        self.assertEqual(doc["kind"], "invariant_violation")
        self.assertEqual(doc["severity"], "critical")
        self.assertEqual(doc["attribution"], "engine")


class InvariantConfigTests(unittest.TestCase):
    def test_defaults_include_record_modes_and_validate(self) -> None:
        self.assertEqual(
            RELIABILITY_DEFAULTS["invariants"]["mode"],
            {name: "record" for name in inv.INVARIANTS})
        self.assertEqual(
            validate_reliability_config(RELIABILITY_DEFAULTS), [])

    def test_unknown_invariant_and_bad_mode_fail_closed(self) -> None:
        bad_id = {"invariants": {
            "mode": {**RELIABILITY_DEFAULTS["invariants"]["mode"],
                     "INV-999": "record"}}}
        self.assertTrue(any("INV-999" in error for error in
                            validate_reliability_config(
                                {**RELIABILITY_DEFAULTS, **bad_id})))
        bad_mode = {"invariants": {"mode": {"INV-001": "block"}}}
        self.assertTrue(validate_reliability_config(
            {**RELIABILITY_DEFAULTS, **bad_mode}))

    def test_partial_mode_override_keeps_remaining_defaults(self) -> None:
        effective = get_reliability_config(
            {"reliability": {"invariants": {"mode": {"INV-003": "enforce"}}}})
        self.assertEqual(effective["invariants"]["mode"]["INV-003"], "enforce")
        self.assertEqual(effective["invariants"]["mode"]["INV-001"], "record")
        self.assertEqual(validate_reliability_config(effective), [])


class FakeIncidents:
    """Async Mongo-like collection capturing incident docs."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def count_documents(self, filt: dict[str, Any]) -> int:
        return 0


class FakeRepo:
    """Dict-backed Mongo fake: execution records, runs and incidents."""

    def __init__(self, tasks: list[dict[str, Any]] | None = None,
                 runs: list[dict[str, Any]] | None = None) -> None:
        self.tasks = {row["task_id"]: dict(row) for row in (tasks or [])}
        self.runs = [dict(row) for row in (runs or [])]
        self.incidents = FakeIncidents()
        self.db = {"reliability_incidents": self.incidents}

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        doc = self.tasks.get(str(task_id))
        return dict(doc) if doc is not None else None

    async def patch_task(self, task_id: str,
                         updates: dict[str, Any]) -> dict[str, Any] | None:
        doc = self.tasks.get(str(task_id))
        if doc is None:
            return None
        doc.update(dict(updates))
        return dict(doc)

    async def find_task_by_source(
        self, source: str, source_id: str
    ) -> dict[str, Any] | None:
        for doc in self.tasks.values():
            if str(doc.get("source") or "") == str(source) and str(
                    doc.get("source_id") or "") == str(source_id):
                return dict(doc)
        return None

    async def interrupted_tier_s_runs(
        self, limit: int = 200
    ) -> list[dict[str, Any]]:
        # Same visible set as MongoRepository (db/mongo.py): waiting runs
        # never come through this listing.
        return [dict(row) for row in self.runs[:limit]
                if str(row.get("status") or "") in
                {"active", "running", "resuming"}]

    def violations(self, invariant_id: str) -> list[dict[str, Any]]:
        return [doc for doc in self.incidents.docs.values()
                if doc.get("engine", {}).get("invariant_id") == invariant_id]


class FakeHub:
    async def broadcast(self, event: str, payload: Any) -> None:
        return None


class FakeBusinessAdapter:
    """In-memory Jardal ERP: resolves, transitions and rereads."""

    is_enabled = True
    provider = "erpnext"

    def __init__(self, doc: dict[str, Any], *, reread_stale: bool = False,
                 ) -> None:
        self.doc = dict(doc)
        self.reread_stale = reread_stale
        self.puts = 0

    async def resolve_task(self, identifier: str) -> dict[str, Any]:
        doc = dict(self.doc)
        if self.reread_stale:
            doc["status"] = "Working"
        return doc

    async def transition_status(
        self, identifier: str, to_status: str, *, work_dates: Any = None
    ) -> dict[str, Any]:
        self.puts += 1
        self.doc = {**self.doc, "status": to_status}
        return {"ok": True, "ticket": dict(self.doc)}


def _exec(task_id: str, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": task_id,
        "status": "in_progress",
        "project": "jardal-openceo",
        "params": {"_mirrors": {"erpnext": {
            "name": "TASK-2026-00377", "status_pushed": "in_progress"}}},
    }
    doc.update(over)
    return doc


def _business() -> dict[str, Any]:
    return {"name": "TASK-2026-00377", "status": "Working",
            "subject": "x", "openceo_task_id": "t-1",
            "openceo_ref": "api:t-1"}


def _service(repo: FakeRepo, adapter: FakeBusinessAdapter) -> TaskService:
    service = TaskService.__new__(TaskService)
    service._repo = repo  # type: ignore[attr-defined]
    service._hub = FakeHub()  # type: ignore[attr-defined]
    service._horizon_terminal = None  # type: ignore[attr-defined]
    service._business = adapter  # type: ignore[attr-defined]
    service._qa_service = None  # type: ignore[attr-defined]
    service._rate_monitor = None  # type: ignore[attr-defined]
    tmp = tempfile.TemporaryDirectory()
    service._reliability_runtime_dir = tmp.name  # type: ignore[attr-defined]
    service._tmp_runtime = tmp  # type: ignore[attr-defined]
    return service


class TransitionWiringTests(unittest.TestCase):
    def test_completed_applies_without_incident(self) -> None:
        repo = FakeRepo([_exec("t-1")])
        service = _service(repo, FakeBusinessAdapter(_business()))
        result = _run(service.transition_ticket(
            "TASK-2026-00377", "Completed", "done",
            evidence="gates and review verified"))
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("outcome"), "applied")
        self.assertEqual(repo.incidents.docs, {})

    def test_completed_without_evidence_is_rejected_by_validation(self) -> None:
        repo = FakeRepo([_exec("t-1")])
        service = _service(repo, FakeBusinessAdapter(_business()))
        result = _run(service.transition_ticket(
            "TASK-2026-00377", "Completed", "done"))
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "invalid_input")
        self.assertEqual(repo.incidents.docs, {})

    def test_reread_miss_records_inv002_without_touching_execution(self) -> None:
        repo = FakeRepo([_exec("t-1")])
        before = dict(repo.tasks["t-1"])
        service = _service(
            repo, FakeBusinessAdapter(_business(), reread_stale=True))
        result = _run(service.transition_ticket(
            "TASK-2026-00377", "Completed", "done",
            evidence="gates and review verified"))
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "mismatch")
        self.assertEqual(repo.tasks["t-1"].get("status"), before["status"])
        found = repo.violations("INV-002")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "critical")

    def test_post_and_reread_miss_record_single_inv002(self) -> None:
        from business.work import JardalERPWork

        repo = FakeRepo([_exec("t-1")])
        probe = _service(repo, FakeBusinessAdapter(_business()))
        settings = SimpleNamespace(JARDAL_ERP_SITE_TIMEZONE="UTC",
                                   JARDAL_ERP_URL="http://127.0.0.1:1")
        probe._business = JardalERPWork(
            settings, client=FakeERPClient(_row(), "Working"),
            on_violation=probe._business_violation_sink)
        result = _run(probe.transition_ticket(
            "TASK-2026-99999", "Completed", "done",
            evidence="gates and review verified"))
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "mismatch")
        self.assertEqual(len(repo.violations("INV-002")), 1)

    def test_two_live_runs_record_inv003_in_record_mode(self) -> None:
        repo = FakeRepo(
            [_exec("t-1")],
            runs=[{"run_id": "r-1", "task_id": "t-1", "status": "running"},
                  {"run_id": "r-2", "task_id": "t-1", "status": "active"}])
        service = _service(repo, FakeBusinessAdapter(_business()))
        result = _run(service.transition_ticket(
            "TASK-2026-00377", "Completed", "done",
            evidence="gates and review verified"))
        self.assertTrue(result.get("ok"), result)
        found = repo.violations("INV-003")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["attribution"], "engine")

    def test_two_live_runs_reject_in_enforce_mode(self) -> None:
        modes = RELIABILITY_DEFAULTS["invariants"]["mode"]
        previous = modes["INV-003"]
        modes["INV-003"] = "enforce"
        self.addCleanup(modes.__setitem__, "INV-003", previous)
        adapter = FakeBusinessAdapter(_business())
        repo = FakeRepo(
            [_exec("t-1")],
            runs=[{"run_id": "r-1", "task_id": "t-1", "status": "running"},
                  {"run_id": "r-2", "task_id": "t-1", "status": "active"}])
        service = _service(repo, adapter)
        result = _run(service.transition_ticket(
            "TASK-2026-00377", "Completed", "done",
            evidence="gates and review verified"))
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "invariant")
        self.assertEqual(result.get("invariant"), "INV-003")
        self.assertEqual(adapter.puts, 0)
        self.assertEqual(len(repo.violations("INV-003")), 1)

    def test_owner_transition_applies_without_violations_in_enforce(self) -> None:
        modes = RELIABILITY_DEFAULTS["invariants"]["mode"]
        previous = modes["INV-003"]
        modes["INV-003"] = "enforce"
        self.addCleanup(modes.__setitem__, "INV-003", previous)
        repo = FakeRepo([_exec("t-1")])
        service = _service(repo, FakeBusinessAdapter(_business()))
        result = _run(service.transition_ticket(
            "TASK-2026-00377", "Working", "owner order",
            owner_instruction="Owner: reabrelo", actor="owner"))
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(repo.incidents.docs, {})

    def test_owner_transition_rejected_on_inv003_in_enforce(self) -> None:
        modes = RELIABILITY_DEFAULTS["invariants"]["mode"]
        previous = modes["INV-003"]
        modes["INV-003"] = "enforce"
        self.addCleanup(modes.__setitem__, "INV-003", previous)
        adapter = FakeBusinessAdapter(_business())
        repo = FakeRepo(
            [_exec("t-1")],
            runs=[{"run_id": "r-1", "task_id": "t-1", "status": "running"},
                  {"run_id": "r-2", "task_id": "t-1", "status": "active"}])
        service = _service(repo, adapter)
        result = _run(service.transition_ticket(
            "TASK-2026-00377", "Completed", "done",
            evidence="gates and review verified",
            owner_instruction="Owner: cierralo", actor="owner"))
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("status"), "invariant")
        self.assertEqual(result.get("invariant"), "INV-003")
        self.assertEqual(adapter.puts, 0)
        self.assertEqual(len(repo.violations("INV-003")), 1)


class FakeERPClient:
    """Minimal Jardal ERP HTTP fake for the adapter transition path."""

    is_configured = True

    def __init__(self, row: dict[str, Any], post_status: str) -> None:
        self.row = dict(row)
        self.post_status = post_status
        self.posts = 0

    async def request(self, method: str, path: str, *,
                      body: Any = None, params: Any = None,
                      key: Any = None) -> dict[str, Any]:
        if method == "GET" and path.startswith("/api/v1/tasks/"):
            return {"task": dict(self.row)}
        if method == "POST" and path.endswith("/transitions"):
            self.posts += 1
            return {"task": {**self.row, "status": self.post_status,
                             "version": int(self.row["version"]) + 1}}
        raise AssertionError(f"unexpected {method} {path}")


def _row() -> dict[str, Any]:
    return {"id": "00000000-0000-0000-0000-000000000001",
            "businessReference": "TASK-2026-99999",
            "title": "Synthetic", "projectKey": "contract-test",
            "status": "Working", "kind": "task", "version": 1,
            "execution": {"ref": "api:synthetic", "autorun": False}}


def _adapter(post_status: str, sink: Any = None) -> Any:
    from business.work import JardalERPWork  # noqa: E402

    settings = SimpleNamespace(JARDAL_ERP_SITE_TIMEZONE="UTC",
                               JARDAL_ERP_URL="http://127.0.0.1:1")
    return JardalERPWork(settings, client=FakeERPClient(_row(), post_status),
                         on_violation=sink)


class AdapterRereadTests(unittest.TestCase):
    def test_matching_post_status_emits_nothing(self) -> None:
        seen: list[Any] = []
        adapter = _adapter("Completed", seen.append)
        result = _run(adapter.transition_status(
            "TASK-2026-99999", "Completed", reason="done"))
        self.assertTrue(result.get("ok"))
        self.assertEqual(seen, [])

    def test_mismatching_post_status_reports_inv002(self) -> None:
        seen: list[Any] = []
        adapter = _adapter("Working", seen.append)
        result = _run(adapter.transition_status(
            "TASK-2026-99999", "Completed", reason="done"))
        self.assertTrue(result.get("ok"))
        self.assertEqual(len(seen), 1)
        payload = seen[0]
        self.assertEqual(payload["violation"]["invariant_id"], "INV-002")
        self.assertEqual(payload["doc"]["kind"], "invariant_violation")
        self.assertEqual(payload["doc"]["severity"], "critical")
        self.assertEqual(payload["doc"]["attribution"], "engine")

    def test_throwing_sink_never_breaks_the_transition(self) -> None:
        def _boom(payload: Any) -> None:
            raise RuntimeError("sink down")

        adapter = _adapter("Working", _boom)
        result = _run(adapter.transition_status(
            "TASK-2026-99999", "Completed", reason="done"))
        self.assertTrue(result.get("ok"))


if __name__ == "__main__":
    unittest.main()
