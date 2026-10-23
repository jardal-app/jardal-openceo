"""Live provider and benchmark refreshes in temp dirs (TASK-2026-00292).

Runs only with ``FREE_ROUTER_LIVE=1``; default runs skip the whole
file. Metadata only, no generation: one ``update --providers`` and one
``update --benchmarks`` per test into throwaway directories. A real
429 skips until the next UTC day. Never prints secrets: assertions touch published snapshots only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_routing import live as router_live
from model_routing import ranking as router_ranking
from model_routing import refresh as router_refresh
from model_routing import secrets as router_secrets
from model_routing import sources as router_sources
from model_routing.publish import load_catalog_doc

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"



def _secrets():
    try:
        return router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")


def _key(name: str) -> str:
    value = getattr(_secrets(), name)
    if not value:
        pytest.skip(f"{name} is unset")
    return value


def _secret_values() -> list[str]:
    found = _secrets()
    return [value for value in vars(found).values() if isinstance(value, str) and value]


def _skip_on_real_429(report) -> None:
    throttled = [failure for failure in report.failed if "429" in failure]
    if throttled:
        pytest.skip(f"real 429 during refresh, resume tomorrow: {throttled[0][:120]}")


def _key_usage(key: str) -> float:
    payload = router_sources.fetch_openrouter_key(key)
    return router_live.parse_openrouter_key_info(payload).usage


def _assert_tops_comply(config: Path) -> None:
    catalog = load_catalog_doc(config / "catalog.json")
    rankings = router_ranking.load_rankings(config / "rankings.json")
    by_id = {entry.canonical_model_id: entry for entry in catalog.entries}
    for profile in ("free-review", "free-research", "free-general"):
        ranking = rankings.profiles[profile]
        for item in ranking.order[:3]:
            entry = by_id[item.canonical_model_id]
            if profile in ("free-review", "free-research"):
                assert entry.capabilities.tools is True, item.canonical_model_id
            floor = ranking.provenance.context_floor
            if floor is not None:
                assert (entry.capabilities.context_tokens or 0) >= floor


def test_update_providers_live(tmp_path):
    key = _key("FREE_ROUTER_OPENROUTER_API_KEY")
    config = tmp_path / "config"
    config.mkdir()
    usage_before = _key_usage(key)
    report = router_refresh.update_providers(config)
    _skip_on_real_429(report)
    assert report.stale is False, "; ".join(report.failed)
    router_live.assert_usage_unchanged(usage_before, _key_usage(key))
    rankings = router_ranking.load_rankings(config / "rankings.json")
    assert rankings.catalog_snapshot == report.catalog_snapshot
    catalog = load_catalog_doc(config / "catalog.json")
    assert catalog.entries, "empty live catalog"
    general = rankings.profiles["free-general"]
    assert not general.order, "providers-only refresh must not invent quality order"
    _assert_tops_comply(config)
    router_live.record_fixture(
        "update-rankings-providers-live.json",
        (config / "rankings.json").read_text(encoding="utf-8"),
        FIXTURES, _secret_values())


def test_update_benchmarks_live(tmp_path):
    _key("FREE_ROUTER_AA_API_KEY")
    _key("FREE_ROUTER_HF_TOKEN")
    config = tmp_path / "config"
    config.mkdir()
    providers_report = router_refresh.update_providers(config)
    _skip_on_real_429(providers_report)
    assert providers_report.stale is False, "; ".join(providers_report.failed)
    report = router_refresh.update_benchmarks(config)
    _skip_on_real_429(report)
    assert report.failed == (), "; ".join(report.failed)
    rankings = router_ranking.load_rankings(config / "rankings.json")
    general = rankings.profiles["free-general"]
    assert general.provenance.aa_index_version is not None
    assert general.provenance.lmarena_target is not None
    assert general.order, "empty general order after benchmarks"
    assert general.order[0].dimensions, "top entry carries no quality dimensions"
    _assert_tops_comply(config)
    router_live.record_fixture(
        "update-rankings-benchmarks-live.json",
        json.dumps(json.loads((config / "rankings.json").read_text(encoding="utf-8")),
                   indent=2),
        FIXTURES, _secret_values())
