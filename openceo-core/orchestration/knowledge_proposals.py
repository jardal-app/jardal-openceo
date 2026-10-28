"""Knowledge proposals at ticket close (TASK-2026-00330, D7 / principle 4).

No autonomous agent writes durable memory directly. At close the agent
judges whether it learned something that changes how to work next time
(a technical fact, an incident cause, a decision or a practice) and, only
then, leaves proposals under ``knowledge_proposals/`` in its ticket
folder — never in the vault. Routine progress and fixes stay in the
ticket (the D7 treatment table).

This module is the deterministic structural gate the orchestrator runs
at collect time (the publisher of step 2.4 consumes what passes): no
tokens, no ticket outcome changes. A proposal failing any check is
rejected with a recorded reason; the ticket close is never blocked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestration.agent_sandbox import ticket_dir

logger = logging.getLogger(__name__)

__all__ = [
    "KNOWLEDGE_PROPOSAL_PROMPT",
    "PROPOSALS_DIRNAME",
    "PROPOSAL_FILENAME_SUFFIXES",
    "CollectedProposal",
    "KnowledgeProposal",
    "ProposalEvidence",
    "collect_proposals",
    "has_proposals",
]

#: Ticket-folder dir where agents leave knowledge proposals for the gate.
PROPOSALS_DIRNAME = "knowledge_proposals"

#: Accepted file suffixes for one proposal per file.
PROPOSAL_FILENAME_SUFFIXES = (".yaml", ".yml")

#: Proposal kinds (D7 treatment: descriptive publishes, normative confirms).
_DESCRIPTIVE = "descriptive"
_NORMATIVE = "normative"

KNOWLEDGE_PROPOSAL_PROMPT = f"""\
When the ticket work is done, judge whether you learned something that \
changes how to work next time (a technical fact, an incident cause, a \
decision or a practice). Only then, leave one file per proposal under \
`{PROPOSALS_DIRNAME}/<topic>.yaml` in this folder (your ticket folder), \
with `target` (note path inside this ticket's project folder), `action` \
(`create` | `update`), `class` (`descriptive` for checked facts, \
`normative` for work rules), `base_version` (git hash of the note you \
read, or null when new), `evidence` (at least one of: ticket, commit, \
path) and `content` (the full note with frontmatter). Before proposing, \
search the read-only project folder for a note on the same topic and \
update it (`action: update`) instead of creating a duplicate. Routine \
progress and fixes stay in the ticket, never as proposals; never write \
outside `{PROPOSALS_DIRNAME}/` for this and never into the vault: the \
orchestrator validates and publishes.\
"""


class ProposalEvidence(BaseModel):
    """One resolvable evidence pointer (at least one field set)."""

    model_config = ConfigDict(extra="forbid")

    ticket: str = ""
    commit: str = ""
    path: str = ""

    @field_validator("ticket", "commit", "path", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class KnowledgeProposal(BaseModel):
    """Minimum D7 proposal format, validated structurally at collect."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    target: str
    action: Literal["create", "update"]
    proposal_class: Literal["descriptive", "normative"] = Field(alias="class")
    base_version: str | None = None
    evidence: list[ProposalEvidence]
    content: str

    @field_validator("target", mode="before")
    @classmethod
    def _strip_target(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("base_version", mode="before")
    @classmethod
    def _strip_base(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("content", mode="before")
    @classmethod
    def _strip_content(cls, value: Any) -> str:
        return str(value or "")


@dataclass(frozen=True)
class CollectedProposal:
    """One structurally sound proposal, ready for the publisher (2.4)."""

    name: str
    source: Path
    target: str
    action: str
    proposal_class: str
    base_version: str | None
    evidence: list[dict[str, str]]
    content: str


def _base(base_dir: str | Path | None) -> Path | None:
    return Path(base_dir) if base_dir is not None else None


def has_proposals(task_id: str, *,
                  base_dir: str | Path | None = None) -> bool:
    """True when the ticket folder holds collectable proposals.

    Read-only: never creates the folder (most closes have none).
    Only entries the collector would consider count — a stray dir,
    symlink or dotfile never re-triggers the gate on every close.
    """
    try:
        target = ticket_dir(task_id, base_dir=_base(base_dir)) \
            / PROPOSALS_DIRNAME
    except ValueError:
        return False
    try:
        if not target.is_dir():
            return False
        return any(
            not entry.name.startswith(".")
            and not entry.is_symlink()
            and entry.is_file()
            and entry.suffix.lower() in PROPOSAL_FILENAME_SUFFIXES
            for entry in target.iterdir())
    except OSError:
        return False


def _target_ok(target: str, project: str) -> str | None:
    """Reason the target is unusable, or None when it is inside project."""
    if not target or target.startswith("/"):
        return f"target {target!r} is not a project-relative note path"
    parts = Path(target).parts
    if any(part in ("", ".", "..") for part in parts):
        return f"target {target!r} escapes the project folder"
    if not target.endswith(".md"):
        return f"target {target!r} is not a vault note (.md)"
    prefix = project.strip().strip("/")
    if not prefix:
        return "ticket has no project folder configured"
    if target != prefix and not target.startswith(prefix + "/"):
        return (f"target {target!r} is outside the ticket project "
                f"{prefix!r}")
    return None


def _has_frontmatter(content: str) -> bool:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() in ("---", "...") for line in lines[1:])


def _check_proposal(data: Any, project: str) -> CollectedProposal | str:
    """Validate one parsed file: CollectedProposal or the reject reason."""
    if not isinstance(data, dict):
        return "proposal file is not a YAML mapping"
    try:
        proposal = KnowledgeProposal.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - reason recorded, never raised
        return f"invalid schema: {exc}"[:500]
    problem = _target_ok(proposal.target, project)
    if problem is not None:
        return problem
    kept = [entry for entry in proposal.evidence
            if entry.ticket or entry.commit or entry.path]
    if not kept:
        return "evidence is missing: at least one ticket, commit or path"
    if not proposal.content.strip():
        return "content is empty: the full note with frontmatter is required"
    if not _has_frontmatter(proposal.content):
        return "content lacks frontmatter: the full note is required"
    return CollectedProposal(
        name="",
        source=Path(),
        target=proposal.target,
        action=proposal.action,
        proposal_class=proposal.proposal_class,
        base_version=proposal.base_version,
        evidence=[{key: value for key, value in entry.model_dump().items()
                   if value}
                  for entry in kept],
        content=proposal.content,
    )


def collect_proposals(task_id: str, project: str, *,
                      base_dir: str | Path | None = None,
                      ) -> tuple[list[CollectedProposal], dict[str, str]]:
    """Read proposals structurally: valid ones plus per-file reasons.

    Never raises and never blocks the close: unreadable files, schema
    errors, targets outside the ticket project, unknown actions or
    classes and missing evidence land in ``rejected`` with the reason
    the orchestrator records on the ticket.
    """
    try:
        target = ticket_dir(task_id, base_dir=_base(base_dir)) \
            / PROPOSALS_DIRNAME
    except ValueError as exc:
        return [], {"": f"invalid ticket id: {exc}"[:300]}
    try:
        entries = sorted(target.iterdir(), key=lambda item: item.name)
    except OSError:
        return [], {}
    valid: list[CollectedProposal] = []
    rejected: dict[str, str] = {}
    for entry in entries:
        if entry.name.startswith(".") or entry.is_symlink() \
                or not entry.is_file() \
                or entry.suffix.lower() not in PROPOSAL_FILENAME_SUFFIXES:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError as exc:
            rejected[entry.name] = f"proposal is unreadable ({exc})"[:300]
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            rejected[entry.name] = f"proposal is not valid YAML ({exc})"[:300]
            continue
        outcome = _check_proposal(data, project)
        if isinstance(outcome, str):
            rejected[entry.name] = outcome
            continue
        valid.append(CollectedProposal(
            name=entry.stem,
            source=entry,
            target=outcome.target,
            action=outcome.action,
            proposal_class=outcome.proposal_class,
            base_version=outcome.base_version,
            evidence=outcome.evidence,
            content=outcome.content,
        ))
    return valid, rejected
