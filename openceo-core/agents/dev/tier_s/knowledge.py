"""Project knowledge: AGENTS.md checks and vault notes (design §5.1, §9.8, F7).

F7a (parallel, documentation): every CLI reads the repo ``AGENTS.md`` at its
root instead of pasted prompts; Tier S never touches ``CONTEXT.md``. This
module checks presence/size (telemetry, never blocking) and owns the prompt
preamble every role prompt starts with.

F7b (after F6): on objective close — or a single ticket whose plan carries
``[durable: yes]`` decisions — ``muse`` writes one vault note through the
``obsidian-vault-links`` skill boundaries, and the orchestrator validates it
with scoped ``vault_check.py --project … check <note>``. A check failure is
an ERP comment, never a failed close.

Deliberate deviation from §9.8: the global ``qmd update`` is NOT run. It
re-indexes every collection with no scope flag, which project sessions must
not trigger; the existing cron maintains the index.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


#: Every Tier S role prompt starts with this (F7a): instructions live in the
#: repo, not in the prompt.
AGENTS_PREAMBLE = (
    "Read AGENTS.md at the repo root first and follow it exactly; "
    "project conventions there override generic habits."
)


def check_agents_md(repo: Path, budget_tokens: int = 3000) -> dict[str, Any]:
    """Presence + size check (telemetry only, §13: ~3k tokens warn)."""
    path = repo / "AGENTS.md"
    if not path.is_file():
        return {"present": False, "tokens": 0, "over_budget": False,
                "warning": "missing repo-root AGENTS.md (F7a)"}
    try:
        chars = len(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {"present": False, "tokens": 0, "over_budget": False,
                "warning": "unreadable repo-root AGENTS.md"}
    tokens = chars // 4
    return {"present": True, "tokens": tokens,
            "over_budget": tokens > budget_tokens,
            "warning": (f"AGENTS.md ~{tokens} tokens over ~{budget_tokens}"
                        if tokens > budget_tokens else "")}


def _vault_paths(vault: Mapping[str, str]) -> tuple[Path, str]:
    root = Path(str(vault.get("dir") or "")).expanduser()
    project = str(vault.get("project") or "")
    return root, project


def _checker(vault: Mapping[str, str]) -> Path | None:
    root, _project = _vault_paths(vault)
    candidate = (root.parent / ".agents" / "skills" / "obsidian-vault-links"
                 / "scripts" / "vault_check.py")
    return candidate if candidate.is_file() else None


def _slug(text: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-"
                      for char in text)[:48]
    return "-".join(part for part in cleaned.split("-") if part) or "note"


def objective_note_relpath(objective_id: str) -> str:
    """Deterministic note path: one research note per objective (§9.8)."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"research/{day}-objective-{_slug(objective_id)}.md"


def ticket_note_relpath(ref: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"research/{day}-{_slug(ref)}.md"


def _note_prompt(
    note_abs: Path,
    project: str,
    subject: str,
    evidence: str,
) -> str:
    return (
        f"{AGENTS_PREAMBLE}\n"
        "Write ONE vault note with the obsidian-vault-links skill rules "
        f"(boundaries, frontmatter, local links): {note_abs}\n"
        f"Project: {project}. Subject: {subject}\n"
        "Content: durable facts, decisions, flows and conclusions only — no "
        "task plans, no transcripts, no secrets, no personal data. Cite "
        "ticket refs as evidence. Frontmatter: type research, org/project "
        "from the project path, today's date.\n"
        f"Evidence:\n{evidence[:6000]}\n"
        "Then emit VAULT_DONE in .tier-s/status."
    )


async def _run_check(
    checker: Path, project: str, relpath: str,
) -> tuple[bool, str]:
    """Scoped ``vault_check.py --project … check <note>`` (never global)."""
    try:
        proc = await _to_thread_subprocess(
            [sys.executable, str(checker), "--project", project, "check",
             relpath])
    except OSError as exc:
        return False, str(exc)[:300]
    ok = proc.returncode == 0
    tail = (proc.stdout + proc.stderr)[-1500:]
    return ok, tail


async def _to_thread_subprocess(argv: list[str]) -> Any:
    import asyncio

    def _run() -> Any:
        return subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace", timeout=180)

    return await asyncio.to_thread(_run)


async def write_objective_note(env: Any, objective: Mapping[str, Any]) -> str:
    """``muse`` writes the objective close note; orchestrator checks it.

    Returns the vault-relative note path. Raises on write/check failure
    (the caller comments ERP and lets the close stand).
    """
    vault = dict(getattr(env, "vault", None) or {})
    root, project = _vault_paths(vault)
    if not root.is_dir() or not project:
        raise RuntimeError("vault not configured (dev.vault_project)")
    checker = _checker(vault)
    if checker is None:
        raise RuntimeError("vault_check.py not found beside the vault")
    relpath = objective_note_relpath(str(objective.get("objective_id")))
    note = root / project / relpath
    if note.exists():
        return relpath  # idempotent: never rewrite a close note
    anchor = objective.get("anchor") or {}
    log = objective.get("tickets_log") or []
    evidence = "\n".join([
        f"Objective {objective.get('objective_id')}: {anchor.get('goal', '')}",
        "Tickets:",
        *[f"- {entry.get('task_id')}: {entry.get('title')} "
          f"[{entry.get('tier')}]" for entry in log],
        "Capsule:",
        (objective.get("capsule") or {}).get("text", ""),
    ])
    timeout_s = int((getattr(env, "timeouts", None) or {}).get("closer", 15)) * 60
    await env.executor.run(
        "closer",
        _note_prompt(note, project,
                     f"objective {objective.get('objective_id')} close",
                     evidence),
        env.repo, timeout_s)
    try:
        await env.executor.cleanup("closer")
    except Exception:
        pass
    if not note.is_file():
        raise RuntimeError(f"closer wrote no note at {relpath}")
    ok, tail = await _run_check(checker, project, relpath)
    if not ok:
        raise RuntimeError(f"vault check failed for {relpath}: {tail[:500]}")
    return relpath


async def write_ticket_note(
    env: Any,
    ticket: Mapping[str, Any],
    durable_decisions: list[str],
    handoff_excerpt: str,
) -> str:
    """Single-ticket note for plans with ``[durable: yes]`` (§9.8)."""
    vault = dict(getattr(env, "vault", None) or {})
    root, project = _vault_paths(vault)
    if not root.is_dir() or not project:
        raise RuntimeError("vault not configured (dev.vault_project)")
    checker = _checker(vault)
    if checker is None:
        raise RuntimeError("vault_check.py not found beside the vault")
    ref = str(ticket.get("ref") or ticket.get("ticket_number")
              or ticket.get("task_id", "")[:8])
    relpath = ticket_note_relpath(ref)
    note = root / project / relpath
    if note.exists():
        return relpath
    evidence = "\n".join([
        f"Ticket {ref}: {ticket.get('title', '')}",
        "Durable decisions:",
        *[f"- {line}" for line in durable_decisions],
        "Handoff:",
        handoff_excerpt[:3000],
    ])
    timeout_s = int((getattr(env, "timeouts", None) or {}).get("closer", 15)) * 60
    await env.executor.run(
        "closer", _note_prompt(note, project, f"ticket {ref}", evidence),
        env.repo, timeout_s)
    try:
        await env.executor.cleanup("closer")
    except Exception:
        pass
    if not note.is_file():
        raise RuntimeError(f"closer wrote no note at {relpath}")
    ok, tail = await _run_check(checker, project, relpath)
    if not ok:
        raise RuntimeError(f"vault check failed for {relpath}: {tail[:500]}")
    return relpath


def durable_plan_decisions(plan_text: str) -> list[str]:
    """PLAN.md DECISIONS flagged ``[durable: yes]`` (§9.8, §16.1)."""
    section = re.compile(r"^[A-Z][A-Z_]*\s*:")
    decisions: list[str] = []
    in_section = False
    for raw in (plan_text or "").splitlines():
        line = raw.strip()
        if line.startswith("DECISIONS:"):
            in_section = True
            continue
        if in_section and section.match(line):
            in_section = False
            continue
        if in_section and line.startswith("- ") and "[durable: yes]" in line:
            decisions.append(line[2:].strip())
    return decisions
