"""Durable ERP event bridge (TASK-2026-00262).

Inbound: ERP decisions and business changes arrive at
``POST /api/integrations/jardal-erp/events`` with the contract envelope.
Outbound: execution lifecycle and evidence leave through a durable outbox.
No global polling: bounded drains plus directed replay only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo.errors import DuplicateKeyError

from business.errors import (
    BusinessPermissionError,
    BusinessUnavailable,
    BusinessValidationError,
)

INBOUND_TYPES = frozenset({
    "approval.decision",
    "task.status_changed",
    "execution.requested",
    "task.progress_changed",
})

OUTBOUND_TYPES = frozenset({
    "execution.accepted",
    "execution.started",
    "execution.paused",
    "execution.finished",
    "execution.evidence",
    "execution.human_needed",
})


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def validate_inbound_envelope(payload: Any, *, settings: Any) -> dict[str, Any]:
    """Validate an ERP event envelope. Unknown types report ignored, not error."""
    if not isinstance(payload, dict):
        raise BusinessValidationError("Evento ERP inválido.", status_code=400)
    event_type = str(payload.get("type") or "").strip()
    if not event_type:
        raise BusinessValidationError("Evento ERP sin tipo.", status_code=400)
    if event_type not in INBOUND_TYPES:
        return {"ignored": "unknown_type", "type": event_type}
    if payload.get("schema_version") != 1:
        raise BusinessValidationError("Versión de evento no soportada.", status_code=400)
    if str(payload.get("origin") or "").strip() != "jardal-erp":
        raise BusinessPermissionError("Origen de evento no autorizado.", status_code=403)
    workspace = str(payload.get("workspace_id") or "").strip()
    configured = str(getattr(settings, "JARDAL_ERP_WORKSPACE_ID", "") or "").strip()
    if not workspace or (configured and workspace != configured):
        raise BusinessPermissionError("Evento fuera del workspace.", status_code=403)
    event_id = str(payload.get("event_id") or "").strip()
    aggregate_id = str(payload.get("aggregate_id") or "").strip()
    if not event_id or len(event_id) > 128 or not aggregate_id or len(aggregate_id) > 128:
        raise BusinessValidationError("Identificadores de evento inválidos.", status_code=400)
    try:
        version = int(payload.get("aggregate_version", -1))
    except (TypeError, ValueError):
        raise BusinessValidationError("Versión de agregado inválida.", status_code=400) from None
    if version < 0:
        raise BusinessValidationError("Versión de agregado inválida.", status_code=400)
    occurred_at = _parse_instant(payload.get("occurred_at"))
    if occurred_at is None:
        raise BusinessValidationError("Fecha de evento inválida.", status_code=400)
    actor = payload.get("actor")
    if not isinstance(actor, dict) or not str(actor.get("id") or "").strip():
        raise BusinessValidationError("Actor de evento inválido.", status_code=400)
    body = payload.get("payload")
    if not isinstance(body, dict):
        raise BusinessValidationError("Payload de evento inválido.", status_code=400)
    return {
        "event_id": event_id, "type": event_type, "aggregate_id": aggregate_id,
        "aggregate_version": version, "occurred_at": occurred_at,
        "correlation_id": str(payload.get("correlation_id") or ""),
        "causation_id": str(payload.get("causation_id") or ""),
        "actor": {"kind": str(actor.get("kind") or "human"), "id": str(actor.get("id"))},
        "payload": body,
    }


def decide_inbound(entry: dict[str, Any] | None, cursor_version: int | None,
                   event: dict[str, Any]) -> str:
    """Pure inbox routing: process, duplicate, or stale (older never wins).

    A failed row replays: redelivery with the same event id retries the
    effect instead of vanishing as a duplicate.
    """
    if entry is not None and entry.get("status") != "failed":
        return "duplicate"
    if cursor_version is not None and event["aggregate_version"] <= cursor_version:
        return "stale"
    return "process"


def build_outbound_event(*, type: str, aggregate_id: str, aggregate_version: int,
                         payload: dict[str, Any], business_reference: str = "",
                         correlation_id: str = "", causation_id: str = "",
                         workspace_id: str = "", project_key: str = "",
                         actor: dict[str, Any] | None = None) -> dict[str, Any]:
    if type not in OUTBOUND_TYPES:
        raise BusinessValidationError(f"Tipo de evento saliente inválido: {type}.", status_code=400)
    if not aggregate_id or aggregate_version < 0:
        raise BusinessValidationError("Agregado saliente inválido.", status_code=400)
    body = dict(payload)
    if business_reference:
        body.setdefault("business_reference", business_reference)
    return {
        "event_id": uuid.uuid4().hex,
        "schema_version": 1,
        "type": type,
        "origin": "openceo-core",
        "workspace_id": workspace_id,
        "project_key": project_key,
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id or uuid.uuid4().hex,
        "causation_id": causation_id,
        "actor": dict(actor) if actor else {"kind": "agent", "id": "openceo-runtime"},
        "payload": body,
        "state": "pending",
        "attempts": 0,
    }


def classify_delivery_error(error: BaseException) -> str:
    """Retry transient failures; fail closed on validation/permission errors."""
    status = getattr(error, "status_code", None)
    if isinstance(error, BusinessUnavailable) or status in {408, 425, 429}:
        return "retry"
    if isinstance(status, int) and status >= 500:
        return "retry"
    return "fail"


class MemoryEventStore:
    """In-memory inbox/outbox/cursors (tests and directed tools)."""

    def __init__(self) -> None:
        self.inbox: dict[str, dict[str, Any]] = {}
        self.outbox: dict[str, dict[str, Any]] = {}
        self.cursors: dict[str, int] = {}

    async def get_inbox(self, event_id: str) -> dict[str, Any] | None:
        row = self.inbox.get(event_id)
        return dict(row) if row is not None else None

    async def put_inbox(self, event_id: str, row: dict[str, Any]) -> None:
        self.inbox[event_id] = dict(row)

    async def get_cursor(self, aggregate_id: str) -> int | None:
        return self.cursors.get(aggregate_id)

    async def put_cursor(self, aggregate_id: str, version: int) -> None:
        current = self.cursors.get(aggregate_id)
        if current is not None and version <= current:
            return
        self.cursors[aggregate_id] = version

    async def claim_inbox(self, event_id: str, row: dict[str, Any]) -> bool:
        existing = self.inbox.get(event_id)
        if existing is not None and existing.get("status") != "failed":
            return False
        self.inbox[event_id] = dict(row)
        return True

    async def put_outbox(self, entry: dict[str, Any]) -> None:
        self.outbox[entry["event_id"]] = dict(entry)

    async def get_outbox(self, event_id: str) -> dict[str, Any] | None:
        row = self.outbox.get(event_id)
        return dict(row) if row is not None else None

    async def update_outbox(self, event_id: str, patch: dict[str, Any]) -> None:
        if event_id in self.outbox:
            self.outbox[event_id].update(patch)

    async def due_outbox(self, limit: int) -> list[dict[str, Any]]:
        rows = [row for row in self.outbox.values() if row.get("state") in {"pending", "retry"}]
        rows.sort(key=lambda row: (row.get("attempts", 0), str(row.get("occurred_at") or "")))
        return [dict(row) for row in rows[: max(1, limit)]]

    async def list_processing(self, limit: int) -> list[str]:
        rows = [event_id for event_id, row in self.inbox.items()
                if row.get("status") == "processing"]
        return sorted(rows)[: max(1, limit)]


class MongoEventStore:
    """Production inbox/outbox/cursors on dedicated collections."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def get_inbox(self, event_id: str) -> dict[str, Any] | None:
        row = await self._db["erp_event_inbox"].find_one({"_id": event_id})
        return dict(row) if row else None

    async def put_inbox(self, event_id: str, row: dict[str, Any]) -> None:
        doc = dict(row)
        doc["_id"] = event_id
        await self._db["erp_event_inbox"].replace_one({"_id": event_id}, doc, upsert=True)

    async def claim_inbox(self, event_id: str, row: dict[str, Any]) -> bool:
        # Concurrent claims on a failed row may both proceed; effects stay
        # idempotent (decision exact-once, status already-applied, adoption
        # unique index), so the race converges instead of duplicating.
        doc = dict(row)
        doc["_id"] = event_id
        try:
            await self._db["erp_event_inbox"].insert_one(doc)
        except DuplicateKeyError:
            existing = await self.get_inbox(event_id)
            if existing is None or existing.get("status") == "failed":
                await self.put_inbox(event_id, row)
                return True
            return False
        return True

    async def get_cursor(self, aggregate_id: str) -> int | None:
        row = await self._db["erp_event_cursors"].find_one({"_id": aggregate_id})
        return int(row["version"]) if row and isinstance(row.get("version"), int) else None

    async def put_cursor(self, aggregate_id: str, version: int) -> None:
        current = await self.get_cursor(aggregate_id)
        if current is not None and version <= current:
            return
        await self._db["erp_event_cursors"].replace_one(
            {"_id": aggregate_id}, {"_id": aggregate_id, "version": version}, upsert=True)

    async def put_outbox(self, entry: dict[str, Any]) -> None:
        doc = dict(entry)
        doc["_id"] = entry["event_id"]
        await self._db["erp_event_outbox"].replace_one({"_id": entry["event_id"]}, doc, upsert=True)

    async def get_outbox(self, event_id: str) -> dict[str, Any] | None:
        row = await self._db["erp_event_outbox"].find_one({"_id": event_id})
        return dict(row) if row else None

    async def update_outbox(self, event_id: str, patch: dict[str, Any]) -> None:
        await self._db["erp_event_outbox"].update_one({"_id": event_id}, {"$set": dict(patch)})

    async def due_outbox(self, limit: int) -> list[dict[str, Any]]:
        cursor = self._db["erp_event_outbox"].find(
            {"state": {"$in": ["pending", "retry"]}},
            sort=[("attempts", 1), ("occurred_at", 1)], limit=max(1, limit))
        return [dict(row) async for row in cursor]

    async def list_processing(self, limit: int) -> list[str]:
        cursor = self._db["erp_event_inbox"].find({"status": "processing"}, limit=max(1, limit))
        return [str(row["_id"]) async for row in cursor]


async def deliver_outbound(client: Any, entry: dict[str, Any]) -> dict[str, Any]:
    """POST one outbox entry. Retries reuse the same event_id key."""
    body = {k: entry[k] for k in (
        "event_id", "schema_version", "type", "origin", "workspace_id", "project_key",
        "aggregate_id", "aggregate_version", "occurred_at", "correlation_id", "causation_id",
        "actor", "payload",
    ) if k in entry}
    try:
        await client.request("POST", "/api/v1/integrations/openceo/events",
                             body=body, key=entry["event_id"])
    except Exception as exc:
        return {"delivered": False, "outcome": classify_delivery_error(exc), "error": str(exc)[:300]}
    return {"delivered": True, "outcome": "sent"}


async def drain_outbox(store: Any, client: Any, limit: int = 25) -> dict[str, Any]:
    """Deliver due entries, bounded. Never raises: failures stay visible."""
    sent = 0
    retried = 0
    failed = 0
    for entry in await store.due_outbox(limit):
        if entry.get("state") not in {"pending", "retry"}:
            continue
        result = await deliver_outbound(client, entry)
        attempts = int(entry.get("attempts") or 0) + 1
        if result["delivered"]:
            await store.update_outbox(entry["event_id"], {"state": "sent", "attempts": attempts,
                                                           "error": None})
            sent += 1
        elif result["outcome"] == "retry" and attempts < 8:
            await store.update_outbox(entry["event_id"], {"state": "retry", "attempts": attempts,
                                                           "error": result.get("error")})
            retried += 1
        else:
            await store.update_outbox(entry["event_id"], {"state": "failed", "attempts": attempts,
                                                           "error": result.get("error")})
            failed += 1
    return {"sent": sent, "retried": retried, "failed": failed}


async def reset_stuck_inbox(store: Any, limit: int = 50) -> dict[str, Any]:
    """Bounded reset of rows stuck in processing (crash between claim and put).

    Safe under the single-instance rule: only a dead holder leaves such
    rows. Reset rows become failed, hence replayable by redelivery.
    """
    reset = 0
    for event_id in await store.list_processing(limit):
        row = await store.get_inbox(event_id)
        if row is None or row.get("status") != "processing":
            continue
        row["status"] = "failed"
        row["error"] = "stuck_processing_reset"
        await store.put_inbox(event_id, row)
        reset += 1
    return {"reset": reset}


async def replay_outbox_entry(store: Any, client: Any, event_id: str) -> dict[str, Any]:
    """Directed replay of one entry by id. Never a sweep."""
    entry = await store.get_outbox(str(event_id or "").strip())
    if entry is None:
        return {"replayed": False, "reason": "not_found"}
    if entry.get("state") == "sent":
        return {"replayed": False, "reason": "already_sent"}
    if entry.get("state") == "void":
        return {"replayed": False, "reason": "void"}
    await store.update_outbox(entry["event_id"], {"state": "pending"})
    entry["state"] = "pending"
    result = await deliver_outbound(client, entry)
    attempts = int(entry.get("attempts") or 0) + 1
    if result["delivered"]:
        await store.update_outbox(entry["event_id"], {"state": "sent", "attempts": attempts,
                                                       "error": None})
        return {"replayed": True, "outcome": "sent"}
    await store.update_outbox(entry["event_id"], {"state": "retry" if result["outcome"] == "retry" else "failed",
                                                  "attempts": attempts, "error": result.get("error")})
    return {"replayed": True, "outcome": result["outcome"]}
