"""Authentication router: login, logout, refresh, register, me."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from auth.jwt import (
    cookie_settings,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from auth.middleware import get_current_user, require_permissions
from auth.service import (
    compute_user_permissions,
    get_groups_for_user,
    hash_password,
    verify_password,
)
from state import GroupDocument, UserDocument

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


def _get_db(request: Request) -> Any:
    """Get DB instance from app state."""
    return request.app.state.auth_db


@auth_router.post("/login")
async def login(request: Request, response: Response) -> dict[str, Any]:
    from settings import get_settings

    settings = get_settings()
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    db = _get_db(request)
    user_doc = db.users.find_one({"username": username, "is_active": True})
    if not user_doc or not verify_password(password, user_doc["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    user = UserDocument(**user_doc)
    groups = get_groups_for_user(user.group_ids, db)

    # Compute permissions
    all_perms = compute_user_permissions(user, groups)

    # Update permissions in DB
    db.users.update_one(
        {"user_id": user.user_id},
        {"$set": {"all_permissions": all_perms, "last_login_at": datetime.now(timezone.utc)}},
    )

    # Create tokens
    access_token = create_access_token(user.user_id, user.username)
    refresh_token = create_refresh_token(user.user_id)

    # Set cookies
    response.set_cookie(
        key="openceo_session",
        value=access_token,
        **cookie_settings(max_age=settings.JWT_EXPIRE_MINUTES * 60),
    )
    response.set_cookie(
        key="openceo_refresh",
        value=refresh_token,
        **cookie_settings(max_age=settings.JWT_REFRESH_EXPIRE_DAYS * 86400, refresh=True),
    )

    user_dict = user.model_dump()
    user_dict["all_permissions"] = all_perms
    del user_dict["password_hash"]

    return {"user": user_dict, "token": "heaven"}


@auth_router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie("openceo_session")
    response.delete_cookie("openceo_refresh")
    return {"ok": True}


@auth_router.post("/refresh")
async def refresh(request: Request, response: Response) -> dict[str, str]:
    from settings import get_settings

    settings = get_settings()
    token = request.cookies.get("openceo_refresh")
    if not token:
        raise HTTPException(status_code=401, detail="No hay token de refresh")

    payload = decode_token(token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Token de refresh inválido")

    user_id = payload.get("sub")
    db = _get_db(request)
    user_doc = db.users.find_one({"user_id": user_id, "is_active": True})
    if not user_doc:
        raise HTTPException(status_code=401, detail="Usuario no encontrado")

    user = UserDocument(**user_doc)
    access_token = create_access_token(user.user_id, user.username)
    refresh_token = create_refresh_token(user.user_id)

    response.set_cookie(
        key="openceo_session",
        value=access_token,
        **cookie_settings(max_age=settings.JWT_EXPIRE_MINUTES * 60),
    )
    response.set_cookie(
        key="openceo_refresh",
        value=refresh_token,
        **cookie_settings(max_age=settings.JWT_REFRESH_EXPIRE_DAYS * 86400, refresh=True),
    )

    return {"token": "heaven"}


@auth_router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    user = await get_current_user(request)
    user_dict = user.model_dump()
    del user_dict["password_hash"]
    return user_dict


@auth_router.post("/register")
async def register(
    request: Request,
    current_user: UserDocument = Depends(require_permissions("manage_users")),
) -> dict[str, Any]:
    body = await request.json()
    db = _get_db(request)

    # Check username/email doesn't exist
    if db.users.find_one({"username": body.get("username")}):
        raise HTTPException(status_code=400, detail="Username ya existe")
    if db.users.find_one({"email": body.get("email")}):
        raise HTTPException(status_code=400, detail="Email ya existe")

    group_ids = body.get("group_ids", [])
    allowed_projects = body.get("allowed_projects", [])
    is_client = body.get("is_client", False)
    groups = get_groups_for_user(group_ids, db)

    temp_user = UserDocument(
        username=body["username"],
        email=body["email"],
        group_ids=group_ids,
        allowed_projects=allowed_projects,
        is_client=is_client,
    )

    user = UserDocument(
        username=body["username"],
        email=body["email"],
        password_hash=hash_password(body["password"]),
        display_name=body.get("display_name", body["username"]),
        group_ids=group_ids,
        allowed_projects=allowed_projects,
        is_client=is_client,
        all_permissions=compute_user_permissions(temp_user, groups),
    )
    db.users.insert_one(user.model_dump())

    user_dict = user.model_dump()
    del user_dict["password_hash"]
    return user_dict
