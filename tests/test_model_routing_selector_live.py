"""native_first selector live checks (TASK-2026-00305).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole file.
The installed OpenCode accepts the generated group B config without
generating, and one minimal real call answers through an external
provider configured in OpenCode, with OpenRouter credit usage
unchanged. A real 429 records the block and skips without retrying.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from model_routing import external as router_external
from model_routing import live as router_live
from model_routing import probes as router_probes
from model_routing import secrets as router_secrets
from model_routing import sources as router_sources
from model_routing import terms as router_terms
from model_routing.models import (
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Confidentiality,
    Deployment,
)

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"

_PREFERRED_MODEL = "recursive/flint-v1.0:free"


def _secret_values() -> list[str]:
    try:
        found = router_secrets.load_router_secrets()
    except router_secrets.SecretsError:
        return []
    return [value for value in vars(found).values() if isinstance(value, str) and value]


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


def _external_dep(provider: str, model_id: str) -> Deployment:
    return Deployment(
        deployment_id=f"{provider}/{model_id}",
        provider=provider,
        route_type="external_free",
        allowed_client="configured_opencode_or_gateway",
        provider_id=provider,
        model_id=model_id,
        gateway_model_id=model_id,
        free_offered=True,
        free_verified=True,
        healthy=True,
        input_price_usd=0.0,
        output_price_usd=0.0,
        approved_confidentiality=[Confidentiality.PUBLIC],
        source="selector-live-test",
        observed_at=datetime.now(timezone.utc),
    )


def _single_model_catalog(provider: str, model_id: str) -> CatalogDocument:
    return CatalogDocument(
        generated_at=datetime.now(timezone.utc),
        generator="selector-live-test",
        entries=[
            CatalogEntry(
                family_id="live-family",
                canonical_model_id=f"live/{model_id}",
                display_name=model_id,
                capabilities=Capabilities(tools=True, context_tokens=131072),
                deployments=[_external_dep(provider, model_id)],
            )
        ],
    )


@contextmanager
def _git_project(config: dict, secret_values: list[str]):
    with tempfile.TemporaryDirectory(prefix="free-router-group-b-") as workdir:
        directory = Path(workdir)
        (directory / "opencode.json").write_text(
            router_live.redact_text(json.dumps(config, indent=2), secret_values),
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=directory, check=True, timeout=60)
        yield directory


def test_group_b_config_accepted_live():
    secret_values = _secret_values()
    catalog = _single_model_catalog("openrouter", "vendor/probe:free")
    config = router_external.build_external_providers_config("v2", catalog)
    leaks = router_live.scan_for_secrets(json.dumps(config), tuple(secret_values))
    assert not [leak for leak in leaks if "key-like" in leak], leaks
    with _git_project(config, secret_values) as project:
        completed = subprocess.run(
            ["opencode", "debug", "config"], cwd=project,
            capture_output=True, text=True, timeout=120, check=False)
        assert completed.returncode == 0, completed.stderr[:500]
        documents = json.loads(completed.stdout)
        ours = [doc for doc in documents
                if doc.get("path") == str(project / "opencode.json")]
        assert ours, "installed OpenCode ignored the generated project config"
        resolved = ours[0]["info"]["providers"]["openrouter"]
        assert resolved["settings"]["baseURL"] == "https://openrouter.ai/api/v1"
        assert set(resolved["models"]) == {"vendor/probe:free"}
        _record(
            "selector-group-b-config.json",
            {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "resolved_provider": "openrouter",
                "resolved_models": sorted(resolved["models"]),
                "api_key_masked": resolved["settings"].get("apiKey") == "***",
            },
            secret_values,
        )


def test_external_call_through_opencode_live():
    secret_values = _secret_values()
    try:
        secrets = router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")
    key = secrets.FREE_ROUTER_OPENROUTER_API_KEY
    if not key:
        pytest.skip("FREE_ROUTER_OPENROUTER_API_KEY is unset")
    terms = router_terms.load_terms()
    with _skip_on_429("openrouter"):
        raw = router_sources.fetch_openrouter_models()
        key_payload = router_sources.fetch_openrouter_key(key)
    entries = router_sources.parse_openrouter_models(raw, key_payload, terms)
    verified = sorted(
        entry.deployments[0].gateway_model_id
        for entry in entries
        if entry.deployments and entry.deployments[0].free_verified)
    assert verified, "no free_verified :free model in the live catalog"
    model_id = _PREFERRED_MODEL if _PREFERRED_MODEL in verified else verified[0]
    usage_before = router_live.parse_openrouter_key_info(key_payload).usage
    catalog = _single_model_catalog("openrouter", model_id)
    config = router_external.build_external_providers_config("v2", catalog)
    previous = os.environ.get("FREE_ROUTER_OPENROUTER_API_KEY")
    os.environ["FREE_ROUTER_OPENROUTER_API_KEY"] = key
    try:
        with _git_project(config, secret_values) as project:
            try:
                result, raw_events = router_probes.run_native_probe(
                    "openrouter", "chat", model_id, enabled=True,
                    workdir=str(project), standalone=True, timeout=240.0)
            except router_live.LiveRateLimited:
                _record(
                    "selector-openrouter-blocked.json",
                    {
                        "recorded_at": datetime.now(timezone.utc).isoformat(),
                        "model": f"openrouter/{model_id}",
                        "note": "real 429 for openrouter; no retry",
                    },
                    secret_values,
                )
                pytest.skip("real 429 for openrouter; resume tomorrow")
    finally:
        if previous is None:
            os.environ.pop("FREE_ROUTER_OPENROUTER_API_KEY", None)
        else:
            os.environ["FREE_ROUTER_OPENROUTER_API_KEY"] = previous
    assert result.deployment_id == f"openrouter/{model_id}"
    texts = [
        event["part"]["text"]
        for event in raw_events["events"]
        if event.get("type") == "text" and isinstance(event.get("part"), dict)
    ]
    assert " ".join(texts).strip(), "external chat carried no text"
    with _skip_on_429("openrouter"):
        after_payload = router_sources.fetch_openrouter_key(key)
    usage_after = router_live.parse_openrouter_key_info(after_payload).usage
    router_live.assert_usage_unchanged(usage_before, usage_after)
    _record(
        "selector-openrouter-chat.json",
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "model": f"openrouter/{model_id}",
            "deployment_id": result.deployment_id,
            "observed_at": result.observed_at,
            "credit_usage_before": usage_before,
            "credit_usage_after": usage_after,
            "event_types": sorted({event.get("type") for event in raw_events["events"]}),
            "events": raw_events["events"],
        },
        secret_values,
    )
