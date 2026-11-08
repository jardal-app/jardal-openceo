"""Intake externo de research (ticket #619).

Dos vias sobre las fachadas y grants existentes, nada nuevo que autenticar:

* ``ticket`` (por defecto, Jardal ERP first): el solicitante externo abre un
  ticket de solicitud; el planner lo enruta a ``research`` + ``investigar``
  (#618) y la corrida admite/sondea bajo el consumidor del ticket, acunado en
  servidor por ``ticket_bridge`` (owner ``ticket:<TASK>``). Jardal ERP es el
  registro de negocio; el token del consumidor nunca sale del servidor.
* ``http`` / ``mcp`` (programatica, grants explicitos): ``router``
  (``/api/research/*``, cookie de sesion + ``run_research`` / ``read_research``)
  y ``mcp_facade`` (herramientas ``openceo_research_*``, token opaco de
  consumidor en ``Authorization``) sobre grants de ``consumers``. Sin
  fail-open: sin identidad no hay autorizacion; el owner nunca viaja en
  argumentos ni ``_meta``; un job ajeno se lee como 404, nunca 403.

Este modulo es la fuente unica de la matriz consumidor -> via que la
documentacion reproduce; los tests fijan ambos lados para que la prosa no
derive de las fachadas.
"""

from __future__ import annotations

from dataclasses import dataclass

from research.consumers import CONSUMER_OPERATIONS, DEFAULT_CONSUMER_OPERATIONS
from research.ticket_bridge import TICKET_CONSUMER_OPERATIONS

__all__ = [
    "CONSUMER_INTAKE",
    "DEFAULT_INTAKE",
    "INTAKE_VIAS",
    "PROGRAMMATIC_OPERATIONS",
    "IntakeEntry",
    "intake_for",
]

#: Via por defecto: el ticket de solicitud (Jardal ERP first).
DEFAULT_INTAKE = "ticket"

#: Vias de intake externo. ``ticket`` es la via por defecto; ``http`` y ``mcp``
#: son la via programatica con grants explicitos.
INTAKE_VIAS = ("ticket", "http", "mcp")


@dataclass(frozen=True)
class IntakeEntry:
    """Una fila de la matriz: que consumidor usa cada via y con que identidad."""

    consumer: str
    via: str
    auth: str
    owner_source: str
    operations: tuple[str, ...]
    facade: str
    notes: str = ""


#: Matriz consumidor -> via. Las operaciones nombran la misma operacion en las
#: tres vias (``submit`` admite, ``get`` lee, ``cancel`` detiene); cada via
#: autoriza a su manera y el owner sale siempre del transporte o del grant.
CONSUMER_INTAKE: tuple[IntakeEntry, ...] = (
    IntakeEntry(
        consumer="solicitante externo",
        via="ticket",
        auth="ticket de solicitud en Jardal ERP",
        owner_source="ticket:<TASK-2026-NNNNN> (lo acuna el puente en servidor)",
        operations=TICKET_CONSUMER_OPERATIONS,
        facade="research/ticket_bridge.py",
        notes="Via por defecto; la corrida research admite (investigar) y sondea "
        "(seguimiento) bajo este consumidor.",
    ),
    IntakeEntry(
        consumer="cliente HTTP programatico",
        via="http",
        auth="cookie de sesion + permisos run_research/read_research",
        owner_source="principal autenticado (get_current_user)",
        operations=DEFAULT_CONSUMER_OPERATIONS,
        facade="research/router.py",
        notes="POST/GET /api/research/jobs y cancel; suscripciones y eventos bajo "
        "los mismos permisos.",
    ),
    IntakeEntry(
        consumer="consumidor MCP programatico",
        via="mcp",
        auth="token opaco de consumidor en Authorization: Bearer",
        owner_source="grant.owner_id del token",
        operations=DEFAULT_CONSUMER_OPERATIONS,
        facade="research/mcp_facade.py",
        notes="El grant fija proyectos y operaciones; eventos y suscripciones "
        "exigen grants explicitos fuera del conjunto por defecto.",
    ),
)

#: Operaciones conocidas en la via programatica. Reexportado para que los tests
#: comprueben que la matriz no inventa operaciones fuera del vocabulario.
PROGRAMMATIC_OPERATIONS = CONSUMER_OPERATIONS


def intake_for(consumer: str) -> IntakeEntry:
    """Fila de la matriz para un consumidor. ``KeyError`` si no existe."""
    for entry in CONSUMER_INTAKE:
        if entry.consumer == consumer:
            return entry
    known = ", ".join(entry.consumer for entry in CONSUMER_INTAKE)
    raise KeyError(f"Consumidor de research desconocido: {consumer!r} (conocidos: {known})")
