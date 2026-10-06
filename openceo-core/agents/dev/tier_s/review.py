"""Review rounds orchestration primitives (design §7, F3).

Pool selection by slot priority and family, quorum-2 of distinct families,
read-only detached worktrees with write detection, pointer review requests,
``FIX_<r>`` verdict parsing and the arbitration trigger. The deterministic
finding pipeline lives in :mod:`agents.dev.tier_s.findings`; the round flow
that calls these helpers lives in :mod:`agents.dev.tier_s.cycle`.

Slot availability persists in ``agent_status``: ``available``,
``cooling_down(until)`` or ``disabled(reason)`` (§7.2). Reactive slots are
used until they fail and the orchestrator classifies the error against the
slot's ``limit_patterns`` / ``expired_patterns``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from agents.dev.tier_s.findings import (
    Finding,
    VerifiedFinding,
    _anchor_text,  # rendering only; anchors stay opaque otherwise
)
from agents.dev.tier_s.knowledge import with_preamble


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewSlot:
    """One ``(cli, model)`` review slot (§7.1)."""

    label: str
    family: str
    model: str = ""
    command: tuple[str, ...] = ()
    prompt_via: str = "argv"  # "argv" | "stdin"
    env: dict[str, str] = field(default_factory=dict)
    priority: int = 50
    quota: str = "reactive"  # "reactive" | "polled"
    expires_on: str = ""  # YYYY-MM-DD; disabled without waiting for an error
    limit_patterns: tuple[str, ...] = ()
    expired_patterns: tuple[str, ...] = ()
    extra: bool = False  # never counts toward quorum (§7.3)
    active: bool = True


def parse_pool(raw: Sequence[Mapping[str, Any]] | None) -> list[ReviewSlot]:
    """Parse the global ``reviewers`` list (config_loader ``dev.tier_s``)."""
    slots: list[ReviewSlot] = []
    for entry in raw or []:
        if not isinstance(entry, Mapping):
            continue
        label = str(entry.get("label") or entry.get("id") or "").strip()
        family = str(entry.get("family") or "").strip().lower()
        if not label or not family:
            continue
        command = entry.get("command") or []
        if isinstance(command, str):
            command = [command]
        try:
            priority = int(entry.get("priority", 50))
        except (TypeError, ValueError):
            priority = 50
        slots.append(ReviewSlot(
            label=label, family=family,
            model=str(entry.get("model") or ""),
            command=tuple(str(token) for token in command),
            prompt_via=str(entry.get("prompt_via") or "argv"),
            env={str(k): str(v)
                 for k, v in dict(entry.get("env") or {}).items()},
            priority=priority,
            quota=str(entry.get("quota") or "reactive"),
            expires_on=str(entry.get("expires_on") or ""),
            limit_patterns=tuple(str(p) for p in entry.get("limit_patterns") or []),
            expired_patterns=tuple(
                str(p) for p in entry.get("expired_patterns") or []),
            extra=bool(entry.get("extra", False)),
            active=entry.get("active", True) is not False,
        ))
    slots.sort(key=lambda slot: (slot.priority, slot.label))
    return slots


class SlotStore(Protocol):
    """Persistence for slot availability (§7.2)."""

    async def get_slot(self, label: str) -> dict[str, Any]: ...
    async def set_slot(self, label: str, state: dict[str, Any]) -> None: ...


class InMemorySlotStore:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}

    async def get_slot(self, label: str) -> dict[str, Any]:
        return dict(self.states.get(label, {"status": "available"}))

    async def set_slot(self, label: str, state: dict[str, Any]) -> None:
        self.states[label] = dict(state)


class MongoSlotStore:
    """Production slot states on ``agent_status`` (one doc per slot label)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_slot(self, label: str) -> dict[str, Any]:
        row = await self._repo.get_agent_status(f"review:{label}")
        if not row:
            return {"status": "available"}
        return {"status": str(row.get("status") or "available"),
                "until": row.get("until"), "reason": row.get("reason")}

    async def set_slot(self, label: str, state: dict[str, Any]) -> None:
        await self._repo.set_agent_status(f"review:{label}", dict(state))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slot_available(
    slot: ReviewSlot,
    state: Mapping[str, Any],
    now: datetime | None = None,
) -> tuple[bool, str]:
    """(eligible, reason). Pure; the caller persists ``expires_on`` cutoffs."""
    now = now or _utcnow()
    if not slot.active:
        return False, "inactive"
    if slot.expires_on:
        try:
            expiry = datetime.strptime(slot.expires_on[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            expiry = None
        if expiry is not None and now >= expiry:
            return False, f"expired {slot.expires_on[:10]}"
    status = str(state.get("status") or "available")
    if status == "disabled":
        return False, str(state.get("reason") or "disabled")
    if status == "cooling_down":
        until = state.get("until")
        try:
            resume = (datetime.fromisoformat(str(until))
                      if until else now + timedelta(hours=1))
            if resume.tzinfo is None:
                resume = resume.replace(tzinfo=timezone.utc)
        except ValueError:
            resume = now + timedelta(hours=1)
        if now < resume:
            return False, f"cooling_down until {resume.isoformat()}"
    return True, ""


def expiry_warning(slot: ReviewSlot, now: datetime | None = None) -> str:
    """Warn 3 days before ``expires_on`` (§7.2); "" when not due."""
    if not slot.expires_on:
        return ""
    now = now or _utcnow()
    try:
        expiry = datetime.strptime(slot.expires_on[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return ""
    delta = expiry - now
    if timedelta(0) <= delta <= timedelta(days=3):
        return (f"review slot {slot.label} expires on {slot.expires_on[:10]} "
                f"({delta.days}d left)")
    return ""


def classify_slot_error(text: str, slot: ReviewSlot) -> tuple[str, dict[str, Any]]:
    """Classify a reactive slot failure (§7.2).

    Returns ``(action, patch)`` where action is ``cooling_down``,
    ``disabled`` or ``invalid_only`` (unclassified: bad report, availability
    untouched).
    """
    lowered = (text or "").lower()
    for pattern in slot.expired_patterns:
        if pattern.lower() in lowered:
            return "disabled", {"status": "disabled",
                                "reason": f"expired pattern: {pattern}"}
    if "model not found" in lowered or "model_not_found" in lowered:
        return "disabled", {"status": "disabled", "reason": "model not found"}
    for pattern in slot.limit_patterns:
        if pattern.lower() in lowered:
            resume = _utcnow() + timedelta(minutes=60)
            return "cooling_down", {"status": "cooling_down",
                                    "until": resume.isoformat(),
                                    "reason": f"limit pattern: {pattern}"}
    return "invalid_only", {}


# ---------------------------------------------------------------------------
# Selection + quorum (§7.2, §7.3)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoundPlan:
    reviewers: tuple[ReviewSlot, ...]
    extras: tuple[ReviewSlot, ...]


async def select_round(
    pool: Sequence[ReviewSlot],
    store: SlotStore,
    count: int,
    builder_family: str = "muse",
    prefer_families: Sequence[str] = (),
    include_extras: bool = True,
    exclude: Sequence[str] = (),
) -> RoundPlan:
    """Pick ``count`` slots of distinct families by priority (§7.2).

    ``prefer_families`` (round 2: families behind dismissed CRITICALs) jump
    the queue but still respect one-slot-per-family. Slots of the builder's
    own family and ``extra`` slots never count: they ride along as
    information only (§7.3). ``exclude`` skips labels already tried (quorum
    replacement wave).
    """
    preferred = {name.lower() for name in prefer_families}
    excluded = set(exclude)
    eligible: list[ReviewSlot] = []
    extras: list[ReviewSlot] = []
    for slot in pool:
        if slot.label in excluded:
            continue
        ok, _reason = slot_available(slot, await store.get_slot(slot.label))
        if not ok:
            continue
        if slot.extra or slot.family == builder_family:
            extras.append(slot)
        else:
            eligible.append(slot)
    eligible.sort(key=lambda slot: (
        0 if slot.family in preferred else 1, slot.priority, slot.label))
    picked: list[ReviewSlot] = []
    seen: set[str] = set()
    for slot in eligible:
        if slot.family in seen:
            continue
        seen.add(slot.family)
        picked.append(slot)
        if len(picked) >= count:
            break
    return RoundPlan(reviewers=tuple(picked),
                     extras=tuple(extras) if include_extras else ())


async def available_families(
    pool: Sequence[ReviewSlot],
    store: SlotStore,
    builder_family: str = "muse",
) -> set[str]:
    """Eligible quorum-counting families (preflight needs ≥3, §6.1)."""
    families: set[str] = set()
    for slot in pool:
        if slot.extra or slot.family == builder_family:
            continue
        ok, _reason = slot_available(slot, await store.get_slot(slot.label))
        if ok:
            families.add(slot.family)
    return families


def quorum_met(
    valid: Mapping[str, bool],
    slots: Sequence[ReviewSlot],
    builder_family: str = "muse",
    quorum: int = 2,
) -> bool:
    """≥``quorum`` valid reports of distinct families (fail-closed, §7.2)."""
    seen: set[str] = set()
    by_label = {slot.label: slot for slot in slots}
    for label, ok in valid.items():
        if not ok:
            continue
        slot = by_label.get(label)
        if slot is None or slot.extra or slot.family == builder_family:
            continue
        if not slot.family:
            continue
        seen.add(slot.family)
        if len(seen) >= quorum:
            return True
    return False


# ---------------------------------------------------------------------------
# Worktree (read-only review environment, §7.5)
# ---------------------------------------------------------------------------

def create_worktree(repo: Path, head_sha: str, tag: str) -> Path:
    """Detached worktree at ``head_sha`` in a scratch dir (outside the repo)."""
    target = Path(tempfile.mkdtemp(prefix=f"tier-s-{tag}-"))
    proc = subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), head_sha],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"worktree failed: {proc.stderr.strip()[:300]}")
    return target


def remove_worktree(repo: Path, worktree: Path) -> None:
    """Best-effort removal; a leftover worktree never fails the cycle."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        shutil.rmtree(worktree, ignore_errors=True)
    except OSError:
        pass


def worktree_snapshot(worktree: Path) -> str:
    """Digest of the worktree state for write detection (§7.5, §20-5)."""
    from agents.dev.tier_s_contract import git_snapshot
    try:
        return git_snapshot(worktree).digest
    except (OSError, subprocess.CalledProcessError):
        return ""


# ---------------------------------------------------------------------------
# Request + prompt (§7.4, §7.5)
# ---------------------------------------------------------------------------

def build_review_request(
    round_no: int,
    goal: str,
    acceptance: Sequence[str],
    base_sha: str,
    head_sha: str,
    diff_stat: str,
    files: Sequence[str],
    out_of_plan: Sequence[str],
    gates_section: str,
    handoff_path: str,
    extra_context: str = "",
) -> str:
    """``REVIEW_REQUEST_<r>.md``: pointers, never pasted diffs (§7.4)."""
    out = set(out_of_plan)
    criteria = [f"- {criterion}" for criterion in acceptance] or ["- (none listed)"]
    lines = [
        f"REVIEW REQUEST R{round_no}",
        f"Goal: {goal}",
        "Acceptance:",
        *criteria,
        f"Base: {base_sha}",
        f"Head: {head_sha}",
        "Diff stat:",
        (diff_stat.strip() or "(empty)")[:4000],
        "Files under review (out-of-plan flagged):",
        *[f"- {name}{'  FUERA DE PLAN' if name in out else ''}" for name in files],
        "Gates (already run by the orchestrator):",
        (gates_section.strip() or "(none)")[:2000],
        f"Handoff: {handoff_path}",
    ]
    if extra_context:
        lines.extend(["", extra_context.strip()[:6000]])
    return "\n".join(lines) + "\n"


def reviewer_prompt(
    slot_label: str,
    request_name: str,
    scope: str,
    round_no: int,
    repo: Path | str | None = None,
) -> str:
    """Pointed review prompt (§7.5): diff-only, no tests, no-wide review."""
    body = (
        f"You are review slot {slot_label}. Read {request_name} in this "
        "worktree (read-only checkout at the ticket head).\n"
        "Review ONLY the listed files, plus directly related ones when a "
        "contract needs it (e.g. a caller). Do NOT run tests or builds: gate "
        "results are in the request. Do NOT do a general system review. A "
        "grave bug outside the diff may be reported, but do not go hunting.\n"
        "Print your report on stdout, exactly one finding per header:\n"
        "`[CRITICAL|WARN|INFO] [bug|security|contract|performance|test|"
        "maintainability] <anchor> — <problem> → <fix>` (§16.4), each line "
        "anchor with its literal `>` quote, optional explanation ≤5 lines. "
        "Anchors: `path:line`, `path:line@base` (deleted/changed code), "
        "`path:line + path:line` (cross-location, quote each), `path` "
        "(whole file), `CRITERIO:<id>`. If clean, print `No findings.`.\n"
        f"End with REVIEW_DONE_{scope}_{round_no} on its own last line."
    )
    return with_preamble(body, repo, "en")


# ---------------------------------------------------------------------------
# FIX verdicts (§7.9, §16.8)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FixVerdict:
    finding_id: int
    verdict: str  # CORREGIDO | DESCARTADO
    reason: str
    out_of_scope: bool


_FIX_RE = re.compile(
    r"^F(\d+)\s*:\s*(CORREGIDO|DESCARTADO)\b\s*(?:[—–-]\s*(.*))?$",
    re.IGNORECASE,
)


def parse_fix(text: str) -> dict[int, list[FixVerdict]]:
    """Parse ``FIX_<r>.md``: finding id → verdicts (repeats kept, §7.9)."""
    verdicts: dict[int, list[FixVerdict]] = {}
    for raw_line in (text or "").splitlines():
        match = _FIX_RE.match(raw_line.strip())
        if not match:
            continue
        reason = (match.group(3) or "").strip()
        lowered = reason.lower()
        verdicts.setdefault(int(match.group(1)), []).append(FixVerdict(
            finding_id=int(match.group(1)),
            verdict=match.group(2).upper(), reason=reason,
            out_of_scope="fuera de alcance" in lowered
            or "out of scope" in lowered))
    return verdicts


def fix_gaps(
    findings: Sequence[Finding],
    verdicts: Mapping[int, list[FixVerdict]],
) -> tuple[list[int], list[int]]:
    """(missing, duplicated) finding ids — mirrors plan_response_gaps."""
    wanted = {item.id for item in findings}
    missing = sorted(wanted - set(verdicts))
    duplicated = sorted(index for index in wanted
                        if len(verdicts.get(index, ())) > 1)
    return missing, duplicated


def muse_fix_prompt(
    round_no: int,
    findings_name: str,
    count: int,
    repo: Path | str | None = None,
) -> str:
    """First-instance judge prompt (§7.9): fix or discard with reasons."""
    body = (
        f"Decide every finding in {findings_name} ({count} findings, round "
        f"{round_no}). They are verified: they point at real code. Judge the "
        "reasoning: is the problem real, is the proposed fix correct, is it "
        "in this ticket's scope?\n"
        f"Write FIX_{round_no}.md with one line per finding: "
        "`F<n>: CORREGIDO — <what you changed>` (and change the code, no "
        "commits) or `F<n>: DESCARTADO — <verifiable technical reason>`. "
        "`I disagree` is not a reason. A multi-family CRITICAL needs an "
        "especially solid reason to discard. Real but out-of-scope problems: "
        "`DESCARTADO — fuera de alcance` (the orchestrator routes them to "
        "the inbox). Do not rewrite uninvolved code.\n"
        f"Then emit FIX_DONE_{round_no} in .tier-s/status."
    )
    return with_preamble(body, repo, "en")


# ---------------------------------------------------------------------------
# Arbitration trigger (§7.10, §6.9)
# ---------------------------------------------------------------------------

def _same_issue(left: VerifiedFinding, right: VerifiedFinding) -> bool:
    if left.raw.category != right.raw.category:
        return False
    for left_anchor in left.raw.anchors:
        for right_anchor in right.raw.anchors:
            if left_anchor.criterion is not None or right_anchor.criterion is not None:
                if left_anchor.criterion == right_anchor.criterion:
                    return True
                continue
            if left_anchor.path != right_anchor.path:
                continue
            if left_anchor.line is None or right_anchor.line is None:
                return True
            if abs(left_anchor.line - right_anchor.line) <= 5:
                return True
    return False


def arbitration_candidates(
    round2: Sequence[VerifiedFinding],
    fix2: Mapping[int, list[FixVerdict]],
    findings2: Sequence[Finding],
    round1_dismissed: Sequence[VerifiedFinding],
) -> list[Finding]:
    """CRITICALs reconfirmed by ≥2 families that ``muse`` dismissed again.

    A candidate needs: CRITICAL in ``FINDINGS_2`` with ≥2 counting families,
    ``DESCARTADO`` in ``FIX_2`` (not out-of-scope), and the same issue
    dismissed in round 1 (reconfirmation, §6.9).
    """
    by_id = {item.id: item for item in findings2}
    candidates: list[Finding] = []
    for finding in findings2:
        if finding.severity != "CRITICAL":
            continue
        if len([name for name in finding.families]) < 2:
            continue
        verdicts = fix2.get(finding.id, [])
        if not verdicts or verdicts[0].verdict != "DESCARTADO":
            continue
        if verdicts[0].out_of_scope:
            continue
        probe = next(
            (item for item in round2
             if item.destination == "findings"
             and any(_anchor_text(a) in {
                 _anchor_text(b) for b in finding.anchors}
                     for a in item.raw.anchors)),
            None)
        if probe is None:
            continue
        if not any(_same_issue(probe, old) for old in round1_dismissed):
            continue
        candidates.append(by_id[finding.id])
    return candidates


def arbiter_prompt(
    finding: Finding,
    muse_reason: str,
    quote: str,
    repo: Path | str | None = None,
) -> str:
    """Short arbitration session (§7.10): the verdict is final."""
    anchors = " + ".join(_anchor_text(a) for a in finding.anchors)
    body = (
        "Arbitrate ONE disputed CRITICAL finding (final verdict).\n"
        f"Finding: [{finding.category}] {anchors} — {finding.problem} → "
        f"{finding.fix}\nFamilies reporting: {', '.join(finding.families)}\n"
        f"Code:\n{quote[:2000]}\n"
        f"muse discarded it: {muse_reason[:1000]}\n"
        "Reply exactly one line: `REAL — <why>` or `FALSO — <verifiable "
        "technical reason>`. Then emit ARBITRATED in .tier-s/status."
    )
    return with_preamble(body, repo, "en")


# ---------------------------------------------------------------------------
# task_reviews persistence (§15.5, append-only)
# ---------------------------------------------------------------------------

class ReviewStore(Protocol):
    """Append-only reviewer reports."""

    async def create_review(self, doc: dict[str, Any]) -> None: ...


class InMemoryReviewStore:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def create_review(self, doc: dict[str, Any]) -> None:
        self.docs.append(dict(doc))


class MongoReviewStore:
    """Production reports on ``task_reviews`` (existing collection)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def create_review(self, doc: dict[str, Any]) -> None:
        await self._repo.create_task_review(dict(doc))


# ---------------------------------------------------------------------------
# Round telemetry (§15.3 rounds[])
# ---------------------------------------------------------------------------

def round_telemetry(
    round_no: int,
    slots: Sequence[str],
    valid_reports: int,
    verdicts: Sequence[VerifiedFinding],
    fix: Mapping[int, list[FixVerdict]],
) -> dict[str, Any]:
    """One ``rounds[]`` entry for the run document."""
    counts = {"verified": 0, "unverified": 0, "pre_existing": 0, "in_scope": 0}
    for item in verdicts:
        if not item.verified:
            counts["unverified"] += 1
        else:
            counts["verified"] += 1
        if item.destination == "inbox":
            counts["pre_existing"] += 1
        elif item.destination == "findings":
            counts["in_scope"] += 1
    fixed = sum(1 for verdicts_list in fix.values()
                for verdict in verdicts_list[:1]
                if verdict.verdict == "CORREGIDO")
    discarded = sum(1 for verdicts_list in fix.values()
                    for verdict in verdicts_list[:1]
                    if verdict.verdict == "DESCARTADO")
    return {
        "r": round_no,
        "slots": list(slots),
        "valid_reports": valid_reports,
        "findings": counts,
        "muse": {"fixed": fixed, "discarded": discarded, "no_response": 0},
    }
