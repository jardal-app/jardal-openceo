"""Boundary tools: cleaned registry, MCP exposure per agent box (TASK-2026-00199).

The registry keeps only justified tools (plus the internals and the
legacy entries other tickets own); the MCP mounts expose the boundary
tools filtered by each agent's box. No tokens.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import yaml

CORE = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from catalog import (  # noqa: E402
    CatalogError,
    MCP_BOUNDARY_TOOLS,
    load_catalog,
)
from mcp_identity import McpDenied, TicketGrant, TokenKind  # noqa: E402
from mcp_server import (  # noqa: E402
    _register_all_tools,
    _register_infra_tools,
)
from settings import Settings  # noqa: E402
from workflow_service import (  # noqa: E402
    MissingWorkflowToolError,
    WorkflowToolRegistry,
)

FAST_MCP_AVAILABLE = True
try:
    from fastmcp import FastMCP  # noqa: E402
except ImportError:  # pragma: no cover - FastMCP is a hard test dependency
    FAST_MCP_AVAILABLE = False
    FastMCP = None  # type: ignore[assignment]

DEV_TICKET = "TASK-2026-00199"
DEV_PROJECT = "jardal-openceo"

EXPECTED_REGISTRY = frozenset({
    "core.complete",
    "core.echo",
    "core.query_tasks",
    "core.query_workflow_runs",
    "secretary.send_email",
    "secretary.list_sender_accounts",
    "secretary.save_note",
    "secretary.query_notes",
    "finance.pago_elfec_house",
    "finance.register_expense",
    "finance.register_income",
    "finance.get_scope_summary",
    "finance.get_contribution_10_status",
    "ceo.plan_day",
    "ceo.review_objectives",
    "ceo.get_next_action",
    "ceo.capture_context",
    "ceo.record_progress",
    "ops.check_project_spend",
    "ops.escalate_project_spend",
    "ops.ensure_erpnext_project",
    "ops.ensure_git_repo",
    "ops.ensure_repo_templates",
    "ops.ensure_project_config",
    "ops.ensure_vault_entry",
    "ops.ensure_dns_record",
})

REMOVED_TOOLS = (
    "pm.create_ticket",
    "pm.create_tickets_batch",
    "pm.extract_tickets",
    "core.check_registry",
    "cloudflare.upsert_dns_record",
    "cloudflare.delete_dns_record",
)

RESEARCH_TOOLS = (
    "research.investigate",
    "research.watch_tick",
    "research.discovery_tick",
)

EXPECTED_MCP = frozenset({
    "manage_dns_record",
    "openceo_create_ticket",
    "openceo_create_tickets_batch",
    "openceo_create_pm_tickets_batch",
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_create_objective",
    "openceo_transition_ticket",
    "openceo_transition_tickets_batch",
    "openceo_update_ticket",
    "openceo_add_ticket_comment",
    "openceo_update_ticket_comment",
    "openceo_delete_ticket_comment",
    "openceo_attach_ticket_file",
    "openceo_get_ticket_attachment",
    "openceo_update_ticket_attachment",
    "openceo_delete_ticket_attachment",
    "openceo_send_email",
    "openceo_ensure_erpnext_project",
    "openceo_register_expense",
    "openceo_register_income",
    "openceo_scope_summary",
    "openceo_contribution_10_status",
    "openceo_elfec_house_payment",
})

DEV_BOX = frozenset({
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_add_ticket_comment",
    "openceo_attach_ticket_file",
})


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


class _FakeTaskService:
    async def get_task(self, task_id: str) -> None:
        return None


class _FakeErpnext:
    is_enabled = True

    async def search_tickets(self, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class RegistrySetTests(unittest.TestCase):
    def test_builtin_names_match_documented_set(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        self.assertEqual(set(registry.names()), set(EXPECTED_REGISTRY))

    def test_removed_tools_are_unknown(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        described = registry.describe()
        for name in REMOVED_TOOLS:
            with self.subTest(tool=name):
                self.assertNotIn(name, described)

    async def _assert_call_unknown(self, registry: WorkflowToolRegistry,
                                   name: str) -> None:
        with self.assertRaises(MissingWorkflowToolError):
            await registry.call(name, {}, {})

    def test_removed_tools_raise_on_call(self) -> None:
        import asyncio

        registry = WorkflowToolRegistry(settings=_settings())
        for name in REMOVED_TOOLS:
            with self.subTest(tool=name):
                asyncio.run(self._assert_call_unknown(registry, name))

    def test_every_registered_tool_has_doc(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        for name, spec in registry.describe().items():
            with self.subTest(tool=name):
                self.assertTrue((spec.get("doc") or "").strip())

    def test_research_tools_stay_conditional_and_internal(self) -> None:
        registry = WorkflowToolRegistry(settings=_settings())
        for name in RESEARCH_TOOLS:
            self.assertNotIn(name, registry.names())
            self.assertNotIn(name, MCP_BOUNDARY_TOOLS)
        registry.set_research_service(object())
        registry.set_research_watch(object())
        for name in RESEARCH_TOOLS:
            self.assertIn(name, registry.names())


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class MCPBoundarySetTests(unittest.TestCase):
    def _server(self) -> Any:
        server: Any = FastMCP("boundary-test")
        _register_all_tools(
            server, _settings(), _FakeTaskService(), _FakeErpnext())
        return server

    def test_known_set_matches_ticket_scope(self) -> None:
        self.assertEqual(set(MCP_BOUNDARY_TOOLS), set(EXPECTED_MCP))

    def test_all_registered_match_known_set(self) -> None:
        server: Any = FastMCP("boundary-names-test")
        names = _register_all_tools(
            server, _settings(), _FakeTaskService(), _FakeErpnext())
        self.assertEqual(set(names), set(EXPECTED_MCP))

    def test_infra_only_registers_dns(self) -> None:
        server: Any = FastMCP("boundary-infra-test")
        names = _register_infra_tools(server, _settings())
        self.assertEqual(tuple(names), ("manage_dns_record",))

    def test_build_rejects_unknown_tool(self) -> None:
        from mcp_server import _build_mcp  # noqa: PLC0415

        def _rogue(server: Any, settings: Settings) -> tuple[str, ...]:
            async def rogue_tool() -> dict[str, str]:
                return {"ok": "no"}

            server.tool(rogue_tool, name="rogue_tool")
            return ("rogue_tool",)

        with self.assertRaises(ValueError):
            _build_mcp(
                settings=_settings(),
                path="/internal/mcp-test",
                name="rogue-test",
                register_fn=_rogue,
            )

    def test_every_description_documents_criterion(self) -> None:
        import asyncio

        from fastmcp import Client  # noqa: PLC0415

        async def _descriptions() -> dict[str, str]:
            async with Client(self._server()) as client:
                tools = await client.list_tools()
            return {tool.name: tool.description or "" for tool in tools}

        descriptions = asyncio.run(_descriptions())
        self.assertEqual(set(descriptions), set(EXPECTED_MCP))
        for name, description in descriptions.items():
            with self.subTest(tool=name):
                self.assertRegex(
                    description, r"Boundary \((a|b|c)\)",
                    "each boundary tool documents its criterion")


class CatalogBoxTests(unittest.TestCase):
    def test_owner_box_is_the_full_set(self) -> None:
        catalog = load_catalog()
        self.assertEqual(
            set(catalog.agents["owner"].boundary_tools),
            set(EXPECTED_MCP))

    def test_dev_box_unchanged(self) -> None:
        catalog = load_catalog()
        self.assertEqual(
            set(catalog.agents["dev"].boundary_tools), set(DEV_BOX))

    def test_pm_batch_only_in_owner_box(self) -> None:
        catalog = load_catalog()
        for name, card in catalog.agents.items():
            with self.subTest(agent=name):
                if name == "owner":
                    self.assertIn(
                        "openceo_create_pm_tickets_batch",
                        card.boundary_tools)
                else:
                    self.assertNotIn(
                        "openceo_create_pm_tickets_batch",
                        card.boundary_tools)

    def test_loader_rejects_unknown_box_tool(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "catalog"
            agents = root / "agents"
            agents.mkdir(parents=True)
            dev = {
                "name": "dev", "type": "ai", "engine": "muse-code",
                "profile": None, "boundary_tools": ["rogue_tool"],
                "skills_dir": "skills/dev", "skills": [],
                "inputs": [], "outputs": [], "enabled": True,
                "disabled_reason": None, "decided_by_ticket": None,
            }
            (agents / "dev.yaml").write_text(
                yaml.safe_dump(dev, sort_keys=False), encoding="utf-8")
            (root / "index.yaml").write_text(
                yaml.safe_dump(
                    {"version": 1, "agents": [
                        {"name": "dev", "summary": "d",
                         "card": "agents/dev.yaml"}]},
                    sort_keys=False),
                encoding="utf-8")
            (root / "staff.yaml").write_text(
                yaml.safe_dump({"roster": []}, sort_keys=False),
                encoding="utf-8")
            with self.assertRaises(CatalogError) as raised:
                load_catalog(root)
        self.assertIn("rogue_tool", str(raised.exception))


class PMBatchOwnerOnlyTests(unittest.TestCase):
    def _grant(self, kind: TokenKind) -> TicketGrant:
        return TicketGrant(
            kind=kind, agent="dev", ticket=DEV_TICKET,
            project=DEV_PROJECT,
            tools=frozenset({"openceo_create_pm_tickets_batch"}))

    def test_owner_may_call(self) -> None:
        self._grant(TokenKind.OWNER).check(
            "openceo_create_pm_tickets_batch",
            {"project": "other", "source_id": "s", "tickets": "[]"})

    def test_service_may_call_until_00209_sunset(self) -> None:
        """The SERVICE exception is the retiring PM subprocess path.

        TASK-2026-00209 removes it; that ticket deletes this test and
        narrows the rule to OWNER alone.
        """
        self._grant(TokenKind.SERVICE).check(
            "openceo_create_pm_tickets_batch",
            {"project": DEV_PROJECT, "source_id": "s", "tickets": "[]"})
        with self.assertRaises(McpDenied):
            self._grant(TokenKind.SERVICE).check(
                "openceo_create_pm_tickets_batch",
                {"project": "other", "source_id": "s", "tickets": "[]"})

    def test_agent_denied_even_in_box(self) -> None:
        with self.assertRaises(McpDenied):
            self._grant(TokenKind.AGENT).check(
                "openceo_create_pm_tickets_batch",
                {"project": DEV_PROJECT, "source_id": "s", "tickets": "[]"})

    def test_readonly_denied(self) -> None:
        with self.assertRaises(McpDenied):
            self._grant(TokenKind.READONLY).check(
                "openceo_create_pm_tickets_batch",
                {"project": DEV_PROJECT, "source_id": "s", "tickets": "[]"})


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class DNSToolTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, calls: list[tuple[str, str]],
                    **kwargs: Any) -> Any:
        from fastmcp import Client  # noqa: PLC0415

        class _FakeManager:
            def __init__(self, settings: Settings) -> None:
                pass

            def check_or_create(self, hostname: str) -> dict[str, Any]:
                calls.append(("create", hostname))
                return {"ok": True, "hostname": hostname}

            def delete(self, hostname: str) -> dict[str, Any]:
                calls.append(("delete", hostname))
                return {"ok": True, "hostname": hostname}

        server: Any = FastMCP("boundary-dns-test")
        with patch("mcp_server.CloudflareDNSManager", _FakeManager):
            _register_infra_tools(server, _settings())
        async with Client(server) as client:
            return await client.call_tool("manage_dns_record", kwargs)

    async def test_create_checks_or_creates(self) -> None:
        calls: list[tuple[str, str]] = []
        result = await self._call(calls, hostname="h.example",
                                  action="create")
        self.assertEqual(calls, [("create", "h.example")])
        self.assertTrue(result.data["ok"])

    async def test_delete_removes(self) -> None:
        calls: list[tuple[str, str]] = []
        result = await self._call(calls, hostname="h.example",
                                  action="delete")
        self.assertEqual(calls, [("delete", "h.example")])
        self.assertTrue(result.data["ok"])

    async def test_old_dns_name_is_gone(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        server: Any = FastMCP("boundary-dns-gone-test")
        _register_infra_tools(server, _settings())
        async with Client(server) as client:
            tools = await client.list_tools()
            self.assertNotIn("manage_dns_check_or_create",
                             {tool.name for tool in tools})
            with self.assertRaises(ToolError):
                await client.call_tool(
                    "manage_dns_check_or_create", {"hostname": "h.example"})


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class EmailDescriptionTests(unittest.TestCase):
    def _descriptions(self) -> dict[str, str]:
        import asyncio

        from fastmcp import Client  # noqa: PLC0415

        async def _collect() -> dict[str, str]:
            server: Any = FastMCP("boundary-email-test")
            _register_all_tools(
                server, _settings(), _FakeTaskService(), _FakeErpnext())
            async with Client(server) as client:
                tools = await client.list_tools()
            return {tool.name: tool.description or "" for tool in tools}

        return asyncio.run(_collect())

    def test_description_lists_senders(self) -> None:
        class _FakeSender:
            def __init__(self, settings: Settings) -> None:
                pass

            def list_accounts(self) -> list[dict[str, str]]:
                return [{"email": "a@jardal.app", "display_name": "A",
                         "header_name": "A"},
                        {"email": "b@jardal.app", "display_name": "B",
                         "header_name": "B"}]

        with patch("mcp_server.SMTPEmailSender", _FakeSender):
            descriptions = self._descriptions()
        description = descriptions["openceo_send_email"]
        self.assertIn("a@jardal.app", description)
        self.assertIn("b@jardal.app", description)

    def test_description_without_accounts(self) -> None:
        descriptions = self._descriptions()
        self.assertIn("no sender account", descriptions["openceo_send_email"])


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class BoundaryDelegationTests(unittest.IsolatedAsyncioTestCase):
    def _server(self) -> Any:
        server: Any = FastMCP("boundary-delegation-test")
        _register_all_tools(
            server, _settings(), _FakeTaskService(), _FakeErpnext())
        return server

    async def test_expense_delegates_to_registry(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        calls: list[dict[str, Any]] = []

        async def fake_expense(
            text: str = "",
            openceo_ref: str | None = None,
            submit: bool = True,
            exchange_rate: str | None = None,
            context: dict[str, Any] | None = None,
        ) -> dict[str, bool]:
            calls.append({
                "text": text, "openceo_ref": openceo_ref,
                "submit": submit, "exchange_rate": exchange_rate,
                "context": context,
            })
            return {"ok": True}

        with patch.object(WorkflowToolRegistry, "_finance_register_expense",
                          new=staticmethod(fake_expense)):
            server = self._server()
            async with Client(server) as client:
                result = await client.call_tool(
                    "openceo_register_expense",
                    {"text": "x", "openceo_ref": "r1"})
        self.assertTrue(result.data["ok"])
        self.assertEqual(calls, [{
            "text": "x", "openceo_ref": "r1", "submit": True,
            "exchange_rate": None, "context": {},
        }])

    async def test_money_without_ref_rejected(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        server = self._server()
        async with Client(server) as client:
            for tool in ("openceo_register_expense",
                         "openceo_register_income"):
                with self.subTest(tool=tool):
                    result = await client.call_tool(tool, {"text": "x"})
                    self.assertFalse(result.data["ok"])
                    self.assertEqual(result.data["status"], "invalid_input")
                    blank = await client.call_tool(
                        tool, {"text": "x", "openceo_ref": "  "})
                    self.assertFalse(blank.data["ok"])
                    self.assertEqual(blank.data["status"], "invalid_input")

    async def test_tool_error_maps_to_not_ok(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        with patch.object(
                WorkflowToolRegistry, "_finance_register_expense",
                new=AsyncMock(side_effect=RuntimeError("boom"))):
            server = self._server()
            async with Client(server) as client:
                result = await client.call_tool(
                    "openceo_register_expense",
                    {"text": "x", "openceo_ref": "r1"})
        self.assertFalse(result.data["ok"])

    async def test_project_uses_adapter(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        from tools.erpnext_tickets import ERPNextTicketAdapter  # noqa: PLC0415

        erpnext = AsyncMock(spec=ERPNextTicketAdapter)
        erpnext.is_enabled = True
        erpnext.find_project.return_value = None
        erpnext.ensure_project.return_value = "PROJ-x"
        server: Any = FastMCP("boundary-project-test")
        _register_all_tools(
            server, _settings(), _FakeTaskService(), erpnext)
        async with Client(server) as client:
            result = await client.call_tool(
                "openceo_ensure_erpnext_project",
                {"project_key": "jardal-openceo", "display_name": "X"})
        self.assertEqual(result.data["name"], "PROJ-x")
        self.assertTrue(result.data["created"])

    async def test_project_without_adapter_is_not_configured(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        server: Any = FastMCP("boundary-project-off-test")
        _register_all_tools(
            server, _settings(), _FakeTaskService(), None)
        async with Client(server) as client:
            result = await client.call_tool(
                "openceo_ensure_erpnext_project", {"project_key": "x"})
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["status"], "not_configured")

    async def test_elfec_stays_practice(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from unittest.mock import Mock  # noqa: PLC0415

        server = self._server()
        with patch.object(
                WorkflowToolRegistry, "_finance_pago_elfec_house",
                new=staticmethod(Mock(return_value={
                    "ok": True, "practice_mode": True}))):
            async with Client(server) as client:
                result = await client.call_tool(
                    "openceo_elfec_house_payment", {})
        self.assertTrue(result.data["practice_mode"])

    async def test_elfec_refuses_non_practice(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from unittest.mock import Mock  # noqa: PLC0415

        server = self._server()
        with patch.object(
                WorkflowToolRegistry, "_finance_pago_elfec_house",
                new=staticmethod(Mock(return_value={
                    "ok": True, "practice_mode": False}))):
            async with Client(server) as client:
                result = await client.call_tool(
                    "openceo_elfec_house_payment", {})
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["status"], "refused")

    async def test_email_sends_off_loop(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from unittest.mock import Mock  # noqa: PLC0415

        server = self._server()
        sender = Mock(return_value={"ok": True})
        with patch("agents.secretary.tools.send_email", sender):
            async with Client(server) as client:
                result = await client.call_tool(
                    "openceo_send_email",
                    {"to": "t@example.com", "subject": "s",
                     "body_text": "b"})
        self.assertTrue(result.data["ok"])
        sender.assert_called_once_with(
            from_email=None, to="t@example.com", subject="s",
            body_text="b", body_html=None, cc=None, bcc=None)


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class NewToolBudgetTests(unittest.TestCase):
    """The 8 non-thread tools keep the same description budget as the rest.

    Every description word travels in each session request, so growth is a
    visible decision, not drift. The email sender list is representative (2
    accounts); production grows with its SMTP accounts.
    """

    BUDGETED = (
        "manage_dns_record",
        "openceo_send_email",
        "openceo_ensure_erpnext_project",
        "openceo_register_expense",
        "openceo_register_income",
        "openceo_scope_summary",
        "openceo_contribution_10_status",
        "openceo_elfec_house_payment",
    )

    def test_new_tools_fit_budget(self) -> None:
        import asyncio

        from fastmcp import Client  # noqa: PLC0415

        class _FakeSender:
            def __init__(self, settings: Settings) -> None:
                pass

            def list_accounts(self) -> list[dict[str, str]]:
                return [{"email": "a@jardal.app", "display_name": "A",
                         "header_name": "A"},
                        {"email": "b@jardal.app", "display_name": "B",
                         "header_name": "B"}]

        async def _descriptions() -> dict[str, str]:
            server: Any = FastMCP("boundary-budget-test")
            with patch("mcp_server.SMTPEmailSender", _FakeSender):
                _register_all_tools(
                    server, _settings(), _FakeTaskService(),
                    _FakeErpnext())
            async with Client(server) as client:
                tools = await client.list_tools()
            return {tool.name: tool.description or "" for tool in tools}

        descriptions = asyncio.run(_descriptions())
        for name in self.BUDGETED:
            with self.subTest(tool=name):
                description = descriptions[name]
                self.assertLessEqual(len(description), 220)
                self.assertLessEqual(len(description.split()), 32)


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class AnonymousBoxTests(unittest.IsolatedAsyncioTestCase):
    """No token, no badge: the default project-management box (Owner order).

    Ticket tools, DNS and the ERPNext project stay open so the Owner keeps
    working from anywhere; the agent-specific tools (PM batch, email,
    finance) need a badge. A forged/unknown token still fails closed.
    """

    EXPECTED_ANONYMOUS = frozenset({
        "manage_dns_record",
        "openceo_create_ticket",
        "openceo_create_tickets_batch",
        "openceo_search_tickets",
        "openceo_get_ticket",
        "openceo_create_objective",
        "openceo_transition_ticket",
        "openceo_transition_tickets_batch",
        "openceo_update_ticket",
        "openceo_add_ticket_comment",
        "openceo_update_ticket_comment",
        "openceo_delete_ticket_comment",
        "openceo_attach_ticket_file",
        "openceo_get_ticket_attachment",
        "openceo_update_ticket_attachment",
        "openceo_delete_ticket_attachment",
        "openceo_ensure_erpnext_project",
    })

    def _server(self) -> Any:
        from mcp_identity import (  # noqa: PLC0415
            MCPIdentityMiddleware,
            MemoryAuditSink,
            MemoryTokenStore,
            TokenIssuer,
        )

        server: Any = FastMCP("boundary-anonymous-test")
        _register_all_tools(
            server, _settings(), _FakeTaskService(), _FakeErpnext())
        issuer = TokenIssuer(MemoryTokenStore(), load_catalog().agents)
        server.add_middleware(MCPIdentityMiddleware(issuer, MemoryAuditSink()))
        return server

    async def test_anonymous_list_is_default_box(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        async with Client(self._server()) as client:
            with patch("mcp_identity.get_http_request") as req:
                req.return_value.headers = {}
                tools = await client.list_tools()
        self.assertEqual(
            {tool.name for tool in tools}, set(self.EXPECTED_ANONYMOUS))

    async def test_anonymous_ticket_call_ok(self) -> None:
        from fastmcp import Client  # noqa: PLC0415

        async with Client(self._server()) as client:
            with patch("mcp_identity.get_http_request") as req:
                req.return_value.headers = {}
                result = await client.call_tool(
                    "openceo_search_tickets", {"limit": 5})
        self.assertTrue(result.data["ok"])

    async def test_anonymous_agent_tools_denied(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        async with Client(self._server()) as client:
            for tool in ("openceo_create_pm_tickets_batch",
                         "openceo_send_email",
                         "openceo_register_expense"):
                with self.subTest(tool=tool):
                    with patch("mcp_identity.get_http_request") as req:
                        req.return_value.headers = {}
                        with self.assertRaises(ToolError) as raised:
                            await client.call_tool(tool, {})
        self.assertIn("badge", str(raised.exception).lower())

    async def test_invalid_token_still_rejected(self) -> None:
        from fastmcp import Client  # noqa: PLC0415
        from mcp.shared.exceptions import McpError  # noqa: PLC0415

        async with Client(self._server()) as client:
            with patch("mcp_identity.get_http_request") as req:
                req.return_value.headers = {
                    "authorization": "Bearer bogus"}
                with self.assertRaises(McpError):
                    await client.list_tools()


@unittest.skipUnless(FAST_MCP_AVAILABLE, "FastMCP is required")
class BoxAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def _names(self, token: str) -> set[str]:
        from fastmcp import Client  # noqa: PLC0415
        from mcp_identity import (  # noqa: PLC0415
            MCPIdentityMiddleware,
            MemoryAuditSink,
            MemoryTokenStore,
            TokenIssuer,
        )

        server: Any = FastMCP("boundary-acceptance-test")
        _register_all_tools(
            server, _settings(), _FakeTaskService(), _FakeErpnext())
        issuer = TokenIssuer(MemoryTokenStore(), load_catalog().agents)
        server.add_middleware(MCPIdentityMiddleware(issuer, MemoryAuditSink()))
        issued = token
        if token == "dev":
            issued = await issuer.issue_agent(
                "dev", DEV_TICKET, DEV_PROJECT)
        elif token == "owner":
            issued = await issuer.issue_owner()
        elif token == "readonly":
            issued = await issuer.issue_readonly("planner")
        async with Client(server) as client:
            with patch("mcp_identity.get_http_request") as req:
                req.return_value.headers = {
                    "authorization": f"Bearer {issued}"}
                tools = await client.list_tools()
        return {tool.name for tool in tools}

    async def test_dev_list_tools_is_its_box(self) -> None:
        self.assertEqual(await self._names("dev"), set(DEV_BOX))

    async def test_owner_list_tools_is_full_set(self) -> None:
        self.assertEqual(await self._names("owner"), set(EXPECTED_MCP))

    async def test_readonly_list_tools_is_readonly_box(self) -> None:
        self.assertEqual(
            await self._names("readonly"),
            {"openceo_search_tickets", "openceo_get_ticket"})


if __name__ == "__main__":
    unittest.main()
