#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

NOTION_API_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MAX_BLOCKS_PER_REQUEST = 100
DEFAULT_STARTER_TITLES = {
    "getting started",
    "project roadmap",
    "meeting notes",
    "team wiki",
    "tasks",
    "roadmap",
}


@dataclass(frozen=True)
class NotionObjectRef:
    id: str
    title: str
    object: str
    url: str | None = None


class NotionAdminClient:
    def __init__(self, token: str) -> None:
        self._token = token.strip()
        if not self._token:
            raise ValueError("Falta NOTION_API_TOKEN.")
        self._timeout = httpx.Timeout(30.0, connect=10.0)

    def search(self, page_size: int = 100) -> dict[str, Any]:
        return self._request(
            "POST",
            "/search",
            json={
                "page_size": page_size,
                "sort": {"direction": "ascending", "timestamp": "last_edited_time"},
            },
        )

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def list_children(self, block_id: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"page_size": MAX_BLOCKS_PER_REQUEST}
            if cursor:
                params["start_cursor"] = cursor
            payload = self._request("GET", f"/blocks/{block_id}/children", params=params)
            results.extend(payload.get("results", []))
            if not payload.get("has_more"):
                return results
            cursor = payload.get("next_cursor")

    def delete_block(self, block_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/blocks/{block_id}")

    def create_page(
        self,
        *,
        parent_page_id: str,
        title: str,
        icon: str | None = None,
        children: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [_text_object(title)],
                }
            },
        }
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        if children:
            payload["children"] = children[:MAX_BLOCKS_PER_REQUEST]
        created = self._request("POST", "/pages", json=payload)

        remaining_children = (children or [])[MAX_BLOCKS_PER_REQUEST:]
        if remaining_children:
            self.append_children(created["id"], remaining_children)
        return created

    def append_children(self, block_id: str, children: list[dict[str, Any]]) -> None:
        for index in range(0, len(children), MAX_BLOCKS_PER_REQUEST):
            chunk = children[index : index + MAX_BLOCKS_PER_REQUEST]
            self._request(
                "PATCH",
                f"/blocks/{block_id}/children",
                json={"children": chunk},
            )

    def create_database(
        self,
        *,
        parent_page_id: str,
        title: str,
        description: str,
        icon: str | None,
        initial_data_source_name: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [_text_object(title)],
            "description": [_text_object(description)],
            "is_inline": False,
            "initial_data_source": {
                "name": initial_data_source_name,
                "properties": properties,
            },
        }
        if icon:
            payload["icon"] = {"type": "emoji", "emoji": icon}
        return self._request("POST", "/databases", json=payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{NOTION_API_BASE_URL}{path}"
        retry_delay = 1.0
        for attempt in range(3):
            with httpx.Client(
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
            ) as client:
                response = client.request(method, url, params=params, json=json)

            if response.status_code == 429 and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else retry_delay
                time.sleep(max(wait_seconds, retry_delay))
                retry_delay *= 2
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                try:
                    payload = response.json()
                    message = payload.get("message") or response.text
                except ValueError:
                    message = response.text
                raise RuntimeError(f"Notion API error: {message}") from exc

            return response.json() if response.content else {}

        raise RuntimeError("Notion rate limited the request repeatedly.")


def _text_object(text: str) -> dict[str, Any]:
    return {"type": "text", "text": {"content": text}}


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [_text_object(text)]},
    }


def _heading(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [_text_object(text)]},
    }


def _bullet(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_text_object(text)]},
    }


def _todo(text: str, checked: bool = False) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {"rich_text": [_text_object(text)], "checked": checked},
    }


def _callout(text: str, emoji: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": emoji},
            "rich_text": [_text_object(text)],
        },
    }


def _link_to_page(page_id: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "link_to_page",
        "link_to_page": {"type": "page_id", "page_id": page_id},
    }


def _inventory(client: NotionAdminClient) -> dict[str, Any]:
    payload = client.search()
    results = []
    for item in payload.get("results", []):
        title = _extract_title(item)
        results.append(
            {
                "object": item.get("object"),
                "id": item.get("id"),
                "title": title,
                "parent_type": (item.get("parent") or {}).get("type"),
                "in_trash": item.get("in_trash"),
                "url": item.get("url"),
            }
        )
    return {
        "count": len(results),
        "results": results,
    }


def _extract_title(item: dict[str, Any]) -> str:
    if item.get("object") == "page":
        for key in ("title", "Name"):
            prop = (item.get("properties") or {}).get(key)
            if isinstance(prop, dict):
                chunks = prop.get("title") or prop.get("rich_text") or []
                title = "".join(str(chunk.get("plain_text") or "") for chunk in chunks).strip()
                if title:
                    return title
        return ""
    if item.get("object") == "database":
        chunks = item.get("title") or []
        return "".join(str(chunk.get("plain_text") or "") for chunk in chunks).strip()
    return ""


def _cleanup_default_children(client: NotionAdminClient, parent_page_id: str) -> list[dict[str, Any]]:
    deleted: list[dict[str, Any]] = []
    for block in client.list_children(parent_page_id):
        block_type = str(block.get("type") or "")
        if block_type not in {"child_page", "child_database"}:
            continue
        title = str((block.get(block_type) or {}).get("title") or "").strip()
        if title.lower() not in DEFAULT_STARTER_TITLES:
            continue
        client.delete_block(str(block["id"]))
        deleted.append({"id": block["id"], "title": title, "type": block_type})
    return deleted


def _bootstrap_workspace(
    client: NotionAdminClient,
    *,
    parent_page_id: str,
    hub_title: str,
    cleanup_defaults: bool,
) -> dict[str, Any]:
    client.retrieve_page(parent_page_id)

    deleted = _cleanup_default_children(client, parent_page_id) if cleanup_defaults else []

    hub = client.create_page(
        parent_page_id=parent_page_id,
        title=hub_title,
        icon="🧠",
        children=[
            _callout(
                "Este es el hub operativo de OpenCEO en Notion. La API no puede crear vistas de base de datos, así que la estructura base queda lista aquí y las vistas se agregan luego desde la UI.",
                "🛠️",
            ),
            _paragraph(
                "Estructura recomendada: una base principal de tickets, una página de workflow, una página de automatización y una carpeta de plantillas reutilizables."
            ),
        ],
    )

    workflow = client.create_page(
        parent_page_id=hub["id"],
        title="Workflow",
        icon="🧭",
        children=[
            _heading("Stages"),
            *[_bullet(text) for text in [
                "Inbox: ticket capturado pero todavía no preparado.",
                "Scoped: ticket aclarado y con contexto suficiente.",
                "Ready: listo para pasar a ejecución.",
                "Dev: trabajo en manos del agente de desarrollo.",
                "Review: revisión humana o técnica en curso.",
                "QA: validación funcional o técnica.",
                "Waiting: detenido a la espera de respuesta, dependencia o ventana.",
                "Blocked: impedimento externo o técnico.",
                "Done: cerrado con resultado satisfactorio.",
                "Failed: ejecución terminó con error.",
                "Canceled: el ticket ya no debe seguir.",
            ]],
            _heading("Runtime status"),
            *[_bullet(text) for text in [
                "queued",
                "in_progress",
                "waiting",
                "completed",
                "failed",
                "escalated",
                "interrupted",
            ]],
        ],
    )

    automation = client.create_page(
        parent_page_id=hub["id"],
        title="Automation",
        icon="⚙️",
        children=[
            _heading("Trigger to Dev"),
            _bullet("Si Stage cambia a Dev y Sync Enabled está activo, n8n envía la tarea a OpenCEO."),
            _bullet("OpenCEO devuelve runtime status, resumen y referencia interna."),
            _bullet("Notion se mantiene como capa humana y OpenCEO como capa de ejecución."),
            _heading("Manual views to add in Notion UI"),
            *[_bullet(text) for text in [
                "Board agrupado por Stage.",
                "Calendar usando Due.",
                "Table All Tickets.",
                "Table Waiting filtrado por Stage = Waiting o Runtime Status = waiting.",
                "Table Blocked/Failed.",
                "Table Dev filtrado por Stage = Dev.",
            ]],
        ],
    )

    templates_root = client.create_page(
        parent_page_id=hub["id"],
        title="Templates",
        icon="🧩",
        children=[
            _paragraph(
                "Estas páginas sirven como plantillas reutilizables. La API de Notion permite usar cualquier página accesible del mismo workspace como template_id al crear un ticket."
            )
        ],
    )

    database = client.create_database(
        parent_page_id=hub["id"],
        title="OpenCEO Tickets",
        description="Base operativa principal para tickets, handoff a dev y sincronización con OpenCEO.",
        icon="🎯",
        initial_data_source_name="Tickets",
        properties={
            "Name": {"title": {}},
            "Stage": {"select": {"options": [
                {"name": "Inbox", "color": "gray"},
                {"name": "Scoped", "color": "brown"},
                {"name": "Ready", "color": "blue"},
                {"name": "Dev", "color": "purple"},
                {"name": "Review", "color": "orange"},
                {"name": "QA", "color": "yellow"},
                {"name": "Waiting", "color": "pink"},
                {"name": "Blocked", "color": "red"},
                {"name": "Done", "color": "green"},
                {"name": "Failed", "color": "default"},
                {"name": "Canceled", "color": "default"},
            ]}},
            "Runtime Status": {"select": {"options": [
                {"name": "queued", "color": "gray"},
                {"name": "in_progress", "color": "blue"},
                {"name": "waiting", "color": "yellow"},
                {"name": "completed", "color": "green"},
                {"name": "failed", "color": "red"},
                {"name": "escalated", "color": "orange"},
                {"name": "interrupted", "color": "pink"},
            ]}},
            "Priority": {"select": {"options": [
                {"name": "low", "color": "gray"},
                {"name": "medium", "color": "blue"},
                {"name": "high", "color": "orange"},
                {"name": "critical", "color": "red"},
            ]}},
            "Type": {"select": {"options": [
                {"name": "feature", "color": "blue"},
                {"name": "bug", "color": "red"},
                {"name": "infra", "color": "purple"},
                {"name": "research", "color": "yellow"},
                {"name": "ops", "color": "orange"},
                {"name": "refactor", "color": "green"},
            ]}},
            "Project": {"select": {"options": [
                {"name": "jardal-openceo", "color": "blue"},
                {"name": "jardal-trading", "color": "orange"},
                {"name": "jardal-vpn", "color": "purple"},
                {"name": "cebitec", "color": "green"},
            ]}},
            "Agent": {"select": {"options": [
                {"name": "dev", "color": "blue"},
                {"name": "finance", "color": "yellow"},
                {"name": "secretary", "color": "green"},
            ]}},
            "Source": {"select": {"options": [
                {"name": "manual", "color": "gray"},
                {"name": "notion", "color": "brown"},
                {"name": "github", "color": "default"},
                {"name": "jira", "color": "blue"},
                {"name": "telegram", "color": "green"},
                {"name": "voice", "color": "purple"},
                {"name": "whatsapp", "color": "green"},
                {"name": "api", "color": "orange"},
                {"name": "openwebui", "color": "pink"},
            ]}},
            "Sync Enabled": {"checkbox": {}},
            "Needs Approval": {"checkbox": {}},
            "Due": {"date": {}},
            "External URL": {"url": {}},
            "Tags": {"multi_select": {"options": [
                {"name": "api", "color": "blue"},
                {"name": "frontend", "color": "green"},
                {"name": "backend", "color": "purple"},
                {"name": "infra", "color": "orange"},
                {"name": "security", "color": "red"},
                {"name": "automation", "color": "yellow"},
            ]}},
            "Source ID": {"rich_text": {}},
            "OpenCEO Ref": {"rich_text": {}},
            "Runtime Summary": {"rich_text": {}},
            "Last Sync At": {"date": {}},
        },
    )

    data_source_id = None
    data_sources = database.get("data_sources") or []
    if isinstance(data_sources, list) and data_sources:
        data_source_id = data_sources[0].get("id")

    template_pages = []
    for title, icon, bullets in [
        (
            "Template - Feature",
            "✨",
            [
                "Objetivo de negocio",
                "Criterios de aceptación",
                "Impacto esperado",
                "Riesgos",
                "Notas técnicas",
            ],
        ),
        (
            "Template - Bug",
            "🐞",
            [
                "Comportamiento actual",
                "Comportamiento esperado",
                "Pasos para reproducir",
                "Severidad",
                "Evidencia",
            ],
        ),
        (
            "Template - Infra",
            "🧱",
            [
                "Contexto operativo",
                "Cambio requerido",
                "Impacto en despliegue",
                "Rollback",
                "Validación",
            ],
        ),
        (
            "Template - Research",
            "🔎",
            [
                "Pregunta a responder",
                "Criterio de decisión",
                "Alternativas",
                "Riesgos",
                "Recomendación final",
            ],
        ),
    ]:
        page = client.create_page(
            parent_page_id=templates_root["id"],
            title=title,
            icon=icon,
            children=[_heading("Checklist"), *[_todo(item) for item in bullets]],
        )
        template_pages.append(
            {
                "id": page["id"],
                "title": title,
                "url": page.get("url"),
            }
        )

    client.append_children(
        hub["id"],
        [
            _heading("Quick links"),
            _link_to_page(workflow["id"]),
            _link_to_page(automation["id"]),
            _link_to_page(templates_root["id"]),
            _link_to_page(database["id"]),
        ],
    )

    return {
        "deleted_default_children": deleted,
        "hub": {"id": hub["id"], "title": hub_title, "url": hub.get("url")},
        "workflow_page": {"id": workflow["id"], "url": workflow.get("url")},
        "automation_page": {"id": automation["id"], "url": automation.get("url")},
        "templates_page": {"id": templates_root["id"], "url": templates_root.get("url")},
        "database": {
            "id": database["id"],
            "url": database.get("url"),
            "data_source_id": data_source_id,
        },
        "template_pages": template_pages,
        "notes": [
            "Las vistas Board y Calendar deben agregarse manualmente en la UI de Notion; la API oficial no soporta gestionarlas.",
            "Las plantillas creadas son páginas reutilizables y pueden usarse como template_id más adelante.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap admin structure in Notion for OpenCEO.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inventory", help="List what the integration can currently access.")

    bootstrap = subparsers.add_parser("bootstrap", help="Create the OpenCEO structure under a shared page.")
    bootstrap.add_argument("--parent-page-id", required=True, help="Shared parent page id.")
    bootstrap.add_argument("--hub-title", default="OpenCEO OS", help="Top-level hub title to create.")
    bootstrap.add_argument(
        "--cleanup-defaults",
        action="store_true",
        help="Delete direct starter children under the parent page if their titles match known Notion defaults.",
    )

    args = parser.parse_args()
    token = os.environ.get("NOTION_API_TOKEN", "").strip()
    if not token:
        print(json.dumps({"ok": False, "error": "Missing NOTION_API_TOKEN"}, ensure_ascii=False))
        return 1

    client = NotionAdminClient(token)

    if args.command == "inventory":
        print(json.dumps({"ok": True, "inventory": _inventory(client)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "bootstrap":
        result = _bootstrap_workspace(
            client,
            parent_page_id=args.parent_page_id,
            hub_title=args.hub_title,
            cleanup_defaults=bool(args.cleanup_defaults),
        )
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0

    print(json.dumps({"ok": False, "error": "Unsupported command"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
