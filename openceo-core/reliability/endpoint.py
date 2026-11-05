"""Runner reporting channel: Unix socket endpoint + runner spool (design v2 5.6).

The core listens on ``.runtime/reliability.sock`` for ``POST /events`` with
``{type: incident|exposure}``. Bodies are schema-validated, capped at 32 KiB
and rate-limited. Events are evidence, never authorization: builders without
sandbox share the user and can reach the socket, so nothing here admits
tickets, changes gates or runs commands. Exposure events are still accepted
so runners need no change, but they are discarded: their only consumer, the
hunter's verification, was retired (TASK-2026-00510).

When the core is unreachable the runner appends raw events to
``.runtime/runner-events-spool.jsonl``; the core ingests them at boot with
``late=true``, without duplicates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from collections import deque
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import capture as _capture
from .config import RELIABILITY_DEFAULTS
from .models import Component, IncidentKind

logger = logging.getLogger(__name__)

SOCKET_FILENAME = "reliability.sock"
RUNNER_SPOOL_FILENAME = "runner-events-spool.jsonl"
HEADER_MAX_BYTES = 8192

_DEFAULTS = RELIABILITY_DEFAULTS["endpoint"]
MAX_EVENT_BYTES = int(_DEFAULTS["max_event_bytes"])
MAX_EVENTS_PER_MINUTE = int(_DEFAULTS["max_per_minute"])


def socket_path(runtime_dir: str | Path | None = None) -> Path:
    return _capture.runtime_dir_for(runtime_dir) / SOCKET_FILENAME


def runner_spool_path(runtime_dir: str | Path | None = None) -> Path:
    return _capture.runtime_dir_for(runtime_dir) / RUNNER_SPOOL_FILENAME


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_INCIDENT_FIELDS = frozenset({
    "type", "event_id", "ts", "kind", "component", "task_id", "attempt_id",
    "run_id", "phase", "exc_type", "exc_text", "frames", "reason_code",
    "brake", "invariant_id", "result_signature", "guard_rule",
    "service_result", "deadline_type", "escape_type", "tier", "review_policy",
    "project_key", "business_ref", "message", "evidence_refs", "shadow",
})

_EXPOSURE_FIELDS = frozenset({
    "type", "event_id", "ts", "component", "project_key", "task_id",
    "attempt_id", "phase",
})


def validate_event(raw: Any) -> dict[str, Any]:
    """Schema-check one event; returns the normalized dict or raises."""
    if not isinstance(raw, dict):
        raise ValueError("event must be an object")
    kind = raw.get("type")
    if kind == "incident":
        allowed = _INCIDENT_FIELDS
    elif kind == "exposure":
        allowed = _EXPOSURE_FIELDS
    else:
        raise ValueError("event type must be incident|exposure")
    unknown = sorted(k for k in raw if k not in allowed)
    if unknown:
        raise ValueError(f"unknown event fields: {unknown}")
    try:
        component = Component(str(raw.get("component") or "")).value
    except ValueError:
        raise ValueError("event needs a known component") from None
    event: dict[str, Any] = {
        key: raw[key] for key in allowed if key in raw}
    event["type"] = kind
    event["component"] = component
    if kind == "incident":
        try:
            event["kind"] = IncidentKind(str(raw.get("kind") or "")).value
        except ValueError:
            raise ValueError("incident needs a known kind") from None
    event_id = str(raw.get("event_id") or "").strip()
    event["event_id"] = event_id or uuid.uuid4().hex
    return event


class RateLimiter:
    """Sliding-window cap; injectable clock for tests."""

    def __init__(self, max_per_minute: int = MAX_EVENTS_PER_MINUTE,
                 now: Callable[[], float] | None = None) -> None:
        self._max = max(1, int(max_per_minute))
        self._now = now or time.monotonic
        self._hits: deque[float] = deque()

    def allow(self) -> bool:
        mark = self._now()
        hits = self._hits
        while hits and hits[0] <= mark - 60.0:
            hits.popleft()
        if len(hits) >= self._max:
            return False
        hits.append(mark)
        return True


def _reason(status: int, detail: str = "") -> bytes:
    body = json.dumps({"ok": status < 300, "detail": detail}).encode()
    head = (f"HTTP/1.1 {status} {_STATUS_TEXT[status]}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n").encode()
    return head + body


_STATUS_TEXT = {202: "Accepted", 400: "Bad Request", 404: "Not Found",
                413: "Content Too Large", 429: "Too Many Requests"}


async def _read_event(reader: asyncio.StreamReader,
                      max_bytes: int) -> dict[str, Any] | bytes:
    """Parse one POST /events request; bytes response on HTTP error."""
    try:
        head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=10.0)
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError,
            asyncio.TimeoutError):
        return _reason(400, "unreadable headers")
    if len(head) > HEADER_MAX_BYTES:
        return _reason(400, "headers too large")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    method, target = (parts[0], parts[1]) if len(parts) == 3 else ("", "")
    if method != "POST" or target != "/events":
        return _reason(404, "only POST /events is served")
    length = -1
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError:
                length = -1
    if length < 0:
        return _reason(400, "content-length is required")
    if length > max_bytes:
        return _reason(413, f"event over {max_bytes} bytes")
    try:
        raw = await asyncio.wait_for(reader.readexactly(length), timeout=10.0)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError):
        return _reason(400, "truncated body")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _reason(400, "body is not JSON")
    try:
        return validate_event(body)
    except ValueError as exc:
        return _reason(400, str(exc)[:200])


async def _store_validated(incidents: Any, event: dict[str, Any],
                           runtime_dir: str | Path | None) -> bool:
    """Persist one validated incident; spool it when the store is down."""
    if event["type"] != "incident":
        return True
    try:
        fields = {k: v for k, v in event.items()
                  if k not in ("type", "event_id", "ts", "kind",
                               "component")}
        stored = await _capture.record_incident(
            incidents, kind=event["kind"], component=event["component"],
            incident_id=event["event_id"], runtime_dir=runtime_dir,
            **fields)
        return stored is not None
    except Exception:
        logger.warning("Endpoint store failed; spooling event", exc_info=True)
        spool_event(event, runtime_dir)
        return True


def _handler(incidents: Any,
             runtime_dir: str | Path | None,
             limiter: RateLimiter,
             max_bytes: int) -> Callable[..., Any]:
    async def _serve(reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            parsed = await _read_event(reader, max_bytes)
            if isinstance(parsed, bytes):
                writer.write(parsed)
            elif not limiter.allow():
                writer.write(_reason(429, "rate limit exceeded"))
            else:
                await _store_validated(incidents, parsed, runtime_dir)
                writer.write(_reason(202, "accepted"))
            await writer.drain()
        except Exception:
            logger.warning("Endpoint connection failed", exc_info=True)
        finally:
            with suppress(OSError):
                writer.close()

    return _serve


async def start_endpoint(incidents: Any, *,
                         runtime_dir: str | Path | None = None,
                         limiter: RateLimiter | None = None,
                         max_bytes: int = MAX_EVENT_BYTES
                         ) -> asyncio.Server:
    """Listen on the Unix socket (060 group); replaces a stale file."""
    root = _capture.runtime_dir_for(runtime_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / SOCKET_FILENAME
    try:
        path.unlink()
    except OSError:
        pass
    server = await asyncio.start_unix_server(
        _handler(incidents, root, limiter or RateLimiter(), max_bytes),
        path=str(path))
    os.chmod(path, 0o660)
    return server


_SERVER: asyncio.Server | None = None


async def stop_endpoint() -> None:
    """Close the listening socket; boot/shutdown helper, never raises."""
    global _SERVER
    try:
        if _SERVER is not None:
            _SERVER.close()
            await _SERVER.wait_closed()
    except Exception:
        logger.warning("Endpoint stop failed", exc_info=True)
    finally:
        _SERVER = None


def spool_event(event: dict[str, Any],
                runtime_dir: str | Path | None = None) -> bool:
    """Append one raw event to the runner spool; never raises."""
    try:
        payload = dict(event)
        payload.setdefault("event_id", uuid.uuid4().hex)
        path = runner_spool_path(runtime_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, default=str) + "\n"
        if (path.stat().st_size if path.exists() else 0) + len(
                line.encode()) > int(RELIABILITY_DEFAULTS["spool_max_bytes"]):
            logger.warning("Runner spool full; event discarded")
            return False
        with open(path, "ab") as handle:
            handle.write(line.encode("utf-8"))
        return True
    except OSError:
        logger.warning("Runner spool write failed", exc_info=True)
        return False


def post_event(event: dict[str, Any], *,
               runtime_dir: str | Path | None = None,
               timeout: float = 5.0) -> bool:
    """POST one event to the core; spool when unreachable. Never raises."""
    payload = dict(event)
    payload.setdefault("event_id", uuid.uuid4().hex)
    try:
        body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(body) > MAX_EVENT_BYTES:
        return spool_event(payload, runtime_dir)
    request = (b"POST /events HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Type: application/json\r\nContent-Length: "
               + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n"
               + body)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(socket_path(runtime_dir)))
            sock.sendall(request)
            head = b""
            while b"\r\n" not in head:
                chunk = sock.recv(256)
                if not chunk:
                    break
                head += chunk
            code = int(head.decode("latin-1").split(" ")[1])
        finally:
            sock.close()
        if 200 <= code < 300:
            return True
        logger.warning("Endpoint refused event (%s); spooling", code)
        return spool_event(payload, runtime_dir)
    except Exception:  # noqa: BLE001 - fail-open: sin core hay spool
        logger.warning("Endpoint unreachable; spooling event")
        return spool_event(payload, runtime_dir)


def _read_runner_spool(
        runtime_dir: str | Path | None) -> list[dict[str, Any]]:
    try:
        path = runner_spool_path(runtime_dir)
        if not path.exists():
            return []
        docs: list[dict[str, Any]] = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                if isinstance(doc, dict):
                    docs.append(doc)
        return docs
    except OSError:
        logger.warning("Runner spool read failed", exc_info=True)
        return []


def _drop_replayed_events(replayed: list[str],
                           runtime_dir: str | Path | None) -> int:
    try:
        done = set(replayed)
        path = runner_spool_path(runtime_dir)
        if not path.exists():
            return 0
        pending = [doc for doc in _read_runner_spool(runtime_dir)
                   if str(doc.get("event_id") or "") not in done]
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            for doc in pending:
                handle.write(json.dumps(doc, default=str) + "\n")
        tmp.replace(path)
        return len(pending)
    except OSError:
        logger.warning("Runner spool rewrite failed", exc_info=True)
        return -1


async def ingest_runner_spool(incidents: Any, *,
                              runtime_dir: str | Path | None = None,
                              now: datetime | None = None) -> dict[str, int]:
    """Replay spooled runner events with late=true; drops replayed lines."""
    outcome = {"replayed": 0, "pending": 0, "failed": 0}
    try:
        pending = _read_runner_spool(runtime_dir)
        if not pending:
            return outcome
        moment = now or _utcnow()
        replayed: list[str] = []
        for raw in pending:
            try:
                event = validate_event(raw)
            except ValueError:
                replayed.append(str(raw.get("event_id") or ""))
                outcome["replayed"] += 1
                continue
            try:
                stamp = moment
                try:
                    stamp = datetime.fromisoformat(
                        str(event.get("ts") or "").replace("Z", "+00:00"))
                except ValueError:
                    pass
                if event["type"] == "incident":
                    fields = {k: v for k, v in event.items()
                              if k not in ("type", "event_id", "ts", "kind",
                                           "component")}
                    await _capture.record_incident(
                        incidents, kind=event["kind"],
                        component=event["component"],
                        incident_id=event["event_id"], late=True,
                        runtime_dir=runtime_dir, now=stamp, **fields)
                replayed.append(event["event_id"])
                outcome["replayed"] += 1
            except Exception:
                logger.warning("Runner spool ingest failed for %s",
                               raw.get("event_id"), exc_info=True)
                outcome["failed"] += 1
                break
        outcome["pending"] = _drop_replayed_events(replayed, runtime_dir)
        return outcome
    except Exception:
        logger.warning("Runner spool ingest failed", exc_info=True)
        return outcome
