"""Authentication middleware and permission dependencies."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from auth.jwt import decode_token
from state import UserDocument


async def _revalidate_federated_user(db: Any, user_doc: dict[str, Any], erp_token: str) -> dict[str, Any]:
    from business.errors import BusinessError
    from settings import get_settings

    from auth.erp_identity import JardalERPIdentity

    adapter = JardalERPIdentity(get_settings())
    try:
        mapped = await adapter.introspect(erp_token)
    except BusinessError as exc:
        if getattr(exc, "status_code", None) in (401, 403):
            raise HTTPException(status_code=401, detail="Sesión ERP revocada o expirada") from None
        raise HTTPException(status_code=503, detail="Identidad ERP no disponible") from None
    finally:
        await adapter.aclose()
    if mapped["external_id"] != (user_doc.get("external_id") or ""):
        raise HTTPException(status_code=401, detail="Sesión ERP no corresponde al usuario")
    updates = {"all_permissions": mapped["all_permissions"], "allowed_projects": mapped["allowed_projects"],
               "is_client": mapped["is_client"], "auth_version": mapped["auth_version"]}
    if any(user_doc.get(k) != v for k, v in updates.items()):
        db.users.update_one({"user_id": user_doc["user_id"]}, {"$set": updates})
        user_doc.update(updates)
    return user_doc


async def get_current_user(request: Request) -> UserDocument:
    """Dependency that gets the current user from the cookie."""
    from settings import get_settings

    from auth.erp_identity import ERP_SESSION_COOKIE, identity_provider_is_erp

    try:
        erp_managed = identity_provider_is_erp(get_settings())
    except ValueError:
        raise HTTPException(status_code=503, detail="Proveedor de identidad no válido") from None
    token = request.cookies.get("openceo_session")
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado")

    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Token inválido")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token corrupto")

    # Get DB from app state
    db = request.app.state.auth_db
    user_doc = db.users.find_one({"user_id": user_id, "is_active": True})
    if not user_doc:
        raise HTTPException(status_code=401, detail="Usuario no encontrado o inactivo")

    if erp_managed:
        if user_doc.get("auth_provider") != "jardal_erp":
            raise HTTPException(status_code=401, detail="Cuenta local desactivada bajo directorio ERP")
        erp_token = request.cookies.get(ERP_SESSION_COOKIE) or ""
        user_doc = await _revalidate_federated_user(db, user_doc, erp_token)

    user_doc.pop("_id", None)
    return UserDocument(**user_doc)


def require_permissions(*required_perms: str):
    """Dependency factory that checks user permissions."""

    async def checker(request: Request) -> UserDocument:
        user = await get_current_user(request)
        if user.is_admin and user.auth_provider == "local":
            return user
        for perm in required_perms:
            if perm not in user.all_permissions:
                raise HTTPException(status_code=403, detail=f"Permiso requerido: {perm}")
        return user

    checker._openceo_auth_kind = "permission"
    return checker


def require_admin():
    """Dependency factory for host-level endpoints (tmux, dev sessions, voice)."""

    async def checker(request: Request) -> UserDocument:
        user = await get_current_user(request)
        if not user.is_admin or user.auth_provider != "local":
            raise HTTPException(status_code=403, detail="Solo administradores")
        return user

    checker._openceo_auth_kind = "admin"
    return checker
