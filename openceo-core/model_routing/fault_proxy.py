"""Loopback fault-injection proxy for quota and fallback tests (TASK-2026-00291).

Scripted responses only, served on 127.0.0.1: the proxy never forwards to
real providers, so injected faults spend no quota and test credentials
never transit. One fault kind per provider, set before ``start()``.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Literal

from model_routing import live as router_live
from model_routing import probes as router_probes

__all__ = [
    "FaultKind",
    "FaultProxy",
    "FaultSpec",
]

FaultKind = Literal[
    "rate_limited",
    "daily_quota",
    "server_error",
    "timeout",
    "billing",
    "stream_cut",
    "empty",
    "ok",
]


@dataclass(frozen=True)
class FaultSpec:
    """Scripted answer for one provider; ``ok`` is the control response."""

    kind: FaultKind = "ok"
    retry_after_s: int = 60
    delay_s: float = 3.0
    message: str = ""


@dataclass
class _ProxyState:
    faults: dict[str, FaultSpec] = field(default_factory=dict)
    default: FaultSpec = field(default_factory=FaultSpec)
    served: list[tuple[str, str]] = field(default_factory=list)


_OK_COMPLETION = {
    "id": "fault-proxy-ok",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9},
}


class _FaultHandler(BaseHTTPRequestHandler):
    server: ThreadingHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        """Access logs stay silent; tests read ``served`` instead."""

    def _state(self) -> _ProxyState:
        state = getattr(self.server, "fault_state", None)
        assert isinstance(state, _ProxyState)
        return state

    def _send_json(self, status: int, payload: dict,
                   headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        state = self._state()
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        parts = (self.path or "").split("?", 1)[0].strip("/").split("/")
        provider = parts[1] if len(parts) == 2 and parts[0] == "fault" else ""
        spec = state.faults.get(provider, state.default)
        state.served.append((provider, spec.kind))
        message = spec.message or f"injected {spec.kind} for {provider or 'unknown'}"
        if spec.kind == "rate_limited":
            self._send_json(429, {"error": {"code": "rate_limited", "message": message}},
                            {"Retry-After": str(spec.retry_after_s)})
        elif spec.kind == "daily_quota":
            self._send_json(429, {"error": {"code": "daily_quota",
                                            "message": f"daily free quota exhausted: {message}"}},
                            {"Retry-After": str(spec.retry_after_s)})
        elif spec.kind == "server_error":
            self._send_json(503, {"error": {"code": "server_error", "message": message}})
        elif spec.kind == "timeout":
            time.sleep(spec.delay_s)
            self._send_json(200, dict(_OK_COMPLETION))
        elif spec.kind == "billing":
            self._send_json(402, {"error": {"code": "billing", "message": message}})
        elif spec.kind == "stream_cut":
            chunk = b'data: {"choices": [{"delta": {"content": "par"}}, "index": 0]}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(chunk)
            self.wfile.flush()
            self.close_connection = True
        elif spec.kind == "empty":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_json(200, dict(_OK_COMPLETION))


class FaultProxy:
    """One fault script per provider, served from an ephemeral loopback port."""

    def __init__(self, faults: dict[str, FaultSpec | FaultKind] | None = None,
                 default: FaultSpec | FaultKind = "ok") -> None:
        self._state = _ProxyState(
            faults={name: spec if isinstance(spec, FaultSpec) else FaultSpec(spec)
                    for name, spec in (faults or {}).items()},
            default=default if isinstance(default, FaultSpec) else FaultSpec(default),
        )
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Bind 127.0.0.1 on an ephemeral port; returns the base URL."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FaultHandler)
        server.daemon_threads = True
        server.fault_state = self._state  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        host, port = server.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        """Shut the listener down; idempotent."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> FaultProxy:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        """Base URL of the running listener."""
        if self._server is None:
            raise RuntimeError("fault proxy is not started")
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @property
    def served(self) -> list[tuple[str, str]]:
        """(provider, kind) pairs served so far; no headers or bodies."""
        return list(self._state.served)

    def post(self, provider: str, payload: dict,
             *, kind: str = "chat", timeout: float = 30.0) -> dict:
        """POST through the proxy with probe-client error semantics.

        Drop-in for ``run_probe(post=...)``: 429 raises ``LiveRateLimited``,
        other HTTP failures raise ``ProbeError``, timeouts surface as
        ``ProbeError`` too. Never carries real credentials.
        """
        url = f"{self.base_url}/fault/{provider}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "free-router-fault-proxy/1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise router_live.LiveRateLimited(
                    provider, url, exc.headers.get("Retry-After")) from exc
            raise router_probes.ProbeError.from_http(
                provider, kind, exc.code, url,
                exc.headers.get("Retry-After")) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise router_probes.ProbeError(
                provider, kind, f"POST {url} failed: {type(exc).__name__}") from exc
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except ValueError as exc:
            raise router_probes.ProbeError(
                provider, kind, f"POST {url} returned non-JSON") from exc
        if not isinstance(parsed, dict):
            raise router_probes.ProbeError(
                provider, kind, f"POST {url} returned an empty payload")
        return parsed
