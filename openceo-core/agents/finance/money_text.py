"""Primitivas deterministas para leer dinero en lenguaje natural. Cero logica LLM.

Un solo lugar donde un monto escrito a mano se convierte en `Decimal` (nunca
`float`, contrato `docs/finance-jardal-erp-integration-contract.md` §11) y donde
la moneda explicita cerca del monto decide BOB / USD / USDT (USDT no es USD).
Lo comparten los flujos de gasto e ingreso de Finance: dos modulos que
dupliquen este parseo divergen en cuanto alguien ajusta un separador decimal.

Tambien vive aqui el vocabulario que separa un ingreso de un gasto, porque
los verbos se solapan ("registra ingreso 500 bs" cumple el detector de gasto)
y esa frontera debe ser una sola, no dos decisiones independientes. Esa
frontera decide el SIGNO del asiento, asi que exige una senal de cobro real y
no vocabulario suelto: "pagaron la luz 200 bs" es un gasto, no un ingreso.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Bolivia no observa horario de verano: offset fijo, sin dependencia de tzdata.
LA_PAZ = timezone(timedelta(hours=-4))

COMPANY = "Jardal"
# Moneda de la Company: la unica que se puede contabilizar sin tasa real
# (contrato §11: "conservar la tasa realmente usada por la transaccion").
COMPANY_CURRENCY = "BOB"
COST_CENTER = "Principal - J"
CASH_ACCOUNT = "Efectivo - J"


class MissingExchangeRateError(ValueError):
    """Movimiento en moneda distinta a la de la Company sin tasa confirmada.

    El contrato §11 prohibe inventar la tasa. Si el dictado esta en USD/USDT y
    no hay tasa real, lo unico seguro es no contabilizar nada y pedirla: un
    asiento por el mismo numero en BOB cambia el significado del dinero.
    """

    def __init__(self, currency: str) -> None:
        self.currency = str(currency or "").upper()
        super().__init__(
            f"El movimiento esta en {self.currency} y no hay una tasa de cambio "
            f"confirmada. No contabilice nada para no inventar el importe: "
            f"indique la tasa o registre el monto en {COMPANY_CURRENCY}."
        )


AMOUNT_RE = re.compile(
    r"(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    # `usdt` va antes que `usd`: son alternativas ordenadas y si `usd` gana
    # primero, de "100 usdt" queda una "t" suelta pegada al concepto.
    r"(?:\s*(?P<cur>bs\.?|bolivianos?|bob|usdt|usd|d[óo]lares?|dolar|\$))?",
    flags=re.IGNORECASE,
)
# `$` va fuera de `\b...\b`: un signo de dólar no es caracter de palabra, así
# que dentro del grupo era inalcanzable y "$500" se leía como BOB.
CURRENCY_WORD_RE = re.compile(
    r"\$|\b(bs\.?|bolivianos?|bob|usdt|usd|d[óo]lares?|dolar)\b",
    flags=re.IGNORECASE,
)
_CURRENCY_TOKEN = CURRENCY_WORD_RE.pattern
# Moneda PEGADA al monto (antes o despues), no en cualquier parte de la frase:
# con una ventana ancha, "3 pagos de 500 bolivianos" hacia que el "3" contara
# como importe con moneda.
_CURRENCY_AFTER_RE = re.compile(r"\s*(" + _CURRENCY_TOKEN + r")", re.IGNORECASE)
_CURRENCY_BEFORE_RE = re.compile(r"(" + _CURRENCY_TOKEN + r")\s*$", re.IGNORECASE)
# Cantidades de tiempo ("15 minutos de supervisión") no son dinero.
TIME_UNIT_RE = re.compile(
    r"^\s*(minutos?|min\.?|horas?|hs?\.?)\b",
    flags=re.IGNORECASE,
)
# "10 %" es una tasa, no un monto: el 10% de una comision se pagaba como 10.00.
_PERCENT_TAIL_RE = re.compile(r"^\s*(?:%|por\s*ciento)", flags=re.IGNORECASE)
DEV_MARKER_RE = re.compile(
    r"\b(tickets?|presets?|sesi[óo]n(?:es)?\s+dev|git|commit|branch|merge|deploy|"
    r"tmux|codex|claude|pytest|tests?|c[óo]digo|refactor|workflow|horizonte|horizon)\b",
    flags=re.IGNORECASE,
)
# Señales de dinero que ENTRA. La distinción no es estética: el signo del
# asiento depende de acertar aqui. Solo cuentan el cobro en primera persona
# ("me pagaron", "cobré", "recibí"), la palabra "ingreso" y un sustantivo de
# ingreso que etiqueta un monto. "pagaron" desnudo NO es señal: "pagaron la luz
# 200 bs" es dinero que SALE.
_RECEIPT_VERB_RE = re.compile(
    r"\b(me\s+pagaron|nos\s+pagaron|me\s+pag[oó]|nos\s+pag[oó]|me\s+depositaron|"
    r"nos\s+depositaron|me\s+transfirieron|nos\s+transfirieron|cobr[eé]|cobramos|"
    r"cobrado|recib[ií]|recibimos|recibido|vend[ií]|vendimos)\b",
    flags=re.IGNORECASE,
)
_INCOME_WORD_RE = re.compile(r"\b(ingresos?)\b", flags=re.IGNORECASE)
INCOME_NOUN_RE = re.compile(
    r"\b(sueldos?|salarios?|honorarios?|docencia|consultor[íi]a|freelance|remoto|"
    r"dep[óo]sito|venta\s+de)\b",
    flags=re.IGNORECASE,
)
# Sustantivo de ingreso ETIQUETANDO un monto: "sueldo 6200 bolivianos". Un
# complemento relativo ("sueldo del empleado", "honorarios del contador",
# "deposito de garantia", "coworking remoto") señala a un tercero o a un viaje:
# es dinero que sale. La estructura, no la lista de palabras, decide.
_INCOME_NOUN_AMOUNT_RE = re.compile(
    r"\b(?:sueldos?|salarios?|honorarios?|docencia|consultor[íi]a|freelance|"
    r"dep[óo]sito|venta\s+de)\b\s*(?:de\s+|del\s+)?(?=\d)",
    flags=re.IGNORECASE,
)
# Orden de dinero que SALE: anula un sustantivo de ingreso suelto.
MONEY_OUT_LEAD_RE = re.compile(
    r"\b(pagos?|pagar|pagu[ée]|compras?|compr[ée]|gastos?|gast[ée]|deudas?|"
    r"pr[ée]stamos?|cuotas?|facturas?|aportes?|diezmos?)\b",
    flags=re.IGNORECASE,
)
_QUESTION_MARK_RE = re.compile(r"[¿?]")
# Solo cuenta como pregunta si el interrogativo ABRE la frase (opcionalmente
# tras un preambulo de cortesia). Con `\b(como|donde|...)\b` en cualquier
# posición, "registra como gasto 50 bs" se descartaba: la voz no trae tildes y
# "como"/"donde" son la forma por defecto de "en calidad de"/"en casa de".
_QUESTION_LEAD_RE = re.compile(
    r"^\s*(?:por\s+favor[,\s]*|dime[,\s]*|necesito\s+saber[,\s]*|"
    r"quiero\s+saber[,\s]*)?"
    r"(cu[áa]ndo|cu[áa]nt[oa]s?|cu[áa]l(?:es)?|c[óo]mo|por\s+qu[ée]|d[óo]nde)\b",
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
    return bool(_QUESTION_LEAD_RE.match(blob))


def looks_like_money_in(text: str) -> bool:
    """Dinero que entra: cobro explicito, "ingreso", o sustantivo con monto.

    La palabra "ingreso" y los cobros en primera persona son señal fuerte. Un
    sustantivo suelto cuenta solo cuando etiqueta un monto ("sueldo 6200
    bolivianos"), no cuando lleva complemento ("sueldo del empleado") ni cuando
    la frase trae una orden de pago ("pago de sueldo 3000 bs"), que es un gasto.
    """
    blob = str(text or "")
    if _INCOME_WORD_RE.search(blob) or _RECEIPT_VERB_RE.search(blob):
        return True
    if not _INCOME_NOUN_AMOUNT_RE.search(blob):
        return False
    return not MONEY_OUT_LEAD_RE.search(blob)


def today_local() -> str:
    return datetime.now(LA_PAZ).date().isoformat()


def now_local_iso() -> str:
    return datetime.now(LA_PAZ).isoformat(timespec="seconds")


def _money_candidates(text: str) -> list[re.Match[str]]:
    """Montos que son dinero: ni tiempo ("15 minutos") ni tasas ("10 %")."""
    blob = str(text or "")
    found: list[re.Match[str]] = []
    for match in AMOUNT_RE.finditer(blob):
        tail = blob[match.end(): match.end() + 12]
        if TIME_UNIT_RE.match(tail) or _PERCENT_TAIL_RE.match(tail):
            continue
        found.append(match)
    return found


def find_money_match(text: str) -> re.Match[str] | None:
    """Primer monto que es dinero, no tiempo ('15 minutos') ni tasa ('10 %')."""
    candidates = _money_candidates(text)
    return candidates[0] if candidates else None


def _currency_near(text: str, start: int, end: int) -> str | None:
    """Token de moneda pegado al monto, antes o despues. None si no hay."""
    blob = str(text or "")
    after = _CURRENCY_AFTER_RE.match(blob[end: end + 16])
    if after:
        return after.group(1)
    # `AMOUNT_RE` puede haber consumido el token de moneda dentro del propio
    # match ("100 usdt"): se revisa el tramo capturado antes de ampliar.
    inside = CURRENCY_WORD_RE.search(blob[start:end])
    if inside:
        return inside.group(0)
    before = _CURRENCY_BEFORE_RE.search(blob[max(0, start - 16): start])
    if before:
        return before.group(1)
    return None


def select_money_match(text: str) -> re.Match[str] | None:
    """Monto a contabilizar cuando la frase trae varios numeros.

    Un conteo ("3 pagos de 500 bolivianos"), un numero de factura o una fecha
    no son el importe. Si hay un solo candidato se usa; si hay varios, el que
    lleva moneda pegada. Si ninguno la lleva, la frase es ambigua y se devuelve
    None para no escribir una cifra inventada (fail-closed).
    """
    candidates = _money_candidates(text)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    blob = str(text or "")
    for match in candidates:
        if _currency_near(blob, match.start(), match.end()):
            return match
    return None


def _looks_like_decimal(cleaned: str, sep: str) -> bool:
    """True si `sep` actua como decimal, no como separador de miles.

    "6.200"/"1,234" (un separador, tres digitos detras) son miles en la
    convencion boliviana; "6.20"/"1,5" son decimales.
    """
    if cleaned.count(sep) != 1:
        return False
    head, _, tail = cleaned.partition(sep)
    if not head:
        return False
    return len(tail) != 3


def parse_amount(raw: str) -> Decimal | None:
    """Convierte '1.234,56'/'1,234.56'/'6.200'/'50' a Decimal. Nunca float.

    En Bolivia el punto separa miles y la coma es decimal: "6.200" son seis mil
    doscientos, no seis con veinte (1000x menos en el libro mayor). Con los dos
    separadores presentes, el ultimo es el decimal.
    """
    cleaned = (raw or "").strip()
    if not cleaned or not re.fullmatch(r"\d[\d.,]*", cleaned):
        return None
    has_dot = "." in cleaned
    has_comma = "," in cleaned
    decimal_sep: str | None = None
    if has_dot and has_comma:
        decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
    elif has_dot and _looks_like_decimal(cleaned, "."):
        decimal_sep = "."
    elif has_comma and _looks_like_decimal(cleaned, ","):
        decimal_sep = ","
    if decimal_sep == ",":
        normalized = cleaned.replace(".", "").replace(",", ".")
    elif decimal_sep == ".":
        normalized = cleaned.replace(",", "")
    else:  # solo separadores de miles
        normalized = cleaned.replace(".", "").replace(",", "")
    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value


def detect_currency(text: str, amount_start: int, amount_end: int) -> str:
    """Moneda explicita PEGADA al monto; BOB por defecto. USDT != USD."""
    token = norm(_currency_near(text, amount_start, amount_end) or "")
    if token in {"usd", "dolar", "dolares", "$"}:
        return "USD"
    if token == "usdt":
        return "USDT"
    return "BOB"


def company_amount(
    amount: Decimal,
    currency: str,
    exchange_rate: Decimal | None = None,
) -> Decimal:
    """Importe en la moneda de la Company. Sin tasa real no hay conversion.

    Un monto en USD/USDT no puede entrar al libro como el mismo numero en BOB:
    o hay tasa confirmada y se convierte, o se levanta `MissingExchangeRateError`
    y no se contabiliza nada (contrato §11).
    """
    code = str(currency or COMPANY_CURRENCY).strip().upper()
    if code == COMPANY_CURRENCY:
        return amount
    if exchange_rate is None:
        raise MissingExchangeRateError(code)
    rate = Decimal(exchange_rate)
    return (amount * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
