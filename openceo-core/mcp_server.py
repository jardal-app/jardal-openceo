from __future__ import annotations

import ipaddress
import json
import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from settings import Settings
from tools.cloudflare import CloudflareDNSManager
from state import TaskCreateRequest, TaskPriority, TaskSource

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

    @property
    def auth_enabled(self) -> bool:
        return bool(self.bearer_token)

    @asynccontextmanager
    async def lifespan(self):
        raw_app = self.server.http_app(path="/", transport=self.transport, stateless_http=self.stateless_http)
        self.app.set_inner_app(raw_app)
        try:
            async with _coerce_lifespan_context(raw_app.lifespan, raw_app):
                yield
        finally:
            self.app.set_inner_app(None)


def build_internal_mcp_mount(settings: Settings, task_service: "TaskService") -> MountedMCPServer | None:
    """Build the internal MCP — all tools (infra + OpenCEO-specific). Used by OpenCEO tmux sessions."""
    if not settings.MCP_SERVER_ENABLED:
        return None
    return _build_mcp(
        settings=settings,
        path=_normalize_path(settings.MCP_SERVER_PATH),
        name="OpenCEO Internal MCP",
        register_fn=lambda s, st: _register_all_tools(s, st, task_service),
    )


def build_global_mcp_mount(settings: Settings) -> MountedMCPServer | None:
    """Build the global MCP — infra tools only (DNS). Used by manual Claude/Codex on any project."""
    if not settings.MCP_SERVER_ENABLED:
        return None
    return _build_mcp(
        settings=settings,
        path=_normalize_path(settings.MCP_GLOBAL_PATH),
        name="OpenCEO Global MCP",
        register_fn=_register_infra_tools,
        transport="streamable-http",
        stateless_http=True,
        json_response=True,  # Enable JSON responses for Codex compatibility
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

    server = FastMCP(name, json_response=json_response)
    tool_names = register_fn(server, settings)

    if bearer_token is None:
        logger.warning("%s enabled without Bearer token; loopback restriction is the only control", name)

    protected_app = _InternalOnlyBearerWrapper(
        allowed_networks=allowed_networks,
        bearer_token=bearer_token,
    )
    return MountedMCPServer(
        path=path,
        app=protected_app,
        server=server,
        transport=transport,
        tool_names=tool_names,
        internal_url=_build_internal_url(settings, path),
        bearer_token=bearer_token,
        stateless_http=stateless_http,
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
        description="DNS: crea/verifica A record en Cloudflare. Usar antes de deploy con Traefik.",
    )
    return ("manage_dns_check_or_create",)


def _register_openceo_tools(server: FastMCP, settings: Settings, task_service: "TaskService") -> tuple[str, ...]:
    """OpenCEO-specific tools (ticket CRUD via MongoDB). Never exposed globally."""

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
        priority: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        tags: list[str] | None = None,
        due_date: str | None = None,
        external_url: str | None = None,
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
        )
        try:
            result = await task_service.create_task(req)
            return {"ok": True, "task_id": result.get("task_id"), "task": result}
        except Exception as exc:
            return {"ok": False, "status": "error", "message": str(exc)}

    async def openceo_create_tickets_batch(tickets: str) -> dict[str, Any]:
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
        source: str = "spillover",
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
        description="Crea un ticket en OpenCEO (MongoDB). El sync a Notion se dispara automaticamente. Requiere: title. Opcionales: description, project, priority, status, ticket_type, source, source_id, tags, due_date, external_url.",
    )
    server.tool(
        openceo_create_tickets_batch,
        name="openceo_create_tickets_batch",
        description='Crea varios tickets en OpenCEO en un lote. Entrada: JSON array de objetos con al menos "title". Max 50.',
    )
    server.tool(
        openceo_create_pm_tickets_batch,
        name="openceo_create_pm_tickets_batch",
        description='Crea tickets derivados (spillover/entrevista) desde un ticket padre. Fuerza project/source/source_id para todo el lote. Requiere: project, source_id, tickets (JSON array con title y opcionales description, priority, tags, ticket_type, due_date, external_url).',
    )
    return ("openceo_create_ticket", "openceo_create_tickets_batch", "openceo_create_pm_tickets_batch")


def _register_all_tools(server: FastMCP, settings: Settings, task_service: "TaskService") -> tuple[str, ...]:
    """Internal MCP: infra + OpenCEO-specific tools."""
    infra = _register_infra_tools(server, settings)
    openceo = _register_openceo_tools(server, settings, task_service)
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

        if self._bearer_token:
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
