"""Fechas reales y estimadas del Gantt en Jardal ERP (TASK-2026-00147).

Contrato que se fija aquí (verificado contra el sitio vivo el 2026-10-08):

- `exp_*` aceptan `YYYY-MM-DD HH:MM:SS` en hora del sitio (UTC-4);
  `completed_on` es Date y no se pone solo.
- Al pasar a Working: inicio real en el mismo PUT. Al cerrar: fin real +
  done en el mismo PUT. Al reabrir: se limpia fin+done (el inicio, nunca)
  y se restaura la explícita o se re-estima.
- Las transiciones guardan ADEMÁS started_at/completed_at en la ejecución.
- Estimación determinista: duración por tier (mediana de cerrados o
  defecto), inicio = máx(hoy, fines de depends_on). Eventos: creación,
  reapertura, cierre de dependencia. Nunca pisa reales, manuales ni
  explícitas; una fecha límite externa se adopta.
- Grupos: span = [mín inicios, máx fines], con la explícita como suelo.

Jardal ERP simulado (fakes al estilo `test_group_progress.py`); 0 tokens.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date, datetime, timezone
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
)


def run(coro):
    return asyncio.run(coro)


ZONE = "America/Caracas"


# --- fakes (misma forma que test_group_progress.py) -------------------------------

class FakeBusinessAdapter:
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

    async def create_ticket(self, **kwargs):
        name = "TASK-9"
        doc = business_doc(name, "Open")
        self.docs[name] = doc
        return {"ok": True, "duplicate": False, "task": dict(doc),
                "ref": kwargs.get("ref")}

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

    async def get_task(self, task_id):
        doc = self.tasks.get(str(task_id))
        return dict(doc) if doc else None

    async def patch_task(self, task_id, updates):
        self.patches.append((str(task_id), dict(updates)))
        doc = self.tasks.get(str(task_id))
        if doc is None:
            return None
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

    async def get_next_ticket_number(self):
        return 999

    async def create_task(self, doc):
        row = dict(doc)
        self.tasks[row["task_id"]] = row
        return dict(row)


class FakeHub:
    def __init__(self):
        self.events: list[tuple] = []

    async def broadcast(self, event, payload):
        self.events.append((event, payload))


def make_service(adapter=None, repo=None):
    service = TaskService.__new__(TaskService)
    service._settings = SimpleNamespace(
        TASK_AUTORUN=False,
        HORIZON_PERSONAL_MIRROR_ENABLED=False,
        JARDAL_ERP_SITE_TIMEZONE=ZONE,
    )
    service._business = adapter
    service._repo = repo
    service._hub = FakeHub()
    service._horizon_terminal = None
    service._config_loader = None
    service._visibility_queue = asyncio.Queue()
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


NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)  # 08:00 en sitio


# --- zona horaria y parsing ---------------------------------------------------------

class SiteTimeTests(unittest.TestCase):
    def test_utc_a_sitio_resta_cuatro(self):
        self.assertEqual(
            visibility.to_site_datetime(NOW, ZONE), "2026-10-08 08:00:00"
        )
        self.assertEqual(visibility.to_site_date(NOW, ZONE), "2026-10-08")

    def test_naive_se_asume_utc(self):
        naive = datetime(2026, 10, 8, 12, 0)
        self.assertEqual(
            visibility.to_site_datetime(naive, ZONE), "2026-10-08 08:00:00"
        )

    def test_zona_invalida_lanza_fail_closed(self):
        with self.assertRaises(Exception):
            visibility.to_site_datetime(NOW, "Zona/No-Existe")

    def test_parse_acepta_con_y_sin_microsegundos(self):
        self.assertEqual(
            visibility.parse_site_datetime("2026-10-08 08:00:00"),
            datetime(2026, 10, 8, 8, 0),
        )
        self.assertEqual(
            visibility.parse_site_datetime("2026-10-08 08:00:00.123456"),
            datetime(2026, 10, 8, 8, 0, 0, 123456),
        )

    def test_parse_ilegible_da_none(self):
        self.assertIsNone(visibility.parse_site_datetime(None))
        self.assertIsNone(visibility.parse_site_datetime(""))
        self.assertIsNone(visibility.parse_site_datetime("no-fecha"))
        self.assertIsNone(visibility.parse_site_day("no-fecha"))
        self.assertEqual(
            visibility.parse_site_day("2026-10-08"), date(2026, 10, 8)
        )

    def test_sitio_a_utc_roundtrip(self):
        back = visibility.site_datetime_to_utc("2026-10-08 08:00:00", ZONE)
        self.assertIsNotNone(back)
        assert back is not None
        self.assertEqual(back, NOW)
        self.assertIsNone(visibility.site_datetime_to_utc("no-fecha", ZONE))

    def test_as_utc_datetime_acepta_todo(self):
        self.assertEqual(
            visibility.as_utc_datetime(datetime(2026, 10, 8, 12, 0)), NOW
        )
        self.assertEqual(visibility.as_utc_datetime(NOW), NOW)
        self.assertEqual(
            visibility.as_utc_datetime("2026-10-08T12:00:00+00:00"), NOW
        )
        self.assertIsNone(visibility.as_utc_datetime(None))
        self.assertIsNone(visibility.as_utc_datetime("basura"))


# --- medianas y ventanas --------------------------------------------------------------

class MedianDurationTests(unittest.TestCase):
    def test_mediana_por_tier(self):
        rows = [
            {"openceo_coding_tool": "tier_c",
             "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-02 18:00:00"},
            {"openceo_coding_tool": "tier_c",
             "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-04 18:00:00"},
            {"openceo_coding_tool": "tier_a",
             "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-06 18:00:00"},
        ]
        self.assertEqual(
            visibility.median_durations(rows), {"tier_c": 2, "tier_a": 5}
        )

    def test_mismo_dia_cuenta_uno(self):
        rows = [{"openceo_coding_tool": "tier_b",
                 "exp_start_date": "2026-10-01 09:00:00",
                 "exp_end_date": "2026-10-01 18:00:00"}]
        self.assertEqual(visibility.median_durations(rows), {"tier_b": 1})

    def test_filas_rotas_se_ignoran(self):
        rows = [
            {"openceo_coding_tool": "tier_c", "exp_start_date": None,
             "exp_end_date": "2026-10-02 18:00:00"},
            {"openceo_coding_tool": "tier_c",
             "exp_start_date": "2026-10-03 09:00:00",
             "exp_end_date": "2026-10-01 18:00:00"},
            {"openceo_coding_tool": None,
             "exp_start_date": "2026-10-01 09:00:00",
             "exp_end_date": "2026-10-03 18:00:00"},
        ]
        self.assertEqual(visibility.median_durations(rows), {None: 2})

    def test_duracion_tier_mediana_o_defecto(self):
        medians = {"tier_c": 4}
        self.assertEqual(visibility.tier_duration_days("tier_c", medians), 4)
        self.assertEqual(visibility.tier_duration_days("tier_a", medians), 3)
        self.assertEqual(visibility.tier_duration_days(None, medians), 1)
        self.assertEqual(
            visibility.tier_duration_days("tier_a", {None: 6}), 6
        )


class EstimateWindowTests(unittest.TestCase):
    def test_sin_dependencias_empieza_hoy(self):
        start, end = visibility.estimate_window(
            today=date(2026, 10, 8), tier="tier_c", dep_ends=[], medians={})
        self.assertEqual(start, "2026-10-08 09:00:00")
        self.assertEqual(end, "2026-10-09 18:00:00")

    def test_dependencias_retrasan_el_inicio(self):
        start, end = visibility.estimate_window(
            today=date(2026, 10, 8), tier="tier_c",
            dep_ends=["2026-10-10 18:00:00", None, "basura"], medians={})
        self.assertEqual(start, "2026-10-10 09:00:00")
        self.assertEqual(end, "2026-10-11 18:00:00")

    def test_dependencia_pasada_no_retrasa(self):
        start, _end = visibility.estimate_window(
            today=date(2026, 10, 8), tier="tier_c",
            dep_ends=["2026-10-01 18:00:00"], medians={})
        self.assertEqual(start, "2026-10-08 09:00:00")

    def test_mediana_manda_sobre_defecto(self):
        _start, end = visibility.estimate_window(
            today=date(2026, 10, 8), tier="tier_c", dep_ends=[],
            medians={"tier_c": 4})
        self.assertEqual(end, "2026-10-12 18:00:00")


# --- fechas puras de transición ----------------------------------------------------------------

class WorkDatesPureTests(unittest.TestCase):
    def test_push_working_fija_inicio_y_limpia_done(self):
        started = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
        dates = visibility.work_dates_for_push(
            openceo_status="in_progress", started_at=started,
            completed_at=None, zone=ZONE, now=NOW)
        self.assertEqual(dates, {"exp_start_date": "2026-10-07 10:00:00",
                                 "completed_on": None, "progress": 0})

    def test_push_completed_fija_fin_y_done(self):
        done = datetime(2026, 10, 8, 11, 30, tzinfo=timezone.utc)
        dates = visibility.work_dates_for_push(
            openceo_status="completed", started_at=None,
            completed_at=done, zone=ZONE, now=NOW)
        self.assertEqual(dates, {"exp_end_date": "2026-10-08 07:30:00",
                                 "completed_on": "2026-10-08", "progress": 100})

    def test_push_sin_fechas_usa_now(self):
        dates = visibility.work_dates_for_push(
            openceo_status="completed", started_at=None,
            completed_at=None, zone=ZONE, now=NOW)
        self.assertEqual(dates["exp_end_date"], "2026-10-08 08:00:00")

    def test_push_otros_estados_no_aportan(self):
        for status in ("queued", "waiting", "failed", "cancelled", "lo-que-sea"):
            self.assertEqual(visibility.work_dates_for_push(
                openceo_status=status, started_at=NOW, completed_at=NOW,
                zone=ZONE, now=NOW), {})

    def test_transition_working_fija_inicio_real(self):
        dates = visibility.work_dates_for_transition(
            target="Working", zone=ZONE, now=NOW)
        self.assertEqual(dates, {"exp_start_date": "2026-10-08 08:00:00",
                                 "completed_on": None, "progress": 0})

    def test_transition_completed_fija_fin_real(self):
        dates = visibility.work_dates_for_transition(
            target="Completed", zone=ZONE, now=NOW,
            explicit_due="2026-10-20 18:00:00")
        # La realidad pisa incluso la explícita (sobrevive en el espejo).
        self.assertEqual(dates["exp_end_date"], "2026-10-08 08:00:00")
        self.assertEqual(dates["completed_on"], "2026-10-08")

    def test_transition_open_restaura_explicita(self):
        dates = visibility.work_dates_for_transition(
            target="Open", zone=ZONE, now=NOW,
            explicit_due="2026-10-20 18:00:00")
        self.assertEqual(dates, {"completed_on": None, "progress": 0,
                                 "exp_end_date": "2026-10-20 18:00:00"})

    def test_transition_open_sin_explicita_limpia_fin(self):
        dates = visibility.work_dates_for_transition(
            target="Open", zone=ZONE, now=NOW)
        self.assertEqual(dates, {"completed_on": None, "progress": 0,
                                 "exp_end_date": None})

    def test_transition_otros_no_tocan_fechas(self):
        for target in ("Pending Review", "Cancelled"):
            self.assertEqual(visibility.work_dates_for_transition(
                target=target, zone=ZONE, now=NOW), {})


class AdoptExplicitTests(unittest.TestCase):
    def test_edicion_manual_se_adopta(self):
        self.assertEqual(
            visibility.adopt_explicit_due("2026-10-20 18:00:00",
                                          "2026-10-09 18:00:00", None),
            "2026-10-20 18:00:00")

    def test_igual_a_estimacion_conserva_registrada(self):
        self.assertEqual(
            visibility.adopt_explicit_due("2026-10-09 18:00:00",
                                          "2026-10-09 18:00:00", None),
            None)

    def test_vivo_vacio_conserva_registrada(self):
        self.assertEqual(
            visibility.adopt_explicit_due(None, "2026-10-09 18:00:00",
                                          "2026-10-20 18:00:00"),
            "2026-10-20 18:00:00")

    def test_sin_nada_da_none(self):
        self.assertIsNone(visibility.adopt_explicit_due(None, None, None))


# --- estimación en el servicio ---------------------------------------------------------

class EstimateServiceTests(unittest.TestCase):
    def _service(self, docs, tasks):
        adapter = FakeBusinessAdapter(docs)
        repo = FakeRepo(tasks)
        return make_service(adapter, repo), adapter, repo

    def test_create_estima_y_registra_marcadores(self):
        business = business_doc("B", "Open", openceo_task_id="exec-b",
                                openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        service, adapter, repo = self._service([business], [execution])
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "create"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates["exp_start_date"], "2026-10-08 09:00:00")
        self.assertEqual(dates["exp_end_date"], "2026-10-09 18:00:00")
        mirror = repo.tasks["exec-b"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["exp_start_estimated"], "2026-10-08 09:00:00")
        self.assertEqual(mirror["exp_end_estimated"], "2026-10-09 18:00:00")

    def test_explicita_respetada_solo_se_estima_inicio(self):
        business = business_doc("B", "Open", exp_end_date="2026-10-20 18:00:00",
                                openceo_task_id="exec-b", openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        execution["params"]["_mirrors"]["erpnext"]["due_date_explicit"] = (
            "2026-10-20 18:00:00")
        service, adapter, _repo = self._service([business], [execution])
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "create"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates, {"exp_start_date": "2026-10-08 09:00:00"})

    def test_edicion_manual_se_adopta_y_no_se_pisa(self):
        business = business_doc(
            "B", "Open", exp_start_date="2026-10-08 09:00:00",
            exp_end_date="2026-10-20 18:00:00", openceo_task_id="exec-b",
            openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        execution["params"]["_mirrors"]["erpnext"]["exp_end_estimated"] = (
            "2026-10-09 18:00:00")
        service, adapter, repo = self._service([business], [execution])
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "push:in_progress"))
        self.assertFalse(wrote)
        self.assertEqual(adapter.work_puts, [])
        mirror = repo.tasks["exec-b"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["due_date_explicit"], "2026-10-20 18:00:00")

    def test_reapertura_limpia_real_obsoleto_y_reestima(self):
        business = business_doc(
            "B", "Open", progress=100.0,
            exp_start_date="2026-10-01 09:00:00",
            exp_end_date="2026-10-05 18:00:00", completed_on="2026-10-05",
            openceo_task_id="exec-b", openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        execution["params"]["_mirrors"]["erpnext"]["exp_end_real"] = (
            "2026-10-05 18:00:00")
        service, adapter, repo = self._service([business], [execution])
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "transition:Open"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertIsNone(dates["completed_on"])
        self.assertEqual(dates["exp_end_date"], "2026-10-09 18:00:00")
        self.assertEqual(dates["progress"], 0)
        mirror = repo.tasks["exec-b"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["exp_end_estimated"], "2026-10-09 18:00:00")

    def test_reapertura_con_explicita_la_restaura(self):
        business = business_doc(
            "B", "Open", exp_start_date="2026-10-01 09:00:00",
            exp_end_date="2026-10-05 18:00:00", completed_on="2026-10-05",
            openceo_task_id="exec-b", openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        execution["params"]["_mirrors"]["erpnext"].update({
            "exp_end_real": "2026-10-05 18:00:00",
            "due_date_explicit": "2026-10-20 18:00:00"})
        service, adapter, repo = self._service([business], [execution])
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "transition:Open"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates["exp_end_date"], "2026-10-20 18:00:00")
        mirror = repo.tasks["exec-b"]["params"]["_mirrors"]["erpnext"]
        self.assertIsNone(mirror.get("exp_end_estimated"))

    def test_cadena_de_dependencias_retrasa_inicio(self):
        dep = business_doc(
            "D", "Completed", exp_start_date="2026-10-01 09:00:00",
            exp_end_date="2026-10-10 18:00:00")
        business = business_doc("B", "Open", depends_on=[{"task": "D"}],
                                openceo_task_id="exec-b", openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        service, adapter, _repo = self._service([dep, business], [execution])
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "create"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates["exp_start_date"], "2026-10-10 09:00:00")
        # D (9 días, tier_c) también alimenta la mediana: fin = 10 + 9.
        self.assertEqual(dates["exp_end_date"], "2026-10-19 18:00:00")

    def test_mediana_del_historial_manda(self):
        h1 = business_doc("H1", "Completed", exp_start_date="2026-10-01 09:00:00",
                          exp_end_date="2026-10-04 18:00:00")
        h2 = business_doc("H2", "Completed", exp_start_date="2026-10-01 09:00:00",
                          exp_end_date="2026-10-06 18:00:00")
        business = business_doc("B", "Open", openceo_task_id="exec-b",
                                openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        service, adapter, _repo = self._service([h1, h2, business], [execution])
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "create"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        # Mediana(3, 5) = 4 días desde el 8 → fin el 12.
        self.assertEqual(dates["exp_end_date"], "2026-10-12 18:00:00")

    def test_sin_ejecucion_solo_rellena_huecos(self):
        business = business_doc("B", "Open", exp_end_date="2026-10-20 18:00:00")
        business2 = business_doc("C", "Open",
                                 exp_start_date="2026-10-08 09:00:00",
                                 exp_end_date="2026-10-20 18:00:00")
        adapter = FakeBusinessAdapter([business, business2])
        service = make_service(adapter, FakeRepo([]))
        wrote = run(service._estimate_open_ticket(
            adapter, business, None, ZONE, NOW, "create"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates, {"exp_start_date": "2026-10-08 09:00:00"})

        wrote2 = run(service._estimate_open_ticket(
            adapter, business2, None, ZONE, NOW, "create"))
        self.assertFalse(wrote2)
        self.assertEqual(len(adapter.work_puts), 1)

    def test_recomputo_solo_en_eventos_que_lo_piden(self):
        def fixture():
            business = business_doc(
                "B", "Open", exp_start_date="2026-10-08 09:00:00",
                exp_end_date="2026-10-09 18:00:00", openceo_task_id="exec-b",
                openceo_ref="api:B")
            execution = exec_doc("exec-b", "B", "queued")
            execution["params"]["_mirrors"]["erpnext"].update({
                "exp_start_estimated": "2026-10-08 09:00:00",
                "exp_end_estimated": "2026-10-09 18:00:00"})
            return business, execution

        business, execution = fixture()
        service, adapter, _repo = self._service([business], [execution])
        self.assertFalse(run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "push:in_progress")))
        self.assertEqual(adapter.work_puts, [])

        business, execution = fixture()
        service, adapter, _repo = self._service([business], [execution])
        self.assertTrue(run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "dep_closed")))
        self.assertEqual(len(adapter.work_puts), 1)

    def test_estimacion_mas_alla_del_padre_lo_extiende_primero(self):
        group = business_doc("G", "Open", is_group=True,
                             exp_end_date="2026-10-02 18:00:00")
        business = business_doc("B", "Open", parent_task="G",
                                openceo_task_id="exec-b", openceo_ref="api:B")
        execution = exec_doc("exec-b", "B", "queued")
        adapter = FakeBusinessAdapter([group, business])
        adapter.fail_next("update_work_dates",
                          JardalERPDateBoundError("pasa al padre"))
        service = make_service(adapter, FakeRepo([execution]))
        wrote = run(service._estimate_open_ticket(
            adapter, business, execution, ZONE, NOW, "create"))
        self.assertTrue(wrote)
        self.assertEqual(adapter.rollup_puts[0][0], "G")
        self.assertEqual(adapter.rollup_puts[0][1]["exp_end_date"],
                         "2026-10-09 18:00:00")
        self.assertEqual(len(adapter.work_puts), 2)


class OwnDatesServiceTests(unittest.TestCase):
    def test_working_sin_inicio_sella_tarde(self):
        business = business_doc("A", "Working", openceo_task_id="exec-a",
                                openceo_ref="api:A")
        execution = exec_doc("exec-a", "A", "in_progress")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([execution])
        service = make_service(adapter, repo)
        wrote = run(service._ensure_own_dates(
            adapter, business, execution, ZONE, NOW, "webhook:manual"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates, {"exp_start_date": "2026-10-08 08:00:00"})
        self.assertEqual(repo.tasks["exec-a"]["started_at"], NOW)

    def test_working_con_inicio_manual_lo_adopta_en_ejecucion(self):
        business = business_doc("A", "Working",
                                exp_start_date="2026-10-07 10:00:00",
                                openceo_task_id="exec-a", openceo_ref="api:A")
        execution = exec_doc("exec-a", "A", "in_progress")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([execution])
        service = make_service(adapter, repo)
        wrote = run(service._ensure_own_dates(
            adapter, business, execution, ZONE, NOW, "webhook:manual"))
        self.assertFalse(wrote)
        self.assertEqual(adapter.work_puts, [])
        adopted = repo.tasks["exec-a"]["started_at"]
        self.assertEqual(adopted, datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc))

    def test_completed_sin_fin_sella_tarde(self):
        business = business_doc("A", "Completed", openceo_task_id="exec-a",
                                openceo_ref="api:A")
        execution = exec_doc("exec-a", "A", "completed")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([execution])
        service = make_service(adapter, repo)
        wrote = run(service._ensure_own_dates(
            adapter, business, execution, ZONE, NOW, "webhook:applied"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates["exp_end_date"], "2026-10-08 08:00:00")
        self.assertEqual(dates["completed_on"], "2026-10-08")
        self.assertEqual(dates["exp_start_date"], "2026-10-08 08:00:00")
        self.assertEqual(repo.tasks["exec-a"]["completed_at"], NOW)

    def test_completed_sin_inicio_prefiere_creation_a_now(self):
        business = business_doc(
            "A", "Completed", exp_end_date="2026-10-05 18:00:00",
            completed_on="2026-10-05", creation="2026-10-01 12:00:00.000000",
            openceo_task_id="exec-a", openceo_ref="api:A")
        execution = exec_doc("exec-a", "A", "completed")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([execution])
        service = make_service(adapter, repo)
        wrote = run(service._ensure_own_dates(
            adapter, business, execution, ZONE, NOW, "backfill"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates, {"exp_start_date": "2026-10-01 12:00:00"})
        self.assertEqual(repo.tasks["exec-a"]["started_at"],
                         datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc))

    def test_completed_con_fechas_registra_real_sin_escribir(self):
        business = business_doc(
            "A", "Completed", exp_start_date="2026-10-01 09:00:00",
            exp_end_date="2026-10-05 18:00:00", completed_on="2026-10-05",
            openceo_task_id="exec-a", openceo_ref="api:A")
        execution = exec_doc("exec-a", "A", "completed")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([execution])
        service = make_service(adapter, repo)
        wrote = run(service._ensure_own_dates(
            adapter, business, execution, ZONE, NOW, "transition:Completed"))
        self.assertFalse(wrote)
        self.assertEqual(adapter.work_puts, [])
        mirror = repo.tasks["exec-a"]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["exp_end_real"], "2026-10-05 18:00:00")
        # …y no lo confunde con una explícita.
        self.assertIsNone(mirror.get("due_date_explicit"))

    def test_pending_review_se_estima_como_abierta(self):
        business = business_doc("A", "Pending Review",
                                openceo_task_id="exec-a", openceo_ref="api:A")
        execution = exec_doc("exec-a", "A", "waiting")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([execution])
        service = make_service(adapter, repo)
        wrote = run(service._ensure_own_dates(
            adapter, business, execution, ZONE, NOW, "backfill"))
        self.assertTrue(wrote)
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates["exp_start_date"], "2026-10-08 09:00:00")
        self.assertEqual(dates["exp_end_date"], "2026-10-09 18:00:00")

    def test_cancelled_no_escribe_fechas(self):
        business = business_doc("A", "Cancelled", openceo_task_id="exec-a",
                                openceo_ref="api:A")
        execution = exec_doc("exec-a", "A", "cancelled")
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([execution])
        service = make_service(adapter, repo)
        wrote = run(service._ensure_own_dates(
            adapter, business, execution, ZONE, NOW, "webhook:applied"))
        self.assertFalse(wrote)
        self.assertEqual(adapter.work_puts, [])


class TransitionDatesTests(unittest.TestCase):
    def _service(self, business_status="Open", exec_status="queued"):
        business = business_doc("TASK-1", business_status,
                                openceo_task_id="exec-1", openceo_ref="api:TASK-1")
        task = exec_doc("exec-1", "TASK-1", exec_status)
        adapter = FakeBusinessAdapter([business])
        repo = FakeRepo([task])
        return make_service(adapter, repo), adapter, repo

    def test_working_mismo_put_y_started(self):
        service, adapter, repo = self._service("Open", "queued")
        result = run(service.transition_ticket("TASK-1", "Working", "empiezo"))
        self.assertEqual(result["outcome"], "applied")
        _name, _status, dates = adapter.status_puts[0]
        self.assertIn("exp_start_date", dates)
        self.assertIsNone(dates["completed_on"])
        self.assertEqual(dates["progress"], 0)
        self.assertIsNotNone(repo.tasks["exec-1"]["started_at"])

    def test_completed_mismo_put_y_completed_at(self):
        service, adapter, repo = self._service("Working", "in_progress")
        result = run(service.transition_ticket(
            "TASK-1", "Completed", "fin", evidence="suite verde"))
        self.assertEqual(result["outcome"], "applied")
        _name, _status, dates = adapter.status_puts[0]
        self.assertIn("exp_end_date", dates)
        self.assertIn("completed_on", dates)
        self.assertIsNotNone(repo.tasks["exec-1"]["completed_at"])

    def test_reopen_limpia_fin_done_y_completed_at(self):
        service, adapter, repo = self._service("Completed", "completed")
        repo.tasks["exec-1"]["completed_at"] = NOW
        adapter.docs["TASK-1"].update({
            "exp_end_date": "2026-10-05 18:00:00", "completed_on": "2026-10-05"})
        result = run(service.transition_ticket("TASK-1", "Open", "reabre"))
        self.assertEqual(result["outcome"], "applied")
        _name, _status, dates = adapter.status_puts[0]
        self.assertIsNone(dates["exp_end_date"])
        self.assertIsNone(dates["completed_on"])
        self.assertIsNone(repo.tasks["exec-1"]["completed_at"])

    def test_pending_review_no_toca_fechas(self):
        service, adapter, _repo = self._service("Working", "in_progress")
        result = run(service.transition_ticket(
            "TASK-1", "Pending Review", "revisar", human_action="lee esto"))
        self.assertEqual(result["outcome"], "applied")
        _name, _status, dates = adapter.status_puts[0]
        self.assertEqual(dates, {})


class GroupSpanServiceTests(unittest.TestCase):
    def test_explicita_del_grupo_es_suelo(self):
        group = business_doc("G", "Open", is_group=True, openceo_task_id="exec-g",
                             openceo_ref="api:G")
        kid = business_doc("A", "Completed", parent_task="G", progress=100.0,
                           exp_start_date="2026-10-01 09:00:00",
                           exp_end_date="2026-10-02 18:00:00",
                           openceo_task_id="exec-a", openceo_ref="api:A")
        adapter = FakeBusinessAdapter([group, kid])
        group_exec = exec_doc("exec-g", "G", "converted")
        group_exec["params"]["_mirrors"]["erpnext"]["due_date_explicit"] = (
            "2026-10-10 18:00:00")
        repo = FakeRepo([group_exec, exec_doc("exec-a", "A", "completed")])
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-a", "name": "A",
             "reason": "transition:Completed"}))
        self.assertTrue(result["ok"])
        _name, rollup = adapter.rollup_puts[0]
        self.assertEqual(rollup["exp_start_date"], "2026-10-01 09:00:00")
        self.assertEqual(rollup["exp_end_date"], "2026-10-10 18:00:00")

    def test_span_excluye_canceladas(self):
        group = business_doc("G", "Open", is_group=True, openceo_task_id="exec-g",
                             openceo_ref="api:G")
        cancelled = business_doc(
            "C", "Cancelled", parent_task="G",
            exp_start_date="2026-09-01 09:00:00",
            exp_end_date="2026-12-31 18:00:00", openceo_task_id="exec-c",
            openceo_ref="api:C")
        kid = business_doc(
            "A", "Completed", parent_task="G", progress=100.0,
            exp_start_date="2026-10-01 09:00:00",
            exp_end_date="2026-10-02 18:00:00", openceo_task_id="exec-a",
            openceo_ref="api:A")
        adapter = FakeBusinessAdapter([group, cancelled, kid])
        repo = FakeRepo([
            exec_doc("exec-g", "G", "converted"),
            exec_doc("exec-c", "C", "cancelled"),
            exec_doc("exec-a", "A", "completed"),
        ])
        service = make_service(adapter, repo)
        result = run(service._refresh_visibility(
            {"task_id": "exec-a", "name": "A",
             "reason": "transition:Completed"}))
        self.assertTrue(result["ok"])
        _name, rollup = adapter.rollup_puts[0]
        self.assertEqual(rollup["exp_start_date"], "2026-10-01 09:00:00")
        self.assertEqual(rollup["exp_end_date"], "2026-10-02 18:00:00")


class DependentsTests(unittest.TestCase):
    def test_cierre_reestima_dependientes_abiertos(self):
        closed = business_doc(
            "A", "Completed", exp_start_date="2026-10-01 09:00:00",
            exp_end_date="2026-10-08 18:00:00", completed_on="2026-10-08",
            openceo_task_id="exec-a", openceo_ref="api:A")
        dependent = business_doc("B", "Open", depends_on=[{"task": "A"}],
                                 openceo_task_id="exec-b", openceo_ref="api:B")
        unrelated = business_doc("C", "Open", openceo_task_id="exec-c",
                                 openceo_ref="api:C")
        adapter = FakeBusinessAdapter([closed, dependent, unrelated])
        repo = FakeRepo([
            exec_doc("exec-a", "A", "completed"),
            exec_doc("exec-b", "B", "queued"),
            exec_doc("exec-c", "C", "queued"),
        ])
        service = make_service(adapter, repo)
        count = run(service._reestimate_dependents(adapter, closed, repo.tasks["exec-a"], ZONE, NOW))
        self.assertEqual(count, 1)
        self.assertEqual(len(adapter.searches), 1)
        names = [name for name, _dates in adapter.work_puts]
        self.assertEqual(names, ["B"])
        _name, dates = adapter.work_puts[0]
        self.assertEqual(dates["exp_start_date"], "2026-10-08 09:00:00")

    def test_sin_ejecucion_no_hay_por_donde_listar(self):
        closed = business_doc("A", "Completed")
        adapter = FakeBusinessAdapter([closed])
        service = make_service(adapter, FakeRepo([]))
        count = run(service._reestimate_dependents(adapter, closed, None, ZONE, NOW))
        self.assertEqual(count, 0)
        self.assertEqual(adapter.searches, [])


class CreateHookTests(unittest.TestCase):
    def test_crear_encola_visibilidad(self):
        from state import TaskCreateRequest

        adapter = FakeBusinessAdapter([])
        repo = FakeRepo([])
        service = make_service(adapter, repo)
        service._notion_queue = asyncio.Queue()
        created = run(service.create_task(TaskCreateRequest(
            source=TaskSource.API, title="t", description="d")))
        self.assertEqual(created["params"]["_mirrors"]["erpnext"]["name"], "TASK-9")
        jobs = drain(service._visibility_queue)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["name"], "TASK-9")
        self.assertEqual(jobs[0]["reason"], "create")
