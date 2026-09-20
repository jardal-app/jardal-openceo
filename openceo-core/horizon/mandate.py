"""Limites del mandato de Horizon, validados en codigo.

La especificacion (seccion 9.2 punto 6) es explicita: el LLM interpreta
contexto y propone acciones estructuradas; el codigo valida capacidad,
dependencias, alcance, autorizacion y presupuesto antes de ejecutarlas.

Todo lo de este modulo es determinista y sin tokens.
"""

from __future__ import annotations

from typing import Any

from horizon.models import ActionStatus, HorizonMandate, ObjectiveHorizon, ObjectiveStatus

DEFAULT_TIMEZONE = "America/La_Paz"
DEFAULT_PLAN_VERSION = "2.1"

# Techos de politica, fijados por la especificacion (9.5 y 9.9). Una
# importacion NO puede relajarlos: si el origen declara un valor mayor, se
# recorta. Cambiar esta politica es una tarea explicita de implementacion, no
# un efecto lateral de reimportar el README.
MAX_CAPACITY_FRACTION = 0.70
MAX_ACTIVE_GROWTH_GOALS = 1
MAX_READY_ACTIONS = 6
MAX_IN_PROGRESS_ACTIONS = 1


def default_mandate(
    *,
    timezone: str = DEFAULT_TIMEZONE,
    source: str | None = None,
    mirror_enabled: bool = False,
) -> HorizonMandate:
    return HorizonMandate(
        timezone=timezone,
        source=source,
        personal_external_mirror_enabled=bool(mirror_enabled),
    )


def mandate_from_declared(
    declared: dict[str, Any],
    *,
    source: str | None = None,
    capacity_ceiling: float | None = None,
    mirror_enabled: bool = False,
) -> HorizonMandate:
    """Construye el mandato desde el bloque de politica importado.

    Los limites de seguridad se recortan contra su techo: una importacion solo
    puede ser mas conservadora que la politica vigente, nunca mas permisiva.
    Si un campo no aparece en el origen, se conserva el valor por defecto del
    modelo, que tambien es el conservador.
    """
    planning = declared.get("planning") or {}
    daily_life = declared.get("daily_life") or {}
    automation = declared.get("automation") or {}

    def _int(value: Any, fallback: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    def _float(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    def _bool(value: Any, fallback: bool) -> bool:
        if isinstance(value, bool):
            return value
        return fallback

    def _clamp_int(value: int, ceiling: int) -> int:
        return max(1, min(value, ceiling))

    ceiling = MAX_CAPACITY_FRACTION if capacity_ceiling is None else float(capacity_ceiling)
    ceiling = min(ceiling, MAX_CAPACITY_FRACTION)

    mandate = HorizonMandate(
        plan_version=str(declared.get("plan_version") or DEFAULT_PLAN_VERSION),
        operating_mode=str(declared.get("operating_mode_target") or "autonomous_within_mandate"),
        active_growth_goals_max=_clamp_int(
            _int(planning.get("active_growth_goals_max"), MAX_ACTIVE_GROWTH_GOALS),
            MAX_ACTIVE_GROWTH_GOALS,
        ),
        personal_ready_actions_max=_clamp_int(
            _int(planning.get("personal_ready_tickets_max"), MAX_READY_ACTIONS),
            MAX_READY_ACTIONS,
        ),
        personal_in_progress_max=_clamp_int(
            _int(planning.get("personal_in_progress_max"), MAX_IN_PROGRESS_ACTIONS),
            MAX_IN_PROGRESS_ACTIONS,
        ),
        personal_capacity_fraction=max(
            0.0,
            min(_float(planning.get("personal_capacity_fraction"), ceiling), ceiling),
        ),
        weekly_human_exceptions_minutes_target=_int(
            planning.get("weekly_human_exceptions_minutes_target"), 10
        ),
        exercise_reserved_before_growth=_bool(
            planning.get("exercise_and_rest_reserved_before_growth"), True
        ),
        missing_data_blocks_only_dependent_actions=_bool(
            planning.get("missing_data_blocks_only_dependent_actions"), True
        ),
        timezone=str(daily_life.get("timezone_proposed") or DEFAULT_TIMEZONE),
        execute_money_movements=_bool(automation.get("execute_money_movements"), False),
        external_messages_require_human_review=_bool(
            automation.get("external_messages_require_human_review"), True
        ),
        infer_completion_from_silence=_bool(automation.get("infer_completion_from_silence"), False),
        require_deduplication=_bool(automation.get("require_deduplication"), True),
        allow_scope_expansion_without_approval=_bool(
            automation.get("allow_scope_expansion_without_approval"), False
        ),
        capture_ideas_without_reconfirmation=_bool(
            automation.get("capture_ideas_without_reconfirmation"), True
        ),
        source=source,
    )

    # El presupuesto inicial de dinero no delegado es cero y no se hereda del
    # origen: si el README cambia, el limite de seguridad se mantiene.
    mandate.execute_money_movements = False

    # El espejo externo de datos personales permanece deshabilitado salvo
    # habilitacion explicita del propietario (``HORIZON_PERSONAL_MIRROR_ENABLED``).
    # El valor declarado en el README nunca lo habilita por si solo: reimportar
    # la especificacion no puede encender la publicacion de datos privados.
    mandate.personal_external_mirror_enabled = bool(mirror_enabled)
    return mandate


class MandateViolation(Exception):
    """Propuesta rechazada por el mandato, no por el LLM."""


def clamp_capacity_fraction(fraction: float | None, mandate: HorizonMandate) -> float:
    """La capacidad programable nunca supera el techo del mandato."""
    candidate = mandate.personal_capacity_fraction if fraction is None else float(fraction)
    return max(0.0, min(candidate, mandate.personal_capacity_fraction))


def validate_growth_goal_addition(
    objective_horizon: ObjectiveHorizon,
    objective_status: ObjectiveStatus,
    active_growth_count: int,
    mandate: HorizonMandate,
) -> None:
    """Un objetivo principal de crecimiento activo, segun seccion 9.5."""
    if objective_horizon == ObjectiveHorizon.SOMEDAY or objective_status != ObjectiveStatus.ACTIVE:
        return
    if active_growth_count >= mandate.active_growth_goals_max:
        raise MandateViolation(
            "Ya existe un objetivo de crecimiento activo. "
            "Los demas se registran aparcados con condicion de revision."
        )


def validate_action_capacity(
    ready_count: int,
    in_progress_count: int,
    mandate: HorizonMandate,
) -> None:
    """Limita el trabajo abierto personal para no crear una cola ilimitada."""
    if ready_count >= mandate.personal_ready_actions_max:
        raise MandateViolation(
            f"Limite de acciones personales listas alcanzado "
            f"({mandate.personal_ready_actions_max})."
        )
    if in_progress_count >= mandate.personal_in_progress_max:
        raise MandateViolation(
            f"Ya hay una accion personal en curso "
            f"({mandate.personal_in_progress_max})."
        )


def requires_human_authorization(kind: str) -> bool:
    """Acciones que el mandato nunca ejecuta de forma autonoma."""
    return kind in {
        "money_movement",
        "external_message",
        "new_commitment",
        "scope_expansion",
        "mission_change",
        "third_party_participation",
    }


def actions_are_exhausted(actions: list[dict[str, Any]]) -> bool:
    return not any(
        str(a.get("status")) in (ActionStatus.READY.value, ActionStatus.IN_PROGRESS.value)
        for a in actions
    )