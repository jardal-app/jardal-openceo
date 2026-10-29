"""Reliability worker: Problems, triage, storm, mute and reconcile.

Targeted unit tests with Mongo fakes, an injected clock and temp runtime
dirs. No network, no CLI, no LLM (epic TASK-2026-00365, ticket TASK-2026-00378).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import worker  # noqa: E402
from reliability.capture import build_incident_doc  # noqa: E402
from reliability.fingerprint import FINGERPRINT_VERSION  # noqa: E402
from reliability.models import ProblemStatus  # noqa: E402


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class DuplicateKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("duplicate")
        self.code = 11000


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    async def to_list(self, length: int = 0) -> list[dict[str, Any]]:
        if length > 0:
            return [dict(doc) for doc in self._docs[:length]]
        return [dict(doc) for doc in self._docs]


def _matches(doc: dict[str, Any], filt: dict[str, Any]) -> bool:
    for key, want in filt.items():
        if isinstance(want, dict) and "$gte" in want:
            if doc.get(key) is None or doc[key] < want["$gte"]:
                return False
        elif doc.get(key) != want:
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


class FakeProblems:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.queries = 0

    async def count_documents(self, filt: dict[str, Any]) -> int:
        self.queries += 1
        return sum(1 for doc in self.docs.values()
                   if _matches(doc, filt))

    def find(self, filt: dict[str, Any]) -> FakeCursor:
        self.queries += 1
        return FakeCursor(
            [doc for doc in self.docs.values() if _matches(doc, filt)])

    async def find_one(self, filt: dict[str, Any]) -> dict[str, Any] | None:
        self.queries += 1
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

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        return "idx"


class FakeIncidents(FakeProblems):
    pass


class FakeDb:
    """Mapping source resolving both reliability collections."""

    def __init__(self) -> None:
        self.incidents = FakeIncidents()
        self.problems = FakeProblems()
        self.budget = FakeProblems()

    def __getitem__(self, name: str) -> FakeProblems:
        if name == "reliability_incidents":
            return self.incidents
        if name == "reliability_problems":
            return self.problems
        if name == "reliability_budget":
            return self.budget
        raise KeyError(name)


def _engine_frames() -> list[dict[str, str]]:
    return [{
        "file": "openceo-core/dispatcher.py",
        "function": "dispatch",
        "line": "10",
        "module": "dispatcher",
    }]


def _exception_incident(task: str, project: str, *,
                        now: datetime | None = None) -> dict[str, Any]:
    return build_incident_doc(
        kind="exception", component="dev_session", now=now,
        exc_type="ValueError", exc_text="boom",
        frames=_engine_frames(), task_id=task,
        attempt_id="a1", project_key=project)


async def _store_and_process(db: FakeDb, incident: dict[str, Any],
                             **kwargs: Any) -> dict[str, Any]:
    """Mirror capture-then-notify: the incident is stored before triage."""
    try:
        await db.incidents.insert_one(dict(incident))
    except DuplicateKeyError:
        pass
    return await worker.process_incident(db, incident, **kwargs)


class WorkerTests(unittest.TestCase):
    def test_same_exception_two_projects_one_engine_problem(self) -> None:
        db = FakeDb()
        moment = _utc(2026, 10, 29, 12, 0, 0)
        outcomes = [asyncio.run(_store_and_process(
            db, _exception_incident(task, project, now=moment)))
            for task, project in (("t1", "proj-a"), ("t2", "proj-a"),
                                  ("t3", "proj-a"), ("t4", "proj-b"),
                                  ("t5", "proj-b"))]
        self.assertEqual(len(db.problems.docs), 1)
        problem = next(iter(db.problems.docs.values()))
        self.assertEqual(problem["scope"], "engine")
        self.assertEqual(problem["counts"]["occurrences"], 5)
        self.assertEqual(problem["counts"]["distinct_tickets"], 5)
        self.assertEqual(problem["counts"]["distinct_projects"], 2)
        self.assertNotIn("context", problem)
        self.assertNotIn("business_ref", problem)
        self.assertNotIn("task_id", problem)
        self.assertTrue(outcomes[-1]["diagnosable"])
        self.assertEqual(outcomes[-1]["reason"], "engine_normal_threshold")

    def test_environment_never_reaches_diagnosis(self) -> None:
        db = FakeDb()
        moment = _utc(2026, 10, 29, 12, 0, 0)
        for index in range(4):
            incident = build_incident_doc(
                kind="unclean_exit", component="core_process", now=moment,
                service_result="oom killed exit 137", task_id=f"t{index}",
                attempt_id="a1", project_key="proj-a")
            self.assertEqual(incident["attribution"], "environment")
            outcome = asyncio.run(_store_and_process(db, incident))
        self.assertEqual(len(db.problems.docs), 1)
        problem = next(iter(db.problems.docs.values()))
        self.assertEqual(problem["status"], ProblemStatus.NO_ACTION.value)
        self.assertFalse(problem["triage"]["eligible"])
        self.assertFalse(outcome["diagnosable"])

    def test_release_storm_groups_members(self) -> None:
        db = FakeDb()
        moment = _utc(2026, 10, 29, 12, 0, 0)
        release = {"sha": "abc123", "dirty": False}
        for index in range(3):
            incident = build_incident_doc(
                kind="invariant_violation", component="gates",
                now=moment + timedelta(minutes=index),
                invariant_id=f"INV-00{index + 1}", task_id=f"t{index}",
                attempt_id="a1", project_key="proj-a", release=release)
            asyncio.run(_store_and_process(db, incident))
        storm = db.problems.docs.get("release_regression:abc123")
        self.assertIsNotNone(storm)
        assert storm is not None
        self.assertEqual(storm["severity_max"], "critical")
        self.assertEqual(len(storm["members"]), 3)
        for member_id in storm["members"]:
            member = db.problems.docs[member_id]
            self.assertEqual(member["links"]["grouped_under"],
                             "release_regression:abc123")
            self.assertFalse(member["triage"]["eligible"])

    def test_reliability_component_never_self_diagnoses(self) -> None:
        db = FakeDb()
        incident = build_incident_doc(
            kind="exception", component="reliability",
            exc_type="ValueError", task_id="t1", attempt_id="a1")
        outcome = asyncio.run(_store_and_process(db, incident))
        self.assertEqual(outcome["reason"], "ignored")
        self.assertEqual(db.problems.docs, {})

    def test_fix_attempts_attach_without_new_diagnosis(self) -> None:
        db = FakeDb()
        moment = _utc(2026, 10, 29, 12, 0, 0)
        asyncio.run(_store_and_process(
            db, _exception_incident("t1", "proj-a", now=moment)))
        fix_try = _exception_incident("TASK-9", "proj-a", now=moment)
        fix_try["from_reliability_ticket"] = True
        outcome = asyncio.run(worker.process_incident(db, fix_try))
        problem = next(iter(db.problems.docs.values()))
        self.assertEqual(problem["counts"]["occurrences"], 1)
        self.assertFalse(outcome["diagnosable"])

    def test_mute_is_owner_only_with_token(self) -> None:
        db = FakeDb()
        asyncio.run(_store_and_process(
            db, _exception_incident("t1", "proj-a")))
        problem_id = next(iter(db.problems.docs))
        handle = worker.ReliabilityWorker(
            db, owner_token=lambda: "s3cret")
        denied_actor = asyncio.run(handle.mute(
            problem_id, actor="agent", token="s3cret", reason="noise",
            until=_utc(2026, 11, 5)))
        denied_token = asyncio.run(handle.mute(
            problem_id, actor="owner", token="wrong", reason="noise",
            until=_utc(2026, 11, 5)))
        self.assertFalse(denied_actor["muted"])
        self.assertFalse(denied_token["muted"])
        ok = asyncio.run(handle.mute(
            problem_id, actor="owner", token="s3cret", reason="noise",
            until=_utc(2026, 11, 5)))
        self.assertTrue(ok["muted"])
        outcome = asyncio.run(_store_and_process(
            db, _exception_incident("t2", "proj-a")))
        self.assertFalse(outcome["diagnosable"])
        self.assertEqual(outcome["reason"], "status_muted")

    def test_mute_expires_on_next_incident(self) -> None:
        db = FakeDb()
        asyncio.run(_store_and_process(
            db, _exception_incident("t1", "proj-a")))
        problem_id = next(iter(db.problems.docs))
        handle = worker.ReliabilityWorker(
            db, owner_token=lambda: "s3cret")
        asyncio.run(handle.mute(
            problem_id, actor="owner", token="s3cret", reason="noise",
            until=_utc(2026, 11, 5)))
        stored = db.problems.docs[problem_id]
        stored["mute"]["until"] = _utc(2026, 10, 20)
        outcome = asyncio.run(_store_and_process(
            db, _exception_incident("t2", "proj-b")))
        self.assertEqual(db.problems.docs[problem_id]["status"], "open")
        self.assertEqual(outcome["reason"], "engine_below_threshold")

    def test_version_change_supersedes_problem(self) -> None:
        db = FakeDb()
        incident = _exception_incident("t1", "proj-a")
        old = worker.build_problem_doc(
            scope="engine", fingerprint=incident["fingerprint"],
            fingerprint_version=0, first=incident,
            counts={"occurrences": 1, "distinct_tickets": 1,
                    "distinct_projects": 1},
            eligible=False, triage_reason="engine_below_threshold")
        asyncio.run(db.problems.insert_one(old))
        outcome = asyncio.run(_store_and_process(db, incident))
        self.assertTrue(outcome["created"])
        self.assertEqual(outcome["reason"], "engine_below_threshold")
        ids = [doc["_id"] for doc in db.problems.docs.values()]
        self.assertIn(worker.problem_id_for(
            "engine", incident["fingerprint"], 0), ids)
        self.assertIn(worker.problem_id_for(
            "engine", incident["fingerprint"], FINGERPRINT_VERSION), ids)

    def test_task_threshold_needs_five_tickets(self) -> None:
        db = FakeDb()
        moment = _utc(2026, 10, 29, 12, 0, 0)
        outcome: dict[str, Any] = {}
        for index in range(5):
            incident = build_incident_doc(
                kind="brake_exhausted", component="runner",
                now=moment + timedelta(minutes=index), brake="gate",
                phase="build", task_id=f"t{index}", attempt_id="a1",
                project_key="proj-a")
            self.assertEqual(incident["attribution"], "task")
            outcome = asyncio.run(_store_and_process(db, incident))
        self.assertEqual(len(db.problems.docs), 1)
        problem = next(iter(db.problems.docs.values()))
        self.assertEqual(problem["scope"], "project:proj-a")
        self.assertTrue(outcome["diagnosable"])
        self.assertEqual(outcome["reason"], "task_threshold")

    def test_idle_worker_makes_no_queries(self) -> None:
        async def _scenario() -> None:
            db = FakeDb()
            handle = worker.ReliabilityWorker(db)
            task = asyncio.get_running_loop().create_task(
                handle.run_forever())
            await asyncio.sleep(0.2)
            idle_queries = (db.problems.queries + db.incidents.queries)
            handle.notify(_exception_incident("t1", "proj-a"))
            for _ in range(200):
                if handle.processed >= 1:
                    break
                await asyncio.sleep(0.01)
            await handle.stop()
            await task
            self.assertEqual(idle_queries, 0)
            self.assertEqual(handle.processed, 1)

        asyncio.run(_scenario())

    def test_reconcile_covers_spool_unprocessed_and_last_exit(self) -> None:
        async def _scenario(tmp: Path) -> None:
            from reliability.capture import spool_path

            db = FakeDb()
            moment = _utc(2026, 10, 29, 12, 0, 0)
            spooled = _exception_incident("spool-1", "proj-a", now=moment)
            path = spool_path(tmp)
            path.parent.mkdir(parents=True, exist_ok=True)
            import json

            path.write_text(json.dumps(spooled, default=str) + "\n",
                            encoding="utf-8")
            queued = _exception_incident("t2", "proj-a", now=moment)
            await db.incidents.insert_one(queued)
            (tmp / "last-exit.json").write_text(
                '{"service_result": "signal: killed", "exit_code": "137",'
                ' "exit_status": "", "at": "2026-10-29T11:00:00Z"}',
                encoding="utf-8")
            summary = await worker.reconcile_on_boot(
                db, runtime_dir=tmp, release={"sha": "abc"})
            self.assertEqual(summary["spool"]["replayed"], 1)
            self.assertEqual(summary["unprocessed"], 3)
            self.assertIsNotNone(summary["unclean_exit"])
            self.assertEqual(len(db.problems.docs), 2)

        with tempfile.TemporaryDirectory() as raw:
            asyncio.run(_scenario(Path(raw)))


if __name__ == "__main__":
    unittest.main()
