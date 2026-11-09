"""Dead-route receipts, streaming failures and native selection; no network."""
import asyncio
import json
from contextlib import closing
from types import SimpleNamespace

import pytest

from model_routing import research_wiring as wiring
from model_routing.availability import AvailabilityStore, Candidate
from model_routing.paths import catalog_path, rankings_path
from research import deep
from research.runtimes.base import (
    RuntimeExecutionError, RuntimeFirstResponseTimeout, RuntimeOutcome, RuntimeTimeout,
)
from research.runtimes.opencode import OpenCodeRuntime
from test_model_routing_research import _entry
from test_model_routing_research_managed import _catalog_entry, _deployment
from test_research_deep_free_662 import build, draft, verify
from test_research_free_profile_648 import setup as free_setup
from test_research_opencode import (
    _FakeBackend, _FakeProcess, _RecordingSpawner, _request, _settings, _touch_cli,
)


def error_event(message="no deployment can serve", *, kind="provider.no-route", status=503):
    return {"type": "error", "sessionID": "ses_receipt",
            "error": {"type": kind, "message": message, "status": status}}


@pytest.mark.parametrize("failure", [
    RuntimeExecutionError("provider.no-route", exit_code=1, runtime_error="provider.no-route status=503"),
    RuntimeOutcome("", "ses_receipt", events=[error_event()], exit_code=1),
    RuntimeOutcome("", "ses_receipt", events=[error_event()], timed_out=True),
    RuntimeOutcome("", "ses_receipt", events=[error_event(kind="model-unavailable", status=400)]),
    RuntimeOutcome("", "ses_receipt", events=[{"type": "error", "error": {
        "name": "APIError", "data": {"message": "provider.no-route", "statusCode": 503}}}]),
    RuntimeFirstResponseTimeout("no assistant turns", outcome=RuntimeOutcome("", None, timed_out=True)),
])
def test_dead_deployment_rotates_once_within_phase_and_is_skipped_by_next_job(free_setup, failure):
    service, repo, runtime, _ = build(free_setup, [failure, draft, verify])
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    assert [r.model for r in runtime.requests] == ["opencode/a", "opencode/b", "opencode/b"]
    assert done["deep_execution"]["phases"] == ["researcher-1", "verifier"]
    assert done["result"]["usage"]["model_calls"] == 3
    assert done["result"]["usage"]["estimated_micro_usd"] == 0
    assert done["result"]["claims"][0]["verification"] == "supported"
    service._deps.ledger.reserve.assert_not_awaited()
    service._deps.deep_model.run_phase.assert_not_awaited()
    with closing(AvailabilityStore()) as store:
        assert store.cooldown_for("opencode/a")
        assert store.provider_blocked("opencode") is None
        assert store.route_disabled("opencode/a") is None
        assert [row.error_class for row in store.errors_since(0)] == ["model_unavailable"]
    service, repo, runtime, _ = build(free_setup, [draft, verify])
    repo.job["research_id"] += "_next"
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert [r.model for r in runtime.requests] == ["opencode/b", "opencode/b"]


def test_rotation_can_serve_a_healthy_deployment_of_the_same_canonical(tmp_path):
    with closing(AvailabilityStore(tmp_path / "availability.sqlite")) as store:
        candidates = [Candidate(f"opencode/{name}", "family", "canonical", "opencode") for name in ("dead", "healthy")]
        session = wiring.ResearchSession(research_id="receipt", candidates=candidates,
                                         store=store, checkpoints=tmp_path, sleeper=lambda _: None)
        seen = []

        def invoke(request):
            seen.append(request.model)
            if request.model.endswith("dead"):
                return wiring.ProviderTurn("", False, error_text="provider.no-route")
            return wiring.ProviderTurn("checked text", True)

        try:
            assert session.run_turn("check receipts", invoke).rotations == 1
        finally:
            session.close()
        assert seen == ["opencode/dead", "opencode/healthy"]
        assert not store.reservations("receipt", session.session_id)


def test_dead_verifier_fleet_keeps_claims_insufficient_and_attempts_bounded(free_setup):
    failure = RuntimeExecutionError("provider.no-route", exit_code=1, runtime_error="provider.no-route")
    service, _, runtime, _ = build(free_setup, [draft, failure, failure, failure])
    done = asyncio.run(service.run_once())
    assert done["status"] == "blocked"
    assert [r.model for r in runtime.requests] == ["opencode/a", "opencode/a", "opencode/b", "opencode/c"]
    checkpoint = wiring.load_checkpoint(wiring.checkpoints_dir(), done["research_id"])
    assert checkpoint.pending == ("verifier",)
    assert done["free_research"]["deep_phases"]["researcher-1"]
    assert not done.get("result")
    assert done["deep_execution"]["estimated_micro_usd"] == 0
    assert service.slot_snapshot()["calls_in_use"] == service.slot_snapshot()["jobs_in_use"] == 0
    service._deps.ledger.reserve.assert_not_awaited()


def test_native_first_keeps_ten_natives_and_native_backup_ahead_of_ranked_external(free_setup):
    service, _, runtime, _ = build(free_setup, [draft, verify])
    catalog = json.loads(catalog_path().read_text())
    ranks = json.loads(rankings_path().read_text())
    order = ranks["profiles"]["free-research"]["order"]
    for number in range(3, 10):
        name = f"native-{number}"
        catalog["entries"].append(_catalog_entry(f"family-{name}", name, name,
            _deployment(f"opencode/{name}", "opencode", "opencode_native_free", "opencode", name)))
        order.append(_entry(number + 1, name, f"family-{name}", f"opencode/{name}"))
    external = _catalog_entry("family-d", "d", "d", _deployment("groq/d", "groq", "external_free", "groq", "d"))
    external["deployments"].append(_deployment("opencode/d", "opencode", "opencode_native_free", "opencode", "d"))
    catalog["entries"].append(external)
    order.insert(0, _entry(1, "d", "family-d", "groq/d", provider="groq", backups=[("opencode/d", "opencode")]))
    for rank, row in enumerate(order, 1):
        row["rank"] = rank
    catalog_path().write_text(json.dumps(catalog))
    rankings_path().write_text(json.dumps(ranks))
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert [r.model for r in runtime.requests] == ["opencode/d", "opencode/d"]
    saved = wiring.load_checkpoint(wiring.checkpoints_dir(), "res_profile")
    assert len(saved.candidates) == 12
    assert [c.provider for c in saved.candidates] == ["opencode"] * 11 + ["groq"]


class ChunkStream:
    def __init__(self, process, chunks):
        self.process, self.chunks = process, list(chunks)

    async def read(self, size):
        if self.chunks:
            await asyncio.sleep(0)
            return self.chunks.pop(0)
        await self.process._release.wait()
        return b""


def runtime_for(tmp_path, process):
    _touch_cli(tmp_path)
    return OpenCodeRuntime(settings=_settings(tmp_path), isolation_backend=_FakeBackend(),
        spawn=_RecordingSpawner(process), broker_url="http://127.0.0.1:9/mcp/research")


@pytest.mark.parametrize("split", [False, True])
def test_streamed_provider_error_kills_live_worker_and_keeps_receipts(tmp_path, split):
    tool = {"type": "tool_use", "part": {"id": "read-receipt", "tool": "research_read",
            "state": {"status": "completed", "output": "source fact"}}}
    prefix = json.dumps(tool).encode() + b'\n{"type":"text","part":{"text":"kept prefix"}}\n'
    error = json.dumps(error_event()).encode()
    chunks = [prefix, error[:20], error[20:]] if split else [prefix + error + b"\n"]
    process = _FakeProcess(hang=True)
    process.stdout = ChunkStream(process, chunks)
    runtime = runtime_for(tmp_path, process)

    async def scenario():
        with pytest.raises(RuntimeExecutionError, match="provider.no-route") as exc:
            await asyncio.wait_for(runtime.run(_request(tmp_path, timeout_seconds=90)), timeout=1)
        return exc.value

    failure = asyncio.run(scenario())
    assert process.killed and not runtime.is_running("res_abc")
    assert failure.outcome.text == "kept prefix"
    assert failure.outcome.events[0] == tool
    assert "provider.no-route" in failure.runtime_error


@pytest.mark.parametrize("startup", [b"", b'{"type":"step_start","sessionID":"ses_receipt"}\n'])
def test_silent_worker_has_first_response_bound_even_with_open_streams(tmp_path, startup):
    process = _FakeProcess(hang=True)
    process.stdout = ChunkStream(process, [startup] if startup else [])
    runtime = runtime_for(tmp_path, process)

    async def scenario():
        with pytest.raises(RuntimeFirstResponseTimeout) as exc:
            await asyncio.wait_for(runtime.run(_request(tmp_path, timeout_seconds=90,
                first_response_timeout_seconds=0.02)), timeout=1)
        return exc.value

    failure = asyncio.run(scenario())
    assert process.killed and not runtime.is_running("res_abc")
    assert failure.outcome.timed_out and not failure.outcome.text


@pytest.mark.parametrize("silent", [False, True])
def test_service_rotates_running_simulated_worker_inside_researcher_phase(free_setup, tmp_path, monkeypatch, silent):
    service, repo, _, reads = build(free_setup, [])
    repo.job["effective_limits"]["seconds"] = 900
    monkeypatch.setattr(deep, "FREE_DEEP_FIRST_RESPONSE_SECONDS", 0.02)
    process = _FakeProcess(hang=True)
    process.stdout = ChunkStream(process, [] if silent else [json.dumps(error_event()).encode() + b"\n"])
    runtime = runtime_for(tmp_path, process)
    calls = []
    phases = [draft, verify]

    async def spawn(argv, *, cwd, env):
        calls.append(argv[argv.index("--model") + 1])
        if len(calls) == 1:
            return process
        outcome = phases.pop(0)(SimpleNamespace(prompt=argv[-1], session_id=None))
        event = {"type": "text", "sessionID": outcome.session_id, "part": {"text": outcome.text}}
        return _FakeProcess(stdout=json.dumps(event).encode() + b"\n")

    runtime._spawn = spawn
    service._deps.runtime = runtime

    async def scenario():
        return await asyncio.wait_for(service.run_once(), timeout=2)

    done = asyncio.run(scenario())
    assert done["status"] == "completed" and process.killed
    assert calls == ["opencode/a", "opencode/b", "opencode/b"]
    assert len(reads) == 1
    assert done["deep_execution"]["phases"] == ["researcher-1", "verifier"]
    assert done["result"]["usage"]["model_calls"] == 3
    assert done["result"]["usage"]["estimated_micro_usd"] == 0
    assert service.slot_snapshot()["calls_in_use"] == service.slot_snapshot()["jobs_in_use"] == 0
    service._deps.ledger.reserve.assert_not_awaited()
    service._deps.deep_model.run_phase.assert_not_awaited()


@pytest.mark.parametrize("event", [
    {"type": "text", "part": {"text": "assistant progress"}},
    {"type": "reasoning", "part": {"text": "reasoning progress"}},
    {"type": "tool_use", "part": {"id": "read", "tool": "research_read"}},
])
def test_assistant_progress_removes_only_first_response_bound(tmp_path, event):
    process = _FakeProcess(hang=True)
    process.stdout = ChunkStream(process, [json.dumps(event).encode() + b"\n"])
    runtime = runtime_for(tmp_path, process)

    async def scenario():
        with pytest.raises(RuntimeTimeout) as exc:
            await runtime.run(_request(tmp_path, timeout_seconds=0.08, first_response_timeout_seconds=0.02))
        assert not isinstance(exc.value, RuntimeFirstResponseTimeout)

    asyncio.run(scenario())
    assert process.killed


def test_deep_requests_first_response_bound_inside_phase_and_paid_stays_unused(free_setup):
    service, _, runtime, _ = build(free_setup, [draft, verify])
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert all(0 < r.first_response_timeout_seconds <= min(15, r.timeout_seconds) for r in runtime.requests)
    service._deps.deep_model.run_phase.assert_not_awaited()


def test_failed_verification_after_rotation_never_promotes_unverified_claims(free_setup, monkeypatch):
    async def slow(request):
        await asyncio.Event().wait()

    failure = RuntimeExecutionError("provider.no-route", exit_code=1, runtime_error="provider.no-route")
    service, _, _, _ = build(free_setup, [failure, draft, slow])
    monkeypatch.setattr(deep, "FREE_DEEP_PHASE_SECONDS", {**deep.FREE_DEEP_PHASE_SECONDS, "verifier": 0.02})
    done = asyncio.run(service.run_once())
    assert done["status"] == "partial"
    assert done["result"]["claims"][0]["verification"] == "insufficient_evidence"
    assert done["result"]["usage"]["estimated_micro_usd"] == 0
    service._deps.ledger.reserve.assert_not_awaited()
