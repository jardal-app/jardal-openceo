"""Registro ocasional de ingresos por voz/chat hacia ERPNext. Cero logica LLM.

Segundo flujo real de la Fase B del contrato
(`docs/finance-erpnext-integration-contract.md` §16): `finance.register_income`,
espejo de `finance.register_expense` (TASK-2026-00032) con las dos patas del
asiento invertidas: debito a efectivo / credito a la cuenta de ingreso.

Cada ingreso ya cobrado ("registra ingreso sueldo 6200 bolivianos",
"me pagaron 500 dólares del contrato", "anota ingreso docencia 3800 bs") se
contabiliza como `Journal Entry` idempotente por `openceo_ref`, en `Decimal`
(nunca `float`), Company `Jardal`, Cost Center `Principal - J` y moneda
explicita (BOB por defecto; USDT no es USD). No crea tickets: el registro es
el efecto.

Las cuentas de ingreso son las que existen en el plan de cuentas real de
Jardal, verificadas contra ERPNext el 2026-09-28; no se crea ninguna cuenta
nueva ni se inventa ninguna dimension.

Contabiliza dinero YA cobrado: `execute_money_movements=false` sigue vigente
(contrato §8), esto no mueve dinero externo ni emite facturas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agents.finance.journal import new_openceo_ref as _new_ref
from agents.finance.journal import submit_journal_entry
from agents.finance.money_text import (
    CASH_ACCOUNT,
    COMPANY,
    COST_CENTER,
    CURRENCY_WORD_RE as _CURRENCY_WORD_RE,
    detect_currency,
    find_money_match as _find_money_match,
    has_dev_marker,
    is_question,
    looks_like_money_in,
    norm as _norm,
    parse_amount,
    today_local,
)

INCOME_SERVICIOS = "Servicios - J"
INCOME_VENTAS = "Ventas - J"
INCOME_INTERESES = "Interest Income - J"
INCOME_INTERES_PLAZO_FIJO = "Interest on Fixed Deposits - J"

_COMMAND_PREFIX_RE = re.compile(
    r"^\s*(?:por\s+favor\s+)?(?:registra(?:r|me)?|registrar|anota(?:r|me)?|anotar|"
    r"apunta(?:r|me)?|apuntar|carga(?:r)?|contabiliza(?:r|me)?|me\s+pagaron|"
    r"nos\s+pagaron|pagaron|cobr[ée]|recib[ií]|depositaron|transfirieron)\b[\s:,]*",
    flags=re.IGNORECASE,
)
_INCOME_FILLER_RE = re.compile(
    r"\b(ingresos?\s*(?:de|del|por)?|por\s+concepto|concepto|correspondiente|"
    r"me|nos|un|una|de|del?|la|el|los|las|en|por|favor|mi|mis|este|esta|mes|ya|que)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedIncome:
    """Ingreso interpretado desde lenguaje natural, listo para contabilizar."""

    concept: str
    amount: Decimal
    currency: str
    income_account: str
    company: str = COMPANY
    cost_center: str = COST_CENTER
    cash_account: str = CASH_ACCOUNT
    posting_date: str = ""
    remark: str = ""


def classify_income_account(concept: str) -> str:
    """Naturaleza economica -> cuenta de ingreso verificada. Sin inventar cuentas.

    Por defecto `Servicios - J`: los ingresos declarados del dueño son trabajo
    remoto y docencia, que son servicios, no venta de bienes.
    """
    blob = _norm(concept)
    if re.search(r"\b(plazo\s+fijo|deposito\s+fijo|fixed\s+deposit)\b", blob):
        return INCOME_INTERES_PLAZO_FIJO
    if re.search(r"\b(inter[eé]ses?|rendimientos?|dividendos?)\b", blob):
        return INCOME_INTERESES
    if re.search(r"\b(ventas?|vend\w+|productos?|mercader[ií]a|art[ií]culos?)\b", blob):
        return INCOME_VENTAS
    return INCOME_SERVICIOS


def extract_concept(text: str) -> str:
    """Lo que queda tras quitar orden, monto y relleno: 'sueldo', 'contrato'."""
    blob = _COMMAND_PREFIX_RE.sub("", str(text or "").strip())
    match = _find_money_match(blob)
    if match is not None:
        blob = (blob[: match.start()] + " " + blob[match.end():]).strip()
    blob = _CURRENCY_WORD_RE.sub(" ", blob)
    blob = _INCOME_FILLER_RE.sub(" ", blob)
    blob = re.sub(r"\s+", " ", blob).strip(" ,.:;")
    return blob or "ingreso personal"


def looks_like_income(text: str) -> bool:
    """Detector determinista de ingreso: dinero que entra + monto, sin marcas dev.

    Exige monto para poder registrar sin preguntar, y descarta preguntas:
    "¿cuándo me pagaron los 500 dólares?" no debe contabilizar nada.
    """
    blob = str(text or "").strip()
    if not blob or has_dev_marker(blob) or is_question(blob):
        return False
    if not looks_like_money_in(blob):
        return False
    return _find_money_match(blob) is not None


def parse_income(text: str) -> ParsedIncome | None:
    """Interpreta un ingreso ocasional. None si falta monto valido."""
    blob = str(text or "").strip()
    if not blob:
        return None
    match = _find_money_match(blob)
    if match is None:
        return None
    amount = parse_amount(match.group("num"))
    if amount is None:
        return None
    currency = detect_currency(blob, match.start(), match.end())
    concept = extract_concept(blob)
    account = classify_income_account(concept)
    today = today_local()
    remark = f"Ingreso por voz: {concept} - {amount:.2f} {currency} ({today})"
    return ParsedIncome(
        concept=concept,
        amount=amount,
        currency=currency,
        income_account=account,
        posting_date=today,
        remark=remark,
    )


def new_openceo_ref() -> str:
    return _new_ref("voice-income")


def build_journal_entry(parsed: ParsedIncome, ref: str) -> dict[str, Any]:
    """Payload cerrado del asiento. El LLM nunca arma esto a mano."""
    amount = f"{parsed.amount:.2f}"
    return {
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "company": parsed.company,
        "posting_date": parsed.posting_date,
        "openceo_ref": ref,
        "user_remark": parsed.remark,
        "accounts": [
            {
                "account": parsed.cash_account,
                "debit_in_account_currency": amount,
                "credit_in_account_currency": 0.0,
                "cost_center": parsed.cost_center,
            },
            {
                "account": parsed.income_account,
                "debit_in_account_currency": 0.0,
                "credit_in_account_currency": amount,
                "cost_center": parsed.cost_center,
            },
        ],
    }


async def register_income(
    client: Any,
    parsed: ParsedIncome,
    *,
    ref: str | None = None,
    submit: bool = True,
) -> dict[str, Any]:
    """Registra el ingreso en ERPNext. Idempotente por `ref` (ver `journal`)."""
    operation_ref = (ref or "").strip() or new_openceo_ref()
    return await submit_journal_entry(
        client,
        build_journal_entry(parsed, operation_ref),
        ref=operation_ref,
        submit=submit,
    )


def build_spoken_summary(parsed: ParsedIncome, doc: dict[str, Any], duplicate: bool) -> str:
    """Respuesta breve apta para TTS: sin tablas, UUIDs ni URLs."""
    name = str(doc.get("name") or "").strip()
    repeat = "Ya estaba registrado, no lo dupliqué. " if duplicate else ""
    seat = f" Asiento {name}." if name else ""
    return (
        f"{repeat}Listo, dejamos registrados {parsed.amount:.2f} "
        f"{parsed.currency} de {parsed.concept} como ingreso.{seat}"
    )
