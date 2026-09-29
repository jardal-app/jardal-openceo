"""Estado de la regla del 10% desde ERPNext. Cero logica LLM.

Funcion determinista de lectura del contrato
(`docs/finance-erpnext-integration-contract.md` §10): ingreso elegible,
objetivo de aporte, aportes confirmados y pendiente, calculados sobre
`GL Entry` con datos confirmados desde ERPNext. El LLM puede explicar el
resultado; la aritmética autoritativa es esta.

**Hallazgo que define el diseño** (verificado contra ERPNext el 2026-09-28): el
plan de cuentas de Jardal **no tiene ninguna cuenta de aportes, donaciones o
diezmo**. Por eso `CONTRIBUTION_ACCOUNTS` está vacía y los aportes confirmados
se devuelven como `None` con `state="needs_input"`, nunca como `0`: el proyecto
exige que lo desconocido no se dibuje como cero. Elegir la cuenta que recibe el
aporte es decisión del dueño (cambia qué significa la medición); este módulo no
la crea ni la adivina. En cuanto exista, se lista aqui y el estado pasa a `ok`
sin tocar nada más.

Base elegible: creditos a cuentas de ingreso en el periodo. La medicion NO
comprueba la contrapartida de caja: un devengado (Sales Invoice sometida)
credita la cuenta de ingreso igual que un cobro, asi que la base son ingresos
**registrados**, no cobros verificados. Por eso la voz ya no dice "cobramos"
(la prosa anterior prometia mas de lo que la medicion prueba). Movimientos
entre cuentas propias y deuda nueva no son ingreso elegible — no se diezma dos
veces el mismo dinero (mandato Horizon).

Solo lectura: no crea, no envía, no cancela. Cada respuesta lleva fecha de
corte, Company, moneda y filtros aplicados (§15). Dinero en `Decimal` (§11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from agents.finance.money_text import (
    COMPANY,
    COST_CENTER,
    LA_PAZ,
    has_dev_marker,
    now_local_iso,
)

CURRENCY = "BOB"
CONTRIBUTION_RATE = Decimal("0.10")

# Cuentas de ingreso reales de Jardal (verificadas en ERPNext, no inventadas).
INCOME_ACCOUNTS: tuple[str, ...] = (
    "Servicios - J",
    "Ventas - J",
    "Interest Income - J",
    "Interest on Fixed Deposits - J",
)
# Vacía a propósito: no existe cuenta de aportes en el plan de cuentas real.
CONTRIBUTION_ACCOUNTS: tuple[str, ...] = ()

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

_CONTRIBUTION_STRONG_RE = re.compile(
    r"\b(diezmos?|regla\s+del\s+10)\b",
    flags=re.IGNORECASE,
)
# "aporte"/"donacion"/"contribucion" son ambiguos: "aporte patronal" y "aporte
# a la reunion" son otra cosa. Solo cuentan como consulta del 10% si la frase
# pregunta por ellos y no es una orden de registro de otra cosa.
_CONTRIBUTION_WEAK_RE = re.compile(
    r"\b(aport\w+|contribuci[óo]n(?:es)?|donaci[óo]n(?:es)?)\b",
    flags=re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"\b10\s*%|diez\s+por\s+ciento", flags=re.IGNORECASE)
# Verbos de consulta. `registra|anota` NO estan: son ordenes de escritura, y
# con ellos "registra pago de luz ... con 10 % de recargo" quedaba interceptado
# por una lectura que no escribe nada.
_QUERY_VERB_RE = re.compile(
    r"\b(cu[áa]nt[oa]s?|cu[áa]l(?:es)?|estado|c[óo]mo|vamos|deb[oe]s?|falt[ao]|"
    r"pendiente|resumen|situaci[óo]n|llevo)\b",
    flags=re.IGNORECASE,
)
_WRITE_ORDER_RE = re.compile(
    r"\b(registra(?:r|me)?|anota(?:r|me)?|apunta(?:r|me)?|carga(?:r)?|"
    r"contabiliza(?:r|me)?|pagu[ée]|pago|gast[ée]|gasto|compr[ée])\b",
    flags=re.IGNORECASE,
)


def looks_like_contribution_query(text: str) -> bool:
    """Detector determinista del estado del aporte. Lectura, no registro.

    Va antes que el detector de gasto en el despacho: "registra mi diezmo
    500 bs" no puede terminar en `Gastos varios - J`, y la respuesta honesta
    hoy es que falta la cuenta que recibe el aporte.

    El vocabulario inequivoco ("diezmo", "regla del 10") se reclama siempre.
    "aporte"/"donacion"/"contribucion" solo cuando la frase pregunta por ellos:
    con una orden de escritura encima ("registra gasto del aporte patronal
    1200 bs", "anota mi aporte a la reunion") la frase pertenece al libro
    mayor o a Horizon, no a esta lectura.
    """
    blob = str(text or "").strip()
    if not blob or has_dev_marker(blob):
        return False
    if _CONTRIBUTION_STRONG_RE.search(blob):
        return True
    if _PERCENT_RE.search(blob) and _QUERY_VERB_RE.search(blob):
        return True
    if not _CONTRIBUTION_WEAK_RE.search(blob):
        return False
    return bool(_QUERY_VERB_RE.search(blob)) and not _WRITE_ORDER_RE.search(blob)


def current_month_period() -> tuple[str, str]:
    """Período por defecto: el mes en curso, en hora de Bolivia."""
    today = datetime.now(LA_PAZ).date()
    return today.replace(day=1).isoformat(), today.isoformat()


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class ContributionStatus:
    """Regla del 10% reconstruible desde ERPNext, con fecha de corte (§15)."""

    as_of: str
    company: str
    currency: str
    cost_center: str | None
    date_from: str | None
    date_to: str | None
    rate: Decimal = CONTRIBUTION_RATE
    eligible_income: Decimal = Decimal("0")
    target_contribution: Decimal = Decimal("0")
    # None = no medible todavía (no hay cuenta de aportes). Nunca 0 falso.
    confirmed_contribution: Decimal | None = None
    pending_contribution: Decimal | None = None
    contribution_accounts: tuple[str, ...] = CONTRIBUTION_ACCOUNTS
    state: str = "needs_input"
    reason: str | None = None
    entries: int = 0
    excluded_entries: int = 0
    limit_reached: bool = False
    income_by_account: dict[str, Decimal] = field(default_factory=dict)
    voucher_nos: list[str] = field(default_factory=list)

    @property
    def period_label(self) -> str:
        month_from, month_to = current_month_period()
        if self.date_from == month_from and self.date_to == month_to:
            return "este mes"
        return f"el período del {self.date_from} al {self.date_to}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "company": self.company,
            "currency": self.currency,
            "cost_center": self.cost_center,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "rate": str(self.rate),
            "eligible_income": f"{self.eligible_income:.2f}",
            "target_contribution": f"{self.target_contribution:.2f}",
            "confirmed_contribution": (
                None if self.confirmed_contribution is None
                else f"{self.confirmed_contribution:.2f}"
            ),
            "pending_contribution": (
                None if self.pending_contribution is None
                else f"{self.pending_contribution:.2f}"
            ),
            "contribution_accounts": list(self.contribution_accounts),
            "state": self.state,
            "reason": self.reason,
            "entries": self.entries,
            "excluded_entries": self.excluded_entries,
            "limit_reached": self.limit_reached,
            "income_by_account": {
                k: f"{v:.2f}" for k, v in sorted(self.income_by_account.items())
            },
            "voucher_nos": self.voucher_nos,
        }


async def get_contribution_10_status(
    client: Any,
    *,
    company: str = COMPANY,
    cost_center: str | None = COST_CENTER,
    currency: str = CURRENCY,
    date_from: str | None = None,
    date_to: str | None = None,
    rate: Decimal = CONTRIBUTION_RATE,
    limit: int = 500,
) -> ContributionStatus:
    """Calcula la regla del 10% sobre el libro mayor. SOLO lectura."""
    period_from = date_from or current_month_period()[0]
    period_to = date_to or current_month_period()[1]
    filters: list[list[Any]] = [
        ["company", "=", company],
        ["docstatus", "=", 1],
        ["is_cancelled", "=", 0],
        ["posting_date", ">=", period_from],
        ["posting_date", "<=", period_to],
    ]
    if cost_center:
        filters.append(["cost_center", "=", cost_center])

    list_gl = getattr(client, "list_gl_entries", None)
    if callable(list_gl):
        fetched = await list_gl(filters=filters, fields=_GL_FIELDS, limit=limit)
    else:  # clientes falsos en tests: solo exponen list_docs
        fetched = await client.list_docs(
            "GL Entry", filters=filters, fields=_GL_FIELDS, limit=limit
        )
    rows = list(fetched or [])

    status = ContributionStatus(
        as_of=now_local_iso(),
        company=company,
        currency=currency,
        cost_center=cost_center,
        date_from=period_from,
        date_to=period_to,
        rate=rate,
        # Se pasa el valor VIVO del global: el default del dataclass se congela
        # al importar y el payload decia `contribution_accounts: []` junto a una
        # aritmetica que si leyo la lista configurada (§15).
        contribution_accounts=CONTRIBUTION_ACCOUNTS,
    )
    income_total = Decimal("0")
    confirmed_total = Decimal("0")
    for row in rows:
        account = str(row.get("account") or "")
        is_income = account in INCOME_ACCOUNTS
        is_contribution = account in CONTRIBUTION_ACCOUNTS
        if not (is_income or is_contribution):
            continue
        if str(row.get("account_currency") or "") != currency:
            status.excluded_entries += 1
            continue
        debit = _to_decimal(row.get("debit_in_account_currency"))
        credit = _to_decimal(row.get("credit_in_account_currency"))
        status.entries += 1
        voucher = str(row.get("voucher_no") or "").strip()
        if voucher and voucher not in status.voucher_nos:
            status.voucher_nos.append(voucher)
        if is_income:
            net = credit - debit
            income_total += net
            status.income_by_account[account] = (
                status.income_by_account.get(account, Decimal("0")) + net
            )
        else:
            confirmed_total += debit - credit

    status.eligible_income = _money(income_total)
    status.target_contribution = _money(income_total * rate)
    status.limit_reached = len(rows) >= limit

    if CONTRIBUTION_ACCOUNTS:
        status.confirmed_contribution = _money(confirmed_total)
        status.pending_contribution = _money(status.target_contribution - confirmed_total)
        status.state = "ok"
        status.reason = None
    else:
        status.confirmed_contribution = None
        status.pending_contribution = None
        status.state = "needs_input"
        status.reason = (
            "El plan de cuentas de Jardal no tiene una cuenta de aportes o "
            "donaciones, así que lo aportado no es medible todavía. Definir esa "
            "cuenta en ERPNext es decisión del dueño."
        )
    return status


def _honesty_notes(status: ContributionStatus) -> str:
    """Avisos que la voz no puede omitir: base parcial o moneda excluida.

    Se calculaban y se serializaban en `to_dict()` pero la rama `ok` retornaba
    antes de contarlos, asi que una base truncada por `limit` (el cliente real
    pide 500 filas sin paginar) se presentaba como completa.
    """
    notes: list[str] = []
    if status.limit_reached:
        notes.append(
            "la consulta se cortó en el límite de filas, así que el total puede ser parcial"
        )
    if status.excluded_entries:
        notes.append(f"{status.excluded_entries} asientos en otra moneda quedaron fuera")
    if not notes:
        return ""
    return " Ojo: " + "; ".join(notes) + "."


def build_spoken_summary(status: ContributionStatus) -> str:
    """Respuesta breve apta para TTS: sin tablas, UUIDs ni URLs.

    Dice "el libro registra" y no "cobramos": la base son créditos a cuentas de
    ingreso, y un devengado cuenta igual que un cobro. La contrapartida de caja
    no se verifica (ver docstring del módulo).
    """
    rate_pct = f"{status.rate * 100:.0f}"
    base = (
        f"En {status.period_label} el libro registra {status.eligible_income:.2f} "
        f"{status.currency} en cuentas de ingreso, así que el {rate_pct}% son "
        f"{status.target_contribution:.2f}."
    )
    notes = _honesty_notes(status)
    if status.state == "ok":
        assert status.confirmed_contribution is not None
        assert status.pending_contribution is not None
        return (
            f"{base} Llevamos aportados {status.confirmed_contribution:.2f} y "
            f"quedan pendientes {status.pending_contribution:.2f}.{notes}"
        )
    return (
        f"{base} Todavía no podemos confirmar cuánto aportamos: falta definir "
        f"en ERPNext la cuenta que recibe los aportes.{notes}"
    )
