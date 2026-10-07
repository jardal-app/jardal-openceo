"""Tier C safety and simple-tier material failure (TASK-2026-00116, F1).

Acceptance for the autonomy routing policy §§22-35: the orchestrator
freezes the qualifying oracle before a C build (hash gate; a builder
touching a protected ref fails the ticket), a material simple-tier
failure resets + preserves evidence + updates the objective + invokes
the planner exactly once (no C->B->A->S staircase, no same-tier
requeue), promoted findings carry deterministic SHA->ticket->objective
attribution, and Tier C telemetry (§32) reports an unknown-by-default
late-failure rate. No network, no CLI, no LLM.
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

from agents.dev.session import (
    DevExecutionOutput,
    DevSessionManager,
    _SimpleBranch,
)
from agents.dev.tier_oracle import (
    freeze_oracle,
    is_qualifying_oracle,
    resolve_oracle_ref,
    verify_oracle,
)
from orchestration import inbox as inbox_mod
from agents.dev.tier_s.cycle import TierSCycle, _Ctx
from objectives import engine as objectives_mod
from objectives.engine import (
    InMemoryObjectiveStore,
    InMemoryTaskLister,
    _planner_prompt,
    _telemetry_after_run,
    blank_tier_c_telemetry,
    record_material_failure,
    record_tier_c,
    tier_c_late_failure_rate,
)
from settings import Settings
from state import CodingTool, TaskStatus


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
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    oracle_dir = repo / "tests"
    oracle_dir.mkdir()
    (oracle_dir / "test_oracle.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _oracle(oracle_type: str = "preexisting_tests",
            refs: Any = ("tests/test_oracle.py",)) -> dict[str, Any]:
    return {"type": oracle_type,
            "refs": [{"ref": ref} for ref in refs]}


def _manager() -> DevSessionManager:
    manager = DevSessionManager(Settings(), MagicMock())
    manager._task_service = None
    manager._rate_monitor = None
    manager._repo = None
    return manager


def _dev_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "project": {"repos": [{"branch_base": "main"}]},
        "dev": {"tier_s": {"budgets": {"gate_retries": 0}}},
    }
    config["dev"].update(overrides)
    return config


class OracleFreezeTests(unittest.TestCase):
    def test_freeze_records_path_hash_type_and_task(self) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        frozen = freeze_oracle(repo, base, _oracle(), "c1")
        self.assertIsNotNone(frozen)
        assert frozen is not None
        self.assertEqual(frozen["oracle_type"], "preexisting_tests")
        self.assertEqual(frozen["task_ref"], "c1")
        self.assertEqual(frozen["base_sha"], base)
        self.assertTrue(frozen["frozen_at"])
        (entry,) = frozen["refs"]
        self.assertEqual(entry["ref"], "tests/test_oracle.py")
        self.assertEqual(entry["path"], "tests/test_oracle.py")
        self.assertTrue(entry["resolved"])
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertEqual(verify_oracle(repo, frozen), [])

    def test_modified_protected_ref_is_detected(self) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        frozen = freeze_oracle(repo, base, _oracle(), "c1")
        assert frozen is not None
        (repo / "tests" / "test_oracle.py").write_text(
            "def test_value():\n    assert False\n", encoding="utf-8")
        self.assertEqual(verify_oracle(repo, frozen),
                         ["tests/test_oracle.py"])

    def test_deleted_protected_ref_is_detected(self) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        frozen = freeze_oracle(repo, base, _oracle(), "c1")
        assert frozen is not None
        (repo / "tests" / "test_oracle.py").unlink()
        self.assertEqual(verify_oracle(repo, frozen),
                         ["tests/test_oracle.py"])

    def test_other_file_changes_do_not_trip_the_gate(self) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        frozen = freeze_oracle(repo, base, _oracle(), "c1")
        assert frozen is not None
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertEqual(verify_oracle(repo, frozen), [])

    def test_test_node_id_resolves_to_its_file(self) -> None:
        repo = _scratch_repo(self)
        self.assertEqual(
            resolve_oracle_ref(repo, "tests/test_oracle.py::test_value"),
            "tests/test_oracle.py")

    def test_unresolvable_ref_is_recorded_unprotected(self) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        frozen = freeze_oracle(
            repo, base,
            _oracle(refs=("tests/test_oracle.py", "contract: golden v3")),
            "c1")
        assert frozen is not None
        by_ref = {entry["ref"]: entry for entry in frozen["refs"]}
        self.assertTrue(by_ref["tests/test_oracle.py"]["resolved"])
        self.assertFalse(by_ref["contract: golden v3"]["resolved"])
        self.assertNotIn("sha256", by_ref["contract: golden v3"])
        self.assertEqual(verify_oracle(repo, frozen), [])

    def test_builder_tests_never_freeze(self) -> None:
        repo = _scratch_repo(self)
        base = _git(repo, "rev-parse", "HEAD")
        self.assertFalse(is_qualifying_oracle(_oracle("builder_tests")))
        self.assertIsNone(
            freeze_oracle(repo, base, _oracle("builder_tests"), "c1"))
        self.assertFalse(is_qualifying_oracle(None))
        self.assertFalse(is_qualifying_oracle({"type": "preexisting_tests"}))
        self.assertIsNone(freeze_oracle(repo, base, None, "c1"))

    def test_traversal_and_outside_paths_do_not_resolve(self) -> None:
        repo = _scratch_repo(self)
        self.assertIsNone(resolve_oracle_ref(repo, "../outside.py"))
        self.assertIsNone(resolve_oracle_ref(repo, "tests/../../evil.py"))
        self.assertIsNone(resolve_oracle_ref(repo, "/etc/hostname"))
        self.assertIsNone(resolve_oracle_ref(repo, ""))
        self.assertIsNone(resolve_oracle_ref(repo, "tests"))

    def test_malformed_frozen_record_fails_closed(self) -> None:
        repo = _scratch_repo(self)
        self.assertEqual(verify_oracle(repo, None), [])
        self.assertEqual(verify_oracle(repo, {"refs": "nope"}),
                         ["oracle record unreadable"])
        self.assertEqual(
            verify_oracle(repo, {"refs": [
                {"ref": "x.py", "resolved": True}]}),
            ["x.py"])


def _open_work_branch(repo: Path, name: str = "work") -> str:
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", name)
    return base


def _c_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "task-c1",
        "ref": "c1",
        "title": "C work",
        "coding_tool": "tier_c",
        "objective_id": "OBJ-1",
        "project": "proj",
        "params": {
            "oracle": _oracle(),
            "tier_reason": "covered by tests; reversible",
            "work_type": "implementation",
        },
    }
    task.update(overrides)
    return task


class _FailingPlannerHarness:
    """Patched engine entry points: the planner runs zero tokens."""

    def __init__(self, test: unittest.TestCase,
                 tickets_created: int = 2) -> None:
        self._record = patch(
            "agents.dev.session.record_material_failure",
            new=AsyncMock(return_value={"title": "C work"}))
        self._plan = patch(
            "agents.dev.session.plan_objective",
            new=AsyncMock(return_value={"ok": True,
                                        "tickets_created": tickets_created}))
        test.addCleanup(self._record.stop)
        test.addCleanup(self._plan.stop)
        self.recorded = self._record.start()
        self.planned = self._plan.start()


class MaterialFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_branch_freezes_c_oracle_only(self) -> None:
        repo = _scratch_repo(self)
        manager = _manager()
        config = _dev_config()
        opened = manager._open_simple_branch(
            _c_task(), config, CodingTool.TIER_C, repo)
        assert isinstance(opened, _SimpleBranch)
        self.assertIsNotNone(opened.oracle_frozen)
        assert opened.oracle_frozen is not None
        self.assertEqual(opened.oracle_frozen["oracle_type"],
                         "preexisting_tests")
        self.assertTrue(opened.oracle_frozen["refs"][0]["resolved"])
        _git(repo, "checkout", "-q", "main")
        _git(repo, "branch", "-D", opened.branch)
        opened_b = manager._open_simple_branch(
            _c_task(), config, CodingTool.TIER_B, repo)
        assert isinstance(opened_b, _SimpleBranch)
        self.assertIsNone(opened_b.oracle_frozen)

    async def test_tampered_oracle_fails_and_replans_once(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        frozen = freeze_oracle(repo, base, _oracle(), "c1")
        assert frozen is not None
        (repo / "tests" / "test_oracle.py").write_text(
            "def test_value():\n    assert False\n", encoding="utf-8")
        manager = _manager()
        harness = _FailingPlannerHarness(self)
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="c1",
                               oracle_frozen=frozen)
        output = await manager._finish_simple_tool(
            _c_task(), _dev_config(), CodingTool.TIER_C,
            DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_C)
        self.assertIsNone(output.waiting_reason)
        self.assertIsNone(output.resume_at)
        kinds = [step["step"] for step in output.steps]
        self.assertIn("oracle", kinds)
        self.assertIn("material_failure", kinds)
        self.assertIn("oracle modified", output.error or "")
        self.assertIn("Planner replan: 2 ticket(s).",
                      output.last_response_summary or "")
        assert output.params is not None
        self.assertIn("oracle_frozen", output.params)
        self.assertEqual(output.params["material_failure"]["tier"], "tier_c")
        harness.recorded.assert_awaited_once()
        _, kwargs = harness.recorded.await_args
        self.assertEqual(kwargs["tier"], "tier_c")
        self.assertIn("oracle modified", kwargs["cause"])
        harness.planned.assert_awaited_once()
        # Candidate reset and dropped: main clean, oracle file restored.
        self.assertEqual(
            _git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertNotIn("work", _git(repo, "branch", "--list", "work"))
        self.assertIn("assert True",
                      (repo / "tests" / "test_oracle.py").read_text())

    async def test_gates_failed_replans_without_staircase(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        manager = _manager()
        harness = _FailingPlannerHarness(self, tickets_created=1)
        config = _dev_config(
            gates=[{"name": "red", "cmd": "false"}],
            tier_s={"budgets": {"gate_retries": 0}})
        task = _c_task(coding_tool="tier_b")
        task["params"] = dict(task["params"])
        del task["params"]["oracle"]
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="c1")
        output = await manager._finish_simple_tool(
            task, config, CodingTool.TIER_B,
            DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertEqual(output.coding_tool, CodingTool.TIER_B)
        self.assertIsNone(output.waiting_reason)
        self.assertIn("gates failed after 0 retries",
                      output.last_response_summary or "")
        self.assertNotIn("tier_s", output.last_response_summary or "")
        harness.recorded.assert_awaited_once()
        harness.planned.assert_awaited_once()

    async def test_size_brake_is_a_material_failure(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        manager = _manager()
        harness = _FailingPlannerHarness(self)
        config = _dev_config(
            tier_s={"budgets": {"lines_per_ticket": 1,
                                "hard_brake_factor": 1,
                                "gate_retries": 0}})
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="c1")
        output = await manager._finish_simple_tool(
            _c_task(), config, CodingTool.TIER_C,
            DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("over budget", output.last_response_summary or "")
        harness.recorded.assert_awaited_once()
        harness.planned.assert_awaited_once()

    async def test_standalone_ticket_fails_without_planner(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        manager = _manager()
        harness = _FailingPlannerHarness(self)
        config = _dev_config(
            gates=[{"name": "red", "cmd": "false"}],
            tier_s={"budgets": {"gate_retries": 0}})
        task = _c_task(coding_tool="tier_b")
        del task["objective_id"]
        branch = _SimpleBranch(repo=repo, branch="work", base="main",
                               base_sha=base, ref="c1")
        output = await manager._finish_simple_tool(
            task, config, CodingTool.TIER_B,
            DevExecutionOutput(status=TaskStatus.IN_PROGRESS), branch)
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("Material tier_b failure", output.error or "")
        self.assertNotIn("Planner replan",
                         output.last_response_summary or "")
        harness.recorded.assert_not_awaited()
        harness.planned.assert_not_awaited()

    async def test_parked_replan_is_evidence_not_an_error(self) -> None:
        repo = _scratch_repo(self)
        base = _open_work_branch(repo)
        (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        manager = _manager()
        with patch("agents.dev.session.record_material_failure",
                   new=AsyncMock(return_value={})), \
                patch("agents.dev.session.plan_objective",
                      new=AsyncMock(return_value={
                          "ok": False, "reason": "max_tickets_reached",
                          "detail": "20 lifetime tickets"})) as planned:
            branch = _SimpleBranch(repo=repo, branch="work", base="main",
                                   base_sha=base, ref="c1")
            output = await manager._material_simple_failure(
                _c_task(), _dev_config(), CodingTool.TIER_C, branch,
                "gates failed")
        self.assertEqual(output.status, TaskStatus.FAILED)
        self.assertIn("Planner replan parked (max_tickets_reached",
                      output.last_response_summary or "")
        planned.assert_awaited_once()

    async def test_record_tier_c_event_bridges_session_to_engine(self) -> None:
        docs = {"OBJ-1": {
            "objective_id": "OBJ-1",
            "material_failures": [],
            "planner_telemetry": {"tier_c": blank_tier_c_telemetry()},
        }}

        class _StubRepo:
            async def get_objective(
                    self, objective_id: str) -> Any:
                doc = docs.get(objective_id)
                return dict(doc) if doc else None

            async def patch_objective(
                    self, objective_id: str,
                    updates: dict[str, Any]) -> Any:
                docs[objective_id].update(updates)
                return dict(docs[objective_id])

        manager = _manager()
        manager._repo = _StubRepo()
        await manager._record_tier_c_event(_c_task(), "started")
        tier_c = docs["OBJ-1"]["planner_telemetry"]["tier_c"]
        self.assertEqual(tier_c["started"], 1)
        self.assertEqual(
            tier_c["by_oracle_type"]["preexisting_tests"]["started"], 1)
        (record,) = tier_c["c_ticket_records"]
        self.assertEqual(record["task_id"], "task-c1")
        self.assertEqual(record["tier_reason"],
                         "covered by tests; reversible")
        # No objective, no repo: silent no-ops, never an error.
        manager._repo = None
        await manager._record_tier_c_event(_c_task(), "started")
        manager._repo = _StubRepo()
        await manager._record_tier_c_event(
            _c_task(objective_id=""), "started")
        self.assertEqual(tier_c["started"], 1)


def _history_repo(test: unittest.TestCase) -> tuple[Path, str]:
    """Base -> buggy line (sha) -> unrelated change. Known SHA case."""
    repo = _scratch_repo(test)
    path = repo / "app.py"
    path.write_text("VALUE = 1\nbuggy_result = compute_now()\n",
                    encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "origin commit")
    sha = _git(repo, "rev-parse", "HEAD")
    path.write_text("VALUE = 1\nbuggy_result = compute_now()\nEXTRA = 0\n",
                    encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "later change")
    return repo, sha


def _candidate(sha: str, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "sha": sha,
        "task_id": "task-origin",
        "objective_id": "OBJ-1",
        "coding_tool": "tier_c",
        "tier_reason": "covered by tests; reversible",
        "oracle_type": "preexisting_tests",
        "oracle_refs": ["tests/test_oracle.py"],
        "files": ["app.py"],
    }
    item.update(overrides)
    return item


class AttributionTests(unittest.TestCase):
    def test_known_sha_attributes_deterministically(self) -> None:
        repo, sha = _history_repo(self)
        result = inbox_mod.attribute_finding(
            repo, "app.py", "buggy_result = compute_now()",
            [_candidate("0" * 40), _candidate(sha)])
        self.assertEqual(result["status"], "attributed")
        self.assertEqual(result["shas"], [sha])
        self.assertEqual(result["ticket_ids"], ["task-origin"])
        self.assertEqual(result["objective_id"], "OBJ-1")
        self.assertEqual(result["coding_tool"], "tier_c")
        self.assertEqual(result["tier_reason"],
                         "covered by tests; reversible")
        self.assertEqual(result["oracle_type"], "preexisting_tests")
        self.assertEqual(result["oracle_refs"], ["tests/test_oracle.py"])
        # Deterministic: same history, same record.
        again = inbox_mod.attribute_finding(
            repo, "app.py", "buggy_result = compute_now()",
            [_candidate(sha)])
        self.assertEqual(again, result)

    def test_multiline_quote_uses_longest_line(self) -> None:
        repo, sha = _history_repo(self)
        result = inbox_mod.attribute_finding(
            repo, "app.py", "x\nbuggy_result = compute_now()\n",
            [_candidate(sha)])
        self.assertEqual(result["status"], "attributed")
        self.assertEqual(result["shas"], [sha])

    def test_unknown_when_no_commit_introduced_the_line(self) -> None:
        repo, sha = _history_repo(self)
        result = inbox_mod.attribute_finding(
            repo, "app.py", "never_committed_line_xyz()",
            [_candidate(sha)])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "no-matching-commit")

    def test_unknown_when_no_known_ticket_matches(self) -> None:
        repo, _sha = _history_repo(self)
        result = inbox_mod.attribute_finding(
            repo, "app.py", "buggy_result = compute_now()",
            [_candidate("1" * 40, task_id="task-other")])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "no-known-ticket")

    def test_unknown_on_short_empty_or_missing_git(self) -> None:
        repo, sha = _history_repo(self)
        self.assertEqual(
            inbox_mod.attribute_finding(
                repo, "app.py", "short", [_candidate(sha)])["status"],
            "unknown")
        self.assertEqual(
            inbox_mod.attribute_finding(
                repo, "", "buggy_result = compute_now()",
                [_candidate(sha)])["reason"], "empty-finding")
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: plain.rmdir())
        self.assertEqual(
            inbox_mod.attribute_finding(
                plain, "app.py", "buggy_result = compute_now()",
                [_candidate(sha)])["reason"], "git-unavailable")

    def test_promotion_payload_carries_attribution(self) -> None:
        entry = {
            "_id": "fp1", "file": "app.py", "category": "correctness",
            "severity_max": "CRITICAL", "quote": "buggy_result",
            "anchor": "app.py:2", "families": ["a", "b"],
            "seen_in": [{"task_id": "task-x"}],
        }
        attributed = {
            "status": "attributed", "shas": ["abc"], "ticket_ids": ["t1"],
            "objective_id": "OBJ-1", "coding_tool": "tier_c",
            "tier_reason": "r", "oracle_type": "preexisting_tests",
            "oracle_refs": ["tests/test_oracle.py"],
        }
        payload = inbox_mod.promotion_ticket(entry, None, attributed)
        self.assertEqual(payload["params"]["attribution"], attributed)
        self.assertIn("Atribución (§31)", payload["description"])
        self.assertIn("t1", payload["description"])
        plain = inbox_mod.promotion_ticket(entry, None, None)
        self.assertNotIn("params", plain)
        self.assertNotIn("Atribución", plain["description"])
        unknown = inbox_mod.promotion_ticket(
            entry, None, {"status": "unknown", "reason": "x"})
        self.assertNotIn("params", unknown)


class PromotionRecordTests(unittest.IsolatedAsyncioTestCase):
    async def test_mark_promoted_stores_attribution(self) -> None:
        store = inbox_mod.InMemoryInboxStore()
        await store.upsert_entry("fp1", {"_id": "fp1", "status": "open"})
        attributed = {"status": "attributed", "ticket_ids": ["t1"]}
        await inbox_mod.mark_promoted(store, "fp1", "task-new", attributed)
        entry = await store.get_entry("fp1")
        assert entry is not None
        self.assertEqual(entry["status"], "promoted")
        self.assertEqual(entry["attribution"], attributed)
        await store.upsert_entry("fp2", {"_id": "fp2", "status": "open"})
        await inbox_mod.mark_promoted(
            store, "fp2", "task-new", {"status": "unknown"})
        entry = await store.get_entry("fp2")
        assert entry is not None
        self.assertNotIn("attribution", entry)


class TierCTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def _store(self, objective_id: str = "OBJ-1",
                     ) -> InMemoryObjectiveStore:
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": objective_id,
            "material_failures": [],
            "planner_telemetry": {
                "cycles": 1, "replans": 0, "no_progress_cycles": 0,
                "tickets_created": 1, "cancelled_unnecessary": 0,
                "agent_reassignments": 0, "tier_reclassifications": 0,
                "tier_c": blank_tier_c_telemetry(),
            },
        })
        return store

    async def test_material_failure_feeds_retry_rule_and_c_counter(
            self) -> None:
        store = await self._store()
        await record_tier_c(
            store, "OBJ-1", "started", task_id="task-c1",
            oracle_type="preexisting_tests", tier_reason="covered",
            work_type="implementation", project="proj")
        entry = await record_material_failure(
            store, "OBJ-1", task_id="task-c1", title="C work",
            tier="tier_c", cause="gates failed")
        self.assertEqual(entry["tier"], "tier_c")
        self.assertTrue(entry["at"])
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        self.assertEqual(len(objective["material_failures"]), 1)
        tier_c = objective["planner_telemetry"]["tier_c"]
        self.assertEqual(tier_c["material_replans"], 1)
        (record,) = tier_c["c_ticket_records"]
        self.assertEqual(record["outcome"], "failed")
        self.assertTrue(record["closed_at"])
        # A B failure records evidence but never touches the C counter.
        await record_material_failure(
            store, "OBJ-1", task_id="task-b1", title="B work",
            tier="tier_b", cause="too big")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        self.assertEqual(len(objective["material_failures"]), 2)
        self.assertEqual(
            objective["planner_telemetry"]["tier_c"]["material_replans"], 1)

    async def test_material_failure_on_unknown_objective_is_empty(
            self) -> None:
        store = InMemoryObjectiveStore()
        self.assertEqual(
            await record_material_failure(store, "OBJ-9", title="t",
                                          tier="tier_c", cause="c"), {})

    async def test_close_cycle_and_late_events_count(self) -> None:
        store = await self._store()
        await record_tier_c(store, "OBJ-1", "started", task_id="task-c1",
                            oracle_type="preexisting_tests")
        await record_tier_c(store, "OBJ-1", "cycle_failure")
        await record_tier_c(store, "OBJ-1", "cycle_failure")
        await record_tier_c(store, "OBJ-1", "closed", task_id="task-c1",
                            oracle_type="preexisting_tests",
                            attributable=True)
        await record_tier_c(store, "OBJ-1", "closed", task_id="task-c2",
                            oracle_type="none", attributable=False)
        await record_tier_c(store, "OBJ-1", "late_failure",
                            task_id="task-c1", severity="CRITICAL",
                            detail="fp1: abc")
        await record_tier_c(store, "OBJ-1", "unknown-event")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        tier_c = objective["planner_telemetry"]["tier_c"]
        self.assertEqual(tier_c["started"], 1)
        self.assertEqual(tier_c["closed"], 2)
        self.assertEqual(tier_c["closed_attributable"], 1)
        self.assertEqual(tier_c["cycle_failures"], 2)
        self.assertEqual(tier_c["late_failures"], 1)
        self.assertEqual(tier_c["late_failure_severity"], {"CRITICAL": 1})
        self.assertEqual(
            tier_c["by_oracle_type"]["preexisting_tests"],
            {"started": 1, "closed": 1})
        (late,) = tier_c["late_failure_records"]
        self.assertEqual(late["task_id"], "task-c1")

    async def test_late_failure_rate_unknown_until_attributable(
            self) -> None:
        self.assertIsNone(tier_c_late_failure_rate(None))
        self.assertIsNone(tier_c_late_failure_rate({}))
        self.assertIsNone(tier_c_late_failure_rate(
            {"closed_attributable": 0, "late_failure_records": []}))
        self.assertIsNone(tier_c_late_failure_rate(
            {"closed_attributable": "many"}))
        store = await self._store()
        await record_tier_c(store, "OBJ-1", "closed", task_id="task-c1",
                            attributable=True)
        await record_tier_c(store, "OBJ-1", "closed", task_id="task-c2",
                            attributable=True)
        await record_tier_c(store, "OBJ-1", "late_failure",
                            task_id="task-c1", severity="WARN")
        await record_tier_c(store, "OBJ-1", "late_failure",
                            task_id="task-c1", severity="CRITICAL")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        tier_c = objective["planner_telemetry"]["tier_c"]
        # Unique offending tickets over attributable closes: 1 / 2.
        self.assertEqual(tier_c_late_failure_rate(tier_c), 0.5)

    async def test_telemetry_after_run_preserves_tier_c(self) -> None:
        store = await self._store()
        await record_tier_c(store, "OBJ-1", "started", task_id="t")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        runs = [{"at": "x", "status": "active", "tickets_created": 1,
                 "complete": False}]
        tele = _telemetry_after_run(objective, runs)
        self.assertEqual(tele["tier_c"]["started"], 1)
        self.assertEqual(tele["cycles"], 1)
        legacy = _telemetry_after_run({"planner_telemetry": {}}, runs)
        self.assertEqual(legacy["tier_c"], blank_tier_c_telemetry())

    def test_planner_prompt_carries_material_failures(self) -> None:
        objective = {
            "objective_id": "OBJ-1",
            "anchor": {"goal": "g"},
            "material_failures": [
                {"title": "C work", "tier": "tier_c",
                 "cause": "gates failed after retries"},
            ],
        }
        prompt = _planner_prompt(objective, "", [], "", [])
        self.assertIn("MATERIAL FAILURES", prompt)
        self.assertIn("C work [tier_c]", prompt)
        self.assertIn("gates failed after retries", prompt)
        clean = _planner_prompt(
            {"objective_id": "OBJ-1", "anchor": {}}, "", [], "", [])
        self.assertIn("MATERIAL FAILURES", clean)


class PromotionAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def _cycle(self, test: unittest.TestCase, tier: str,
                     ) -> tuple[TierSCycle, SimpleNamespace,
                                dict[str, Any], InMemoryObjectiveStore]:
        repo, sha = _history_repo(test)
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-1",
            "tickets_log": [{"task_id": "task-origin", "title": "origin",
                             "tier": tier, "sha": sha, "files": ["app.py"]}],
            "material_failures": [],
            "planner_telemetry": {"tier_c": blank_tier_c_telemetry()},
        })
        tasks = InMemoryTaskLister({
            "task-origin": {
                "task_id": "task-origin", "coding_tool": tier,
                "params": {"tier_reason": "covered by tests",
                           "oracle": {"type": "preexisting_tests",
                                      "refs": [{"ref": "tests/t.py"}]}},
            },
        })
        env = SimpleNamespace(objectives=store, tasks=tasks)
        cycle = TierSCycle(MagicMock(), MagicMock(), MagicMock(),
                           MagicMock(), objective_env=env)
        ctx = SimpleNamespace(
            ticket={"task_id": "task-new", "objective_id": "OBJ-1",
                    "project": "proj"},
            repo=repo)
        entry = {"_id": "fp1", "file": "app.py",
                 "quote": "buggy_result = compute_now()",
                 "severity_max": "CRITICAL"}
        return cycle, ctx, entry, store

    async def test_c_origin_records_late_failure(self) -> None:
        cycle, ctx, entry, store = await self._cycle(self, "tier_c")
        result = await cycle._attribute_promotion(ctx, entry)
        assert result is not None
        self.assertEqual(result["status"], "attributed")
        self.assertEqual(result["ticket_ids"], ["task-origin"])
        self.assertEqual(result["coding_tool"], "tier_c")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        tier_c = objective["planner_telemetry"]["tier_c"]
        self.assertEqual(tier_c["late_failures"], 1)
        self.assertEqual(tier_c["late_failure_severity"], {"CRITICAL": 1})
        (late,) = tier_c["late_failure_records"]
        self.assertEqual(late["task_id"], "task-origin")

    async def test_non_c_origin_attributes_without_late_failure(
            self) -> None:
        cycle, ctx, entry, store = await self._cycle(self, "tier_b")
        result = await cycle._attribute_promotion(ctx, entry)
        assert result is not None
        self.assertEqual(result["status"], "attributed")
        self.assertEqual(result["coding_tool"], "tier_b")
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        self.assertEqual(
            objective["planner_telemetry"]["tier_c"]["late_failures"], 0)

    async def test_no_objective_returns_none(self) -> None:
        cycle, _ctx, entry, _store = await self._cycle(self, "tier_c")
        ctx = SimpleNamespace(ticket={"task_id": "task-new"}, repo=Path("."))
        self.assertIsNone(await cycle._attribute_promotion(ctx, entry))
        cycle._objective_env = None
        ctx = SimpleNamespace(
            ticket={"task_id": "t", "objective_id": "OBJ-1"}, repo=Path("."))
        self.assertIsNone(await cycle._attribute_promotion(ctx, entry))

    async def test_evaluate_promotions_carries_attribution(self) -> None:
        repo, sha = _history_repo(self)
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-1",
            "tickets_log": [{"task_id": "task-origin", "title": "origin",
                             "tier": "tier_c", "sha": sha,
                             "files": ["app.py"]}],
            "material_failures": [],
            "planner_telemetry": {"tier_c": blank_tier_c_telemetry()},
        })
        tasks = InMemoryTaskLister({
            "task-origin": {
                "task_id": "task-origin", "coding_tool": "tier_c",
                "params": {"tier_reason": "covered by tests",
                           "oracle": {"type": "preexisting_tests",
                                      "refs": [{"ref": "tests/t.py"}]}},
            },
        })
        inbox = inbox_mod.InMemoryInboxStore()
        await inbox.upsert_entry("fp1", {
            "_id": "fp1", "project": "proj", "file": "app.py",
            "category": "correctness", "severity_max": "CRITICAL",
            "quote": "buggy_result = compute_now()", "anchor": "",
            "strength": "strong", "source": "pre_existing",
            "families": ["a", "b"], "seen_in": [{"task_id": "task-x"}],
            "status": "open", "assigned_to": None, "promoted_to": None,
        })
        created: list[dict[str, Any]] = []

        async def create_ticket(
                payload: dict[str, Any]) -> dict[str, Any]:
            created.append(payload)
            return {"task_id": "task-fix"}

        env = SimpleNamespace(objectives=store, tasks=tasks)
        cycle = TierSCycle(MagicMock(), MagicMock(), MagicMock(),
                           MagicMock(), inbox=inbox,
                           create_ticket=create_ticket, objective_env=env)
        ctx = _Ctx(
            ticket={"task_id": "task-new", "objective_id": "OBJ-1",
                    "project": "proj"},
            repo=repo, ref="r", run_id="run", branch="b",
            base_branch="main", budgets={}, timeouts={}, waiting_cfg={},
            auto_merge=True, budget_exclude=[], gate_specs=[], roles={},
            inbox_fps=["fp1"])
        await cycle._evaluate_promotions(ctx)
        (payload,) = created
        self.assertEqual(
            payload["params"]["attribution"]["ticket_ids"], ["task-origin"])
        self.assertIn("Atribución (§31)", payload["description"])
        entry = await inbox.get_entry("fp1")
        assert entry is not None
        self.assertEqual(entry["status"], "promoted")
        self.assertEqual(entry["attribution"]["ticket_ids"], ["task-origin"])


if __name__ == "__main__":
    unittest.main()
