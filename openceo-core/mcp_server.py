from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from mcp_identity import (
    MCPIdentityMiddleware,
    McpAuthError,
    bearer_from_headers,
)
from catalog import MCP_BOUNDARY_TOOLS
from settings import Settings
from tools.cloudflare import CloudflareDNSManager
from tools.email_sender import SMTPEmailSender
from workflow_service import WorkflowToolRegistry
from tools.erpnext_client import (
    ERPNextDuplicateError,
    ERPNextPermissionError,
    ERPNextUnavailable,
    ERPNextValidationError,
)
from tools.erpnext_tickets import AttachmentRejected
from state import CodingTool, TaskCreateRequest, TaskPriority, TaskSource

logger = logging.getLogger(__name__)

try:
    from fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - validated at runtime when enabled
    FastMCP = None  # type: ignore[assignment]
    _FASTMCP_IMPORT_ERROR = exc
else:
    _FASTMCP_IMPORT_ERROR = None

_DEFAULT_MCP_PATH = "/internal/mcp"
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
    identity_enforced: bool = False

    @property
    def auth_enabled(self) -> bool:
        return bool(self.bearer_token) or self.identity_enforced

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
    issuer: Any | None = None,
    audit: Any | None = None,
) -> MountedMCPServer | None:
    """Build the shared internal MCP for managed runs and manual CLI sessions."""
    if not settings.MCP_SERVER_ENABLED:
        return None
    return _build_mcp(
        settings=settings,
        path=_normalize_path(settings.MCP_SERVER_PATH),
        name="OpenCEO Internal MCP",
        register_fn=lambda s, st: _register_all_tools(s, st, task_service, erpnext),
        stateless_http=True,
        json_response=True,
        issuer=issuer,
        audit=audit,
    )


def _build_mcp(
    settings: Settings,
    path: str,
    name: str,
    register_fn: Any,
    transport: str | None = None,
    stateless_http: bool = False,
    json_response: bool = False,
    issuer: Any | None = None,
    audit: Any | None = None,
) -> MountedMCPServer:
    if FastMCP is None:
        raise RuntimeError("MCP_SERVER_ENABLED=true pero fastmcp no está instalado.") from _FASTMCP_IMPORT_ERROR
    if (issuer is None) != (audit is None):
        raise ValueError("Ticket identity needs both issuer and audit, or neither.")

    transport = _normalize_transport(transport if transport is not None else settings.MCP_SERVER_TRANSPORT)
    allowed_networks = _parse_allowed_networks(settings.MCP_SERVER_ALLOWED_IPS)

    server = FastMCP(name)
    tool_names = register_fn(server, settings)
    unknown = sorted(set(tool_names) - MCP_BOUNDARY_TOOLS)
    if unknown:
        raise ValueError(
            f"{name} registers unknown tools: {', '.join(unknown)}")
    if issuer is not None and audit is not None:
        if settings.MCP_SERVER_API_KEY.strip():
            logger.warning(
                "%s enforces ticket tokens; the shared MCP_SERVER_API_KEY is ignored",
                name,
            )
        server.add_middleware(MCPIdentityMiddleware(issuer, audit))
        return build_mounted_mcp_server(
            path=path,
            name=name,
            server=server,
            bearer_token=None,
            allowed_networks=allowed_networks,
            transport=transport,
            tool_names=tool_names,
            internal_url=_build_internal_url(settings, path),
            stateless_http=stateless_http,
            json_response=json_response,
            authenticator=issuer,
        )
    bearer_token = settings.MCP_SERVER_API_KEY.strip() or None
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
    authenticator: Any | None = None,
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
    if bearer_token is None and authenticator is None:
        logger.warning("%s enabled without Bearer token; loopback restriction is the only control", name)
    return MountedMCPServer(
        path=_normalize_path(path),
        app=_InternalOnlyBearerWrapper(
            allowed_networks=allowed_networks,
            bearer_token=bearer_token,
            authenticator=authenticator,
        ),
        server=server,
        transport=_normalize_transport(transport),
        tool_names=tool_names,
        internal_url=internal_url,
        bearer_token=bearer_token,
        stateless_http=stateless_http,
        json_response=json_response,
        identity_enforced=authenticator is not None,
    )


def build_mcp_provider_env(settings: Settings) -> dict[str, str]:
    """Env vars injected into OpenCEO tmux sessions — points to internal MCP (all tools).

    Managed callers receive their own explicit ticket badge separately.
    """
    if not settings.MCP_SERVER_ENABLED:
        return {}

    return {
        "OPENCEO_INTERNAL_MCP_ENABLED": "true",
        "OPENCEO_INTERNAL_MCP_URL": _build_internal_url(settings, _normalize_path(settings.MCP_SERVER_PATH)),
        "OPENCEO_INTERNAL_MCP_TRANSPORT": _normalize_transport(settings.MCP_SERVER_TRANSPORT),
    }


def build_mcp_provider_config(
    settings: Settings, bearer: str | None = None,
) -> dict[str, Any] | None:
    """Serializable MCP config for headless provider CLIs such as Claude Code.

    `bearer` is an explicit caller badge; otherwise the anonymous box applies.
    """
    if not settings.MCP_SERVER_ENABLED:
        return None

    transport = _normalize_transport(settings.MCP_SERVER_TRANSPORT)
    server: dict[str, Any] = {
        "type": "sse" if transport == "sse" else "http",
        "url": _build_internal_url(settings, _normalize_path(settings.MCP_SERVER_PATH)),
    }
    if bearer:
        server["headers"] = {"Authorization": f"Bearer {bearer}"}
    return {"mcpServers": {"openceo-internal": server}}


def _register_infra_tools(server: FastMCP, settings: Settings) -> tuple[str, ...]:
    manager = CloudflareDNSManager(settings)

    def manage_dns_record(
        hostname: str, action: Literal["create", "delete"] = "create",
    ) -> dict[str, Any]:
        if action == "delete":
            return manager.delete(hostname=hostname)
        return manager.check_or_create(hostname=hostname)

    server.tool(
        manage_dns_record,
        name="manage_dns_record",
        description="DNS: create (idempotent) or delete an A record in Cloudflare. "
        "Use create before deploying with Traefik. "
        "Boundary (a): holds the Cloudflare token.",
    )
    return ("manage_dns_record",)


def _register_openceo_tools(
    server: FastMCP,
    settings: Settings,
    task_service: "TaskService",
    erpnext: Any | None = None,
) -> tuple[str, ...]:

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

    async def openceo_create_objective(
        project: str,
        goal: str,
        acceptance: list[str] | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """Top-down company objective: ERP group + planner (F2)."""
        try:
            objective = await task_service.create_objective(
                project=project,
                goal=goal,
                acceptance=acceptance,
                source="api",
                source_id=source_id,
            )
            return {"ok": True,
                    "objective_id": objective.get("objective_id"),
                    "objective": objective}
        except Exception as exc:  # noqa: BLE001 - mismo contrato que create_ticket
            return {"ok": False, "status": "error", "message": str(exc)}

    async def openceo_transition_ticket(
        ticket: str,
        to_status: Literal["Open", "Working", "Pending Review", "Completed", "Cancelled"],
        reason: str,
        human_action: str | None = None,
        evidence: str | None = None,
        owner_instruction: str | None = None,
        secret: str | None = None,
        sources_checked: list[str] | None = None,
    ) -> dict[str, Any]:
        """Explicit business-state move under the global policy (F4).

        With `secret`: missing-secret report (TASK-2026-00142) — the ticket
        parks in Working naming the exact secret, and the cockpit reads it
        as blocked, never as a decision. Only valid with `to_status` Working.
        """
        if task_service is None:
            return {"ok": False, "status": "not_configured", "message": "Sin TaskService."}
        if secret is not None or sources_checked is not None:
            if to_status != "Working":
                return {
                    "ok": False,
                    "status": "invalid_input",
                    "message": "secret solo aparca en Working: un secreto ausente "
                    "bloquea el trabajo, no lo cierra ni lo escala.",
                }
            try:
                return await task_service.report_missing_secret(
                    ticket,
                    secret or "",
                    sources_checked or [],
                    note=reason,
                )
            except Exception as exc:  # noqa: BLE001 - el servicio ya clasifica lo esperado
                return {"ok": False, "status": "error", "message": str(exc)}
        try:
            return await task_service.transition_ticket(
                ticket,
                to_status,
                reason,
                human_action=human_action,
                evidence=evidence,
                owner_instruction=owner_instruction,
            )
        except Exception as exc:  # noqa: BLE001 - el servicio ya clasifica lo esperado
            return {"ok": False, "status": "error", "message": str(exc)}

    async def openceo_transition_tickets_batch(transitions: str) -> dict[str, Any]:
        """Sequential transitions, each applied and verified by reread (F4)."""
        if task_service is None:
            return {"ok": False, "status": "not_configured", "message": "Sin TaskService."}
        try:
            parsed = json.loads(transitions)
        except json.JSONDecodeError as exc:
            return {"ok": False, "status": "invalid_input", "message": f"transitions no es JSON valido: {exc}"}
        if not isinstance(parsed, list):
            return {"ok": False, "status": "invalid_input", "message": "Se espera JSON array."}
        if len(parsed) > 50:
            return {"ok": False, "status": "invalid_input", "message": "Maximo 50 transiciones por lote."}

        results: list[dict[str, Any]] = []
        applied = 0
        escalated = 0
        failed = 0
        for item in parsed:
            if not isinstance(item, dict) or not str(item.get("ticket") or "").strip():
                failed += 1
                results.append({"ok": False, "status": "invalid", "message": "Falta ticket"})
                continue
            try:
                result = await task_service.transition_ticket(
                    str(item.get("ticket")),
                    str(item.get("to_status") or ""),
                    str(item.get("reason") or ""),
                    human_action=item.get("human_action"),
                    evidence=item.get("evidence"),
                    owner_instruction=item.get("owner_instruction"),
                )
            except Exception as exc:  # noqa: BLE001 - un item no aborta el lote
                failed += 1
                results.append({"ok": False, "status": "error", "message": str(exc)})
                continue
            results.append(result)
            if not result.get("ok"):
                failed += 1
            elif result.get("outcome") == "escalated":
                escalated += 1
            else:
                applied += 1
        return {
            "ok": True,
            "applied_count": applied,
            "escalated_count": escalated,
            "failed_count": failed,
            "results": results,
        }

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
        coding_tool: Literal["tier_s", "tier_a", "tier_b", "tier_c"] | None = None,
        autorun: bool = False,
        parent_task: str | None = None,
        depends_on: list[str] | None = None,
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
            autorun=autorun,
            parent_task=parent_task,
            depends_on=depends_on,
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
                    parent_task=item.get("parent_task"),
                    depends_on=item.get("depends_on"),
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
                    parent_task=item.get("parent_task"),
                    depends_on=item.get("depends_on"),
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
        description="Create a ticket in OpenCEO. It is registered in ERPNext (the business record) and in MongoDB (the execution record). Required: title. Optional: description, project, priority (low/medium/high/critical), status (queued/in_progress/completed/failed/escalated/interrupted/waiting), ticket_type, source, source_id, tags, due_date, external_url, coding_tool, autorun (default false), parent_task (TASK-... group), depends_on (list of TASK-...). Set autorun=true ONLY when the ticket must start executing now; by default it is only registered. Before creating, consider openceo_search_tickets to avoid duplicating existing work. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_create_tickets_batch,
        name="openceo_create_tickets_batch",
        description='Batch-create tickets. Input: JSON array of objects with at least "title" (each may also carry parent_task and depends_on with TASK-... names). Max 50. Registered but NOT executed unless autorun=true. Boundary (c): writes the business record.',
    )
    server.tool(
        openceo_create_pm_tickets_batch,
        name="openceo_create_pm_tickets_batch",
        description='Create child tickets (spillover/interview) from a parent ticket. Forces project/source/source_id across the batch. Required: project, source_id, tickets (JSON array with title and optional description, priority, tags, ticket_type, due_date, external_url, parent_task, depends_on). Optional: source (default "spillover"), autorun (default false). Owner token only. Boundary (c): writes the business record.',
    )
    server.tool(
        openceo_search_tickets,
        name="openceo_search_tickets",
        description="Search business tickets in ERPNext before creating new ones. Returns ERPNext tickets (name, subject, status, priority, project, openceo_ref, is_group, parent_task) plus execution_status from the runtime when the ticket has been started. Optional filters: query (substring of the subject), project (project_key, e.g. 'jardal-openceo'), status (Open/Working/Pending Review/Completed/Cancelled), limit (default 20). Boundary (a): reads through the ERPNext credential.",
    )
    server.tool(
        openceo_get_ticket,
        name="openceo_get_ticket",
        description="Read one ticket in full: fields, execution status, comments (newest first) and attachments with download URLs. Accepts the ERPNext name (e.g. 'TASK-2026-00025') or the OpenCEO ref. Boundary (a): reads through the ERPNext credential.",
    )
    server.tool(
        openceo_create_objective,
        name="openceo_create_objective",
        description="Create a top-down company objective: one ERPNext group Task (business identity) whose children the planner creates. Required: project, goal. Optional: acceptance (delivery criteria), source_id. Refuses duplicates and admission limits deterministically. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_transition_ticket,
        name="openceo_transition_ticket",
        description="Move a ticket's business status under the global policy: reason always, human_action for review, evidence for close, owner quote to cancel owner-set work. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_transition_tickets_batch,
        name="openceo_transition_tickets_batch",
        description="Apply several status transitions one by one, each verified by reread. Input: JSON array of {ticket, to_status, reason, ...}. Reports applied/escalated/failed counts. Boundary (c): writes the business record.",
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
        description="Edit a ticket's subject, description, priority or due_date. Only the given fields change. Never status, dependencies or execution. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_add_ticket_comment,
        name="openceo_add_ticket_comment",
        description="Comment on a ticket; it appears in the ERPNext timeline. Idempotent: identical text from this agent is not posted twice. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_update_ticket_comment,
        name="openceo_update_ticket_comment",
        description="Edit any comment by its id, whoever wrote it. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_delete_ticket_comment,
        name="openceo_delete_ticket_comment",
        description="Delete a comment. Requires the id and its ticket. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_attach_ticket_file,
        name="openceo_attach_ticket_file",
        description="Attach a file to a ticket. Give exactly one of source_path (local file inside the allowed root) or content_base64. Private by default, deduplicated by content. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_get_ticket_attachment,
        name="openceo_get_ticket_attachment",
        description="Read one attachment by its id: metadata and authenticated download_url. include_content=true also returns text or base64 when it fits in 256 KiB. Boundary (a): reads through the ERPNext credential.",
    )
    server.tool(
        openceo_update_ticket_attachment,
        name="openceo_update_ticket_attachment",
        description="Rename an attachment (file_name) or change its privacy (private). Requires the id and its ticket. Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_delete_ticket_attachment,
        name="openceo_delete_ticket_attachment",
        description="Delete an attachment. Requires the id and its ticket. Boundary (c): writes the business record.",
    )

    # --- boundary tools (TASK-2026-00199) ---------------------------------
    # One implementation, two access paths: the YAML registry owns the logic
    # and these wrappers expose it to agents. Each description documents the
    # criterion that justifies it: (a) secret, (b) lock, (c) business record.
    boundary = WorkflowToolRegistry(settings=settings)

    def _senders_text() -> str:
        try:
            accounts = SMTPEmailSender(settings).list_accounts()
        except Exception as exc:  # noqa: BLE001 - config surface varies; logged, then fallback
            logger.warning("SMTP sender list unavailable: %s", exc)
            return "no sender account configured"
        emails = [str(item.get("email") or "") for item in accounts
                  if str(item.get("email") or "").strip()]
        return ", ".join(emails) if emails else "no sender account configured"

    async def openceo_send_email(
        to: str | list[str],
        subject: str,
        body_text: str,
        from_email: str | None = None,
        body_html: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
    ) -> dict[str, Any]:
        from agents.secretary.tools import send_email
        try:
            # Sync SMTP leaf: off the event loop on purpose.
            return await asyncio.to_thread(
                send_email,
                from_email=from_email,
                to=to,
                subject=subject,
                body_text=body_text,
                body_html=body_html,
                cc=cc,
                bcc=bcc,
            )
        except Exception as exc:  # noqa: BLE001 - classified and returned, never raised
            return _classify(exc)

    async def openceo_ensure_erpnext_project(
        project_key: str, display_name: str = "",
    ) -> dict[str, Any]:
        if erpnext is None or not erpnext.is_enabled:
            return _not_configured()
        try:
            from ops.create_project import normalize_key
            key = normalize_key(project_key)
            existing = await erpnext.find_project(key)
            name = await erpnext.ensure_project(
                key, (display_name or "").strip() or None)
            return {"name": name, "created": not existing}
        except Exception as exc:  # noqa: BLE001 - classified and returned, never raised
            return _classify(exc)

    def _money_ref(openceo_ref: str | None) -> str | None:
        # Money over MCP has no task context to derive a stable ref from, so
        # the caller supplies one: without it a retried call would double-post
        # (review follow-up O1).
        ref = (openceo_ref or "").strip()
        return ref or None

    async def openceo_register_expense(
        text: str = "",
        openceo_ref: str | None = None,
        submit: bool = True,
        exchange_rate: str | None = None,
    ) -> dict[str, Any]:
        ref = _money_ref(openceo_ref)
        if ref is None:
            return {"ok": False, "status": "invalid_input",
                    "message": "openceo_ref is required: a stable key you "
                    "reuse when retrying, so a retry never double-posts."}
        try:
            return await boundary.call("finance.register_expense", {
                "text": text,
                "openceo_ref": ref,
                "submit": submit,
                "exchange_rate": exchange_rate,
            }, {})
        except Exception as exc:  # noqa: BLE001 - classified and returned, never raised
            return _classify(exc)

    async def openceo_register_income(
        text: str = "",
        openceo_ref: str | None = None,
        submit: bool = True,
        exchange_rate: str | None = None,
    ) -> dict[str, Any]:
        ref = _money_ref(openceo_ref)
        if ref is None:
            return {"ok": False, "status": "invalid_input",
                    "message": "openceo_ref is required: a stable key you "
                    "reuse when retrying, so a retry never double-posts."}
        try:
            return await boundary.call("finance.register_income", {
                "text": text,
                "openceo_ref": ref,
                "submit": submit,
                "exchange_rate": exchange_rate,
            }, {})
        except Exception as exc:  # noqa: BLE001 - classified and returned, never raised
            return _classify(exc)

    async def openceo_scope_summary(
        cost_center: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await boundary.call("finance.get_scope_summary", {
                "cost_center": cost_center,
                "date_from": date_from,
                "date_to": date_to,
            }, {})
        except Exception as exc:  # noqa: BLE001 - classified and returned, never raised
            return _classify(exc)

    async def openceo_contribution_10_status(
        date_from: str | None = None,
        date_to: str | None = None,
        cost_center: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await boundary.call("finance.get_contribution_10_status", {
                "date_from": date_from,
                "date_to": date_to,
                "cost_center": cost_center,
            }, {})
        except Exception as exc:  # noqa: BLE001 - classified and returned, never raised
            return _classify(exc)

    async def openceo_elfec_house_payment(
        expected_row_label: str = "",
    ) -> dict[str, Any]:
        try:
            # Sync Playwright leaf: off the event loop on purpose.
            result = await asyncio.to_thread(
                WorkflowToolRegistry._finance_pago_elfec_house,
                expected_row_label=expected_row_label,
            )
        except Exception as exc:  # noqa: BLE001 - classified and returned, never raised
            return _classify(exc)
        if not isinstance(result, dict) or result.get("practice_mode") is not True:
            return {"ok": False, "status": "refused",
                    "message": "Elfec over MCP runs practice mode only."}
        return result

    server.tool(
        openceo_send_email,
        name="openceo_send_email",
        description="Send an operational email from one of: "
        f"{_senders_text()}. Unknown senders are rejected. "
        "Boundary (a): holds the SMTP credentials.",
    )
    server.tool(
        openceo_ensure_erpnext_project,
        name="openceo_ensure_erpnext_project",
        description="Ensure the ERPNext Project identity (idempotent by project_key). "
        "Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_register_expense,
        name="openceo_register_expense",
        description="Register an occasional expense in ERPNext from free text "
        "(Journal Entry; openceo_ref is required and makes retries idempotent). "
        "Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_register_income,
        name="openceo_register_income",
        description="Register collected income in ERPNext from free text "
        "(Journal Entry; openceo_ref is required and makes retries idempotent). "
        "Boundary (c): writes the business record.",
    )
    server.tool(
        openceo_scope_summary,
        name="openceo_scope_summary",
        description="Balance/expense summary by scope from the ledger (read-only). "
        "Boundary (a): reads through the ERPNext credential.",
    )
    server.tool(
        openceo_contribution_10_status,
        name="openceo_contribution_10_status",
        description="10% contribution rule status from the ledger (read-only). "
        "Boundary (a): reads through the ERPNext credential.",
    )
    server.tool(
        openceo_elfec_house_payment,
        name="openceo_elfec_house_payment",
        description="Elfec house payment in practice mode: drives the portal to "
        "the card form and stops, never pays. "
        "Boundary (a): holds the Elfec credentials.",
    )
    return (
        "openceo_create_ticket",
        "openceo_create_tickets_batch",
        "openceo_create_pm_tickets_batch",
        "openceo_search_tickets",
        "openceo_get_ticket",
        "openceo_create_objective",
        "openceo_transition_ticket",
        "openceo_transition_tickets_batch",
        "openceo_update_ticket",
        "openceo_add_ticket_comment",
        "openceo_update_ticket_comment",
        "openceo_delete_ticket_comment",
        "openceo_attach_ticket_file",
        "openceo_get_ticket_attachment",
        "openceo_update_ticket_attachment",
        "openceo_delete_ticket_attachment",
        "openceo_send_email",
        "openceo_ensure_erpnext_project",
        "openceo_register_expense",
        "openceo_register_income",
        "openceo_scope_summary",
        "openceo_contribution_10_status",
        "openceo_elfec_house_payment",
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
        authenticator: Any | None = None,
    ) -> None:
        self._allowed_networks = allowed_networks
        self._bearer_token = bearer_token
        self._authenticator = authenticator
        self._inner_app: Any | None = None

    def set_inner_app(self, app: Any | None) -> None:
        self._inner_app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type != "http" and scope_type != "lifespan":
            logger.warning("Rejecting non-HTTP MCP scope: %s", scope_type)
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 4401})
            else:
                await _send_json_error(send, 403, "MCP interno solo admite HTTP.")
            return
        if scope_type != "http":
            if self._inner_app is None:
                await _send_json_error(send, 503, "MCP interno no inicializado.")
                return
            await self._inner_app(scope, receive, send)
            return

        client_host = _extract_client_host(scope)
        if not _is_allowed_client(client_host, self._allowed_networks):
            await _send_json_error(send, 403, "MCP interno disponible solo desde loopback.")
            return

        # Ticket identity mounts authenticate every presented badge,
        # streams included. Calls with no badge pass to the default box
        # (Owner order); a present but invalid badge still fails closed.
        # Legacy mounts only: GET SSE streams may omit the
        # header and rely on the IP allowlist instead.
        if self._authenticator is not None:
            authorization = _decode_header(scope.get("headers", []), b"authorization")
            token = bearer_from_headers({"authorization": authorization})
            if token is not None:
                try:
                    await self._authenticator.authenticate(token)
                except McpAuthError as exc:
                    logger.warning(
                        "MCP %s %s denied for %s (%s)",
                        scope.get("method"), scope.get("path"),
                        client_host or "unknown", exc.reason,
                    )
                    await _send_json_error(send, 401, "MCP ticket token invalid.")
                    return
        elif self._bearer_token and scope.get("method") != "GET":
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
