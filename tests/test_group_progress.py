"""% de avance de los Task grupo + maquinaria de visibilidad (TASK-2026-00146).

Contrato que se fija aquí (verificado contra el sitio vivo el 2026-10-08):

- Frappe NO calcula el % del grupo: Completed=100, Cancelled fuera del
  denominador, hijo grupo aporta su propio %, resto 0. Sin hijos
  activos → 0. El progress guardado de una hoja NO se promedia (Frappe
  lo deja en 100 al reabrir).
- El rollup (progreso + span) se recalcula por evento con jobs
  `kind=visibility`, un PUT por padre, propagación hacia arriba.
- Todo compare-before-write: sin cambios no hay PUT (el eco no escribe).
- El fin del hijo no pasa el fin del padre (417 `InvalidDates`): quien
  escribe extiende ancestros y reintenta una vez.
- Jardal ERP caído → `pending_visibility` en el espejo (reconciliación o
  siguiente evento).

Jardal ERP simulado (fakes al estilo `test_ticket_transition.py`); 0 tokens.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import visibility  # noqa: E402
from state import TaskSource  # noqa: E402
from task_service import TaskService  # noqa: E402
from tools.jardal_erp_client import (  # noqa: E402
    JardalERPDateBoundError,
    JardalERPUnavailable,
    JardalERPValidationError,
    classify_417,
)
from tools.jardal_erp_tickets import JardalERPTicketAdapter  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --- payload real observado contra el sitio vivo (2026-10-08) ---------------

DATE_BOUND_PAYLOAD = {
    "exception": (
        "frappe.exceptions.InvalidDates: Expected End Date should be less "
        "than or equal to parent task's Expected End Date 03-10-2026."
    ),
    "exc_type": "InvalidDates",
    "_server_messages": [
        '{"message":"Expected End Date should be less than or equal to '
        "parent task's Expected End Date 03-10-2026.\",\"as_table\":false}"
    ],
}


# --- fakes ------------------------------------------------------------------

class FakeClient:
    """Cliente REST falso para el adapter real: registra y sirve docs."""

    def __init__(self, docs=None):
        self.docs = {d["name"]: dict(d) for d in (docs or [])}
        self.updated: list[tuple] = []
        self.listed: list[tuple] = []

    async def get_doc(self, doctype, name):
        row = self.docs.get(name)
        if row is None:
            raise JardalERPValidationError(f"No existe {doctype} {name}", status_code=404)
        return dict(row)

    async def list_docs(self, doctype, *, filters=None, fields=None, limit=20):
        self.listed.append((doctype, filters, fields, limit))
        rows = [d for d in self.docs.values() if self._match(d, filters or [])]
        if fields:
            rows = [{k: r.get(k) for k in fields} for r in rows]
        return [dict(r) for r in rows[:limit]]

    @staticmethod
    def _match(doc, filters):
        for field, op, want in filters:
            if op == "=" and doc.get(field) != want:
                return False
        return True

    async def update_doc(self, doctype, name, body):
        self.updated.append((doctype, name, dict(body)))
        row = self.docs.get(name)
        if row is None:
            raise JardalERPValidationError(f"No existe {doctype} {name}", status_code=404)
        row.update(body)
        return dict(row)


def make_adapter(client):
    settings = SimpleNamespace(JARDAL_ERP_TICKETS_PROJECT_COMPANY="Jardal")
    return JardalERPTicketAdapter(settings, client=client)  # type: ignore[arg-type]


class FakeBusinessAdapter:
    """Jardal ERP en memoria para el servicio: docs + fallos programados."""

    is_enabled = True

    def __init__(self, docs=None):
        self.docs = {d["name"]: dict(d) for d in (docs or [])}
        self.work_puts: list[tuple] = []
        self.rollup_puts: list[tuple] = []
        self.status_puts: list[tuple] = []
        self.searches: list[tuple] = []
        self.fail_plan: dict[str, list[Exception]] = {}

    def fail_next(self, op, exc):
        self.fail_plan.setdefault(op, []).append(exc)

    def _maybe_fail(self, op):
        planned = self.fail_plan.get(op) or []
        if planned:
            raise planned.pop(0)

    async def resolve_task(self, identifier):
        self._maybe_fail("resolve_task")
        key = str(identifier or "").strip()
        if key in self.docs:
            return dict(self.docs[key])
        for doc in self.docs.values():
            if str(doc.get("openceo_ref") or "") == key:
                return dict(doc)
        raise JardalERPValidationError(f"No existe el ticket {key}.", status_code=404)

    async def list_children(self, parent_name, *, limit=200):
        self._maybe_fail("list_children")
        name = str(parent_name or "").strip()
        return [
            dict(d) for d in self.docs.values() if d.get("parent_task") == name
        ][:limit]

    async def list_closed_with_dates(self, project_name, *, limit=200):
        self._maybe_fail("list_closed_with_dates")
        return [
            dict(d) for d in self.docs.values()
            if d.get("project") == project_name and d.get("status") == "Completed"
        ][:limit]

    async def update_work_dates(self, identifier, dates):
        # Los puts registran INTENTOS (también los fallidos): los tests de
        # reintento cuentan llamadas, no éxitos.
        self.work_puts.append((str(identifier), dict(dates)))
        self._maybe_fail("update_work_dates")
        doc = await self.resolve_task(identifier)
        doc.update(dates)
        self.docs[doc["name"]] = doc
        return {"ok": True, "changed": sorted(dates), "ticket": {"name": doc["name"]}}

    async def update_group_rollup(self, identifier, rollup):
        self.rollup_puts.append((str(identifier), dict(rollup)))
        self._maybe_fail("update_group_rollup")
        doc = await self.resolve_task(identifier)
        doc.update(rollup)
        self.docs[doc["name"]] = doc
        return {"ok": True, "changed": sorted(rollup), "ticket": {"name": doc["name"]}}

    async def update_status(self, name, status, *, task=None, work_dates=None):
        self.status_puts.append((name, status, dict(work_dates or {})))
        self._maybe_fail("update_status")
        doc = await self.resolve_task(name)
        doc.update({"status": f"BUS:{status}", **dict(work_dates or {})})
        self.docs[name] = doc
        return {"name": name}

    async def transition_status(self, identifier, to_status, *, work_dates=None):
        doc = await self.resolve_task(identifier)
        self.status_puts.append((doc["name"], to_status, dict(work_dates or {})))
        self._maybe_fail("transition_status")
        doc.update({"status": to_status, **dict(work_dates or {})})
        self.docs[doc["name"]] = doc
        return {"ok": True, "ticket": {"name": doc["name"], "status": to_status}}

    async def search_tickets(self, *, query=None, project_key=None, status=None, limit=20):
        self._maybe_fail("search_tickets")
        self.searches.append((query, project_key, status, limit))
        name = "PROJ-0001" if project_key == "jardal-openceo" else project_key
        rows = [d for d in self.docs.values() if d.get("project") == name]
        if status:
            wanted = {status} if isinstance(status, str) else set(status)
            rows = [d for d in rows if d.get("status") in wanted]
        return [dict(d) for d in rows[:limit]]


class FakeRepo:
    def __init__(self, tasks=None):
        self.tasks = {t["task_id"]: dict(t) for t in (tasks or [])}
        self.patches: list[tuple] = []
        self.pending_visibility: list[dict] = []

    async def get_task(self, task_id):
        doc = self.tasks.get(str(task_id))
        return dict(doc) if doc else None

    async def patch_task(self, task_id, updates):
        self.patches.append((str(task_id), dict(updates)))
        doc = self.tasks.get(str(task_id))
        if doc is None:
            return None
        # Fusión de params como el servicio (None borra la clave).
        merged = dict(doc)
        for key, value in dict(updates).items():
            if key == "params" and isinstance(value, dict):
                params = dict(doc.get("params") or {})
                params.update(value)
                merged["params"] = params
            else:
                merged[key] = value
        self.tasks[str(task_id)] = merged
        return dict(merged)

    async def find_task_by_source(self, source, source_id):
        for doc in self.tasks.values():
            if str(doc.get("source") or "") == str(source) and str(
                doc.get("source_id") or ""
            ) == str(source_id):
                return dict(doc)
        return None

    async def list_tasks_pending_visibility(self, limit=200):
        return [dict(t) for t in self.pending_visibility[:limit]]

    async def list_tasks_needing_jardal_erp_sync(self, limit=200):
        return []


class FakeHub:
    def __init__(self):
        self.events: list[tuple] = []

    async def broadcast(self, event, payload):
        self.events.append((event, payload))


def make_service(adapter=None, repo=None, *, queue=True):
    service = TaskService.__new__(TaskService)
    service._settings = SimpleNamespace(
        TASK_AUTORUN=False,
        HORIZON_PERSONAL_MIRROR_ENABLED=False,
        JARDAL_ERP_SITE_TIMEZONE="America/Caracas",
    )
    service._business = adapter
    service._repo = repo
    service._hub = FakeHub()
    service._horizon_terminal = None
    service._config_loader = None
    if queue:
        service._visibility_queue = asyncio.Queue()
    service._visibility_worker = None
    return service


def business_doc(name, status="Open", **over):
    doc = {
        "name": name,
        "subject": f"Asunto {name}",
        "status": status,
        "project": "PROJ-0001",
        "openceo_ref": f"api:{name}",
        "openceo_task_id": f"exec-{name}",
        "openceo_coding_tool": "tier_c",
        "is_group": False,
        "parent_task": None,
        "progress": 0.0,
        "exp_start_date": None,
        "exp_end_date": None,
        "completed_on": None,
        "depends_on": [],
    }
    doc.update(over)
    return doc


def exec_doc(task_id, business_name, status="queued", **over):
    doc = {
        "task_id": task_id,
        "status": status,
        "project": "jardal-openceo",
        "source": TaskSource.API.value,
        "source_id": business_name,
        "coding_tool": "tier_c",
        "started_at": None,
        "completed_at": None,
        "params": {"_mirrors": {"erpnext": {"name": business_name}}},
    }
    doc.update(over)
    return doc


def drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


# --- regla pura ---------------------------------------------------------------

class GroupProgressRuleTests(unittest.TestCase):
    def test_todo_completed_da_cien(self):
        kids = [{"status": "Completed"}, {"status": "Completed"}]
        self.assertEqual(visibility.group_progress(kids), 100.0)

    def test_mixto_promedia(self):
        kids = [{"status": "Completed"}, {"status": "Open"}]
        self.assertEqual(visibility.group_progress(kids), 50.0)

    def test_cancelled_sale_del_denominador(self):
        kids = [{"status": "Completed"}, {"status": "Cancelled"}]
        self.assertEqual(visibility.group_progress(kids), 100.0)

    def test_hijo_grupo_aporta_su_propio_porcentaje(self):
        kids = [
            {"status": "Completed"},
            {"status": "Open", "is_group": True, "progress": 50.0},
        ]
        self.assertEqual(visibility.group_progress(kids), 75.0)

    def test_no_promedia_el_progress_guardado_de_hojas(self):
        # Frappe deja 100 al reabrir: manda el status, no el progress.
        kids = [
            {"status": "Open", "progress": 100.0},
            {"status": "Completed", "progress": 100.0},
        ]
        self.assertEqual(visibility.group_progress(kids), 50.0)

    def test_sin_hijos_activos_da_cero(self):
        self.assertEqual(visibility.group_progress([]), 0.0)
        self.assertEqual(
            visibility.group_progress([{"status": "Cancelled"}]), 0.0
        )

    def test_progreso_basura_en_grupo_aporta_cero_pero_cuenta(self):
        kids = [
            {"status": "Completed"},
            {"status": "Open", "is_group": True, "progress": "abc"},
        ]
        self.assertEqual(visibility.group_progress(kids), 50.0)

    def test_redondea_a_dos_decimales(self):
        kids = [{"status": "Completed"}, {"status": "Open"}, {"status": "Open"}]
        self.assertEqual(visibility.group_progress(kids), 33.33)


class GroupSpanTests(unittest.TestCase):
    def test_abarca_minimo_y_maximo(self):
        kids = [
            {"status": "Open", "exp_start_date": "2026-10-02 09:00:00",
             "exp_end_date": "2026-10-03 18:00:00"},
            {"status": "Open", "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-05 18:00:00"},
        ]
        self.assertEqual(
            visibility.group_span(kids),
            ("2026-10-01 09:00:00", "2026-10-05 18:00:00"),
        )

    def test_canceladas_fuera_del_span(self):
        kids = [
            {"status": "Cancelled", "exp_start_date": "2026-09-01 09:00:00",
             "exp_end_date": "2026-12-31 18:00:00"},
            {"status": "Open", "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-02 18:00:00"},
        ]
        self.assertEqual(
            visibility.group_span(kids),
            ("2026-10-01 09:00:00", "2026-10-02 18:00:00"),
        )

    def test_explicita_del_grupo_es_suelo_del_fin(self):
        kids = [
            {"status": "Open", "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-02 18:00:00"},
        ]
        self.assertEqual(
            visibility.group_span(kids, "2026-10-10 18:00:00"),
            ("2026-10-01 09:00:00", "2026-10-10 18:00:00"),
        )

    def test_sin_hijos_fechados_da_nones(self):
        self.assertEqual(visibility.group_span([]), (None, None))
        self.assertEqual(
            visibility.group_span([{"status": "Open"}]), (None, None)
        )

    def test_sin_hijos_pero_con_explicita(self):
        self.assertEqual(
            visibility.group_span([], "2026-10-10 18:00:00"),
            (None, "2026-10-10 18:00:00"),
        )

    def test_fechas_ilegibles_se_ignoran(self):
        kids = [
            {"status": "Open", "exp_start_date": "no-fecha", "exp_end_date": None},
            {"status": "Open", "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-02 18:00:00"},
        ]
        self.assertEqual(
            visibility.group_span(kids),
            ("2026-10-01 09:00:00", "2026-10-02 18:00:00"),
        )

    def test_span_invertido_degenera_a_punto(self):
        # Hijo Working con inicio tardío y sin fin + hijo cerrado con fin
        # temprano y sin inicio: Frappe rechaza fin < inicio.
        kids = [
            {"status": "Working", "exp_start_date": "2026-10-08 09:00:00",
             "exp_end_date": None},
            {"status": "Completed", "exp_start_date": None,
             "exp_end_date": "2026-10-02 18:00:00"},
        ]
        self.assertEqual(
            visibility.group_span(kids),
            ("2026-10-08 09:00:00", "2026-10-08 09:00:00"),
        )


class DateBoundClassifyTests(unittest.TestCase):
    def test_payload_vivo_es_date_bound(self):
        self.assertIs(classify_417(DATE_BOUND_PAYLOAD), JardalERPDateBoundError)

    def test_invalid_dates_sin_padre_sigue_duro(self):
        payload = {"exception": "frappe.exceptions.InvalidDates: algo raro",
                   "exc_type": "InvalidDates"}
        self.assertIs(classify_417(payload), JardalERPValidationError)

    def test_date_bound_es_subclase_de_validacion(self):
        self.assertTrue(issubclass(JardalERPDateBoundError, JardalERPValidationError))


# --- operaciones cerradas del adapter ------------------------------------------

class VisibilityAdapterTests(unittest.TestCase):
    def test_list_children_filtra_por_padre(self):
        client = FakeClient([
            business_doc("G", "Open", is_group=True),
            business_doc("A", "Completed", parent_task="G"),
            business_doc("B", "Open", parent_task="G"),
            business_doc("Z", "Open"),
        ])
        rows = run(make_adapter(client).list_children("G"))
        self.assertEqual({r["name"] for r in rows}, {"A", "B"})
        _doctype, filters, fields, limit = client.listed[0]
        self.assertEqual(filters, [["parent_task", "=", "G"]])
        self.assertIn("progress", fields)
        self.assertIn("exp_start_date", fields)
        self.assertIn("exp_end_date", fields)

    def test_list_children_sin_padre_no_llama(self):
        client = FakeClient()
        self.assertEqual(run(make_adapter(client).list_children("  ")), [])
        self.assertEqual(client.listed, [])

    def test_update_work_dates_escribe_exactamente_lo_presente(self):
        client = FakeClient([business_doc("TASK-1", "Working")])
        result = run(make_adapter(client).update_work_dates(
            "TASK-1", {"exp_start_date": "2026-10-08 09:00:00", "completed_on": None}))
        self.assertTrue(result["ok"])
        _doctype, name, body = client.updated[0]
        self.assertEqual(name, "TASK-1")
        self.assertEqual(body, {"exp_start_date": "2026-10-08 09:00:00",
                               "completed_on": None})

    def test_update_work_dates_rechaza_vacio_y_claves_abiertas(self):
        client = FakeClient([business_doc("TASK-1", "Working")])
        adapter = make_adapter(client)
        with self.assertRaises(JardalERPValidationError):
            run(adapter.update_work_dates("TASK-1", {}))
        with self.assertRaises(JardalERPValidationError):
            run(adapter.update_work_dates("TASK-1", {"subject": "hack"}))
        self.assertEqual(client.updated, [])

    def test_update_work_dates_descongela_tool_v1_en_el_mismo_put(self):
        client = FakeClient([
            business_doc("TASK-1", "Working", openceo_coding_tool="codex")])
        run(make_adapter(client).update_work_dates(
            "TASK-1", {"exp_start_date": "2026-10-08 09:00:00"}))
        _doctype, _name, body = client.updated[0]
        self.assertIsNone(body["openceo_coding_tool"])
        self.assertEqual(body["exp_start_date"], "2026-10-08 09:00:00")

    def test_update_group_rollup_descongela_tool_v1_en_el_mismo_put(self):
        client = FakeClient([
            business_doc("TASK-9", "Open", is_group=True,
                         openceo_coding_tool="claude")])
        run(make_adapter(client).update_group_rollup("TASK-9", {"progress": 10.0}))
        _doctype, _name, body = client.updated[0]
        self.assertIsNone(body["openceo_coding_tool"])
        self.assertEqual(body["progress"], 10.0)

    def test_update_group_rollup_un_solo_put(self):
        client = FakeClient([business_doc("TASK-9", "Open", is_group=True)])
        result = run(make_adapter(client).update_group_rollup(
            "TASK-9", {"progress": 50.0, "exp_start_date": "2026-10-01 09:00:00",
                       "exp_end_date": "2026-10-05 18:00:00"}))
        self.assertTrue(result["ok"])
        self.assertEqual(len(client.updated), 1)
        _doctype, name, body = client.updated[0]
        self.assertEqual(body["progress"], 50.0)

    def test_update_group_rollup_rechaza_claves_abiertas(self):
        client = FakeClient([business_doc("TASK-9", "Open", is_group=True)])
        with self.assertRaises(JardalERPValidationError):
            run(make_adapter(client).update_group_rollup(
                "TASK-9", {"status": "Completed"}))
        self.assertEqual(client.updated, [])

    def test_list_closed_with_dates_filtra_proyecto_y_estado(self):
        client = FakeClient([
            business_doc("A", "Completed"),
            business_doc("B", "Open"),
        ])
        rows = run(make_adapter(client).list_closed_with_dates("PROJ-0001"))
        self.assertEqual([r["name"] for r in rows], ["A"])
        _doctype, filters, _fields, _limit = client.listed[0]
        self.assertIn(["project", "=", "PROJ-0001"], filters)
        self.assertIn(["status", "=", "Completed"], filters)

    def test_update_status_fusiona_fechas_en_el_mismo_put(self):
        client = FakeClient([business_doc("TASK-1", "Open")])
        adapter = make_adapter(client)
        run(adapter.update_status(
            "TASK-1", "in_progress",
            work_dates={"exp_start_date": "2026-10-08 09:00:00", "progress": 0}))
        self.assertEqual(client.updated[0], (
            "Task", "TASK-1",
            {"status": "Working", "exp_start_date": "2026-10-08 09:00:00",
             "progress": 0},
        ))

    def test_update_status_rechaza_claves_de_fecha_abiertas(self):
        client = FakeClient()
        with self.assertRaises(JardalERPValidationError):
            run(make_adapter(client).update_status(
                "TASK-1", "in_progress", work_dates={"subject": "hack"}))
        self.assertEqual(client.updated, [])

    def test_transition_status_fusiona_fechas_en_el_mismo_put(self):
        client = FakeClient([business_doc("TASK-1", "Working")])
        run(make_adapter(client).transition_status(
            "TASK-1", "Completed",
            work_dates={"exp_end_date": "2026-10-08 18:00:00"}))
        _doctype, _name, body = client.updated[0]
        self.assertEqual(body["status"], "Completed")
        self.assertEqual(body["exp_end_date"], "2026-10-08 18:00:00")


# --- worker de visibilidad ----------------------------------------------------

def _rollup_fixture():
    group = business_doc("G", "Open", is_group=True, openceo_task_id="exec-g",
                         openceo_ref="api:G")
    kid_a = business_doc(
        "A", "Completed", parent_task="G", progress=100.0,
        exp_start_date="2026-10-01 09:00:00", exp_end_date="2026-10-02 18:00:00",
        completed_on="2026-10-02", openceo_task_id="exec-a", openceo_ref="api:A")
    kid_b = business_doc(
        "B", "Open", parent_task="G",
        exp_start_date="2026-10-03 09:00:00", exp_end_date="2026-10-04 18:00:00",
        openceo_task_id="exec-b", openceo_ref="api:B")
    adapter = FakeBusinessAdapter([group, kid_a, kid_b])
    repo = FakeRepo([
        exec_doc("exec-g", "G", "converted"),
        exec_doc("exec-a", "A", "completed"),
        exec_doc("exec-b", "B", "queued"),
    ])
    return adapter, repo


class RefreshRollupTests(unittest.TestCase):
    def test_cierre_recalcula_padre_en_un_put(self):
        adapter, repo = _rollup_fixture()
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-a", "name": "A",
             "reason": "transition:Completed"}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["rolled"], ["G"])
        self.assertEqual(len(adapter.rollup_puts), 1)
        name, rollup = adapter.rollup_puts[0]
        self.assertEqual(name, "G")
        self.assertEqual(rollup["progress"], 50.0)
        self.assertEqual(rollup["exp_start_date"], "2026-10-01 09:00:00")
        self.assertEqual(rollup["exp_end_date"], "2026-10-04 18:00:00")

    def test_cancelada_fuera_del_denominador(self):
        adapter, repo = _rollup_fixture()
        adapter.docs["B"]["status"] = "Cancelled"
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-a", "name": "A",
             "reason": "transition:Completed"}))
        self.assertTrue(result["ok"])
        _name, rollup = adapter.rollup_puts[0]
        self.assertEqual(rollup["progress"], 100.0)

    def test_sin_cambios_no_hay_put_eco(self):
        adapter, repo = _rollup_fixture()
        adapter.docs["G"].update({
            "progress": 50.0, "exp_start_date": "2026-10-01 09:00:00",
            "exp_end_date": "2026-10-04 18:00:00"})
        # B ya estimado y registrado: nada propio que escribir.
        repo.tasks["exec-b"]["params"]["_mirrors"]["erpnext"].update({
            "exp_start_estimated": "2026-10-03 09:00:00",
            "exp_end_estimated": "2026-10-04 18:00:00"})
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-b", "name": "B",
             "reason": "push:in_progress"}))
        self.assertTrue(result["ok"])
        self.assertEqual(adapter.rollup_puts, [])
        self.assertEqual(adapter.work_puts, [])

    def test_propagacion_anidada_de_abajo_arriba(self):
        root = business_doc("ROOT", "Open", is_group=True, openceo_task_id="exec-r",
                            openceo_ref="api:ROOT")
        mid = business_doc("MID", "Open", is_group=True, parent_task="ROOT",
                           openceo_task_id="exec-m", openceo_ref="api:MID")
        leaf = business_doc(
            "L", "Completed", parent_task="MID", progress=100.0,
            exp_start_date="2026-10-01 09:00:00",
            exp_end_date="2026-10-02 18:00:00", openceo_task_id="exec-l",
            openceo_ref="api:L")
        adapter = FakeBusinessAdapter([root, mid, leaf])
        repo = FakeRepo([
            exec_doc("exec-r", "ROOT", "converted"),
            exec_doc("exec-m", "MID", "converted"),
            exec_doc("exec-l", "L", "completed"),
        ])
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-l", "name": "L",
             "reason": "transition:Completed"}))
        self.assertTrue(result["ok"])
        self.assertEqual([name for name, _roll in adapter.rollup_puts],
                         ["MID", "ROOT"])
        _name, root_roll = adapter.rollup_puts[1]
        self.assertEqual(root_roll["progress"], 100.0)

    def test_hijo_borrado_refresca_padre_del_espejo(self):
        adapter, repo = _rollup_fixture()
        del adapter.docs["B"]
        repo.tasks["exec-b"]["params"]["_mirrors"]["erpnext"]["parent_task"] = "G"
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-b", "name": "B",
             "reason": "webhook:applied"}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["orphan_parent"], "G")
        _name, rollup = adapter.rollup_puts[0]
        self.assertEqual(rollup["progress"], 100.0)

    def test_hijo_borrado_sin_padre_no_hace_nada(self):
        adapter = FakeBusinessAdapter([])
        repo = FakeRepo([exec_doc("exec-x", "X", "cancelled")])
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-x", "name": "X",
             "reason": "webhook:applied"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "no_parent")

    def test_business_caido_deja_pending_visibility(self):
        adapter, repo = _rollup_fixture()
        adapter.fail_next("resolve_task", JardalERPUnavailable("caído"))
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-a", "name": "A",
             "reason": "transition:Completed"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "unavailable")
        mirror = repo.tasks["exec-a"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["pending_visibility"], "A")

    def test_exito_limpia_pending_visibility(self):
        adapter, repo = _rollup_fixture()
        repo.tasks["exec-a"]["params"]["_mirrors"]["erpnext"]["pending_visibility"] = "A"
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-a", "name": "A",
             "reason": "reconcile"}))
        self.assertTrue(result["ok"])
        mirror = repo.tasks["exec-a"]["params"]["_mirrors"]["erpnext"]
        self.assertIsNone(mirror["pending_visibility"])

    def test_error_de_negocio_no_lanza(self):
        adapter, repo = _rollup_fixture()
        adapter.fail_next("resolve_task", JardalERPValidationError("roto"))
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-a", "name": "A",
             "reason": "transition:Completed"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "business_error")

    def test_grupo_vacio_progreso_cero_y_sin_fechas(self):
        group = business_doc("G", "Open", is_group=True, progress=80.0,
                             exp_start_date="2026-10-01 09:00:00",
                             exp_end_date="2026-10-02 18:00:00",
                             openceo_task_id="exec-g", openceo_ref="api:G")
        adapter = FakeBusinessAdapter([group])
        repo = FakeRepo([exec_doc("exec-g", "G", "converted")])
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-g", "name": "G",
             "reason": "webhook:applied"}))
        self.assertTrue(result["ok"])
        _name, rollup = adapter.rollup_puts[0]
        self.assertEqual(rollup["progress"], 0.0)
        self.assertIsNone(rollup["exp_start_date"])
        self.assertIsNone(rollup["exp_end_date"])


# --- productores de jobs --------------------------------------------------------

class VisibilityEnqueueTests(unittest.TestCase):
    def test_push_con_exito_encola_visibilidad(self):
        task = exec_doc("exec-1", "TASK-1", "in_progress")
        task["started_at"] = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        adapter = FakeBusinessAdapter([business_doc("TASK-1", "Working")])
        repo = FakeRepo([task])
        service = make_service(adapter, repo)
        run(service._push_status(
            {"task_id": "exec-1", "name": "TASK-1", "status": "in_progress"}))
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["name"], "TASK-1")
        self.assertEqual(jobs[0]["reason"], "push:in_progress")
        # Y las fechas viajaron en el mismo PUT del estado.
        _name, _status, dates = adapter.status_puts[0]
        self.assertEqual(dates["exp_start_date"], "2026-10-08 08:00:00")

    def test_push_fallido_no_encola(self):
        adapter = FakeBusinessAdapter([business_doc("TASK-1", "Open")])
        adapter.fail_next("update_status", JardalERPValidationError("roto"))
        repo = FakeRepo([exec_doc("exec-1", "TASK-1", "in_progress")])
        service = make_service(adapter, repo)
        run(service._push_status(
            {"task_id": "exec-1", "name": "TASK-1", "status": "in_progress"}))
        self.assertEqual(drain(service._visibility_queue), [])

    def test_transition_aplicada_encola_visibilidad(self):
        business = business_doc("TASK-1", "Working", openceo_task_id="exec-1",
                                openceo_ref="api:TASK-1")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([exec_doc("exec-1", "TASK-1", "in_progress")])
        service = make_service(adapter, repo)
        result = run(service.transition_ticket(
            "TASK-1", "Completed", "fin", evidence="suite verde"))
        self.assertEqual(result["outcome"], "applied")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reason"], "transition:Completed")
        # Mismo PUT: estado + fin real + done.
        _name, _status, dates = adapter.status_puts[0]
        self.assertIn("exp_end_date", dates)
        self.assertIn("completed_on", dates)

    def test_reconciliacion_reencola_visibilidad_pendiente(self):
        flagged = exec_doc("exec-1", "TASK-1", "completed")
        flagged["params"]["_mirrors"]["erpnext"]["pending_visibility"] = "TASK-1"
        adapter = FakeBusinessAdapter([business_doc("TASK-1", "Completed")])
        repo = FakeRepo([flagged])
        repo.pending_visibility = [flagged]
        service = make_service(adapter, repo)
        repaired = run(service.startup_reconcile_jardal_erp())
        self.assertEqual(repaired, 1)
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reason"], "reconcile")

    def test_visibility_loop_procesa_jobs(self):
        async def scenario():
            adapter, repo = _rollup_fixture()
            service = make_service(adapter, repo)
            service.start_visibility_worker()
            service._visibility_queue.put_nowait(
                {"task_id": "exec-a", "name": "A",
                 "reason": "transition:Completed"})
            for _ in range(100):
                if adapter.rollup_puts:
                    break
                await asyncio.sleep(0.01)
            await service.stop_visibility_worker()
            return adapter

        adapter = asyncio.run(scenario())
        self.assertEqual(len(adapter.rollup_puts), 1)
        self.assertEqual(adapter.rollup_puts[0][0], "G")


# --- webhook inverso --------------------------------------------------------------

class WebhookVisibilityTests(unittest.TestCase):
    def _service(self, business_status="Open", exec_status="queued", **over):
        business = business_doc("TASK-1", business_status,
                                openceo_task_id="exec-1", openceo_ref="api:TASK-1")
        task = exec_doc("exec-1", "TASK-1", exec_status)
        task.update(over)
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([task])
        return make_service(adapter, repo), adapter, repo

    def test_working_manual_encola(self):
        service, _adapter, _repo = self._service("Working", "queued")
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Working", parent_task=None))
        self.assertEqual(result["status"], "ignored")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reason"], "webhook:manual")

    def test_eco_de_nuestro_working_no_encola(self):
        service, _adapter, _repo = self._service("Working", "in_progress")
        service._repo.tasks["exec-1"]["started_at"] = datetime(
            2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Working", parent_task=None))
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(drain(service._visibility_queue), [])

    def test_reapertura_manual_encola(self):
        service, _adapter, _repo = self._service("Open", "completed")
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Open", parent_task=None))
        self.assertEqual(result["status"], "ignored")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reason"], "webhook:manual")

    def test_open_con_ejecucion_abierta_no_encola(self):
        service, _adapter, _repo = self._service("Open", "queued")
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Open", parent_task=None))
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(drain(service._visibility_queue), [])

    def test_relink_sincroniza_espejo_y_encola(self):
        service, _adapter, repo = self._service("Working", "in_progress")
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Working", parent_task="G-NEW"))
        self.assertEqual(result["status"], "ignored")
        mirror = repo.tasks["exec-1"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["parent_task"], "G-NEW")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reason"], "webhook:relink")

    def test_already_applied_con_relink_encola(self):
        service, _adapter, repo = self._service("Completed", "completed")
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Completed", parent_task="G-NEW"))
        self.assertEqual(result["status"], "already_applied")
        mirror = repo.tasks["exec-1"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["parent_task"], "G-NEW")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reason"], "webhook:relink")

    def test_already_applied_sin_cambios_no_encola(self):
        service, _adapter, _repo = self._service("Completed", "completed")
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Completed", parent_task=None))
        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(drain(service._visibility_queue), [])

    def test_aplicado_encola(self):
        service, _adapter, _repo = self._service("Working", "in_progress")
        result = run(service.apply_business_status(
            task_id="exec-1", business_status="Completed", parent_task=None))
        self.assertEqual(result["status"], "applied")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["reason"], "webhook:applied")

    def test_sin_ejecucion_encola_job_sin_espejo(self):
        adapter = FakeBusinessAdapter([business_doc("TASK-9", "Working")])
        repo = FakeRepo([])
        service = make_service(adapter, repo)
        result = run(service.apply_business_status(
            business_status="Working", business_name="TASK-9"))
        self.assertEqual(result["status"], "not_found")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["task_id"])
        self.assertEqual(jobs[0]["name"], "TASK-9")


# --- límite de fechas padre/hijo ----------------------------------------------------

class DateBoundRetryTests(unittest.TestCase):
    def test_push_extiende_padre_y_reintenta_una_vez(self):
        group = business_doc("G", "Open", is_group=True,
                             exp_start_date="2026-10-01 09:00:00",
                             exp_end_date="2026-10-02 18:00:00",
                             openceo_task_id="exec-g", openceo_ref="api:G")
        kid = business_doc("A", "Working", parent_task="G",
                           openceo_task_id="exec-a", openceo_ref="api:A")
        adapter = FakeBusinessAdapter([group, kid])
        adapter.fail_next("update_status",
                          JardalERPDateBoundError("Expected End Date should be less "
                                                "than or equal to parent task's."))
        task = exec_doc("exec-a", "A", "completed")
        task["completed_at"] = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        repo = FakeRepo([task])
        service = make_service(adapter, repo)
        run(service._push_status(
            {"task_id": "exec-a", "name": "A", "status": "completed"}))
        # Primero se extendió el padre, luego se reintentó el hijo.
        self.assertEqual(len(adapter.rollup_puts), 1)
        self.assertEqual(adapter.rollup_puts[0][0], "G")
        self.assertEqual(adapter.rollup_puts[0][1]["exp_end_date"],
                         "2026-10-05 12:00:00")
        self.assertEqual(len(adapter.status_puts), 2)
        mirror = repo.tasks["exec-a"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["status_pushed"], "completed")
        self.assertIsNone(mirror.get("pending_status"))

    def test_segundo_date_bound_es_error_duro(self):
        adapter = FakeBusinessAdapter([business_doc("A", "Working", parent_task="G"),
                                       business_doc("G", "Open", is_group=True)])
        adapter.fail_next("update_status", JardalERPDateBoundError("límite 1"))
        adapter.fail_next("update_status", JardalERPDateBoundError("límite 2"))
        task = exec_doc("exec-a", "A", "completed")
        task["completed_at"] = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        repo = FakeRepo([task])
        service = make_service(adapter, repo)
        run(service._push_status(
            {"task_id": "exec-a", "name": "A", "status": "completed"}))
        self.assertEqual(len(adapter.status_puts), 2)
        mirror = repo.tasks["exec-a"]["params"]["_mirrors"]["erpnext"]
        self.assertNotIn("status_pushed", mirror)

    def test_ensure_span_extiende_toda_la_cadena_de_arriba_abajo(self):
        root = business_doc("ROOT", "Open", is_group=True,
                            exp_start_date="2026-10-01 09:00:00",
                            exp_end_date="2026-10-02 18:00:00")
        mid = business_doc("MID", "Open", is_group=True, parent_task="ROOT",
                           exp_start_date="2026-10-01 09:00:00",
                           exp_end_date="2026-10-02 18:00:00")
        leaf = business_doc("L", "Open", parent_task="MID")
        adapter = FakeBusinessAdapter([root, mid, leaf])
        service = make_service(adapter, FakeRepo([]))
        run(service._ensure_span_for_child(
            adapter, "L", start="2026-10-01 09:00:00", end="2026-10-09 18:00:00"))
        self.assertEqual([name for name, _need in adapter.rollup_puts],
                         ["ROOT", "MID"])
        for _name, need in adapter.rollup_puts:
            self.assertEqual(need, {"exp_end_date": "2026-10-09 18:00:00"})
            self.assertNotIn("exp_start_date", need)
