#!/usr/bin/env python3
"""Sync the anonymous internal MCP into installed coding CLI profiles."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import tomllib
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from settings import get_settings
from mcp_server import build_mcp_provider_config

_SERVER_NAME = "openceo-internal"
_LEGACY_NAMES = ("openceo", "openceo-global")
_JSON_PROFILES = (
    (".claude.json", "claude"),
    (".claude-j/.claude.json", "claude"),
    (".claude-deepseek/.claude.json", "claude"),
    (".config/muse/settings.json", "muse"),
    (".config/opencode/opencode.json", "opencode"),
    (".qwen/settings.json", "qwen"),
    (".gemini/settings.json", "gemini"),
    (".gemini/config/mcp_config.json", "agy"),
)
_TOML_PROFILES = (".codex/config.toml", ".codex-j/config.toml")
_SERVER_TABLE = re.compile(
    r"^\[mcp_servers\.(?:[\"'])?(?:openceo-internal|openceo-global|openceo)"
    r"(?:[\"'])?(?:\.|\])"
)


def _write_config(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        os.chmod(temporary, mode)
        file.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_servers(servers: dict, server: dict) -> None:
    for name in _LEGACY_NAMES:
        servers.pop(name, None)
    servers[_SERVER_NAME] = server


def _sync_json(path: Path, profile: str, server: dict, project_dir: Path) -> bool:
    original = path.read_text()
    config = json.loads(original)
    key = "mcp" if profile == "opencode" else "mcpServers"
    url = server["url"]
    if profile == "opencode":
        entry = {"type": "remote", "url": url, "enabled": True, "oauth": False}
    elif profile in ("qwen", "gemini"):
        entry = {"httpUrl": url, "timeout": 30000, "trust": True}
    elif profile == "agy":
        entry = {"serverUrl": url, "disabled": False}
    else:
        entry = server
    _replace_servers(config.setdefault(key, {}), entry)
    if profile == "claude":
        local = config.get("projects", {}).get(str(project_dir), {})
        local_servers = local.get("mcpServers", {})
        for name in (*_LEGACY_NAMES, _SERVER_NAME):
            local_servers.pop(name, None)
    if config == json.loads(original):
        return False
    _write_config(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    return True


def _sync_toml(path: Path, server: dict) -> bool:
    original = path.read_text()
    config = tomllib.loads(original)
    entry = {
        "url": server["url"],
        "startup_timeout_sec": 30.0,
        "tool_timeout_sec": 60.0,
        "default_tools_approval_mode": "approve",
    }
    servers = config.get("mcp_servers", {})
    if servers.get(_SERVER_NAME) == entry and not any(name in servers for name in _LEGACY_NAMES):
        return False
    blocks = re.split(r"(?m)(?=^\[)", original)
    retained = "".join(block for block in blocks if not _SERVER_TABLE.match(block)).rstrip()
    lines = [f"[mcp_servers.{_SERVER_NAME}]"]
    lines.extend(f"{key} = {json.dumps(value)}" for key, value in entry.items())
    updated = retained + "\n\n" + "\n".join(lines) + "\n"
    try:
        parsed = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError("Unsupported MCP TOML layout; configuration left untouched") from exc
    expected = dict(config)
    expected["mcp_servers"] = dict(servers)
    _replace_servers(expected["mcp_servers"], entry)
    if parsed != expected:
        raise ValueError("Unsupported MCP TOML layout; configuration left untouched")
    _write_config(path, updated)
    return True


def sync_mcp_config() -> None:
    settings = get_settings()
    mcp_config = build_mcp_provider_config(settings)
    if mcp_config is None:
        print("MCP server disabled; skipping CLI config sync.")
        return
    server = mcp_config["mcpServers"][_SERVER_NAME]
    project_dir = Path(__file__).resolve().parents[1]
    failures = []
    targets = [(Path.home() / name, profile) for name, profile in _JSON_PROFILES]
    targets.extend((Path.home() / name, "codex") for name in _TOML_PROFILES)
    for target, profile in targets:
        if not target.exists():
            continue
        target = target.resolve()
        try:
            changed = (_sync_toml(target, server) if profile == "codex"
                       else _sync_json(target, profile, server, project_dir))
            print(f"{target}: {'updated' if changed else 'unchanged'} (no default MCP token)")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            failures.append(str(target))
            print(f"{target}: config sync failed ({type(exc).__name__})", file=sys.stderr)
    if failures:
        raise RuntimeError(f"MCP config sync failed for {len(failures)} profile(s)")


if __name__ == "__main__":
    sync_mcp_config()
