"""External interfaces: gateway, OpenCode config, snapshot, reserve CLI (TASK-2026-00295).

Offline only: golden files, fail-closed generation, T14 native
fallback, T20 security freeze, and external reservations over the
shared store. No network, no models, no real state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from model_routing import external as router_external
from model_routing.availability import AvailabilityStore
from model_routing.cli import main as cli_main
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
)
from model_routing.publish import publish_catalog
from model_routing.ranking import publish_rankings

GOLDENS = Path(__file__).resolve().parent / "fixtures" / "model_routing_external"


def _now() -> datetime:
    return datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)


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


def _entry(canonical, family, deployments):
    return CatalogEntry(
        family_id=family,
        canonical_model_id=canonical,
        display_name=canonical,
        capabilities=Capabilities(tools=True, context_tokens=131072),
        deployments=deployments,
    )


def _sample_catalog() -> CatalogDocument:
    return CatalogDocument(
        generated_at=_now(),
        generator="external-test",
        entries=[
            _entry("example/alpha", "alpha", [
                _dep("openrouter/example-alpha:free", "openrouter", "example/alpha:free"),
                _dep("groq/gpt-alpha", "groq", "gpt-alpha"),
            ]),
            _entry("example/beta", "beta", [
                _dep("opencode/example-beta-free", "opencode", "example-beta-free"),
                _dep("nvidia/org-beta", "nvidia", "org/beta"),
            ]),
            _entry("example/gamma", "gamma", [
                _dep("gemini/gamma-flash", "gemini", "gamma-flash"),
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


def _sample_rankings() -> RankingsDocument:
    return RankingsDocument(
        generated_at=_now(),
        generator="external-test",
        catalog_snapshot="catalog-test",
        catalog_sha256="0" * 64,
        profiles={
            "free-research": _profile("free-research", [
                _ranked("example/alpha", "alpha", 1,
                        "openrouter/example-alpha:free", "openrouter",
                        backups=[("groq/gpt-alpha", "groq")], quality=0.9),
                _ranked("example/beta", "beta", 2,
                        "nvidia/org-beta", "nvidia", quality=0.7),
            ]),
            "free-review": _profile("free-review", [
                _ranked("example/alpha", "alpha", 1,
                        "groq/gpt-alpha", "groq", quality=0.85),
                _ranked("example/gamma", "gamma", 2,
                        "gemini/gamma-flash", "gemini", quality=0.75),
            ]),
            "free-general": _profile("free-general", [
                _ranked("example/beta", "beta", 1,
                        "opencode/example-beta-free", "opencode", quality=0.6),
            ]),
        },
    )


def _sample_plan():
    return router_external.build_gateway_plan(_sample_catalog(), _sample_rankings())


def test_gateway_plan_order_follows_ranking():
    plan = _sample_plan()
    assert plan.backend == "litellm"
    research = [entry for entry in plan.model_list
                if entry["model_name"] == "free-research"]
    assert [entry["litellm_params"]["order"] for entry in research] == [1, 2, 3]
    assert research[0]["litellm_params"]["model"] == "openrouter/example/alpha:free"
    assert research[1]["litellm_params"]["model"] == "groq/gpt-alpha"
    assert research[2]["litellm_params"]["model"] == "nvidia_nim/org/beta"
    assert research[0]["litellm_params"]["api_key"] == (
        "os.environ/FREE_ROUTER_OPENROUTER_API_KEY")
    assert research[1]["litellm_params"]["api_key"] == "os.environ/FREE_ROUTER_GROQ_API_KEY"
    review = [entry for entry in plan.model_list
              if entry["model_name"] == "free-review"]
    assert [entry["litellm_params"]["order"] for entry in review] == [1, 2]
    general = [entry for entry in plan.model_list
               if entry["model_name"] == "free-general"]
    assert general == []


def test_gateway_review_aliases_one_per_deployment():
    plan = _sample_plan()
    assert plan.review_aliases == ("review-candidate-001", "review-candidate-002")
    assert plan.review_routes == (
        ("review-candidate-001", "groq/gpt-alpha"),
        ("review-candidate-002", "gemini/gamma-flash"),
    )
    aliases = [entry for entry in plan.model_list
               if entry["model_name"].startswith("review-candidate-")]
    assert len(aliases) == 2
    for entry in aliases:
        params = entry["litellm_params"]
        assert params["num_retries"] == 0
        assert "order" not in params
    assert aliases[0]["litellm_params"]["model"] == "groq/gpt-alpha"
    assert aliases[1]["litellm_params"]["model"] == "gemini/gamma-flash"


def test_gateway_render_master_key_and_content_logging_off():
    text = router_external.render_gateway_yaml(_sample_plan())
    assert "master_key: os.environ/FREE_ROUTER_LITELLM_MASTER_KEY" in text
    assert "turn_off_message_logging: true" in text
    assert "eligible-sha256:" in text
    assert "FREE_ROUTER" in text
    parsed = yaml.safe_load(text)
    assert isinstance(parsed["model_list"], list)
    assert parsed["general_settings"]["master_key"] == (
        "os.environ/FREE_ROUTER_LITELLM_MASTER_KEY")


def test_gateway_golden():
    text = router_external.render_gateway_yaml(_sample_plan())
    expected = (GOLDENS / "gateway.expected.yaml").read_text(encoding="utf-8")
    assert text == expected


def test_gateway_rejects_missing_ranked_deployment():
    catalog = _sample_catalog()
    rankings = _sample_rankings()
    rankings.profiles["free-research"].order.append(
        _ranked("example/ghost", "ghost", 9, "openrouter/ghost:free", "openrouter"))
    with pytest.raises(router_external.ExternalError, match="missing from the catalog"):
        router_external.build_gateway_plan(catalog, rankings)


def test_gateway_rejects_unusable_ranked_deployment():
    catalog = _sample_catalog()
    catalog.entries[0].deployments[0].free_verified = False
    with pytest.raises(router_external.ExternalError, match="no longer usable"):
        router_external.build_gateway_plan(catalog, _sample_rankings())


def test_gateway_rejects_unmapped_provider():
    catalog = _sample_catalog()
    rankings = _sample_rankings()
    catalog.entries[0].deployments[0].provider = "acme"
    rankings.profiles["free-research"].order[0].primary.provider = "acme"
    with pytest.raises(router_external.ExternalError, match="unmapped provider"):
        router_external.build_gateway_plan(catalog, rankings)


def test_gateway_rejects_provider_mismatch():
    catalog = _sample_catalog()
    catalog.entries[0].deployments[0].provider = "acme"
    with pytest.raises(router_external.ExternalError, match="catalog says"):
        router_external.build_gateway_plan(catalog, _sample_rankings())


def test_gateway_skips_opencode_native_only():
    plan = _sample_plan()
    assert ("opencode/example-beta-free", "opencode",
            "native-only provider (SPEC 13.3)") in [
                (skip.deployment_id, skip.provider, skip.reason)
                for skip in plan.skipped]
    assert [route.deployment_id for route in plan.native_routes] == [
        "opencode/example-beta-free"]
    route = plan.native_routes[0]
    assert route.command == ("opencode", "run", "-m", "opencode/example-beta-free")
    assert route.family_id == "beta"
    assert all("example-beta-free" not in entry["litellm_params"]["model"]
               for entry in plan.model_list)


def test_gateway_skips_unhealthy():
    catalog = _sample_catalog()
    catalog.entries[2].deployments[0].healthy = False
    plan = router_external.build_gateway_plan(catalog, _sample_rankings())
    assert ("gemini/gamma-flash", "unhealthy at generation time") in [
        (skip.deployment_id, skip.reason) for skip in plan.skipped]
    assert plan.review_aliases == ("review-candidate-001",)
    assert all("gamma-flash" not in entry["litellm_params"]["model"]
               for entry in plan.model_list)


def test_gateway_unknown_backend():
    with pytest.raises(router_external.ExternalError, match="unknown gateway backend"):
        router_external.build_gateway_plan(
            _sample_catalog(), _sample_rankings(), backend="envoy")


def test_litellm_model_mapping():
    assert router_external.litellm_model("openrouter", "a/b:free", "openrouter/a/b:free") == (
        "openrouter/a/b:free")
    assert router_external.litellm_model("nvidia", "o/m", "nvidia/o/m") == "nvidia_nim/o/m"
    assert router_external.litellm_model("groq", "m", "groq/m") == "groq/m"
    assert router_external.litellm_model("gemini", "g", "gemini/g") == "gemini/g"
    with pytest.raises(router_external.ExternalError, match="no gateway mapping"):
        router_external.litellm_model("opencode", "m", "opencode/m")
    with pytest.raises(router_external.ExternalError, match="no provider model id"):
        router_external.litellm_model("groq", None, "groq/m")


def test_eligible_hash_and_reload():
    first = _sample_plan()
    second = _sample_plan()
    assert first.eligible_sha256 == second.eligible_sha256
    assert len(first.eligible_sha256) == 64
    text = router_external.render_gateway_yaml(first)
    assert router_external.extract_eligible_hash(text) == first.eligible_sha256
    assert router_external.gateway_reload_needed(text, second) is False
    assert router_external.gateway_reload_needed(None, second) is True
    assert router_external.gateway_reload_needed("model_list:\n", second) is True
    catalog = _sample_catalog()
    catalog.entries[0].deployments[0].gateway_model_id = "example/alpha-v2:free"
    moved = router_external.build_gateway_plan(catalog, _sample_rankings())
    assert moved.eligible_sha256 != first.eligible_sha256
    assert router_external.gateway_reload_needed(text, moved) is True


def test_opencode_classic_golden():
    config = router_external.build_opencode_config("classic")
    expected = json.loads((GOLDENS / "opencode-classic.expected.json").read_text())
    assert config == expected
    provider = config["provider"]["free-gateway"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:4000/v1"
    assert set(provider["models"]) == {"free-research", "free-general", "free-review"}


def test_opencode_v2_golden():
    config = router_external.build_opencode_config("v2")
    expected = json.loads((GOLDENS / "opencode-v2.expected.json").read_text())
    assert config == expected
    provider = config["providers"]["free-gateway"]
    assert provider["package"] == "@opencode/ai/providers/openai-compatible"
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:4000/v1"
    assert "provider" not in config


def test_detect_opencode_schema():
    assert router_external.detect_opencode_schema({"provider": {}}) == "classic"
    assert router_external.detect_opencode_schema({"providers": {}}) == "v2"
    assert router_external.detect_opencode_schema({"$schema": "x"}) == "classic"
    with pytest.raises(router_external.ExternalError, match="mixes classic and v2"):
        router_external.detect_opencode_schema({"provider": {}, "providers": {}})


def test_build_opencode_config_rejects_unknown():
    with pytest.raises(router_external.ExternalError, match="unknown opencode schema"):
        router_external.build_opencode_config("v3")
    with pytest.raises(router_external.ExternalError, match="unknown profiles"):
        router_external.build_opencode_config("classic", profiles=("free-review", "nope"))


def test_read_opencode_config(tmp_path):
    target = tmp_path / "opencode.json"
    target.write_text('{"provider": {}}', encoding="utf-8")
    assert router_external.read_opencode_config(target) == {"provider": {}}
    with pytest.raises(router_external.ExternalError, match="unreadable"):
        router_external.read_opencode_config(tmp_path / "missing.json")
    target.write_text('{"provider": {', encoding="utf-8")
    with pytest.raises(router_external.ExternalError, match="not plain JSON"):
        router_external.read_opencode_config(target)


def test_strip_and_merge_gateway_provider():
    generated = router_external.build_opencode_config("classic")
    merged = router_external.merge_gateway_provider(
        {"$schema": "https://opencode.ai/config.json",
         "provider": {"other": {"npm": "x"}}},
        generated)
    assert set(merged["provider"]) == {"other", "free-gateway"}
    native = router_external.strip_gateway_provider(merged)
    assert native == {"$schema": "https://opencode.ai/config.json",
                      "provider": {"other": {"npm": "x"}}}
    assert "free-gateway" in merged["provider"]
    bare = router_external.merge_gateway_provider({}, generated)
    assert set(bare["provider"]) == {"free-gateway"}
    assert router_external.strip_gateway_provider(bare) == {
        "$schema": "https://opencode.ai/config.json"}
    with pytest.raises(router_external.ExternalError, match="cannot merge"):
        router_external.merge_gateway_provider({"providers": {}}, generated)
    with pytest.raises(router_external.ExternalError, match="not an object"):
        router_external.merge_gateway_provider({"provider": []}, generated)


def test_freeze_blocks_generation(tmp_path):
    assert router_external.gateway_freeze(tmp_path) is None
    record = router_external.freeze_gateway(tmp_path, "advisory LITELLM-1")
    assert record.reason == "advisory LITELLM-1"
    assert router_external.gateway_freeze(tmp_path) == record
    with pytest.raises(router_external.GatewayFrozenError, match="advisory LITELLM-1"):
        router_external.build_gateway_plan(
            _sample_catalog(), _sample_rankings(), freeze_dir=tmp_path)
    assert router_external.unfreeze_gateway(tmp_path) is True
    assert router_external.unfreeze_gateway(tmp_path) is False
    assert router_external.gateway_freeze(tmp_path) is None
    plan = router_external.build_gateway_plan(
        _sample_catalog(), _sample_rankings(), freeze_dir=tmp_path)
    assert plan.eligible_sha256


def test_corrupt_freeze_marker_reads_frozen(tmp_path):
    (tmp_path / router_external.GATEWAY_FROZEN_FILENAME).write_text(
        "{not-json", encoding="utf-8")
    assert router_external.gateway_freeze(tmp_path) is not None
    with pytest.raises(router_external.GatewayFrozenError):
        router_external.build_gateway_plan(
            _sample_catalog(), _sample_rankings(), freeze_dir=tmp_path)


def test_apply_security_rollback_t20(tmp_path):
    merged = router_external.merge_gateway_provider(
        {"provider": {"other": {"npm": "x"}}},
        router_external.build_opencode_config("classic"))
    result = router_external.apply_security_rollback(
        merged, tmp_path, "upstream advisory")
    assert result.native_config == {
        "$schema": "https://opencode.ai/config.json",
        "provider": {"other": {"npm": "x"}}}
    assert result.freeze.reason == "upstream advisory"
    assert router_external.gateway_freeze(tmp_path) == result.freeze
    with pytest.raises(router_external.GatewayFrozenError):
        router_external.build_gateway_plan(
            _sample_catalog(), _sample_rankings(), freeze_dir=tmp_path)


def test_plan_consumer_routes_gateway_mode():
    plans = router_external.plan_consumer_routes(
        _sample_catalog(), _sample_rankings(), gateway_available=True)
    assert plans["free-research"].mode == "gateway"
    assert plans["free-research"].aliases == ("free-research",)
    assert "gateway alias" in plans["free-research"].notice
    rankings = _sample_rankings()
    rankings.profiles["free-general"] = _profile("free-general", [])
    plans = router_external.plan_consumer_routes(
        _sample_catalog(), rankings, gateway_available=True)
    assert plans["free-general"].mode == "blocked"
    assert "empty" in plans["free-general"].notice


def test_plan_consumer_routes_native_mode_t14():
    plans = router_external.plan_consumer_routes(
        _sample_catalog(), _sample_rankings(), gateway_available=False)
    general = plans["free-general"]
    assert general.mode == "native"
    assert [route.deployment_id for route in general.native] == [
        "opencode/example-beta-free"]
    assert general.native[0].command == (
        "opencode", "run", "-m", "opencode/example-beta-free")
    assert "gateway unavailable" in general.notice
    research = plans["free-research"]
    assert research.mode == "blocked"
    assert "no native" in research.notice
    assert "nothing paid touched" in research.notice


def test_plan_consumer_routes_skips_unusable_native():
    catalog = _sample_catalog()
    catalog.entries[1].deployments[0].free_verified = False
    plans = router_external.plan_consumer_routes(
        catalog, _sample_rankings(), gateway_available=False)
    assert plans["free-general"].mode == "blocked"


def test_plan_consumer_routes_rejects_catalog_provider_mismatch():
    catalog = _sample_catalog()
    catalog.entries[1].deployments[0].provider = "groq"
    plans = router_external.plan_consumer_routes(
        catalog, _sample_rankings(), gateway_available=False)
    assert plans["free-general"].mode == "blocked"


def test_export_and_load_snapshot_round_trip(tmp_path):
    target = tmp_path / "external-snapshot.json"
    snapshot = router_external.export_consumer_snapshot(
        _sample_catalog(), _sample_rankings(), target,
        generator="external-test", gateway_available=False, freeze_dir=tmp_path)
    assert snapshot.schema_version == router_external.EXTERNAL_SNAPSHOT_SCHEMA_VERSION
    assert snapshot.gateway.frozen is False
    assert snapshot.gateway.eligible_sha256 == _sample_plan().eligible_sha256
    assert snapshot.rankings_sha256.startswith("sha256:")
    research = snapshot.profiles["free-research"]
    assert [route.deployment_id for route in research.routes] == [
        "openrouter/example-alpha:free", "groq/gpt-alpha", "nvidia/org-beta"]
    assert [route.gateway_alias for route in research.routes] == ["free-research"] * 3
    review = snapshot.profiles["free-review"]
    assert [route.gateway_alias for route in review.routes] == [
        "review-candidate-001", "review-candidate-002"]
    general = snapshot.profiles["free-general"]
    assert general.routes[0].gateway_alias is None
    assert general.routes[0].native_cli_model == "opencode/example-beta-free"
    assert "gateway unavailable" in general.native_notice
    reread = router_external.load_consumer_snapshot(target)
    assert reread == snapshot
    router_external.freeze_gateway(tmp_path, "advisory")
    frozen = router_external.export_consumer_snapshot(
        _sample_catalog(), _sample_rankings(), target, freeze_dir=tmp_path)
    assert frozen.gateway.frozen is True


def test_export_snapshot_golden(tmp_path):
    target = tmp_path / "external-snapshot.json"
    router_external.export_consumer_snapshot(
        _sample_catalog(), _sample_rankings(), target,
        generator="external-test", gateway_available=False)
    actual = json.loads(target.read_text(encoding="utf-8"))
    expected = json.loads(
        (GOLDENS / "consumer-snapshot.expected.json").read_text(encoding="utf-8"))
    assert actual == {**expected, "generated_at": actual["generated_at"]}
    assert actual["schema_version"] == 1


def test_export_snapshot_refuses_unusable():
    catalog = _sample_catalog()
    catalog.entries[0].deployments[0].free_verified = False
    with pytest.raises(router_external.ExternalError, match="no longer usable"):
        router_external.export_consumer_snapshot(
            catalog, _sample_rankings(), Path("/nonexistent/snap.json"))


def test_load_snapshot_rejects_invalid(tmp_path):
    with pytest.raises(router_external.ExternalError, match="unreadable"):
        router_external.load_consumer_snapshot(tmp_path / "missing.json")
    target = tmp_path / "snap.json"
    target.write_text("{oops", encoding="utf-8")
    with pytest.raises(router_external.ExternalError, match="not valid JSON"):
        router_external.load_consumer_snapshot(target)
    target.write_text('{"schema_version": 1}', encoding="utf-8")
    with pytest.raises(router_external.ExternalError, match="schema validation"):
        router_external.load_consumer_snapshot(target)
    snapshot = router_external.export_consumer_snapshot(
        _sample_catalog(), _sample_rankings(), target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(router_external.ExternalError, match="unsupported snapshot"):
        router_external.load_consumer_snapshot(target)
    assert snapshot.schema_version == 1


def test_load_consumer_inputs(tmp_path):
    config = tmp_path / "cfg"
    publish_catalog(_sample_catalog(), config)
    publish_rankings(_sample_rankings(), config)
    catalog, rankings = router_external.load_consumer_inputs(config)
    assert len(catalog.entries) == 3
    assert set(rankings.profiles) == {"free-research", "free-review", "free-general"}
    with pytest.raises(router_external.ExternalError, match="consumer inputs"):
        router_external.load_consumer_inputs(tmp_path / "empty")


def test_cli_reserve_release_round_trip(tmp_path, capsys):
    state = tmp_path / "st"
    assert cli_main(["reserve", "--run-id", "ext-1", "--round", "1", "--slot", "a",
                     "--canonical", "example/alpha", "--family", "alpha",
                     "--deployment", "groq/gpt-alpha",
                     "--state-dir", str(state), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deployment_id"] == "groq/gpt-alpha"
    assert payload["family_id"] == "alpha"
    assert cli_main(["reservations", "--run-id", "ext-1", "--round", "1",
                     "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert "groq/gpt-alpha" in out and "family alpha" in out
    assert cli_main(["release", "--run-id", "ext-1", "--round", "1", "--slot", "a",
                     "--state-dir", str(state)]) == 0
    assert "released a" in capsys.readouterr().out
    assert cli_main(["release", "--run-id", "ext-1", "--round", "1", "--slot", "a",
                     "--state-dir", str(state)]) == 0
    assert "no reservation held" in capsys.readouterr().out
    assert cli_main(["reservations", "--run-id", "ext-1", "--round", "1",
                     "--state-dir", str(state), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_reserve_family_conflict(tmp_path, capsys):
    state = tmp_path / "st"
    base = ["--run-id", "ext-2", "--round", "1", "--state-dir", str(state)]
    assert cli_main(["reserve", "--slot", "a", "--canonical", "example/alpha",
                     "--family", "alpha", "--deployment", "groq/gpt-alpha",
                     *base]) == 0
    capsys.readouterr()
    assert cli_main(["reserve", "--slot", "b", "--canonical", "example/alpha-2",
                     "--family", "alpha", "--deployment", "openrouter/x:free",
                     *base]) == 1
    assert "already reserved" in capsys.readouterr().out
    assert cli_main(["reserve", "--slot", "b", "--canonical", "example/beta",
                     "--family", "beta", "--deployment", "nvidia/org-beta",
                     *base]) == 0
    capsys.readouterr()
    assert cli_main(["reservations", "--run-id", "ext-2", "--round", "1",
                     "--state-dir", str(state), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert sorted(row["family_id"] for row in rows) == ["alpha", "beta"]


def test_cli_reserve_rejects_bad_ttl(tmp_path, capsys):
    assert cli_main(["reserve", "--run-id", "x", "--round", "1", "--slot", "a",
                     "--canonical", "c", "--family", "f", "--deployment", "d",
                     "--ttl-s", "0", "--state-dir", str(tmp_path)]) == 2
    assert "above zero" in capsys.readouterr().out


def test_cli_cooldowns(tmp_path, capsys):
    state = tmp_path / "st"
    store = AvailabilityStore(state / "availability.sqlite")
    try:
        store.set_cooldown("groq/gpt-alpha", "rate_limited", seconds=600)
    finally:
        store.close()
    assert cli_main(["cooldowns", "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert "groq/gpt-alpha" in out and "rate_limited" in out
    assert cli_main(["cooldowns", "--state-dir", str(state), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["deployment_id"] for row in rows] == ["groq/gpt-alpha"]
    assert rows[0]["failures"] == 1
    assert cli_main(["cooldowns", "--state-dir", str(tmp_path / "fresh")]) == 0
    assert "no active cooldowns" in capsys.readouterr().out


def test_active_cooldowns_skips_expired(tmp_path):
    store = AvailabilityStore(tmp_path / "availability.sqlite")
    try:
        store.set_cooldown("groq/old", "rate_limited", seconds=60, now=1000.0)
        store.set_cooldown("groq/new", "server_error", seconds=600, now=1000.0)
        rows = store.active_cooldowns(now=1200.0)
        assert [row["deployment_id"] for row in rows] == ["groq/new"]
        assert rows[0]["reason"] == "server_error"
    finally:
        store.close()
