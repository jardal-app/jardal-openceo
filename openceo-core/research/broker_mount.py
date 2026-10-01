"""Montaje real del broker MCP del worker (TASK-2026-00063).

Por que existe este modulo
==========================
`build_research_broker` construia el servidor FastMCP y se probaba, pero **ninguna
ruta de la aplicacion lo servia**: el worker no tenia endpoint al que conectarse, asi
que el circuito modelo -> tool MCP -> lectura no existia. Aqui se cierra ese hueco con
dos piezas:

1. Las **operaciones** que el broker inyecta (`BrokerDependencies`), construidas sobre
   el mismo lector HTTP, el mismo almacen de evidencias y las mismas politicas que el
   resto del servicio. El broker no tiene claves ni red propias.
2. La **montura** HTTP con la unica frontera que importa para un endpoint interno:
   loopback + Bearer de transporte. El alcance por job no viaja ahi: viaja en la
   capability que cada llamada de herramienta presenta.

El worker corre bajo bwrap con la red compartida, asi que alcanza esta montura por
`127.0.0.1`; nunca se publica en Traefik.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from research.contracts import AccessMethod, ResearchEvidence
from research.worker.broker import (
    BrokerDependencies,
    build_research_broker,
    untrusted_payload,
)
from research.worker.capabilities import Capability, CapabilityStore

logger = logging.getLogger(__name__)

__all__ = [
    "BROKER_NAME",
    "BrokerReadLog",
    "DEFAULT_BROKER_PATH",
    "MountedResearchBroker",
    "broker_url_for",
    "build_broker_operations",
    "build_research_broker_mount",
]

BROKER_NAME = "OpenCEO Research Broker"
DEFAULT_BROKER_PATH = "/internal/mcp-research-broker"

# Techo del texto que vuelve al modelo. El payload completo queda en la evidencia; el
# modelo no necesita (ni debe) recibir un documento entero por una tool.
MAX_PAYLOAD_CHARS = 12_000


class BrokerReadLog:
    """Registro en memoria de las lecturas que el **modelo** hizo por el broker.

    El servicio construye su propia evidencia leyendo fuentes; cuando el modelo abre
    una fuente por `research_read`, esa lectura es igual de real y **tambien** queda
    persistida. Sin este registro, la cita que el modelo hace del `evidence_id` que
    recibio del broker se rechazaria como "evidencia de otro job" y el circuito
    terminaria `partial`: el defecto seria de integracion, no del modelo.

    Es en memoria a proposito: vive lo que dura el job en el proceso que lo ejecuta.
    """

    def __init__(self) -> None:
        self._by_job: dict[str, dict[str, dict[str, Any]]] = {}

    def record(self, research_id: str, evidence_id: str, row: dict[str, Any]) -> None:
        if not research_id or not evidence_id:
            return
        self._by_job.setdefault(research_id, {})[evidence_id] = row

    def take(self, research_id: str) -> dict[str, dict[str, Any]]:
        """Devuelve y retira lo leido por el modelo para ese job."""
        return self._by_job.pop(research_id, {})


def _evidence_id_for(research_id: str, url: str, content_hash: str | None) -> str:
    """Id determinista: el mismo contenido y la misma fuente no crean filas nuevas."""
    material = f"{research_id}|{url}|{content_hash or ''}".encode("utf-8")
    return "br-" + hashlib.sha256(material).hexdigest()[:24]


def _iso(moment: Any) -> str | None:
    if moment is None:
        return None
    if hasattr(moment, "isoformat"):
        return moment.isoformat()
    return str(moment)


def build_broker_operations(
    *,
    reader: Callable[..., Awaitable[dict[str, Any]]] | None,
    evidence: Any | None,
    discover: Callable[..., Awaitable[Any]] | None = None,
    grounded: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    read_log: "BrokerReadLog | None" = None,
    clock: Callable[[], Any] | None = None,
) -> BrokerDependencies:
    """Operaciones del broker con el alcance derivado **de la capability**.

    Ninguna operacion acepta un `project_key`, `owner_id` o `research_id` del payload:
    los toma de `capability.scope`, que ya fue autenticada. Es la unica identidad que
    el broker reconoce.
    """
    now = clock or (lambda: None)

    async def broker_read(capability: Capability, url: str) -> dict[str, Any]:
        if reader is None:
            raise RuntimeError("lector HTTP no configurado")
        scope = capability.scope
        page = await reader(
            url=url, research_id=scope.research_id, project_key=scope.project_key
        )
        text = str(page.get("text") or "")
        final_url = str(page.get("url") or url)
        content_hash = page.get("content_hash")
        observed_at = page.get("observed_at") or now()

        evidence_id: str | None = None
        stored = None
        if evidence is not None:
            evidence_id = _evidence_id_for(scope.research_id, final_url, content_hash)
            record = ResearchEvidence(
                id=evidence_id,
                url=final_url,
                access_method=AccessMethod.HTTP,
                visibility=scope.classification,
                observed_at=observed_at,
                published_at=page.get("published_at"),
                content_hash=content_hash,
                project_key=scope.project_key,
                owner_id=scope.owner_id,
            )
            stored = await evidence.asave(
                record, content=text, provider=page.get("provider")
            )
        if read_log is not None and evidence_id:
            read_log.record(
                scope.research_id,
                evidence_id,
                {"url": final_url, "text": text, "stored": stored is not None},
            )
        return untrusted_payload(
            url=final_url,
            text=text[:MAX_PAYLOAD_CHARS],
            evidence_id=evidence_id,
            content_hash=content_hash,
            observed_at=_iso(observed_at),
            truncated=len(text) > MAX_PAYLOAD_CHARS,
            persisted=stored is not None,
        )

    async def broker_discover(capability: Capability, query: str, limit: int) -> dict[str, Any]:
        if discover is None:
            raise RuntimeError("adaptador de descubrimiento no configurado")
        scope = capability.scope
        found = await discover(
            question=query, research_id=scope.research_id, project_key=scope.project_key
        )
        items: list[dict[str, Any]] = []
        for item in getattr(found, "items", ()) or ():
            item_url = getattr(item, "url", None)
            if not isinstance(item_url, str) or not item_url:
                continue
            items.append(
                {
                    "url": item_url,
                    "provider": getattr(item, "provider", None),
                    "rank": getattr(item, "rank", None),
                    # El buscador no aporta titulo ni snippet a proposito (condiciones
                    # de Brave): el SERP es transitorio y no se almacena.
                    "trust": "untrusted",
                }
            )
            if len(items) >= limit:
                break
        return {"items": items, "count": len(items), "provider": getattr(found, "provider", None)}

    async def broker_grounded(capability: Capability, question: str) -> dict[str, Any]:
        if grounded is None:
            raise RuntimeError("grounding no configurado")
        scope = capability.scope
        return await grounded(
            question=question, research_id=scope.research_id, project_key=scope.project_key
        )

    return BrokerDependencies(
        discover=broker_discover if discover is not None else None,
        read=broker_read if reader is not None else None,
        grounded=broker_grounded if grounded is not None else None,
    )


@dataclass
class MountedResearchBroker:
    """Montura lista para servir. `url` es lo que se le da al runtime del worker."""

    path: str
    url: str
    mount: Any

    @property
    def bearer(self) -> str | None:
        return getattr(self.mount, "bearer_token", None)


def broker_url_for(settings: Any, path: str) -> str:
    """URL de loopback que ve el worker. `127.0.0.1`, nunca una interfaz publica."""
    port = int(getattr(settings, "OPENCEO_PORT", 18765) or 18765)
    return f"http://127.0.0.1:{port}{path}/"


def build_research_broker_mount(
    *,
    settings: Any,
    capabilities: CapabilityStore,
    deps: BrokerDependencies,
    path: str | None = None,
) -> MountedResearchBroker | None:
    """Construye la montura del broker. `None` si el ajuste la deja apagada.

    La montura nace apagada a proposito, igual que la fachada MCP de consumidores:
    abrir un endpoint es una decision de despliegue, no un efecto lateral de arrancar
    el servicio.
    """
    if not bool(getattr(settings, "RESEARCH_BROKER_ENABLED", False)):
        return None
    from mcp_server import _parse_allowed_networks, build_mounted_mcp_server  # noqa: PLC0415

    resolved = path or str(
        getattr(settings, "RESEARCH_BROKER_PATH", "") or DEFAULT_BROKER_PATH
    )
    server = build_research_broker(capabilities=capabilities, deps=deps)
    bearer = str(getattr(settings, "RESEARCH_BROKER_BEARER", "") or "") or None
    mount = build_mounted_mcp_server(
        path=resolved,
        name=BROKER_NAME,
        server=server,
        bearer_token=bearer,
        allowed_networks=_parse_allowed_networks(
            str(getattr(settings, "MCP_SERVER_ALLOWED_IPS", "") or "127.0.0.1/32,::1/128")
        ),
        stateless_http=True,
        json_response=True,
    )
    return MountedResearchBroker(
        path=mount.path, url=broker_url_for(settings, mount.path), mount=mount
    )
