"""One-shot migration out of the legacy secretary notes collection.

Scope: TASK-2026-00220 retires ``secretary.save_note`` /
``secretary.query_notes`` so the Mongo ``notes`` collection stops being a
third memory (besides the vault and Horizon). Stored notes move exactly
once, verified with counts:

- personal (no project) -> Horizon capture (channel ``import``);
- company with a ``ticket:TASK-...`` tag -> a comment on that ticket;
- other company notes -> a markdown note under the project's vault path.

Move semantics: a note leaves ``notes`` only after its sink write (or a
proven duplicate) succeeds. Refused and failed notes stay, named by id
with their reason and never with their content. Dry-run is the default;
``--apply`` executes.

Crash window (documented, narrow): a crash between a ticket-comment
write and the source delete re-posts that comment on rerun (the
migration marker inside makes the duplicate visible). Horizon dedupes
by content and the vault writer refuses existing files, so those two
sinks never duplicate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NOTES_COLLECTION = "notes"
MIGRATION_TICKET = "TASK-2026-00220"
ORIGIN_PREFIX = "notes-migration"
TICKET_TAG_PREFIX = "ticket:"
TASK_PATTERN = r"^TASK-2026-\d+$"
_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}"
_MAPPING_PART = r"[a-z0-9_-]+"
_SECRET_TEXT_MARKERS = ("BEGIN PRIVATE KEY", "auth_token=", "ct0=",
                        "Bearer ")
_SECRET_KEY_PATTERN = re.compile(r"sk-[a-zA-Z0-9_-]{12,}")


class NoteMigrationError(RuntimeError):
    """The vault writer refused or failed; the note stays."""


class NoteDuplicateError(NoteMigrationError):
    """The vault already holds this note; the source may go."""


class NoteRefusedError(NoteMigrationError):
    """The vault writer refused the content; the note stays."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classify_note(note: dict[str, Any]) -> str:
    """Company notes name a project; the rest is personal."""
    project = str(note.get("project") or "").strip()
    return "company" if project else "personal"


def ticket_tag_target(note: dict[str, Any]) -> str | None:
    """First ``ticket:TASK-...`` tag, validated; None without one."""
    tags = note.get("tags") or []
    if not isinstance(tags, list):
        return None
    for tag in tags:
        text = str(tag or "").strip()
        if not text.startswith(TICKET_TAG_PREFIX):
            continue
        candidate = text[len(TICKET_TAG_PREFIX):].strip()
        if re.fullmatch(TASK_PATTERN, candidate):
            return candidate
    return None


def note_has_secret(note: dict[str, Any]) -> bool:
    """High-confidence secret markers only; mail text stays welcome."""
    content = str(note.get("content") or "")
    if any(marker in content for marker in _SECRET_TEXT_MARKERS):
        return True
    return bool(_SECRET_KEY_PATTERN.search(content))


def migration_marker(note_id: str) -> str:
    return f"<!-- {ORIGIN_PREFIX}:{note_id} -->"


def _note_date(note: dict[str, Any]) -> str:
    created = note.get("created_at")
    text = ""
    if hasattr(created, "date"):
        try:
            text = str(created.date())  # type: ignore[union-attr]
        except ValueError:
            text = ""
    elif created is not None:
        text = str(created)
    match = re.match(_DATE_PATTERN, text)
    if match:
        return match.group(0)
    return date.today().isoformat()


def _check_mapping(mapping: str) -> tuple[str, ...]:
    parts = PurePosixPath(mapping).parts
    if (len(parts) not in {2, 3} or parts[0] != "projects"
            or any(part in {"..", ".", "shared"}
                   or not re.fullmatch(_MAPPING_PART, part)
                   for part in parts)):
        raise NoteMigrationError(f"invalid_project_mapping: {mapping!r}")
    return parts


def _note_title(content: str) -> str:
    for line in str(content or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    return "Nota migrada"


def _file_holds_note(path: Path, note_id: str) -> bool:
    """True when an existing vault file is this very note's migration."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            head = stream.read(2048)
    except OSError:
        return False
    return f"migrated_from: notes/{note_id}" in head


def write_company_note(vault_root: Path | str, mapping: str, slug: str, *,
                       project: str, date: str, tags: list[str],
                       note_id: str, content: str) -> str:
    """Write one migrated company note; never overwrite, never follow links.

    Returns the vault-relative path. Raises :class:`NoteDuplicateError`
    when the destination already holds this note (marker-verified) and
    :class:`NoteMigrationError` (or its ``NoteRefusedError`` with
    ``reason``) when the destination or the content is unacceptable — a
    file of a *different* note under the same slug is a ``slug_collision``
    refusal, never a duplicate (review 00015).
    """
    if not re.fullmatch(_SLUG_PATTERN, slug):
        raise NoteMigrationError(f"invalid_note_slug: {slug!r}")
    if note_has_secret({"content": content}):
        raise NoteRefusedError("secrets_suspect")
    parts = _check_mapping(mapping)
    raw_root = Path(vault_root).expanduser().absolute()
    if any(part.is_symlink() for part in (raw_root, *raw_root.parents)):
        raise NoteMigrationError("symlink_rejected")
    root = raw_root.resolve()
    target_dir = root.joinpath(*parts)
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved = target_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise NoteMigrationError("destination_outside_vault")
    target = resolved / f"{slug}.md"
    if target.is_symlink() or target.exists():
        if _file_holds_note(target, note_id):
            raise NoteDuplicateError(str(target))
        raise NoteRefusedError("slug_collision")
    tags_line = "[" + ", ".join(f'"{tag}"' for tag in tags) + "]"
    body = (
        "---\n"
        "type: note\n"
        f"project: {project}\n"
        f"date: {date}\n"
        f"tags: {tags_line}\n"
        f"migrated_from: notes/{note_id}\n"
        "---\n"
        "\n"
        f"# {_note_title(content)}\n"
        "\n"
        f"{content.rstrip()}\n"
        "\n"
        f"Migrated from the legacy secretary notes collection "
        f"(`notes/{note_id}`, {MIGRATION_TICKET}).\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o666)
    except FileExistsError:
        if _file_holds_note(target, note_id):
            raise NoteDuplicateError(str(target)) from None
        raise NoteRefusedError("slug_collision") from None
    except OSError as exc:
        raise NoteMigrationError(f"destination_unavailable: {exc}") from None
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(body)
    return str(resolved.relative_to(root) / f"{slug}.md")


def _slug_for_note(note_id: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]", "", note_id.lower())[:12]
    if not cleaned:
        raise NoteMigrationError("invalid_note_id")
    return f"nota-{cleaned}"


def _ticket_comment(note_id: str, content: str) -> str:
    return (f"{migration_marker(note_id)}\n\n{content.rstrip()}\n\n"
            f"_Migrated from the legacy secretary notes collection "
            f"(`notes/{note_id}`, {MIGRATION_TICKET})._")


async def migrate_notes(notes: Any, *, horizon: Any, tickets: Any,
                        vault_root: Path | str,
                        mappings: dict[str, str],
                        dry_run: bool = True,
                        channel: Any = None) -> dict[str, Any]:
    """Move every stored note to its sink; return the exact counts.

    ``notes`` is the motor ``notes`` collection (``find`` + ``delete_one``
    only); ``horizon`` answers ``capture`` like ``HorizonService`` and
    ``tickets`` answers ``get_ticket``/``add_comment`` like the Jardal ERP
    adapter. ``mappings`` routes company projects to vault paths
    (``{"demo": "projects/jardal/demo"}``); ``channel`` defaults to the
    Horizon ``import`` channel.
    """
    if channel is None:
        from horizon.models import CaptureChannel
        channel = CaptureChannel.IMPORT
    from horizon.service import _MAX_TEXT_CHARS as horizon_text_limit

    result: dict[str, Any] = {
        "total": 0, "personal": 0, "company_vault": 0,
        "company_ticket": 0, "duplicates": 0,
        "refused": [], "errors": [], "dry_run": bool(dry_run),
    }
    async for doc in notes.find({}):
        note = dict(doc)
        result["total"] += 1
        note_id = str(note.get("note_id") or "")
        if not note_id:
            result["errors"].append({"note_id": "", "reason": "missing_id"})
            continue
        content = str(note.get("content") or "")
        target = classify_note(note)
        if not content.strip():
            result["refused"].append({"note_id": note_id,
                                      "reason": "empty", "target": target})
            continue
        if note_has_secret(note):
            ticket = ticket_tag_target(note)
            sink = "ticket" if target == "company" and ticket else (
                "vault" if target == "company" else "personal")
            result["refused"].append({"note_id": note_id,
                                      "reason": "secrets_suspect",
                                      "target": sink})
            continue
        # Horizon capture truncates at its text limit: a longer personal
        # note refuses (kept) instead of migrating without its tail.
        if (target == "personal"
                and len(content.strip()) > horizon_text_limit):
            result["refused"].append({"note_id": note_id,
                                      "reason": "overlong",
                                      "target": "personal"})
            continue
        ticket = ticket_tag_target(note) if target == "company" else None
        if dry_run:
            if target == "personal":
                result["personal"] += 1
            elif ticket:
                result["company_ticket"] += 1
            else:
                result["company_vault"] += 1
            continue
        try:
            if target == "personal":
                outcome = await horizon.capture(
                    str(note.get("user") or "admin"), content,
                    channel=channel,
                    origin=f"{ORIGIN_PREFIX}:{note_id}", dedupe=True)
                if outcome.get("duplicate") and not outcome.get("captured"):
                    result["duplicates"] += 1
                elif outcome.get("captured"):
                    result["personal"] += 1
                else:
                    result["errors"].append(
                        {"note_id": note_id, "reason": "capture_refused"})
                    continue
            elif ticket:
                exists = await tickets.get_ticket(ticket)
                if exists is None:
                    result["refused"].append(
                        {"note_id": note_id, "reason": "missing_ticket",
                         "target": "ticket"})
                    continue
                await tickets.add_comment(
                    ticket, _ticket_comment(note_id, content))
                result["company_ticket"] += 1
            else:
                mapping = mappings.get(str(note.get("project") or ""))
                if not mapping:
                    result["refused"].append(
                        {"note_id": note_id, "reason": "mapping_missing",
                         "target": "vault"})
                    continue
                try:
                    write_company_note(
                        vault_root, mapping, _slug_for_note(note_id),
                        project=str(note.get("project") or ""),
                        date=_note_date(note),
                        tags=[str(tag) for tag in note.get("tags") or []
                              if str(tag or "").strip()],
                        note_id=note_id, content=content)
                except NoteDuplicateError:
                    result["duplicates"] += 1
                else:
                    result["company_vault"] += 1
        except NoteRefusedError as exc:
            result["refused"].append(
                {"note_id": note_id, "reason": exc.reason,
                 "target": "vault" if target == "company" else target})
            continue
        except NoteMigrationError as exc:
            result["errors"].append(
                {"note_id": note_id, "reason": str(exc)[:80]})
            continue
        except Exception:
            reason = ("capture_failed" if target == "personal"
                      else "sink_failed")
            result["errors"].append({"note_id": note_id, "reason": reason})
            continue
        try:
            await notes.delete_one({"note_id": note_id})
        except Exception:
            result["errors"].append(
                {"note_id": note_id, "reason": "delete_failed"})
    return result


def _load_mappings(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise NoteMigrationError("mappings file must hold an object")
    mappings = {str(key): str(value) for key, value in data.items()}
    for project, mapping in mappings.items():
        _check_mapping(mapping)
    return mappings


def _print_human(result: dict[str, Any]) -> None:
    mode = "dry-run" if result["dry_run"] else "applied"
    print(f"notes migration ({mode}): total={result['total']} "
          f"personal={result['personal']} "
          f"vault={result['company_vault']} "
          f"ticket={result['company_ticket']} "
          f"duplicates={result['duplicates']} "
          f"refused={len(result['refused'])} "
          f"errors={len(result['errors'])}")
    for entry in result["refused"]:
        print(f"refused {entry['note_id']}: {entry['reason']} "
              f"(target {entry['target']})")
    for entry in result["errors"]:
        print(f"error {entry['note_id']}: {entry['reason']}")


def main(argv: Sequence[str] | None = None) -> int:
    """Dry-run the migration by default; ``--apply`` moves the notes."""
    parser = argparse.ArgumentParser(
        description="Migrate the legacy secretary notes out of Mongo.")
    parser.add_argument("--apply", action="store_true",
                        help="move notes; without it, only count")
    parser.add_argument("--mappings", default=None,
                        help="JSON file {project_key: vault/path}")
    parser.add_argument("--json", action="store_true",
                        help="print the counts as JSON")
    args = parser.parse_args(argv)

    from db.mongo import MongoRepository
    from horizon.service import HorizonService
    from settings import get_settings
    from tools.jardal_erp_tickets import JardalERPTicketAdapter

    try:
        mappings = _load_mappings(args.mappings)
    except (OSError, ValueError, NoteMigrationError) as exc:
        print(f"mappings: {exc}")
        return 2

    async def _run() -> dict[str, Any]:
        settings = get_settings()
        repo = MongoRepository(settings)
        await repo.connect()
        tickets = JardalERPTicketAdapter(settings)
        try:
            horizon = HorizonService(settings, repo)
            return await migrate_notes(
                repo.db[NOTES_COLLECTION], horizon=horizon,
                tickets=tickets, vault_root=settings.VAULT_ROOT,
                mappings=mappings, dry_run=not args.apply)
        finally:
            await tickets.aclose()
            await repo.close()

    result = asyncio.run(_run())
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        _print_human(result)
    if result["errors"] or result["refused"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
