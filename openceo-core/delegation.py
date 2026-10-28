"""Delegation of external actions by category and project (TASK-2026-00340).

D12: actions on external systems that are not messages on behalf of the
Owner start with confirmation. The Owner may delegate them by category
and project, with evidence. The delegation is an executable policy in
the tool (``tool_locks.check_delegated_action``), never a model
conclusion, and it is revocable. Messages on behalf of the Owner,
production, protected-branch merges, irreversible actions and anything
outside a delegation always confirm. No learning and no precedent ever
creates or widens a delegation: only an explicit Owner grant does.

External clients sit behind an interface; tests use the simulated
client. Without a configured client the execution fails closed.

No tokens. The category naming (channel), the message-operation set
and the simulated default are agent decisions, reopenable by the Owner
(recorded in TASK-2026-00340).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

__all__ = [
    "IRREVERSIBLE_OPERATION_PREFIXES",
    "MESSAGE_OPERATIONS",
    "DelegatedClient",
    "DelegationError",
    "SimulatedDelegatedClient",
    "configure_delegated_client",
    "delegated_action",
    "delegated_fingerprint",
    "delegation_record",
    "execute_delegated_action",
    "grant_delegation",
    "is_delegated",
    "is_irreversible_operation",
    "is_message_operation",
    "list_delegations",
    "reset_delegated_clients",
    "reset_delegations",
    "revoke_delegation",
]

#: Operations that count as a message on behalf of the Owner through
#: the delegated path (D12): they always confirm, even when their
#: channel and project hold a delegation. Comments and replies in
#: Jira/Autotask address third parties, so they are messages too.
MESSAGE_OPERATIONS: tuple[str, ...] = (
    "send",
    "send_message",
    "post_message",
    "post",
    "add_comment",
    "comment",
    "create_comment",
    "post_comment",
    "reply",
    "add_reply",
    "add_note",
)

#: Destructive prefixes: an operation starting with one of these is
#: irreversible (D12, ticket clause 3) and always confirms, even when
#: its channel and project hold a delegation.
IRREVERSIBLE_OPERATION_PREFIXES: tuple[str, ...] = (
    "delete_",
    "destroy_",
    "purge_",
    "drop_",
)


class DelegationError(ValueError):
    """The delegation request is malformed or has no client (fail-closed)."""


class DelegatedClient(Protocol):
    """Interface every delegated external action executes through."""

    def execute(self, action: Mapping[str, Any]) -> dict[str, Any]:
        """Run the approved action; return the provider outcome."""
        ...  # pragma: no cover


class SimulatedDelegatedClient:
    """In-memory client for tests: records executions, delivers nothing."""

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    def execute(self, action: Mapping[str, Any]) -> dict[str, Any]:
        stored = dict(action)
        self.executed.append(stored)
        return {"ok": True, "simulated": True, "action": stored}


_DELEGATIONS: dict[tuple[str, str], dict[str, Any]] = {}
_CLIENTS: dict[str, DelegatedClient] = {}


def _clean_project(project: str) -> str:
    clean = str(project or "").strip()
    if not clean:
        raise DelegationError("a delegation needs its project")
    return clean


def _clean_category(category: str) -> str:
    clean = str(category or "").strip().lower()
    if not clean:
        raise DelegationError("a delegation needs its category")
    return clean


def _clean_channel(channel: str) -> str:
    clean = str(channel or "").strip().lower()
    if not clean:
        raise DelegationError("a delegated action needs its channel")
    return clean


def _clean_operation(operation: str) -> str:
    clean = str(operation or "").strip().lower()
    if not clean:
        raise DelegationError("a delegated action needs its operation")
    return clean


def _clean_target(target: str) -> str:
    clean = str(target or "").strip()
    if not clean:
        raise DelegationError("a delegated action needs its target")
    return clean


def grant_delegation(
    *,
    project: str,
    category: str,
    evidence: str,
    granted_by: str = "owner",
) -> dict[str, Any]:
    """Record the Owner's delegation of a category in a project. Pure.

    Evidence is required (D12: delegation with evidence): a blank
    reference fails closed instead of recording an unjustified grant.
    Re-granting refreshes the evidence. Precedent stores never call
    this: no learning path widens a delegation by itself.
    """
    clean_project = _clean_project(project)
    clean_category = _clean_category(category)
    clean_evidence = str(evidence or "").strip()
    if not clean_evidence:
        raise DelegationError(
            "a delegation needs its evidence "
            "(accepted proposals, incident-free run, owner note)")
    record = {
        "project": clean_project,
        "category": clean_category,
        "evidence": clean_evidence,
        "granted_by": str(granted_by or "").strip() or "owner",
        "granted_at": datetime.now(timezone.utc).isoformat(),
    }
    _DELEGATIONS[(clean_project, clean_category)] = record
    return dict(record)


def revoke_delegation(*, project: str, category: str) -> bool:
    """Drop a delegation. True when one existed. Pure."""
    key = (_clean_project(project), _clean_category(category))
    return _DELEGATIONS.pop(key, None) is not None


def is_delegated(*, project: str, category: str) -> bool:
    """Tell whether a category runs without asking in a project. Pure."""
    try:
        key = (_clean_project(project), _clean_category(category))
    except DelegationError:
        return False
    return key in _DELEGATIONS


def delegation_record(project: str, category: str) -> dict[str, Any] | None:
    """The stored grant with its evidence, or None. Pure."""
    try:
        key = (_clean_project(project), _clean_category(category))
    except DelegationError:
        return None
    record = _DELEGATIONS.get(key)
    return dict(record) if record is not None else None


def list_delegations(*, project: str | None = None) -> list[dict[str, Any]]:
    """Stored grants, optionally scoped to one project. Pure."""
    if project is None:
        return [dict(record) for record in _DELEGATIONS.values()]
    clean = _clean_project(project)
    return [dict(record) for (proj, _), record in _DELEGATIONS.items()
            if proj == clean]


def reset_delegations() -> None:
    """Forget every grant (tests)."""
    _DELEGATIONS.clear()


def configure_delegated_client(
    channel: str, client: DelegatedClient
) -> None:
    """Register the client one channel executes delegated actions through."""
    _CLIENTS[_clean_channel(channel)] = client


def reset_delegated_clients() -> None:
    """Forget registered clients (tests)."""
    _CLIENTS.clear()


def is_message_operation(operation: str) -> bool:
    """Tell whether an operation is a message on behalf of the Owner. Pure."""
    return _clean_operation(operation) in MESSAGE_OPERATIONS


def is_irreversible_operation(operation: str) -> bool:
    """Tell whether an operation is destructive (always confirms). Pure."""
    clean = _clean_operation(operation)
    return clean in ("delete", "destroy", "purge", "drop") or clean.startswith(
        IRREVERSIBLE_OPERATION_PREFIXES)


def delegated_action(
    channel: str, operation: str, target: str, detail: str = ""
) -> dict[str, Any]:
    """Stored action a delegation (or an approval) executes. Pure."""
    return {
        "tool": f"delegated:{_clean_channel(channel)}",
        "channel": _clean_channel(channel),
        "operation": _clean_operation(operation),
        "target": _clean_target(target),
        "detail": str(detail or ""),
    }


def delegated_fingerprint(
    channel: str, operation: str, target: str, detail: str = ""
) -> str:
    """Stable id of the exact delegated params. Pure.

    Asking again with changed params yields another id, so a change
    can never ride an old approval or an old delegation check.
    """
    action = delegated_action(channel, operation, target, detail)
    canonical = json.dumps(
        action, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def execute_delegated_action(action: Mapping[str, Any]) -> dict[str, Any]:
    """Run the stored action through the registered client.

    Only the delegation path and the lock decision path call this.
    Raises DelegationError without a registered client or with a
    blank target/operation (fail-closed, never a silent run).
    """
    if not isinstance(action, Mapping):
        raise DelegationError("delegated action is not a mapping")
    channel = _clean_channel(str(action.get("channel") or ""))
    operation = _clean_operation(str(action.get("operation") or ""))
    target = _clean_target(str(action.get("target") or ""))
    client = _CLIENTS.get(channel)
    if client is None:
        raise DelegationError(
            f"no client configured for {channel}; real use with "
            "credentials is out of this epic")
    return dict(client.execute({
        "tool": f"delegated:{channel}",
        "channel": channel,
        "operation": operation,
        "target": target,
        "detail": str(action.get("detail") or ""),
    }))
