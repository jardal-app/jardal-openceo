from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config_loader import ConfigLoader
from mcp_server import build_mcp_provider_config, build_mcp_provider_env
from settings import Settings
from state import AgentName, TaskCreateRequest, TaskPriority, TaskSource, TaskStatus

logger = logging.getLogger(__name__)

_PM_SUCCESS_PATTERNS = re.compile(
    r"(created|creado|creé|tickets?\s+cread|batch.*ok|\"ok\":\s*true|OK:\s*\d+)",
    re.IGNORECASE,
)
_PM_FAILURE_PATTERNS = re.compile(
    r"(error|failed|fallo|falló|\"ok\":\s*false|no se pudo)",
    re.IGNORECASE,
)
_PM_CREATED_COUNT_PATTERNS = (
    re.compile(r"\bOK:\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\b(\d+)\s*tickets?\s+cread", re.IGNORECASE),
)

_EXTRACTION_SYSTEM_PROMPT = """\
Eres un PM experto. Recibirás una entrevista con un cliente o un documento de requerimientos en texto libre.

Tu trabajo: fragmentar el contenido en tickets accionables de desarrollo.

Cada ticket DEBE tener:
- title: título corto, imperativo, específico (ej: "Implementar login con Google OAuth")
- description: qué hacer, criterios de aceptación, restricciones técnicas si las hay
- priority: low | medium | high | critical

Cada ticket PUEDE tener:
- tags: lista de etiquetas (ej: ["backend", "auth", "api"])
- ticket_type: tipo de trabajo (ej: "feature", "bug", "refactor", "research")
- due_date: fecha límite si se menciona, formato YYYY-MM-DD

Responde SOLO con JSON válido (sin markdown, sin explicaciones):
{"tickets": [{"title": "...", "description": "...", "priority": "...", "tags": [...], "ticket_type": "...", "due_date": "..."}]}

Reglas de fragmentación:
- Cada ticket = 1 unidad de trabajo que un desarrollador puede completar independientemente
- No agrupes múltiples funcionalidades en un solo ticket
- No crees tickets vagos como "mejorar la app" — sé específico
- Si el cliente menciona algo urgente o bloqueante, marca como high/critical
- Si no hay indicación de prioridad, usa medium
- Si el cliente menciona algo que es "nice to have" o futuro, marca como low
- Máximo 50 tickets
- Idioma del ticket = idioma de la entrevista
- Incluye tickets de infraestructura implícitos (ej: si piden "login con Google", incluye "Configurar OAuth credentials en Google Cloud")
"""

_CLI_PROMPT_TEMPLATE = """\
Eres un PM experto. Tu tarea: leer la entrevista/requerimiento de abajo, \
fragmentarla en tickets accionables de desarrollo y crearlos en Notion.

USA la herramienta MCP `notion_create_pm_tickets_batch` para crear todos los tickets de una vez.
Pasa estos parámetros fijos a la herramienta:
- project: "{project}"
- source_id: "{source_id}"
- source: "entrevista"

El parámetro `tickets` es un JSON string con un array de objetos. Cada objeto necesita:
- title (obligatorio): título corto, imperativo, específico
- description: qué hacer + criterios de aceptación
- priority: low | medium | high | critical (default: medium)
- tags: array de strings

No pongas `project`, `source` ni `source_id` dentro de cada ticket; la herramienta los inyecta por lote.

Reglas:
- 1 ticket = 1 unidad de trabajo independiente
- No agrupes funcionalidades distintas en un ticket
- Incluye tickets implícitos de infraestructura
- Si algo es urgente/bloqueante → high/critical
- Máximo 50 tickets

IMPORTANTE: Responde SOLO con una línea concisa indicando el resultado. Ejemplo:
- "OK: 12 tickets creados en Notion"
- "ERROR: no se pudieron crear los tickets: [razón]"
NO listes los tickets individuales en tu respuesta.

--- ENTREVISTA ---
{interview}
--- FIN ENTREVISTA ---"""

# Subprocess timeout in seconds (10 minutes)
_SUBPROCESS_TIMEOUT = 600


@dataclass
class PMExtractionResult:
    status: TaskStatus
    tickets_created: int = 0
    tickets_failed: int = 0
    ticket_summaries: list[dict[str, Any]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def summary(self) -> str:
        if self.error:
            return f"Error en extracción PM: {self.error}"
        parts = [f"Extraje y registré {self.tickets_created} ticket(s) en MongoDB."]
        if self.tickets_failed:
            parts.append(f"{self.tickets_failed} fallaron.")
        if self.ticket_summaries:
            lines = []
            for i, t in enumerate(self.ticket_summaries[:10], 1):
                status = "✓" if t.get("ok") else "✗"
                lines.append(f"  {status} {i}. {t.get('title', '?')}")
            parts.append("\n".join(lines))
            if len(self.ticket_summaries) > 10:
                parts.append(f"  ... y {len(self.ticket_summaries) - 10} más")
        return "\n".join(parts)


class PMSessionManager:
    """Extracts tickets from interviews/requirements using DeepSeek or CLI subprocess."""

    def __init__(
        self,
        settings: Settings,
        config_loader: ConfigLoader | None = None,
    ) -> None:
        self._settings = settings
        self._config_loader = config_loader
        self._llm: ChatOpenAI | None = None
        self._task_service: Any = None

        if settings.DEEPSEEK_API_KEY:
            self._llm = ChatOpenAI(
                model=settings.CEO_LLM_MODEL,
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.CEO_LLM_BASE_URL,
                temperature=0,
            )

    def set_task_service(self, task_service: Any) -> None:
        """Late-bind task service after construction (avoids circular dependency)."""
        self._task_service = task_service

    # ---- DeepSeek direct extraction (langraph) ----

    async def extract_tickets(
        self,
        interview_text: str,
        project: str,
        autorun: bool = False,
    ) -> PMExtractionResult:
        if self._llm is None:
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error="No LLM configured (DEEPSEEK_API_KEY missing)",
            )

        if self._task_service is None:
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error="Task service not configured (call set_task_service first)",
            )

        try:
            response = await self._llm.ainvoke([
                SystemMessage(content=_EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=interview_text),
            ])
        except Exception as exc:
            logger.error("PM LLM extraction failed: %s", exc)
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"LLM error: {exc}",
            )

        try:
            payload = _extract_json_payload(response.content)
            tickets = payload.get("tickets", [])
            if not isinstance(tickets, list):
                tickets = []
        except (ValueError, AttributeError) as exc:
            raw_preview = _coerce_llm_text(getattr(response, "content", ""))[:500]
            logger.error("PM JSON parse failed: %s — raw: %s", exc, raw_preview)
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"LLM returned invalid JSON: {exc}",
            )

        if not tickets:
            return PMExtractionResult(
                status=TaskStatus.COMPLETED,
                tickets_created=0,
                error="No se encontraron tickets accionables en el texto.",
            )

        source_id = f"pm-langraph-{uuid4().hex[:12]}"
        created = 0
        failed = 0
        task_ids: list[str] = []
        summaries: list[dict[str, Any]] = []

        for idx, t in enumerate(tickets[:50], 1):
            ticket_source_id = f"{source_id}:{idx}"
            priority = _coerce_priority(t.get("priority"))
            tag_list = t.get("tags")
            if isinstance(tag_list, list):
                tag_list = [str(tag).strip() for tag in tag_list if str(tag).strip()]

            req = TaskCreateRequest(
                project=project,
                source=TaskSource.API,
                source_id=ticket_source_id,
                title=str(t.get("title", "Sin título")).strip(),
                description=str(t.get("description") or "").strip(),
                agent_hint=AgentName.DEV,
                priority=priority,
                params={
                    "workflow": "pm.extract_tickets",
                    "pm_batch": {
                        "source": "entrevista",
                        "batch_id": source_id,
                        "position": idx,
                        "total": len(tickets),
                    },
                    "tags": tag_list,
                    "ticket_type": t.get("ticket_type"),
                    "due_date": t.get("due_date"),
                    "external_url": t.get("external_url"),
                },
                autorun=autorun,
            )

            try:
                task = await self._task_service.create_task(req)
                task_ids.append(str(task.get("task_id") or ""))
                summaries.append({
                    "ok": True,
                    "title": str(t.get("title", "?")),
                    "task_id": str(task.get("task_id") or ""),
                    "status": str(task.get("status") or ""),
                })
                created += 1
            except Exception as exc:
                logger.warning("PM task create failed for ticket %d: %s", idx, exc)
                summaries.append({
                    "ok": False,
                    "title": str(t.get("title", "?")),
                    "error": str(exc),
                })
                failed += 1

        return PMExtractionResult(
            status=TaskStatus.COMPLETED,
            tickets_created=created,
            tickets_failed=failed,
            ticket_summaries=summaries,
            task_ids=task_ids,
        )

    # ---- Claude / Codex headless subprocess extraction ----

    async def extract_tickets_subprocess(
        self,
        interview_text: str,
        project: str,
        tool: str,
        autorun: bool = False,
    ) -> PMExtractionResult:
        """Extract tickets using Claude/Codex in headless subprocess mode.

        The subprocess extracts tickets via LLM and invokes the MCP tool
        `notion_create_pm_tickets_batch`. We parse the structured output
        (results JSON from the MCP tool) to build MongoDB tasks.
        """
        if self._config_loader is None:
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error="ConfigLoader not configured for PM",
            )

        if self._task_service is None:
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error="Task service not configured (call set_task_service first)",
            )

        config = self._config_loader.load_project(project)
        dev_cfg = config.get("dev", {})
        providers_cfg = dev_cfg.get("providers", {})
        provider_cfg = providers_cfg.get(tool, {})

        command = provider_cfg.get("command", [])
        binary = command[0] if command else None

        if not binary:
            if tool == "claude":
                binary = self._settings.CLAUDE_CODE_PATH
            elif tool == "codex":
                binary = self._settings.CODEX_CLI_PATH
            else:
                return PMExtractionResult(
                    status=TaskStatus.FAILED,
                    error=f"Unknown tool: {tool}",
                )

        cwd = provider_cfg.get("cwd") or None
        env_vars = dict(provider_cfg.get("env") or {})
        source_id = f"pm-{tool}-{uuid4().hex[:12]}"

        if tool == "claude":
            mcp_config = build_mcp_provider_config(self._settings)
            cmd = [
                binary,
                "-p",
                "--dangerously-skip-permissions",
                "--model", "opus",
                "--effort", "high",
            ]
            if mcp_config is not None:
                cmd.extend([
                    "--mcp-config",
                    json.dumps(mcp_config, separators=(",", ":")),
                    "--strict-mcp-config",
                ])
        elif tool == "codex":
            cmd = [
                binary,
                "exec",
                "-m", "gpt-5.4",
                "-c", "model_reasoning_effort=xhigh",
                "-",
            ]
        else:
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"Unknown tool: {tool}",
            )

        prompt = _CLI_PROMPT_TEMPLATE.format(project=project, source_id=source_id, interview=interview_text)

        full_env = dict(os.environ)
        full_env.update(build_mcp_provider_env(self._settings))
        full_env.update(env_vars)

        logger.info("PM subprocess: launching %s for project=%s (cwd=%s)", tool, project, cwd)

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=full_env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=_SUBPROCESS_TIMEOUT,
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            logger.error("PM subprocess timed out (%ds) for tool=%s project=%s", _SUBPROCESS_TIMEOUT, tool, project)
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"CLI timeout ({_SUBPROCESS_TIMEOUT}s) with tool={tool}",
            )
        except FileNotFoundError:
            logger.error("PM subprocess binary not found: %s", binary)
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"Binary not found: {binary}",
            )
        except Exception as exc:
            logger.error("PM subprocess error: %s", exc)
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"Subprocess error: {exc}",
            )

        output = stdout_bytes.decode(errors="replace").strip()
        err_output = stderr_bytes.decode(errors="replace").strip()

        logger.info("PM subprocess exit code=%s, stdout=%d chars, stderr=%d chars",
                     proc.returncode, len(output), len(err_output))

        if proc.returncode != 0:
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"CLI exit code {proc.returncode}: {err_output[:500]}",
            )

        if _PM_FAILURE_PATTERNS.search(output) and not _PM_SUCCESS_PATTERNS.search(output):
            return PMExtractionResult(
                status=TaskStatus.FAILED,
                error=f"CLI reported error: {output[:500]}",
            )

        tickets = _parse_tickets_from_subprocess_output(output)
        if not tickets:
            return PMExtractionResult(
                status=TaskStatus.COMPLETED,
                tickets_created=0,
                error="No se pudieron parsear tickets del output del CLI.",
            )

        created = 0
        failed = 0
        task_ids: list[str] = []
        summaries: list[dict[str, Any]] = []

        for idx, t in enumerate(tickets, 1):
            ticket_source_id = f"{source_id}:{idx}"
            priority = _coerce_priority(t.get("priority"))
            tag_list = t.get("tags")
            if isinstance(tag_list, list):
                tag_list = [str(tag).strip() for tag in tag_list if str(tag).strip()]

            req = TaskCreateRequest(
                project=project,
                source=TaskSource.API,
                source_id=ticket_source_id,
                title=str(t.get("title", "Sin título")).strip(),
                description=str(t.get("description") or "").strip(),
                agent_hint=AgentName.DEV,
                priority=priority,
                params={
                    "workflow": "pm.extract_tickets_subprocess",
                    "pm_batch": {
                        "source": "entrevista",
                        "batch_id": source_id,
                        "tool": tool,
                        "position": idx,
                        "total": len(tickets),
                    },
                    "tags": tag_list,
                    "ticket_type": t.get("ticket_type"),
                    "due_date": t.get("due_date"),
                    "external_url": t.get("external_url"),
                },
                autorun=autorun,
            )

            try:
                task = await self._task_service.create_task(req)
                task_ids.append(str(task.get("task_id") or ""))
                summaries.append({
                    "ok": True,
                    "title": str(t.get("title", "?")),
                    "task_id": str(task.get("task_id") or ""),
                    "status": str(task.get("status") or ""),
                })
                created += 1
            except Exception as exc:
                logger.warning("PM task create failed for ticket %d: %s", idx, exc)
                summaries.append({
                    "ok": False,
                    "title": str(t.get("title", "?")),
                    "error": str(exc),
                })
                failed += 1

        return PMExtractionResult(
            status=TaskStatus.COMPLETED,
            tickets_created=created,
            tickets_failed=failed,
            ticket_summaries=summaries,
            task_ids=task_ids,
        )

    async def _lookup_created_tickets(
        self,
        notion_manager: Any | None,
        source_id: str,
    ) -> PMExtractionResult | None:
        if notion_manager is None:
            return None

        try:
            result = await asyncio.to_thread(lambda: notion_manager.list_tickets_by_source_id(source_id))
        except Exception as exc:
            logger.warning("PM subprocess: no se pudo consultar Notion por source_id=%s: %s", source_id, exc)
            return None

        if not result.get("ok"):
            return None

        tickets = result.get("tickets") or []
        if not isinstance(tickets, list) or not tickets:
            return None

        return PMExtractionResult(
            status=TaskStatus.COMPLETED,
            tickets_created=int(result.get("count") or len(tickets)),
            ticket_summaries=tickets,
        )


def _extract_json_payload(raw_content: Any) -> dict[str, Any]:
    if isinstance(raw_content, dict):
        return raw_content
    if isinstance(raw_content, list):
        return {"tickets": raw_content}

    text = _coerce_llm_text(raw_content).strip()
    if not text:
        raise ValueError("LLM returned empty content")

    candidates = [text]
    fenced_blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidates.extend(block.strip() for block in fenced_blocks if block.strip())

    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1].strip())

    seen: set[str] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(payload, list):
            return {"tickets": payload}
        if isinstance(payload, dict):
            return payload
        last_error = ValueError("LLM JSON payload must be an object or an array.")

    if last_error is not None:
        raise ValueError(str(last_error)) from last_error
    raise ValueError("LLM returned invalid JSON.")


def _coerce_llm_text(raw_content: Any) -> str:
    if isinstance(raw_content, str):
        return raw_content
    if isinstance(raw_content, list):
        parts: list[str] = []
        for item in raw_content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part).strip()
    return str(raw_content)


def _parse_tickets_from_subprocess_output(output: str) -> list[dict[str, Any]]:
    """Try to extract ticket list from subprocess stdout (MCP tool result or LLM JSON)."""
    if not output:
        return []

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue

        if isinstance(parsed, dict):
            result_data = parsed.get("result") or parsed.get("results") or parsed.get("tickets")
            if isinstance(result_data, list) and result_data:
                return result_data
            if parsed.get("ok") and isinstance(parsed, dict):
                for key in ("result", "results", "tickets", "data"):
                    val = parsed.get(key)
                    if isinstance(val, list) and val:
                        return val

        if isinstance(parsed, list) and parsed:
            return parsed

    text = output.strip()
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start >= 0 and end > start:
            candidate = text[start:end + 1].strip()
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, list) and parsed:
                return parsed
            if isinstance(parsed, dict):
                for key in ("tickets", "results", "data"):
                    val = parsed.get(key)
                    if isinstance(val, list) and val:
                        return val

    return []


def _extract_created_count(output: str) -> int:
    for pattern in _PM_CREATED_COUNT_PATTERNS:
        match = pattern.search(output or "")
        if match is None:
            continue
        raw_value = next((group for group in match.groups() if group), None)
        if raw_value is None:
            continue
        try:
            return int(raw_value)
        except ValueError:
            continue
    return -1


def _coerce_priority(raw: Any) -> TaskPriority:
    if isinstance(raw, TaskPriority):
        return raw
    value = str(raw or "").strip().lower()
    if value == "critical":
        return TaskPriority.CRITICAL
    if value == "high":
        return TaskPriority.HIGH
    if value == "low":
        return TaskPriority.LOW
    return TaskPriority.MEDIUM
