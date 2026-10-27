"""Per-profile ranking: unification, scoring, retention (TASK-2026-00292).

Pipeline (SPEC §8, §12.2, as corrected by TASK-2026-00319): capability
gates BEFORE ordering, weighted percentile scoring (§8.3), an
experimental bucket listing thin evidence, top-3 retention until the
challenger repeats, and atomic publication with provenance (T19).
Benchmarks, serve evidence and quota ORDER the single multi-provider
pool; no numeric floor or drop excludes a registered live model.
Consumers freeze one document per round and never see a mid-round
swap (T12).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Container, NamedTuple

from pydantic import ValidationError

from model_routing import benchmarks as router_benchmarks
from model_routing.availability import ProfileRequirements, meets_profile
from model_routing.benchmarks import AliasFile
from model_routing.models import (
    RANKINGS_SCHEMA_VERSION,
    BenchmarkScore,
    Capabilities,
    CatalogDocument,
    CatalogEntry,
    Confidentiality,
    Deployment,
    ExperimentalEntry,
    PendingTop3,
    ProfileRanking,
    RankedDeployment,
    RankedEntry,
    RankingProvenance,
    RankingsDocument,
    RouterPolicy,
    is_usable,
)
from model_routing.paths import HISTORY_DIRNAME, RANKINGS_FILENAME
from model_routing.publish import PublishError, atomic_write_json

__all__ = [
    "RANK_PROFILES",
    "RESEARCH_CONTEXT_FALLBACK_TOKENS",
    "RESEARCH_CONTEXT_PREFERRED_TOKENS",
    "ExplainedModel",
    "RankedCatalog",
    "RankingError",
    "RankingsSnapshot",
    "ResolvedScoring",
    "fallback_chain",
    "load_rankings",
    "profile_requirements",
    "publish_rankings",
    "rank_catalog",
    "resolve_scoring",
    "score_dimensions",
    "unify_canonical",
]

RANK_PROFILES = ("free-research", "free-review", "free-general")

RESEARCH_CONTEXT_PREFERRED_TOKENS = 131072
RESEARCH_CONTEXT_FALLBACK_TOKENS = 65536

_DEFAULT_WEIGHTS = {
    "free-research": {"coding": 0.20, "agentic": 0.45, "intelligence": 0.35},
    "free-review": {"coding": 0.60, "agentic": 0.30, "intelligence": 0.10},
    "free-general": {"coding": 0.25, "agentic": 0.25, "intelligence": 0.50},
}
_DEFAULT_FLOORS = {"free-research": 0.60, "free-review": 0.65, "free-general": 0.45}
_DEFAULT_DROPS = {"free-research": 0.20, "free-review": 0.15, "free-general": 0.25}

_RANK_REQUIREMENTS = {
    "free-research": ProfileRequirements(
        needs_tools=True,
        min_context_tokens=RESEARCH_CONTEXT_PREFERRED_TOKENS,
        confidentiality=Confidentiality.PUBLIC,
    ),
    "free-review": ProfileRequirements(
        needs_tools=True,
        min_context_tokens=65536,
        confidentiality=Confidentiality.PUBLIC,
    ),
    "free-general": ProfileRequirements(
        min_context_tokens=16384,
        confidentiality=Confidentiality.PUBLIC,
    ),
}

_TOP3_SIZE = 3
_PROMOTE_AFTER_REPEATS = 2


class RankingError(RuntimeError):
    """Ranking input invalid; nothing is published."""


class ResolvedScoring(NamedTuple):
    """Effective weights, floor and drop for one profile."""

    weights: dict[str, float]
    quality_floor: float
    max_quality_drop: float


class RankedCatalog(NamedTuple):
    """One profile order before top-3 retention."""

    order: list[RankedEntry]
    experimental: list[ExperimentalEntry]
    context_floor: int | None


@dataclass(frozen=True)
class RankingsSnapshot:
    """Provenance of one rankings publication."""

    snapshot_id: str
    sha256: str
    created_at: str


@dataclass(frozen=True)
class ExplainedModel:
    """Why one model ranks, waits in experimental, or is excluded."""

    subject: str
    profile: str
    verdict: str
    rank: int | None = None
    quality: float | None = None
    reasons: tuple[str, ...] = ()


def profile_requirements(profile: str) -> ProfileRequirements:
    """Hard gates one profile applies; unknown profiles refuse (addendum §3)."""
    try:
        return _RANK_REQUIREMENTS[profile]
    except KeyError:
        raise ValueError(
            f"unknown profile: {profile!r} (known: {sorted(RANK_PROFILES)})") from None


def resolve_scoring(profile: str, policy: RouterPolicy | None = None) -> ResolvedScoring:
    """Merge policy overrides over the SPEC §8.3 weights and §18 floors."""
    if profile not in RANK_PROFILES:
        raise ValueError(f"unknown profile: {profile!r} (known: {sorted(RANK_PROFILES)})")
    weights = dict(_DEFAULT_WEIGHTS[profile])
    floor = _DEFAULT_FLOORS[profile]
    drop = _DEFAULT_DROPS[profile]
    override = (policy.profiles.get(profile) if policy else None)
    if override is not None:
        if override.weights is not None:
            weights = dict(override.weights)
        if override.quality_floor is not None:
            floor = override.quality_floor
        if override.max_quality_drop is not None:
            drop = override.max_quality_drop
    return ResolvedScoring(weights, floor, drop)


def _dimension_value(name: str, benchmarks: dict[str, BenchmarkScore]) -> float | None:
    if name in benchmarks:
        if name == "agentic" and "arena" in benchmarks:
            return (benchmarks["agentic"].score + benchmarks["arena"].score) / 2.0
        return benchmarks[name].score
    if name == "agentic" and "arena" in benchmarks:
        return benchmarks["arena"].score
    return None


def score_dimensions(
    benchmarks: dict[str, BenchmarkScore], weights: dict[str, float]
) -> tuple[float, float, dict[str, float]]:
    """SPEC §8.3 quality: weighted present dimensions times coverage."""
    total = sum(weights.values())
    dims: dict[str, float] = {}
    for name in weights:
        value = _dimension_value(name, benchmarks)
        if value is not None:
            dims[name] = value
    present = sum(weights[name] for name in dims)
    coverage = present / total if total > 0 else 0.0
    quality = sum(weights[name] * dims[name] for name in dims) * coverage
    return quality, coverage, dims


def _merge_capabilities(entries: list[CatalogEntry]) -> Capabilities:
    contexts = [entry.capabilities.context_tokens for entry in entries
                if entry.capabilities.context_tokens is not None]
    return Capabilities(
        tools=any(entry.capabilities.tools for entry in entries),
        streaming=any(entry.capabilities.streaming for entry in entries),
        context_tokens=max(contexts) if contexts else None,
        verified_in_opencode=any(entry.capabilities.verified_in_opencode for entry in entries),
    )


def unify_canonical(document: CatalogDocument, aliases: AliasFile) -> CatalogDocument:
    """Merge provider-scoped entries sharing one alias canonical model."""
    groups: dict[tuple[str, str], list[CatalogEntry]] = {}
    order: list[tuple[str, str]] = []
    leftovers: list[CatalogEntry] = []
    for entry in document.entries:
        hit = None
        for deployment in entry.deployments:
            if deployment.gateway_model_id is None:
                continue
            hit = router_benchmarks.try_resolve(
                deployment.provider, deployment.gateway_model_id, aliases)
            if hit is not None:
                break
        if hit is None:
            leftovers.append(entry)
            continue
        key = (hit.family_id, hit.canonical_id)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)
    merged: list[CatalogEntry] = []
    for key in order:
        members = groups[key]
        deployments: list[Deployment] = []
        for member in members:
            deployments.extend(member.deployments)
        benchmarks: dict[str, BenchmarkScore] = {}
        for member in members:
            benchmarks.update(member.benchmarks)
        display = next((member.display_name for member in members if member.display_name),
                       key[1])
        merged.append(CatalogEntry(
            family_id=key[0],
            canonical_model_id=key[1],
            display_name=display,
            benchmarks=benchmarks,
            capabilities=_merge_capabilities(members),
            deployments=deployments,
        ))
    merged.extend(leftovers)
    try:
        return CatalogDocument.model_validate({
            "schema_version": document.schema_version,
            "generated_at": document.generated_at.isoformat() if document.generated_at else None,
            "generator": document.generator,
            "stale": document.stale,
            "stale_reason": document.stale_reason,
            "entries": [entry.model_dump() for entry in merged],
        })
    except ValueError as exc:
        raise RankingError(f"canonical unification collides: {exc}") from exc


def _known_limits(deployment: Deployment) -> int:
    limits = deployment.limits
    return sum(value is not None for value in (limits.rpm, limits.rpd, limits.tpm, limits.tpd))


def _route_key(deployment: Deployment) -> tuple[int, str, str]:
    return (-_known_limits(deployment), deployment.provider, deployment.deployment_id)


def _eligible_routes(
    entry: CatalogEntry,
    requirements: ProfileRequirements,
    policy: RouterPolicy,
) -> tuple[list[Deployment], list[str]]:
    """Routes registered as free and alive; verification only orders.

    TASK-2026-00319: ``free_offered`` (or legacy ``free_verified``)
    admits the route. Serve evidence and quota never exclude here;
    they surface later as ordering signals.
    """
    routes: list[Deployment] = []
    reasons: list[str] = []
    for deployment in entry.deployments:
        if not (deployment.free_offered or deployment.free_verified):
            reasons.append(f"{deployment.deployment_id}: not registered free")
            continue
        if not deployment.healthy:
            reasons.append(f"{deployment.deployment_id}: not healthy")
            continue
        if not is_usable(deployment, policy):
            reasons.append(f"{deployment.deployment_id}: fails the catalog price gate")
            continue
        ok, reason = meets_profile(entry, deployment, requirements)
        if not ok:
            reasons.append(f"{deployment.deployment_id}: {reason}")
            continue
        routes.append(deployment)
    routes.sort(key=_route_key)
    return routes, reasons


def _experimental_reason(count: int) -> str:
    minimum = router_benchmarks.EXPERIMENTAL_MIN_OBSERVATIONS
    if count == 0:
        return "no benchmark observations from any source"
    missing = minimum - count
    return f"only {count} benchmark observation(s); {missing} more needed out of experimental"


def rank_catalog(
    profile: str,
    catalog: CatalogDocument,
    policy: RouterPolicy | None = None,
) -> RankedCatalog:
    """Hard gates first, then quality order; thin evidence stays experimental."""
    if profile not in RANK_PROFILES:
        raise ValueError(f"unknown profile: {profile!r} (known: {sorted(RANK_PROFILES)})")
    active = policy or RouterPolicy()
    scoring = resolve_scoring(profile, active)
    requirements = _RANK_REQUIREMENTS[profile]
    context_floor = requirements.min_context_tokens
    ranked = _rank_with_requirements(catalog, scoring, requirements, active)
    if not ranked[0] and profile == "free-research":
        requirements = replace(
            requirements, min_context_tokens=RESEARCH_CONTEXT_FALLBACK_TOKENS)
        context_floor = RESEARCH_CONTEXT_FALLBACK_TOKENS
        ranked = _rank_with_requirements(catalog, scoring, requirements, active)
    return RankedCatalog(ranked[0], ranked[1], context_floor)


def _verified_count(entry: CatalogEntry) -> int:
    """Verified routes first: serve evidence and quota order, never exclude."""
    return sum(1 for route in entry.deployments if route.free_verified)


def _rank_with_requirements(
    catalog: CatalogDocument,
    scoring: ResolvedScoring,
    requirements: ProfileRequirements,
    policy: RouterPolicy,
) -> tuple[list[RankedEntry], list[ExperimentalEntry]]:
    """Order every registered live entry; nothing numeric discards.

    TASK-2026-00319: thin-evidence entries are listed under
    ``experimental`` for transparency AND ranked (low quality sorts
    them down). Quality floors and max drops survive in provenance
    only; they no longer exclude.
    """
    ordered: list[RankedEntry] = []
    experimental: list[ExperimentalEntry] = []
    verified_of: dict[str, int] = {}
    for entry in catalog.entries:
        routes, _reasons = _eligible_routes(entry, requirements, policy)
        if not routes:
            continue
        count = len(entry.benchmarks)
        if count < router_benchmarks.EXPERIMENTAL_MIN_OBSERVATIONS:
            experimental.append(ExperimentalEntry(
                canonical_model_id=entry.canonical_model_id,
                family_id=entry.family_id,
                reason=_experimental_reason(count),
                observations=count,
            ))
        quality, coverage, dims = score_dimensions(entry.benchmarks, scoring.weights)
        sources = sorted({score.source for score in entry.benchmarks.values() if score.source})
        primary, *backups = routes
        verified_of[entry.canonical_model_id] = _verified_count(entry)
        ordered.append(RankedEntry(
            rank=0,
            canonical_model_id=entry.canonical_model_id,
            family_id=entry.family_id,
            primary=RankedDeployment(
                deployment_id=primary.deployment_id, provider=primary.provider),
            backups=[RankedDeployment(deployment_id=route.deployment_id, provider=route.provider)
                     for route in backups],
            quality=quality,
            coverage=coverage,
            dimensions=dims,
            quality_sources=sources,
        ))
    ordered.sort(key=lambda item: (
        -item.quality,
        -verified_of.get(item.canonical_model_id, 0),
        _route_key(_route_of(catalog, item.primary.deployment_id)),
        item.canonical_model_id,
    ))
    for position, item in enumerate(ordered, start=1):
        ordered[position - 1] = item.model_copy(update={"rank": position})
    experimental.sort(key=lambda item: item.canonical_model_id)
    return ordered, experimental


def _route_of(catalog: CatalogDocument, deployment_id: str) -> Deployment:
    for entry in catalog.entries:
        for deployment in entry.deployments:
            if deployment.deployment_id == deployment_id:
                return deployment
    raise RankingError(f"ranked unknown deployment: {deployment_id}")


def apply_top3_retention(
    ordered: list[RankedEntry],
    previous: ProfileRanking | None,
    *,
    now: str,
    catalog_snapshot: str,
) -> tuple[list[RankedEntry], PendingTop3 | None]:
    """Hold a changed top-3 until the same challenger repeats (SPEC §8.3)."""
    proposed = [item.canonical_model_id for item in ordered[:_TOP3_SIZE]]
    if previous is None:
        return ordered, None
    before = [item.canonical_model_id for item in previous.order[:_TOP3_SIZE]]
    if proposed == before:
        return ordered, None
    pending = previous.pending_top3
    repeated = pending is not None and pending.proposed == proposed
    repeats = pending.consecutive_refreshes + 1 if repeated else 1
    if repeated and repeats >= _PROMOTE_AFTER_REPEATS:
        return ordered, None
    by_id = {item.canonical_model_id: item for item in ordered}
    retained = [by_id[candidate] for candidate in before if candidate in by_id]
    if not retained:
        return ordered, None
    rest = [item for item in ordered if item.canonical_model_id not in set(before)]
    effective = retained + rest
    for position, item in enumerate(effective, start=1):
        effective[position - 1] = item.model_copy(update={"rank": position})
    dropped = [candidate for candidate in before if candidate not in by_id]
    reason = "top-3 challenger held for one repeat; previous top-3 kept"
    if dropped:
        reason += f"; left the eligible set: {', '.join(dropped)}"
    return effective, PendingTop3(
        proposed=proposed,
        previous=before,
        first_seen=pending.first_seen if repeated else now,
        consecutive_refreshes=repeats,
        reason=reason,
        catalog_snapshot=catalog_snapshot,
    )


def fallback_chain(
    ranking: ProfileRanking,
    policy: RouterPolicy | None = None,
    *,
    down: Container[str] = (),
) -> list[RankedEntry]:
    """Ordered servable chain with no numeric discard (TASK-2026-00319).

    The published order is the chain: only ``down`` (really dead or
    already tried) removes an entry. Floors and drops survive in
    provenance as ordering context, never as gates.
    """
    chain: list[RankedEntry] = []
    for item in ranking.order:
        routes = [item.primary.deployment_id,
                  *(route.deployment_id for route in item.backups)]
        if item.canonical_model_id in down or all(route in down for route in routes):
            continue
        chain.append(item)
    return chain


def explain_model(
    subject: str,
    profile: str,
    catalog: CatalogDocument,
    rankings: RankingsDocument,
    policy: RouterPolicy | None = None,
) -> ExplainedModel:
    """Why one canonical model or deployment ranks, waits, or is excluded."""
    if profile not in RANK_PROFILES:
        raise ValueError(f"unknown profile: {profile!r} (known: {sorted(RANK_PROFILES)})")
    active = policy or RouterPolicy()
    ranking = rankings.profiles.get(profile)
    if ranking is not None:
        for item in ranking.order:
            routes = [item.primary.deployment_id,
                      *(route.deployment_id for route in item.backups)]
            if subject in (item.canonical_model_id, *routes):
                thin = any(exp.canonical_model_id == item.canonical_model_id
                           for exp in ranking.experimental)
                verdict = (f"ranked #{item.rank} (thin evidence)"
                           if thin else f"ranked #{item.rank}")
                return ExplainedModel(
                    subject=subject, profile=profile,
                    verdict=verdict,
                    rank=item.rank, quality=item.quality,
                    reasons=(f"primary route {item.primary.deployment_id} via "
                             f"{item.primary.provider}; {len(item.backups)} backup(s)",),
                )
        for item in ranking.experimental:
            if subject == item.canonical_model_id:
                return ExplainedModel(
                    subject=subject, profile=profile, verdict="experimental",
                    reasons=(item.reason,),
                )
    requirements = _RANK_REQUIREMENTS[profile]
    for entry in catalog.entries:
        if subject not in ([entry.canonical_model_id]
                           + [route.deployment_id for route in entry.deployments]):
            continue
        _routes, reasons = _eligible_routes(entry, requirements, active)
        scoring = resolve_scoring(profile, active)
        count = len(entry.benchmarks)
        quality, _coverage, _dims = score_dimensions(entry.benchmarks, scoring.weights)
        if count < router_benchmarks.EXPERIMENTAL_MIN_OBSERVATIONS:
            return ExplainedModel(
                subject=subject, profile=profile, verdict="ranked (thin evidence)",
                quality=quality,
                reasons=(_experimental_reason(count),),
            )
        return ExplainedModel(
            subject=subject, profile=profile, verdict="excluded",
            quality=quality,
            reasons=tuple(reasons) if reasons else ("no gate detail recorded",),
        )
    return ExplainedModel(
        subject=subject, profile=profile, verdict="unknown",
        reasons=(f"no catalog entry or ranked route matches {subject!r}",),
    )


def build_rankings(
    catalog: CatalogDocument,
    policy: RouterPolicy | None = None,
    *,
    previous: RankingsDocument | None = None,
    catalog_snapshot: str = "",
    catalog_sha256: str = "",
    benchmark_sources: list[str] | None = None,
    aa_index_version: float | None = None,
    lmarena_target: str | None = None,
    generator: str = "model_routing.ranking",
    now: datetime | None = None,
) -> RankingsDocument:
    """Rank every profile with retention and provenance (T19)."""
    active = policy or RouterPolicy()
    moment = now or datetime.now(timezone.utc)
    profiles: dict[str, ProfileRanking] = {}
    providers = sorted({route.provider for entry in catalog.entries for route in entry.deployments})
    for profile in RANK_PROFILES:
        ranked = rank_catalog(profile, catalog, active)
        scoring = resolve_scoring(profile, active)
        prior = previous.profiles.get(profile) if previous else None
        effective, pending = apply_top3_retention(
            ranked.order, prior, now=moment.isoformat(), catalog_snapshot=catalog_snapshot)
        profiles[profile] = ProfileRanking(
            profile=profile,
            order=effective,
            experimental=ranked.experimental,
            pending_top3=pending,
            provenance=RankingProvenance(
                catalog_snapshot=catalog_snapshot,
                catalog_sha256=catalog_sha256,
                sources=[*providers, *(benchmark_sources or [])],
                observed_at=moment.isoformat(),
                weights=scoring.weights,
                quality_floor=scoring.quality_floor,
                context_floor=ranked.context_floor,
                aa_index_version=aa_index_version,
                lmarena_target=lmarena_target,
            ),
        )
    return RankingsDocument(
        schema_version=RANKINGS_SCHEMA_VERSION,
        generated_at=moment,
        generator=generator,
        catalog_snapshot=catalog_snapshot,
        catalog_sha256=catalog_sha256,
        profiles=profiles,
    )


def load_rankings(path: Path) -> RankingsDocument:
    """Read and validate a published rankings document."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise PublishError(f"rankings missing or unreadable: {path}") from exc
    try:
        payload = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise PublishError(f"rankings {path} is not valid JSON") from exc
    try:
        document = RankingsDocument.model_validate(payload)
    except ValidationError as exc:
        raise PublishError(f"rankings {path} failed schema validation") from exc
    if document.schema_version != RANKINGS_SCHEMA_VERSION:
        raise PublishError(f"unsupported rankings schema_version: {document.schema_version}")
    return document


def publish_rankings(
    document: RankingsDocument,
    config_dir: Path,
    *,
    generator: str = "model_routing.ranking",
) -> RankingsSnapshot:
    """Validate and publish rankings plus an immutable history snapshot."""
    if document.schema_version != RANKINGS_SCHEMA_VERSION:
        raise RankingError(f"unsupported rankings schema_version: {document.schema_version}")
    stamped = document.model_copy(update={"generator": generator})
    payload = stamped.model_dump_json(indent=2)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    created = datetime.now(timezone.utc)
    snapshot_id = f"rankings-{created.strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}"
    try:
        atomic_write_json(config_dir / HISTORY_DIRNAME / f"{snapshot_id}.json", payload)
        atomic_write_json(config_dir / RANKINGS_FILENAME, payload)
    except OSError as exc:
        raise PublishError(f"publication failed for {config_dir / RANKINGS_FILENAME}") from exc
    RankingsDocument.model_validate(json.loads(payload))
    return RankingsSnapshot(
        snapshot_id=snapshot_id,
        sha256=digest,
        created_at=created.isoformat(),
    )
