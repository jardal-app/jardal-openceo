"""Perfiles y configuracion del worker OpenCode V2 (TASK-2026-00063).

El worker **no hereda** la configuracion global del host. Esta funcion construye su
configuracion completa: agentes, permisos deny por defecto y el unico servidor MCP
autorizado, el broker de adquisicion con alcance por job.

Decisiones que importan
=======================
* **Deny por defecto.** Solo se autorizan las herramientas del broker. `shell`,
  edicion, lectura amplia, `glob`/`grep`, `websearch`, `webfetch`, subagentes y
  skills quedan denegados: la lectura web que el investigador necesita pasa por el
  broker, que aplica egress, politica y ledger.
* **El modelo no puede cambiar modelos, cuentas ni `budget_ref`** desde una
  herramienta: esos valores no son argumentos de ninguna tool.
* **El timeout del servidor MCP es un objeto explicito y cabe en el deadline** del
  job; no se deja el defecto del runtime.
* `codemode=false` para que el catalogo sea directo y auditable.

Formato de permisos: lista ordenada de reglas, no dict
=====================================================
En OpenCode V2 `permissions` es una **lista ordenada** de reglas
``{"action", "resource", "effect"}`` y gana la ultima que coincide
(https://opencode.ai/v2/docs/agents). El formato V1 (dict
``{"bash": "deny"}``) **no** produce un error de validacion: el CLI descarta el
agente entero y luego falla con ``Agent not found: "investigator"``. Se comprobo
contra el binario v2.0.20 el 2026-10-01 (``tests/fixtures/README.md``); por eso
`assert_worker_config_is_closed` rechaza explicitamente el dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "BROKER_SERVER_NAME",
    "DENIED_ACTIONS",
    "DENIED_BUILTIN_TOOLS",
    "BrokerEndpoint",
    "RESEARCH_TOOLS",
    "WORKER_AGENTS",
    "agent_profiles",
    "assert_worker_config_is_closed",
    "build_worker_config",
    "broker_server_config",
    "worker_permissions",
]

BROKER_SERVER_NAME = "research-broker"
RESEARCH_TOOLS = ("research_discover", "research_read", "research_grounded_lookup")

# Acciones de V2 que se deniegan ademas del deny global. Es defensa en profundidad:
# si alguien quitara la regla `*`, estas siguen cerradas.
DENIED_ACTIONS = (
    "shell",
    "edit",
    "read",
    "glob",
    "grep",
    "webfetch",
    "websearch",
    "skill",
    "subagent",
)

# Nombres de herramienta del CLI que nunca pueden quedar permitidos. Se comprueba de
# forma explicita en los tests y en `assert_worker_config_is_closed`: una lista
# implicita ("lo que no este permitido") se rompe en cuanto el runtime anade una tool
# nueva con otro nombre.
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


def worker_permissions(*, server_name: str = BROKER_SERVER_NAME) -> list[dict[str, str]]:
    """Permisos de V2: deny por defecto y allow solo para las tools del broker.

    Devuelve la **lista ordenada** que espera ``agents.<nombre>.permissions`` en
    OpenCode V2. La regla ``*`` va primero y las excepciones despues, porque gana la
    ultima regla que coincide.

    Las tools se autorizan por **nombre normalizado**: el nombre desnudo y la forma
    ``<servidor>_<tool>``, porque el runtime puede exponer una u otra.
    """
    rules: list[dict[str, str]] = [{"action": "*", "resource": "*", "effect": "deny"}]
    for action in DENIED_ACTIONS:
        rules.append({"action": action, "resource": "*", "effect": "deny"})
    for tool in RESEARCH_TOOLS:
        rules.append({"action": tool, "resource": "*", "effect": "allow"})
        rules.append({"action": f"{server_name}_{tool}", "resource": "*", "effect": "allow"})
    return rules


def agent_profiles(*, model: str, server_name: str = BROKER_SERVER_NAME) -> dict[str, Any]:
    """Perfiles `investigator`, `researcher` y `verifier`.

    En la Entrega A solo se usa `investigator`; los otros dos existen para la
    Entrega D y heredan **el mismo** alcance y consumo, sin ampliar permisos.

    ``mode: primary`` es explicito: el runtime selecciona el agente con `--agent` para
    una sesion principal, y no se deja al defecto del CLI.
    """
    permissions = worker_permissions(server_name=server_name)
    return {
        "investigator": {
            "model": model,
            "mode": "primary",
            "description": "Investiga, contrasta y sintetiza con evidencia citada.",
            "permissions": [dict(rule) for rule in permissions],
        },
        "researcher": {
            "model": model,
            "mode": "primary",
            "description": "Subtarea acotada de investigacion; mismo alcance y presupuesto.",
            "permissions": [dict(rule) for rule in permissions],
        },
        "verifier": {
            "model": model,
            "mode": "primary",
            "description": "Revisa soporte real de las citas en contexto nuevo.",
            "permissions": [dict(rule) for rule in permissions],
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


def _permission_problems(agent_name: str, permissions: Any) -> list[str]:
    """Comprueba la lista ordenada de reglas de un agente."""
    problems: list[str] = []
    if isinstance(permissions, dict):
        # Este es el defecto real encontrado con v2.0.20: el CLI no avisa del esquema,
        # simplemente descarta el agente y responde "Agent not found".
        problems.append(
            f"{agent_name}: permissions debe ser una lista de reglas V2, no un dict "
            "(V1 descarta el agente: 'Agent not found')"
        )
        return problems
    if not isinstance(permissions, list) or not permissions:
        problems.append(f"{agent_name}: permissions debe ser una lista de reglas no vacia")
        return problems

    deny_all_index: int | None = None
    allowed: set[str] = set()
    for index, rule in enumerate(permissions):
        if not isinstance(rule, dict):
            problems.append(f"{agent_name}: cada regla debe ser un objeto")
            continue
        action = rule.get("action")
        resource = rule.get("resource")
        effect = rule.get("effect")
        if effect not in ("allow", "ask", "deny"):
            problems.append(f"{agent_name}: efecto invalido en la regla {index}: {effect!r}")
        if effect == "deny" and action == "*" and resource == "*" and deny_all_index is None:
            deny_all_index = index
        if effect == "allow" and isinstance(action, str):
            allowed.add(action)

    if deny_all_index is None:
        problems.append(f"{agent_name}: falta la regla deny por defecto {{'*','*','deny'}}")
    for action in allowed:
        if action in DENIED_BUILTIN_TOOLS or action in DENIED_ACTIONS:
            problems.append(f"{agent_name}: {action} no puede estar permitido")
        # Una excepcion solo vale si existe el deny global y va antes.
        if deny_all_index is None or deny_all_index > _first_allow_index(permissions, action):
            problems.append(f"{agent_name}: el allow de {action} debe ir despues del deny global")

    for tool in RESEARCH_TOOLS:
        if tool not in allowed:
            problems.append(f"{agent_name}: falta autorizar {tool}")
    return problems


def _first_allow_index(permissions: list[Any], action: str) -> int:
    for index, rule in enumerate(permissions):
        if isinstance(rule, dict) and rule.get("action") == action and rule.get("effect") == "allow":
            return index
    return len(permissions)


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
        problems.extend(_permission_problems(agent_name, (profile or {}).get("permissions")))
    return problems
