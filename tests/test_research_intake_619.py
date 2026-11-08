"""External research intake is defined and verified (ticket #619).

The request ticket (Jardal ERP first) is the default way in and HTTP/MCP with
explicit consumer grants is the programmatic way, over the existing facades
(``router``, ``mcp_facade``, ``consumers``, ``ticket_bridge``) and the
consumer->via matrix in ``docs/research-operations.md``. Verified with a
simulated service and mocks: no live models, no network.
"""

from __future__ import annotations

import asyncio
import sys
import unicodedata
import unittest
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "openceo-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from research.consumers import (  # noqa: E402
    CONSUMER_OPERATIONS,
    DEFAULT_CONSUMER_OPERATIONS,
    ConsumerAuthError,
    ConsumerRegistry,
)
from research.contracts import ResearchJobRequest  # noqa: E402
from research.intake import (  # noqa: E402
    CONSUMER_INTAKE,
    DEFAULT_INTAKE,
    INTAKE_VIAS,
    intake_for,
)
from research.mcp_facade import (  # noqa: E402
    FACADE_TOOLS,
    build_research_mcp_facade,
    transport_consumer_token,
)
from research.policies import (  # noqa: E402
    RESEARCH_READ,
    RESEARCH_RUN,
    Principal,
    derive_effective_policy,
)
from research.router import _authorized_or_404, build_research_router  # noqa: E402
from research.ticket_bridge import (  # noqa: E402
    TICKET_CONSUMER_OPERATIONS,
    ticket_owner,
)
from settings import Settings  # noqa: E402
from test_research_agent_222 import _bridge  # noqa: E402

TICKET = "TASK-2026-00619"
PROJECT = "jardal-openceo"

OPERATIONS = REPO / "docs" / "research-operations.md"
ARCH = REPO / "docs" / "ARCHITECTURE_REFERENCE_2026.md"

#: Identity-looking arguments. None of them may appear in a facade tool schema:
#: the owner never travels in arguments or _meta.
IDENTITY_PARAMS = ("owner_id", "owner", "_meta", "session", "session_id", "sessionid", "token", "credential")


def _project_cfg() -> dict[str, Any]:
    return {
        "research": {
            "enabled": True,
            "allowed_modes": ["quick"],
            "classification_ceiling": "public",
            "adapters": {"brave": True, "serper": False, "firecrawl": False, "grounding": False},
            "paid_budget_usd": "0.00",
            "limits": {"quick": {"search_requests": 3, "pages": 5, "steps": 8, "seconds": 120}},
            "allowed_account_refs": [],
        }
    }


def _principal() -> Principal:
    return Principal(
        user_id="usr_owner",
        permissions=frozenset({RESEARCH_RUN, RESEARCH_READ}),
        allowed_projects=frozenset({PROJECT}),
    )


def _intake_section() -> str:
    return OPERATIONS.read_text(encoding="utf-8").split("## Intake externo")[1].split("## ")[0]


def _facade_tools(server: Any) -> list[Any]:
    return asyncio.run(server.list_tools())


def _fold(text: str) -> str:
    """Accent-fold for code<->docs comparison (code stays ASCII by convention)."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


class IntakeDefinitionTest(unittest.TestCase):
    def test_ticket_is_the_default_via(self) -> None:
        self.assertEqual(DEFAULT_INTAKE, "ticket")
        self.assertEqual(intake_for("solicitante externo").via, DEFAULT_INTAKE)

    def test_matrix_covers_every_via_once_per_consumer(self) -> None:
        self.assertEqual(INTAKE_VIAS, ("ticket", "http", "mcp"))
        self.assertEqual(
            sorted(entry.via for entry in CONSUMER_INTAKE), ["http", "mcp", "ticket"]
        )
        self.assertEqual(
            len({entry.consumer for entry in CONSUMER_INTAKE}), len(CONSUMER_INTAKE)
        )

    def test_unknown_consumer_is_a_key_error(self) -> None:
        with self.assertRaises(KeyError):
            intake_for("consumidor inventado")

    def test_ticket_row_uses_the_bridge_operations(self) -> None:
        entry = intake_for("solicitante externo")
        self.assertEqual(entry.operations, TICKET_CONSUMER_OPERATIONS)
        self.assertEqual(entry.operations, ("submit", "get"))
        self.assertIn("ticket_bridge", entry.facade)

    def test_programmatic_rows_stay_inside_the_grant_vocabulary(self) -> None:
        for consumer in ("cliente HTTP programatico", "consumidor MCP programatico"):
            with self.subTest(consumer=consumer):
                for operation in intake_for(consumer).operations:
                    self.assertIn(operation, CONSUMER_OPERATIONS)

    def test_mcp_row_defaults_to_the_registry_default_grant(self) -> None:
        self.assertEqual(
            intake_for("consumidor MCP programatico").operations,
            DEFAULT_CONSUMER_OPERATIONS,
        )

    def test_existing_facades_are_reused_not_replaced(self) -> None:
        router = build_research_router(get_service=lambda request: None)
        paths = sorted({route.path for route in router.routes})
        for expected in (
            "/api/research/jobs",
            "/api/research/jobs/{research_id}",
            "/api/research/jobs/{research_id}/cancel",
        ):
            self.assertIn(expected, paths)
        server = build_research_mcp_facade(
            consumers=ConsumerRegistry(), get_service=lambda: None
        )
        self.assertEqual(
            {tool.name for tool in _facade_tools(server)}, set(FACADE_TOOLS)
        )


class TicketViaTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_cycle_admits_and_polls_under_the_ticket(self) -> None:
        bridge, _consumers, live, _woken = _bridge()
        admitted = await bridge.submit(
            ticket=TICKET, project=PROJECT, purpose="decide X", question="which?"
        )
        self.assertTrue(admitted["ok"], admitted)
        self.assertEqual(live.jobs[admitted["research_id"]]["owner_id"], ticket_owner(TICKET))
        seen = await bridge.get(
            ticket=TICKET, project=PROJECT, research_id=admitted["research_id"]
        )
        self.assertTrue(seen["ok"], seen)
        self.assertEqual(seen["job"]["research_id"], admitted["research_id"])

    async def test_ticket_consumer_is_minimal_submit_and_get_only(self) -> None:
        bridge, consumers, _live, _woken = _bridge()
        await bridge.submit(ticket=TICKET, project=PROJECT, purpose="a", question="qa")
        self.assertFalse(hasattr(bridge, "cancel"))
        self.assertFalse(hasattr(bridge, "subscribe"))
        (grant,) = {consumers.authenticate(token) for token in bridge._tokens.values()}
        self.assertEqual(grant.consumer_id, ticket_owner(TICKET))
        self.assertEqual(grant.owner_id, ticket_owner(TICKET))
        self.assertEqual(tuple(sorted(grant.operations)), tuple(sorted(TICKET_CONSUMER_OPERATIONS)))

    async def test_cross_ticket_read_is_not_found(self) -> None:
        bridge, _consumers, _live, _woken = _bridge()
        admitted = await bridge.submit(
            ticket=TICKET, project=PROJECT, purpose="decide X", question="which?"
        )
        out = await bridge.get(
            ticket="TASK-2026-00001", project=PROJECT, research_id=admitted["research_id"]
        )
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["reason"], "not_found")
        self.assertEqual(out["error"]["status"], 404)


class ProgrammaticGrantsTest(unittest.TestCase):
    def test_missing_and_unknown_credentials_do_not_fail_open(self) -> None:
        registry = ConsumerRegistry()
        token = registry.register(
            consumer_id="jobhunter", owner_id="usr_owner", projects=[PROJECT]
        )
        self.assertTrue(registry.authenticate(token).allows("submit", project_key=PROJECT))
        for bad in (None, "", "inventado"):
            with self.subTest(credential=repr(bad)):
                with self.assertRaises(ConsumerAuthError) as raised:
                    registry.authenticate(bad)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.reason, "unauthenticated")

    def test_grants_are_explicit_per_project_and_operation(self) -> None:
        registry = ConsumerRegistry()
        token = registry.register(
            consumer_id="jobhunter", owner_id="usr_owner", projects=[PROJECT]
        )
        grant = registry.authenticate(token)
        grant.require("submit", project_key=PROJECT)
        with self.assertRaises(ConsumerAuthError):
            grant.require("submit", project_key="otro-proyecto")
        with self.assertRaises(ConsumerAuthError):
            grant.require("events", project_key=PROJECT)

    def test_no_identity_without_transport_no_authorization(self) -> None:
        # Fuera de un transporte MCP con Authorization no hay credencial que leer
        # y el llamante rechaza: nunca se inventa una identidad.
        self.assertEqual(transport_consumer_token(), "")

    def test_no_facade_tool_takes_identity_arguments(self) -> None:
        server = build_research_mcp_facade(
            consumers=ConsumerRegistry(), get_service=lambda: None
        )
        for tool in _facade_tools(server):
            params = {name.lower() for name in (tool.parameters or {}).get("properties", {})}
            for forbidden in IDENTITY_PARAMS:
                self.assertNotIn(
                    forbidden, params, f"{tool.name} acepta identidad por argumento"
                )

    def test_a_forged_payload_owner_grants_nothing(self) -> None:
        honest = ResearchJobRequest(
            project_key=PROJECT, purpose="brief", question="Pregunta"
        )
        forged = ResearchJobRequest(
            project_key=PROJECT, purpose="brief", question="Pregunta",
            owner_id="usr_otro_falsificado",
        )
        self.assertEqual(honest.fingerprint(), forged.fingerprint())
        policy = derive_effective_policy(
            principal=_principal(), request=forged, project_cfg=_project_cfg(),
            settings=Settings(_env_file=None),
        )
        self.assertEqual(policy.owner_id, "usr_owner")

    def test_a_foreign_job_reads_as_404_not_403(self) -> None:
        from fastapi import HTTPException  # noqa: E402

        with self.assertRaises(HTTPException) as raised:
            _authorized_or_404(
                _principal(),
                {"owner_id": "usr_otro", "project_key": PROJECT},
            )
        self.assertEqual(raised.exception.status_code, 404)


class DocsMatrixTest(unittest.TestCase):
    def test_operations_renders_every_matrix_row(self) -> None:
        section = _intake_section()
        self.assertIn("Jardal ERP", section)
        self.assertIn("404", section)
        plain = _fold(section.replace("`", ""))
        for entry in CONSUMER_INTAKE:
            with self.subTest(consumer=entry.consumer):
                self.assertIn(entry.consumer, section)
                self.assertIn(entry.facade, section)
                self.assertIn(f"| {entry.consumer} | {entry.via}", section)
                self.assertIn(f"| {_fold(entry.auth)} |", plain)
                self.assertIn(f"| {_fold(entry.owner_source)} |", plain)
                if entry.via == "mcp":
                    # Prose cell ("las del grant explicito (...) por defecto"):
                    # pin the named default triple, not the wording.
                    self.assertIn(", ".join(entry.operations), plain)
                else:
                    self.assertIn(f"| {', '.join(entry.operations)} |", plain)

    def test_operations_marks_ticket_as_default(self) -> None:
        section = _intake_section()
        self.assertIn("por defecto", section)
        self.assertIn("tests/test_research_intake_619.py", section)

    def test_architecture_reference_records_the_defined_intake(self) -> None:
        text = ARCH.read_text(encoding="utf-8")
        self.assertIn("External intake is defined (ticket #619)", text)
        self.assertNotIn("external intake and the free-research profile are later stretches", text)


if __name__ == "__main__":
    unittest.main()
