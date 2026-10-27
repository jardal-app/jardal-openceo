"""Ticket-token identity for the internal MCP mount (TASK-2026-00196).

Every MCP caller authenticates with an opaque token that works as a badge:
which agent, on which ticket. The server filters ``tools/list`` to the
grant's box — taken from the agent's catalog card — and rejects
out-of-box calls. Calls with no badge get the default
project-management box (``ANONYMOUS_BOX``); a present but invalid badge
still fails closed with 401 on ``/internal/mcp``.

Token kinds: ``owner`` (every tool, every ticket), ``agent`` (its card
box, scoped to its own ticket and project), ``readonly`` (the ``planner``
and ``ceo`` principals: reads only, the engine creates after validating),
``service`` (orchestrator-issued for a retiring flow: explicit tool list,
project-scoped).

Plaintext is returned once at issuance and stored as SHA-256. Agent
badges expire when their ticket closes (plus a TTL backstop).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

try:
    from fastmcp.exceptions import ToolError
    from fastmcp.server.dependencies import get_http_request
    from fastmcp.server.middleware import Middleware
except ImportError:  # pragma: no cover - FastMCP is installed with the core
    ToolError = None  # type: ignore[assignment,misc]
    Middleware = object  # type: ignore[assignment,misc]

    def get_http_request() -> Any:  # type: ignore[misc]
        raise RuntimeError("FastMCP is not installed")

from catalog import TICKET_PATTERN, AgentCard

logger = logging.getLogger(__name__)

__all__ = [
    "ANONYMOUS_BOX",
    "LOCKABLE_MCP_TOOLS",
    "READONLY_BOX",
    "READONLY_PRINCIPALS",
    "McpAuthError",
    "McpDenied",
    "MCPIdentityMiddleware",
    "MemoryAuditSink",
    "MemoryTokenStore",
    "MongoAuditSink",
    "MongoTokenStore",
    "TicketGrant",
    "TokenIssuer",
    "TokenKind",
    "bearer_from_headers",
    "box_for_agent",
    "provision_owner_token",
]

READONLY_BOX = frozenset({"openceo_search_tickets", "openceo_get_ticket"})
READONLY_PRINCIPALS = ("planner", "ceo")

#: Calls with no token get this box (Owner order: keep working everywhere
#: without a badge; the agent-specific tools need one). A present but
#: unknown/invalid token still fails closed. The owner-token-sees-all flow
#: is a pending ticket, outside the agents epic.
ANONYMOUS_BOX = frozenset({
    "manage_dns_record",
    "openceo_create_ticket",
    "openceo_create_tickets_batch",
    "openceo_search_tickets",
    "openceo_get_ticket",
    "openceo_create_objective",
    "openceo_create_mission",
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
    "openceo_ensure_business_project",
})

AGENT_TOKEN_TTL = timedelta(days=7)
OWNER_TOKEN_TTL = timedelta(days=90)
READONLY_TOKEN_TTL = timedelta(days=1)
SERVICE_TOKEN_TTL = timedelta(hours=1)

_TOKEN_PREFIX = "mcp_"
_TASK_PATTERN = re.compile(TICKET_PATTERN)
_AUDIT_REASON_MAX_CHARS = 300


class TokenKind(str, Enum):
    OWNER = "owner"
    AGENT = "agent"
    READONLY = "readonly"
    SERVICE = "service"


class McpAuthError(RuntimeError):
    """Unauthenticated caller. Typed reason, never the token."""

    reason = "unauthenticated"
    status_code = 401

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class McpDenied(McpAuthError):
    """Authenticated caller outside its box."""

    reason = "forbidden"
    status_code = 403


@dataclass(frozen=True)
class TicketGrant:
    """What one token may do. Everything else derives from here."""

    kind: TokenKind
    agent: str
    ticket: str
    project: str
    tools: frozenset[str] = field(default_factory=frozenset)
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    def check(self, tool: str, args: Mapping[str, Any] | None) -> None:
        if tool not in self.tools:
            raise McpDenied(f"Tool {tool} is outside this token's box")
        # PM batches are owner-only (TASK-2026-00209 narrowed the
        # 00199 SERVICE exception away with the retiring PM subprocess).
        if (tool == "openceo_create_pm_tickets_batch"
                and self.kind is not TokenKind.OWNER):
            raise McpDenied("Only the Owner token creates PM batches")
        params = dict(args or {})
        if self.kind is TokenKind.OWNER:
            return
        if self.kind is TokenKind.AGENT:
            self._check_agent(tool, params)
            return
        if self.kind is TokenKind.READONLY:
            if tool not in READONLY_BOX:
                raise McpDenied(f"Read-only token cannot call {tool}")
            return
        if self.kind is TokenKind.SERVICE:
            if params.get("project") != self.project:
                raise McpDenied("Service token is bound to its project")
            # Service badges carry no ticket, so they can never own the
            # origin a lock parks: lockables stay agent/owner-only
            # (TASK-2026-00208). No live service badge holds them.
            if tool in LOCKABLE_MCP_TOOLS and (
                    tool != "manage_dns_record"
                    or params.get("action") == "delete"):
                raise McpDenied("Service tokens cannot call lockable tools")
            return
        raise McpDenied(f"Unknown token kind {self.kind}")  # fail closed

    def _check_agent(self, tool: str, params: dict[str, Any]) -> None:
        if tool in ("openceo_add_ticket_comment", "openceo_attach_ticket_file"):
            if params.get("ticket") != self.ticket:
                raise McpDenied("Agents only write on their own ticket")
            return
        if tool == "openceo_get_ticket":
            if params.get("identifier") != self.ticket:
                raise McpDenied("Agents only read their own ticket")
            return
        if tool == "openceo_search_tickets":
            if params.get("project") != self.project:
                raise McpDenied("Agents only search their own project")
            return
        if tool == "openceo_ensure_business_project":
            # Creation tools carry no ticket/project params to bind, so the
            # box on the token is the whole rule.
            return
        if tool in ("openceo_register_expense", "openceo_register_income",
                    "openceo_scope_summary",
                    "openceo_contribution_10_status"):
            # Dated finance reads/writes (TASK-2026-00221): idempotent by
            # caller ref with their own booking rules, ticket-agnostic by
            # design, so the box is the whole rule. Elfec stays ticket
            # bound under LOCKABLE_MCP_TOOLS.
            return
        if tool in ("openceo_research_submit", "openceo_research_get"):
            # Ticket research bridge (TASK-2026-00222): params must name
            # this grant; provider credentials stay server-held (no lock).
            if params.get("ticket") != self.ticket:
                raise McpDenied("Research tools need your own ticket")
            if params.get("project") != self.project:
                raise McpDenied("Research tools need your own project")
            return
        if tool in LOCKABLE_MCP_TOOLS:
            # The dangerous action locks through the Owner (TASK-2026-00208)
            # and parks the caller's ticket: the params must name exactly
            # this grant, never another ticket. DNS creation keeps the box
            # rule alone (no lock, idempotent).
            if tool == "manage_dns_record" and params.get("action") != "delete":
                return
            if params.get("ticket") != self.ticket:
                raise McpDenied("Lockable tools need your own ticket")
            if params.get("project") != self.project:
                raise McpDenied("Lockable tools need your own project")
            return
        raise McpDenied(f"Tool {tool} has no agent scoping rule")


def bearer_from_headers(headers: Mapping[str, str]) -> str | None:
    for key, value in headers.items():
        if key.lower() != "authorization":
            continue
        scheme, _, token = str(value).partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = token.strip()
        if not token or any(ch.isspace() for ch in token):
            return None
        return token
    return None


def box_for_agent(card: AgentCard) -> frozenset[str]:
    if not card.enabled:
        raise McpAuthError(f"Agent {card.name} is not enabled")
    return frozenset(card.boundary_tools)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _StoredToken:
    digest: str
    grant: TicketGrant
    revoked: bool = False
    revoked_reason: str = ""


#: Tools whose dangerous action carries an Owner lock inside
#: (TASK-2026-00208). Agent callers bind their ticket/project params to
#: the grant; anonymous callers may never name an origin ticket.
LOCKABLE_MCP_TOOLS = frozenset({
    "manage_dns_record",
    "openceo_send_email",
    "openceo_elfec_house_payment",
})


def _grant_to_doc(grant: TicketGrant) -> dict[str, Any]:
    return {
        "kind": grant.kind.value,
        "agent": grant.agent,
        "ticket": grant.ticket,
        "project": grant.project,
        "tools": sorted(grant.tools),
        "issued_at": grant.issued_at,
        "expires_at": grant.expires_at,
    }


def _grant_from_doc(doc: Mapping[str, Any]) -> TicketGrant:
    issued = doc.get("issued_at") or datetime.now(timezone.utc)
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    expires = doc.get("expires_at")
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return TicketGrant(
        kind=TokenKind(str(doc.get("kind") or "")),
        agent=str(doc.get("agent") or ""),
        ticket=str(doc.get("ticket") or ""),
        project=str(doc.get("project") or ""),
        tools=frozenset(str(t) for t in (doc.get("tools") or [])),
        issued_at=issued,
        expires_at=expires,
    )


class MemoryTokenStore:
    """In-memory grants: tests and the provisioning CLI's dry surface."""

    def __init__(self) -> None:
        self._by_digest: dict[str, _StoredToken] = {}

    async def save(self, token: str, grant: TicketGrant) -> None:
        digest = _digest(token)
        self._by_digest[digest] = _StoredToken(digest=digest, grant=grant)

    async def load(self, token: str) -> _StoredToken | None:
        return self._by_digest.get(_digest(token or ""))

    async def revoke_digest(self, token: str, reason: str) -> bool:
        stored = self._by_digest.get(_digest(token or ""))
        if stored is None:
            return False
        stored.revoked = True
        stored.revoked_reason = reason
        return True

    async def revoke_ticket(self, ticket: str, reason: str) -> int:
        revoked = 0
        for stored in self._by_digest.values():
            if stored.grant.ticket != ticket or stored.revoked:
                continue
            stored.revoked = True
            stored.revoked_reason = reason
            revoked += 1
        return revoked

    async def revoke_kind(self, kind: TokenKind, reason: str) -> int:
        revoked = 0
        for stored in self._by_digest.values():
            if stored.grant.kind is not kind or stored.revoked:
                continue
            stored.revoked = True
            stored.revoked_reason = reason
            revoked += 1
        return revoked

    def count(self) -> int:
        return len(self._by_digest)

    def record_for(self, token: str) -> dict[str, Any] | None:
        stored = self._by_digest.get(_digest(token or ""))
        if stored is None:
            return None
        return {
            "kind": stored.grant.kind.value,
            "agent": stored.grant.agent,
            "ticket": stored.grant.ticket,
            "revoked": stored.revoked,
        }


class MongoTokenStore:
    """Durable grants: hashes survive restarts, revocation is immediate."""

    def __init__(
        self,
        db: Any | None = None,
        collection: str = "mcp_token_grants",
        *,
        db_provider: Callable[[], Any] | None = None,
    ) -> None:
        # `db_provider` defers resolution: main.py builds the issuer at
        # import time, before the repository connects. (A motor Database
        # is misleadingly callable, so no duck-typing here.)
        if db is None and db_provider is None:
            raise ValueError("MongoTokenStore needs db or db_provider")
        self._db = db
        self._provider = db_provider
        self._collection_name = collection
        self._indexes_ready = False

    def _collection(self) -> Any:
        db = self._provider() if self._provider is not None else self._db
        return db[self._collection_name]

    async def _ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        await self._collection().create_index("digest", unique=True)
        await self._collection().create_index("grant.ticket")
        await self._collection().create_index(
            "grant.expires_at", expireAfterSeconds=0)
        self._indexes_ready = True

    async def save(self, token: str, grant: TicketGrant) -> None:
        await self._ensure_indexes()
        await self._collection().insert_one({
            "digest": _digest(token),
            "grant": _grant_to_doc(grant),
            "revoked": False,
            "revoked_reason": "",
        })

    async def load(self, token: str) -> _StoredToken | None:
        await self._ensure_indexes()
        doc = await self._collection().find_one(
            {"digest": _digest(token or "")})
        if doc is None:
            return None
        try:
            grant = _grant_from_doc(doc.get("grant") or {})
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Dropping corrupt MCP grant record", exc_info=True)
            return None
        return _StoredToken(
            digest=str(doc.get("digest") or ""),
            grant=grant,
            revoked=bool(doc.get("revoked", False)),
            revoked_reason=str(doc.get("revoked_reason") or ""),
        )

    async def revoke_digest(self, token: str, reason: str) -> bool:
        await self._ensure_indexes()
        result = await self._collection().update_one(
            {"digest": _digest(token or ""), "revoked": False},
            {"$set": {"revoked": True, "revoked_reason": reason}})
        return result.modified_count > 0

    async def revoke_ticket(self, ticket: str, reason: str) -> int:
        await self._ensure_indexes()
        result = await self._collection().update_many(
            {"grant.ticket": ticket, "revoked": False},
            {"$set": {"revoked": True, "revoked_reason": reason}})
        return int(result.modified_count)

    async def revoke_kind(self, kind: TokenKind, reason: str) -> int:
        await self._ensure_indexes()
        result = await self._collection().update_many(
            {"grant.kind": kind.value, "revoked": False},
            {"$set": {"revoked": True, "revoked_reason": reason}})
        return int(result.modified_count)


class TokenIssuer:
    """Issues and authenticates ticket tokens against one store."""

    def __init__(
        self,
        store: MemoryTokenStore | MongoTokenStore,
        cards: Mapping[str, AgentCard],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._cards = dict(cards)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def issue_owner(
        self, ttl: timedelta | None = None,
    ) -> str:
        card = self._cards.get("owner")
        if card is None:
            raise McpAuthError("No owner card in the catalog")
        return await self._issue(
            TokenKind.OWNER, "owner", "", "",
            box_for_agent(card), ttl or OWNER_TOKEN_TTL)

    async def issue_agent(
        self,
        agent: str,
        ticket: str,
        project: str,
        ttl: timedelta | None = None,
    ) -> str:
        card = self._cards.get(agent)
        if card is None:
            raise McpAuthError(f"Unknown agent {agent}")
        if not _TASK_PATTERN.match(ticket or ""):
            raise McpAuthError("Agent tokens bind to one TASK-2026-NNNNN ticket")
        if not (project or "").strip():
            raise McpAuthError("Agent tokens bind to one project")
        return await self._issue(
            TokenKind.AGENT, agent, ticket, project,
            box_for_agent(card), ttl or AGENT_TOKEN_TTL)

    async def issue_readonly(
        self, principal: str, ttl: timedelta | None = None,
    ) -> str:
        if principal not in READONLY_PRINCIPALS:
            raise ValueError(f"Read-only principals: {READONLY_PRINCIPALS}")
        return await self._issue(
            TokenKind.READONLY, principal, "", "",
            READONLY_BOX, ttl or READONLY_TOKEN_TTL)

    async def issue_service(
        self,
        name: str,
        project: str,
        tools: tuple[str, ...] | list[str] | frozenset[str],
        ttl: timedelta | None = None,
    ) -> str:
        box = frozenset(tools or [])
        if not (name or "").strip():
            raise ValueError("Service tokens need a name")
        if not (project or "").strip():
            raise ValueError("Service tokens bind to one project")
        if not box:
            raise ValueError("Service tokens need an explicit tool list")
        return await self._issue(
            TokenKind.SERVICE, name, "", project,
            box, ttl or SERVICE_TOKEN_TTL)

    async def _issue(
        self,
        kind: TokenKind,
        agent: str,
        ticket: str,
        project: str,
        tools: frozenset[str],
        ttl: timedelta,
    ) -> str:
        now = self._clock()
        token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        await self._store.save(token, TicketGrant(
            kind=kind, agent=agent, ticket=ticket, project=project,
            tools=tools, issued_at=now, expires_at=now + ttl))
        return token

    async def authenticate(self, token: str | None) -> TicketGrant:
        if not token or not isinstance(token, str):
            raise McpAuthError("Missing MCP credential")
        stored = await self._store.load(token)
        if stored is None:
            raise McpAuthError("Unknown MCP credential")
        if not hmac.compare_digest(_digest(token), stored.digest):
            raise McpAuthError("Invalid MCP credential")
        if stored.revoked:
            raise McpAuthError(
                f"Revoked MCP credential: {stored.revoked_reason or 'no reason'}")
        expires = stored.grant.expires_at
        if expires is not None and expires <= self._clock():
            raise McpAuthError("Expired MCP credential")
        return stored.grant

    async def revoke(self, token: str, reason: str = "revoked") -> bool:
        return await self._store.revoke_digest(token, reason)

    async def revoke_ticket(
        self, ticket: str, reason: str = "ticket closed",
    ) -> int:
        if not (ticket or "").strip():
            return 0
        return await self._store.revoke_ticket(ticket, reason)

    async def revoke_kind(
        self, kind: TokenKind, reason: str = "revoked by kind",
    ) -> int:
        return await self._store.revoke_kind(kind, reason)


class MemoryAuditSink:
    """Call log for tests: every tool call lands here."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


class MongoAuditSink:
    """Durable call log: agent, ticket, tool, outcome. Cost/audit base."""

    def __init__(
        self,
        db: Any | None = None,
        collection: str = "mcp_calls",
        *,
        db_provider: Callable[[], Any] | None = None,
    ) -> None:
        if db is None and db_provider is None:
            raise ValueError("MongoAuditSink needs db or db_provider")
        self._db = db
        self._provider = db_provider
        self._collection_name = collection

    async def record(self, event: dict[str, Any]) -> None:
        db = self._provider() if self._provider is not None else self._db
        await db[self._collection_name].insert_one({
            "ts": datetime.now(timezone.utc),
            **event,
        })


def _unauthenticated() -> Any:
    if ToolError is None:  # pragma: no cover - FastMCP is installed
        raise McpAuthError("MCP identity required")
    return ToolError("MCP identity required: missing or invalid ticket token")


class MCPIdentityMiddleware(Middleware):  # type: ignore[misc]
    """Enforces the badge: filters tools/list, blocks out-of-box calls."""

    def __init__(
        self,
        issuer: TokenIssuer,
        audit: MemoryAuditSink | MongoAuditSink,
    ) -> None:
        super().__init__()
        self._issuer = issuer
        self._audit = audit

    async def _grant(self) -> TicketGrant | None:
        try:
            request = get_http_request()
            headers = dict(getattr(request, "headers", {}) or {})
        except Exception as exc:
            raise McpAuthError(f"No request to authenticate ({exc})")
        if bearer_from_headers(headers) is None:
            return None
        return await self._issuer.authenticate(bearer_from_headers(headers))

    async def _record(self, grant: TicketGrant | None, tool: str,
                      ok: bool, reason: str = "") -> None:
        try:
            await self._audit.record({
                "kind": grant.kind.value if grant else "none",
                "agent": grant.agent if grant else "",
                "ticket": grant.ticket if grant else "",
                "project": grant.project if grant else "",
                "tool": tool,
                "ok": ok,
                "reason": str(reason or "")[:_AUDIT_REASON_MAX_CHARS],
            })
        except Exception:
            logger.warning("MCP audit record failed", exc_info=True)

    async def on_list_tools(self, context: Any, call_next: Any) -> Any:
        try:
            grant = await self._grant()
        except McpAuthError as exc:
            await self._record(None, "tools/list", False, exc.detail)
            raise _unauthenticated()
        tools = await call_next(context)
        await self._record(grant, "tools/list", True)
        box = grant.tools if grant is not None else ANONYMOUS_BOX
        return [tool for tool in tools if tool.name in box]

    async def _hide_surface(self, surface: str) -> Any:
        # Lists answer empty: clients probe every surface at startup and
        # treat an error as fatal, while an empty list leaks nothing.
        try:
            grant = await self._grant()
        except McpAuthError as exc:
            await self._record(None, surface, False, exc.detail)
            raise _unauthenticated()
        await self._record(grant, surface, True)
        return []

    async def _deny_surface(self, surface: str) -> Any:
        try:
            grant = await self._grant()
        except McpAuthError as exc:
            await self._record(None, surface, False, exc.detail)
            raise _unauthenticated()
        await self._record(grant, surface, False, "surface not exposed")
        if ToolError is None:  # pragma: no cover - FastMCP is installed
            raise McpDenied(f"{surface} is not exposed")
        raise ToolError(f"Denied: {surface} is not exposed")

    async def on_list_resources(self, context: Any, call_next: Any) -> Any:
        return await self._hide_surface("resources/list")

    async def on_list_resource_templates(
        self, context: Any, call_next: Any,
    ) -> Any:
        return await self._hide_surface("resources/templates/list")

    async def on_read_resource(self, context: Any, call_next: Any) -> Any:
        return await self._deny_surface("resources/read")

    async def on_list_prompts(self, context: Any, call_next: Any) -> Any:
        return await self._hide_surface("prompts/list")

    async def on_get_prompt(self, context: Any, call_next: Any) -> Any:
        return await self._deny_surface("prompts/get")

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        try:
            grant = await self._grant()
        except McpAuthError as exc:
            await self._record(None, "tools/call", False, exc.detail)
            raise _unauthenticated()
        name = str(getattr(context.message, "name", "") or "")
        args = getattr(context.message, "arguments", None) or {}
        try:
            if grant is None:
                if name not in ANONYMOUS_BOX:
                    raise McpAuthError(
                        f"{name} needs an Owner or agent badge")
                if name in LOCKABLE_MCP_TOOLS and args.get("ticket"):
                    raise McpAuthError(
                        "anonymous calls cannot name an origin ticket")
            else:
                grant.check(name, args)
        except McpAuthError as exc:
            await self._record(grant, name, False, exc.detail)
            if ToolError is None:  # pragma: no cover - FastMCP is installed
                raise
            raise ToolError(f"Denied: {exc.detail}")
        try:
            result = await call_next(context)
        except Exception as exc:
            await self._record(grant, name, False, f"tool error ({exc})")
            raise
        await self._record(grant, name, True)
        return result


async def provision_owner_token(
    store: MemoryTokenStore | MongoTokenStore,
    ttl: timedelta | None = None,
) -> str:
    """Issues the Owner token. Plaintext is returned once, never stored."""
    from catalog import load_catalog  # deferred: CLI/tests entry only

    issuer = TokenIssuer(store, load_catalog().agents)
    return await issuer.issue_owner(ttl or OWNER_TOKEN_TTL)


def _run_cli(argv: list[str]) -> int:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        description="MCP ticket-token provisioning (TASK-2026-00196)")
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser(
        "provision-owner", help="Issue the Owner token (prints once)")
    prov.add_argument("--ttl-days", type=int, default=90)
    rev = sub.add_parser("revoke-ticket", help="Revoke one ticket's badges")
    rev.add_argument("ticket")
    sub.add_parser("revoke-owner", help="Revoke every Owner badge")
    args = parser.parse_args(argv)

    from db.mongo import MongoRepository
    from settings import Settings

    async def _main() -> str | None:
        repo = MongoRepository(Settings())
        await repo.connect()
        try:
            return await _run_command(args, repo)
        finally:
            await repo.close()

    async def _run_command(args: Any, repo: Any) -> str | None:
        store = MongoTokenStore(repo.db)
        if args.command == "provision-owner":
            token = await provision_owner_token(
                store, timedelta(days=args.ttl_days))
            print(token)
            print("Owner token printed once: store it in the Owner shell "
                  "environment, never in a file robots read.", flush=True)
            return None
        if args.command == "revoke-owner":
            revoked = await store.revoke_kind(TokenKind.OWNER, "revoked by CLI")
            return f"revoked {revoked} owner badge(s)"
        revoked = await store.revoke_ticket(args.ticket, "revoked by CLI")
        return f"revoked {revoked} badge(s) for {args.ticket}"

    result = asyncio.run(_main())
    if result:
        print(result)
    return 0


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(_run_cli(_sys.argv[1:]))
