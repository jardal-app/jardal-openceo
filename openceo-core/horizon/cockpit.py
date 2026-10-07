"""Cockpit del dueno (F5, TASK-2026-00120): empresa + decisiones en Horizon.

Puro: normaliza decisiones, calcula urgencia y compone mensajes de voz.
No toca repos ni red; la orquestacion vive en ``HorizonService``.

Reglas que este modulo fija:

- La bandeja lista la misma fuente que la vista Requiere humano
  (TASK-2026-00041): tickets en ``waiting`` + QA pendiente o con cambios
  pedidos. El filtrado por "espera humana de verdad" lo hace
  ``waiting_needs_human_review`` (F4, TASK-2026-00118), no este modulo.
- Urgente es determinista: prioridad critica, costo con plazo cercano o
  tickets bloqueados detras. Lo urgente reaparece hasta responderse; lo
  agrupado se entrega una sola vez y no se repite.
- Desconocido es ``None``, nunca ``0``: sin lectura no se inventan cifras.
- Voz en primera persona plural, sin UUIDs, URLs ni tablas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

# Entidad de ``horizon_events`` que marca una decision como entregada.
# Solo guarda referencia (task_id + titulo): los datos de empresa entran a
# Horizon en solo lectura y nunca se vuelca un cuerpo de ticket.
DECISION_DELIVERY_ENTITY = "decision"
DECISION_DELIVERY_SOURCE_PREFIX = "decision-delivered:"

# Un costo con vencimiento dentro de este plazo es urgente (ticket: "plazo de
# un costo, bloqueo").
URGENT_COST_DUE_HOURS = 72

# Topes de lectura del cockpit. Acotados y con indice; nunca un barrido.
DECISIONS_LIMIT = 50
COMPANY_CLOSED_LIMIT = 10
COMPANY_BLOCKED_LIMIT = 20
COMPANY_BUSINESS_LIMIT = 50


def delivery_source_id(task_id: str) -> str:
    return f"{DECISION_DELIVERY_SOURCE_PREFIX}{task_id}"


def erp_mirror_name(task: Mapping[str, Any] | None) -> str | None:
    """Nombre de negocio (``TASK-...``) si el ticket ya viajo a ERPNext."""
    if not task:
        return None
    params = task.get("params")
    if not isinstance(params, dict):
        return None
    mirrors = params.get("_mirrors")
    if not isinstance(mirrors, dict):
        return None
    erp = mirrors.get("erpnext")
    if not isinstance(erp, dict):
        return None
    name = str(erp.get("name") or "").strip()
    return name or None


def decision_kind(task: Mapping[str, Any]) -> str:
    """Tipo de decision. Espeja ``inboxType`` de HumanInbox.jsx.

    ``owner_escalation`` va primero: un escalado del mandato (F4) tambien
    puede traer ``approval_state`` pendiente y no es una aprobacion generica.
    """
    phase = str(task.get("phase") or "").strip()
    if phase == "owner_escalation":
        return "owner_escalation"
    qa_state = str(task.get("qa_review_state") or "").strip()
    if qa_state in ("pending", "changes_requested") or phase == "qa_review":
        return "qa"
    if phase == "client_signoff":
        return "cliente"
    return "aprobacion"


def escalation_category(task: Mapping[str, Any]) -> str | None:
    params = task.get("params")
    if not isinstance(params, dict):
        return None
    escalation = params.get("escalation")
    if not isinstance(escalation, dict):
        # Forma plana de respaldo: waiting_reason "owner_escalation:<categoria>".
        reason = str(task.get("waiting_reason") or "")
        if reason.startswith("owner_escalation:"):
            return reason.split(":", 1)[1].strip() or None
        return None
    category = str(escalation.get("category") or "").strip()
    return category or None


def decision_question(task: Mapping[str, Any], *, max_chars: int = 300) -> str:
    """La pregunta concreta al dueno, apta para voz."""
    params = task.get("params") if isinstance(task.get("params"), dict) else {}
    escalation = params.get("escalation")
    if isinstance(escalation, dict):
        human_action = str(escalation.get("human_action") or "").strip()
        if human_action:
            return _short(human_action, max_chars)
    summary = str(task.get("last_response_summary") or "").strip()
    if summary:
        return _short(summary, max_chars)
    title = str(task.get("title") or "").strip()
    return _short(title or "Decisión pendiente.", max_chars)


def _short(text: str, max_chars: int) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def task_due_date(task: Mapping[str, Any]) -> datetime | None:
    """Vencimiento del ticket, si viene en un formato conocido.

    ``due_date`` viaja en ``params`` (MCP/API, ``YYYY-MM-DD``) o arriba del
    documento. Lo desconocido o ilegible es ``None``: nunca inventa urgencia.
    """
    raw = task.get("due_date")
    if raw is None and isinstance(task.get("params"), dict):
        raw = task.get("params", {}).get("due_date")
    if isinstance(raw, datetime):
        candidate = raw
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("T", " ").replace("Z", "")
        candidate = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                candidate = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        if candidate is None:
            return None
    else:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate


def urgency_reason(
    task: Mapping[str, Any],
    *,
    now: datetime | None = None,
    has_dependents: bool = False,
) -> str | None:
    """Motivo de urgencia, o ``None`` si la decision va agrupada.

    Tres causas, en orden: prioridad critica, costo con plazo dentro de
    ``URGENT_COST_DUE_HOURS`` y bloqueo (tickets en espera detras de este).
    """
    if str(task.get("priority") or "").strip().lower() == "critical":
        return "prioridad_critica"
    if has_dependents:
        return "bloqueo"
    if (
        decision_kind(task) == "owner_escalation"
        and escalation_category(task) == "gasto_nuevo"
    ):
        due = task_due_date(task)
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if due is not None and due <= moment + timedelta(hours=URGENT_COST_DUE_HOURS):
            return "plazo_de_costo"
    return None


def normalize_decision(
    task: Mapping[str, Any],
    *,
    ordinal: int,
    delivered: bool,
    urgent: bool,
    urgency: str | None,
    answerable: bool,
) -> dict[str, Any]:
    """Item de bandeja. Referencia de empresa, sin datos personales."""
    return {
        "ordinal": ordinal,
        "task_id": str(task.get("task_id") or ""),
        "erp_name": erp_mirror_name(task),
        "ticket_number": task.get("ticket_number"),
        "title": str(task.get("title") or ""),
        "project": task.get("project"),
        "kind": decision_kind(task),
        "category": escalation_category(task),
        "question": decision_question(task),
        "urgent": urgent,
        "urgency": urgency,
        "delivered": delivered,
        "answerable": answerable,
        "status": task.get("status"),
        "phase": task.get("phase"),
        "waiting_reason": task.get("waiting_reason"),
        "priority": task.get("priority"),
        "updated_at": task.get("updated_at"),
    }


def describe_company(company: Mapping[str, Any]) -> str:
    """Parrafo de voz con el status de empresa."""
    parts: list[str] = []
    objectives = company.get("objectives") or []
    if objectives:
        names = ", ".join(
            str(o.get("title") or o.get("goal") or o.get("objective_id") or "")
            for o in objectives[:3]
        )
        if len(objectives) > 1:
            parts.append(f"Tenemos {len(objectives)} objetivos de empresa activos: {names}.")
        else:
            parts.append(f"Tenemos un objetivo de empresa activo: {names}.")
    else:
        parts.append("No tenemos objetivos de empresa activos.")
    closed = company.get("recently_closed") or []
    if closed:
        titles = ", ".join(str(t.get("title") or "") for t in closed[:3])
        if len(closed) > 1:
            parts.append(f"Cerramos {len(closed)} tickets recientemente: {titles}.")
        else:
            parts.append(f"Cerramos recientemente: {titles}.")
    blocked = company.get("blocked") or []
    if blocked:
        titles = ", ".join(str(t.get("title") or "") for t in blocked[:3])
        if len(blocked) > 1:
            parts.append(f"Tenemos {len(blocked)} tickets bloqueados: {titles}.")
        else:
            parts.append(f"Tenemos un ticket bloqueado: {titles}.")
    desk_only = company.get("business_open_without_execution") or []
    if desk_only:
        if len(desk_only) > 1:
            parts.append(
                f"Hay {len(desk_only)} tickets de negocio sin ejecución asociada."
            )
        else:
            parts.append("Hay un ticket de negocio sin ejecución asociada.")
    if company.get("business_unknown"):
        parts.append("No pudimos leer el registro de negocio ahora mismo.")
    if company.get("objectives_unknown"):
        parts.append("No pudimos leer los objetivos ahora mismo.")
    return " ".join(parts)


def describe_decisions(
    urgent: list[Mapping[str, Any]],
    grouped: list[Mapping[str, Any]],
) -> str:
    """Parrafo de voz con las decisiones pendientes."""
    if not urgent and not grouped:
        return "No hay decisiones esperando."
    parts: list[str] = []
    if urgent:
        if len(urgent) == 1:
            item = urgent[0]
            parts.append(
                f"Urgente: {item.get('title')}. {item.get('question')}"
            )
        else:
            titles = "; ".join(
                f"{i + 1}) {item.get('title')}" for i, item in enumerate(urgent)
            )
            parts.append(f"Tenemos {len(urgent)} decisiones urgentes: {titles}.")
    if grouped:
        if len(grouped) == 1:
            parts.append(
                f"Ademas hay una decision agrupada: {grouped[0].get('title')}. "
                f"{grouped[0].get('question')}"
            )
        else:
            parts.append(f"Ademas hay {len(grouped)} decisiones agrupadas en el brief.")
    return " ".join(parts)


def describe_decision_list(items: list[Mapping[str, Any]], *, limit: int = 5) -> str:
    """Lista corta con ordinales ("1) titulo"), para pedir cual responder."""
    shown = list(items or [])[: max(1, limit)]
    if not shown:
        return "No hay decisiones en bandeja."
    joined = "; ".join(
        f"{item.get('ordinal')}) {str(item.get('title') or 'sin título')}"
        for item in shown
    )
    rest = len(items or []) - len(shown)
    tail = f" Y {rest} más." if rest > 0 else ""
    return f"{joined}.{tail}"
