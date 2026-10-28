"""Batch Owner confirmation of normative vault proposals (TASK-2026-00332).

D7: practices and work rules for future agents go to Owner confirmation,
grouped in the summary. The 2.4 publisher queues normative proposals
(and descriptive ones touching mandate/permissions) under
``knowledge_confirm_queue/`` in each ticket folder; this module groups
every pending one into a single ``question`` owner-channel ticket (no
second approval system), and applies the Owner's answer:

* approve publishes exactly what was shown, bound by the content hash:
  an item whose bytes changed after the question was built is skipped
  and kept queued, so the approval never publishes unseen content;
* deny discards the proposals, with the reason recorded for each
  origin ticket.

The channel records one decision per question, so per-item approval
inside the block is not possible: the block is all-or-nothing (agent
decision, reopenable by the Owner). Tests use a simulated channel and
temporary repos/vaults only: never the real vault. No tokens.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml
from owner_channel import build_owner_question
from orchestration.agent_sandbox import ticket_dir
from orchestration.knowledge_proposals import (
    MAX_PROPOSAL_BYTES,
    MAX_PROPOSAL_FILES,
    PROPOSAL_FILENAME_SUFFIXES,
    KnowledgeProposal,
    _schema_reason,
)
from orchestration.vault_publisher import (
    CONFIRM_QUEUE_DIRNAME,
    PublishConfig,
    publish_approved,
)

__all__ = [
    "CONFIRM_MARKER",
    "CONFIRM_SOURCE_PREFIX",
    "ConfirmOutcome",
    "PendingConfirm",
    "apply_approved_proposals",
    "apply_denied_proposals",
    "build_confirm_question",
    "collect_pending_confirmations",
    "confirm_items",
    "confirm_question_parts",
    "confirm_source_id",
    "format_confirm_comment",
    "is_confirm_question",
]

#: Checkpoint key marking an owner question as a 2.5 confirmation block.
CONFIRM_MARKER = "knowledge_confirm"

#: Source-id prefix for grouped confirmation questions.
CONFIRM_SOURCE_PREFIX = "knowledge-confirm"

#: Characters of note content shown per item in the grouped question.
EXCERPT_CHARS = 500


@dataclass(frozen=True)
class PendingConfirm:
    """One queued proposal awaiting Owner confirmation."""

    origin: str
    name: str
    target: str
    sha256: str
    path: Path
    excerpt: str = ""
    proposal_class: str = ""


@dataclass
class ConfirmOutcome:
    """Fate of one confirmation answer; files are consumed or kept."""

    published: list[str] = field(default_factory=list)
    skipped_changed: list[str] = field(default_factory=list)
    skipped_foreign: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    deferred: dict[str, str] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)


def _default_base() -> Path:
    return ticket_dir("_probe").parent


def _within_prefix(target: str, prefix: str) -> bool:
    clean = prefix.strip().strip("/")
    return bool(clean) and (
        target == clean or target.startswith(clean + "/"))


def _queue_files(base_dir: Path | None) -> list[tuple[str, Path]]:
    base = Path(base_dir) if base_dir is not None else _default_base()
    try:
        roots = sorted(
            entry for entry in base.iterdir()
            if not entry.name.startswith(".") and entry.is_dir()
            and not entry.is_symlink())
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for root in roots:
        queue = root / CONFIRM_QUEUE_DIRNAME
        try:
            entries = sorted(queue.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            if (entry.name.startswith(".") or entry.is_symlink()
                    or not entry.is_file()
                    or entry.suffix.lower()
                    not in PROPOSAL_FILENAME_SUFFIXES):
                continue
            found.append((root.name, entry))
    return found[:MAX_PROPOSAL_FILES]


def collect_pending_confirmations(
        base_dir: str | Path | None,
        project_prefix: str | None = None) -> list[PendingConfirm]:
    """Queued proposals under the ticket base for one project.

    With a prefix, only proposals whose target falls inside it are
    grouped: other projects' proposals stay queued for their own round,
    so one block never publishes or deletes foreign files. Items with
    no parseable target cannot be attributed and stay in the block, so
    the apply step rejects them with a recorded reason. Never raises:
    unreadable or oversized files are skipped here and kept queued.
    """
    base = Path(base_dir) if base_dir is not None else _default_base()
    prefix = str(project_prefix or "").strip().strip("/")
    pending: list[PendingConfirm] = []
    for origin, entry in _queue_files(base):
        try:
            if entry.stat().st_size > MAX_PROPOSAL_BYTES:
                continue
            blob = entry.read_bytes()
        except OSError:
            continue
        try:
            data = yaml.safe_load(blob.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            data = None
        target = ""
        proposal_class = ""
        excerpt = ""
        if isinstance(data, dict):
            target = str(data.get("target") or "")
            proposal_class = str(data.get("class") or "")
            excerpt = str(data.get("content") or "")[:EXCERPT_CHARS]
        if prefix and target and not _within_prefix(target, prefix):
            continue
        pending.append(PendingConfirm(
            origin=origin,
            name=entry.stem,
            target=target,
            sha256=hashlib.sha256(blob).hexdigest(),
            path=entry,
            excerpt=excerpt,
            proposal_class=proposal_class,
        ))
    return pending


def confirm_source_id(
        project: str, pending: list[PendingConfirm]) -> str:
    """Stable idempotency key for one pending set of one project.

    The same set never duplicates its question; a new queued item
    changes the set and opens a new round.
    """
    fingerprint = hashlib.sha256(
        "\n".join(sorted(
            f"{item.origin}/{item.name}/{item.sha256}"
            for item in pending)).encode("utf-8")).hexdigest()[:16]
    clean_project = str(project or "").strip().strip("/") or "project"
    return f"{CONFIRM_SOURCE_PREFIX}:{clean_project}:{fingerprint}"


def confirm_question_parts(
        pending: list[PendingConfirm]) -> dict[str, Any]:
    """Question, human action and checkpoint for one grouped block."""
    lines = [
        "Confirmación en bloque (2.5): estas propuestas normativas "
        f"pendientes se publican solo si las apruebas ({len(pending)}).",
        "",
    ]
    for item in pending:
        label = f"- {item.origin}/{item.name}: {item.target or '(sin destino)'}"
        if item.proposal_class:
            label += f" [{item.proposal_class}]"
        label += f" (sha256 {item.sha256[:16]}...)"
        lines.append(label)
        if item.excerpt:
            lines.append(f"    {item.excerpt}")
    items = [{"origin": item.origin, "name": item.name,
              "target": item.target, "sha256": item.sha256}
             for item in pending]
    return {
        "question": "\n".join(lines),
        "human_action": (
            f"Aprobar (publica exactamente lo mostrado) o rechazar "
            f"(descarta con el motivo) {len(pending)} propuesta(s) "
            f"normativa(s)"),
        "checkpoint": {CONFIRM_MARKER: items},
    }


def build_confirm_question(
        pending: list[PendingConfirm], *,
        project: str | None) -> dict[str, Any]:
    """Single owner-channel spec grouping every pending proposal."""
    if not pending:
        raise ValueError("a confirmation block needs at least one proposal")
    parts = confirm_question_parts(pending)
    return build_owner_question(
        kind="question",
        question=str(parts["question"]),
        human_action=str(parts["human_action"]),
        checkpoint=dict(parts["checkpoint"]),
        project=project,
        source_id=confirm_source_id(str(project or ""), pending),
    )


def confirm_items(block: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Items bound to a confirmation question, or [] when absent.

    Accepts the ``owner_question`` block itself (marker under
    ``checkpoint``) or the checkpoint mapping directly.
    """
    if not isinstance(block, Mapping):
        return []
    raw = block.get(CONFIRM_MARKER)
    if not isinstance(raw, list):
        checkpoint = block.get("checkpoint")
        raw = checkpoint.get(CONFIRM_MARKER) if isinstance(
            checkpoint, Mapping) else None
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        origin = str(entry.get("origin") or "")
        name = str(entry.get("name") or "")
        digest = str(entry.get("sha256") or "")
        if not origin or not name or len(digest) != 64:
            continue
        items.append({
            "origin": origin,
            "name": name,
            "target": str(entry.get("target") or ""),
            "sha256": digest,
        })
    return items


def is_confirm_question(
        task_or_block: Mapping[str, Any] | None) -> bool:
    """True when the mapping is (or carries) a 2.5 confirmation block."""
    if not isinstance(task_or_block, Mapping):
        return False
    params = task_or_block.get("params")
    block = (params.get("owner_question")
             if isinstance(params, Mapping) else None)
    if isinstance(block, Mapping):
        return bool(confirm_items(block))
    return bool(confirm_items(task_or_block))


def _load_queued(path: Path) -> tuple[KnowledgeProposal, bytes] | str:
    try:
        blob = path.read_bytes()
    except OSError as exc:
        return f"queued file is unreadable ({exc})"[:300]
    try:
        data = yaml.safe_load(blob.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        return f"queued file is not valid YAML ({exc})"[:300]
    if not isinstance(data, dict):
        return "queued file is not a YAML mapping"
    try:
        return KnowledgeProposal.model_validate(data), blob
    except Exception as exc:  # noqa: BLE001 - reason recorded, never raised
        return _schema_reason(exc)


def _queued_path(
        base: Path | None, origin: str, name: str) -> Path | None:
    try:
        folder = ticket_dir(origin, base_dir=base) / CONFIRM_QUEUE_DIRNAME
    except ValueError:
        return None
    for suffix in PROPOSAL_FILENAME_SUFFIXES:
        candidate = folder / f"{name}{suffix}"
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
        except OSError:
            continue
    return None


async def apply_approved_proposals(
        items: list[dict[str, Any]], *,
        config: PublishConfig,
        base_dir: str | Path | None) -> ConfirmOutcome:
    """Publish exactly the shown bytes (2.5 approve path).

    An item whose queued bytes no longer match the approved sha256 is
    skipped with its file kept: the approval no longer covers it. Items
    outside this block's project are skipped with the file kept, so a
    block never deletes foreign proposals. Gone files are reported, not
    silent. Never raises for proposal failures.
    """
    from orchestration.knowledge_proposals import CollectedProposal

    outcome = ConfirmOutcome()
    base = Path(base_dir) if base_dir is not None else _default_base()
    prefix = str(config.project or "").strip().strip("/")
    ready: list[CollectedProposal] = []
    for item in items:
        key = f"{item['origin']}/{item['name']}"
        path = _queued_path(base, item["origin"], item["name"])
        if path is None:
            try:
                ticket_dir(item["origin"], base_dir=base)
            except ValueError:
                outcome.rejected[key] = "invalid origin ticket id"
            else:
                outcome.missing.append(key)
            continue
        loaded = _load_queued(path)
        if isinstance(loaded, str):
            outcome.rejected[key] = loaded
            try:
                path.unlink()
            except OSError:
                pass
            continue
        _, blob = loaded
        if hashlib.sha256(blob).hexdigest() != item["sha256"]:
            outcome.skipped_changed.append(key)
            continue
        proposal, _ = loaded
        if (prefix and proposal.target
                and not _within_prefix(proposal.target, prefix)):
            outcome.skipped_foreign.append(key)
            continue
        ready.append(CollectedProposal(
            name=item["name"], source=path, target=proposal.target,
            action=proposal.action,
            proposal_class=proposal.proposal_class,
            base_version=proposal.base_version,
            evidence=[{key: value
                       for key, value in entry.model_dump().items() if value}
                      for entry in proposal.evidence
                      if entry.ticket or entry.commit or entry.path],
            content=proposal.content,
        ))
    published, deferred, rejected = await asyncio.to_thread(
        publish_approved, ready, config)
    published_set = set(published)
    by_target: dict[str, list[CollectedProposal]] = {}
    for entry in ready:
        by_target.setdefault(entry.target, []).append(entry)
    for target, entries in by_target.items():
        if target in published_set:
            first, rest = entries[0], entries[1:]
            outcome.published.append(
                f"{first.source.parent.parent.name}/{first.name}")
            try:
                if first.source.is_file():
                    first.source.unlink()
            except OSError:
                pass
            for entry in rest:
                key = f"{entry.source.parent.parent.name}/{entry.name}"
                outcome.deferred[key] = (
                    "same note published once in this round; kept")
        elif target in deferred:
            for entry in entries:
                key = f"{entry.source.parent.parent.name}/{entry.name}"
                outcome.deferred[key] = deferred[target]
        elif target in rejected:
            for entry in entries:
                key = f"{entry.source.parent.parent.name}/{entry.name}"
                outcome.rejected[key] = rejected[target]
                try:
                    if entry.source.is_file():
                        entry.source.unlink()
                except OSError:
                    pass
        else:
            for entry in entries:
                key = f"{entry.source.parent.parent.name}/{entry.name}"
                outcome.deferred[key] = "vault unavailable; retry later"
    return outcome


def apply_denied_proposals(
        items: list[dict[str, Any]], *,
        base_dir: str | Path | None) -> dict[str, list[str]]:
    """Discard every queued item of a denied block (2.5 deny path).

    Returns origin -> discarded names for per-ticket reason comments.
    Best-effort file removal: a failure is reported, never raised.
    """
    base = Path(base_dir) if base_dir is not None else _default_base()
    discarded: dict[str, list[str]] = {}
    for item in items:
        path = _queued_path(base, item["origin"], item["name"])
        if path is None:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        discarded.setdefault(item["origin"], []).append(item["name"])
    return discarded


def format_confirm_comment(
        outcome: ConfirmOutcome, *,
        approved: bool,
        owners_reason: str = "") -> str:
    """Ticket comment for a confirmation answer."""
    verdict = "aprobado" if approved else "denegado"
    lines = [f"Confirmación en bloque (2.5): bloque {verdict}: "
             f"{len(outcome.published)} publicadas, "
             f"{len(outcome.skipped_changed)} omitidas por cambio, "
             f"{len(outcome.skipped_foreign)} de otro proyecto, "
             f"{len(outcome.missing)} ya consumidas, "
             f"{len(outcome.deferred)} diferidas, "
             f"{len(outcome.rejected)} rechazadas."]
    if outcome.published:
        lines.append("Publicadas (exactamente lo mostrado):")
        lines.extend(f"- {name}" for name in sorted(outcome.published))
    if outcome.skipped_changed:
        lines.append("Omitidas: el contenido cambió tras la pregunta, "
                     "la aprobación ya no vale (siguen en cola):")
        lines.extend(f"- {name}"
                     for name in sorted(outcome.skipped_changed))
    if outcome.skipped_foreign:
        lines.append("De otro proyecto (siguen en cola para su ronda):")
        lines.extend(f"- {name}"
                     for name in sorted(outcome.skipped_foreign))
    if outcome.missing:
        lines.append("Ya consumidas en otra ronda (sin acción):")
        lines.extend(f"- {name}" for name in sorted(outcome.missing))
    if outcome.deferred:
        lines.append("Diferidas, propuesta conservada:")
        lines.extend(f"- {name}: {reason}"
                     for name, reason in sorted(outcome.deferred.items()))
    if outcome.rejected:
        lines.append("Rechazadas con motivo:")
        lines.extend(f"- {name}: {reason}"
                     for name, reason in sorted(outcome.rejected.items()))
    if owners_reason.strip():
        lines.append(f"Motivo del Owner: {owners_reason.strip()}"[:2000])
    return "\n".join(lines)[:8000]
