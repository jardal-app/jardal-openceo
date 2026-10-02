"""Explicit local export of selected conclusions; never mounted in model/runtime.

Callers must authorize the exact project's mapping and review the selected public
conclusion for durable value, secrets and personal data. No auto-export of reports.
Uses directory descriptors/O_NOFOLLOW, not a resolve-then-write pathname check.
"""
import os
import re
from pathlib import Path, PurePosixPath


class ExportError(RuntimeError):
    pass


def export_conclusion(*, event, project_key, owner_id, mappings, vault_root, slug,
                      claim_ids, authorized=False, reviewed_durable_public=False):
    if not authorized or not reviewed_durable_public:
        raise ExportError("explicit_local_authorization_required")
    if (event.get("project_key") != project_key or event.get("owner_id") != owner_id
            or event.get("classification") != "public" or event.get("status") != "completed"
            or event.get("freshness") == "stale"):
        raise ExportError("event_not_exportable")
    mapping = mappings.get(project_key)
    if mapping is None:
        raise ExportError("project_mapping_missing")
    parts = PurePosixPath(mapping).parts
    if (len(parts) not in {2, 3} or parts[0] != "projects" or
            any(p in {"..", ".", "shared"} or not re.fullmatch(r"[a-z0-9_-]+", p) for p in parts)):
        raise ExportError("invalid_project_mapping")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ExportError("invalid_note_slug")
    selected = [c for c in event.get("claims", []) if c["id"] in claim_ids]
    if not selected or len(selected) != len(set(claim_ids)):
        raise ExportError("invalid_selection")
    if any(c.get("verification") != "supported" or c.get("kind") != "fact" for c in selected):
        raise ExportError("unverified_conclusion")
    text = "\n\n".join(c["statement"] for c in selected)
    if any(marker in text for marker in ("[[", "![[", "<script", "BEGIN PRIVATE KEY", "auth_token=", "ct0=", "Bearer ")):
        raise ExportError("unsafe_note_content")
    if re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|sk-[a-zA-Z0-9_-]{12,}", text):
        raise ExportError("unsafe_note_content")
    root = Path(vault_root).absolute()
    # Reject symlinks in configured root and all ancestors before using descriptors;
    # each subsequent directory open is also no-follow to guard path replacement.
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ExportError("symlink_rejected")
    date = str(event["observed_at"])[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ExportError("invalid_event_date")
    org = f"org: {parts[1]}\n" if len(parts) == 3 else ""
    body = f"---\ntype: research\n{org}project: {parts[-1]}\ndate: {date}\n---\n\n# {slug.replace('-', ' ')}\n\n{text}\n\nEvidencia: evento `{event['event_id']}`, research `{event['research_id']}`.\n"
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (*parts, "research"):
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new
        file_fd = os.open(slug + ".md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(file_fd, "w") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ExportError("destination_unavailable_or_unsafe") from None
    finally:
        os.close(fd)
    return {"project": mapping, "note": f"{mapping}/research/{slug}.md"}
