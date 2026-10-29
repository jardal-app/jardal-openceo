"""Playbooks: objective-level crystallization (TASK-2026-00217).

A playbook is a recipe of tickets across agents — stages with agent
and skill, order, inputs/outputs, stop conditions — proposed by the
planner on objective close (or on Owner request) and saved to
``playbooks/<name>/`` in git only with Owner approval. The planner
receives approved playbooks in its prompt index, follows them by
default, explains deviations and proposes updates through the same
approval. Loading is fail-open (an invalid file is skipped with a
warning: the planner must never break on content); saving is
fail-closed (invalid payloads never touch git).

Proposing runs through :func:`propose_playbook` with an explicit
agents/skills map (the engine hook supplies them); saving through
:func:`save_approved_playbook` over a dedicated worktree on
origin/main, so the service checkout is never touched.
"""

from __future__ import annotations

import logging
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

__all__ = [
    "PLAYBOOK_FILENAME",
    "PLAYBOOK_PROPOSAL_FILE",
    "PLAYBOOKS_DIRNAME",
    "Playbook",
    "PlaybookApproval",
    "PlaybookError",
    "PlaybookProposal",
    "PlaybookStage",
    "PlaybookUpdate",
    "build_objective_brief",
    "load_playbooks",
    "parse_proposal",
    "playbook_source_id",
    "playbooks_index_text",
    "playbooks_root",
    "proposal_prompt",
    "propose_playbook",
    "save_approved_playbook",
    "update_source_id",
    "validate_proposal",
]

#: Playbook storage: ``playbooks/<name>/playbook.yaml`` at the repo root.
PLAYBOOKS_DIRNAME = "playbooks"
PLAYBOOK_FILENAME = "playbook.yaml"

#: Proposal output the planner run writes under the objective scratch.
PLAYBOOK_PROPOSAL_FILE = "PLAYBOOK_PROPOSAL.json"

#: Objective scratch prefix (mirror of engine.SCRATCH_DIRNAME: the root
#: is shared, the engines are not — TASK-2026-00141; renamed together
#: by TASK-2026-00194).
PLAYBOOK_SCRATCH_PREFIX = ".openceo/objectives"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class PlaybookError(ValueError):
    """The playbook payload or save cannot proceed (question stays open)."""


class PlaybookStage(BaseModel):
    """One playbook stage: who runs it and what crosses in and out."""

    order: int = Field(ge=1)
    agent: str = Field(min_length=1)
    skill: str | None = None
    goal: str = Field(min_length=1)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)

    @field_validator("agent", "goal")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class PlaybookApproval(BaseModel):
    """Approval reference stamped on save (never planner-written)."""

    question: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    at: str = Field(min_length=1)


class Playbook(BaseModel):
    """One approved playbook, versioned in git."""

    name: str = Field(pattern=_NAME_RE.pattern)
    version: int = Field(default=1, ge=1)
    summary: str = Field(min_length=1)
    when_to_use: str = Field(min_length=1)
    stages: list[PlaybookStage] = Field(min_length=1)
    stop: list[str] = Field(min_length=1)
    approved_by: PlaybookApproval | None = None
    update_notes: list[str] = Field(default_factory=list)

    @field_validator("stages")
    @classmethod
    def _orders_unique(cls, value: list[PlaybookStage]) -> list[PlaybookStage]:
        orders = [stage.order for stage in value]
        if len(set(orders)) != len(orders):
            raise ValueError("stage orders must be unique")
        return sorted(value, key=lambda stage: stage.order)


class PlaybookProposal(BaseModel):
    """Planner recurrence judgement plus the drafted playbook."""

    recurrent: bool
    reason: str = Field(min_length=1)
    playbook: Playbook | None = None

    @field_validator("playbook")
    @classmethod
    def _recurrent_has_playbook(
        cls, value: Playbook | None, info: Any,
    ) -> Playbook | None:
        recurrent = bool((info.data or {}).get("recurrent", False))
        if recurrent and value is None:
            raise ValueError("a recurrent objective needs its playbook")
        if not recurrent and value is not None:
            raise ValueError("a one-off objective carries no playbook")
        return value


class PlaybookUpdate(BaseModel):
    """Planner-proposed change to an approved playbook."""

    playbook: str = Field(pattern=_NAME_RE.pattern)
    change: str = Field(min_length=1)
    reason: str = Field(min_length=1)


def playbooks_root(repo: str | Path | None = None) -> Path:
    """Playbook storage root (the OpenCEO checkout unless told otherwise)."""
    base = Path(repo) if repo is not None else Path(__file__).resolve().parent.parent.parent
    return base / PLAYBOOKS_DIRNAME


def load_playbooks(repo: str | Path | None = None) -> dict[str, Playbook]:
    """Load every valid playbook, skipping invalid ones with a warning."""
    found: dict[str, Playbook] = {}
    root = playbooks_root(repo)
    if not root.is_dir():
        return found
    for entry in sorted(path for path in root.iterdir() if path.is_dir()):
        target = entry / PLAYBOOK_FILENAME
        if not target.is_file():
            continue
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8"))
            book = Playbook.model_validate(data)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.warning("playbook %s skipped (%s)", entry.name, exc)
            continue
        if book.name != entry.name:
            logger.warning("playbook %s skipped (name mismatch)", entry.name)
            continue
        found[book.name] = book
    return found


def playbooks_index_text(books: Mapping[str, Playbook]) -> str:
    """Prompt index: name + trigger + stages per playbook ("" when none)."""
    lines = []
    for name in sorted(books):
        book = books[name]
        stages = ", ".join(
            f"{stage.agent}/{stage.skill or '-'}"
            for stage in book.stages)
        lines.append(f"- {name}: {book.when_to_use} [{stages}]")
    if not lines:
        return ""
    return "PLAYBOOKS (approved recipes across agents; follow by " \
        "default, explain deviations, propose updates):\n" + "\n".join(lines)


def build_objective_brief(objective: Mapping[str, Any],
                           children: Any) -> str:
    """Deterministic recurrence brief: goal, stages, stop (no LLM)."""
    anchor = objective.get("anchor") or {}
    lines = [f"GOAL: {anchor.get('goal') or '(none)'}", "",
             "STAGES (completed children, close order unknown):"]
    rows = [item for item in (children or [])
            if str(item.get("status") or "") == "completed"]
    for item in rows:
        params = item.get("params")
        params = params if isinstance(params, Mapping) else {}
        agent = str(params.get("target_agent") or "").strip() or "?"
        skill = str(params.get("skill") or "").strip()
        who = f"{agent}/{skill}" if skill else agent
        verdict = params.get("agent_result")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        delivered = verdict.get("entregables")
        delivered = [str(entry) for entry in delivered
                     if str(entry or "").strip()] \
            if isinstance(delivered, list) else []
        lines.append(f"- {item.get('title') or item.get('task_id')}: "
                     f"{who}: {verdict.get('motivo') or ''}".rstrip())
        if delivered:
            lines.append(f"  outputs: {'; '.join(delivered)[:500]}")
    acceptance = anchor.get("acceptance") or []
    lines.extend(["", "STOP (objective acceptance):"])
    lines.extend(f"- {item}" for item in acceptance)
    return "\n".join(lines)


def proposal_prompt(objective_id: str, brief: str,
                    scratch_rel: str) -> str:
    """Recurrence-analysis prompt with its OUTPUT contract."""
    return "\n".join([
        "Judge whether this closed objective is a recurrent process "
        "worth a playbook (a recipe of tickets across agents). Judge by "
        "content only: never scan history, never invent other objectives.",
        "",
        brief.strip(),
        "",
        f"OUTPUT: write {scratch_rel} with exactly this JSON object "
        "(no YAML, no markdown fences around the file content):",
        "{",
        '  "recurrent": true | false,',
        '  "reason": "<why it repeats, or why it is one-off>",',
        '  "playbook": null | {',
        '    "name": "<unique kebab-case key>",',
        '    "summary": "<what the recipe achieves>",',
        '    "when_to_use": "<trigger to follow it>",',
        '    "stages": [ {"order": 1, "agent": "<enabled agent>",',
        '      "skill?": "<its skill>", "goal": "<stage goal>",',
        '      "inputs": ["<from the previous stage>"],',
        '      "outputs": ["<for the next stage>"]} ],',
        '    "stop": ["<when the recipe is done>"]',
        "  }",
        "}",
        "Rules: recurrent=true carries the playbook, recurrent=false "
        "carries null. Stages mirror the objective above (agent and skill "
        "must name real ones); order is execution order.",
    ])


def parse_proposal(text: str) -> PlaybookProposal:
    """Parse one proposal file. Raises PlaybookError, never raw errors."""
    import json
    try:
        data = json.loads(text or "")
    except ValueError as exc:
        raise PlaybookError(f"proposal is not JSON: {exc}") from exc
    try:
        return PlaybookProposal.model_validate(data)
    except ValidationError as exc:
        raise PlaybookError(
            f"proposal shape: {exc.errors()[0]['msg']}") from exc


def validate_proposal(proposal: PlaybookProposal,
                      agents: Mapping[str, Any] | None,
                      skills: Mapping[str, Any] | None) -> list[str]:
    """Deterministic validation: enabled agents, real skills. Errors."""
    book = proposal.playbook
    if book is None:
        return []
    errors: list[str] = []
    registry = agents or {}
    for stage in book.stages:
        entry = registry.get(stage.agent)
        enabled = bool(entry.get("enabled", False)) \
            if isinstance(entry, Mapping) else False
        if not enabled:
            errors.append(f"stage {stage.order}: agent {stage.agent!r} "
                          "is not enabled")
            continue
        if skills is None or not stage.skill:
            continue
        allowed = skills.get(stage.agent)
        if allowed is None:
            continue
        if stage.skill not in set(allowed):
            errors.append(f"stage {stage.order}: skill "
                          f"{stage.skill!r} is not a "
                          f"{stage.agent} skill")
    return errors


def playbook_source_id(objective_id: str) -> str:
    """Idempotency key: one proposal question per objective."""
    return f"playbook:{objective_id}"


def update_source_id(playbook: str, objective_id: str) -> str:
    """Idempotency key: one update question per playbook per cycle."""
    return f"playbook-update:{playbook}:{objective_id}"


async def propose_playbook(env: Any, objective_id: str, trigger: str, *,
                           agents: Mapping[str, Any] | None = None,
                           skills: Mapping[str, Any] | None = None,
                           ) -> dict[str, Any]:
    """Propose a playbook for a closed objective (close or Owner ask).

    Runs the planner once over the deterministic brief; a recurrent
    judgement opens one owner question (kind ``playbook``) with the
    drafted recipe. Never raises: every outcome is an ``action`` dict
    and a failed proposal never blocks anything.
    """
    from owner_channel import build_owner_question, question_answered

    objective = await env.objectives.get_objective(objective_id)
    if objective is None:
        return {"ok": False, "action": "no-objective"}
    if str(objective.get("status") or "") != "closed":
        return {"ok": False, "action": "not_closed"}
    children = await env.tasks.list_by_objective(objective_id)
    brief = build_objective_brief(objective, children)
    repo = Path(env.repo)
    rel = (f"{PLAYBOOK_SCRATCH_PREFIX}/{objective_id}/"
           f"{PLAYBOOK_PROPOSAL_FILE}")
    target = repo / rel
    prompt = proposal_prompt(objective_id, brief, rel)
    timeout_s = int((env.planner_cfg or {}).get("timeout_minutes", 15)
                    ) * 60
    try:
        try:
            target.unlink()
        except OSError:
            pass
        await env.executor.run("objective_planner", prompt, repo,
                               timeout_s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("playbook proposal run failed for %s: %s",
                       objective_id, exc)
        return {"ok": False, "action": "proposal_error",
                "detail": f"{type(exc).__name__}"}
    finally:
        try:
            await env.executor.cleanup("objective_planner")
        except Exception:  # noqa: BLE001, S110
            pass
    try:
        text = target.read_text(encoding="utf-8") \
            if target.is_file() else ""
    except OSError:
        text = ""
    if not text.strip():
        return {"ok": False, "action": "proposal_invalid",
                "detail": f"no {PLAYBOOK_PROPOSAL_FILE}"}
    try:
        proposal = parse_proposal(text)
    except PlaybookError as exc:
        return {"ok": False, "action": "proposal_invalid",
                "detail": str(exc)}
    if not proposal.recurrent:
        return {"ok": True, "action": "not_recurrent"}
    errors = validate_proposal(proposal, agents, skills)
    if errors:
        return {"ok": False, "action": "proposal_invalid",
                "detail": "; ".join(errors[:3])}
    assert proposal.playbook is not None
    draft = proposal.playbook.model_dump(mode="json")
    asked = "the Owner asked for it" if trigger == "owner_request" \
        else "recurrent by content"
    spec = build_owner_question(
        kind="playbook",
        question=f"Approve playbook {draft['name']}?",
        human_action="Approve the playbook recipe or reject it",
        context=(f"Objective {objective_id} closed ({asked}): "
                 f"{proposal.reason}. Stages: "
                 + ", ".join(f"{item['agent']}/{item.get('skill') or '-'}"
                             for item in draft["stages"])),
        playbook=draft,
        project=str(objective.get("project") or "") or None,
        source_id=playbook_source_id(objective_id),
    )
    created = await env.create_ticket({
        "title": spec["title"],
        "description": spec["description"],
        "project": spec["project"],
        "source": spec["source"],
        "source_id": spec["source_id"],
        "params": spec["params"],
        "priority": spec["priority"],
        "tags": spec["tags"],
        "autorun": False,
    })
    if not created or question_answered(created):
        return {"ok": True, "action": "already_proposed"}
    question_id = str(created.get("task_id") or "")
    if not question_id:
        return {"ok": False, "action": "proposal_error",
                "detail": "question without task_id"}
    await env.patch_task(question_id, {
        "status": "waiting",
        "phase": spec["phase"],
        "waiting_reason": spec["waiting_reason"],
        "workflow_key": None,
        "workflow_phases": [],
        "phase_index": 0,
        "phase_assignments": {},
    })
    return {"ok": True, "action": "proposed",
            "question_task_id": question_id,
            "playbook": draft["name"]}


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Exclusive file lock (mirror of the crystallization save step)."""
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        timeout=120)
    if completed.returncode != 0:
        raise PlaybookError(
            f"git {' '.join(args)} failed: "
            f"{completed.stderr.strip()[:300]}")
    return completed.stdout.strip()


def save_approved_playbook(repo: str | Path, payload: Mapping[str, Any], *,
                           approval: Mapping[str, Any],
                           update: bool = False) -> dict[str, Any]:
    """Save an Owner-approved playbook to git (worktree on origin/main).

    Validates before touching git; new playbooks enter at version 1,
    updates require the playbook and bump the version. Raises
    PlaybookError (the question stays open for a retry).
    """
    root = Path(repo)
    try:
        draft = Playbook.model_validate(dict(payload))
        seal = PlaybookApproval.model_validate(dict(approval))
    except ValidationError as exc:
        raise PlaybookError(
            f"playbook shape: {exc.errors()[0]['msg']}") from exc
    rel = f"{PLAYBOOKS_DIRNAME}/{draft.name}/{PLAYBOOK_FILENAME}"
    with _locked(root / ".runtime" / "playbooks.lock"):
        _git(root, "fetch", "origin", "main")
        worktree = root / ".runtime" / f"playbook-{draft.name}"
        try:
            _git(root, "worktree", "remove", "--force", str(worktree))
        except PlaybookError:
            pass
        try:
            _git(root, "worktree", "add", "--detach", str(worktree),
                 "origin/main")
            target = worktree / rel
            if update:
                if not target.is_file():
                    raise PlaybookError(
                        f"playbook {draft.name!r} does not exist")
                current = Playbook.model_validate(yaml.safe_load(
                    target.read_text(encoding="utf-8")))
                draft.version = current.version + 1
            elif target.is_file():
                raise PlaybookError(
                    f"playbook {draft.name!r} already exists")
            draft.approved_by = seal
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(yaml.safe_dump(
                draft.model_dump(mode="json"), sort_keys=False),
                encoding="utf-8")
            _git(worktree, "add", rel)
            if not _git(worktree, "status", "--porcelain"):
                raise PlaybookError("playbook save produced no changes")
            verb = "Update" if update else "Add"
            _git(worktree, "commit", "-m",
                 f"{verb} playbook {draft.name} "
                 f"(approved {seal.question})")
            try:
                _git(worktree, "push", "origin", "HEAD:main")
            except PlaybookError:
                _git(worktree, "pull", "--rebase", "origin", "main")
                _git(worktree, "push", "origin", "HEAD:main")
        finally:
            try:
                _git(root, "worktree", "remove", "--force", str(worktree))
            except PlaybookError:
                pass
    return {"ok": True, "name": draft.name, "version": draft.version,
            "path": f"{PLAYBOOKS_DIRNAME}/{draft.name}"}
