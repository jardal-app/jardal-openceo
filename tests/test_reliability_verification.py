"""Fix verification by exposure, fix cycle and regressions (v2 section 11).

Targeted unit tests with fake stores, an injected clock, an injected
containment checker and synthetic git repos. No network, no CLI, no LLM
(epic TASK-2026-00365, ticket TASK-2026-00388).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import verification as ver  # noqa: E402
from reliability import worker as wrk  # noqa: E402


def _utc(day: int = 29, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc)


def _problem(**override: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "_id": "engine|abc123",
        "schema_version": 1,
        "scope": "engine",
        "kind": "exception",
        "component": "dispatcher",
        "attribution": "engine",
        "attribution_rule": "A3",
        "severity_max": "high",
        "status": "ticketed",
        "counts": {"occurrences": 3, "suppressed": 0,
                   "distinct_tickets": 2, "distinct_projects": 1},
        "first_seen": _utc(20),
        "last_seen": _utc(29),
        "first_release": "rel0",
        "releases_seen": ["rel0"],
        "triage": {"eligible": True, "reason": "engine_normal_threshold"},
        "diagnosis": {"count": 1, "result": "fix"},
        "ticket": {"business_ref": "TASK-2026-00431",
                   "source_id": "reliability:problem:engine|abc123",
                   "type": "fix"},
        "fix": {"sha": None, "merged_at": None, "deployed_release": None,
                "baseline_rate": None, "required_exposures": None,
                "exposures_since": 0, "occurrences_since": 0},
        "links": {"duplicate_of": None, "grouped_under": None},
        "mute": {"reason": None, "actor": None, "until": None},
        "affected_open_tasks": [],
        "fingerprint": "abc123",
        "fingerprint_version": 1,
        "problem_key": "engine|abc123",
        "version": 1,
        "updated_at": _utc(),
    }
    doc.update(override)
    return doc


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def to_list(self, length: int = 100) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self._docs[:length]]


def _matches(doc: dict[str, Any], filt: dict[str, Any]) -> bool:
    for key, want in filt.items():
        got = doc.get(key)
        if isinstance(want, dict) and "$gte" in want:
            if not str(got or "") >= str(want["$gte"]):
                return False
        elif isinstance(want, dict) and "$in" in want:
            if got not in want["$in"]:
                return False
        elif got != want:
            return False
    return True


class FakeColl:
    """Minimal async Mongo collection behind the verification flows."""

    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        for doc in docs or []:
            self.docs[str(doc.get("_id"))] = dict(doc)
        self.updates: list[tuple[dict, dict]] = []

    async def find_one(self, filt: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self.docs.values():
            if _matches(doc, filt):
                return dict(doc)
        return None

    def find(self, filt: dict[str, Any]) -> FakeCursor:
        return FakeCursor(
            [doc for doc in self.docs.values() if _matches(doc, filt)])

    async def update_one(self, filt: dict[str, Any],
                         update: dict[str, Any]) -> None:
        self.updates.append((dict(filt), dict(update)))
        doc = self.docs.get(str(filt.get("_id")))
        if doc is None:
            return
        for key, value in update.get("$set", {}).items():
            doc[key] = value
        for key, value in update.get("$inc", {}).items():
            doc[key] = int(doc.get(key, 0) or 0) + int(value)


class FakeSink:
    def __init__(self) -> None:
        self.reopened: list[tuple[str, str]] = []

    async def reopen_ticket(
            self, business_ref: str, note: str) -> dict[str, Any]:
        self.reopened.append((business_ref, note))
        return {"business_ref": business_ref, "status": "Open"}


class RuleOfThreeTest(unittest.TestCase):
    def test_five_percent_needs_sixty(self) -> None:
        need, capped = ver.required_exposures_for(0.05)
        self.assertEqual(need, 60)
        self.assertFalse(capped)

    def test_certain_failure_clamps_to_minimum(self) -> None:
        need, capped = ver.required_exposures_for(1.0)
        self.assertEqual(need, 20)
        self.assertFalse(capped)

    def test_rare_failure_clamps_to_maximum_and_notes_it(self) -> None:
        need, capped = ver.required_exposures_for(0.001)
        self.assertEqual(need, 500)
        self.assertTrue(capped)

    def test_baseline_without_data_defaults_to_five_percent(self) -> None:
        self.assertEqual(ver.baseline_rate_for(5, 0), 0.05)
        self.assertEqual(ver.baseline_rate_for(0, 0), 0.05)
        self.assertEqual(ver.baseline_rate_for("junk", "junk"), 0.05)

    def test_baseline_is_occurrences_over_exposures(self) -> None:
        self.assertAlmostEqual(ver.baseline_rate_for(3, 60), 0.05)


class FixCycleTest(unittest.TestCase):
    def test_sixty_clean_exposures_resolve(self) -> None:
        problem = _problem()
        updated, event = ver.register_fix(
            problem, "fixsha1", occurrences=3, exposures=60, now=_utc())
        self.assertEqual(event, "fix_merged")
        self.assertEqual(updated["fix"]["required_exposures"], 60)
        updated, event = ver.note_release(
            updated, "rel1", True, now=_utc())
        self.assertEqual(event, "verifying")
        for _ in range(59):
            updated, event = ver.note_exposure(updated, now=_utc())
            self.assertEqual(event, "counted")
        self.assertEqual(
            updated["status"], "verifying")
        updated, event = ver.note_exposure(
            updated, release_sha="rel1", now=_utc())
        self.assertEqual(event, "resolved")
        self.assertEqual(updated["status"], "resolved")

    def test_fix_without_traffic_stays_verifying(self) -> None:
        problem = _problem()
        updated, _ = ver.register_fix(
            problem, "fixsha1", occurrences=3, exposures=60, now=_utc())
        updated, _ = ver.note_release(updated, "rel1", True, now=_utc())
        for _ in range(5):
            updated, event = ver.note_exposure(updated, now=_utc())
            self.assertEqual(event, "counted")
        self.assertEqual(updated["status"], "verifying")

    def test_no_resolution_by_time_alone(self) -> None:
        problem = _problem()
        updated, _ = ver.register_fix(
            problem, "fixsha1", occurrences=3, exposures=60,
            now=_utc(day=1))
        updated, _ = ver.note_release(updated, "rel1", True, now=_utc(2))
        updated, event = ver.note_exposure_counted(
            updated, 0, now=_utc(29))
        self.assertEqual(event, "counted")
        self.assertEqual(updated["status"], "verifying")

    def test_release_without_fix_keeps_waiting(self) -> None:
        problem = _problem()
        updated, _ = ver.register_fix(
            problem, "fixsha1", occurrences=3, exposures=60, now=_utc())
        same, event = ver.note_release(updated, "rel0", False, now=_utc())
        self.assertEqual(event, "waiting_for_release")
        self.assertEqual(same["status"], "fix_merged")

    def test_same_sha_registration_is_idempotent(self) -> None:
        problem = _problem()
        updated, _ = ver.register_fix(problem, "fixsha1", now=_utc())
        same, event = ver.register_fix(updated, "fixsha1", now=_utc())
        self.assertEqual(event, "already_registered")


class RegressionTest(unittest.TestCase):
    def _resolved(self) -> dict[str, Any]:
        problem = _problem()
        updated, _ = ver.register_fix(
            problem, "fixsha1", occurrences=3, exposures=60, now=_utc())
        updated, _ = ver.note_release(updated, "rel1", True, now=_utc())
        for _ in range(60):
            updated, _ = ver.note_exposure(updated, now=_utc())
        self.assertEqual(updated["status"], "resolved")
        return updated

    def test_later_recurrence_regresses_and_reopens_same_ticket(
            self) -> None:
        problem = self._resolved()
        updated, event = ver.note_incident(
            problem, True, release_sha="rel1", now=_utc())
        self.assertEqual(event, "regressed")
        self.assertEqual(updated["status"], "regressed")
        payload = ver.build_reopen(updated)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["business_ref"], "TASK-2026-00431")

    def test_recurrence_without_fix_is_not_regression(self) -> None:
        problem = _problem()
        updated, _ = ver.register_fix(
            problem, "fixsha1", occurrences=3, exposures=60, now=_utc())
        updated, _ = ver.note_release(updated, "rel1", True, now=_utc())
        before = dict(updated["fix"])
        same, event = ver.note_incident(
            updated, False, release_sha="rel0", now=_utc())
        self.assertEqual(event, "not_regression")
        self.assertEqual(same["status"], "verifying")
        self.assertEqual(same["fix"]["exposures_since"],
                        before["exposures_since"])
        self.assertEqual(same["fix"]["occurrences_since"], 0)

    def test_fix_attempt_incidents_never_regress(self) -> None:
        problem = self._resolved()
        outcome = _run(ver.check_incident(
            problem,
            {"from_reliability_ticket": True,
             "release": {"sha": "rel1"}},
            release_contains_fix=True))
        self.assertEqual(outcome["status"], "ignored")
        self.assertEqual(outcome["reason"], "fix_attempt")

    def test_unknown_containment_never_regresses(self) -> None:
        problem = self._resolved()
        outcome = _run(ver.check_incident(
            problem, {"release": {"sha": "rel2"}},
            release_contains_fix=None))
        self.assertEqual(outcome["status"], "ignored")
        self.assertEqual(outcome["reason"], "containment_unknown")


class CompletionHookTest(unittest.TestCase):
    def test_completed_hunter_ticket_registers_sha(self) -> None:
        problems = FakeColl([_problem()])
        task = {"source_id": "reliability:problem:engine|abc123",
                "params": {"_hunter": {"problem_key": "engine|abc123",
                                       "fix_sha": "fixsha9"}}}
        outcome = _run(ver.note_ticket_completed(
            task, "TASK-2026-00431", problems, now=_utc()))
        self.assertEqual(outcome["status"], "fix_merged")
        stored = _run(problems.find_one({"_id": "engine|abc123"}))
        assert stored is not None
        self.assertEqual(stored["status"], "fix_merged")
        self.assertEqual(stored["fix"]["sha"], "fixsha9")

    def test_non_hunter_ticket_is_ignored(self) -> None:
        problems = FakeColl([_problem()])
        outcome = _run(ver.note_ticket_completed(
            {"source_id": "github:1", "params": {}}, "TASK-1",
            problems, now=_utc()))
        self.assertEqual(outcome, {"status": "ignored",
                                   "reason": "not_hunter", "ticket": "TASK-1"})

    def test_hunter_ticket_without_sha_is_ignored(self) -> None:
        problems = FakeColl([_problem()])
        task = {"source_id": "reliability:problem:engine|abc123",
                "params": {"_hunter": {"problem_key": "engine|abc123"}}}
        outcome = _run(ver.note_ticket_completed(
            task, "TASK-2026-00431", problems, now=_utc()))
        self.assertEqual(outcome["reason"], "no_fix_sha")


class BootEvaluationTest(unittest.TestCase):
    def test_boot_moves_merged_to_verifying_resolves_and_regresses(
            self) -> None:
        merged = _problem()
        merged["_id"] = "engine|merged"
        merged["problem_key"] = "engine|merged"
        merged["status"] = "fix_merged"
        merged["fix"] = {"sha": "fixA", "merged_at": _utc(28),
                         "deployed_release": None, "baseline_rate": 0.05,
                         "required_exposures": 60, "required_capped": False,
                         "exposures_since": 0, "occurrences_since": 0}
        verifying = _problem()
        verifying["_id"] = "engine|verifying"
        verifying["problem_key"] = "engine|verifying"
        verifying["status"] = "verifying"
        verifying["fix"] = {"sha": "fixB", "merged_at": _utc(28),
                            "deployed_release": "rel9",
                            "baseline_rate": 0.05,
                            "required_exposures": 2,
                            "required_capped": False,
                            "exposures_since": 0, "occurrences_since": 0}
        bad = _problem()
        bad["_id"] = "engine|bad"
        bad["problem_key"] = "engine|bad"
        bad["status"] = "verifying"
        bad["fix"] = {"sha": "fixC", "merged_at": _utc(28),
                      "deployed_release": "rel9", "baseline_rate": 0.05,
                      "required_exposures": 500, "required_capped": True,
                      "exposures_since": 0, "occurrences_since": 0}
        problems = FakeColl([merged, verifying, bad])
        exposures = FakeColl([
            {"_id": "dispatcher|unknown|2026-10-29", "component": "dispatcher",
             "project": "unknown", "day": "2026-10-29", "count": 5},
        ])
        incidents = FakeColl([
            {"_id": "inc1", "problem_key": "engine|bad",
             "created_at": _utc(29), "from_reliability_ticket": False,
             "release": {"sha": "rel9"}},
        ])
        sink = FakeSink()
        outcome = _run(ver.evaluate_release(
            problems, exposures, "rel9", incidents=incidents,
            contains=lambda fix, rel: True, ticket_sink=sink, now=_utc()))
        self.assertEqual(outcome["evaluated"], 3)
        self.assertEqual(outcome["waiting"], 1)
        self.assertEqual(outcome["resolved"], 1)
        self.assertEqual(outcome["regressed"], 1)
        self.assertEqual(len(sink.reopened), 1)
        self.assertEqual(sink.reopened[0][0], "TASK-2026-00431")
        regressed = _run(problems.find_one({"_id": "engine|bad"}))
        assert regressed is not None
        self.assertEqual(regressed["status"], "regressed")
        self.assertTrue(regressed["ticket"]["reopen_pending"])

    def test_boot_without_store_stays_empty(self) -> None:
        outcome = _run(ver.evaluate_release(
            None, None, "rel9", now=_utc()))
        self.assertEqual(outcome["evaluated"], 0)


class WorkerFastPathTest(unittest.TestCase):
    def test_same_release_incident_regresses_verifying_problem(self) -> None:
        key = wrk.problem_id_for("engine", "abc123", 1)
        problem = _problem()
        problem["_id"] = key
        problem["problem_key"] = key
        problem["status"] = "verifying"
        problem["fix"] = {"sha": "fixsha1", "merged_at": _utc(28),
                          "deployed_release": "rel7", "baseline_rate": 0.05,
                          "required_exposures": 60, "required_capped": False,
                          "exposures_since": 10, "occurrences_since": 0}
        problems = FakeColl([problem])
        incidents = FakeColl([])

        class FakeDb(dict):
            def __getitem__(self, key: str) -> FakeColl:
                if key == wrk.PROBLEMS_COLLECTION:
                    return problems
                return incidents

        incident = {
            "_id": "inc-live", "kind": "exception",
            "component": "dispatcher", "attribution": "engine",
            "severity": "high", "fingerprint": "abc123",
            "fingerprint_version": 1, "engine": {},
            "release": {"sha": "rel7", "dirty": False},
            "created_at": _utc(), "schema_version": 1,
        }
        outcome = _run(wrk.process_incident(
            FakeDb(), incident, now=_utc()))
        self.assertEqual(outcome["reason"], "regressed_same_release")
        stored = _run(problems.find_one({"_id": key}))
        assert stored is not None
        self.assertEqual(stored["status"], "regressed")
        self.assertTrue(stored["ticket"]["reopen_pending"])


class GitAncestorTest(unittest.TestCase):
    @staticmethod
    def _repo_with_two_commits() -> tuple[tempfile.TemporaryDirectory, str,
                                          str]:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        for args in (["init"], ["config", "user.email", "t@t"],
                     ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=root, check=True,
                           capture_output=True)
        (root / "a.txt").write_text("a")
        subprocess.run(["git", "add", "a.txt"], cwd=root, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "a"], cwd=root, check=True,
                       capture_output=True)
        first = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True).stdout.strip()
        (root / "b.txt").write_text("b")
        subprocess.run(["git", "add", "b.txt"], cwd=root, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "b"], cwd=root, check=True,
                       capture_output=True)
        second = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True).stdout.strip()
        return tmp, first, second

    def test_merge_base_containment_on_synthetic_repo(self) -> None:
        tmp, first, second = self._repo_with_two_commits()
        try:
            self.assertTrue(
                ver.git_is_ancestor(tmp.name, first, second))
            self.assertFalse(
                ver.git_is_ancestor(tmp.name, second, first))
            self.assertFalse(
                ver.git_is_ancestor(tmp.name, "deadbee", second))
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
