"""REST write-route auth inventory (TASK-2026-00188).

Every POST/PUT/PATCH/DELETE route must carry recognizable authentication, or
be an explicit, justified exception. The epic contract forbids anonymous REST
access; this test is the automatic inventory from the ticket scope.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from fastapi.routing import APIRoute

import main
from auth import middleware as auth_middleware

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Public by design, each with its reason. Adding an entry here is a deliberate
# code change reviewed like any other auth exception.
ALLOWLIST: dict[str, str] = {
    "/api/auth/login": "login flow issues the session; has nothing to present yet",
    "/api/auth/logout": "login flow; only clears cookies",
    "/api/auth/refresh": "login flow; presents the refresh cookie, validated inside",
}

ADMIN_ONLY_ROUTES = frozenset(
    {
        "POST /api/dev/sessions",
        "POST /api/dev/sessions/{session_id}/message",
        "POST /api/dev/sessions/{session_id}/control",
        "POST /api/voice/peer-programming",
        "POST /api/tmux/sessions",
        "POST /api/tmux/sessions/{name}/keys",
        "DELETE /api/tmux/sessions/{name}",
    }
)

ADMIN_ONLY_GET_ROUTES = frozenset(
    {
        "GET /api/dev/sessions",
        "GET /api/dev/sessions/{session_id}",
        "GET /api/tmux/sessions",
        "GET /api/tmux/sessions/{name}/pane",
    }
)


def _iter_write_routes() -> list[APIRoute]:
    routes = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        if WRITE_METHODS.intersection(route.methods or set()):
            routes.append(route)
    return sorted(routes, key=lambda r: r.path)


def _dependency_calls(route: APIRoute) -> list:
    calls = []
    for dep in route.dependencies or []:
        calls.append(getattr(dep, "dependency", None))
    dependant = getattr(route, "dependant", None)
    if dependant is not None:
        calls.extend(getattr(dep, "call", None) for dep in (dependant.dependencies or []))
    return [call for call in calls if call is not None]


def _is_admin_checker(call) -> bool:
    return getattr(call, "__qualname__", "") == "require_admin.<locals>.checker"


def _is_perm_checker(call) -> bool:
    return getattr(call, "__qualname__", "") == "require_permissions.<locals>.checker"


def _route_auth_kind(route: APIRoute) -> str | None:
    for call in _dependency_calls(route):
        if call is auth_middleware.get_current_user:
            return "session"
        if _is_admin_checker(call):
            return "admin"
        if _is_perm_checker(call):
            return "permission"
        if call is main.verify_api_key:
            return "api_key"
    dependant = getattr(route, "dependant", None)
    headers = getattr(dependant, "header_params", None) if dependant is not None else None
    for param in headers or []:
        if getattr(param, "alias", "") == "X-OpenCEO-Webhook-Secret":
            return "webhook_secret"
    return None


class RestAuthInventoryTests(unittest.TestCase):
    def test_write_routes_require_auth(self) -> None:
        unguarded = []
        for route in _iter_write_routes():
            kind = _route_auth_kind(route)
            if kind is not None:
                continue
            reason = ALLOWLIST.get(route.path)
            if reason is None:
                methods = ",".join(sorted(WRITE_METHODS.intersection(route.methods or set())))
                unguarded.append(f"{methods} {route.path} ({route.endpoint.__name__})")
        self.assertEqual(
            unguarded,
            [],
            f"write routes without authentication: {unguarded}",
        )

    def test_allowlist_is_minimal(self) -> None:
        self.assertEqual(
            set(ALLOWLIST),
            {"/api/auth/login", "/api/auth/logout", "/api/auth/refresh"},
            "allowlist changed: every new anonymous write route needs an explicit decision",
        )

    def test_tmux_dev_voice_write_routes_require_admin(self) -> None:
        by_key = {}
        for route in _iter_write_routes():
            for method in sorted(WRITE_METHODS.intersection(route.methods or set())):
                by_key[f"{method} {route.path}"] = _route_auth_kind(route)
        for key in sorted(ADMIN_ONLY_ROUTES):
            self.assertIn(key, by_key, f"route disappeared: {key}")
            self.assertEqual(by_key[key], "admin", f"{key} must be admin-only")

    def test_tmux_dev_read_routes_require_admin(self) -> None:
        by_key = {}
        for route in main.app.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted((route.methods or set())):
                by_key[f"{method} {route.path}"] = _route_auth_kind(route)
        for key in sorted(ADMIN_ONLY_GET_ROUTES):
            self.assertIn(key, by_key, f"route disappeared: {key}")
            self.assertEqual(by_key[key], "admin", f"{key} must be admin-only")

    def test_pm_extract_requires_session(self) -> None:
        for route in _iter_write_routes():
            if route.path == "/api/pm/extract":
                self.assertEqual(_route_auth_kind(route), "session")
                return
        self.fail("POST /api/pm/extract disappeared")

    def test_webhook_exception_still_guarded_by_secret(self) -> None:
        for route in _iter_write_routes():
            if route.path == "/api/integrations/erpnext/task":
                self.assertEqual(_route_auth_kind(route), "webhook_secret")
                return
        self.fail("webhook route disappeared")

    def test_v1_exception_still_guarded_by_api_key(self) -> None:
        seen = set()
        for route in _iter_write_routes():
            if route.path.startswith("/v1/"):
                seen.add(route.path)
                self.assertEqual(_route_auth_kind(route), "api_key", route.path)
        self.assertTrue(seen, "no /v1 write routes found")


if __name__ == "__main__":
    unittest.main()
