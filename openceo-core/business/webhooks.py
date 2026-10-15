import hmac
from typing import Any

from fastapi import HTTPException


def require_legacy_webhook(settings: Any, provider: str, token: str | None) -> None:
    secret = str(settings.ERPNEXT_WEBHOOK_SECRET or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="ERPNEXT_WEBHOOK_SECRET no está configurado.")
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Secreto de webhook inválido.")
    if provider != "erpnext":
        raise HTTPException(status_code=409, detail="legacy_source_inactive")
