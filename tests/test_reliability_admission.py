"""Hunter admission gate (design v2 section 9.3).

Targeted unit tests with an ERP fake, an injected clock and synthetic
execution tickets. No network, no CLI, no LLM (epic TASK-2026-00365,
ticket TASK-2026-00387).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import admission as adm  # noqa: E402
from reliability import publisher as pub  # noqa: E402

PROBLEM_KEY = "engine|abc123"
PROPOSAL = {
    "hypothesis": {"cause": "race on requeue", "confidence": "supported"},
    "proposed_change": "guard the requeue",
    "target_components": ["dispatcher"],
    "regression_test": "replays the race with fakes",
    "recovery": "none",
}
DIGEST = pub.proposal_hash(PROPOSAL)
RESOURCE = adm.approval_resource_for(PROBLEM_KEY, DIGEST)


def _future(days: int = 7) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _past(days: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _hunter_task(**override: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "exec-hunter-1",
        "status": "queued",
        "project": "jardal-openceo",
        "source": "spillover",
        "source_id": f"reliability:problem:{PROBLEM_KEY}",
        "autorun": True,
        "params": {
            "_hunter": {
                "problem_key": PROBLEM_KEY,
                "proposal_hash": DIGEST,
                "approval_id": "apr-1",
            },
            "_erp_decisions": {
                "dec-1": {
                    "verdict": "approved",
                    "approval_id": "apr-1",
                    "applied_at": "2026-10-29T12:00:00+00:00",
                    "actor_kind": "human",
                    "actor_id": "owner-1",
                    "version": 3,
                },
            },
        },
    }
    task.update(override)
    return task


def _live_approval(**override: Any) -> dict[str, Any]:
    approval: dict[str, Any] = {
        "id": "apr-1",
        "status": "approved",
        "resource": RESOURCE,
        "version": 3,
        "decision": {"verdict": "approved",
                     "actor": {"kind": "human", "id": "owner-1"}},
        "expires_at": _future(),
    }
    approval.update(override)
    return approval


class FakeReader:
    """In-memory ERP approval reader behind the gate protocol."""

    def __init__(self, approvals: dict[str, Any] | None = None,
                 fail: bool = False) -> None:
        self.approvals = approvals or {}
        self.fail = fail
        self.calls: list[str] = []

    async def get_approval(self, identifier: str) -> dict[str, Any]:
        self.calls.append(identifier)
        if self.fail:
            raise ConnectionError("ERP is down")
        found = self.approvals.get(identifier)
        if found is None:
            raise KeyError(identifier)
        return dict(found)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class DetectionTest(unittest.TestCase):
    def test_non_hunter_is_not_detected(self) -> None:
        task = {"task_id": "t", "source": "api",
                "source_id": "api:123", "params": {}}
        self.assertFalse(adm.is_hunter_task(task))
        self.assertIsNone(adm.hunter_claim(task))

    def test_source_id_prefix_detects(self) -> None:
        task = {"task_id": "t", "source": "spillover",
                "source_id": "reliability:problem:x", "params": {}}
        self.assertTrue(adm.is_hunter_task(task))

    def test_params_marker_detects(self) -> None:
        task = {"task_id": "t", "source": "api", "source_id": "api:1",
                "params": {"_hunter": {"problem_key": "k",
                                       "proposal_hash": "h"}}}
        self.assertTrue(adm.is_hunter_task(task))

    def test_resource_references_problem_and_hash(self) -> None:
        resource = adm.approval_resource_for(PROBLEM_KEY, DIGEST)
        self.assertIn(PROBLEM_KEY, resource)
        self.assertIn(DIGEST, resource)

    def test_build_request_is_stable_and_keyed(self) -> None:
        first, key_first = adm.build_approval_request(
            PROBLEM_KEY, DIGEST, business_ref="TASK-2026-00001")
        second, key_second = adm.build_approval_request(PROBLEM_KEY, DIGEST)
        self.assertEqual(first["resource"], second["resource"])
        self.assertEqual(key_first, key_second)
        self.assertIn(DIGEST, first["resource"])


class AdmissionCheckTest(unittest.TestCase):
    def test_allowed_without_any_erp_read_when_not_hunter(self) -> None:
        reader = FakeReader()
        task = {"task_id": "t", "source": "api", "source_id": "api:1",
                "params": {}}
        verdict = _run(adm.check_admission(task, approvals=reader))
        self.assertEqual(verdict, {"allowed": True, "reason": "not_hunter"})
        self.assertEqual(reader.calls, [])

    def test_autorun_without_approval_is_blocked(self) -> None:
        task = _hunter_task()
        params = dict(task["params"])
        hunter = dict(params["_hunter"])
        hunter["approval_id"] = None
        params["_hunter"] = hunter
        task["params"] = params
        verdict = _run(adm.check_admission(
            task, approvals=FakeReader({"apr-1": _live_approval()})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "no_approval")

    def test_missing_claim_is_blocked(self) -> None:
        task = _hunter_task(params={})
        verdict = _run(adm.check_admission(
            task, approvals=FakeReader({"apr-1": _live_approval()})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "no_hunter_claim")

    def test_older_proposal_approval_does_not_authorize(self) -> None:
        stale = _live_approval(resource=adm.approval_resource_for(
            PROBLEM_KEY, "old-hash"))
        verdict = _run(adm.check_admission(
            _hunter_task(), approvals=FakeReader({"apr-1": stale})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "stale_approval")

    def test_agent_decision_does_not_authorize(self) -> None:
        task = _hunter_task()
        books = dict(task["params"]["_erp_decisions"])
        entry = dict(books["dec-1"])
        entry["actor_kind"] = "agent"
        books["dec-1"] = entry
        params = dict(task["params"])
        params["_erp_decisions"] = books
        task["params"] = params
        verdict = _run(adm.check_admission(
            task, approvals=FakeReader({"apr-1": _live_approval()})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "agent_decision")

    def test_live_agent_actor_does_not_authorize(self) -> None:
        live = _live_approval(decision={"verdict": "approved",
                                        "actor": {"kind": "agent",
                                                  "id": "agent-9"}})
        verdict = _run(adm.check_admission(
            _hunter_task(), approvals=FakeReader({"apr-1": live})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "agent_decision")

    def test_erp_down_does_not_execute(self) -> None:
        verdict = _run(adm.check_admission(
            _hunter_task(), approvals=FakeReader(fail=True)))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approval_unverifiable")

    def test_no_reader_is_fail_closed_for_hunter(self) -> None:
        verdict = _run(adm.check_admission(_hunter_task(), approvals=None))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approval_unverifiable")

    def test_verified_human_approval_allows(self) -> None:
        verdict = _run(adm.check_admission(
            _hunter_task(), approvals=FakeReader({"apr-1": _live_approval()})))
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approved")

    def test_expired_approval_is_blocked(self) -> None:
        live = _live_approval(expires_at=_past())
        verdict = _run(adm.check_admission(
            _hunter_task(), approvals=FakeReader({"apr-1": live})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approval_expired")

    def test_denied_verdict_is_blocked(self) -> None:
        live = _live_approval(status="decided",
                              decision={"verdict": "denied",
                                        "actor": {"kind": "human",
                                                  "id": "owner-1"}})
        verdict = _run(adm.check_admission(
            _hunter_task(), approvals=FakeReader({"apr-1": live})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approval_not_approved")

    def test_recorded_denial_is_blocked(self) -> None:
        task = _hunter_task()
        books = dict(task["params"]["_erp_decisions"])
        entry = dict(books["dec-1"])
        entry["verdict"] = "denied"
        books["dec-1"] = entry
        params = dict(task["params"])
        params["_erp_decisions"] = books
        task["params"] = params
        verdict = _run(adm.check_admission(
            task, approvals=FakeReader({"apr-1": _live_approval()})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approval_denied")

    def test_attach_approval_registers_id(self) -> None:
        task = {"task_id": "t", "params": {}}
        updated = adm.attach_approval(task, approval_id="apr-7",
                                      problem_key=PROBLEM_KEY,
                                      proposal_hash=DIGEST)
        hunter = updated["params"]["_hunter"]
        self.assertEqual(hunter["approval_id"], "apr-7")
        self.assertEqual(hunter["proposal_hash"], DIGEST)
        self.assertEqual(task["params"], {})


class PublisherApprovalTest(unittest.TestCase):
    def test_fix_publish_requests_approval_and_registers_id(self) -> None:
        seen: dict[str, Any] = {}

        class ApprovingErp:
            async def find_by_source_id(
                    self, source: str, source_id: str) -> Any:
                return None

            async def create_ticket(self, payload: dict) -> dict:
                seen.update(payload)
                return {"business_ref": "TASK-2026-90001", **payload}

            async def count_pending_hunter(self) -> int:
                return 0

            async def update_ticket(
                    self, business_ref: str, patch: dict) -> dict:
                raise AssertionError("no update expected")

            async def add_comment(
                    self, business_ref: str, content: str) -> None:
                pass

            async def request_approval(
                    self, request: dict, *, key: str) -> dict:
                seen["approval_request"] = request
                seen["approval_key"] = key
                assert DIGEST in request["resource"]
                assert PROBLEM_KEY in request["resource"]
                return {"id": "apr-42", "status": "pending"}

        problem = {
            "_id": f"engine|{PROBLEM_KEY}", "scope": "engine",
            "kind": "exception", "component": "dispatcher",
            "attribution": "engine", "severity_max": "high",
            "status": "diagnosed",
            "counts": {"occurrences": 5, "distinct_tickets": 3,
                       "distinct_projects": 1},
            "problem_key": PROBLEM_KEY, "version": 1,
        }
        outcome = _run(pub.publish(
            problem, ticket_type="fix", erp=ApprovingErp(),
            proposal=dict(PROPOSAL),
            tickets_cfg={"enabled": True, "max_pending_owner": 5}))
        self.assertEqual(outcome["status"], "published")
        self.assertEqual(seen["approval_id"], "apr-42")
        ticket = outcome["problem_update"]["ticket"]
        self.assertEqual(ticket["approval_id"], "apr-42")
        self.assertIn(DIGEST, seen["approval_request"]["resource"])

    def test_publish_without_requester_keeps_previous_flow(self) -> None:
        class PlainErp:
            async def find_by_source_id(
                    self, source: str, source_id: str) -> Any:
                return None

            async def create_ticket(self, payload: dict) -> dict:
                return {"business_ref": "TASK-2026-90002", **payload}

            async def count_pending_hunter(self) -> int:
                return 0

            async def update_ticket(
                    self, business_ref: str, patch: dict) -> dict:
                raise AssertionError("no update expected")

            async def add_comment(
                    self, business_ref: str, content: str) -> None:
                pass

        problem = {
            "_id": f"engine|{PROBLEM_KEY}", "scope": "engine",
            "kind": "exception", "component": "dispatcher",
            "attribution": "engine", "severity_max": "high",
            "status": "diagnosed",
            "counts": {"occurrences": 5, "distinct_tickets": 3,
                       "distinct_projects": 1},
            "problem_key": PROBLEM_KEY, "version": 1,
        }
        outcome = _run(pub.publish(
            problem, ticket_type="fix", erp=PlainErp(),
            proposal=dict(PROPOSAL),
            tickets_cfg={"enabled": True, "max_pending_owner": 5}))
        self.assertEqual(outcome["status"], "published")
        self.assertNotIn("approval_id", outcome["ticket"])
        self.assertIsNone(
            outcome["problem_update"]["ticket"]["approval_id"])

    def test_investigate_publish_requests_no_approval(self) -> None:
        calls: list[dict] = []

        class ApprovingErp:
            async def find_by_source_id(
                    self, source: str, source_id: str) -> Any:
                return None

            async def create_ticket(self, payload: dict) -> dict:
                return {"business_ref": "TASK-2026-90003", **payload}

            async def count_pending_hunter(self) -> int:
                return 0

            async def update_ticket(
                    self, business_ref: str, patch: dict) -> dict:
                raise AssertionError("no update expected")

            async def add_comment(
                    self, business_ref: str, content: str) -> None:
                pass

            async def request_approval(
                    self, request: dict, *, key: str) -> dict:
                calls.append(request)
                return {"id": "apr-9", "status": "pending"}

        problem = {
            "_id": f"engine|{PROBLEM_KEY}", "scope": "engine",
            "kind": "exception", "component": "dispatcher",
            "attribution": "engine", "severity_max": "high",
            "status": "diagnosed",
            "counts": {"occurrences": 5, "distinct_tickets": 3,
                       "distinct_projects": 1},
            "problem_key": PROBLEM_KEY, "version": 1,
        }
        outcome = _run(pub.publish(
            problem, ticket_type="investigate", erp=ApprovingErp(),
            tickets_cfg={"enabled": True, "max_pending_owner": 5}))
        self.assertEqual(outcome["status"], "published")
        self.assertEqual(calls, [])


class _FakeRepo:
    def __init__(self, tasks: list[dict]) -> None:
        self.tasks = {t["task_id"]: dict(t) for t in tasks}
        self.backup: dict | None = None

    async def list_tasks_by_status(
            self, status: Any, limit: int = 200) -> list[dict]:
        value = getattr(status, "value", status)
        return [dict(t) for t in self.tasks.values()
                if t.get("status") == value][:limit]

    async def due_waiting_tasks(self, now_utc: Any) -> list[dict]:
        return []

    async def get_task(self, task_id: str) -> dict | None:
        row = self.tasks.get(task_id)
        return dict(row) if row else None

    async def patch_task(self, task_id: str, updates: dict) -> dict | None:
        row = self.tasks.get(task_id)
        if row is None:
            return None
        row.update(updates)
        return dict(row)

    async def claim_queued_ticket(self, task_id: str) -> dict | None:
        row = self.tasks.get(task_id)
        if row is None or row.get("status") != "queued":
            return None
        row.update({"status": "in_progress", "waiting_reason": None,
                    "resume_at": None})
        return dict(row)

    async def get_dispatcher_backup(self) -> dict | None:
        return self.backup

    async def ensure_dispatcher_backup(
            self, next_run_at: Any, reason: str = "") -> dict:
        self.backup = {"next_run_at": next_run_at, "reason": reason}
        return dict(self.backup)

    async def clear_dispatcher_backup(self) -> bool:
        existed = self.backup is not None
        self.backup = None
        return existed

    async def repo_lock_holder(self, repo: str) -> dict | None:
        return None

    async def list_tasks_blocked_by(self, task_id: str) -> list[dict]:
        return []


class _FakeService:
    def __init__(self, repo: _FakeRepo) -> None:
        self._repo = repo

    def _task_allows_autorun(self, task: dict) -> bool:
        autorun = task.get("autorun")
        if autorun is None:
            return True
        return bool(autorun)

    async def blockers_open(self, task: dict) -> bool:
        return False

    async def patch_task(self, task_id: str, updates: dict) -> dict | None:
        return await self._repo.patch_task(task_id, updates)


class DispatcherGateTest(unittest.TestCase):
    def _dispatcher(self, repo: _FakeRepo, service: _FakeService,
                    launched: list, reader: Any) -> Any:
        from dispatcher import Dispatcher

        async def launcher(task_id: str) -> None:
            launched.append(task_id)

        return Dispatcher(
            repo=repo,
            settings=SimpleNamespace(
                DISPATCHER_BACKUP_SECONDS=300,
                DISPATCHER_MAX_ACTIVE_PER_PROJECT=10,
                DISPATCHER_MAX_ACTIVE_PER_OBJECTIVE=10),
            service=service,
            rate_monitor=None,
            launcher=launcher,
            repo_for_project=None,
            hunter_reader=reader)

    def test_hunter_without_approval_parks_and_never_launches(self) -> None:
        task = _hunter_task()
        params = dict(task["params"])
        hunter = dict(params["_hunter"])
        hunter["approval_id"] = None
        params["_hunter"] = hunter
        task["params"] = params
        repo = _FakeRepo([task])
        launched: list = []
        dispatcher = self._dispatcher(
            repo, _FakeService(repo), launched,
            FakeReader({"apr-1": _live_approval()}))
        outcome = _run(dispatcher.dispatch_cycle())
        self.assertEqual(outcome["launched"], [])
        self.assertEqual(outcome["parked"], 1)
        self.assertEqual(launched, [])
        stored = _run(repo.get_task("exec-hunter-1"))
        self.assertTrue(str(stored["waiting_reason"]).startswith(
            "hunter_approval:"))

    def test_hunter_with_verified_approval_launches(self) -> None:
        repo = _FakeRepo([_hunter_task()])
        launched: list = []
        dispatcher = self._dispatcher(
            repo, _FakeService(repo), launched,
            FakeReader({"apr-1": _live_approval()}))
        outcome = _run(dispatcher.dispatch_cycle())
        self.assertEqual(outcome["launched"], ["exec-hunter-1"])
        self.assertEqual(launched, ["exec-hunter-1"])

    def test_hunter_with_erp_down_parks(self) -> None:
        repo = _FakeRepo([_hunter_task()])
        launched: list = []
        dispatcher = self._dispatcher(
            repo, _FakeService(repo), launched, FakeReader(fail=True))
        outcome = _run(dispatcher.dispatch_cycle())
        self.assertEqual(outcome["launched"], [])
        self.assertEqual(launched, [])


class RunTaskGateTest(unittest.TestCase):
    def _service(self, repo: _FakeRepo) -> Any:
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._repo = repo
        service._settings = SimpleNamespace()
        service._business = None
        service._hub = SimpleNamespace(broadcast=AsyncMock())
        service._dispatcher_wake = None
        service._locks = {}
        service._project_locks = {}
        service._notion = None
        service._workflow_service = None
        return service

    def test_manual_run_without_approval_parks_instead_of_executing(
            self) -> None:
        task = _hunter_task(status="queued")
        params = dict(task["params"])
        hunter = dict(params["_hunter"])
        hunter["approval_id"] = None
        params["_hunter"] = hunter
        task["params"] = params
        repo = _FakeRepo([task])
        service = self._service(repo)
        result = _run(service.run_task("exec-hunter-1", force=True))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "queued")
        self.assertTrue(str(result["waiting_reason"]).startswith(
            "hunter_approval:"))

    def test_manual_run_with_verified_approval_passes_gate(self) -> None:
        repo = _FakeRepo([_hunter_task(status="queued")])
        service = self._service(repo)
        service.set_hunter_approval_reader(
            FakeReader({"apr-1": _live_approval()}))
        calls: list = []

        async def fake_graph_execute(task: dict, **kwargs: object) -> dict:
            calls.append(task["task_id"])
            raise RuntimeError("must not reach the graph without more fakes")

        service._graph = SimpleNamespace(execute=fake_graph_execute)
        service._plan_pedido_pre_stage = AsyncMock(
            side_effect=lambda task, force=False: (task, None))
        try:
            _run(service.run_task("exec-hunter-1", force=True))
        except Exception:
            pass
        # The gate passed: execution reached the graph (which the fake
        # then crashed). A denial would have parked without any call.
        self.assertEqual(calls, ["exec-hunter-1"])


class VersionRegressionTest(unittest.TestCase):
    def test_regressed_live_version_is_blocked(self) -> None:
        task = _hunter_task()
        verdict = _run(adm.check_admission(
            task, approvals=FakeReader({"apr-1": _live_approval(version=2)})))
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "stale_approval")

    def test_newest_recorded_entry_wins(self) -> None:
        task = _hunter_task()
        params = dict(task["params"])
        params["_erp_decisions"] = {
            "dec-old": {
                "verdict": "denied", "approval_id": "apr-1",
                "applied_at": "2026-10-29T11:00:00+00:00",
                "actor_kind": "human", "actor_id": "owner-1", "version": 2,
            },
            "dec-new": {
                "verdict": "approved", "approval_id": "apr-1",
                "applied_at": "2026-10-29T12:00:00+00:00",
                "actor_kind": "human", "actor_id": "owner-1", "version": 3,
            },
        }
        task["params"] = params
        verdict = _run(adm.check_admission(
            task, approvals=FakeReader({"apr-1": _live_approval()})))
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approved")


class ChangedProposalRepublishTest(unittest.TestCase):
    def _ticketed(self) -> dict[str, Any]:
        return {
            "_id": f"engine|{PROBLEM_KEY}", "scope": "engine",
            "kind": "exception", "component": "dispatcher",
            "attribution": "engine", "severity_max": "high",
            "status": "ticketed",
            "counts": {"occurrences": 5, "distinct_tickets": 3,
                       "distinct_projects": 1},
            "problem_key": PROBLEM_KEY, "version": 2,
            "ticket": {
                "business_ref": "TASK-2026-90001",
                "source_id": f"reliability:problem:{PROBLEM_KEY}",
                "type": "fix", "proposal_hash": "0" * 64,
                "approval_id": "apr-old",
            },
        }

    def test_changed_proposal_requests_new_approval(self) -> None:
        seen: dict[str, Any] = {}

        class ApprovingErp:
            async def find_by_source_id(
                    self, source: str, source_id: str) -> Any:
                return None

            async def create_ticket(self, payload: dict) -> dict:
                raise AssertionError("republish must update, not create")

            async def count_pending_hunter(self) -> int:
                return 0

            async def update_ticket(
                    self, business_ref: str, patch: dict) -> dict:
                seen["business_ref"] = business_ref
                seen["patch"] = patch
                return {"business_ref": business_ref, **patch}

            async def add_comment(
                    self, business_ref: str, content: str) -> None:
                pass

            async def request_approval(
                    self, request: dict, *, key: str) -> dict:
                seen["approval_request"] = request
                return {"id": "apr-43", "status": "pending"}

        outcome = _run(pub.publish(
            self._ticketed(), ticket_type="fix", erp=ApprovingErp(),
            proposal=dict(PROPOSAL),
            tickets_cfg={"enabled": True, "max_pending_owner": 5}))
        self.assertEqual(outcome["status"], "republished")
        self.assertEqual(seen["business_ref"], "TASK-2026-90001")
        self.assertEqual(seen["patch"]["approval_id"], "apr-43")
        self.assertEqual(seen["patch"]["proposal_hash"], DIGEST)
        self.assertIn(DIGEST, seen["approval_request"]["resource"])
        ticket = outcome["problem_update"]["ticket"]
        self.assertEqual(ticket["approval_id"], "apr-43")
        self.assertEqual(ticket["proposal_hash"], DIGEST)

    def test_same_proposal_keeps_hands_off_refusal(self) -> None:
        class QuietErp:
            async def find_by_source_id(
                    self, source: str, source_id: str) -> Any:
                return None

            async def create_ticket(self, payload: dict) -> dict:
                raise AssertionError("no create expected")

            async def count_pending_hunter(self) -> int:
                return 0

            async def update_ticket(
                    self, business_ref: str, patch: dict) -> dict:
                raise AssertionError("no update expected")

            async def add_comment(
                    self, business_ref: str, content: str) -> None:
                pass

        proposal = {"hypothesis": {"cause": "x"},
                    "proposed_change": "y",
                    "target_components": ["dispatcher"],
                    "regression_test": "z",
                    "recovery": "none"}
        problem = self._ticketed()
        problem["ticket"] = dict(problem["ticket"])
        problem["ticket"]["proposal_hash"] = pub.proposal_hash(proposal)
        outcome = _run(pub.publish(
            problem, ticket_type="fix", erp=QuietErp(),
            proposal=proposal,
            tickets_cfg={"enabled": True, "max_pending_owner": 5}))
        self.assertEqual(outcome["status"], "refused")


class HunterEvidenceWithoutRoutingTest(unittest.TestCase):
    """H1: a hunter approval.decision is recorded even when no
    human-wait routing applies, so the gate can verify it."""

    def _service(self, repo: Any) -> Any:
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._repo = repo
        service._settings = SimpleNamespace(
            JARDAL_ERP_OPENCEO_USER="openceo@jardal.app")
        service._business = None
        service._hub = SimpleNamespace(broadcast=AsyncMock())
        service._dispatcher_wake = None
        service._locks = {}
        service._project_locks = {}
        service._notion = None
        service._workflow_service = None
        service.apply_owner_decision = AsyncMock(
            return_value={"applied": False, "reason": "not_answerable"})
        return service

    def test_decision_evidence_recorded_then_gate_admits(self) -> None:
        task = _hunter_task()
        params = dict(task["params"])
        params.pop("_erp_decisions", None)
        task["params"] = params
        repo = _FakeRepo([task])
        service = self._service(repo)
        event = {
            "event_id": "evt-h1", "type": "approval.decision",
            "actor": {"kind": "human", "id": "owner-1"},
            "payload": {"approval_id": "apr-1", "decision_id": "dec-9",
                        "verdict": "approved", "version": 3,
                        "execution_task_id": "exec-hunter-1"},
        }
        outcome = _run(service.apply_erp_event(event))
        self.assertEqual(outcome["status"], "not_answerable")
        self.assertTrue(outcome["pending"])
        stored = _run(repo.get_task("exec-hunter-1"))
        books = stored["params"]["_erp_decisions"]
        self.assertEqual(books["dec-9"]["actor_kind"], "human")
        self.assertEqual(books["dec-9"]["approval_id"], "apr-1")
        verdict = _run(adm.check_admission(
            stored, approvals=FakeReader({"apr-1": _live_approval()})))
        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["reason"], "approved")

    def test_claim_fields_helper(self) -> None:
        self.assertIsNone(adm.claim_fields(None))
        self.assertIsNone(adm.claim_fields({"problem_key": "k"}))
        fields = adm.claim_fields(
            {"problem_key": "k", "proposal_hash": "h",
             "approval_id": "apr-1"})
        self.assertEqual(fields, {
            "problem_key": "k", "proposal_hash": "h",
            "approval_id": "apr-1"})


if __name__ == "__main__":
    unittest.main()
