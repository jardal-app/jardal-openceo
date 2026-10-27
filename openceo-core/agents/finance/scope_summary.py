"""Resumen de saldos y gasto por Scope desde Jardal ERP. Cero logica LLM.

Lectura Fase A del contrato (`docs/finance-jardal-erp-integration-contract.md`
§4 y §15): `finance.get_scope_summary` es un tool semantico cerrado de SOLO
lectura sobre `GL Entry`. No crea, no envia, no cancela nada: el unico
escritor del libro mayor sigue siendo el submit del `Journal Entry` de
`finance.register_expense`.

Cada respuesta lleva fecha/hora de corte (`as_of`), Company, moneda y
filtros aplicados (§15). Sin ledger paralelo: Mongo guarda como maximo la
referencia consultada. Dinero siempre en `Decimal`, nunca `float` (§11).
`execute_money_movements=false` sigue vigente.

Filtro de Scope: el DocType `OpenCEO Scope` existe pero no tiene registros
y el GL no trae columna de scope, asi que el eje operativo es Cost Center
(`Principal - J`) + Company. No se inventa ninguna dimension.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

# Bolivia no observa horario de verano: offset fijo, sin dependencia de tzdata.
_LA_PAZ = timezone(timedelta(hours=-4))

COMPANY = "Jardal"
COST_CENTER = "Principal - J"
CURRENCY = "BOB"

_GL_FIELDS = [
    "name",
    "posting_date",
    "account",
    "account_currency",
    "debit_in_account_currency",
    "credit_in_account_currency",
    "cost_center",
    "company",
    "voucher_no",
    "is_cancelled",
    "docstatus",
]

_BALANCE_VERB_RE = re.compile(
    r"\b(cu[áa]nto|saldo|balance|gastamos|gastado|resumen|total|"
    r"cuentas?|fondos?|efectivo|caja)\b",
    flags=re.IGNORECASE,
)
_FINANCE_NOUN_RE = re.compile(
    r"\b(gastos?|saldo|balance|resumen|cuentas?|fondos?|efectivo|caja|"
    r"scope|centro\s+de\s+costos?|cost\s*center|"
    r"mes(?:es)?|semana|d[íi]a|hoy|ayer|a[ñn]o)\b",
    flags=re.IGNORECASE,
)
_DEV_MARKER_RE = re.compile(
    r"\b(tickets?|presets?|sesi[óo]n(?:es)?\s+dev|git|commit|branch|merge|deploy|"
    r"tmux|codex|claude|pytest|tests?|c[óo]digo|refactor|workflow|horizonte|horizon)\b",
    flags=re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_QUERY_WORD_RE = re.compile(
    r"\b(cu[áa]nto|c[uú]al(?:es)?|dime|dame|mu[ée]strame)\b",
    flags=re.IGNORECASE,
)


def looks_like_balance_query(text: str) -> bool:
    """Detector determinista de consultas de saldo/gasto. Sin monto, sin marcas dev."""
    blob = str(text or "").strip()
    if not blob or _DEV_MARKER_RE.search(blob):
        return False
    if not _BALANCE_VERB_RE.search(blob):
        return False
    return bool(_FINANCE_NOUN_RE.search(blob) or _QUERY_WORD_RE.search(blob))


_PERIOD_YESTERDAY_RE = re.compile(r"\bayer\b")
_PERIOD_TODAY_RE = re.compile(r"\b(hoy|ahora)\b")
_PERIOD_WEEK_RE = re.compile(r"\b(esta\s+semana|la\s+semana|semana)\b")
_PERIOD_PAST_MONTH_RE = re.compile(r"\b(mes\s+pasado|mes\s+anterior)\b")
_PERIOD_MONTH_RE = re.compile(r"\b(este\s+mes|mes)\b")
_PERIOD_YEAR_RE = re.compile(r"\b(este\s+ano|ano)\b")


def period_bounds(text: str) -> tuple[str | None, str | None]:
    """Cotas de fecha que el dictado pide, en hora de Bolivia.

    Devuelve `(None, None)` cuando la frase no acota período. Antes la ruta de
    voz no pasaba el texto, así que "cuánto gastamos este mes" respondía el
    total histórico sin decirlo (el detector sí enruta las palabras de período
    hacia esta consulta, así que la cotas tienen que derivarse aquí).
    """
    blob = _strip_accents(str(text or "")).lower()
    today = datetime.now(_LA_PAZ).date()
    if _PERIOD_YESTERDAY_RE.search(blob):
        day = today - timedelta(days=1)
        return day.isoformat(), day.isoformat()
    if _PERIOD_TODAY_RE.search(blob):
        return today.isoformat(), today.isoformat()
    if _PERIOD_WEEK_RE.search(blob):
        return (today - timedelta(days=today.weekday())).isoformat(), today.isoformat()
    if _PERIOD_PAST_MONTH_RE.search(blob):
        previous = today.replace(day=1) - timedelta(days=1)
        return previous.replace(day=1).isoformat(), previous.isoformat()
    if _PERIOD_MONTH_RE.search(blob):
        return today.replace(day=1).isoformat(), today.isoformat()
    if _PERIOD_YEAR_RE.search(blob):
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    return None, None


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


@dataclass
class ScopeSummary:
    """Resumen reconstruible desde Jardal ERP, con fecha de corte (§15)."""

    as_of: str
    company: str
    currency: str
    cost_center: str | None
    date_from: str | None
    date_to: str | None
    total_debit: Decimal = Decimal("0")
    total_credit: Decimal = Decimal("0")
    net: Decimal = Decimal("0")
    entries: int = 0
    excluded_entries: int = 0
    limit_reached: bool = False
    by_account: dict[str, Decimal] = field(default_factory=dict)
    voucher_nos: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "company": self.company,
            "currency": self.currency,
            "cost_center": self.cost_center,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "total_debit": f"{self.total_debit:.2f}",
            "total_credit": f"{self.total_credit:.2f}",
            "net": f"{self.net:.2f}",
            "entries": self.entries,
            "excluded_entries": self.excluded_entries,
            "limit_reached": self.limit_reached,
            "by_account": {k: f"{v:.2f}" for k, v in sorted(self.by_account.items())},
            "voucher_nos": self.voucher_nos,
        }


async def get_scope_summary(
    client: Any,
    *,
    company: str = COMPANY,
    cost_center: str | None = COST_CENTER,
    currency: str = CURRENCY,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 500,
) -> ScopeSummary:
    """Suma debe-haber del libro mayor. SOLO lectura: usa el lector dedicado
    `list_gl_entries` del cliente (o `list_docs` en clientes sin ese metodo).
    Nunca crea ni modifica."""
    filters: list[list[Any]] = [
        ["company", "=", company],
        ["docstatus", "=", 1],
        ["is_cancelled", "=", 0],
    ]
    if cost_center:
        filters.append(["cost_center", "=", cost_center])
    if date_from:
        filters.append(["posting_date", ">=", date_from])
    if date_to:
        filters.append(["posting_date", "<=", date_to])

    # SOLO lectura: lector dedicado del cliente real, `list_docs` en fakes.
    # Nunca crea ni modifica.
    list_gl = getattr(client, "list_gl_entries", None)
    if callable(list_gl):
        fetched = await list_gl(filters=filters, fields=_GL_FIELDS, limit=limit)
    else:  # clientes falsos en tests: solo exponen list_docs
        fetched = await client.list_docs(
            "GL Entry", filters=filters, fields=_GL_FIELDS, limit=limit
        )
    rows = list(fetched or [])
    summary = ScopeSummary(
        as_of=datetime.now(_LA_PAZ).isoformat(timespec="seconds"),
        company=company,
        currency=currency,
        cost_center=cost_center,
        date_from=date_from,
        date_to=date_to,
    )
    for row in rows:
        if str(row.get("account_currency") or "") != currency:
            summary.excluded_entries += 1
            continue
        debit = _to_decimal(row.get("debit_in_account_currency"))
        credit = _to_decimal(row.get("credit_in_account_currency"))
        summary.total_debit += debit
        summary.total_credit += credit
        summary.entries += 1
        account = str(row.get("account") or "sin cuenta")
        summary.by_account[account] = summary.by_account.get(account, Decimal("0")) + debit - credit
        voucher = str(row.get("voucher_no") or "").strip()
        if voucher and voucher not in summary.voucher_nos:
            summary.voucher_nos.append(voucher)
    summary.net = summary.total_debit - summary.total_credit
    summary.limit_reached = len(rows) >= limit
    return summary


def _period_label(summary: ScopeSummary) -> str:
    if summary.date_from and summary.date_to:
        if summary.date_from == summary.date_to:
            return f" del {summary.date_from}"
        return f" del {summary.date_from} al {summary.date_to}"
    return " del histórico completo"


def build_spoken_summary(summary: ScopeSummary) -> str:
    """Respuesta breve apta para TTS: sin tablas, UUIDs ni URLs.

    Lleva el período medido (o "histórico completo") y los calificadores que
    `excluded_entries`/`limit_reached` ya calculaban: sin ellos una base
    truncada o sin moneda se presentaba como completa.
    """
    scope = f" en {summary.cost_center}" if summary.cost_center else ""
    notes: list[str] = []
    if summary.excluded_entries:
        notes.append(f"{summary.excluded_entries} asientos en otra moneda quedaron fuera")
    if summary.limit_reached:
        notes.append(
            "la consulta se cortó en el límite de filas, así que el total puede ser parcial"
        )
    extra = (" Ojo: " + "; ".join(notes) + ".") if notes else ""
    return (
        f"Tenemos {summary.net:.2f} {summary.currency} netos{scope}"
        f"{_period_label(summary)}: "
        f"{summary.total_debit:.2f} en debe y {summary.total_credit:.2f} en haber, "
        f"en {summary.entries} asientos.{extra}"
    )
