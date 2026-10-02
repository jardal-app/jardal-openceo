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
from research.policies import Principal
from research.router import is_safe_project_key, public_job_payload
from research.service import ResearchService, ResearchServiceError
from research.subscriptions import (
    ResearchSubscriptions,
    SubscriptionConflict,
    SubscriptionLimitExceeded,
    SubscriptionNotFound,
    SubscriptionRequest,
    SubscriptionUpdate,
    SubscriptionValidationError,
)

from .consumers import ConsumerAuthError, ConsumerGrant, ConsumerRegistry

logger = logging.getLogger(__name__)

__all__ = ["build_research_mcp_facade", "transport_consumer_token"]

FACADE_TOOLS = (
    "openceo_research_submit",
    "openceo_research_get",
    "openceo_research_cancel",
    "openceo_research_subscribe",
    "openceo_research_subscription_list",
    "openceo_research_subscription_get",
    "openceo_research_subscription_events",
    "openceo_research_subscription_update",
    "openceo_research_subscription_control",
)


def _http_headers() -> dict[str, str]:
    """Encabezados del transporte HTTP, **incluida** `Authorization`.

    `fastmcp` >= 3 excluye `authorization` del valor por defecto de
    `get_http_headers()` (lo trata como encabezado de proxy para no reenviarlo). Con
    el defecto, esta fachada rechazaba con `unauthenticated` **todas** las llamadas de
    un consumidor legitimo: la credencial viaja en ese encabezado por contrato, asi
    que hay que pedirlo de forma explicita. Se prueban las tres formas porque la firma
    cambio entre 2.x y 3.x.
    """
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:  # noqa: BLE001 - sin fastmcp no hay transporte MCP
        return {}
    for kwargs in ({"include": {"authorization"}}, {"include_all": True}, {}):
        try:
            headers = get_http_headers(**kwargs) or {}
        except TypeError:  # firma distinta en esta version
            continue
        if headers:
            return {str(k).lower(): v for k, v in headers.items()}
    # Ultimo recurso: la peticion HTTP cruda, que nunca filtra encabezados.
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        return {str(k).lower(): v for k, v in request.headers.items()}
    except Exception:  # noqa: BLE001 - transporte sin HTTP o sin contexto
        return {}


def transport_consumer_token() -> str:
    """Lee la credencial del encabezado del transporte MCP.

    En un transporte sin HTTP no hay encabezado: se devuelve cadena vacia y el
    llamante rechaza. No se busca la credencial en argumentos ni en `_meta`.
    """
    value = _http_headers().get("authorization")
    if isinstance(value, str) and value.lower().startswith("bearer "):
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
    get_subscriptions: Callable[[], ResearchSubscriptions | None] | None = None,
):
    """Servidor MCP con las herramientas de la fachada (jobs y suscripciones)."""
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
        if not is_safe_project_key(project_key):
            # Misma respuesta que un proyecto ajeno; la clave nunca llega al cargador.
            raise ResearchAccessDenied("Proyecto no autorizado")
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
        seed_urls: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Admite una investigacion y devuelve su identificador de inmediato.

        `seed_urls` son fuentes conocidas que el consumidor ya tiene. Sin buscador
        configurado (sin clave de Brave/Serper) son la **unica** via de adquisicion: sin
        ellas la peticion publica no tendria evidencia que leer y el informe no podria
        apoyarse en nada. No son identidad ni alcance, asi que no relajan la autorizacion.
        """
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
                seed_urls=[url for url in (seed_urls or []) if isinstance(url, str)],
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

    # ------------------------------------------------------------ suscripciones

    def _subscriptions() -> ResearchSubscriptions | dict[str, Any]:
        subscriptions = get_subscriptions() if get_subscriptions is not None else None
        if subscriptions is None:
            return _error("unavailable", "Servicio de suscripciones no montado", status=503)
        service = _service()
        if isinstance(service, dict):
            return service
        return subscriptions

    def _subscription_error(exc: Exception) -> dict[str, Any]:
        table = (
            (SubscriptionNotFound, "not_found", 404),
            (SubscriptionConflict, "conflict", 409),
            (SubscriptionValidationError, "validation_error", 422),
            (SubscriptionLimitExceeded, "limit_exceeded", 429),
            (ResearchAccessDenied, "forbidden", 403),
            (ResearchPolicyError, "forbidden", 403),
        )
        for kind, reason, code in table:
            if isinstance(exc, kind):
                return _error(reason, str(exc), status=code)
        raise exc

    def _grant_principal(grant: ConsumerGrant) -> Principal:
        # Leer, pausar y cancelar solo exigen ser el owner: detener trabajo recurrente
        # nunca debe depender de que la cuenta siga autorizada para crearlo.
        return Principal(user_id=grant.owner_id, allowed_projects=frozenset(grant.projects))

    def _visible(grant: ConsumerGrant, operation: str, view: dict[str, Any] | None) -> bool:
        return view is not None and grant.allows(operation, project_key=str(view.get("project_key") or ""))

    @server.tool(name="openceo_research_subscribe")
    async def openceo_research_subscribe(
        project_key: str,
        purpose: str,
        question: str,
        kind: str = "watch",
        source: dict[str, Any] | None = None,
        filter: dict[str, Any] | None = None,
        interval_seconds: int | None = None,
        daily_at: str | None = None,
        timezone: str = "UTC",
        backfill: bool = False,
        max_searches_per_day: int | None = None,
        count: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Crea una suscripcion recurrente. No gasta ni lee la red al crearla.

        `kind=watch` vigila `source` (`{"type": "rss", "url": ...}` o
        `{"type": "greenhouse"|"lever", "board": ...}`) y admite una investigacion solo si
        hay items nuevos o cambiados que pasen `filter` (`include_terms`/`exclude_terms`).
        `kind=discovery` busca fuentes nuevas para `question` con cupo diario propio. La
        primera pasada es una linea base salvo `backfill=true`. Misma identidad y mismo
        spec es idempotente; otro spec es un conflicto.
        """
        grant = _authenticate("subscribe")
        if isinstance(grant, dict):
            return grant
        try:
            grant.require("subscribe", project_key=project_key)
        except ConsumerAuthError as exc:
            return _error(exc.reason, exc.detail, status=exc.status_code)
        subscriptions = _subscriptions()
        if isinstance(subscriptions, dict):
            return subscriptions
        try:
            request = SubscriptionRequest(
                project_key=project_key, purpose=purpose, question=question, kind=kind,  # type: ignore[arg-type]
                source=source, filter=filter, interval_seconds=interval_seconds,
                daily_at=daily_at, timezone=timezone, backfill=backfill,
                max_searches_per_day=max_searches_per_day, count=count, idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 - validacion del contrato
            return _error("validation_error", f"Peticion invalida: {exc.__class__.__name__}", status=422)
        try:
            principal = await subscriptions.principal_for_consumer(grant.owner_id, grant.projects)
            subscription, created = await subscriptions.subscribe(
                request, principal=principal, project_cfg=_cfg(project_key),
                created_by_consumer=grant.consumer_id,
            )
        except Exception as exc:  # noqa: BLE001 - se traduce o se relanza
            return _subscription_error(exc)
        return {"ok": True, "created": created, "subscription": subscription}

    @server.tool(name="openceo_research_subscription_list")
    async def openceo_research_subscription_list(
        project_key: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        """Lista las suscripciones propias, opcionalmente por proyecto o estado."""
        grant = _authenticate("subscriptions")
        if isinstance(grant, dict):
            return grant
        subscriptions = _subscriptions()
        if isinstance(subscriptions, dict):
            return subscriptions
        try:
            if project_key is not None:
                grant.require("subscriptions", project_key=project_key)
            rows = await subscriptions.list(
                principal=_grant_principal(grant), project_key=project_key,
                statuses=[status] if status else None,
            )
        except ConsumerAuthError as exc:
            return _error(exc.reason, exc.detail, status=exc.status_code)
        except Exception as exc:  # noqa: BLE001
            return _subscription_error(exc)
        return {"ok": True, "subscriptions": [row for row in rows if _visible(grant, "subscriptions", row)]}

    @server.tool(name="openceo_research_subscription_get")
    async def openceo_research_subscription_get(subscription_id: str) -> dict[str, Any]:
        """Estado, cadencia, cobertura y ultimo resultado de una suscripcion propia."""
        grant = _authenticate("subscriptions")
        if isinstance(grant, dict):
            return grant
        subscriptions = _subscriptions()
        if isinstance(subscriptions, dict):
            return subscriptions
        try:
            view = await subscriptions.get(subscription_id, principal=_grant_principal(grant))
        except Exception as exc:  # noqa: BLE001
            return _subscription_error(exc)
        if not _visible(grant, "subscriptions", view):
            return _error("not_found", "Suscripcion no encontrada", status=404)
        return {"ok": True, "subscription": view}

    @server.tool(name="openceo_research_subscription_events")
    async def openceo_research_subscription_events(
        subscription_id: str, status: str | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        """Eventos detectados por una suscripcion propia (nuevos, cambiados, admitidos)."""
        grant = _authenticate("subscriptions")
        if isinstance(grant, dict):
            return grant
        subscriptions = _subscriptions()
        if isinstance(subscriptions, dict):
            return subscriptions
        try:
            view = await subscriptions.get(subscription_id, principal=_grant_principal(grant))
            if not _visible(grant, "subscriptions", view):
                return _error("not_found", "Suscripcion no encontrada", status=404)
            events = await subscriptions.events(
                subscription_id, principal=_grant_principal(grant),
                statuses=[status] if status else None, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            return _subscription_error(exc)
        return {"ok": True, "events": events}

    @server.tool(name="openceo_research_subscription_update")
    async def openceo_research_subscription_update(
        subscription_id: str,
        question: str | None = None,
        filter: dict[str, Any] | None = None,
        interval_seconds: int | None = None,
        daily_at: str | None = None,
        timezone: str | None = None,
        backfill: bool | None = None,
        max_searches_per_day: int | None = None,
        count: int | None = None,
    ) -> dict[str, Any]:
        """Edita una suscripcion propia; solo cambian los campos que se envian.

        La fuente y el proposito no se editan: para cambiarlos, cancela y crea otra.
        """
        grant = _authenticate("manage_subscription")
        if isinstance(grant, dict):
            return grant
        subscriptions = _subscriptions()
        if isinstance(subscriptions, dict):
            return subscriptions
        provided = {key: value for key, value in {
            "question": question, "filter": filter, "interval_seconds": interval_seconds,
            "daily_at": daily_at, "timezone": timezone, "backfill": backfill,
            "max_searches_per_day": max_searches_per_day, "count": count,
        }.items() if value is not None}
        try:
            update = SubscriptionUpdate(**provided)
        except Exception as exc:  # noqa: BLE001
            return _error("validation_error", f"Peticion invalida: {exc.__class__.__name__}", status=422)
        try:
            current = await subscriptions.get(subscription_id, principal=_grant_principal(grant))
            if not grant.allows("manage_subscription", project_key=str(current.get("project_key") or "")):
                return _error("not_found", "Suscripcion no encontrada", status=404)
            principal = await subscriptions.principal_for_consumer(grant.owner_id, grant.projects)
            view = await subscriptions.update(
                subscription_id, update, principal=principal, project_cfg_for=_cfg,
            )
        except Exception as exc:  # noqa: BLE001
            return _subscription_error(exc)
        return {"ok": True, "subscription": view}

    @server.tool(name="openceo_research_subscription_control")
    async def openceo_research_subscription_control(subscription_id: str, action: str) -> dict[str, Any]:
        """Pausa, reanuda o cancela una suscripcion propia (`action`: pause|resume|cancel).

        Cancelar detiene los ticks futuros y descarta lo no admitido; una investigacion ya
        admitida termina y se cancela con `openceo_research_cancel`.
        """
        grant = _authenticate("manage_subscription")
        if isinstance(grant, dict):
            return grant
        if action not in {"pause", "resume", "cancel"}:
            return _error("validation_error", "action debe ser pause, resume o cancel", status=422)
        subscriptions = _subscriptions()
        if isinstance(subscriptions, dict):
            return subscriptions
        try:
            current = await subscriptions.get(subscription_id, principal=_grant_principal(grant))
            if not grant.allows("manage_subscription", project_key=str(current.get("project_key") or "")):
                return _error("not_found", "Suscripcion no encontrada", status=404)
            if action == "pause":
                view = await subscriptions.pause(subscription_id, principal=_grant_principal(grant))
            elif action == "cancel":
                view = await subscriptions.cancel(subscription_id, principal=_grant_principal(grant))
            else:
                principal = await subscriptions.principal_for_consumer(grant.owner_id, grant.projects)
                view = await subscriptions.resume(
                    subscription_id, principal=principal, project_cfg_for=_cfg,
                )
        except Exception as exc:  # noqa: BLE001
            return _subscription_error(exc)
        return {"ok": True, "subscription": view}

    return server
