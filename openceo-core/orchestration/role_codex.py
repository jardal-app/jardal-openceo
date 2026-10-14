"""Role profiles translated for Codex CLI (TASK-2026-00246).

The Tier B pattern, reused for generic runs: the MCP badge travels as a
``-c mcp_servers.<server>.bearer_token_env_var`` override pointing at a
per-run env var, and ``openceo-internal`` itself comes from the account's
own ``config.toml`` (present in both codex logins on this host). The role
card rides the prompt (the generic prompt already carries who/never);
Codex has no skill-activation flags, so skills work by prompt reference
to their SKILL.md like everywhere else.

Headless shape verified: ``codexj exec --ephemeral`` reads the prompt
from stdin when no positional prompt is given (``codex exec --help``);
the binary on this host is ``codex-cli 0.162.1``. Live runs land in the
epic Acceptance.
"""

from __future__ import annotations

__all__ = [
    "CODEX_MCP_SERVER",
    "CODEX_MCP_TOKEN_ENV",
    "codex_mcp_env",
    "render_argv_extras",
    "render_mcp_override",
]

#: MCP server name inside the account's codex config.
CODEX_MCP_SERVER = "openceo-internal"

#: Per-run env var carrying the ticket badge (mirrors the Tier B
#: convention with its own name so generic runs never share it).
CODEX_MCP_TOKEN_ENV = "OPENCEO_GENERIC_MCP_TOKEN"


def render_mcp_override(
    token_env: str = CODEX_MCP_TOKEN_ENV,
    server: str = CODEX_MCP_SERVER,
) -> list[str]:
    """``-c`` override pointing the badged server at the token env var."""
    return ["-c",
            f"mcp_servers.{server}.bearer_token_env_var=\"{token_env}\""]


def codex_mcp_env(badge: str) -> dict[str, str]:
    """Per-run env carrying the ticket badge for the override above."""
    return {CODEX_MCP_TOKEN_ENV: badge}


def render_argv_extras(
    token_env: str = CODEX_MCP_TOKEN_ENV,
) -> list[str]:
    """Translator extras: the MCP override only (card rides the prompt)."""
    return render_mcp_override(token_env)
