"""Mechanic verdict: a closed action vocabulary the core executes.

The mechanic investigates freely but changes nothing itself: it returns
actions from this list and the core applies them with validation. New
situations need no new code; only a new kind of tool would.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ActionKind(str, Enum):
    RESUME = "resume"
    DELEGATE = "delegate"
    WAIT = "wait"
    FAIL = "fail"
    ESCALATE = "escalate"
    COMMENT = "comment"
    FIX_TICKET = "fix_ticket"
    GIT_UNSTAGE = "git_unstage"
    GIT_STASH = "git_stash"
    REMOVE_INDEX_LOCK = "remove_index_lock"


STATE_ACTIONS = frozenset({ActionKind.RESUME, ActionKind.DELEGATE, ActionKind.WAIT,
                           ActionKind.FAIL, ActionKind.ESCALATE})
TASK_ACTIONS = STATE_ACTIONS | {ActionKind.COMMENT}
REPO_ACTIONS = frozenset({ActionKind.GIT_UNSTAGE, ActionKind.GIT_STASH,
                          ActionKind.REMOVE_INDEX_LOCK})
MAX_ACTIONS = 10
MIN_WAIT_MINUTES = 5
MAX_WAIT_MINUTES = 1440


class MechanicAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ActionKind
    task_id: str | None = None
    repo: str | None = None
    paths: list[str] = Field(default_factory=list)
    minutes: int | None = None
    title: str = ""
    text: str = ""


class MechanicVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    summary: str
    diagnosis: str
    actions: list[MechanicAction] = Field(default_factory=list)


@dataclass(frozen=True)
class VerdictContext:
    run_id: str
    task_ids: frozenset[str]
    waiting_ids: frozenset[str]
    repos: frozenset[str]


def parse_verdict(text: str) -> MechanicVerdict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"verdict is not JSON: {exc}") from exc
    try:
        return MechanicVerdict.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(bit) for bit in err['loc'])}: {err['msg']}"
            for err in exc.errors())
        raise ValueError(f"verdict schema: {problems}") from exc


def _safe_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    return (bool(path.strip()) and not candidate.is_absolute()
            and ".." not in candidate.parts and not path.startswith("-"))


def validate_verdict(verdict: MechanicVerdict, ctx: VerdictContext) -> list[str]:
    errors: list[str] = []
    if verdict.run_id != ctx.run_id:
        errors.append(f"run_id must be {ctx.run_id!r}")
    if not verdict.summary.strip() or len(verdict.summary) > 1000:
        errors.append("summary must be 1-1000 characters")
    if not verdict.diagnosis.strip() or len(verdict.diagnosis) > 6000:
        errors.append("diagnosis must be 1-6000 characters")
    if len(verdict.actions) > MAX_ACTIONS:
        errors.append(f"at most {MAX_ACTIONS} actions")
    state_targets: set[str] = set()
    for index, action in enumerate(verdict.actions):
        label = f"actions[{index}] {action.kind.value}"
        if action.kind in TASK_ACTIONS:
            if action.task_id not in ctx.task_ids:
                errors.append(f"{label}: task_id must be one of the findings")
            elif action.kind in STATE_ACTIONS:
                if action.task_id not in ctx.waiting_ids:
                    errors.append(f"{label}: only waiting tickets change state")
                if action.task_id in state_targets:
                    errors.append(f"{label}: one state action per ticket")
                state_targets.add(str(action.task_id))
        if action.kind in REPO_ACTIONS and action.repo not in ctx.repos:
            errors.append(f"{label}: repo must be one of the situation repos")
        if action.kind is ActionKind.GIT_UNSTAGE and (
                not action.paths or len(action.paths) > 20
                or not all(_safe_path(path) for path in action.paths)):
            errors.append(f"{label}: 1-20 relative paths inside the repo")
        if action.kind is ActionKind.WAIT and not (
                action.minutes is not None
                and MIN_WAIT_MINUTES <= action.minutes <= MAX_WAIT_MINUTES):
            errors.append(f"{label}: minutes must be "
                          f"{MIN_WAIT_MINUTES}-{MAX_WAIT_MINUTES}")
        if action.kind is ActionKind.FIX_TICKET:
            if not 10 <= len(action.title.strip()) <= 160:
                errors.append(f"{label}: title must be 10-160 characters")
            if len(action.text.strip()) < 40:
                errors.append(f"{label}: text needs diagnosis, evidence, "
                              "fix and acceptance")
        if not action.text.strip() or len(action.text) > 6000:
            errors.append(f"{label}: text must be 1-6000 characters")
    return errors
