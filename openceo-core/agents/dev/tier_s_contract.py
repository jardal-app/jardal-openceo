"""Small, deterministic contracts for a Tier S attempt.

The files in .tier-s are an agent interface. MongoDB owns the process state.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


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
        if name == ".tier-s" or name.startswith(".tier-s/"):
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


_FINDING_RE = re.compile(r"^\[(CRITICAL|WARN|SUGGEST)\] .+", re.MULTILINE)


def valid_review(text: str) -> bool:
    body = text.strip()
    return bool(body and (re.search(r"^No findings\.$", body, re.MULTILINE) or _FINDING_RE.search(body)))


def blocking_findings(text: str) -> bool:
    return bool(re.search(r"^\[(CRITICAL|WARN)\] ", text, re.MULTILINE))


def review_quorum(reports: dict[str, str], families: dict[str, str]) -> bool:
    """Two valid reports from two distinct model families.

    Tier S design §7 (F0): codexj is out of code review, so any two pool
    slots from distinct families form a quorum. ``families`` maps reviewer
    label to its declared family from the global pool. Fail-closed: a
    report whose label has no known family never counts toward quorum.
    """
    seen: set[str] = set()
    for label, text in reports.items():
        if not valid_review(text):
            continue
        family = (families.get(label) or "").strip()
        if not family:
            continue
        seen.add(family)
        if len(seen) >= 2:
            return True
    return False


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
