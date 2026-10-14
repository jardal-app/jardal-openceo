"""Role profiles translated for agy (TASK-2026-00246).

One CLI, three Owner OAuth logins (``agyd``/``agyj``/``agy2`` select the
account via their wrapper: default dir, ``--gemini_dir ~/.gemini-j``,
``--gemini_dir ~/.gemini-2``). The run is headless through ``--print``,
which takes the prompt as its value (verified: bare ``--print`` errors
``flag needs an argument``). The role card rides the prompt; agy has no
skill-activation flags, so skills work by prompt reference to their
SKILL.md like everywhere else.

The badged MCP server travels as a per-run overlay of the account's own
``config/mcp_config.json``: the base file is read, ``openceo-internal``
is replaced by the badged entry (same ``serverUrl``/``headers`` shape
agy itself stores), everything else stays. An unreadable base refuses
with :class:`RoleProfileError` — only effectively configured accounts
run. The binary on this host is ``agy 1.3.5``. Live runs land in the
epic Acceptance.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from orchestration.role_muse import RoleProfileError

__all__ = [
    "AGY_HOME_SUBDIRS",
    "AGY_MCP_SERVER",
    "agy_config_bytes",
    "overlay_relpath",
    "render_argv_extras",
    "render_mcp_config",
]

#: Account command → home subdir holding its config (single origin; the
#: sandbox fence reads it too). Bare ``agy`` shares the default dir.
AGY_HOME_SUBDIRS = {
    "agyd": ".gemini",
    "agyj": ".gemini-j",
    "agy2": ".gemini-2",
}

#: MCP server name inside the account config.
AGY_MCP_SERVER = "openceo-internal"


def overlay_relpath(account: str) -> str:
    """Home-relative config path the fence overlays for one agy run."""
    subdir = AGY_HOME_SUBDIRS.get(account)
    if subdir is None:
        raise RoleProfileError(
            f"unknown agy account {account!r} "
            f"(known: {', '.join(sorted(AGY_HOME_SUBDIRS))})")
    return f"{subdir}/config/mcp_config.json"


def render_argv_extras() -> list[str]:
    """Translator extras: ``--print`` takes the prompt as its value."""
    return ["--print"]


def render_mcp_config(
    base: Mapping[str, Any],
    mcp_servers: Mapping[str, Any],
) -> dict[str, Any]:
    """Account config with the badged servers replaced, rest untouched.

    ``mcp_servers`` entries carry ``url`` + ``headers`` (the badge is
    already in the headers); each renders to agy's own
    ``serverUrl``/``headers`` entry shape.
    """
    out = copy.deepcopy(dict(base))
    servers = out.get("mcpServers")
    if servers is None:
        servers = {}
        out["mcpServers"] = servers
    if not isinstance(servers, dict):
        raise RoleProfileError("agy mcpServers section is not a map")
    for name, server in dict(mcp_servers).items():
        entry = dict(server or {})
        url = str(entry.get("url") or "").strip()
        if not url:
            raise RoleProfileError(
                f"agy mcp server {name!r} has no url")
        translated: dict[str, Any] = {
            "disabled": False,
            "serverUrl": url,
        }
        if entry.get("headers"):
            translated["headers"] = copy.deepcopy(dict(entry["headers"]))
        servers[str(name)] = translated
    return out


def agy_config_bytes(
    home: str | Path,
    account: str,
    mcp_servers: Mapping[str, Any],
) -> bytes:
    """Render + serialize the MCP config overlay for one agy run."""
    relpath = overlay_relpath(account)
    path = Path(home) / relpath
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoleProfileError(
            f"agy account config unreadable: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RoleProfileError(
            f"agy account config is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RoleProfileError("agy account config must be an object")
    rendered = render_mcp_config(data, mcp_servers)
    return (json.dumps(rendered, indent=2,
                       ensure_ascii=False) + "\n").encode("utf-8")
