"""Skill version metrics, revert proposals and retirement (TASK-2026-00334).

D9: every skill version records uses, verified successes, blocked /
not_applicable returns and rework. A regression against the previous
version opens a revert proposal (Owner channel or normative proposal)
and never reverts alone. Unused or low-success skills retire. Skills
are edited with small evidenced changes, never full rewrites. Metrics
are visible per version to the API/dashboard surfaces.

No tokens, no ticket outcome changes. Thresholds are agent decisions,
reopenable by the Owner (recorded in TASK-2026-00334).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "MAX_CLOSES_WITHOUT_USE",
    "MIN_SUCCESS_RATE",
    "MIN_USES_BEFORE_RETIRE",
    "OUTCOMES",
    "REGRESSION_MIN_DROP",
    "REGRESSION_MIN_USES",
    "REWRITE_MIN_SIMILARITY",
    "STORE_FILENAME",
    "RevertProposal",
    "VersionMetrics",
    "closes_since_use",
    "default_store_path",
    "empty_store",
    "format_metrics_lines",
    "format_revert_question",
    "is_full_rewrite",
    "latest_version",
    "load_store",
    "mark_close",
    "metrics_summary",
    "record_use",
    "record_use_of_skill",
    "regression_decision",
    "retire_check",
    "retire_decision",
    "save_store",
    "success_rate",
    "version_of_skill",
]

#: Valid close outcomes for one skill use (D9 + principle 7: only
#: OpenCEO-verified results count; inconclusive closes are not recorded).
OUTCOMES = ("verified_success", "rework", "blocked", "not_applicable")

#: Runtime metrics file, next to the crystallization lock (gitignored).
STORE_FILENAME = "skill_metrics.json"

#: Retire when uses reach this count with success below MIN_SUCCESS_RATE.
MIN_USES_BEFORE_RETIRE = 5

#: Minimum verified-success share to keep a skill with enough uses.
MIN_SUCCESS_RATE = 0.4

#: Retire when a skill records no use across this many closes.
MAX_CLOSES_WITHOUT_USE = 10

#: A new version needs this many uses before a regression can open.
REGRESSION_MIN_USES = 5

#: Minimum absolute success-rate drop vs the previous version.
REGRESSION_MIN_DROP = 0.25

#: Bodies less similar than this are a full rewrite, never an edit.
REWRITE_MIN_SIMILARITY = 0.25


@dataclass
class VersionMetrics:
    """Counters for one skill version (``version`` is a content digest)."""

    version: str
    uses: int = 0
    verified_successes: int = 0
    blocked: int = 0
    not_applicable: int = 0
    rework: int = 0
    last_ticket: str | None = None


@dataclass
class RevertProposal:
    """A regression opens this; applying it is the Owner's decision."""

    role: str
    name: str
    previous_version: str
    current_version: str
    reason: str
    evidence_ticket: str


def empty_store() -> dict[str, Any]:
    """A fresh metrics store: one close counter plus per-skill entries."""
    return {"closes": 0, "skills": {}}


def default_store_path(repo_root: str | Path) -> Path:
    """Runtime metrics file for one checkout (gitignored, never committed)."""
    return Path(repo_root) / ".runtime" / STORE_FILENAME


def load_store(path: str | Path) -> dict[str, Any]:
    """Read a metrics store; missing or corrupt reads as empty."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_store()
    if not isinstance(data, dict) or not isinstance(
            data.get("skills"), dict):
        return empty_store()
    return data


def save_store(store: dict[str, Any], path: str | Path) -> None:
    """Persist a metrics store, creating the runtime dir as needed."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(store, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8",
                                     dir=str(target.parent),
                                     delete=False) as tmp:
        tmp.write(payload)
        tmp_path = tmp.name
    os.replace(tmp_path, target)


def _entry(store: dict[str, Any], role: str, name: str) -> dict[str, Any]:
    skills = store.setdefault("skills", {})
    key = f"{role}/{name}"
    entry = skills.setdefault(key, {"versions": {}, "order": [],
                                    "last_use_close": None})
    entry.setdefault("versions", {})
    entry.setdefault("order", [])
    return entry


def record_use(store: dict[str, Any], *, role: str, name: str,
               version: str, outcome: str, ticket: str) -> VersionMetrics:
    """Record one close that used one skill version.

    Raises ``ValueError`` on an unknown outcome (fail-closed: only
    verified results count, never guesses).
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown skill outcome {outcome!r}")
    if not version or not ticket:
        raise ValueError("skill metrics need a version and a ticket")
    entry = _entry(store, role, name)
    raw = entry["versions"].setdefault(version, {"version": version})
    metrics = VersionMetrics(**{**raw})
    metrics.uses += 1
    if outcome == "verified_success":
        metrics.verified_successes += 1
    elif outcome == "blocked":
        metrics.blocked += 1
    elif outcome == "not_applicable":
        metrics.not_applicable += 1
    else:
        metrics.rework += 1
    metrics.last_ticket = ticket
    entry["versions"][version] = asdict(metrics)
    if version not in entry["order"]:
        entry["order"].append(version)
    entry["last_use_close"] = store.get("closes", 0)
    return metrics


def record_use_of_skill(store: dict[str, Any], *, role: str, name: str,
                        skill_dir: str | Path, outcome: str,
                        ticket: str) -> VersionMetrics:
    """Record one close binding the version to the skill dir contents.

    The version digest comes from ``version_of_skill`` so callers never
    invent it. No automatic caller exists yet: nothing tracks which
    skill version a ticket used, so the close hook that will call this
    is a seam for future wiring, not dead code to invoke speculatively.
    """
    return record_use(store, role=role, name=name,
                      version=version_of_skill(skill_dir),
                      outcome=outcome, ticket=ticket)


def mark_close(store: dict[str, Any]) -> int:
    """Advance the close counter; staleness is measured in closes."""
    store["closes"] = int(store.get("closes", 0)) + 1
    return store["closes"]


def success_rate(metrics: VersionMetrics) -> float:
    """Verified-success share, 0.0 before any recorded use."""
    if metrics.uses <= 0:
        return 0.0
    return metrics.verified_successes / metrics.uses


def latest_version(store: Mapping[str, Any], role: str,
                   name: str) -> VersionMetrics | None:
    """Newest recorded version of one skill, None when never used."""
    entry = store.get("skills", {}).get(f"{role}/{name}")
    if not entry or not entry.get("order"):
        return None
    raw = entry["versions"][entry["order"][-1]]
    return VersionMetrics(**raw)


def closes_since_use(store: Mapping[str, Any], role: str,
                     name: str) -> int | None:
    """Closes since the last recorded use, None when never used."""
    entry = store.get("skills", {}).get(f"{role}/{name}")
    if not entry or entry.get("last_use_close") is None:
        return None
    return int(store.get("closes", 0)) - int(entry["last_use_close"])


def regression_decision(previous: VersionMetrics,
                        current: VersionMetrics) -> str | None:
    """Reason to propose reverting, None when the version holds.

    A regression opens only with enough uses on both versions and a
    material success-rate drop; the proposal never reverts alone.
    """
    if current.uses < REGRESSION_MIN_USES:
        return None
    if previous.uses < REGRESSION_MIN_USES:
        return None
    drop = success_rate(previous) - success_rate(current)
    if drop < REGRESSION_MIN_DROP:
        return None
    return (f"success {success_rate(previous):.0%} -> "
            f"{success_rate(current):.0%} over {current.uses} uses "
            f"(drop {drop:.0%} >= {REGRESSION_MIN_DROP:.0%})")


def retire_decision(metrics: VersionMetrics | None,
                    idle_closes: int | None) -> str | None:
    """Reason to retire a skill, None when it stays.

    Retires skills with enough uses but low verified success, or
    skills idle past the grace period. A skill with no recorded entry
    at all retires once the store lived past grace (the caller passes
    the total close count as ``idle_closes``). New skills (no recorded
    use within grace) are never retired.
    """
    if metrics is None:
        if idle_closes is not None \
                and idle_closes >= MAX_CLOSES_WITHOUT_USE:
            return f"no recorded use across {idle_closes} closes"
        return None
    if metrics.uses >= MIN_USES_BEFORE_RETIRE \
            and success_rate(metrics) < MIN_SUCCESS_RATE:
        return (f"low success {success_rate(metrics):.0%} over "
                f"{metrics.uses} uses "
                f"(needs {MIN_SUCCESS_RATE:.0%})")
    if metrics.uses == 0 and idle_closes is not None \
            and idle_closes >= MAX_CLOSES_WITHOUT_USE:
        return f"no recorded use across {idle_closes} closes"
    return None


def retire_check(store: Mapping[str, Any], role: str,
                 name: str) -> str | None:
    """Retire reason for one skill through the real store path.

    Never-recorded skills count the whole store lifetime as idle, so
    the unused branch is reachable without hand-built entries. Pass
    one catalog skill name per call; iterating the catalog is the
    caller's job (no usage telemetry exists yet to do it here).
    """
    entry = store.get("skills", {}).get(f"{role}/{name}")
    if entry is None or not entry.get("order"):
        return retire_decision(None, int(store.get("closes", 0)))
    metrics = VersionMetrics(**entry["versions"][entry["order"][-1]])
    return retire_decision(metrics, closes_since_use(store, role, name))


def format_revert_question(proposal: RevertProposal) -> str:
    """Owner-channel question text for one revert proposal."""
    return (
        f"Skill {proposal.role}/{proposal.name} regressed "
        f"({proposal.reason}). Revert version {proposal.current_version} "
        f"to {proposal.previous_version}? Evidence: "
        f"{proposal.evidence_ticket}. "
        f"Reply approve to revert, deny with a reason to keep it.")


def version_of_skill(skill_dir: str | Path) -> str:
    """12-hex content digest of one skill dir: the version identity."""
    root = Path(skill_dir)
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*")
                   if path.is_file() and not path.is_symlink())
    for path in files:
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def _body(text: str) -> str:
    """SKILL.md body after the frontmatter (the header is gate-stamped)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    closing = next(
        (index for index, line in enumerate(lines[1:], start=1)
         if line.strip() == "---"),
        None)
    if closing is None:
        return text
    return "".join(lines[closing + 1:])


def is_full_rewrite(old_text: str, new_text: str) -> bool:
    """True when an edit shares almost nothing with the previous body.

    Compares bodies only: the gate restamps headers on every save, so
    header bytes never count as change.
    """
    old, new = _body(old_text), _body(new_text)
    if old == new:
        return False
    if not new.strip():
        return True
    if not old.strip():
        return False
    return SequenceMatcher(None, old, new).ratio() < REWRITE_MIN_SIMILARITY


def format_metrics_lines(store: Mapping[str, Any], role: str,
                         name: str) -> list[str]:
    """One visible line per recorded version, oldest first."""
    entry = store.get("skills", {}).get(f"{role}/{name}")
    if not entry:
        return []
    lines = []
    for version in entry.get("order", []):
        metrics = VersionMetrics(**entry["versions"][version])
        lines.append(
            f"{role}/{name}@{version}: {metrics.uses} uses, "
            f"{success_rate(metrics):.0%} verified "
            f"({metrics.verified_successes} ok, {metrics.blocked} blocked, "
            f"{metrics.not_applicable} n/a, {metrics.rework} rework; "
            f"last {metrics.last_ticket})")
    return lines


def metrics_summary(store: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-serializable per-version metrics for the API/dashboard."""
    out: dict[str, Any] = {}
    for key, entry in store.get("skills", {}).items():
        versions = []
        for version in entry.get("order", []):
            metrics = VersionMetrics(**entry["versions"][version])
            row = asdict(metrics)
            row["success_rate"] = success_rate(metrics)
            versions.append(row)
        out[key] = {"closes": store.get("closes", 0),
                    "closes_since_use": (
                        None if entry.get("last_use_close") is None
                        else int(store.get("closes", 0))
                        - int(entry["last_use_close"])),
                    "versions": versions}
    return out
