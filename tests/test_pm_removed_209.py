"""PM retired: no second ticket creator (TASK-2026-00209).

PM extracted tickets on its own (DeepSeek interview flow, chat model,
endpoints); marketing, sales and media were never wired. All four are
gone from the enum, the router, the catalog and the code: the former
PM entries produce one pedido for the single planner, and the PM batch
tool is Owner-only.

No network, no LLM: the endpoint runs against a fake task service.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from catalog import find_consumer_mismatches, load_catalog  # noqa: E402
from config_loader import ConfigLoader  # noqa: E402
from graph import WIRED_AGENTS, OpenCEOGraph  # noqa: E402
from mcp_identity import McpDenied, TicketGrant, TokenKind  # noqa: E402
from router import CEORouter  # noqa: E402
from state import AgentName  # noqa: E402


def _planner_registry() -> dict[str, Any]:
    cfg = ConfigLoader._merge_with_defaults("", {})
    return dict(cfg["objective_planner"]["agents"])


class EnumRemovalTests(unittest.TestCase):
    def test_retired_agents_left_the_enum(self) -> None:
        names = {member.value for member in AgentName}
        for gone in ("pm", "marketing", "sales", "media"):
            self.assertNotIn(gone, names)
        for kept in ("dev", "finance", "secretary", "operations",
                     "research", "ceo"):
            self.assertIn(kept, names)

    def test_router_vocabulary_lost_the_aliases(self) -> None:
        router = CEORouter()
        for hint in ("pm", "PM", "marketing", "sales", "media"):
            with self.subTest(hint=hint):
                self.assertIsNone(router.resolve_explicit(hint, None))
        self.assertEqual(router.resolve_explicit("dev", None),
                         AgentName.DEV)

    def test_coding_tool_still_forces_dev(self) -> None:
        router = CEORouter()
        for tool in ("tier_s", "tier_c", "codex", "muse"):
            with self.subTest(tool=tool):
                self.assertEqual(
                    router.resolve_explicit("pm", tool), AgentName.DEV)


class CatalogRemovalTests(unittest.TestCase):
    def test_retired_cards_are_gone(self) -> None:
        catalog = load_catalog()
        for gone in ("pm", "marketing", "sales", "media"):
            self.assertNotIn(gone, catalog.agents)

    def test_real_views_match_without_legacy(self) -> None:
        catalog = load_catalog()
        mismatches = find_consumer_mismatches(
            [member.value for member in AgentName],
            [member.value for member in CEORouter.AGENT_ALIASES.values()],
            _planner_registry(),
            WIRED_AGENTS,
            catalog)
        self.assertEqual([], mismatches)


class GraphRemovalTests(unittest.TestCase):
    def test_only_dev_stays_wired(self) -> None:
        self.assertEqual(("dev",), tuple(WIRED_AGENTS))

    def test_pm_routes_to_unsupported(self) -> None:
        graph = OpenCEOGraph(None, None, None)
        self.assertEqual(
            "unsupported",
            graph._after_ceo_route({"selected_agent": "pm"}))
        self.assertEqual(
            "dev", graph._after_ceo_route({"selected_agent": "dev"}))

    def test_pm_module_stays_gone(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            import agents.pm.session  # noqa: F401


class OwnerOnlyBatchTests(unittest.TestCase):
    def _grant(self, kind: TokenKind) -> TicketGrant:
        return TicketGrant(
            kind=kind, agent="dev", ticket="TASK-1",
            project="jardal-openceo",
            tools=frozenset({"openceo_create_pm_tickets_batch"}))

    def test_owner_may_call(self) -> None:
        self._grant(TokenKind.OWNER).check(
            "openceo_create_pm_tickets_batch",
            {"project": "other", "source_id": "s", "tickets": "[]"})

    def test_service_denied_after_sunset(self) -> None:
        with self.assertRaises(McpDenied):
            self._grant(TokenKind.SERVICE).check(
                "openceo_create_pm_tickets_batch",
                {"project": "jardal-openceo", "source_id": "s",
                 "tickets": "[]"})

    def test_agent_denied_even_in_box(self) -> None:
        with self.assertRaises(McpDenied):
            self._grant(TokenKind.AGENT).check(
                "openceo_create_pm_tickets_batch",
                {"project": "jardal-openceo", "source_id": "s",
                 "tickets": "[]"})


class PmExtractPedidoTests(unittest.IsolatedAsyncioTestCase):
    async def test_extract_creates_one_undecided_ticket(self) -> None:
        import main
        from state import TaskCreateRequest

        created: list[Any] = []

        async def fake_create(payload: TaskCreateRequest) -> dict[str, Any]:
            created.append(payload)
            return {"task_id": "task-1", "status": "queued"}

        fake_service = MagicMock()
        fake_service.create_task = AsyncMock(side_effect=fake_create)
        fake_loader = MagicMock()
        fake_loader.has_project.return_value = True
        user = MagicMock()
        with unittest.mock.patch.object(main, "task_service",
                                        fake_service), \
                unittest.mock.patch.object(main, "config_loader",
                                           fake_loader):
            answer = await main.pm_extract_tickets(
                {"project": "proj", "interview": "Build a store",
                 "tool": "langraph"},
                user)
        self.assertEqual(len(created), 1)
        pedido = created[0]
        self.assertIsNone(pedido.agent_hint)
        self.assertIsNone(pedido.coding_tool)
        self.assertNotIn("target_agent", (pedido.params or {}))
        self.assertEqual(answer["task_id"], "task-1")

    async def test_extract_rejects_missing_fields(self) -> None:
        import main
        from fastapi import HTTPException

        user = MagicMock()
        for payload in ({}, {"project": "p"}, {"interview": "x"}):
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPException):
                    await main.pm_extract_tickets(dict(payload), user)

    async def test_extract_rejects_unknown_project(self) -> None:
        import main
        from fastapi import HTTPException

        fake_loader = MagicMock()
        fake_loader.has_project.return_value = False
        user = MagicMock()
        with unittest.mock.patch.object(main, "config_loader",
                                        fake_loader):
            with self.assertRaises(HTTPException):
                await main.pm_extract_tickets(
                    {"project": "ghost", "interview": "x"}, user)


class ChatEntryTests(unittest.TestCase):
    def test_pm_is_no_direct_model(self) -> None:
        import main
        self.assertIsNone(main._resolve_direct_agent("pm"))
        self.assertIsNone(main._resolve_direct_agent("openceo-pm"))
        self.assertIsNone(main._resolve_direct_agent("@pm do it"))
        self.assertEqual(main._resolve_direct_agent("dev"), "dev")

    def test_pm_guided_flow_is_gone(self) -> None:
        import main
        self.assertFalse(hasattr(main, "_handle_openceo_pm_direct"))
        from db.mongo import MongoRepository
        for gone in ("get_pm_state", "set_pm_state", "clear_pm_state",
                     "mark_interrupted_pm_states"):
            with self.subTest(method=gone):
                self.assertFalse(hasattr(MongoRepository, gone))
