"""Fachada HTTP y MCP de research (TASK-2026-00066).

La autorizacion se prueba **sobre el transporte real**: ASGI con el middleware y el JWT
del proyecto para la API, y un transporte MCP HTTP (streamable-http montado en ASGI)
para la fachada MCP. Invocar solo la funcion Python del handler no demostraria nada.

Las dependencias tecnicas externas (repositorio, servicio) son fakes: lo que se prueba
aqui es identidad, permisos, mapeo de errores y que el alcance no venga del payload.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StreamableHttpTransport  # noqa: E402

from auth.jwt import create_access_token  # noqa: E402
from research.contracts import (  # noqa: E402
    ResearchIdempotencyConflict,
    ResearchJobStatus,
    ResearchTransitionError,
    can_transition,
)
from research.consumers import (  # noqa: E402
    ConsumerAuthError,
    ConsumerForbidden,
    ConsumerRegistry,
)
from research.mcp_facade import FACADE_TOOLS, build_research_mcp_facade  # noqa: E402
from research.router import build_research_router  # noqa: E402
from research.service import ResearchDependencies, ResearchService  # noqa: E402
from settings import Settings  # noqa: E402

COOKIE = "openceo_session"


# --------------------------------------------------------------------- dobles


class _FakeRepo:
    """Repositorio en memoria con la semantica de idempotencia que importa."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.claims = 0

    async def create_research_job(self, doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        payload = dict(doc)
        key = payload.get("idempotency_key")
        if key:
            for existing in self.jobs.values():
                same_scope = (
                    existing["project_key"] == payload["project_key"]
                    and existing["owner_id"] == payload["owner_id"]
                    and existing["purpose"] == payload["purpose"]
                    and existing.get("idempotency_key") == key
                )
                if not same_scope:
                    continue
                if existing["request_fingerprint"] != payload["request_fingerprint"]:
                    raise ResearchIdempotencyConflict("payload distinto para la misma clave")
                return dict(existing), False
        self.jobs[payload["research_id"]] = payload
        return dict(payload), True

    async def get_research_job(self, research_id: str) -> dict[str, Any] | None:
        row = self.jobs.get(research_id)
        return dict(row) if row else None

    async def transition_research_job(
        self, research_id: str, new_status: Any, *, updates: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        row = self.jobs.get(research_id)
        if row is None:
            return None
        target = new_status.value if hasattr(new_status, "value") else str(new_status)
        current = ResearchJobStatus(row["status"])
        if not can_transition(current, ResearchJobStatus(target)):
            raise ResearchTransitionError(f"{current.value} -> {target}")
        updated = dict(row)
        updated["status"] = target
        updated.update(updates or {})
        self.jobs[research_id] = updated
        return dict(updated)

    async def patch_research_job(self, research_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        row = self.jobs.get(research_id)
        if row is None:
            return None
        updated = dict(row)
        updated.update(updates)
        self.jobs[research_id] = updated
        return dict(updated)

    async def claim_next_research_job(self, **kwargs: Any) -> dict[str, Any] | None:
        for row in self.jobs.values():
            if row["status"] == ResearchJobStatus.QUEUED.value:
                self.claims += 1
                return dict(row)
        return None

    async def list_research_jobs_with_expired_lease(self, cutoff: Any, limit: int = 50) -> list[dict[str, Any]]:
        return []


class _FakeUsers:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self._docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return dict(doc)
        return None


class _FakeAuthDB:
    def __init__(self, users: list[dict[str, Any]]) -> None:
        self.users = _FakeUsers(users)


USERS = [
    {
        "user_id": "usr_owner",
        "username": "owner",
        "email": "owner@example.org",
        "is_active": True,
        "is_admin": False,
        "all_permissions": ["run_research", "read_research"],
        "allowed_projects": ["jardal-openceo"],
    },
    {
        "user_id": "usr_reader",
        "username": "reader",
        "email": "reader@example.org",
        "is_active": True,
        "is_admin": False,
        "all_permissions": ["read_research"],
        "allowed_projects": ["jardal-openceo"],
    },
    {
        "user_id": "usr_other",
        "username": "other",
        "email": "other@example.org",
        "is_active": True,
        "is_admin": False,
        "all_permissions": ["run_research", "read_research"],
        "allowed_projects": ["otro-proyecto"],
    },
]

PROJECT_CFG = {
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


def _settings() -> Settings:
    return Settings(_env_file=None, RESEARCH_ENABLED=True)


def _service(repo: _FakeRepo, *, enabled: bool = True) -> ResearchService:
    return ResearchService(
        settings=_settings() if enabled else Settings(_env_file=None, RESEARCH_ENABLED=False),
        deps=ResearchDependencies(repo=repo),
    )


def _app(service: ResearchService, *, on_admitted=None, project_cfg: dict[str, Any] | None = None) -> FastAPI:
    app = FastAPI()
    app.state.auth_db = _FakeAuthDB(USERS)
    app.include_router(
        build_research_router(
            get_service=lambda request: service,
            project_cfg_for=lambda request, project_key: project_cfg or PROJECT_CFG,
            on_admitted=on_admitted,
        )
    )
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _cookie(user_id: str) -> dict[str, str]:
    user = next(u for u in USERS if u["user_id"] == user_id)
    return {COOKIE: create_access_token(user["user_id"], user["username"])}


def _body(**overrides: Any) -> dict[str, Any]:
    payload = {
        "project_key": "jardal-openceo",
        "purpose": "brief",
        "mode": "quick",
        "question": "Pregunta",
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------- consumers


class ConsumerRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ConsumerRegistry(ttl_seconds=600)
        self.token = self.registry.register(
            consumer_id="jobhunter", owner_id="usr_owner", projects=["jardal-openceo"]
        )

    def test_a_registered_consumer_authenticates_with_its_grant(self) -> None:
        grant = self.registry.authenticate(self.token)
        self.assertEqual(grant.owner_id, "usr_owner")
        self.assertTrue(grant.allows("submit", project_key="jardal-openceo"))

    def test_the_token_is_stored_hashed(self) -> None:
        record = self.registry.record_for(self.token)
        self.assertIsNotNone(record)
        self.assertNotIn("token", record)
        self.assertNotIn(self.token, self.registry._by_digest)  # noqa: SLF001

    def test_unknown_empty_and_revoked_credentials_are_rejected(self) -> None:
        for bad in ("inventado", "", None):
            with self.assertRaises(ConsumerAuthError):
                self.registry.authenticate(bad)  # type: ignore[arg-type]
        self.registry.revoke(self.token, reason="rotada")
        with self.assertRaises(ConsumerAuthError):
            self.registry.authenticate(self.token)

    def test_revoking_a_consumer_covers_all_its_tokens(self) -> None:
        second = self.registry.register(
            consumer_id="jobhunter", owner_id="usr_owner", projects=["jardal-openceo"]
        )
        self.assertEqual(self.registry.revoke_consumer("jobhunter"), 2)
        for token in (self.token, second):
            with self.assertRaises(ConsumerAuthError):
                self.registry.authenticate(token)

    def test_an_expired_credential_is_rejected(self) -> None:
        clock = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
        registry = ConsumerRegistry(clock=lambda: clock[0], ttl_seconds=10)
        token = registry.register(consumer_id="c", owner_id="o", projects=["p"])
        clock[0] = clock[0] + timedelta(seconds=60)
        with self.assertRaises(ConsumerAuthError):
            registry.authenticate(token)
        self.assertEqual(registry.purge_expired(), 1)

    def test_grants_are_explicit(self) -> None:
        grant = self.registry.authenticate(self.token)
        grant.require("submit", project_key="jardal-openceo")
        with self.assertRaises(ConsumerForbidden):
            grant.require("get", project_key="otro-proyecto")

    def test_an_unknown_operation_cannot_be_granted(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(
                consumer_id="c", owner_id="o", projects=["p"], operations=("borrar_todo",)
            )

    def test_a_consumer_without_owner_or_projects_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register(consumer_id="c", owner_id="", projects=["p"])
        with self.assertRaises(ValueError):
            self.registry.register(consumer_id="c", owner_id="o", projects=[])


# ------------------------------------------------------------------------ API


class ApiFacadeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.service = _service(self.repo)
        self.app = _app(self.service)

    async def test_without_a_session_the_request_is_rejected(self) -> None:
        async with _client(self.app) as client:
            response = await client.post("/api/research/jobs", json=_body())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.repo.jobs, {})

    async def test_without_the_run_permission_it_is_forbidden(self) -> None:
        async with _client(self.app) as client:
            response = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_reader")
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.repo.jobs, {})

    async def test_a_valid_submit_returns_202_with_the_identifier(self) -> None:
        async with _client(self.app) as client:
            response = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_owner")
            )
        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertTrue(payload["created"])
        self.assertIn("research_id", payload)
        self.assertEqual(response.headers["location"], f"/api/research/jobs/{payload['research_id']}")
        stored = await self.repo.get_research_job(payload["research_id"])
        self.assertEqual(stored["status"], ResearchJobStatus.QUEUED.value)

    async def test_the_owner_comes_from_the_principal_not_the_payload(self) -> None:
        async with _client(self.app) as client:
            response = await client.post(
                "/api/research/jobs",
                json=_body(owner_id="usr_otro_falsificado"),
                cookies=_cookie("usr_owner"),
            )
        stored = await self.repo.get_research_job(response.json()["research_id"])
        self.assertEqual(stored["owner_id"], "usr_owner")

    async def test_a_forged_project_is_refused(self) -> None:
        async with _client(self.app) as client:
            response = await client.post(
                "/api/research/jobs",
                json=_body(project_key="otro-proyecto"),
                cookies=_cookie("usr_owner"),
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.repo.jobs, {})

    async def test_a_retry_with_the_same_key_returns_the_same_job(self) -> None:
        headers = {"Idempotency-Key": "brief:1"}
        async with _client(self.app) as client:
            first = await client.post(
                "/api/research/jobs", json=_body(), headers=headers, cookies=_cookie("usr_owner")
            )
            second = await client.post(
                "/api/research/jobs", json=_body(), headers=headers, cookies=_cookie("usr_owner")
            )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["research_id"], second.json()["research_id"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(len(self.repo.jobs), 1)

    async def test_the_same_key_with_a_different_body_is_a_conflict(self) -> None:
        headers = {"Idempotency-Key": "brief:2"}
        async with _client(self.app) as client:
            await client.post(
                "/api/research/jobs", json=_body(), headers=headers, cookies=_cookie("usr_owner")
            )
            conflict = await client.post(
                "/api/research/jobs",
                json=_body(question="Otra pregunta"),
                headers=headers,
                cookies=_cookie("usr_owner"),
            )
        self.assertEqual(conflict.status_code, 409)

    async def test_a_validation_error_is_422(self) -> None:
        async with _client(self.app) as client:
            response = await client.post(
                "/api/research/jobs",
                json={"project_key": "jardal-openceo", "purpose": "brief"},
                cookies=_cookie("usr_owner"),
            )
        self.assertEqual(response.status_code, 422)

    async def test_get_returns_our_job(self) -> None:
        async with _client(self.app) as client:
            created = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_owner")
            )
            response = await client.get(
                f"/api/research/jobs/{created.json()['research_id']}", cookies=_cookie("usr_owner")
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["research_id"], created.json()["research_id"])
        # La fachada publica una lista blanca, sin campos internos de lease.
        self.assertNotIn("lease_owner", response.json())

    async def test_another_owner_job_is_not_found_not_forbidden(self) -> None:
        async with _client(self.app) as client:
            created = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_owner")
            )
            response = await client.get(
                f"/api/research/jobs/{created.json()['research_id']}", cookies=_cookie("usr_other")
            )
        self.assertEqual(response.status_code, 404)

    async def test_an_unknown_job_is_404(self) -> None:
        async with _client(self.app) as client:
            response = await client.get(
                "/api/research/jobs/res_inexistente", cookies=_cookie("usr_owner")
            )
        self.assertEqual(response.status_code, 404)

    async def test_get_requires_the_read_permission(self) -> None:
        app = FastAPI()
        app.state.auth_db = _FakeAuthDB(
            [
                {
                    "user_id": "usr_writer",
                    "username": "writer",
                    "email": "writer@example.org",
                    "is_active": True,
                    "all_permissions": ["run_research"],
                    "allowed_projects": ["jardal-openceo"],
                }
            ]
        )
        app.include_router(
            build_research_router(
                get_service=lambda request: self.service,
                project_cfg_for=lambda request, project_key: PROJECT_CFG,
            )
        )
        async with _client(app) as client:
            created = await client.post(
                "/api/research/jobs",
                json=_body(),
                cookies={COOKIE: create_access_token("usr_writer", "writer")},
            )
            response = await client.get(
                f"/api/research/jobs/{created.json()['research_id']}",
                cookies={COOKIE: create_access_token("usr_writer", "writer")},
            )
        self.assertEqual(response.status_code, 403)

    async def test_cancel_moves_the_job_to_cancelled(self) -> None:
        async with _client(self.app) as client:
            created = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_owner")
            )
            response = await client.post(
                f"/api/research/jobs/{created.json()['research_id']}/cancel",
                cookies=_cookie("usr_owner"),
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], ResearchJobStatus.CANCELLED.value)

    async def test_cancelling_someone_elses_job_is_404(self) -> None:
        async with _client(self.app) as client:
            created = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_owner")
            )
            response = await client.post(
                f"/api/research/jobs/{created.json()['research_id']}/cancel",
                cookies=_cookie("usr_other"),
            )
        self.assertEqual(response.status_code, 404)

    async def test_errors_do_not_leak_paths_or_secrets(self) -> None:
        async with _client(self.app) as client:
            response = await client.post(
                "/api/research/jobs",
                json=_body(project_key="otro-proyecto"),
                cookies=_cookie("usr_owner"),
            )
        text = json.dumps(response.json()).lower()
        for forbidden in ("/home/", "jardal-knowledge", "api_key", "bearer ", "secret"):
            self.assertNotIn(forbidden, text)

    async def test_a_disabled_service_is_503_with_its_reason(self) -> None:
        app = _app(_service(self.repo, enabled=False))
        async with _client(app) as client:
            response = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_owner")
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("deshabilitado", response.json()["detail"])

    async def test_submit_does_not_wait_for_the_dispatcher(self) -> None:
        started: list[bool] = []

        async def slow_dispatch(research_id: str) -> None:
            await __import__("asyncio").sleep(1.0)
            started.append(True)

        app = _app(self.service, on_admitted=slow_dispatch)
        begin = time.monotonic()
        async with _client(app) as client:
            response = await client.post(
                "/api/research/jobs", json=_body(), cookies=_cookie("usr_owner")
            )
        elapsed = time.monotonic() - begin
        self.assertEqual(response.status_code, 202)
        self.assertLess(elapsed, 0.5, "la admision no puede esperar al despachador")


# -------------------------------------------------------------------- MCP


class McpFacadeTests(unittest.IsolatedAsyncioTestCase):
    """Protocolo MCP real sobre transporte HTTP montado en ASGI."""

    async def asyncSetUp(self) -> None:
        self.repo = _FakeRepo()
        self.service = _service(self.repo)
        self.registry = ConsumerRegistry(ttl_seconds=600)
        self.token = self.registry.register(
            consumer_id="jobhunter", owner_id="usr_owner", projects=["jardal-openceo"]
        )
        self.server = build_research_mcp_facade(
            consumers=self.registry,
            get_service=lambda: self.service,
            project_cfg_for=lambda project_key: PROJECT_CFG,
        )
        mcp_app = self.server.http_app(path="/")
        self.mcp_app = mcp_app
        parent = FastAPI(lifespan=mcp_app.lifespan)
        parent.mount("/mcp", mcp_app)
        self.parent = parent

    def _transport(self, token: str | None = None) -> StreamableHttpTransport:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers=headers,
            httpx_client_factory=lambda **kw: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.parent), **kw
            ),
        )

    async def _with_client(self, token: str | None):
        return Client(self._transport(token))

    async def test_the_catalog_is_exactly_the_three_facade_tools(self) -> None:
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(self.token) as client:
                tools = await client.list_tools()
        names = {tool.name for tool in tools}
        self.assertEqual(names, set(FACADE_TOOLS))
        for forbidden in ("openceo_get_ticket", "notion_create_ticket", "cloudflare_dns_check_or_create"):
            self.assertNotIn(forbidden, names)

    async def test_owner_identity_is_not_a_tool_argument(self) -> None:
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(self.token) as client:
                tools = await client.list_tools()
        for tool in tools:
            properties = set(((tool.inputSchema or {}).get("properties") or {}).keys())
            self.assertNotIn("owner_id", properties, tool.name)
            self.assertNotIn("session_id", properties, tool.name)

    async def test_a_transport_without_a_credential_does_not_authorize(self) -> None:
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(None) as client:
                result = await client.call_tool(
                    "openceo_research_submit",
                    {"project_key": "jardal-openceo", "purpose": "brief", "question": "q"},
                )
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["error"]["reason"], "unauthenticated")
        self.assertEqual(self.repo.jobs, {})

    async def test_a_revoked_credential_is_rejected(self) -> None:
        self.registry.revoke(self.token, reason="rotada")
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(self.token) as client:
                result = await client.call_tool(
                    "openceo_research_submit",
                    {"project_key": "jardal-openceo", "purpose": "brief", "question": "q"},
                )
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["error"]["reason"], "unauthenticated")

    async def test_the_operator_and_the_consumer_are_different_identities(self) -> None:
        # Un token de solo lectura no puede admitir trabajo.
        read_only = self.registry.register(
            consumer_id="dashboard",
            owner_id="usr_owner",
            projects=["jardal-openceo"],
            operations=("get",),
        )
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(read_only) as client:
                result = await client.call_tool(
                    "openceo_research_submit",
                    {"project_key": "jardal-openceo", "purpose": "brief", "question": "q"},
                )
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["error"]["reason"], "forbidden")

    async def test_a_project_outside_the_grant_is_forbidden(self) -> None:
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(self.token) as client:
                result = await client.call_tool(
                    "openceo_research_submit",
                    {"project_key": "otro-proyecto", "purpose": "brief", "question": "q"},
                )
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["error"]["reason"], "forbidden")
        self.assertEqual(self.repo.jobs, {})

    async def test_submit_then_get_and_cancel(self) -> None:
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(self.token) as client:
                submitted = await client.call_tool(
                    "openceo_research_submit",
                    {"project_key": "jardal-openceo", "purpose": "brief", "question": "q"},
                )
                research_id = submitted.data["research_id"]
                fetched = await client.call_tool(
                    "openceo_research_get", {"research_id": research_id}
                )
                cancelled = await client.call_tool(
                    "openceo_research_cancel", {"research_id": research_id}
                )
        self.assertTrue(submitted.data["ok"])
        self.assertTrue(fetched.data["ok"])
        self.assertEqual(fetched.data["job"]["research_id"], research_id)
        self.assertEqual(cancelled.data["job"]["status"], ResearchJobStatus.CANCELLED.value)

    async def test_a_job_of_another_owner_is_not_found(self) -> None:
        other = self.registry.register(
            consumer_id="trading", owner_id="usr_other", projects=["otro-proyecto"]
        )
        other_repo = _FakeRepo()
        other_service = _service(other_repo)
        from research.contracts import ResearchJobRequest
        from research.policies import Principal, RESEARCH_READ, RESEARCH_RUN

        document = await other_service.submit(
            ResearchJobRequest(
                project_key="otro-proyecto", purpose="brief", question="q", mode="quick"
            ),
            principal=Principal(
                user_id="usr_other",
                permissions=frozenset({RESEARCH_RUN, RESEARCH_READ}),
                allowed_projects=frozenset({"otro-proyecto"}),
            ),
            project_cfg={
                "research": {
                    "enabled": True,
                    "allowed_modes": ["quick"],
                    "limits": {"quick": {"search_requests": 1, "pages": 1, "steps": 2, "seconds": 30}},
                }
            },
        )
        server = build_research_mcp_facade(
            consumers=self.registry,
            get_service=lambda: other_service,
            project_cfg_for=lambda project_key: PROJECT_CFG,
        )
        mcp_app = server.http_app(path="/")
        parent = FastAPI(lifespan=mcp_app.lifespan)
        parent.mount("/mcp", mcp_app)
        transport = StreamableHttpTransport(
            url="http://testserver/mcp/",
            headers={"Authorization": f"Bearer {self.token}"},
            httpx_client_factory=lambda **kw: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=parent), **kw
            ),
        )
        async with mcp_app.router.lifespan_context(parent):
            async with Client(transport) as client:
                result = await client.call_tool(
                    "openceo_research_get", {"research_id": document["research_id"]}
                )
        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["error"]["reason"], "not_found")

    async def test_research_never_creates_tickets(self) -> None:
        async with self.mcp_app.router.lifespan_context(self.parent):
            async with await self._with_client(self.token) as client:
                await client.call_tool(
                    "openceo_research_submit",
                    {"project_key": "jardal-openceo", "purpose": "brief", "question": "q"},
                )
        # El repositorio de research no tiene coleccion de tickets y nada la toca.
        self.assertFalse(hasattr(self.repo, "tasks"))


class WiringTests(unittest.IsolatedAsyncioTestCase):
    """El montaje real del servicio y su cierre (TASK-2026-00066)."""

    def _settings(self, tmp: str, **overrides: Any) -> Settings:
        payload = {
            "RESEARCH_ENABLED": True,
            "RESEARCH_EVIDENCE_DIR": tmp,
            "RESEARCH_OPENCODE_CLI_PATH": str(Path(tmp) / "no-existe"),
            "RESEARCH_BRAVE_API_KEY": "",
        }
        payload.update(overrides)
        return Settings(_env_file=None, **payload)

    async def test_the_service_is_built_even_without_keys_or_cli(self) -> None:
        import tempfile

        from research.wiring import build_research_service

        with tempfile.TemporaryDirectory() as tmp:
            repo = _FakeRepo()
            repo.research_budget_accounts = object()  # type: ignore[attr-defined]
            repo.research_usage = object()  # type: ignore[attr-defined]
            service, resources = build_research_service(self._settings(tmp), repo)
            try:
                available, reason = service.available()
                self.assertTrue(available)
                # El ledger se monta con las colecciones del repositorio.
                self.assertIsNotNone(service._deps.ledger)  # noqa: SLF001
                # Sin clave de Brave no hay descubrimiento; sin CLI no hay runtime.
                self.assertIsNone(service._deps.discover)  # noqa: SLF001
                self.assertIsNone(service._deps.runtime)  # noqa: SLF001
                # El almacen de evidencias si existe: es local y no necesita claves.
                self.assertIsNotNone(service._deps.evidence)  # noqa: SLF001
            finally:
                await resources.aclose()
            self.assertIsNotNone(reason)

    async def test_closing_resources_is_idempotent(self) -> None:
        import tempfile

        from research.wiring import build_research_service

        with tempfile.TemporaryDirectory() as tmp:
            service, resources = build_research_service(self._settings(tmp), _FakeRepo())
            await resources.aclose()
            await resources.aclose()  # no debe lanzar


class MainWiringTests(unittest.TestCase):
    """`main.py` expone la fachada HTTP y deja la montura MCP apagada por defecto."""

    def test_the_research_router_is_registered_in_the_real_app(self) -> None:
        import importlib.util

        if importlib.util.find_spec("libtmux") is None:
            self.skipTest("libtmux no instalado: main.py no se puede importar en este entorno")
        import main

        paths = {
            route.path
            for route in main.app.routes
            if getattr(route, "path", "").startswith("/api/research")
        }
        self.assertEqual(
            paths,
            {
                "/api/research/jobs",
                "/api/research/jobs/{research_id}",
                "/api/research/jobs/{research_id}/cancel",
            },
        )

    def test_the_mcp_facade_is_off_by_default(self) -> None:
        import importlib.util

        if importlib.util.find_spec("libtmux") is None:
            self.skipTest("libtmux no instalado: main.py no se puede importar en este entorno")
        import main

        # Montar un endpoint nuevo es una decision explicita de despliegue: por
        # defecto no se expone, y su control son las credenciales de consumidor.
        self.assertFalse(Settings(_env_file=None).RESEARCH_MCP_ENABLED)
        self.assertIsNone(main._research_mcp_app)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
