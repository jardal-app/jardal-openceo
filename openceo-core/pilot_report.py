"""Pilot trial report from execution records (TASK-2026-00234).

Builds the trial report a pilot ticket requires — tokens and time,
Owner interventions, failures and how they were handled, out-of-role
calls — from the execution records, per ticket, per objective and per
mission when one exists. Cost reuses TASK-2026-00212; unknown tokens
render as unknown, never 0 (executor usage is still unreported).

Run it from ``openceo-core``::

    ../.venv/bin/python pilot_report.py --ticket TASK-2026-00219
    ../.venv/bin/python pilot_report.py --ticket <task_id> --format json
"""

from __future__ import annotations

from business.links import business_mirror

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from objectives import ticket_cost as cost_mod

__all__ = [
    "MongoTrialReader",
    "TrialRecords",
    "build_trial_report",
    "main",
    "trial_report_data",
]

_MISSION_NOTE = "sin datos (la capa de misiones llega en TASK-2026-00218)"


@dataclass(frozen=True)
class TrialRecords:
    """Execution records behind one trial report (all optional)."""

    ticket_ref: str
    ticket: Mapping[str, Any] | None = None
    objective_id: str | None = None
    objective_tickets: Sequence[Mapping[str, Any]] = ()
    mission_id: str | None = None
    mission_tickets: Sequence[Mapping[str, Any]] = ()
    owner_questions: Sequence[Mapping[str, Any]] = ()
    denied_calls: Sequence[Mapping[str, Any]] = ()


def _cost_of(doc: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    cost = (doc or {}).get("cost")
    return cost if isinstance(cost, Mapping) else None


def _tokens_text(cost: Mapping[str, Any] | None) -> str:
    if not isinstance(cost, Mapping) or not cost.get("runs"):
        return "sin medir (sin corridas)"
    if cost.get("tokens_in") is None and cost.get("tokens_out") is None \
            and cost.get("tokens_cached") is None:
        runs = cost.get("runs")
        return (f"no medidos ({runs}/{runs} corridas sin usage de "
                "ejecutores)")
    text = (f"in={cost.get('tokens_in', '?')} "
            f"out={cost.get('tokens_out', '?')} "
            f"cached={cost.get('tokens_cached', '?')}")
    unknown = int(cost.get("tokens_unknown_runs") or 0)
    if unknown:
        text += f" (+{unknown} sin medir)"
    return text


_FAILED_STATES = ("failed", "escalated", "interrupted", "cancelled")
_RUNNING_STATES = ("queued", "in_progress", "waiting", "converted")


def _failure_lines(ticket: Mapping[str, Any] | None) -> list[str]:
    if ticket is None:
        return ["ticket no encontrado en ejecucion"]
    params = ticket.get("params")
    params = params if isinstance(params, dict) else {}
    verdict = params.get("agent_result")
    verdict = verdict if isinstance(verdict, dict) else {}
    status = ticket.get("status")
    lines = [f"estado: {status}"]
    bad = status in _FAILED_STATES
    if verdict:
        lines.append(f"veredicto: {verdict.get('resultado')}: "
                     f"{verdict.get('motivo')}")
        bad = bad or verdict.get("resultado") != "hecho"
    if params.get("returns_from"):
        lines.append(f"vuelta al planner desde: {params['returns_from']}")
        bad = True
    if ticket.get("waiting_reason"):
        lines.append(f"en espera: {ticket.get('waiting_reason')}")
    if ticket.get("error"):
        lines.append(f"error: {str(ticket.get('error'))[:200]}")
        bad = True
    attempts = params.get("planner_attempts")
    if attempts:
        lines.append(f"intentos del planner: {attempts}")
    if not bad:
        if status in _RUNNING_STATES:
            lines.append(f"sin fallos (en curso: {status})")
        else:
            lines.append("sin fallos registrados")
    return lines


def _duration_text(cost: Mapping[str, Any] | None) -> str:
    """Measured seconds, unknown as unknown, never a fake 0.0."""
    if not isinstance(cost, Mapping) or not cost.get("runs"):
        return "sin medir"
    runs = int(cost.get("runs") or 0)
    unknown = int(cost.get("duration_unknown_runs") or 0)
    seconds = float(cost.get("duration_s") or 0.0)
    if seconds == 0.0 and unknown >= runs and unknown > 0:
        return "sin medir"
    text = f"{seconds:.1f} s"
    if unknown:
        text += f" (+{unknown} sin medir)"
    return text


def _question_lines(question: Mapping[str, Any]) -> str:
    params = question.get("params")
    params = params if isinstance(params, dict) else {}
    block = params.get("owner_question")
    block = block if isinstance(block, dict) else {}
    kind = block.get("kind") or "?"
    text = str(block.get("question") or "")[:160]
    decision = block.get("decision")
    decision = decision if isinstance(decision, dict) else {}
    if decision:
        verdict = decision.get("verdict") or decision.get("action") or "?"
        by = decision.get("by") or decision.get("actor") or "?"
        return f"[{kind}] {text} -> {verdict} ({by})"
    return f"[{kind}] {text} -> pendiente"


def _ticket_row(doc: Mapping[str, Any]) -> dict[str, Any]:
    params = doc.get("params")
    params = params if isinstance(params, dict) else {}
    cost = _cost_of(doc)
    return {
        "ref": business_mirror(params.get("_mirrors")).get(
            "name") or doc.get("task_id"),
        "status": doc.get("status"),
        "agent": doc.get("selected_agent") or params.get("target_agent")
        or doc.get("agent_hint") or doc.get("agent"),
        "runs": (cost or {}).get("runs", 0),
        "tokens": _tokens_text(cost),
        "duration_s": round(float((cost or {}).get("duration_s") or 0.0), 1),
        "duration_text": _duration_text(cost),
    }


def trial_report_data(records: TrialRecords) -> dict[str, Any]:
    """JSON-able trial report: ticket, objective, mission sections."""
    ticket_cost = _cost_of(records.ticket)
    children = [dict(_ticket_row(doc)) for doc in records.objective_tickets]
    objective_cost = cost_mod.aggregate_objective_cost(
        [_cost_of(doc) for doc in records.objective_tickets])
    mission_cost = cost_mod.aggregate_objective_cost(
        [_cost_of(doc) for doc in records.mission_tickets])
    return {
        "ticket": {
            "ref": records.ticket_ref,
            "title": (records.ticket or {}).get("title"),
            "status": (records.ticket or {}).get("status"),
            "row": _ticket_row(records.ticket or {}),
            "tokens": _tokens_text(ticket_cost),
            "engines": list((ticket_cost or {}).get("engines") or []),
            "models": list((ticket_cost or {}).get("models") or []),
            "failures": _failure_lines(records.ticket),
        },
        "interventions": [_question_lines(question)
                          for question in records.owner_questions],
        "denied_calls": [{
            "tool": call.get("tool"),
            "agent": call.get("agent"),
            "reason": call.get("reason"),
            "at": str(call.get("ts") or ""),
        } for call in records.denied_calls],
        "objective": None if records.objective_id is None else {
            "id": records.objective_id,
            "tickets": len(children),
            "tokens": _tokens_text(objective_cost),
            "children": children,
        },
        "mission": None if records.mission_id is None else {
            "id": records.mission_id,
            "tickets": len(records.mission_tickets),
            "tokens": _tokens_text(mission_cost),
            "note": None if records.mission_tickets else _MISSION_NOTE,
        },
    }


def build_trial_report(records: TrialRecords) -> str:
    """Markdown trial report, ready for the pilot ticket."""
    data = trial_report_data(records)
    ticket = data["ticket"]
    lines = [
        f"# Informe de prueba - {records.ticket_ref}",
        "",
        f"- ticket: {ticket['title'] or '?'} "
        f"({ticket['row']['ref']}), estado {ticket['status']}",
        f"- costo: runs={ticket['row']['runs']}, "
        f"tokens {ticket['tokens']}, "
        f"duracion {ticket['row']['duration_text']}",
    ]
    if ticket["engines"]:
        lines.append(f"- motores: {', '.join(ticket['engines'])}")
    lines.extend(["", "## Intervenciones del Owner "
                      f"({len(data['interventions'])})"])
    if data["interventions"]:
        lines.extend(f"- {item}" for item in data["interventions"])
    else:
        lines.append("- ninguna")
    lines.extend(["", "## Fallos y manejo"])
    lines.extend(f"- {item}" for item in ticket["failures"])
    lines.extend(["", "## Llamadas fuera de rol "
                      f"({len(data['denied_calls'])})"])
    if data["denied_calls"]:
        lines.extend(
            f"- {call['tool']} ({call['agent']}): {call['reason']}"
            for call in data["denied_calls"])
    else:
        lines.append("- ninguna")
    if data["objective"] is not None:
        objective = data["objective"]
        lines.extend(["", f"## Objetivo {objective['id']} "
                          f"({objective['tickets']} tickets)"])
        lines.append(f"- tokens: {objective['tokens']}")
        for child in objective["children"]:
            lines.append(f"- {child['ref']}: {child['status']}, "
                         f"runs={child['runs']}, tokens {child['tokens']}")
    if data["mission"] is not None:
        mission = data["mission"]
        lines.extend(["", f"## Mision {mission['id']}"])
        if mission["note"]:
            lines.append(f"- {mission['note']}")
        else:
            lines.append(f"- tokens: {mission['tokens']}")
    return "\n".join(lines) + "\n"


class MongoTrialReader:
    """Read-only trial records from the execution database."""

    def __init__(self, db: Any) -> None:
        self._db = db

    @staticmethod
    def _clean(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(row, Mapping):
            return None
        return {key: value for key, value in row.items() if key != "_id"}

    async def _ticket(self, ref: str) -> dict[str, Any] | None:
        if ref.startswith("TASK-"):
            row = await self._db["tasks"].find_one(
                {"$or": [{"params._mirrors.business.name": ref}, {"params._mirrors.business": {"$exists": False}, "params._mirrors.erpnext.name": ref}]})
        else:
            row = await self._db["tasks"].find_one({"task_id": ref})
        return self._clean(row)

    async def read(
        self,
        ticket_ref: str,
        *,
        objective_id: str | None = None,
        mission_id: str | None = None,
    ) -> TrialRecords:
        """Fetch every record the report renders. Read-only."""
        ticket = await self._ticket(ticket_ref)
        task_id = (ticket or {}).get("task_id") or ""
        params = ((ticket or {}).get("params") or {})
        mirrors = (params.get("_mirrors") or {})
        mirror = (business_mirror(mirrors)).get("name") or ""
        children: list[dict[str, Any]] = []
        if objective_id:
            cursor = self._db["tasks"].find({"objective_id": objective_id})
            children = [clean async for row in cursor
                        if (clean := self._clean(row)) is not None]
        mission_docs: list[dict[str, Any]] = []
        if mission_id:
            cursor = self._db["tasks"].find(
                {"params.mission_id": mission_id})
            mission_docs = [clean async for row in cursor
                            if (clean := self._clean(row)) is not None]
        origins = [task_id, *(doc.get("task_id") or "" for doc in children)]
        origins = [origin for origin in origins if origin]
        questions: list[dict[str, Any]] = []
        if origins:
            cursor = self._db["tasks"].find(
                {"params.owner_question.origin_task_id": {"$in": origins}})
            questions = [clean async for row in cursor
                         if (clean := self._clean(row)) is not None]
        tickets = [mirror, task_id, *(business_mirror((doc.get("params") or {}).get("_mirrors")).get("name") or "" for doc in children)]
        tickets = sorted({name for name in tickets if name})
        denied: list[dict[str, Any]] = []
        if tickets:
            cursor = self._db["mcp_calls"].find(
                {"ticket": {"$in": tickets}, "ok": False})
            async for row in cursor:
                clean = self._clean(row)
                if clean is None:
                    continue
                tool = str(clean.get("tool") or "")
                reason = str(clean.get("reason") or "")
                if "/" in tool or reason.startswith("tool error"):
                    continue
                denied.append(clean)
        return TrialRecords(
            ticket_ref=ticket_ref, ticket=ticket,
            objective_id=objective_id, objective_tickets=children,
            mission_id=mission_id, mission_tickets=mission_docs,
            owner_questions=questions, denied_calls=denied)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: print the trial report for one ticket, exit 0/2."""
    parser = argparse.ArgumentParser(
        description="Informe de prueba desde los registros de ejecucion")
    parser.add_argument("--ticket", required=True,
                        help="task_id o TASK-2026-NNNNN")
    parser.add_argument("--objective", default=None)
    parser.add_argument("--mission", default=None)
    parser.add_argument("--format", choices=("markdown", "json"),
                        default="markdown")
    args = parser.parse_args(argv)

    from db.mongo import MongoRepository
    from settings import Settings

    async def _read() -> TrialRecords:
        repo = MongoRepository(Settings())
        await repo.connect()
        try:
            return await MongoTrialReader(repo.db).read(
                args.ticket, objective_id=args.objective,
                mission_id=args.mission)
        finally:
            await repo.close()

    records = asyncio.run(_read())
    if records.ticket is None:
        print(f"ticket {args.ticket} no encontrado en ejecucion",
              file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(trial_report_data(records), indent=2,
                         sort_keys=True, default=str))
    else:
        print(build_trial_report(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
