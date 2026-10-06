"""Jerarquia de negocio en ERPNext (TASK-2026-00013).

Lo que se prueba aqui es el contrato verificado contra el sitio vivo:

- Frappe devuelve 417 para duplicados Y validaciones: el cliente clasifica por
  contenido (`classify_417`), no por codigo;
- `parent_task`/`depends_on`/`is_group` viajan en la creacion y en operaciones
  semanticas cerradas (nunca un `create_document` generico);
- `blocked_by` (task_ids) se resuelve a nombres de negocio para `depends_on`;
- completar con dependientes abiertos no es un error de datos: queda pendiente y
  se reintenta (orden topologico);
- los cambios de jerarquia no generan eco en el camino inverso.
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

import mcp_server  # noqa: E402
from state import TaskCreateRequest, TaskSource, make_task_document  # noqa: E402
from tools.erpnext_client import (  # noqa: E402
    ERPNextDependencyBlockedError,
    ERPNextDuplicateError,
    ERPNextValidationError,
    classify_417,
)
from tools.erpnext_tickets import (  # noqa: E402
    ERPNextTicketAdapter,
    depends_on_names,
    depends_on_rows,
)


def run(coro):
    return asyncio.run(coro)


# --- payloads reales observados contra el sitio vivo (2026-10-06) -------------

DUPLICATE_PAYLOAD = {
    "exception": (
        "frappe.exceptions.UniqueValidationError: ('Task', 'TASK-2026-00129', "
        "IntegrityError(1062, \"Duplicate entry 'r1' for key 'openceo_ref'\"))"
    ),
    "exc_type": "UniqueValidationError",
    "_server_messages": ['{"message":"OpenCEO Ref must be unique"}'],
}

DEPENDENCY_BLOCKED_PAYLOAD = {
    "exception": (
        "frappe.exceptions.ValidationError: Cannot complete task "
        "<strong>TASK-2026-00127</strong> as its dependant task "
        "<strong>TASK-2026-00125</strong> are not completed / cancelled."
    ),
    "exc_type": "ValidationError",
}

PARENT_NOT_GROUP_PAYLOAD = {
    "exception": (
        "erpnext.projects.doctype.task.task.ParentIsGroupError: Parent Task "
        "TASK-2026-00126 must be a Group Task"
    ),
    "exc_type": "ParentIsGroupError",
}

LINK_EXISTS_PAYLOAD = {
    "exception": (
        "frappe.exceptions.LinkExistsError: Cannot delete or cancel because Task "
        "TASK-2026-00125 is linked with Task TASK-2026-00124"
    ),
    "exc_type": "LinkExistsError",
}


class Classify417Tests(unittest.TestCase):
    def test_duplicado_por_exc_type(self):
        self.assertIs(classify_417(DUPLICATE_PAYLOAD), ERPNextDuplicateError)

    def test_duplicado_por_texto_aunque_falte_exc_type(self):
        payload = {"exception": "Duplicate entry 'x' for key 'openceo_ref'"}
        self.assertIs(classify_417(payload), ERPNextDuplicateError)

    def test_bloqueo_por_dependientes_es_transitorio(self):
        self.assertIs(classify_417(DEPENDENCY_BLOCKED_PAYLOAD), ERPNextDependencyBlockedError)

    def test_padre_no_grupo_es_error_de_datos(self):
        self.assertIs(classify_417(PARENT_NOT_GROUP_PAYLOAD), ERPNextValidationError)

    def test_enlace_existente_es_error_de_datos(self):
        self.assertIs(classify_417(LINK_EXISTS_PAYLOAD), ERPNextValidationError)

    def test_payload_vacio_o_desconocido_es_error_de_datos(self):
        self.assertIs(classify_417({}), ERPNextValidationError)
        self.assertIs(classify_417(None), ERPNextValidationError)

    def test_bloqueo_es_subclase_de_validacion(self):
        # Los llamadores que solo distinguen duro/transitorio siguen viendo un
        # ValidationError; los que reintentan afinan con la subclase.
        self.assertTrue(issubclass(ERPNextDependencyBlockedError, ERPNextValidationError))


class DependsOnHelperTests(unittest.TestCase):
    def test_filas_con_formato_de_frappe(self):
        self.assertEqual(
            depends_on_rows(["TASK-1", "TASK-2"]),
            [{"task": "TASK-1"}, {"task": "TASK-2"}],
        )

    def test_vacio_devuelve_none_para_no_vaciar_sin_querer(self):
        # Un PUT reemplaza la tabla: mandar [] sin querer la vaciaria.
        self.assertIsNone(depends_on_rows(None))
        self.assertIsNone(depends_on_rows([]))
        self.assertIsNone(depends_on_rows(["  ", ""]))

    def test_limpia_y_deduplica_conservando_orden(self):
        self.assertEqual(
            depends_on_rows([" TASK-2 ", "TASK-1", "TASK-2", ""]),
            [{"task": "TASK-2"}, {"task": "TASK-1"}],
        )

    def test_nombres_desde_doc_completo(self):
        task = {"depends_on": [{"task": "TASK-1"}, {"task": "TASK-2"}, {"task": ""}, "basura"]}
        self.assertEqual(depends_on_names(task), ["TASK-1", "TASK-2"])

    def test_nombres_none_cuando_el_list_no_trae_la_tabla(self):
        self.assertIsNone(depends_on_names({"name": "TASK-1"}))
        self.assertIsNone(depends_on_names(None))


class FakeClient:
    """Cliente falso con documentos dirigibles por nombre y por ref."""

    def __init__(self) -> None:
        self.is_configured = True
        self.docs: dict[str, dict] = {}
        self.created: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []

    def config_error(self) -> str | None:
        return None

    async def get_doc(self, doctype, name):
        row = self.docs.get(name)
        if row is None:
            raise ERPNextValidationError(f"No existe {doctype} {name}")
        return dict(row)

    async def list_docs(self, doctype, *, filters=None, fields=None, limit=20):
        if doctype == "Task Type":
            return [{"name": "Bug"}]
        if doctype == "Project":
            return []
        if doctype == "Task" and filters:
            for field, _op, value in filters:
                for row in self.docs.values():
                    if row.get(field) == value:
                        return [dict(row)]
            return []
        return []

    async def create_doc(self, doctype, body):
        self.created.append((doctype, dict(body)))
        return {"name": "TASK-2026-00999", **body}

    async def update_doc(self, doctype, name, body):
        self.updated.append((doctype, name, dict(body)))
        row = self.docs.get(name, {"name": name})
        return {**row, **body}


def make_adapter(client: FakeClient) -> ERPNextTicketAdapter:
    settings = SimpleNamespace(ERPNEXT_TICKETS_PROJECT_COMPANY="Jardal")
    return ERPNextTicketAdapter(settings, client=client)  # type: ignore[arg-type]


class HierarchyPayloadTests(unittest.TestCase):
    def test_build_payload_incluye_jerarquia(self):
        payload = run(
            make_adapter(FakeClient()).build_payload(
                ref="api:x",
                title="hijo",
                parent_task="TASK-2026-00110",
                depends_on=["TASK-2026-00111"],
            )
        )
        self.assertEqual(payload["parent_task"], "TASK-2026-00110")
        self.assertEqual(payload["depends_on"], [{"task": "TASK-2026-00111"}])
        self.assertNotIn("is_group", payload)

    def test_build_payload_omite_jerarquia_ausente(self):
        payload = run(make_adapter(FakeClient()).build_payload(ref="api:x", title="suelto"))
        self.assertNotIn("parent_task", payload)
        self.assertNotIn("depends_on", payload)
        self.assertNotIn("is_group", payload)

    def test_create_group_task_marca_grupo(self):
        client = FakeClient()
        run(make_adapter(client).create_group_task(ref="api:g", title="grupo"))
        _doctype, body = client.created[0]
        self.assertEqual(body["is_group"], 1)


class HierarchyOpsTests(unittest.TestCase):
    def _client_with(self, *rows: dict) -> FakeClient:
        client = FakeClient()
        for row in rows:
            client.docs[row["name"]] = dict(row)
        return client

    def test_convert_to_group_hace_put(self):
        client = self._client_with({"name": "TASK-1", "is_group": 0})
        result = run(make_adapter(client).convert_to_group("TASK-1"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(client.updated, [("Task", "TASK-1", {"is_group": 1})])

    def test_convert_to_group_idempotente(self):
        client = self._client_with({"name": "TASK-1", "is_group": 1})
        result = run(make_adapter(client).convert_to_group("TASK-1"))
        self.assertTrue(result["duplicate"])
        self.assertEqual(client.updated, [])

    def test_link_child_enlaza_por_nombres(self):
        client = self._client_with(
            {"name": "TASK-1", "parent_task": None},
            {"name": "TASK-9", "is_group": 1},
        )
        result = run(make_adapter(client).link_child("TASK-1", "TASK-9"))
        self.assertTrue(result["ok"])
        self.assertEqual(client.updated, [("Task", "TASK-1", {"parent_task": "TASK-9"})])

    def test_link_child_resuelve_refs(self):
        client = self._client_with(
            {"name": "TASK-1", "parent_task": None, "openceo_ref": "api:hijo"},
            {"name": "TASK-9", "is_group": 1, "openceo_ref": "api:padre"},
        )
        result = run(make_adapter(client).link_child("api:hijo", "api:padre"))
        self.assertTrue(result["ok"])
        self.assertEqual(client.updated, [("Task", "TASK-1", {"parent_task": "TASK-9"})])

    def test_link_child_idempotente(self):
        client = self._client_with(
            {"name": "TASK-1", "parent_task": "TASK-9"},
            {"name": "TASK-9", "is_group": 1},
        )
        result = run(make_adapter(client).link_child("TASK-1", "TASK-9"))
        self.assertTrue(result["duplicate"])
        self.assertEqual(client.updated, [])

    def test_link_child_rechaza_auto_hijo(self):
        client = self._client_with({"name": "TASK-1", "parent_task": None})
        with self.assertRaises(ERPNextValidationError):
            run(make_adapter(client).link_child("TASK-1", "TASK-1"))

    def test_set_dependencies_reemplaza_la_tabla(self):
        client = self._client_with({"name": "TASK-1"})
        run(make_adapter(client).set_dependencies("TASK-1", ["TASK-2", "TASK-3"]))
        self.assertEqual(
            client.updated,
            [("Task", "TASK-1", {"depends_on": [{"task": "TASK-2"}, {"task": "TASK-3"}]})],
        )

    def test_set_dependencies_vacias_limpian(self):
        client = self._client_with({"name": "TASK-1"})
        run(make_adapter(client).set_dependencies("TASK-1", []))
        self.assertEqual(client.updated, [("Task", "TASK-1", {"depends_on": []})])


class TicketViewTests(unittest.TestCase):
    def test_vista_expone_jerarquia(self):
        adapter = make_adapter(FakeClient())
        view = adapter._ticket_view({
            "name": "TASK-1",
            "is_group": 1,
            "parent_task": None,
            "depends_on": [{"task": "TASK-0"}],
        })
        self.assertTrue(view["is_group"])
        self.assertIsNone(view["parent_task"])
        self.assertEqual(view["depends_on"], ["TASK-0"])

    def test_vista_omite_depends_on_que_no_se_sabe(self):
        adapter = make_adapter(FakeClient())
        view = adapter._ticket_view({"name": "TASK-1", "is_group": 0, "parent_task": "TASK-9"})
        self.assertFalse(view["is_group"])
        self.assertEqual(view["parent_task"], "TASK-9")
        self.assertNotIn("depends_on", view)


class FakeBusinessAdapter:
    """Adapter falso: registra la creacion y permite forzar el push."""

    def __init__(self) -> None:
        self.is_enabled = True
        self.calls: list[dict] = []
        self.push_error: Exception | None = None

        class _Client:
            @staticmethod
            def config_error() -> str | None:
                return None

        self.client = _Client()

    async def create_ticket(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True, "task": {"name": "TASK-2026-00042", "status": "Open"}, "ref": kwargs.get("ref")}

    async def update_status(self, name, status, *, task=None):
        if self.push_error is not None:
            raise self.push_error
        return {"name": name, "status": status}


class FakeRepo:
    def __init__(self, tasks: dict | None = None) -> None:
        self.tasks: dict[str, dict] = tasks or {}
        self.patches: list[tuple[str, dict]] = []

    async def get_task(self, task_id):
        row = self.tasks.get(task_id)
        return dict(row) if row is not None else None

    async def patch_task(self, task_id, updates):
        self.patches.append((task_id, dict(updates)))
        row = self.tasks.get(task_id, {"task_id": task_id})
        row = {**row, **updates}
        self.tasks[task_id] = row
        return dict(row)


def make_service(*, adapter=None, repo=None):
    from task_service import TaskService

    service = TaskService.__new__(TaskService)
    service._settings = SimpleNamespace(TASK_AUTORUN=False, HORIZON_PERSONAL_MIRROR_ENABLED=False)
    service._erpnext = adapter
    service._repo = repo
    return service


def business_task(task_id: str, business_name: str | None) -> dict:
    params: dict = {}
    if business_name:
        params = {"_mirrors": {"erpnext": {"name": business_name, "ref": f"api:{task_id}"}}}
    return {"task_id": task_id, "status": "queued", "params": params}


class BlockedByMappingTests(unittest.TestCase):
    def test_task_ids_se_resuelven_a_nombres_de_negocio(self):
        repo = FakeRepo({
            "b1": business_task("b1", "TASK-2026-00111"),
            "b2": business_task("b2", "TASK-2026-00112"),
        })
        names = run(make_service(repo=repo)._blocked_by_business_names(["b1", "b2"]))
        self.assertEqual(names, ["TASK-2026-00111", "TASK-2026-00112"])

    def test_nombres_de_negocio_pasan_tal_cual(self):
        names = run(make_service(repo=FakeRepo())._blocked_by_business_names(["TASK-2026-00111"]))
        self.assertEqual(names, ["TASK-2026-00111"])

    def test_bloqueador_sin_espejo_se_omite(self):
        repo = FakeRepo({"b1": business_task("b1", None), "perdido": None})
        repo.tasks.pop("perdido")
        names = run(make_service(repo=repo)._blocked_by_business_names(["b1", "fantasma"]))
        self.assertEqual(names, [])

    def test_sin_repo_solo_pasan_los_nombres(self):
        names = run(make_service()._blocked_by_business_names(["b1", "TASK-9"]))
        self.assertEqual(names, ["TASK-9"])


class BusinessRegistrationHierarchyTests(unittest.TestCase):
    def _service(self, adapter, repo):
        return make_service(adapter=adapter, repo=repo)

    def _doc(self, **kw):
        return make_task_document(TaskCreateRequest(source=TaskSource.API, title="t", description="d", **kw))

    def test_padre_y_deps_viajan_al_adapter(self):
        adapter = FakeBusinessAdapter()
        service = self._service(adapter, FakeRepo())
        mirror, pending = run(
            service._register_business_ticket(
                self._doc(project="p"),
                parent_task="TASK-2026-00110",
                depends_on=["TASK-2026-00111"],
            )
        )
        self.assertFalse(pending)
        self.assertEqual(adapter.calls[0]["parent_task"], "TASK-2026-00110")
        self.assertEqual(adapter.calls[0]["depends_on"], ["TASK-2026-00111"])
        self.assertEqual(mirror["parent_task"], "TASK-2026-00110")
        self.assertEqual(mirror["depends_on"], ["TASK-2026-00111"])

    def test_blocked_by_se_une_al_depends_on_explicito(self):
        adapter = FakeBusinessAdapter()
        repo = FakeRepo({"b1": business_task("b1", "TASK-2026-00111")})
        service = self._service(adapter, repo)
        mirror, _ = run(
            service._register_business_ticket(
                self._doc(project="p", blocked_by=["b1"]),
                depends_on=["TASK-2026-00114", "TASK-2026-00111"],
            )
        )
        self.assertEqual(adapter.calls[0]["depends_on"], ["TASK-2026-00114", "TASK-2026-00111"])
        self.assertEqual(mirror["depends_on"], ["TASK-2026-00114", "TASK-2026-00111"])

    def test_sin_jerarquia_no_se_manda_nada(self):
        adapter = FakeBusinessAdapter()
        service = self._service(adapter, FakeRepo())
        mirror, _ = run(service._register_business_ticket(self._doc(project="p")))
        self.assertIsNone(adapter.calls[0]["parent_task"])
        self.assertIsNone(adapter.calls[0]["depends_on"])
        self.assertNotIn("parent_task", mirror)
        self.assertNotIn("depends_on", mirror)


class PushBlockedTests(unittest.TestCase):
    def test_bloqueo_por_dependencias_deja_pending(self):
        adapter = FakeBusinessAdapter()
        adapter.push_error = ERPNextDependencyBlockedError("dependientes abiertos")
        task = business_task("t1", "TASK-1")
        repo = FakeRepo({"t1": task})
        service = make_service(adapter=adapter, repo=repo)
        run(service._push_status({"task_id": "t1", "name": "TASK-1", "status": "completed"}))
        _task_id, updates = repo.patches[-1]
        mirror = updates["params"]["_mirrors"]["erpnext"]
        self.assertEqual(mirror["pending_status"], "completed")
        self.assertNotIn("status_pushed", mirror)

    def test_otro_rechazo_no_deja_pending(self):
        adapter = FakeBusinessAdapter()
        adapter.push_error = ERPNextValidationError("datos mal")
        repo = FakeRepo({"t1": business_task("t1", "TASK-1")})
        service = make_service(adapter=adapter, repo=repo)
        run(service._push_status({"task_id": "t1", "name": "TASK-1", "status": "in_progress"}))
        self.assertEqual(repo.patches, [])


class WebhookHierarchyEchoTests(unittest.TestCase):
    def test_cambio_de_jerarquia_con_estado_no_terminal_se_ignora(self):
        repo = FakeRepo({"t1": business_task("t1", "TASK-1")})
        service = make_service(adapter=object(), repo=repo)
        result = run(
            service.apply_business_status(task_id="t1", business_status="Working", deleted=False)
        )
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(repo.patches, [], "un eco no debe escribir nada")

    def test_completed_que_ya_esta_aplicado_no_rebota(self):
        task = business_task("t1", "TASK-1")
        task["status"] = "completed"
        repo = FakeRepo({"t1": task})
        service = make_service(adapter=object(), repo=repo)
        result = run(
            service.apply_business_status(task_id="t1", business_status="Completed", deleted=False)
        )
        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(repo.patches, [])


class FakeMCPServer:
    def __init__(self) -> None:
        self.tools: dict[str, tuple] = {}

    def tool(self, fn, *, name, description):  # noqa: ANN001 - firma de FastMCP
        self.tools[name] = (fn, description)


class FakeTaskService:
    def __init__(self) -> None:
        self.requests: list = []

    async def create_task(self, req):
        self.requests.append(req)
        return {"task_id": f"t{len(self.requests)}"}

    async def get_task(self, task_id):
        return None


class McpHierarchyTests(unittest.TestCase):
    def _tools(self, task_service):
        server = FakeMCPServer()
        settings = SimpleNamespace()
        mcp_server._register_openceo_tools(server, settings, task_service, None)
        return {name: fn for name, (fn, _desc) in server.tools.items()}

    def test_create_ticket_acepta_jerarquia(self):
        task_service = FakeTaskService()
        tools = self._tools(task_service)
        result = run(
            tools["openceo_create_ticket"](
                title="hijo", parent_task="TASK-2026-00110", depends_on=["TASK-2026-00111"]
            )
        )
        self.assertTrue(result["ok"])
        req = task_service.requests[0]
        self.assertEqual(req.parent_task, "TASK-2026-00110")
        self.assertEqual(req.depends_on, ["TASK-2026-00111"])

    def test_batch_acepta_jerarquia_por_item(self):
        import json

        task_service = FakeTaskService()
        tools = self._tools(task_service)
        result = run(
            tools["openceo_create_tickets_batch"](
                tickets=json.dumps([
                    {"title": "a", "parent_task": "TASK-9"},
                    {"title": "b", "depends_on": ["TASK-1", "TASK-2"]},
                    {"title": "c"},
                ])
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(task_service.requests[0].parent_task, "TASK-9")
        self.assertIsNone(task_service.requests[0].depends_on)
        self.assertEqual(task_service.requests[1].depends_on, ["TASK-1", "TASK-2"])
        self.assertIsNone(task_service.requests[2].parent_task)

    def test_pm_batch_acepta_jerarquia_por_item(self):
        import json

        task_service = FakeTaskService()
        tools = self._tools(task_service)
        run(
            tools["openceo_create_pm_tickets_batch"](
                project="p",
                source_id="origen",
                tickets=json.dumps([{"title": "h", "parent_task": "TASK-9"}]),
            )
        )
        self.assertEqual(task_service.requests[0].parent_task, "TASK-9")
