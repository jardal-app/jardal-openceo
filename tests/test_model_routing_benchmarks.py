"""Benchmark sources, normalization, aliases and families (TASK-2026-00290). Offline only."""

from __future__ import annotations

import urllib.error
from email.message import Message

import pytest

from model_routing import benchmarks as router_benchmarks
from model_routing import sources as router_sources
from model_routing.models import CatalogDocument, CatalogEntry, Deployment


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


def _aa_payload(*items, version=4.3, page=1, total_pages=4, has_more=True):
    return {
        "tier": "free",
        "intelligence_index_version": version,
        "pagination": {
            "page": page,
            "page_size": 200,
            "total_pages": total_pages,
            "has_more": has_more,
        },
        "data": list(items),
    }


def _aa_model(slug, intelligence=None, coding=None, agentic=None, creator="Example"):
    return {
        "id": f"id-{slug}",
        "name": slug,
        "slug": slug,
        "model_creator": {"name": creator},
        "evaluations": {
            "artificial_analysis_intelligence_index": intelligence,
            "artificial_analysis_coding_index": coding,
            "artificial_analysis_agentic_index": agentic,
        },
    }


def _splits_payload(*pairs, pending=None, failed=None):
    return {
        "splits": [{"dataset": "d", "config": config, "split": split} for config, split in pairs],
        "pending": pending or [],
        "failed": failed or [],
    }


def _rows_payload(*rows, features=("model", "score"), extra_features=()):
    names = [*(f for f in features), *extra_features]
    return {
        "features": [{"feature_idx": idx, "name": name} for idx, name in enumerate(names)],
        "rows": [
            {"row_idx": idx, "row": dict(row) if isinstance(row, dict) else row, "truncated_cells": []}
            for idx, row in enumerate(rows)
        ],
    }


def _alias_file_text(*entries, exceptions=(), verified_at="2026-10-23"):
    lines = ["schema_version: 1", f'verified_at: "{verified_at}"', "aliases:"]
    for entry in entries:
        lines.append(f"  - canonical_id: {entry['canonical_id']}")
        lines.append(f"    family_id: {entry['family_id']}")
        for vendor, ids in entry.get("providers", {}).items():
            lines.append(f"    providers: {{{vendor}: [{', '.join(ids)}]}}")
        if entry.get("aa_slugs"):
            lines.append(f"    aa_slugs: [{', '.join(entry['aa_slugs'])}]")
        if entry.get("lmarena_names"):
            lines.append(f"    lmarena_names: [{', '.join(entry['lmarena_names'])}]")
    lines.append("exceptions:" if exceptions else "exceptions: []")
    for override in exceptions:
        lines.append(f"  - vendor: {override['vendor']}")
        lines.append(f"    external_id: {override['external_id']}")
        lines.append(f"    canonical_id: {override['canonical_id']}")
    return "\n".join(lines) + "\n"


@pytest.fixture()
def tiny_aliases(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text(
        _alias_file_text(
            {
                "canonical_id": "example/alpha",
                "family_id": "example",
                "providers": {"openrouter": ["example/alpha:free"]},
                "aa_slugs": ["alpha"],
                "lmarena_names": ["Alpha"],
            },
            {
                "canonical_id": "example/beta",
                "family_id": "example",
                "providers": {"groq": ["example/beta"]},
                "aa_slugs": ["beta"],
            },
            {
                "canonical_id": "other/gamma",
                "family_id": "other",
                "providers": {"nvidia": ["other/gamma"]},
            },
        ),
        encoding="utf-8",
    )
    return router_benchmarks.load_aliases(path)


# Artificial Analysis fetch and parse


def test_aa_fetch_sends_key_and_page():
    seen: list = []
    get = _fake_get(_aa_payload(), seen=seen)
    router_benchmarks.fetch_aa_models("secret", get)
    url, headers = seen[0]
    assert url == router_benchmarks.AA_FREE_URL
    assert headers["x-api-key"] == "secret"
    assert "secret" not in url
    router_benchmarks.fetch_aa_models("secret", get, page=3)
    assert seen[1][0] == router_benchmarks.AA_FREE_URL + "?page=3"
    with pytest.raises(ValueError):
        router_benchmarks.fetch_aa_models("secret", get, page=0)


def test_aa_fetch_errors_map_to_source_kinds():
    payload = _aa_payload()
    with pytest.raises(router_sources.SourceError) as exc:
        router_benchmarks.fetch_aa_models("k", _fake_get(payload, code=401))
    assert exc.value.kind == "auth"
    with pytest.raises(router_sources.SourceError) as exc:
        router_benchmarks.fetch_aa_models("k", _fake_get(payload, code=429, retry_after="60"))
    assert exc.value.kind == "rate_limited"
    assert exc.value.retry_after == "60"
    assert exc.value.retryable
    with pytest.raises(router_sources.SourceError) as exc:
        router_benchmarks.fetch_aa_models("k", _fake_get(payload, code=503))
    assert exc.value.kind == "server"
    assert "x-api-key" not in str(exc.value)


def test_aa_parse_indexes_and_pagination():
    parsed = router_benchmarks.parse_aa_models(
        _aa_payload(
            _aa_model("alpha", intelligence=10.0, coding=20.0, agentic=30.0, creator="Example"),
            _aa_model("beta", intelligence=5.0),
        )
    )
    assert parsed.index_version == 4.3
    assert parsed.page == 1
    assert parsed.total_pages == 4
    assert parsed.has_more is True
    assert parsed.skipped == 0
    alpha = parsed.models[0]
    assert (alpha.slug, alpha.creator) == ("alpha", "Example")
    assert (alpha.intelligence, alpha.coding, alpha.agentic) == (10.0, 20.0, 30.0)
    assert parsed.models[1].coding is None


def test_aa_parse_skips_bad_rows_but_requires_version():
    parsed = router_benchmarks.parse_aa_models(
        _aa_payload("nope", {"name": "no-slug"}, _aa_model("ok", intelligence="bad"))
    )
    assert parsed.skipped == 2
    assert parsed.models[0].intelligence is None
    with pytest.raises(router_sources.SourceError):
        router_benchmarks.parse_aa_models({"data": []})
    with pytest.raises(router_sources.SourceError):
        router_benchmarks.parse_aa_models({"intelligence_index_version": 4.3})


def test_aa_major_guard_refuses_cross_major_comparison():
    router_benchmarks.assert_same_aa_major(4.3, 4.9)
    assert router_benchmarks.aa_major(4.3) == 4
    with pytest.raises(ValueError, match="major changed"):
        router_benchmarks.assert_same_aa_major(4.3, 5.0)


# LMArena splits and rows


def test_splits_fetch_uses_bearer_token():
    seen: list = []
    get = _fake_get(_splits_payload(("agent", "train")), seen=seen)
    router_benchmarks.fetch_lmarena_splits("token", get)
    url, headers = seen[0]
    assert url == router_benchmarks.LMARENA_SPLITS_URL
    assert headers["Authorization"] == "Bearer token"


def test_splits_parse_and_target_selection():
    catalog = router_benchmarks.parse_lmarena_splits(
        _splits_payload(
            ("webdev", "train"),
            ("agent", "validation"),
            ("agent", "train"),
            pending=[{"dataset": "d", "config": "search", "split": "train"}],
            failed=["junk"],
        )
    )
    assert len(catalog.splits) == 3
    assert catalog.pending == ["search/train"]
    assert catalog.failed == []
    target = router_benchmarks.select_lmarena_target(catalog)
    assert (target.config, target.split) == ("agent", "train")
    only_webdev = router_benchmarks.parse_lmarena_splits(_splits_payload(("webdev", "test")))
    target = router_benchmarks.select_lmarena_target(only_webdev)
    assert (target.config, target.split) == ("webdev", "test")


def test_splits_target_missing_names_available_configs():
    catalog = router_benchmarks.parse_lmarena_splits(_splits_payload(("other", "train")))
    with pytest.raises(router_sources.SourceError, match="other"):
        router_benchmarks.select_lmarena_target(catalog)
    with pytest.raises(router_sources.SourceError):
        router_benchmarks.parse_lmarena_splits({"pending": []})
    with pytest.raises(router_sources.SourceError):
        router_benchmarks.parse_lmarena_splits({"splits": [{"config": "agent"}]})


def test_rows_fetch_validates_slice_bounds():
    seen: list = []
    get = _fake_get(_rows_payload({"model": "a", "score": 1.0}), seen=seen)
    router_benchmarks.fetch_lmarena_rows("t", "agent", "train", get, length=10, offset=5)
    url, headers = seen[0]
    assert "config=agent" in url and "split=train" in url
    assert "length=10" in url and "offset=5" in url
    assert headers["Authorization"] == "Bearer t"
    with pytest.raises(ValueError):
        router_benchmarks.fetch_lmarena_rows("t", "agent", "train", get, length=101)
    with pytest.raises(ValueError):
        router_benchmarks.fetch_lmarena_rows("t", "agent", "train", get, offset=-1)


def test_rows_parse_agent_intervals_and_text_ratings():
    agent = router_benchmarks.parse_lmarena_rows(
        _rows_payload(
            {"model": "Alpha", "score": 1200.0, "ci_low": 1180.0, "ci_high": 1220.0},
            {"model": "", "score": 1.0},
            {"model": "Beta", "score": "bad"},
            "junk",
            features=("model", "score"),
            extra_features=("ci_low", "ci_high"),
        ),
        "agent",
        "train",
    )
    assert agent.features == ["ci_high", "ci_low", "model", "score"]
    assert agent.skipped_rows == 3
    (observation,) = agent.observations
    assert observation.kind == "agent_arena"
    assert (observation.lower, observation.upper) == (1180.0, 1220.0)
    live_shaped = router_benchmarks.parse_lmarena_rows(
        _rows_payload(
            {"model_name": "M", "score": 0.5, "score_ci_lower": 0.4, "score_ci_upper": 0.6},
            features=("model_name", "score"),
            extra_features=("score_ci_lower", "score_ci_upper"),
        ),
        "agent",
        "latest",
    )
    assert (live_shaped.observations[0].lower, live_shaped.observations[0].upper) == (0.4, 0.6)
    text = router_benchmarks.parse_lmarena_rows(
        _rows_payload({"model_name": "Alpha", "rating": 42.0}, features=("model_name", "rating")),
        "webdev",
        "train",
    )
    assert text.observations[0].kind == "text_ranking"
    assert text.observations[0].lower is None


def test_rows_parse_unknown_schema_fails_closed_with_columns():
    with pytest.raises(router_sources.SourceError, match="prompt"):
        router_benchmarks.parse_lmarena_rows(
            _rows_payload({"prompt": "hi"}, features=("prompt",)),
            "agent",
            "train",
        )
    with pytest.raises(router_sources.SourceError):
        router_benchmarks.parse_lmarena_rows({"features": []}, "agent", "train")


# Normalization


def test_percentile_ranks_within_cohort():
    assert router_benchmarks.percentile([1.0, 2.0, 3.0, 4.0], 3.0) == pytest.approx(0.625)
    assert router_benchmarks.percentile([5.0, 5.0, 5.0], 5.0) == pytest.approx(0.5)
    assert router_benchmarks.percentile([9.0], 9.0) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        router_benchmarks.percentile([], 1.0)


# Aliases and families


def test_seed_alias_file_loads():
    aliases = router_benchmarks.load_aliases()
    assert aliases.schema_version == 1
    assert len(aliases.aliases) >= 20
    hit = router_benchmarks.resolve_canonical("openrouter", "cohere/north-mini-code:free", aliases)
    assert (hit.canonical_id, hit.family_id, hit.via) == (
        "cohere/north-mini-code",
        "cohere",
        "openrouter",
    )


def test_explicit_alias_maps_and_via(tiny_aliases):
    assert router_benchmarks.resolve_canonical(
        "openrouter", "example/alpha:free", tiny_aliases
    ).via == "openrouter"
    assert router_benchmarks.resolve_canonical("aa", "alpha", tiny_aliases).via == "aa"
    assert router_benchmarks.resolve_canonical("lmarena", "Alpha", tiny_aliases).via == "lmarena"
    with pytest.raises(router_benchmarks.AliasError, match="no alias entry"):
        router_benchmarks.resolve_canonical("openrouter", "example/alpha", tiny_aliases)
    with pytest.raises(router_benchmarks.AliasError, match="no alias entry"):
        router_benchmarks.resolve_canonical("aa", "alpha-2", tiny_aliases)
    with pytest.raises(router_benchmarks.AliasError, match="no alias entry"):
        router_benchmarks.resolve_canonical("lmarena", "alpha", tiny_aliases)


def test_no_fuzzy_matching(tiny_aliases):
    assert router_benchmarks.try_resolve("aa", "alph", tiny_aliases) is None
    assert router_benchmarks.try_resolve("aa", "ALPHA", tiny_aliases) is None
    assert router_benchmarks.try_resolve("lmarena", "Alpha ", tiny_aliases) is None
    assert router_benchmarks.try_resolve("openrouter", "example/alpha:free ", tiny_aliases) is None
    assert router_benchmarks.try_resolve("unknown-vendor", "alpha", tiny_aliases) is None


def test_exceptions_win_over_maps(tmp_path):
    path = tmp_path / "aliases.yaml"
    path.write_text(
        _alias_file_text(
            {
                "canonical_id": "example/alpha",
                "family_id": "example",
                "providers": {"openrouter": ["example/shared"]},
            },
            {
                "canonical_id": "other/alpha",
                "family_id": "other",
            },
            exceptions=[
                {
                    "vendor": "openrouter",
                    "external_id": "example/shared",
                    "canonical_id": "other/alpha",
                }
            ],
        ),
        encoding="utf-8",
    )
    aliases = router_benchmarks.load_aliases(path)
    hit = router_benchmarks.resolve_canonical("openrouter", "example/shared", aliases)
    assert (hit.canonical_id, hit.via) == ("other/alpha", "exception")


def test_alias_loader_rejects_bad_files(tmp_path):
    with pytest.raises(router_benchmarks.AliasError):
        router_benchmarks.load_aliases(tmp_path / "missing.yaml")
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("aliases: [unclosed", encoding="utf-8")
    with pytest.raises(router_benchmarks.AliasError):
        router_benchmarks.load_aliases(bad_yaml)
    dupe = tmp_path / "dupe.yaml"
    dupe.write_text(
        _alias_file_text(
            {"canonical_id": "a/m", "family_id": "a", "aa_slugs": ["s"]},
            {"canonical_id": "b/m", "family_id": "b", "aa_slugs": ["s"]},
        ),
        encoding="utf-8",
    )
    with pytest.raises(router_benchmarks.AliasError, match="maps twice"):
        router_benchmarks.load_aliases(dupe)
    dangling = tmp_path / "dangling.yaml"
    dangling.write_text(
        _alias_file_text(
            {"canonical_id": "a/m", "family_id": "a"},
            exceptions=[{"vendor": "aa", "external_id": "s", "canonical_id": "ghost/m"}],
        ),
        encoding="utf-8",
    )
    with pytest.raises(router_benchmarks.AliasError, match="dangles"):
        router_benchmarks.load_aliases(dangling)
    bad_date = tmp_path / "date.yaml"
    bad_date.write_text(
        _alias_file_text({"canonical_id": "a/m", "family_id": "a"}, verified_at="yesterday"),
        encoding="utf-8",
    )
    with pytest.raises(router_benchmarks.AliasError):
        router_benchmarks.load_aliases(bad_date)


def test_conservative_family_resolution(tiny_aliases):
    assert router_benchmarks.resolve_family("example/alpha", tiny_aliases) == "example"
    assert (
        router_benchmarks.resolve_family("ghost/model", tiny_aliases)
        == router_benchmarks.UNKNOWN_FAMILY
    )
    assert router_benchmarks.distinct_families("example", "other") is True
    assert router_benchmarks.distinct_families("example", "example") is False
    assert (
        router_benchmarks.distinct_families(router_benchmarks.UNKNOWN_FAMILY, "other") is False
    )
    assert (
        router_benchmarks.distinct_families(
            router_benchmarks.UNKNOWN_FAMILY, router_benchmarks.UNKNOWN_FAMILY
        )
        is False
    )


# Quality merge and experimental bucket


def test_build_quality_merges_sources_with_degraded_confidence(tiny_aliases):
    aa = router_benchmarks.parse_aa_models(
        _aa_payload(
            _aa_model("alpha", intelligence=10.0, coding=20.0, agentic=30.0),
            _aa_model("beta", intelligence=5.0),
            _aa_model("ghost", intelligence=1.0),
        )
    )
    arena = router_benchmarks.parse_lmarena_rows(
        _rows_payload({"model": "Alpha", "score": 1200.0}, {"model": "Ghost", "score": 1.0}),
        "agent",
        "train",
    )
    qualities, unresolved = router_benchmarks.build_quality(
        tiny_aliases, "2026-10-23", aa=aa, arena=arena
    )
    by_id = {quality.canonical_model_id: quality for quality in qualities}
    alpha = by_id["example/alpha"]
    assert alpha.family_id == "example"
    assert alpha.experimental is False
    assert alpha.observations == 4
    assert set(alpha.dimensions) == {"intelligence", "coding", "agentic", "arena"}
    assert alpha.dimensions["intelligence"].confidence == "full"
    assert alpha.dimensions["intelligence"].coverage == pytest.approx(1.0)
    assert "artificial-analysis:index4.3@2026-10-23" in alpha.sources
    assert "lmarena:agent/train@2026-10-23" in alpha.sources
    beta = by_id["example/beta"]
    assert beta.dimensions["intelligence"].confidence == "degraded"
    assert beta.dimensions["intelligence"].coverage == pytest.approx(1 / 3)
    assert beta.experimental is True
    assert "1 more needed" in beta.experimental_reason
    gamma = by_id["other/gamma"]
    assert gamma.experimental is True
    assert gamma.observations == 0
    assert gamma.experimental_reason == "no benchmark observations from any source"
    vendors = {(item.vendor, item.external_id) for item in unresolved}
    assert ("aa", "ghost") in vendors
    assert ("lmarena", "Ghost") in vendors


def test_build_quality_without_sources_marks_everything_experimental(tiny_aliases):
    qualities, unresolved = router_benchmarks.build_quality(tiny_aliases, "2026-10-23")
    assert len(qualities) == 3
    assert all(quality.experimental for quality in qualities)
    assert unresolved == []


def test_find_unresolved_reports_each_namespace(tiny_aliases):
    aa = router_benchmarks.parse_aa_models(_aa_payload(_aa_model("ghost")))
    arena = router_benchmarks.parse_lmarena_rows(
        _rows_payload({"model": "Ghost", "score": 1.0}), "agent", "train"
    )
    missing = router_benchmarks.find_unresolved(
        tiny_aliases,
        aa=aa,
        arena=arena,
        provider_ids={"openrouter": ["example/alpha:free", "ghost/model:free"]},
    )
    assert {(item.vendor, item.external_id) for item in missing} == {
        ("aa", "ghost"),
        ("lmarena", "Ghost"),
        ("openrouter", "ghost/model:free"),
    }
    assert all(item.reason for item in missing)


def test_apply_families_fills_known_and_keeps_unknown(tiny_aliases):
    document = CatalogDocument(
        entries=[
            CatalogEntry(
                family_id=router_benchmarks.UNKNOWN_FAMILY,
                canonical_model_id="openrouter/example/alpha:free",
                deployments=[
                    Deployment(
                        deployment_id="openrouter/example/alpha:free",
                        provider="openrouter",
                        route_type="external_free",
                        allowed_client="configured_opencode_or_gateway",
                        provider_id="openrouter",
                        model_id="example/alpha:free",
                        gateway_model_id="example/alpha:free",
                    )
                ],
            ),
            CatalogEntry(
                family_id=router_benchmarks.UNKNOWN_FAMILY,
                canonical_model_id="openrouter/ghost",
                deployments=[
                    Deployment(
                        deployment_id="openrouter/ghost",
                        provider="openrouter",
                        route_type="external_free",
                        allowed_client="configured_opencode_or_gateway",
                        provider_id="openrouter",
                        model_id="ghost",
                        gateway_model_id="ghost/model:free",
                    )
                ],
            ),
        ]
    )
    updated = router_benchmarks.apply_families(document, tiny_aliases)
    assert updated.entries[0].family_id == "example"
    assert updated.entries[0].canonical_model_id == "openrouter/example/alpha:free"
    assert updated.entries[1].family_id == router_benchmarks.UNKNOWN_FAMILY
    assert document.entries[0].family_id == router_benchmarks.UNKNOWN_FAMILY
    with pytest.raises(router_benchmarks.AliasError):
        router_benchmarks.apply_families({"entries": []}, tiny_aliases)


def test_apply_families_tries_each_deployment(tiny_aliases):
    document = CatalogDocument(
        entries=[
            CatalogEntry(
                family_id=router_benchmarks.UNKNOWN_FAMILY,
                canonical_model_id="multi/alpha",
                deployments=[
                    Deployment(
                        deployment_id="openrouter/ghost",
                        provider="openrouter",
                        route_type="external_free",
                        allowed_client="configured_opencode_or_gateway",
                        provider_id="openrouter",
                        model_id="ghost",
                        gateway_model_id=None,
                    ),
                    Deployment(
                        deployment_id="openrouter/alpha",
                        provider="openrouter",
                        route_type="external_free",
                        allowed_client="configured_opencode_or_gateway",
                        provider_id="openrouter",
                        model_id="alpha",
                        gateway_model_id="example/alpha:free",
                    ),
                ],
            ),
        ]
    )
    updated = router_benchmarks.apply_families(document, tiny_aliases)
    assert updated.entries[0].family_id == "example"
