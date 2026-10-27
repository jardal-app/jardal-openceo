"""Hilo del ticket: edicion, comentarios y adjuntos por el MCP (TASK-2026-00079).

Lo que se prueba aqui son las decisiones que costaron verificacion contra el sitio vivo
(Frappe 16.35.0 / Jardal ERP 16.36.0, 2026-10-01) y las que no se pueden deducir del codigo:

- `Comment` **no** es legible ni escribible por rol (`Projects User` + `Accounts User`):
  Frappe lo reserva a `System Manager`/`Website Manager`. Por eso se usan los metodos de
  la linea de tiempo (`add_comment`, `update_comment`, `get_docinfo`, `remove_attach`),
  que comprueban permiso sobre el **ticket**, en vez de abrir escritura generica.
- **Leer un adjunto** no se puede por `/api/resource/File`: la lista de `File` esta
  acotada al propietario. `get_docinfo` devuelve lo que muestra la linea de tiempo.
- `upload_file` **crea un `File` nuevo en cada llamada** aunque el contenido sea identico
  (el `content_hash` reutiliza el archivo en disco, no el documento), asi que la
  idempotencia tiene que ser nuestra: `md5` del contenido + `attached_to`.
- El estado del ticket no es un campo de edicion: eso lo mueve la ejecucion y lo
  sostiene `status_pushed` en el observador.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import mcp_server  # noqa: E402
from tools.jardal_erp_client import (  # noqa: E402
    JardalERPDuplicateError,
    JardalERPPermissionError,
    JardalERPUnavailable,
    JardalERPValidationError,
)
from tools.jardal_erp_tickets import (  # noqa: E402
    INLINE_CONTENT_MAX_BYTES,
    AttachmentRejected,
    JardalERPTicketAdapter,
    assert_attachment_name_allowed,
    comment_plain_text,
    content_md5,
    resolve_attachment_path,
    sanitize_file_name,
)

API_USER = "openceo@jardal.app"


def run(coro):
    return asyncio.run(coro)


class RecordingClient:
    """Cliente falso: registra llamadas y sirve respuestas canjeadas por el test."""

    def __init__(self) -> None:
        self.is_configured = True
        self.calls: list[tuple] = []
        self.docs: dict[str, dict] = {}
        self.list_docs_result: dict[str, list] = {}
        self.methods: dict[str, dict] = {}
        self.upload_result: dict = {"name": "FILE-1", "file_url": "/private/files/x.txt", "file_size": 3}
        self.download_result = b"contenido\n"
        self.errors: dict[str, Exception] = {}
        self.updated: list[tuple[str, str, dict]] = []

    def config_error(self) -> str | None:
        return None

    async def get_doc(self, doctype, name):
        self.calls.append(("get_doc", doctype, name))
        if "get_doc" in self.errors:
            raise self.errors["get_doc"]
        row = self.docs.get(name)
        if row is None:
            raise JardalERPValidationError(f"No existe {doctype} {name}", status_code=404)
        return row

    async def list_docs(self, doctype, *, filters=None, fields=None, limit=20):
        self.calls.append(("list_docs", doctype, filters))
        return self.list_docs_result.get(doctype, [])

    async def update_doc(self, doctype, name, body):
        self.calls.append(("update_doc", doctype, name, body))
        self.updated.append((doctype, name, body))
        if "update_doc" in self.errors:
            raise self.errors["update_doc"]
        return {**self.docs.get(name, {}), "name": name, **body}

    async def call_method(self, method, *, http_method="POST", params=None, json=None, attempts=3):
        self.calls.append(("call_method", method, http_method, params, json, attempts))
        if method in self.errors:
            raise self.errors[method]
        return self.methods.get(method, {})

    async def upload_file(self, *, filename, content, doctype, docname, is_private, folder="Home/Attachments"):
        self.calls.append(("upload_file", filename, content, doctype, docname, is_private))
        if "upload_file" in self.errors:
            raise self.errors["upload_file"]
        return {**self.upload_result, "file_name": filename, "is_private": 1 if is_private else 0}

    async def download_bytes(self, path, *, max_bytes):
        self.calls.append(("download_bytes", path, max_bytes))
        if "download_bytes" in self.errors:
            raise self.errors["download_bytes"]
        return self.download_result

    async def delete_doc(self, doctype, name):
        self.calls.append(("delete_doc", doctype, name))
        if "delete_doc" in self.errors:
            raise self.errors["delete_doc"]

    def called(self, name: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == name]

    def method_calls(self, method: str) -> list[tuple]:
        """Llamadas a un metodo whitelisted concreto (`call_method` las registra todas)."""
        return [call for call in self.calls if call[0] == "call_method" and call[1] == method]


def make_adapter(client: RecordingClient, *, root: str, max_bytes: int = 1024) -> JardalERPTicketAdapter:
    settings = SimpleNamespace(
        ERPNEXT_TICKETS_PROJECT_COMPANY="Jardal",
        ERPNEXT_URL="https://erp.test",
        ERPNEXT_USER=API_USER,
        ERPNEXT_ATTACHMENT_ALLOWED_ROOTS=root,
        ERPNEXT_ATTACHMENT_MAX_BYTES=max_bytes,
    )
    return JardalERPTicketAdapter(settings, client=client)  # type: ignore[arg-type]


class TempRootCase(unittest.TestCase):
    """Caso con una raiz autorizada real de la que se puede leer un archivo."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.client = RecordingClient()
        self.adapter = make_adapter(self.client, root=str(self.root))
        self.ticket = {
            "name": "TASK-2026-00079",
            "subject": "MCP comentarios y adjuntos",
            "status": "Open",
            "priority": "Medium",
            "project": "PROJ-0001",
            "openceo_ref": "api:abc",
        }
        self.client.docs["TASK-2026-00079"] = dict(self.ticket)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: bytes = b"contenido\n") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


# ---------------------------------------------------------------------------
# Saneado y limites del adjunto
# ---------------------------------------------------------------------------


class FileNameTests(unittest.TestCase):
    def test_traversal_no_sobrevive(self):
        # Solo queda el ultimo segmento: no hay `..` que valga.
        self.assertEqual(sanitize_file_name("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_file_name("..\\..\\windows\\system32\\cmd.txt"), "cmd.txt")
        self.assertEqual(sanitize_file_name("/etc/shadow"), "shadow")

    def test_ocultos_control_y_separadores_fuera(self):
        self.assertEqual(sanitize_file_name(".bashrc"), "bashrc")
        self.assertEqual(sanitize_file_name("informe\x00.txt"), "informe")
        self.assertEqual(sanitize_file_name("a<b>c:d|e?f*.txt"), "abcdef.txt")
        self.assertEqual(sanitize_file_name("  dos   espacios.txt "), "dos espacios.txt")

    def test_vacio_cae_en_un_nombre_util(self):
        self.assertEqual(sanitize_file_name(""), "adjunto")
        self.assertEqual(sanitize_file_name("   "), "adjunto")
        self.assertEqual(sanitize_file_name(None), "adjunto")

    def test_nombre_largo_conserva_extension(self):
        name = sanitize_file_name("x" * 300 + ".pdf")
        self.assertLessEqual(len(name), 120)
        self.assertTrue(name.endswith(".pdf"))

    def test_extensiones_peligrosas_rechazadas(self):
        for name in ("pago.html", "grafico.svg", "run.sh", "app.exe", "nota.JS"):
            with self.assertRaises(AttachmentRejected) as ctx:
                assert_attachment_name_allowed(name)
            self.assertEqual(ctx.exception.status, "unsupported_media")

    def test_extensiones_normales_pasan(self):
        for name in ("informe.pdf", "datos.csv", "notas.md", "captura.png", "sin_extension"):
            assert_attachment_name_allowed(name)


class PathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.outside = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_relativa_rechazada(self):
        with self.assertRaises(AttachmentRejected) as ctx:
            resolve_attachment_path("informe.txt", [str(self.root)])
        self.assertEqual(ctx.exception.status, "invalid_input")

    def test_inexistente_rechazada(self):
        with self.assertRaises(AttachmentRejected) as ctx:
            resolve_attachment_path(str(self.root / "no-existe.txt"), [str(self.root)])
        self.assertEqual(ctx.exception.status, "not_found")

    def test_directorio_rechazado(self):
        with self.assertRaises(AttachmentRejected) as ctx:
            resolve_attachment_path(str(self.root), [str(self.root)])
        self.assertEqual(ctx.exception.status, "invalid_input")

    def test_fuera_del_alcance_rechazada(self):
        fuera = self.outside / "secreto.txt"
        fuera.write_text("no deberia subirse")
        with self.assertRaises(AttachmentRejected) as ctx:
            resolve_attachment_path(str(fuera), [str(self.root)])
        self.assertEqual(ctx.exception.status, "forbidden")

    def test_traversal_rechazado(self):
        (self.root / "sub").mkdir()
        (self.outside / "secreto.txt").write_text("x")
        with self.assertRaises(AttachmentRejected) as ctx:
            resolve_attachment_path(str(self.root / "sub" / ".." / ".." / self.outside.name / "secreto.txt"), [str(self.root)])
        self.assertEqual(ctx.exception.status, "forbidden")

    def test_enlace_simbolico_que_escapa_rechazado(self):
        """El enlace se resuelve antes de comparar: sin esto, la raiz no confinaria nada."""
        (self.outside / "secreto.txt").write_text("x")
        link = self.root / "atajo.txt"
        link.symlink_to(self.outside / "secreto.txt")
        with self.assertRaises(AttachmentRejected) as ctx:
            resolve_attachment_path(str(link), [str(self.root)])
        self.assertEqual(ctx.exception.status, "forbidden")

    def test_dentro_del_alcance_pasa(self):
        inside = self.root / "sub" / "informe.txt"
        inside.parent.mkdir(parents=True)
        inside.write_text("ok")
        self.assertEqual(resolve_attachment_path(str(inside), [str(self.root)]), inside.resolve())


class HashAndTextTests(unittest.TestCase):
    def test_md5_es_el_de_frappe(self):
        # Frappe: `hashlib.md5(content, usedforsecurity=False).hexdigest()`. Tiene que
        # coincidir byte a byte o la deduplicacion no veria el adjunto existente.
        content = b"informe de pruebas\n"
        self.assertEqual(content_md5(content), hashlib.md5(content, usedforsecurity=False).hexdigest())

    def test_texto_comparable_ignora_el_render(self):
        # La escritura guarda el HTML tal cual y la lectura lo devuelve convertido de
        # markdown a HTML: la comparacion tiene que caer en el texto.
        self.assertEqual(comment_plain_text("Revisado, tests verdes."), "Revisado, tests verdes.")
        self.assertEqual(comment_plain_text("<p>Revisado, tests verdes.</p>\n"), "Revisado, tests verdes.")
        self.assertEqual(comment_plain_text("<p>a</p><p>b</p>"), comment_plain_text("a b"))
        self.assertEqual(comment_plain_text("<p>R&amp;D</p>"), "R&D")
        self.assertEqual(comment_plain_text("<script>alert(1)</script>hola"), "hola")


# ---------------------------------------------------------------------------
# Ticket: resolucion y edicion
# ---------------------------------------------------------------------------


class ResolveTests(TempRootCase):
    def test_resuelve_por_nombre(self):
        row = run(self.adapter.resolve_task("TASK-2026-00079"))
        self.assertEqual(row["name"], "TASK-2026-00079")
        self.assertEqual(self.client.called("get_doc")[0][1:], ("Task", "TASK-2026-00079"))

    def test_resuelve_por_openceo_ref(self):
        self.client.list_docs_result["Task"] = [dict(self.ticket)]
        row = run(self.adapter.resolve_task("api:abc"))
        self.assertEqual(row["name"], "TASK-2026-00079")
        self.assertEqual(self.client.called("list_docs")[0][1], "Task")

    def test_ticket_inexistente_es_not_found(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.resolve_task("TASK-2026-09999"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.client.called("update_doc"), [])

    def test_ref_inexistente_es_not_found(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.resolve_task("api:no-existe"))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_sin_identificador_es_fallo_de_datos(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.resolve_task("  "))
        self.assertEqual(ctx.exception.status_code, 400)


class UpdateTicketTests(TempRootCase):
    def test_edita_los_campos_pedidos(self):
        result = run(
            self.adapter.update_ticket(
                "TASK-2026-00079",
                subject="Titulo corregido",
                description="linea 1\nlinea 2",
                priority="critical",
                due_date="2026-10-15",
            )
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            self.client.updated[0],
            (
                "Task",
                "TASK-2026-00079",
                {
                    "subject": "Titulo corregido",
                    "description": "linea 1<br>linea 2",
                    "priority": "Urgent",
                    "exp_end_date": "2026-10-15 00:00:00",
                },
            ),
        )

    def test_respuesta_acotada_a_la_vista_corta(self):
        """El documento entero de `Task` no viaja al contexto del agente.

        Cambio de contrato deliberado (TASK-2026-00013): la vista suma `is_group`
        y `parent_task` para que las operaciones de jerarquia (convertir, enlazar)
        sean verificables en su propia respuesta, sin un GET extra. Sigue siendo
        una vista corta y cerrada: ocho escalares como maximo, nunca el documento.
        """
        result = run(self.adapter.update_ticket("TASK-2026-00079", subject="Titulo"))
        self.assertEqual(
            set(result["ticket"]),
            {"name", "subject", "status", "priority", "project", "openceo_ref",
             "is_group", "parent_task"},
        )
        self.assertEqual(result["changed"], ["subject"])
        self.assertEqual(result["ticket"]["status"], "Open")

    def test_nunca_empuja_estado_ni_dependencias(self):
        """El estado es una maquina de negocio: editarlo aqui saltaria `status_pushed`."""
        run(self.adapter.update_ticket("TASK-2026-00079", subject="Solo titulo"))
        body = self.client.updated[0][2]
        for field in ("status", "depends_on", "openceo_ref", "openceo_task_id", "project"):
            self.assertNotIn(field, body)

    def test_titulo_se_trunca_al_limite_de_frappe(self):
        run(self.adapter.update_ticket("TASK-2026-00079", subject="t" * 300))
        self.assertEqual(len(self.client.updated[0][2]["subject"]), 140)

    def test_texto_plano_se_escapa_y_conserva_saltos(self):
        run(self.adapter.update_ticket("TASK-2026-00079", description="sin etiquetas & 5 > 3\nsegunda linea"))
        body = self.client.updated[0][2]["description"]
        self.assertIn("&amp;", body)
        self.assertIn("<br>", body)

    def test_descripcion_ya_en_html_no_se_mutila(self):
        """Leer el HTML del ticket y volverlo a escribir no puede mostrar las etiquetas.

        El campo es un Text Editor: si se escapara, la descripcion entera apareceria con
        `<br>` literales. Frappe sanea el HTML al guardar (verificado en vivo), asi que
        pasar el HTML es seguro.
        """
        existing = "Linea 1<br><b>Titulo</b><br>Linea 2 &amp; mas"
        run(self.adapter.update_ticket("TASK-2026-00079", description=existing))
        self.assertEqual(self.client.updated[0][2]["description"], existing)

    def test_texto_con_menor_que_no_es_etiqueta_se_escapa(self):
        run(self.adapter.update_ticket("TASK-2026-00079", description="si 3 < 5 y <3"))
        body = self.client.updated[0][2]["description"]
        self.assertIn("&lt; 5", body)
        self.assertIn("&lt;3", body)

    def test_prioridad_invalida_se_rechaza_en_vez_de_asumir_medium(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.update_ticket("TASK-2026-00079", priority="urgentisimo"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.client.updated, [])

    def test_titulo_vacio_se_rechaza(self):
        with self.assertRaises(JardalERPValidationError):
            run(self.adapter.update_ticket("TASK-2026-00079", subject="   "))

    def test_sin_campos_no_llama_a_business(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.update_ticket("TASK-2026-00079"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.client.updated, [])


# ---------------------------------------------------------------------------
# Comentarios
# ---------------------------------------------------------------------------


def docinfo(comments: list[dict] | None = None, attachments: list[dict] | None = None) -> dict:
    return {"docinfo": {"comments": comments or [], "attachments": attachments or []}}


class ListThreadTests(TempRootCase):
    def test_ordena_comentarios_del_mas_nuevo_y_cuenta_adjuntos(self):
        self.client.methods["frappe.desk.form.load.get_docinfo"] = docinfo(
            comments=[
                {"name": "c1", "creation": "2026-10-01 10:00:00", "content": "<p>viejo</p>", "owner": API_USER},
                {"name": "c2", "creation": "2026-10-01 11:00:00", "content": "<p>nuevo</p>", "owner": "otra@jardal.app"},
            ],
            attachments=[{"name": "F1", "file_name": "a.txt", "file_url": "/private/files/a.txt", "is_private": 1}],
        )
        result = run(self.adapter.list_thread("TASK-2026-00079"))
        self.assertEqual([c["id"] for c in result["comments"]], ["c2", "c1"])
        self.assertEqual(result["comment_count"], 2)
        self.assertEqual(result["attachment_count"], 1)
        self.assertTrue(result["attachments"][0]["private"])
        self.assertEqual(result["attachments"][0]["download_url"], "https://erp.test/private/files/a.txt")

    def test_marca_lo_editable(self):
        """Solo el autor puede editar: decirlo evita una llamada condenada."""
        self.client.methods["frappe.desk.form.load.get_docinfo"] = docinfo(
            comments=[
                {"name": "c1", "content": "x", "owner": API_USER},
                {"name": "c2", "content": "y", "owner": "persona@jardal.app"},
            ]
        )
        result = run(self.adapter.list_thread("TASK-2026-00079"))
        by_id = {row["id"]: row for row in result["comments"]}
        self.assertTrue(by_id["c1"]["editable"])
        self.assertFalse(by_id["c2"]["editable"])

    def test_limite_acotado(self):
        self.client.methods["frappe.desk.form.load.get_docinfo"] = docinfo(
            comments=[{"name": f"c{i}", "content": "x", "owner": API_USER, "creation": f"2026-10-01 10:00:{i:02d}"}
                      for i in range(10)]
        )
        result = run(self.adapter.list_thread("TASK-2026-00079", limit=3))
        self.assertEqual(len(result["comments"]), 3)
        self.assertEqual(result["comment_count"], 10)


class AddCommentTests(TempRootCase):
    def test_publica_por_el_metodo_oficial(self):
        self.client.methods["frappe.desk.form.utils.add_comment"] = {
            "message": {"name": "abc123", "content": "hola", "creation": "2026-10-01 12:00:00"}
        }
        result = run(self.adapter.add_comment("TASK-2026-00079", "  hola  "))
        self.assertTrue(result["ok"])
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["comment"]["id"], "abc123")

        _, method, http_method, _params, payload, attempts = self.client.method_calls(
            "frappe.desk.form.utils.add_comment"
        )[0]
        self.assertEqual(method, "frappe.desk.form.utils.add_comment")
        self.assertEqual(http_method, "POST")
        self.assertEqual(payload["reference_name"], "TASK-2026-00079")
        self.assertEqual(payload["comment_email"], API_USER)
        self.assertEqual(payload["comment_by"], "OpenCEO")
        # Un reintento automatico del cliente duplicaria el comentario.
        self.assertEqual(attempts, 1)

    def test_no_abre_escritura_generica_a_comment(self):
        run(self.adapter.add_comment("TASK-2026-00079", "hola"))
        self.assertEqual(self.client.called("create_doc"), [])
        self.assertEqual(self.client.called("update_doc"), [])

    def test_reintento_identico_no_duplica(self):
        self.client.methods["frappe.desk.form.load.get_docinfo"] = docinfo(
            comments=[{"name": "abc123", "content": "<p>hola</p>\n", "owner": API_USER,
                       "creation": "2026-10-01 12:00:00"}]
        )
        result = run(self.adapter.add_comment("TASK-2026-00079", "hola"))
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["comment"]["id"], "abc123")
        self.assertEqual(self.client.method_calls("frappe.desk.form.utils.add_comment"), [])

    def test_comentario_igual_de_otra_persona_no_deduplica(self):
        """La deduplicacion es del reintento propio, no de un texto que alguien ya escribio."""
        self.client.methods["frappe.desk.form.load.get_docinfo"] = docinfo(
            comments=[{"name": "ajeno", "content": "<p>hola</p>", "owner": "persona@jardal.app"}]
        )
        self.client.methods["frappe.desk.form.utils.add_comment"] = {"message": {"name": "nuevo", "content": "hola"}}
        result = run(self.adapter.add_comment("TASK-2026-00079", "hola"))
        self.assertFalse(result["duplicate"])
        self.assertEqual(len(self.client.method_calls("frappe.desk.form.utils.add_comment")), 1)

    def test_vacio_se_rechaza(self):
        with self.assertRaises(JardalERPValidationError):
            run(self.adapter.add_comment("TASK-2026-00079", "   "))
        self.assertEqual(self.client.method_calls("frappe.desk.form.utils.add_comment"), [])

    def test_permiso_denegado_llega_como_permiso(self):
        self.client.errors["frappe.desk.form.utils.add_comment"] = JardalERPPermissionError(
            "no permitido", status_code=403
        )
        with self.assertRaises(JardalERPPermissionError):
            run(self.adapter.add_comment("TASK-2026-00079", "hola"))


class UpdateCommentTests(TempRootCase):
    def test_edita_por_el_metodo_oficial(self):
        result = run(self.adapter.update_comment("abc123", "corregido"))
        self.assertTrue(result["ok"])
        _, method, _http, _params, payload, attempts = self.client.called("call_method")[0]
        self.assertEqual(method, "frappe.desk.form.utils.update_comment")
        self.assertEqual(payload, {"name": "abc123", "content": "corregido"})
        self.assertEqual(attempts, 1)

    def test_sin_id_o_sin_texto_se_rechaza(self):
        for args in ((("", "x")), (("abc", "  "))):
            with self.assertRaises(JardalERPValidationError):
                run(self.adapter.update_comment(*args))
        self.assertEqual(self.client.called("call_method"), [])

    def test_comentario_ajeno_se_edita_por_recurso(self):
        """El metodo oficial solo edita lo propio; el rol dedicado permite lo de cualquiera.

        Decision del dueño (2026-10-01): control completo del hilo. La via de reserva
        comprueba antes que el comentario cuelga de un ticket legible.
        """
        self.client.errors["frappe.desk.form.utils.update_comment"] = JardalERPPermissionError(
            "Comment can only be edited by the owner", status_code=417
        )
        self.client.docs["ajeno"] = {
            "name": "ajeno", "comment_type": "Comment", "owner": "persona@jardal.app",
            "reference_doctype": "Task", "reference_name": "TASK-2026-00079",
        }
        result = run(self.adapter.update_comment("ajeno", "corregido por el MCP"))
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.updated[-1], ("Comment", "ajeno", {"content": "corregido por el MCP"}))

    def test_comentario_de_otro_doctype_no_se_toca(self):
        """El rol abre `Comment` en todo el sitio: la frontera la pone el adapter."""
        self.client.errors["frappe.desk.form.utils.update_comment"] = JardalERPPermissionError(
            "solo el autor", status_code=417
        )
        self.client.docs["ajeno"] = {
            "name": "ajeno", "comment_type": "Comment",
            "reference_doctype": "Journal Entry", "reference_name": "ACC-JV-2026-00001",
        }
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.update_comment("ajeno", "no"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.client.updated, [])

    def test_sin_el_rol_tampoco_puede(self):
        """Si se revoca el permiso, el fallo es de permiso clasificado, no un 500."""
        self.client.errors["frappe.desk.form.utils.update_comment"] = JardalERPPermissionError(
            "solo el autor", status_code=417
        )
        self.client.docs["ajeno"] = {
            "name": "ajeno", "comment_type": "Comment",
            "reference_doctype": "Task", "reference_name": "TASK-2026-00079",
        }
        self.client.errors["update_doc"] = JardalERPPermissionError("sin permiso", status_code=403)
        with self.assertRaises(JardalERPPermissionError):
            run(self.adapter.update_comment("ajeno", "no"))


class DeleteCommentTests(TempRootCase):
    def _comment(self, **overrides) -> dict:
        return {
            "name": "c1", "comment_type": "Comment", "owner": "persona@jardal.app",
            "reference_doctype": "Task", "reference_name": "TASK-2026-00079", **overrides,
        }

    def test_borra_el_comentario_del_ticket(self):
        self.client.docs["c1"] = self._comment()
        result = run(self.adapter.delete_comment("c1", "TASK-2026-00079"))
        self.assertEqual(result["deleted"], "c1")
        self.assertEqual(self.client.called("delete_doc"), [("delete_doc", "Comment", "c1")])

    def test_ticket_distinto_se_rechaza_sin_borrar(self):
        """Un id equivocado no debe borrar el comentario de otro trabajo."""
        self.client.docs["c1"] = self._comment(reference_name="TASK-2026-00001")
        self.client.docs["TASK-2026-00001"] = {"name": "TASK-2026-00001", "status": "Open"}
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.delete_comment("c1", "TASK-2026-00079"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.client.called("delete_doc"), [])

    def test_registro_que_no_es_comentario_se_rechaza(self):
        self.client.docs["c1"] = self._comment(comment_type="Attachment")
        with self.assertRaises(JardalERPValidationError):
            run(self.adapter.delete_comment("c1", "TASK-2026-00079"))
        self.assertEqual(self.client.called("delete_doc"), [])

    def test_inexistente_es_not_found(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.delete_comment("no-existe", "TASK-2026-00079"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.client.called("delete_doc"), [])


# ---------------------------------------------------------------------------
# Adjuntos
# ---------------------------------------------------------------------------


class AttachFileTests(TempRootCase):
    def test_sube_con_nombre_saneado_y_privado(self):
        path = self.write("informe final.txt")
        result = run(self.adapter.attach_file("TASK-2026-00079", source_path=str(path)))
        self.assertFalse(result["duplicate"])
        _, filename, content, doctype, docname, is_private = self.client.called("upload_file")[0]
        self.assertEqual(filename, "informe final.txt")
        self.assertEqual(content, b"contenido\n")
        self.assertEqual((doctype, docname), ("Task", "TASK-2026-00079"))
        self.assertTrue(is_private)
        self.assertEqual(result["attachment"]["id"], "FILE-1")
        self.assertEqual(result["attachment"]["download_url"], "https://erp.test/private/files/x.txt")

    def test_deduplica_por_content_hash_sin_subir(self):
        """Un reintento tras un timeout ambiguo no puede crear un segundo adjunto."""
        path = self.write("informe.txt")
        digest = content_md5(b"contenido\n")
        self.client.list_docs_result["File"] = [
            {"name": "FILE-9", "file_name": "informe.txt", "file_url": "/private/files/informe.txt",
             "content_hash": digest, "is_private": 1, "file_size": 10}
        ]
        result = run(self.adapter.attach_file("TASK-2026-00079", source_path=str(path)))
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["attachment"]["id"], "FILE-9")
        self.assertEqual(self.client.called("upload_file"), [])
        self.assertEqual(self.client.called("list_docs")[0][2][-1], ["content_hash", "=", digest])

    def test_base64_con_nombre_explicito(self):
        payload = base64.b64encode(b"datos").decode("ascii")
        run(self.adapter.attach_file("TASK-2026-00079", content_base64=payload, file_name="../../ruta/datos.csv"))
        self.assertEqual(self.client.called("upload_file")[0][1], "datos.csv")

    def test_exige_exactamente_una_fuente(self):
        path = str(self.write("x.txt"))
        for kwargs in (
            {},
            {"source_path": path, "content_base64": base64.b64encode(b"x").decode()},
        ):
            with self.assertRaises(AttachmentRejected) as ctx:
                run(self.adapter.attach_file("TASK-2026-00079", **kwargs))
            self.assertEqual(ctx.exception.status, "invalid_input")
        self.assertEqual(self.client.called("upload_file"), [])

    def test_limite_de_tamano(self):
        path = self.write("grande.bin", b"x" * 2000)
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.attach_file("TASK-2026-00079", source_path=str(path)))
        self.assertEqual(ctx.exception.status, "too_large")
        self.assertEqual(self.client.called("upload_file"), [])

    def test_archivo_vacio_rechazado(self):
        path = self.write("vacio.txt", b"")
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.attach_file("TASK-2026-00079", source_path=str(path)))
        self.assertEqual(ctx.exception.status, "invalid_input")

    def test_ruta_fuera_del_alcance_rechazada(self):
        ajeno = Path(tempfile.mkdtemp()) / "ajeno.txt"
        ajeno.write_text("no")
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.attach_file("TASK-2026-00079", source_path=str(ajeno)))
        self.assertEqual(ctx.exception.status, "forbidden")
        self.assertEqual(self.client.called("upload_file"), [])

    def test_base64_invalido_rechazado(self):
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.attach_file("TASK-2026-00079", content_base64="no es base64!!"))
        self.assertEqual(ctx.exception.status, "invalid_input")

    def test_tipo_bloqueado_rechazado(self):
        path = self.write("panel.html", b"<script>alert(1)</script>")
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.attach_file("TASK-2026-00079", source_path=str(path)))
        self.assertEqual(ctx.exception.status, "unsupported_media")

    def test_ticket_inexistente_no_lee_el_archivo_ni_sube(self):
        path = self.write("informe.txt")
        with self.assertRaises(JardalERPValidationError):
            run(self.adapter.attach_file("TASK-2026-09999", source_path=str(path)))
        self.assertEqual(self.client.called("upload_file"), [])

    def test_publico_solo_si_se_pide(self):
        path = self.write("informe.txt")
        run(self.adapter.attach_file("TASK-2026-00079", source_path=str(path), private=False))
        self.assertFalse(self.client.called("upload_file")[0][5])


class UpdateAttachmentTests(TempRootCase):
    def _file(self, **overrides) -> dict:
        return {
            "name": "FILE-1",
            "file_name": "informe.txt",
            "file_url": "/private/files/informe.txt",
            "is_private": 1,
            "file_size": 10,
            "attached_to_doctype": "Task",
            "attached_to_name": "TASK-2026-00079",
            **overrides,
        }

    def test_renombra_y_cambia_privacidad(self):
        self.client.docs["FILE-1"] = self._file()
        result = run(
            self.adapter.update_attachment("FILE-1", "TASK-2026-00079", file_name="../../otro.txt", private=False)
        )
        doctype, name, body = self.client.updated[0]
        self.assertEqual((doctype, name), ("File", "FILE-1"))
        self.assertEqual(body, {"file_name": "otro.txt", "is_private": 0})
        self.assertFalse(result["attachment"]["private"])

    def test_adjunto_de_otro_ticket_se_rechaza(self):
        """Un id adivinado no debe alcanzar el archivo de otro ticket."""
        self.client.docs["FILE-1"] = self._file(attached_to_name="TASK-2026-00001")
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.update_attachment("FILE-1", "TASK-2026-00079", private=False))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(self.client.updated, [])

    def test_carpeta_no_es_adjunto(self):
        self.client.docs["FILE-1"] = self._file(is_folder=1)
        with self.assertRaises(JardalERPValidationError):
            run(self.adapter.update_attachment("FILE-1", "TASK-2026-00079", private=False))

    def test_sin_cambios_no_llama(self):
        self.client.docs["FILE-1"] = self._file()
        with self.assertRaises(JardalERPValidationError):
            run(self.adapter.update_attachment("FILE-1", "TASK-2026-00079"))
        self.assertEqual(self.client.updated, [])

    def test_tipo_bloqueado_en_el_renombrado(self):
        self.client.docs["FILE-1"] = self._file()
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.update_attachment("FILE-1", "TASK-2026-00079", file_name="pago.html"))
        self.assertEqual(ctx.exception.status, "unsupported_media")


class DeleteAttachmentTests(TempRootCase):
    def _file(self, **overrides) -> dict:
        return {
            "name": "FILE-1",
            "file_name": "informe.txt",
            "file_url": "/private/files/informe.txt",
            "attached_to_doctype": "Task",
            "attached_to_name": "TASK-2026-00079",
            **overrides,
        }

    def test_borra_con_el_metodo_oficial(self):
        self.client.docs["FILE-1"] = self._file()
        result = run(self.adapter.delete_attachment("FILE-1", "TASK-2026-00079"))
        self.assertEqual(result["deleted"], "FILE-1")
        _, method, _http, _params, payload, attempts = self.client.called("call_method")[0]
        self.assertEqual(method, "frappe.desk.form.utils.remove_attach")
        self.assertEqual(payload, {"fid": "FILE-1"})
        self.assertEqual(attempts, 1)

    def test_adjunto_de_otro_ticket_se_rechaza_sin_borrar(self):
        self.client.docs["FILE-1"] = self._file(attached_to_name="TASK-2026-00001")
        with self.assertRaises(JardalERPValidationError):
            run(self.adapter.delete_attachment("FILE-1", "TASK-2026-00079"))
        self.assertEqual(self.client.called("call_method"), [])

    def test_inexistente_es_not_found(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.delete_attachment("NO-EXISTE", "TASK-2026-00079"))
        self.assertEqual(ctx.exception.status_code, 404)


class GetAttachmentTests(TempRootCase):
    def _file(self, **overrides) -> dict:
        return {
            "name": "FILE-1",
            "file_name": "informe.txt",
            "file_url": "/private/files/informe.txt",
            "is_private": 1,
            "file_size": 10,
            "attached_to_doctype": "Task",
            "attached_to_name": "TASK-2026-00079",
            **overrides,
        }

    def test_metadatos_sin_contenido(self):
        self.client.docs["FILE-1"] = self._file()
        result = run(self.adapter.get_attachment("FILE-1"))
        self.assertEqual(result["attachment"]["download_url"], "https://erp.test/private/files/informe.txt")
        self.assertNotIn("content_text", result["attachment"])
        self.assertEqual(self.client.called("download_bytes"), [])

    def test_texto_cuando_se_pide(self):
        self.client.docs["FILE-1"] = self._file()
        result = run(self.adapter.get_attachment("FILE-1", include_content=True))
        self.assertEqual(result["attachment"]["content_text"], "contenido\n")
        self.assertEqual(self.client.called("download_bytes")[0][2], INLINE_CONTENT_MAX_BYTES)

    def test_binario_vuelve_en_base64(self):
        self.client.docs["FILE-1"] = self._file()
        self.client.download_result = b"\xff\xfe\x00\x01"
        result = run(self.adapter.get_attachment("FILE-1", include_content=True))
        self.assertEqual(base64.b64decode(result["attachment"]["content_base64"]), b"\xff\xfe\x00\x01")

    def test_grande_no_se_incrusta(self):
        self.client.docs["FILE-1"] = self._file(file_size=INLINE_CONTENT_MAX_BYTES + 1)
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.get_attachment("FILE-1", include_content=True))
        self.assertEqual(ctx.exception.status, "too_large")
        self.assertEqual(self.client.called("download_bytes"), [])

    def test_url_remota_no_se_sigue(self):
        """Sin esto habria SSRF: el campo `file_url` es texto de Jardal ERP."""
        self.client.docs["FILE-1"] = self._file(file_url="http://169.254.169.254/latest/meta-data/")
        with self.assertRaises(AttachmentRejected) as ctx:
            run(self.adapter.get_attachment("FILE-1", include_content=True))
        self.assertEqual(ctx.exception.status, "invalid_input")
        self.assertEqual(self.client.called("download_bytes"), [])

    def test_inexistente_es_not_found(self):
        with self.assertRaises(JardalERPValidationError) as ctx:
            run(self.adapter.get_attachment("NO-EXISTE"))
        self.assertEqual(ctx.exception.status_code, 404)


# ---------------------------------------------------------------------------
# Superficie MCP: nombres, descripciones cortas y clasificacion de errores
# ---------------------------------------------------------------------------


class FakeMCPServer:
    """Servidor FastMCP minimo: guarda las funciones registradas por nombre."""

    def __init__(self) -> None:
        self.tools: dict[str, tuple] = {}

    def tool(self, fn, *, name, description):  # noqa: ANN001 - firma de FastMCP
        self.tools[name] = (fn, description)


class FakeAdapter:
    """Adapter con cualquier metodo: registra la llamada o lanza el error canjeado."""

    is_enabled = True

    def __init__(self, *, result: dict | None = None, error: Exception | None = None,
                 results: dict | None = None) -> None:
        self.result = result if result is not None else {"ok": True}
        self.error = error
        self.results = results or {}
        self.calls: list[tuple] = []

    def __getattr__(self, item):
        async def call(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            if self.error is not None:
                raise self.error
            return self.results.get(item, self.result)

        return call


NEW_TOOLS = (
    "openceo_update_ticket",
    "openceo_add_ticket_comment",
    "openceo_update_ticket_comment",
    "openceo_delete_ticket_comment",
    "openceo_attach_ticket_file",
    "openceo_get_ticket_attachment",
    "openceo_update_ticket_attachment",
    "openceo_delete_ticket_attachment",
)


class McpSurfaceTests(unittest.TestCase):
    def _register(self, adapter):
        server = FakeMCPServer()
        settings = SimpleNamespace(ERPNEXT_ATTACHMENT_ALLOWED_ROOTS="/tmp", ERPNEXT_ATTACHMENT_MAX_BYTES=1024)
        names = mcp_server._register_openceo_tools(server, settings, None, adapter)
        return server, names

    def test_las_nuevas_herramientas_se_anuncian(self):
        server, names = self._register(FakeAdapter())
        for name in NEW_TOOLS:
            self.assertIn(name, names)
            self.assertIn(name, server.tools)

    def test_no_queda_un_tool_separado_para_el_hilo(self):
        """Un tool de lectura de menos: el hilo viene dentro de `openceo_get_ticket`."""
        server, names = self._register(FakeAdapter())
        self.assertNotIn("openceo_get_ticket_thread", names)
        self.assertNotIn("openceo_get_ticket_thread", server.tools)

    def test_get_ticket_devuelve_todo_el_contexto_en_una_lectura(self):
        adapter = FakeAdapter(results={
            "get_ticket": {"name": "TASK-1", "subject": "s", "status": "Open"},
            "thread_of": {"comment_count": 2, "attachment_count": 1,
                          "comments": [{"id": "c1"}, {"id": "c2"}],
                          "attachments": [{"id": "f1", "download_url": "https://erp.test/private/files/f1"}]},
        })
        server, _ = self._register(adapter)
        result = run(server.tools["openceo_get_ticket"][0](identifier="TASK-1"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["ticket"]["name"], "TASK-1")
        self.assertEqual(result["comment_count"], 2)
        self.assertEqual([c["id"] for c in result["comments"]], ["c1", "c2"])
        self.assertEqual(result["attachments"][0]["download_url"], "https://erp.test/private/files/f1")
        self.assertEqual([call[0] for call in adapter.calls], ["get_ticket", "thread_of"])

    def test_get_ticket_de_algo_inexistente_es_not_found(self):
        adapter = FakeAdapter(results={"get_ticket": None})
        server, _ = self._register(adapter)
        result = run(server.tools["openceo_get_ticket"][0](identifier="TASK-9"))
        self.assertEqual(result["status"], "not_found")
        self.assertEqual([call[0] for call in adapter.calls], ["get_ticket"])

    def test_descripciones_cortas(self):
        """Cada palabra de una descripcion viaja en cada peticion de la sesion de codigo."""
        server, _ = self._register(FakeAdapter())
        for name in NEW_TOOLS:
            description = server.tools[name][1]
            self.assertLessEqual(len(description), 220, f"{name} es demasiado larga")
            self.assertLessEqual(len(description.split()), 32, f"{name} usa demasiadas palabras")

    def test_sin_business_devuelve_not_configured(self):
        server, _ = self._register(None)
        result = run(server.tools["openceo_add_ticket_comment"][0](ticket="TASK-1", content="x"))
        self.assertEqual(result, {"ok": False, "status": "not_configured", "message": "Jardal ERP no esta configurado."})

    def test_clasifica_los_fallos(self):
        cases = (
            (AttachmentRejected("pesa mucho", status="too_large"), "too_large"),
            (AttachmentRejected("fuera", status="forbidden"), "forbidden"),
            (JardalERPPermissionError("sin permiso", status_code=403), "forbidden"),
            (JardalERPValidationError("no existe", status_code=404), "not_found"),
            (JardalERPValidationError("datos", status_code=400), "invalid_input"),
            (JardalERPValidationError("grande", status_code=413), "too_large"),
            (JardalERPValidationError("datos raros"), "invalid_input"),
            (JardalERPUnavailable("caido"), "unavailable"),
            (JardalERPDuplicateError("ya existe"), "duplicate"),
            (RuntimeError("inesperado"), "error"),
        )
        for error, expected in cases:
            server, _ = self._register(FakeAdapter(error=error))
            result = run(server.tools["openceo_add_ticket_comment"][0](ticket="TASK-1", content="x"))
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], expected, f"{type(error).__name__} -> {result}")

    def test_pasa_los_argumentos_al_adapter(self):
        adapter = FakeAdapter(result={"ok": True, "duplicate": False})
        server, _ = self._register(adapter)
        run(server.tools["openceo_attach_ticket_file"][0](
            ticket="TASK-1", source_path="/tmp/a.txt", file_name="a.txt", private=False
        ))
        self.assertEqual(
            adapter.calls[0],
            ("attach_file", ("TASK-1",), {"source_path": "/tmp/a.txt", "content_base64": None,
                                          "file_name": "a.txt", "private": False}),
        )

    def test_el_error_no_se_propaga(self):
        server, _ = self._register(FakeAdapter(error=ValueError("boom")))
        result = run(server.tools["openceo_update_ticket"][0](ticket="TASK-1", subject="s"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()