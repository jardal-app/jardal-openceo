"""Vault publisher: close-time proposals into project notes (TASK-2026-00331).

D7/D8: autonomous agents never write durable memory. They leave proposals
in their ticket folder (2.3); this publisher — outside the sandbox, at the
same close point as the crystallization save — validates and publishes
them with no human intervention. A failed publication keeps the proposal
for retry and never blocks the ticket close.

Per proposal, structurally valid ones from ``knowledge_proposals`` take
exactly one path:

* descriptive with checked evidence -> published (commit, no push);
* normative (work rules for future agents) -> confirmation queue for 2.5;
* descriptive touching permissions, obligations, policies or mandate ->
  same queue, flagged for express Owner approval;
* transient infrastructure state (busy vault, changed base version,
  uncommitted note) -> kept for a later retry;
* permanent validation failures -> rejected with the recorded reason.

Blocking git/checker work runs in threads; the entry point is async and
never raises for proposal failures. Tests use synthetic repos and vaults
only: never the real vault, never other projects. No tokens.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from orchestration.knowledge_proposals import (
    CollectedProposal,
    collect_proposals,
)

__all__ = [
    "CONFIRM_QUEUE_DIRNAME",
    "LOCK_NAME",
    "OWNER_APPROVAL_RE",
    "FORBIDDEN_BASENAMES",
    "PublishConfig",
    "PublishOutcome",
    "format_publish_comment",
    "publish_after_close",
    "publish_approved",
    "publish_collected",
]

#: Ticket-folder dir holding normative proposals awaiting Owner confirm (2.5).
CONFIRM_QUEUE_DIRNAME = "knowledge_confirm_queue"

#: Same lock file the nightly backup takes (D8 mutual exclusion).
LOCK_NAME = "jardal-knowledge-backup.lock"

#: Basenames never published, even inside the project folder (D7).
FORBIDDEN_BASENAMES = frozenset({
    "CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "SKILL.md",
})

#: Fail-safe tripwire: descriptive content about these topics needs
#: express Owner approval instead of automatic publication (D7 table).
OWNER_APPROVAL_RE = re.compile(
    r"mandat[oe]|allowlist|permiso|obligaci[óo]n|pol[íi]tica\b|policy\b"
    r"|aprobaci[óo]n (expresa|del owner)|irreversible|producci[óo]n",
    re.IGNORECASE,
)

TICKET_RE = re.compile(r"^TASK-\d{4}-\d+$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)

_SECRET_RES = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{10,}"),
    re.compile(r"\bxox[bposa]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
)


@dataclass(frozen=True)
class PublishConfig:
    """Wiring for one publish run (all paths on the host, outside sandbox)."""

    ticket_ref: str
    vault_repo: Path
    vault_dir: Path
    project: str
    evidence_repo: Path | None = None
    checker: Path | None = None
    git_timeout_s: int = 30
    check_timeout_s: int = 180


@dataclass
class PublishOutcome:
    """Per-file fate of one publish run; files are consumed or kept."""

    published: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    queued_reasons: dict[str, str] = field(default_factory=dict)
    deferred: dict[str, str] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)


def _secret_hit(content: str) -> str | None:
    for rx in _SECRET_RES:
        if rx.search(content):
            return "content looks like it holds a secret; remove it first"
    return None


def _target_usable(target: str, project: str) -> str | None:
    if not target or target.startswith("/"):
        return f"target {target!r} is not a project-relative note path"
    if Path(target).name in FORBIDDEN_BASENAMES:
        return f"target {target!r} is never published (reserved file)"
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


def _git(repo: Path, *args: str, timeout_s: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 128, "git unavailable"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _git_dir(repo: Path) -> Path | None:
    code, out = _git(repo, "rev-parse", "--git-dir")
    if code != 0 or not out:
        return None
    candidate = Path(out)
    return candidate if candidate.is_absolute() else repo / candidate


def _note_rel(vault_repo: Path, note: Path) -> str | None:
    try:
        return note.relative_to(vault_repo).as_posix()
    except ValueError:
        return None


def _run_checker(config: PublishConfig, target: str) -> tuple[bool, str]:
    checker = config.checker
    if checker is None:
        candidate = (config.vault_dir.parent / ".agents" / "skills"
                     / "llm-vault-links" / "scripts" / "vault_check.py")
        checker = candidate if candidate.is_file() else None
    if checker is None:
        return False, "vault_check.py not found beside the vault"
    if not Path(checker).is_file():
        return False, f"vault_check.py not found at {checker}"
    try:
        proc = subprocess.run(
            [sys.executable, str(checker), "--vault", str(config.vault_dir),
             "--project", config.project, "check", target],
            cwd=config.vault_repo, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            timeout=config.check_timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"vault check unavailable ({exc})"[:300]
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stdout + proc.stderr)[-1500:].strip()[:800]


async def _evidence_ok(
    proposal: CollectedProposal,
    config: PublishConfig,
    ticket_exists: Callable[[str], Awaitable[bool | None]] | None,
) -> tuple[bool, bool, str]:
    """(ok, transient, reason) for the proposal evidence (D7: resolvable)."""
    repo = config.evidence_repo
    for entry in proposal.evidence:
        ticket = str(entry.get("ticket") or "").strip()
        commit = str(entry.get("commit") or "").strip()
        path = str(entry.get("path") or "").strip()
        if ticket:
            if not TICKET_RE.match(ticket):
                continue
            if ticket_exists is None:
                return True, False, ""
            try:
                found = await ticket_exists(ticket)
            except Exception:  # noqa: BLE001
                return False, True, (
                    f"ticket evidence {ticket!r} unverifiable; retry later")
            if found is True:
                return True, False, ""
            if found is None:
                return False, True, (
                    f"ticket evidence {ticket!r} unverifiable; retry later")
            continue
        if commit:
            if repo is None or not COMMIT_RE.match(commit):
                continue
            code, _ = await asyncio.to_thread(
                _git, repo, "cat-file", "-e", commit,
                timeout_s=config.git_timeout_s)
            if code == 0:
                return True, False, ""
            continue
        if path:
            if repo is None or Path(path).is_absolute() or ".." in Path(path).parts:
                continue
            if await asyncio.to_thread(
                    lambda: (repo / path).exists()):
                return True, False, ""
            continue
    return False, False, "evidence does not resolve: no ticket, commit or path exists"


def _read_note(note: Path) -> bytes | None:
    try:
        if not note.is_file() or note.is_symlink():
            return None
        return note.read_bytes()
    except OSError:
        return None


def _publish_one(
    proposal: CollectedProposal,
    config: PublishConfig,
    evidence_reason: str | None,
    queue_only: bool,
    queue_reason: str,
) -> tuple[str, str, bytes | None]:
    """Write/check/commit one proposal under the held lock.

    Returns (fate, detail, queued_bytes): fate in
    published/queued/deferred/rejected; queued_bytes only for queued.
    Never raises for proposal failures (infrastructure errors defer).
    """
    vault_repo = config.vault_repo
    note = config.vault_dir / proposal.target
    rel = _note_rel(vault_repo, note)
    if rel is None:
        return "rejected", f"target {proposal.target!r} escapes the vault checkout", None
    if note.is_symlink():
        return "rejected", f"target {proposal.target!r} resolves through a symlink", None
    if evidence_reason is not None:
        return "rejected", evidence_reason, None
    try:
        note.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return "deferred", f"vault note dir unavailable ({exc}); retry later", None
    original = _read_note(note)
    try:
        code, out = _git(vault_repo, "ls-files", "--error-unmatch", rel,
                         timeout_s=config.git_timeout_s)
        tracked = code == 0
    except Exception:  # noqa: BLE001
        return "deferred", "vault git unavailable; retry later", None
    if proposal.action == "create" and tracked:
        return "rejected", "note already exists; use action update", None
    if proposal.action == "update" and not tracked:
        if note.exists():
            return "deferred", "note has uncommitted changes; retry later", None
        return "rejected", "note is missing for action update", None
    if proposal.action == "update" and proposal.base_version is None:
        return "rejected", "update needs base_version to verify freshness", None
    code, out = _git(vault_repo, "status", "--porcelain", "--", rel,
                     timeout_s=config.git_timeout_s)
    if code != 0:
        return "deferred", "vault git unavailable; retry later", None
    dirty = [line for line in out.splitlines()
             if line.strip() and not line.startswith("??")]
    if dirty:
        return "deferred", "note has uncommitted changes; retry later", None
    if "??" in {line[:2] for line in out.splitlines() if line.strip()}:
        return "deferred", "note has uncommitted changes; retry later", None
    if tracked and proposal.base_version is not None:
        code, current = _git(vault_repo, "log", "-1", "--format=%H", "--", rel,
                             timeout_s=config.git_timeout_s)
        current_commit = current.strip().splitlines()[0] if code == 0 and current.strip() else ""
        try:
            blob = hashlib.sha1(
                b"blob %d\0" % len(original or b"") + (original or b"")).hexdigest()
        except Exception:  # noqa: BLE001
            blob = ""
        if proposal.base_version not in {current_commit, blob}:
            return "deferred", "note version changed since the proposal; retry later", None
    if original is not None and original == proposal.content.encode("utf-8"):
        return "published", "note already up to date", None
    try:
        note.write_text(proposal.content, encoding="utf-8")
    except OSError as exc:
        return "deferred", f"vault note unwritable ({exc}); retry later", None
    ok, tail = _run_checker(config, proposal.target)
    if not ok:
        _restore(note, original)
        if tail.startswith(("vault_check.py not found",
                            "vault check unavailable")):
            return "deferred", f"vault check unavailable ({tail[:150]}); retry later", None
        return "rejected", f"vault check failed for {proposal.target}: {tail[:300]}", None
    if queue_only:
        digest = hashlib.sha256(note.read_bytes()).hexdigest()
        _restore(note, original)
        return "queued", f"{queue_reason}; sha256 {digest}", None
    code, out = _git(vault_repo, "add", "--", rel, timeout_s=config.git_timeout_s)
    if code != 0:
        _restore(note, original)
        return "deferred", f"vault git add failed ({out[:150]}); retry later", None
    code, out = _git(
        vault_repo, "commit", "--quiet", "-m",
        f"[{config.ticket_ref}] publish {proposal.target}", "--", rel,
        timeout_s=config.git_timeout_s)
    if code != 0:
        _restore(note, original)
        _git(vault_repo, "reset", "--quiet", "--", rel,
             timeout_s=config.git_timeout_s)
        return "deferred", f"vault commit failed ({out[:150]}); retry later", None
    return "published", "", None


def _restore(note: Path, original: bytes | None) -> None:
    try:
        if original is None:
            if note.is_file() or note.is_symlink():
                note.unlink()
        else:
            note.write_bytes(original)
    except OSError:
        pass


def _lock_if_free(vault_repo: Path) -> object | None:
    git_dir = _git_dir(vault_repo)
    if git_dir is None:
        return None
    lock_path = git_dir / LOCK_NAME
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w")
    except OSError:
        return None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return "busy"
    return handle


def _unlock(handle: object) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:
        pass
    try:
        handle.close()  # type: ignore[attr-defined]
    except OSError:
        pass


def publish_collected(
    valid: list[CollectedProposal],
    config: PublishConfig,
    evidence: dict[str, str | None],
) -> PublishOutcome:
    """Publish structurally valid proposals under the backup lock (D8).

    ``evidence`` maps proposal name -> None (resolves) or the reject
    reason. Never raises for proposal failures; infrastructure errors
    defer with the proposal kept.
    """
    outcome = PublishOutcome()
    if not valid:
        return outcome
    handle = _lock_if_free(config.vault_repo)
    if handle is None:
        for item in valid:
            outcome.deferred[item.name] = "vault git unavailable; retry later"
        return outcome
    if handle == "busy":
        for item in valid:
            outcome.deferred[item.name] = (
                "vault busy (backup or another publish); retry later")
        return outcome
    try:
        for item in valid:
            problem = _target_usable(item.target, config.project)
            if problem is not None:
                outcome.rejected[item.name] = problem
                continue
            if _secret_hit(item.content) is not None:
                outcome.rejected[item.name] = _secret_hit(item.content) or ""
                continue
            queue_only = item.proposal_class == "normative"
            queue_reason = "normative content goes to Owner confirmation (2.5)"
            if OWNER_APPROVAL_RE.search(item.content):
                queue_only = True
                queue_reason = ("touches permissions, obligations, policies "
                                "or mandate; needs express Owner approval")
            reason = evidence.get(item.name)
            fate, detail, _blob = _publish_one(
                item, config, reason, queue_only, queue_reason)
            if fate == "published":
                outcome.published.append(item.name)
            elif fate == "queued":
                outcome.queued.append(item.name)
                outcome.queued_reasons[item.name] = detail
            elif fate == "deferred":
                outcome.deferred[item.name] = detail
            else:
                outcome.rejected[item.name] = detail
    finally:
        _unlock(handle)
    return outcome


def publish_approved(
    valid: list[CollectedProposal],
    config: PublishConfig,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Publish Owner-approved queued proposals (2.5 approve path, D7).

    Unlike :func:`publish_collected`, normative content publishes: the
    Owner approved exactly the shown bytes. Returns (published targets,
    deferred mapping target -> reason, rejected mapping target -> reason)
    keyed by target, so same-named proposals from different origins never
    collide. The fate comes from :func:`_publish_one` directly, never from
    matching free text. Never raises for proposal failures; infrastructure
    errors defer with the proposal kept.
    """
    published: list[str] = []
    deferred: dict[str, str] = {}
    rejected: dict[str, str] = {}
    if not valid:
        return published, deferred, rejected
    handle = _lock_if_free(config.vault_repo)
    if handle is None:
        for item in valid:
            deferred[item.target] = "vault git unavailable; retry later"
        return published, deferred, rejected
    if handle == "busy":
        for item in valid:
            deferred[item.target] = (
                "vault busy (backup or another publish); retry later")
        return published, deferred, rejected
    try:
        for item in valid:
            problem = _target_usable(item.target, config.project)
            if problem is not None:
                rejected[item.target] = problem
                continue
            if _secret_hit(item.content) is not None:
                rejected[item.target] = _secret_hit(item.content) or ""
                continue
            fate, detail, _blob = _publish_one(
                item, config, None, False, "")
            if fate == "published":
                published.append(item.target)
            elif fate == "rejected":
                rejected[item.target] = detail or "not published"
            else:
                deferred[item.target] = detail or "retry later"
    finally:
        _unlock(handle)
    return published, deferred, rejected


def format_publish_comment(
    outcome: PublishOutcome,
    structural_rejected: dict[str, str],
) -> str | None:
    """Ticket comment for a publish run: None when nothing happened."""
    rejected = dict(structural_rejected)
    rejected.update(outcome.rejected)
    if (not outcome.published and not outcome.queued
            and not outcome.deferred and not rejected):
        return None
    lines = [f"Vault publisher (D7/D8): {len(outcome.published)} published, "
             f"{len(outcome.queued)} queued for confirmation (2.5), "
             f"{len(outcome.deferred)} deferred, {len(rejected)} rejected."]
    if outcome.published:
        lines.append("Published:")
        lines.extend(f"- {name}" for name in sorted(outcome.published))
    if outcome.queued:
        lines.append("Queued for Owner confirmation (2.5):")
        for name in sorted(outcome.queued):
            reason = outcome.queued_reasons.get(name, "")
            lines.append(f"- {name}: {reason}" if reason else f"- {name}")
    if outcome.deferred:
        lines.append("Deferred, proposal kept for retry (close not blocked):")
        lines.extend(f"- {name}: {reason}"
                     for name, reason in sorted(outcome.deferred.items()))
    if rejected:
        lines.append("Rejected with reason:")
        lines.extend(f"- {name}: {reason}"
                     for name, reason in sorted(rejected.items()))
    return "\n".join(lines)[:8000]


async def publish_after_close(
    task_id: str, *,
    config: PublishConfig,
    base_dir: str | Path | None = None,
    ticket_exists: Callable[[str], Awaitable[bool | None]] | None = None,
    comment: Callable[[str, str], Awaitable[bool | None]] | None = None,
) -> PublishOutcome:
    """Collect the ticket proposals and publish them (2.4, D7/D8).

    Descriptive proposals with resolving evidence publish; normative ones
    (and mandate-touching ones) queue for 2.5; transient states defer with
    the proposal kept; permanent failures reject with the reason recorded.
    One comment covers the run. Never raises for proposal failures and
    never touches the ticket outcome.
    """
    from orchestration.agent_sandbox import ticket_dir
    from orchestration.knowledge_proposals import PROPOSALS_DIRNAME
    valid, structural_rejected = await asyncio.to_thread(
        collect_proposals, task_id, config.project, base_dir=base_dir)
    outcome = PublishOutcome()
    base = Path(base_dir) if base_dir is not None else None
    proposals_dir = ticket_dir(task_id, base_dir=base) / PROPOSALS_DIRNAME
    for name, reason in structural_rejected.items():
        key = Path(name).stem or name
        if key in outcome.rejected:
            key = name
        outcome.rejected[key] = reason
        try:
            candidate = proposals_dir / name
            if candidate.is_file():
                candidate.unlink()
        except OSError:
            pass
    evidence: dict[str, str | None] = {}
    pending: list[CollectedProposal] = []
    for item in valid:
        ok, transient, reason = await _evidence_ok(item, config, ticket_exists)
        if ok:
            evidence[item.name] = None
            pending.append(item)
        elif transient:
            outcome.deferred[item.name] = reason
        else:
            evidence[item.name] = reason
    published = await asyncio.to_thread(
        publish_collected, pending, config,
        {item.name: None for item in pending})
    outcome.published.extend(published.published)
    outcome.queued.extend(published.queued)
    outcome.queued_reasons.update(published.queued_reasons)
    outcome.deferred.update(published.deferred)
    outcome.rejected.update(published.rejected)
    outcome.rejected.update({k: v for k, v in evidence.items()
                             if v is not None})
    queue_dir = ticket_dir(task_id, base_dir=base) / CONFIRM_QUEUE_DIRNAME
    moved = []
    for name in outcome.queued:
        src = next((v.source for v in valid if v.name == name), None)
        if src is None or not src.is_file():
            continue
        try:
            queue_dir.mkdir(parents=True, exist_ok=True)
            blob = src.read_bytes()
            digest = hashlib.sha256(blob).hexdigest()
            (queue_dir / f"{name}.yaml").write_bytes(blob)
            src.unlink()
            moved.append(f"{name} (sha256 {digest[:16]}...)")
        except OSError:
            continue
    for path in [v.source for v in valid
                 if v.name in outcome.published
                 or v.name in outcome.rejected]:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    if comment is not None:
        text = format_publish_comment(outcome, {})
        if moved and text is not None:
            text += "\nQueued files: " + ", ".join(sorted(moved))
        if text is not None:
            await comment(task_id, text)
    return outcome
