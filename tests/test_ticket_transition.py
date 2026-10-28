"""Transicion explicita de estado de negocio (TASK-2026-00132).

Contrato que se fija aqui:

- el criterio global vive en el servidor: `reason` siempre, `human_action`
  para Pending Review (nunca defecto), `evidence` para Completed, y el gate
  de QA no lo salta la evidencia;
- cancelar es fail-closed: solo lo probadamente creado por agentes se
  cancela solo; lo demas exige citar la orden del dueno y, sin ella, crea
  el escalado en vez de cancelar;
- el 417 por dependientes abiertos se reporta con detalle (nombres + depends);
- idempotente: relee el negocio y lo devuelve; si la ejecucion no terminal
  discrepa, la alinea (reparacion, no resurreccion);
- el espejo queda coherente sin eco: ni el observador reenvia ni el webhook
  rebota (`status_pushed` en vocabulario OpenCEO);
- superficie MCP: `openceo_transition_ticket` + batch secuencial verificado.

Jardal ERP simulado (fakes al estilo `test_business_hierarchy.py`); 0 tokens.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import mcp_server  # noqa: E402
from company_mandate import EscalationCategory, parse_mandate  # noqa: E402
from state import TaskSource  # noqa: E402
from task_service import TaskService, _transition_cancel_needs_owner  # noqa: E402
from tools.jardal_erp_client import (  # noqa: E402
    JardalERPDependencyBlockedError,
    JardalERPUnavailable,
    JardalERPValidationError,
)
from tools.jardal_erp_tickets import (  # noqa: E402
    JardalERPTicketAdapter,
    normalize_transition_status,
)


def run(coro):
    return asyncio.run(coro)


BLOCKED_MESSAGE = (
    "frappe.exceptions.ValidationError: Cannot complete task "
    "<strong>TASK-2026-00010</strong> as its dependant task "
    "<strong>TASK-2026-00011</strong> are not completed / cancelled."
)


def _valid_mandate_dict() -> dict:
    return {
        "version": 1,
        "ejes": [],
        "presupuesto": {
            "moneda": "USD",
            "gasto_nuevo_sin_preguntar": "0",
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
    }


class FakeBusinessAdapter:
    """Jardal ERP en memoria: resuelve, transiciona y relee como el adapter real."""

    is_enabled = True

    def __init__(self, docs=None, *, failures=None, ignore_puts=False):
        self.docs = {doc["name"]: dict(doc) for doc in (docs or [])}
        self.ops: list[str] = []
        self.puts: list[tuple] = []
        self.failures = list(failures or [])
        self.ignore_puts = ignore_puts

    async def resolve_task(self, identifier):
        self.ops.append("resolve")
        key = str(identifier or "").strip()
        if key in self.docs:
            return dict(self.docs[key])
        for doc in self.docs.values():
            if str(doc.get("openceo_ref") or "") == key:
                return dict(doc)
        raise JardalERPValidationError(
            f"No existe el ticket {identifier}.", status_code=404
        )

    async def transition_status(self, identifier, to_status, *, work_dates=None):
        self.ops.append("put")
        if self.failures:
            raise self.failures.pop(0)
        doc = await self.resolve_task(identifier)
        name = str(doc.get("name"))
        self.puts.append((name, to_status, dict(work_dates or {})))
        if not self.ignore_puts:
            self.docs[name] = {**doc, "status": to_status, **dict(work_dates or {})}
        return {"ok": True, "ticket": {"name": name, "status": to_status}}


class FakeRepo:
    def __init__(self, tasks=None):
        self.tasks = {t["task_id"]: dict(t) for t in (tasks or [])}
        self.patches: list[tuple] = []

    async def get_task(self, task_id):
        doc = self.tasks.get(str(task_id))
        return dict(doc) if doc else None

    async def patch_task(self, task_id, updates):
        doc = self.tasks.get(str(task_id))
        if doc is None:
            return None
        merged = {**doc, **dict(updates)}
        self.tasks[str(task_id)] = merged
        self.patches.append((str(task_id), dict(updates)))
        return dict(merged)

    async def find_task_by_source(self, source, source_id):
        for doc in self.tasks.values():
            if str(doc.get("source") or "") == str(source) and str(
                doc.get("source_id") or ""
            ) == str(source_id):
                return dict(doc)
        return None


class FakeHub:
    def __init__(self):
        self.events: list[tuple] = []

    async def broadcast(self, event, payload):
        self.events.append((event, payload))


def business_doc(name="TASK-2026-00010", status="Working", **over):
    doc = {
        "name": name,
        "subject": f"Asunto {name}",
        "status": status,
        "project": "PROJ-0001",
        "openceo_ref": f"api:{name}",
        "openceo_task_id": "exec-1",
        "is_group": False,
        "parent_task": None,
    }
    doc.update(over)
    return doc


def exec_doc(task_id="exec-1", status="in_progress", **over):
    doc = {
        "task_id": task_id,
        "status": status,
        "project": "jardal-openceo",
        "source": TaskSource.API.value,
        "source_id": "x",
        "tags": [],
        "params": {
            "_mirrors": {
                "erpnext": {
                    "name": "TASK-2026-00010",
                    "status_pushed": "in_progress",
                }
            }
        },
    }
    doc.update(over)
    return doc


def make_service(adapter, repo):
    service = TaskService.__new__(TaskService)
    service._business = adapter
    service._repo = repo
    service._hub = FakeHub()
    service._horizon_terminal = None
    service._config_loader = None
    return service


def mirror_of(task):
    return ((task.get("params") or {}).get("_mirrors") or {}).get("erpnext") or {}


class NormalizeTests(unittest.TestCase):
    def test_canonicos_pasan(self):
        for target in ("Open", "Working", "Pending Review", "Completed", "Cancelled"):
            self.assertEqual(normalize_transition_status(target), target)

    def test_insensible_a_mayusculas_y_separadores(self):
        self.assertEqual(normalize_transition_status("completed"), "Completed")
        self.assertEqual(normalize_transition_status("pending_review"), "Pending Review")
        self.assertEqual(normalize_transition_status("PENDING-REVIEW"), "Pending Review")
        self.assertEqual(normalize_transition_status("  working "), "Working")

    def test_desconocido_es_400(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            normalize_transition_status("Done")
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(JardalERPValidationError):
            normalize_transition_status("")


class CancelRuleTests(unittest.TestCase):
    def test_grupo_siempre_pregunta(self):
        needs, _ = _transition_cancel_needs_owner(
            {"is_group": True}, {"objective_id": "OBJ-1"}
        )
        self.assertTrue(needs)

    def test_escalado_siempre_pregunta(self):
        for task in (
            {"phase": "owner_escalation"},
            {"waiting_reason": "owner_escalation:irreversible"},
            {"tags": ["escalado-dueno"]},
        ):
            needs, _ = _transition_cancel_needs_owner({"is_group": False}, task)
            self.assertTrue(needs, task)

    def test_marcas_de_agente_no_preguntan(self):
        for task in (
            {"objective_id": "OBJ-1"},
            {"source": "spillover"},
            {"source": TaskSource.SPILLOVER},
            {"tags": ["spillover_from:abc"]},
            {"created_by": "ceo"},
        ):
            needs, _ = _transition_cancel_needs_owner({"is_group": False}, task)
            self.assertFalse(needs, task)

    def test_suelto_y_desconocido_preguntan(self):
        needs, _ = _transition_cancel_needs_owner(
            {"is_group": False}, {"source": "api", "tags": []}
        )
        self.assertTrue(needs)
        needs, _ = _transition_cancel_needs_owner({"is_group": False}, None)
        self.assertTrue(needs)
        needs, _ = _transition_cancel_needs_owner(None, None)
        self.assertTrue(needs)


class CriteriaTests(unittest.TestCase):
    def _service(self, business=None, task=None):
        adapter = FakeBusinessAdapter(
            [business or business_doc()], failures=None
        )
        repo = FakeRepo([task] if task is not None else [exec_doc()])
        return make_service(adapter, repo), adapter, repo

    def test_sin_adapter_es_not_configured(self):
        service = make_service(None, FakeRepo())
        result = run(service.transition_ticket("TASK-1", "Working", "x"))
        self.assertEqual(result["status"], "not_configured")

    def test_destino_invalido_y_reason_vacia(self):
        service, _, _ = self._service()
        result = run(service.transition_ticket("TASK-2026-00010", "Done", "x"))
        self.assertEqual(result["status"], "invalid_input")
        result = run(service.transition_ticket("TASK-2026-00010", "Working", "  "))
        self.assertEqual(result["status"], "invalid_input")

    def test_ticket_inexistente_es_not_found(self):
        service, _, _ = self._service()
        result = run(service.transition_ticket("TASK-2026-00999", "Working", "x"))
        self.assertEqual(result["status"], "not_found")

    def test_pending_review_sin_human_action_no_mueve_nada(self):
        service, adapter, repo = self._service()
        result = run(
            service.transition_ticket("TASK-2026-00010", "Pending Review", "revisar")
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(adapter.puts, [])
        self.assertEqual(repo.patches, [])

    def test_completed_sin_evidence_no_mueve_nada(self):
        service, adapter, repo = self._service()
        result = run(service.transition_ticket("TASK-2026-00010", "Completed", "fin"))
        self.assertFalse(result["ok"])
        self.assertEqual(adapter.puts, [])
        self.assertEqual(repo.patches, [])

    def test_completed_con_qa_pendiente_exige_revision(self):
        task = exec_doc(qa_review_required=True, qa_review_state="pending")
        service, adapter, repo = self._service(task=task)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="tests verdes"
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "qa_required")
        self.assertEqual(adapter.puts, [])
        self.assertEqual(repo.patches, [])

    def test_completed_con_qa_aprobada_aplica(self):
        task = exec_doc(qa_review_required=True, qa_review_state="approved")
        service, adapter, _ = self._service(task=task)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="tests verdes"
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "applied")
        self.assertEqual(adapter.docs["TASK-2026-00010"]["status"], "Completed")

    def test_open_y_working_solo_exigen_reason(self):
        service, adapter, _ = self._service(
            business=business_doc(status="Open"), task=exec_doc(status="queued")
        )
        result = run(service.transition_ticket("TASK-2026-00010", "Working", "retomo"))
        self.assertEqual(result["outcome"], "applied")
        self.assertEqual(result["task_status"], "in_progress")


class ApplyTests(unittest.TestCase):
    def test_completed_relee_verifica_y_alinea_el_espejo(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="suite verde"
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "applied")
        self.assertEqual(result["business_status"], "Completed")
        self.assertEqual(result["previous_status"], "Working")
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(result["mirror"], "aligned")
        # Orden: resolver -> PUT -> releer (la relectura verifica).
        self.assertEqual(adapter.ops, ["resolve", "put", "resolve", "resolve"])
        updated = repo.tasks["exec-1"]
        self.assertEqual(updated["status"], "completed")
        mirror = mirror_of(updated)
        self.assertEqual(mirror["status_pushed"], "completed")
        self.assertNotEqual(mirror["status_pushed"], "Completed")
        self.assertEqual(mirror["business_status"], "Completed")
        audit = mirror["transition"]
        self.assertEqual(
            (audit["from"], audit["to"], audit["reason"], audit["evidence"]),
            ("Working", "Completed", "fin", "suite verde"),
        )

    def test_pending_review_marca_aprobacion_pendiente(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010",
                "Pending Review",
                "revisar",
                human_action="Aprueba el diseno",
            )
        )
        self.assertEqual(result["outcome"], "applied")
        updated = repo.tasks["exec-1"]
        self.assertEqual(updated["status"], "waiting")
        self.assertTrue(updated["approval_required"])
        self.assertEqual(updated["approval_state"], "pending")
        self.assertEqual(updated["waiting_reason"], "Aprueba el diseno")
        self.assertEqual(mirror_of(updated)["status_pushed"], "waiting")

    def test_cancel_de_hijo_de_objetivo_es_automatico(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        task = exec_doc(status="in_progress", objective_id="OBJ-1")
        repo = FakeRepo([task])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket("TASK-2026-00010", "Cancelled", "duplicado")
        )
        self.assertEqual(result["outcome"], "applied")
        updated = repo.tasks["exec-1"]
        self.assertEqual(updated["status"], "cancelled")
        self.assertIsNone(updated.get("resume_at"))
        self.assertIsNone(updated.get("waiting_reason"))
        self.assertIn("duplicado", updated.get("error") or "")

    def test_cancel_con_orden_del_dueno_aplica_y_la_cita(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010",
                "Cancelled",
                "lo asume el dueno",
                owner_instruction="cancela el 10, lo hago yo",
            )
        )
        self.assertEqual(result["outcome"], "applied")
        audit = mirror_of(repo.tasks["exec-1"])["transition"]
        self.assertEqual(audit["owner_instruction"], "cancela el 10, lo hago yo")

    def test_ticket_de_desk_sin_ejecucion_solo_mueve_negocio(self):
        doc = business_doc(openceo_task_id="", openceo_ref="")
        adapter = FakeBusinessAdapter([doc])
        repo = FakeRepo([])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="verificado"
            )
        )
        self.assertEqual(result["outcome"], "applied")
        self.assertEqual(result["mirror"], "no_execution_record")
        self.assertEqual(repo.patches, [])


class EscalationTests(unittest.TestCase):
    def _service(self, business=None, task=None):
        adapter = FakeBusinessAdapter([business or business_doc(status="Open")])
        repo = FakeRepo([task if task is not None else exec_doc(status="queued")])
        service = make_service(adapter, repo)
        calls: list[tuple] = []

        async def fake_escalate(*, action, decision, source_id=None):
            calls.append((action, decision, source_id))
            return {
                "task_id": "esc-1",
                "status": "waiting",
                "params": {
                    "_mirrors": {"erpnext": {"name": "TASK-2026-00020"}}
                },
            }

        service.create_owner_escalation = fake_escalate  # type: ignore[method-assign]
        return service, adapter, repo, calls

    def test_cancel_sin_orden_escala_y_no_cancela(self):
        service, adapter, _, calls = self._service()
        result = run(
            service.transition_ticket("TASK-2026-00010", "Cancelled", "ya no hace falta")
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "escalated")
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["business_status"], "Open")
        self.assertEqual(adapter.puts, [])
        self.assertEqual(adapter.docs["TASK-2026-00010"]["status"], "Open")
        self.assertEqual(len(calls), 1)
        action, decision, source_id = calls[0]
        self.assertEqual(action.kind, "ticket_cancel")
        self.assertTrue(action.irreversible)
        self.assertTrue(decision.escalate)
        self.assertEqual(source_id, "transition-cancel:TASK-2026-00010")
        self.assertEqual(result["escalation"]["ticket"], "TASK-2026-00020")

    def test_segundo_intento_devuelve_el_escalado_sin_reaparcar(self):
        service, adapter, repo, calls = self._service()
        repo.tasks["esc-1"] = {
            "task_id": "esc-1",
            "status": "completed",
            "source": TaskSource.API.value,
            "source_id": "transition-cancel:TASK-2026-00010",
            "params": {"_mirrors": {"erpnext": {"name": "TASK-2026-00020"}}},
        }
        result = run(
            service.transition_ticket("TASK-2026-00010", "Cancelled", "ya no hace falta")
        )
        self.assertEqual(result["outcome"], "escalated")
        self.assertTrue(result["duplicate"])
        self.assertEqual(calls, [])
        self.assertEqual(adapter.puts, [])
        # Ni siquiera un escalado respondido reabre o cancela solo.
        self.assertEqual(repo.tasks["esc-1"]["status"], "completed")

    def test_cancel_de_grupo_escala_como_replan_estrategica(self):
        business = business_doc(status="Working", is_group=True)
        service, _, _, calls = self._service(business=business)
        result = run(service.transition_ticket("TASK-2026-00010", "Cancelled", "sobra"))
        self.assertEqual(result["outcome"], "escalated")
        action, _, _ = calls[0]
        self.assertTrue(action.strategic_replan)

    def test_con_mandato_la_categoria_es_irreversible(self):
        service, _, _, calls = self._service()
        mandate = parse_mandate(_valid_mandate_dict())
        service._config_loader = SimpleNamespace(
            load_company_mandate=lambda: mandate
        )
        result = run(service.transition_ticket("TASK-2026-00010", "Cancelled", "sobra"))
        self.assertEqual(result["outcome"], "escalated")
        _, decision, _ = calls[0]
        self.assertEqual(decision.category, EscalationCategory.IRREVERSIBLE)


class BlockedTests(unittest.TestCase):
    def test_417_reporta_bloqueo_con_detalle_y_no_toca_nada(self):
        business = business_doc(
            status="Working", depends_on=[{"task": "TASK-2026-00011"}]
        )
        adapter = FakeBusinessAdapter(
            [business],
            failures=[
                JardalERPDependencyBlockedError(BLOCKED_MESSAGE, status_code=417)
            ],
        )
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="x"
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocked_by"], ["TASK-2026-00011"])
        self.assertEqual(result["depends_on"], ["TASK-2026-00011"])
        self.assertIn("TASK-2026-00011", result["message"])
        self.assertEqual(adapter.docs["TASK-2026-00010"]["status"], "Working")
        self.assertEqual(repo.tasks["exec-1"]["status"], "in_progress")
        self.assertEqual(repo.patches, [])


class IdempotencyTests(unittest.TestCase):
    def test_mismo_estado_no_escribe_negocio(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(service.transition_ticket("TASK-2026-00010", "Working", "da igual"))
        self.assertEqual(result["outcome"], "already_applied")
        self.assertEqual(result["business_status"], "Working")
        self.assertFalse(result["mirror_healed"])
        self.assertEqual(adapter.puts, [])
        self.assertEqual(repo.patches, [])

    def test_sana_la_ejecucion_no_terminal_que_discrepa(self):
        adapter = FakeBusinessAdapter([business_doc(status="Completed")])
        task = exec_doc(status="in_progress")
        task["params"]["_mirrors"]["erpnext"]["status_pushed"] = "in_progress"
        repo = FakeRepo([task])
        service = make_service(adapter, repo)
        result = run(service.transition_ticket("TASK-2026-00010", "Completed", "fin"))
        self.assertEqual(result["outcome"], "already_applied")
        self.assertTrue(result["mirror_healed"])
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(adapter.puts, [])
        self.assertEqual(repo.tasks["exec-1"]["status"], "completed")
        self.assertEqual(mirror_of(repo.tasks["exec-1"])["status_pushed"], "completed")

    def test_no_resucita_ejecucion_terminal(self):
        adapter = FakeBusinessAdapter([business_doc(status="Completed")])
        repo = FakeRepo([exec_doc(status="failed")])
        service = make_service(adapter, repo)
        result = run(service.transition_ticket("TASK-2026-00010", "Completed", "fin"))
        self.assertEqual(result["outcome"], "already_applied")
        self.assertFalse(result["mirror_healed"])
        self.assertEqual(repo.patches, [])
        self.assertEqual(repo.tasks["exec-1"]["status"], "failed")


class TerminalTests(unittest.TestCase):
    def test_negocio_cancelled_es_terminal(self):
        adapter = FakeBusinessAdapter([business_doc(status="Cancelled")])
        repo = FakeRepo([exec_doc(status="cancelled")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Working", "reabrir", evidence="x"
            )
        )
        self.assertEqual(result["status"], "terminal")
        self.assertEqual(adapter.puts, [])

    def test_ejecucion_cancelled_es_terminal(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc(status="cancelled")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="x"
            )
        )
        self.assertEqual(result["status"], "terminal")
        self.assertEqual(adapter.puts, [])

    def test_padre_convertido_cierra_por_el_objetivo(self):
        adapter = FakeBusinessAdapter(
            [business_doc(status="Working", is_group=True)]
        )
        repo = FakeRepo([exec_doc(status="converted", objective_id="OBJ-1")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010",
                "Completed",
                "fin",
                evidence="x",
                owner_instruction="cierra",
            )
        )
        self.assertEqual(result["status"], "converted")
        self.assertEqual(adapter.puts, [])


class NoEchoTests(unittest.TestCase):
    def test_observador_no_reenvia_ni_webhook_rebota(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="suite verde"
            )
        )
        self.assertEqual(result["outcome"], "applied")
        updated = repo.tasks["exec-1"]
        service._jardal_erp_queue = asyncio.Queue()
        run(service.observe_task_status(updated))
        self.assertTrue(service._jardal_erp_queue.empty())
        echo = run(
            service.apply_business_status(
                task_id="exec-1", business_status="Completed"
            )
        )
        self.assertEqual(echo["status"], "already_applied")
        pending = run(
            service.apply_business_status(
                task_id="exec-1", business_status="Pending Review"
            )
        )
        self.assertEqual(pending["status"], "ignored")


class AvailabilityTests(unittest.TestCase):
    def test_put_reintenta_y_aplica(self):
        adapter = FakeBusinessAdapter(
            [business_doc(status="Working")],
            failures=[JardalERPUnavailable("caido")],
        )
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="x"
            )
        )
        self.assertEqual(result["outcome"], "applied")

    def test_no_disponible_no_cambia_nada(self):
        adapter = FakeBusinessAdapter(
            [business_doc(status="Working")],
            failures=[JardalERPUnavailable("c") for _ in range(3)],
        )
        task = exec_doc(status="in_progress")
        repo = FakeRepo([task])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="x"
            )
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(adapter.docs["TASK-2026-00010"]["status"], "Working")
        self.assertEqual(repo.patches, [])
        # Sin `pending_status` huesped: la ejecucion no cambio, no hay nada
        # que la reconciliacion deba empujar.
        self.assertNotIn("pending_status", mirror_of(repo.tasks["exec-1"]))

    def test_sin_relectura_no_se_afirma_aplicado(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        resolves = {"n": 0}
        real_resolve = adapter.resolve_task

        async def flaky_resolve(identifier):
            resolves["n"] += 1
            if resolves["n"] >= 3:
                raise JardalERPUnavailable("corte a mitad")
            return await real_resolve(identifier)

        adapter.resolve_task = flaky_resolve  # type: ignore[method-assign]
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="x"
            )
        )
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(repo.patches, [])

    def test_desajuste_no_toca_la_ejecucion(self):
        adapter = FakeBusinessAdapter(
            [business_doc(status="Working")], ignore_puts=True
        )
        repo = FakeRepo([exec_doc(status="in_progress")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Completed", "fin", evidence="x"
            )
        )
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["business_status"], "Working")
        self.assertEqual(repo.patches, [])


class FakeClient:
    """Cliente HTTP minimo: solo get/update existen."""

    def __init__(self, docs=None):
        self.docs = {doc["name"]: dict(doc) for doc in (docs or [])}
        self.updated: list[tuple] = []

    async def get_doc(self, doctype, name):
        return dict(self.docs[str(name)])

    async def update_doc(self, doctype, name, body):
        self.updated.append((doctype, str(name), dict(body)))
        self.docs[str(name)] = {**self.docs[str(name)], **dict(body)}
        return dict(self.docs[str(name)])


def make_adapter(client):
    settings = SimpleNamespace(JARDAL_ERP_TICKETS_PROJECT_COMPANY="Jardal")
    return JardalERPTicketAdapter(settings, client=client)  # type: ignore[arg-type]


class AdapterOpTests(unittest.TestCase):
    def test_transition_status_escribe_solo_status(self):
        client = FakeClient([business_doc(status="Working")])
        adapter = make_adapter(client)
        result = run(adapter.transition_status("TASK-2026-00010", "Completed"))
        self.assertTrue(result["ok"])
        self.assertEqual(
            client.updated, [("Task", "TASK-2026-00010", {"status": "Completed"})]
        )
        self.assertEqual(result["ticket"]["status"], "Completed")

    def test_transition_status_normaliza_y_rechaza(self):
        client = FakeClient([business_doc(status="Working")])
        adapter = make_adapter(client)
        run(adapter.transition_status("TASK-2026-00010", "pending_review"))
        self.assertEqual(client.docs["TASK-2026-00010"]["status"], "Pending Review")
        with self.assertRaises(JardalERPValidationError):
            run(adapter.transition_status("TASK-2026-00010", "Done"))

    def test_transition_status_vacia_coding_tool_v1_congelado(self):
        """TASK-2026-00043: un valor v1 (p. ej. `codex`) congela CUALQUIER
        escritura del documento (el Select v2 lo rechaza con 417); el mismo
        PUT lo vacia para descongelar, sin inventar una letra."""
        client = FakeClient(
            [business_doc(status="Open", openceo_coding_tool="codex")]
        )
        adapter = make_adapter(client)
        result = run(adapter.transition_status("TASK-2026-00010", "Completed"))
        self.assertTrue(result["ok"])
        self.assertEqual(
            client.updated,
            [
                (
                    "Task",
                    "TASK-2026-00010",
                    {"status": "Completed", "openceo_coding_tool": None},
                )
            ],
        )
        self.assertIsNone(client.docs["TASK-2026-00010"]["openceo_coding_tool"])

    def test_transition_status_no_toca_coding_tool_vigente(self):
        """Una letra v2 o un campo vacio viajan sin el campo extra."""
        for stored in ("tier_b", "", None):
            with self.subTest(stored=stored):
                client = FakeClient(
                    [business_doc(status="Open", openceo_coding_tool=stored)]
                )
                adapter = make_adapter(client)
                run(adapter.transition_status("TASK-2026-00010", "Completed"))
                self.assertEqual(
                    client.updated,
                    [("Task", "TASK-2026-00010", {"status": "Completed"})],
                )

    def test_retier_ticket_escribe_solo_coding_tool(self):
        """TASK-2026-00148: reclasificacion por PUT cerrado del campo."""
        client = FakeClient(
            [business_doc(status="Open", openceo_coding_tool="tier_b")]
        )
        adapter = make_adapter(client)
        result = run(adapter.retier_ticket("TASK-2026-00010", "tier_c"))
        self.assertTrue(result["ok"])
        self.assertEqual(
            client.updated,
            [("Task", "TASK-2026-00010", {"openceo_coding_tool": "tier_c"})],
        )
        self.assertEqual(
            client.docs["TASK-2026-00010"]["openceo_coding_tool"], "tier_c")

    def test_retier_ticket_normaliza_mayusculas(self):
        client = FakeClient(
            [business_doc(status="Open", openceo_coding_tool="tier_b")]
        )
        adapter = make_adapter(client)
        run(adapter.retier_ticket("TASK-2026-00010", "  TIER_C "))
        self.assertEqual(
            client.updated,
            [("Task", "TASK-2026-00010", {"openceo_coding_tool": "tier_c"})],
        )

    def test_retier_ticket_rechaza_valor_no_vigente_sin_escribir(self):
        """Un valor fuera del Select haria fallar el PUT entero: se
        rechaza antes de escribir nada."""
        for bad in ("codex", "tier9", "", "  ", "tier_cx"):
            with self.subTest(bad=bad):
                client = FakeClient(
                    [business_doc(status="Open",
                                  openceo_coding_tool="tier_b")]
                )
                adapter = make_adapter(client)
                with self.assertRaises(JardalERPValidationError):
                    run(adapter.retier_ticket("TASK-2026-00010", bad))
                self.assertEqual(client.updated, [])


class FakeMCPServer:
    """Servidor FastMCP minimo: guarda las funciones registradas por nombre."""

    def __init__(self) -> None:
        self.tools: dict[str, tuple] = {}

    def tool(self, fn, *, name, description):
        self.tools[name] = (fn, description)


class FakeTaskService:
    def __init__(self, handler=None):
        self.handler = handler or (lambda *a, **k: {"ok": True, "outcome": "applied"})
        self.calls: list[tuple] = []

    async def transition_ticket(self, ticket, to_status, reason, **kwargs):
        self.calls.append((ticket, to_status, reason, kwargs))
        return self.handler(ticket, to_status, reason, **kwargs)


NEW_TOOLS = (
    "openceo_transition_ticket",
    "openceo_transition_tickets_batch",
)


class McpSurfaceTests(unittest.TestCase):
    def _register(self, task_service):
        server = FakeMCPServer()
        settings = SimpleNamespace()
        names = mcp_server._register_openceo_tools(server, settings, task_service, None)
        return server, names

    def test_las_herramientas_se_anuncian(self):
        server, names = self._register(FakeTaskService())
        for name in NEW_TOOLS:
            self.assertIn(name, names)
            self.assertIn(name, server.tools)

    def test_descripciones_cortas(self):
        server, _ = self._register(FakeTaskService())
        for name in NEW_TOOLS:
            description = server.tools[name][1]
            self.assertLessEqual(len(description), 220, f"{name} es demasiado larga")
            self.assertLessEqual(len(description.split()), 32, f"{name} usa demasiadas palabras")

    def test_pasa_los_argumentos_al_servicio(self):
        svc = FakeTaskService()
        server, _ = self._register(svc)
        result = run(
            server.tools["openceo_transition_ticket"][0](
                ticket="TASK-1",
                to_status="Pending Review",
                reason="r",
                human_action="h",
                evidence="e",
                owner_instruction="o",
            )
        )
        self.assertTrue(result["ok"])
        (ticket, to_status, reason, kwargs), = svc.calls
        self.assertEqual(
            (ticket, to_status, reason),
            ("TASK-1", "Pending Review", "r"),
        )
        self.assertEqual(
            kwargs,
            {"human_action": "h", "evidence": "e", "owner_instruction": "o"},
        )

    def test_sin_task_service_es_not_configured(self):
        server, _ = self._register(None)
        result = run(
            server.tools["openceo_transition_ticket"][0](
                ticket="TASK-1", to_status="Working", reason="r"
            )
        )
        self.assertEqual(result["status"], "not_configured")
        batch = run(
            server.tools["openceo_transition_tickets_batch"][0](transitions="[]")
        )
        self.assertEqual(batch["status"], "not_configured")

    def test_batch_cuenta_y_no_aborta_por_item(self):
        def handler(ticket, to_status, reason, **kwargs):
            if ticket == "TASK-1":
                return {"ok": True, "outcome": "applied", "ticket": ticket}
            if ticket == "TASK-2":
                return {"ok": True, "outcome": "escalated", "ticket": ticket}
            if ticket == "TASK-3":
                raise RuntimeError("raro")
            return {"ok": False, "status": "blocked", "ticket": ticket}

        server, _ = self._register(FakeTaskService(handler))
        result = run(
            server.tools["openceo_transition_tickets_batch"][0](
                transitions=json.dumps(
                    [
                        {"ticket": "TASK-1", "to_status": "Working", "reason": "r"},
                        {"ticket": "TASK-2", "to_status": "Cancelled", "reason": "r"},
                        {"ticket": "TASK-3", "to_status": "Working", "reason": "r"},
                        {"ticket": "TASK-4", "to_status": "Completed", "reason": "r"},
                        {"to_status": "Working", "reason": "sin ticket"},
                    ]
                )
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["applied_count"], 1)
        self.assertEqual(result["escalated_count"], 1)
        self.assertEqual(result["failed_count"], 3)
        self.assertEqual(len(result["results"]), 5)

    def test_batch_rechaza_json_malo_y_lote_grande(self):
        server, _ = self._register(FakeTaskService())
        bad = run(server.tools["openceo_transition_tickets_batch"][0](transitions="{"))
        self.assertEqual(bad["status"], "invalid_input")
        not_array = run(
            server.tools["openceo_transition_tickets_batch"][0](transitions="{}")
        )
        self.assertEqual(not_array["status"], "invalid_input")
        big = run(
            server.tools["openceo_transition_tickets_batch"][0](
                transitions=json.dumps([{"ticket": f"T-{n}"} for n in range(51)])
            )
        )
        self.assertEqual(big["status"], "invalid_input")


SECRET_MARKER_A = {
    "secret": "X_API_KEY",
    "sources_checked": ["env", ".env"],
    "reason": "Bloqueado por secreto ausente: X_API_KEY.",
    "reported_at": "2026-10-07T00:00:00+00:00",
}

SECRET_MARKER_B = {
    "secret": "Y_TOKEN",
    "sources_checked": ["env"],
    "reason": "Bloqueado por secreto ausente: Y_TOKEN.",
    "reported_at": "2026-10-07T01:00:00+00:00",
}


def exec_doc_with_marker(marker, status="in_progress", pushed="in_progress"):
    return exec_doc(
        status=status,
        params={
            "secret_block": dict(marker),
            "_mirrors": {
                "erpnext": {
                    "name": "TASK-2026-00010",
                    "status_pushed": pushed,
                }
            },
        },
    )


class SecretBlockTransitionTests(unittest.TestCase):
    """Marcador de secreto ausente (TASK-2026-00142): solo aparca en Working,
    se escribe con la transicion y se limpia al salir de Working."""

    def test_marcador_solo_aparca_en_working(self):
        adapter = FakeBusinessAdapter([business_doc(status="Open")])
        repo = FakeRepo([exec_doc(status="queued")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Open", "x",
                secret_block=dict(SECRET_MARKER_A),
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(adapter.puts, [])
        self.assertEqual(repo.patches, [])

    def test_marcador_sin_secreto_se_rechaza(self):
        adapter = FakeBusinessAdapter([business_doc(status="Open")])
        repo = FakeRepo([exec_doc(status="queued")])
        service = make_service(adapter, repo)
        for bad in ({"sources_checked": ["env"]}, {"secret": "  "}, "X_API_KEY"):
            with self.subTest(bad=bad):
                result = run(
                    service.transition_ticket(
                        "TASK-2026-00010", "Working", "x", secret_block=bad
                    )
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(adapter.puts, [])
        self.assertEqual(repo.patches, [])

    def test_marcador_se_escribe_en_working(self):
        adapter = FakeBusinessAdapter([business_doc(status="Open")])
        repo = FakeRepo([exec_doc(status="queued")])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Working", "falta la clave",
                secret_block=dict(SECRET_MARKER_A),
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "applied")
        self.assertEqual(result["business_status"], "Working")
        stored = repo.tasks["exec-1"]
        self.assertEqual(stored["params"]["secret_block"], SECRET_MARKER_A)
        self.assertEqual(mirror_of(stored)["business_status"], "Working")
        self.assertEqual(mirror_of(stored)["status_pushed"], "in_progress")

    def test_transicion_normal_limpia_el_marcador(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc_with_marker(SECRET_MARKER_A)])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket("TASK-2026-00010", "Open", "retomado")
        )
        self.assertEqual(result["outcome"], "applied")
        stored = repo.tasks["exec-1"]
        self.assertNotIn("secret_block", stored["params"])

    def test_noop_conserva_el_marcador(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc_with_marker(SECRET_MARKER_A)])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket("TASK-2026-00010", "Working", "sigue")
        )
        self.assertEqual(result["outcome"], "already_applied")
        self.assertFalse(result["mirror_healed"])
        self.assertEqual(repo.patches, [])
        self.assertEqual(
            repo.tasks["exec-1"]["params"]["secret_block"], SECRET_MARKER_A
        )

    def test_reporte_actualiza_el_marcador(self):
        adapter = FakeBusinessAdapter([business_doc(status="Working")])
        repo = FakeRepo([exec_doc_with_marker(SECRET_MARKER_A)])
        service = make_service(adapter, repo)
        result = run(
            service.transition_ticket(
                "TASK-2026-00010", "Working", "otra clave",
                secret_block=dict(SECRET_MARKER_B),
            )
        )
        self.assertEqual(result["outcome"], "already_applied")
        self.assertTrue(result["mirror_healed"])
        self.assertEqual(
            repo.tasks["exec-1"]["params"]["secret_block"], SECRET_MARKER_B
        )


class McpSecretReportTests(unittest.TestCase):
    def _register(self, task_service):
        server = FakeMCPServer()
        names = mcp_server._register_openceo_tools(
            server, SimpleNamespace(), task_service, None
        )
        return server, names

    def test_secret_enruta_a_report_missing_secret(self):
        svc = FakeTaskService()
        reports = []

        async def report(ticket, secret, sources_checked, *, note=None):
            reports.append(
                (ticket, secret, list(sources_checked or []), note)
            )
            return {"ok": True, "outcome": "applied", "ticket": ticket}

        svc.report_missing_secret = report
        server, _ = self._register(svc)
        result = run(
            server.tools["openceo_transition_ticket"][0](
                ticket="TASK-1",
                to_status="Working",
                reason="falta clave",
                secret="X_API_KEY",
                sources_checked=["env", ".env"],
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            reports, [("TASK-1", "X_API_KEY", ["env", ".env"], "falta clave")]
        )
        self.assertEqual(svc.calls, [])

    def test_secret_con_otro_estado_es_invalid_input(self):
        svc = FakeTaskService()
        server, _ = self._register(svc)
        result = run(
            server.tools["openceo_transition_ticket"][0](
                ticket="TASK-1",
                to_status="Completed",
                reason="x",
                evidence="e",
                secret="X_API_KEY",
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(svc.calls, [])


if __name__ == "__main__":
    unittest.main()
