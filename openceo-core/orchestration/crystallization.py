"""Internal crystallization save step: drafts become skills (TASK-2026-00210).

On ticket close the agent judges recurrence and leaves drafts under
``skill_drafts/`` in its ticket folder — it cannot write ``skills/``
directly (the repo mounts read-only in every fenced run). This module is
the deterministic save gate the orchestrator runs: no tokens, no ticket
outcome changes. A draft that fails any check is discarded with a
recorded reason; the ticket result never changes.

Gate rules (code, not model obedience):

* only the agent's own role: changes land in ``skills/<role>/`` plus the
  role card and profile skill lists, never anywhere else;
* everything new enters as ``borrador``;
* a draft promotes to ``activa`` only when another ticket reuses it
  and closes with an OpenCEO-verified result (D9);
* a draft declaring external content promotes only with an
  independent review approval (D9);
* at most 10 drafts per role; the oldest goes (header date, ticket,
  name — no mtime dependence);
* every touched skill passes the normative loader plus ``muse skills
  validate``; changed scripts pass their tests inside the fence.

The result commits in a dedicated worktree over origin/main — never in
the Owner checkout — with the ticket number, and pushes at once (fetch +
rebase + retry on conflict), serialized by a blocking flock so
concurrent closes never interleave.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import yaml
from pydantic import ValidationError

from catalog import (
    ENGINE_SKILLS,
    TICKET_PATTERN,
    AgentCard,
    AgentType,
    CatalogError,
    RoleProfile,
    RoleSkill,
    check_skill_names_unique,
    load_role_skills,
    parse_role_skill,
)
from orchestration.agent_sandbox import plan_agent, ticket_dir
from orchestration.role_muse import badge_ticket_for_task
from orchestration.skill_metrics import is_full_rewrite
from research.runtimes.base import IsolationUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "CRYSTALLIZATION_PROMPT",
    "DRAFTS_DIRNAME",
    "LOCK_FILENAME",
    "MAX_DRAFTS_PER_ROLE",
    "MAX_DRAFT_BYTES",
    "MAX_DRAFT_FILES",
    "SaveOutcome",
    "SaveStepConfig",
    "collect_drafts",
    "fenced_skill_tests",
    "has_drafts",
    "mirror_ticket",
    "muse_validate_skill",
    "rewrite_skills_list",
    "run_save_step",
    "script_dependencies",
]

#: Ticket-folder dir where agents leave skill drafts for the save step.
DRAFTS_DIRNAME = "skill_drafts"

#: Hard cap of drafts per role; the oldest is deleted past it.
MAX_DRAFTS_PER_ROLE = 10

#: Per-draft size and file caps (agents must not fill the disk).
MAX_DRAFT_BYTES = 128_000
MAX_DRAFT_FILES = 64

#: Blocking lock serializing concurrent save steps (``.runtime/``).
LOCK_FILENAME = "crystallization.lock"

#: Git identity for crystallization commits (deterministic, no ambient
#: config dependence; signing policy is never overridden).
_GIT_USER_NAME = "openceo-crystallization"
_GIT_USER_EMAIL = "openceo@jardal.app"

_GIT_TIMEOUT_S = 120
_VALIDATE_TIMEOUT_S = 120
_SKILL_TEST_TIMEOUT_S = 180

#: Agent filename guard (defense only; catalog validators own semantics).
_AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")

CRYSTALLIZATION_PROMPT = f"""\
When the ticket work is done, judge whether you ran a recurrent process.
If you did and no skill covers it, leave a draft under `{DRAFTS_DIRNAME}/<skill-name>/` \
in this folder (your ticket folder): `SKILL.md` opens with `name` (== the dir, \
lowercase letters/digits/`-`) and a one-line `description`, then the recipe; add \
`scripts/` only where exactness or safe repetition is needed (every script ships \
its `test_<stem>.py` next to it), `references/` for stable context, and \
`workflow.yaml` for the exact part. A script that needs packages outside the \
standard library declares them in a PEP 723 `# /// script` header \
(`dependencies = [...]`) and SKILL.md runs it with `uv run <script>`. If a skill already covers it, write the \
improved skill dir with small evidenced changes under `{DRAFTS_DIRNAME}/<same-name>/` \
instead (never a full rewrite); two drafts \
of the same thing fuse into one. Never write outside `{DRAFTS_DIRNAME}/` for this: \
the orchestrator promotes drafts to skills — everything new enters as a draft \
(`borrador`), and a draft becomes a skill (`activa`) only when another ticket \
reuses it and closes with a verified result. A failed close may leave a draft \
but never promotes it. A draft with external content (declare \
`external: true` in the SKILL.md header) needs an independent \
review approval before promotion.\
"""


@dataclass(frozen=True)
class CollectedDraft:
    """One structurally sound draft, ready for the save gate."""

    name: str
    source: Path
    description: str


@dataclass(frozen=True)
class SaveStepConfig:
    """What one save run needs. ``validate``/``run_skill_tests`` default
    to the real ``muse`` gate and the fenced pytest run; tests inject
    fakes. ``pre_push`` is a test-only seam for the push race.
    ``verified`` is True only when the closing ticket ended with an
    OpenCEO-verified result (D9); ``external_approved`` names the
    drafts whose external content passed independent review."""

    repo: str | Path
    agent: str
    ticket: str
    drafts_dir: str | Path
    verified: bool = False
    external_approved: tuple[str, ...] = ()
    lock_path: str | Path | None = None
    remote: str = "origin"
    base_branch: str = "main"
    push_retries: int = 2
    today: date | None = None
    muse_bin: str | None = None
    validate: Callable[[Path], tuple[bool, str]] | None = None
    run_skill_tests: Callable[..., tuple[bool, str]] | None = None
    pre_push: Callable[[], None] | None = None


@dataclass
class SaveOutcome:
    """What one save run did. ``comment`` is the Jardal ERP acta (None when
    there is nothing to report); ``deferred`` means an infrastructure
    failure kept the drafts for the next close."""

    skipped: str | None = None
    created: list[str] = field(default_factory=list)
    promoted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    awaiting_review: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    discarded: dict[str, str] = field(default_factory=dict)
    deleted_oldest: list[str] = field(default_factory=list)
    deferred: str | None = None
    commit: str | None = None
    comment: str | None = None


class _Defer(Exception):
    """Infrastructure failure: keep every draft for the next close."""


def mirror_ticket(task: Mapping[str, Any]) -> str | None:
    """Jardal ERP ticket name stamping headers and commits, None to skip."""
    name = badge_ticket_for_task(task)
    if name is None or re.match(TICKET_PATTERN, name) is None:
        return None
    return name


def has_drafts(task_id: str, *,
               base_dir: str | Path | None = None) -> bool:
    """True when the ticket folder holds unprocessed drafts.

    Read-only: never creates the folder (most closes have no drafts).
    Only entries the collector would consider count — a stray file,
    symlink or dotdir never re-triggers the gate on every close.
    """
    try:
        target = ticket_dir(task_id, base_dir=_base(base_dir)) \
            / DRAFTS_DIRNAME
    except ValueError:
        return False
    try:
        if not target.is_dir():
            return False
        return any(
            not entry.name.startswith(".")
            and not entry.is_symlink()
            and entry.is_dir()
            for entry in target.iterdir())
    except OSError:
        return False


def _base(base_dir: str | Path | None) -> Path | None:
    return Path(base_dir) if base_dir is not None else None


def collect_drafts(agent: str, drafts_dir: str | Path
                   ) -> tuple[list[CollectedDraft], dict[str, str]]:
    """Read drafts leniently: a valid name plus a description suffices.

    The gate stamps status/ticket/date itself, so the agent header is
    never trusted; symlinks, caches and oversize trees are refused.
    Returns ``(drafts, discarded)`` with per-draft reasons.
    """
    target = Path(drafts_dir)
    if not target.is_dir():
        return [], {}
    drafts: list[CollectedDraft] = []
    discarded: dict[str, str] = {}
    try:
        entries = sorted(target.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        logger.warning("crystallization: drafts unreadable at %s: %s",
                       target, exc)
        return [], {}
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink() \
                or not entry.is_dir():
            continue
        problem: str | None = None
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            problem = "SKILL.md is missing"
        else:
            files = _copyable_files(entry)
            size = sum(_file_size(path) for path in files)
            if len(files) > MAX_DRAFT_FILES or size > MAX_DRAFT_BYTES:
                problem = (f"too large ({len(files)} files, {size} "
                           f"bytes): over the draft cap")
        description: str | None = None
        if problem is None:
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError as exc:
                problem = f"SKILL.md is unreadable ({exc})"
            else:
                split = _split_frontmatter(text)
                if isinstance(split, str):
                    problem = f"SKILL.md: {split}"
                else:
                    header, _body = split
                    if header.get("name") != entry.name \
                            or not _name_ok(agent, entry.name):
                        problem = (f"invalid skill name {entry.name!r}: "
                                   "lowercase letters, digits, '-'/'_'")
                    elif not isinstance(header.get("description"), str) \
                            or not str(header["description"]).strip():
                        problem = "description is missing or blank"
                    else:
                        description = str(header["description"]).strip()
        if problem is not None or description is None:
            discarded[entry.name] = problem or "unreadable draft"
        else:
            drafts.append(CollectedDraft(name=entry.name, source=entry,
                                         description=description))
    return drafts, discarded


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _copyable_files(root: Path) -> list[Path]:
    """Draft payload: real files only, no caches, objects or escapes."""
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if "__pycache__" in path.parts or ".git" in path.parts:
            continue
        if path.suffix == ".pyc":
            continue
        out.append(path)
    return out


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str] | str:
    """``(header, body)`` or an error string.

    Same shape rules as the normative ``catalog._read_frontmatter``;
    the gate only reads name/description and stamps the rest.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return "missing frontmatter (SKILL.md must open with ---)"
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1)
         if line.strip() == "---"),
        None)
    if closing is None:
        return "missing frontmatter (no closing ---)"
    try:
        data = yaml.safe_load("".join(lines[1:closing]))
    except yaml.YAMLError as exc:
        return f"invalid frontmatter YAML ({exc})"
    if not isinstance(data, dict):
        return "frontmatter must be a mapping"
    return data, "".join(lines[closing + 1:])


def _name_ok(role: str, name: str) -> bool:
    """Skill-name rules, judged by the normative model validators."""
    try:
        RoleSkill.model_validate({
            "role": role, "name": name, "description": "ok",
            "status": "borrador", "ticket": "TASK-2026-00001",
            "date": "2026-10-12", "source": "draft"})
    except ValidationError:
        return False
    return True


def rewrite_skills_list(path: str | Path,
                        names: list[str] | tuple[str, ...]) -> None:
    """Replace the top-level ``skills:`` value, preserving other lines.

    Empty renders flow (``skills: []``), non-empty a ``- `` block keeping
    the detected item indent (two spaces when the key is new or flow);
    a missing key inserts after ``skills_dir:``/``rules:``.
    """
    target = Path(path)
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    found = next((index for index, line in enumerate(lines)
                  if line.startswith("skills:")), None)
    if found is None:
        anchor = next(
            (index for index, line in enumerate(lines)
             if line.startswith("skills_dir:") or line.startswith("rules:")),
            None)
        block = _skills_block(list(names), "  ")
        if anchor is None:
            lines.extend(block)
        else:
            lines[anchor + 1:anchor + 1] = block
        target.write_text("".join(lines), encoding="utf-8")
        return
    rest = lines[found][len("skills:"):]
    end = found + 1
    indent: str | None = None
    if rest.strip() == "":
        while end < len(lines):
            line = lines[end]
            if line.strip() == "":
                end += 1
                continue
            match = re.match(r"^( *)- ", line)
            if match is None:
                break
            if indent is None:
                indent = match.group(1)
            end += 1
    lines[found:end] = _skills_block(
        list(names), indent if indent is not None else "  ")
    target.write_text("".join(lines), encoding="utf-8")


def _skills_block(names: list[str], indent: str) -> list[str]:
    if not names:
        return ["skills: []\n"]
    return ["skills:\n", *(f"{indent}- {name}\n" for name in names)]


def run_save_step(config: SaveStepConfig) -> SaveOutcome:
    """Run one crystallization save: drafts in, skills commit out.

    Blocking and synchronous (callers offload to a thread): fetch, a
    dedicated worktree over the base ref, per-draft validation, cap,
    card/profile surgery, commit and push with rebase retries — all
    under a blocking flock. Raises ``ValueError`` on a bad ticket
    stamp; every other failure lands in the outcome (discarded,
    skipped or deferred with the drafts kept).
    """
    if re.match(TICKET_PATTERN, config.ticket) is None:
        raise ValueError(
            f"crystallization needs a TASK ticket stamp, got "
            f"{config.ticket!r}")
    outcome = SaveOutcome()
    drafts_dir = Path(config.drafts_dir)
    drafts, collect_errors = collect_drafts(config.agent, drafts_dir)
    outcome.discarded.update(collect_errors)
    if not drafts:
        if collect_errors:
            _consume(drafts_dir, list(collect_errors))
            outcome.comment = _comment(config.ticket, outcome)
        else:
            outcome.skipped = "no drafts"
        return outcome
    repo = Path(config.repo)
    lock_path = Path(config.lock_path) if config.lock_path is not None \
        else repo / ".runtime" / LOCK_FILENAME
    with _locked(lock_path):
        try:
            _fetch(config, repo)
            worktree = _make_worktree(config, repo)
        except _Defer as exc:
            return _deferred(outcome, config.ticket, str(exc))
        try:
            try:
                _apply(config, outcome, drafts, worktree)
            except _Defer as exc:
                return _deferred(outcome, config.ticket, str(exc))
        finally:
            _remove_worktree(repo, worktree)
    handled = [draft.name for draft in drafts
               if draft.name in outcome.created
               or draft.name in outcome.promoted
               or draft.name in outcome.updated
               or draft.name in outcome.awaiting_review
               or draft.name in outcome.unchanged
               or draft.name in outcome.discarded]
    _consume(drafts_dir, handled + list(collect_errors))
    outcome.comment = _comment(config.ticket, outcome)
    return outcome


def _deferred(outcome: SaveOutcome, ticket: str,
              reason: str) -> SaveOutcome:
    """Infrastructure failure: nothing landed, drafts stay for retry."""
    outcome.created.clear()
    outcome.promoted.clear()
    outcome.updated.clear()
    outcome.awaiting_review.clear()
    outcome.unchanged.clear()
    outcome.deleted_oldest.clear()
    outcome.discarded.clear()
    outcome.deferred = reason
    outcome.comment = _comment(ticket, outcome)
    logger.warning("crystallization deferred for %s: %s", ticket, reason)
    return outcome


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Blocking flock: concurrent closes serialize, never interleave."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            timeout=_GIT_TIMEOUT_S, check=False)
    except FileNotFoundError as exc:
        raise _Defer("git is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise _Defer(f"git {' '.join(args)} timed out") from exc


def _fetch(config: SaveStepConfig, repo: Path) -> None:
    proc = _git(repo, "fetch", "--quiet", config.remote)
    if proc.returncode != 0:
        raise _Defer(f"fetch from {config.remote} failed: "
                      f"{proc.stderr.strip()[:200]}")


def _make_worktree(config: SaveStepConfig, repo: Path) -> Path:
    """Dedicated worktree over the base ref, outside every checkout."""
    target = Path(tempfile.mkdtemp(prefix="crystallize-"))
    proc = _git(repo, "worktree", "add", "--detach", str(target),
                f"{config.remote}/{config.base_branch}")
    if proc.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise _Defer(f"worktree over {config.remote}/"
                      f"{config.base_branch} failed: "
                      f"{proc.stderr.strip()[:200]}")
    return target


def _remove_worktree(repo: Path, worktree: Path) -> None:
    """Best-effort removal; a leftover never fails the close."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    shutil.rmtree(worktree, ignore_errors=True)


def _load_card(worktree: Path, agent: str) -> AgentCard | None:
    """One agent card from the worktree, None when unusable."""
    if not agent or _AGENT_NAME_RE.fullmatch(agent) is None:
        return None
    try:
        data = yaml.safe_load(
            (worktree / "catalog" / "agents" / f"{agent}.yaml")
            .read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        card = AgentCard.model_validate(data)
    except ValidationError:
        return None
    return card if card.name == agent else None


def _other_role_skill_names(worktree: Path, agent: str) -> dict[str, str]:
    """Skill name to owning role for every other AI role.

    Fail-closed: a sibling the gate cannot judge defers the save —
    uniqueness in the flat exposure namespace must be provable.
    """
    agents_dir = worktree / "catalog" / "agents"
    owned: dict[str, str] = {}
    for card_file in sorted(agents_dir.glob("*.yaml")):
        if card_file.stem == agent:
            continue
        try:
            data = yaml.safe_load(card_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise _Defer(f"role {card_file.stem} is unreadable: "
                          f"{exc}") from exc
        if not isinstance(data, dict):
            raise _Defer(f"role {card_file.stem} has no card mapping")
        try:
            card = AgentCard.model_validate(data)
        except ValidationError as exc:
            raise _Defer(f"role {card_file.stem} has an invalid card: "
                          f"{exc}") from exc
        if card.type is not AgentType.AI:
            continue
        try:
            skills = load_role_skills(card.name, card.skills_dir, worktree)
        except CatalogError as exc:
            raise _Defer(f"role {card.name} skills are red: "
                          f"{exc}") from exc
        for skill in skills:
            owned[skill.name] = card.name
    return owned


def _apply(config: SaveStepConfig, outcome: SaveOutcome,
           drafts: list[CollectedDraft], worktree: Path) -> None:
    card = _load_card(worktree, config.agent)
    if card is None or card.type is not AgentType.AI or not card.enabled:
        for draft in drafts:
            outcome.discarded[draft.name] = (
                f"agent {config.agent!r} is not an enabled AI agent")
        return
    try:
        current = {skill.name: skill for skill in load_role_skills(
            config.agent, card.skills_dir, worktree)}
    except CatalogError as exc:
        raise _Defer(f"role {config.agent} skills are red: {exc}") from exc
    owned = _other_role_skill_names(worktree, config.agent)
    today = config.today or date.today()
    for draft in drafts:
        owner = owned.get(draft.name)
        if owner is not None and draft.name not in current:
            outcome.discarded[draft.name] = (
                f"skill name is owned by role {owner}")
            continue
        _apply_draft(config, outcome, current, draft, card, worktree,
                     today)
    _enforce_cap(outcome, current, worktree, card.skills_dir)
    names = sorted(current)
    card_rel = f"catalog/agents/{config.agent}.yaml"
    rewrite_skills_list(worktree / card_rel, names)
    profile_rel: str | None = None
    if card.profile:
        profile_rel = _profile_rel(worktree, card.profile)
        if profile_rel is None:
            raise _Defer(f"profile {card.profile!r} escapes the catalog")
        profile_path = worktree / "catalog" / profile_rel
        try:
            data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            profile = RoleProfile.model_validate(data)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise _Defer(f"profile {card.profile!r} is unreadable: "
                          f"{exc}") from exc
        kept = [skill for skill in profile.skills
                if skill in ENGINE_SKILLS or skill in names]
        kept.extend(name for name in names if name not in kept)
        rewrite_skills_list(profile_path, kept)
    _verify_save(worktree, config.agent)
    scoped = [card.skills_dir, card_rel]
    if profile_rel:
        scoped.append(f"catalog/{profile_rel}")
    if not _tree_changed(worktree, *scoped):
        return
    _git(worktree, "add", "-A", "--", *scoped)
    _commit(config, outcome, worktree)
    if config.pre_push is not None:
        config.pre_push()
    _push(config, worktree)
    outcome.commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()


def _profile_rel(worktree: Path, pointed: str) -> str | None:
    """Profile path confined to the catalog, None on escape."""
    cleaned = pointed.strip()
    if not cleaned or cleaned.startswith("/") \
            or ".." in Path(cleaned).parts:
        return None
    base = (worktree / "catalog").resolve()
    target = (worktree / "catalog" / cleaned).resolve()
    if base not in target.parents and target != base:
        return None
    return cleaned


def _declares_external(header: Mapping[str, Any]) -> bool:
    """True when a draft header marks its content as external (D9).

    Agent decision: the marker is an explicit header declaration
    (``external: true`` or ``origin``/``source: external``), never a
    guessed body scan, so ordinary notes with links stay unaffected.
    """
    if header.get("external") is True:
        return True
    external = header.get("external")
    if isinstance(external, str) and external.strip().lower() == "true":
        return True
    for key in ("origin", "source"):
        value = header.get(key)
        if isinstance(value, str) and value.strip().lower() == "external":
            return True
    return False


def _read_header(skill_md: Path) -> dict[str, Any]:
    """Frontmatter of one SKILL.md, empty when unreadable."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return {}
    split = _split_frontmatter(text)
    return {} if isinstance(split, str) else dict(split[0])


def _draft_header(draft: CollectedDraft) -> dict[str, Any]:
    """Frontmatter of the incoming draft, empty when unreadable."""
    return _read_header(draft.source / "SKILL.md")


def _apply_draft(config: SaveStepConfig, outcome: SaveOutcome,
                 current: dict[str, Any], draft: CollectedDraft,
                 card: AgentCard, worktree: Path, today: date) -> None:
    existing = current.get(draft.name)
    skill_rel = f"{card.skills_dir}/{draft.name}"
    target = worktree / skill_rel
    stored_header = _read_header(target / "SKILL.md")
    stored_external = _declares_external(stored_header)
    incoming_external = _declares_external(_draft_header(draft))
    approved = draft.name in set(config.external_approved)
    needs_review = not approved and (stored_external or incoming_external)
    carried: dict[str, Any] = {}
    if stored_external and not incoming_external:
        for key in ("external", "origin", "source"):
            if key in stored_header:
                carried[key] = stored_header[key]
    if existing is not None and existing.status == "activa":
        try:
            old_text = (target / "SKILL.md").read_text(encoding="utf-8")
            new_text = (draft.source / "SKILL.md").read_text(
                encoding="utf-8")
        except OSError:
            old_text = new_text = ""
        if old_text and new_text and is_full_rewrite(old_text, new_text):
            outcome.discarded[draft.name] = (
                "full rewrite rejected: edit the active skill in small "
                "evidenced changes instead (D9)")
            return
    blocked_hold = False
    demoted_hold = False
    if existing is None:
        decision, status = "created", "borrador"
        keep_ticket, keep_day = config.ticket, today
    elif (existing.status == "borrador"
            and existing.ticket != config.ticket
            and config.verified
            and not needs_review):
        decision, status = "promoted", "activa"
        keep_ticket, keep_day = existing.ticket, existing.date
    else:
        decision = "updated"
        status = existing.status
        keep_ticket, keep_day = existing.ticket, existing.date
        if (existing.status == "borrador"
                and existing.ticket != config.ticket
                and needs_review):
            blocked_hold = True
        elif existing.status == "activa" and needs_review:
            status = "borrador"
            demoted_hold = True
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for path in _copyable_files(draft.source):
        dest = target / path.relative_to(draft.source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    _stamp_header(target / "SKILL.md", draft.name, draft.description,
                  status, keep_ticket, keep_day, carried=carried)
    if existing is not None and not _tree_changed(worktree, skill_rel):
        outcome.unchanged.append(draft.name)
        return
    try:
        parsed = parse_role_skill(target / "SKILL.md", config.agent)
    except CatalogError as exc:
        _rollback(worktree, skill_rel, target, existing is not None)
        outcome.discarded[draft.name] = f"invalid skill: {exc}"
        return
    pairing = _script_problems(target)
    if pairing:
        _rollback(worktree, skill_rel, target, existing is not None)
        outcome.discarded[draft.name] = "; ".join(pairing)
        return
    validate = config.validate or (
        lambda path: muse_validate_skill(path, muse_bin=config.muse_bin))
    ok, reason = validate(target)
    if not ok:
        _rollback(worktree, skill_rel, target, existing is not None)
        outcome.discarded[draft.name] = f"validate: {reason}"
        return
    if _should_run_tests(worktree, skill_rel, target):
        runner = config.run_skill_tests or fenced_skill_tests
        ok, reason = runner(skill_rel=skill_rel, worktree=worktree)
        if not ok:
            _rollback(worktree, skill_rel, target, existing is not None)
            outcome.discarded[draft.name] = f"skill tests: {reason}"
            return
    if demoted_hold or (blocked_hold and config.verified):
        outcome.awaiting_review.append(draft.name)
    getattr(outcome, decision).append(draft.name)
    current[draft.name] = parsed


def _stamp_header(skill_md: Path, name: str, description: str,
                  status: str, ticket: str, day: date,
                  carried: Mapping[str, Any] | None = None) -> None:
    """Rewrite the header with the gate stamp, preserving body bytes.

    ``carried`` restores stored marker keys the incoming draft omitted
    (D9: the external flag survives updates until review approves).
    """
    split = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
    if isinstance(split, str):
        raise _Defer(f"draft {name} turned unreadable: {split}")
    header, body = split
    ordered: dict[str, Any] = {
        "name": name, "description": description, "status": status,
        "ticket": ticket, "date": day}
    for key in sorted(header):
        if key not in ordered:
            ordered[key] = header[key]
    for key, value in (carried or {}).items():
        ordered.setdefault(key, value)
    skill_md.write_text(
        "---\n" + yaml.safe_dump(ordered, sort_keys=False,
                                 allow_unicode=True) + "---\n" + body,
        encoding="utf-8")


def _script_problems(skill_dir: Path) -> list[str]:
    """Every script needs its sibling test.

    Mirror of the normative ``catalog._skill_script_problems`` for
    staged drafts (the final ``load_role_skills`` judges again).
    """
    scripts = skill_dir / "scripts"
    if not scripts.is_dir():
        return []
    tests = {path.name for path in skill_dir.rglob("test_*.py")
             if path.is_file() and not path.is_symlink()}
    problems = []
    for script in sorted(scripts.rglob("*.py")):
        if not script.is_file() or script.is_symlink() \
                or script.name.startswith("test_"):
            continue
        want = f"test_{script.stem}.py"
        if want not in tests:
            problems.append(
                f"{script.name}: script without its test (add {want} "
                "under the skill dir)")
    return problems


def _rollback(worktree: Path, skill_rel: str, target: Path,
              existed: bool) -> None:
    """Restore the skill dir after a failed draft validation."""
    if existed:
        _git(worktree, "checkout", "--", skill_rel)
        _git(worktree, "clean", "-fdq", "--", skill_rel)
    else:
        shutil.rmtree(target, ignore_errors=True)


def _status_paths(worktree: Path, *paths: str) -> list[str]:
    proc = _git(worktree, "status", "--porcelain", "-z", "--", *paths)
    return [entry for entry in proc.stdout.split("\0")
            if entry and not entry.startswith(("##", "!!"))]


def _status_token(entry: str) -> str:
    """Path half of one porcelain entry (rename-safe)."""
    path = entry[3:] if len(entry) > 3 and entry[2] == " " else entry
    return path.rstrip("/")


def _tree_changed(worktree: Path, *paths: str) -> bool:
    return bool(_status_paths(worktree, *paths))


def _should_run_tests(worktree: Path, skill_rel: str,
                      target: Path) -> bool:
    """Script tests run only when scripts changed and tests exist."""
    if not any(path.is_file() and not path.is_symlink()
               for path in target.rglob("test_*.py")):
        return False
    prefix = f"{skill_rel}/scripts"
    for entry in _status_paths(worktree, skill_rel):
        token = _status_token(entry)
        if token == prefix or token.startswith(prefix + "/") \
                or prefix.startswith(token + "/"):
            return True
    return False


def _enforce_cap(outcome: SaveOutcome, current: dict[str, Any],
                 worktree: Path, skills_dir: str) -> None:
    """Delete the oldest drafts past the cap (never fresh ones first).

    Order is header (date, ticket, name): deterministic, no mtime. A
    draft touched by this same save dies only when a single ticket
    leaves more than the whole cap.
    """
    fresh = set(outcome.created) | set(outcome.promoted) \
        | set(outcome.updated) | set(outcome.unchanged)
    while True:
        drafts = sorted(
            (skill for skill in current.values()
             if skill.status == "borrador"),
            key=lambda skill: (skill.date, skill.ticket, skill.name))
        if len(drafts) <= MAX_DRAFTS_PER_ROLE:
            return
        victim = next((skill for skill in drafts
                       if skill.name not in fresh), drafts[0])
        shutil.rmtree(worktree / skills_dir / victim.name,
                      ignore_errors=True)
        del current[victim.name]
        outcome.deleted_oldest.append(victim.name)


def _verify_save(worktree: Path, agent: str) -> None:
    """Prove the staged tree keeps the catalog bootable.

    Mirrors the skill-relevant subset of ``load_catalog`` (content
    rules, card==disk, profile coverage both ways, flat-namespace
    uniqueness). Exposure links are host-local state, absent from any
    worktree by design; the Owner checkout recreates them post-pull
    (see ``skills/README.md``). Any mismatch defers: the drafts stay.
    """
    card = _load_card(worktree, agent)
    if card is None:
        raise _Defer(f"role {agent} card turned unreadable")
    try:
        disk = load_role_skills(agent, card.skills_dir, worktree)
    except CatalogError as exc:
        raise _Defer(f"staged skills for {agent} are invalid: "
                      f"{exc}") from exc
    names = {skill.name for skill in disk}
    if set(card.skills) != names:
        raise _Defer(f"card {agent} lists {sorted(card.skills)} but "
                      f"disk holds {sorted(names)}")
    if card.profile:
        profile_rel = _profile_rel(worktree, card.profile)
        if profile_rel is None:
            raise _Defer(f"profile {card.profile!r} escapes the catalog")
        try:
            data = yaml.safe_load(
                (worktree / "catalog" / profile_rel)
                .read_text(encoding="utf-8"))
            profile = RoleProfile.model_validate(data)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            raise _Defer(f"profile {card.profile!r} turned unreadable: "
                          f"{exc}") from exc
        foreign = [skill for skill in profile.skills
                   if skill not in card.skills
                   and skill not in ENGINE_SKILLS]
        if foreign:
            raise _Defer(f"profile {agent} loads skills missing from "
                          f"its card: {foreign}")
        uncovered = [skill for skill in card.skills
                     if skill not in profile.skills]
        if uncovered:
            raise _Defer(f"card {agent} lists skills the profile does "
                          f"not load: {uncovered}")
    by_role: dict[str, list[Any]] = {agent: disk}
    for card_file in sorted((worktree / "catalog" / "agents").glob("*.yaml")):
        if card_file.stem == agent:
            continue
        try:
            data = yaml.safe_load(card_file.read_text(encoding="utf-8"))
            other = AgentCard.model_validate(data)
            siblings = load_role_skills(
                other.name, other.skills_dir, worktree) \
                if other.type is AgentType.AI else []
        except (OSError, yaml.YAMLError, ValidationError,
                CatalogError) as exc:
            raise _Defer(f"role {card_file.stem} turned unreadable: "
                          f"{exc}") from exc
        if other.type is AgentType.AI:
            by_role[other.name] = siblings
    problems = check_skill_names_unique(by_role)
    if problems:
        raise _Defer(f"skill names collide: {problems[0]}")


def _commit(config: SaveStepConfig, outcome: SaveOutcome,
            worktree: Path) -> None:
    touched = [*outcome.created, *outcome.promoted, *outcome.updated,
               *outcome.deleted_oldest] or ["catalog alignment"]
    subject = (f"skills({config.agent}): crystallize "
               f"{', '.join(touched)} ({config.ticket})")
    bits = []
    if outcome.created:
        bits.append(f"created: {', '.join(outcome.created)}")
    if outcome.promoted:
        bits.append(f"promoted: {', '.join(outcome.promoted)}")
    if outcome.updated:
        bits.append(f"updated: {', '.join(outcome.updated)}")
    if outcome.deleted_oldest:
        bits.append(f"deleted oldest: {', '.join(outcome.deleted_oldest)}")
    for name in sorted(outcome.discarded):
        bits.append(f"discarded {name}: "
                     f"{_short(outcome.discarded[name])}")
    message = subject + ("\n\n" + "\n".join(bits) if bits else "")
    proc = _git(worktree, "-c", f"user.name={_GIT_USER_NAME}",
                "-c", f"user.email={_GIT_USER_EMAIL}",
                "commit", "--quiet", "-m", message)
    if proc.returncode != 0:
        raise _Defer(f"commit failed: {proc.stderr.strip()[:200]}")


def _push(config: SaveStepConfig, worktree: Path) -> None:
    """Push at once; on refusal fetch, rebase, re-verify and retry."""
    base = f"{config.remote}/{config.base_branch}"
    for attempt in range(config.push_retries + 1):
        proc = _git(worktree, "push", "--quiet", config.remote,
                    f"HEAD:{config.base_branch}")
        if proc.returncode == 0:
            return
        if attempt >= config.push_retries:
            break
        _fetch(config, worktree)
        rebased = _git(worktree, "rebase", "--quiet", base)
        if rebased.returncode != 0:
            _git(worktree, "rebase", "--abort")
            raise _Defer(f"push conflict with {base}: rebase hit a "
                          f"conflict, drafts kept")
        _verify_save(worktree, config.agent)
    raise _Defer(f"push to {config.remote}/{config.base_branch} refused "
                 f"after retries: {proc.stderr.strip()[:200]}")


def muse_validate_skill(skill_dir: Path | str,
                        *, muse_bin: str | None = None,
                        timeout_s: int = _VALIDATE_TIMEOUT_S
                        ) -> tuple[bool, str]:
    """``muse skills validate`` on one staged skill (trusted binary)."""
    binary = muse_bin or shutil.which("muse")
    if not binary:
        return False, "muse binary is missing, refusing unvalidated skill"
    env = {**os.environ, "MUSE_NO_AUTO_UPDATE": "1"}
    try:
        proc = subprocess.run(
            [binary, "skills", "validate", str(skill_dir)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout_s, env=env,
            check=False)
    except OSError as exc:
        return False, f"validate failed to run: {exc}"
    except subprocess.TimeoutExpired:
        return False, "validate timed out"
    if proc.returncode != 0:
        detail = (proc.stdout or "").strip()
        return False, detail[-500:] if detail else "validate rejected it"
    return True, ""


_PEP723_BLOCK = re.compile(
    r"(?m)^# /// (?P<type>[a-zA-Z0-9-]+)$\s(?P<content>(^#(| .*)$\s)+)^# ///$")


def script_dependencies(skill_dir: str | Path) -> list[str]:
    """Third-party packages the skill's scripts declare (PEP 723).

    Skill scripts run with ``uv run``, which builds their environment from
    the inline ``# /// script`` block (TASK-2026-00485); test files are not
    scanned. Raises ValueError on malformed metadata.
    """
    found: list[str] = []
    scripts = Path(skill_dir) / "scripts"
    if not scripts.is_dir():
        return found
    for path in sorted(scripts.rglob("*.py")):
        if path.name.startswith("test_") or not path.is_file() \
                or path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        blocks = [match for match in _PEP723_BLOCK.finditer(text)
                  if match.group("type") == "script"]
        if len(blocks) > 1:
            raise ValueError(f"{path.name}: more than one PEP 723 block")
        if not blocks:
            continue
        content = "".join(
            line[2:] if line.startswith("# ") else line[1:]
            for line in blocks[0].group("content").splitlines(keepends=True))
        try:
            meta = tomllib.loads(content)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path.name}: invalid PEP 723 metadata: "
                             f"{exc}") from exc
        deps = meta.get("dependencies", [])
        if not isinstance(deps, list):
            raise ValueError(f"{path.name}: dependencies must be a list")
        for dep in deps:
            text_dep = str(dep).strip() if isinstance(dep, str) else ""
            # A leading '-' would reach uv as an option, not a package.
            if not text_dep or not text_dep[0].isalnum() or "\n" in text_dep:
                raise ValueError(f"{path.name}: invalid dependency {dep!r}")
            if text_dep not in found:
                found.append(text_dep)
    return found


def fenced_skill_tests(*, skill_rel: str, worktree: str | Path,
                       timeout_s: int = _SKILL_TEST_TIMEOUT_S
                       ) -> tuple[bool, str]:
    """Run the skill's tests fenced like an agent run.

    Agent-authored code is untrusted: the worktree is the workspace
    (rw, discarded with it), the service interpreter mounts read-only,
    and bytecode/caches stay off. No tests means nothing to run. Scripts
    that declare PEP 723 dependencies get them through ``uv run`` inside
    the same fence (an ephemeral env, nothing installed on the host).
    """
    root = Path(worktree)
    target = root / skill_rel
    tests = sorted(path for path in target.rglob("test_*.py")
                   if path.is_file() and not path.is_symlink())
    if not tests:
        return True, "no tests"
    try:
        deps = script_dependencies(target)
    except (ValueError, OSError) as exc:
        return False, f"invalid script dependencies: {exc}"
    pytest_args = [*(str(path.relative_to(root)) for path in tests),
                   "-q", "-p", "no:cacheprovider"]
    if deps:
        uv = shutil.which("uv")
        if uv is None:
            return False, "skill scripts declare dependencies but uv is missing"
        argv = [uv, "run", "--no-project", "--quiet", "--with", "pytest",
                *[token for dep in deps for token in ("--with", dep)],
                "python", "-B", "-m", "pytest", *pytest_args]
    else:
        argv = [sys.executable, "-B", "-m", "pytest", *pytest_args]
    try:
        plan = plan_agent(
            argv=argv,
            workspace=root, base=os.environ,
            extra_read_roots=[sys.prefix])
    except (IsolationUnavailable, ValueError, OSError) as exc:
        return False, f"sandbox refused: {exc}"
    try:
        proc = subprocess.run(
            plan.argv, cwd=plan.cwd, env=plan.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout_s, check=False)
    except OSError as exc:
        return False, f"test run failed: {exc}"
    except subprocess.TimeoutExpired:
        return False, "skill tests timed out"
    finally:
        shutil.rmtree(root / ".openceo", ignore_errors=True)
    if proc.returncode != 0:
        detail = (proc.stdout or "").strip()
        tail = detail[-800:] if detail else "tests failed"
        return False, tail
    return True, ""


def _short(reason: str, limit: int = 120) -> str:
    cleaned = " ".join(str(reason).split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def _comment(ticket: str, outcome: SaveOutcome) -> str | None:
    if outcome.skipped is not None:
        return None
    if outcome.deferred is not None:
        return (f"Crystallization deferred ({outcome.deferred}); drafts "
                f"kept for the next close.")
    bits = []
    if outcome.created:
        bits.append(f"created {', '.join(outcome.created)}")
    if outcome.promoted:
        bits.append(f"promoted {', '.join(outcome.promoted)}")
    if outcome.updated:
        bits.append(f"updated {', '.join(outcome.updated)}")
    if outcome.awaiting_review:
        bits.append(f"awaiting independent review "
                    f"(external content): "
                    f"{', '.join(outcome.awaiting_review)}")
    if outcome.unchanged:
        bits.append(f"already present: {', '.join(outcome.unchanged)}")
    if outcome.deleted_oldest:
        bits.append(f"deleted oldest: {', '.join(outcome.deleted_oldest)}")
    for name in sorted(outcome.discarded):
        bits.append(f"discarded {name} ({_short(outcome.discarded[name])})")
    if not bits:
        return None
    text = f"Crystallization ({ticket}): " + "; ".join(bits) + "."
    if outcome.commit:
        text += f" Commit {outcome.commit[:12]}."
    return text


def _consume(drafts_dir: Path, names: list[str]) -> None:
    """Remove handled drafts so a re-run never reprocesses them."""
    for name in names:
        shutil.rmtree(drafts_dir / name, ignore_errors=True)


