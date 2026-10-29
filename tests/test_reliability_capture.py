"""Reliability capture: record_incident, spool, avalanche, unclean_exit.

Targeted unit tests with Mongo fakes, an injected clock and temp runtime
dirs. No network, no CLI, no LLM (epic TASK-2026-00365, ticket TASK-2026-00372).
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from reliability import capture  # noqa: E402
from reliability.capture import (  # noqa: E402
    build_incident_doc,
    build_incident_id,
    ensure_reliability_indexes,
    record_incident,
    record_incident_sync,
    record_unclean_exit,
    replay_spool,
    spool_status,
    transient_recovered,
)
from reliability.supervisor import LoopSupervisor, backoff_for  # noqa: E402


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class DuplicateKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("duplicate")
        self.code = 11000


class FakeCollection:
    """Async motor-like collection backed by a dict."""

    def __init__(self, *, fail: Exception | None = None,
                 slow: float = 0.0) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.fail = fail
        self.slow = slow
        self.inserts = 0
        self.indexes: list[Any] = []

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        self.inserts += 1
        if self.slow:
            await asyncio.sleep(self.slow)
        if self.fail is not None:
            raise self.fail
        if doc["_id"] in self.docs:
            raise DuplicateKeyError()
        self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def count_documents(self, filt: dict[str, Any]) -> int:
        if isinstance(self.fail, Exception) and self.fail is not None:
            raise self.fail
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

    async def create_index(self, keys: Any, **kwargs: Any) -> str:
        self.indexes.append((keys, kwargs))
        return "idx"


class SyncCollection:
    """Blocking collection for the sync variant and thread-safety tests."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.inserts = 0

    def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        with self.lock:
            self.inserts += 1
            if doc["_id"] in self.docs:
                raise DuplicateKeyError()
            self.docs[doc["_id"]] = dict(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    def count_documents(self, filt: dict[str, Any]) -> int:
        want_fp = filt.get("fingerprint")
        with self.lock:
            return sum(1 for doc in self.docs.values()
                       if want_fp is None or doc.get("fingerprint") == want_fp)


def _tmpdir(test: unittest.TestCase) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    return Path(tmp.name)


class CaptureTests(unittest.TestCase):
    def test_happy_path_sanitizes_and_notifies(self) -> None:
        coll = FakeCollection()
        runtime = _tmpdir(self)
        seen: list[dict[str, Any]] = []
        doc_id = asyncio.run(record_incident(
            coll, kind="exception", component="dispatcher",
            exc_type="ValueError", exc_text="boom sk-live-AAAA9999bbbb",
            frames=[{"module": "m", "function": "f",
                     "file": "openceo-core/x.py", "line": "3"}],
            task_id="TASK-2026-00372", attempt_id="a-1",
            reason_code="rc-1", message="hello user@example.com",
            runtime_dir=runtime, notify=seen.append,
            now=_utc(2026, 10, 29)))
        self.assertIsNotNone(doc_id)
        doc = coll.docs[doc_id]
        self.assertEqual(doc["kind"], "exception")
        self.assertEqual(doc["processed"], False)
        self.assertEqual(doc["schema_version"], 1)
        self.assertIn("problem_key", doc)
        self.assertNotIn("sk-live", doc["context"]["message"])
        self.assertNotIn("user@example.com", doc["context"]["message"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(
            doc["expire_at"] - doc["created_at"], timedelta(days=90))

    def test_deterministic_id_with_natural_identity(self) -> None:
        first = build_incident_id(
            "exception", task_id="T", attempt_id="a",
            discriminator="rc")
        second = build_incident_id(
            "exception", task_id="T", attempt_id="a",
            discriminator="rc")
        self.assertEqual(first, second)
        other = build_incident_id(
            "exception", task_id="T", attempt_id="b",
            discriminator="rc")
        self.assertNotEqual(first, other)
        bare = build_incident_id("exception")
        self.assertTrue(bare)

    def test_record_is_idempotent_on_duplicate(self) -> None:
        coll = FakeCollection()
        runtime = _tmpdir(self)
        kwargs: dict[str, Any] = {
            "kind": "exception", "component": "dispatcher",
            "task_id": "T", "attempt_id": "a", "reason_code": "rc",
            "runtime_dir": runtime, "now": _utc(2026, 10, 29),
        }
        first = asyncio.run(record_incident(coll, **kwargs))
        # Simulate a crash before processed: same natural identity replays.
        second = asyncio.run(record_incident(coll, **kwargs))
        self.assertEqual(first, second)
        self.assertEqual(len(coll.docs), 1)

    def test_mongo_down_spools_and_replays_without_duplicates(self) -> None:
        runtime = _tmpdir(self)
        down: Exception = RuntimeError("mongo down")
        failing = FakeCollection(fail=down)
        doc_id = asyncio.run(record_incident(
            failing, kind="exception", component="runner",
            reason_code="rc-spool", runtime_dir=runtime,
            now=_utc(2026, 10, 29)))
        self.assertIsNotNone(doc_id)
        status = spool_status(runtime)
        self.assertEqual(status["pending"], 1)

        healthy = FakeCollection()
        outcome = asyncio.run(replay_spool(healthy, runtime_dir=runtime))
        self.assertEqual(outcome["replayed"], 1)
        self.assertEqual(outcome["pending"], 0)
        # Replaying an already-present id is a no-op, never a double.
        outcome = asyncio.run(replay_spool(healthy, runtime_dir=runtime))
        self.assertEqual(outcome, {"replayed": 0, "pending": 0, "failed": 0})

    def test_slow_mongo_hits_timeout_and_spools(self) -> None:
        runtime = _tmpdir(self)
        coll = FakeCollection(slow=5.0)
        doc_id = asyncio.run(record_incident(
            coll, kind="exception", component="runner",
            reason_code="rc-slow", runtime_dir=runtime,
            now=_utc(2026, 10, 29)))
        self.assertIsNotNone(doc_id)
        self.assertEqual(spool_status(runtime)["pending"], 1)

    def test_avalanche_caps_at_20_per_fingerprint_hour(self) -> None:
        coll = FakeCollection()
        runtime = _tmpdir(self)
        capture.reset_counters()
        stored = 0
        for _ in range(1000):
            doc_id = asyncio.run(record_incident(
                coll, kind="exception", component="runner",
                exc_type="ValueError", exc_text="same",
                frames=[], reason_code="same-rc",
                runtime_dir=runtime, now=_utc(2026, 10, 29, 10)))
            if doc_id is not None:
                stored += 1
        self.assertEqual(stored, 20)
        self.assertEqual(len(coll.docs), 20)
        fingerprint = next(iter(coll.docs.values()))["fingerprint"]
        self.assertEqual(
            capture.suppressed_counts().get(fingerprint), 1000 - 20)

    def test_full_spool_discards_and_counts(self) -> None:
        coll = FakeCollection(fail=RuntimeError("down"))
        runtime = _tmpdir(self)
        capture.reset_counters()
        doc_id = asyncio.run(record_incident(
            coll, kind="exception", component="runner",
            reason_code="rc-full", runtime_dir=runtime, spool_max_bytes=10,
            now=_utc(2026, 10, 29)))
        self.assertIsNotNone(doc_id)
        self.assertEqual(capture.spool_dropped(), 1)
        self.assertEqual(spool_status(runtime)["pending"], 0)

    def test_sync_variant_is_thread_safe(self) -> None:
        coll = SyncCollection()
        runtime = _tmpdir(self)
        results: list[str | None] = []
        lock = threading.Lock()

        def _work(n: int) -> None:
            doc_id = record_incident_sync(
                coll, kind="exception", component="dispatcher",
                task_id="TASK-2026-00372", attempt_id=f"a-{n}",
                reason_code="rc-sync", runtime_dir=runtime,
                now=_utc(2026, 10, 29))
            with lock:
                results.append(doc_id)

        threads = [threading.Thread(target=_work, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(len(coll.docs), 8)
        self.assertTrue(all(results))

    def test_never_throws_on_garbage_source(self) -> None:
        runtime = _tmpdir(self)
        self.assertIsNotNone(asyncio.run(record_incident(
            object(), kind="exception", component="runner",
            reason_code="rc", runtime_dir=runtime, now=_utc(2026, 10, 29))))
        self.assertEqual(spool_status(runtime)["pending"], 1)

    def test_doc_carries_section_13_1_shape(self) -> None:
        doc = build_incident_doc(
            kind="terminal_failure", component="tier_s",
            reason_code="pytest failure", now=_utc(2026, 10, 29),
            release={"sha": "abc", "dirty": False})
        for field in ("_id", "kind", "severity", "attribution",
                      "attribution_rule", "component", "phase", "release",
                      "fingerprint", "fingerprint_version", "problem_key",
                      "engine", "context", "shadow", "late",
                      "from_reliability_ticket", "glitchtip_event_id",
                      "processed", "created_at", "expire_at"):
            self.assertIn(field, doc)

    def test_ensure_indexes_covers_section_13_1(self) -> None:
        coll = FakeCollection()
        asyncio.run(ensure_reliability_indexes(coll))
        kinds = [keys for keys, _ in coll.indexes]
        self.assertIn([("problem_key", 1), ("created_at", -1)], kinds)
        self.assertIn([("processed", 1)], kinds)
        self.assertIn([("expire_at", 1)], kinds)
        partial = [kw for _, kw in coll.indexes if "partialFilterExpression" in kw]
        self.assertEqual(partial, [{"partialFilterExpression":
                                    {"processed": False}}])
        ttl = [kw for _, kw in coll.indexes if "expireAfterSeconds" in kw]
        self.assertEqual(ttl, [{"expireAfterSeconds": 0}])

    def test_unclean_exit_idempotent_by_at_and_oom_is_environment(self) -> None:
        runtime = _tmpdir(self)
        (runtime / "last-exit.json").write_text(json.dumps({
            "at": "2026-10-29T05:00:00Z", "service_result": "signal",
            "exit_code": "killed", "exit_status": "SIGKILL",
        }), encoding="utf-8")
        coll = FakeCollection()
        first = asyncio.run(record_unclean_exit(
            coll, runtime_dir=runtime, release={"sha": "x", "dirty": False},
            now=_utc(2026, 10, 29, 6)))
        second = asyncio.run(record_unclean_exit(
            coll, runtime_dir=runtime, release={"sha": "x", "dirty": False},
            now=_utc(2026, 10, 29, 6)))
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(len(coll.docs), 1)
        self.assertEqual(coll.docs[first]["attribution"], "environment")

    def test_unclean_exit_clean_result_records_nothing(self) -> None:
        runtime = _tmpdir(self)
        (runtime / "last-exit.json").write_text(json.dumps({
            "at": "2026-10-29T05:00:00Z", "service_result": "success",
            "exit_code": "exited", "exit_status": "0",
        }), encoding="utf-8")
        coll = FakeCollection()
        self.assertIsNone(asyncio.run(
            record_unclean_exit(coll, runtime_dir=runtime)))
        self.assertEqual(coll.docs, {})

    def test_transient_recovered_goes_to_warning(self) -> None:
        self.assertTrue(transient_recovered("connection reset by peer"))
        self.assertFalse(transient_recovered("ValueError: bad ticket shape"))

    def test_supervisor_backoff_grows_and_resets(self) -> None:
        self.assertEqual(backoff_for(0), 1.0)
        self.assertEqual(backoff_for(1), 2.0)
        self.assertEqual(backoff_for(100), 60.0)
        supervisor = LoopSupervisor(name="t", component="dispatcher")
        asyncio.run(supervisor.failure(ValueError("x"), source=object()))
        self.assertEqual(supervisor.streak, 1)
        supervisor.success()
        self.assertEqual(supervisor.streak, 0)

    def test_supervisor_records_exception_and_restarts(self) -> None:
        coll = FakeCollection()
        supervisor = LoopSupervisor(name="t", component="dispatcher")

        async def _boom() -> None:
            raise RuntimeError("loop exploded")

        async def _main() -> None:
            try:
                await _boom()
            except RuntimeError as exc:
                wait = await supervisor.failure(
                    exc, source=coll, phase="cycle")
                self.assertGreaterEqual(wait, 1.0)

        asyncio.run(_main())
        self.assertEqual(len(coll.docs), 1)
        self.assertEqual(supervisor.streak, 1)

    def test_supervisor_skips_capture_without_collection(self) -> None:
        supervisor = LoopSupervisor(name="t", component="ceo_loop")

        async def _main() -> float:
            try:
                raise RuntimeError("no db here")
            except RuntimeError as exc:
                return await supervisor.failure(exc, source=object())

        wait = asyncio.run(_main())
        self.assertGreaterEqual(wait, 1.0)

    def test_replay_preserves_datetime_types(self) -> None:
        runtime = _tmpdir(self)
        failing = FakeCollection(fail=RuntimeError("mongo down"))
        doc_id = asyncio.run(record_incident(
            failing, kind="exception", component="runner",
            reason_code="rc-dates", runtime_dir=runtime,
            now=_utc(2026, 10, 29)))
        self.assertIsNotNone(doc_id)
        healthy = FakeCollection()
        outcome = asyncio.run(replay_spool(healthy, runtime_dir=runtime))
        self.assertEqual(outcome, {"replayed": 1, "pending": 0, "failed": 0})
        doc = healthy.docs[doc_id]
        self.assertIsInstance(doc["created_at"], datetime)
        self.assertIsInstance(doc["expire_at"], datetime)
        self.assertEqual(doc["expire_at"] - doc["created_at"],
                         timedelta(days=90))

    def test_replay_keeps_concurrent_appends(self) -> None:
        runtime = _tmpdir(self)
        failing = FakeCollection(fail=RuntimeError("mongo down"))
        first = asyncio.run(record_incident(
            failing, kind="exception", component="runner",
            reason_code="rc-first", runtime_dir=runtime,
            now=_utc(2026, 10, 29)))
        self.assertIsNotNone(first)
        slow = FakeCollection(slow=0.4)

        def _append_while_replaying() -> str | None:
            import time
            time.sleep(0.1)
            return asyncio.run(record_incident(
                failing, kind="exception", component="runner",
                reason_code="rc-late", runtime_dir=runtime,
                now=_utc(2026, 10, 29)))

        appender = threading.Thread(target=_append_while_replaying)
        appender.start()
        outcome = asyncio.run(replay_spool(slow, runtime_dir=runtime))
        appender.join()
        self.assertEqual(outcome["replayed"], 1)
        self.assertEqual(outcome["failed"], 0)
        # The late append is never lost: inserted or still spooled.
        late_pending = spool_status(runtime)["pending"]
        late_ids = [doc.get("_id") for doc in slow.docs.values()]
        from reliability.capture import _read_spool
        late_spooled = [doc.get("_id") for doc in _read_spool(runtime)]
        self.assertLessEqual(late_pending, 1)
        self.assertEqual(len(late_ids) + late_pending, 2)
        self.assertNotIn(first, late_spooled)

    def test_sync_replays_spool_after_good_write(self) -> None:
        runtime = _tmpdir(self)
        spooled = record_incident_sync(
            object(), kind="exception", component="runner",
            reason_code="rc-sync-spool", runtime_dir=runtime,
            now=_utc(2026, 10, 29))
        self.assertIsNotNone(spooled)
        self.assertEqual(spool_status(runtime)["pending"], 1)
        coll = SyncCollection()
        stored = record_incident_sync(
            coll, kind="exception", component="runner",
            reason_code="rc-sync-good", runtime_dir=runtime,
            now=_utc(2026, 10, 29))
        self.assertIsNotNone(stored)
        self.assertEqual(spool_status(runtime)["pending"], 0)
        self.assertEqual(len(coll.docs), 2)
        doc = coll.docs[spooled]
        self.assertIsInstance(doc["created_at"], datetime)

    def test_supervisor_passes_frames_for_engine_attribution(self) -> None:
        coll = FakeCollection()
        supervisor = LoopSupervisor(name="t", component="dispatcher")

        async def _boom() -> None:
            raise RuntimeError("loop exploded with frames")

        async def _main() -> None:
            try:
                await _boom()
            except RuntimeError as exc:
                await supervisor.failure(exc, source=coll, phase="cycle")

        asyncio.run(_main())
        self.assertEqual(len(coll.docs), 1)
        doc = next(iter(coll.docs.values()))
        # Frames travel from the traceback to the stored doc (empty before).
        frames = doc["engine"].get("frames")
        self.assertIsInstance(frames, list)
        self.assertEqual(len(frames), 2)

    def test_in_app_frames_classify_as_engine_high(self) -> None:
        coll = FakeCollection()
        runtime = _tmpdir(self)
        doc_id = asyncio.run(record_incident(
            coll, kind="exception", component="dispatcher",
            exc_type="RuntimeError", exc_text="cycle failed",
            frames=[{"module": "?", "function": "dispatch_cycle",
                     "file": "openceo-core/dispatcher.py", "line": "212"}],
            phase="dispatch_cycle", runtime_dir=runtime,
            now=_utc(2026, 10, 29)))
        self.assertIsNotNone(doc_id)
        doc = coll.docs[doc_id]
        self.assertEqual(doc["attribution"], "engine")
        self.assertEqual(doc["severity"], "high")


if __name__ == "__main__":
    unittest.main()
