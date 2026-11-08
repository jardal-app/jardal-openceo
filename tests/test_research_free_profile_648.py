"""Short service-level contracts with a scripted runtime; no model or network."""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "openceo-core"))

from model_routing import research_wiring as wiring
from model_routing.availability import AvailabilityStore
from model_routing.paths import catalog_path, rankings_path
from research.contracts import ResearchJobStatus, can_transition
from research.runtimes.base import RuntimeCancelled, RuntimeExecutionError, RuntimeOutcome, RuntimeTimeout, RuntimeUnavailable
from research.service import ResearchDependencies, ResearchService, ResearchServiceError
from research import free_profile
from test_model_routing_research import _entry, _rankings_doc
from test_model_routing_research_managed import _catalog_doc, _catalog_entry, _deployment


class Repo:
    def __init__(self):
        self.job = {"research_id": "res_profile", "project_key": "jardal-openceo", "owner_id": "usr_owner",
                    "question": "What does this source establish?", "mode": "quick", "status": "queued",
                    "classification": "public", "seed_urls": ["https://example.org/source"],
                    "effective_limits": {"steps": 8, "seconds": 60, "pages": 1}}

    async def get_research_job(self, research_id):
        return copy.deepcopy(self.job) if research_id == self.job["research_id"] else None

    async def claim_next_research_job(self, **kwargs):
        if self.job["status"] != "queued":
            return None
        self.job["status"] = "running"
        return copy.deepcopy(self.job)

    async def patch_research_job(self, research_id, updates):
        self.job.update(copy.deepcopy(updates))
        return copy.deepcopy(self.job)

    async def transition_research_job(self, research_id, status, *, updates=None):
        assert can_transition(ResearchJobStatus(self.job["status"]), status)
        self.job.update(copy.deepcopy(updates or {}), status=status.value)
        return copy.deepcopy(self.job)


class Runtime:
    def __init__(self, script):
        self.script, self.requests = list(script), []

    async def run(self, request):
        self.requests.append(request)
        action = self.script.pop(0)
        if callable(action):
            action = action(request)
        if asyncio.iscoroutine(action):
            action = await action
        if isinstance(action, BaseException):
            raise action
        return action


def report():
    return RuntimeOutcome(text=json.dumps({"summary": "Source establishes a fact", "claims": [
        {"id": "c1", "statement": "Source establishes a fact", "kind": "fact",
         "evidence_ids": ["res_profile-e1"], "support": [{"extract": "source fact"}]}]}), session_id="ses_test")


def fail(status, *, events=(), text=""):
    return RuntimeExecutionError("provider failure", exit_code=1, runtime_error=f"status={status}",
                                 outcome=RuntimeOutcome(text=text, session_id="ses_test", events=list(events)))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(wiring, "_RETRY_BUDGET", SimpleNamespace(delay=lambda *args: 0.0))
    monkeypatch.setattr(free_profile, "load_router_secrets", lambda: SimpleNamespace(
        present=lambda: {"FREE_ROUTER_GROQ_API_KEY": True}))
    catalog_path().parent.mkdir(parents=True)
    entries, order = [], []
    for rank, name in enumerate(("a", "b", "c"), 1):
        dep = _deployment(f"opencode/{name}", "opencode", "opencode_native_free", "opencode", name)
        entries.append(_catalog_entry(f"family-{name}", name, name, dep))
        order.append(_entry(rank, name, f"family-{name}", f"opencode/{name}"))
    catalog_path().write_text(json.dumps(_catalog_doc(entries)))
    rankings_path().write_text(json.dumps(_rankings_doc(order)))

    def build(script):
        repo, runtime, reads = Repo(), Runtime(script), []

        async def read(**kwargs):
            reads.append(kwargs)
            return {"url": kwargs["url"], "text": "source fact"}

        settings = SimpleNamespace(RESEARCH_ENABLED=True, RESEARCH_OPENCODE_TEST_MODEL="opencode/static-free",
                                   RESEARCH_WORKSPACE_DIR=str(tmp_path / "workspace"))
        service = ResearchService(settings=settings, deps=ResearchDependencies(repo=repo, runtime=runtime, read=read))
        return service, repo, runtime, reads

    return build


def test_service_checkpoints_each_real_turn_and_keeps_affinity(setup):
    checkpoints = []

    def synthesis(request):
        checkpoints.append(wiring.load_checkpoint(wiring.checkpoints_dir(), request.research_id))
        return report()

    service, repo, runtime, reads = setup([RuntimeOutcome("Evidence outline", "ses_test"), synthesis])
    result = asyncio.run(service.run_once())
    assert result["status"] == "completed"
    assert [r.model for r in runtime.requests] == ["opencode/a", "opencode/a"]
    assert runtime.requests[1].session_id == "ses_test"
    assert checkpoints[0].turn == 1 and checkpoints[0].queries_and_sources
    assert checkpoints[0].pending == ("synthesis",)
    saved = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
    assert saved.turn == 2 and saved.rankings_snapshot.startswith("sha256:")
    assert not saved.pending
    assert saved.plan == ("analysis", "synthesis") and saved.validated_results
    assert len(reads) == 1
    assert service.slot_snapshot()["calls_in_use"] == service.slot_snapshot()["jobs_in_use"] == 0
    with closing(AvailabilityStore()) as store:
        assert not store.reservations(repo.job["research_id"], saved.session_id)


@pytest.mark.parametrize("failure", [fail(429), fail(404),
                                      RuntimeOutcome("", "ses_test")])
def test_service_rotates_on_unusable_response_and_pins_replacement(setup, failure):
    service, _, runtime, _ = setup([failure, RuntimeOutcome("Outline", "ses_test"), report()])
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert [r.model for r in runtime.requests] == ["opencode/a", "opencode/b", "opencode/b"]
    with closing(AvailabilityStore()) as store:
        assert store.errors_since(0)


@pytest.mark.parametrize("external_first", [False, True])
def test_ranked_existing_provider_is_served_and_recovers_native_auth_failure(setup, external_first):
    service, _, runtime, _ = setup(([] if external_first else [fail(401)]) + [RuntimeOutcome("Outline", "ses_test"), report()])
    catalog = json.loads(catalog_path().read_text())
    catalog["entries"].append(_catalog_entry("family-d", "d", "d",
        _deployment("groq/d", "groq", "external_free", "groq", "d")))
    catalog_path().write_text(json.dumps(catalog))
    document = json.loads(rankings_path().read_text())
    order = document["profiles"]["free-research"]["order"]
    order.insert(0 if external_first else len(order), _entry(1 if external_first else 4, "d", "family-d", "groq/d", provider="groq"))
    for rank, entry in enumerate(order, 1):
        entry["rank"] = rank
    rankings_path().write_text(json.dumps(document))
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert [r.model for r in runtime.requests] == ([] if external_first else ["opencode/a"]) + ["groq/d", "groq/d"]
    config = runtime.requests[-1].provider_config
    assert set(config) == {"groq"} and config["groq"]["settings"]["apiKey"] == "{env:FREE_ROUTER_GROQ_API_KEY}"


@pytest.mark.parametrize("failure", [fail(503), RuntimeTimeout("slow")])
def test_service_retries_same_then_rotates_without_resetting_budget(setup, failure):
    service, repo, runtime, _ = setup([failure, failure, RuntimeOutcome("Outline", "ses_test"), report()])
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert [r.model for r in runtime.requests] == ["opencode/a", "opencode/a", "opencode/b", "opencode/b"]
    assert repo.job["free_research"]["steps_left"] == 3


def test_partial_failure_carries_tool_ledger_and_prefix_to_next_attempt(setup):
    tool = {"type": "tool_use", "part": {"id": "call-source", "tool": "research_read",
            "state": {"status": "completed", "output": "source fact"}}}
    partial = fail(503, events=[tool, {"type": "text", "part": {"text": "Kept prefix"}}], text="Kept prefix")

    def rebuilt(request):
        assert "Kept prefix" in request.prompt and "call-source=source fact" in request.prompt
        checkpoint = wiring.load_checkpoint(wiring.checkpoints_dir(), request.research_id)
        assert checkpoint.partial_text == "Kept prefix"
        assert checkpoint.applied_tools[0].id == "call-source"
        return RuntimeOutcome("Outline", "ses_test")

    service, _, runtime, reads = setup([partial, rebuilt, report()])
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert len(reads) == 1 and len(runtime.requests) == 3


@pytest.mark.parametrize("kind", ["missing", "invalid", "expired", "empty", "disabled", "paid", "tools", "context", "stale_catalog"])
def test_degraded_profile_blocks_loud_without_static_model(setup, kind):
    service, repo, runtime, _ = setup([])
    if kind == "missing":
        rankings_path().unlink()
    elif kind == "invalid":
        rankings_path().write_text("{broken")
    elif kind == "disabled":
        service._settings.RESEARCH_FREE_ROUTER_ENABLED = False
    elif kind in {"paid", "tools", "context", "stale_catalog"}:
        catalog = json.loads(catalog_path().read_text())
        if kind == "stale_catalog":
            catalog["stale"] = True
        for entry in catalog["entries"]:
            if kind == "paid":
                entry["deployments"][0]["input_price_usd"] = 1
            elif kind == "tools":
                entry["capabilities"]["tools"] = False
            elif kind == "context":
                entry["capabilities"]["context_tokens"] = 100
        catalog_path().write_text(json.dumps(catalog))
    else:
        document = json.loads(rankings_path().read_text())
        if kind == "expired":
            document["generated_at"] = (datetime.now(timezone.utc) - timedelta(hours=31)).isoformat()
        else:
            document["profiles"]["free-research"]["order"] = []
        rankings_path().write_text(json.dumps(document))
    result = asyncio.run(service.run_once())
    assert result["status"] == "blocked" and "BLOCKED_NO_ELIGIBLE_MODEL" in result["failure_reason"]
    assert not runtime.requests and not result.get("result")
    assert wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"]) is not None
    assert asyncio.run(service.run_once()) is None


@pytest.mark.parametrize("searches", [0, 1])
def test_block_resume_keeps_snapshot_phase_and_sources_without_acquisition_replay(setup, searches):
    service, repo, runtime, reads = setup([RuntimeOutcome("Outline", "ses_test"), fail(429), fail(429), fail(429)])
    repo.job["effective_limits"]["search_requests"] = 3
    if searches:
        repo.job["seed_urls"] = []
    repo.job["adapters"] = {"serper": True}
    repo.job["provider_status"] = {name: {"configured": True, "mounted": True, "verified": None}
                                   for name in ("serper", "firecrawl", "deep")}
    service._deps.discover = AsyncMock(return_value=SimpleNamespace(
        provider="serper", items=[SimpleNamespace(url="https://example.org/source")]))
    reader = service._deps.read

    async def read(**kwargs):
        return {**await reader(**kwargs), "provider": "firecrawl"}

    service._deps.read = read
    expected_providers = copy.deepcopy(repo.job["provider_status"])
    expected_providers["serper"]["verified"] = True if searches else None
    expected_providers["firecrawl"]["verified"] = True

    async def scenario():
        blocked = await service.run_once()
        assert blocked["status"] == "blocked" and blocked["free_research"]["phase"] == "synthesis"
        assert blocked["free_research"]["search_requests"] == searches
        assert blocked["provider_status"] == expected_providers
        saved = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
        assert saved.turn == 1
        with closing(AvailabilityStore()) as store:
            assert all(store.cooldown_for(f"opencode/{name}") for name in ("a", "b", "c"))
        rankings_path().write_text(json.dumps(_rankings_doc([_entry(1, "c", "family-c", "opencode/c")])))
        with closing(AvailabilityStore()) as store:
            for name in ("a", "b", "c"):
                store.set_cooldown(f"opencode/{name}", "expired test cooldown", seconds=0, now=0)
        replacement = ResearchService(settings=service._settings, deps=service._deps)
        await replacement.resume(repo.job["research_id"])
        runtime.script.append(report())
        done = await replacement.run_once()
        assert done["status"] == "completed"
        assert done["result"]["usage"]["search_requests"] == searches
        assert done["provider_status"] == expected_providers
        after = wiring.load_checkpoint(wiring.checkpoints_dir(), repo.job["research_id"])
        assert after.turn == 2 and after.rankings_snapshot == saved.rankings_snapshot
        assert runtime.requests[-1].model == "opencode/c"

    asyncio.run(scenario())
    assert len(reads) == 1
    assert service._deps.discover.await_count == searches


@pytest.mark.parametrize("failure", [fail(403), RuntimeUnavailable("missing runtime")])
def test_configuration_failures_block_without_arbitrary_rotation(setup, failure):
    script = [failure, failure] if isinstance(failure, RuntimeExecutionError) else [failure]
    service, _, runtime, _ = setup(script)
    result = asyncio.run(service.run_once())
    assert result["status"] == "blocked" and "BLOCKED_PROVIDER" in result["failure_reason"]
    assert {r.model for r in runtime.requests} == {"opencode/a"}


def test_tool_without_result_blocks_before_replay(setup):
    missing = fail(503, events=[{"type": "tool_use", "part": {"id": "call-unknown", "tool": "research_read"}}])
    service, _, runtime, _ = setup([missing])
    result = asyncio.run(service.run_once())
    assert result["status"] == "blocked" and "BLOCKED_TOOL_CONFLICT" in result["failure_reason"]
    assert len(runtime.requests) == 1
    with pytest.raises(ResearchServiceError, match="reconciled"):
        asyncio.run(service.resume(result["research_id"]))


def test_cancel_during_a_turn_keeps_terminal_state_and_releases_slots(setup):
    service, _, runtime, _ = setup([])

    async def cancel(request):
        await service.cancel(request.research_id)
        return RuntimeCancelled("cancelled")

    runtime.script.append(cancel)
    assert asyncio.run(service.run_once())["status"] == "cancelled"
    assert len(runtime.requests) == 1 and service.slot_snapshot()["calls_in_use"] == 0


def test_resume_endpoint_checks_owner_and_wakes_dispatcher(setup, monkeypatch):
    from test_research_facade import _app, _client, _cookie
    import auth.erp_identity

    monkeypatch.setattr(auth.erp_identity, "identity_provider_is_erp", lambda settings: False)

    service, repo, _, _ = setup([])
    repo.job["status"] = "blocked"
    wakes = []

    async def scenario():
        async with _client(_app(service, on_admitted=wakes.append)) as client:
            url = f"/api/research/jobs/{repo.job['research_id']}/resume"
            client.cookies.update(_cookie("usr_other"))
            denied = await client.post(url)
            assert denied.status_code == 404 and repo.job["status"] == "blocked"
            client.cookies.update(_cookie("usr_owner"))
            accepted = await client.post(url)
            assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
            assert "free_research" not in accepted.json()

    asyncio.run(scenario())
    assert wakes == [repo.job["research_id"]]


@pytest.mark.parametrize("timeout", [False, True])
def test_runtime_keeps_events_and_partial_text_when_process_fails(tmp_path, timeout):
    from research.runtimes.opencode import OpenCodeRuntime
    from test_research_opencode import _FakeBackend, _FakeProcess, _RecordingSpawner, _request, _settings, _touch_cli

    _touch_cli(tmp_path)
    events = [{"type": "tool_use", "part": {"id": "call-source", "tool": "research_read",
               "state": {"status": "completed", "output": "source fact"}}},
              {"type": "text", "part": {"text": "Kept prefix"}}]
    raw = "\n".join(json.dumps(event) for event in events).encode()
    process = _FakeProcess(stdout=raw, returncode=1, hang=timeout)
    runtime = OpenCodeRuntime(settings=_settings(tmp_path), isolation_backend=_FakeBackend(),
                             spawn=_RecordingSpawner(process), broker_url="http://127.0.0.1:9/mcp/research")
    with pytest.raises(RuntimeTimeout if timeout else RuntimeExecutionError) as error:
        asyncio.run(runtime.run(_request(tmp_path, timeout_seconds=0.01 if timeout else 1)))
    assert error.value.outcome.text == "Kept prefix"
    assert error.value.outcome.events[0]["part"]["id"] == "call-source"
    if timeout:
        assert process.killed


def test_worker_receives_only_selected_free_provider_credential(tmp_path, monkeypatch):
    from research.runtimes.opencode import OpenCodeRuntime, WORKER_CONFIG_RELPATH
    from model_routing.external import build_external_providers_config
    from model_routing.models import CatalogDocument
    import model_routing.secrets
    from test_research_opencode import _FakeBackend, _FakeProcess, _RecordingSpawner, _request, _settings, _touch_cli

    _touch_cli(tmp_path)
    monkeypatch.setattr(model_routing.secrets, "load_router_secrets", lambda: SimpleNamespace(
        FREE_ROUTER_GROQ_API_KEY="synthetic-groq-key", FREE_ROUTER_GEMINI_API_KEY="synthetic-unselected-key"))
    config = build_external_providers_config("v2", CatalogDocument.model_validate(_catalog_doc([
        _catalog_entry("family-d", "d", "d", _deployment("groq/d", "groq", "external_free", "groq", "d"))])))
    process = _FakeProcess(stdout=b'{"type":"text","part":{"text":"answer"}}')
    spawn = _RecordingSpawner(process)
    runtime = OpenCodeRuntime(settings=_settings(tmp_path), isolation_backend=_FakeBackend(), spawn=spawn,
        broker_url="http://127.0.0.1:9/mcp/research", base_env={"BRAVE_API_KEY": "synthetic-host-key"})
    asyncio.run(runtime.run(_request(tmp_path, model="groq/d", provider="groq", provider_config=config["providers"])))
    env = spawn.calls[0]["env"]
    assert env["FREE_ROUTER_GROQ_API_KEY"] == "synthetic-groq-key"
    assert "FREE_ROUTER_GEMINI_API_KEY" not in env and "BRAVE_API_KEY" not in env
    written = (Path(env["XDG_CONFIG_HOME"]) / WORKER_CONFIG_RELPATH).read_text()
    assert "synthetic-groq-key" not in written and "{env:FREE_ROUTER_GROQ_API_KEY}" in written
