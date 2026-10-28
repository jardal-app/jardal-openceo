"""External learning candidates from Jira, Autotask and Teams (TASK-2026-00339).

D13: Jira, Autotask and Teams stay the source of truth of their tickets
and conversations. OpenCEO keeps references (``source``, ``source_id``)
and never duplicates them. Only concrete events teach: a requested
change in a PR, a reopened or rejected ticket, or a direct message
from a person with declared authority in the project (authority list
in the project configuration). The derived rule is stored with its
link, never the conversation. Each candidate is a normative proposal
bound for the 2.5 block confirmation over ``owner_channel``.

No tokens. Thresholds (event names, authority key, filename shape) are
agent decisions, reopenable by the Owner (recorded in TASK-2026-00339).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

from external_send import EXTERNAL_TOOLS
from orchestration.agent_sandbox import ticket_dir
from orchestration.knowledge_proposals import KnowledgeProposal
from orchestration.vault_publisher import CONFIRM_QUEUE_DIRNAME

__all__ = [
    "CONCRETE_EVENT_KINDS",
    "EXTERNAL_CHANNELS",
    "ExternalLearningError",
    "authorized_persons",
    "build_external_candidate",
    "candidate_filename",
    "evaluate_external_event",
    "is_authorized",
    "is_concrete_event",
    "queue_external_candidate",
]

#: Channels that can teach (same set as the 4.1 sends and 4.2 reads).
EXTERNAL_CHANNELS: tuple[str, ...] = EXTERNAL_TOOLS

#: Only these event kinds ever yield a candidate (D13, agent decision).
CONCRETE_EVENT_KINDS: tuple[str, ...] = (
    "pr_change_requested",
    "ticket_reopened",
    "ticket_rejected",
    "direct_message",
)

_FILENAME_BAD_RE = re.compile(r"[^a-z0-9_-]+")


class ExternalLearningError(ValueError):
    """An external event cannot become a learning candidate (fail-closed)."""


def _clean_channel(channel: str) -> str:
    clean = str(channel or "").strip().lower()
    if clean not in EXTERNAL_CHANNELS:
        raise ExternalLearningError(f"unknown external channel {channel!r}")
    return clean


def _clean_kind(kind: str) -> str:
    clean = str(kind or "").strip().lower()
    if not clean:
        raise ExternalLearningError("an external event needs its kind")
    return clean


def is_concrete_event(kind: str) -> bool:
    """Tell whether an event kind is concrete enough to teach. Pure."""
    return _clean_kind(kind) in CONCRETE_EVENT_KINDS


def _clean_person(name: str) -> str:
    return str(name or "").strip().casefold()


def authorized_persons(project_config: Mapping[str, Any] | None) -> list[str]:
    """Declared authority list of one project, or [] when absent.

    Reads ``dev.external_learning.authorities`` (agent decision,
    reopenable by the Owner). Never raises: without a list nobody is
    authorized and every event is ignored.
    """
    try:
        dev = (project_config or {}).get("dev") or {}
        if not isinstance(dev, Mapping):
            return []
        node = dev.get("external_learning") or {}
        if not isinstance(node, Mapping):
            return []
        raw = node.get("authorities")
        if not isinstance(raw, list):
            return []
        seen: list[str] = []
        for entry in raw:
            clean = str(entry or "").strip()
            if clean and clean not in seen:
                seen.append(clean)
        return seen
    except Exception:
        return []


def is_authorized(author: str, authorities: list[str] | tuple[str, ...]) -> bool:
    """Tell whether an author holds declared authority. Pure."""
    wanted = _clean_person(author)
    if not wanted:
        return False
    return any(_clean_person(entry) == wanted for entry in (authorities or []))


def evaluate_external_event(
    event: Mapping[str, Any],
    authorities: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Decide whether one external event teaches. Pure, never raises.

    Returns ``{"learnable": bool, "reason": str}``. Generic kinds and
    authors without declared authority are ignored; unknown channels
    and missing links fail closed as not learnable.
    """
    if not isinstance(event, Mapping):
        return {"learnable": False, "reason": "event is not a mapping"}
    try:
        channel = _clean_channel(event.get("channel"))
    except ExternalLearningError as exc:
        return {"learnable": False, "reason": str(exc)}
    kind = str(event.get("kind") or "").strip().lower()
    if kind not in CONCRETE_EVENT_KINDS:
        return {"learnable": False,
                "reason": f"generic event {kind!r} never teaches"}
    source_id = str(event.get("source_id") or "").strip()
    if not source_id:
        return {"learnable": False, "reason": "event carries no link"}
    author = str(event.get("author") or "")
    if not is_authorized(author, authorities):
        return {"learnable": False,
                "reason": f"author {author.strip()!r} has no declared authority"}
    return {"learnable": True, "channel": channel, "kind": kind,
            "reason": "concrete event from an authorized author"}


def _target_inside_project(target: str, project: str) -> str | None:
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


def _candidate_content(
    *,
    rule: str,
    channel: str,
    kind: str,
    source_id: str,
    author: str,
    origin_ticket: str,
) -> str:
    title = " ".join(rule.strip().split())[:80] or "Regla aprendida"
    return (
        "---\ntype: practice\nproject: external-learning\n"
        f"date: {date.today().isoformat()}\n---\n\n"
        f"# {title}\n\n{rule.strip()}\n\n"
        f"Fuente: {channel}:{source_id} "
        f"({kind}, propuesto por {author.strip()}).\n"
        f"Origen: {origin_ticket}.\n"
    )


def build_external_candidate(
    *,
    channel: str,
    kind: str,
    author: str,
    source_id: str,
    rule: str,
    project: str,
    target: str,
    origin_ticket: str,
    action: str = "create",
    base_version: str | None = None,
) -> dict[str, Any]:
    """Build one normative candidate bound for the 2.5 confirmation. Pure.

    Only the derived rule and its link enter the note: the caller never
    passes the conversation, so no conversation text can be stored. The
    evidence points at the local observing ticket; the external link
    lives in the content as ``Fuente: <channel>:<source_id>``.
    Raises :class:`ExternalLearningError` fail-closed on any invalid
    input, including generic kinds and unauthorized authors.
    """
    clean_channel = _clean_channel(channel)
    clean_kind = _clean_kind(kind)
    if clean_kind not in CONCRETE_EVENT_KINDS:
        raise ExternalLearningError(
            f"generic event {kind!r} never teaches")
    clean_author = str(author or "").strip()
    if not clean_author:
        raise ExternalLearningError("a candidate needs its author")
    clean_source = str(source_id or "").strip()
    if not clean_source:
        raise ExternalLearningError("a candidate needs its link")
    clean_rule = str(rule or "").strip()
    if not clean_rule:
        raise ExternalLearningError("a candidate needs its derived rule")
    clean_target = str(target or "").strip()
    problem = _target_inside_project(clean_target, project)
    if problem is not None:
        raise ExternalLearningError(problem)
    clean_origin = str(origin_ticket or "").strip()
    if not clean_origin:
        raise ExternalLearningError("a candidate needs its origin ticket")
    clean_action = str(action or "").strip()
    if clean_action not in ("create", "update"):
        raise ExternalLearningError(
            f"unknown proposal action {action!r}")
    base = None if base_version is None else str(base_version).strip() or None
    content = _candidate_content(
        rule=clean_rule, channel=clean_channel, kind=clean_kind,
        source_id=clean_source, author=clean_author,
        origin_ticket=clean_origin)
    candidate = {
        "target": clean_target,
        "action": clean_action,
        "class": "normative",
        "base_version": base,
        "evidence": [{"ticket": clean_origin}],
        "content": content,
    }
    KnowledgeProposal.model_validate(candidate)
    return candidate


def _has_frontmatter(content: str) -> bool:
    lines = str(content or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() in ("---", "...") for line in lines[1:])


def candidate_filename(channel: str, source_id: str, kind: str) -> str:
    """Stable queue filename for one external candidate. Pure."""
    clean_channel = _clean_channel(channel)
    clean_kind = _clean_kind(kind)
    slug = _FILENAME_BAD_RE.sub(
        "-", str(source_id or "").strip().lower()).strip("-") or "link"
    return f"{clean_channel}-{slug}-{clean_kind}.yaml"


def queue_external_candidate(
    candidate: Mapping[str, Any],
    *,
    origin: str,
    base_dir: str | Path | None = None,
    filename: str | None = None,
) -> Path:
    """Write one candidate into the 2.5 confirmation queue.

    The file lands under ``knowledge_confirm_queue/`` in the origin
    ticket folder, where ``collect_pending_confirmations`` groups it
    into the next block question. The candidate is schema-validated
    first, so an invalid one raises instead of queueing a file the
    confirmation step would reject. Returns the written path.
    """
    if not isinstance(candidate, Mapping):
        raise ExternalLearningError("a candidate is a mapping")
    data = {str(key): value for key, value in dict(candidate).items()}
    KnowledgeProposal.model_validate(data)
    if not _has_frontmatter(str(data.get("content") or "")):
        raise ExternalLearningError(
            "content lacks frontmatter: the full note is required")
    try:
        folder = ticket_dir(origin, base_dir=(
            Path(base_dir) if base_dir is not None else None))
    except ValueError as exc:
        raise ExternalLearningError(f"invalid origin ticket id ({exc})")
    queue = folder / CONFIRM_QUEUE_DIRNAME
    queue.mkdir(parents=True, exist_ok=True)
    name = str(filename or "").strip()
    if not name:
        name = "external-candidate.yaml"
    if "/" in name or name.startswith("."):
        raise ExternalLearningError(f"unsafe candidate filename {name!r}")
    if not name.endswith((".yaml", ".yml")):
        raise ExternalLearningError(f"candidate filename {name!r} needs yaml")
    path = queue / name
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return path
