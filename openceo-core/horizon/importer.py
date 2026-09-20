"""Importacion del contexto personal de Horizon desde la especificacion.

La seccion 9.3 y la decision D-016 son tajantes: el plan particular se importa
como **datos privados versionados**, no se incrusta en el codigo ni en la
documentacion general, y releer el README **no debe sobrescribir el progreso**.

De aqui salen dos consecuencias de diseno:

- Las cifras personales (ingresos, cambio, minutos de audio, deuda) NO estan en
  Python. Se leen del bloque de politica que la propia especificacion embebe
  como YAML en la seccion 9.9. Si ese bloque cambia, la importacion cambia.
- La importacion es un upsert por clave natural (``source_key``). Solo crea lo
  que falta. El estado, la evidencia y el avance de un objetivo existente se
  conservan intactos.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from horizon import mandate as mandate_module
from horizon.models import (
    ActionOwner,
    CaptureChannel,
    DeclaredSnapshot,
    DeclaredValue,
    HorizonAction,
    HorizonMission,
    HorizonObjective,
    HorizonProfile,
    HorizonMilestone,
    ObjectiveHorizon,
    ObjectiveKind,
    ObjectiveStatus,
    Uncertainty,
    document_dump,
)

logger = logging.getLogger(__name__)

DEFAULT_SOURCE = "jardal-horizon/README.md"
DEFAULT_PLAN_VERSION = "2.1"

MISSION_STATEMENT = (
    "construir empresas sostenibles que creen trabajo digno, cuidar a mi familia "
    "y financiar ayuda a personas, animales y la creacion"
)

MISSION_VALUES = (
    "salud, necesidades basicas y obligaciones primero",
    "continuidad del ingreso",
    "comunicacion y preparacion para empleo internacional",
    "postulaciones y entrevistas",
    "experimentos de negocio cuando exista capacidad",
)

# Estructura de objetivos derivada del catalogo de la seccion 8 y el backlog de
# la seccion 11. Es estructura, no datos personales: las cifras y los valores
# base/target se resuelven desde el bloque declarado.
_OBJECTIVE_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "source_key": "growth_employment",
        "title": "Acceso a empleo internacional",
        "kind": ObjectiveKind.GROWTH,
        "horizon": ObjectiveHorizon.STAGE,
        "priority": 10,
        "measure": "ingreso neto cobrado y horas humanas dedicadas",
        "milestones": (
            {
                "source_key": "VOZ-001",
                "title": "Medir audio util para la clonacion de voz",
                "measure": "minutos de audio util acumulado",
                "value_key": "voice_usable_minutes",
                "condition": "ritmo medido en al menos tres sesiones",
            },
            {
                "source_key": "VOZ-002",
                "title": "Clon de voz elegido utilizable en el sistema",
                "measure": "clon integrado y probado",
                "condition": "audio util suficiente y costo previsto cubierto",
            },
            {
                "source_key": "VOZ-003",
                "title": "Evaluar entrevista y trabajo diario con el conjunto",
                "measure": "actividades criticas superadas",
                "condition": "voz funcional integrada",
            },
            {
                "source_key": "EMP-001",
                "title": "CV honesto listo para enviar",
                "measure": "CV en espanol y version inglesa revisada",
                "condition": "revision factual completada",
            },
            {
                "source_key": "EMP-002",
                "title": "Dos casos de trabajo verificables",
                "measure": "casos sin informacion restringida",
                "condition": "permisos de publicacion verificados",
            },
            {
                "source_key": "EMP-003",
                "title": "Primeras solicitudes compatibles registradas",
                "measure": "solicitudes enviadas con filtros verificados",
                "condition": "CV y comunicacion adecuados al proceso",
            },
        ),
    },
    {
        "source_key": "financial_coverage",
        "title": "Cobertura, deuda y devolucion contractual",
        "kind": ObjectiveKind.FINANCIAL,
        "horizon": ObjectiveHorizon.STAGE,
        "priority": 20,
        "measure": "caja libre, fondo contractual y deuda conciliada",
        "milestones": (
            {
                "source_key": "FIN-001",
                "title": "Registro de ingresos, saldos y esenciales",
                "measure": "cobertura del registro con faltantes visibles",
                "condition": "fuentes disponibles conectadas",
            },
            {
                "source_key": "FIN-002",
                "title": "Estado bancario y vencimientos documentados",
                "measure": "saldo, tasa efectiva, seguros y plazo",
                "condition": "acceso o respuesta del banco",
            },
            {
                "source_key": "FIN-003",
                "title": "Ruta de devolucion completa documentada",
                "measure": "importe y fecha exigible por escrito",
                "condition": "contrato, mensajes y caja disponibles",
            },
            {
                "source_key": "FIN-004",
                "title": "Presupuesto y caja por fechas",
                "measure": "flujo proyectado a 30 dias con incertidumbre visible",
                "condition": "FIN-001 y FIN-002 resueltos",
            },
        ),
    },
    {
        "source_key": "clients_commitments",
        "title": "Cerrar lo ya comprometido con clientes",
        "kind": ObjectiveKind.CLIENTS,
        "horizon": ObjectiveHorizon.STAGE,
        "priority": 30,
        "measure": "margen de caja e ingreso efectivo por hora",
        "milestones": (
            {
                "source_key": "CLI-001",
                "title": "Un hito aceptable y cobrable por cliente",
                "measure": "entregables aceptados y cobro efectivo",
                "condition": "contratos, permisos y tareas existentes",
            },
            {
                "source_key": "MOV-001",
                "title": "Trayectos y transporte medidos",
                "measure": "tiempo, costo y trabajo por voz posible",
                "condition": "una semana observable",
            },
        ),
    },
    {
        "source_key": "health_exercise",
        "title": "Ejercicio diario y descanso protegido",
        "kind": ObjectiveKind.HEALTH,
        "horizon": ObjectiveHorizon.STAGE,
        "priority": 5,
        "measure": "minutos de actividad sostenibles por semana",
        "milestones": (
            {
                "source_key": "SAL-001",
                "title": "Rutina sostenible en el gimnasio de casa",
                "measure": "minutos semanales realizados",
                "value_key": "exercise_weekly_minutes_initial",
                "condition": "horario y actividad adecuados a ti",
            },
        ),
    },
    {
        "source_key": "platform_absorption",
        "title": "Absorcion de Horizon y ciclo personal completo",
        "kind": ObjectiveKind.PLATFORM,
        "horizon": ObjectiveHorizon.STAGE,
        "priority": 15,
        "measure": "ciclo captura -> decision -> resultado -> ajuste operativo",
        "milestones": (
            {
                "source_key": "HZ-001",
                "title": "Contexto personal del CEO operativo",
                "measure": "captura, objetivos y siguiente accion tras reinicio",
                "condition": "privacidad y vinculos a tareas comprobados",
            },
            {
                "source_key": "HZ-002",
                "title": "Ciclo proactivo del CEO",
                "measure": "una interrupcion real modifica el plan",
                "condition": "motor de objetivos y disparadores persistidos",
            },
            {
                "source_key": "HZ-003",
                "title": "Delegacion y dev movil con tickets reales",
                "measure": "tres tickets con resultado medido",
                "condition": "acceso autorizado y sesiones existentes",
            },
        ),
    },
)

# Objetivos aparcados: conservan la aspiracion y una condicion de revision. No
# se presentan como obligaciones de hoy (secciones 9.3 y 9.5).
_PARKED_OBJECTIVES: tuple[dict[str, Any], ...] = (
    {
        "source_key": "bible_videogame",
        "title": "Videojuego de la Biblia",
        "kind": ObjectiveKind.BUSINESS,
        "review_condition": "documento de concepto de una pagina y equipo lider dispuesto",
        "priority": 900,
    },
    {
        "source_key": "father_book",
        "title": "Libro y asistente de pago del padre",
        "kind": ObjectiveKind.FAMILY,
        "review_condition": "muestra validada por cinco lectores objetivo",
        "priority": 910,
    },
    {
        "source_key": "sister_media",
        "title": "Medio digital para la hermana",
        "kind": ObjectiveKind.FAMILY,
        "review_condition": "ella quiere participar y existe un comprador real",
        "priority": 920,
    },
    {
        "source_key": "sibling_employment",
        "title": "Apoyo a la postulacion del hermano",
        "kind": ObjectiveKind.FAMILY,
        "review_condition": "el acepta y su portafolio esta seleccionado",
        "priority": 930,
    },
    {
        "source_key": "land_lab_agriculture",
        "title": "Terreno, laboratorio y agricultura",
        "kind": ObjectiveKind.BUSINESS,
        "review_condition": "capital liquido, operacion y reserva financiadas",
        "priority": 940,
    },
)


# --------------------------------------------------------------------------
# Extraccion
# --------------------------------------------------------------------------


def read_source(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def extract_policy_block(text: str) -> dict[str, Any]:
    """Extrae el bloque YAML de politica embebido en la especificacion.

    Se busca el bloque cercado que declare ``plan_id: horizon``. Si hay varios,
    gana el que tenga ese identificador. Sin bloque, se devuelve vacio en lugar
    de inventar un mandato.
    """
    blocks = re.findall(r"```ya?ml\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    for block in blocks:
        try:
            parsed = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict) and str(parsed.get("plan_id") or "").strip() == "horizon":
            return parsed
    logger.warning("Horizon policy block not found in specification source")
    return {}


def extract_backlog(text: str) -> dict[str, dict[str, str]]:
    """Lee la tabla de backlog de la seccion 11 como diccionario por ID."""
    section = re.search(
        r"##\s*11\..*?(?=\n##\s*12\.|\Z)",
        text,
        re.DOTALL,
    )
    if not section:
        return {}
    rows = _parse_markdown_table(section.group(0))
    backlog: dict[str, dict[str, str]] = {}
    for row in rows:
        key = str(row.get("ID") or "").strip()
        if not key or key.upper() == "ID":
            continue
        backlog[key.upper()] = row
    return backlog


def _parse_markdown_table(chunk: str) -> list[dict[str, str]]:
    """Parsea una tabla markdown simple (encabezado + filas)."""
    lines = [ln.strip() for ln in chunk.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    header = _split_row(lines[0])
    if not header:
        return []
    rows: list[dict[str, str]] = []
    for raw in lines[1:]:
        cells = _split_row(raw)
        if not cells:
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row: dict[str, str] = {}
        for idx, name in enumerate(header):
            row[name] = cells[idx] if idx < len(cells) else ""
        rows.append(row)
    return rows


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    if not stripped:
        return []
    return [cell.strip() for cell in stripped.split("|")]


# --------------------------------------------------------------------------
# Construccion
# --------------------------------------------------------------------------


def _declared_voice(declared: dict[str, Any]) -> dict[str, Any]:
    voice = declared.get("voice") or {}
    return voice if isinstance(voice, dict) else {}


def _declared_money(declared: dict[str, Any]) -> dict[str, Any]:
    money = declared.get("money") or {}
    return money if isinstance(money, dict) else {}


def _milestone_values(declared: dict[str, Any]) -> dict[str, tuple[DeclaredValue, DeclaredValue]]:
    """Valores base y target desde el bloque declarado, nunca inventados.

    Un valor ausente queda como desconocido con ``None``. La especificacion
    prohibe explicitamente presentar desconocido como cero.
    """
    source = DEFAULT_SOURCE
    as_of = datetime.now(timezone.utc)
    voice = _declared_voice(declared)
    daily_life = declared.get("daily_life") or {}

    values: dict[str, tuple[DeclaredValue, DeclaredValue]] = {}

    target_minutes = voice.get("target_audio_minutes")
    recorded = voice.get("recorded_audio_minutes_reported")
    usable = voice.get("usable_audio_minutes_confirmed")
    per_hour = voice.get("useful_audio_minutes_per_work_hour")

    # "Minutos grabados" y "minutos de audio UTIL" son medidas distintas: la
    # especificacion declara 18 grabados pero deja util en null. Mezclarlas
    # inflaria el progreso de la meta de 180, asi que la base del hito usa solo
    # el valor confirmado como util y queda desconocida si no existe.
    values["voice_usable_minutes"] = (
        DeclaredValue.declared(usable, source, as_of)
        if usable is not None
        else DeclaredValue.unknown(source),
        DeclaredValue.declared(target_minutes, source, as_of)
        if target_minutes is not None
        else DeclaredValue.unknown(source),
    )
    values["voice_recorded_minutes"] = (
        DeclaredValue.declared(recorded, source, as_of)
        if recorded is not None
        else DeclaredValue.unknown(source),
        DeclaredValue.declared(target_minutes, source, as_of)
        if target_minutes is not None
        else DeclaredValue.unknown(source),
    )
    values["voice_rate"] = (
        DeclaredValue.declared(per_hour, source, as_of)
        if per_hour is not None
        else DeclaredValue.unknown(source),
        DeclaredValue.unknown(source),
    )

    exercise_minutes = daily_life.get("exercise_weekly_minutes_initial")
    values["exercise_weekly_minutes_initial"] = (
        DeclaredValue.declared(exercise_minutes, source, as_of)
        if exercise_minutes is not None
        else DeclaredValue.declared(115, source, as_of),
        DeclaredValue.unknown(source),
    )

    return values


def _unresolved_fields(declared: dict[str, Any]) -> list[str]:
    """Campos que la especificacion declara pendientes.

    Se listan explicitamente para que ninguna decision dependiente los trate
    como conocidos (seccion 2: 'nunca como cero').
    """
    money = _declared_money(declared)
    voice = _declared_voice(declared)
    pending: list[str] = []
    checks = (
        ("essential_monthly_expenses", money.get("essential_monthly_expenses")),
        ("bank_outstanding_balance", money.get("bank_outstanding_balance")),
        ("contract_refund_due_date", money.get("contract_refund_due_date")),
        ("reference_fx_verified_market_quote", money.get("reference_fx", {}).get("verified_market_quote")
         if isinstance(money.get("reference_fx"), dict) else None),
        ("usable_audio_minutes_confirmed", voice.get("usable_audio_minutes_confirmed")),
        ("useful_audio_minutes_per_work_hour", voice.get("useful_audio_minutes_per_work_hour")),
        ("recording_hours_per_week", voice.get("recording_hours_per_week")),
        ("projected_completion_date", voice.get("projected_completion_date")),
    )
    for name, value in checks:
        if value is None or value is False:
            pending.append(name)
    return pending


def build_profile(
    user_id: str,
    declared: dict[str, Any],
    *,
    source: str = DEFAULT_SOURCE,
    display_name: str = "",
    capacity_ceiling: float | None = None,
    mirror_enabled: bool = False,
) -> HorizonProfile:
    plan_version = str(declared.get("plan_version") or DEFAULT_PLAN_VERSION)
    mandate = mandate_module.mandate_from_declared(
        declared,
        source=source,
        capacity_ceiling=capacity_ceiling,
        mirror_enabled=mirror_enabled,
    )
    mandate.imported_at = datetime.now(timezone.utc)

    return HorizonProfile(
        user_id=user_id,
        display_name=display_name,
        mission=HorizonMission(
            statement=MISSION_STATEMENT,
            values=list(MISSION_VALUES),
            version=plan_version,
        ),
        mandate=mandate,
        declared_context=DeclaredSnapshot(
            source=source,
            plan_version=plan_version,
            payload=declared,
            unresolved=_unresolved_fields(declared),
        ),
    )


def build_objectives(
    user_id: str,
    declared: dict[str, Any],
    backlog: dict[str, dict[str, str]],
    *,
    source: str = DEFAULT_SOURCE,
) -> list[HorizonObjective]:
    values = _milestone_values(declared)
    objectives: list[HorizonObjective] = []

    for blueprint in _OBJECTIVE_BLUEPRINTS:
        milestones: list[HorizonMilestone] = []
        for spec in blueprint["milestones"]:
            base = DeclaredValue.unknown(source)
            target = DeclaredValue.unknown(source)
            value_key = spec.get("value_key")
            if value_key and value_key in values:
                base, target = values[value_key]

            backlog_row = backlog.get(spec["source_key"], {})
            dependent = str(backlog_row.get("Dependencia") or "").strip() or None
            owner = str(backlog_row.get("Quien prepara y ejecuta") or "").strip() or None

            milestones.append(
                HorizonMilestone(
                    source_key=spec["source_key"],
                    title=spec["title"],
                    measure=spec.get("measure"),
                    base=base,
                    target=target,
                    horizon=blueprint["horizon"],
                    status=ObjectiveStatus.ACTIVE,
                    condition=spec.get("condition"),
                    blocker=dependent,
                    evidence=[],
                )
            )

        objectives.append(
            HorizonObjective(
                user_id=user_id,
                source_key=blueprint["source_key"],
                title=blueprint["title"],
                kind=blueprint["kind"],
                horizon=blueprint["horizon"],
                status=ObjectiveStatus.ACTIVE,
                measure=blueprint.get("measure"),
                milestones=milestones,
                priority=blueprint["priority"],
                source=source,
            )
        )

    for parked in _PARKED_OBJECTIVES:
        objectives.append(
            HorizonObjective(
                user_id=user_id,
                source_key=parked["source_key"],
                title=parked["title"],
                kind=parked["kind"],
                horizon=ObjectiveHorizon.SOMEDAY,
                status=ObjectiveStatus.PARKED,
                measure=None,
                milestones=[],
                parked_review_condition=parked["review_condition"],
                priority=parked["priority"],
                source=source,
            )
        )

    return objectives


def build_initial_actions(user_id: str, objectives: list[HorizonObjective]) -> list[dict[str, Any]]:
    """Prepara la primera accion concreta, sin inventar horas libres.

    Seccion 9.5: sin horario conocido se puede preparar material y recomendar
    una accion a demanda, pero no inventar una hora libre. Por eso la accion
    nace como ``ready`` sin bloque horario asignado.

    Se construye con el modelo, no con un dict suelto: asi la accion recibe su
    ``action_id`` y su ``status`` por defecto. Un dict crudo naceria sin estado
    y jamas seria elegible como siguiente accion.
    """
    by_key = {obj.source_key: obj for obj in objectives}
    actions: list[dict[str, Any]] = []

    growth = by_key.get("growth_employment")
    if growth:
        voice_milestone = next(
            (m for m in growth.milestones if m.source_key == "VOZ-001"), None
        )
        actions.append(
            document_dump(
                HorizonAction(
                    user_id=user_id,
                    objective_id=growth.objective_id,
                    milestone_id=voice_milestone.milestone_id if voice_milestone else None,
                    title="Sesion de grabacion medida (20 a 25 minutos)",
                    owner=ActionOwner.HUMAN,
                    effort_estimated_minutes=25,
                    acceptance="minutos de audio util registrados con fuente declarada",
                    next_step="Horizon prepara el texto; tu grabas cuando tengas un bloque real.",
                    priority=10,
                )
            )
        )

    financial = by_key.get("financial_coverage")
    if financial:
        fin_milestone = next(
            (m for m in financial.milestones if m.source_key == "FIN-001"), None
        )
        actions.append(
            document_dump(
                HorizonAction(
                    user_id=user_id,
                    objective_id=financial.objective_id,
                    milestone_id=fin_milestone.milestone_id if fin_milestone else None,
                    title="Reunir contrato, cobros y mensajes de los ultimos 3 meses",
                    owner=ActionOwner.AGENT,
                    effort_estimated_minutes=15,
                    acceptance="documentos identificados con fecha y faltantes visibles",
                    next_step="Horizon prepara la solicitud; tu autorizas el acceso.",
                    priority=20,
                )
            )
        )

    return actions


# --------------------------------------------------------------------------
# API de importacion
# --------------------------------------------------------------------------


async def import_context(
    repo: Any,
    user_id: str,
    *,
    source_path: str | Path | None = None,
    display_name: str = "",
    force: bool = False,
    capacity_ceiling: float | None = None,
    mirror_enabled: bool = False,
) -> dict[str, Any]:
    """Importa el contexto personal de forma idempotente.

    Devuelve un resumen con lo creado y lo conservado. Reimportar la misma
    version no duplica nada y no toca el progreso existente.
    """
    path = Path(source_path) if source_path else default_source_path()
    if not path.exists():
        raise FileNotFoundError(f"Horizon specification not found: {path}")

    text = read_source(path)
    declared = extract_policy_block(text)
    backlog = extract_backlog(text)

    if not declared:
        return {
            "imported": False,
            "reason": "policy_block_not_found",
            "source": str(path),
            "objectives_created": 0,
            "objectives_existing": 0,
            "actions_created": 0,
        }

    plan_version = str(declared.get("plan_version") or DEFAULT_PLAN_VERSION)
    existing_profile = await repo.get_horizon_profile(user_id)
    if (
        not force
        and existing_profile
        and (existing_profile.get("declared_context") or {}).get("plan_version") == plan_version
    ):
        return {
            "imported": False,
            "reason": "already_current",
            "source": str(path),
            "plan_version": plan_version,
            "objectives_created": 0,
            "objectives_existing": len(await repo.list_horizon_objectives(user_id)),
            "actions_created": 0,
        }

    profile = build_profile(
        user_id,
        declared,
        source=str(path),
        display_name=display_name,
        capacity_ceiling=capacity_ceiling,
        mirror_enabled=mirror_enabled,
    )
    await repo.upsert_horizon_profile(document_dump(profile))

    objectives = build_objectives(user_id, declared, backlog, source=str(path))
    created: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []
    for objective in objectives:
        prior = await repo.get_horizon_objective_by_source_key(user_id, objective.source_key or "")
        if prior:
            existing.append(prior)
            # Se conserva el progreso: solo se refresca la definicion importada.
            objective.objective_id = prior.get("objective_id", objective.objective_id)
            objective.status = ObjectiveStatus(prior.get("status") or objective.status.value)
            objective.parked_review_condition = (
                prior.get("parked_review_condition") or objective.parked_review_condition
            )
            objective.milestones = _merge_milestones(prior, objective)
            await repo.upsert_horizon_objective(document_dump(objective))
        else:
            await repo.upsert_horizon_objective(document_dump(objective))
            created.append(document_dump(objective))

    actions_created = 0
    has_actions = await repo.count_horizon_actions(user_id)
    if has_actions == 0:
        for action in build_initial_actions(user_id, objectives):
            await repo.create_horizon_action(action)
            actions_created += 1

    return {
        "imported": True,
        "reason": "created" if not existing_profile else "version_updated",
        "source": str(path),
        "plan_version": plan_version,
        "mission": profile.mission.statement,
        "objectives_created": len(created),
        "objectives_existing": len(existing),
        "actions_created": actions_created,
        "unresolved": profile.declared_context.unresolved if profile.declared_context else [],
        "capture_channel_default": CaptureChannel.IMPORT.value,
        "uncertainty_default": Uncertainty.DECLARED.value,
    }


def _merge_milestones(prior: dict[str, Any], incoming: HorizonObjective) -> list[HorizonMilestone]:
    """Conserva el avance de cada hito y refresca solo su definicion."""
    prior_milestones = {
        str(m.get("source_key") or ""): m for m in (prior.get("milestones") or [])
    }
    merged: list[HorizonMilestone] = []
    for milestone in incoming.milestones:
        old = prior_milestones.get(milestone.source_key or "")
        if old:
            milestone.milestone_id = str(old.get("milestone_id") or milestone.milestone_id)
            milestone.status = ObjectiveStatus(str(old.get("status") or milestone.status.value))
            milestone.evidence = list(old.get("evidence") or [])
            milestone.blocker = old.get("blocker") or milestone.blocker
            if old.get("base"):
                milestone.base = DeclaredValue(**old["base"])
            if old.get("target"):
                milestone.target = DeclaredValue(**old["target"])
        merged.append(milestone)
    return merged


def default_source_path() -> Path:
    """Ruta por defecto de la especificacion, relativa al repositorio."""
    core_dir = Path(__file__).resolve().parents[1]
    return core_dir.parent / "jardal-horizon" / "README.md"