"""Authenticated social reads. Account grants remain mandatory even for admins."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from auth.middleware import require_permissions
from research.policies import RESEARCH_READ, RESEARCH_RUN, principal_from_user
from research.router import is_safe_project_key
from research.social import SocialError


class SocialReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_key: str
    account_id: str
    operation: str
    source: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=5)


def build_social_router(*, get_broker, project_cfg_for, enabled):
    router = APIRouter(prefix="/api/research/social", tags=["research"])

    def authority(request, payload, user):
        principal = principal_from_user(user)
        if (not is_safe_project_key(payload.project_key) or
                (not principal.is_admin and "*" not in principal.allowed_projects
                 and payload.project_key not in principal.allowed_projects)):
            raise HTTPException(404, "Proyecto no encontrado")
        cfg = project_cfg_for(request, payload.project_key).get("research", {})
        if not enabled() or not cfg.get("enabled"):
            raise HTTPException(503, "Research social deshabilitado")
        if payload.account_id not in cfg.get("allowed_account_refs", []):
            raise HTTPException(403, "Cuenta no autorizada por el proyecto")
        broker = get_broker(request)
        if broker is None:
            raise HTTPException(503, "Sesiones sociales no configuradas")
        scope = {"account_id": payload.account_id, "project": payload.project_key,
                 "owner": principal.user_id, "consumer": "http:" + principal.user_id,
                 "operation": payload.operation, "source": payload.source}
        return broker, scope

    @router.post("/read")
    async def read(payload: SocialReadRequest, request: Request,
                   user: Any = Depends(require_permissions(RESEARCH_RUN))):
        broker, scope = authority(request, payload, user)
        try:
            return await broker.read(scope=scope, limit=payload.limit)
        except SocialError as exc:
            code = 403 if exc.reason in {"access_denied", "operation_denied"} else 409
            raise HTTPException(code, exc.reason) from None

    @router.post("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str, payload: SocialReadRequest, request: Request,
                       user: Any = Depends(require_permissions(RESEARCH_READ))):
        broker, scope = authority(request, payload, user)
        try:
            return await broker.get_artifact(artifact_id, scope=scope)
        except SocialError as exc:
            raise HTTPException(404, "Artefacto no disponible") from None

    return router
