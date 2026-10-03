from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import BulkWriteError, DuplicateKeyError

from horizon.privacy import mirror_exclusion_filter
from research.contracts import (
    TERMINAL_STATUSES,
    ResearchIdempotencyConflict,
    ResearchJobStatus,
    ResearchTransitionError,
    can_transition,
)
from settings import Settings
from state import DevSessionStatus, TaskStatus

logger = logging.getLogger(__name__)


class MongoRepository:
    """Persistence layer for task and dev-session lifecycle state."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncIOMotorClient | None = None

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(self._settings.MONGODB_URL)
        await self.ensure_indexes()

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def db(self):
        if self._client is None:
            raise RuntimeError("MongoRepository not connected")
        return self._client[self._settings.MONGODB_DATABASE]

    @property
    def tasks(self):
        return self.db["tasks"]

    @property
    def dev_sessions(self):
        return self.db["dev_sessions"]

    @property
    def user_context(self):
        return self.db["user_context"]

    @property
    def agent_status(self):
        return self.db["agent_status"]

    @property
    def browser_tasks(self):
        return self.db["browser_tasks"]

    @property
    def task_reviews(self):
        return self.db["task_reviews"]

    @property
    def workflow_runs(self):
        return self.db["workflow_runs"]

    @property
    def tier_s_runs(self):
        return self.db["tier_s_runs"]

    @property
    def repo_locks(self):
        return self.db["repo_locks"]

    @property
    def discovery_runs(self):
        return self.db["discovery_runs"]

    @property
    def users(self):
        return self.db["users"]

    @property
    def groups(self):
        return self.db["groups"]

    @property
    def counters(self):
        return self.db["counters"]

    @property
    def projects(self):
        return self.db["projects"]

    @property
    def project_presets(self):
        return self.db["project_presets"]

    # ------------------------- Research (TASK-2026-00056) -------------------------
    # Colecciones propias del servicio research. Deliberadamente separadas de
    # ``tasks``: un job de research NO es un ticket de negocio y no pasa por el
    # observador de estado de ERPNext.

    @property
    def research_jobs(self):
        return self.db["research_jobs"]

    @property
    def research_budget_accounts(self):
        return self.db["research_budget_accounts"]

    @property
    def research_usage(self):
        return self.db["research_usage"]

    @property
    def research_subscriptions(self):
        return self.db["research_subscriptions"]

    @property
    def research_schedules(self):
        return self.db["research_schedules"]

    @property
    def research_sessions(self):
        return self.db["research_sessions"]

    @property
    def research_watch_events(self):
        return self.db["research_watch_events"]

    # ------------------------- Horizon (espacio personal) -------------------------
    # Cada documento lleva user_id y su indice. El aislamiento no depende del
    # nombre del proyecto: vive en el filtro por propietario y en el gate de
    # privacidad de ``horizon.privacy``.

    @property
    def horizon_profile(self):
        return self.db["horizon_profile"]

    @property
    def horizon_objectives(self):
        return self.db["horizon_objectives"]

    @property
    def horizon_captures(self):
        return self.db["horizon_captures"]

    @property
    def horizon_actions(self):
        return self.db["horizon_actions"]

    @property
    def horizon_events(self):
        return self.db["horizon_events"]

    @property
    def horizon_plans(self):
        return self.db["horizon_plans"]

    @property
    def horizon_triggers(self):
        return self.db["horizon_triggers"]

    async def ensure_indexes(self) -> None:
        await self.tasks.create_index([("task_id", ASCENDING)], unique=True)
        await self.tasks.create_index([("status", ASCENDING), ("resume_at", ASCENDING)])
        await self.tasks.create_index([("project", ASCENDING), ("created_at", DESCENDING)])
        await self.tasks.create_index(
            [("project", ASCENDING), ("workflow_key", ASCENDING), ("created_at", DESCENDING)],
            name="idx_project_workflow_key",
        )
        await self.tasks.create_index(
            [("phase", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)],
            name="idx_phase_status_updated",
        )
        await self.tasks.create_index(
            [("source", ASCENDING), ("source_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"source_id": {"$gt": ""}},
        )
        # Index for mirror reconciliation queries
        await self.tasks.create_index(
            [("post_actions_executed", ASCENDING), ("source", ASCENDING), ("created_at", ASCENDING)],
            name="idx_mirror_pending",
        )
        # Unique ticket number (partial so existing nulls don't break)
        await self.tasks.create_index(
            [("ticket_number", ASCENDING)],
            unique=True,
            partialFilterExpression={"ticket_number": {"$exists": True}},
        )
        # Multikey index for user visibility across all roles
        await self.tasks.create_index([("involved_user_ids", ASCENDING)])
        # QA review queries
        await self.tasks.create_index(
            [("qa_review_required", ASCENDING), ("qa_review_state", ASCENDING), ("updated_at", DESCENDING)],
            name="idx_qa_review_pending",
        )

        await self.dev_sessions.create_index([("session_id", ASCENDING)], unique=True)
        await self.dev_sessions.create_index([("project", ASCENDING), ("created_at", DESCENDING)])
        await self.dev_sessions.create_index([("project", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING)])
        await self.dev_sessions.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

        await self.user_context.create_index([("user_id", ASCENDING)], unique=True)

        await self.agent_status.create_index([("updated_at", ASCENDING)])
        await self.browser_tasks.create_index([("task_id", ASCENDING)], unique=True)
        await self.browser_tasks.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        await self.task_reviews.create_index([("review_id", ASCENDING)], unique=True)
        await self.task_reviews.create_index([("task_id", ASCENDING), ("created_at", DESCENDING)])
        await self.task_reviews.create_index([("project", ASCENDING), ("created_at", DESCENDING)])

        await self.workflow_runs.create_index([("run_id", ASCENDING)], unique=True)
        await self.workflow_runs.create_index([("task_id", ASCENDING), ("created_at", DESCENDING)])
        await self.workflow_runs.create_index([("workflow", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
        await self.workflow_runs.create_index([("status", ASCENDING), ("updated_at", DESCENDING)])

        await self.tier_s_runs.create_index([("run_id", ASCENDING)], unique=True)
        await self.tier_s_runs.create_index([("owner_id", ASCENDING), ("updated_at", DESCENDING)])
        await self.tier_s_runs.create_index(
            [("active_repo", ASCENDING)], unique=True,
            partialFilterExpression={"active_repo": {"$type": "string"}},
        )
        await self.tier_s_runs.create_index([("cleanup_at", ASCENDING)])

        # Common execution lock: one document per repo. The TTL index is a
        # backstop for crashed holders; live code takes over expired locks
        # directly in acquire_repo_lock.
        await self.repo_locks.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)

        await self.discovery_runs.create_index([("discovery_id", ASCENDING)], unique=True)
        await self.discovery_runs.create_index([("task_id", ASCENDING), ("created_at", DESCENDING)])
        await self.discovery_runs.create_index([("status", ASCENDING), ("updated_at", DESCENDING)])

        # Auth indexes
        await self.users.create_index([("user_id", ASCENDING)], unique=True)
        await self.users.create_index([("username", ASCENDING)], unique=True)
        await self.users.create_index([("email", ASCENDING)], unique=True)
        await self.users.create_index([("is_client", ASCENDING), ("allowed_projects", ASCENDING)])
        await self.groups.create_index([("group_id", ASCENDING)], unique=True)
        await self.groups.create_index([("name", ASCENDING)], unique=True)

        # Projects index
        await self.projects.create_index([("name", ASCENDING)], unique=True)

        # Presets index
        await self.project_presets.create_index([("project", ASCENDING), ("name", ASCENDING)], unique=True)

        # Horizon: el propietario encabeza todos los indices porque toda
        # consulta del espacio personal filtra por user_id.
        await self.horizon_profile.create_index([("user_id", ASCENDING)], unique=True)
        await self.horizon_objectives.create_index(
            [("user_id", ASCENDING), ("status", ASCENDING), ("priority", ASCENDING)],
            name="idx_horizon_objectives_owner_status",
        )
        await self.horizon_objectives.create_index(
            [("user_id", ASCENDING), ("source_key", ASCENDING)],
            unique=True,
            partialFilterExpression={"source_key": {"$type": "string"}},
            name="idx_horizon_objectives_source_key",
        )
        await self.horizon_captures.create_index(
            [("user_id", ASCENDING), ("captured_at", DESCENDING)],
            name="idx_horizon_captures_owner_time",
        )
        # Deduplicacion de capturas: un pendiente repetido no crea trabajo dos veces.
        await self.horizon_captures.create_index(
            [("user_id", ASCENDING), ("dedupe_key", ASCENDING)],
            unique=True,
            name="idx_horizon_captures_dedupe",
        )
        await self.horizon_actions.create_index(
            [("user_id", ASCENDING), ("status", ASCENDING), ("priority", ASCENDING)],
            name="idx_horizon_actions_owner_status",
        )
        # Deduplicacion de eventos: repetir un recibo no duplica progreso.
        await self.horizon_events.create_index(
            [("user_id", ASCENDING), ("source_id", ASCENDING)],
            unique=True,
            name="idx_horizon_events_source",
        )
        await self.horizon_events.create_index(
            [("user_id", ASCENDING), ("occurred_at", DESCENDING)],
            name="idx_horizon_events_owner_time",
        )

        # Planes: el indice parcial es la garantia dura de que existe UNA sola
        # version vigente por usuario (seccion 9.10). El indice por (user_id,
        # version) es distinto: impide que dos escritores acunen la misma
        # version. Ninguno de los dos basta solo.
        await self.horizon_plans.create_index(
            [("user_id", ASCENDING)],
            unique=True,
            partialFilterExpression={"is_current": True},
            name="idx_horizon_plans_current",
        )
        await self.horizon_plans.create_index(
            [("user_id", ASCENDING), ("version", ASCENDING)],
            unique=True,
            name="idx_horizon_plans_version",
        )
        await self.horizon_plans.create_index(
            [("user_id", ASCENDING), ("date_key", DESCENDING), ("version", DESCENDING)],
            name="idx_horizon_plans_owner_date",
        )

        # Disparadores: (status, next_run_at) es la unica consulta del timer, y
        # la clave de deduplicacion impide que un evento repetido cree trabajo
        # dos veces.
        await self.horizon_triggers.create_index(
            [("status", ASCENDING), ("next_run_at", ASCENDING)],
            name="idx_horizon_triggers_due",
        )
        await self.horizon_triggers.create_index(
            [("user_id", ASCENDING), ("dedupe_key", ASCENDING)],
            unique=True,
            name="idx_horizon_triggers_dedupe",
        )
        await self.horizon_triggers.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)],
            name="idx_horizon_triggers_owner_time",
        )

        # ------------------------- Research -------------------------
        # Idempotencia de solicitudes: indice **parcial** solo para claves presentes.
        # Sin clave se admite un job nuevo (no se bloquean consultas nuevas con un
        # indice unico sobre un campo vacio) y la identidad queda scoped por
        # proyecto/owner/proposito. El campo se omite en el documento cuando no hay
        # clave, de modo que ``$type: string`` excluye exactamente esos casos.
        await self.research_jobs.create_index(
            [
                ("project_key", ASCENDING),
                ("owner_id", ASCENDING),
                ("purpose", ASCENDING),
                ("idempotency_key", ASCENDING),
            ],
            unique=True,
            partialFilterExpression={"idempotency_key": {"$type": "string"}},
            name="idx_research_jobs_idempotency",
        )
        await self.research_jobs.create_index(
            [("research_id", ASCENDING)], unique=True, name="idx_research_jobs_identity"
        )
        await self.research_jobs.create_index(
            [("status", ASCENDING), ("created_at", ASCENDING)],
            name="idx_research_jobs_queue",
        )
        await self.research_jobs.create_index(
            [("project_key", ASCENDING), ("owner_id", ASCENDING), ("created_at", DESCENDING)],
            name="idx_research_jobs_owner_time",
        )
        await self.research_jobs.create_index(
            [("lease_expires_at", ASCENDING)],
            name="idx_research_jobs_lease",
        )
        # Libro de uso: append-only e idempotente por operacion. Sin TTL: la
        # autoridad de gasto no se borra sola.
        await self.research_usage.create_index([("operation_id", ASCENDING)], unique=True)
        await self.research_usage.create_index(
            [("provider", ASCENDING), ("recorded_at", DESCENDING)],
            name="idx_research_usage_provider_time",
        )
        await self.research_usage.create_index(
            [("project_key", ASCENDING), ("recorded_at", DESCENDING)],
            name="idx_research_usage_project_time",
        )
        # La autoridad de saldo usa ``_id`` como clave natural (proveedor:cuenta).
        await self.research_subscriptions.create_index(
            [("project_key", ASCENDING), ("owner_id", ASCENDING), ("purpose", ASCENDING)],
            name="idx_research_subs_scope",
        )
        await self.research_sessions.create_index(
            [("research_id", ASCENDING), ("owner_id", ASCENDING)],
            name="idx_research_sessions_scope",
        )
        # Schedules (TASK-2026-00068). Sin TTL: la expiracion es un estado explicito y
        # un TTL borraria schedules activos. La toma es atomica sobre un solo documento,
        # y (status, run_state, next_run_at) es la unica consulta del timer.
        await self.research_schedules.create_index(
            [("schedule_id", ASCENDING)], unique=True, name="idx_research_schedules_identity"
        )
        await self.research_schedules.create_index(
            [("dedupe_key", ASCENDING)],
            unique=True,
            partialFilterExpression={"dedupe_key": {"$type": "string"}},
            name="idx_research_schedules_dedupe",
        )
        await self.research_schedules.create_index(
            [("status", ASCENDING), ("run_state", ASCENDING), ("next_run_at", ASCENDING)],
            name="idx_research_schedules_due",
        )
        await self.research_schedules.create_index(
            [("project_key", ASCENDING), ("owner_id", ASCENDING), ("created_at", DESCENDING)],
            name="idx_research_schedules_owner_time",
        )
        await self.research_schedules.create_index(
            [("run_state", ASCENDING), ("lease_expires_at", ASCENDING)],
            name="idx_research_schedules_lease",
        )
        # Retencion de jobs terminales (TASK-2026-00069). El campo ``expires_at`` solo
        # lo escribe la transicion a un estado terminal, asi que un job vivo (queued,
        # running) no tiene el campo y el TTL jamas lo toca. La dedup de ocurrencias
        # vive en ``research_watch_events``, no aqui: borrar un job no la pierde.
        await self.research_jobs.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
            name="idx_research_jobs_ttl",
        )
        # Eventos de suscripcion: dedup durable de items. La clave unica es la autoridad
        # de "ya lo vi"; el TTL es **deslizante** (ver ``touch_research_watch_events``)
        # para que un item que sigue listado en la fuente no se vuelva a emitir como
        # nuevo al vencer su registro.
        await self.research_watch_events.create_index(
            [("subscription_id", ASCENDING), ("event_key", ASCENDING)],
            unique=True,
            name="idx_research_watch_events_identity",
        )
        await self.research_watch_events.create_index(
            [("subscription_id", ASCENDING), ("item_key", ASCENDING)],
            name="idx_research_watch_events_item",
        )
        await self.research_watch_events.create_index(
            [("subscription_id", ASCENDING), ("status", ASCENDING), ("first_seen_at", ASCENDING)],
            name="idx_research_watch_events_pending",
        )
        await self.research_watch_events.create_index(
            [("expires_at", ASCENDING)],
            expireAfterSeconds=0,
            name="idx_research_watch_events_ttl",
        )

    # ------------------------- tasks -------------------------

    async def create_task(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self.tasks.insert_one(doc)
        return await self.get_task(doc["task_id"])

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = await self.tasks.find_one({"task_id": task_id})
        return self._clean(row)

    async def get_task_by_ticket_number(self, ticket_number: int, project: str | None = None) -> dict[str, Any] | None:
        query: dict[str, Any] = {"ticket_number": ticket_number}
        if project:
            query["project"] = project
        row = await self.tasks.find_one(query)
        if not row and project:
            # Fallback across projects if not found in current project
            row = await self.tasks.find_one({"ticket_number": ticket_number})
        return self._clean(row)

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.tasks.find({}, sort=[("created_at", -1)]).limit(limit)
        return [self._clean(row) async for row in rows]

    async def list_tasks_by_status(self, status: TaskStatus, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.tasks.find({"status": status.value}, sort=[("created_at", 1)]).limit(limit)
        return [self._clean(row) async for row in rows]

    async def list_tasks_missing_notion_mirror(self, limit: int = 200) -> list[dict[str, Any]]:
        query = {
            "source": {"$ne": "notion"},
            "post_actions_executed": {"$ne": "notion_mirror_created"},
            # El espacio personal de Horizon nunca se proyecta a un espejo
            # externo. Sin este filtro, el safety net de arranque publicaria
            # datos privados en el primer reinicio del servicio.
            **mirror_exclusion_filter(),
            "$or": [
                {"params._mirrors.notion.page_id": {"$exists": False}},
                {"params._mirrors.notion.page_id": ""},
                {"params": {"$exists": False}},
            ],
        }
        rows = self.tasks.find(query, sort=[("created_at", 1)]).limit(limit)
        return [self._clean(row) async for row in rows]

    async def patch_task(self, task_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not updates:
            return await self.get_task(task_id)

        updates["updated_at"] = datetime.now(timezone.utc)
        await self.tasks.update_one({"task_id": task_id}, {"$set": updates})
        task = await self.get_task(task_id)
        await self._notify_status_observer(task, updates)
        return task

    def set_status_observer(self, observer: Any | None) -> None:
        """Instala el observador de cambios de estado (uno solo, opcional).

        El observador se asigna desde fuera y se lee con `getattr` dentro de
        `_notify_status_observer`, nunca se crea en `__init__`: los dobles de test
        construyen el repositorio sin llamar a `super().__init__()`, y un atributo del
        constructor no existiria ahi.
        """
        self._status_observer = observer

    async def _notify_status_observer(self, task: dict[str, Any] | None, updates: dict[str, Any]) -> None:
        """Avisa de una transicion de estado para propagarla al sistema de negocio.

        Violacion de capas consciente: la persistencia dispara una notificacion. Se hace
        AQUI y no en `TaskService.patch_task` porque este metodo es el unico camino por el
        que pasan TODAS las transiciones, incluidas las que se saltan el embudo oficial
        (el gate de rate limit, `run_task`, la revision de QA, el outcome de un workflow).
        Engancharse en el embudo perderia esas transiciones en silencio.

        El observador es opcional, se ejecuta fuera del camino critico y no puede lanzar:
        un fallo suyo no debe romper un `patch_task`.
        """
        if task is None or "status" not in updates:
            return
        observer = getattr(self, "_status_observer", None)
        if observer is None:
            return
        try:
            await observer(task)
        except Exception:
            logger.warning(
                "El observador de estado fallo para el ticket %s",
                task.get("task_id"),
                exc_info=True,
            )

    async def due_waiting_tasks(self, now_utc: datetime) -> list[dict[str, Any]]:
        rows = self.tasks.find(
            {
                "status": TaskStatus.WAITING.value,
                "resume_at": {"$lte": now_utc},
            },
            sort=[("resume_at", ASCENDING)],
        )
        return [self._clean(row) async for row in rows]

    async def list_tasks_needing_erpnext_sync(self, limit: int = 200) -> list[dict[str, Any]]:
        """Tickets con el enlace de negocio roto, para reconciliar al arrancar.

        Solo mira tickets donde la integracion **ya intento** enlazar (existe
        `params._mirrors.erpnext`). NO hace backfill de los tickets historicos que nunca
        pasaron por ERPNext: eso inventaria registros de negocio hacia atras, que es justo
        lo que el contrato prohibe. Es reparacion de intentos propios, no migracion.

        Lleva el filtro de privacidad de Horizon desde el primer commit. Sin el, la
        reconciliacion publicaria datos personales en el primer reinicio — el mismo fallo
        que ya se corrigio para el espejo de Notion (`tests/test_horizon_privacy.py`).

        Acotada por `limit`: es reconciliacion de arranque, no un barrido periodico.
        """
        query: dict[str, Any] = {
            "params._mirrors.erpnext": {"$exists": True},
            "$or": [
                {"params._mirrors.erpnext.name": {"$in": [None, ""]}},
                {"params._mirrors.erpnext.pending_status": {"$ne": None}},
            ],
            **mirror_exclusion_filter(),
        }
        rows = self.tasks.find(query).limit(limit)
        return [self._clean(row) async for row in rows]

    async def mark_interrupted_tasks(self) -> int:
        tier_result = await self.tasks.update_many(
            {"status": TaskStatus.IN_PROGRESS.value, "$or": [
                {"coding_tool": "tier_s"}, {"params.coding_tool": "tier_s"},
            ]},
            {"$set": {"status": TaskStatus.WAITING.value,
                      "waiting_reason": "Tier S pausado por reinicio; relanza el ticket para reanudar el mismo intento",
                      "resume_at": None, "updated_at": datetime.now(timezone.utc)}},
        )
        result = await self.tasks.update_many(
            {"status": TaskStatus.IN_PROGRESS.value},
            {
                "$set": {
                    "status": TaskStatus.INTERRUPTED.value,
                    "error": "Service restart interrupted task execution",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count + tier_result.modified_count

    async def find_task_by_source(self, source: str, source_id: str) -> dict[str, Any] | None:
        row = await self.tasks.find_one({"source": source, "source_id": source_id})
        return self._clean(row)

    async def create_task_review(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self.task_reviews.insert_one(doc)
        return await self.get_task_review(doc["review_id"])

    async def get_task_review(self, review_id: str) -> dict[str, Any] | None:
        row = await self.task_reviews.find_one({"review_id": review_id})
        return self._clean(row)

    async def list_task_reviews(self, task_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.task_reviews.find({"task_id": task_id}, sort=[("created_at", -1)]).limit(limit)
        return [self._clean(row) async for row in rows]

    # ------------------------- workflow runs -------------------------

    # ------------------------- Tier S attempts -------------------------

    async def create_tier_s_run(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self.tier_s_runs.insert_one(doc)
        return self._clean(doc)

    async def get_tier_s_run(self, run_id: str) -> dict[str, Any] | None:
        return self._clean(await self.tier_s_runs.find_one({"run_id": run_id}))

    async def active_tier_s_run(self, owner_id: str) -> dict[str, Any] | None:
        row = await self.tier_s_runs.find_one(
            {"owner_id": owner_id, "active_repo": {"$type": "string"}},
            sort=[("updated_at", DESCENDING)],
        )
        return self._clean(row)

    async def active_tier_s_run_for_repo(self, repo_path: str) -> dict[str, Any] | None:
        return self._clean(await self.tier_s_runs.find_one({"active_repo": repo_path}))

    # ------------------------- Repo execution lock -------------------------

    async def acquire_repo_lock(self, repo: str, holder: str, ttl_seconds: int = 86400) -> dict[str, Any] | None:
        """Atomically take the per-repo execution lock shared by every tier.

        Tier S design §11.1. Re-acquiring as the same ``holder`` refreshes
        the lease. Returns the lock document, or None when another live
        holder owns it.
        """
        now = datetime.now(timezone.utc)
        try:
            row = await self.repo_locks.find_one_and_update(
                {"_id": repo, "$or": [{"holder": holder}, {"expires_at": {"$lte": now}}]},
                {"$set": {"holder": holder, "acquired_at": now,
                          "expires_at": now + timedelta(seconds=ttl_seconds)}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # No match but the _id exists (or a concurrent upsert won): the
            # lock is held by another live holder.
            return None
        if row is not None and row.get("holder") == holder:
            return self._clean(row)
        return None

    async def release_repo_lock(self, repo: str, holder: str) -> bool:
        """Release the repo lock, but only if ``holder`` owns it."""
        result = await self.repo_locks.delete_one({"_id": repo, "holder": holder})
        return result.deleted_count == 1

    async def touch_repo_lock(self, repo: str, holder: str, ttl_seconds: int = 86400) -> bool:
        """Extend a lock owned by ``holder`` (waiting retains the lock)."""
        now = datetime.now(timezone.utc)
        result = await self.repo_locks.update_one(
            {"_id": repo, "holder": holder},
            {"$set": {"expires_at": now + timedelta(seconds=ttl_seconds)}},
        )
        return result.modified_count == 1

    async def repo_lock_holder(self, repo: str) -> dict[str, Any] | None:
        return self._clean(await self.repo_locks.find_one({"_id": repo}))

    async def interrupted_tier_s_runs(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.tier_s_runs.find({"active_repo": {"$type": "string"},
                                     "status": {"$in": ["active", "running", "resuming"]}}).limit(limit)
        return [self._clean(row) async for row in rows]

    async def list_tier_s_waits(self, project: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.tier_s_runs.find(
            {"project": project, "status": "waiting"},
            sort=[("updated_at", DESCENDING)],
        ).limit(limit)
        return [self._clean(row) async for row in rows]

    async def answer_tier_s_question(self, owner_id: str, answer: str) -> dict[str, Any] | None:
        row = await self.tier_s_runs.find_one_and_update(
            {"owner_id": owner_id, "active_repo": {"$type": "string"},
             "status": "waiting", "question": {"$type": "object"},
             "human_answer": None},
            {"$set": {"human_answer": answer, "status": "resuming",
                      "updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def patch_tier_s_run(self, run_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        patch = {**updates, "updated_at": datetime.now(timezone.utc)}
        await self.tier_s_runs.update_one({"run_id": run_id}, {"$set": patch})
        return await self.get_tier_s_run(run_id)

    async def expired_tier_s_runs(self, now: datetime, repo_path: str,
                                  limit: int = 50) -> list[dict[str, Any]]:
        rows = self.tier_s_runs.find({"cleanup_at": {"$lte": now}, "active_repo": None,
                                     "repo_path": repo_path}).limit(limit)
        return [self._clean(row) async for row in rows]

    async def expired_tier_s_runs_any(self, now: datetime, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.tier_s_runs.find({"cleanup_at": {"$lte": now}, "active_repo": None}).limit(limit)
        return [self._clean(row) async for row in rows]

    async def create_workflow_run(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self.workflow_runs.insert_one(doc)
        return await self.get_workflow_run(doc["run_id"])

    async def get_workflow_run(self, run_id: str) -> dict[str, Any] | None:
        row = await self.workflow_runs.find_one({"run_id": run_id})
        return self._clean(row)

    async def list_workflow_runs(
        self,
        *,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if task_id:
            query["task_id"] = task_id
        if status:
            query["status"] = status
        rows = self.workflow_runs.find(query, sort=[("created_at", -1)]).limit(limit)
        return [self._clean(row) async for row in rows]

    async def latest_workflow_run_for_task(self, task_id: str) -> dict[str, Any] | None:
        row = await self.workflow_runs.find_one({"task_id": task_id}, sort=[("created_at", DESCENDING)])
        return self._clean(row)

    async def patch_workflow_run(self, run_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not updates:
            return await self.get_workflow_run(run_id)
        updates["updated_at"] = datetime.now(timezone.utc)
        await self.workflow_runs.update_one({"run_id": run_id}, {"$set": updates})
        return await self.get_workflow_run(run_id)

    # ------------------------- discovery runs -------------------------

    async def create_discovery_run(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self.discovery_runs.insert_one(doc)
        return await self.get_discovery_run(doc["discovery_id"])

    async def get_discovery_run(self, discovery_id: str) -> dict[str, Any] | None:
        row = await self.discovery_runs.find_one({"discovery_id": discovery_id})
        return self._clean(row)

    async def list_discovery_runs(
        self,
        *,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if task_id:
            query["task_id"] = task_id
        if status:
            query["status"] = status
        rows = self.discovery_runs.find(query, sort=[("created_at", -1)]).limit(limit)
        return [self._clean(row) async for row in rows]

    async def patch_discovery_run(self, discovery_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not updates:
            return await self.get_discovery_run(discovery_id)
        updates["updated_at"] = datetime.now(timezone.utc)
        await self.discovery_runs.update_one({"discovery_id": discovery_id}, {"$set": updates})
        return await self.get_discovery_run(discovery_id)

    # ------------------------- dev sessions -------------------------

    async def create_dev_session(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self.dev_sessions.insert_one(doc)
        return await self.get_dev_session(doc["session_id"])

    async def get_dev_session(self, session_id: str) -> dict[str, Any] | None:
        row = await self.dev_sessions.find_one({"session_id": session_id})
        return self._clean(row)

    async def list_dev_sessions(
        self,
        project: str | None = None,
        user_id: str | None = None,
        status: DevSessionStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if project:
            query["project"] = project
        if user_id:
            query["user_id"] = user_id
        if status:
            query["status"] = status.value

        rows = self.dev_sessions.find(query, sort=[("created_at", -1)]).limit(limit)
        return [self._clean(row) async for row in rows]

    async def patch_dev_session(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not updates:
            return await self.get_dev_session(session_id)

        updates["updated_at"] = datetime.now(timezone.utc)
        await self.dev_sessions.update_one({"session_id": session_id}, {"$set": updates})
        return await self.get_dev_session(session_id)

    async def append_dev_message(self, session_id: str, message: dict[str, Any]) -> dict[str, Any] | None:
        await self.dev_sessions.update_one(
            {"session_id": session_id},
            {
                "$push": {"messages": message},
                "$set": {"updated_at": datetime.now(timezone.utc)},
            },
        )
        return await self.get_dev_session(session_id)

    async def find_active_project_session(
        self,
        project: str,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        query: dict[str, Any] = {
            "project": project,
            "status": {
                "$in": [
                    DevSessionStatus.RUNNING.value,
                    DevSessionStatus.WAITING_USER.value,
                ]
            },
        }
        if user_id:
            query["user_id"] = user_id

        row = await self.dev_sessions.find_one(
            query,
            sort=[("created_at", 1)],
        )
        return self._clean(row)

    async def find_latest_user_open_session(
        self,
        user_id: str,
        project: str | None = None,
    ) -> dict[str, Any] | None:
        query: dict[str, Any] = {
            "user_id": user_id,
            "status": {
                "$in": [
                    DevSessionStatus.RUNNING.value,
                    DevSessionStatus.WAITING_USER.value,
                ]
            },
        }
        if project:
            query["project"] = project

        row = await self.dev_sessions.find_one(query, sort=[("updated_at", DESCENDING)])
        return self._clean(row)

    async def find_latest_user_session(
        self,
        user_id: str,
        project: str | None = None,
    ) -> dict[str, Any] | None:
        query: dict[str, Any] = {"user_id": user_id}
        if project:
            query["project"] = project
        row = await self.dev_sessions.find_one(query, sort=[("updated_at", DESCENDING)])
        return self._clean(row)

    async def mark_interrupted_dev_sessions(self) -> int:
        result = await self.dev_sessions.update_many(
            {
                "status": {
                    "$in": [
                        DevSessionStatus.RUNNING.value,
                    ]
                }
            },
            {
                "$set": {
                    "status": DevSessionStatus.WAITING_USER.value,
                    "waiting_reason": "Service restart: session paused, waiting for user resume",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return result.modified_count

    # ------------------------- project selection -------------------------

    async def set_selected_project(self, user_id: str, project: str) -> dict[str, Any]:
        payload = {
            "user_id": user_id,
            "selected_project": project,
            "updated_at": datetime.now(timezone.utc),
        }
        await self.user_context.update_one({"user_id": user_id}, {"$set": payload}, upsert=True)
        return payload

    async def get_selected_project(self, user_id: str) -> str | None:
        row = await self.user_context.find_one({"user_id": user_id})
        if not row:
            return None
        return str(row.get("selected_project")) if row.get("selected_project") else None

    async def set_active_dev_chat_session(self, user_id: str, session_id: str, project: str) -> dict[str, Any]:
        payload = {
            "user_id": user_id,
            "active_dev_session_id": session_id,
            "active_dev_project": project,
            "active_dev_mode": "session",
            "updated_at": datetime.now(timezone.utc),
        }
        await self.user_context.update_one({"user_id": user_id}, {"$set": payload}, upsert=True)
        # Also persist in project_sessions map so we can restore when switching projects
        await self.user_context.update_one(
            {"user_id": user_id},
            {"$set": {f"project_sessions.{project}": session_id}},
        )
        row = await self.user_context.find_one({"user_id": user_id})
        return self._clean(row) or payload

    async def clear_active_dev_chat_session(self, user_id: str) -> dict[str, Any]:
        await self.user_context.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "active_dev_mode": "conversation",
                    "updated_at": datetime.now(timezone.utc),
                },
                "$unset": {
                    "active_dev_session_id": "",
                    "active_dev_project": "",
                },
            },
            upsert=True,
        )
        row = await self.user_context.find_one({"user_id": user_id})
        return self._clean(row) or {"user_id": user_id, "active_dev_mode": "conversation"}

    async def get_active_dev_chat_session(
        self,
        user_id: str,
    ) -> tuple[str | None, str | None]:
        row = await self.user_context.find_one({"user_id": user_id})
        if not row:
            return None, None
        session_id = str(row.get("active_dev_session_id")) if row.get("active_dev_session_id") else None
        project = str(row.get("active_dev_project")) if row.get("active_dev_project") else None
        return session_id, project

    async def set_active_ticket(
        self,
        user_id: str,
        ticket_number: int,
        task_id: str,
        project: str,
    ) -> dict[str, Any]:
        payload = {
            "active_ticket_number": ticket_number,
            "active_ticket_id": task_id,
            "active_ticket_project": project,
            "updated_at": datetime.now(timezone.utc),
        }
        await self.user_context.update_one({"user_id": user_id}, {"$set": payload}, upsert=True)
        return payload

    async def get_active_ticket(self, user_id: str) -> dict[str, Any] | None:
        row = await self.user_context.find_one({"user_id": user_id})
        if not row or not row.get("active_ticket_id"):
            return None
        return {
            "ticket_number": row.get("active_ticket_number"),
            "task_id": row.get("active_ticket_id"),
            "project": row.get("active_ticket_project"),
        }

    async def clear_active_ticket(self, user_id: str) -> None:
        await self.user_context.update_one(
            {"user_id": user_id},
            {
                "$unset": {
                    "active_ticket_number": "",
                    "active_ticket_id": "",
                    "active_ticket_project": "",
                },
                "$set": {
                    "updated_at": datetime.now(timezone.utc),
                },
            },
        )

    async def set_pm_state(self, user_id: str, state: dict[str, Any]) -> None:
        await self.user_context.update_one(
            {"user_id": user_id},
            {"$set": {"pm_state": state, "updated_at": datetime.now(timezone.utc)}},
            upsert=True,
        )

    async def get_pm_state(self, user_id: str) -> dict[str, Any] | None:
        row = await self.user_context.find_one({"user_id": user_id})
        if not row:
            return None
        return row.get("pm_state")

    async def clear_pm_state(self, user_id: str) -> None:
        await self.user_context.update_one(
            {"user_id": user_id},
            {"$unset": {"pm_state": ""}},
        )

    async def mark_interrupted_pm_states(self) -> int:
        now = datetime.now(timezone.utc)
        message = "Service restart interrupted PM background job"
        result = await self.user_context.update_many(
            {"pm_state.step": "background_running"},
            {
                "$set": {
                    "pm_state.step": "background_failed",
                    "pm_state.error": message,
                    "pm_state.summary": message,
                    "pm_state.job.status": "failed",
                    "pm_state.job.error": message,
                    "pm_state.job.completed_at": now,
                    "updated_at": now,
                }
            },
        )
        return result.modified_count

    async def get_project_session(self, user_id: str, project: str) -> str | None:
        """Return the saved session_id for a specific project, if any."""
        row = await self.user_context.find_one({"user_id": user_id})
        if not row:
            return None
        sessions_map = row.get("project_sessions") or {}
        val = sessions_map.get(project)
        return str(val) if val else None

    async def get_all_project_sessions(self, user_id: str) -> dict[str, str]:
        """Return {project: session_id} for all tracked projects."""
        row = await self.user_context.find_one({"user_id": user_id})
        if not row:
            return {}
        return dict(row.get("project_sessions") or {})

    async def clear_project_session(self, user_id: str, project: str) -> None:
        """Remove a single project entry from project_sessions."""
        await self.user_context.update_one(
            {"user_id": user_id},
            {"$unset": {f"project_sessions.{project}": ""}},
        )

    # ------------------------- pending preset (waiting for dictation) -------------------------

    async def get_pending_preset(self, user_id: str) -> dict[str, Any] | None:
        """Get pending preset waiting for dictation."""
        row = await self.user_context.find_one({"user_id": user_id})
        if not row:
            return None
        return row.get("pending_preset")

    async def set_pending_preset(
        self,
        user_id: str,
        preset_name: str,
        session_name: str,
        initial_prompt: str,
        project: str,
    ) -> None:
        """Store pending preset waiting for continuation / dictation."""
        doc = {
            "name": preset_name,
            "session_name": session_name,
            "initial_prompt": initial_prompt,
            "project": project,
            "created_at": datetime.now(timezone.utc),
        }
        await self.user_context.update_one(
            {"user_id": user_id},
            {"$set": {"pending_preset": doc}},
            upsert=True,
        )

    async def clear_pending_preset(self, user_id: str) -> None:
        """Clear pending preset."""
        await self.user_context.update_one(
            {"user_id": user_id},
            {"$unset": {"pending_preset": ""}},
        )

    async def get_next_ticket_number(self) -> int:
        """Atomically increment and return the next ticket number."""
        result = await self.counters.find_one_and_update(
            {"_id": "ticket_number"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(result["seq"])

    # ------------------------- project presets -------------------------

    async def get_project_presets(self, project: str) -> list[dict[str, Any]]:
        """Return all dynamic presets saved for a project in MongoDB."""
        rows = self.project_presets.find({"project": project}, sort=[("name", 1)])
        return [self._clean(r) async for r in rows]

    async def save_project_preset(
        self,
        project: str,
        name: str,
        prompt: str = "",
        cmd: str = "",
        description: str = "",
        tmux: bool = True,
    ) -> dict[str, Any]:
        """Upsert a project preset (prompt for dev tmux session)."""
        prompt_val = (prompt or cmd).strip()
        doc = {
            "project": project,
            "name": name.strip().lower(),
            "prompt": prompt_val,
            "cmd": prompt_val,
            "description": description.strip(),
            "tmux": bool(tmux),
            "updated_at": datetime.now(timezone.utc),
        }
        await self.project_presets.update_one(
            {"project": project, "name": doc["name"]},
            {"$set": doc},
            upsert=True,
        )
        row = await self.project_presets.find_one({"project": project, "name": doc["name"]})
        return self._clean(row)

    async def delete_project_preset(self, project: str, name: str) -> bool:
        """Delete a project preset."""
        res = await self.project_presets.delete_one({"project": project, "name": name.strip().lower()})
        return res.deleted_count > 0

    # ------------------------- open tasks helpers -------------------------

    async def get_open_tasks_for_project(self, project: str, limit: int = 10) -> list[dict[str, Any]]:
        """Get active non-completed tasks for a project."""
        query = {
            "project": project,
            "status": {"$in": ["queued", "in_progress", "waiting", "escalated"]},
        }
        rows = self.tasks.find(query, sort=[("created_at", -1)]).limit(limit)
        return [self._clean(r) async for r in rows]

    async def count_open_tasks_for_project(self, project: str) -> int:
        """Count active non-completed tasks for a project."""
        query = {
            "project": project,
            "status": {"$in": ["queued", "in_progress", "waiting", "escalated"]},
        }
        return await self.tasks.count_documents(query)

    # ------------------------- Horizon (espacio personal) -------------------------
    # Toda lectura y escritura filtra por user_id: es la unica barrera que
    # separa el espacio personal del resto de la organizacion.

    async def get_horizon_profile(self, user_id: str) -> dict[str, Any] | None:
        row = await self.horizon_profile.find_one({"user_id": user_id})
        return self._clean(row)

    async def upsert_horizon_profile(self, doc: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        payload = dict(doc)
        payload.pop("_id", None)
        # created_at solo puede vivir en $setOnInsert: Mongo rechaza el mismo
        # campo en $set y $setOnInsert ("would create a conflict"). Asi la fecha
        # de alta se fija una vez y una reimportacion no la reescribe.
        payload.pop("created_at", None)
        await self.horizon_profile.update_one(
            {"user_id": payload["user_id"]},
            {"$set": {**payload, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        return await self.get_horizon_profile(payload["user_id"]) or payload

    async def list_horizon_objectives(
        self,
        user_id: str,
        *,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"user_id": user_id}
        if statuses:
            query["status"] = {"$in": statuses}
        rows = self.horizon_objectives.find(query, sort=[("priority", ASCENDING)])
        return [self._clean(row) async for row in rows]

    async def get_horizon_objective_by_source_key(
        self,
        user_id: str,
        source_key: str,
    ) -> dict[str, Any] | None:
        if not source_key:
            return None
        row = await self.horizon_objectives.find_one({"user_id": user_id, "source_key": source_key})
        return self._clean(row)

    async def upsert_horizon_objective(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Upsert por clave natural. La importacion nunca duplica objetivos."""
        now = datetime.now(timezone.utc)
        payload = dict(doc)
        payload.pop("_id", None)
        payload.pop("created_at", None)
        key = {"user_id": payload["user_id"], "source_key": payload.get("source_key")}
        await self.horizon_objectives.update_one(
            key,
            {"$set": {**payload, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )
        row = await self.horizon_objectives.find_one(key)
        return self._clean(row) or payload

    async def update_horizon_milestone(
        self,
        *,
        user_id: str,
        objective_id: str,
        milestone_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Actualiza un hito concreto preservando el resto del objetivo."""
        now = datetime.now(timezone.utc)
        set_fields = {f"milestones.$.{key}": value for key, value in updates.items()}
        set_fields["updated_at"] = now
        result = await self.horizon_objectives.update_one(
            {
                "user_id": user_id,
                "objective_id": objective_id,
                "milestones.milestone_id": milestone_id,
            },
            {"$set": set_fields},
        )
        if result.matched_count == 0:
            return None
        objective = await self.horizon_objectives.find_one(
            {"user_id": user_id, "objective_id": objective_id}
        )
        clean = self._clean(objective)
        if not clean:
            return None
        for milestone in clean.get("milestones") or []:
            if str(milestone.get("milestone_id")) == milestone_id:
                return milestone
        return None

    async def create_horizon_action(self, doc: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        payload = dict(doc)
        payload.pop("_id", None)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        await self.horizon_actions.insert_one(dict(payload))
        return payload

    async def list_horizon_actions(
        self,
        user_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"user_id": user_id}
        if statuses:
            query["status"] = {"$in": statuses}
        rows = self.horizon_actions.find(query, sort=[("priority", ASCENDING)]).limit(limit)
        return [self._clean(row) async for row in rows]

    async def count_horizon_actions(self, user_id: str) -> int:
        return await self.horizon_actions.count_documents({"user_id": user_id})

    async def create_horizon_capture(self, doc: dict[str, Any]) -> dict[str, Any]:
        payload = dict(doc)
        payload.pop("_id", None)
        await self.horizon_captures.insert_one(dict(payload))
        return payload

    async def get_horizon_capture_by_dedupe_key(
        self,
        user_id: str,
        dedupe_key: str,
    ) -> dict[str, Any] | None:
        row = await self.horizon_captures.find_one({"user_id": user_id, "dedupe_key": dedupe_key})
        return self._clean(row)

    async def list_horizon_captures(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.horizon_captures.find(
            {"user_id": user_id}, sort=[("captured_at", DESCENDING)]
        ).limit(limit)
        return [self._clean(row) async for row in rows]

    async def create_horizon_event(self, doc: dict[str, Any]) -> dict[str, Any]:
        payload = dict(doc)
        payload.pop("_id", None)
        await self.horizon_events.insert_one(dict(payload))
        return payload

    async def get_horizon_event_by_source(
        self,
        user_id: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        row = await self.horizon_events.find_one({"user_id": user_id, "source_id": source_id})
        return self._clean(row)

    # ------------------------- Horizon: planes -------------------------

    async def create_horizon_plan(self, doc: dict[str, Any]) -> dict[str, Any]:
        payload = dict(doc)
        payload.pop("_id", None)
        payload.setdefault("is_current", False)
        await self.horizon_plans.insert_one(dict(payload))
        return payload

    async def get_horizon_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = await self.horizon_plans.find_one({"plan_id": plan_id})
        return self._clean(row)

    async def get_current_horizon_plan(self, user_id: str) -> dict[str, Any] | None:
        """Devuelve el plan vigente, con respaldo por version.

        Publicar es marcar-y-luego-insertar, asi que existe una ventana de
        milisegundos sin ningun plan marcado (y permanente si el proceso muere
        ahi). El respaldo por version mas alta cierra las dos: desde el lector,
        siempre hay exactamente un plan vigente.
        """
        row = await self.horizon_plans.find_one({"user_id": user_id, "is_current": True})
        if row is not None:
            return self._clean(row)
        fallback = await self.horizon_plans.find_one(
            {"user_id": user_id}, sort=[("version", DESCENDING)]
        )
        return self._clean(fallback)

    async def list_recent_horizon_plans(
        self, user_id: str, limit: int = 2
    ) -> list[dict[str, Any]]:
        rows = (
            self.horizon_plans.find({"user_id": user_id}, sort=[("version", DESCENDING)])
            .limit(max(1, int(limit)))
        )
        return [self._clean(row) async for row in rows]

    async def next_horizon_plan_version(self, user_id: str) -> int:
        row = await self.horizon_plans.find_one(
            {"user_id": user_id}, sort=[("version", DESCENDING)]
        )
        if not row:
            return 1
        return int(row.get("version") or 0) + 1

    async def set_current_horizon_plan(
        self, user_id: str, plan_id: str
    ) -> dict[str, Any] | None:
        """Marca un plan como vigente y retira el anterior.

        El orden importa: primero se retira el vigente y despues se marca el
        nuevo. Al reves, el indice unico parcial lanzaria ``DuplicateKeyError``
        mientras el anterior siguiera marcado, y la excepcion seria el camino
        normal en vez de la excepcion.
        """
        now = datetime.now(timezone.utc)
        try:
            await self.horizon_plans.update_many(
                {"user_id": user_id, "is_current": True, "plan_id": {"$ne": plan_id}},
                {"$set": {"is_current": False, "superseded_by": plan_id, "superseded_at": now}},
            )
            result = await self.horizon_plans.update_one(
                {"user_id": user_id, "plan_id": plan_id},
                {"$set": {"is_current": True, "superseded_by": None, "superseded_at": None}},
            )
        except DuplicateKeyError:
            # Otro escritor gano la carrera. Lo obsoleto se descarta; que se
            # recoloque es decision de quien publica, no de esta capa.
            return None
        if result.matched_count == 0:
            return None
        return await self.get_horizon_plan(plan_id)

    async def patch_horizon_plan(
        self, plan_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        payload = dict(updates)
        payload.pop("_id", None)
        await self.horizon_plans.update_one({"plan_id": plan_id}, {"$set": payload})
        return await self.get_horizon_plan(plan_id)

    async def push_horizon_plan_aviso(
        self, plan_id: str, aviso: dict[str, Any], *, keep: int = 20
    ) -> dict[str, Any] | None:
        """Agrega un aviso al plan, conservando solo los ultimos.

        Es lo que permite resolver un "hecho" tardio contra la accion que se le
        mostro al usuario, sin una tercera coleccion.
        """
        await self.horizon_plans.update_one(
            {"plan_id": plan_id},
            {"$push": {"avisos": {"$each": [dict(aviso)], "$slice": -abs(int(keep))}}},
        )
        return await self.get_horizon_plan(plan_id)

    # ------------------------- Horizon: disparadores -------------------------

    async def create_horizon_trigger(self, doc: dict[str, Any]) -> dict[str, Any] | None:
        payload = dict(doc)
        payload.pop("_id", None)
        now = datetime.now(timezone.utc)
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        try:
            await self.horizon_triggers.insert_one(dict(payload))
        except DuplicateKeyError:
            # La clave natural ya existia: no es un error, es la deduplicacion
            # haciendo su trabajo.
            return None
        return payload

    async def get_horizon_trigger_by_dedupe(self, dedupe_key: str) -> dict[str, Any] | None:
        row = await self.horizon_triggers.find_one({"dedupe_key": dedupe_key})
        return self._clean(row)

    async def list_due_horizon_triggers(
        self, now: datetime, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = (
            self.horizon_triggers.find(
                {"status": "pending", "next_run_at": {"$lte": now}},
                sort=[("next_run_at", ASCENDING)],
            ).limit(max(1, int(limit)))
        )
        return [self._clean(row) async for row in rows]

    async def next_horizon_trigger_due_at(self) -> datetime | None:
        rows = (
            self.horizon_triggers.find({"status": "pending"}, sort=[("next_run_at", ASCENDING)])
            .limit(1)
        )
        async for row in rows:
            return row.get("next_run_at")
        return None

    async def mark_horizon_trigger_running(self, trigger_id: str) -> dict[str, Any] | None:
        """Toma atomica del disparador. Devuelve None si otro ya lo tomo."""
        now = datetime.now(timezone.utc)
        row = await self.horizon_triggers.find_one_and_update(
            {"trigger_id": trigger_id, "status": "pending"},
            {"$set": {"status": "running", "started_at": now, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def patch_horizon_trigger(
        self, trigger_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        payload = dict(updates)
        payload.pop("_id", None)
        payload["updated_at"] = datetime.now(timezone.utc)
        row = await self.horizon_triggers.find_one_and_update(
            {"trigger_id": trigger_id},
            {"$set": payload},
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def list_horizon_triggers(
        self, user_id: str, *, statuses: list[str] | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"user_id": user_id}
        if statuses:
            query["status"] = {"$in": statuses}
        rows = (
            self.horizon_triggers.find(query, sort=[("next_run_at", ASCENDING)])
            .limit(max(1, int(limit)))
        )
        return [self._clean(row) async for row in rows]

    async def recover_stale_horizon_triggers(self, cutoff: datetime) -> int:
        """Devuelve a la cola lo que quedo a medias por una caida.

        Un disparador en ``running`` mas viejo que el margen pertenece a un
        proceso que ya no existe. Se reintenta con la espera creciente que
        corresponda, o se declara si ya agoto sus intentos.
        """
        now = datetime.now(timezone.utc)
        stale = self.horizon_triggers.find({"status": "running", "started_at": {"$lt": cutoff}})
        recovered = 0
        async for row in stale:
            attempts = int(row.get("attempts") or 0) + 1
            max_attempts = int(row.get("max_attempts") or 3)
            exhausted = attempts >= max_attempts
            await self.horizon_triggers.update_one(
                {"trigger_id": row.get("trigger_id")},
                {
                    "$set": {
                        "status": "failed" if exhausted else "pending",
                        "attempts": attempts,
                        "next_run_at": now,
                        "last_error": "recovered_after_restart",
                        "updated_at": now,
                    }
                },
            )
            recovered += 1
        return recovered

    async def expire_horizon_triggers(self, now: datetime) -> int:
        """Descarta avisos obsoletos (seccion 9.7) en vez de ejecutarlos."""
        result = await self.horizon_triggers.update_many(
            {
                "status": "pending",
                "expires_at": {"$ne": None, "$lte": now},
            },
            {
                "$set": {
                    "status": "expired",
                    "finished_at": now,
                    "last_error": "expired_before_run",
                    "updated_at": now,
                }
            },
        )
        return int(result.modified_count or 0)

    # ------------------------- Horizon: acciones -------------------------

    async def get_horizon_action(self, action_id: str) -> dict[str, Any] | None:
        row = await self.horizon_actions.find_one({"action_id": action_id})
        return self._clean(row)

    async def get_horizon_action_by_task_id(
        self, user_id: str, task_id: str
    ) -> dict[str, Any] | None:
        row = await self.horizon_actions.find_one({"user_id": user_id, "task_id": task_id})
        return self._clean(row)

    async def patch_horizon_action(
        self, action_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        payload = dict(updates)
        payload.pop("_id", None)
        payload["updated_at"] = datetime.now(timezone.utc)
        row = await self.horizon_actions.find_one_and_update(
            {"action_id": action_id},
            {"$set": payload},
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    # ---------------- Research jobs (TASK-2026-00056 / 00058) ----------------
    # Un job de research NO es un ticket de negocio: ninguna de estas operaciones
    # escribe en ``tasks`` ni pasa por ``_notify_status_observer``. Deliberadamente
    # separado, porque mezclarlos haria que un ciclo automatico creara tickets.

    async def create_research_job(self, doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Inserta un job. Devuelve ``(documento, creado)``.

        Idempotencia: misma clave + mismo fingerprint devuelve el original; misma
        clave + fingerprint incompatible lanza conflicto (no es una segunda
        ejecucion). Sin clave se genera una identidad nueva y no colisiona con nadie.
        """
        payload = dict(doc)
        payload.pop("_id", None)
        try:
            await self.research_jobs.insert_one(payload)
            return self._clean(dict(payload)), True
        except DuplicateKeyError:
            existing = await self._find_research_job_by_scope(
                project_key=payload.get("project_key"),
                owner_id=payload.get("owner_id"),
                purpose=payload.get("purpose"),
                idempotency_key=payload.get("idempotency_key"),
            )
            if existing is None:
                # La colision no vino de la clave idempotente (p. ej. research_id
                # repetido): no es una deduplicacion, es un fallo real.
                raise
            if existing.get("request_fingerprint") != payload.get("request_fingerprint"):
                raise ResearchIdempotencyConflict(
                    "La clave idempotente ya existe con un payload distinto"
                )
            return existing, False

    async def get_research_job(self, research_id: str) -> dict[str, Any] | None:
        return self._clean(await self.research_jobs.find_one({"research_id": research_id}))

    async def find_research_job_by_idempotency(
        self,
        *,
        project_key: str,
        owner_id: str,
        purpose: str,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        return await self._find_research_job_by_scope(
            project_key=project_key,
            owner_id=owner_id,
            purpose=purpose,
            idempotency_key=idempotency_key,
        )

    async def _find_research_job_by_scope(
        self,
        *,
        project_key: str | None,
        owner_id: str | None,
        purpose: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        if not idempotency_key:
            # Sin clave no hay identidad idempotente que buscar: dos consultas nuevas
            # iguales son dos jobs distintos por diseno.
            return None
        row = await self.research_jobs.find_one(
            {
                "project_key": project_key,
                "owner_id": owner_id,
                "purpose": purpose,
                "idempotency_key": idempotency_key,
            }
        )
        return self._clean(row)

    async def transition_research_job(
        self,
        research_id: str,
        new_status: ResearchJobStatus | str,
        *,
        updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Transicion por CAS: valida la tabla y no pisa una carrera.

        Devuelve el documento nuevo, o ``None`` si el job no existe o si otro
        escritor gano la carrera (el estado ya no era el leido).
        """
        current = await self.get_research_job(research_id)
        if current is None:
            return None
        current_status = ResearchJobStatus(current["status"])
        target = ResearchJobStatus(new_status)
        if not can_transition(current_status, target):
            raise ResearchTransitionError(
                f"Transicion no permitida {current_status.value} -> {target.value}"
            )
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {"status": target.value, "updated_at": now}
        if target is ResearchJobStatus.RUNNING and not current.get("started_at"):
            payload["started_at"] = now
        if target.value in {s.value for s in TERMINAL_STATUSES}:
            payload["finished_at"] = now
            retention_days = int(getattr(self._settings, "RESEARCH_JOB_RETENTION_DAYS", 0) or 0)
            if retention_days > 0:
                # TTL de campo solo-terminal: un job vivo no tiene ``expires_at``.
                payload["expires_at"] = now + timedelta(days=retention_days)
        if updates:
            payload.update(updates)
        payload.pop("_id", None)
        row = await self.research_jobs.find_one_and_update(
            {"research_id": research_id, "status": current_status.value},
            {"$set": payload},
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def patch_research_job(
        self, research_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        payload = dict(updates)
        payload.pop("_id", None)
        payload.pop("status", None)  # el estado solo cambia por transicion validada
        payload["updated_at"] = datetime.now(timezone.utc)
        row = await self.research_jobs.find_one_and_update(
            {"research_id": research_id},
            {"$set": payload},
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def claim_next_research_job(
        self,
        *,
        lease_owner: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Toma atomica de un job encolado.

        ``find_one_and_update`` evita que dos despachadores tomen el mismo job. No
        limita por si solo la concurrencia global: eso es un slot que administra el
        servicio.
        """
        moment = now or datetime.now(timezone.utc)
        row = await self.research_jobs.find_one_and_update(
            {"status": ResearchJobStatus.QUEUED.value},
            {
                "$set": {
                    "status": ResearchJobStatus.RUNNING.value,
                    "lease_owner": lease_owner,
                    "lease_expires_at": moment + timedelta(seconds=lease_seconds),
                    "started_at": moment,
                    "updated_at": moment,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def list_research_jobs_with_expired_lease(
        self, cutoff: datetime, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Jobs `running` cuya concesion vencio: candidatos a recuperacion tras un crash.

        Acotado por ``limit``: es recuperacion de arranque, no un barrido periodico.
        """
        rows = self.research_jobs.find(
            {
                "status": ResearchJobStatus.RUNNING.value,
                "lease_expires_at": {"$lte": cutoff},
            },
            sort=[("lease_expires_at", ASCENDING)],
        ).limit(limit)
        return [self._clean(row) async for row in rows]

    async def patch_research_job_heartbeat(
        self, research_id: str, *, lease_owner: str, lease_seconds: int
    ) -> bool:
        """Renueva la concesion solo si sigue siendo nuestra (fencing por owner)."""
        now = datetime.now(timezone.utc)
        result = await self.research_jobs.update_one(
            {
                "research_id": research_id,
                "status": ResearchJobStatus.RUNNING.value,
                "lease_owner": lease_owner,
            },
            {"$set": {"lease_expires_at": now + timedelta(seconds=lease_seconds), "updated_at": now}},
        )
        return bool(getattr(result, "modified_count", 0))

    # --------------- Research schedules (TASK-2026-00068) ---------------
    # Igual que los jobs: nada de esto escribe en ``tasks`` ni pasa por el observador
    # de ERPNext. Un tick de schedule es ejecucion, no un ticket de negocio.

    async def create_research_schedule(
        self, doc: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Inserta un schedule. Devuelve ``(documento, creado)``.

        Idempotente por ``dedupe_key`` (indice parcial) y por ``schedule_id``. Volver a
        crear el mismo schedule devuelve el existente; el store decide si el spec
        incompatible es un conflicto.
        """
        payload = dict(doc)
        payload.pop("_id", None)
        try:
            await self.research_schedules.insert_one(dict(payload))
            return self._clean(dict(payload)), True
        except DuplicateKeyError:
            existing = await self._find_research_schedule_by_dedupe(payload.get("dedupe_key"))
            if existing is None:
                existing = await self.get_research_schedule(payload.get("schedule_id"))
            if existing is None:
                raise
            return existing, False

    async def get_research_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        return self._clean(await self.research_schedules.find_one({"schedule_id": schedule_id}))

    async def _find_research_schedule_by_dedupe(
        self, dedupe_key: str | None
    ) -> dict[str, Any] | None:
        if not dedupe_key:
            return None
        return self._clean(await self.research_schedules.find_one({"dedupe_key": dedupe_key}))

    async def find_research_schedule_by_dedupe(
        self, dedupe_key: str | None
    ) -> dict[str, Any] | None:
        return await self._find_research_schedule_by_dedupe(dedupe_key)

    async def list_research_schedules(
        self,
        *,
        project_key: str | None = None,
        owner_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
        workflows: list[str] | None = None,
        created_by_consumer: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if project_key is not None:
            query["project_key"] = project_key
        if owner_id is not None:
            query["owner_id"] = owner_id
        if statuses:
            query["status"] = {"$in": list(statuses)}
        if workflows:
            query["workflow"] = {"$in": list(workflows)}
        if created_by_consumer is not None:
            query["created_by_consumer"] = created_by_consumer
        rows = (
            self.research_schedules.find(query, sort=[("next_run_at", ASCENDING)])
            .limit(max(1, int(limit)))
        )
        return [self._clean(row) async for row in rows]

    async def claim_next_research_schedule(
        self,
        *,
        lease_owner: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Toma atomica del schedule vencido. Devuelve None si no hay ninguno.

        La condicion es ``(status=active, run_state=idle, next_run_at <= now)`` y la
        mutacion ocurre en la **misma** operacion sobre el **mismo** documento: dos
        dispatchers no pueden tomar la misma ocurrencia. ``scheduled_for`` congela el
        vencimiento ejecutado y el ``fencing_token`` se incrementa para que un ejecutor
        viejo no cierre una toma que ya no es suya.
        """
        moment = now or datetime.now(timezone.utc)
        row = await self.research_schedules.find_one_and_update(
            {
                "status": "active",
                "run_state": "idle",
                "next_run_at": {"$lte": moment},
            },
            [
                {
                    "$set": {
                        "run_state": "running",
                        "lease_owner": lease_owner,
                        "lease_expires_at": moment + timedelta(seconds=lease_seconds),
                        # Retry/recovery keeps the original occurrence identity.
                        "scheduled_for": {"$ifNull": ["$scheduled_for", "$next_run_at"]},
                        "fencing_token": {"$add": [{"$ifNull": ["$fencing_token", 0]}, 1]},
                        "updated_at": moment,
                    }
                }
            ],
            sort=[("next_run_at", ASCENDING), ("priority", DESCENDING)],
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def patch_research_schedule(
        self,
        schedule_id: str,
        updates: dict[str, Any],
        *,
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Aplica cambios. Con fencing, solo si la toma sigue siendo la de este ejecutor."""
        payload = dict(updates)
        payload.pop("_id", None)
        payload["updated_at"] = now or datetime.now(timezone.utc)
        query: dict[str, Any] = {"schedule_id": schedule_id}
        if lease_owner is not None:
            query["lease_owner"] = lease_owner
            query["run_state"] = "running"
        if fencing_token is not None:
            query["fencing_token"] = int(fencing_token)
        row = await self.research_schedules.find_one_and_update(
            query, {"$set": payload}, return_document=ReturnDocument.AFTER
        )
        return self._clean(row)

    async def set_research_schedule_status(
        self,
        schedule_id: str,
        status: str,
        *,
        now: datetime | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Pausa, reanuda o cancela. Cancelar libera el lease para no dejar colgado.

        ``reason`` explica por que el sistema (no el owner) detuvo la suscripcion, p. ej.
        ``access_revoked``. Reanudar lo limpia: un motivo viejo no describe el estado.
        """
        moment = now or datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "status": status,
            "status_reason": reason,
            "updated_at": moment,
        }
        if status == "cancelled":
            payload["run_state"] = "idle"
            payload["lease_owner"] = None
            payload["lease_expires_at"] = None
            # Un schedule cancelado libera su identidad: volver a suscribirse a lo mismo crea
            # una suscripcion nueva en lugar de devolver la cancelada como "idempotente". El
            # indice es parcial sobre strings, asi que ``None`` lo saca de la unicidad.
            payload["dedupe_key"] = None
        # Los terminales son estables: una pausa tardia (p. ej. tras un tick en vuelo) no
        # puede resucitar un schedule ya cancelado o expirado. La condicion va en la misma
        # operacion que la mutacion; no hay lectura previa que pueda quedar vieja.
        row = await self.research_schedules.find_one_and_update(
            {"schedule_id": schedule_id, "status": {"$nin": ["cancelled", "expired"]}},
            {"$set": payload},
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(row)

    async def next_research_schedule_due_at(self) -> datetime | None:
        """Proximo vencimiento de un schedule activo, sin despertar ni ejecutar nada."""
        rows = (
            self.research_schedules.find(
                {"status": "active", "run_state": "idle"},
                sort=[("next_run_at", ASCENDING)],
            ).limit(1)
        )
        async for row in rows:
            return row.get("next_run_at")
        return None

    async def recover_stale_research_schedules(
        self, cutoff: datetime, *, now: datetime | None = None
    ) -> int:
        """Recupera **solo** leases vencidas al arrancar, con **un** catch-up util."""
        moment = now or datetime.now(timezone.utc)
        recovered = 0
        stale = self.research_schedules.find(
            {"run_state": "running", "lease_expires_at": {"$lte": cutoff}}
        )
        async for row in stale:
            patch: dict[str, Any] = {
                "run_state": "idle",
                "lease_owner": None,
                "lease_expires_at": None,
                # La reentrega no se afirma como exactly-once: el handler debe reconciliar
                # el cursor/la adquisicion antes de repetir gasto.
                "pending_reconcile": True,
                "last_error": "recovered_after_restart",
                "updated_at": moment,
            }
            next_at = row.get("next_run_at")
            if next_at is not None and next_at.tzinfo is None:
                next_at = next_at.replace(tzinfo=timezone.utc)
            if row.get("status") == "active" and (next_at is None or next_at <= moment):
                # Un solo catch-up: se ejecuta una vez ahora y el posterior vencimiento
                # queda futuro, en vez de una rafaga por cada intervalo perdido.
                patch["next_run_at"] = moment
            await self.research_schedules.update_one(
                {"schedule_id": row.get("schedule_id"), "run_state": "running"},
                {"$set": patch},
            )
            recovered += 1
        return recovered

    async def expire_research_schedules(self, now: datetime) -> int:
        """Expira schedules vencidos sin borrarlos: un TTL borraria activos."""
        result = await self.research_schedules.update_many(
            {"status": "active", "expires_at": {"$ne": None, "$lte": now}},
            # Expirar tambien libera la identidad (ver ``set_research_schedule_status``).
            {"$set": {"status": "expired", "dedupe_key": None, "updated_at": now}},
        )
        return int(getattr(result, "modified_count", 0) or 0)

    # --------------- Research watch events (TASK-2026-00069) ---------------
    # Un evento es un item de una fuente vigilada que el tick ya vio. Es la autoridad
    # de dedup y la cola de admision: nada de esto toca ``tasks``.

    async def find_research_watch_events_by_item(
        self, subscription_id: str, item_keys: list[str]
    ) -> list[dict[str, Any]]:
        """Eventos ya vistos para esos items (todas sus versiones de contenido)."""
        if not item_keys:
            return []
        rows = self.research_watch_events.find(
            {"subscription_id": subscription_id, "item_key": {"$in": list(item_keys)}}
        )
        return [self._clean(row) async for row in rows]

    async def insert_research_watch_events(self, docs: list[dict[str, Any]]) -> list[str]:
        """Inserta eventos nuevos y devuelve los ``event_key`` que **si** se insertaron.

        Un solo viaje (``insert_many`` desordenado) en vez de uno por evento: un baseline de
        cientos de items no debe consumir el lease del tick. Idempotente: un evento que ya
        existe (reentrega tras un crash) se ignora por la clave unica sin abortar el resto.
        """
        payloads = []
        for doc in docs:
            payload = dict(doc)
            payload.pop("_id", None)
            payloads.append(payload)
        if not payloads:
            return []
        try:
            await self.research_watch_events.insert_many(payloads, ordered=False)
            return [str(p["event_key"]) for p in payloads]
        except BulkWriteError as exc:
            errors = (exc.details or {}).get("writeErrors") or []
            if any(error.get("code") != 11000 for error in errors):
                raise  # solo los duplicados son esperables; cualquier otra cosa es un fallo real
            failed = {int(error["index"]) for error in errors}
            return [str(p["event_key"]) for index, p in enumerate(payloads) if index not in failed]

    async def touch_research_watch_events(
        self,
        subscription_id: str,
        event_keys: list[str],
        *,
        seen_at: datetime,
        expires_at: datetime | None,
    ) -> int:
        """TTL deslizante: un item que sigue en la fuente no caduca como "nuevo"."""
        if not event_keys:
            return 0
        update: dict[str, Any] = {"last_seen_at": seen_at}
        if expires_at is not None:
            update["expires_at"] = expires_at
        result = await self.research_watch_events.update_many(
            {"subscription_id": subscription_id, "event_key": {"$in": list(event_keys)}},
            {"$set": update},
        )
        return int(getattr(result, "modified_count", 0) or 0)

    async def list_pending_research_watch_events(
        self, subscription_id: str, limit: int
    ) -> list[dict[str, Any]]:
        """Eventos por admitir, del mas viejo al mas nuevo: no se saltan items."""
        rows = (
            self.research_watch_events.find(
                {"subscription_id": subscription_id, "status": "pending"},
                sort=[("first_seen_at", ASCENDING), ("event_key", ASCENDING)],
            )
            .limit(max(1, int(limit)))
        )
        return [self._clean(row) async for row in rows]

    async def count_research_watch_events(
        self, subscription_id: str, *, status: str | None = None
    ) -> int:
        query: dict[str, Any] = {"subscription_id": subscription_id}
        if status is not None:
            query["status"] = status
        return int(await self.research_watch_events.count_documents(query))

    async def mark_research_watch_events(
        self,
        subscription_id: str,
        event_keys: list[str],
        status: str,
        *,
        research_id: str | None = None,
        only_from: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Cambia el estado de un lote. ``only_from`` evita pisar un estado ya decidido."""
        if not event_keys:
            return 0
        query: dict[str, Any] = {
            "subscription_id": subscription_id,
            "event_key": {"$in": list(event_keys)},
        }
        if only_from is not None:
            query["status"] = only_from
        update: dict[str, Any] = {
            "status": status,
            "updated_at": now or datetime.now(timezone.utc),
        }
        if research_id is not None:
            update["research_id"] = research_id
        result = await self.research_watch_events.update_many(query, {"$set": update})
        return int(getattr(result, "modified_count", 0) or 0)

    async def drop_pending_research_watch_events(
        self, subscription_id: str, *, now: datetime | None = None
    ) -> int:
        """Descarta lo no admitido (al cancelar): un job ya admitido no se toca."""
        result = await self.research_watch_events.update_many(
            {"subscription_id": subscription_id, "status": "pending"},
            {"$set": {"status": "dropped", "updated_at": now or datetime.now(timezone.utc)}},
        )
        return int(getattr(result, "modified_count", 0) or 0)

    async def list_research_watch_events(
        self,
        subscription_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"subscription_id": subscription_id}
        if statuses:
            query["status"] = {"$in": list(statuses)}
        rows = (
            self.research_watch_events.find(query, sort=[("first_seen_at", DESCENDING)])
            .limit(max(1, int(limit)))
        )
        return [self._clean(row) async for row in rows]

    @staticmethod
    def _clean(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        if doc is None:
            return None
        doc.pop("_id", None)
        return doc
