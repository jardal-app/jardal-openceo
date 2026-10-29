"""Integration gate over protected routes, anti-Goodhart (design v2 §9.4).

Epic TASK-2026-00365, ticket TASK-2026-00389. Relaxing detection, gates
or limits must require an explicit Owner approval, whoever proposes it.

At integration time (Tier S orchestrator, A/B/C and generic-agent close,
automatico.py close) the diff is checked: when it touches a protected
route, the merge requires the Owner approval over the hash of that diff,
through the same ERP approvals mechanism as the admission gate
(ticket TASK-2026-00387). Without it the ticket parks with the exact
human action ``Aprobar el diff <hash>, que modifica <rutas>``.

Coverage: this module only runs inside the three deterministic
integration paths that call it. Manual Owner sessions never call it, so
they stay out by construction: the Owner is the approver.

Activation: the gate is off until the calibration child owns it
(TASK-2026-00391, pending Owner decision §20.5). ``routes_gate_enabled``
reads the global reliability config; anything unreadable stays off.
When on and the approval cannot be verified, the gate blocks
(fail-closed), exactly like the admission gate.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import hashlib
import json
import logging
import subprocess
import sys
from typing import Any, Awaitable, Protocol

from .admission import ApprovalReader, verify_approval

logger = logging.getLogger(__name__)

#: Version of the protected-routes list. Bumping it is a code change to
#: this same file, which is itself a protected route.
LIST_VERSION = 1

#: Initial protected list (design v2 §9.4). ``trailing /`` entries are
#: prefixes, entries with ``*`` are fnmatch globs, the rest are exact
#: repo-relative paths. The list itself stays protected: weakening it
#: needs the same diff approval as any other entry here.
PROTECTED_ROUTES: tuple[str, ...] = (
    "openceo-core/reliability/",
    "openceo-core/reliability/config.py",
    "openceo-core/reliability/protected_routes.py",
    "openceo-core/objectives/agent_result.py",
    "openceo-core/agents/dev/tier_s/models.py",
    "openceo-core/orchestration/review.py",
    "openceo-core/orchestration/provider_recovery.py",
    "openceo-core/company_mandate.py",
    "projects/*/project.yaml",
    "automatico.py",
)

HUMAN_ACTION_TEMPLATE = "Aprobar el diff {digest}, que modifica {routes}"

#: Routes shown in the human action before collapsing the remainder.
HUMAN_ACTION_MAX_ROUTES = 10


class RoutesApprover(Protocol):
    """Minimal ERP surface the integration gate needs; tests inject fakes."""

    async def request_approval(
            self, request: dict[str, Any], *, key: str) -> dict[str, Any]:
        ...

    async def get_approval(self, identifier: str) -> dict[str, Any]:
        ...


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value
    return value


def routes_gate_enabled() -> bool:
    """True only when the global reliability config enables the gate."""
    try:
        from .config import RELIABILITY_DEFAULTS

        block = RELIABILITY_DEFAULTS.get("routes_gate")
        if isinstance(block, dict):
            return bool(block.get("enabled", False))
    except Exception:  # noqa: BLE001 - gate fail-closed to off, logged
        logger.warning("routes gate config unreadable, gate stays off",
                       exc_info=True)
    return False


def _normalize(path: str) -> str:
    clean = str(path or "").strip().replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean.lstrip("/")


def _matches(route: str, path: str) -> bool:
    if route.endswith("/"):
        return path == route.rstrip("/") or path.startswith(route)
    if "*" in route:
        return fnmatch.fnmatchcase(path, route)
    return path == route


def touched_protected(files: Any) -> list[str]:
    """Protected list entries touched by repo-relative paths, sorted."""
    if not isinstance(files, (list, tuple)):
        return []
    touched: set[str] = set()
    for raw in files:
        path = _normalize(raw if isinstance(raw, str) else "")
        if not path:
            continue
        for route in PROTECTED_ROUTES:
            if _matches(route, path):
                touched.add(route)
    return sorted(touched)


def diff_digest(files: Any, diff: bytes | None) -> str:
    """Stable sha256 over the sorted file list plus the raw diff bytes."""
    names = sorted({_normalize(f) for f in (files or []) if isinstance(f, str)})
    payload = hashlib.sha256()
    payload.update(b"\x00".join(n.encode("utf-8") for n in names))
    payload.update(b"\x00\x00")
    payload.update(diff if isinstance(diff, (bytes, bytearray)) else b"")
    return payload.hexdigest()


def approval_resource_for(diff_digest_value: str) -> str:
    """Canonical approval resource referencing the diff hash."""
    return f"reliability:diff:{diff_digest_value}"


def build_approval_request(
        diff_digest_value: str, routes: list[str], *,
        business_ref: str | None = None) -> tuple[dict[str, Any], str]:
    """Approval request plus idempotency key for a protected diff."""
    request: dict[str, Any] = {
        "resource": approval_resource_for(diff_digest_value),
        "routes": list(routes),
        "list_version": LIST_VERSION,
    }
    if business_ref:
        request["business_reference"] = business_ref
    return request, f"reliability:diff:{diff_digest_value[:16]}"


def human_action_for(diff_digest_value: str, routes: list[str]) -> str:
    """Exact Pending Review human action for a blocked protected diff."""
    shown = list(routes)[:HUMAN_ACTION_MAX_ROUTES]
    rest = len(routes) - len(shown)
    text = ", ".join(shown)
    if rest > 0:
        text += f" y {rest} más"
    return HUMAN_ACTION_TEMPLATE.format(
        digest=diff_digest_value, routes=text or "rutas protegidas")


async def request_diff_approval(
        adapter: RoutesApprover | None, diff_digest_value: str,
        routes: list[str], *,
        business_ref: str | None = None) -> dict[str, Any] | None:
    """Ask the ERP for a diff approval. Never raises; None on any failure."""
    if adapter is None:
        return None
    request, key = build_approval_request(
        diff_digest_value, routes, business_ref=business_ref)
    try:
        created = await _maybe_await(
            adapter.request_approval(request, key=key))
    except Exception:  # noqa: BLE001 - logged, returns None
        logger.warning("diff approval request failed (fail-closed)",
                       exc_info=True)
        return None
    return created if isinstance(created, dict) else None


async def check_integration(
        files: Any, diff: bytes | None, *,
        approval_id: str | None = None,
        approvals: ApprovalReader | None = None) -> dict[str, Any]:
    """Integration verdict for one diff. Never raises, fail-closed.

    Diffs outside the protected list are allowed without any ERP read.
    Protected diffs need a live approval over the current diff hash,
    approved by a human, fresh, and referencing this exact digest:
    an approval for another hash authorizes nothing.
    """
    routes = touched_protected(files)
    if not routes:
        return {"allowed": True, "reason": "no_protected_routes",
                "routes": []}
    digest = diff_digest(files, diff)
    human_action = human_action_for(digest, routes)
    base = {"digest": digest, "routes": routes,
            "human_action": human_action, "list_version": LIST_VERSION}
    clean_id = str(approval_id or "").strip()
    if not clean_id:
        return {"allowed": False, "reason": "no_approval",
                "approval_id": None, **base}
    if approvals is None:
        return {"allowed": False, "reason": "approval_unverifiable",
                "approval_id": clean_id, **base}
    try:
        live = await _maybe_await(approvals.get_approval(clean_id))
    except Exception:  # noqa: BLE001 - logged, blocks fail-closed
        logger.warning("diff approval read failed (fail-closed)",
                       exc_info=True)
        return {"allowed": False, "reason": "approval_unverifiable",
                "approval_id": clean_id, **base}
    ok, reason = verify_approval(live, proposal_hash=digest)
    if not ok:
        return {"allowed": False, "reason": reason,
                "approval_id": clean_id, **base}
    return {"allowed": True, "reason": "approved",
            "approval_id": clean_id, **base}


def collect_diff(repo: str, base: str, head: str) -> tuple[list[str], bytes]:
    """Repo-relative changed files plus raw diff bytes for a base..head range."""
    names = subprocess.run(
        ["git", "diff", "--name-only", "-z", base, head, "--", "."],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False)
    files = [p for p in names.stdout.decode("utf-8", "replace").split("\x00")
             if p] if names.returncode == 0 else []
    raw = subprocess.run(
        ["git", "diff", "--binary", base, head, "--", "."],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False)
    return sorted(set(files)), raw.stdout if raw.returncode == 0 else b""


def _live_reader() -> Any | None:
    try:
        from settings import get_settings

        from business.approvals import JardalERPApproval
    except Exception:  # noqa: BLE001 - no ERP surface, logged below
        logger.warning("ERP approval surface unreadable, no live read",
                       exc_info=True)
        return None
    try:
        client = JardalERPApproval(get_settings())
    except Exception:  # noqa: BLE001 - misconfigured ERP, logged
        logger.warning("ERP approval client unreadable, no live read",
                       exc_info=True)
        return None
    return client if client.is_enabled else None


def check_command(argv: list[str] | None = None) -> int:
    """``check`` CLI: JSON verdict on stdout; 0 allowed, 2 blocked, 1 error."""
    parser = argparse.ArgumentParser(
        description="Protected-routes integration gate check (§9.4).")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--approval-id", default="")
    args = parser.parse_args(argv)
    files, diff = collect_diff(args.repo, args.base, args.head)
    routes = touched_protected(files)
    if not routes:
        print(json.dumps({"allowed": True, "reason": "no_protected_routes",
                          "routes": [], "files": len(files)}))
        return 0
    digest = diff_digest(files, diff)
    verdict: dict[str, Any] = {
        "allowed": False, "reason": "no_approval",
        "digest": digest, "routes": routes,
        "human_action": human_action_for(digest, routes),
        "list_version": LIST_VERSION,
    }
    if args.approval_id.strip():
        reader = _live_reader()
        try:
            verdict = asyncio.run(check_integration(
                files, diff, approval_id=args.approval_id.strip(),
                approvals=reader))
        except Exception as exc:  # noqa: BLE001 - reported as JSON verdict
            print(json.dumps({"allowed": False, "reason": "error",
                              "detail": str(exc)[:200]}))
            return 1
    print(json.dumps(verdict))
    return 0 if verdict.get("allowed") else 2


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    sys.exit(check_command())
