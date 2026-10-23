"""Feed research sessions from the free-research ranking (TASK-2026-00294).

SPEC §10 as code: one free model serves a whole research session
while viable (SPEC §10.2); the ranking snapshot stays frozen for
the session; every turn checkpoints objective, plan, queries and
sources, validated results, pending items, decisions and artifacts;
recovery follows §10.3 (before-response failure rotates the
deployment, partial text rebuilds from the checkpoint, executed
tools are never repeated, and no eligible model blocks resumably).

Only OpenCode-served routes feed research: synthesis runs the
``opencode`` CLI without credentials, so a ranked entry counts only
when its primary route (or one backup) runs on the ``opencode``
provider. Anything else (missing, expired or empty snapshot,
disabled switch) falls back to today's static model
(``RESEARCH_OPENCODE_TEST_MODEL``), unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol
from uuid import uuid4

from model_routing.availability import (
    AvailabilityStore,
    Candidate,
    CatalogEntry,
    Deployment,
    Exclusions,
    NoEligibleCandidate,
    ProfileRequirements,
    ReservationConflict,
    get_next_candidate,
    meets_profile,
    record_provider_error,
    release_reservation,
    reserve_model,
    write_audit_event,
)
from model_routing.errors import (
    ClassifiedError,
    RetryBudget,
    classify_http,
    classify_response,
    classify_text,
    classify_timeout,
)
from model_routing.models import CatalogDocument, ProfileRanking, RankingsDocument
from model_routing.paths import rankings_path, state_dir
from model_routing.ranking import fallback_chain, load_rankings

__all__ = [
    "BLOCKED_NO_ELIGIBLE_MODEL",
    "BLOCKED_PROVIDER",
    "BLOCKED_TOOL_CONFLICT",
    "CHECKPOINTS_DIRNAME",
    "CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_SNAPSHOT_MAX_AGE_HOURS",
    "FREE_RESEARCH_PROFILE",
    "RESEARCH_FALLBACK_MAX",
    "RESEARCH_SAME_RETRY_MAX",
    "RESEARCH_SYNTH_SLOT",
    "AppliedTool",
    "ProviderTurn",
    "ResearchCheckpoint",
    "ResearchRoutingError",
    "ResearchSession",
    "SessionBlocked",
    "SessionSnapshot",
    "SynthesisRouting",
    "ToolCall",
    "TurnInvoke",
    "TurnOutcome",
    "TurnRequest",
    "assemble_research_model",
    "checkpoints_dir",
    "close_synthesis_routing",
    "fit_candidate",
    "load_checkpoint",
    "load_session_snapshot",
    "plan_synthesis_retry",
    "record_checkpoint",
    "research_candidates",
    "research_router_settings",
]

logger = logging.getLogger(__name__)

FREE_RESEARCH_PROFILE = "free-research"

#: Rankings republish about every 6 h; past this age the order is too
#: stale to feed a session and the static model takes over.
DEFAULT_SNAPSHOT_MAX_AGE_HOURS = 30.0

#: §10.3 rotations per turn/synthesis; relief comes from the store TTLs.
RESEARCH_FALLBACK_MAX = 2

#: Same-model rebuilds per turn/synthesis (stream cut, timeout, novel
#: shapes); past this the session rotates or blocks, never spins.
RESEARCH_SAME_RETRY_MAX = 1

#: Slot label holding the session reservation in the shared store.
RESEARCH_SYNTH_SLOT = "investigator"

#: Resumable-block codes. Only the first one is SPEC §10.3.4; the
#: other two are fail-safe blocks, all saved with their checkpoint.
BLOCKED_NO_ELIGIBLE_MODEL = "BLOCKED_NO_ELIGIBLE_MODEL"
BLOCKED_TOOL_CONFLICT = "BLOCKED_TOOL_CONFLICT"
BLOCKED_PROVIDER = "BLOCKED_PROVIDER"

CHECKPOINTS_DIRNAME = "research_checkpoints"
CHECKPOINT_SCHEMA_VERSION = 1

#: §9.2 classes rotating to the next compatible deployment at once.
#: ``bad_response`` rotates here (unlike reviews): an empty or
#: unusable answer is a failure before a usable response (§10.3.1),
#: and the driver is the only retry authority of the session.
RESEARCH_ROTATE_CLASSES = frozenset({
    "rate_limited", "daily_quota", "auth", "billing",
    "not_found", "tools_unsupported", "context_exceeded", "bad_response",
})

#: Classes retrying on the same model first (SPEC §9.2 bounded
#: retries, §10.2 affinity, §10.3.2): one 5xx, timeout or cut stream
#: does not prove the model unviable; a repeat within the same turn
#: rotates instead.
RESEARCH_REBUILD_CLASSES = frozenset({"server_error", "stream_cut"})

#: Never rotates: a free-tier 403 signals the tool frontier, not a
#: dead model (TASK-2026-00288 handoff), and rotating would burn
#: models without diagnosing the config. Persistent 403 blocks.
RESEARCH_NEVER_ROTATE_CLASSES = frozenset({"forbidden"})

FailureStage = Literal["before_response", "partial_text", "after_tool"]

_RETRY_ACTION = Literal["rotate", "retry_same", "blocked"]

#: Backoff source for session retries (§9.2); the driver's own
#: counters bound the attempts, this only spaces them.
_RETRY_BUDGET = RetryBudget()


class ResearchRoutingError(RuntimeError):
    """Research feeding misconfigured or unreadable; the message says why."""


class SessionBlocked(ResearchRoutingError):
    """Resumable session block; the checkpoint on disk holds the state."""

    def __init__(self, code: str, message: str, *, checkpoint_path: Path | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.checkpoint_path = checkpoint_path


def research_router_settings(settings: Any) -> dict[str, Any]:
    """Normalized research free-router knobs; active by default.

    ``settings`` is the research settings object (``RESEARCH_*``
    attributes); missing attributes fall back to the defaults so
    minimal test doubles keep working.
    """
    enabled = getattr(settings, "RESEARCH_FREE_ROUTER_ENABLED", True)
    try:
        max_age = float(getattr(
            settings, "RESEARCH_FREE_ROUTER_SNAPSHOT_MAX_AGE_HOURS",
            DEFAULT_SNAPSHOT_MAX_AGE_HOURS))
    except (TypeError, ValueError) as exc:
        raise ResearchRoutingError(
            "RESEARCH_FREE_ROUTER_SNAPSHOT_MAX_AGE_HOURS must be a number") from exc
    if not max_age > 0:
        raise ResearchRoutingError(
            "RESEARCH_FREE_ROUTER_SNAPSHOT_MAX_AGE_HOURS must be above zero")
    return {"enabled": enabled is not False, "snapshot_max_age_hours": max_age}


def checkpoints_dir() -> Path:
    """Session checkpoints live in state, next to availability (SPEC §6.1)."""
    return state_dir() / CHECKPOINTS_DIRNAME


@dataclass(frozen=True)
class SessionSnapshot:
    """Rankings frozen for one session; consumers never see a swap (T12)."""

    document: RankingsDocument
    profile: ProfileRanking
    file_sha256: str
    catalog_snapshot: str
    generated_at: str


def load_session_snapshot(
    path: Path | None = None,
    *,
    max_age_hours: float = DEFAULT_SNAPSHOT_MAX_AGE_HOURS,
) -> SessionSnapshot | None:
    """Frozen free-research order, or None when nothing was published yet.

    A missing file is the normal static-model state, not an error.
    Unreadable, invalid, undated or expired snapshots fail closed.
    """
    target = Path(path) if path is not None else rankings_path()
    if not target.exists():
        return None
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ResearchRoutingError(f"rankings unreadable: {target}") from exc
    try:
        document = load_rankings(target)
    except Exception as exc:
        raise ResearchRoutingError(f"rankings {target} invalid: {exc}") from exc
    profile = document.profiles.get(FREE_RESEARCH_PROFILE)
    if profile is None:
        raise ResearchRoutingError(
            f"rankings {target} carry no {FREE_RESEARCH_PROFILE} profile")
    generated = document.generated_at
    if generated is None:
        raise ResearchRoutingError(f"rankings {target} are undated")
    moment = generated if generated.tzinfo else generated.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - moment).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        raise ResearchRoutingError(
            f"rankings {target} expired ({age_hours:.1f}h old, "
            f"max {max_age_hours:g}h)")
    digest = hashlib.sha256(raw).hexdigest()
    return SessionSnapshot(
        document=document, profile=profile, file_sha256=f"sha256:{digest}",
        catalog_snapshot=document.catalog_snapshot,
        generated_at=moment.isoformat())


def _find_deployment(
    catalog: CatalogDocument, deployment_id: str,
) -> tuple[CatalogEntry, Deployment] | None:
    for entry in catalog.entries:
        for deployment in entry.deployments:
            if deployment.deployment_id == deployment_id:
                return entry, deployment
    return None


def fit_candidate(
    candidate: Candidate,
    catalog: CatalogDocument,
    requirements: ProfileRequirements,
) -> tuple[bool, str]:
    """Pre-retry context and tools check against the catalog (SPEC §10.3).

    Unknown or missing capability data fails closed: a retry must
    never land on a model that cannot serve this session.
    """
    found = _find_deployment(catalog, candidate.deployment_id)
    if found is None:
        return False, f"{candidate.deployment_id} not in catalog"
    entry, deployment = found
    return meets_profile(entry, deployment, requirements)


def research_candidates(
    ranking: ProfileRanking,
    policy: Any | None = None,
    *,
    catalog: CatalogDocument | None = None,
    requirements: ProfileRequirements | None = None,
) -> list[Candidate]:
    """Ranked entries servable through the ``opencode`` CLI, in order.

    Floor and drop come from :func:`fallback_chain` (SPEC §8.4).
    Same canonical model twice collapses to its first occurrence:
    backups are not opinions (T05). Unknown families stay eligible:
    research needs one working model, not a verifiable quorum
    (unlike reviews, SPEC §11.2). With a catalog, entries also
    clear the context and tools gates before they can serve.
    """
    candidates: list[Candidate] = []
    seen_canonicals: set[str] = set()
    for item in fallback_chain(ranking, policy):
        if item.canonical_model_id in seen_canonicals:
            continue
        route = None
        if item.primary.provider == "opencode":
            route = item.primary.deployment_id
        else:
            for backup in item.backups:
                if backup.provider == "opencode":
                    route = backup.deployment_id
                    break
        if route is None:
            continue
        candidate = Candidate(
            deployment_id=route, family_id=item.family_id,
            canonical_model_id=item.canonical_model_id,
            provider="opencode", quality=item.quality)
        if catalog is not None:
            gates = requirements or ProfileRequirements(
                needs_tools=True, min_context_tokens=65536)
            fits, _reason = fit_candidate(candidate, catalog, gates)
            if not fits:
                continue
        seen_canonicals.add(item.canonical_model_id)
        candidates.append(candidate)
    return candidates


@dataclass(frozen=True)
class AppliedTool:
    """One tool call applied during the session; never re-executed."""

    id: str
    name: str
    result: str = ""


@dataclass(frozen=True)
class ToolCall:
    """One tool execution claimed by the provider for this attempt."""

    id: str
    name: str
    args_hash: str = ""


@dataclass(frozen=True)
class ResearchCheckpoint:
    """SPEC §10.2 checkpoint: resume state, never token-by-token."""

    research_id: str
    session_id: str
    turn: int
    objective: str
    plan: tuple[str, ...] = ()
    queries_and_sources: tuple[tuple[str, str], ...] = ()
    validated_results: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    applied_tools: tuple[AppliedTool, ...] = ()
    partial_text: str = ""
    candidates: tuple[Candidate, ...] = ()
    static_model: str = ""
    effective_deployment: str = ""
    rankings_snapshot: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "research_id": self.research_id,
            "session_id": self.session_id,
            "turn": self.turn,
            "objective": self.objective,
            "plan": list(self.plan),
            "queries_and_sources": [list(pair) for pair in self.queries_and_sources],
            "validated_results": list(self.validated_results),
            "pending": list(self.pending),
            "decisions": list(self.decisions),
            "artifacts": list(self.artifacts),
            "applied_tools": [
                {"id": tool.id, "name": tool.name, "result": tool.result}
                for tool in self.applied_tools],
            "partial_text": self.partial_text,
            "candidates": [
                {"deployment_id": item.deployment_id, "family_id": item.family_id,
                 "canonical_model_id": item.canonical_model_id,
                 "provider": item.provider, "quality": item.quality}
                for item in self.candidates],
            "static_model": self.static_model,
            "effective_deployment": self.effective_deployment,
            "rankings_snapshot": self.rankings_snapshot,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchCheckpoint":
        if not isinstance(payload, Mapping):
            raise ResearchRoutingError("checkpoint is not a mapping")
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ResearchRoutingError(
                "checkpoint schema_version is not "
                f"{CHECKPOINT_SCHEMA_VERSION}")
        try:
            research_id = str(payload["research_id"])
            session_id = str(payload["session_id"])
            turn = int(payload["turn"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchRoutingError(
                f"checkpoint misses identity fields: {exc}") from exc
        if not research_id or not session_id or turn < 0:
            raise ResearchRoutingError("checkpoint identity fields are invalid")

        def _texts(key: str) -> tuple[str, ...]:
            raw = payload.get(key) or []
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise ResearchRoutingError(f"checkpoint field {key} is not a text list")
            return tuple(raw)

        queries: list[tuple[str, str]] = []
        raw_queries = payload.get("queries_and_sources") or []
        if not isinstance(raw_queries, list):
            raise ResearchRoutingError("checkpoint queries_and_sources is not a list")
        for pair in raw_queries:
            if not isinstance(pair, list) or len(pair) != 2 or any(
                    not isinstance(item, str) for item in pair):
                raise ResearchRoutingError(
                    "checkpoint queries_and_sources holds a bad pair")
            queries.append((pair[0], pair[1]))
        tools: list[AppliedTool] = []
        raw_tools = payload.get("applied_tools") or []
        if not isinstance(raw_tools, list):
            raise ResearchRoutingError("checkpoint applied_tools is not a list")
        for item in raw_tools:
            if not isinstance(item, Mapping) or not item.get("id") or not item.get("name"):
                raise ResearchRoutingError("checkpoint applied_tools holds a bad tool")
            tools.append(AppliedTool(
                id=str(item["id"]), name=str(item["name"]),
                result=str(item.get("result") or "")))
        ordered: list[Candidate] = []
        raw_candidates = payload.get("candidates") or []
        if not isinstance(raw_candidates, list):
            raise ResearchRoutingError("checkpoint candidates is not a list")
        for item in raw_candidates:
            if not isinstance(item, Mapping) or not item.get("deployment_id"):
                raise ResearchRoutingError("checkpoint candidates holds a bad entry")
            quality = item.get("quality")
            ordered.append(Candidate(
                deployment_id=str(item["deployment_id"]),
                family_id=str(item.get("family_id") or ""),
                canonical_model_id=str(item.get("canonical_model_id") or ""),
                provider=str(item.get("provider") or ""),
                quality=float(quality) if quality is not None else None))
        return cls(
            research_id=research_id, session_id=session_id, turn=turn,
            objective=str(payload.get("objective") or ""),
            plan=_texts("plan"), queries_and_sources=tuple(queries),
            validated_results=_texts("validated_results"),
            pending=_texts("pending"), decisions=_texts("decisions"),
            artifacts=_texts("artifacts"), applied_tools=tuple(tools),
            partial_text=str(payload.get("partial_text") or ""),
            candidates=tuple(ordered),
            static_model=str(payload.get("static_model") or ""),
            effective_deployment=str(payload.get("effective_deployment") or ""),
            rankings_snapshot=str(payload.get("rankings_snapshot") or ""),
            updated_at=str(payload.get("updated_at") or ""))


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "session"


def checkpoint_path(checkpoints: Path, research_id: str) -> Path:
    """Checkpoint file of one research session."""
    return Path(checkpoints) / f"{_safe_name(research_id)}.json"


def record_checkpoint(checkpoints: Path, checkpoint: ResearchCheckpoint) -> Path:
    """Persist one checkpoint atomically; returns its path."""
    target = checkpoint_path(checkpoints, checkpoint.research_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(checkpoint.to_dict(), handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return target


def load_checkpoint(checkpoints: Path, research_id: str) -> ResearchCheckpoint | None:
    """Saved checkpoint, None when the session never checkpointed.

    A corrupt checkpoint fails closed: resume must never silently
    restart and re-execute applied tools.
    """
    target = checkpoint_path(checkpoints, research_id)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResearchRoutingError(
            f"checkpoint {target} unreadable: {exc}") from exc
    return ResearchCheckpoint.from_dict(payload)


def _classify_text(text: str, *, responded: bool = False) -> ClassifiedError:
    """Free-form failure mapped to §9.2 with explicit status winning.

    The CLI reports ``status=NNN`` (synthesis) and prose may carry a
    bare code; a bare 429 cools the deployment down instead of
    retrying it. Other bare codes follow the marks/transient path.
    """
    status = re.search(r"status=(\d{3})", text or "")
    if status is not None:
        return classify_http(int(status.group(1)), detail=text or "")
    if re.search(r"\b429\b", text or ""):
        return classify_http(429, detail=text or "")
    return classify_text(text or "", responded=responded)


def _retry_action(
    error_class: str, *, same_used: int, rotations_used: int,
) -> _RETRY_ACTION:
    """One §10.3 step shared by the session driver and synthesis."""
    if error_class in RESEARCH_ROTATE_CLASSES:
        if rotations_used < RESEARCH_FALLBACK_MAX:
            return "rotate"
        return "blocked"
    if error_class in RESEARCH_REBUILD_CLASSES:
        if same_used < RESEARCH_SAME_RETRY_MAX:
            return "retry_same"
        if rotations_used < RESEARCH_FALLBACK_MAX:
            return "rotate"
        return "blocked"
    # forbidden, unclassified and anything future: confirm once on the
    # same model, then block resumably; never burn the chain blindly.
    # The saved checkpoint plus the error-registry entry feed the
    # findings flow (TASK-2026-00297) for new or recurring classes.
    if same_used < RESEARCH_SAME_RETRY_MAX:
        return "retry_same"
    return "blocked"


@dataclass(frozen=True)
class TurnRequest:
    """One attempt seen by the provider: prompt plus resume context."""

    prompt: str
    model: str
    resume: bool = False
    applied_tools: tuple[AppliedTool, ...] = ()
    partial_text: str = ""


@dataclass(frozen=True)
class ProviderTurn:
    """One provider attempt: usable text, or a classified failure.

    ``executed_tools`` lists the tools the provider ran during THIS
    attempt; their results arrive in ``tool_results`` as
    ``(tool id, result summary)`` pairs. A complete turn without the
    result of a claimed tool is inconsistent, never a success (T13).
    """

    text: str
    complete: bool
    executed_tools: tuple[ToolCall, ...] = ()
    tool_results: tuple[tuple[str, str], ...] = ()
    error_text: str = ""
    failure_stage: FailureStage = "before_response"


class TurnInvoke(Protocol):
    """One turn attempt against any provider (simulated or real)."""

    def __call__(self, request: TurnRequest) -> ProviderTurn:  # pragma: no cover
        ...


@dataclass
class TurnOutcome:
    """One finished turn: text, effective model and its checkpoint."""

    text: str
    turn: int
    model_effective: str
    deployment_id: str
    resumed: bool
    rotations: int
    checkpoint: ResearchCheckpoint


class ResearchSession:
    """One free-research session: affinity, checkpoints, §10.3 recovery.

    The same model serves every turn while viable; the candidate
    order stays frozen for the session; every finished or blocked
    turn persists a checkpoint. Without candidates the session runs
    on ``static_model`` with same-model rebuilds only.
    """

    def __init__(
        self,
        *,
        research_id: str,
        candidates: list[Candidate] | tuple[Candidate, ...] = (),
        store: AvailabilityStore | None = None,
        checkpoints: Path | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        objective: str = "",
        plan: tuple[str, ...] | list[str] = (),
        static_model: str = "",
        rankings_snapshot: str = "",
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        if not research_id:
            raise ResearchRoutingError("a research session needs a research_id")
        self._research_id = research_id
        self._candidates = tuple(candidates)
        self._store = store
        self._checkpoints = Path(checkpoints) if checkpoints is not None else checkpoints_dir()
        self._run_id = run_id or research_id
        self._session_id = session_id or f"rses-{uuid4().hex[:12]}"
        self._static_model = static_model
        self._rankings_snapshot = rankings_snapshot
        self._objective_plan = (objective, tuple(plan))
        self._sleeper = sleeper or time.sleep
        self._turn = 0
        self._queries: list[tuple[str, str]] = []
        self._validated: list[str] = []
        self._pending: list[str] = []
        self._decisions: list[str] = []
        self._artifacts: list[str] = []
        self._ledger: dict[str, AppliedTool] = {}
        self._partial_text = ""
        self._effective: Candidate | None = None
        self._reserved = False
        self._closed = False

    @property
    def research_id(self) -> str:
        return self._research_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def effective_model(self) -> str:
        if self._effective is not None:
            return self._effective.deployment_id
        return self._static_model

    @property
    def ledger(self) -> dict[str, AppliedTool]:
        return dict(self._ledger)

    def _snapshot_checkpoint(self, *, turn: int, partial_text: str) -> ResearchCheckpoint:
        return ResearchCheckpoint(
            research_id=self._research_id, session_id=self._session_id,
            turn=turn, objective=self._objective_plan[0],
            plan=self._objective_plan[1],
            queries_and_sources=tuple(self._queries),
            validated_results=tuple(self._validated),
            pending=tuple(self._pending), decisions=tuple(self._decisions),
            artifacts=tuple(self._artifacts),
            applied_tools=tuple(self._ledger.values()),
            partial_text=partial_text, candidates=self._candidates,
            static_model=self._static_model,
            effective_deployment=self.effective_model,
            rankings_snapshot=self._rankings_snapshot,
            updated_at=datetime.now(timezone.utc).isoformat())

    def _save(self, *, turn: int, partial_text: str) -> ResearchCheckpoint:
        checkpoint = self._snapshot_checkpoint(turn=turn, partial_text=partial_text)
        record_checkpoint(self._checkpoints, checkpoint)
        return checkpoint

    @classmethod
    def resume(
        cls,
        checkpoints: Path,
        research_id: str,
        *,
        store: AvailabilityStore | None = None,
    ) -> "ResearchSession":
        """Rebuild a session from its last consistent checkpoint.

        The frozen candidate order, the tool ledger and the valid
        prefix come back verbatim; affinity re-selects the pinned
        effective deployment when it is still servable.
        """
        saved = load_checkpoint(checkpoints, research_id)
        if saved is None:
            raise ResearchRoutingError(
                f"no checkpoint for {research_id} in {checkpoints}")
        session = cls(
            research_id=saved.research_id, candidates=list(saved.candidates),
            store=store, checkpoints=checkpoints, run_id=saved.research_id,
            session_id=saved.session_id, objective=saved.objective,
            plan=list(saved.plan), static_model=saved.static_model,
            rankings_snapshot=saved.rankings_snapshot)
        session._turn = saved.turn
        session._queries = list(saved.queries_and_sources)
        session._validated = list(saved.validated_results)
        session._pending = list(saved.pending)
        session._decisions = list(saved.decisions)
        session._artifacts = list(saved.artifacts)
        session._ledger = {tool.id: tool for tool in saved.applied_tools}
        session._partial_text = saved.partial_text
        for candidate in session._candidates:
            if candidate.deployment_id == saved.effective_deployment:
                session._effective = candidate
                break
        return session

    def close(self) -> None:
        """Release the session reservation; best-effort, idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._reserved and self._store is not None:
            try:
                release_reservation(
                    self._store, self._run_id, self._session_id, RESEARCH_SYNTH_SLOT)
            except Exception:  # closing never fails a session
                logger.warning("research session release failed", exc_info=True)
            self._reserved = False

    def _reserve(self, candidate: Candidate) -> None:
        if self._store is None:
            return
        reserve_model(
            self._store, self._run_id, self._session_id, RESEARCH_SYNTH_SLOT,
            candidate.canonical_model_id, candidate.family_id,
            candidate.deployment_id)
        self._reserved = True

    def _release(self) -> None:
        if self._reserved and self._store is not None:
            release_reservation(
                self._store, self._run_id, self._session_id, RESEARCH_SYNTH_SLOT)
            self._reserved = False

    def _next_candidate(self, skip: Exclusions) -> Candidate:
        if self._store is None:
            for candidate in self._candidates:
                if candidate.deployment_id in skip.deployments:
                    continue
                if candidate.canonical_model_id in skip.canonicals:
                    continue
                if candidate.family_id in skip.families:
                    continue
                return candidate
            raise NoEligibleCandidate("session order exhausted")
        return get_next_candidate(
            list(self._candidates), self._store, exclusions=skip)

    def _adopt_or_reserve(self) -> None:
        """Adopt our live slot row, else reserve the pinned model."""
        store = self._store
        pinned = self._effective
        if store is None or pinned is None:
            return
        held = [item for item in store.reservations(
            self._run_id, self._session_id) if item.slot == RESEARCH_SYNTH_SLOT]
        if held:
            self._reserved = True
            return
        try:
            self._reserve(pinned)
        except ReservationConflict:
            self._effective = None

    def _select_initial(self) -> None:
        """Pin the first reservable candidate, skipping raced ones."""
        tried_deployments: set[str] = set()
        tried_canonicals: set[str] = set()
        for _ in range(len(self._candidates) + 1):
            try:
                first = self._next_candidate(Exclusions(
                    deployments=frozenset(tried_deployments),
                    canonicals=frozenset(tried_canonicals)))
            except NoEligibleCandidate:
                return
            self._effective = first
            try:
                self._reserve(first)
            except ReservationConflict:
                tried_deployments.add(first.deployment_id)
                tried_canonicals.add(first.canonical_model_id)
                self._effective = None
                continue
            return

    def _reconcile_tools(self, attempt: ProviderTurn) -> None:
        """Merge claimed executions into the ledger (§10.3.3).

        Applied tools are never re-executed; a claimed tool without
        its result, or with a result contradicting the ledger, fails
        safe instead of declaring success (T13).
        """
        results = dict(attempt.tool_results)
        for claimed in attempt.executed_tools:
            known = self._ledger.get(claimed.id)
            if known is not None:
                if known.name != claimed.name:
                    raise SessionBlocked(
                        BLOCKED_TOOL_CONFLICT,
                        f"tool {claimed.id} renamed mid-session "
                        f"({known.name} -> {claimed.name})")
                fresh = results.get(claimed.id)
                if fresh is not None and fresh != known.result:
                    raise SessionBlocked(
                        BLOCKED_TOOL_CONFLICT,
                        f"tool {claimed.id} result contradicts the ledger")
                continue
            result = results.get(claimed.id)
            if result is None:
                raise SessionBlocked(
                    BLOCKED_TOOL_CONFLICT,
                    f"tool {claimed.id} executed without its result")
            self._ledger[claimed.id] = AppliedTool(
                id=claimed.id, name=claimed.name, result=result)

    def run_turn(
        self,
        prompt: str,
        invoke: TurnInvoke,
        *,
        queries_and_sources: list[tuple[str, str]] | None = None,
        validated: list[str] | None = None,
        pending: list[str] | None = None,
        decisions: list[str] | None = None,
        artifacts: list[str] | None = None,
    ) -> TurnOutcome:
        """Run one turn to completion, recovery (§10.3) included.

        Returns the finished turn with its checkpoint. Raises
        :class:`SessionBlocked` with the state saved when no
        compatible model remains or a tool conflict fails safe.
        """
        if self._closed:
            raise ResearchRoutingError("the session is closed")
        if not (prompt or "").strip():
            raise ResearchRoutingError("a turn needs a prompt")
        pending_goal = list(pending or [])
        attempt_prompt = prompt
        resumed = False
        rotations = 0
        same_used = 0
        if self._effective is not None and self._store is not None:
            try:
                get_next_candidate([self._effective], self._store)
            except NoEligibleCandidate:
                self._release()
                self._effective = None
            else:
                if not self._reserved:
                    self._adopt_or_reserve()
        if self._effective is None:
            self._select_initial()
        if self._effective is None and not self._static_model:
            checkpoint = self._save_with_goal(pending_goal)
            raise SessionBlocked(
                BLOCKED_NO_ELIGIBLE_MODEL,
                "no eligible research model for this session",
                checkpoint_path=checkpoint_path(self._checkpoints, self._research_id))
        if self._effective is None:
            return self._run_static_turn(
                prompt, invoke, queries_and_sources=queries_and_sources,
                validated=validated, pending=pending_goal, decisions=decisions,
                artifacts=artifacts)
        candidate = self._effective
        tried_deployments = {candidate.deployment_id}
        tried_canonicals = {candidate.canonical_model_id}
        while True:
            try:
                attempt = invoke(TurnRequest(
                    prompt=attempt_prompt, model=candidate.deployment_id,
                    resume=resumed,
                    applied_tools=tuple(self._ledger.values()),
                    partial_text=self._partial_text))
            except SessionBlocked:
                raise
            except Exception as exc:
                attempt = ProviderTurn(
                    text="", complete=False, error_text=str(exc),
                    failure_stage="before_response")
            try:
                self._reconcile_tools(attempt)
            except SessionBlocked as blocked:
                checkpoint = self._save_with_goal(pending_goal)
                raise SessionBlocked(
                    blocked.code, str(blocked),
                    checkpoint_path=checkpoint_path(
                        self._checkpoints, self._research_id)) from None
            if attempt.complete and attempt.text.strip():
                self._apply_deltas(
                    queries_and_sources, validated, pending_goal, decisions,
                    artifacts)
                self._turn += 1
                self._partial_text = ""
                checkpoint = self._save(turn=self._turn, partial_text="")
                return TurnOutcome(
                    text=attempt.text, turn=self._turn,
                    model_effective=candidate.deployment_id,
                    deployment_id=candidate.deployment_id, resumed=resumed,
                    rotations=rotations, checkpoint=checkpoint)
            if attempt.complete:
                failure: ProviderTurn = ProviderTurn(
                    text="", complete=False,
                    error_text="empty response without usable text",
                    failure_stage="before_response")
            else:
                failure = attempt
            if failure.failure_stage == "partial_text" and len(failure.text) > len(
                    self._partial_text):
                self._partial_text = failure.text
            classified = self._classify(failure)
            action = _retry_action(
                classified.error_class, same_used=same_used,
                rotations_used=rotations)
            if action == "retry_same":
                self._backoff(same_used + rotations)
                same_used += 1
                resumed = True
                attempt_prompt = self._rebuild_prompt(prompt)
                continue
            if action == "rotate":
                self._backoff(same_used + rotations)
                rotations += 1
                resumed = True
                self._record_rotation(candidate, classified, failure.error_text)
                self._release()
                tried_deployments.add(candidate.deployment_id)
                tried_canonicals.add(candidate.canonical_model_id)
                skip = Exclusions(
                    deployments=frozenset(tried_deployments),
                    canonicals=frozenset(tried_canonicals))
                while True:
                    try:
                        candidate = self._next_candidate(skip)
                    except NoEligibleCandidate:
                        checkpoint = self._save_with_goal(pending_goal)
                        raise SessionBlocked(
                            BLOCKED_NO_ELIGIBLE_MODEL,
                            "no compatible research model left this turn",
                            checkpoint_path=checkpoint_path(
                                self._checkpoints, self._research_id)) from None
                    self._effective = candidate
                    try:
                        self._reserve(candidate)
                    except ReservationConflict:
                        tried_deployments.add(candidate.deployment_id)
                        tried_canonicals.add(candidate.canonical_model_id)
                        skip = Exclusions(
                            deployments=frozenset(tried_deployments),
                            canonicals=frozenset(tried_canonicals))
                        continue
                    break
                attempt_prompt = self._rebuild_prompt(prompt)
                continue
            checkpoint = self._save_with_goal(pending_goal)
            if classified.error_class in (
                    RESEARCH_ROTATE_CLASSES | RESEARCH_REBUILD_CLASSES):
                code = BLOCKED_NO_ELIGIBLE_MODEL
            else:
                code = BLOCKED_PROVIDER
            raise SessionBlocked(
                code,
                f"research turn blocked on {classified.error_class}: "
                f"{classified.action}",
                checkpoint_path=checkpoint_path(
                    self._checkpoints, self._research_id))

    def _run_static_turn(
        self,
        prompt: str,
        invoke: TurnInvoke,
        *,
        queries_and_sources: list[tuple[str, str]] | None,
        validated: list[str] | None,
        pending: list[str],
        decisions: list[str] | None,
        artifacts: list[str] | None,
    ) -> TurnOutcome:
        """Single-model turn: rebuild once, then block resumably."""
        same_used = 0
        resumed = False
        attempt_prompt = prompt
        while True:
            try:
                attempt = invoke(TurnRequest(
                    prompt=attempt_prompt, model=self._static_model,
                    resume=resumed,
                    applied_tools=tuple(self._ledger.values()),
                    partial_text=self._partial_text))
            except Exception as exc:
                attempt = ProviderTurn(
                    text="", complete=False, error_text=str(exc),
                    failure_stage="before_response")
            try:
                self._reconcile_tools(attempt)
            except SessionBlocked as blocked:
                checkpoint = self._save_with_goal(pending)
                raise SessionBlocked(
                    blocked.code, str(blocked),
                    checkpoint_path=checkpoint_path(
                        self._checkpoints, self._research_id)) from None
            if attempt.complete and attempt.text.strip():
                self._apply_deltas(
                    queries_and_sources, validated, pending, decisions,
                    artifacts)
                self._turn += 1
                self._partial_text = ""
                checkpoint = self._save(turn=self._turn, partial_text="")
                return TurnOutcome(
                    text=attempt.text, turn=self._turn,
                    model_effective=self._static_model,
                    deployment_id=self._static_model, resumed=resumed,
                    rotations=0, checkpoint=checkpoint)
            failure = attempt if not attempt.complete else ProviderTurn(
                text="", complete=False, error_text="empty response without usable text",
                failure_stage="before_response")
            if failure.failure_stage == "partial_text" and len(failure.text) > len(
                    self._partial_text):
                self._partial_text = failure.text
            classified = self._classify(failure)
            if classified.error_class in RESEARCH_NEVER_ROTATE_CLASSES:
                action: _RETRY_ACTION = (
                    "retry_same" if same_used < RESEARCH_SAME_RETRY_MAX else "blocked")
            elif same_used < RESEARCH_SAME_RETRY_MAX:
                action = "retry_same"
            else:
                action = "blocked"
            if action == "retry_same":
                self._backoff(same_used)
                same_used += 1
                resumed = True
                attempt_prompt = self._rebuild_prompt(prompt)
                continue
            checkpoint = self._save_with_goal(pending)
            raise SessionBlocked(
                BLOCKED_NO_ELIGIBLE_MODEL,
                f"static research model failed ({classified.error_class}); "
                "resume when it serves again",
                checkpoint_path=checkpoint_path(
                    self._checkpoints, self._research_id))

    def _save_with_goal(self, pending_goal: list[str]) -> ResearchCheckpoint:
        for item in pending_goal:
            if item not in self._pending:
                self._pending.append(item)
        return self._save(turn=self._turn, partial_text=self._partial_text)

    def _apply_deltas(
        self,
        queries: list[tuple[str, str]] | None,
        validated: list[str] | None,
        pending: list[str],
        decisions: list[str] | None,
        artifacts: list[str] | None,
    ) -> None:
        for pair in queries or []:
            if pair not in self._queries:
                self._queries.append(pair)
        for item in validated or []:
            if item not in self._validated:
                self._validated.append(item)
        for item in pending:
            if item not in self._pending:
                self._pending.append(item)
        for item in decisions or []:
            if item not in self._decisions:
                self._decisions.append(item)
        for item in artifacts or []:
            if item not in self._artifacts:
                self._artifacts.append(item)

    def _rebuild_prompt(self, prompt: str) -> str:
        """Resume context for a rebuild: valid prefix plus applied tools."""
        lines = [prompt]
        if self._partial_text:
            lines.append(f"Valid prefix kept, continue from it: {self._partial_text}")
        if self._ledger:
            applied = ", ".join(
                f"{tool.id}={tool.result}" for tool in self._ledger.values())
            lines.append(f"Already applied, do not re-execute: {applied}")
        return "\n".join(lines)

    def _classify(self, failure: ProviderTurn) -> ClassifiedError:
        text = failure.error_text or failure.text or ""
        partial = failure.failure_stage == "partial_text"
        classified = _classify_text(text, responded=partial)
        if classified.error_class == "unclassified" and partial:
            return classify_response("stream_cut")
        return classified

    def _backoff(self, hops_used: int) -> None:
        self._sleeper(_RETRY_BUDGET.delay(hops_used))

    def _record_rotation(
        self, candidate: Candidate, classified: ClassifiedError, excerpt: str,
    ) -> None:
        if self._store is None:
            return
        record_provider_error(
            self._store, provider=candidate.provider,
            deployment_id=candidate.deployment_id,
            model=candidate.deployment_id, classified=classified,
            run_id=self._run_id, excerpt=excerpt or "")
        write_audit_event(
            self._store, self._run_id, "research_fallback",
            f"{candidate.deployment_id}: {classified.error_class}")


@dataclass
class SynthesisRouting:
    """One-shot routing for research synthesis; None-store means static."""

    model: str
    active: bool
    candidates: list[Candidate] = field(default_factory=list)
    store: AvailabilityStore | None = None
    owns_store: bool = False
    run_id: str = ""
    session_id: str = ""
    effective: Candidate | None = None
    tried_deployments: set[str] = field(default_factory=set)
    tried_canonicals: set[str] = field(default_factory=set)
    rotations: int = 0
    same_used: int = 0
    rankings_snapshot: str = ""


def assemble_research_model(
    settings: Any,
    *,
    research_id: str,
    store: AvailabilityStore | None = None,
    rankings_file: Path | None = None,
) -> SynthesisRouting:
    """Model for one synthesis: ranking-fed, or today's static model.

    Disabled switch, missing snapshot and empty order all fall back
    to ``RESEARCH_OPENCODE_TEST_MODEL``; unreadable, invalid or
    expired snapshots degrade to static with a warning, like the
    review pool (TASK-2026-00293).
    """
    static_model = str(getattr(settings, "RESEARCH_OPENCODE_TEST_MODEL", ""))
    routing = SynthesisRouting(model=static_model, active=False, run_id=research_id)
    try:
        options = research_router_settings(settings)
    except ResearchRoutingError as exc:
        logger.warning("research router misconfigured, static model: %s", exc)
        return routing
    if not options["enabled"]:
        return routing
    try:
        snapshot = load_session_snapshot(
            rankings_file, max_age_hours=options["snapshot_max_age_hours"])
    except ResearchRoutingError as exc:
        logger.warning("research rankings unusable, static model: %s", exc)
        return routing
    if snapshot is None:
        return routing
    candidates = research_candidates(snapshot.profile)
    if not candidates:
        logger.warning("research rankings carry no opencode route, static model")
        return routing
    active_store = store if store is not None else AvailabilityStore()
    routing = SynthesisRouting(
        model=candidates[0].deployment_id, active=True,
        candidates=candidates, store=active_store,
        owns_store=store is None, run_id=research_id,
        session_id=f"rses-{uuid4().hex[:12]}",
        rankings_snapshot=snapshot.file_sha256)
    for candidate in candidates:
        try:
            get_next_candidate([candidate], active_store)
        except NoEligibleCandidate:
            routing.tried_deployments.add(candidate.deployment_id)
            routing.tried_canonicals.add(candidate.canonical_model_id)
            continue
        try:
            reserve_model(
                active_store, research_id, routing.session_id,
                RESEARCH_SYNTH_SLOT, candidate.canonical_model_id,
                candidate.family_id, candidate.deployment_id)
        except ReservationConflict:
            routing.tried_deployments.add(candidate.deployment_id)
            routing.tried_canonicals.add(candidate.canonical_model_id)
            continue
        routing.model = candidate.deployment_id
        routing.effective = candidate
        routing.tried_deployments.add(candidate.deployment_id)
        routing.tried_canonicals.add(candidate.canonical_model_id)
        return routing
    logger.warning("no eligible research model, static model")
    if routing.owns_store:
        try:
            active_store.close()
        except Exception:  # degrading never fails synthesis
            logger.warning("research store close failed", exc_info=True)
    return SynthesisRouting(model=static_model, active=False, run_id=research_id)


def _classify_runtime_error(exc: BaseException) -> ClassifiedError | None:
    """Runtime failure mapped to §9.2, or None when it must not retry."""
    from research.runtimes.base import (  # local: research owns these types
        RuntimeCancelled,
        RuntimeConfigurationError,
        RuntimeExecutionError,
        RuntimeMalformedOutput,
        RuntimeOutputTooLarge,
        RuntimeTimeout,
        RuntimeUnavailable,
    )

    if isinstance(exc, (RuntimeCancelled, RuntimeConfigurationError,
                        RuntimeUnavailable, RuntimeOutputTooLarge)):
        return None
    if isinstance(exc, RuntimeTimeout):
        return classify_timeout(responded=False)
    if isinstance(exc, RuntimeMalformedOutput):
        return classify_text(str(exc) or "empty response without usable text")
    if isinstance(exc, RuntimeExecutionError):
        return _classify_text(exc.runtime_error or str(exc))
    return _classify_text(str(exc))


def plan_synthesis_retry(
    routing: SynthesisRouting, exc: BaseException,
) -> str | None:
    """Next §10.3 step for synthesis: model, same rebuild, or terminal.

    Returns the deployment to retry (rotated or unchanged), or None
    when synthesis keeps today's behavior: a coverage gap, no silent
    success. A reservation race skips to the next candidate.
    """
    if not routing.active or routing.store is None or routing.effective is None:
        return None
    classified = _classify_runtime_error(exc)
    if classified is None:
        return None
    action = _retry_action(
        classified.error_class, same_used=routing.same_used,
        rotations_used=routing.rotations)
    if action == "retry_same":
        routing.same_used += 1
        return routing.effective.deployment_id
    if action != "rotate":
        return None
    effective = routing.effective
    record_provider_error(
        routing.store, provider=effective.provider,
        deployment_id=effective.deployment_id,
        model=effective.deployment_id, classified=classified,
        run_id=routing.run_id, excerpt=str(exc)[:300])
    release_reservation(
        routing.store, routing.run_id, routing.session_id, RESEARCH_SYNTH_SLOT)
    for _ in range(len(routing.candidates) + 1):
        try:
            nxt = get_next_candidate(
                list(routing.candidates), routing.store,
                exclusions=Exclusions(
                    deployments=frozenset(routing.tried_deployments),
                    canonicals=frozenset(routing.tried_canonicals)))
        except NoEligibleCandidate:
            return None
        try:
            reserve_model(
                routing.store, routing.run_id, routing.session_id,
                RESEARCH_SYNTH_SLOT, nxt.canonical_model_id, nxt.family_id,
                nxt.deployment_id)
        except ReservationConflict:
            write_audit_event(
                routing.store, routing.run_id, "research_fallback_conflict",
                nxt.deployment_id)
            routing.tried_deployments.add(nxt.deployment_id)
            routing.tried_canonicals.add(nxt.canonical_model_id)
            continue
        routing.rotations += 1
        routing.effective = nxt
        routing.tried_deployments.add(nxt.deployment_id)
        routing.tried_canonicals.add(nxt.canonical_model_id)
        write_audit_event(
            routing.store, routing.run_id, "research_fallback",
            f"{effective.deployment_id} -> {nxt.deployment_id}")
        return nxt.deployment_id
    return None


def close_synthesis_routing(routing: SynthesisRouting) -> None:
    """Release the synthesis reservation and an owned store handle."""
    if routing.active and routing.store is not None:
        try:
            release_reservation(
                routing.store, routing.run_id, routing.session_id,
                RESEARCH_SYNTH_SLOT)
        except Exception:  # closing never fails synthesis
            logger.warning("research synthesis release failed", exc_info=True)
        if routing.owns_store:
            try:
                routing.store.close()
            except Exception:
                logger.warning("research store close failed", exc_info=True)
