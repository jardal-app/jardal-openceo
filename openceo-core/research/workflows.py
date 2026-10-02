"""Deterministic schedule admission, outside the business-task runner (TASK-2026-00082).

A tick admits a job; it does not wait for the model or complete a business Task.
Authorization is resolved again at each tick, never reconstructed from YAML params.
The occurrence key survives a lost response after admission and a scheduler restart.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from research.contracts import ResearchJobRequest
from research.policies import Principal, ResearchAccessDenied, principal_from_user
from research.scheduler import (
    ResearchScheduleStore, ResearchScheduler, execution_key_for, schedule_fingerprint,
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


async def admit_investigation(service: Any, request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    execution = require_research_context(context)
    if service is None:
        raise RuntimeError("Servicio research no montado")
    # Identity and replay keys belong to the persisted schedule, not its params.
    forbidden = {"project_key", "owner_id", "purpose", "idempotency_key", "mode"}
    if forbidden.intersection(request):
        raise ResearchAccessDenied("Los parametros no pueden reemplazar el scope del schedule")
    payload = ResearchJobRequest(
        **request, project_key=execution.project_key, owner_id=execution.owner_id,
        purpose=execution.purpose, mode="quick", idempotency_key=execution.execution_key,
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


def principal_resolver_for(repo: Any) -> Callable[[str], Any]:
    async def resolve(owner_id: str) -> Principal:
        # Read only current active users. all_permissions is the canonical effective
        # permission set maintained by auth, also used by the HTTP facade.
        user = await repo.db.users.find_one({"user_id": owner_id, "is_active": True})
        if user is None:
            raise ResearchAccessDenied("Owner ausente o inactivo")
        return principal_from_user(user)
    return resolve


class ResearchScheduleHandler:
    def __init__(self, *, repo: Any, workflows: Any, resolve_principal: Callable,
                 project_cfg_for: Callable, on_admitted: Callable | None = None,
                 now_fn: Callable | None = None) -> None:
        self.repo = repo
        self.workflows = workflows
        self.resolve_principal = resolve_principal
        self.project_cfg_for = project_cfg_for
        self.on_admitted = on_admitted
        self.now = now_fn or (lambda: datetime.now(timezone.utc))

    async def __call__(self, claimed: dict[str, Any]) -> dict[str, Any]:
        if claimed.get("workflow") != "research.investigate":
            raise ValueError("Workflow research no soportado por este runner")
        principal = await self.resolve_principal(str(claimed["owner_id"]))
        cfg = self.project_cfg_for(str(claimed["project_key"]))
        if inspect.isawaitable(cfg):
            cfg = await cfg
        # Check lifecycle/fencing after resolving auth: a pause during an awaited
        # lookup stops admission too. A job already admitted may finish normally.
        current = await self.repo.get_research_schedule(claimed["schedule_id"])
        if current is None or current.get("status") != "active":
            return {"skipped": "schedule_inactive"}
        if (current.get("run_state") != "running"
                or current.get("fencing_token") != claimed.get("fencing_token")
                or current.get("lease_owner") != claimed.get("lease_owner")):
            return {"skipped": "stale_claim"}
        lease = current.get("lease_expires_at")
        if lease is None or lease.replace(tzinfo=lease.tzinfo or timezone.utc) <= self.now():
            return {"skipped": "expired_lease"}
        scope_fields = ("project_key", "owner_id", "purpose")
        if (any(current.get(key) != claimed.get(key) for key in scope_fields)
                or schedule_fingerprint(current) != schedule_fingerprint(claimed)):
            raise ValueError("El spec cambio durante la toma; reintentar con el spec vigente")
        execution = ResearchExecutionContext(
            project_key=current["project_key"], owner_id=current["owner_id"],
            purpose=current["purpose"], principal=principal, project_cfg=cfg,
            execution_key=execution_key_for(claimed), on_admitted=self.on_admitted,
        )
        return await self.workflows.execute_research_workflow(
            current["workflow"], current.get("params") or {},
            context={"scope": "research", "research_context": execution},
        )


class ResearchScheduleRuntime:
    """One coordinator per process; disabled unless both explicit flags are enabled."""
    def __init__(self, *, repo: Any, workflows: Any, service: Any, settings: Any,
                 project_cfg_for: Callable, on_admitted: Callable | None = None,
                 resolve_principal: Callable | None = None) -> None:
        self.settings = settings
        self.service = service
        workflows.set_research_service(service)
        self.store = ResearchScheduleStore(repo, settings=settings)
        self.handler = ResearchScheduleHandler(
            repo=repo, workflows=workflows,
            resolve_principal=resolve_principal or principal_resolver_for(repo),
            project_cfg_for=project_cfg_for, on_admitted=on_admitted,
        )
        # No capped wait: absence of a due time means indefinite event wait.
        self.scheduler = ResearchScheduler(
            repo=repo, handler=self.handler, store=self.store, settings=settings,
            lease_seconds=getattr(settings, "RESEARCH_SCHEDULER_LEASE_SECONDS", 120),
            batch_limit=getattr(settings, "RESEARCH_SCHEDULER_BATCH_LIMIT", 20),
            backoff_base=getattr(settings, "RESEARCH_SCHEDULER_BACKOFF_SECONDS", 60),
        )

    async def start(self) -> bool:
        if not (getattr(self.settings, "RESEARCH_ENABLED", False)
                and getattr(self.settings, "RESEARCH_SCHEDULER_ENABLED", False)):
            return False
        await self.scheduler.start()
        return True

    async def stop(self) -> None:
        await self.scheduler.stop()
