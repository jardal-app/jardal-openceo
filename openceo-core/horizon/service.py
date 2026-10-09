"""Servicio de Horizon: captura, decision, progreso y consulta de objetivos.

Todas las escrituras y lecturas filtran por ``user_id``. Ese filtro, junto con
el gate de privacidad, es lo que impide que el espacio personal se mezcle con
los proyectos de empresa (seccion 9.1: ningun contexto autoriza por si mismo a
modificar compromisos de otro).

El LLM no participa aqui. La seccion 9.2 punto 6 lo dice: el modelo interpreta
y propone; el codigo valida y ejecuta. Por eso este modulo funciona igual con
el presupuesto de IA agotado.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from company_mandate import SECRET_BLOCK_PARAM
from horizon import capacity as capacity_module
from horizon import classify as classify_module
from horizon import cockpit as cockpit_module
from horizon import importer as importer_module
from horizon import mandate as mandate_module
from horizon import planner as planner_module
from horizon import privacy as privacy_module
from horizon import promotion as promotion_module
from horizon import triggers as triggers_module
from horizon.models import (
    ActionOwner,
    ActionStatus,
    AvisoKind,
    CaptureChannel,
    CaptureKind,
    CaptureStatus,
    DeclaredValue,
    HorizonCapture,
    HorizonPlan,
    HorizonProgressEvent,
    HorizonProfile,
    ObjectiveHorizon,
    ObjectiveKind,
    ObjectiveStatus,
    PlanAviso,
    PlanCapacity,
    ProgressKind,
    TriggerKind,
    TriggerStatus,
    Uncertainty,
    document_dump,
)
from settings import Settings

logger = logging.getLogger(__name__)

# Limite de caracteres guardados por captura. El texto original se conserva
# acotado para que una nota de voz larga no infle el documento.
_MAX_TEXT_CHARS = 2000

# Un avance declarado por el usuario sin identificador externo se deduplica por
# contenido + dia. Asi reenviar el mismo mensaje no duplica progreso (seccion
# 9.10), pero la misma frase en dias distintos si cuenta.
_MEASUREMENT_UNITS = {"minutes", "minutos"}

# Rollup de hijos por objetivo (cockpit F5). "failed" cuenta como abierto para
# el encolador (no se hizo el trabajo), pero aqui es bloqueo de negocio.
_OPEN_STATUSES = frozenset({"queued", "in_progress", "waiting"})
_BLOCKED_STATUSES = frozenset({"failed", "escalated"})
_CLOSED_STATUSES = frozenset({"completed", "cancelled", "converted"})


def _secret_block_marker(task: dict[str, Any]) -> dict[str, Any]:
    params = task.get("params") if isinstance(task, dict) else None
    marker = params.get(SECRET_BLOCK_PARAM) if isinstance(params, dict) else None
    return marker if isinstance(marker, dict) else {}


def _task_secret_blocked(task: dict[str, Any]) -> bool:
    """Working aparcado por secreto ausente (TASK-2026-00142).

    Cuenta como bloqueo de negocio aunque su estado de ejecucion siga
    abierto; un terminal con marcador rancio no cuenta (el cierre manda).
    """
    if str(task.get("status") or "") in _CLOSED_STATUSES:
        return False
    return bool(str(_secret_block_marker(task).get("secret") or "").strip())


def _secret_block_reason(task: dict[str, Any]) -> str:
    marker = _secret_block_marker(task)
    return str(marker.get("reason") or marker.get("secret") or "").strip()


def _short_text(text: str, max_chars: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _has_human_phase(task: dict[str, Any]) -> bool:
    """True si la fase vigente del ticket espera a un humano.

    Espeja la resolucion de ``TaskService._current_phase`` (por ``phase`` y,
    si no, por ``phase_index``): la vista previa de la bandeja no puede pedir
    prestado el metodo privado del otro servicio.
    """
    phases = task.get("workflow_phases") or []
    if not phases:
        return False
    index = 0
    try:
        index = int(task.get("phase_index") or 0)
    except (TypeError, ValueError):
        index = 0
    current_phase = str(task.get("phase") or "")
    if current_phase:
        for position, phase in enumerate(phases):
            if not isinstance(phase, dict):
                continue
            key = str(phase.get("key") or phase.get("name") or "")
            if key == current_phase:
                index = position
                break
    index = max(0, min(index, len(phases) - 1))
    current = phases[index]
    if not isinstance(current, dict):
        return False
    return str(current.get("type") or "") == "human"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe_key(text: str, kind: str) -> str:
    normalized = classify_module.normalize_text(text)
    digest = hashlib.sha1(f"{kind}|{normalized}".encode("utf-8")).hexdigest()
    return digest[:32]


def _event_source_id(text: str) -> str:
    """Identificador estable para un avance declarado sin origen externo."""
    normalized = classify_module.normalize_text(text)
    day = _now().strftime("%Y-%m-%d")
    return hashlib.sha1(f"user|{day}|{normalized}".encode("utf-8")).hexdigest()[:32]


def _metric_key_for(text: str) -> str | None:
    """Identifica la metrica que un avance actualiza, si es inequivoca.

    Seccion 9.3: solo se actualiza cada metrica con evidencia apropiada.
    """
    normalized = classify_module.normalize_text(text)
    measurement = classify_module.extract_measurement(text)
    if not measurement:
        return None
    _amount, unit = measurement
    if unit not in _MEASUREMENT_UNITS:
        return None
    if "audio" in normalized or "grab" in normalized:
        return "voice_usable_minutes"
    if any(token in normalized for token in ("ejercicio", "caminata", "gimnasio", "movilidad")):
        return "exercise_weekly_minutes_initial"
    return None


class HorizonService:
    """Capacidad personal del CEO. Un solo coordinador, sin cola paralela."""

    def __init__(self, settings: Settings, repo: Any, task_service: Any | None = None) -> None:
        self._settings = settings
        self._repo = repo
        self._task_service = task_service
        self._company_reader: Any | None = None
        self._company_config: Any | None = None

    def set_task_service(self, task_service: Any) -> None:
        self._task_service = task_service

    def set_company_reader(self, reader: Any | None) -> None:
        """Lector del registro de negocio (ERPNext). Opcional.

        Sin lector, la seccion de empresa del cockpit queda en desconocido
        (``unknown``), nunca en cero y nunca inventada.
        """
        self._company_reader = reader

    def set_company_config(self, config_loader: Any | None) -> None:
        """Configuracion de empresa (proyectos + mandato). Opcional.

        Sin ella no se puede validar el toque de empresa ni leer el mandato:
        toda promocion se rechaza (fail-closed) y la delegacion personal
        sigue intacta.
        """
        self._company_config = config_loader

    # ------------------------------------------------------------------
    # Perfil e importacion
    # ------------------------------------------------------------------

    async def ensure_profile(self, user_id: str, *, display_name: str = "") -> dict[str, Any]:
        """Devuelve el perfil, creando uno minimo si aun no existe.

        Sin importacion previa el perfil arranca en blanco: no se inventan
        objetivos ni cifras.
        """
        profile = await self._repo.get_horizon_profile(user_id)
        if profile:
            return profile

        mandate = mandate_module.default_mandate(
            timezone=self._default_timezone(),
            mirror_enabled=self._mirror_enabled(),
        )
        fresh = document_dump(
            HorizonProfile(
                user_id=user_id,
                display_name=display_name,
                mandate=mandate,
            )
        )
        await self._repo.upsert_horizon_profile(fresh)
        return await self._repo.get_horizon_profile(user_id) or fresh

    def _default_timezone(self) -> str:
        return str(getattr(self._settings, "HORIZON_TIMEZONE", mandate_module.DEFAULT_TIMEZONE))

    async def import_context(
        self,
        user_id: str,
        *,
        source_path: str | None = None,
        display_name: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """Importa el contexto personal desde la especificacion, idempotente."""
        return await importer_module.import_context(
            self._repo,
            user_id,
            source_path=source_path or getattr(self._settings, "HORIZON_SOURCE_PATH", "") or None,
            display_name=display_name,
            force=force,
            capacity_ceiling=self._capacity_ceiling(),
            mirror_enabled=self._mirror_enabled(),
        )

    def _mirror_enabled(self) -> bool:
        """Espejo externo de datos personales: apagado salvo habilitacion explicita.

        La reimportacion del README no puede encenderlo; solo este ajuste puede.
        """
        return bool(getattr(self._settings, "HORIZON_PERSONAL_MIRROR_ENABLED", False))

    def _capacity_ceiling(self) -> float:
        """Techo de capacidad programable, usado para recortar la importacion."""
        try:
            return float(getattr(self._settings, "HORIZON_CAPACITY_FRACTION", 0.70))
        except (TypeError, ValueError):
            return 0.70

    # ------------------------------------------------------------------
    # Captura
    # ------------------------------------------------------------------

    async def capture(
        self,
        user_id: str,
        text: str,
        *,
        kind: CaptureKind | None = None,
        channel: CaptureChannel = CaptureChannel.CHAT,
        origin: str | None = None,
        dedupe: bool = True,
    ) -> dict[str, Any]:
        """Registra una idea, pendiente, avance o compromiso.

        Un pendiente repetido no crea trabajo dos veces (seccion 9.10). La
        respuesta indica si fue una captura nueva o un duplicado ya registrado.
        """
        clean = (text or "").strip()
        if not clean:
            return {"captured": False, "reason": "empty", "message": "No llegó contenido para registrar."}

        clean = clean[:_MAX_TEXT_CHARS]
        resolved_kind = kind or classify_module.classify_capture_kind(clean)
        key = _dedupe_key(clean, resolved_kind.value)

        if dedupe:
            existing = await self._repo.get_horizon_capture_by_dedupe_key(user_id, key)
            if existing:
                return {
                    "captured": False,
                    "duplicate": True,
                    "reason": "duplicate",
                    "capture": existing,
                    "message": "Ya teníamos anotado eso. No lo dupliqué.",
                }

        objective = await self._link_objective_for(user_id, clean, resolved_kind)
        requires_decision, reason = classify_module.wants_decision(clean, resolved_kind)

        review_condition = None
        if objective and str(objective.get("status")) == ObjectiveStatus.PARKED.value:
            review_condition = objective.get("parked_review_condition")

        capture = HorizonCapture(
            user_id=user_id,
            kind=resolved_kind,
            text=clean,
            channel=channel,
            origin=origin,
            dedupe_key=key,
            status=CaptureStatus.LINKED if objective else CaptureStatus.NEW,
            linked_objective_id=str(objective.get("objective_id")) if objective else None,
            review_condition=review_condition,
            requires_decision=requires_decision,
            decision_reason=reason,
        )
        stored = await self._repo.create_horizon_capture(document_dump(capture))

        # Un compromiso o una captura que exige decision se registra igual: la
        # seccion 9.4 dice que Horizon captura y prepara, y consulta solo la
        # decision que no le corresponde.
        return {
            "captured": True,
            "duplicate": False,
            "capture": stored,
            "kind": resolved_kind.value,
            "requires_decision": requires_decision,
            "decision_reason": reason,
            "message": self.describe_capture(stored),
        }

    async def _link_objective_for(
        self,
        user_id: str,
        text: str,
        kind: CaptureKind,
    ) -> dict[str, Any] | None:
        """Enlaza la captura al objetivo que le corresponde, si es inequivoco."""
        normalized = classify_module.normalize_text(text)
        objectives = await self._repo.list_horizon_objectives(user_id)
        if not objectives:
            return None

        def find(key: str) -> dict[str, Any] | None:
            return next((o for o in objectives if o.get("source_key") == key), None)

        if any(tok in normalized for tok in ("voz", "grab", "audio", "entrevista", "cv", "curriculum", "empleo", "postulacion")):
            return find("growth_employment")
        if any(tok in normalized for tok in ("deuda", "banco", "presupuesto", "diezmo", "prestamo", "contrato", "cobro", "pago")):
            return find("financial_coverage")
        if any(tok in normalized for tok in ("cliente", "proyecto de cliente", "mantenimiento", "soporte")):
            return find("clients_commitments")
        if any(tok in normalized for tok in ("ejercicio", "gimnasio", "caminata", "movilidad", "descanso", "dormir")):
            return find("health_exercise")

        parked = [o for o in objectives if str(o.get("status")) == ObjectiveStatus.PARKED.value]
        if parked:
            if any(tok in normalized for tok in ("videojuego", "juego", "biblia")):
                return find("bible_videogame")
            if any(tok in normalized for tok in ("libro", "padre", "asistente musical")):
                return find("father_book")
            if any(tok in normalized for tok in ("hermana", "medio digital", "periodismo")):
                return find("sister_media")
            if any(tok in normalized for tok in ("hermano", "3d", "modelado")):
                return find("sibling_employment")
            if any(tok in normalized for tok in ("terreno", "laboratorio", "agricultura", "granja")):
                return find("land_lab_agriculture")

        return None

    @staticmethod
    def describe_capture(capture: dict[str, Any]) -> str:
        """Frase corta para voz o chat. Sin identificadores."""
        text = str(capture.get("text") or "")
        snippet = text if len(text) <= 120 else text[:117] + "..."
        kind = str(capture.get("kind") or CaptureKind.IDEA.value)
        if kind == CaptureKind.PENDING.value:
            base = f"Anotado como pendiente: {snippet}"
        elif kind == CaptureKind.COMMITMENT.value:
            base = f"Anotado como compromiso: {snippet}"
        elif kind == CaptureKind.PROGRESS.value:
            base = f"Registrado el avance: {snippet}"
        else:
            base = f"Anotada la idea: {snippet}"
        if capture.get("requires_decision"):
            base += " Esto necesita tu decisión; lo dejo preparado."
        return base

    # ------------------------------------------------------------------
    # Progreso
    # ------------------------------------------------------------------

    async def record_progress(
        self,
        user_id: str,
        text: str,
        *,
        source_id: str | None = None,
        objective_id: str | None = None,
        source: str = "user",
    ) -> dict[str, Any]:
        """Registra un avance con evidencia y deduplicacion por origen."""
        clean = (text or "").strip()
        if not clean:
            return {"recorded": False, "reason": "empty"}

        clean = clean[:_MAX_TEXT_CHARS]
        event_source_id = source_id or _event_source_id(clean)
        existing = await self._repo.get_horizon_event_by_source(user_id, event_source_id)
        if existing:
            # Seccion 9.3: repetir una notificacion o recibo no duplica progreso.
            return {
                "recorded": False,
                "duplicate": True,
                "reason": "duplicate_source_id",
                "event": existing,
            }

        measurement = classify_module.extract_measurement(clean)
        metric_key = _metric_key_for(clean)
        target_milestone = await self._resolve_target_milestone(user_id, metric_key, objective_id)

        value = (
            _measurement_value(measurement, source)
            if measurement
            else _unknown_value(source)
        )

        event = HorizonProgressEvent(
            user_id=user_id,
            kind=ProgressKind.RESULT if measurement else ProgressKind.PROGRESS,
            entity="milestone" if target_milestone else "capture",
            entity_id=str(target_milestone.get("milestone_id")) if target_milestone else None,
            source=source,
            source_id=event_source_id,
            objective_id=str(target_milestone.get("objective_id")) if target_milestone else objective_id,
            text=clean,
            value=value,
        )
        stored = await self._repo.create_horizon_event(document_dump(event))

        updated_milestone = None
        if target_milestone and measurement:
            updated_milestone = await self._apply_measurement(
                user_id=user_id,
                milestone=target_milestone,
                amount=measurement[0],
                source=source,
                event_id=stored.get("event_id"),
            )

        return {
            "recorded": True,
            "duplicate": False,
            "event": stored,
            "metric": metric_key,
            "milestone": updated_milestone,
            "message": self.describe_progress(clean, updated_milestone, measurement),
        }

    async def _resolve_target_milestone(
        self,
        user_id: str,
        metric_key: str | None,
        objective_id: str | None,
    ) -> dict[str, Any] | None:
        if not metric_key:
            return None
        objectives = await self._repo.list_horizon_objectives(user_id)
        for objective in objectives:
            if objective_id and str(objective.get("objective_id")) != objective_id:
                continue
            for milestone in objective.get("milestones") or []:
                measure = classify_module.normalize_text(str(milestone.get("measure") or ""))
                if metric_key == "voice_usable_minutes" and "audio" in measure:
                    return {**milestone, "objective_id": objective.get("objective_id")}
                if metric_key == "exercise_weekly_minutes_initial" and (
                    "actividad" in measure or "minutos" in measure
                ):
                    return {**milestone, "objective_id": objective.get("objective_id")}
        return None

    async def _apply_measurement(
        self,
        *,
        user_id: str,
        milestone: dict[str, Any],
        amount: int,
        source: str,
        event_id: str | None,
    ) -> dict[str, Any] | None:
        """Suma la medicion al acumulado del hito, sin inventar una base.

        Si el acumulado era desconocido, la medicion lo inicia. No se parte de
        un valor declarado de otra naturaleza (grabado != util).
        """
        base = milestone.get("base") or {}
        current = base.get("value")
        new_total = int(amount) if current is None else int(current) + int(amount)

        evidence = list(milestone.get("evidence") or [])
        if event_id and event_id not in evidence:
            evidence.append(str(event_id))

        milestone_id = str(milestone.get("milestone_id") or "")
        objective_id = str(milestone.get("objective_id") or "")
        if not milestone_id or not objective_id:
            return None

        inherited_source = str(base.get("source") or "")
        merged_source = inherited_source or source
        return await self._repo.update_horizon_milestone(
            user_id=user_id,
            objective_id=objective_id,
            milestone_id=milestone_id,
            updates={
                "base": {
                    "value": new_total,
                    "uncertainty": Uncertainty.DECLARED.value,
                    "source": merged_source,
                    "as_of": _now(),
                },
                "evidence": evidence,
                "status": ObjectiveStatus.ACTIVE.value,
            },
        )

    @staticmethod
    def describe_progress(
        text: str,
        milestone: dict[str, Any] | None,
        measurement: tuple[int, str] | None,
    ) -> str:
        """Frase corta para voz, sin identificadores ni tablas."""
        if milestone and measurement:
            base = milestone.get("base") or {}
            target = milestone.get("target") or {}
            current = base.get("value")
            goal = target.get("value")
            title = str(milestone.get("title") or "").strip()
            if current is not None and goal is not None:
                return (
                    f"Registrado: {title.lower()}. Vamos {current} de {goal}. "
                    "El siguiente paso sigue siendo el bloque de grabación."
                )
            if current is not None:
                return f"Registrado: {title.lower()}. Acumulamos {current}."
            return f"Registrado el avance en {title.lower()}."
        if measurement:
            return f"Registrado el avance: {measurement[0]} minutos."
        snippet = text if len(text) <= 120 else text[:117] + "..."
        return f"Registrado el avance: {snippet}"

    # ------------------------------------------------------------------
    # Decision: que sigue
    # ------------------------------------------------------------------

    async def next_action(self, user_id: str, *, include_company: bool = False) -> dict[str, Any]:
        """Elige UNA accion principal, con su motivo y su siguiente paso.

        Seccion 9.5: una accion principal y hasta dos secundarias. Horizon no
        pregunta "que quieres hacer hoy" si puede elegir con lo que ya sabe.

        Con ``include_company`` (F5, TASK-2026-00120) suma ``company`` y
        ``decisions``: el "que sigue" del dueno trae plan personal + status de
        empresa + decisiones pendientes. Las llamadas internas que solo
        necesitan la accion (delegacion) lo dejan apagado para no pagar las
        lecturas de empresa.
        """
        profile = await self.ensure_profile(user_id)
        mandate = mandate_module.HorizonMandate(**(profile.get("mandate") or {}))

        actions = await self._repo.list_horizon_actions(
            user_id,
            statuses=[ActionStatus.READY.value, ActionStatus.IN_PROGRESS.value],
        )
        objectives = await self._repo.list_horizon_objectives(user_id)
        objectives_by_id = {str(o.get("objective_id")): o for o in objectives}

        # Una accion en curso conserva la continuidad delante de una nueva.
        in_progress = [a for a in actions if str(a.get("status")) == ActionStatus.IN_PROGRESS.value]
        ready = [a for a in actions if str(a.get("status")) == ActionStatus.READY.value]
        ordered = sorted(
            in_progress or ready,
            key=lambda a: (int(a.get("priority") or 100), str(a.get("created_at") or "")),
        )

        if not ordered:
            result: dict[str, Any] = {
                "has_action": False,
                "message": await self._no_action_message(user_id),
                "active_growth_goal": self._growth_goal(objectives),
                "ready_count": 0,
                "in_progress_count": 0,
            }
        else:
            primary = ordered[0]
            secondary = ordered[1:3]
            objective = objectives_by_id.get(str(primary.get("objective_id") or ""))
            result = {
                "has_action": True,
                "action": primary,
                "secondary": secondary,
                "objective": objective,
                "milestone": self._milestone_of(objective, primary.get("milestone_id")),
                "active_growth_goal": self._growth_goal(objectives),
                "ready_count": len(ready),
                "in_progress_count": len(in_progress),
                "limits": {
                    "ready_max": mandate.personal_ready_actions_max,
                    "in_progress_max": mandate.personal_in_progress_max,
                    "capacity_fraction": mandate.personal_capacity_fraction,
                },
                "message": self.describe_next_action(primary, objective),
            }

        if include_company:
            company = await self.company_status()
            decisions = await self.pending_decisions(user_id)
            result["company"] = company
            result["decisions"] = decisions
            extra = " ".join(
                part
                for part in (
                    str(company.get("message") or ""),
                    str(decisions.get("message") or ""),
                )
                if part
            )
            if extra:
                result["message"] = f"{result['message']} {extra}"
        return result

    def _growth_goal(self, objectives: list[dict[str, Any]]) -> dict[str, Any] | None:
        for objective in objectives:
            if (
                str(objective.get("kind")) == ObjectiveKind.GROWTH.value
                and str(objective.get("status")) == ObjectiveStatus.ACTIVE.value
                and str(objective.get("horizon")) != ObjectiveHorizon.SOMEDAY.value
            ):
                return objective
        return None

    @staticmethod
    def _milestone_of(objective: dict[str, Any] | None, milestone_id: Any) -> dict[str, Any] | None:
        if not objective or not milestone_id:
            return None
        for milestone in objective.get("milestones") or []:
            if str(milestone.get("milestone_id")) == str(milestone_id):
                return milestone
        return None

    async def _no_action_message(self, user_id: str) -> str:
        objectives = await self._repo.list_horizon_objectives(user_id)
        if not objectives:
            return (
                "Todavía no tenemos objetivos importados. "
                "Puedo importar el contexto personal o anotar una idea para empezar."
            )
        return (
            "No hay una acción personal lista ahora mismo. "
            "Puedo preparar material del objetivo de crecimiento o registrar algo nuevo."
        )

    @staticmethod
    def describe_next_action(action: dict[str, Any], objective: dict[str, Any] | None) -> str:
        """Frase breve, primera persona plural, apta para voz."""
        title = str(action.get("title") or "").strip()
        effort = action.get("effort_estimated_minutes")
        effort_part = f"unos {int(effort)} minutos" if effort else "un bloque corto"
        goal_part = ""
        if objective:
            goal = str(objective.get("title") or "").strip()
            if goal:
                goal_part = f" Eso alimenta el objetivo de {goal.lower()}."
        next_step = str(action.get("next_step") or "").strip()
        step_part = f" {next_step}" if next_step else ""
        return f"Lo siguiente es {title.lower()}, {effort_part}.{goal_part}{step_part}"

    # ------------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------------

    async def list_objectives(self, user_id: str) -> list[dict[str, Any]]:
        return await self._repo.list_horizon_objectives(user_id)

    async def plan_summary(self, user_id: str) -> dict[str, Any]:
        """Resumen consultable del rumbo: mision, objetivos y estado de la voz."""
        profile = await self.ensure_profile(user_id)
        objectives = await self._repo.list_horizon_objectives(user_id)
        captures = await self._repo.list_horizon_captures(user_id, limit=5)

        active = [o for o in objectives if str(o.get("status")) == ObjectiveStatus.ACTIVE.value]
        parked = [o for o in objectives if str(o.get("status")) == ObjectiveStatus.PARKED.value]
        voice = self._voice_progress(objectives)

        return {
            "mission": (profile.get("mission") or {}).get("statement"),
            "plan_version": (profile.get("declared_context") or {}).get("plan_version"),
            "imported_at": (profile.get("declared_context") or {}).get("imported_at"),
            "unresolved": (profile.get("declared_context") or {}).get("unresolved") or [],
            "active_objectives": active,
            "parked_objectives": parked,
            "growth_goal": self._growth_goal(objectives),
            "voice": voice,
            "recent_captures": captures,
            "message": self.describe_plan(profile, active, parked, voice),
        }

    def _voice_progress(self, objectives: list[dict[str, Any]]) -> dict[str, Any]:
        """Estado de la voz, con desconocido explicito en vez de cero."""
        for objective in objectives:
            if objective.get("source_key") != "growth_employment":
                continue
            for milestone in objective.get("milestones") or []:
                if milestone.get("source_key") != "VOZ-001":
                    continue
                base = milestone.get("base") or {}
                target = milestone.get("target") or {}
                usable = base.get("value")
                goal = target.get("value")
                remaining = None
                if usable is not None and goal is not None:
                    remaining = max(0, int(goal) - int(usable))
                return {
                    "usable_minutes": usable,
                    "target_minutes": goal,
                    "remaining_minutes": remaining,
                    "confirmed": usable is not None,
                    "blocker": milestone.get("blocker"),
                }
        return {"usable_minutes": None, "target_minutes": None, "remaining_minutes": None, "confirmed": False}

    @staticmethod
    def describe_plan(
        profile: dict[str, Any],
        active: list[dict[str, Any]],
        parked: list[dict[str, Any]],
        voice: dict[str, Any],
    ) -> str:
        mission = (profile.get("mission") or {}).get("statement") or ""
        parts: list[str] = []
        if mission:
            parts.append(f"La misión sigue siendo {mission}.")
        if active:
            parts.append(
                "Tenemos " + str(len(active)) + " objetivos activos: "
                + ", ".join(str(o.get("title") or "") for o in active[:3]) + "."
            )
        if voice.get("confirmed") and voice.get("remaining_minutes") is not None:
            parts.append(
                f"En voz llevamos {voice['usable_minutes']} de {voice['target_minutes']} minutos útiles; "
                f"quedan {voice['remaining_minutes']}."
            )
        else:
            parts.append(
                "De la voz todavía no tenemos minutos útiles medidos, así que no proyecto fecha."
            )
        if parked:
            parts.append(f"Hay {len(parked)} ideas aparcadas esperando su condición de revisión.")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Cockpit del dueno (F5, TASK-2026-00120)
    # ------------------------------------------------------------------

    async def cockpit(self, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Plan del dia + status de empresa + decisiones pendientes.

        Es la interfaz principal del dueno: "que sigue" y el brief combinan
        lo personal con lo que avanzo el CEO y lo que espera decision. Hasta
        que exista el status del CEO (TASK-2026-00119), la empresa se lee de
        ERPNext y de los objetivos directamente. Sin LLM, como todo Horizon.
        """
        moment = now or _now()
        personal = await self.next_action(user_id, include_company=False)
        plan = await self.plan_today(user_id, channel="cockpit", now=moment)
        company = await self.company_status()
        decisions = await self.pending_decisions(user_id, now=moment)
        message = " ".join(
            part
            for part in (
                str(plan.get("message") or ""),
                str(company.get("message") or ""),
                str(decisions.get("message") or ""),
            )
            if part
        )
        return {
            "personal": personal,
            "plan": plan,
            "company": company,
            "decisions": decisions,
            "message": message,
        }

    async def company_status(self) -> dict[str, Any]:
        """Status de empresa: objetivos, cerrados, bloqueados y negocio.

        Cada fuente degrada a desconocido por separado: una lectura caida no
        vacia las demas. Desconocido es ``None``/``*_unknown``, nunca ``0``.
        """
        objectives: list[dict[str, Any]] = []
        objectives_unknown = False
        try:
            active = await self._repo.list_active_objectives()
        except Exception:
            logger.warning("cockpit: objetivos no legibles", exc_info=True)
            active = None
        if active is None:
            objectives_unknown = True
        else:
            for objective in list(active)[:10]:
                objective_id = str(objective.get("objective_id") or "")
                children: list[dict[str, Any]] = []
                try:
                    children = await self._repo.list_tasks_by_objective(
                        objective_id, limit=100
                    )
                except Exception:
                    logger.warning(
                        "cockpit: hijos del objetivo %s no legibles",
                        objective_id,
                        exc_info=True,
                    )
                open_count = sum(
                    1
                    for c in children
                    if str(c.get("status") or "") in _OPEN_STATUSES
                    and not _task_secret_blocked(c)
                )
                blocked_count = sum(
                    1
                    for c in children
                    if str(c.get("status") or "") in _BLOCKED_STATUSES
                    or _task_secret_blocked(c)
                )
                closed_count = sum(
                    1 for c in children if str(c.get("status") or "") in _CLOSED_STATUSES
                )
                anchor = objective.get("anchor") if isinstance(objective.get("anchor"), dict) else {}
                objectives.append(
                    {
                        "objective_id": objective_id,
                        "project": objective.get("project"),
                        "entry": objective.get("entry"),
                        "goal": _short_text(str(anchor.get("goal") or ""), 200),
                        "erp_group_ref": objective.get("erp_group_ref"),
                        "status": objective.get("status"),
                        "open_children": open_count,
                        "blocked_children": blocked_count,
                        "closed_children": closed_count,
                    }
                )

        closed: list[dict[str, Any]] = []
        closed_unknown = False
        try:
            rows = await self._repo.list_recently_closed_tasks(
                limit=cockpit_module.COMPANY_CLOSED_LIMIT
            )
            closed = [
                {
                    "task_id": str(t.get("task_id") or ""),
                    "erp_name": cockpit_module.erp_mirror_name(t),
                    "ticket_number": t.get("ticket_number"),
                    "title": str(t.get("title") or ""),
                    "project": t.get("project"),
                    "completed_at": t.get("completed_at"),
                }
                for t in rows
            ]
        except Exception:
            logger.warning("cockpit: cerrados no legibles", exc_info=True)
            closed_unknown = True

        blocked: list[dict[str, Any]] = []
        blocked_unknown = False
        try:
            rows = await self._repo.list_blocked_tasks(
                limit=cockpit_module.COMPANY_BLOCKED_LIMIT
            )
            blocked = [
                {
                    "task_id": str(t.get("task_id") or ""),
                    "erp_name": cockpit_module.erp_mirror_name(t),
                    "ticket_number": t.get("ticket_number"),
                    "title": str(t.get("title") or ""),
                    "project": t.get("project"),
                    "status": t.get("status"),
                    "reason": _short_text(
                        str(
                            _secret_block_reason(t)
                            or t.get("error")
                            or t.get("waiting_reason")
                            or t.get("last_response_summary")
                            or ""
                        ),
                        200,
                    ),
                }
                for t in rows
            ]
        except Exception:
            logger.warning("cockpit: bloqueados no legibles", exc_info=True)
            blocked_unknown = True

        business_open: list[dict[str, Any]] = []
        business_unknown = False
        if self._company_reader is None:
            business_unknown = True
        else:
            try:
                rows = await self._company_reader.search_tickets(
                    status=["Open", "Working", "Pending Review"],
                    limit=cockpit_module.COMPANY_BUSINESS_LIMIT,
                )
            except Exception:
                logger.warning("cockpit: ERPNext no legible", exc_info=True)
                business_unknown = True
            else:
                desk_only = [r for r in (rows or []) if not str(r.get("openceo_ref") or "").strip()]
                business_open = [
                    {
                        "erp_name": str(r.get("name") or ""),
                        "subject": str(r.get("subject") or ""),
                        "status": str(r.get("status") or ""),
                        "project": r.get("project"),
                    }
                    for r in desk_only[:10]
                ]

        ceo: dict[str, Any] | None = None
        try:
            latest_review = await self._repo.latest_ceo_review()
        except Exception:
            logger.warning(
                "cockpit: ultima revision CEO no legible", exc_info=True
            )
            latest_review = None
        if isinstance(latest_review, dict) and latest_review:
            outcome = latest_review.get("outcome") or {}
            decision = latest_review.get("decision") or {}
            ceo = {
                "review_id": latest_review.get("review_id"),
                "at": latest_review.get("at"),
                "kind": latest_review.get("kind"),
                "action": outcome.get("action"),
                "ok": outcome.get("ok"),
                "summary": (
                    decision.get("summary") if isinstance(decision, dict)
                    else None
                ),
                "status": latest_review.get("status"),
            }

        company = {
            "objectives": objectives,
            "objectives_unknown": objectives_unknown,
            "recently_closed": closed,
            "closed_unknown": closed_unknown,
            "blocked": blocked,
            "blocked_unknown": blocked_unknown,
            "business_open_without_execution": business_open,
            "business_unknown": business_unknown,
            "ceo": ceo,
        }
        company["message"] = cockpit_module.describe_company(company)
        return company

    async def pending_decisions(
        self, user_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Bandeja de decisiones: misma fuente que Requiere humano.

        Filtra las esperas operativas con ``waiting_needs_human_review`` (F4):
        la bandeja trae lo que nombra una accion humana, no temporizadores ni
        parques automaticos. Urgente primero; el resto agrupado. Lo personal
        nunca entra (el repo ya lo excluye y aqui se vuelve a comprobar).
        """
        from tools.erpnext_tickets import waiting_needs_human_review

        moment = now or _now()
        rows = await self._repo.list_tasks_needing_human(
            limit=cockpit_module.DECISIONS_LIMIT
        )
        candidates = []
        for task in rows:
            if privacy_module.is_personal_scope(task):
                continue
            qa_state = str(task.get("qa_review_state") or "")
            if qa_state in ("pending", "changes_requested") or waiting_needs_human_review(task):
                candidates.append(task)

        ids = [str(t.get("task_id") or "") for t in candidates if t.get("task_id")]
        with_dependents = await self._repo.tasks_with_dependents(ids)
        blocked_runs = await self._repo.latest_blocked_runs(ids)

        urgent: list[dict[str, Any]] = []
        grouped: list[dict[str, Any]] = []
        for task in candidates:
            task_id = str(task.get("task_id") or "")
            reason = cockpit_module.urgency_reason(
                task, now=moment, has_dependents=task_id in with_dependents
            )
            delivered = (
                await self._repo.get_horizon_event_by_source(
                    user_id, cockpit_module.delivery_source_id(task_id)
                )
            ) is not None
            answerable = (
                task_id in blocked_runs
                or _has_human_phase(task)
                or (
                    str(task.get("phase") or "") == "owner_escalation"
                    and str(task.get("status") or "") == "waiting"
                )
            )
            item = cockpit_module.normalize_decision(
                task,
                ordinal=0,
                delivered=delivered,
                urgent=reason is not None,
                urgency=reason,
                answerable=answerable,
            )
            (urgent if reason is not None else grouped).append(item)

        ordered = urgent + grouped
        for position, item in enumerate(ordered, start=1):
            item["ordinal"] = position
        fresh_grouped = [item for item in grouped if not item.get("delivered")]
        return {
            "urgent": urgent,
            "grouped": grouped,
            "count": len(ordered),
            "urgent_count": len(urgent),
            "undelivered_grouped_count": len(fresh_grouped),
            "message": cockpit_module.describe_decisions(urgent, fresh_grouped),
        }

    async def mark_decisions_delivered(
        self,
        user_id: str,
        decisions: list[dict[str, Any]],
        *,
        channel: str = "api",
    ) -> dict[str, Any]:
        """Marca decisiones como entregadas (entrega unica, HZ-003).

        Solo guarda referencia (task_id + titulo): dato de empresa en solo
        lectura dentro del espacio personal. Idempotente por
        ``(user_id, source_id, entity)``.
        """
        marked = 0
        for item in decisions or []:
            task_id = str((item or {}).get("task_id") or "")
            if not task_id:
                continue
            source_id = cockpit_module.delivery_source_id(task_id)
            existing = await self._repo.get_horizon_event_by_source(user_id, source_id)
            if existing is not None:
                continue
            title = str(item.get("title") or task_id)
            event = HorizonProgressEvent(
                user_id=user_id,
                kind=ProgressKind.RESULT,
                entity=cockpit_module.DECISION_DELIVERY_ENTITY,
                entity_id=task_id,
                source=channel,
                source_id=source_id,
                text=f"Decisión mostrada: {title}"[:500],
            )
            try:
                await self._repo.create_horizon_event(document_dump(event))
            except Exception:
                # Carrera con otra entrega: el indice unico ya la marco.
                logger.debug("cockpit: entrega duplicada de %s", task_id, exc_info=True)
                continue
            marked += 1
        return {"marked": marked}

    async def answer_decision(
        self,
        user_id: str,
        *,
        task_id: str | None = None,
        ordinal: int | None = None,
        ticket_number: int | None = None,
        approve: bool = True,
        comment: str = "",
        channel: str = "api",
    ) -> dict[str, Any]:
        """Aplica la respuesta del dueno a una decision pendiente.

        El destino se da de exactamente una forma: ``task_id`` (movil/API),
        ``ordinal`` de la bandeja actual (voz) o ``ticket_number`` de negocio.
        """
        targets = [
            value
            for value in (task_id, ordinal, ticket_number)
            if value is not None and str(value).strip() != ""
        ]
        if len(targets) != 1:
            return {"applied": False, "reason": "ambiguous_target"}
        if self._task_service is None:
            return {"applied": False, "reason": "task_service_unavailable"}

        resolved_id = str(task_id or "").strip()
        if ordinal is not None:
            tray = await self.pending_decisions(user_id)
            ordered = list(tray.get("urgent") or []) + list(tray.get("grouped") or [])
            try:
                position = int(ordinal)
            except (TypeError, ValueError):
                return {"applied": False, "reason": "ordinal_not_found"}
            if position < 1 or position > len(ordered):
                return {"applied": False, "reason": "ordinal_not_found"}
            resolved_id = str(ordered[position - 1].get("task_id") or "")
        elif ticket_number is not None:
            try:
                number = int(ticket_number)
            except (TypeError, ValueError):
                return {"applied": False, "reason": "ticket_not_found"}
            found = await self._repo.get_task_by_ticket_number(number)
            if not found:
                return {"applied": False, "reason": "ticket_not_found"}
            resolved_id = str(found.get("task_id") or "")
        if not resolved_id:
            return {"applied": False, "reason": "task_not_found"}

        result = await self._task_service.apply_owner_decision(
            resolved_id,
            approve=approve,
            actor_id=user_id,
            comment=comment,
        )
        if not result.get("applied"):
            return result
        task = result.get("task") or {}
        title = str(task.get("title") or resolved_id)
        verb = "aprobé" if approve else "rechacé"
        result["message"] = f"Listo, {verb} {title}."
        try:
            await self.mark_decisions_delivered(
                user_id, [{"task_id": resolved_id, "title": title}], channel=channel
            )
        except Exception:
            logger.warning("cockpit: no se pudo marcar entrega de %s", resolved_id, exc_info=True)
        return result

    # ------------------------------------------------------------------
    # Identidad del espacio personal
    # ------------------------------------------------------------------

    def owner_for(self, channel: CaptureChannel | str, user_id: str | None = None) -> str:
        """Resuelve el propietario del espacio personal. Unico punto de verdad.

        El espacio personal es **uno**. Voz y chat no traen identidad
        autenticada (seccion 9.6): el cliente de chat manda su propio nombre y la
        app movil no manda ninguno, asi que sin esta resolucion una captura por
        voz se guardaria bajo un propietario que el dashboard nunca consulta, y
        el espacio pareceria vacio sin que nada fallara.

        La API si trae usuario autenticado y lo conserva: ahi el aislamiento por
        propietario ya funciona y no hay que sustituirlo.
        """
        configured = str(getattr(self._settings, "HORIZON_OWNER_USER_ID", "") or "").strip()
        value = str(channel.value if isinstance(channel, CaptureChannel) else channel or "")
        if value in (CaptureChannel.CHAT.value, CaptureChannel.VOICE.value):
            return configured or (user_id or "").strip()
        return (user_id or "").strip() or configured

    # ------------------------------------------------------------------
    # Plan del dia
    # ------------------------------------------------------------------

    def _timezone(self, profile: dict[str, Any]) -> str:
        mandate = profile.get("mandate") or {}
        return str(mandate.get("timezone") or self._default_timezone())

    def _carried_capacity(
        self, current_plan: dict[str, Any] | None, date_key: str
    ) -> tuple[int | None, bool | None]:
        """Reaprovecha la disponibilidad ya declarada para hoy.

        El propio plan es el registro de lo que el usuario dijo: si hoy declaro
        15 minutos y luego llega un resultado de ticket, la replanificacion no
        puede olvidar ese dato. Solo se hereda si el plan es del mismo dia.
        """
        if not current_plan or str(current_plan.get("date_key")) != date_key:
            return None, None
        capacity = current_plan.get("capacity") or {}
        free = capacity.get("free_minutes") or {}
        value = free.get("value")
        return (int(value) if value is not None else None), capacity.get("exercise_included")

    async def plan_day(
        self,
        user_id: str,
        *,
        declared_free_minutes: int | None = None,
        exercise_included: bool | None = None,
        date_key: str | None = None,
        trigger_kind: str = TriggerKind.DAILY_PLAN.value,
        source: str = "user",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Calcula y publica el plan del dia. Camino unico de escritura del plan."""
        moment = now or _now()
        profile = await self.ensure_profile(user_id)
        timezone_name = self._timezone(profile)
        mandate = mandate_module.HorizonMandate(**(profile.get("mandate") or {}))
        resolved_date = date_key or triggers_module.local_date_key(moment, timezone_name)

        current_plan = await self._repo.get_current_horizon_plan(user_id)
        carried_free, carried_exercise = self._carried_capacity(current_plan, resolved_date)
        free = declared_free_minutes if declared_free_minutes is not None else carried_free
        exercise = (
            exercise_included if exercise_included is not None else carried_exercise
        )

        capacity = capacity_module.compute_capacity(
            mandate=mandate,
            date_key=resolved_date,
            declared_free_minutes=free,
            exercise_included=exercise,
            source=source,
        )

        objectives = await self._repo.list_horizon_objectives(user_id)
        actions = await self._repo.list_horizon_actions(
            user_id,
            statuses=[ActionStatus.READY.value, ActionStatus.IN_PROGRESS.value],
        )

        payload = planner_module.plan_day(
            user_id=user_id,
            date_key=resolved_date,
            timezone_name=timezone_name,
            mandate=mandate,
            objectives=objectives,
            actions=actions,
            capacity=capacity,
            current_plan=current_plan,
            trigger_kind=trigger_kind,
            now=moment,
        )
        plan, outcome = await self.publish_plan(payload)
        result = dict(plan or {})
        result["publish_outcome"] = outcome
        return result

    async def publish_plan(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Publica el plan como version vigente, resolviendo carreras.

        Los efectos derivados solo se aplican si esta publicacion gano. Si otro
        escritor publico antes, la escritura se descarta entera: aplicar sus
        efectos dejaria acciones mutadas por un plan que nunca llego a vigente.
        """
        side_effects = dict(payload.pop("_side_effects", {}) or {})
        user_id = str(payload.get("user_id") or "")
        version = await self._repo.next_horizon_plan_version(user_id)
        payload["version"] = version
        payload["is_current"] = False

        created = await self._repo.create_horizon_plan(payload)
        plan_id = str(created.get("plan_id") or "")

        published = await self._repo.set_current_horizon_plan(user_id, plan_id)
        if published is None:
            winner = await self._repo.get_current_horizon_plan(user_id)
            if winner is not None and str(winner.get("plan_id")) != plan_id:
                await self._repo.patch_horizon_plan(
                    plan_id,
                    {
                        "status": "superseded",
                        "superseded_by": winner.get("plan_id"),
                        "superseded_at": _now(),
                    },
                )
                return winner, "stale_discarded"
            return None, "publish_failed"

        await self._apply_side_effects(side_effects)
        return published, "published"

    async def _apply_side_effects(self, side_effects: dict[str, Any]) -> None:
        for adjustment in side_effects.get("adjustments") or []:
            action_id = str(adjustment.get("action_id") or "")
            if not action_id:
                continue
            updates = {k: v for k, v in adjustment.items() if k != "action_id"}
            updates["last_replan_at"] = _now()
            # Un vencimiento externo no se mueve desde el planificador (9.10).
            updates.pop("due_date", None)
            await self._repo.patch_horizon_action(action_id, updates)

        for waiting in side_effects.get("waiting") or []:
            action_id = str(waiting.get("action_id") or "")
            if not action_id:
                continue
            await self._repo.patch_horizon_action(
                action_id,
                {
                    "status": ActionStatus.WAITING.value,
                    "blocked_reason": "dependencia_de_tercero",
                    "last_replan_at": _now(),
                },
            )

    async def current_plan(self, user_id: str) -> dict[str, Any] | None:
        return await self._repo.get_current_horizon_plan(user_id)

    async def plan_today(
        self,
        user_id: str,
        *,
        channel: str = "api",
        date_key: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Plan vigente de hoy. Si no existe, lo prepara y lo deja disponible."""
        moment = now or _now()
        profile = await self.ensure_profile(user_id)
        resolved_date = date_key or triggers_module.local_date_key(
            moment, self._timezone(profile)
        )
        current = await self._repo.get_current_horizon_plan(user_id)
        if current and str(current.get("date_key")) == resolved_date:
            return current

        plan = await self.plan_day(
            user_id,
            date_key=resolved_date,
            trigger_kind=TriggerKind.DAILY_PLAN.value,
            source="on_demand",
            now=moment,
        )
        return plan

    async def deliver_plan(
        self,
        user_id: str,
        plan: dict[str, Any],
        *,
        channel: str = "api",
    ) -> dict[str, Any]:
        """Registra el aviso de lo que se le mostro al usuario.

        El aviso guarda que accion se mostraba. Es lo que despues permite que un
        "hecho" tardio actualice esa accion y no la que este vigente hoy (9.10).
        """
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id:
            return plan
        action_id = str(plan.get("primary_action_id") or "")
        if not action_id:
            return plan

        shown = [
            {
                "action_id": str(b.get("action_id") or ""),
                "label": str(b.get("label") or ""),
            }
            for b in (plan.get("blocks") or [])
            if b.get("is_growth") and b.get("action_id")
        ]
        aviso = PlanAviso(
            action_id=action_id,
            kind=(
                AvisoKind.DAILY_PLAN
                if str(plan.get("trigger_kind")) == TriggerKind.DAILY_PLAN.value
                else AvisoKind.NEXT_ACTION
            ),
            channel=channel,
            text=str(plan.get("message") or ""),
        )
        stored = document_dump(aviso)
        stored["shown_actions"] = shown
        updated = await self._repo.push_horizon_plan_aviso(plan_id, stored)
        if updated is None:
            return plan
        updated = await self._repo.patch_horizon_plan(
            plan_id,
            {"delivered_at": _now(), "delivery_channel": channel},
        ) or updated
        return updated

    # ------------------------------------------------------------------
    # Disparadores
    # ------------------------------------------------------------------

    async def handle_trigger(self, trigger: dict[str, Any]) -> dict[str, Any]:
        """Ejecuta un disparador vencido. El timer no sabe de reglas."""
        kind = str(trigger.get("kind") or "")
        payload = trigger.get("payload") or {}
        user_id = str(trigger.get("user_id") or "")

        if kind in (TriggerKind.DAILY_PLAN.value, TriggerKind.CLOSE_DAY.value):
            plan = await self.plan_day(
                user_id,
                trigger_kind=kind,
                date_key=str(payload.get("date_key") or "") or None,
            )
            return {
                "plan_id": plan.get("plan_id"),
                "version": plan.get("version"),
                "status": plan.get("status"),
                "message": plan.get("message"),
            }

        if kind in (TriggerKind.WEEKLY_REVIEW.value, TriggerKind.MONTHLY_REVIEW.value):
            review = await self.review_objectives(user_id, kind=kind)
            return review

        if kind == TriggerKind.REACTIVE_REPLAN.value:
            return await self._replan_after_result(user_id)

        return {"handled": False, "reason": f"kind_desconocido:{kind}"}

    async def _replan_after_result(self, user_id: str) -> dict[str, Any]:
        plan = await self.plan_day(
            user_id,
            trigger_kind=TriggerKind.REACTIVE_REPLAN.value,
            source="task_result",
        )
        return {
            "plan_id": plan.get("plan_id"),
            "version": plan.get("version"),
            "status": plan.get("status"),
            "message": plan.get("message"),
            "publish_outcome": plan.get("publish_outcome"),
        }

    async def ensure_upcoming_triggers(
        self, user_id: str, *, now: datetime | None = None, store: Any = None
    ) -> int:
        """Deja encolada la proxima ocurrencia de cada trabajo recurrente."""
        profile = await self.ensure_profile(user_id)
        resolved_store = store or triggers_module.HorizonTriggerStore(
            self._repo, settings=self._settings
        )
        return await resolved_store.ensure_upcoming(
            user_id=user_id,
            timezone_name=self._timezone(profile),
            now=now or _now(),
        )

    async def list_triggers(
        self, user_id: str, *, statuses: list[str] | None = None
    ) -> list[dict[str, Any]]:
        return await self._repo.list_horizon_triggers(user_id, statuses=statuses)

    async def enqueue_replan(
        self, user_id: str, *, task_id: str, status: str, store: Any = None
    ) -> bool:
        """Encola una replanificacion por resultado de tarea, sin duplicarla."""
        resolved_store = store or triggers_module.HorizonTriggerStore(
            self._repo, settings=self._settings
        )
        key = triggers_module.dedupe_key(
            TriggerKind.REACTIVE_REPLAN.value, user_id, task_id=task_id, status=status
        )
        before = await self._repo.get_horizon_trigger_by_dedupe(key)
        if before:
            return False
        created = await resolved_store.enqueue(
            user_id=user_id,
            kind=TriggerKind.REACTIVE_REPLAN.value,
            dedupe=key,
            payload={"task_id": task_id, "status": status},
        )
        return created is not None

    # ------------------------------------------------------------------
    # Resultado de tarea y "hecho" tardio
    # ------------------------------------------------------------------

    async def acknowledge_task_result(self, task: dict[str, Any]) -> dict[str, Any]:
        """Registra el resultado de una tarea personal o promovida y replanifica.

        Se resuelve por ``task_id``, nunca por cercania temporal en el chat: un
        resultado de agente es evidencia objetiva y no debe depender de cual fue
        el ultimo mensaje.
        """
        user_id = privacy_module.personal_owner(task)
        task_id = str(task.get("task_id") or "")
        status = str(task.get("status") or "")
        if not user_id and promotion_module.is_horizon_promoted(task):
            # Trabajo promovido: resuelve por proveniencia, no por alcance
            # personal (un promovido nunca es personal). Sin esto las
            # promovidas quedarian en curso para siempre y la entrega unica
            # no las veria.
            params = task.get("params") if isinstance(task.get("params"), dict) else {}
            user_id = str(params.get(promotion_module.PROMOTION_OWNER_KEY) or "")
        if not user_id or not task_id:
            return {"recorded": False, "reason": "not_personal_scope"}

        action = await self._repo.get_horizon_action_by_task_id(user_id, task_id)
        source_id = f"task:{task_id}:{status}"

        progress = await self.record_progress(
            user_id,
            f"Resultado de la tarea {task_id}: {status}",
            source_id=source_id,
            objective_id=str((action or {}).get("objective_id") or "") or None,
            source="agent",
        )

        if action:
            updates: dict[str, Any] = {"last_replan_at": _now()}
            if status == "completed":
                updates["status"] = ActionStatus.DONE.value
            elif status in ("failed", "escalated"):
                updates["status"] = ActionStatus.BLOCKED.value
                updates["blocked_reason"] = f"tarea_{status}"

            # HZ-003: Guardar evidencia y resumen para entrega unica
            summary = str(task.get("last_response_summary") or task.get("error") or status)
            params = task.get("params") or {}
            links: list[str] = []
            if isinstance(params, dict):
                if params.get("review_links") and isinstance(params["review_links"], list):
                    links.extend([str(l) for l in params["review_links"]])
                if params.get("test_url"):
                    links.append(str(params["test_url"]))
                if params.get("client_review_url"):
                    links.append(str(params["client_review_url"]))
            updates["result_summary"] = summary
            updates["evidence_links"] = links
            updates["delivered_at"] = None
            await self._repo.patch_horizon_action(str(action.get("action_id")), updates)

        enqueued = await self.enqueue_replan(user_id, task_id=task_id, status=status)
        return {
            "recorded": bool(progress.get("recorded")),
            "duplicate": bool(progress.get("duplicate")),
            "action_id": (action or {}).get("action_id"),
            "replan_enqueued": enqueued,
        }

    # ------------------------------------------------------------------
    # F5: promocion automatica a empresa (TASK-2026-00121)
    # ------------------------------------------------------------------

    def _company_mandate(self) -> Any | None:
        """Mandato de empresa validado, o None si falta o es ilegible.

        None no deshabilita la promocion: ``requires_owner`` escala todo sin
        mandato valido (fail-closed hacia preguntar, no hacia actuar).
        """
        loader = self._company_config
        if loader is None:
            return None
        try:
            return loader.load_company_mandate()
        except Exception:
            logger.warning(
                "Company mandate unreadable; promotion escalates fail-closed",
                exc_info=True,
            )
            return None

    async def _promotion_objective_link(
        self, project: str
    ) -> tuple[str | None, str | None]:
        """Enlace (parent_task, objective_id) hacia el objetivo del proyecto.

        Solo cuando hay EXACTAMENTE un objetivo activo Y tiene grupo ERP:
        cero, dos o mas, o uno sin grupo van sueltos (sin medio enlace).
        Sin listador (tests) tambien sueltos.
        """
        lister = getattr(self._repo, "list_active_objectives", None)
        if lister is None:
            return None, None
        try:
            rows = await lister(project)
        except Exception:
            return None, None
        actives = [
            row
            for row in (rows or [])
            if isinstance(row, dict) and str(row.get("status") or "") == "active"
        ]
        if len(actives) != 1:
            return None, None
        ref = str(actives[0].get("erp_group_ref") or "").strip()
        if not ref:
            return None, None
        objective_id = str(actives[0].get("objective_id") or "").strip() or None
        return ref, objective_id

    async def promote_action(
        self,
        user_id: str,
        *,
        action_id: str | None = None,
        project: str | None = None,
        repo: str | None = None,
        kind: str = "ticket",
        instruction: str | None = None,
        dedupe_key: str | None = None,
        amount: Any | None = None,
        irreversible: bool = False,
        strategic_replan: bool = False,
        human_action_hint: str | None = None,
        task_service: Any | None = None,
    ) -> dict[str, Any]:
        """Promueve una accion de empresa a Objetivo o ticket (ERPNext primero).

        Solo viaja la especificacion del trabajo (``promotion.build_work_spec``).
        Pregunta al dueno solo si la politica de escalado (TASK-2026-00114) lo
        exige; lo demas se hace sin preguntar. La capacidad del dueno no se
        consulta: frena los escalados (la pregunta espera), nunca el trabajo
        autonomo. Idempotente por accion y por ``source_id`` de la API.
        """
        target_action: dict[str, Any] | None = None
        if action_id:
            target_action = await self._repo.get_horizon_action(action_id)
            if not target_action or target_action.get("user_id") != user_id:
                return {
                    "promoted": False,
                    "reason": "action_not_found",
                    "message": "Acción no encontrada.",
                }
        else:
            chosen_res = await self.next_action(user_id)
            if not chosen_res.get("has_action") or not chosen_res.get("action"):
                return {
                    "promoted": False,
                    "reason": "no_action_available",
                    "message": "No hay ninguna acción lista para promover.",
                }
            target_action = chosen_res["action"]

        aid = str(target_action.get("action_id") or "")
        svc = task_service or self._task_service
        if svc is None:
            return {
                "promoted": False,
                "reason": "task_service_unavailable",
                "message": "El servicio de tareas no está disponible para promover.",
            }

        resolved_kind = str(kind or "ticket").strip().lower()
        if resolved_kind not in ("ticket", "objective"):
            return {
                "promoted": False,
                "reason": "unknown_kind",
                "message": f"Destino desconocido: {kind}. Usa ticket u objective.",
            }

        classification = promotion_module.classify_action(
            target_action,
            explicit_project=project,
            explicit_repo=repo,
            company_config=self._company_config,
        )
        scope = classification["scope"]
        if scope == promotion_module.SCOPE_PERSONAL:
            return {
                "promoted": False,
                "reason": "not_company_work",
                "action_id": aid,
                "message": (
                    "Esa acción es personal: no toca ningún proyecto o repo "
                    "de empresa. Para ejecutarla en dev usa delegar."
                ),
            }
        if scope == promotion_module.SCOPE_REFUSED:
            reason = str(classification["reason"])
            return {
                "promoted": False,
                "reason": reason,
                "action_id": aid,
                "message": promotion_module.REFUSAL_MESSAGES.get(
                    reason, "Promoción rechazada; no se creó nada."
                ),
            }

        company_project = str(classification["project"] or "")

        # Declaraciones explicitas validadas: persisten para que la proxima
        # delegacion promueva sola. "" borra. Solo se escriben en caminos
        # con efecto; los no-op no mutan estado.
        declarations: dict[str, Any] = {}
        if project is not None:
            declarations["company_project"] = str(project or "").strip() or None
        if repo is not None:
            declarations["company_repo"] = str(repo or "").strip() or None

        current_status = str(target_action.get("status") or "")
        if (
            target_action.get("promoted_at")
            and current_status == ActionStatus.IN_PROGRESS.value
        ):
            # Retry of a linked promotion whose contract never landed:
            # re-drive planning instead of duplicating (TASK-2026-00174).
            redriven: list[str] = []
            if (target_action.get("promotion_kind") or "ticket") == "ticket":
                redriven = await self._redrive_linked_contract(
                    svc, str(target_action.get("task_id") or ""))
            return {
                "promoted": True,
                "already_promoted": True,
                "action_id": aid,
                "task_id": target_action.get("task_id"),
                "project": target_action.get("delegated_project") or company_project,
                "kind": target_action.get("promotion_kind") or "ticket",
                "planning_redriven": bool(redriven),
                "contracts_completed": redriven,
                "message": "Esa acción ya está promovida y en curso.",
            }
        if (
            target_action.get("task_id")
            and current_status == ActionStatus.IN_PROGRESS.value
        ):
            return {
                "promoted": False,
                "reason": "already_delegated_personal",
                "action_id": aid,
                "task_id": target_action.get("task_id"),
                "message": (
                    "Esa acción ya está delegada como personal; "
                    "no la duplico como empresa."
                ),
            }

        # Escalado pendiente: relee la decision grabada, no el estado (aprobar
        # y denegar dejan `completed`). Sin registro, se re-escala con el
        # mismo source_id.
        skip_escalation = False
        escalation_id = str(target_action.get("promotion_escalation_task_id") or "")
        if escalation_id and current_status == ActionStatus.WAITING.value:
            escalation_task = await svc.get_task(escalation_id)
            outcome = promotion_module.escalation_outcome(escalation_task)
            if outcome == promotion_module.ESCALATION_WAITING:
                return {
                    "promoted": False,
                    "escalated": True,
                    "already_escalated": True,
                    "action_id": aid,
                    "escalation_task_id": escalation_id,
                    "message": (
                        "Esa promoción sigue esperando tu respuesta; "
                        "no la dupliqué."
                    ),
                }
            if outcome in (
                promotion_module.ESCALATION_DENIED,
                promotion_module.ESCALATION_CLOSED,
            ):
                return {
                    "promoted": False,
                    "reason": "escalation_denied",
                    "action_id": aid,
                    "escalation_task_id": escalation_id,
                    "message": (
                        "Esa promoción fue denegada; no se creó "
                        "trabajo de empresa."
                    ),
                }
            skip_escalation = outcome == promotion_module.ESCALATION_APPROVED

        spec = promotion_module.build_work_spec(target_action, instruction)

        if not skip_escalation:
            from company_mandate import OwnerAction, requires_owner

            owner_action = OwnerAction(
                kind="horizon_promotion",
                summary=spec["title"],
                project=company_project,
                amount=amount,
                irreversible=bool(irreversible),
                strategic_replan=bool(strategic_replan),
                human_action_hint=(str(human_action_hint or "").strip() or None),
            )
            try:
                decision = requires_owner(owner_action, self._company_mandate())
            except Exception as exc:
                return {
                    "promoted": False,
                    "reason": "invalid_signals",
                    "action_id": aid,
                    "detail": str(exc)[:300],
                    "message": (
                        "Las señales de la promoción no son válidas; "
                        "no se creó nada."
                    ),
                }
            if decision.escalate:
                created = await svc.create_owner_escalation(
                    action=owner_action,
                    decision=decision,
                    source_id=promotion_module.promotion_escalation_source_id(aid),
                )
                esc_task_id = str(created.get("task_id") or "")
                updates = {
                    **declarations,
                    "status": ActionStatus.WAITING.value,
                    "blocked_reason": "owner_escalation",
                    "promotion_escalation_task_id": esc_task_id,
                    "promotion_kind": resolved_kind,
                }
                await self._repo.patch_horizon_action(aid, updates)
                category = getattr(decision.category, "value", decision.category)
                human_action = decision.human_action or decision.reason
                return {
                    "promoted": False,
                    "escalated": True,
                    "already_escalated": False,
                    "action_id": aid,
                    "escalation_task_id": esc_task_id,
                    "category": str(category or ""),
                    "human_action": decision.human_action,
                    "message": f"Antes de promover necesito tu respuesta: {human_action}",
                }

        from state import AgentName, TaskCreateRequest, TaskPriority, TaskSource

        priority_val = int(target_action.get("priority") or 100)
        task_priority = TaskPriority.HIGH if priority_val <= 50 else TaskPriority.MEDIUM

        if resolved_kind == "objective":
            from objectives.engine import ObjectiveErpError, ObjectiveRejected

            acceptance = " ".join(str(target_action.get("acceptance") or "").split())
            try:
                created_obj = await svc.create_objective(
                    project=company_project,
                    goal=spec["title"],
                    acceptance=[acceptance] if acceptance else None,
                    entry="horizon_promotion",
                    source=TaskSource.API.value,
                    source_id=promotion_module.promotion_objective_source_id(aid),
                    requester=user_id,
                )
            except (ObjectiveRejected, ObjectiveErpError) as exc:
                return {
                    "promoted": False,
                    "reason": "objective_rejected",
                    "action_id": aid,
                    "detail": str(exc)[:300],
                    "message": "El objetivo no fue admitido; no se creó nada.",
                }
            objective_id = str(created_obj.get("objective_id") or "")
            updates = {
                **declarations,
                "task_id": str(created_obj.get("conversion_source_task_id") or "") or None,
                "status": ActionStatus.IN_PROGRESS.value,
                "owner": ActionOwner.AGENT.value,
                "delegated_at": _now(),
                "delegated_project": company_project,
                "delivered_at": None,
                "result_summary": None,
                "promoted_at": _now(),
                "promotion_kind": "objective",
                "promotion_ref": objective_id or None,
            }
            await self._repo.patch_horizon_action(aid, updates)
            return {
                "promoted": True,
                "already_promoted": False,
                "action_id": aid,
                "objective_id": objective_id,
                "task_id": updates["task_id"],
                "project": company_project,
                "kind": "objective",
                "message": f"Promoví «{spec['title']}» a objetivo de {company_project}.",
            }

        parent_task, objective_id = await self._promotion_objective_link(
            company_project
        )
        task_req = TaskCreateRequest(
            project=company_project,
            source=TaskSource.API,
            source_id=promotion_module.promotion_source_id(aid),
            title=spec["title"][:120],
            description=spec["description"],
            agent_hint=AgentName.DEV,
            priority=task_priority,
            autorun=True,
            params=promotion_module.promotion_params(user_id, aid, dedupe_key),
            parent_task=parent_task,
            objective_id=objective_id,
        )
        created = await svc.create_task(task_req)
        task_id = str(created.get("task_id") or "")
        contracts_completed: list[str] = []
        if objective_id:
            # The linked stub is tierless by construction: the single
            # planner completes its contract (TASK-2026-00174). Never
            # fails the promotion: without a contract the stub waits
            # exactly as before, and a retry re-drives the plan.
            contracts_completed = await self._plan_linked_objective(
                svc, objective_id)
        promotion_ref = None
        created_params = created.get("params")
        mirrors = (
            created_params.get("_mirrors")
            if isinstance(created_params, dict)
            else None
        )
        if isinstance(mirrors, dict):
            erp = mirrors.get("erpnext")
            if isinstance(erp, dict):
                promotion_ref = str(erp.get("name") or "").strip() or None
        updates = {
            **declarations,
            "task_id": task_id,
            "status": ActionStatus.IN_PROGRESS.value,
            "owner": ActionOwner.AGENT.value,
            "delegated_at": _now(),
            "delegated_project": company_project,
            "delivered_at": None,
            "result_summary": None,
            "promoted_at": _now(),
            "promotion_kind": "ticket",
            "promotion_ref": promotion_ref,
        }
        await self._repo.patch_horizon_action(aid, updates)
        return {
            "promoted": True,
            "already_promoted": False,
            "action_id": aid,
            "task_id": task_id,
            "project": company_project,
            "kind": "ticket",
            "objective_id": objective_id,
            "planning_triggered": bool(objective_id),
            "contracts_completed": contracts_completed,
            "message": f"Promoví «{spec['title']}» a ticket de {company_project}.",
        }

    async def _plan_linked_objective(
        self,
        svc: Any,
        objective_id: str,
    ) -> list[str]:
        """Trigger one planning cycle; completed contract task_ids.

        Never raises: planning failures degrade to the pre-00174 wait
        (the stub stays tierless and visible) instead of failing the
        promotion that already created the ticket.
        """
        plan = getattr(svc, "plan_objective", None)
        if plan is None or not objective_id:
            return []
        try:
            result = await plan(objective_id)
        except Exception:
            logger.warning("linked promotion planning failed for %s",
                           objective_id, exc_info=True)
            return []
        if not isinstance(result, dict) or not result.get("ok"):
            return []
        completed = result.get("contracts_completed") or []
        return [str(task_id) for task_id in completed]

    async def _redrive_linked_contract(
        self,
        svc: Any,
        task_id: str,
    ) -> list[str]:
        """Re-drive planning when a retry finds its stub still tierless."""
        if not task_id:
            return []
        try:
            task = await svc.get_task(task_id)
        except Exception:
            return []
        if not isinstance(task, dict):
            return []
        if task.get("coding_tool") or not task.get("objective_id"):
            return []
        return await self._plan_linked_objective(
            svc, str(task.get("objective_id") or ""))

    # ------------------------------------------------------------------
    # HZ-003: Delegacion autonoma y dev movil
    # ------------------------------------------------------------------

    async def delegate_action(
        self,
        user_id: str,
        *,
        action_id: str | None = None,
        project: str | None = None,
        instruction: str | None = None,
        dedupe_key: str | None = None,
        task_service: Any | None = None,
    ) -> dict[str, Any]:
        """Encarga una accion de alcance personal para ejecucion autonoma en background.

        Seccion 9.6: Identificar proyecto/ticket, encargar, desconectar y conservar contexto.
        """
        target_action: dict[str, Any] | None = None
        if action_id:
            target_action = await self._repo.get_horizon_action(action_id)
            if not target_action or target_action.get("user_id") != user_id:
                return {
                    "delegated": False,
                    "reason": "action_not_found",
                    "message": "Acción no encontrada.",
                }
        else:
            chosen_res = await self.next_action(user_id)
            if not chosen_res.get("has_action") or not chosen_res.get("action"):
                return {
                    "delegated": False,
                    "reason": "no_action_available",
                    "message": "No hay ninguna acción lista para delegar.",
                }
            target_action = chosen_res["action"]

        aid = str(target_action.get("action_id") or "")

        # F5 (TASK-2026-00121): una accion que toca empresa no se delega como
        # personal solo-Mongo; se promueve por la API comun (ERPNext primero).
        # El `project` del encargo es substrato, no propiedad: no declara.
        touch = promotion_module.classify_action(
            target_action, company_config=self._company_config
        )
        if touch["scope"] == promotion_module.SCOPE_COMPANY:
            result = await self.promote_action(
                user_id,
                action_id=aid,
                instruction=instruction,
                dedupe_key=dedupe_key,
                task_service=task_service,
            )
            result["delegated"] = bool(result.get("promoted"))
            return result
        if touch["scope"] == promotion_module.SCOPE_REFUSED:
            reason = str(touch["reason"])
            return {
                "delegated": False,
                "reason": reason,
                "message": promotion_module.REFUSAL_MESSAGES.get(
                    reason, "No se puede delegar; no se creó nada."
                ),
            }

        # Idempotencia y reconexion movil: si ya tiene tarea activa en curso, devolverla sin duplicar
        existing_task_id = target_action.get("task_id")
        current_status = str(target_action.get("status") or "")
        if existing_task_id and current_status == ActionStatus.IN_PROGRESS.value:
            return {
                "delegated": True,
                "already_in_progress": True,
                "action_id": aid,
                "task_id": existing_task_id,
                "project": target_action.get("delegated_project") or project or "jardal-openceo",
                "message": f"La tarea ya está delegada ({existing_task_id}) y ejecutándose en segundo plano.",
            }

        svc = task_service or self._task_service
        if svc is None:
            return {
                "delegated": False,
                "reason": "task_service_unavailable",
                "message": "El servicio de tareas no está disponible para delegar.",
            }

        resolved_project = project or target_action.get("delegated_project") or "jardal-openceo"
        title = f"[Horizon] {target_action.get('title') or 'Acción personal'}"
        desc = instruction or target_action.get("title") or "Tarea delegada desde Horizon"
        task_params = privacy_module.personal_scope_params(
            user_id,
            extra={
                "horizon_action_id": aid,
                "dedupe_key": dedupe_key or f"del:{aid}",
                "delegated_by": "horizon",
            },
        )

        from state import AgentName, TaskCreateRequest, TaskPriority, TaskSource

        priority_val = int(target_action.get("priority") or 100)
        task_priority = TaskPriority.HIGH if priority_val <= 50 else TaskPriority.MEDIUM

        task_req = TaskCreateRequest(
            project=resolved_project,
            source=TaskSource.API,
            title=title[:120],
            description=desc,
            agent_hint=AgentName.DEV,
            priority=task_priority,
            autorun=True,
            params=task_params,
        )

        created = await svc.create_task(task_req)
        task_id = str(created.get("task_id") or "")

        updates = {
            "task_id": task_id,
            "status": ActionStatus.IN_PROGRESS.value,
            "owner": ActionOwner.AGENT.value,
            "delegated_at": _now(),
            "delegated_project": resolved_project,
            "delivered_at": None,
            "result_summary": None,
        }
        await self._repo.patch_horizon_action(aid, updates)

        return {
            "delegated": True,
            "already_in_progress": False,
            "action_id": aid,
            "task_id": task_id,
            "project": resolved_project,
            "status": ActionStatus.IN_PROGRESS.value,
            "message": f"Acción '{target_action.get('title')}' delegada a dev en {resolved_project}. Ejecutando en segundo plano.",
        }

    async def deliver_action_result(
        self,
        user_id: str,
        *,
        action_id: str | None = None,
        mark_delivered: bool = True,
        channel: str = "chat",
    ) -> dict[str, Any]:
        """Entrega el resultado de una tarea delegada UNA SOLA VEZ (seccion 9.8)."""
        target: dict[str, Any] | None = None
        if action_id:
            target = await self._repo.get_horizon_action(action_id)
            if not target or target.get("user_id") != user_id:
                return {
                    "delivered": False,
                    "reason": "action_not_found",
                    "message": "Acción no encontrada.",
                }
        else:
            actions = await self._repo.list_horizon_actions(
                user_id,
                statuses=[ActionStatus.DONE.value, ActionStatus.BLOCKED.value],
                limit=50,
            )
            delegated = [a for a in actions if a.get("task_id")]
            if not delegated:
                return {
                    "delivered": False,
                    "reason": "no_delegated_actions",
                    "message": "No hay resultados de tareas delegadas disponibles.",
                }
            undelivered = [a for a in delegated if not a.get("delivered_at")]
            target = undelivered[0] if undelivered else delegated[0]

        aid = str(target.get("action_id"))
        status = str(target.get("status"))
        summary = target.get("result_summary") or "Sin resumen disponible."
        evidence = target.get("evidence_links") or []
        was_delivered = target.get("delivered_at") is not None

        if not was_delivered and mark_delivered:
            delivered_time = _now()
            await self._repo.patch_horizon_action(
                aid,
                {"delivered_at": delivered_time, "delivery_channel": channel},
            )
            target["delivered_at"] = delivered_time

        prefix = "Resultado ya entregado previamente" if was_delivered else "Resultado de la tarea delegada"
        msg = f"{prefix} ('{target.get('title')}'): estado {status}. {summary}"

        return {
            "delivered": True,
            "already_delivered": was_delivered,
            "action_id": aid,
            "task_id": target.get("task_id"),
            "status": status,
            "result_summary": summary,
            "evidence_links": evidence,
            "delivered_at": target.get("delivered_at"),
            "message": msg,
        }

    async def record_delegation_metrics(
        self,
        user_id: str,
        action_id: str,
        *,
        human_supervision_minutes: int | None = None,
        human_rework_minutes: int | None = None,
        rework: bool = False,
        note: str = "",
    ) -> dict[str, Any]:
        """Registra tiempo humano de encargo/supervisión y retrabajo (seccion 9.6 y 10.3)."""
        target = await self._repo.get_horizon_action(action_id)
        if not target or target.get("user_id") != user_id:
            return {
                "recorded": False,
                "reason": "action_not_found",
                "message": "Acción no encontrada.",
            }

        cur_sup = int(target.get("human_supervision_minutes") or 0)
        cur_rew = int(target.get("human_rework_minutes") or 0)
        cur_cnt = int(target.get("rework_count") or 0)

        add_sup = int(human_supervision_minutes or 0)
        add_rew = int(human_rework_minutes or 0)
        inc_cnt = 1 if rework or add_rew > 0 else 0

        updates = {
            "human_supervision_minutes": cur_sup + add_sup,
            "human_rework_minutes": cur_rew + add_rew,
            "rework_count": cur_cnt + inc_cnt,
        }
        await self._repo.patch_horizon_action(action_id, updates)

        event_text = note or f"Métricas para '{target.get('title')}': {add_sup} min supervisión, {add_rew} min retrabajo."
        source_id = f"metrics:{action_id}:{_now().timestamp()}"
        await self.record_progress(
            user_id,
            event_text,
            source_id=source_id,
            objective_id=target.get("objective_id"),
            source="user",
        )

        return {
            "recorded": True,
            "action_id": action_id,
            "human_supervision_minutes": cur_sup + add_sup,
            "human_rework_minutes": cur_rew + add_rew,
            "rework_count": cur_cnt + inc_cnt,
            "message": f"Registrado: {add_sup} min de supervisión y {add_rew} min de retrabajo.",
        }

    async def get_delegation_summary(self, user_id: str) -> dict[str, Any]:
        """Calcula el resumen de delegacion efectiva segun la tabla de indicadores (seccion 10.3)."""
        actions = await self._repo.list_horizon_actions(user_id, limit=200)
        delegated = [a for a in actions if a.get("task_id")]

        total = len(delegated)
        completed = sum(1 for a in delegated if str(a.get("status")) == ActionStatus.DONE.value)
        blocked = sum(1 for a in delegated if str(a.get("status")) == ActionStatus.BLOCKED.value)
        in_progress = sum(1 for a in delegated if str(a.get("status")) == ActionStatus.IN_PROGRESS.value)
        total_sup = sum(int(a.get("human_supervision_minutes") or 0) for a in delegated)
        total_rew = sum(int(a.get("human_rework_minutes") or 0) for a in delegated)
        total_rework_count = sum(int(a.get("rework_count") or 0) for a in delegated)
        pending_delivery = sum(
            1 for a in delegated
            if str(a.get("status")) in (ActionStatus.DONE.value, ActionStatus.BLOCKED.value)
            and not a.get("delivered_at")
        )

        msg = (
            f"Tenemos {total} tareas delegadas: {completed} completadas, {in_progress} en curso "
            f"y {blocked} bloqueadas. Tiempo humano: {total_sup} min de supervisión, "
            f"{total_rew} min de retrabajo ({total_rework_count} retrabajos)."
        )

        return {
            "total_delegated": total,
            "completed": completed,
            "blocked": blocked,
            "in_progress": in_progress,
            "pending_delivery": pending_delivery,
            "human_supervision_minutes": total_sup,
            "human_rework_minutes": total_rew,
            "rework_count": total_rework_count,
            "message": msg,
        }

    async def resolve_aviso(
        self,
        user_id: str,
        *,
        aviso_id: str | None = None,
        text: str = "",
    ) -> dict[str, Any] | None:
        """Resuelve a que accion se refiere un "hecho" tardio.

        La seccion 9.3 es explicita: "Hecho" se vincula a la accion mostrada en
        ese mensaje, no a otra accion elegida despues. Por eso el aviso mas
        reciente sin resolver gana sobre la accion en curso.

        Un aviso viejo no puede tragarse una confirmacion de hoy: si el ultimo
        aviso supera ``_AVISO_TTL_HOURS`` y existe una accion en curso, gana la
        accion en curso. Es la unica heuristica de la escalera y esta acotada.
        """
        plans = await self._repo.list_recent_horizon_plans(user_id, limit=2)
        if aviso_id:
            for plan in plans:
                for aviso in plan.get("avisos") or []:
                    if str(aviso.get("aviso_id")) == aviso_id:
                        return {"action_id": aviso.get("action_id"), "aviso": aviso, "via": "aviso_id"}

        newest: dict[str, Any] | None = None
        for plan in plans:
            for aviso in plan.get("avisos") or []:
                if aviso.get("resolved_at"):
                    continue
                shown_at = aviso.get("shown_at")
                if newest is None or (shown_at and shown_at > (newest.get("shown_at") or shown_at)):
                    newest = {**aviso, "plan_id": plan.get("plan_id")}
        if newest is not None:
            shown_at = newest.get("shown_at")
            stale = False
            if isinstance(shown_at, datetime):
                reference = shown_at if shown_at.tzinfo else shown_at.replace(tzinfo=timezone.utc)
                stale = (_now() - reference).total_seconds() > _AVISO_TTL_HOURS * 3600
            if not stale:
                return {
                    "action_id": newest.get("action_id"),
                    "aviso": newest,
                    "via": "aviso_reciente",
                }

        in_progress = await self._repo.list_horizon_actions(
            user_id, statuses=[ActionStatus.IN_PROGRESS.value]
        )
        if len(in_progress) == 1:
            return {
                "action_id": in_progress[0].get("action_id"),
                "aviso": None,
                "via": "accion_en_curso",
            }

        return None

    async def confirm_done(
        self,
        user_id: str,
        *,
        aviso_id: str | None = None,
        text: str = "",
    ) -> dict[str, Any]:
        """Cierra la accion referenciada por el aviso, no la vigente."""
        resolved = await self.resolve_aviso(user_id, aviso_id=aviso_id, text=text)
        if resolved is None:
            candidates = await self._repo.list_horizon_actions(
                user_id, statuses=[ActionStatus.READY.value, ActionStatus.IN_PROGRESS.value]
            )
            names = ", ".join(str(a.get("title") or "") for a in candidates[:2])
            return {
                "confirmed": False,
                "reason": "ambiguo",
                "message": (
                    "No sé a cuál te refieres"
                    + (f": ¿{names}?" if names else ".")
                    + " Dime cuál y lo cierro."
                ),
            }

        action_id = str(resolved.get("action_id") or "")
        action = await self._repo.patch_horizon_action(
            action_id, {"status": ActionStatus.DONE.value, "last_replan_at": _now()}
        )

        aviso = resolved.get("aviso") or {}
        if aviso.get("aviso_id"):
            plan_id = str(aviso.get("plan_id") or "")
            if plan_id:
                # Se reescribe la lista entera en vez de usar un array filter de
                # Mongo: el resultado es el mismo y no depende de sintaxis que el
                # repositorio en memoria tendria que replicar a mano.
                plan = await self._repo.get_horizon_plan(plan_id)
                avisos = list((plan or {}).get("avisos") or [])
                for item in avisos:
                    if str(item.get("aviso_id")) == str(aviso.get("aviso_id")):
                        item["resolved_at"] = _now()
                        item["resolved_action_id"] = action_id
                await self._repo.patch_horizon_plan(plan_id, {"avisos": avisos})

        await self.record_progress(
            user_id,
            text or f"Confirmado: {str((action or {}).get('title') or '')}",
            source_id=f"aviso:{aviso.get('aviso_id') or action_id}",
            source="user",
        )
        title = str((action or {}).get("title") or "esa acción")
        return {
            "confirmed": True,
            "action_id": action_id,
            "via": resolved.get("via"),
            "message": f"Listo, cierro {title.lower()}.",
        }

    # ------------------------------------------------------------------
    # Revision periodica
    # ------------------------------------------------------------------

    async def review_objectives(
        self,
        user_id: str,
        *,
        kind: str = TriggerKind.WEEKLY_REVIEW.value,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Revision determinista de resultados, desviaciones y carga."""
        from horizon import review as review_module

        moment = now or _now()
        profile = await self.ensure_profile(user_id)
        mandate = mandate_module.HorizonMandate(**(profile.get("mandate") or {}))
        objectives = await self._repo.list_horizon_objectives(user_id)
        actions = await self._repo.list_horizon_actions(
            user_id,
            statuses=[
                ActionStatus.READY.value,
                ActionStatus.IN_PROGRESS.value,
                ActionStatus.WAITING.value,
                ActionStatus.BLOCKED.value,
                ActionStatus.DONE.value,
            ],
        )
        plans = await self._repo.list_recent_horizon_plans(user_id, limit=14)
        result = review_module.run_review(
            kind=kind,
            profile=profile,
            mandate=mandate,
            objectives=objectives,
            actions=actions,
            plans=plans,
            now=moment,
        )

        # Los ajustes de estimacion y las marcas de descomposicion son
        # delegados (seccion 9.4): Horizon los aplica. Las decisiones reservadas
        # al usuario viajan en ``pending_decisions`` y no se tocan aqui.
        applied = 0
        for correction in result.get("corrections") or []:
            action_id = str(correction.get("action_id") or "")
            if not action_id:
                continue
            updates = {k: v for k, v in correction.items() if k not in ("action_id", "title")}
            updates["last_replan_at"] = _now()
            await self._repo.patch_horizon_action(action_id, updates)
            applied += 1
        result["applied_corrections"] = applied
        return result


# Un aviso sin resolver mas viejo que esto no puede tragarse una confirmacion de
# hoy (seccion 9.3: si hay ambiguedad se aclara solo esa ambiguedad).
_AVISO_TTL_HOURS = 24


def _measurement_value(measurement: tuple[int, str], source: str) -> DeclaredValue:
    """Valor declarado a partir de una medicion del usuario."""
    return DeclaredValue(
        value=int(measurement[0]),
        uncertainty=Uncertainty.DECLARED,
        source=source,
        as_of=_now(),
    )


def _unknown_value(source: str) -> DeclaredValue:
    return DeclaredValue.unknown(source)