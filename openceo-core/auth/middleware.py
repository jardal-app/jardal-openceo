"""Authentication middleware and permission dependencies."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from auth.jwt import decode_token
from state import UserDocument


async def get_current_user(request: Request) -> UserDocument:
    """Dependency that gets the current user from the cookie."""
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

    user_doc.pop("_id", None)
    return UserDocument(**user_doc)


def require_permissions(*required_perms: str):
    """Dependency factory that checks user permissions."""

    async def checker(request: Request) -> UserDocument:
        user = await get_current_user(request)
        if user.is_admin:
            return user
        for perm in required_perms:
            if perm not in user.all_permissions:
                raise HTTPException(status_code=403, detail=f"Permiso requerido: {perm}")
        return user

    return checker


def require_admin():
    """Dependency factory for host-level endpoints (tmux, dev sessions)."""

    async def checker(request: Request) -> UserDocument:
        user = await get_current_user(request)
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Solo administradores")
        return user

    return checker
