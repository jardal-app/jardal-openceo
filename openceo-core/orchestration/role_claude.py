"""Role profiles translated for Claude Code (TASK-2026-00200).

The same engine-neutral profile (``catalog/profiles/<role>.yaml``) becomes
a Claude run: the card rides ``--append-system-prompt``, the box tools are
pre-approved via ``--allowedTools`` with explicit ``mcp__<server>__<tool>``
names (wildcards are unreliable in headless mode), and the badged MCP
server arrives via ``--mcp-config`` + ``--strict-mcp-config`` over a
per-run overlay. Rules come from the workspace ``AGENTS.md`` (read by the
motor itself) and are named in the prompt, never inlined.

Control surface verified against Claude Code 2.1.296 (``claude --help``)
plus the headless MCP conventions (``mcp__<server>__<tool>`` naming,
``--strict-mcp-config``). Live runs land in the epic Acceptance.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from orchestration.role_muse import RoleProfileError, render_card

logger = logging.getLogger(__name__)

__all__ = [
    "CLAUDE_BUILTIN_TOOLS",
    "CLAUDE_MCP_OVERLAY_RELPATH",
    "CLAUDE_MCP_SERVER",
    "CLAUDE_NO_AUTO_MEMORY_ENV",
    "claude_config_bytes",
    "claude_no_auto_memory_env",
    "ensure_mcp_placeholder",
    "profile_box_tools",
    "render_allowed_tools",
    "render_argv_extras",
    "render_mcp_config",
    "render_system_prompt",
]

#: Home-relative MCP config path the fence overlays per role run. The
#: parent (``~/.claude``) is already a fenced read-only bind, so the
#: overlay shadows it per run; the translator only ensures an empty
#: 0600 placeholder exists host-side (the fence never creates targets).
CLAUDE_MCP_OVERLAY_RELPATH = ".claude/openceo-run-mcp.json"

#: MCP server name the box tools are pre-approved under.
CLAUDE_MCP_SERVER = "openceo-internal"

#: Auto memory stays off in OpenCEO-launched Claude runs (D5): learnings
#: belong to the project vault, never to the engine's own memory.
CLAUDE_NO_AUTO_MEMORY_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


def claude_no_auto_memory_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Launch env for a Claude run: ``extra`` over the D5 memory kill-switch."""
    return {**CLAUDE_NO_AUTO_MEMORY_ENV,
            **{str(k): str(v) for k, v in dict(extra or {}).items()}}

#: Engine file/shell tools pre-approved next to the box. The fence still
#: decides WHERE the run may write and the badge which MCP tools answer;
#: this list only keeps headless runs from blocking on approval prompts.
CLAUDE_BUILTIN_TOOLS = ("Read", "Glob", "Grep", "Bash", "Edit", "Write")


def render_system_prompt(profile: Any) -> str:
    """Role identity for ``--append-system-prompt`` (the shared card)."""
    return render_card(profile)


def profile_box_tools(profile: Any, catalog: Any | None = None) -> tuple[str, ...]:
    """MCP tool names the run may call: the card box, or the readonly box.

    Planner/ceo profiles are read-only principals, never catalog cards;
    anything else unknown refuses instead of guessing a box.
    """
    from mcp_identity import READONLY_BOX, READONLY_PRINCIPALS
    if profile.agent in READONLY_PRINCIPALS:
        return tuple(sorted(READONLY_BOX))
    cards = catalog.agents if catalog is not None else _load_agents()
    card = cards.get(profile.agent)
    if card is None:
        raise RoleProfileError(
            f"profile {profile.name!r} agent {profile.agent!r} has no "
            "catalog card and is not a read-only principal")
    return tuple(card.boundary_tools)


def _load_agents() -> Mapping[str, Any]:
    from catalog import load_catalog
    return load_catalog().agents


def render_allowed_tools(box_tools: Collection[str],
                         server: str = CLAUDE_MCP_SERVER) -> str:
    """One ``--allowedTools`` token: builtins plus explicit MCP names."""
    names = [f"mcp__{server}__{tool}" for tool in sorted(set(box_tools))]
    return ",".join([*CLAUDE_BUILTIN_TOOLS, *names])


def render_mcp_config(mcp_servers: Mapping[str, Any]) -> dict[str, Any]:
    """``--mcp-config`` content: exactly the badged servers, nothing else."""
    return {"mcpServers": dict(mcp_servers)}


def claude_config_bytes(mcp_servers: Mapping[str, Any]) -> bytes:
    """Render + serialize the MCP config overlay for one role run."""
    return (json.dumps(render_mcp_config(mcp_servers), indent=2,
                       ensure_ascii=False) + "\n").encode("utf-8")


def ensure_mcp_placeholder(home: str | Path) -> Path:
    """Empty 0600 file the fence overlays per-run MCP content over."""
    target = Path(home) / CLAUDE_MCP_OVERLAY_RELPATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RoleProfileError(
            f"claude mcp placeholder parent refused: {exc}") from exc
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RoleProfileError(
            f"claude mcp placeholder is not a file: {target}")
    if not target.exists():
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # a concurrent run created it first (review 00019)
        except OSError as exc:
            raise RoleProfileError(
                f"claude mcp placeholder refused: {exc}") from exc
        else:
            with os.fdopen(fd, "wb"):
                pass
    return target


def render_argv_extras(profile: Any, box_tools: Collection[str],
                       mcp_config_path: str | Path,
                       server: str = CLAUDE_MCP_SERVER) -> list[str]:
    """Claude argv carrying the card, the box tools and the badged MCP."""
    return [
        "--append-system-prompt", render_system_prompt(profile),
        "--allowedTools", render_allowed_tools(box_tools, server),
        "--mcp-config", str(mcp_config_path),
        "--strict-mcp-config",
    ]
