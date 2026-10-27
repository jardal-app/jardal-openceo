"""Native serve evidence producer for group A verification (TASK-2026-00315). Offline only."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from model_routing import review_wiring as review_mod
from model_routing import serve_evidence as evidence_mod
from model_routing import sources as router_sources
from model_routing import terms as router_terms
from model_routing import probes as router_probes
from model_routing import research_wiring as research_mod
from model_routing.availability import Candidate


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


def _terms():
    today = datetime.now(timezone.utc).date().isoformat()
    terms = [
        router_terms.ProviderTerms(
            provider=provider, free_tier=True, requires_payment_method=False,
            one_time_credit_only=False, data_policy="unknown",
            retention="unknown", confidentiality_allowed=["public"],
            source="https://example.test/console", verified_at=today,
            basis="synthetic",
        )
        for provider in router_sources.PROVIDERS
    ]
    free_list = router_terms.OpenCodeFreeList(
        source="https://example.test/models", verified_at=today,
        basis="synthetic", model_ids=["big-pickle"],
    )
    return router_terms.TermsFile(terms=terms, opencode_free_models=free_list)


# Schema and store -----------------------------------------------------------


def test_record_and_load_roundtrip(tmp_path):
    target = tmp_path / "serve-evidence.json"
    stamp = evidence_mod.record_native_serve("opencode", "big-pickle", path=target)
    assert stamp is not None
    assert evidence_mod.load_serve_evidence(target) == {"big-pickle": stamp}


def test_default_path_lives_under_state_dir(isolated_home):
    stamp = evidence_mod.record_native_serve("opencode", "big-pickle")
    assert stamp is not None
    assert evidence_mod.load_serve_evidence() == {"big-pickle": stamp}


def test_prefixed_deployment_id_normalizes_to_bare_id(tmp_path):
    target = tmp_path / "serve-evidence.json"
    stamp = evidence_mod.record_native_serve(
        "opencode", "opencode/big-pickle", path=target)
    assert stamp is not None
    assert evidence_mod.load_serve_evidence(target) == {"big-pickle": stamp}


@pytest.mark.parametrize("provider,model", [
    ("openrouter", "vendor/model:free"),
    ("groq", "openai/gpt-oss-20b"),
    ("opencode", ""),
    ("opencode", "   "),
    ("opencode", "a/b"),
    ("opencode", "/"),
])
def test_non_native_or_bad_ids_record_nothing(tmp_path, provider, model):
    target = tmp_path / "serve-evidence.json"
    assert evidence_mod.record_native_serve(provider, model, path=target) is None
    assert not target.exists()


def test_naive_timestamp_is_refused(tmp_path):
    target = tmp_path / "serve-evidence.json"
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert evidence_mod.record_native_serve(
        "opencode", "big-pickle", observed_at=naive, path=target) is None
    assert not target.exists()


def test_missing_file_loads_empty(tmp_path):
    assert evidence_mod.load_serve_evidence(tmp_path / "absent.json") == {}


def test_corrupt_or_schemaless_file_loads_empty(tmp_path):
    for body in ("{oops", "[1, 2]", '{"entries": ["not-a-map"]}', '{"other": {}}'):
        target = tmp_path / "serve-evidence.json"
        target.write_text(body, encoding="utf-8")
        assert evidence_mod.load_serve_evidence(target) == {}


def test_malformed_entries_are_skipped(tmp_path):
    target = tmp_path / "serve-evidence.json"
    now = datetime.now(timezone.utc).isoformat()
    target.write_text(json.dumps({"schema_version": 1, "entries": {
        "big-pickle": now,
        "opencode/other": now,
        "bad-stamp": "not-a-date",
    }}), encoding="utf-8")
    assert evidence_mod.load_serve_evidence(target) == {"big-pickle": now}


# TTL ------------------------------------------------------------------------


def test_fresh_within_seven_days_stale_after(tmp_path):
    assert evidence_mod.is_fresh(datetime.now(timezone.utc).isoformat())
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    assert not evidence_mod.is_fresh(old)
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    assert not evidence_mod.is_fresh(naive)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    assert not evidence_mod.is_fresh(future)


def test_write_prunes_stale_entries(tmp_path):
    target = tmp_path / "serve-evidence.json"
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    target.write_text(json.dumps({"schema_version": 1, "entries": {
        "big-pickle": old, "beam-free": fresh}}), encoding="utf-8")
    evidence_mod.record_native_serve("opencode", "big-pickle", path=target)
    loaded = evidence_mod.load_serve_evidence(target)
    assert set(loaded) == {"big-pickle", "beam-free"}
    assert loaded["beam-free"] == fresh


# Catalog integration --------------------------------------------------------


def test_recorded_evidence_verifies_offered_model():
    terms = _terms()
    raw = {"data": [{"id": "big-pickle", "object": "model"}]}
    before = router_sources.parse_opencode_models(raw, terms.opencode_free_models, terms)
    assert before[0].deployments[0].free_verified is False
    stamp = datetime.now(timezone.utc).isoformat()
    after = router_sources.parse_opencode_models(
        raw, terms.opencode_free_models, terms, {"big-pickle": stamp})
    assert after[0].deployments[0].free_verified is True


def test_stale_recorded_evidence_stays_unverified():
    terms = _terms()
    raw = {"data": [{"id": "big-pickle", "object": "model"}]}
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    (entry,) = router_sources.parse_opencode_models(
        raw, terms.opencode_free_models, terms, {"big-pickle": old})
    assert entry.deployments[0].free_verified is False


def test_refresh_source_accepts_recorded_evidence(tmp_path):
    terms = _terms()
    stamp = datetime.now(timezone.utc).isoformat()
    target = tmp_path / "serve-evidence.json"
    evidence_mod.record_native_serve("opencode", "big-pickle", path=target)
    evidence = evidence_mod.load_serve_evidence(target)

    def _get(url, headers):
        if url.endswith("/models"):
            return {"data": [{"id": "big-pickle", "object": "model"}]}
        raise AssertionError(f"unexpected GET {url}")

    fetched = router_sources.refresh_source(
        "opencode", terms, serve_evidence=evidence, get=_get)
    assert fetched.entries[0].deployments[0].free_verified is True
    fetched = router_sources.refresh_source("opencode", terms, get=_get)
    assert fetched.entries[0].deployments[0].free_verified is False


# CLI ------------------------------------------------------------------------


def test_cli_records_and_lists_evidence(tmp_path, capsys):
    from model_routing import cli as router_cli

    state = tmp_path / "state"
    assert router_cli.main([
        "serve-evidence", "--model", "big-pickle",
        "--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert "recorded big-pickle" in out
    assert router_cli.main(
        ["serve-evidence", "--state-dir", str(state), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["model"], row["fresh"]) for row in rows] == [("big-pickle", True)]


def test_cli_refuses_non_native_model(tmp_path, capsys):
    from model_routing import cli as router_cli

    assert router_cli.main([
        "serve-evidence", "--model", "vendor/model:free",
        "--state-dir", str(tmp_path / "state")]) == 2
    assert "refused" in capsys.readouterr().out


def test_cli_empty_store_lists_nothing(tmp_path, capsys):
    from model_routing import cli as router_cli

    assert router_cli.main(
        ["serve-evidence", "--state-dir", str(tmp_path / "state")]) == 0
    assert "no entries" in capsys.readouterr().out


# Producer hooks -------------------------------------------------------------


def _native_ok_events():
    def _event(payload):
        return json.dumps(payload)

    return "\n".join([
        _event({"type": "text", "part": {"text": "ok"}}),
        _event({"type": "step_finish", "part": {"cost": 0}}),
    ]) + "\n"


def test_successful_native_probe_records_evidence(isolated_home):
    def _runner(argv, _cwd, _timeout):
        return router_probes.NativeRun(
            args=tuple(argv), returncode=0, stdout=_native_ok_events(), stderr="")

    result, _ = router_probes.run_native_probe(
        "opencode", "chat", "big-pickle", enabled=True, runner=_runner)
    assert result.deployment_id == "opencode/big-pickle"
    assert set(evidence_mod.load_serve_evidence()) == {"big-pickle"}


def test_failed_native_probe_records_nothing(isolated_home):
    event = {"type": "error",
             "error": {"type": "provider.auth", "message": "login expired", "status": 401}}

    def _failing(argv, _cwd, _timeout):
        return router_probes.NativeRun(
            args=tuple(argv), returncode=1,
            stdout=json.dumps(event) + "\n", stderr="")

    with pytest.raises(router_probes.ProbeError):
        router_probes.run_native_probe(
            "opencode", "chat", "big-pickle", enabled=True, runner=_failing)
    assert evidence_mod.load_serve_evidence() == {}


def _native_routing(tmp_path):
    target = review_mod.ReviewTarget(
        deployment_id="opencode/big-pickle", family_id="test-family",
        canonical_model_id="opencode/big-pickle", provider="opencode",
        quality=0.9, route_type="opencode_native_free",
        provider_id="opencode", model_id="big-pickle",
        opencode_model="opencode/big-pickle")
    return SimpleNamespace(
        run_id="run-ev", round="1", receipts_dir=tmp_path,
        receipts={}, candidates=[target],
        states={"free-review-1": review_mod.LabelState(
            requested=target, effective=target)},
        snapshot=SimpleNamespace(
            file_sha256="sha256:synthetic", catalog_snapshot="synthetic"))


def test_valid_native_receipt_records_evidence(tmp_path, isolated_home):
    routing = _native_routing(tmp_path)
    receipt = review_mod.record_receipt(
        routing, "free-review-1", report_text="## Report\n\nNo findings.\n",
        reviewed_commit="abc123",
        started_at="2026-10-26T00:00:00+00:00",
        finished_at="2026-10-26T00:01:00+00:00")
    assert receipt.route_type == "opencode_native_free"
    assert set(evidence_mod.load_serve_evidence()) == {"big-pickle"}


def test_external_receipt_records_nothing(tmp_path, isolated_home):
    routing = _native_routing(tmp_path)
    target = review_mod.ReviewTarget(
        deployment_id="openrouter/acme/gamma:free", family_id="test-family",
        canonical_model_id="openrouter/acme/gamma:free", provider="openrouter",
        quality=0.9, route_type="external_free",
        provider_id="openrouter", model_id="acme/gamma:free",
        opencode_model="openrouter/acme/gamma:free")
    routing.candidates = [target]
    routing.states = {"free-review-1": review_mod.LabelState(
        requested=target, effective=target)}
    review_mod.record_receipt(
        routing, "free-review-1", report_text="## Report\n\nNo findings.\n",
        reviewed_commit="abc123",
        started_at="2026-10-26T00:00:00+00:00",
        finished_at="2026-10-26T00:01:00+00:00")
    assert evidence_mod.load_serve_evidence() == {}


def _research_session(tmp_path, provider="opencode", deployment="opencode/big-pickle"):
    return research_mod.ResearchSession(
        research_id="r-evidence",
        candidates=[Candidate(
            deployment_id=deployment, family_id="test-family",
            canonical_model_id=deployment, provider=provider)],
        checkpoints=tmp_path / "ckpt")


def test_finished_managed_turn_records_evidence(tmp_path, isolated_home):
    session = _research_session(tmp_path)
    try:
        outcome = session.run_turn(
            "synthetic prompt",
            lambda req: research_mod.ProviderTurn(text="ok", complete=True))
    finally:
        session.close()
    assert outcome.model_effective == "opencode/big-pickle"
    assert set(evidence_mod.load_serve_evidence()) == {"big-pickle"}


def test_static_managed_turn_records_evidence(tmp_path, isolated_home):
    session = research_mod.ResearchSession(
        research_id="r-static", checkpoints=tmp_path / "ckpt",
        static_model="opencode/big-pickle")
    try:
        outcome = session.run_turn(
            "synthetic prompt",
            lambda req: research_mod.ProviderTurn(text="ok", complete=True))
    finally:
        session.close()
    assert outcome.model_effective == "opencode/big-pickle"
    assert set(evidence_mod.load_serve_evidence()) == {"big-pickle"}


def test_blocked_managed_turn_records_nothing(tmp_path, isolated_home):
    session = _research_session(tmp_path)
    try:
        with pytest.raises(research_mod.SessionBlocked):
            session.run_turn(
                "synthetic prompt",
                lambda req: research_mod.ProviderTurn(
                    text="", complete=False, error_text="429 quota spent",
                    failure_stage="before_response"))
    finally:
        session.close()
    assert evidence_mod.load_serve_evidence() == {}
