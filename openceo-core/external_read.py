"""Third-party connectors in read-only mode (TASK-2026-00338).

D13: Jira, Autotask and Teams stay the original source of their tickets
and conversations. OpenCEO keeps references (``source``, ``source_id``)
and never duplicates them in the ERP. Connectors run read-only; every
external write goes through the 4.1 tools (``tool_external_send`` over
``tool_locks`` and ``owner_channel``).

The 1.1 audit (TASK-2026-00323) found no third-party connectors in use:
``jira`` exists only as a source label (``TaskSource``). This module is
the standing guarantee for when connectors are added: a read-operation
allowlist per channel plus a closed gate for anything else. Future
connector code must call :func:`require_external_read` before any
remote call and :func:`resolve_external_reference` before mirroring
an external item locally.

No tokens. The channel set mirrors ``external_send.EXTERNAL_TOOLS``;
the operation names are agent decisions, reopenable by the Owner
(recorded in TASK-2026-00338).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from external_send import EXTERNAL_TOOLS

__all__ = [
    "EXTERNAL_CHANNELS",
    "READ_OPERATIONS",
    "ExternalReadError",
    "is_read_operation",
    "check_external_operation",
    "require_external_read",
    "external_reference",
    "reference_key",
    "resolve_external_reference",
]

#: Channels covered by the read-only guarantee (same set as 4.1 sends).
EXTERNAL_CHANNELS: tuple[str, ...] = EXTERNAL_TOOLS

#: Read-only operations a future connector may call directly. Anything
#: not listed here is treated as a write and must go through the 4.1
#: send tools with Owner confirmation.
READ_OPERATIONS: dict[str, tuple[str, ...]] = {
    "jira": (
        "get_issue",
        "search_issues",
        "list_projects",
        "get_comments",
        "get_transitions",
        "get_user",
    ),
    "autotask": (
        "get_ticket",
        "query_tickets",
        "get_notes",
        "get_task",
        "query_tasks",
    ),
    "teams": (
        "get_message",
        "list_messages",
        "list_channels",
        "get_channel",
        "search_messages",
    ),
}


class ExternalReadError(ValueError):
    """An external connector call is not read-only or not allowlisted."""


def _clean_channel(channel: str) -> str:
    clean = str(channel or "").strip().lower()
    if clean not in EXTERNAL_CHANNELS:
        raise ExternalReadError(f"unknown external channel {channel!r}")
    return clean


def _clean_operation(operation: str) -> str:
    clean = str(operation or "").strip().lower()
    if not clean:
        raise ExternalReadError("an external call needs its operation")
    return clean


def is_read_operation(channel: str, operation: str) -> bool:
    """Tell whether a connector call is an allowlisted read. Pure."""
    clean_channel = _clean_channel(channel)
    clean_operation = _clean_operation(operation)
    return clean_operation in READ_OPERATIONS.get(clean_channel, ())


def check_external_operation(
    *,
    channel: str,
    operation: str,
) -> dict[str, Any]:
    """Gate one connector call: reads pass, writes do not. Pure.

    Anything outside the read allowlist — writes and unknown
    operations alike — is refused fail-closed and pointed at the 4.1
    send tools, which carry the Owner confirmation.
    """
    clean_channel = _clean_channel(channel)
    clean_operation = _clean_operation(operation)
    if clean_operation in READ_OPERATIONS.get(clean_channel, ()):
        return {
            "allowed": True,
            "channel": clean_channel,
            "operation": clean_operation,
            "reason": "allowlisted read-only operation",
        }
    return {
        "allowed": False,
        "channel": clean_channel,
        "operation": clean_operation,
        "reason": (
            f"operation {clean_operation!r} is not read-only; external "
            "writes go through tool_external_send (4.1) with Owner "
            "confirmation"
        ),
    }


def require_external_read(
    channel: str, operation: str
) -> dict[str, str]:
    """Allow one connector call or raise. Pure, fail-closed.

    Future connector code calls this before any remote call; a write
    attempt raises instead of reaching the provider.
    """
    decision = check_external_operation(
        channel=channel, operation=operation)
    if not decision["allowed"]:
        raise ExternalReadError(str(decision["reason"]))
    return {
        "channel": str(decision["channel"]),
        "operation": str(decision["operation"]),
    }


def external_reference(source: str, source_id: str) -> dict[str, str]:
    """Build the local handle of an external item. Pure.

    OpenCEO stores this reference instead of duplicating the item in
    the ERP (D13): the remote system stays the source of truth.
    """
    clean_source = _clean_channel(source)
    clean_id = "" if source_id is None else str(source_id).strip()
    if not clean_id:
        raise ExternalReadError("an external reference needs its id")
    return {"source": clean_source, "source_id": clean_id}


def reference_key(reference: Mapping[str, Any]) -> str:
    """Stable ``source:source_id`` key of one external reference. Pure."""
    if not isinstance(reference, Mapping):
        raise ExternalReadError("an external reference is a mapping")
    raw_source = reference.get("source")
    raw_id = reference.get("source_id")
    return "{source}:{source_id}".format(
        **external_reference(
            "" if raw_source is None else str(raw_source),
            "" if raw_id is None else str(raw_id),
        )
    )


def resolve_external_reference(
    find_existing: Callable[[str, str], Any | None],
    create_new: Callable[[dict[str, str]], Any],
    *,
    source: str,
    source_id: str,
) -> dict[str, Any]:
    """Return the local item for an external id without duplicating it.

    When the reference already resolves locally the existing item is
    returned and ``create_new`` never runs; otherwise it runs exactly
    once with the validated reference. Mirrors the
    ``find_task_by_source`` idempotency in ``task_service``.
    Adapters are synchronous: resolve any coroutine before calling.
    """
    reference = external_reference(source, source_id)
    existing = find_existing(reference["source"], reference["source_id"])
    if existing is not None:
        return {"created": False, "task": existing}
    return {"created": True, "task": create_new(reference)}
