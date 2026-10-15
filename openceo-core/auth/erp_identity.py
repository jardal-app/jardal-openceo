"""Federated human identity backed by Jardal ERP (TASK-2026-00261).

Local directory stays the default. ERP federation activates only with full ERP
config plus the ready authority manifest produced by TASK-2026-00266, mirroring
the business provider gate. Revocation, expiry and denial never fall back to an
admin user, and an enterprise admin is never a host/personal grant.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from business.errors import BusinessPermissionError, BusinessValidationError
from business.http import JardalERPClient, erp_service_configured
from business.provider import validate_authority_manifest

ERP_SESSION_COOKIE = "erp_session"

# ERP capability -> Core permissions (SECURITY.md mapping table). Unknown ERP
# capabilities map to no Core permission (fail-closed); finance/identity/
# execution capabilities have no Core equivalent and stay ERP-side.
ERP_PERMISSION_MAP: dict[str, tuple[str, ...]] = {
    "task.read": ("read_tickets",),
    "task.write": ("write_tickets",),
    "task.transition": ("write_tickets",),
    "approval.decide": ("approve_tickets", "reject_tickets", "review_tickets"),
    "approval.request": ("review_tickets",),
    "client_review": ("read_tickets", "client_review"),
    "qa_review": ("qa_review",),
}


def identity_provider_is_erp(settings: Any) -> bool:
    if str(getattr(settings, "IDENTITY_PROVIDER", "local")).strip() != "jardal_erp":
        return False
    validate_authority_manifest(settings)
    if not erp_service_configured(settings):
        raise ValueError("Directorio jardal_erp sin config de servicio completa.")
    return True


def ensure_local_directory(settings: Any) -> None:
    from fastapi import HTTPException

    if identity_provider_is_erp(settings):
        raise HTTPException(status_code=409, detail="Directorio gestionado por Jardal ERP; alta/baja en el ERP")


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, (str, int))]
    return []


def map_erp_identity(view: dict[str, Any], *, settings: Any) -> dict[str, Any]:
    """Validate an ERP identity view and map it to Core fields.

    Requires stable id + email + active account. Privilege fields parse
    defensively and fail closed: unknown shapes grant nothing.
    """
    if not isinstance(view, dict):
        raise BusinessValidationError("Vista de identidad ERP inválida.", status_code=502)
    node = view.get("user", view)
    if not isinstance(node, dict):
        raise BusinessValidationError("Vista de identidad ERP inválida.", status_code=502)
    external_id = str(node.get("id") or node.get("userId") or "").strip()
    email = str(node.get("email") or "").strip().lower()
    active = node.get("isActive", node.get("active", node.get("is_active", None)))
    if not external_id or not email or active is not True:
        raise BusinessPermissionError("Identidad ERP inactiva o incompleta.", status_code=401)
    configured_workspace = str(getattr(settings, "JARDAL_ERP_WORKSPACE_ID", "") or "")
    seen_workspaces = _as_str_list(node.get("workspaceIds") or node.get("workspaces") or node.get("workspace_id"))
    if seen_workspaces and configured_workspace and configured_workspace not in seen_workspaces:
        raise BusinessPermissionError("Identidad ERP fuera del workspace.", status_code=403)
    raw_caps: list[str] = []
    for key in ("permissions", "scopes", "capabilities"):
        raw_caps.extend(_as_str_list(node.get(key)))
    perms: set[str] = set()
    for cap in raw_caps:
        perms.update(ERP_PERMISSION_MAP.get(cap.strip(), ()))
    projects = _as_str_list(node.get("projects") or node.get("allowedProjects") or node.get("projectKeys"))
    is_client = bool(node.get("isClient", node.get("is_client", False)))
    if is_client:
        perms = {"read_tickets", "client_review"}
    try:
        auth_version = int(node.get("authVersion", node.get("auth_version", node.get("sessionVersion", 0))) or 0)
    except (TypeError, ValueError):
        auth_version = 0
    return {
        "external_id": external_id,
        "email": email,
        "username": str(node.get("username") or email.split("@")[0]),
        "display_name": str(node.get("displayName") or node.get("display_name") or email.split("@")[0]),
        "all_permissions": sorted(perms),
        "allowed_projects": sorted(set(projects)),
        "is_client": is_client,
        "auth_version": auth_version,
    }


class JardalERPIdentity:
    provider = "jardal_erp"

    def __init__(self, settings: Any, client: JardalERPClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self.client = client or JardalERPClient(settings, transport=transport)
        self._owns_client = client is None

    @property
    def is_enabled(self) -> bool:
        return self.client.is_configured

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def login(self, *, email: str, password: str) -> dict[str, Any]:
        result = await self.client.request(
            "POST", "/api/v1/auth/login",
            body={"email": email, "password": password},
            key=f"auth.login:{email.strip().lower()}",
            anonymous=True,
        )
        token = str(result.get("token") or result.get("sessionToken") or "")
        if not token:
            raise BusinessValidationError("Login ERP sin token de sesión.", status_code=502)
        return {"token": token, "identity": map_erp_identity(result, settings=self._settings)}

    async def introspect(self, user_token: str) -> dict[str, Any]:
        if not user_token or len(user_token) > 512:
            raise BusinessPermissionError("Sesión ERP ausente o inválida.", status_code=401)
        result = await self.client.request("GET", "/api/v1/auth/me", bearer=user_token)
        return map_erp_identity(result, settings=self._settings)

    async def logout(self, user_token: str) -> None:
        if not user_token:
            return
        await self.client.request("POST", "/api/v1/auth/logout", body={}, bearer=user_token,
                                  key=f"auth.logout:{hash(user_token) & 0xFFFFFFFF:08x}")


def upsert_mapped_user(db: Any, mapped: dict[str, Any]) -> dict[str, Any]:
    from state import UserDocument

    now = datetime.now(timezone.utc)
    link = db.identity_map.find_one({"erp_user_id": mapped["external_id"]})
    if link:
        user_doc = db.users.find_one({"user_id": link["user_id"]})
        if user_doc:
            fresh = {"email": mapped["email"], "display_name": mapped["display_name"],
                     "all_permissions": mapped["all_permissions"],
                     "allowed_projects": mapped["allowed_projects"],
                     "is_client": mapped["is_client"], "is_active": True, "is_admin": False,
                     "auth_provider": "jardal_erp", "external_id": mapped["external_id"],
                     "auth_version": mapped["auth_version"], "last_login_at": now,
                     "updated_at": now}
            db.users.update_one({"user_id": link["user_id"]}, {"$set": fresh})
            db.identity_map.update_one(
                {"erp_user_id": mapped["external_id"]},
                {"$set": {"email": mapped["email"], "auth_version": mapped["auth_version"],
                          "updated_at": now}},
            )
            user_doc.update(fresh)
            return user_doc
    clashing = db.users.find_one({"email": mapped["email"]})
    if clashing:
        raise BusinessValidationError(
            "Cuenta sin mapeo explícito; registrar el vínculo antes del login.", status_code=409)
    user = UserDocument(
        username=mapped["username"], email=mapped["email"], password_hash="",
        display_name=mapped["display_name"], is_admin=False,
        is_client=mapped["is_client"], all_permissions=mapped["all_permissions"],
        allowed_projects=mapped["allowed_projects"], auth_provider="jardal_erp",
        external_id=mapped["external_id"], auth_version=mapped["auth_version"],
    )
    dumped = user.model_dump()
    dumped["last_login_at"] = now
    db.users.insert_one(dict(dumped))
    db.identity_map.insert_one({"erp_user_id": mapped["external_id"], "user_id": user.user_id,
                                "email": mapped["email"], "auth_version": mapped["auth_version"],
                                "mapped_at": now, "updated_at": now})
    reread = db.identity_map.find_one({"erp_user_id": mapped["external_id"]})
    if not reread or reread.get("user_id") != user.user_id:
        raise BusinessValidationError("Mapeo de identidad no verificado.", status_code=500)
    return dumped


def export_identity_map(db: Any, identities: list[dict[str, Any]]) -> dict[str, Any]:
    """Write an explicit bounded identity mapping list, verifying each write.

    No discovery, no sweep: the caller supplies every row. Rejects personal
    records and refuses to overwrite an existing link with another user.
    """
    if not isinstance(identities, list) or len(identities) > 5000:
        raise BusinessValidationError("Lista de mapeo inválida o excesiva.", status_code=400)
    now = datetime.now(timezone.utc)
    written = 0
    for row in identities:
        if not isinstance(row, dict):
            raise BusinessValidationError("Fila de mapeo inválida.", status_code=400)
        if row.get("personal") or row.get("space") == "personal":
            raise BusinessValidationError("Registros personales fuera del mapeo empresarial.", status_code=400)
        erp_user_id = str(row.get("erp_user_id") or "").strip()
        user_id = str(row.get("user_id") or "").strip()
        if not erp_user_id or not user_id:
            raise BusinessValidationError("Fila de mapeo incompleta.", status_code=400)
        existing = db.identity_map.find_one({"erp_user_id": erp_user_id})
        if existing and existing.get("user_id") != user_id:
            raise BusinessValidationError(f"Mapeo en conflicto para {erp_user_id}.", status_code=409)
        if existing:
            continue
        try:
            auth_version = int(row.get("auth_version") or 0)
        except (TypeError, ValueError):
            raise BusinessValidationError("Versión de autorización inválida.", status_code=400) from None
        db.identity_map.insert_one({"erp_user_id": erp_user_id, "user_id": user_id,
                                    "email": str(row.get("email") or ""),
                                    "auth_version": auth_version,
                                    "mapped_at": now, "updated_at": now})
        reread = db.identity_map.find_one({"erp_user_id": erp_user_id})
        if not reread or reread.get("user_id") != user_id:
            raise BusinessValidationError("Mapeo de identidad no verificado.", status_code=500)
        written += 1
    return {"written": written, "total": len(identities)}
