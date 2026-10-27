"""F6 acceptance: T01–T20 + addendum A01–A12 battery, §16 metrics, findings, ops CLI (TASK-2026-00297, TASK-2026-00309).

Offline only: simulated providers, injected faults and the recorded
live fixtures already in the repo. No network leaves loopback and no
model is called; the usage-unchanged check lives in
test_model_routing_acceptance_live.py.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "openceo-core"))

from model_routing import acceptance as router_acceptance  # noqa: E402
from model_routing import alerts as router_alerts  # noqa: E402
from model_routing import cli as router_cli  # noqa: E402
from model_routing import error_findings as router_findings  # noqa: E402
from model_routing import external as router_external  # noqa: E402
from model_routing import refresh as router_refresh  # noqa: E402
from model_routing import research_wiring as research_mod  # noqa: E402
from model_routing.availability import AvailabilityStore  # noqa: E402
from model_routing.errors import classify_http, classify_text  # noqa: E402
from model_routing.models import CatalogDocument  # noqa: E402
from model_routing.publish import publish_catalog  # noqa: E402
from orchestration.inbox import InMemoryInboxStore  # noqa: E402


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


def _store(tmp_path: Path) -> AvailabilityStore:
    return AvailabilityStore(tmp_path / "availability.sqlite")


# Battery ---------------------------------------------------------------------


def test_battery_is_green_and_complete(tmp_path):
    report = router_acceptance.run_battery(tmp_path / "battery")
    assert [item.id for item in report.scenarios] == [
        *[f"T{i:02d}" for i in range(1, 21)],
        *[f"A{i:02d}" for i in range(1, 13)],
    ]
    failed = [item for item in report.scenarios if not item.passed]
    assert failed == []
    assert report.passed
    assert len(report.replay_notes) == 9


def test_addendum_scenarios_cover_section_11(tmp_path):
    report = router_acceptance.run_battery(tmp_path / "battery")
    addendum = [item for item in report.scenarios
                if item.id.startswith("A")]
    assert [item.id for item in addendum] == [f"A{i:02d}" for i in range(1, 13)]
    assert all(item.passed and item.evidence for item in addendum)
    text = router_acceptance.render_report(report)
    assert "## Addendum §11 scenarios" in text


def test_a01_gateway_check_rejects_native_routes():
    plan = router_external.build_gateway_plan(
        router_acceptance.battery_catalog(),
        router_acceptance.battery_rankings())
    served = router_acceptance._gateway_served_models(plan)
    assert served
    assert not [model for model in served if "opencode/" in model]
    poisoned = router_external.GatewayPlan(
        backend=plan.backend,
        model_list=(*plan.model_list,
                    {"model_name": "evil",
                     "litellm_params": {"model": "opencode/evil-free"}}),
        review_aliases=plan.review_aliases,
        review_routes=plan.review_routes,
        skipped=plan.skipped,
        native_routes=plan.native_routes,
        eligible_sha256=plan.eligible_sha256)
    bad = [model for model in router_acceptance._gateway_served_models(poisoned)
           if "opencode/" in model]
    assert bad == ["opencode/evil-free"]
    assert "opencode/" in json.dumps(poisoned.model_list)


def test_a04_reads_quota_fixture_from_passed_dir(tmp_path):
    fixtures = tmp_path / "fixtures"
    shutil.copytree(
        Path(__file__).resolve().parent / "fixtures" / "model_routing_live",
        fixtures)
    report = router_acceptance.run_battery(tmp_path / "ok", fixtures_dir=fixtures)
    assert report.passed
    payload_path = fixtures / "opencode-free-quota-429.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["event"]["error"]["response"]["body"] = "synthetic 200 ok"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    broken = router_acceptance.run_battery(tmp_path / "broken", fixtures_dir=fixtures)
    failed = {item.id for item in broken.scenarios if not item.passed}
    assert "A04" in failed


def test_battery_metrics_meet_section_16(tmp_path):
    report = router_acceptance.run_battery(tmp_path / "battery")
    metrics = report.metrics
    assert metrics.paid_invocations == 0
    assert metrics.receipts_without_effective == 0
    assert metrics.family_collisions == 0
    assert (metrics.snapshots_valid, metrics.snapshots_published) == (2, 2)
    assert (metrics.quota_classified, metrics.quota_errors) == (4, 4)
    assert (metrics.pre_response_recovered, metrics.pre_response_attempts) == (7, 7)
    assert metrics.pre_response_recovery_rate >= 0.95
    assert metrics.tool_repeats_without_check == 0
    assert metrics.failures() == []


def test_real_calls_table_sums_to_total():
    rows = router_acceptance.REAL_CALLS_BY_TICKET
    assert len(rows) == 17
    assert sum(count for _ticket, count, _note in rows) == router_acceptance.total_real_calls()
    assert router_acceptance.total_real_calls() == 116
    by_ticket = {ticket: count for ticket, count, _note in rows}
    assert by_ticket["TASK-2026-00297"] == 2
    assert by_ticket["TASK-2026-00309"] == 2


def test_committed_report_matches_regenerated_battery(tmp_path):
    text = router_acceptance.generate_report_text(tmp_path / "battery")
    committed = router_acceptance.ACCEPTANCE_REPORT_PATH.read_text(encoding="utf-8")
    assert text == committed


# Findings bridge --------------------------------------------------------------


def test_record_error_queues_exactly_one_pending_finding(tmp_path):
    store = _store(tmp_path)
    try:
        record = store.record_error(
            provider="groq", deployment_id="groq/delta-20b", model="groq/delta-20b",
            classified=classify_http(429, retry_after="30"), run_id="run-findings")
        pending = store.pending_findings()
        assert len(pending) == 1
        fingerprint, payload = pending[0]
        assert fingerprint == router_findings.finding_fingerprint(
            router_findings.appearance_for_record(record))
        assert store.finding_seen_count(fingerprint) == 1
        store.record_error(
            provider="groq", deployment_id="groq/delta-20b", model="groq/delta-20b",
            classified=classify_http(429, retry_after="30"), run_id="run-findings-2")
        assert len(store.pending_findings()) == 1
        assert store.finding_seen_count(fingerprint) == 2
    finally:
        store.close()


def test_new_error_drains_to_a_single_deduplicated_finding(tmp_path):
    store = _store(tmp_path)
    inbox = InMemoryInboxStore()
    try:
        store.record_error(
            provider="nvidia", deployment_id="nvidia/delta-20b", model="nvidia/delta-20b",
            classified=classify_text("not a valid model ID"), run_id="run-new")
        forwarded = asyncio.run(router_findings.drain_pending(store, inbox, "jardal-openceo"))
        assert len(forwarded) == 1
        assert store.pending_findings() == []
        entry = asyncio.run(inbox.get_entry(forwarded[0]))
        assert entry is not None and len(entry["seen_in"]) == 1
        store.record_error(
            provider="nvidia", deployment_id="nvidia/delta-20b", model="nvidia/delta-20b",
            classified=classify_text("not a valid model ID"), run_id="run-recurrent")
        forwarded_again = asyncio.run(
            router_findings.drain_pending(store, inbox, "jardal-openceo"))
        assert forwarded_again == forwarded
        entry = asyncio.run(inbox.get_entry(forwarded[0]))
        assert entry is not None and len(entry["seen_in"]) == 2
        assert len(inbox.entries) == 1
    finally:
        store.close()


def test_finding_preview_is_autorun_false(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_error(
            provider="openrouter", deployment_id="openrouter/m", model="m",
            classified=classify_http(402), run_id="run-billing")
        (fingerprint, _payload), = store.pending_findings()
        preview = router_findings.preview_payload(store, fingerprint)
        assert preview is not None
        assert preview["autorun"] is False
        assert preview["origin"] == "router_errors"
        assert preview["finding_fingerprint"] == fingerprint
        assert preview["coding_tool"] == "tier_s"
    finally:
        store.close()


def test_drain_skips_corrupt_payloads(tmp_path):
    store = _store(tmp_path)
    inbox = InMemoryInboxStore()
    try:
        store.queue_finding("deadbeef", "{not json")
        assert asyncio.run(router_findings.drain_pending(store, inbox, "jardal-openceo")) == []
        assert store.pending_findings() == []
    finally:
        store.close()


# Cost audit and sessions -------------------------------------------------------


def test_cost_audit_clean_over_battery_receipts(tmp_path):
    report = router_acceptance.run_battery(tmp_path / "battery")
    assert report.passed
    receipts = [tmp_path / "battery" / name / "receipts" for name in ("t06", "t08", "t18")]
    store = _store(tmp_path)
    try:
        audit = router_acceptance.cost_audit(store, 0.0, receipts)
        assert audit.receipts_checked == 3
        assert audit.receipts_missing_model == 0
        assert audit.receipts_invalid == 0
        assert audit.billing_rows == 0
        assert audit.clean
    finally:
        store.close()


def test_cost_audit_flags_billing_and_broken_receipts(tmp_path):
    store = _store(tmp_path)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    try:
        store.record_error(
            provider="openrouter", deployment_id="openrouter/m", model="m",
            classified=classify_http(402), run_id="run-audit")
        (receipts / "broken.json").write_text("{oops", encoding="utf-8")
        audit = router_acceptance.cost_audit(store, 0.0, [receipts])
        assert audit.billing_rows == 1
        assert audit.receipts_invalid == 1
        assert not audit.clean
    finally:
        store.close()


def _checkpoint_file(directory: Path, name: str, updated_at: str) -> None:
    checkpoint = research_mod.ResearchCheckpoint(
        research_id=name, session_id=f"ses-{name}", turn=3,
        objective="synthetic", effective_deployment="opencode/alpha-free",
        updated_at=updated_at)
    (directory / f"{name}.json").write_text(
        json.dumps(checkpoint.to_dict()), encoding="utf-8")


def test_active_sessions_by_freshness(tmp_path):
    checkpoints = tmp_path / "ckpt"
    checkpoints.mkdir()
    now = datetime.now(timezone.utc)
    _checkpoint_file(checkpoints, "fresh", now.isoformat())
    _checkpoint_file(checkpoints, "old", (now - timedelta(hours=49)).isoformat())
    (checkpoints / "garbage.json").write_text("{oops", encoding="utf-8")
    rows = router_acceptance.active_sessions(checkpoints, max_age_hours=24.0)
    by_id = {row.research_id: row for row in rows}
    assert by_id["fresh"].status == "active"
    assert by_id["fresh"].turn == 3
    assert by_id["old"].status == "stale"
    assert by_id["garbage"].status == "corrupt"
    assert router_acceptance.active_sessions(tmp_path / "missing") == []


# Alerts ------------------------------------------------------------------------


def test_alert_round_trip_skips_corrupt_lines(tmp_path):
    assert router_alerts.emit_alert(tmp_path, "refresh_failed", "update --providers", "boom")
    with open(tmp_path / "alerts.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{oops\n")
    assert router_alerts.emit_alert(tmp_path, "no_free_models", "update", "empty")
    alerts = router_alerts.list_alerts(tmp_path)
    assert [(item.kind, item.source) for item in alerts] == [
        ("refresh_failed", "update --providers"), ("no_free_models", "update")]
    assert len(router_alerts.list_alerts(tmp_path, kind="no_free_models")) == 1
    assert router_alerts.list_alerts(tmp_path / "missing") == []


def test_run_update_alerts_on_failure_and_empty_catalog(tmp_path):
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()
    code, _detail = router_refresh.run_update(
        "providers", config, state, update=_boom)
    assert code == 1
    failures = router_alerts.list_alerts(state, kind="refresh_failed")
    assert len(failures) == 1 and "boom" in failures[0].detail
    empty = CatalogDocument.model_validate(
        router_acceptance.battery_catalog().model_dump())
    for entry in empty.entries:
        for route in entry.deployments:
            route.free_offered = False
            route.free_verified = False
    publish_catalog(empty, config)
    ok_report = router_refresh.RefreshReport(
        kind="providers", catalog_snapshot="catalog-1", rankings_snapshot="rankings-1",
        stale=False, stale_reason=None, fetched=("openrouter",), failed=())
    code, _detail = router_refresh.run_update(
        "providers", config, state, update=lambda: ok_report)
    assert code == 0
    assert len(router_alerts.list_alerts(state, kind="no_free_models")) == 1


def test_run_update_stays_quiet_with_usable_catalog(tmp_path):
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()
    publish_catalog(router_acceptance.battery_catalog(), config)
    ok_report = router_refresh.RefreshReport(
        kind="providers", catalog_snapshot="catalog-1", rankings_snapshot="rankings-1",
        stale=False, stale_reason=None, fetched=("openrouter",), failed=())
    code, _detail = router_refresh.run_update(
        "providers", config, state, update=lambda: ok_report)
    assert code == 0
    assert router_alerts.list_alerts(state) == []


def _boom():
    raise router_refresh.RefreshError("boom")


def test_security_rollback_emits_gateway_alert(tmp_path):
    merged = router_external.merge_gateway_provider(
        {"provider": {"other": {"npm": "x"}}},
        router_external.build_opencode_config("classic"))
    alerts_dir = tmp_path / "alerts"
    router_external.apply_security_rollback(merged, tmp_path, "advisory-1", alerts_dir=alerts_dir)
    alerts = router_alerts.list_alerts(alerts_dir, kind="gateway_security")
    assert len(alerts) == 1 and "advisory-1" in alerts[0].detail


# F6 CLI -------------------------------------------------------------------------


def test_cli_cost_audit_json_and_exit_codes(tmp_path, capsys):
    state = tmp_path / "state"
    store = AvailabilityStore(state / "availability.sqlite")
    try:
        store.record_error(
            provider="groq", deployment_id="groq/a", model="a",
            classified=classify_http(429, retry_after="30"), run_id="run-cost")
    finally:
        store.close()
    assert router_cli.main(
        ["cost-audit", "--since", "24h", "--state-dir", str(state), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clean"] is True and payload["billing_rows"] == 0
    assert payload["by_class"] == {"rate_limited": 1}
    store = AvailabilityStore(state / "availability.sqlite")
    try:
        store.record_error(
            provider="openrouter", deployment_id="openrouter/m", model="m",
            classified=classify_http(402), run_id="run-cost")
    finally:
        store.close()
    assert router_cli.main(
        ["cost-audit", "--since", "24h", "--state-dir", str(state)]) == 1
    assert "VIOLATIONS" in capsys.readouterr().out


def test_cli_sessions_active_filter(tmp_path, capsys):
    checkpoints = tmp_path / "state" / "research_checkpoints"
    checkpoints.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    _checkpoint_file(checkpoints, "fresh", now.isoformat())
    _checkpoint_file(checkpoints, "old", (now - timedelta(hours=49)).isoformat())
    assert router_cli.main(
        ["sessions", "--state-dir", str(tmp_path / "state"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(item["research_id"] for item in payload) == ["fresh", "old"]
    assert router_cli.main(
        ["sessions", "--active", "--state-dir", str(tmp_path / "state"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["research_id"] for item in payload] == ["fresh"]
    assert router_cli.main(
        ["sessions", "--max-age-h", "0", "--state-dir", str(tmp_path / "state")]) == 2


def test_cli_errors_summary_and_sync_findings(isolated_home, capsys):
    handle = AvailabilityStore()
    try:
        handle.record_error(
            provider="groq", deployment_id="groq/a", model="a",
            classified=classify_http(429, retry_after="30"), run_id="run-sum")
        handle.record_error(
            provider="groq", deployment_id="groq/a", model="a",
            classified=classify_http(503), run_id="run-sum")
    finally:
        handle.close()
    assert router_cli.main(["errors", "--since", "24h", "--summary"]) == 0
    out = capsys.readouterr().out
    assert "2 errors" in out and "rate_limited: 1" in out
    assert router_cli.main(["errors", "--since", "24h", "--summary", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 2 and payload["by_provider"] == {"groq": 2}
    assert router_cli.main(["errors", "--sync-findings"]) == 0
    out = capsys.readouterr().out
    assert "autorun=false" in out
    assert router_cli.main(["errors"]) == 2


def test_active_sessions_honours_fixed_now(tmp_path):
    checkpoints = tmp_path / "ckpt"
    checkpoints.mkdir()
    _checkpoint_file(checkpoints, "fixed", "2026-10-23T00:00:00+00:00")
    soon = datetime(2026, 10, 23, 1, 0, 0, tzinfo=timezone.utc).timestamp()
    late = datetime(2026, 10, 25, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    rows = router_acceptance.active_sessions(checkpoints, max_age_hours=24.0, now=soon)
    assert {row.research_id: row.status for row in rows} == {"fixed": "active"}
    rows = router_acceptance.active_sessions(checkpoints, max_age_hours=24.0, now=late)
    assert {row.research_id: row.status for row in rows} == {"fixed": "stale"}


def test_metrics_detect_paid_missing_and_collisions():
    ledger = router_acceptance._Ledger(
        served=["opencode/alpha-free", "paid/evil"],
        receipts_effective=["opencode/alpha-free", ""],
        round_families=[("fam-alpha", "fam-alpha")],
        snapshots_published=1, snapshots_valid=1,
        quota_errors=1, quota_classified=1,
        pre_response_attempts=1, pre_response_recovered=1)
    metrics = router_acceptance._metrics(ledger, router_acceptance.battery_catalog())
    assert metrics.paid_invocations == 1
    assert metrics.receipts_without_effective == 1
    assert metrics.family_collisions == 1
    assert metrics.failures() != []


def test_cli_errors_state_dir(tmp_path, capsys):
    state = tmp_path / "custom" / "state"
    handle = AvailabilityStore(state / "availability.sqlite")
    try:
        handle.record_error(
            provider="groq", deployment_id="groq/a", model="a",
            classified=classify_http(429, retry_after="30"), run_id="run-custom")
    finally:
        handle.close()
    assert router_cli.main(
        ["errors", "--since", "24h", "--summary", "--state-dir", str(state)]) == 0
    assert "1 errors" in capsys.readouterr().out
    assert router_cli.main(["errors", "--sync-findings", "--state-dir", str(state)]) == 0
    assert "autorun=false" in capsys.readouterr().out


def test_no_free_models_alert_ignores_unknown_price_policy(tmp_path):
    # TASK-2026-00319: unknown prices order, never exclude, so the
    # no_free_models alert never fires for them whatever the policy says.
    config = tmp_path / "config"
    state = tmp_path / "state"
    config.mkdir()
    catalog = router_acceptance.battery_catalog()
    for entry in catalog.entries:
        for route in entry.deployments:
            route.input_price_usd = None
            route.output_price_usd = None
    publish_catalog(catalog, config)
    (config / "policy.yaml").write_text("allow_unknown_price: true\n", encoding="utf-8")
    ok_report = router_refresh.RefreshReport(
        kind="providers", catalog_snapshot="catalog-1", rankings_snapshot="rankings-1",
        stale=False, stale_reason=None, fetched=("openrouter",), failed=())
    code, _detail = router_refresh.run_update(
        "providers", config, state, update=lambda: ok_report)
    assert code == 0
    assert router_alerts.list_alerts(state, kind="no_free_models") == []
    (config / "policy.yaml").write_text("allow_unknown_price: false\n", encoding="utf-8")
    code, _detail = router_refresh.run_update(
        "providers", config, state, update=lambda: ok_report)
    assert code == 0
    assert router_alerts.list_alerts(state, kind="no_free_models") == []


def test_drain_logs_corrupt_payloads(tmp_path, caplog):
    store = _store(tmp_path)
    inbox = InMemoryInboxStore()
    try:
        store.queue_finding("deadbeef", "{not json")
        with caplog.at_level("WARNING", logger="model_routing.error_findings"):
            forwarded = asyncio.run(
                router_findings.drain_pending(store, inbox, "jardal-openceo"))
        assert forwarded == []
        assert store.pending_findings() == []
        assert "corrupt" in caplog.text.lower()
    finally:
        store.close()
