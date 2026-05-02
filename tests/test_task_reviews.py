from __future__ import annotations

import sys
import unittest
from pathlib import Path


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from settings import Settings
from state import (
    AgentName,
    ApprovalState,
    TaskCreateRequest,
    TaskReviewAction,
    TaskReviewActorType,
    TaskReviewCreateRequest,
    TaskSource,
    make_task_document,
)
from task_service import TaskService
from tools.notion import NotionTicketManager


class TaskApprovalStateTests(unittest.TestCase):
    def test_make_task_document_defaults_approval_state_to_not_required(self) -> None:
        payload = TaskCreateRequest(
            project="melanyryr",
            source=TaskSource.API,
            title="Ticket",
            description="Descripcion",
        )

        doc = make_task_document(payload)

        self.assertFalse(doc.approval_required)
        self.assertEqual(doc.approval_state, ApprovalState.NOT_REQUIRED)

    def test_make_task_document_defaults_approval_state_to_pending_when_required(self) -> None:
        payload = TaskCreateRequest(
            project="melanyryr",
            source=TaskSource.API,
            title="Ticket",
            description="Descripcion",
            approval_required=True,
        )

        doc = make_task_document(payload)

        self.assertTrue(doc.approval_required)
        self.assertEqual(doc.approval_state, ApprovalState.PENDING)


class TaskReviewServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_task_review_updates_parent_task_and_keeps_history_separate(self) -> None:
        class FakeRepo:
            def __init__(self) -> None:
                self.task = {
                    "task_id": "task-1",
                    "project": "melanyryr",
                    "agent": AgentName.PM.value,
                    "source": TaskSource.API.value,
                    "title": "Ticket",
                    "description": "Descripcion",
                    "approval_required": False,
                    "approval_state": ApprovalState.NOT_REQUIRED.value,
                    "post_actions_executed": [],
                }
                self.reviews: list[dict] = []

            async def get_task(self, task_id: str):
                if task_id != "task-1":
                    return None
                return dict(self.task)

            async def create_task_review(self, doc: dict):
                self.reviews.append(dict(doc))
                return dict(doc)

            async def patch_task(self, task_id: str, updates: dict):
                self.task.update(updates)
                return dict(self.task)

            async def list_task_reviews(self, task_id: str, limit: int = 100):
                return [dict(item) for item in self.reviews[:limit]]

        class FakeHub:
            def __init__(self) -> None:
                self.events: list[tuple[str, dict]] = []

            async def broadcast(self, event: str, payload: dict) -> None:
                self.events.append((event, dict(payload)))

        service = TaskService(
            settings=Settings(),
            repo=FakeRepo(),
            graph=object(),  # not used in these tests
            realtime_hub=FakeHub(),
            notion=None,
            rate_monitor=None,
        )

        result = await service.create_task_review(
            "task-1",
            TaskReviewCreateRequest(
                actor_type=TaskReviewActorType.CLIENT,
                actor_id="client-1",
                action=TaskReviewAction.REQUEST_CHANGES,
                comment="Falta incluir devoluciones.",
            ),
        )

        review = result["review"]
        task = result["task"]
        self.assertEqual(review["task_id"], "task-1")
        self.assertEqual(review["approval_state_after"], ApprovalState.CHANGES_REQUESTED.value)
        self.assertEqual(task["approval_required"], True)
        self.assertEqual(task["approval_state"], ApprovalState.CHANGES_REQUESTED.value)
        self.assertEqual(len(await service.list_task_reviews("task-1")), 1)


class NotionApprovalSyncTests(unittest.TestCase):
    def test_update_page_properties_supports_approval_fields_when_schema_exists(self) -> None:
        settings = Settings(
            NOTION_API_TOKEN="token",
            NOTION_TICKETS_PARENT_KIND="data_source",
            NOTION_TICKETS_PARENT_ID="ds-1",
            NOTION_TICKETS_TITLE_PROPERTY="Name",
        )
        manager = NotionTicketManager(settings)
        data_source = {
            "id": "ds-1",
            "properties": {
                "Name": {"id": "title", "name": "Name", "type": "title", "title": {}},
                "Needs Approval": {"id": "na", "name": "Needs Approval", "type": "checkbox", "checkbox": {}},
                "Approval State": {
                    "id": "as",
                    "name": "Approval State",
                    "type": "select",
                    "select": {"options": [{"id": "p1", "name": "pending", "color": "yellow"}]},
                },
            },
        }
        captured: dict[str, dict] = {}

        def fake_request(method: str, path: str, *, params=None, json=None):  # type: ignore[no-untyped-def]
            if method == "GET" and path == "/data_sources/ds-1":
                return data_source
            if method == "PATCH" and path == "/pages/page-1":
                captured["payload"] = json or {}
                return {"id": "page-1"}
            raise AssertionError(f"Unexpected request: {method} {path}")

        manager._request = fake_request  # type: ignore[method-assign]

        result = manager.update_page_properties(
            page_id="page-1",
            approval_required=True,
            approval_state="pending",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            captured["payload"]["properties"],
            {
                "Needs Approval": {"checkbox": True},
                "Approval State": {"select": {"name": "pending"}},
            },
        )

    def test_update_page_properties_auto_adds_missing_approval_state_option(self) -> None:
        settings = Settings(
            NOTION_API_TOKEN="token",
            NOTION_TICKETS_PARENT_KIND="data_source",
            NOTION_TICKETS_PARENT_ID="ds-1",
            NOTION_TICKETS_TITLE_PROPERTY="Name",
        )
        manager = NotionTicketManager(settings)
        data_source = {
            "id": "ds-1",
            "properties": {
                "Name": {"id": "title", "name": "Name", "type": "title", "title": {}},
                "Approval State": {
                    "id": "as",
                    "name": "Approval State",
                    "type": "select",
                    "select": {"options": []},
                },
            },
        }
        captured: list[tuple[str, str, dict]] = []

        def fake_request(method: str, path: str, *, params=None, json=None):  # type: ignore[no-untyped-def]
            if method == "GET" and path == "/data_sources/ds-1":
                return data_source
            if method == "PATCH" and path == "/data_sources/ds-1":
                captured.append((method, path, json or {}))
                data_source["properties"]["Approval State"]["select"]["options"] = (
                    json["properties"]["Approval State"]["select"]["options"]
                )
                return {"id": "ds-1"}
            if method == "PATCH" and path == "/pages/page-1":
                captured.append((method, path, json or {}))
                return {"id": "page-1"}
            raise AssertionError(f"Unexpected request: {method} {path}")

        manager._request = fake_request  # type: ignore[method-assign]

        result = manager.update_page_properties(
            page_id="page-1",
            approval_state="changes_requested",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(captured[0][0:2], ("PATCH", "/data_sources/ds-1"))
        self.assertEqual(
            captured[0][2]["properties"]["Approval State"]["select"]["options"],
            [{"name": "changes_requested", "color": "default"}],
        )
        self.assertEqual(
            captured[1][2]["properties"],
            {"Approval State": {"select": {"name": "changes_requested"}}},
        )


if __name__ == "__main__":
    unittest.main()
