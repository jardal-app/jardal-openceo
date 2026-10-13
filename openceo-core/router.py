"""Explicit agent vocabulary (TASK-2026-00204).

The single planner decides every pedido; an explicit request only
resolves against this vocabulary and is then validated against the
catalog by the graph agent gate. No keyword, config-trigger,
requirement-heuristic or LLM classification lives here anymore: the
router that guessed agents from ticket text was retired with the
single planner, together with the project ``routing:`` config block
that fed it.
"""

from __future__ import annotations

from typing import Any

from state import AgentName


def resolve_explicit_name(
    agent_hint: Any,
    coding_tool: Any,
) -> AgentName | None:
    """Resolve an explicit routing decision. Pure, reads no ticket text.

    A set ``coding_tool`` is a persisted tier decision: every tier
    value is a dev executor, so it forces DEV before any name lookup
    (without this, Tier S tickets misroute and "complete" without
    ever reaching dev — F0 pilot 00097). Otherwise the hint resolves
    through :data:`CEORouter.AGENT_ALIASES`. Unknown or absent names
    resolve to None: the caller waits fail-closed, never guesses.
    """
    if coding_tool is not None and str(coding_tool).strip():
        return AgentName.DEV
    if agent_hint is None:
        return None
    return CEORouter.AGENT_ALIASES.get(str(agent_hint).strip().lower())


class CEORouter:
    """Explicit agent vocabulary holder.

    Kept as a class because the catalog back-check (R6, TASK-2026-00195)
    reads ``AGENT_ALIASES`` from it and call sites hold an instance;
    it decides nothing and holds no LLM client.
    """

    AGENT_ALIASES = {
        "dev": AgentName.DEV,
        "developer": AgentName.DEV,
        "devops": AgentName.DEV,
        "finance": AgentName.FINANCE,
        "secretary": AgentName.SECRETARY,
        "operations": AgentName.OPERATIONS,
        "research": AgentName.RESEARCH,
    }

    def __init__(self, settings: Any = None) -> None:
        del settings

    @staticmethod
    def resolve_explicit(
        agent_hint: Any,
        coding_tool: Any,
    ) -> AgentName | None:
        """Resolve an explicit routing decision (see :func:`resolve_explicit_name`)."""
        return resolve_explicit_name(agent_hint, coding_tool)
