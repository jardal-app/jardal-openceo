"""Reliability escaped defects: revert, linked bug and reopen (§5.5).

Targeted unit tests with git and ERP fakes, an injected clock and temp
runtime dirs. No network, no CLI, no LLM (epic TASK-2026-00365, ticket
TASK-2026-00382).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import escapes  # noqa: E402
from reliability.fingerprint import fingerprint_escape  # noqa: E402


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class DuplicateKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("duplicate")
        self.code = 11000


class FakeCollection:
    """Async motor-like collection backed by a dict."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        if doc["_id"] in self.docs:
            raise DuplicateKeyError()
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def count_documents(self, filt: dict[str, Any]) -> int:
        since = (filt.get("created_at") or {}).get("$gte")
        want_fp = filt.get("fingerprint")
        total = 0
        for doc in self.docs.values():
            if want_fp is not None and doc.get("fingerprint") != want_fp:
                continue
            if since is not None and doc.get("created_at") < since:
                continue
            total += 1
        return total


class FakeGit:
    """Commit-message and ticket-completion fakes for revert checks."""

    def __init__(self, *, messages: dict[str, str],
                 completed: dict[str, bool]) -> None:
        self.messages = dict(messages)
        self.completed = dict(completed)

    def message_of(self, sha: str) -> str:
        return self.messages.get(sha, "")

    def is_completed(self, ref: str) -> bool:
        return bool(self.completed.get(ref, False))


def _tmpdir(test: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def _task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {"task_id": "exec-1", "coding_tool": "tier_s"}
    task.update(overrides)
    return task


class ParseRevertTests(unittest.TestCase):
    def test_standard_message_yields_lowercase_sha(self) -> None:
        sha = escapes.parse_revert_sha(
            "This reverts commit abc123DEF456.\n\nReason: bad fix.")
        self.assertEqual(sha, "abc123def456")

    def test_non_revert_is_none(self) -> None:
        self.assertIsNone(escapes.parse_revert_sha("fix: typo"))
        self.assertIsNone(escapes.parse_revert_sha(""))
        self.assertIsNone(escapes.parse_revert_sha(None))


class ClassifyRevertTests(unittest.TestCase):
    def test_revert_of_completed_other_ticket_is_escape(self) -> None:
        verdict = escapes.classify_revert(
            "This reverts commit deadbeef.", reverted_ref="TASK-2026-00300",
            reverted_completed=True, integrating_ref="TASK-2026-00301")
        self.assertTrue(verdict["escape"])
        self.assertEqual(verdict["sha"], "deadbeef")

    def test_revert_of_open_ticket_is_nothing(self) -> None:
        verdict = escapes.classify_revert(
            "This reverts commit deadbeef.", reverted_ref="TASK-2026-00300",
            reverted_completed=False, integrating_ref="TASK-2026-00301")
        self.assertFalse(verdict["escape"])
        self.assertEqual(verdict["reason"], "reverted_not_completed")

    def test_unknown_reverted_ticket_is_nothing(self) -> None:
        verdict = escapes.classify_revert(
            "This reverts commit deadbeef.", reverted_ref=None,
            reverted_completed=True)
        self.assertFalse(verdict["escape"])
        self.assertEqual(verdict["reason"], "reverted_unknown")

    def test_same_ticket_revert_is_nothing(self) -> None:
        verdict = escapes.classify_revert(
            "This reverts commit deadbeef.", reverted_ref="TASK-2026-00300",
            reverted_completed=True, integrating_ref="TASK-2026-00300")
        self.assertFalse(verdict["escape"])
        self.assertEqual(verdict["reason"], "same_ticket")


class RecordRevertTests(unittest.TestCase):
    def test_revert_of_completed_stores_escape(self) -> None:
        git = FakeGit(
            messages={"r1": "This reverts commit deadbeef."},
            completed={"TASK-2026-00300": True})
        coll = FakeCollection()
        runtime = _tmpdir(self)
        outcome = asyncio.run(escapes.record_revert_escape(
            coll, revert_message=git.message_of("r1"),
            reverted_ref="TASK-2026-00300",
            reverted_completed=git.is_completed("TASK-2026-00300"),
            integrating_ref="TASK-2026-00301",
            task=_task(), business_ref="TASK-2026-00301",
            project_key="jardal-openceo",
            runtime_dir=runtime,
            now=_utc(2026, 10, 31)))
        self.assertTrue(outcome["recorded"])
        doc = coll.docs[str(outcome["incident_id"])]
        self.assertEqual(doc["kind"], "escape")
        self.assertEqual(doc["attribution"], "unknown")
        self.assertEqual(doc["attribution_rule"], "A10")
        self.assertEqual(
            doc["fingerprint"],
            fingerprint_escape("revert", "tier_s", "standard"))

    def test_revert_is_idempotent_per_sha(self) -> None:
        coll = FakeCollection()
        runtime = _tmpdir(self)
        kwargs: dict[str, Any] = {
            "revert_message": "This reverts commit deadbeef.",
            "reverted_ref": "TASK-2026-00300",
            "reverted_completed": True,
            "integrating_ref": "TASK-2026-00301",
            "task": _task(), "business_ref": "TASK-2026-00301",
            "project_key": "jardal-openceo",
            "runtime_dir": runtime, "now": _utc(2026, 10, 31),
        }
        first = asyncio.run(escapes.record_revert_escape(coll, **kwargs))
        second = asyncio.run(escapes.record_revert_escape(coll, **kwargs))
        self.assertTrue(first["recorded"])
        self.assertEqual(first["incident_id"], second["incident_id"])
        self.assertEqual(len(coll.docs), 1)

    def test_revert_of_open_ticket_stores_nothing(self) -> None:
        coll = FakeCollection()
        outcome = asyncio.run(escapes.record_revert_escape(
            coll, revert_message="This reverts commit deadbeef.",
            reverted_ref="TASK-2026-00300", reverted_completed=False,
            task=_task(), business_ref="TASK-2026-00301",
            runtime_dir=_tmpdir(self), now=_utc(2026, 10, 31)))
        self.assertFalse(outcome["recorded"])
        self.assertEqual(coll.docs, {})


class LinkedBugTests(unittest.TestCase):
    def test_linked_bug_stays_not_covered(self) -> None:
        coverage = escapes.linked_bug_coverage()
        self.assertFalse(coverage["covered"])
        self.assertIn("caused_by", coverage["reason"])
        verdict = escapes.classify_linked_bug(ticket={"task_id": "exec-9"})
        self.assertFalse(verdict["escape"])
        self.assertEqual(verdict["reason"], "not_covered")


class ReopenTests(unittest.TestCase):
    def test_completed_to_open_and_working_are_escapes(self) -> None:
        self.assertTrue(escapes.is_reopen_escape("Completed", "Open"))
        self.assertTrue(escapes.is_reopen_escape("Completed", "Working"))
        self.assertTrue(escapes.is_reopen_escape("completed", "working"))

    def test_other_transitions_are_nothing(self) -> None:
        self.assertFalse(escapes.is_reopen_escape("Open", "Working"))
        self.assertFalse(escapes.is_reopen_escape("Working", "Completed"))
        self.assertFalse(escapes.is_reopen_escape("Completed", "Completed"))
        self.assertFalse(escapes.is_reopen_escape("Completed", "Cancelled"))
        self.assertFalse(escapes.is_reopen_escape(None, "Working"))

    def test_erp_reopen_stores_escape_with_actor(self) -> None:
        coll = FakeCollection()
        runtime = _tmpdir(self)
        incident_id = asyncio.run(escapes.record_reopen_escape(
            coll, previous_status="Completed", new_status="Working",
            task=_task(), business_ref="TASK-2026-00310",
            project_key="jardal-openceo", actor="erp:owner-1",
            event_id="evt-1", runtime_dir=runtime,
            now=_utc(2026, 10, 31)))
        self.assertIsNotNone(incident_id)
        doc = coll.docs[str(incident_id)]
        self.assertEqual(doc["kind"], "escape")
        self.assertEqual(doc["attribution"], "unknown")
        self.assertEqual(
            doc["fingerprint"],
            fingerprint_escape("reopen", "tier_s", "standard"))
        self.assertIn("erp:owner-1", doc["context"]["message"])
        self.assertEqual(doc["context"]["evidence_refs"], ["evt-1"])

    def test_semantic_and_erp_echo_share_one_id(self) -> None:
        task = _task()
        first = escapes.reopen_incident_id(
            "exec-1", "Completed", "Working")
        second = escapes.reopen_incident_id("exec-1", "completed", "working")
        self.assertEqual(first, second)
        coll = FakeCollection()
        runtime = _tmpdir(self)
        one = asyncio.run(escapes.record_reopen_escape(
            coll, previous_status="Completed", new_status="Working",
            task=task, business_ref="TASK-2026-00310",
            runtime_dir=runtime, now=_utc(2026, 10, 31)))
        two = asyncio.run(escapes.record_reopen_escape(
            coll, previous_status="completed", new_status="working",
            task=task, business_ref="TASK-2026-00310",
            event_id="evt-2", runtime_dir=runtime,
            now=_utc(2026, 10, 31)))
        self.assertEqual(one, two)
        self.assertEqual(len(coll.docs), 1)

    def test_reopen_of_non_completed_stores_nothing(self) -> None:
        coll = FakeCollection()
        incident_id = asyncio.run(escapes.record_reopen_escape(
            coll, previous_status="Working", new_status="Open",
            task=_task(), business_ref="TASK-2026-00310",
            runtime_dir=_tmpdir(self), now=_utc(2026, 10, 31)))
        self.assertIsNone(incident_id)
        self.assertEqual(coll.docs, {})


class NoStoreTests(unittest.TestCase):
    def test_hooks_skip_repos_without_incident_store(self) -> None:
        class BareRepo:
            pass

        runtime = _tmpdir(self)
        reopen = asyncio.run(escapes.record_reopen_escape(
            BareRepo(), previous_status="Completed", new_status="Working",
            task=_task(), business_ref="TASK-2026-00310",
            runtime_dir=runtime, now=_utc(2026, 10, 31)))
        self.assertIsNone(reopen)
        revert = asyncio.run(escapes.record_revert_escape(
            BareRepo(), revert_message="This reverts commit deadbeef.",
            reverted_ref="TASK-2026-00300", reverted_completed=True,
            task=_task(), business_ref="TASK-2026-00301",
            runtime_dir=runtime, now=_utc(2026, 10, 31)))
        self.assertFalse(revert["recorded"])
        self.assertEqual(revert["reason"], "no_store")
        self.assertEqual(list(runtime.iterdir()), [])


class TierPolicyTests(unittest.TestCase):
    def test_tier_and_policy_defaults(self) -> None:
        self.assertEqual(escapes.tier_of(_task()), "tier_s")
        self.assertEqual(escapes.tier_of({}), "unknown")
        self.assertEqual(escapes.review_policy_of(_task()), "standard")
        self.assertEqual(
            escapes.review_policy_of(_task(qa_review_required=True)),
            "qa_required")


if __name__ == "__main__":
    unittest.main()
