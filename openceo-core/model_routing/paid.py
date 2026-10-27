"""Paid OpenRouter ranking for the review last resort (00310 follow-up).

Cheapest-first among paid models fit to review, auto-refreshed with the
catalog: the cascade paid slot serves the top entry while the static
config whitelist stays as the fallback. Benchmarks rarely cover the
cheapest models, so price leads and only benchmarked-below-floor sinks.
No network here; refresh feeds a catalog, the guard reads the file.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from model_routing.paths import HISTORY_DIRNAME, PAID_FILENAME
from model_routing.publish import atomic_write_json

__all__ = [
    "PAID_SCHEMA_VERSION",
    "PAID_TOP_N",
    "PAID_TTL",
    "PaidModel",
    "PaidRanking",
    "load_paid_ranking",
    "paid_providers_map",
    "paid_review_target",
    "publish_paid_ranking",
    "rank_paid_models",
    "rank_premium_models",
    "resolve_paid_model",
    "resolve_paid_review",
]

PAID_SCHEMA_VERSION = 1
PAID_TOP_N = 5
PAID_TTL = timedelta(days=7)

#: Review-sized worst case (tokens) for price ordering; reasoning or
#: search extras are not priced (the per-review cap bounds the damage).
WORST_CASE_INPUT_TOKENS = 150_000
WORST_CASE_OUTPUT_TOKENS = 5_000

#: Benchmarked models under this quality sink below unbenchmarked ones.
QUALITY_SINK_FLOOR = 0.4

#: Premium needs proven quality: benchmarked with enough observations.
PREMIUM_MIN_OBSERVATIONS = 3

MIN_REVIEW_CONTEXT_TOKENS = 65_536


@dataclass(frozen=True)
class PaidModel:
    """One ranked paid model (OpenRouter model id, no provider prefix)."""

    model_id: str
    input_price_usd: float
    output_price_usd: float
    worst_case_usd: float
    quality: float | None
    observations: int
    context_tokens: int


@dataclass(frozen=True)
class PaidRanking:
    """Published paid ranking with provenance."""

    generated_at: str
    generator: str
    catalog_snapshot: str
    models: list[PaidModel]
    premium: list[PaidModel] = field(default_factory=list)


def _quality(entry: Any) -> tuple[float | None, int]:
    scores = [item.score for item in entry.benchmarks.values()]
    if not scores:
        return None, 0
    return sum(scores) / len(scores), len(scores)


def _eligible(entry: Any, deployment: Any) -> bool:
    if deployment.provider != "openrouter" or deployment.free_verified:
        return False
    if deployment.route_type == "opencode_native_free":
        return False
    caps = entry.capabilities
    if not caps.tools or (caps.context_tokens or 0) < MIN_REVIEW_CONTEXT_TOKENS:
        return False
    prices = (deployment.input_price_usd, deployment.output_price_usd)
    if any(item is None or item < 0 for item in prices):
        return False
    if deployment.input_price_usd == 0 and deployment.output_price_usd == 0:
        return False
    model_id = deployment.model_id
    if model_id.startswith("~") or ":batch" in model_id:
        return False
    return True


def rank_paid_models(catalog: Any, *, top_n: int = PAID_TOP_N) -> list[PaidModel]:
    """Cheapest-first paid models fit to review (price leads, bad sinks)."""
    ranked: list[tuple[tuple, PaidModel]] = []
    for entry in catalog.entries:
        for deployment in entry.deployments:
            if not _eligible(entry, deployment):
                continue
            quality, observations = _quality(entry)
            sunk = quality is not None and quality < QUALITY_SINK_FLOOR
            worst = (deployment.input_price_usd * WORST_CASE_INPUT_TOKENS
                     + deployment.output_price_usd * WORST_CASE_OUTPUT_TOKENS)
            ranked.append((
                (sunk, worst, -(entry.capabilities.context_tokens or 0),
                 deployment.model_id),
                PaidModel(
                    model_id=deployment.model_id,
                    input_price_usd=deployment.input_price_usd,
                    output_price_usd=deployment.output_price_usd,
                    worst_case_usd=worst, quality=quality,
                    observations=observations,
                    context_tokens=entry.capabilities.context_tokens or 0),
            ))
    ranked.sort(key=lambda item: item[0])
    seen: set[str] = set()
    models: list[PaidModel] = []
    for _key, model in ranked:
        if model.model_id in seen:
            continue
        seen.add(model.model_id)
        models.append(model)
        if len(models) >= top_n:
            break
    return models


def rank_premium_models(catalog: Any, *, top_n: int = PAID_TOP_N) -> list[PaidModel]:
    """Best proven paid models (quality first); empty without benchmarks."""
    ranked: list[tuple[tuple, PaidModel]] = []
    for entry in catalog.entries:
        quality, observations = _quality(entry)
        if quality is None or observations < PREMIUM_MIN_OBSERVATIONS:
            continue
        if quality < QUALITY_SINK_FLOOR:
            continue
        for deployment in entry.deployments:
            if not _eligible(entry, deployment):
                continue
            worst = (deployment.input_price_usd * WORST_CASE_INPUT_TOKENS
                     + deployment.output_price_usd * WORST_CASE_OUTPUT_TOKENS)
            ranked.append((
                (-quality, worst, deployment.model_id),
                PaidModel(
                    model_id=deployment.model_id,
                    input_price_usd=deployment.input_price_usd,
                    output_price_usd=deployment.output_price_usd,
                    worst_case_usd=worst, quality=quality,
                    observations=observations,
                    context_tokens=entry.capabilities.context_tokens or 0),
            ))
    ranked.sort(key=lambda item: item[0])
    seen: set[str] = set()
    models: list[PaidModel] = []
    for _key, model in ranked:
        if model.model_id in seen:
            continue
        seen.add(model.model_id)
        models.append(model)
        if len(models) >= top_n:
            break
    return models


def _dump_model(item: PaidModel) -> dict[str, Any]:
    return {"model_id": item.model_id,
            "input_price_usd": item.input_price_usd,
            "output_price_usd": item.output_price_usd,
            "worst_case_usd": item.worst_case_usd,
            "quality": item.quality, "observations": item.observations,
            "context_tokens": item.context_tokens}


def _load_model(item: Any) -> PaidModel | None:
    if not isinstance(item, dict):
        return None
    try:
        return PaidModel(
            model_id=str(item["model_id"]),
            input_price_usd=float(item["input_price_usd"]),
            output_price_usd=float(item["output_price_usd"]),
            worst_case_usd=float(item["worst_case_usd"]),
            quality=(None if item.get("quality") is None
                     else float(item["quality"])),
            observations=int(item["observations"]),
            context_tokens=int(item["context_tokens"]))
    except (KeyError, TypeError, ValueError):
        return None


def publish_paid_ranking(config_dir: Path, catalog: Any, *,
                         generator: str, catalog_snapshot: str,
                         top_n: int = PAID_TOP_N) -> PaidRanking:
    """Rank from the catalog and publish paid.json plus a history copy."""
    models = rank_paid_models(catalog, top_n=top_n)
    premium = rank_premium_models(catalog, top_n=top_n)
    created = datetime.now(timezone.utc)
    ranking = PaidRanking(
        generated_at=created.isoformat(), generator=generator,
        catalog_snapshot=catalog_snapshot, models=models, premium=premium)
    payload = json.dumps({
        "schema_version": PAID_SCHEMA_VERSION,
        "generated_at": ranking.generated_at,
        "generator": ranking.generator,
        "catalog_snapshot": ranking.catalog_snapshot,
        "models": [_dump_model(item) for item in models],
        "premium": [_dump_model(item) for item in premium],
    }, indent=2)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    snapshot_id = f"paid-{created.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"
    atomic_write_json(config_dir / HISTORY_DIRNAME / f"{snapshot_id}.json", payload)
    atomic_write_json(config_dir / PAID_FILENAME, payload)
    return ranking


def load_paid_ranking(path: Path, *, now: datetime | None = None) -> PaidRanking | None:
    """Fresh paid ranking or None (missing, stale, invalid → static wins)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != PAID_SCHEMA_VERSION:
        return None
    try:
        generated = datetime.fromisoformat(str(raw.get("generated_at")))
    except (TypeError, ValueError):
        return None
    moment = now or datetime.now(timezone.utc)
    if moment - generated > PAID_TTL:
        return None
    models: list[PaidModel] = []
    entries = raw.get("models")
    if not isinstance(entries, list) or not entries:
        return None
    for item in entries:
        model = _load_model(item)
        if model is None:
            return None
        models.append(model)
    premium: list[PaidModel] = []
    raw_premium = raw.get("premium", [])
    if not isinstance(raw_premium, list):
        return None
    for item in raw_premium:
        model = _load_model(item)
        if model is None:
            return None
        premium.append(model)
    return PaidRanking(
        generated_at=str(raw.get("generated_at")),
        generator=str(raw.get("generator") or ""),
        catalog_snapshot=str(raw.get("catalog_snapshot") or ""),
        models=models, premium=premium)


def resolve_paid_model(path: Path) -> PaidModel | None:
    """Top paid model when the ranking is fresh, else None (static wins)."""
    ranking = load_paid_ranking(path)
    if ranking is None:
        return None
    return ranking.models[0]


def paid_providers_map(models: list[PaidModel]) -> dict[str, Any]:
    """OpenCode provider entry serving the ranked paid models."""
    return {"openrouter": {
        "name": "Paid OpenRouter (auto-ranked)",
        "package": "@opencode/ai/providers/openai-compatible",
        "env": ["FREE_ROUTER_OPENROUTER_API_KEY"],
        "settings": {
            "baseURL": "https://openrouter.ai/api/v1",
            "apiKey": "{env:FREE_ROUTER_OPENROUTER_API_KEY}",
        },
        "models": {item.model_id: {"name": item.model_id} for item in models},
    }}


def paid_review_target(models: list[PaidModel]) -> dict[str, Any]:
    """Hardened review target over the ranked paid models."""
    return {"env_var": "FREE_ROUTER_OPENROUTER_API_KEY",
            "providers": paid_providers_map(models)}


def resolve_paid_review(
    command: list[str], *, path: Path | None = None,
    premium: bool = False,
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """Dynamic paid slot: fresh top model, target and argv, else static.

    Returns ``(target, argv, model_id)`` with the auto-ranked top model;
    a missing or stale ranking returns ``(None, command, None)`` and the
    caller keeps the static slot untouched. Premium serves the proven
    list and degrades to the cheap list when it is empty (same rule as
    the spend guard, so both sides always agree).
    """
    from model_routing.paths import paid_path

    ranking = load_paid_ranking(path or paid_path())
    if ranking is None:
        return None, command, None
    shortlist = (ranking.premium if premium and ranking.premium
                 else ranking.models)
    top = shortlist[0]
    model_arg = f"openrouter/{top.model_id}"
    argv = list(command)
    for flag in ("-m", "--model"):
        if flag in argv:
            argv[argv.index(flag) + 1:argv.index(flag) + 2] = [model_arg]
            break
    else:
        argv += ["-m", model_arg]
    return paid_review_target(shortlist), argv, model_arg
