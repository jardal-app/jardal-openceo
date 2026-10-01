"""Broker MCP de adquisicion para el worker de research (TASK-2026-00063).

El worker no recibe claves de buscadores ni acceso de red libre: recibe **un** MCP,
el broker, con tres herramientas. Cada llamada presenta una capability corta y el
broker deriva de ella el proyecto, el owner y el research_id. Los argumentos de la
herramienta no pueden cambiar el alcance, y `_meta.sessionID` es correlacion, no
autorizacion: no se lee en ningun punto.

Por que un broker y no las monturas existentes
==============================================
Las monturas internas/globales exponen infraestructura y tickets protegidas por un
Bearer compartido y loopback: eso controla el transporte, **no** identifica al owner.
Publicar alli herramientas multiowner asumiria una identidad que no existe.

La web es **dato no confiable**: el contenido devuelto se marca como tal y nunca se
interpreta como una orden de ejecutar codigo, cambiar permisos o exfiltrar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

try:  # pragma: no cover - la dependencia existe en el runtime real
    from fastmcp import FastMCP
except Exception:  # noqa: BLE001
    FastMCP = None  # type: ignore[assignment]

from research.acquisition.base import AcquisitionError

from .capabilities import (
    BROKER_OPERATIONS,
    Capability,
    CapabilityError,
    CapabilityStore,
)

__all__ = [
    "BrokerDependencies",
    "build_research_broker",
    "untrusted_payload",
]

# Marca explicita de procedencia. Un payload de la web entra como dato; el modelo no
# puede convertirlo en autoridad.
UNTRUSTED_MARK = "untrusted"


def untrusted_payload(*, url: str, text: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"url": url, "text": text, "trust": UNTRUSTED_MARK}
    payload.update(extra)
    return payload


@dataclass
class BrokerDependencies:
    """Operaciones reales del broker, inyectadas.

    Cada funcion recibe la **capability ya autenticada** y devuelve un payload
    estructurado. Inyectarlas mantiene el broker sin claves ni red propias y permite
    que el servicio comparta exactamente la misma politica y el mismo ledger que el
    resto de la adquisicion.
    """

    discover: Callable[[Capability, str, int], Awaitable[dict[str, Any]]] | None = None
    read: Callable[[Capability, str], Awaitable[dict[str, Any]]] | None = None
    grounded: Callable[[Capability, str], Awaitable[dict[str, Any]]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _error_payload(exc: CapabilityError) -> dict[str, Any]:
    return {"ok": False, "error": exc.to_payload()}


def _missing_config(operation: str, detail: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"reason": "missing_config", "detail": detail, "operation": operation},
    }


def build_research_broker(
    *,
    capabilities: CapabilityStore,
    deps: BrokerDependencies,
    name: str = "research-broker",
):
    """Servidor FastMCP con exactamente las tres operaciones del broker."""
    if FastMCP is None:  # pragma: no cover - se comprueba en el test de catalogo
        raise RuntimeError("fastmcp no esta instalado; el broker no puede montarse")

    server = FastMCP(name)

    async def _guarded(
        operation: str,
        capability_token: str,
        handler: Callable[[Capability], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            capability = capabilities.authenticate(capability_token, operation=operation)
        except CapabilityError as exc:
            return _error_payload(exc)
        try:
            result = await handler(capability)
        except AcquisitionError as exc:
            return {
                "ok": False,
                "scope": capability.scope.to_payload(),
                "error": {
                    "reason": exc.kind,
                    "detail": exc.detail,
                    "coverage_reason": exc.coverage_reason.value,
                },
            }
        payload = {"ok": True, "scope": capability.scope.to_payload(), "operation": operation}
        payload.update(result)
        return payload

    @server.tool(name="research_discover")
    async def research_discover(query: str, capability: str, limit: int = 5) -> dict[str, Any]:
        """Descubre URLs para una consulta. El alcance sale de la capability."""
        if deps.discover is None:
            return _missing_config("research_discover", "Adaptador de descubrimiento no configurado")
        bounded = max(1, min(int(limit), 20))
        return await _guarded(
            "research_discover", capability, lambda cap: deps.discover(cap, query, bounded)
        )

    @server.tool(name="research_read")
    async def research_read(url: str, capability: str) -> dict[str, Any]:
        """Lee una URL publica. El contenido vuelve marcado como no confiable."""
        if deps.read is None:
            return _missing_config("research_read", "Lector HTTP no configurado")
        return await _guarded("research_read", capability, lambda cap: deps.read(cap, url))

    @server.tool(name="research_grounded_lookup")
    async def research_grounded_lookup(question: str, capability: str) -> dict[str, Any]:
        """Grounding pagado y opcional. Sin presupuesto responde missing_config."""
        if deps.grounded is None:
            return _missing_config(
                "research_grounded_lookup",
                "Grounding deshabilitado o sin presupuesto; el catalogo sigue disponible",
            )
        return await _guarded(
            "research_grounded_lookup", capability, lambda cap: deps.grounded(cap, question)
        )

    return server


def broker_operations() -> tuple[str, ...]:
    """Catalogo declarado del broker, para compararlo con el real en los tests."""
    return BROKER_OPERATIONS
