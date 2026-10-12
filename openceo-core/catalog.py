"""Single agent catalog: format, loader and validation (TASK-2026-00191).

The catalog (``catalog/`` in git) is the only source of who exists in the
company and what each one can do: a short index plus one card per agent,
plus the staff roster. The loader validates with Pydantic and fails
closed: :func:`load_catalog` raises :class:`CatalogError` naming every
problem, and the service refuses to start on an invalid catalog
(``main.py`` lifespan gate).

Shape validated here; boundary tool names resolve against
``MCP_BOUNDARY_TOOLS`` (TASK-2026-00199). Role skills (TASK-2026-00203)
live under each card's ``skills_dir`` (``skills/<role>/<skill>/SKILL.md``)
and are loaded and cross-checked: an AI card lists exactly what is on
disk, profiles add only Muse engine skills beyond their card, and every
skill is exposed once via ``.codex/skills/``. Role ``profile`` files
(TASK-2026-00197) exist under ``catalog/profiles/``: a card pointing at
one loads and cross-checks it, and every file in that directory must
parse. Consumers (TASK-2026-00195) validate at use.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

__all__ = [
    "CATALOG_VERSION",
    "ENGINE_SKILLS",
    "INDEX_SUMMARY_MAX_CHARS",
    "MCP_BOUNDARY_TOOLS",
    "OWNER_SKILLS",
    "PROJECT_SKILLS_DIR",
    "RESERVED_NAMES",
    "AgentCard",
    "AgentType",
    "Catalog",
    "CatalogError",
    "CatalogStatus",
    "IndexEntry",
    "KNOWN_ENGINES",
    "LEGACY_LIVE_DISABLED",
    "RoleCard",
    "RoleProfile",
    "RoleSkill",
    "StaffEntry",
    "StaffRoster",
    "TICKET_PATTERN",
    "catalog_fingerprint",
    "catalog_root",
    "check_catalog",
    "check_skill_links",
    "check_skill_names_unique",
    "find_consumer_mismatches",
    "is_staff_active",
    "load_all_role_skills",
    "load_catalog",
    "load_role_profile",
    "load_role_skills",
    "parse_role_skill",
    "planner_registry_view",
    "profiles_root",
    "short_index",
    "skills_index_for_planner",
    "suggestable_skills",
    "sync_skill_links",
]

#: The only index version this loader reads. A newer version fails closed
#: instead of loading with unknown semantics.
CATALOG_VERSION = 1

#: One index line per agent: single line, bounded so the always-loaded
#: short index stays short.
INDEX_SUMMARY_MAX_CHARS = 280

#: The Owner's skills (epic contract, TASK-2026-00191 scope).
OWNER_SKILLS = ("decidir", "aprobar", "responder", "hacer")

#: Muse engine skills a role profile may allow beyond its card skills
#: (TASK-2026-00203): skill authoring everywhere, git knowledge for dev.
ENGINE_SKILLS = ("create-skill", "git")

#: Project skill exposure dir, relative to the repo root. Muse discovers
#: project skills only under ``.codex/skills/`` (``.claude/skills/`` is the
#: lower-priority twin), so each role skill is exposed there as one
#: symlink to its source dir under ``skills/<role>/``.
PROJECT_SKILLS_DIR = ".codex/skills"

#: Never executors, never catalog names: the CEO strategy and the planner
#: decide work, they do not run tickets.
RESERVED_NAMES = ("ceo", "planner")

#: Engines a role profile may name (TASK-2026-00197; TASK-2026-00200 adds
#: translators, not engines: agents keep running muse-code by default).
KNOWN_ENGINES = ("muse-code",)

#: Every tool the MCP mounts may expose (TASK-2026-00199). A card box
#: naming anything else fails the load; a registration outside this set
#: fails the mount build. Both directions fail closed.
MCP_BOUNDARY_TOOLS = frozenset({
    "manage_dns_record",
    "openceo_create_ticket",
    "openceo_create_tickets_batch",
    "openceo_create_pm_tickets_batch",
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_create_objective",
    "openceo_transition_ticket",
    "openceo_transition_tickets_batch",
    "openceo_update_ticket",
    "openceo_add_ticket_comment",
    "openceo_update_ticket_comment",
    "openceo_delete_ticket_comment",
    "openceo_attach_ticket_file",
    "openceo_get_ticket_attachment",
    "openceo_update_ticket_attachment",
    "openceo_delete_ticket_attachment",
    "openceo_send_email",
    "openceo_ensure_erpnext_project",
    "openceo_register_expense",
    "openceo_register_income",
    "openceo_scope_summary",
    "openceo_contribution_10_status",
    "openceo_elfec_house_payment",
})

_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"
#: Skill slugs accept hyphens (``notion-api-local``), agent names do not.
_SKILL_NAME_PATTERN = r"^[a-z][a-z0-9_-]*$"
TICKET_PATTERN = r"^TASK-2026-\d+$"
_TICKET_PATTERN = TICKET_PATTERN


class CatalogError(Exception):
    """The catalog tree is invalid; the message names every problem."""


class AgentType(str, Enum):
    AI = "ai"
    HUMAN = "human"


def _nonempty_unique(items: list[str], field: str) -> list[str]:
    cleaned = [item.strip() for item in items]
    if any(not item for item in cleaned):
        raise ValueError(f"{field}: items must be non-empty strings")
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"{field}: items must be unique")
    return cleaned


class IndexEntry(BaseModel):
    """One line of the short index: what the agent does and does not do."""

    name: str = Field(pattern=_NAME_PATTERN)
    summary: str = Field(min_length=1, max_length=INDEX_SUMMARY_MAX_CHARS)
    card: str

    @field_validator("name")
    @classmethod
    def _name_not_reserved(cls, value: str) -> str:
        if value in RESERVED_NAMES:
            raise ValueError(f"name: {value!r} is reserved, never an executor")
        return value

    @field_validator("summary")
    @classmethod
    def _summary_single_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("summary: must be a single line")
        return value.strip()

    @field_validator("card")
    @classmethod
    def _card_relative_yaml(cls, value: str) -> str:
        if not value or value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("card: must be a relative path inside the catalog")
        if not value.endswith(".yaml"):
            raise ValueError("card: must point to a .yaml file")
        return value


class AgentCard(BaseModel):
    """One agent: type, engine and profile, tool box, skills, enablement."""

    name: str = Field(pattern=_NAME_PATTERN)
    type: AgentType
    engine: str | None = None
    profile: str | None = None
    boundary_tools: list[str] = Field(default_factory=list)
    skills_dir: str
    skills: list[str] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    enabled: bool
    disabled_reason: str | None = None
    decided_by_ticket: str | None = Field(default=None, pattern=_TICKET_PATTERN)

    @field_validator("name")
    @classmethod
    def _name_not_reserved(cls, value: str) -> str:
        if value in RESERVED_NAMES:
            raise ValueError(f"name: {value!r} is reserved, never an executor")
        return value

    @field_validator("engine", "profile")
    @classmethod
    def _optional_nonempty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be null or a non-empty string")
        return cleaned

    @field_validator("boundary_tools", "skills", "inputs", "outputs")
    @classmethod
    def _lists_clean(cls, value: list[str], info: Any) -> list[str]:
        return _nonempty_unique(value, str(info.field_name))

    @field_validator("skills_dir")
    @classmethod
    def _skills_dir_relative(cls, value: str) -> str:
        if not value or value.startswith("/") or ".." in Path(value).parts:
            raise ValueError(
                "skills_dir: must be a relative path inside the repo")
        return value

    @model_validator(mode="after")
    def _cross_rules(self) -> "AgentCard":
        if self.type is AgentType.AI and not self.engine:
            raise ValueError("engine: required for ai agents")
        if self.type is AgentType.HUMAN and self.engine is not None:
            raise ValueError("engine: must be null for human agents")
        if self.enabled:
            if self.disabled_reason is not None:
                raise ValueError(
                    "disabled_reason: must be null when enabled")
            if self.decided_by_ticket is not None:
                raise ValueError(
                    "decided_by_ticket: must be null when enabled")
        else:
            if not (self.disabled_reason or "").strip():
                raise ValueError(
                    "disabled_reason: required when enabled is false")
            if not self.decided_by_ticket:
                raise ValueError(
                    "decided_by_ticket: required when enabled is false")
        if self.name == "owner":
            if self.type is not AgentType.HUMAN:
                raise ValueError("owner: type must be human")
            if set(self.skills) != set(OWNER_SKILLS):
                raise ValueError(
                    f"skills: owner must list exactly {', '.join(OWNER_SKILLS)}")
        return self


class RoleCard(BaseModel):
    """Who the role is, what it delivers, what it never does."""

    model_config = ConfigDict(extra="forbid")

    who: str = Field(min_length=1)
    delivers: list[str] = Field(min_length=1)
    never: list[str] = Field(min_length=1)

    @field_validator("delivers", "never")
    @classmethod
    def _lists_clean(cls, value: list[str], info: Any) -> list[str]:
        return _nonempty_unique(value, str(info.field_name))


class RoleProfile(BaseModel):
    """One engine-neutral role profile (TASK-2026-00197).

    The card identity, rules, allowed skills and output schema travel
    here; each engine translator renders them for its own CLI. ``agent``
    is the catalog agent or the planner/ceo principal the profile
    belongs to; launch roles (builder, closer) share their agent file.
    ``rules`` and ``output_schema`` are repo-relative paths; the schema
    documents the verdict contract while TASK-2026-00202 owns the
    ``--output-schema`` flag wiring.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_NAME_PATTERN)
    agent: str = Field(pattern=_NAME_PATTERN)
    engine: str
    card: RoleCard
    rules: str
    skills: list[str] = Field(default_factory=list)
    output_schema: str | None = None

    @field_validator("engine")
    @classmethod
    def _engine_known(cls, value: str) -> str:
        if value not in KNOWN_ENGINES:
            raise ValueError(f"engine: unknown engine {value!r}")
        return value

    @field_validator("skills")
    @classmethod
    def _skills_clean(cls, value: list[str]) -> list[str]:
        return _nonempty_unique(value, "skills")

    @field_validator("rules", "output_schema")
    @classmethod
    def _repo_relpath(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("must be null or a relative path inside the repo")
        return value

    @model_validator(mode="after")
    def _belongs_to_one(self) -> "RoleProfile":
        if self.agent != self.name:
            raise ValueError(
                f"agent: {self.agent!r} must match the profile name "
                f"{self.name!r}")
        return self


class RoleSkill(BaseModel):
    """One versioned role skill (TASK-2026-00203).

    Source: ``skills/<role>/<skill>/SKILL.md`` (Agent Skills standard
    plus the OpenCEO header: ``status``, creation ``ticket``, ``date``).
    ``source`` is the skill directory as loaded; the catalog resolves it
    against the ``.codex/skills/`` exposure symlink.
    """

    role: str = Field(pattern=_NAME_PATTERN)
    name: str = Field(pattern=_SKILL_NAME_PATTERN)
    description: str = Field(min_length=1)
    status: Literal["borrador", "activa"]
    ticket: str = Field(pattern=_TICKET_PATTERN)
    date: date
    source: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _name_not_engine(cls, value: str) -> str:
        if value in ENGINE_SKILLS:
            raise ValueError(
                f"name: {value!r} is a muse engine skill, never a role skill "
                "(activation matches by short id across scopes)")
        return value

    @field_validator("description")
    @classmethod
    def _description_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("description: must not be blank")
        return cleaned


class StaffEntry(BaseModel):
    """One staff member: role, projects, active period."""

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    projects: list[str] = Field(min_length=1)
    active_from: date
    active_until: date | None = None

    @field_validator("projects")
    @classmethod
    def _projects_clean(cls, value: list[str]) -> list[str]:
        return _nonempty_unique(value, "projects")

    @model_validator(mode="after")
    def _period_ordered(self) -> "StaffEntry":
        if self.active_until is not None and self.active_until < self.active_from:
            raise ValueError("active_until: must not precede active_from")
        return self


class StaffRoster(BaseModel):
    roster: list[StaffEntry] = Field(default_factory=list)


class Catalog(BaseModel):
    """A validated catalog: index order, cards by name, staff."""

    version: int
    entries: list[IndexEntry] = Field(min_length=1)
    agents: dict[str, AgentCard]
    staff: StaffRoster

    @field_validator("version")
    @classmethod
    def _version_known(cls, value: int) -> int:
        if value != CATALOG_VERSION:
            raise ValueError(
                f"version: this loader reads {CATALOG_VERSION}, got {value}")
        return value


def catalog_root(explicit: str | Path | None = None) -> Path:
    """Catalog directory: ``catalog/`` at the repo root unless overridden."""
    if explicit is not None:
        return Path(explicit)
    return Path(__file__).resolve().parent.parent / "catalog"


def _read_yaml(path: Path) -> tuple[Any, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"unreadable ({exc})"
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:
        return None, f"invalid YAML ({exc})"


def profiles_root(explicit: str | Path | None = None) -> Path:
    """Role profiles directory (``profiles/`` under the catalog root)."""
    return catalog_root(explicit) / "profiles"


def load_role_profile(
    name: str, root: str | Path | None = None,
) -> RoleProfile:
    """Load one role profile by name. Fail-closed, no partial profile."""
    base = catalog_root(root)
    cleaned = (name or "").strip()
    if not cleaned:
        raise CatalogError("invalid role profile name: empty")
    candidate = (base / "profiles" / f"{cleaned}.yaml").resolve()
    base_resolved = base.resolve()
    if base_resolved not in candidate.parents:
        raise CatalogError(
            f"invalid role profile name {cleaned!r}: escapes the catalog")
    data, problem = _read_yaml(candidate)
    if problem is not None:
        raise CatalogError(f"profiles/{cleaned}.yaml: {problem}")
    if not isinstance(data, dict):
        raise CatalogError(
            f"profiles/{cleaned}.yaml: top level must be a mapping")
    try:
        profile = RoleProfile.model_validate(data)
    except ValidationError as exc:
        raise CatalogError(
            f"profiles/{cleaned}.yaml: {_format_pydantic(exc)}") from exc
    if profile.name != cleaned:
        raise CatalogError(
            f"profiles/{cleaned}.yaml: profile name {profile.name!r} must "
            f"match the file name {cleaned!r}")
    return profile


def _format_pydantic(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(bit) for bit in error["loc"])
        prefix = f"{loc}: " if loc else ""
        parts.append(f"{prefix}{error['msg']}")
    return "; ".join(parts)


def _read_frontmatter(skill_file: Path) -> tuple[dict[str, Any], str | None]:
    """Parse the YAML frontmatter of one SKILL.md (error text or None)."""
    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, f"unreadable ({exc})"
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, "missing frontmatter (SKILL.md must open with ---)"
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1)
         if line.strip() == "---"),
        None)
    if closing is None:
        return {}, "missing frontmatter (no closing ---)"
    try:
        data = yaml.safe_load("".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        return {}, f"invalid frontmatter YAML ({exc})"
    if not isinstance(data, dict):
        return {}, "frontmatter must be a mapping"
    return data, None


def parse_role_skill(path: str | Path, role: str) -> RoleSkill:
    """Parse one ``SKILL.md`` with its OpenCEO header. Fail-closed."""
    skill_file = Path(path)
    header, problem = _read_frontmatter(skill_file)
    if problem is not None:
        raise CatalogError(f"{skill_file}: {problem}")
    try:
        skill = RoleSkill.model_validate(
            {"role": role, "source": str(skill_file.parent), **header})
    except ValidationError as exc:
        raise CatalogError(
            f"{skill_file}: {_format_pydantic(exc)}") from exc
    if skill.name != skill_file.parent.name:
        raise CatalogError(
            f"{skill_file}: skill name {skill.name!r} must match "
            f"the directory {skill_file.parent.name!r}")
    return skill


def _skill_script_problems(skill_dir: Path) -> list[str]:
    """Every script needs its sibling test (mirror of the vault example).

    Each ``scripts/**/*.py`` that is not itself a test requires a
    ``test_<stem>.py`` somewhere under the skill dir.
    """
    scripts = skill_dir / "scripts"
    if not scripts.is_dir():
        return []
    tests = {path.name for path in skill_dir.rglob("test_*.py")
             if path.is_file()}
    problems = []
    for script in sorted(scripts.rglob("*.py")):
        if not script.is_file() or script.name.startswith("test_"):
            continue
        want = f"test_{script.stem}.py"
        if want not in tests:
            problems.append(
                f"{script}: script without its test (add {want} "
                "under the skill dir)")
    return problems


def load_role_skills(role: str, skills_dir: str,
                     repo_root: str | Path | None = None) -> list[RoleSkill]:
    """Load one role's skills, sorted by name. Fail-closed, all named.

    A missing role dir means no skills yet, never an error: only real
    source dirs (never stray files or symlinks) are read.
    """
    root = Path(repo_root) if repo_root is not None \
        else catalog_root().parent
    target = root / skills_dir
    if not target.exists():
        return []
    if not target.is_dir():
        raise CatalogError(f"{target}: skills_dir is not a directory")
    errors: list[str] = []
    found: list[RoleSkill] = []
    for entry in sorted(target.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(".") or entry.is_symlink() \
                or not entry.is_dir():
            continue
        try:
            found.append(parse_role_skill(entry / "SKILL.md", role))
        except CatalogError as exc:
            errors.append(str(exc))
            continue
        errors.extend(_skill_script_problems(entry))
    if errors:
        raise CatalogError(
            f"invalid role skills for {role}:\n"
            + "\n".join(f"  {line}" for line in errors))
    return sorted(found, key=lambda skill: skill.name)


def load_all_role_skills(
    catalog: Catalog,
    repo_root: str | Path | None = None,
) -> dict[str, list[RoleSkill]]:
    """Every AI agent's on-disk skills (humans are exempt)."""
    root = Path(repo_root) if repo_root is not None \
        else catalog_root().parent
    return {name: load_role_skills(name, card.skills_dir, root)
            for name, card in catalog.agents.items()
            if card.type is AgentType.AI}


def check_skill_names_unique(
    by_agent: Mapping[str, Collection[RoleSkill]],
) -> list[str]:
    """Skill names share one flat exposure namespace: no duplicates."""
    seen: dict[str, str] = {}
    problems: list[str] = []
    for agent in sorted(by_agent):
        for skill in by_agent[agent]:
            owner = seen.get(skill.name)
            if owner is None:
                seen[skill.name] = agent
            else:
                problems.append(
                    f"skill {skill.name!r} is owned by {owner!r} and "
                    f"{agent!r}: skill names share the flat "
                    f"{PROJECT_SKILLS_DIR}/ exposure namespace")
    return problems


def check_skill_links(
    by_agent: Mapping[str, Collection[RoleSkill]],
    repo_root: str | Path | None = None,
) -> list[str]:
    """Each role skill is exposed once via ``.codex/skills/``.

    The symlink is the exposure, ``skills/<role>/`` stays the single
    source. Real directories there (legacy ``notion-api-local``) are not
    role skills and stay untouched.
    """
    root = Path(repo_root) if repo_root is not None \
        else catalog_root().parent
    exposed = root / PROJECT_SKILLS_DIR
    sources = {skill.name: os.path.realpath(skill.source)
               for items in by_agent.values() for skill in items}
    problems: list[str] = []
    for name in sorted(sources):
        link = exposed / name
        if not os.path.lexists(link):
            problems.append(
                f"{PROJECT_SKILLS_DIR}/{name}: missing exposure symlink "
                f"to {sources[name]}")
        elif not link.is_symlink() or os.path.realpath(link) != sources[name]:
            problems.append(
                f"{PROJECT_SKILLS_DIR}/{name}: must be a symlink to "
                f"{sources[name]}")
    if exposed.is_dir():
        for entry in sorted(exposed.iterdir(), key=lambda item: item.name):
            if entry.name.startswith(".") or not entry.is_symlink():
                continue
            if os.path.realpath(entry) not in set(sources.values()):
                problems.append(
                    f"{PROJECT_SKILLS_DIR}/{entry.name}: symlink points "
                    "outside the role skills")
    return problems


def sync_skill_links(
    by_agent: Mapping[str, Collection[RoleSkill]],
    repo_root: str | Path | None = None,
) -> list[str]:
    """Create missing exposure symlinks; return what was created.

    The exposure dir is local runtime state (``.codex/`` is gitignored),
    so a fresh clone has no links: run this after adding a skill (the
    crystallization save step calls it, TASK-2026-00210). Only missing
    links are created, with repo-relative targets; anything already
    there — right, dangling or foreign — is left for
    :func:`check_skill_links` to judge, never silently replaced.
    """
    root = Path(repo_root) if repo_root is not None \
        else catalog_root().parent
    exposed = root / PROJECT_SKILLS_DIR
    created: list[str] = []
    for agent in sorted(by_agent):
        for skill in sorted(by_agent[agent], key=lambda item: item.name):
            link = exposed / skill.name
            if os.path.lexists(link):
                continue
            src = Path(skill.source)
            if not src.is_absolute():
                src = root / src
            exposed.mkdir(parents=True, exist_ok=True)
            link.symlink_to(os.path.relpath(os.path.realpath(src),
                                             os.path.realpath(exposed)))
            created.append(f"{PROJECT_SKILLS_DIR}/{skill.name}")
    return created


def skills_index_for_planner(
    by_agent: Mapping[str, Collection[RoleSkill]],
) -> str:
    """Name + description + status per skill, for the planner prompt.

    Agents without skills are omitted; no skills at all renders empty
    (the prompt carries no block then).
    """
    lines = []
    for agent in sorted(by_agent):
        for skill in sorted(by_agent[agent], key=lambda item: item.name):
            lines.append(f"- {agent}: {skill.name} — "
                         f"{skill.description} ({skill.status})")
    return "\n".join(lines)


def suggestable_skills(
    catalog: Catalog,
    by_agent: Mapping[str, Collection[RoleSkill]],
) -> dict[str, list[str]]:
    """Skills the planner may suggest per agent: on-disk for AI agents
    (active or draft), the card skills for the owner."""
    mapping: dict[str, list[str]] = {}
    for name, card in catalog.agents.items():
        if card.type is AgentType.AI:
            mapping[name] = [skill.name for skill in by_agent.get(name, ())]
        elif name == "owner":
            mapping[name] = list(card.skills)
    return mapping


def load_catalog(root: str | Path | None = None) -> Catalog:
    """Load and validate the whole catalog tree.

    Raises :class:`CatalogError` listing every problem found: no partial
    catalog is ever returned.
    """
    base = catalog_root(root)
    errors: list[str] = []

    def rel(path: Path) -> str:
        try:
            return str(path.relative_to(base))
        except ValueError:
            return str(path)

    index_path = base / "index.yaml"
    index_data, index_problem = _read_yaml(index_path)
    entries: list[IndexEntry] = []
    if index_problem is not None:
        errors.append(f"{rel(index_path)}: {index_problem}")
        index_data = None
    elif not isinstance(index_data, dict):
        errors.append(f"{rel(index_path)}: top level must be a mapping")
        index_data = None
    else:
        version = index_data.get("version")
        if version != CATALOG_VERSION:
            errors.append(
                f"{rel(index_path)}: version: this loader reads "
                f"{CATALOG_VERSION}, got {version!r}")
        raw_entries = index_data.get("agents")
        if not isinstance(raw_entries, list) or not raw_entries:
            errors.append(f"{rel(index_path)}: agents: needs at least one entry")
        else:
            seen: set[str] = set()
            for position, raw in enumerate(raw_entries):
                where = f"{rel(index_path)} agents[{position}]"
                if not isinstance(raw, dict):
                    errors.append(f"{where}: must be a mapping")
                    continue
                try:
                    entry = IndexEntry.model_validate(raw)
                except ValidationError as exc:
                    errors.append(f"{where}: {_format_pydantic(exc)}")
                    continue
                if entry.name in seen:
                    errors.append(f"{where}: duplicate index entry "
                                  f"{entry.name!r}")
                    continue
                seen.add(entry.name)
                entries.append(entry)

    agents: dict[str, AgentCard] = {}
    referenced: set[str] = set()
    base_resolved = base.resolve()
    for entry in entries:
        card_path = (base / entry.card).resolve()
        if base_resolved not in card_path.parents:
            errors.append(f"{rel(base / entry.card)}: escapes the catalog")
            continue
        if not card_path.is_file():
            errors.append(f"{rel(base / entry.card)}: card file is missing")
            continue
        referenced.add(str(card_path.relative_to(base_resolved)))
        card_data, card_problem = _read_yaml(card_path)
        if card_problem is not None:
            errors.append(f"{rel(card_path)}: {card_problem}")
            continue
        if not isinstance(card_data, dict):
            errors.append(f"{rel(card_path)}: top level must be a mapping")
            continue
        try:
            card = AgentCard.model_validate(card_data)
        except ValidationError as exc:
            errors.append(f"{rel(card_path)}: {_format_pydantic(exc)}")
            continue
        if card.name != entry.name or card_path.stem != entry.name:
            errors.append(
                f"{rel(card_path)}: card name {card.name!r} must match "
                f"index entry {entry.name!r} and the file name")
            continue
        for tool in card.boundary_tools:
            if tool not in MCP_BOUNDARY_TOOLS:
                errors.append(
                    f"{rel(card_path)}: boundary_tools: {tool!r} is not "
                    "an MCP boundary tool")
        agents[entry.name] = card

    agents_dir = base / "agents"
    if agents_dir.is_dir():
        for orphan in sorted(agents_dir.glob("*.yaml")):
            if str(orphan.relative_to(base_resolved)) not in referenced:
                errors.append(f"{rel(orphan)}: card file not listed in "
                              "index.yaml")

    profiles_dir = base / "profiles"
    parsed_profiles: dict[str, RoleProfile] = {}
    if profiles_dir.is_dir():
        for profile_file in sorted(profiles_dir.glob("*.yaml")):
            data, problem = _read_yaml(profile_file)
            if problem is not None:
                errors.append(f"{rel(profile_file)}: {problem}")
                continue
            if not isinstance(data, dict):
                errors.append(
                    f"{rel(profile_file)}: top level must be a mapping")
                continue
            try:
                parsed = RoleProfile.model_validate(data)
            except ValidationError as exc:
                errors.append(
                    f"{rel(profile_file)}: {_format_pydantic(exc)}")
                continue
            if parsed.name != profile_file.stem:
                errors.append(
                    f"{rel(profile_file)}: profile name {parsed.name!r} "
                    f"must match the file name {profile_file.stem!r}")
                continue
            parsed_profiles[rel(profile_file)] = parsed
    for entry in entries:
        card = agents.get(entry.name)
        if card is None or card.profile is None:
            continue
        pointed = card.profile.strip()
        if (not pointed or pointed.startswith("/")
                or ".." in Path(pointed).parts
                or not pointed.endswith(".yaml")):
            errors.append(
                f"agents/{entry.name}.yaml: profile: must be a .yaml path "
                "inside the catalog")
            continue
        target = (base / pointed).resolve()
        if base_resolved not in target.parents or not target.is_file():
            errors.append(
                f"agents/{entry.name}.yaml: profile: {pointed!r} is missing "
                "from the catalog")
            continue
        data, problem = _read_yaml(target)
        if problem is not None or not isinstance(data, dict):
            errors.append(f"{rel(target)}: unreadable profile")
            continue
        try:
            linked = RoleProfile.model_validate(data)
        except ValidationError as exc:
            errors.append(f"{rel(target)}: {_format_pydantic(exc)}")
            continue
        if linked.name != entry.name:
            errors.append(
                f"{rel(target)}: profile name {linked.name!r} must match "
                f"the card {entry.name!r}")
        elif linked.engine != card.engine:
            errors.append(
                f"{rel(target)}: engine {linked.engine!r} disagrees with "
                f"the card engine {card.engine!r}")
        else:
            foreign = [skill for skill in linked.skills
                       if skill not in card.skills
                       and skill not in ENGINE_SKILLS]
            if foreign:
                errors.append(
                    f"{rel(target)}: skills {foreign!r} are not on the "
                    f"card {entry.name!r} or a muse engine skill")
            uncovered = [skill for skill in card.skills
                         if skill not in linked.skills]
            if uncovered:
                errors.append(
                    f"{rel(target)}: card skills {uncovered!r} are not "
                    f"loaded by the profile: the run would suggest them "
                    "but never see them")
    linked_paths = {card.profile for card in agents.values()
                    if card.profile}
    for relpath in sorted(parsed_profiles):
        if relpath in linked_paths:
            continue
        outsiders = [skill for skill in parsed_profiles[relpath].skills
                     if skill not in ENGINE_SKILLS]
        if outsiders:
            errors.append(
                f"{relpath}: skills {outsiders!r} are not a muse engine "
                "skill: profiles without a card load engine skills only")

    repo = base.parent
    role_skills: dict[str, list[RoleSkill]] = {}
    for entry in entries:
        card = agents.get(entry.name)
        if card is None or card.type is not AgentType.AI:
            continue
        try:
            found = load_role_skills(entry.name, card.skills_dir, repo)
        except CatalogError as exc:
            errors.append(f"skills for {entry.name}: {exc}")
            continue
        role_skills[entry.name] = found
        on_disk = {skill.name for skill in found}
        for missing in sorted(set(card.skills) - on_disk):
            errors.append(
                f"agents/{entry.name}.yaml: skills: {missing!r} is not "
                f"on disk under {card.skills_dir}")
        for unlisted in sorted(on_disk - set(card.skills)):
            errors.append(
                f"{card.skills_dir}/{unlisted}: skill is on disk but not "
                f"on the card {entry.name!r}")
    errors.extend(check_skill_names_unique(role_skills))
    errors.extend(check_skill_links(role_skills, repo))

    staff_path = base / "staff.yaml"
    staff = StaffRoster()
    staff_data, staff_problem = _read_yaml(staff_path)
    if staff_problem is not None:
        errors.append(f"{rel(staff_path)}: {staff_problem}")
    elif not isinstance(staff_data, dict):
        errors.append(f"{rel(staff_path)}: top level must be a mapping")
    else:
        try:
            staff = StaffRoster.model_validate(staff_data)
        except ValidationError as exc:
            errors.append(f"{rel(staff_path)}: {_format_pydantic(exc)}")

    if errors:
        raise CatalogError(
            f"invalid agent catalog at {base}:\n"
            + "\n".join(f"  {line}" for line in errors))

    version_value = index_data.get("version")
    return Catalog(version=version_value, entries=entries, agents=agents,
                   staff=staff)


@dataclass(frozen=True)
class CatalogStatus:
    """Health of the catalog tree (:func:`check_catalog`)."""

    ok: bool
    path: str
    agents: int
    sha: str
    problem: str = ""


def check_catalog(root: str | Path | None = None) -> CatalogStatus:
    """Is the catalog tree valid? Never raises, never loads partial state."""
    base = catalog_root(root)
    try:
        catalog = load_catalog(base)
    except CatalogError as exc:
        return CatalogStatus(False, str(base), 0, "", str(exc))
    except Exception as exc:  # fail closed on anything unexpected too
        return CatalogStatus(False, str(base), 0, "",
                             f"unexpected loader failure ({exc})")
    return CatalogStatus(True, str(base), len(catalog.agents),
                         catalog_fingerprint(catalog))


def catalog_fingerprint(catalog: Catalog) -> str:
    """Short SHA-256 (12 hex) of the canonical catalog content.

    Planning runs store it so a decision can be traced to the catalog the
    planner actually saw. Plain hashing: no model call, never in the prompt.
    """
    canonical = json.dumps(catalog.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def short_index(catalog: Catalog) -> str:
    """Render the always-loaded short index: ``name: summary`` per line."""
    return "\n".join(f"{entry.name}: {entry.summary}"
                     for entry in catalog.entries)


#: Disabled names that stay live in the legacy enum until their removal
#: ticket lands (TASK-2026-00209). The consistency check keeps them.
LEGACY_LIVE_DISABLED = ("pm", "marketing", "sales", "media")

#: Reason recorded when a staff member is outside their active period.
STAFF_INACTIVE_REASON = "not in active period"


def is_staff_active(entry: StaffEntry, today: date | None = None) -> bool:
    """Is the staff member active on ``today`` (default: current date)?"""
    day = today or date.today()
    if entry.active_from > day:
        return False
    return entry.active_until is None or entry.active_until >= day


def planner_registry_view(
    catalog: Catalog, today: date | None = None,
) -> dict[str, dict[str, Any]]:
    """Planner registry derived from the catalog (TASK-2026-00198).

    AI agents plus humans: the owner and the staff roster addressed by
    name. Shape kept (name → {"enabled": bool, "reason"?: str}) for the
    planner prompt and ``validate_planner_result``.
    """
    view: dict[str, dict[str, Any]] = {}
    for name, card in catalog.agents.items():
        if card.type.value != "ai" and name != "owner":
            continue
        if card.enabled:
            view[name] = {"enabled": True}
        else:
            view[name] = {"enabled": False,
                          "reason": card.disabled_reason or "disabled"}
    for entry in catalog.staff.roster:
        if is_staff_active(entry, today):
            view[entry.name] = {"enabled": True}
        else:
            view[entry.name] = {"enabled": False,
                                "reason": STAFF_INACTIVE_REASON}
    return view


def find_consumer_mismatches(
    agent_names: Collection[str],
    alias_values: Collection[str],
    registry: Mapping[str, Mapping[str, Any]],
    wired: Collection[str],
    catalog: Catalog,
) -> list[str]:
    """Mismatches between the four agent views and the catalog.

    Empty means every view matches. Pure: views arrive as arguments so
    this module never imports its consumers (no cycles).

    R1: every enum name except ``ceo`` is a catalog agent.
    R2: every router alias value is a catalog agent.
    R3: the planner registry is exactly the catalog AI agents plus the
    humans (owner and staff names), with the catalog enabled flags and
    reasons.
    R4: every wired graph agent is a catalog agent.
    R5: the disabled-but-live legacy names stay in the enum until removal.
    """
    problems: list[str] = []
    names = set(catalog.agents)
    expected = planner_registry_view(catalog)

    for name in sorted(set(agent_names)):
        if name != "ceo" and name not in names:
            problems.append(
                f"R1: enum agent {name!r} is not in the catalog")
    for value in sorted(set(alias_values)):
        if value not in names:
            problems.append(
                f"R2: router alias value {value!r} is not in the catalog")
    registry_names = set(registry)
    for name in sorted(registry_names - set(expected)):
        problems.append(
            f"R3: planner registry agent {name!r} is not a catalog agent "
            "or staff name")
    for name in sorted(set(expected) - registry_names):
        problems.append(
            f"R3: catalog agent {name!r} is missing from the planner "
            "registry")
    for name in sorted(registry_names & set(expected)):
        spec = registry[name]
        want = expected[name]
        enabled = bool(spec.get("enabled", False)) if isinstance(spec, Mapping) \
            else False
        if enabled != want["enabled"]:
            problems.append(
                f"R3: planner registry enabled={enabled!r} for {name!r} "
                f"disagrees with the catalog ({want['enabled']!r})")
        elif not want["enabled"] and str(spec.get("reason") or "") != str(
                want.get("reason") or ""):
            problems.append(
                f"R3: planner registry reason for {name!r} disagrees with "
                "the catalog")
    for name in sorted(set(wired)):
        if name not in names:
            problems.append(
                f"R4: wired graph agent {name!r} is not in the catalog")
    enum_names = set(agent_names)
    for name in LEGACY_LIVE_DISABLED:
        if name not in enum_names:
            problems.append(
                f"R5: disabled-but-live legacy agent {name!r} left the enum "
                "before its removal (TASK-2026-00209)")
    return problems
