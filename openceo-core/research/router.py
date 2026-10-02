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
import re
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

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
from research.subscriptions import (
    ResearchSubscriptions,
    SubscriptionConflict,
    SubscriptionError,
    SubscriptionLimitExceeded,
    SubscriptionNotFound,
    SubscriptionRequest,
    SubscriptionUpdate,
    SubscriptionValidationError,
)

logger = logging.getLogger(__name__)

__all__ = ["build_research_router", "is_safe_project_key", "public_job_payload", "subscription_http_error"]

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


_SAFE_PROJECT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def is_safe_project_key(project_key: Any) -> bool:
    return isinstance(project_key, str) and bool(_SAFE_PROJECT_KEY.match(project_key))


def subscription_http_error(exc: Exception) -> HTTPException:
    """Traduce un error del servicio de suscripciones a su estado HTTP."""
    mapping = (
        (SubscriptionNotFound, status.HTTP_404_NOT_FOUND),
        (SubscriptionConflict, status.HTTP_409_CONFLICT),
        (SubscriptionValidationError, status.HTTP_422_UNPROCESSABLE_ENTITY),
        (SubscriptionLimitExceeded, status.HTTP_429_TOO_MANY_REQUESTS),
        (ResearchAccessDenied, status.HTTP_403_FORBIDDEN),
        (ResearchPolicyError, status.HTTP_403_FORBIDDEN),
        (SubscriptionError, status.HTTP_400_BAD_REQUEST),
    )
    for kind, code in mapping:
        if isinstance(exc, kind):
            return HTTPException(status_code=code, detail=str(exc))
    raise exc


def build_research_router(
    *,
    get_service: Callable[[Request], ResearchService | None],
    project_cfg_for: Callable[[Request, str], dict[str, Any]] | None = None,
    on_admitted: Callable[[str], Awaitable[None] | None] | None = None,
    get_subscriptions: Callable[[Request], ResearchSubscriptions | None] | None = None,
) -> APIRouter:
    """Construye el router de la fachada.

    ``get_service`` devuelve el servicio del proceso (o ``None`` si no esta montado);
    ``project_cfg_for`` entrega la politica del proyecto; ``on_admitted`` despierta al
    despachador sin bloquear la respuesta; ``get_subscriptions`` devuelve el servicio de
    suscripciones (503 si no esta montado).
    """
    router = APIRouter(prefix="/api/research", tags=["research"])

    def _project_cfg(request: Request, project_key: str) -> dict[str, Any]:
        if not is_safe_project_key(project_key):
            # La clave llega del cliente y el cargador la usa como nombre de carpeta: una
            # ruta ("../x") no debe llegar a el. Misma respuesta que un proyecto ajeno.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proyecto no encontrado")
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

    # ------------------------------------------------------------ suscripciones

    def _subs_or_503(request: Request) -> ResearchSubscriptions:
        subscriptions = get_subscriptions(request) if get_subscriptions is not None else None
        if subscriptions is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Servicio de suscripciones no disponible",
            )
        # Las suscripciones dependen del mismo interruptor que los jobs.
        _service_or_503(request, get_service)
        return subscriptions

    @router.post("/subscriptions")
    async def create_subscription(
        payload: SubscriptionRequest,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        user: Any = Depends(require_permissions(RESEARCH_RUN)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        if idempotency_key and idempotency_key.strip():
            payload = payload.model_copy(update={"idempotency_key": idempotency_key.strip()})
        try:
            subscription, created = await subscriptions.subscribe(
                payload, principal=principal_from_user(user),
                project_cfg=_project_cfg(request, payload.project_key),
            )
        except Exception as exc:  # noqa: BLE001 - se traduce o se relanza
            raise subscription_http_error(exc) from exc
        response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        response.headers["Location"] = f"/api/research/subscriptions/{subscription['subscription_id']}"
        return {**subscription, "created": created}

    @router.get("/subscriptions")
    async def list_subscriptions(
        request: Request,
        project_key: str | None = None,
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=100),
        user: Any = Depends(require_permissions(RESEARCH_READ)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        rows = await subscriptions.list(
            principal=principal_from_user(user), project_key=project_key,
            statuses=[status_filter] if status_filter else None, limit=limit,
        )
        return {"subscriptions": rows}

    @router.get("/subscriptions/{subscription_id}")
    async def get_subscription(
        subscription_id: str,
        request: Request,
        user: Any = Depends(require_permissions(RESEARCH_READ)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        try:
            return await subscriptions.get(subscription_id, principal=principal_from_user(user))
        except Exception as exc:  # noqa: BLE001
            raise subscription_http_error(exc) from exc

    @router.get("/subscriptions/{subscription_id}/events")
    async def list_subscription_events(
        subscription_id: str,
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=200),
        user: Any = Depends(require_permissions(RESEARCH_READ)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        try:
            events = await subscriptions.events(
                subscription_id, principal=principal_from_user(user),
                statuses=[status_filter] if status_filter else None, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise subscription_http_error(exc) from exc
        return {"events": events}

    @router.patch("/subscriptions/{subscription_id}")
    async def update_subscription(
        subscription_id: str,
        payload: SubscriptionUpdate,
        request: Request,
        user: Any = Depends(require_permissions(RESEARCH_RUN)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        try:
            return await subscriptions.update(
                subscription_id, payload, principal=principal_from_user(user),
                project_cfg_for=lambda project: _project_cfg(request, project),
            )
        except Exception as exc:  # noqa: BLE001
            raise subscription_http_error(exc) from exc

    # Detener no exige ``run_research``: quien perdio ese permiso (o ya no puede crear) debe
    # poder seguir deteniendo lo que programo. Es suficiente ser el owner con acceso de lectura.
    @router.post("/subscriptions/{subscription_id}/pause")
    async def pause_subscription(
        subscription_id: str,
        request: Request,
        user: Any = Depends(require_permissions(RESEARCH_READ)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        try:
            return await subscriptions.pause(subscription_id, principal=principal_from_user(user))
        except Exception as exc:  # noqa: BLE001
            raise subscription_http_error(exc) from exc

    @router.post("/subscriptions/{subscription_id}/resume")
    async def resume_subscription(
        subscription_id: str,
        request: Request,
        user: Any = Depends(require_permissions(RESEARCH_RUN)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        try:
            return await subscriptions.resume(
                subscription_id, principal=principal_from_user(user),
                project_cfg_for=lambda project: _project_cfg(request, project),
            )
        except Exception as exc:  # noqa: BLE001
            raise subscription_http_error(exc) from exc

    @router.post("/subscriptions/{subscription_id}/cancel")
    async def cancel_subscription(
        subscription_id: str,
        request: Request,
        user: Any = Depends(require_permissions(RESEARCH_READ)),
    ) -> dict[str, Any]:
        subscriptions = _subs_or_503(request)
        try:
            return await subscriptions.cancel(subscription_id, principal=principal_from_user(user))
        except Exception as exc:  # noqa: BLE001
            raise subscription_http_error(exc) from exc

    return router
