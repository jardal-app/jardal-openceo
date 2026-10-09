"""Frozen qualifying oracle for the simple tiers (TASK-2026-00116, 00148, 00178).

Routing policy §24: before a simple-tier build with a declared
qualifying oracle begins, the orchestrator registers the frozen oracle
references (reference/path + content hash + oracle type + ticket).
During/after the build the orchestrator verifies them and RUNS the
executable types; if the builder modified a protected reference, or the
oracle fails, the gate fails and the ticket must not close on it.

Only file-backed references are hash-protected. Semantic references that
do not resolve to a repo file are recorded unresolved: the verification
gate only covers protected references. ``builder_tests`` never qualifies
(§35) and is never frozen.

Running the oracle (``oracle_run_plan``): the executable types run through
the project's ``dev.oracle_cmd`` template, a required post-build gate with
the same retries as the other gates. The project owns the runner (pytest,
vitest, ...); without ``dev.oracle_cmd`` the oracle is hash-protected only
and the plan says so, so the limit is visible in the ticket steps.
Declared refs that resolve to no file never run; when the oracle runs
with the resolved ones, the caller names the rest via
``unresolved_oracle_refs`` so they figure as not verified (TASK-2026-00167).
"""

from __future__ import annotations

import hashlib
import os
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "EXECUTABLE_ORACLE_TYPES",
    "OracleRun",
    "QUALIFYING_ORACLE_TYPES",
    "freeze_oracle",
    "is_qualifying_oracle",
    "oracle_run_plan",
    "resolve_oracle_ref",
    "unresolved_oracle_refs",
    "validate_oracle_config",
    "verify_oracle",
]

#: Oracle types that may be frozen on any simple tier (policy §23 minus
#: ``builder_tests``, which never qualifies per §35).
QUALIFYING_ORACLE_TYPES = frozenset({
    "preexisting_tests",
    "contract_or_schema",
    "bug_reproduction",
    "separate_ticket_tests",
    "planner_executable_spec",
})

#: Qualifying types whose refs are executable tests/specs. ``contract_or_schema``
#: is a static artifact: it is hash-protected and exercised by the gates
#: that consume it, never run on its own.
EXECUTABLE_ORACLE_TYPES = frozenset({
    "preexisting_tests",
    "bug_reproduction",
    "separate_ticket_tests",
    "planner_executable_spec",
})

#: Default timeout of the oracle gate (``dev.oracle_timeout_minutes``).
DEFAULT_ORACLE_TIMEOUT_MINUTES = 10


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_qualifying_oracle(oracle: Any) -> bool:
    """True when the declared oracle may be frozen (qualifying type + refs)."""
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
    """Freeze the qualifying oracle before the build (orchestrator-side).

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


@dataclass(frozen=True)
class OracleRun:
    """What the orchestrator executes for a frozen oracle (policy §24)."""

    cmd: str
    timeout_minutes: int
    refs: tuple[str, ...]


def _oracle_timeout(dev_cfg: Mapping[str, Any] | None) -> int:
    raw = (dev_cfg or {}).get("oracle_timeout_minutes")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return DEFAULT_ORACLE_TIMEOUT_MINUTES


def oracle_run_plan(
    frozen: Any,
    dev_cfg: Mapping[str, Any] | None,
) -> tuple[OracleRun | None, str]:
    """``(run, note)``: the command to execute for ``frozen``, or why none.

    Policy §24 says the orchestrator, not the builder, runs the qualifying
    oracle. ``run`` is the post-build gate to append to the project gates;
    ``note`` is non-empty exactly when a frozen oracle will NOT be
    executed, and is written to the ticket steps so the limit is visible
    (hash-protected only). No frozen oracle: ``(None, "")``.

    The template is the project's ``dev.oracle_cmd`` with ``{refs}``
    replaced by the resolved refs, each shell-quoted (``path::node`` is
    kept: it is how a test runner selects one test). Plain replace, never
    ``str.format``: a runner command may contain braces.
    """
    if frozen is None:
        return None, ""
    if not isinstance(frozen, Mapping):
        return None, "frozen oracle record unreadable: not executed"
    oracle_type = str(frozen.get("oracle_type") or "")
    if oracle_type not in EXECUTABLE_ORACLE_TYPES:
        return None, (
            f"frozen oracle ({oracle_type or 'unknown type'}) is "
            "hash-protected only: a static artifact is verified by the "
            "gates that consume it, not run on its own")
    refs = list(dict.fromkeys(
        str(entry.get("ref"))
        for entry in frozen.get("refs") or []
        if isinstance(entry, Mapping) and entry.get("resolved")
        and entry.get("ref")))
    if not refs:
        return None, ("frozen oracle has no file-backed refs: "
                      "hash-protected only, nothing to execute")
    template = str((dev_cfg or {}).get("oracle_cmd") or "").strip()
    if not template:
        return None, (
            "frozen oracle is hash-protected only: dev.oracle_cmd is not "
            "configured for this project, so the orchestrator cannot run it")
    cmd = template.replace("{refs}", " ".join(shlex.quote(ref) for ref in refs))
    return OracleRun(cmd=cmd, timeout_minutes=_oracle_timeout(dev_cfg),
                     refs=tuple(refs)), ""


def unresolved_oracle_refs(frozen: Any) -> list[str]:
    """Raw labels of declared refs that resolved to no file ([] = none).

    The oracle runs its resolved refs only (``oracle_run_plan``); the rest
    stay unprotected and unverified (policy §34), so the caller names them
    in the ticket steps instead of hiding them behind an "oracle ok"
    verdict (TASK-2026-00167). Order-preserving, deduplicated; entries
    without a label show as ``?``. Nothing frozen or an unreadable record
    yields ``[]``: those cases already have their own visible note.
    """
    if not isinstance(frozen, Mapping) or not isinstance(
            frozen.get("refs"), list):
        return []
    return list(dict.fromkeys(
        str(entry.get("ref") or "?")
        for entry in frozen.get("refs") or []
        if isinstance(entry, Mapping) and not entry.get("resolved")))


def validate_oracle_config(dev_cfg: Mapping[str, Any] | None) -> list[str]:
    """Human-readable errors for ``dev.oracle_cmd`` / ``dev.oracle_timeout_minutes``.

    ``[]`` when valid or absent (an explicit null counts as absent). A
    project without ``oracle_cmd`` keeps working: its frozen oracles are
    hash-protected only, and the ticket steps say so.
    """
    cfg = dev_cfg or {}
    errors: list[str] = []
    command = cfg.get("oracle_cmd")
    if command is not None:
        if not isinstance(command, str) or not command.strip():
            errors.append("dev.oracle_cmd must be a non-empty string")
        elif "{refs}" not in command:
            errors.append("dev.oracle_cmd must contain the {refs} placeholder")
    timeout = cfg.get("oracle_timeout_minutes")
    if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int)
            or timeout <= 0):
        errors.append("dev.oracle_timeout_minutes must be a positive integer")
    return errors
