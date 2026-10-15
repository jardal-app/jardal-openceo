"""Personal Horizon domain against private Jardal ERP (TASK-2026-00264)."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

import httpx

from business.errors import BusinessPermissionError, BusinessValidationError
from business.http import JardalERPClient, erp_service_configured
from business.provider import validate_authority_manifest

_request_erp_token: ContextVar[str | None] = ContextVar("request_erp_token", default=None)
_request_erp_owner: ContextVar[str | None] = ContextVar("request_erp_owner", default=None)


def bind_request_erp_token(token: str | None, owner: str | None = None) -> None:
    """Bind the current request's user ERP token and Core owner. Task-local."""
    _request_erp_token.set(token if token else None)
    _request_erp_owner.set(str(owner) if owner else None)


def current_request_erp_token() -> str | None:
    return _request_erp_token.get()


def current_request_erp_owner() -> str | None:
    return _request_erp_owner.get()


def horizon_store_is_erp(settings: Any) -> bool:
    if str(getattr(settings, "HORIZON_STORE", "mongo")).strip() != "jardal_erp":
        return False
    validate_authority_manifest(settings)
    if str(getattr(settings, "IDENTITY_PROVIDER", "local")).strip() != "jardal_erp":
        raise ValueError("Horizon ERP exige identidad federada (sesiones de usuario).")
    if not erp_service_configured(settings):
        raise ValueError("Horizon ERP sin config de servicio completa.")
    return True


def _require_user_token() -> str:
    token = current_request_erp_token()
    if not token:
        raise BusinessPermissionError("Llamada personal sin sesión de usuario.", status_code=401)
    return token


def _node(result: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise BusinessValidationError("Respuesta personal inválida.", status_code=502)
    node: Any = result
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            break
    if not isinstance(node, dict):
        raise BusinessValidationError("Respuesta personal inválida.", status_code=502)
    return node


def _as_list(result: Any, *keys: str) -> list[dict[str, Any]]:
    node: Any = result
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            break
    if isinstance(node, dict):
        for fallback in ("items", "results", "data"):
            if isinstance(node.get(fallback), list):
                node = node[fallback]
                break
    if not isinstance(node, list):
        raise BusinessValidationError("Listado personal inválido.", status_code=502)
    return [row for row in node if isinstance(row, dict)]


class JardalERPPersonal:
    """Private ERP personal domain. User token only, never the service token."""

    provider = "jardal_erp"

    def __init__(self, settings: Any, client: JardalERPClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self.client = client or JardalERPClient(settings, transport=transport)
        self._owns_client = client is None

    @property
    def is_enabled(self) -> bool:
        return erp_service_configured(self._settings)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self.client.request("GET", path, params=params, bearer=_require_user_token())

    async def _send(self, method: str, path: str, body: dict[str, Any], *, key: str) -> Any:
        return await self.client.request(method, path, body=body, key=key,
                                         bearer=_require_user_token())

    async def list_objectives(self) -> list[dict[str, Any]]:
        raw = await self._get("/api/v1/horizon/objectives")
        if isinstance(raw, dict) and (raw.get("has_more") or raw.get("truncated")):
            raise BusinessValidationError("Listado de objetivos truncado.", status_code=502)
        return _as_list(raw, "objectives")

    async def create_objective(self, objective: dict[str, Any], *, key: str) -> dict[str, Any]:
        if not isinstance(objective, dict) or not str(objective.get("title") or "").strip():
            raise BusinessValidationError("Objetivo sin título.", status_code=400)
        return _node(await self._send("POST", "/api/v1/horizon/objectives", objective, key=key),
                     "objective")

    async def patch_objective(self, identifier: str, patch: dict[str, Any], *, key: str) -> dict[str, Any]:
        if not str(identifier or "").strip() or not isinstance(patch, dict):
            raise BusinessValidationError("Objetivo inválido.", status_code=400)
        return _node(await self._send("PATCH", "/api/v1/horizon/objectives/" + quote(identifier, safe=""),
                                      patch, key=key), "objective")

    async def delete_objective(self, identifier: str, *, key: str) -> None:
        if not str(identifier or "").strip():
            raise BusinessValidationError("Objetivo inválido.", status_code=400)
        await self._send("DELETE", "/api/v1/horizon/objectives/" + quote(identifier, safe=""),
                         {}, key=key)

    async def list_captures(self, limit: int = 20) -> list[dict[str, Any]]:
        return _as_list(await self._get("/api/v1/horizon/captures",
                                        params={"limit": max(1, min(limit, 200))}), "captures")

    async def create_capture(self, capture: dict[str, Any], *, key: str) -> dict[str, Any]:
        if not isinstance(capture, dict) or not str(capture.get("text") or "").strip():
            raise BusinessValidationError("Captura sin texto.", status_code=400)
        return _node(await self._send("POST", "/api/v1/horizon/captures", capture, key=key),
                     "capture")

    async def patch_capture(self, identifier: str, patch: dict[str, Any], *, key: str) -> dict[str, Any]:
        if not str(identifier or "").strip() or not isinstance(patch, dict):
            raise BusinessValidationError("Captura inválida.", status_code=400)
        return _node(await self._send("PATCH", "/api/v1/horizon/captures/" + quote(identifier, safe=""),
                                      patch, key=key), "capture")

    async def import_personal(self, payload: dict[str, Any], *, key: str) -> dict[str, Any]:
        """Idempotent import by external ids. Whole payload, one call."""
        if not isinstance(payload, dict):
            raise BusinessValidationError("Importación inválida.", status_code=400)
        result = await self._send("POST", "/api/v1/horizon/import", payload, key=key)
        return result if isinstance(result, dict) else {"ok": True}


class ERPHorizonStore:
    """Personal store with the ERP product subset; execution state stays Mongo.

    Objectives (+milestones) and captures live in the private ERP domain.
    Profile, actions, events, plans, triggers and every company reader
    delegate to the Mongo repo: one writer per collection and stage.

    Background work without a user session reads Mongo (loud stale warning)
    and refuses ERP-subset writes: no split brain, no silent duplicates.
    """

    def __init__(self, repo: Any, adapter: JardalERPPersonal) -> None:
        self._repo = repo
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)

    @staticmethod
    def _owned(user_id: str) -> bool:
        """True when a user session is bound and owns this user_id."""
        if current_request_erp_token() is None:
            return False
        owner = current_request_erp_owner()
        if owner is not None and owner != str(user_id or ""):
            raise BusinessPermissionError("Sesión ERP de otro usuario.", status_code=403)
        return True

    def _fallback_notice(self, method: str) -> None:
        import logging

        logging.getLogger(__name__).warning(
            "Horizon ERP sin sesión de usuario en %s; lectura Mongo (puede estar desactualizada).",
            method)

    @staticmethod
    def _objective_to_core(row: dict[str, Any], user_id: str) -> dict[str, Any]:
        milestones = []
        for item in row.get("milestones") or []:
            if not isinstance(item, dict):
                continue
            milestones.append({
                "milestone_id": str(item.get("id") or item.get("milestone_id") or ""),
                "source_key": item.get("externalId", item.get("source_key")),
                "title": str(item.get("title") or ""),
                "measure": item.get("measure"),
                "status": str(item.get("status") or "active"),
                "condition": item.get("condition"),
            })
        return {
            "objective_id": str(row.get("id") or row.get("objective_id") or ""),
            "user_id": user_id,
            "source_key": row.get("externalId", row.get("source_key")),
            "title": str(row.get("title") or ""),
            "kind": str(row.get("kind") or "growth"),
            "horizon": str(row.get("horizon") or "stage"),
            "status": str(row.get("status") or "active"),
            "measure": row.get("measure"),
            "milestones": milestones,
            "priority": row.get("priority", 100),
        }

    @staticmethod
    def _objective_to_erp(doc: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"title": doc.get("title")}
        for key in ("kind", "horizon", "status", "measure", "priority"):
            if doc.get(key) is not None:
                body[key] = doc[key]
        if doc.get("source_key"):
            body["externalId"] = doc["source_key"]
        milestones = []
        for item in doc.get("milestones") or []:
            if not isinstance(item, dict):
                continue
            entry = {"title": item.get("title")}
            for key in ("measure", "status", "condition", "horizon"):
                if item.get(key) is not None:
                    entry[key] = item[key]
            if item.get("source_key"):
                entry["externalId"] = item["source_key"]
            milestones.append(entry)
        if milestones:
            body["milestones"] = milestones
        return body

    @staticmethod
    def _capture_to_core(row: dict[str, Any], user_id: str) -> dict[str, Any]:
        return {
            "capture_id": str(row.get("id") or row.get("capture_id") or ""),
            "user_id": user_id,
            "kind": str(row.get("kind") or row.get("type") or "idea"),
            "text": str(row.get("text") or ""),
            "channel": str(row.get("channel") or "api"),
            "origin": row.get("origin"),
            "dedupe_key": str(row.get("externalId") or row.get("dedupe_key") or ""),
            "status": str(row.get("status") or "new"),
            "linked_objective_id": row.get("linked_objective_id"),
            "linked_task_id": row.get("linked_task_id"),
        }

    @staticmethod
    def _milestone_to_erp(item: dict[str, Any]) -> dict[str, Any]:
        entry: dict[str, Any] = {}
        if item.get("milestone_id"):
            entry["id"] = item["milestone_id"]
        if item.get("source_key"):
            entry["externalId"] = item["source_key"]
        for key in ("title", "measure", "status", "condition", "horizon"):
            if item.get(key) is not None:
                entry[key] = item[key]
        return entry

    async def list_horizon_objectives(self, user_id: str, *,
                                      statuses: list[str] | None = None) -> list[dict[str, Any]]:
        if not self._owned(user_id):
            self._fallback_notice("list_horizon_objectives")
            return await self._repo.list_horizon_objectives(user_id, statuses=statuses)
        rows = await self._adapter.list_objectives()
        core = [self._objective_to_core(row, user_id) for row in rows]
        if statuses:
            wanted = set(statuses)
            core = [row for row in core if str(row.get("status") or "") in wanted]
        return core

    async def get_horizon_objective_by_source_key(self, user_id: str, source_key: str) -> dict[str, Any] | None:
        if not source_key:
            return None
        for row in await self.list_horizon_objectives(user_id):
            if row.get("source_key") == source_key:
                return row
        return None

    async def upsert_horizon_objective(self, doc: dict[str, Any]) -> dict[str, Any]:
        user_id = str(doc.get("user_id") or "")
        if not self._owned(user_id):
            raise BusinessPermissionError("Escritura personal sin sesión de usuario.", status_code=401)
        source_key = doc.get("source_key")
        if source_key:
            existing = await self.get_horizon_objective_by_source_key(user_id, str(source_key))
            if existing:
                patched = await self._adapter.patch_objective(
                    existing["objective_id"], self._objective_to_erp(doc),
                    key=f"horizon.objective.upsert:{source_key}")
                return self._objective_to_core(patched, user_id)
            created = await self._adapter.create_objective(
                self._objective_to_erp(doc), key=f"horizon.objective.create:{source_key}")
            return self._objective_to_core(created, user_id)
        for row in await self.list_horizon_objectives(user_id):
            if row.get("title") == doc.get("title"):
                patched = await self._adapter.patch_objective(
                    row["objective_id"], self._objective_to_erp(doc),
                    key=f"horizon.objective.upsert:title:{doc.get('title')}")
                return self._objective_to_core(patched, user_id)
        created = await self._adapter.create_objective(
            self._objective_to_erp(doc), key=f"horizon.objective.create:{doc.get('objective_id') or 'new'}")
        return self._objective_to_core(created, user_id)

    async def update_horizon_milestone(self, *, user_id: str, objective_id: str,
                                       milestone_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        if not self._owned(user_id):
            raise BusinessPermissionError("Escritura personal sin sesión de usuario.", status_code=401)
        objectives = await self.list_horizon_objectives(user_id)
        target = next((row for row in objectives if row.get("objective_id") == objective_id), None)
        if target is None:
            return None
        milestones = []
        found = False
        for item in target.get("milestones") or []:
            if str(item.get("milestone_id") or "") == milestone_id:
                found = True
                merged = dict(item)
                merged.update({k: v for k, v in (updates or {}).items() if k != "milestone_id"})
                milestones.append(self._milestone_to_erp(merged))
            else:
                milestones.append(self._milestone_to_erp(item))
        if not found:
            return None
        patched = await self._adapter.patch_objective(
            objective_id, {"milestones": milestones},
            key=f"horizon.milestone.update:{objective_id}:{milestone_id}")
        for item in self._objective_to_core(patched, user_id).get("milestones") or []:
            if str(item.get("milestone_id") or "") == milestone_id:
                return item
        return None

    async def create_horizon_capture(self, doc: dict[str, Any]) -> dict[str, Any]:
        user_id = str(doc.get("user_id") or "")
        if not self._owned(user_id):
            raise BusinessPermissionError("Escritura personal sin sesión de usuario.", status_code=401)
        body = {"text": doc.get("text"), "kind": doc.get("kind"), "channel": doc.get("channel")}
        for key in ("origin", "status", "linked_objective_id", "linked_task_id"):
            if doc.get(key) is not None:
                body[key] = doc[key]
        if doc.get("dedupe_key"):
            body["externalId"] = doc["dedupe_key"]
        created = await self._adapter.create_capture(
            body, key=f"horizon.capture.create:{doc.get('dedupe_key') or doc.get('capture_id') or 'new'}")
        return self._capture_to_core(created, user_id)

    async def get_horizon_capture_by_dedupe_key(self, user_id: str, dedupe_key: str) -> dict[str, Any] | None:
        if not self._owned(user_id):
            self._fallback_notice("get_horizon_capture_by_dedupe_key")
            return await self._repo.get_horizon_capture_by_dedupe_key(user_id, dedupe_key)
        rows = await self._adapter.list_captures(limit=200)
        for row in rows:
            core = self._capture_to_core(row, user_id)
            if core.get("dedupe_key") == dedupe_key:
                return core
        if len(rows) >= 200:
            raise BusinessValidationError("Búsqueda de captura ambigua: página llena.", status_code=502)
        return None

    async def list_horizon_captures(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self._owned(user_id):
            self._fallback_notice("list_horizon_captures")
            return await self._repo.list_horizon_captures(user_id, limit=limit)
        rows = await self._adapter.list_captures(limit=limit)
        return [self._capture_to_core(row, user_id) for row in rows]


EXPORT_CAPTURE_LIMIT = 5000


async def export_personal_subset(repo: Any, user_id: str) -> dict[str, Any]:
    """Read one user's ERP-bound personal docs. Explicit user, never a sweep."""
    if not str(user_id or "").strip():
        raise BusinessValidationError("Exportación sin usuario.", status_code=400)
    objectives = await repo.list_horizon_objectives(user_id)
    captures = await repo.list_horizon_captures(user_id, limit=EXPORT_CAPTURE_LIMIT)
    if len(captures or []) >= EXPORT_CAPTURE_LIMIT:
        raise BusinessValidationError("Demasiadas capturas para un lote; migrar por partes.", status_code=400)
    return {"user_id": user_id,
            "objectives": [dict(row) for row in (objectives or [])],
            "captures": [dict(row) for row in (captures or [])]}


async def import_personal_subset(adapter: JardalERPPersonal, snapshot: dict[str, Any],
                                 *, key: str) -> dict[str, Any]:
    """Write one user's subset to the ERP import. Idempotent by external ids."""
    if not isinstance(snapshot, dict) or not str(snapshot.get("user_id") or "").strip():
        raise BusinessValidationError("Importación sin usuario.", status_code=400)
    objectives = []
    for row in snapshot.get("objectives") or []:
        if not isinstance(row, dict):
            continue
        body = ERPHorizonStore._objective_to_erp(row)
        body["externalId"] = row.get("source_key") or row.get("objective_id")
        objectives.append(body)
    captures = []
    for row in snapshot.get("captures") or []:
        if not isinstance(row, dict):
            continue
        entry: dict[str, Any] = {"text": row.get("text"), "kind": row.get("kind"),
                                 "channel": row.get("channel"),
                                 "externalId": row.get("dedupe_key") or row.get("capture_id")}
        for field in ("origin", "status", "linked_objective_id", "linked_task_id"):
            if row.get(field) is not None:
                entry[field] = row[field]
        captures.append(entry)
    return await adapter.import_personal({"objectives": objectives, "captures": captures}, key=key)


async def verify_personal_subset(adapter: JardalERPPersonal, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Re-read the ERP subset and compare external ids. Bounded, explicit."""
    if not isinstance(snapshot, dict):
        raise BusinessValidationError("Verificación inválida.", status_code=400)
    user_id = str(snapshot.get("user_id") or "")
    want_objectives = {str((row.get("source_key") or row.get("objective_id") or ""))
                       for row in (snapshot.get("objectives") or []) if isinstance(row, dict)}
    want_captures = {str((row.get("dedupe_key") or row.get("capture_id") or ""))
                     for row in (snapshot.get("captures") or []) if isinstance(row, dict)}
    want_objectives.discard("")
    want_captures.discard("")
    store = ERPHorizonStore(None, adapter)
    seen_objectives = {str(row.get("source_key") or "") for row in await store.list_horizon_objectives(user_id)}
    seen_captures = {str(row.get("dedupe_key") or "") for row in await store.list_horizon_captures(user_id)}
    missing_objectives = sorted(want_objectives - seen_objectives)
    missing_captures = sorted(want_captures - seen_captures)
    return {"ok": not missing_objectives and not missing_captures,
            "missing_objectives": missing_objectives, "missing_captures": missing_captures,
            "want_objectives": len(want_objectives), "want_captures": len(want_captures),
            "seen_objectives": len(seen_objectives), "seen_captures": len(seen_captures)}
