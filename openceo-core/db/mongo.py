from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from horizon.privacy import mirror_exclusion_filter
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
        return result.modified_count

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

    @staticmethod
    def _clean(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        if doc is None:
            return None
        doc.pop("_id", None)
        return doc
