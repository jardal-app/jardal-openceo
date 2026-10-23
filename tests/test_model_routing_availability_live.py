"""Live availability failover and recorded-error classification (TASK-2026-00291).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole file.
One proxy-injected 429 fails over to a real minimal chat, one real
not-found error is recorded and classified, and OpenRouter credit usage
must not move. Never prints secrets: assertions touch only sanitized
responses.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from model_routing import availability as router_availability
from model_routing import errors as router_errors
from model_routing import fault_proxy as router_proxy
from model_routing import live as router_live
from model_routing import probes as router_probes
from model_routing import secrets as router_secrets
from model_routing import sources as router_sources
from model_routing.availability import Candidate, get_next_candidate

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"

def _openrouter_candidates() -> list[str]:
    recorded = json.loads(
        (FIXTURES / "sources-openrouter-catalog.json").read_text(encoding="utf-8"))
    ids = [item["id"] for item in recorded.get("verified_free", [])]
    preferred = "recursive/flint-v1.0:free"
    ordered = [preferred] if preferred in ids else []
    ordered += [model for model in ids if model != preferred]
    assert ordered, "no verified :free model recorded today"
    return ordered



def _secrets():
    try:
        return router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")


def _key(name: str) -> tuple[str, list[str]]:
    found = _secrets()
    value = getattr(found, name)
    if not value:
        pytest.skip(f"{name} is unset")
    return value, [v for v in vars(found).values() if isinstance(v, str) and v]


def _record(name: str, payload: dict, secret_values: list[str]) -> None:
    router_live.record_fixture(name, json.dumps(payload, indent=2), FIXTURES, secret_values)


@contextmanager
def _skip_on_429(provider: str):
    try:
        yield
    except router_live.LiveRateLimited:
        pytest.skip(f"real 429 for {provider}; resume tomorrow")
    except router_sources.SourceError as exc:
        if exc.kind == "rate_limited":
            pytest.skip(f"real 429 for {provider}; resume tomorrow")
        raise


def _key_usage(key: str) -> float:
    with _skip_on_429("openrouter"):
        payload = router_sources.fetch_openrouter_key(key)
    info = router_live.parse_openrouter_key_info(payload)
    return info.usage


def _run_chat_with_candidates(provider: str, candidates, headers: dict,
                              statuses=(400, 404, 503)):
    last_error: Exception | None = None
    for model in candidates:
        try:
            return router_probes.run_probe(
                provider, "chat", model, headers, enabled=True)
        except router_live.LiveRateLimited:
            pytest.skip(f"real 429 for {provider}; resume tomorrow")
        except router_probes.ProbeError as exc:
            # Per-model 4xx/5xx and transport timeouts fail over to the
            # next candidate; anything else is a harness bug, not a fallback.
            if exc.http_status in statuses or exc.http_status is None:
                last_error = exc
                continue
            raise
    assert last_error is not None
    raise last_error


def test_failover_injected_429_to_real_chat_live(tmp_path):
    # The fault is injected on NVIDIA (via the proxy, spending nothing)
    # and the real minimal chat answers from OpenRouter; NVIDIA served
    # only timeouts and per-account 404s during this cycle (see the
    # ticket comment), so it cannot carry the real leg today.
    or_key, or_secrets = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    store = router_availability.AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        usage_before = _key_usage(or_key)
        pool = [
            Candidate("nvidia/openai/gpt-oss-20b", "famfault", "canon-fault", "nvidia", 0.9),
            Candidate("openrouter/recursive/flint-v1.0:free", "famreal", "canon-real",
                      "openrouter", 0.8),
        ]
        assert get_next_candidate(pool, store) == pool[0]
        with router_proxy.FaultProxy(
                {"nvidia": router_proxy.FaultSpec("rate_limited", retry_after_s=60)}) as proxy:
            sender = lambda url, headers, payload: proxy.post("nvidia", payload)  # noqa: E731
            with pytest.raises(router_live.LiveRateLimited):
                router_probes.run_probe("nvidia", "chat", "openai/gpt-oss-20b", {},
                                        enabled=True, post=sender)
            assert proxy.served == [("nvidia", "rate_limited")]
        classified = router_errors.classify_http(429, retry_after="60")
        router_availability.record_provider_error(
            store, provider="nvidia", deployment_id="nvidia/openai/gpt-oss-20b",
            model="openai/gpt-oss-20b", classified=classified, run_id="run-live-failover")
        assert store.cooldown_for("nvidia/openai/gpt-oss-20b") is not None
        assert get_next_candidate(pool, store) == pool[1]
        result, raw = _run_chat_with_candidates(
            "openrouter", _openrouter_candidates(), {"Authorization": f"Bearer {or_key}"})
        content = raw["choices"][0]["message"]["content"]
        assert "ok" in content.lower()
        _record("availability-failover-chat.json", {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "provider": "openrouter",
            "deployment_id": result.deployment_id,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "note": "minimal synthetic chat after an injected 429; no content recorded",
        }, or_secrets)
        router_availability.write_audit_event(
            store, "run-live-failover", "fallback",
            "nvidia/gpt-oss-20b -> openrouter (injected 429)")
        usage_after = _key_usage(or_key)
        router_live.assert_usage_unchanged(usage_before, usage_after)
        rows = store.errors_since(0)
        assert [(item.error_class, item.action, item.run_id) for item in rows] == [
            ("rate_limited", "cooldown_deployment", "run-live-failover")]
        assert store.audit_since("run-live-failover", 0)[0][1] == "fallback"
    finally:
        store.close()


def test_nonexistent_model_not_found_recorded_live(tmp_path):
    or_key, secret_values = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    store = router_availability.AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        usage_before = _key_usage(or_key)
        bogus = "free-router-no-such-model-001"
        try:
            router_probes.run_probe(
                "openrouter", "chat", bogus, {"Authorization": f"Bearer {or_key}"},
                enabled=True)
        except router_probes.ProbeError as exc:
            # OpenRouter answers unknown models with 400, not 404
            # ("not a valid model ID", verified 2026-10-23); pin that shape.
            assert exc.http_status == 400, (
                f"expected 400 for a nonexistent model, got {exc.http_status}")
            detail = exc.detail
        else:
            raise AssertionError("nonexistent model unexpectedly served")
        summary = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "provider": "openrouter",
            "model": bogus,
            "http_status": 400,
            "kind": "not_found",
            "note": "synthetic model id; the error precedes any generation",
        }
        _record("availability-not-found-openrouter.json", summary, secret_values)
        findings = router_live.scan_for_secrets(json.dumps(summary), secret_values)
        assert findings == [], f"fixture leaks key material: {len(findings)} findings"
        classified = router_errors.classify_http(400, detail=detail)
        assert (classified.error_class, classified.action) == ("not_found", "block_route")
        router_availability.record_provider_error(
            store, provider="openrouter", deployment_id=f"openrouter/{bogus}",
            model=bogus, classified=classified, run_id="run-live-404")
        assert store.route_disabled(f"openrouter/{bogus}") is not None
        usage_after = _key_usage(or_key)
        router_live.assert_usage_unchanged(usage_before, usage_after)
    finally:
        store.close()
