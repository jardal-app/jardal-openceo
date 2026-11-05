from __future__ import annotations

import hashlib
import json
import base64
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from business.http import JardalERPClient
from business.files import read_attachment
from business.contracts import WorkTicketV1
from business.errors import BusinessUnavailable, BusinessValidationError, BusinessVersionConflict
from owner_channel import owner_question_block
from tools.jardal_erp_tickets import map_priority, normalize_transition_status, resolve_jardal_erp_status


def operation_key(operation: str, *values: Any) -> str:
    serialized = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return operation + ":" + hashlib.sha256(serialized.encode()).hexdigest()


def task_view(row: dict[str, Any], detail: dict[str, Any] | None = None, *, zone: ZoneInfo = ZoneInfo("UTC")) -> dict[str, Any]:
    try:
        row = WorkTicketV1.model_validate(row).model_dump(mode="json")
    except ValidationError:
        raise BusinessUnavailable("Jardal ERP respondió con un ticket fuera del contrato v1.") from None
    def site_time(value: Any) -> str | None:
        if not value:
            return None
        try:
            date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise BusinessUnavailable("Fecha fuera del contrato v1.") from None
        if date.tzinfo is None:
            raise BusinessUnavailable("El ERP debe devolver fechas con zona horaria.")
        return date.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")

    execution = row.get("execution") or {}
    result = {
        **{key: value for key, value in row.items() if key != "registration"},
        "name": row["businessReference"],
        "business_ticket_id": row["id"],
        "provider": "jardal_erp",
        "subject": row["title"],
        "project": row["projectKey"],
        "is_group": row.get("kind") == "epic",
        "parent_task": row.get("parentReference"),
        "openceo_ref": execution.get("ref"),
        "openceo_task_id": execution.get("taskId"),
        "openceo_ticket_number": execution.get("ticketNumber", 0),
        "openceo_coding_tool": execution.get("codingTool"),
        "exp_start_date": site_time(row.get("actualStart") if row["status"] in {"Working", "Pending Review", "Completed", "Cancelled"} else row.get("plannedStart")),
        "exp_end_date": site_time(row.get("actualEnd") if row["status"] in {"Completed", "Cancelled"} else row.get("plannedEnd")),
        "creation": site_time(row.get("createdAt")),
        "completed_on": (site_time(row.get("actualEnd")) or "")[:10] or None,
        "progress": row.get("progressPercent"),
    }
    if detail is not None:
        result["depends_on"] = [{"task": d["businessReference"]} for d in detail.get("prerequisites", [])]
    return result


class JardalERPWork:
    provider = "jardal_erp"
    owns_visibility = False
    legacy_date_repairs = False

    def __init__(self, settings: Any, *, client: JardalERPClient | None = None) -> None:
        self.client = client or JardalERPClient(settings)
        self._settings = settings
        self._zone = ZoneInfo(str(getattr(settings, "JARDAL_ERP_SITE_TIMEZONE", "UTC")))

    def _view(self, row: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return task_view(row, detail, zone=self._zone)

    def rollup_view(self, task: dict[str, Any]) -> dict[str, Any]:
        planned = self._view({**task, "status": "Open"})
        return {**task, "exp_start_date": planned["exp_start_date"], "exp_end_date": planned["exp_end_date"]}

    @property
    def is_enabled(self) -> bool:
        return self.client.is_configured

    async def aclose(self) -> None:
        await self.client.aclose()

    async def find_project(self, project_key: str) -> str | None:
        try:
            result = await self.client.request("GET", "/api/v1/projects/" + quote(project_key, safe=""))
        except BusinessValidationError as exc:
            if exc.status_code == 404:
                return None
            raise
        return result["project"]["key"]

    async def ensure_project(self, project_key: str, display_name: str | None = None) -> str:
        result = await self.client.request("POST", "/api/v1/projects", body={
            "key": project_key, "name": display_name or project_key,
        }, key=operation_key("project", project_key, display_name or project_key))
        return result["project"]["key"]

    async def create_ticket(
        self, *, ref: str, title: str, description: str = "", project_key: str | None = None,
        priority: str | None = None, ticket_type: str | None = None, tags: list[str] | None = None,
        due_date: str | None = None, ticket_number: int | None = None, task_id: str | None = None,
        coding_tool: Any = None, external_url: str | None = None, parent_task: str | None = None,
        depends_on: list[str] | None = None, is_group: bool = False,
    ) -> dict[str, Any]:
        if not ref or not project_key or not title.strip():
            raise BusinessValidationError("ref, project_key y title son obligatorios.", status_code=422)
        body: dict[str, Any] = {
            "projectKey": project_key, "title": title, "description": description,
            "priority": map_priority(priority), "kind": "epic" if is_group else "task",
            "execution": {"ref": ref, "taskId": task_id, "ticketNumber": ticket_number or 0,
                          "codingTool": getattr(coding_tool, "value", coding_tool), "autorun": False},
            "dependsOn": list(dict.fromkeys(depends_on or [])),
            "tags": tags or [],
        }
        if parent_task:
            body["parentReference"] = parent_task
        if due_date:
            body["plannedEnd"] = due_date
        if ticket_type:
            body["ticketType"] = ticket_type
        if external_url:
            body["externalUrl"] = external_url
        try:
            result = await self.client.request("POST", "/api/v1/tasks", body=body, key=ref)
        except BusinessValidationError as exc:
            if exc.status_code != 409 or (exc.payload or {}).get("code") != "IDEMPOTENCY_CONFLICT":
                raise
            existing = (await self._detail(ref))["task"]
            registration = existing.get("registration")
            if not isinstance(registration, dict) or self._business_registration(registration) != self._business_registration(body):
                raise exc
            result = {"task": existing, "duplicate": True, "recovered": True}
        return {"ok": True, "duplicate": result.get("duplicate", False), "ref": ref,
                "recovered": result.get("recovered", False), "task": self._view(result["task"])}

    @staticmethod
    def _business_registration(body: dict[str, Any]) -> dict[str, Any]:
        result = dict(body)
        result["execution"] = {key: value for key, value in (body.get("execution") or {}).items() if key not in {"taskId", "ticketNumber"}}
        return result

    async def create_group_task(self, **kwargs: Any) -> dict[str, Any]:
        return await self.create_ticket(**kwargs, is_group=True)

    async def _detail(self, identifier: str) -> dict[str, Any]:
        if not identifier.strip():
            raise BusinessValidationError("Falta el identificador del ticket.", status_code=400)
        return await self.client.request("GET", "/api/v1/tasks/" + quote(identifier, safe=""))

    async def resolve_task(self, identifier: str) -> dict[str, Any]:
        detail = await self._detail(identifier)
        return self._view(detail["task"], detail)

    async def get_ticket(self, identifier: str) -> dict[str, Any] | None:
        try:
            return await self.resolve_task(identifier)
        except BusinessValidationError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def find_by_ref(self, ref: str) -> dict[str, Any] | None:
        return await self.get_ticket(ref)

    async def search_tickets(
        self, *, query: str | None = None, project_key: str | None = None,
        status: str | list[str] | None = None, limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        for name, value in {"search": query, "projectKey": project_key, "status": status}.items():
            if value:
                params[name] = ",".join(value) if isinstance(value, list) else value
        result = await self.client.request("GET", "/api/v1/tasks", params=params)
        return [self._view(row) for row in result["tasks"]]

    async def _task_pages(self, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        rows = []
        cursor = None
        seen = set()
        while len(rows) < limit:
            query = {**params, "limit": min(100, limit - len(rows))}
            if cursor:
                query["cursor"] = cursor
            result = await self.client.request("GET", "/api/v1/tasks", params=query)
            page = result["tasks"]
            rows.extend(self._view(row) for row in page)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in seen or not page:
                raise BusinessUnavailable("Cursor de paginación ERP sin avance.")
            seen.add(cursor)
        return rows[:limit]

    async def transition_status(
        self, identifier: str, to_status: str, *, work_dates: dict[str, Any] | None = None,
        reason: str = "Transición validada por OpenCEO", evidence: str | None = None,
        human_action: str | None = None, retry_version_conflicts: bool = False,
    ) -> dict[str, Any]:
        task = await self.resolve_task(identifier)
        target = normalize_transition_status(to_status)
        body: dict[str, Any] = {"toStatus": target, "reason": reason, "version": task["version"]}
        if evidence:
            body["evidence"] = evidence
        if human_action:
            body["humanAction"] = human_action
        if work_dates:
            for source, target_field in (("exp_start_date", "actualStart"), ("exp_end_date", "actualEnd")):
                value = work_dates.get(source)
                if value and (target == "Completed" or target == "Working" and target_field == "actualStart"):
                    date = datetime.fromisoformat(str(value))
                    body[target_field] = (date if date.tzinfo else date.replace(tzinfo=self._zone)).isoformat()
        original_status = task["status"]
        for attempt in range(3):
            if task["status"] == target:
                return {"ok": True, "ticket": task, "duplicate": True}
            body["version"] = task["version"]
            key = operation_key("transition", task["name"], body)
            try:
                result = await self.client.request("POST", self._path(task) + "/transitions", body=body, key=key)
                view = self._view(result["task"])
                return {"ok": True, "ticket": view}
            except BusinessVersionConflict:
                if not retry_version_conflicts or attempt == 2:
                    raise
                task = await self.resolve_task(identifier)
                if task["status"] not in {original_status, target}:
                    raise
        raise BusinessVersionConflict("Conflicto de versión.", status_code=409)

    async def update_status(
        self, name: str, opencero_status: str | None, *, task: dict[str, Any] | None = None,
        work_dates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        target = resolve_jardal_erp_status({**(task or {}), "status": opencero_status})
        if target is None:
            return None
        context = (task or {}).get("params") or {}
        human_action = context.get("human_action")
        if target == "Pending Review":
            if (task or {}).get("qa_review_required"):
                human_action = f"Revisar y aprobar la QA de {name}."
            elif owner_question_block(task):
                question = owner_question_block(task)
                human_action = str(question.get("question") or question.get("text") or (task or {}).get("description") or "").strip()
            else:
                human_action = human_action or (task or {}).get("waiting_reason")
            if not human_action:
                raise BusinessValidationError("Falta una acción humana concreta para Pending Review.", status_code=422)
        evidence = str((task or {}).get("last_response_summary") or "").strip()
        if target == "Completed" and task:
            evidence = f"Core execution {task.get('task_id')}: completed; " + (evidence or "gates de ejecución y QA satisfechos")
        return await self.transition_status(
            name, target, reason="Estado de ejecución validado por OpenCEO",
            evidence=evidence or None, human_action=human_action, retry_version_conflicts=True,
            work_dates=work_dates,
        )

    @staticmethod
    def _path(task: dict[str, Any]) -> str:
        return "/api/v1/tasks/" + quote(task["business_ticket_id"], safe="")

    async def _patch(self, identifier: str, changes: dict[str, Any], *, current: dict[str, Any] | None = None) -> dict[str, Any]:
        task = current if current is not None else await self.resolve_task(identifier)
        body = {**changes, "version": task["version"]}
        result = await self.client.request("PATCH", self._path(task), body=body,
                                         key=operation_key("update", task["name"], body))
        return {"ok": True, "ticket": self._view(result["task"]), "changed": sorted(changes)}

    async def update_ticket(
        self, identifier: str, *, subject: str | None = None, description: str | None = None,
        priority: str | None = None, due_date: str | None = None,
    ) -> dict[str, Any]:
        body = {key: value for key, value in {"title": subject, "description": description,
                "priority": map_priority(priority) if priority else None, "plannedEnd": due_date}.items() if value is not None}
        if not body:
            raise BusinessValidationError("Nada que actualizar.", status_code=400)
        return await self._patch(identifier, body)

    async def convert_to_group(self, identifier: str) -> dict[str, Any]:
        return await self._patch(identifier, {"kind": "epic"})

    async def link_child(self, child: str, parent: str) -> dict[str, Any]:
        return await self._patch(child, {"parentReference": parent})

    async def set_dependencies(self, identifier: str, depends_on: list[str] | None) -> dict[str, Any]:
        return await self._patch(identifier, {"dependsOn": list(dict.fromkeys(depends_on or []))})

    async def retier_ticket(self, identifier: str, coding_tool: str) -> dict[str, Any]:
        if coding_tool not in {"tier_s", "tier_a", "tier_b", "tier_c"}:
            raise BusinessValidationError("Tier inválido.", status_code=422)
        return await self._patch(identifier, {"codingTool": coding_tool})

    def _date_changes(self, values: dict[str, Any]) -> dict[str, Any]:
        fields = {"exp_start_date": "plannedStart", "exp_end_date": "plannedEnd", "progress": "progressPercent"}
        unknown = set(values) - fields.keys() - {"completed_on"}
        if unknown or not values:
            raise BusinessValidationError("Campos de fechas/progreso inválidos.", status_code=422)
        result = {}
        for key, value in values.items():
            if key == "completed_on":
                continue
            if key != "progress" and value:
                try:
                    date = datetime.fromisoformat(str(value))
                except ValueError:
                    raise BusinessValidationError("Fecha de trabajo inválida.", status_code=422) from None
                value = (date if date.tzinfo else date.replace(tzinfo=self._zone)).isoformat()
            result[fields[key]] = value
        return result

    async def update_work_dates(self, identifier: str, dates: dict[str, Any]) -> dict[str, Any]:
        task = await self.resolve_task(identifier)
        if task["status"] != "Open" or task["is_group"]:
            raise BusinessValidationError("Las fechas reales no se reparan mediante estimaciones de planificación.", status_code=422)
        changes = self._date_changes(dates)
        return await self._patch(identifier, changes, current=task) if changes else {"ok": True, "changed": []}

    async def update_group_rollup(self, identifier: str, rollup: dict[str, Any]) -> dict[str, Any]:
        task = await self.resolve_task(identifier)
        if not task["is_group"]:
            raise BusinessValidationError("El rollup requiere un grupo.", status_code=422)
        return await self._patch(identifier, self._date_changes(rollup), current=task)

    async def list_children(self, parent_name: str, *, limit: int = 200) -> list[dict[str, Any]]:
        task = await self.resolve_task(parent_name)
        return await self._task_pages({"parentId": task["business_ticket_id"]}, max(1, min(limit, 500)))

    async def list_closed_with_dates(self, project_name: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return await self._task_pages({"projectKey": project_name, "status": "Completed"}, max(1, min(limit, 500)))

    async def list_thread(self, identifier: str, *, limit: int = 20) -> dict[str, Any]:
        detail = await self._detail(identifier)
        return {"ok": True, "ticket": self._view(detail["task"], detail),
                "comments": [{**row, "author": row.get("authorName"), "creation": row.get("createdAt")} for row in detail.get("comments", [])[:limit]],
                "attachments": [self._attachment_view(row) for row in detail.get("attachments", [])[:limit]],
                "comment_count": len(detail.get("comments", [])), "attachment_count": len(detail.get("attachments", []))}

    async def thread_of(self, task_name: str, *, limit: int = 20) -> dict[str, Any]:
        result = await self.list_thread(task_name, limit=limit)
        return {key: result[key] for key in ("comments", "attachments", "comment_count", "attachment_count")}

    async def add_comment(self, identifier: str, content: str) -> dict[str, Any]:
        task = await self.resolve_task(identifier)
        result = await self.client.request("POST", self._path(task) + "/comments", body={"content": content},
                                         key=operation_key("comment", task["name"], content))
        return {"ok": True, "ticket": task, **result}

    async def update_comment(self, comment_id: str, content: str) -> dict[str, Any]:
        path = "/api/v1/comments/" + quote(comment_id, safe="")
        current = (await self.client.request("GET", path))["comment"]
        body = {"content": content, "version": current["version"]}
        result = await self.client.request("PATCH", path, body=body, key=operation_key("comment.update", comment_id, body))
        return {"ok": True, **result}

    async def delete_comment(self, comment_id: str, ticket: str) -> dict[str, Any]:
        task = await self.resolve_task(ticket)
        path = "/api/v1/comments/" + quote(comment_id, safe="")
        current = (await self.client.request("GET", path))["comment"]
        body = {"ticketId": task["business_ticket_id"], "version": current["version"]}
        result = await self.client.request("DELETE", path, body=body, key=operation_key("comment.delete", comment_id, body))
        return {"ok": True, **result}

    def _attachment_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {**row, "file_name": row["fileName"], "size": row["fileSize"], "private": row["isPrivate"],
                "content_hash": row["sha256Hash"], "attached_to": row["taskId"],
                "download_url": self._settings.JARDAL_ERP_URL.rstrip("/") + row.get("downloadUrl", f"/api/v1/attachments/{row['id']}/content")}

    async def attach_file(
        self, identifier: str, *, source_path: str | None = None, content_base64: str | None = None,
        file_name: str | None = None, private: bool = True,
    ) -> dict[str, Any]:
        content, name = read_attachment(self._settings, source_path=source_path, content_base64=content_base64, file_name=file_name)
        task = await self.resolve_task(identifier)
        body = {"fileName": name, "contentBase64": base64.b64encode(content).decode("ascii"), "isPrivate": private}
        key = operation_key("attachment", task["name"], hashlib.sha256(content).hexdigest(), name, private)
        result = await self.client.request("POST", self._path(task) + "/attachments/content", body=body, key=key)
        return {"ok": True, "ticket": task, "attachment": self._attachment_view(result["attachment"]), "duplicate": result.get("duplicate", False)}

    async def get_attachment(self, attachment: str, *, include_content: bool = False) -> dict[str, Any]:
        result = await self.client.request("GET", "/api/v1/attachments/" + quote(attachment, safe=""), params={"includeContent": str(include_content).lower()})
        return {"ok": True, "attachment": self._attachment_view(result["attachment"])}

    async def update_attachment(
        self, attachment: str, identifier: str, *, file_name: str | None = None, private: bool | None = None,
    ) -> dict[str, Any]:
        task = await self.resolve_task(identifier)
        current = (await self.get_attachment(attachment))["attachment"]
        body: dict[str, Any] = {"ticketId": task["business_ticket_id"], "version": current["version"]}
        if file_name is not None:
            body["fileName"] = file_name
        if private is not None:
            body["isPrivate"] = private
        result = await self.client.request("PATCH", "/api/v1/attachments/" + quote(attachment, safe=""), body=body, key=operation_key("attachment.update", attachment, body))
        return {"ok": True, "attachment": self._attachment_view(result["attachment"])}

    async def delete_attachment(self, attachment: str, ticket: str) -> dict[str, Any]:
        task = await self.resolve_task(ticket)
        current = (await self.get_attachment(attachment))["attachment"]
        body = {"ticketId": task["business_ticket_id"], "version": current["version"]}
        result = await self.client.request("DELETE", "/api/v1/attachments/" + quote(attachment, safe=""), body=body, key=operation_key("attachment.delete", attachment, body))
        return {"ok": True, **result}
