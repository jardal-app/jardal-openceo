"""Live benchmark sources: one AA query and one LMArena download (TASK-2026-00290).

Runs only with ``FREE_ROUTER_LIVE=1``. One metadata call to Artificial
Analysis (of its 100 daily) plus two to Hugging Face (splits discovery
and one rows slice); normalization, aliases and families run over the
real data. A real 429 skips that provider until the next UTC day.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from model_routing import benchmarks as router_benchmarks
from model_routing import live as router_live
from model_routing import secrets as router_secrets
from model_routing import sources as router_sources
from model_routing.config import ConfigError, load_policy

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"
AA_RECORDED = FIXTURES / "artificial-analysis-metadata.json"

_AA = None
_TARGET = None

_AA_ATTRIBUTION = "Benchmark data by Artificial Analysis (artificialanalysis.ai)"


def _policy():
    try:
        return load_policy()
    except ConfigError:
        return None


def _budget_guard(provider: str) -> None:
    budget = router_live.read_budget()
    if budget.used.get(provider, 0) >= router_live.effective_cap(provider, _policy()):
        pytest.skip(f"daily live budget exhausted for {provider}")


def _key(name: str) -> tuple[str, list[str]]:
    try:
        found = router_secrets.load_router_secrets()
    except router_secrets.SecretsError as exc:
        pytest.skip(f"router secrets unavailable: {exc}")
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


def test_aa_page_live():
    global _AA
    key, secret_values = _key("FREE_ROUTER_AA_API_KEY")
    _budget_guard("artificial_analysis")
    with _skip_on_429("artificial_analysis"):
        raw = router_benchmarks.fetch_aa_models(key)
    parsed = router_benchmarks.parse_aa_models(raw)
    assert parsed.index_version > 0
    assert parsed.models, "AA free endpoint returned no models"
    assert any(
        model.intelligence is not None or model.coding is not None or model.agentic is not None
        for model in parsed.models
    )
    recorded = json.loads(AA_RECORDED.read_text(encoding="utf-8"))
    router_benchmarks.assert_same_aa_major(
        float(recorded["intelligence_index_version"]), parsed.index_version
    )
    _AA = parsed
    aliases = router_benchmarks.load_aliases()
    qualities, unresolved = router_benchmarks.build_quality(
        aliases, router_live.today_utc(), aa=parsed
    )
    assert len(qualities) == len(aliases.aliases)
    assert all(item.reason for item in unresolved)
    _record(
        "benchmarks-aa-page1.json",
        {
            "attribution": _AA_ATTRIBUTION,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "response": raw,
        },
        secret_values,
    )
    router_live.spend("artificial_analysis", policy=_policy())


def test_lmarena_splits_live():
    global _TARGET
    token, secret_values = _key("FREE_ROUTER_HF_TOKEN")
    _budget_guard("huggingface")
    with _skip_on_429("lmarena"):
        raw = router_benchmarks.fetch_lmarena_splits(token)
    _record("benchmarks-lmarena-splits.json", raw, secret_values)
    catalog = router_benchmarks.parse_lmarena_splits(raw)
    assert catalog.splits, "datasets-server returned no splits"
    _TARGET = router_benchmarks.select_lmarena_target(catalog)
    assert _TARGET.config in router_benchmarks.WANTED_LMARENA_CONFIGS
    router_live.spend("huggingface", policy=_policy())


def test_lmarena_rows_live():
    if _TARGET is None:
        pytest.skip("splits test did not select a target")
    token, secret_values = _key("FREE_ROUTER_HF_TOKEN")
    _budget_guard("huggingface")
    with _skip_on_429("lmarena"):
        raw = router_benchmarks.fetch_lmarena_rows(token, _TARGET.config, _TARGET.split)
    serialized = json.dumps(raw)
    if len(serialized) > 200_000:
        rows = raw.get("rows") if isinstance(raw.get("rows"), list) else []
        _record(
            "benchmarks-lmarena-rows.json",
            {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "config": _TARGET.config,
                "split": _TARGET.split,
                "truncated_for_fixture": True,
                "total_rows_received": len(rows),
                "features": raw.get("features"),
                "rows": rows[:10],
            },
            secret_values,
        )
    else:
        _record("benchmarks-lmarena-rows.json", raw, secret_values)
    parsed = router_benchmarks.parse_lmarena_rows(raw, _TARGET.config, _TARGET.split)
    assert parsed.observations, "rows slice carried no usable per-model scores"
    aliases = router_benchmarks.load_aliases()
    qualities, unresolved = router_benchmarks.build_quality(
        aliases, router_live.today_utc(), aa=_AA, arena=parsed
    )
    assert len(qualities) == len(aliases.aliases)
    assert all(item.reason for item in unresolved)
    router_live.spend("huggingface", policy=_policy())
