"""Clasificador determinista de Horizon, previo al LLM.

Hay dos razones para que esto exista y sea previo a la llamada al modelo:

1. **Seguridad de enrutamiento.** Con una sesion dev activa, el prompt de la
   primera capa trata cualquier frase como ``dev_instruction`` y la escribe en
   tmux. Decir "el cliente me pidio otro cambio" es una nota personal, no una
   instruccion de codigo. La seccion 9.2 punto 2 exige que una clasificacion
   ambigua NO inicie sesiones de codigo ni envie notas personales a tmux.

2. **Continuidad sin IA.** La seccion 9.7 exige que con el presupuesto de LLM
   agotado sigan funcionando el registro estructurado, los calculos y la
   consulta del ultimo plan valido. Una captura tiene que funcionar sin modelo.

Regla de precedencia: un verbo explicito de registro ("anota", "apunta",
"recorda", "pendiente:") GANA sobre cualquier marcador tecnico, porque el
usuario declaro su intencion de forma inequivoca. El resto de frases cede ante
los marcadores tecnicos para no robarle trabajo al flujo dev.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone

from horizon.models import CaptureKind

logger = logging.getLogger(__name__)

# Intents que este clasificador puede devolver.
INTENT_CAPTURE = "horizon_capture"
INTENT_NEXT_ACTION = "horizon_next_action"
INTENT_PROGRESS = "horizon_progress"
INTENT_PLAN_QUERY = "horizon_plan_query"
# Fase 2: ciclo proactivo.
INTENT_CAPACITY = "horizon_capacity"
INTENT_PLAN_DAY = "horizon_plan_day"
INTENT_REVIEW = "horizon_review"
INTENT_IMPORT = "horizon_import"
# Fase 3: delegacion y dev movil.
INTENT_DELEGATE = "horizon_delegate"
INTENT_DELIVERY = "horizon_delivery"
INTENT_METRICS = "horizon_metrics"
# F5: cockpit del dueno (TASK-2026-00120).
INTENT_BRIEF = "horizon_brief"
INTENT_DECISIONS = "horizon_decisions"
INTENT_DECIDE = "horizon_decide"

# Verbos de registro explicito: precedencia maxima.
_CAPTURE_PREFIX_PATTERNS = (
    r"^\s*(?:anota|anotar|apunta|apuntar|apunt[aá]me|registra|registrar|guarda|guardar)\b",
    r"^\s*recuerd[aá](?:me)?\b",
    r"^\s*record[aá](?:me)?\b",
    r"^\s*(?:idea|pendiente|compromiso|nota)\s*:",
    r"^\s*(?:se me ocurri[oó]|se me vino a la cabeza)\b",
)

# Frases que implican captura pero no de forma explicita.
_CAPTURE_SOFT_PATTERNS = (
    r"\bquiero (?:hacer|construir|armar|empezar|lanzar|aprender)\b",
    r"\b(?:el|un|mi) cliente (?:me )?(?:pidi[oó]|solicit[oó]|pide|solicita)\b",
    r"\bme (?:pidieron|solicitaron|encargaron)\b",
    r"\b(?:tengo|hay) (?:que|que) (?:hacer|resolver|ver|revisar) (?:el|la|un)\b.*\b(?:contrato|deuda|banco|cliente|familia|moto|casa)\b",
    r"\btengo (?:una|otra) idea\b",
)

# Avances: verbos de cierre/movimiento con o sin metrica.
_PROGRESS_PATTERNS = (
    r"\b(?:grabe|grab[eé]|termin[eé]|termin[eé] de|complet[eé]|avanc[eé]|hice|entregu[eé]|cerr[eé]|pag[ué])\b",
    r"\bya (?:est[aá]|qued[oó]|sali[oó]|entregu[eé]|termin[eé]|pagu[eé])\b",
    r"\bcu[aá]nt[oa]s? (?:minutos|horas|audio)\b.*\b(?:grab|util)",
    r"\bme (?:pagaron|depositaron|cobr[eé])\b",
)

# Consultas de direccion.
_NEXT_ACTION_PATTERNS = (
    r"\bqu[eé] (?:sigue|sigo|hago ahora|hacemos ahora|toca ahora|toca)\b",
    r"\bsiguiente (?:paso|acci[oó]n|bloque)\b",
    r"\bcu[aá]l es (?:la )?(?:siguiente )?(?:acci[oó]n|prioridad|paso)\b",
    r"\ben qu[eé] me (?:concentro|enfoco)\b",
    r"\bpor d[oó]nde (?:empiezo|arranco)\b",
)

_PLAN_QUERY_PATTERNS = (
    r"\bc[oó]mo va (?:el )?(?:plan|la misi[oó]n|el objetivo)\b",
    r"\bqu[eé] (?:plan|objetivos?) tenemos\b",
    r"\b(?:muestra|mu[eé]strame|dame|lista|cu[aá]les son) (?:el |los |mis )?(?:plan|objetivos?|misi[oó]n)\b",
    r"\bcu[aá]l es (?:la )?misi[oó]n\b",
    r"\bmi plan (?:de hoy|del d[ií]a)\b",
    r"\bqu[eé] (?:tengo|tenemos) (?:hoy|para hoy)\b",
    r"\bc[oó]mo vamos con\b",
)

# Marcadores tecnicos: si aparecen, la frase pertenece al flujo dev salvo que
# exista un verbo de registro explicito.
_DEV_MARKERS = (
    r"\btickets?\b",
    r"\bpresets?\b",
    r"\bsesi[oó]n(?:es)? dev\b",
    r"\bsesion dev\b",
    r"\bgit\b",
    r"\bcommit\b",
    r"\bbranch\b",
    r"\bmerge\b",
    r"\bdeploy\b",
    r"\btmux\b",
    r"\bcodex\b",
    r"\bclaude\b",
    r"\bpytest\b",
    r"\btest(?:s)?\b",
    r"\bcompil(?:a|ar|acion)\b",
    r"\bc[oó]digo\b",
    r"\brefactor\b",
    r"\bpr(?:ueba)?s? unitari",
)

# Palabras que confirman contexto personal y desempatan a favor de Horizon.
_PERSONAL_MARKERS = (
    r"\bmi familia\b",
    r"\bmis? padr(?:e|es)\b",
    r"\bmi hermana\b",
    r"\bmi hermano\b",
    r"\bmis? hij",
    r"\bgimnasio\b",
    r"\bejercicio\b",
    r"\bdescanso\b",
    r"\bsalud\b",
    r"\bmi voz\b",
    r"\bgrabaci[oó]n\b",
    r"\bentrevista\b",
    r"\bcv\b",
    r"\bcurr[ií]culum\b",
    r"\bdiezmo\b",
    r"\bpresupuesto\b",
    r"\bdeuda\b",
    r"\bbanco\b",
    r"\bempleo\b",
    r"\bmoto\b",
    r"\bcomida\b",
    r"\bcasa\b",
    # Trabajo propio y vida diaria: la seccion 9.3 nombra explicitamente
    # "el cliente me pidio otro cambio" como una captura personal, no como una
    # instruccion de codigo. La guarda contra frases dev es ``not dev``.
    r"\bclientes?\b",
    r"\bcontrato\b",
    r"\bcobro\b",
    r"\bfactura\b",
    r"\buniversidad\b",
    r"\bdocencia\b",
    r"\btraslado\b",
    r"\bviaje\b",
)

# Aspiraciones y proyectos personales de la seccion 8. Son vida, no codigo:
# "quiero hacer un videojuego biblico" registra una aspiracion aparcada.
_ASPIRATION_MARKERS = (
    r"\bvideojuegos?\b",
    r"\bbiblia\b",
    r"\blibros?\b",
    r"\bterreno\b",
    r"\blaboratorio\b",
    r"\bagricultura\b",
    r"\bgranja\b",
    r"\bnegocios?\b",
    r"\bempresas?\b",
    r"\bportafolio\b",
    r"\bherman[oa]\b",
    r"\bmedio digital\b",
    r"\bingl[eé]s\b",
    r"\bid[ií]oma\b",
    r"\bcurso\b",
    r"\bsueldos?\b",
    r"\bsalarios?\b",
    r"\bhonorarios\b",
    r"\bremotos?\b",
    r"\bn[oó]mina\b",
)

# Capacidad declarada del dia. Deliberadamente estrecho: exige "hoy/ahora" junto
# a un verbo de disponibilidad y un numero. Sin esa restriccion, "hice 30 minutos
# de ejercicio" -que es un avance, no una disponibilidad- caeria aqui, porque la
# unica palabra que comparten es "minutos".
_CAPACITY_PATTERNS = (
    r"\b(?:hoy|ahora)\s+(?:solo\s+|solamente\s+)?(?:tengo|me quedan|me queda|cuento con)\s+\d{1,3}\s*(?:minutos?|min|horas?|h)\b",
    r"\bhoy no (?:puedo|me da|me alcanza|alcanzo|voy a poder)\b",
    r"\bse me complic[oó] (?:el|la) (?:d[ií]a|jornada|ma[nñ]ana)\b",
    r"\bhoy (?:estoy|ando) (?:saturado|cargado|sin tiempo)\b",
)

# Pedido explicito del plan del dia o de la revision.
_PLAN_DAY_PATTERNS = (
    r"\b(?:planifica|prepara|prep[aá]rame|arm[aá]|hazme|dame)\s+(?:mi|el)\s+(?:d[ií]a|plan)\b",
    r"\bplan (?:de hoy|del d[ií]a)\b",
    r"\b(?:qu[eé] hago|en qu[eé] trabajo) hoy\b",
)

_REVIEW_PATTERNS = (
    r"\b(?:revisa|revisemos|revisemos la|analiza|c[oó]mo (?:va|vamos|cerr[oó]))\s+(?:la|el|mi|esta)\s+(?:semana|rumbo|mes)\b",
    r"\brevisi[oó]n (?:semanal|mensual|del mes|de la semana)\b",
    r"\bc[oó]mo (?:nos fue|cerr[oó] la semana)\b",
)

_IMPORT_PATTERNS = (
    r"\bimporta(?:r|me)?\s+(?:mi|el)\s+contexto\b",
    r"\btrae(?:me)?\s+(?:mi|el)\s+contexto\b",
    r"\bcarga(?:r|me)?\s+(?:mi|el)\s+contexto\b",
)

# Fase 3: Delegacion, entrega y metricas de supervisión/retrabajo.
_DELEGATE_PATTERNS = (
    r"\b(?:delega|delegar|encarga|encargar)\s+(?:la\s+|el\s+|esta\s+|este\s+)?(?:siguiente\s+)?(?:acci[oó]n|tarea|ticket)\b",
    r"\bdelega\s+(?:la\s+)?siguiente\b",
    r"\bencarga\s+(?:la\s+)?siguiente\b",
    r"\bdelega\s+esta\s+acci[oó]n\b",
    r"\bencarga\s+esta\s+acci[oó]n\b",
    r"\bhaz\s+la\s+siguiente\s+acci[oó]n\s+en\s+dev\b",
)

_DELIVERY_PATTERNS = (
    r"\bqu[eé]\s+(?:resultado|respuesta)\s+(?:dio|entreg[oó]|hay de)\s+(?:la\s+)?(?:delegaci[oó]n|tarea\s+delegada|acci[oó]n\s+delegada)\b",
    r"\bc[oó]mo\s+(?:sali[oó]|va|termin[oó])\s+(?:la\s+delegaci[oó]n|el\s+encargo|lo\s+que\s+encargu[eé]|lo\s+delegado)\b",
    r"\bqu[eé]\s+entreg[oó]\s+el\s+dev\b",
    r"\bentregas?\s+de\s+(?:la\s+)?delegaci[oó]n\b",
    r"\bresultado\s+de\s+lo\s+delegado\b",
)

_METRICS_PATTERNS = (
    r"\b(?:anota|registra|fueron|tom[oó]|cost[oó])\s+\d+\s+minutos?\s+de\s+(?:supervisi[oó]n|revisi[oó]n|retrabajo|correcci[oó]n)\b",
    r"\b\d+\s+minutos?\s+de\s+(?:supervisi[oó]n|retrabajo)\b",
    r"\bhubo\s+(?:retrabajo|que\s+corregir)\b",
)

# F5: brief con empresa, bandeja y respuesta. Estrechos a proposito: una frase
# dev ("revisa el ticket", "mergea la rama") nunca cae aqui aunque mencione
# tickets, porque exige vocabulario de brief/decision.
_BRIEF_PATTERNS = (
    r"\bbrief\s+(?:del\s+d[ií]a|diario|de\s+hoy|de\s+la\s+empresa|empresarial)\b",
    r"\bbriefing\s+(?:del\s+d[ií]a|diario|matutino)\b",
    r"\bresumen\s+(?:del\s+d[ií]a|de\s+hoy)\b",
    r"\bqu[eé]\s+avanz[oó]\s+(?:el\s+ceo|la\s+empresa|hoy|esta\s+semana)\b",
    r"\bestado\s+de\s+la\s+empresa\b",
    r"\bc[oó]mo\s+va\s+la\s+empresa\b",
    r"\bqu[eé]\s+(?:pas[oó]|hay)\s+en\s+la\s+empresa\b",
)

_DECISIONS_PATTERNS = (
    r"\bdecisiones?\s+pendientes\b",
    r"\bpendientes?\s+de\s+(?:decisi[oó]n|aprobaci[oó]n)\b",
    r"\bqu[eé]\s+(?:espera|esperan|necesita|necesitan|requiere|requieren)\s+(?:mi|tu|nuestra)\s+(?:decisi[oó]n|aprobaci[oó]n|respuesta)\b",
    r"\bqu[eé]\s+tengo\s+que\s+(?:decidir|aprobar|responder)\b",
    r"\bbandeja\s+(?:de\s+decisiones|requiere\s+humano)\b",
    r"\brequiere\s+humano\b",
)

# F5: responder una decision. Exige verbo + (sustantivo de decision | ordinal
# | "ticket N"): un "apruebalo" suelto o un "si" no deciden nada. "Autorizo"
# NO entra: un compromiso personal ("autorizo el pago") es captura, no una
# respuesta a la bandeja de empresa.
_DECIDE_APPROVE_VERBS = (
    r"\baprueb\w*\b",
    r"\baprobad\w*\b",
)
_DECIDE_DENY_VERBS = (
    r"\brechaz\w*\b",
    r"\bdenieg\w*\b",
    r"\bdenegad\w*\b",
)
_DECIDE_NOUNS = (
    r"\bdecisi[oó]n(?:es)?\b",
    r"\bescalado\b",
    r"\baprobaci[oó]n\b",
)
_DECIDE_ORDINALS = (
    r"\bprimera\b",
    r"\bsegunda\b",
    r"\btercera\b",
    r"\bcuarta\b",
    r"\bquinta\b",
)
_ORDINAL_VALUES = {
    "primera": 1,
    "segunda": 2,
    "tercera": 3,
    "cuarta": 4,
    "quinta": 5,
}

_MEASUREMENT_PATTERN = re.compile(
    r"\b(\d{1,4})\s*(minutos?|min|horas?|h)\b",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """Normaliza para deduplicacion: minusculas, sin acentos, espacios simples."""
    if not text:
        return ""
    lowered = text.strip().lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = re.sub(r"[^a-z0-9áéíóúüñ\s:%]", " ", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def has_dev_markers(text: str) -> bool:
    return _matches(text, _DEV_MARKERS)


def has_personal_markers(text: str) -> bool:
    """Contexto personal o de aspiracion propio de la vida, no del codigo."""
    return _matches(text, _PERSONAL_MARKERS) or _matches(text, _ASPIRATION_MARKERS)


def has_explicit_capture(text: str) -> bool:
    return _matches(text, _CAPTURE_PREFIX_PATTERNS)


def is_finance_operative(text: str) -> bool:
    """True si la frase pertenece a Finance (ERPNext), no a Horizon.

    La autoridad es el mismo clasificador que usa Finance
    (`agents.finance.intent`), no un vocabulario duplicado aqui: si los dos
    modulos deciden por su cuenta, una frase de dinero termina como nota
    personal y el libro mayor no se entera.

    Solo cuenta la **escritura y la regla del 10%** (ingreso, gasto, aporte):
    la lectura de saldo (`BALANCE`) se decide por vocabulario ordinario ("total",
    "resumen", "cuentas", "hoy", "mes") y no exige monto ni moneda, asi que
    reclamaba frases personales del dueno ("tengo que ver las cuentas del banco
    con mi padre", "me gustaria un resumen del plan de hoy", "hoy solo tengo 15
    minutos en total"). Esas frases vuelven a Horizon; el ruteo a Finance de las
    consultas de saldo sigue vivo en `first_layer`/`main` via `is_finance_text`.

    Sin esta guarda el dano NO era solo una nota mal archivada: en el
    comportamiento anterior Horizon dejaba pasar la frase y Finance la
    contabilizaba con el signo invertido ("registra ingreso sueldo 6200
    bolivianos" -> debito a gasto / credito a efectivo). Ese es el motivo real
    de la guarda, no la estetica del enrutado. El import es perezoso y un fallo
    degrada a "Horizon conserva la frase" (fail-open), pero ahora deja registro.
    """
    try:
        from agents.finance.intent import CONTRIBUTION, EXPENSE, INCOME
        from agents.finance.intent import classify as classify_finance
    except Exception as exc:  # pragma: no cover - depende del arbol de imports
        logger.warning(
            "Finance intent guard unavailable (%s); Horizon keeps the phrase", exc
        )
        return False
    try:
        return classify_finance(text) in {INCOME, EXPENSE, CONTRIBUTION}
    except Exception:
        logger.warning("Finance intent guard failed; Horizon keeps the phrase", exc_info=True)
        return False


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    if not text:
        return False
    normalized = normalize_text(text)
    for pattern in patterns:
        if re.search(pattern, normalized, re.IGNORECASE):
            return True
    return False


def _is_decide(normalized: str) -> bool:
    """Verbo de decision + sustantivo, ordinal o "ticket N"."""
    has_verb = _matches(normalized, _DECIDE_APPROVE_VERBS) or _matches(
        normalized, _DECIDE_DENY_VERBS
    )
    if not has_verb:
        return False
    if _matches(normalized, _DECIDE_NOUNS):
        return True
    if _matches(normalized, _DECIDE_ORDINALS):
        return True
    if re.search(r"\bticket\s+\d+\b", normalized):
        return True
    return bool(re.search(r"\b(?:la|el)\s+\d{1,2}\b", normalized))


def parse_decide_input(text: str) -> dict[str, Any]:
    """Extrae veredicto (aprueba/rechaza) y destino (ordinal/ticket).

    ``approve`` es None si hay verbos de ambos lados o de ninguno: quien
    llama aclara en vez de adivinar.
    """
    normalized = normalize_text(text)
    approve_hit = _matches(normalized, _DECIDE_APPROVE_VERBS)
    deny_hit = _matches(normalized, _DECIDE_DENY_VERBS)
    approve: bool | None = None
    if approve_hit and not deny_hit:
        approve = True
    elif deny_hit and not approve_hit:
        approve = False

    ordinal: int | None = None
    for word, value in _ORDINAL_VALUES.items():
        if re.search(rf"\b{word}\b", normalized):
            ordinal = value
            break
    if ordinal is None:
        m = re.search(r"\b(?:la|el)\s+(\d{1,2})\b", normalized)
        if m:
            ordinal = int(m.group(1))

    ticket_number: int | None = None
    m = re.search(r"\bticket\s+(\d+)\b", normalized)
    if m:
        ticket_number = int(m.group(1))

    return {"approve": approve, "ordinal": ordinal, "ticket_number": ticket_number}


def _first_match(text: str, patterns: tuple[str, ...]) -> str | None:
    if not text:
        return None
    normalized = normalize_text(text)
    for pattern in patterns:
        m = re.search(pattern, normalized, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def extract_measurement(text: str) -> tuple[int, str] | None:
    """Extrae (cantidad, unidad) de una frase de avance medible."""
    m = _MEASUREMENT_PATTERN.search(text or "")
    if not m:
        return None
    amount = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("h") and not unit.startswith("ho"):
        return amount * 60, "minutes"
    if unit.startswith("h"):
        return amount * 60, "minutes"
    return amount, "minutes"


def classify_capture_kind(text: str) -> CaptureKind:
    """Tipo de captura segun el vocabulario de la seccion 9.3."""
    normalized = normalize_text(text)
    if re.search(r"\b(?:pendiente|falta|debo|hay que)\b", normalized):
        return CaptureKind.PENDING
    if re.search(r"\b(?:compromisos?|me compromet\w*|promet\w*|acord[eé] con)\b", normalized):
        return CaptureKind.COMMITMENT
    if re.search(r"\b(?:grabe|grab[eé]|termin[eé]|complet[eé]|avanc[eé]|entregu[eé]|pag[ué])\b", normalized):
        return CaptureKind.PROGRESS
    return CaptureKind.IDEA


def wants_decision(text: str, kind: CaptureKind) -> tuple[bool, str | None]:
    """Detecta si la captura desplaza un compromiso y exige decision humana.

    Seccion 9.4: las acciones monetarias, compromisos contractuales nuevos y
    mensajes a terceros necesitan autorizacion. Horizon deja todo preparado,
    pero no decide por el usuario.
    """
    if kind == CaptureKind.COMMITMENT:
        return True, "Compromiso nuevo: requiere tu autorizacion explicita."
    normalized = normalize_text(text)
    if re.search(r"\b(?:pagar|pago|transferir|comprar|invertir|prestar)\b", normalized):
        return True, "Movimiento de dinero: Horizon prepara, tu autorizas."
    if re.search(r"\b(?:renunciar|dejar el trabajo|despedir|contratar)\b", normalized):
        return True, "Cambia un compromiso laboral: decision exclusivamente tuya."
    return False, None


def classify(text: str) -> str | None:
    """Devuelve un intent de Horizon, o ``None`` si la frase no le pertenece.

    ``None`` significa "deja que siga el flujo normal" (LLM de primera capa o
    enrutamiento a agentes). No significa error.
    """
    if not text or not text.strip():
        return None

    normalized = normalize_text(text)
    if not normalized:
        return None

    dev = has_dev_markers(text)
    personal = has_personal_markers(text)
    explicit = has_explicit_capture(text)

    # 0. Metricas de delegacion/retrabajo especificas (gana sobre captura generica).
    if _matches(normalized, _METRICS_PATTERNS):
        return INTENT_METRICS

    # 0b. Finanzas operativas (ingreso, gasto, saldo, regla del 10%):
    #     pertenecen a Finance sobre ERPNext, no a Horizon. Devolver None deja
    #     que siga el flujo normal (CEO->Finance). Se consulta al clasificador
    #     de Finance con el texto crudo: el normalizado aqui pierde `.`, `,` y
    #     `$`, que es justo lo que `money_text` necesita para leer el monto.
    if is_finance_operative(text):
        return None

    # 1. Verbo de registro explicito: gana siempre. Una nota personal declarada
    #    como tal no debe terminar en tmux aunque mencione un ticket.
    if explicit:
        return INTENT_CAPTURE

    # 2. Delegacion y entregas (debe ir antes de consultas de direccion,
    #    para que "delega la siguiente accion" ejecute y no solo consulte).
    if _matches(normalized, _DELEGATE_PATTERNS):
        return INTENT_DELEGATE
    if _matches(normalized, _DELIVERY_PATTERNS):
        return INTENT_DELIVERY

    # 2b. Cockpit F5: brief, bandeja y respuesta. Van antes de direccion
    #     porque "que sigue" también trae empresa, pero el brief y la bandeja
    #     son superficies propias con vocabulario propio.
    if _matches(normalized, _BRIEF_PATTERNS):
        return INTENT_BRIEF
    if _matches(normalized, _DECISIONS_PATTERNS):
        return INTENT_DECISIONS
    if _is_decide(normalized):
        return INTENT_DECIDE

    # 3. Consultas de direccion.
    if _matches(normalized, _NEXT_ACTION_PATTERNS):
        return INTENT_NEXT_ACTION
    if _matches(normalized, _PLAN_QUERY_PATTERNS):
        return INTENT_PLAN_QUERY

    # 4. Ciclo proactivo. Va despues de la captura explicita -que debe seguir
    #    ganando- y antes de la deteccion de avances: "hoy solo tengo 15
    #    minutos" declara capacidad, no registra un avance de 15 minutos.
    if _matches(normalized, _CAPACITY_PATTERNS):
        return INTENT_CAPACITY
    if _matches(normalized, _PLAN_DAY_PATTERNS):
        return INTENT_PLAN_DAY
    if _matches(normalized, _REVIEW_PATTERNS):
        return INTENT_REVIEW
    if _matches(normalized, _IMPORT_PATTERNS):
        return INTENT_IMPORT

    # 3. Avance medible o verbos de cierre, solo con contexto personal o
    #    metrica explicita, para no robarle frases dev.
    progress_marker = _matches(normalized, _PROGRESS_PATTERNS)
    measurement = extract_measurement(text)
    if progress_marker and (personal or measurement):
        return INTENT_PROGRESS

    # 4. Captura blanda: exige contexto personal y ausencia de tecnico.
    if _matches(normalized, _CAPTURE_SOFT_PATTERNS) and personal and not dev:
        return INTENT_CAPTURE

    # 5. Una idea personal sin marcadores tecnicos: la seccion 9.3 pide
    #    capturarla sin exigir clasificacion al usuario.
    if personal and not dev and re.search(r"\b(?:quiero|necesito|deber[ií]a|me gustar[ií]a|tengo)\b", normalized):
        return INTENT_CAPTURE

    return None


def is_personal_only(text: str) -> bool:
    """True si la frase es personal y no debe tocar tmux ni sesiones dev."""
    intent = classify(text)
    if intent is None:
        return False
    return not has_dev_markers(text) or has_explicit_capture(text)


def strip_capture_prefix(text: str) -> str:
    """Quita el verbo imperativo para guardar la sustancia, no la orden.

    "anota que el cliente me pidio otro cambio" se registra como
    "el cliente me pidio otro cambio", que es lo que el usuario quiso decir.
    """
    if not text:
        return ""
    stripped = text.strip()
    for pattern in (
        r"^\s*(?:anota|anotar|apunta|apuntar|apunt[aá]me|registra|registrar|guarda|guardar)\s*:\s*",
        r"^\s*(?:anota|anotar|apunta|apuntar|apunt[aá]me|registra|registrar|guarda|guardar)\s+(?:que\s+)?",
        r"^\s*recuerd[aá](?:me)?\s*:?\s*(?:que\s+)?",
        r"^\s*record[aá](?:me)?\s*:?\s*(?:que\s+)?",
        r"^\s*(?:idea|pendiente|compromiso|nota)\s*:\s*",
    ):
        new_value = re.sub(pattern, "", stripped, flags=re.IGNORECASE)
        if new_value != stripped:
            stripped = new_value
            break
    return stripped.strip() or text.strip()


def parse_declared_minutes(text: str) -> int | None:
    """Minutos de disponibilidad declarados, si la frase los trae.

    Solo se interpreta cuando la frase es realmente de capacidad: una cantidad
    suelta no basta, porque "grabé 15 minutos" es un avance y no una
    disponibilidad.
    """
    if classify(text) != INTENT_CAPACITY:
        return None
    measurement = extract_measurement(text)
    if not measurement:
        return None
    amount, _unit = measurement
    return int(amount)


def parse_exercise_included(text: str) -> bool | None:
    """Si el usuario dijo que el ejercicio ya estaba dentro de su tiempo libre.

    ``None`` significa que no lo dijo, y entonces se reserva igual: la reserva
    es la opcion conservadora y es lo que el mandato ordena.
    """
    normalized = normalize_text(text)
    if re.search(r"\b(?:sin contar|aparte del?|ademas del?|m[aá]s) (?:el )?ejercicio\b", normalized):
        return False
    if re.search(r"\b(?:incluido|ya incluido|con) (?:el )?ejercicio\b", normalized):
        return True
    if re.search(r"\bya (?:incluye|contaba con) (?:el )?ejercicio\b", normalized):
        return True
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_metrics_input(text: str) -> dict[str, Any]:
    """Extrae minutos de supervision, retrabajo y si hubo retrabajo."""
    normalized = normalize_text(text)
    sup_m = re.search(r"(\d{1,4})\s*minutos?\s*(?:de\s+)?(?:supervisi[oó]n|revisi[oó]n)", normalized)
    rew_m = re.search(r"(\d{1,4})\s*minutos?\s*(?:de\s+)?(?:retrabajo|correcci[oó]n)", normalized)
    sup = int(sup_m.group(1)) if sup_m else None
    rew = int(rew_m.group(1)) if rew_m else None
    has_rework = bool(rew) or bool(re.search(r"\b(?:retrabajo|hubo que corregir|corregir)\b", normalized))
    return {
        "human_supervision_minutes": sup,
        "human_rework_minutes": rew,
        "rework": has_rework,
    }