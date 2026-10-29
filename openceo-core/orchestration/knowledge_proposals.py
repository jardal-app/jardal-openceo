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

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from orchestration.agent_sandbox import ticket_dir

__all__ = [
    "KNOWLEDGE_PROPOSAL_PROMPT",
    "MAX_PROPOSAL_BYTES",
    "MAX_PROPOSAL_FILES",
    "PROPOSALS_DIRNAME",
    "PROPOSAL_FILENAME_SUFFIXES",
    "CollectedProposal",
    "KnowledgeProposal",
    "ProposalEvidence",
    "collect_and_comment",
    "collect_proposals",
    "format_close_comment",
    "has_proposals",
    "knowledge_target_prefix",
    "proposal_prompt_for",
    "proposals_dir_for",
]

#: Ticket-folder dir where agents leave knowledge proposals for the gate.
PROPOSALS_DIRNAME = "knowledge_proposals"

#: Accepted file suffixes for one proposal per file.
PROPOSAL_FILENAME_SUFFIXES = (".yaml", ".yml")

#: Per-file and per-close caps so one close cannot fill the disk or the
#: ticket with proposals (mirrors the crystallization caps).
MAX_PROPOSAL_BYTES = 128_000
MAX_PROPOSAL_FILES = 64

KNOWLEDGE_PROPOSAL_PROMPT = f"""\
When the ticket work is done, judge whether you learned something that \
changes how to work next time (a technical fact, an incident cause, a \
decision or a practice). Only then, leave one file per proposal under \
`{PROPOSALS_DIRNAME}/<topic>.yaml` in this folder (your ticket folder), \
with `target` (vault-relative note path inside this ticket's project \
folder, e.g. `projects/<org>/<proyecto>/practices/<topic>.md`), `action` \
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


def knowledge_target_prefix(project_config: Mapping[str, Any] | None) -> str:
    """Vault-relative target prefix for a project config, or "".

    Reads ``dev.vault_project.project`` (e.g.
    ``projects/jardal/jardal-openceo``). Never raises: without a mapping
    the caller omits the proposal instruction (no vault to propose to).
    """
    try:
        dev = (project_config or {}).get("dev") or {}
        vault = dev.get("vault_project") or {}
        return str(vault.get("project") or "").strip().strip("/")
    except Exception:  # noqa: BLE001
        return ""


def proposals_dir_for(
    task_id: str, *,
    base_dir: str | Path | None = None,
) -> Path | None:
    """Absolute proposals dir for a ticket folder, or None when invalid."""
    try:
        return ticket_dir(task_id, base_dir=_base(base_dir)) \
            / PROPOSALS_DIRNAME
    except ValueError:
        return None


def proposal_prompt_for(
    task_id: str, *,
    base_dir: str | Path | None = None,
    project_prefix: str = "",
) -> str:
    """Close-review instruction pointing at the real ticket folder.

    Executors whose cwd is the project repo (Tier S builder, Tier A/B/C
    coding) cannot use the relative ``in this folder`` form: it would
    land in the repo root, never in the folder the gate collects from.
    Falls back to the generic prompt when the id is unusable.
    """
    folder = proposals_dir_for(task_id, base_dir=base_dir)
    if folder is None:
        return KNOWLEDGE_PROPOSAL_PROMPT
    example = (f"`{project_prefix}/practices/<topic>.md`"
               if project_prefix.strip().strip("/") else
               "`projects/<org>/<proyecto>/practices/<topic>.md`")
    return f"""\
When the ticket work is done, judge whether you learned something that \
changes how to work next time (a technical fact, an incident cause, a \
decision or a practice). Only then, leave one file per proposal under \
`{folder}/<topic>.yaml` (your ticket folder), with `target` \
(vault-relative note path inside this ticket's project folder, e.g. \
{example}), `action` (`create` | `update`), `class` (`descriptive` for \
checked facts, `normative` for work rules), `base_version` (git hash of \
the note you read, or null when new), `evidence` (at least one of: \
ticket, commit, path) and `content` (the full note with frontmatter). \
Before proposing, search the read-only project folder for a note on the \
same topic and update it (`action: update`) instead of creating a \
duplicate. Routine progress and fixes stay in the ticket, never as \
proposals; never write outside `{folder}/` for this and never into the \
vault: the orchestrator validates and publishes.\
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


def _target_ok(target: str, project: str | None) -> str | None:
    """Reason the target is unusable, or None when it is inside project."""
    if not target or target.startswith("/"):
        return f"target {target!r} is not a project-relative note path"
    parts = Path(target).parts
    if any(part in ("", ".", "..") for part in parts):
        return f"target {target!r} escapes the project folder"
    if not target.endswith(".md"):
        return f"target {target!r} is not a vault note (.md)"
    prefix = str(project or "").strip().strip("/")
    if not prefix:
        return "ticket has no project folder configured"
    if target != prefix and not target.startswith(prefix + "/"):
        return (f"target {target!r} is outside the ticket project "
                f"{prefix!r}")
    return None


def _schema_reason(exc: Exception) -> str:
    """Rejection reason for a schema failure without echoing content.

    Pydantic v2 renders the full offending mapping (including the note
    body) inside ``str(exc)``; the reason lands on the ticket, so only
    locations and messages are kept, never input values.
    """
    if isinstance(exc, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', ())) or '?'}: "
            f"{item.get('msg', 'invalid value')}"
            for item in exc.errors()
        )
        return f"invalid schema: {details or 'invalid proposal'}"[:500]
    return f"invalid schema ({type(exc).__name__})"[:300]


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
        return _schema_reason(exc)
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


def collect_proposals(task_id: str, project: str | None, *,
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
    eligible = [
        entry for entry in entries
        if not entry.name.startswith(".") and not entry.is_symlink()
        and entry.is_file()
        and entry.suffix.lower() in PROPOSAL_FILENAME_SUFFIXES
    ]
    for entry in eligible[:MAX_PROPOSAL_FILES]:
        try:
            if entry.stat().st_size > MAX_PROPOSAL_BYTES:
                rejected[entry.name] = (
                    "proposal exceeds "
                    f"{MAX_PROPOSAL_BYTES} bytes and is skipped")[:300]
                continue
        except OSError as exc:
            rejected[entry.name] = f"proposal is unreadable ({exc})"[:300]
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
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
    for entry in eligible[MAX_PROPOSAL_FILES:]:
        rejected[entry.name] = (
            f"too many proposal files (cap {MAX_PROPOSAL_FILES})")[:300]
    return valid, rejected


def format_close_comment(
    valid: Sequence[CollectedProposal],
    rejected: Mapping[str, str],
) -> str | None:
    """Ticket comment for a close collect: None when there is nothing.

    Names every valid proposal (ready for the 2.4 publisher) and every
    rejection with its reason, so the record lives on the ticket without
    blocking its close.
    """
    names = [item.name for item in valid]
    if not names and not rejected:
        return None
    lines = [f"Knowledge proposals (D7): {len(names)} valid, "
             f"{len(rejected)} rejected."]
    if names:
        lines.append("Valid, ready for the publisher (2.4):")
        lines.extend(f"- {name}" for name in sorted(names))
    if rejected:
        lines.append("Rejected with reason:")
        lines.extend(f"- {name}: {reason}"
                     for name, reason in sorted(rejected.items()))
    return "\n".join(lines)[:8000]


async def collect_and_comment(
    task_id: str, *,
    project_prefix: str | None,
    base_dir: str | Path | None = None,
    comment: Callable[[str, str], Awaitable[bool | None]],
) -> tuple[list[CollectedProposal], dict[str, str]]:
    """Collect at close and record the outcome on the ticket (H1/2.3).

    Runs the blocking read off the event loop, then posts one comment
    with the valid proposals and every rejection reason. Never raises
    for collect failures; a failing comment propagates to the hook,
    which logs it without touching the ticket outcome.
    """
    valid, rejected = await asyncio.to_thread(
        collect_proposals, task_id, project_prefix, base_dir=base_dir)
    text = format_close_comment(valid, rejected)
    if text is not None:
        await comment(task_id, text)
    return valid, rejected
