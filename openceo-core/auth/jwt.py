"""JWT encode/decode, cookie settings."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

from settings import get_settings


def _get_settings():
    return get_settings()


def create_access_token(user_id: str, username: str) -> str:
    settings = _get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload: dict[str, Any] = {
        "sub": user_id,
        "username": username,
        "exp": expire,
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    settings = _get_settings()
    expire = datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS)
    payload: dict[str, Any] = {
        "sub": user_id,
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    settings = _get_settings()
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def cookie_settings(max_age: int = 86400, refresh: bool = False) -> dict[str, Any]:
    """Returns dict for Set-Cookie header."""
    return {
        "httponly": True,
        "secure": os.getenv("ENV", "development") == "production",
        "samesite": "lax",
        "path": "/",
        "max_age": max_age,
    }
