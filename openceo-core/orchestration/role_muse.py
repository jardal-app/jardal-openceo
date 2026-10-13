"""Role profiles translated for Muse Code (TASK-2026-00197).

A role profile (``catalog/profiles/<role>.yaml``, engine-neutral) becomes
a Muse run: argv extras, a rendered ``settings.json`` overlay and an MCP
badge. The overlay is bind-mounted over the motor's settings by the
fence (``agent_sandbox.plan_agent``), so the Owner's global config is
never touched and concurrent runs never race on a shared file.

Control surface verified against Muse 1.4.7 (``MUSE_PINNED_VERSION``):
``-c`` accepts ``run.*`` overrides but refuses every ``skills.*`` key and
every ``mcpServers.<name>`` override (reserved names); the settings file
carries skill activation (``skills.activation.<scope>``) and the badged
MCP server instead. Anything else fails the smoke test on upgrade.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol

from catalog import RoleProfile, load_role_profile

logger = logging.getLogger(__name__)

__all__ = [
    "MUSE_PINNED_VERSION",
    "SETTINGS_OVERLAY_RELPATH",
    "ObjectiveBadgeProvider",
    "ReadonlyBadgeProvider",
    "RoleProfileError",
    "ServiceBadgeProvider",
    "SessionMeasure",
    "TicketBadgeProvider",
    "badge_ticket_for_task",
    "check_pinned_version",
    "dev_box_tools",
    "enumerate_skills",
    "issuer_for_repo",
    "load_global_settings",
    "measure_session_log",
    "render_activation",
    "render_argv_extras",
    "render_card",
    "render_settings",
    "resolve_muse_bin",
    "settings_overlay_bytes",
    "strip_echo_incompatible",
]

#: Pinned Muse build for role runs. Upgrading is deliberate: update the
#: pin, run the echo smoke test, then restart the service.
MUSE_PINNED_VERSION = "1.4.7-R6101.1"

#: Home-relative settings path the fence overlays per role run.
SETTINGS_OVERLAY_RELPATH = ".config/muse/settings.json"

#: ``skills list`` scopes the translator maps to activation keys
#: (``list`` reports ``bundled`` where ``disable`` takes ``built-in``).
_SKILL_SCOPES = ("bundled", "user", "plugin", "project")

#: Activation URI template for built-in skills (verified on the pinned
#: build; the smoke test fails if a skill escapes it after an upgrade).
_BUILTIN_KEY_TEMPLATE = "bundled://muse-core/skills/{skill_id}/SKILL.md"

#: Flags echo runs reject (meta-only or model-bound).
_ECHO_DROPPED_FLAGS = ("--model", "--reasoning-effort", "--effort",
                       "--output-schema")


class RoleProfileError(Exception):
    """The role profile cannot be honored; the run is refused."""


def render_card(profile: RoleProfile) -> str:
    """One-line role identity for ``run.developer_prompt``."""
    delivers = "; ".join(profile.card.delivers)
    never = "; ".join(profile.card.never)
    return (f"Role: {profile.name}. {profile.card.who} "
            f"Delivers: {delivers}. Never: {never}.")


def render_argv_extras(profile: RoleProfile) -> list[str]:
    """Muse argv turning the engine extras off (verified on the pin).

    Workflows, subagent delegation, reminder agents, foreign personal
    context and session persistence stay off; the role card rides
    ``run.developer_prompt``. ``profile`` only feeds the card today.
    """
    _ = profile
    return [
        "--disable-reminders",
        "--no-foreign-personal-context",
        "--no-session-log",
        "-c", 'run.workflow_trigger_mode="off"',
        "-c", 'run.subagent_delegation_mode="off"',
    ]


def render_card_argv(profile: RoleProfile) -> list[str]:
    """Card override as argv (kept separate: echo runs take it too)."""
    return ["-c", f"run.developer_prompt={json.dumps(render_card(profile))}"]


def strip_echo_incompatible(argv: list[str]) -> list[str]:
    """Drop flags ``--provider echo`` refuses (smoke test only)."""
    out: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        if token in _ECHO_DROPPED_FLAGS:
            skip_next = True
            continue
        if token.startswith("--model=") or token.startswith(
                "--reasoning-effort=") or token.startswith(
                "--output-schema=") or token.startswith("--effort="):
            continue
        out.append(token)
    return out


def resolve_muse_bin() -> str:
    """Plain ``muse`` binary (never the ``musej`` wrapper)."""
    found = shutil.which("muse")
    if not found:
        raise RoleProfileError("muse binary not found on PATH")
    return found


def check_pinned_version(muse_bin: str | None = None) -> str:
    """Refuse when the Muse build drifted from the pin (no silent skew)."""
    binary = muse_bin or resolve_muse_bin()
    env = {**os.environ, "MUSE_NO_AUTO_UPDATE": "1"}
    try:
        proc = subprocess.run(
            [binary, "--version"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            timeout=60, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RoleProfileError(f"muse --version failed: {exc}") from exc
    output = (proc.stdout or "").strip()
    if proc.returncode != 0 or MUSE_PINNED_VERSION not in output:
        raise RoleProfileError(
            f"muse version drift (want {MUSE_PINNED_VERSION}, got "
            f"{output[:120]!r}): update the pin and run the smoke test")
    return output


def enumerate_skills(
    workspace: str | Path,
    muse_bin: str | None = None,
    timeout_s: int = 120,
) -> dict[str, Any]:
    """Activation keys for every skill the binary sees, by scope.

    Runs ``muse skills list`` service-side (real home, run workspace):
    the overlay disables a superset of what the fenced child could
    load. Any failure refuses the run instead of degrading silently.
    """
    binary = muse_bin or resolve_muse_bin()
    ws = str(Path(workspace).resolve())
    env = {**os.environ, "MUSE_NO_AUTO_UPDATE": "1"}
    try:
        proc = subprocess.run(
            [binary, "skills", "list", "--source", "all", "--workspace", ws,
             "--trust-workspace", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            errors="replace", timeout=timeout_s, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RoleProfileError(f"muse skills list failed: {exc}") from exc
    if proc.returncode != 0:
        raise RoleProfileError(
            f"muse skills list failed: {(proc.stderr or '')[:300]}")
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise RoleProfileError(
            f"muse skills list is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RoleProfileError("muse skills list is not an object")
    skills = payload.get("skills")
    if not isinstance(skills, list):
        raise RoleProfileError("muse skills list has no skills array")
    off: dict[str, Any] = {"bundled": [], "user": [], "plugin": [],
                           "projects": {ws: []}}
    for entry in skills:
        if not isinstance(entry, dict):
            continue
        scope = str(entry.get("scope") or "")
        skill_id = str(entry.get("id") or entry.get("name") or "")
        path = str(entry.get("path") or "")
        if scope not in _SKILL_SCOPES or not skill_id:
            raise RoleProfileError(
                f"unmappable skill scope {scope!r} for {skill_id!r}: "
                "handle it deliberately")
        if scope == "bundled":
            short = skill_id.split(":", 1)[-1]
            off["bundled"].append(
                _BUILTIN_KEY_TEMPLATE.format(skill_id=short))
        elif scope in ("user", "plugin"):
            if not path:
                raise RoleProfileError(
                    f"skill {skill_id!r} has no path to disable")
            off[scope].append(path)
        else:
            if not path or path.startswith("/"):
                raise RoleProfileError(
                    f"project skill {skill_id!r} has no workspace path")
            off["projects"][ws].append(
                {"id": skill_id, "path": path})
    off["projects"][ws].sort(key=lambda item: item["path"])
    for key in ("bundled", "user", "plugin"):
        off[key] = sorted(set(off[key]))
    return off


def _activation_skill_id(key: str) -> str:
    """Skill id from an activation key (dir above ``SKILL.md``)."""
    parts = str(key).rstrip("/").split("/")
    if len(parts) >= 2 and parts[-1] == "SKILL.md":
        return parts[-2]
    return parts[-1]


def render_activation(
    skills_off: Mapping[str, Any],
    allowed: list[str] | None = None,
) -> dict[str, Any]:
    """``skills.activation`` switching everything off but ``allowed``."""
    keep = set(allowed or [])
    activation: dict[str, Any] = {}
    for scope in ("bundled", "user", "plugin"):
        keys = [key for key in skills_off.get(scope, [])]
        if keep:
            keys = [key for key in keys
                    if _activation_skill_id(key) not in keep]
        if keys:
            activation[scope] = {key: "off" for key in keys}
    projects = skills_off.get("projects", {})
    rendered_projects: dict[str, Any] = {}
    for project_ws, entries in projects.items():
        rows: dict[str, str] = {}
        for entry in entries:
            item = entry if isinstance(entry, dict) else {"path": entry}
            path = str(item.get("path") or "")
            skill_id = str(item.get("id") or "") or Path(path).parent.name
            if not path or skill_id in keep:
                continue
            rows[path] = "off"
        if rows:
            rendered_projects[str(project_ws)] = rows
    if rendered_projects:
        activation["projects"] = rendered_projects
    return activation


def load_global_settings(home: str | Path) -> dict[str, Any]:
    """Owner's global Muse settings (read-only; never written)."""
    path = Path(home) / SETTINGS_OVERLAY_RELPATH
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RoleProfileError(
            f"global muse settings unreadable: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RoleProfileError(
            f"global muse settings are not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RoleProfileError("global muse settings must be an object")
    return data


def render_settings(
    profile: RoleProfile,
    global_settings: Mapping[str, Any],
    mcp_servers: Mapping[str, Any],
    skills_activation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Full settings for one role run (the fence overlays this file).

    The MCP map is replaced, never merged: roles see exactly the given
    servers, never the Owner's personal ones. Pass an empty map when
    the run has no badge. Skill activation merges over the Owner's,
    so an Owner-disabled skill stays off.
    """
    out = copy.deepcopy(dict(global_settings))
    run = dict(out.get("run") or {})
    run["workflow_trigger_mode"] = "off"
    run["subagent_delegation_mode"] = "off"
    run["developer_prompt"] = render_card(profile)
    out["run"] = run
    if skills_activation:
        skills = copy.deepcopy(dict(out.get("skills") or {}))
        activation = copy.deepcopy(dict(skills.get("activation") or {}))
        for scope, keys in skills_activation.items():
            if scope == "projects":
                projects = copy.deepcopy(dict(activation.get("projects")
                                              or {}))
                for project_ws, rows in dict(keys).items():
                    merged = dict(projects.get(project_ws) or {})
                    merged.update(dict(rows))
                    projects[project_ws] = merged
                activation["projects"] = projects
            else:
                merged_scope = dict(activation.get(scope) or {})
                merged_scope.update(dict(keys))
                activation[scope] = merged_scope
        skills["activation"] = activation
        out["skills"] = skills
    out["mcpServers"] = copy.deepcopy(dict(mcp_servers))
    return out


def settings_overlay_bytes(
    profile: RoleProfile,
    home: str | Path,
    workspace: str | Path,
    mcp_servers: Mapping[str, Any],
    muse_bin: str | None = None,
) -> bytes:
    """Render + serialize the settings overlay for one role run."""
    check_pinned_version(muse_bin)
    global_settings = load_global_settings(home)
    skills_off = enumerate_skills(workspace, muse_bin=muse_bin)
    activation = render_activation(
        skills_off, allowed=list(profile.skills))
    rendered = render_settings(profile, global_settings, mcp_servers,
                               activation)
    return (json.dumps(rendered, indent=2,
                       ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class SessionMeasure:
    """Echo-run evidence: prompt bytes, skills, tools, blocks."""

    total_lane_bytes: int
    skill_ids: frozenset[str] = frozenset()
    mcp_tool_names: frozenset[str] = frozenset()
    blocks: dict[str, int] = field(default_factory=dict)
    rules_text: str = ""
    developer_prompt: str = ""
    reminder_roster: int = 0


def measure_session_log(path: str | Path,
                        max_bytes: int = 64 << 20) -> SessionMeasure:
    """Parse one echo ``session.jsonl`` into prompt evidence.

    ``total_lane_bytes`` is the exact prompt size the binary reports.
    Anything missing refuses the measurement instead of guessing.
    """
    log = Path(path)
    try:
        size = log.stat().st_size
    except OSError as exc:
        raise RoleProfileError(f"session log unreadable: {exc}") from exc
    if size > max_bytes:
        raise RoleProfileError(f"session log too large: {size} bytes")
    lanes: int | None = None
    skill_ids: set[str] = set()
    mcp_tools: set[str] = set()
    blocks: dict[str, int] = {}
    rules_text = ""
    developer_prompt = ""
    roster = 0
    try:
        lines = log.read_text(encoding="utf-8",
                              errors="replace").splitlines()
    except OSError as exc:
        raise RoleProfileError(f"session log unreadable: {exc}") from exc
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") or {}
        if record.get("payload_type") == \
                "runtime.mcp_tool_identity_catalog":
            for entry in payload.get("entries") or []:
                name = (entry or {}).get("raw_tool_name")
                if name:
                    mcp_tools.add(str(name))
            continue
        if record.get("payload_type") != "runtime.session":
            continue
        event = payload.get("event") or {}
        kind = event.get("kind")
        if kind == "model_input_trace_recorded":
            raw_lanes = (event.get("bounded") or {}).get(
                "total_lane_bytes")
            if raw_lanes is None:
                lanes = None
            else:
                try:
                    lanes = int(raw_lanes)
                except (TypeError, ValueError) as exc:
                    raise RoleProfileError(
                        "model input trace has no byte count") from exc
        elif kind == "context_block_diagnostic":
            block_id = str(event.get("block_id") or "")
            if block_id:
                blocks[block_id] = int(event.get("text_bytes") or 0)
        elif kind == "model_request_configured":
            developer_prompt = str(event.get("developer_prompt") or "")
            roster = len((event.get("reminder_roster") or {}).get(
                "agents") or [])
            for message in event.get("run_context_messages") or []:
                if not isinstance(message, dict):
                    continue
                if message.get("id") == "rules_file":
                    rules_text = str(message.get("text") or "")
                if message.get("id") == "skills_catalog":
                    skill_ids = _catalog_skill_ids(
                        str(message.get("text") or ""))
    if lanes is None:
        raise RoleProfileError("session log has no model input trace")
    return SessionMeasure(
        total_lane_bytes=lanes, skill_ids=frozenset(skill_ids),
        mcp_tool_names=frozenset(mcp_tools), blocks=blocks,
        rules_text=rules_text, developer_prompt=developer_prompt,
        reminder_roster=roster)


def _catalog_skill_ids(catalog_text: str) -> set[str]:
    """Skill ids listed in a ``<skill-catalog>`` block."""
    return set(re.findall(r'<skill id=\\?"?([^\\?">\s]+)', catalog_text))


class BadgeProvider(Protocol):
    """One-shot MCP badges: minted per role run, revoked right after."""

    async def mint(self, role: str) -> str: ...
    def mcp_servers(self, badge: str) -> dict[str, Any]: ...
    async def revoke(self, badge: str) -> None: ...


def issuer_for_repo(repo: Any) -> Any | None:
    """Token issuer over the service Mongo, None when unbound (tests)."""
    from catalog import load_catalog
    from mcp_identity import MongoTokenStore, TokenIssuer
    if repo is None or getattr(repo, "db", None) is None:
        return None
    return TokenIssuer(MongoTokenStore(db_provider=lambda: repo.db),
                       load_catalog().agents)


def _provider_servers(settings: Any, badge: str) -> dict[str, Any]:
    from mcp_server import build_mcp_provider_config
    config = build_mcp_provider_config(settings, bearer=badge)
    if not config:
        return {}
    return dict(config.get("mcpServers") or {})


def badge_ticket_for_task(task: Mapping[str, Any]) -> str | None:
    """ERPNext ticket name for an agent badge, None without a mirror.

    Agent badges bind to the business ticket (``TASK-2026-NNNNN``), never
    to the Mongo ``task_id`` uuid: the issuer refuses anything else, and
    only the business name matches the ticket params agents call with.
    """
    params = task.get("params")
    mirrors = params.get("_mirrors") if isinstance(params, dict) else None
    mirror = mirrors.get("erpnext") if isinstance(mirrors, dict) else None
    name = str(mirror.get("name") or "").strip() \
        if isinstance(mirror, dict) else ""
    return name or None


class TicketBadgeProvider:
    """Ticket-bound agent badge (dev builder/closer/Tier C)."""

    def __init__(self, issuer: Any, settings: Any, agent: str,
                 ticket: str, project: str,
                 ttl: timedelta | None = None) -> None:
        self._issuer = issuer
        self._settings = settings
        self._agent = agent
        self._ticket = ticket
        self._project = project
        self._ttl = ttl

    async def mint(self, role: str) -> str:
        del role
        return await self._issuer.issue_agent(
            self._agent, self._ticket, self._project, self._ttl)

    def mcp_servers(self, badge: str) -> dict[str, Any]:
        return _provider_servers(self._settings, badge)

    async def revoke(self, badge: str) -> None:
        try:
            await self._issuer.revoke(badge, "role run finished")
        except Exception:
            logger.warning("role badge revocation failed", exc_info=True)


class ReadonlyBadgeProvider:
    """Read-only principal badge (planner, ceo)."""

    def __init__(self, issuer: Any, settings: Any,
                 principal: str) -> None:
        self._issuer = issuer
        self._settings = settings
        self._principal = principal

    async def mint(self, role: str) -> str:
        del role
        return await self._issuer.issue_readonly(self._principal)

    def mcp_servers(self, badge: str) -> dict[str, Any]:
        return _provider_servers(self._settings, badge)

    async def revoke(self, badge: str) -> None:
        try:
            await self._issuer.revoke(badge, "role run finished")
        except Exception:
            logger.warning("role badge revocation failed", exc_info=True)


class ServiceBadgeProvider:
    """Project-scoped service badge (closer, interactive dev)."""

    def __init__(self, issuer: Any, settings: Any, name: str,
                 project: str, tools: list[str] | tuple[str, ...],
                 ttl: timedelta | None = None) -> None:
        self._issuer = issuer
        self._settings = settings
        self._name = name
        self._project = project
        self._tools = list(tools)
        self._ttl = ttl

    async def mint(self, role: str) -> str:
        del role
        return await self._issuer.issue_service(
            self._name, self._project, self._tools, self._ttl)

    def mcp_servers(self, badge: str) -> dict[str, Any]:
        return _provider_servers(self._settings, badge)

    async def revoke(self, badge: str) -> None:
        try:
            await self._issuer.revoke(badge, "role run finished")
        except Exception:
            logger.warning("role badge revocation failed", exc_info=True)


def dev_box_tools() -> tuple[str, ...]:
    """Dev boundary box from the catalog (closer service badges)."""
    from catalog import load_catalog
    card = load_catalog().agents.get("dev")
    if card is None:
        raise RoleProfileError("no dev card in the catalog")
    return tuple(card.boundary_tools)


class ObjectiveBadgeProvider:
    """Objective flows: readonly planner, ticket or service closer."""

    def __init__(self, issuer: Any, settings: Any, project: str,
                 ticket: str | None = None,
                 closer_tools: list[str] | tuple[str, ...] | None = None,
                 ) -> None:
        self._issuer = issuer
        self._settings = settings
        self._project = project
        self._ticket = (ticket or "").strip() or None
        self._closer_tools = closer_tools

    async def mint(self, role: str) -> str:
        if role == "objective_planner":
            return await self._issuer.issue_readonly("planner")
        if role == "closer":
            if self._ticket:
                return await self._issuer.issue_agent(
                    "dev", self._ticket, self._project, None)
            tools = (list(self._closer_tools) if self._closer_tools
                     else list(dev_box_tools()))
            return await self._issuer.issue_service(
                "closer", self._project, tools)
        raise RoleProfileError(
            f"no badge rule for objective role {role!r}")

    def mcp_servers(self, badge: str) -> dict[str, Any]:
        return _provider_servers(self._settings, badge)

    async def revoke(self, badge: str) -> None:
        try:
            await self._issuer.revoke(badge, "role run finished")
        except Exception:
            logger.warning("role badge revocation failed", exc_info=True)
