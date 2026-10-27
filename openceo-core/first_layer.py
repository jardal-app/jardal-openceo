from __future__ import annotations

import asyncio
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
from horizon import classify as horizon_classify
from horizon import cockpit as horizon_cockpit
from horizon.models import CaptureChannel
from horizon.service import HorizonService
from project_service import ProjectService
from settings import Settings
from state import (
    ChatCompletionRequest,
    DevProvider,
    DevSessionControlRequest,
    DevSessionCreateRequest,
    DevSessionMessageRequest,
    DevSessionMode,
    DevSessionStatus,
    SessionControlAction,
    TaskStatus,
)
from task_service import TaskService

logger = logging.getLogger(__name__)

_PROJECT_TOKEN_STOPWORDS = {"jardal", "develop", "digital", "innovations", "app", "rest", "api"}

_ORDINAL_MAP: dict[str, int] = {
    "primero": 1,
    "primer": 1,
    "primera": 1,
    "1ro": 1,
    "1er": 1,
    "1ra": 1,
    "segundo": 2,
    "segunda": 2,
    "2do": 2,
    "2da": 2,
    "tercero": 3,
    "tercer": 3,
    "tercera": 3,
    "3ro": 3,
    "3er": 3,
    "3ra": 3,
    "cuarto": 4,
    "cuarta": 4,
    "4to": 4,
    "4ta": 4,
    "quinto": 5,
    "quinta": 5,
    "5to": 5,
    "5ta": 5,
    "sexto": 6,
    "sexta": 6,
    "6to": 6,
    "6ta": 6,
}


def _extract_project_from_text(text: str, project_names: list[str]) -> str | None:
    if not text or not project_names:
        return None
    lowered = text.lower().strip()

    # 1. Exact project name substring
    for name in sorted(project_names, key=len, reverse=True):
        if name.lower() in lowered:
            return name

    # 2. Distinctive token matching (e.g. 'trading' -> 'jardal-trading')
    best_match: str | None = None
    best_score = 0
    for name in project_names:
        tokens = [tok for tok in re.split(r"[^a-z0-9]+", name.lower()) if tok and len(tok) >= 3]
        tokens = [tok for tok in tokens if tok not in _PROJECT_TOKEN_STOPWORDS]
        if not tokens:
            continue
        score = sum(1 for tok in tokens if re.search(rf"\b{re.escape(tok)}\b", lowered))
        if score > best_score:
            best_score = score
            best_match = name
    if best_match and best_score > 0:
        return best_match

    # 3. Numerical project references: 'proyecto 1', 'proyecto nro 1', 'en el 1', 'sesion en el 2', '#1'
    num_patterns = [
        re.compile(r"\b(?:proyecto|repo|repositorio|project)\s*(?:n[roº\.]*|n[uú]mero|#)?\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:sesi[oó]n|session)\s+(?:dev\s+)?(?:en|al|para|del?)\s+(?:el\s+)?(?:proyecto\s+)?(?:n[roº\.]*|n[uú]mero|#)?\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:en|al|para|del?)\s+(?:el\s+)?(?:proyecto\s+)(?:n[roº\.]*|n[uú]mero|#)?\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:en|al|para|del?)\s+(?:el\s+)(?:n[roº\.]+|n[uú]mero|#)\s*(\d{1,2})\b", re.IGNORECASE),
        re.compile(r"\b(?:en|al)\s+el\s+(\d{1,2})\b", re.IGNORECASE),
    ]
    for pat in num_patterns:
        m = pat.search(lowered)
        if m:
            idx = int(m.group(1))
            if 1 <= idx <= len(project_names):
                return project_names[idx - 1]

    # 4. Ordinal project references: 'primer proyecto', 'el primero', 'segundo', etc.
    for word, idx in _ORDINAL_MAP.items():
        if (
            re.search(rf"\b(?:el\s+)?{word}\s+(?:proyecto|repo|repositorio|project)\b", lowered)
            or re.search(rf"\b(?:proyecto|repo|repositorio|project)\s+(?:el\s+)?{word}\b", lowered)
            or re.search(rf"\b(?:en|al|para|del?)\s+(?:el\s+)?{word}\b", lowered)
        ):
            if 1 <= idx <= len(project_names):
                return project_names[idx - 1]

    return None


def _resolve_preset_from_query(presets: dict[str, dict[str, Any]], query: str | int | None) -> dict[str, Any] | None:
    if not presets or query is None:
        return None
    raw = str(query).strip().lower()

    # Direct match by name or number string
    if raw in presets:
        return presets[raw]

    # Clean "preset" prefix: "preset review" -> "review", "preset 1" -> "1"
    cleaned = re.sub(r"^(?:el\s+)?preset\s*(?:n[roº\.]*|n[uú]mero|#)?\s*", "", raw).strip()
    if cleaned in presets:
        return presets[cleaned]

    # Match number in query
    m = re.search(r"\b(?:preset\s+)?(?:n[roº\.]*|n[uú]mero|#)?\s*(\d{1,2})\b", raw)
    if m and m.group(1) in presets:
        return presets[m.group(1)]

    # Ordinal match: "primer preset", "el primero"
    for ord_word, ord_idx in _ORDINAL_MAP.items():
        if ord_word in raw:
            if str(ord_idx) in presets:
                return presets[str(ord_idx)]

    # Partial name match
    for k, p in presets.items():
        if isinstance(p, dict) and p.get("name") and (p["name"] in raw or raw in p["name"]):
            return p

    return None


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
        horizon_service: HorizonService | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.project_service = project_service
        self.dev_session_service = dev_session_service
        self.task_service = task_service
        self.horizon = horizon_service
        self._finance_handler: Any | None = None
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

    def set_finance_handler(self, handler: Any | None) -> None:
        """Voz/chat -> skill exacta de finanzas (TASK-2026-00221).

        El handler recibe `(skill, texto)` y corre la skill exacta de
        forma sincrona; devuelve un resumen breve para voz. Sin handler,
        el turno sigue su curso normal.
        """
        self._finance_handler = handler

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

        # Presets for selected project
        presets = await self.project_service.get_project_presets(selected_project)

        # Open tasks for selected project
        open_tasks = await self.repo.get_open_tasks_for_project(selected_project, limit=6)
        open_count = await self.repo.count_open_tasks_for_project(selected_project)

        # Recent tasks for selected project
        recent_tasks = (
            await self.repo.tasks.find({"project": selected_project})
            .sort("created_at", -1)
            .limit(6)
            .to_list(6)
        )

        # Pending preset waiting for dictation
        pending_preset = await self.repo.get_pending_preset(user_id)

        return {
            "selected_project": selected_project,
            "active_ticket": active_ticket_doc,
            "active_session": active_session,
            "projects": projects,
            "all_tmux": all_tmux,
            "presets": presets,
            "open_tasks": open_tasks,
            "open_count": open_count,
            "recent_tasks": recent_tasks,
            "pending_preset": pending_preset,
        }

    def _build_context_prompt(self, ctx: dict[str, Any]) -> str:
        lines: list[str] = []

        lines.append("PROYECTOS DISPONIBLES (orden numérico 1-based para el usuario):")
        for idx, p in enumerate(ctx["projects"], start=1):
            lines.append(f"{idx}. {p['name']}: {p.get('title', p['name'])} (default: {p.get('default_provider', 'auto')})")

        lines.append(f"\nPROYECTO SELECCIONADO ACTUALMENTE: {ctx['selected_project']}")

        # Presets
        presets = ctx.get("presets", {})
        if presets:
            lines.append(f"\nPRESETS DE PROMPTS CONFIGURADOS EN {ctx['selected_project']} (en project.yaml):")
            unique_presets: dict[str, dict[str, Any]] = {}
            for p in presets.values():
                if isinstance(p, dict) and p.get("name") and p["name"] not in unique_presets:
                    unique_presets[p["name"]] = p
            for name, pr in unique_presets.items():
                p_num = pr.get("number")
                num_str = f"#{p_num} " if p_num else ""
                desc = f" ({pr.get('description')})" if pr.get('description') else ""
                prompt_snippet = (pr.get('prompt') or pr.get('cmd') or "")[:85]
                lines.append(f"- {num_str}'{name}': \"{prompt_snippet}\"{desc}")
        else:
            lines.append(f"\nPRESETS DE PROMPTS EN {ctx['selected_project']}: Ninguno configurado aún.")

        # Pending preset waiting for dictation
        pp = ctx.get("pending_preset")
        if pp:
            lines.append(
                f"\nPRESET PENDIENTE ESPERANDO DICTADO EN TERMINAL:"
                f"\n- Preset activo: '{pp.get('name')}' en la sesión tmux '{pp.get('session_name')}'"
                f"\n- Texto inicial ya escrito en pantalla (SIN ENTER): \"{pp.get('initial_prompt', '')[:100]}\""
                f"\n- El usuario puede dictar lo que sigue, o decir 'dale enter', o decir 'cancela'."
            )

        # Active ticket
        at = ctx.get("active_ticket")
        if at:
            num = at.get("ticket_number", "?")
            status = at.get("status", "unknown")
            title = at.get("title", "")
            err = at.get("error") or at.get("last_response_summary") or at.get("waiting_reason") or "ninguno"
            phase = at.get("phase", "dev_work")
            lines.append(
                f"\nTICKET ACTUALMENTE EN FOCO: #{num} [{status}] ({title}) - Fase: {phase} - Último detalle/error: {str(err)[:180]}"
            )
        else:
            lines.append("\nTICKET ACTUALMENTE EN FOCO: Ninguno")

        # Open tickets
        open_tasks = ctx.get("open_tasks", [])
        open_count = ctx.get("open_count", 0)
        lines.append(f"\nTICKETS ABIERTOS EN {ctx['selected_project']} (TOTAL: {open_count}):")
        if open_tasks:
            for t in open_tasks:
                t_num = t.get("ticket_number", "?")
                t_st = t.get("status", "unknown")
                t_tit = str(t.get("title", ""))[:60]
                lines.append(f"- #{t_num} [{t_st}]: {t_tit}")
        else:
            lines.append("- No hay tickets abiertos pendientes.")

        # Active dev session
        s = ctx.get("active_session")
        if s:
            lines.append(
                f"\nSESIÓN DEV ACTIVA: {s.get('tmux_session_name', s.get('session_id'))} [{s.get('status')}] en {s.get('project')}"
            )
        else:
            lines.append("\nSESIÓN DEV ACTIVA: Ninguna (modo conversación)")

        # Tmux sessions
        tmux_list = ctx.get("all_tmux", [])
        if tmux_list:
            tmux_summary = [f"{t['name']}" + (" (adjunto)" if t.get("attached") else "") for t in tmux_list]
            lines.append(f"\nSESIONES TMUX EN EL SERVIDOR: [{', '.join(tmux_summary)}]")
        else:
            lines.append("\nSESIONES TMUX EN EL SERVIDOR: []")

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
        if not message.strip():
            return False, ""

        text = message.strip()

        # Horizon: clasificacion determinista ANTES del modelo por dos razones.
        # 1) Con una sesion dev activa, el prompt trata cualquier frase como
        #    dev_instruction y la escribe en tmux. Una nota personal no debe
        #    terminar ahi por ambiguedad del clasificador.
        # 2) La seccion 9.7 exige que captura, calculos y consulta sigan
        #    funcionando con el presupuesto de IA agotado. Va antes del guard
        #    del LLM a proposito: sin modelo, el registro sigue vivo.
        if self.horizon is not None:
            horizon_intent = horizon_classify.classify(text)
            if horizon_intent:
                handled, reply = await self._handle_horizon(user_id, horizon_intent, text)
                if handled:
                    return True, reply

        if self._llm is None:
            return False, ""

        ctx = await self.gather_context(user_id)
        context_str = self._build_context_prompt(ctx)

        system_prompt = f"""Eres la Primera Capa Cognitiva y Asistente Ejecutivo de OpenCEO.
El usuario te habla desde su móvil o por voz mientras camina en la calle o trabaja en su PC, gestionando proyectos, tickets, sesiones de código y comandos de terminal.

CONTEXTO ACTUAL DEL WORKSPACE:
{context_str}

PERSONALIDAD Y TONO DE VOZ (CRÍTICO):
1. Hablas en español de forma totalmente NATURAL, fluida, cercana y profesional, como si fueras un socio técnico o director de operaciones conversando con su colega.
2. Usas SIEMPRE la primera persona del plural impersonal ("Tenemos...", "Estamos en el proyecto...", "Veo que tenemos...", "Tenemos abiertos 3 tickets...").
3. Respuestas MUY CORTAS, concisas y directas para VOZ (máximo 1 o 2 oraciones, leídas por TTS): NUNCA devuelvas tablas markdown, listas largas, UUIDs, URLs, diffs ni fragmentos de código. NUNCA leas ni repitas en voz alta el contenido de los prompts, git diffs ni nombres de sesiones tmux. Sé breve como una persona real al teléfono o por radio.
4. Si el usuario pregunta "¿cuántos tickets tenemos abiertos?", "¿qué tickets hay?" o similar:
   - Responde inmediatamente en primera persona plural con el número exacto y un detalle CORTO de los tickets abiertos.
   - Ejemplo: "Tenemos abiertos 3 tickets en {ctx['selected_project']}: el número 48 sobre trading en progreso, el 49 sobre la VPN esperando revisión, y el 50 pendiente de inicio. ¿Quieres que enfoquemos alguno o que veamos qué pasó?"
5. Si el usuario está PERDIDO, no sabe qué hacer, pide 'ayuda', dice 'estoy perdido', '¿qué puedo hacer?' o '¿dónde estamos?':
   - Marca intent="guidance".
   - En spoken_reply guíalo activamente mencionando el proyecto actual, cuántos tickets abiertos tenemos, qué presets hay listos y dale 3 o 4 sugerencias concretas que pueda pedirte por voz.
6. Si el usuario dice algo confuso, incomprensible o que no parece un comando claro:
   - En spoken_reply responde de forma amable y colaborativa: "No te entendí bien esa última frase. ¿Querías consultar los tickets, ejecutar un preset de prompts para la sesión dev, o abrir una sesión nueva? Dime y te ayudo."
7. PRESETS DE PROMPTS Y DICTADO EN TERMINAL TMUX:
   Los presets están configurados manualmente en project.yaml. Son prompts iniciales diseñados para escribirse en la terminal tmux del proyecto SIN dar Enter, para que luego el usuario dicte el resto.
   - CARGAR/ESCRIBIR UN PRESET ('pon el preset review', 'escribe el preset test_fix', 'carga el preset refactor', 'preset review'):
     marca intent="preset_load" con preset_name.
     Si el usuario ya dicta qué agregar en la misma frase (ej: 'pon el preset review y revisa los tests'): incluye dictation="revisa los tests".
     Si el usuario dice explícitamente que le dé enter ('y dale enter', 'y ejecútalo'): send_enter=true. De lo contrario, SIEMPRE send_enter=false (NO DAR ENTER).
   - DICTAR LA CONTINUACIÓN (cuando hay un preset pendiente o el usuario continúa dictando la instrucción técnica):
     marca intent="preset_dictate" con dictation="texto dictado" y send_enter (true si pide enter o terminar, false si solo sigue dictando).
   - DAR ENTER / ENVIAR ('dale enter', 'envíalo', 'mándalo', 'ejecútalo ya', 'aprieta enter'):
     marca intent="tmux_enter".
   - CANCELAR / LIMPIAR LÍNEA ('cancela el preset', 'borra la línea', 'limpia la terminal', 'cancela'):
     marca intent="preset_cancel".
   - LISTAR PRESETS ('qué presets tenemos', 'cuáles son los presets', 'lista los presets'):
     marca intent="preset_list".
   - CONSULTAR SALIDA / PANTALLA ('qué sale en pantalla', 'cómo va la sesión', 'qué respondió'):
     marca intent="dev_output".
8. FOCO DE TICKETS:
   - 'vamos al ticket 48', 'enfoca el 49', 'cómo va el 48' -> intent="focus_ticket" con ticket_number.
   - '¿por qué falló?', '¿qué falta?', '¿qué cambios pide?' con ticket en foco -> intent="ticket_query".
   - 'reintenta el ticket', 'ejecuta el ticket' -> intent="ticket_retry".
   - 'crea un objetivo para ...', 'nuevo objetivo: ...', 'quiero un objetivo que ...' -> intent="objective_create" con dictation igual al objetivo pedido y project (el proyecto actual salvo que nombre otro). Un objetivo genera sus tickets solo; no pidas aprobación por ticket.
   - 'crea una misión para ...', 'nueva misión: ...', 'haz andar ...', 'quiero una misión que ...' -> intent="mission_create" con dictation igual a la misión pedida y project (el proyecto actual salvo que nombre otro). Una misión encadena sus objetivos sola; no pidas aprobación por objetivo.
9. SESIONES DEV (CÓDIGO/PROGRAMACIÓN) Y TERMINALES TMUX:
   - 'crea una sesión en jardal-trading', 'cree una sesión en jardal-trading', 'abre una sesión en trading', 'crea una sesión dev', 'abre sesión con codex|claude|muse|opencode' -> SIEMPRE intent="dev_session_start" con project y opcional provider. Toda petición de crear/abrir sesión para un proyecto de código es una sesión dev.
   - 'crea una sesión en el proyecto nro 1', 'crea sesión en el 1', 'crea una sesión en el proyecto 1', 'abre sesión en el 2', 'crea sesión en el nro 1' -> intent="dev_session_start" con el project correspondiente al número según la lista 'PROYECTOS DISPONIBLES' (ej: 1 -> jardal-openceo, 2 -> jardal-trading, etc.).
   - 'cierra la sesión dev', 'elimina la sesión dev', 'termina la sesión' -> intent="dev_session_stop".
   - instrucción directa de código con sesión dev -> intent="dev_instruction" con instruction.
   - 'qué sesiones de tmux hay', 'mis tmux', 'qué sesiones hay' -> intent="list_tmux".
   - 'qué sale en tr', 'pantalla de investor' -> intent="tmux_inspect" con session_name.
   - 'manda git status a tr' -> intent="tmux_send" con session_name y keys.
   - 'crea una terminal tmux llamada monitor', 'abre un tmux limpio llamado test' -> intent="tmux_create" con session_name (SOLO para terminales tmux genéricas explícitas, NUNCA para sesiones de proyectos de desarrollo).
10. PROYECTOS Y NÚMEROS DE PROYECTO:
   - Los proyectos están numerados del 1 al N en la lista 'PROYECTOS DISPONIBLES'.
   - El usuario puede referirse a ellos por su número o nombre: 'proyecto 1', 'proyecto nro 1', 'en el 1', 'el primero', 'proyecto 2', etc.
   - 'cambia al proyecto trading', 'selecciona melanyryr', 'vamos a cebitec', 'cambia al 1', 'vamos al proyecto 2' -> intent="switch_project" con project resuelto al nombre oficial.
   - 'qué proyectos hay', 'cuáles son los proyectos', 'lista los proyectos', 'qué proyectos tenemos' -> intent="list_projects".

11. CONTEXTO PERSONAL (HORIZON) — TU VIDA, NO EL CÓDIGO:
   El CEO incorpora Horizon: tu rumbo personal (empleo, voz, dinero, familia, salud, descanso). No es otro agente ni otra sesión.
   - Ideas, pendientes, avances o compromisos sobre tu vida ('quiero hacer un videojuego bíblico', 'el cliente me pidió otro cambio', 'anota que debo revisar el contrato', 'se me ocurrió un negocio de rescate animal') -> intent="horizon_capture" con dictation igual al texto original del usuario.
   - Cierres o avances medibles ('grabé 4 minutos útiles', 'terminé el CV', 'me pagaron el remoto', 'hice 30 minutos de ejercicio') -> intent="horizon_progress" con dictation igual al texto original.
   - 'qué sigue', 'cuál es la siguiente acción', 'en qué me concentro', 'por dónde empiezo' -> intent="horizon_next_action".
   - 'cómo va el plan', 'qué objetivos tenemos', 'cuál es la misión', 'mi plan de hoy' -> intent="horizon_plan_query".
   - Disponibilidad del día ('hoy solo tengo 15 minutos', 'hoy no puedo', 'se me complicó el día') -> intent="horizon_capacity" con dictation igual al texto original. Es una DECLARACIÓN DE CAPACIDAD, no un avance: nunca la clasifiques como horizon_progress.
   - 'planifica mi día', 'prepara el plan', 'qué hago hoy', 'plan del día' -> intent="horizon_plan_day".
   - 'revisa la semana', 'cómo va la semana', 'revisión semanal' -> intent="horizon_review".
   - 'importa mi contexto', 'trae mi contexto' -> intent="horizon_import".
   - 'delega la siguiente acción', 'encarga la siguiente tarea', 'haz la siguiente acción en dev' -> intent="horizon_delegate".
   - 'qué resultado dio la tarea', 'cómo va lo delegado', 'qué entregó el dev' -> intent="horizon_delivery".
   - 'anota 15 minutos de supervisión', 'fueron 10 minutos de retrabajo' -> intent="horizon_metrics".
   - 'brief del día', 'resumen de hoy', 'qué avanzó el CEO', 'cómo va la empresa' -> intent="horizon_brief" (cockpit: plan + empresa + decisiones).
   - 'decisiones pendientes', 'qué espera mi decisión', 'qué tengo que decidir', 'bandeja requiere humano' -> intent="horizon_decisions".
   - 'aprueba la decisión', 'rechaza el escalado', 'aprueba la primera', 'rechaza el ticket 170' -> intent="horizon_decide" con dictation igual al texto original. Solo con verbo aprueba/rechaza MÁS sustantivo (decisión/escalado/aprobación), ordinal o 'ticket N'. Un 'sí'/'dale' suelto NUNCA es horizon_decide. 'Autorizo ...' es un compromiso personal -> horizon_capture, nunca horizon_decide.
   REGLA DE ORO: una frase sobre tu vida personal NUNCA es una instrucción de código. Ante duda entre dev_instruction y horizon_capture, si la frase habla de tu vida, tu familia, tu dinero, tu salud, tu voz, tus clientes o una idea tuya, elige horizon_capture. NUNCA envíes notas personales a tmux.

12. FINANZAS (Jardal, Jardal ERP) — intent="finance_skill" con skill y dictation igual al texto original:
   Traduce el pedido a una skill exacta de finance; el código la valida contra el catálogo y la corre sin tickets (el registro o la consulta es el efecto).
   - Dinero que SALE con monto (gastar, pagar, comprar, "pago de sueldo 3000") -> skill="registrar-gasto".
   - Dinero que ENTRA con monto y señal real de cobro ("me pagaron", "cobré", "recibí", "sueldo 6200") -> skill="registrar-ingreso". Ojo: "pagaron" desnudo y sustantivos con complemento ("honorarios del contador", "sueldo del empleado") son GASTO, no ingreso.
   - Diezmo, diez por ciento, aporte, donación -> skill="regla-10" (gana sobre gasto: "registra mi diezmo 500" es regla-10).
   - Preguntas de saldo, totales o resúmenes de gasto ("cuánto gastamos este mes", "dame el saldo") -> skill="consultar-resumen". Una pregunta NUNCA registra.
   - Pagar la luz de la casa (Elfec House, práctica segura, nunca paga de verdad) -> skill="pago-elfec".
   - Sin monto no hay registro: "registra gasto carne" (sin monto) también es finance_skill con skill="registrar-gasto" y el dictado completo; el flujo pide el monto.

Responde SIEMPRE en formato JSON estricto con esta estructura:
{{
  "intent": "focus_ticket" | "ticket_query" | "ticket_count" | "ticket_retry" | "objective_create" | "list_tickets" | "preset_load" | "preset_dictate" | "preset_list" | "tmux_enter" | "preset_cancel" | "list_tmux" | "tmux_inspect" | "tmux_send" | "tmux_create" | "tmux_kill" | "dev_session_start" | "dev_session_stop" | "dev_instruction" | "dev_output" | "switch_project" | "list_projects" | "horizon_capture" | "horizon_next_action" | "horizon_progress" | "horizon_plan_query" | "horizon_capacity" | "horizon_plan_day" | "horizon_review" | "horizon_import" | "horizon_delegate" | "horizon_delivery" | "horizon_metrics" | "horizon_brief" | "horizon_decisions" | "horizon_decide" | "finance_skill" | "guidance" | "chat_response",
  "ticket_number": int | null,
  "session_name": string | null,
  "project": string | null,
  "provider": string | null,
  "preset_name": string | null,
  "dictation": string | null,
  "skill": string | null,
  "send_enter": bool,
  "keys": string | null,
  "instruction": string | null,
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
        preset_name = parsed.get("preset_name")
        dictation_param = parsed.get("dictation")
        skill_param = parsed.get("skill")
        send_enter_param = parsed.get("send_enter", False)
        keys_param = parsed.get("keys")
        instruction_param = parsed.get("instruction")

        tmux_runtime = self.dev_session_service._tmux

        # Accurate project resolution using text analysis and fuzzy token matching
        project_names = [p["name"] for p in ctx["projects"]]
        extracted_proj = _extract_project_from_text(text, project_names)
        if not extracted_proj and project_param:
            extracted_proj = _extract_project_from_text(str(project_param), project_names)
        if not extracted_proj and session_name:
            extracted_proj = _extract_project_from_text(str(session_name), project_names)
        target_proj = extracted_proj or ctx["selected_project"]

        # Safeguard: If classified as tmux_create but refers to a known project or asks for a session,
        # redirect to dev_session_start to prevent creating orphaned raw tmux terminals
        if intent == "tmux_create":
            candidate_proj = extracted_proj or _extract_project_from_text(str(session_name or ""), project_names)
            looks_like_dev = any(w in text.lower() for w in ["sesion", "sesión", "dev", "codigo", "código", "programar", "desarrollo"])
            if candidate_proj or looks_like_dev:
                intent = "dev_session_start"
                target_proj = candidate_proj or target_proj

        # ------------------- 1. FOCUS TICKET -------------------
        if intent == "focus_ticket":
            if isinstance(ticket_num, int):
                task = await self.repo.get_task_by_ticket_number(ticket_num, project=target_proj)
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
                reply = f"El ticket #{t_num} está en estado {t_st}."
                if t_err:
                    reply += f" Detalle: {str(t_err)[:150]}."
                return True, reply
            return True, "No tenemos ningún ticket enfocado ahora mismo. Dime cuál quieres enfocar diciendo por ejemplo 'vamos al ticket 48'."

        # ------------------- 3. TICKET RETRY -------------------
        if intent == "ticket_retry":
            at = ctx.get("active_ticket")
            target_task = None
            if isinstance(ticket_num, int):
                target_task = await self.repo.get_task_by_ticket_number(ticket_num, project=target_proj)
            elif at:
                target_task = at

            if target_task:
                task_id = target_task["task_id"]
                t_num = target_task.get("ticket_number", ticket_num)
                proj = target_task.get("project", target_proj)
                try:
                    await self.task_service.retry_task(task_id)
                except Exception:
                    pass
                msg = spoken_reply or f"Reintentando ticket #{t_num} en {proj} en segundo plano."
                return True, msg
            return True, "No encontré el ticket que quieres reintentar. Di por ejemplo: reintenta el ticket 48."

        # ------------------- 3b. OBJECTIVE CREATE (F2) -------------------
        if intent == "objective_create":
            goal = str(dictation_param or "").strip() or text.strip()
            try:
                await self.task_service.create_objective(
                    project=target_proj,
                    goal=goal,
                    source="voice",
                    requester=user_id,
                )
            except Exception as exc:
                return True, (
                    "No pudimos crear el objetivo en "
                    f"{target_proj}: {str(exc)[:160]}. ¿Lo replanteamos?")
            return True, (
                f"Tenemos un objetivo nuevo en marcha en {target_proj}: "
                "el planificador ya está creando sus tickets. "
                "Te aviso cuando cierre por revisión final.")

        # ------------------- 3b2. MISSION CREATE (00218) -------------------
        if intent == "mission_create":
            goal = str(dictation_param or "").strip() or text.strip()
            try:
                await self.task_service.create_mission(
                    project=target_proj,
                    goal=goal,
                    source="voice",
                    requester=user_id,
                )
            except Exception as exc:
                return True, (
                    "No pudimos crear la misión en "
                    f"{target_proj}: {str(exc)[:160]}. ¿La replanteamos?")
            return True, (
                f"Tenemos una misión nueva en marcha en {target_proj}: "
                "la estrategia ya está encadenando sus objetivos. "
                "Te aviso cuando cierre con su evidencia.")

        # ------------------- 3c. FINANCE SKILL (explicit voice/chat) -------------------
        if intent == "finance_skill":
            skill_name = str(skill_param or "").strip()
            said = str(dictation_param or "").strip() or text.strip()
            try:
                from catalog import load_catalog

                card = load_catalog().agents.get("finance")
                valid = bool(card is not None and card.enabled
                             and skill_name in list(card.skills or []))
            except Exception:
                logger.warning("Finance skill validation failed",
                               exc_info=True)
                return True, (
                    "No pude validar la operación de finanzas ahora "
                    "mismo. Lo intentamos de nuevo.")
            if self._finance_handler is None:
                return True, (
                    spoken_reply
                    or "Finanzas no está disponible ahora mismo.")
            if not valid:
                return True, (
                    spoken_reply
                    or "No reconocí esa operación de finanzas. "
                    "Dime por ejemplo: registra un gasto, anota un "
                    "ingreso, cuánto gastamos este mes o cómo vamos "
                    "con el diezmo.")
            try:
                summary = await self._finance_handler(skill_name, said)
            except Exception:
                logger.warning("Finance skill run failed", exc_info=True)
                return True, (
                    "No pude resolver eso con finanzas ahora mismo. "
                    "Lo intentamos de nuevo.")
            if summary:
                return True, str(summary)
            return True, (
                spoken_reply
                or "No pude resolver eso con finanzas ahora mismo. "
                "Lo intentamos de nuevo.")

        # ------------------- 4. TICKET COUNT & LIST TICKETS -------------------
        if intent in ("ticket_count", "list_tickets"):
            if spoken_reply and "tenemos" in spoken_reply.lower() and len(spoken_reply) > 20:
                return True, spoken_reply

            open_count = await self.repo.count_open_tasks_for_project(target_proj)
            open_tasks = await self.repo.get_open_tasks_for_project(target_proj, limit=4)

            if open_count == 0:
                return True, f"No tenemos ningún ticket abierto en {target_proj}. Todo está al día. ¿Quieres crear un ticket nuevo o cambiar de proyecto?"

            parts = []
            for t in open_tasks[:3]:
                num = t.get("ticket_number", "?")
                st = t.get("status", "en proceso")
                tit = str(t.get("title", ""))[:45]
                parts.append(f"el #{num} de '{tit}' en estado {st}")
            summary = ", ".join(parts)
            return True, f"Tenemos abiertos {open_count} tickets en {target_proj}: {summary}. ¿Quieres que enfoquemos alguno o veamos su detalle?"

        # ------------------- 5. PRESET LOAD / WRITE (WITHOUT ENTER) -------------------
        if intent in ("preset_load", "preset_run"):
            presets = await self.project_service.get_project_presets(target_proj)
            pr = _resolve_preset_from_query(presets, preset_name) or _resolve_preset_from_query(presets, text)

            if pr:
                p_name = pr["name"]
                prompt = pr.get("prompt") or pr.get("cmd") or ""

                # Resolve target tmux session: use active session if alive, or create automatically
                active_s = await self.repo.find_active_project_session(target_proj, user_id=user_id)
                target_tmux = None
                if active_s and active_s.get("tmux_session_name") and tmux_runtime.has_session_name(active_s["tmux_session_name"]):
                    target_tmux = active_s["tmux_session_name"]
                elif session_name and tmux_runtime.has_session_name(session_name):
                    target_tmux = session_name
                elif tmux_runtime.has_session_name(f"openceo-dev-{target_proj}"):
                    target_tmux = f"openceo-dev-{target_proj}"

                if not target_tmux:
                    try:
                        created = await self.dev_session_service.create_session(
                            DevSessionCreateRequest(
                                project=target_proj,
                                user_id=user_id,
                                mode=DevSessionMode.PEER,
                                provider=DevProvider.AUTO,
                                title=f"Sesión dev ({target_proj})",
                                objective=f"Preset {p_name}: {pr.get('description', '')}",
                            )
                        )
                        target_tmux = created.get("tmux_session_name") or f"openceo-dev-{target_proj}"
                        await self.repo.set_active_dev_chat_session(user_id, created["session_id"], target_proj)
                    except Exception:
                        target_tmux = f"openceo-dev-{target_proj}"
                        if not tmux_runtime.has_session_name(target_tmux):
                            tmux_runtime.create_generic_session(target_tmux)

                text_to_write = prompt
                if dictation_param:
                    sep = " " if not text_to_write.endswith(" ") else ""
                    text_to_write += sep + dictation_param.strip()

                should_enter = bool(send_enter_param)
                tmux_runtime.send_keys_by_name(target_tmux, text_to_write, enter=should_enter)

                if not should_enter:
                    await self.repo.set_pending_preset(
                        user_id=user_id,
                        preset_name=p_name,
                        session_name=target_tmux,
                        initial_prompt=text_to_write,
                        project=target_proj,
                    )
                    return True, f"Listo, preset {p_name} en pantalla en {target_proj}. Dime qué le agregamos."
                else:
                    await self.repo.clear_pending_preset(user_id)
                    return True, f"Listo, preset {p_name} enviado en {target_proj}."
            else:
                unique_names: list[str] = []
                for p in presets.values():
                    if isinstance(p, dict) and p.get("name") and p["name"] not in [x.split()[-1] for x in unique_names]:
                        p_num = p.get("number")
                        num_prefix = f"{p_num} " if p_num else ""
                        unique_names.append(f"{num_prefix}{p['name']}")
                names = ", ".join(unique_names) if unique_names else "ninguno"
                return True, f"No encontré ese preset. Los presets disponibles en {target_proj} son: {names}."

        # ------------------- 5b. PRESET DICTATE (CONTINUATION) -------------------
        if intent == "preset_dictate":
            pending = await self.repo.get_pending_preset(user_id)
            target_tmux = (pending.get("session_name") if pending else None) or session_name or f"openceo-dev-{target_proj}"
            dict_text = (dictation_param or text).strip()
            should_enter = bool(send_enter_param)

            if tmux_runtime.has_session_name(target_tmux):
                sep = " "
                tmux_runtime.send_keys_by_name(target_tmux, sep + dict_text, enter=should_enter)

                if should_enter:
                    await self.repo.clear_pending_preset(user_id)
                    return True, "Listo, agregado y enviado."
                else:
                    return True, "Anotado. ¿Algo más o le damos enter?"
            return True, "No encontré la terminal abierta para continuar el dictado."

        # ------------------- 5c. TMUX ENTER / PRESET SUBMIT -------------------
        if intent in ("tmux_enter", "preset_submit"):
            pending = await self.repo.get_pending_preset(user_id)
            target_tmux = (pending.get("session_name") if pending else None) or session_name or f"openceo-dev-{target_proj}"
            if tmux_runtime.has_session_name(target_tmux):
                tmux_runtime.send_keys_by_name(target_tmux, "", enter=True)
                await self.repo.clear_pending_preset(user_id)
                return True, "Listo, enviado."
            return True, "No encontré ninguna terminal activa para darle enter."

        # ------------------- 5d. PRESET CANCEL / CLEAR LINE -------------------
        if intent in ("preset_cancel", "tmux_clear"):
            pending = await self.repo.get_pending_preset(user_id)
            target_tmux = (pending.get("session_name") if pending else None) or session_name or f"openceo-dev-{target_proj}"
            if tmux_runtime.has_session_name(target_tmux):
                tmux_runtime.send_keys_by_name(target_tmux, "C-u", enter=False)
            await self.repo.clear_pending_preset(user_id)
            return True, "Listo, borrado."

        # ------------------- 6. PRESET LIST -------------------
        if intent == "preset_list":
            presets = await self.project_service.get_project_presets(target_proj)
            if not presets:
                return True, f"No tenemos presets configurados en {target_proj}."
            unique_names = []
            for p in presets.values():
                if isinstance(p, dict) and p.get("name") and p["name"] not in [x.split()[-1] for x in unique_names]:
                    p_num = p.get("number")
                    num_prefix = f"{p_num} " if p_num else ""
                    unique_names.append(f"{num_prefix}{p['name']}")
            return True, f"En {target_proj} tenemos los presets: {', '.join(unique_names)}. ¿Cuál ponemos?"

        # ------------------- 8. PRESET OUTPUT / DEV OUTPUT -------------------
        if intent in ("preset_output", "dev_output"):
            active_s_id, active_s_proj = await self.repo.get_active_dev_chat_session(user_id)
            target_s = None
            if active_s_id:
                target_s = await self.repo.get_dev_session(active_s_id)
            if not target_s:
                target_s = await self.repo.find_active_project_session(target_proj, user_id=user_id)

            if target_s:
                status = target_s.get("status", "unknown")
                last_reply = target_s.get("last_response") or target_s.get("last_response_summary")
                tmux_name = target_s.get("tmux_session_name") or f"openceo-dev-{target_s.get('project', target_proj)}"

                if status in (DevSessionStatus.COMPLETED.value, "completed") and last_reply:
                    clean_reply = str(last_reply).strip()
                    if len(clean_reply) > 180:
                        clean_reply = clean_reply[:180] + "..."
                    return True, f"La sesión dev completó su tarea. Respondió: {clean_reply}."

                if status in (DevSessionStatus.RUNNING.value, "running"):
                    pane_text = tmux_runtime.capture_pane_by_name(tmux_name, max_lines=6)
                    if pane_text:
                        lines = [l.strip() for l in pane_text.splitlines() if l.strip()]
                        tail = " | ".join(lines[-2:]) if lines else "procesando"
                        return True, f"La sesión dev sigue trabajando en segundo plano en {target_s.get('project')}. En la terminal tmux se ve: {tail}."
                    return True, f"La sesión dev sigue trabajando en segundo plano en el proyecto {target_s.get('project')}."

                if status in (DevSessionStatus.WAITING_USER.value, "waiting_user"):
                    reason = target_s.get("waiting_reason") or last_reply or "confirmación"
                    return True, f"La sesión dev está esperando tu respuesta. Motivo: {str(reason)[:120]}."

                if status in (DevSessionStatus.FAILED.value, "failed"):
                    err = target_s.get("waiting_reason") or "error en la ejecución"
                    return True, f"La sesión dev falló con el siguiente detalle: {str(err)[:120]}."

                if last_reply:
                    return True, f"Estado de la sesión dev: {status}. Última respuesta: {str(last_reply)[:140]}."
                return True, f"La sesión dev en {target_proj} está en estado {status}."

            return True, f"No tenemos ninguna sesión dev activa en {target_proj}. Puedes iniciar una diciendo 'crea una sesión dev' o 'pon el preset review'."

        # ------------------- 9. GUIDANCE (USUARIO PERDIDO O CONFUNDIDO) -------------------
        if intent == "guidance":
            if spoken_reply and len(spoken_reply) > 30:
                return True, spoken_reply
            open_count = ctx.get("open_count", 0)
            presets = ctx.get("presets", {})
            pr_sample = list(presets.keys())[0] if presets else "review"
            at = ctx.get("active_ticket")
            foco_str = f" con el ticket #{at['ticket_number']} en foco" if at else ""
            return True, (
                f"Estamos en el proyecto {target_proj}{foco_str}. Tenemos {open_count} tickets abiertos y {len(presets)} presets listos. "
                f"Puedes pedirme por ejemplo: '¿cuántos tickets tenemos abiertos?', 'vamos al ticket 48', "
                f"'pon el preset {pr_sample}' para escribirlo en la terminal y dictarme el resto, "
                f"o 'qué presets tenemos'. ¿Qué te gustaría hacer?"
            )

        # ------------------- 10. LIST TMUX -------------------
        if intent == "list_tmux":
            all_tmux = tmux_runtime.list_all_host_sessions()
            if not all_tmux:
                return True, "No hay sesiones de tmux activas en este servidor."
            names = [s["name"] for s in all_tmux]
            return True, f"Tenemos {len(names)} sesiones de tmux activas: {', '.join(names)}."

        # ------------------- 11. TMUX INSPECT -------------------
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

        # ------------------- 12. TMUX SEND -------------------
        if intent == "tmux_send":
            if session_name and keys_param:
                ok = tmux_runtime.send_keys_by_name(session_name, keys_param, enter=True)
                if ok:
                    return True, f"Comando enviado a tmux {session_name}."
                return True, f"No pude enviar el comando a {session_name}."

        # ------------------- 13. TMUX CREATE -------------------
        if intent == "tmux_create":
            s_name = session_name or f"session-{len(ctx.get('all_tmux', [])) + 1}"
            cwd = None
            if target_proj and self.project_service._config_loader.has_project(target_proj):
                p_cfg = self.project_service._config_loader.load_project(target_proj)
                repos = p_cfg.get("project", {}).get("repos", [])
                if repos and isinstance(repos[0], dict) and repos[0].get("local_path"):
                    cwd = repos[0]["local_path"]
                else:
                    cwd = p_cfg.get("dev", {}).get("providers", {}).get("codex", {}).get("cwd")
            ok = tmux_runtime.create_generic_session(s_name, cwd=cwd)
            if ok:
                return True, f"Sesión de tmux '{s_name}' creada exitosamente."
            return True, f"No pude crear la sesión de tmux '{s_name}'."

        # ------------------- 14. TMUX KILL -------------------
        if intent == "tmux_kill":
            if session_name:
                ok = tmux_runtime.kill_by_name(session_name)
                if ok:
                    return True, f"Sesión de tmux '{session_name}' cerrada."
                return True, f"No encontré la sesión de tmux '{session_name}'."

        # ------------------- 15. SWITCH PROJECT -------------------
        if intent == "switch_project":
            switch_target = project_param or target_proj
            if switch_target:
                try:
                    await self.project_service.select_project(user_id=user_id, project=switch_target)
                    open_c = await self.repo.count_open_tasks_for_project(switch_target)
                    pr_count = len(await self.project_service.get_project_presets(switch_target))
                    return True, f"Cambiado al proyecto {switch_target}. Tenemos {open_c} tickets abiertos y {pr_count} presets configurados. ¿Por dónde empezamos?"
                except ValueError:
                    return True, f"No encontré el proyecto {switch_target}. Los proyectos disponibles son: {', '.join([p['name'] for p in ctx['projects']])}."

        # ------------------- 15b. LIST PROJECTS -------------------
        if intent == "list_projects":
            proj_lines = [f"{i} {p['name']}" for i, p in enumerate(ctx["projects"], start=1)]
            curr = ctx["selected_project"]
            return True, f"Tenemos {len(ctx['projects'])} proyectos: {', '.join(proj_lines)}. Ahora mismo estamos en {curr}. ¿A cuál vamos o en cuál abrimos sesión?"

        # ------------------- 16. DEV SESSION START -------------------
        if intent == "dev_session_start":
            p_hint = None
            if provider_param:
                try:
                    p_hint = DevProvider(provider_param.lower())
                except Exception:
                    pass

            # Sync active project in user context if switching to a new project
            if target_proj != ctx["selected_project"]:
                try:
                    await self.project_service.select_project(user_id=user_id, project=target_proj)
                except Exception:
                    pass

            # Check if there is already an active dev session running for this project
            active_s = await self.repo.find_active_project_session(target_proj, user_id=user_id)
            if active_s and self.dev_session_service._tmux.has_session(target_proj):
                await self.repo.set_active_dev_chat_session(
                    user_id=user_id,
                    session_id=active_s["session_id"],
                    project=target_proj,
                )
                return True, f"Tenemos la sesión dev en {target_proj} activa y lista."

            session = await self.dev_session_service.create_session(
                DevSessionCreateRequest(
                    project=target_proj,
                    user_id=user_id,
                    mode=DevSessionMode.PEER,
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

        # ------------------- 16b. DEV SESSION STOP -------------------
        if intent == "dev_session_stop":
            active_s_id, active_s_proj = await self.repo.get_active_dev_chat_session(user_id)
            if active_s_id:
                try:
                    await self.dev_session_service.control_session(
                        session_id=active_s_id,
                        payload=DevSessionControlRequest(user_id=user_id, action=SessionControlAction.CANCEL),
                    )
                except Exception:
                    pass
                await self.repo.clear_active_dev_chat_session(user_id)
                return True, f"Sesión dev de {active_s_proj} cerrada y cancelada."
            return True, "No tenemos ninguna sesión dev activa para cerrar."

        # ------------------- 17. DEV INSTRUCTION -------------------
        if intent == "dev_instruction":
            active_s = ctx.get("active_session")
            target_sid = None
            if active_s:
                target_sid = str(active_s["session_id"])
            else:
                active_s_id, active_s_proj = await self.repo.get_active_dev_chat_session(user_id)
                if active_s_id and active_s_proj == target_proj:
                    target_sid = active_s_id
                else:
                    active_doc = await self.repo.find_active_project_session(target_proj, user_id=user_id)
                    if active_doc:
                        target_sid = active_doc["session_id"]
                        await self.repo.set_active_dev_chat_session(user_id, target_sid, target_proj)

            if target_sid:
                instruction_text = instruction_param or text
                await self.dev_session_service.send_message_async(
                    session_id=target_sid,
                    payload=DevSessionMessageRequest(user_id=user_id, message=instruction_text),
                )
                return True, "Instrucción enviada al agente de código en segundo plano."
            else:
                return True, f"No tenemos una sesión dev abierta en {target_proj}. Puedes decirme 'crea una sesión dev' o pedir un preset como 'ejecuta el preset review'."

        # ------------------- 17b. HORIZON (CONTEXTO PERSONAL) -------------------
        # Segunda capa: el modelo reconoce frases libres que el clasificador
        # determinista no cubre. El enrutamiento a Horizon ocurre antes de
        # cualquier rama de tmux o sesion dev.
        if intent in (
            "horizon_capture",
            "horizon_next_action",
            "horizon_progress",
            "horizon_plan_query",
            "horizon_capacity",
            "horizon_plan_day",
            "horizon_review",
            "horizon_import",
            "horizon_delegate",
            "horizon_delivery",
            "horizon_metrics",
            "horizon_brief",
            "horizon_decisions",
            "horizon_decide",
        ):
            horizon_text = (dictation_param or text).strip()
            handled, reply = await self._handle_horizon(user_id, intent, horizon_text)
            if handled:
                return True, reply

        # ------------------- 18. CHAT RESPONSE / CLARIFICATION -------------------
        if spoken_reply:
            return True, spoken_reply

        return False, ""

    # ------------------------------------------------------------------
    # Horizon: contexto personal del CEO
    # ------------------------------------------------------------------

    async def _handle_horizon(
        self,
        user_id: str,
        intent: str,
        text: str,
    ) -> tuple[bool, str]:
        """Ejecuta una capacidad de Horizon y devuelve la respuesta para voz.

        Devuelve ``(False, "")`` si Horizon no esta disponible, para que el
        turno siga su curso normal en vez de fallar en silencio.
        """
        if self.horizon is None:
            return False, ""

        # El chat no trae identidad autenticada, asi que se resuelve al
        # propietario configurado del espacio personal. Sin esto, una captura por
        # voz se guardaria bajo un nombre que el dashboard nunca consulta y el
        # espacio pareceria vacio sin que nada fallara.
        owner = self.horizon.owner_for(CaptureChannel.CHAT, user_id)

        try:
            if intent == "horizon_capture":
                clean = horizon_classify.strip_capture_prefix(text)
                kind = horizon_classify.classify_capture_kind(text)
                result = await self.horizon.capture(
                    owner,
                    clean,
                    kind=kind,
                    channel=CaptureChannel.CHAT,
                )
                return True, str(result.get("message") or "Anotado.")

            if intent == "horizon_progress":
                result = await self.horizon.record_progress(owner, text)
                if not result.get("recorded") and result.get("duplicate"):
                    return True, "Ese avance ya estaba registrado, no lo conté dos veces."
                return True, str(result.get("message") or "Registrado el avance.")

            if intent == "horizon_next_action":
                result = await self.horizon.next_action(owner, include_company=True)
                return True, str(result.get("message") or "No tengo una acción lista ahora mismo.")

            if intent == "horizon_plan_query":
                result = await self.horizon.plan_summary(owner)
                return True, str(result.get("message") or "Todavía no tenemos plan importado.")

            if intent == "horizon_capacity":
                minutes = horizon_classify.parse_declared_minutes(text)
                exercise = horizon_classify.parse_exercise_included(text)
                plan = await self.horizon.plan_day(
                    owner,
                    declared_free_minutes=minutes,
                    exercise_included=exercise,
                    trigger_kind="daily_plan",
                    source="chat",
                )
                await self.horizon.deliver_plan(owner, plan, channel="chat")
                return True, str(plan.get("message") or "Ajusté el plan con esa capacidad.")

            if intent == "horizon_plan_day":
                plan = await self.horizon.plan_today(owner, channel="chat")
                plan = await self.horizon.deliver_plan(owner, plan, channel="chat")
                return True, str(plan.get("message") or "No hay plan para hoy todavía.")

            if intent == "horizon_review":
                review = await self.horizon.review_objectives(owner)
                return True, str(review.get("message") or "No hubo desviaciones que reportar.")

            if intent == "horizon_import":
                # Cierra el hueco de uso de la fase 1: importar el contexto
                # personal sin necesidad de una sesion web autenticada.
                result = await self.horizon.import_context(owner, display_name="")
                if result.get("imported"):
                    return True, (
                        "Listo, importé el contexto personal: "
                        f"{result.get('objectives_created', 0)} objetivos nuevos y "
                        f"{result.get('objectives_existing', 0)} ya existentes."
                    )
                if result.get("reason") == "already_current":
                    return True, "Ya teníamos el contexto importado, no lo dupliqué."
                return True, "No encontré el contexto personal para importar."

            if intent == "horizon_delegate":
                result = await self.horizon.delegate_action(owner, instruction=text)
                return True, str(result.get("message") or "Acción delegada.")

            if intent == "horizon_delivery":
                result = await self.horizon.deliver_action_result(owner, channel="chat")
                return True, str(result.get("message") or "No hay entregas pendientes.")

            if intent == "horizon_metrics":
                parsed = horizon_classify.parse_metrics_input(text)
                actions = await self.horizon._repo.list_horizon_actions(owner, limit=20)
                target = next((a for a in actions if a.get("task_id")), None)
                if target:
                    res = await self.horizon.record_delegation_metrics(
                        owner,
                        str(target["action_id"]),
                        human_supervision_minutes=parsed.get("human_supervision_minutes"),
                        human_rework_minutes=parsed.get("human_rework_minutes"),
                        rework=bool(parsed.get("rework")),
                        note=text,
                    )
                    return True, str(res.get("message") or "Métricas registradas.")
                return True, "No encontré una acción delegada para registrar esas métricas."

            if intent == "horizon_brief":
                cockpit = await self.horizon.cockpit(owner)
                await self.horizon.deliver_plan(
                    owner, cockpit.get("plan") or {}, channel="chat"
                )
                fresh = [
                    item
                    for item in (cockpit.get("decisions") or {}).get("grouped") or []
                    if not item.get("delivered")
                ]
                await self.horizon.mark_decisions_delivered(owner, fresh, channel="chat")
                return True, str(cockpit.get("message") or "No tengo el brief ahora mismo.")

            if intent == "horizon_decisions":
                tray = await self.horizon.pending_decisions(owner)
                ordered = list(tray.get("urgent") or []) + list(tray.get("grouped") or [])
                fresh = [item for item in tray.get("grouped") or [] if not item.get("delivered")]
                await self.horizon.mark_decisions_delivered(owner, fresh, channel="chat")
                if not ordered:
                    return True, "No hay decisiones esperando."
                listing = horizon_cockpit.describe_decision_list(ordered)
                return True, f"{tray.get('message') or ''} {listing}".strip()

            if intent == "horizon_decide":
                parsed = horizon_classify.parse_decide_input(text)
                approve = parsed.get("approve")
                if approve is None:
                    return True, (
                        "¿La apruebas o la rechazas? Dime 'aprueba la primera' "
                        "o 'rechaza la decisión'."
                    )
                ordinal = parsed.get("ordinal")
                ticket_number = parsed.get("ticket_number")
                if ordinal is None and ticket_number is None:
                    tray = await self.horizon.pending_decisions(owner)
                    ordered = list(tray.get("urgent") or []) + list(tray.get("grouped") or [])
                    if len(ordered) == 1:
                        only = ordered[0]
                        res = await self.horizon.answer_decision(
                            owner,
                            task_id=str(only.get("task_id") or ""),
                            approve=bool(approve),
                            comment="",
                            channel="chat",
                        )
                        return True, str(
                            res.get("message") or "No pude aplicar esa respuesta."
                        )
                    listing = horizon_cockpit.describe_decision_list(ordered)
                    return True, f"¿Cuál? {listing}"
                res = await self.horizon.answer_decision(
                    owner,
                    ordinal=ordinal,
                    ticket_number=ticket_number,
                    approve=bool(approve),
                    comment="",
                    channel="chat",
                )
                if not res.get("applied"):
                    return True, str(
                        res.get("message")
                        or "No pude aplicar esa respuesta. Se responde desde la bandeja."
                    )
                return True, str(res.get("message") or "Aplicada.")
        except Exception:
            logger.warning("Horizon handling failed for intent %s", intent, exc_info=True)
            return True, "No pude completar esa operación del espacio personal. Lo intentamos de nuevo."

        return False, ""
