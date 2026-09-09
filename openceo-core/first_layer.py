from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from config_loader import ConfigLoader
from db.mongo import MongoRepository
from dev_session_service import DevSessionService
from project_service import ProjectService
from settings import Settings
from state import ChatCompletionRequest, DevProvider, DevSessionCreateRequest, TaskStatus
from task_service import TaskService

logger = logging.getLogger(__name__)


class FirstLayerGateway:
    """DeepSeek Direct First Layer Cognitive Gateway for OpenCEO.

    Provides deep context-aware intent understanding without fragile keyword patterns.
    Knows available projects, running host tmux sessions, tickets, and user focus state.
    """

    def __init__(
        self,
        settings: Settings,
        repo: MongoRepository,
        project_service: ProjectService,
        dev_session_service: DevSessionService,
        task_service: TaskService,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.project_service = project_service
        self.dev_session_service = dev_session_service
        self.task_service = task_service
        self._llm: ChatOpenAI | None = None

        if settings.DEEPSEEK_API_KEY:
            self._llm = ChatOpenAI(
                model=settings.CEO_LLM_MODEL,
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.CEO_LLM_BASE_URL,
                temperature=0,
                max_tokens=2000,
                timeout=15,
            )

    async def gather_context(self, user_id: str) -> dict[str, Any]:
        """Gather real-time workspace context for the user."""
        selected_project = await self.repo.get_selected_project(user_id) or self.settings.DEFAULT_PROJECT

        # Active ticket focus
        active_ticket = await self.repo.get_active_ticket(user_id)
        active_ticket_doc = None
        if active_ticket and active_ticket.get("task_id"):
            active_ticket_doc = await self.repo.get_task(active_ticket["task_id"])

        # Active dev session
        active_session_id, active_session_project = await self.repo.get_active_dev_chat_session(user_id)
        active_session = None
        if active_session_id:
            active_session = await self.repo.get_dev_session(active_session_id)

        # Projects list
        projects = self.project_service.list_projects()

        # Host tmux sessions
        tmux_runtime = self.dev_session_service._tmux
        all_tmux = tmux_runtime.list_all_host_sessions()

        # Recent tasks for selected project
        recent_tasks = (
            await self.repo.tasks.find({"project": selected_project})
            .sort("created_at", -1)
            .limit(6)
            .to_list(6)
        )

        return {
            "selected_project": selected_project,
            "active_ticket": active_ticket_doc,
            "active_session": active_session,
            "projects": projects,
            "all_tmux": all_tmux,
            "recent_tasks": recent_tasks,
        }

    def _build_context_prompt(self, ctx: dict[str, Any]) -> str:
        lines: list[str] = []

        lines.append("PROYECTOS DISPONIBLES:")
        for p in ctx["projects"]:
            lines.append(f"- {p['name']}: {p.get('title', p['name'])} (default: {p.get('default_provider', 'auto')})")

        lines.append(f"\nPROYECTO SELECCIONADO ACTUALMENTE: {ctx['selected_project']}")

        # Active ticket
        at = ctx.get("active_ticket")
        if at:
            num = at.get("ticket_number", "?")
            status = at.get("status", "unknown")
            title = at.get("title", "")
            err = at.get("error") or at.get("last_response_summary") or at.get("waiting_reason") or "ninguno"
            phase = at.get("phase", "dev_work")
            lines.append(
                f"TICKET ACTUALMENTE EN FOCO: #{num} [{status}] ({title}) - Fase: {phase} - Último detalle/error: {str(err)[:200]}"
            )
        else:
            lines.append("TICKET ACTUALMENTE EN FOCO: Ninguno")

        # Active dev session
        s = ctx.get("active_session")
        if s:
            lines.append(
                f"SESIÓN DEV ACTIVA: {s.get('tmux_session_name', s.get('session_id'))} [{s.get('status')}] en {s.get('project')}"
            )
        else:
            lines.append("SESIÓN DEV ACTIVA: Ninguna (modo conversación)")

        # Tmux sessions
        tmux_list = ctx.get("all_tmux", [])
        if tmux_list:
            tmux_summary = [f"{t['name']}" + (" (adjunto)" if t.get("attached") else "") for t in tmux_list]
            lines.append(f"SESIONES TMUX EN EL SERVIDOR: [{', '.join(tmux_summary)}]")
        else:
            lines.append("SESIONES TMUX EN EL SERVIDOR: []")

        # Recent tickets
        recent = ctx.get("recent_tasks", [])
        if recent:
            lines.append(f"TICKETS RECIENTES EN {ctx['selected_project']}:")
            for t in recent:
                t_num = t.get("ticket_number", "?")
                t_st = t.get("status", "unknown")
                t_tit = str(t.get("title", ""))[:60]
                lines.append(f"- #{t_num} [{t_st}]: {t_tit}")

        return "\n".join(lines)

    async def process_turn(
        self,
        user_id: str,
        message: str,
        payload: ChatCompletionRequest,
    ) -> tuple[bool, str]:
        """Process incoming user turn through the First Layer cognitive gateway.

        Returns (handled, response_text). If handled is True, response_text is the final answer.
        """
        if self._llm is None or not message.strip():
            return False, ""

        text = message.strip()
        ctx = await self.gather_context(user_id)
        context_str = self._build_context_prompt(ctx)

        system_prompt = f"""Eres la Primera Capa Cognitiva y Asistente Ejecutivo de OpenCEO.
El usuario te habla desde su móvil o por voz mientras camina en la calle, gestionando múltiples proyectos y tickets.
Comprende el lenguaje natural en español sin depender de palabras clave rígidas.

CONTEXTO ACTUAL DEL WORKSPACE:
{context_str}

DIRECTIVAS CLAVE:
1. El usuario trabaja con un concepto de 'FOCO': puede enfocar un ticket para conversar con él, consultar su estado, error o tareas, y luego cambiar el foco a otro ticket.
2. Si el usuario pide enfocar o ver un ticket específico ('vamos al ticket 48', 'enfoca el 49', 'cómo va el 48'), extrae ticket_number.
3. Si el usuario pregunta cosas como '¿por qué falló?', '¿qué falta?' o '¿qué cambios pide?' y hay un TICKET EN FOCO, entiende que se refiere a ese ticket.
4. Si el usuario pide reintentar, ejecutar o continuar el ticket en foco o mencionado, marca ticket_retry.
5. Si el usuario pregunta por sesiones de tmux ('qué sesiones de tmux hay', 'mis tmux'), marca list_tmux.
6. Si el usuario pide ver la pantalla o salida de un tmux ('qué sale en tr', 'pantalla de investor'), marca tmux_inspect con session_name.
7. Si el usuario pide enviar comando o teclas a un tmux ('manda git status a tr'), marca tmux_send.
8. Si el usuario pide abrir un nuevo tmux ('abre un tmux para trading'), marca tmux_create.
9. Si el usuario da una instrucción de programación y hay una SESIÓN DEV ACTIVA, marca dev_instruction.
10. Si el usuario pide crear una sesión dev de código en un proyecto, marca dev_session_start.
11. Siempre proporciona un 'spoken_reply' en español claro, conciso y natural, perfectamente optimizado para ser escuchado por voz. Evita UUIDs, URLs largas o JSONs en spoken_reply.

Responde SIEMPRE en formato JSON estricto con esta estructura:
{{
  "intent": "focus_ticket" | "ticket_query" | "ticket_retry" | "list_tickets" | "list_tmux" | "tmux_inspect" | "tmux_send" | "tmux_create" | "tmux_kill" | "dev_session_start" | "dev_session_stop" | "dev_instruction" | "dev_output" | "switch_project" | "agent_dispatch" | "chat_response",
  "ticket_number": int | null,
  "session_name": string | null,
  "project": string | null,
  "provider": string | null,
  "keys": string | null,
  "instruction": string | null,
  "agent": "dev" | "pm" | "finance" | "secretary" | null,
  "spoken_reply": string
}}
"""

        try:
            resp = await self._llm.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=text),
                ]
            )
            raw_content = resp.content.strip()
            if raw_content.startswith("```"):
                raw_content = re.sub(r"^```(?:json)?\n?", "", raw_content)
                raw_content = re.sub(r"\n?```$", "", raw_content)

            parsed = json.loads(raw_content)
        except Exception as exc:
            logger.warning("FirstLayerGateway classification failed: %s", exc)
            return False, ""

        intent = str(parsed.get("intent") or "chat_response")
        spoken_reply = str(parsed.get("spoken_reply") or "").strip()
        ticket_num = parsed.get("ticket_number")
        project_param = parsed.get("project")
        session_name = parsed.get("session_name")
        provider_param = parsed.get("provider")
        keys_param = parsed.get("keys")

        tmux_runtime = self.dev_session_service._tmux

        # ------------------- 1. FOCUS TICKET -------------------
        if intent == "focus_ticket":
            if isinstance(ticket_num, int):
                target_project = project_param or ctx["selected_project"]
                task = await self.repo.get_task_by_ticket_number(ticket_num, project=target_project)
                if task:
                    await self.repo.set_active_ticket(
                        user_id=user_id,
                        ticket_number=ticket_num,
                        task_id=task["task_id"],
                        project=task["project"],
                    )
                    status_text = task.get("status", "desconocido")
                    title = task.get("title", "")
                    return True, f"Enfocado el ticket #{ticket_num} en {task['project']}: '{title}'. Estado actual: {status_text}."
                else:
                    return True, f"No encontré el ticket #{ticket_num} en la base de datos."

        # ------------------- 2. TICKET QUERY -------------------
        if intent == "ticket_query":
            if spoken_reply and len(spoken_reply) > 15:
                return True, spoken_reply
            at = ctx.get("active_ticket")
            if at:
                t_num = at.get("ticket_number", "")
                t_st = at.get("status", "")
                t_err = at.get("error") or at.get("last_response_summary") or at.get("waiting_reason")
                reply = f"Ticket #{t_num} en estado {t_st}."
                if t_err:
                    reply += f" Detalle: {str(t_err)[:150]}."
                return True, reply
            return True, "No tienes ningún ticket enfocado ahora mismo. Dime cuál quieres enfocar."

        # ------------------- 3. TICKET RETRY -------------------
        if intent == "ticket_retry":
            at = ctx.get("active_ticket")
            target_task = None
            if isinstance(ticket_num, int):
                target_task = await self.repo.get_task_by_ticket_number(ticket_num, project=ctx["selected_project"])
            elif at:
                target_task = at

            if target_task:
                task_id = target_task["task_id"]
                t_num = target_task.get("ticket_number", ticket_num)
                proj = target_task.get("project", ctx["selected_project"])
                try:
                    await self.task_service.retry_task(task_id)
                except Exception:
                    pass
                msg = spoken_reply or f"Reintentando ticket #{t_num} en {proj} en segundo plano."
                return True, msg
            return True, "No encontré el ticket que quieres reintentar. Di por ejemplo: reintenta el ticket 48."

        # ------------------- 4. LIST TICKETS -------------------
        if intent == "list_tickets":
            target_proj = project_param or ctx["selected_project"]
            recent = (
                await self.repo.tasks.find({"project": target_proj})
                .sort("created_at", -1)
                .limit(6)
                .to_list(6)
            )
            if not recent:
                return True, f"No hay tickets recientes en el proyecto {target_proj}."
            parts = []
            for t in recent[:4]:
                parts.append(f"#{t.get('ticket_number')}: {t.get('title', '')[:40]} ({t.get('status')})")
            summary = "; ".join(parts)
            return True, f"En {target_proj} tienes: {summary}. ¿Cuál quieres enfocar?"

        # ------------------- 5. LIST TMUX -------------------
        if intent == "list_tmux":
            all_tmux = tmux_runtime.list_all_host_sessions()
            if not all_tmux:
                return True, "No hay sesiones de tmux activas en este servidor."
            names = [s["name"] for s in all_tmux]
            return True, f"Tienes {len(names)} sesiones de tmux activas: {', '.join(names)}."

        # ------------------- 6. TMUX INSPECT -------------------
        if intent == "tmux_inspect":
            target_s = session_name
            if not target_s and ctx.get("all_tmux"):
                target_s = ctx["all_tmux"][0]["name"]
            if target_s:
                pane_text = tmux_runtime.capture_pane_by_name(target_s, max_lines=10)
                if pane_text is not None:
                    last_lines = [l.strip() for l in pane_text.splitlines() if l.strip()]
                    tail = " | ".join(last_lines[-3:]) if last_lines else "pantalla vacía"
                    return True, f"En la sesión {target_s}: {tail}"
                return True, f"No pude leer la sesión tmux {target_s}."

        # ------------------- 7. TMUX SEND -------------------
        if intent == "tmux_send":
            if session_name and keys_param:
                ok = tmux_runtime.send_keys_by_name(session_name, keys_param, enter=True)
                if ok:
                    return True, f"Comando enviado a tmux {session_name}."
                return True, f"No pude enviar el comando a {session_name}."

        # ------------------- 8. TMUX CREATE -------------------
        if intent == "tmux_create":
            s_name = session_name or f"session-{len(ctx.get('all_tmux', [])) + 1}"
            cwd = None
            if project_param and self.project_service._config_loader.has_project(project_param):
                p_cfg = self.project_service._config_loader.load_project(project_param)
                cwd = p_cfg.get("dev", {}).get("cwd")
            ok = tmux_runtime.create_generic_session(s_name, cwd=cwd)
            if ok:
                return True, f"Sesión de tmux '{s_name}' creada exitosamente."
            return True, f"No pude crear la sesión de tmux '{s_name}'."

        # ------------------- 9. TMUX KILL -------------------
        if intent == "tmux_kill":
            if session_name:
                ok = tmux_runtime.kill_by_name(session_name)
                if ok:
                    return True, f"Sesión de tmux '{session_name}' cerrada."
                return True, f"No encontré la sesión de tmux '{session_name}'."

        # ------------------- 10. SWITCH PROJECT -------------------
        if intent == "switch_project" and project_param:
            try:
                await self.project_service.select_project(user_id=user_id, project=project_param)
                reply = spoken_reply or f"Cambiado al proyecto {project_param}."
                return True, reply
            except ValueError:
                pass

        # ------------------- 11. DEV SESSION START -------------------
        if intent == "dev_session_start":
            target_proj = project_param or ctx["selected_project"]
            p_hint = None
            if provider_param:
                try:
                    p_hint = DevProvider(provider_param.lower())
                except Exception:
                    pass
            session = await self.dev_session_service.create_session(
                DevSessionCreateRequest(
                    project=target_proj,
                    user_id=user_id,
                    mode="peer",
                    provider=p_hint or DevProvider.AUTO,
                    title=f"Sesión dev ({target_proj})",
                    objective="Sesión dev creada desde FirstLayer Gateway.",
                )
            )
            await self.repo.set_active_dev_chat_session(
                user_id=user_id,
                session_id=session["session_id"],
                project=target_proj,
            )
            reply = spoken_reply or f"Sesión de desarrollo creada y activa en {target_proj}."
            return True, reply

        # ------------------- 12. DEV INSTRUCTION -------------------
        if intent == "dev_instruction":
            active_s = ctx.get("active_session")
            if active_s:
                session_id = str(active_s["session_id"])
                await self.dev_session_service.send_message_async(
                    session_id=session_id,
                    message=text,
                    user_id=user_id,
                )
                return True, "Instrucción enviada al agente de código en segundo plano."

        # ------------------- 13. CHAT RESPONSE -------------------
        if spoken_reply:
            return True, spoken_reply

        return False, ""
