from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from settings import Settings
from state import TaskStatus
from tool_locks import is_lock_pending
from tools.email_sender import SMTPEmailSender


class EmailLockUnavailable(RuntimeError):
    """Off-allowlist send without a TaskService to escalate through."""


@dataclass
class SecretaryExecutionOutput:
    status: TaskStatus
    steps: list[dict[str, Any]] = field(default_factory=list)
    last_response_summary: str | None = None
    error: str | None = None
    waiting_reason: str | None = None
    owner_question_id: str | None = None


class SecretarySessionManager:
    """Minimal secretary runtime focused on SMTP email sending."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._email_sender = SMTPEmailSender(settings)
        self._task_service: Any | None = None
        self._llm: ChatOpenAI | None = None
        if settings.DEEPSEEK_API_KEY:
            self._llm = ChatOpenAI(
                model=settings.CEO_LLM_MODEL,
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.CEO_LLM_BASE_URL,
                temperature=0,
            )

    def set_task_service(self, task_service: Any) -> None:
        """Late-bound TaskService for the email lock (TASK-2026-00208)."""
        self._task_service = task_service

    def _mandate(self) -> Any | None:
        from company_mandate import MandateError
        from config_loader import ConfigLoader
        try:
            return ConfigLoader(self._settings).load_company_mandate()
        except MandateError:
            return None

    async def run(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any] | None = None,
    ) -> SecretaryExecutionOutput:
        output = SecretaryExecutionOutput(status=TaskStatus.IN_PROGRESS)

        payload = await self._extract_email_payload(task)
        if payload is None:
            detail = (
                "No pude extraer un payload de email válido. "
                "Usa formato JSON o indica claramente remitente, destinatario, asunto y cuerpo."
            )
            output.steps.append(self._step("extract_email_payload", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output

        attachments = self._merge_attachments(payload.get("attachments"), task.get("attachments"))

        output.steps.append(
            self._step(
                "extract_email_payload",
                "ok",
                f"Remitente {payload.get('from_email') or self._settings.SMTP_DEFAULT_SENDER} "
                f"→ {', '.join(payload.get('to', []))}"
                f"{self._language_detail(payload.get('language'))}"
                f"{self._attachments_detail(attachments)}",
            )
        )

        try:
            result = await self._gated_send(task, project_config, payload, attachments)
        except Exception as exc:
            output.steps.append(self._step("send_email", "failed", str(exc)))
            output.status = TaskStatus.FAILED
            output.error = str(exc)
            return output

        if is_lock_pending(result):
            detail = (
                "Candado: pendiente de aprobacion del dueno "
                f"({result.get('question_task_id') or 'sin pregunta'})."
            )
            output.steps.append(self._step("send_email", "waiting_approval", detail))
            output.status = TaskStatus.WAITING
            output.waiting_reason = str(result.get("waiting_reason") or "tool_lock:email")
            output.owner_question_id = result.get("question_task_id")
            output.last_response_summary = detail
            return output

        if str(result.get("status") or "") == "denied":
            detail = str(result.get("reason") or "El dueno denego el envio.")
            output.steps.append(self._step("send_email", "failed", detail))
            output.status = TaskStatus.FAILED
            output.error = detail
            return output

        summary = (
            f"Correo enviado desde {result['from']} a {', '.join(result['to'])}. "
            f"Asunto: {result['subject']}"
            f"{self._attachments_sent_detail(result.get('attachments', []))}"
        )
        output.steps.append(self._step("send_email", "ok", summary))
        output.status = TaskStatus.COMPLETED
        output.last_response_summary = summary
        return output

    async def _gated_send(
        self,
        task: dict[str, Any],
        project_config: dict[str, Any] | None,
        payload: dict[str, Any],
        attachments: list[str],
    ) -> dict[str, Any]:
        """Email lock (TASK-2026-00208): allowlist sends, the rest locks."""
        from tool_locks import check_email_lock

        decision = check_email_lock(
            to=payload.get("to", []),
            cc=payload.get("cc", []),
            bcc=payload.get("bcc", []),
            project_config=project_config or {},
            mandate=self._mandate(),
        )
        if not decision.get("escalate"):
            return self._email_sender.send_email(
                from_email=payload.get("from_email"),
                to=payload.get("to", []),
                cc=payload.get("cc", []),
                bcc=payload.get("bcc", []),
                subject=str(payload.get("subject") or "").strip(),
                body_text=str(payload.get("body_text") or "").strip(),
                body_html=payload.get("body_html"),
                reply_to=payload.get("reply_to"),
                attachments=attachments,
                language=payload.get("language"),
            )
        if self._task_service is None:
            raise EmailLockUnavailable(
                "Candado sin TaskService: no enviado.")
        return await self._task_service.tool_send_email(
            from_email=payload.get("from_email"),
            to=payload.get("to", []),
            cc=payload.get("cc", []),
            bcc=payload.get("bcc", []),
            subject=str(payload.get("subject") or "").strip(),
            body_text=str(payload.get("body_text") or "").strip(),
            body_html=payload.get("body_html"),
            reply_to=payload.get("reply_to"),
            attachments=attachments,
            language=payload.get("language"),
            project=task.get("project"),
            origin_task_id=task.get("task_id"),
        )

    async def _extract_email_payload(self, task: dict[str, Any]) -> dict[str, Any] | None:
        text = str(task.get("description") or "").strip()
        if not text:
            return None

        by_json = self._extract_json_payload(text)
        if by_json is not None:
            return by_json

        by_pattern = self._extract_pattern_payload(text)
        if by_pattern is not None:
            return by_pattern

        if self._llm is None:
            return None

        instruction = (
            "Extrae un payload para enviar email. Devuelve JSON estricto con las claves: "
            "action, from_email, to, cc, bcc, subject, body_text, body_html, reply_to, attachments, language. "
            "Si no es una petición de correo responde {\"action\":\"unknown\"}. "
            "to/cc/bcc/attachments deben ser arrays de strings. language debe ser es o en cuando el usuario lo indique."
        )
        response = await self._llm.ainvoke(
            [
                SystemMessage(content=instruction),
                HumanMessage(content=text),
            ]
        )
        payload = self._extract_json_payload(str(getattr(response, "content", "") or ""))
        if not payload:
            return None
        if str(payload.get("action") or "").strip().lower() not in {"send_email", "email"}:
            return None
        return self._normalize_payload(payload)

    def _extract_json_payload(self, text: str) -> dict[str, Any] | None:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()

        candidates: list[str] = []
        if cleaned.startswith("{") and cleaned.endswith("}"):
            candidates.append(cleaned)

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidates.append(cleaned[start : end + 1])

        for candidate in candidates:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return self._normalize_payload(payload)
        return None

    def _extract_pattern_payload(self, text: str) -> dict[str, Any] | None:
        patterns = {
            "from_email": r"^(?:from|de|remitente)\s*:\s*(.+)$",
            "to": r"^(?:to|para|destinatarios?)\s*:\s*(.+)$",
            "cc": r"^(?:cc)\s*:\s*(.+)$",
            "bcc": r"^(?:bcc)\s*:\s*(.+)$",
            "reply_to": r"^(?:reply-to|reply_to|responder a)\s*:\s*(.+)$",
            "subject": r"^(?:subject|asunto)\s*:\s*(.+)$",
            "attachments": r"^(?:attachments|adjuntos|archivos)\s*:\s*(.+)$",
            "language": r"^(?:language|idioma|lang)\s*:\s*(.+)$",
        }

        extracted: dict[str, Any] = {}
        lines = text.splitlines()
        for key, pattern in patterns.items():
            for line in lines:
                match = re.match(pattern, line.strip(), flags=re.IGNORECASE)
                if match:
                    extracted[key] = match.group(1).strip()
                    break

        body_match = re.search(r"(?:^|\n)(?:body|cuerpo)\s*:\s*(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
        if body_match:
            extracted["body_text"] = body_match.group(1).strip()

        if "to" not in extracted or "subject" not in extracted or "body_text" not in extracted:
            return None

        return self._normalize_payload(extracted)

    def _normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "action": str(payload.get("action") or "send_email").strip().lower(),
            "from_email": self._optional_str(payload.get("from_email")),
            "to": self._ensure_list(payload.get("to")),
            "cc": self._ensure_list(payload.get("cc")),
            "bcc": self._ensure_list(payload.get("bcc")),
            "reply_to": self._optional_str(payload.get("reply_to")),
            "subject": str(payload.get("subject") or "").strip(),
            "body_text": str(payload.get("body_text") or "").strip(),
            "body_html": self._optional_str(payload.get("body_html")),
            "attachments": self._ensure_list(payload.get("attachments")),
            "language": self._optional_str(payload.get("language")),
        }

    @staticmethod
    def _ensure_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            items = [str(item).strip() for item in value]
        else:
            items = [item.strip() for item in str(value).split(",")]
        return [item for item in items if item]

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _merge_attachments(payload_attachments: Any, task_attachments: Any) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for source in (payload_attachments, task_attachments):
            for item in SecretarySessionManager._ensure_list(source):
                if item in seen:
                    continue
                seen.add(item)
                merged.append(item)
        return merged

    @staticmethod
    def _attachments_detail(attachments: list[str]) -> str:
        if not attachments:
            return ""
        return f" con {len(attachments)} adjunto(s)"

    @staticmethod
    def _attachments_sent_detail(attachments: list[str]) -> str:
        if not attachments:
            return ""
        return f". Adjuntos: {', '.join(attachments)}"

    @staticmethod
    def _language_detail(language: str | None) -> str:
        if not language:
            return ""
        return f" [{language}]"

    @staticmethod
    def _step(step: str, status: str, detail: str) -> dict[str, Any]:
        return {
            "step": step,
            "status": status,
            "detail": detail,
            "timestamp": datetime.now(timezone.utc),
        }
