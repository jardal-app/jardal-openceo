"""Ticket handoff: real outputs travel between tickets (TASK-2026-00211).

The planner freezes INPUTS as text before the previous ticket ran, so
every stage but dev (which reads the repo) guesses. On close, the
ticket's files land as private Jardal ERP attachments and its data in the
execution record (``params.ticket_outputs``); on launch, the executor
resolves the ``blocked_by`` predecessors and delivers their recorded
outputs — staged files plus a prompt section. Playbooks do not exist
yet (TASK-2026-00217): the plan's order is the delivery map, and every
completed predecessor's outputs travel whole — no fuzzy match against
the plan's free-text inputs, the agent decides like with its skill.

All entry points are deterministic and never raise past their caller:
publish marks per-file failures in the record, resolve marks unknown
or unfinished predecessors as skipped, and staging verifies the sha
before copying.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from orchestration.agent_sandbox import ticket_dir

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_OUTPUT_FILES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_TEXT_CHARS",
    "TICKET_OUTPUTS_KEY",
    "UPSTREAM_DIRNAME",
    "UPSTREAM_OUTPUTS_KEY",
    "collect_ticket_outputs",
    "publish_ticket_outputs",
    "render_upstream_section",
    "resolve_upstream_outputs",
    "stage_upstream_files",
]

#: Execution-record key carrying the published outputs.
TICKET_OUTPUTS_KEY = "ticket_outputs"

#: Exact-skill params key carrying the resolved upstream outputs.
UPSTREAM_OUTPUTS_KEY = "upstream_outputs"

#: Ticket-folder dir the next ticket's staged files land under.
UPSTREAM_DIRNAME = "upstream"

#: Caps: a verdict listing more files than this still closes.
MAX_OUTPUT_FILES = 20

#: One attached file stays under this (Jardal ERP allows 10 MiB; prompts
#: and Mongo rows stay usable far below it).
MAX_OUTPUT_FILE_BYTES = 256 * 1024

#: One stored text stays under this; verdict lines are short by nature.
MAX_TEXT_CHARS = 8000

#: Collected names never escape into these.
_SKIPPED_TOP_DIRS = frozenset({UPSTREAM_DIRNAME, ".codex", "skill_drafts"})


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + "…(truncated)"


def _is_internal_name(raw: str) -> bool:
    """Folder mechanics, never an output: hidden, staged or draft paths."""
    base = Path(raw)
    return not base.parts or base.parts[0] in _SKIPPED_TOP_DIRS \
        or any(part.startswith(".") and part not in (".", "..")
               for part in base.parts)


def _safe_relative(ticket_folder: Path, raw: str) -> Path | None:
    """A verdict path resolving inside the ticket folder, else None."""
    candidate = str(raw or "").strip()
    if not candidate:
        return None
    base = Path(candidate)
    if base.is_absolute() or ".." in base.parts \
            or _is_internal_name(candidate):
        return None
    link = ticket_folder / base
    if link.is_symlink():
        return None
    try:
        resolved = link.resolve()
        resolved.relative_to(ticket_folder.resolve())
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def collect_ticket_outputs(
    verdict: Mapping[str, Any],
    ticket_folder: str | Path | None,
    declared_outputs: Sequence[str] = (),
) -> dict[str, Any]:
    """Split a verdict into file outputs, texts and evidence.

    An entregable naming a file inside the ticket folder becomes a
    file output (name, sha256, size); anything else stays text, since
    the string itself may still mean something downstream. Only folder
    mechanics (hidden, staged-upstream and draft paths) and symlinks
    drop out with a note. Files carry ``attachment: None`` until
    publish fills them.
    """
    folder = Path(ticket_folder) if ticket_folder is not None else None
    items = verdict.get("entregables") or []
    if not isinstance(items, list):
        items = []
    files: list[dict[str, Any]] = []
    texts: list[str] = []
    notes: list[str] = []
    if folder is None or not folder.is_dir():
        notes.append("no ticket folder: every entregable stays text")
        folder = None
    kept = 0
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if _is_internal_name(text):
            notes.append(f"dropped internal path: {text}")
            continue
        target = _safe_relative(folder, text) if folder is not None else None
        if target is None:
            probe = Path(text)
            if folder is not None and not probe.is_absolute() \
                    and ".." not in probe.parts \
                    and (folder / probe).is_symlink():
                notes.append(f"dropped symlink: {text}")
                continue
            texts.append(_truncate(text))
            continue
        try:
            size = target.stat().st_size
        except OSError:
            texts.append(_truncate(text))
            continue
        if size > MAX_OUTPUT_FILE_BYTES:
            files.append({"name": text, "sha256": "", "size": size,
                          "attachment": None,
                          "skipped": f"over {MAX_OUTPUT_FILE_BYTES} bytes"})
            continue
        if kept >= MAX_OUTPUT_FILES:
            files.append({"name": text, "sha256": "", "size": size,
                          "attachment": None,
                          "skipped": f"over {MAX_OUTPUT_FILES} files"})
            continue
        try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            texts.append(_truncate(text))
            continue
        kept += 1
        files.append({"name": text, "sha256": digest, "size": size,
                      "attachment": None, "skipped": None})
    evidence = verdict.get("evidencia") or ""
    return {
        "declared": [str(name) for name in declared_outputs],
        "texts": texts,
        "evidence": _truncate(str(evidence)) if str(evidence).strip() else "",
        "files": files,
        "notes": notes,
    }


async def publish_ticket_outputs(
    *,
    agent: str,
    skill: str | None,
    verdict: Mapping[str, Any],
    ticket_folder: str | Path | None,
    mirror: str | None,
    attach: Callable[..., Any] | None,
    declared_outputs: Sequence[str] = (),
) -> dict[str, Any]:
    """Collect outputs and attach the files to the business mirror.

    Files travel as private attachments (idempotent by content hash on
    the adapter); the returned record lands in the execution record.
    Never raises: every failure is marked in the record.
    """
    try:
        record = collect_ticket_outputs(verdict, ticket_folder,
                                        declared_outputs)
        record["agent"] = str(agent or "")
        record["skill"] = skill
        record["verdict"] = str(verdict.get("resultado") or "")
        record["published_at"] = datetime.now(timezone.utc).isoformat()
        if not mirror or attach is None:
            return record
        folder = Path(ticket_folder) if ticket_folder is not None else None
        for entry in record["files"]:
            if entry["skipped"] is not None:
                continue
            target = _safe_relative(folder, entry["name"]) \
                if folder is not None else None
            if target is None:
                entry["skipped"] = "file left the ticket folder"
                continue
            try:
                content = target.read_bytes()
                answer = await attach(
                    mirror, content_base64=base64.b64encode(
                        content).decode("ascii"),
                    file_name=entry["name"], private=True)
            except Exception as exc:  # noqa: BLE001 - marked, never raised
                entry["skipped"] = f"attach failed: {exc}"
                continue
            view = (answer or {}).get("attachment") \
                if isinstance(answer, dict) else None
            ref = ""
            if isinstance(view, dict):
                ref = str(view.get("name") or view.get("file_name") or "")
            entry["attachment"] = {
                "duplicate": bool((answer or {}).get("duplicate", False))
                if isinstance(answer, dict) else False,
                "ref": ref,
            }
        return record
    except Exception as exc:  # noqa: BLE001 - publish never fails a close
        logger.warning("publish_ticket_outputs failed: %s", exc)
        return {"agent": str(agent or ""), "skill": skill,
                "verdict": str(verdict.get("resultado") or ""),
                "declared": [str(name) for name in declared_outputs],
                "texts": [], "evidence": "", "files": [],
                "notes": [], "error": str(exc)}


def _skipped_entry(task_id: str, reason: str) -> dict[str, Any]:
    return {"task_id": task_id, "title": "", "verdict": "", "skill": None,
            "declared": [], "texts": [], "evidence": "", "files": [],
            "source_dir": None, "skipped": reason}


async def resolve_upstream_outputs(
    repo: Any,
    blocked_by: Sequence[str],
    base_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Read the recorded outputs of completed predecessors, in order."""
    resolved: list[dict[str, Any]] = []
    for dep in blocked_by:
        task_id = str(dep or "").strip()
        if not task_id:
            continue
        try:
            row = await repo.get_task(task_id)
        except Exception as exc:  # noqa: BLE001 - skipped, never raised
            resolved.append(_skipped_entry(
                task_id, f"unreadable row: {exc}"))
            continue
        if not isinstance(row, dict):
            resolved.append(_skipped_entry(task_id, "unknown ticket"))
            continue
        status = str(row.get("status") or "")
        if status != "completed":
            resolved.append(_skipped_entry(
                task_id, f"status {status or 'unknown'}, no outputs"))
            continue
        stored = (row.get("params") or {}).get(TICKET_OUTPUTS_KEY)
        if not isinstance(stored, dict):
            resolved.append(_skipped_entry(
                task_id, "no recorded outputs"))
            continue
        try:
            folder = ticket_dir(task_id, base_dir=(
                Path(base_dir) if base_dir is not None else None))
            source = str(folder) if folder.is_dir() else None
        except (ValueError, OSError):
            source = None
        files = stored.get("files") or []
        resolved.append({
            "task_id": task_id,
            "title": str(row.get("title") or ""),
            "verdict": str(stored.get("verdict") or ""),
            "skill": stored.get("skill"),
            "declared": list(stored.get("declared") or []),
            "texts": [str(item) for item in (stored.get("texts") or [])],
            "evidence": str(stored.get("evidence") or ""),
            "files": [dict(entry) for entry in files
                      if isinstance(entry, dict)],
            "source_dir": source,
            "skipped": None,
        })
    return resolved


def render_upstream_section(
    resolved: Sequence[Mapping[str, Any]],
    staged: Collection[tuple[str, str]] = (),
) -> str:
    """Prompt block with the predecessors' real outputs ("" when none)."""
    if not resolved:
        return ""
    lines = ["ENTRADAS DE TICKETS ANTERIORES (salidas reales, no adivinar)"]
    for entry in resolved:
        task_id = str(entry.get("task_id") or "")
        skipped = entry.get("skipped")
        if skipped:
            lines.append(f"- {task_id}: sin salidas ({skipped})")
            continue
        title = str(entry.get("title") or "").strip()
        head = f"- {task_id} ({entry.get('verdict')})"
        if title:
            head += f": {title}"
        lines.append(head)
        for text in entry.get("texts") or []:
            lines.append(f"  - {text}")
        evidence = str(entry.get("evidence") or "").strip()
        if evidence:
            lines.append(f"  evidencia: {evidence}")
        for item in entry.get("files") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if item.get("skipped"):
                lines.append(f"  - {name} (no disponible: "
                             f"{item['skipped']})")
            elif (task_id, name) in set(staged):
                lines.append(f"  - {name} (en "
                             f"{UPSTREAM_DIRNAME}/{task_id}/{name})")
            else:
                lines.append(f"  - {name} (sha "
                             f"{str(item.get('sha256') or '')[:12]})")
    return "\n".join(lines)


def stage_upstream_files(
    resolved: Sequence[Mapping[str, Any]],
    ticket_folder: str | Path,
) -> list[tuple[str, str]]:
    """Copy verified predecessor files under ``upstream/<task_id>/``.

    A file lands only when it is still there and its sha matches the
    record; anything else stays out. Returns the staged pairs.
    """
    folder = Path(ticket_folder)
    staged: list[tuple[str, str]] = []
    for entry in resolved:
        if entry.get("skipped"):
            continue
        task_id = str(entry.get("task_id") or "")
        source_dir = entry.get("source_dir")
        if not task_id or not source_dir:
            continue
        base = Path(str(source_dir))
        for item in entry.get("files") or []:
            if not isinstance(item, dict) or item.get("skipped"):
                continue
            name = str(item.get("name") or "")
            target = _safe_relative(base, name)
            if target is None:
                continue
            try:
                content = target.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(content).hexdigest() != item.get("sha256"):
                logger.warning("upstream file %s/%s changed since close, "
                               "not staged", task_id, name)
                continue
            dest = folder / UPSTREAM_DIRNAME / task_id / name
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(content)
            except OSError:
                continue
            staged.append((task_id, name))
    return staged
