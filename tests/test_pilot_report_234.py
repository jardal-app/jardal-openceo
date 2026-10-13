"""Pilot trial report builders and reader (TASK-2026-00234, scope 2).

Pure builders over fabricated records plus the Mongo reader against
an in-memory fake. No live database, no tokens.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Mapping

from pilot_report import (
    MongoTrialReader,
    TrialRecords,
    build_trial_report,
    trial_report_data,
)

_UNKNOWN_COST = {
    "runs": 2,
    "tokens_in": None,
    "tokens_out": None,
    "tokens_cached": None,
    "tokens_unknown_runs": 2,
    "duration_s": 12.5,
    "duration_unknown_runs": 0,
    "engines": ["muse-code"],
    "models": ["muse-spark-1.3-contributor"],
    "money": None,
    "money_currency": None,
}

_MEASURED_COST = {
    **_UNKNOWN_COST,
    "runs": 3,
    "tokens_in": 1200,
    "tokens_out": 300,
    "tokens_cached": 0,
    "tokens_unknown_runs": 1,
}


def _ticket(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "task_id": "task-pilot-1",
        "title": "Crear proyecto demo",
        "status": "completed",
        "selected_agent": "operations",
        "cost": dict(_UNKNOWN_COST),
        "params": {
            "target_agent": "operations",
            "skill": "crear-proyecto",
            "_mirrors": {"erpnext": {"name": "TASK-2026-00219"}},
            "agent_result": {"resultado": "hecho",
                             "motivo": "project skeleton ready"},
        },
    }
    doc.update(overrides)
    return doc


def _question(**overrides: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "kind": "lock",
        "question": "Borrar el registro DNS de demo.invalid?",
        "human_action": "Aprobar o rechazar el borrado",
        "origin_task_id": "task-pilot-1",
    }
    block.update(overrides.pop("block", {}))
    doc: dict[str, Any] = {
        "task_id": "task-question-1",
        "status": "completed",
        "params": {"owner_question": block},
    }
    doc.update(overrides)
    return doc


class TokensTests(unittest.TestCase):
    def test_unknown_renders_unknown_never_zero(self) -> None:
        data = trial_report_data(TrialRecords(
            ticket_ref="TASK-2026-00219", ticket=_ticket()))
        self.assertIn("no medidos (2/2", data["ticket"]["tokens"])
        self.assertNotIn("in=0", data["ticket"]["tokens"])

    def test_measured_renders_with_unknown_remainder(self) -> None:
        ticket = _ticket(cost=dict(_MEASURED_COST))
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=ticket))
        self.assertIn("in=1200", data["ticket"]["tokens"])
        self.assertIn("+1 sin medir", data["ticket"]["tokens"])

    def test_missing_cost_renders_unmeasured(self) -> None:
        ticket = _ticket()
        del ticket["cost"]
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=ticket))
        self.assertEqual(data["ticket"]["tokens"],
                         "sin medir (sin corridas)")

    def test_missing_ticket_renders_not_found(self) -> None:
        data = trial_report_data(TrialRecords(ticket_ref="nope"))
        self.assertIn("no encontrado",
                      " ".join(data["ticket"]["failures"]))


class FailuresTests(unittest.TestCase):
    def test_completed_hecho_has_no_failures(self) -> None:
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=_ticket()))
        self.assertIn("sin fallos registrados",
                      data["ticket"]["failures"])

    def test_failed_ticket_lists_every_signal(self) -> None:
        ticket = _ticket(
            status="failed", error="boom " * 60,
            waiting_reason="owner_question:lock",
            params={
                "target_agent": "operations",
                "agent_result": {"resultado": "bloqueado",
                                "motivo": "dns hold"},
                "returns_from": "task-pilot-1",
                "planner_attempts": 2,
                "_mirrors": {"erpnext": {"name": "TASK-2026-00219"}},
            })
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=ticket))
        joined = " ".join(data["ticket"]["failures"])
        for needle in ("failed", "bloqueado", "vuelta al planner",
                       "en espera", "error", "intentos del planner"):
            self.assertIn(needle, joined)


class InterventionsTests(unittest.TestCase):
    def test_pending_and_answered_render(self) -> None:
        answered = _question(block={"decision": {"verdict": "approve",
                                                "by": "owner"}})
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=_ticket(),
            owner_questions=[_question(), answered]))
        self.assertEqual(len(data["interventions"]), 2)
        self.assertIn("pendiente", data["interventions"][0])
        self.assertIn("approve (owner)", data["interventions"][1])

    def test_empty_renders_ninguna(self) -> None:
        report = build_trial_report(TrialRecords(
            ticket_ref="ref", ticket=_ticket()))
        self.assertIn("Intervenciones del Owner (0)", report)
        self.assertIn("- ninguna", report)


class DeniedCallsTests(unittest.TestCase):
    def test_denied_listed_with_tool_and_reason(self) -> None:
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=_ticket(),
            denied_calls=[{"tool": "openceo_send_email",
                           "agent": "operations",
                           "reason": "outside agent box", "ts": "t"}]))
        self.assertEqual(len(data["denied_calls"]), 1)
        report = build_trial_report(TrialRecords(
            ticket_ref="ref", ticket=_ticket(),
            denied_calls=[{"tool": "openceo_send_email",
                           "agent": "operations",
                           "reason": "outside agent box"}]))
        self.assertIn("openceo_send_email (operations)", report)


class ObjectiveMissionTests(unittest.TestCase):
    def test_objective_aggregates_children(self) -> None:
        children = [_ticket(), _ticket(task_id="task-pilot-2",
                                       cost=dict(_MEASURED_COST))]
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=_ticket(), objective_id="OBJ-1",
            objective_tickets=children))
        objective = data["objective"]
        self.assertEqual(objective["tickets"], 2)
        self.assertIn("in=1200", objective["tokens"])
        self.assertEqual(len(objective["children"]), 2)

    def test_absent_objective_renders_no_section(self) -> None:
        report = build_trial_report(TrialRecords(
            ticket_ref="ref", ticket=_ticket()))
        self.assertNotIn("## Objetivo", report)

    def test_mission_without_layer_notes_00218(self) -> None:
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=_ticket(), mission_id="M-1"))
        self.assertIsNotNone(data["mission"])
        self.assertIn("00218", data["mission"]["note"])

    def test_mission_with_tickets_aggregates(self) -> None:
        data = trial_report_data(TrialRecords(
            ticket_ref="ref", ticket=_ticket(), mission_id="M-1",
            mission_tickets=[_ticket(cost=dict(_MEASURED_COST))]))
        self.assertIsNone(data["mission"]["note"])
        self.assertIn("in=1200", data["mission"]["tokens"])


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __aiter__(self) -> Any:
        async def _gen() -> Any:
            for row in self._rows:
                yield row

        return _gen()


class FakeCollection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.queries: list[dict[str, Any]] = []

    async def find_one(self, query: Mapping[str, Any]) -> Any:
        self.queries.append(dict(query))
        return self._rows[0] if self._rows else None

    def find(self, query: Mapping[str, Any]) -> FakeCursor:
        self.queries.append(dict(query))
        return FakeCursor(list(self._rows))


class ReaderTests(unittest.TestCase):
    def _db(self) -> dict[str, FakeCollection]:
        return {
            "tasks": FakeCollection([_ticket()]),
            "mcp_calls": FakeCollection([{"tool": "x", "ok": False}]),
        }

    def test_read_by_task_id(self) -> None:
        db = self._db()
        records = asyncio.run(MongoTrialReader(db).read("task-pilot-1"))
        self.assertEqual(records.ticket_ref, "task-pilot-1")
        self.assertEqual((records.ticket or {}).get("task_id"),
                         "task-pilot-1")
        self.assertIn({"task_id": "task-pilot-1"}, db["tasks"].queries)
        self.assertIsNone(records.objective_id)
        self.assertEqual(records.objective_tickets, [])

    def test_queries_target_the_right_fields(self) -> None:
        db = self._db()
        asyncio.run(MongoTrialReader(db).read(
            "TASK-2026-00219", objective_id="OBJ-1", mission_id="M-1"))
        queries = db["tasks"].queries
        self.assertIn({"params._mirrors.erpnext.name": "TASK-2026-00219"},
                      queries)
        self.assertIn({"objective_id": "OBJ-1"}, queries)
        self.assertIn({"params.mission_id": "M-1"}, queries)
        self.assertTrue(any("params.owner_question.origin_task_id" in query
                            for query in queries))
        denied = db["mcp_calls"].queries[0]
        self.assertEqual(denied["ok"], False)
        self.assertIn("TASK-2026-00219", denied["ticket"]["$in"])


if __name__ == "__main__":
    unittest.main()
