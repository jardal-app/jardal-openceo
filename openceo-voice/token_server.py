"""Minimal token server for LiveKit voice clients."""
from __future__ import annotations

import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api
from pydantic import BaseModel
from project_env import load_project_env

load_project_env()

app = FastAPI(title="OpenCEO Voice Token Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_API_KEY = (os.getenv("LIVEKIT_API_KEY") or "").strip()
LIVEKIT_API_SECRET = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
LIVEKIT_URL = (os.getenv("LIVEKIT_URL") or "ws://127.0.0.1:7880").strip()


class TokenRequest(BaseModel):
    user_id: str
    room: str = "openceo-voice"
    project: str | None = None
    model: str = "dev"


@app.get("/health")
async def health() -> dict:
    configured = bool(LIVEKIT_API_KEY and LIVEKIT_API_SECRET)
    return {
        "status": "ok" if configured else "degraded",
        "service": "openceo-voice-token",
        "livekit_configured": configured,
        "livekit_url": LIVEKIT_URL,
    }


@app.post("/api/voice/token")
async def create_token(req: TokenRequest) -> dict:
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(500, "LIVEKIT_API_KEY/SECRET not configured")

    metadata = {"user_id": req.user_id, "model": req.model}
    if req.project:
        metadata["project"] = req.project

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(req.user_id)
        .with_name(req.user_id)
        .with_grants(api.VideoGrants(room_join=True, room=req.room))
        .with_metadata(json.dumps(metadata))
        .to_jwt()
    )

    return {"token": token, "url": LIVEKIT_URL}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("VOICE_TOKEN_PORT", "18766"))
    uvicorn.run(app, host="0.0.0.0", port=port)
