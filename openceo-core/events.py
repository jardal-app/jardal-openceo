from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from fastapi import WebSocket


class RealtimeHub:
    """WebSocket broadcaster for dashboard live updates."""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        if not self._connections:
            return

        message = {"event": event, "payload": payload}
        dead: list[WebSocket] = []

        for ws in self._connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)

        for ws in dead:
            with suppress(Exception):
                await ws.close()
            self.disconnect(ws)

    async def keepalive_loop(self, websocket: WebSocket) -> AsyncIterator[str]:
        while True:
            data = await websocket.receive_text()
            yield data
