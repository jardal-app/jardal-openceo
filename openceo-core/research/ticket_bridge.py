"""Ticket bridge to the research service (TASK-2026-00222).

The catalog `research` agent holds only its ticket badge. These two calls
run server-side under a consumer the bridge mints per ticket: submit
admission (investigar) and status polling (seguimiento). The consumer
token never leaves the server; isolation between tickets comes from the
owner check on read, mirroring the public facade.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from research.consumers import ConsumerAuthError, ConsumerGrant, ConsumerRegistry
from research.contracts import ResearchIdempotencyConflict, ResearchJobRequest
from research.policies import (
    ResearchAccessDenied,
    ResearchPolicyError,
    principal_from_user,
)
from research.router import is_safe_project_key, public_job_payload
from research.service import ResearchService, ResearchServiceError

logger = logging.getLogger(__name__)

__all__ = ["TICKET_CONSUMER_OPERATIONS", "TicketResearchBridge", "ticket_owner"]

#: What a ticket consumer may do: admit (investigar) and poll (seguimiento).
TICKET_CONSUMER_OPERATIONS = ("submit", "get")


def ticket_owner(ticket: str) -> str:
    """Owner identity of a ticket consumer: the ticket owns its jobs."""
    return f"ticket:{ticket}"


def _error(reason: str, detail: str, *, status: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": {"reason": reason, "detail": detail}}
    if status is not None:
        payload["error"]["status"] = status
    return payload


class TicketResearchBridge:
    """Server-side ticket consumers over the in-process service."""

    def __init__(
        self,
        *,
        consumers: ConsumerRegistry,
        get_service: Callable[[], ResearchService | None],
        project_cfg_for: Callable[[str], dict[str, Any]] | None = None,
        on_admitted: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._consumers = consumers
        self._get_service = get_service
        self._project_cfg_for = project_cfg_for
        self._on_admitted = on_admitted
        self._tokens: dict[str, str] = {}

    def _grant(self, ticket: str, project: str) -> ConsumerGrant:
        token = self._tokens.get(ticket)
        if token is not None:
            try:
                return self._consumers.authenticate(token)
            except ConsumerAuthError as exc:
                logger.debug("Re-mint del consumidor de %s: %s", ticket, exc.detail)
        self._consumers.purge_expired()
        token = self._consumers.register(
            consumer_id=ticket_owner(ticket),
            owner_id=ticket_owner(ticket),
            projects=[project],
            operations=TICKET_CONSUMER_OPERATIONS,
        )
        self._tokens[ticket] = token
        return self._consumers.authenticate(token)

    def _service(self) -> ResearchService | dict[str, Any]:
        service = self._get_service()
        if service is None:
            return _error("unavailable", "Servicio de research no montado", status=503)
        available, reason = service.available()
        if not available:
            return _error("unavailable", reason, status=503)
        return service

    def _cfg(self, project_key: str) -> dict[str, Any]:
        if not is_safe_project_key(project_key):
            raise ResearchAccessDenied("Proyecto no autorizado")
        if self._project_cfg_for is not None:
            return self._project_cfg_for(project_key)
        from config_loader import ConfigLoader
        from settings import get_settings

        return ConfigLoader(get_settings()).load_project(project_key)

    async def submit(
        self,
        *,
        ticket: str,
        project: str,
        purpose: str,
        question: str,
        mode: str = "quick",
        seed_urls: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Admit one investigation for the ticket consumer."""
        grant = self._grant(ticket, project)
        try:
            grant.require("submit", project_key=project)
        except ConsumerAuthError as exc:
            return _error(exc.reason, exc.detail, status=exc.status_code)
        service = self._service()
        if isinstance(service, dict):
            return service
        try:
            request = ResearchJobRequest(
                project_key=project,
                purpose=purpose,
                question=question,
                mode=mode,
                seed_urls=[url for url in (seed_urls or []) if isinstance(url, str)],
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001 - validacion del contrato
            return _error("validation_error", f"Peticion invalida: {exc.__class__.__name__}",
                          status=422)
        try:
            document = await service.submit(
                request,
                principal=principal_from_user(
                    {"user_id": grant.owner_id, "all_permissions": ["run_research", "read_research"],
                     "allowed_projects": sorted(grant.projects)}
                ),
                project_cfg=self._cfg(project),
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
        if self._on_admitted is not None:
            try:
                scheduled = self._on_admitted(document["research_id"])
                if scheduled is not None:
                    import asyncio

                    asyncio.ensure_future(scheduled)
            except Exception:  # noqa: BLE001
                logger.warning("No se pudo despertar al despachador (puente)", exc_info=True)
        return {
            "ok": True,
            "research_id": document["research_id"],
            "status": document.get("status"),
            "created": created,
            "poll_with": "openceo_research_get",
        }

    async def get(self, *, ticket: str, project: str, research_id: str) -> dict[str, Any]:
        """Status and result of one of the ticket's own investigations."""
        grant = self._grant(ticket, project)
        try:
            grant.require("get", project_key=project)
        except ConsumerAuthError as exc:
            return _error(exc.reason, exc.detail, status=exc.status_code)
        service = self._service()
        if isinstance(service, dict):
            return service
        document = await service.status(research_id)
        if document is None or str(document.get("owner_id")) != str(grant.owner_id):
            return _error("not_found", "Job no encontrado", status=404)
        if not grant.allows("get", project_key=str(document.get("project_key") or "")):
            return _error("not_found", "Job no encontrado", status=404)
        return {"ok": True, "job": public_job_payload(await service.result_view(document))}
