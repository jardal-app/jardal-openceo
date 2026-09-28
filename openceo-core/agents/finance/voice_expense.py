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

No hay ledger paralelo: Mongo guarda como maximo la referencia devuelta.
`execute_money_movements=false` sigue vigente: esto contabiliza gasto ya
ocurrido, no mueve dinero externo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

# Bolivia no observa horario de verano: offset fijo, sin dependencia de tzdata.
_LA_PAZ = timezone(timedelta(hours=-4))

COMPANY = "Jardal"
COST_CENTER = "Principal - J"
CASH_ACCOUNT = "Efectivo - J"

EXPENSE_SERVICIOS_PUBLICOS = "Servicios públicos - J"
EXPENSE_GASTOS_VARIOS = "Gastos varios - J"
EXPENSE_TRANSPORTE = "CARGOS DE TRANSITO Y TRANSPORTE - J"
EXPENSE_TELEFONO = "Cuenta telefonica - J"

_AMOUNT_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"(?:\s*(?P<cur>bs\.?|bolivianos?|bob|usd|d[óo]lares?|dolar|usdt|\$))?",
    flags=re.IGNORECASE,
)
_CURRENCY_WORD_RE = re.compile(
    r"\b(bs\.?|bolivianos?|bob|usd|d[óo]lares?|dolar|usdt|\$)\b",
    flags=re.IGNORECASE,
)
# Cantidades de tiempo ("15 minutos de supervisión") no son dinero.
_TIME_UNIT_RE = re.compile(
    r"^\s*(minutos?|min\.?|horas?|hs?\.?)\b",
    flags=re.IGNORECASE,
)

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
_DEV_MARKER_RE = re.compile(
    r"\b(tickets?|presets?|sesi[óo]n(?:es)?\s+dev|git|commit|branch|merge|deploy|"
    r"tmux|codex|claude|pytest|tests?|c[óo]digo|refactor|workflow|horizonte|horizon)\b",
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


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _norm(text: str) -> str:
    return _strip_accents(str(text or "").lower())


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


def _find_money_match(text: str) -> re.Match[str] | None:
    """Primer monto que es dinero, no tiempo ('15 minutos' se salta)."""
    blob = str(text or "")
    for match in _AMOUNT_RE.finditer(blob):
        if _TIME_UNIT_RE.match(blob[match.end(): match.end() + 12]):
            continue
        return match
    return None


def detect_currency(text: str, amount_start: int, amount_end: int) -> str:
    """Moneda explicita cerca del monto; BOB por defecto. USDT != USD."""
    window = str(text or "")[max(0, amount_start - 12): amount_end + 14]
    match = _CURRENCY_WORD_RE.search(window)
    token = _norm(match.group(1)) if match else ""
    if token in {"usd", "dolar", "dolares", "$"}:
        return "USD"
    if token == "usdt":
        return "USDT"
    return "BOB"


def parse_amount(raw: str) -> Decimal | None:
    """Convierte '1.234,56'/'1,234.56'/'50' a Decimal. Nunca float."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    decimal_sep = "."
    if "," in cleaned and "." in cleaned:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
    elif "," in cleaned:
        decimal_sep = ","
    if decimal_sep == ",":
        normalized = cleaned.replace(".", "").replace(",", ".")
    else:
        normalized = cleaned.replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value


def classify_expense_account(concept: str) -> str:
    """Naturaleza economica -> Account verificada. Sin inventar cuentas."""
    blob = _norm(concept)
    if re.search(r"\b(luz|elfec|electricidad|electrica|energia|agua|servicios?\s+publicos?)\b", blob):
        return EXPENSE_SERVICIOS_PUBLICOS
    if re.search(r"\b(pasajes?|transporte|taxi|micro|trufi|colectivo)\b", blob):
        return EXPENSE_TRANSPORTE
    if re.search(r"\b(telefono|celular|recarga|entel|viva|tigo)\b", blob):
        return EXPENSE_TELEFONO
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
    """
    blob = str(text or "").strip()
    if not blob or _DEV_MARKER_RE.search(blob):
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
    today = datetime.now(_LA_PAZ).date().isoformat()
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
    """Identidad idempotente de la operacion. Reintento = mismo valor."""
    return f"voice-expense:{uuid4().hex}"


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


_JOURNAL_FIELDS = ["name", "docstatus", "openceo_ref", "total_debit", "posting_date"]


async def register_expense(
    client: Any,
    parsed: ParsedExpense,
    *,
    ref: str | None = None,
    submit: bool = True,
) -> dict[str, Any]:
    """Registra el gasto en ERPNext. Idempotente por `ref`.

    Crea el `Journal Entry` en borrador y lo envia (submit) para que
    llegue al libro mayor. Un reintento con el mismo `ref` choca con el
    indice unico (HTTP 417): se relee por `ref` y se devuelve como exito
    idempotente; si quedo en borrador, se envia.
    """
    operation_ref = (ref or "").strip() or new_openceo_ref()
    try:
        created = await client.create_doc(
            "Journal Entry", build_journal_entry(parsed, operation_ref)
        )
    except Exception as exc:
        if getattr(exc, "status_code", None) != 417:
            raise
        existing = await find_by_ref(client, operation_ref)
        if not existing:
            raise
        doc = await _ensure_submitted(client, existing, submit)
        return {"ok": True, "duplicate": True, "doc": doc, "ref": operation_ref}

    doc = await _ensure_submitted(client, created, submit)
    return {"ok": True, "duplicate": False, "doc": doc, "ref": operation_ref}


async def find_by_ref(client: Any, ref: str) -> dict[str, Any] | None:
    rows = await client.list_docs(
        "Journal Entry",
        filters=[["openceo_ref", "=", ref]],
        fields=_JOURNAL_FIELDS,
        limit=1,
    )
    return rows[0] if rows else None


async def _ensure_submitted(client: Any, doc: dict[str, Any], submit: bool) -> dict[str, Any]:
    if not submit:
        return doc
    if int(doc.get("docstatus") or 0) != 0:
        return doc
    name = str(doc.get("name") or "").strip()
    if not name:
        return doc
    return await client.update_doc("Journal Entry", name, {"docstatus": 1})


def build_spoken_summary(parsed: ParsedExpense, doc: dict[str, Any], duplicate: bool) -> str:
    """Respuesta breve apta para TTS: sin tablas, UUIDs ni URLs."""
    name = str(doc.get("name") or "").strip()
    repeat = "Ya estaba registrado, no lo dupliqué. " if duplicate else ""
    seat = f" Asiento {name}." if name else ""
    return (
        f"{repeat}Listo, dejamos registrados {parsed.amount:.2f} "
        f"{parsed.currency} de {parsed.concept} en gastos personales.{seat}"
    )
