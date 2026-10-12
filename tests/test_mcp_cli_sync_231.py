from __future__ import annotations

import importlib.util
import json
import os
import tomllib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync-mcp-config.py"
SPEC = importlib.util.spec_from_file_location("mcp_cli_sync", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
SERVER = {"type": "http", "url": "http://127.0.0.1:18765/internal/mcp/"}


@pytest.mark.parametrize("profile", ["claude", "muse", "opencode", "qwen", "gemini", "agy"])
def test_json_migration_removes_credentials_and_preserves_other_config(tmp_path, profile):
    path = tmp_path / "settings.json"
    project = tmp_path / "project"
    key = "mcp" if profile == "opencode" else "mcpServers"
    other = {"command": "synthetic-server", "env": {"OTHER_TOKEN": "synthetic"}}
    old = {"url": "/internal/mcp-global", "headers": {"Authorization": "synthetic"}}
    config = {
        "model": "existing-model",
        key: {"other": other, "openceo-global": old, "openceo": old, "openceo-internal": old},
        "projects": {
            str(project): {"mcpServers": {"openceo-global": old, "openceo-internal": old, "other": other}},
        },
    }
    path.write_text(json.dumps(config))
    os.chmod(path, 0o600)

    assert sync._sync_json(path, profile, SERVER, project)
    updated = json.loads(path.read_text())
    servers = updated[key]
    assert set(servers) == {"other", "openceo-internal"}
    assert servers["other"] == other
    assert updated["model"] == config["model"]
    entry = servers["openceo-internal"]
    assert SERVER["url"] in entry.values()
    assert not any("header" in key.lower() or "token" in key.lower() for key in entry)
    if profile == "opencode":
        assert entry["oauth"] is False
    if profile == "claude":
        assert updated["projects"][str(project)]["mcpServers"] == {"other": other}
    else:
        assert updated["projects"] == config["projects"]
    assert path.stat().st_mode & 0o777 == 0o600
    contents = path.read_text()
    assert not sync._sync_json(path, profile, SERVER, project)
    assert path.read_text() == contents


def test_codex_migration_removes_nested_auth_and_preserves_unrelated_toml(tmp_path):
    path = tmp_path / "config.toml"
    original = '''model = "existing-model"

[mcp_servers."openceo-global"]
url = "http://127.0.0.1:18765/internal/mcp-global"
bearer_token_env_var = "SYNTHETIC_TOKEN"

[mcp_servers."openceo-global".http_headers]
Authorization = "synthetic"

[mcp_servers.openceo-internal]
url = "old"
enabled_tools = ["only-one"]

[mcp_servers.other]
command = "synthetic-server"

[projects."/synthetic"]
trust_level = "trusted"
'''
    path.write_text(original)
    os.chmod(path, 0o600)
    assert sync._sync_toml(path, SERVER)
    updated = tomllib.loads(path.read_text())
    prior = tomllib.loads(original)
    assert updated["model"] == prior["model"]
    assert updated["projects"] == prior["projects"]
    assert updated["mcp_servers"]["other"] == prior["mcp_servers"]["other"]
    assert set(updated["mcp_servers"]) == {"other", "openceo-internal"}
    assert updated["mcp_servers"]["openceo-internal"] == {
        "url": SERVER["url"],
        "startup_timeout_sec": 30.0,
        "tool_timeout_sec": 60.0,
        "default_tools_approval_mode": "approve",
    }
    assert path.stat().st_mode & 0o777 == 0o600
    contents = path.read_text()
    assert not sync._sync_toml(path, SERVER)
    assert path.read_text() == contents


def test_codex_unsupported_inline_mcp_is_left_untouched(tmp_path):
    path = tmp_path / "config.toml"
    contents = 'mcp_servers = { openceo-global = { url = "old" } }\n'
    path.write_text(contents)
    with pytest.raises(ValueError, match="Unsupported MCP TOML layout"):
        sync._sync_toml(path, SERVER)
    assert path.read_text() == contents
