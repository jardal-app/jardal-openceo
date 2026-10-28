"""Federated ERP identity (TASK-2026-00261): delegation, mapping, revocation.

Mocks and synthetic fixtures only; no live models, no network, no real data.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from auth.erp_identity import (
    ERP_SESSION_COOKIE,
    JardalERPIdentity,
    ensure_local_directory,
    export_identity_map,
    identity_provider_is_erp,
    map_erp_identity,
    upsert_mapped_user,
)
from auth.middleware import get_current_user, require_admin
from business.errors import BusinessPermissionError, BusinessUnavailable, BusinessValidationError


def settings(tmp_path=None, **changes):
    base = dict(
        IDENTITY_PROVIDER="local",
        BUSINESS_PROVIDER="jardal_erp",
        BUSINESS_AUTHORITY_FILE="",
        JARDAL_ERP_URL="http://127.0.0.1:31874",
        JARDAL_ERP_SERVICE_TOKEN="synthetic-service-token",
        JARDAL_ERP_WORKSPACE_ID="synthetic-workspace",
        JARDAL_ERP_TIMEOUT_SECONDS=1,
        JARDAL_ERP_CONNECT_TIMEOUT_SECONDS=1,
        MONGODB_DATABASE="synthetic_exec_db",
        JWT_EXPIRE_MINUTES=1440,
        JWT_REFRESH_EXPIRE_DAYS=7,
    )
    base.update(changes)
    return SimpleNamespace(**base)


def manifest_doc(url="http://127.0.0.1:31874"):
    return {"schema_version": 1, "provider": "jardal_erp", "state": "ready",
            "workspace_id": "synthetic-workspace", "origin": url,
            "execution_database": "synthetic_exec_db"}


def erp_settings(tmp_path, **changes):
    manifest = tmp_path / "authority.json"
    manifest.write_text(json.dumps(manifest_doc()))
    return settings(IDENTITY_PROVIDER="jardal_erp", BUSINESS_AUTHORITY_FILE=str(manifest), **changes)


ERP_USER = {"id": "erp-user-001", "email": "staff@synthetic.test", "isActive": True,
            "username": "staff", "displayName": "Synthetic Staff",
            "permissions": ["task.read", "task.write", "approval.decide"],
            "projects": ["contract-test"]}


class FakeCollection:
    def __init__(self):
        self.docs = []

    def find_one(self, filt, *_, **__):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filt.items()):
                return dict(doc)
        return None

    def insert_one(self, doc):
        self.docs.append(dict(doc))

    def update_one(self, filt, update):
        doc = self.find_one(filt)
        if doc is None:
            return
        stored = next(d for d in self.docs if all(d.get(k) == v for k, v in filt.items()))
        stored.update(update.get("$set", {}))


class FakeDB:
    def __init__(self):
        self.users = FakeCollection()
        self.identity_map = FakeCollection()


def test_login_delegates_anonymously_and_maps_identity(tmp_path):
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            assert request.url.path == "/api/v1/auth/login"
            return httpx.Response(200, json={"token": "synthetic-erp-token", "user": ERP_USER})

        cfg = erp_settings(tmp_path)
        adapter = JardalERPIdentity(cfg, transport=httpx.MockTransport(handle))
        try:
            result = await adapter.login(email="staff@synthetic.test", password="synthetic-secret")
        finally:
            await adapter.aclose()
        assert result["token"] == "synthetic-erp-token"
        assert "authorization" not in requests[0].headers
        assert requests[0].headers["X-Workspace-Id"] == "synthetic-workspace"
        assert requests[0].headers["Idempotency-Key"] == "auth.login:staff@synthetic.test"
        identity = result["identity"]
        assert identity["external_id"] == "erp-user-001"
        assert identity["all_permissions"] == ["approve_tickets", "read_tickets", "reject_tickets",
                                               "review_tickets", "write_tickets"]
        assert identity["allowed_projects"] == ["contract-test"]
        db = FakeDB()
        user_doc = upsert_mapped_user(db, identity)
        assert user_doc["auth_provider"] == "jardal_erp"
        assert user_doc["password_hash"] == ""
        assert user_doc["is_admin"] is False
        link = db.identity_map.find_one({"erp_user_id": "erp-user-001"})
        assert link and link["user_id"] == user_doc["user_id"]
    asyncio.run(run())


def test_login_rejects_bad_credentials_without_side_effects(tmp_path):
    async def run():
        def handle(request):
            return httpx.Response(401, json={"code": "UNAUTHORIZED"})

        cfg = erp_settings(tmp_path)
        adapter = JardalERPIdentity(cfg, transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(BusinessPermissionError):
                await adapter.login(email="staff@synthetic.test", password="wrong")
        finally:
            await adapter.aclose()
    asyncio.run(run())


def test_introspect_uses_user_token_and_denies_revoked(tmp_path):
    async def run():
        seen = {}

        def handle(request):
            seen["auth"] = request.headers.get("authorization")
            if request.headers.get("authorization") == "Bearer revoked-token":
                return httpx.Response(401, json={"code": "UNAUTHORIZED"})
            return httpx.Response(200, json={"user": ERP_USER})

        cfg = erp_settings(tmp_path)
        adapter = JardalERPIdentity(cfg, transport=httpx.MockTransport(handle))
        try:
            mapped = await adapter.introspect("valid-token")
            assert seen["auth"] == "Bearer valid-token"
            assert mapped["email"] == "staff@synthetic.test"
            with pytest.raises(BusinessPermissionError):
                await adapter.introspect("revoked-token")
            with pytest.raises(BusinessPermissionError):
                await adapter.introspect("")
        finally:
            await adapter.aclose()
    asyncio.run(run())


def test_mapping_is_fail_closed_and_never_grants_admin(tmp_path):
    cfg = erp_settings(tmp_path)
    view = {"user": dict(ERP_USER, permissions=["finance.read", "identity.manage", "execution.operate",
                                                "unknown.capability"], isAdmin=True, role="admin")}
    mapped = map_erp_identity(view, settings=cfg)
    assert mapped["all_permissions"] == []
    db = FakeDB()
    user_doc = upsert_mapped_user(db, mapped)
    assert user_doc["is_admin"] is False
    assert user_doc["all_permissions"] == []


def test_client_scope_is_restricted_to_granted_deliverables(tmp_path):
    cfg = erp_settings(tmp_path)
    view = {"user": dict(ERP_USER, permissions=["task.write", "approval.decide", "task.read"],
                         isClient=True, projects=["client-project"])}
    mapped = map_erp_identity(view, settings=cfg)
    assert mapped["all_permissions"] == ["client_review", "read_tickets"]
    assert mapped["allowed_projects"] == ["client-project"]
    assert mapped["is_client"] is True


@pytest.mark.parametrize("view", [
    {"user": dict(ERP_USER, isActive=False)},
    {"user": {k: v for k, v in ERP_USER.items() if k != "id"}},
    {"user": {k: v for k, v in ERP_USER.items() if k != "email"}},
    {"user": "not-a-dict"},
    "not-a-dict",
])
def test_inactive_or_incomplete_identity_is_rejected(tmp_path, view):
    with pytest.raises((BusinessPermissionError, BusinessValidationError)):
        map_erp_identity(view, settings=erp_settings(tmp_path))


def test_workspace_mismatch_is_rejected_absent_is_accepted(tmp_path):
    cfg = erp_settings(tmp_path)
    bad = {"user": dict(ERP_USER, workspaceIds=["other-workspace"])}
    with pytest.raises(BusinessPermissionError):
        map_erp_identity(bad, settings=cfg)
    ok = {"user": dict(ERP_USER, workspaceIds=["synthetic-workspace"])}
    assert map_erp_identity(ok, settings=cfg)["external_id"] == "erp-user-001"
    assert map_erp_identity({"user": ERP_USER}, settings=cfg)["external_id"] == "erp-user-001"


def test_provider_gate_requires_manifest_and_config(tmp_path):
    assert identity_provider_is_erp(settings()) is False
    with pytest.raises(ValueError):
        identity_provider_is_erp(settings(IDENTITY_PROVIDER="jardal_erp"))
    assert identity_provider_is_erp(erp_settings(tmp_path)) is True
    with pytest.raises(ValueError):
        identity_provider_is_erp(erp_settings(tmp_path, JARDAL_ERP_URL=""))
    with pytest.raises(ValueError):
        identity_provider_is_erp(erp_settings(tmp_path, JARDAL_ERP_SERVICE_TOKEN=""))
    with pytest.raises(HTTPException) as exc:
        ensure_local_directory(erp_settings(tmp_path))
    assert exc.value.status_code == 409
    ensure_local_directory(settings())


def test_export_identity_map_writes_bounded_list_and_verifies(tmp_path):
    db = FakeDB()
    rows = [{"erp_user_id": "erp-a", "user_id": "usr_a", "email": "a@synthetic.test", "auth_version": 1},
            {"erp_user_id": "erp-b", "user_id": "usr_b", "email": "b@synthetic.test"}]
    assert export_identity_map(db, rows) == {"written": 2, "total": 2}
    assert export_identity_map(db, rows) == {"written": 0, "total": 2}
    with pytest.raises(BusinessValidationError):
        export_identity_map(db, [{"erp_user_id": "erp-a", "user_id": "usr_other"}])
    with pytest.raises(BusinessValidationError):
        export_identity_map(db, [{"erp_user_id": "erp-p", "user_id": "usr_p", "personal": True}])
    with pytest.raises(BusinessValidationError):
        export_identity_map(db, [{"erp_user_id": "", "user_id": ""}])
    with pytest.raises(BusinessValidationError):
        export_identity_map(db, ["not-a-dict"])


def test_login_timeout_is_unavailable_not_success(tmp_path):
    async def run():
        def handle(request):
            raise httpx.ConnectTimeout("synthetic", request=request)

        cfg = erp_settings(tmp_path)
        adapter = JardalERPIdentity(cfg, transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(BusinessUnavailable):
                await adapter.login(email="staff@synthetic.test", password="synthetic-secret")
        finally:
            await adapter.aclose()
    asyncio.run(run())


def _request_with_cookies(cookies, db):
    from starlette.requests import Request

    header = "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()
    scope = {"type": "http", "headers": [(b"cookie", header)],
             "app": SimpleNamespace(state=SimpleNamespace(auth_db=db))}
    return Request(scope)


def test_require_admin_rejects_federated_admin_but_keeps_local():
    from auth.jwt import create_access_token
    from state import UserDocument

    async def run():
        db = FakeDB()
        fed = UserDocument(username="fed", email="fed@synthetic.test", is_admin=True,
                           auth_provider="jardal_erp", external_id="erp-fed").model_dump()
        local = UserDocument(username="root", email="root@synthetic.test", is_admin=True).model_dump()
        db.users.insert_one(fed)
        db.users.insert_one(local)
        checker = require_admin()
        fed_req = _request_with_cookies({"openceo_session": create_access_token(fed["user_id"], "fed")}, db)
        with pytest.raises(HTTPException) as exc:
            await checker(fed_req)
        assert exc.value.status_code == 403
        local_req = _request_with_cookies({"openceo_session": create_access_token(local["user_id"], "root")}, db)
        assert (await checker(local_req)).username == "root"
    asyncio.run(run())


def test_federated_middleware_revalidates_and_rejects_mismatch():
    async def run():
        from state import UserDocument

        db = FakeDB()
        user = UserDocument(username="staff", email="staff@synthetic.test",
                            auth_provider="jardal_erp", external_id="erp-user-001",
                            all_permissions=["read_tickets"]).model_dump()
        db.users.insert_one(user)
        from auth.jwt import create_access_token

        req = _request_with_cookies({"openceo_session": create_access_token(user["user_id"], "staff"),
                                     ERP_SESSION_COOKIE: "valid-token"}, db)
        mapped = dict(ERP_USER)
        fake_adapter = AsyncMock()
        fake_adapter.introspect.return_value = {
            "external_id": "erp-user-001", "email": "staff@synthetic.test",
            "all_permissions": ["read_tickets", "write_tickets"], "allowed_projects": ["contract-test"],
            "is_client": False, "auth_version": 2}
        fake_adapter.aclose.return_value = None
        with patch("auth.erp_identity.identity_provider_is_erp", return_value=True), \
             patch("auth.erp_identity.JardalERPIdentity", return_value=fake_adapter):
            seen = await get_current_user(req)
            assert seen.all_permissions == ["read_tickets", "write_tickets"]
            assert seen.auth_version == 2
            fake_adapter.introspect.return_value = dict(fake_adapter.introspect.return_value,
                                                        external_id="erp-other")
            with pytest.raises(HTTPException) as exc:
                await get_current_user(req)
            assert exc.value.status_code == 401
    asyncio.run(run())


def test_federated_middleware_denies_local_accounts_and_unavailable_erp():
    async def run():
        from state import UserDocument

        db = FakeDB()
        local = UserDocument(username="root", email="root@synthetic.test").model_dump()
        fed = UserDocument(username="staff", email="staff@synthetic.test",
                           auth_provider="jardal_erp", external_id="erp-user-001").model_dump()
        db.users.insert_one(local)
        db.users.insert_one(fed)
        from auth.jwt import create_access_token

        fake_adapter = AsyncMock()
        fake_adapter.aclose.return_value = None
        with patch("auth.erp_identity.identity_provider_is_erp", return_value=True), \
             patch("auth.erp_identity.JardalERPIdentity", return_value=fake_adapter):
            local_req = _request_with_cookies(
                {"openceo_session": create_access_token(local["user_id"], "root")}, db)
            with pytest.raises(HTTPException) as exc:
                await get_current_user(local_req)
            assert exc.value.status_code == 401
            fed_req = _request_with_cookies(
                {"openceo_session": create_access_token(fed["user_id"], "staff"),
                 ERP_SESSION_COOKIE: "valid-token"}, db)
            fake_adapter.introspect.side_effect = BusinessUnavailable("down")
            with pytest.raises(HTTPException) as exc:
                await get_current_user(fed_req)
            assert exc.value.status_code == 503
            fake_adapter.introspect.side_effect = BusinessPermissionError("revoked", status_code=401)
            with pytest.raises(HTTPException) as exc:
                await get_current_user(fed_req)
            assert exc.value.status_code == 401
    asyncio.run(run())


def test_user_document_defaults_keep_local_compat():
    from state import UserDocument

    user = UserDocument(username="legacy", email="legacy@synthetic.test")
    assert user.auth_provider == "local"
    assert user.external_id is None
    assert user.auth_version == 0


def test_export_rejects_non_numeric_auth_version():
    db = FakeDB()
    with pytest.raises(BusinessValidationError) as exc:
        export_identity_map(db, [{"erp_user_id": "erp-x", "user_id": "usr_x", "auth_version": "abc"}])
    assert exc.value.status_code == 400


def test_upsert_requires_explicit_link_on_email_clash(tmp_path):
    from state import UserDocument

    cfg = erp_settings(tmp_path)
    mapped = map_erp_identity({"user": ERP_USER}, settings=cfg)
    db = FakeDB()
    dormant = UserDocument(username="staff", email="staff@synthetic.test").model_dump()
    db.users.insert_one(dormant)
    with pytest.raises(BusinessValidationError) as exc:
        upsert_mapped_user(db, mapped)
    assert exc.value.status_code == 409
    export_identity_map(db, [{"erp_user_id": "erp-user-001", "user_id": dormant["user_id"],
                              "email": "staff@synthetic.test"}])
    adopted = upsert_mapped_user(db, mapped)
    assert adopted["user_id"] == dormant["user_id"]
    assert adopted["auth_provider"] == "jardal_erp"
    assert adopted["email"] == "staff@synthetic.test"
    assert adopted["display_name"] == "Synthetic Staff"
    assert adopted["is_client"] is False


def test_require_permissions_ignores_federated_admin_flag():
    from auth.jwt import create_access_token
    from auth.middleware import require_permissions
    from state import UserDocument

    async def run():
        db = FakeDB()
        fed = UserDocument(username="fed", email="fed@synthetic.test", is_admin=True,
                           auth_provider="jardal_erp", external_id="erp-fed",
                           all_permissions=["read_tickets"]).model_dump()
        db.users.insert_one(fed)
        checker = require_permissions("write_tickets")
        req = _request_with_cookies({"openceo_session": create_access_token(fed["user_id"], "fed")}, db)
        with pytest.raises(HTTPException) as exc:
            await checker(req)
        assert exc.value.status_code == 403
        reader = require_permissions("read_tickets")
        assert (await reader(req)).username == "fed"
    asyncio.run(run())


def test_federated_refresh_binds_erp_session_to_token_subject(tmp_path):
    from unittest.mock import Mock

    from auth.jwt import create_refresh_token
    from auth.router import refresh
    from state import UserDocument

    async def run():
        db = FakeDB()
        user = UserDocument(username="staff", email="staff@synthetic.test",
                            auth_provider="jardal_erp", external_id="erp-user-001").model_dump()
        db.users.insert_one(user)
        cookies = {"openceo_refresh": create_refresh_token(user["user_id"]),
                   ERP_SESSION_COOKIE: "erp-token-x"}
        fake_adapter = AsyncMock()
        fake_adapter.aclose.return_value = None
        fake_adapter.introspect.return_value = {"external_id": "erp-other"}
        response = Mock()
        with patch("auth.erp_identity.identity_provider_is_erp", return_value=True), \
             patch("auth.erp_identity.JardalERPIdentity", return_value=fake_adapter):
            with pytest.raises(HTTPException) as exc:
                await refresh(_request_with_cookies(cookies, db), response)
            assert exc.value.status_code == 401
            fake_adapter.introspect.return_value = {"external_id": "erp-user-001"}
            assert (await refresh(_request_with_cookies(cookies, db), response)) == {"token": "heaven"}
            assert response.set_cookie.call_count == 2
    asyncio.run(run())


def test_federated_login_issues_both_cookies(tmp_path):
    from unittest.mock import Mock

    from auth.router import _login_via_erp

    async def run():
        cfg = erp_settings(tmp_path)
        db = FakeDB()
        mapped = map_erp_identity({"user": ERP_USER}, settings=cfg)
        fake_adapter = AsyncMock()
        fake_adapter.aclose.return_value = None
        fake_adapter.login.return_value = {"token": "erp-token-new", "identity": mapped}
        request = Mock()
        request.json = AsyncMock(return_value={})
        request.app.state.auth_db = db
        response = Mock()
        with patch("auth.erp_identity.JardalERPIdentity", return_value=fake_adapter):
            result = await _login_via_erp(request, response, cfg, "staff@synthetic.test", "secret")
        assert result["user"]["auth_provider"] == "jardal_erp"
        assert "password_hash" not in result["user"]
        cookie_names = [call.kwargs.get("key", call.args[0] if call.args else None)
                        for call in response.set_cookie.call_args_list]
        assert cookie_names == ["openceo_session", "openceo_refresh", ERP_SESSION_COOKIE]
    asyncio.run(run())
