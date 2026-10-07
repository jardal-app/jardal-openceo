"""Automatic tier assignment for loose tickets (autonomy F3, TASK-2026-00137).

Loose tickets (no ``objective_id``) that reach dev without a ``coding_tool``
used to park in ``waiting`` with ``routing_missing``. Objective children
always carry the planner tier, so an empty routing value at the dev boundary
means "loose and unclassified" — this module is the tier authority that
assigns it.

The classifier is deterministic and spends zero tokens: ordered
heuristic rules over title + description + attachment names (Spanish and
English), mirroring the normative tier semantics of
``docs/objective-planner-routing-policy.md`` §18-26 in S → A → C → B order.
Every decision carries a ``tier_reason`` citing the fired signal, so a
misclassification is auditable and tunable.

Safety bias (explicit rule, scope §3 of the ticket): on doubt the
classifier errs UPWARD. Assigning a stronger tier than needed wastes
scarcity; assigning a weaker one accepts wrong work without independent
detection (C) or under-solves high-stakes work (S/A). The safe default is
``tier_b``, never ``tier_c``: policy §25 makes the C conjunction
(qualifying oracle AND bounded/reversible) mandatory, and a keyword rule
cannot verify an oracle — so C requires explicit, extractable oracle
evidence (named ``tests/...`` refs) plus a coverage claim plus
boundedness. ``priority`` alone never selects S (§19), and ``tier_*``
tokens in the text are NOT honored: the ``coding_tool`` field is the
explicit channel and this classifier only runs when it is empty.

Quota never enters: the signature takes no quota input, so admission
(00113) cannot be silently degraded by classification. The enqueuer may
peek with this pure function for tier-aware admission; assignment itself
happens at dispatch in ``DevSessionManager.run`` and persists through the
existing ``DevExecutionOutput`` funnel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from state import CodingTool

#: Provenance marker recorded in ``params.tier_assigned_by``.
ASSIGNED_BY = "loose_classifier/v1"


@dataclass(frozen=True)
class LooseTierDecision:
    """Result of :func:`classify_loose_tier`."""

    tier: CodingTool
    reason: str
    signals: tuple[str, ...] = ()
    oracle: dict | None = None
    why_consensus_is_needed: str | None = None
    why_single_session_is_insufficient: str | None = None


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
_S_SIGNALS = (_S_AUTH, _S_MONEY, _S_DESTRUCTIVE, _S_INTEGRITY, _S_DISTRIBUTED)
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

# -- Tier C: qualifying oracle + bounded/reversible (policy §25) --------------
_C_CLAIM = _compile((
    _phrase("covered by"),
    _phrase("cubierto por"),
    _phrase("cubierta por"),
    _phrase("cubiertos por"),
    _alt("preexisting", "preexistente", "preexistentes"),
    _phrase("existing tests"),
    _phrase("existing test"),
    _phrase("tests existentes"),
    _phrase("test existentes"),
    _phrase("already covered"),
    _phrase("ya cubierto"),
    _phrase("ya cubierta"),
    _phrase("ya cubiertos"),
))
_C_BOUNDED = _compile((
    _alt("bounded", "acotado", "acotada", "acotados", "acotadas",
         "reversible", "reversibles", "revertible", "revertibles",
         "small", "pequeño", "pequeno", "pequeña", "pequena",
         "pequeños", "pequenos", "typo", "typos", "trivial", "triviales",
         "minor"),
    _phrase("single file"),
    _phrase("un solo archivo"),
    _phrase("one file"),
    _phrase("one-line"),
    _phrase("one line"),
    _phrase("una línea"),
    _phrase("una linea"),
))
#: Repo test paths citable as a freezable ``preexisting_tests`` oracle
#: (``path::node`` test ids accepted; ``freeze_oracle`` strips the node).
_C_TEST_PATH = re.compile(
    r"\btests?/[A-Za-z0-9_.\-/]*[A-Za-z0-9_]\.py"
    r"(?:::[A-Za-z0-9_]+)?\b",
    re.IGNORECASE | re.UNICODE,
)

_COMPILED_S: tuple[tuple[str, "re.Pattern[str]", str, str], ...] = tuple(
    (signal_id, _compile(patterns), why_consensus, why_single)
    for signal_id, patterns, why_consensus, why_single in _S_SIGNALS
)
_COMPILED_A: tuple[tuple[str, "re.Pattern[str]", str], ...] = tuple(
    (signal_id, _compile(patterns), reason)
    for signal_id, patterns, reason in _A_SIGNALS
)


def _scan_text(title: str, description: str,
               attachments: Sequence[str]) -> str:
    parts = [str(title or ""), str(description or "")]
    parts.extend(str(name or "") for name in attachments or ())
    return "\n".join(part for part in parts if part)


def _first_match(pattern: "re.Pattern[str]", text: str) -> str | None:
    found = pattern.search(text)
    return found.group(0) if found else None


def classify_loose_tier(
    title: str,
    description: str,
    attachments: Sequence[str] | None = None,
) -> LooseTierDecision:
    """Assign a DEV tier to a loose ticket (no ``objective_id``).

    Pure and deterministic: same text always yields the same decision, with
    zero token spend. Evaluation order is S → A → C → B (policy §18-26);
    the first tier with a fired signal wins and the reason cites it. With
    no signal anywhere the safe default is ``tier_b``.
    """
    text = _scan_text(title, description, attachments or ())

    for signal_id, pattern, why_consensus, why_single in _COMPILED_S:
        matched = _first_match(pattern, text)
        if matched is not None:
            return LooseTierDecision(
                tier=CodingTool.TIER_S,
                reason=(f"high-stakes signal ({signal_id}: "
                        f"{matched.strip()!r}); independent "
                        f"challenge/review reduces residual risk (§19)"),
                signals=(f"{signal_id}:{matched.strip()}",),
                why_consensus_is_needed=why_consensus,
                why_single_session_is_insufficient=why_single,
            )
    incident = _first_match(_S_INCIDENT_WORDS, text)
    unknown = _first_match(_S_INCIDENT_UNKNOWN, text)
    if incident is not None and unknown is not None:
        return LooseTierDecision(
            tier=CodingTool.TIER_S,
            reason=("critical multi-system incident with unknown cause "
                    f"({incident.strip()!r} + {unknown.strip()!r}, §19)"),
            signals=(f"s_incident:{incident.strip()}+{unknown.strip()}",),
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

    for signal_id, pattern, reason in _COMPILED_A:
        matched = _first_match(pattern, text)
        if matched is not None:
            return LooseTierDecision(
                tier=CodingTool.TIER_A,
                reason=f"{reason} ({signal_id}: {matched.strip()!r})",
                signals=(f"{signal_id}:{matched.strip()}",),
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
        )

    claim = _first_match(_C_CLAIM, text)
    bounded = _first_match(_C_BOUNDED, text)
    refs: list[str] = []
    for found in _C_TEST_PATH.finditer(text):
        ref = found.group(0)
        if ref not in refs:
            refs.append(ref)
    if claim is not None and bounded is not None and refs:
        return LooseTierDecision(
            tier=CodingTool.TIER_C,
            reason=(f"qualifying oracle cited (preexisting_tests: "
                    f"{len(refs)} ref(s), e.g. {refs[0]!r}) + "
                    f"bounded/reversible change ({bounded.strip()!r}, "
                    f"§25); refs frozen at branch time"),
            signals=tuple(f"c_oracle:{ref}" for ref in refs),
            oracle={"type": "preexisting_tests",
                    "refs": [{"ref": ref} for ref in refs]},
        )

    return LooseTierDecision(
        tier=CodingTool.TIER_B,
        reason=("no S/A high-stakes or discovery signals and no qualifying "
                "oracle evidence; bounded work defaults to tier_b (safe "
                "default, upward bias per TASK-2026-00137)"),
        signals=(),
    )
