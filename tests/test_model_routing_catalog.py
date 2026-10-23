"""Catalog contracts, secrets, publication and live harness (TASK-2026-00287). Offline only."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
from pathlib import Path

import pytest
from pydantic import ValidationError

from model_routing import cli as router_cli
from model_routing import config as router_config
from model_routing import live as router_live
from model_routing import models as router_models
from model_routing import publish as router_publish
from model_routing import secrets as router_secrets

SECRET = "synthetic-secret-value-001"


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    for name in (
        "FREE_ROUTER_OPENROUTER_API_KEY",
        "FREE_ROUTER_NVIDIA_NIM_API_KEY",
        "FREE_ROUTER_GROQ_API_KEY",
        "FREE_ROUTER_GEMINI_API_KEY",
        "FREE_ROUTER_AA_API_KEY",
        "FREE_ROUTER_HF_TOKEN",
        "FREE_ROUTER_GATEWAY_KEY",
        "FREE_ROUTER_LITELLM_MASTER_KEY",
        "GROQ_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _deployment(**overrides):
    base = {
        "deployment_id": "openrouter/example/model:free",
        "provider": "openrouter",
        "free_verified": True,
        "input_price_usd": 0,
        "output_price_usd": 0,
    }
    base.update(overrides)
    return router_models.Deployment(**base)


def _catalog(*deployments):
    entry = router_models.CatalogEntry(
        family_id="example-family",
        canonical_model_id="example/model-1",
        deployments=list(deployments),
    )
    return router_models.CatalogDocument(
        generator="test", entries=[entry]
    )


def test_catalog_entry_round_trip():
    doc = _catalog(_deployment())
    assert router_models.CatalogDocument.model_validate(doc.model_dump()) == doc


def test_catalog_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        router_models.Deployment(
            deployment_id="x", provider="y", injected=True
        )


def test_catalog_rejects_bad_types():
    with pytest.raises(ValidationError):
        router_models.Deployment(
            deployment_id="x", provider="y", free_verified="sure"
        )


def test_free_verified_never_defaults_true():
    assert router_models.Deployment(deployment_id="x", provider="y").free_verified is False



def test_usability_matrix():
    policy = router_models.RouterPolicy()
    assert _usability(_deployment()) == "usable"
    assert _usability(_deployment(free_verified=False)) == "unverified"
    assert _usability(_deployment(input_price_usd=None)) == "unknown_price"
    assert _usability(_deployment(output_price_usd=0.5)) == "paid"
    assert _usability(_deployment(requires_payment_method=True)) == "paywalled"
    assert _usability(_deployment(stale=True)) == "usable"
    open_policy = router_models.RouterPolicy(allow_unknown_price=True)
    assert router_models.usability(_deployment(input_price_usd=None), open_policy) == "usable"
    assert router_models.is_usable(_deployment(), policy) is True
    assert router_models.is_usable(_deployment(input_price_usd=None), policy) is False


def _usability(deployment):
    return router_models.usability(deployment, router_models.RouterPolicy())


def test_policy_and_providers_round_trip(tmp_path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("policy_version: 1\nallow_unknown_price: true\n")
    policy = router_config.load_policy(policy_file)
    assert policy.strict_zero_cost is True
    assert policy.allow_unknown_price is True
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text("providers:\n  - {name: openrouter}\n")
    providers = router_config.load_providers(providers_file)
    assert [entry.name for entry in providers.providers] == ["openrouter"]


def test_policy_rejects_bad_schema(tmp_path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("policy_version: two\n")
    with pytest.raises(router_config.ConfigError):
        router_config.load_policy(policy_file)


def test_config_rejects_empty_and_broken_yaml(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with pytest.raises(router_config.ConfigError):
        router_config.load_policy(empty)
    broken = tmp_path / "broken.yaml"
    broken.write_text("providers: [unclosed\n")
    with pytest.raises(router_config.ConfigError):
        router_config.load_providers(broken)


def test_providers_rejects_secret_keys_without_values(tmp_path):
    providers_file = tmp_path / "providers.yaml"
    providers_file.write_text(f"providers:\n  - {{name: x, api_key: {SECRET}}}\n")
    with pytest.raises(router_config.ConfigError) as exc:
        router_config.load_providers(providers_file)
    assert "api_key" in str(exc.value)
    assert SECRET not in str(exc.value)


def _secrets_file(tmp_path, body, mode=0o600):
    path = tmp_path / ".env.free-router.local"
    path.write_text(body)
    os.chmod(path, mode)
    return path


def test_secrets_load_and_perms(tmp_path, isolated_home):
    loaded = router_secrets.load_router_secrets(
        _secrets_file(tmp_path, f"FREE_ROUTER_GROQ_API_KEY={SECRET}\n")
    )
    assert loaded.FREE_ROUTER_GROQ_API_KEY == SECRET
    assert loaded.FREE_ROUTER_GEMINI_API_KEY == ""
    assert loaded.present()["FREE_ROUTER_GROQ_API_KEY"] is True
    assert SECRET not in repr(loaded)
    assert SECRET not in str(loaded)


def test_secrets_reject_wide_perms_and_missing(tmp_path, isolated_home):
    with pytest.raises(router_secrets.SecretsError):
        router_secrets.load_router_secrets(_secrets_file(tmp_path, "A=B\n", mode=0o644))
    with pytest.raises(router_secrets.SecretsError):
        router_secrets.load_router_secrets(tmp_path / "nope.env")


def test_secrets_ignore_unprefixed_names(tmp_path, isolated_home, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "voice-key-in-env")
    loaded = router_secrets.load_router_secrets(
        _secrets_file(tmp_path, "GROQ_API_KEY=voice-key-in-file\n")
    )
    assert loaded.FREE_ROUTER_GROQ_API_KEY == ""


def test_publish_writes_catalog_and_snapshot(tmp_path):
    record = router_publish.publish_catalog(_catalog(_deployment()), tmp_path)
    catalog_file = tmp_path / "catalog.json"
    assert catalog_file.is_file()
    assert router_publish.load_catalog_doc(catalog_file).entries[0].deployments[0].free_verified is True
    snapshot = tmp_path / "history" / f"{record.snapshot_id}.json"
    assert snapshot.is_file()
    assert hashlib.sha256(catalog_file.read_bytes()).hexdigest() == record.sha256
    assert record.price_changes == ()


def test_publish_demotes_changed_prices(tmp_path):
    router_publish.publish_catalog(_catalog(_deployment()), tmp_path)
    changed = _deployment(output_price_usd=0.5)
    record = router_publish.publish_catalog(_catalog(changed), tmp_path)
    assert record.price_changes == ("openrouter/example/model:free",)
    reread = router_publish.load_catalog_doc(tmp_path / "catalog.json")
    assert reread.entries[0].deployments[0].free_verified is False


def test_publish_failure_keeps_previous_catalog(tmp_path, monkeypatch):
    router_publish.publish_catalog(_catalog(_deployment()), tmp_path)
    before = (tmp_path / "catalog.json").read_bytes()

    real = router_publish.atomic_write_json

    def flaky(path, payload):
        if path.name == "catalog.json":
            raise OSError("injected mid-publish failure")
        return real(path, payload)

    monkeypatch.setattr(router_publish, "atomic_write_json", flaky)
    with pytest.raises(router_publish.PublishError):
        router_publish.publish_catalog(_catalog(_deployment()), tmp_path)
    assert (tmp_path / "catalog.json").read_bytes() == before
    assert list(tmp_path.glob(".catalog.json.*.tmp")) == []


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "ledger.json"
    target.write_text("{}")
    monkeypatch.setattr(os, "replace", _raise_oserror)
    with pytest.raises(OSError):
        router_publish.atomic_write_json(target, '{"a": 1}')
    assert target.read_text() == "{}"
    assert list(tmp_path.glob(".ledger.json.*.tmp")) == []


def _raise_oserror(_src, _dst):
    raise OSError("injected rename failure")


def test_mark_stale_keeps_serving_last_good(tmp_path):
    router_publish.publish_catalog(_catalog(_deployment()), tmp_path)
    record = router_publish.mark_catalog_stale(tmp_path, "source unreachable")
    assert record.stale is True
    reread = router_publish.load_catalog_doc(tmp_path / "catalog.json")
    assert reread.stale_reason == "source unreachable"
    assert len(reread.entries[0].deployments) == 1
    assert router_models.is_usable(reread.entries[0].deployments[0], router_models.RouterPolicy())


def test_load_catalog_rejects_broken(tmp_path):
    broken = tmp_path / "catalog.json"
    broken.write_text("{nope")
    with pytest.raises(router_publish.PublishError):
        router_publish.load_catalog_doc(broken)
    with pytest.raises(router_publish.PublishError):
        router_publish.load_catalog_doc(tmp_path / "missing.json")


def test_assert_no_auth():
    router_live.assert_no_auth({})
    router_live.assert_no_auth({"Accept": "application/json"})
    with pytest.raises(router_live.LiveGuardError) as exc:
        router_live.assert_no_auth({"Authorization": SECRET})
    assert "authorization" in str(exc.value).lower()
    assert SECRET not in str(exc.value)


def test_assert_free_verified():
    doc = _catalog(_deployment())
    router_live.assert_free_verified("openrouter/example/model:free", doc)
    with pytest.raises(router_live.LiveGuardError):
        router_live.assert_free_verified(
            "openrouter/example/model:free", _catalog(_deployment(free_verified=False))
        )
    with pytest.raises(router_live.LiveGuardError):
        router_live.assert_free_verified("missing/deployment", doc)


def test_redact_and_scan_findings_hide_values():
    text = f"key={SECRET} and sk-abcdef123456 tail"
    redacted = router_live.redact_text(text, [SECRET])
    assert SECRET not in redacted
    assert "sk-abcdef123456" not in redacted
    findings = router_live.scan_for_secrets(text, [SECRET])
    assert len(findings) == 2
    assert all(SECRET not in finding for finding in findings)
    assert all("sk-abcdef123456" not in finding for finding in findings)


def test_record_and_replay_round_trip(tmp_path):
    fixtures = tmp_path / "fixtures"
    path = router_live.record_fixture("sample.json", f'{{"k": "{SECRET}"}}', fixtures, [SECRET])
    assert SECRET not in path.read_text()
    assert router_live.load_fixture("sample.json", fixtures) == '{"k": "[REDACTED]"}'
    with pytest.raises(router_live.LiveError):
        router_live.record_fixture("../escape.json", "{}", fixtures)
    with pytest.raises(router_live.LiveError):
        router_live.record_fixture("notes.txt", "{}", fixtures)


def test_committed_fixtures_hold_no_secrets():
    fixtures = Path(__file__).resolve().parent / "fixtures" / "model_routing_live"
    assert fixtures.is_dir(), "live fixtures directory is missing"
    seen = 0
    for fixture in sorted(fixtures.glob("*.json")):
        text = fixture.read_text(encoding="utf-8")
        json.loads(text)
        findings = router_live.scan_for_secrets(text, [])
        assert findings == [], f"{fixture.name}: {findings}"
        seen += 1
    assert seen > 0, "no live fixtures found"


def test_http_errors_hide_credentials(monkeypatch):
    def boom_429(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "slow", {"Retry-After": "60"}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom_429)
    with pytest.raises(router_live.LiveRateLimited) as exc:
        router_live.http_get_json(f"https://example.test/v1?key={SECRET}")
    assert SECRET not in str(exc.value)
    assert "key=" not in str(exc.value)

    def boom_500(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "bad", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom_500)
    with pytest.raises(router_live.LiveError) as exc:
        router_live.http_get_json("https://example.test/v1/models")
    assert "HTTP 500" in str(exc.value)


def test_http_sets_identifying_user_agent(monkeypatch):
    seen = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout=None):
        seen.update(request.header_items())
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert router_live.http_get_json("https://example.test/v1/models") == {"ok": True}
    assert seen["User-agent"] == "free-router-live/1"


def test_openrouter_key_parsing_and_guard():
    info = router_live.parse_openrouter_key_info({
        "data": {"usage": 1.5, "free_model_daily_requests": {"used": 3, "limit": 1000, "remaining": 997}}
    })
    assert (info.usage, info.free_limit, info.free_used) == (1.5, 1000, 3)
    bare = router_live.parse_openrouter_key_info({"data": {"usage": 0}})
    assert bare.free_limit is None
    with pytest.raises(router_live.LiveError):
        router_live.parse_openrouter_key_info({"nope": True})
    router_live.assert_usage_unchanged(1.5, 1.5)
    with pytest.raises(router_live.LiveGuardError):
        router_live.assert_usage_unchanged(1.5, 1.6)


def test_cli_validate_config_missing(isolated_home, capsys):
    assert router_cli.main(["validate-config"]) == 1
    out = capsys.readouterr().out
    assert "policy: FAIL" in out
    assert "providers: FAIL" in out


def test_cli_validate_config_valid(isolated_home, capsys, monkeypatch):
    from model_routing.paths import config_dir

    config_dir().mkdir(parents=True)
    (config_dir() / "policy.yaml").write_text("policy_version: 1\n")
    (config_dir() / "providers.yaml").write_text("providers:\n  - {name: openrouter}\n")

    class _Secrets:
        def present(self):
            return {"FREE_ROUTER_GROQ_API_KEY": True}

    monkeypatch.setattr(router_config, "load_router_secrets", lambda: _Secrets())
    assert router_cli.main(["validate-config"]) == 0
    assert "policy: ok" in capsys.readouterr().out


def test_status_reports_policy_and_providers(isolated_home):
    from model_routing.paths import config_dir

    assert router_models.collect_status().policy_present is False
    config_dir().mkdir(parents=True)
    (config_dir() / "policy.yaml").write_text("policy_version: 1\n")
    (config_dir() / "providers.yaml").write_text("providers: []\n")
    report = router_models.collect_status()
    assert report.policy_present is True
    assert report.providers_present is True


def test_publish_demotes_lost_free_offered(tmp_path):
    router_publish.publish_catalog(_catalog(_deployment(free_offered=True)), tmp_path)
    lost = _deployment(free_offered=False)
    record = router_publish.publish_catalog(_catalog(lost), tmp_path)
    assert record.price_changes == ("openrouter/example/model:free",)
    reread = router_publish.load_catalog_doc(tmp_path / "catalog.json")
    assert reread.entries[0].deployments[0].free_verified is False


def test_catalog_rejects_duplicate_ids():
    with pytest.raises(ValidationError):
        _catalog(_deployment(), _deployment())
    first = router_models.CatalogEntry(
        family_id="fam", canonical_model_id="canon", deployments=[_deployment()]
    )
    second = router_models.CatalogEntry(
        family_id="fam",
        canonical_model_id="canon",
        deployments=[_deployment(deployment_id="other/dep")],
    )
    with pytest.raises(ValidationError):
        router_models.CatalogDocument(generator="test", entries=[first, second])


def test_key_patterns_ignore_infix_substrings():
    assert router_live.scan_for_secrets("risk-assessment task-management", []) == []
    assert router_live.redact_text("desk-configuration notes", []) == "desk-configuration notes"
    token = "sk-" + "x" * 12
    assert router_live.scan_for_secrets(f"token {token}", []) != []


def test_config_rejects_non_utf8(tmp_path):
    broken = tmp_path / "policy.yaml"
    broken.write_bytes(b"policy_version: 1\nlabel: \xff\xfe\n")
    with pytest.raises(router_config.ConfigError):
        router_config.load_policy(broken)


def test_safe_url_strips_userinfo_and_query():
    safe = router_live._safe_url("https://user:pass@example.test/v1/models?key=abc#frag")
    assert safe == "https://example.test/v1/models"
    assert "user" not in safe
    assert "key=" not in safe
