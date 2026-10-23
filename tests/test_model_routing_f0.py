"""F0 skeleton: paths, status contract and CLI (TASK-2026-00285). Offline only."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import model_routing
from model_routing import cli as router_cli
from model_routing import models as router_models
from model_routing import paths as router_paths


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path


def test_package_importable():
    assert model_routing.__name__ == "model_routing"


def test_xdg_defaults_to_home(isolated_home):
    assert router_paths.config_dir() == isolated_home / ".config" / "free-router"
    assert router_paths.state_dir() == isolated_home / ".local" / "state" / "free-router"


def test_xdg_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "st"))
    assert router_paths.config_dir() == tmp_path / "cfg" / "free-router"
    assert router_paths.state_dir() == tmp_path / "st" / "free-router"
    assert router_paths.policy_path().name == "policy.yaml"
    assert router_paths.providers_path().name == "providers.yaml"
    assert router_paths.catalog_path().name == "catalog.json"
    assert router_paths.rankings_path().name == "rankings.json"
    assert router_paths.history_dir().name == "history"
    assert router_paths.availability_db_path().name == "availability.sqlite"


def test_xdg_relative_falls_back_to_home(monkeypatch, isolated_home):
    monkeypatch.setenv("XDG_CONFIG_HOME", "rel/cfg")
    monkeypatch.setenv("XDG_STATE_HOME", "rel/state")
    assert router_paths.config_dir() == isolated_home / ".config" / "free-router"
    assert router_paths.state_dir() == isolated_home / ".local" / "state" / "free-router"


def test_paths_create_nothing(isolated_home):
    router_paths.policy_path()
    router_paths.providers_path()
    router_paths.catalog_path()
    router_paths.rankings_path()
    router_paths.history_dir()
    router_paths.availability_db_path()
    router_models.collect_status()
    assert list(isolated_home.iterdir()) == []


def test_status_report_round_trip():
    report = router_models.StatusReport(
        config_dir="/cfg", state_dir="/st", config_present=True,
        state_present=False, catalog_present=False, rankings_present=False,
    )
    assert report.schema_version == router_models.STATUS_SCHEMA_VERSION
    assert report.phase == router_models.MODULE_PHASE
    assert router_models.StatusReport.model_validate(report.model_dump()) == report


def test_status_report_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        router_models.StatusReport(
            config_dir="/cfg", state_dir="/st", config_present=True,
            state_present=False, catalog_present=False, rankings_present=False,
            injected=True,
        )


def test_status_report_rejects_bad_types():
    with pytest.raises(ValidationError):
        router_models.StatusReport(
            schema_version="not-a-number", config_dir="/cfg", state_dir="/st",
            config_present=True, state_present=False, catalog_present=False,
            rankings_present=False,
        )


def test_collect_status_reflects_filesystem(isolated_home):
    assert router_models.collect_status().config_present is False
    router_paths.config_dir().mkdir(parents=True)
    router_paths.catalog_path().write_text("{}")
    report = router_models.collect_status()
    assert report.config_present is True
    assert report.catalog_present is True
    assert report.rankings_present is False
    assert report.snapshot is None


def test_cli_status_json(isolated_home, capsys):
    assert router_cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert router_models.StatusReport.model_validate(payload).phase == "F0"


def test_cli_status_human(isolated_home, capsys):
    assert router_cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "free-router status (phase F0" in out
    assert str(isolated_home) in out


def test_cli_requires_subcommand(capsys):
    with pytest.raises(SystemExit) as exc:
        router_cli.main([])
    assert exc.value.code == 2
