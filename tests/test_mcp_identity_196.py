"""MCP identity per ticket and tool box per agent (TASK-2026-00196).

Every MCP caller authenticates with an opaque ticket token (the badge:
which agent, on which ticket). The server filters tools/list to the
grant's box — taken from the agent's catalog card — and rejects
out-of-box calls. No anonymous access. No tokens.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import httpx  # noqa: E402

from catalog import AgentCard, AgentType, load_catalog  # noqa: E402
from mcp_identity import (  # noqa: E402
    MCPIdentityMiddleware,
    McpAuthError,
    MemoryAuditSink,
    MemoryTokenStore,
    TicketGrant,
    TokenIssuer,
    TokenKind,
    bearer_from_headers,
    box_for_agent,
)
from mcp_server import (  # noqa: E402
    _register_all_tools,
    build_mounted_mcp_server,
)
from settings import Settings  # noqa: E402

FAST_MCP_AVAILABLE = True
try:
    from fastmcp import FastMCP  # noqa: E402
except ImportError:  # pragma: no cover - FastMCP is a hard test dependency
    FAST_MCP_AVAILABLE = False
    FastMCP = None  # type: ignore[assignment]

DEV_TICKET = "TASK-2026-00196"
DEV_PROJECT = "jardal-openceo"


def _dev_card() -> AgentCard:
    return AgentCard.model_validate({
        "name": "dev",
        "type": AgentType.AI,
        "engine": "muse-code",
        "profile": None,
        "boundary_tools": [
            "openceo_search_tickets",
            "openceo_get_ticket",
            "openceo_add_ticket_comment",
            "openceo_attach_ticket_file",
        ],
        "skills_dir": "skills/dev",
        "skills": [],
        "inputs": [],
        "outputs": [],
        "enabled": True,
        "disabled_reason": None,
        "decided_by_ticket": None,
    })


def _owner_card(box: frozenset[str] | set[str]) -> AgentCard:
    return AgentCard.model_validate({
        "name": "owner",
        "type": AgentType.HUMAN,
        "engine": None,
        "profile": None,
        "boundary_tools": sorted(box),
        "skills_dir": "skills/owner",
        "skills": ["decidir", "aprobar", "responder", "hacer"],
        "inputs": [],
        "outputs": [],
        "enabled": True,
        "disabled_reason": None,
        "decided_by_ticket": None,
    })


def _issuer(store: MemoryTokenStore | None = None) -> tuple[TokenIssuer, MemoryTokenStore]:
    store = store or MemoryTokenStore()
    cards = {
        "dev": _dev_card(),
        "owner": _owner_card({"openceo_create_ticket",
                              "manage_dns_check_or_create"}),
    }
    return TokenIssuer(store, cards), store


class BearerParsingTests(unittest.TestCase):
    def test_bearer_extracted(self) -> None:
        self.assertEqual(
            bearer_from_headers({"authorization": "Bearer abc123"}), "abc123")

    def test_case_insensitive_scheme(self) -> None:
        self.assertEqual(
            bearer_from_headers({"Authorization": "bearer abc123"}), "abc123")

    def test_missing_or_malformed_is_none(self) -> None:
        self.assertIsNone(bearer_from_headers({}))
        self.assertIsNone(bearer_from_headers({"authorization": "Basic abc"}))
        self.assertIsNone(bearer_from_headers({"authorization": "Bearer "}))
        self.assertIsNone(bearer_from_headers({"authorization": "Bearer a b"}))


class BoxResolutionTests(unittest.TestCase):
    def test_box_comes_from_the_card(self) -> None:
        self.assertEqual(box_for_agent(_dev_card()), frozenset({
            "openceo_search_tickets",
            "openceo_get_ticket",
            "openceo_add_ticket_comment",
            "openceo_attach_ticket_file",
        }))

    def test_disabled_card_has_no_box(self) -> None:
        card = _dev_card().model_copy(update={
            "enabled": False,
            "disabled_reason": "retired",
            "decided_by_ticket": "TASK-2026-00209",
        })
        with self.assertRaises(McpAuthError):
            box_for_agent(card)


class IssuerTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_token_roundtrip(self) -> None:
        issuer, _ = _issuer()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        self.assertTrue(token)
        grant = await issuer.authenticate(token)
        self.assertEqual(grant.kind, TokenKind.AGENT)
        self.assertEqual(grant.agent, "dev")
        self.assertEqual(grant.ticket, DEV_TICKET)
        self.assertEqual(grant.project, DEV_PROJECT)
        self.assertIn("openceo_add_ticket_comment", grant.tools)
        self.assertNotIn("openceo_transition_ticket", grant.tools)

    async def test_plaintext_never_stored(self) -> None:
        issuer, store = _issuer()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        self.assertEqual(store.count(), 1)
        self.assertIsNone(store.record_for("wrong-token"))
        record = store.record_for(token)
        self.assertIsNotNone(record)
        self.assertNotIn(token, str(record))

    async def test_unknown_token_rejected(self) -> None:
        issuer, _ = _issuer()
        with self.assertRaises(McpAuthError) as ctx:
            await issuer.authenticate("no-such-token")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_missing_token_rejected(self) -> None:
        issuer, _ = _issuer()
        with self.assertRaises(McpAuthError) as ctx:
            await issuer.authenticate(None)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_unknown_agent_rejected(self) -> None:
        issuer, _ = _issuer()
        with self.assertRaises(McpAuthError):
            await issuer.issue_agent("nope", DEV_TICKET, DEV_PROJECT)

    async def test_disabled_agent_rejected(self) -> None:
        card = _dev_card().model_copy(update={
            "enabled": False,
            "disabled_reason": "retired",
            "decided_by_ticket": "TASK-2026-00209",
        })
        issuer = TokenIssuer(MemoryTokenStore(), {"dev": card})
        with self.assertRaises(McpAuthError):
            await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)

    async def test_readonly_token_cannot_write(self) -> None:
        issuer, _ = _issuer()
        grant = await issuer.authenticate(
            await issuer.issue_readonly("planner"))
        self.assertEqual(grant.tools, frozenset({
            "openceo_search_tickets", "openceo_get_ticket"}))
        with self.assertRaises(McpAuthError) as ctx:
            grant.check("openceo_create_ticket", {"title": "x"})
        self.assertEqual(ctx.exception.status_code, 403)

    async def test_owner_token_comes_from_its_card(self) -> None:
        issuer, _ = _issuer()
        grant = await issuer.authenticate(await issuer.issue_owner())
        self.assertEqual(grant.kind, TokenKind.OWNER)
        grant.check("openceo_create_ticket", {"title": "x"})
        grant.check("manage_dns_check_or_create", {"hostname": "h.example"})
        with self.assertRaises(McpAuthError):
            grant.check("openceo_transition_ticket", {"ticket": "t"})

    async def test_service_token_is_explicit_and_project_scoped(self) -> None:
        issuer, _ = _issuer()
        token = await issuer.issue_service(
            "pm-extract", DEV_PROJECT,
            tools=("openceo_create_pm_tickets_batch",))
        grant = await issuer.authenticate(token)
        grant.check("openceo_create_pm_tickets_batch",
                    {"project": DEV_PROJECT, "source_id": "s", "tickets": "[]"})
        with self.assertRaises(McpAuthError):
            grant.check("openceo_create_pm_tickets_batch",
                        {"project": "other", "source_id": "s", "tickets": "[]"})
        with self.assertRaises(McpAuthError):
            grant.check("openceo_create_ticket", {"title": "x"})

    async def test_expired_token_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        issuer = TokenIssuer(MemoryTokenStore(), {"dev": _dev_card()},
                             clock=lambda: now)
        token = await issuer.issue_agent(
            "dev", DEV_TICKET, DEV_PROJECT, ttl=timedelta(seconds=60))
        issuer._clock = lambda: now + timedelta(seconds=61)
        with self.assertRaises(McpAuthError) as ctx:
            await issuer.authenticate(token)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_revoked_token_rejected(self) -> None:
        issuer, _ = _issuer()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        self.assertTrue(await issuer.revoke(token))
        with self.assertRaises(McpAuthError):
            await issuer.authenticate(token)

    async def test_revoke_ticket_closes_the_badge(self) -> None:
        issuer, _ = _issuer()
        mine = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        other = await issuer.issue_agent("dev", "TASK-2026-00001", DEV_PROJECT)
        self.assertEqual(await issuer.revoke_ticket(DEV_TICKET), 1)
        with self.assertRaises(McpAuthError):
            await issuer.authenticate(mine)
        await issuer.authenticate(other)

    async def test_revoke_empty_ticket_is_noop(self) -> None:
        issuer, _ = _issuer()
        owner = await issuer.issue_owner()
        self.assertEqual(await issuer.revoke_ticket(""), 0)
        await issuer.authenticate(owner)

    async def test_revoke_kind_owner(self) -> None:
        issuer, _ = _issuer()
        owner = await issuer.issue_owner()
        agent = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        self.assertEqual(await issuer.revoke_kind(TokenKind.OWNER), 1)
        with self.assertRaises(McpAuthError):
            await issuer.authenticate(owner)
        await issuer.authenticate(agent)


class AgentScopingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        issuer, _ = _issuer()
        self.grant = await issuer.authenticate(
            await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT))

    def test_comment_and_attach_own_ticket(self) -> None:
        self.grant.check("openceo_add_ticket_comment",
                         {"ticket": DEV_TICKET, "content": "hi"})
        self.grant.check("openceo_attach_ticket_file",
                         {"ticket": DEV_TICKET, "file_name": "f.txt"})

    def test_comment_and_attach_other_ticket_denied(self) -> None:
        with self.assertRaises(McpAuthError) as ctx:
            self.grant.check("openceo_add_ticket_comment",
                             {"ticket": "TASK-2026-00001", "content": "hi"})
        self.assertEqual(ctx.exception.status_code, 403)
        with self.assertRaises(McpAuthError):
            self.grant.check("openceo_attach_ticket_file",
                             {"ticket": "TASK-2026-00001"})

    def test_get_own_ticket_only(self) -> None:
        self.grant.check("openceo_get_ticket", {"identifier": DEV_TICKET})
        with self.assertRaises(McpAuthError):
            self.grant.check("openceo_get_ticket",
                             {"identifier": "TASK-2026-00001"})
        with self.assertRaises(McpAuthError):
            self.grant.check("openceo_get_ticket",
                             {"identifier": "api:some-ref"})

    def test_search_requires_own_project(self) -> None:
        self.grant.check("openceo_search_tickets", {"project": DEV_PROJECT})
        with self.assertRaises(McpAuthError):
            self.grant.check("openceo_search_tickets", {"project": "other"})
        with self.assertRaises(McpAuthError):
            self.grant.check("openceo_search_tickets", {})

    def test_no_state_changes_no_creates(self) -> None:
        for tool, args in [
            ("openceo_transition_ticket",
             {"ticket": DEV_TICKET, "to_status": "Completed", "reason": "r"}),
            ("openceo_create_ticket", {"title": "x"}),
            ("openceo_update_ticket", {"ticket": DEV_TICKET}),
            ("openceo_delete_ticket_comment",
             {"comment_id": "c", "ticket": DEV_TICKET}),
            ("manage_dns_check_or_create", {"hostname": "h.example"}),
        ]:
            with self.assertRaises(McpAuthError, msg=tool):
                self.grant.check(tool, args)


class AuditTruncationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reason_truncated(self) -> None:
        issuer, _ = _issuer()
        audit = MemoryAuditSink()
        middleware = MCPIdentityMiddleware(issuer, audit)
        grant = await issuer.authenticate(
            await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT))
        await middleware._record(grant, "t", False, "x" * 500)
        self.assertEqual(len(audit.events), 1)
        self.assertEqual(len(audit.events[0]["reason"]), 300)


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class MiddlewareBoxTests(unittest.IsolatedAsyncioTestCase):
    async def _server(self) -> Any:
        from fastmcp import FastMCP  # noqa: PLC0415

        server: Any = FastMCP("identity-test")

        async def openceo_add_ticket_comment(ticket: str, content: str) -> dict[str, Any]:
            return {"ok": True, "ticket": ticket}

        async def openceo_transition_ticket(ticket: str) -> dict[str, Any]:
            return {"ok": True, "ticket": ticket}

        server.tool(openceo_add_ticket_comment, name="openceo_add_ticket_comment")
        server.tool(openceo_transition_ticket, name="openceo_transition_ticket")
        return server

    async def test_list_tools_filtered_to_box(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        issuer, _ = _issuer()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        server = await self._server()
        audit = MemoryAuditSink()
        server.add_middleware(MCPIdentityMiddleware(issuer, audit))
        async with Client(server) as client:
            with patch("mcp_identity.get_http_request") as req:
                req.return_value.headers = {
                    "authorization": f"Bearer {token}"}
                tools = await client.list_tools()
        names = {t.name for t in tools}
        self.assertIn("openceo_add_ticket_comment", names)
        self.assertNotIn("openceo_transition_ticket", names)
        self.assertTrue(any(e["tool"] == "tools/list" and e["ok"]
                            for e in audit.events))

    async def test_call_out_of_box_blocked(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        issuer, _ = _issuer()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        server = await self._server()
        audit = MemoryAuditSink()
        server.add_middleware(MCPIdentityMiddleware(issuer, audit))
        async with Client(server) as client:
            with patch("mcp_identity.get_http_request") as req:
                req.return_value.headers = {
                    "authorization": f"Bearer {token}"}
                with self.assertRaises(ToolError):
                    await client.call_tool(
                        "openceo_transition_ticket", {"ticket": DEV_TICKET})
                result = await client.call_tool(
                    "openceo_add_ticket_comment",
                    {"ticket": DEV_TICKET, "content": "hi"})
        self.assertTrue(result.data["ok"])
        denied = [e for e in audit.events if not e["ok"]]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["tool"], "openceo_transition_ticket")
        self.assertEqual(denied[0]["ticket"], DEV_TICKET)

    async def test_non_tool_surfaces_hidden_or_denied(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        issuer, _ = _issuer()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        server = await self._server()
        audit = MemoryAuditSink()
        server.add_middleware(MCPIdentityMiddleware(issuer, audit))
        async with Client(server) as client:
            with patch("mcp_identity.get_http_request") as req:
                req.return_value.headers = {
                    "authorization": f"Bearer {token}"}
                self.assertEqual(await client.list_resources(), [])
                self.assertEqual(
                    await client.list_resource_templates(), [])
                self.assertEqual(await client.list_prompts(), [])
                with self.assertRaises(Exception):
                    await client.read_resource("mem://x")
                with self.assertRaises(Exception):
                    await client.get_prompt("p", {})
        denied = [e for e in audit.events if not e["ok"]]
        self.assertEqual(len(denied), 2)


class _FakeTaskService:
    async def get_task(self, task_id: str) -> None:
        return None


class _FakeErpnext:
    is_enabled = True

    async def add_comment(self, ticket: str, content: str) -> dict[str, Any]:
        return {"ok": True, "ticket": ticket}

    async def search_tickets(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


def _mounted() -> tuple[Any, TokenIssuer, MemoryAuditSink]:
    server = FastMCP("identity-http-test")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    _register_all_tools(server, settings, _FakeTaskService(), _FakeErpnext())
    issuer = TokenIssuer(MemoryTokenStore(), load_catalog().agents)
    audit = MemoryAuditSink()
    server.add_middleware(MCPIdentityMiddleware(issuer, audit))
    mount = build_mounted_mcp_server(
        path="/internal/mcp",
        name="identity-http-test",
        server=server,
        bearer_token=None,
        stateless_http=True,
        json_response=True,
        authenticator=issuer,
    )
    return mount, issuer, audit


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class HttpEnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def _post(self, mount: Any, payload: dict[str, Any],
                    token: str | None) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        transport = httpx.ASGITransport(app=mount.app)  # type: ignore[arg-type]
        async with mount.lifespan():
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://t") as client:
                return await client.post("/", json=payload, headers=headers)

    async def test_list_without_token_is_401(self) -> None:
        mount, _, _ = _mounted()
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": {}}, None)
        self.assertEqual(response.status_code, 401)

    async def test_list_with_bad_token_is_401(self) -> None:
        mount, _, _ = _mounted()
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": {}}, "bad")
        self.assertEqual(response.status_code, 401)

    async def test_legacy_shared_bearer_not_accepted(self) -> None:
        mount, _, _ = _mounted()
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": {}}, "legacy-shared-key")
        self.assertEqual(response.status_code, 401)

    async def test_agent_list_sees_only_its_box(self) -> None:
        mount, issuer, _ = _mounted()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": {}}, token)
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("openceo_add_ticket_comment", body)
        self.assertNotIn("openceo_transition_ticket", body)

    async def test_owner_list_sees_everything(self) -> None:
        mount, issuer, _ = _mounted()
        token = await issuer.issue_owner()
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": {}}, token)
        self.assertEqual(response.status_code, 200)
        self.assertIn("openceo_transition_ticket", response.text)

    async def test_agent_call_in_box_passes(self) -> None:
        mount, issuer, audit = _mounted()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "openceo_add_ticket_comment",
                               "arguments": {"ticket": DEV_TICKET,
                                             "content": "hi"}}}, token)
        self.assertEqual(response.status_code, 200)
        self.assertIn('"ok":true', response.text.replace(" ", ""))
        self.assertTrue(any(e["ok"] and e["agent"] == "dev"
                            for e in audit.events))

    async def test_agent_call_out_of_box_fails(self) -> None:
        mount, issuer, audit = _mounted()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "openceo_transition_ticket",
                               "arguments": {"ticket": DEV_TICKET}}}, token)
        self.assertEqual(response.status_code, 200)
        self.assertIn("isError", response.text)
        denied = [e for e in audit.events if not e["ok"]]
        self.assertEqual(len(denied), 1)

    async def test_get_stream_without_token_is_401(self) -> None:
        mount, _, _ = _mounted()
        transport = httpx.ASGITransport(app=mount.app)  # type: ignore[arg-type]
        async with mount.lifespan():
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://t") as client:
                response = await client.get(
                    "/", headers={"Accept": "text/event-stream"})
        self.assertEqual(response.status_code, 401)

    async def test_get_with_token_passes_auth(self) -> None:
        mount, issuer, _ = _mounted()
        token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
        transport = httpx.ASGITransport(app=mount.app)  # type: ignore[arg-type]
        async with mount.lifespan():
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://t") as client:
                response = await client.get(
                    "/", headers={"Accept": "text/event-stream",
                                  "Authorization": f"Bearer {token}"})
        self.assertNotEqual(response.status_code, 401)

    async def test_delete_without_token_is_401(self) -> None:
        mount, _, _ = _mounted()
        transport = httpx.ASGITransport(app=mount.app)  # type: ignore[arg-type]
        async with mount.lifespan():
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://t") as client:
                response = await client.delete("/")
        self.assertEqual(response.status_code, 401)

    async def test_readonly_list_over_http(self) -> None:
        mount, issuer, _ = _mounted()
        token = await issuer.issue_readonly("planner")
        response = await self._post(
            mount, {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                    "params": {}}, token)
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("openceo_search_tickets", body)
        self.assertNotIn("openceo_add_ticket_comment", body)


class WrapperScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_scope_rejected(self) -> None:
        from mcp_server import (  # noqa: PLC0415
            _InternalOnlyBearerWrapper,
            _parse_allowed_networks,
        )

        issuer, _ = _issuer()
        wrapper = _InternalOnlyBearerWrapper(
            allowed_networks=_parse_allowed_networks("127.0.0.1/32"),
            bearer_token=None,
            authenticator=issuer,
        )
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def receive() -> dict[str, Any]:
            return {"type": "websocket.connect"}

        await wrapper(
            {"type": "websocket", "client": ("127.0.0.1", 12345),
             "headers": [], "method": "GET", "path": "/"},
            receive, send)
        self.assertEqual(sent, [{"type": "websocket.close", "code": 4401}])


class CatalogBoxConsistencyTests(unittest.TestCase):
    def test_real_cards_match_the_registry(self) -> None:
        catalog = load_catalog()
        server = FastMCP("identity-registry-test")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        names = _register_all_tools(
            server, settings, _FakeTaskService(), _FakeErpnext())
        registered = set(names)
        self.assertEqual(len(registered), 17)
        owner_box = set(catalog.agents["owner"].boundary_tools)
        self.assertEqual(owner_box, registered)
        dev_box = set(catalog.agents["dev"].boundary_tools)
        self.assertTrue(dev_box <= registered)
        self.assertNotIn("openceo_transition_ticket", dev_box)
        self.assertNotIn("openceo_create_ticket", dev_box)


class PMTokenWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_path_carries_service_token(self) -> None:
        from agents.pm.session import PMSessionManager  # noqa: PLC0415
        from config_loader import ConfigLoader  # noqa: PLC0415
        from state import TaskStatus  # noqa: PLC0415

        settings = Settings(PROJECTS_DIR="projects",
                            DEFAULT_PROJECT="jardal-openceo",
                            MCP_SERVER_ENABLED=True)
        manager = PMSessionManager(
            settings, config_loader=ConfigLoader(settings))
        manager.set_task_service(_FakePMTaskService())
        issuer, _ = _issuer()
        manager.set_token_issuer(issuer)
        captured: dict[str, object] = {}

        class FakeProc:
            returncode = 0

            async def communicate(self, input=None):  # type: ignore[no-untyped-def]
                return (b"OK: 1 ticket creado\n"
                        b'{"tickets": [{"title": "Ticket A"}]}', b"")

        async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
            captured["args"] = args
            captured["kwargs"] = kwargs
            config_at = list(args).index("--mcp-config") + 1
            captured["config_path"] = list(args)[config_at]
            import json as _json

            captured["config"] = _json.loads(
                Path(str(captured["config_path"])).read_text())
            return FakeProc()

        with patch("agents.pm.session.asyncio.create_subprocess_exec",
                   side_effect=fake_exec):
            result = await manager.extract_tickets_subprocess(
                interview_text="login con Google", project="melanyryr",
                tool="claude")
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        args = list(captured["args"])  # type: ignore[arg-type]
        self.assertNotIn("mcp_", " ".join(str(a) for a in args))
        headers = captured["config"]["mcpServers"][  # type: ignore[index]
            "openceo-internal"]["headers"]
        grant = await issuer.authenticate(
            headers["Authorization"].removeprefix("Bearer "))
        self.assertEqual(grant.kind, TokenKind.SERVICE)
        self.assertEqual(grant.project, "melanyryr")
        self.assertFalse(Path(str(captured["config_path"])).exists())

    async def test_codex_path_carries_token_env_override(self) -> None:
        from agents.pm.session import PMSessionManager  # noqa: PLC0415
        from config_loader import ConfigLoader  # noqa: PLC0415
        from state import TaskStatus  # noqa: PLC0415

        settings = Settings(PROJECTS_DIR="projects",
                            DEFAULT_PROJECT="jardal-openceo",
                            MCP_SERVER_ENABLED=True)
        manager = PMSessionManager(
            settings, config_loader=ConfigLoader(settings))
        manager.set_task_service(_FakePMTaskService())
        issuer, _ = _issuer()
        manager.set_token_issuer(issuer)
        captured: dict[str, object] = {}

        class FakeProc:
            returncode = 0

            async def communicate(self, input=None):  # type: ignore[no-untyped-def]
                return (b"OK: 1 ticket creado\n"
                        b'{"tickets": [{"title": "Ticket A"}]}', b"")

        async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProc()

        with patch("agents.pm.session.asyncio.create_subprocess_exec",
                   side_effect=fake_exec):
            result = await manager.extract_tickets_subprocess(
                interview_text="login con Google", project="melanyryr",
                tool="codex")
        self.assertEqual(result.status, TaskStatus.COMPLETED)
        args = list(captured["args"])  # type: ignore[arg-type]
        override = next(
            a for a in args if "bearer_token_env_var" in str(a))
        env = captured["kwargs"]["env"]  # type: ignore[index]
        var = override.split("=", 1)[1].strip().strip('"')
        grant = await issuer.authenticate(env[var])
        self.assertEqual(grant.kind, TokenKind.SERVICE)

    async def test_missing_issuer_fails_closed(self) -> None:
        from agents.pm.session import PMSessionManager  # noqa: PLC0415
        from config_loader import ConfigLoader  # noqa: PLC0415
        from state import TaskStatus  # noqa: PLC0415

        settings = Settings(PROJECTS_DIR="projects",
                            DEFAULT_PROJECT="jardal-openceo",
                            MCP_SERVER_ENABLED=True)
        manager = PMSessionManager(
            settings, config_loader=ConfigLoader(settings))
        manager.set_task_service(_FakePMTaskService())
        result = await manager.extract_tickets_subprocess(
            interview_text="login con Google", project="melanyryr",
            tool="claude")
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("token issuer", (result.error or "").lower())

    async def test_mcp_disabled_needs_no_issuer(self) -> None:
        from agents.pm.session import PMSessionManager  # noqa: PLC0415
        from config_loader import ConfigLoader  # noqa: PLC0415
        from state import TaskStatus  # noqa: PLC0415

        settings = Settings(PROJECTS_DIR="projects",
                            DEFAULT_PROJECT="jardal-openceo",
                            MCP_SERVER_ENABLED=False)
        manager = PMSessionManager(
            settings, config_loader=ConfigLoader(settings))
        manager.set_task_service(_FakePMTaskService())
        with patch("agents.pm.session.asyncio.create_subprocess_exec",
                   side_effect=FileNotFoundError("no-binary")):
            result = await manager.extract_tickets_subprocess(
                interview_text="login con Google", project="melanyryr",
                tool="claude")
        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertIn("Binary not found", result.error or "")


class _FakePMTaskService:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def create_task(self, req: Any) -> dict[str, Any]:
        self.requests.append(req)
        return {"task_id": f"task-{len(self.requests)}", "status": "queued"}

    async def get_task(self, task_id: str) -> None:
        return None


class MongoStoreTests(unittest.IsolatedAsyncioTestCase):
    async def _repo_db(self) -> Any:
        from db.mongo import MongoRepository  # noqa: PLC0415
        from pymongo import MongoClient  # noqa: PLC0415

        client = MongoClient("mongodb://localhost:27017",
                             serverSelectionTimeoutMS=400)
        try:
            client.admin.command("ping")
        finally:
            client.close()
        repo = MongoRepository(Settings(
            _env_file=None,  # type: ignore[call-arg]
            MONGODB_URL="mongodb://localhost:27017",
            MONGODB_DATABASE="openceo_test_mcp_identity"))
        await repo.connect()
        return repo, repo.db

    async def test_roundtrip_survives_reload(self) -> None:
        try:
            from mcp_identity import MongoTokenStore  # noqa: PLC0415
        except ImportError as exc:
            self.skipTest(f"pymongo unavailable ({exc})")
        try:
            repo, db = await self._repo_db()
        except Exception as exc:
            self.skipTest(f"mongo unavailable ({exc})")
        try:
            store = MongoTokenStore(
                db_provider=lambda: db,
                collection="mcp_token_grants_test196")
            issuer = TokenIssuer(store, {"dev": _dev_card()})
            token = await issuer.issue_agent("dev", DEV_TICKET, DEV_PROJECT)
            reloaded = TokenIssuer(
                MongoTokenStore(db, collection="mcp_token_grants_test196"),
                {"dev": _dev_card()})
            grant = await reloaded.authenticate(token)
            self.assertEqual(grant.ticket, DEV_TICKET)
            self.assertEqual(await reloaded.revoke_ticket(DEV_TICKET), 1)
            with self.assertRaises(McpAuthError):
                await reloaded.authenticate(token)
            import hashlib as _hashlib

            await db["mcp_token_grants_test196"].insert_one({
                "digest": _hashlib.sha256(
                    b"corrupt-record-token").hexdigest(),
                "grant": {"kind": "not-a-kind"},
                "revoked": False,
                "revoked_reason": "",
            })
            with self.assertRaises(McpAuthError):
                await reloaded.authenticate("corrupt-record-token")
            indexes = await db[
                "mcp_token_grants_test196"].index_information()
            self.assertTrue(indexes["digest_1"].get("unique"))
            self.assertIn("grant.ticket_1", indexes)
            self.assertEqual(
                indexes["grant.expires_at_1"].get("expireAfterSeconds"), 0)
            stored = await db["mcp_token_grants_test196"].find_one(
                {"grant.ticket": DEV_TICKET})
            self.assertIsNotNone(stored)
            self.assertNotIn("mcp_", str(stored))
        finally:
            await db["mcp_token_grants_test196"].drop()
            await repo.close()


class _RecordingIssuer:
    def __init__(self, fail: bool = False) -> None:
        self.revoked: list[str] = []
        self._fail = fail

    async def revoke_ticket(self, ticket: str) -> int:
        if self._fail:
            raise RuntimeError("store down")
        self.revoked.append(ticket)
        return 1


class _HookRepo:
    def __init__(self, task: dict[str, Any] | None = None,
                 fail: bool = False) -> None:
        self._task = task
        self._fail = fail
        self.lookups: list[str] = []

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        self.lookups.append(task_id)
        if self._fail:
            raise RuntimeError("db down")
        return self._task


def _hooked_service(issuer: Any, repo: Any) -> Any:
    from task_service import TaskService  # noqa: PLC0415

    service = TaskService.__new__(TaskService)
    service._token_issuer = issuer
    service._repo = repo
    return service


def _mirrored_task(ticket: str) -> dict[str, Any]:
    return {"task_id": "exec-1",
            "params": {"_mirrors": {"erpnext": {"name": ticket}}}}


class RevokeHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_task_revokes_its_badges(self) -> None:
        issuer = _RecordingIssuer()
        service = _hooked_service(issuer, _HookRepo())
        await service._revoke_ticket_badges(_mirrored_task(DEV_TICKET))
        self.assertEqual(issuer.revoked, [DEV_TICKET])

    async def test_unmirrored_task_revokes_nothing(self) -> None:
        issuer = _RecordingIssuer()
        service = _hooked_service(issuer, _HookRepo())
        await service._revoke_ticket_badges({"task_id": "exec-1"})
        await service._revoke_ticket_badges(None)
        self.assertEqual(issuer.revoked, [])

    async def test_hook_never_breaks_the_close(self) -> None:
        service = _hooked_service(_RecordingIssuer(fail=True), _HookRepo())
        await service._revoke_ticket_badges(_mirrored_task(DEV_TICKET))
        service = _hooked_service(None, _HookRepo())
        await service._revoke_ticket_badges(_mirrored_task(DEV_TICKET))

    async def test_patch_hook_filters_non_terminal(self) -> None:
        issuer = _RecordingIssuer()
        repo = _HookRepo(_mirrored_task(DEV_TICKET))
        service = _hooked_service(issuer, repo)
        await service.revoke_badges_for_patch("exec-1", "completed")
        self.assertEqual(issuer.revoked, [DEV_TICKET])
        await service.revoke_badges_for_patch("exec-1", "in_progress")
        await service.revoke_badges_for_patch("exec-1", None)
        self.assertEqual(issuer.revoked, [DEV_TICKET])
        self.assertEqual(len(repo.lookups), 1)

    async def test_patch_hook_swallows_lookup_failure(self) -> None:
        issuer = _RecordingIssuer()
        service = _hooked_service(issuer, _HookRepo(fail=True))
        await service.revoke_badges_for_patch("exec-1", "completed")
        self.assertEqual(issuer.revoked, [])


class ProvisioningTests(unittest.IsolatedAsyncioTestCase):
    async def test_provision_owner_returns_plaintext_once(self) -> None:
        from mcp_identity import provision_owner_token  # noqa: PLC0415

        store = MemoryTokenStore()
        token = await provision_owner_token(store)
        self.assertTrue(token)
        issuer = TokenIssuer(store, {})
        grant = await issuer.authenticate(token)
        self.assertEqual(grant.kind, TokenKind.OWNER)


if __name__ == "__main__":
    unittest.main()
