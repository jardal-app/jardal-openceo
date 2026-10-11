"""Single global Objective Planner contract (autonomy epic TASK-2026-00110).

The planner (Muse Code headless, one fresh run per cycle) returns
:class:`PlannerResult` JSON; Pydantic validates the shape and
:func:`validate_planner_result` applies the deterministic rules from
``openceo-core/objectives/routing_policy.md`` §17. Invalid output never
persists. The machine contract is JSON only: there is no YAML mirror.

Not to be confused with the Tier S ticket planner (Claude/Codex plan for
one ticket): this module decides *which tickets exist* for an objective.
It moves out of Tier S with the objective engine (TASK-2026-00117).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Collection, Literal, Mapping, Sequence

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

__all__ = [
    "DevTier",
    "InformationGap",
    "ObjectiveStatus",
    "Oracle",
    "OracleRef",
    "OracleType",
    "PlannedTicket",
    "PlannerResult",
    "find_local_cycles",
    "load_policy_text",
    "parse_planner_result",
    "validate_planner_result",
]

from enum import Enum


class ObjectiveStatus(str, Enum):
    ACTIVE = "active"
    COMPLETE_CANDIDATE = "complete_candidate"
    BLOCKED = "blocked"
    CAPABILITY_GAP = "capability_gap"


class DevTier(str, Enum):
    TIER_S = "tier_s"
    TIER_A = "tier_a"
    TIER_B = "tier_b"
    TIER_C = "tier_c"


class OracleType(str, Enum):
    PREEXISTING_TESTS = "preexisting_tests"
    CONTRACT_OR_SCHEMA = "contract_or_schema"
    BUG_REPRODUCTION = "bug_reproduction"
    SEPARATE_TICKET_TESTS = "separate_ticket_tests"
    PLANNER_EXECUTABLE_SPEC = "planner_executable_spec"
    BUILDER_TESTS = "builder_tests"


def _string_list_nonblank(value: list[str]) -> list[str]:
    for item in value:
        if not str(item).strip():
            raise ValueError("must not contain blank items")
    return value


#: ``roadmap_remaining`` is the planner's outlook (policy §12): one short note
#: per foreseen step. The whole list is pasted into every later planning prompt
#: and into the Objective capsule, so its size is bounded (TASK-2026-00183).
#: The prompt and the policy quote these numbers; a test keeps them aligned.
ROADMAP_MAX_LINES = 10
ROADMAP_MAX_CHARS = 300


def _roadmap_within_bounds(value: list[str]) -> list[str]:
    if len(value) > ROADMAP_MAX_LINES:
        raise ValueError(
            f"roadmap_remaining carries at most {ROADMAP_MAX_LINES} lines, "
            f"got {len(value)}: keep the next steps that matter, one line each")
    for index, item in enumerate(value, start=1):
        size = len(str(item).strip())
        if size > ROADMAP_MAX_CHARS:
            raise ValueError(
                f"roadmap_remaining line {index} has {size} characters "
                f"(max {ROADMAP_MAX_CHARS}): one short line per step, naming "
                "what it depends on")
    return value


class InformationGap(BaseModel):
    gap: str = Field(min_length=1)
    materiality: Literal["low", "medium", "high", "critical"]
    resolution: str = Field(min_length=1)

    @field_validator("gap", "resolution")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class OracleRef(BaseModel):
    """Semantic reference emitted by the planner.

    Before execution, the orchestrator resolves file/artifact references to
    their canonical IDs/paths and freezes hashes for qualifying oracle
    inputs (any simple tier, policy §24).
    """

    ref: str = Field(min_length=1)
    description: str | None = None

    @field_validator("ref")
    @classmethod
    def _ref_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class Oracle(BaseModel):
    type: OracleType
    refs: list[OracleRef] = Field(min_length=1)


class PlannedTicket(BaseModel):
    local_ref: str = Field(min_length=1)
    title: str = Field(min_length=1)
    target_agent: str = Field(min_length=1)
    work_type: str = Field(min_length=1)

    goal: str = Field(min_length=1)
    why_now: str = Field(min_length=1)

    inputs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)

    acceptance_criteria: list[str] = Field(min_length=1)
    expected_artifacts: list[str] = Field(min_length=1)
    blocked_by: list[str] = Field(default_factory=list)

    coding_tool: DevTier | None = None
    tier_reason: str | None = None
    oracle: Oracle | None = None

    # Suggested skill for any agent (TASK-2026-00198): a hint, the agent
    # decides. Checked against the real role skills by TASK-2026-00203.
    skill: str | None = None

    # Required only when coding_tool == tier_s.
    why_consensus_is_needed: str | None = None
    why_single_session_is_insufficient: str | None = None

    # Required when coding_tool is tier_a or tier_b (policy §§21/26):
    # what breaks if the default tier executes this ticket. Accepted
    # (not required) for tier_s, whose pair already answers it (§20);
    # rejected for tier_c (the default needs no such statement, so one
    # means the planner meant A/B) and for non-dev.
    why_not_c: str | None = None

    # Tier S v2 §8.4 carry-over (not in the §B schema): pre-existing inbox
    # findings this ticket claims. The orchestrator marks them assigned.
    includes_findings: list[str] = Field(default_factory=list)

    # Adoption (TASK-2026-00174): complete the contract of an existing
    # tierless linked child instead of creating a ticket. The entry
    # carries the full contract (tier when dev, acceptance, artefacts);
    # title/goal/why_now ride along unused and blocked_by/includes are
    # rejected (completions drain backlog, they take no deps and claim
    # no findings).
    completes_task_id: str | None = None

    @field_validator("local_ref", "title", "target_agent", "work_type",
                     "goal", "why_now")
    @classmethod
    def _fields_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("tier_reason", "why_consensus_is_needed",
                     "why_single_session_is_insufficient", "why_not_c",
                     "completes_task_id", "skill")
    @classmethod
    def _optional_fields_nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("inputs", "constraints", "out_of_scope",
                     "acceptance_criteria", "expected_artifacts",
                     "blocked_by", "includes_findings")
    @classmethod
    def _lists_nonblank(cls, value: list[str]) -> list[str]:
        return _string_list_nonblank(value)

    @model_validator(mode="after")
    def validate_routing_contract(self) -> "PlannedTicket":
        is_dev = self.target_agent == "dev"

        if not is_dev:
            if self.coding_tool is not None:
                raise ValueError(
                    "coding_tool is only valid when target_agent == 'dev'")
            if self.oracle is not None:
                raise ValueError(
                    "oracle is only valid for DEV tickets")
            if self.tier_reason is not None:
                raise ValueError("tier_reason is only valid for DEV tickets")
            if self.why_consensus_is_needed is not None:
                raise ValueError(
                    "Tier-S justification is only valid for DEV tickets")
            if self.why_single_session_is_insufficient is not None:
                raise ValueError(
                    "Tier-S justification is only valid for DEV tickets")
            if self.why_not_c is not None:
                raise ValueError("why_not_c is only valid for DEV tickets")
            return self

        if self.coding_tool is None:
            raise ValueError("DEV tickets require coding_tool")
        if not self.tier_reason:
            raise ValueError("DEV tickets require tier_reason")

        if self.coding_tool == DevTier.TIER_S:
            if not self.why_consensus_is_needed:
                raise ValueError("Tier S requires why_consensus_is_needed")
            if not self.why_single_session_is_insufficient:
                raise ValueError(
                    "Tier S requires why_single_session_is_insufficient")

        if self.coding_tool in (DevTier.TIER_A, DevTier.TIER_B):
            if not self.why_not_c:
                raise ValueError(
                    f"{self.coding_tool.value} requires why_not_c: name "
                    "what breaks if the default tier executes it")

        # Tier C is the default (policy §25): no oracle and no
        # justification required. A declared oracle of any type is
        # evidence; only qualifying types freeze (§24), which the
        # orchestrator enforces, not this schema. A why_not_c on a C
        # ticket contradicts itself: the planner meant tier_a/tier_b.
        if self.coding_tool == DevTier.TIER_C and self.why_not_c:
            raise ValueError(
                "tier_c is the default and carries no why_not_c: choose "
                "tier_a/tier_b for it or drop the statement")

        return self


class PlannerResult(BaseModel):
    objective_status: ObjectiveStatus
    state_summary: str = Field(min_length=1)

    information_gaps: list[InformationGap] = Field(default_factory=list)
    tickets: list[PlannedTicket] = Field(default_factory=list, max_length=3)
    roadmap_remaining: list[str] = Field(default_factory=list)

    @field_validator("state_summary")
    @classmethod
    def _summary_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("roadmap_remaining")
    @classmethod
    def _roadmap_bounded(cls, value: list[str]) -> list[str]:
        return _roadmap_within_bounds(_string_list_nonblank(value))

    @model_validator(mode="after")
    def validate_objective_state(self) -> "PlannerResult":
        if self.objective_status == ObjectiveStatus.COMPLETE_CANDIDATE \
                and self.tickets:
            raise ValueError("complete_candidate must not create child tickets")

        if self.objective_status in {
            ObjectiveStatus.BLOCKED,
            ObjectiveStatus.CAPABILITY_GAP,
        } and self.tickets:
            raise ValueError(
                f"{self.objective_status.value} should not create executable "
                "child tickets")

        refs = [ticket.local_ref for ticket in self.tickets]
        if len(refs) != len(set(refs)):
            raise ValueError("ticket local_ref values must be unique")

        return self


def parse_planner_result(text: str) -> PlannerResult:
    """Parse planner JSON output. Raises ValueError when malformed."""
    try:
        data = json.loads(text or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid PLANNER_RESULT.json: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("PLANNER_RESULT.json must be a JSON object")
    try:
        return PlannerResult.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: "
            f"{error['msg']}" for error in exc.errors()[:8])
        raise ValueError(f"invalid PlannerResult: {details}") from exc


def find_local_cycles(tickets: Sequence[PlannedTicket]) -> list[str]:
    """Dependency cycles over this result's local_refs (pure)."""
    keys = {ticket.local_ref for ticket in tickets}
    edges = {ticket.local_ref:
             [dep for dep in ticket.blocked_by if dep in keys]
             for ticket in tickets}
    errors: list[str] = []
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, stack: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            errors.append(f"blocked_by cycle: {' -> '.join([*stack, node])}")
            return
        visiting.add(node)
        for dep in edges.get(node, []):
            visit(dep, [*stack, node])
        visiting.discard(node)
        done.add(node)

    for key in edges:
        visit(key, [])
    return errors


def _effective_horizon(horizon: Any) -> tuple[int, int] | None:
    """Coerce a [min, max] horizon; None when malformed."""
    if horizon is None:
        return (1, 3)
    try:
        low, high = int(horizon[0]), int(horizon[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if low < 0 or high < low:
        return None
    return (low, high)


async def _validate_completion(
    errors: list[str],
    where: str,
    ticket: "PlannedTicket",
    task_exists: Callable[[str], Awaitable[bool]],
) -> None:
    """Deterministic rules for a contract completion (TASK-2026-00174).

    Completions reference instead of creating: no duplicate-title or
    same-tier-retry checks (those govern new work) and no deps or
    claimed findings. Tier presence for dev rides on the Pydantic
    routing contract; existence is checked here; tierless/linked/open
    suitability is enforced at apply time (it can change between
    prompt and patch, so it skips instead of failing the plan).
    """
    if ticket.blocked_by:
        errors.append(f"{where}: completions take no blocked_by")
    if ticket.includes_findings:
        errors.append(f"{where}: completions claim no findings")
    try:
        exists = await task_exists(str(ticket.completes_task_id or ""))
    except Exception:
        exists = False
    if not exists:
        errors.append(f"{where}: completes unknown "
                      f"{str(ticket.completes_task_id or '')!r}")


async def validate_planner_result(
    result: PlannerResult,
    *,
    agents: Mapping[str, Any],
    open_titles: Collection[str],
    task_exists: Callable[[str], Awaitable[bool]],
    material_failures: Sequence[Any] | None = None,
    horizon: Any = None,
) -> list[str]:
    """Deterministic validation (routing policy §17). Returns errors.

    ``agents`` is the authoritative registry (name → {enabled, reason?});
    ``open_titles`` holds open-sibling titles; ``task_exists`` resolves
    non-local ``blocked_by`` targets; ``material_failures`` lists
    ``{title, tier}`` pairs recorded by earlier material failures (fed by
    TASK-2026-00116) for the same-tier-retry rule; ``horizon`` is the
    configured [min, max] ticket horizon (default [1, 3]).
    """
    errors: list[str] = []
    bounds = _effective_horizon(horizon)
    if bounds is None:
        return ["planner horizon is misconfigured "
                "(global objective_planner.ticket_horizon)"]
    low, high = bounds
    if result.objective_status != ObjectiveStatus.ACTIVE:
        return errors  # Pydantic already enforced no tickets.
    creations = [ticket for ticket in result.tickets
                 if not ticket.completes_task_id]
    completions = [ticket for ticket in result.tickets
                   if ticket.completes_task_id]
    if len(creations) + len(completions) < low:
        errors.append(f"active status requires at least {low} ticket(s) "
                      "(policy §37: only blocked/capability_gap/"
                      "complete_candidate carry no tickets)")
    if len(creations) > high:
        errors.append(f"planning horizon is {low}-{high} tickets, got "
                      f"{len(creations)} new")
    if len(completions) > high:
        errors.append(f"planning horizon admits at most {high} contract "
                      f"completions, got {len(completions)}")
    refs = {ticket.local_ref for ticket in result.tickets}
    completion_tasks = {ticket.local_ref: str(ticket.completes_task_id or "")
                        for ticket in result.tickets
                        if ticket.completes_task_id}
    normalized_open = {str(title).strip().lower() for title in open_titles}
    seen_titles = set(normalized_open)
    failed = set()
    for entry in (material_failures or []):
        if not isinstance(entry, Mapping):
            continue
        failed.add((str(entry.get("title") or "").strip().lower(),
                    str(entry.get("tier") or "")))
    for ticket in result.tickets:
        where = f"ticket {ticket.local_ref!r}"
        spec = agents.get(ticket.target_agent)
        if not isinstance(spec, Mapping):
            errors.append(f"{where}: unknown target_agent "
                          f"{ticket.target_agent!r}")
        elif not spec.get("enabled", False):
            reason = str(spec.get("reason") or "agent disabled").strip()
            errors.append(f"{where}: target_agent "
                          f"{ticket.target_agent!r} is disabled ({reason})")
        if ticket.completes_task_id:
            await _validate_completion(
                errors, where, ticket, task_exists)
            continue
        for dep in ticket.blocked_by:
            if dep in completion_tasks:
                errors.append(
                    f"{where}: blocked_by names completion {dep!r}; "
                    f"depend on {completion_tasks[dep]!r} instead")
                continue
            if dep in refs:
                continue
            try:
                exists = await task_exists(str(dep))
            except Exception:
                exists = False
            if not exists:
                errors.append(f"{where}: blocked_by unknown {dep!r}")
        normalized_title = ticket.title.strip().lower()
        if normalized_title in seen_titles:
            errors.append(f"{where}: duplicates an open ticket")
        seen_titles.add(normalized_title)
        tier = ticket.coding_tool.value if ticket.coding_tool else ""
        if (normalized_title, tier) in failed:
            errors.append(
                f"{where}: same-tier retry after a material failure "
                f"({tier}); split the ticket or change its contract/"
                "oracle/scope first")
    errors.extend(find_local_cycles(result.tickets))
    return errors


#: The planner prompt carries at most this many characters of the policy
#: (:func:`policy_prompt_text`); anything longer is cut off, tail first, and
#: the tail is where the compact runtime kernel (§42) lives.
POLICY_PROMPT_MAX_CHARS = 60000


def policy_file(policy_path: str | Path | None = None) -> Path:
    """Where the planner reads its policy: ``policy_path`` or the shipped file.

    The policy is part of the planner's prompt, so by default it ships next to
    this module (TASK-2026-00181) and travels with the code in any deployment
    (host, Docker image) instead of living under docs/.
    """
    if policy_path is None:
        return Path(__file__).resolve().with_name("routing_policy.md")
    return Path(policy_path)


def load_policy_text(policy_path: str | Path | None = None) -> str:
    """Read the normative routing policy ("" when unreadable)."""
    try:
        text = policy_file(policy_path).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if text.strip() else ""


def policy_prompt_text(policy_text: str) -> str:
    """Exactly the policy text the planner prompt carries (one definition)."""
    return (policy_text or "").strip()[:POLICY_PROMPT_MAX_CHARS]


def policy_fingerprint(policy_text: str) -> str:
    """Short SHA-256 (12 hex) of what the planner prompt carries ("" if empty).

    Each planning run stores it (``planning_runs[].policy_sha``) so a decision
    can be traced to the policy text the planner actually saw. Plain hashing:
    no model call, and it never enters the prompt.
    """
    carried = policy_prompt_text(policy_text)
    if not carried:
        return ""
    return hashlib.sha256(carried.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class PolicyStatus:
    """Health of the planner policy file (:func:`check_policy`)."""

    ok: bool
    path: str
    chars: int
    sha: str
    problem: str = ""


def check_policy(policy_path: str | Path | None = None) -> PolicyStatus:
    """Is the policy readable and does it fit the prompt untruncated?"""
    path = policy_file(policy_path)
    text = load_policy_text(policy_path)
    if not text:
        return PolicyStatus(
            False, str(path), 0, "",
            "the file is missing, unreadable or empty")
    chars = len(text.strip())
    if chars > POLICY_PROMPT_MAX_CHARS:
        return PolicyStatus(
            False, str(path), chars, policy_fingerprint(text),
            f"{chars} characters exceed the prompt budget of "
            f"{POLICY_PROMPT_MAX_CHARS}: the tail (the compact runtime "
            "kernel) would be cut off")
    return PolicyStatus(True, str(path), chars, policy_fingerprint(text))


def log_policy_status(
    policy_path: str | Path | None, log: Any,
) -> PolicyStatus:
    """Boot-time report of :func:`check_policy` on ``log`` (a logger).

    INFO with the fingerprint when healthy, ERROR saying what will happen
    otherwise. It never raises and never blocks startup: the planner keeps
    failing closed per cycle (``plan_objective``), this only says so early.
    """
    status = check_policy(policy_path)
    if status.ok:
        log.info("Planner routing policy: %s (%d chars, sha %s)",
                 status.path, status.chars, status.sha)
    elif status.chars == 0:
        log.error(
            "Planner routing policy PROBLEM at %s: %s. Objective planning "
            "parks with 'config' (refusing to plan blind) until it is fixed.",
            status.path, status.problem)
    else:
        log.error(
            "Planner routing policy PROBLEM at %s: %s. The planner would "
            "run with the truncated text (sha %s).",
            status.path, status.problem, status.sha)
    return status
