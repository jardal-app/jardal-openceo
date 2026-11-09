"""Free deep phases, explicit paid authority and resumable receipts; no network."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from contextlib import closing

import httpx
import pytest

from model_routing import research_wiring as wiring
from model_routing.paths import catalog_path, rankings_path
from research.contracts import ResearchJobRequest
from research.deep_model import BudgetedDeepModel, DeepModelError
from research.policies import ResearchPolicyError
from research.runtimes.base import RuntimeOutcome, RuntimeUnavailable
from research.service import ResearchDependencies, ResearchService, ResearchServiceError
from research.wiring import _provider_status
from test_model_routing_research import _entry
from test_model_routing_research_managed import _catalog_entry, _deployment
from test_research_deep_live_641 import MemoryLedger, PRINCIPAL, config, settings, local_auth
from test_research_facade import _FakeRepo, _app, _client, _cookie
from test_research_free_profile_648 import setup as free_setup, fail


def phase_payload(request):
    return json.JSONDecoder().raw_decode(request.prompt)[0]


def draft(request):
    payload = phase_payload(request)
    return RuntimeOutcome(json.dumps({"summary": "Source fact", "claims": [{
        "id": "c1", "statement": "source fact", "kind": "fact",
        "evidence_ids": [payload["evidence"][0]["id"]], "support": [{"extract": "source fact"}]}]}), "ses_researcher")


def verify(request, verdict="supported"):
    payload = phase_payload(request)
    assert request.session_id is None
    assert "draft" not in payload and "summary" not in payload
    return RuntimeOutcome(json.dumps({"verdicts": [{"claim_digest": c["claim_digest"],
        "verdict": verdict, "evidence_id": c["evidence_ids"][0], "extract": "source fact",
        "reason": "Checked the retained source"} for c in payload["claims"]]}), "ses_verifier")


def build(free_setup, script):
    service, repo, runtime, reads = free_setup(script)
    repo.job["mode"] = "deep"
    repo.job["effective_limits"]["steps"] = 15
    repo.job["provider_status"] = {name: {"configured": True, "mounted": True, "verified": None}
                                   for name in ("deep", "deep-free")}
    service._deps.ledger = SimpleNamespace(reserve=AsyncMock(side_effect=AssertionError("paid reservation")))
    service._deps.deep_model = SimpleNamespace(run_phase=AsyncMock(side_effect=AssertionError("paid model")))
    return service, repo, runtime, reads


def test_default_deep_is_free_with_checked_claims_zero_ledger_and_affinity(free_setup):
    service, repo, runtime, reads = build(free_setup, [draft, verify])
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    assert done["result"]["claims"][0]["verification"] == "supported"
    assert done["deep_execution"]["phases"] == ["researcher-1", "verifier"]
    assert done["result"]["usage"]["estimated_micro_usd"] == 0
    assert done["result"]["usage"]["model_calls"] == 2
    assert done["result"]["usage"]["uncertain"] is False
    assert done["provider_status"]["deep-free"]["verified"] is True
    assert done["provider_status"]["deep"]["verified"] is None
    assert [r.model for r in runtime.requests] == ["opencode/a", "opencode/a"]
    assert all(r.session_id is None and not r.allow_session_resume and not r.tool_credential for r in runtime.requests)
    assert len(reads) == 1 and repo.job["free_research"]["steps_left"] == 12
    service._deps.ledger.reserve.assert_not_awaited()
    service._deps.deep_model.run_phase.assert_not_awaited()
    assert service.slot_snapshot()["calls_in_use"] == 0


@pytest.mark.parametrize("unavailable", ["disabled", "catalog", "runtime", "exhausted"])
def test_free_unavailable_blocks_with_reason_and_never_pays(free_setup, unavailable):
    script = [RuntimeUnavailable("missing isolation")] if unavailable == "runtime" else []
    if unavailable == "exhausted":
        script = [fail(429), fail(429), fail(429)]
    service, _, runtime, _ = build(free_setup, script)
    if unavailable == "disabled":
        service._settings.RESEARCH_FREE_ROUTER_ENABLED = False
    elif unavailable == "catalog":
        catalog_path().unlink()
    done = asyncio.run(service.run_once())
    assert done["status"] == "blocked" and "BLOCKED_" in done["failure_reason"]
    assert not done.get("result")
    assert done["deep_execution"]["estimated_micro_usd"] == 0
    assert done["provider_status"]["deep-free"]["verified"] is None
    service._deps.ledger.reserve.assert_not_awaited()
    service._deps.deep_model.run_phase.assert_not_awaited()
    assert all(r.model != "opencode/static-free" for r in runtime.requests)


def test_deep_native_group_precedes_external_rank_and_correction_is_bounded(free_setup):
    def correction(request):
        payload = phase_payload(request)
        assert request.session_id is None
        return RuntimeOutcome(json.dumps(payload["draft"]), "ses_correction")

    service, _, runtime, _ = build(free_setup, [draft, lambda r: verify(r, "contradicted"), correction, verify])
    catalog = json.loads(catalog_path().read_text())
    catalog["entries"].append(_catalog_entry("family-d", "d", "d",
        _deployment("groq/d", "groq", "external_free", "groq", "d")))
    catalog_path().write_text(json.dumps(catalog))
    ranks = json.loads(rankings_path().read_text())
    order = ranks["profiles"]["free-research"]["order"]
    order.insert(0, _entry(1, "d", "family-d", "groq/d", provider="groq"))
    for rank, row in enumerate(order, 1):
        row["rank"] = rank
    rankings_path().write_text(json.dumps(ranks))
    done = asyncio.run(service.run_once())
    assert done["status"] == "partial"
    assert done["deep_execution"]["phases"] == ["researcher-1", "verifier", "correction", "verifier-final"]
    assert [r.model for r in runtime.requests] == ["opencode/a"] * 4
    assert any(row["verdict"] == "contradicted" for row in done["deep_execution"]["verification_history"])


def test_resume_retains_finished_phases_and_acquisition_without_duplicate_effects(free_setup):
    tool = {"type": "tool_use", "part": {"id": "source-receipt", "tool": "research_read",
            "state": {"status": "completed", "output": "source fact"}}}
    service, repo, runtime, reads = build(free_setup, [draft, fail(429, events=[tool]), fail(429), fail(429)])

    async def scenario():
        blocked = await service.run_once()
        assert blocked["status"] == "blocked"
        assert "researcher-1" in blocked["free_research"]["deep_phases"]
        with closing(wiring.AvailabilityStore()) as store:
            for name in ("a", "b", "c"):
                store.set_cooldown(f"opencode/{name}", "synthetic recovery", seconds=0, now=0)
        def resumed(request):
            assert "source-receipt=source fact" in request.prompt
            return verify(request)
        runtime.script.append(resumed)
        await service.resume(repo.job["research_id"])
        return await service.run_once()

    done = asyncio.run(scenario())
    assert done["status"] == "completed"
    assert len(reads) == 1 and len(runtime.requests) == 5
    assert done["result"]["usage"]["model_calls"] == 5
    assert done["result"]["usage"]["estimated_micro_usd"] == 0


def test_paid_flag_is_strict_and_free_and_paid_requests_have_distinct_identity():
    request = ResearchJobRequest(project_key="jardal-openceo", purpose="brief", mode="deep", question="Public fact?")
    assert request.deep_paid_opt_in is False
    assert request.fingerprint() != request.model_copy(update={"deep_paid_opt_in": True}).fingerprint()
    with pytest.raises(ValueError):
        ResearchJobRequest(**{**request.model_dump(), "deep_paid_opt_in": "false"})


def test_zero_paid_budget_admits_free_but_fences_paid_opt_in():
    async def scenario():
        repo = _FakeRepo()
        s = settings(RESEARCH_PAID_SPEND_USD="0.00")
        service = ResearchService(settings=s, deps=ResearchDependencies(repo=repo))
        request = ResearchJobRequest(project_key="jardal-openceo", purpose="brief", mode="deep", question="Public fact?")
        job = await service.submit(request, principal=PRINCIPAL, project_cfg=config())
        assert job["paid_budget_micro_usd"] == 0 and job["deep_paid_opt_in"] is False
        with pytest.raises(ResearchPolicyError):
            await service.submit(request.model_copy(update={"deep_paid_opt_in": True}), principal=PRINCIPAL, project_cfg=config())
        assert len(repo.jobs) == 1
    asyncio.run(scenario())


def test_http_admission_and_public_status_use_free_by_default(local_auth):
    async def scenario():
        repo = _FakeRepo()
        service = ResearchService(settings=settings(RESEARCH_PAID_SPEND_USD="0.00"), deps=ResearchDependencies(repo=repo))
        service._deps.provider_status = {"deep-free": {"configured": True, "mounted": True, "verified": None}}
        async with _client(_app(service, project_cfg=config())) as client:
            client.cookies.update(_cookie("usr_owner"))
            payload = {"project_key": "jardal-openceo", "purpose": "brief", "mode": "deep", "question": "Public fact?"}
            admitted = await client.post("/api/research/jobs", json=payload)
            assert admitted.status_code == 202
            rid = admitted.json()["research_id"]
            status = await client.get(f"/api/research/jobs/{rid}")
            assert status.status_code == 200
            assert status.json()["provider_status"]["deep-free"]["verified"] is None
            assert repo.jobs[rid]["deep_paid_opt_in"] is False
            denied = await client.post("/api/research/jobs", json={**payload, "deep_paid_opt_in": True})
            assert denied.status_code == 403 and len(repo.jobs) == 1
    asyncio.run(scenario())


def test_free_jobs_share_two_call_slots_and_release_them(free_setup):
    from research.service import JobSlots
    slots, peak = JobSlots(), 0

    async def slow(request):
        nonlocal peak
        peak = max(peak, slots.calls_in_use)
        await asyncio.sleep(0.01)
        return draft(request) if phase_payload(request)["task"] == "researcher" else verify(request)

    services = []
    for number in range(3):
        service, repo, _, _ = build(free_setup, [slow, slow])
        repo.job["research_id"] += str(number)
        service._deps.slots = slots
        services.append(service)
    async def scenario():
        return await asyncio.gather(*(service.run_once() for service in services))
    results = asyncio.run(scenario())
    assert all(row["status"] == "completed" for row in results)
    assert peak == 2 and slots.calls_in_use == slots.jobs_in_use == 0


def test_free_deep_tool_conflict_blocks_resume_and_reported_cost_is_uncertain(free_setup):
    tool = {"type": "tool_use", "part": {"id": "unconfirmed-tool", "tool": "research_read"}}
    service, repo, runtime, _ = build(free_setup, [fail(503, events=[tool])])
    blocked = asyncio.run(service.run_once())
    assert blocked["status"] == "blocked" and "BLOCKED_TOOL_CONFLICT" in blocked["failure_reason"]
    with pytest.raises(ResearchServiceError, match="reconciled"):
        asyncio.run(service.resume(repo.job["research_id"]))
    assert len(runtime.requests) == 1
    paid_receipt = RuntimeOutcome("{}", "ses_unexpected", events=[{"type": "step_finish", "part": {"cost": 0.001}}])
    service, _, runtime, _ = build(free_setup, [paid_receipt])
    blocked = asyncio.run(service.run_once())
    assert blocked["status"] == "blocked" and "nonzero cost" in blocked["failure_reason"]
    assert blocked["deep_execution"]["estimated_micro_usd"] is None
    assert blocked["deep_execution"]["uncertain"] is True and len(runtime.requests) == 1


def test_paid_adapter_requires_opt_in_before_reservation_or_http():
    async def scenario():
        ledger = MemoryLedger()
        requests = []
        def handler(request):
            assert ledger.rows
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            model = BudgetedDeepModel(settings(), ledger, client=client)
            job = {"research_id": "res_paid", "project_key": "jardal-openceo", "classification": "public", "paid_budget_micro_usd": 10_000}
            for flag in (None, False, "true", 1):
                with pytest.raises(DeepModelError, match="paid_opt_in_required"):
                    await model.run_phase(job={**job, "deep_paid_opt_in": flag}, phase="researcher", prompt="{}", seconds=2)
            assert not ledger.rows and not requests
            await model.run_phase(job={**job, "deep_paid_opt_in": True}, phase="researcher", prompt="{}", seconds=2)
            assert len(ledger.rows) == len(requests) == 1
    asyncio.run(scenario())


def test_free_provider_mount_needs_runtime_but_never_paid_ledger():
    s = settings(RESEARCH_FREE_ROUTER_ENABLED=True, RESEARCH_OPENCODE_CLI_PATH="opencode")
    before = _provider_status(s, ledger=None, discover=None, firecrawl=None, deep_model=None)
    assert before["deep-free"] == {"configured": True, "mounted": False, "verified": None}
    after = _provider_status(s, ledger=None, discover=None, firecrawl=None, deep_model=None, runtime=object())
    assert after["deep-free"] == {"configured": True, "mounted": True, "verified": None}
