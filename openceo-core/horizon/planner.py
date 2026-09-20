"""Planificador del dia: los ocho pasos de la seccion 9.5, sin LLM.

El modelo interpreta y propone; aqui solo se calcula y se aplican reglas. Por eso
el ciclo completo sigue funcionando con el presupuesto de IA agotado (9.7): no
hay ninguna llamada a un modelo en este camino.

Dos decisiones que no son obvias y conviene leer antes de tocar el orden:

- **La comparacion es lexicografica, no una suma ponderada.** La seccion 9.5 dice
  que Horizon "no fabrica un retorno monetario por hora para actividades cuyo
  efecto no conoce". Una suma de pesos inventaria precisamente ese intercambio.
  Una escalera ordenada solo decide cuando el criterio anterior empato.
- **Un vencimiento externo no se toca.** La seccion 9.10 exige que decir "hoy
  solo tengo 15 minutos" reduzca el trabajo "sin mover vencimientos externos". El
  planificador no escribe fechas comprometidas con terceros; la regla vive aqui,
  no en un comentario de la interfaz.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from horizon import capacity as capacity_module
from horizon.models import (
    ActionStatus,
    AvisoKind,
    BlockKind,
    HorizonPlan,
    ObjectiveStatus,
    PlanBlock,
    PlanCapacity,
    PlanDecision,
    PlanExclusion,
    PlanStatus,
    TriggerKind,
    Uncertainty,
    document_dump,
)

# Cuantas acciones secundarias acompanan a la principal (seccion 9.5: una accion
# principal y hasta dos secundarias).
MAX_SECONDARY_ACTIONS = 2

# Tras tres bloques perdidos por la misma causa no se vuelve a programar igual
# (seccion 9.5). El umbral es una regla inicial ajustable, no un juicio.
LOST_BLOCK_THRESHOLD = 3

# Cuanta incertidumbre se tolera para preferir una accion sobre otra.
_CONFIDENCE_ORDER = {
    Uncertainty.VERIFIED.value: 0,
    Uncertainty.DECLARED.value: 1,
    Uncertainty.ESTIMATED.value: 2,
    Uncertainty.UNKNOWN.value: 3,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _objective_index(objectives: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(o.get("objective_id")): o for o in objectives}


def _active_objective_ids(objectives: list[dict[str, Any]]) -> set[str]:
    return {
        str(o.get("objective_id"))
        for o in objectives
        if str(o.get("status")) == ObjectiveStatus.ACTIVE.value
    }


def _unblocking_counts(actions: list[dict[str, Any]]) -> dict[str, int]:
    """Cuantas acciones esperan a cada una, segun su campo ``dependency``."""
    counts: dict[str, int] = {}
    for action in actions:
        dependency = str(action.get("dependency") or "").strip()
        if not dependency:
            continue
        for candidate in actions:
            candidate_id = str(candidate.get("action_id") or "")
            title = str(candidate.get("title") or "")
            if not candidate_id:
                continue
            if dependency in (candidate_id, title) or (
                title and title.lower() in dependency.lower()
            ):
                counts[candidate_id] = counts.get(candidate_id, 0) + 1
    return counts


def _has_external_deadline(action: dict[str, Any]) -> bool:
    return bool(action.get("external_due_date"))


def _rank_key(
    action: dict[str, Any],
    *,
    objectives: dict[str, dict[str, Any]],
    unblocking: dict[str, int],
    continued_action_id: str | None,
) -> tuple:
    """Escalera de comparacion de la seccion 9.5, en orden de prioridad.

    Cada nivel solo decide cuando el anterior empato. El orden es: continuidad,
    vencimiento externo real, desbloqueo, prioridad del objetivo, menor esfuerzo
    y mayor confianza.
    """
    action_id = str(action.get("action_id") or "")
    objective = objectives.get(str(action.get("objective_id") or "")) or {}
    base = action.get("base") or {}
    uncertainty = str(base.get("uncertainty") or Uncertainty.UNKNOWN.value)
    effort = action.get("effort_estimated_minutes")
    return (
        0 if action_id == continued_action_id else 1,
        0 if _has_external_deadline(action) else 1,
        -unblocking.get(action_id, 0),
        int(objective.get("priority") or 100),
        int(effort) if isinstance(effort, int) else 10_000,
        _CONFIDENCE_ORDER.get(uncertainty, 3),
        str(action.get("created_at") or ""),
    )


def _lost_block_adjustment(action: dict[str, Any]) -> dict[str, Any] | None:
    """Corrige una accion que ya perdio tres bloques por la misma causa.

    La seccion 9.5 pide cambiar la estimacion, el metodo o la secuencia en vez de
    mover el mismo ticket indefinidamente. Devuelve los cambios a aplicar, o
    ``None`` si la accion no esta en ese caso.
    """
    lost = int(action.get("lost_blocks") or 0)
    if lost < LOST_BLOCK_THRESHOLD:
        return None

    actual = action.get("effort_actual_minutes")
    estimated = action.get("effort_estimated_minutes")
    changes: dict[str, Any] = {"needs_decomposition": True}
    # Con datos reales, la estimacion se corrige con lo observado en vez de
    # repetir el numero que ya fallo tres veces.
    if isinstance(actual, int) and actual > 0 and actual != estimated:
        changes["effort_estimated_minutes"] = actual
        changes["estimate_corrected_from"] = estimated
    return changes


def plan_day(
    *,
    user_id: str,
    date_key: str,
    timezone_name: str,
    mandate: Any,
    objectives: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    capacity: PlanCapacity,
    current_plan: dict[str, Any] | None = None,
    trigger_kind: str = TriggerKind.DAILY_PLAN.value,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Construye el plan del dia. No lo publica: eso es del servicio."""
    moment = now or _now()
    objectives_by_id = _objective_index(objectives)
    active_ids = _active_objective_ids(objectives)
    unblocking = _unblocking_counts(actions)

    continued_action_id = None
    if current_plan:
        continued_action_id = current_plan.get("primary_action_id")

    eligible: list[dict[str, Any]] = []
    excluded: list[PlanExclusion] = []
    decisions: list[PlanDecision] = []
    waiting_updates: list[dict[str, Any]] = []
    adjustments: list[dict[str, Any]] = []

    budget = capacity.growth_budget_minutes

    for action in actions:
        action_id = str(action.get("action_id") or "")
        title = str(action.get("title") or "")
        status = str(action.get("status") or ActionStatus.READY.value)
        if status not in (ActionStatus.READY.value, ActionStatus.IN_PROGRESS.value):
            continue

        objective_id = str(action.get("objective_id") or "")
        if objective_id and objective_id not in active_ids:
            # Las ideas aparcadas no se presentan como obligaciones de hoy
            # (seccion 9.5).
            excluded.append(
                PlanExclusion(action_id=action_id, title=title, reason="objetivo_aparcado")
            )
            continue

        requires_auth = bool(action.get("requires_human_authorization"))
        if requires_auth:
            decisions.append(
                PlanDecision(
                    decision_id=f"dec_{action_id}",
                    kind="authorization",
                    recommendation=f"Autorizar «{title}» o dejarla fuera del plan.",
                    consequence="Sin tu autorización no se ejecuta; sigue preparada.",
                    blocked_action_ids=[action_id],
                    requires_authorization=True,
                )
            )
            excluded.append(
                PlanExclusion(action_id=action_id, title=title, reason="requiere_autorizacion")
            )
            continue

        effort = action.get("effort_estimated_minutes")
        if budget is None:
            # Sin capacidad conocida no se descarta la accion: se ofrece a
            # demanda. Lo que no se hace es inventarle una hora.
            eligible.append({**action, "_on_demand": True})
            continue

        if budget <= 0 or not capacity_module.fits_block(effort, budget):
            excluded.append(
                PlanExclusion(
                    action_id=action_id,
                    title=title,
                    reason="no_cabe_en_la_capacidad_de_hoy",
                )
            )
            continue

        adjustment = _lost_block_adjustment(action)
        if adjustment is not None:
            adjustments.append({"action_id": action_id, **adjustment})
            if adjustment.get("needs_decomposition"):
                decisions.append(
                    PlanDecision(
                        decision_id=f"dec_split_{action_id}",
                        kind="decomposition",
                        recommendation=(
                            f"«{title}» perdió {LOST_BLOCK_THRESHOLD} bloques por la misma "
                            "causa. Conviene dividirla en pasos más pequeños."
                        ),
                        consequence="Se corrigió la estimación; el siguiente intento es más corto.",
                        blocked_action_ids=[action_id],
                    )
                )

        eligible.append({**action, "_on_demand": False})

    # Un bloqueo de otra persona se registra como espera con seguimiento, y se
    # elige trabajo independiente (seccion 9.5).
    for action in eligible:
        dependency = str(action.get("dependency") or "").strip()
        if dependency:
            waiting_updates.append({"action_id": str(action.get("action_id") or "")})
            action["_waiting"] = True
    eligible = [a for a in eligible if not a.get("_waiting")]

    eligible.sort(
        key=lambda a: _rank_key(
            a,
            objectives=objectives_by_id,
            unblocking=unblocking,
            continued_action_id=continued_action_id,
        )
    )

    primary = eligible[0] if eligible else None
    secondary = eligible[1 : 1 + MAX_SECONDARY_ACTIONS]

    blocks: list[PlanBlock] = []
    # El ejercicio se reserva antes de repartir tiempo entre proyectos (9.5).
    if capacity.exercise_reserved_minutes > 0:
        blocks.append(
            PlanBlock(
                kind=BlockKind.RESERVED_EXERCISE,
                label="Ejercicio en casa",
                minutes=capacity.exercise_reserved_minutes,
                is_growth=False,
            )
        )

    scheduled = 0
    if budget is not None and budget > 0:
        for action in [primary, *secondary]:
            if action is None:
                continue
            effort = action.get("effort_estimated_minutes")
            if effort is None:
                continue
            blocks.append(
                PlanBlock(
                    kind=BlockKind.SCHEDULED,
                    action_id=str(action.get("action_id") or ""),
                    label=str(action.get("title") or ""),
                    minutes=int(effort),
                    is_growth=True,
                )
            )
            scheduled += int(effort)
    elif primary is not None:
        # A demanda: hay accion recomendada, no hay hora inventada.
        blocks.append(
            PlanBlock(
                kind=BlockKind.ON_DEMAND,
                action_id=str(primary.get("action_id") or ""),
                label=str(primary.get("title") or ""),
                minutes=int(primary.get("effort_estimated_minutes") or 0),
                is_growth=True,
            )
        )

    capacity.scheduled_minutes = scheduled

    if budget is None:
        status = PlanStatus.NEEDS_INPUT
    elif primary is None:
        status = PlanStatus.EMPTY
    else:
        status = PlanStatus.READY

    reason, factors = _explain(primary, objectives_by_id, capacity, status)
    objective = objectives_by_id.get(str((primary or {}).get("objective_id") or "")) or {}

    plan = HorizonPlan(
        user_id=user_id,
        date_key=date_key,
        timezone=timezone_name,
        trigger_kind=trigger_kind,
        status=status,
        reason=reason,
        decision_factors=factors,
        sources=["horizon_objectives", "horizon_actions"],
        capacity=capacity,
        blocks=blocks,
        primary_action_id=str(primary.get("action_id")) if primary else None,
        secondary_action_ids=[str(a.get("action_id")) for a in secondary],
        objective_id=str(objective.get("objective_id")) if objective else None,
        milestone_id=str(primary.get("milestone_id")) if primary else None,
        candidate_count=len(eligible),
        excluded=excluded,
        pending_decisions=decisions,
        generated_at=moment,
        message="",
    )
    plan.message = describe_plan(document_dump(plan))
    payload = document_dump(plan)
    # Los efectos derivados viajan aparte para que quien publica los aplique
    # SOLO si la publicacion gano. Si se aplicaran aqui, el escritor perdedor de
    # una carrera mutaria acciones de un plan que nunca llego a vigente.
    payload["_side_effects"] = {
        "adjustments": adjustments,
        "waiting": waiting_updates,
    }
    return payload


def _explain(
    primary: dict[str, Any] | None,
    objectives: dict[str, dict[str, Any]],
    capacity: PlanCapacity,
    status: PlanStatus,
) -> tuple[str, list[str]]:
    """Motivo en una frase y los factores que lo decidieron (seccion 9.4)."""
    factors: list[str] = []
    if status == PlanStatus.NEEDS_INPUT:
        return (
            "No sabemos cuánto tiempo libre tienes hoy, así que dejo la acción "
            "preparada sin asignarle una hora.",
            ["capacidad_desconocida"],
        )
    if status == PlanStatus.EMPTY or primary is None:
        return (
            "Con la capacidad y las dependencias de hoy no hay una acción de "
            "crecimiento que quepa. Mantengo lo esencial.",
            ["sin_accion_elegible"],
        )

    title = str(primary.get("title") or "")
    objective = objectives.get(str(primary.get("objective_id") or "")) or {}
    objective_title = str(objective.get("title") or "")
    if objective_title:
        factors.append(f"objetivo:{objective_title}")
    if _has_external_deadline(primary):
        factors.append("vencimiento_externo")
    if capacity.growth_budget_minutes is not None:
        factors.append(f"capacidad:{capacity.growth_budget_minutes}")
    reason = f"Elegimos «{title}»"
    if objective_title:
        reason += f" porque alimenta el objetivo de {objective_title.lower()}"
    reason += "."
    return reason, factors


def describe_plan(plan: dict[str, Any]) -> str:
    """Frase lista para voz, en primera persona plural y sin identificadores."""
    status = str(plan.get("status") or "")
    capacity = plan.get("capacity") or {}
    budget = capacity.get("growth_budget_minutes")
    blocks = plan.get("blocks") or []

    if status == PlanStatus.NEEDS_INPUT.value:
        return capacity_module.describe_capacity(PlanCapacity(**capacity))

    growth = [b for b in blocks if b.get("is_growth")]

    if not growth:
        # Un dia lleno tiene dos formas distintas y no se dicen igual: puede no
        # quedar margen (presupuesto cero) o quedar margen pero no alcanzar para
        # ninguna accion lista. Decir "podemos dedicar 10 minutos" y acto seguido
        # "no programamos crecimiento" se contradice y no explica nada.
        if status == PlanStatus.EMPTY.value and budget:
            return (
                f"Hoy caben unos {budget} minutos, pero ninguna acción lista entra "
                "en ese margen: las que tenemos piden más. Mantengo lo esencial y "
                "te dejo una versión más corta preparada."
            )
        return capacity_module.describe_capacity(PlanCapacity(**capacity))

    parts: list[str] = []
    if budget is not None:
        parts.append(capacity_module.describe_capacity(PlanCapacity(**capacity)))

    first = growth[0]
    parts.append(f"Lo principal es {str(first.get('label') or '').lower()}.")
    if len(growth) > 1:
        extras = ", ".join(str(b.get("label") or "").lower() for b in growth[1:])
        parts.append(f"Si sobra margen, también entra {extras}.")
    return " ".join(parts)


def _aviso_kind_for(trigger_kind: str) -> AvisoKind:
    if trigger_kind == TriggerKind.DAILY_PLAN.value:
        return AvisoKind.DAILY_PLAN
    return AvisoKind.NEXT_ACTION
