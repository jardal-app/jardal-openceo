from __future__ import annotations

import httpx


class N8NClient:
    """Minimal n8n webhook client."""

    def __init__(self, base_url: str, api_key: str = "") -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def trigger_webhook(self, workflow_id: str, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-N8N-API-KEY"] = self._api_key

        url = f"{self._base_url}/webhook/{workflow_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json() if response.content else {"ok": True}
