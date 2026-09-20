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

from horizon import capacity as capacity_module
from horizon import classify as classify_module
from horizon import importer as importer_module
from horizon import mandate as mandate_module
from horizon import planner as planner_module
from horizon import privacy as privacy_module
from horizon import triggers as triggers_module
from horizon.models import (
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

    def __init__(self, settings: Settings, repo: Any) -> None:
        self._settings = settings
        self._repo = repo

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

    async def next_action(self, user_id: str) -> dict[str, Any]:
        """Elige UNA accion principal, con su motivo y su siguiente paso.

        Seccion 9.5: una accion principal y hasta dos secundarias. Horizon no
        pregunta "que quieres hacer hoy" si puede elegir con lo que ya sabe.
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
            return {
                "has_action": False,
                "message": await self._no_action_message(user_id),
                "active_growth_goal": self._growth_goal(objectives),
                "ready_count": 0,
                "in_progress_count": 0,
            }

        primary = ordered[0]
        secondary = ordered[1:3]
        objective = objectives_by_id.get(str(primary.get("objective_id") or ""))

        return {
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
        """Registra el resultado de una tarea personal y pide replanificar.

        Se resuelve por ``task_id``, nunca por cercania temporal en el chat: un
        resultado de agente es evidencia objetiva y no debe depender de cual fue
        el ultimo mensaje.
        """
        user_id = privacy_module.personal_owner(task)
        task_id = str(task.get("task_id") or "")
        status = str(task.get("status") or "")
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
            await self._repo.patch_horizon_action(str(action.get("action_id")), updates)

        enqueued = await self.enqueue_replan(user_id, task_id=task_id, status=status)
        return {
            "recorded": bool(progress.get("recorded")),
            "duplicate": bool(progress.get("duplicate")),
            "action_id": (action or {}).get("action_id"),
            "replan_enqueued": enqueued,
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