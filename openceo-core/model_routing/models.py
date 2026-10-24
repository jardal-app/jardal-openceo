"""Typed contracts for the free-model router (TASK-2026-00285, TASK-2026-00287).

F0 status report plus the F1 catalog subset: catalog entries with the
three identity levels (SPEC §6.2, §6.3), policy/providers documents
(SPEC §18 subset) and the published rankings
(TASK-2026-00292). Every document is fail-closed (``extra="forbid"``)
and validated on each read. Catalog schema v2 (TASK-2026-00304,
addendum v1.2 §6) pins each deployment to its route
(``route_type``/``allowed_client``) and to the real provider/model ids
as the OpenCode instance sees them.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from model_routing.paths import (
    catalog_path,
    config_dir,
    history_dir,
    policy_path,
    providers_path,
    rankings_path,
    state_dir,
)

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "MODULE_PHASE",
    "POLICY_SCHEMA_VERSION",
    "PROVIDERS_SCHEMA_VERSION",
    "RANKINGS_SCHEMA_VERSION",
    "SCORING_DIMENSIONS",
    "AllowedClient",
    "BenchmarkScore",
    "Capabilities",
    "CatalogDocument",
    "CatalogEntry",
    "Confidentiality",
    "DataPolicy",
    "Deployment",
    "DeploymentLimits",
    "ExperimentalEntry",
    "PendingTop3",
    "ProfileRanking",
    "ProfileScoring",
    "ProviderEntry",
    "ProvidersDocument",
    "RankedDeployment",
    "RankedEntry",
    "RankingProvenance",
    "RankingsDocument",
    "RouteType",
    "RouterPolicy",
    "StatusReport",
    "Usability",
    "collect_status",
    "is_usable",
    "migrate_catalog_v1_to_v2",
    "usability",
]

STATUS_SCHEMA_VERSION = 2
MODULE_PHASE = "F1"
CATALOG_SCHEMA_VERSION = 2
POLICY_SCHEMA_VERSION = 1
PROVIDERS_SCHEMA_VERSION = 1
RANKINGS_SCHEMA_VERSION = 1

SCORING_DIMENSIONS = ("coding", "agentic", "intelligence")


class DataPolicy(str, Enum):
    """Per-deployment training/retention terms (SPEC §14.4)."""

    NO_TRAIN = "no_train"
    ZERO_RETENTION = "zero_retention"
    TRAINS = "trains"
    UNKNOWN = "unknown"


class Confidentiality(str, Enum):
    """Per-project confidentiality levels (SPEC §14.4)."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"


class BenchmarkScore(BaseModel):
    """One normalized quality observation with provenance."""

    model_config = ConfigDict(extra="forbid")

    score: float
    source: str
    observed_at: datetime | None = None


class Capabilities(BaseModel):
    """Deployment capabilities; deny by default until probed."""

    model_config = ConfigDict(extra="forbid")

    tools: bool = False
    streaming: bool = False
    context_tokens: int | None = None
    verified_in_opencode: bool = False


class DeploymentLimits(BaseModel):
    """Known rate/throughput ceilings; null means unknown."""

    model_config = ConfigDict(extra="forbid")

    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None


RouteType = Literal["opencode_native_free", "external_free"]
AllowedClient = Literal["opencode_only", "configured_opencode_or_gateway"]


class Deployment(BaseModel):
    """One provider+model+endpoint+account instance (SPEC §6.2, §6.3).

    v2 route pinning (addendum v1.2 §6): ``route_type``/``allowed_client``
    name the only legitimate path, and ``provider_id``/``model_id`` are
    the real ids as the OpenCode instance sees them. The family stays on
    the entry (``CatalogEntry.family_id``): family, deployment and
    provider ids are never mixed.
    """

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    provider: str
    route_type: RouteType
    allowed_client: AllowedClient
    provider_id: str
    model_id: str
    gateway_model_id: str | None = None
    free_offered: bool = False
    free_verified: bool = False
    healthy: bool = False
    input_price_usd: float | None = None
    output_price_usd: float | None = None
    rate_limit_policy: str | None = None
    data_policy: DataPolicy = DataPolicy.UNKNOWN
    retention_policy: str = "unknown"
    approved_confidentiality: list[Confidentiality] = Field(default_factory=list)
    limits: DeploymentLimits = Field(default_factory=DeploymentLimits)
    last_success_at: datetime | None = None
    state: str = "unknown"
    requires_payment_method: bool = False
    source: str | None = None
    observed_at: datetime | None = None
    stale: bool = False


class CatalogEntry(BaseModel):
    """One canonical model with its deployments (SPEC §6.2)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CATALOG_SCHEMA_VERSION
    family_id: str
    canonical_model_id: str
    display_name: str = ""
    benchmarks: dict[str, BenchmarkScore] = Field(default_factory=dict)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    deployments: list[Deployment] = Field(default_factory=list)


class CatalogDocument(BaseModel):
    """Published catalog; ``stale`` keeps serving the last good content."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = CATALOG_SCHEMA_VERSION
    generated_at: datetime | None = None
    generator: str = ""
    stale: bool = False
    stale_reason: str | None = None
    entries: list[CatalogEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_unique_ids(self):
        seen_entries: set[tuple[str, str]] = set()
        seen_deployments: set[str] = set()
        for entry in self.entries:
            key = (entry.family_id, entry.canonical_model_id)
            if key in seen_entries:
                raise ValueError(f"duplicate catalog entry: {key[0]}/{key[1]}")
            seen_entries.add(key)
            for deployment in entry.deployments:
                if deployment.deployment_id in seen_deployments:
                    raise ValueError(f"duplicate deployment_id: {deployment.deployment_id}")
                seen_deployments.add(deployment.deployment_id)
        return self


def migrate_catalog_v1_to_v2(data: dict) -> dict:
    """Fill v2 route fields on a v1 catalog document; refuses anything else.

    ``opencode`` deployments pin to the native route, every other
    provider to the external one; ``provider_id`` keeps the router
    provider key and ``model_id`` the deployment suffix. Malformed
    input raises ``ValueError`` so the caller fails closed.
    """
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("migration needs a v1 catalog document")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("v1 catalog has no entries list")
    migrated_entries: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("v1 catalog entry is not an object")
        deployments = entry.get("deployments")
        if not isinstance(deployments, list):
            raise ValueError("v1 catalog entry has no deployments list")
        migrated_deployments: list[dict] = []
        for deployment in deployments:
            if not isinstance(deployment, dict):
                raise ValueError("v1 catalog deployment is not an object")
            provider = deployment.get("provider")
            deployment_id = deployment.get("deployment_id")
            if not isinstance(provider, str) or not provider:
                raise ValueError("v1 deployment misses its provider")
            if not isinstance(deployment_id, str) or "/" not in deployment_id:
                raise ValueError(f"v1 deployment id is not provider/model: {deployment_id!r}")
            migrated = dict(deployment)
            if provider == "opencode":
                migrated["route_type"] = "opencode_native_free"
                migrated["allowed_client"] = "opencode_only"
            else:
                migrated["route_type"] = "external_free"
                migrated["allowed_client"] = "configured_opencode_or_gateway"
            migrated["provider_id"] = provider
            migrated["model_id"] = deployment_id.split("/", 1)[1]
            if not migrated.get("gateway_model_id"):
                migrated["gateway_model_id"] = migrated["model_id"]
            migrated_deployments.append(migrated)
        migrated_entry = dict(entry)
        migrated_entry["schema_version"] = CATALOG_SCHEMA_VERSION
        migrated_entry["deployments"] = migrated_deployments
        migrated_entries.append(migrated_entry)
    migrated_doc = dict(data)
    migrated_doc["schema_version"] = CATALOG_SCHEMA_VERSION
    migrated_doc["entries"] = migrated_entries
    return migrated_doc


class ProfileScoring(BaseModel):
    """Per-profile scoring knobs (SPEC §8.3, §8.4); None keeps the default."""

    model_config = ConfigDict(extra="forbid")

    weights: dict[str, float] | None = None
    quality_floor: float | None = None
    max_quality_drop: float | None = None

    @field_validator("weights")
    @classmethod
    def _check_weights(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(SCORING_DIMENSIONS))
        if unknown:
            raise ValueError(f"unknown scoring dimensions: {', '.join(unknown)}")
        if any(weight < 0 for weight in value.values()):
            raise ValueError("scoring weights must be non-negative")
        if sum(value.values()) <= 0:
            raise ValueError("scoring weights must sum above zero")
        return value

    @field_validator("quality_floor", "max_quality_drop")
    @classmethod
    def _check_unit(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError(f"score bound must sit in 0..1: {value}")
        return value


class RouterPolicy(BaseModel):
    """Approved persistent rules; SPEC §18 subset for the catalog stage."""

    model_config = ConfigDict(extra="forbid")

    policy_version: int = POLICY_SCHEMA_VERSION
    strict_zero_cost: bool = True
    allow_paid_fallback: bool = False
    allow_unknown_price: bool = False
    use_only_verified_endpoints: bool = True
    confidentiality_default: Confidentiality = Confidentiality.INTERNAL
    profiles: dict[str, ProfileScoring] = Field(default_factory=dict)


class ProviderEntry(BaseModel):
    """One provider account; never carries keys (checked before parsing)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    account_type: str = "free"
    enabled: bool = True


class ProvidersDocument(BaseModel):
    """Providers and account types, without keys."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = PROVIDERS_SCHEMA_VERSION
    providers: list[ProviderEntry] = Field(default_factory=list)


class StatusReport(BaseModel):
    """Result of ``free-router status``; validated on every read."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = STATUS_SCHEMA_VERSION
    phase: str = MODULE_PHASE
    config_dir: str
    state_dir: str
    config_present: bool
    state_present: bool
    catalog_present: bool
    rankings_present: bool
    policy_present: bool = False
    providers_present: bool = False
    # Latest published catalog snapshot, if any (wired by TASK-2026-00291).
    snapshot: str | None = None


def _latest_snapshot() -> str | None:
    """Newest history snapshot id; None when nothing was published yet."""
    history = history_dir()
    if not history.is_dir():
        return None
    names = sorted(
        path.name for path in history.iterdir()
        if path.is_file() and path.name.startswith("catalog-") and path.name.endswith(".json"))
    if not names:
        return None
    return names[-1][:-5]


def collect_status() -> StatusReport:
    """Read-only filesystem probe behind ``free-router status``."""
    config = config_dir()
    state = state_dir()
    return StatusReport(
        config_dir=str(config),
        state_dir=str(state),
        config_present=config.is_dir(),
        state_present=state.is_dir(),
        catalog_present=catalog_path().is_file(),
        rankings_present=rankings_path().is_file(),
        policy_present=policy_path().is_file(),
        providers_present=providers_path().is_file(),
        snapshot=_latest_snapshot(),
    )


Usability = Literal["usable", "unverified", "unknown_price", "paid", "paywalled"]


def usability(deployment: Deployment, policy: RouterPolicy) -> Usability:
    """Catalog-level gate (SPEC §5, §12.2, §14.2; T11 catalog part).

    Unknown or changed prices (demoted to unverified at publish time)
    keep the deployment out of the usable set; staleness is
    informational so a failed source never empties the catalog.
    """
    if not deployment.free_verified:
        return "unverified"
    if deployment.requires_payment_method:
        return "paywalled"
    prices = (deployment.input_price_usd, deployment.output_price_usd)
    if any(price is None for price in prices) and not policy.allow_unknown_price:
        return "unknown_price"
    if any(price not in (None, 0) for price in prices):
        return "paid"
    return "usable"


def is_usable(deployment: Deployment, policy: RouterPolicy) -> bool:
    """Catalog-level gate only; health/capacity/privacy arrive in 00291/00292."""
    return usability(deployment, policy) == "usable"


class RankedDeployment(BaseModel):
    """One servable route for a ranked canonical model."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    provider: str


class RankedEntry(BaseModel):
    """One ordered canonical model with its primary route and backups (T05)."""

    model_config = ConfigDict(extra="forbid")

    rank: int
    canonical_model_id: str
    family_id: str
    primary: RankedDeployment
    backups: list[RankedDeployment] = Field(default_factory=list)
    quality: float
    coverage: float
    dimensions: dict[str, float] = Field(default_factory=dict)
    quality_sources: list[str] = Field(default_factory=list)


class ExperimentalEntry(BaseModel):
    """Thin-evidence canonical model; never ranked, only listed with a reason."""

    model_config = ConfigDict(extra="forbid")

    canonical_model_id: str
    family_id: str
    reason: str
    observations: int = 0


class PendingTop3(BaseModel):
    """Challenger top-3 held back until it repeats (SPEC §8.3; T19)."""

    model_config = ConfigDict(extra="forbid")

    proposed: list[str]
    previous: list[str]
    first_seen: str
    consecutive_refreshes: int = 1
    reason: str = ""
    catalog_snapshot: str = ""


class RankingProvenance(BaseModel):
    """Where one profile order came from and under which rules."""

    model_config = ConfigDict(extra="forbid")

    catalog_snapshot: str
    catalog_sha256: str
    sources: list[str] = Field(default_factory=list)
    observed_at: str = ""
    weights: dict[str, float] = Field(default_factory=dict)
    quality_floor: float = 0.0
    context_floor: int | None = None
    aa_index_version: float | None = None
    lmarena_target: str | None = None


class ProfileRanking(BaseModel):
    """Effective order for one profile plus its held-back challenger."""

    model_config = ConfigDict(extra="forbid")

    profile: str
    order: list[RankedEntry] = Field(default_factory=list)
    experimental: list[ExperimentalEntry] = Field(default_factory=list)
    pending_top3: PendingTop3 | None = None
    provenance: RankingProvenance


class RankingsDocument(BaseModel):
    """Published per-profile orders; consumers freeze it for the round."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = RANKINGS_SCHEMA_VERSION
    generated_at: datetime | None = None
    generator: str = ""
    catalog_snapshot: str = ""
    catalog_sha256: str = ""
    profiles: dict[str, ProfileRanking] = Field(default_factory=dict)
