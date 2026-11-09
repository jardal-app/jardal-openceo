"""Worker paths and spawn diagnostics with process doubles; no network or models."""
from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from model_routing.availability import AvailabilityStore
from model_routing.errors import sanitize_excerpt
from research.runtimes.isolation import BubblewrapBackend
from research.runtimes.opencode import OpenCodeRuntime
from settings import Settings
from test_research_free_profile_648 import setup as free_setup
from test_research_opencode import (
    BROKER_URL, _FakeBackend, _FakeProcess, _RecordingSpawner, _request, _settings, _touch_cli,
)


@pytest.mark.parametrize("name", [
    "RESEARCH_STATE_DIR", "RESEARCH_EVIDENCE_DIR", "RESEARCH_WORKER_DIR",
    "RESEARCH_WORKSPACE_DIR", "RESEARCH_SOCIAL_SESSION_DIR",
])
def test_settings_pin_relative_research_directories_at_load(tmp_path, monkeypatch, name):
    startup = tmp_path / "core"
    startup.mkdir()
    monkeypatch.chdir(startup)
    relative = "../.runtime/research/data"
    monkeypatch.setenv(name, relative)
    settings = Settings(_env_file=None)

    monkeypatch.chdir(tmp_path)
    assert getattr(settings, name) == str((startup / relative).resolve())


def test_settings_preserve_absolute_directory_and_disabled_social_sessions(tmp_path):
    settings = Settings(_env_file=None, RESEARCH_STATE_DIR=str(tmp_path), RESEARCH_SOCIAL_SESSION_DIR="")
    assert settings.RESEARCH_STATE_DIR == str(tmp_path)
    assert settings.RESEARCH_SOCIAL_SESSION_DIR == ""


@pytest.mark.parametrize("override", [False, True])
def test_relative_state_and_workspace_produce_existing_absolute_bwrap_sources(tmp_path, monkeypatch, override):
    startup = tmp_path / "core"
    startup.mkdir()
    monkeypatch.chdir(startup)
    relative_state = Path("../.runtime/research")
    state = (startup / relative_state).resolve()
    cli = _touch_cli(tmp_path)
    settings = SimpleNamespace(RESEARCH_OPENCODE_CLI_PATH=str(cli), RESEARCH_STATE_DIR=str(relative_state))
    process = _FakeProcess(stdout=b'{"type":"text","part":{"text":"checked"}}')
    spawn = _RecordingSpawner(process)
    runtime = OpenCodeRuntime(
        settings=settings, isolation_backend=BubblewrapBackend(), spawn=spawn,
        state_dir=relative_state if override else None, broker_url=BROKER_URL, base_env={},
    )
    launch = tmp_path / "launch" / "nested"
    launch.mkdir(parents=True)
    monkeypatch.chdir(launch)
    workspace = Path("../../workspace")
    result = asyncio.run(runtime.run(_request(tmp_path, workspace=workspace)))

    assert result.text == "checked"
    call = spawn.calls[0]
    argv = call["argv"][:call["argv"].index("--")]
    sources = [Path(argv[i + 1]) for i, flag in enumerate(argv) if flag in {"--bind", "--ro-bind"}]
    assert sources and all(source.is_absolute() and source.exists() for source in sources)
    home = state / "worker" / "res_abc" / "home"
    assert home in sources and home / "resolv.conf" in sources
    assert call["cwd"] == str(workspace.resolve())
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        assert Path(call["env"][name]).is_absolute()


@pytest.mark.parametrize("stderr", [
    "",
    "bwrap: Can't find source path ../.runtime/research/worker/res/home/resolv.conf\n"
    "sk-syntheticSecret123 Bearer syntheticBearer123 status=429\n" + "detail " * 100 + "TAIL_SENTINEL",
], ids=["empty", "bwrap-with-secrets"])
def test_failed_spawn_records_sanitized_stderr_without_changing_recovery(free_setup, tmp_path, stderr):
    _touch_cli(tmp_path)

    async def spawn(argv, *, cwd, env):
        return _FakeProcess(returncode=1, stderr=stderr.encode())

    runtime = OpenCodeRuntime(settings=_settings(tmp_path), isolation_backend=_FakeBackend(),
                              spawn=spawn, broker_url=BROKER_URL, base_env={})
    service, _, scripted, _ = free_setup([runtime.run, runtime.run])
    result = asyncio.run(service.run_once())

    assert result["status"] == "blocked"
    assert [request.model for request in scripted.requests] == ["opencode/a", "opencode/a"]
    with closing(AvailabilityStore()) as store:
        errors = store.errors_since(0)
        assert len(errors) == 1
        error = errors[0]
        assert error.error_class == "unclassified" and error.action == "record_only"
        assert "OpenCode termino con codigo 1" in error.excerpt
        assert len(error.excerpt) <= 300
        assert store.cooldown_for("opencode/a") is None
        assert store.provider_blocked("opencode") is None
        if stderr:
            assert "bwrap: Can't find source path" in error.excerpt
            assert "resolv.conf" in error.excerpt
            assert sanitize_excerpt(stderr)[:150] in error.excerpt
            assert "sk-syntheticSecret123" not in error.excerpt
            assert "syntheticBearer123" not in error.excerpt
            assert "TAIL_SENTINEL" not in error.excerpt
