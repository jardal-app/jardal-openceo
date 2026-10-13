"""Company missions over objectives (TASK-2026-00218).

Acceptance with zero tokens: a mission is born as an ERP group shell
plus a Mongo doc, chains objectives through CEO pedidos, asks the
Owner for criteria without blocking, and closes with evidence. The
CEO and the planner are simulated; no network, no CLI, no LLM.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from ceo.models import (  # noqa: E402
    CEODecision,
    CEOValidationContext,
    validate_ceo_decision,
)
from ceo.review import (  # noqa: E402
    CEOEnv,
    CEOSignals,
    _ceo_prompt,
    _missions_section,
    run_review,
)
from company_mandate import parse_mandate  # noqa: E402
from objectives import engine as engine_mod  # noqa: E402
from objectives import missions as missions_mod  # noqa: E402
from objectives.engine import (  # noqa: E402
    InMemoryObjectiveStore,
    InMemoryTaskLister,
)
from objectives.missions import (  # noqa: E402
    InMemoryMissionStore,
    MissionDuplicate,
    MissionEnv,
    MissionErpError,
    MissionRejected,
)

REPO = Path(__file__).resolve().parents[1]


def _mandate() -> Any:
    return parse_mandate({
        "version": 1,
        "ejes": [],
        "principios": ["ganar más"],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": "0.00",
            "cuota": "suscripciones_ya_pagadas",
        },
        "limites": {
            "objetivos_activos": 3,
            "objetivos_activos_por_proyecto": 2,
            "tickets_activos_por_objetivo": 6,
        },
        "revision_ceo": {"cadencia": "semanal"},
        "escalar_al_dueno": [
            "gasto_nuevo",
            "accion_irreversible",
            "replanificacion_estrategica",
            "cambio_de_mandato",
            "revision_exigida_por_politica",
        ],
    })


class _FakeTickets:
    """create/patch/get over a dict (shells, pedidos, questions)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.created: list[dict[str, Any]] = []
        self.patches: dict[str, dict[str, Any]] = {}
        self._seq = 0

    async def create_ticket(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._seq += 1
        task_id = f"t-{self._seq}"
        row = {**payload, "task_id": task_id, "status": "queued"}
        self.rows[task_id] = row
        self.created.append(row)
        return dict(row)

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> None:
        self.patches[task_id] = dict(updates)
        if task_id in self.rows:
            self.rows[task_id].update(updates)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.rows.get(task_id)
        return dict(row) if row is not None else None

    async def list_by_objective(
        self, objective_id: str
    ) -> list[dict[str, Any]]:
        return [dict(row) for row in self.rows.values()
                if row.get("objective_id") == objective_id]

    def lister(self) -> InMemoryTaskLister:
        return InMemoryTaskLister(self.rows)


def _mission_env(tickets: _FakeTickets,
                 wakes: list[str]) -> MissionEnv:
    async def _notify(task_id: str, text: str) -> None:
        del task_id, text

    return MissionEnv(
        missions=InMemoryMissionStore(),
        objectives=InMemoryObjectiveStore(),
        tasks=tickets,
        create_ticket=tickets.create_ticket,
        patch_task=tickets.patch_task,
        notify=_notify,
        ceo_wake=wakes.append,
    )


class MissionStoreTests(unittest.TestCase):
    def test_round_trip_and_active_listing(self) -> None:
        import asyncio

        async def _run() -> None:
            store = InMemoryMissionStore()
            await store.create_mission(
                {"mission_id": "MIS-1", "status": "active",
                 "project": "shop"})
            await store.create_mission(
                {"mission_id": "MIS-2", "status": "closed",
                 "project": "shop"})
            await store.create_mission(
                {"mission_id": "MIS-3", "status": "active",
                 "project": "ops"})
            doc = await store.get_mission("MIS-1")
            assert doc is not None
            self.assertEqual(doc["project"], "shop")
            patched = await store.patch_mission(
                "MIS-1", {"acceptance_status": "confirmed"})
            assert patched is not None
            self.assertEqual(
                patched["acceptance_status"], "confirmed")
            active = await store.list_active_missions()
            self.assertEqual(
                sorted(item["mission_id"] for item in active),
                ["MIS-1", "MIS-3"])
            scoped = await store.list_active_missions("ops")
            self.assertEqual(
                [item["mission_id"] for item in scoped], ["MIS-3"])
            self.assertIsNone(await store.get_mission("MIS-9"))
            self.assertIsNone(
                await store.patch_mission("MIS-9", {"status": "x"}))

        asyncio.run(_run())


class CreateMissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_shell_group_plus_doc_plus_wake(self) -> None:
        tickets = _FakeTickets()
        wakes: list[str] = []
        mission = await missions_mod.create_mission(
            _mission_env(tickets, wakes),
            project="shop",
            goal="Haz andar la tienda",
            acceptance=["Vende cada día."],
            requester="owner",
        )
        self.assertTrue(mission["mission_id"].startswith("MIS-"))
        self.assertEqual(mission["status"], "active")
        self.assertEqual(mission["acceptance"], ["Vende cada día."])
        self.assertEqual(mission["acceptance_source"], "owner")
        self.assertEqual(mission["objectives"], [])
        shell = tickets.created[0]
        self.assertTrue(shell["is_group"])
        self.assertIn("mision", shell["tags"])
        self.assertEqual(shell["origin"], "mission_shell")
        self.assertFalse(shell["autorun"])
        self.assertEqual(
            tickets.patches[shell["task_id"]]["status"], "converted")
        self.assertEqual(len(wakes), 1)
        self.assertIn(mission["mission_id"], wakes[0])
        self.assertNotIn("propose", wakes[0])

    async def test_without_acceptance_starts_pending(self) -> None:
        tickets = _FakeTickets()
        wakes: list[str] = []
        mission = await missions_mod.create_mission(
            _mission_env(tickets, wakes),
            project="shop",
            goal="Haz andar la tienda",
        )
        self.assertEqual(mission["acceptance"], [])
        self.assertEqual(mission["acceptance_source"], "pending")
        self.assertEqual(mission["acceptance_status"], "pending")
        self.assertIsNone(mission["question_task_id"])
        self.assertIn("propose", wakes[0])

    async def test_duplicates_and_bad_shape_refused(self) -> None:
        tickets = _FakeTickets()
        env = _mission_env(tickets, [])
        await missions_mod.create_mission(
            env, project="shop", goal="Haz andar la tienda")
        with self.assertRaises(MissionDuplicate):
            await missions_mod.create_mission(
                env, project="shop", goal="  haz ANDAR la tienda ")
        with self.assertRaises(MissionRejected):
            await missions_mod.create_mission(
                env, project="", goal="x")
        with self.assertRaises(MissionRejected):
            await missions_mod.create_mission(
                env, project="shop", goal="  ")
        with self.assertRaises(MissionRejected):
            await missions_mod.create_mission(
                env, project="shop", goal="x", entry="cohete")

    async def test_shell_failure_refuses_group_less(self) -> None:
        tickets = _FakeTickets()

        async def _none(payload: dict[str, Any]) -> None:
            del payload
            return None

        tickets.create_ticket = _none  # type: ignore[method-assign]
        with self.assertRaises(MissionErpError):
            await missions_mod.create_mission(
                _mission_env(tickets, []),
                project="shop",
                goal="Haz andar la tienda",
            )


def _mission_doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "mission_id": "MIS-1",
        "project": "shop",
        "entry": "top_down",
        "goal": "Haz andar la tienda",
        "acceptance": [],
        "acceptance_source": "pending",
        "acceptance_status": "pending",
        "acceptance_disputed": False,
        "question_task_id": None,
        "question_deadline": None,
        "erp_group_ref": "TASK-2026-00999",
        "status": "active",
        "objectives": [],
        "conversion_source_task_id": "t-1",
        "requester": "owner",
        "delivery": None,
    }
    doc.update(overrides)
    return doc


def _question(
    mission_id: str = "MIS-1",
    deadline: str = "2026-10-15T12:00:00+00:00",
    answered: bool = False,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "owner_question": {
            "kind": "question",
            "question": "¿Con estos criterios?",
            "human_action": "Confirma.",
            "origin_task_id": "t-1",
            "checkpoint": {
                "mission_id": mission_id,
                "proposed": ["Vende cada día."],
                "deadline": deadline,
            },
        }
    }
    if answered:
        params["owner_question"]["decision"] = "approved"
    return {
        "task_id": "t-q",
        "status": "waiting",
        "project": "shop",
        "params": params,
    }


class AdoptProposalTests(unittest.TestCase):
    def test_pending_mission_adopts(self) -> None:
        patch = missions_mod.adopt_proposal(
            _mission_doc(), ["Vende cada día.", "  "])
        assert patch is not None
        self.assertEqual(patch["acceptance"], ["Vende cada día."])
        self.assertEqual(patch["acceptance_source"], "proposal")
        self.assertEqual(patch["acceptance_status"], "proposed")

    def test_nothing_applies_without_pending(self) -> None:
        self.assertIsNone(missions_mod.adopt_proposal(
            _mission_doc(acceptance=["Ya hay."]), ["Otra."]))
        self.assertIsNone(missions_mod.adopt_proposal(
            _mission_doc(question_task_id="t-q"), ["Otra."]))
        self.assertIsNone(missions_mod.adopt_proposal(
            _mission_doc(status="closed"), ["Otra."]))
        self.assertIsNone(
            missions_mod.adopt_proposal(_mission_doc(), ["  "]))

    def test_question_spec_carries_checkpoint(self) -> None:
        spec = missions_mod.acceptance_question_spec(
            _mission_doc(
                acceptance=["Vende cada día."],
                acceptance_source="proposal",
            ),
            proposed_at=datetime(
                2026, 10, 13, 12, 0, tzinfo=timezone.utc),
        )
        block = spec["params"]["owner_question"]
        self.assertEqual(block["kind"], "question")
        self.assertEqual(block["origin_task_id"], "t-1")
        self.assertEqual(block["checkpoint"]["mission_id"], "MIS-1")
        self.assertEqual(
            block["checkpoint"]["proposed"], ["Vende cada día."])
        self.assertEqual(
            block["checkpoint"]["deadline"],
            "2026-10-15T12:00:00+00:00")
        self.assertEqual(
            spec["source_id"], "mission-acceptance:MIS-1")
        self.assertIn("analízalo tú", block["human_action"])

    def test_question_spec_without_proposal_rejected(self) -> None:
        with self.assertRaises(MissionRejected):
            missions_mod.acceptance_question_spec(_mission_doc())


class MarkerExpiryTests(unittest.TestCase):
    def test_marker_only_on_mission_questions(self) -> None:
        marker = missions_mod.acceptance_question_marker(_question())
        assert marker is not None
        self.assertEqual(marker["mission_id"], "MIS-1")
        self.assertIsNone(
            missions_mod.acceptance_question_marker(None))
        self.assertIsNone(missions_mod.acceptance_question_marker(
            {"task_id": "t-x", "params": {}}))
        other = _question()
        other["params"]["owner_question"]["kind"] = "lock"
        self.assertIsNone(
            missions_mod.acceptance_question_marker(other))

    def test_expiry_needs_deadline_and_silence(self) -> None:
        moment = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)
        self.assertTrue(missions_mod.mission_question_expired(
            _question(), moment))
        self.assertFalse(missions_mod.mission_question_expired(
            _question(answered=True), moment))
        early = datetime(2026, 10, 14, 13, 0, tzinfo=timezone.utc)
        self.assertFalse(
            missions_mod.mission_question_expired(_question(), early))
        self.assertFalse(missions_mod.mission_question_expired(
            {"task_id": "t-x"}, moment))


class AcceptanceAnswerTests(unittest.TestCase):
    def test_approve_with_criteria_adopts(self) -> None:
        patch = missions_mod.apply_acceptance_answer(
            _mission_doc(), approve=True,
            comment="Vende cada día.\nSin deuda nueva.")
        self.assertEqual(
            patch["acceptance"],
            ["Vende cada día.", "Sin deuda nueva."])
        self.assertEqual(patch["acceptance_source"], "owner")
        self.assertEqual(patch["acceptance_status"], "confirmed")

    def test_you_decide_confirms_proposal(self) -> None:
        for text in ("analízalo tú", "  ANALIZALO TU  ",
                     "por favor, analízalo tú y sigue"):
            with self.subTest(text=text):
                patch = missions_mod.apply_acceptance_answer(
                    _mission_doc(), approve=True, comment=text)
                self.assertEqual(
                    patch["acceptance_status"], "confirmed")
                self.assertNotIn("acceptance", patch)

    def test_approve_empty_confirms(self) -> None:
        patch = missions_mod.apply_acceptance_answer(
            _mission_doc(), approve=True, comment="  ")
        self.assertEqual(patch["acceptance_status"], "confirmed")

    def test_deny_with_criteria_adopts_them(self) -> None:
        patch = missions_mod.apply_acceptance_answer(
            _mission_doc(), approve=False,
            comment="Solo esto: vende.")
        self.assertEqual(patch["acceptance"], ["Solo esto: vende."])
        self.assertEqual(patch["acceptance_source"], "owner")

    def test_deny_empty_disputes_without_blocking(self) -> None:
        patch = missions_mod.apply_acceptance_answer(
            _mission_doc(), approve=False, comment="")
        self.assertTrue(patch["acceptance_disputed"])
        self.assertNotIn("acceptance", patch)


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_link_and_close_are_idempotent(self) -> None:
        store = InMemoryMissionStore()
        await store.create_mission(_mission_doc())
        env = MissionEnv(
            missions=store,
            objectives=InMemoryObjectiveStore(),
            tasks=InMemoryTaskLister(),
            create_ticket=AsyncMock(),
            patch_task=AsyncMock(),
        )
        await missions_mod.record_objective_linked(
            store, "MIS-1", "OBJ-1")
        await missions_mod.record_objective_linked(
            store, "MIS-1", "OBJ-1")
        doc = await store.get_mission("MIS-1")
        assert doc is not None
        self.assertEqual(
            doc["objectives"],
            [{"objective_id": "OBJ-1", "status": "active"}])
        await missions_mod.record_objective_closed(
            store, "MIS-1", "OBJ-1")
        doc = await store.get_mission("MIS-1")
        assert doc is not None
        self.assertEqual(doc["objectives"][0]["status"], "closed")
        self.assertIsNone(
            await missions_mod.record_objective_linked(
                store, "MIS-9", "OBJ-1"))
        self.assertIsNone(
            await missions_mod.record_objective_closed(
                store, "MIS-9", "OBJ-1"))


class CloseMissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_cites_evidence_and_completes_shell(self) -> None:
        tickets = _FakeTickets()
        store = InMemoryMissionStore()
        await store.create_mission(_mission_doc(
            acceptance=["Vende cada día."],
            acceptance_source="owner",
            acceptance_status="confirmed",
            objectives=[{"objective_id": "OBJ-1", "status": "closed"}],
        ))
        objectives = InMemoryObjectiveStore()
        await objectives.create_objective({
            "objective_id": "OBJ-1", "status": "closed",
            "delivery": {"verdict": "clean"},
        })
        shell = await tickets.create_ticket({"title": "shell"})
        await store.patch_mission(
            "MIS-1", {"conversion_source_task_id": shell["task_id"]})
        await tickets.patch_task(shell["task_id"], {"status": "converted"})
        notified: list[str] = []

        async def _notify(task_id: str, text: str) -> None:
            notified.append(f"{task_id}:{text[:20]}")

        env = MissionEnv(
            missions=store,
            objectives=objectives,
            tasks=tickets,
            create_ticket=tickets.create_ticket,
            patch_task=tickets.patch_task,
            notify=_notify,
        )
        result = await missions_mod.close_mission(
            env, "MIS-1", reason="Vende cada día desde el lunes.")
        self.assertTrue(result["ok"])
        doc = await store.get_mission("MIS-1")
        assert doc is not None
        self.assertEqual(doc["status"], "closed")
        self.assertEqual(
            doc["delivery"]["acceptance"], ["Vende cada día."])
        self.assertEqual(
            doc["delivery"]["objectives"], ["OBJ-1"])
        self.assertEqual(
            doc["delivery"]["evidence"][0]["delivery"],
            {"verdict": "clean"})
        self.assertEqual(
            tickets.patches[shell["task_id"]]["status"], "completed")
        self.assertEqual(len(notified), 1)

    async def test_unknown_or_inactive_rejected(self) -> None:
        env = MissionEnv(
            missions=InMemoryMissionStore(),
            objectives=InMemoryObjectiveStore(),
            tasks=InMemoryTaskLister(),
            create_ticket=AsyncMock(),
            patch_task=AsyncMock(),
        )
        with self.assertRaises(MissionRejected):
            await missions_mod.close_mission(env, "MIS-9")
        await env.missions.create_mission(
            _mission_doc(status="closed"))
        with self.assertRaises(MissionRejected):
            await missions_mod.close_mission(env, "MIS-1")


def _ctx(**overrides: Any) -> CEOValidationContext:
    base: dict[str, Any] = {
        "mandate": _mandate(),
        "known_projects": ["ops", "shop"],
        "open_titles": [],
        "active_objectives": [],
        "open_escalations": [],
        "kpi_readings": {},
    }
    base.update(overrides)
    return CEOValidationContext(**base)


def _mission_ctx_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "status": "active",
        "shell_status": "converted",
        "acceptance": ["Vende cada día."],
    }
    entry.update(overrides)
    return entry


class DecisionContractTests(unittest.TestCase):
    def test_close_mission_needs_its_spec(self) -> None:
        with self.assertRaises(Exception):
            CEODecision.model_validate(
                {"action": "close_mission", "summary": "Done"})
        with self.assertRaises(Exception):
            CEODecision.model_validate(
                {"action": "keep_going", "summary": "Idle",
                 "mission": {"mission_id": "MIS-1"}})
        decision = CEODecision.model_validate(
            {"action": "close_mission", "summary": "Done",
             "mission": {"mission_id": "MIS-1"}})
        self.assertEqual(decision.mission is not None, True)


class CloseValidationTests(unittest.TestCase):
    def _close(self, mission_id: str = "MIS-1") -> CEODecision:
        return CEODecision.model_validate(
            {"action": "close_mission", "summary": "Vende cada día.",
             "mission": {"mission_id": mission_id}})

    def test_happy_path(self) -> None:
        errors = validate_ceo_decision(
            self._close(),
            _ctx(
                missions={"MIS-1": _mission_ctx_entry()},
                mission_objectives={"MIS-1": [
                    {"objective_id": "OBJ-1", "status": "closed"},
                    {"objective_id": "OBJ-2", "status": "cancelled"},
                ]},
            ),
        )
        self.assertEqual(errors, [])

    def test_unknown_inactive_or_blind_rejected(self) -> None:
        self.assertIn(
            "unknown mission",
            validate_ceo_decision(self._close("MIS-9"), _ctx())[0])
        errors = validate_ceo_decision(
            self._close(),
            _ctx(missions={"MIS-1": _mission_ctx_entry(
                status="closed")}))
        self.assertIn("not active", errors[0])
        errors = validate_ceo_decision(
            self._close(),
            _ctx(
                missions={"MIS-1": _mission_ctx_entry()},
                mission_objectives={"MIS-1": [
                    {"objective_id": "OBJ-1", "status": "closed"}]},
                missions_unknown=True,
            ))
        self.assertIn("unreadable", errors[0])

    def test_acceptance_and_evidence_required(self) -> None:
        errors = validate_ceo_decision(
            self._close(),
            _ctx(
                missions={"MIS-1": _mission_ctx_entry(acceptance=[])},
                mission_objectives={"MIS-1": [
                    {"objective_id": "OBJ-1", "status": "closed"}]},
            ))
        self.assertIn("no acceptance", errors[0])
        errors = validate_ceo_decision(
            self._close(),
            _ctx(
                missions={"MIS-1": _mission_ctx_entry()},
                mission_objectives={"MIS-1": []},
            ))
        self.assertIn("never empty", errors[0])
        errors = validate_ceo_decision(
            self._close(),
            _ctx(
                missions={"MIS-1": _mission_ctx_entry()},
                mission_objectives={"MIS-1": [
                    {"objective_id": "OBJ-1", "status": "closed"},
                    {"objective_id": "OBJ-2", "status": "active"},
                ]},
            ))
        self.assertIn("still runs", errors[0])
        self.assertIn("OBJ-2", errors[0])

    def test_cancelled_shell_rejected(self) -> None:
        errors = validate_ceo_decision(
            self._close(),
            _ctx(
                missions={"MIS-1": _mission_ctx_entry(
                    shell_status="cancelled")},
                mission_objectives={"MIS-1": [
                    {"objective_id": "OBJ-1", "status": "closed"}]},
            ))
        self.assertIn("cancelled", errors[0])


class PedidoMissionLinkTests(unittest.TestCase):
    def _pedido(self, **spec: Any) -> CEODecision:
        body: dict[str, Any] = {
            "project": "shop",
            "title": "Fix checkout",
            "description": "The button fails",
            "justificacion": "Checkout pierde ventas.",
        }
        body.update(spec)
        return CEODecision.model_validate(
            {"action": "crear_pedido", "summary": "Shop needs checkout",
             "pedido": body})

    def test_valid_link(self) -> None:
        errors = validate_ceo_decision(
            self._pedido(mission_id="MIS-1"),
            _ctx(missions={"MIS-1": _mission_ctx_entry()}),
        )
        self.assertEqual(errors, [])

    def test_unknown_inactive_terminal_or_blind_rejected(self) -> None:
        self.assertIn(
            "unknown mission",
            validate_ceo_decision(
                self._pedido(mission_id="MIS-9"), _ctx())[0])
        errors = validate_ceo_decision(
            self._pedido(mission_id="MIS-1"),
            _ctx(missions={"MIS-1": _mission_ctx_entry(
                status="closed")}))
        self.assertIn("not active", errors[0])
        for shell in ("cancelled", "completed"):
            with self.subTest(shell=shell):
                errors = validate_ceo_decision(
                    self._pedido(mission_id="MIS-1"),
                    _ctx(missions={"MIS-1": _mission_ctx_entry(
                        shell_status=shell)}))
                self.assertIn(shell, errors[0])
        errors = validate_ceo_decision(
            self._pedido(mission_id="MIS-1"),
            _ctx(
                missions={"MIS-1": _mission_ctx_entry()},
                missions_unknown=True,
            ))
        self.assertIn("unreadable", errors[0])


def _mission_signal(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "mission_id": "MIS-1",
        "project": "shop",
        "goal": "Haz andar la tienda",
        "status": "active",
        "shell_status": "converted",
        "acceptance": ["Vende cada día."],
        "acceptance_source": "proposal",
        "acceptance_status": "confirmed",
        "acceptance_disputed": False,
        "question_state": "",
        "objectives": [
            {"objective_id": "OBJ-1", "status": "closed"}],
        "cost": None,
    }
    item.update(overrides)
    return item


class MissionPromptTests(unittest.TestCase):
    def test_missions_section_renders_state(self) -> None:
        section = _missions_section(CEOSignals(
            missions=[
                _mission_signal(),
                _mission_signal(
                    mission_id="MIS-2", acceptance=[],
                    acceptance_source="pending",
                    acceptance_status="pending",
                    objectives=[],
                    question_state="open (deadline 2026-10-15)",
                    cost={"runs": 2, "tokens": {"in": 3}}),
            ]))
        self.assertIn("MIS-1", section)
        self.assertIn("Vende cada día.", section)
        self.assertIn("OBJ-1=closed", section)
        self.assertIn("no criteria yet", section)
        self.assertIn("open (deadline 2026-10-15)", section)
        self.assertIn("cost: ", section)

    def test_missions_section_empty_and_unknown(self) -> None:
        self.assertIn("(none active)",
                      _missions_section(CEOSignals()))
        self.assertIn("UNKNOWN", _missions_section(
            CEOSignals(missions_unknown=True)))

    def test_full_prompt_carries_five_actions(self) -> None:
        prompt = _ceo_prompt(
            CEOSignals(missions=[_mission_signal()]),
            kind="reactive",
            reasons=["Objective OBJ-1 closed"],
            review_id="ceo-test",
            repo_root=REPO,
            ops_project="ops",
        )
        self.assertIn("MISSIONS", prompt)
        self.assertIn("MIS-1", prompt)
        self.assertIn("close_mission", prompt)
        self.assertIn("mission_id", prompt)
        self.assertNotIn("one of the four", prompt)


class MissionGuideTests(unittest.IsolatedAsyncioTestCase):
    async def test_guide_names_chain_and_gap(self) -> None:
        store = InMemoryMissionStore()
        await store.create_mission(_mission_doc(
            acceptance=["Vende cada día."]))
        env = SimpleNamespace(missions=store)
        guide = await engine_mod._mission_guide_section(
            env, {"task_id": "t-1", "title": "Fix",
                  "params": {"mission_id": "MIS-1"}})
        self.assertIn("MISSION MIS-1", guide)
        self.assertIn("Vende cada día.", guide)
        self.assertIn("capability_gap", guide)

    async def test_guide_empty_without_link(self) -> None:
        store = InMemoryMissionStore()
        env = SimpleNamespace(missions=store)
        self.assertEqual(
            await engine_mod._mission_guide_section(
                env, {"task_id": "t-1", "title": "Fix"}),
            "")
        self.assertEqual(
            await engine_mod._mission_guide_section(
                SimpleNamespace(), {"task_id": "t-1"}),
            "")
        self.assertEqual(
            await engine_mod._mission_guide_section(
                env, {"task_id": "t-1",
                      "params": {"mission_id": "MIS-9"}}),
            "")


class _RecordedCEO:
    def __init__(self, repo: Path, payloads: list[dict[str, Any]]) -> None:
        self._repo = repo
        self._payloads = list(payloads)
        self.prompts: list[str] = []

    async def run(self, role: str, prompt: str, workdir: Path,
                  timeout_s: int) -> Any:
        del workdir, timeout_s
        assert role == "ceo_reviewer", role
        self.prompts.append(prompt)
        target = self._repo / ".openceo" / "CEO_DECISION.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self._payloads.pop(0)), encoding="utf-8")
        return SimpleNamespace(returncode=0, output_tail="CEO_READY")

    async def cleanup(self, role: str) -> None:
        del role


def _ceo_env(
    repo: Path,
    tickets: _FakeTickets,
    objectives: InMemoryObjectiveStore,
    missions: InMemoryMissionStore,
    ceo: _RecordedCEO,
) -> CEOEnv:
    async def _empty(*args: Any, **kwargs: Any) -> list[Any]:
        del args, kwargs
        return []

    async def _business() -> dict[str, Any]:
        return {"rows": [], "unknown": False}

    async def _publish(record: dict[str, Any]) -> None:
        del record

    async def _escalate(**kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise AssertionError("no escalation expected")

    async def _close_mission(
        mission_id: str, reason: str = ""
    ) -> dict[str, Any]:
        return await missions_mod.close_mission(
            MissionEnv(
                missions=missions,
                objectives=objectives,
                tasks=tickets,
                create_ticket=tickets.create_ticket,
                patch_task=tickets.patch_task,
            ),
            mission_id,
            reason=reason,
        )

    from ceo.models import CEOKpiReading

    async def _kpi(fuente: str) -> CEOKpiReading:
        del fuente
        return CEOKpiReading(value=None, unknown=True)

    return CEOEnv(
        mandate=_mandate(),
        objectives=objectives,
        tasks=tickets.lister(),
        open_titles=_empty,
        open_escalations=_empty,
        recently_closed=_empty,
        blocked_tasks=_empty,
        recent_research=_empty,
        business_open=_business,
        read_kpi=_kpi,
        known_projects=["ops", "shop"],
        ops_project="ops",
        create_ticket=tickets.create_ticket,
        escalate=_escalate,
        executor=ceo,
        repo_root=repo,
        publish=_publish,
        ceo_cfg={},
        now=lambda: datetime(
            2026, 10, 13, 12, 0, tzinfo=timezone.utc),
        missions=missions,
        patch_task=tickets.patch_task,
        close_mission=_close_mission,
    )


class MissionReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_pedido_is_born_under_the_group(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tickets = _FakeTickets()
            shell = await tickets.create_ticket({"title": "shell"})
            shell["params"] = {"_mirrors": {"erpnext": {
                "name": "TASK-2026-00999"}}}
            tickets.rows[shell["task_id"]] = shell
            missions = InMemoryMissionStore()
            await missions.create_mission(_mission_doc(
                acceptance=["Vende cada día."],
                conversion_source_task_id=shell["task_id"],
            ))
            ceo = _RecordedCEO(repo, [{
                "action": "crear_pedido",
                "summary": "First objective",
                "pedido": {
                    "project": "shop",
                    "title": "Fix checkout",
                    "description": "The button fails",
                    "justificacion": "Checkout pierde ventas.",
                    "mission_id": "MIS-1",
                },
            }])
            env = _ceo_env(
                repo, tickets, InMemoryObjectiveStore(),
                missions, ceo)
            outcome = await run_review(env, kind="reactive",
                                       reasons=["Mission created"])
            self.assertEqual(outcome["action"], "pedido_created")
            pedido = tickets.created[-1]
            self.assertEqual(
                pedido["params"]["mission_id"], "MIS-1")
            self.assertEqual(
                pedido.get("parent_task"), "TASK-2026-00999")
            self.assertIn("MIS-1", ceo.prompts[0])

    async def test_close_mission_runs_the_flow(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tickets = _FakeTickets()
            shell = await tickets.create_ticket({"title": "shell"})
            await tickets.patch_task(
                shell["task_id"], {"status": "converted"})
            missions = InMemoryMissionStore()
            await missions.create_mission(_mission_doc(
                acceptance=["Vende cada día."],
                conversion_source_task_id=shell["task_id"],
                objectives=[
                    {"objective_id": "OBJ-1", "status": "closed"}],
            ))
            objectives = InMemoryObjectiveStore()
            await objectives.create_objective({
                "objective_id": "OBJ-1", "status": "closed",
                "delivery": {"verdict": "clean"},
            })
            ceo = _RecordedCEO(repo, [{
                "action": "close_mission",
                "summary": "Vende cada día desde el lunes.",
                "mission": {"mission_id": "MIS-1"},
            }])
            env = _ceo_env(repo, tickets, objectives, missions, ceo)
            outcome = await run_review(env, kind="reactive",
                                       reasons=["Objective closed"])
            self.assertEqual(outcome["action"], "mission_closed")
            doc = await missions.get_mission("MIS-1")
            assert doc is not None
            self.assertEqual(doc["status"], "closed")
            self.assertEqual(
                doc["delivery"]["evidence"][0]["delivery"],
                {"verdict": "clean"})

    async def test_silent_deadline_confirms(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tickets = _FakeTickets()
            shell = await tickets.create_ticket({"title": "shell"})
            question = _question(deadline=(
                "2026-10-13T11:00:00+00:00"))
            question["task_id"] = "t-q"
            tickets.rows["t-q"] = question
            missions = InMemoryMissionStore()
            await missions.create_mission(_mission_doc(
                acceptance=["Vende cada día."],
                acceptance_source="proposal",
                acceptance_status="proposed",
                conversion_source_task_id=shell["task_id"],
                question_task_id="t-q",
                question_deadline="2026-10-13T11:00:00+00:00",
            ))
            ceo = _RecordedCEO(repo, [{
                "action": "do_nothing",
                "summary": "Nothing new needed.",
            }])
            published: list[dict[str, Any]] = []

            async def _publish(record: dict[str, Any]) -> None:
                published.append(record)

            env = _ceo_env(
                repo, tickets, InMemoryObjectiveStore(),
                missions, ceo)
            env.publish = _publish  # type: ignore[method-assign]
            outcome = await run_review(env, kind="scheduled",
                                       reasons=[])
            self.assertEqual(outcome["action"], "do_nothing")
            self.assertEqual(
                published[0]["expired_questions"], ["MIS-1"])
            self.assertIn(
                "closed without recorded answer", ceo.prompts[0])
            doc = await missions.get_mission("MIS-1")
            assert doc is not None
            self.assertEqual(
                doc["acceptance_status"], "confirmed")
            self.assertEqual(
                tickets.patches["t-q"]["status"], "completed")


class ConvertAdoptTests(unittest.IsolatedAsyncioTestCase):
    async def test_convert_links_and_asks(self) -> None:
        from unittest.mock import patch

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tickets = _FakeTickets()
            missions = InMemoryMissionStore()
            await missions.create_mission(_mission_doc())
            objectives = InMemoryObjectiveStore()
            ticket = {
                "task_id": "t-pedido", "title": "Fix checkout",
                "description": "The button fails",
                "project": "shop",
                "acceptance": ["Vende cada día."],
                "params": {"mission_id": "MIS-1"},
            }
            env = SimpleNamespace(
                objectives=objectives,
                tasks=tickets.lister(),
                patch_task=tickets.patch_task,
                create_ticket=tickets.create_ticket,
                notify=None,
                erp=None,
                missions=missions,
                repo=Path(tmp),
                auto_merge=False,
                base_branch="main",
            )
            with patch.object(
                engine_mod, "plan_objective",
                new=AsyncMock(return_value={"ok": True,
                                            "reason": "stubbed"}),
            ):
                objective = await engine_mod.convert_ticket(
                    env, ticket, "OBJECTIVE_NEEDED", "")
            stored = await objectives.get_objective(
                objective["objective_id"])
            assert stored is not None
            self.assertEqual(stored["mission_id"], "MIS-1")
            doc = await missions.get_mission("MIS-1")
            assert doc is not None
            self.assertEqual(
                doc["acceptance"], ["Vende cada día."])
            self.assertEqual(
                doc["acceptance_source"], "proposal")
            self.assertEqual(
                doc["objectives"],
                [{"objective_id": objective["objective_id"],
                  "status": "active"}])
            question = tickets.created[-1]
            marker = missions_mod.acceptance_question_marker(question)
            assert marker is not None
            self.assertEqual(marker["mission_id"], "MIS-1")
            self.assertFalse(question["autorun"])
            patch = tickets.patches[question["task_id"]]
            self.assertEqual(patch["status"], "waiting")
            self.assertEqual(patch["phase"], "owner_question")

    async def test_convert_without_proposal_stays_pending(self) -> None:
        from unittest.mock import patch

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tickets = _FakeTickets()
            missions = InMemoryMissionStore()
            await missions.create_mission(_mission_doc())
            objectives = InMemoryObjectiveStore()
            ticket = {
                "task_id": "t-pedido", "title": "Fix checkout",
                "description": "The button fails",
                "project": "shop",
                "acceptance": [],
                "params": {"mission_id": "MIS-1"},
            }
            env = SimpleNamespace(
                objectives=objectives,
                tasks=tickets.lister(),
                patch_task=tickets.patch_task,
                create_ticket=tickets.create_ticket,
                notify=None,
                erp=None,
                missions=missions,
                repo=Path(tmp),
                auto_merge=False,
                base_branch="main",
            )
            with patch.object(
                engine_mod, "plan_objective",
                new=AsyncMock(return_value={"ok": True,
                                            "reason": "stubbed"}),
            ):
                objective = await engine_mod.convert_ticket(
                    env, ticket, "OBJECTIVE_NEEDED", "")
            doc = await missions.get_mission("MIS-1")
            assert doc is not None
            self.assertEqual(doc["acceptance"], [])
            self.assertEqual(
                doc["acceptance_status"], "pending")
            self.assertIsNone(doc["question_task_id"])
            self.assertEqual(len(doc["objectives"]), 1)
            self.assertEqual(objective["entry"], "convert")


class FinishLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_records_and_wakes_with_mission(self) -> None:
        store = InMemoryObjectiveStore()
        await store.create_objective({
            "objective_id": "OBJ-1", "status": "active",
            "project": "shop",
            "mission_id": "MIS-1",
            "conversion_source_task_id": "t1",
            "anchor": {"goal": "Sell", "acceptance": ["a"]},
            "tickets_log": [],
        })
        missions = InMemoryMissionStore()
        await missions.create_mission(_mission_doc(
            objectives=[{"objective_id": "OBJ-1",
                         "status": "active"}]))
        rows = {"t1": {"task_id": "t1", "status": "converted"}}
        patches: dict[str, dict[str, Any]] = {}
        wakes: list[str] = []

        async def _patch(task_id: str, updates: dict[str, Any]) -> None:
            patches[task_id] = updates
            rows[task_id].update(updates)

        env = SimpleNamespace(
            objectives=store,
            tasks=InMemoryTaskLister(rows),
            patch_task=_patch,
            notify=None,
            ceo_wake=wakes.append,
            auto_merge=True,
            base_branch="main",
            vault=None,
            missions=missions,
        )
        objective = await store.get_objective("OBJ-1")
        assert objective is not None
        result = await engine_mod._finish_objective(env, objective)
        self.assertTrue(result["ok"])
        doc = await missions.get_mission("MIS-1")
        assert doc is not None
        self.assertEqual(doc["objectives"][0]["status"], "closed")
        self.assertEqual(len(wakes), 1)
        self.assertIn("OBJ-1", wakes[0])
        self.assertIn("MIS-1", wakes[0])


class MissionCostTests(unittest.TestCase):
    def test_mission_aggregates_children(self) -> None:
        from objectives.ticket_cost import aggregate_mission_cost

        total = aggregate_mission_cost([
            {"runs": 1, "tokens": {"in": 2}},
            {"runs": 2, "tokens": {"in": 3}},
            None,
        ])
        assert total is not None
        self.assertEqual(total["runs"], 3)
        self.assertIsNone(aggregate_mission_cost([]))
        self.assertIsNone(aggregate_mission_cost(None))


class MissionCockpitTests(unittest.TestCase):
    def test_describe_names_missions(self) -> None:
        from horizon.cockpit import describe_company

        text = describe_company({
            "missions": [
                {"mission_id": "MIS-1", "goal": "Haz andar la tienda"}],
            "objectives": [],
        })
        self.assertIn("una misión activa", text)
        self.assertIn("Haz andar la tienda", text)
        text = describe_company({
            "missions": [
                {"mission_id": "MIS-1", "goal": "Una"},
                {"mission_id": "MIS-2", "goal": "Dos"}],
            "objectives": [],
            "missions_unknown": True,
        })
        self.assertIn("2 misiones activas", text)
        self.assertIn("No pudimos leer las misiones", text)
        self.assertNotIn("misión", describe_company(
            {"missions": [], "objectives": []}))


class MissionVoiceTests(unittest.IsolatedAsyncioTestCase):
    def _gateway(self, intent_json: dict[str, Any],
                 task_service: Any) -> Any:
        import json as json_mod

        from first_layer import FirstLayerGateway

        gateway = FirstLayerGateway.__new__(FirstLayerGateway)
        gateway.horizon = None
        gateway._finance_handler = None

        class _Llm:
            async def ainvoke(self, _messages: Any) -> Any:
                return SimpleNamespace(
                    content=json_mod.dumps(intent_json))

        gateway._llm = _Llm()
        gateway.dev_session_service = SimpleNamespace(_tmux=None)
        gateway.task_service = task_service

        async def _ctx(_user_id: str) -> dict[str, Any]:
            return {"selected_project": "proj",
                    "projects": [{"name": "proj"}],
                    "active_ticket": None, "open_tasks": [],
                    "open_count": 0, "active_session": None,
                    "all_tmux": []}

        gateway.gather_context = _ctx  # type: ignore[method-assign]
        return gateway

    async def test_voice_creates_mission(self) -> None:
        task_service = SimpleNamespace(
            create_mission=AsyncMock(
                return_value={"mission_id": "MIS-1"}))
        gateway = self._gateway(
            {"intent": "mission_create",
             "dictation": "haz andar la tienda",
             "project": None, "spoken_reply": ""},
            task_service)
        handled, reply = await gateway.process_turn(
            "owner", "crea una misión para andar la tienda",
            SimpleNamespace())
        self.assertTrue(handled)
        self.assertIn("misión nueva", reply)
        self.assertIn("Tenemos", reply)
        _args, kwargs = task_service.create_mission.call_args
        self.assertEqual(kwargs["goal"], "haz andar la tienda")
        self.assertEqual(kwargs["project"], "proj")
        self.assertEqual(kwargs["source"], "voice")


class MissionAnswerHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_answer_records_on_mission(self) -> None:
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        missions = InMemoryMissionStore()
        await missions.create_mission(_mission_doc(
            acceptance=["Vende cada día."],
            acceptance_source="proposal",
            question_task_id="t-q",
        ))
        service._repo = SimpleNamespace(
            get_mission=missions.get_mission,
            patch_mission=missions.patch_mission,
        )
        service.comment_on_ticket = AsyncMock()  # type: ignore[attr-defined]
        route = await service._apply_mission_acceptance_decision(
            _question(), {"mission_id": "MIS-1"},
            approve=True, record={"comment": "Vende siempre."})
        self.assertEqual(route, "owner_question_mission_recorded")
        doc = await missions.get_mission("MIS-1")
        assert doc is not None
        self.assertEqual(doc["acceptance"], ["Vende siempre."])
        self.assertEqual(doc["acceptance_source"], "owner")


class MissionAcceptanceLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_mission_to_close_with_simulated_runs(self) -> None:
        """Misión → obj1 → cierre → obj2 → aceptación → cierre."""
        from unittest.mock import patch

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            tickets = _FakeTickets()
            missions = InMemoryMissionStore()
            objectives = InMemoryObjectiveStore()
            wakes: list[str] = []
            mission = await missions_mod.create_mission(
                MissionEnv(
                    missions=missions,
                    objectives=objectives,
                    tasks=tickets,
                    create_ticket=tickets.create_ticket,
                    patch_task=tickets.patch_task,
                    ceo_wake=wakes.append,
                ),
                project="shop",
                goal="Haz andar la tienda",
            )
            mission_id = mission["mission_id"]
            self.assertEqual(mission["acceptance_status"], "pending")

            async def _ceo_review(payload: dict[str, Any]) -> dict[str, Any]:
                ceo = _RecordedCEO(repo, [payload])
                env = _ceo_env(
                    repo, tickets, objectives, missions, ceo)
                return await run_review(env, kind="reactive",
                                        reasons=wakes)

            async def _close(objective_id: str) -> None:
                objective = await objectives.get_objective(objective_id)
                assert objective is not None
                env = SimpleNamespace(
                    objectives=objectives,
                    tasks=tickets.lister(),
                    patch_task=tickets.patch_task,
                    notify=None,
                    ceo_wake=wakes.append,
                    auto_merge=True,
                    base_branch="main",
                    vault=None,
                    missions=missions,
                )
                result = await engine_mod._finish_objective(
                    env, objective)
                self.assertTrue(result["ok"])

            import asyncio

            async def _convert_async(
                ticket: dict[str, Any]
            ) -> dict[str, Any]:
                env = SimpleNamespace(
                    objectives=objectives,
                    tasks=tickets.lister(),
                    patch_task=tickets.patch_task,
                    create_ticket=tickets.create_ticket,
                    notify=None,
                    erp=None,
                    missions=missions,
                    repo=repo,
                    auto_merge=False,
                    base_branch="main",
                )
                with patch.object(
                    engine_mod, "plan_objective",
                    new=AsyncMock(return_value={
                        "ok": True, "reason": "stubbed"}),
                ):
                    return await engine_mod.convert_ticket(
                        env, ticket, "OBJECTIVE_NEEDED", "")

            async def _objective_cycle(title: str) -> str:
                outcome = await _ceo_review({
                    "action": "crear_pedido",
                    "summary": f"Need {title}",
                    "pedido": {
                        "project": "shop",
                        "title": title,
                        "description": "Work the mission",
                        "acceptance": ["Vende cada día."],
                        "justificacion": "La tienda no vende.",
                        "mission_id": mission_id,
                    },
                })
                self.assertEqual(outcome["action"], "pedido_created")
                pedido = tickets.created[-1]
                objective = await _convert_async(pedido)
                self.assertEqual(objective["entry"], "convert")
                await _close(objective["objective_id"])
                return str(objective["objective_id"])

            obj1 = await _objective_cycle("Fix checkout")
            doc = await missions.get_mission(mission_id)
            assert doc is not None
            self.assertEqual(
                doc["acceptance"], ["Vende cada día."])
            self.assertEqual(doc["acceptance_source"], "proposal")
            self.assertIsNotNone(doc["question_task_id"])
            obj2 = await _objective_cycle("Open the doors")
            self.assertNotEqual(obj1, obj2)
            doc = await missions.get_mission(mission_id)
            assert doc is not None
            self.assertEqual(len(doc["objectives"]), 2)
            outcome = await _ceo_review({
                "action": "close_mission",
                "summary": "Vende cada día desde el lunes.",
                "mission": {"mission_id": mission_id},
            })
            self.assertEqual(outcome["action"], "mission_closed")
            doc = await missions.get_mission(mission_id)
            assert doc is not None
            self.assertEqual(doc["status"], "closed")
            self.assertEqual(len(doc["delivery"]["evidence"]), 2)


if __name__ == "__main__":
    unittest.main()
