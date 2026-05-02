from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from settings import Settings

logger = logging.getLogger(__name__)

_NOTION_API_BASE_URL = "https://api.notion.com/v1"
_DEFAULT_NOTION_VERSION = "2026-03-11"
_SUPPORTED_PARENT_KINDS = {"data_source", "database", "page"}
_SUPPORTED_PROPERTY_TYPES = {
    "title",
    "rich_text",
    "number",
    "select",
    "multi_select",
    "status",
    "date",
    "checkbox",
    "url",
    "email",
    "phone_number",
    "files",
}
_CANONICAL_TICKET_FIELDS = {
    "description",
    "priority",
    "status",
    "project",
    "ticket_type",
    "source",
    "source_id",
    "tags",
    "due_date",
    "external_url",
}
_MAX_RICH_TEXT_CHARS = 1900
_MAX_BLOCKS_PER_REQUEST = 100

_SYNC_BACK_FIELDS = {
    "runtime_status",
    "runtime_summary",
    "last_sync_at",
    "openceo_ref",
    "agent",
    "approval_required",
    "approval_state",
}
_DEFAULT_SYNC_BACK_PROPERTY_MAP = {
    "runtime_status": "Runtime Status",
    "runtime_summary": "Runtime Summary",
    "last_sync_at": "Last Sync At",
    "openceo_ref": "OpenCEO Ref",
    "agent": "Agent",
    "approval_required": "Needs Approval",
    "approval_state": "Approval State",
}


@dataclass(frozen=True)
class NotionPropertyMatch:
    id: str
    name: str
    type: str


@dataclass(frozen=True)
class NotionParentContext:
    parent: dict[str, str]
    parent_kind: str
    parent_id: str
    data_source_id: str | None
    title_property: NotionPropertyMatch
    properties: dict[str, NotionPropertyMatch]


class NotionTicketManager:
    """Shared Notion helper focused on ticket creation."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token = settings.NOTION_API_TOKEN.strip()
        self._base_url = settings.NOTION_API_BASE_URL.strip() or _NOTION_API_BASE_URL
        self._version = settings.NOTION_API_VERSION.strip() or _DEFAULT_NOTION_VERSION
        self._timeout = httpx.Timeout(20.0, connect=10.0)
        self._database_cache: dict[str, dict[str, Any]] = {}
        self._data_source_cache: dict[str, dict[str, Any]] = {}
        self._http: httpx.Client | None = None

    def _get_http(self) -> httpx.Client:
        if self._http is None or self._http.is_closed:
            self._http = httpx.Client(
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Notion-Version": self._version,
                },
            )
        return self._http

    def close(self) -> None:
        if self._http is not None and not self._http.is_closed:
            self._http.close()
            self._http = None

    def create_ticket(
        self,
        *,
        title: str,
        description: str = "",
        project: str | None = None,
        priority: str | None = None,
        status: str | None = None,
        ticket_type: str | None = None,
        source: str | None = None,
        source_id: str | None = None,
        tags: list[str] | None = None,
        due_date: str | None = None,
        external_url: str | None = None,
        # Sync-back fields included in initial creation
        runtime_status: str | None = None,
        runtime_summary: str | None = None,
        last_sync_at: str | None = None,
        openceo_ref: str | None = None,
        agent: str | None = None,
        approval_required: bool | None = None,
        approval_state: str | None = None,
    ) -> dict[str, Any]:
        normalized_title = " ".join(title.split()).strip()
        if not normalized_title:
            return {
                "ok": False,
                "status": "invalid_input",
                "message": "El ticket de Notion requiere un titulo no vacio.",
            }

        token_error = self._token_error()
        if token_error is not None:
            return token_error

        try:
            parent = self._resolve_parent_context()
            ticket_fields = self._normalize_ticket_fields(
                description=description,
                project=project,
                priority=priority,
                status=status,
                ticket_type=ticket_type,
                source=source,
                source_id=source_id,
                tags=tags,
                due_date=due_date,
                external_url=external_url,
            )
            property_map = self._resolve_property_map(parent.properties) if parent.parent_kind != "page" else {}
            self._ensure_dynamic_property_options(
                parent=parent,
                property_map=property_map,
                ticket_fields=ticket_fields,
            )
            properties = self._build_ticket_properties(
                title=normalized_title,
                ticket_fields=ticket_fields,
                parent=parent,
                property_map=property_map,
            )
            body_blocks = self._build_ticket_body(
                description=ticket_fields.get("description"),
                ticket_fields=ticket_fields,
                mapped_fields=set(property_map.keys()),
            )
            # Merge sync-back properties (Agent, Runtime Status, etc.) into
            # the initial creation so they appear immediately in Notion.
            sync_back_values = {
                "runtime_status": runtime_status,
                "runtime_summary": runtime_summary[:_MAX_RICH_TEXT_CHARS] if runtime_summary else runtime_summary,
                "last_sync_at": last_sync_at,
                "openceo_ref": openceo_ref,
                "agent": agent,
                "approval_required": approval_required,
                "approval_state": approval_state,
            }
            if any(v is not None for v in sync_back_values.values()):
                sync_map = self._resolve_sync_back_property_map(parent.properties)
                self._ensure_sync_back_property_options(
                    parent=parent,
                    sync_map=sync_map,
                    field_values=sync_back_values,
                )
                for field_name, prop in sync_map.items():
                    raw_value = sync_back_values.get(field_name)
                    if raw_value is None:
                        continue
                    try:
                        properties[prop.name] = self._encode_property_value(prop, raw_value)
                    except ValueError:
                        pass
        except ValueError as exc:
            return {
                "ok": False,
                "status": "config_error",
                "message": str(exc),
            }

        payload: dict[str, Any] = {
            "parent": parent.parent,
            "properties": properties,
        }
        template_payload = self._build_template_payload()
        if template_payload is not None and parent.parent_kind == "page":
            return {
                "ok": False,
                "status": "config_error",
                "message": "Las plantillas de Notion para tickets requieren parent de tipo data_source o database.",
            }
        if template_payload is not None:
            payload["template"] = template_payload
        elif body_blocks:
            payload["children"] = body_blocks[:_MAX_BLOCKS_PER_REQUEST]

        try:
            created = self._request("POST", "/pages", json=payload)
            page_id = str(created["id"])
            appended_blocks = 0
            if template_payload is not None and body_blocks:
                appended_blocks = self._append_blocks(page_id, body_blocks)
            else:
                appended_blocks = min(len(body_blocks), _MAX_BLOCKS_PER_REQUEST)
                remaining_blocks = body_blocks[_MAX_BLOCKS_PER_REQUEST:]
                if remaining_blocks:
                    appended_blocks += self._append_blocks(page_id, remaining_blocks)
        except ValueError as exc:
            return {
                "ok": False,
                "status": "api_error",
                "message": str(exc),
            }

        return {
            "ok": True,
            "status": "created",
            "page_id": created.get("id"),
            "url": created.get("url"),
            "parent_kind": parent.parent_kind,
            "parent_id": parent.parent_id,
            "data_source_id": parent.data_source_id,
            "title": normalized_title,
            "appended_blocks": appended_blocks,
            "applied_properties": sorted(properties.keys()),
            "message": "Ticket creado en Notion.",
        }

    def create_tickets_batch(
        self,
        *,
        tickets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple tickets in one call. Each item needs at least 'title'."""
        if not tickets:
            return {"ok": False, "status": "invalid_input", "message": "Lista vacia."}
        if len(tickets) > 50:
            return {"ok": False, "status": "invalid_input", "message": "Maximo 50 tickets por lote."}

        results: list[dict[str, Any]] = []
        created_count = 0
        failed_count = 0

        for idx, item in enumerate(tickets):
            if not isinstance(item, dict):
                results.append({"index": idx, "ok": False, "error": "Formato invalido"})
                failed_count += 1
                continue

            title = str(item.get("title") or "").strip()
            if not title:
                results.append({"index": idx, "ok": False, "error": "Sin titulo"})
                failed_count += 1
                continue

            result = self.create_ticket(
                title=title,
                description=str(item.get("description") or ""),
                project=item.get("project"),
                priority=item.get("priority"),
                status=item.get("status"),
                ticket_type=item.get("ticket_type"),
                source=item.get("source"),
                source_id=item.get("source_id"),
                tags=item.get("tags"),
                due_date=item.get("due_date"),
                external_url=item.get("external_url"),
            )
            results.append({
                "index": idx,
                "ok": result.get("ok", False),
                "title": title,
                "page_id": result.get("page_id"),
                "url": result.get("url"),
                "error": result.get("message") if not result.get("ok") else None,
            })
            if result.get("ok"):
                created_count += 1
            else:
                failed_count += 1

        return {
            "ok": failed_count == 0,
            "status": "batch_done",
            "total": len(tickets),
            "created": created_count,
            "failed": failed_count,
            "results": results,
        }

    def describe_tickets_target(self) -> dict[str, Any]:
        token_error = self._token_error()
        if token_error is not None:
            return token_error

        try:
            parent = self._resolve_parent_context()
            property_map = self._resolve_property_map(parent.properties)
        except ValueError as exc:
            return {
                "ok": False,
                "status": "config_error",
                "message": str(exc),
            }

        return {
            "ok": True,
            "status": "ready",
            "parent_kind": parent.parent_kind,
            "parent_id": parent.parent_id,
            "data_source_id": parent.data_source_id,
            "title_property": {
                "id": parent.title_property.id,
                "name": parent.title_property.name,
                "type": parent.title_property.type,
            },
            "property_map": {
                field: {
                    "id": prop.id,
                    "name": prop.name,
                    "type": prop.type,
                }
                for field, prop in sorted(property_map.items())
            },
            "template_mode": self._template_mode(),
            "append_description_to_body": self._settings.NOTION_TICKETS_APPEND_DESCRIPTION_TO_BODY,
            "message": "Configuracion de tickets de Notion resuelta correctamente.",
        }

    def update_page_properties(
        self,
        *,
        page_id: str,
        runtime_status: str | None = None,
        runtime_summary: str | None = None,
        last_sync_at: str | None = None,
        openceo_ref: str | None = None,
        agent: str | None = None,
        approval_required: bool | None = None,
        approval_state: str | None = None,
    ) -> dict[str, Any]:
        token_error = self._token_error()
        if token_error is not None:
            return token_error

        try:
            parent = self._resolve_parent_context()
        except ValueError as exc:
            return {"ok": False, "status": "config_error", "message": str(exc)}

        sync_map = self._resolve_sync_back_property_map(parent.properties)
        if not sync_map:
            logger.warning("No sync-back properties resolved — skipping Notion update for page %s", page_id)
            return {"ok": False, "status": "no_sync_properties", "message": "No sync-back properties found."}

        field_values: dict[str, Any] = {
            "runtime_status": runtime_status,
            "runtime_summary": runtime_summary[:_MAX_RICH_TEXT_CHARS] if runtime_summary else runtime_summary,
            "last_sync_at": last_sync_at,
            "openceo_ref": openceo_ref,
            "agent": agent,
            "approval_required": approval_required,
            "approval_state": approval_state,
        }
        self._ensure_sync_back_property_options(
            parent=parent,
            sync_map=sync_map,
            field_values=field_values,
        )

        properties: dict[str, Any] = {}
        for field_name, prop in sync_map.items():
            raw_value = field_values.get(field_name)
            if raw_value is None:
                continue
            try:
                properties[prop.name] = self._encode_property_value(prop, raw_value)
            except ValueError:
                logger.warning("Skipping sync-back field '%s': encode error", field_name)

        if not properties:
            return {"ok": True, "status": "noop", "message": "No properties to update."}

        try:
            self._request("PATCH", f"/pages/{page_id}", json={"properties": properties})
        except ValueError as exc:
            return {"ok": False, "status": "api_error", "message": str(exc)}

        return {
            "ok": True,
            "status": "updated",
            "page_id": page_id,
            "updated_properties": sorted(properties.keys()),
        }

    def _ensure_sync_back_property_options(
        self,
        *,
        parent: NotionParentContext,
        sync_map: dict[str, NotionPropertyMatch],
        field_values: dict[str, Any],
    ) -> None:
        if parent.data_source_id is None:
            return

        for field_name, prop in sync_map.items():
            raw_value = field_values.get(field_name)
            if raw_value in (None, "", []):
                continue
            if prop.type != "select":
                continue
            self._ensure_property_options(
                parent.data_source_id,
                prop,
                [self._coerce_single_text(raw_value, prop.name)],
            )

    def list_tickets_by_source_id(self, source_id: str) -> dict[str, Any]:
        token_error = self._token_error()
        if token_error is not None:
            return token_error

        normalized = str(source_id or "").strip()
        if not normalized:
            return {"ok": False, "status": "invalid_input", "message": "source_id vacio."}

        try:
            parent = self._resolve_parent_context()
            if parent.data_source_id is None:
                raise ValueError("La configuracion de Notion no expone un data source consultable.")
            source_property_name = self._property_map().get("source_id")
            if not source_property_name:
                raise ValueError("La propiedad source_id no esta mapeada en Notion.")
            source_prop = self._match_property(parent.properties, source_property_name)
            if source_prop is None:
                raise ValueError("La propiedad source_id no existe en el data source configurado.")
            filter_payload = self._build_query_filter(source_prop, normalized)
            query = self._request(
                "POST",
                f"/data_sources/{parent.data_source_id}/query",
                json={"filter": filter_payload},
            )
        except ValueError as exc:
            return {"ok": False, "status": "config_error", "message": str(exc)}

        pages = query.get("results") or []
        if not isinstance(pages, list):
            pages = []

        tickets = [self._page_summary(page, parent.title_property) for page in pages if isinstance(page, dict)]
        return {
            "ok": True,
            "status": "listed",
            "source_id": normalized,
            "count": len(tickets),
            "tickets": tickets,
        }

    def _sync_back_property_map(self) -> dict[str, str]:
        raw = self._settings.NOTION_SYNC_BACK_PROPERTY_MAP_JSON.strip()
        if not raw:
            return dict(_DEFAULT_SYNC_BACK_PROPERTY_MAP)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("NOTION_SYNC_BACK_PROPERTY_MAP_JSON is invalid JSON — using defaults")
            return dict(_DEFAULT_SYNC_BACK_PROPERTY_MAP)
        if not isinstance(payload, dict):
            logger.warning("NOTION_SYNC_BACK_PROPERTY_MAP_JSON is not an object — using defaults")
            return dict(_DEFAULT_SYNC_BACK_PROPERTY_MAP)
        return {str(k).strip(): str(v).strip() for k, v in payload.items() if str(k).strip() in _SYNC_BACK_FIELDS}

    def _resolve_sync_back_property_map(
        self,
        properties: dict[str, NotionPropertyMatch],
    ) -> dict[str, NotionPropertyMatch]:
        resolved: dict[str, NotionPropertyMatch] = {}
        for field_name, property_name in self._sync_back_property_map().items():
            matched = self._match_property(properties, property_name)
            if matched is None:
                logger.warning("Sync-back property '%s' (%s) not found in Notion — skipping", field_name, property_name)
                continue
            resolved[field_name] = matched
        return resolved

    def _resolve_parent_context(self) -> NotionParentContext:
        parent_kind = self._settings.NOTION_TICKETS_PARENT_KIND.strip().lower() or "data_source"
        if parent_kind not in _SUPPORTED_PARENT_KINDS:
            raise ValueError(
                "NOTION_TICKETS_PARENT_KIND debe ser data_source, database o page."
            )

        parent_id = self._settings.NOTION_TICKETS_PARENT_ID.strip()
        if not parent_id:
            raise ValueError("Falta NOTION_TICKETS_PARENT_ID para crear tickets en Notion.")

        if parent_kind == "page":
            title_property = NotionPropertyMatch(id="title", name="title", type="title")
            return NotionParentContext(
                parent={"page_id": parent_id},
                parent_kind="page",
                parent_id=parent_id,
                data_source_id=None,
                title_property=title_property,
                properties={title_property.name.lower(): title_property},
            )

        data_source_id = parent_id if parent_kind == "data_source" else self._resolve_data_source_id(parent_id)
        data_source = self._retrieve_data_source(data_source_id)
        properties = self._parse_data_source_properties(data_source)
        title_property = self._resolve_title_property(properties)

        return NotionParentContext(
            parent={"data_source_id": data_source_id},
            parent_kind=parent_kind,
            parent_id=parent_id,
            data_source_id=data_source_id,
            title_property=title_property,
            properties=properties,
        )

    def _resolve_data_source_id(self, database_id: str) -> str:
        database = self._retrieve_database(database_id)
        data_sources = database.get("data_sources") or []
        if not isinstance(data_sources, list) or not data_sources:
            raise ValueError("La database de Notion no expone data sources accesibles para la integracion.")

        preferred_name = self._settings.NOTION_TICKETS_DATA_SOURCE_NAME.strip().lower()
        if preferred_name:
            for item in data_sources:
                if str(item.get("name", "")).strip().lower() == preferred_name:
                    return str(item["id"])
            raise ValueError(
                "NOTION_TICKETS_DATA_SOURCE_NAME no coincide con ningun data source de la database configurada."
            )

        if len(data_sources) > 1:
            raise ValueError(
                "La database de Notion tiene multiples data sources. Define NOTION_TICKETS_DATA_SOURCE_NAME."
            )

        return str(data_sources[0]["id"])

    def _resolve_title_property(
        self,
        properties: dict[str, NotionPropertyMatch],
    ) -> NotionPropertyMatch:
        configured = self._settings.NOTION_TICKETS_TITLE_PROPERTY.strip()
        if configured:
            matched = self._match_property(properties, configured)
            if matched is None:
                raise ValueError("NOTION_TICKETS_TITLE_PROPERTY no existe en el data source configurado.")
            if matched.type != "title":
                raise ValueError("NOTION_TICKETS_TITLE_PROPERTY debe apuntar a una propiedad de tipo title.")
            return matched

        for prop in properties.values():
            if prop.type == "title":
                return prop

        raise ValueError("El data source de Notion no tiene una propiedad title accesible.")

    def _build_ticket_properties(
        self,
        *,
        title: str,
        ticket_fields: dict[str, Any],
        parent: NotionParentContext,
        property_map: dict[str, NotionPropertyMatch],
    ) -> dict[str, Any]:
        properties: dict[str, Any] = {
            parent.title_property.name: self._encode_property_value(parent.title_property, title),
        }

        if parent.parent_kind == "page":
            return properties

        for field_name, prop in property_map.items():
            raw_value = ticket_fields.get(field_name)
            if raw_value in (None, "", []):
                continue
            properties[prop.name] = self._encode_property_value(prop, raw_value)

        return properties

    def _resolve_property_map(
        self,
        properties: dict[str, NotionPropertyMatch],
    ) -> dict[str, NotionPropertyMatch]:
        resolved: dict[str, NotionPropertyMatch] = {}
        for field_name, property_name in self._property_map().items():
            matched = self._match_property(properties, property_name)
            if matched is None:
                raise ValueError(
                    f"La propiedad configurada para '{field_name}' no existe en el data source de Notion."
                )
            if matched.type not in _SUPPORTED_PROPERTY_TYPES:
                raise ValueError(
                    f"La propiedad '{matched.name}' usa un tipo no soportado para escritura: {matched.type}."
                )
            resolved[field_name] = matched
        return resolved

    def _ensure_dynamic_property_options(
        self,
        *,
        parent: NotionParentContext,
        property_map: dict[str, NotionPropertyMatch],
        ticket_fields: dict[str, Any],
    ) -> None:
        if parent.data_source_id is None:
            return

        dynamic_fields = {
            "project": ticket_fields.get("project"),
            "tags": ticket_fields.get("tags"),
        }
        for field_name, raw_value in dynamic_fields.items():
            if raw_value in (None, "", []):
                continue
            prop = property_map.get(field_name)
            if prop is None or prop.type not in {"select", "multi_select"}:
                continue
            if prop.type == "select":
                values = [self._coerce_single_text(raw_value, prop.name)]
            else:
                values = self._coerce_text_list(raw_value)
            self._ensure_property_options(parent.data_source_id, prop, values)

    def _ensure_property_options(
        self,
        data_source_id: str,
        prop: NotionPropertyMatch,
        values: list[str],
    ) -> None:
        requested = [value.strip() for value in values if str(value).strip()]
        if not requested:
            return

        data_source = self._retrieve_data_source(data_source_id)
        raw_prop = self._find_raw_property(data_source, prop)
        if raw_prop is None:
            raise ValueError(f"No se pudo ubicar la propiedad '{prop.name}' en el data source de Notion.")

        prop_payload = raw_prop.get(prop.type)
        if not isinstance(prop_payload, dict):
            return
        raw_options = prop_payload.get("options")
        if not isinstance(raw_options, list):
            return

        existing = {
            str(option.get("name") or "").strip().lower()
            for option in raw_options
            if isinstance(option, dict)
        }
        missing = [value for value in requested if value.lower() not in existing]
        if not missing:
            return

        updated_options = [dict(option) for option in raw_options if isinstance(option, dict)]
        updated_options.extend(
            {
                "name": value,
                "color": self._default_option_color(prop.name, value),
            }
            for value in missing
        )

        logger.info(
            "Notion schema auto-update: agregando %s=%s en data_source=%s",
            prop.name,
            ", ".join(missing),
            data_source_id,
        )
        self._request(
            "PATCH",
            f"/data_sources/{data_source_id}",
            json={"properties": {prop.name: {prop.type: {"options": updated_options}}}},
        )
        self._data_source_cache.pop(data_source_id, None)

    def _find_raw_property(
        self,
        data_source: dict[str, Any],
        prop: NotionPropertyMatch,
    ) -> dict[str, Any] | None:
        raw_properties = data_source.get("properties")
        if not isinstance(raw_properties, dict):
            return None
        lookup_name = prop.name.strip().lower()
        lookup_id = prop.id.strip().lower()
        for key, value in raw_properties.items():
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or key).strip().lower()
            property_id = str(value.get("id") or "").strip().lower()
            if name == lookup_name or property_id == lookup_id:
                return value
        return None

    def _build_query_filter(
        self,
        prop: NotionPropertyMatch,
        raw_value: str,
    ) -> dict[str, Any]:
        if prop.type == "rich_text":
            return {"property": prop.name, "rich_text": {"equals": raw_value}}
        if prop.type == "title":
            return {"property": prop.name, "title": {"equals": raw_value}}
        if prop.type == "select":
            return {"property": prop.name, "select": {"equals": raw_value}}
        raise ValueError(f"No se puede filtrar por source_id usando una propiedad de tipo {prop.type}.")

    def _page_summary(
        self,
        page: dict[str, Any],
        title_property: NotionPropertyMatch,
    ) -> dict[str, Any]:
        title = "?"
        raw_properties = page.get("properties")
        if isinstance(raw_properties, dict):
            raw_title = raw_properties.get(title_property.name)
            if isinstance(raw_title, dict):
                rich_items = raw_title.get(title_property.type)
                if isinstance(rich_items, list):
                    parts = [str(item.get("plain_text") or "").strip() for item in rich_items if isinstance(item, dict)]
                    rendered = "".join(parts).strip()
                    if rendered:
                        title = rendered
        return {
            "page_id": page.get("id"),
            "url": page.get("url"),
            "title": title,
            "ok": True,
        }

    def _default_option_color(self, property_name: str, option_name: str) -> str:
        normalized_property = property_name.strip().lower()
        if normalized_property == "project":
            palette = ["blue", "green", "orange", "purple", "pink", "yellow", "gray", "brown"]
            stable_index = sum(ord(ch) for ch in option_name.strip().lower()) % len(palette)
            return palette[stable_index]
        return "default"

    def _encode_property_value(self, prop: NotionPropertyMatch, raw_value: Any) -> dict[str, Any]:
        if prop.type == "title":
            text = self._coerce_single_text(raw_value, prop.name)
            return {"title": self._rich_text_array(text)}
        if prop.type == "rich_text":
            text = self._coerce_single_text(raw_value, prop.name)
            return {"rich_text": self._rich_text_array(text)}
        if prop.type == "number":
            return {"number": self._coerce_number(raw_value, prop.name)}
        if prop.type == "select":
            return {"select": {"name": self._coerce_single_text(raw_value, prop.name)}}
        if prop.type == "status":
            return {"status": {"name": self._coerce_single_text(raw_value, prop.name)}}
        if prop.type == "multi_select":
            values = self._coerce_text_list(raw_value)
            return {"multi_select": [{"name": item} for item in values]}
        if prop.type == "date":
            return {"date": self._coerce_date(raw_value, prop.name)}
        if prop.type == "checkbox":
            return {"checkbox": self._coerce_bool(raw_value)}
        if prop.type == "url":
            return {"url": self._coerce_single_text(raw_value, prop.name)}
        if prop.type == "email":
            return {"email": self._coerce_single_text(raw_value, prop.name)}
        if prop.type == "phone_number":
            return {"phone_number": self._coerce_single_text(raw_value, prop.name)}
        if prop.type == "files":
            values = self._coerce_text_list(raw_value)
            return {
                "files": [
                    {
                        "name": self._name_from_url(item),
                        "type": "external",
                        "external": {"url": item},
                    }
                    for item in values
                ]
            }
        raise ValueError(f"Tipo de propiedad de Notion no soportado: {prop.type}")

    def _build_ticket_body(
        self,
        *,
        description: Any,
        ticket_fields: dict[str, Any],
        mapped_fields: set[str],
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if self._settings.NOTION_TICKETS_APPEND_DESCRIPTION_TO_BODY:
            blocks.extend(self._paragraph_blocks(str(description or "")))

        context_lines: list[str] = []
        for field_name in sorted(ticket_fields):
            if field_name == "description":
                continue
            if field_name in mapped_fields:
                continue
            value = ticket_fields[field_name]
            if value in (None, "", []):
                continue
            label = field_name.replace("_", " ").capitalize()
            if isinstance(value, list):
                rendered = ", ".join(str(item) for item in value)
            else:
                rendered = str(value)
            context_lines.append(f"{label}: {rendered}")

        if context_lines:
            if blocks:
                blocks.append(self._divider_block())
            blocks.append(self._heading_block("Contexto OpenCEO"))
            blocks.extend(self._bulleted_list_blocks(context_lines))

        return blocks

    def _build_template_payload(self) -> dict[str, str] | None:
        template_id = self._settings.NOTION_TICKETS_TEMPLATE_ID.strip()
        if template_id:
            return {"type": "template_id", "template_id": template_id}
        if self._settings.NOTION_TICKETS_USE_DEFAULT_TEMPLATE:
            return {"type": "default"}
        return None

    def _template_mode(self) -> str:
        template_id = self._settings.NOTION_TICKETS_TEMPLATE_ID.strip()
        if template_id:
            return "template_id"
        if self._settings.NOTION_TICKETS_USE_DEFAULT_TEMPLATE:
            return "default"
        return "none"

    def _normalize_ticket_fields(self, **ticket_fields: Any) -> dict[str, Any]:
        normalized: dict[str, Any] = {
            "description": self._normalize_multiline_text(ticket_fields.get("description")),
            "priority": ticket_fields.get("priority") or self._settings.NOTION_TICKETS_DEFAULT_PRIORITY.strip(),
            "status": ticket_fields.get("status") or self._settings.NOTION_TICKETS_DEFAULT_STATUS.strip(),
            "project": ticket_fields.get("project"),
            "ticket_type": ticket_fields.get("ticket_type"),
            "source": ticket_fields.get("source") or self._settings.NOTION_TICKETS_DEFAULT_SOURCE.strip(),
            "source_id": ticket_fields.get("source_id"),
            "tags": self._coerce_text_list(ticket_fields.get("tags") or []),
            "due_date": ticket_fields.get("due_date"),
            "external_url": ticket_fields.get("external_url"),
        }
        return {key: value for key, value in normalized.items() if key in _CANONICAL_TICKET_FIELDS}

    def _property_map(self) -> dict[str, str]:
        raw = self._settings.NOTION_TICKETS_PROPERTY_MAP_JSON.strip()
        if not raw:
            return {}

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("NOTION_TICKETS_PROPERTY_MAP_JSON no es JSON valido.") from exc

        if not isinstance(payload, dict):
            raise ValueError("NOTION_TICKETS_PROPERTY_MAP_JSON debe ser un objeto JSON.")

        normalized: dict[str, str] = {}
        for raw_field, raw_property in payload.items():
            field_name = str(raw_field).strip().lower()
            if field_name not in _CANONICAL_TICKET_FIELDS:
                raise ValueError(
                    f"Campo de ticket no soportado en NOTION_TICKETS_PROPERTY_MAP_JSON: {raw_field}"
                )
            property_name = str(raw_property).strip()
            if not property_name:
                raise ValueError(f"Propiedad vacia para el campo '{raw_field}' en Notion.")
            normalized[field_name] = property_name
        return normalized

    def _match_property(
        self,
        properties: dict[str, NotionPropertyMatch],
        name_or_id: str,
    ) -> NotionPropertyMatch | None:
        lookup = name_or_id.strip().lower()
        for prop in properties.values():
            if prop.name.strip().lower() == lookup or prop.id.strip().lower() == lookup:
                return prop
        return None

    def _parse_data_source_properties(self, payload: dict[str, Any]) -> dict[str, NotionPropertyMatch]:
        raw_properties = payload.get("properties")
        if not isinstance(raw_properties, dict) or not raw_properties:
            raise ValueError("El data source de Notion no devolvio propiedades accesibles.")

        properties: dict[str, NotionPropertyMatch] = {}
        for key, value in raw_properties.items():
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or key).strip()
            property_id = str(value.get("id") or name).strip()
            property_type = str(value.get("type") or "").strip()
            if not name or not property_type:
                continue
            properties[name.lower()] = NotionPropertyMatch(
                id=property_id,
                name=name,
                type=property_type,
            )

        if not properties:
            raise ValueError("No se pudieron resolver propiedades del data source de Notion.")

        return properties

    def _append_blocks(self, page_id: str, blocks: list[dict[str, Any]]) -> int:
        appended = 0
        for index in range(0, len(blocks), _MAX_BLOCKS_PER_REQUEST):
            chunk = blocks[index : index + _MAX_BLOCKS_PER_REQUEST]
            if not chunk:
                continue
            self._request(
                "PATCH",
                f"/blocks/{page_id}/children",
                json={"children": chunk},
            )
            appended += len(chunk)
        return appended

    def _retrieve_database(self, database_id: str) -> dict[str, Any]:
        if database_id not in self._database_cache:
            self._database_cache[database_id] = self._request("GET", f"/databases/{database_id}")
        return self._database_cache[database_id]

    def _retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        if data_source_id not in self._data_source_cache:
            self._data_source_cache[data_source_id] = self._request("GET", f"/data_sources/{data_source_id}")
        return self._data_source_cache[data_source_id]

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url.rstrip('/')}{path}"
        retry_delay_seconds = 1.0
        client = self._get_http()

        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                response = client.request(method, url, params=params, json=json)
            except httpx.HTTPError as exc:
                raise ValueError(f"Error de red llamando a Notion: {exc}") from exc

            if response.status_code == 429 and attempt < max_attempts - 1:
                retry_after = response.headers.get("Retry-After")
                wait_seconds = float(retry_after) if retry_after else retry_delay_seconds
                wait_seconds = max(wait_seconds, retry_delay_seconds) + random.uniform(0, 1.0)
                time.sleep(wait_seconds)
                retry_delay_seconds *= 2
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(self._format_error(response)) from exc

            try:
                return response.json()
            except ValueError as exc:
                raise ValueError("Notion devolvio una respuesta JSON invalida.") from exc

        raise ValueError("Notion no acepto la solicitud despues de varios reintentos.")

    def _format_error(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return f"Notion devolvio HTTP {response.status_code}."

        code = str(payload.get("code") or "").strip()
        message = str(payload.get("message") or "").strip()
        parts = [part for part in [code, message] if part]
        if not parts:
            parts.append(f"HTTP {response.status_code}")
        return f"Error de Notion: {' - '.join(parts)}"

    def _token_error(self) -> dict[str, Any] | None:
        if self._token:
            return None
        return {
            "ok": False,
            "status": "missing_config",
            "message": "Falta NOTION_API_TOKEN en la configuracion.",
        }

    def _coerce_single_text(self, raw_value: Any, property_name: str) -> str:
        if raw_value is None:
            raise ValueError(f"La propiedad '{property_name}' requiere texto.")
        text = str(raw_value).strip()
        if not text:
            raise ValueError(f"La propiedad '{property_name}' requiere texto no vacio.")
        return text

    def _normalize_multiline_text(self, raw_value: Any) -> str:
        if raw_value is None:
            return ""
        lines = [str(line).strip() for line in str(raw_value).splitlines()]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned.strip()

    def _coerce_text_list(self, raw_value: Any) -> list[str]:
        if raw_value in (None, "", []):
            return []
        if isinstance(raw_value, str):
            candidates = [item.strip() for item in raw_value.split(",")]
        elif isinstance(raw_value, list):
            candidates = [str(item).strip() for item in raw_value]
        else:
            raise ValueError("Se esperaba texto o lista de textos para una propiedad de lista.")
        return [item for item in candidates if item]

    def _coerce_number(self, raw_value: Any, property_name: str) -> int | float:
        if isinstance(raw_value, bool):
            raise ValueError(f"La propiedad '{property_name}' espera numero, no booleano.")
        if isinstance(raw_value, (int, float)):
            return raw_value
        value = str(raw_value).strip()
        if not value:
            raise ValueError(f"La propiedad '{property_name}' requiere un numero.")
        try:
            return int(value) if value.isdigit() else float(value)
        except ValueError as exc:
            raise ValueError(f"La propiedad '{property_name}' requiere un numero valido.") from exc

    def _coerce_date(self, raw_value: Any, property_name: str) -> dict[str, str]:
        if isinstance(raw_value, datetime):
            return {"start": raw_value.isoformat()}
        if isinstance(raw_value, date):
            return {"start": raw_value.isoformat()}

        value = str(raw_value).strip()
        if not value:
            raise ValueError(f"La propiedad '{property_name}' requiere una fecha no vacia.")

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return {"start": parsed.isoformat()}
        except ValueError:
            try:
                parsed_date = date.fromisoformat(value)
                return {"start": parsed_date.isoformat()}
            except ValueError as exc:
                raise ValueError(
                    f"La propiedad '{property_name}' requiere fecha ISO 8601 valida."
                ) from exc

    def _coerce_bool(self, raw_value: Any) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        value = str(raw_value).strip().lower()
        if value in {"true", "1", "yes", "si", "sí"}:
            return True
        if value in {"false", "0", "no"}:
            return False
        raise ValueError("Se esperaba un booleano valido para una propiedad checkbox.")

    def _rich_text_array(self, text: str) -> list[dict[str, Any]]:
        chunks = self._chunk_text(text)
        return [self._text_rich_object(chunk) for chunk in chunks]

    def _paragraph_blocks(self, text: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for paragraph in self._split_paragraphs(text):
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": self._rich_text_array(paragraph)},
                }
            )
        return blocks

    def _bulleted_list_blocks(self, lines: list[str]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for line in lines:
            blocks.append(
                {
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": self._rich_text_array(line)},
                }
            )
        return blocks

    def _heading_block(self, text: str) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": self._rich_text_array(text),
                "is_toggleable": False,
                "color": "default",
            },
        }

    def _divider_block(self) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "divider",
            "divider": {},
        }

    def _split_paragraphs(self, text: str) -> list[str]:
        stripped = text.strip()
        if not stripped:
            return []

        paragraphs: list[str] = []
        for paragraph in stripped.splitlines():
            value = paragraph.strip()
            if not value:
                continue
            paragraphs.extend(self._chunk_text(value))
        return paragraphs

    def _chunk_text(self, text: str) -> list[str]:
        stripped = text.strip()
        if not stripped:
            return []

        chunks: list[str] = []
        remaining = stripped
        while remaining:
            if len(remaining) <= _MAX_RICH_TEXT_CHARS:
                chunks.append(remaining)
                break

            split_at = remaining.rfind(" ", 0, _MAX_RICH_TEXT_CHARS)
            if split_at <= 0:
                split_at = _MAX_RICH_TEXT_CHARS
            chunks.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].strip()
        return [chunk for chunk in chunks if chunk]

    def _text_rich_object(self, text: str) -> dict[str, Any]:
        return {
            "type": "text",
            "text": {
                "content": text,
            },
        }

    def _name_from_url(self, value: str) -> str:
        parsed = urlparse(value)
        filename = parsed.path.rsplit("/", 1)[-1].strip()
        if filename:
            return filename[:128]
        return parsed.netloc[:128] or "external-file"
