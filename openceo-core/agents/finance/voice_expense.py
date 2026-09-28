"""Registro ocasional de gastos por voz/chat hacia ERPNext. Cero logica LLM.

Flujo Fase B del contrato (`docs/finance-erpnext-integration-contract.md`):
un solo flujo real end-to-end, `finance.register_expense` sobre el unico
DocType que este flujo crea: `Journal Entry` (que ya trae `openceo_ref`
unico y verificado: reintento con el mismo ref devuelve HTTP 417).

Cada gasto ocasional ("registra gasto personal carne 50 bolivianos",
"pago de luz de casa 300 bs") se registra como asiento de diario
debito a gasto / credito a efectivo, en BOB salvo moneda explicita,
con `Decimal` (nunca `float`), Company `Jardal` y Cost Center
`Principal - J`. No crea tickets: el registro es el efecto.

El parseo de dinero vive en `money_text` y la idempotencia del asiento en
`journal`, compartidos con el flujo de ingreso. Este modulo aporta solo lo
propio del gasto: su detector, la clasificacion a cuentas de gasto reales y
el concepto.

No hay ledger paralelo: Mongo guarda como maximo la referencia devuelta.
`execute_money_movements=false` sigue vigente: esto contabiliza gasto ya
ocurrido, no mueve dinero externo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from agents.finance.journal import new_openceo_ref as _new_ref
from agents.finance.journal import submit_journal_entry
from agents.finance.money_text import (  # noqa: F401  (reexport publico)
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
    strip_accents as _strip_accents,
    today_local,
)

EXPENSE_SERVICIOS_PUBLICOS = "Servicios públicos - J"
EXPENSE_GASTOS_VARIOS = "Gastos varios - J"
EXPENSE_TRANSPORTE = "CARGOS DE TRANSITO Y TRANSPORTE - J"
EXPENSE_TELEFONO = "Cuenta telefonica - J"
EXPENSE_SALARIOS = "Salario. - J"

_FINANCE_VERB_RE = re.compile(
    r"\b(registr\w*|anot\w*|apunt\w*|carg\w*|pag\w*|compr\w*|gast\w*)\b",
    flags=re.IGNORECASE,
)
_FINANCE_NOUN_RE = re.compile(
    r"\b(gastos?|facturas?|recibos?|cuentas?|luz|elfec|electricidad|el[ée]ctrica|"
    r"energ[íi]a|agua|alquiler|carne|mercado|comida|alimentos?|hogar|tienda|"
    r"supermercado|farmacia|pasajes?|transporte|taxi|tel[ée]fono|celular|recarga)\b",
    flags=re.IGNORECASE,
)

_COMMAND_PREFIX_RE = re.compile(
    r"^\s*(?:por\s+favor\s+)?(?:registra(?:me)?|registrar|anota(?:me)?|anotar|"
    r"apunta(?:me)?|apuntar|apuntame|carga|cargar)\b[\s:,]*",
    flags=re.IGNORECASE,
)
_EXPENSE_FILLER_RE = re.compile(
    r"\b(gastos?\s+personales?|gastos?|pago\s+de|pago|pagar|pagu[ée]|de|del?|la|el|en|un|una|por|favor|mi|mis)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedExpense:
    """Gasto interpretado desde lenguaje natural, listo para contabilizar."""

    concept: str
    amount: Decimal
    currency: str
    expense_account: str
    company: str = COMPANY
    cost_center: str = COST_CENTER
    cash_account: str = CASH_ACCOUNT
    posting_date: str = ""
    remark: str = ""


def classify_expense_account(concept: str) -> str:
    """Naturaleza economica -> Account verificada. Sin inventar cuentas."""
    blob = _norm(concept)
    if re.search(r"\b(luz|elfec|electricidad|electrica|energia|agua|servicios?\s+publicos?)\b", blob):
        return EXPENSE_SERVICIOS_PUBLICOS
    if re.search(r"\b(pasajes?|transporte|taxi|micro|trufi|colectivo)\b", blob):
        return EXPENSE_TRANSPORTE
    if re.search(r"\b(telefono|celular|recarga|entel|viva|tigo)\b", blob):
        return EXPENSE_TELEFONO
    if re.search(r"\b(sueldos?|salarios?|nomina|planilla)\b", blob):
        return EXPENSE_SALARIOS
    return EXPENSE_GASTOS_VARIOS


def extract_concept(text: str) -> str:
    """Lo que queda tras quitar orden, monto y relleno: 'carne', 'luz casa'."""
    blob = _COMMAND_PREFIX_RE.sub("", str(text or "").strip())
    match = _find_money_match(blob)
    if match is not None:
        blob = (blob[: match.start()] + " " + blob[match.end():]).strip()
    blob = _CURRENCY_WORD_RE.sub(" ", blob)
    blob = _EXPENSE_FILLER_RE.sub(" ", blob)
    blob = re.sub(r"\s+", " ", blob).strip(" ,.:;")
    return blob or "gasto personal"


def looks_like_expense(text: str) -> bool:
    """Detector determinista: monto + senal financiera, sin marcas dev.

    Exige monto para poder registrar sin preguntar. Sin monto no hay
    registro posible, asi que no se reclama la frase.

    Dos exclusiones protegen el signo del asiento: una frase de dinero que
    ENTRA ("registra ingreso 500 bs" cumple el verbo `registr*` + monto) no es
    un gasto, y una pregunta no es una orden de registro.
    """
    blob = str(text or "").strip()
    if not blob or has_dev_marker(blob) or is_question(blob) or looks_like_money_in(blob):
        return False
    amount_match = _find_money_match(blob)
    if amount_match is None:
        return False
    if _CURRENCY_WORD_RE.search(blob):
        return True
    return bool(_FINANCE_VERB_RE.search(blob) or _FINANCE_NOUN_RE.search(blob))


def parse_expense(text: str) -> ParsedExpense | None:
    """Interpreta un gasto ocasional. None si falta monto valido."""
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
    account = classify_expense_account(concept)
    today = today_local()
    remark = f"Registro por voz: {concept} - {amount:.2f} {currency} ({today})"
    return ParsedExpense(
        concept=concept,
        amount=amount,
        currency=currency,
        expense_account=account,
        posting_date=today,
        remark=remark,
    )


def new_openceo_ref() -> str:
    return _new_ref("voice-expense")


def build_journal_entry(parsed: ParsedExpense, ref: str) -> dict[str, Any]:
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
                "account": parsed.expense_account,
                "debit_in_account_currency": amount,
                "credit_in_account_currency": 0.0,
                "cost_center": parsed.cost_center,
            },
            {
                "account": parsed.cash_account,
                "debit_in_account_currency": 0.0,
                "credit_in_account_currency": amount,
                "cost_center": parsed.cost_center,
            },
        ],
    }


async def register_expense(
    client: Any,
    parsed: ParsedExpense,
    *,
    ref: str | None = None,
    submit: bool = True,
) -> dict[str, Any]:
    """Registra el gasto en ERPNext. Idempotente por `ref` (ver `journal`)."""
    operation_ref = (ref or "").strip() or new_openceo_ref()
    return await submit_journal_entry(
        client,
        build_journal_entry(parsed, operation_ref),
        ref=operation_ref,
        submit=submit,
    )


def build_spoken_summary(parsed: ParsedExpense, doc: dict[str, Any], duplicate: bool) -> str:
    """Respuesta breve apta para TTS: sin tablas, UUIDs ni URLs."""
    name = str(doc.get("name") or "").strip()
    repeat = "Ya estaba registrado, no lo dupliqué. " if duplicate else ""
    seat = f" Asiento {name}." if name else ""
    return (
        f"{repeat}Listo, dejamos registrados {parsed.amount:.2f} "
        f"{parsed.currency} de {parsed.concept} en gastos personales.{seat}"
    )
