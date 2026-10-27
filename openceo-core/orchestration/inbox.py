"""Findings inbox (design §8, F4).

Candidates that are not tickets yet: pre-existing findings from any review
round, ``muse`` out-of-scope discards, and CRITICALs left ``disputed``
without arbitration. Deduplicated by fingerprint (file + category +
normalized quote), promoted to tickets by a deterministic rule, cleaned up
automatically when the quoted code disappears. Never a Kanban, never in ERP.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


#: Entry lifecycle (§15.4).
INBOX_STATUSES = ("open", "assigned", "promoted", "resolved", "stale")

#: Promotion rule defaults (design §17.1 ``inbox.promotion``).
PROMOTION_DEFAULTS: dict[str, Any] = {
    "severity": "CRITICAL",
    "anchor": "strong",
    "min_families": 2,
    "or_category": "security",
}

#: Fixed fix-ticket tiers (design §3.3, §17.1 ``fix_ticket_tiers``).
FIX_TICKET_TIERS_DEFAULTS: dict[str, str] = {
    "security": "tier_s",
    "critical": "tier_a",
    "default": "tier_c",
}


def normalize_quote(quote: str) -> str:
    """Collapse whitespace: repetition matching ignores formatting drift."""
    return " ".join((quote or "").split())


def fingerprint(file: str, category: str, quote: str) -> str:
    """``hash(file, category, normalized quote)`` (§8.2).

    Independent of words and line numbers, which shift every commit; two
    reviewers flagging the same bug quote the same code.
    """
    material = "\0".join([file.strip(), category.strip().lower(),
                          normalize_quote(quote)])
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pickaxe_shas(repo: Path, file: str, line: str) -> list[str] | None:
    """Commit SHAs that changed the occurrences of a line (git pickaxe).

    None when git cannot answer (no repo, timeout, error): attribution
    then stays unknown instead of guessing.
    """
    try:
        proc = subprocess.run(
            ["git", "log", "--format=%H", "-S", line, "--", file],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [part for part in proc.stdout.split() if part]


def attribute_finding(
    repo: Path,
    file: str,
    quote: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attribute a finding to originating tickets (policy §31).

    Deterministic Git backbone: the pickaxe (``git log -S`` over the
    quote's longest significant line) lists the commits that introduced
    the quoted code, intersected with the known ticket SHAs from the
    objective ``tickets_log``. Each candidate carries ``sha``,
    ``task_id`` and, when known, ``objective_id``, ``coding_tool``,
    ``tier_reason``, ``oracle_type`` and ``oracle_refs``.

    Returns the §31 record or ``{"status": "unknown", "reason": ...}``
    when git cannot resolve it: without SHA → ticket attribution the
    late-failure rate stays unknown, never invented.
    """
    unknown = lambda reason: {"status": "unknown", "reason": reason}
    if not str(file or "").strip() or not str(quote or "").strip():
        return unknown("empty-finding")
    candidates_lines = [part.strip() for part in str(quote).splitlines()
                        if part.strip()]
    line = max(candidates_lines, key=len) if candidates_lines else ""
    if len(line) < 8:
        return unknown("quote-too-short")
    shas = _pickaxe_shas(repo, str(file).strip(), line)
    if shas is None:
        return unknown("git-unavailable")
    if not shas:
        return unknown("no-matching-commit")
    known = [dict(item) for item in candidates
             if isinstance(item, Mapping) and item.get("sha")
             and item.get("task_id")]
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sha in shas:
        for item in known:
            cand = str(item.get("sha") or "")
            if not cand or str(item.get("task_id")) in seen:
                continue
            if sha == cand or sha.startswith(cand) or cand.startswith(sha):
                seen.add(str(item.get("task_id")))
                matched.append({"sha": sha, "ticket": item})
    if not matched:
        return unknown("no-known-ticket")
    first = matched[0]["ticket"]
    oracle_refs = [str(ref) for ref in first.get("oracle_refs") or []]
    return {
        "status": "attributed",
        "shas": [item["sha"] for item in matched],
        "ticket_ids": [str(item["ticket"].get("task_id"))
                       for item in matched],
        "objective_id": str(first.get("objective_id") or ""),
        "coding_tool": str(first.get("coding_tool") or ""),
        "tier_reason": str(first.get("tier_reason") or ""),
        "oracle_type": str(first.get("oracle_type") or "") or None,
        "oracle_refs": oracle_refs,
    }


class InboxStore(Protocol):
    """Persistence for ``findings_inbox`` (§15.4)."""

    async def get_entry(self, fp: str) -> dict[str, Any] | None: ...
    async def upsert_entry(self, fp: str, entry: dict[str, Any]) -> None: ...
    async def list_open_for_files(
        self, files: Sequence[str], limit: int) -> list[dict[str, Any]]: ...


class InMemoryInboxStore:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}

    async def get_entry(self, fp: str) -> dict[str, Any] | None:
        entry = self.entries.get(fp)
        return dict(entry) if entry is not None else None

    async def upsert_entry(self, fp: str, entry: dict[str, Any]) -> None:
        self.entries[fp] = dict(entry)

    async def list_open_for_files(
        self, files: Sequence[str], limit: int) -> list[dict[str, Any]]:
        wanted = set(files)
        rows = [entry for entry in self.entries.values()
                if entry.get("status") == "open" and entry.get("file") in wanted]
        rows.sort(key=lambda entry: (
            0 if entry.get("severity_max") == "CRITICAL" else 1,
            -len(entry.get("families") or [])))
        return [dict(entry) for entry in rows[:limit]]


class MongoInboxStore:
    """Production inbox on ``findings_inbox`` (§15.4)."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_entry(self, fp: str) -> dict[str, Any] | None:
        return await self._repo.get_findings_entry(fp)

    async def upsert_entry(self, fp: str, entry: dict[str, Any]) -> None:
        await self._repo.upsert_findings_entry(fp, dict(entry))

    async def list_open_for_files(
        self, files: Sequence[str], limit: int) -> list[dict[str, Any]]:
        return await self._repo.list_open_findings_for_files(list(files), limit)


@dataclass(frozen=True)
class Appearance:
    """One sighting of a candidate finding."""

    file: str
    category: str
    severity: str  # CRITICAL | WARN
    quote: str
    strength: str  # strong | weak
    source: str  # pre_existing | out_of_scope | disputed
    family: str
    task_id: str
    run_id: str
    round_no: int
    anchor: str = ""


async def record_appearance(
    store: InboxStore,
    project: str,
    appearance: Appearance,
) -> str:
    """Record a sighting; every new one adds family + ticket (§8.2)."""
    fp = fingerprint(appearance.file, appearance.category, appearance.quote)
    entry = await store.get_entry(fp)
    seen = {"task_id": appearance.task_id, "run_id": appearance.run_id,
            "round": appearance.round_no, "at": _utcnow_iso()}
    if entry is None:
        await store.upsert_entry(fp, {
            "_id": fp,
            "project": project,
            "file": appearance.file,
            "category": appearance.category.lower(),
            "severity_max": appearance.severity,
            "quote": normalize_quote(appearance.quote),
            "anchor": appearance.anchor,
            "strength": appearance.strength,
            "source": appearance.source,
            "families": [appearance.family] if appearance.family else [],
            "seen_in": [seen],
            "status": "open",
            "assigned_to": None,
            "promoted_to": None,
        })
        return fp
    families = list(entry.get("families") or [])
    if appearance.family and appearance.family not in families:
        families.append(appearance.family)
    entry["families"] = families
    entry["seen_in"] = [*(entry.get("seen_in") or []), seen]
    if appearance.severity == "CRITICAL":
        entry["severity_max"] = "CRITICAL"
    if appearance.source == "disputed" and entry.get("source") != "disputed":
        entry["source"] = "disputed"
    await store.upsert_entry(fp, entry)
    return fp


def promotion_ready(
    entry: Mapping[str, Any],
    rule: Mapping[str, Any] | None = None,
) -> bool:
    """Deterministic promotion rule (§8.3).

    CRITICAL + verified strong anchor + ≥2 accumulated families, or category
    ``security``. Only ``open`` entries; ``assigned`` ones left the pool when
    the objective planner claimed them (§8.4).
    """
    rule = rule or PROMOTION_DEFAULTS
    if entry.get("status") != "open":
        return False
    category = str(entry.get("category") or "").lower()
    if category == str(rule.get("or_category") or "security"):
        return True
    if str(entry.get("severity_max")) != str(rule.get("severity", "CRITICAL")):
        return False
    if str(entry.get("strength")) != str(rule.get("anchor", "strong")):
        return False
    try:
        need = int(rule.get("min_families", 2))
    except (TypeError, ValueError):
        need = 2
    return len(entry.get("families") or []) >= need


async def mark_assigned(
    store: InboxStore, fp: str, task_id: str) -> None:
    """An objective ticket claimed this entry: out of promotion (§8.4)."""
    entry = await store.get_entry(fp)
    if entry is None:
        return
    entry["status"] = "assigned"
    entry["assigned_to"] = task_id
    await store.upsert_entry(fp, entry)


async def mark_promoted(
    store: InboxStore, fp: str, task_id: str,
    attribution: Mapping[str, Any] | None = None,
) -> None:
    """The same finding is never promoted twice (§8.3).

    A successful §31 attribution travels on the entry for the Tier C
    late-failure telemetry; unknown origins record nothing.
    """
    entry = await store.get_entry(fp)
    if entry is None:
        return
    entry["status"] = "promoted"
    entry["promoted_to"] = task_id
    if isinstance(attribution, Mapping) \
            and attribution.get("status") == "attributed":
        entry["attribution"] = dict(attribution)
    await store.upsert_entry(fp, entry)


async def mark_resolved(store: InboxStore, fp: str, stale: bool = False) -> None:
    """The quoted code is gone from the base HEAD: done (§8.2)."""
    entry = await store.get_entry(fp)
    if entry is None:
        return
    entry["status"] = "stale" if stale else "resolved"
    await store.upsert_entry(fp, entry)


def promotion_ticket(
    entry: Mapping[str, Any],
    fix_tiers: Mapping[str, str] | None = None,
    attribution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the deterministic fix-ticket payload (§8.3).

    The caller creates it through the internal ticket API (Jardal ERP first);
    the fingerprint travels on the ticket so re-sightings converge. A
    successful §31 attribution travels on ``params`` plus a description
    block; unknown origins leave the payload unchanged.
    """
    tiers = dict(FIX_TICKET_TIERS_DEFAULTS)
    tiers.update(fix_tiers or {})
    category = str(entry.get("category") or "").lower()
    severity = str(entry.get("severity_max") or "WARN")
    if category == "security":
        tier = tiers["security"]
    elif severity == "CRITICAL":
        tier = tiers["critical"]
    else:
        tier = tiers["default"]
    families = ", ".join(entry.get("families") or [])
    seen = entry.get("seen_in") or []
    first_seen = seen[0].get("task_id") if seen else "?"
    quote = str(entry.get("quote") or "")
    anchor = str(entry.get("anchor") or entry.get("file") or "")
    priority = "high" if (severity == "CRITICAL" or category == "security") else "medium"
    payload: dict[str, Any] = {
        "priority": priority,
        "title": f"[{severity}] {category} en {entry.get('file')}: "
                 f"{quote[:80]}".strip(),
        "description": (
            f"Hallazgo promovido desde la bandeja (§8.3).\n"
            f"Ubicación: {anchor}\n"
            f"Cita: {quote[:500]}\n"
            f"Familias: {families}\n"
            f"Visto en: {first_seen} (+{max(0, len(seen) - 1)} apariciones)\n"
            f"Huella: {entry.get('_id')}"
        ),
        "acceptance": [
            "reproducir el bug; si no se reproduce, cerrar como no válido",
        ],
        "coding_tool": tier,
        "origin": "inbox_promotion",
        "finding_fingerprint": entry.get("_id"),
    }
    if isinstance(attribution, Mapping) \
            and attribution.get("status") == "attributed":
        payload["params"] = {"attribution": dict(attribution)}
        tickets = ", ".join(str(item) for item in
                            attribution.get("ticket_ids") or [])
        shas = ", ".join(str(item)[:12] for item in
                         attribution.get("shas") or [])
        payload["description"] += (
            f"\nAtribución (§31): {tickets} "
            f"({attribution.get('coding_tool') or '?'}, oráculo "
            f"{attribution.get('oracle_type') or 'ninguno'})"
            f"\nSHA(s): {shas}"
            f"\nObjetivo: {attribution.get('objective_id') or '?'}"
        )
    return payload


def _head_lines(repo: Path, ref: str, file: str) -> list[str] | None:
    """File lines at ``ref``; None when the path does not exist there."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{ref}:{file}"], cwd=repo,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", errors="replace").splitlines()


async def cleanup_run_entries(
    store: InboxStore,
    repo: Path,
    ref: str,
    fps: Sequence[str],
) -> dict[str, str]:
    """Auto-cleanup after close (§8.2): quote gone → resolved/stale.

    ``ref`` is the branch the ticket merged into (or its own branch when
    kept). Returns ``{fp: new_status}`` for entries that changed; read-only
    on git, no model.
    """
    changed: dict[str, str] = {}
    for fp in fps:
        entry = await store.get_entry(fp)
        if entry is None or entry.get("status") not in {"open", "assigned"}:
            continue
        lines = _head_lines(repo, ref, str(entry.get("file") or ""))
        quote = normalize_quote(str(entry.get("quote") or ""))
        if lines is None:
            await mark_resolved(store, fp, stale=True)
            changed[fp] = "stale"
        elif quote and not any(quote in " ".join(line.split()) for line in lines):
            await mark_resolved(store, fp)
            changed[fp] = "resolved"
    return changed


async def inbox_md_for_planner(
    store: InboxStore,
    files: Sequence[str],
    limit: int = 15,
) -> str:
    """``INBOX.md``: open entries touching objective files, worst first (§8.4)."""
    rows = await store.list_open_for_files(files, limit)
    if not rows:
        return "INBOX\nNo open entries touch this objective's files.\n"
    lines = ["INBOX"]
    for entry in rows:
        families = ", ".join(entry.get("families") or [])
        repeats = len(entry.get("seen_in") or [])
        lines.append(
            f"- [{entry.get('severity_max')}] [{entry.get('category')}] "
            f"{entry.get('file')} — {entry.get('quote', '')[:160]} "
            f"(familias: {families}; repeticiones: {repeats}; "
            f"huella: {entry.get('_id')})")
    return "\n".join(lines) + "\n"
