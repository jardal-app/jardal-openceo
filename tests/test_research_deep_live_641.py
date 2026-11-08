"""Deep admission, provider authority and budget enforcement without network."""

import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from config_loader import ConfigLoader
from research.acquisition.base import (
    BudgetRefusedError, DiscoveryItem, DiscoveryResult, ProviderDisabledError,
)
from research.acquisition.serper import SerperClient
from research.budget import PaidBudgetDisabled, Reservation, ReserveDecision, ResearchPolicyLedger
from research.contracts import ResearchJobRequest, ResearchLimits
from research.deep_model import BudgetedDeepModel, DeepModelError, PhaseOutcome
from research.policies import Principal, ResearchPolicyError, derive_effective_policy
from research.provider_credits import ProviderCredits
from research.router import public_job_payload
from research.service import ResearchDependencies, ResearchService
from research.wiring import _build_discovery, _provider_status, build_research_service
from settings import Settings
from test_research_facade import _FakeRepo, _app, _client, _cookie


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def local_auth(monkeypatch):
    s = Settings(_env_file=None, IDENTITY_PROVIDER="local", JWT_SECRET="synthetic-test-key-for-offline-641")
    monkeypatch.setattr("settings.get_settings", lambda: s)
    monkeypatch.setattr("auth.jwt._get_settings", lambda: s)


PROJECT = "jardal-openceo"
PRINCIPAL = Principal(user_id="usr_owner", permissions=frozenset({"run_research", "read_research"}),
                      allowed_projects=frozenset({PROJECT}))


def settings(**changes):
    return Settings(_env_file=None, **{
        "RESEARCH_ENABLED": True, "RESEARCH_PAID_SPEND_USD": "2.00",
        "RESEARCH_DEEP_ACCOUNT_CEILING_USD": "2.00", "RESEARCH_DEEP_API_KEY": "synthetic",
        "RESEARCH_DEEP_MODEL": "explicit", "RESEARCH_DEEP_INPUT_USD_PER_MILLION": "0.10",
        "RESEARCH_DEEP_OUTPUT_USD_PER_MILLION": "0.20", "RESEARCH_SERPER_API_KEY": "synthetic",
        "RESEARCH_SERPER_ACCOUNT_CREDIT_QUERIES": "1", "RESEARCH_FIRECRAWL_API_KEY": "",
        "RESEARCH_EVIDENCE_DIR": "", "RESEARCH_OPENCODE_CLI_PATH": "", **changes})


def config():
    return ConfigLoader(settings()).load_project(PROJECT)


def request(**changes):
    return ResearchJobRequest(**{"project_key": PROJECT, "purpose": "brief", "mode": "deep",
                                "question": "Revenue?", **changes})


class MemoryLedger:
    def __init__(self):
        self.rows = {}

    async def reserve(self, **kw):
        key = (kw["provider"], kw["operation_id"])
        if key in self.rows:
            return ReserveDecision(True, Reservation(**self.rows[key][0], reused=True))
        rows = [row for (provider, _), row in self.rows.items() if provider == kw["provider"]]
        for field, value, limit in ((None, None, kw.get("ceiling_micro_usd")),
                ("project_key", kw["project_key"], kw.get("project_limit_micro_usd")),
                ("research_id", kw["research_id"], kw.get("job_limit_micro_usd"))):
            used = sum(amount for row, amount in rows if field is None or row[field] == value)
            if limit is not None and used + kw["amount_micro_usd"] > limit:
                return ReserveDecision(False, reason="limit")
        row = {name: kw[name] for name in ("provider", "operation_id", "project_key", "research_id", "amount_micro_usd")}
        row["account_id"] = kw.get("account", "default")
        self.rows[key] = (row, kw["amount_micro_usd"])
        return ReserveDecision(True, Reservation(**row))

    async def commit(self, reservation, *, charged_micro_usd):
        key = (reservation.provider, reservation.operation_id)
        row, _ = self.rows[key]
        self.rows[key] = (row, charged_micro_usd)

    async def mark_uncertain(self, reservation):
        return None

    async def release(self, reservation):
        self.rows.pop((reservation.provider, reservation.operation_id), None)


class Phases:
    controlled_calls = True

    def __init__(self):
        self.prompts = []

    async def run_phase(self, *, job, phase, prompt, seconds):
        payload = json.loads(prompt)
        self.prompts.append((phase, payload))
        if phase.startswith("researcher"):
            result = {"summary": "Revenue", "claims": [{"id": "revenue", "statement": "Revenue is 10",
                "kind": "fact", "evidence_ids": [payload["evidence"][0]["id"]],
                "support": [{"extract": "Revenue is 10"}]}]}
        else:
            result = {"verdicts": [{"claim_digest": c["claim_digest"], "verdict": "supported",
                "evidence_id": c["evidence_ids"][0], "extract": "Revenue is 10", "reason": "Checked source"}
                for c in payload["claims"]]}
        return PhaseOutcome(json.dumps(result), estimated_micro_usd=1, usage={"simulated": True})


async def admitted(repo, **changes):
    service = ResearchService(settings=settings(), deps=ResearchDependencies(repo=repo))
    return await service.submit(request(**changes), principal=PRINCIPAL, project_cfg=config())


@pytest.mark.anyio
async def test_yaml_admits_deep_and_caps_limits_at_server_project_and_request():
    cfg = config()
    assert cfg["research"]["paid_budget_usd"] == "2.00"
    assert cfg["research"]["read_provider"] == "http"
    assert cfg["research"]["adapters"]["serper"] and cfg["research"]["adapters"]["firecrawl"]
    s = settings(RESEARCH_PAID_SPEND_USD="0.30", RESEARCH_DEEP_MAX_PAGES=2)
    repo = _FakeRepo()
    service = ResearchService(settings=s, deps=ResearchDependencies(repo=repo))
    job = await service.submit(request(), principal=PRINCIPAL, project_cfg=cfg,
        effective_limits=ResearchLimits(search_requests=99, pages=99, steps=99, seconds=999))
    assert job["paid_budget_micro_usd"] == 300_000
    assert job["effective_limits"] == {"search_requests": 15, "pages": 2, "steps": 30, "seconds": 900}
    cfg["research"]["paid_budget_usd"] = "0.10"
    policy = derive_effective_policy(principal=PRINCIPAL, request=request(), project_cfg=cfg, settings=s)
    assert policy.paid_budget_micro_usd == 100_000
    assert not s.RESEARCH_SCHEDULER_ENABLED and not s.RESEARCH_AUTO_TOPUPS


@pytest.mark.anyio
@pytest.mark.parametrize("side", ["server", "project"])
async def test_zero_budget_denies_deep_before_persistence(side):
    cfg, s, repo = config(), settings(), _FakeRepo()
    if side == "server":
        s.RESEARCH_PAID_SPEND_USD = "0.00"
    else:
        cfg["research"]["paid_budget_usd"] = "0.00"
    service = ResearchService(settings=s, deps=ResearchDependencies(repo=repo))
    with pytest.raises(ResearchPolicyError):
        await service.submit(request(), principal=PRINCIPAL, project_cfg=cfg)
    assert not repo.jobs
    await service.submit(request(mode="quick"), principal=PRINCIPAL, project_cfg=cfg)


@pytest.mark.anyio
async def test_shared_paid_reservation_survives_commit_and_another_instance():
    repo, ledger = _FakeRepo(), MemoryLedger()
    job = await admitted(repo)
    first, second = (ResearchPolicyLedger(ledger, repo, 2_000_000, 2_000_000) for _ in range(2))
    kw = dict(provider="deepseek-research", amount_micro_usd=1_500_000, operation_id="researcher",
              project_key=PROJECT, research_id=job["research_id"], ceiling_micro_usd=2_000_000)
    decision = await first.reserve(**kw)
    assert decision.accepted
    await first.commit(decision.reservation, charged_micro_usd=1)
    other = await second.reserve(**{**kw, "provider": "serper", "operation_id": "search", "amount_micro_usd": 600_000})
    assert not other.accepted
    assert not (await second.reserve(**kw)).accepted
    assert not any(provider == "serper" for provider, _ in ledger.rows)


@pytest.mark.anyio
async def test_existing_account_spend_is_not_reset_and_effective_budget_is_enforced():
    repo, ledger = _FakeRepo(), MemoryLedger()
    job = await admitted(repo)
    await ledger.reserve(provider="deepseek-research", operation_id="legacy", project_key=PROJECT,
        research_id="old-job", amount_micro_usd=1_999_999, ceiling_micro_usd=2_000_000)
    policy = ResearchPolicyLedger(ledger, repo, 2_000_000, 2_000_000)
    kw = dict(provider="serper", amount_micro_usd=1000, operation_id="search", project_key=PROJECT,
              research_id=job["research_id"], ceiling_micro_usd=100_000)
    assert not (await policy.reserve(**kw)).accepted
    ledger.rows.clear()
    repo.jobs[job["research_id"]]["paid_budget_micro_usd"] = 999
    assert not (await policy.reserve(**kw)).accepted
    for update in ({"project_key": "other"}, {"classification": "restricted"}, {"adapters": {}}):
        original = deepcopy(repo.jobs[job["research_id"]])
        repo.jobs[job["research_id"]].update(update)
        with pytest.raises(PaidBudgetDisabled):
            await policy.reserve(**kw)
        repo.jobs[job["research_id"]] = original


@pytest.mark.anyio
async def test_model_failure_has_one_http_attempt_and_keeps_the_shared_reservation():
    repo, ledger = _FakeRepo(), MemoryLedger()
    job = await admitted(repo)
    seen = []
    async def timeout(req):
        assert any(provider == "deepseek-research" for provider, _ in ledger.rows)
        seen.append(req.url)
        raise httpx.ReadTimeout("synthetic timeout")
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        model = BudgetedDeepModel(settings(), ResearchPolicyLedger(ledger, repo, 2_000_000, 2_000_000), client=client)
        with pytest.raises(DeepModelError):
            await model.run_phase(job=job, phase="researcher-1", prompt="{}", seconds=1)
    assert len(seen) == 1 and sum(amount for _, amount in ledger.rows.values()) > 0


@pytest.mark.anyio
async def test_serper_debits_native_credits_before_http_and_does_not_refund_failure():
    db = SimpleNamespace(research_provider_credits=SimpleNamespace(update_one=AsyncMock(
        side_effect=[SimpleNamespace(), SimpleNamespace(matched_count=1), SimpleNamespace(), SimpleNamespace(matched_count=0)])),
        research_provider_credit_usage=SimpleNamespace(insert_one=AsyncMock(), update_one=AsyncMock()))
    quota = ProviderCredits(db, provider="serper", account="default", capacity=1, window="nonrenewing")
    seen = []
    async def timeout(req):
        assert quota.usage.insert_one.await_count == 1
        seen.append(req.url)
        raise httpx.ReadTimeout("synthetic timeout")
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        serper = SerperClient(settings=settings(), http_client=client, credit_budget=quota, max_attempts=1)
        result = await serper.search_web("public", project_key=PROJECT, research_id="job")
        assert result.degradations
        with pytest.raises(BudgetRefusedError):
            await serper.search_web("public", project_key=PROJECT, research_id="job")
    assert len(seen) == 1
    assert quota.usage.insert_one.call_args.args[0]["state"] == "uncertain"
    quota.usage.update_one.assert_not_awaited()


@pytest.mark.anyio
async def test_discovery_uses_persisted_grants_and_never_fails_over():
    repo = _FakeRepo()
    job = await admitted(repo)
    brave = SimpleNamespace(search_web=AsyncMock(return_value=DiscoveryResult(provider="brave")))
    serper = SimpleNamespace(search_web=AsyncMock(return_value=DiscoveryResult(provider="serper")))
    with patch("research.acquisition.brave.BraveClient", return_value=brave), \
            patch("research.acquisition.serper.SerperClient", return_value=serper):
        discover = _build_discovery(settings(RESEARCH_BRAVE_API_KEY="synthetic"), None, repo=repo)
    kw = dict(question="public", research_id=job["research_id"], project_key=PROJECT)
    assert (await discover(**kw, provider="serper")).provider == "serper"
    repo.jobs[job["research_id"]]["adapters"]["serper"] = False
    assert (await discover(**kw, provider="serper")).degradations
    forged = SimpleNamespace(project_key="other", adapters={"serper": True}, classification=SimpleNamespace(value="public"))
    assert (await discover(**{**kw, "project_key": "other"}, provider="serper", policy=forged)).degradations
    brave.search_web.assert_not_awaited()
    assert serper.search_web.await_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("fire_present", [False, True])
async def test_firecrawl_needs_persisted_read_grant_and_never_switches_to_http(fire_present):
    repo = _FakeRepo()
    job = await admitted(repo)
    repo.jobs[job["research_id"]]["read_provider"] = "firecrawl"
    http = AsyncMock()
    fire = SimpleNamespace(scrape=AsyncMock()) if fire_present else None
    with patch("research.wiring._build_http_reader", return_value=SimpleNamespace(provider="http", read=http)), \
            patch("research.wiring._build_firecrawl", return_value=fire):
        service, resources = build_research_service(settings(), repo)
    try:
        repo.jobs[job["research_id"]]["adapters"]["firecrawl"] = False
        with pytest.raises(ProviderDisabledError):
            await service._deps.read(url="https://example.org", research_id=job["research_id"], project_key=PROJECT)
        repo.jobs[job["research_id"]]["adapters"]["firecrawl"] = True
        if not fire_present:
            with pytest.raises(Exception, match="Firecrawl no montado"):
                await service._deps.read(url="https://example.org", research_id=job["research_id"], project_key=PROJECT)
        with pytest.raises(ProviderDisabledError):
            await service._deps.read(url="https://example.org", research_id=job["research_id"], project_key="other")
        http.assert_not_awaited()
    finally:
        await resources.aclose()


@pytest.mark.anyio
async def test_http_deep_cycle_exposes_status_and_firecrawl_gap_stays_partial():
    repo, model = _FakeRepo(), Phases()
    now = datetime.now(timezone.utc)
    fire = SimpleNamespace(scrape=AsyncMock(return_value=SimpleNamespace(degradations=(),
        final_url="https://example.org/source", text="Revenue is 10", observed_at=now, published_at=None)))
    with patch("research.wiring._build_http_reader", return_value=None), \
            patch("research.wiring._build_firecrawl", return_value=fire):
        service, resources = build_research_service(settings(), repo)
    service._deps.deep_model = model
    cfg = config()
    cfg["research"]["read_provider"] = "firecrawl"
    async with _client(_app(service, project_cfg=cfg)) as client:
        client.cookies.update(_cookie("usr_owner"))
        response = await client.post("/api/research/jobs", json={
            **request(seed_urls=["https://example.org/source"]).model_dump(mode="json"),
            "limits": {"search_requests": 0, "pages": 1, "steps": 8, "seconds": 10}})
        assert response.status_code == 202
        rid = response.json()["research_id"]
        before = await client.get(f"/api/research/jobs/{rid}")
        assert before.json()["provider_status"]["deep"]["verified"] is None
        repo.jobs[rid]["status"] = "running"
        with patch("research.policies.EgressGuard.validate", new_callable=AsyncMock) as guard:
            done = await service._execute(deepcopy(repo.jobs[rid]))
        assert guard.await_count == 2
        response = await client.get(f"/api/research/jobs/{rid}")
        payload = response.json()
        assert done["status"] == payload["status"] == payload["result"]["status"] == "partial"
        assert payload["provider_status"]["deep"]["verified"] is True
        assert payload["provider_status"]["firecrawl"]["verified"] is True
        assert any(gap["source"] == "firecrawl" for gap in payload["result"]["coverage_gaps"])
        assert payload["result"]["usage"]["reconciled_micro_usd"] is None
        assert "synthetic" not in json.dumps(payload)
    assert [phase for phase, _ in model.prompts] == ["researcher-1", "verifier"]
    assert "draft" not in model.prompts[1][1]
    assert service.slot_snapshot()["calls_in_use"] == 0
    await resources.aclose()


@pytest.mark.anyio
async def test_service_schedules_serper_as_complement_and_stops_on_failed_brave():
    repo = _FakeRepo()
    job = await admitted(repo, limits=ResearchLimits(search_requests=2, pages=3, steps=10, seconds=10))
    repo.jobs[job["research_id"]]["status"] = "running"
    async def discovery(**kw):
        return DiscoveryResult(provider=kw["provider"], items=(DiscoveryItem(
            url=f"https://example.org/{kw['provider']}", provider=kw["provider"], rank=1),))
    discover = AsyncMock(side_effect=discovery)
    service = ResearchService(settings=settings(), deps=ResearchDependencies(repo=repo, discover=discover))
    await service._execute(deepcopy(repo.jobs[job["research_id"]]))
    assert [call.kwargs["provider"] for call in discover.await_args_list] == ["brave", "serper"]
    discover.reset_mock(side_effect=True)
    discover.side_effect = ProviderDisabledError("unavailable", provider="brave")
    repo.jobs[job["research_id"]]["status"] = "running"
    await service._execute(deepcopy(repo.jobs[job["research_id"]]))
    assert discover.await_count == 1


def test_configured_mounted_and_verified_are_separate_and_never_contain_credentials():
    status = _provider_status(settings(), ledger=None, discover=None, firecrawl=None, deep_model=None)
    assert status["deep"] == {"configured": True, "mounted": False, "verified": None}
    status = _provider_status(settings(RESEARCH_DEEP_INPUT_USD_PER_MILLION="NaN"),
        ledger=object(), discover=SimpleNamespace(providers={"serper"}), firecrawl=None, deep_model=object())
    assert status["deep"] == {"configured": False, "mounted": True, "verified": None}
    assert public_job_payload({"provider_status": status})["provider_status"] == status
    assert "synthetic" not in json.dumps(status)


@pytest.mark.anyio
@pytest.mark.parametrize("steps,cancelled,expected_calls", [(2, False, 1), (8, True, 0)])
async def test_deep_obeys_shared_steps_and_cancellation_before_another_phase(steps, cancelled, expected_calls):
    repo, model = _FakeRepo(), Phases()
    reader = AsyncMock(return_value={"url": "https://example.org", "text": "Revenue is 10"})
    service = ResearchService(settings=settings(), deps=ResearchDependencies(repo=repo, read=reader, deep_model=model))
    job = await service.submit(request(seed_urls=["https://example.org"], limits=ResearchLimits(
        search_requests=0, pages=1, steps=steps, seconds=10)), principal=PRINCIPAL, project_cfg=config())
    repo.jobs[job["research_id"]]["status"] = "running"
    if cancelled:
        service._cancelled.add(job["research_id"])
    result = await service._execute(deepcopy(repo.jobs[job["research_id"]]))
    assert result["status"] == "partial"
    assert len(model.prompts) == expected_calls
    assert service.slot_snapshot()["calls_in_use"] == 0


@pytest.mark.anyio
async def test_wired_serper_without_ledger_or_native_credit_authority_never_sends():
    repo = _FakeRepo()
    job = await admitted(repo)
    with patch("research.acquisition.serper.SerperClient._send", new_callable=AsyncMock) as send:
        discover = _build_discovery(settings(), None, repo=repo)
        result = await discover(question="public", research_id=job["research_id"], project_key=PROJECT, provider="serper")
    assert result.degradations
    send.assert_not_awaited()
