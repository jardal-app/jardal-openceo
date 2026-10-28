import hmac
from typing import Any

from fastapi import HTTPException


def require_erp_webhook(settings: Any, provider: str, token: str | None) -> None:
    secret = str(getattr(settings, "JARDAL_ERP_WEBHOOK_SECRET", "") or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="JARDAL_ERP_WEBHOOK_SECRET no está configurado.")
    if not token or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Secreto de webhook inválido.")
    if provider != "jardal_erp":
        raise HTTPException(status_code=409, detail="jardal_erp_source_inactive")
