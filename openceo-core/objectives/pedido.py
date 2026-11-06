"""Pedido detection: loose work with no decided agent (TASK-2026-00204).

A pedido is a loose ticket (no ``objective_id``) carrying no explicit
decision. Explicit means decided by someone the rumbo trusts — the
Owner or a validated plan — expressed in the ticket fields, and it
only passes deterministic validation against the catalog: the planner
never runs for it. Approved playbooks (TASK-2026-00217) guide the
planner instead of bypassing it: the planner follows them by default
from its prompt index.

Explicit channels (any one suffices):

- top-level ``coding_tool`` (every tier value is dev executors);
- top-level ``agent_hint``, or ``agent`` naming a real agent (Owner
  ``@``-prefix, planner children, validated flows). The persisted
  ``agent="ceo"`` default is undecided, not a decision;
- ``params.target_agent`` (planner target backup, approved tools);
- ``objective_id`` (objective children arrive planner-decided);
- an explicit YAML/workflow target (``params.workflow`` family or
  ``workflow_phases``): a validated plan or an approval flow in
  flight, which planning must not override. The retired discovery
  flags (``discover_workflow``, ``workflow_discovery``) decide
  nothing since TASK-2026-00215.

Excluded even when undecided: owner questions (``params`` carries an
``owner_question`` block): human-channel tickets the planner must
never patch.

A lone ``params.skill`` names no agent and decides nothing: the ticket
stays a pedido and the planner confirms skill and agent together.

Dependency-free (stdlib only): sessions, the graph, the dispatcher and
the funnel import this without cycles.
"""

from __future__ import annotations

from typing import Any, Mapping

from mechanic.handoff import agent_handoff

#: Params keys whose presence marks a validated-plan target: planning
#: must not override them.
_EXPLICIT_WORKFLOW_PARAMS = (
    "workflow",
    "workflow_id",
    "workflow_ref",
    "use_workflow",
)


def _nonblank(value: Any) -> bool:
    return bool(str(value or "").strip())


def _agent_field_names(ticket: Mapping[str, Any]) -> bool:
    """True when top-level routing names a real agent.

    ``agent="ceo"`` is the persisted default for undecided work, not a
    decision: only other non-blank names count.
    """
    if _nonblank(ticket.get("agent_hint")):
        return True
    agent = str(ticket.get("agent") or "").strip().lower()
    return bool(agent) and agent != "ceo"


def is_pedido(ticket: Mapping[str, Any] | None) -> bool:
    """True when the ticket is undecided work for the single planner."""
    ticket = ticket or {}
    if _nonblank(ticket.get("objective_id")):
        return False
    if _nonblank(ticket.get("coding_tool")):
        return False
    if _agent_field_names(ticket):
        return False
    params = ticket.get("params")
    params = params if isinstance(params, dict) else {}
    if params.get("owner_question"):
        return False
    if _nonblank(params.get("target_agent")):
        return False
    if agent_handoff(ticket) is None:
        for key in _EXPLICIT_WORKFLOW_PARAMS:
            value = params.get(key)
            if isinstance(value, bool):
                if value:
                    return False
            elif _nonblank(value):
                return False
    phases = ticket.get("workflow_phases")
    if isinstance(phases, list) and phases:
        return False
    return True


def is_explicit_dev(ticket: Mapping[str, Any] | None) -> bool:
    """True when the ticket explicitly names dev without a tier.

    Explicit dev without a tier runs the safe C default (policy §25):
    specified work with no S/A/B justification. Only the planner ever
    assigns S/A/B or picks agents; this default assigns neither.
    """
    ticket = ticket or {}
    if _nonblank(ticket.get("coding_tool")):
        return False
    if _nonblank(ticket.get("objective_id")):
        return False
    params = ticket.get("params")
    params = params if isinstance(params, dict) else {}
    names = (
        str(ticket.get("agent_hint") or "").strip().lower(),
        str(ticket.get("agent") or "").strip().lower(),
        str(params.get("target_agent") or "").strip().lower(),
    )
    return "dev" in names
