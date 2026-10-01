"""Operaciones semanticas de tickets sobre ERPNext. Cero logica LLM.

Los tickets de OpenCEO viven en el DocType `Task` de ERPNext (modulo Projects) y la
identidad de proyecto en `Project`. MongoDB conserva el estado de **ejecucion**; ERPNext
es el registro de **negocio**. La direccion esta en `docs/OPENCEO_DIRECTION.md` v1.1.

Este modulo expone operaciones cerradas y con nombre de negocio. Nunca un
`create_document(doctype, payload)` generico: el modelo no elige DocTypes ni campos.

Notas de esquema verificadas contra el sitio vivo (2026-09-24):

- `Task.subject` es **Data de 140 caracteres** y es el unico campo obligatorio. Un titulo
  mas largo devuelve `CharacterLengthExceededError`, asi que se trunca aqui.
- `Task.description` es un **Text Editor (HTML)**: Frappe sanitiza el HTML al guardar, de
  modo que texto plano con `<`, `>` o `&` se corrompe en silencio. Se escapa y se
  convierten los saltos de linea a `<br>`.
- `Task.exp_end_date` es **Datetime**, no Date: una fecha desnuda no entra de forma fiable.
- `Task.status` admite Open/Working/Pending Review/Overdue/Template/Completed/Cancelled.
- `Task.priority` admite Low/Medium/High/**Urgent** (OpenCEO usa `critical`).
- `Project` exige `naming_series`, `project_name` y `company`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import logging
import re
from pathlib import Path
from typing import Any

from settings import Settings
from state import CodingTool, QAReviewState
from tools.erpnext_client import (
    ERPNextClient,
    ERPNextDuplicateError,
    ERPNextError,
    ERPNextPermissionError,
    ERPNextValidationError,
)

logger = logging.getLogger(__name__)

SUBJECT_MAX_LENGTH = 140

#: Valores que acepta el Select `openceo_coding_tool` en ERPNext. Se derivan del enum
#: para que no puedan desincronizarse: si se agrega un provider, se agrega aca solo.
_CODING_TOOLS: frozenset[str] = frozenset(tool.value for tool in CodingTool)

#: OpenCEO -> ERPNext. `completed` se resuelve por ticket (ver `resolve_erpnext_status`):
#: con revision humana pendiente -> `Pending Review` (cola QA); sin ella -> `Completed`
#: directo (Done). Decision del dueño 2026-09-28 (TASK-2026-00040, refina TASK-2026-00033):
#: para trabajo rutinario el gate tecnico (tests verdes + ausencia de QA requerida)
#: actua como aceptacion; la aceptacion manual queda para lo marcado. El mapa puro
#: deja `completed` en `Pending Review` como defecto seguro cuando no hay contexto.
#: `failed`/`escalated`/`interrupted` tampoco empujan: son resultado de ejecucion, no de negocio.
_STATUS_MAP: dict[str, str] = {
    "queued": "Open",
    "in_progress": "Working",
    "waiting": "Pending Review",
    "completed": "Pending Review",
}

_PRIORITY_MAP: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "critical": "Urgent",
}

_TASK_FIELDS = [
    "name",
    "subject",
    "status",
    "priority",
    "project",
    "openceo_ref",
    "openceo_ticket_number",
    "openceo_task_id",
    "openceo_coding_tool",
    "openceo_external_url",
]


def truncate_subject(title: str) -> str:
    """`Task.subject` es Data(140). Frappe no trunca solo: falla."""
    clean = (title or "").strip() or "Sin titulo"
    if len(clean) <= SUBJECT_MAX_LENGTH:
        return clean
    return clean[: SUBJECT_MAX_LENGTH - 1].rstrip() + "…"


def to_html_description(text: str) -> str:
    """Convierte texto plano de OpenCEO en HTML seguro para un Text Editor."""
    raw = (text or "").strip()
    if not raw:
        return ""
    return "<br>".join(html.escape(line) for line in raw.splitlines())


#: Etiqueta HTML con nombre (`<b>`, `<a href="x">`). No es un parser: solo decide si lo
#: que llega **ya es** el HTML del campo o si hay que tratarlo como texto plano.
_HTML_TAG_PATTERN = re.compile(r"<[a-zA-Z][a-zA-Z0-9]*(\s[^>]*)?>")


def to_description_html(text: str) -> str:
    """HTML si el texto ya es HTML; texto plano escapado con `<br>` si no.

    `Task.description` es un Text Editor (HTML). La creacion recibe texto plano del turno
    de voz y lo escapa; la edicion puede recibir la descripcion existente ya en HTML, y
    escaparla mostraria las etiquetas como texto literal. Pasar el HTML tal cual es
    seguro: **Frappe lo sanea al guardar** (verificado contra el sitio vivo el
    2026-10-01: `<script>` y los `href="javascript:"` se eliminan y el formato se
    conserva).

    Regla: si hay una etiqueta con nombre, es HTML; si no, es texto plano. `<3` o un `<b`
    sin cerrar no cuentan como etiqueta.
    """
    raw = str(text or "")
    if _HTML_TAG_PATTERN.search(raw):
        return raw.strip()
    return to_html_description(raw)


def map_priority(priority: str | None) -> str:
    return _PRIORITY_MAP.get(str(priority or "").strip().lower(), "Medium")


def map_status(status: str | None) -> str | None:
    """Devuelve el estado de ERPNext, o None si ese estado no debe empujarse."""
    return _STATUS_MAP.get(str(status or "").strip().lower())


def task_needs_human_review(task: dict[str, Any] | None) -> bool:
    """Senal establecida de "necesita ojo humano": QA requerida y sin aprobar.

    Es la misma senal que alimenta `QADashboard` y el gate de `QAReviewService`:
    no se inventa nada nuevo. Sin contexto (task None) se asume revision, que es
    el defecto seguro.
    """
    if not isinstance(task, dict):
        return True
    if not task.get("qa_review_required"):
        return False
    return task.get("qa_review_state") != QAReviewState.APPROVED.value


def resolve_erpnext_status(task: dict[str, Any] | None) -> str | None:
    """Estado de ERPNext para un ticket completo, o None si no debe empujarse."""
    status = str((task or {}).get("status") or "").strip().lower()
    if status == "completed":
        return "Pending Review" if task_needs_human_review(task) else "Completed"
    return _STATUS_MAP.get(status)


def normalize_due_date(value: str | None) -> str | None:
    """`exp_end_date` es Datetime: `2026-09-30` -> `2026-09-30 00:00:00`."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        return f"{raw} 00:00:00"
    return raw


def as_user_tags(tags: list[str] | None) -> str | None:
    clean = [str(tag).strip().replace(",", " ") for tag in (tags or []) if str(tag).strip()]
    return ",".join(clean) if clean else None


def map_coding_tool(value: Any) -> str | None:
    """Normaliza el provider. Devuelve None si no es un valor conocido.

    `openceo_coding_tool` es un **Select**, asi que un valor fuera de la lista haria
    fallar el POST y el ticket no se crearia. Un provider desconocido se omite: se
    pierde el dato de ejecucion, no el ticket de negocio.
    """
    raw = str(getattr(value, "value", value) or "").strip().lower()
    return raw if raw in _CODING_TOOLS else None


def tags_footer(tags: list[str] | None) -> str:
    """Las etiquetas van al cuerpo, no a `_user_tags`.

    Verificado contra el sitio vivo: Frappe **descarta** `_user_tags` en un POST por REST
    (es una columna de sistema que administra el motor de tags, no un campo escribible), y
    `frappe.client.add_tag` no existe en esta version. La via correcta seria crear
    documentos `Tag Link`, pero eso es **una llamada HTTP por etiqueta** en el camino de
    creacion del ticket, que es el camino del turno de voz. No vale el costo en v1.
    """
    value = as_user_tags(tags)
    if not value:
        return ""
    return f"<br><br><i>Etiquetas: {html.escape(value)}</i>"


# ---------------------------------------------------------------------------
# Adjuntos (TASK-2026-00079)
# ---------------------------------------------------------------------------

#: Extensiones que no se aceptan. No es una lista blanca de tipos —bloquear por
#: extension nunca es exhaustivo—, sino el corte de lo que se ejecuta o se sirve con
#: scripts: un HTML/SVG adjunto servido desde el mismo origen de ERPNext es un XSS
#: almacenado si el archivo se vuelve publico.
_BLOCKED_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        "app", "apk", "bat", "cmd", "com", "dll", "dmg", "exe", "htm", "html", "jar",
        "js", "mjs", "msi", "ps1", "scr", "sh", "so", "svg", "xhtml",
    }
)

_ATTACHMENT_NAME_MAX = 120
_FALLBACK_ATTACHMENT_NAME = "adjunto"

#: Tope de contenido que se devuelve **dentro** de una respuesta MCP. Un adjunto de
#: 10 MiB en base64 son millones de tokens en el contexto del agente: por encima de
#: esto se devuelve solo la referencia autenticada.
INLINE_CONTENT_MAX_BYTES = 256 * 1024


class AttachmentRejected(ValueError):
    """Archivo local rechazado antes de salir hacia ERPNext.

    `status` es la clasificacion que devuelve el MCP: `invalid_input`, `forbidden`
    (fuera de las raices autorizadas), `not_found`, `too_large` o `unsupported_media`.
    """

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


def sanitize_file_name(name: str | None) -> str:
    """Nombre de archivo seguro: sin ruta, sin control, sin ocultos, acotado.

    Impide traversal por construccion: solo sobrevive el ultimo segmento, y despues se
    limpia lo que Frappe guarda como nombre visible.
    """
    raw = str(name or "").replace("\\", "/").split("\x00")[0]
    base = raw.split("/")[-1]
    base = "".join(ch for ch in base if ch.isprintable() and ch not in '<>:"|?*')
    base = re.sub(r"\s+", " ", base).strip().strip(".")
    base = base.lstrip(".")
    if not base:
        return _FALLBACK_ATTACHMENT_NAME
    if len(base) <= _ATTACHMENT_NAME_MAX:
        return base
    stem, dot, extension = base.rpartition(".")
    if not dot:
        return base[:_ATTACHMENT_NAME_MAX]
    keep = _ATTACHMENT_NAME_MAX - len(extension) - 1
    return f"{stem[:keep]}.{extension}" if keep > 0 else base[:_ATTACHMENT_NAME_MAX]


def attachment_extension(name: str | None) -> str:
    return str(name or "").rpartition(".")[2].strip().lower() if "." in str(name or "") else ""


def assert_attachment_name_allowed(name: str) -> None:
    extension = attachment_extension(name)
    if extension in _BLOCKED_ATTACHMENT_EXTENSIONS:
        raise AttachmentRejected(
            f"Tipo de archivo no permitido como adjunto: .{extension}", status="unsupported_media"
        )


def content_md5(content: bytes) -> str:
    """El mismo hash que Frappe guarda en `File.content_hash` (md5 del contenido).

    Es la clave de deduplicacion: un reintento con el mismo contenido devuelve el
    adjunto existente en vez de crear otro. Frappe reutiliza el archivo en disco por
    `content_hash`, pero **siempre** crea un documento `File` nuevo.
    """
    return hashlib.md5(content, usedforsecurity=False).hexdigest()


def resolve_attachment_path(path: str | None, roots: list[str]) -> Path:
    """Resuelve una ruta local y la confina a las raices autorizadas.

    Se resuelve el enlace **y** se comprueba el prefijo: una ruta con `..` o un enlace
    simbolico que apunte fuera del alcance autorizado se rechaza.
    """
    raw = str(path or "").strip()
    if not raw:
        raise AttachmentRejected("Falta source_path o content_base64.", status="invalid_input")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise AttachmentRejected("source_path debe ser una ruta absoluta.", status="invalid_input")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AttachmentRejected(f"No existe el archivo {raw}.", status="not_found") from None
    if not resolved.is_file():
        raise AttachmentRejected(f"{raw} no es un archivo regular.", status="invalid_input")
    if not any(resolved == Path(root).resolve() or Path(root).resolve() in resolved.parents for root in roots):
        raise AttachmentRejected(
            "source_path esta fuera del alcance autorizado de adjuntos "
            f"({', '.join(roots)}).",
            status="forbidden",
        )
    return resolved


def comment_plain_text(value: str | None) -> str:
    """Texto comparable de un comentario.

    La lectura (`get_docinfo`) devuelve el contenido convertido de markdown a HTML y la
    escritura guarda el HTML tal cual, asi que la deduplicacion compara texto sin
    etiquetas y con espacios colapsados.
    """
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", str(value or ""), flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


#: Autor que se muestra en la linea de tiempo del ticket por los comentarios del agente.
COMMENT_AUTHOR_LABEL = "OpenCEO"

_ATTACHMENT_FIELDS = [
    "name",
    "file_name",
    "file_url",
    "file_type",
    "file_size",
    "is_private",
    "is_folder",
    "attached_to_doctype",
    "attached_to_name",
    "attached_to_field",
    "content_hash",
    "folder",
    "owner",
    "creation",
]


class ERPNextTicketAdapter:
    """Superficie semantica de tickets. Envuelve al cliente HTTP."""

    def __init__(self, settings: Settings, *, client: ERPNextClient | None = None) -> None:
        self._settings = settings
        self._client = client or ERPNextClient(settings)
        self._company = (settings.ERPNEXT_TICKETS_PROJECT_COMPANY or "Jardal").strip()
        self._projects_cache: dict[str, str] = {}
        self._task_types: frozenset[str] | None = None
        # `getattr` con default porque los tests construyen el adapter con un
        # `SimpleNamespace` minimo; un campo nuevo no debe romperlos.
        roots = str(getattr(settings, "ERPNEXT_ATTACHMENT_ALLOWED_ROOTS", "") or "").strip()
        self._attachment_roots = [root.strip() for root in roots.split(",") if root.strip()]
        self._attachment_max_bytes = int(
            getattr(settings, "ERPNEXT_ATTACHMENT_MAX_BYTES", 0) or 10 * 1024 * 1024
        )
        self._base_url = str(getattr(settings, "ERPNEXT_URL", "") or "").strip().rstrip("/")
        self._author = str(getattr(settings, "ERPNEXT_USER", "") or "").strip()

    @property
    def client(self) -> ERPNextClient:
        return self._client

    @property
    def is_enabled(self) -> bool:
        """Si el adapter puede operar. No hay interruptor: o esta configurado o no.

        Antes esto leia un flag `ERPNEXT_TICKETS_ENABLED`. Se elimino a proposito: con el
        apagado, OpenCEO creaba tickets que vivian solo en Mongo y el sistema de negocio
        nunca veia, y la reconciliacion no los recupera (no hace backfill). Un modo asi no
        es degradacion, es corrupcion silenciosa. La resiliencia ante ERPNext caido la da
        la marca `erpnext_pending`.
        """
        return self._client.is_configured

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- maestros --------------------------------------------------------

    async def find_project(self, project_key: str) -> str | None:
        """Busca el `Project` por su clave. NO crea nada: es camino de lectura."""
        key = (project_key or "").strip()
        if not key:
            return None
        if key in self._projects_cache:
            return self._projects_cache[key]
        found = await self._client.list_docs("Project", filters=[["project_key", "=", key]], limit=1)
        if not found:
            return None
        name = str(found[0].get("name") or "")
        if name:
            self._projects_cache[key] = name
        return name or None

    async def ensure_project(self, project_key: str, display_name: str | None = None) -> str | None:
        """Devuelve el `name` del Project, creandolo si no existe.

        Idempotente por `project_key` (campo custom con indice unico). Es el patron
        `ensure_*` del contrato de Finance §6.2: buscar por identidad externa, devolver
        el existente o crear y devolver el creado.
        """
        key = (project_key or "").strip()
        if not key:
            return None
        existing = await self.find_project(key)
        if existing:
            return existing

        created = await self._client.create_doc(
            "Project",
            {
                "naming_series": "PROJ-.####",
                "project_name": (display_name or key).strip(),
                "company": self._company,
                "project_key": key,
            },
        )
        name = str(created.get("name") or "")
        if name:
            self._projects_cache[key] = name
        return name or None

    async def _valid_task_types(self) -> frozenset[str]:
        """Los `Task Type` reales del sitio. Si la consulta falla, se omiten los tipos."""
        if self._task_types is None:
            try:
                rows = await self._client.list_docs("Task Type", fields=["name"], limit=50)
                self._task_types = frozenset(str(row.get("name") or "") for row in rows)
            except ERPNextError as exc:
                logger.warning("No se pudieron leer los Task Type: %s", exc)
                self._task_types = frozenset()
        return self._task_types

    # --- tickets ---------------------------------------------------------

    async def find_by_ref(self, ref: str) -> dict[str, Any] | None:
        key = (ref or "").strip()
        if not key:
            return None
        rows = await self._client.list_docs("Task", filters=[["openceo_ref", "=", key]], fields=_TASK_FIELDS, limit=1)
        return rows[0] if rows else None

    async def get_ticket(self, identifier: str) -> dict[str, Any] | None:
        """Lee un ticket por su nombre de ERPNext (`TASK-...`) o por su `openceo_ref`.

        Existe para que un agente pueda comprobar el estado de un ticket concreto antes de
        decidir si crea otro.
        """
        key = (identifier or "").strip()
        if not key:
            return None
        if key.upper().startswith("TASK-"):
            try:
                return await self._client.get_doc("Task", key)
            except ERPNextError:
                return None
        return await self.find_by_ref(key)

    async def search_tickets(
        self,
        *,
        query: str | None = None,
        project_key: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Busca tickets de negocio en ERPNext.

        Es la capacidad que un agente necesita para **no duplicar**: antes de registrar
        trabajo conviene mirar si ya existe un ticket que lo cubra. Busca sobre el registro
        de negocio, no sobre Mongo, porque el backlog vive ahi y puede no tener todavia una
        tarea de ejecucion asociada.

        Solo lectura: un `project_key` inexistente devuelve lista vacia en vez de crear el
        proyecto, que es lo que haria `ensure_project`.
        """
        filters: list[list[Any]] = []
        if query:
            filters.append(["subject", "like", f"%{str(query).strip()}%"])
        if project_key:
            project_name = await self.find_project(project_key)
            if not project_name:
                return []
            filters.append(["project", "=", project_name])
        if status:
            filters.append(["status", "=", str(status).strip()])

        return await self._client.list_docs(
            "Task",
            filters=filters or None,
            fields=_TASK_FIELDS,
            limit=max(1, min(int(limit or 20), 100)),
        )

    async def build_payload(
        self,
        *,
        ref: str,
        title: str,
        description: str = "",
        project_name: str | None = None,
        priority: str | None = None,
        ticket_type: str | None = None,
        tags: list[str] | None = None,
        due_date: str | None = None,
        ticket_number: int | None = None,
        task_id: str | None = None,
        coding_tool: Any = None,
        external_url: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": truncate_subject(title),
            "status": "Open",
            "priority": map_priority(priority),
            "openceo_ref": ref,
        }

        body = to_html_description(description)
        # Si el titulo se trunco, el original completo sobrevive al principio del cuerpo.
        if body and len((title or "").strip()) > SUBJECT_MAX_LENGTH:
            body = f"<b>{html.escape((title or '').strip())}</b><br><br>{body}"

        footer = tags_footer(tags)
        if footer:
            body = f"{body}{footer}" if body else footer.lstrip("<br>")
        if body:
            payload["description"] = body

        if project_name:
            payload["project"] = project_name

        if ticket_type:
            valid = await self._valid_task_types()
            if str(ticket_type) in valid:
                payload["type"] = str(ticket_type)
            else:
                # Un tipo desconocido no puede tumbar la creacion del ticket.
                logger.info("Task Type '%s' no existe en ERPNext; se omite.", ticket_type)

        normalized_due = normalize_due_date(due_date)
        if normalized_due:
            payload["exp_end_date"] = normalized_due

        if ticket_number is not None:
            payload["openceo_ticket_number"] = int(ticket_number)
        if task_id:
            payload["openceo_task_id"] = str(task_id)

        coding = map_coding_tool(coding_tool)
        if coding:
            payload["openceo_coding_tool"] = coding
        url = str(external_url or "").strip()
        if url:
            payload["openceo_external_url"] = url

        return payload

    async def create_ticket(
        self,
        *,
        ref: str,
        title: str,
        description: str = "",
        project_key: str | None = None,
        priority: str | None = None,
        ticket_type: str | None = None,
        tags: list[str] | None = None,
        due_date: str | None = None,
        ticket_number: int | None = None,
        task_id: str | None = None,
        coding_tool: Any = None,
        external_url: str | None = None,
    ) -> dict[str, Any]:
        """Crea el Task. Idempotente por `ref`.

        No hace un GET previo en el camino feliz: un round trip en el caso normal en vez
        de dos. Si el POST choca con el indice unico (HTTP 417), significa que un intento
        anterior si llego a crear el documento, asi que se relee por `ref` y se devuelve
        como exito idempotente.
        """
        project_name = await self.ensure_project(project_key) if project_key else None
        payload = await self.build_payload(
            ref=ref,
            title=title,
            description=description,
            project_name=project_name,
            priority=priority,
            ticket_type=ticket_type,
            tags=tags,
            due_date=due_date,
            ticket_number=ticket_number,
            task_id=task_id,
            coding_tool=coding_tool,
            external_url=external_url,
        )

        try:
            created = await self._client.create_doc("Task", payload)
        except ERPNextDuplicateError:
            existing = await self.find_by_ref(ref)
            if existing is None:
                raise
            return {"ok": True, "duplicate": True, "task": existing, "ref": ref}

        return {"ok": True, "duplicate": False, "task": created, "ref": ref}

    async def update_status(
        self,
        name: str,
        opencero_status: str | None,
        *,
        task: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Empuja una transicion de estado. Devuelve None si ese estado no se empuja.

        Para `completed` el destino depende del ticket (QA condicional): con `task`
        se resuelve (`Pending Review` o `Completed`); sin contexto rige el defecto
        seguro (`Pending Review`).
        """
        status = str(opencero_status or "").strip().lower()
        if status == "completed":
            # Sin contexto rige el defecto seguro (cola QA); con ticket se resuelve.
            target = (
                "Pending Review"
                if task is None
                else resolve_erpnext_status({**task, "status": status})
            )
        else:
            target = map_status(status)
        if target is None or not (name or "").strip():
            return None
        return await self._client.update_doc("Task", name.strip(), {"status": target})

    async def update_tier_s_progress(self, name: str, summary: dict[str, Any]) -> None:
        """Project execution detail in the existing ticket without changing business status."""
        if not name.strip():
            return
        # Visible sentinels survive Frappe's HTML sanitizer; comments do not.
        start = "OPENCEO_TIER_S_BEGIN"
        end = "OPENCEO_TIER_S_END"
        task = await self._client.get_doc("Task", name.strip())
        description = str(task.get("description") or "")
        if start in description and end in description:
            pattern = re.compile(
                rf"<p[^>]*>\s*{start}\s*</p>.*?<p[^>]*>\s*{end}\s*</p>",
                re.IGNORECASE | re.DOTALL,
            )
            cleaned = pattern.sub("", description, count=1)
            if cleaned == description:
                before, rest = description.split(start, 1)
                _, after = rest.split(end, 1)
                cleaned = before + after
            description = cleaned
        phase = html.escape(str(summary.get("phase") or "pending"))
        status = html.escape(str(summary.get("status") or "pending"))
        subplan = html.escape(str(summary.get("subplan") or "-"))
        round_ = html.escape(str(summary.get("round") or "-"))
        reviewers = summary.get("reviewers") or {}
        completed = ", ".join(html.escape(str(label)) for label, ok in reviewers.items() if ok)
        detail = (f"<p><b>TIER S:</b> {phase} · {status} · subplan {subplan} · ronda {round_}. "
                  f"Revisores: {completed or 'pendientes'}.</p>")
        availability = summary.get("reviewer_availability") or {}
        unavailable = ", ".join(
            f"{html.escape(str(label))} ({html.escape(str(state))})"
            for label, state in availability.items() if state != "available"
        )
        if unavailable:
            detail += f"<p>Revisores pendientes: {unavailable}.</p>"
        reason = summary.get("waiting_reason")
        if reason:
            detail += f"<p>{html.escape(str(reason))}</p>"
        verification = summary.get("verification") or {}
        if isinstance(verification, dict) and verification.get("qa_status"):
            detail += f"<p>QA: {html.escape(str(verification['qa_status']))}. "
            detail += f"{html.escape(str(verification.get('detail') or ''))}</p>"
        question = summary.get("question")
        if isinstance(question, dict):
            detail += f"<p><b>Requiere atención humana:</b> {html.escape(str(question.get('text') or ''))}</p>"
            options = question.get("options") or []
            detail += "<ul>" + "".join(f"<li>{html.escape(str(option))}</li>" for option in options) + "</ul>"
        body = description.rstrip() + f"\n<p>{start}</p>{detail}<p>{end}</p>"
        await self._client.update_doc("Task", name.strip(), {"description": body})

    # --- edicion del ticket (TASK-2026-00079) -----------------------------

    async def resolve_task(self, identifier: str) -> dict[str, Any]:
        """Resuelve `TASK-2026-000NN` u `openceo_ref` a la fila del ticket.

        Se hace antes de leer o escribir: un identificador adivinado nunca debe ser el
        destino de un comentario, un adjunto o una edicion. No comprueba el proyecto
        contra una lista —el MCP sirve a varias sesiones y no conoce "el proyecto
        activo"—; devuelve `project` y `openceo_ref` para que quien llama lo compruebe.
        """
        key = (identifier or "").strip()
        if not key:
            raise ERPNextValidationError(
                "Falta el ticket: usa TASK-2026-000NN u openceo_ref.", status_code=400
            )
        if key.upper().startswith("TASK-"):
            try:
                row = await self._client.get_doc("Task", key)
            except ERPNextError as exc:
                if exc.status_code == 404:
                    raise self._ticket_not_found(key) from None
                raise
        else:
            row = await self.find_by_ref(key)
            if row is None:
                raise self._ticket_not_found(key)
        return row

    @staticmethod
    def _ticket_not_found(identifier: str) -> ERPNextValidationError:
        return ERPNextValidationError(f"No existe el ticket {identifier}.", status_code=404)

    def _ticket_view(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": task.get("name"),
            "subject": task.get("subject"),
            "status": task.get("status"),
            "priority": task.get("priority"),
            "project": task.get("project"),
            "openceo_ref": task.get("openceo_ref"),
        }

    async def update_ticket(
        self,
        identifier: str,
        *,
        subject: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
    ) -> dict[str, Any]:
        """Edita los campos de contenido del ticket. No toca el estado.

        El estado y las dependencias son una maquina de estados de negocio, no un campo
        de edicion: los mueve la ejecucion (o el desk), y escribirlos aqui saltaria la
        contabilidad de `status_pushed` del observador y podria rebotar con el webhook.
        """
        task = await self.resolve_task(identifier)
        name = str(task.get("name") or "").strip()
        body: dict[str, Any] = {}

        if subject is not None:
            clean_subject = str(subject).strip()
            if not clean_subject:
                raise ERPNextValidationError("El titulo no puede quedar vacio.", status_code=400)
            body["subject"] = truncate_subject(clean_subject)
        if description is not None:
            body["description"] = to_description_html(str(description))
        if priority is not None:
            clean_priority = str(priority).strip().lower()
            if clean_priority not in _PRIORITY_MAP:
                raise ERPNextValidationError(
                    f"Prioridad invalida: {priority}. Usa {'/'.join(_PRIORITY_MAP)}.", status_code=400
                )
            body["priority"] = map_priority(clean_priority)
        if due_date is not None:
            normalized = normalize_due_date(str(due_date))
            if not normalized:
                raise ERPNextValidationError("Fecha de vencimiento invalida.", status_code=400)
            body["exp_end_date"] = normalized

        if not body:
            raise ERPNextValidationError(
                "Nada que actualizar: indica subject, description, priority o due_date.",
                status_code=400,
            )

        updated = await self._client.update_doc("Task", name, body)
        # Se devuelve la vista corta, no el documento entero: cada campo de la respuesta
        # viaja al contexto del agente. `changed` dice que se toco.
        return {"ok": True, "changed": sorted(body), "ticket": self._ticket_view({**task, **(updated or {})})}

    # --- hilo del ticket: comentarios -------------------------------------

    def _author_email(self) -> str:
        return self._author or "openceo@jardal.app"

    async def _docinfo(self, task_name: str) -> dict[str, Any]:
        """Lee el hilo del ticket con el cargador del desk (`get_docinfo`).

        Es el unico camino de lectura de comentarios y adjuntos que tiene este
        principal: `Comment` no es legible por rol (`Projects User` + `Accounts User`) y
        la lista de `File` esta acotada al propietario. `get_docinfo` comprueba permiso
        de lectura sobre el **ticket** y devuelve exactamente lo que muestra la linea de
        tiempo.
        """
        body = await self._client.call_method(
            "frappe.desk.form.load.get_docinfo",
            http_method="GET",
            params={"doctype": "Task", "name": task_name},
        )
        info = body.get("docinfo")
        if not isinstance(info, dict):
            message = body.get("message")
            info = message.get("docinfo") if isinstance(message, dict) else None
        return info if isinstance(info, dict) else {}

    def _comment_view(self, row: dict[str, Any]) -> dict[str, Any]:
        author = str(row.get("owner") or row.get("comment_email") or "")
        return {
            "id": row.get("name"),
            "author": author,
            "creation": row.get("creation"),
            "content": row.get("content"),
            "editable": bool(self._author) and author.lower() == self._author.lower(),
        }

    def _attachment_view(self, row: dict[str, Any]) -> dict[str, Any]:
        url = str(row.get("file_url") or "")
        view: dict[str, Any] = {
            "id": row.get("name"),
            "file_name": row.get("file_name"),
            "file_type": row.get("file_type"),
            "size": row.get("file_size"),
            "private": bool(row.get("is_private")),
            "file_url": url,
            "download_url": f"{self._base_url}{url}" if url.startswith("/") else url,
        }
        # El listado del hilo (`get_docinfo`) no devuelve estos dos campos, y el listado
        # propio si: se omiten cuando no se saben, en vez de afirmar `null`.
        if row.get("attached_to_name"):
            view["attached_to"] = row["attached_to_name"]
        if row.get("content_hash"):
            view["content_hash"] = row["content_hash"]
        return view

    async def thread_of(self, task_name: str, *, limit: int = 20) -> dict[str, Any]:
        """Comentarios y adjuntos de un ticket ya resuelto.

        Separado de `list_thread` para que quien ya tiene el ticket (la lectura unica del
        MCP) no vuelva a resolverlo: un GET menos por lectura.
        """
        info = await self._docinfo(str(task_name or ""))
        top = max(1, min(int(limit or 20), 200))
        comments = [row for row in (info.get("comments") or []) if isinstance(row, dict)]
        attachments = [row for row in (info.get("attachments") or []) if isinstance(row, dict)]
        comments.sort(key=lambda row: str(row.get("creation") or ""), reverse=True)
        return {
            "comment_count": len(comments),
            "attachment_count": len(attachments),
            "comments": [self._comment_view(row) for row in comments[:top]],
            "attachments": [self._attachment_view(row) for row in attachments],
        }

    async def list_thread(self, identifier: str, *, limit: int = 20) -> dict[str, Any]:
        """Comentarios y adjuntos visibles en la linea de tiempo del ticket."""
        task = await self.resolve_task(identifier)
        thread = await self.thread_of(str(task.get("name") or ""), limit=limit)
        return {"ok": True, "ticket": self._ticket_view(task), **thread}

    def _find_identical_comment(self, info: dict[str, Any], content: str) -> dict[str, Any] | None:
        """Deduplicacion comprobable de comentarios: mismo autor y mismo texto.

        Un reintento tras un timeout ambiguo no debe duplicar el comentario, y Frappe no
        da donde guardar una clave: el comentario se inserta por el metodo oficial, que
        no acepta campos extra. Se compara el texto normalizado contra los comentarios
        del mismo autor en este ticket, y se devuelve el existente. El costo es el mismo
        texto repetido a proposito en el mismo ticket: se evita añadiendo algo al final.
        """
        wanted = comment_plain_text(content)
        if not wanted:
            return None
        for row in info.get("comments") or []:
            if not isinstance(row, dict):
                continue
            author = str(row.get("owner") or row.get("comment_email") or "")
            if self._author and author.lower() != self._author.lower():
                continue
            if comment_plain_text(row.get("content")) == wanted:
                return self._comment_view(row)
        return None

    async def add_comment(self, identifier: str, content: str) -> dict[str, Any]:
        """Publica un comentario en el ticket, idempotente por contenido."""
        task = await self.resolve_task(identifier)
        text = str(content or "").strip()
        if not text:
            raise ERPNextValidationError("El comentario no puede estar vacio.", status_code=400)
        name = str(task.get("name") or "").strip()
        info = await self._docinfo(name)
        existing = self._find_identical_comment(info, text)
        if existing is not None:
            return {"ok": True, "duplicate": True, "ticket": self._ticket_view(task), "comment": existing}

        body = await self._client.call_method(
            "frappe.desk.form.utils.add_comment",
            json={
                "reference_doctype": "Task",
                "reference_name": name,
                "content": text,
                "comment_email": self._author_email(),
                "comment_by": COMMENT_AUTHOR_LABEL,
            },
            attempts=1,  # crea: un reintento automatico duplicaria
        )
        message = body.get("message")
        created = message if isinstance(message, dict) else {}
        return {
            "ok": True,
            "duplicate": False,
            "ticket": self._ticket_view(task),
            "comment": self._comment_view({**created, "owner": created.get("owner") or self._author_email()}),
        }

    async def _comment_row(self, comment_id: str, ticket: str | None = None) -> dict[str, Any]:
        """Lee un comentario y comprueba que cuelga de un ticket (y de **ese**, si se da).

        Es la frontera que sustituye a la comprobacion que hace el metodo oficial del desk:
        con el rol `OpenCEO API` el principal puede escribir cualquier `Comment` del sitio,
        asi que aqui se acota a los comentarios de un `Task` que se puede leer.
        """
        key = str(comment_id or "").strip()
        if not key:
            raise ERPNextValidationError("Falta el id del comentario.", status_code=400)
        try:
            row = await self._client.get_doc("Comment", key)
        except ERPNextError as exc:
            if exc.status_code == 404:
                raise ERPNextValidationError(f"No existe el comentario {key}.", status_code=404) from None
            raise
        if str(row.get("comment_type") or "Comment") != "Comment":
            raise ERPNextValidationError(
                f"{key} no es un comentario (es {row.get('comment_type')}).", status_code=400
            )
        reference = str(row.get("reference_doctype") or "")
        if reference != "Task":
            raise ERPNextValidationError(
                f"El comentario {key} cuelga de {reference or 'nada'}, no de un ticket.", status_code=400
            )
        await self.resolve_task(str(row.get("reference_name") or ""))
        if ticket:
            expected = str(ticket).strip()
            actual = str(row.get("reference_name") or "")
            if expected.upper().startswith("TASK-"):
                if actual != expected:
                    raise ERPNextValidationError(
                        f"El comentario {key} no cuelga del ticket {expected}.", status_code=400
                    )
            else:
                resolved = await self.resolve_task(expected)
                if str(resolved.get("name") or "") != actual:
                    raise ERPNextValidationError(
                        f"El comentario {key} no cuelga del ticket {expected}.", status_code=400
                    )
        return row

    async def update_comment(self, comment_id: str, content: str) -> dict[str, Any]:
        """Edita un comentario, sea de quien sea.

        Primero por el metodo oficial del desk (`update_comment`), que solo acepta al
        autor: si lo rechaza, se edita por `/api/resource/Comment`, que el rol dedicado
        `OpenCEO API` si permite (decision del dueño del 2026-10-01: control completo del
        hilo). La via de reserva comprueba antes que el comentario cuelga de un ticket
        legible, para no escribir sobre documentos ajenos al hilo.
        """
        key = str(comment_id or "").strip()
        if not key:
            raise ERPNextValidationError("Falta el id del comentario.", status_code=400)
        text = str(content or "").strip()
        if not text:
            raise ERPNextValidationError("El comentario no puede estar vacio.", status_code=400)
        try:
            await self._client.call_method(
                "frappe.desk.form.utils.update_comment",
                json={"name": key, "content": text},
                attempts=1,  # no es idempotente por contenido: no reintentar a ciegas
            )
        except ERPNextPermissionError:
            await self._comment_row(key)
            await self._client.update_doc("Comment", key, {"content": text})
        return {"ok": True, "comment_id": key, "content": text}

    async def delete_comment(self, comment_id: str, ticket: str) -> dict[str, Any]:
        """Borra un comentario. Exige el rol `OpenCEO API`: Frappe no tiene metodo publico.

        El metodo del desk solo edita; el borrado de `Comment` lo reserva Frappe a
        `System Manager`/`Website Manager`. Con el rol dedicado (autorizado por el dueño
        el 2026-10-01) se puede, y se exige el ticket para que un id equivocado no borre
        el comentario de otro trabajo.
        """
        row = await self._comment_row(comment_id, ticket)
        key = str(row.get("name") or "").strip()
        await self._client.delete_doc("Comment", key)
        return {"ok": True, "deleted": key, "ticket": str(row.get("reference_name") or "")}

    # --- hilo del ticket: adjuntos ----------------------------------------

    def _read_attachment_source(
        self,
        *,
        source_path: str | None,
        content_base64: str | None,
        file_name: str | None,
    ) -> tuple[bytes, str]:
        """Obtiene (contenido, nombre saneado) de las dos formas aceptadas."""
        given = [bool(str(source_path or "").strip()), bool(str(content_base64 or "").strip())]
        if sum(given) != 1:
            raise AttachmentRejected(
                "Da exactamente uno: source_path (ruta local autorizada) o content_base64.",
                status="invalid_input",
            )

        if given[0]:
            resolved = resolve_attachment_path(source_path, self._attachment_roots)
            name = file_name or resolved.name
            try:
                content = resolved.read_bytes()
            except OSError as exc:
                raise AttachmentRejected(f"No se pudo leer {resolved}: {exc}", status="invalid_input") from exc
        else:
            try:
                content = base64.b64decode(str(content_base64), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise AttachmentRejected(f"content_base64 no es base64 valido: {exc}", status="invalid_input") from exc
            name = file_name or None

        if not content:
            raise AttachmentRejected("El archivo esta vacio.", status="invalid_input")
        if len(content) > self._attachment_max_bytes:
            raise AttachmentRejected(
                f"El archivo pesa {len(content)} bytes y el limite es {self._attachment_max_bytes}.",
                status="too_large",
            )
        clean_name = sanitize_file_name(name)
        assert_attachment_name_allowed(clean_name)
        return content, clean_name

    async def attach_file(
        self,
        identifier: str,
        *,
        source_path: str | None = None,
        content_base64: str | None = None,
        file_name: str | None = None,
        private: bool = True,
    ) -> dict[str, Any]:
        """Adjunta un archivo al ticket, idempotente por `content_hash`."""
        task = await self.resolve_task(identifier)
        name = str(task.get("name") or "").strip()
        content, clean_name = self._read_attachment_source(
            source_path=source_path, content_base64=content_base64, file_name=file_name
        )
        digest = content_md5(content)

        # La lista de `File` esta acotada al propietario (condicion de permiso de
        # Frappe), asi que esto deduplica los adjuntos que sube este principal. Un
        # archivo identico subido por una persona no se ve desde aqui.
        existing = await self._client.list_docs(
            "File",
            filters=[
                ["attached_to_doctype", "=", "Task"],
                ["attached_to_name", "=", name],
                ["content_hash", "=", digest],
            ],
            fields=_ATTACHMENT_FIELDS,
            limit=1,
        )
        if existing:
            return {
                "ok": True,
                "duplicate": True,
                "ticket": self._ticket_view(task),
                "attachment": self._attachment_view(existing[0]),
            }

        created = await self._client.upload_file(
            filename=clean_name,
            content=content,
            doctype="Task",
            docname=name,
            is_private=bool(private),
        )
        return {
            "ok": True,
            "duplicate": False,
            "ticket": self._ticket_view(task),
            "attachment": self._attachment_view({**created, "content_hash": digest}),
        }

    async def get_attachment(self, attachment: str, *, include_content: bool = False) -> dict[str, Any]:
        """Lee un adjunto: metadatos, referencia autenticada y contenido opcional."""
        key = str(attachment or "").strip()
        if not key:
            raise ERPNextValidationError("Falta el id del adjunto.", status_code=400)
        try:
            row = await self._client.get_doc("File", key)
        except ERPNextError as exc:
            if exc.status_code == 404:
                raise ERPNextValidationError(f"No existe el adjunto {key}.", status_code=404) from None
            raise
        view = self._attachment_view(row)
        if not include_content:
            return {"ok": True, "attachment": view}

        url = str(row.get("file_url") or "")
        if not url.startswith("/") or row.get("is_folder"):
            raise AttachmentRejected("El adjunto no tiene contenido descargable.", status="invalid_input")
        if int(row.get("file_size") or 0) > INLINE_CONTENT_MAX_BYTES:
            raise AttachmentRejected(
                f"El adjunto supera {INLINE_CONTENT_MAX_BYTES} bytes: usa download_url.",
                status="too_large",
            )
        content = await self._client.download_bytes(url, max_bytes=INLINE_CONTENT_MAX_BYTES)
        try:
            return {"ok": True, "attachment": {**view, "content_text": content.decode("utf-8")}}
        except UnicodeDecodeError:
            return {"ok": True, "attachment": {**view, "content_base64": base64.b64encode(content).decode("ascii")}}

    async def _linked_attachment(self, attachment: str, identifier: str) -> tuple[str, dict[str, Any]]:
        """Comprueba que un adjunto existe y cuelga **de ese** ticket.

        Antes de renombrar o borrar: un id adivinado no debe alcanzar el archivo de otro
        ticket, y el ticket se resuelve igual que en las demas operaciones.
        """
        key = str(attachment or "").strip()
        if not key:
            raise ERPNextValidationError("Falta el id del adjunto.", status_code=400)
        task = await self.resolve_task(identifier)
        name = str(task.get("name") or "").strip()
        try:
            row = await self._client.get_doc("File", key)
        except ERPNextError as exc:
            if exc.status_code == 404:
                raise ERPNextValidationError(f"No existe el adjunto {key}.", status_code=404) from None
            raise
        if row.get("is_folder"):
            raise ERPNextValidationError(f"{key} es una carpeta, no un adjunto.", status_code=400)
        if str(row.get("attached_to_doctype") or "") != "Task" or str(row.get("attached_to_name") or "") != name:
            raise ERPNextValidationError(
                f"El adjunto {key} no cuelga del ticket {name}.", status_code=400
            )
        return name, row

    async def update_attachment(
        self,
        attachment: str,
        ticket: str,
        *,
        file_name: str | None = None,
        private: bool | None = None,
    ) -> dict[str, Any]:
        """Renombra un adjunto o cambia su privacidad."""
        _, row = await self._linked_attachment(attachment, ticket)
        body: dict[str, Any] = {}
        if file_name is not None:
            clean_name = sanitize_file_name(file_name)
            assert_attachment_name_allowed(clean_name)
            body["file_name"] = clean_name
        if private is not None:
            body["is_private"] = 1 if private else 0
        if not body:
            raise ERPNextValidationError("Nada que actualizar: indica file_name o private.", status_code=400)
        updated = await self._client.update_doc("File", str(row.get("name")), body)
        return {"ok": True, "attachment": self._attachment_view(updated or row)}

    async def delete_attachment(self, attachment: str, ticket: str) -> dict[str, Any]:
        """Borra un adjunto con el metodo oficial del desk."""
        _, row = await self._linked_attachment(attachment, ticket)
        await self._client.call_method(
            "frappe.desk.form.utils.remove_attach",
            json={"fid": str(row.get("name"))},
            attempts=1,  # borrar ya lo hizo: un reintento informaria un falso fallo
        )
        return {"ok": True, "deleted": str(row.get("name")), "file_name": row.get("file_name")}
