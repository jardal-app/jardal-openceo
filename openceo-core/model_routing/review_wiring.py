"""Feed dev review slots from the free-review ranking (TASK-2026-00293).

SPEC §11 as code: the two best eligible free-review models of
verifiably distinct families feed the free OpenCode review slots of
tiers A/B/C/S (SPEC §11.1, §11.2); round 2 follows ``reuse_best_plus_novel``
by default with ``four_distinct`` and ``reuse_top_two`` configurable
(SPEC §11.3); one failed reviewer restarts alone on the next ranked
model over the same snapshot and prompt (§11.6); every valid report
carries an extended receipt (§11.7).

Only OpenCode-served routes feed review slots: reviewers run the
``opencode`` CLI without credentials, so a ranked entry counts only
when its primary route (or one backup) runs on the ``opencode``
provider. Anything else (missing, expired or pair-less snapshot,
disabled switch) falls back to today's static pool: ``mimo``,
``nemotron`` and the ``reviewer_models.yaml`` reserves, unchanged.
Non-free slots (dsh, claudep, gemini, muse-review) never change.

Addendum v1.2 (TASK-2026-00306): when a v2 catalog is published next
to the rankings, the fed slots take the ``native_first`` list of the
``free-review`` profile instead: group A OpenCode Free targets first,
then group B external targets configured directly in OpenCode, every
reviewer launched with an explicit ``--model`` and restarted alone
from the same snapshot and prompt on the next eligible target when
it fails recoverably. Group B credentials reach the fenced run
through the audited handoff of ``reviewer_isolation`` (references in
the config, values only in the process env, ``/proc`` denied to the
review tools). Without a catalog the legacy OpenCode-only path above
still serves; the manual list and ``reviewer_models.yaml`` stay as
the respaldo.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Container, Mapping, Sequence

from model_routing import serve_evidence as router_serve_evidence
from model_routing.availability import (
    AvailabilityStore,
    Candidate,
    Exclusions,
    NoEligibleCandidate,
    ReservationConflict,
    get_next_candidate,
    record_provider_error,
    release_reservation,
    reserve_model,
    write_audit_event,
)
from model_routing.errors import ClassifiedError, classify_text, classify_timeout
from model_routing.external import (
    ExternalError,
    build_external_providers_config,
    credential_env_for,
)
from model_routing.models import (
    CatalogDocument,
    CatalogEntry,
    Deployment,
    ProfileRanking,
    RankingsDocument,
)
from model_routing.paths import catalog_path, rankings_path
from model_routing.probes import read_instance_models
from model_routing.publish import PublishError, load_catalog_doc
from model_routing.ranking import fallback_chain, load_rankings
from model_routing.secrets import SecretsError, load_router_secrets
from model_routing.selector import NoEligibleTarget, select_targets
from orchestration.review import ReviewSlot

__all__ = [
    "DEFAULT_ROUND2_POLICY",
    "DEFAULT_SNAPSHOT_MAX_AGE_HOURS",
    "FREE_REVIEW_PROFILE",
    "HARDENED_MARK",
    "REVIEW_FALLBACK_MAX",
    "REVIEW_SLOT_LABELS",
    "REVIEW_SLOT_PRIORITIES",
    "ROTATE_CLASSES",
    "ROUND2_POLICIES",
    "UNVERIFIED_FAMILIES",
    "Assembly",
    "GROUP_B_CONFIG_SCHEMA",
    "LabelState",
    "NoReviewPair",
    "R1Result",
    "REVIEW_TARGET_SPEC",
    "ReviewReceipt",
    "ReviewRoutingError",
    "ReviewTarget",
    "RoundBlocked",
    "RoundRouting",
    "RoundSnapshot",
    "assemble_review_pool",
    "build_receipt",
    "derive_slot",
    "dynamic_entry",
    "free_router_settings",
    "load_round_snapshot",
    "load_valid_receipt",
    "native_review_targets",
    "pick_pair",
    "plan_fallback",
    "receipt_filenames",
    "record_receipt",
    "review_candidates",
    "select_round2_pair",
    "static_exclusions",
    "sync_executor_pool",
]

logger = logging.getLogger(__name__)

FREE_REVIEW_PROFILE = "free-review"

#: Stable labels of the ranking-fed slots; models rotate under them.
REVIEW_SLOT_LABELS = ("free-review-1", "free-review-2")

#: Same priorities the static mimo/nemotron slots hold today, so
#: round 1 keeps the free pair first and its priority order otherwise.
REVIEW_SLOT_PRIORITIES = (1, 1)

ROUND2_POLICIES = ("reuse_best_plus_novel", "four_distinct", "reuse_top_two")
DEFAULT_ROUND2_POLICY = "reuse_best_plus_novel"

#: Rankings republish about every 6 h; past this age the order is too
#: stale to feed a round and the static pool takes over.
DEFAULT_SNAPSHOT_MAX_AGE_HOURS = 30

#: §11.6 restarts per slot per round; relief waves cover the rest.
REVIEW_FALLBACK_MAX = 2

#: Raw-spec marker the live executor honors with the §14.3 fence.
HARDENED_MARK = "review_hardened"

#: Families that never count toward the strict quorum (SPEC §11.2).
UNVERIFIED_FAMILIES = frozenset({"", "unknown", "unknown-family", "anonymous"})

#: §9.2 classes that rotate the reviewer to the next ranked model.
#: ``forbidden`` stays out on purpose: a free-tier 403 signals the
#: tool frontier, not a dead model, and rotating would burn models
#: without diagnosing the config (TASK-2026-00288 handoff).
#: ``bad_response`` stays out too: malformed reports retry on the
#: same model through the existing retryable kinds.
ROTATE_CLASSES = frozenset({
    "rate_limited", "daily_quota", "server_error", "auth", "billing",
    "not_found", "tools_unsupported", "context_exceeded", "stream_cut",
})


class ReviewRoutingError(RuntimeError):
    """Reviewer feeding misconfigured or unreadable; the message says why."""


class NoReviewPair(ReviewRoutingError):
    """The frozen ranking yields no two distinct verified families (T07)."""


class RoundBlocked(ReviewRoutingError):
    """``four_distinct`` cannot plan four families across both rounds."""


#: Spec key carrying one group B handoff (names and config, no values).
REVIEW_TARGET_SPEC = "review_target"

#: OpenCode config grammar of the installed CLI (v2.0.22, TASK-2026-00305).
GROUP_B_CONFIG_SCHEMA = "v2"


@dataclass(frozen=True)
class ReviewTarget(Candidate):
    """One native_first review candidate (addendum v1.2 §3, §5.1).

    Group A carries the real native ids; group B additionally names
    its credential env var and embeds the single-provider OpenCode
    config built at assembly time, with ``{env:...}`` references only.
    The availability flow (pair pick, reservations, §11.6 walk) only
    reads the :class:`Candidate` fields.
    """

    route_type: str = "opencode_native_free"
    provider_id: str = ""
    model_id: str = ""
    opencode_model: str = ""
    credential_env: str = ""
    provider_config: dict[str, Any] | None = None

    @property
    def is_external(self) -> bool:
        """True for group B targets served through a configured provider."""
        return self.route_type == "external_free"

    def model_arg(self) -> str:
        """Explicit ``opencode run --model`` value (never an alias)."""
        return self.opencode_model or self.deployment_id


def _model_arg(candidate: Candidate) -> str:
    if isinstance(candidate, ReviewTarget):
        return candidate.model_arg()
    return candidate.deployment_id


def _target_group(candidate: Candidate) -> str:
    if isinstance(candidate, ReviewTarget) and candidate.is_external:
        return "B"
    return "A"


def _target_route(candidate: Candidate) -> str:
    if isinstance(candidate, ReviewTarget) and candidate.route_type:
        return candidate.route_type
    return "opencode_native_free"


def _target_ids(candidate: Candidate) -> tuple[str, str]:
    if isinstance(candidate, ReviewTarget) and candidate.provider_id:
        return candidate.provider_id, candidate.model_id
    return candidate.provider, candidate.deployment_id


def free_router_settings(review_cfg: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalized ``review.free_router`` knobs; active by Owner decision."""
    raw = (review_cfg or {}).get("free_router", {})
    if not isinstance(raw, Mapping):
        raise ReviewRoutingError("review.free_router must be a mapping or absent")
    try:
        max_age = float(raw.get("snapshot_max_age_hours", DEFAULT_SNAPSHOT_MAX_AGE_HOURS))
    except (TypeError, ValueError) as exc:
        raise ReviewRoutingError(
            "review.free_router.snapshot_max_age_hours must be a number") from exc
    if not max_age > 0:
        raise ReviewRoutingError(
            "review.free_router.snapshot_max_age_hours must be above zero")
    policy = str(raw.get("round2_policy") or DEFAULT_ROUND2_POLICY).strip()
    if policy not in ROUND2_POLICIES:
        raise ReviewRoutingError(
            f"unknown round2_policy {policy!r} (known: {', '.join(ROUND2_POLICIES)})")
    return {
        "enabled": raw.get("enabled", True) is not False,
        "round2_policy": policy,
        "snapshot_max_age_hours": max_age,
    }


@dataclass(frozen=True)
class RoundSnapshot:
    """Rankings frozen for one round; consumers never see a swap (T12)."""

    document: RankingsDocument
    profile: ProfileRanking
    file_sha256: str
    catalog_snapshot: str
    generated_at: str


def load_round_snapshot(
    path: Path | None = None,
    *,
    max_age_hours: float = DEFAULT_SNAPSHOT_MAX_AGE_HOURS,
) -> RoundSnapshot | None:
    """Frozen free-review order, or None when nothing was published yet.

    A missing file is the normal manual-list state, not an error.
    Unreadable, invalid, undated or expired snapshots fail closed.
    """
    target = Path(path) if path is not None else rankings_path()
    if not target.exists():
        return None
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ReviewRoutingError(f"rankings unreadable: {target}") from exc
    try:
        document = load_rankings(target)
    except Exception as exc:
        raise ReviewRoutingError(f"rankings {target} invalid: {exc}") from exc
    profile = document.profiles.get(FREE_REVIEW_PROFILE)
    if profile is None:
        raise ReviewRoutingError(f"rankings {target} carry no {FREE_REVIEW_PROFILE} profile")
    generated = document.generated_at
    if generated is None:
        raise ReviewRoutingError(f"rankings {target} are undated")
    moment = generated if generated.tzinfo else generated.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - moment).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        raise ReviewRoutingError(
            f"rankings {target} expired ({age_hours:.1f}h old, "
            f"max {max_age_hours:g}h)")
    digest = hashlib.sha256(raw).hexdigest()
    return RoundSnapshot(
        document=document, profile=profile, file_sha256=f"sha256:{digest}",
        catalog_snapshot=document.catalog_snapshot,
        generated_at=moment.isoformat())


def review_candidates(
    ranking: ProfileRanking,
    policy: Any | None = None,
) -> list[Candidate]:
    """Single multi-provider pool in ranking order (TASK-2026-00319).

    Every ranked route is its own failover entry: the primary first,
    then each backup, preserving the ranking order. The order decides,
    never the provider; benchmarks, serve evidence and quota already
    ordered the ranking. The strict pair still needs two distinct
    families, enforced by :func:`pick_pair`, not here.
    """
    candidates: list[Candidate] = []
    for item in fallback_chain(ranking, policy):
        for route in (item.primary, *item.backups):
            candidates.append(Candidate(
                deployment_id=route.deployment_id, family_id=item.family_id,
                canonical_model_id=item.canonical_model_id,
                provider=route.provider, quality=item.quality))
    return candidates


def _catalog_deployments(
    catalog: CatalogDocument,
) -> dict[str, tuple[CatalogEntry, Deployment]]:
    return {
        deployment.deployment_id: (entry, deployment)
        for entry in catalog.entries
        for deployment in entry.deployments
    }


def _single_provider_config(
    entry: CatalogEntry,
    deployment: Deployment,
) -> dict[str, Any] | None:
    """v2 OpenCode entries for one group B deployment, refs only.

    Reuses the group B builder over a one-entry document so the same
    fail-closed checks apply; None when the builder refuses it.
    """
    try:
        document = CatalogDocument(entries=[CatalogEntry(
            family_id=entry.family_id,
            canonical_model_id=entry.canonical_model_id,
            display_name=entry.display_name,
            deployments=[deployment])])
        built = build_external_providers_config(
            GROUP_B_CONFIG_SCHEMA, document)
    except (ExternalError, ValueError):
        return None
    providers = built.get("providers")
    return dict(providers) if isinstance(providers, dict) else None


def native_review_targets(
    snapshot: RoundSnapshot,
    catalog: CatalogDocument,
    store: AvailabilityStore,
    *,
    exclusions: Exclusions | None = None,
    instance_models: Container[tuple[str, str]] | None = None,
    credentials: Mapping[str, str] | None = None,
    run_id: str | None = None,
) -> list[ReviewTarget]:
    """Ordered group A then group B review targets (addendum v1.2 §3).

    The :mod:`selector` order (zero-cost, health, profile, instance
    allowlist, store gates) converts to review candidates; group B
    additionally needs a configured credential and a buildable
    single-provider config, else it drops with an audit event. Empty
    raises :class:`NoReviewPair` like the legacy path.
    """
    try:
        selection = select_targets(
            FREE_REVIEW_PROFILE, catalog, snapshot.document,
            store=store, exclusions=exclusions,
            instance_models=instance_models, run_id=run_id)
    except NoEligibleTarget as exc:
        raise NoReviewPair(str(exc)) from exc
    index = _catalog_deployments(catalog)
    have = {str(name) for name, value in dict(credentials or {}).items()
            if str(value)}
    targets: list[ReviewTarget] = []
    for target in selection.targets:
        found = index.get(target.deployment_id)
        if found is None:
            continue
        entry, deployment = found
        external = target.group != "A"
        env_var = ""
        provider_config: dict[str, Any] | None = None
        if external:
            try:
                env_var = credential_env_for(deployment.provider)
            except ExternalError:
                write_audit_event(
                    store, run_id or "-", "review_group_b_unmapped",
                    f"{target.deployment_id}: provider "
                    f"{deployment.provider} has no OpenCode mapping")
                continue
            if env_var not in have:
                write_audit_event(
                    store, run_id or "-", "review_group_b_uncredentialed",
                    f"{target.deployment_id}: {env_var} is unset")
                continue
            provider_config = _single_provider_config(entry, deployment)
            if not provider_config:
                write_audit_event(
                    store, run_id or "-", "review_group_b_unbuildable",
                    f"{target.deployment_id}: provider config refused")
                continue
        targets.append(ReviewTarget(
            deployment_id=target.deployment_id,
            family_id=target.family_id,
            canonical_model_id=target.canonical_model_id,
            provider=deployment.provider, quality=target.quality,
            route_type=target.route_type,
            provider_id=target.provider_id, model_id=target.model_id,
            opencode_model=target.opencode_model,
            credential_env=env_var, provider_config=provider_config))
    if not targets:
        raise NoReviewPair(
            f"no servable {FREE_REVIEW_PROFILE} target after the group B "
            "credential and config gates")
    return targets


def _resolve_round_credentials(
    router_secrets: Mapping[str, str] | None,
) -> dict[str, str]:
    """Credential presence for one round; names only, never values."""
    if router_secrets is not None:
        return {str(name): str(value)
                for name, value in dict(router_secrets).items()
                if str(value)}
    try:
        loaded = load_router_secrets()
    except SecretsError as exc:
        logger.warning("free-router group B unavailable: %s", exc)
        return {}
    return {name: value for name, value in vars(loaded).items()
            if isinstance(value, str) and value}


def _resolve_instance_models(
    store: AvailabilityStore,
    run_id: str,
    instance_models: Container[tuple[str, str]] | None,
    auto_instance: bool,
) -> Container[tuple[str, str]] | None:
    """Instance allowlist for group A; best-effort when automatic.

    An unavailable or empty instance listing skips the gate with an
    audit event instead of failing the round: the target still faces
    every other gate, and a stale id fails fast at ``opencode run``.
    """
    if instance_models is not None:
        return instance_models
    if not auto_instance:
        return None
    try:
        found = read_instance_models()
    except Exception as exc:  # noqa: BLE001
        write_audit_event(
            store, run_id, "review_instance_unlisted", str(exc)[:200])
        logger.warning("free-router instance listing unavailable: %s", exc)
        return None
    if not found.models:
        write_audit_event(store, run_id, "review_instance_unlisted",
                          "instance listed no models")
        return None
    return found.models


def _round_candidates(
    snapshot: RoundSnapshot,
    store: AvailabilityStore,
    run_id: str,
    *,
    base_exclusions: frozenset[str],
    catalog_file: Path | None,
    router_secrets: Mapping[str, str] | None,
    instance_models: Container[tuple[str, str]] | None,
    auto_instance: bool,
) -> list[Candidate]:
    """Native-first candidates with a catalog, else the legacy order."""
    target = Path(catalog_file) if catalog_file is not None else catalog_path()
    if not target.exists():
        return review_candidates(snapshot.profile)
    try:
        catalog = load_catalog_doc(target)
    except PublishError as exc:
        raise ReviewRoutingError(
            f"catalog {target} invalid: {exc}") from exc
    return native_review_targets(
        snapshot, catalog, store,
        exclusions=Exclusions(families=base_exclusions),
        instance_models=_resolve_instance_models(
            store, run_id, instance_models, auto_instance),
        credentials=_resolve_round_credentials(router_secrets),
        run_id=run_id)


def static_exclusions(
    raw_pool: Sequence[Mapping[str, Any]],
    *,
    builder_family: str,
) -> frozenset[str]:
    """Families the ranking-fed pair must avoid.

    The builder's own family plus every active, quorum-counting static
    slot the router does not feed (dsh, gemini): the pair stays
    distinct from the other reviewers of the round. Static OpenCode
    entries are skipped: the pair replaces them.
    """
    excluded = {builder_family.strip().lower()}
    for entry in raw_pool:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("provider") or "") == "opencode":
            continue
        if bool(entry.get("extra", False)):
            continue
        if entry.get("active", True) is False:
            continue
        family = str(entry.get("family") or "").strip().lower()
        if family:
            excluded.add(family)
    return frozenset(excluded)


@dataclass
class LabelState:
    """Fallback chase of one ranking-fed label within its round."""

    requested: Candidate
    effective: Candidate
    tried_deployments: set[str] = field(default_factory=set)
    tried_canonicals: set[str] = field(default_factory=set)
    tried_families: set[str] = field(default_factory=set)
    fallback_count: int = 0
    last_change_reason: str = ""


@dataclass(frozen=True)
class ReviewReceipt:
    """Extended §11.7 receipt of one valid ranking-fed report."""

    run_id: str
    round: str
    slot: str
    requested_model: str
    effective_model: str
    family_id: str
    canonical_model_id: str
    provider: str
    deployment_id: str
    rank: int | None
    quality: float | None
    rankings_snapshot: str
    catalog_snapshot: str
    reviewed_commit: str
    report_path: str
    sha256: str
    result: str
    fallback_count: int
    started_at: str
    finished_at: str
    route_type: str = "opencode_native_free"
    provider_id: str = ""
    model_id: str = ""
    change_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "round": self.round, "slot": self.slot,
            "requested_model": self.requested_model,
            "effective_model": self.effective_model,
            "family_id": self.family_id,
            "canonical_model_id": self.canonical_model_id,
            "provider": self.provider,
            "deployment_id": self.deployment_id, "rank": self.rank,
            "quality": self.quality,
            "rankings_snapshot": self.rankings_snapshot,
            "catalog_snapshot": self.catalog_snapshot,
            "reviewed_commit": self.reviewed_commit,
            "report_path": self.report_path, "sha256": self.sha256,
            "result": self.result, "fallback_count": self.fallback_count,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "route_type": self.route_type,
            "provider_id": self.provider_id, "model_id": self.model_id,
            "change_reason": self.change_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewReceipt":
        data = dict(payload)
        rank = data.get("rank")
        quality = data.get("quality")
        return cls(
            run_id=str(data["run_id"]), round=str(data["round"]),
            slot=str(data["slot"]),
            requested_model=str(data["requested_model"]),
            effective_model=str(data["effective_model"]),
            family_id=str(data["family_id"]),
            canonical_model_id=str(data.get("canonical_model_id") or ""),
            provider=str(data["provider"]),
            deployment_id=str(data["deployment_id"]),
            rank=int(rank) if rank is not None else None,
            quality=float(quality) if quality is not None else None,
            rankings_snapshot=str(data["rankings_snapshot"]),
            catalog_snapshot=str(data.get("catalog_snapshot") or ""),
            reviewed_commit=str(data["reviewed_commit"]),
            report_path=str(data["report_path"]),
            sha256=str(data["sha256"]), result=str(data["result"]),
            fallback_count=int(data.get("fallback_count") or 0),
            started_at=str(data.get("started_at") or ""),
            finished_at=str(data.get("finished_at") or ""),
            route_type=str(
                data.get("route_type") or "opencode_native_free"),
            provider_id=str(data.get("provider_id") or ""),
            model_id=str(data.get("model_id") or ""),
            change_reason=str(data.get("change_reason") or ""))


@dataclass
class RoundRouting:
    """Live state of one ranking-fed round; closed when the round ends."""

    run_id: str
    round: str
    snapshot: RoundSnapshot
    store: AvailabilityStore
    candidates: list[Candidate]
    labels: tuple[str, ...]
    states: dict[str, LabelState]
    round_families: frozenset[str]
    receipts_dir: Path
    receipts: dict[str, ReviewReceipt] = field(default_factory=dict)

    def close(self) -> None:
        """Release the availability handle; reservations expire by TTL."""
        try:
            self.store.close()
        except Exception:  # closing never fails a round
            logger.warning("review routing store close failed", exc_info=True)


@dataclass(frozen=True)
class R1Result:
    """What round 2 may reuse from round 1 (SPEC §11.3).

    ``receipts`` carries valid ranking-fed reports only; static
    slots never repeat through the §11.3 policies.
    """

    receipts: tuple[ReviewReceipt, ...] = ()
    round_families: frozenset[str] = frozenset()
    tried_families: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Assembly:
    """Pool plus routing for one round; ``routing`` None means static."""

    raw_pool: list[dict[str, Any]]
    routing: RoundRouting | None
    prefer_families: tuple[str, ...] = ()


def pick_pair(
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    *,
    exclude_families: frozenset[str] | set[str] = frozenset(),
) -> tuple[Candidate, Candidate]:
    """Two best servable candidates of distinct families (TASK-2026-00319)."""
    excluded = frozenset(exclude_families)
    try:
        first = get_next_candidate(
            list(candidates), store, exclusions=Exclusions(families=excluded))
    except NoEligibleCandidate as exc:
        raise NoReviewPair(
            f"no servable {FREE_REVIEW_PROFILE} model outside "
            f"{sorted(excluded) or 'nothing'}") from exc
    try:
        second = get_next_candidate(
            list(candidates), store, exclusions=Exclusions(
                families=excluded | {first.family_id},
                canonicals=frozenset({first.canonical_model_id}),
                deployments=frozenset({first.deployment_id})))
    except NoEligibleCandidate as exc:
        raise NoReviewPair(
            f"no second {FREE_REVIEW_PROFILE} family distinct from "
            f"{first.family_id}") from exc
    return first, second


def _gated(
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    exclusions: Exclusions,
) -> Candidate | None:
    try:
        return get_next_candidate(list(candidates), store, exclusions=exclusions)
    except NoEligibleCandidate:
        return None


def _resolve_r1(
    receipts: Sequence[ReviewReceipt],
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    skip: Exclusions,
) -> list[Candidate]:
    """Valid R1 models, best first, that still serve in this round."""
    by_canonical: dict[str, Candidate] = {}
    for candidate in candidates:
        by_canonical.setdefault(candidate.canonical_model_id, candidate)
    ordered = sorted(
        receipts,
        key=lambda item: (item.rank is None, item.rank or 0, item.slot))
    resolved: list[Candidate] = []
    for receipt in ordered:
        candidate = by_canonical.get(receipt.canonical_model_id)
        if candidate is None:
            continue
        hit = _gated([candidate], store, skip)
        if hit is not None:
            resolved.append(hit)
    return resolved


def select_round2_pair(
    *,
    policy: str,
    r1: R1Result,
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    exclude_families: frozenset[str] | set[str] = frozenset(),
) -> tuple[Candidate, Candidate | None]:
    """Round 2 models under one SPEC §11.3 policy.

    Novelty is measured against the families round 1 worked with;
    the builder's family stays excluded. ``four_distinct`` without a
    four-family plan raises :class:`RoundBlocked`; the other policies
    degrade to a single fed slot rather than blocking.
    """
    name = (policy or "").strip()
    if name not in ROUND2_POLICIES:
        raise ReviewRoutingError(
            f"unknown round2_policy {policy!r} (known: {', '.join(ROUND2_POLICIES)})")
    base = frozenset(exclude_families)
    used = base | set(r1.round_families) | set(r1.tried_families)
    if name == "four_distinct":
        return _select_four_distinct(
            r1, candidates, store, base=base, used=used)
    if name == "reuse_top_two":
        return _select_reuse_top_two(
            r1, candidates, store, base=base, used=used)
    return _select_best_plus_novel(
        r1, candidates, store, base=base, used=used)


def _novel(
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    skip_families: set[str],
    skip_canonicals: set[str] | frozenset[str] = frozenset(),
    skip_deployments: set[str] | frozenset[str] = frozenset(),
) -> Candidate | None:
    return _gated(
        candidates, store,
        Exclusions(families=frozenset(skip_families),
                   canonicals=frozenset(skip_canonicals),
                   deployments=frozenset(skip_deployments)))


def _select_best_plus_novel(
    r1: R1Result,
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    *,
    base: frozenset[str],
    used: set[str],
) -> tuple[Candidate, Candidate | None]:
    resolved = _resolve_r1(r1.receipts, candidates, store,
                           Exclusions(families=base))
    if resolved:
        first: Candidate | None = resolved[0]
    else:
        first = _novel(candidates, store, set(base))
    if first is None:
        raise NoReviewPair("no servable round 2 model")
    runner_up = next(
        (item for item in resolved[1:]
         if item.family_id != first.family_id
         and item.canonical_model_id != first.canonical_model_id),
        None)
    second = _novel(
        candidates, store,
        set(used) | {first.family_id},
        skip_canonicals={first.canonical_model_id},
        skip_deployments={first.deployment_id})
    if second is None:
        second = runner_up
    if second is None:
        second = _novel(
            candidates, store, set(base) | {first.family_id},
            skip_canonicals={first.canonical_model_id},
            skip_deployments={first.deployment_id})
    return first, second


def _select_four_distinct(
    r1: R1Result,
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    *,
    base: frozenset[str],
    used: set[str],
) -> tuple[Candidate, Candidate | None]:
    first = _novel(candidates, store, set(used))
    if first is None:
        raise NoReviewPair("no novel round 2 family outside round 1")
    second = _novel(
        candidates, store, set(used) | {first.family_id},
        skip_canonicals={first.canonical_model_id},
        skip_deployments={first.deployment_id})
    planned = set(r1.round_families) | {first.family_id}
    if second is not None:
        planned.add(second.family_id)
    if len(planned) < 4:
        raise RoundBlocked(
            f"four_distinct needs four families across both rounds, "
            f"only {len(planned)} plannable ({', '.join(sorted(planned)) or 'none'})")
    return first, second


def _select_reuse_top_two(
    r1: R1Result,
    candidates: Sequence[Candidate],
    store: AvailabilityStore,
    *,
    base: frozenset[str],
    used: set[str],
) -> tuple[Candidate, Candidate | None]:
    resolved = _resolve_r1(r1.receipts, candidates, store,
                           Exclusions(families=base))
    distinct = [resolved[0]] if resolved else []
    distinct.extend(item for item in resolved[1:]
                    if item.family_id != resolved[0].family_id)
    first = distinct[0] if distinct else _novel(candidates, store, set(base))
    if first is None:
        raise NoReviewPair("no servable round 2 model")
    second = next(
        (item for item in distinct[1:]
         if item.canonical_model_id != first.canonical_model_id),
        None)
    if second is None:
        second = _novel(
            candidates, store,
            set(used) | {first.family_id},
            skip_canonicals={first.canonical_model_id},
            skip_deployments={first.deployment_id})
    if second is None:
        second = _novel(
            candidates, store, set(base) | {first.family_id},
            skip_canonicals={first.canonical_model_id},
            skip_deployments={first.deployment_id})
    return first, second


def derive_slot(slot: ReviewSlot, candidate: Candidate) -> ReviewSlot:
    """Same label and priority, new ranking-fed model identity."""
    model = _model_arg(candidate)
    return replace(
        slot, model=model, family=candidate.family_id,
        command=("opencode", "run", "-m", model))


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "round"


def receipt_filenames(
    receipts_dir: Path, run_id: str, round: str, slot: str,
) -> tuple[Path, Path]:
    """(receipt JSON, report markdown) paths for one fed slot."""
    stem = f"{_safe_name(run_id)}-r{_safe_name(round)}-{_safe_name(slot)}"
    base = Path(receipts_dir)
    return base / f"{stem}.json", base / f"{stem}.md"


def build_receipt(
    routing: RoundRouting,
    label: str,
    *,
    report_text: str,
    reviewed_commit: str,
    started_at: str,
    finished_at: str,
) -> ReviewReceipt:
    """§11.7 receipt of a valid fed report; files land via record."""
    state = routing.states[label]
    effective = state.effective
    rank: int | None = None
    quality: float | None = None
    for position, candidate in enumerate(routing.candidates, start=1):
        if candidate.deployment_id == effective.deployment_id:
            rank, quality = position, candidate.quality
            break
    _, report_path = receipt_filenames(
        routing.receipts_dir, routing.run_id, routing.round, label)
    provider_id, model_id = _target_ids(effective)
    return ReviewReceipt(
        run_id=routing.run_id, round=routing.round, slot=label,
        requested_model=_model_arg(state.requested),
        effective_model=_model_arg(effective),
        family_id=effective.family_id,
        canonical_model_id=effective.canonical_model_id,
        provider=effective.provider,
        deployment_id=effective.deployment_id, rank=rank, quality=quality,
        rankings_snapshot=routing.snapshot.file_sha256,
        catalog_snapshot=routing.snapshot.catalog_snapshot,
        reviewed_commit=reviewed_commit, report_path=str(report_path),
        sha256=hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
        result="validated", fallback_count=state.fallback_count,
        started_at=started_at, finished_at=finished_at,
        route_type=_target_route(effective),
        provider_id=provider_id, model_id=model_id,
        change_reason=state.last_change_reason)


def record_receipt(
    routing: RoundRouting,
    label: str,
    *,
    report_text: str,
    reviewed_commit: str,
    started_at: str,
    finished_at: str,
) -> ReviewReceipt:
    """Build, persist and register one valid-report receipt (T18 source)."""
    receipt = build_receipt(
        routing, label, report_text=report_text,
        reviewed_commit=reviewed_commit, started_at=started_at,
        finished_at=finished_at)
    receipts_dir = Path(routing.receipts_dir)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = receipt_filenames(
        receipts_dir, routing.run_id, routing.round, label)
    md_path.write_text(report_text, encoding="utf-8")
    json_path.write_text(
        json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    routing.receipts[label] = receipt
    if receipt.route_type == "opencode_native_free":
        router_serve_evidence.record_native_serve(receipt.provider, receipt.model_id)
    return receipt


def load_valid_receipt(
    receipts_dir: Path,
    *,
    run_id: str,
    round: str,
    slot: str,
    reviewed_commit: str,
) -> tuple[ReviewReceipt, str] | None:
    """Valid receipt plus its report bytes; None unless everything checks.

    A restart reuses the verified report only when the receipt pins
    this run, round, slot and commit, claims ``validated`` and the
    stored sha256 still matches the stored bytes (T18).
    """
    json_path, md_path = receipt_filenames(receipts_dir, run_id, round, slot)
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        text = md_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        receipt = ReviewReceipt.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None
    if receipt.result != "validated":
        return None
    if (receipt.run_id, receipt.round, receipt.slot) != (run_id, round, slot):
        return None
    if receipt.reviewed_commit != reviewed_commit:
        return None
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != receipt.sha256:
        return None
    return receipt, text


def plan_fallback(
    routing: RoundRouting,
    label: str,
    *,
    outcome_kind: str,
    text: str,
    timeout_repeat: bool = False,
    responded: bool = False,
) -> Candidate | None:
    """One §11.6 step: record, release, reserve the next model, restart.

    Returns the next candidate the caller restarts alone on the same
    snapshot and prompt, or None when this failure must not rotate
    (same-model retry, inspection and fail-safe classes) or when the
    chain or the per-label budget is exhausted. A reservation race
    skips to the next family instead of sharing one (T04).
    """
    state = routing.states.get(label)
    if state is None or state.fallback_count >= REVIEW_FALLBACK_MAX:
        return None
    kind = outcome_kind or ""
    if kind == "timeout" and not timeout_repeat:
        return None
    if kind == "timeout":
        classified = classify_timeout(responded=responded)
    else:
        classified = classify_text(text or "")
    rotate = classified.error_class in ROTATE_CLASSES or (
        classified.error_class == "unclassified"
        and kind in {"cooling_down", "disabled"})
    if not rotate:
        return None
    effective = state.effective
    record_provider_error(
        routing.store, provider=effective.provider,
        deployment_id=effective.deployment_id, model=effective.deployment_id,
        classified=classified, run_id=routing.run_id, excerpt=text or "")
    release_reservation(routing.store, routing.run_id, routing.round, label)
    skip_families = set(routing.round_families) | state.tried_families
    skip_canonicals = set(state.tried_canonicals)
    skip_deployments = set(state.tried_deployments)
    for _ in range(len(routing.candidates) + 1):
        try:
            nxt = get_next_candidate(
                list(routing.candidates), routing.store,
                exclusions=Exclusions(
                    families=frozenset(skip_families),
                    canonicals=frozenset(skip_canonicals),
                    deployments=frozenset(skip_deployments)))
        except NoEligibleCandidate:
            break
        try:
            reserve_model(
                routing.store, routing.run_id, routing.round, label,
                nxt.canonical_model_id, nxt.family_id, nxt.deployment_id)
        except ReservationConflict:
            write_audit_event(
                routing.store, routing.run_id, "review_fallback_conflict",
                f"{label}: {nxt.deployment_id}")
            skip_families.add(nxt.family_id)
            skip_canonicals.add(nxt.canonical_model_id)
            skip_deployments.add(nxt.deployment_id)
            continue
        state.effective = nxt
        state.tried_deployments.add(nxt.deployment_id)
        state.tried_canonicals.add(nxt.canonical_model_id)
        state.tried_families.add(nxt.family_id)
        state.fallback_count += 1
        before, after = _target_group(effective), _target_group(nxt)
        crossed = f" (group {before}->{after})" if before != after else ""
        state.last_change_reason = (
            f"{classified.error_class}: {effective.deployment_id} -> "
            f"{nxt.deployment_id}{crossed}")
        write_audit_event(
            routing.store, routing.run_id, "review_fallback",
            f"{label}: {effective.deployment_id} -> {nxt.deployment_id} "
            f"({classified.error_class})")
        return nxt
    write_audit_event(
        routing.store, routing.run_id, "review_fallback_exhausted",
        f"{label}: {effective.deployment_id} ({classified.error_class})")
    return None


def dynamic_entry(label: str, priority: int, candidate: Candidate) -> dict[str, Any]:
    """Raw pool entry of one ranking-fed label (hardened §14.3)."""
    model = _model_arg(candidate)
    entry: dict[str, Any] = {
        "label": label, "provider": "opencode",
        "model": model, "family": candidate.family_id,
        "priority": priority, "active": True,
        "cost_tier": "free",
        "quota": "reactive", "prompt_via": "argv",
        "command": ["opencode", "run", "-m", model],
        HARDENED_MARK: True,
    }
    if isinstance(candidate, ReviewTarget) and candidate.is_external:
        entry[REVIEW_TARGET_SPEC] = {
            "provider_id": candidate.provider_id,
            "model_id": candidate.model_id,
            "route_type": candidate.route_type,
            "env_var": candidate.credential_env,
            "providers": copy.deepcopy(candidate.provider_config or {}),
        }
    return entry


_dynamic_entry = dynamic_entry


def _marked_static(raw_pool: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Static pool copy with the §14.3 fence marked on free slots."""
    marked: list[dict[str, Any]] = []
    for entry in raw_pool:
        if not isinstance(entry, Mapping):
            continue
        item = dict(entry)
        if str(item.get("provider") or "") == "opencode":
            item[HARDENED_MARK] = True
        marked.append(item)
    return marked


def assemble_review_pool(
    raw_pool: Sequence[Mapping[str, Any]],
    *,
    review_cfg: Mapping[str, Any] | None,
    run_id: str,
    round: str,
    builder_family: str,
    receipts_dir: Path | str,
    rankings_path: Path | None = None,
    r1: R1Result | None = None,
    catalog_file: Path | None = None,
    router_secrets: Mapping[str, str] | None = None,
    instance_models: Container[tuple[str, str]] | None = None,
    auto_instance: bool = True,
) -> Assembly:
    """Pool plus routing for one dev-review round.

    Ranking-fed when the switch is on and the frozen snapshot yields
    a pair: static OpenCode entries give way to ``free-review-1/2``.
    Every other shape (switch off, no snapshot, expired snapshot,
    no pair) keeps the static pool; degraded shapes audit why.
    Round 2 takes ``r1`` and the configured §11.3 policy; only
    ``four_distinct`` without a four-family plan raises. With a
    catalog next to the rankings the pair comes from the native_first
    list (group A then group B); without one the legacy
    OpenCode-only order still serves.
    """
    settings = free_router_settings(review_cfg)
    static = _marked_static(raw_pool)
    if not settings["enabled"]:
        return Assembly(
            raw_pool=[dict(entry) for entry in raw_pool
                      if isinstance(entry, Mapping)],
            routing=None)
    try:
        snapshot = load_round_snapshot(
            rankings_path, max_age_hours=settings["snapshot_max_age_hours"])
    except ReviewRoutingError as exc:
        logger.warning("free-router static fallback: %s", exc)
        return Assembly(raw_pool=static, routing=None)
    if snapshot is None:
        return Assembly(raw_pool=static, routing=None)
    store = AvailabilityStore()
    static_fams = static_exclusions(raw_pool, builder_family=builder_family)
    base_exclusions = (static_fams if r1 is None
                       else frozenset({builder_family.strip().lower()}))
    try:
        candidates = _round_candidates(
            snapshot, store, run_id, base_exclusions=base_exclusions,
            catalog_file=catalog_file, router_secrets=router_secrets,
            instance_models=instance_models, auto_instance=auto_instance)
    except NoReviewPair as exc:
        write_audit_event(store, run_id, "review_pair_unavailable", str(exc)[:200])
        logger.warning("free-router static fallback: %s", exc)
        store.close()
        return Assembly(raw_pool=static, routing=None)
    except ReviewRoutingError as exc:
        logger.warning("free-router static fallback: %s", exc)
        store.close()
        return Assembly(raw_pool=static, routing=None)
    try:
        if r1 is None:
            first_pick, second_pick = pick_pair(
                candidates, store, exclude_families=static_fams)
            pair = (first_pick, second_pick)
            prefer = (first_pick.family_id, second_pick.family_id)
        else:
            pair = select_round2_pair(
                policy=settings["round2_policy"], r1=r1,
                candidates=candidates, store=store,
                exclude_families=frozenset({builder_family.strip().lower()}))
            prefer = tuple(item.family_id for item in pair if item is not None)
    except RoundBlocked:
        store.close()
        raise
    except ReviewRoutingError as exc:
        write_audit_event(store, run_id, "review_pair_unavailable", str(exc)[:200])
        logger.warning("free-router static fallback: %s", exc)
        store.close()
        return Assembly(raw_pool=static, routing=None)
    picked = [item for item in pair if item is not None]
    labels = REVIEW_SLOT_LABELS[: len(picked)]
    for label in labels:
        release_reservation(store, run_id, round, label)
    try:
        store.reserve_round(
            run_id, round,
            [(label, item.canonical_model_id, item.family_id, item.deployment_id)
             for label, item in zip(labels, picked)])
    except ReservationConflict as exc:
        write_audit_event(store, run_id, "review_reserve_conflict", str(exc)[:200])
        logger.warning("free-router static fallback: %s", exc)
        store.close()
        return Assembly(raw_pool=static, routing=None)
    states = {
        label: LabelState(
            requested=item, effective=item,
            tried_deployments={item.deployment_id},
            tried_canonicals={item.canonical_model_id},
            tried_families={item.family_id})
        for label, item in zip(labels, picked)
    }
    dynamics = [
        _dynamic_entry(label, REVIEW_SLOT_PRIORITIES[position], item)
        for position, (label, item) in enumerate(zip(labels, picked))
    ]
    merged: list[dict[str, Any]] = []
    placed = False
    for entry in static:
        if str(entry.get("provider") or "") == "opencode":
            if not placed:
                merged.extend(dynamics)
                placed = True
            continue
        merged.append(entry)
    if not placed:
        merged.extend(dynamics)
    routing = RoundRouting(
        run_id=run_id, round=str(round), snapshot=snapshot, store=store,
        candidates=candidates, labels=tuple(labels), states=states,
        round_families=(static_fams if r1 is None
                        else frozenset({builder_family.strip().lower()})
                        | set(r1.round_families) | set(r1.tried_families)),
        receipts_dir=Path(receipts_dir))
    write_audit_event(
        store, run_id, "review_round_assembled",
        f"r{round}: {', '.join(f'{label}={item.deployment_id}' for label, item in zip(labels, picked))} "
        f"({snapshot.file_sha256[:19]})")
    return Assembly(raw_pool=merged, routing=routing, prefer_families=prefer)


def sync_executor_pool(executor: Any, raw_pool: Sequence[Mapping[str, Any]]) -> None:
    """Hand round-scoped entries to a live executor when it takes them.

    Scripted executors resolve roles by name and need nothing; only
    the production process executor registers dynamic labels and
    hardening marks.
    """
    register = getattr(executor, "register_pool_entries", None)
    if callable(register):
        register([dict(entry) for entry in raw_pool if isinstance(entry, Mapping)])
