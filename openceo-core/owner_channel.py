"""Single human channel: owner-question tickets (TASK-2026-00206).

Pure builders and predicates; :class:`TaskService` persists them
ERPNext-first. Everything the Owner must see arrives as one waiting
ticket in ``Pending Review`` with the exact human action, and the
Owner's answer resumes the origin ticket from its checkpoint.

Kinds (epic rumbo point 10): ``question`` (only the Owner can answer
from their own data, including Tier S questions), ``lock`` (a boundary
tool checkpoint, producers in TASK-2026-00208), ``new_tool`` (a missing
boundary tool the planner proposes), ``playbook`` (reserved: proposed
on objective close, TASK-2026-00217). Technical needs never become
questions: they return to the planner as agent verdicts.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "OWNER_QUESTION_PHASE",
    "OWNER_QUESTION_TAG",
    "OWNER_KINDS",
    "OwnerChannelError",
    "build_owner_question",
    "build_tool_ticket",
    "denied_origin_verdict",
    "is_owner_question",
    "owner_answer_record",
    "owner_question_block",
    "question_answered",
    "tool_proposal_source_id",
]

#: Execution phase of owner-question tickets (tray + cockpit read it).
OWNER_QUESTION_PHASE = "owner_question"

#: Business tag of owner-question tickets.
OWNER_QUESTION_TAG = "pregunta-dueno"

#: Kinds allowed through the channel. Anything else is rejected: technical
#: waits (fallen reviewers, broken tests) return to the planner instead.
OWNER_KINDS: tuple[str, ...] = ("question", "lock", "new_tool", "playbook")

_KIND_TITLES = {
    "question": "Pregunta",
    "lock": "Candado",
    "new_tool": "Tool nueva",
    "playbook": "Playbook",
}

#: ERPNext Task subject fits 140 chars.
_TITLE_MAX = 140


class OwnerChannelError(ValueError):
    """The question does not pass the human-channel filter."""


def build_owner_question(
    *,
    kind: str,
    question: str,
    human_action: str,
    origin_task_id: str | None = None,
    origin_title: str | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    context: str | None = None,
    tool: Mapping[str, Any] | None = None,
    project: str | None = None,
    source_id: str | None = None,
    priority: str = "high",
) -> dict[str, Any]:
    """Spec for an owner-question ticket. Pure: no I/O, no tokens.

    Raises OwnerChannelError when the kind is not allowed through the
    channel or the question/human action is blank.
    """
    clean_kind = str(kind or "").strip()
    if clean_kind not in OWNER_KINDS:
        raise OwnerChannelError(
            f"kind {clean_kind!r} is not for the Owner "
            f"(allowed: {', '.join(OWNER_KINDS)}); "
            "technical needs return to the planner"
        )
    clean_question = str(question or "").strip()
    if not clean_question:
        raise OwnerChannelError("an owner question needs the question text")
    clean_action = str(human_action or "").strip()
    if not clean_action:
        raise OwnerChannelError("an owner question needs the exact human action")
    origin = str(origin_task_id or "").strip() or None
    title = f"[{_KIND_TITLES[clean_kind]}] {clean_question}"[:_TITLE_MAX]
    lines = [
        f"Pregunta al dueno ({clean_kind}).",
        "",
        f"Pregunta: {clean_question}",
        f"Accion humana: {clean_action}",
    ]
    if origin:
        origin_line = f"Ticket de origen: {origin}"
        if str(origin_title or "").strip():
            origin_line += f" ({str(origin_title).strip()})"
        lines.extend(["", origin_line])
    if isinstance(checkpoint, Mapping) and checkpoint:
        rendered = "; ".join(
            f"{key}={checkpoint[key]}" for key in sorted(checkpoint, key=str)
        )
        lines.extend(["", f"Punto de control: {rendered}"])
    clean_context = str(context or "").strip()
    if clean_context:
        lines.extend(["", f"Contexto: {clean_context}"])
    return {
        "title": title,
        "description": "\n".join(lines),
        "tags": [OWNER_QUESTION_TAG, clean_kind],
        "phase": OWNER_QUESTION_PHASE,
        "waiting_reason": f"{OWNER_QUESTION_PHASE}:{clean_kind}",
        "params": {
            "owner_question": {
                "kind": clean_kind,
                "question": clean_question,
                "human_action": clean_action,
                "origin_task_id": origin,
                "origin_title": str(origin_title or "").strip() or None,
                "checkpoint": dict(checkpoint)
                if isinstance(checkpoint, Mapping) and checkpoint
                else None,
                "tool": dict(tool)
                if isinstance(tool, Mapping) and tool
                else None,
            }
        },
        "source": "api",
        "source_id": str(source_id or "").strip() or None,
        "project": project,
        "priority": priority,
        "autorun": False,
    }


def owner_question_block(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``params.owner_question`` block, or {} when absent/misshaped."""
    if not isinstance(task, Mapping):
        return {}
    params = task.get("params")
    if not isinstance(params, Mapping):
        return {}
    block = params.get("owner_question")
    return dict(block) if isinstance(block, Mapping) else {}


def is_owner_question(task: Mapping[str, Any] | None) -> bool:
    """Structural check: phase plus a channel kind in params."""
    if not isinstance(task, Mapping):
        return False
    if str(task.get("phase") or "") != OWNER_QUESTION_PHASE:
        return False
    return str(owner_question_block(task).get("kind") or "") in OWNER_KINDS


def question_answered(task: Mapping[str, Any] | None) -> bool:
    """True once the Owner's decision is recorded on the question."""
    return "decision" in owner_question_block(task)


def owner_answer_record(
    *,
    approve: bool,
    actor_id: str,
    comment: str,
    decided_at: str,
) -> dict[str, Any]:
    """Decision record stored on the question (and copied to the origin)."""
    return {
        "decision": "approved" if approve else "denied",
        "by": str(actor_id or "").strip() or "owner",
        "at": str(decided_at or ""),
        "comment": " ".join(str(comment or "").split())[:2000],
    }


def denied_origin_verdict(*, kind: str, owner_text: str) -> dict[str, Any]:
    """``bloqueado`` agent verdict for a denied (or tool-waiting) origin.

    Plain dict matching the ``agent_result`` schema (resultado + motivo);
    objective children return to the planner through it, loose tickets
    keep it as the recorded verdict.
    """
    text = " ".join(str(owner_text or "").split())[:500]
    return {
        "resultado": "bloqueado",
        "motivo": text or f"denegado por el dueno ({kind})",
        "necesidad": "",
    }


def build_tool_ticket(
    *,
    name: str,
    need: str,
    spec_text: str,
    question_task_id: str,
    origin_task_id: str | None,
    project: str | None,
) -> dict[str, Any]:
    """Spec for the dev ticket an approved ``new_tool`` question creates."""
    clean_name = str(name or "").strip()
    lines = [
        f"Tool de frontera aprobada por el dueno: {clean_name}.",
        "",
        f"Necesidad: {str(need or '').strip()}",
        "",
        str(spec_text or "").strip(),
        "",
        f"Pregunta del Owner: {question_task_id}",
    ]
    if str(origin_task_id or "").strip():
        lines.append(f"Ticket de origen: {origin_task_id}")
    return {
        "title": f"[Tool de frontera] {clean_name}"[:_TITLE_MAX],
        "description": "\n".join(lines),
        "params": {
            "target_agent": "dev",
            "proposed_tool": {
                "name": clean_name,
                "need": str(need or "").strip(),
                "question_task_id": str(question_task_id or ""),
                "origin_task_id": str(origin_task_id or "").strip() or None,
            },
        },
        "source": "api",
        "source_id": f"tool-approved:{question_task_id}",
        "project": project,
        "priority": "high",
    }


def tool_proposal_source_id(objective_id: str, name: str) -> str:
    """Stable idempotency key for a planner tool proposal."""
    return f"tool-proposal:{objective_id}:{str(name or '').strip()}"
