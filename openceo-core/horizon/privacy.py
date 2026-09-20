"""Privacidad y alcance personal de Horizon.

La especificacion (seccion 9.3) exige que ``project: null`` o un nombre de
proyecto NO basten para aislar la vida personal. Este modulo es el unico lugar
que decide que es alcance personal y como se excluye de los espejos externos.

Hay DOS rutas hacia Notion y ambas deben filtrarse:

1. ``TaskService._task_needs_notion_mirror`` — el guard reactivo que corre al
   crear una tarea.
2. ``MongoRepository.list_tasks_missing_notion_mirror`` — el safety net de
   reconciliacion que corre al arrancar el servicio.

Si solo se filtrara la primera, el safety net publicaria los datos personales
en el primer reinicio. Por eso la exclusion vive aqui y se usa en las dos.
"""

from __future__ import annotations

from typing import Any

# Claves que se escriben dentro de ``task.params``.
PERSONAL_SCOPE_KEY = "horizon_scope"
PERSONAL_OWNER_KEY = "horizon_owner"
PERSONAL_SCOPE_VALUE = "personal"

# Colecciones cuyo contenido es estrictamente del propietario.
PERSONAL_COLLECTIONS = (
    "horizon_profile",
    "horizon_objectives",
    "horizon_captures",
    "horizon_actions",
    "horizon_events",
    "horizon_plans",
    "horizon_triggers",
)


def personal_scope_params(user_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Construye el bloque ``params`` de una tarea de alcance personal."""
    params: dict[str, Any] = {
        PERSONAL_SCOPE_KEY: PERSONAL_SCOPE_VALUE,
        PERSONAL_OWNER_KEY: user_id,
    }
    if extra:
        params.update(extra)
    return params


def _task_params(task: dict[str, Any] | None) -> dict[str, Any]:
    if not task:
        return {}
    params = task.get("params")
    return params if isinstance(params, dict) else {}


def is_personal_scope(task: dict[str, Any] | None) -> bool:
    """True si la tarea pertenece al espacio personal del usuario."""
    params = _task_params(task)
    return str(params.get(PERSONAL_SCOPE_KEY) or "") == PERSONAL_SCOPE_VALUE


def personal_owner(task: dict[str, Any] | None) -> str | None:
    params = _task_params(task)
    owner = params.get(PERSONAL_OWNER_KEY)
    return str(owner) if owner else None


def is_visible_to(task: dict[str, Any] | None, user_id: str) -> bool:
    """Una tarea personal solo la ve su propietario; el resto mantiene su regla."""
    if not is_personal_scope(task):
        return True
    return personal_owner(task) == user_id


def mirror_exclusion_filter() -> dict[str, Any]:
    """Filtro de Mongo que excluye el alcance personal de cualquier espejo.

    Un documento sin ``params.horizon_scope`` satisface ``$ne`` y por tanto
    sigue siendo elegible para el espejo, que es el comportamiento deseado.
    """
    return {f"params.{PERSONAL_SCOPE_KEY}": {"$ne": PERSONAL_SCOPE_VALUE}}


def allows_external_mirror(snapshot: dict[str, Any] | None) -> bool:
    """Decide si el contexto personal admite espejo externo.

    Por defecto el espejo de datos personales esta deshabilitado (seccion 9.9:
    ``personal_external_mirror_default: disabled``).
    """
    if not snapshot:
        return False
    return bool(snapshot.get("personal_external_mirror_enabled"))