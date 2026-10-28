"""Reglas de visibilidad de negocio en Jardal ERP (TASK-2026-00146 + 00147).

Puras y deterministas: cero I/O, cero LLM. Todo lo que toca red o Mongo vive
en `TaskService` (`_refresh_visibility` y ganchos); aquí solo cálculo sobre
datos ya leídos, con el `now` inyectado para que los tests sean exactos.

Verificadas contra el sitio vivo (2026-10-08):

- Frappe NO calcula el % del grupo; SÍ pone 100 al cerrar una hoja y lo
  deja en 100 al reabrir → la regla lee `status`, nunca el progress hoja.
- El fin del hijo no puede pasar el fin del padre (417 `InvalidDates`);
  el padre corto sí se acepta → al extender, ancestros primero.
- Frappe NO reprograma dependientes ni al grupo al mover fechas.
- `exp_*` son Datetime en hora del sitio (`YYYY-MM-DD HH:MM:SS`);
  `completed_on` es Date (`YYYY-MM-DD`) y no se pone solo.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Mapping
from zoneinfo import ZoneInfo

#: Formatos canónicos del sitio (verificados por roundtrip en vivo).
SITE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
SITE_DATE_FORMAT = "%Y-%m-%d"

#: Zona del sitio cuando nadie la configura. UTC-4 fijo, coherente con todos
#: los pares creation/created_at observados (2026-09-28 a 2026-10-07).
#: `JARDAL_ERP_SITE_TIMEZONE` la sustituye; ver `settings.py`.
DEFAULT_SITE_TIMEZONE = "America/Caracas"

#: Duración estimada por tier cuando no hay historial (días naturales).
#: El dueño acepta que la estimación sea aproximada: lo que importa es que
#: la gráfica funcione.
TIER_DEFAULT_DAYS: dict[str, int] = {
    "tier_s": 5,
    "tier_a": 3,
    "tier_b": 2,
    "tier_c": 1,
}

#: Jornada supuesta para las estimaciones (hora del sitio).
WORKDAY_START_HOUR = 9
WORKDAY_END_HOUR = 18

#: Topes anti-barrido de la visibilidad (eventos acotados, nunca polling).
MAX_ROLLUP_DEPTH = 5
MAX_CHILDREN_ROLLUP = 200
MAX_HISTORY_ROWS = 200
MAX_DEPENDENT_RESOLVE = 50


# --- zona horaria del sitio -----------------------------------------------

def site_zone(name: str | None) -> ZoneInfo:
    """Zona IANA del sitio. Estricta: un nombre malo lanza (fail-closed).

    Unas fechas convertidas en silencio a la zona equivocada serían peores
    que un error ruidoso en el log.
    """
    return ZoneInfo(str(name or "").strip() or DEFAULT_SITE_TIMEZONE)


def _as_utc(value: datetime) -> datetime:
    # Motor/Mongo decodifica en naive; en OpenCEO todo naive es UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def as_utc_datetime(value: Any) -> datetime | None:
    """datetime o ISO 8601 → UTC aware. None o ilegible → None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def to_site_datetime(value: datetime, zone: str | None) -> str:
    """Instante UTC → `YYYY-MM-DD HH:MM:SS` en hora del sitio."""
    return _as_utc(value).astimezone(site_zone(zone)).strftime(SITE_DATETIME_FORMAT)


def to_site_date(value: datetime, zone: str | None) -> str:
    """Instante UTC → `YYYY-MM-DD` en hora del sitio."""
    return _as_utc(value).astimezone(site_zone(zone)).strftime(SITE_DATE_FORMAT)


def parse_site_datetime(text: Any) -> datetime | None:
    """`YYYY-MM-DD HH:MM:SS[.ffffff]` → naive en hora del sitio.

    None o ilegible → None (el llamador ignora ese dato, no inventa).
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    for fmt in (SITE_DATETIME_FORMAT, SITE_DATETIME_FORMAT + ".%f"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_site_day(text: Any) -> date | None:
    """Fecha (día) de un Datetime o Date del sitio. None si ilegible."""
    stamp = parse_site_datetime(text)
    if stamp is not None:
        return stamp.date()
    raw = str(text or "").strip()
    try:
        return datetime.strptime(raw, SITE_DATE_FORMAT).date()
    except ValueError:
        return None


def site_datetime_to_utc(text: Any, zone: str | None) -> datetime | None:
    """`YYYY-MM-DD HH:MM:SS` del sitio → UTC aware. None si ilegible."""
    naive = parse_site_datetime(text)
    if naive is None:
        return None
    try:
        return naive.replace(tzinfo=site_zone(zone)).astimezone(timezone.utc)
    except Exception:
        return None


# --- % de avance de grupos (00146) -----------------------------------------

def group_progress(children: list[Mapping[str, Any]]) -> float:
    """% de un grupo desde sus hijos (vocabulario de NEGOCIO).

    Completed = 100, Cancelled sale del denominador, un hijo grupo aporta
    su propio % guardado (fresco por propagación bottom-up) y el resto 0.
    Sin hijos activos → 0.0. Nunca promedia progress de hojas (Frappe lo
    deja en 100 al reabrir).
    """
    total = 0.0
    count = 0
    for child in children:
        status = str(child.get("status") or "").strip()
        if status == "Cancelled":
            continue
        count += 1
        if status == "Completed":
            total += 100.0
        elif child.get("is_group"):
            try:
                total += max(0.0, min(100.0, float(child.get("progress") or 0.0)))
            except (TypeError, ValueError):
                continue
    if not count:
        return 0.0
    return round(total / count, 2)


def group_span(
    children: list[Mapping[str, Any]],
    explicit_end: str | None = None,
) -> tuple[str | None, str | None]:
    """Span de un grupo: [mín inicios, máx fines] de sus hijos fechados.

    Las canceladas salen del span (no ocurrieron). El fin nunca baja de la
    fecha límite explícita del grupo: Frappe exige fin-hijo ≤ fin-padre, así
    que la explícita es un suelo, no un valor a pisar. Sin hijos fechados →
    (None, explícita o None).
    """
    starts: list[str] = []
    ends: list[str] = []
    for child in children:
        if str(child.get("status") or "").strip() == "Cancelled":
            continue
        start = parse_site_datetime(child.get("exp_start_date"))
        end = parse_site_datetime(child.get("exp_end_date"))
        if start is not None:
            starts.append(start.strftime(SITE_DATETIME_FORMAT))
        if end is not None:
            ends.append(end.strftime(SITE_DATETIME_FORMAT))
    due = parse_site_datetime(explicit_end)
    if due is not None:
        ends.append(due.strftime(SITE_DATETIME_FORMAT))
    if not starts and not ends:
        return None, None
    # El formato canónico ordena lexicográficamente igual que cronológico.
    span_start = min(starts) if starts else None
    span_end = max(ends) if ends else None
    if span_start is not None and span_end is not None and span_end < span_start:
        # Hijos con fechas parciales inconsistentes (un inicio tardío sin fin
        # y un fin temprano sin inicio): Frappe rechaza fin < inicio, así que
        # el span degenera a un punto. Cazado por el backfill 00147.
        span_end = span_start
    return span_start, span_end


# --- estimación determinista (00147) ----------------------------------------

def median_durations(rows: list[Mapping[str, Any]]) -> dict[str | None, int]:
    """Mediana de días naturales por tier desde cerrados con ambas fechas.

    Mínimo 1 día (un ticket abierto y cerrado el mismo día estima 1).
    La clave None agrupa los cerrados sin tier conocido.
    """
    buckets: dict[str | None, list[int]] = {}
    for row in rows:
        start = parse_site_datetime(row.get("exp_start_date"))
        end = parse_site_datetime(row.get("exp_end_date"))
        if start is None or end is None or end < start:
            continue
        days = max(1, (end.date() - start.date()).days)
        tier = row.get("openceo_coding_tool")
        key = str(tier).strip() if tier else None
        buckets.setdefault(key, []).append(days)
    return {tier: max(1, int(round(median(days)))) for tier, days in buckets.items()}


def tier_duration_days(tier: Any, medians: Mapping[str | None, int]) -> int:
    """Duración estimada: mediana del tier, o defecto (c: 1 día)."""
    key = str(tier or "").strip() or None
    if key is not None and key in medians:
        return max(1, int(medians[key]))
    if None in medians:
        return max(1, int(medians[None]))
    return TIER_DEFAULT_DAYS.get(key or "", TIER_DEFAULT_DAYS["tier_c"])


def estimate_window(
    *,
    today: date,
    tier: Any,
    dep_ends: list[Any],
    medians: Mapping[str | None, int],
) -> tuple[str, str]:
    """Ventana estimada: inicio = máx(hoy, fines de depends_on).

    Las 09:00–18:00 en hora del sitio son convención de jornada, no dato.
    """
    latest = today
    for raw in dep_ends:
        day = parse_site_day(raw)
        if day is not None and day > latest:
            latest = day
    start_day = latest
    end_day = latest + timedelta(days=max(1, tier_duration_days(tier, medians)))
    start = datetime(start_day.year, start_day.month, start_day.day, WORKDAY_START_HOUR)
    end = datetime(end_day.year, end_day.month, end_day.day, WORKDAY_END_HOUR)
    return start.strftime(SITE_DATETIME_FORMAT), end.strftime(SITE_DATETIME_FORMAT)


# --- fechas en el mismo PUT del estado ---------------------------------------

def work_dates_for_push(
    *,
    openceo_status: str,
    started_at: datetime | None,
    completed_at: datetime | None,
    zone: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Fechas para el PUT de `update_status` (camino runtime).

    Solo los estados que fijan un hecho temporal aportan fechas; el resto
    devuelve {} y el PUT queda como hasta ahora. `now` UTC inyectado.
    """
    status = str(openceo_status or "").strip().lower()
    moment = _as_utc(now)
    if status == "in_progress":
        return {
            "exp_start_date": to_site_datetime(started_at or moment, zone),
            "completed_on": None,
            "progress": 0,
        }
    if status == "completed":
        done = completed_at or moment
        return {
            "exp_end_date": to_site_datetime(done, zone),
            "completed_on": to_site_date(done, zone),
            "progress": 100,
        }
    return {}


def work_dates_for_transition(
    *,
    target: str,
    zone: str | None,
    now: datetime,
    explicit_due: str | None = None,
) -> dict[str, Any]:
    """Fechas para el PUT de `transition_status` (MCP/sesiones).

    Working fija el inicio real (pisa estimación, nunca un real: el inicio
    solo se escribe al pasar a Working). Completed fija fin + done (pisa
    estimación y explícita: la realidad manda; la explícita sobrevive en el
    espejo). Open (reapertura) limpia fin+done y restaura la explícita si la
    hay; sin ella el worker re-estima. El resto no toca fechas.
    """
    moment = _as_utc(now)
    if target == "Working":
        return {
            "exp_start_date": to_site_datetime(moment, zone),
            "completed_on": None,
            "progress": 0,
        }
    if target == "Completed":
        return {
            "exp_end_date": to_site_datetime(moment, zone),
            "completed_on": to_site_date(moment, zone),
            "progress": 100,
        }
    if target == "Open":
        dates: dict[str, Any] = {"completed_on": None, "progress": 0}
        due = parse_site_datetime(explicit_due)
        dates["exp_end_date"] = (
            due.strftime(SITE_DATETIME_FORMAT) if due is not None else None
        )
        return dates
    return {}


def adopt_explicit_due(
    live_end: Any,
    recorded_estimate: Any,
    recorded_explicit: Any,
) -> str | None:
    """Adopta como explícita una fecha límite puesta fuera de OpenCEO.

    Si el vivo difiere de nuestra última estimación y de la explícita
    registrada, alguien (dueño en desk, agente por MCP) puso una fecha:
    se adopta y las estimaciones dejan de pisarla. Sin cambios → se
    conserva la registrada.
    """
    live = parse_site_datetime(live_end)
    if live is None:
        recorded = parse_site_datetime(recorded_explicit)
        return recorded.strftime(SITE_DATETIME_FORMAT) if recorded else None
    live_str = live.strftime(SITE_DATETIME_FORMAT)
    estimate = parse_site_datetime(recorded_estimate)
    estimate_str = estimate.strftime(SITE_DATETIME_FORMAT) if estimate else None
    if live_str != estimate_str:
        return live_str
    recorded = parse_site_datetime(recorded_explicit)
    return recorded.strftime(SITE_DATETIME_FORMAT) if recorded else None
