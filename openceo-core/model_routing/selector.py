"""native_first target selection over ranking order (TASK-2026-00305).

Addendum v1.2 §2-§3 as code: one selector delivers, per logical
profile, the ordered list of real ``provider_id/model_id`` targets:
first OpenCode Free native (group A), then zero-cost external
providers configured directly in OpenCode (group B). ``best_global``
exists only as an explicit policy opt-in; the default is
``native_first``. No HTTP alias spans both groups: group B runs
through the provider configured in OpenCode (or the approved
gateway, still on HOLD).

Filters reuse the sibling gates instead of duplicating them: the
catalog price gate (:func:`is_usable`), health, :func:`meets_profile`
(context/tools/confidentiality), the ranking floor/drop
(:func:`fallback_chain`), the instance allowlist for group A, and
the availability store (cooldowns, disabled routes, provider
blocks). With no eligible target the selector blocks with every
reason recorded, never degrading silently. One
:class:`AttemptBudget` counts every attempt so OpenCode, the
gateway and the coordinator never multiply retries; failing over
across providers never eludes a shared OpenRouter quota because
the budget is global, not per provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Container, Iterator

from model_routing.availability import (
    AvailabilityStore,
    Exclusions,
    meets_profile,
    write_audit_event,
)
from model_routing.models import (
    CatalogDocument,
    CatalogEntry,
    Deployment,
    RankingsDocument,
    RouterPolicy,
    is_usable,
    usability,
)
from model_routing.ranking import RANK_PROFILES, fallback_chain, profile_requirements

__all__ = [
    "AttemptBudget",
    "BudgetExhausted",
    "DroppedTarget",
    "NoEligibleTarget",
    "Selection",
    "SelectionError",
    "Target",
    "select_targets",
    "walk_targets",
]


class SelectionError(RuntimeError):
    """Selection misconfigured; the message says why."""


class NoEligibleTarget(SelectionError):
    """Blocked: no target survives the gates; every reason is recorded."""

    def __init__(self, profile: str, reasons: list[str]) -> None:
        self.profile = profile
        self.reasons = list(reasons)
        detail = "; ".join(self.reasons) if self.reasons else "no reason recorded"
        super().__init__(f"no eligible {profile} target: {detail}")


class BudgetExhausted(SelectionError):
    """The single global attempt budget ran out."""


@dataclass(frozen=True)
class Target:
    """One ordered route a session or reviewer can take (addendum §6)."""

    profile: str
    target_id: str
    group: str
    provider_id: str
    model_id: str
    route_type: str
    allowed_client: str
    family_id: str
    canonical_model_id: str
    deployment_id: str
    opencode_model: str
    rank: int
    quality: float


@dataclass(frozen=True)
class DroppedTarget:
    """One ranked route that failed a gate, with its reason."""

    deployment_id: str
    canonical_model_id: str
    reason: str


@dataclass(frozen=True)
class Selection:
    """Ordered targets plus everything the gates set aside."""

    profile: str
    policy: str
    targets: tuple[Target, ...] = ()
    dropped: tuple[DroppedTarget, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AttemptBudget:
    """Single global attempt counter (addendum §7).

    One instance travels with the fallback walk across groups,
    providers and rounds: consuming it once per attempt is what
    keeps OpenCode, the gateway and the coordinator from
    multiplying retries.
    """

    max_attempts: int
    attempts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise SelectionError(
                f"attempt budget must allow at least one try: {self.max_attempts}")

    @property
    def remaining(self) -> int:
        """Attempts left before the budget runs out."""
        return max(0, self.max_attempts - len(self.attempts))

    def consume(self, label: str) -> "AttemptBudget":
        """Count one attempt; raise when the budget is already spent."""
        if len(self.attempts) >= self.max_attempts:
            raise BudgetExhausted(
                f"attempt budget spent ({self.max_attempts}); {label} refused")
        return replace(self, attempts=(*self.attempts, label))


def walk_targets(
    targets: list[Target] | tuple[Target, ...],
    budget: AttemptBudget,
) -> Iterator[tuple[Target, AttemptBudget]]:
    """Yield targets in order, counting one attempt each (addendum §7)."""
    remaining = budget
    for target in targets:
        remaining = remaining.consume(target.target_id)
        yield target, remaining


def _deployment_index(
    catalog: CatalogDocument,
) -> dict[str, tuple[CatalogEntry, Deployment]]:
    return {
        deployment.deployment_id: (entry, deployment)
        for entry in catalog.entries
        for deployment in entry.deployments
    }


def _exclusion_hit(deployment: Deployment, family_id: str, canonical: str,
                   skipped: Exclusions) -> str | None:
    if family_id in skipped.families:
        return f"family {family_id} already tried"
    if canonical in skipped.canonicals:
        return f"canonical {canonical} already tried"
    if deployment.deployment_id in skipped.deployments:
        return f"deployment {deployment.deployment_id} already tried"
    if deployment.provider in skipped.providers:
        return f"provider {deployment.provider} already tried"
    return None


def _store_hit(store: AvailabilityStore, deployment_id: str, provider: str,
               now: float | None) -> str | None:
    cooled = store.cooldown_for(deployment_id, now=now)
    if cooled is not None:
        return f"cooling down: {cooled.get('reason', 'cooldown')}"
    disabled = store.route_disabled(deployment_id)
    if disabled is not None:
        return f"route disabled: {disabled}"
    blocked = store.provider_blocked(provider, now=now)
    if blocked is not None:
        return f"provider blocked: {blocked.get('reason', 'block')}"
    return None


def select_targets(
    profile: str,
    catalog: CatalogDocument,
    rankings: RankingsDocument,
    *,
    policy: RouterPolicy | None = None,
    store: AvailabilityStore | None = None,
    exclusions: Exclusions | None = None,
    enabled_providers: Container[str] | None = None,
    instance_models: Container[tuple[str, str]] | None = None,
    now: float | None = None,
    run_id: str | None = None,
) -> Selection:
    """Ordered group A then group B targets for one profile (addendum §3).

    ``instance_models`` is the ``(provider_id, model_id)`` allowlist the
    OpenCode instance lists: a native deployment outside it is not an
    eligible target. Group B skips that gate because project-configured
    providers are invisible to the shared instance listing. With no
    eligible target the call blocks with every reason recorded (and
    an audit event when ``store`` and ``run_id`` travel together).
    """
    if profile not in RANK_PROFILES:
        raise SelectionError(
            f"unknown profile: {profile!r} (known: {sorted(RANK_PROFILES)})")
    active = policy or RouterPolicy()
    ranking = rankings.profiles.get(profile)
    if ranking is None:
        return _blocked(
            profile, active, store, run_id,
            [DroppedTarget("*", "*", f"no {profile} ranking published")])
    requirements = profile_requirements(profile)
    skipped = exclusions or Exclusions()
    index = _deployment_index(catalog)
    ordered: list[Target] = []
    dropped: list[DroppedTarget] = []
    seen: set[str] = set()

    def _drop(deployment_id: str, canonical: str, reason: str) -> None:
        dropped.append(DroppedTarget(deployment_id, canonical, reason))

    for item in fallback_chain(ranking, active):
        routes = [item.primary, *item.backups]
        for route in routes:
            found = index.get(route.deployment_id)
            if found is None:
                _drop(route.deployment_id, item.canonical_model_id,
                      "ranked route missing from the catalog")
                continue
            entry, deployment = found
            if route.provider != deployment.provider:
                _drop(route.deployment_id, item.canonical_model_id,
                      f"ranked provider {route.provider} mismatches "
                      f"catalog provider {deployment.provider}")
                continue
            if route.deployment_id in seen:
                _drop(route.deployment_id, item.canonical_model_id,
                      "duplicate of an earlier target")
                continue
            if enabled_providers is not None and deployment.provider not in enabled_providers:
                _drop(route.deployment_id, item.canonical_model_id,
                      f"provider {deployment.provider} not enabled")
                continue
            if not is_usable(deployment, active):
                _drop(route.deployment_id, item.canonical_model_id,
                      f"fails the zero-cost gate: {usability(deployment, active)}")
                continue
            if not deployment.healthy:
                _drop(route.deployment_id, item.canonical_model_id, "not healthy")
                continue
            ok, reason = meets_profile(entry, deployment, requirements)
            if not ok:
                _drop(route.deployment_id, item.canonical_model_id, reason)
                continue
            native = deployment.route_type == "opencode_native_free"
            if native and instance_models is not None and (
                    deployment.provider_id, deployment.model_id) not in instance_models:
                _drop(route.deployment_id, item.canonical_model_id,
                      "not listed by this OpenCode instance")
                continue
            hit = _exclusion_hit(deployment, entry.family_id,
                                 item.canonical_model_id, skipped)
            if hit is not None:
                _drop(route.deployment_id, item.canonical_model_id, hit)
                continue
            if store is not None:
                gated = _store_hit(store, deployment.deployment_id,
                                   deployment.provider, now)
                if gated is not None:
                    _drop(route.deployment_id, item.canonical_model_id, gated)
                    continue
            seen.add(route.deployment_id)
            ordered.append(Target(
                profile=profile,
                target_id=("native:" if native else "external:")
                          + f"{deployment.provider_id}/{deployment.model_id}",
                group="A" if native else "B",
                provider_id=deployment.provider_id,
                model_id=deployment.model_id,
                route_type=deployment.route_type,
                allowed_client=deployment.allowed_client,
                family_id=entry.family_id,
                canonical_model_id=item.canonical_model_id,
                deployment_id=route.deployment_id,
                opencode_model=f"{deployment.provider_id}/{deployment.model_id}",
                rank=item.rank,
                quality=item.quality,
            ))
    if active.selection_policy == "native_first":
        ordered = ([target for target in ordered if target.group == "A"]
                   + [target for target in ordered if target.group != "A"])
    if not ordered:
        return _blocked(profile, active, store, run_id, dropped)
    return Selection(profile=profile, policy=active.selection_policy,
                     targets=tuple(ordered), dropped=tuple(dropped))


def _blocked(
    profile: str,
    policy: RouterPolicy,
    store: AvailabilityStore | None,
    run_id: str | None,
    dropped: list[DroppedTarget],
) -> Selection:
    reasons = [f"{item.deployment_id}: {item.reason}" for item in dropped]
    if not reasons:
        reasons = ["ranking order is empty after the quality floor and drop"]
    if store is not None and run_id:
        write_audit_event(store, run_id, "selection_blocked",
                          f"{profile} [{policy.selection_policy}]: "
                          + "; ".join(reasons[:5]))
    raise NoEligibleTarget(profile, reasons)
