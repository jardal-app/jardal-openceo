"""Small, deterministic contracts for a Tier S attempt.

The files in .openceo are an agent interface. MongoDB owns the process state.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Mapping


# Child process environment (TASK-2026-00192): allowlist, not denylist.
# Agent CLIs inherit only operational locale/terminal/identity vars; every
# service credential stays out. The dsh re-billing trap (TASK-2026-00105) is
# covered structurally: DEEPSEEK_API_KEY is not allowlisted, so dsh falls
# back to the operator's own $DSH_HOME/.credentials.yaml.
CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM",
    "LANG", "LC_ALL", "LANGUAGE", "TZ", "TMPDIR", "NO_COLOR",
})

# Backstop: a base key matching these never passes, even if allowlisted.
# Mirrors research/runtimes/isolation.py, extended with agent-relevant
# providers. The overlay is exempt: explicit config is operator intent.
_SECRET_ENV_PREFIXES = (
    "ANTHROPIC_", "AWS_", "BRAVE_", "CLOUDFLARE_", "DEEPSEEK_",
    "ERPNEXT_", "FIRECRAWL_", "GROQ_", "JWT_", "MCP_", "META_",
    "MONGODB_", "N8N_", "NOTION_", "OPENCEO_", "OPENCODE_",
    "OPENWEBUI_", "R2_", "RESEARCH_", "SERPER_", "SMTP_",
)
_SECRET_ENV_SUFFIXES = (
    "_API_KEY", "_TOKEN", "_SECRET", "_SECRETS", "_PASSWORD", "_PASS",
    "_CREDENTIAL", "_CREDENTIALS", "_AUTH",
)


def _looks_like_secret(key: str) -> bool:
    upper = str(key).upper()
    return (upper.startswith(_SECRET_ENV_PREFIXES)
            or upper.endswith(_SECRET_ENV_SUFFIXES))


def child_env(base: Mapping[str, str], overlay: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merge a child process env over ``base`` without leaking credentials.

    From ``base`` only allowlisted, non-secret-looking keys survive.
    The overlay applies verbatim: explicit config is operator intent
    (motor auth, ticket badges, per-slot env) and wins.
    """
    overlay = {str(k): str(v) for k, v in dict(overlay or {}).items()}
    merged = {
        str(k): str(v) for k, v in dict(base).items()
        if k in CHILD_ENV_ALLOWLIST and not _looks_like_secret(k)
    }
    merged.update(overlay)
    return merged


@dataclass(frozen=True)
class GitSnapshot:
    files: dict[str, str | None]
    digest: str

    def changed_since(self, previous: "GitSnapshot") -> list[str]:
        missing = object()
        return sorted(
            path for path in self.files.keys() | previous.files.keys()
            if self.files.get(path, missing) != previous.files.get(path, missing)
        )


def git_snapshot(repo: Path) -> GitSnapshot:
    """Hash real working-tree changes, including untracked files and deletions."""
    names: set[str] = set()
    for args in (("diff", "--no-renames", "--name-only", "-z", "HEAD"),
                 ("ls-files", "--others", "--exclude-standard", "-z")):
        result = subprocess.run(
            ["git", *args], cwd=repo, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        )
        names.update(os.fsdecode(part) for part in result.stdout.split(b"\0") if part)
    files: dict[str, str | None] = {}
    for name in sorted(names):
        if name == ".openceo" or name.startswith(".openceo/"):
            continue
        path = repo / name
        if path.is_symlink():
            data = os.readlink(path).encode()
        elif path.is_file():
            data = path.read_bytes()
        elif path.exists():
            continue
        else:
            files[name] = None
            continue
        files[name] = hashlib.sha256(data).hexdigest()
    digest = hashlib.sha256(repr(sorted(files.items())).encode()).hexdigest()
    return GitSnapshot(files, digest)


# Severity vocabulary of §16.4 (findings.SEVERITIES); the retired SUGGEST
# rejected INFO-only reports (TASK-2026-00162, found by 3 pilot reviewers).
_FINDING_RE = re.compile(r"^\[(CRITICAL|WARN|INFO)\] .+", re.MULTILINE)


def valid_review(text: str) -> bool:
    body = text.strip()
    return bool(body and (re.search(r"^No findings\.$", body, re.MULTILINE) or _FINDING_RE.search(body)))


def blocking_findings(text: str) -> bool:
    return bool(re.search(r"^\[(CRITICAL|WARN)\] ", text, re.MULTILINE))


def review_quorum(
    reports: dict[str, str],
    families: dict[str, str],
    extra: Collection[str] = (),
) -> bool:
    """Two valid reports from two distinct model families.

    Tier S design §7 (F0): codexj is out of code review, so any two pool
    slots from distinct families form a quorum. ``families`` maps reviewer
    label to its declared family from the global pool. Fail-closed: a
    report whose label has no known family never counts toward quorum.
    ``extra`` holds self-review slots (design v2 §7.3, e.g. ``muse-review``
    on a muse-built ticket): their reports arrive as information but never
    count toward quorum.
    """
    excluded = set(extra)
    seen: set[str] = set()
    for label, text in reports.items():
        if label in excluded:
            continue
        if not valid_review(text):
            continue
        family = (families.get(label) or "").strip()
        if not family:
            continue
        seen.add(family)
        if len(seen) >= 2:
            return True
    return False


# ---------------------------------------------------------------------------
# Non-blocking plan review (design v2 §6.4, §16.2, §16.3 — F0.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PlanObservation:
    """One numbered codexj observation from PLAN_REVIEW.md."""

    id: int
    severity: str  # CRITICAL | WARN
    anchor: str  # e.g. "CURRENT_PLAN.md:12" or "src/a.py:42"
    path: str  # anchor path without the line suffix
    line: int | None
    problem: str
    suggestion: str


_PLAN_OBSERVATION_RE = re.compile(
    r"^O(\d+)\s+\[(CRITICAL|WARN)\]\s+(\S+)\s+[—–-]\s*(.+?)\s*(?:→|->)\s*(.+?)\s*$"
)
_PLAN_RESPONSE_RE = re.compile(
    r"^O(\d+)\s*:\s*(ACEPTADA|RECHAZADA|DIFERIDA)\b\s*(?:[—–-]\s*(.*))?$",
    re.IGNORECASE,
)
_PLAN_DONE_RE = re.compile(r"^REVIEW_DONE_plan_0\s*$", re.MULTILINE)


def parse_plan_observations(report: str) -> list[PlanObservation]:
    """Parse the ``O<n>`` observations of a plan review (design §16.2).

    Malformed lines are skipped (callers treat zero observations from a
    non-empty report as an invalid report). The optional trailing
    ``REVIEW_DONE_plan_0`` signal and ``>`` quote lines are ignored.
    """
    observations: list[PlanObservation] = []
    for raw_line in report.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(">") or _PLAN_DONE_RE.match(line):
            continue
        match = _PLAN_OBSERVATION_RE.match(line)
        if not match:
            continue
        anchor = match.group(3)
        path, _, line_part = anchor.rpartition(":")
        line_no: int | None = None
        if path and line_part.isdigit():
            line_no = int(line_part)
        else:
            path = anchor
        observations.append(PlanObservation(
            id=int(match.group(1)),
            severity=match.group(2),
            anchor=anchor,
            path=path,
            line=line_no,
            problem=match.group(4).strip(),
            suggestion=match.group(5).strip(),
        ))
    return observations


def valid_plan_review(report: str) -> bool:
    """A plan review is valid with zero findings or ≥1 observation."""
    body = report.strip()
    if not body:
        return False
    if re.search(r"^No findings\.$", body, re.MULTILINE):
        return True
    return bool(parse_plan_observations(body))


def parse_plan_responses(text: str) -> dict[int, list[str]]:
    """Parse PLAN_RESPONSE.md (design §16.3): observation id → verdicts.

    Verdicts are normalized to ``ACEPTADA``/``RECHAZADA``/``DIFERIDA``.
    Repeated ids keep every verdict so the completeness check can flag
    duplicates (§6.4: each ``O<n>`` has exactly one response).
    """
    responses: dict[int, list[str]] = {}
    for raw_line in text.splitlines():
        match = _PLAN_RESPONSE_RE.match(raw_line.strip())
        if not match:
            continue
        responses.setdefault(int(match.group(1)), []).append(match.group(2).upper())
    return responses


def plan_response_gaps(
    observations: list[PlanObservation],
    responses: dict[int, list[str]],
) -> tuple[list[int], list[int]]:
    """Missing and duplicated observation ids in a plan response.

    Returns ``(missing, duplicated)``, both sorted. Responses to unknown
    ids are ignored (they answer nothing).
    """
    wanted = {obs.id for obs in observations}
    missing = sorted(wanted - set(responses))
    duplicated = sorted(obs_id for obs_id in wanted
                        if len(responses.get(obs_id, ())) > 1)
    return missing, duplicated


def verify_plan_anchor(
    observation: PlanObservation,
    plan_path: Path,
    repo_path: Path,
) -> bool:
    """Mechanical anchor check: the file exists and the line is in range.

    F0.1 covers existence + range; quote matching joins the shared
    verification module in F3 (§7.11). The plan file may be cited bare
    (``CURRENT_PLAN.md``) or with the ``.openceo/`` prefix.
    """
    name = observation.path.strip().lstrip("./")
    if name in {"CURRENT_PLAN.md", ".openceo/CURRENT_PLAN.md", plan_path.name}:
        target = plan_path
    else:
        target = repo_path / name
    try:
        resolved = target.resolve()
        repo_root = repo_path.resolve()
    except OSError:
        return False
    if resolved != repo_root and repo_root not in resolved.parents:
        return False
    if not resolved.is_file():
        return False
    if observation.line is None:
        return True
    try:
        total = sum(1 for _ in resolved.open("r", encoding="utf-8", errors="replace"))
    except OSError:
        return False
    return 1 <= observation.line <= total


_QUESTION_OPTION = re.compile(r"(?m)^\s*(?P<selected>[❯›])?\s*(?P<number>[1-9])[.)]\s+(?P<text>.{2,180})$")


def detect_claude_question(pane: str) -> dict[str, object] | None:
    """Conservative TUI fallback; a timeout alone never approves a plan."""
    tail = "\n".join(pane.splitlines()[-45:])
    last_line = next((line.strip() for line in reversed(tail.splitlines()) if line.strip()), "")
    if not last_line.startswith(("Enter to select", "Intro para seleccionar")):
        return None
    option_matches = list(_QUESTION_OPTION.finditer(tail))
    if not option_matches:
        return None
    group_start = next((match.start() for match in reversed(option_matches)
                        if match.group("number") == "1"), option_matches[0].start())
    question_lines = [line.strip() for line in tail[:group_start].splitlines()
                      if line.strip().endswith("?")]
    if not question_lines:
        return None
    after = tail[group_start:]
    options = []
    option_indices = []
    selected = 0
    input_index = None
    for match in _QUESTION_OPTION.finditer(after):
        number = int(match.group("number")) - 1
        value = match.group("text").strip()
        if match.group("selected"):
            selected = number
        if value.lower().startswith("type something"):
            input_index = number
        elif not value.lower().startswith("chat about this"):
            options.append(value)
            option_indices.append(number)
    if not options:
        return None
    return {"text": question_lines[-1], "options": options[:8],
            "option_indices": option_indices[:8],
            "selected_index": selected, "input_index": input_index}
