"""Horizon personal domain on private Jardal ERP (TASK-2026-00264).

Mocks and synthetic fixtures only; no live models, no network, no real data.
Personal content here is synthetic and stays out of business channels.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from business.errors import BusinessPermissionError, BusinessValidationError
from business.personal import (
    ERPHorizonStore,
    JardalERPPersonal,
    bind_request_erp_token,
    current_request_erp_token,
    export_personal_subset,
    horizon_store_is_erp,
    import_personal_subset,
    verify_personal_subset,
)


def settings(**changes):
    base = dict(
        HORIZON_STORE="mongo",
        IDENTITY_PROVIDER="local",
        BUSINESS_PROVIDER="jardal_erp",
        BUSINESS_AUTHORITY_FILE="",
        JARDAL_ERP_URL="http://127.0.0.1:31874",
        JARDAL_ERP_SERVICE_TOKEN="synthetic-service-token",
        JARDAL_ERP_WORKSPACE_ID="synthetic-workspace",
        JARDAL_ERP_TIMEOUT_SECONDS=1,
        JARDAL_ERP_CONNECT_TIMEOUT_SECONDS=1,
        MONGODB_DATABASE="synthetic_exec_db",
    )
    base.update(changes)
    return SimpleNamespace(**base)


def erp_settings(tmp_path, **changes):
    manifest = tmp_path / "authority.json"
    manifest.write_text(json.dumps({"schema_version": 1, "provider": "jardal_erp", "state": "ready",
                                    "workspace_id": "synthetic-workspace",
                                    "origin": "http://127.0.0.1:31874",
                                    "execution_database": "synthetic_exec_db"}))
    kwargs = dict(HORIZON_STORE="jardal_erp", IDENTITY_PROVIDER="jardal_erp",
                  BUSINESS_AUTHORITY_FILE=str(manifest))
    kwargs.update(changes)
    return settings(**kwargs)


ERP_OBJECTIVES = [
    {"id": "hob-1", "externalId": "growth_health", "title": "Synthetic health",
     "kind": "growth", "horizon": "stage", "status": "active",
     "milestones": [{"id": "hml-1", "externalId": "VOZ-001", "title": "Walk daily",
                     "status": "active"}]},
]
ERP_CAPTURES = [
    {"id": "hcap-1", "externalId": "dd-1", "text": "synthetic idea",
     "kind": "idea", "channel": "api", "status": "new"},
]


class FakeRepo:
    def __init__(self):
        self.objectives = [{"objective_id": "hob-9", "user_id": "user-1",
                            "source_key": "growth_health", "title": "Synthetic health",
                            "kind": "growth", "horizon": "stage", "status": "active",
                            "milestones": []}]
        self.captures = [{"capture_id": "hcap-9", "user_id": "user-1", "kind": "idea",
                          "text": "synthetic idea", "channel": "api", "dedupe_key": "dd-1",
                          "status": "new"}]
        self.profile = {"user_id": "user-1", "display_name": "Synthetic",
                        "mission": {"statement": "synthetic mission", "values": ["health"]}}
        self.plans_touched = 0

    async def list_horizon_objectives(self, user_id, statuses=None):
        rows = [dict(row) for row in self.objectives if row["user_id"] == user_id]
        if statuses:
            rows = [row for row in rows if row.get("status") in set(statuses)]
        return rows

    async def get_horizon_capture_by_dedupe_key(self, user_id, dedupe_key):
        for row in self.captures:
            if row["user_id"] == user_id and row.get("dedupe_key") == dedupe_key:
                return dict(row)
        return None

    async def list_horizon_captures(self, user_id, limit=20):
        return [dict(row) for row in self.captures if row["user_id"] == user_id][:limit]

    async def get_horizon_profile(self, user_id):
        return dict(self.profile) if self.profile["user_id"] == user_id else None

    async def list_recent_horizon_plans(self, user_id, limit=5):
        self.plans_touched += 1
        return []


def test_gate_requires_manifest_and_federation(tmp_path):
    assert horizon_store_is_erp(settings()) is False
    with pytest.raises(ValueError):
        horizon_store_is_erp(settings(HORIZON_STORE="jardal_erp"))
    with pytest.raises(ValueError):
        horizon_store_is_erp(erp_settings(tmp_path, IDENTITY_PROVIDER="local"))
    assert horizon_store_is_erp(erp_settings(tmp_path)) is True


def test_personal_calls_need_user_token_never_service(tmp_path):
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"objectives": ERP_OBJECTIVES})

        adapter = JardalERPPersonal(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        try:
            bind_request_erp_token(None)
            with pytest.raises(BusinessPermissionError):
                await adapter.list_objectives()
            bind_request_erp_token("synthetic-user-token")
            try:
                rows = await adapter.list_objectives()
            finally:
                bind_request_erp_token(None)
            assert len(rows) == 1 and rows[0]["externalId"] == "growth_health"
            assert requests[0].headers["authorization"] == "Bearer synthetic-user-token"
        finally:
            await adapter.aclose()
        assert current_request_erp_token() is None
    asyncio.run(run())


def test_store_maps_objectives_captures_and_delegates_execution_state(tmp_path):
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            path, method = request.url.path, request.method
            if path == "/api/v1/horizon/objectives" and method == "GET":
                return httpx.Response(200, json={"objectives": ERP_OBJECTIVES})
            if path == "/api/v1/horizon/objectives" and method == "POST":
                return httpx.Response(200, json={"objective": dict(
                    ERP_OBJECTIVES[0], id="hob-2", externalId="growth_wealth")})
            if path.startswith("/api/v1/horizon/objectives/") and method == "PATCH":
                return httpx.Response(200, json={"objective": ERP_OBJECTIVES[0]})
            if path == "/api/v1/horizon/captures" and method == "GET":
                return httpx.Response(200, json={"captures": ERP_CAPTURES})
            if path == "/api/v1/horizon/captures" and method == "POST":
                return httpx.Response(200, json={"capture": ERP_CAPTURES[0]})
            raise AssertionError(f"unexpected {method} {path}")

        repo = FakeRepo()
        adapter = JardalERPPersonal(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        store = ERPHorizonStore(repo, adapter)
        bind_request_erp_token("synthetic-user-token", "user-1")
        try:
            objectives = await store.list_horizon_objectives("user-1")
            assert objectives[0]["objective_id"] == "hob-1"
            assert objectives[0]["source_key"] == "growth_health"
            assert objectives[0]["milestones"][0]["source_key"] == "VOZ-001"
            assert await store.list_horizon_objectives("user-1", statuses=["archived"]) == []
            found = await store.get_horizon_objective_by_source_key("user-1", "growth_health")
            assert found and found["title"] == "Synthetic health"
            assert await store.get_horizon_objective_by_source_key("user-1", "missing") is None
            created = await store.upsert_horizon_objective(
                {"user_id": "user-1", "source_key": "growth_wealth", "title": "Synthetic wealth"})
            assert created["objective_id"] == "hob-2"
            updated = await store.update_horizon_milestone(
                user_id="user-1", objective_id="hob-1", milestone_id="hml-1",
                updates={"status": "done"})
            assert updated and updated["milestone_id"] == "hml-1"
            patched_body = json.loads(requests[-1].content)["milestones"][0]
            assert patched_body["id"] == "hml-1" and patched_body["externalId"] == "VOZ-001"
            assert patched_body["status"] == "done"
            assert await store.update_horizon_milestone(
                user_id="user-1", objective_id="hob-x", milestone_id="hml-1", updates={}) is None
            captures = await store.list_horizon_captures("user-1")
            assert captures[0]["dedupe_key"] == "dd-1" and captures[0]["user_id"] == "user-1"
            assert (await store.get_horizon_capture_by_dedupe_key("user-1", "dd-1"))["capture_id"] == "hcap-1"
            assert await store.get_horizon_capture_by_dedupe_key("user-1", "absent") is None
            made = await store.create_horizon_capture(
                {"user_id": "user-1", "text": "synthetic idea", "dedupe_key": "dd-1"})
            assert made["capture_id"] == "hcap-1"
            assert requests
            assert all(r.headers["authorization"] == "Bearer synthetic-user-token" for r in requests)
            assert await store.list_recent_horizon_plans("user-1") == []
            assert repo.plans_touched == 1
            with pytest.raises(BusinessPermissionError):
                await store.list_horizon_objectives("user-2")
        finally:
            bind_request_erp_token(None)
            await adapter.aclose()
    asyncio.run(run())


def test_background_without_session_reads_mongo_and_refuses_writes(tmp_path):
    async def run():
        repo = FakeRepo()
        adapter = JardalERPPersonal(erp_settings(tmp_path),
                                    transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(
                                        AssertionError("no network without session"))))
        store = ERPHorizonStore(repo, adapter)
        bind_request_erp_token(None)
        try:
            objectives = await store.list_horizon_objectives("user-1")
            assert objectives[0]["objective_id"] == "hob-9"
            assert (await store.get_horizon_capture_by_dedupe_key("user-1", "dd-1"))["capture_id"] == "hcap-9"
            with pytest.raises(BusinessPermissionError):
                await store.upsert_horizon_objective({"user_id": "user-1", "title": "x"})
            with pytest.raises(BusinessPermissionError):
                await store.create_horizon_capture({"user_id": "user-1", "text": "x"})
            with pytest.raises(BusinessPermissionError):
                await store.update_horizon_milestone(
                    user_id="user-1", objective_id="hob-9", milestone_id="hml-9", updates={})
        finally:
            await adapter.aclose()
    asyncio.run(run())


def test_migration_export_import_verify_is_idempotent():
    async def run():
        repo = FakeRepo()
        snapshot = await export_personal_subset(repo, "user-1")
        assert len(snapshot["objectives"]) == 1 and len(snapshot["captures"]) == 1
        assert "mission" not in snapshot
        with pytest.raises(BusinessValidationError):
            await export_personal_subset(repo, "")

        requests = []

        def handle(request):
            requests.append(request)
            body = json.loads(request.content)
            assert body["objectives"][0]["externalId"] == "growth_health"
            assert body["captures"][0]["externalId"] == "dd-1"
            assert body["captures"][0]["channel"] == "api"
            assert body["captures"][0]["status"] == "new"
            assert "mission" not in body
            return httpx.Response(200, json={"imported": True, "duplicate": len(requests) > 1})

        adapter = JardalERPPersonal(settings(), transport=httpx.MockTransport(handle))
        bind_request_erp_token("synthetic-user-token")
        try:
            first = await import_personal_subset(adapter, snapshot, key="migrate:user-1")
            second = await import_personal_subset(adapter, snapshot, key="migrate:user-1")
            assert first["imported"] is True and second["duplicate"] is True
            assert requests[0].url.path == "/api/v1/horizon/import"
            assert requests[0].headers["Idempotency-Key"] == "migrate:user-1"
            with pytest.raises(BusinessValidationError):
                await import_personal_subset(adapter, {"user_id": ""}, key="migrate:x")
        finally:
            bind_request_erp_token(None)
            await adapter.aclose()
    asyncio.run(run())


def test_verify_compares_external_ids(tmp_path):
    async def run():
        def handle(request):
            return httpx.Response(200, json={"objectives": ERP_OBJECTIVES, "captures": ERP_CAPTURES})

        adapter = JardalERPPersonal(erp_settings(tmp_path), transport=httpx.MockTransport(handle))
        bind_request_erp_token("synthetic-user-token")
        try:
            full = {"user_id": "user-1",
                    "objectives": [{"source_key": "growth_health"}],
                    "captures": [{"dedupe_key": "dd-1"}]}
            assert (await verify_personal_subset(adapter, full))["ok"] is True
            partial = {"user_id": "user-1",
                       "objectives": [{"source_key": "growth_health"}, {"source_key": "missing"}],
                       "captures": []}
            result = await verify_personal_subset(adapter, partial)
            assert result["ok"] is False and result["missing_objectives"] == ["missing"]
        finally:
            bind_request_erp_token(None)
            await adapter.aclose()
    asyncio.run(run())


def test_no_leak_personal_never_publishes_and_promotion_is_spec_only():
    from horizon import promotion as promotion_module
    from horizon.privacy import PERSONAL_COLLECTIONS, is_personal_scope

    assert set(PERSONAL_COLLECTIONS) >= {"horizon_objectives", "horizon_captures"}
    personal = {"task_id": "exec-p", "params": {"horizon_scope": "personal", "horizon_owner": "user-1"}}
    assert is_personal_scope(personal) is True
    params = promotion_module.promotion_params("user-1", "hact-1")
    assert "horizon_scope" not in params and params["promoted_by"] == "horizon"
    assert promotion_module.promotion_source_id("hact-1") == "horizon-promotion:hact-1"

    async def run():
        from task_service import TaskService

        service = TaskService.__new__(TaskService)
        service._business = SimpleNamespace(provider="jardal_erp", is_enabled=True, client=None)
        assert await service._publish_execution_event("execution.started", personal) is None
    asyncio.run(run())
