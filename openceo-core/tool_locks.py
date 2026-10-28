"""Locks inside dangerous boundary tools (TASK-2026-00208).

Pure checks, no I/O, no tokens. What is irreversible must not depend on
the LLM obeying: email to off-allowlist recipients, DNS deletion and
payments go through ``requires_owner`` and, on escalation, through the
single human channel as ``lock`` owner-questions (``owner_channel.py``).

The approval executes the stored action exactly once; re-runs return the
stored outcome instead of re-executing. :class:`TaskService` owns the
persistence and execution; this module owns the decisions and shapes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from company_mandate import OwnerAction, requires_owner
from owner_channel import build_owner_question, owner_question_block
import delegation as delegation_mod

__all__ = [
    "LOCK_QUESTION_KIND",
    "LOCK_SOURCE_PREFIX",
    "LOCK_WAITING_PREFIX",
    "PENDING_STATUS",
    "ToolLockError",
    "action_fingerprint",
    "check_delegated_action",
    "check_dns_delete_lock",
    "check_email_lock",
    "check_payment_lock",
    "email_allowance",
    "is_lock_pending",
    "lock_hold_action",
    "lock_question_spec",
    "lock_source_id",
    "lock_waiting_reason",
    "pending_lock_result",
]

#: Kind of the owner-question a lock raises (see ``owner_channel``).
LOCK_QUESTION_KIND = "lock"

#: Execution ``waiting_reason`` prefix of an origin parked on a lock.
LOCK_WAITING_PREFIX = "tool_lock:"

#: ``source_id`` prefix of lock questions (idempotency across re-runs).
LOCK_SOURCE_PREFIX = "tool-lock:"

#: ``status`` of a tool result held for Owner approval.
PENDING_STATUS = "pending_approval"

#: Max entries rendered into a question description.
_MAX_RENDERED = 8

#: Max characters of a rendered field in a question description.
_RENDER_CAP = 500


class ToolLockError(ValueError):
    """The lock request is malformed (fail-closed: fix the call)."""


def _clean_addresses(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        # Mirror SMTPEmailSender._normalize_addresses: a string holds a
        # comma-joined envelope, and each address locks on its own.
        items = values.split(",")
    else:
        try:
            items = list(values)
        except TypeError:
            items = [values]
    cleaned: list[str] = []
    for item in items:
        if item is None:
            continue
        # Stricter than the sender on purpose (review 00008): an element
        # hiding several addresses behind a comma locks on each one,
        # never on the joined tail.
        for part in str(item).split(","):
            text = part.strip()
            if text:
                cleaned.append(text)
    return cleaned


def _is_simple_address(value: str) -> bool:
    """One plain address: exactly one @ and no whitespace. Pure."""
    if not value or any(ch.isspace() for ch in value):
        return False
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and bool(domain)


def email_allowance(
    recipients: Sequence[str] | None,
    allowed: Sequence[str] | None,
) -> dict[str, list[str]]:
    """Split recipients into allowed and blocked. Pure.

    An allowlist entry is an exact address (case-insensitive) or an
    ``@domain`` suffix. No entry matches nobody: an empty allowlist
    blocks everything (fail-closed).
    """
    rules = [str(entry).strip().lower() for entry in (allowed or [])]
    rules = [rule for rule in rules if rule]
    exact = {rule for rule in rules if not rule.startswith("@")}
    suffixes = tuple(rule for rule in rules if rule.startswith("@") and len(rule) > 1)
    passed: list[str] = []
    blocked: list[str] = []
    for recipient in _clean_addresses(recipients):
        lowered = recipient.lower()
        if lowered in exact or any(
            lowered.endswith(suffix) and "@" in lowered for suffix in suffixes
        ):
            passed.append(recipient)
        else:
            blocked.append(recipient)
    return {"allowed": passed, "blocked": blocked}


def _decision_dict(decision: Any) -> dict[str, Any]:
    return {
        "escalate": bool(decision.escalate),
        "category": decision.category.value if decision.category else None,
        "reason": decision.reason,
        "human_action": decision.human_action,
    }


def check_email_lock(
    *,
    to: Any,
    cc: Any = None,
    bcc: Any = None,
    project_config: Mapping[str, Any] | None,
    mandate: Any | None,
) -> dict[str, Any]:
    """Decide whether an email send needs the Owner. Pure.

    Every envelope recipient must be on the project's
    ``secretary.allowed_recipients``; one off-list recipient makes the
    send an irreversible action through ``requires_owner``.
    """
    envelope = _clean_addresses(to) + _clean_addresses(cc) + _clean_addresses(bcc)
    secretary = (project_config or {}).get("secretary")
    allowed = secretary.get("allowed_recipients") if isinstance(secretary, dict) else None
    split = email_allowance(envelope, allowed if isinstance(allowed, list) else [])
    # Anything that is not one plain address (spaces, malformed,
    # display-name forms) never rides the allowlist (review 00008).
    split["blocked"].extend(
        item for item in split["allowed"] if not _is_simple_address(item))
    split["allowed"] = [
        item for item in split["allowed"] if _is_simple_address(item)]
    if not split["blocked"]:
        return {
            "escalate": False,
            "category": None,
            "reason": "all recipients on the project allowlist",
            "human_action": None,
            "recipients": envelope,
            "blocked": [],
        }
    off = ", ".join(split["blocked"][:_MAX_RENDERED])
    action = OwnerAction(
        kind="send-email",
        summary=f"Enviar correo a {off}",
        irreversible=True,
        human_action_hint=(
            f"Autoriza el envio a destinatario(s) fuera de la lista: {off}."
        ),
        details={"blocked": sorted({item.lower() for item in split["blocked"]})},
    )
    decision = requires_owner(action, mandate)
    result = _decision_dict(decision)
    result.update({
        "recipients": envelope,
        "blocked": split["blocked"],
    })
    return result


def check_dns_delete_lock(*, hostname: str, mandate: Any | None) -> dict[str, Any]:
    """Decide whether a DNS deletion needs the Owner. Pure: always."""
    action = OwnerAction(
        kind="dns-delete",
        summary=f"Borrar el registro DNS de {str(hostname or '').strip()}",
        irreversible=True,
        details={"hostname": str(hostname or "").strip()},
    )
    return _decision_dict(requires_owner(action, mandate))


def check_payment_lock(
    *,
    summary: str,
    mandate: Any | None,
    amount: Any = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether a payment needs the Owner. Pure.

    Every payment run passes ``requires_owner`` with its declared
    amount; practice runs carry no amount and still escalate as
    irreversible payment-tool runs, so the lock never depends on a
    mode flag the caller sets.
    """
    action = OwnerAction(
        kind="payment",
        summary=str(summary or "Ejecutar un pago").strip(),
        amount=amount,
        irreversible=True,
        details=dict(details or {}),
    )
    return _decision_dict(requires_owner(action, mandate))


def action_fingerprint(parts: Mapping[str, Any]) -> str:
    """Stable 12-hex id of normalized action params. Pure."""
    canonical = json.dumps(
        parts, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def lock_source_id(origin_key: str, tool: str, fingerprint: str) -> str:
    """Stable idempotency key for a lock question. Pure."""
    clean_origin = str(origin_key or "").strip() or "no-origin"
    clean_tool = str(tool or "").strip() or "tool"
    clean_fp = str(fingerprint or "").strip() or "nofp"
    return f"{LOCK_SOURCE_PREFIX}{clean_origin}:{clean_tool}:{clean_fp}"


def lock_waiting_reason(tool: str) -> str:
    """Operational wait of an origin parked on a lock. Pure."""
    return f"{LOCK_WAITING_PREFIX}{str(tool or '').strip() or 'tool'}"


def _rendered(value: Any) -> str:
    return " ".join(str(value or "").split())[:_RENDER_CAP]


def lock_question_spec(
    *,
    tool: str,
    name: str,
    question: str,
    action: Mapping[str, Any],
    fingerprint: str,
    decision: Mapping[str, Any],
    describe: Mapping[str, Any] | None = None,
    context: str | None = None,
    origin_task_id: str | None = None,
    origin_title: str | None = None,
    project: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    """Spec for a ``lock`` owner-question. Pure: no I/O, no tokens.

    The flat checkpoint renders into the description (what the Owner
    approves); the ``tool`` slot stores the exact spec the approval
    executes once. Raises ToolLockError without a decision to escalate.
    """
    if not decision.get("escalate"):
        raise ToolLockError("nothing to escalate: the lock decision is automatic")
    human_action = str(decision.get("human_action") or "").strip()
    if not human_action:
        raise ToolLockError("a lock question needs the exact human action")
    checkpoint: dict[str, Any] = {
        "tool_lock": True,
        "tool": str(tool or "").strip(),
        "fingerprint": str(fingerprint or "").strip(),
    }
    for key, value in dict(describe or {}).items():
        checkpoint[str(key)] = _rendered(value)
    return build_owner_question(
        kind=LOCK_QUESTION_KIND,
        question=question,
        human_action=human_action,
        origin_task_id=origin_task_id,
        origin_title=origin_title,
        checkpoint=checkpoint,
        context=context,
        tool={
            "name": str(name or "").strip(),
            "action": json.loads(json.dumps(dict(action), default=str)),
            "fingerprint": str(fingerprint or "").strip(),
        },
        project=project,
        source_id=source_id,
    )


def lock_hold_action(task: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Stored lock action of a ``lock`` question, else None. Pure."""
    block = owner_question_block(task)
    if str(block.get("kind") or "") != LOCK_QUESTION_KIND:
        return None
    checkpoint = block.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    if checkpoint.get("tool_lock") is not True:
        return None
    stored = block.get("tool")
    action = stored.get("action") if isinstance(stored, Mapping) else None
    if not isinstance(action, Mapping):
        return None
    return {
        "tool": str(checkpoint.get("tool") or ""),
        "name": str(stored.get("name") or "") if isinstance(stored, Mapping) else "",
        "fingerprint": str(checkpoint.get("fingerprint") or ""),
        "action": dict(action),
        "execution": dict(block.get("lock") or {}).get("execution"),
    }


def pending_lock_result(
    *,
    tool: str,
    question_task_id: str | None,
    waiting_reason: str,
    detail: str,
) -> dict[str, Any]:
    """Tool answer while the Owner decides. Pure."""
    return {
        "ok": True,
        "status": PENDING_STATUS,
        "tool": str(tool or ""),
        "question_task_id": question_task_id,
        "waiting_reason": waiting_reason,
        "detail": _rendered(detail),
        "guidance": (
            "Quedo pendiente de aprobacion del dueno; "
            "termina como bloqueado."
        ),
    }


def is_lock_pending(result: Any) -> bool:
    """True when a tool result is a lock hold. Pure."""
    return isinstance(result, dict) and result.get("status") == PENDING_STATUS


def check_delegated_action(
    *,
    project: str,
    category: str,
    operation: str = "",
) -> dict[str, Any]:
    """Decide whether an external action needs the Owner. Pure.

    A stored Owner grant for the (project, category) pair runs without
    asking; anything else escalates with the exact human action. The
    grant itself is recorded by ``delegation.grant_delegation`` with
    its evidence — this check only reads it, so no learning path can
    widen a delegation through here. Messages on behalf of the Owner
    and irreversible operations always escalate, even with a grant.
    """
    clean_category = str(category or "").strip().lower()
    clean_project = str(project or "").strip()
    clean_operation = str(operation or "").strip().lower()
    if clean_operation and (
            delegation_mod.is_message_operation(clean_operation)
            or delegation_mod.is_irreversible_operation(clean_operation)):
        return {
            "escalate": True,
            "delegated": False,
            "category": clean_category or "external",
            "project": clean_project,
            "reason": (
                "messages on behalf of the owner and irreversible "
                "actions always confirm"),
            "human_action": (
                f"Autoriza la accion {clean_operation} de "
                f"{clean_category or 'external'} "
                f"en {clean_project or 'este proyecto'} "
                "(siempre confirma)."),
        }
    if delegation_mod.is_delegated(
            project=clean_project, category=clean_category):
        return {
            "escalate": False,
            "delegated": True,
            "category": clean_category,
            "project": clean_project,
            "reason": "owner delegation covers this category",
            "human_action": None,
        }
    return {
        "escalate": True,
        "delegated": False,
        "category": clean_category or "external",
        "project": clean_project,
        "reason": "no owner delegation covers this category",
        "human_action": (
            f"Autoriza la accion externa de {clean_category or 'external'} "
            f"en {clean_project or 'este proyecto'} "
            "(o delega la categoria con evidencia)."
        ),
    }
