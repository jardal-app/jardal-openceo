"""Minimal live metadata checks, one per provider (TASK-2026-00287).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole file.
Each test spends one call from the real daily budget, records a
sanitized fixture, and never sends repo content. A real 429 skips that
provider until the next UTC day instead of failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_routing import live as router_live
from model_routing import secrets as router_secrets
from model_routing.config import ConfigError, load_policy

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"

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


def _key(name: str) -> tuple[str, list[str]]:
    secrets = _secrets()
    value = getattr(secrets, name)
    if not value:
        pytest.skip(f"{name} is unset")
    return value, [v for v in vars(secrets).values() if isinstance(v, str) and v]


def _record(provider: str, payload: dict, secret_values: list[str]) -> None:
    router_live.record_fixture(
        f"{provider}-metadata.json", json.dumps(payload, indent=2), FIXTURES, secret_values
    )


def test_openrouter_key_quota():
    global _USAGE_BEFORE
    key, secret_values = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    _budget_guard("openrouter")
    try:
        payload = router_live.http_get_json(
            "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}
        )
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for openrouter; resume tomorrow")
    info = router_live.parse_openrouter_key_info(payload)
    assert info.free_limit == 1000
    _USAGE_BEFORE = info.usage
    _record("openrouter-key", payload, secret_values)
    router_live.spend("openrouter", policy=_policy())


def test_nvidia_models_metadata():
    key, secret_values = _key("FREE_ROUTER_NVIDIA_NIM_API_KEY")
    _budget_guard("nvidia")
    try:
        payload = router_live.http_get_json(
            "https://integrate.api.nvidia.com/v1/models", {"Authorization": f"Bearer {key}"}
        )
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for nvidia; resume tomorrow")
    assert payload.get("data"), "NVIDIA models list is empty"
    _record("nvidia", payload, secret_values)
    router_live.spend("nvidia", policy=_policy())


def test_groq_models_metadata():
    key, secret_values = _key("FREE_ROUTER_GROQ_API_KEY")
    _budget_guard("groq")
    try:
        payload = router_live.http_get_json(
            "https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"}
        )
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for groq; resume tomorrow")
    assert payload.get("data"), "Groq models list is empty"
    _record("groq", payload, secret_values)
    router_live.spend("groq", policy=_policy())


def test_gemini_models_metadata():
    key, secret_values = _key("FREE_ROUTER_GEMINI_API_KEY")
    _budget_guard("gemini")
    try:
        payload = router_live.http_get_json(
            "https://generativelanguage.googleapis.com/v1beta/models",
            {"x-goog-api-key": key},
        )
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for gemini; resume tomorrow")
    assert payload.get("models"), "Gemini models list is empty"
    _record("gemini", payload, secret_values)
    router_live.spend("gemini", policy=_policy())


def test_artificial_analysis_free_endpoint():
    key, secret_values = _key("FREE_ROUTER_AA_API_KEY")
    _budget_guard("artificial_analysis")
    try:
        payload = router_live.http_get_json(
            "https://artificialanalysis.ai/api/v2/language/models/free",
            {"x-api-key": key},
        )
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for artificial_analysis; resume tomorrow")
    assert payload, "Artificial Analysis free endpoint returned an empty object"
    _record("artificial-analysis", payload, secret_values)
    router_live.spend("artificial_analysis", policy=_policy())


def test_huggingface_token_identity():
    key, secret_values = _key("FREE_ROUTER_HF_TOKEN")
    _budget_guard("huggingface")
    try:
        payload = router_live.http_get_json(
            "https://huggingface.co/api/whoami-v2", {"Authorization": f"Bearer {key}"}
        )
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for huggingface; resume tomorrow")
    assert payload.get("name"), "Hugging Face whoami returned no identity"
    _record("huggingface", payload, secret_values)
    router_live.spend("huggingface", policy=_policy())


def test_opencode_models_without_credential():
    headers: dict[str, str] = {}
    router_live.assert_no_auth(headers)
    _budget_guard("opencode")
    try:
        payload = router_live.http_get_json("https://opencode.ai/inference/v1/models", headers)
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for opencode; resume tomorrow")
    assert payload.get("data"), "OpenCode models list is empty"
    router_live.record_fixture(
        "opencode-metadata.json", json.dumps(payload, indent=2), FIXTURES
    )
    router_live.spend("opencode", policy=_policy())


def test_openrouter_credits_unchanged():
    if _USAGE_BEFORE is None:
        pytest.skip("openrouter quota test did not run")
    key, _secret_values = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    _budget_guard("openrouter")
    try:
        payload = router_live.http_get_json(
            "https://openrouter.ai/api/v1/key", {"Authorization": f"Bearer {key}"}
        )
    except router_live.LiveRateLimited:
        pytest.skip("real 429 for openrouter; resume tomorrow")
    info = router_live.parse_openrouter_key_info(payload)
    router_live.assert_usage_unchanged(_USAGE_BEFORE, info.usage)
    router_live.spend("openrouter", policy=_policy())
    assert router_live.read_budget().used.get("openrouter", 0) >= 2
