from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import httpx

from business.errors import (
    BusinessDependencyBlockedError,
    BusinessPermissionError,
    BusinessUnavailable,
    BusinessValidationError,
    BusinessVersionConflict,
)


def erp_service_configured(settings: Any) -> bool:
    url = str(getattr(settings, "JARDAL_ERP_URL", "") or "").rstrip("/")
    token = str(getattr(settings, "JARDAL_ERP_SERVICE_TOKEN", "") or "")
    workspace = str(getattr(settings, "JARDAL_ERP_WORKSPACE_ID", "") or "")
    if not url or not token or not workspace:
        return False
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})


class JardalERPClient:
    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = str(settings.JARDAL_ERP_URL).rstrip("/")
        self._token = str(settings.JARDAL_ERP_SERVICE_TOKEN)
        self.workspace_id = str(settings.JARDAL_ERP_WORKSPACE_ID)
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(float(settings.ERPNEXT_TIMEOUT_SECONDS), connect=float(settings.ERPNEXT_CONNECT_TIMEOUT_SECONDS)),
            follow_redirects=False,
        )

    def config_error(self) -> str | None:
        if not self._url or not self._token or not self.workspace_id:
            return "JARDAL_ERP_URL, JARDAL_ERP_SERVICE_TOKEN y JARDAL_ERP_WORKSPACE_ID son obligatorios."
        url = urlsplit(self._url)
        if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            return "JARDAL_ERP_URL debe contener solo el origen, sin credenciales."
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
            return "Jardal ERP requiere HTTPS fuera de loopback."
        return None

    @property
    def is_configured(self) -> bool:
        return self.config_error() is None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, key: str | None = None,
        bearer: str | None = None, anonymous: bool = False,
    ) -> dict[str, Any]:
        if self.config_error():
            raise BusinessUnavailable(self.config_error())
        if not path.startswith("/api/v1/") or ".." in path or "?" in path or "#" in path:
            raise BusinessValidationError("Ruta ERP inválida.", status_code=400)
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise BusinessValidationError("Método ERP inválido.", status_code=400)
        if method != "GET" and not key:
            raise BusinessValidationError("La escritura ERP requiere una clave idempotente.", status_code=400)
        headers = {"X-Workspace-Id": self.workspace_id}
        if not anonymous:
            headers["Authorization"] = f"Bearer {bearer if bearer is not None else self._token}"
        if key:
            headers["Idempotency-Key"] = key
        for attempt in range(3):
            try:
                response = await self._http.request(method, self._url + path, headers=headers, json=body, params=params)
            except httpx.TransportError:
                if attempt == 2:
                    raise BusinessUnavailable("Jardal ERP no respondió; conservar la clave y reintentar.") from None
                await asyncio.sleep(0.05 * 2 ** attempt)
                continue
            if response.status_code >= 500 or response.status_code == 429:
                if attempt < 2:
                    await asyncio.sleep(0.05 * 2 ** attempt)
                    continue
                raise BusinessUnavailable("Jardal ERP no disponible.", status_code=response.status_code)
            try:
                result = response.json()
            except ValueError:
                raise BusinessUnavailable("Respuesta ERP inválida.", status_code=response.status_code) from None
            if not isinstance(result, dict):
                raise BusinessUnavailable("Respuesta ERP inválida.")
            if response.is_success:
                return result
            error = str(result.get("code", "ERP_ERROR"))
            cls = BusinessValidationError
            if response.status_code in {401, 403}:
                cls = BusinessPermissionError
            elif error.lower() == "dependency_open":
                cls = BusinessDependencyBlockedError
            elif error.lower() in {"version_conflict", "concurrent_update_conflict"}:
                cls = BusinessVersionConflict
            raise cls(f"Jardal ERP: {error}", status_code=response.status_code, payload={"code": error})
        raise BusinessUnavailable("Jardal ERP no disponible.")
