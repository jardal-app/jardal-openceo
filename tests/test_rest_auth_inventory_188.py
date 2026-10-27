"""REST write-route auth inventory (TASK-2026-00188).

Every POST/PUT/PATCH/DELETE APIRoute must carry recognizable authentication,
or be an explicit, justified exception. The epic contract forbids anonymous
REST access; this test is the automatic inventory from the ticket scope.

Boundary: FastAPI APIRoutes only. WebSocket routes and mounted sub-apps (the
MCP mounts, authenticated by the MCP badge middleware from TASK-2026-00196)
use no Depends gate the classifier could judge, so they stay out — but the
import-time mount set itself is pinned, and any new static mount fails the
suite for review (lifespan-added mounts stay out of reach by construction).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parents[1] / "openceo-core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from fastapi.routing import APIRoute
from starlette.routing import Mount

import main
from auth import middleware as auth_middleware

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Public by design, each with its reason. Adding an entry here is a deliberate
# code change reviewed like any other auth exception.
ALLOWLIST: dict[str, str] = {
    "POST /api/auth/login": "login flow issues the session; has nothing to present yet",
    "POST /api/auth/logout": "login flow; only clears cookies",
    "POST /api/auth/refresh": "login flow; presents the refresh cookie, validated inside",
}

# Mounted sub-apps pin: a new mount needs an explicit auth review.
KNOWN_MOUNTS = frozenset({"/internal/mcp"})

# Host-level surface: every method on these prefixes, plus the pinned voice
# route, must be admin-only. Prefix matching (not a fixed list) so a future
# dev/tmux route without require_admin fails the suite (review round 00004 H5).
ADMIN_ONLY_PREFIXES = ("/api/dev/", "/api/tmux/")
ADMIN_ONLY_PATHS = frozenset({"/api/voice/peer-programming"})

ADMIN_ONLY_ROUTES = frozenset(
    {
        "POST /api/dev/sessions",
        "GET /api/dev/sessions",
        "GET /api/dev/sessions/{session_id}",
        "POST /api/dev/sessions/{session_id}/message",
        "POST /api/dev/sessions/{session_id}/control",
        "POST /api/voice/peer-programming",
        "GET /api/tmux/sessions",
        "GET /api/tmux/sessions/{name}/pane",
        "POST /api/tmux/sessions",
        "POST /api/tmux/sessions/{name}/keys",
        "DELETE /api/tmux/sessions/{name}",
    }
)


def _is_host_route(path: str) -> bool:
    return path in ADMIN_ONLY_PATHS or path.startswith(ADMIN_ONLY_PREFIXES)


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


def _route_auth_kind(route: APIRoute) -> str | None:
    for call in _dependency_calls(route):
        if call is auth_middleware.get_current_user:
            return "session"
        kind = getattr(call, "_openceo_auth_kind", None)
        if kind in ("admin", "permission"):
            return kind
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
            methods = sorted(WRITE_METHODS.intersection(route.methods or set()))
            uncovered = [m for m in methods if f"{m} {route.path}" not in ALLOWLIST]
            if uncovered:
                unguarded.append(
                    f"{','.join(uncovered)} {route.path} ({route.endpoint.__name__})"
                )
        self.assertEqual(
            unguarded,
            [],
            f"write routes without authentication: {unguarded}",
        )

    def test_allowlist_is_minimal(self) -> None:
        self.assertEqual(
            set(ALLOWLIST),
            {
                "POST /api/auth/login",
                "POST /api/auth/logout",
                "POST /api/auth/refresh",
            },
            "allowlist changed: every new anonymous write route needs an explicit decision",
        )

    def test_allowlist_entries_still_anonymous(self) -> None:
        by_key = {}
        for route in _iter_write_routes():
            for method in sorted(WRITE_METHODS.intersection(route.methods or set())):
                by_key[f"{method} {route.path}"] = _route_auth_kind(route)
        for key in sorted(ALLOWLIST):
            self.assertIn(key, by_key, f"allowlisted route disappeared: {key}")
            self.assertIsNone(
                by_key[key],
                f"{key} gained auth: drop it from the allowlist",
            )

    def test_mount_set_is_pinned(self) -> None:
        mounts = {
            route.path for route in main.app.routes if isinstance(route, Mount)
        }
        self.assertEqual(
            mounts,
            set(KNOWN_MOUNTS),
            f"mount set changed: a new mounted sub-app needs an explicit auth review ({mounts})",
        )

    def test_host_routes_require_admin(self) -> None:
        by_key = {}
        for route in main.app.routes:
            if not isinstance(route, APIRoute):
                continue
            if not _is_host_route(route.path):
                continue
            for method in sorted(route.methods or set()):
                by_key[f"{method} {route.path}"] = _route_auth_kind(route)
        self.assertTrue(by_key, "no host routes found")
        for key in sorted(ADMIN_ONLY_ROUTES):
            self.assertIn(key, by_key, f"route disappeared: {key}")
        non_admin = sorted(key for key, kind in by_key.items() if kind != "admin")
        self.assertEqual(non_admin, [], f"host routes without admin gate: {non_admin}")

    def test_pm_extract_requires_session(self) -> None:
        for route in _iter_write_routes():
            if route.path == "/api/pm/extract":
                self.assertEqual(_route_auth_kind(route), "session")
                return
        self.fail("POST /api/pm/extract disappeared")

    def test_webhook_exception_still_guarded_by_secret(self) -> None:
        for route in _iter_write_routes():
            if route.path == "/api/integrations/jardal-erp/task":
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
