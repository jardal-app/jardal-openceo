"""Fachada HTTP del servicio research (TASK-2026-00066).

Reglas de autorizacion
======================
* La identidad sale de `get_current_user` (cookie de sesion) y de
  `require_permissions`: **no** hay `verify_api_key` fail-open.
* El owner del job es el del principal autenticado. Un `owner_id`, `project_key` o
  `budget_ref` del payload no crean autorizacion: solo se contrastan contra la
  politica del proyecto.
* Consultar el resultado, la evidencia o la sesion de otro owner responde **404**, no
  403: no se revela la existencia de trabajos ajenos.
* `Idempotency-Key` es un encabezado, no un campo del cuerpo, y su repeticion con un
  cuerpo distinto es **409** (conflicto), no una segunda ejecucion.
* Los errores no exponen rutas del filesystem, claves ni cuerpos de proveedor.

La respuesta es `202` con el identificador en cuanto el job esta persistido: la
fachada no mantiene abierta una peticion durante toda la investigacion.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from auth.middleware import require_permissions
from research.contracts import (
    ResearchIdempotencyConflict,
    ResearchJobRequest,
    ResearchJobStatus,
)
from research.policies import (
    RESEARCH_READ,
    RESEARCH_RUN,
    ResearchAccessDenied,
    ResearchPolicyError,
    principal_from_user,
)
from research.service import ResearchService, ResearchServiceError

logger = logging.getLogger(__name__)

__all__ = ["build_research_router", "public_job_payload"]

# Campos que la fachada publica de un job. Lista blanca: lo que no esta aqui no sale
# (ni rutas internas, ni ids de lease, ni documentos crudos de proveedor).
_PUBLIC_FIELDS = (
    "research_id",
    "status",
    "project_key",
    "owner_id",
    "purpose",
    "mode",
    "question",
    "classification",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "failure_reason",
)


def public_job_payload(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if document is None:
        return None
    payload = {key: document.get(key) for key in _PUBLIC_FIELDS if key in document}
    limits = document.get("effective_limits")
    if isinstance(limits, dict):
        payload["effective_limits"] = dict(limits)
    gaps = document.get("coverage_gaps")
    if gaps:
        payload["coverage_gaps"] = list(gaps)
    result = document.get("result")
    if isinstance(result, dict):
        payload["result"] = result
    return payload


def _service_or_503(request: Request, get_service: Callable[[Request], ResearchService | None]) -> ResearchService:
    service = get_service(request)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio de research no disponible",
        )
    available, reason = service.available()
    if not available:
        # Se explica el motivo (falta de configuracion, flag apagada) en vez de
        # devolver un 500 opaco.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=reason)
    return service


def _authorized_or_404(principal: Any, document: dict[str, Any] | None) -> dict[str, Any]:
    """Autoriza la vista de un job. Un job ajeno responde 404, no 403."""
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job no encontrado")
    if str(document.get("owner_id")) != str(principal.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job no encontrado")
    project = str(document.get("project_key") or "")
    if not principal.is_admin and "*" not in principal.allowed_projects and project not in principal.allowed_projects:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job no encontrado")
    return document


def build_research_router(
    *,
    get_service: Callable[[Request], ResearchService | None],
    project_cfg_for: Callable[[Request, str], dict[str, Any]] | None = None,
    on_admitted: Callable[[str], Awaitable[None] | None] | None = None,
) -> APIRouter:
    """Construye el router de la fachada.

    ``get_service`` devuelve el servicio del proceso (o ``None`` si no esta montado);
    ``project_cfg_for`` entrega la politica del proyecto; ``on_admitted`` despierta al
    despachador sin bloquear la respuesta.
    """
    router = APIRouter(prefix="/api/research", tags=["research"])

    def _project_cfg(request: Request, project_key: str) -> dict[str, Any]:
        if project_cfg_for is not None:
            return project_cfg_for(request, project_key)
        from config_loader import ConfigLoader
        from settings import get_settings

        return ConfigLoader(get_settings()).load_project(project_key)

    @router.post("/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def submit_job(
        payload: ResearchJobRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        user: Any = Depends(require_permissions(RESEARCH_RUN)),
    ) -> dict[str, Any]:
        service = _service_or_503(request, get_service)
        principal = principal_from_user(user)
        if idempotency_key and idempotency_key.strip():
            payload = payload.model_copy(update={"idempotency_key": idempotency_key.strip()})
        try:
            document = await service.submit(
                payload,
                principal=principal,
                project_cfg=_project_cfg(request, payload.project_key),
            )
        except ResearchIdempotencyConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ResearchAccessDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ResearchPolicyError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        except ResearchServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

        created = bool(document.pop("_created", False))
        if on_admitted is not None:
            try:
                scheduled = on_admitted(document["research_id"])
                if scheduled is not None:
                    # Se programa el despacho sin esperar: la respuesta ya puede salir.
                    import asyncio

                    asyncio.ensure_future(scheduled)
            except Exception:  # noqa: BLE001 - despertar al despachador no puede romper la admision
                logger.warning("No se pudo despertar al despachador", exc_info=True)
        response.headers["Location"] = f"/api/research/jobs/{document['research_id']}"
        return {
            "research_id": document["research_id"],
            "status": document.get("status", ResearchJobStatus.QUEUED.value),
            "created": created,
            "mode": document.get("mode"),
            "project_key": document.get("project_key"),
        }

    @router.get("/jobs/{research_id}")
    async def get_job(
        research_id: str,
        request: Request,
        user: Any = Depends(require_permissions(RESEARCH_READ)),
    ) -> dict[str, Any]:
        service = _service_or_503(request, get_service)
        principal = principal_from_user(user)
        document = _authorized_or_404(principal, await service.status(research_id))
        return public_job_payload(document) or {}

    @router.post("/jobs/{research_id}/cancel")
    async def cancel_job(
        research_id: str,
        request: Request,
        user: Any = Depends(require_permissions(RESEARCH_RUN)),
    ) -> dict[str, Any]:
        service = _service_or_503(request, get_service)
        principal = principal_from_user(user)
        _authorized_or_404(principal, await service.status(research_id))
        cancelled = await service.cancel(research_id)
        if cancelled is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job no encontrado")
        return public_job_payload(cancelled) or {}

    return router
