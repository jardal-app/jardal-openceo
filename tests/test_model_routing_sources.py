"""Provider sources, free verification and probes (TASK-2026-00289). Offline only."""

from __future__ import annotations

import json
import urllib.error
from datetime import date, datetime, timedelta, timezone
from email.message import Message

import pytest
from pydantic import ValidationError

from model_routing import live as router_live
from model_routing import probes as router_probes
from model_routing import sources as router_sources
from model_routing import terms as router_terms
from model_routing.models import CatalogDocument


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _record(provider: str, **overrides):
    base = {
        "provider": provider,
        "free_tier": True,
        "requires_payment_method": False,
        "one_time_credit_only": False,
        "data_policy": "unknown",
        "retention": "unknown",
        "confidentiality_allowed": ["public"],
        "source": "https://example.test/console",
        "verified_at": _today(),
        "basis": "synthetic test record",
    }
    base.update(overrides)
    return router_terms.ProviderTerms(**base)


def _fresh_terms(per_provider: dict | None = None, free_ids: list[str] | None = None):
    per_provider = per_provider or {}
    terms = [
        _record(provider, **per_provider.get(provider, {}))
        for provider in router_sources.PROVIDERS
    ]
    free_list = router_terms.OpenCodeFreeList(
        source="https://example.test/models",
        verified_at=_today(),
        basis="synthetic",
        model_ids=free_ids if free_ids is not None else ["big-pickle", "beam-free"],
    )
    return router_terms.TermsFile(terms=terms, opencode_free_models=free_list)


def _fake_get(payload=None, *, code=None, retry_after=None, seen=None):
    def _get(url, headers):
        if seen is not None:
            seen.append((url, dict(headers)))
        if code is not None:
            headers_msg = Message()
            if retry_after is not None:
                headers_msg["Retry-After"] = retry_after
            raise urllib.error.HTTPError(url, code, f"HTTP {code}", headers_msg, None)
        assert isinstance(payload, dict)
        return payload

    return _get


def _fake_post(response=None, *, seen=None, error=None):
    def _post(url, headers, payload):
        if seen is not None:
            seen.append((url, dict(headers), payload))
        if error is not None:
            raise error
        assert isinstance(response, dict)
        return response

    return _post


def _key_payload(limit=1000, used=12, remaining=988, usage=10.0):
    return {
        "data": {
            "usage": usage,
            "free_model_daily_requests": {"limit": limit, "used": used, "remaining": remaining},
        }
    }


def _or_models(*items):
    return {"data": list(items)}


def _or_model(model_id, prompt="0", completion="0", **extra):
    pricing = {"prompt": prompt, "completion": completion}
    pricing.update(extra)
    return {"id": model_id, "pricing": pricing, "context_length": 131072}


# Terms records


def test_terms_versioned_file_has_five_providers_and_big_pickle():
    terms = router_terms.load_terms()
    assert sorted(entry.provider for entry in terms.terms) == sorted(router_sources.PROVIDERS)
    assert "big-pickle" in terms.opencode_free_models.model_ids
    anchor = date(2026, 11, 1)  # re-verification campaign TASK-2026-00427
    for entry in terms.terms:
        assert router_terms.is_fresh(entry.verified_at, terms.default_ttl_days, today=anchor)


def test_terms_unknown_provider_fails_closed():
    terms = _fresh_terms()
    with pytest.raises(router_terms.TermsError):
        router_terms.terms_for(terms, "cerebras")
    assert router_sources.is_terms_usable(terms, "cerebras") is False


def test_terms_rejects_missing_file(tmp_path):
    with pytest.raises(router_terms.TermsError):
        router_terms.load_terms(tmp_path / "absent.yaml")


def test_terms_rejects_duplicate_provider(tmp_path):
    target = tmp_path / "terms.yaml"
    target.write_text(
        "schema_version: 1\ndefault_ttl_days: 7\n"
        "terms:\n"
        "  - {provider: groq, free_tier: true, requires_payment_method: false,"
        " one_time_credit_only: false, data_policy: unknown, retention: unknown,"
        " confidentiality_allowed: [public], source: https://example.test,"
        " verified_at: '2026-10-23', basis: test}\n"
        "  - {provider: groq, free_tier: true, requires_payment_method: false,"
        " one_time_credit_only: false, data_policy: unknown, retention: unknown,"
        " confidentiality_allowed: [public], source: https://example.test,"
        " verified_at: '2026-10-23', basis: test}\n"
        "opencode_free_models: {source: https://example.test, verified_at: '2026-10-23',"
        " model_ids: []}\n",
        encoding="utf-8",
    )
    with pytest.raises(router_terms.TermsError):
        router_terms.load_terms(target)


def test_terms_rejects_bad_date():
    with pytest.raises(ValidationError):
        _record("groq", verified_at="yesterday")


def test_terms_freshness_boundaries():
    assert router_terms.is_fresh(_today(), 7) is True
    week_ago = (datetime.now(timezone.utc).date() - timedelta(days=7)).isoformat()
    assert router_terms.is_fresh(week_ago, 7) is True
    old = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
    assert router_terms.is_fresh(old, 7) is False
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    assert router_terms.is_fresh(tomorrow, 7) is False


# OpenRouter parsing


def test_openrouter_zero_price_free_suffix_and_quota_verifies():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model:free"))
    (entry,) = router_sources.parse_openrouter_models(raw, _key_payload(), terms)
    deployment = entry.deployments[0]
    assert deployment.free_offered is True
    assert deployment.free_verified is True
    assert deployment.input_price_usd == 0
    assert deployment.output_price_usd == 0
    assert deployment.deployment_id == "openrouter/vendor/model:free"
    assert entry.capabilities.context_tokens == 131072


def test_openrouter_nonzero_price_is_not_offered():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model:free", prompt="0.0001"))
    (entry,) = router_sources.parse_openrouter_models(raw, _key_payload(), terms)
    deployment = entry.deployments[0]
    assert deployment.free_offered is False
    assert deployment.free_verified is False


def test_openrouter_unknown_price_fails_closed():
    terms = _fresh_terms()
    raw = _or_models({"id": "vendor/model:free", "pricing": {"prompt": "n/a"}})
    (entry,) = router_sources.parse_openrouter_models(raw, _key_payload(), terms)
    deployment = entry.deployments[0]
    assert deployment.input_price_usd is None
    assert deployment.free_offered is False
    assert deployment.free_verified is False


def test_openrouter_free_suffix_removed_stays_unverified():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model"))
    (entry,) = router_sources.parse_openrouter_models(raw, _key_payload(), terms)
    deployment = entry.deployments[0]
    assert deployment.free_offered is True
    assert deployment.free_verified is False


def test_openrouter_nonzero_extra_price_blocks_verification():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model:free", web_search="0.01"))
    (entry,) = router_sources.parse_openrouter_models(raw, _key_payload(), terms)
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


def test_openrouter_exhausted_daily_quota_stays_unverified():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model:free"))
    (entry,) = router_sources.parse_openrouter_models(
        raw, _key_payload(used=1000, remaining=0), terms
    )
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


def test_openrouter_missing_key_check_stays_unverified():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model:free"))
    (entry,) = router_sources.parse_openrouter_models(raw, None, terms)
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


def test_openrouter_malformed_key_payload_stays_unverified():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model:free"))
    (entry,) = router_sources.parse_openrouter_models(raw, {"data": {}}, terms)
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


def test_openrouter_missing_data_list_is_parse_error():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.parse_openrouter_models({"error": "x"}, _key_payload(), _fresh_terms())
    assert exc_info.value.kind == "parse"


def test_openrouter_skips_entries_without_id():
    terms = _fresh_terms()
    raw = _or_models({"pricing": {"prompt": "0", "completion": "0"}}, _or_model("v/m:free"))
    entries = router_sources.parse_openrouter_models(raw, _key_payload(), terms)
    assert [item.canonical_model_id for item in entries] == ["openrouter/v/m:free"]


# OpenCode parsing


def _oc_models(*ids):
    return {"data": [{"id": model_id, "object": "model"} for model_id in ids]}


def test_opencode_free_without_suffix_verifies_with_serve_evidence():
    terms = _fresh_terms()
    raw = _oc_models("big-pickle")
    evidence = {"big-pickle": datetime.now(timezone.utc).isoformat()}
    (entry,) = router_sources.parse_opencode_models(
        raw, terms.opencode_free_models, terms, evidence
    )
    deployment = entry.deployments[0]
    assert deployment.free_offered is True
    assert deployment.free_verified is True
    assert deployment.input_price_usd == 0
    assert deployment.output_price_usd == 0


def test_opencode_unknown_id_fails_closed():
    terms = _fresh_terms()
    raw = _oc_models("mystery-model")
    evidence = {"mystery-model": datetime.now(timezone.utc).isoformat()}
    (entry,) = router_sources.parse_opencode_models(
        raw, terms.opencode_free_models, terms, evidence
    )
    deployment = entry.deployments[0]
    assert deployment.free_offered is False
    assert deployment.free_verified is False
    assert deployment.input_price_usd is None


def test_opencode_offered_without_evidence_stays_unverified():
    terms = _fresh_terms()
    raw = _oc_models("big-pickle")
    (entry,) = router_sources.parse_opencode_models(raw, terms.opencode_free_models, terms)
    deployment = entry.deployments[0]
    assert deployment.free_offered is True
    assert deployment.free_verified is False


def test_opencode_stale_evidence_stays_unverified():
    terms = _fresh_terms()
    raw = _oc_models("big-pickle")
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    (entry,) = router_sources.parse_opencode_models(
        raw, terms.opencode_free_models, terms, {"big-pickle": old}
    )
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


def test_opencode_naive_evidence_timestamp_fails_closed():
    terms = _fresh_terms()
    raw = _oc_models("big-pickle")
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    (entry,) = router_sources.parse_opencode_models(
        raw, terms.opencode_free_models, terms, {"big-pickle": naive}
    )
    deployment = entry.deployments[0]
    assert deployment.free_offered is True
    assert deployment.free_verified is False


def test_opencode_capabilities_come_from_list_metadata():
    terms = _fresh_terms()
    raw = {"data": [{
        "id": "big-pickle", "object": "model",
        "capabilities": {"tools": {"supported": True}},
        "limit": {"context": 200000},
    }]}
    (entry,) = router_sources.parse_opencode_models(
        raw, terms.opencode_free_models, terms)
    assert entry.capabilities.tools is True
    assert entry.capabilities.context_tokens == 200000


def test_opencode_capabilities_missing_or_malformed_stay_blank():
    terms = _fresh_terms()
    cases = [
        ({}, False, None),
        ({"capabilities": {"tools": {"supported": "yes"}}}, False, None),
        ({"capabilities": {"tools": {"supported": True}},
          "limit": {"context": -5}}, True, None),
        ({"capabilities": None, "limit": {"context": True}}, False, None),
    ]
    for extra, tools, context in cases:
        raw = {"data": [{"id": "big-pickle", "object": "model", **extra}]}
        (entry,) = router_sources.parse_opencode_models(
            raw, terms.opencode_free_models, terms)
        assert entry.capabilities.tools is tools
        assert entry.capabilities.context_tokens is context


def test_deployments_carry_manual_retention_and_confidentiality():
    terms = _fresh_terms({"groq": {"retention": "zero-30d", "confidentiality_allowed": ["public"]}})
    (entry,) = router_sources.parse_groq_models({"data": [{"id": "m"}]}, terms)
    deployment = entry.deployments[0]
    assert deployment.retention_policy == "zero-30d"
    assert [level.value for level in deployment.approved_confidentiality] == ["public"]
    trimmed = terms.model_copy(update={"terms": [item for item in terms.terms if item.provider != "groq"]})
    (entry,) = router_sources.parse_groq_models({"data": [{"id": "m"}]}, trimmed)
    deployment = entry.deployments[0]
    assert deployment.retention_policy == "unknown"
    assert deployment.approved_confidentiality == []


def test_openrouter_source_names_key_check_only_when_present():
    terms = _fresh_terms()
    raw = _or_models(_or_model("vendor/model:free"))
    (entry,) = router_sources.parse_openrouter_models(raw, _key_payload(), terms)
    assert entry.deployments[0].source.startswith("openrouter-models+key @")
    (entry,) = router_sources.parse_openrouter_models(raw, None, terms)
    assert entry.deployments[0].source.startswith("openrouter-models @")


def test_opencode_stale_free_list_offers_nothing():
    terms = _fresh_terms()
    old = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
    stale_list = terms.opencode_free_models.model_copy(update={"verified_at": old})
    raw = _oc_models("big-pickle")
    evidence = {"big-pickle": datetime.now(timezone.utc).isoformat()}
    (entry,) = router_sources.parse_opencode_models(raw, stale_list, terms, evidence)
    deployment = entry.deployments[0]
    assert deployment.free_offered is False


def test_opencode_fetch_sends_no_auth_header():
    seen: list = []
    raw = _oc_models("big-pickle")
    result = router_sources.fetch_opencode_models(_fake_get(raw, seen=seen))
    assert result == raw
    assert len(seen) == 1
    url, headers = seen[0]
    assert url == "https://opencode.ai/inference/v1/models"
    router_live.assert_no_auth(headers)


def test_opencode_missing_data_list_is_parse_error():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.parse_opencode_models({}, _fresh_terms().opencode_free_models, _fresh_terms())
    assert exc_info.value.kind == "parse"


# Groq / Gemini / NVIDIA parsing


def test_groq_listed_model_verifies_with_effective_zero_price():
    terms = _fresh_terms()
    raw = {"data": [{"id": "openai/gpt-oss-20b", "context_window": 131072}]}
    (entry,) = router_sources.parse_groq_models(raw, terms)
    deployment = entry.deployments[0]
    assert deployment.free_offered is True
    assert deployment.free_verified is True
    assert deployment.input_price_usd == 0
    assert deployment.requires_payment_method is False
    assert entry.capabilities.context_tokens == 131072


def test_groq_invalid_key_verifies_nothing():
    raw = {"data": [{"id": "openai/gpt-oss-20b"}]}
    (entry,) = router_sources.parse_groq_models(raw, _fresh_terms(), key_valid=False)
    deployment = entry.deployments[0]
    assert deployment.free_offered is False
    assert deployment.free_verified is False


def test_gemini_uses_free_tier_terms_and_metadata_context():
    terms = _fresh_terms({"gemini": {"data_policy": "trains"}})
    raw = {"models": [{"name": "models/gemini-2.5-flash", "inputTokenLimit": 1048576}]}
    (entry,) = router_sources.parse_gemini_models(raw, terms)
    deployment = entry.deployments[0]
    assert deployment.deployment_id == "gemini/gemini-2.5-flash"
    assert deployment.free_verified is True
    assert deployment.data_policy == "trains"
    assert entry.capabilities.context_tokens == 1048576


def test_gemini_missing_models_list_is_parse_error():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.parse_gemini_models({"data": []}, _fresh_terms())
    assert exc_info.value.kind == "parse"


def test_nvidia_listed_model_verifies():
    terms = _fresh_terms()
    raw = {"data": [{"id": "meta/llama-3.1-8b-instruct"}]}
    (entry,) = router_sources.parse_nvidia_models(raw, terms)
    deployment = entry.deployments[0]
    assert deployment.free_verified is True
    assert deployment.input_price_usd == 0


def test_one_time_credit_terms_verify_nothing():
    terms = _fresh_terms({"groq": {"one_time_credit_only": True}})
    raw = {"data": [{"id": "openai/gpt-oss-20b"}]}
    (entry,) = router_sources.parse_groq_models(raw, terms)
    deployment = entry.deployments[0]
    assert deployment.free_verified is False
    assert router_sources.is_terms_usable(terms, "groq") is False


def test_card_required_terms_verify_nothing():
    terms = _fresh_terms({"nvidia": {"requires_payment_method": True}})
    raw = {"data": [{"id": "meta/llama-3.1-8b-instruct"}]}
    (entry,) = router_sources.parse_nvidia_models(raw, terms)
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


def test_stale_terms_verify_nothing():
    old = (datetime.now(timezone.utc).date() - timedelta(days=8)).isoformat()
    terms = _fresh_terms({"gemini": {"verified_at": old}})
    raw = {"models": [{"name": "models/gemini-2.5-flash"}]}
    (entry,) = router_sources.parse_gemini_models(raw, terms)
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


def test_missing_terms_record_verifies_nothing():
    terms = _fresh_terms()
    trimmed = terms.model_copy(update={"terms": [entry for entry in terms.terms if entry.provider != "groq"]})
    raw = {"data": [{"id": "openai/gpt-oss-20b"}]}
    (entry,) = router_sources.parse_groq_models(raw, trimmed)
    deployment = entry.deployments[0]
    assert deployment.free_verified is False


# Source errors


def _fetch_error(provider_url, code, retry_after=None):
    def _get(url, headers):
        headers_msg = Message()
        if retry_after is not None:
            headers_msg["Retry-After"] = retry_after
        raise urllib.error.HTTPError(url, code, f"HTTP {code}", headers_msg, None)

    return _get


def test_source_401_is_auth_without_retry():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.fetch_groq_models("synthetic", _fetch_error("groq", 401))
    error = exc_info.value
    assert (error.provider, error.kind, error.http_status, error.retryable) == ("groq", "auth", 401, False)


def test_source_403_is_forbidden_without_retry():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.fetch_nvidia_models("synthetic", _fetch_error("nvidia", 403))
    error = exc_info.value
    assert error.kind == "forbidden"
    assert error.retryable is False


def test_source_429_is_retryable_with_retry_after():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.fetch_gemini_models("synthetic", _fetch_error("gemini", 429, retry_after="120"))
    error = exc_info.value
    assert error.kind == "rate_limited"
    assert error.retryable is True
    assert error.retry_after == "120"


def test_source_500_is_retryable_server_error():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.fetch_openrouter_models(_fetch_error("openrouter", 503))
    error = exc_info.value
    assert error.kind == "server"
    assert error.retryable is True


def test_source_network_failure_is_retryable():
    def _get(url, headers):
        raise urllib.error.URLError("boom")

    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.fetch_opencode_models(_get)
    assert exc_info.value.kind == "network"
    assert exc_info.value.retryable is True


def test_source_non_object_payload_is_parse_error():
    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.fetch_opencode_models(lambda url, headers: ["not", "a", "dict"])
    assert exc_info.value.kind == "parse"


def test_refresh_source_rejects_unknown_provider():
    with pytest.raises(router_sources.SourceError):
        router_sources.refresh_source("cerebras", _fresh_terms())


def test_source_error_carries_truncated_server_detail():
    import io

    def _get(url, headers):
        body = io.BytesIO(b'{"message": "model earth-chan-1 not found"}')
        raise urllib.error.HTTPError(url, 404, "HTTP 404", Message(), body)

    with pytest.raises(router_sources.SourceError) as exc_info:
        router_sources.fetch_nvidia_models("synthetic", _get)
    assert "earth-chan-1 not found" in str(exc_info.value)


def test_probe_error_carries_truncated_server_detail():
    import io

    body = io.BytesIO(b'{"error": "model not served on this endpoint"}')
    http_error = urllib.error.HTTPError("https://example.test", 404, "HTTP 404", Message(), body)
    detail = router_probes._error_body(http_error)
    error = router_probes.ProbeError.from_http("nvidia", "chat", 404, "https://example.test", None, detail)
    assert "model not served" in str(error)


# TTL, build and merge


def _fetched(provider, *entries, age_hours=0):
    return router_sources.FetchedCatalog(
        provider=provider,
        entries=tuple(entries),
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        ttl=timedelta(hours=6),
    )


def test_fetched_catalog_freshness_boundaries():
    (entry,) = router_sources.parse_groq_models(
        {"data": [{"id": "m"}]}, _fresh_terms()
    )
    assert _fetched("groq", entry).is_fresh() is True
    assert _fetched("groq", entry, age_hours=7).is_fresh() is False


def test_build_catalog_uses_unknown_family_pending_aliases():
    (entry,) = router_sources.parse_groq_models(
        {"data": [{"id": "openai/gpt-oss-20b"}]}, _fresh_terms()
    )
    deployment = entry.deployments[0]
    doc = router_sources.build_catalog([_fetched("groq", entry)])
    (entry,) = doc.entries
    assert entry.family_id == router_sources.UNKNOWN_FAMILY
    assert entry.canonical_model_id == deployment.deployment_id
    assert CatalogDocument.model_validate(doc.model_dump()) == doc


def test_build_catalog_rejects_duplicate_deployments():
    (entry,) = router_sources.parse_groq_models(
        {"data": [{"id": "openai/gpt-oss-20b"}]}, _fresh_terms()
    )
    deployment = entry.deployments[0]
    with pytest.raises(ValidationError):
        router_sources.build_catalog(
            [_fetched("groq", entry), _fetched("groq", entry)]
        )


def test_merge_without_failures_is_fresh():
    (entry,) = router_sources.parse_groq_models(
        {"data": [{"id": "openai/gpt-oss-20b"}]}, _fresh_terms()
    )
    deployment = entry.deployments[0]
    doc, stale, reason = router_sources.merge_with_previous(
        None, [_fetched("groq", entry)], {}
    )
    assert stale is False
    assert reason is None
    assert len(doc.entries) == 1


def test_merge_keeps_previous_entries_for_dead_source():
    (old_nvidia_entry,) = router_sources.parse_nvidia_models(
        {"data": [{"id": "meta/llama-3.1-8b-instruct"}]}, _fresh_terms()
    )
    old_nvidia = old_nvidia_entry.deployments[0]
    previous = router_sources.build_catalog([_fetched("nvidia", old_nvidia_entry)])
    (fresh_groq_entry,) = router_sources.parse_groq_models(
        {"data": [{"id": "openai/gpt-oss-20b"}]}, _fresh_terms()
    )
    fresh_groq = fresh_groq_entry.deployments[0]
    doc, stale, reason = router_sources.merge_with_previous(
        previous, [_fetched("groq", fresh_groq_entry)], {"nvidia": "HTTP 503"}
    )
    assert stale is True
    assert "nvidia" in (reason or "")
    assert sorted(entry.deployments[0].provider for entry in doc.entries) == ["groq", "nvidia"]
    assert CatalogDocument.model_validate(doc.model_dump()) == doc


def test_merge_without_previous_marks_partial_stale():
    (fresh_groq_entry,) = router_sources.parse_groq_models(
        {"data": [{"id": "openai/gpt-oss-20b"}]}, _fresh_terms()
    )
    fresh_groq = fresh_groq_entry.deployments[0]
    doc, stale, reason = router_sources.merge_with_previous(
        None, [_fetched("groq", fresh_groq_entry)], {"nvidia": "network"}
    )
    assert stale is True
    assert doc.stale is True
    assert "nvidia" in (reason or "")


# Probe payloads and parsers


def test_openai_chat_payload_is_minimal_and_deterministic():
    payload = router_probes.openai_chat_payload("vendor/model")
    assert payload["model"] == "vendor/model"
    assert payload["max_tokens"] == router_probes.CHAT_MAX_TOKENS
    assert payload["temperature"] == 0
    assert payload["messages"] == [{"role": "user", "content": router_probes.CHAT_PROMPT}]


def test_openai_tool_payload_offers_single_synthetic_tool():
    payload = router_probes.openai_tool_payload("vendor/model")
    assert payload["max_tokens"] == router_probes.TOOL_MAX_TOKENS
    (tool,) = payload["tools"]
    assert tool["function"]["name"] == "test_echo"


def test_gemini_payloads_carry_no_model_field():
    assert "model" not in router_probes.gemini_chat_payload()
    tool_payload = router_probes.gemini_tool_payload()
    (declaration,) = tool_payload["tools"][0]["functionDeclarations"]
    assert declaration["name"] == "test_echo"


def _chat_response(text="ok"):
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2},
    }


def _tool_response(call_id="call_1", name="test_echo", arguments='{"word": "ok"}'):
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"id": call_id, "function": {"name": name, "arguments": arguments}}
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def test_parse_openai_chat_extracts_text():
    assert router_probes.parse_openai_chat("groq", _chat_response("ok")) == "ok"


def test_parse_openai_chat_rejects_empty_or_missing_text():
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_openai_chat("groq", _chat_response("  "))
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_openai_chat("groq", {"choices": []})


def test_parse_openai_tool_calls_validates_id_name_and_args():
    (call,) = router_probes.parse_openai_tool_calls("groq", _tool_response())
    assert (call.call_id, call.name, call.arguments) == ("call_1", "test_echo", {"word": "ok"})


def test_parse_openai_tool_calls_rejects_bad_shapes():
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_openai_tool_calls("groq", _tool_response(call_id=""))
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_openai_tool_calls("groq", _tool_response(arguments="not json"))
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_openai_tool_calls("groq", _chat_response())


def test_second_turn_builder_echoes_each_tool_call_id():
    (call,) = router_probes.parse_openai_tool_calls("groq", _tool_response())
    assistant = {"role": "assistant", "tool_calls": [{"id": "call_1"}]}
    messages = router_probes.build_openai_second_turn("hi", assistant, [call], ['{"word": "ok"}'])
    assert messages[0] == {"role": "user", "content": "hi"}
    assert messages[-1] == {"role": "tool", "tool_call_id": "call_1", "content": '{"word": "ok"}'}
    with pytest.raises(router_probes.ProbeError):
        router_probes.build_openai_second_turn("hi", assistant, [call], [])


def _gemini_response(*parts):
    return {"candidates": [{"content": {"parts": list(parts)}}]}


def test_parse_gemini_text_joins_parts():
    response = _gemini_response({"text": "o"}, {"text": "k"})
    assert router_probes.parse_gemini_text("gemini", response) == "ok"


def test_parse_gemini_text_rejects_function_only_answers():
    response = _gemini_response({"functionCall": {"name": "test_echo", "args": {}}})
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_gemini_text("gemini", response)


def test_parse_gemini_function_calls_validates_name_and_args():
    response = _gemini_response({"functionCall": {"name": "test_echo", "args": {"word": "ok"}}})
    (call,) = router_probes.parse_gemini_function_calls("gemini", response)
    assert call.name == "test_echo"
    assert call.arguments == {"word": "ok"}
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_gemini_function_calls("gemini", _gemini_response({"text": "ok"}))


def test_parse_stream_events_requires_finish_reason():
    events = [
        {"choices": [{"delta": {"content": "o"}, "finish_reason": None}]},
        {"choices": [{"delta": {"content": "k"}, "finish_reason": "stop"}]},
    ]
    text, finished = router_probes.parse_stream_events("groq", events)
    assert (text, finished) == ("ok", True)
    cut = [{"choices": [{"delta": {"content": "o"}, "finish_reason": None}]}]
    _, finished = router_probes.parse_stream_events("groq", cut)
    assert finished is False


def test_parse_stream_events_rejects_non_list_choices():
    with pytest.raises(router_probes.ProbeError):
        router_probes.parse_stream_events("groq", [{"choices": {"delta": {}}}])


# Probe runner


def test_probe_disabled_by_default_makes_no_call():
    def _post(url, headers, payload):
        raise AssertionError("disabled probe must not call the network")

    with pytest.raises(router_probes.ProbeError) as exc_info:
        router_probes.run_probe("groq", "chat", "m", {"Authorization": "Bearer x"}, post=_post)
    assert "disabled" in str(exc_info.value)


def test_probe_refuses_opencode_credentials(isolated_home):
    headers = {"Authorization": "Bearer synthetic"}
    with pytest.raises(router_live.LiveGuardError):
        router_probes.run_probe(
            "opencode", "chat", "big-pickle", headers, enabled=True,
            post=_fake_post(_chat_response()),
        )


def test_probe_chat_success(isolated_home):
    seen: list = []
    result, raw = router_probes.run_probe(
        "groq", "chat", "openai/gpt-oss-20b", {"Authorization": "Bearer synthetic"},
        enabled=True, post=_fake_post(_chat_response(), seen=seen),
    )
    assert result.deployment_id == "groq/openai/gpt-oss-20b"
    assert (result.input_tokens, result.output_tokens) == (12, 2)
    assert raw["choices"][0]["message"]["content"] == "ok"
    assert seen[0][0] == "https://api.groq.com/openai/v1/chat/completions"


def test_probe_tool_success_marks_tools_capability(isolated_home):
    result, _ = router_probes.run_probe(
        "groq", "tool_calls", "openai/gpt-oss-20b", {"Authorization": "Bearer synthetic"},
        enabled=True, post=_fake_post(_tool_response()),
    )
    assert result.capabilities.tools is True


def test_probe_rate_limit_propagates(isolated_home):
    error = router_live.LiveRateLimited("groq", "https://example.test", None)
    with pytest.raises(router_live.LiveRateLimited):
        router_probes.run_probe(
            "groq", "chat", "m", {"Authorization": "Bearer synthetic"},
            enabled=True, post=_fake_post(_chat_response(), error=error),
        )


def test_probe_rejects_unknown_provider_kind_and_shapes(isolated_home):
    post = _fake_post(_chat_response())
    with pytest.raises(router_probes.ProbeError):
        router_probes.run_probe("cerebras", "chat", "m", {}, enabled=True, post=post)
    with pytest.raises(router_probes.ProbeError):
        router_probes.run_probe("groq", "telepathy", "m", {}, enabled=True, post=post)
    with pytest.raises(router_probes.ProbeError):
        router_probes.run_probe("groq", "second_turn", "m", {}, enabled=True, post=post)
    with pytest.raises(router_probes.ProbeError):
        router_probes.run_probe("gemini", "streaming", "m", {}, enabled=True, post=post)


def test_probe_streaming_requires_complete_stream(isolated_home):
    complete = [[
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
    ]]
    result, _ = router_probes.run_probe(
        "groq", "streaming", "m", {"Authorization": "Bearer synthetic"},
        enabled=True, post_stream=lambda url, headers, payload: complete[0],
    )
    assert result.capabilities.streaming is True
    cut = [[{"choices": [{"delta": {"content": "o"}, "finish_reason": None}]}]]
    with pytest.raises(router_probes.ProbeError):
        router_probes.run_probe(
            "groq", "streaming", "m", {"Authorization": "Bearer synthetic"},
            enabled=True, post_stream=lambda url, headers, payload: cut[0],
        )


def test_probe_gemini_chat_reports_usage_metadata(isolated_home):
    response = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 1},
    }
    result, _ = router_probes.run_probe(
        "gemini", "chat", "gemini-3.8-flash", {"x-goog-api-key": "synthetic"},
        enabled=True, post=_fake_post(response),
    )
    assert (result.input_tokens, result.output_tokens) == (7, 1)


def test_refresh_source_wires_each_provider():
    terms = _fresh_terms()
    evidence = {"big-pickle": datetime.now(timezone.utc).isoformat()}
    cases = [
        ("openrouter", {"data": [{"id": "v/m:free", "pricing": {"prompt": "0", "completion": "0"}}]}),
        ("opencode", {"data": [{"id": "big-pickle"}]}),
        ("groq", {"data": [{"id": "m"}]}),
        ("nvidia", {"data": [{"id": "m"}]}),
        ("gemini", {"models": [{"name": "models/m"}]}),
    ]
    for provider, payload in cases:
        def _get(url, headers, _payload=payload, _provider=provider):
            if _provider == "openrouter" and url.endswith("/key"):
                return _key_payload()
            return _payload

        fetched = router_sources.refresh_source(
            provider, terms, api_key="synthetic", serve_evidence=evidence, get=_get
        )
        assert fetched.provider == provider
        assert fetched.entries, f"{provider} refresh produced no entries"
        assert fetched.is_fresh() is True
