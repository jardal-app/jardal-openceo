"""Role profiles translated for OpenCode (TASK-2026-00200).

The same engine-neutral profile (``catalog/profiles/<role>.yaml``) becomes
an OpenCode agent entry: the card rides the agent ``prompt`` together with
the rules file and the allowed skills, ``permission.skill`` scopes the
skill tool to the role, and the ``mcp`` map is replaced by the badged
server (never merged with the Owner's). The run selects it with
``--agent <role>``.

Config surface verified against the official docs (agents, permissions,
remote MCP servers with ``headers``); the binary on this host is
``opencode v2.0.22``. The overlay starts from the host global config, so a
host that never initialized OpenCode (no ``~/.config/opencode/``) refuses
with :class:`RoleProfileError` — fail-closed, never a bare defaults-only
config. Live runs land in the epic Acceptance.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Mapping

from orchestration.role_muse import RoleProfileError, render_card

logger = logging.getLogger(__name__)

__all__ = [
    "OPENCODE_OVERLAY_RELPATH",
    "opencode_config_bytes",
    "render_agent_entry",
    "render_argv_extras",
    "render_config",
    "render_mcp_servers",
]

#: Home-relative config path the fence overlays per role run. The file
#: exists (global OpenCode config); the overlay shadows it per run, so
#: the Owner's config is never touched.
OPENCODE_OVERLAY_RELPATH = ".config/opencode/opencode.json"


def render_agent_entry(profile: Any, model: str | None = None) -> dict[str, Any]:
    """``agent.<role>`` entry: prompt, skill permissions, optional model."""
    skills = list(profile.skills)
    if skills:
        skill_lines = ("Skills: " + ", ".join(skills) +
                       " (use only these skills).")
        permission: dict[str, Any] = {
            "skill": {name: "allow" for name in skills}}
    else:
        skill_lines = "Skills: none (do not use the skill tool)."
        permission = {"skill": "deny"}
    entry: dict[str, Any] = {
        "description": profile.card.who,
        "mode": "primary",
        "prompt": (render_card(profile) + "\n\n" +
                   f"Rules: {profile.rules} (repo root, always loaded).\n" +
                   skill_lines),
        "permission": permission,
    }
    if model:
        entry["model"] = model
    return entry


def render_mcp_servers(mcp_servers: Mapping[str, Any]) -> dict[str, Any]:
    """Badged servers translated to OpenCode remote entries (with bearer)."""
    out: dict[str, Any] = {}
    for name, server in dict(mcp_servers).items():
        entry = dict(server or {})
        url = str(entry.get("url") or "").strip()
        if not url:
            raise RoleProfileError(
                f"opencode mcp server {name!r} has no url")
        translated: dict[str, Any] = {
            "type": "remote",
            "url": url,
            "enabled": True,
        }
        if entry.get("headers"):
            translated["headers"] = copy.deepcopy(dict(entry["headers"]))
        out[str(name)] = translated
    return out


def render_config(profile: Any, global_config: Mapping[str, Any],
                  mcp_servers: Mapping[str, Any],
                  model: str | None = None) -> dict[str, Any]:
    """Full config for one role run (the fence overlays this file).

    The role agent entry and the MCP map are replaced, never merged:
    the role sees exactly its prompt and the badged server. The rest of
    the Owner's config (theme, other agents, defaults) stays untouched.
    """
    out = copy.deepcopy(dict(global_config))
    agents = out.get("agent")
    if agents is None:
        agents = {}
        out["agent"] = agents
    if not isinstance(agents, dict):
        raise RoleProfileError("opencode global agent section is not a map")
    agents[profile.name] = render_agent_entry(profile, model=model)
    out["mcp"] = render_mcp_servers(mcp_servers)
    return out


def opencode_config_bytes(profile: Any, home: str | Path,
                          mcp_servers: Mapping[str, Any],
                          model: str | None = None) -> bytes:
    """Render + serialize the config overlay for one role run."""
    path = Path(home) / OPENCODE_OVERLAY_RELPATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoleProfileError(
            f"global opencode config unreadable: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RoleProfileError(
            f"global opencode config is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RoleProfileError("global opencode config must be an object")
    rendered = render_config(profile, data, mcp_servers, model=model)
    return (json.dumps(rendered, indent=2,
                       ensure_ascii=False) + "\n").encode("utf-8")


def render_argv_extras(agent_name: str) -> list[str]:
    """OpenCode argv selecting the role agent entry."""
    return ["--agent", agent_name]
