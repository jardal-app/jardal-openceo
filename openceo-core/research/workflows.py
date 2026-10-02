"""Deterministic schedule admission, outside the business-task runner (TASK-2026-00082).

A tick admits a job; it does not wait for the model or complete a business Task.
Authorization is resolved again at each tick, never reconstructed from YAML params.
The occurrence key survives a lost response after admission and a scheduler restart.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from research.execution import (  # noqa: F401 - re-exportados: son la API historica de este modulo
    ResearchExecutionContext, admit_job, require_research_context,
)
from research.policies import ResearchAccessDenied, ResearchPolicyError, Principal, principal_from_user
from research.scheduler import (
    ResearchScheduleStore, ResearchScheduler, execution_key_for, schedule_fingerprint,
)
from research.watch import WORKFLOW_DISCOVERY, WORKFLOW_WATCH, WatchEngine

logger = logging.getLogger(__name__)

# Workflows que el runner de schedules sabe ejecutar. Cualquier otro se rechaza antes de
# resolver la autoridad: un schedule no puede apuntar a un workflow de negocio.
SUPPORTED_WORKFLOWS = frozenset({"research.investigate", WORKFLOW_WATCH, WORKFLOW_DISCOVERY})


async def admit_investigation(service: Any, request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    execution = require_research_context(context)
    return await admit_job(service, execution, request)


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
        if claimed.get("workflow") not in SUPPORTED_WORKFLOWS:
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
            schedule=current,
        )
        return await self.workflows.execute_research_workflow(
            current["workflow"], current.get("params") or {},
            context={"scope": "research", "research_context": execution},
        )


class ResearchScheduleRuntime:
    """One coordinator per process; disabled unless both explicit flags are enabled."""
    def __init__(self, *, repo: Any, workflows: Any, service: Any, settings: Any,
                 project_cfg_for: Callable, on_admitted: Callable | None = None,
                 resolve_principal: Callable | None = None,
                 adapters: dict[str, Any] | None = None, discover: Callable | None = None,
                 now_fn: Callable | None = None) -> None:
        self.settings = settings
        self.service = service
        self.repo = repo
        workflows.set_research_service(service)
        # Los adaptadores de fuente y el descubrimiento llegan montados desde wiring (mismo
        # lector con guard de egress, mismo ledger). Sin ellos los ticks lo declaran.
        self.watch = WatchEngine(repo=repo, settings=settings, service=service,
                                 adapters=adapters, discover=discover, now_fn=now_fn)
        workflows.set_research_watch(self.watch)
        self.store = ResearchScheduleStore(repo, settings=settings)
        self.handler = ResearchScheduleHandler(
            repo=repo, workflows=workflows,
            resolve_principal=resolve_principal or principal_resolver_for(repo),
            project_cfg_for=project_cfg_for, on_admitted=on_admitted,
        )
        # No capped wait: absence of a due time means indefinite event wait.
        self.scheduler = ResearchScheduler(
            repo=repo, handler=self._guarded, store=self.store, settings=settings,
            lease_seconds=getattr(settings, "RESEARCH_SCHEDULER_LEASE_SECONDS", 120),
            batch_limit=getattr(settings, "RESEARCH_SCHEDULER_BATCH_LIMIT", 20),
            backoff_base=getattr(settings, "RESEARCH_SCHEDULER_BACKOFF_SECONDS", 60),
        )

    async def _guarded(self, claimed: dict[str, Any]) -> dict[str, Any]:
        """Un acceso revocado PAUSA la suscripcion; un fallo transitorio se reintenta.

        Reintentar una denegacion solo repetiria una peticion que ya se sabe prohibida y
        la dejaria viva para siempre. Se pausa con el motivo para que el owner lo vea y,
        si recupera el acceso, la reanude explicitamente.
        """
        try:
            return await self.handler(claimed)
        except (ResearchAccessDenied, ResearchPolicyError) as exc:
            reason = "access_revoked" if isinstance(exc, ResearchAccessDenied) else "policy_denied"
            logger.warning("research: schedule %s pausado (%s)", claimed.get("schedule_id"), reason)
            # Los eventos `pending` se conservan: si el owner reanuda, se admiten entonces.
            await self.store.pause(str(claimed["schedule_id"]), reason=reason)
            return {"skipped": reason}

    async def start(self) -> bool:
        if not (getattr(self.settings, "RESEARCH_ENABLED", False)
                and getattr(self.settings, "RESEARCH_SCHEDULER_ENABLED", False)):
            return False
        await self.scheduler.start()
        return True

    async def stop(self) -> None:
        await self.scheduler.stop()
