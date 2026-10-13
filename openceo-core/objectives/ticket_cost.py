"""Measured cost per ticket and per objective (TASK-2026-00212).

Pure, no I/O, no tokens. Executors report one run record per invocation;
:meth:`record_runs` folds them onto the ticket's stored ``cost`` and
:func:`aggregate_objective_cost` sums tickets into the objective view the
CEO strategy, the planner and the cockpit read. Unknown is null, never 0:
a token total is null until at least one run measured it, and the
aggregation is null until at least one run exists anywhere.

Money stays null: no ERPNext reader links money to a ticket or a project
yet (Journal Entry carries no ticket/project link; GL reads are
company-wide by Cost Center). The ``money``/``money_currency`` fields are
the seam a dated finance reader (HZ-005) fills later; until then they
travel as explicit nulls, like the unknown finance KPI sources.

Cost is information only: no threshold, tier or dispatch decision derives
from it (same rule as the planner ``tier_mix`` telemetry).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = [
    "COST_RUNS_KEY",
    "aggregate_objective_cost",
    "cost_summary_line",
    "empty_ticket_cost",
    "objective_cost_section",
    "pedido_cost_line",
    "normalize_run",
    "record_money",
    "record_run",
    "record_runs",
    "run_from_exec_result",
    "run_from_session",
]

#: ``graph_out`` key carrying this execution's run records to ``run_task``.
COST_RUNS_KEY = "cost_runs"

#: Closed per-engine usage vocabularies, most specific first. Engines name
#: the same counters differently; anything outside these keys is ignored,
#: never guessed.
_TOKENS_IN_KEYS = ("tokens_in", "input_tokens", "prompt_tokens")
_TOKENS_OUT_KEYS = ("tokens_out", "output_tokens", "completion_tokens")
_TOKENS_CACHED_KEYS = ("tokens_cached", "cached_tokens", "cache_tokens")
_DURATION_KEYS = ("duration_s", "elapsed_s", "elapsed")


def _as_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _as_non_negative_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if result >= 0 else None
    return None


def _as_label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if mapping.get(key) is not None:
            return mapping.get(key)
    nested = mapping.get("usage")
    if isinstance(nested, Mapping):
        for key in keys:
            if nested.get(key) is not None:
                return nested.get(key)
    return None


def normalize_run(run: Mapping[str, Any] | None) -> dict[str, Any]:
    """One executor invocation as a canonical run record. Best-effort.

    Token keys are read top-level first, then under a nested ``usage``
    mapping (the :class:`ExecResult` shape). Unparseable values degrade
    to null, never to 0 and never raising: telemetry must not break
    execution.
    """
    source = run if isinstance(run, Mapping) else {}
    return {
        "tokens_in": _as_non_negative_int(
            _first(source, _TOKENS_IN_KEYS)),
        "tokens_out": _as_non_negative_int(
            _first(source, _TOKENS_OUT_KEYS)),
        "tokens_cached": _as_non_negative_int(
            _first(source, _TOKENS_CACHED_KEYS)),
        "duration_s": _as_non_negative_float(
            _first(source, _DURATION_KEYS)),
        "engine": _as_label(source.get("engine")),
        "model": _as_label(source.get("model")),
    }


def run_from_exec_result(
    *,
    usage: Mapping[str, Any] | None,
    elapsed_s: float,
    engine: str,
    model: str = "",
) -> dict[str, Any]:
    """Run record from a one-shot executor result (:class:`ExecResult`)."""
    return normalize_run({
        "engine": engine,
        "model": model,
        "duration_s": elapsed_s,
        "usage": dict(usage or {}),
    })


def run_from_session(session: Mapping[str, Any]) -> dict[str, Any]:
    """Run record from a Tier S session entry (``SessionMetric`` dict)."""
    source = session if isinstance(session, Mapping) else {}
    return normalize_run({
        "engine": source.get("slot") or source.get("role"),
        "model": source.get("model"),
        "duration_s": source.get("duration_s"),
        "usage": source.get("usage"),
    })


def empty_ticket_cost() -> dict[str, Any]:
    """Fold seed. Never persisted: recording always adds at least one run."""
    return {
        "runs": 0,
        "tokens_in": None,
        "tokens_out": None,
        "tokens_cached": None,
        "tokens_unknown_runs": 0,
        "duration_s": 0.0,
        "duration_unknown_runs": 0,
        "engines": [],
        "models": [],
        "money": None,
        "money_currency": None,
    }


def _as_cost(cost: Mapping[str, Any] | None) -> dict[str, Any]:
    base = empty_ticket_cost()
    if not isinstance(cost, Mapping):
        return base
    for key in ("runs", "tokens_unknown_runs", "duration_unknown_runs"):
        value = cost.get(key)
        if isinstance(value, int) and not isinstance(value, bool) \
                and value >= 0:
            base[key] = value
    for key in ("tokens_in", "tokens_out", "tokens_cached"):
        value = cost.get(key)
        if isinstance(value, int) and not isinstance(value, bool) \
                and value >= 0:
            base[key] = value
    duration = cost.get("duration_s")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
            and duration >= 0:
        base["duration_s"] = float(duration)
    for key in ("engines", "models"):
        items = cost.get(key)
        if isinstance(items, (list, tuple)):
            seen: set[str] = set()
            ordered: list[str] = []
            for item in items:
                label = _as_label(item)
                if label is not None and label not in seen:
                    seen.add(label)
                    ordered.append(label)
            base[key] = ordered
    money = cost.get("money")
    if isinstance(money, str) and money.strip():
        base["money"] = money.strip()
        currency = cost.get("money_currency")
        if isinstance(currency, str) and currency.strip():
            base["money_currency"] = currency.strip()
    return base


def record_run(
    cost: Mapping[str, Any] | None,
    run: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Fold one normalized run onto a ticket cost. Pure."""
    folded = _as_cost(cost)
    item = normalize_run(run if isinstance(run, Mapping) else {})
    folded["runs"] += 1
    measured_tokens = False
    for key in ("tokens_in", "tokens_out", "tokens_cached"):
        value = item[key]
        if value is None:
            continue
        measured_tokens = True
        folded[key] = (folded[key] or 0) + value
    if not measured_tokens:
        folded["tokens_unknown_runs"] += 1
    if item["duration_s"] is None:
        folded["duration_unknown_runs"] += 1
    else:
        folded["duration_s"] += item["duration_s"]
    for key, field in (("engine", "engines"), ("model", "models")):
        label = item[key]
        if label is not None and label not in folded[field]:
            folded[field] = [*folded[field], label]
    folded["engines"] = sorted(folded["engines"])
    folded["models"] = sorted(folded["models"])
    return folded


def record_runs(
    cost: Mapping[str, Any] | None,
    runs: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Fold a whole execution's runs onto a ticket cost. Pure."""
    folded = _as_cost(cost)
    for run in runs or []:
        folded = record_run(folded, run)
    return folded


def record_money(
    cost: Mapping[str, Any] | None,
    *,
    amount: str,
    currency: str,
) -> dict[str, Any]:
    """Attach ERPNext money to a ticket cost. Pure.

    The seam for the dated finance reader: until one lands, money stays
    null (blank amounts are refused, never stored as zero).
    """
    folded = _as_cost(cost)
    text = (amount or "").strip()
    code = (currency or "").strip()
    if not text or not code:
        return folded
    folded["money"] = text
    folded["money_currency"] = code
    return folded


def aggregate_objective_cost(
    costs: Sequence[Mapping[str, Any] | None] | None,
) -> dict[str, Any] | None:
    """Sum ticket costs into the objective view. Null when nothing ran."""
    total = empty_ticket_cost()
    for cost in costs or []:
        item = _as_cost(cost)
        total["runs"] += item["runs"]
        for key in ("tokens_in", "tokens_out", "tokens_cached"):
            if item[key] is not None:
                total[key] = (total[key] or 0) + item[key]
        total["tokens_unknown_runs"] += item["tokens_unknown_runs"]
        total["duration_s"] += item["duration_s"]
        total["duration_unknown_runs"] += item["duration_unknown_runs"]
        for key in ("engines", "models"):
            merged = list(total[key])
            for label in item[key]:
                if label not in merged:
                    merged.append(label)
            total[key] = sorted(merged)
    if total["runs"] == 0:
        return None
    return total


def cost_summary_line(cost: Mapping[str, Any] | None) -> str:
    """Compact cost line for prompts (CEO, planner).

    The stored mapping is normalized first: a legacy or hand-edited
    cost renders as unmeasured, never raises (review 00013/H5).
    """
    if not isinstance(cost, Mapping):
        return "(no measured runs)"
    view = _as_cost(cost)
    if view["runs"] <= 0:
        return "(no measured runs)"
    parts = [f"runs={view['runs']}"]
    tokens = []
    for key in ("tokens_in", "tokens_out", "tokens_cached"):
        value = view[key]
        tokens.append(f"{key}={value if value is not None else '?'}")
    unknown = int(view["tokens_unknown_runs"])
    if unknown:
        tokens.append(f"{unknown} run(s) unmeasured")
    parts.append(f"tokens[{', '.join(tokens)}]")
    duration = view["duration_s"]
    parts.append(f"duration_s={float(duration):.1f}")
    if int(view["duration_unknown_runs"]):
        parts[-1] += (f" (+{int(view['duration_unknown_runs'])} "
                      "unmeasured)")
    engines = list(view["engines"])
    if engines:
        parts.append(f"engines=[{', '.join(engines)}]")
    money = view["money"]
    if money is None:
        parts.append("money=unknown")
    else:
        parts.append(f"money={money} {view['money_currency'] or ''}"
                     .strip())
    return " ".join(parts)


def objective_cost_section(cost: Mapping[str, Any] | None) -> str:
    """Planner/CEO cost section. Empty when nothing ran (callers skip it)."""
    if not isinstance(cost, Mapping) or _as_cost(cost)["runs"] <= 0:
        return ""
    return (
        "MEASURED COST (tickets so far; information only, never a "
        "threshold):\n"
        f"- {cost_summary_line(cost)}"
    )


def pedido_cost_line(cost: Mapping[str, Any] | None) -> str:
    """The pedido's own prior runs, for replans. Empty when nothing ran."""
    if not isinstance(cost, Mapping) or _as_cost(cost)["runs"] <= 0:
        return ""
    return f"PRIOR RUNS: {cost_summary_line(cost)}"
