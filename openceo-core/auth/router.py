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

    from auth.erp_identity import identity_provider_is_erp

    settings = get_settings()
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    try:
        erp_managed = identity_provider_is_erp(settings)
    except ValueError:
        raise HTTPException(status_code=503, detail="Proveedor de identidad no válido") from None
    if erp_managed:
        return await _login_via_erp(request, response, settings, username, password)

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


async def _login_via_erp(request: Request, response: Response, settings: Any,
                        username: str, password: str) -> dict[str, Any]:
    from business.errors import BusinessError

    from auth.erp_identity import ERP_SESSION_COOKIE, JardalERPIdentity, upsert_mapped_user

    email = str(username or "").strip().lower()
    if not email or not password:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    adapter = JardalERPIdentity(settings)
    try:
        result = await adapter.login(email=email, password=password)
    except BusinessError as exc:
        if getattr(exc, "status_code", None) in (401, 403):
            raise HTTPException(status_code=401, detail="Credenciales inválidas") from None
        raise HTTPException(status_code=503, detail="Identidad ERP no disponible") from None
    finally:
        await adapter.aclose()
    db = _get_db(request)
    user_doc = upsert_mapped_user(db, result["identity"])
    user = UserDocument(**{k: v for k, v in user_doc.items() if k != "_id"})
    access_token = create_access_token(user.user_id, user.username)
    refresh_token = create_refresh_token(user.user_id)
    response.set_cookie(key="openceo_session", value=access_token,
                        **cookie_settings(max_age=settings.JWT_EXPIRE_MINUTES * 60))
    response.set_cookie(key="openceo_refresh", value=refresh_token,
                        **cookie_settings(max_age=settings.JWT_REFRESH_EXPIRE_DAYS * 86400, refresh=True))
    response.set_cookie(key=ERP_SESSION_COOKIE, value=result["token"],
                        **cookie_settings(max_age=settings.JWT_REFRESH_EXPIRE_DAYS * 86400, refresh=True))
    user_dict = user.model_dump()
    user_dict.pop("password_hash", None)
    return {"user": user_dict, "token": "heaven"}


@auth_router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    from business.errors import BusinessError
    from settings import get_settings

    from auth.erp_identity import ERP_SESSION_COOKIE, JardalERPIdentity, identity_provider_is_erp

    try:
        if identity_provider_is_erp(get_settings()):
            erp_token = request.cookies.get(ERP_SESSION_COOKIE) or ""
            if erp_token:
                adapter = JardalERPIdentity(get_settings())
                try:
                    await adapter.logout(erp_token)
                except BusinessError:
                    pass
                finally:
                    await adapter.aclose()
            response.delete_cookie(ERP_SESSION_COOKIE)
    except ValueError:
        pass
    response.delete_cookie("openceo_session")
    response.delete_cookie("openceo_refresh")
    return {"ok": True}


@auth_router.post("/refresh")
async def refresh(request: Request, response: Response) -> dict[str, str]:
    from business.errors import BusinessError
    from settings import get_settings

    from auth.erp_identity import ERP_SESSION_COOKIE, JardalERPIdentity, identity_provider_is_erp

    settings = get_settings()
    try:
        erp_managed = identity_provider_is_erp(settings)
    except ValueError:
        raise HTTPException(status_code=503, detail="Proveedor de identidad no válido") from None
    erp_identity: dict[str, Any] | None = None
    if erp_managed:
        erp_token = request.cookies.get(ERP_SESSION_COOKIE) or ""
        adapter = JardalERPIdentity(settings)
        try:
            erp_identity = await adapter.introspect(erp_token)
        except BusinessError:
            raise HTTPException(status_code=401, detail="Sesión ERP revocada o expirada") from None
        finally:
            await adapter.aclose()
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
    if erp_identity is not None and user.external_id != erp_identity["external_id"]:
        raise HTTPException(status_code=401, detail="Sesión ERP no corresponde al usuario")
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
    from settings import get_settings

    from auth.erp_identity import ensure_local_directory

    ensure_local_directory(get_settings())
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
