"""Fachada MCP de research (TASK-2026-00066).

Por que una montura propia y no las existentes
=============================================
Las monturas internas/globales de OpenCEO usan un Bearer compartido y restriccion de
loopback: eso controla el **transporte**, no identifica al owner. Publicar alli
herramientas multiowner asumiria una identidad que no existe.

Esta montura verifica una **credencial de consumidor** leida del encabezado
`Authorization` del propio transporte MCP. Ni el `owner_id` ni `_meta.sessionID` de los
argumentos participan: `_meta.sessionID` es correlacion, no autorizacion. Si no hay
encabezado (por ejemplo en un transporte en memoria) la llamada se rechaza: sin
identidad no hay autorizacion, y no se inventa una.

Las tres herramientas devuelven de inmediato el identificador; la investigacion sigue
en el servicio y el consumidor consulta su estado con `openceo_research_get`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

try:  # pragma: no cover - dependencia presente en el runtime real
    from fastmcp import FastMCP
except Exception:  # noqa: BLE001
    FastMCP = None  # type: ignore[assignment]

from research.contracts import ResearchIdempotencyConflict, ResearchJobRequest
from research.policies import ResearchAccessDenied, ResearchPolicyError, principal_from_user
from research.router import public_job_payload
from research.service import ResearchService, ResearchServiceError

from .consumers import ConsumerAuthError, ConsumerGrant, ConsumerRegistry

logger = logging.getLogger(__name__)

__all__ = ["build_research_mcp_facade", "transport_consumer_token"]

FACADE_TOOLS = (
    "openceo_research_submit",
    "openceo_research_get",
    "openceo_research_cancel",
)


def transport_consumer_token() -> str:
    """Lee la credencial del encabezado del transporte MCP.

    En un transporte sin HTTP no hay encabezado: se devuelve cadena vacia y el
    llamante rechaza. No se busca la credencial en argumentos ni en `_meta`.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers() or {}
    except Exception:  # noqa: BLE001 - transporte sin HTTP o sin contexto
        return ""
    for name, value in headers.items():
        if str(name).lower() == "authorization" and isinstance(value, str):
            if value.lower().startswith("bearer "):
                return value[7:].strip()
    return ""


def _error(reason: str, detail: str, *, status: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": {"reason": reason, "detail": detail}}
    if status is not None:
        payload["error"]["status"] = status
    return payload


def build_research_mcp_facade(
    *,
    consumers: ConsumerRegistry,
    get_service: Callable[[], ResearchService | None],
    name: str = "openceo-research",
    project_cfg_for: Callable[[str], dict[str, Any]] | None = None,
    on_admitted: Callable[[str], Awaitable[None] | None] | None = None,
):
    """Servidor MCP con las tres herramientas de la fachada."""
    if FastMCP is None:  # pragma: no cover - se comprueba en el catalogo
        raise RuntimeError("fastmcp no esta instalado; la fachada MCP no puede montarse")

    server = FastMCP(name)

    def _authenticate(operation: str) -> ConsumerGrant | dict[str, Any]:
        token = transport_consumer_token()
        if not token:
            return _error(
                "unauthenticated",
                "Falta la credencial de consumidor en el transporte "
                "(Authorization: Bearer ...). Un transporte sin identidad no autoriza.",
                status=401,
            )
        try:
            grant = consumers.authenticate(token)
        except ConsumerAuthError as exc:
            return _error(exc.reason, exc.detail, status=exc.status_code)
        try:
            grant.require(operation)
        except ConsumerAuthError as exc:
            return _error(exc.reason, exc.detail, status=exc.status_code)
        return grant

    def _service() -> ResearchService | dict[str, Any]:
        service = get_service()
        if service is None:
            return _error("unavailable", "Servicio de research no montado", status=503)
        available, reason = service.available()
        if not available:
            return _error("unavailable", reason, status=503)
        return service

    def _cfg(project_key: str) -> dict[str, Any]:
        if project_cfg_for is not None:
            return project_cfg_for(project_key)
        from config_loader import ConfigLoader
        from settings import get_settings

        return ConfigLoader(get_settings()).load_project(project_key)

    @server.tool(name="openceo_research_submit")
    async def openceo_research_submit(
        project_key: str,
        purpose: str,
        question: str,
        mode: str = "quick",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Admite una investigacion y devuelve su identificador de inmediato."""
        grant = _authenticate("submit")
        if isinstance(grant, dict):
            return grant
        try:
            grant.require("submit", project_key=project_key)
        except ConsumerAuthError as exc:
            return _error(exc.reason, exc.detail, status=exc.status_code)
        service = _service()
        if isinstance(service, dict):
            return service
        try:
            request = ResearchJobRequest(
                project_key=project_key,
                purpose=purpose,
                question=question,
                mode=mode,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 - validacion del contrato
            return _error("validation_error", f"Peticion invalida: {exc.__class__.__name__}", status=422)
        try:
            document = await service.submit(
                request,
                principal=principal_from_user(
                    {"user_id": grant.owner_id, "all_permissions": ["run_research", "read_research"],
                     "allowed_projects": sorted(grant.projects)}
                ),
                project_cfg=_cfg(project_key),
            )
        except ResearchIdempotencyConflict as exc:
            return _error("conflict", str(exc), status=409)
        except ResearchAccessDenied as exc:
            return _error("forbidden", str(exc), status=403)
        except ResearchPolicyError as exc:
            return _error("forbidden", str(exc), status=403)
        except ResearchServiceError as exc:
            return _error("unavailable", str(exc), status=503)

        created = bool(document.pop("_created", False))
        if on_admitted is not None:
            try:
                scheduled = on_admitted(document["research_id"])
                if scheduled is not None:
                    asyncio.ensure_future(scheduled)
            except Exception:  # noqa: BLE001
                logger.warning("No se pudo despertar al despachador (MCP)", exc_info=True)
        return {
            "ok": True,
            "research_id": document["research_id"],
            "status": document.get("status"),
            "created": created,
            "poll_with": "openceo_research_get",
        }

    @server.tool(name="openceo_research_get")
    async def openceo_research_get(research_id: str) -> dict[str, Any]:
        """Estado y resultado de una investigacion propia."""
        grant = _authenticate("get")
        if isinstance(grant, dict):
            return grant
        service = _service()
        if isinstance(service, dict):
            return service
        document = await service.status(research_id)
        if document is None or str(document.get("owner_id")) != str(grant.owner_id):
            # Un job ajeno no existe para este consumidor.
            return _error("not_found", "Job no encontrado", status=404)
        if not grant.allows("get", project_key=str(document.get("project_key") or "")):
            return _error("not_found", "Job no encontrado", status=404)
        return {"ok": True, "job": public_job_payload(document)}

    @server.tool(name="openceo_research_cancel")
    async def openceo_research_cancel(research_id: str) -> dict[str, Any]:
        """Cancela una investigacion propia."""
        grant = _authenticate("cancel")
        if isinstance(grant, dict):
            return grant
        service = _service()
        if isinstance(service, dict):
            return service
        document = await service.status(research_id)
        if document is None or str(document.get("owner_id")) != str(grant.owner_id):
            return _error("not_found", "Job no encontrado", status=404)
        if not grant.allows("cancel", project_key=str(document.get("project_key") or "")):
            return _error("not_found", "Job no encontrado", status=404)
        cancelled = await service.cancel(research_id)
        return {"ok": True, "job": public_job_payload(cancelled)}

    return server
