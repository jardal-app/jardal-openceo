"""Single-plan parsing and structural validation (design §6.2–§6.3, F2).

No subplans: one ``PLAN.md`` per ticket (§16.1). Validation is pure Python —
no LLM judges it. Line estimates are the planner's guess; the real line
control happens on the git diff after the build (§6.6).
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
    valid: bool
    errors: tuple[str, ...] = ()
    files_counted: int = 0  # non-test, non-excluded files (budget §13)

    def to_dict(self) -> dict[str, object]:
        return {"valid": self.valid, "errors": list(self.errors),
                "files_counted": self.files_counted}


_SECTION_RE = re.compile(r"^(TICKET|OBJECTIVE|GOAL|ACCEPTANCE|FILES|CONTRACTS|DECISIONS|STEPS|ESTIMATE|OUT_OF_SCOPE)\s*:\s*(.*)$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")
_STEP_RE = re.compile(r"^\d+\.\s+(.*)$")
_FILE_RE = re.compile(r"^(\S+)\s+\((create|modify|delete)\)\s*[—–-]\s*(.+)$")
_ESTIMATE_RE = re.compile(r"files\s*=\s*(\d+)\s+lines\s*=\s*(\d+)")

#: Paths that never count toward the file budget (§13: "sin tests").
_TEST_HINTS = ("tests/", "test/", "/tests/", "/test/", "conftest.py")


def _is_test_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return (
        lowered.startswith(("tests/", "test/"))
        or "/tests/" in lowered
        or "/test/" in lowered
        or lowered.endswith("_test.py")
        or lowered.startswith("test_")
        or lowered == "conftest.py"
    )


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

    est_files: int | None = None
    est_lines: int | None = None
    for line in sections.get("ESTIMATE", []):
        match = _ESTIMATE_RE.search(line)
        if match:
            est_files, est_lines = int(match.group(1)), int(match.group(2))
    inline_est = inline.get("ESTIMATE", "")
    if est_files is None and inline_est:
        match = _ESTIMATE_RE.search(inline_est)
        if match:
            est_files, est_lines = int(match.group(1)), int(match.group(2))

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
) -> PlanValidation:
    """Structural validation (design §6.3): format, routes, file budget."""
    errors: list[str] = []
    if not plan.ticket:
        errors.append("missing TICKET")
    if not plan.goal:
        errors.append("missing GOAL")
    if not plan.acceptance:
        errors.append("missing ACCEPTANCE criteria")
    if not plan.files:
        errors.append("missing FILES")
    if not plan.steps:
        errors.append("missing STEPS")

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
        if not excluded and not _is_test_path(item.path):
            counted += 1

    if counted > budget_files:
        errors.append(
            f"file budget exceeded: {counted} files > {budget_files} "
            "(→ objective conversion, §6.3)")
    return PlanValidation(valid=not errors, errors=tuple(errors),
                          files_counted=counted)


def plan_declares_objective_needed(status_text: str) -> bool:
    """True when the planner sized the ticket out (design §6.2)."""
    return "OBJECTIVE_NEEDED" in (status_text or "")
