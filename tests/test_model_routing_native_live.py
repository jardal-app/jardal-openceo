"""Native OpenCode contract live checks (TASK-2026-00304).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole file.
Reads the installed instance catalog without generating and contrasts it
with the router catalog; runs one minimal native chat probe when the
Free quota allows. A real FreeUsageLimitError records the block and
skips without retrying.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from model_routing import live as router_live
from model_routing import probes as router_probes
from model_routing import secrets as router_secrets

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"


def _redaction_secrets() -> list[str]:
    try:
        found = router_secrets.load_router_secrets()
    except router_secrets.SecretsError:
        return []
    return [value for value in vars(found).values() if isinstance(value, str) and value]


def _record(name: str, payload: dict, secret_values: list[str]) -> None:
    router_live.record_fixture(name, json.dumps(payload, indent=2), FIXTURES, secret_values)


def test_instance_catalog_read_live():
    secret_values = _redaction_secrets()
    catalog = router_probes.read_instance_models()
    assert catalog.models, "instance listed no models"
    assert ("opencode", "big-pickle") in catalog.models
    recorded = json.loads((FIXTURES / "sources-opencode-catalog.json").read_text(encoding="utf-8"))
    router_ids = {
        item["id"] for item in recorded.get("data", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    instance_ids = {model for provider, model in catalog.models if provider == "opencode"}
    overlap = sorted(router_ids & instance_ids)
    assert "big-pickle" in overlap, "router and instance catalogs share no verified model"
    _record(
        "native-instance-catalog.json",
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source": catalog.source,
            "instance_models": len(catalog.models),
            "router_models": len(router_ids),
            "overlap": len(overlap),
            "overlap_ids": overlap,
            "instance_only": sorted(instance_ids - router_ids),
            "router_only": sorted(router_ids - instance_ids),
        },
        secret_values,
    )


def test_native_chat_probe_live():
    secret_values = _redaction_secrets()
    try:
        result, raw = router_probes.run_native_probe(
            "opencode", "chat", "big-pickle", enabled=True
        )
    except router_live.LiveRateLimited:
        _record(
            "native-opencode-blocked.json",
            {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "provider": "opencode",
                "note": "real FreeUsageLimitError; native group blocked, no retry",
            },
            secret_values,
        )
        pytest.skip("real 429 for opencode; resume tomorrow")
    assert result.deployment_id == "opencode/big-pickle"
    texts = [
        event["part"]["text"]
        for event in raw["events"]
        if event.get("type") == "text" and isinstance(event.get("part"), dict)
    ]
    assert " ".join(texts).strip(), "native chat carried no text"
    _record(
        "native-opencode-chat.json",
        {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "model": "opencode/big-pickle",
            "deployment_id": result.deployment_id,
            "observed_at": result.observed_at,
            "event_types": sorted({event.get("type") for event in raw["events"]}),
            "events": raw["events"],
        },
        secret_values,
    )
