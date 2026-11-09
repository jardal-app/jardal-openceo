"""Perfiles y configuracion del worker OpenCode V2 (TASK-2026-00063).

El worker **no hereda** la configuracion global del host. Esta funcion construye su
configuracion completa: agentes, policies deny por defecto y el unico servidor MCP
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
    "worker_policies",
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
    "question",
    "opencode_list_mcp_resources",
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
    "question",
    "opencode_list_mcp_resources",
    "opencode_read_mcp_resource",
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

    Tres campos que se comprobaron contra el CLI real y su documentacion:

    * ``oauth: false`` — un servidor remoto usa **OAuth por defecto**
      (<https://opencode.ai/v2/docs/mcp-servers>). El broker no habla OAuth: su
      credencial viaja en la cabecera ``Authorization``. Sin desactivarlo, el CLI
      intenta descubrir un servidor de autorizacion, falla y deja las tools fuera de la
      sesion: el modelo responde que "la herramienta no esta disponible".
    * ``timeout`` con las claves de V2 (``startup``/``catalog``/``execution``), no con
      las de V1 (``connect``/``request``). Todas caben en el deadline del job.
    * ``codemode: false`` para que el catalogo sea directo y auditable.
    """
    deadline_ms = max(1000, int(float(deadline_seconds) * 1000))
    startup_ms = min(30_000, max(500, deadline_ms // 10))
    catalog_ms = min(30_000, max(500, deadline_ms // 10))
    execution_ms = max(1000, deadline_ms - startup_ms)
    server: dict[str, Any] = {
        "type": "remote",
        "url": endpoint.url,
        "disabled": False,
        "oauth": False,
        "codemode": False,
        "timeout": {
            "startup": startup_ms,
            "catalog": catalog_ms,
            "execution": execution_ms,
        },
    }
    headers = endpoint.headers()
    if headers:
        server["headers"] = headers
    return {"mcp": {"servers": {endpoint.server_name: server}}}


def worker_policies(*, server_name: str = BROKER_SERVER_NAME) -> list[dict[str, str]]:
    """Frontera de ejecucion: deny por defecto en la capa de policies.

    OpenCode V2 separa dos capas (<https://opencode.ai/v2/docs/policies>):

    * ``agents.<n>.permissions`` — reglas por agente, anadidas a los permisos nativos.
    * ``experimental.policies`` — declaraciones **binarias, nunca preguntan**, que solo
      pueden **endurecer** lo que permisos y proveedores ya permiten; no las levanta una
      regla de agente ni una aprobacion guardada.

    El deny pertenece a esta capa: con cualquier deny en permissions de agente,
    v2.0.20 devolvia provider.auth 403 en el proveedor gratuito. La captura real y
    su procedencia estan en ``tests/fixtures/README.md``.

    Orden: primero el deny global y despues las excepciones (gana la ultima que coincide);
    las tools se autorizan por **nombre normalizado**, desnudo y ``<servidor>_<tool>``.
    """
    statements: list[dict[str, str]] = [
        {"action": "permission", "resource": "*", "effect": "deny"}
    ]
    for tool in RESEARCH_TOOLS:
        statements.append(
            {"action": "permission", "resource": f"{tool}:*", "effect": "allow"}
        )
        statements.append(
            {"action": "permission", "resource": f"{server_name}_{tool}:*", "effect": "allow"}
        )
    return statements


def worker_permissions(*, server_name: str = BROKER_SERVER_NAME) -> list[dict[str, str]]:
    """Solo reglas allow para las tools MCP del broker, con su nombre cualificado.

    OpenCode V2 anade estas reglas al perfil nativo, que permite tools por defecto.
    La frontera efectiva la impone ``worker_policies``; un deny aqui reintroduce
    el 403 medido del proveedor gratuito (ver ``tests/fixtures/README.md``).
    """
    return [
        {"action": f"{server_name}_{tool}", "resource": "*", "effect": "allow"}
        for tool in RESEARCH_TOOLS
    ]


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
        # La frontera del worker. `policies` es binaria, no pregunta y solo endurece;
        # `agents.*.permissions` no lleva deny por compatibilidad con el proveedor.
        "experimental": {"policies": worker_policies(server_name=endpoint.server_name)},
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
    """Reglas allow del broker; ningun deny de agente compatible con la capa libre."""
    problems: list[str] = []
    if isinstance(permissions, dict):
        # Defecto real con v2.0.20: el CLI no avisa del esquema, descarta el agente y
        # responde "Agent not found".
        problems.append(
            f"{agent_name}: permissions debe ser una lista de reglas V2, no un dict "
            "(V1 descarta el agente: 'Agent not found')"
        )
        return problems
    if not isinstance(permissions, list) or not permissions:
        problems.append(f"{agent_name}: permissions debe ser una lista de reglas no vacia")
        return problems
    expected = {f"{BROKER_SERVER_NAME}_{tool}" for tool in RESEARCH_TOOLS}
    allowed: set[str] = set()
    for index, rule in enumerate(permissions):
        if not isinstance(rule, dict):
            problems.append(f"{agent_name}: cada regla debe ser un objeto")
            continue
        effect = rule.get("effect")
        if effect not in ("allow", "ask", "deny"):
            problems.append(f"{agent_name}: efecto invalido en la regla {index}: {effect!r}")
        action = rule.get("action")
        if effect == "deny":
            problems.append(
                f"{agent_name}: un deny en permissions deja al worker sin modelo "
                "(403 de la capa libre); la frontera va en experimental.policies"
            )
        if effect == "ask":
            problems.append(f"{agent_name}: el worker no admite permisos interactivos")
        if not isinstance(action, str) or action not in expected:
            problems.append(f"{agent_name}: accion fuera del broker: {action!r}")
        elif effect == "allow" and rule.get("resource") == "*":
            allowed.add(action)
        if rule.get("resource") != "*":
            problems.append(f"{agent_name}: recurso invalido en la regla {index}")
    for action in sorted(expected - allowed):
        problems.append(f"{agent_name}: falta autorizar {action}")
    return problems


def _policy_problems(policies: Any) -> list[str]:
    """Comprueba la frontera: deny global primero y solo las tools del broker despues."""
    problems: list[str] = []
    if not isinstance(policies, list) or not policies:
        problems.append("experimental.policies debe declarar la frontera (deny por defecto)")
        return problems
    deny_all_index: int | None = None
    allowed: set[str] = set()
    for index, statement in enumerate(policies):
        if not isinstance(statement, dict):
            problems.append(f"policies: la declaracion {index} debe ser un objeto")
            continue
        action = statement.get("action")
        resource = statement.get("resource")
        effect = statement.get("effect")
        if action != "permission":
            problems.append(f"policies: accion no soportada en {index}: {action!r}")
        if effect not in ("allow", "deny"):
            problems.append(f"policies: efecto invalido en {index}: {effect!r}")
        if effect == "deny" and resource == "*" and deny_all_index is None:
            deny_all_index = index
        if effect == "allow" and isinstance(resource, str):
            allowed.add(resource.split(":", 1)[0])
    if deny_all_index is None:
        problems.append("policies: falta el deny global {'permission','*','deny'}")
    for resource in allowed:
        if resource in DENIED_BUILTIN_TOOLS or resource in DENIED_ACTIONS:
            problems.append(f"policies: {resource} no puede estar permitido")
    for tool in RESEARCH_TOOLS:
        if tool not in allowed and f"{BROKER_SERVER_NAME}_{tool}" not in allowed:
            problems.append(f"policies: falta autorizar {tool}")
    # El allow tiene que ir despues del deny global: gana la ultima que coincide.
    for index, statement in enumerate(policies):
        if not isinstance(statement, dict) or statement.get("effect") != "allow":
            continue
        resource = str(statement.get("resource") or "").split(":", 1)[0]
        if resource in RESEARCH_TOOLS or resource.startswith(f"{BROKER_SERVER_NAME}_"):
            if deny_all_index is None or deny_all_index > index:
                problems.append(
                    f"policies: el allow de {resource} debe ir despues del deny global"
                )
    return problems


def assert_worker_config_is_closed(config: dict[str, Any]) -> list[str]:
    """Problemas de aislamiento de la configuracion. Lista vacia = cerrada.

    Devuelve motivos en lugar de lanzar para que el servicio pueda reportarlos y
    negarse a arrancar el worker con un diagnostico util.
    """
    problems: list[str] = []
    if config.get("websearch") is not False:
        problems.append("websearch debe estar deshabilitado")
    problems.extend(_policy_problems((config.get("experimental") or {}).get("policies")))
    servers = (config.get("mcp") or {}).get("servers") or {}
    if len(servers) != 1 or BROKER_SERVER_NAME not in servers:
        problems.append("solo debe montarse el broker de research")
    for name, server in servers.items():
        if name != BROKER_SERVER_NAME:
            problems.append(f"servidor MCP no autorizado: {name}")
        timeout = server.get("timeout")
        if not isinstance(timeout, dict) or not {"startup", "catalog", "execution"} <= set(timeout):
            problems.append(
                f"{name}: timeout debe ser un objeto V2 con startup, catalog y execution"
            )
        if server.get("oauth") is not False:
            # Un remoto con OAuth activo (el defecto del CLI) intenta autenticarse
            # contra el broker y deja las tools fuera de la sesion.
            problems.append(f"{name}: oauth debe ser false (el broker usa cabecera Bearer)")
        if server.get("codemode") not in (False,):
            problems.append(f"{name}: codemode debe ser false")
        if server.get("disabled") is True:
            problems.append(f"{name}: el broker no puede estar deshabilitado")
    for agent_name, profile in (config.get("agents") or {}).items():
        problems.extend(_permission_problems(agent_name, (profile or {}).get("permissions")))
    return problems
