"""Perfiles y configuracion del worker OpenCode V2 (TASK-2026-00063).

El worker **no hereda** la configuracion global del host. Esta funcion construye su
configuracion completa: agentes, permisos deny por defecto y el unico servidor MCP
autorizado, el broker de adquisicion con alcance por job.

Decisiones que importan
=======================
* **Deny por defecto.** Solo se autorizan las herramientas del broker, por nombre
  normalizado. `bash`, edicion, lectura amplia, `glob`/`grep`, `websearch`,
  `webfetch`, subagentes y plugins heredados quedan denegados: la lectura web que el
  investigador necesita pasa por el broker, que aplica egress, politica y ledger.
* **El modelo no puede cambiar modelos, cuentas ni `budget_ref`** desde una
  herramienta: esos valores no son argumentos de ninguna tool.
* **El timeout del servidor MCP es un objeto explicito y cabe en el deadline** del
  job; no se deja el defecto del runtime.
* `codemode=false` para que el catalogo sea directo y auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "BROKER_SERVER_NAME",
    "DENIED_BUILTIN_TOOLS",
    "BrokerEndpoint",
    "WORKER_AGENTS",
    "agent_profiles",
    "assert_worker_config_is_closed",
    "build_worker_config",
    "broker_server_config",
    "worker_permissions",
]

BROKER_SERVER_NAME = "research-broker"
RESEARCH_TOOLS = ("research_discover", "research_read", "research_grounded_lookup")

# Herramientas internas del CLI que se deniegan siempre en el worker. La lista es
# explicita y se comprueba en los tests: una lista implicita ("lo que no este
# permitido") se rompe en cuanto el runtime anade una tool nueva con otro nombre.
DENIED_BUILTIN_TOOLS = (
    "bash",
    "shell",
    "terminal",
    "edit",
    "write",
    "patch",
    "multiedit",
    "read",
    "glob",
    "grep",
    "list",
    "ls",
    "webfetch",
    "websearch",
    "fetch",
    "browser",
    "task",
    "agent",
    "subagent",
    "todowrite",
    "todoread",
    "notebook",
    "skill",
    "plugin",
)

WORKER_AGENTS = ("investigator", "researcher", "verifier")


@dataclass(frozen=True)
class BrokerEndpoint:
    """Endpoint del broker de adquisicion para el worker.

    La credencial es la del **transporte**; el alcance por job viaja en la
    capability que cada llamada presenta. No se monta el MCP interno/global.
    """

    url: str
    bearer: str = ""
    server_name: str = BROKER_SERVER_NAME

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer}"} if self.bearer else {}


def broker_server_config(*, endpoint: BrokerEndpoint, deadline_seconds: float) -> dict[str, Any]:
    """Entrada ``mcp.servers`` del worker para el broker.

    ``timeout`` es un objeto con connect/request: el runtime no debe quedarse con su
    valor por defecto, que puede superar el deadline del job.
    """
    budget_ms = max(1000, int(float(deadline_seconds) * 1000))
    connect_ms = min(5_000, max(500, budget_ms // 10))
    request_ms = max(1000, min(budget_ms - connect_ms, 60_000))
    server: dict[str, Any] = {
        "type": "remote",
        "url": endpoint.url,
        "disabled": False,
        "codemode": False,
        "timeout": {"connect": connect_ms, "request": request_ms},
    }
    headers = endpoint.headers()
    if headers:
        server["headers"] = headers
    return {"mcp": {"servers": {endpoint.server_name: server}}}


def worker_permissions(*, server_name: str = BROKER_SERVER_NAME) -> dict[str, str]:
    """Matriz de permisos: deny por defecto, allow solo para las tools del broker.

    Las tools se autorizan por **nombre normalizado**: el nombre desnudo y la forma
    ``<servidor>_<tool>``, porque el runtime puede exponer una u otra.
    """
    permissions: dict[str, str] = {"*": "deny"}
    for tool in DENIED_BUILTIN_TOOLS:
        permissions[tool] = "deny"
    for tool in RESEARCH_TOOLS:
        permissions[tool] = "allow"
        permissions[f"{server_name}_{tool}"] = "allow"
    return permissions


def agent_profiles(*, model: str, server_name: str = BROKER_SERVER_NAME) -> dict[str, Any]:
    """Perfiles `investigator`, `researcher` y `verifier`.

    En la Entrega A solo se usa `investigator`; los otros dos existen para la
    Entrega D y heredan **el mismo** alcance y consumo, sin ampliar permisos.
    """
    permissions = worker_permissions(server_name=server_name)
    return {
        "investigator": {
            "model": model,
            "description": "Investiga, contrasta y sintetiza con evidencia citada.",
            "permissions": dict(permissions),
        },
        "researcher": {
            "model": model,
            "description": "Subtarea acotada de investigacion; mismo alcance y presupuesto.",
            "permissions": dict(permissions),
        },
        "verifier": {
            "model": model,
            "description": "Revisa soporte real de las citas en contexto nuevo.",
            "permissions": dict(permissions),
        },
    }


def build_worker_config(
    *,
    model: str,
    endpoint: BrokerEndpoint,
    deadline_seconds: float,
    agents: Iterable[str] = ("investigator",),
) -> dict[str, Any]:
    """Configuracion completa del worker. Nada se hereda del host."""
    if not model:
        raise ValueError("El worker necesita un modelo explicito")
    profiles = agent_profiles(model=model, server_name=endpoint.server_name)
    selected = {name: profiles[name] for name in agents if name in profiles}
    if not selected:
        raise ValueError("El worker necesita al menos un agente configurado")
    config: dict[str, Any] = {
        "agents": selected,
        # El buscador integrado no implementa nuestra politica de coste y permisos:
        # la adquisicion pasa por el broker.
        "websearch": False,
        # Ni skills ni plugins heredados del host.
        "skills": {"enabled": False},
        "plugins": {"enabled": False},
        "mcp": broker_server_config(endpoint=endpoint, deadline_seconds=deadline_seconds)["mcp"],
    }
    return config


def assert_worker_config_is_closed(config: dict[str, Any]) -> list[str]:
    """Problemas de aislamiento de la configuracion. Lista vacia = cerrada.

    Devuelve motivos en lugar de lanzar para que el servicio pueda reportarlos y
    negarse a arrancar el worker con un diagnostico util.
    """
    problems: list[str] = []
    if config.get("websearch") is not False:
        problems.append("websearch debe estar deshabilitado")
    servers = (config.get("mcp") or {}).get("servers") or {}
    if len(servers) != 1 or BROKER_SERVER_NAME not in servers:
        problems.append("solo debe montarse el broker de research")
    for name, server in servers.items():
        if name != BROKER_SERVER_NAME:
            problems.append(f"servidor MCP no autorizado: {name}")
        timeout = server.get("timeout")
        if not isinstance(timeout, dict) or "connect" not in timeout or "request" not in timeout:
            problems.append(f"{name}: timeout debe ser un objeto con connect y request")
        if server.get("codemode") not in (False,):
            problems.append(f"{name}: codemode debe ser false")
        if server.get("disabled") is True:
            problems.append(f"{name}: el broker no puede estar deshabilitado")
    for agent_name, profile in (config.get("agents") or {}).items():
        permissions = profile.get("permissions") or {}
        if permissions.get("*") != "deny":
            problems.append(f"{agent_name}: los permisos deben ser deny por defecto")
        for tool in DENIED_BUILTIN_TOOLS:
            if permissions.get(tool) == "allow":
                problems.append(f"{agent_name}: {tool} no puede estar permitido")
        for tool in RESEARCH_TOOLS:
            if permissions.get(tool) != "allow":
                problems.append(f"{agent_name}: falta autorizar {tool}")
    return problems
