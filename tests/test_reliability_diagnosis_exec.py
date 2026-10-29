"""Diagnosis execution without tools and durable budget (design v2
sections 8.1, 8.3, 8.5 and 13.4; epic TASK-2026-00365, ticket
TASK-2026-00386).

Focused tests with a simulated executor, Mongo fakes and an injected
clock. No network, no CLI tokens, no LLM.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import diagnosis as diag  # noqa: E402
from reliability import diagnosis_exec as ex  # noqa: E402


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class DuplicateKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("duplicate")
        self.code = 11000


def _matches(doc: dict[str, Any], filt: dict[str, Any]) -> bool:
    for key, want in filt.items():
        value = doc.get(key)
        if isinstance(want, dict):
            for op, arg in want.items():
                if op == "$lt":
                    if value is None or not value < arg:
                        return False
                elif op == "$lte":
                    if value is None or not value <= arg:
                        return False
                elif op == "$gt":
                    if value is None or not value > arg:
                        return False
                elif op == "$gte":
                    if value is None or not value >= arg:
                        return False
                else:
                    return False
        elif value != want:
            return False
    return True


def _set_dotted(doc: dict[str, Any], key: str, value: Any) -> None:
    target = doc
    parts = key.split(".")
    for part in parts[:-1]:
        child = target.get(part)
        if not isinstance(child, dict):
            child = {}
            target[part] = child
        target = child
    target[parts[-1]] = value


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def to_list(self, length: int = 0) -> list[dict[str, Any]]:
        if length > 0:
            return [dict(doc) for doc in self._docs[:length]]
        return [dict(doc) for doc in self._docs]


class FakeColl:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def find(self, filt: dict[str, Any]) -> FakeCursor:
        return FakeCursor(
            [doc for doc in self.docs.values() if _matches(doc, filt)])

    async def find_one(self, filt: dict[str, Any]) -> Any:
        for doc in self.docs.values():
            if _matches(doc, filt):
                return dict(doc)
        return None

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        if doc["_id"] in self.docs:
            raise DuplicateKeyError()
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, filt: dict[str, Any],
                         update: dict[str, Any]) -> SimpleNamespace:
        for doc_id, doc in self.docs.items():
            if _matches(doc, filt):
                for key, value in update.get("$set", {}).items():
                    _set_dotted(doc, key, value)
                for key, value in update.get("$inc", {}).items():
                    doc[key] = int(doc.get(key, 0)) + int(value)
                self.docs[doc_id] = doc
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def delete_one(self, filt: dict[str, Any]) -> SimpleNamespace:
        for doc_id, doc in list(self.docs.items()):
            if _matches(doc, filt):
                del self.docs[doc_id]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class FakeDb:
    def __init__(self) -> None:
        self.budget = FakeColl()
        self.problems = FakeColl()
        self.incidents = FakeColl()

    def __getitem__(self, name: str) -> FakeColl:
        if name == "reliability_budget":
            return self.budget
        if name == "reliability_problems":
            return self.problems
        if name == "reliability_incidents":
            return self.incidents
        raise KeyError(name)


def _problem(problem_id: str = "problem-1",
             status: str = "open") -> dict[str, Any]:
    return {
        "_id": problem_id,
        "status": status,
        "counts": {"occurrences": 5, "distinct_tickets": 3},
        "diagnosis": {"count": 0, "last_at": None,
                      "occurrences_at_last": 0, "claim_until": None,
                      "result": None},
    }


def _diag_cfg(**override: Any) -> dict[str, Any]:
    from reliability.config import RELIABILITY_DEFAULTS
    import copy

    cfg = copy.deepcopy(RELIABILITY_DEFAULTS["diagnosis"])
    cfg.update(override)
    return cfg


def _toolless_candidate(name: str = "api-direct") -> dict[str, Any]:
    return {"name": name, "toolless": True, "input_via": "api",
            "output": "json", "max_output_tokens": 2000,
            "timeout_s": 300, "nested_recovery": False}


class ScriptedExecutor:
    """Simulated tool-less executor: scripted (returncode, stdout) pairs."""

    nested_recovery = False

    def __init__(self, script: list[tuple[int, str]]) -> None:
        self._script = list(script)
        self.runs = 0
        self.aborted = False
        self.inputs: list[str] = []

    async def run_text(self, prompt: str, *, timeout_s: int) -> Any:
        self.runs += 1
        self.inputs.append(prompt)
        code, out = self._script.pop(0)
        return code, out

    async def abort(self) -> None:
        self.aborted = True


def _valid_payload() -> dict[str, Any]:
    import json

    return json.dumps({
        "schema_version": 1,
        "classification": "engine_bug",
        "attribution": "engine",
        "cluster_consistent": True,
        "duplicate_of": None,
        "root_cause": "race on requeue",
        "confidence": "supported",
        "alternatives": [{"cause": "slow provider", "status": "open"}],
        "evidence_refs": ["problem:summary"],
        "proposed_change": "guard the requeue",
        "target_components": ["dispatcher"],
        "regression_test": "replays the race with fakes",
        "recovery": "none",
        "ticket_type": "fix",
        "unknowns": [],
    })


def _bundle() -> dict[str, Any]:
    problem = {"_id": "problem-1", "scope": "engine", "kind": "exception",
               "component": "dispatcher", "attribution": "engine",
               "attribution_rule": "A3",
               "counts": {"occurrences": 5, "distinct_tickets": 3,
                          "distinct_projects": 1},
               "releases_seen": ["abc"], "severity_max": "high"}
    incident = {"_id": "inc-01", "kind": "exception",
                "component": "dispatcher", "severity": "high",
                "created_at": _utc(2026, 10, 1),
                "release": {"sha": "abc", "dirty": False},
                "engine": {"task_id": "TASK-1", "project_key": "p",
                           "frames": ["a:b:1"]},
                "context": {"message": "boom"}}
    return diag.build_bundle(problem, [incident])


class ProfileTest(unittest.TestCase):
    def test_profile_is_toolless_and_catalog_stays_valid(self) -> None:
        from catalog import check_catalog, load_role_profile

        profile = load_role_profile("reliability_diagnosis")
        self.assertEqual(profile.skills, [])
        self.assertTrue(any("tool" in line.lower()
                            for line in profile.card.never))
        status = check_catalog()
        self.assertTrue(status.ok, status.problem)


class CertifyTest(unittest.TestCase):
    def test_toolless_candidate_certifies(self) -> None:
        ok, _ = ex.certify_toolless([_toolless_candidate()])
        self.assertTrue(ok)

    def test_tool_using_candidate_rejected(self) -> None:
        bad = _toolless_candidate()
        bad["toolless"] = False
        ok, reasons = ex.certify_toolless([bad])
        self.assertFalse(ok)
        self.assertTrue(reasons)

    def test_no_candidate_disables_and_fails_closed(self) -> None:
        ok, _ = ex.certify_toolless([])
        self.assertFalse(ok)
        self.assertEqual(ex.validate_diagnosis_runtime(
            _diag_cfg(enabled=True), toolless_ok=False), [
            "diagnosis: enabled without a certified tool-less executor "
            "(fail-closed)"])
        self.assertEqual(ex.validate_diagnosis_runtime(
            _diag_cfg(enabled=False), toolless_ok=False), [])


class BudgetTest(unittest.TestCase):
    def test_fourth_start_in_same_hour_waits(self) -> None:
        async def _scenario() -> None:
            db = FakeDb()
            cfg = _diag_cfg()
            moment = _utc(2026, 10, 29, 12, 0, 0)
            for index in range(3):
                pid = f"problem-{index}"
                await db.problems.insert_one(_problem(pid))
                outcome = await ex.consume_budget(
                    db.budget, db.problems, pid, cfg, now=moment)
                self.assertTrue(outcome["ok"], outcome)
                await db.problems.update_one(
                    {"_id": pid},
                    {"$set": {"status": "diagnosed",
                              "diagnosis.claim_until": None}})
            await db.problems.insert_one(_problem("problem-3"))
            outcome = await ex.consume_budget(
                db.budget, db.problems, "problem-3", cfg, now=moment)
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["reason"], "hourly_budget_exhausted")
            self.assertEqual(outcome["next_slot"],
                             _utc(2026, 10, 29, 13, 0, 0))

        asyncio.run(_scenario())

    def test_budget_survives_restart_without_refund(self) -> None:
        async def _scenario() -> None:
            db = FakeDb()
            cfg = _diag_cfg()
            moment = _utc(2026, 10, 29, 12, 0, 0)
            await db.problems.insert_one(_problem("problem-1"))
            first = await ex.consume_budget(
                db.budget, db.problems, "problem-1", cfg, now=moment)
            self.assertTrue(first["ok"], first)
            stored = await db.problems.find_one({"_id": "problem-1"})
            self.assertEqual(stored["status"], "diagnosing")
            later = moment + timedelta(seconds=601)
            from reliability import worker as _worker

            expired = await _worker._expire_claims(db, now=later)
            self.assertEqual(expired, 1)
            reopened = await db.problems.find_one({"_id": "problem-1"})
            self.assertEqual(reopened["status"], "open")
            self.assertIsNone(
                reopened["diagnosis"]["claim_until"])
            hour = await db.budget.find_one({"_id": ex.hour_id(moment)})
            self.assertEqual(hour["count"], 1)
            await db.problems.insert_one(_problem("problem-2"))
            second = await ex.consume_budget(
                db.budget, db.problems, "problem-2", cfg, now=later)
            self.assertTrue(second["ok"], second)

        asyncio.run(_scenario())

    def test_daily_exhaustion_does_not_leak_hourly_slot(self) -> None:
        async def _scenario() -> None:
            db = FakeDb()
            cfg = _diag_cfg(max_starts_per_day=1)
            moment = _utc(2026, 10, 29, 12, 0, 0)
            await db.problems.insert_one(_problem("problem-0"))
            first = await ex.consume_budget(
                db.budget, db.problems, "problem-0", cfg, now=moment)
            self.assertTrue(first["ok"], first)
            await db.problems.update_one(
                {"_id": "problem-0"},
                {"$set": {"status": "diagnosed",
                          "diagnosis.claim_until": None}})
            await db.problems.insert_one(_problem("problem-1"))
            refused = await ex.consume_budget(
                db.budget, db.problems, "problem-1", cfg, now=moment)
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["reason"], "daily_budget_exhausted")
            self.assertEqual(refused["next_slot"],
                             _utc(2026, 10, 30, 0, 0, 0))
            hour = await db.budget.find_one({"_id": ex.hour_id(moment)})
            self.assertEqual(hour["count"], 1)

        asyncio.run(_scenario())


class RunTest(unittest.TestCase):
    def test_partial_output_with_nonzero_exit_rejected(self) -> None:
        async def _scenario() -> None:
            bundle = _bundle()
            refs = diag.manifest_refs(bundle)
            executor = ScriptedExecutor(
                [(1, '{"schema_version": 1, "classifica')])
            outcome = await ex.run_diagnosis(
                executor, bundle, refs, timeout_s=300)
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["error"], "nonzero_exit")
            self.assertEqual(outcome["processes_used"], 1)
            self.assertEqual(executor.runs, 1)

        asyncio.run(_scenario())

    def test_invalid_json_gets_one_repair_and_never_a_third(self) -> None:
        async def _scenario() -> None:
            bundle = _bundle()
            refs = diag.manifest_refs(bundle)
            executor = ScriptedExecutor(
                [(0, "not json"), (0, _valid_payload())])
            outcome = await ex.run_diagnosis(
                executor, bundle, refs, timeout_s=300)
            self.assertTrue(outcome["ok"], outcome)
            self.assertEqual(outcome["decision"], "fix")
            self.assertEqual(outcome["processes_used"], 2)
            self.assertEqual(executor.runs, 2)
            stuck = ScriptedExecutor([(0, "nope"), (0, "still nope")])
            failed = await ex.run_diagnosis(
                stuck, bundle, refs, timeout_s=300)
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["error"], "invalid_json")
            self.assertEqual(stuck.runs, 2)

        asyncio.run(_scenario())

    def test_timeout_kills_the_process_group(self) -> None:
        async def _scenario() -> None:
            bundle = _bundle()
            refs = diag.manifest_refs(bundle)
            executor = ex.SubprocessDiagnosisExecutor(
                [sys.executable, "-c",
                 "import time; time.sleep(30)"])
            outcome = await ex.run_diagnosis(
                executor, bundle, refs, timeout_s=1)
            self.assertFalse(outcome["ok"])
            self.assertEqual(outcome["error"], "timeout")
            self.assertTrue(executor.killed)
            self.assertEqual(outcome["processes_used"], 1)
            self.assertEqual(executor.runs, 1)

        asyncio.run(_scenario())


class RediagnosisTest(unittest.TestCase):
    def test_gates(self) -> None:
        fresh = _problem()
        self.assertTrue(ex.may_diagnose(fresh)[0])
        diagnosed = _problem()
        diagnosed["status"] = "diagnosed"
        diagnosed["diagnosis"]["count"] = 1
        diagnosed["diagnosis"]["occurrences_at_last"] = 5
        ok, reason = ex.may_diagnose(diagnosed)
        self.assertFalse(ok)
        self.assertEqual(reason, "needs_unknown_trigger")
        regressed = dict(diagnosed, status="regressed")
        self.assertTrue(ex.may_diagnose(regressed)[0])
        doubled = dict(diagnosed, status="open")
        doubled["counts"] = {"occurrences": 10, "distinct_tickets": 4}
        self.assertTrue(ex.may_diagnose(doubled)[0])
        self.assertTrue(ex.may_diagnose(diagnosed, owner_requested=True)[0])
        self.assertFalse(ex.may_diagnose(_problem(status="muted"))[0])


if __name__ == "__main__":
    unittest.main()
