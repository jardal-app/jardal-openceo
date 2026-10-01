"""Paquete del servicio research (TASK-2026-00056, Entrega A).

Modulos:

* ``money``       — enteros escalados en micro-USD, sin floats.
* ``budget``      — autoridad de saldo con reservas atomicas de un solo documento.
* ``contracts``   — modelos validados de solicitud/resultado y estados del job.
* ``policies``    — acceso, privacidad, routing y guard de egress reutilizable.
* ``acquisition`` — clientes cerrados Brave/Serper/Firecrawl y lectura HTTP/RSS.
* ``evidence``    — almacen de evidencia por proyecto (SQLite/FTS5) con retencion.
* ``runtimes``    — runtime sustituible; OpenCode V2 con aislamiento obligatorio.
* ``worker``      — capacidades por job, perfiles V2 cerrados y broker MCP acotado.
* ``service``     — admission, ciclo quick, cancelacion y partial honesto.
* ``consumers``   — credenciales de consumidor revocables con grants explicitos.
* ``router``      — fachada HTTP /api/research con la identidad del proyecto.
* ``mcp_facade``  — tools openceo_research_* con la credencial leida del transporte.
* ``wiring``      — montaje real del servicio y cierre de sus recursos.

El paquete se importa sin claves de proveedor y sin red: la ausencia de
configuracion debe producir degradacion estructurada, nunca un fallback implicito
a un proveedor pagado.
"""

from __future__ import annotations

__all__: list[str] = []
