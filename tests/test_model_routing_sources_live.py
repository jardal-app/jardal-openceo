"""Live source catalogs and minimal probes, one flow per provider (TASK-2026-00289).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole file.
Catalog tests fetch real model lists and assert free_verified decisions;
probe tests run one minimal chat and one tool_calls probe per provider
with a free model. A real 429 skips that provider until the next UTC day.
Never prints secrets: assertions touch only sanitized responses.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from model_routing import live as router_live
from model_routing import probes as router_probes
from model_routing import secrets as router_secrets
from model_routing import sources as router_sources
from model_routing import terms as router_terms
from model_routing.config import ConfigError, load_policy

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"

_RAW: dict[str, dict] = {}
_ENTRIES: dict[str, list] = {}
_USAGE_BEFORE: float | None = None


def _policy():
    try:
        return load_policy()
    except ConfigError:
        return None


def _budget_guard(provider: str) -> None:
    budget = router_live.read_budget()
    if budget.used.get(provider, 0) >= router_live.effective_cap(provider, _policy()):
        pytest.skip(f"daily live budget exhausted for {provider}")


def _secrets():
    try:
        return router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")


def _redaction_secrets() -> list[str]:
    try:
        found = router_secrets.load_router_secrets()
    except router_secrets.SecretsError:
        return []
    return [value for value in vars(found).values() if isinstance(value, str) and value]


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


def _verified_ids(provider: str) -> list[str]:
    return [
        entry.deployments[0].deployment_id
        for entry in _ENTRIES.get(provider, [])
        if entry.deployments[0].free_verified
    ]


# Catalog downloads with free_verified decisions


def test_openrouter_catalog_live():
    global _USAGE_BEFORE
    key, secret_values = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    _budget_guard("openrouter")
    terms = router_terms.load_terms()
    with _skip_on_429("openrouter"):
        raw = router_sources.fetch_openrouter_models()
        key_payload = router_sources.fetch_openrouter_key(key)
    entries = router_sources.parse_openrouter_models(raw, key_payload, terms)
    verified = [entry for entry in entries if entry.deployments[0].free_verified]
    assert verified, "no free_verified :free model in the live catalog"
    for entry in verified:
        deployment = entry.deployments[0]
        assert deployment.deployment_id.endswith(":free")
        assert deployment.input_price_usd == 0
        assert deployment.output_price_usd == 0
    info = router_live.parse_openrouter_key_info(key_payload)
    assert info.free_limit == 1000
    _USAGE_BEFORE = info.usage
    _RAW["openrouter"] = raw
    _ENTRIES["openrouter"] = entries
    by_id = {
        item["id"]: item
        for item in raw["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    _record(
        "sources-openrouter-catalog.json",
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "total_models": len(raw["data"]),
            "verified_free": [by_id[entry.deployments[0].gateway_model_id] for entry in verified],
        },
        secret_values,
    )
    _record("sources-openrouter-key-before.json", key_payload, secret_values)
    router_live.spend("openrouter", 2, policy=_policy())


def test_opencode_catalog_live():
    secret_values = _redaction_secrets()
    _budget_guard("opencode")
    terms = router_terms.load_terms()
    with _skip_on_429("opencode"):
        raw = router_sources.fetch_opencode_models()
    entries = router_sources.parse_opencode_models(raw, terms.opencode_free_models, terms)
    offered = {entry.deployments[0].gateway_model_id for entry in entries if entry.deployments[0].free_offered}
    assert "big-pickle" in offered, "big-pickle must be free by published price, not suffix"
    _RAW["opencode"] = raw
    _ENTRIES["opencode"] = entries
    _record("sources-opencode-catalog.json", raw, secret_values)
    router_live.spend("opencode", policy=_policy())


def test_groq_catalog_live():
    key, secret_values = _key("FREE_ROUTER_GROQ_API_KEY")
    _budget_guard("groq")
    terms = router_terms.load_terms()
    with _skip_on_429("groq"):
        raw = router_sources.fetch_groq_models(key)
    entries = router_sources.parse_groq_models(raw, terms)
    verified = [entry for entry in entries if entry.deployments[0].free_verified]
    assert verified, "no free_verified model on the Groq free tier"
    assert all(entry.deployments[0].requires_payment_method is False for entry in verified)
    _RAW["groq"] = raw
    _ENTRIES["groq"] = entries
    _record("sources-groq-catalog.json", raw, secret_values)
    router_live.spend("groq", policy=_policy())


def test_gemini_catalog_live():
    key, secret_values = _key("FREE_ROUTER_GEMINI_API_KEY")
    _budget_guard("gemini")
    terms = router_terms.load_terms()
    with _skip_on_429("gemini"):
        raw = router_sources.fetch_gemini_models(key)
    entries = router_sources.parse_gemini_models(raw, terms)
    verified = [entry for entry in entries if entry.deployments[0].free_verified]
    assert verified, "no free_verified model on the Gemini free tier"
    assert all(entry.deployments[0].data_policy == "trains" for entry in verified)
    _RAW["gemini"] = raw
    _ENTRIES["gemini"] = entries
    _record("sources-gemini-catalog.json", raw, secret_values)
    router_live.spend("gemini", policy=_policy())


def test_nvidia_catalog_live():
    key, secret_values = _key("FREE_ROUTER_NVIDIA_NIM_API_KEY")
    _budget_guard("nvidia")
    terms = router_terms.load_terms()
    with _skip_on_429("nvidia"):
        raw = router_sources.fetch_nvidia_models(key)
    entries = router_sources.parse_nvidia_models(raw, terms)
    verified = [entry for entry in entries if entry.deployments[0].free_verified]
    assert verified, "no free_verified model on the NVIDIA trial tier"
    _RAW["nvidia"] = raw
    _ENTRIES["nvidia"] = entries
    _record("sources-nvidia-catalog.json", raw, secret_values)
    router_live.spend("nvidia", policy=_policy())


# Minimal probes with free models


def _require_catalog(provider: str) -> None:
    if provider not in _RAW:
        pytest.skip(f"{provider} catalog test did not run")


def _live_ids(provider: str) -> list[str]:
    raw = _RAW[provider]
    items = raw.get("data") if provider != "gemini" else raw.get("models")
    ids: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if provider == "gemini":
            name = item.get("name", "")
            ids.append(name.split("/")[-1])
        elif isinstance(item.get("id"), str):
            ids.append(item["id"])
    return ids


def _openrouter_tool_model() -> str:
    by_id = {item["id"]: item for item in _RAW["openrouter"]["data"] if isinstance(item, dict)}
    for deployment_id in _verified_ids("openrouter"):
        native = deployment_id.split("/", 1)[1]
        params = by_id.get(native, {}).get("supported_parameters") or []
        if "tools" in params:
            return native
    raise AssertionError("no verified :free model advertises tools support")


_PROBE_MODEL: dict[str, str] = {}


def _gemini_probe_candidates() -> list[str]:
    # gemini-2.5-flash is listed but retired for new keys (API 404 message,
    # 2026-10-23); the API itself recommends gemini-3.8-flash.
    ids = _live_ids("gemini")
    assert ids, "Gemini catalog is empty"
    ordered = [model for model in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash") if model in ids]
    for candidate in ids:
        lowered = candidate.lower()
        if (
            "flash" in lowered
            and candidate not in ordered
            and not any(tag in lowered for tag in ("preview", "tts", "image", "lite", "omni"))
        ):
            ordered.append(candidate)
            break
    if ids[0] not in ordered:
        ordered.append(ids[0])
    return ordered


def _nvidia_probe_candidates() -> list[str]:
    # The trial key lists 80+ models but serves only a provisioned subset
    # (unlisted ones 404 per account); small chat models first.
    ids = _live_ids("nvidia")
    assert ids, "NVIDIA catalog is empty"
    preferred = (
        "openai/gpt-oss-20b",
        "mistralai/mistral-7b-instruct-v0.3",
        "google/gemma-3-4b-it",
        "zyphra/zamba2-7b-instruct",
        "nvidia/mistral-nemo-minitron-8b-8k-instruct",
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "deepseek-ai/deepseek-v4.1-flash",
        "z-ai/glm-5.3-flash",
    )
    ordered = [model for model in preferred if model in ids]
    for candidate in ids:
        if "instruct" in candidate.lower() and candidate not in ordered:
            ordered.append(candidate)
            break
    if ids[0] not in ordered:
        ordered.append(ids[0])
    return ordered


def _run_with_candidates(provider, runner, candidates, headers, secret_values, statuses=(404, 503)):
    ordered = list(candidates)
    if provider in _PROBE_MODEL:
        stashed = _PROBE_MODEL[provider]
        ordered = [stashed] + [model for model in ordered if model != stashed]
    last_error: Exception | None = None
    for model in ordered:
        try:
            result = runner(provider, model, headers, secret_values)
        except router_probes.ProbeError as exc:
            if exc.http_status in statuses:
                last_error = exc
                continue
            raise
        _PROBE_MODEL[provider] = model
        return result
    assert last_error is not None
    raise last_error


def _run_chat(provider: str, model: str, headers: dict, secret_values: list[str]):
    _require_catalog(provider)
    _budget_guard(provider)
    try:
        result, raw = router_probes.run_probe(
            provider, "chat", model, headers, enabled=True, policy=_policy()
        )
    except router_live.LiveRateLimited:
        pytest.skip(f"real 429 for {provider}; resume tomorrow")
    assert result.deployment_id == f"{provider}/{model}"
    _record(f"sources-{provider}-chat.json", raw, secret_values)
    return result


def _run_tools(provider: str, model: str, headers: dict, secret_values: list[str]):
    _require_catalog(provider)
    _budget_guard(provider)
    try:
        result, raw = router_probes.run_probe(
            provider, "tool_calls", model, headers, enabled=True, policy=_policy()
        )
    except router_live.LiveRateLimited:
        pytest.skip(f"real 429 for {provider}; resume tomorrow")
    assert result.capabilities.tools is True
    _record(f"sources-{provider}-tools.json", raw, secret_values)
    return result


def test_openrouter_chat_probe_live():
    key, secret_values = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    _require_catalog("openrouter")
    ids = _verified_ids("openrouter")
    assert ids, "no verified :free model to probe"
    _run_chat("openrouter", ids[0].split("/", 1)[1], {"Authorization": f"Bearer {key}"}, secret_values)


def test_openrouter_tool_probe_live():
    key, secret_values = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    _require_catalog("openrouter")
    _run_tools("openrouter", _openrouter_tool_model(), {"Authorization": f"Bearer {key}"}, secret_values)


def test_opencode_direct_api_blocked_live():
    # The docs say free chat models serve without auth, but the live API
    # answers 403 FreeTierError: the free tier only serves from within the
    # OpenCode CLI (verified 2026-10-23). This pins that behavior: if the
    # provider ever opens direct calls, the failure tells us to re-enable
    # HTTPS probes. No budget impact: failures never spend, and the raw
    # POST helper bypasses the pre-check so the block stays provable at cap.
    secret_values = _redaction_secrets()
    router_live.assert_no_auth({})
    url = "https://opencode.ai/inference/openai/v1/chat/completions"
    blocked: dict[str, dict] = {}
    for kind, payload in (
        ("chat", router_probes.openai_chat_payload("big-pickle")),
        ("tool_calls", router_probes.openai_tool_payload("big-pickle")),
    ):
        try:
            router_probes._http_post_json("opencode", kind, url, {}, payload)
        except router_probes.ProbeError as exc:
            assert exc.http_status == 403, f"expected the documented 403 block, got {exc.http_status}"
            assert "FreeTierError" in exc.detail, "expected FreeTierError, provider behavior changed"
            blocked[kind] = {"http_status": 403, "error_type": "FreeTierError", "detail": exc.detail}
        else:
            raise AssertionError(f"direct OpenCode {kind} unexpectedly served; re-enable HTTPS probes")
    _record(
        "sources-opencode-blocked.json",
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "note": "free tier serves only from within the OpenCode CLI; direct HTTPS is policy-blocked",
            "blocked": blocked,
        },
        secret_values,
    )


def test_opencode_cli_probe_live(tmp_path):
    # big-pickle serves from within the CLI (the only allowed free path):
    # one run proves both text answers and tool calls, with a cost:0 receipt.
    # Honors the live budget (guard + spend) and runs only with FREE_ROUTER_LIVE=1.
    import re
    import shutil
    import subprocess

    secret_values = _redaction_secrets()
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI is not installed")
    _budget_guard("opencode")
    prompt = "Use a file tool to list the files in the current directory, then reply with exactly: ok"
    try:
        completed = subprocess.run(
            ["opencode", "run", "--standalone", "--format", "json",
             "--model", "opencode/big-pickle", prompt],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=110,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("opencode CLI run timed out")
    assert completed.returncode == 0, f"CLI run failed: {completed.stderr[-500:]}"
    events: list[dict] = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped:
            try:
                events.append(json.loads(stripped))
            except ValueError:
                pytest.fail(f"CLI run emitted a non-JSON line: {stripped[:120]}")
    assert events, "CLI run produced no JSONL events"
    tools = [
        event for event in events
        if event.get("type") == "tool_use"
        and isinstance(event.get("part"), dict)
        and event["part"].get("id")
        and event["part"].get("tool")
    ]
    assert tools, "CLI run produced no tool_use event"
    texts = [
        event["part"]["text"]
        for event in events
        if event.get("type") == "text" and isinstance(event.get("part"), dict)
    ]
    assert re.search(r"\bok\b", " ".join(texts).lower()), "CLI run answer misses the exact word"
    costs = [
        event["part"]["cost"]
        for event in events
        if event.get("type") == "step_finish" and isinstance(event.get("part"), dict)
        and isinstance(event["part"].get("cost"), (int, float))
    ]
    assert costs, "CLI run emitted no step_finish cost receipt"
    assert all(cost == 0 for cost in costs), "CLI free run must cost zero"
    observed_at = datetime.now(timezone.utc).isoformat()
    _record(
        "sources-opencode-cli.json",
        {
            "recorded_at": observed_at,
            "model": "opencode/big-pickle",
            "prompt": prompt,
            "tool_names": sorted({event["part"]["tool"] for event in tools}),
            "events": events,
        },
        secret_values,
    )
    router_live.spend("opencode", policy=_policy())
    terms = router_terms.load_terms()
    recorded = json.loads((FIXTURES / "sources-opencode-catalog.json").read_text(encoding="utf-8"))
    entries = router_sources.parse_opencode_models(
        recorded, terms.opencode_free_models, terms, {"big-pickle": observed_at}
    )
    verified = {
        entry.deployments[0].gateway_model_id for entry in entries if entry.deployments[0].free_verified
    }
    assert "big-pickle" in verified, "big-pickle must verify by published price plus CLI serving"


def test_groq_chat_probe_live():
    key, secret_values = _key("FREE_ROUTER_GROQ_API_KEY")
    _require_catalog("groq")
    assert "openai/gpt-oss-20b" in _live_ids("groq"), "groq probe model retired; update selection"
    _run_chat("groq", "openai/gpt-oss-20b", {"Authorization": f"Bearer {key}"}, secret_values)


def test_groq_tool_probe_live():
    key, secret_values = _key("FREE_ROUTER_GROQ_API_KEY")
    _require_catalog("groq")
    assert "openai/gpt-oss-20b" in _live_ids("groq"), "groq probe model retired; update selection"
    _run_tools("groq", "openai/gpt-oss-20b", {"Authorization": f"Bearer {key}"}, secret_values)


def test_gemini_chat_probe_live():
    key, secret_values = _key("FREE_ROUTER_GEMINI_API_KEY")
    _require_catalog("gemini")
    _run_with_candidates(
        "gemini", _run_chat, _gemini_probe_candidates(), {"x-goog-api-key": key}, secret_values
    )


def test_gemini_tool_probe_live():
    key, secret_values = _key("FREE_ROUTER_GEMINI_API_KEY")
    _require_catalog("gemini")
    _run_with_candidates(
        "gemini", _run_tools, _gemini_probe_candidates(), {"x-goog-api-key": key}, secret_values,
        statuses=(400, 404, 422, 503),
    )


def test_nvidia_chat_probe_live():
    key, secret_values = _key("FREE_ROUTER_NVIDIA_NIM_API_KEY")
    _require_catalog("nvidia")
    _run_with_candidates(
        "nvidia", _run_chat, _nvidia_probe_candidates(),
        {"Authorization": f"Bearer {key}"}, secret_values,
    )


def test_nvidia_tool_probe_live():
    key, secret_values = _key("FREE_ROUTER_NVIDIA_NIM_API_KEY")
    _require_catalog("nvidia")
    _run_with_candidates(
        "nvidia", _run_tools, _nvidia_probe_candidates(),
        {"Authorization": f"Bearer {key}"}, secret_values,
        statuses=(400, 404, 422, 503),
    )


# Invalid credential and zero-cost guard


def test_invalid_key_yields_401_live():
    secret_values = _redaction_secrets()
    fake_key = "sk-invalid-401-synthetic-0000"
    try:
        router_sources.fetch_groq_models(fake_key)
    except router_sources.SourceError as exc:
        assert exc.kind == "auth"
        assert exc.http_status == 401
    else:
        raise AssertionError("invalid key did not yield 401")
    summary = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "provider": "groq",
        "http_status": 401,
        "kind": "auth",
        "note": "synthetic invalid credential; the key itself is never recorded",
    }
    _record("sources-401-groq.json", summary, secret_values + [fake_key])
    findings = router_live.scan_for_secrets(json.dumps(summary), [fake_key])
    assert findings == [], f"fixture leaks key material: {len(findings)} findings"


def test_openrouter_credits_unchanged_live():
    if _USAGE_BEFORE is None:
        pytest.skip("openrouter catalog test did not run")
    key, _secret_values = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    _budget_guard("openrouter")
    try:
        key_payload = router_sources.fetch_openrouter_key(key)
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for openrouter; resume tomorrow")
    except router_sources.SourceError as exc:
        if exc.kind == "rate_limited":
            pytest.skip("real 429 for openrouter; resume tomorrow")
        raise
    info = router_live.parse_openrouter_key_info(key_payload)
    router_live.assert_usage_unchanged(_USAGE_BEFORE, info.usage)
    router_live.spend("openrouter", policy=_policy())
    assert router_live.read_budget().used.get("openrouter", 0) >= 3
