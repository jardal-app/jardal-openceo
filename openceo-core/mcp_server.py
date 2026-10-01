from __future__ import annotations

import ipaddress
import json
import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from settings import Settings
from tools.cloudflare import CloudflareDNSManager
from tools.erpnext_client import (
    ERPNextDuplicateError,
    ERPNextPermissionError,
    ERPNextUnavailable,
    ERPNextValidationError,
)
from tools.erpnext_tickets import AttachmentRejected
from state import CodingTier, CodingTool, TaskCreateRequest, TaskPriority, TaskSource

logger = logging.getLogger(__name__)

try:
    from fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - validated at runtime when enabled
    FastMCP = None  # type: ignore[assignment]
    _FASTMCP_IMPORT_ERROR = exc
else:
    _FASTMCP_IMPORT_ERROR = None

_DEFAULT_MCP_PATH = "/internal/mcp"
_DEFAULT_GLOBAL_PATH = "/internal/mcp-global"
_DEFAULT_ALLOWED_IPS = "127.0.0.1/32,::1/128"
_SUPPORTED_TRANSPORTS = {"http", "streamable-http", "sse"}


@dataclass(frozen=True)
class MountedMCPServer:
    path: str
    app: Any
    server: Any
    transport: str
    tool_names: tuple[str, ...]
    internal_url: str
    bearer_token: str | None
    stateless_http: bool = False
    json_response: bool = False

    @property
    def auth_enabled(self) -> bool:
        return bool(self.bearer_token)

    @asynccontextmanager
    async def lifespan(self):
        raw_app = self.server.http_app(
            path="/",
            transport=self.transport,
            stateless_http=self.stateless_http,
            json_response=self.json_response,
        )
        self.app.set_inner_app(raw_app)
        try:
            async with _coerce_lifespan_context(raw_app.lifespan, raw_app):
                yield
        finally:
            self.app.set_inner_app(None)


def build_internal_mcp_mount(
    settings: Settings,
    task_service: "TaskService",
    erpnext: Any | None = None,
) -> MountedMCPServer | None:
    """Build the internal MCP — all tools (infra + OpenCEO-specific). Used by OpenCEO tmux sessions."""
    if not settings.MCP_SERVER_ENABLED:
        return None
    return _build_mcp(
        settings=settings,
        path=_normalize_path(settings.MCP_SERVER_PATH),
        name="OpenCEO Internal MCP",
        register_fn=lambda s, st: _register_all_tools(s, st, task_service, erpnext),
        stateless_http=True,
        json_response=True,
    )


def build_global_mcp_mount(
    settings: Settings,
    task_service: "TaskService | None" = None,
    erpnext: Any | None = None,
) -> MountedMCPServer | None:
    """Build the global MCP — infra + tickets. Used by manual Claude/Codex on any project.

    Originalmente exponia solo infraestructura (DNS). Se le agregaron los tools de tickets
    porque es la unica superficie que ven las sesiones manuales de Codex CLI
    (`~/.codex/config.toml` apunta aqui), y sin ellos un agente fuera de OpenCEO no puede
    ni buscar ni registrar trabajo.

    Los dos mounts exponen lo mismo; la diferencia es **quien los recibe**, no que
    contienen: el interno se inyecta por configuracion en las sesiones que OpenCEO
    gestiona, y el global es opt-in para sesiones manuales del host.

    Superficie de seguridad: `MCP_SERVER_ALLOWED_IPS` es loopback-only por defecto y el
    mount no se publica en Traefik, asi que solo alcanzable desde esta maquina.
    """
    if not settings.MCP_SERVER_ENABLED:
        return None
    if task_service is None:
        register_fn = _register_infra_tools
    else:
        register_fn = lambda s, st: _register_all_tools(s, st, task_service, erpnext)
    return _build_mcp(
        settings=settings,
        path=_normalize_path(settings.MCP_GLOBAL_PATH),
        name="OpenCEO Global MCP",
        register_fn=register_fn,
        transport="streamable-http",
        stateless_http=False,
        json_response=True,
    )


def _build_mcp(
    settings: Settings,
    path: str,
    name: str,
    register_fn: Any,
    transport: str | None = None,
    stateless_http: bool = False,
    json_response: bool = False,
) -> MountedMCPServer:
    if FastMCP is None:
        raise RuntimeError("MCP_SERVER_ENABLED=true pero fastmcp no está instalado.") from _FASTMCP_IMPORT_ERROR

    transport = _normalize_transport(transport if transport is not None else settings.MCP_SERVER_TRANSPORT)
    bearer_token = _resolve_bearer_token(settings)
    allowed_networks = _parse_allowed_networks(settings.MCP_SERVER_ALLOWED_IPS)

    server = FastMCP(name)
    tool_names = register_fn(server, settings)
    return build_mounted_mcp_server(
        path=path,
        name=name,
        server=server,
        bearer_token=bearer_token,
        allowed_networks=allowed_networks,
        transport=transport,
        tool_names=tool_names,
        internal_url=_build_internal_url(settings, path),
        stateless_http=stateless_http,
        json_response=json_response,
    )


def build_mounted_mcp_server(
    *,
    path: str,
    name: str,
    server: Any,
    bearer_token: str | None,
    allowed_networks: tuple[ipaddress._BaseNetwork, ...] | None = None,
    transport: str = "streamable-http",
    tool_names: tuple[str, ...] = (),
    internal_url: str = "",
    stateless_http: bool = False,
    json_response: bool = False,
) -> MountedMCPServer:
    """Protege y monta un servidor FastMCP ya construido.

    Existe para que una montura que **no** se arma con `_build_mcp` (p. ej. el broker
    de research, cuyas tools necesitan dependencias inyectadas) no duplique — ni
    olvide — la unica frontera que importa: loopback + Bearer. Sin esta funcion, una
    montura nueva tendria que copiar `_InternalOnlyBearerWrapper` a mano, que es
    exactamente como se acaba publicando un endpoint sin proteccion.
    """
    if allowed_networks is None:
        allowed_networks = _parse_allowed_networks(_DEFAULT_ALLOWED_IPS)
    if bearer_token is None:
        logger.warning("%s enabled without Bearer token; loopback restriction is the only control", name)
    return MountedMCPServer(
        path=_normalize_path(path),
        app=_InternalOnlyBearerWrapper(
            allowed_networks=allowed_networks,
            bearer_token=bearer_token,
        ),
        server=server,
        transport=_normalize_transport(transport),
        tool_names=tool_names,
        internal_url=internal_url,
        bearer_token=bearer_token,
        stateless_http=stateless_http,
        json_response=json_response,
    )


def build_mcp_provider_env(settings: Settings) -> dict[str, str]:
    """Env vars injected into OpenCEO tmux sessions — points to internal MCP (all tools)."""
    if not settings.MCP_SERVER_ENABLED:
        return {}

    env = {
        "OPENCEO_INTERNAL_MCP_ENABLED": "true",
        "OPENCEO_INTERNAL_MCP_URL": _build_internal_url(settings, _normalize_path(settings.MCP_SERVER_PATH)),
        "OPENCEO_INTERNAL_MCP_TRANSPORT": _normalize_transport(settings.MCP_SERVER_TRANSPORT),
    }
    bearer_token = _resolve_bearer_token(settings)
    if bearer_token:
        env["OPENCEO_INTERNAL_MCP_BEARER_TOKEN"] = bearer_token
    return env


def build_mcp_provider_config(settings: Settings) -> dict[str, Any] | None:
    """Serializable MCP config for headless provider CLIs such as Claude Code."""
    if not settings.MCP_SERVER_ENABLED:
        return None

    transport = _normalize_transport(settings.MCP_SERVER_TRANSPORT)
    server: dict[str, Any] = {
        "type": "sse" if transport == "sse" else "http",
        "url": _build_internal_url(settings, _normalize_path(settings.MCP_SERVER_PATH)),
    }
    bearer_token = _resolve_bearer_token(settings)
    if bearer_token:
        server["headers"] = {"Authorization": f"Bearer {bearer_token}"}
    return {"mcpServers": {"openceo-internal": server}}


# ---------------------------------------------------------------------------
# Tool registration — split into infra (global) and all (internal)
# ---------------------------------------------------------------------------

def _register_infra_tools(server: FastMCP, settings: Settings) -> tuple[str, ...]:
    """Infrastructure tools available globally (DNS)."""
    manager = CloudflareDNSManager(settings)

    def manage_dns_check_or_create(hostname: str) -> dict[str, Any]:
        return manager.check_or_create(hostname=hostname)

    server.tool(
        manage_dns_check_or_create,
        name="manage_dns_check_or_create",
        description="DNS: create/verify A record in Cloudflare. Use before deploying with Traefik.",
    )
    return ("manage_dns_check_or_create",)


def _register_openceo_tools(
    server: FastMCP,
    settings: Settings,
    task_service: "TaskService",
    erpnext: Any | None = None,
) -> tuple[str, ...]:
    """OpenCEO-specific tools (ticket CRUD via ERPNext + MongoDB). Never exposed globally."""

    async def _enrich_with_execution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Añade el estado de EJECUCION de Mongo a los tickets de negocio de ERPNext.

        El ticket de negocio puede no tener tarea de ejecucion todavia (backlog
        registrado y aun no arrancado), y al reves, la ejecucion puede ir por detras del
        estado de negocio. Se devuelven los dos para que un agente vea la foto completa.
        """
        for row in rows:
            task_id = str(row.get("openceo_task_id") or "").strip()
            if not task_id:
                continue
            try:
                task = await task_service.get_task(task_id)
            except Exception:  # noqa: BLE001 - el estado de ejecucion es un extra, no un requisito
                task = None
            if task:
                row["execution_status"] = task.get("status")
                row["execution_ticket_number"] = task.get("ticket_number")
        return rows

    def _not_configured() -> dict[str, Any]:
        return {"ok": False, "status": "not_configured", "message": "ERPNext no esta configurado."}

    def _classify(exc: Exception) -> dict[str, Any]:
        """Fallo -> estado clasificado: permiso, datos, tamano, disponibilidad."""
        if isinstance(exc, AttachmentRejected):
            return {"ok": False, "status": exc.status, "message": str(exc)}
        if isinstance(exc, ERPNextUnavailable):
            return {"ok": False, "status": "unavailable", "message": str(exc)}
        if isinstance(exc, ERPNextPermissionError):
            return {"ok": False, "status": "forbidden", "message": str(exc)}
        if isinstance(exc, ERPNextDuplicateError):
            return {"ok": False, "status": "duplicate", "message": str(exc)}
        if isinstance(exc, ERPNextValidationError):
            status = {404: "not_found", 413: "too_large", 400: "invalid_input"}.get(
                exc.status_code, "invalid_input"
            )
            return {"ok": False, "status": status, "message": str(exc)}
        return {"ok": False, "status": "error", "message": str(exc)}

    async def _guard(operation: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Resuelve el metodo **despues** de comprobar el adapter: `erpnext` puede ser None."""
        if erpnext is None or not erpnext.is_enabled:
            return _not_configured()
        try:
            return await getattr(erpnext, operation)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - se clasifica y se devuelve, no se propaga
            return _classify(exc)

    async def openceo_search_tickets(
        query: str | None = None,
        project: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if erpnext is None or not erpnext.is_enabled:
            return {"ok": False, "status": "not_configured", "message": "ERPNext no esta configurado."}
        try:
            rows = await erpnext.search_tickets(
                query=query, project_key=project, status=status, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - se devuelve el error, no se propaga
            return {"ok": False, "status": "error", "message": str(exc)}
        return {"ok": True, "count": len(rows), "tickets": await _enrich_with_execution(rows)}

    async def openceo_get_ticket(identifier: str, limit: int = 10) -> dict[str, Any]:
        """El ticket completo de una vez: datos, ejecucion, comentarios y adjuntos.

        Una sola lectura da todo el contexto. Estuvo separado en `openceo_get_ticket_thread`
        y se unio aqui (decision del dueño, 2026-10-01): dos tools para entender un ticket
        obligan al agente a un paso extra en cada turno.
        """
        if erpnext is None or not erpnext.is_enabled:
            return _not_configured()
        try:
            row = await erpnext.get_ticket(identifier)
            if row is None:
                return {"ok": False, "status": "not_found", "message": f"No existe el ticket {identifier}."}
            enriched = (await _enrich_with_execution([row]))[0]
            thread = await erpnext.thread_of(str(row.get("name") or ""), limit=limit)
        except Exception as exc:  # noqa: BLE001 - se clasifica y se devuelve, no se propaga
            return _classify(exc)
        return {"ok": True, "ticket": enriched, **thread}

    def _map_priority(priority: str | None) -> TaskPriority:
        try:
            return TaskPriority(priority.strip().lower()) if priority else TaskPriority.MEDIUM
        except ValueError:
            return TaskPriority.MEDIUM

    def _map_source(source: str | None) -> TaskSource:
        try:
            return TaskSource(source.strip().lower()) if source else TaskSource.API
        except ValueError:
            return TaskSource.API

    def _build_params(ticket_type: str | None, due_date: str | None, external_url: str | None, status: str | None) -> dict[str, Any] | None:
        params: dict[str, Any] = {}
        if ticket_type:
            params["ticket_type"] = ticket_type
        if due_date:
            params["due_date"] = due_date
        if external_url:
            params["external_url"] = external_url
        if status:
            params["initial_status"] = status
        return params if params else None

    async def openceo_create_ticket(
        title: str,
        description: str = "",
        project: str | None = None,
        priority: Literal["low", "medium", "high", "critical"] | None = None,
        status: Literal["queued", "in_progress", "completed", "failed", "escalated", "interrupted", "waiting"] | None = None,
        ticket_type: str | None = None,
        source: Literal["jira", "notion", "github", "telegram", "voice", "whatsapp", "api", "entrevista", "spillover"] | None = None,
        source_id: str | None = None,
        tags: list[str] | None = None,
        due_date: str | None = None,
        external_url: str | None = None,
        coding_tool: Literal["codex", "claude", "glm", "gemini", "opencode", "kimi", "tier_s"] | None = None,
        coding_tier: Literal["tier1", "tier2", "tier3", "tier4"] | None = None,
        autorun: bool = False,
    ) -> dict[str, Any]:
        req = TaskCreateRequest(
            title=title,
            description=description,
            project=project,
            priority=_map_priority(priority),
            source=_map_source(source),
            source_id=source_id,
            tags=tags,
            params=_build_params(ticket_type, due_date, external_url, status),
            coding_tool=CodingTool(coding_tool) if coding_tool else None,
            coding_tier=CodingTier(coding_tier) if coding_tier else None,
            autorun=autorun,
        )
        try:
            result = await task_service.create_task(req)
            return {"ok": True, "task_id": result.get("task_id"), "task": result}
        except Exception as exc:
            return {"ok": False, "status": "error", "message": str(exc)}

    async def openceo_create_tickets_batch(tickets: str, autorun: bool = False) -> dict[str, Any]:
        try:
            parsed = json.loads(tickets)
        except json.JSONDecodeError as exc:
            return {"ok": False, "status": "invalid_input", "message": f"tickets no es JSON valido: {exc}"}
        if not isinstance(parsed, list):
            return {"ok": False, "status": "invalid_input", "message": "Se espera JSON array."}

        results: list[dict[str, Any]] = []
        created = 0
        failed = 0
        for item in parsed:
            if not isinstance(item, dict) or not item.get("title"):
                failed += 1
                results.append({"ok": False, "status": "invalid", "message": "Falta title"})
                continue
            try:
                req = TaskCreateRequest(
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    project=item.get("project"),
                    priority=_map_priority(item.get("priority")),
                    source=_map_source(item.get("source")),
                    source_id=item.get("source_id"),
                    tags=item.get("tags"),
                    params=_build_params(
                        item.get("ticket_type"),
                        item.get("due_date"),
                        item.get("external_url"),
                        item.get("status"),
                    ),
                    autorun=autorun,
                )
                result = await task_service.create_task(req)
                created += 1
                results.append({"ok": True, "task_id": result.get("task_id"), "task": result})
            except Exception as exc:
                failed += 1
                results.append({"ok": False, "status": "error", "message": str(exc)})
        return {"ok": True, "created_count": created, "failed_count": failed, "results": results}

    async def openceo_create_pm_tickets_batch(
        project: str,
        source_id: str,
        tickets: str,
        source: Literal["jira", "notion", "github", "telegram", "voice", "whatsapp", "api", "entrevista", "spillover"] = "spillover",
        autorun: bool = False,
    ) -> dict[str, Any]:
        normalized_project = str(project or "").strip()
        normalized_source_id = str(source_id or "").strip()
        normalized_source = str(source or "").strip() or "spillover"
        if not normalized_project:
            return {"ok": False, "status": "invalid_input", "message": "project es obligatorio."}
        if not normalized_source_id:
            return {"ok": False, "status": "invalid_input", "message": "source_id es obligatorio."}

        # Anti-loop: el padre no puede ser ya un spillover
        parent = await task_service.get_task(normalized_source_id)
        if parent:
            parent_tags = parent.get("tags") or []
            if any(t.startswith("spillover_from:") for t in parent_tags):
                return {
                    "ok": False,
                    "status": "anti_loop",
                    "message": f"Ticket {normalized_source_id} ya es un spillover. No se permite nested spillover.",
                }

        try:
            parsed = json.loads(tickets)
        except json.JSONDecodeError as exc:
            return {"ok": False, "status": "invalid_input", "message": f"tickets no es JSON valido: {exc}"}
        if not isinstance(parsed, list):
            return {"ok": False, "status": "invalid_input", "message": "Se espera JSON array."}

        mapped_source = _map_source(normalized_source)
        results: list[dict[str, Any]] = []
        created = 0
        failed = 0
        for item in parsed:
            if not isinstance(item, dict):
                failed += 1
                results.append({"ok": False, "status": "invalid", "message": "Item no es dict"})
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                failed += 1
                results.append({"ok": False, "status": "invalid", "message": "Falta title"})
                continue
            try:
                child_tags = list(item.get("tags") or [])
                spillover_tag = f"spillover_from:{normalized_source_id}"
                if spillover_tag not in child_tags:
                    child_tags.append(spillover_tag)
                req = TaskCreateRequest(
                    title=title,
                    description=item.get("description", ""),
                    project=normalized_project,
                    priority=_map_priority(item.get("priority")),
                    source=mapped_source,
                    source_id=normalized_source_id,
                    tags=child_tags if child_tags else None,
                    params=_build_params(
                        item.get("ticket_type"),
                        item.get("due_date"),
                        item.get("external_url"),
                        item.get("status"),
                    ),
                    autorun=autorun,
                )
                result = await task_service.create_task(req)
                created += 1
                results.append({"ok": True, "task_id": result.get("task_id"), "task": result})
            except Exception as exc:
                failed += 1
                results.append({"ok": False, "status": "error", "message": str(exc)})
        return {"ok": True, "created_count": created, "failed_count": failed, "results": results}

    server.tool(
        openceo_create_ticket,
        name="openceo_create_ticket",
        description="Create a ticket in OpenCEO. It is registered in ERPNext (the business record) and in MongoDB (the execution record). Required: title. Optional: description, project, priority (low/medium/high/critical), status (queued/in_progress/completed/failed/escalated/interrupted/waiting), ticket_type, source, source_id, tags, due_date, external_url, coding_tool, coding_tier, autorun (default false). Set autorun=true ONLY when the ticket must start executing now; by default it is only registered. Before creating, consider openceo_search_tickets to avoid duplicating existing work.",
    )
    server.tool(
        openceo_create_tickets_batch,
        name="openceo_create_tickets_batch",
        description='Batch-create tickets. Input: JSON array of objects with at least "title". Max 50. Registered but NOT executed unless autorun=true.',
    )
    server.tool(
        openceo_create_pm_tickets_batch,
        name="openceo_create_pm_tickets_batch",
        description='Create child tickets (spillover/interview) from a parent ticket. Forces project/source/source_id across the batch. Required: project, source_id, tickets (JSON array with title and optional description, priority, tags, ticket_type, due_date, external_url). Optional: source (default "spillover"), autorun (default false).',
    )
    server.tool(
        openceo_search_tickets,
        name="openceo_search_tickets",
        description="Search business tickets in ERPNext before creating new ones. Returns ERPNext tickets (name, subject, status, priority, project, openceo_ref) plus execution_status from the runtime when the ticket has been started. Optional filters: query (substring of the subject), project (project_key, e.g. 'jardal-openceo'), status (Open/Working/Pending Review/Completed/Cancelled), limit (default 20).",
    )
    server.tool(
        openceo_get_ticket,
        name="openceo_get_ticket",
        description="Read one ticket in full: fields, execution status, comments (newest first) and attachments with download URLs. Accepts the ERPNext name (e.g. 'TASK-2026-00025') or the OpenCEO ref.",
    )
    # --- hilo del ticket: edicion, comentarios y adjuntos (TASK-2026-00079) ----
    #
    # Descripciones cortas a proposito: cada palabra viaja en **cada** peticion de la
    # sesion de codigo (regla de `docs/ARCHITECTURE_REFERENCE_2026.md`), y el esquema de
    # argumentos ya documenta la forma.

    async def openceo_update_ticket(
        ticket: str,
        subject: str | None = None,
        description: str | None = None,
        priority: Literal["low", "medium", "high", "critical"] | None = None,
        due_date: str | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            "update_ticket",
            ticket,
            subject=subject,
            description=description,
            priority=priority,
            due_date=due_date,
        )

    async def openceo_add_ticket_comment(ticket: str, content: str) -> dict[str, Any]:
        return await _guard("add_comment", ticket, content)

    async def openceo_update_ticket_comment(comment_id: str, content: str) -> dict[str, Any]:
        return await _guard("update_comment", comment_id, content)

    async def openceo_delete_ticket_comment(comment_id: str, ticket: str) -> dict[str, Any]:
        return await _guard("delete_comment", comment_id, ticket)

    async def openceo_attach_ticket_file(
        ticket: str,
        source_path: str | None = None,
        content_base64: str | None = None,
        file_name: str | None = None,
        private: bool = True,
    ) -> dict[str, Any]:
        return await _guard(
            "attach_file",
            ticket,
            source_path=source_path,
            content_base64=content_base64,
            file_name=file_name,
            private=private,
        )

    async def openceo_get_ticket_attachment(attachment: str, include_content: bool = False) -> dict[str, Any]:
        return await _guard("get_attachment", attachment, include_content=include_content)

    async def openceo_update_ticket_attachment(
        attachment: str,
        ticket: str,
        file_name: str | None = None,
        private: bool | None = None,
    ) -> dict[str, Any]:
        return await _guard(
            "update_attachment",
            attachment,
            ticket,
            file_name=file_name,
            private=private,
        )

    async def openceo_delete_ticket_attachment(attachment: str, ticket: str) -> dict[str, Any]:
        return await _guard("delete_attachment", attachment, ticket)

    server.tool(
        openceo_update_ticket,
        name="openceo_update_ticket",
        description="Edit a ticket's subject, description (plain text or HTML), priority (low/medium/high/critical) or due_date. Only the given fields change. Status, dependencies and execution are untouched.",
    )
    server.tool(
        openceo_add_ticket_comment,
        name="openceo_add_ticket_comment",
        description="Comment on a ticket; it appears in the ERPNext timeline. Idempotent: identical text from this agent is not posted twice.",
    )
    server.tool(
        openceo_update_ticket_comment,
        name="openceo_update_ticket_comment",
        description="Edit any comment by its id, whoever wrote it.",
    )
    server.tool(
        openceo_delete_ticket_comment,
        name="openceo_delete_ticket_comment",
        description="Delete a comment. Requires the id and its ticket.",
    )
    server.tool(
        openceo_attach_ticket_file,
        name="openceo_attach_ticket_file",
        description="Attach a file to a ticket. Give exactly one of source_path (local file inside the allowed root) or content_base64. Private by default, deduplicated by content, name sanitized.",
    )
    server.tool(
        openceo_get_ticket_attachment,
        name="openceo_get_ticket_attachment",
        description="Read one attachment by its id: metadata and authenticated download_url. include_content=true also returns text or base64 when it fits in 256 KiB.",
    )
    server.tool(
        openceo_update_ticket_attachment,
        name="openceo_update_ticket_attachment",
        description="Rename an attachment (file_name) or change its privacy (private). Requires the id and its ticket.",
    )
    server.tool(
        openceo_delete_ticket_attachment,
        name="openceo_delete_ticket_attachment",
        description="Delete an attachment. Requires the id and its ticket.",
    )
    return (
        "openceo_create_ticket",
        "openceo_create_tickets_batch",
        "openceo_create_pm_tickets_batch",
        "openceo_search_tickets",
        "openceo_get_ticket",
        "openceo_update_ticket",
        "openceo_add_ticket_comment",
        "openceo_update_ticket_comment",
        "openceo_delete_ticket_comment",
        "openceo_attach_ticket_file",
        "openceo_get_ticket_attachment",
        "openceo_update_ticket_attachment",
        "openceo_delete_ticket_attachment",
    )


def _register_all_tools(
    server: FastMCP,
    settings: Settings,
    task_service: "TaskService",
    erpnext: Any | None = None,
) -> tuple[str, ...]:
    """Internal MCP: infra + OpenCEO-specific tools."""
    infra = _register_infra_tools(server, settings)
    openceo = _register_openceo_tools(server, settings, task_service, erpnext)
    return infra + openceo


def _normalize_path(path: str) -> str:
    value = (path or _DEFAULT_MCP_PATH).strip() or _DEFAULT_MCP_PATH
    if not value.startswith("/"):
        value = f"/{value}"
    value = value.rstrip("/")
    return value or _DEFAULT_MCP_PATH


def _normalize_transport(transport: str) -> str:
    value = (transport or "streamable-http").strip().lower() or "streamable-http"
    if value not in _SUPPORTED_TRANSPORTS:
        raise ValueError(f"MCP_SERVER_TRANSPORT no soportado: {transport}")
    return value


def _resolve_bearer_token(settings: Settings) -> str | None:
    token = settings.MCP_SERVER_API_KEY.strip()
    return token or None


def _build_internal_url(settings: Settings, path: str) -> str:
    host = _format_host_for_url(settings.MCP_SERVER_INTERNAL_HOST.strip() or "127.0.0.1")
    return f"http://{host}:{settings.OPENCEO_PORT}{path}/"


def _format_host_for_url(host: str) -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.version == 6:
        return f"[{host}]"
    return host


def _parse_allowed_networks(raw_value: str) -> tuple[ipaddress._BaseNetwork, ...]:
    candidates = [item.strip() for item in (raw_value or _DEFAULT_ALLOWED_IPS).split(",")]
    networks: list[ipaddress._BaseNetwork] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            if "/" not in candidate:
                address = ipaddress.ip_address(candidate)
                suffix = 128 if address.version == 6 else 32
                candidate = f"{candidate}/{suffix}"
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid MCP allowed network: %s", candidate)
    if networks:
        return tuple(networks)
    return (
        ipaddress.ip_network("127.0.0.1/32"),
        ipaddress.ip_network("::1/128"),
    )


class _InternalOnlyBearerWrapper:
    def __init__(
        self,
        allowed_networks: tuple[ipaddress._BaseNetwork, ...],
        bearer_token: str | None,
    ) -> None:
        self._allowed_networks = allowed_networks
        self._bearer_token = bearer_token
        self._inner_app: Any | None = None

    def set_inner_app(self, app: Any | None) -> None:
        self._inner_app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            if self._inner_app is None:
                await _send_json_error(send, 503, "MCP interno no inicializado.")
                return
            await self._inner_app(scope, receive, send)
            return

        client_host = _extract_client_host(scope)
        if not _is_allowed_client(client_host, self._allowed_networks):
            await _send_json_error(send, 403, "MCP interno disponible solo desde loopback.")
            return

        # GET SSE streams (server→client notifications) may omit the
        # Authorization header — rely on the IP allowlist above instead.
        # POST/DELETE always require the Bearer token when one is configured.
        if self._bearer_token and scope.get("method") != "GET":
            authorization = _decode_header(scope.get("headers", []), b"authorization")
            expected = f"Bearer {self._bearer_token}"
            if authorization != expected:
                await _send_json_error(send, 401, "Bearer token inválido para MCP interno.")
                return

        if self._inner_app is None:
            await _send_json_error(send, 503, "MCP interno no inicializado.")
            return

        await self._inner_app(scope, receive, send)


def _extract_client_host(scope: dict[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        host = client[0]
        if isinstance(host, str):
            return host
    return ""


def _is_allowed_client(host: str, networks: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in networks)


def _decode_header(headers: list[tuple[bytes, bytes]], target: bytes) -> str:
    target_lower = target.lower()
    for key, value in headers:
        if key.lower() == target_lower:
            return value.decode("latin-1")
    return ""


async def _send_json_error(send: Any, status_code: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _coerce_lifespan_context(factory: Any, app: Any) -> AbstractAsyncContextManager[Any]:
    context = factory(app)
    if isinstance(context, AbstractAsyncContextManager):
        return context
    return asynccontextmanager(factory)(app)
