"""Contexto de ejecucion research y admision de jobs, fuera del runner de Tasks.

Vive aparte de ``workflows.py`` para que el motor de vigilancia (``watch.py``) y el
runner de schedules compartan el contexto sin importarse en ciclo (TASK-2026-00069).

El contexto lo construye **el handler** a partir del schedule persistido y del principal
resuelto en ese tick. Nunca sale del YAML ni de los params: un tick no puede declarar
otro owner, otro proyecto ni otra clave idempotente.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from research.contracts import ResearchJobRequest
from research.policies import Principal, ResearchAccessDenied

__all__ = [
    "FORBIDDEN_REQUEST_FIELDS",
    "ResearchExecutionContext",
    "admit_job",
    "require_research_context",
]

# Identidad y claves de replay pertenecen al schedule persistido, no a sus params.
FORBIDDEN_REQUEST_FIELDS = frozenset(
    {"project_key", "owner_id", "purpose", "idempotency_key", "mode"}
)


@dataclass(frozen=True)
class ResearchExecutionContext:
    project_key: str
    owner_id: str
    purpose: str
    principal: Principal
    project_cfg: dict[str, Any]
    execution_key: str
    on_admitted: Callable[[str], Any] | None = None
    # Schedule vigente del tick (cursor, lease y fencing). Lo necesitan los ticks que
    # avanzan un cursor; ``research.investigate`` no lo usa.
    schedule: dict[str, Any] | None = None


def require_research_context(context: dict[str, Any]) -> ResearchExecutionContext:
    execution = context.get("research_context")
    if (context.get("scope") != "research" or "task" in context or "task_id" in context
            or not isinstance(execution, ResearchExecutionContext)):
        raise ResearchAccessDenied("Se requiere un contexto de ejecucion research")
    if execution.principal.user_id != execution.owner_id:
        raise ResearchAccessDenied("El principal no corresponde al owner del schedule")
    if not all((execution.project_key, execution.owner_id, execution.purpose, execution.execution_key)):
        raise ResearchAccessDenied("Contexto research incompleto")
    return execution


async def admit_job(
    service: Any,
    execution: ResearchExecutionContext,
    fields: dict[str, Any],
    *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Admite un job quick con el scope del contexto. No espera al modelo.

    ``idempotency_key`` por defecto es la clave de la ocurrencia; un tick que admite
    varios lotes la deriva del lote para que un replay reconcilie el mismo job.
    """
    if service is None:
        raise RuntimeError("Servicio research no montado")
    forbidden = FORBIDDEN_REQUEST_FIELDS.intersection(fields)
    if forbidden:
        raise ResearchAccessDenied("Los parametros no pueden reemplazar el scope del schedule")
    payload = ResearchJobRequest(
        **fields, project_key=execution.project_key, owner_id=execution.owner_id,
        purpose=execution.purpose, mode="quick",
        idempotency_key=idempotency_key or execution.execution_key,
    )
    document = await service.submit(
        payload, principal=execution.principal, project_cfg=execution.project_cfg,
    )
    # A replay can repair a wake lost after persistence. Terminal jobs are not run again.
    if document.get("status") == "queued" and execution.on_admitted is not None:
        result = execution.on_admitted(document["research_id"])
        if inspect.isawaitable(result):
            await result
    return {"research_id": document["research_id"], "status": document["status"],
            "created": bool(document.get("_created"))}
