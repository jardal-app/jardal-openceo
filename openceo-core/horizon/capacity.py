"""Capacidad diaria de Horizon. Determinista y sin tokens.

La seccion 9.5 fija la regla: programar como maximo el 70% del tiempo personal
libre conocido, **despues** de obligaciones y reservando ejercicio y descanso
antes de repartir tiempo entre proyectos. La seccion 10.2 da la tabla concreta.

Tres reglas que este modulo hace estructurales, no comentarios:

1. **Desconocido no es cero.** Sin horario conocido no se inventa una hora libre
   (9.5): el presupuesto queda en ``None`` y el plan lo declara. Cero es un valor
   medido y significa "dia saturado". Confundirlos convierte "no lo se" en
   "no puedes".
2. **Ejercicio y descanso no se descuentan dos veces** (10.2). Si la
   disponibilidad declarada ya los incluia, no se vuelven a restar; si no se
   sabe, se reservan igualmente, porque el mandato dice
   ``exercise_and_rest_reserved_before_growth``.
3. **Un bloque corto no recibe una tarea larga** (9.5). El ajuste se hace por
   esfuerzo estimado mas la transicion, no por deseo.
"""

from __future__ import annotations

from datetime import date

from horizon.models import DeclaredValue, PlanCapacity, Uncertainty
from horizon.mandate import HorizonMandate

# Agenda inicial de la seccion 10.1, en minutos por dia de la semana
# (lunes=0 ... domingo=6). Suma 115 minutos semanales, que es el numero que la
# especificacion declara. No es un programa deportivo: es la reserva a proteger.
EXERCISE_AGENDA_MINUTES: dict[int, int] = {
    0: 20,  # lunes   - fuerza general
    1: 15,  # martes  - aerobico comodo
    2: 15,  # miercoles - caminata o movilidad
    3: 20,  # jueves  - fuerza general
    4: 15,  # viernes - aerobico comodo
    5: 15,  # sabado  - caminata o movilidad
    6: 15,  # domingo - movimiento suave
}

WEEKLY_EXERCISE_MINUTES = 115

# Coste de preparacion y transicion por bloque (§10.2: "35 de preparacion y
# transiciones"). Se cobra al colocar cada bloque, no al calcular el techo.
TRANSITION_MINUTES_PER_BLOCK = 5

# Paso de redondeo del presupuesto. La seccion 10.2 dice 60 min libres -> "hasta
# 40" y 120 -> "hasta 80", que es 66,7% y no el 70% de 9.5. No hay contradiccion:
# el 70% es el techo y la tabla es la salida redondeada a un multiplo de 5.
# 60*0.70=42 -> 40; 120*0.70=84 -> 80; 15*0.70=10.5 -> 10.
BUDGET_STEP_MINUTES = 5


def floor_to_step(minutes: float, step: int = BUDGET_STEP_MINUTES) -> int:
    """Redondea hacia abajo a un multiplo del paso. Nunca devuelve negativo."""
    if step <= 0:
        return max(0, int(minutes))
    return max(0, int(minutes // step) * step)


def exercise_minutes_for(date_key: str) -> int:
    """Minutos de ejercicio que la agenda reserva para esa fecha."""
    try:
        parsed = date.fromisoformat(date_key)
    except (TypeError, ValueError):
        return 0
    return EXERCISE_AGENDA_MINUTES.get(parsed.weekday(), 0)


def compute_capacity(
    *,
    mandate: HorizonMandate,
    date_key: str,
    declared_free_minutes: int | None,
    exercise_included: bool | None = None,
    source: str | None = None,
) -> PlanCapacity:
    """Calcula el presupuesto de crecimiento del dia.

    ``exercise_included`` describe si el tiempo libre declarado ya contaba el
    bloque de ejercicio. Con ``None`` (no lo dijo) se reserva igualmente y se
    deja constancia en ``assumptions``: la reserva es la opcion conservadora y
    ademas es lo que el mandato ordena.
    """
    reserved_exercise = exercise_minutes_for(date_key)
    fraction = float(mandate.personal_capacity_fraction)
    assumptions: list[str] = []
    capacity = PlanCapacity(
        fraction=fraction,
        transition_minutes_per_block=TRANSITION_MINUTES_PER_BLOCK,
        source=source,
        exercise_included=exercise_included,
    )

    if exercise_included is None:
        assumptions.append("reserve_exercise:availability_did_not_state_it")
        capacity.exercise_reserved_minutes = reserved_exercise
    elif exercise_included:
        # Ya venia dentro de lo declarado: restarlo otra vez seria descontar dos
        # veces el mismo bloque (§10.2).
        capacity.exercise_reserved_minutes = 0
    else:
        capacity.exercise_reserved_minutes = reserved_exercise

    if declared_free_minutes is None:
        # Sin dato no se inventa una hora libre. None viaja hasta el plan.
        capacity.free_minutes = DeclaredValue.unknown(source)
        capacity.billable_minutes = None
        capacity.growth_budget_minutes = None
        capacity.uncertainty = Uncertainty.UNKNOWN
        capacity.assumptions = assumptions
        return capacity

    free = max(0, int(declared_free_minutes))
    billable = max(0, free - capacity.exercise_reserved_minutes)
    capacity.free_minutes = DeclaredValue(
        value=free,
        uncertainty=Uncertainty.DECLARED,
        source=source,
    )
    capacity.billable_minutes = billable
    capacity.growth_budget_minutes = floor_to_step(billable * fraction)
    capacity.uncertainty = Uncertainty.DECLARED
    capacity.assumptions = assumptions
    return capacity


def fits_block(effort_minutes: int | None, available_minutes: int) -> bool:
    """True si la accion cabe en el bloque, contando su transicion.

    Un esfuerzo desconocido no cabe en un bloque corto: no se puede prometer que
    algo quepa cuando no se sabe cuanto ocupa.
    """
    if available_minutes <= 0:
        return False
    if effort_minutes is None:
        return False
    return int(effort_minutes) + TRANSITION_MINUTES_PER_BLOCK <= available_minutes


def describe_capacity(capacity: PlanCapacity) -> str:
    """Frase corta para voz, sin identificadores ni tablas."""
    if capacity.growth_budget_minutes is None:
        return (
            "Todavía no sabemos cuánto tiempo libre tienes hoy, así que no te "
            "invento una hora. Puedo dejarte el material preparado y lo colocamos "
            "cuando me digas cuánto tienes."
        )
    budget = capacity.growth_budget_minutes
    if budget <= 0:
        return (
            "Con la capacidad de hoy no programamos crecimiento: el día ya está "
            "lleno. Mantengo el ejercicio y lo esencial."
        )
    return (
        f"Hoy podemos dedicar unos {budget} minutos a crecimiento, "
        "ya descontados el ejercicio y el resto."
    )
