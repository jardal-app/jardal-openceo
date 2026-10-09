"""Deterministic finding verification and consolidation (design §7.7–§7.8, §7.11, F3).

Parses reviewer reports (§16.4), verifies every anchor against git objects
(``git show <sha>:<path>``, quote matched space-normalized within ±5 lines),
splits in-ticket findings from pre-existing code, groups duplicates
deterministically and renders the single ``FINDINGS_<r>.md`` that ``muse``
decides on. Also builds the append-only ``task_reviews`` documents (§15.5).

No model is called here. Hallucinations (invented quotes) die in
:func:`verify_report`, before any agent sees them.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence


#: Severities (§16.4), ordered worst-first for grouping.
SEVERITIES = ("CRITICAL", "WARN", "INFO")

#: Closed category set (§7.6).
CATEGORIES = (
    "bug",
    "security",
    "contract",
    "performance",
    "test",
    "maintainability",
)

#: Line window for quote matching and diff-range scope checks (§7.7).
ANCHOR_WINDOW = 5

#: Trailing signal of every report (§15.7); recorded, not required. Any
#: ``REVIEW_DONE_*`` line is skipped (legacy trailers also named the slot).
_DONE_RE = re.compile(r"^REVIEW_DONE_\S*$")

#: Markdown a model wraps around a header (TASK-2026-00162): one list
#: bullet or number, then bold/italic/code markers.
_LEAD = r"(?:[-*•+]\s+|\d+[.)]\s+)?"
_DRESS = r"[*_`]*"

#: ``[CRITICAL] [bug] <anchor> — <problem> → <fix>`` (§16.4). Severity and
#: category match case-insensitively; ``WARNING`` reads as ``WARN``.
_FINDING_RE = re.compile(
    rf"^{_LEAD}{_DRESS}\[\s*(CRITICAL|WARN(?:ING)?|INFO)\s*\]{_DRESS}\s*"
    rf"{_DRESS}\[\s*(bug|security|contract|performance|test|maintainability)"
    rf"\s*\]{_DRESS}\s+"
    r"(\S+(?:\s+\+\s+\S+)*)\s+[—–-]\s*(.+?)\s*(?:→|->)\s*(.+?)\s*$",
    re.IGNORECASE,
)

#: A line that opens like a finding (severity bracket first). It must parse
#: as a full header or the whole report is malformed: tolerance to prose
#: never drops a finding silently (fail-closed, TASK-2026-00162).
_HEADER_LIKE_RE = re.compile(
    rf"^{_LEAD}{_DRESS}\[\s*(?:CRITICAL|WARN(?:ING)?|INFO)\s*\]",
    re.IGNORECASE,
)

#: ``path:line``, ``path:line@base``, ``path`` or ``CRITERIO:<id>``.
_ANCHOR_RE = re.compile(
    r"^(?:(?P<criterion>[Cc][Rr][Ii][Tt][Ee][Rr][Ii][Oo]:\S+)|"
    r"(?P<path>[^:\s]+?)(?::(?P<line>\d+))?(?P<base>@base)?)$"
)

#: The clean-report marker; dressing and the trailing dot are tolerated.
_NO_FINDINGS_RE = re.compile(rf"^{_DRESS}No findings\b", re.IGNORECASE)

#: Markdown code fences carry no report content.
_FENCE_RE = re.compile(r"^(?:```|~~~)")

#: Explanation lines kept per finding (§16.4); further prose is dropped.
MAX_EXPLANATION_LINES = 5

#: Characters a model wraps around an anchor (code span, bold, quotes).
#: Never ``_``: paths such as ``__init__.py`` start with it.
_ANCHOR_WRAP = "`*'\""

#: Characters a model wraps around a whole quote (code span, quotes).
_QUOTE_WRAP = "`'\""

#: Per-field cap for finding text persisted in ``task_reviews``.
_TEXT_CAP = 600


class ReportMalformed(ValueError):
    """A report line does not follow §16.4: the whole report is invalid."""


@dataclass(frozen=True)
class Anchor:
    """One parsed anchor. ``line`` is None for file/criterion anchors."""

    raw: str
    path: str | None  # repo-relative, None for CRITERIO anchors
    line: int | None
    base: bool  # @base: verified against base_sha
    criterion: str | None

    @property
    def weak(self) -> bool:
        """File and criterion anchors verify existence only (§7.6)."""
        return self.criterion is not None or self.line is None


@dataclass
class RawFinding:
    """One finding as reported by one slot, before verification."""

    severity: str
    category: str
    anchors: list[Anchor]
    problem: str
    fix: str
    quotes: list[str] = field(default_factory=list)
    explanation: str = ""


@dataclass
class VerifiedFinding:
    """A raw finding after mechanical verification (§7.7)."""

    raw: RawFinding
    slot: str
    family: str
    verified: bool
    strength: str  # "strong" | "weak"
    scope: str  # "ticket" | "pre_existing"
    destination: str  # "findings" | "inbox" | "discarded"
    reason: str = ""


@dataclass
class Finding:
    """Consolidated finding: same category + overlapping anchors (§7.7.4)."""

    id: int  # F<n>, assigned at render time
    severity: str
    category: str
    anchors: list[Anchor]
    problem: str
    fix: str
    strength: str
    families: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    explanations: dict[str, str] = field(default_factory=dict)  # family -> text
    low_priority: bool = False  # maintainability passes deprioritized


# ---------------------------------------------------------------------------
# Parsing (§16.4: tolerant to prose, strict on findings — TASK-2026-00162)
# ---------------------------------------------------------------------------

def _parse_anchor(raw: str) -> Anchor:
    text = raw.strip().strip(_ANCHOR_WRAP).strip()
    match = _ANCHOR_RE.match(text)
    if not match:
        raise ReportMalformed(f"bad anchor: {raw!r}")
    criterion = match.group("criterion")
    if criterion is not None:
        return Anchor(raw=text, path=None, line=None, base=False,
                      criterion=criterion.upper())
    path = str(match.group("path") or "").strip().lstrip("./")
    line_raw = match.group("line")
    if not path or path.startswith(("/", "~")) or ".." in path.split("/"):
        raise ReportMalformed(f"bad anchor path: {raw!r}")
    return Anchor(raw=text, path=path,
                  line=int(line_raw) if line_raw else None,
                  base=bool(match.group("base")), criterion=None)


def _unwrap_quote(text: str) -> str:
    """Drop a code span/quote wrapped around the whole quote.

    Only edge characters go, so a quote that matched the source before
    still matches (a substring of it): unwrapping never weakens verification.
    """
    quote = text.strip()
    while len(quote) >= 2 and quote[0] == quote[-1] and quote[0] in _QUOTE_WRAP:
        quote = quote[1:-1].strip()
    return quote


def parse_report(text: str) -> list[RawFinding]:
    """Parse a reviewer report. Raises :class:`ReportMalformed`.

    Tolerant to prose, strict on findings (TASK-2026-00162; the pilot lost
    4 of 5 valid reports to a one-line preamble). Prose before the first
    finding is skipped, prose after a finding is its explanation (first
    5 lines kept), and blank lines, code fences and ``REVIEW_DONE_*``
    trailers are skipped. Strict, and fatal for the whole report: a line
    that opens like a finding (severity bracket first) must be a complete
    §16.4 header with valid anchors, every line anchor needs its literal
    ``>`` quote, and a report with neither findings nor the ``No findings``
    marker is empty. Anything a finding claims is still verified against
    git by :func:`verify_report`; prose never reaches it.
    """
    findings: list[RawFinding] = []
    current: RawFinding | None = None
    explanation_lines: list[str] = []
    clean = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or _DONE_RE.match(line) or _FENCE_RE.match(line):
            continue
        header = _FINDING_RE.match(line)
        if header is not None:
            if current is not None:
                current.explanation = "\n".join(explanation_lines).strip()
            anchor_raws = [part.strip() for part in header.group(3).split("+")]
            anchors = [_parse_anchor(part) for part in anchor_raws if part]
            if not anchors:
                raise ReportMalformed(f"finding without anchor: {line!r}")
            severity = header.group(1).upper()
            current = RawFinding(
                severity="WARN" if severity == "WARNING" else severity,
                category=header.group(2).lower(),
                anchors=anchors, problem=header.group(4).strip(),
                fix=header.group(5).strip())
            findings.append(current)
            explanation_lines = []
            continue
        if _HEADER_LIKE_RE.match(line):
            raise ReportMalformed(f"bad finding header: {line[:200]!r}")
        if current is None:
            # Preamble: summaries, markdown, stray quotes. Never content.
            if _NO_FINDINGS_RE.match(line):
                clean = True
            continue
        if line.startswith(">"):
            current.quotes.append(_unwrap_quote(line[1:]))
            continue
        if len(explanation_lines) < MAX_EXPLANATION_LINES:
            explanation_lines.append(line)
    if current is not None:
        current.explanation = "\n".join(explanation_lines).strip()
    if not findings and not clean:
        raise ReportMalformed("empty report: no findings and no 'No findings.'")
    # Line anchors require their literal quote (§7.6); file/criterion do not.
    for item in findings:
        need = sum(1 for anchor in item.anchors if anchor.line is not None)
        if len(item.quotes) < need:
            raise ReportMalformed(
                f"missing literal quote for {item.anchors[0].raw!r}")
    return findings


# ---------------------------------------------------------------------------
# Git reads (the only I/O; no writes, no network)
# ---------------------------------------------------------------------------

def _git_show(repo: Path, sha: str, path: str) -> list[str] | None:
    """File lines at ``sha``; None when the path does not exist there."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{sha}:{path}"], cwd=repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace").splitlines()


def _normalize(text: str) -> str:
    return " ".join(text.split())


def changed_ranges(repo: Path, base_sha: str, head_sha: str) -> dict[str, list[tuple[int, int]]]:
    """New-side changed line ranges per file from ``git diff -U0`` (§7.11)."""
    try:
        proc = subprocess.run(
            ["git", "diff", "-U0", "--no-renames", base_sha, head_sha, "--", "."],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[len("+++ b/"):]
            ranges.setdefault(current, [])
        elif line.startswith("@@ ") and current is not None:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) else 1
                if count > 0:
                    ranges[current].append((start, start + count - 1))
    return ranges


# ---------------------------------------------------------------------------
# Verification + scope (§7.7)
# ---------------------------------------------------------------------------

def _quote_matches(lines: Sequence[str], quote: str, line: int) -> bool:
    want = _normalize(quote)
    if not want:
        return False
    low = max(1, line - ANCHOR_WINDOW)
    high = min(len(lines), line + ANCHOR_WINDOW)
    for number in range(low, high + 1):
        if want in _normalize(lines[number - 1]):
            return True
    return False


def _criterion_exists(criteria: Sequence[str], criterion: str) -> bool:
    ident = criterion.split(":", 1)[1].strip() if ":" in criterion else ""
    if ident.isdigit():
        index = int(ident)
        return 1 <= index <= len(criteria)
    lowered = ident.lower()
    return any(lowered in entry.lower() for entry in criteria)


def verify_report(
    findings: Sequence[RawFinding],
    repo: Path,
    head_sha: str,
    base_sha: str,
    slot: str,
    family: str,
    acceptance: Sequence[str] = (),
) -> list[VerifiedFinding]:
    """Verify anchors and split ticket scope from pre-existing (§7.7).

    Unverifiable findings are discarded (but recorded for ``task_reviews``);
    verified pre-existing ones route to the inbox and never reach ``muse``.
    """
    ranges = changed_ranges(repo, base_sha, head_sha)
    diff_files = set(ranges)
    cache: dict[tuple[str, str], list[str] | None] = {}

    def lines_at(sha: str, path: str) -> list[str] | None:
        key = (sha, path)
        if key not in cache:
            cache[key] = _git_show(repo, sha, path)
        return cache[key]

    verified: list[VerifiedFinding] = []
    for raw in findings:
        ok, weak, in_ticket = _verify_one(
            raw, lines_at, head_sha, base_sha, ranges, diff_files, acceptance)
        if not ok:
            verified.append(VerifiedFinding(
                raw=raw, slot=slot, family=family, verified=False,
                strength="weak", scope="ticket", destination="discarded",
                reason="unverified anchor or quote"))
            continue
        if raw.severity == "INFO":
            verified.append(VerifiedFinding(
                raw=raw, slot=slot, family=family, verified=True,
                strength="weak" if weak else "strong",
                scope="ticket" if in_ticket else "pre_existing",
                destination="discarded", reason="INFO filtered (§7.7.5)"))
            continue
        verified.append(VerifiedFinding(
            raw=raw, slot=slot, family=family, verified=True,
            strength="weak" if weak else "strong",
            scope="ticket" if in_ticket else "pre_existing",
            destination="findings" if in_ticket else "inbox"))
    return verified


def _verify_one(
    raw: RawFinding,
    lines_at: Callable[[str, str], list[str] | None],
    head_sha: str,
    base_sha: str,
    ranges: dict[str, list[tuple[int, int]]],
    diff_files: set[str],
    acceptance: Sequence[str],
) -> tuple[bool, bool, bool]:
    """(verified, weak, in_ticket) for one raw finding."""
    get = lines_at
    weak_any = False
    in_ticket = False
    quote_index = 0
    for anchor in raw.anchors:
        if anchor.criterion is not None:
            if not _criterion_exists(acceptance, anchor.criterion):
                return False, True, False
            weak_any = True
            in_ticket = True  # criterion anchors are always ours (§7.7.3)
            continue
        assert anchor.path is not None
        sha = base_sha if anchor.base else head_sha
        lines = get(sha, anchor.path)
        if lines is None:
            return False, True, False
        if anchor.line is None:
            weak_any = True
            if anchor.path in diff_files:
                in_ticket = True
            continue
        if not 1 <= anchor.line <= len(lines):
            return False, False, False
        quote = raw.quotes[quote_index] if quote_index < len(raw.quotes) else ""
        quote_index += 1
        if not _quote_matches(lines, quote, anchor.line):
            return False, False, False
        if anchor.base:
            in_ticket = True
        else:
            for start, end in ranges.get(anchor.path, []):
                if start - ANCHOR_WINDOW <= anchor.line <= end + ANCHOR_WINDOW:
                    in_ticket = True
                    break
    return True, weak_any, in_ticket


# ---------------------------------------------------------------------------
# Grouping + FINDINGS output (§7.7.4, §7.8, §16.5)
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"CRITICAL": 0, "WARN": 1, "INFO": 2}


def _anchors_overlap(left: Anchor, right: Anchor) -> bool:
    if left.criterion is not None or right.criterion is not None:
        return left.criterion is not None and left.criterion == right.criterion
    if left.path != right.path:
        return False
    if left.line is None or right.line is None:
        return True
    return abs(left.line - right.line) <= ANCHOR_WINDOW


def _findings_overlap(left: VerifiedFinding, right: VerifiedFinding) -> bool:
    if left.raw.category != right.raw.category:
        return False  # same line, other category: never merged (§7.7.4)
    return any(_anchors_overlap(left_anchor, right_anchor)
               for left_anchor in left.raw.anchors
               for right_anchor in right.raw.anchors)


def consolidate(findings: Sequence[VerifiedFinding]) -> list[Finding]:
    """Group same-category overlapping findings; worst severity wins (§7.7.4).

    Only ``destination == "findings"`` entries consolidate; the caller keeps
    inbox/discarded ones for ``task_reviews`` and the F4 handoff.
    """
    in_scope = [item for item in findings if item.destination == "findings"]
    groups: list[list[VerifiedFinding]] = []
    for item in in_scope:
        placed = False
        for group in groups:
            if any(_findings_overlap(item, peer) for peer in group):
                group.append(item)
                placed = True
                break
        if not placed:
            groups.append([item])
    consolidated: list[Finding] = []
    for group in groups:
        first = min(group, key=lambda item: (
            _SEVERITY_RANK.get(item.raw.severity, 9), item.slot))
        families: list[str] = []
        quotes: list[str] = []
        explanations: dict[str, str] = {}
        strength = "strong"
        for item in sorted(group, key=lambda entry: entry.slot):
            if item.family not in families:
                families.append(item.family)
            for quote in item.raw.quotes:
                if quote not in quotes:
                    quotes.append(quote)
            if item.raw.explanation and item.family not in explanations:
                explanations[item.family] = item.raw.explanation
            if item.strength == "weak":
                strength = "weak"
        consolidated.append(Finding(
            id=0, severity=first.raw.severity, category=first.raw.category,
            anchors=list(first.raw.anchors), problem=first.raw.problem,
            fix=first.raw.fix, strength=strength, families=families,
            quotes=quotes, explanations=explanations,
            low_priority=first.raw.category == "maintainability"))
    # Worst severity, then most families (§7.8); ids assigned in that order.
    consolidated.sort(key=lambda item: (
        _SEVERITY_RANK.get(item.severity, 9), -len(item.families)))
    for number, item in enumerate(consolidated, 1):
        item.id = number
    return consolidated


def _anchor_text(anchor: Anchor) -> str:
    if anchor.criterion is not None:
        return anchor.criterion
    text = anchor.path or ""
    if anchor.line is not None:
        text += f":{anchor.line}"
    if anchor.base:
        text += "@base"
    return text


def render_findings(
    findings: Sequence[Finding],
    round_no: int,
    head_sha: str,
    builder_family: str = "muse",
) -> str:
    """Render ``FINDINGS_<r>.md`` (§16.5): the only input ``muse`` gets.

    Families matching the builder's own (§7.3) are listed for transparency
    but flagged so the orchestrator never counts them toward consensus.
    """
    lines = [f"FINDINGS R{round_no} · head {head_sha}"]
    if not findings:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    for item in findings:
        anchors = " + ".join(_anchor_text(anchor) for anchor in item.anchors)
        families = ", ".join(
            f"{name} (self-review, not counted)" if name == builder_family else name
            for name in item.families)
        tail = f"   familias: {families}"
        if item.low_priority:
            tail += "   · prioridad baja"
        lines.append(
            f"F{item.id} [{item.severity}] [{item.category}] {anchors} "
            f"({item.strength}) — {item.problem} → {item.fix}{tail}")
        for quote in item.quotes:
            lines.append(f"> {quote}")
        for name, text in item.explanations.items():
            lines.append(f"  {name}: {text}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# task_reviews documents (§15.5, append-only)
# ---------------------------------------------------------------------------

def review_document(
    review_id: str,
    task_id: str,
    run_id: str,
    scope: str,
    round_no: int,
    slot: str,
    family: str,
    valid: bool,
    verdicts: Sequence[VerifiedFinding],
    invalid_reason: str = "",
    meta: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """One document per reviewer report: the source of per-slot precision.

    Every attempt is recorded, valid or not, and each finding carries its
    own text (problem, fix, quotes, explanation; capped) so a closed round
    stays auditable without the scratch files (TASK-2026-00162). ``meta``
    adds the attempt facts (``attempt``, ``kind``, ``elapsed_s``,
    ``exit_code``, ``project``).
    """
    return {
        "review_id": review_id,
        "task_id": task_id,
        "run_id": run_id,
        "scope": scope,  # plan | ticket | objective_final
        "round": round_no,
        "slot": slot,
        "family": family,
        "valid": valid,
        "invalid_reason": invalid_reason,
        **dict(meta or {}),
        "created_at": datetime.now(timezone.utc),
        "findings": [
            {
                "severity": item.raw.severity,
                "category": item.raw.category,
                "anchor": " + ".join(_anchor_text(a) for a in item.raw.anchors),
                "problem": item.raw.problem[:_TEXT_CAP],
                "fix": item.raw.fix[:_TEXT_CAP],
                "quotes": [quote[:_TEXT_CAP] for quote in item.raw.quotes[:4]],
                "explanation": item.raw.explanation[:_TEXT_CAP],
                "verified": item.verified,
                "strength": item.strength,
                "scope": item.scope,
                "destination": item.destination,
                "reason": item.reason,
            }
            for item in verdicts
        ],
    }
