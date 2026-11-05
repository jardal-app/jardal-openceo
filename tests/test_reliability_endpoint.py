"""Reliability runner channel: socket, spool and sandbox fence.

Targeted unit tests with a temp socket, fake stores and temp dirs. No
network, no CLI, no LLM (epic TASK-2026-00365, ticket TASK-2026-00379).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
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

from reliability import endpoint  # noqa: E402
from reliability.endpoint import (  # noqa: E402
    RateLimiter,
    ingest_runner_spool,
    post_event,
    spool_event,
    start_endpoint,
    stop_endpoint,
    validate_event,
)


class DuplicateKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("duplicate")
        self.code = 11000


class FakeIncidents:
    """Async motor-like collection backed by a dict."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        if doc["_id"] in self.docs:
            raise DuplicateKeyError()
        self.docs[doc["_id"]] = doc
        return SimpleNamespace(inserted_id=doc["_id"])

    async def count_documents(self, filt: dict[str, Any]) -> int:
        return 0


def _raw_post(runtime: Path, body: bytes, path: str = "/events") -> int:
    import socket

    request = (f"POST {path} HTTP/1.1\r\nHost: x\r\n"
               "Content-Type: application/json\r\n"
               f"Content-Length: {len(body)}\r\n"
               "Connection: close\r\n\r\n").encode() + body
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5.0)
    try:
        sock.connect(str(runtime / endpoint.SOCKET_FILENAME))
        sock.sendall(request)
        head = b""
        while b"\r\n" not in head:
            chunk = sock.recv(256)
            if not chunk:
                break
            head += chunk
        return int(head.decode("latin-1").split(" ")[1])
    finally:
        sock.close()


class ValidationTests(unittest.TestCase):
    def test_rejects_unknown_type(self) -> None:
        with self.assertRaises(ValueError):
            validate_event({"type": "nope", "component": "runner"})

    def test_rejects_unknown_kind_and_component(self) -> None:
        with self.assertRaises(ValueError):
            validate_event({"type": "incident", "kind": "nope",
                            "component": "runner"})
        with self.assertRaises(ValueError):
            validate_event({"type": "exposure", "component": "nope"})

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ValueError):
            validate_event({"type": "exposure", "component": "runner",
                            "authorize": True})

    def test_accepts_incident_and_exposure(self) -> None:
        incident = validate_event({"type": "incident", "kind": "stall",
                                   "component": "runner"})
        self.assertEqual(incident["kind"], "stall")
        self.assertTrue(incident["event_id"])
        exposure = validate_event({"type": "exposure",
                                   "component": "runner"})
        self.assertEqual(exposure["type"], "exposure")


class SocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        self.incidents = FakeIncidents()
        self._loop: Any = None
        self._thread: Any = None

    def tearDown(self) -> None:
        self._close()
        self._tmp.cleanup()

    def _serve(self, **kwargs: Any) -> None:
        import threading

        loop = asyncio.new_event_loop()

        async def _start() -> None:
            endpoint._SERVER = await start_endpoint(
                self.incidents, runtime_dir=self.runtime, **kwargs)

        loop.run_until_complete(_start())
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self._loop = loop
        self._thread = thread

    def _close(self) -> None:
        if self._loop is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    stop_endpoint(), self._loop)
                future.result(timeout=10)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=10)
                self._loop.close()
                self._loop = None

    def test_socket_mode_is_0660(self) -> None:
        self._serve()
        try:
            mode = stat.S_IMODE(os.stat(
                self.runtime / endpoint.SOCKET_FILENAME).st_mode)
            self.assertEqual(mode, 0o660)
        finally:
            self._close()

    def test_invalid_payload_rejected(self) -> None:
        self._serve()
        try:
            self.assertEqual(_raw_post(self.runtime, b"{}"), 400)
            self.assertEqual(_raw_post(self.runtime, b"not json"), 400)
            self.assertEqual(_raw_post(self.runtime, b"[]"), 400)
            self.assertEqual(_raw_post(self.runtime, b"{}",
                                       path="/nope"), 404)
        finally:
            self._close()

    def test_oversize_payload_rejected(self) -> None:
        self._serve(max_bytes=64)
        try:
            self.assertEqual(_raw_post(self.runtime, b"x" * 65), 413)
        finally:
            self._close()

    def test_unknown_fields_rejected_over_socket(self) -> None:
        self._serve()
        try:
            body = json.dumps({"type": "exposure", "component": "runner",
                               "authorize": True}).encode()
            self.assertEqual(_raw_post(self.runtime, body), 400)
            self.assertEqual(len(self.incidents.docs), 0)
        finally:
            self._close()

    def test_malformed_request_line_rejected(self) -> None:
        self._serve()
        try:
            self.assertEqual(_raw_post(self.runtime, b"{}", path="/"), 404)
        finally:
            self._close()

    def test_valid_incident_stored(self) -> None:
        self._serve()
        try:
            event = {"type": "incident", "kind": "stall",
                     "component": "runner", "task_id": "TASK-2026-00379",
                     "attempt_id": "TASK-2026-00379-s1", "phase": "work",
                     "guard_rule": "G1", "result_signature": "work:X"}
            self.assertTrue(post_event(event, runtime_dir=self.runtime))
            self.assertEqual(len(self.incidents.docs), 1)
            doc = next(iter(self.incidents.docs.values()))
            self.assertEqual(doc["kind"], "stall")
            self.assertFalse(doc["late"])
        finally:
            self._close()

    def test_valid_exposure_accepted_and_discarded(self) -> None:
        self._serve()
        try:
            self.assertTrue(post_event(
                {"type": "exposure", "component": "runner"},
                runtime_dir=self.runtime))
            self.assertEqual(len(self.incidents.docs), 0)
            self.assertFalse(
                (self.runtime / endpoint.RUNNER_SPOOL_FILENAME).exists())
        finally:
            self._close()

    def test_rate_limit_rejects_excess(self) -> None:
        self._serve(limiter=RateLimiter(max_per_minute=2,
                                        now=self._clock()))
        try:
            body = json.dumps({"type": "exposure",
                               "component": "runner"}).encode()
            self.assertEqual(_raw_post(self.runtime, body), 202)
            self.assertEqual(_raw_post(self.runtime, body), 202)
            self.assertEqual(_raw_post(self.runtime, body), 429)
        finally:
            self._close()

    def _clock(self):  # frozen clock: all hits inside one window
        return lambda: 1000.0


class SpoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        self.incidents = FakeIncidents()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_core_down_spools_then_ingests_late_without_dups(self) -> None:
        event = {"type": "incident", "kind": "exception",
                 "component": "runner", "task_id": "TASK-2026-00379",
                 "attempt_id": "TASK-2026-00379-s1", "phase": "work",
                 "exc_text": "boom"}
        self.assertTrue(post_event(event, runtime_dir=self.runtime))
        spool = self.runtime / endpoint.RUNNER_SPOOL_FILENAME
        self.assertTrue(spool.exists())

        async def _ingest() -> dict[str, int]:
            return await ingest_runner_spool(
                self.incidents, runtime_dir=self.runtime)

        outcome = asyncio.run(_ingest())
        self.assertEqual(outcome["replayed"], 1)
        self.assertEqual(outcome["pending"], 0)
        self.assertEqual(len(self.incidents.docs), 1)
        doc = next(iter(self.incidents.docs.values()))
        self.assertTrue(doc["late"])

        again = asyncio.run(_ingest())
        self.assertEqual(again["pending"], 0)
        self.assertEqual(len(self.incidents.docs), 1)

    def test_oversize_event_spooled_directly(self) -> None:
        big = {"type": "exposure", "component": "runner",
               "task_id": "x" * (endpoint.MAX_EVENT_BYTES + 1)}
        self.assertTrue(post_event(big, runtime_dir=self.runtime))
        self.assertTrue(
            (self.runtime / endpoint.RUNNER_SPOOL_FILENAME).exists())

    def test_spool_event_never_raises(self) -> None:
        self.assertFalse(spool_event({"type": "exposure"},
                                      runtime_dir="/proc/nope/nada"))

    def test_spool_event_assigns_event_id_and_ingests_once(self) -> None:
        self.assertTrue(spool_event({"type": "incident", "kind": "stall",
                                     "component": "runner"},
                                    runtime_dir=self.runtime))
        lines = (self.runtime / endpoint.RUNNER_SPOOL_FILENAME
                 ).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(json.loads(lines[0])["event_id"])

        async def _ingest() -> dict[str, int]:
            return await ingest_runner_spool(
                self.incidents, runtime_dir=self.runtime)

        outcome = asyncio.run(_ingest())
        self.assertEqual(outcome["replayed"], 1)
        self.assertEqual(outcome["pending"], 0)
        again = asyncio.run(_ingest())
        self.assertEqual(again["pending"], 0)
        self.assertEqual(len(self.incidents.docs), 1)

    def test_ingest_preserves_incident_ts(self) -> None:
        self.assertTrue(spool_event(
            {"type": "incident", "kind": "stall", "component": "runner",
             "task_id": "TASK-2026-00379", "ts": "2026-10-20T10:00:00+00:00"},
            runtime_dir=self.runtime))

        async def _ingest() -> dict[str, int]:
            return await ingest_runner_spool(
                self.incidents, runtime_dir=self.runtime,
                now=datetime(2026, 10, 29, tzinfo=timezone.utc))

        self.assertEqual(asyncio.run(_ingest())["replayed"], 1)
        doc = next(iter(self.incidents.docs.values()))
        self.assertEqual(doc["created_at"],
                         datetime(2026, 10, 20, 10, 0,
                                  tzinfo=timezone.utc))


class RunnerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        import automatico
        self._automatico = automatico
        self._repo = automatico.REPO
        automatico.REPO = self.runtime
        automatico._ENDPOINT["module"] = None
        automatico._ENDPOINT["failed"] = False

    def tearDown(self) -> None:
        self._automatico.REPO = self._repo
        self._automatico._ENDPOINT["module"] = None
        self._automatico._ENDPOINT["failed"] = False
        self._tmp.cleanup()

    def test_report_spools_when_core_down(self) -> None:
        self._automatico.report_runner_event(
            "TASK-2026-00379", "exposure", phase="work",
            attempt_id="TASK-2026-00379-s1")
        lines = (self.runtime / ".runtime"
                 / "runner-events-spool.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        event = json.loads(lines[0])
        self.assertEqual(event["type"], "exposure")
        self.assertEqual(event["task_id"], "TASK-2026-00379")

    def test_report_never_raises_without_endpoint(self) -> None:
        self._automatico._ENDPOINT["failed"] = True
        self._automatico._ENDPOINT["module"] = None
        self._automatico.report_runner_event(
            "TASK-2026-00379", "incident", kind="stall",
            attempt_id="TASK-2026-00379-s1", phase="work",
            guard_rule="G1", result_signature="work:X")


class SandboxFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        home = self.root / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".local" / "bin").mkdir(parents=True)
        self.home = home
        self.ws = self.root / "ws"
        (self.ws / ".runtime").mkdir(parents=True)
        (self.ws / ".runtime" / endpoint.SOCKET_FILENAME).write_text("x")
        (self.ws / ".runtime" / endpoint.RUNNER_SPOOL_FILENAME).write_text(
            "{}\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_socket_and_spools_not_mounted(self) -> None:
        from unittest.mock import patch
        from orchestration import agent_sandbox

        with patch.object(agent_sandbox.BubblewrapBackend,
                          "available", True):
            plan = agent_sandbox.plan_agent(
                argv=["muse", "exec", "prompt"], workspace=self.ws,
                base={"PATH": "/bin", "HOME": str(self.home)},
                home=self.home)
        argv = plan.argv
        rt_target = str(self.ws / ".runtime")
        self.assertIn(rt_target, argv)
        self.assertEqual(argv[argv.index(rt_target) - 1], "--tmpfs")
        blob = "\n".join(argv)
        self.assertNotIn(endpoint.SOCKET_FILENAME, blob)
        self.assertNotIn(endpoint.RUNNER_SPOOL_FILENAME, blob)


if __name__ == "__main__":
    unittest.main()
