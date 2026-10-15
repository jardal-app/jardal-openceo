"""Business approvals against Jardal ERP (TASK-2026-00262)."""
from __future__ import annotations

from typing import Any

import httpx

from business.errors import BusinessValidationError
from business.http import JardalERPClient, erp_service_configured


def _approval_view(result: Any) -> dict[str, Any]:
    node = result.get("approval", result) if isinstance(result, dict) else None
    if not isinstance(node, dict) or not str(node.get("id") or "").strip():
        raise BusinessValidationError("Respuesta de aprobación inválida.", status_code=502)
    return {
        "id": str(node.get("id")),
        "status": str(node.get("status") or "pending"),
        "resource": node.get("resource") or {},
        "version": node.get("version", 0),
        "decision": node.get("decision"),
        "expires_at": node.get("expiresAt", node.get("expires_at")),
    }


class JardalERPApproval:
    provider = "jardal_erp"

    def __init__(self, settings: Any, client: JardalERPClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self.client = client or JardalERPClient(settings, transport=transport)
        self._owns_client = client is None

    @property
    def is_enabled(self) -> bool:
        return erp_service_configured(self._settings)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def request_approval(self, request: dict[str, Any], *, key: str) -> dict[str, Any]:
        if not isinstance(request, dict) or not str(request.get("resource") or "").strip():
            raise BusinessValidationError("Solicitud de aprobación incompleta.", status_code=400)
        result = await self.client.request("POST", "/api/v1/approvals", body=request, key=key)
        return _approval_view(result)

    async def get_approval(self, identifier: str) -> dict[str, Any]:
        if not str(identifier or "").strip() or len(identifier) > 128:
            raise BusinessValidationError("Identificador de aprobación inválido.", status_code=400)
        from urllib.parse import quote

        result = await self.client.request("GET", "/api/v1/approvals/" + quote(identifier, safe=""))
        return _approval_view(result)
