"""Bounded vault index for Dev prompts (TASK-2026-00329, D6 / principle 8).

At the start of each ticket Dev receives a bounded index of the active
project hub and ``practices/`` — title, relative path and one line per
note — never note bodies. The detail stays on demand inside the
read-only mount wired by TASK-2026-00328.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from orchestration.agent_sandbox import vault_project_read_root

__all__ = [
    "VAULT_INDEX_MAX_CHARS",
    "VAULT_INDEX_BLURB_CHARS",
    "build_vault_index",
]

#: Agent decision (TASK-2026-00329): hard cap for the rendered index.
#: The real project index is ~1.5 KB; 4000 leaves room to grow while
#: keeping the prompt bounded. Over the cap the index truncates with a
#: notice and the detail is never loaded.
VAULT_INDEX_MAX_CHARS = 4000

#: One-line blurb cap per entry.
VAULT_INDEX_BLURB_CHARS = 160


def _title_and_blurb(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.stem, ""
    lines = text.splitlines()
    pos = 0
    if lines and lines[0].strip() == "---":
        end = next(
            (idx for idx in range(1, len(lines))
             if lines[idx].strip() in ("---", "...")),
            len(lines),
        )
        pos = end + 1
    title = ""
    blurb = ""
    for line in lines[pos:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if not title and stripped.startswith("# "):
                title = stripped[2:].strip()
            continue
        if not blurb:
            blurb = stripped
        if title and blurb:
            break
    if not title:
        for line in lines[pos:]:
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                title = stripped
                break
    return title or path.stem, blurb[:VAULT_INDEX_BLURB_CHARS]


def _iter_notes(root: Path) -> list[tuple[str, Path]]:
    candidates = [root / f"{root.name}.md"]
    practices = root / "practices"
    try:
        # A symlinked practices/ dir would read another folder's notes.
        if not practices.is_symlink() and practices.is_dir():
            candidates.extend(sorted(practices.glob("*.md")))
    except OSError:
        pass
    try:
        resolved_root = root.resolve()
    except OSError:
        resolved_root = root
    notes: list[tuple[str, Path]] = []
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if resolved_root not in candidate.resolve().parents:
                continue
        except OSError:
            continue
        notes.append((str(candidate.relative_to(root)), candidate))
    return notes


def build_vault_index(
    project_config: Mapping[str, object] | None,
    max_chars: int = VAULT_INDEX_MAX_CHARS,
) -> str:
    """Render the bounded hub + practices index, or ``""`` without vault.

    Never fails: a missing mapping, an unreadable vault, or a project
    without hub and ``practices/`` yields ``""`` so the prompt runs
    exactly as before. Note bodies are never included — one line each.
    """
    try:
        roots = vault_project_read_root(project_config)
    except Exception:
        return ""
    if not roots:
        return ""
    root = Path(roots[0])
    try:
        if not root.is_dir():
            return ""
        notes = _iter_notes(root)
    except OSError:
        return ""
    if not notes:
        return ""
    header = ("KNOWLEDGE INDEX (hub + practices titles, one line each; "
              "read the pertinent notes on demand from the read-only "
              "project folder, never load the whole vault):")
    entries: list[str] = []
    for rel, path in notes:
        title, blurb = _title_and_blurb(path)
        entries.append(f"- {title} ({rel}): {blurb}" if blurb
                       else f"- {title} ({rel})")
    body = header + "\n" + "\n".join(entries)
    if len(body) <= max_chars:
        return body
    notice = (f"...(index truncated to {max_chars} chars: "
              "detail not loaded; read notes on demand)")
    allowed = max_chars - len(notice)
    if allowed <= 0:
        return notice[:max_chars]
    truncated = header + "\n"
    if len(truncated) > allowed:
        truncated = truncated[:allowed]
    else:
        for entry in entries:
            if len(truncated) + len(entry) + 1 > allowed:
                break
            truncated += entry + "\n"
    return truncated + notice
