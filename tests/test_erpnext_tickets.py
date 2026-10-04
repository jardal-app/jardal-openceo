"""Adapter de tickets hacia ERPNext.

Lo que se prueba aqui son las decisiones que NO son obvias y que ya costaron
verificacion contra el sitio vivo:

- el titulo se trunca a 140 porque `Task.subject` es Data(140) y Frappe falla en vez
  de truncar;
- la descripcion se escapa porque `Task.description` es un Text Editor y Frappe
  sanea el HTML, corrompiendo texto plano en silencio;
- `completed` **no** se empuja, porque cerrar un ticket es una decision de negocio y
  no un efecto de que un agente termine su ejecucion;
- un 417 significa "ya existia", no un fallo.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from tools.erpnext_client import ERPNextDuplicateError, ERPNextValidationError  # noqa: E402
from tools.erpnext_tickets import (  # noqa: E402
    SUBJECT_MAX_LENGTH,
    ERPNextTicketAdapter,
    map_coding_tool,
    map_priority,
    map_status,
    normalize_due_date,
    resolve_erpnext_status,
    tags_footer,
    to_html_description,
    truncate_subject,
)
from state import CodingTool  # noqa: E402

TITULO_LARGO = "Arreglar el login con Google cuando el usuario tiene 2FA activo " * 4


class FakeClient:
    """Cliente falso: registra llamadas y permite forzar errores."""

    def __init__(self, *, create_error: Exception | None = None, by_ref: dict | None = None) -> None:
        self.create_error = create_error
        self.by_ref = by_ref
        self.is_configured = True
        self.created: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.listed: list[tuple[str, list | None]] = []
        self.limits: list[int] = []
        self.docs: dict[str, dict] = {}

    async def get_doc(self, doctype, name):
        row = self.docs.get(name)
        if row is None:
            raise ERPNextValidationError(f"No existe {doctype} {name}")
        return row

    def config_error(self) -> str | None:
        return None if self.is_configured else "Falta configuracion de ERPNext: ERPNEXT_URL."

    async def list_docs(self, doctype, *, filters=None, fields=None, limit=20):
        self.listed.append((doctype, filters))
        self.limits.append(limit)
        if doctype == "Task Type":
            return [{"name": "Bug"}, {"name": "Feature"}]
        if doctype == "Project":
            if filters and any(f[1] == "=" and f[2] == "proj-x" for f in filters):
                return [{"name": "PROJ-0001"}]
            return []
        if doctype == "Task" and self.by_ref is not None and filters:
            return [self.by_ref]
        return []

    async def create_doc(self, doctype, body):
        if self.create_error is not None:
            raise self.create_error
        self.created.append((doctype, body))
        return {"name": "TASK-2026-00001", **body}

    async def update_doc(self, doctype, name, body):
        self.updated.append((doctype, name, body))
        return {"name": name, **body}


def make_adapter(client: FakeClient) -> ERPNextTicketAdapter:
    settings = SimpleNamespace(ERPNEXT_TICKETS_PROJECT_COMPANY="Jardal")
    return ERPNextTicketAdapter(settings, client=client)  # type: ignore[arg-type]


def run(coro):
    return asyncio.run(coro)


class MappingTests(unittest.TestCase):
    def test_subject_se_trunca_al_limite_de_frappe(self):
        self.assertEqual(len(truncate_subject(TITULO_LARGO)), SUBJECT_MAX_LENGTH)
        self.assertTrue(truncate_subject(TITULO_LARGO).endswith("…"))

    def test_subject_corto_no_se_toca(self):
        self.assertEqual(truncate_subject("  Arreglar login  "), "Arreglar login")

    def test_subject_vacio_no_produce_cadena_vacia(self):
        # `subject` es el unico campo obligatorio de Task: vacio fallaria el POST.
        self.assertTrue(truncate_subject("").strip())

    def test_descripcion_escapa_html_y_convierte_saltos(self):
        html = to_html_description("Detalle con <b>etiquetas</b> & ampersand\nsegunda linea")
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("&amp;", html)
        self.assertIn("<br>", html)
        self.assertNotIn("<b>", html)

    def test_prioridad_critical_mapea_a_urgent(self):
        # OpenCEO dice `critical`; ERPNext solo admite Low/Medium/High/Urgent.
        self.assertEqual(map_priority("critical"), "Urgent")
        self.assertEqual(map_priority("high"), "High")
        self.assertEqual(map_priority(None), "Medium")
        self.assertEqual(map_priority("lo-que-sea"), "Medium")

    def test_completed_empuja_a_pending_review(self):
        """Terminar el trabajo lleva el ticket a la cola QA sin intervencion manual.

        `docs/ARCHITECTURE_REFERENCE_2026.md` §50/§119: "completed en un registro de
        ejecucion no salta una aprobacion de negocio". Por eso se empuja `Pending Review`
        (cola de revision) y NUNCA `Completed`: el cierre sigue siendo decision de negocio
        en el desk (TASK-2026-00033). `failed`/`escalated`/`interrupted` no se empujan:
        son resultado de ejecucion, no de negocio.
        """
        self.assertEqual(map_status("completed"), "Pending Review")
        self.assertIsNone(map_status("failed"))
        self.assertIsNone(map_status("escalated"))
        self.assertIsNone(map_status("interrupted"))

    def test_se_empujan_cuatro_estados(self):
        self.assertEqual(map_status("queued"), "Open")
        self.assertEqual(map_status("in_progress"), "Working")
        self.assertEqual(map_status("waiting"), "Pending Review")

    def test_fecha_se_normaliza_a_datetime(self):
        # `exp_end_date` es Datetime: una fecha desnuda no entra de forma fiable.
        self.assertEqual(normalize_due_date("2026-09-30"), "2026-09-30 00:00:00")
        self.assertEqual(normalize_due_date("2026-09-30 14:00:00"), "2026-09-30 14:00:00")
        self.assertIsNone(normalize_due_date(""))
        self.assertIsNone(normalize_due_date(None))

    def test_etiquetas_van_al_cuerpo(self):
        # Frappe descarta `_user_tags` en un POST por REST, asi que no se pierden: se
        # muestran en el cuerpo del ticket.
        footer = tags_footer(["backend", "auth"])
        self.assertIn("backend", footer)
        self.assertIn("auth", footer)
        self.assertEqual(tags_footer([]), "")

    def test_provider_se_normaliza_desde_el_enum(self):
        self.assertEqual(map_coding_tool(CodingTool.TIER_S), "tier_s")
        self.assertEqual(map_coding_tool("TIER_S"), "tier_s")
        self.assertEqual(map_coding_tool("tier_a"), "tier_a")
        self.assertEqual(map_coding_tool("TIER_C"), "tier_c")
        # Desk moved to the v2 letters on 2026-10-04: provider names would
        # fail the POST, so they are omitted until the code migrates to v2.
        self.assertIsNone(map_coding_tool("codex"))

    def test_provider_desconocido_devuelve_none(self):
        """`openceo_coding_tool` es un Select: un valor invalido haria fallar el POST.

        Omitirlo pierde un dato de ejecucion; mandarlo perderia el ticket entero.
        """
        self.assertIsNone(map_coding_tool("provider-nuevo"))
        self.assertIsNone(map_coding_tool(""))
        self.assertIsNone(map_coding_tool(None))


class PayloadTests(unittest.TestCase):
    def test_payload_completo(self):
        adapter = make_adapter(FakeClient())
        payload = run(
            adapter.build_payload(
                ref="api:abc",
                title=TITULO_LARGO,
                description="cuerpo",
                project_name="PROJ-0001",
                priority="critical",
                ticket_type="Bug",
                tags=["backend"],
                due_date="2026-09-30",
                ticket_number=48,
                task_id="task-1",
            )
        )
        self.assertEqual(len(payload["subject"]), SUBJECT_MAX_LENGTH)
        self.assertEqual(payload["status"], "Open")
        self.assertEqual(payload["priority"], "Urgent")
        self.assertEqual(payload["type"], "Bug")
        self.assertEqual(payload["project"], "PROJ-0001")
        self.assertEqual(payload["exp_end_date"], "2026-09-30 00:00:00")
        self.assertEqual(payload["openceo_ref"], "api:abc")
        self.assertEqual(payload["openceo_ticket_number"], 48)
        self.assertEqual(payload["openceo_task_id"], "task-1")
        # El titulo completo no se pierde al truncar: sobrevive en el cuerpo.
        self.assertIn(TITULO_LARGO.strip()[:60], payload["description"])
        self.assertNotIn("_user_tags", payload)

    def test_tipo_desconocido_se_omite_y_no_tumba_el_ticket(self):
        adapter = make_adapter(FakeClient())
        payload = run(adapter.build_payload(ref="r", title="t", ticket_type="NoExiste"))
        self.assertNotIn("type", payload)

    def test_sin_campos_opcionales_no_se_inventan_claves(self):
        adapter = make_adapter(FakeClient())
        payload = run(adapter.build_payload(ref="r", title="t"))
        self.assertNotIn("project", payload)
        self.assertNotIn("exp_end_date", payload)
        self.assertNotIn("description", payload)
        self.assertNotIn("openceo_coding_tool", payload)
        self.assertNotIn("openceo_external_url", payload)

    def test_provider_y_origen_llegan_a_erpnext(self):
        """Sin ellos el tablero oculta si un ticket es Tier S y de donde vino."""
        adapter = make_adapter(FakeClient())
        payload = run(
            adapter.build_payload(
                ref="r",
                title="t",
                coding_tool=CodingTool.TIER_S,
                external_url="https://x.test/1",
            )
        )
        self.assertEqual(payload["openceo_coding_tool"], "tier_s")
        self.assertEqual(payload["openceo_external_url"], "https://x.test/1")

    def test_provider_invalido_no_tumba_el_ticket(self):
        adapter = make_adapter(FakeClient())
        payload = run(adapter.build_payload(ref="r", title="t", coding_tool="lo-que-sea"))
        self.assertNotIn("openceo_coding_tool", payload)
        self.assertEqual(payload["subject"], "t")


class EnsureProjectTests(unittest.TestCase):
    def test_devuelve_el_existente_sin_crear(self):
        client = FakeClient()
        adapter = make_adapter(client)
        name = run(adapter.ensure_project("proj-x"))
        self.assertEqual(name, "PROJ-0001")
        self.assertEqual(client.created, [])

    def test_crea_cuando_no_existe_con_company_y_naming_series(self):
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.ensure_project("proj-nuevo"))
        doctype, body = client.created[0]
        self.assertEqual(doctype, "Project")
        # Project exige naming_series, project_name y company: faltar uno falla el POST.
        self.assertEqual(body["naming_series"], "PROJ-.####")
        self.assertEqual(body["company"], "Jardal")
        self.assertEqual(body["project_key"], "proj-nuevo")

    def test_cachea_para_no_repetir_la_busqueda(self):
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.ensure_project("proj-x"))
        run(adapter.ensure_project("proj-x"))
        self.assertEqual(len([c for c in client.listed if c[0] == "Project"]), 1)


class CreateTicketTests(unittest.TestCase):
    def test_crea_y_devuelve_el_name(self):
        client = FakeClient()
        adapter = make_adapter(client)
        result = run(adapter.create_ticket(ref="api:1", title="Hola", project_key="proj-x"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["task"]["name"], "TASK-2026-00001")

    def test_417_se_trata_como_exito_idempotente(self):
        """Un 417 significa que un intento anterior SI creo el documento."""
        existente = {"name": "TASK-2026-00007", "status": "Open", "openceo_ref": "api:1"}
        client = FakeClient(
            create_error=ERPNextDuplicateError("duplicado"),
            by_ref=existente,
        )
        adapter = make_adapter(client)
        result = run(adapter.create_ticket(ref="api:1", title="Hola"))
        self.assertTrue(result["ok"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["task"]["name"], "TASK-2026-00007")

    def test_update_status_no_llama_si_el_estado_no_se_empuja(self):
        client = FakeClient()
        adapter = make_adapter(client)
        self.assertIsNone(run(adapter.update_status("TASK-1", "failed")))
        self.assertIsNone(run(adapter.update_status("TASK-1", "escalated")))
        self.assertEqual(client.updated, [])

    def test_update_status_completed_empuja_pending_review(self):
        """TASK-2026-00033: sin contexto rige el defecto seguro (cola QA)."""
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.update_status("TASK-1", "completed"))
        self.assertEqual(client.updated[0], ("Task", "TASK-1", {"status": "Pending Review"}))

    def test_update_status_completed_qa_condicional(self):
        """TASK-2026-00040: solo va a QA lo que necesita ojo humano, el resto a Done."""
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.update_status("TASK-1", "completed", task={"status": "completed"}))
        self.assertEqual(client.updated[-1], ("Task", "TASK-1", {"status": "Completed"}))
        run(adapter.update_status(
            "TASK-1", "completed",
            task={"status": "completed", "qa_review_required": True,
                  "qa_review_state": "pending"}))
        self.assertEqual(client.updated[-1], ("Task", "TASK-1", {"status": "Pending Review"}))
        run(adapter.update_status(
            "TASK-1", "completed",
            task={"status": "completed", "qa_review_required": True,
                  "qa_review_state": "approved"}))
        self.assertEqual(client.updated[-1], ("Task", "TASK-1", {"status": "Completed"}))

    def test_resolve_erpnext_status_no_qa_es_done(self):
        self.assertEqual(resolve_erpnext_status({"status": "completed"}), "Completed")
        self.assertEqual(
            resolve_erpnext_status({"status": "completed", "qa_review_required": True,
                                    "qa_review_state": "pending"}),
            "Pending Review",
        )
        self.assertEqual(resolve_erpnext_status({"status": "in_progress"}), "Working")
        self.assertIsNone(resolve_erpnext_status({"status": "failed"}))
        self.assertIsNone(resolve_erpnext_status({"status": "lo-que-sea"}))

    def test_autorun_se_persiste_en_el_documento(self):
        """TASK-2026-00043: el False del registro se perdia (TaskDocument sin campo)
        y el scheduler lanzaba el ticket igual -> Working fantasma."""
        from state import TaskCreateRequest, TaskSource, make_task_document

        def make_doc(**kw):
            return make_task_document(
                TaskCreateRequest(source=TaskSource.API, title="t", description="d", **kw))

        self.assertFalse(make_doc(autorun=False).autorun)
        self.assertTrue(make_doc(autorun=True).autorun)
        self.assertTrue(make_doc().autorun)

    def test_scheduler_respeta_autorun_false(self):
        """Registrar con autorun=false nunca ejecuta, ni en el reencolado."""
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        self.assertFalse(service._task_allows_autorun({"autorun": False}))
        self.assertTrue(service._task_allows_autorun({"autorun": True}))
        self.assertTrue(service._task_allows_autorun({}))

    def test_update_status_empuja_los_que_si(self):
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.update_status("TASK-1", "in_progress"))
        self.assertEqual(client.updated[0], ("Task", "TASK-1", {"status": "Working"}))

    def test_tier_s_progress_replaces_own_section_and_escapes_question(self):
        client = FakeClient()
        client.docs["TASK-1"] = {"description": "<p>Original</p>"}
        adapter = make_adapter(client)
        summary = {"phase": "planner", "status": "waiting", "subplan": 1,
                   "round": 1, "reviewers": {},
                   "question": {"text": "Use <script>?", "options": ["A & B", "C"]}}
        run(adapter.update_tier_s_progress("TASK-1", summary))
        first = client.updated[-1][2]["description"]
        self.assertIn("<p>Original</p>", first)
        self.assertIn("Use &lt;script&gt;?", first)
        self.assertIn("A &amp; B", first)
        self.assertNotIn("status", client.updated[-1][2])
        client.docs["TASK-1"]["description"] = first
        run(adapter.update_tier_s_progress("TASK-1", {**summary, "phase": "review"}))
        self.assertEqual(client.updated[-1][2]["description"].count("OPENCEO_TIER_S_BEGIN"), 1)


class FakeAdapter:
    """Adapter falso: ejercita las tres ramas sin red."""

    def __init__(self, *, enabled: bool = True, error: Exception | None = None) -> None:
        self.is_enabled = enabled
        self.error = error
        self.calls: list[dict] = []

        class _Client:
            @staticmethod
            def config_error() -> str | None:
                return None if enabled else "Falta configuracion de ERPNext: ERPNEXT_URL."

        self.client = _Client()

    async def create_ticket(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"ok": True, "task": {"name": "TASK-2026-00042", "status": "Open"}, "ref": kwargs.get("ref")}


class BusinessRegistrationTests(unittest.TestCase):
    """`TaskService._register_business_ticket`: las tres ramas de la Fase 2."""

    def _service(self, adapter, *, personal_mirror: bool = False):
        from state import TaskCreateRequest, TaskSource, make_task_document
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._settings = SimpleNamespace(
            TASK_AUTORUN=False,
            HORIZON_PERSONAL_MIRROR_ENABLED=personal_mirror,
        )
        service._erpnext = adapter
        self._make_doc = lambda **kw: make_task_document(
            TaskCreateRequest(source=TaskSource.API, title="t", description="d", **kw)
        )
        return service

    def test_sin_adapter_no_hace_nada(self):
        service = self._service(None)
        mirror, pending = run(service._register_business_ticket(self._make_doc(project="jardal-openceo")))
        self.assertIsNone(mirror)
        self.assertFalse(pending)

    def test_no_configurado_falla_fuerte(self):
        """No configurado NO es un modo degradado: es un error de despliegue.

        Antes existia un flag `ERPNEXT_TICKETS_ENABLED` que, apagado, dejaba crear
        tickets solo en Mongo. Eso producia tickets que el sistema de negocio nunca
        veria, y la reconciliacion no los recupera. Ahora se falla.
        """
        from tools.erpnext_client import ERPNextValidationError

        adapter = FakeAdapter(enabled=False)
        service = self._service(adapter)
        with self.assertRaises(ERPNextValidationError):
            run(service._register_business_ticket(self._make_doc(project="jardal-openceo")))
        self.assertEqual(adapter.calls, [])

    def test_alcance_personal_no_se_publica(self):
        """Horizon §9.9: el espejo externo personal esta deshabilitado por defecto."""
        from horizon.privacy import personal_scope_params

        adapter = FakeAdapter()
        service = self._service(adapter)
        doc = self._make_doc(project="jardal-openceo")
        doc.params = personal_scope_params("usr_1", extra={})
        mirror, pending = run(service._register_business_ticket(doc))
        self.assertIsNone(mirror)
        self.assertFalse(pending)
        self.assertEqual(adapter.calls, [], "un ticket personal no debe llegar a ERPNext")

    def test_alcance_personal_se_publica_si_el_dueno_lo_habilita(self):
        from horizon.privacy import personal_scope_params

        adapter = FakeAdapter()
        service = self._service(adapter, personal_mirror=True)
        doc = self._make_doc(project="jardal-openceo")
        doc.params = personal_scope_params("usr_1", extra={})
        mirror, pending = run(service._register_business_ticket(doc))
        self.assertIsNotNone(mirror)
        self.assertFalse(pending)

    def test_exito_devuelve_el_enlace(self):
        adapter = FakeAdapter()
        service = self._service(adapter)
        mirror, pending = run(service._register_business_ticket(self._make_doc(source_id="s1", project="jardal-openceo")))
        self.assertFalse(pending)
        self.assertEqual(mirror["name"], "TASK-2026-00042")
        # `status_pushed` guarda el vocabulario de OpenCEO (`queued`), no el de ERPNext
        # (`Open`): es contra este valor que el observador decide si hubo transicion.
        self.assertEqual(mirror["status_pushed"], "queued")
        self.assertEqual(adapter.calls[0]["project_key"], "jardal-openceo")

    def test_erpnext_caido_deja_el_enlace_pendiente_y_no_lanza(self):
        from tools.erpnext_client import ERPNextUnavailable

        adapter = FakeAdapter(error=ERPNextUnavailable("timeout"))
        service = self._service(adapter)
        mirror, pending = run(service._register_business_ticket(self._make_doc(project="jardal-openceo")))
        self.assertTrue(pending, "ERPNext caido debe marcar el enlace como pendiente")
        self.assertIsNone(mirror["name"])
        self.assertTrue(mirror["ref"])

    def test_datos_invalidos_lanzan_para_no_dejar_ejecucion_huerfana(self):
        """Un 4xx no puede producir un ticket de ejecucion sin contraparte de negocio."""
        from tools.erpnext_client import ERPNextValidationError

        adapter = FakeAdapter(error=ERPNextValidationError("subject invalido"))
        service = self._service(adapter)
        with self.assertRaises(ERPNextValidationError):
            run(service._register_business_ticket(self._make_doc(project="jardal-openceo")))

    def test_ref_determinista_con_source_id(self):
        adapter = FakeAdapter()
        service = self._service(adapter)
        run(service._register_business_ticket(self._make_doc(source_id="abc", project="p")))
        self.assertEqual(adapter.calls[0]["ref"], "api:abc")

    def test_ref_usa_task_id_cuando_no_hay_source_id(self):
        adapter = FakeAdapter()
        service = self._service(adapter)
        doc = self._make_doc(project="p")
        run(service._register_business_ticket(doc))
        self.assertEqual(adapter.calls[0]["ref"], doc.task_id)


class StatusObserverTests(unittest.TestCase):
    """Propagacion de transiciones: que se empuja, que no, y por que."""

    def _service(self, adapter, *, status_pushed="Open", personal_mirror=False):
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._settings = SimpleNamespace(
            TASK_AUTORUN=False, HORIZON_PERSONAL_MIRROR_ENABLED=personal_mirror
        )
        service._erpnext = adapter
        service._erpnext_queue = asyncio.Queue()
        self._mirror = {"name": "TASK-1", "ref": "api:1", "status_pushed": status_pushed}
        return service

    def _task(self, status, **params):
        return {
            "task_id": "t1",
            "status": status,
            "params": {"_mirrors": {"erpnext": dict(self._mirror)}, **params},
        }

    def _queued(self, service):
        items = []
        while not service._erpnext_queue.empty():
            items.append(service._erpnext_queue.get_nowait())
        return items

    def test_in_progress_se_empuja(self):
        service = self._service(FakeAdapter())
        run(service.observe_task_status(self._task("in_progress")))
        self.assertEqual(self._queued(service), [{"task_id": "t1", "name": "TASK-1", "status": "in_progress"}])

    def test_completed_se_empuja_a_pending_review(self):
        """Terminar lleva el ticket a la cola QA: sale de `Working` sin acto manual."""
        service = self._service(FakeAdapter())
        run(service.observe_task_status(self._task("completed")))
        self.assertEqual(self._queued(service), [{"task_id": "t1", "name": "TASK-1", "status": "completed"}])

    def test_failed_no_se_empuja(self):
        service = self._service(FakeAdapter())
        run(service.observe_task_status(self._task("failed")))
        self.assertEqual(self._queued(service), [])

    def test_espera_operativa_tier_s_no_se_convierte_en_revision_de_negocio(self):
        service = self._service(FakeAdapter(), status_pushed="in_progress")
        task = self._task("waiting")
        task["tier_s_progress"] = {"status": "waiting", "phase": "planner"}
        run(service.observe_task_status(task))
        self.assertEqual(self._queued(service), [])
        for terminal in ("cancelled", "failed"):
            task["tier_s_progress"] = {"status": terminal, "phase": "planner"}
            run(service.observe_task_status(task))
            self.assertEqual(self._queued(service), [])
        task["tier_s_progress"] = {"status": "completed", "phase": "complete"}
        run(service.observe_task_status(task))
        self.assertEqual(self._queued(service)[0]["status"], "waiting")

    def test_estado_ya_empujado_no_se_repite(self):
        """Idempotencia: se compara contra el ultimo estado EMPUJADO, no contra la base.

        Ambos valores estan en el vocabulario de OpenCEO: comparar `in_progress` contra el
        `Working` de ERPNext haria que cada parche pareciera una transicion nueva.
        """
        service = self._service(FakeAdapter(), status_pushed="in_progress")
        run(service.observe_task_status(self._task("in_progress")))
        self.assertEqual(self._queued(service), [])

    def test_enlace_pendiente_no_empuja_estado(self):
        service = self._service(FakeAdapter())
        servicio_mirror = dict(self._mirror)
        servicio_mirror["name"] = None
        task = self._task("in_progress")
        task["params"]["_mirrors"]["erpnext"] = servicio_mirror
        run(service.observe_task_status(task))
        self.assertEqual(self._queued(service), [])

    def test_alcance_personal_no_se_publica(self):
        from horizon.privacy import personal_scope_params

        service = self._service(FakeAdapter())
        task = self._task("in_progress")
        task["params"].update(personal_scope_params("usr_1", extra={}))
        run(service.observe_task_status(task))
        self.assertEqual(self._queued(service), [], "una transicion personal no debe salir a ERPNext")

    def test_adapter_apagado_no_encola(self):
        service = self._service(FakeAdapter(enabled=False))
        run(service.observe_task_status(self._task("in_progress")))
        self.assertEqual(self._queued(service), [])


class ReconciliationQueryTests(unittest.TestCase):
    """La consulta de arranque: sin el filtro de privacidad se publican datos personales."""

    def _query(self, rows):
        import horizon_support

        repo = horizon_support.make_fake_mongo(rows)
        run(repo.list_tasks_needing_erpnext_sync())
        return repo.tasks.last_query

    def test_lleva_el_filtro_de_privacidad(self):
        query = self._query([])
        self.assertEqual(query["params.horizon_scope"], {"$ne": "personal"})

    def test_solo_mira_tickets_que_ya_intentaron_enlazar(self):
        """No hace backfill: los tickets historicos no se vuelven registros de negocio."""
        query = self._query([])
        self.assertEqual(query["params._mirrors.erpnext"], {"$exists": True})


class ReversePathTests(unittest.TestCase):
    """Camino inverso: una decision humana en ERPNext llega al runtime.

    Solo se propagan las decisiones de NEGOCIO. Los demas estados de `Task` son eco de
    lo que OpenCEO ya empujo, y propagarlos seria un bucle infinito.
    """

    def _service(self, status="in_progress", *, found=True):
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._settings = SimpleNamespace(TASK_AUTORUN=False, HORIZON_PERSONAL_MIRROR_ENABLED=False)
        service._erpnext = FakeAdapter()
        self._task = {
            "task_id": "t1",
            "status": status,
            "params": {"_mirrors": {"erpnext": {"name": "TASK-1", "ref": "api:1", "status_pushed": status}}},
        }
        self._patches: list[tuple[str, dict]] = []

        async def fake_get_task(task_id):
            return dict(self._task) if found else None

        async def fake_patch_task(task_id, updates):
            self._patches.append((task_id, updates))
            return {**self._task, **updates}

        service._repo = SimpleNamespace(get_task=fake_get_task)
        service.patch_task = fake_patch_task  # type: ignore[method-assign]
        return service

    def test_completed_del_negocio_cierra_el_ticket(self):
        service = self._service("waiting")
        result = run(service.apply_business_status(task_id="t1", business_status="Completed"))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["task_status"], "completed")
        self.assertEqual(self._patches[0][1]["status"], "completed")

    def test_cancelled_no_se_reanuda_nunca(self):
        service = self._service("waiting")
        result = run(service.apply_business_status(task_id="t1", business_status="Cancelled"))
        self.assertEqual(result["task_status"], "cancelled")
        updates = self._patches[0][1]
        self.assertEqual(updates["status"], "cancelled")
        # Sin esto, un `waiting` con resume_at volveria a arrancar trabajo cancelado.
        self.assertIsNone(updates["resume_at"])

    def test_borrado_en_erpnext_cancela_el_ticket(self):
        service = self._service("in_progress")
        result = run(service.apply_business_status(task_id="t1", deleted=True))
        self.assertEqual(result["task_status"], "cancelled")
        self.assertEqual(self._patches[0][1]["status"], "cancelled")

    def test_estado_eco_se_ignora(self):
        """`Working` es lo que OpenCEO ya empujo: reenviarlo seria un bucle."""
        service = self._service("in_progress")
        result = run(service.apply_business_status(task_id="t1", business_status="Working"))
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(self._patches, [])

    def test_estado_ya_aplicado_no_se_repite(self):
        service = self._service("completed")
        result = run(service.apply_business_status(task_id="t1", business_status="Completed"))
        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(self._patches, [])

    def test_el_parche_lleva_el_guardia_anti_eco(self):
        service = self._service("waiting")
        run(service.apply_business_status(task_id="t1", business_status="Completed"))
        mirror = self._patches[0][1]["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["status_pushed"], "completed")
        self.assertEqual(mirror["business_status"], "Completed")

    def test_ticket_desconocido_no_revienta(self):
        service = self._service(found=False)
        result = run(service.apply_business_status(task_id="no-existe", business_status="Completed"))
        self.assertEqual(result["status"], "not_found")

    def test_resuelve_por_ref_cuando_no_hay_task_id(self):
        from task_service import TaskService

        service = self._service("waiting")
        vistos = []

        async def fake_find(source, source_id):
            vistos.append((source, source_id))
            return dict(self._task)

        service._repo.find_task_by_source = fake_find
        result = run(service.apply_business_status(ref="api:1", business_status="Completed"))
        self.assertEqual(vistos, [("api", "1")])
        self.assertEqual(result["status"], "applied")


class SearchAndReadTests(unittest.TestCase):
    """Buscar antes de crear: es lo que evita el ticket duplicado."""

    def test_busqueda_arma_filtro_de_texto(self):
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.search_tickets(query="login google"))
        _, filters = [c for c in client.listed if c[0] == "Task"][0]
        self.assertIn(["subject", "like", "%login google%"], filters)

    def test_busqueda_filtra_por_proyecto_y_estado(self):
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.search_tickets(project_key="proj-x", status="Open"))
        _, filters = [c for c in client.listed if c[0] == "Task"][0]
        self.assertIn(["project", "=", "PROJ-0001"], filters)
        self.assertIn(["status", "=", "Open"], filters)

    def test_busqueda_en_proyecto_inexistente_no_crea_nada(self):
        """Camino de lectura: un project_key desconocido NO debe crear el Project."""
        client = FakeClient()
        adapter = make_adapter(client)
        rows = run(adapter.search_tickets(project_key="no-existe"))
        self.assertEqual(rows, [])
        self.assertEqual(client.created, [], "buscar no puede tener efectos secundarios")

    def test_sin_filtros_no_inventa_clausulas(self):
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.search_tickets())
        _, filters = [c for c in client.listed if c[0] == "Task"][0]
        self.assertIsNone(filters)

    def test_limite_se_acota(self):
        client = FakeClient()
        adapter = make_adapter(client)
        run(adapter.search_tickets(limit=9999))
        self.assertEqual(client.limits[-1], 100)

    def test_get_ticket_por_nombre_usa_el_doc_directo(self):
        client = FakeClient()
        client.docs["TASK-2026-00025"] = {"name": "TASK-2026-00025", "subject": "x"}
        adapter = make_adapter(client)
        row = run(adapter.get_ticket("TASK-2026-00025"))
        self.assertEqual(row["name"], "TASK-2026-00025")

    def test_get_ticket_por_ref_cae_a_busqueda(self):
        client = FakeClient(by_ref={"name": "TASK-2026-00026", "openceo_ref": "api:1"})
        adapter = make_adapter(client)
        row = run(adapter.get_ticket("api:1"))
        self.assertEqual(row["name"], "TASK-2026-00026")

    def test_get_ticket_inexistente_devuelve_none(self):
        client = FakeClient()
        adapter = make_adapter(client)
        self.assertIsNone(run(adapter.get_ticket("TASK-2026-99999")))


if __name__ == "__main__":
    unittest.main()
