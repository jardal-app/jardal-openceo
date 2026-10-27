"""Company missions over objectives (TASK-2026-00218).

A mission is one Jardal ERP group Task (business identity) whose children
are objective groups, plus a Mongo document (execution state). The CEO
strategy chains objectives until the acceptance is met, then closes
the mission with evidence. Creation is deterministic; the semantic
parts (criteria proposal, next-pedido need, completion judgement)
belong to the CEO review, and the Owner only sees the acceptance
proposal through the single human channel.
"""

from __future__ import annotations

from business.links import business_mirror

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

__all__ = [
    "MISSION_ACCEPTANCE_DEADLINE_HOURS",
    "MISSION_ENTRIES",
    "MISSION_SHELL_ORIGIN",
    "MISSION_TAG",
    "MissionEnv",
    "MissionErpError",
    "MissionRejected",
    "MissionDuplicate",
    "MissionStore",
    "InMemoryMissionStore",
    "MongoMissionStore",
    "acceptance_question_marker",
    "acceptance_question_spec",
    "adopt_proposal",
    "apply_acceptance_answer",
    "close_mission",
    "create_mission",
    "deadline_confirmation_patch",
    "find_mission_duplicate",
    "mission_acceptance_deadline",
    "mission_question_expired",
    "new_mission_id",
    "normalize_mission_goal",
    "record_objective_closed",
    "record_objective_linked",
]

#: Valid ``entry`` values on the mission document.
MISSION_ENTRIES = frozenset({"top_down", "horizon_promotion"})

#: Origin marker of the parent ticket minted for a mission.
MISSION_SHELL_ORIGIN = "mission_shell"

#: Business tag of mission shells.
MISSION_TAG = "mision"

#: The Owner answers the proposed criteria within this long, or the
#: proposal stands. Reabrible: no Owner value was given (00218).
MISSION_ACCEPTANCE_DEADLINE_HOURS = 48

#: Objective statuses that end the ledger entry (read live, not trusted).
MISSION_TERMINAL_OBJECTIVE = frozenset({"closed", "cancelled"})


class MissionRejected(Exception):
    """Deterministic refusal: bad shape, unknown project, duplicate."""


class MissionDuplicate(MissionRejected):
    """An active mission already covers the goal in the project."""

    def __init__(self, message: str, existing_id: str = "") -> None:
        super().__init__(message)
        self.existing_id = existing_id


class MissionErpError(RuntimeError):
    """Business identity is unavailable (fail closed, never orphan)."""


class MissionStore(Protocol):
    async def create_mission(self, doc: dict[str, Any]) -> dict[str, Any]: ...
    async def get_mission(self, mission_id: str) -> dict[str, Any] | None: ...
    async def patch_mission(
        self, mission_id: str, updates: dict[str, Any]) -> dict[str, Any] | None: ...
    async def list_active_missions(
        self, project: str | None = None) -> list[dict[str, Any]]: ...


class InMemoryMissionStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    async def create_mission(self, doc: dict[str, Any]) -> dict[str, Any]:
        stored = dict(doc)
        self.docs[stored["mission_id"]] = stored
        return dict(stored)

    async def get_mission(self, mission_id: str) -> dict[str, Any] | None:
        doc = self.docs.get(mission_id)
        return dict(doc) if doc is not None else None

    async def patch_mission(
        self, mission_id: str, updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        doc = self.docs.get(mission_id)
        if doc is None:
            return None
        doc.update(updates)
        return dict(doc)

    async def list_active_missions(
        self, project: str | None = None,
    ) -> list[dict[str, Any]]:
        return [dict(doc) for doc in self.docs.values()
                if doc.get("status") == "active"
                and (not project or doc.get("project") == project)]


class MongoMissionStore:
    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def create_mission(self, doc: dict[str, Any]) -> dict[str, Any]:
        return await self._repo.create_mission(doc)

    async def get_mission(self, mission_id: str) -> dict[str, Any] | None:
        return await self._repo.get_mission(mission_id)

    async def patch_mission(
        self, mission_id: str, updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        return await self._repo.patch_mission(mission_id, updates)

    async def list_active_missions(
        self, project: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._repo.list_active_missions(project)


@dataclass
class MissionEnv:
    """Everything the mission flows need (wired by the service)."""

    missions: MissionStore
    objectives: Any
    tasks: Any
    create_ticket: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
    patch_task: Callable[[str, dict[str, Any]], Awaitable[None]]
    notify: Callable[[str, str], Awaitable[None]] | None = None
    ceo_wake: Callable[[str], None] | None = None
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(timezone.utc))


def new_mission_id() -> str:
    return f"MIS-{uuid.uuid4().hex[:8].upper()}"


def normalize_mission_goal(goal: str) -> str:
    """Canonical goal text for the deterministic duplicate check."""
    return " ".join(str(goal or "").split()).strip().lower()


def find_mission_duplicate(
    candidates: Sequence[Mapping[str, Any]],
    goal: str,
) -> Mapping[str, Any] | None:
    """Active mission with the same normalized goal, if any."""
    want = normalize_mission_goal(goal)
    if not want:
        return None
    for candidate in candidates:
        if normalize_mission_goal(candidate.get("goal", "")) == want:
            return candidate
    return None


def _utcnow_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def mission_acceptance_deadline(
    proposed_at: datetime | None = None,
) -> str:
    """ISO deadline of an acceptance proposal (48h, reabrible)."""
    base = proposed_at or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(hours=MISSION_ACCEPTANCE_DEADLINE_HOURS)
            ).isoformat()


def _new_mission_doc(
    *,
    mission_id: str,
    project: str,
    entry: str,
    goal: str,
    acceptance: list[str],
    acceptance_source: str,
    source_task_id: str,
    erp_group_ref: str | None,
    requester: str | None,
) -> dict[str, Any]:
    return {
        "mission_id": mission_id,
        "project": project,
        "entry": entry,
        "goal": goal,
        "acceptance": acceptance,
        "acceptance_source": acceptance_source,
        "acceptance_status": "pending" if not acceptance else "set",
        "acceptance_disputed": False,
        "question_task_id": None,
        "question_deadline": None,
        "erp_group_ref": erp_group_ref,
        "status": "active",
        "objectives": [],
        "conversion_source_task_id": source_task_id,
        "requester": requester,
        "delivery": None,
    }


async def create_mission(
    env: MissionEnv,
    *,
    project: str,
    goal: str,
    acceptance: Sequence[str] | None = None,
    entry: str = "top_down",
    source: str = "api",
    source_id: str | None = None,
    requester: str | None = None,
) -> dict[str, Any]:
    """Create a mission: shell group ticket + Mongo doc + CEO wake.

    Mirrors the objective top-down entry: deterministic admission
    (duplicates; no numeric cap in v1), the shell ticket is the ERP
    group (``is_group`` through the common creation, ERP-first) and
    moves to ``converted`` at birth so it never executes. Without
    Owner acceptance the mission starts criteria-pending: the CEO
    proposes in its first mission pedido and the conversion adopts
    the proposal and asks the Owner (non-blocking, 48h deadline).
    Creation always wakes the CEO: nothing chains objective 1
    otherwise. Raises :class:`MissionRejected` when refused,
    :class:`MissionErpError` when business identity fails.
    """
    project = str(project or "").strip()
    if not project:
        raise MissionRejected("a mission needs exactly one project")
    goal = str(goal or "").strip()
    if not goal:
        raise MissionRejected("a mission needs a goal")
    if entry not in MISSION_ENTRIES:
        raise MissionRejected(f"unknown entry {entry!r}")
    siblings = await env.missions.list_active_missions(project)
    dupe = find_mission_duplicate(siblings, goal)
    if dupe is not None:
        raise MissionDuplicate(
            f"active mission {dupe.get('mission_id')} already covers "
            f"this goal in {project}",
            existing_id=str(dupe.get("mission_id") or ""))

    mission_id = new_mission_id()
    criteria = [str(item).strip() for item in (acceptance or [])
                if str(item).strip()]
    shell = await env.create_ticket({
        "title": f"[Misión] {goal[:120]}",
        "description": "\n".join([
            f"GOAL: {goal}",
            "",
            "ACCEPTANCE (mission criteria):",
            *([f"- {item}" for item in criteria]
              or ["- (the CEO proposes them in its first pedido)"]),
            "",
            f"Entry: {entry}."
            + (f" Requested by {requester}." if requester else ""),
        ]),
        "acceptance": criteria,
        "project": project,
        "origin": MISSION_SHELL_ORIGIN,
        "autorun": False,
        "is_group": True,
        "source": source,
        "source_id": source_id or f"mis-shell:{mission_id}",
        "tags": [MISSION_TAG],
    })
    shell_id = str((shell or {}).get("task_id") or "")
    if not shell_id:
        raise MissionErpError(
            "the shell ticket was not registered; refusing a group-less "
            "mission")
    await env.patch_task(shell_id, {
        "status": "converted",
        "waiting_reason": None,
        "resume_at": None,
        "last_response_summary": (
            f"Shell of mission {mission_id}: the group Task carries "
            "the objective groups; this ticket never executes."),
    })
    group = _erp_group_name(shell)
    mission = await env.missions.create_mission(_new_mission_doc(
        mission_id=mission_id,
        project=project,
        entry=entry,
        goal=goal,
        acceptance=criteria,
        acceptance_source="owner" if criteria else "pending",
        source_task_id=shell_id,
        erp_group_ref=group or None,
        requester=requester,
    ))
    if env.notify is not None:
        await env.notify(
            shell_id,
            f"Mission {mission_id} created ({entry}) in {project}. "
            f"Goal: {goal[:200]}")
    if env.ceo_wake is not None:
        try:
            env.ceo_wake(
                f"Mission {mission_id} created in {project}: emit its "
                "first objective pedido"
                + (" and propose its acceptance criteria"
                   if not criteria else "")
                + "."
            )
        except Exception:
            pass
    return mission


def _erp_group_name(ticket: Mapping[str, Any] | None) -> str:
    """Business ``TASK-…`` name from the ticket's business link."""
    if not isinstance(ticket, Mapping):
        return ""
    params = ticket.get("params")
    mirrors = params.get("_mirrors") if isinstance(params, Mapping) else None
    mirror = business_mirror(mirrors)
    if not isinstance(mirror, Mapping):
        return ""
    return str(mirror.get("name") or "").strip()


async def record_objective_linked(
    missions: MissionStore, mission_id: str, objective_id: str,
) -> dict[str, Any] | None:
    """Append an objective to the mission ledger (None without doc)."""
    mission = await missions.get_mission(mission_id)
    if mission is None:
        return None
    ledger = [dict(item) for item in mission.get("objectives") or []]
    if any(str(item.get("objective_id") or "") == objective_id
           for item in ledger):
        return mission
    ledger.append({"objective_id": objective_id, "status": "active"})
    return await missions.patch_mission(
        mission_id, {"objectives": ledger})


async def record_objective_closed(
    missions: MissionStore, mission_id: str, objective_id: str,
) -> dict[str, Any] | None:
    """Mark an objective closed in the ledger (None without doc)."""
    mission = await missions.get_mission(mission_id)
    if mission is None:
        return None
    ledger = [dict(item) for item in mission.get("objectives") or []]
    known = False
    for item in ledger:
        if str(item.get("objective_id") or "") == objective_id:
            item["status"] = "closed"
            known = True
    if not known:
        ledger.append({"objective_id": objective_id, "status": "closed"})
    return await missions.patch_mission(
        mission_id, {"objectives": ledger})


def adopt_proposal(
    mission: Mapping[str, Any], proposed: Sequence[str],
) -> dict[str, Any] | None:
    """Patch adopting the CEO proposal, or None when nothing applies.

    Pure: only a criteria-pending mission without a question adopts,
    and only a non-blank proposal. The caller opens the question.
    """
    if str(mission.get("status") or "") != "active":
        return None
    if not isinstance(mission.get("acceptance"), list) or mission.get(
            "acceptance"):
        return None
    if mission.get("question_task_id"):
        return None
    criteria = [str(item).strip() for item in proposed
                if str(item).strip()]
    if not criteria:
        return None
    return {
        "acceptance": criteria,
        "acceptance_source": "proposal",
        "acceptance_status": "proposed",
    }


def acceptance_question_spec(
    mission: Mapping[str, Any],
    *,
    proposed_at: datetime | None = None,
) -> dict[str, Any]:
    """Owner-question spec for the proposed criteria (pure, no I/O).

    Raises :class:`MissionRejected` without a proposal to ask about.
    The question never blocks: the mission proceeds with the proposal
    and the deadline only confirms it by silence.
    """
    from owner_channel import build_owner_question

    mission_id = str(mission.get("mission_id") or "").strip()
    proposed = [str(item).strip() for item in mission.get("acceptance") or []
                if str(item).strip()]
    if not mission_id or not proposed:
        raise MissionRejected("no proposed criteria to ask about")
    deadline = mission_acceptance_deadline(proposed_at)
    criteria = "\n".join(f"- {item}" for item in proposed)
    shell_id = str(mission.get("conversion_source_task_id") or "").strip()
    return {
        **build_owner_question(
            kind="question",
            question=(
                f"¿Con estos criterios juzgo la misión {mission_id}?"
            ),
            human_action=(
                "Confirma los criterios propuestos, ajustalos en tu "
                "respuesta, o dime «analízalo tú» para que el CEO siga "
                f"con su propuesta (plazo {deadline})."
            ),
            origin_task_id=shell_id or None,
            origin_title=f"[Misión] {str(mission.get('goal') or '')[:80]}",
            checkpoint={
                "mission_id": mission_id,
                "proposed": proposed,
                "deadline": deadline,
            },
            context=(
                "Criterios propuestos por el CEO:\n"
                f"{criteria}\nLa misión sigue con la propuesta; tu "
                "respuesta la confirma o la ajusta."
            ),
            project=str(mission.get("project") or "") or None,
            source_id=f"mission-acceptance:{mission_id}",
        ),
    }


def acceptance_question_marker(
    question: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The mission checkpoint, or None when this is another question."""
    from owner_channel import owner_question_block

    block = owner_question_block(question)
    if str(block.get("kind") or "") != "question":
        return None
    checkpoint = block.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return None
    if not str(checkpoint.get("mission_id") or "").strip():
        return None
    if not str(checkpoint.get("deadline") or "").strip():
        return None
    return dict(checkpoint)


def mission_question_expired(
    question: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> bool:
    """True when an unanswered mission question passed its deadline."""
    from owner_channel import question_answered

    marker = acceptance_question_marker(question)
    if marker is None or question_answered(question):
        return False
    try:
        deadline = datetime.fromisoformat(str(marker["deadline"]))
    except ValueError:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment >= deadline


def deadline_confirmation_patch() -> dict[str, Any]:
    """Patch confirming the proposal after a silent deadline (pure)."""
    return {
        "acceptance_status": "confirmed",
        "acceptance_note": (
            "Plazo vencido sin respuesta del Owner; la propuesta del "
            "CEO queda confirmada."
        ),
    }


def _looks_like_you_decide(text: str) -> bool:
    folded = " ".join(str(text or "").lower().split())
    compact = (folded.replace("á", "a").replace("é", "e")
               .replace("í", "i").replace("ó", "o").replace("ú", "u"))
    return "analizalo tu" in compact.replace("  ", " ")


def apply_acceptance_answer(
    mission: Mapping[str, Any],
    *,
    approve: bool,
    comment: str,
) -> dict[str, Any]:
    """Patch recording the Owner's answer on the mission (pure).

    Text wins over the button: criteria lines are adopted, and
    «analízalo tú» confirms the proposal. An empty deny keeps the
    proposal working but disputed and visible (no re-ask loop in v1).
    """
    text = " ".join(str(comment or "").split())
    if _looks_like_you_decide(text):
        return {
            "acceptance_status": "confirmed",
            "acceptance_note": "El Owner delego en el CEO («analizalo tu»).",
        }
    lines = [line.strip() for line in str(comment or "").splitlines()
             if line.strip()]
    if lines:
        return {
            "acceptance": lines,
            "acceptance_source": "owner",
            "acceptance_status": "confirmed",
            "acceptance_note": (
                "Criterios del Owner desde su respuesta."
                if approve else
                "El Owner denego la propuesta y dio estos criterios."
            ),
        }
    if approve:
        return {
            "acceptance_status": "confirmed",
            "acceptance_note": "El Owner confirmo la propuesta.",
        }
    return {
        "acceptance_disputed": True,
        "acceptance_note": (
            "El Owner denego la propuesta sin alternativa; la propuesta "
            "sigue vigente y visible hasta que de criterios."
        ),
    }


async def close_mission(
    env: MissionEnv,
    mission_id: str,
    *,
    reason: str = "",
) -> dict[str, Any]:
    """Close the mission: delivery record with evidence, shell completes.

    The caller (CEO review) judged the acceptance met; the delivery
    cites every mission objective and its own delivery record. The
    shell completes through the standard patch (business push through
    the queue, never direct). Raises :class:`MissionRejected` without
    the mission or outside active.
    """
    mission = await env.missions.get_mission(mission_id)
    if mission is None:
        raise MissionRejected(f"unknown mission {mission_id!r}")
    if str(mission.get("status") or "") != "active":
        raise MissionRejected(
            f"mission {mission_id} is not active "
            f"({mission.get('status')})")
    ledger = [dict(item) for item in mission.get("objectives") or []]
    evidence: list[dict[str, Any]] = []
    for item in ledger:
        objective_id = str(item.get("objective_id") or "")
        objective = await env.objectives.get_objective(objective_id)
        if objective is None:
            evidence.append({"objective_id": objective_id,
                             "status": "unknown"})
            continue
        evidence.append({
            "objective_id": objective_id,
            "status": str(objective.get("status") or ""),
            "delivery": objective.get("delivery"),
        })
    closed_ids = [str(item.get("objective_id") or "") for item in ledger]
    await env.missions.patch_mission(mission_id, {
        "status": "closed",
        "closed_at": _utcnow_iso(env.now()),
        "delivery": {
            "acceptance": list(mission.get("acceptance") or []),
            "acceptance_source": mission.get("acceptance_source"),
            "objectives": closed_ids,
            "evidence": evidence,
            "reason": str(reason or "").strip()[:500],
        },
    })
    shell_id = str(mission.get("conversion_source_task_id") or "")
    if shell_id:
        shell = await env.tasks.get_task(shell_id)
        if shell is not None and str(shell.get("status") or "") in {
                "converted", "completed"}:
            await env.patch_task(shell_id, {
                "status": "completed",
                "waiting_reason": None,
                "resume_at": None,
                "completed_at": _utcnow_iso(env.now()),
                "last_response_summary": (
                    f"Mission {mission_id} closed: acceptance met."),
            })
    if env.notify is not None:
        await env.notify(
            shell_id or mission_id,
            f"Mission {mission_id} closed: {len(evidence)} objectives, "
            "acceptance met with evidence.")
    return {"ok": True, "action": "closed", "mission_id": mission_id}
