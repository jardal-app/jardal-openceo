from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from settings import Settings
from state import DevSessionStatus, TaskStatus


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

    # ------------------------- tasks -------------------------

    async def create_task(self, doc: dict[str, Any]) -> dict[str, Any]:
        await self.tasks.insert_one(doc)
        return await self.get_task(doc["task_id"])

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = await self.tasks.find_one({"task_id": task_id})
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
        return await self.get_task(task_id)

    async def due_waiting_tasks(self, now_utc: datetime) -> list[dict[str, Any]]:
        rows = self.tasks.find(
            {
                "status": TaskStatus.WAITING.value,
                "resume_at": {"$lte": now_utc},
            },
            sort=[("resume_at", ASCENDING)],
        )
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

    async def get_next_ticket_number(self) -> int:
        """Atomically increment and return the next ticket number."""
        result = await self.counters.find_one_and_update(
            {"_id": "ticket_number"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(result["seq"])

    @staticmethod
    def _clean(doc: dict[str, Any] | None) -> dict[str, Any] | None:
        if doc is None:
            return None
        doc.pop("_id", None)
        return doc
