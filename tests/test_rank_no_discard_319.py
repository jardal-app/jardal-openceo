"""Rank without discarding: single free multi-provider pool (TASK-2026-00319).

Focused contract: no numeric floor/drop excludes a registered live free
model; per-task rankings order; the order decides, never the provider;
the same model on N providers yields N failover entries; benchmarks,
serve evidence and quota order but never exclude.
"""

from __future__ import annotations

from datetime import datetime, timezone

from model_routing import ranking as router_ranking
from model_routing import review_wiring as review_mod
from model_routing import research_wiring as research_mod
from model_routing.availability import AvailabilityStore
from model_routing.models import (
    BenchmarkScore,
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Confidentiality,
    Deployment,
    DeploymentLimits,
    ProfileRanking,
    RankedDeployment,
    RankedEntry,
    RankingProvenance,
    RankingsDocument,
    RouterPolicy,
)


def _now():
    return datetime.now(timezone.utc)


def _dep(deployment_id, provider="openrouter", **overrides):
    route = "opencode_native_free" if provider == "opencode" else "external_free"
    client = "opencode_only" if provider == "opencode" else "configured_opencode_or_gateway"
    base = {
        "deployment_id": deployment_id,
        "provider": provider,
        "route_type": route,
        "allowed_client": client,
        "provider_id": provider,
        "model_id": deployment_id.split("/", 1)[1],
        "gateway_model_id": "example/alpha:free",
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


def _entry(canonical, provider="openrouter", benchmarks=None, **dep_over):
    return CatalogEntry(
        family_id="fam-" + canonical.replace("/", "-"),
        canonical_model_id=canonical,
        display_name=canonical,
        benchmarks=benchmarks if benchmarks is not None else _bench(
            coding=0.9, agentic=0.9, intelligence=0.9),
        capabilities=Capabilities(tools=True, context_tokens=131072),
        deployments=[_dep(f"{provider}/{canonical}:free", provider, **dep_over)],
    )


def _catalog(*entries):
    return CatalogDocument(generated_at=_now(), generator="test-00319", entries=list(entries))


def test_low_quality_model_still_ranked_not_discarded():
    catalog = _catalog(
        _entry("org/high", benchmarks=_bench(coding=0.95, agentic=0.95, intelligence=0.95)),
        _entry("org/low", benchmarks=_bench(coding=0.10, agentic=0.10, intelligence=0.10)),
    )
    ranked = router_ranking.rank_catalog("free-review", catalog)
    ids = [item.canonical_model_id for item in ranked.order]
    assert "org/high" in ids
    assert "org/low" in ids
    assert ids.index("org/high") < ids.index("org/low")


def test_thin_evidence_model_still_ranked():
    catalog = _catalog(_entry("org/thin", benchmarks={}))
    ranked = router_ranking.rank_catalog("free-review", catalog)
    assert [item.canonical_model_id for item in ranked.order] == ["org/thin"]


def test_fallback_chain_applies_no_floor_or_drop():
    catalog = _catalog(
        _entry("org/high", benchmarks=_bench(coding=0.95, agentic=0.95, intelligence=0.95)),
        _entry("org/low", benchmarks=_bench(coding=0.10, agentic=0.10, intelligence=0.10)),
    )
    ranked = router_ranking.rank_catalog("free-review", catalog)
    assert ranked.order, "ranking must not be empty after removing the gates"
    doc_order = [
        RankedEntry(
            rank=item.rank, canonical_model_id=item.canonical_model_id,
            family_id=item.family_id, primary=item.primary, backups=item.backups,
            quality=item.quality, coverage=item.coverage, dimensions=item.dimensions,
            quality_sources=item.quality_sources)
        for item in ranked.order
    ]
    ranking = ProfileRanking(
        profile="free-review", order=doc_order, experimental=[],
        pending_top3=None,
        provenance=RankingProvenance(catalog_snapshot="t", catalog_sha256="0" * 64),
    )
    chain = router_ranking.fallback_chain(ranking)
    assert [item.canonical_model_id for item in chain] == [
        item.canonical_model_id for item in ranking.order]


def _doc(order):
    return RankingsDocument(
        schema_version=1, generated_at=_now(), generator="test-00319",
        catalog_snapshot="t", catalog_sha256="0" * 64,
        profiles={"free-review": ProfileRanking(
            profile="free-review", order=order, experimental=[], pending_top3=None,
            provenance=RankingProvenance(catalog_snapshot="t", catalog_sha256="0" * 64))},
    )


def _item(rank, canonical, family, deployment, provider, quality=0.8, backups=()):
    return RankedEntry(
        rank=rank, canonical_model_id=canonical, family_id=family,
        primary=RankedDeployment(deployment_id=deployment, provider=provider),
        backups=[RankedDeployment(deployment_id=dep, provider=prov) for dep, prov in backups],
        quality=quality, coverage=1.0, dimensions={"coding": quality},
        quality_sources=["synthetic"])


def test_review_candidates_span_all_providers_in_rank_order():
    ranking = _doc([
        _item(1, "org/ext-first", "ext1", "openrouter/org/ext-first:free", "openrouter",
              quality=0.95),
        _item(2, "org/native-second", "nat2", "opencode/org/native-second", "opencode",
              quality=0.50,
              backups=[("openrouter/org/native-second:free", "openrouter")]),
    ]).profiles["free-review"]
    candidates = review_mod.review_candidates(ranking)
    providers = [item.provider for item in candidates]
    assert "openrouter" in providers
    assert candidates[0].canonical_model_id == "org/ext-first"
    deployments = [item.deployment_id for item in candidates]
    assert "openrouter/org/native-second:free" in deployments


def test_research_candidates_span_all_providers_in_rank_order():
    ranking = _doc([
        _item(1, "org/ext-first", "ext1", "openrouter/org/ext-first:free", "openrouter",
              quality=0.95),
        _item(2, "org/native-second", "nat2", "opencode/org/native-second", "opencode",
              quality=0.50,
              backups=[("groq/native-second", "groq")]),
    ]).profiles["free-review"]
    research_ranking = ranking.model_copy(update={"profile": "free-research"})
    candidates = research_mod.research_candidates(research_ranking)
    assert candidates[0].canonical_model_id == "org/ext-first"
    assert {item.provider for item in candidates} >= {"openrouter"}


def test_selector_keeps_ranking_order_by_default():
    from model_routing.selector import select_targets

    catalog = _catalog(
        _entry("org/ext-first", provider="openrouter",
               benchmarks=_bench(coding=0.95, agentic=0.95, intelligence=0.95)),
        _entry("org/native-second", provider="opencode",
               benchmarks=_bench(coding=0.50, agentic=0.50, intelligence=0.50)),
    )
    doc = RankingsDocument(
        schema_version=1, generated_at=_now(), generator="test-00319",
        catalog_snapshot="t", catalog_sha256="0" * 64,
        profiles={profile: ProfileRanking(
            profile=profile,
            order=router_ranking.rank_catalog(profile, catalog).order,
            experimental=[],
            pending_top3=None,
            provenance=RankingProvenance(catalog_snapshot="t", catalog_sha256="0" * 64))
            for profile in ("free-review", "free-research", "free-general")},
    )
    assert RouterPolicy().selection_policy == "best_global"
    selection = select_targets("free-review", catalog, doc)
    assert selection.targets, "ranking order must yield targets"
    assert selection.targets[0].canonical_model_id == "org/ext-first"


def test_offered_without_verified_or_known_price_still_usable():
    from model_routing.models import is_usable, usability

    dep = _dep("openrouter/org/novel:free", free_verified=False,
               input_price_usd=None, output_price_usd=None)
    assert usability(dep, RouterPolicy()) == "usable"
    assert is_usable(dep, RouterPolicy()) is True


def test_same_model_two_providers_yields_two_failover_targets(tmp_path):
    from model_routing.selector import walk_targets, AttemptBudget, select_targets

    entry = CatalogEntry(
        family_id="fam-dup", canonical_model_id="org/dup",
        display_name="dup",
        benchmarks=_bench(coding=0.9, agentic=0.9, intelligence=0.9),
        capabilities=Capabilities(tools=True, context_tokens=131072),
        deployments=[
            _dep("openrouter/org/dup:free", "openrouter"),
            _dep("groq/dup", "groq"),
        ],
    )
    catalog = _catalog(entry)
    doc = RankingsDocument(
        schema_version=1, generated_at=_now(), generator="test-00319",
        catalog_snapshot="t", catalog_sha256="0" * 64,
        profiles={"free-review": ProfileRanking(
            profile="free-review",
            order=router_ranking.rank_catalog("free-review", catalog).order,
            experimental=[], pending_top3=None,
            provenance=RankingProvenance(catalog_snapshot="t", catalog_sha256="0" * 64))},
    )
    store = AvailabilityStore(tmp_path / "avail.sqlite")
    try:
        selection = select_targets("free-review", catalog, doc, store=store)
    finally:
        store.close()
    dup_targets = [t for t in selection.targets if t.canonical_model_id == "org/dup"]
    assert len(dup_targets) == 2
    walked = [t.target_id for t, _ in walk_targets(list(selection.targets),
                                                   AttemptBudget(max_attempts=10))]
    assert len(walked) == len(selection.targets)
