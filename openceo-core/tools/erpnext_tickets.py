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

import html
import logging
import re
from typing import Any

from settings import Settings
from state import CodingTool
from tools.erpnext_client import (
    ERPNextClient,
    ERPNextDuplicateError,
    ERPNextError,
)

logger = logging.getLogger(__name__)

SUBJECT_MAX_LENGTH = 140

#: Valores que acepta el Select `openceo_coding_tool` en ERPNext. Se derivan del enum
#: para que no puedan desincronizarse: si se agrega un provider, se agrega aca solo.
_CODING_TOOLS: frozenset[str] = frozenset(tool.value for tool in CodingTool)

#: OpenCEO -> ERPNext. `completed` NO esta a proposito:
#: `docs/ARCHITECTURE_REFERENCE_2026.md` dice que "completed en un registro de ejecucion no
#: salta una aprobacion de negocio de ERPNext" y que "la aceptacion de negocio queda sujeta a
#: su workflow". Que un agente termine su trabajo no es que un humano lo acepte. Cerrar el
#: ticket es una transicion de negocio explicita, que se hace en el desk de ERPNext.
#: Por eso un ticket terminado sigue en `Pending Review`: esta esperando esa decision.
#: `failed`/`escalated`/`interrupted` tampoco empujan: son resultado de ejecucion, no de negocio.
_STATUS_MAP: dict[str, str] = {
    "queued": "Open",
    "in_progress": "Working",
    "waiting": "Pending Review",
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


def map_priority(priority: str | None) -> str:
    return _PRIORITY_MAP.get(str(priority or "").strip().lower(), "Medium")


def map_status(status: str | None) -> str | None:
    """Devuelve el estado de ERPNext, o None si ese estado no debe empujarse."""
    return _STATUS_MAP.get(str(status or "").strip().lower())


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


class ERPNextTicketAdapter:
    """Superficie semantica de tickets. Envuelve al cliente HTTP."""

    def __init__(self, settings: Settings, *, client: ERPNextClient | None = None) -> None:
        self._settings = settings
        self._client = client or ERPNextClient(settings)
        self._company = (settings.ERPNEXT_TICKETS_PROJECT_COMPANY or "Jardal").strip()
        self._projects_cache: dict[str, str] = {}
        self._task_types: frozenset[str] | None = None

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

    async def update_status(self, name: str, opencero_status: str | None) -> dict[str, Any] | None:
        """Empuja una transicion de estado. Devuelve None si ese estado no se empuja."""
        target = map_status(opencero_status)
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
