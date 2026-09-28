"""Primitivas deterministas para leer dinero en lenguaje natural. Cero logica LLM.

Un solo lugar donde un monto escrito a mano se convierte en `Decimal` (nunca
`float`, contrato `docs/finance-erpnext-integration-contract.md` §11) y donde
la moneda explicita cerca del monto decide BOB / USD / USDT (USDT no es USD).
Lo comparten los flujos de gasto e ingreso de Finance: dos modulos que
dupliquen este parseo divergen en cuanto alguien ajusta un separador decimal.

Tambien vive aqui el vocabulario que separa un ingreso de un gasto, porque
los verbos se solapan ("registra ingreso 500 bs" cumple el detector de gasto)
y esa frontera debe ser una sola, no dos decisiones independientes.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

# Bolivia no observa horario de verano: offset fijo, sin dependencia de tzdata.
LA_PAZ = timezone(timedelta(hours=-4))

COMPANY = "Jardal"
COST_CENTER = "Principal - J"
CASH_ACCOUNT = "Efectivo - J"

AMOUNT_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    # `usdt` va antes que `usd`: son alternativas ordenadas y si `usd` gana
    # primero, de "100 usdt" queda una "t" suelta pegada al concepto.
    r"(?:\s*(?P<cur>bs\.?|bolivianos?|bob|usdt|usd|d[óo]lares?|dolar|\$))?",
    flags=re.IGNORECASE,
)
CURRENCY_WORD_RE = re.compile(
    r"\b(bs\.?|bolivianos?|bob|usdt|usd|d[óo]lares?|dolar|\$)\b",
    flags=re.IGNORECASE,
)
# Cantidades de tiempo ("15 minutos de supervisión") no son dinero.
TIME_UNIT_RE = re.compile(
    r"^\s*(minutos?|min\.?|horas?|hs?\.?)\b",
    flags=re.IGNORECASE,
)
DEV_MARKER_RE = re.compile(
    r"\b(tickets?|presets?|sesi[óo]n(?:es)?\s+dev|git|commit|branch|merge|deploy|"
    r"tmux|codex|claude|pytest|tests?|c[óo]digo|refactor|workflow|horizonte|horizon)\b",
    flags=re.IGNORECASE,
)
# Señales de dinero que ENTRA, en dos niveles. La distinción no es estética:
# "pago de sueldo 3000 bs" y "pago de intereses" son GASTOS que contienen un
# sustantivo de ingreso, y "me pagaron el sueldo 6200 bs" es un ingreso que
# contiene la palabra "pago". El signo del asiento depende de acertar aqui.
INCOME_STRONG_RE = re.compile(
    r"\b(ingresos?|me\s+pagaron|nos\s+pagaron|pagaron|depositaron|transfirieron|"
    r"cobr(?:e|é|ado|amos|o)|recib(?:i|í|ido|imos)|vend(?:i|í|emos))\b",
    flags=re.IGNORECASE,
)
INCOME_NOUN_RE = re.compile(
    r"\b(sueldos?|salarios?|honorarios?|docencia|consultor[íi]a|freelance|remoto|"
    r"dep[óo]sito|venta\s+de)\b",
    flags=re.IGNORECASE,
)
# Orden de dinero que SALE: anula un sustantivo de ingreso suelto.
MONEY_OUT_LEAD_RE = re.compile(
    r"\b(pagos?|pagar|pagu[ée]|compras?|compr[ée]|gastos?|gast[ée]|deudas?|"
    r"pr[ée]stamos?|cuotas?|facturas?|aportes?|diezmos?)\b",
    flags=re.IGNORECASE,
)
_QUESTION_MARK_RE = re.compile(r"[¿?]")
_INTERROGATIVE_RE = re.compile(
    r"\b(cu[áa]ndo|cu[áa]nt[oa]s?|cu[áa]l(?:es)?|c[óo]mo|por\s+qu[ée]|d[óo]nde)\b",
    flags=re.IGNORECASE,
)


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def norm(text: str) -> str:
    return strip_accents(str(text or "").lower())


def has_dev_marker(text: str) -> bool:
    """Tráfico de desarrollo (tickets, git, sesiones): nunca es finanzas."""
    return bool(DEV_MARKER_RE.search(str(text or "")))


def is_question(text: str) -> bool:
    """Una pregunta no es una orden de registro.

    Solo la usan los detectores de ESCRITURA: preguntar "¿cuándo me pagaron
    los 500 dólares?" no debe contabilizar nada, y las lecturas (saldo, regla
    del 10%) siguen atendiendo preguntas porque ese es justamente su trabajo.
    """
    blob = str(text or "").strip()
    if _QUESTION_MARK_RE.search(blob):
        return True
    return bool(_INTERROGATIVE_RE.search(blob))


def looks_like_money_in(text: str) -> bool:
    """Dinero que entra: señal fuerte, o sustantivo de ingreso sin orden de pago.

    La palabra "ingreso" y los verbos de cobro son siempre señal fuerte. Un
    sustantivo suelto ("sueldo 6200 bolivianos") también cuenta, salvo que la
    frase pida pagar algo ("pago de sueldo 3000 bs"), que es un gasto.
    """
    blob = str(text or "")
    if INCOME_STRONG_RE.search(blob):
        return True
    if not INCOME_NOUN_RE.search(blob):
        return False
    return not MONEY_OUT_LEAD_RE.search(blob)



def today_local() -> str:
    return datetime.now(LA_PAZ).date().isoformat()


def now_local_iso() -> str:
    return datetime.now(LA_PAZ).isoformat(timespec="seconds")


def find_money_match(text: str) -> re.Match[str] | None:
    """Primer monto que es dinero, no tiempo ('15 minutos' se salta)."""
    blob = str(text or "")
    for match in AMOUNT_RE.finditer(blob):
        if TIME_UNIT_RE.match(blob[match.end(): match.end() + 12]):
            continue
        return match
    return None


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


def detect_currency(text: str, amount_start: int, amount_end: int) -> str:
    """Moneda explicita cerca del monto; BOB por defecto. USDT != USD."""
    window = str(text or "")[max(0, amount_start - 12): amount_end + 14]
    match = CURRENCY_WORD_RE.search(window)
    token = norm(match.group(1)) if match else ""
    if token in {"usd", "dolar", "dolares", "$"}:
        return "USD"
    if token == "usdt":
        return "USDT"
    return "BOB"
