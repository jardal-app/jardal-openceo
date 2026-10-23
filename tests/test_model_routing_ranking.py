"""Profile ranking, refresh and scheduling (TASK-2026-00292). Offline only."""

from __future__ import annotations

import fcntl
import json
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path

import pytest

from model_routing import benchmarks as router_benchmarks
from model_routing import ranking as router_ranking
from model_routing import refresh as router_refresh
from model_routing import schedule as router_schedule
from model_routing import sources as router_sources
from model_routing import terms as router_terms
from model_routing.secrets import RouterSecrets
from model_routing.availability import ProfileRequirements, meets_profile
from model_routing.benchmarks import AliasEntry, AliasFile
from model_routing.cli import main as cli_main
from model_routing.models import (
    BenchmarkScore,
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Confidentiality,
    Deployment,
    DeploymentLimits,
    ProfileRanking,
    ProfileScoring,
    RankedDeployment,
    RankedEntry,
    RankingProvenance,
    RankingsDocument,
    RouterPolicy,
)
from model_routing.publish import load_catalog_doc, publish_catalog


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dep(deployment_id, provider="openrouter", gateway="example/alpha:free", **overrides):
    base = {
        "deployment_id": deployment_id,
        "provider": provider,
        "gateway_model_id": gateway,
        "free_offered": True,
        "free_verified": True,
        "healthy": True,
        "input_price_usd": 0.0,
        "output_price_usd": 0.0,
        "approved_confidentiality": [Confidentiality.PUBLIC],
        "source": "synthetic",
        "observed_at": _now(),
    }
    base.update(overrides)
    return Deployment(**base)


def _bench(**dims):
    return {name: BenchmarkScore(score=value, source="synthetic") for name, value in dims.items()}


def _entry(canonical, family="example", deployments=None, benchmarks=None, context=131072,
           tools=True):
    return CatalogEntry(
        family_id=family,
        canonical_model_id=canonical,
        display_name=canonical,
        benchmarks=benchmarks if benchmarks is not None else _bench(
            coding=0.9, agentic=0.9, intelligence=0.9),
        capabilities=Capabilities(tools=tools, context_tokens=context),
        deployments=deployments if deployments is not None else [
            _dep(f"openrouter/{canonical}:free")],
    )


def _catalog(*entries):
    return CatalogDocument(
        generated_at=_now(), generator="synthetic-test", entries=list(entries))


def _tiny_aliases():
    return AliasFile(
        schema_version=1,
        verified_at="2026-10-23",
        aliases=[
            AliasEntry(
                canonical_id="example/alpha", family_id="example",
                providers={"openrouter": ["example/alpha:free"], "groq": ["alpha-groq"]},
                aa_slugs=["alpha"], lmarena_names=["Alpha"]),
            AliasEntry(
                canonical_id="example/beta", family_id="example",
                providers={"openrouter": ["example/beta:free"]},
                aa_slugs=["beta"], lmarena_names=["Beta"]),
            AliasEntry(
                canonical_id="example/gamma", family_id="example",
                providers={"groq": ["gamma-groq"]},
                aa_slugs=["gamma"], lmarena_names=["Gamma"]),
        ],
        exceptions=[],
    )


def _record(provider, **overrides):
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


def _fresh_terms():
    terms = [_record(provider) for provider in router_sources.PROVIDERS]
    free_list = router_terms.OpenCodeFreeList(
        source="https://example.test/models",
        verified_at=_today(),
        basis="synthetic",
        model_ids=["alpha-free", "beta-free"],
    )
    return router_terms.TermsFile(terms=terms, opencode_free_models=free_list)


def _secrets():
    return RouterSecrets(
        FREE_ROUTER_OPENROUTER_API_KEY="or-synthetic",
        FREE_ROUTER_NVIDIA_NIM_API_KEY="nv-synthetic",
        FREE_ROUTER_GROQ_API_KEY="groq-synthetic",
        FREE_ROUTER_GEMINI_API_KEY="gemini-synthetic",
        FREE_ROUTER_AA_API_KEY="aa-synthetic",
        FREE_ROUTER_HF_TOKEN="hf-synthetic",
    )


def _or_model(model_id, prompt="0", completion="0", context=131072, tools=True, **extra):
    pricing = {"prompt": prompt, "completion": completion}
    pricing.update(extra)
    payload = {"id": model_id, "pricing": pricing, "context_length": context}
    if tools:
        payload["supported_parameters"] = ["tools", "temperature"]
    return payload


def _key_payload(limit=1000, used=3, remaining=997, usage=0.0):
    return {
        "data": {
            "usage": usage,
            "free_model_daily_requests": {"limit": limit, "used": used, "remaining": remaining},
        }
    }


def _routed_get(routes, *, seen=None, fail=None):
    def _get(url, headers):
        if seen is not None:
            seen.append(url)
        for needle, payload in routes:
            if needle in url:
                if fail is not None and needle in fail:
                    message = Message()
                    raise urllib.error.HTTPError(url, 500, "HTTP 500", message, None)
                return payload() if callable(payload) else payload
        raise AssertionError(f"unexpected URL: {url}")

    return _get


def _provider_routes(or_items, *, key=None, groq_ids=(), nvidia_ids=(),
                     gemini_ids=(), opencode_ids=()):
    key = key if key is not None else _key_payload()
    return [
        ("api/v1/key", key),
        ("openrouter.ai/api/v1/models", {"data": list(or_items)}),
        ("api.groq.com", {"data": [
            {"id": model_id, "context_window": 131072} for model_id in groq_ids]}),
        ("integrate.api.nvidia.com", {"data": [{"id": model_id} for model_id in nvidia_ids]}),
        ("generativelanguage.googleapis.com", {"models": [
            {"name": f"models/{model_id}", "inputTokenLimit": 1048576,
             "supportedGenerationMethods": ["generateContent"]}
            for model_id in gemini_ids]}),
        ("opencode.ai/inference/v1/models", {"data": [
            {"id": model_id} for model_id in opencode_ids]}),
    ]


def _aa_payload(*items, version=4.3, page=1, total_pages=1, has_more=False):
    return {
        "tier": "free",
        "intelligence_index_version": version,
        "pagination": {"page": page, "page_size": 200,
                       "total_pages": total_pages, "has_more": has_more},
        "data": list(items),
    }


def _aa_model(slug, intelligence=None, coding=None, agentic=None):
    return {
        "id": f"id-{slug}",
        "name": slug,
        "slug": slug,
        "model_creator": {"name": "Example"},
        "evaluations": {
            "artificial_analysis_intelligence_index": intelligence,
            "artificial_analysis_coding_index": coding,
            "artificial_analysis_agentic_index": agentic,
        },
    }


def _splits_payload(*pairs):
    return {
        "splits": [{"dataset": "d", "config": config, "split": split}
                   for config, split in pairs],
        "pending": [],
        "failed": [],
    }


def _rows_payload(*rows):
    return {
        "features": [{"feature_idx": 0, "name": "model"},
                     {"feature_idx": 1, "name": "score"}],
        "rows": [{"row_idx": idx, "row": dict(row), "truncated_cells": []}
                 for idx, row in enumerate(rows)],
    }


# Policy scoring defaults and overrides


def test_resolve_scoring_matches_spec_weights_and_floors():
    review = router_ranking.resolve_scoring("free-review")
    assert review.weights == {"coding": 0.60, "agentic": 0.30, "intelligence": 0.10}
    assert review.quality_floor == pytest.approx(0.65)
    research = router_ranking.resolve_scoring("free-research")
    assert research.weights == {"coding": 0.20, "agentic": 0.45, "intelligence": 0.35}
    assert research.quality_floor == pytest.approx(0.60)
    general = router_ranking.resolve_scoring("free-general")
    assert general.weights == {"coding": 0.25, "agentic": 0.25, "intelligence": 0.50}
    assert general.quality_floor == pytest.approx(0.45)


def test_resolve_scoring_rejects_unknown_profile():
    with pytest.raises(ValueError, match="unknown profile"):
        router_ranking.resolve_scoring("free-review-r1-a")


def test_policy_profile_override_merges_per_field():
    policy = RouterPolicy(profiles={
        "free-review": ProfileScoring(weights={"coding": 1.0}),
    })
    resolved = router_ranking.resolve_scoring("free-review", policy)
    assert resolved.weights == {"coding": 1.0}
    assert resolved.quality_floor == pytest.approx(0.65)
    assert resolved.max_quality_drop == pytest.approx(0.15)


def test_policy_profile_floor_and_drop_override():
    policy = RouterPolicy(profiles={
        "free-general": ProfileScoring(quality_floor=0.1, max_quality_drop=0.5),
    })
    resolved = router_ranking.resolve_scoring("free-general", policy)
    assert resolved.quality_floor == pytest.approx(0.1)
    assert resolved.max_quality_drop == pytest.approx(0.5)
    assert resolved.weights["intelligence"] == pytest.approx(0.50)


def test_profile_scoring_rejects_unknown_dimensions():
    with pytest.raises(ValueError, match="unknown scoring dimensions"):
        ProfileScoring(weights={"coding": 0.5, "vibes": 0.5})


def test_profile_scoring_rejects_empty_weights():
    with pytest.raises(ValueError, match="sum above zero"):
        ProfileScoring(weights={"coding": 0.0})


def test_profile_scoring_rejects_out_of_range_bounds():
    with pytest.raises(ValueError, match="0\\.\\.1"):
        ProfileScoring(quality_floor=1.5)
    with pytest.raises(ValueError, match="0\\.\\.1"):
        ProfileScoring(max_quality_drop=-0.1)


def test_policy_without_profiles_still_validates():
    policy = RouterPolicy.model_validate({"strict_zero_cost": True})
    assert policy.profiles == {}
    assert router_ranking.resolve_scoring("free-review", policy).quality_floor > 0


# Scoring math


def test_score_dimensions_full_coverage():
    quality, coverage, dims = router_ranking.score_dimensions(
        _bench(coding=0.8, agentic=0.6, intelligence=0.4),
        {"coding": 0.6, "agentic": 0.3, "intelligence": 0.1})
    assert coverage == pytest.approx(1.0)
    assert quality == pytest.approx(0.6 * 0.8 + 0.3 * 0.6 + 0.1 * 0.4)
    assert dims == {"coding": 0.8, "agentic": 0.6, "intelligence": 0.4}


def test_score_dimensions_missing_metric_degrades():
    quality, coverage, dims = router_ranking.score_dimensions(
        _bench(coding=1.0), {"coding": 0.6, "agentic": 0.3, "intelligence": 0.1})
    assert coverage == pytest.approx(0.6)
    assert quality == pytest.approx(0.6 * 1.0 * 0.6)
    assert dims == {"coding": 1.0}


def test_score_dimensions_arena_backs_agentic():
    quality, _coverage, dims = router_ranking.score_dimensions(
        _bench(coding=0.5, arena=0.7), {"coding": 0.5, "agentic": 0.5, "intelligence": 0.0})
    assert dims["agentic"] == pytest.approx(0.7)
    assert quality == pytest.approx((0.5 * 0.5 + 0.5 * 0.7) * 1.0)


def test_score_dimensions_arena_averages_with_agentic():
    _quality, _coverage, dims = router_ranking.score_dimensions(
        _bench(agentic=0.6, arena=0.8), {"agentic": 1.0})
    assert dims["agentic"] == pytest.approx(0.7)


# Hard gates and ordering


def test_review_requires_tools_but_general_does_not():
    catalog = _catalog(_entry("example/plain", tools=False))
    assert router_ranking.rank_catalog("free-review", catalog).order == []
    assert len(router_ranking.rank_catalog("free-general", catalog).order) == 1


def test_context_floor_excludes_small_context():
    catalog = _catalog(_entry("example/small", context=8192))
    assert router_ranking.rank_catalog("free-review", catalog).order == []
    assert router_ranking.rank_catalog("free-general", catalog).order == []


def test_unverified_and_unhealthy_excluded_t11():
    catalog = _catalog(
        _entry("example/unverified",
               deployments=[_dep("openrouter/example/unverified:free", free_verified=False)]),
        _entry("example/sick",
               deployments=[_dep("openrouter/example/sick:free", healthy=False)]),
    )
    assert router_ranking.rank_catalog("free-general", catalog).order == []


def test_unknown_price_excluded():
    catalog = _catalog(_entry(
        "example/pricy",
        deployments=[_dep("openrouter/example/pricy:free", input_price_usd=None)]))
    assert router_ranking.rank_catalog("free-general", catalog).order == []


def test_confidentiality_gate():
    catalog = _catalog(_entry(
        "example/secret",
        deployments=[_dep("openrouter/example/secret:free", approved_confidentiality=[])]))
    assert router_ranking.rank_catalog("free-general", catalog).order == []


def test_meets_profile_rpm_gate():
    base = _entry("example/limited")
    deployment = _dep("openrouter/example/limited:free",
                      limits=DeploymentLimits(rpm=10, rpd=100, tpm=1000))
    strict = ProfileRequirements(min_rpm=20)
    relaxed = ProfileRequirements(min_rpm=5)
    assert meets_profile(base, deployment, strict) == (False, "RPM below the profile floor")
    assert meets_profile(base, deployment, relaxed) == (True, "")
    unknown = _dep("openrouter/example/limited:free")
    assert meets_profile(base, unknown, relaxed)[0] is False
    assert meets_profile(base, unknown, ProfileRequirements()) == (True, "")


def test_experimental_models_never_rank():
    catalog = _catalog(
        _entry("example/thin", benchmarks=_bench(coding=1.0)),
        _entry("example/none", benchmarks={}),
    )
    ranked = router_ranking.rank_catalog("free-general", catalog)
    assert ranked.order == []
    assert [item.canonical_model_id for item in ranked.experimental] == [
        "example/none", "example/thin"]
    assert "no benchmark observations" in ranked.experimental[0].reason
    assert "1 more needed" in ranked.experimental[1].reason


def test_quality_floor_excludes_weak_models():
    catalog = _catalog(_entry(
        "example/weak", benchmarks=_bench(coding=0.1, agentic=0.1, intelligence=0.1)))
    assert router_ranking.rank_catalog("free-review", catalog).order == []


def test_order_sorts_by_quality_with_deterministic_tiebreak():
    catalog = _catalog(
        _entry("example/mid", benchmarks=_bench(coding=0.5, agentic=0.5, intelligence=0.5)),
        _entry("example/top", benchmarks=_bench(coding=0.9, agentic=0.9, intelligence=0.9)),
        _entry("example/tied", benchmarks=_bench(coding=0.9, agentic=0.9, intelligence=0.9)),
    )
    order = router_ranking.rank_catalog("free-general", catalog).order
    assert [item.canonical_model_id for item in order] == [
        "example/tied", "example/top", "example/mid"]
    assert [item.rank for item in order] == [1, 2, 3]


def test_quota_headroom_breaks_quality_ties():
    rich = _dep("openrouter/example/rich:free",
                limits=DeploymentLimits(rpm=20, rpd=1000, tpm=50000, tpd=100000))
    catalog = _catalog(
        _entry("example/bare"),
        _entry("example/rich", deployments=[rich]),
    )
    order = router_ranking.rank_catalog("free-general", catalog).order
    assert [item.canonical_model_id for item in order] == ["example/rich", "example/bare"]


def test_primary_and_backups_split_routes_t05():
    catalog = _catalog(_entry(
        "example/duo",
        deployments=[
            _dep("groq/duo-groq", provider="groq", gateway="duo-groq"),
            _dep("openrouter/duo:free", limits=DeploymentLimits(rpm=20)),
        ],
    ))
    (item,) = router_ranking.rank_catalog("free-general", catalog).order
    assert item.primary.deployment_id == "openrouter/duo:free"
    assert [route.deployment_id for route in item.backups] == ["groq/duo-groq"]


def test_research_prefers_long_context_with_explicit_fallback():
    long_catalog = _catalog(_entry("example/long", context=200000))
    ranked = router_ranking.rank_catalog("free-research", long_catalog)
    assert len(ranked.order) == 1
    assert ranked.context_floor == 131072
    short_catalog = _catalog(_entry("example/short", context=70000))
    fallen = router_ranking.rank_catalog("free-research", short_catalog)
    assert len(fallen.order) == 1
    assert fallen.context_floor == 65536


# Canonical unification


def _provider_scoped(provider, model_id, context=131072, tools=False):
    deployment_id = f"{provider}/{model_id}"
    return CatalogEntry(
        family_id="unknown-family",
        canonical_model_id=deployment_id,
        display_name=model_id,
        capabilities=Capabilities(context_tokens=context, tools=tools),
        deployments=[_dep(deployment_id, provider=provider, gateway=model_id)],
    )


def test_unify_canonical_merges_provider_entries():
    document = _catalog(
        _provider_scoped("openrouter", "example/alpha:free", tools=True),
        _provider_scoped("groq", "alpha-groq", context=65536),
        _provider_scoped("openrouter", "example/unmapped:free"),
    )
    unified = router_ranking.unify_canonical(document, _tiny_aliases())
    assert len(unified.entries) == 2
    merged = next(entry for entry in unified.entries
                  if entry.canonical_model_id == "example/alpha")
    assert merged.family_id == "example"
    assert sorted(route.deployment_id for route in merged.deployments) == [
        "groq/alpha-groq", "openrouter/example/alpha:free"]
    assert merged.capabilities.tools is True
    assert merged.capabilities.context_tokens == 131072
    leftover = next(entry for entry in unified.entries
                    if entry.canonical_model_id == "openrouter/example/unmapped:free")
    assert leftover.family_id == "unknown-family"


def test_unify_canonical_collision_fails_closed():
    document = _catalog(
        _provider_scoped("openrouter", "example/alpha:free"),
        CatalogEntry(
            family_id="example", canonical_model_id="example/alpha",
            deployments=[_dep("groq/stray", provider="groq", gateway="stray")],
        ),
    )
    with pytest.raises(Exception, match="collides"):
        router_ranking.unify_canonical(document, _tiny_aliases())


# Top-3 retention and provenance (T19)


def _ranked(canonical, quality=0.9, rank=0):
    return RankedEntry(
        rank=rank, canonical_model_id=canonical, family_id="example",
        primary=RankedDeployment(deployment_id=f"openrouter/{canonical}:free",
                                 provider="openrouter"),
        quality=quality, coverage=1.0,
        dimensions={"coding": quality}, quality_sources=["synthetic"],
    )


def _profile_ranking(profile, canonicals, pending=None):
    return ProfileRanking(
        profile=profile,
        order=[_ranked(canonical, rank=position)
               for position, canonical in enumerate(canonicals, start=1)],
        pending_top3=pending,
        provenance=RankingProvenance(
            catalog_snapshot="catalog-old", catalog_sha256="0" * 64),
    )


def test_first_publication_has_no_pending_top3():
    ordered = [_ranked("example/a"), _ranked("example/b")]
    effective, pending = router_ranking.apply_top3_retention(
        ordered, None, now="2026-10-23T00:00:00+00:00", catalog_snapshot="catalog-1")
    assert effective == ordered
    assert pending is None


def test_changed_top3_held_with_provenance_t19():
    previous = _profile_ranking("free-review", ["example/a", "example/b", "example/c"])
    ordered = [_ranked("example/x"), _ranked("example/y"), _ranked("example/a"),
               _ranked("example/b"), _ranked("example/c")]
    effective, pending = router_ranking.apply_top3_retention(
        ordered, previous, now="2026-10-23T01:00:00+00:00", catalog_snapshot="catalog-2")
    assert [item.canonical_model_id for item in effective] == [
        "example/a", "example/b", "example/c", "example/x", "example/y"]
    assert [item.rank for item in effective] == [1, 2, 3, 4, 5]
    assert pending is not None
    assert pending.proposed == ["example/x", "example/y", "example/a"]
    assert pending.previous == ["example/a", "example/b", "example/c"]
    assert pending.first_seen == "2026-10-23T01:00:00+00:00"
    assert pending.consecutive_refreshes == 1
    assert pending.catalog_snapshot == "catalog-2"


def test_repeated_challenger_promotes():
    previous = _profile_ranking(
        "free-review", ["example/a", "example/b", "example/c"],
        pending=router_ranking.PendingTop3(
            proposed=["example/x", "example/y", "example/a"],
            previous=["example/a", "example/b", "example/c"],
            first_seen="2026-10-23T01:00:00+00:00",
            consecutive_refreshes=1, catalog_snapshot="catalog-2"),
    )
    ordered = [_ranked("example/x"), _ranked("example/y"), _ranked("example/a")]
    effective, pending = router_ranking.apply_top3_retention(
        ordered, previous, now="2026-10-23T02:00:00+00:00", catalog_snapshot="catalog-3")
    assert [item.canonical_model_id for item in effective] == [
        "example/x", "example/y", "example/a"]
    assert pending is None


def test_new_challenger_resets_pending():
    previous = _profile_ranking(
        "free-review", ["example/a", "example/b", "example/c"],
        pending=router_ranking.PendingTop3(
            proposed=["example/x", "example/y", "example/z"],
            previous=["example/a", "example/b", "example/c"],
            first_seen="2026-10-23T01:00:00+00:00",
            consecutive_refreshes=1, catalog_snapshot="catalog-2"),
    )
    ordered = [_ranked("example/p"), _ranked("example/q"), _ranked("example/a")]
    _effective, pending = router_ranking.apply_top3_retention(
        ordered, previous, now="2026-10-23T02:00:00+00:00", catalog_snapshot="catalog-3")
    assert pending is not None
    assert pending.proposed == ["example/p", "example/q", "example/a"]
    assert pending.consecutive_refreshes == 1
    assert pending.first_seen == "2026-10-23T02:00:00+00:00"


def test_retained_but_ineligible_top3_drops_with_reason():
    previous = _profile_ranking("free-review", ["example/a", "example/b", "example/c"])
    ordered = [_ranked("example/x"), _ranked("example/a"), _ranked("example/y")]
    effective, pending = router_ranking.apply_top3_retention(
        ordered, previous, now="2026-10-23T01:00:00+00:00", catalog_snapshot="catalog-2")
    assert [item.canonical_model_id for item in effective] == [
        "example/a", "example/x", "example/y"]
    assert pending is not None
    assert "example/b" in pending.reason and "example/c" in pending.reason


def test_unchanged_top3_clears_pending():
    previous = _profile_ranking("free-review", ["example/a", "example/b", "example/c"])
    ordered = [_ranked("example/a", rank=1), _ranked("example/b", rank=2),
               _ranked("example/c", rank=3), _ranked("example/x", rank=4)]
    effective, pending = router_ranking.apply_top3_retention(
        ordered, previous, now="2026-10-23T01:00:00+00:00", catalog_snapshot="catalog-2")
    assert [item.canonical_model_id for item in effective] == [
        "example/a", "example/b", "example/c", "example/x"]
    assert pending is None


def test_build_rankings_records_provenance():
    catalog = _catalog(_entry("example/alpha"))
    document = router_ranking.build_rankings(
        catalog, catalog_snapshot="catalog-9", catalog_sha256="ab" * 32,
        benchmark_sources=["artificial-analysis:index4.3@t"],
        aa_index_version=4.3, lmarena_target="agent/train")
    assert sorted(document.profiles) == ["free-general", "free-research", "free-review"]
    provenance = document.profiles["free-review"].provenance
    assert provenance.catalog_snapshot == "catalog-9"
    assert provenance.catalog_sha256 == "ab" * 32
    assert provenance.aa_index_version == pytest.approx(4.3)
    assert provenance.lmarena_target == "agent/train"
    assert provenance.weights["coding"] == pytest.approx(0.60)
    assert document.catalog_snapshot == "catalog-9"


# Fallback chain (T10) and explain


def test_fallback_follows_ranking_order_t10():
    ranking = _profile_ranking(
        "free-general", ["example/a", "example/b", "example/c"])
    chain = router_ranking.fallback_chain(ranking, down={"example/a"})
    assert [item.canonical_model_id for item in chain] == ["example/b", "example/c"]
    chain = router_ranking.fallback_chain(
        ranking, down={"openrouter/example/a:free"})
    assert [item.canonical_model_id for item in chain] == ["example/b", "example/c"]


def test_fallback_keeps_canonical_with_live_backup():
    ranking = ProfileRanking(
        profile="free-general",
        order=[RankedEntry(
            rank=1, canonical_model_id="example/a", family_id="example",
            primary=RankedDeployment(deployment_id="openrouter/example/a:free",
                                     provider="openrouter"),
            backups=[RankedDeployment(deployment_id="groq/a-groq", provider="groq")],
            quality=0.9, coverage=1.0)],
        provenance=RankingProvenance(catalog_snapshot="c", catalog_sha256="s"),
    )
    chain = router_ranking.fallback_chain(
        ranking, down={"openrouter/example/a:free"})
    assert [item.canonical_model_id for item in chain] == ["example/a"]
    assert router_ranking.fallback_chain(
        ranking, down={"openrouter/example/a:free", "groq/a-groq"}) == []


def test_fallback_enforces_floor_and_drop():
    ranking = _profile_ranking("free-review", ["example/a", "example/b", "example/c"])
    ranking.order[1] = ranking.order[1].model_copy(update={"quality": 0.80})
    ranking.order[2] = ranking.order[2].model_copy(update={"quality": 0.50})
    chain = router_ranking.fallback_chain(ranking)
    assert [item.canonical_model_id for item in chain] == ["example/a", "example/b"]


def test_explain_verdicts():
    catalog = _catalog(
        _entry("example/ranked"),
        _entry("example/thin", benchmarks=_bench(coding=1.0)),
        _entry("example/zgated", tools=False),
    )
    document = router_ranking.build_rankings(catalog, catalog_snapshot="c", catalog_sha256="s")
    ranked = router_ranking.explain_model(
        "example/ranked", "free-general", catalog, document)
    assert ranked.verdict == "ranked #1"
    assert ranked.quality == pytest.approx(0.9)
    thin = router_ranking.explain_model("example/thin", "free-general", catalog, document)
    assert thin.verdict == "experimental"
    gated = router_ranking.explain_model("example/zgated", "free-review", catalog, document)
    assert gated.verdict == "excluded"
    assert any("tools" in reason for reason in gated.reasons)
    missing = router_ranking.explain_model("example/nope", "free-general", catalog, document)
    assert missing.verdict == "unknown"


def test_explain_rejects_unknown_profile():
    catalog = _catalog()
    document = RankingsDocument()
    with pytest.raises(ValueError, match="unknown profile"):
        router_ranking.explain_model("example/a", "free-typo", catalog, document)


# Rankings publication and frozen snapshots (T12 consumer part)


def test_publish_and_load_rankings_round_trip(tmp_path):
    catalog = _catalog(_entry("example/alpha"))
    document = router_ranking.build_rankings(
        catalog, catalog_snapshot="catalog-1", catalog_sha256="ff" * 32)
    snapshot = router_ranking.publish_rankings(document, tmp_path)
    assert (tmp_path / "rankings.json").is_file()
    assert (tmp_path / "history" / f"{snapshot.snapshot_id}.json").is_file()
    reread = router_ranking.load_rankings(tmp_path / "rankings.json")
    assert reread.profiles["free-general"].order[0].canonical_model_id == "example/alpha"


def test_frozen_rankings_survive_republication(tmp_path):
    first = router_ranking.build_rankings(
        _catalog(_entry("example/alpha")),
        catalog_snapshot="catalog-1", catalog_sha256="aa" * 32)
    router_ranking.publish_rankings(first, tmp_path)
    frozen = router_ranking.load_rankings(tmp_path / "rankings.json")
    second = router_ranking.build_rankings(
        _catalog(_entry("example/beta")),
        catalog_snapshot="catalog-2", catalog_sha256="bb" * 32)
    router_ranking.publish_rankings(second, tmp_path)
    assert frozen.profiles["free-general"].order[0].canonical_model_id == "example/alpha"
    assert frozen.catalog_snapshot == "catalog-1"
    current = router_ranking.load_rankings(tmp_path / "rankings.json")
    assert current.catalog_snapshot == "catalog-2"


def test_load_rankings_rejects_invalid(tmp_path):
    target = tmp_path / "rankings.json"
    target.write_text("{not json", encoding="utf-8")
    with pytest.raises(Exception, match="not valid JSON"):
        router_ranking.load_rankings(target)
    target.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(Exception, match="[Ss]chema"):
        router_ranking.load_rankings(target)


# Declared tools and catalog verdicts


def test_openrouter_supported_parameters_set_tools():
    terms = _fresh_terms()
    entries = router_sources.parse_openrouter_models(
        {"data": [
            _or_model("example/alpha:free"),
            _or_model("example/plain:free", tools=False),
            {"id": "example/odd:free", "pricing": {"prompt": "0", "completion": "0"},
             "supported_parameters": "tools"},
        ]},
        _key_payload(),
        terms,
    )
    assert entries[0].capabilities.tools is True
    assert entries[1].capabilities.tools is False
    assert entries[2].capabilities.tools is False


def test_apply_catalog_verdicts_marks_listed_verified_healthy():
    decided = router_refresh.apply_catalog_verdicts(_catalog(
        _entry("example/good",
               deployments=[_dep("openrouter/example/good:free", healthy=False)]),
        _entry("example/bad",
               deployments=[_dep("openrouter/example/bad:free", healthy=True,
                                 free_verified=False)]),
    ))
    assert decided.entries[0].deployments[0].healthy is True
    assert decided.entries[1].deployments[0].healthy is False


def test_attach_benchmarks_replaces_dimensions():
    catalog = _catalog(_entry("example/alpha"))
    qualities, _unresolved = router_benchmarks.build_quality(
        _tiny_aliases(),
        "2026-10-23T00:00:00+00:00",
        aa=router_benchmarks.parse_aa_models(_aa_payload(
            _aa_model("alpha", intelligence=90, coding=80, agentic=70),
            _aa_model("beta", intelligence=60, coding=50, agentic=40),
        )),
    )
    enriched = router_refresh.attach_benchmarks(catalog, qualities)
    dims = enriched.entries[0].benchmarks
    assert set(dims) == {"intelligence", "coding", "agentic"}
    assert dims["coding"].score == pytest.approx(0.75)
    assert "artificial-analysis" in dims["coding"].source


# Provider refresh (injected fetches, isolated ledger)


def _provider_config(tmp_path):
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()
    state.mkdir()
    return config, state


def test_update_providers_publishes_catalog_and_rankings(isolated_home, tmp_path):
    config, _state = _provider_config(tmp_path)
    get = _routed_get(_provider_routes(
        [_or_model("example/alpha:free"), _or_model("example/beta:free")],
        groq_ids=["alpha-groq", "gamma-groq"],
        nvidia_ids=["nv-only"],
        gemini_ids=["gemini-alpha"],
        opencode_ids=["alpha-free"],
    ))
    report = router_refresh.update_providers(
        config, get=get, secrets=_secrets(), terms=_fresh_terms(),
        aliases=_tiny_aliases())
    assert report.kind == "providers"
    assert report.failed == ()
    assert report.stale is False
    catalog = load_catalog_doc(config / "catalog.json")
    by_id = {entry.canonical_model_id: entry for entry in catalog.entries}
    assert by_id["example/alpha"].family_id == "example"
    assert len(by_id["example/alpha"].deployments) == 2
    assert by_id["example/alpha"].capabilities.tools is True
    assert all(route.healthy for route in by_id["example/alpha"].deployments)
    assert by_id["example/beta"].capabilities.tools is True
    assert "openrouter/example/paid:free" not in by_id
    rankings = router_ranking.load_rankings(config / "rankings.json")
    assert rankings.catalog_snapshot == report.catalog_snapshot
    assert rankings.profiles["free-general"].provenance.catalog_sha256


def test_update_providers_preserves_previous_benchmarks(isolated_home, tmp_path):
    config, _state = _provider_config(tmp_path)
    publish_catalog(_catalog(_entry(
        "example/alpha",
        deployments=[
            _dep("openrouter/example/alpha:free"),
            _dep("groq/alpha-groq", provider="groq", gateway="alpha-groq"),
        ],
        benchmarks=_bench(coding=0.8, agentic=0.8, intelligence=0.8),
    )), config)
    get = _routed_get(_provider_routes(
        [_or_model("example/alpha:free"), _or_model("example/beta:free")],
        groq_ids=["alpha-groq", "gamma-groq"],
        nvidia_ids=["nv-only"],
        gemini_ids=["gemini-alpha"],
        opencode_ids=["alpha-free"],
    ))
    report = router_refresh.update_providers(
        config, get=get, secrets=_secrets(), terms=_fresh_terms(),
        aliases=_tiny_aliases())
    assert report.stale is False
    catalog = load_catalog_doc(config / "catalog.json")
    alpha = next(entry for entry in catalog.entries
                 if entry.canonical_model_id == "example/alpha")
    assert set(alpha.benchmarks) >= {"coding", "agentic", "intelligence"}
    beta = next(entry for entry in catalog.entries
                if entry.canonical_model_id == "example/beta")
    assert beta.benchmarks == {}
    rankings = router_ranking.load_rankings(config / "rankings.json")
    general = rankings.profiles["free-general"]
    assert [item.canonical_model_id for item in general.order] == ["example/alpha"]
    assert any(item.canonical_model_id == "example/beta"
               for item in general.experimental)


def test_update_providers_failed_source_keeps_last_good(isolated_home, tmp_path):
    config, _state = _provider_config(tmp_path)
    publish_catalog(_catalog(_entry(
        "example/old",
        deployments=[_dep("groq/old-groq", provider="groq", gateway="old-groq")])),
        config)
    before = (config / "catalog.json").read_bytes()
    get = _routed_get(
        _provider_routes([_or_model("example/alpha:free")], groq_ids=["alpha-groq"]),
        fail={"api.groq.com"})
    report = router_refresh.update_providers(
        config, get=get, secrets=_secrets(), terms=_fresh_terms(),
        aliases=_tiny_aliases(), providers=["openrouter", "groq"])
    assert report.stale is True
    assert any("groq" in failure for failure in report.failed)
    catalog = load_catalog_doc(config / "catalog.json")
    assert catalog.stale is True
    assert "groq" in (catalog.stale_reason or "")
    assert any(route.provider == "groq" for entry in catalog.entries
               for route in entry.deployments)
    assert (config / "catalog.json").read_bytes() != before


@pytest.mark.parametrize("groq_first", [False, True])
def test_update_providers_partial_failure_with_unified_previous(
        isolated_home, tmp_path, groq_first):
    config, _state = _provider_config(tmp_path)
    deployments = [
        _dep("openrouter/example/alpha:free"),
        _dep("groq/alpha-groq", provider="groq", gateway="alpha-groq"),
    ]
    if groq_first:
        deployments.reverse()
    publish_catalog(_catalog(_entry("example/alpha", deployments=deployments)), config)
    get = _routed_get(
        _provider_routes([_or_model("example/alpha:free")], groq_ids=["alpha-groq"]),
        fail={"api.groq.com"})
    report = router_refresh.update_providers(
        config, get=get, secrets=_secrets(), terms=_fresh_terms(),
        aliases=_tiny_aliases(), providers=["openrouter", "groq"])
    assert report.stale is True
    assert any("groq" in failure for failure in report.failed)
    catalog = load_catalog_doc(config / "catalog.json")
    alpha = next(entry for entry in catalog.entries
                 if entry.canonical_model_id == "example/alpha")
    assert sorted(route.provider for route in alpha.deployments) == ["groq", "openrouter"]


def test_update_providers_all_failed_keeps_previous_byte_identical(isolated_home, tmp_path):
    config, state = _provider_config(tmp_path)
    publish_catalog(_catalog(_entry("example/old")), config)
    before = (config / "catalog.json").read_bytes()

    def _boom(url, headers):
        raise router_sources.SourceError("test", "network", "boom")

    with pytest.raises(router_refresh.RefreshError, match="all provider sources failed"):
        router_refresh.update_providers(
            config, get=_boom, secrets=_secrets(), terms=_fresh_terms(),
            aliases=_tiny_aliases(), providers=["openrouter"])
    assert (config / "catalog.json").read_bytes() == before
    assert not (config / "rankings.json").exists()
    code, detail = router_refresh.run_update(
        "providers", config, state,
        update=lambda: router_refresh.update_providers(
            config, get=_boom, secrets=_secrets(), terms=_fresh_terms(),
            aliases=_tiny_aliases(), providers=["openrouter"]))
    assert code == 1
    assert "previous snapshot kept" in detail
    assert "FAIL" in (state / "updater.log").read_text(encoding="utf-8")


# Benchmark refresh


def _benchmark_routes(aa_pages, *, splits=None, rows=None):
    def _aa_page(url, _headers):
        if "page=" in url:
            page = int(url.split("page=")[1].split("&")[0])
        else:
            page = 1
        return aa_pages[page - 1]

    def _get(url, headers):
        if "artificialanalysis.ai" in url:
            return _aa_page(url, headers)
        if "/splits" in url:
            assert splits is not None
            return splits
        if "/rows" in url:
            assert rows is not None
            return rows
        raise AssertionError(f"unexpected URL: {url}")

    return _get


def _seed_catalog(config):
    catalog = router_refresh.apply_catalog_verdicts(_catalog(
        _entry("example/alpha"),
        _entry("example/beta"),
    ))
    publish_catalog(catalog, config)
    return catalog


def test_update_benchmarks_attaches_quality_and_publishes(isolated_home, tmp_path):
    config, _state = _provider_config(tmp_path)
    _seed_catalog(config)
    page1 = _aa_payload(
        _aa_model("alpha", intelligence=90, coding=80, agentic=70),
        _aa_model("beta", intelligence=60, coding=50, agentic=40),
        version=4.3, page=1, total_pages=2, has_more=True)
    page2 = _aa_payload(
        _aa_model("gamma", intelligence=30, coding=20, agentic=10),
        version=4.3, page=2, total_pages=2, has_more=False)
    get = _benchmark_routes(
        [page1, page2],
        splits=_splits_payload(("agent", "train")),
        rows=_rows_payload({"model": "Alpha", "score": 1200.0},
                           {"model": "Beta", "score": 1000.0}))
    report = router_refresh.update_benchmarks(
        config, get=get, secrets=_secrets(), aliases=_tiny_aliases())
    assert report.kind == "benchmarks"
    assert report.failed == ()
    catalog = load_catalog_doc(config / "catalog.json")
    alpha = next(entry for entry in catalog.entries
                 if entry.canonical_model_id == "example/alpha")
    assert set(alpha.benchmarks) >= {"coding", "agentic", "intelligence", "arena"}
    rankings = router_ranking.load_rankings(config / "rankings.json")
    general = rankings.profiles["free-general"]
    assert general.order[0].canonical_model_id == "example/alpha"
    assert general.provenance.aa_index_version == pytest.approx(4.3)
    assert general.provenance.lmarena_target == "agent/train"


def test_update_benchmarks_aa_major_move_fails_closed(isolated_home, tmp_path):
    config, _state = _provider_config(tmp_path)
    _seed_catalog(config)
    catalog = load_catalog_doc(config / "catalog.json")
    router_ranking.publish_rankings(
        router_ranking.build_rankings(
            catalog, catalog_snapshot="catalog-1", catalog_sha256="00",
            aa_index_version=4.3),
        config)
    before = (config / "catalog.json").read_bytes()
    get = _benchmark_routes(
        [_aa_payload(_aa_model("alpha", intelligence=90), version=5.0)],
        splits=_splits_payload(("agent", "train")),
        rows=_rows_payload({"model": "Alpha", "score": 1.0}))
    with pytest.raises(router_refresh.RefreshError, match="AA major moved"):
        router_refresh.update_benchmarks(
            config, get=get, secrets=_secrets(), aliases=_tiny_aliases())
    assert (config / "catalog.json").read_bytes() == before


def test_update_benchmarks_needs_a_catalog_first(isolated_home, tmp_path):
    config, _state = _provider_config(tmp_path)
    with pytest.raises(router_refresh.RefreshError, match="run update --providers first"):
        router_refresh.update_benchmarks(
            config, get=_benchmark_routes([]), secrets=_secrets(),
            aliases=_tiny_aliases())


def test_update_benchmarks_survives_one_dead_source(isolated_home, tmp_path):
    config, _state = _provider_config(tmp_path)
    _seed_catalog(config)

    def _get(url, headers):
        if "artificialanalysis.ai" in url:
            return _aa_payload(
                _aa_model("alpha", intelligence=90, coding=80, agentic=70),
                _aa_model("beta", intelligence=60, coding=50, agentic=40))
        raise router_sources.SourceError("huggingface", "network", "down")

    report = router_refresh.update_benchmarks(
        config, get=_get, secrets=_secrets(), aliases=_tiny_aliases())
    assert any("huggingface" in failure for failure in report.failed)
    catalog = load_catalog_doc(config / "catalog.json")
    alpha = next(entry for entry in catalog.entries
                 if entry.canonical_model_id == "example/alpha")
    assert set(alpha.benchmarks) == {"intelligence", "coding", "agentic"}


# run_update guard, staleness and scheduling


def test_run_update_rejects_unknown_kind(tmp_path):
    code, detail = router_refresh.run_update("bogus", tmp_path, tmp_path)
    assert code == 2
    assert "unknown update kind" in detail


def test_run_update_logs_success_and_failure(isolated_home, tmp_path):
    config, state = _provider_config(tmp_path)
    ok_report = router_refresh.RefreshReport(
        kind="providers", catalog_snapshot="catalog-1", rankings_snapshot="rankings-1",
        stale=False, stale_reason=None, fetched=("openrouter",), failed=())
    code, detail = router_refresh.run_update(
        "providers", config, state, update=lambda: ok_report)
    assert code == 0
    assert "catalog-1" in detail
    log = state / "updater.log"
    assert "update --providers OK" in log.read_text(encoding="utf-8")

    def _fail():
        raise router_refresh.RefreshError("nope")

    code, _detail = router_refresh.run_update("benchmarks", config, state, update=_fail)
    assert code == 1
    assert "update --benchmarks FAIL" in log.read_text(encoding="utf-8")


def test_run_update_skips_when_locked(tmp_path):
    config, state = _provider_config(tmp_path)
    lock_path = state / "update.lock"
    lock_path.touch()
    with open(lock_path, "a+", encoding="utf-8") as guard:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, detail = router_refresh.run_update(
            "providers", config, state,
            update=lambda: (_ for _ in ()).throw(AssertionError("must not run")))
    assert code == 1
    assert "another update holds" in detail
    assert "skipping this run" in (state / "updater.log").read_text(encoding="utf-8")


def test_source_staleness_reports_fresh_and_stale():
    now = _now()
    catalog = _catalog(
        _entry("example/fresh",
               deployments=[_dep("openrouter/example/fresh:free", observed_at=now)]),
        _entry("example/stale",
               deployments=[_dep("groq/stale-groq", provider="groq", gateway="stale-groq",
                                 observed_at=now - timedelta(hours=12))]),
    )
    report = {row.provider: row for row in router_refresh.source_staleness(catalog, now=now)}
    assert report["openrouter"].fresh is True
    assert report["openrouter"].verified == 1
    assert report["groq"].fresh is False
    assert report["nvidia"].entries == 0
    assert report["nvidia"].fresh is False


def test_schedule_block_renders_both_jobs(tmp_path):
    block = router_schedule.render_crontab_block(
        "/venv/bin/python", "/repo/openceo-core/model_routing/cli.py", str(tmp_path))
    assert "update --benchmarks" in block
    assert "update --providers" in block
    assert "flock -n" not in block
    assert "updater.log" in block
    assert block.startswith(router_schedule.CRON_MARK_BEGIN)


def test_schedule_block_has_no_outer_flock_wrapper(tmp_path):
    block = router_schedule.render_crontab_block("py", "cli.py", str(tmp_path))
    jobs = [line for line in block.splitlines()
            if line and not line.startswith("#")]
    assert len(jobs) == 2
    assert all("flock" not in job for job in jobs)


def test_schedule_install_is_idempotent_and_uninstall_cleans():
    block = router_schedule.render_crontab_block("py", "cli.py", "/state")
    installed = router_schedule.install_block("0 1 * * * other-job\n", block)
    assert installed.count(router_schedule.CRON_MARK_BEGIN) == 1
    assert "other-job" in installed
    assert router_schedule.install_block(installed, block) == installed
    assert "other-job" in router_schedule.remove_block(installed)
    assert router_schedule.CRON_MARK_BEGIN not in router_schedule.remove_block(installed)
    assert router_schedule.remove_block("0 1 * * * other-job\n") == "0 1 * * * other-job\n"


def test_schedule_main_dry_run_by_default(capsys):
    assert router_schedule.main(["--python", "py", "--cli", "cli", "--state-dir", "/s"]) == 0
    out = capsys.readouterr().out
    assert "update --providers" in out
    assert "dry-run" in out


# CLI


def _publish_cli_docs(config):
    publish_catalog(
        router_refresh.apply_catalog_verdicts(_catalog(
            _entry("example/alpha"),
            _entry("example/beta", benchmarks=_bench(coding=0.7, agentic=0.7,
                                                     intelligence=0.7)),
        )),
        config)
    catalog = load_catalog_doc(config / "catalog.json")
    record_snapshot = sorted((config / "history").glob("catalog-*.json"))[-1].stem
    router_ranking.publish_rankings(
        router_ranking.build_rankings(
            catalog, catalog_snapshot=record_snapshot, catalog_sha256="00"),
        config)


def test_cli_models_lists_top(capsys, tmp_path):
    config, _state = _provider_config(tmp_path)
    _publish_cli_docs(config)
    assert cli_main(["models", "--profile", "free-general", "--top", "1",
                     "--config-dir", str(config)]) == 0
    out = capsys.readouterr().out
    assert "#1 example/alpha" in out
    assert cli_main(["models", "--profile", "free-general", "--top", "0",
                     "--config-dir", str(config)]) == 2
    assert cli_main(["models", "--profile", "free-general",
                     "--config-dir", str(tmp_path / "missing")]) == 1


def test_cli_models_json(tmp_path, capsys):
    config, _state = _provider_config(tmp_path)
    _publish_cli_docs(config)
    assert cli_main(["models", "--profile", "free-review", "--json",
                     "--config-dir", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "free-review"
    assert payload["order"][0]["canonical_model_id"] == "example/alpha"


def test_cli_explain_and_simulate(capsys, tmp_path):
    config, _state = _provider_config(tmp_path)
    _publish_cli_docs(config)
    assert cli_main(["explain", "--model", "example/alpha",
                     "--config-dir", str(config)]) == 0
    out = capsys.readouterr().out
    assert "free-general: example/alpha -> ranked #1" in out
    assert cli_main(["simulate-fallback", "--profile", "free-general",
                     "--down", "example/alpha",
                     "--config-dir", str(config)]) == 0
    out = capsys.readouterr().out
    assert "serves: #2 example/beta" in out


def test_cli_simulate_blocked_and_json(capsys, tmp_path):
    config, _state = _provider_config(tmp_path)
    _publish_cli_docs(config)
    assert cli_main(["simulate-fallback", "--profile", "free-general",
                     "--down", "example/alpha", "example/beta",
                     "--config-dir", str(config)]) == 0
    assert "BLOCKED_NO_ELIGIBLE_MODEL" in capsys.readouterr().out
    assert cli_main(["simulate-fallback", "--profile", "free-general", "--json",
                     "--config-dir", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["chain"]) == 2


def test_cli_simulate_honors_policy_overrides(capsys, tmp_path):
    config, _state = _provider_config(tmp_path)
    _publish_cli_docs(config)
    (config / "policy.yaml").write_text(
        "policy_version: 1\nprofiles:\n"
        "  free-general: {quality_floor: 0.1, max_quality_drop: 0.05}\n")
    assert cli_main(["simulate-fallback", "--profile", "free-general", "--json",
                     "--config-dir", str(config)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["floor"] == pytest.approx(0.1)
    assert payload["max_drop"] == pytest.approx(0.05)
    assert [item["canonical_model_id"] for item in payload["chain"]] == ["example/alpha"]


def test_cli_sources_shows_staleness(capsys, tmp_path):
    config, _state = _provider_config(tmp_path)
    _publish_cli_docs(config)
    assert cli_main(["sources", "--config-dir", str(config)]) == 0
    assert "openrouter: 2 entries" in capsys.readouterr().out
    assert cli_main(["sources", "--show-staleness", "--config-dir", str(config)]) == 0
    out = capsys.readouterr().out
    assert "fresh" in out


def test_cli_update_benchmarks_without_catalog_fails(tmp_path, capsys):
    config, state = _provider_config(tmp_path)
    assert cli_main(["update", "--benchmarks", "--config-dir", str(config),
                     "--state-dir", str(state)]) == 1
    assert "run update --providers first" in capsys.readouterr().out
