"""native_first reviewers with a group B reserve (TASK-2026-00306).

Offline proof with simulated executors: the fed slots take the
``free-review`` native_first list (group A then group B, explicit
``--model`` each), one failed reviewer restarts alone from the same
snapshot and prompt, family reservations hold across simultaneous
and concurrent failures, and the group B credential never reaches
the review tools. No network, no CLI, no live model calls; the one
real group B review lives in test_model_routing_review_group_b_live.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import review_wiring as wiring  # noqa: E402
from model_routing import reviewer_isolation as iso  # noqa: E402
from model_routing.availability import (  # noqa: E402
    AvailabilityStore,
    Exclusions,
    ReservationConflict,
    record_provider_error,
    reserve_model,
)
from model_routing.errors import classify_text  # noqa: E402
from model_routing.external import credential_env_for  # noqa: E402
from test_model_routing_review_wiring import (  # noqa: E402
    _rankings_doc,
    _static_pool,
)

try:
    from orchestration.agent_sandbox import sandbox_available

    _BWRAP = sandbox_available()
except Exception:
    _BWRAP = False

needs_bwrap = pytest.mark.skipif(not _BWRAP, reason="bwrap is missing")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"

FAKE_SECRETS = {
    "FREE_ROUTER_OPENROUTER_API_KEY": "TEST-ONLY-FAKE-OPENROUTER",
    "FREE_ROUTER_GROQ_API_KEY": "TEST-ONLY-FAKE-GROQ",
}


def _deployment(deployment_id, provider, route, provider_id, model_id):
    allowed = ("opencode_only" if route == "opencode_native_free"
               else "configured_opencode_or_gateway")
    return {
        "deployment_id": deployment_id, "provider": provider,
        "route_type": route, "allowed_client": allowed,
        "provider_id": provider_id, "model_id": model_id,
        "gateway_model_id": model_id,
        "free_offered": True, "free_verified": True, "healthy": True,
        "input_price_usd": 0.0, "output_price_usd": 0.0,
        "approved_confidentiality": ["public"],
        "source": "review-native-test",
    }


def _catalog_entry(family, canonical, display, deployment):
    return {
        "family_id": family, "canonical_model_id": canonical,
        "display_name": display,
        "capabilities": {"tools": True, "streaming": False,
                         "context_tokens": 131072},
        "deployments": [deployment],
    }


def _catalog_doc(entries):
    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "review-native-test",
        "entries": entries,
    }


def _rank_entry(rank, canonical, family, deployment_id, provider, quality):
    return {
        "rank": rank, "canonical_model_id": canonical,
        "family_id": family,
        "primary": {"deployment_id": deployment_id, "provider": provider},
        "backups": [],
        "quality": quality, "coverage": 1.0,
        "dimensions": {"coding": quality},
        "quality_sources": ["synthetic-fixture"],
    }


NATIVE_ALPHA = ("org/alpha", "alpha", "opencode/alpha-free", "opencode")
NATIVE_BETA = ("org/beta", "beta", "opencode/beta-free", "opencode")
EXTERNAL_GAMMA = ("acme/gamma", "gamma", "openrouter/acme/gamma:free",
                  "openrouter")
EXTERNAL_DELTA = ("acme/delta", "delta", "openrouter/acme/delta:free",
                  "openrouter")


def _standard_round():
    order = [
        _rank_entry(1, *NATIVE_ALPHA, 0.90),
        _rank_entry(2, *NATIVE_BETA, 0.88),
        _rank_entry(3, *EXTERNAL_GAMMA, 0.85),
        _rank_entry(4, *EXTERNAL_DELTA, 0.83),
    ]
    entries = [
        _catalog_entry("alpha", "org/alpha", "Alpha Free", _deployment(
            "opencode/alpha-free", "opencode", "opencode_native_free",
            "opencode", "alpha-free")),
        _catalog_entry("beta", "org/beta", "Beta Free", _deployment(
            "opencode/beta-free", "opencode", "opencode_native_free",
            "opencode", "beta-free")),
        _catalog_entry("gamma", "acme/gamma", "Gamma Free", _deployment(
            "openrouter/acme/gamma:free", "openrouter", "external_free",
            "openrouter", "acme/gamma:free")),
        _catalog_entry("delta", "acme/delta", "Delta Free", _deployment(
            "openrouter/acme/delta:free", "openrouter", "external_free",
            "openrouter", "acme/delta:free")),
    ]
    return order, entries


def _write_round(base, order, entries):
    cfg = base / "cfg" / "free-router"
    (cfg / "rankings.json").write_text(
        json.dumps(_rankings_doc(order)), encoding="utf-8")
    if entries is not None:
        (cfg / "catalog.json").write_text(
            json.dumps(_catalog_doc(entries)), encoding="utf-8")


@contextmanager
def _round_env(order=None, entries="standard"):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "cfg" / "free-router").mkdir(parents=True)
        (base / "st").mkdir(parents=True)
        if order is None:
            order, default_entries = _standard_round()
            if entries == "standard":
                entries = default_entries
        _write_round(base, order, entries)
        with patch.dict(os.environ,
                        {"XDG_CONFIG_HOME": str(base / "cfg"),
                         "XDG_STATE_HOME": str(base / "st")}):
            yield base


def _assemble(base, run_id="run-n", round="1", r1=None, **kwargs):
    params = {
        "review_cfg": {}, "run_id": run_id, "round": round,
        "builder_family": "muse",
        "receipts_dir": base / "receipts",
        "router_secrets": dict(FAKE_SECRETS),
        "auto_instance": False,
    }
    params.update(kwargs)
    return wiring.assemble_review_pool(_static_pool(), **params)


def _quota_block_opencode(base):
    payload = json.loads(
        (FIXTURES / "opencode-free-quota-429.json").read_text(
            encoding="utf-8"))
    text = payload["event"]["error"]["response"]["body"]
    classified = classify_text(text)
    assert classified.error_class == "daily_quota"
    store = AvailabilityStore()
    try:
        record_provider_error(
            store, provider="opencode",
            deployment_id="opencode/alpha-free",
            model="opencode/alpha-free", classified=classified,
            run_id="quota-fixture", excerpt=text[:200])
    finally:
        store.close()


class NativeSelectionTests(unittest.TestCase):
    def test_pair_prefers_group_a_with_explicit_models(self):
        with _round_env() as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                assert assembly.prefer_families == ("alpha", "beta")
                for label in ("free-review-1", "free-review-2"):
                    state = routing.states[label]
                    assert isinstance(state.effective,
                                      wiring.ReviewTarget)
                    assert not state.effective.is_external
                first = next(item for item in assembly.raw_pool
                             if item.get("label") == "free-review-1")
                assert first["command"] == [
                    "opencode", "run", "-m", "opencode/alpha-free"]
                assert wiring.REVIEW_TARGET_SPEC not in first
                groups = [
                    "B" if item.is_external else "A"
                    for item in routing.candidates
                    if isinstance(item, wiring.ReviewTarget)]
                assert groups == ["A", "A", "B", "B"]
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_group_a_exhausted_serves_group_b(self):
        with _round_env() as base:
            _quota_block_opencode(base)
            probe = AvailabilityStore()
            try:
                assert probe.provider_blocked("opencode") is not None
            finally:
                probe.close()
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                assert assembly.prefer_families == ("gamma", "delta")
                first = next(item for item in assembly.raw_pool
                             if item.get("label") == "free-review-1")
                assert first["command"] == [
                    "opencode", "run", "-m",
                    "openrouter/acme/gamma:free"]
                handoff = first[wiring.REVIEW_TARGET_SPEC]
                assert handoff["provider_id"] == "openrouter"
                assert handoff["model_id"] == "acme/gamma:free"
                assert handoff["route_type"] == "external_free"
                assert handoff["env_var"] == (
                    "FREE_ROUTER_OPENROUTER_API_KEY")
                api_key = handoff["providers"]["openrouter"][
                    "settings"]["apiKey"]
                assert api_key == "{env:FREE_ROUTER_OPENROUTER_API_KEY}"
                assert "TEST-ONLY-FAKE" not in json.dumps(first)
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_uncredentialed_group_b_drops_with_audit(self):
        with _round_env() as base:
            assembly = _assemble(base, router_secrets={})
            try:
                routing = assembly.routing
                assert routing is not None
                assert assembly.prefer_families == ("alpha", "beta")
                groups = [
                    "B" if item.is_external else "A"
                    for item in routing.candidates
                    if isinstance(item, wiring.ReviewTarget)]
                assert groups == ["A", "A"]
                events = routing.store.audit_since("run-n", 0.0)
                kinds = [event for _ts, event, _detail in events]
                assert "review_group_b_uncredentialed" in kinds
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_unmapped_external_provider_drops(self):
        order = [
            _rank_entry(1, *NATIVE_ALPHA, 0.90),
            _rank_entry(2, *NATIVE_BETA, 0.88),
            _rank_entry(3, "zeta/z-model", "zeta",
                        "zhipu/z-model-free", "zhipu", 0.85),
        ]
        _, standard_entries = _standard_round()
        entries = standard_entries[:2] + [
            _catalog_entry("zeta", "zeta/z-model", "Zeta Free",
                           _deployment("zhipu/z-model-free", "zhipu",
                                       "external_free", "zhipu",
                                       "z-model-free")),
        ]
        with _round_env(order, entries) as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                assert assembly.prefer_families == ("alpha", "beta")
                events = routing.store.audit_since("run-n", 0.0)
                kinds = [event for _ts, event, _detail in events]
                assert "review_group_b_unmapped" in kinds
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_same_family_across_providers_is_not_two_reviewers(self):
        order = [
            _rank_entry(1, *NATIVE_ALPHA, 0.90),
            _rank_entry(2, "org/alpha", "alpha",
                        "groq/alpha-free", "groq", 0.88),
            _rank_entry(3, *NATIVE_BETA, 0.85),
            _rank_entry(4, *EXTERNAL_GAMMA, 0.83),
        ]
        _, standard_entries = _standard_round()
        alpha_entry = dict(standard_entries[0])
        alpha_entry["deployments"] = [
            *alpha_entry["deployments"],
            _deployment("groq/alpha-free", "groq", "external_free",
                        "groq", "alpha-free"),
        ]
        entries = [alpha_entry, standard_entries[1], standard_entries[2]]
        with _round_env(order, entries) as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                assert assembly.prefer_families == ("alpha", "beta")
                nxt = wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="disabled",
                    text="model not found: opencode/alpha-free")
                assert nxt is not None
                assert nxt.family_id == "gamma"
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_gemini_family_counts_for_diversity(self):
        order = [
            _rank_entry(1, *NATIVE_ALPHA, 0.90),
            _rank_entry(2, "google/gem", "gemini",
                        "opencode/gem-free", "opencode", 0.88),
            _rank_entry(3, *NATIVE_BETA, 0.85),
        ]
        _, standard_entries = _standard_round()
        entries = standard_entries[:2] + [
            _catalog_entry("gemini", "google/gem", "Gem Free", _deployment(
                "opencode/gem-free", "opencode", "opencode_native_free",
                "opencode", "gem-free")),
        ]
        with _round_env(order, entries) as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                assert assembly.prefer_families == ("alpha", "beta")
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_missing_catalog_keeps_legacy_order(self):
        order, _entries = _standard_round()
        legacy = [
            {"rank": item["rank"],
             "canonical_model_id": item["canonical_model_id"],
             "family_id": item["family_id"],
             "primary": dict(item["primary"]), "backups": [],
             "quality": item["quality"], "coverage": 1.0,
             "dimensions": dict(item["dimensions"]),
             "quality_sources": list(item["quality_sources"])}
            for item in order[:2]
        ]
        with _round_env(legacy, None) as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                assert assembly.prefer_families == ("alpha", "beta")
                assert not any(
                    isinstance(item, wiring.ReviewTarget)
                    for item in routing.candidates)
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_invalid_catalog_falls_back_to_static(self):
        with _round_env() as base:
            (base / "cfg" / "free-router" / "catalog.json").write_text(
                "{not-json", encoding="utf-8")
            assembly = _assemble(base)
            try:
                assert assembly.routing is None
                labels = [item.get("label") for item in assembly.raw_pool]
                assert "mimo" in labels
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()


class SimultaneousFailureTests(unittest.TestCase):
    def test_simultaneous_failure_keeps_distinct_families(self):
        with _round_env() as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                first = wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="disabled",
                    text="model not found: opencode/alpha-free")
                second = wiring.plan_fallback(
                    routing, "free-review-2", outcome_kind="disabled",
                    text="model not found: opencode/beta-free")
                assert first is not None
                assert second is not None
                assert first.family_id != second.family_id
                assert {first.family_id, second.family_id} == {
                    "gamma", "delta"}
                assert routing.states["free-review-1"].requested\
                    .deployment_id == "opencode/alpha-free"
                assert routing.states["free-review-2"].requested\
                    .deployment_id == "opencode/beta-free"
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_concurrent_reserve_keeps_one_family_per_label(self):
        with _round_env():
            barrier = threading.Barrier(2)
            winners: list[str] = []
            conflicts: list[str] = []
            errors: list[BaseException] = []

            def _race(label):
                store = AvailabilityStore()
                try:
                    barrier.wait(timeout=30)
                    reserve_model(
                        store, "run-race", "1", label,
                        "org/gamma", "gamma",
                        "openrouter/acme/gamma:free")
                    winners.append(label)
                except ReservationConflict:
                    conflicts.append(label)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    store.close()

            workers = [
                threading.Thread(target=_race, args=(label,))
                for label in ("free-review-1", "free-review-2")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=60)
            assert not errors
            assert len(winners) == 1
            assert len(conflicts) == 1
            probe = AvailabilityStore()
            try:
                held = {(row.slot, row.family_id)
                        for row in probe.reservations("run-race", "1")}
                assert held == {(winners[0], "gamma")}
            finally:
                probe.close()


class RestartFromSnapshotTests(unittest.TestCase):
    def test_failed_reviewer_restarts_alone_from_same_snapshot(self):
        with _round_env() as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                frozen = routing.snapshot
                kept = wiring.record_receipt(
                    routing, "free-review-1",
                    report_text="# Report\n\nNo findings.\n",
                    reviewed_commit="abc123",
                    started_at="2026-10-24T00:00:00+00:00",
                    finished_at="2026-10-24T00:01:00+00:00")
                nxt = wiring.plan_fallback(
                    routing, "free-review-2", outcome_kind="disabled",
                    text="model not found: opencode/beta-free")
                assert nxt is not None
                assert routing.snapshot is frozen
                assert routing.receipts["free-review-1"] == kept
                assert routing.states["free-review-2"].requested\
                    .deployment_id == "opencode/beta-free"
                assert routing.states["free-review-2"].fallback_count == 1
                assert "free-review-2" not in routing.receipts
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_group_crossing_lands_in_receipt(self):
        with _round_env() as base:
            _quota_block_opencode(base)
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                assert routing.states["free-review-1"].effective\
                    .is_external
                receipt = wiring.record_receipt(
                    routing, "free-review-1",
                    report_text="# Report\n\nNo findings.\n",
                    reviewed_commit="abc123",
                    started_at="2026-10-24T00:00:00+00:00",
                    finished_at="2026-10-24T00:01:00+00:00")
                assert receipt.route_type == "external_free"
                assert receipt.provider_id == "openrouter"
                assert receipt.model_id == "acme/gamma:free"
                assert receipt.effective_model == (
                    "openrouter/acme/gamma:free")
                assert receipt.deployment_id == (
                    "openrouter/acme/gamma:free")
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()

    def test_fallback_reason_records_class_and_crossing(self):
        with _round_env() as base:
            assembly = _assemble(base)
            try:
                routing = assembly.routing
                assert routing is not None
                routing.store.record_error(
                    provider="opencode",
                    deployment_id="opencode/beta-free",
                    model="opencode/beta-free",
                    classified=classify_text(
                        "model not found: opencode/beta-free"),
                    run_id="run-n")
                nxt = wiring.plan_fallback(
                    routing, "free-review-1", outcome_kind="disabled",
                    text="model not found: opencode/alpha-free")
                assert nxt is not None
                reason = routing.states["free-review-1"].last_change_reason
                assert reason.startswith("not_found: ")
                assert "opencode/alpha-free" in reason
                assert "(group A->B)" in reason
                receipt = wiring.build_receipt(
                    routing, "free-review-1",
                    report_text="# Report\n",
                    reviewed_commit="abc123",
                    started_at="2026-10-24T00:00:00+00:00",
                    finished_at="2026-10-24T00:01:00+00:00")
                assert receipt.change_reason == reason
                assert receipt.fallback_count == 1
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()


class ReceiptTests(unittest.TestCase):
    def test_receipt_round_trip_with_route_fields(self):
        receipt = wiring.ReviewReceipt(
            run_id="run-n", round="1", slot="free-review-1",
            requested_model="opencode/alpha-free",
            effective_model="openrouter/acme/gamma:free",
            family_id="gamma", canonical_model_id="acme/gamma",
            provider="openrouter",
            deployment_id="openrouter/acme/gamma:free",
            rank=3, quality=0.85,
            rankings_snapshot="sha256:abc", catalog_snapshot="catalog-x",
            reviewed_commit="abc123", report_path="/tmp/r.md",
            sha256="0" * 64, result="validated", fallback_count=1,
            started_at="2026-10-24T00:00:00+00:00",
            finished_at="2026-10-24T00:01:00+00:00",
            route_type="external_free", provider_id="openrouter",
            model_id="acme/gamma:free",
            change_reason="not_found: opencode/alpha-free -> "
            "openrouter/acme/gamma:free (group A->B)")
        assert wiring.ReviewReceipt.from_dict(receipt.to_dict()) == receipt

    def test_receipt_reads_pre_v12_dicts(self):
        payload = {
            "run_id": "run-n", "round": "1", "slot": "free-review-1",
            "requested_model": "opencode/alpha-free",
            "effective_model": "opencode/alpha-free",
            "family_id": "alpha", "canonical_model_id": "org/alpha",
            "provider": "opencode",
            "deployment_id": "opencode/alpha-free",
            "rank": 1, "quality": 0.90,
            "rankings_snapshot": "sha256:abc",
            "catalog_snapshot": "catalog-x",
            "reviewed_commit": "abc123", "report_path": "/tmp/r.md",
            "sha256": "0" * 64, "result": "validated",
            "fallback_count": 0,
            "started_at": "2026-10-24T00:00:00+00:00",
            "finished_at": "2026-10-24T00:01:00+00:00",
        }
        receipt = wiring.ReviewReceipt.from_dict(payload)
        assert receipt.route_type == "opencode_native_free"
        assert receipt.provider_id == ""
        assert receipt.model_id == ""
        assert receipt.change_reason == ""

    def test_round2_policy_unchanged_over_native_targets(self):
        with _round_env() as base:
            first = _assemble(base, run_id="run-r2")
            try:
                routing = first.routing
                assert routing is not None
                first_receipt = wiring.build_receipt(
                    routing, "free-review-1",
                    report_text="# Report\n",
                    reviewed_commit="abc123",
                    started_at="2026-10-24T00:00:00+00:00",
                    finished_at="2026-10-24T00:01:00+00:00")
                r1 = wiring.R1Result(
                    receipts=(first_receipt,),
                    round_families=frozenset({"alpha", "beta"}),
                    tried_families=frozenset({"alpha", "beta"}))
            finally:
                if first.routing is not None:
                    first.routing.close()
            second = _assemble(base, run_id="run-r2", round="2", r1=r1)
            try:
                assert second.routing is not None
                assert second.prefer_families[0] == "alpha"
            finally:
                if second.routing is not None:
                    second.routing.close()


def _policy_match(policies, tool, resource):
    import fnmatch

    hits = []
    for position, entry in enumerate(policies):
        scope, _, pattern = entry["resource"].partition(":")
        if scope != tool:
            continue
        if fnmatch.fnmatchcase(resource, pattern):
            hits.append((position, entry["effect"]))
    return hits


def _policy_decision(policies, tool, resource):
    hits = _policy_match(policies, tool, resource)
    assert hits, f"no policy matches {tool}:{resource}"
    return hits[-1][1]


class KeySecrecyTests(unittest.TestCase):
    def test_tool_policies_deny_every_secret_vector(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            report = project / "reviews" / "r1.md"
            policies = iso.reviewer_policies(report, project)
            home = os.path.expanduser("~")
            for tool in ("read", "grep", "glob"):
                for secret in (
                        "/proc/self/environ",
                        "/proc/1/environ",
                        f"{home}/.config/opencode/auth.json",
                        f"{home}/.config/opencode/service.json",
                        "openceo-core/.env.free-router.local",
                        "/etc/passwd"):
                    assert _policy_decision(policies, tool, secret) == "deny", (
                        tool, secret)
            for tool in ("read", "grep", "glob"):
                assert _policy_decision(
                    policies, tool, str(project / "src" / "tiny.py")) == "allow"
            assert _policy_decision(policies, "write", str(report)) == "allow"
            assert _policy_decision(policies, "edit", str(report)) == "allow"

    def test_agent_entry_has_no_exec_tool(self):
        permission = iso.review_agent_entry()["permission"]
        assert set(permission) <= {"read", "glob", "grep", "edit", "write"}
        for banned in ("bash", "exec", "shell", "terminal", "run"):
            assert banned not in permission

    def test_handoff_accepts_each_group_b_credential(self):
        for provider in ("openrouter", "nvidia", "groq", "gemini"):
            env_var = credential_env_for(provider)
            providers = {
                provider: {
                    "settings": {"apiKey": "{env:%s}" % env_var},
                    "models": {"probe/free": {"name": "Probe"}},
                }
            }
            kept_providers, kept_secrets = iso.check_group_b_handoff(
                providers, {env_var: "TEST-ONLY-FAKE"})
            assert kept_providers == providers
            assert kept_secrets == {env_var: "TEST-ONLY-FAKE"}

    def test_handoff_refuses(self):
        good_providers = {
            "openrouter": {
                "settings": {
                    "apiKey": "{env:FREE_ROUTER_OPENROUTER_API_KEY}"},
                "models": {"probe/free": {"name": "Probe"}},
            }
        }
        good_secrets = {"FREE_ROUTER_OPENROUTER_API_KEY": "TEST-ONLY-FAKE"}
        assert iso.check_group_b_handoff(None, None) == (None, None)
        for providers, secrets in (
                (good_providers, None),
                (None, good_secrets),
                (good_providers, {}),
                (good_providers,
                 {"FREE_ROUTER_LITELLM_MASTER_KEY": "TEST-ONLY-FAKE"}),
                (good_providers, {"FREE_ROUTER_OPENROUTER_API_KEY": ""}),
                ({}, good_secrets),
                ({"openrouter": {"settings": {"apiKey": "TEST-ONLY-FAKE"},
                                 "models": {}}}, good_secrets),
                ({"openrouter": {"settings": {"apiKey": "{env:OTHER_KEY}"},
                                 "models": {}}}, good_secrets),
                ({"openrouter": {"settings": {"apiKey": "sk-live"},
                                 "models": {}}}, good_secrets)):
            with self.assertRaises(ValueError):
                iso.check_group_b_handoff(providers, secrets)

    def test_plan_delivers_secret_only_to_process_env(self):
        with _round_env() as base:
            _quota_block_opencode(base)
            assembly = _assemble(base)
            try:
                first = next(item for item in assembly.raw_pool
                             if item.get("label") == "free-review-1")
                handoff = first[wiring.REVIEW_TARGET_SPEC]
            finally:
                if assembly.routing is not None:
                    assembly.routing.close()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            plan = iso.plan_reviewer(
                argv=["opencode", "run", "--model",
                     "openrouter/acme/gamma:free"],
                workspace=workspace, base={}, report=None,
                project=workspace,
                providers=handoff["providers"],
                provider_secrets={"FREE_ROUTER_OPENROUTER_API_KEY":
                                  "TEST-ONLY-FAKE-OPENROUTER"})
            assert plan.env["FREE_ROUTER_OPENROUTER_API_KEY"] == (
                "TEST-ONLY-FAKE-OPENROUTER")
            content = plan.env["OPENCODE_CONFIG_CONTENT"]
            assert "{env:FREE_ROUTER_OPENROUTER_API_KEY}" in content
            assert "TEST-ONLY-FAKE-OPENROUTER" not in content
            assert "TEST-ONLY-FAKE-OPENROUTER" not in " ".join(plan.argv)
            assert "TEST-ONLY-FAKE-OPENROUTER" not in json.dumps(
                handoff["providers"])

    @needs_bwrap
    def test_fence_masks_secret_files_but_delivers_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            router_cfg = home / ".config" / "free-router"
            router_cfg.mkdir(parents=True)
            (router_cfg / "policy.yaml").write_text("CANARY-CONFIG\n")
            checkout = root / "checkout"
            core = checkout / "openceo-core"
            core.mkdir(parents=True)
            (core / ".env.free-router.local").write_text(
                "FREE_ROUTER_OPENROUTER_API_KEY=CANARY-KEY\n")
            fake = checkout / "opencode"
            fake.write_text(
                "#!/bin/sh\n"
                "echo \"ENV:$FREE_ROUTER_OPENROUTER_API_KEY\"\n"
                "echo \"DOTENV:$(cat "
                f"{checkout}/openceo-core/.env.free-router.local "
                "2>/dev/null)\"\n"
                "echo \"ROUTERCFG:$(cat "
                f"{home}/.config/free-router/policy.yaml "
                "2>/dev/null)\"\n",
                encoding="utf-8")
            fake.chmod(0o755)
            workspace = root / "ws"
            workspace.mkdir()
            providers = {
                "openrouter": {
                    "settings": {
                        "baseURL": "https://openrouter.ai/api/v1",
                        "apiKey": "{env:FREE_ROUTER_OPENROUTER_API_KEY}"},
                    "models": {"acme/gamma:free": {"name": "Gamma"}},
                }
            }
            plan = iso.plan_reviewer(
                argv=[str(fake), "run"], workspace=workspace, base={},
                report=None, project=workspace, home=home,
                extra_read_roots=[checkout],
                providers=providers,
                provider_secrets={"FREE_ROUTER_OPENROUTER_API_KEY":
                                  "TEST-ONLY-FAKE-OPENROUTER"})
            completed = subprocess.run(
                plan.argv, env=plan.env, cwd=plan.cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", timeout=120)
            assert completed.returncode == 0, completed.stdout
            assert "ENV:TEST-ONLY-FAKE-OPENROUTER" in completed.stdout
            assert "CANARY" not in completed.stdout


class HardenedGroupBExecutorTests(unittest.TestCase):
    def _spec(self, **overrides):
        spec = {
            "label": "free-review-1", "provider": "opencode",
            "model": "openrouter/acme/gamma:free", "family": "gamma",
            "command": ["opencode", "run", "-m",
                        "openrouter/acme/gamma:free"],
            "review_hardened": True,
            wiring.REVIEW_TARGET_SPEC: {
                "provider_id": "openrouter",
                "model_id": "acme/gamma:free",
                "route_type": "external_free",
                "env_var": "FREE_ROUTER_OPENROUTER_API_KEY",
                "providers": {
                    "openrouter": {
                        "settings": {
                            "baseURL": "https://openrouter.ai/api/v1",
                            "apiKey":
                            "{env:FREE_ROUTER_OPENROUTER_API_KEY}"},
                        "models": {"acme/gamma:free": {"name": "Gamma"}},
                    }
                },
            },
        }
        spec.update(overrides)
        return spec

    def test_missing_credential_refuses_without_spawning(self):
        import asyncio

        from model_routing.secrets import SecretsError
        from orchestration.live import ProcessExecutor

        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "ws"
            workdir.mkdir()
            executor = ProcessExecutor(pool=[self._spec()])
            with patch("orchestration.live.load_router_secrets",
                       side_effect=SecretsError("gone")):
                result = asyncio.run(executor.run(
                    "review:free-review-1", "hello", workdir, 60))
            assert result.returncode == 1
            assert result.output_tail.startswith("reviewer misconfigured")
            assert "FAKE" not in result.output_tail

    @needs_bwrap
    def test_group_b_run_receives_providers_and_env(self):
        import asyncio

        from model_routing.secrets import RouterSecrets
        from orchestration.live import ProcessExecutor

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bindir = base / "bin"
            bindir.mkdir()
            fake = bindir / "opencode"
            fake.write_text(
                "#!/bin/sh\n"
                "echo \"ARGV:$@\"\n"
                "if [ -z \"$FREE_ROUTER_OPENROUTER_API_KEY\" ]; then "
                "echo ENV-EMPTY; else echo ENV-SET; fi\n"
                "if grep -q '\"openrouter\"' "
                "\"$HOME/.config/opencode/opencode.json\" "
                "2>/dev/null; then echo HAS-GROUP-B-PROVIDER; "
                "else echo NO-GROUP-B-PROVIDER; fi\n",
                encoding="utf-8")
            fake.chmod(0o755)
            workdir = base / "ws"
            workdir.mkdir()
            spec = self._spec(command=[str(fake)],
                              extra_read_roots=[str(bindir)])
            executor = ProcessExecutor(pool=[spec])
            loaded = RouterSecrets(
                FREE_ROUTER_OPENROUTER_API_KEY="TEST-ONLY-FAKE-OPENROUTER")
            with patch("orchestration.live.load_router_secrets",
                       return_value=loaded):
                result = asyncio.run(executor.run(
                    "review:free-review-1", "hello", workdir, 60))
            assert result.returncode == 0, result.output_tail
            assert "ENV-SET" in result.output_tail
            assert "HAS-GROUP-B-PROVIDER" in result.output_tail
            assert "TEST-ONLY-FAKE" not in result.output_tail
