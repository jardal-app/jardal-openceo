"""Typed contracts for the free-model router (TASK-2026-00285, TASK-2026-00287).

F0 status report plus the F1 catalog subset: catalog entries with the
three identity levels (SPEC §6.2, §6.3), policy/providers documents
(SPEC §18 subset) and the live-budget ledger. Ranking and availability
models arrive with their own tickets (00291, 00292). Every document is
fail-closed (``extra="forbid"``) and validated on each read.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    "STATUS_SCHEMA_VERSION",
    "BenchmarkScore",
    "Capabilities",
    "CatalogDocument",
    "CatalogEntry",
    "Confidentiality",
    "DataPolicy",
    "Deployment",
    "DeploymentLimits",
    "LiveBudget",
    "ProviderEntry",
    "ProvidersDocument",
    "RouterPolicy",
    "StatusReport",
    "Usability",
    "collect_status",
    "is_usable",
    "usability",
]

STATUS_SCHEMA_VERSION = 2
MODULE_PHASE = "F1"
CATALOG_SCHEMA_VERSION = 1
POLICY_SCHEMA_VERSION = 1
PROVIDERS_SCHEMA_VERSION = 1


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


class Deployment(BaseModel):
    """One provider+model+endpoint+account instance (SPEC §6.2, §6.3)."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    provider: str
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


class RouterPolicy(BaseModel):
    """Approved persistent rules; SPEC §18 subset for the catalog stage."""

    model_config = ConfigDict(extra="forbid")

    policy_version: int = POLICY_SCHEMA_VERSION
    strict_zero_cost: bool = True
    allow_paid_fallback: bool = False
    allow_unknown_price: bool = False
    use_only_verified_endpoints: bool = True
    confidentiality_default: Confidentiality = Confidentiality.INTERNAL
    live_budgets: dict[str, int] = Field(default_factory=dict)


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


class LiveBudget(BaseModel):
    """Daily live-test call accounting for one calendar day (UTC)."""

    model_config = ConfigDict(extra="forbid")

    date: str
    used: dict[str, int] = Field(default_factory=dict)


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
