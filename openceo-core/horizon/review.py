"""Revision periodica de Horizon (seccion 9.7). Determinista y sin LLM.

La revision no es un formulario que el usuario rellene: es un analisis que el
sistema produce con lo que ya observo. Separa dos cosas que la seccion 9.4
distingue con nitidez:

- **Ajustes delegados**: corregir una estimacion, cambiar la secuencia, marcar
  una accion para dividir. Horizon los aplica solo.
- **Decisiones reservadas**: cambiar la mision, comprometer dinero, obligar a un
  tercero, mover un vencimiento externo. Horizon prepara una recomendacion lista
  y no decide.

La revision mensual es la misma funcion con otra ventana: no hay un segundo
camino de codigo que se pueda desincronizar del primero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from horizon.models import ActionStatus, ObjectiveStatus, TriggerKind

WEEKLY_WINDOW_DAYS = 7
MONTHLY_WINDOW_DAYS = 28

# Minutos humanos de gestion al dia que la seccion 11 marca como objetivo
# ("unos 2 min/dia mas excepciones semanales"). Si la revision encuentra mas,
# el propio asistente es lo que hay que corregir.
DAILY_MANAGEMENT_MINUTES_TARGET = 2

# Una accion en espera sin movimiento mas alla de esto merece seguimiento.
WAITING_DAYS_BEFORE_FOLLOWUP = 7

# La seccion 9.5 pide revisar capacidad o descomponer tras tres bloques perdidos.
LOST_BLOCK_THRESHOLD = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _window_days(kind: str) -> int:
    return MONTHLY_WINDOW_DAYS if kind == TriggerKind.MONTHLY_REVIEW.value else WEEKLY_WINDOW_DAYS


def _effort_deviations(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Estimado contra real. Solo con datos observados, nunca inventados."""
    deviations: list[dict[str, Any]] = []
    for action in actions:
        estimated = action.get("effort_estimated_minutes")
        actual = action.get("effort_actual_minutes")
        if not isinstance(estimated, int) or not isinstance(actual, int):
            continue
        if estimated <= 0:
            continue
        ratio = actual / estimated
        if ratio >= 1.5 or ratio <= 0.5:
            deviations.append(
                {
                    "action_id": action.get("action_id"),
                    "title": action.get("title"),
                    "estimated_minutes": estimated,
                    "actual_minutes": actual,
                    "direction": "subestima" if ratio >= 1.5 else "sobrestima",
                }
            )
    return deviations


def _lost_block_findings(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for action in actions:
        lost = int(action.get("lost_blocks") or 0)
        if lost < LOST_BLOCK_THRESHOLD:
            continue
        findings.append(
            {
                "action_id": action.get("action_id"),
                "title": action.get("title"),
                "lost_blocks": lost,
                "lost_reason": action.get("lost_reason"),
                "rule": "tres bloques perdidos por la misma causa: revisar capacidad o descomponer",
            }
        )
    return findings


def _corrections(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ajustes delegados que la revision puede aplicar por si misma (9.4)."""
    corrections: list[dict[str, Any]] = []
    observed = [
        int(a["effort_actual_minutes"])
        for a in actions
        if isinstance(a.get("effort_actual_minutes"), int) and int(a["effort_actual_minutes"]) > 0
    ]
    # La mediana de lo observado es una correccion honesta: no la inventa el
    # modelo, sale de los datos. Con menos de tres muestras no se corrige nada.
    typical = int(median(observed)) if len(observed) >= 3 else None

    for action in actions:
        action_id = str(action.get("action_id") or "")
        if not action_id:
            continue
        lost = int(action.get("lost_blocks") or 0)
        estimated = action.get("effort_estimated_minutes")
        if lost >= LOST_BLOCK_THRESHOLD:
            updates: dict[str, Any] = {"needs_decomposition": True}
            if typical is not None and typical != estimated:
                updates["effort_estimated_minutes"] = typical
                updates["estimate_corrected_from"] = estimated
            corrections.append({"action_id": action_id, **updates})
        elif typical is not None and isinstance(estimated, int) and estimated > typical * 2:
            corrections.append(
                {
                    "action_id": action_id,
                    "effort_estimated_minutes": typical,
                    "estimate_corrected_from": estimated,
                }
            )
    return corrections


def _waiting_findings(actions: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for action in actions:
        if str(action.get("status")) != ActionStatus.WAITING.value:
            continue
        since = _aware(action.get("updated_at")) or _aware(action.get("created_at"))
        if since is None:
            continue
        if (now - since) >= timedelta(days=WAITING_DAYS_BEFORE_FOLLOWUP):
            findings.append(
                {
                    "action_id": action.get("action_id"),
                    "title": action.get("title"),
                    "days_waiting": (now - since).days,
                    "rule": "registrar espera con proximo seguimiento y elegir trabajo independiente",
                }
            )
    return findings


def _stalled_objectives(
    objectives: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Objetivos activos sin ninguna accion terminada ni en curso."""
    moved = {
        str(a.get("objective_id"))
        for a in actions
        if str(a.get("status")) in (ActionStatus.DONE.value, ActionStatus.IN_PROGRESS.value)
    }
    stalled: list[dict[str, Any]] = []
    for objective in objectives:
        if str(objective.get("status")) != ObjectiveStatus.ACTIVE.value:
            continue
        objective_id = str(objective.get("objective_id") or "")
        if objective_id and objective_id not in moved:
            stalled.append(
                {
                    "objective_id": objective_id,
                    "title": objective.get("title"),
                    "rule": "sin movimiento en la ventana: revisar si falta un dato o un paso mas pequeno",
                }
            )
    return stalled


def _capacity_findings(plans: list[dict[str, Any]]) -> dict[str, Any]:
    """Cuantos planes se prepararon sin saber la capacidad real."""
    unknown = 0
    scheduled = 0
    for plan in plans:
        capacity = plan.get("capacity") or {}
        if capacity.get("growth_budget_minutes") is None:
            unknown += 1
        else:
            scheduled += int(capacity.get("scheduled_minutes") or 0)
    return {
        "plans_reviewed": len(plans),
        "plans_without_known_capacity": unknown,
        "scheduled_minutes_total": scheduled,
        "note": (
            "Sin horas libres declaradas no se programa crecimiento; el plan queda a demanda."
            if unknown
            else "La capacidad declarada alcanzo para programar."
        ),
    }


def _pending_decisions(
    *,
    objectives: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    corrections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Lo que la seccion 9.4 reserva al usuario, con recomendacion lista."""
    decisions: list[dict[str, Any]] = []

    for objective in objectives:
        if objective.get("requires_decision"):
            decisions.append(
                {
                    "kind": "mision_o_compromiso",
                    "recommendation": f"Revisar «{objective.get('title')}» con tu decisión explícita.",
                    "consequence": "Cambia el rumbo, así que no lo decidimos por ti.",
                }
            )

    for action in actions:
        if action.get("requires_human_authorization"):
            decisions.append(
                {
                    "kind": "autorizacion",
                    "recommendation": f"Autorizar «{action.get('title')}» o dejarla fuera.",
                    "consequence": "Queda preparada, sin ejecutarse.",
                }
            )
        # Un vencimiento externo no se mueve desde el planificador (9.10).
        if action.get("external_due_date") and action.get("due_date_changed"):
            decisions.append(
                {
                    "kind": "vencimiento_externo",
                    "recommendation": (
                        f"«{action.get('title')}» tiene un vencimiento comprometido con un tercero."
                    ),
                    "consequence": "Solo tú puedes renegociarlo; el plan no lo mueve.",
                }
            )

    for correction in corrections:
        if correction.get("needs_decomposition"):
            decisions.append(
                {
                    "kind": "descomposicion",
                    "recommendation": (
                        f"«{correction.get('title') or correction.get('action_id')}» "
                        "perdió varios bloques por la misma causa."
                    ),
                    "consequence": "Se corrigió la estimación; conviene dividirla.",
                }
            )

    return decisions


def run_review(
    *,
    kind: str,
    profile: dict[str, Any],
    mandate: Any,
    objectives: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Analiza la ventana y devuelve desviaciones, correcciones y decisiones."""
    moment = now or _now()
    window = _window_days(kind)

    deviations = _effort_deviations(actions)
    lost = _lost_block_findings(actions)
    waiting = _waiting_findings(actions, now=moment)
    stalled = _stalled_objectives(objectives, actions)
    capacity = _capacity_findings(plans)
    corrections = _corrections(actions)
    for correction in corrections:
        title = next(
            (a.get("title") for a in actions if a.get("action_id") == correction.get("action_id")),
            None,
        )
        if title:
            correction["title"] = title

    decisions = _pending_decisions(
        objectives=objectives, actions=actions, corrections=corrections
    )

    notes: list[str] = []
    if capacity["plans_without_known_capacity"]:
        notes.append(
            "Hay planes preparados sin horas libres declaradas; por eso no programan crecimiento."
        )
    if stalled:
        notes.append(f"{len(stalled)} objetivos activos sin movimiento en la ventana.")

    return {
        "kind": kind,
        "window_days": window,
        "reviewed_at": moment,
        "counts": {
            "objectives": len(objectives),
            "actions": len(actions),
            "plans": len(plans),
        },
        "deviations": deviations,
        "lost_blocks": lost,
        "waiting": waiting,
        "stalled_objectives": stalled,
        "capacity": capacity,
        "corrections": corrections,
        "pending_decisions": decisions,
        "notes": notes,
        "message": describe_review(deviations, lost, stalled, decisions),
    }


def describe_review(
    deviations: list[dict[str, Any]],
    lost: list[dict[str, Any]],
    stalled: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> str:
    """Frase para voz: lo que cambio y lo que espera una decision tuya."""
    parts: list[str] = []
    if deviations:
        parts.append(
            f"En {len(deviations)} acciones la estimación no se cumplió; "
            "corregimos el tiempo previsto."
        )
    if lost:
        parts.append(
            f"{len(lost)} acciones perdieron varios bloques por la misma causa, "
            "así que cambiamos el método antes de repetirlo igual."
        )
    if stalled:
        parts.append(f"{len(stalled)} objetivos no se movieron en la ventana.")
    if decisions:
        parts.append(
            f"Quedan {len(decisions)} decisiones que te corresponden; "
            "te dejo la recomendación preparada."
        )
    if not parts:
        return "La semana se sostuvo: no hubo desviaciones que exijan un cambio de método."
    return " ".join(parts)
