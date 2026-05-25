#!/usr/bin/env python3
"""Sync MCP provider config into Claude Code CLI config files.

Writes the openceo-internal MCP server block into:
- ~/.claude/.claude.json       (clauded — planner)
- ~/.claude-deepseek/.claude.json (claudep — builder)

Run at bootstrap time (before OpenCEO tmux sessions start) so that
planner/builder agents can call openceo_* MCP tools.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure we can import from openceo-core adjacent to this script
_SCRIPT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _SCRIPT_DIR.parent / "openceo-core"
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from settings import get_settings
from mcp_server import build_mcp_provider_config

TARGETS = [
    Path.home() / ".claude" / ".claude.json",
    Path.home() / ".claude-deepseek" / ".claude.json",
]


def _backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".backup." + os.urandom(3).hex())


def sync_mcp_config() -> None:
    settings = get_settings()
    mcp_config = build_mcp_provider_config(settings)
    if mcp_config is None:
        print("MCP server not enabled (MCP_SERVER_ENABLED=false). Skipping config sync.")
        return

    mcp_servers_block = mcp_config.get("mcpServers", {})
    if not mcp_servers_block:
        print("No mcpServers block generated. Skipping.")
        return

    for target in TARGETS:
        try:
            existing: dict = {}
            if target.exists():
                existing = json.loads(target.read_text())

            # Only update if the mcpServers block is missing or different
            current_mcp = existing.get("mcpServers", {})
            if current_mcp.get("openceo-internal") == mcp_servers_block.get("openceo-internal"):
                print(f"  {target} already up to date — skipping")
                continue

            # Backup existing file
            if target.exists():
                backup = _backup_path(target)
                target.rename(backup)
                print(f"  Backed up {target} → {backup.name}")

            existing.setdefault("mcpServers", {}).update(mcp_servers_block)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(existing, indent=2) + "\n")
            print(f"  Written {target}")
        except Exception as exc:
            print(f"  ERROR updating {target}: {exc}")

    print("MCP config sync complete.")


if __name__ == "__main__":
    sync_mcp_config()
