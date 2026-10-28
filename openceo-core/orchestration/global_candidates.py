"""Global rule candidates for global-agent-instructions.md (TASK-2026-00342).

D2: what one project learns never reaches global automatically. Only
cross-project patterns confirmed in at least two distinct projects, or
expressly approved by the Owner, may change the global file. Agents
never write global directly: they leave candidates under
``global_candidates/`` in their ticket folder (manual sessions use the
same YAML in a ticket comment); the Owner confirms before anything is
applied and synced. No tokens, no side effects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from orchestration.agent_sandbox import ticket_dir

__all__ = [
    "GLOBAL_CANDIDATES_DIRNAME",
    "GLOBAL_TARGET",
    "MAX_CANDIDATE_BYTES",
    "MAX_CANDIDATE_FILES",
    "CANDIDATE_FILENAME_SUFFIXES",
    "CANDIDATE_PROMPT",
    "CollectedGlobalCandidate",
    "GlobalCandidate",
    "GlobalEvidence",
    "apply_decision",
    "collect_global_candidates",
    "eligible_for_global",
    "format_candidate_comment",
    "requires_owner_confirmation",
]

#: Ticket-folder dir for global candidates (sibling of knowledge_proposals/).
GLOBAL_CANDIDATES_DIRNAME = "global_candidates"

#: Single global target these candidates may change.
GLOBAL_TARGET = "config/global-agent-instructions.md"

#: Accepted file suffixes for one candidate per file.
CANDIDATE_FILENAME_SUFFIXES = (".yaml", ".yml")

#: Per-file and per-collect caps mirroring the 2.3 proposal gate.
MAX_CANDIDATE_BYTES = 64_000
MAX_CANDIDATE_FILES = 32

#: Instruction text shared by ticket agents and manual sessions.
CANDIDATE_PROMPT = f"""\
To propose a global rule, leave one file per candidate under \
`{GLOBAL_CANDIDATES_DIRNAME}/<topic>.yaml` in your ticket folder (manual \
sessions paste the same YAML in a ticket comment), with `rule` (the exact \
1-2 line global text), `evidence` (at least one: ticket or session plus \
commit/path when available), `projects` (project folders where the pattern \
was observed, e.g. `projects/jardal/<proyecto>`), `risk` (`low` | `medium` \
| `high`) and `owner_approved` (`false` when proposed by an agent). Only \
cross-project patterns qualify: observed in at least 2 distinct projects, \
or expressly approved by the Owner. Never propose single-project practices, \
precedents or notes as-is, and never include secrets. The Owner confirms \
before anything is applied to `{GLOBAL_TARGET}` and synced.\
"""

_SECRET_PATTERNS = (
    r"sk-[A-Za-z0-9]{8,}",
    r"AKIA[0-9A-Z]{16}",
    r"(?i)\bpassword\s*[:=]",
    r"(?i)\bsecret\s*[:=]",
    r"(?i)bearer\s+[A-Za-z0-9._~+/-]+",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
)


class GlobalEvidence(BaseModel):
    """One evidence pointer: a ticket/session plus optional commit/path."""

    model_config = ConfigDict(extra="forbid")

    ticket: str = ""
    session: str = ""
    commit: str = ""
    path: str = ""

    @field_validator("ticket", "session", "commit", "path", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()


class GlobalCandidate(BaseModel):
    """Minimum global-candidate format, validated structurally."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    rule: str
    evidence: list[GlobalEvidence]
    projects: list[str]
    risk: Literal["low", "medium", "high"]
    owner_approved: bool = False

    @field_validator("rule", mode="before")
    @classmethod
    def _strip_rule(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("projects", mode="before")
    @classmethod
    def _clean_projects(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip().strip("/") for item in value
                if str(item or "").strip()]


@dataclass(frozen=True)
class CollectedGlobalCandidate:
    """One structurally sound candidate, ready for Owner confirmation."""

    name: str
    source: Path
    rule: str
    projects: list[str]
    risk: str
    owner_approved: bool
    evidence: list[dict[str, str]]


def requires_owner_confirmation() -> bool:
    """Global changes always need Owner confirmation (principle 5)."""
    return True


def _secret_hit(rule: str) -> bool:
    return any(re.search(pattern, rule) for pattern in _SECRET_PATTERNS)


def _meets_cross_project_criterion(projects: Sequence[str], owner_approved: bool) -> bool:
    distinct = {project for project in projects if project}
    return len(distinct) >= 2 or bool(owner_approved)


def eligible_for_global(candidate: GlobalCandidate) -> tuple[bool, str]:
    """Whether the candidate meets the cross-project criterion."""
    if not _meets_cross_project_criterion(candidate.projects, candidate.owner_approved):
        return False, (
            "needs a second project or Owner express approval: "
            "single-project patterns never go global (D2)")
    if len({project for project in candidate.projects if project}) >= 2:
        return True, "observed in 2+ distinct projects"
    return True, "Owner express approval recorded"


def _check_candidate(data: Any) -> CollectedGlobalCandidate | str:
    if not isinstance(data, dict):
        return "candidate file is not a YAML mapping"
    try:
        candidate = GlobalCandidate.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item.get('loc', ())) or '?'}: "
            f"{item.get('msg', 'invalid value')}"
            for item in exc.errors())
        return f"invalid schema: {details or 'invalid candidate'}"[:500]
    if not candidate.rule:
        return "rule is empty: the exact 1-2 line global text is required"
    if len(candidate.rule) > 500:
        return "rule exceeds 500 chars: keep global rules to 1-2 lines"
    if _secret_hit(candidate.rule):
        return "candidate may contain a secret: never propose secrets"
    if not candidate.projects:
        return "projects is empty: list where the pattern was observed"
    kept = [entry for entry in candidate.evidence
            if entry.ticket or entry.session or entry.commit or entry.path]
    if not kept:
        return "evidence is missing: at least one ticket, session, commit or path"
    return CollectedGlobalCandidate(
        name="",
        source=Path(),
        rule=candidate.rule,
        projects=list(candidate.projects),
        risk=candidate.risk,
        owner_approved=bool(candidate.owner_approved),
        evidence=[{key: value for key, value in entry.model_dump().items()
                   if value} for entry in kept],
    )


def collect_global_candidates(
        task_id: str, *,
        base_dir: str | Path | None = None,
) -> tuple[list[CollectedGlobalCandidate], dict[str, str]]:
    """Read global candidates structurally, never blocking the close."""
    try:
        target = ticket_dir(task_id, base_dir=(
            Path(base_dir) if base_dir is not None else None)) \
            / GLOBAL_CANDIDATES_DIRNAME
    except ValueError as exc:
        return [], {"": f"invalid ticket id: {exc}"[:300]}
    try:
        entries = sorted(target.iterdir(), key=lambda item: item.name)
    except OSError:
        return [], {}
    valid: list[CollectedGlobalCandidate] = []
    rejected: dict[str, str] = {}
    eligible = [
        entry for entry in entries
        if not entry.name.startswith(".") and not entry.is_symlink()
        and entry.is_file()
        and entry.suffix.lower() in CANDIDATE_FILENAME_SUFFIXES
    ]
    for entry in eligible[:MAX_CANDIDATE_FILES]:
        try:
            if entry.stat().st_size > MAX_CANDIDATE_BYTES:
                rejected[entry.name] = (
                    f"candidate exceeds {MAX_CANDIDATE_BYTES} bytes")[:300]
                continue
        except OSError as exc:
            rejected[entry.name] = f"candidate is unreadable ({exc})"[:300]
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            rejected[entry.name] = f"candidate is unreadable ({exc})"[:300]
            continue
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            rejected[entry.name] = f"candidate is not valid YAML ({exc})"[:300]
            continue
        outcome = _check_candidate(data)
        if isinstance(outcome, str):
            rejected[entry.name] = outcome
            continue
        valid.append(CollectedGlobalCandidate(
            name=entry.stem, source=entry, rule=outcome.rule,
            projects=outcome.projects, risk=outcome.risk,
            owner_approved=outcome.owner_approved,
            evidence=outcome.evidence))
    for entry in eligible[MAX_CANDIDATE_FILES:]:
        rejected[entry.name] = (
            f"too many candidate files (cap {MAX_CANDIDATE_FILES})")[:300]
    return valid, rejected


def apply_decision(candidate: CollectedGlobalCandidate,
                   answer: str | None) -> tuple[str, str]:
    """Owner gate outcome: (verdict, reason).

    Verdicts: ``pending`` (no answer yet), ``discarded`` (Owner denied),
    ``deferred`` (approved but criterion unmet) or ``ready_to_apply``
    (approved and criterion met; the applier writes ``GLOBAL_TARGET``
    and syncs, never the agent).
    """
    if answer not in ("approve", "deny"):
        return "pending", "awaiting Owner confirmation: nothing applies"
    if answer == "deny":
        return "discarded", "Owner denied: candidate discarded with reason"
    if _meets_cross_project_criterion(candidate.projects, candidate.owner_approved):
        return "ready_to_apply", "Owner approved an eligible candidate"
    return ("deferred",
            "Owner approval needs a second project or express approval "
            "for single-project patterns (D2)")


def format_candidate_comment(
        valid: Sequence[CollectedGlobalCandidate],
        rejected: Mapping[str, str]) -> str | None:
    """Ticket comment for a collect: None when there is nothing."""
    names = [item.name for item in valid]
    if not names and not rejected:
        return None
    lines = [f"Global candidates (1.7): {len(names)} valid, "
             f"{len(rejected)} rejected. Owner confirmation required."]
    if names:
        lines.append("Valid, awaiting Owner confirmation:")
        lines.extend(f"- {name}" for name in sorted(names))
    if rejected:
        lines.append("Rejected with reason:")
        lines.extend(f"- {name}: {reason}"
                     for name, reason in sorted(rejected.items()))
    return "\n".join(lines)[:8000]
