"""Automatic tier assignment for loose tickets (autonomy F3, TASK-2026-00137).

Loose tickets (no ``objective_id``) that reach dev without a ``coding_tool``
used to park in ``waiting`` with ``routing_missing``. Objective children
always carry the planner tier, so an empty routing value at the dev boundary
means "loose and unclassified" — this module is the tier authority that
assigns it.

The classifier is deterministic and spends zero tokens: ordered
heuristic rules over title + description + attachment names (Spanish and
English), mirroring the normative tier semantics of
``openceo-core/objectives/routing_policy.d/dev.md`` §18-27 in S → A → B → C
order (recalibrated by TASK-2026-00148: ``tier_c`` is the default).
Every decision carries a ``tier_reason`` citing the fired signal, so a
misclassification is auditable and tunable. A/B decisions also carry
``why_not_c``: what breaks if the default tier executes the ticket.

Tier S is chosen for the value of orchestrating several independent
model families, in ANY domain (policy §19, TASK-2026-00178); this
classifier can only see its textual evidence, in two forms. (1) An
explicit request for it (``s_assurance``: multi-model orchestration,
consensus, independent review, maximum reliability, a near fault-free
result), which is the domain-free path. (2) Domain vocabulary
(auth, money, destructive, integrity, distributed, privacy/compliance,
critical incidents, a money-sensitive project's domain). Whatever
else needs S is the planner's call for objective work, or the explicit
``coding_tool`` of whoever creates the ticket.

Selection bias (explicit rule): keyword signals fire on mere mention,
so ambiguous high-stakes, discovery or complexity wording resolves
UPWARD to S/A/B — over-assigning wastes scarcity, under-assigning
ships complex work to the default tier. Silence resolves to the
default ``tier_c``: policy §25 selects C for specified work with no
S/A/B justification, and a keyword rule cannot prove complexity any
more than it could prove an oracle. ``priority`` alone never selects
S (§19), and ``tier_*`` tokens in the text are NOT honored: the
``coding_tool`` field is the explicit channel and this classifier only
runs when it is empty. A mention that its own clause clearly excludes
from the work ("no toca pagos", "payments excluded"), that sits in an
out-of-scope section, or that only cites the routing policy (a §
reference, not work) is not a work signal and never selects S on its
own (TASK-2026-00171); a goal stated as an end state ("sin pagos
duplicados") still fires, since the work is the money logic itself.

A cited oracle (coverage claim plus ``tests/...`` refs in one
sentence) is attached as evidence to any tier and freezes at branch
time (§24); it never decides the tier. A significant blast radius
below S stakes (§26), and the weak verification that only aggravates
it, need judgment the keywords do not have, so the planner — not this
classifier — owns that B prong.

Quota never enters: the signature takes no quota input, so admission
(00113) cannot be silently degraded by classification. The enqueuer may
peek with this pure function for tier-aware admission; assignment itself
happens at dispatch in ``DevSessionManager.run`` and persists through the
existing ``DevExecutionOutput`` funnel.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from state import CodingTool

logger = logging.getLogger(__name__)

#: Provenance marker recorded in ``params.tier_assigned_by``.
#: v4 = excluded/out-of-scope/policy-cited S mentions stop firing
#: (TASK-2026-00171); v3 = explicit-assurance S signal,
#: privacy/compliance S signal and a stricter oracle-evidence
#: extraction (TASK-2026-00178); v2 = recalibrated S → A → B → C order
#: with ``tier_c`` default (TASK-2026-00148); v1 decisions were
#: B-default. Older decisions stay readable.
ASSIGNED_BY = "loose_classifier/v4"


@dataclass(frozen=True)
class LooseTierDecision:
    """Result of :func:`classify_loose_tier`."""

    tier: CodingTool
    reason: str
    signals: tuple[str, ...] = ()
    oracle: dict | None = None
    why_consensus_is_needed: str | None = None
    why_single_session_is_insufficient: str | None = None
    why_not_c: str | None = None


def _alt(*words: str) -> str:
    return r"\b(?:" + "|".join(words) + r")\b"


def _phrase(phrase: str) -> str:
    return r"\b" + r"\s+".join(re.escape(part) for part in phrase.split()) + r"\b"


def _compile(patterns: Sequence[str]) -> "re.Pattern[str]":
    return re.compile("|".join(f"(?:{item})" for item in patterns),
                       re.IGNORECASE | re.UNICODE)


# -- Tier S: independent high assurance (policy §19) ---------------------------
# Each entry: (signal id, patterns, why_consensus, why_single_insufficient).
_S_AUTH = (
    "s_auth",
    (
        _alt("auth", "authentication", "autenticación", "autenticacion",
             "authorization", "autorización", "autorizacion", "login",
             "oauth", "saml", "openid", "sso", "mfa", "2fa",
             "privilege", "privileges", "privilegio", "privilegios",
             "vulnerability", "vulnerabilities", "vulnerable",
             "vulnerabilidad", "vulnerabilidades", "cve",
             "xss", "csrf", "rce"),
        _phrase("access control"),
        _phrase("control de acceso"),
        _phrase("security boundary"),
        _phrase("frontera de seguridad"),
        _phrase("sql injection"),
        _phrase("inyección sql"),
        _phrase("inyeccion sql"),
    ),
    "Independent planning/review by another model family is expected to catch "
    "access-control bypasses or credential-handling flaws a single builder "
    "session would bake into the auth/security boundary.",
    "A single session both designs and implements the auth/security "
    "boundary, so its blind spots (missed bypass paths, confused-deputy "
    "flows) survive to merge.",
)
_S_MONEY = (
    "s_money",
    (
        _alt("pago", "pagos", "payment", "payments", "cobro", "cobros",
             "facturación", "facturacion", "billing", "invoice", "invoices",
             "factura", "facturas", "dinero", "money", "monetary",
             "transferencia", "transferencias", "payout", "payouts",
             "refund", "refunds", "reembolso", "reembolsos"),
        _phrase("money transfer"),
        _phrase("bank transfer"),
        _phrase("wire transfer"),
    ),
    "Independent challenge is expected to catch money-movement or "
    "amount-computation errors (wrong debits/credits, rounding, duplicated "
    "charges) a single session would ship.",
    "One session owns both the money logic and its verification, so a "
    "misread of the money rules propagates to production.",
)
_S_DESTRUCTIVE = (
    "s_destructive",
    (
        _alt("destructive", "destructiva", "destructivo", "destructivas",
             "destructivos", "irreversible", "irreversibles"),
        _phrase("data loss"),
        _phrase("pérdida de datos"),
        _phrase("perdida de datos"),
        _phrase("drop table"),
        _phrase("truncate table"),
        _phrase("delete from"),
        _phrase("hard delete"),
        _phrase("borrado masivo"),
        _phrase("mass delete"),
        _phrase("bulk delete"),
        _phrase("borrar producción"),
        _phrase("borrar produccion"),
        _phrase("wipe database"),
        _phrase("wipe data"),
    ),
    "Independent review is expected to catch scope errors in the destructive "
    "step (wrong predicate, missing backup, unrecoverable path) before data "
    "is gone.",
    "A single session executes an irreversible step with no second pair of "
    "eyes on the blast radius; a wrong predicate deletes production data.",
)
_S_INTEGRITY = (
    "s_integrity",
    (
        _alt("invariant", "invariants", "invariante", "invariantes"),
        _phrase("data integrity"),
        _phrase("integridad de datos"),
        _phrase("data consistency"),
        _phrase("consistencia de datos"),
    ),
    "Independent challenge is expected to catch invariant violations and "
    "consistency holes a single session's happy-path reasoning misses.",
    "One session both defines and implements the invariant, so its misread "
    "of the consistency rule becomes the implementation.",
)
_S_DISTRIBUTED = (
    "s_distributed",
    (
        _alt("distributed", "distribuido", "distribuida", "distribuidos",
             "distribuidas", "cross-repo", "cross-repository",
             "multi-system", "multisystem", "retrocompatibilidad"),
        _phrase("backward compatibility"),
        _phrase("compatibilidad hacia atrás"),
        _phrase("compatibilidad hacia atras"),
    ),
    "Independent planning is expected to catch subtle cross-system or "
    "distributed failures (ordering, partial failure, version skew) a "
    "single session would miss.",
    "A single session cannot hold the whole multi-system interaction in "
    "one head of reasoning; its blind spots become cross-system bugs.",
)
_S_PRIVACY = (
    "s_privacy",
    (
        _phrase("frontera de privacidad"),
        _phrase("privacy boundary"),
        _phrase("cumplimiento normativo"),
        _phrase("regulatory compliance"),
        _alt("gdpr", "rgpd", "lopd", "hipaa"),
        _phrase("pci dss"),
        _phrase("pci-dss"),
    ),
    "Independent review is expected to catch privacy or regulatory "
    "boundary leaks (data crossing a trust line, missing consent or "
    "retention rules) a single session would not see from inside its own "
    "design.",
    "One session both draws the privacy boundary and implements it, so "
    "its blind spots become the compliance gap.",
)
# Domain vocabulary. Bare ambiguous words ("datos personales", "public
# API") are deliberately absent: they appear in constraint lines such as
# "no personal data in artifacts" and would fire on mere mention.
_S_SIGNALS = (_S_AUTH, _S_MONEY, _S_DESTRUCTIVE, _S_INTEGRITY, _S_DISTRIBUTED,
              _S_PRIVACY)

# -- Tier S, explicit request (policy §19, TASK-2026-00178) -------------------
# The domain-free path: the requester asks for the multi-model result. Phrases
# are qualified on purpose ("varios modelos" alone is ORM models, "máxima
# calidad" is image quality) and a negated mention ("no requiere revisión
# independiente") never fires.
_S_ASSURANCE = (
    "s_assurance",
    (
        _phrase("orquestación de modelos"),
        _phrase("orquestacion de modelos"),
        _phrase("orquestación de varios modelos"),
        _phrase("orquestacion de varios modelos"),
        _phrase("orquestación con varios modelos"),
        _phrase("orquestacion con varios modelos"),
        _phrase("orquestación multimodelo"),
        _phrase("orquestacion multimodelo"),
        _phrase("orquestación multi-modelo"),
        _phrase("orquestacion multi-modelo"),
        _phrase("model orchestration"),
        _phrase("multi-model orchestration"),
        _phrase("multimodel orchestration"),
        _phrase("multi-model consensus"),
        _phrase("model consensus"),
        _phrase("consenso de modelos"),
        _phrase("consenso entre modelos"),
        _phrase("consenso multimodelo"),
        _phrase("varios modelos de ia"),
        _phrase("múltiples modelos de ia"),
        _phrase("multiples modelos de ia"),
        _phrase("varios modelos independientes"),
        _phrase("múltiples modelos independientes"),
        _phrase("multiples modelos independientes"),
        # The owner's spoken form ("usa varios modelos para esto"). A verb of
        # use keeps it apart from ORM talk ("crear varios modelos en la BD").
        _phrase("usa varios modelos"),
        _phrase("usar varios modelos"),
        _phrase("usando varios modelos"),
        _phrase("usa múltiples modelos"),
        _phrase("usar múltiples modelos"),
        _phrase("usando múltiples modelos"),
        _phrase("multiple ai models"),
        _phrase("several ai models"),
        _phrase("several independent models"),
        _phrase("multiple independent models"),
        _phrase("use multiple models"),
        _phrase("using multiple models"),
        _phrase("use several models"),
        _phrase("using several models"),
        _phrase("multiple model families"),
        _phrase("varias familias de modelos"),
        _phrase("revisión independiente"),
        _phrase("revision independiente"),
        _phrase("revisión cruzada"),
        _phrase("revision cruzada"),
        _phrase("revisión multimodelo"),
        _phrase("revision multimodelo"),
        _phrase("revisión multifamilia"),
        _phrase("revision multifamilia"),
        _phrase("verificación independiente"),
        _phrase("verificacion independiente"),
        _phrase("independent review"),
        _phrase("independent verification"),
        _phrase("cross-review"),
        _phrase("cross review"),
        _phrase("multi-model review"),
        _phrase("multi-family review"),
        _phrase("máxima fiabilidad"),
        _phrase("maxima fiabilidad"),
        _phrase("máximo rigor"),
        _phrase("maximo rigor"),
        _phrase("maximum assurance"),
        _phrase("maximum reliability"),
        _phrase("high assurance"),
        _phrase("casi sin fallas"),
        _phrase("casi sin fallos"),
        _phrase("casi sin errores"),
        _phrase("cero fallas"),
        _phrase("cero fallos"),
        _phrase("cero defectos"),
        _phrase("zero defects"),
        _phrase("minimizar errores"),
        _phrase("minimizar los errores"),
        _phrase("minimizar aún más los errores"),
        _phrase("minimizando errores"),
        _phrase("minimizando los errores"),
        _phrase("minimizando aún más los errores"),
        _phrase("minimize errors"),
        _phrase("minimizing errors"),
    ),
    "The requester asked for independent multi-model verification to "
    "minimize residual errors: independent families are expected to catch "
    "what one session's blind spots would ship.",
    "A single session cannot independently challenge its own plan and "
    "implementation; the requester explicitly wants more assurance than "
    "one session can give.",
)
#: Domain S signals whose mention is skipped only as a clear exclusion:
#: an exclusion marker, or a negation word governing a scope verb, in
#: the clause before or after the mention (TASK-2026-00171). Bare
#: negation is not enough here: "no toca pagos" excludes the work,
#: while "sin pagos duplicados" states it as a goal.
_EXCLUSION_GUARDED = frozenset({
    "s_auth", "s_money", "s_destructive", "s_integrity", "s_distributed",
})
_NEGATION_WORDS = re.compile(
    r"(?:\b(?:no|ni|sin|not|without|never|nunca|tampoco|nor)\b"
    r"|\bnada\s+de\b|n't\b)", re.IGNORECASE | re.UNICODE)
_NEGATION_EXEMPT = re.compile(
    r"\b(?:no\s+s[óo]lo|not\s+only)\b", re.IGNORECASE | re.UNICODE)
#: Touch/scope verbs and nouns: with a negation word in the same clause
#: they exclude the mention ("no toca pagos", "without changes in the
#: login", "sin modificar pagos"). Deliberately NOT "except/salvo": "all
#: X except Y" excludes Y but keeps the rest of X in scope, which a
#: keyword rule cannot tell apart — upward bias keeps it firing.
_SCOPE_VERBS = re.compile(
    r"\b(?:toca\w*|touch\w*|inclu\w*|include\w*|afecta\w*|affect\w*|"
    r"cambia\w*|cambio\w*|chang\w*|modif\w*|alter\w*|romp\w*|break|"
    r"breaks|breaking|usa|usan|usar|usando|usad[oa]s?|use|uses|used|"
    r"using|uso\w*|utiliz\w*|requier\w*|requir\w*|necesit\w*|need\w*|"
    r"implica\w*|involv\w*|cubr\w*|cover\w*)\b",
    re.IGNORECASE | re.UNICODE)
#: Exclusion markers: they exclude the mention on their own, with no
#: negation word needed ("payments excluded", "login untouched",
#: "pagos, ajenos al ticket", "non-goal: pagos").
_EXCLUSION_MARKERS = re.compile(
    r"\b(?:exclu\w*|exclud\w*|untouched|ajeno\w*|non-?goals?|"
    r"no\s+objetivos?)\b|\bout\s+of\s+scope\b|"
    r"\bfuera\s+de[ln]?\s+alcance\b",
    re.IGNORECASE | re.UNICODE)
#: A sentence that cites the routing policy as an example list instead of
#: describing work ("Per routing policy §19, S examples include
#: authentication"). Both halves must share the sentence: a bare §
#: reference next to real work ("Fix the OAuth flow per §19") still
#: fires, since the work is genuine.
_POLICY_CITE = re.compile(
    r"§\s*\d|routing_policy|\brouting\s+policy\b|"
    r"política\s+de\s+ruteo|politica\s+de\s+ruteo",
    re.IGNORECASE | re.UNICODE)
_EXAMPLE_WORDS = re.compile(
    r"\b(?:examples?|ejemplos?|includes?|including|inclu\w*|"
    r"such\s+as|tales\s+como|mentions?|mencion\w*|mención\w*|"
    r"cited?|cita\w*)\b",
    re.IGNORECASE | re.UNICODE)
_SENTENCE_BREAK = re.compile(r"[.!?\n]")
#: Stake qualifiers: "real/live" negated at the mention's edge means no
#: real stake ("bot sin dinero real"), while a goal ("sin pagos
#: duplicados") still fires. Before the match the qualifier must touch
#: it ("without real money", never "el problema real es el login");
#: after it a copula gap is allowed ("el dinero no es real"), but no
#: content word ("pagos sin errores reales" still fires).
_STAKE_QUALIFIER = r"(?:real(?:es)?|live|vivo\w*|en\s+vivo)"
_QUALIFIER_BEFORE = re.compile(
    _STAKE_QUALIFIER + r"\W*$", re.IGNORECASE | re.UNICODE)
_QUALIFIER_GAP = (r"(?:no|not|nunca|never|ni|nor|sin|without|es|son|sea|"
                  r"sean|ser|está|están|estar|is|are|be|been|el|la|los|"
                  r"las|the|a|an|que|that|which)")
_QUALIFIER_AFTER = re.compile(
    r"^\W*(?:" + _QUALIFIER_GAP + r"\W+)*" + _STAKE_QUALIFIER + r"\b",
    re.IGNORECASE | re.UNICODE)
_CLAUSE_BREAK = re.compile(r"[.;:,\n(]")
#: Section headers whose body is out of scope: the header line and every
#: line until the next blank line, markdown header, or end of text.
_OUT_OF_SCOPE_HEADER = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:out\s+of\s+scope|fuera\s+de[ln]?\s+alcance|"
    r"alcance\s+excluido|non-?goals?|no\s+objetivos?)\b",
    re.IGNORECASE | re.UNICODE)
_MD_HEADER = re.compile(r"^\s*#{1,6}\s+\S")
_S_INCIDENT_WORDS = _compile((
    _alt("incident", "incidents", "incidente", "incidentes", "outage",
         "outages", "caída", "caida", "caídas", "caidas"),
))
_S_INCIDENT_UNKNOWN = _compile((
    _alt("unknown", "desconocido", "desconocida", "desconocidos",
         "desconocidas"),
    _phrase("sin causa"),
    _phrase("root cause"),
    _phrase("causa raíz"),
    _phrase("causa raiz"),
))

# -- Tier S money-domain extension (per-project guide, TASK-2026-00161) -----
# Active only when the ticket's project sets ``dev.routing.money_sensitive``.
# A money-domain signal is a conjunction of DOMAIN context (trading venues,
# bots, markets) and STAKE (money at risk): either half alone is too
# ambiguous to route on ("poner en orden", "el problema real", "un bot de
# avisos"). Precedent for the pair shape: the s_incident pair above.
MONEY_DOMAIN_WORDS = (
    "trading", "trader", "traders", "exchange", "exchanges",
    "broker", "brokers", "bróker", "brókeres", "bot", "bots",
    "mercado", "mercados", "market", "markets", "bolsa", "bolsas",
    "forex", "crypto", "cripto", "criptos", "criptomoneda",
    "criptomonedas", "cryptocurrency", "estrategia", "estrategias",
    "strategy", "strategies", "portfolio", "portafolio", "portafolios",
    "ticker", "tickers",
)
MONEY_STAKE_WORDS = (
    "live", "real", "reales", "capital", "capitales", "riesgo",
    "riesgos", "orden", "ordenes", "órdenes", "order", "orders",
    "posicion", "posición", "posiciones", "fondo", "fondos", "fund",
    "funds", "apalancamiento", "apalancamientos", "leverage",
    "leveraged", "margen", "margenes", "márgenes", "margin", "margins",
    "liquidacion", "liquidación", "liquidaciones", "slippage",
    "spread", "spreads", "lotaje", "comision", "comisión",
    "comisiones", "fee", "fees", "perdida", "pérdida", "perdidas",
    "pérdidas", "ganancia", "ganancias", "profit", "profits",
    "balance", "balances",
)
MONEY_STAKE_PHRASES = (
    "en vivo", "dinero real", "capital real", "cuenta real",
    "fondos reales", "stop loss", "take profit", "margin call",
    "orden de compra", "orden de venta", "orden pendiente",
    "ordenes pendientes", "órdenes pendientes", "posicion abierta",
    "posición abierta", "posiciones abiertas", "abrir posicion",
    "abrir posición", "cerrar posicion", "cerrar posición",
    "tamaño de posicion", "tamaño de posición", "tamano de posicion",
    "gestion del riesgo", "gestión del riesgo", "gestion de riesgo",
    "gestión de riesgo", "gestion de capital", "gestión de capital",
    "risk management", "money management", "cuenta live", "bot vivo",
    "bots vivos", "operativa live", "operativa en vivo", "real money",
    "live account", "live trading", "market order", "market orders",
    "limit order", "limit orders",
)
_COMPILED_M_DOMAIN = _compile((_alt(*MONEY_DOMAIN_WORDS),))
_COMPILED_M_STAKE = _compile((
    _alt(*MONEY_STAKE_WORDS),
    *(_phrase(entry) for entry in MONEY_STAKE_PHRASES),
))

# -- Tier A: discovery / architecture / recovery (policy §21) -----------------
_A_UNKNOWN = (
    "a_unknown",
    (
        _alt("undiagnosed", "intermittent", "intermitente", "intermitentes",
             "flaky", "esporádico", "esporadico", "esporádica", "esporadica",
             "esporádicos", "esporadicos"),
        _phrase("causa desconocida"),
        _phrase("causas desconocidas"),
        _phrase("unknown cause"),
        _phrase("root cause"),
        _phrase("causa raíz"),
        _phrase("causa raiz"),
        _phrase("no se sabe"),
        _phrase("sin causa clara"),
        _phrase("sin diagnosticar"),
    ),
    "dominant difficulty is discovering the true/unknown cause (policy §21)",
    "executing before knowing the true cause ships the wrong fix; "
    "discovery must come first (§21)",
)
_A_CONTRADICTORY = (
    "a_contradictory",
    (
        _alt("contradictory", "contradictorio", "contradictoria",
             "contradictorios", "contradictorias"),
        _phrase("conflicting evidence"),
        _phrase("evidencia contradictoria"),
        _phrase("evidencias contradictorias"),
    ),
    "dominant difficulty is reconciling contradictory evidence (policy §21)",
    "executing on unresolved contradictory evidence bakes one reading "
    "in; reconciliation must come first (§21)",
)
_A_LEGACY = (
    "a_legacy",
    (
        _phrase("undocumented legacy"),
        _phrase("legacy sin documentar"),
        _phrase("código heredado"),
        _phrase("codigo heredado"),
        _phrase("undocumented code"),
        _phrase("código sin documentar"),
        _phrase("codigo sin documentar"),
    ),
    "dominant difficulty is recovering undocumented legacy semantics (§21)",
    "executing on unrecovered legacy semantics breaks hidden behavior; "
    "recovery must come first (§21)",
)
_A_TOOLING = (
    "a_tooling",
    (
        _phrase("broken build"),
        _phrase("build roto"),
        _phrase("build rota"),
        _phrase("broken deploy"),
        _phrase("deploy roto"),
        _phrase("broken pipeline"),
        _phrase("pipeline roto"),
        _phrase("pipeline rota"),
        _phrase("tooling roto"),
        _phrase("broken tooling"),
        _phrase("ci roto"),
        _phrase("ci rota"),
        _phrase("broken ci"),
        _phrase("cd roto"),
        _phrase("broken cd"),
    ),
    "dominant difficulty is recovering broken build/deploy/tooling (§21)",
    "executing on a broken build/deploy pipeline cannot verify "
    "anything; recovery must come first (§21)",
)
_A_CROSSSYSTEM = (
    "a_crosssystem",
    (
        _alt("cross-system", "cross-service"),
        _phrase("cross system"),
        _phrase("entre sistemas"),
        _phrase("cross service"),
    ),
    "dominant difficulty is a cross-system invariant (policy §21)",
    "executing without mapping the cross-system invariant breaks the "
    "other side; discovery must come first (§21)",
)
_A_SIGNALS = (_A_UNKNOWN, _A_CONTRADICTORY, _A_LEGACY, _A_TOOLING,
              _A_CROSSSYSTEM)
_A_ARCH_VERBS = _compile((
    _alt("definir", "diseñar", "disenar", "decidir", "evaluar", "elegir",
         "replantear", "rediseñar", "redisenar", "design", "decide",
         "evaluate", "choose", "rethink", "architect"),
))
_A_ARCH_NOUNS = _compile((
    _alt("arquitectura", "arquitecturas", "architecture", "architectures",
         "mecanismo", "mecanismos", "mechanism", "mechanisms"),
))

# -- Tier B: really complex single-session work (policy §26) ------------------
# Each entry: (signal id, patterns, reason, why_not_c). Deliberately
# narrow phrases: bare "migration", "refactor" or "backfill" (routine
# data work, e.g. visibility tickets) must NOT fire — only the
# complexity-bearing variants below justify leaving the C default.
_B_REFACTOR = (
    "b_refactor",
    (
        _phrase("semantic refactor"),
        _phrase("refactorización semántica"),
        _phrase("refactorizacion semantica"),
        _phrase("refactor semántico"),
        _phrase("refactor semantico"),
        _phrase("refactor without behavior change"),
        _phrase("refactor sin cambiar el comportamiento"),
        _phrase("refactor sin cambiar comportamiento"),
    ),
    "semantic refactor: errors pass local tests, correctness needs "
    "judgment (§26)",
    "a misjudged semantic refactor passes local tests and ships "
    "behavior drift only judgment-heavy review would catch (§26)",
)
_B_MIGRATION = (
    "b_migration",
    (
        _phrase("data migration"),
        _phrase("migración de datos"),
        _phrase("migracion de datos"),
        _phrase("schema migration"),
        _phrase("migración de esquema"),
        _phrase("migracion de esquema"),
        _phrase("migración con compatibilidad"),
        _phrase("migracion con compatibilidad"),
    ),
    "data/schema migration with compatibility constraints (§26)",
    "a wrong migration step corrupts or strands data past the point "
    "local tests cover (§26)",
)
_B_CONCURRENCY = (
    "b_concurrency",
    (
        _phrase("race condition"),
        _phrase("condición de carrera"),
        _phrase("condicion de carrera"),
        _phrase("data race"),
        _alt("deadlock", "deadlocks", "interbloqueo", "interbloqueos"),
        _phrase("concurrency bug"),
        _phrase("bug de concurrencia"),
    ),
    "concurrency defect: timing-dependent, errors pass happy-path tests (§26)",
    "a timing-dependent bug survives happy-path tests; only reasoning "
    "about interleavings catches it (§26)",
)
_B_PERFORMANCE = (
    "b_performance",
    (
        _phrase("performance optimization"),
        _phrase("optimización de rendimiento"),
        _phrase("optimizacion de rendimiento"),
        _phrase("cuello de botella"),
        _phrase("cuellos de botella"),
        _alt("bottleneck", "bottlenecks"),
        _phrase("performance regression"),
        _phrase("regresión de rendimiento"),
        _phrase("regresion de rendimiento"),
    ),
    "performance work: needs measurement-driven trade-offs (§26)",
    "a wrong performance trade-off ships slowness or overload no "
    "functional test measures (§26)",
)
_B_INTERPRETATION = (
    "b_interpretation",
    (
        _phrase("requiere interpretación"),
        _phrase("requiere interpretacion"),
        _phrase("requires interpretation"),
        _phrase("reglas difusas"),
        _phrase("fuzzy rules"),
        _phrase("juicio semántico"),
        _phrase("juicio semantico"),
        _phrase("semantic judgment"),
    ),
    "solution needs significant interpretation of fuzzy rules (§26)",
    "a misread fuzzy rule ships wrong behavior that passes every check "
    "written from the same reading (§26)",
)
_B_COMPAT = (
    "b_compat",
    (
        _phrase("restricciones de compatibilidad"),
        _phrase("compatibility constraints"),
        _phrase("matriz de compatibilidad"),
        _phrase("compatibility matrix"),
    ),
    "compatibility-constrained change: trade-offs need judgment (§26)",
    "a wrong compatibility trade-off breaks downstream consumers "
    "outside the tested surface (§26)",
)
_B_SIGNALS = (_B_REFACTOR, _B_MIGRATION, _B_CONCURRENCY,
              _B_PERFORMANCE, _B_INTERPRETATION, _B_COMPAT)

# -- Oracle evidence: coverage claim + test refs (policy §24) -----------------
# Attached to any tier as freeze material; it never decides the tier.
# A bare "preexisting"/"preexistente" is NOT a claim: it also means "a
# pre-existing failure" ("arreglar el fallo preexistente en tests/x.py"),
# and freezing the very test the ticket must fix fails the work requested
# (TASK-2026-00178).
_C_CLAIM = _compile((
    _phrase("covered by"),
    _phrase("cubierto por"),
    _phrase("cubierta por"),
    _phrase("cubiertos por"),
    _phrase("preexisting tests"),
    _phrase("tests preexistentes"),
    _phrase("pruebas preexistentes"),
    _phrase("existing tests"),
    _phrase("existing test"),
    _phrase("existing test suite"),
    _phrase("tests existentes"),
    _phrase("test existentes"),
    _phrase("pruebas existentes"),
    _phrase("already covered"),
    _phrase("ya cubierto"),
    _phrase("ya cubierta"),
    _phrase("ya cubiertos"),
))
#: Repo test paths citable as a freezable ``preexisting_tests`` oracle
#: (``path::node`` test ids accepted; ``freeze_oracle`` strips the node).
_C_TEST_PATH = re.compile(
    r"\btests?/[A-Za-z0-9_.\-/]*[A-Za-z0-9_]\.py"
    r"(?:::[A-Za-z0-9_]+)?\b",
    re.IGNORECASE | re.UNICODE,
)
#: One claim covers only the paths of its own sentence/paragraph.
_C_SEGMENT_SPLIT = re.compile(r"\n\s*\n|(?<=[.!?;])\s+")
#: An edit verb right before a path ("Fix the failing tests in <path>")
#: means the ticket changes that file: it cannot be its own oracle.
_C_EDIT_VERBS = (r"(?:fix|repair|update|modify|edit|rewrite|extend|"
                 r"add\s+to|arregl\w*|corrig\w*|repar\w*|actualiz\w*|"
                 r"modific\w*|edit\w*|reescrib\w*|extend\w*|ampli\w*|"
                 r"agreg\w*\s+a)")
_C_ADJECTIVES = (r"(?:(?:failing|broken|flaky|existing|old|current|roto|"
                 r"rotos|rota|rotas|existente|existentes|actual|"
                 r"actuales)\s+)*")
_C_EDIT_BEFORE_PATH = re.compile(
    r"\b" + _C_EDIT_VERBS + r"\s+(?:(?:the|a|an|el|la|los|las|un|una)\s+)?"
    + _C_ADJECTIVES                      # English order: "failing tests"
    + r"(?:(?:tests?|pruebas?|suite|specs?|casos?)\s+)?"
    + _C_ADJECTIVES                      # Spanish order: "tests existentes"
    + r"(?:(?:in|en|de|del|of)\s+)?$",
    re.IGNORECASE | re.UNICODE,
)

_COMPILED_S: tuple[tuple[str, "re.Pattern[str]", str, str], ...] = tuple(
    (signal_id, _compile(patterns), why_consensus, why_single)
    for signal_id, patterns, why_consensus, why_single in _S_SIGNALS
)
_ASSURANCE_ID, _ASSURANCE_PATTERNS, _ASSURANCE_WHY, _ASSURANCE_SINGLE = \
    _S_ASSURANCE
_COMPILED_S_ASSURANCE = _compile(_ASSURANCE_PATTERNS)
_COMPILED_A: tuple[tuple[str, "re.Pattern[str]", str, str], ...] = tuple(
    (signal_id, _compile(patterns), reason, why_not_c)
    for signal_id, patterns, reason, why_not_c in _A_SIGNALS
)
_COMPILED_B: tuple[tuple[str, "re.Pattern[str]", str, str], ...] = tuple(
    (signal_id, _compile(patterns), reason, why_not_c)
    for signal_id, patterns, reason, why_not_c in _B_SIGNALS
)

#: Caps for project-provided routing vocabulary (TASK-2026-00161).
#: ``ConfigLoader.validate_project`` enforces the same caps and parks the
#: project's tickets on violation; the runtime caps below only keep a
#: malformed file from crashing classification — dispatch parks it next.
_M_CUSTOM_ENTRY_MAX = 80
_M_CUSTOM_LIST_MAX = 64


def _clean_custom_entries(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    cleaned: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if not text or len(text) > _M_CUSTOM_ENTRY_MAX:
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned[:_M_CUSTOM_LIST_MAX]


def _normalize_money_routing(routing: Any) -> dict[str, Any]:
    """Enabled project routing, or ``{}`` (fail-open, never raises).

    Only ``money_sensitive: true`` enables the guide; custom vocabulary
    without the flag is ignored, and malformed values normalize to
    disabled (shape errors are reported by ``validate_project``).
    """
    if not isinstance(routing, dict) \
            or routing.get("money_sensitive") is not True:
        return {}
    normalized: dict[str, Any] = {"money_sensitive": True}
    domain = _clean_custom_entries(routing.get("money_domain"))
    stake = _clean_custom_entries(routing.get("money_stake"))
    if domain:
        normalized["money_domain"] = domain
    if stake:
        normalized["money_stake"] = stake
    return normalized


def project_money_routing(project_config: Any) -> dict[str, Any]:
    """Normalized ``dev.routing`` for one project (``{}`` when absent)."""
    if not isinstance(project_config, dict):
        return {}
    dev = project_config.get("dev")
    if not isinstance(dev, dict):
        return {}
    return _normalize_money_routing(dev.get("routing"))


def _compile_custom(entries: Sequence[str]) -> "re.Pattern[str] | None":
    """Custom vocabulary as one literal pattern (entries are never regex).

    Word guards are lookarounds, not ``\\b``: entries may start or end in
    symbols (``c++``), where a word boundary never matches.
    """
    if not entries:
        return None
    patterns = []
    for entry in entries:
        if any(ch.isspace() for ch in entry):
            patterns.append(_phrase(entry))
        else:
            patterns.append(r"(?<!\w)" + re.escape(entry) + r"(?!\w)")
    return _compile(patterns)


def routing_guide_section(project: str, routing: Any) -> str:
    """Planner prompt section for a money-sensitive project (or ``""``).

    Pure and fail-open: anything but an enabled ``dev.routing`` mapping
    yields no section. The guide prefers ``tier_s`` for money/algorithm
    work while keeping A/B/C eligible with their normal justification —
    guidance, not a rule, and an explicit ``coding_tool`` always wins.
    """
    normalized = _normalize_money_routing(routing)
    if not normalized:
        return ""
    customs: list[str] = list(normalized.get("money_domain", ())) \
        + list(normalized.get("money_stake", ()))
    words = sorted(set(MONEY_DOMAIN_WORDS) | set(MONEY_STAKE_WORDS)
                   | {entry for entry in customs
                      if not any(ch.isspace() for ch in entry)})
    phrases = sorted(set(MONEY_STAKE_PHRASES)
                     | {entry for entry in customs
                        if any(ch.isspace() for ch in entry)})
    quoted = ", ".join(f'"{entry}"' for entry in phrases)
    return (
        f'PROJECT TIER GUIDE (money-sensitive project "{project}"; '
        "guidance, not a rule):\n"
        "- This project moves real money and live algorithms: prefer "
        "tier_s for money/algorithm work when the S justification applies "
        "(why_consensus_is_needed + why_single_session_is_insufficient per "
        "§20: wrong debits/credits, duplicated live orders, mishandled "
        "positions).\n"
        "- Domain vocabulary (money context, never a forced tier). Words: "
        + ", ".join(words) + ". Phrases: " + quoted + ".\n"
        "- tier_a stays for real discovery/root-cause/architecture, tier_b "
        "for complex single-session work, tier_c remains THE DEFAULT for "
        "specified work; A/B keep their normal why_not_c justification.\n"
        "- An explicit coding_tool always wins; this guide never overrides "
        "it and never collapses to a single tier."
    )


def _scan_text(title: str, description: str,
               attachments: Sequence[str]) -> str:
    parts = [str(title or ""), str(description or "")]
    parts.extend(str(name or "") for name in attachments or ())
    return "\n".join(part for part in parts if part)


def _first_match(pattern: "re.Pattern[str]", text: str) -> str | None:
    found = pattern.search(text)
    return found.group(0) if found else None


def _mask_out_of_scope_sections(text: str) -> str:
    """Text without out-of-scope sections (S signals only).

    From an out-of-scope header line to the next blank line, markdown
    header, or end of text, mentions describe work the ticket will NOT
    do, so S signals must not read them. A/B scans keep the raw text:
    the ticket asked to fix them, and 00171 scopes the exclusion to S.
    """
    kept: list[str] = []
    skipping = False
    for line in text.split("\n"):
        if _OUT_OF_SCOPE_HEADER.match(line):
            skipping = True
            continue
        if skipping and (not line.strip() or _MD_HEADER.match(line)):
            skipping = False
        if skipping:
            continue
        kept.append(line)
    return "\n".join(kept)


def _first_unnegated_match(pattern: "re.Pattern[str]", text: str) -> str | None:
    """First match that its own clause does not negate.

    Used by ``s_assurance``/``s_privacy``: "No requiere revisión
    independiente" or "sin datos GDPR" is the opposite of a request,
    so it never fires. The clause is the text since the last
    punctuation/newline before the match; "no solo" is not a negation.
    Policy-cited and excluded mentions are skipped as well.
    """
    return _first_valid_match(pattern, text, bare_negation_skips=True)


def _first_valid_match(pattern: "re.Pattern[str]", text: str,
                       *, bare_negation_skips: bool) -> str | None:
    """First match that counts as a work signal.

    A match is skipped when its sentence cites the routing policy as an
    example list, when either neighboring clause carries an exclusion
    marker, or when a negation governs the mention: assurance/privacy
    and the incident halves keep the older rule where any clause
    negation skips (``bare_negation_skips``), while domain signals and
    the money halves require a scope verb ("no toca pagos") or a
    negated stake qualifier at the mention's edge ("sin dinero real") —
    a bare "sin pagos duplicados" states the work as a goal and still
    fires. Clauses end at punctuation, newlines and open parens; "no
    solo" is not a negation.
    """
    for found in pattern.finditer(text):
        before = _CLAUSE_BREAK.split(text[:found.start()])[-1]
        after = _CLAUSE_BREAK.split(text[found.end():], maxsplit=1)[0]
        before = _NEGATION_EXEMPT.sub(" ", before)
        after = _NEGATION_EXEMPT.sub(" ", after)
        sentence = (_SENTENCE_BREAK.split(text[:found.start()])[-1]
                    + " " + _SENTENCE_BREAK.split(text[found.end():],
                                                  maxsplit=1)[0])
        if _POLICY_CITE.search(sentence) and _EXAMPLE_WORDS.search(sentence):
            continue
        if _EXCLUSION_MARKERS.search(before) or _EXCLUSION_MARKERS.search(after):
            continue
        negated = (_NEGATION_WORDS.search(before) is not None
                   or _NEGATION_WORDS.search(after) is not None)
        if not negated:
            return found.group(0)
        if bare_negation_skips:
            continue
        if _SCOPE_VERBS.search(before) or _SCOPE_VERBS.search(after):
            continue
        if _QUALIFIER_BEFORE.search(before) or _QUALIFIER_AFTER.search(after):
            continue
        # A multi-word hit can carry the qualifier inside itself
        # ("bot sin dinero real" hits the "dinero real" stake phrase).
        matched = found.group(0)
        if (_QUALIFIER_BEFORE.search(matched)
                or _QUALIFIER_AFTER.search(matched)):
            continue
        return found.group(0)
    return None


def _extract_oracle_evidence(text: str) -> dict[str, Any] | None:
    """Declared oracle evidence (coverage claim + test refs), if cited.

    Evidence only: it freezes at branch time (§24) and never decides
    the tier. The claim and the path must share a sentence/paragraph
    (a claim about one thing says nothing about a path cited elsewhere),
    paths without a claim could be builder work, and a path the ticket
    edits ("fix the failing tests in <path>") is never its own oracle.
    A claim without extractable paths is not freezable.
    """
    refs: list[str] = []
    for segment in _C_SEGMENT_SPLIT.split(text):
        if _first_match(_C_CLAIM, segment) is None:
            continue
        for found in _C_TEST_PATH.finditer(segment):
            if _C_EDIT_BEFORE_PATH.search(segment[:found.start()]):
                continue
            ref = found.group(0)
            if ref not in refs:
                refs.append(ref)
    if not refs:
        return None
    return {"type": "preexisting_tests",
            "refs": [{"ref": ref} for ref in refs]}


def classify_loose_tier(
    title: str,
    description: str,
    attachments: Sequence[str] | None = None,
    *,
    project_routing: dict[str, Any] | None = None,
) -> LooseTierDecision:
    """Assign a DEV tier to a loose ticket (no ``objective_id``).

    Pure and deterministic: same text always yields the same decision, with
    zero token spend. Evaluation order is S → A → B → C (policy §18-27);
    the first tier with a fired signal wins and the reason cites it. With
    no signal anywhere the default is ``tier_c`` (§25). Inside S, an
    explicit request for multi-model assurance (``s_assurance``, the
    domain-free path) is read first. A money-sensitive project routing
    (``dev.routing``) adds the ``s_money_domain`` S signal for this
    project only; anything else normalizes to disabled. An S mention
    that its clause excludes from the work, that sits in an
    out-of-scope section, or that only cites the routing policy is not
    a work signal and never selects S on its own (TASK-2026-00171).
    """
    text = _scan_text(title, description, attachments or ())
    oracle = _extract_oracle_evidence(text)
    # S signals read the text without out-of-scope sections: mentions
    # there describe work the ticket will NOT do (TASK-2026-00171).
    s_text = _mask_out_of_scope_sections(text)

    requested = _first_unnegated_match(_COMPILED_S_ASSURANCE, s_text)
    if requested is not None:
        return LooseTierDecision(
            tier=CodingTool.TIER_S,
            reason=(f"explicit assurance request ({_ASSURANCE_ID}: "
                    f"{requested.strip()!r}); the ticket asks for "
                    f"multi-model orchestration (§19)"),
            signals=(f"{_ASSURANCE_ID}:{requested.strip()}",),
            oracle=oracle,
            why_consensus_is_needed=_ASSURANCE_WHY,
            why_single_session_is_insufficient=_ASSURANCE_SINGLE,
        )

    routing = _normalize_money_routing(project_routing)
    if routing:
        # Either half excluded or policy-cited is no stake: "bot de
        # avisos sin dinero real" is not money work, while "stop loss
        # no salta" is a live-money bug (00171) — hence exclusion mode,
        # not bare negation, like the domain signals below.
        domain_hit = _first_valid_match(
            _COMPILED_M_DOMAIN, s_text, bare_negation_skips=False)
        if domain_hit is None:
            custom_domain = _compile_custom(
                routing.get("money_domain", ()))
            if custom_domain is not None:
                domain_hit = _first_valid_match(
                    custom_domain, s_text, bare_negation_skips=False)
        stake_hit = _first_valid_match(
            _COMPILED_M_STAKE, s_text, bare_negation_skips=False)
        if stake_hit is None:
            custom_stake = _compile_custom(
                routing.get("money_stake", ()))
            if custom_stake is not None:
                stake_hit = _first_valid_match(
                    custom_stake, s_text, bare_negation_skips=False)
        if domain_hit is not None and stake_hit is not None:
            return LooseTierDecision(
                tier=CodingTool.TIER_S,
                reason=(f"money-domain signal (s_money_domain: "
                        f"{domain_hit.strip()!r} + {stake_hit.strip()!r}); "
                        f"independent challenge/review reduces residual "
                        f"risk (§19; money_sensitive project guide)"),
                signals=(f"s_money_domain:{domain_hit.strip()}"
                         f"+{stake_hit.strip()}",),
                oracle=oracle,
                why_consensus_is_needed=(
                    "Independent challenge is expected to catch "
                    "money/algorithm errors (wrong amounts, duplicated "
                    "live orders, mishandled positions) a single session "
                    "would ship to real funds."),
                why_single_session_is_insufficient=(
                    "One session owns both the money logic and its "
                    "verification, so a misread of the live-money rules "
                    "propagates to production."),
            )

    for signal_id, pattern, why_consensus, why_single in _COMPILED_S:
        # s_privacy is not exclusion-guarded: it keeps bare negation.
        matched = _first_valid_match(
            pattern, s_text,
            bare_negation_skips=signal_id not in _EXCLUSION_GUARDED)
        if matched is not None:
            return LooseTierDecision(
                tier=CodingTool.TIER_S,
                reason=(f"high-stakes signal ({signal_id}: "
                        f"{matched.strip()!r}); independent "
                        f"challenge/review reduces residual risk (§19)"),
                signals=(f"{signal_id}:{matched.strip()}",),
                oracle=oracle,
                why_consensus_is_needed=why_consensus,
                why_single_session_is_insufficient=why_single,
            )
    incident = _first_valid_match(
        _S_INCIDENT_WORDS, s_text, bare_negation_skips=True)
    unknown = _first_valid_match(
        _S_INCIDENT_UNKNOWN, s_text, bare_negation_skips=True)
    if incident is not None and unknown is not None:
        return LooseTierDecision(
            tier=CodingTool.TIER_S,
            reason=("critical multi-system incident with unknown cause "
                    f"({incident.strip()!r} + {unknown.strip()!r}, §19)"),
            signals=(f"s_incident:{incident.strip()}+{unknown.strip()}",),
            oracle=oracle,
            why_consensus_is_needed=(
                "Independent triage/planning is expected to converge on "
                "the true cause of a critical incident faster and with "
                "fewer wrong fixes than one session guessing under "
                "pressure."),
            why_single_session_is_insufficient=(
                "A single session chasing an unknown-cause production "
                "incident risks misdiagnosis and wrong fixes; independent "
                "challenge converges."),
        )

    for signal_id, pattern, reason, why_not_c in _COMPILED_A:
        matched = _first_match(pattern, text)
        if matched is not None:
            return LooseTierDecision(
                tier=CodingTool.TIER_A,
                reason=f"{reason} ({signal_id}: {matched.strip()!r})",
                signals=(f"{signal_id}:{matched.strip()}",),
                oracle=oracle,
                why_not_c=why_not_c,
            )
    verb = _first_match(_A_ARCH_VERBS, text)
    noun = _first_match(_A_ARCH_NOUNS, text)
    if verb is not None and noun is not None:
        return LooseTierDecision(
            tier=CodingTool.TIER_A,
            reason=("dominant difficulty is discovering the correct "
                    f"architecture/design ({verb.strip()!r} + "
                    f"{noun.strip()!r}, §21)"),
            signals=(f"a_architecture:{verb.strip()}+{noun.strip()}",),
            oracle=oracle,
            why_not_c=("executing before the architecture is decided "
                       "locks the wrong design in (§21)"),
        )

    for signal_id, pattern, reason, why_not_c in _COMPILED_B:
        matched = _first_match(pattern, text)
        if matched is not None:
            return LooseTierDecision(
                tier=CodingTool.TIER_B,
                reason=f"{reason} ({signal_id}: {matched.strip()!r})",
                signals=(f"{signal_id}:{matched.strip()}",),
                oracle=oracle,
                why_not_c=why_not_c,
            )

    reason = ("specified work with no S/A/B justification; default "
              "tier_c (§25)")
    signals: tuple[str, ...] = ()
    if oracle is not None:
        refs = [str(item.get("ref")) for item in oracle.get("refs", [])
                if isinstance(item, dict)]
        reason += (f"; declared oracle (preexisting_tests: {len(refs)} "
                   f"ref(s), e.g. {refs[0]!r}) freezes at branch time")
        signals = tuple(f"c_oracle:{ref}" for ref in refs)
    return LooseTierDecision(
        tier=CodingTool.TIER_C,
        reason=reason,
        signals=signals,
        oracle=oracle,
    )


async def record_loose_tier_decision(
    repo: Any,
    *,
    task_id: str,
    ticket_number: int | None,
    decision: LooseTierDecision,
) -> None:
    """Persist one classifier assignment (policy §32, information only).

    Called only for real assignments at dispatch, never for dispatcher
    peeks. Fail-open: telemetry never breaks the dispatch.
    """
    if repo is None:
        return
    try:
        await repo.record_loose_tier_decision({
            "task_id": str(task_id or ""),
            "ticket_number": ticket_number,
            "tier": decision.tier.value,
            "reason": decision.reason,
            "why_not_c": decision.why_not_c,
            "signals": list(decision.signals),
            "by": ASSIGNED_BY,
            "at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        logger.warning("loose-tier telemetry failed for %s", task_id,
                       exc_info=True)
