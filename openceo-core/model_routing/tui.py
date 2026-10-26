"""Interactive OpenCode launcher over managed sessions (TASK-2026-00308).

Addendum v1.2 §5.3, preferred strategy: ``opencode-free-research``
prepares a managed session on the profile's best target and attaches
the interactive TUI to it with the documented ``--session`` flag.
The controller stays connected through the standalone session API:
it observes failures in the launcher-initiated session (§11.9) and
routes the next turn to the next eligible target. A turn
interrupted mid-stream is never recovered: the fallback lands on
the next turn, and a hand-opened TUI is not covered.

Verified against opencode v2.0.22 without spending quota: a
standalone-created session is visible to the TUI (shared
``opencode.db`` storage), the TUI attach preserves a valid
controller-set model, ``GET /api/session/{id}/message`` reports
newest-first ``idle`` outcome markers and ``model-switched``
records, and ``switchModel`` reroutes the next turn. Audits keep
ids, models and outcomes only: message text never leaves OpenCode.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from model_routing.availability import AvailabilityStore, write_audit_event
from model_routing.research_wiring import (
    FREE_RESEARCH_PROFILE,
    ResearchRoutingError,
    ResearchTarget,
    SessionClient,
    merge_session_provider_config,
)

__all__ = [
    "LAUNCHER_NAME",
    "TUI_LIMITATION",
    "LaunchPlan",
    "TuiFailure",
    "TuiFallback",
    "TuiObservation",
    "apply_next_turn_fallback",
    "observe_tui_session",
    "prepare_tui_session",
]

#: Logical launcher name from addendum §5.3; the CLI command is
#: ``free-router tui-launch``.
LAUNCHER_NAME = "opencode-free-research"

#: Visible capability boundary, repeated in CLI help and the runbook.
TUI_LIMITATION = (
    "managed launcher sessions only: a hand-opened TUI is not "
    "covered; fallback applies to the next turn, never mid-turn"
)


@dataclass(frozen=True)
class LaunchPlan:
    """A managed session ready for the interactive TUI to attach."""

    session_id: str
    profile: str
    target: ResearchTarget
    project_dir: Path
    argv: tuple[str, ...]
    audit: str


@dataclass(frozen=True)
class TuiFailure:
    """Newest explicit failure marker in a launcher session."""

    message_id: str
    message_type: str
    created: int | None


@dataclass(frozen=True)
class TuiObservation:
    """What the controller sees in a launcher-initiated session."""

    session_id: str
    model: str
    cost: float
    failure: TuiFailure | None
    failures_skipped: int
    drift: tuple[tuple[str, str], ...] = ()
    switched_since_failure: bool = False
    watermark: str | None = None
    messages_seen: int = 0
    watermark_unknown: bool = False


@dataclass(frozen=True)
class TuiFallback:
    """Next-turn routing decision after an observation."""

    applied: bool
    previous_model: str
    new_model: str | None
    reason: str
    audit: str


def _target_pair(target: ResearchTarget) -> tuple[str, str]:
    return (target.provider_id, target.model_id)


def _model_pair(model: str) -> tuple[str, str]:
    provider_id, _, model_id = (model or "").partition("/")
    return (provider_id, model_id)


def _audit(store: AvailabilityStore | None, run_id: str, event: str,
           detail: str) -> None:
    if store is not None:
        write_audit_event(store, run_id, event, detail)


def prepare_tui_session(
    *,
    client: SessionClient,
    project_dir: Path | str,
    targets: Sequence[ResearchTarget],
    cli: str = "opencode",
    title: str = "",
    profile: str = FREE_RESEARCH_PROFILE,
    store: AvailabilityStore | None = None,
    run_id: str = "-",
) -> LaunchPlan:
    """Create a session on the best target and return the attach plan.

    The first target wins, group A before group B. A group B first
    target merges its provider entries (``{env:...}`` references
    only) into the project ``opencode.json`` before the session is
    created, so a refused merge never orphans a session. The served
    model is read back: a mismatch fails closed with the session id
    left for inspection.
    """
    directory = Path(project_dir)
    if not cli:
        raise ResearchRoutingError("a launcher needs a CLI path")
    if not directory.is_dir():
        raise ResearchRoutingError(
            f"launcher project dir is not a directory: {directory}")
    if not targets:
        raise ResearchRoutingError(
            f"no servable {profile} target for the launcher")
    first = targets[0]
    if not first.provider_id or not first.model_id:
        raise ResearchRoutingError(
            "launcher targets need real provider and model ids")
    if first.is_external:
        if not first.provider_config:
            raise ResearchRoutingError(
                f"group B target {first.deployment_id} carries "
                "no provider config")
        merge_session_provider_config(directory, first.provider_config)
    session_id = client.create_session(
        directory, title=title or f"{LAUNCHER_NAME} {profile}")
    client.switch_model(directory, session_id, first.provider_id,
                        first.model_id)
    info = client.get_session(directory, session_id)
    if info.opencode_model != first.model_arg():
        raise ResearchRoutingError(
            f"launcher session {session_id} serves "
            f"{info.opencode_model or 'an unset model'}, "
            f"wanted {first.model_arg()}")
    audit = (f"{session_id} {profile} initial "
             f"{first.provider_id}/{first.model_id} result=ok")
    _audit(store, run_id, "tui_session_prepared", audit)
    return LaunchPlan(
        session_id=session_id, profile=profile, target=first,
        project_dir=directory,
        argv=(cli, "--session", session_id, str(directory)), audit=audit)


def _failure_marker(message: Mapping[str, Any]) -> bool:
    if not isinstance(message.get("id"), str):
        return False
    if message.get("outcome") == "failed":
        return True
    return message.get("type") == "error"


def observe_tui_session(
    *,
    client: SessionClient,
    project_dir: Path | str,
    session_id: str,
    after: str | None = None,
) -> TuiObservation:
    """Read a launcher session: model, cost, failures and drift.

    Only messages newer than the ``after`` watermark count; an
    unknown watermark considers everything and says so. Failure
    means an explicit signal (``outcome: failed`` of any type, or an
    ``error`` message): unknown shapes never trigger a fallback.
    ``drift`` lists ``model-switched`` records newer than the
    watermark, so the controller can tell a manual switch apart from
    its own. Ids, types and outcomes only: text stays in OpenCode.
    """
    info = client.get_session(project_dir, session_id)
    messages = client.list_messages(project_dir, session_id)
    watermark = None
    for message in messages:
        if isinstance(message.get("id"), str):
            watermark = message["id"]
            break
    considered: list[Mapping[str, Any]] = []
    watermark_unknown = False
    if after is None:
        considered = list(messages)
    else:
        for message in messages:
            if message.get("id") == after:
                break
            considered.append(message)
        else:
            watermark_unknown = any(
                isinstance(item.get("id"), str) for item in messages)
            if watermark_unknown:
                considered = list(messages)
    failure: TuiFailure | None = None
    failures_skipped = 0
    for message in messages:
        if not _failure_marker(message):
            continue
        if any(item is message for item in considered):
            if failure is None:
                created = message.get("time", {}).get("created") \
                    if isinstance(message.get("time"), dict) else None
                failure = TuiFailure(
                    message_id=str(message.get("id")),
                    message_type=str(message.get("type")),
                    created=created if isinstance(created, int) else None)
        else:
            failures_skipped += 1
    drift: list[tuple[str, str]] = []
    for message in considered:
        if message.get("type") != "model-switched":
            continue
        model = message.get("model")
        if not isinstance(model, dict):
            continue
        provider_id = model.get("providerID")
        model_id = model.get("id")
        if not provider_id or not model_id:
            continue
        drift.append((str(message.get("id")),
                      f"{provider_id}/{model_id}"))
    switched_since_failure = False
    if failure is not None:
        for message in considered:
            if message.get("id") == failure.message_id:
                break
            if (message.get("type") == "model-switched"
                    and isinstance(message.get("model"), dict)):
                switched_since_failure = True
    return TuiObservation(
        session_id=session_id, model=info.opencode_model, cost=info.cost,
        failure=failure, failures_skipped=failures_skipped,
        drift=tuple(drift), switched_since_failure=switched_since_failure,
        watermark=watermark, messages_seen=len(messages),
        watermark_unknown=watermark_unknown)


def apply_next_turn_fallback(
    *,
    client: SessionClient,
    project_dir: Path | str,
    observation: TuiObservation,
    targets: Sequence[ResearchTarget],
    profile: str = FREE_RESEARCH_PROFILE,
    store: AvailabilityStore | None = None,
    run_id: str = "-",
) -> TuiFallback:
    """Route the next turn after an observed failure, or stand down.

    No failure means no switch. A manual switch newer than the
    failure marker means the operator already moved on: the
    controller reports the drift instead of overriding it. The spare
    is the first ordered target serving a different model than the
    session serves now; without one the session stays resumable and
    blocked, untouched. A group B spare merges its provider entries
    before the switch, and the served model is read back.
    """
    directory = Path(project_dir)
    failure = observation.failure
    if failure is None:
        return TuiFallback(
            applied=False, previous_model=observation.model,
            new_model=None, reason="no-failure-observed",
            audit=f"{observation.session_id} {profile} "
                  f"{observation.model or 'unset'} result=healthy")
    if observation.switched_since_failure:
        return TuiFallback(
            applied=False, previous_model=observation.model,
            new_model=None, reason="model-changed-since-failure",
            audit=f"{observation.session_id} {profile} "
                  f"{observation.model or 'unset'} "
                  f"reason={failure.message_id} result=manual-switch-kept")
    current = _model_pair(observation.model)
    spare = next((item for item in targets
                  if _target_pair(item) != current
                  and item.provider_id and item.model_id), None)
    if spare is None:
        audit = (f"{observation.session_id} {profile} "
                 f"{observation.model or 'unset'} "
                 f"reason={failure.message_id} result=blocked")
        _audit(store, run_id, "tui_next_turn_fallback", audit)
        return TuiFallback(
            applied=False, previous_model=observation.model,
            new_model=None, reason="no-eligible-target", audit=audit)
    if spare.is_external:
        if not spare.provider_config:
            raise ResearchRoutingError(
                f"group B target {spare.deployment_id} carries "
                "no provider config")
        merge_session_provider_config(directory, spare.provider_config)
    client.switch_model(directory, observation.session_id,
                        spare.provider_id, spare.model_id)
    info = client.get_session(directory, observation.session_id)
    if info.opencode_model != spare.model_arg():
        raise ResearchRoutingError(
            f"launcher session {observation.session_id} serves "
            f"{info.opencode_model or 'an unset model'}, "
            f"wanted {spare.model_arg()}")
    audit = (f"{observation.session_id} {profile} "
             f"{observation.model or 'unset'} -> "
             f"{spare.provider_id}/{spare.model_id} "
             f"reason={failure.message_id} result=ok")
    _audit(store, run_id, "tui_next_turn_fallback", audit)
    return TuiFallback(
        applied=True, previous_model=observation.model,
        new_model=f"{spare.provider_id}/{spare.model_id}",
        reason="failure-observed", audit=audit)
