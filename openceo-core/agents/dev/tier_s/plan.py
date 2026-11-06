"""Single-plan parsing and structural validation (design §6.2–§6.3, F2).

No subplans: one ``PLAN.md`` per ticket (§16.1). Validation is pure Python —
no LLM judges it. The planner's line estimate is held to the budget here, so
an oversized ticket converts before any build; the hard control stays on the
git diff after the build (§6.6).
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class PlannedFile:
    path: str
    operation: str  # create | modify | delete
    reason: str


@dataclass(frozen=True)
class Plan:
    ticket: str
    objective: str
    goal: str
    acceptance: tuple[str, ...]
    files: tuple[PlannedFile, ...]
    contracts: tuple[str, ...]
    decisions: tuple[str, ...]
    steps: tuple[str, ...]
    estimate_files: int | None
    estimate_lines: int | None
    out_of_scope: tuple[str, ...]


@dataclass(frozen=True)
class PlanValidation:
    """Errors make a plan unbuildable or unsafe; warnings never stop it."""

    valid: bool
    errors: tuple[str, ...] = ()
    files_counted: int = 0  # non-test, non-excluded files (budget §13)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"valid": self.valid, "errors": list(self.errors),
                "warnings": list(self.warnings),
                "files_counted": self.files_counted}


_SECTION_RE = re.compile(r"^(TICKET|OBJECTIVE|GOAL|ACCEPTANCE|FILES|CONTRACTS|DECISIONS|STEPS|ESTIMATE|OUT_OF_SCOPE)\s*:\s*(.*)$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")
_STEP_RE = re.compile(r"^\d+\.\s+(.*)$")
_FILE_RE = re.compile(r"^(\S+)\s+\((create|modify|delete)\)\s*[—–-]\s*(.+)$")
_ESTIMATE_RE = re.compile(r"files\s*=\s*(\d+)\s+lines\s*=\s*(\d+)")
_ESTIMATE_LINE_RE = re.compile(r"^\s*ESTIMATE\b\s*:?\s*(.*)$", re.IGNORECASE)
_EST_FILES_RE = re.compile(
    r"(?:files?|ficheros?|archivos?)\s*[=:]?\s*~?(\d+)"
    r"|(\d+)\s*(?:files?|ficheros?|archivos?)\b", re.IGNORECASE)
_EST_LINES_RE = re.compile(
    r"(?:lines?|l[ií]neas?)\s*[=:]?\s*~?(\d+)"
    r"|(\d+)\s*(?:lines?|l[ií]neas?)\b", re.IGNORECASE)

_TEST_DIRS = frozenset({"tests", "test", "__tests__"})
_TEST_NAME_RE = re.compile(r"^test_|_test\.|\.(test|spec)\.")


def is_test_path(path: str) -> bool:
    """True for test code: it never counts toward the size budgets (§13).

    One rule for the plan validator and the size brake on the diff.
    """
    parts = path.replace("\\", "/").lower().split("/")
    name = parts[-1]
    return (any(part in _TEST_DIRS for part in parts[:-1])
            or name == "conftest.py"
            or bool(_TEST_NAME_RE.search(name)))


def parse_plan(text: str) -> Plan:
    """Parse §16.1. Unknown lines are ignored; validation reports gaps."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    inline: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        match = _SECTION_RE.match(line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
            if match.group(2):
                inline[current] = match.group(2).strip()
            continue
        if current is None or not line:
            continue
        sections[current].append(line)

    def bullets(name: str) -> tuple[str, ...]:
        items: list[str] = []
        for line in sections.get(name, []):
            match = _BULLET_RE.match(line)
            items.append(match.group(1).strip() if match else line)
        return tuple(item for item in items if item)

    files: list[PlannedFile] = []
    for line in sections.get("FILES", []):
        match = _FILE_RE.match(line[2:].strip() if line.startswith("- ") else line)
        if not match:
            files.append(PlannedFile(path=line, operation="?", reason=""))
            continue
        files.append(PlannedFile(
            path=match.group(1), operation=match.group(2),
            reason=match.group(3).strip()))

    steps: list[str] = []
    for line in sections.get("STEPS", []):
        match = _STEP_RE.match(line)
        steps.append(match.group(1).strip() if match else line)

    # The planner prompt long showed "ESTIMATE files=<n> lines=<n>" without
    # a colon, so a line the section grammar misses still counts.
    loose = [match.group(1) for line in text.splitlines()
             if (match := _ESTIMATE_LINE_RE.match(line))]
    est_files, est_lines = _parse_estimate(
        [inline.get("ESTIMATE", ""), *sections.get("ESTIMATE", []), *loose])

    return Plan(
        ticket=inline.get("TICKET", ""),
        objective=inline.get("OBJECTIVE", "none"),
        goal=inline.get("GOAL", "") or " ".join(sections.get("GOAL", [])),
        acceptance=bullets("ACCEPTANCE"),
        files=tuple(files),
        contracts=bullets("CONTRACTS"),
        decisions=bullets("DECISIONS"),
        steps=tuple(s for s in steps if s),
        estimate_files=est_files,
        estimate_lines=est_lines,
        out_of_scope=bullets("OUT_OF_SCOPE"),
    )


def _parse_estimate(lines: Sequence[str]) -> tuple[int | None, int | None]:
    """Canonical ``files=<n> lines=<n>`` first, then any wording of it."""
    files: int | None = None
    count: int | None = None
    for line in lines:
        match = _ESTIMATE_RE.search(line or "")
        if match:
            return int(match.group(1)), int(match.group(2))
        if files is None and (found := _EST_FILES_RE.search(line or "")):
            files = int(next(group for group in found.groups() if group))
        if count is None and (found := _EST_LINES_RE.search(line or "")):
            count = int(next(group for group in found.groups() if group))
    return files, count


def _inside_repo(repo: Path, raw_path: str) -> Path | None:
    """Resolve a planned path; None when it escapes the repo or is absolute."""
    cleaned = raw_path.strip().lstrip("./")
    if not cleaned or cleaned.startswith(("/", "~")) or ".." in cleaned.split("/"):
        return None
    try:
        target = (repo / cleaned).resolve()
        root = repo.resolve()
    except OSError:
        return None
    if target != root and root not in target.parents:
        return None
    return target


def validate_plan(
    plan: Plan,
    repo: Path,
    budget_files: int,
    budget_exclude: Sequence[str] = (),
    budget_lines: int | None = None,
) -> PlanValidation:
    """Structural validation (design §6.3): format, routes, size budget."""
    errors: list[str] = []
    warnings: list[str] = []
    if not plan.ticket:
        warnings.append("TICKET line missing (informational)")
    if not plan.goal:
        errors.append("missing GOAL")
    if not plan.acceptance:
        errors.append("missing ACCEPTANCE criteria")
    if not plan.files:
        errors.append("missing FILES")
    if not plan.steps:
        errors.append("missing STEPS")
    if plan.estimate_lines is None:
        # The size brake on the real diff still bounds the build (§6.6).
        warnings.append("ESTIMATE lines missing: plan-time line check "
                        "skipped, the build-time size brake still applies")

    counted = 0
    for item in plan.files:
        if item.operation not in {"create", "modify", "delete"}:
            errors.append(f"bad FILES line: {item.path}")
            continue
        target = _inside_repo(repo, item.path)
        if target is None:
            errors.append(f"path outside repo: {item.path}")
            continue
        if item.operation in {"modify", "delete"} and not target.exists():
            errors.append(f"{item.operation} missing file: {item.path}")
        if item.operation == "create" and target.exists():
            errors.append(f"create over existing file: {item.path}")
        excluded = any(fnmatch.fnmatch(item.path, pat)
                       for pat in budget_exclude)
        if not excluded and not is_test_path(item.path):
            counted += 1

    if counted > budget_files:
        errors.append(
            f"file budget exceeded: {counted} files > {budget_files} "
            "(→ objective conversion, §6.3)")
    if (budget_lines is not None and plan.estimate_lines is not None
            and plan.estimate_lines > budget_lines):
        errors.append(
            f"line budget exceeded: {plan.estimate_lines} lines > "
            f"{budget_lines} (→ objective conversion, §6.3)")
    return PlanValidation(valid=not errors, errors=tuple(errors),
                          files_counted=counted, warnings=tuple(warnings))


def plan_declares_objective_needed(status_text: str) -> bool:
    """True when the planner sized the ticket out (design §6.2)."""
    return "OBJECTIVE_NEEDED" in (status_text or "")
