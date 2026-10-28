"""External sends on behalf of the Owner: Teams, Jira, Autotask (TASK-2026-00337).

D12: messages on behalf of the Owner always need confirmation showing the
destination and the exact text; what is approved sends exactly once. These
tools reuse ``tool_locks`` and ``owner_channel``; no second approval system
is created.

External clients sit behind an interface; tests use the simulated client.
Real use with credentials is out of this epic: without a configured client
the execution fails closed instead of sending.

No tokens. Thresholds (scope naming, simulated default) are agent decisions,
reopenable by the Owner (recorded in TASK-2026-00337).
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from company_mandate import OwnerAction, requires_owner
from orchestration.precedents import record_owner_precedent
from tool_locks import (
    action_fingerprint,
    lock_question_spec,
    lock_source_id,
)

__all__ = [
    "EXTERNAL_TOOLS",
    "ExternalSendError",
    "ExternalClient",
    "SimulatedExternalClient",
    "configure_external_client",
    "reset_external_clients",
    "check_external_send_lock",
    "external_send_action",
    "external_send_fingerprint",
    "external_send_question_spec",
    "external_scope",
    "execute_external_send",
    "record_external_answer",
    "record_external_answer_in_store",
]

#: Channels sent only through the Owner lock (D12).
EXTERNAL_TOOLS: tuple[str, ...] = ("teams", "jira", "autotask")


class ExternalSendError(ValueError):
    """The external send request is malformed or has no client (fail-closed)."""


class ExternalClient(Protocol):
    """Interface every external channel client implements."""

    def send(self, destination: str, text: str) -> dict[str, Any]:
        """Deliver the approved text; return the provider outcome."""
        ...  # pragma: no cover


class SimulatedExternalClient:
    """In-memory client for tests: records sends, delivers nothing."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, destination: str, text: str) -> dict[str, Any]:
        self.sent.append({"destination": destination, "text": text})
        return {"ok": True, "simulated": True,
                "destination": destination, "text": text}


_CLIENTS: dict[str, ExternalClient] = {}


def configure_external_client(tool: str, client: ExternalClient) -> None:
    """Register the client one external channel sends through."""
    clean = str(tool or "").strip().lower()
    if clean not in EXTERNAL_TOOLS:
        raise ExternalSendError(f"unknown external tool {tool!r}")
    _CLIENTS[clean] = client


def reset_external_clients() -> None:
    """Forget registered clients (tests)."""
    _CLIENTS.clear()


def _clean_tool(tool: str) -> str:
    clean = str(tool or "").strip().lower()
    if clean not in EXTERNAL_TOOLS:
        raise ExternalSendError(f"unknown external tool {tool!r}")
    return clean


def _clean_destination(destination: str) -> str:
    clean = str(destination or "").strip()
    if not clean:
        raise ExternalSendError("an external send needs its destination")
    return clean


def _clean_text(text: str) -> str:
    if not str(text or "").strip():
        raise ExternalSendError("an external send needs its text")
    return str(text)


def check_external_send_lock(
    *,
    tool: str,
    destination: str,
    text: str,
    mandate: Any | None,
) -> dict[str, Any]:
    """Decide whether an external send needs the Owner. Pure: always.

    Every external send is an irreversible action through
    ``requires_owner``; the human action shows the destination and the
    exact text (D12 confirmation).
    """
    clean_tool = _clean_tool(tool)
    clean_destination = _clean_destination(destination)
    clean_text = _clean_text(text)
    action = OwnerAction(
        kind=f"external-send:{clean_tool}",
        summary=f"Enviar por {clean_tool} a {clean_destination}",
        irreversible=True,
        human_action_hint=(
            f"Autoriza el envio por {clean_tool} a {clean_destination} "
            f"con el texto exacto: {clean_text}."
        ),
        details={"tool": clean_tool, "destination": clean_destination},
    )
    decision = requires_owner(action, mandate)
    return {
        "escalate": bool(decision.escalate),
        "category": decision.category.value if decision.category else None,
        "reason": decision.reason,
        "human_action": decision.human_action,
        "tool": clean_tool,
        "destination": clean_destination,
        "text": clean_text,
    }


def external_send_action(
    tool: str, destination: str, text: str
) -> dict[str, Any]:
    """Stored action an approval executes exactly once. Pure."""
    return {
        "tool": _clean_tool(tool),
        "destination": _clean_destination(destination),
        "text": _clean_text(text),
    }


def external_send_fingerprint(
    tool: str, destination: str, text: str
) -> str:
    """Stable id of the exact destination + text. Pure.

    The fingerprint binds the exact text: asking again with a changed
    text yields another id, so it can never ride an old approval.
    """
    action = external_send_action(tool, destination, text)
    return action_fingerprint(action)


def external_send_question_spec(
    *,
    tool: str,
    destination: str,
    text: str,
    decision: Mapping[str, Any],
    origin_task_id: str | None = None,
    origin_title: str | None = None,
    project: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Spec for a ``lock`` owner-question over an external send. Pure."""
    clean_tool = _clean_tool(tool)
    clean_destination = _clean_destination(destination)
    clean_text = _clean_text(text)
    fingerprint = external_send_fingerprint(
        clean_tool, clean_destination, clean_text)
    origin_key = str(origin_task_id or "").strip() or "no-origin"
    return lock_question_spec(
        tool=clean_tool,
        name=f"{clean_tool}_send",
        question=f"Enviar por {clean_tool} a {clean_destination}?"[:200],
        action=external_send_action(
            clean_tool, clean_destination, clean_text),
        fingerprint=fingerprint,
        decision=decision,
        describe={"destination": clean_destination, "text": clean_text},
        context=f"Motivo: {decision.get('reason')}",
        origin_task_id=origin_task_id,
        origin_title=origin_title,
        project=project,
        source_id=(str(source_id or "").strip()
                   or lock_source_id(origin_key, clean_tool, fingerprint)),
    )


def external_scope(tool: str) -> str:
    """Precedent scope of one external channel (agent decision, 00337)."""
    return f"external:{_clean_tool(tool)}"


def execute_external_send(action: Mapping[str, Any]) -> dict[str, Any]:
    """Deliver the stored action through the registered client.

    Only the lock decision path calls this, after the execution marker
    check. Raises ExternalSendError without a registered client or
    with a blank destination/text (fail-closed, never a silent send).
    """
    if not isinstance(action, Mapping):
        raise ExternalSendError("locked action is not a mapping")
    tool = _clean_tool(str(action.get("tool") or ""))
    destination = _clean_destination(str(action.get("destination") or ""))
    text = _clean_text(str(action.get("text") or ""))
    client = _CLIENTS.get(tool)
    if client is None:
        raise ExternalSendError(
            f"no client configured for {tool}; real use with credentials "
            "is out of this epic")
    return dict(client.send(destination, text))


def record_external_answer(
    store: dict[str, Any],
    *,
    project: str,
    tool: str,
    question_task_id: str,
    question_summary: str,
    origin_task_id: str | None,
    approve: bool,
    answered_by: str,
    comment: str,
    decided_at: str | None = None,
):
    """Log one Owner answer to an external send as a precedent (D10)."""
    return record_owner_precedent(
        store,
        project=project,
        scope=external_scope(tool),
        question_task_id=question_task_id,
        question_kind="lock",
        question_summary=question_summary,
        origin_task_id=origin_task_id,
        approve=approve,
        answered_by=answered_by,
        comment=comment,
        decided_at=decided_at,
    )


def record_external_answer_in_store(
    store_path: Any,
    *,
    project: str,
    tool: str,
    question_task_id: str,
    question_summary: str,
    origin_task_id: str | None,
    approve: bool,
    answered_by: str,
    comment: str,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Load the runtime store, log the answer, save. Never breaks callers."""
    from orchestration.precedents import load_store, save_store

    store = load_store(store_path)
    precedent = record_external_answer(
        store,
        project=project,
        tool=tool,
        question_task_id=question_task_id,
        question_summary=question_summary,
        origin_task_id=origin_task_id,
        approve=approve,
        answered_by=answered_by,
        comment=comment,
        decided_at=decided_at,
    )
    save_store(store, store_path)
    return {"applied": True, "precedent_id": precedent.precedent_id}

