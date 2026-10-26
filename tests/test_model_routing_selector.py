"""native_first selector: group A/B order, gates, budget (TASK-2026-00305).

Offline only: addendum §11.2 (native preference), §11.3 (native
fallback), §11.4 (native to external crossing without a global HTTP
alias), §11.5 (context, tools and privacy), §11.10 (no costs), the
no-eligible block with recorded reasons, explicit best_global
opt-in, and the single global attempt budget. No network, no
models, no real state.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from model_routing import selector as router_selector
from model_routing.availability import (
    AvailabilityStore,
    Exclusions,
)
from model_routing.models import (
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Confidentiality,
    Deployment,
    ProfileRanking,
    RankedDeployment,
    RankedEntry,
    RankingProvenance,
    RankingsDocument,
    RouterPolicy,
)

PUBLIC = [Confidentiality.PUBLIC]


def _now() -> datetime:
    return datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


def _dep(deployment_id, provider="openrouter", **overrides):
    native = provider == "opencode"
    base = {
        "deployment_id": deployment_id,
        "provider": provider,
        "route_type": "opencode_native_free" if native else "external_free",
        "allowed_client": "opencode_only" if native else "configured_opencode_or_gateway",
        "provider_id": provider,
        "model_id": deployment_id.split("/", 1)[1],
        "gateway_model_id": deployment_id.split("/", 1)[1],
        "free_offered": True,
        "free_verified": True,
        "healthy": True,
        "input_price_usd": 0.0,
        "output_price_usd": 0.0,
        "approved_confidentiality": list(PUBLIC),
        "source": "synthetic",
        "observed_at": _now(),
    }
    base.update(overrides)
    return Deployment(**base)


def _entry(canonical, family, deployments, *, tools=True, context=131072):
    return CatalogEntry(
        family_id=family,
        canonical_model_id=canonical,
        display_name=canonical,
        capabilities=Capabilities(tools=tools, context_tokens=context),
        deployments=deployments,
    )


def _catalog() -> CatalogDocument:
    return CatalogDocument(
        generated_at=_now(),
        generator="selector-test",
        entries=[
            _entry("example/alpha", "alpha", [
                _dep("opencode/alpha-free", "opencode"),
                _dep("openrouter/example/alpha:free", "openrouter"),
            ]),
            _entry("example/beta", "beta", [
                _dep("opencode/beta-free", "opencode"),
                _dep("groq/beta-70b", "groq"),
            ]),
            _entry("example/gamma", "gamma", [
                _dep("nvidia/gamma", "nvidia"),
            ]),
        ],
    )


def _ranked(canonical, family, rank, deployment_id, provider, backups=(), quality=0.8):
    return RankedEntry(
        rank=rank,
        canonical_model_id=canonical,
        family_id=family,
        primary=RankedDeployment(deployment_id=deployment_id, provider=provider),
        backups=[RankedDeployment(deployment_id=dep, provider=prov)
                 for dep, prov in backups],
        quality=quality,
        coverage=1.0,
    )


def _profile(name, order):
    return ProfileRanking(
        profile=name,
        order=list(order),
        experimental=[],
        pending_top3=None,
        provenance=RankingProvenance(
            catalog_snapshot="catalog-test",
            catalog_sha256="0" * 64,
        ),
    )


def _rankings() -> RankingsDocument:
    return RankingsDocument(
        generated_at=_now(),
        generator="selector-test",
        catalog_snapshot="catalog-test",
        catalog_sha256="0" * 64,
        profiles={
            "free-research": _profile("free-research", [
                _ranked("example/alpha", "alpha", 1,
                        "opencode/alpha-free", "opencode",
                        backups=[("openrouter/example/alpha:free", "openrouter")],
                        quality=0.9),
                _ranked("example/gamma", "gamma", 2,
                        "nvidia/gamma", "nvidia", quality=0.85),
                _ranked("example/beta", "beta", 3,
                        "groq/beta-70b", "groq",
                        backups=[("opencode/beta-free", "opencode")],
                        quality=0.8),
            ]),
            "free-review": _profile("free-review", [
                _ranked("example/beta", "beta", 1,
                        "opencode/beta-free", "opencode", quality=0.9),
            ]),
            "free-general": _profile("free-general", [
                _ranked("example/gamma", "gamma", 1,
                        "nvidia/gamma", "nvidia", quality=0.7),
            ]),
        },
    )


def test_native_first_is_default_and_best_global_is_explicit():
    assert RouterPolicy().selection_policy == "native_first"
    explicit = RouterPolicy.model_validate({"selection_policy": "best_global"})
    assert explicit.selection_policy == "best_global"
    with pytest.raises(ValueError, match="selection_policy"):
        RouterPolicy.model_validate({"selection_policy": "cheapest"})


def test_unknown_profile_refuses():
    with pytest.raises(router_selector.SelectionError, match="unknown profile"):
        router_selector.select_targets("free-vip", _catalog(), _rankings())


def test_missing_profile_ranking_blocks_with_reason():
    rankings = _rankings()
    del rankings.profiles["free-review"]
    with pytest.raises(router_selector.NoEligibleTarget) as exc_info:
        router_selector.select_targets("free-review", _catalog(), rankings)
    assert exc_info.value.profile == "free-review"
    assert "no free-review ranking published" in str(exc_info.value)


def test_native_preference_11_2():
    selection = router_selector.select_targets(
        "free-research", _catalog(), _rankings())
    assert selection.policy == "native_first"
    assert [target.deployment_id for target in selection.targets] == [
        "opencode/alpha-free",
        "opencode/beta-free",
        "openrouter/example/alpha:free",
        "nvidia/gamma",
        "groq/beta-70b",
    ]
    first = selection.targets[0]
    assert (first.group, first.target_id) == (
        "A", "native:opencode/alpha-free")
    assert first.opencode_model == "opencode/alpha-free"
    assert (first.route_type, first.allowed_client) == (
        "opencode_native_free", "opencode_only")
    assert (first.rank, first.quality) == (1, 0.9)


def test_best_global_keeps_quality_order():
    policy = RouterPolicy(selection_policy="best_global")
    selection = router_selector.select_targets(
        "free-research", _catalog(), _rankings(), policy=policy)
    assert selection.policy == "best_global"
    assert [target.deployment_id for target in selection.targets] == [
        "opencode/alpha-free",
        "openrouter/example/alpha:free",
        "nvidia/gamma",
        "groq/beta-70b",
        "opencode/beta-free",
    ]


def test_native_fallback_11_3_skips_tried_and_cooled(tmp_path):
    store = AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        store.set_cooldown("opencode/alpha-free", "rate_limited", seconds=60)
        selection = router_selector.select_targets(
            "free-research", _catalog(), _rankings(), store=store,
            exclusions=Exclusions(deployments=frozenset({"opencode/beta-free"})))
        assert selection.targets[0].deployment_id == "openrouter/example/alpha:free"
        assert selection.targets[0].group == "B"
        dropped = {item.deployment_id: item.reason for item in selection.dropped}
        assert "cooling down" in dropped["opencode/alpha-free"]
        assert "already tried" in dropped["opencode/beta-free"]
    finally:
        store.close()


def test_crossing_to_external_11_4_has_no_global_alias(tmp_path):
    store = AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        store.block_provider("opencode", "daily_quota: FreeUsageLimitError",
                             seconds=3600)
        selection = router_selector.select_targets(
            "free-research", _catalog(), _rankings(), store=store)
        assert [target.deployment_id for target in selection.targets] == [
            "openrouter/example/alpha:free",
            "nvidia/gamma",
            "groq/beta-70b",
        ]
        assert {target.group for target in selection.targets} == {"B"}
        for target in selection.targets:
            assert target.target_id.startswith("external:")
            assert target.opencode_model == (
                f"{target.provider_id}/{target.model_id}")
            assert "http" not in target.target_id + target.opencode_model
            assert (target.route_type, target.allowed_client) == (
                "external_free", "configured_opencode_or_gateway")
    finally:
        store.close()


def test_context_tools_privacy_gates_11_5():
    catalog = _catalog()
    catalog.entries.append(_entry("example/delta", "delta", [
        _dep("opencode/delta-free", "opencode"),
    ], tools=False))
    catalog.entries.append(_entry("example/eps", "eps", [
        _dep("openrouter/eps:free", "openrouter",
             approved_confidentiality=[Confidentiality.INTERNAL]),
    ], context=32768))
    rankings = _rankings()
    order = rankings.profiles["free-research"].order
    order.append(_ranked("example/delta", "delta", 4,
                         "opencode/delta-free", "opencode", quality=0.9))
    order.append(_ranked("example/eps", "eps", 5,
                         "openrouter/eps:free", "openrouter", quality=0.9))
    selection = router_selector.select_targets("free-research", catalog, rankings)
    served = {target.deployment_id for target in selection.targets}
    assert "opencode/delta-free" not in served
    assert "openrouter/eps:free" not in served
    dropped = {item.deployment_id: item.reason for item in selection.dropped}
    assert "tools" in dropped["opencode/delta-free"]
    assert "context" in dropped["openrouter/eps:free"]
    general = router_selector.select_targets("free-general", catalog, rankings)
    assert general.targets[0].deployment_id == "nvidia/gamma"


def test_privacy_gate_blocks_unapproved_confidentiality():
    catalog = _catalog()
    catalog.entries[0].deployments[0].approved_confidentiality = []
    selection = router_selector.select_targets(
        "free-research", catalog, _rankings())
    served = [target.deployment_id for target in selection.targets]
    assert "opencode/alpha-free" not in served
    assert "openrouter/example/alpha:free" in served
    dropped = {item.deployment_id: item.reason for item in selection.dropped}
    assert "confidentiality" in dropped["opencode/alpha-free"]


def test_no_costs_11_10_nothing_paid_or_unverified_serves():
    catalog = _catalog()
    alpha, beta = catalog.entries[0], catalog.entries[1]
    alpha.deployments[0].input_price_usd = 0.01
    alpha.deployments[1].free_verified = False
    beta.deployments[0].requires_payment_method = True
    beta.deployments[1].input_price_usd = None
    beta.deployments[1].output_price_usd = None
    rankings = _rankings()
    with pytest.raises(router_selector.NoEligibleTarget):
        router_selector.select_targets("free-review", catalog, rankings)
    selection = router_selector.select_targets("free-research", catalog, rankings)
    assert [target.deployment_id for target in selection.targets] == ["nvidia/gamma"]
    dropped = {item.deployment_id: item.reason for item in selection.dropped}
    assert "paid" in dropped["opencode/alpha-free"]
    assert "unverified" in dropped["openrouter/example/alpha:free"]
    assert "paywalled" in dropped["opencode/beta-free"]
    assert "unknown_price" in dropped["groq/beta-70b"]


def test_quality_floor_excludes_and_block_records_every_reason(tmp_path):
    rankings = _rankings()
    rankings.profiles["free-review"].order[0].quality = 0.10
    store = AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        with pytest.raises(router_selector.NoEligibleTarget) as exc_info:
            router_selector.select_targets(
                "free-review", _catalog(), rankings,
                store=store, run_id="selector-test")
        assert "quality floor" in str(exc_info.value)
        events = store.audit_since("selector-test", 0.0)
        assert [event for _ts, event, _detail in events] == ["selection_blocked"]
        assert "free-review [native_first]" in events[0][2]
    finally:
        store.close()


def test_instance_allowlist_covers_group_a_only():
    selection = router_selector.select_targets(
        "free-research", _catalog(), _rankings(),
        instance_models={("opencode", "alpha-free")})
    served = [target.deployment_id for target in selection.targets]
    assert served == [
        "opencode/alpha-free",
        "openrouter/example/alpha:free",
        "nvidia/gamma",
        "groq/beta-70b",
    ]
    dropped = {item.deployment_id: item.reason for item in selection.dropped}
    assert "not listed by this OpenCode instance" in dropped["opencode/beta-free"]


def test_disabled_providers_and_stale_routes_stay_recorded():
    catalog = _catalog()
    catalog.entries[0].deployments[1].provider = "groq"
    rankings = _rankings()
    order = rankings.profiles["free-research"].order
    order.append(_ranked("example/stale", "stale", 4,
                         "groq/ghost-1", "groq", quality=0.9))
    selection = router_selector.select_targets(
        "free-research", catalog, rankings, enabled_providers={"opencode", "groq"})
    served = {target.deployment_id for target in selection.targets}
    assert "openrouter/example/alpha:free" not in served
    assert "groq/ghost-1" not in served
    dropped = {item.deployment_id: item.reason for item in selection.dropped}
    assert "mismatches" in dropped["openrouter/example/alpha:free"]
    assert "missing from the catalog" in dropped["groq/ghost-1"]
    assert "opencode/alpha-free" in served


def test_attempt_budget_counts_every_try_once():
    budget = router_selector.AttemptBudget(max_attempts=2)
    assert budget.remaining == 2
    first = budget.consume("native:opencode/alpha-free")
    assert (first.remaining, budget.remaining) == (1, 2)
    second = first.consume("external:openrouter/example/alpha:free")
    assert second.remaining == 0
    with pytest.raises(router_selector.BudgetExhausted, match="attempt budget spent"):
        second.consume("external:nvidia/gamma")
    with pytest.raises(router_selector.SelectionError, match="at least one try"):
        router_selector.AttemptBudget(max_attempts=0)


def test_walk_targets_spends_one_attempt_per_target():
    selection = router_selector.select_targets(
        "free-research", _catalog(), _rankings())
    walked = list(router_selector.walk_targets(
        selection.targets, router_selector.AttemptBudget(max_attempts=5)))
    assert [target.deployment_id for target, _ in walked] == [
        "opencode/alpha-free",
        "opencode/beta-free",
        "openrouter/example/alpha:free",
        "nvidia/gamma",
        "groq/beta-70b",
    ]
    assert walked[-1][1].remaining == 0
    short = list(itertools.islice(router_selector.walk_targets(
        selection.targets, router_selector.AttemptBudget(max_attempts=2)), 2))
    assert [target.deployment_id for target, _ in short] == [
        "opencode/alpha-free", "opencode/beta-free"]
    assert short[-1][1].remaining == 0
    with pytest.raises(router_selector.BudgetExhausted):
        list(router_selector.walk_targets(
            selection.targets, router_selector.AttemptBudget(max_attempts=2)))
