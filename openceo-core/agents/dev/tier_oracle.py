"""Frozen qualifying oracle for Tier C (autonomy F1, TASK-2026-00116).

Routing policy §24: before a qualifying C build begins, the orchestrator
registers the frozen oracle references (reference/path + content hash +
oracle type + ticket). During/after the build the orchestrator verifies
them; if the builder modified a protected reference, the gate fails and
the ticket must not close as C.

Only file-backed references are hash-protected. Semantic references that
do not resolve to a repo file are recorded unresolved: the verification
gate only covers protected references. ``builder_tests`` never qualifies
(§35) and is never frozen.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "QUALIFYING_ORACLE_TYPES",
    "freeze_oracle",
    "is_qualifying_oracle",
    "resolve_oracle_ref",
    "verify_oracle",
]

#: Oracle types that may qualify Tier C work (policy §23 minus
#: ``builder_tests``, which never qualifies per §35).
QUALIFYING_ORACLE_TYPES = frozenset({
    "preexisting_tests",
    "contract_or_schema",
    "bug_reproduction",
    "separate_ticket_tests",
    "planner_executable_spec",
})


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_qualifying_oracle(oracle: Any) -> bool:
    """True when the planner oracle may qualify Tier C (type + refs)."""
    if not isinstance(oracle, Mapping):
        return False
    if str(oracle.get("type") or "") not in QUALIFYING_ORACLE_TYPES:
        return False
    refs = oracle.get("refs")
    return isinstance(refs, list) and len(refs) > 0


def resolve_oracle_ref(repo: Path, ref: str) -> str | None:
    """Resolve a planner ref to a repo-relative file path, or None.

    Accepts ``path`` and ``path::node`` (test node ids); rejects absolute
    paths outside the repo and ``..`` escapes. Directories and missing
    files do not resolve.
    """
    candidate = str(ref or "").split("::", 1)[0].strip()
    if not candidate:
        return None
    path = Path(candidate)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(repo.resolve())
        except (OSError, ValueError):
            return None
    else:
        if ".." in path.parts:
            return None
    target = repo / path
    try:
        if not target.is_file():
            return None
        target.resolve().relative_to(repo.resolve())
    except (OSError, ValueError):
        return None
    return os.fspath(path).replace(os.sep, "/")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def freeze_oracle(
    repo: Path,
    base_sha: str,
    oracle: Any,
    task_ref: str,
) -> dict[str, Any] | None:
    """Freeze the qualifying oracle before the C build (orchestrator-side).

    Returns the frozen record (policy §24: reference/path, content hash,
    oracle type, ticket) or None when there is no qualifying oracle to
    protect. Call once, on the pristine work branch, before the builder
    runs: worktree content then equals ``base_sha`` content.
    """
    if not is_qualifying_oracle(oracle):
        return None
    refs: list[dict[str, Any]] = []
    for entry in oracle.get("refs") or []:
        raw = str((entry or {}).get("ref") or "") if isinstance(
            entry, Mapping) else str(entry or "")
        path = resolve_oracle_ref(repo, raw)
        if path is None:
            refs.append({"ref": raw, "resolved": False})
            continue
        try:
            digest = _sha256_file(repo / path)
        except OSError:
            refs.append({"ref": raw, "resolved": False})
            continue
        refs.append({"ref": raw, "path": path, "sha256": digest,
                     "resolved": True})
    return {
        "oracle_type": str(oracle.get("type")),
        "task_ref": str(task_ref or ""),
        "base_sha": str(base_sha or ""),
        "frozen_at": _utcnow_iso(),
        "refs": refs,
    }


def verify_oracle(repo: Path, frozen: Any) -> list[str]:
    """Protected refs the builder modified or deleted (empty = intact).

    Only resolved file-backed references are checked; unresolved ones
    were never hash-protected. Malformed records fail closed: an
    unreadable frozen record cannot prove integrity. None means no
    qualifying oracle was frozen, so nothing is protected.
    """
    if frozen is None:
        return []
    if not isinstance(frozen, Mapping) or not isinstance(
            frozen.get("refs"), list):
        return ["oracle record unreadable"]
    modified: list[str] = []
    for entry in frozen.get("refs") or []:
        if not isinstance(entry, Mapping) or not entry.get("resolved"):
            continue
        label = str(entry.get("ref") or entry.get("path") or "?")
        path = str(entry.get("path") or "")
        expected = str(entry.get("sha256") or "")
        if not path or not expected or ".." in Path(path).parts:
            modified.append(label)
            continue
        target = repo / Path(path)
        try:
            if not target.is_file():
                modified.append(label)
                continue
            if _sha256_file(target) != expected:
                modified.append(label)
        except OSError:
            modified.append(label)
    return sorted(modified)
