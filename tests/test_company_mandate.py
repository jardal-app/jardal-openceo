"""Company mandate and owner escalation (TASK-2026-00114, F4).

Acceptance with zero tokens: the versioned YAML loads validated and fails
closed; requires_owner escalates exactly the six contract categories and
automates everything else; the CEO cannot edit its own mandate; an
escalation persists ERPNext-first and parks in waiting/Pending Review with
the exact human action. No network, no CLI, no LLM.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
REPO = Path(__file__).resolve().parents[1]

from company_mandate import (  # noqa: E402
    ESCALATION_PHASE,
    EscalationCategory,
    EscalationDecision,
    MandateError,
    OwnerAction,
    apply_mandate_change,
    build_escalation_ticket,
    load_mandate_file,
    parse_mandate,
    project_requires_human_review,
    requires_owner,
)
from config_loader import ConfigLoader  # noqa: E402
from settings import Settings  # noqa: E402
from state import TaskPriority  # noqa: E402
from task_service import TaskService  # noqa: E402
from tools.erpnext_tickets import resolve_erpnext_status  # noqa: E402

SIX = [
    "gasto_nuevo",
    "credencial",
    "accion_irreversible",
    "replanificacion_estrategica",
    "cambio_de_mandato",
    "revision_exigida_por_politica",
]


def _valid_mandate_dict() -> dict[str, Any]:
    return {
        "version": 1,
        "ejes": [
            {
                "id": "crecimiento",
                "meta": "crecer de forma sostenible",
                "kpis": [{"id": "mrr", "fuente": "erpnext.finance"}],
            },
        ],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": "100.00",
            "cuota": "suscripciones_ya_pagadas",
        },
        "limites": {
            "objetivos_activos": 3,
            "objetivos_activos_por_proyecto": 2,
            "tickets_activos_por_objetivo": 6,
        },
        "revision_ceo": {"cadencia": "semanal"},
        "escalar_al_dueno": list(SIX),
    }


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class MandateLoadTest(unittest.TestCase):
    def test_valid_dict_parses(self) -> None:
        mandate = parse_mandate(_valid_mandate_dict())
        self.assertEqual(mandate.limites.objetivos_activos, 3)
        self.assertEqual(mandate.ejes[0].kpis[0].fuente, "erpnext.finance")

    def test_versioned_yaml_loads(self) -> None:
        mandate = load_mandate_file(REPO / "projects" / "company_mandate.yaml")
        self.assertEqual(mandate.ejes, [])
        self.assertEqual(str(mandate.presupuesto.gasto_nuevo_sin_preguntar), "0.00")

    def test_empty_axes_is_valid(self) -> None:
        data = _valid_mandate_dict()
        data["ejes"] = []
        self.assertEqual(parse_mandate(data).ejes, [])

    def test_axis_without_kpis_is_valid(self) -> None:
        data = _valid_mandate_dict()
        data["ejes"] = [{"id": "e", "meta": "m", "kpis": []}]
        self.assertEqual(parse_mandate(data).ejes[0].kpis, [])

    def test_non_mapping_rejected(self) -> None:
        for bad in (None, [], "x", 42):
            with self.subTest(bad=bad):
                with self.assertRaises(MandateError):
                    parse_mandate(bad)

    def test_invalid_dicts_rejected(self) -> None:
        cases: list[tuple[str, Any]] = [
            ("bad version", {"version": 2}),
            ("unknown category", {"escalar_al_dueno": [*SIX[:5], "propina"]}),
            ("missing category", {"escalar_al_dueno": SIX[:5]}),
            ("extra category", {"escalar_al_dueno": [*SIX, "propina"]}),
            ("zero objectives", {"limites": {"objetivos_activos": 0}}),
            ("negative tickets", {"limites": {"tickets_activos_por_objetivo": -1}}),
            ("negative threshold", {"presupuesto": {"gasto_nuevo_sin_preguntar": "-1"}}),
            ("non-numeric threshold", {"presupuesto": {"gasto_nuevo_sin_preguntar": "mucho"}}),
            ("bad currency", {"presupuesto": {"moneda": "dolar"}}),
            ("blank quota", {"presupuesto": {"cuota": "  "}}),
            ("bad cadence", {"revision_ceo": {"cadencia": "anual"}}),
            ("duplicate axes", {"ejes": [
                {"id": "a", "meta": "m"}, {"id": "a", "meta": "m"}]}),
            ("duplicate kpis", {"ejes": [
                {"id": "a", "meta": "m", "kpis": [
                    {"id": "k", "fuente": "s.r"}, {"id": "k", "fuente": "s.r"}]}]}),
            ("blank kpi source", {"ejes": [
                {"id": "a", "meta": "m", "kpis": [{"id": "k", "fuente": " "}]}]}),
            ("blank axis goal", {"ejes": [{"id": "a", "meta": " "}]}),
            ("missing budget", {"presupuesto": None}),
        ]
        for name, override in cases:
            with self.subTest(name):
                data = _valid_mandate_dict()
                for key, value in override.items():
                    if value is None:
                        data.pop(key, None)
                    elif isinstance(value, dict) and isinstance(data.get(key), dict):
                        data[key] = {**data[key], **value}
                    else:
                        data[key] = value
                with self.assertRaises(MandateError):
                    parse_mandate(data)

    def test_file_errors_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            with self.assertRaises(MandateError):
                load_mandate_file(tmpdir / "missing.yaml")
            with self.assertRaises(MandateError):
                load_mandate_file(_write(tmpdir / "broken.yaml", "ejes: [unclosed"))
            with self.assertRaises(MandateError):
                load_mandate_file(_write(tmpdir / "empty.yaml", ""))
            with self.assertRaises(MandateError):
                load_mandate_file(_write(tmpdir / "list.yaml", "- just\n- a\n- list\n"))


class RequiresOwnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mandate = parse_mandate(_valid_mandate_dict())

    def test_each_category_escalates(self) -> None:
        cases = [
            (OwnerAction(kind="gasto", summary="comprar dominio", amount="150.00"),
             EscalationCategory.NEW_SPEND),
            (OwnerAction(kind="alta", summary="crear cuenta", needs_owner_credential=True),
             EscalationCategory.CREDENTIAL),
            (OwnerAction(kind="borrado", summary="borrar prod", irreversible=True),
             EscalationCategory.IRREVERSIBLE),
            (OwnerAction(kind="replan", summary="mover fecha", strategic_replan=True),
             EscalationCategory.STRATEGIC_REPLAN),
            (OwnerAction(kind="mandato", summary="subir presupuesto", mandate_change=True),
             EscalationCategory.MANDATE_CHANGE),
            (OwnerAction(kind="entrega", summary="entregar", project="acme",
                         policy_requires_human_review=True),
             EscalationCategory.POLICY_REVIEW),
        ]
        for action, category in cases:
            with self.subTest(category=category.value):
                decision = requires_owner(action, self.mandate)
                self.assertTrue(decision.escalate)
                self.assertEqual(decision.category, category)
                self.assertTrue(str(decision.human_action or "").strip())
                self.assertIn(action.summary, str(decision.human_action))

    def test_routine_work_is_automatic(self) -> None:
        kinds = ["crear_ticket", "replan_rutina", "desarrollo", "merge",
                 "reinicio", "reintento", "revision_agentes"]
        for kind in kinds:
            with self.subTest(kind=kind):
                decision = requires_owner(
                    OwnerAction(kind=kind, summary="trabajo rutinario"), self.mandate)
                self.assertFalse(decision.escalate)
                self.assertIsNone(decision.category)
                self.assertIsNone(decision.human_action)

    def test_spend_under_or_at_threshold_is_automatic(self) -> None:
        for amount in (None, 0, "0", "0.00", "50", "99.99", "100", "100.00"):
            with self.subTest(amount=amount):
                decision = requires_owner(
                    OwnerAction(kind="gasto", summary="g", amount=amount),
                    self.mandate,
                )
                self.assertFalse(decision.escalate)

    def test_paid_quota_is_not_new_spend(self) -> None:
        for amount in (None, 0, "0.00"):
            with self.subTest(amount=amount):
                decision = requires_owner(
                    OwnerAction(kind="dev", summary="usar cuota",
                                amount=amount, uses_paid_quota=True),
                    self.mandate,
                )
                self.assertFalse(decision.escalate)

    def test_paid_quota_with_amount_escalates(self) -> None:
        decision = requires_owner(
            OwnerAction(kind="dev", summary="incoherente",
                        amount="10", uses_paid_quota=True),
            self.mandate,
        )
        self.assertTrue(decision.escalate)
        self.assertEqual(decision.category, EscalationCategory.NEW_SPEND)

    def test_no_mandate_escalates_everything(self) -> None:
        decision = requires_owner(
            OwnerAction(kind="crear_ticket", summary="rutinario"), None)
        self.assertTrue(decision.escalate)
        self.assertEqual(decision.category, EscalationCategory.MANDATE_UNAVAILABLE)
        self.assertIn("company_mandate.yaml", str(decision.human_action))

    def test_first_contract_signal_wins(self) -> None:
        decision = requires_owner(
            OwnerAction(kind="x", summary="s", mandate_change=True,
                        needs_owner_credential=True),
            self.mandate,
        )
        self.assertEqual(decision.category, EscalationCategory.CREDENTIAL)
        decision = requires_owner(
            OwnerAction(kind="x", summary="s", strategic_replan=True,
                        policy_requires_human_review=True),
            self.mandate,
        )
        self.assertEqual(decision.category, EscalationCategory.STRATEGIC_REPLAN)

    def test_human_action_hint_wins(self) -> None:
        decision = requires_owner(
            OwnerAction(kind="gasto", summary="s", amount="500",
                        human_action_hint="Llama al proveedor y paga."),
            self.mandate,
        )
        self.assertEqual(decision.human_action, "Llama al proveedor y paga.")

    def test_malformed_actions_rejected(self) -> None:
        bad = [
            OwnerAction(kind=" ", summary="s"),
            OwnerAction(kind="k", summary="  "),
            OwnerAction(kind="k", summary="s", amount=1.5),
            OwnerAction(kind="k", summary="s", amount=True),
            OwnerAction(kind="k", summary="s", amount="-5"),
            OwnerAction(kind="k", summary="s", amount="abc"),
        ]
        for action in bad:
            with self.subTest(action=action):
                with self.assertRaises(MandateError):
                    requires_owner(action, self.mandate)


class ProjectPolicyTest(unittest.TestCase):
    def test_require_human_review_signal(self) -> None:
        self.assertFalse(project_requires_human_review(None))
        self.assertFalse(project_requires_human_review({}))
        self.assertFalse(project_requires_human_review({"dev": {}}))
        self.assertFalse(project_requires_human_review({"dev": {"qa": {}}}))
        self.assertFalse(project_requires_human_review({"dev": "x"}))
        self.assertFalse(project_requires_human_review(
            {"dev": {"qa": {"require_human_review": "true"}}}))
        self.assertFalse(project_requires_human_review(
            {"dev": {"qa": {"require_human_review": 1}}}))
        self.assertTrue(project_requires_human_review(
            {"dev": {"qa": {"require_human_review": True}}}))

    def test_default_is_false(self) -> None:
        loader = ConfigLoader(Settings())
        merged = loader.load_project(None)
        self.assertIs(merged["dev"]["qa"]["require_human_review"], False)
        self.assertFalse(project_requires_human_review(merged))


class ConfigLoaderMandateTest(unittest.TestCase):
    def test_mandate_path_and_load(self) -> None:
        loader = ConfigLoader(Settings())
        self.assertEqual(loader.company_mandate_path().name, "company_mandate.yaml")
        mandate = loader.load_company_mandate()
        self.assertEqual(mandate.limites.objetivos_activos, 3)
        self.assertEqual(
            {str(entry) for entry in mandate.escalar_al_dueno}, set(SIX))


class MandateWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "company_mandate.yaml"
        import yaml

        self.path.write_text(
            yaml.safe_dump(_valid_mandate_dict(), sort_keys=False,
                           allow_unicode=True),
            encoding="utf-8",
        )

    def test_owner_change_applies_and_persists(self) -> None:
        for channel in ("voice", "horizon", "ticket"):
            with self.subTest(channel=channel):
                mandate = apply_mandate_change(
                    self.path, {"limites": {"objetivos_activos": 5}},
                    requested_by="owner", channel=channel,
                )
                self.assertEqual(mandate.limites.objetivos_activos, 5)
                self.assertEqual(
                    load_mandate_file(self.path).limites.objetivos_activos, 5)

    def test_ceo_cannot_change_mandate(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        for requested_by in ("ceo", "", "agent", "owner "):
            with self.subTest(requested_by=requested_by):
                with self.assertRaises(MandateError):
                    apply_mandate_change(
                        self.path, {"limites": {"objetivos_activos": 9}},
                        requested_by=requested_by, channel="voice",
                    )
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_bad_channel_rejected(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(MandateError):
            apply_mandate_change(
                self.path, {"limites": {"objetivos_activos": 9}},
                requested_by="owner", channel="api",
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_empty_change_rejected(self) -> None:
        with self.assertRaises(MandateError):
            apply_mandate_change(
                self.path, {}, requested_by="owner", channel="voice")

    def test_invalid_change_rejected_without_writing(self) -> None:
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(MandateError):
            apply_mandate_change(
                self.path, {"limites": {"objetivos_activos": 0}},
                requested_by="owner", channel="voice",
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_creates_missing_file(self) -> None:
        target = Path(self._tmp.name) / "fresh.yaml"
        mandate = apply_mandate_change(
            target, _valid_mandate_dict(),
            requested_by="owner", channel="ticket",
        )
        self.assertEqual(mandate.presupuesto.moneda, "USD")
        self.assertEqual(load_mandate_file(target).presupuesto.moneda, "USD")

    def test_broken_current_file_rejected(self) -> None:
        self.path.write_text("ejes: [unclosed", encoding="utf-8")
        with self.assertRaises(MandateError):
            apply_mandate_change(
                self.path, {"limites": {"objetivos_activos": 9}},
                requested_by="owner", channel="voice",
            )


class EscalationSpecTest(unittest.TestCase):
    def setUp(self) -> None:
        self.mandate = parse_mandate(_valid_mandate_dict())

    def test_automatic_decision_has_no_spec(self) -> None:
        decision = requires_owner(
            OwnerAction(kind="merge", summary="m"), self.mandate)
        with self.assertRaises(MandateError):
            build_escalation_ticket(
                OwnerAction(kind="merge", summary="m"), decision)

    def test_missing_human_action_rejected(self) -> None:
        decision = EscalationDecision(
            escalate=True, category=EscalationCategory.CREDENTIAL,
            reason="r", human_action="  ")
        with self.assertRaises(MandateError):
            build_escalation_ticket(
                OwnerAction(kind="k", summary="s"), decision)

    def test_spec_carries_exact_human_action(self) -> None:
        action = OwnerAction(
            kind="gasto", summary="comprar dominio ejemplo.com",
            project="acme", amount="150.00",
            human_action_hint="Aprueba la compra del dominio.",
            details={"proveedor": "x", "monto": "150.00 USD"},
        )
        decision = requires_owner(action, self.mandate)
        spec = build_escalation_ticket(action, decision)
        self.assertTrue(spec["title"].startswith("[Escalado]"))
        self.assertIn("comprar dominio ejemplo.com", spec["title"])
        self.assertIn("Aprueba la compra del dominio.", spec["description"])
        self.assertIn("gasto_nuevo", spec["description"])
        self.assertIn("Proyecto: acme", spec["description"])
        self.assertIn("proveedor=x", spec["description"])
        self.assertIn("escalado-dueno", spec["tags"])
        self.assertIn("escalado-gasto_nuevo", spec["tags"])
        self.assertEqual(spec["phase"], ESCALATION_PHASE)
        self.assertEqual(spec["waiting_reason"], "owner_escalation:gasto_nuevo")
        self.assertEqual(spec["params"]["escalation"]["category"], "gasto_nuevo")
        self.assertEqual(
            spec["params"]["escalation"]["human_action"],
            "Aprueba la compra del dominio.",
        )


class _FakeProjects:
    async def find_one(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        return None


class _FakeRepo:
    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.events: list[str] = []
        self.projects = _FakeProjects()
        self._number = 500

    async def find_task_by_source(
        self, source: str, source_id: str
    ) -> dict[str, Any] | None:
        for task in self.tasks.values():
            if task.get("source") == source and task.get("source_id") == source_id:
                return deepcopy(task)
        return None

    async def get_next_ticket_number(self) -> int:
        self._number += 1
        return self._number

    async def create_task(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.events.append("mongo_create")
        self.tasks[doc["task_id"]] = deepcopy(doc)
        return deepcopy(doc)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.tasks.get(task_id)
        return deepcopy(task) if task is not None else None

    async def patch_task(
        self, task_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        self.events.append("mongo_patch")
        task = self.tasks.get(task_id)
        if task is None:
            return None
        task.update(deepcopy(updates))
        return deepcopy(task)


class _FakeHub:
    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        del event, payload


class _FakeErp:
    def __init__(self, events: list[str]) -> None:
        self.is_enabled = True
        self.calls: list[dict[str, Any]] = []
        self._events = events

    async def create_ticket(self, **kwargs: Any) -> dict[str, Any]:
        self._events.append("erp_create")
        self.calls.append(kwargs)
        return {"task": {"name": "TASK-2026-09999"}}


class CreateOwnerEscalationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.mandate = parse_mandate(_valid_mandate_dict())
        self.repo = _FakeRepo()
        self.erp = _FakeErp(self.repo.events)
        self.graph = MagicMock()
        self.service = TaskService(
            settings=Settings(),
            repo=self.repo,  # type: ignore[arg-type]
            graph=self.graph,
            realtime_hub=_FakeHub(),  # type: ignore[arg-type]
        )
        self.service._erpnext = self.erp  # noqa: SLF001

    def _spend(self) -> tuple[OwnerAction, EscalationDecision]:
        action = OwnerAction(
            kind="gasto", summary="comprar dominio ejemplo.com",
            project="acme", amount="150.00")
        return action, requires_owner(action, self.mandate)

    async def test_escalation_parks_waiting_with_human_action(self) -> None:
        action, decision = self._spend()
        assert decision.human_action is not None
        ticket = await self.service.create_owner_escalation(
            action=action, decision=decision, source_id="esc-1")
        self.assertEqual(ticket["status"], "waiting")
        self.assertEqual(ticket["phase"], "owner_escalation")
        self.assertEqual(ticket["waiting_reason"], "owner_escalation:gasto_nuevo")
        self.assertIsNone(ticket.get("resume_at"))
        self.assertIn("escalado-dueno", ticket.get("tags") or [])
        self.assertIn(decision.human_action, ticket.get("description") or "")
        self.assertEqual(ticket.get("priority"), TaskPriority.HIGH.value)
        self.assertEqual(ticket.get("created_by"), "ceo")
        self.assertEqual(ticket.get("workflow_phases"), [])
        self.assertIsNone(ticket.get("workflow_key"))
        stored = await self.repo.get_task(ticket["task_id"])
        assert stored is not None
        mirror = (stored.get("params") or {}).get("_mirrors", {}).get("erpnext", {})
        self.assertEqual(mirror.get("name"), "TASK-2026-09999")
        self.graph.execute.assert_not_called()

    async def test_erpnext_first_then_park(self) -> None:
        action, decision = self._spend()
        await self.service.create_owner_escalation(
            action=action, decision=decision, source_id="esc-2")
        self.assertEqual(
            self.repo.events, ["erp_create", "mongo_create", "mongo_patch"])
        self.assertEqual(len(self.erp.calls), 1)
        self.assertIn("comprar dominio", self.erp.calls[0]["title"])

    async def test_waiting_pushes_pending_review(self) -> None:
        action, decision = self._spend()
        ticket = await self.service.create_owner_escalation(
            action=action, decision=decision, source_id="esc-3")
        await self.service.observe_task_status(ticket)
        item = self.service._erpnext_queue.get_nowait()  # noqa: SLF001
        self.assertEqual(item["status"], "waiting")
        self.assertEqual(item["name"], "TASK-2026-09999")
        self.assertEqual(
            resolve_erpnext_status({"status": item["status"]}), "Pending Review")

    async def test_same_source_id_is_idempotent(self) -> None:
        action, decision = self._spend()
        first = await self.service.create_owner_escalation(
            action=action, decision=decision, source_id="esc-dup")
        second = await self.service.create_owner_escalation(
            action=action, decision=decision, source_id="esc-dup")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(self.erp.calls), 1)
        self.assertEqual(len(self.repo.tasks), 1)

    async def test_automatic_decision_raises_before_creating(self) -> None:
        action = OwnerAction(kind="merge", summary="m")
        decision = requires_owner(action, self.mandate)
        with self.assertRaises(ValueError):
            await self.service.create_owner_escalation(
                action=action, decision=decision)
        self.assertEqual(self.erp.calls, [])
        self.assertEqual(self.repo.tasks, {})


if __name__ == "__main__":
    unittest.main()
