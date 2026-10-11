"""Declared spend vs the company mandate (TASK-2026-00207).

Pure checks, no I/O, no tokens. The planner declares costs per ticket or
objective (``PlannerResult``/``PlannedTicket.costs``) and explicit requests
declare the same shape in ``params.costs``; before anything persists or
launches, the engine and ticket creation call :func:`check_plan_spend`,
which asks ``requires_owner``. Unknown cost (``amount`` None) escalates:
it is never treated as zero.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

__all__ = [
    "SPEND_WAITING_REASON",
    "PlanSpendError",
    "check_plan_spend",
    "collect_plan_costs",
    "loose_spend_source_id",
    "parse_costs",
    "spend_guide_section",
    "spend_hold_checkpoint",
    "spend_hold_objective",
    "spend_total_text",
]

#: Operational wait of a loose ticket held for spend approval: the
#: question carries the human action, the origin keeps no business push.
SPEND_WAITING_REASON = "spend_approval"


class PlanSpendError(ValueError):
    """Declared costs are malformed (fail-closed: fix the declaration)."""


def parse_costs(entries: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Sum declared costs. Pure.

    ``entries`` is ``[{label, amount}]`` with the amount a decimal
    string, an int, or None when unknown. Returns ``{total: Decimal,
    unknown: [labels], details: {label: amount-or-None}}``.
    """
    total = Decimal("0")
    unknown: list[str] = []
    details: dict[str, str | None] = {}
    for index, cost in enumerate(entries or []):
        if not isinstance(cost, Mapping):
            raise PlanSpendError(f"costs[{index}] must be an object")
        label = str(cost.get("label") or "").strip()
        if not label:
            raise PlanSpendError(f"costs[{index}].label must not be blank")
        raw = cost.get("amount")
        if raw is None:
            unknown.append(label)
            details[label] = None
            continue
        if isinstance(raw, bool) or isinstance(raw, float):
            raise PlanSpendError(
                f"costs[{index}].amount must be a decimal string or int, "
                f"got {type(raw).__name__}"
            )
        try:
            amount = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, AttributeError) as exc:
            raise PlanSpendError(
                f"costs[{index}].amount is not a number: {raw!r}"
            ) from exc
        if amount < 0:
            raise PlanSpendError(
                f"costs[{index}].amount must not be negative")
        total += amount
        details[label] = str(amount)
    return {"total": total, "unknown": unknown, "details": details}


def collect_plan_costs(result: Any) -> list[dict[str, Any]]:
    """Objective-level plus every ticket's costs, tagged by origin."""
    entries: list[dict[str, Any]] = []
    for cost in list(getattr(result, "costs", None) or []):
        entries.append({
            "label": f"objective: {cost.label}",
            "amount": cost.amount,
        })
    for ticket in list(getattr(result, "tickets", None) or []):
        for cost in list(getattr(ticket, "costs", None) or []):
            entries.append({
                "label": f"{ticket.local_ref}: {cost.label}",
                "amount": cost.amount,
            })
    return entries


def check_plan_spend(
    *,
    costs: Sequence[Mapping[str, Any]] | None,
    summary: str,
    mandate: Any | None,
) -> dict[str, Any]:
    """Decide whether declared spend needs the Owner. Pure.

    With no costs declared the work launches without even reading the
    mandate. Otherwise ``requires_owner`` decides; an unknown amount
    escalates fail-closed, and without a valid mandate any declared
    spend escalates too. Returns the ``check_spend`` shape plus the
    ``unknown`` labels.
    """
    from company_mandate import EscalationCategory, OwnerAction, requires_owner

    if not costs:
        return {
            "escalate": False,
            "category": None,
            "reason": "no declared spend",
            "human_action": None,
            "amount": "0",
            "currency": _mandate_currency(mandate),
            "unknown": [],
        }
    parsed = parse_costs(costs)
    total: Decimal = parsed["total"]
    unknown: list[str] = parsed["unknown"]
    action = OwnerAction(
        kind="plan-spend",
        summary=str(summary or "Launch work with declared spend").strip(),
        amount=total,
        details=dict(parsed["details"]),
    )
    decision = requires_owner(action, mandate)
    if unknown:
        names = ", ".join(unknown)
        return {
            "escalate": True,
            "category": EscalationCategory.NEW_SPEND.value,
            "reason": f"unknown cost (never zero): {names}",
            "human_action": (
                f"Confirma los montos desconocidos ({names}) o deniega: "
                f"{action.summary}."
            ),
            "amount": str(total),
            "currency": _mandate_currency(mandate),
            "unknown": unknown,
        }
    return {
        "escalate": bool(decision.escalate),
        "category": decision.category.value if decision.category else None,
        "reason": decision.reason,
        "human_action": decision.human_action,
        "amount": str(total),
        "currency": _mandate_currency(mandate),
        "unknown": [],
    }


def _mandate_currency(mandate: Any | None) -> str:
    if mandate is None:
        return "?"
    try:
        return str(mandate.presupuesto.moneda)
    except AttributeError:
        return "?"


def spend_guide_section(mandate: Any | None) -> str:
    """Mandate spend context for the planner prompt (one definition)."""
    if mandate is None:
        return "\n".join([
            "SPEND (company mandate unavailable): declare every new cost "
            "in `costs` anyway; any declared spend waits for the Owner "
            "before launch.",
        ])
    try:
        currency = str(mandate.presupuesto.moneda)
        threshold = str(mandate.presupuesto.gasto_nuevo_sin_preguntar)
    except AttributeError:
        return spend_guide_section(None)
    return "\n".join([
        f"SPEND (company mandate): currency {currency}, spend above "
        f"{threshold} needs Owner approval BEFORE launch. Declare every "
        "new cost the work needs in `costs`.",
    ])


def loose_spend_source_id(task_id: str) -> str:
    """Stable idempotency key for a loose ticket's spend question."""
    return f"spend-launch:{task_id}"


def spend_total_text(spend: Mapping[str, Any]) -> str:
    """Human total of a spend check: amount plus unknown labels."""
    amount = str(spend.get("amount") or "0")
    currency = str(spend.get("currency") or "?")
    total = f"{amount} {currency}".strip()
    unknown = [str(item) for item in spend.get("unknown") or []]
    if unknown:
        total += f" (desconocido: {', '.join(unknown)})"
    return total


def spend_hold_checkpoint(objective_id: str) -> dict[str, Any]:
    """Checkpoint linking a spend question to its held plan."""
    return {"objective_id": str(objective_id or ""),
            "spend_hold": True}


def spend_hold_objective(task: Mapping[str, Any] | None) -> str | None:
    """Held-plan objective of a spend question, else None. Pure."""
    from owner_channel import owner_question_block

    block = owner_question_block(task)
    if str(block.get("kind") or "") != "question":
        return None
    checkpoint = block.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    if checkpoint.get("spend_hold") is not True:
        return None
    objective_id = str(checkpoint.get("objective_id") or "").strip()
    return objective_id or None
