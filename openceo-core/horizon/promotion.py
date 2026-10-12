"""Promocion automatica Horizon -> empresa (F5, TASK-2026-00121).

Determinista y sin tokens: si una accion toca un proyecto o repo de la empresa,
es trabajo de empresa y se promueve por la API comun (ERPNext primero). Solo
viaja la especificacion del trabajo; lo personal (mision, finanzas, familia,
salud) nunca sale.

Reglas de frontera (ver workfile del ticket):

- La senal de empresa vive en la ACCION (`company_project` / `company_repo`
  declarados). El `project` de la delegacion HZ-003 es substrato de ejecucion,
  no propiedad de negocio: una accion personal delegada sobre un proyecto de
  empresa sigue personal.
- Sin toque declarado -> personal (fail-closed hacia privacidad). Sin
  adivinanza por texto libre: una mencion casual no puede filtrar una nota
  personal a un ticket de empresa.
- Toque que no resuelve contra la configuracion -> rechazo (`refused`): no se
  crea nada, ni siquiera personal. Degradarlo a personal ocultaria trabajo de
  empresa del negocio y recrearia la contradiccion con AGENTS.md.
- Un ticket promovido es trabajo de empresa ordinario: ERPNext primero y
  espejos normales. Por eso NO lleva `horizon_scope=personal` (ese flag lo
  excluiria de ERPNext y volveria a ser solo-Mongo).
"""

from __future__ import annotations

from typing import Any

# Alcances de la clasificacion.
SCOPE_PERSONAL = "personal"
SCOPE_COMPANY = "company"
SCOPE_REFUSED = "refused"

# Motivos de clasificacion.
REASON_OK = "ok"
REASON_NO_TOUCH = "no_company_touch"
REASON_UNKNOWN_PROJECT = "unknown_project"
REASON_UNRESOLVABLE_REPO = "unresolvable_repo"
REASON_PROJECT_REPO_MISMATCH = "project_repo_mismatch"
REASON_NO_COMPANY_CONFIG = "no_company_config"

REFUSAL_MESSAGES = {
    REASON_UNKNOWN_PROJECT: (
        "La acción declara un proyecto de empresa desconocido; no se creó "
        "nada. Corrige el proyecto o quita la declaración."
    ),
    REASON_UNRESOLVABLE_REPO: (
        "La acción declara un repo que no pertenece a ningún proyecto de "
        "empresa conocido; no se creó nada."
    ),
    REASON_PROJECT_REPO_MISMATCH: (
        "La acción declara un proyecto y un repo que no pertenecen al mismo "
        "proyecto de empresa; no se creó nada."
    ),
    REASON_NO_COMPANY_CONFIG: (
        "La acción declara trabajo de empresa pero la configuración de "
        "empresa no está disponible; no se creó nada."
    ),
}

# Proveniencia en `params` del ticket promovido. `horizon_owner` es enrutado
# (a que accion vuelve el resultado), no dato personal.
PROMOTED_BY_KEY = "promoted_by"
PROMOTED_BY_VALUE = "horizon"
PROMOTION_ACTION_KEY = "horizon_action_id"
PROMOTION_OWNER_KEY = "horizon_owner"


def promotion_source_id(action_id: str) -> str:
    """Idempotencia de la API para el ticket promovido."""
    return f"horizon-promotion:{action_id}"


def promotion_objective_source_id(action_id: str) -> str:
    """Idempotencia de la API para el objetivo promovido."""
    return f"horizon-promotion-obj:{action_id}"


def promotion_escalation_source_id(action_id: str) -> str:
    """Idempotencia de la API para el escalado de una promocion."""
    return f"horizon-promotion-esc:{action_id}"


def promotion_params(
    user_id: str,
    action_id: str,
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """Bloque `params` de un ticket promovido. Sin alcance personal."""
    return {
        PROMOTED_BY_KEY: PROMOTED_BY_VALUE,
        PROMOTION_ACTION_KEY: action_id,
        PROMOTION_OWNER_KEY: user_id,
        "dedupe_key": dedupe_key or f"hprom:{action_id}",
        "delegated_by": PROMOTED_BY_VALUE,
    }


def is_horizon_promoted(task: dict[str, Any] | None) -> bool:
    """True si el ticket es trabajo promovido desde Horizon.

    Un promovido NUNCA es alcance personal (ver docstring del modulo): son
    predicados disjuntos por construccion.
    """
    if not isinstance(task, dict):
        return False
    params = task.get("params")
    if not isinstance(params, dict):
        return False
    if str(params.get(PROMOTED_BY_KEY) or "") != PROMOTED_BY_VALUE:
        return False
    return bool(str(params.get(PROMOTION_ACTION_KEY) or "").strip())


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _project_repos(company_config: Any, project: str) -> list[dict[str, Any]]:
    try:
        loaded = company_config.load_project(project)
    except Exception:
        return []
    if not isinstance(loaded, dict):
        return []
    info = loaded.get("project")
    if not isinstance(info, dict):
        return []
    repos = info.get("repos")
    return [dict(entry) for entry in repos if isinstance(entry, dict)]


def _normalize_path(value: str) -> str:
    return value.strip().rstrip("/")


def resolve_repo_project(company_config: Any, repo: str) -> str | None:
    """Proyecto de empresa dueno de `repo`, o None si no resuelve.

    Coincidencia exacta contra `name` o `local_path` de cada repo
    configurado. Recorre los proyectos ordenados para que el resultado sea
    determinista si dos proyectos listaran el mismo repo.
    """
    want = _normalize_path(repo)
    if not want:
        return None
    try:
        projects = sorted(company_config.available_projects())
    except Exception:
        return None
    for project in projects:
        for entry in _project_repos(company_config, project):
            name = str(entry.get("name") or "").strip()
            local_path = _normalize_path(str(entry.get("local_path") or ""))
            if want == name or (local_path and want == local_path):
                return project
    return None


def classify_action(
    action: dict[str, Any] | None,
    *,
    explicit_project: str | None = None,
    explicit_repo: str | None = None,
    company_config: Any | None = None,
) -> dict[str, Any]:
    """Clasifica una accion en personal, empresa o rechazo.

    `explicit_*`: None conserva lo almacenado en la accion; "" lo borra; un
    valor lo declara. El llamador persiste lo validado; aqui no se escribe.
    """
    stored = action if isinstance(action, dict) else {}
    if explicit_project is not None:
        project = _clean(explicit_project)
    else:
        project = _clean(stored.get("company_project"))
    if explicit_repo is not None:
        repo = _clean(explicit_repo)
    else:
        repo = _clean(stored.get("company_repo"))

    if not project and not repo:
        return {
            "scope": SCOPE_PERSONAL,
            "project": None,
            "repo": None,
            "reason": REASON_NO_TOUCH,
        }
    if company_config is None:
        return {
            "scope": SCOPE_REFUSED,
            "project": project,
            "repo": repo,
            "reason": REASON_NO_COMPANY_CONFIG,
        }
    if project:
        try:
            known = company_config.has_project(project)
        except Exception:
            known = False
        if not known:
            return {
                "scope": SCOPE_REFUSED,
                "project": project,
                "repo": repo,
                "reason": REASON_UNKNOWN_PROJECT,
            }
    if repo:
        resolved = resolve_repo_project(company_config, repo)
        if resolved is None:
            return {
                "scope": SCOPE_REFUSED,
                "project": project,
                "repo": repo,
                "reason": REASON_UNRESOLVABLE_REPO,
            }
        if project and resolved != project:
            return {
                "scope": SCOPE_REFUSED,
                "project": project,
                "repo": repo,
                "reason": REASON_PROJECT_REPO_MISMATCH,
            }
        project = project or resolved
    return {
        "scope": SCOPE_COMPANY,
        "project": project,
        "repo": repo,
        "reason": REASON_OK,
    }


def build_work_spec(
    action: dict[str, Any] | None,
    instruction: str | None = None,
) -> dict[str, str]:
    """Especificacion del trabajo que viaja a empresa. Lista cerrada.

    Solo titulo, aceptacion, siguiente paso, esfuerzo estimado e instruccion
    explicita. Nunca contexto ambiental personal: ni titulo del objetivo, ni
    mision, ni perfil, ni finanzas, ni familia, ni salud. Aunque la accion
    trajera esas claves, no se leen.
    """
    fields = action if isinstance(action, dict) else {}
    action_id = str(fields.get("action_id") or "").strip()

    def _flat(value: Any) -> str:
        return " ".join(str(value or "").split())

    title = _flat(fields.get("title"))[:120] or "Trabajo promovido desde Horizon"
    lines = [title]
    acceptance = _flat(fields.get("acceptance"))
    if acceptance:
        lines += ["", f"Aceptación: {acceptance}"]
    next_step = _flat(fields.get("next_step"))
    if next_step:
        lines += ["", f"Siguiente paso: {next_step}"]
    effort = fields.get("effort_estimated_minutes")
    if isinstance(effort, bool):
        effort = None
    if isinstance(effort, int) and effort > 0:
        lines += ["", f"Esfuerzo estimado: {effort} min"]
    clean_instruction = _flat(instruction)
    if clean_instruction:
        lines += ["", clean_instruction]
    lines += ["", f"Promovido desde Horizon (acción {action_id or 'desconocida'})."]
    return {"title": title, "description": "\n".join(lines).strip()}


# Resultado de revisar un escalado pendiente al reintentar la promocion.
ESCALATION_APPROVED = "approved"
ESCALATION_WAITING = "waiting"
ESCALATION_DENIED = "denied"
ESCALATION_CLOSED = "closed"
ESCALATION_MISSING = "missing"


def escalation_outcome(task: dict[str, Any] | None) -> str:
    """Estado de un escalado pendiente, para decidir el reintento.

    Aprobar y denegar dejan `completed`: se distinguen por la decision
    grabada en `params.escalation.decision`. Un `completed` sin decision
    grabada (cierre manual en el desk) cuenta como denegado: promover
    trabajo con costo sobre una aprobacion ambigua seria fail-open.
    """
    if task is None:
        return ESCALATION_MISSING
    status = str(task.get("status") or "")
    if status in ("waiting", "queued", "in_progress"):
        return ESCALATION_WAITING
    if status == "completed":
        params = task.get("params")
        escalation = params.get("escalation") if isinstance(params, dict) else None
        decision = escalation.get("decision") if isinstance(escalation, dict) else None
        verdict = decision.get("decision") if isinstance(decision, dict) else None
        return ESCALATION_APPROVED if verdict == "approved" else ESCALATION_DENIED
    return ESCALATION_CLOSED
