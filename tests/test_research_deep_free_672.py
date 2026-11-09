"""Receipt-only regressions for schema, quote fidelity and free phase deadlines."""
import asyncio
import hashlib
import importlib.util
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON
from bson.codec_options import CodecOptions

from model_routing import research_wiring as wiring
from model_routing.paths import catalog_path, rankings_path
from research import deep
from research.runtimes.base import RuntimeOutcome, RuntimeUnavailable
from research.service import StepBudget, extract_report_payload
from test_model_routing_research import _entry
from test_model_routing_research_managed import _catalog_entry, _deployment
from test_research_deep_free_662 import build, draft, phase_payload, verify
from test_research_free_profile_648 import setup as free_setup  # noqa: F401


def correction(request):
    return RuntimeOutcome(json.dumps(phase_payload(request)["draft"]), "ses_correction")


def test_prose_verifier_gets_one_fresh_retry_and_zero_paid_effects(free_setup):
    def retry(request):
        assert phase_payload(request)["format_error"]
        return verify(request)

    service, repo, runtime, _ = build(free_setup, [draft, RuntimeOutcome("**Hecho 1:** supported", "ses_bad"), retry])
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    assert done["result"]["claims"][0]["verification"] == "supported"
    assert len(runtime.requests) == done["result"]["usage"]["model_calls"] == 3
    assert all(r.session_id is None and not r.tool_credential for r in runtime.requests)
    assert repo.job["free_research"]["format_attempts"]["verifier"] == 2
    assert done["result"]["usage"]["estimated_micro_usd"] == 0
    service._deps.ledger.reserve.assert_not_awaited()
    service._deps.deep_model.run_phase.assert_not_awaited()


@pytest.mark.parametrize("text", ["**Hecho 1:** supported", '{}', '{"verdicts": "supported"}',
                                  '{"verdicts":[{"verdict":"supported","claim_digest":[]}]}'])
def test_malformed_verifier_exhausts_once_and_never_certifies(free_setup, text):
    service, repo, runtime, _ = build(free_setup, [draft, RuntimeOutcome(text, "ses_bad"), RuntimeOutcome(text, "ses_bad")])
    done = asyncio.run(service.run_once())
    assert done["status"] == "partial"
    assert done["result"]["claims"][0]["verification"] == "insufficient_evidence"
    assert "verifier" not in repo.job["free_research"]["deep_phases"]
    assert len(runtime.requests) == 3 and not done["deep_execution"]["verification_history"]
    assert service.slot_snapshot()["calls_in_use"] == 0


def test_wrapped_verdict_json_preserves_braces_inside_quotes(free_setup):
    def wrapped(request):
        payload = json.loads(verify(request).text)
        payload["verdicts"][0]["reason"] = 'Source says "a } b { c"'
        return RuntimeOutcome('```json\n' + json.dumps(payload) + '\n```', "ses_json")

    service, _, runtime, _ = build(free_setup, [draft, wrapped])
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed" and len(runtime.requests) == 2
    assert extract_report_payload('Preface {"summary":"draft"}\n' + wrapped(runtime.requests[-1]).text,
                                  required_key="verdicts")["verdicts"]


@pytest.mark.parametrize("failure", ["malformed", "supported"])
def test_stitched_quote_is_removed_even_after_verifier_failure(free_setup, failure):
    def stitched(request):
        payload = json.loads(draft(request).text)
        payload["claims"][0]["support"] = [{"extract": "source fact"}]
        payload["claims"][0]["verification"] = "supported"
        return RuntimeOutcome(json.dumps(payload), "ses_stitched")

    def valid_verdict(request):
        payload = json.loads(verify(request).text)
        payload["verdicts"][0]["extract"] = "source"
        return RuntimeOutcome(json.dumps(payload), "ses_verifier")

    script = [stitched] + ([RuntimeOutcome("**Hecho 1:** supported", "ses_bad")] * 2 if failure == "malformed"
                          else [valid_verdict, correction, valid_verdict])
    service, _, _, _ = build(free_setup, script)

    async def read(**kwargs):
        return {"url": kwargs["url"], "text": "source intervening text fact"}

    service._deps.read = read
    done = asyncio.run(service.run_once())
    assert done["status"] == "partial"
    claim = done["result"]["claims"][0]
    assert claim["verification"] == "insufficient_evidence" and claim["support"] == []


def test_correction_can_replace_stitched_quote_with_separate_contiguous_quotes(free_setup):
    def fixed(request):
        payload = phase_payload(request)["draft"]
        payload["claims"][0]["support"] = [{"extract": "source"}, {"extract": "fact"}]
        return RuntimeOutcome(json.dumps(payload), "ses_fixed")

    def checked(request):
        payload = json.loads(verify(request).text)
        payload["verdicts"][0]["extract"] = "source"
        return RuntimeOutcome(json.dumps(payload), "ses_checked")

    service, _, _, _ = build(free_setup, [draft, checked, fixed, checked])

    async def read(**kwargs):
        return {"url": kwargs["url"], "text": "source intervening text fact"}

    service._deps.read = read
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    assert done["result"]["claims"][0]["verification"] == "supported"
    assert len(done["result"]["claims"][0]["support"]) == 2


def test_duplicate_claim_ids_cannot_transfer_a_supported_verdict(free_setup):
    def duplicate_ids(request):
        payload = json.loads(draft(request).text)
        payload["claims"].append({**payload["claims"][0], "statement": "unverified inference"})
        return RuntimeOutcome(json.dumps(payload), "ses_draft")

    def checked(request):
        payload = json.loads(verify(request).text)
        payload["verdicts"][0]["verdict"] = "insufficient_evidence"
        return RuntimeOutcome(json.dumps(payload), "ses_checked")

    service, _, _, _ = build(free_setup, [duplicate_ids, checked, correction, checked])
    done = asyncio.run(service.run_once())
    assert done["status"] == "partial"
    assert [c["verification"] for c in done["result"]["claims"]] == ["insufficient_evidence", "supported"]


@pytest.mark.parametrize("quote,expected", [("source fact", "supported"), ("source\t  fact", "insufficient_evidence")])
def test_quotes_match_the_canonical_persisted_text(free_setup, quote, expected):
    def quoted(request):
        payload = json.loads(draft(request).text)
        assert payload["claims"] and phase_payload(request)["evidence"][0]["text"] == "source fact"
        payload["claims"][0]["support"][0]["extract"] = quote
        return RuntimeOutcome(json.dumps(payload), "ses_draft")

    service, _, _, _ = build(free_setup, [quoted, verify, correction, verify])

    async def read(**kwargs):
        return {"url": kwargs["url"], "text": "source\t  fact"}

    service._deps.read = read
    done = asyncio.run(service.run_once())
    assert done["result"]["claims"][0]["verification"] == expected
    if expected == "insufficient_evidence":
        assert done["result"]["claims"][0]["support"] == []


def test_free_deep_uses_the_scoped_blob_instead_of_transient_page_text(free_setup):
    service, repo, _, _ = build(free_setup, [draft, verify])
    stored = SimpleNamespace(content="source fact", content_hash="receipt-hash", expires_at=None)
    service._deps.evidence = SimpleNamespace(asave=AsyncMock(return_value=stored), aget=AsyncMock(return_value=stored))

    async def read(**kwargs):
        return {"url": kwargs["url"], "text": "transient page text"}

    service._deps.read = read
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed" and done["result"]["claims"][0]["verification"] == "supported"
    scope, eid = service._deps.evidence.aget.await_args.args
    assert (scope.project_key, scope.owner_id, eid) == (repo.job["project_key"], repo.job["owner_id"], repo.job["research_id"] + "-e1")
    assert done["result"]["evidence"][0]["content_hash"] == "receipt-hash"


def test_missing_blob_blocks_and_resume_rechecks_it_without_acquisition(free_setup):
    service, repo, runtime, reads = build(free_setup, [])
    stored = SimpleNamespace(content="source fact", content_hash="receipt-hash", expires_at=None)
    service._deps.evidence = SimpleNamespace(asave=AsyncMock(return_value=stored), aget=AsyncMock(return_value=None))

    async def scenario():
        blocked = await service.run_once()
        assert blocked["status"] == "blocked" and not runtime.requests
        service._deps.evidence.aget.return_value = stored
        runtime.script[:] = [draft, verify]
        await service.resume(repo.job["research_id"])
        return await service.run_once()

    done = asyncio.run(scenario())
    assert done["status"] == "completed" and len(reads) == 1
    assert service._deps.evidence.aget.await_count == 2


def test_retry_survives_provider_block_without_replaying_researcher(free_setup):
    service, repo, runtime, reads = build(free_setup, [draft, RuntimeOutcome("Prose", "ses_bad"), RuntimeUnavailable("offline")])

    async def scenario():
        blocked = await service.run_once()
        assert blocked["status"] == "blocked"
        runtime.script.append(verify)
        await service.resume(repo.job["research_id"])
        return await service.run_once()

    done = asyncio.run(scenario())
    assert done["status"] == "completed" and len(reads) == 1
    assert len(runtime.requests) == 4 and done["result"]["usage"]["model_calls"] == 4


@pytest.mark.parametrize("phase", list(deep.FREE_DEEP_PHASE_SECONDS))
def test_prompt_drift_resets_attempts_once_and_preserves_retry_on_resume(free_setup, phase):
    service, repo, runtime, _ = build(free_setup, [])
    verifier = phase.startswith("verifier")
    limit = 2 if verifier else 1
    text = json.dumps({"verdicts" if verifier else "claims": []})
    payload = {"schema": {}, "evidence": [{"observed_at": datetime(2026, 11, 9, microsecond=123456,
                                                                 tzinfo=timezone.utc)}]}
    original = json.dumps(payload, default=str)
    retained = BSON(BSON.encode(payload)).decode(codec_options=CodecOptions(tz_aware=True, tzinfo=timezone.utc))
    assert retained["evidence"][0]["observed_at"].microsecond == 123000
    prompt = json.dumps(retained, default=str)
    assert original != prompt
    # A checkpoint from before attempt digests existed must also resume safely.
    repo.job["free_research"] = {"phase": phase, "format_attempts": {phase: limit},
        "deep_phases": {phase: {"digest": hashlib.sha256(original.encode()).hexdigest(), "text": text}}}
    model = deep.FreeDeepModel(service, repo.job, None, StepBudget(steps=10, seconds=60), {}, [])

    def prose(request):
        assert "format_error" not in phase_payload(request)
        return RuntimeOutcome("Prose", "ses_bad")

    def success(request):
        assert ("format_error" in phase_payload(request)) == verifier
        return RuntimeOutcome(text, "ses_valid")

    runtime.script[:] = ([prose] if verifier else []) + [RuntimeUnavailable("offline"), success]

    async def scenario():
        try:
            cached = await model.run_phase(job=repo.job, phase=phase, prompt=original, seconds=60)
            assert cached.usage["cached"] and not runtime.requests
            with pytest.raises(wiring.SessionBlocked):
                await model.run_phase(job=repo.job, phase=phase, prompt=prompt, seconds=60)
        finally:
            model.close()
        job = await repo.get_research_job(repo.job["research_id"])
        assert job["free_research"]["format_attempts"][phase] == int(verifier)
        resumed = deep.FreeDeepModel(service, job, None,
            StepBudget(steps=job["free_research"]["steps_left"], seconds=60), {}, [])
        try:
            outcome = await resumed.run_phase(job=job, phase=phase, prompt=prompt, seconds=60)
            assert outcome.text == text
            cached = await resumed.run_phase(job=job, phase=phase, prompt=prompt, seconds=60)
            assert cached.usage["cached"]
        finally:
            resumed.close()

    asyncio.run(scenario())
    assert repo.job["free_research"]["format_attempts"][phase] == limit
    assert len(runtime.requests) == limit + 1


@pytest.mark.parametrize("phase", list(deep.FREE_DEEP_PHASE_SECONDS))
def test_phase_caps_bound_runtime_and_release_slots_on_timeout(free_setup, monkeypatch, phase):
    service, repo, runtime, _ = build(free_setup, [])
    budget = StepBudget(steps=10, seconds=900)
    evidence = {"e1": {"text": "source fact", "url": "https://example.org/source"}}
    model = deep.FreeDeepModel(service, repo.job, None, budget, evidence, [])
    caps = dict(deep.FREE_DEEP_PHASE_SECONDS)
    caps[phase] = 0.02
    monkeypatch.setattr(deep, "FREE_DEEP_PHASE_SECONDS", caps)

    async def slow(request):
        await asyncio.Event().wait()

    runtime.script.append(slow)

    async def scenario():
        try:
            with pytest.raises(TimeoutError):
                await model.run_phase(job=repo.job, phase=phase, prompt='{"schema":{}}', seconds=900)
        finally:
            model.close()

    asyncio.run(scenario())
    assert len(runtime.requests) == 1 and service.slot_snapshot()["calls_in_use"] == 0
    assert model.state["deep_model_calls"] == 1


def test_verifier_timeout_preserves_unverified_claims_and_releases_resources(free_setup, monkeypatch):
    async def slow(request):
        await asyncio.Event().wait()

    service, _, runtime, _ = build(free_setup, [draft, slow])
    caps = {**deep.FREE_DEEP_PHASE_SECONDS, "verifier": 0.02}
    monkeypatch.setattr(deep, "FREE_DEEP_PHASE_SECONDS", caps)
    done = asyncio.run(service.run_once())
    assert done["status"] == "partial" and len(runtime.requests) == 2
    assert done["result"]["claims"][0]["verification"] == "insufficient_evidence"
    assert any("verifier" in gap["detail"] and "TimeoutError" in gap["detail"] for gap in done["coverage_gaps"])
    assert service.slot_snapshot()["calls_in_use"] == service.slot_snapshot()["jobs_in_use"] == 0


def test_format_retry_uses_remaining_phase_time(free_setup):
    service, repo, runtime, _ = build(free_setup, [])
    budget = StepBudget(steps=10, seconds=900)
    model = deep.FreeDeepModel(service, repo.job, None, budget, {}, [])

    def prose(request):
        assert request.timeout_seconds <= 90
        model.phase_deadline -= 100
        return RuntimeOutcome("**Hecho 1:** supported", "ses_prose")

    def retry(request):
        assert request.timeout_seconds <= 20 and request.session_id is None
        return RuntimeOutcome('{"verdicts":[]}', "ses_json")

    runtime.script[:] = [prose, retry]

    async def scenario():
        try:
            return await model.run_phase(job=repo.job, phase="verifier", prompt='{"schema":{}}', seconds=900)
        finally:
            model.close()

    assert json.loads(asyncio.run(scenario()).text) == {"verdicts": []}
    assert budget.steps_left == 8 and model.state["deep_model_calls"] == 2


def test_free_caps_allow_slow_success_and_keep_requested_smaller_deadline(free_setup):
    service, repo, runtime, _ = build(free_setup, [draft, verify])
    repo.job["effective_limits"]["seconds"] = 900
    now = [service._clock()]
    service._clock = lambda: now[0]

    def slow_draft(request):
        assert request.timeout_seconds <= deep.FREE_DEEP_PHASE_SECONDS["researcher-1"]
        now[0] += timedelta(seconds=80)
        return draft(request)

    def slow_verify(request):
        assert request.timeout_seconds <= 90
        now[0] += timedelta(seconds=80)
        return verify(request)

    runtime.script[:] = [slow_draft, slow_verify]
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed"
    service, repo, runtime, _ = build(free_setup, [draft, verify])
    repo.job["research_id"] += "_short"
    repo.job["effective_limits"]["seconds"] = 5
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert all(request.timeout_seconds <= 5 for request in runtime.requests)


def test_free_total_cap_and_paid_probe_deadline_remain_explicit(free_setup, monkeypatch):
    from research import service as service_module

    budgets = []

    def budget(**kwargs):
        budgets.append(kwargs["seconds"])
        return StepBudget(**kwargs)

    monkeypatch.setattr(service_module, "StepBudget", budget)
    service, repo, _, _ = build(free_setup, [draft, verify])
    repo.job["effective_limits"]["seconds"] = 900
    assert asyncio.run(service.run_once())["status"] == "completed"
    assert budgets == [deep.FREE_DEEP_SECONDS]

    from test_research_deep import Model
    service, repo, _, _ = build(free_setup, [])
    repo.job["research_id"] += "_paid"
    repo.job["deep_paid_opt_in"] = True
    repo.job["effective_limits"]["seconds"] = 900
    service._deps.deep_model = Model()
    assert asyncio.run(service.run_once())["status"] == "partial"
    assert budgets[-1] == 900

    spec = importlib.util.spec_from_file_location("probe", Path(__file__).resolve().parents[1] / "scripts/research_deep_probe.py")
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe._probe_limits(False)["seconds"] == deep.FREE_DEEP_SECONDS
    assert probe._probe_limits(True)["seconds"] == 120


def test_native_first_honors_order_and_external_only_follows_unavailability(free_setup):
    service, _, runtime, _ = build(free_setup, [draft, verify])
    catalog = json.loads(catalog_path().read_text())
    catalog["entries"].append(_catalog_entry("family-d", "d", "d",
        _deployment("groq/d", "groq", "external_free", "groq", "d")))
    catalog_path().write_text(json.dumps(catalog))
    ranks = json.loads(rankings_path().read_text())
    order = ranks["profiles"]["free-research"]["order"]
    order.insert(0, _entry(1, "d", "family-d", "groq/d", provider="groq"))
    for rank, entry in enumerate(order, 1):
        entry["rank"] = rank
    rankings_path().write_text(json.dumps(ranks))
    with closing(wiring.AvailabilityStore()) as store:
        for name in ("a", "b", "c"):
            store.set_cooldown(f"opencode/{name}", "synthetic unavailable", seconds=600)
    done = asyncio.run(service.run_once())
    assert done["status"] == "completed" and [r.model for r in runtime.requests] == ["groq/d"] * 2
    saved = wiring.load_checkpoint(wiring.checkpoints_dir(), done["research_id"])
    assert saved.candidates[0].deployment_id == "opencode/a"
