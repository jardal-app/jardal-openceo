from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from agents.pm import session as pm_session
from agents.pm.session import PMSessionManager
from config_loader import ConfigLoader
from settings import Settings
from state import TaskStatus
from tools.notion import NotionTicketManager


class FakeTaskService:
    """Task service falso: captura los TaskCreateRequest y devuelve dicts."""

    def __init__(self) -> None:
        self.requests: list = []
        self.seq = 0

    async def create_task(self, req):  # type: ignore[no-untyped-def]
        self.requests.append(req)
        self.seq += 1
        return {"task_id": f"task-{self.seq}", "status": "queued"}


class PMJsonParsingTests(unittest.TestCase):
    def test_extract_json_payload_accepts_markdown_fences(self) -> None:
        payload = pm_session._extract_json_payload(
            """```json
            {"tickets":[{"title":"Implementar login","priority":"high"}]}
            ```"""
        )

        self.assertEqual(payload["tickets"][0]["title"], "Implementar login")

    def test_extract_json_payload_accepts_plain_array(self) -> None:
        payload = pm_session._extract_json_payload(
            '[{"title":"Crear dashboard","priority":"medium"}]'
        )

        self.assertEqual(payload["tickets"][0]["title"], "Crear dashboard")


class NotionDynamicSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            NOTION_API_TOKEN="test-token",
            NOTION_TICKETS_PARENT_KIND="data_source",
            NOTION_TICKETS_PARENT_ID="ds-1",
            NOTION_TICKETS_TITLE_PROPERTY="Name",
            NOTION_TICKETS_PROPERTY_MAP_JSON='{"project":"Project","tags":"Tags","source_id":"Source ID"}',
        )
        self.manager = NotionTicketManager(self.settings)
        self.data_source = {
            "id": "ds-1",
            "properties": {
                "Name": {"id": "title", "name": "Name", "type": "title", "title": {}},
                "Project": {
                    "id": "proj",
                    "name": "Project",
                    "type": "select",
                    "select": {"options": [{"id": "p1", "name": "cebitec", "color": "green"}]},
                },
                "Tags": {
                    "id": "tags",
                    "name": "Tags",
                    "type": "multi_select",
                    "multi_select": {"options": [{"id": "t1", "name": "backend", "color": "blue"}]},
                },
                "Source ID": {"id": "sid", "name": "Source ID", "type": "rich_text", "rich_text": {}},
            },
        }
        self.patch_payloads: list[dict] = []

    def _request(self, method: str, path: str, *, params=None, json=None):  # type: ignore[no-untyped-def]
        if method == "GET" and path == "/data_sources/ds-1":
            return self.data_source
        if method == "PATCH" and path == "/data_sources/ds-1":
            self.patch_payloads.append(json or {})
            properties = (json or {}).get("properties", {})
            for name, payload in properties.items():
                prop = self.data_source["properties"][name]
                prop_type = prop["type"]
                prop[prop_type]["options"] = payload[prop_type]["options"]
            return self.data_source
        if method == "POST" and path == "/pages":
            return {"id": "page-1", "url": "https://notion.test/page-1"}
        raise AssertionError(f"Unexpected request: {method} {path}")

    def test_create_ticket_auto_adds_missing_project_and_tags(self) -> None:
        with patch.object(self.manager, "_request", side_effect=self._request):
            result = self.manager.create_ticket(
                title="Ticket smoke",
                project="melanyryr",
                tags=["backend", "auth"],
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(self.patch_payloads), 2)
        self.assertIn(
            "melanyryr",
            [option["name"] for option in self.data_source["properties"]["Project"]["select"]["options"]],
        )
        self.assertIn(
            "auth",
            [option["name"] for option in self.data_source["properties"]["Tags"]["multi_select"]["options"]],
        )

    def test_list_tickets_by_source_id_queries_configured_property(self) -> None:
        def fake_request(method: str, path: str, *, params=None, json=None):  # type: ignore[no-untyped-def]
            if method == "GET" and path == "/data_sources/ds-1":
                return self.data_source
            if method == "POST" and path == "/data_sources/ds-1/query":
                self.assertEqual(
                    json,
                    {"filter": {"property": "Source ID", "rich_text": {"equals": "pm-codex-abc123"}}},
                )
                return {
                    "results": [
                        {
                            "id": "page-1",
                            "url": "https://notion.test/page-1",
                            "properties": {
                                "Name": {
                                    "title": [{"plain_text": "Configurar login con Google"}],
                                }
                            },
                        }
                    ]
                }
            raise AssertionError(f"Unexpected request: {method} {path}")

        with patch.object(self.manager, "_request", side_effect=fake_request):
            result = self.manager.list_tickets_by_source_id("pm-codex-abc123")

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["tickets"][0]["title"], "Configurar login con Google")


class PMSubprocessReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_subprocess_injects_explicit_mcp_config(self) -> None:
        settings = Settings(PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo", MCP_SERVER_ENABLED=True)
        manager = PMSessionManager(settings, config_loader=ConfigLoader(settings))
        service = FakeTaskService()
        manager.set_task_service(service)
        captured: dict[str, object] = {}

        class FakeProc:
            returncode = 0

            async def communicate(self, input=None):  # type: ignore[no-untyped-def]
                return (b"OK: 2 tickets creados en Notion\n"
                        b'{"tickets": [{"title": "Ticket A"}, {"title": "Ticket B"}]}', b"")

        async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProc()

        with patch("agents.pm.session.asyncio.create_subprocess_exec", side_effect=fake_exec):
            result = await manager.extract_tickets_subprocess(
                interview_text="El cliente quiere login con Google.",
                project="melanyryr",
                tool="claude",
            )

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.tickets_created, 2)
        self.assertEqual(len(service.requests), 2)
        args = list(captured["args"])
        self.assertIn("--mcp-config", args)
        self.assertIn("--strict-mcp-config", args)
        env = captured["kwargs"]["env"]
        self.assertIn("OPENCEO_INTERNAL_MCP_URL", env)

    async def test_timeout_returns_failed(self) -> None:
        settings = Settings(PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo")
        manager = PMSessionManager(settings, config_loader=ConfigLoader(settings))
        manager.set_task_service(FakeTaskService())

        class FakeProc:
            returncode = 0

            async def communicate(self, input=None):  # type: ignore[no-untyped-def]
                await asyncio.sleep(1)
                return (b"", b"")

            def kill(self) -> None:
                return None

            async def wait(self) -> None:
                return None

        async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
            return FakeProc()

        with patch("agents.pm.session.asyncio.create_subprocess_exec", side_effect=fake_exec):
            with patch.object(pm_session, "_SUBPROCESS_TIMEOUT", 0.01):
                result = await manager.extract_tickets_subprocess(
                    interview_text="El cliente quiere login con Google.",
                    project="melanyryr",
                    tool="codex",
                )

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("CLI timeout", result.error or "")


class PMDeepSeekTicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_langraph_writes_source_id_into_task_batch(self) -> None:
        settings = Settings(PROJECTS_DIR="projects", DEFAULT_PROJECT="jardal-openceo")
        manager = PMSessionManager(settings, config_loader=ConfigLoader(settings))

        class FakeLLM:
            async def ainvoke(self, messages):  # type: ignore[no-untyped-def]
                return type("Response", (), {"content": '{"tickets":[{"title":"Crear login","priority":"high"}]}'})()

        service = FakeTaskService()
        manager._llm = FakeLLM()
        manager.set_task_service(service)

        result = await manager.extract_tickets(
            interview_text="El sistema necesita login con Google.",
            project="melanyryr",
        )

        self.assertEqual(result.status, TaskStatus.COMPLETED)
        self.assertEqual(result.tickets_created, 1)
        self.assertEqual(len(service.requests), 1)
        self.assertTrue(service.requests[0].source_id.startswith("pm-langraph-"))


if __name__ == "__main__":
    unittest.main()
