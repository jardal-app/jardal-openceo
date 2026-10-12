"""Money-sensitive project guide for the planner (TASK-2026-00161).

A money-sensitive project (``dev.routing.money_sensitive`` in
``project.yaml``) moves real money and live algorithms: the planner
prompt carries a PROJECT TIER GUIDE section preferring ``tier_s`` for
money/algorithm work while keeping A/B/C eligible with their normal
justification — guidance, never a rule, and an explicit ``coding_tool``
always wins.

Moved verbatim out of the retired keyword classifier
(``agents.dev.loose_tier``, TASK-2026-00204): the classifier no longer
decides tiers, but the guide still feeds the planner context for
objective and pedido runs alike.

Dependency-free (stdlib only): the objectives engine and its tests
import this without cycles.
"""

from __future__ import annotations

from typing import Any

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
