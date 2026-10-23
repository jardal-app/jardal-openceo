"""Availability, error classification and fault injection (TASK-2026-00291). Offline only."""

from __future__ import annotations

import http.client
import json
import multiprocessing
import os
import random
import time
import urllib.request
from pathlib import Path

import pytest

from model_routing import availability as router_availability
from model_routing import cli as router_cli
from model_routing import errors as router_errors
from model_routing import fault_proxy as router_proxy
from model_routing import live as router_live
from model_routing import models as router_models
from model_routing import probes as router_probes
from model_routing.availability import (
    Candidate,
    Exclusions,
    NoEligibleCandidate,
    ProfileRequirements,
    ReservationConflict,
    get_next_candidate,
    list_eligible_models,
    meets_profile,
    record_provider_error,
)
from model_routing.errors import (
    RetryBudget,
    classify_http,
    classify_response,
    classify_text,
    classify_timeout,
    cooldown_seconds,
)
from model_routing.fault_proxy import FaultProxy, FaultSpec

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


@pytest.fixture()
def store(tmp_path):
    handle = router_availability.AvailabilityStore(tmp_path / "availability.sqlite")
    yield handle
    handle.close()


def _deployment(deployment_id: str, provider: str, **overrides):
    base: dict = {
        "deployment_id": deployment_id,
        "provider": provider,
        "free_offered": True,
        "free_verified": True,
        "healthy": True,
        "input_price_usd": 0.0,
        "output_price_usd": 0.0,
    }
    base.update(overrides)
    return router_models.Deployment(**base)


def _entry(family: str, canonical: str, deployments: list, **capabilities):
    return router_models.CatalogEntry(
        family_id=family,
        canonical_model_id=canonical,
        capabilities=router_models.Capabilities(**capabilities),
        deployments=deployments,
    )


def _catalog(entries: list) -> router_models.CatalogDocument:
    return router_models.CatalogDocument(entries=entries)


# SPEC §9.2 classification ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_class", "action", "retryable"),
    [
        (429, "rate_limited", "cooldown_deployment", True),
        (401, "auth", "disable_credential", False),
        (403, "forbidden", "inspect", False),
        (402, "billing", "block_route", False),
        (404, "not_found", "block_route", False),
        (500, "server_error", "retry_next", True),
        (503, "server_error", "retry_next", True),
        (418, "unclassified", "record_only", False),
    ],
)
def test_classify_http_table(status, error_class, action, retryable):
    classified = classify_http(status, retry_after="45", detail="synthetic detail")
    assert classified.error_class == error_class
    assert classified.action == action
    assert classified.retryable is retryable
    if error_class in ("rate_limited", "server_error"):
        assert classified.retry_after_s == 45
    assert classified.excerpt == "synthetic detail"


def test_sanitize_excerpt_redacts_before_truncating():
    excerpt = router_errors.sanitize_excerpt("x" * 295 + " sk-" + "C" * 24)
    assert len(excerpt) == 300
    assert "sk-" not in excerpt
    assert router_live.scan_for_secrets(excerpt, ()) == []


def test_classify_http_400_needs_body_text():
    assert classify_http(400).error_class == "unclassified"
    assert classify_http(400, detail="weird shape").error_class == "unclassified"
    tools = classify_http(400, detail="this model does not support tools")
    assert (tools.error_class, tools.action) == ("tools_unsupported", "exclude_tools_profiles")
    missing = classify_http(400, detail="abc-123 is not a valid model ID")
    assert (missing.error_class, missing.action) == ("not_found", "block_route")
    overflow = classify_http(400, detail="maximum context length exceeded")
    assert (overflow.error_class, overflow.action) == ("context_exceeded", "escalate_context")


def test_classify_http_ignores_date_retry_after():
    classified = classify_http(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT")
    assert classified.retry_after_s is None
    assert classify_http(429, retry_after="-3").retry_after_s is None


@pytest.mark.parametrize(
    ("text", "error_class", "action"),
    [
        ("Daily free quota exhausted for this account", "daily_quota", "disable_provider"),
        ("FreeTierError: only usable from within OpenCode", "daily_quota", "disable_provider"),
        ("Billing required: purchase credits to continue", "billing", "block_route"),
        ("402 Payment Required", "billing", "block_route"),
        ("Unauthorized: invalid api key", "auth", "disable_credential"),
        ("Model not found: retired-flash", "not_found", "block_route"),
        ("abc-123 is not a valid model ID", "not_found", "block_route"),
        ("This model does not support tools", "tools_unsupported", "exclude_tools_profiles"),
        ("function calling is not enabled for this model", "tools_unsupported",
         "exclude_tools_profiles"),
        ("maximum context length exceeded", "context_exceeded", "escalate_context"),
        ("connection reset by peer", "server_error", "retry_next"),
        ("empty response from model", "bad_response", "fail_safe"),
        ("corrupt tool_call_id in reply", "bad_response", "fail_safe"),
        ("Service Unavailable, try later", "server_error", "retry_next"),
        ("a confusing brand-new failure mode", "unclassified", "record_only"),
    ],
)
def test_classify_text_table(text, error_class, action):
    classified = classify_text(text)
    assert classified.error_class == error_class
    assert classified.action == action


def test_classify_text_cut_stream_after_partial_output():
    classified = classify_text("connection closed mid-stream", responded=True)
    assert classified.error_class == "stream_cut"
    assert classified.action == "rebuild_from_checkpoint"
    assert classified.retryable is False


def test_classify_text_excerpt_is_sanitized():
    classified = classify_text("failed with token sk-abcdefgh12345678  end")
    assert "sk-abcdefgh12345678" not in classified.excerpt
    assert "[REDACTED]" in classified.excerpt
    assert len(classified.excerpt) <= 300


@pytest.mark.parametrize(
    ("anomaly", "error_class", "action"),
    [
        ("stream_cut", "stream_cut", "rebuild_from_checkpoint"),
        ("empty", "bad_response", "fail_safe"),
        ("bad_json", "bad_response", "fail_safe"),
        ("corrupt_tool_call_id", "bad_response", "fail_safe"),
    ],
)
def test_classify_response_anomalies(anomaly, error_class, action):
    classified = classify_response(anomaly)
    assert (classified.error_class, classified.action) == (error_class, action)
    assert classified.retryable is False


def test_classify_timeout_before_and_after_output():
    assert classify_timeout(responded=False).error_class == "server_error"
    assert classify_timeout(responded=False).retryable is True
    assert classify_timeout(responded=True).error_class == "stream_cut"


def test_retry_budget_hops_and_backoff_bounds():
    budget = RetryBudget()
    assert [budget.allows(hop) for hop in range(7)] == [True] * 5 + [False] * 2
    assert budget.allows(-1) is False
    rng = random.Random(7)
    first = [budget.delay(0, rng) for _ in range(50)]
    assert all(0 <= value <= 1.0 for value in first)
    assert any(value > 0.5 for value in first)
    rng = random.Random(7)
    assert all(budget.delay(3, rng) <= 8.0 for _ in range(20))
    assert RetryBudget(max_hops=2, base_s=1.0, cap_s=1.5).delay(9, random.Random(1)) <= 1.5


def test_cooldown_seconds_progressive_and_retry_after():
    assert cooldown_seconds(1) == 60
    assert cooldown_seconds(2) == 120
    assert cooldown_seconds(3) == 240
    assert cooldown_seconds(1, 300) == 300
    assert cooldown_seconds(3, 30) == 240
    assert cooldown_seconds(10) == 3600


# Reservations, cooldowns, blocks ----------------------------------------------


def test_reserve_and_release_round_trip(store):
    held = store.reserve("run-1", "r1", "s1", "canon-a", "fama", "dep-a")
    assert held.slot == "s1"
    assert [item.slot for item in store.reservations("run-1", "r1")] == ["s1"]
    assert store.release("run-1", "r1", "s1") is True
    assert store.release("run-1", "r1", "s1") is False
    assert store.reservations("run-1", "r1") == []


def test_reserve_round_is_atomic(store):
    store.reserve("run-1", "r1", "taken", "canon-x", "famx", "dep-x")
    with pytest.raises(ReservationConflict):
        store.reserve_round("run-1", "r1", [
            ("s1", "canon-a", "fama", "dep-a"),
            ("taken", "canon-b", "famb", "dep-b"),
        ])
    assert [item.slot for item in store.reservations("run-1", "r1")] == ["taken"]


def test_reserve_round_rejects_duplicate_family_inside(store):
    with pytest.raises(ReservationConflict):
        store.reserve_round("run-1", "r1", [
            ("s1", "canon-a", "fama", "dep-a"),
            ("s2", "canon-b", "fama", "dep-b"),
        ])
    assert store.reservations("run-1", "r1") == []


def test_reserve_family_conflict_needs_recorded_exception(store):
    store.reserve("run-1", "r1", "s1", "canon-a", "fama", "dep-a")
    with pytest.raises(ReservationConflict):
        store.reserve("run-1", "r1", "s2", "canon-b", "fama", "dep-b")
    store.reserve("run-1", "r2", "s2", "canon-b", "fama", "dep-b")
    assert len(store.reservations("run-1", "r2")) == 1
    excused = store.reserve("run-1", "r1", "s3", "canon-c", "fama", "dep-c",
                            exception="quorum fallback, owner rule T07")
    assert excused.exception == "quorum fallback, owner rule T07"
    assert len(store.reservations("run-1", "r1")) == 2


def test_t04_two_processes_race_for_one_family(tmp_path):
    path = tmp_path / "race.sqlite"
    router_availability.AvailabilityStore(path).close()

    def attempt(queue):
        worker = router_availability.AvailabilityStore(path)
        try:
            worker.reserve("run-race", "r1", f"slot-{os.getpid()}", "canon",
                           "deepseek", f"dep-{os.getpid()}")
            queue.put("ok")
        except ReservationConflict:
            queue.put("conflict")
        finally:
            worker.close()

    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    contenders = [context.Process(target=attempt, args=(queue,)) for _ in range(2)]
    for contender in contenders:
        contender.start()
    for contender in contenders:
        contender.join(timeout=60)
    assert all(contender.exitcode == 0 for contender in contenders)
    assert sorted([queue.get(timeout=10), queue.get(timeout=10)]) == ["conflict", "ok"]
    check = router_availability.AvailabilityStore(path)
    try:
        assert len(check.reservations("run-race", "r1")) == 1
    finally:
        check.close()


def test_expired_reservations_read_as_absent_and_recover_purges(store):
    now = time.time()
    store.reserve("run-1", "r1", "old", "canon-a", "fama", "dep-a", ttl_s=10, now=now)
    store.reserve("run-1", "r1", "live", "canon-b", "famb", "dep-b", now=now)
    later = now + 60
    assert [item.slot for item in store.reservations("run-1", "r1", now=later)] == ["live"]
    purged, live = store.recover(now=later)
    assert purged == 1
    assert [item.slot for item in live] == ["live"]


def test_crash_recovery_reopen_keeps_live_reservations(tmp_path):
    path = tmp_path / "crash.sqlite"
    before = time.time() - 120
    first = router_availability.AvailabilityStore(path)
    first.reserve("run-9", "r1", "old", "canon-a", "fama", "dep-a", ttl_s=10, now=before)
    first.reserve("run-9", "r1", "live", "canon-b", "famb", "dep-b", now=before)
    first.close()
    second = router_availability.AvailabilityStore(path)
    try:
        assert [item.slot for item in second.reservations("run-9", "r1")] == ["live"]
        purged, live = second.recover()
        assert purged == 1
        assert [item.slot for item in live] == ["live"]
    finally:
        second.close()


def test_cooldown_lifecycle_and_progressive_stretch(store):
    now = time.time()
    assert store.cooldown_for("dep-a", now=now) is None
    store.set_cooldown("dep-a", "429 once", seconds=10, now=now)
    first = store.cooldown_for("dep-a", now=now)
    assert first is not None and first["failures"] == 1
    assert first["until_ts"] == pytest.approx(now + 60)
    store.set_cooldown("dep-a", "429 again", seconds=10, now=now + 61)
    second = store.cooldown_for("dep-a", now=now + 61)
    assert second is not None and second["failures"] == 2
    assert second["until_ts"] == pytest.approx(now + 61 + 120)
    assert store.cooldown_for("dep-a", now=now + 10000) is None


def test_provider_block_lifecycle(store):
    now = time.time()
    store.block_provider("openrouter", "daily quota", seconds=100, now=now)
    assert store.provider_blocked("openrouter", now=now)["reason"] == "daily quota"
    assert store.provider_blocked("openrouter", now=now + 101) is None


def test_disabled_route_blocks_until_cleared(store):
    store.disable_route("dep-bad", "billing: model left the free tier")
    assert store.route_disabled("dep-bad") == "billing: model left the free tier"
    assert store.route_disabled("dep-ok") is None


def test_counters_roll_daily(store):
    now = time.time()
    assert store.bump_counter("groq", now=now) == 1
    assert store.bump_counter("groq", now=now + 5) == 2
    assert store.bump_counter("groq", now=now + 86401) == 1


# Error registry and audit -----------------------------------------------------


def test_record_error_applies_cooldown_and_registers(store):
    now = time.time()
    record = record_provider_error(
        store, provider="openrouter", deployment_id="openrouter/a:free",
        model="a:free", classified=classify_http(429, retry_after="120"),
        run_id="run-t01", now=now)
    assert record.error_class == "rate_limited"
    assert record.retry_after_s == 120
    held = store.cooldown_for("openrouter/a:free", now=now)
    assert held is not None and held["retry_after_s"] == 120
    rows = store.errors_since(0)
    assert [(item.provider, item.deployment_id, item.model, item.action, item.run_id)
            for item in rows] == [
        ("openrouter", "openrouter/a:free", "a:free", "cooldown_deployment", "run-t01")]
    assert rows[0].ts == pytest.approx(now)


@pytest.mark.parametrize(
    ("classified", "check"),
    [
        (classify_text("daily quota exceeded"), "provider"),
        (classify_http(401), "provider"),
        (classify_http(402), "route"),
        (classify_http(404), "route"),
    ],
)
def test_record_error_blocks_provider_or_route(store, classified, check):
    record_provider_error(
        store, provider="groq", deployment_id="groq/some-model",
        model="some-model", classified=classified, run_id="run-block")
    if check == "provider":
        assert store.provider_blocked("groq") is not None
    else:
        assert store.route_disabled("groq/some-model") is not None


@pytest.mark.parametrize(
    "classified",
    [
        classify_http(429), classify_text("daily quota"), classify_http(503),
        classify_http(401), classify_http(403), classify_http(402),
        classify_http(404), classify_text("does not support tools"),
        classify_text("maximum context length"), classify_response("stream_cut"),
        classify_response("empty"), classify_text("surprise!"),
    ],
)
def test_record_error_logs_every_class(store, classified):
    record_provider_error(
        store, provider="nvidia", deployment_id="nvidia/m", model="m",
        classified=classified, run_id="run-all")
    rows = store.errors_since(0)
    assert len(rows) == 1
    assert rows[0].error_class == classified.error_class
    assert rows[0].action == classified.action


def test_errors_since_filters_by_time(store):
    now = time.time()
    record_provider_error(
        store, provider="groq", deployment_id="groq/a", model="a",
        classified=classify_http(503), run_id="run-1", now=now - 100)
    record_provider_error(
        store, provider="groq", deployment_id="groq/a", model="a",
        classified=classify_http(503), run_id="run-1", now=now)
    assert len(store.errors_since(now - 50)) == 1
    assert len(store.errors_since(0)) == 2


def test_recorded_401_fixture_classifies_to_disable_credential(store):
    recorded = json.loads((FIXTURES / "sources-401-groq.json").read_text(encoding="utf-8"))
    classified = classify_http(recorded["http_status"])
    assert (classified.error_class, classified.action) == ("auth", "disable_credential")
    record_provider_error(
        store, provider=recorded["provider"], deployment_id="groq/catalog",
        model="catalog", classified=classified, run_id="run-401")
    assert store.provider_blocked("groq") is not None


def test_registry_never_stores_key_material(store):
    record_provider_error(
        store, provider="groq", deployment_id="groq/a", model="a",
        classified=classify_http(500),
        excerpt="key gsk_abcdefgh12345678 header " + "Bearer" + " " + "q" * 20 + " end",
        run_id="run-sec")
    blob = "\n".join(item.excerpt for item in store.errors_since(0))
    assert "gsk_abcdefgh12345678" not in blob
    assert "q" * 20 not in blob
    assert "[REDACTED]" in blob
    assert router_live.scan_for_secrets(blob, ()) == []


def test_audit_round_trip_without_content(store):
    now = time.time()
    store.write_audit("run-1", "fallback", "a:free  ->  b  (429)", now=now)
    assert store.audit_since("run-1", 0) == [(pytest.approx(now), "fallback",
                                              "a:free -> b (429)")]
    assert store.audit_since("run-2", 0) == []


# Eligibility, profiles and candidates -----------------------------------------


def test_meets_profile_hard_gates():
    entry = _entry("fama", "canon-a", [_deployment("dep-a", "groq")],
                   tools=True, context_tokens=128000)
    deployment = _deployment(
        "dep-a", "groq",
        limits=router_models.DeploymentLimits(tpm=6000, rpd=1000),
        approved_confidentiality=[router_models.Confidentiality.INTERNAL])
    gates = ProfileRequirements(needs_tools=True, min_context_tokens=32000,
                                min_tpm=1000, min_rpd=100,
                                confidentiality=router_models.Confidentiality.INTERNAL)
    assert meets_profile(entry, deployment, gates) == (True, "")
    assert meets_profile(_entry("f", "c", [], tools=False), deployment, gates)[0] is False
    unknown = _entry("f", "c", [_deployment("d", "g")])
    assert meets_profile(unknown, deployment, gates)[0] is False
    assert meets_profile(entry, _deployment("d", "g"), gates)[0] is False
    assert meets_profile(entry, deployment, ProfileRequirements())[0] is True


def test_list_eligible_models_filters_every_gate(store):
    catalog = _catalog([
        _entry("fama", "canon-a", [_deployment("dep-good", "groq")], tools=True),
        _entry("famb", "canon-b", [_deployment("dep-unverified", "groq", free_verified=False)],
               tools=True),
        _entry("famc", "canon-c", [_deployment("dep-sick", "groq", healthy=False)], tools=True),
        _entry("famd", "canon-d", [_deployment("dep-notools", "groq")]),
    ])
    store.set_cooldown("dep-good", "flaky", seconds=600)
    assert list_eligible_models("free-review", catalog, store) == []
    fresh = router_availability.AvailabilityStore(store._path.parent / "fresh.sqlite")
    try:
        eligible = list_eligible_models("free-review", catalog, fresh)
        assert [item.deployment_id for item in eligible] == ["dep-good"]
        general = list_eligible_models("free-general", catalog, fresh)
        assert [item.deployment_id for item in general] == ["dep-good", "dep-notools"]
    finally:
        fresh.close()


def test_list_eligible_models_rejects_unknown_profile(store):
    catalog = _catalog([
        _entry("fama", "canon-a", [_deployment("dep-a", "groq")], tools=True),
    ])
    with pytest.raises(ValueError, match="unknown profile"):
        list_eligible_models("free-typo", catalog, store)
    bypassed = list_eligible_models("free-typo", catalog, store, requirements=ProfileRequirements())
    assert [item.deployment_id for item in bypassed] == ["dep-a"]


def test_get_next_candidate_skips_and_blocks(store):
    pool = [
        Candidate("dep-a", "fama", "canon-a", "openrouter", 0.9),
        Candidate("dep-b", "famb", "canon-b", "groq", 0.8),
        Candidate("dep-c", "famc", "canon-c", "nvidia", 0.7),
    ]
    assert get_next_candidate(pool, store) == pool[0]
    assert get_next_candidate(pool, store, exclusions=Exclusions(families=frozenset({"fama"}))) == pool[1]
    assert get_next_candidate(pool, store, minimum_quality=0.75) == pool[0]
    assert get_next_candidate(pool, store, minimum_quality=0.85) == pool[0]
    with pytest.raises(NoEligibleCandidate):
        get_next_candidate(pool, store, minimum_quality=0.95)
    store.set_cooldown("dep-a", "429", seconds=600)
    store.block_provider("groq", "quota", seconds=600)
    store.disable_route("dep-c", "billing")
    with pytest.raises(NoEligibleCandidate):
        get_next_candidate(pool, store)


def test_backups_share_canonical_not_opinion():
    first = Candidate("openrouter/m:free", "fama", "canon-m", "openrouter")
    backup = Candidate("groq/m", "fama", "canon-m", "groq")
    other = Candidate("groq/n", "famb", "canon-n", "groq")
    assert router_availability.backups_of(first, [first, backup, other]) == [backup]


def test_t01_429_moves_to_next_eligible_deployment(store):
    pool = [
        Candidate("openrouter/a:free", "fama", "canon-a", "openrouter", 0.9),
        Candidate("groq/b", "famb", "canon-b", "groq", 0.8),
    ]
    assert get_next_candidate(pool, store) == pool[0]
    record_provider_error(
        store, provider="openrouter", deployment_id="openrouter/a:free",
        model="a:free", classified=classify_http(429, retry_after="120"),
        run_id="run-t01")
    assert get_next_candidate(pool, store) == pool[1]
    rows = store.errors_since(0)
    assert [(item.error_class, item.action) for item in rows] == [
        ("rate_limited", "cooldown_deployment")]


def test_t02_provider_quota_blocks_only_that_provider(store):
    pool = [
        Candidate("openrouter/a:free", "fama", "canon-a", "openrouter", 0.9),
        Candidate("openrouter/b:free", "famb", "canon-b", "openrouter", 0.85),
        Candidate("nvidia/c", "famc", "canon-c", "nvidia", 0.8),
    ]
    record_provider_error(
        store, provider="openrouter", deployment_id="openrouter/a:free",
        model="a:free", classified=classify_text("daily quota exceeded"),
        run_id="run-t02")
    assert store.provider_blocked("openrouter") is not None
    assert get_next_candidate(pool, store) == pool[2]


def test_t03_fallback_only_when_profile_floors_hold(store):
    gates = ProfileRequirements(min_context_tokens=32000, min_tpm=1000,
                                confidentiality=router_models.Confidentiality.INTERNAL)
    nvidia_entry = _entry("famn", "canon-n", [_deployment(
        "nvidia/n", "nvidia",
        limits=router_models.DeploymentLimits(tpm=6000),
        approved_confidentiality=[router_models.Confidentiality.INTERNAL])],
        context_tokens=128000)
    weak_groq = _entry("famg", "canon-g", [_deployment("groq/g", "groq")])
    assert meets_profile(weak_groq, weak_groq.deployments[0], gates)[0] is False
    record_provider_error(
        store, provider="nvidia", deployment_id="nvidia/n", model="n",
        classified=classify_http(503), run_id="run-t03")
    catalog = _catalog([nvidia_entry, weak_groq])
    assert list_eligible_models("free-general", catalog, store, requirements=gates) == []
    strong_groq = _entry("famg", "canon-g", [_deployment(
        "groq/g", "groq",
        limits=router_models.DeploymentLimits(tpm=6000),
        approved_confidentiality=[router_models.Confidentiality.INTERNAL])],
        context_tokens=128000)
    catalog = _catalog([nvidia_entry, strong_groq])
    eligible = list_eligible_models("free-general", catalog, store, requirements=gates)
    assert [item.deployment_id for item in eligible] == ["groq/g"]


def test_t05_same_base_model_is_backup_not_opinion(store):
    pool = [
        Candidate("openrouter/m:free", "fama", "canon-m", "openrouter", 0.9),
        Candidate("groq/m", "fama", "canon-m", "groq", 0.85),
        Candidate("nvidia/n", "famb", "canon-n", "nvidia", 0.8),
    ]
    assert router_availability.backups_of(pool[0], pool) == [pool[1]]
    second_opinion = get_next_candidate(
        pool, store, exclusions=Exclusions(canonicals=frozenset({"canon-m"})))
    assert second_opinion == pool[2]


def test_t16_defective_tools_excluded_from_review_profile(store):
    catalog = _catalog([
        _entry("fama", "canon-a", [_deployment("dep-broken", "groq")]),
        _entry("famb", "canon-b", [_deployment("dep-ok", "groq")], tools=True),
    ])
    eligible = list_eligible_models("free-review", catalog, store)
    assert [item.deployment_id for item in eligible] == ["dep-ok"]


# Fault-injection proxy --------------------------------------------------------


def test_proxy_rate_limited_429_with_retry_after():
    with FaultProxy({"groq": FaultSpec("rate_limited", retry_after_s=90)}) as proxy:
        with pytest.raises(router_live.LiveRateLimited) as caught:
            proxy.post("groq", {"model": "m"})
        assert caught.value.provider == "groq"
        assert caught.value.retry_after == "90"
        assert proxy.served == [("groq", "rate_limited")]


def test_proxy_daily_quota_body_names_the_quota():
    with FaultProxy({"nvidia": "daily_quota"}) as proxy:
        with pytest.raises(router_live.LiveRateLimited):
            proxy.post("nvidia", {"model": "m"})
        request = urllib.request.Request(
            f"{proxy.base_url}/fault/nvidia", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(request, timeout=10)
            raise AssertionError("quota fault unexpectedly served")
        except urllib.error.HTTPError as exc:
            assert exc.code == 429
            assert exc.headers.get("Retry-After") == "60"
            assert "daily" in exc.read().decode("utf-8").lower()


@pytest.mark.parametrize(
    ("kind", "status", "retryable"),
    [("server_error", 503, True), ("billing", 402, False)],
)
def test_proxy_http_faults_map_to_probe_errors(kind, status, retryable):
    with FaultProxy({"groq": FaultSpec(kind)}) as proxy:
        with pytest.raises(router_probes.ProbeError) as caught:
            proxy.post("groq", {"model": "m"})
        assert caught.value.http_status == status
        assert caught.value.retryable is retryable


def test_proxy_timeout_fault_exceeds_client_deadline():
    with FaultProxy({"groq": FaultSpec("timeout", delay_s=1.0)}) as proxy:
        with pytest.raises(router_probes.ProbeError, match="failed"):
            proxy.post("groq", {"model": "m"}, timeout=0.2)


def test_proxy_empty_fault_is_not_json():
    with FaultProxy({"groq": "empty"}) as proxy:
        with pytest.raises(router_probes.ProbeError, match="empty payload"):
            proxy.post("groq", {"model": "m"})


def test_proxy_ok_control_answers_minimal_chat():
    with FaultProxy({"groq": "ok"}) as proxy:
        payload = proxy.post("groq", {"model": "m"})
    assert payload["choices"][0]["message"]["content"] == "ok"


def test_proxy_stream_cut_ends_without_done():
    with FaultProxy({"groq": "stream_cut"}) as proxy:
        host = proxy.base_url.split("://", 1)[1]
        host, _, port = host.partition(":")
        connection = http.client.HTTPConnection(host, int(port), timeout=10)
        connection.request("POST", "/fault/groq", body="{}",
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        body = response.read()
        connection.close()
    assert b"par" in body
    assert b"[DONE]" not in body


def test_proxy_served_log_carries_no_bodies():
    with FaultProxy({"groq": "rate_limited"}, default="ok") as proxy:
        with pytest.raises(router_live.LiveRateLimited):
            proxy.post("groq", {"model": "m", "messages": [{"role": "user", "content": "secret"}]})
        proxy.post("nvidia", {"model": "n"})
        assert proxy.served == [("groq", "rate_limited"), ("nvidia", "ok")]


def test_run_probe_through_proxy_reports_429(isolated_home):
    with FaultProxy({"groq": "rate_limited"}) as proxy:
        sender = lambda url, headers, payload: proxy.post("groq", payload)  # noqa: E731
        with pytest.raises(router_live.LiveRateLimited):
            router_probes.run_probe("groq", "chat", "openai/gpt-oss-20b", {},
                                    enabled=True, post=sender)


# CLI: errors --since ----------------------------------------------------------


def test_cli_errors_since_lists_records(isolated_home, capsys):
    handle = router_availability.AvailabilityStore()
    try:
        handle.record_error(
            provider="groq", deployment_id="groq/a", model="a",
            classified=classify_http(429, retry_after="30"), run_id="run-cli")
    finally:
        handle.close()
    assert router_cli.main(["errors", "--since", "24h"]) == 0
    out = capsys.readouterr().out
    assert "rate_limited" in out and "cooldown_deployment" in out and "run-cli" in out


def test_cli_errors_since_json_and_iso(isolated_home, capsys):
    handle = router_availability.AvailabilityStore()
    try:
        handle.record_error(
            provider="groq", deployment_id="groq/a", model="a",
            classified=classify_http(503), run_id="run-json")
    finally:
        handle.close()
    assert router_cli.main(["errors", "--since", "2020-01-01T00:00:00+00:00", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(item["class"], item["action"], item["run_id"]) for item in payload] == [
        ("server_error", "retry_next", "run-json")]
    assert router_cli.main(["errors", "--since", "2030-01-01T00:00:00+00:00"]) == 0
    assert capsys.readouterr().out.strip() == "no errors recorded"


def test_cli_errors_since_rejects_garbage(isolated_home, capsys):
    assert router_cli.main(["errors", "--since", "someday"]) == 2


def test_status_reports_latest_snapshot(isolated_home):
    from model_routing.models import collect_status
    from model_routing.paths import history_dir

    assert collect_status().snapshot is None
    history_dir().mkdir(parents=True)
    (history_dir() / "catalog-20261023T060000Z-aaa.json").write_text("{}", encoding="utf-8")
    (history_dir() / "catalog-20261023T070000Z-bbb.json").write_text("{}", encoding="utf-8")
    (history_dir() / "catalog-20261023T080000Z-ccc.tmp").write_text("{}", encoding="utf-8")
    (history_dir() / "scratch.txt").write_text("{}", encoding="utf-8")
    assert collect_status().snapshot == "catalog-20261023T070000Z-bbb"

