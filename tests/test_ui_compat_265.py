"""ERP UI/mobile compatibility (TASK-2026-00265): auth, surface, bundle, voice.

Mocks and synthetic fixtures only; no live models, no network, no real data.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

REPO = Path(__file__).resolve().parents[1]

# Frozen compatibility surface: Core endpoints the ERP UI relies on during
# the transition (baseline = what openceo-react calls today).
COMPAT_ROUTES = [
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/tasks"),
    ("POST", "/api/tasks"),
    ("GET", "/api/tasks/stats"),
    ("POST", "/api/tasks/search"),
    ("GET", "/api/groups"),
    ("POST", "/api/groups"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("GET", "/api/users/names"),
    ("GET", "/api/projects"),
    ("POST", "/api/projects"),
    ("GET", "/api/horizon/plan"),
    ("GET", "/api/horizon/plan/today"),
    ("GET", "/api/horizon/next-action"),
    ("GET", "/api/horizon/delivery"),
    ("GET", "/api/horizon/delegation/summary"),
    ("POST", "/api/horizon/capture"),
    ("POST", "/api/horizon/progress"),
    ("POST", "/api/horizon/delegate"),
    ("POST", "/api/horizon/plan/day"),
    ("POST", "/api/upload"),
]


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
        for stored in self.docs:
            if all(stored.get(k) == v for k, v in filt.items()):
                stored.update(update.get("$set", {}))


class FakeDB:
    def __init__(self):
        self.users = FakeCollection()
        self.identity_map = FakeCollection()


def _request(headers=None, cookies=None, db=None):
    from starlette.requests import Request

    raw = []
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    for key, value in (headers or {}).items():
        raw.append((key.lower().encode(), value.encode()))
    scope = {"type": "http", "headers": raw,
             "app": SimpleNamespace(state=SimpleNamespace(auth_db=db or FakeDB()))}
    return Request(scope)


def test_compat_surface_frozen():
    import main as core_app

    from fastapi.routing import APIRoute

    have = {(sorted(route.methods)[0] if len(route.methods) == 1 else m, route.path)
            for route in core_app.app.routes if isinstance(route, APIRoute)
            for m in (route.methods or set())}
    missing = [f"{method} {path}" for method, path in COMPAT_ROUTES
               if (method, path) not in have]
    assert missing == []


def test_bearer_core_jwt_local_and_heaven_never_authenticates():
    from auth.jwt import create_access_token
    from auth.middleware import get_current_user
    from state import UserDocument

    async def run():
        db = FakeDB()
        user = UserDocument(username="root", email="root@synthetic.test").model_dump()
        db.users.insert_one(user)
        token = create_access_token(user["user_id"], "root")
        seen = await get_current_user(_request(headers={"authorization": f"Bearer {token}"}, db=db))
        assert seen.username == "root"
        cookie_req = _request(headers={"authorization": "Bearer heaven"},
                              cookies={"openceo_session": token}, db=db)
        assert (await get_current_user(cookie_req)).username == "root"
        with pytest.raises(HTTPException) as exc:
            await get_current_user(_request(headers={"authorization": "Bearer heaven"}, db=db))
        assert exc.value.status_code == 401
        with pytest.raises(HTTPException) as exc:
            await get_current_user(_request(headers={"authorization": "Bearer garbage"}, db=db))
        assert exc.value.status_code == 401
    asyncio.run(run())


def test_bearer_erp_session_needs_link_and_federation():
    from auth.middleware import get_current_user
    from state import UserDocument

    async def run():
        db = FakeDB()
        user = UserDocument(username="staff", email="staff@synthetic.test",
                            auth_provider="jardal_erp", external_id="erp-1").model_dump()
        db.users.insert_one(user)
        mapped = {"external_id": "erp-1", "email": "staff@synthetic.test",
                  "all_permissions": ["read_tickets"], "allowed_projects": [],
                  "is_client": False, "auth_version": 1}
        fake_adapter = AsyncMock()
        fake_adapter.introspect.return_value = mapped
        fake_adapter.aclose.return_value = None
        req = _request(headers={"authorization": "Bearer erp-token-x"}, db=db)
        with patch("auth.erp_identity.identity_provider_is_erp", return_value=True), \
             patch("auth.erp_identity.JardalERPIdentity", return_value=fake_adapter):
            with pytest.raises(HTTPException) as exc:
                await get_current_user(req)
            assert exc.value.status_code == 401
            db.identity_map.insert_one({"erp_user_id": "erp-1", "user_id": user["user_id"]})
            seen = await get_current_user(req)
            assert seen.username == "staff" and seen.all_permissions == ["read_tickets"]
            assert fake_adapter.introspect.await_count == 2
    asyncio.run(run())


def test_login_exchange_establishes_mapping_without_cookies():
    from unittest.mock import Mock

    from auth.router import login

    async def run():
        db = FakeDB()
        mapped = {"external_id": "erp-9", "email": "new@synthetic.test", "username": "new",
                  "display_name": "New", "all_permissions": ["read_tickets"],
                  "allowed_projects": [], "is_client": False, "auth_version": 1}
        fake_adapter = AsyncMock()
        fake_adapter.introspect.return_value = mapped
        fake_adapter.aclose.return_value = None
        request = Mock()
        request.json = AsyncMock(return_value={"erp_token": "erp-token-new"})
        request.app.state.auth_db = db
        response = Mock()
        cfg = SimpleNamespace(IDENTITY_PROVIDER="jardal_erp", JWT_EXPIRE_MINUTES=1440,
                              JWT_REFRESH_EXPIRE_DAYS=7)
        with patch("auth.erp_identity.identity_provider_is_erp", return_value=True), \
             patch("auth.erp_identity.JardalERPIdentity", return_value=fake_adapter), \
             patch("settings.get_settings", return_value=cfg):
            result = await login(request, response)
        assert result["user"]["auth_provider"] == "jardal_erp"
        assert result["access_token"] and result["refresh_token"]
        from auth.jwt import decode_token

        assert decode_token(result["access_token"])["type"] == "access"
        assert db.identity_map.find_one({"erp_user_id": "erp-9"}) is not None
    asyncio.run(run())


def test_client_bundle_carries_no_secrets():
    vite_names = set()
    for path in (REPO / "openceo-react" / "src").rglob("*.*"):
        if path.suffix not in {".js", ".jsx", ".ts", ".tsx"}:
            continue
        text = path.read_text()
        for match in __import__("re").finditer(r"VITE_[A-Z_0-9]+", text):
            vite_names.add(match.group(0))
    assert vite_names <= {"VITE_API_URL", "VITE_NODE_ENV"}
    token_ext = (REPO / "openceo-voice-android" / "app" / "src" / "main" / "java" /
                 "io" / "livekit" / "android" / "example" / "voiceassistant" / "TokenExt.kt")
    text = token_ext.read_text()
    assert 'hardcodedToken = ""' in text
    assert "SECRET" not in text and "API_KEY" not in text


def _load_token_server(monkeypatch):
    calls = {}

    class FakeGrants:
        def __init__(self, **kwargs):
            calls["grants"] = kwargs

    class FakeToken:
        def __init__(self, key, secret):
            calls["key"] = (key, secret)

        def with_identity(self, value):
            calls["identity"] = value
            return self

        def with_name(self, value):
            calls["name"] = value
            return self

        def with_grants(self, value):
            return self

        def with_metadata(self, value):
            calls["metadata"] = value
            return self

        def to_jwt(self):
            return "synthetic-jwt"

    livekit = ModuleType("livekit")
    livekit.api = SimpleNamespace(AccessToken=FakeToken, VideoGrants=FakeGrants)
    monkeypatch.setitem(sys.modules, "livekit", livekit)
    monkeypatch.syspath_prepend(str(REPO / "openceo-voice"))
    import project_env  # noqa: F401  (real loader, reads local env only)

    sys.modules.pop("token_server", None)
    import token_server

    return token_server, calls


def test_voice_token_contract(monkeypatch):
    async def run():
        server, calls = _load_token_server(monkeypatch)
        monkeypatch.setattr(server, "LIVEKIT_API_KEY", "")
        monkeypatch.setattr(server, "LIVEKIT_API_SECRET", "")
        assert (await server.health())["status"] == "degraded"
        with pytest.raises(Exception):
            await server.create_token(server.TokenRequest(user_id="synthetic-user"))
        monkeypatch.setattr(server, "LIVEKIT_API_KEY", "synthetic-key")
        monkeypatch.setattr(server, "LIVEKIT_API_SECRET", "synthetic-secret")
        assert (await server.health())["status"] == "ok"
        result = await server.create_token(server.TokenRequest(
            user_id="synthetic-user", room="openceo-voice", project="contract-test", model="ceo"))
        assert result == {"token": "synthetic-jwt", "url": server.LIVEKIT_URL}
        assert calls["identity"] == "synthetic-user"
        assert calls["grants"] == {"room_join": True, "room": "openceo-voice"}
        import json as jsonlib

        assert jsonlib.loads(calls["metadata"])["project"] == "contract-test"
    asyncio.run(run())
