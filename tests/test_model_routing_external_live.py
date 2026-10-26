"""External interfaces live test (TASK-2026-00295).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. Two parts, one real model call:

1. The installed OpenCode accepts the generated classic config: a run
   against a local stub gateway (127.0.0.1, 404s everything) fails
   with a clear provider error after the stub sees the request. Zero
   tokens; the gateway stays absent (HOLD).
2. One native call with the gateway absent answers through the free
   tier without touching the gateway entry. Zero cost holds
   structurally (CLI without credentials on a free-tier id).

Serving evidence for the native model (same day, no quota spent to
learn it): the loop coordinator probed ``opencode/beam-free`` healthy
and completed a full review with it minutes before this cycle; the
second reviewer used ``opencode/nemotron-3.5-lightning-free``. Prompts
are synthetic and tiny. Never prints secrets.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import external as router_external  # noqa: E402
from model_routing import live as router_live  # noqa: E402

pytestmark = pytest.mark.live

PROMPT = "Reply with exactly: ok"

NATIVE_CANDIDATES = (
    "opencode/beam-free",
    "opencode/nemotron-3.5-lightning-free",
)


class _StubGateway(BaseHTTPRequestHandler):
    """Absent gateway: records hits, 404s everything, spends nothing."""

    hits: list[tuple[str, str]] = []

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).hits.append((self.command, self.path))
        body = json.dumps(
            {"error": {"message": "stub gateway: absent", "code": 404}}).encode()
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *args):
        pass


def _run_opencode(model: str, config: dict, cwd: Path, timeout: int):
    env = dict(os.environ)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    env["FREE_GATEWAY_KEY"] = "dummy-unreachable"
    try:
        return subprocess.run(
            ["opencode", "run", "--standalone", "--format", "json",
             "--model", model, PROMPT],
            cwd=cwd, capture_output=True, text=True,
            timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired:
        pytest.fail(f"opencode run {model} timed out")


def _raise_on_429(output: str) -> None:
    lowered = output.lower()
    if "429" in lowered or "rate limit" in lowered or "rate_limit" in lowered:
        raise router_live.LiveRateLimited("opencode", "opencode-cli", None)


def test_generated_config_accepted_and_native_call_answers(tmp_path):
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI is not installed")
    _StubGateway.hits = []
    server = HTTPServer(("127.0.0.1", 0), _StubGateway)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = router_external.build_opencode_config(
            "classic", base_url=f"http://127.0.0.1:{port}/v1")
        work = tmp_path / "work"
        work.mkdir()

        failed = _run_opencode("free-gateway/free-research", config, work, timeout=90)
        assert failed.returncode != 0, "run against the absent gateway must fail"
        assert any(method == "POST" and path == "/v1/chat/completions"
                   for method, path in _StubGateway.hits), (
            f"stub saw no chat request: {_StubGateway.hits}")
        errors = [json.loads(line) for line in failed.stdout.splitlines()
                  if line.strip()]
        assert any(event.get("type") == "error"
                   and (event.get("error") or {}).get("status") == 404
                   for event in errors), failed.stdout[-500:]
        stub_hits_after_part1 = len(_StubGateway.hits)

        last_tail = ""
        for candidate in NATIVE_CANDIDATES:
            completed = _run_opencode(candidate, config, work, timeout=150)
            combined = completed.stdout + completed.stderr
            _raise_on_429(combined)
            events = []
            for line in completed.stdout.splitlines():
                if line.strip():
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        break
            texts = [event["part"]["text"] for event in events
                     if event.get("type") == "text"
                     and isinstance(event.get("part"), dict)]
            # One-shot text answers carry no step_finish receipt;
            # zero cost holds structurally: the CLI runs without
            # credentials on a free-tier model id, so a paid route
            # would fail instead of billing (TASK-2026-00289).
            if (completed.returncode == 0 and texts
                    and re.search(r"\bok\b", " ".join(texts).lower())):
                assert len(_StubGateway.hits) == stub_hits_after_part1, (
                    "native call must not touch the gateway")
                return
            last_tail = combined[-500:]
        pytest.fail(f"no native candidate answered; last tail: {last_tail}")
    finally:
        server.shutdown()
