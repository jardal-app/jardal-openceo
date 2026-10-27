"""Paid auto-ranking for the cascade openrouter slot (TASK-2026-00310).

Cheapest-first ranking with a quality sink, dynamic whitelist for the
spend guard, and the executor override. Offline fixtures only.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_routing import paid as paid_mod
from model_routing.models import (
    BenchmarkScore,
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Deployment,
)
from model_routing.paid import (
    PaidModel,
    PaidRanking,
    load_paid_ranking,
    publish_paid_ranking,
    rank_paid_models,
    rank_premium_models,
    resolve_paid_model,
    resolve_paid_review,
)
from model_routing.paths import PAID_FILENAME
from orchestration.review import (
    CascadeGuard,
    CascadeSpendState,
    ReviewSlot,
    arm_premium_slots,
    cascade_for_priority,
    disarm_premium_slots,
    premium_slots,
)

NOW = datetime.now(timezone.utc)


def _deployment(model_id, prompt, completion):
    return Deployment(
        deployment_id=f"openrouter:{model_id}", provider="openrouter",
        route_type="external_free", allowed_client="opencode_only",
        provider_id="openrouter", model_id=model_id,
        input_price_usd=prompt, output_price_usd=completion)


def _entry(model_id, prompt, completion, scores=(), context=131072,
           tools=True):
    return CatalogEntry(
        family_id=model_id.split("/")[0], canonical_model_id=model_id,
        benchmarks={f"bench-{idx}": BenchmarkScore(score=score, source="t")
                    for idx, score in enumerate(scores)},
        capabilities=Capabilities(tools=tools, streaming=True,
                                  context_tokens=context),
        deployments=[_deployment(model_id, prompt, completion)])


CATALOG = CatalogDocument(
    generated_at=NOW, generator="test", entries=[
        _entry("openai/gpt-oss-20b", 0.00000002, 0.00000008,
               scores=(0.31,) * 5),
        _entry("inclusionai/ling-3.0-flash", 0.00000002, 0.00000009),
        _entry("mistralai/mistral-nemo", 0.00000003, 0.00000006),
        _entry("qwen/qwen3.7-flash", 0.00000003, 0.00000010,
               scores=(0.72,) * 12),
        _entry("rich/quality", 0.00000100, 0.00000400, scores=(0.91,) * 40),
        _entry("weak/sinked", 0.00000001, 0.00000002, scores=(0.20,) * 9),
        _entry("zero/free", 0.0, 0.0),
        _entry("vendor/model:batch", 0.00000001, 0.00000001),
        _entry("~tilde/odd", 0.00000001, 0.00000001),
        _entry("tiny/ctx", 0.00000001, 0.00000001, context=4096),
        _entry("notools/plain", 0.00000001, 0.00000001, tools=False),
        _entry("few/obs", 0.00000100, 0.00000400, scores=(0.95,) * 2),
    ])


def _write_paid(path: Path, generated: datetime, ids=("dyn/cheap",)):
    path.write_text(json.dumps({
        "schema_version": paid_mod.PAID_SCHEMA_VERSION,
        "generated_at": generated.isoformat(), "generator": "test",
        "catalog_snapshot": "test",
        "models": [{"model_id": item, "input_price_usd": 0.00000001,
                    "output_price_usd": 0.00000002, "worst_case_usd": 0.0016,
                    "quality": None, "observations": 0,
                    "context_tokens": 131072} for item in ids]}))


def _guard(paid_loader, whitelist=("static/old",), premium_loader=None,
           premium_whitelist=(), premium_cap="1.00", premium_estimate="0.50"):
    return CascadeGuard(
        {"enabled": True, "paid_cap_usd": "0.50",
         "paid_estimate_usd": "0.05", "dsh_estimate_usd": "0.00",
         "paid_dynamic": True, "paid_whitelist": list(whitelist),
         "premium_cap_usd": premium_cap,
         "premium_estimate_usd": premium_estimate,
         "premium_whitelist": list(premium_whitelist)},
        threshold_usd=Decimal("10.00"), paid_loader=paid_loader,
        premium_loader=premium_loader)


def _paid_slot(model: str, premium: bool = False) -> ReviewSlot:
    return ReviewSlot(label="openrouter-paid", family="openrouter",
                      model=f"openrouter/{model}", cost_tier="metered",
                      cascade_only=True, premium=premium)


def test_rule_orders_cheapest_first_and_skips_nonstandard():
    ids = [m.model_id for m in rank_paid_models(CATALOG, top_n=9)]
    assert ids[:2] == ["inclusionai/ling-3.0-flash", "mistralai/mistral-nemo"]
    for excluded in ("zero/free", "vendor/model:batch", "~tilde/odd",
                     "tiny/ctx", "notools/plain"):
        assert excluded not in ids


def test_quality_sink_sorts_after_qualifying_when_cheaper():
    ids = [m.model_id for m in rank_paid_models(CATALOG, top_n=9)]
    assert ids.index("weak/sinked") > ids.index("qwen/qwen3.7-flash")
    assert ids.index("openai/gpt-oss-20b") > ids.index("rich/quality")


def test_pricier_quality_sorts_by_worst_case():
    ids = [m.model_id for m in rank_paid_models(CATALOG, top_n=9)]
    assert ids.index("qwen/qwen3.7-flash") < ids.index("rich/quality")


def test_dynamic_whitelist_wins_over_static():
    guard = _guard(lambda: ["dyn/cheap"])
    assert guard.whitelist == ["dyn/cheap"]
    allowed = guard.check(_paid_slot("dyn/cheap"), CascadeSpendState())
    assert allowed.allowed
    denied = guard.check(_paid_slot("static/old"), CascadeSpendState())
    assert not denied.allowed and "whitelist" in denied.reason


def test_loader_failure_keeps_static():
    def _boom():
        raise RuntimeError("unreadable")
    guard = _guard(_boom)
    assert guard.whitelist == ["static/old"]


def test_stale_ranking_file_loads_none(tmp_path):
    path = tmp_path / PAID_FILENAME
    _write_paid(path, NOW - paid_mod.PAID_TTL - timedelta(days=1))
    assert load_paid_ranking(path) is None
    assert load_paid_ranking(tmp_path / "missing.json") is None


def test_executor_override_rewrites_model_and_map(tmp_path):
    path = tmp_path / PAID_FILENAME
    _write_paid(path, NOW)
    target, argv, model = resolve_paid_review(
        ["opencode", "run", "-m", "openrouter/static/old"], path=path)
    assert model == "openrouter/dyn/cheap"
    assert argv[argv.index("-m") + 1] == "openrouter/dyn/cheap"
    assert (list(target["providers"]["openrouter"]["models"])
            == ["dyn/cheap"])


def test_stale_ranking_keeps_static_argv(tmp_path):
    path = tmp_path / PAID_FILENAME
    _write_paid(path, NOW - paid_mod.PAID_TTL - timedelta(days=1))
    command = ["opencode", "run", "-m", "openrouter/static/old"]
    assert resolve_paid_review(command, path=path) == (None, command, None)


def test_resolve_model_picks_ranking_top(tmp_path):
    path = tmp_path / PAID_FILENAME
    _write_paid(path, NOW, ids=("dyn/cheap", "dyn/second"))
    top = resolve_paid_model(path)
    assert isinstance(top, PaidModel) and top.model_id == "dyn/cheap"
    assert resolve_paid_model(tmp_path / "missing.json") is None
    ranking = load_paid_ranking(path)
    assert isinstance(ranking, PaidRanking) and len(ranking.models) == 2


def test_premium_orders_quality_first_and_requires_proof():
    ids = [m.model_id for m in rank_premium_models(CATALOG, top_n=9)]
    assert ids == ["rich/quality", "qwen/qwen3.7-flash"]


def test_guard_premium_uses_own_cap_and_accrues():
    guard = _guard(lambda: ["dyn/cheap"],
                   premium_loader=lambda: ["prem/a"])
    state = CascadeSpendState()
    assert guard.check(_paid_slot("prem/a", premium=True), state).allowed
    assert guard.check(_paid_slot("prem/a", premium=True), state).allowed
    third = guard.check(_paid_slot("prem/a", premium=True), state)
    assert not third.allowed and "over cap" in third.reason
    assert state.spent_usd == Decimal("1.00")


def test_guard_premium_rejects_cheap_and_over_threshold():
    guard = _guard(lambda: ["dyn/cheap"],
                   premium_loader=lambda: ["prem/a"])
    denied = guard.check(_paid_slot("dyn/cheap", premium=True),
                         CascadeSpendState())
    assert not denied.allowed and "premium paid whitelist" in denied.reason
    tight = CascadeGuard(
        {"enabled": True, "paid_cap_usd": "0.50",
         "paid_estimate_usd": "0.05", "dsh_estimate_usd": "0.00",
         "premium_cap_usd": "1.00", "premium_estimate_usd": "0.50"},
        threshold_usd=Decimal("0.50"),
        paid_loader=lambda: ["dyn/cheap"],
        premium_loader=lambda: ["prem/a"])
    refused = tight.check(_paid_slot("prem/a", premium=True),
                          CascadeSpendState())
    assert not refused.allowed and "exceeds mandate" in refused.reason


def test_premium_degrades_to_cheap_without_data(tmp_path):
    guard = _guard(lambda: ["dyn/cheap"], premium_loader=lambda: [])
    assert guard.whitelist_for(True) == ["dyn/cheap"]
    decision = guard.check(_paid_slot("dyn/cheap", premium=True),
                           CascadeSpendState())
    assert decision.allowed and decision.cap_usd == "0.50"
    path = tmp_path / PAID_FILENAME
    _write_paid(path, NOW)
    _target, argv, model = resolve_paid_review(
        ["opencode", "run", "-m", "openrouter/static/old"], path=path,
        premium=True)
    assert model == "openrouter/dyn/cheap"
    assert argv[argv.index("-m") + 1] == "openrouter/dyn/cheap"


def test_static_premium_fallback_without_loaders():
    guard = _guard(lambda: None, premium_loader=lambda: None,
                   premium_whitelist=["stat/prem"])
    assert guard.whitelist_for(True) == ["stat/prem"]
    decision = guard.check(_paid_slot("stat/prem", premium=True),
                           CascadeSpendState())
    assert decision.allowed and decision.cap_usd == "1.00"


def test_premium_slots_flag_only_cascade_and_keep_tier():
    free = ReviewSlot(label="free-a", family="a", model="opencode/x",
                      cost_tier="free")
    paid = _paid_slot("dyn/cheap")
    flagged = premium_slots([free, paid], True)
    assert flagged[0].premium is False
    assert flagged[1].premium is True
    assert flagged[1].cost_tier == "metered"
    assert flagged[1].cascade_only is True
    plain = premium_slots([free, paid], False)
    assert plain[1].premium is False


def test_cascade_for_priority_arms_only_critical():
    assert cascade_for_priority({"enabled": True}, "critical")["premium"]
    assert cascade_for_priority({"enabled": True}, "CRITICAL")["premium"]
    assert not cascade_for_priority({"enabled": True}, "high")["premium"]
    assert not cascade_for_priority({"enabled": True}, None)["premium"]
    assert not cascade_for_priority(None, "critical").get("enabled", False)


def test_arm_disarm_roundtrip_and_scripted_executor():
    class _Exec:
        def __init__(self):
            self.pool = {"openrouter-paid": {"label": "openrouter-paid"}}

        def arm_premium(self, label):
            self.pool[label]["premium"] = True

        def disarm_premium(self, label):
            self.pool[label].pop("premium", None)

    pool = [_paid_slot("dyn/cheap")]
    executor = _Exec()
    cascade = arm_premium_slots(executor, pool, {"enabled": True}, True)
    assert cascade["premium"] is True
    assert executor.pool["openrouter-paid"]["premium"] is True
    disarm_premium_slots(executor, pool)
    assert "premium" not in executor.pool["openrouter-paid"]
    scripted = object()
    cascade = arm_premium_slots(scripted, pool, {"enabled": True}, True)
    assert cascade["premium"] is True
    disarm_premium_slots(scripted, pool)
    cascade = arm_premium_slots(executor, pool, {"enabled": True}, False)
    assert cascade["premium"] is False
    assert "premium" not in executor.pool["openrouter-paid"]


def test_executor_premium_override_serves_proven_top(tmp_path):
    path = tmp_path / PAID_FILENAME
    payload = {
        "schema_version": paid_mod.PAID_SCHEMA_VERSION,
        "generated_at": NOW.isoformat(), "generator": "test",
        "catalog_snapshot": "test",
        "models": [{"model_id": "dyn/cheap", "input_price_usd": 1e-8,
                    "output_price_usd": 2e-8, "worst_case_usd": 0.0016,
                    "quality": None, "observations": 0,
                    "context_tokens": 131072}],
        "premium": [{"model_id": "prem/a", "input_price_usd": 1e-6,
                     "output_price_usd": 4e-6, "worst_case_usd": 0.17,
                     "quality": 0.9, "observations": 10,
                     "context_tokens": 131072}],
    }
    path.write_text(json.dumps(payload))
    command = ["opencode", "run", "-m", "openrouter/static/old"]
    _target, _argv, cheap = resolve_paid_review(command, path=path)
    assert cheap == "openrouter/dyn/cheap"
    target, argv, model = resolve_paid_review(
        command, path=path, premium=True)
    assert model == "openrouter/prem/a"
    assert argv[argv.index("-m") + 1] == "openrouter/prem/a"
    assert (list(target["providers"]["openrouter"]["models"])
            == ["prem/a"])


def test_publish_load_premium_roundtrip_and_legacy_file(tmp_path):
    ranking = publish_paid_ranking(
        tmp_path, CATALOG, generator="test", catalog_snapshot="test")
    assert [m.model_id for m in ranking.premium] == [
        "rich/quality", "qwen/qwen3.7-flash"]
    loaded = load_paid_ranking(tmp_path / PAID_FILENAME)
    assert loaded is not None
    assert [m.model_id for m in loaded.premium] == [
        "rich/quality", "qwen/qwen3.7-flash"]
    legacy = tmp_path / "legacy.json"
    payload = json.loads((tmp_path / PAID_FILENAME).read_text())
    del payload["premium"]
    legacy.write_text(json.dumps(payload))
    assert load_paid_ranking(legacy).premium == []
